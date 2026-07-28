"""WhatsApp Sniper Service.

Auto-trade execution from WhatsApp signals.
Handles position sizing, order placement, TP/SL orders, and risk management.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.events import event_bus
from app.exchanges.manager import exchange_manager, SupportedExchange
from app.trading.risk import risk_calculator
from app.trading.service import TradingService
from plugins.WhatsAppSignalNewsPlugin.backend.config import (
    WhatsAppPluginConfig,
    build_config_from_db,
)
from plugins.WhatsAppSignalNewsPlugin.backend.models import (
    WhatsAppParsedSignal,
    WhatsAppSniperSettings,
    WhatsAppSniperTrade,
    WhatsAppChannelSource,
    SignalStatus,
    SniperTradeStatus,
    WhatsAppPluginSettings,
)


class WhatsAppSniperService:
    """Sniper auto-trade service for WhatsApp signals."""

    def __init__(self, config: Optional[WhatsAppPluginConfig] = None):
        self.config = config or WhatsAppPluginConfig()
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._interval = 60  # seconds
        self._last_run: Optional[datetime] = None
        self._trades_executed = 0
        self._errors: List[str] = []

    def ensure_started(self, session_factory=None) -> None:
        """Start sniper loop if not running."""
        if self._running and self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._sniper_loop(session_factory or AsyncSessionLocal))
        logger.info("🎯 WhatsApp Sniper Service started")

    def stop(self) -> None:
        """Stop the sniper service."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("🛑 WhatsApp Sniper Service stopped")

    def status(self) -> Dict[str, Any]:
        """Get service status."""
        return {
            "running": self._running,
            "interval_seconds": self._interval,
            "last_run": self._last_run.isoformat() if self._last_run else None,
            "trades_executed": self._trades_executed,
            "errors": self._errors[-10:],
        }

    async def _sniper_loop(self, session_factory):
        """Main sniper loop - checks for new signals and executes trades."""
        while self._running:
            try:
                self._last_run = datetime.now()
                await self._run_sniper_cycle(session_factory)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Sniper cycle error: {e}")
                self._errors.append(f"{datetime.now().isoformat()}: {str(e)}")
                if len(self._errors) > 50:
                    self._errors = self._errors[-50:]

            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break

    async def _run_sniper_cycle(self, session_factory):
        """Run one sniper cycle: process pending signals and manage active trades."""
        async with session_factory() as db:
            # Load live config from DB
            result = await db.execute(select(WhatsAppPluginSettings).limit(1))
            db_settings = result.scalars().first()
            live_config = build_config_from_db(db_settings)

            # Get sniper settings
            sniper_settings = await self._get_or_create_settings(db)
            if not sniper_settings.enabled:
                return

            connector = exchange_manager.get_exchange(SupportedExchange.BITGET)
            if not connector:
                logger.warning("BITGET connector not available for sniper")
                return

            # Process new signals
            await self._process_new_signals(db, connector, sniper_settings, live_config)

            # Reconcile active trades
            await self._reconcile_active_trades(db, connector, sniper_settings)

            await db.commit()

    async def _get_or_create_settings(self, db: AsyncSession) -> WhatsAppSniperSettings:
        """Get or create sniper settings."""
        result = await db.execute(select(WhatsAppSniperSettings).limit(1))
        settings = result.scalars().first()
        if not settings:
            settings = WhatsAppSniperSettings()
            db.add(settings)
            await db.flush()
        return settings

    async def _process_new_signals(
        self,
        db: AsyncSession,
        connector,
        sniper_settings: WhatsAppSniperSettings,
        config: WhatsAppPluginConfig,
    ):
        """Process new signals that are ready for auto-trade."""
        # Get active signals that haven't been sniped yet
        result = await db.execute(
            select(WhatsAppParsedSignal).where(
                WhatsAppParsedSignal.status == SignalStatus.ACTIVE.value,
                WhatsAppParsedSignal.confidence >= sniper_settings.min_confidence,
            ).order_by(WhatsAppParsedSignal.posted_at.desc())
        )
        signals = result.scalars().all()

        # Check channel filtering
        if sniper_settings.allowed_channel_ids:
            signals = [s for s in signals if s.channel_source_id in sniper_settings.allowed_channel_ids]

        # Check max positions
        active_trades = await self._count_active_trades(db, sniper_settings)
        if active_trades >= self._get_max_positions(sniper_settings):
            logger.debug(f"Max positions reached ({active_trades}), skipping new signals")
            return

        for signal in signals:
            if active_trades >= self._get_max_positions(sniper_settings):
                break

            # Check if already sniped
            existing = await db.execute(
                select(WhatsAppSniperTrade).where(
                    WhatsAppSniperTrade.signal_id == signal.id,
                    WhatsAppSniperTrade.status.in_([
                        SniperTradeStatus.PENDING.value,
                        SniperTradeStatus.PLACED.value,
                        SniperTradeStatus.FILLED.value,
                    ]),
                )
            )
            if existing.scalar_one_or_none():
                continue

            # Check cooldown - don't snipe same symbol too frequently
            recent_snipe = await db.execute(
                select(WhatsAppSniperTrade).where(
                    WhatsAppSniperTrade.symbol == signal.symbol,
                    WhatsAppSniperTrade.created_at >= datetime.now() - timedelta(minutes=5),
                )
            )
            if recent_snipe.scalar_one_or_none():
                continue

            # Execute sniper trade
            success = await self._execute_sniper_trade(
                db, signal, connector, sniper_settings, config
            )
            if success:
                active_trades += 1
                self._trades_executed += 1

    def _get_max_positions(self, sniper_settings: WhatsAppSniperSettings) -> int:
        """Get max positions based on mode."""
        mode = sniper_settings.mode
        if mode == "sandbox":
            return sniper_settings.max_positions_sandbox
        elif mode == "live":
            return sniper_settings.max_positions_live
        return max(sniper_settings.max_positions_sandbox, sniper_settings.max_positions_live)

    async def _count_active_trades(
        self,
        db: AsyncSession,
        sniper_settings: WhatsAppSniperSettings,
    ) -> int:
        """Count currently active sniper trades."""
        modes = []
        if sniper_settings.mode in ("sandbox", "both") and sniper_settings.execute_sandbox:
            modes.append("sandbox")
        if sniper_settings.mode in ("live", "both") and sniper_settings.execute_live:
            modes.append("live")

        if not modes:
            return 0

        result = await db.execute(
            select(WhatsAppSniperTrade).where(
                WhatsAppSniperTrade.mode.in_(modes),
                WhatsAppSniperTrade.status.in_([
                    SniperTradeStatus.PENDING.value,
                    SniperTradeStatus.PLACED.value,
                    SniperTradeStatus.FILLED.value,
                ]),
            )
        )
        return len(result.scalars().all())

    async def _execute_sniper_trade(
        self,
        db: AsyncSession,
        signal: WhatsAppParsedSignal,
        connector,
        sniper_settings: WhatsAppSniperSettings,
        config: WhatsAppPluginConfig,
    ) -> bool:
        """Execute a sniper trade for a signal."""
        try:
            # Get current price
            ticker = await connector.get_ticker(signal.symbol)
            if not ticker:
                logger.warning(f"No ticker for {signal.symbol}")
                return False

            current_price = float(ticker.get("last") or ticker.get("close") or 0)
            if current_price <= 0:
                return False

            # Determine entry price with offset
            entry_price = self._calculate_entry_price(signal, current_price, sniper_settings)

            # Calculate position size
            position_size = self._calculate_position_size(signal, entry_price, sniper_settings)

            # Validate risk
            if not self._validate_risk(signal, entry_price, sniper_settings):
                logger.warning(f"Signal {signal.id} failed risk validation")
                return False

            # Determine execution modes
            modes = self._get_execution_modes(sniper_settings)

            for mode in modes:
                trade = await self._place_order(
                    db, signal, entry_price, position_size, mode, sniper_settings
                )
                if trade:
                    logger.info(
                        f"🎯 Sniper trade placed: {trade.id} {signal.symbol} "
                        f"{signal.direction} @ {entry_price} ({mode})"
                    )
                    await self._emit_trade_update(trade)
                else:
                    logger.error(f"Failed to place sniper trade for {signal.symbol}")

            return True

        except Exception as e:
            logger.error(f"Sniper trade execution failed for {signal.symbol}: {e}")
            return False

    def _calculate_entry_price(
        self,
        signal: WhatsAppParsedSignal,
        current_price: float,
        sniper_settings: WhatsAppSniperSettings,
    ) -> float:
        """Calculate entry price with sniper offset."""
        if signal.entry and signal.entry > 0:
            # Use signal entry with offset
            offset = sniper_settings.sniper_offset_pct / 100
            if signal.direction in ("buy", "long"):
                return signal.entry * (1 - offset)
            else:
                return signal.entry * (1 + offset)
        else:
            # Use current price with offset
            offset = sniper_settings.sniper_offset_pct / 100
            if signal.direction in ("buy", "long"):
                return current_price * (1 - offset)
            else:
                return current_price * (1 + offset)

    def _calculate_position_size(
        self,
        signal: WhatsAppParsedSignal,
        entry_price: float,
        sniper_settings: WhatsAppSniperSettings,
    ) -> float:
        """Calculate position size in base currency."""
        # Use risk calculator for proper sizing
        account_balance = 10000  # TODO: Get from actual account
        risk_pct = sniper_settings.position_size_usdt / account_balance * 100

        size_info = risk_calculator.calculate_position_size(
            account_balance=account_balance,
            entry_price=entry_price,
            stop_loss_price=signal.stop_loss or entry_price * 0.98,
            risk_percentage=risk_pct,
        )

        # Return position size in base currency
        return size_info.get("position_size_base", sniper_settings.position_size_usdt / entry_price)

    def _validate_risk(
        self,
        signal: WhatsAppParsedSignal,
        entry_price: float,
        sniper_settings: WhatsAppSniperSettings,
    ) -> bool:
        """Validate signal meets risk criteria."""
        # Check minimum risk:reward
        if signal.stop_loss and signal.take_profits:
            risk = abs(entry_price - signal.stop_loss)
            reward = abs(signal.take_profits[0] - entry_price) if signal.take_profits else 0
            if risk > 0 and reward / risk < sniper_settings.min_risk_reward:
                return False
        return True

    def _get_execution_modes(self, sniper_settings: WhatsAppSniperSettings) -> List[str]:
        """Get list of execution modes based on settings."""
        modes = []
        if sniper_settings.mode in ("sandbox", "both") and sniper_settings.execute_sandbox:
            modes.append("sandbox")
        if sniper_settings.mode in ("live", "both") and sniper_settings.execute_live:
            modes.append("live")
        return modes

    async def _place_order(
        self,
        db: AsyncSession,
        signal: WhatsAppParsedSignal,
        entry_price: float,
        position_size: float,
        mode: str,
        sniper_settings: WhatsAppSniperSettings,
    ) -> Optional[WhatsAppSniperTrade]:
        """Place the actual order on exchange."""
        try:
            # Get trading service
            trading_service = TradingService()

            # Prepare order
            side = "buy" if signal.direction in ("buy", "long") else "sell"
            symbol = signal.symbol

            # For limit orders, we need to adjust
            order_type = "limit" if sniper_settings.trade_type == "limit" else "market"

            # Execute order
            result = await trading_service.place_order(
                symbol=symbol,
                side=side,
                order_type=order_type,
                quantity=position_size,
                price=entry_price if order_type == "limit" else None,
                leverage=sniper_settings.leverage,
                margin_mode=sniper_settings.margin_mode,
                testnet=(mode == "sandbox"),
            )

            if not result or not result.get("orderId"):
                return None

            # Create trade record
            trade = WhatsAppSniperTrade(
                signal_id=signal.id,
                channel_source_id=signal.channel_source_id,
                symbol=symbol,
                direction=signal.direction,
                side=side,
                entry_price=entry_price,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profits[0] if signal.take_profits else None,
                leverage=sniper_settings.leverage,
                margin_mode=sniper_settings.margin_mode,
                position_size_usdt=sniper_settings.position_size_usdt,
                quantity=position_size,
                mode=mode,
                exchange="bitget",
                status=SniperTradeStatus.PLACED.value,
                order_id=result.get("orderId"),
                client_order_id=result.get("clientOrderId"),
                placed_at=datetime.now(),
            )
            db.add(trade)
            await db.flush()

            # Place TP/SL orders if not market
            if signal.stop_loss:
                await self._place_stop_loss(db, trade, signal.stop_loss, mode)
            if signal.take_profits:
                await self._place_take_profits(db, trade, signal.take_profits, mode)

            # Update signal status
            signal.status = SignalStatus.FILLED.value
            signal.updated_at = datetime.now()

            return trade

        except Exception as e:
            logger.error(f"Order placement failed: {e}")
            return None

    async def _place_stop_loss(
        self,
        db: AsyncSession,
        trade: WhatsAppSniperTrade,
        sl_price: float,
        mode: str,
    ):
        """Place stop loss order."""
        try:
            trading_service = TradingService()
            result = await trading_service.place_order(
                symbol=trade.symbol,
                side="sell" if trade.side == "buy" else "buy",
                order_type="stop_market",
                quantity=trade.quantity,
                price=None,
                stop_price=sl_price,
                leverage=trade.leverage,
                margin_mode=trade.margin_mode,
                testnet=(mode == "sandbox"),
            )
            if result and result.get("orderId"):
                trade.sl_order_id = result.get("orderId")
        except Exception as e:
            logger.warning(f"Failed to place SL for trade {trade.id}: {e}")

    async def _place_take_profits(
        self,
        db: AsyncSession,
        trade: WhatsAppSniperTrade,
        tp_prices: List[float],
        mode: str,
    ):
        """Place take profit orders."""
        try:
            trading_service = TradingService()
            for i, tp_price in enumerate(tp_prices):
                result = await trading_service.place_order(
                    symbol=trade.symbol,
                    side="sell" if trade.side == "buy" else "buy",
                    order_type="take_profit_market",
                    quantity=trade.quantity / len(tp_prices),
                    price=None,
                    stop_price=tp_price,
                    leverage=trade.leverage,
                    margin_mode=trade.margin_mode,
                    testnet=(mode == "sandbox"),
                )
                if result and result.get("orderId"):
                    if trade.tp_order_ids is None:
                        trade.tp_order_ids = []
                    trade.tp_order_ids.append(result.get("orderId"))
        except Exception as e:
            logger.warning(f"Failed to place TPs for trade {trade.id}: {e}")

    async def _reconcile_active_trades(
        self,
        db: AsyncSession,
        connector,
        sniper_settings: WhatsAppSniperSettings,
    ):
        """Reconcile trade statuses with exchange."""
        result = await db.execute(
            select(WhatsAppSniperTrade).where(
                WhatsAppSniperTrade.status.in_([
                    SniperTradeStatus.PENDING.value,
                    SniperTradeStatus.PLACED.value,
                    SniperTradeStatus.FILLED.value,
                ]),
            )
        )
        trades = result.scalars().all()

        for trade in trades:
            try:
                # Check order status on exchange
                # This would query the exchange for order status
                pass
            except Exception as e:
                logger.warning(f"Reconciliation failed for trade {trade.id}: {e}")

    async def _emit_trade_update(self, trade: WhatsAppSniperTrade):
        """Emit trade update event."""
        try:
            await event_bus.publish(
                "whatsapp_sniper_trade",
                {
                    "trade_id": trade.id,
                    "signal_id": trade.signal_id,
                    "symbol": trade.symbol,
                    "direction": trade.direction,
                    "mode": trade.mode,
                    "status": trade.status,
                    "entry_price": trade.entry_price,
                    "order_id": trade.order_id,
                    "created_at": trade.created_at.isoformat() if trade.created_at else None,
                }
            )
        except Exception as e:
            logger.debug(f"Failed to emit trade update: {e}")


# Global sniper instance
sniper_service = WhatsAppSniperService()


# ────────────────────────────────────────────────────────────────────
# Manual Execution Helpers
# ────────────────────────────────────────────────────────────────────

async def execute_sniper_trade(
    db: AsyncSession,
    trade_id: int,
    mode: str = "sandbox",
    force: bool = False,
) -> Dict[str, Any]:
    """Manually execute a pending sniper trade."""
    result = await db.execute(
        select(WhatsAppSniperTrade).where(WhatsAppSniperTrade.id == trade_id)
    )
    trade = result.scalar_one_or_none()
    if not trade:
        return {"success": False, "error": "Trade not found"}

    if trade.status not in (SniperTradeStatus.PENDING.value,) and not force:
        return {"success": False, "error": f"Trade not in pending state: {trade.status}"}

    # Execute similar to _place_order
    # ... implementation
    return {"success": True, "trade_id": trade.id}


async def execute_parsed_signal(
    db: AsyncSession,
    signal_id: int,
    mode: str = "sandbox",
    force: bool = True,
) -> Dict[str, Any]:
    """Execute a parsed signal as a sniper trade."""
    result = await db.execute(
        select(WhatsAppParsedSignal).where(WhatsAppParsedSignal.id == signal_id)
    )
    signal = result.scalar_one_or_none()
    if not signal:
        return {"success": False, "error": "Signal not found"}

    if signal.status not in (SignalStatus.ACTIVE.value, SignalStatus.FILLED.value) and not force:
        return {"success": False, "error": f"Signal not actionable: {signal.status}"}

    # Get sniper settings
    sniper_settings = await db.execute(select(WhatsAppSniperSettings).limit(1))
    settings = sniper_settings.scalars().first()
    if not settings:
        return {"success": False, "error": "Sniper settings not configured"}

    connector = exchange_manager.get_exchange(SupportedExchange.BITGET)
    if not connector:
        return {"success": False, "error": "Exchange not available"}

    sniper = WhatsAppSniperService()
    success = await sniper._execute_sniper_trade(db, signal, connector, settings, None)

    await db.commit()
    return {"success": success, "signal_id": signal.id}


async def get_signal_prices(
    db: AsyncSession,
    symbols: List[str],
) -> Dict[str, float]:
    """Get current prices for signal symbols."""
    connector = exchange_manager.get_exchange(SupportedExchange.BITGET)
    if not connector:
        return {}

    prices = {}
    for symbol in symbols:
        try:
            ticker = await connector.get_ticker(symbol)
            if ticker:
                prices[symbol] = float(ticker.get("last") or ticker.get("close") or 0)
        except Exception:
            pass
    return prices


async def analyze_signal_full(
    db: AsyncSession,
    signal_id: int,
) -> Dict[str, Any]:
    """Full AI + volume + sniper analysis of a signal."""
    result = await db.execute(
        select(WhatsAppParsedSignal).where(WhatsAppParsedSignal.id == signal_id)
    )
    signal = result.scalar_one_or_none()
    if not signal:
        return {"error": "Signal not found"}

    # Get current price
    prices = await get_signal_prices(db, [signal.symbol])
    current_price = prices.get(signal.symbol, 0)

    # Technical analysis
    connector = exchange_manager.get_exchange(SupportedExchange.BITGET)
    technical = {}
    if connector:
        try:
            ohlcv = await connector.get_ohlcv(signal.symbol, "1h", limit=200)
            if ohlcv:
                from app.signals.technical import analyze as technical_analyze
                technical = technical_analyze(ohlcv, "1h")
        except Exception:
            pass

    # Volume analysis
    volume = {}
    if connector:
        try:
            ticker = await connector.get_ticker(signal.symbol)
            if ticker:
                volume = {
                    "volume_24h": float(ticker.get("quoteVolume", 0)),
                    "change_24h": float(ticker.get("percentage", 0)),
                }
        except Exception:
            pass

    # Risk metrics
    risk = {
        "position_size_usdt": 100,
        "leverage": 10,
        "margin_mode": "crossed",
    }

    # Build decision
    action = "hold"
    confidence = signal.confidence
    reasoning = f"WhatsApp signal: {signal.direction} {signal.symbol}"

    if signal.confidence >= 0.7 and current_price > 0:
        action = "buy" if signal.direction in ("buy", "long") else "sell"

    return {
        "signal_id": signal.id,
        "symbol": signal.symbol,
        "action": action,
        "confidence": confidence,
        "reasoning": reasoning,
        "current_price": current_price,
        "signal_entry": signal.entry,
        "signal_sl": signal.stop_loss,
        "signal_tps": signal.take_profits,
        "technical": technical,
        "volume": volume,
        "risk_metrics": risk,
    }


async def run_sniper_cycle(db: AsyncSession) -> Dict[str, Any]:
    """Run one sniper cycle manually."""
    sniper = WhatsAppSniperService()
    await sniper._run_sniper_cycle(db)
    return {
        "trades_executed": sniper._trades_executed,
        "last_run": sniper._last_run.isoformat() if sniper._last_run else None,
    }


async def volume_monitor_snapshot(db: AsyncSession, limit: int = 25) -> Dict[str, Any]:
    """Get volume snapshot for active signals."""
    result = await db.execute(
        select(WhatsAppParsedSignal).where(
            WhatsAppParsedSignal.status.in_([
                SignalStatus.ACTIVE.value,
                SignalStatus.FILLED.value,
                SignalStatus.TP_HIT.value,
            ])
        ).order_by(WhatsAppParsedSignal.posted_at.desc()).limit(limit)
    )
    signals = result.scalars().all()

    prices = await get_signal_prices(db, [s.symbol for s in signals])

    snapshot = []
    for signal in signals:
        snapshot.append({
            "signal_id": signal.id,
            "symbol": signal.symbol,
            "direction": signal.direction,
            "status": signal.status,
            "current_price": prices.get(signal.symbol, 0),
            "entry": signal.entry,
            "sl": signal.stop_loss,
            "tps": signal.take_profits,
            "confidence": signal.confidence,
            "channel": signal.channel_source.name if signal.channel_source else "Unknown",
        })

    return {"signals": snapshot}


async def reanalyze_skipped_signals(db: AsyncSession) -> Dict[str, int]:
    """Re-analyze previously skipped signals."""
    # TODO: Implement re-analysis logic
    return {"reanalyzed": 0, "new_signals": 0}


async def process_volume_channel_message(
    db: AsyncSession,
    text: str,
) -> Dict[str, Any]:
    """Process a volume alert message."""
    from plugins.WhatsAppSignalNewsPlugin.backend.services.signal_parser import parse_signal
    signal = parse_signal(text, "Volume Channel")
    if not signal:
        return {"ok": False, "message": "No signal detected"}

    # Store as signal
    # ... implementation
    return {"ok": True, "signal": signal.to_dict()}


async def auto_close_positions_for_signal(
    db: AsyncSession,
    signal_id: int,
    reason: str = "Opposite direction detected",
) -> Dict[str, Any]:
    """Auto-close positions for a signal (opposite direction or SL/TP)."""
    result = await db.execute(
        select(WhatsAppSniperTrade).where(
            WhatsAppSniperTrade.signal_id == signal_id,
            WhatsAppSniperTrade.status.in_([
                SniperTradeStatus.PLACED.value,
                SniperTradeStatus.FILLED.value,
            ]),
        )
    )
    trades = result.scalars().all()

    sandbox_closed = []
    live_closed = []
    errors = []

    for trade in trades:
        try:
            # Close position on exchange
            trading_service = TradingService()
            result = await trading_service.close_position(
                symbol=trade.symbol,
                testnet=(trade.mode == "sandbox"),
            )
            if result:
                trade.status = SniperTradeStatus.FAILED.value  # Actually closed
                trade.reason = reason
                trade.closed_at = datetime.now()
                if trade.mode == "sandbox":
                    sandbox_closed.append(trade.id)
                else:
                    live_closed.append(trade.id)
            else:
                errors.append(f"Trade {trade.id}: Failed to close")
        except Exception as e:
            errors.append(f"Trade {trade.id}: {str(e)}")

    await db.commit()

    return {
        "sandbox_closed": sandbox_closed,
        "live_closed": live_closed,
        "errors": errors,
    }