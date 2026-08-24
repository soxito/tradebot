"""Background monitor: polls enabled channels every minute and creates signals."""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import desc, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from plugins.TelegramSignalNewsPlugin.backend.config import build_config_from_db
from plugins.TelegramSignalNewsPlugin.backend.models import (
    SignalStatus,
    SourceKind,
    TelegramChannelSource,
    TelegramIngestMessage,
    TelegramParsedSignal,
    TelegramPluginSettings,
)
from plugins.TelegramSignalNewsPlugin.backend.schemas import TelegramPollRequest
from plugins.TelegramSignalNewsPlugin.backend.services.ingest_service import run_poll
from plugins.TelegramSignalNewsPlugin.backend.services.signal_parser import (
    classify_market_type,
    parse_entry_signal,
    parse_outcome,
)
from plugins.TelegramSignalNewsPlugin.backend.services.sniper_service import (
    run_sniper_cycle,
    reanalyze_skipped_signals,
    process_volume_channel_message,
    get_or_create_settings as get_sniper_settings,
    auto_close_positions_for_signal,
    _get_live_price,
)
from plugins.TelegramSignalNewsPlugin.backend.services.forex_price_service import (
    get_forex_price,
    is_forex_pair,
)
from plugins.TelegramSignalNewsPlugin.backend.models import (
    TelegramSniperTrade,
    SniperTradeStatus,
)
from plugins.TelegramSignalNewsPlugin.backend.services.news_sentiment_service import (
    process_news_to_sentiment,
)
from plugins.TelegramSignalNewsPlugin.backend.services.telegram_provider import (
    TelegramProviderRegistry,
)
from plugins.TelegramSignalNewsPlugin.backend.timezone_utils import now_utc_naive


def _emit_event(topic: str, data: dict) -> None:
    """Best-effort push to the core realtime EventBus (SSE). No-op without core."""
    try:
        from app.core.events import event_bus  # type: ignore
        event_bus.emit(topic, data)
    except Exception:
        pass


MONITOR_INTERVAL_SECONDS = 60          # base loop cadence (sniper trigger checks)
POLL_INTERVAL_SECONDS = 300            # poll Telegram for new messages every 5 min

#: In-flight update handlers. asyncio only holds weak references to tasks, so
#: without this a long-running reply can be garbage-collected mid-await.
_UPDATE_TASKS: set[asyncio.Task] = set()


def _spawn_update(update: dict, token: str, allowed: list, session_factory) -> None:
    """Handle one update concurrently so a slow reply cannot stall the loop."""
    async def _guarded() -> None:
        try:
            await TelegramSignalMonitor._process_update(update, token, allowed, session_factory)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("[BotPolling] Update processing error: {}", exc)

    task = asyncio.create_task(_guarded())
    _UPDATE_TASKS.add(task)
    task.add_done_callback(_UPDATE_TASKS.discard)
POLL_LIMIT_PER_CHANNEL = 50

# Zero-based ladder index the trailing stop locks onto and never moves past:
# 2 == TP3. Everything above it (TP4, TP5, …) is ridden with the stop parked at
# TP3 so the position can reach the channel's final target instead of being
# stopped out in profit halfway through the move.
TRAIL_LOCK_TP_INDEX = 2

_last_skipped_reanalyze: datetime | None = None


def _signal_dedupe_hash(channel_source_id: int, message_id: str, symbol: str) -> str:
    raw = f"{channel_source_id}:{message_id}:{symbol}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


async def create_signals_from_messages(
    db: AsyncSession, limit: int = 500, since_hours: int | None = 24
) -> dict[str, int]:
    """Scan recent ingest messages, create entry signals, and apply outcomes.

    Idempotent: a signal is keyed by (channel, message_id, symbol) so re-runs
    never duplicate. Returns counts for telemetry.

    ``since_hours`` bounds how far back to scan. The live monitor uses 24h for a
    cheap incremental pass; pass ``None`` (manual rebuild) to scan all history.
    """
    created = 0
    outcomes_applied = 0

    # Pull the most recent SIGNALS-kind messages only — volume/news channels
    # are filtered out so they don't eat up the scan budget.
    conditions = [TelegramIngestMessage.source_kind == SourceKind.SIGNALS]
    if since_hours is not None:
        conditions.append(
            TelegramIngestMessage.created_at >= now_utc_naive() - timedelta(hours=since_hours)
        )
    result = await db.execute(
        select(TelegramIngestMessage)
        .where(*conditions)
        .order_by(desc(TelegramIngestMessage.created_at))
        .limit(limit)
    )
    messages = list(result.scalars().all())

    # Map channel_source_id -> title for labelling
    chan_rows = await db.execute(select(TelegramChannelSource.id, TelegramChannelSource.title))
    channel_titles = {row[0]: row[1] for row in chan_rows.all()}

    # Map channel_source_id -> market_type ('crypto' | 'forex')
    chan_type_rows = await db.execute(select(TelegramChannelSource.id, TelegramChannelSource.market_type))
    channel_market_types: dict[int, str] = {row[0]: (row[1] or "crypto") for row in chan_type_rows.all()}

    # Process oldest-first so outcomes apply after their entry exists
    for msg in reversed(messages):
        text = msg.raw_text or ""

        # 1) Actionable entry signal?
        parsed = parse_entry_signal(text)
        if parsed is not None:
            # Route by the actual symbol first (XAUUSD -> forex, TRXUSDT ->
            # crypto); fall back to the channel's configured market type.
            market_type = classify_market_type(parsed.symbol) or channel_market_types.get(
                msg.channel_source_id, "crypto"
            )
            dedupe = _signal_dedupe_hash(msg.channel_source_id, msg.telegram_message_id, parsed.symbol)
            insert_stmt = (
                pg_insert(TelegramParsedSignal)
                .values(
                        channel_source_id=msg.channel_source_id,
                        channel_title=channel_titles.get(msg.channel_source_id),
                        telegram_message_id=msg.telegram_message_id,
                        symbol=parsed.symbol,
                        direction=parsed.direction,
                        leverage=parsed.leverage,
                        entry=parsed.entry,
                        entry_raw=parsed.entry_raw,
                        stop_loss=parsed.stop_loss,
                        stop_loss_raw=parsed.stop_loss_raw,
                        take_profits_json=parsed.take_profits,
                        status=SignalStatus.ACTIVE,
                        confidence=parsed.confidence,
                        raw_text=text,
                        posted_at=msg.posted_at,
                        dedupe_hash=dedupe,
                        market_type=market_type,
                )
                .on_conflict_do_nothing()
                .returning(TelegramParsedSignal.id)
                )
            inserted_id = (await db.execute(insert_stmt)).scalar_one_or_none()
            if inserted_id is not None:
                created += 1
            continue

        # 2) Outcome update for the latest active signal of this symbol/channel?
        outcome = parse_outcome(text)
        if outcome is not None:
            status_map = {
                "tp_hit": SignalStatus.TP_HIT,
                "sl_hit": SignalStatus.SL_HIT,
                "filled": SignalStatus.FILLED,
                "closed": SignalStatus.CLOSED,
                # opposite_direction: close the old signal AND cancel its pending
                # sniper trade so a new entry for the reversed direction is accepted.
                "opposite_direction": SignalStatus.CLOSED,
            }
            new_status = status_map.get(outcome.kind)
            if new_status is None:
                continue
            target = await db.execute(
                select(TelegramParsedSignal)
                .where(
                    TelegramParsedSignal.symbol == outcome.symbol,
                    TelegramParsedSignal.channel_source_id == msg.channel_source_id,
                    TelegramParsedSignal.status.in_(
                        [SignalStatus.ACTIVE, SignalStatus.FILLED, SignalStatus.TP_HIT]
                    ),
                )
                .order_by(desc(TelegramParsedSignal.created_at))
                .limit(1)
            )
            sig = target.scalar_one_or_none()
            if sig is not None:
                # Don't downgrade a closed/sl signal back to filled
                if outcome.kind == "filled" and sig.status != SignalStatus.ACTIVE:
                    continue
                sig.status = new_status
                sig.updated_at = now_utc_naive()
                outcomes_applied += 1

                # ── Opposite-direction close: cancel the pending sniper trade ──
                # The old position is invalid. Any PENDING trade for this signal
                # is cancelled so the incoming new (reversed) signal can create
                # a fresh sniper plan. PLACED trades are left open — the user
                # should close them manually on /trading.
                if outcome.kind == "opposite_direction":
                    sniper_q = await db.execute(
                        select(TelegramSniperTrade)
                        .where(
                            TelegramSniperTrade.signal_id == sig.id,
                            TelegramSniperTrade.status == SniperTradeStatus.PENDING,
                        )
                    )
                    for st in sniper_q.scalars().all():
                        st.status = SniperTradeStatus.SKIPPED
                        st.reason = (
                            f"Cancelled \u2014 opposite direction signal received: "
                            f"{outcome.detail or 'direction reversed'}"
                        )
                        st.updated_at = now_utc_naive()
                    # Auto-close any open positions — protects from losses even
                    # when the user is not monitoring the screen.
                    await db.commit()
                    try:
                        await auto_close_positions_for_signal(
                            db, sig.id,
                            reason="Opposite direction auto-close (monitor loop)",
                        )
                    except Exception as ac_exc:  # noqa: BLE001
                        logger.warning("Auto-close failed for signal {}: {}", sig.id, ac_exc)
                    logger.info(
                        "[Monitor] Opposite-direction close for {} \u2014 signal closed, "
                        "pending sniper cancelled, positions auto-closed.",
                        outcome.symbol,
                    )

                # ── SL-hit / channel-closed: flatten any still-open position ──
                # The channel says this trade is done. Cancel pending sniper
                # trades and close any sandbox/live/MT5 positions that the broker
                # hasn't already closed, so the signal truly leaves "active".
                elif outcome.kind in ("sl_hit", "closed"):
                    sniper_q = await db.execute(
                        select(TelegramSniperTrade)
                        .where(
                            TelegramSniperTrade.signal_id == sig.id,
                            TelegramSniperTrade.status == SniperTradeStatus.PENDING,
                        )
                    )
                    for st in sniper_q.scalars().all():
                        st.status = SniperTradeStatus.SKIPPED
                        st.reason = f"Cancelled \u2014 channel {outcome.kind}: {outcome.detail or ''}".strip()
                        st.updated_at = now_utc_naive()
                    await db.commit()
                    try:
                        summary = await auto_close_positions_for_signal(
                            db, sig.id,
                            reason=f"Channel {outcome.kind} auto-close (monitor loop)",
                        )
                        if any(summary.get(k) for k in ("mt5_closed", "live_closed", "sandbox_closed")):
                            logger.info(
                                "[Monitor] {} close for {} \u2014 flattened sandbox={} live={} mt5={}",
                                outcome.kind, outcome.symbol,
                                len(summary.get("sandbox_closed", [])),
                                len(summary.get("live_closed", [])),
                                len(summary.get("mt5_closed", [])),
                            )
                    except Exception as ac_exc:  # noqa: BLE001
                        logger.warning("Auto-close failed for signal {}: {}", sig.id, ac_exc)

    await db.commit()
    return {"created": created, "outcomes_applied": outcomes_applied}


async def reconcile_active_signals_from_live_price(
    db: AsyncSession,
    *,
    limit: int = 300,
) -> dict[str, int]:
    """Update trailing stop-loss and promote ACTIVE signals to TP_HIT / SL_HIT.

    Trailing stop-loss logic
    ========================
    1. Sort the signal's TP list in trade direction (ascending for LONG,
       descending for SHORT) so we process them in the order price hits them.
    2. Count how many TPs the current live price has already crossed.
    3. The stop moves once, at the ``TRAIL_LOCK_TP_INDEX`` (TP3) milestone,
       and then holds: the original stop stands below TP3, jumps to BREAK-EVEN
       when TP3 prints so the trade can no longer lose, and is frozen there
       while price works through TP4, TP5, … Ladders with fewer than 3 TPs use
       their final TP as the milestone.
    4. The trade closes as TP_HIT only when the FINAL TP is crossed, or when
       price retraces through the break-even stop.
    5. A signal closes as SL_HIT when price hits the original stop-loss before
       any TP has been reached (clean loss, no profit was locked).
    """
    # Fetch sniper settings once for the trailing percentage.
    sniper_cfg = await get_sniper_settings(db)
    trail_pct = float(getattr(sniper_cfg, "tp_trail_pct", 1.5)) / 100.0

    result = await db.execute(
        select(TelegramParsedSignal)
        .where(TelegramParsedSignal.status == SignalStatus.ACTIVE)
        .order_by(desc(TelegramParsedSignal.created_at))
        .limit(limit)
    )
    rows = list(result.scalars().all())

    # Cache prices to avoid double-fetching the same symbol in one pass.
    price_cache: dict[str, float | None] = {}

    # Signals that actually produced a live/sandbox order — only these get
    # lifecycle notifications (so untraded signals never spam the bot).
    notify_enabled = bool(getattr(sniper_cfg, "notify_executions", True))
    executed_ids: set[int] = set()
    if notify_enabled:
        ex = await db.execute(
            select(TelegramSniperTrade.signal_id).where(
                TelegramSniperTrade.status == SniperTradeStatus.PLACED
            )
        )
        executed_ids = {r[0] for r in ex.all()}

    checked = 0
    moved_tp = 0
    moved_sl = 0
    trailing_updated = 0

    for sig in rows:
        direction = (sig.direction or "").lower()
        if direction not in {"long", "short"}:
            continue

        sym = sig.symbol
        if sym not in price_cache:
            # Choose price source based on market type.
            mt = getattr(sig, "market_type", "crypto") or "crypto"
            if mt == "forex" or is_forex_pair(sym):
                price_cache[sym] = await get_forex_price(sym)
            else:
                price_cache[sym] = await _get_live_price(sym)
        live = price_cache[sym]
        if live is None:
            continue
        checked += 1

        is_long = direction == "long"
        entry_price = sig.entry if isinstance(sig.entry, (int, float)) else None

        # ── Sanitise trailing_sl: reset if it was set by a corrupted TP ─────
        # Rule 1: For LONG, trailing_sl must be >= original SL.
        #         For SHORT, trailing_sl must be <= original SL.
        # Rule 2 (stronger): trailing_sl must be on the PROFIT side of entry —
        #         for LONG it must be >= entry (locks profit above entry);
        #         for SHORT it must be <= entry.
        # If either rule is violated the trailing_sl came from a bogus TP and
        # we wipe it so the original SL takes over.
        orig_sl = sig.stop_loss if isinstance(sig.stop_loss, (int, float)) else None
        cur_trail = getattr(sig, "trailing_sl", None)

        def _is_bad_trail(trail: float) -> bool:
            if orig_sl is not None:
                if is_long and trail < orig_sl:
                    return True
                if not is_long and trail > orig_sl:
                    return True
            # Trailing SL must be on the profit side of entry to be meaningful.
            if entry_price is not None and entry_price > 0:
                if is_long and trail < entry_price * 0.999:
                    return True
                if not is_long and trail > entry_price * 1.001:
                    return True
            return False

        if cur_trail is not None and _is_bad_trail(cur_trail):
            logger.warning(
                "[Reconcile] {} {} trailing_sl ({}) is invalid (entry={}, orig_sl={}) "
                "— resetting (bad TP caused by parse error)",
                direction.upper(), sym, cur_trail, entry_price, orig_sl,
            )
            setattr(sig, "trailing_sl", None)
            setattr(sig, "tp_reached_count", 0)
            sig.updated_at = now_utc_naive()
            trailing_updated += 1

        # Build sorted TP ladder: first element = closest target for this direction.
        # IMPORTANT: only count TPs that are in the PROFIT direction from entry.
        # e.g. for LONG a TP of "4" (below entry 4203) is a parsing artefact and
        # must be excluded — otherwise it would be immediately "crossed" and set
        # the trailing SL to 4, blocking the real SL from ever triggering.
        raw_tps = [
            float(tp)
            for tp in (sig.take_profits_json or [])
            if isinstance(tp, (int, float)) and float(tp) > 0
        ]
        if entry_price is not None and entry_price > 0:
            # Allow a 0.1 % tolerance so TPs right at entry are not dropped.
            if is_long:
                raw_tps = [tp for tp in raw_tps if tp > entry_price * 0.999]
            else:
                raw_tps = [tp for tp in raw_tps if tp < entry_price * 1.001]
        tps = sorted(raw_tps, reverse=not is_long)  # asc for LONG, desc for SHORT

        # ── How many TPs has the current price already crossed? ──────────────
        if tps:
            if is_long:
                crossed = sum(1 for t in tps if live >= t)
            else:
                crossed = sum(1 for t in tps if live <= t)
        else:
            crossed = 0

        prev_reached = int(getattr(sig, "tp_reached_count", 0) or 0)

        # ── Advance trailing SL when new TP levels are hit ───────────────────
        # The stop moves exactly ONCE, at the TP3 milestone, and then holds:
        #
        #   before TP3 : the signal's original stop stands
        #   at TP3     : the stop jumps to BREAK-EVEN (the entry price), so the
        #                trade can no longer lose
        #   after TP3  : frozen — TP4, TP5, … do not move it
        #
        # Ratcheting the stop onto every TP crossed meant a routine pullback
        # after TP4/TP5 closed a position that was still heading for the
        # channel's last target. Break-even removes the downside while leaving
        # far more room to run than parking the stop on TP3 itself would.
        # Ladders shorter than 3 TPs use their final TP as the milestone.
        changed = False
        trail_before = getattr(sig, "trailing_sl", None)
        if crossed > prev_reached and tps:
            lock_idx = min(TRAIL_LOCK_TP_INDEX, len(tps) - 1)
            setattr(sig, "tp_reached_count", crossed)
            reached_lock = (crossed - 1) >= lock_idx
            # Break-even needs a known entry; without one fall back to the
            # milestone TP so the trade is still protected.
            break_even = (
                float(entry_price) if (entry_price and entry_price > 0) else tps[lock_idx]
            )
            if reached_lock:
                current_trail = getattr(sig, "trailing_sl", None)
                if current_trail is None:
                    setattr(sig, "trailing_sl", break_even)
                else:
                    # Never give back a stop that is already safer than break-even.
                    setattr(sig, "trailing_sl", (
                        max(current_trail, break_even) if is_long
                        else min(current_trail, break_even)
                    ))
                sig.updated_at = now_utc_naive()
                changed = True
                trailing_updated += 1
            logger.info(
                "[Trailing SL] {} {} — TP {}/{} hit @ {} | stop = {}{}",
                direction.upper(), sym, crossed, len(tps), tps[crossed - 1],
                getattr(sig, "trailing_sl", None) or orig_sl,
                (
                    f" (BREAK-EVEN from TP{lock_idx + 1}, held until TP{len(tps)})"
                    if reached_lock else f" (original stop until TP{lock_idx + 1})"
                ),
            )
            if notify_enabled and sig.id in executed_ids:
                try:
                    from plugins.TelegramSignalNewsPlugin.backend.services import notifications as _notif
                    await _notif.notify(_notif.format_tp_hit(
                        symbol=sym, direction=direction, tp_index=crossed, tp_total=len(tps),
                        tp_price=tps[crossed - 1], trailing_sl=getattr(sig, "trailing_sl", None),
                        channel=getattr(sig, "channel_title", None),
                    ), db)
                except Exception:  # noqa: BLE001
                    pass

        # ── Sync trailing SL to the linked sim AND live positions ────────
        # When trailing_sl changes it must reach every place the position
        # actually lives: the sim book (so /trading shows the stop) and, for a
        # real telegram order, the live exchange — otherwise the locked profit
        # only exists on the signal row, not on the money at risk.
        async def _apply_trailing_sl(new_sl: float) -> None:
            try:
                trade_row = await db.execute(
                    select(TelegramSniperTrade).where(
                        TelegramSniperTrade.signal_id == sig.id,
                        TelegramSniperTrade.status == SniperTradeStatus.PLACED,
                    )
                )
                sniper_trades = list(trade_row.scalars().all())
            except Exception:
                sniper_trades = []
            for sniper_trade in sniper_trades:
                if sniper_trade.sim_order_id is not None:
                    try:
                        from app.models.database import SimPosition
                        pos_row = await db.execute(
                            select(SimPosition).where(
                                SimPosition.order_id == sniper_trade.sim_order_id,
                                SimPosition.status == "open",
                            )
                        )
                        sim_pos = pos_row.scalar_one_or_none()
                        if sim_pos is not None:
                            sim_pos.stop_loss = new_sl
                            try:
                                sim_pos.sl_type = "trailing"
                            except Exception:
                                pass
                            logger.info(
                                "[Trailing SL→Sim] {} {} position id={} stop_loss→{}",
                                direction.upper(), sym, sim_pos.id, new_sl,
                            )
                    except Exception as sync_exc:  # noqa: BLE001
                        logger.warning("[Trailing SL→Sim] sync failed for {}: {}", sym, sync_exc)
                # Live money: push the trail to the exchange so the stop actually moves.
                if getattr(sniper_trade, "executed_mode", None) == "live":
                    try:
                        from app.exchanges.manager import exchange_manager, SupportedExchange
                        connector = exchange_manager.get_exchange(SupportedExchange.BITGET)
                        if connector is not None:
                            await connector.replace_tpsl_orders(
                                symbol=sniper_trade.symbol,
                                hold_side="long" if is_long else "short",
                                new_sl=new_sl,
                            )
                            logger.info(
                                "[Trailing SL→Live] {} {} SL→{} on Bitget",
                                direction.upper(), sniper_trade.symbol, new_sl,
                            )
                    except Exception as live_exc:  # noqa: BLE001
                        logger.warning(
                            "[Trailing SL→Live] {} push failed: {}", sniper_trade.symbol, live_exc,
                        )
                # Forex on MT5: the locked stop has to reach the broker too, or
                # it only exists on the signal row while the money still sits
                # behind the signal's original stop.
                if str(getattr(sniper_trade, "executed_mode", "") or "").startswith("mt5"):
                    tickets = [
                        t.strip() for t in str(sniper_trade.live_order_id or "").split(",")
                        if t.strip().isdigit()
                    ]
                    if tickets:
                        try:
                            from plugins.MT5TradingPlugin.backend.services.mt5_client import mt5_client
                            from plugins.MT5TradingPlugin.backend.models import MT5Account
                            acct_q = await db.execute(
                                select(MT5Account).where(MT5Account.api_reachable.is_(True))
                            )
                            for acct in acct_q.scalars().all():
                                for ticket in tickets:
                                    try:
                                        await mt5_client.modify_order(
                                            login=acct.login, server=acct.server,
                                            password=acct.password_encrypted,
                                            ticket=int(ticket), sl=new_sl,
                                        )
                                    except Exception:  # noqa: BLE001
                                        continue  # ticket belongs to another account
                            logger.info(
                                "[Trailing SL→MT5] {} {} SL→{} on ticket(s) {}",
                                direction.upper(), sniper_trade.symbol, new_sl, ",".join(tickets),
                            )
                        except Exception as mt5_exc:  # noqa: BLE001
                            logger.warning(
                                "[Trailing SL→MT5] {} push failed: {}",
                                sniper_trade.symbol, mt5_exc,
                            )

        new_trail_now = getattr(sig, "trailing_sl", None)
        # Only push when the stop actually moved — once locked at TP3, later TP
        # crossings re-derive the same value and must not re-hit the exchange.
        if changed and new_trail_now is not None and new_trail_now != trail_before:
            await _apply_trailing_sl(new_trail_now)

        # ── Full TP reached → CLOSE as TP_HIT (do NOT keep trailing) ─────────
        # Once every take-profit is crossed the trade is complete: lock it in as
        # a win and stop tracking it. (Previously a live percentage trail kept
        # the signal ACTIVE until price retraced to the trailing SL, which left
        # fully-completed signals showing as active on /telegram-signals.)
        if tps and crossed >= len(tps):
            sig.status = SignalStatus.TP_HIT
            sig.updated_at = now_utc_naive()
            moved_tp += 1
            logger.info(
                "[Reconcile] {} {} closed as TP_HIT — all {} TPs reached (live={})",
                direction.upper(), sym, len(tps), live,
            )
            if notify_enabled and sig.id in executed_ids:
                try:
                    from plugins.TelegramSignalNewsPlugin.backend.services import notifications as _notif
                    await _notif.notify(_notif.format_close(
                        symbol=sym, direction=direction, kind="tp", price=live,
                        channel=getattr(sig, "channel_title", None),
                    ), db)
                except Exception:  # noqa: BLE001
                    pass
            continue

        # ── Resolve effective stop-loss ───────────────────────────────────────
        effective_sl = getattr(sig, "trailing_sl", None) or sig.stop_loss

        # ── Safety fallback: if original SL is breached AND no valid TPs have
        # been captured yet, always close as SL_HIT regardless of trailing_sl.
        # This handles edge cases where trailing_sl was set by a bad TP value
        # that slipped through the sanitisation above.
        orig_sl_breached = bool(
            orig_sl is not None
            and int(getattr(sig, "tp_reached_count", 0) or 0) == 0
            and (
                (is_long and live <= orig_sl)
                or (not is_long and live >= orig_sl)
            )
        )

        # ── Check if effective SL has been breached ───────────────────────────
        # The trailing stop sits exactly ON the TP level price just reached, so
        # it must be breached STRICTLY: price has to trade back THROUGH the
        # locked level, not merely touch it. With `<=` the trade closed on the
        # same tick the TP was hit (live == trailing_sl), banking TP1 the
        # instant it printed and killing every run before it started. The
        # original hard stop keeps `<=` — touching it is a stop-out.
        trail_sl = getattr(sig, "trailing_sl", None)
        sl_breached = orig_sl_breached or bool(
            effective_sl is not None
            and (
                (is_long and (live < effective_sl if trail_sl is not None else live <= effective_sl))
                or (not is_long and (live > effective_sl if trail_sl is not None else live >= effective_sl))
            )
        )

        if sl_breached:
            if orig_sl_breached:
                # Original SL hit with no captured profits → definite loss.
                sig.status = SignalStatus.SL_HIT
                moved_sl += 1
            elif int(getattr(sig, "tp_reached_count", 0) or 0) > 0:
                # Price fell back to the trailing SL after profits were locked → TP_HIT.
                sig.status = SignalStatus.TP_HIT
                moved_tp += 1
            else:
                # Hit effective SL with no TPs captured → loss.
                sig.status = SignalStatus.SL_HIT
                moved_sl += 1
            sig.updated_at = now_utc_naive()
            logger.info(
                "[Reconcile] {} {} closed as {} (live={}, eff_sl={}, orig_sl={})",
                direction.upper(), sym, sig.status.value, live, effective_sl, orig_sl,
            )
            if notify_enabled and sig.id in executed_ids:
                try:
                    from plugins.TelegramSignalNewsPlugin.backend.services import notifications as _notif
                    _kind = "tp" if sig.status == SignalStatus.TP_HIT else "sl"
                    await _notif.notify(_notif.format_close(
                        symbol=sym, direction=direction, kind=_kind, price=live,
                        channel=getattr(sig, "channel_title", None),
                    ), db)
                except Exception:  # noqa: BLE001
                    pass

    if moved_tp or moved_sl or trailing_updated:
        await db.commit()

    return {
        "checked": checked,
        "tp_hit": moved_tp,
        "sl_hit": moved_sl,
        "trailing_updated": trailing_updated,
        "updated": moved_tp + moved_sl,
    }

async def expire_stale_forex_signals(db: AsyncSession, *, hours: int = 3) -> int:
    """Move stale, never-executed FOREX signals out of ACTIVE → EXPIRED.

    A forex signal still ACTIVE more than `hours` after it was posted, with no
    PLACED sniper trade, is a dead/missed trade. Marking it EXPIRED keeps the
    Active tab showing only live trades; the UI surfaces these under "Not
    Traded". Crypto signals are left untouched (this is forex-only).
    """
    from plugins.TelegramSignalNewsPlugin.backend.services.forex_price_service import is_forex_pair

    executed = {
        r[0]
        for r in (
            await db.execute(
                select(TelegramSniperTrade.signal_id).where(
                    TelegramSniperTrade.status == SniperTradeStatus.PLACED
                )
            )
        ).all()
    }

    rows = await db.execute(
        select(TelegramParsedSignal).where(TelegramParsedSignal.status == SignalStatus.ACTIVE)
    )
    now = now_utc_naive()
    expired = 0
    for sig in rows.scalars().all():
        is_fx = (getattr(sig, "market_type", "") == "forex") or is_forex_pair(sig.symbol)
        if not is_fx or sig.id in executed:
            continue
        ref = sig.posted_at or sig.created_at
        if ref is None:
            continue
        if (now - ref).total_seconds() / 3600.0 < hours:
            continue
        sig.status = SignalStatus.EXPIRED
        sig.updated_at = now
        expired += 1
    if expired:
        await db.commit()
        logger.info("[Expire] moved {} stale forex signals (>{}h) to EXPIRED", expired, hours)
    return expired

async def run_monitor_cycle(db: AsyncSession, *, do_poll: bool = True) -> dict[str, Any]:
    """One monitor tick.

    When `do_poll` is True: poll enabled channels from Telegram, build signals,
    process news → sentiment, then run the sniper. When False: run the sniper
    only (fast trigger checks between the 5-minute Telegram polls).
    """
    poll_channels = 0
    messages_saved = 0
    signal_stats = {"created": 0, "outcomes_applied": 0}
    live_outcome_stats: dict[str, Any] = {"checked": 0, "tp_hit": 0, "sl_hit": 0, "updated": 0}
    news_stats: dict[str, Any] = {"skipped": True}

    if do_poll:
        result = await db.execute(select(TelegramPluginSettings).limit(1))
        settings = result.scalars().first()
        cfg = build_config_from_db(settings)
        registry = TelegramProviderRegistry(cfg)

        # Poll all enabled channels (run_poll handles enabled-only selection)
        poll = await run_poll(
            db=db,
            request=TelegramPollRequest(user_id="0", limit_per_channel=POLL_LIMIT_PER_CHANNEL),
            provider_registry=registry,
            cfg=cfg,
        )
        poll_channels = poll.channels_scanned
        messages_saved = poll.messages_saved

        signal_stats = await create_signals_from_messages(db)

        # Push NEWS messages into the core sentiment system (feeds auto-trading)
        try:
            news_stats = await process_news_to_sentiment(db)
        except Exception as exc:  # noqa: BLE001
            logger.warning("News→sentiment failed: {}", exc)
            news_stats = {"error": str(exc)}

    # Safety net: advance trailing SL and move stale ACTIVE signals to TP_HIT / SL_HIT.
    try:
        live_outcome_stats = await reconcile_active_signals_from_live_price(db)
        signal_stats["outcomes_applied"] += int(live_outcome_stats.get("updated", 0))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Live outcome reconciliation failed: {}", exc)
        live_outcome_stats = {"error": str(exc)}

    # Keep the Active tab clean: expire never-executed forex signals older than 3h.
    try:
        expired_forex = await expire_stale_forex_signals(db, hours=3)
        if expired_forex:
            live_outcome_stats["forex_expired"] = expired_forex
    except Exception as exc:  # noqa: BLE001
        logger.warning("Forex signal expiry failed: {}", exc)

    # Run the sniper auto-trade engine over fresh signals (no-op if disabled)
    try:
        sniper_stats = await run_sniper_cycle(db)
    except Exception as exc:  # noqa: BLE001 — sniper must never break the monitor
        logger.warning("Sniper cycle failed: {}", exc)
        sniper_stats = {"error": str(exc)}

    # Re-analyse skipped signals on their configured cadence
    global _last_skipped_reanalyze
    skipped_stats: dict[str, Any] = {"skipped": True}
    try:
        sniper_cfg = await get_sniper_settings(db)
        cadence = int(getattr(sniper_cfg, "skipped_reanalyze_minutes", 15) or 15)
        now_dt = now_utc_naive()
        if cadence > 0 and (
            _last_skipped_reanalyze is None
            or (now_dt - _last_skipped_reanalyze).total_seconds() >= cadence * 60
        ):
            skipped_stats = await reanalyze_skipped_signals(db)
            _last_skipped_reanalyze = now_dt
    except Exception as exc:  # noqa: BLE001
        logger.warning("Skipped-signal re-analysis failed: {}", exc)
        skipped_stats = {"error": str(exc)}

    # Process volume-channel messages (if a channel is configured)
    vol_stats: dict[str, Any] = {"skipped": True}
    try:
        sniper_cfg2 = await get_sniper_settings(db)
        vol_chan = getattr(sniper_cfg2, "volume_channel_id", None)
        if vol_chan and do_poll:
            # Fetch recent messages from the volume channel regardless of source_kind
            # (the channel may be SIGNALS or NEWS — we process it as volume either way)
            since = now_utc_naive() - timedelta(minutes=10)
            vol_msgs_res = await db.execute(
                select(TelegramIngestMessage)
                .where(
                    TelegramIngestMessage.channel_source_id == vol_chan,
                    TelegramIngestMessage.created_at >= since,
                )
                .order_by(desc(TelegramIngestMessage.created_at))
                .limit(50)  # whale channels post frequently
            )
            vol_triggered: list[str] = []
            processed = 0
            for vmsg in vol_msgs_res.scalars().all():
                res = await process_volume_channel_message(db, vmsg.raw_text or "")
                if res.get("triggered"):
                    vol_triggered.extend(res["triggered"])
                if res.get("reassessed", 0) > 0:
                    processed += 1
            vol_stats = {
                "channel_id": vol_chan,
                "messages_processed": processed,
                "triggered": list(set(vol_triggered)),
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("Volume-channel processing failed: {}", exc)
        vol_stats = {"error": str(exc)}

    return {
        "polled": do_poll,
        "polled_channels": poll_channels,
        "messages_saved": messages_saved,
        "signals_created": signal_stats["created"],
        "outcomes_applied": signal_stats["outcomes_applied"],
        "live_outcomes": live_outcome_stats,
        "news_sentiment": news_stats,
        "sniper": sniper_stats,
        "skipped_reanalysis": skipped_stats,
        "volume_channel": vol_stats,
    }


class TelegramSignalMonitor:
    """Singleton background monitor. Idempotent start; safe to call repeatedly."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._bot_polling_task: asyncio.Task | None = None
        self._running = False
        self._last_run: datetime | None = None
        self._last_poll: datetime | None = None
        self._last_result: dict[str, Any] | None = None
        self._last_error: str | None = None
        self._session_factory = None

    def is_running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    def status(self) -> dict[str, Any]:
        return {
            "running": self.is_running(),
            "interval_seconds": MONITOR_INTERVAL_SECONDS,
            "poll_interval_seconds": POLL_INTERVAL_SECONDS,
            "last_run": self._last_run.isoformat() if self._last_run else None,
            "last_poll": self._last_poll.isoformat() if self._last_poll else None,
            "last_result": self._last_result,
            "last_error": self._last_error,
        }

    def ensure_started(self, session_factory) -> None:
        """Start the loop if not already running (called from a request handler)."""
        self._session_factory = session_factory
        if self.is_running():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._loop())
        self._running = True
        logger.info("📡 Telegram signal monitor started (every {}s)", MONITOR_INTERVAL_SECONDS)
        # Also start bot polling if it is configured
        self._maybe_start_bot_polling(loop, session_factory)

    def stop(self) -> None:
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
        if self._bot_polling_task is not None and not self._bot_polling_task.done():
            self._bot_polling_task.cancel()
        self._bot_polling_task = None

    def start_bot_polling(self, session_factory) -> bool:
        """Start the Telegram bot polling loop if not already running.

        Returns True if started (or already running), False if polling is
        disabled in the database config.
        """
        self._session_factory = session_factory
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        if self._bot_polling_task is not None and not self._bot_polling_task.done():
            return True
        self._bot_polling_task = loop.create_task(self._bot_polling_loop(session_factory))
        logger.info("🤖 Telegram bot polling loop started")
        return True

    def stop_bot_polling(self) -> None:
        """Stop the bot polling loop."""
        if self._bot_polling_task is not None and not self._bot_polling_task.done():
            self._bot_polling_task.cancel()
        self._bot_polling_task = None
        logger.info("🤖 Telegram bot polling loop stopped")

    @staticmethod
    async def _process_update(update: dict, token: str, allowed: list, session_factory) -> None:
        """Handle one Telegram update and send whatever reply it produces."""
        from plugins.TelegramSignalNewsPlugin.backend.services.bot_service import (
            answer_callback_query,
            send_message,
        )
        from plugins.TelegramSignalNewsPlugin.backend.services.command_service import (
            parse_and_execute,
        )

        # Resolve chat_id — works for both message and callback_query
        if "callback_query" in update:
            cq = update["callback_query"]
            chat_id = (cq.get("message") or {}).get("chat", {}).get("id")
            cq_id = cq.get("id")
        else:
            chat_id = (
                (update.get("message") or update.get("edited_message") or {})
                .get("chat", {}).get("id")
            )
            cq_id = None

        async with session_factory() as db2:
            result = await parse_and_execute(update, token, allowed, db2)

        # Normalise to 3-tuple (text, parse_mode, reply_markup)
        if len(result) == 2:
            reply_text, parse_mode, reply_markup = result[0], result[1], None
        else:
            reply_text, parse_mode, reply_markup = result

        # Acknowledge button press first so Telegram removes the spinner
        if cq_id:
            ack_text = "⏳ Processing…" if reply_text else "✅"
            await answer_callback_query(token, cq_id, text=ack_text)

        if reply_text and chat_id:
            await send_message(token, chat_id, reply_text, parse_mode, reply_markup)

    def _maybe_start_bot_polling(self, loop: asyncio.AbstractEventLoop, session_factory) -> None:
        """Start bot polling if configured — best-effort, never raises."""
        try:
            if self._bot_polling_task is not None and not self._bot_polling_task.done():
                return
            self._bot_polling_task = loop.create_task(
                self._bot_polling_loop(session_factory)
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[BotPolling] Auto-start skipped: {}", exc)

    async def _bot_polling_loop(self, session_factory) -> None:
        """Long-poll Telegram Bot API for incoming updates.

        Runs as a background task alongside the signal monitor.  Only active
        when ``TelegramBotConfig.polling_enabled`` is True in the DB.
        """
        from plugins.TelegramSignalNewsPlugin.backend.models import TelegramBotConfig
        from plugins.TelegramSignalNewsPlugin.backend.services.bot_service import (
            get_updates,
            send_message,
            answer_callback_query,
            sync_bot_commands,
        )
        from plugins.TelegramSignalNewsPlugin.backend.services.command_service import (
            parse_and_execute,
        )
        from sqlalchemy import select

        await asyncio.sleep(3)  # let the main loop start first
        logger.info("🤖 Bot polling loop running")

        offset: int | None = None
        commands_synced_for: str | None = None  # token the / menu was pushed for

        while True:
            try:
                async with session_factory() as db:
                    cfg_row = (await db.execute(select(TelegramBotConfig).limit(1))).scalars().first()
                    if cfg_row is None or not cfg_row.polling_enabled:
                        # Polling disabled — sleep and re-check
                        await asyncio.sleep(10)
                        continue

                    token = cfg_row.bot_token_override or ""
                    if not token:
                        # Fall back to plugin settings token
                        from plugins.TelegramSignalNewsPlugin.backend.models import TelegramPluginSettings
                        ps = (await db.execute(select(TelegramPluginSettings).limit(1))).scalars().first()
                        token = (ps.bot_token if ps else "") or ""

                    if not token:
                        await asyncio.sleep(10)
                        continue

                    allowed = list(cfg_row.allowed_chat_ids_json or [])
                    if cfg_row.last_update_id is not None:
                        offset = cfg_row.last_update_id + 1

                # Push the / menu once per token so it always matches the code.
                if commands_synced_for != token:
                    if await sync_bot_commands(token):
                        commands_synced_for = token

                resp = await get_updates(token, offset=offset, timeout=2)
                if not resp.get("ok"):
                    await asyncio.sleep(5)
                    continue

                updates = resp.get("result", [])
                for update in updates:
                    update_id = update.get("update_id")
                    if update_id is not None:
                        offset = update_id + 1

                    # Each update is handled on its own task. Awaiting them here
                    # meant one slow command — an image read is ~60s, a room
                    # session minutes — stalled every other chat behind it, and
                    # stopped this loop collecting new updates at all.
                    _spawn_update(update, token, list(allowed), session_factory)

                # Persist last update_id
                if updates and offset is not None:
                    try:
                        async with session_factory() as db3:
                            cfg2 = (await db3.execute(
                                select(TelegramBotConfig).limit(1)
                            )).scalars().first()
                            if cfg2 is not None:
                                cfg2.last_update_id = offset - 1
                                await db3.commit()
                    except Exception:  # noqa: BLE001
                        pass

                await asyncio.sleep(1)

            except asyncio.CancelledError:
                logger.info("🤖 Bot polling loop cancelled")
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning("[BotPolling] Loop error: {}", exc)
                await asyncio.sleep(5)

    async def _loop(self) -> None:
        # Small initial delay so startup requests finish first
        await asyncio.sleep(2)
        while self._running:
            # Poll Telegram at most every POLL_INTERVAL_SECONDS; run sniper each tick.
            now = now_utc_naive()
            do_poll = (
                self._last_poll is None
                or (now - self._last_poll).total_seconds() >= POLL_INTERVAL_SECONDS
            )
            try:
                async with self._session_factory() as db:
                    self._last_result = await run_monitor_cycle(db, do_poll=do_poll)
                    self._last_run = now_utc_naive()
                    if do_poll:
                        self._last_poll = self._last_run
                    self._last_error = None
                    if do_poll:
                        logger.info(
                            "📡 Telegram poll tick: {} new signal(s), {} outcome(s), {} msg saved",
                            self._last_result.get("signals_created"),
                            self._last_result.get("outcomes_applied"),
                            self._last_result.get("messages_saved"),
                        )
                    # Push realtime status to SSE subscribers so the Telegram page
                    # updates live instead of polling every 30s.
                    _emit_event("monitor.status", {
                        "last_run": self._last_run.isoformat() if self._last_run else None,
                        "signals_created": self._last_result.get("signals_created"),
                        "outcomes_applied": self._last_result.get("outcomes_applied"),
                        "messages_saved": self._last_result.get("messages_saved"),
                    })
                    if (self._last_result.get("signals_created") or 0) > 0:
                        _emit_event("signal.new", {
                            "source": "telegram",
                            "count": self._last_result.get("signals_created"),
                        })
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001 — never let the loop die
                self._last_error = str(exc)
                logger.warning("Telegram monitor tick failed: {}", exc)

            try:
                await asyncio.sleep(MONITOR_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                break

        self._running = False
        self._task = None
        self._bot_polling_task: asyncio.Task | None = None


# Module-level singleton
signal_monitor = TelegramSignalMonitor()
