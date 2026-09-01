"""
Trading-room execution — turns an agreed board decision into a real order.

Every order passes the same gauntlet, in order, and any failure stops it dead:

  1. execution_enabled          — the master switch on the settings page
  2. venue allowed             — sim / crypto / MT5 each opt in separately
  3. consensus + confidence    — the board must actually agree
  4. daily trade cap           — a runaway loop cannot drain the account
  5. open-position cap         — bounded concurrent exposure
  6. risk sizing               — size derived from equity x risk%, never guessed
  7. dry_run                   — routes to the demo/paper account only

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
        if s.allow_crypto:
            out.append("crypto")
        elif has_mt5:
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
                level_source=level_source, ref=ref or symbol,
            ))

    if "crypto" in venues:
        orders.append(await _crypto_fill(db, s, symbol=symbol, signal_id=signal_id))

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

    try:
        s = await get_settings(db)
        await _check_gates(s, result, consensus)
        if reason := await _daily_loss_blocked(db, s):
            raise Blocked(reason)
    except Blocked as exc:
        return await _report(symbol, action, "skipped", str(exc))
    except Exception as exc:  # noqa: BLE001
        return await _report(symbol, action, "error", f"gate check failed: {exc}")

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
        )
        orders = await _route(
            db, s, symbol=symbol, action=action, price=price, stop_loss=stop_loss,
            take_profit=take_profit, level_source=level_source,
            ref=(result.get("signal") or {}).get("id") or symbol,
            signal_id=(result.get("signal") or {}).get("id"),
        )
    except Blocked as exc:
        return await _report(symbol, action, "skipped", str(exc))
    except Exception as exc:  # noqa: BLE001 - a broker error must not kill the session
        logger.warning(f"[room-exec] {symbol} failed: {exc}")
        return await _report(symbol, action, "error", str(exc))

    status, reason = _summarise(orders)
    if status == "placed":
        _note_order(symbol)
    primary = next((o for o in orders if o.get("status") == "placed"), orders[0])
    return await _report(symbol, action, status, reason, {**primary, "orders": orders})


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

    dry = bool(s.dry_run)
    if dry:
        targets = [a for a in (demo,) if a is not None]
        note = "dry run — demo account only, the live account is not touched"
    else:
        targets = [a for a in (demo, live) if a is not None]
        note = (
            "armed — demo and live take the trade together"
            if demo is not None and live is not None
            else "armed — live account only (no demo account is configured)"
            if live is not None else "armed — demo account only"
        )
    return {"demo": demo, "live": live, "targets": targets, "note": note, "dry_run": dry}


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

    open_count = getattr(account, "position_count", 0) or 0
    if open_count >= s.max_open_positions:
        raise Blocked(f"{open_count} positions already open (cap {s.max_open_positions})")
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
    risk_pct = await effective_risk_pct(s, symbol)

    order: Dict[str, Any] = {
        "venue": "mt5",
        "account_id": account.id,
        "account": f"{account.login}@{account.server}",
        "role": "demo" if account.id == getattr(s, "mt5_demo_account_id", None) else "live",
        "symbol": symbol,
        "levels": level_source,
        "side": action,
        "entry": round(price, 5),
        "stop_loss": round(stop_loss, 5),
        "take_profit": round(take_profit, 5),
        "risk_pct": risk_pct,
    }

    try:
        await _prepare_account(db, account, s, symbol=symbol)
    except Blocked as exc:
        return {**order, "status": "skipped", "reason": str(exc)}

    equity = float(getattr(account, "equity", 0) or getattr(account, "balance", 0) or 0)
    if equity <= 0:
        return {**order, "status": "skipped",
                "reason": "account equity unavailable — refusing to size blind"}

    # ── Dollar-risk cap (post-mortem 2026-08-28): a 4M demo @ 1% risks $40k per trade,
    # which is how the $50k losses were sized. Even with the 0.45× metals dampener
    # above, 0.45% of 4M is still $18k. Cap absolute risk so a single idea cannot
    # lose > $X regardless of demo equity. Tunable via RoomSettings.max_usd_risk_per_trade
    # when that column exists; otherwise use a sensible default that scales with account size.
    orig_equity = equity
    try:
        cap = float(getattr(s, "max_usd_risk_per_trade", 0) or 0)
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
    except Exception:
        equity = orig_equity

    # The risk budget AND the free margin both bound the lot, rounding floors so
    # it can never exceed the budget, and an account too small for one broker
    # minimum lot either trades at the floor (small-account mode) or not at all.
    volume, size_note = affordable_mt5_lot(
        equity=equity,
        free_margin=getattr(account, "free_margin", None),
        leverage=getattr(account, "leverage", None),
        risk_pct=risk_pct,
        entry=price,
        stop_loss=stop_loss,
        symbol=symbol,
        floor_lot=float(getattr(s, "mt5_lot_size", 0.01) or 0.01),
        max_risk_pct=float(getattr(s, "mt5_max_risk_pct", 5.0) or 5.0),
        small_account_mode=bool(getattr(s, "mt5_small_account_mode", True)),
    )
    if order.get("equity_capped"):
        size_note = f"${order['cap_usd']:.0f} cap (equity {orig_equity:,.0f}→{equity:,.0f}) · " + size_note
    order.update(equity=equity, volume=volume, sizing=size_note)
    if volume is None:
        return {**order, "status": "skipped",
                "reason": f"cannot size within risk limits — {size_note}"}

    try:
        placed = await mt5_client.place_order(
            login=account.login, server=account.server,
            password=account.password_encrypted,
            symbol=symbol, order_type=action, volume=volume, price=0,
            sl=stop_loss, tp=take_profit,
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
    return {**order, "status": "placed", "ticket": ticket}


async def _crypto_fill(
    db: AsyncSession, s: RoomSettings, *, symbol: str, signal_id: Any,
) -> Dict[str, Any]:
    """Take the trade on the exchange. Never raises.

    The live engine owns sizing, leverage, margin mode and the exchange's own
    symbol spelling, so the room hands it the decision rather than reimplementing
    any of that. In a dry run this venue does not run at all — the exchange has
    no demo to fall back to, so the paper account is the mirror and real crypto
    is left alone. That is the same guarantee the live MT5 account gets.
    """
    from app.trading.live import LiveTradeEngine

    base = {"venue": "crypto", "role": "exchange", "symbol": symbol}
    if s.dry_run:
        return {**base, "status": "skipped",
                "reason": "dry run — the exchange has no demo, so crypto trades on paper only"}
    if not signal_id:
        return {**base, "status": "skipped", "reason": "no signal was saved for this decision"}

    try:
        outcome = await LiveTradeEngine.execute_signal(db, signal_id)
    except Exception as exc:  # noqa: BLE001
        return {**base, "status": "error", "reason": str(exc)[:200]}
    if outcome.get("error"):
        return {**base, "status": "skipped", "reason": str(outcome["error"])[:200]}
    _bump_daily_counter()
    return {**base, "status": "placed", "reason": "sent to Bitget", **outcome}


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
            source=SignalSource.SYSTEM.value,
            symbol=symbol,
            action=SignalAction.BUY.value if action == "buy" else SignalAction.SELL.value,
            price=entry,
            timeframe="1h",
            strength=0.7,
            confidence=0.7,
            status=SignalStatus.PENDING.value,
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
    except Blocked as exc:
        return await _report(symbol, action, "skipped", str(exc))

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

    orders = await _route(
        db, s, symbol=symbol, action=action, price=entry, stop_loss=stop_loss,
        take_profit=take_profit, level_source="published signal card",
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
        {"orders": orders, "source": "signal_card"},
    )


async def _mark_signal_executed(db: AsyncSession, signal_id: Any) -> None:
    from app.models.database import SignalStatus

    if not signal_id:
        return
    try:
        row = await db.get(Signal, signal_id)
        if row is not None:
            row.status = SignalStatus.EXECUTED.value
            await db.commit()
    except Exception as exc:  # noqa: BLE001 — the trade is already placed
        logger.debug(f"[room-exec] could not close out signal {signal_id}: {exc}")


# ── Reporting ───────────────────────────────────────────────────────────────


async def _report(
    symbol: str, action: str, status: str, reason: str, order: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """One shape for every outcome, broadcast so the room can show it."""
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
        await event_bus.publish(Topics.ROOM_EXECUTION, payload)
    except Exception:  # noqa: BLE001
        pass
    return payload
