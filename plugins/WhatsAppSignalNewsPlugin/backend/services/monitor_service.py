"""WhatsApp Signal Monitor Service.

Background monitoring for signal TP/SL hits, position tracking,
and auto-reconciliation.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional

from loguru import logger
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.core.events import event_bus
from app.exchanges.manager import exchange_manager, SupportedExchange
from plugins.WhatsAppSignalNewsPlugin.backend.config import (
    WhatsAppPluginConfig,
    build_config_from_db,
)
from plugins.WhatsAppSignalNewsPlugin.backend.models import (
    WhatsAppParsedSignal,
    WhatsAppPluginSettings,
    WhatsAppSniperTrade,
    SignalStatus,
    SniperTradeStatus,
)


class WhatsAppSignalMonitor:
    """Background signal monitor for TP/SL tracking."""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._interval = 30  # seconds
        self._last_run: Optional[datetime] = None
        self._signals_monitored = 0
        self._positions_checked = 0
        self._errors: list = []

    def ensure_started(self, session_factory=None) -> None:
        """Start the monitor loop if not already running."""
        if self._running and self._task and not self._task.done():
            return

        self._running = True
        self._errors = []
        self._task = asyncio.create_task(self._monitor_loop(session_factory or AsyncSessionLocal))
        logger.info("📡 WhatsApp signal monitor started")

    def stop(self) -> None:
        """Stop the monitor loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("📡 WhatsApp signal monitor stopped")

    def status(self) -> Dict:
        """Get monitor status."""
        return {
            "running": self._running,
            "interval_seconds": self._interval,
            "last_run": self._last_run.isoformat() if self._last_run else None,
            "signals_monitored": self._signals_monitored,
            "positions_checked": self._positions_checked,
            "errors": self._errors[-10:] if self._errors else [],
        }

    async def _monitor_loop(self, session_factory):
        """Main monitor loop."""
        while self._running:
            try:
                await self._run_monitor_cycle(session_factory)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"WhatsApp monitor cycle error: {e}")
                self._errors.append(f"{datetime.now().isoformat()}: {str(e)}")

            # Wait for next interval
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break

    async def _run_monitor_cycle(self, session_factory):
        """Run one monitoring cycle."""
        self._last_run = datetime.now()
        async with session_factory() as db:
            await self._check_active_signals(db)

    async def _check_active_signals(self, db: AsyncSession):
        """Check all active signals for TP/SL hits."""
        # Load config
        result = await db.execute(select(WhatsAppPluginSettings).limit(1))
        db_settings = result.scalars().first()
        live_config = build_config_from_db(db_settings)

        # Get BITGET connector for price data
        connector = exchange_manager.get_exchange(SupportedExchange.BITGET)
        if not connector:
            logger.warning("BITGET connector not available for signal monitoring")
            return

        # Get active signals
        result = await db.execute(
            select(WhatsAppParsedSignal).where(
                WhatsAppParsedSignal.status.in_([
                    SignalStatus.ACTIVE.value,
                    SignalStatus.FILLED.value,
                    SignalStatus.TP_HIT.value,
                ])
            )
        )
        signals = result.scalars().all()

        self._signals_monitored = len(signals)
        self._positions_checked = 0

        for signal in signals:
            try:
                await self._check_signal(db, signal, connector, live_config)
                self._positions_checked += 1
            except Exception as e:
                logger.warning(f"Error checking signal {signal.id} ({signal.symbol}): {e}")
                self._errors.append(f"{datetime.now().isoformat()}: signal {signal.id} - {str(e)}")

        await db.commit()

    async def _check_signal(
        self,
        db: AsyncSession,
        signal: WhatsAppParsedSignal,
        connector,
        config: WhatsAppPluginConfig,
    ):
        """Check a single signal for TP/SL hits."""
        # Get current price from exchange
        ticker = await connector.get_ticker(signal.symbol)
        if not ticker:
            return

        current_price = float(ticker.get("last") or ticker.get("close") or 0)
        if current_price <= 0:
            return

        # Check take profits
        tp_hit = await self._check_take_profits(db, signal, current_price)
        if tp_hit:
            return

        # Check stop loss
        sl_hit = await self._check_stop_loss(db, signal, current_price)
        if sl_hit:
            return

        # Check expiration (signals older than 24h without fill)
        if signal.status == SignalStatus.ACTIVE.value:
            age = datetime.now() - signal.posted_at
            if age > timedelta(hours=24):
                signal.status = SignalStatus.EXPIRED.value
                signal.updated_at = datetime.now()
                logger.info(f"Signal {signal.id} ({signal.symbol}) expired after 24h")
                await self._emit_signal_update(signal)

    async def _check_take_profits(
        self,
        db: AsyncSession,
        signal: WhatsAppParsedSignal,
        current_price: float,
    ) -> bool:
        """Check if any take profit level was hit."""
        if not signal.take_profits:
            return False

        tps = signal.take_profits if isinstance(signal.take_profits, list) else json.loads(signal.take_profits)
        if not tps:
            return False

        direction_multiplier = 1 if signal.direction in ("buy", "long") else -1

        for i, tp_price in enumerate(tps):
            if i < signal.tp_reached_count:
                continue  # Already hit

            # Check if TP hit (accounting for direction)
            hit = False
            if direction_multiplier > 0 and current_price >= tp_price:
                hit = True
            elif direction_multiplier < 0 and current_price <= tp_price:
                hit = True

            if hit:
                signal.tp_reached_count = i + 1
                signal.updated_at = datetime.now()

                if signal.tp_reached_count >= len(tps):
                    signal.status = SignalStatus.TP_HIT.value
                    logger.info(f"🎯 Signal {signal.id} ALL TPs hit for {signal.symbol}")
                else:
                    signal.status = SignalStatus.TP_HIT.value  # Partial TP
                    logger.info(f"🎯 Signal {signal.id} TP{signal.tp_reached_count} hit for {signal.symbol} @ {tp_price}")

                await self._emit_signal_update(signal)
                await self._check_sniper_tp(db, signal, i + 1)
                return True

        return False

    async def _check_stop_loss(
        self,
        db: AsyncSession,
        signal: WhatsAppParsedSignal,
        current_price: float,
    ) -> bool:
        """Check if stop loss was hit."""
        if not signal.stop_loss:
            return False

        direction_multiplier = 1 if signal.direction in ("buy", "long") else -1

        hit = False
        if direction_multiplier > 0 and current_price <= signal.stop_loss:
            hit = True
        elif direction_multiplier < 0 and current_price >= signal.stop_loss:
            hit = True

        if hit:
            signal.status = SignalStatus.SL_HIT.value
            signal.updated_at = datetime.now()
            logger.warning(f"🛑 Signal {signal.id} SL hit for {signal.symbol} @ {signal.stop_loss}")
            await self._emit_signal_update(signal)
            await self._check_sniper_sl(db, signal)
            return True

        return False

    async def _check_sniper_tp(
        self,
        db: AsyncSession,
        signal: WhatsAppParsedSignal,
        tp_number: int,
    ):
        """Check and handle sniper trades for TP hit."""
        result = await db.execute(
            select(WhatsAppSniperTrade).where(
                WhatsAppSniperTrade.signal_id == signal.id,
                WhatsAppSniperTrade.status.in_([
                    SniperTradeStatus.PLACED.value,
                    SniperTradeStatus.FILLED.value,
                ]),
            )
        )
        trades = result.scalars().all()

        for trade in trades:
            logger.info(f"Sniper trade {trade.id} TP{tp_number} hit for {trade.symbol}")

    async def _check_sniper_sl(
        self,
        db: AsyncSession,
        signal: WhatsAppParsedSignal,
    ):
        """Check and handle sniper trades for SL hit."""
        result = await db.execute(
            select(WhatsAppSniperTrade).where(
                WhatsAppSniperTrade.signal_id == signal.id,
                WhatsAppSniperTrade.status.in_([
                    SniperTradeStatus.PLACED.value,
                    SniperTradeStatus.FILLED.value,
                ]),
            )
        )
        trades = result.scalars().all()

        for trade in trades:
            trade.status = SniperTradeStatus.FAILED.value
            trade.reason = "Stop loss hit"
            trade.closed_at = datetime.now()
            trade.updated_at = datetime.now()
            logger.warning(f"Sniper trade {trade.id} SL hit for {trade.symbol}")

    async def _emit_signal_update(self, signal: WhatsAppParsedSignal):
        """Emit signal update event for SSE."""
        try:
            await event_bus.publish(
                "whatsapp_signal_update",
                {
                    "signal_id": signal.id,
                    "symbol": signal.symbol,
                    "direction": signal.direction,
                    "status": signal.status,
                    "tp_reached": signal.tp_reached_count,
                    "updated_at": signal.updated_at.isoformat() if signal.updated_at else None,
                }
            )
        except Exception as e:
            logger.debug(f"Failed to emit signal update: {e}")

    # ────────────────────────────────────────────────────────────────
    # Manual Triggers
    # ────────────────────────────────────────────────────────────────

    async def trigger_check(self, session_factory, signal_id: Optional[int] = None):
        """Manually trigger a check for specific signal or all."""
        connector = exchange_manager.get_exchange(SupportedExchange.BITGET)
        if not connector:
            return

        async with session_factory() as db:
            result = await db.execute(select(WhatsAppPluginSettings).limit(1))
            db_settings = result.scalars().first()
            live_config = build_config_from_db(db_settings)

            if signal_id:
                result = await db.execute(
                    select(WhatsAppParsedSignal).where(WhatsAppParsedSignal.id == signal_id)
                )
                signal = result.scalar_one_or_none()
                if signal:
                    await self._check_signal(db, signal, connector, live_config)
            else:
                await self._check_active_signals(db)
            await db.commit()


# Global monitor instance
signal_monitor = WhatsAppSignalMonitor()


# ────────────────────────────────────────────────────────────────────
# Reconciliation & Cleanup
# ────────────────────────────────────────────────────────────────────

async def reconcile_active_signals(db: AsyncSession) -> Dict[str, int]:
    """Reconcile signal statuses with actual position data.

    Useful after restart or when monitor was down.
    """
    stats = {"checked": 0, "updated": 0, "closed": 0, "errors": 0}

    connector = exchange_manager.get_exchange(SupportedExchange.BITGET)
    if not connector:
        return stats

    result = await db.execute(
        select(WhatsAppParsedSignal).where(
            WhatsAppParsedSignal.status.in_([
                SignalStatus.ACTIVE.value,
                SignalStatus.FILLED.value,
                SignalStatus.TP_HIT.value,
            ])
        )
    )
    signals = result.scalars().all()

    for signal in signals:
        stats["checked"] += 1
        try:
            # Get current price
            ticker = await connector.get_ticker(signal.symbol)
            if not ticker:
                continue
            current_price = float(ticker.get("last") or ticker.get("close") or 0)
            if current_price <= 0:
                continue

            old_status = signal.status

            # Check TPs
            if signal.take_profits:
                tps = signal.take_profits if isinstance(signal.take_profits, list) else json.loads(signal.take_profits)
                direction = 1 if signal.direction in ("buy", "long") else -1
                for i, tp in enumerate(tps):
                    if i >= signal.tp_reached_count:
                        if (direction > 0 and current_price >= tp) or (direction < 0 and current_price <= tp):
                            signal.tp_reached_count = i + 1
                            signal.status = SignalStatus.TP_HIT.value

            # Check SL
            if signal.stop_loss:
                direction = 1 if signal.direction in ("buy", "long") else -1
                if (direction > 0 and current_price <= signal.stop_loss) or \
                   (direction < 0 and current_price >= signal.stop_loss):
                    signal.status = SignalStatus.SL_HIT.value

            if signal.status != old_status:
                signal.updated_at = datetime.now()
                stats["updated"] += 1
                await signal_monitor._emit_signal_update(signal)

                if signal.status in (SignalStatus.TP_HIT.value, SignalStatus.SL_HIT.value):
                    stats["closed"] += 1

        except Exception as e:
            logger.warning(f"Reconciliation error for signal {signal.id}: {e}")
            stats["errors"] += 1

    await db.commit()
    return stats


async def cleanup_old_messages(db: AsyncSession, days: int = 30) -> int:
    """Clean up old processed messages."""
    from plugins.WhatsAppSignalNewsPlugin.backend.models import WhatsAppMessage
    cutoff = datetime.now() - timedelta(days=days)
    result = await db.execute(
        delete(WhatsAppMessage).where(
            WhatsAppMessage.processed == True,
            WhatsAppMessage.received_at < cutoff,
        )
    )
    await db.commit()
    return result.rowcount


async def cleanup_old_signals(db: AsyncSession, days: int = 90) -> int:
    """Clean up old closed signals."""
    cutoff = datetime.now() - timedelta(days=days)
    result = await db.execute(
        delete(WhatsAppParsedSignal).where(
            WhatsAppParsedSignal.status.in_([
                SignalStatus.CLOSED.value,
                SignalStatus.EXPIRED.value,
                SignalStatus.CANCELLED.value,
            ]),
            WhatsAppParsedSignal.updated_at < cutoff,
        )
    )
    await db.commit()
    return result.rowcount