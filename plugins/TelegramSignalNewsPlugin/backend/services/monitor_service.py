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


MONITOR_INTERVAL_SECONDS = 60          # base loop cadence (sniper trigger checks)
POLL_INTERVAL_SECONDS = 300            # poll Telegram for new messages every 5 min
POLL_LIMIT_PER_CHANNEL = 50

_last_skipped_reanalyze: datetime | None = None


def _signal_dedupe_hash(channel_source_id: int, message_id: str, symbol: str) -> str:
    raw = f"{channel_source_id}:{message_id}:{symbol}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


async def create_signals_from_messages(db: AsyncSession, limit: int = 500) -> dict[str, int]:
    """Scan recent ingest messages, create entry signals, and apply outcomes.

    Idempotent: a signal is keyed by (channel, message_id, symbol) so re-runs
    never duplicate. Returns counts for telemetry.
    """
    created = 0
    outcomes_applied = 0

    # Pull the most recent SIGNALS-kind messages only — volume/news channels
    # are filtered out so they don't eat up the scan budget. We only look at
    # messages from the last 24 hours so the set stays manageable even as the
    # history grows over months.
    since_24h = now_utc_naive() - timedelta(hours=24)
    result = await db.execute(
        select(TelegramIngestMessage)
        .where(
            TelegramIngestMessage.source_kind == SourceKind.SIGNALS,
            TelegramIngestMessage.created_at >= since_24h,
        )
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
                        market_type=channel_market_types.get(msg.channel_source_id, "crypto"),
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
    3. If that count has increased since the last check → advance the trailing
       SL to the new highest TP crossed (locks in profit at each level).
    4. When the FINAL TP is crossed the trailing SL is set to that TP value and
       we also enable a live percentage trail so further profit is protected:
           trailing_sl = max(trailing_sl, live * (1 - trail_pct))   [LONG]
           trailing_sl = min(trailing_sl, live * (1 + trail_pct))   [SHORT]
    5. A signal closes as TP_HIT when price falls back to the trailing SL
       (which means we closed in profit).
    6. A signal closes as SL_HIT when price hits the original stop-loss before
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
        changed = False
        if crossed > prev_reached and tps:
            # The highest TP crossed in the sorted ladder.
            new_trail_anchor = tps[crossed - 1]
            setattr(sig, "tp_reached_count", crossed)
            # Ensure trailing SL only ever moves in the profit direction.
            current_trail = getattr(sig, "trailing_sl", None)
            if current_trail is None:
                setattr(sig, "trailing_sl", new_trail_anchor)
            else:
                if is_long:
                    setattr(sig, "trailing_sl", max(current_trail, new_trail_anchor))
                else:
                    setattr(sig, "trailing_sl", min(current_trail, new_trail_anchor))
            sig.updated_at = now_utc_naive()
            changed = True
            trailing_updated += 1
            logger.info(
                "[Trailing SL] {} {} — TP {}/{} hit @ {} | new trailing SL = {}",
                direction.upper(), sym, crossed, len(tps),
                new_trail_anchor, getattr(sig, "trailing_sl", None),
            )

        # ── Sync trailing SL to any linked open sim position ─────────────
        # When trailing_sl has just changed, push it to the sim account
        # position so /trading reflects the correct stop-loss level.
        new_trail_now = getattr(sig, "trailing_sl", None)
        if changed and new_trail_now is not None:
            try:
                trade_row = await db.execute(
                    select(TelegramSniperTrade).where(
                        TelegramSniperTrade.signal_id == sig.id,
                        TelegramSniperTrade.status == SniperTradeStatus.PLACED,
                        TelegramSniperTrade.sim_order_id.isnot(None),
                    )
                )
                for sniper_trade in trade_row.scalars().all():
                    from app.models.database import SimPosition
                    pos_row = await db.execute(
                        select(SimPosition).where(
                            SimPosition.order_id == sniper_trade.sim_order_id,
                            SimPosition.status == "open",
                        )
                    )
                    sim_pos = pos_row.scalar_one_or_none()
                    if sim_pos is not None:
                        sim_pos.stop_loss = new_trail_now
                        # Tag the position as having a trailing stop-loss so the
                        # /trading page can show the 🔒 indicator.
                        try:
                            sim_pos.sl_type = "trailing"
                        except Exception:
                            pass
                        logger.info(
                            "[Trailing SL→Sim] {} {} position id={} stop_loss→{}",
                            direction.upper(), sym, sim_pos.id, new_trail_now,
                        )
            except Exception as sync_exc:  # noqa: BLE001
                logger.warning("[Trailing SL→Sim] sync failed for {}: {}", sym, sync_exc)

        # ── Live percentage trail after all TPs are exhausted ────────────────
        current_trail = getattr(sig, "trailing_sl", None)
        if crossed >= len(tps) and len(tps) > 0 and current_trail is not None and trail_pct > 0:
            if is_long:
                candidate = live * (1.0 - trail_pct)
                if candidate > current_trail:
                    setattr(sig, "trailing_sl", candidate)
                    sig.updated_at = now_utc_naive()
                    changed = True
            else:
                candidate = live * (1.0 + trail_pct)
                if candidate < current_trail:
                    setattr(sig, "trailing_sl", candidate)
                    sig.updated_at = now_utc_naive()
                    changed = True

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
        sl_breached = orig_sl_breached or bool(
            effective_sl is not None
            and (
                (is_long and live <= effective_sl)
                or (not is_long and live >= effective_sl)
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

    if moved_tp or moved_sl or trailing_updated:
        await db.commit()

    return {
        "checked": checked,
        "tp_hit": moved_tp,
        "sl_hit": moved_sl,
        "trailing_updated": trailing_updated,
        "updated": moved_tp + moved_sl,
    }


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

    def stop(self) -> None:
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

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


# Module-level singleton
signal_monitor = TelegramSignalMonitor()
