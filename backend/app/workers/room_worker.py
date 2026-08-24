"""
Trading-room worker — keeps the agent board in session around the clock.

Each cycle picks one symbol and runs the full orchestrator pipeline on it, so
the room has fresh decisions whether or not anyone has the page open:

  1. the pair the user pinned via the room's focus selector, else
  2. the newest un-analysed signal (Telegram, Kronos, technical), else
  3. the next pair in a rotation over recently-active symbols.

Cooldowns stop the same symbol being re-analysed back to back.
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
FOCUS_TIMEFRAME_CHOICES = ("5m", "15m", "30m", "1h", "4h", "1d")


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
    global _task, _guard_task, _running
    if not _running and (_task is None or _task.done()):
        return False
    _running = False
    if _task:
        _task.cancel()
        _task = None
    if _guard_task:
        _guard_task.cancel()
        _guard_task = None
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
        "interval_s": _interval,
        "cooldown_s": _cooldown_s,
        "focus_interval_s": _focus_interval_s,
        "focus_timeframe": _focus_timeframe,
        "focus_symbol": room.get_focus_symbol(),
        "focus_symbols": focus_symbols,
        "focus_next_in_s": next_in,
        "recently_analyzed": sorted(_last_analyzed, key=_last_analyzed.get, reverse=True)[:10],
    }
