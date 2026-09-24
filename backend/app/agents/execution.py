"""
Trading-room execution — turns an agreed board decision into a real order.

Every order passes the same gauntlet, in order, and any failure stops it dead:

  1. execution_enabled          — the master switch on the settings page
  2. venue allowed             — sim / crypto / MT5 each opt in separately
  3. consensus + confidence    — the board must actually agree
  4. daily trade cap           — a runaway loop cannot drain the account
  5. open-position cap         — bounded concurrent exposure
  6. risk sizing               — size derived from equity x risk%, never guessed
  7. OBLIVION re-check         — the engine re-reads the pair on current bars;
                                 no fresh confirmed signal on this side, no order
  8. dry_run                   — live trading off: demo/paper accounts only

``dry_run`` is a *routing* switch, not a mute button. On, the demo (or paper)
account takes every trade for real and the live account is never touched — so
the desk builds a record you can actually judge instead of a log of orders that
never existed. Off, demo and live take the same trade at the same moment, which
keeps the demo a running mirror of the real account rather than a history that
stops the day you arm it.

Sizing is always derived from live account equity and the stop distance, so a
wider stop buys a smaller position rather than a bigger loss — and each account
is sized on its own equity, never copied lot-for-lot from the other.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import brain_log, oblivion_gate
from app.core.events import Topics, event_bus
from app.models.database import RoomSettings, Signal
from app.trading.order_tags import SOURCE_ROOM, build_comment

# Rolling count of orders placed today, reset when the date rolls over.
_today: str = ""
_trades_today: int = 0


async def get_settings(db: AsyncSession) -> RoomSettings:
    row = (await db.execute(select(RoomSettings).where(RoomSettings.id == 1))).scalar_one_or_none()
    if row is None:
        row = RoomSettings(id=1)
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


def _bump_daily_counter() -> int:
    global _today, _trades_today
    today = time.strftime("%Y-%m-%d")
    if today != _today:
        _today = today
        _trades_today = 0
    _trades_today += 1
    return _trades_today


def trades_today() -> int:
    return _trades_today if time.strftime("%Y-%m-%d") == _today else 0


#: Symbol → when the room last sent an order for it. The board's decision and
#: the published card are two paths to the same trade; without this a pair that
#: the room decided *and* published would go out twice.
_recent_orders: Dict[str, float] = {}
REORDER_GUARD_S = 900.0


def _note_order(symbol: str) -> None:
    _recent_orders[(symbol or "").upper().replace("/", "")] = time.time()


def ordered_recently(symbol: str) -> bool:
    last = _recent_orders.get((symbol or "").upper().replace("/", ""))
    return last is not None and (time.time() - last) < REORDER_GUARD_S


def forget_orders() -> None:
    """Clear the re-order guard — for tests."""
    _recent_orders.clear()


class Blocked(Exception):
    """A gate refused the order. The reason is surfaced to the room."""


def _mt5_ticket_from(result: Any) -> Optional[str]:
    """The broker's ticket for a placed order, or None when it opened nothing."""
    if not isinstance(result, dict):
        return None
    for key in ("ticket", "order", "orderId", "id"):
        value = result.get(key)
        if value:
            return str(value)
    inner = result.get("orderInternal")
    if isinstance(inner, dict) and inner.get("ticket"):
        return str(inner["ticket"])
    return None


def _mt5_symbol(symbol: str) -> str:
    """Normalise a symbol to the MT5/CFD format Exness (and similar) uses.

    Exchange-format symbols use USDT as the quote (BTCUSDT, ETH/USDT); MT5
    brokers list the same instruments against USD (BTCUSD, ETHUSD). Stripping
    the slash and dropping the trailing T on USDT is safe for any pair:
    XAUUSD and EURUSD pass through unchanged because they don't end in USDT.
    """
    s = (symbol or "").upper().replace("/", "")
    if s.endswith("USDT") and len(s) > 4:
        return s[:-1]   # BTCUSDT → BTCUSD
    if s.endswith("USDC") and len(s) > 4:
        return s[:-4] + "USD"   # BTCUSDC → BTCUSD
    return s


def _margin_headroom(account: Any, s: Any) -> Optional[float]:
    """Free margin a new order may use so the account's margin level stays at
    or above the room's warning level (default 300%) once it is open.

    Many concurrent trades at 1–5% risk each would otherwise walk the account
    toward stop-out one fill at a time. ``affordable_mt5_lot`` spends 80% of
    what it is given, hence the division.
    """
    free = getattr(account, "free_margin", None)
    try:
        equity = float(getattr(account, "equity", 0) or 0)
        used = float(getattr(account, "margin", 0) or 0)
        warn = float(getattr(s, "margin_warn_pct", 300.0) or 300.0)
        if equity <= 0 or warn <= 0:
            return free
        headroom = max(0.0, equity / (warn / 100.0) - used) / 0.8
        return min(float(free), headroom) if free is not None else headroom
    except (TypeError, ValueError):
        return free


async def quote_to_usd(symbol: str, price: float) -> float:
    """Multiply a quote-currency amount by this to get USD.

    XXXUSD (and metals, US indices, crypto CFDs) → 1; USDXXX → 1/price;
    crosses (…JPY, …CHF, …CAD) → 1/USD{quote} from a live quote.
    """
    s = _mt5_symbol(symbol)
    if len(s) != 6 or not s.isalpha() or s.startswith(("XAU", "XAG")):
        return 1.0
    base, quote = s[:3], s[3:]
    if quote == "USD":
        return 1.0
    if base == "USD":
        return 1.0 / price if price else 1.0
    try:
        from app.services import market_data

        q = await market_data.get_quote(f"USD{quote}")
        rate = float(getattr(q, "price", 0) or 0)
        if rate > 0:
            return 1.0 / rate
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[room-exec] no USD{quote} rate to size {symbol}: {exc}")
    raise Blocked(f"cannot convert {quote} to USD to size {symbol} — no USD{quote} quote")


def _round_lot(volume: float) -> float:
    """Brokers reject odd volumes — snap to 0.01 lots, floor at the minimum."""
    return max(0.01, round(volume / 0.01) * 0.01)


def _contract_size(symbol: str) -> float:
    """Units per lot. Prefers the MT5 plugin's table; mirrors it when absent."""
    try:
        from plugins.MT5TradingPlugin.backend.services.smc_strategy import contract_size_for_symbol
        return contract_size_for_symbol(symbol)
    except Exception:  # noqa: BLE001 - plugin-optional
        s = (symbol or "").upper().replace("/", "")
        if s.startswith("XAU"):
            return 100.0
        if s.startswith("XAG"):
            return 5000.0
        if len(s) == 6 and s.isalpha():
            return 100000.0
        return 100.0


def mt5_volume_for_risk(
    *, equity: float, risk_pct: float, entry: float, stop_loss: float, symbol: str,
) -> float:
    """Lots such that entry→SL costs about ``risk_pct``% of equity.

    volume = risk_amount / (stop_distance x contract_size)
    """
    distance = abs(entry - stop_loss)
    if distance <= 0:
        raise Blocked("stop loss equals entry — cannot size the trade")
    contract = _contract_size(symbol) or 1.0
    risk_amount = max(equity, 0.0) * (risk_pct / 100.0)
    loss_per_lot = distance * contract
    if loss_per_lot <= 0:
        raise Blocked("cannot derive risk per lot for this symbol")
    return _round_lot(risk_amount / loss_per_lot)


async def effective_risk_pct(s: RoomSettings, symbol: str) -> float:
    """Configured risk after cycle and metal-volatility reductions.

    Two independent dampers stack:
    1. Bitcoin cycle's auto reduction (when enabled) for cycle-driven symbols.
    2. Metals dampener: XAU/XAG move $40-100 per day on 0.01 lot; a flat 1% on a
       $4M demo is $40k of absolute risk, which is how 2026-08-28 produced $50k
       single-trade losses. Metals are sized at 0.45× the configured risk so a
       1% setting risks 0.45% on gold/silver until a dollar cap (below) engages.
    Both only shrink. A calendar that fails to resolve leaves risk untouched:
    sizing must never depend on the cycle resolving.
    """
    base = float(getattr(s, "risk_pct", 1.0) or 1.0)
    # Metals dampener — always on, not gated by cycle_auto_risk
    sym_norm = (symbol or "").upper().replace("/", "")
    if sym_norm.startswith("XAU") or sym_norm.startswith("XAG"):
        base = round(base * 0.45, 4)  # 1% → 0.45% on metals
    if not bool(getattr(s, "cycle_auto_risk", False)):
        return base
    try:
        from app.services import market_cycle

        if not market_cycle.cycle_applies(symbol):
            return base
        snap = await market_cycle.resolve_cycle_snapshot(
            bull_days=int(getattr(s, "cycle_bull_days", 0) or 0) or None,
            bear_days=int(getattr(s, "cycle_bear_days", 0) or 0) or None,
        )
        if snap is None or not snap.ok:
            return base
        if snap.phase == "bear" or snap.late_phase:
            mult = float(getattr(s, "cycle_risk_multiplier", 0.5) or 0.5)
            return round(base * max(0.0, min(1.0, mult)), 4)
    except Exception:  # noqa: BLE001 — a calendar outage is not a sizing input
        return base
    return base


def _seat(decisions: list[dict], role: str) -> dict:
    for d in decisions or []:
        if str(d.get("agent_role") or "").lower() == role:
            return d
    return {}


def _positive(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


async def _levels_from(
    decisions: list[dict], price: float, action: str, symbol: str = "",
    oblivion: Optional[Dict[str, Any]] = None,
) -> tuple[float, float, str]:
    """The stop and target this order goes out with, and how they were derived.

    Three things had to change here, and each of them was losing money on its own:

    *The levels the board actually chose were being thrown away.* The seats quote
    absolute prices — the same ones the published card and the drawn chart carry —
    and this function ignored them in favour of a flat 2%/4% band. On gold that is
    a ~90-point stop where the plan said 26, so the order that went to the broker
    was not the trade the room had agreed, and the chart the user was watching
    described neither.

    *The nearest target was being used as the exit.* An order whose take-profit is
    TP1 is closed at TP1, which is precisely "closing the trade too early if the
    market favours it". The ladder's furthest rung is what the position is sized
    and held for; the trailing stop is what protects the way there.

    *Nothing checked whether the stop could survive ordinary noise.* See
    :mod:`app.trading.stop_quality` — a stop inside one average bar is hit by an
    average bar, and widening it here costs nothing, because the sizer derives
    volume from the stop distance.
    """
    is_long = action == "buy"
    executor, signal = _seat(decisions, "trade_executor"), _seat(decisions, "signal_generator")

    zone = [z for z in (_positive(v) for v in (signal.get("entry_zone") or [])) if z]
    entry = (
        _positive(executor.get("entry")) or _positive(executor.get("entry_price"))
        or (sum(zone) / len(zone) if len(zone) >= 2 else None)
        or _positive(signal.get("entry_price")) or price
    )

    stop = _positive(executor.get("stop_loss")) or _positive(signal.get("stop_loss"))
    targets = [t for t in (_positive(v) for v in (signal.get("take_profits") or [])) if t]
    if target := _positive(executor.get("take_profit")):
        targets.append(target)
    # The furthest rung ahead of the entry — the one the plan is actually for.
    ahead = [t for t in targets if (t > entry if is_long else t < entry)]
    take_profit = (max(ahead) if is_long else min(ahead)) if ahead else None

    # The risk seat reviews the signal seat, so its numbers win where it gave
    # them — in absolute terms if it quoted a level, in percent if it did not.
    for d in decisions or []:
        if (adjusted := _positive(d.get("adjusted_sl"))) is not None:
            stop = adjusted
        if (adjusted := _positive(d.get("adjusted_tp"))) is not None:
            take_profit = adjusted

    # Percentages only fill what the board left blank, and only against the price
    # the order is going out at. Read from every seat, newest wins, because the
    # risk manager's tightening arrives after the signal seat's proposal.
    sl_pct = _positive(signal.get("stop_loss_pct")) or 2.0
    tp_pct = _positive(signal.get("take_profit_pct")) or 4.0
    for d in decisions or []:
        sl_pct = _positive(d.get("stop_loss_pct")) or sl_pct
        tp_pct = _positive(d.get("take_profit_pct")) or tp_pct
        sl_pct = _positive(d.get("adjusted_sl_pct")) or sl_pct
        tp_pct = _positive(d.get("adjusted_tp_pct")) or tp_pct
    source = "board levels"
    # The OBLIVION engine's own plan fills what the board left blank before any
    # percentage does: its stop is the structural swing (or 1.5×ATR) and its
    # TP3 is the 3R rung — the levels the chart is drawing. Only a same-side
    # signal counts; a plan for the other direction is not this trade's plan.
    obl_sig = (oblivion or {}).get("signal") or {}
    if obl_sig.get("entry") is None:
        obl_sig = (oblivion or {}).get("last_signal") or {}
    if obl_sig.get("action") == action and obl_sig.get("entry") is not None:
        if stop is None and (obl_stop := _positive(obl_sig.get("stop_loss"))) is not None:
            if (is_long and obl_stop < price) or (not is_long and obl_stop > price):
                stop = obl_stop
                source = "Oblivion stop"
        if take_profit is None:
            obl_tps = [t for t in (_positive(obl_sig.get(k)) for k in ("tp1", "tp2", "tp3")) if t]
            obl_ahead = [t for t in obl_tps if (t > price if is_long else t < price)]
            if obl_ahead:
                take_profit = max(obl_ahead) if is_long else min(obl_ahead)
                source = "Oblivion levels" if source == "Oblivion stop" else f"{source}, Oblivion target"
    if stop is None:
        stop = price * (1 - sl_pct / 100) if is_long else price * (1 + sl_pct / 100)
        source = "percentage fallback"
    if take_profit is None:
        take_profit = price * (1 + tp_pct / 100) if is_long else price * (1 - tp_pct / 100)
        source = "percentage fallback" if source != "board levels" else "board stop, derived target"

    # ── Is that stop far enough from the entry to be measuring the trade? ──
    try:
        from app.services import candles as candle_source
        from app.trading import stop_quality

        bars = await candle_source.fetch(symbol, "1h") if symbol else []
        assessment = stop_quality.assess(
            entry=price, proposed_stop=stop, is_long=is_long, candles=bars,
        )
        if assessment and assessment.widened:
            stop = assessment.stop
            source += f"; stop widened to the volatility floor ({assessment.reason})"
    except Exception as exc:  # noqa: BLE001 — a failed check must not block the order
        logger.debug(f"[room-exec] stop-quality check skipped for {symbol}: {exc}")

    return stop, take_profit, source


async def oblivion_recheck(
    s: RoomSettings, *, symbol: str, action: str, timeframe: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Re-read OBLIVION on the pair's current bars; raise ``Blocked`` unless it
    still has a fresh, confirmed signal on ``action``'s side.

    This is where "the engine saw this pair" is proven: the re-read is stored
    in ``oblivion_reads``. When the feed cannot produce 60 bars the stored read
    of the same pair and timeframe stands in — the room never trades blind.
    """
    tf = str(timeframe or getattr(s, "focus_timeframe", None) or "1h").lower()
    fresh = await oblivion_gate.read_pair(symbol, tf, source="execution_recheck", session_id=session_id)
    verdict = fresh.get("verdict") or {}
    read_id = fresh.get("read_id")
    if (fresh.get("result") or {}).get("error"):
        max_age = max(2 * int(getattr(s, "focus_interval_s", 300) or 300), 2 * oblivion_gate.tf_seconds(tf))
        stored = await oblivion_gate.latest_read(symbol, tf, max_age)
        if stored is None:
            raise Blocked(f"OBLIVION has not seen {symbol} on {tf} recently — no fresh read, no trade")
        if not stored.tradeable or stored.signal_action != action:
            raise Blocked(
                f"OBLIVION's last read of {symbol} {tf} does not support {action.upper()}: "
                f"{stored.verdict_reason}"
            )
        verdict = {
            "tradeable": True, "action": stored.signal_action, "kind": stored.signal_kind,
            "entry": stored.entry, "stop_loss": stored.stop_loss, "tp1": stored.tp1,
            "tp2": stored.tp2, "tp3": stored.tp3, "reason": stored.verdict_reason,
        }
        read_id = stored.id
    else:
        if not verdict.get("tradeable"):
            raise Blocked(f"OBLIVION re-check: {verdict.get('reason')}")
        if verdict.get("action") != action:
            raise Blocked(
                f"OBLIVION re-check says {str(verdict.get('action')).upper()}, not {action.upper()}"
            )
    return {"verdict": verdict, "read_id": read_id, "timeframe": tf, "price": fresh.get("price")}


async def _room_entry_gates(db: AsyncSession, symbol: str, timeframe: Optional[str]) -> None:
    """The backtest must have approved this pair/timeframe, and the weekly
    target must not have paused new entries."""
    from app.agents import pair_gate, target_pacer
    from app.signals.oblivion_profiles import profile_for

    if timeframe and profile_for(timeframe):
        row = await pair_gate.approval(db, symbol, timeframe)
        if row is None:
            raise Blocked(f"{symbol} has not been backtested on {timeframe} yet — no trades until it is")
        if not row.approved:
            raise Blocked(f"{symbol} {timeframe} is not approved by its backtest: {row.reason}")
    from app.signals.news_gate import live_block

    s = await get_settings(db)
    news = await live_block(symbol, pre_min=int(getattr(s, "news_pre_min", 30) or 30),
                            cooloff_min=int(getattr(s, "news_cooloff_min", 120) or 120))
    if news is not None:
        raise Blocked(news["reason"])
    try:
        target = await target_pacer.state(db)
    except Exception as exc:  # noqa: BLE001 — an unreadable target never blocks a trade
        logger.debug(f"[room-exec] target state unavailable: {exc}")
        return
    if target.get("entries_paused"):
        raise Blocked(f"weekly target of {target['target_usd']:,.0f} reached — new entries paused until Monday")
    day = target.get("daily") or {}
    if target.get("day_capped"):
        raise Blocked(f"today's portfolio profit {day.get('progress_pct', 0):.1f}% reached the {day.get('max_pct', 0):.0f}% "
                      f"daily cap — no new entries until 00:00 UTC, the day is locked in")
    await _daily_floor(db, s, symbol, timeframe)


async def _daily_floor(db: AsyncSession, s: Any, symbol: str, timeframe: Optional[str]) -> None:
    """Once today's closed room trades win less than the floor (90%), only the
    strongest backtested pairs may open for the rest of the day.

    Stopping outright was tested and did not lift the daily win rate — it only
    froze a bad day where it stood and gave up the wins that followed — so the
    room narrows to its best setups instead. It cannot promise every day stays
    above the floor: one loss on a day with fewer than nine wins breaks it.
    """
    from app.agents import pair_gate, target_pacer

    floor = float(getattr(s, "daily_min_win_rate", 0.9) or 0)
    elite = float(getattr(s, "daily_recovery_min_wr", 0.97) or 0)
    if floor <= 0:
        return
    try:
        accounts = (await mt5_targets(db, s))["targets"]
        day = await target_pacer.room_day_record(db, accounts)
    except Exception as exc:  # noqa: BLE001 — an unreadable record never blocks a trade
        logger.debug(f"[room-exec] daily record unavailable: {exc}")
        return
    if not day["losses"] or (day["win_rate"] or 0) >= floor:
        return
    row = await pair_gate.approval(db, symbol, timeframe) if timeframe else None
    wr = float(row.win_rate) if row is not None and row.win_rate is not None else 0.0
    if elite > 0 and wr >= elite:
        return
    raise Blocked(
        f"today's room win rate is {day['win_rate'] * 100:.0f}% ({day['wins']}W/{day['losses']}L), below the "
        f"{floor * 100:.0f}% daily floor — only pairs whose backtest won ≥{elite * 100:.0f}% trade for the rest "
        f"of the day ({symbol} {timeframe or ''}: {wr * 100:.1f}%)")


def _plan_from(check: Dict[str, Any], *, market_price: float) -> Optional[Dict[str, Any]]:
    """The room profile's ladder for this order, or None for a plain market order.

    A buy limit above the market (or sell limit below it) would fill at once —
    the pullback has already happened — so that goes as a market order at the
    better price, keeping the same stop and targets.
    """
    v = check.get("verdict") or {}
    if v.get("entry_mode") != "limit" or not v.get("limit") or not v.get("stop_loss"):
        return None
    is_long = v.get("action") == "buy"
    limit = float(v["limit"])
    already = market_price > 0 and ((is_long and market_price <= limit) or (not is_long and market_price >= limit))
    from app.agents.oblivion_gate import tf_seconds

    return {
        "entry_mode": "market" if already else "limit",
        "entry": market_price if already else limit,
        "limit": limit, "stop_loss": float(v["stop_loss"]),
        "tp1": v.get("tp1"), "tp2": v.get("tp2"), "tp3": float(v["tp3"]),
        "tp1_close": v.get("tp1_close", 0.5), "tp2_close": v.get("tp2_close", 0.25),
        "trail_atr": v.get("trail_atr"), "trail_from": int(v.get("trail_from") or 1),
        "floor_at_tp1": bool(v.get("floor_at_tp1")),
        "hold_past_tp3": bool(v.get("hold_past_tp3")),
        "timeframe": v.get("timeframe") or check.get("timeframe"),
        "expires_in_s": int(v.get("wait_bars") or 6) * tf_seconds(v.get("timeframe") or check.get("timeframe") or "1h"),
        "read_id": check.get("read_id"),
    }


def oblivion_levels(
    verdict: Dict[str, Any], action: str, price: float,
    stop_loss: float, take_profit: float, level_source: str,
) -> tuple[float, float, str]:
    """OBLIVION's plan is the order's plan; the board may only tighten the stop."""
    is_long = action == "buy"

    def _ahead(level: Optional[float]) -> bool:
        return level is not None and (level > price if is_long else level < price)

    def _behind(level: Optional[float]) -> bool:
        return level is not None and (level < price if is_long else level > price)

    obl_stop = _positive(verdict.get("stop_loss"))
    obl_tp = next((t for t in (_positive(verdict.get(k)) for k in ("tp3", "tp2", "tp1")) if _ahead(t)), None)
    stop, src = stop_loss, level_source
    if _behind(obl_stop):
        stop, src = obl_stop, "OBLIVION plan"
        board_tighter = (
            level_source.startswith("board levels") and _behind(stop_loss)
            and (stop_loss > obl_stop if is_long else stop_loss < obl_stop)
        )
        if board_tighter:
            stop, src = stop_loss, "OBLIVION plan, board-tightened stop"
    target = obl_tp if obl_tp is not None else take_profit
    if obl_tp is None:
        src += "; target from the board (no OBLIVION rung ahead of price)"
    return stop, target, src


async def _check_gates(s: RoomSettings, result: Dict[str, Any], consensus: Dict[str, Any]) -> None:
    if not s.execution_enabled:
        raise Blocked("execution is switched off")

    action = str(result.get("final_action") or "hold").lower()
    if action not in {"buy", "sell"}:
        raise Blocked(f"no tradeable action ({action})")

    agreement = float(consensus.get("agreement") or 0)
    if agreement < s.min_consensus:
        raise Blocked(f"consensus {agreement:.0%} below the {s.min_consensus:.0%} floor")

    confidence = float(result.get("final_confidence") or 0)
    if confidence < s.min_confidence:
        raise Blocked(f"confidence {confidence:.0%} below the {s.min_confidence:.0%} floor")

    # ── Post-mortem 2026-08-28: local fallback at 0.355 was taking BUY while the
    # market analyst correctly called bearish 0.65 into heavy selling. Any AI
    # call that disagrees strongly with a bearish high-conviction analyst read
    # on a metal should be blocked unless sentiment + signal both agree bearish.
    try:
        symbol = str(result.get("symbol") or "").upper()
        is_metal = symbol.replace("/", "").startswith(("XAU", "XAG"))
        if is_metal and action == "buy":
            decisions = result.get("decisions") or []
            analyst = next((d for d in decisions if str(d.get("agent_role","")).lower()=="market_analyst"), None)
            if analyst and str(analyst.get("action","")).lower() in {"bearish", "sell", "short", "down"}:
                try:
                    a_conf = float(analyst.get("confidence") or 0)
                except Exception:
                    a_conf = 0.0
                if a_conf >= 0.55:
                    raise Blocked(
                        f"metal BUY vetoed: market analyst is {analyst.get('action')} ({a_conf:.0%}) into heavy selling — board disagreement, require aligned bearish confirmation"
                    )
    except Blocked:
        raise
    except Exception:
        pass

    if trades_today() >= s.max_trades_per_day:
        raise Blocked(f"daily cap of {s.max_trades_per_day} trades reached")

    # ── Daily loss circuit breaker: if the account already lost > threshold
    # today, stop opening new risk until tomorrow. The 2026-08-28 session lost
    # $50k per trade while continuing to open new longs into the same sell-off.
    # Check is best-effort (requires DB) and never blocks when data unavailable.
    try:
        # This check is intentionally lightweight and is enriched in the caller where DB is available.
        pass
    except Exception:
        pass


def venues_for(symbol: str, s: RoomSettings) -> list:
    """Every venue that can actually trade *symbol*, given what is enabled.

    This used to be an if/elif chain — MT5, else crypto, else sim — so the first
    enabled venue took every trade and the others never fired at all. With MT5
    on, a Bitget account sat idle through every crypto signal the desk published,
    which is the shape of "it posts signals but nothing is executed".

    Venues are chosen by *instrument*, not by priority: gold and the FX crosses
    can only go to the broker, and a perpetual can only go to the exchange. The
    fallback matters too — a crypto pair with the exchange switched off is still
    tradeable on an MT5 broker that lists it, and vice versa.

    The paper account is added to whatever else runs. It is the one venue that
    trades in both modes, so there is always a complete record of the desk's
    calls to check the live ones against.
    """
    from app.services import market_data

    out: list = []
    is_crypto = market_data.classify(market_data.normalize_symbol(symbol)) == market_data.CRYPTO
    has_mt5 = bool(s.allow_mt5 and (s.mt5_account_id or getattr(s, "mt5_demo_account_id", None)))

    if is_crypto:
        # USDT/USDC-quoted (ETHUSDT, BTC/USDT) → exchange format; Bitget lists these.
        # USD-quoted (ETHUSD, BTCUSD) → MT5 CFD format; Bitget does NOT list these.
        sym_norm = (symbol or "").upper().replace("/", "")
        is_usdt_quoted = sym_norm.endswith(("USDT", "USDC"))
        if s.allow_crypto and is_usdt_quoted:
            out.append("crypto")
        if has_mt5:
            out.append("mt5")
    else:
        if has_mt5:
            out.append("mt5")
        elif s.allow_crypto:
            # A broker-only instrument has no business on a crypto exchange;
            # only a symbol the exchange might actually list gets this fallback.
            out.append("crypto")

    if s.allow_sim:
        out.append("sim")
    return out


async def _route(
    db: AsyncSession,
    s: RoomSettings,
    *,
    symbol: str,
    action: str,
    price: float,
    stop_loss: float,
    take_profit: float,
    level_source: str,
    ref: Any = None,
    signal_id: Any = None,
    plan: Optional[Dict[str, Any]] = None,
) -> list:
    """Place one decision on every venue that can take it. Never raises."""
    venues = venues_for(symbol, s)
    orders: list = []

    if "mt5" in venues:
        routing = await mt5_targets(db, s)
        if not routing["targets"]:
            orders.append({
                "venue": "mt5", "role": "—", "status": "skipped",
                "reason": (
                    "no MT5 demo account is configured — a dry run has nowhere "
                    "to trade" if routing["dry_run"] else "no MT5 account is configured"
                ),
            })
        for account in routing["targets"]:
            orders.append(await _place_on(
                db, account, s, symbol=symbol, action=action, price=price,
                stop_loss=stop_loss, take_profit=take_profit,
                level_source=level_source, ref=ref or symbol, plan=plan,
            ))

    if "crypto" in venues:
        orders.extend(await _crypto_orders(db, s, symbol=symbol, signal_id=signal_id))

    if "sim" in venues:
        orders.append(await _sim_fill(
            db, s, signal_id=signal_id, symbol=symbol, action=action,
            price=price, stop_loss=stop_loss, take_profit=take_profit,
        ))

    if not orders:
        orders.append({"venue": "—", "role": "—", "status": "skipped",
                       "reason": "no venue is enabled for this instrument"})
    return orders


def _summarise(orders: list) -> tuple:
    """One status and one sentence for a decision that went to several places."""
    placed = [o for o in orders if o.get("status") == "placed"]
    if placed:
        where = ", ".join(
            f"{o.get('venue')}/{o.get('role', '')}".rstrip("/")
            + (f" #{o['ticket']}" if o.get("ticket") else "")
            for o in placed
        )
        return "placed", f"taken on {where}"
    status = "error" if any(o.get("status") == "error" for o in orders) else "skipped"
    return status, "; ".join(
        f"{o.get('venue', '?')}: {o.get('reason', '?')}" for o in orders
    )


async def _daily_loss_blocked(db: AsyncSession, s: RoomSettings) -> Optional[str]:
    """Has the desk already lost too much today? Returns a block reason or None.

    Post-mortem 2026-08-28: the desk kept opening new XAU longs while the same
    sell-off was bleeding the book — each new loss added to a day that already
    printed -$118k on XAU alone. A daily stop must be absolute, not per-trade.

    Threshold is the tighter of a % of starting equity and a dollar cap. Defaults
    are conservative on large demos where % risk is misleading: $4M @ 3% = $120k
    is a whole quarter's edge, so the dollar cap (default $12k) binds first.
    Tunable via RoomSettings.max_daily_loss_pct / max_daily_loss_usd when those
    columns exist; otherwise defaults apply. Best-effort: any DB error is silence.
    """
    try:
        from datetime import datetime, timedelta
        from sqlalchemy import text as _text
        # Resolve the account the cap is measured against (demo when armed).
        from plugins.MT5TradingPlugin.backend.models import MT5Account
        acct_id = getattr(s, "mt5_demo_account_id", None) or getattr(s, "mt5_account_id", None)
        if not acct_id:
            return None
        acct = await db.get(MT5Account, acct_id)
        if acct is None:
            return None
        equity = float(getattr(acct, "equity", 0) or getattr(acct, "balance", 0) or 0)
        if equity <= 0:
            return None
        pct = float(getattr(s, "max_daily_loss_pct", 0) or 0) or 3.0
        cap_usd = float(getattr(s, "max_daily_loss_usd", 0) or 0)
        # Default dollar caps only on large books; small accounts are governed by %.
        if cap_usd <= 0:
            cap_usd = 12000.0 if equity > 300_000 else equity * (pct / 100.0) * 1.2
        # Tighter for metals-heavy days: XAU can lose $45k in one wick, so cap is lower when the book is metals-concentrated.
        # For now keep one cap; a per-symbol cap can be added later.
        threshold = min(equity * (pct / 100.0), cap_usd)
        # Sum of today's deal PnL for this account (UTC date).
        row = await db.execute(_text(
            "SELECT COALESCE(SUM(profit+swap+commission+fee),0) FROM mt5_deals "
            "WHERE account_id=:aid AND mt5_time::date = (NOW() AT TIME ZONE 'UTC')::date"
        ), {"aid": acct_id})
        pnl_today = float(row.scalar() or 0)
        # pnl_today is net (wins-losses). Only block when deep negative.
        if pnl_today < -abs(threshold):
            return f"daily loss limit hit: {pnl_today:+.0f} vs {threshold:.0f} limit ({pct:.1f}% / ${cap_usd:.0f}) — no new risk until tomorrow"
    except Exception as exc:  # noqa: BLE001 — never block on a failed check
        from loguru import logger as _lg
        _lg.debug(f"[execution] daily loss check skipped: {exc}")
    return None


async def execute_decision(
    db: AsyncSession, result: Dict[str, Any], consensus: Dict[str, Any],
) -> Dict[str, Any]:
    """Run the gauntlet and place the trade. Never raises — reports instead."""
    symbol = result.get("symbol") or ""
    action = str(result.get("final_action") or "hold").lower()
    ctx = {"session_id": result.get("session_id"), "read_id": result.get("oblivion_read_id")}

    try:
        s = await get_settings(db)
        await _check_gates(s, result, consensus)
        if reason := await _daily_loss_blocked(db, s):
            raise Blocked(reason)
        check = await oblivion_recheck(
            s, symbol=symbol, action=action, timeframe=result.get("timeframe"),
            session_id=result.get("session_id"),
        )
        ctx["read_id"] = check.get("read_id") or ctx["read_id"]
        await _room_entry_gates(db, symbol, check.get("timeframe"))
    except Blocked as exc:
        return await _report(symbol, action, "skipped", str(exc), **ctx)
    except Exception as exc:  # noqa: BLE001
        return await _report(symbol, action, "error", f"gate check failed: {exc}", **ctx)

    try:
        price = float(result.get("price") or 0)
        if price <= 0:
            from app.services import market_data

            quote = await market_data.get_quote(symbol, db=db)
            price = float(getattr(quote, "price", 0) or 0)
        if price <= 0:
            raise Blocked("no live price to size against")

        stop_loss, take_profit, level_source = await _levels_from(
            result.get("decisions", []), price, action, symbol=symbol,
            oblivion=result.get("oblivion"),
        )
        plan = _plan_from(check, market_price=price)
        if plan:
            # The tuned profile's own plan: a pullback limit, stop and TP3.
            price, stop_loss, take_profit = plan["entry"], plan["stop_loss"], plan["tp3"]
            level_source = f"OBLIVION {plan['timeframe']} profile — {plan['entry_mode']} entry"
        else:
            stop_loss, take_profit, level_source = oblivion_levels(
                check["verdict"], action, price, stop_loss, take_profit, level_source,
            )
        orders = await _route(
            db, s, symbol=symbol, action=action, price=price, stop_loss=stop_loss,
            take_profit=take_profit, level_source=level_source,
            ref=(result.get("signal") or {}).get("id") or symbol,
            signal_id=(result.get("signal") or {}).get("id"), plan=plan,
        )
    except Blocked as exc:
        return await _report(symbol, action, "skipped", str(exc), **ctx)
    except Exception as exc:  # noqa: BLE001 - a broker error must not kill the session
        logger.warning(f"[room-exec] {symbol} failed: {exc}")
        return await _report(symbol, action, "error", str(exc), **ctx)

    status, reason = _summarise(orders)
    if status == "placed":
        _note_order(symbol)
    primary = next((o for o in orders if o.get("status") == "placed"), orders[0])
    return await _report(symbol, action, status, reason, {**primary, "orders": orders}, **ctx)


# ── Which accounts the room may touch ───────────────────────────────────────


async def mt5_targets(db: AsyncSession, s: RoomSettings) -> Dict[str, Any]:
    """The MT5 accounts this room may trade and manage right now.

    ``dry_run`` used to mean "prepare the order and send nothing". It now means
    something more useful and, in practice, safer: **the demo account trades for
    real and the live account is not touched at all.** A dry run that sends
    nothing anywhere proves the plumbing works and nothing else — no fills, no
    slippage, no management, nothing to watch. Running the same decisions on a
    demo account gives a real record to judge the desk by, at no risk.

    With the dry run off, both accounts take the trade *simultaneously*, so the
    demo stays a live mirror of what the real account is doing and remains the
    thing you watch rather than a record that stops the day you go live.

    Returns ``{"demo", "live", "targets", "note"}``. ``targets`` is what callers
    act on, and in a dry run it can never contain the live account — that is the
    single guarantee this function exists to make.
    """
    from plugins.MT5TradingPlugin.backend.models import MT5Account

    demo_id = getattr(s, "mt5_demo_account_id", None)
    live_id = getattr(s, "mt5_account_id", None)

    demo = await db.get(MT5Account, demo_id) if demo_id else None
    live = await db.get(MT5Account, live_id) if live_id else None
    # One account configured in both slots is one account, not two orders.
    if demo is not None and live is not None and demo.id == live.id:
        live = None

    # Every other connected account: demo accounts always trade, live accounts
    # only when their own switch is on (room_trading_accounts).
    extra_demos: list = []
    extra_lives: list = []
    toggles: Dict[int, bool] = {}
    try:
        from app.services.crypto_accounts import live_enabled_ids

        toggles = await live_enabled_ids(db, "mt5")
        for acct in (await db.execute(select(MT5Account))).scalars().all():
            if acct.id in {demo_id, live_id}:
                continue
            if is_demo_account(acct):
                extra_demos.append(acct)
            elif toggles.get(acct.id):
                extra_lives.append(acct)
    except Exception as exc:  # noqa: BLE001 — the configured slots still route
        logger.debug(f"[room-exec] account list unavailable: {exc}")
    # The configured live slot follows its own switch; an install that has no
    # switch row for it yet keeps trading it as before.
    if live is not None and toggles.get(live.id, True) is False:
        live = None

    demos = [a for a in (demo,) if a is not None] + extra_demos
    lives = [a for a in (live,) if a is not None] + extra_lives
    dry = bool(s.dry_run)
    if dry:
        targets = list(demos)
        note = "dry run — demo accounts only, the live accounts are not touched"
    else:
        targets = demos + lives
        note = (
            f"armed — {len(demos)} demo and {len(lives)} live account(s) take the trade together"
            if demos and lives
            else f"armed — live account(s) only ({len(lives)}; no demo account is configured)"
            if lives else "armed — demo account(s) only (no live account is switched on)"
        )
    return {
        "demo": demos[0] if demos else None, "live": lives[0] if lives else None,
        "demos": demos, "lives": lives, "targets": targets, "note": note, "dry_run": dry,
    }


def is_demo_account(account: Any) -> bool:
    kind = getattr(account, "account_type", None)
    return str(getattr(kind, "value", kind) or "").lower() == "demo"


# ── Venues ──────────────────────────────────────────────────────────────────


async def _prepare_account(db: AsyncSession, account: Any, s: RoomSettings, symbol: str = "") -> None:
    """Make sure the broker's own numbers are fresh enough to size against.

    A never-synced or stale account still holds its default zero equity, which
    reads downstream as "unfunded" and blocks every order — the usual shape of
    "demo trades but live does nothing" right after a live account is first
    selected.

    Post-mortem 2026-08-28 adds two guards kept here because this is the single
    choke-point every MT5 order passes through: a dollar-risk cap for large demo
    balances (4M @ 1% = 40k per trade is how the $50k losses were sized) and a
    correlated-metal exposure check (XAU+XAG counted as one bucket).
    """
    from plugins.MT5TradingPlugin.backend.models import MT5AccountStatus

    last_sync = getattr(account, "last_sync_at", None)
    if (
        account.status != MT5AccountStatus.ACTIVE
        or last_sync is None
        or datetime.utcnow() - last_sync > timedelta(minutes=10)
    ):
        from plugins.MT5TradingPlugin.backend.services.sync_service import MT5SyncService

        try:
            synced = await MT5SyncService.sync_account(db, account)
        except Exception as exc:  # noqa: BLE001 - surfaced as a readable block
            raise Blocked(
                f"account sync failed ({exc}) — check login/server/password for "
                f"{account.login}@{account.server}"
            ) from exc
        if not synced:
            raise Blocked(
                f"could not reach {account.login}@{account.server} — check the "
                "login, server name and password for this account"
            )

    from app.agents import trade_manager

    if blocked := trade_manager.entries_blocked("mt5", account.id):
        raise Blocked(f"margin protection: {blocked}")

    open_count = getattr(account, "position_count", 0) or 0
    if open_count >= s.max_open_positions:
        raise Blocked(f"{open_count} positions already open (cap {s.max_open_positions})")

    # ── Margin level guard ─────────────────────────────────────────────────
    # margin_level = (equity / margin_used) × 100.  At 100% the account has
    # no buffer; below ~150% most brokers are near stop-out.  We block new
    # orders before the account reaches that danger zone.
    try:
        _margin_used = float(getattr(account, "margin", 0) or 0)
        _ml = float(getattr(account, "margin_level", 0) or 0)
        if _ml <= 0 and _margin_used > 0:
            # Derive from components when the broker field is absent/null.
            _eq = float(getattr(account, "equity", 0) or 0)
            _ml = (_eq / _margin_used) * 100.0
        # Configurable floor; default 500 % — orders only when the account has
        # ample headroom. Set min_margin_level_pct in room settings to override.
        _min_ml = float(getattr(s, "min_margin_level_pct", 0) or 0) or 500.0
        if 0 < _ml < _min_ml:
            raise Blocked(
                f"margin level {_ml:.0f}% is below the {_min_ml:.0f}% floor — "
                "close positions or add funds before opening new trades"
            )
    except Blocked:
        raise
    except Exception as exc:  # noqa: BLE001 — never block on a failed check
        logger.debug(f"[execution] margin level check skipped for {getattr(account, 'login', '?')}: {exc}")
    # ── Post-mortem 2026-08-28: XAU had 4-6 concurrent longs (15+ lots each) while
    # max_open_positions=3 was per-account total. Gold and silver are 0.85 correlated —
    # treat XAU+XAG longs as one bucket: if the requested symbol is a metal and the
    # account already holds metal exposure, require stricter capacity.
    if symbol:
        try:
            norm = symbol.upper().replace("/", "")
            is_metal = norm.startswith(("XAU", "XAG"))
            if is_metal:
                from plugins.MT5TradingPlugin.backend.models import MT5Position
                from sqlalchemy import select as _sel
                rows = (await db.execute(_sel(MT5Position).where(MT5Position.account_id == account.id))).scalars().all()
                metal_longs = sum(1 for p in rows if str(getattr(p, "symbol", "") or "").upper().replace("/", "").startswith(("XAU", "XAG")) and str(getattr(getattr(p, "side", ""), "value", getattr(p, "side", ""))).lower() == "buy")
                # Allow at most 2 concurrent metal longs regardless of total cap; the 3rd metal long is highly correlated.
                if metal_longs >= 2:
                    raise Blocked(f"metal exposure cap: {metal_longs} XAU/XAG longs already open (limit 2) — metals are correlated, cannot add {symbol}")
                # Also block if total correlated longs would exceed the global cap when accounting for metals overlap
                if metal_longs >= 1 and open_count >= max(2, s.max_open_positions - 1):
                    raise Blocked(f"correlated cap: {open_count} positions with {metal_longs} metal longs — adding another metal would breach concentration")
        except Blocked:
            raise
        except Exception as exc:  # noqa: BLE001 — DB failure should not block, but log
            from loguru import logger as _lg
            _lg.debug(f"[execution] metal exposure check skipped for {symbol}: {exc}")


async def _place_on(
    db: AsyncSession,
    account: Any,
    s: RoomSettings,
    *,
    symbol: str,
    action: str,
    price: float,
    stop_loss: float,
    take_profit: float,
    level_source: str,
    ref: Any,
    plan: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Size and send one order to one account. Never raises — reports instead.

    Each account is sized on its *own* equity, which is the whole point of
    mirroring rather than copying: a 4.7M demo and a small live account take the
    same trade at the same levels, each risking its own configured percentage.
    """
    from plugins.MT5TradingPlugin.backend.services.mt5_client import mt5_client
    from plugins.TelegramSignalNewsPlugin.backend.services.sniper_service import (
        affordable_mt5_lot,
    )

    # The cycle's auto risk reduction applies per symbol before any sizing.

    # Verify the symbol is listed on this specific broker/server before sizing.
    # MetaQuotes and other forex-only demos don't carry crypto CFDs (ETHUSD, BTCUSD).
    mt5_sym = _mt5_symbol(symbol)
    try:
        _sym_info = await mt5_client.get_symbol_info(
            account.login, account.server, account.password_encrypted, mt5_sym
        )
        _msg = str((_sym_info or {}).get("message", "")).lower()
        if "not found" in _msg or "symbol" in _msg and "not" in _msg:
            return {
                "venue": "mt5", "account_id": account.id,
                "account": f"{account.login}@{account.server}",
                "role": "demo" if (account.id == getattr(s, "mt5_demo_account_id", None) or is_demo_account(account)) else "live",
                "symbol": symbol, "status": "skipped",
                "reason": f"{mt5_sym} is not available on {account.server} — skipping this account",
            }
    except Exception:  # noqa: BLE001 — symbol check is best-effort; proceed if API unreachable
        pass
    risk_pct = await effective_risk_pct(s, symbol)
    # OBLIVION profile trades are sized inside the room's risk band (1–5% of
    # equity by default): each pair gets the risk its backtested weekly R needs
    # to reach its weekly minimum, sized up when this week is behind.
    banded = bool(plan)
    if banded:
        from app.agents import target_pacer

        try:
            eq_for_band = float(getattr(account, "equity", 0) or getattr(account, "balance", 0) or 0)
            risk_pct = await target_pacer.banded_risk_pct(db, s, symbol, eq_for_band)
        except Exception as exc:  # noqa: BLE001 — the band floor if pacing is unreadable
            logger.debug(f"[room-exec] risk band fallback for {symbol}: {exc}")
            risk_pct = float(getattr(s, "min_risk_pct", 1.0) or 1.0)

    order: Dict[str, Any] = {
        "venue": "mt5",
        "account_id": account.id,
        "account": f"{account.login}@{account.server}",
        "role": "demo" if (account.id == getattr(s, "mt5_demo_account_id", None) or is_demo_account(account)) else "live",
        "symbol": symbol,
        "levels": level_source,
        "side": action,
        "entry": round(price, 5),
        "stop_loss": round(stop_loss, 5),
        "take_profit": round(take_profit, 5),
        "risk_pct": risk_pct,
        "leverage": int(getattr(account, "leverage", 0) or 0) or None,
    }

    try:
        await _prepare_account(db, account, s, symbol=symbol)
    except Blocked as exc:
        return {**order, "status": "skipped", "reason": str(exc)}

    equity = float(getattr(account, "equity", 0) or getattr(account, "balance", 0) or 0)
    if equity <= 0:
        return {**order, "status": "skipped",
                "reason": "account equity unavailable — refusing to size blind"}

    # Annotate the order with the live margin level so the response and logs show it.
    _ml_live = float(getattr(account, "margin_level", 0) or 0)
    if _ml_live > 0:
        order["margin_level_pct"] = round(_ml_live, 1)

    # ── Dollar-risk cap (post-mortem 2026-08-28): a 4M demo @ 1% risks $40k per trade,
    # which is how the $50k losses were sized. Even with the 0.45× metals dampener
    # above, 0.45% of 4M is still $18k. Cap absolute risk so a single idea cannot
    # lose > $X regardless of demo equity. Tunable via RoomSettings.max_usd_risk_per_trade
    # when that column exists; otherwise use a sensible default that scales with account size.
    orig_equity = equity
    try:
        cap = float(getattr(s, "max_usd_risk_per_trade", 0) or 0)
        if banded and cap <= 0:
            raise StopIteration  # the risk band is the user's explicit sizing — no implicit caps
        sym_norm = (symbol or "").upper().replace("/", "")
        is_metal = sym_norm.startswith(("XAU", "XAG"))
        if cap <= 0 and equity > 300_000:
            # Default caps only engage on large balances where % risk becomes dangerous in absolute terms.
            cap = 6000.0 if is_metal else 9000.0
            # On very large demos (>1M) tighten further: the notional is already huge.
            if equity > 1_500_000 and is_metal:
                cap = 4500.0
        if cap > 0 and risk_pct > 0:
            implied_equity_at_cap = cap / (risk_pct / 100.0)
            if equity > implied_equity_at_cap:
                equity = implied_equity_at_cap
                order["equity_capped"] = True
                order["cap_usd"] = cap
    except StopIteration:
        pass
    except Exception:
        equity = orig_equity

    # Weekly target pacing: when the week is behind target, size up — never
    # past the multiplier the user allowed, and never past the free margin below.
    try:
        from app.agents import target_pacer

        pace = await target_pacer.state(db)
        mult = 1.0 if banded else float(pace.get("multiplier") or 1.0)
        if mult > 1.0:
            equity *= mult
            order["target_multiplier"] = mult
    except Exception as exc:  # noqa: BLE001 — pacing is never a reason to miss the trade
        logger.debug(f"[room-exec] target pacing skipped: {exc}")

    # The risk budget AND the free margin both bound the lot, rounding floors so
    # it can never exceed the budget, and an account too small for one broker
    # minimum lot either trades at the floor (small-account mode) or not at all.
    q2usd = await quote_to_usd(symbol, price)
    order["quote_to_usd"] = round(q2usd, 8)
    volume, size_note = affordable_mt5_lot(
        quote_to_usd=q2usd,
        equity=equity,
        free_margin=_margin_headroom(account, s),
        leverage=getattr(account, "leverage", None),
        risk_pct=risk_pct,
        entry=price,
        stop_loss=stop_loss,
        symbol=symbol,
        floor_lot=float(getattr(s, "mt5_lot_size", 0.01) or 0.01),
        max_risk_pct=max(float(getattr(s, "mt5_max_risk_pct", 5.0) or 5.0),
                         float(getattr(s, "max_risk_pct", 5.0) or 5.0) if banded else 0.0),
        small_account_mode=bool(getattr(s, "mt5_small_account_mode", True)),
    )
    if order.get("equity_capped"):
        size_note = f"${order['cap_usd']:.0f} cap (equity {orig_equity:,.0f}→{equity:,.0f}) · " + size_note
    if volume is not None:
        try:
            params = await mt5_client.get_symbol_params(account.login, account.server,
                                                        account.password_encrypted, _mt5_symbol(symbol))
            max_lots = float(((params or {}).get("symbolGroup") or {}).get("maxLots") or 0)
            if max_lots > 0 and volume > max_lots:
                size_note += f" · clamped to the broker's {max_lots:g}-lot maximum"
                volume = max_lots
        except Exception as exc:  # noqa: BLE001 — the broker rejects an oversize order anyway
            logger.debug(f"[room-exec] max-lot lookup failed for {symbol}: {exc}")
    order.update(equity=equity, volume=volume, sizing=size_note)
    if volume is None:
        return {**order, "status": "skipped",
                "reason": f"cannot size within risk limits — {size_note}"}

    limit_order = bool(plan and plan.get("entry_mode") == "limit")
    order["order_type"] = f"{action}_limit" if limit_order else action
    try:
        placed = await mt5_client.place_order(
            login=account.login, server=account.server,
            password=account.password_encrypted,
            symbol=_mt5_symbol(symbol), order_type=order["order_type"], volume=volume,
            price=round(price, 5) if limit_order else 0,
            # Held past TP3 on a trailing stop: the broker gets no take-profit.
            sl=stop_loss, tp=None if (plan and plan.get("hold_past_tp3")) else take_profit,
            # Tagged so the position is identifiable in MT5, separable from
            # orders placed by hand, and manageable by the room's own guard.
            comment=build_comment(SOURCE_ROOM, ref or symbol),
        )
    except Exception as exc:  # noqa: BLE001 — one account failing is not both
        logger.warning(f"[room-exec] {symbol} on {order['account']} failed: {exc}")
        return {**order, "status": "error", "reason": str(exc)[:200]}

    ticket = _mt5_ticket_from(placed)
    if ticket is None:
        # No ticket means the broker did not open a position; reporting it as
        # placed would leave the room tracking an order that does not exist.
        return {**order, "status": "error",
                "reason": f"broker returned no ticket: {str(placed)[:160]}"}
    _bump_daily_counter()
    if plan:
        await _save_plan(db, plan, account_id=account.id, ticket=ticket, symbol=symbol, side=action)
    return {**order, "status": "placed", "ticket": ticket}


async def _save_plan(db: AsyncSession, plan: Dict[str, Any], *, account_id: int, ticket: str,
                     symbol: str, side: str) -> None:
    """Remember the ladder so the trade manager can bank TP1/TP2 and expire the limit."""
    from app.core.timezone import now_sast
    from app.models.database import RoomTradePlan

    try:
        db.add(RoomTradePlan(
            venue="mt5", account_id=account_id, ticket=str(ticket), symbol=symbol, side=side,
            timeframe=plan.get("timeframe"), entry_mode=plan["entry_mode"], entry=plan["entry"],
            stop_loss=plan["stop_loss"], tp1=plan.get("tp1"), tp2=plan.get("tp2"), tp3=plan.get("tp3"),
            tp1_close=plan.get("tp1_close", 0.5), tp2_close=plan.get("tp2_close", 0.25),
            trail_atr=plan.get("trail_atr"), trail_from=int(plan.get("trail_from") or 1),
            floor_at_tp1=bool(plan.get("floor_at_tp1")),
            status="pending" if plan["entry_mode"] == "limit" else "open",
            expires_at=now_sast() + timedelta(seconds=int(plan.get("expires_in_s") or 0))
            if plan["entry_mode"] == "limit" else None,
            oblivion_read_id=plan.get("read_id"),
        ))
        await db.commit()
    except Exception as exc:  # noqa: BLE001 — the order is live; losing the ladder is logged loudly
        logger.warning(f"[room-exec] could not save the trade plan for #{ticket}: {exc}")


async def _crypto_orders(
    db: AsyncSession, s: RoomSettings, *, symbol: str, signal_id: Any,
) -> list:
    """One order per exchange account: demo accounts always, enabled live ones when armed."""
    from app.services.crypto_accounts import crypto_targets

    routing = await crypto_targets(db, s)
    if not routing["has_accounts"]:
        # No accounts connected in the app yet — the .env account, as before.
        return [await _crypto_fill(db, s, symbol=symbol, signal_id=signal_id)]
    if not routing["targets"]:
        return [{
            "venue": "crypto", "role": "—", "symbol": symbol, "status": "skipped",
            "reason": (
                "dry run — no demo exchange account is connected; the paper account is the demo mirror"
                if routing["dry_run"] else "no exchange account is switched on for live trading"
            ),
        }]
    out = []
    for acct in routing["targets"]:
        out.append(await _crypto_fill(db, s, symbol=symbol, signal_id=signal_id, account=acct))
    return out


async def _crypto_fill(
    db: AsyncSession, s: RoomSettings, *, symbol: str, signal_id: Any, account: Any = None,
) -> Dict[str, Any]:
    """Take the trade on one exchange account. Never raises.

    The live engine owns sizing, leverage, margin mode and the exchange's own
    symbol spelling, so the room hands it the decision rather than reimplementing
    any of that. A demo account (exchange demo trading) always trades; a live
    account only when live trading is on. ``account=None`` is the .env account,
    which has no demo — in a dry run it is left alone and the paper account is
    the mirror.
    """
    from app.trading.live import LiveTradeEngine

    demo = False
    kwargs: Dict[str, Any] = {}
    if account is not None:
        from app.services.crypto_accounts import connector_for, is_demo

        demo = is_demo(account)
        base = {"venue": "crypto", "role": "demo" if demo else "live",
                "account_id": account.id, "account": f"{account.name} ({account.exchange})",
                "symbol": symbol}
        try:
            kwargs = {"connector": connector_for(account), "crypto_account_id": account.id, "demo": demo}
        except Exception as exc:  # noqa: BLE001
            return {**base, "status": "error", "reason": f"cannot open this account: {exc}"[:200]}
    else:
        base = {"venue": "crypto", "role": "exchange", "symbol": symbol}
    if s.dry_run and not demo:
        return {**base, "status": "skipped",
                "reason": "dry run — no demo exchange account is connected; the paper account is the demo mirror"}
    if account is not None:
        from app.agents import trade_manager

        if blocked := trade_manager.entries_blocked("crypto", account.id):
            return {**base, "status": "skipped", "reason": f"margin protection: {blocked}"}
    if not signal_id:
        return {**base, "status": "skipped", "reason": "no signal was saved for this decision"}

    try:
        outcome = await LiveTradeEngine.execute_signal(db, signal_id, **kwargs)
    except Exception as exc:  # noqa: BLE001
        return {**base, "status": "error", "reason": str(exc)[:200]}
    if outcome.get("error"):
        return {**base, "status": "skipped", "reason": str(outcome["error"])[:200]}
    if outcome.get("dry_run"):
        return {**base, "status": "skipped", "reason": "live-trade engine is in dry run (Live Trade settings)"}
    _bump_daily_counter()
    return {**base, "status": "placed", "reason": f"sent to {base.get('account', 'Bitget')}", **outcome}


async def _sim_fill(
    db: AsyncSession,
    s: RoomSettings,
    *,
    signal_id: Any = None,
    symbol: str = "",
    action: str = "",
    price: float = 0.0,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
) -> Dict[str, Any]:
    """Fill the decision on the paper account. Never raises.

    This venue had never once worked: it called ``SimulationEngine.execute_signal``,
    a method that does not exist, so every paper order since the room was built
    raised ``AttributeError``, was swallowed by the venue's own error handling,
    and reported as a failed execution. The engine's actual entry point is
    ``place_order``, which needs a size — derived here from the paper balance and
    the stop distance, the same way every other venue sizes.

    Withholding this during a dry run would be the other half of the problem: the
    safest venue in the app producing no record at all during exactly the period
    the desk is meant to be proving itself.
    """
    from app.trading.simulation import SimulationEngine

    base = {"venue": "sim", "role": "paper", "symbol": symbol}
    side = str(action or "").lower()
    if side not in {"buy", "sell"} or not price or price <= 0:
        return {**base, "status": "skipped", "reason": "no side or price to fill at"}

    try:
        account = await SimulationEngine.get_or_create_account(db)
        balance = float(getattr(account, "balance", 0) or 0)
        if balance <= 0:
            return {**base, "status": "skipped", "reason": "paper account has no balance"}

        # Risk the configured percentage against the stop, exactly as the broker
        # venue does — a wider stop buys a smaller position, not a bigger loss.
        # The cycle's auto reduction applies here too: same symbol, same rule.
        sim_risk_pct = await effective_risk_pct(s, symbol)
        risk_amount = balance * (float(sim_risk_pct or 1.0) / 100.0)
        distance = abs(price - stop_loss) if stop_loss else 0.0
        amount = (risk_amount / distance) if distance > 0 else (risk_amount / price)
        # Cap the *margin* a single idea can tie up, not the notional: on a
        # levered account those are different numbers, and capping notional
        # silently shrinks a correctly-risked position to a fraction of itself.
        leverage = max(1, min(int(s.max_leverage or 5), 10))
        margin_cap = balance * 0.25
        amount = min(amount, margin_cap * leverage / price)
        if amount <= 0:
            return {**base, "status": "skipped", "reason": "position sized to zero"}

        outcome = await SimulationEngine.place_order(
            db=db, symbol=symbol, side=side, amount=amount, price=price,
            order_type="market", signal_id=signal_id,
            stop_loss=stop_loss, take_profit=take_profit, sl_type="signal",
            trade_type="futures", leverage=leverage,
        )
    except Exception as exc:  # noqa: BLE001
        return {**base, "status": "error", "reason": str(exc)[:200]}

    if not (outcome or {}).get("success", True):
        return {**base, "status": "skipped", "reason": str(outcome.get("error"))[:200]}
    return {**base, "status": "placed",
            "reason": f"paper fill {amount:.6g} @ {price:.6g}", **(outcome or {})}


# ── Published signals ───────────────────────────────────────────────────────


async def _signal_row_for_card(
    db: AsyncSession, *, symbol: str, action: str, entry: float,
    stop_loss: float, take_profit: float,
) -> Optional[int]:
    """A Signal row for a published card, so the venue engines can execute it.

    The crypto and paper engines both work from a saved signal — that is where
    they read the levels, and where the resulting trade is linked back to. A
    card published without one therefore had no way to reach either venue, which
    is half of "it posts signals but does not execute them".
    """
    from app.models.database import SignalAction, SignalSource, SignalStatus

    try:
        risk = abs(entry - stop_loss)
        reward = abs(take_profit - entry)
        sig = Signal(
            source=SignalSource.SYSTEM,
            symbol=symbol,
            action=SignalAction.BUY if action == "buy" else SignalAction.SELL,
            price=entry,
            timeframe="1h",
            strength=0.7,
            confidence=0.7,
            status=SignalStatus.PENDING,
            raw_data=json.dumps({"origin": "published_signal_card"}),
            indicators=json.dumps({
                "sl_pct": round(risk / entry * 100, 4) if entry else 2.0,
                "tp_pct": round(reward / entry * 100, 4) if entry else 4.0,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
            }),
        )
        db.add(sig)
        await db.flush()
        await db.commit()
        return sig.id
    except Exception as exc:  # noqa: BLE001 — MT5 can still take the trade
        logger.warning(f"[room-exec] could not record a signal for {symbol}: {exc}")
        return None


async def mirror_published_card(
    db: AsyncSession,
    *,
    symbol: str,
    side: str,
    entry: float,
    stop_loss: float,
    take_profits: list,
    ref: Any = None,
) -> Dict[str, Any]:
    """Put a signal the agents just published onto every account that can take it.

    A card on the channel and no order anywhere is the worst of both: the desk
    is judged on trades it never took, and there is no record to check the calls
    against. This closes that gap — whatever goes out as a signal is taken on
    the broker *and* the exchange, each according to what the instrument is.

    The board's own decision path (:func:`execute_decision`) already places most
    of these, so this is guarded against re-ordering the same pair: the card that
    follows a decision finds the order already there and does nothing. What it
    catches is the case the decision path never covered — a seat publishing a
    directional plan under a HOLD verdict, which is a signal on the channel that
    the room had no order for at all.

    The consensus and confidence gates are deliberately not re-applied. Whether
    to publish is exactly that judgement; having made it, refusing to trade the
    published plan would put the desk's own signal beyond its own reach. The
    protective gates — the master switch, the daily cap and the position cap —
    all still apply.
    """
    action = str(side or "").lower()
    if action not in {"buy", "sell"}:
        return {"status": "skipped", "reason": f"not a tradeable side ({side})"}

    try:
        s = await get_settings(db)
        if not s.execution_enabled:
            raise Blocked("execution is switched off")
        if trades_today() >= s.max_trades_per_day:
            raise Blocked(f"daily cap of {s.max_trades_per_day} trades reached")
        if ordered_recently(symbol):
            raise Blocked("the room already placed this pair — not doubling up")
        if reason := await _daily_loss_blocked(db, s):
            raise Blocked(reason)
        check = await oblivion_recheck(s, symbol=symbol, action=action)
        await _room_entry_gates(db, symbol, check.get("timeframe"))
    except Blocked as exc:
        return await _report(symbol, action, "skipped", f"published signal → {exc}")

    is_long = action == "buy"
    ahead = [t for t in take_profits or [] if t and (t > entry if is_long else t < entry)]
    take_profit = (max(ahead) if is_long else min(ahead)) if ahead else None
    if not stop_loss or not take_profit or not entry:
        return await _report(symbol, action, "skipped",
                             "the published card had no usable entry, stop or target")
    # Price has already left the plan — filling here would open a trade that is
    # past its own stop before it starts.
    if (is_long and entry <= stop_loss) or (not is_long and entry >= stop_loss):
        return await _report(symbol, action, "skipped",
                             "price is already through the published stop")

    venues = venues_for(symbol, s)
    signal_id = None
    if {"crypto", "sim"} & set(venues):
        signal_id = await _signal_row_for_card(
            db, symbol=symbol, action=action, entry=entry,
            stop_loss=stop_loss, take_profit=take_profit,
        )

    stop_loss, take_profit, level_source = oblivion_levels(
        check["verdict"], action, entry, stop_loss, take_profit, "board levels (published signal card)",
    )
    orders = await _route(
        db, s, symbol=symbol, action=action, price=entry, stop_loss=stop_loss,
        take_profit=take_profit, level_source=level_source,
        ref=ref or symbol, signal_id=signal_id,
    )
    status, reason = _summarise(orders)
    if status == "placed":
        _note_order(symbol)
        # Close the row out. It exists so the venue engines could read the
        # levels; left PENDING it is also an open invitation to any auto-trade
        # loop that later gets switched on to place the very same trade again.
        await _mark_signal_executed(db, signal_id)
    return await _report(
        symbol, action, status, f"published signal → {reason}",
        {"orders": orders, "source": "signal_card"}, read_id=check.get("read_id"),
    )


async def _mark_signal_executed(db: AsyncSession, signal_id: Any) -> None:
    from app.models.database import SignalStatus

    if not signal_id:
        return
    try:
        row = await db.get(Signal, signal_id)
        if row is not None:
            row.status = SignalStatus.EXECUTED
            await db.commit()
    except Exception as exc:  # noqa: BLE001 — the trade is already placed
        logger.debug(f"[room-exec] could not close out signal {signal_id}: {exc}")


# ── Reporting ───────────────────────────────────────────────────────────────


async def _report(
    symbol: str, action: str, status: str, reason: str, order: Optional[Dict[str, Any]] = None,
    *, session_id: Optional[str] = None, read_id: Optional[int] = None,
) -> Dict[str, Any]:
    """One shape for every outcome, broadcast so the room can show it — and
    recorded in the AI brain, placed or blocked, every time."""
    payload = {
        "symbol": symbol,
        "action": action,
        "status": status,     # placed | dry_run | skipped | error
        "reason": reason,
        "order": order,
        "at": time.time(),
    }
    # "skipped" is logged too: every blocked gate reports as skipped, and a
    # silent skip is indistinguishable from the room never deciding at all.
    if status in {"skipped", "error"}:
        logger.warning(f"[room-exec] {symbol} {action} → {status}: {reason}")
    elif status in {"placed", "dry_run"}:
        logger.info(f"[room-exec] {symbol} {action} → {status}: {reason}")
    try:
        orders = (order or {}).get("orders") or []
        if orders:
            for o in orders:
                await brain_log.log_order(o, symbol=symbol, action=action,
                                          session_id=session_id, oblivion_read_id=read_id)
        else:
            await brain_log.record_decision(
                agent_name="Execution", agent_role="execution", symbol=symbol,
                action=f"{action}:{status}", confidence=0.0, reasoning=reason,
                market_data=payload, session_id=session_id, ai_called=False,
                origin="execution_blocked" if status != "placed" else "execution",
                oblivion_read_id=read_id,
            )
    except Exception as exc:  # noqa: BLE001 — recording must never block the report
        logger.warning(f"[room-exec] brain record failed for {symbol}: {exc}")
    try:
        await event_bus.publish(Topics.ROOM_EXECUTION, payload)
    except Exception:  # noqa: BLE001
        pass
    return payload
