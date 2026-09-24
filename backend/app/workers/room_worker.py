"""
Trading-room worker — keeps the agent board in session around the clock.

Each cycle picks one symbol and runs the full orchestrator pipeline on it, so
the room has fresh decisions whether or not anyone has the page open:

  1. the pair the user pinned via the room's focus selector, else
  2. the newest un-analysed signal (Telegram, Kronos, technical), else
  3. the next pair in a rotation over recently-active symbols.

Cooldowns stop the same symbol being re-analysed back to back.

OBLIVION is the room's main engine. Alongside the board's cadence an OBLIVION
scan reads every monitored pair (``scope.active_symbols``: pinned pairs, open
positions, taken signals) on a fast clock, stores each read, and convenes the
board the moment a pair confirms a new signal — the board then confirms or
vetoes the engine's call.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from loguru import logger
from sqlalchemy import desc, select

from app.agents import room
from app.core.database import AsyncSessionLocal
from app.models.database import Signal

_task: asyncio.Task | None = None
#: The trade guard runs alongside the board on its own, faster clock.
_guard_task: asyncio.Task | None = None
#: The OBLIVION scan runs beside the board on the engine's own clock.
_oblivion_task: asyncio.Task | None = None
#: Real-time management of open room trades (app.agents.trade_manager).
_manage_task: asyncio.Task | None = None
#: (symbol, timeframe) → signal time the board was last convened for.
_last_oblivion_signal: dict[tuple[str, str], object] = {}
_last_scan: dict[str, object] = {"at": 0.0, "pairs": [], "signals": []}
OBLIVION_SCAN_CONCURRENCY = 4
_running = False
_interval = 300
_cooldown_s = 1800
_focus_interval_s = 300
_focus_timeframe = "1h"
_last_analyzed: dict[str, float] = {}
_rotation_index = 0
_focus_rotation = 0

FALLBACK_ROTATION = ["BTCUSDT", "ETHUSDT", "XAUUSD", "SOLUSDT", "EURUSD"]

# Cadences the focus selector offers, in seconds.
FOCUS_INTERVAL_CHOICES = (300, 900, 3600, 7200, 14400)

# Timeframes the room may be set to analyse on. The room's chart draws the same
# one, so this list is also what the front end offers.
FOCUS_TIMEFRAME_CHOICES = ("5m", "15m", "30m", "1h", "2h", "4h", "1d")


def _cooled_down(symbol: str) -> bool:
    # Never longer than the cadence the user chose: the cooldown exists to stop
    # one pair hogging the rotation, not to overrule "look at this every hour".
    gap = min(_cooldown_s, max(_focus_interval_s, 60))
    last = _last_analyzed.get(symbol)
    return last is None or (time.time() - last) >= gap


def _focus_due(symbol: str) -> bool:
    """A pinned pair runs on its own cadence, not the rotation cooldown.

    The rotation cooldown exists to stop unpinned pairs being re-analysed back
    to back. Applying it to the focused pair too would mean pinning a pair made
    the room look at it *less* often than the interval the user picked.
    """
    last = _last_analyzed.get(symbol)
    return last is None or (time.time() - last) >= _focus_interval_s


def set_focus_interval(seconds: int) -> int:
    """Change the focus cadence live — no restart, no losing the current cycle."""
    global _focus_interval_s
    _focus_interval_s = max(60, int(seconds))
    logger.info(f"🏛️  [TRADING ROOM] Focus cadence set to {_focus_interval_s}s")
    return _focus_interval_s


def set_focus_timeframe(timeframe: str) -> str:
    """Change the timeframe the board analyses on, live."""
    global _focus_timeframe
    tf = (timeframe or "").strip().lower()
    if tf in FOCUS_TIMEFRAME_CHOICES:
        _focus_timeframe = tf
        logger.info(f"🏛️  [TRADING ROOM] Analysis timeframe set to {tf}")
    return _focus_timeframe


def get_focus_timeframe() -> str:
    """The timeframe the room analyses on — the one the settings page set.

    Every surface that shows the room reads this rather than its own default,
    so the chart on the wall, the agents' candles and the levels they publish
    are all the same timeframe.
    """
    return _focus_timeframe


def _tradeable(symbol: str) -> bool:
    """Is this string an instrument at all?

    Signal rows carry whatever the parsers produced, and a channel's parse noise
    ("NOK") became a full board meeting and a published trade plan. Anything the
    resolver cannot classify is not something to convene on.
    """
    try:
        from app.services import market_data

        return market_data.classify(market_data.normalize_symbol(symbol)) != market_data.UNKNOWN
    except Exception:  # noqa: BLE001 — a resolver failure is not a veto
        return True


async def _recent_signal_symbol(db) -> Optional[str]:
    """Newest signal whose symbol is off cooldown."""
    result = await db.execute(select(Signal).order_by(desc(Signal.created_at)).limit(25))
    for signal in result.scalars():
        if signal.symbol and _tradeable(signal.symbol) and _cooled_down(signal.symbol):
            return signal.symbol
    return None


async def _rotation_symbol(db) -> Optional[str]:
    """Next pair in the rotation — recently-signalled symbols, else the fallback list."""
    global _rotation_index
    result = await db.execute(select(Signal.symbol).order_by(desc(Signal.created_at)).limit(80))
    seen: list[str] = []
    for (symbol,) in result:
        if symbol and symbol not in seen and _tradeable(symbol):
            seen.append(symbol)
    pool = seen or FALLBACK_ROTATION

    for _ in range(len(pool)):
        symbol = pool[_rotation_index % len(pool)]
        _rotation_index += 1
        if _cooled_down(symbol):
            return symbol
    return None


async def _focus_signal_symbol(db, focus: list[str]) -> Optional[str]:
    """A focused pair with a telegram/system signal newer than our last look.

    When a pair is pinned we still want its fresh signals acted on promptly,
    not just on the rotation clock — so a new signal on a focused pair jumps
    the queue for the next cycle.
    """
    if not focus:
        return None
    wanted = {s.replace("/", "").upper(): s for s in focus}
    result = await db.execute(select(Signal).order_by(desc(Signal.created_at)).limit(50))
    for sig in result.scalars():
        key = (sig.symbol or "").replace("/", "").upper()
        if key not in wanted:
            continue
        ts = sig.created_at.timestamp() if sig.created_at else 0.0
        if ts > _last_analyzed.get(wanted[key], 0.0):
            return wanted[key]
    return None


async def _pick_symbol(db) -> tuple[Optional[str], str]:
    focus = room.get_focus_symbols()
    if focus:
        # A fresh signal on a pinned pair is analysed immediately.
        sig_symbol = await _focus_signal_symbol(db, focus)
        if sig_symbol:
            return sig_symbol, "focus_signal"
        # Otherwise rotate through the pinned pairs on the focus cadence.
        global _focus_rotation
        for _ in range(len(focus)):
            symbol = focus[_focus_rotation % len(focus)]
            _focus_rotation += 1
            if _focus_due(symbol):
                return symbol, "focus"
        # Pinned but none due yet — stay on the pinned set rather than wandering
        # off to other pairs.
        return None, "focus_waiting"

    symbol = await _recent_signal_symbol(db)
    if symbol:
        return symbol, "signal"
    return await _rotation_symbol(db), "rotation"


async def _review_open_positions(db) -> None:
    """Keep the Risk Manager + Position Reviewer working on open positions and
    pending orders.

    Runs every cycle so a linked account with positions — or a resting limit
    order — is re-checked on the chosen room cadence. Every reviewer
    early-returns cheaply when there is nothing to manage, so this spends no
    tokens unless there is real work.
    """
    from app.agents.orchestrator import AgentOrchestrator

    # Re-review a position roughly once per chosen focus interval.
    min_hold = max(0.05, _focus_interval_s / 3600.0)
    for fn, label in (
        (AgentOrchestrator.analyze_positions, "crypto"),
        (AgentOrchestrator.analyze_sim_positions, "sim"),
    ):
        try:
            await fn(db, min_hold_hours=min_hold)
        except Exception as exc:  # noqa: BLE001 - a bad review must not kill the loop
            logger.debug(f"🏛️  [TRADING ROOM] {label} position review skipped: {exc}")

    # Pending limit orders: confirm the entry is still optimal, adjust for a
    # better fill, or cancel if the setup has gone.
    try:
        await AgentOrchestrator.analyze_limit_orders(db, min_age_minutes=5.0)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"🏛️  [TRADING ROOM] limit-order review skipped: {exc}")

    # MT5 is a linked account like any other. The reviewers used to see only
    # crypto and sim, so a room-placed broker position was managed by nothing
    # but its original stop once the meeting ended.
    try:
        await AgentOrchestrator.analyze_mt5_positions(db, min_hold_hours=min_hold)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"🏛️  [TRADING ROOM] MT5 position review skipped: {exc}")


async def _guard_loop() -> None:
    """Watch open trades and live signals on a fast, fixed cadence.

    Deliberately separate from the board's cycle. The room may be set to meet
    once an hour — a sensible cadence for forming an opinion — but a stop about
    to be swept, or a setup that has just been invalidated, is decided in
    minutes. Tying the two together meant every trade was unattended for up to
    an hour at exactly the moments that decide it.
    """
    from app.agents import guardian

    logger.info(
        f"🛡  [TRADING ROOM] Trade guard active — every {guardian.GUARD_INTERVAL_S}s"
    )
    while _running:
        try:
            async with AsyncSessionLocal() as db:
                summary = await guardian.guard_cycle(db)
            acted = sum(
                len(summary.get(part) or [])
                for part in ("positions", "crypto", "signals")
            )
            if acted:
                logger.info(f"🛡  [TRADING ROOM] Guard acted on {acted} trade(s)")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a bad pass must not end the watch
            logger.warning(f"🛡  [TRADING ROOM] Guard cycle failed: {exc}")
        try:
            await asyncio.sleep(guardian.GUARD_INTERVAL_S)
        except asyncio.CancelledError:
            raise


def oblivion_scan_interval() -> int:
    """How often the scan loop wakes: a quarter of the finest bar, 1–5 minutes.
    Each timeframe is then only re-read on its own cadence (``tf_due``)."""
    from app.agents.oblivion_gate import tf_seconds
    from app.signals.oblivion_profiles import ROOM_SCAN_TIMEFRAMES

    finest = min(tf_seconds(tf) for tf in ROOM_SCAN_TIMEFRAMES)
    return int(max(60, min(300, finest // 4)))


_last_tf_scan: dict[str, float] = {}


def tf_due(tf: str, now: float | None = None) -> bool:
    """A timeframe is re-read every quarter bar, at most every 15 minutes."""
    from app.agents.oblivion_gate import tf_seconds

    now = now or time.time()
    every = max(60, min(900, tf_seconds(tf) // 4))
    return now - _last_tf_scan.get(tf, 0.0) >= every - 5


async def _scan_pairs(db) -> list[str]:
    """Pinned pairs plus the OBLIVION trading universe when it is switched on."""
    from app.agents import pair_gate

    return await pair_gate.room_symbols(db)


async def oblivion_scan(db) -> list[dict]:
    """Read every room pair through OBLIVION on each timeframe that is due.

    Only (pair, timeframe) combinations that are approved by their backtest —
    or not yet backtested — are read; every read is stored. Returns the reads
    carrying a fresh confirmed signal the board has not yet been convened for,
    slower timeframes first.
    """
    from app.agents import oblivion_gate, pair_gate
    from app.signals.oblivion_profiles import ROOM_SCAN_TIMEFRAMES

    pairs = await _scan_pairs(db)
    now = time.time()
    due = [tf for tf in ROOM_SCAN_TIMEFRAMES if tf_due(tf, now)]
    sem = asyncio.Semaphore(OBLIVION_SCAN_CONCURRENCY)

    async def _one(sym: str, tf: str):
        async with sem:
            try:
                return sym, tf, await oblivion_gate.read_pair(sym, tf, source="room_scan")
            except Exception as exc:  # noqa: BLE001 — one pair must not stop the scan
                logger.debug(f"🏛️  [OBLIVION] scan of {sym} {tf} failed: {exc}")
                return sym, tf, None

    tradeable: set = set()
    untested: dict[str, list[str]] = {}
    to_read: list[tuple[str, str]] = []
    for p in pairs:
        ok, _ = await pair_gate.supported(db, p)
        for tf in ROOM_SCAN_TIMEFRAMES:
            row = await pair_gate.approval(db, p, tf)
            if row is None:
                untested.setdefault(tf, []).append(p)
            elif ok and row.approved:
                tradeable.add((p, tf))
                if tf in due:
                    to_read.append((p, tf))
    await _backtest_untested(untested)
    for tf in due:
        _last_tf_scan[tf] = now

    fresh: list[dict] = []
    for sym, tf, read in await asyncio.gather(*(_one(p, tf) for p, tf in to_read)):
        verdict = (read or {}).get("verdict") or {}
        if not verdict.get("tradeable"):
            continue
        if _last_oblivion_signal.get((sym, tf)) == verdict.get("signal_time"):
            continue
        from app.signals.news_gate import live_block

        news = await live_block(sym)
        if news is not None:
            # Not consumed: once the cool-off is over a still-fresh signal is taken.
            logger.info(f"🧿 [OBLIVION] {sym} {tf} signal waits — {news['reason']}")
            continue
        fresh.append({"symbol": sym, "timeframe": tf, "verdict": verdict,
                      "read_id": (read or {}).get("read_id")})
    order = {tf: i for i, tf in enumerate(ROOM_SCAN_TIMEFRAMES)}
    fresh.sort(key=lambda f: (order.get(f["timeframe"], 9), -(f["verdict"].get("signal_time") or 0)))
    _last_scan.update(at=now, pairs=pairs, timeframes=list(ROOM_SCAN_TIMEFRAMES), due=due,
                      reads=len(to_read), tradeable=sorted(f"{p} {tf}" for p, tf in tradeable),
                      signals=[f"{f['symbol']} {f['timeframe']}" for f in fresh])
    if not pairs:
        logger.info("🧿 [OBLIVION] no pair is pinned or in the universe — nothing to monitor")
    return fresh


_backtest_requested: dict[str, float] = {}


async def _backtest_untested(untested: dict[str, list[str]]) -> None:
    """Pinned pairs with no backtest yet get one — the report goes to Telegram."""
    from sqlalchemy import select

    from app.backtest import service
    from app.core.database import AsyncSessionLocal
    from app.models.database import BacktestRun

    async with AsyncSessionLocal() as db:
        busy = {r for (r,) in (await db.execute(
            select(BacktestRun.timeframe).where(BacktestRun.status == "running"))).all()}
    for tf, syms in untested.items():
        key = f"{tf}:{','.join(sorted(syms))}"
        if tf in busy:
            continue  # a run on this timeframe is queued or running — its approvals land when it ends
        if time.time() - _backtest_requested.get(key, 0) < 12 * 3600:
            continue
        _backtest_requested[key] = time.time()
        params = {"timeframe": tf, "start_balance": 10000.0, "risk_pct": 1.0,
                  "bars": 20000 if tf in ("5m", "15m", "30m") else 5000, "agents_rule": "opposite",
                  "use_room_profile": True}
        try:
            run_id = await service.start(syms, params, send_telegram=True)
            logger.info(f"🧿 [OBLIVION] backtesting untested pinned pairs {syms} on {tf} (run #{run_id})")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"🧿 [OBLIVION] could not start a backtest for {syms}: {exc}")


async def _oblivion_loop() -> None:
    """OBLIVION leads: scan the monitored pairs and convene on a new signal."""
    await asyncio.sleep(20)  # let the API finish starting
    logger.info(f"🧿 [OBLIVION] Room scan active — every {oblivion_scan_interval()}s")
    while _running:
        try:
            async with AsyncSessionLocal() as db:
                fresh = await oblivion_scan(db)
                for item in fresh:
                    sym = item["symbol"]
                    _last_oblivion_signal[(sym, item["timeframe"])] = item["verdict"].get("signal_time")
                    _last_analyzed[sym] = time.time()
                    logger.info(
                        f"🧿 [OBLIVION] {sym} {item['timeframe']}: {item['verdict'].get('reason')} — convening the board"
                    )
                    from app.agents.orchestrator import AgentOrchestrator

                    # "oblivion": an automated trigger that is not bound by the
                    # pinned-pair focus lock — universe pairs convene too.
                    await AgentOrchestrator.analyze_symbol(
                        db, sym, timeframe=item["timeframe"], trigger="oblivion",
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a bad scan must not end the watch
            logger.warning(f"🧿 [OBLIVION] scan failed: {exc}")
        try:
            await asyncio.sleep(oblivion_scan_interval())
        except asyncio.CancelledError:
            raise


async def _loop() -> None:
    global _running
    _running = True
    logger.info(f"🏛️  [TRADING ROOM] Worker started — cycle every {_interval}s")

    while _running:
        try:
            async with AsyncSessionLocal() as db:
                symbol, reason = await _pick_symbol(db)
                if symbol:
                    _last_analyzed[symbol] = time.time()
                    logger.info(f"🏛️  [TRADING ROOM] Convening on {symbol} ({reason})")
                    from app.agents.orchestrator import AgentOrchestrator

                    await AgentOrchestrator.analyze_symbol(
                        db,
                        symbol,
                        timeframe=_focus_timeframe,
                        trigger="room",
                    )
                else:
                    logger.debug(f"🏛️  [TRADING ROOM] Nothing to analyse ({reason})")

                # Risk Manager + Position Reviewer stay on the desk: re-check any
                # open positions on linked accounts every cycle (cheap when flat).
                await _review_open_positions(db)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a bad cycle must not kill the loop
            logger.warning(f"[TRADING ROOM] Cycle failed: {exc}")

        # "Re-analyse the focused pair every …" is THE cadence of this room —
        # including when the board is kept meeting 24/7. The rotation interval
        # is only ever a ceiling for the unpinned case, and it used to override
        # the chosen cadence the moment no pair happened to be pinned, so a
        # 5-minute setting silently became the 5-minute default and a 4-hour
        # setting silently became five minutes. One number, both cases.
        delay = _focus_interval_s if room.get_focus_symbols() else min(_interval, _focus_interval_s)
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise

    logger.info("🏛️  [TRADING ROOM] Worker stopped")


def start_room_worker(
    interval: int = 300,
    cooldown: int = 1800,
    focus_interval: int | None = None,
) -> bool:
    global _task, _guard_task, _interval, _cooldown_s, _running
    if _task is not None and not _task.done():
        logger.warning("Trading room worker already running")
        return False
    _interval = max(60, interval)
    _cooldown_s = max(_interval, cooldown)
    if focus_interval is not None:
        set_focus_interval(focus_interval)
    _running = True
    _task = asyncio.create_task(_loop())
    _guard_task = asyncio.create_task(_guard_loop())
    global _oblivion_task, _manage_task
    _oblivion_task = asyncio.create_task(_oblivion_loop())
    from app.agents import trade_manager

    _manage_task = asyncio.create_task(trade_manager.run_forever(lambda: _running))
    return True


async def arm_from_settings(interval: int, cooldown: int) -> bool:
    """Start the worker if the saved room settings say it should be running.

    The env flag is a floor, not a ceiling: once someone has armed the board
    from the settings page that choice is what survives a restart, which is the
    whole point of persisting it.
    """
    try:
        from app.agents.execution import get_settings

        async with AsyncSessionLocal() as db:
            s = await get_settings(db)
            enabled = bool(getattr(s, "worker_enabled", True))
            focus_interval = int(getattr(s, "focus_interval_s", 300))
            saved_focus = getattr(s, "focus_symbol", None)
            set_focus_timeframe(str(getattr(s, "focus_timeframe", "1h") or "1h"))
    except Exception as exc:  # noqa: BLE001 - never block startup on this
        logger.warning(f"[TRADING ROOM] Could not read room settings, staying idle: {exc}")
        return False

    # Re-pin the pair(s) the user chose before the restart (comma-joined form).
    if saved_focus:
        await room.set_focus(saved_focus)
        logger.info(f"🏛️  [TRADING ROOM] Restored focus on {', '.join(room.get_focus_symbols())}")

    if not enabled:
        logger.info("🏛️  [TRADING ROOM] Worker disabled in room settings — not starting")
        return False
    return start_room_worker(interval, cooldown, focus_interval=focus_interval)


def stop_room_worker() -> bool:
    global _task, _guard_task, _oblivion_task, _manage_task, _running
    if not _running and (_task is None or _task.done()):
        return False
    _running = False
    if _task:
        _task.cancel()
        _task = None
    if _guard_task:
        _guard_task.cancel()
        _guard_task = None
    if _oblivion_task:
        _oblivion_task.cancel()
        _oblivion_task = None
    if _manage_task:
        _manage_task.cancel()
        _manage_task = None
    return True


def room_worker_status() -> dict:
    focus_symbols = room.get_focus_symbols()
    # Countdown reflects the soonest pinned pair due for re-analysis.
    next_in = 0
    if focus_symbols:
        dues = [
            max(0, round(_focus_interval_s - (time.time() - _last_analyzed[s])))
            for s in focus_symbols if s in _last_analyzed
        ]
        next_in = min(dues) if dues else 0
    return {
        "running": _running and _task is not None and not _task.done(),
        "guard_running": _running and _guard_task is not None and not _guard_task.done(),
        "oblivion_scan_running": _running and _oblivion_task is not None and not _oblivion_task.done(),
        "trade_manager_running": _running and _manage_task is not None and not _manage_task.done(),
        "oblivion_scan_interval_s": oblivion_scan_interval(),
        "oblivion_last_scan": dict(_last_scan),
        "interval_s": _interval,
        "cooldown_s": _cooldown_s,
        "focus_interval_s": _focus_interval_s,
        "focus_timeframe": _focus_timeframe,
        "focus_symbol": room.get_focus_symbol(),
        "focus_symbols": focus_symbols,
        "focus_next_in_s": next_in,
        "recently_analyzed": sorted(_last_analyzed, key=_last_analyzed.get, reverse=True)[:10],
    }
