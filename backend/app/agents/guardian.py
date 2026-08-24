"""Stay with the trade after the board has left the room.

The room meets, decides, and publishes. Until now that was the end of the
agents' involvement: whatever happened next was left to a fixed stop-loss and,
on MT5, a break-even/trailing pass that only ever moved a stop *forward*. That
handles the trade that goes right. It does nothing for the two cases that cost
money:

  the sweep      price reaches through the stop to take the liquidity resting
                 under it and then goes where the analysis said it would. The
                 stop is hit; the idea was correct. (A published gold long at
                 4501 with its stop at 4475 was taken out by a wick to 4451 and
                 the market then ran to 4541 — through every target on the card.)

  the turn       the higher timeframe genuinely reverses after publication. The
                 position sits there bleeding to a stop that is now only a
                 slower way of being wrong.

:mod:`app.agents.guard_read` tells those two apart. This module is what acts on
the answer — on the broker for an open position, and on the published signal for
one that has not been taken. It runs on its own fast cadence, because both cases
are decided in minutes, not on the hour the board next sits.

Nothing here widens risk silently. A stop pushed out is always paired with a
proportional partial close, so the money at risk stays what the trade was sized
to lose, and any single position can be widened only once.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import guard_read
from app.core.events import Topics, event_bus

#: How often the guard runs. The board's own cadence can be an hour; a sweep is
#: over in ten minutes, so this loop is deliberately independent of it.
GUARD_INTERVAL_S = 60

#: Per-ticket / per-signal quiet period after an action, so a market sitting on
#: a level cannot produce a broker call every cycle.
COOLDOWN_S = 180.0

#: Timeframes the guard reads: the one it manages on, and the one that gets the
#: casting vote on whether the idea is still alive.
MANAGE_TF = "15m"
STRUCTURE_TF = "4h"

_last_action: Dict[str, float] = {}
#: Positions/signals whose stop has already been pushed out once.
_widened: set[str] = set()
#: Symbols no feed could produce bars for, and when to bother trying again.
#: Signal channels carry parse noise ("NOK"), and a guard running every minute
#: must not spend the whole minute asking four providers about a symbol that
#: does not exist.
_no_bars: Dict[str, float] = {}
_NO_BARS_BACKOFF_S = 1800.0
#: Positions already announced for a given action. A trail that advances every
#: few minutes is doing its job quietly; the message that matters is the first
#: one, when the trade stopped being able to lose.
_announced: set[str] = set()


def may_widen(*, already_widened: bool, trailing_sl: Any, tp_reached_count: Any) -> bool:
    """Is this trade still one whose stop may be pushed out?

    No, once it has proven itself. A stop that has been pulled up to protect a
    printed target is the trade's profit; moving it back out would hand that
    profit back to the market in the name of protecting the trade. Widening is
    for a trade that has not yet gone anywhere — and then only once.
    """
    if already_widened:
        return False
    if trailing_sl:
        return False
    try:
        return int(tp_reached_count or 0) <= 0
    except (TypeError, ValueError):
        return True


def _cooled(key: str) -> bool:
    last = _last_action.get(key)
    return last is None or (time.time() - last) >= COOLDOWN_S


def _mark(key: str) -> None:
    _last_action[key] = time.time()


def reset_state() -> None:
    """Forget cooldowns and widen history — for tests and for a fresh start."""
    _last_action.clear()
    _widened.clear()
    _no_bars.clear()
    _announced.clear()


async def _read(symbol: str) -> tuple[list, list]:
    """The two candle series the verdict is formed from, or two empty lists."""
    from app.services import candles as candle_source

    until = _no_bars.get(symbol)
    if until and time.time() < until:
        return [], []

    ltf = await candle_source.fetch(symbol, MANAGE_TF)
    htf = await candle_source.fetch(symbol, STRUCTURE_TF)
    if len(ltf) < 30 or len(htf) < 30:
        _no_bars[symbol] = time.time() + _NO_BARS_BACKOFF_S
        return [], []
    _no_bars.pop(symbol, None)
    return ltf, htf


async def _announce(text: str) -> None:
    """Tell the user what the desk just did to their trade. Never raises."""
    try:
        from plugins.TelegramSignalNewsPlugin.backend.services import notifications

        await notifications.notify(text)
    except Exception as exc:  # noqa: BLE001 — plugin-optional, delivery is best effort
        logger.debug(f"[Guardian] notification skipped: {exc}")


async def _publish(payload: Dict[str, Any]) -> None:
    try:
        await event_bus.publish(Topics.ROOM_EXECUTION, {"guard": True, **payload})
    except Exception:  # noqa: BLE001
        pass


# ── Open MT5 positions ───────────────────────────────────────────────────────


def _round_lot(volume: float) -> float:
    return max(0.0, round(volume / 0.01) * 0.01)


async def guard_mt5_positions(
    db: AsyncSession, *, accounts: Optional[List[Any]] = None, send: bool = True,
) -> List[Dict[str, Any]]:
    """Watch every position this app opened on an account the room may manage.

    ``accounts`` is the room's routing decision (see
    :func:`app.agents.execution.mt5_targets`), and in a dry run it contains the
    demo account only — so a live position is never touched by the desk while
    the room is in dry run, not even to protect it. Passing ``None`` means every
    active account, which is only used by tooling that has already decided.

    Only app-placed positions are managed. A trade the user opened by hand in
    the terminal is theirs: moving its stop would be overriding a decision the
    desk was never asked to make.
    """
    from app.trading.order_tags import is_app_order
    from plugins.MT5TradingPlugin.backend.models import (
        MT5Account, MT5AccountStatus, MT5Position,
    )
    from plugins.MT5TradingPlugin.backend.services.mt5_client import mt5_client

    acts: List[Dict[str, Any]] = []
    if accounts is None:
        accounts = (await db.execute(
            select(MT5Account).where(MT5Account.status == MT5AccountStatus.ACTIVE)
        )).scalars().all()
    if not accounts:
        return acts

    for account in accounts:
        positions = (await db.execute(
            select(MT5Position).where(MT5Position.account_id == account.id)
        )).scalars().all()

        for pos in positions:
            if not is_app_order(getattr(pos, "comment", "")):
                continue
            key = f"mt5:{account.id}:{pos.mt5_ticket}"
            if not _cooled(key):
                continue

            side = getattr(pos.side, "value", str(pos.side))
            entry = float(pos.price_open or 0)
            price = float(pos.price_current or entry or 0)
            if entry <= 0 or price <= 0:
                continue

            ltf, htf = await _read(pos.symbol)
            if not ltf or not htf:
                continue

            verdict = guard_read.assess(
                side=side, entry=entry, stop=pos.sl,
                take_profits=[pos.tp] if pos.tp else None,
                price=price, ltf_candles=ltf, htf_candles=htf,
            )
            act = await _apply_mt5(
                account, pos, verdict, key=key, send=send, client=mt5_client,
            )
            if act:
                acts.append(act)
    return acts


async def _apply_mt5(
    account: Any,
    pos: Any,
    verdict: guard_read.GuardVerdict,
    *,
    key: str,
    send: bool,
    client: Any,
) -> Optional[Dict[str, Any]]:
    """Carry out one verdict on one broker position."""
    if verdict.action == "hold":
        return None
    # Widening is a one-time concession to a sweep, not a habit.
    if verdict.action == "widen_stop" and key in _widened:
        return None

    report: Dict[str, Any] = {
        "venue": "mt5", "account_id": account.id, "ticket": pos.mt5_ticket,
        "symbol": pos.symbol, "verdict": verdict.verdict, "action": verdict.action,
        "reason": verdict.summary(), "sent": send,
    }

    if verdict.action in {"advance_stop", "widen_stop"}:
        if not verdict.suggested_stop:
            return None
        report["stop"] = float(verdict.suggested_stop)
        if send:
            await client.modify_order(
                login=account.login, server=account.server,
                password=account.password_encrypted,
                ticket=int(pos.mt5_ticket), sl=round(float(verdict.suggested_stop), 5),
                tp=pos.tp,
            )
        if verdict.action == "widen_stop":
            _widened.add(key)
            volume = float(pos.volume or 0)
            cut = _round_lot(volume * (verdict.reduce_fraction or 0))
            remaining = _round_lot(volume - cut)
            # A cut that leaves nothing (or takes nothing) is not a hedge against
            # the sweep, it is just closing the trade — skip it and let the wider
            # stop stand on the size already on.
            if cut >= 0.01 and remaining >= 0.01:
                report["closed_volume"] = cut
                if send:
                    await client.close_position(
                        login=account.login, server=account.server,
                        password=account.password_encrypted,
                        ticket=int(pos.mt5_ticket), volume=cut,
                    )
            else:
                report["closed_volume"] = 0.0
                report["reason"] += " (too small to cut — stop widened on full size)"

    elif verdict.action == "close":
        if send:
            await client.close_position(
                login=account.login, server=account.server,
                password=account.password_encrypted, ticket=int(pos.mt5_ticket),
            )

    _mark(key)
    logger.info(
        f"[Guardian] {pos.symbol} #{pos.mt5_ticket} → {verdict.action} "
        f"({verdict.verdict}){'' if send else ' [not sent — execution is off]'}: "
        f"{verdict.summary()}"
    )
    await _publish(report)
    # Widening and closing are decisions the trader has to know about. A trail
    # step is housekeeping: say it once, when the stop first moves, and then let
    # it work. The room UI shows every step either way.
    announce_key = f"{key}:{verdict.action}"
    if verdict.action != "advance_stop" or announce_key not in _announced:
        _announced.add(announce_key)
        await _announce(_position_message(pos, verdict, report))
    return report


def _position_message(pos: Any, verdict: guard_read.GuardVerdict, report: Dict[str, Any]) -> str:
    head = {
        "widen_stop": "🛡 Stop moved behind the sweep",
        "advance_stop": "🔒 Stop advanced — profit secured",
        "close": "🚪 Position closed early",
    }.get(verdict.action, "🛡 Position review")
    lines = [f"{head} — <b>{pos.symbol}</b> #{pos.mt5_ticket}", verdict.summary()]
    if report.get("stop"):
        lines.append(f"New stop: <b>{float(report['stop']):.5g}</b>")
    if report.get("closed_volume"):
        lines.append(f"Partial close: {report['closed_volume']:.2f} lots")
    if not report.get("sent"):
        lines.append("<i>Execution is switched off — nothing was sent to the broker.</i>")
    return "\n".join(lines)


# ── Open crypto positions ────────────────────────────────────────────────────


def _position_size(pos: Dict[str, Any]) -> float:
    for key in ("total", "available", "size", "contracts"):
        try:
            value = abs(float(pos.get(key) or 0))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0.0


async def guard_crypto_positions(db: AsyncSession, *, send: bool = True) -> List[Dict[str, Any]]:
    """Watch the exchange side of the book the same way the broker side is watched.

    The guard only ever covered MT5, which was defensible while the crypto venue
    never fired. It fires now, so a Bitget position opened by the room would
    otherwise sit unmanaged behind the stop it was opened with — the exact
    situation the guard exists to end.

    Ownership comes from the app's own trade rows rather than a tag: nothing the
    user does by hand in the exchange UI is written there, so a row on this table
    is a trade this app opened.
    """
    from sqlalchemy import select

    from app.exchanges.manager import SupportedExchange, exchange_manager
    from app.models.database import Trade

    acts: List[Dict[str, Any]] = []
    connector = exchange_manager.get_exchange(SupportedExchange.BITGET)
    if connector is None:
        return acts

    rows = (await db.execute(
        select(Trade).where(
            Trade.exchange == "bitget",
            Trade.status == "open",
            Trade.trade_side == "open",
        ).limit(50)
    )).scalars().all()
    if not rows:
        return acts

    try:
        exchange_positions = await connector.get_futures_positions()
    except Exception as exc:  # noqa: BLE001 — no positions read, nothing to manage
        logger.debug(f"[Guardian] exchange positions unavailable: {exc}")
        return acts

    by_symbol = {
        str(p.get("symbol") or "").upper().replace("/", ""): p
        for p in (exchange_positions or [])
        if _position_size(p) > 0
    }

    for trade in rows:
        key = f"crypto:{trade.id}"
        if not _cooled(key):
            continue
        live_pos = by_symbol.get(str(trade.symbol or "").upper().replace("/", ""))
        if live_pos is None:
            continue  # closed on the exchange already; the reconciler owns that

        entry = float(trade.average_price or trade.price or 0)
        stop = trade.stop_loss
        if entry <= 0:
            continue

        from app.services import market_data

        quote = await market_data.get_quote(trade.symbol, db=db)
        price = float(getattr(quote, "price", 0) or 0)
        if price <= 0:
            continue

        ltf, htf = await _read(trade.symbol)
        if not ltf or not htf:
            continue

        verdict = guard_read.assess(
            side=str(trade.side or "buy"), entry=entry, stop=stop,
            take_profits=[trade.take_profit] if trade.take_profit else None,
            price=price, ltf_candles=ltf, htf_candles=htf,
        )
        act = await _apply_crypto(
            db, connector, trade, live_pos, verdict, key=key, send=send,
        )
        if act:
            acts.append(act)
    return acts


async def _apply_crypto(
    db: AsyncSession,
    connector: Any,
    trade: Any,
    live_pos: Dict[str, Any],
    verdict: guard_read.GuardVerdict,
    *,
    key: str,
    send: bool,
) -> Optional[Dict[str, Any]]:
    """Carry out one verdict on one exchange position."""
    if verdict.action == "hold":
        return None
    if verdict.action == "widen_stop" and key in _widened:
        return None

    hold_side = str(live_pos.get("holdSide") or ("long" if str(trade.side) == "buy" else "short"))
    raw_symbol = str(live_pos.get("symbol") or trade.symbol)
    size = _position_size(live_pos)

    report: Dict[str, Any] = {
        "venue": "crypto", "trade_id": trade.id, "symbol": trade.symbol,
        "verdict": verdict.verdict, "action": verdict.action,
        "reason": verdict.summary(), "sent": send,
    }

    try:
        if verdict.action in {"advance_stop", "widen_stop"} and verdict.suggested_stop:
            report["stop"] = float(verdict.suggested_stop)
            if send:
                # A position-level stop, so it applies to whatever size is left
                # after any partial close rather than to the original order.
                await connector.place_tpsl_order(
                    symbol=raw_symbol, margin_coin="USDT", plan_type="pos_loss",
                    trigger_price=float(verdict.suggested_stop), hold_side=hold_side,
                )
                trade.stop_loss = float(verdict.suggested_stop)
                await db.commit()

            if verdict.action == "widen_stop":
                _widened.add(key)
                cut = size * (verdict.reduce_fraction or 0)
                if cut > 0 and cut < size:
                    report["closed_size"] = cut
                    if send:
                        await _close_crypto(connector, raw_symbol, hold_side, cut)
                else:
                    report["closed_size"] = 0.0
                    report["reason"] += " (too small to cut — stop widened on full size)"

        elif verdict.action == "close":
            report["closed_size"] = size
            if send:
                await _close_crypto(connector, raw_symbol, hold_side, size)
    except Exception as exc:  # noqa: BLE001 — one position, not the whole pass
        logger.warning(f"[Guardian] {trade.symbol} exchange action failed: {exc}")
        report["error"] = str(exc)[:200]

    _mark(key)
    logger.info(
        f"[Guardian] {trade.symbol} (bitget) → {verdict.action} ({verdict.verdict})"
        f"{'' if send else ' [not sent — execution is off]'}: {verdict.summary()}"
    )
    await _publish(report)
    announce_key = f"{key}:{verdict.action}"
    if verdict.action != "advance_stop" or announce_key not in _announced:
        _announced.add(announce_key)
        await _announce(_crypto_message(trade, verdict, report))
    return report


async def _close_crypto(connector: Any, symbol: str, hold_side: str, size: float) -> None:
    """Market-close *size* of a position, reduce-only so it can only shrink it.

    ccxt rather than the native client on purpose: it resolves one-way versus
    hedge mode and the reduce-only flag per account, which the native call's
    ``tradeSide`` parameter gets wrong on a one-way account.
    """
    close_side = "sell" if hold_side.lower() in {"long", "buy"} else "buy"
    await connector.exchange.create_order(
        symbol=symbol, type="market", side=close_side, amount=size,
        params={"reduceOnly": True},
    )


def _crypto_message(trade: Any, verdict: guard_read.GuardVerdict, report: Dict[str, Any]) -> str:
    head = {
        "widen_stop": "🛡 Stop moved behind the sweep",
        "advance_stop": "🔒 Stop advanced — profit secured",
        "close": "🚪 Position closed early",
    }.get(verdict.action, "🛡 Position review")
    lines = [f"{head} — <b>{trade.symbol}</b> (Bitget)", verdict.summary()]
    if report.get("stop"):
        lines.append(f"New stop: <b>{float(report['stop']):.6g}</b>")
    if report.get("closed_size"):
        lines.append(f"Closed: {float(report['closed_size']):.6g}")
    if report.get("error"):
        lines.append(f"⚠️ The exchange refused: {report['error']}")
    if not report.get("sent"):
        lines.append("<i>Execution is switched off — nothing was sent to the exchange.</i>")
    return "\n".join(lines)


# ── Published signals that are still live ────────────────────────────────────


def _now():
    from plugins.TelegramSignalNewsPlugin.backend.models import now_utc_naive

    return now_utc_naive()


async def guard_active_signals(db: AsyncSession, *, limit: int = 60) -> List[Dict[str, Any]]:
    """Re-read every active published signal against the market as it is now.

    A signal nobody has taken still matters: it is on the channel, people are in
    it, and the numbers on it are the ones they are managing to. When the stop is
    about to be taken by a raid the desk moves it and says so; when the idea is
    genuinely dead the desk says that, instead of leaving a losing plan standing.
    """
    from app.services import market_data
    from plugins.TelegramSignalNewsPlugin.backend.models import (
        SignalStatus, TelegramParsedSignal,
    )

    acts: List[Dict[str, Any]] = []
    rows = (await db.execute(
        select(TelegramParsedSignal)
        .where(TelegramParsedSignal.status == SignalStatus.ACTIVE)
        .order_by(TelegramParsedSignal.created_at.desc())
        .limit(limit)
    )).scalars().all()

    for sig in rows:
        key = f"sig:{sig.id}"
        if not _cooled(key):
            continue
        direction = (sig.direction or "").lower()
        entry = sig.entry if isinstance(sig.entry, (int, float)) else None
        stop = sig.trailing_sl or sig.stop_loss
        if direction not in {"long", "short"} or not entry or not stop:
            continue

        quote = await market_data.get_quote(sig.symbol, db=db)
        price = float(getattr(quote, "price", 0) or 0)
        if price <= 0:
            continue

        # Nothing to decide while the trade is neither near its stop nor in
        # front — and deciding it anyway costs two candle fetches per signal per
        # minute across the whole active list.
        is_long = direction == "long"
        risk = abs(float(entry) - float(stop)) or 1e-9
        to_stop = abs(price - float(stop))
        in_profit = (price > entry) if is_long else (price < entry)
        if to_stop > risk * 0.6 and not in_profit:
            continue

        ltf, htf = await _read(sig.symbol)
        if not ltf or not htf:
            continue

        verdict = guard_read.assess(
            side=direction, entry=float(entry), stop=float(stop),
            take_profits=list(sig.take_profits_json or []), price=price,
            ltf_candles=ltf, htf_candles=htf,
        )

        # A published signal has no size to cut, so the only honest way to move
        # its stop is to say so on the channel — which is what a trader needs in
        # order to move their own.
        widenable = may_widen(
            already_widened=key in _widened,
            trailing_sl=sig.trailing_sl,
            tp_reached_count=getattr(sig, "tp_reached_count", 0),
        )

        if verdict.action == "widen_stop" and widenable and verdict.suggested_stop:
            _widened.add(key)
            old = float(stop)
            sig.stop_loss = float(verdict.suggested_stop)
            sig.trailing_sl = None
            sig.updated_at = _now()
            await db.commit()
            acts.append({"signal_id": sig.id, "symbol": sig.symbol,
                         "action": "widen_stop", "stop": sig.stop_loss})
            await _announce(
                f"🛡 <b>{sig.symbol}</b> {direction.upper()} — stop moved "
                f"{old:.6g} → <b>{sig.stop_loss:.6g}</b>\n{verdict.summary()}\n"
                "<i>The old level sat where the resting liquidity is; the structure "
                "this trade was taken on has not broken. Size down rather than "
                "carrying the wider stop at full risk.</i>"
            )
            _mark(key)

        elif verdict.action == "close":
            sig.status = SignalStatus.CLOSED
            sig.updated_at = _now()
            await db.commit()
            acts.append({"signal_id": sig.id, "symbol": sig.symbol, "action": "close"})
            await _announce(
                f"⚠️ <b>{sig.symbol}</b> {direction.upper()} — the setup is void.\n"
                f"{verdict.summary()}\n<i>Close it rather than waiting for the stop.</i>"
            )
            _mark(key)

    return acts


# ── The cycle ────────────────────────────────────────────────────────────────


async def guard_cycle(db: AsyncSession) -> Dict[str, Any]:
    """One full pass. Never raises — a bad cycle must not stop the next one."""
    summary: Dict[str, Any] = {"positions": [], "crypto": [], "signals": [], "errors": []}

    accounts: List[Any] = []
    send = False
    dry_run = True
    try:
        from app.agents.execution import get_settings, mt5_targets

        s = await get_settings(db)
        send = bool(s.execution_enabled)
        dry_run = bool(s.dry_run)
        routing = await mt5_targets(db, s)
        accounts = routing["targets"]
        summary["routing"] = routing["note"]
    except Exception as exc:  # noqa: BLE001 — default to touching nothing
        summary["errors"].append(f"settings: {exc}")

    try:
        if accounts:
            summary["positions"] = await guard_mt5_positions(
                db, accounts=accounts, send=send,
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[Guardian] MT5 pass failed: {exc}")
        summary["errors"].append(f"mt5: {exc}")

    try:
        # The exchange has no dry-run account, so the room only manages crypto
        # when it is armed — the same rule that stops it *opening* crypto in a
        # dry run. Managing a position the room did not open would be reaching
        # into the user's own book.
        if not dry_run:
            summary["crypto"] = await guard_crypto_positions(db, send=send)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[Guardian] crypto pass failed: {exc}")
        summary["errors"].append(f"crypto: {exc}")

    try:
        summary["signals"] = await guard_active_signals(db)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[Guardian] signal pass failed: {exc}")
        summary["errors"].append(f"signals: {exc}")

    return summary
