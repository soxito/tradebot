"""
Simulation (Paper Trading) Engine
Manages virtual accounts, simulated orders, positions, and auto-trading from signals.
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional


from app.core.timezone import now_sast

_SAST = timezone(timedelta(hours=2))  # Africa/Johannesburg UTC+2


def _utcnow():
    """Naive SAST datetime for DB compatibility."""
    return now_sast()
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, desc, func, or_

from app.models.database import (
    SimAccount, SimOrder, SimPosition,
    Signal, SignalAction, SignalStatus,
)
from app.core.events import event_bus, Topics
from app.exchanges.manager import exchange_manager, SupportedExchange
from app.exchanges.bitget import BitgetConnector
from app.signals.generator import SignalGenerator
from app.signals.technical import (
    ohlcv_to_dataframe, bollinger_bands, sma,
)
from loguru import logger
import numpy as np

from app.utils.precision import smart_round


# ──────────────────────────────────────────────────────────────
# Smart Stop-Loss Calculator
# ──────────────────────────────────────────────────────────────

class SmartStopLoss:
    """Calculate intelligent stop-loss levels using multiple methods."""

    @staticmethod
    def from_atr(df, side: str, multiplier: float = 1.5) -> Optional[float]:
        """ATR-based stop-loss (adapts to volatility)."""
        if len(df) < 15:
            return None
        tr = np.maximum(
            df["high"] - df["low"],
            np.maximum(
                abs(df["high"] - df["close"].shift(1)),
                abs(df["low"] - df["close"].shift(1)),
            ),
        )
        atr = tr.rolling(14).mean().iloc[-1]
        price = df["close"].iloc[-1]
        if np.isnan(atr):
            return None
        if side == "long":
            return smart_round(price - atr * multiplier, price)
        return smart_round(price + atr * multiplier, price)

    @staticmethod
    def from_bollinger(df, side: str) -> Optional[float]:
        """Bollinger-band based stop-loss."""
        if len(df) < 21:
            return None
        bb = bollinger_bands(df)
        bb_price = float(bb["lower"].iloc[-1]) if side == "long" else float(bb["upper"].iloc[-1])
        ref = float(df["close"].iloc[-1])
        return smart_round(bb_price, ref)

    @staticmethod
    def from_pct(price: float, side: str, pct: float = 0.02) -> float:
        """Fixed-percentage stop-loss (fallback)."""
        if side == "long":
            return smart_round(price * (1 - pct), price)
        return smart_round(price * (1 + pct), price)

    @staticmethod
    def from_support(df, side: str, lookback: int = 20) -> Optional[float]:
        """Recent swing-low/swing-high as support/resistance."""
        if len(df) < lookback:
            return None
        recent = df.iloc[-lookback:]
        ref = float(df["close"].iloc[-1])
        if side == "long":
            return smart_round(float(recent["low"].min()), ref)
        return smart_round(float(recent["high"].max()), ref)

    @classmethod
    def calculate(cls, ohlcv, side: str, price: float) -> Dict[str, Any]:
        """
        Pick the best stop-loss from multiple methods.
        Returns: {stop_loss, take_profit, sl_type, methods}
        """
        df = ohlcv_to_dataframe(ohlcv) if not hasattr(ohlcv, "columns") else ohlcv
        methods: Dict[str, Optional[float]] = {}

        methods["atr"] = cls.from_atr(df, side)
        methods["bb"] = cls.from_bollinger(df, side)
        methods["support"] = cls.from_support(df, side)
        methods["pct"] = cls.from_pct(price, side)

        # Choose: prefer ATR, fall back to BB → support → pct
        chosen_sl = None
        sl_type = "pct"
        for method_name in ["atr", "bb", "support", "pct"]:
            val = methods[method_name]
            if val is not None:
                # Validate: SL must be on the correct side of price
                if side == "long" and val < price:
                    chosen_sl = val
                    sl_type = method_name
                    break
                elif side == "short" and val > price:
                    chosen_sl = val
                    sl_type = method_name
                    break

        if chosen_sl is None:
            chosen_sl = methods["pct"]
            sl_type = "pct"

        # Take-profit at 2:1 reward-to-risk
        risk = abs(price - chosen_sl)
        if side == "long":
            take_profit = smart_round(price + risk * 2, price)
        else:
            take_profit = smart_round(price - risk * 2, price)

        return {
            "stop_loss": chosen_sl,
            "take_profit": take_profit,
            "sl_type": sl_type,
            "methods": {k: v for k, v in methods.items() if v is not None},
        }


# ──────────────────────────────────────────────────────────────
# Simulation Engine
# ──────────────────────────────────────────────────────────────

class SimulationEngine:
    """Core paper-trading engine."""

    @staticmethod
    def _position_margin(pos: SimPosition) -> float:
        notional = (pos.amount or 0) * (pos.entry_price or 0)
        lev = max(1, pos.leverage or 1) if (getattr(pos, "trade_type", "spot") or "spot") == "futures" else 1
        return notional / lev if lev > 0 else notional

    @staticmethod
    def _position_equity_value(pos: SimPosition) -> float:
        trade_type = (getattr(pos, "trade_type", "spot") or "spot")
        unrealized = pos.unrealized_pnl or 0.0
        if trade_type == "futures":
            return SimulationEngine._position_margin(pos) + unrealized
        if pos.side == "long":
            return (pos.current_price or pos.entry_price or 0) * (pos.amount or 0)
        return unrealized

    @staticmethod
    async def _refresh_open_positions(db: AsyncSession, account_id: int) -> List[SimPosition]:
        result = await db.execute(
            select(SimPosition).where(
                SimPosition.account_id == account_id,
                SimPosition.status == "open",
            )
        )
        positions = result.scalars().all()
        from app.services import market_data

        for pos in positions:
            try:
                # Routed by asset class: a gold or FX position must not be
                # priced off a crypto exchange that has never listed it.
                current_price = await market_data.live_price(pos.symbol, db=db)
                if not current_price:
                    continue
                pos.current_price = current_price
                pos.unrealized_pnl = SimulationEngine._calc_pnl(pos, current_price)
            except Exception as e:
                logger.warning(f"Failed to refresh price for {pos.symbol}: {e}")
                continue

        if positions:
            await db.commit()
        return positions

    @staticmethod
    def _account_metrics(account: SimAccount, positions: List[SimPosition]) -> Dict[str, Any]:
        unrealized_pnl = sum(pos.unrealized_pnl or 0.0 for pos in positions)
        reserved_margin = sum(
            SimulationEngine._position_margin(pos)
            for pos in positions
            if (getattr(pos, "trade_type", "spot") or "spot") == "futures"
        )
        equity = account.balance + sum(SimulationEngine._position_equity_value(pos) for pos in positions)
        return {
            "balance": round(account.balance, 2),
            "equity": round(equity, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "reserved_margin": round(reserved_margin, 2),
            "open_positions_count": len(positions),
        }

    @staticmethod
    async def get_account_snapshot(db: AsyncSession, account: Optional[SimAccount] = None) -> Dict[str, Any]:
        account = account or await SimulationEngine.get_or_create_account(db)
        positions = await SimulationEngine._refresh_open_positions(db, account.id)
        return SimulationEngine.account_to_dict(account, SimulationEngine._account_metrics(account, positions))

    # ── Account Management ──────────────────────────────────

    @staticmethod
    async def get_or_create_account(db: AsyncSession) -> SimAccount:
        result = await db.execute(select(SimAccount).limit(1))
        account = result.scalar_one_or_none()
        if not account:
            account = SimAccount(
                name="Paper Trading",
                balance=10000.0,
                initial_balance=10000.0,
                is_active=False,
            )
            db.add(account)
            await db.commit()
            await db.refresh(account)
        return account

    @staticmethod
    async def get_account(db: AsyncSession) -> Optional[SimAccount]:
        result = await db.execute(select(SimAccount).limit(1))
        return result.scalar_one_or_none()

    @staticmethod
    async def toggle_active(db: AsyncSession, active: bool) -> SimAccount:
        account = await SimulationEngine.get_or_create_account(db)
        account.is_active = active
        await db.commit()
        await db.refresh(account)
        return account

    @staticmethod
    async def add_funds(db: AsyncSession, amount: float) -> SimAccount:
        account = await SimulationEngine.get_or_create_account(db)
        account.balance += amount
        account.initial_balance += amount
        await db.commit()
        await db.refresh(account)
        return account

    @staticmethod
    async def reset_account(db: AsyncSession, starting_balance: float = 10000.0) -> SimAccount:
        account = await SimulationEngine.get_or_create_account(db)
        # Close all open positions
        await db.execute(
            update(SimPosition)
            .where(SimPosition.account_id == account.id, SimPosition.status == "open")
            .values(status="closed", closed_at=_utcnow(), realized_pnl=0)
        )
        account.balance = starting_balance
        account.initial_balance = starting_balance
        account.total_pnl = 0
        account.total_trades = 0
        account.winning_trades = 0
        account.losing_trades = 0
        await db.commit()
        await db.refresh(account)
        return account

    # Valid settings ranges
    VALID_TIMEFRAMES = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M"}
    VALID_MODES = {"spot", "futures"}
    VALID_MARGIN_MODES = {"crossed", "isolated"}
    VALID_AMOUNT_MODES = {"quote", "base"}
    VALID_AI_PROVIDERS = {"orchestrator", "tradingagents"}

    @staticmethod
    async def update_settings(
        db: AsyncSession,
        auto_trade: Optional[bool] = None,
        auto_trade_pairs: Optional[List[str]] = None,
        auto_trade_timeframe: Optional[str] = None,
        auto_trade_max_positions: Optional[int] = None,
        auto_trade_risk_pct: Optional[float] = None,
        auto_trade_mode: Optional[str] = None,
        auto_trade_leverage: Optional[int] = None,
        auto_trade_margin_mode: Optional[str] = None,
        auto_trade_amount_mode: Optional[str] = None,
        auto_trade_pine_script_id: Optional[int] = None,
        enable_ai: Optional[bool] = None,
        auto_trade_ai_provider: Optional[str] = None,
        tradingagents_llm_provider: Optional[str] = None,
        tradingagents_deep_think_llm: Optional[str] = None,
        tradingagents_quick_think_llm: Optional[str] = None,
        tradingagents_backend_url: Optional[str] = None,
        tradingagents_max_debate_rounds: Optional[int] = None,
        tradingagents_max_risk_discuss_rounds: Optional[int] = None,
        margin_size_usdt: Optional[float] = None,
        min_entry_gap_pct: Optional[float] = None,
        min_pump_pct: Optional[float] = None,
        min_confidence: Optional[float] = None,
        sniper_max_entries: Optional[int] = None,
    ) -> SimAccount:
        account = await SimulationEngine.get_or_create_account(db)
        if enable_ai is not None:
            account.enable_ai = enable_ai
        if auto_trade_ai_provider is not None:
            provider = str(auto_trade_ai_provider).strip().lower()
            if provider in SimulationEngine.VALID_AI_PROVIDERS:
                account.auto_trade_ai_provider = provider
            else:
                logger.warning(f"Invalid AI provider '{auto_trade_ai_provider}', keeping current")
        if tradingagents_llm_provider is not None:
            text_value = str(tradingagents_llm_provider).strip()
            if text_value:
                account.tradingagents_llm_provider = text_value
        if tradingagents_deep_think_llm is not None:
            text_value = str(tradingagents_deep_think_llm).strip()
            if text_value:
                account.tradingagents_deep_think_llm = text_value
        if tradingagents_quick_think_llm is not None:
            text_value = str(tradingagents_quick_think_llm).strip()
            if text_value:
                account.tradingagents_quick_think_llm = text_value
        if tradingagents_backend_url is not None:
            text_value = str(tradingagents_backend_url).strip()
            account.tradingagents_backend_url = text_value or None
        if tradingagents_max_debate_rounds is not None:
            account.tradingagents_max_debate_rounds = max(1, min(6, int(tradingagents_max_debate_rounds)))
        if tradingagents_max_risk_discuss_rounds is not None:
            account.tradingagents_max_risk_discuss_rounds = max(1, min(6, int(tradingagents_max_risk_discuss_rounds)))
        if auto_trade is not None:
            account.auto_trade = auto_trade
        if auto_trade_pairs is not None:
            account.auto_trade_pairs = json.dumps(auto_trade_pairs)
        if auto_trade_timeframe is not None:
            if auto_trade_timeframe in SimulationEngine.VALID_TIMEFRAMES:
                account.auto_trade_timeframe = auto_trade_timeframe
            else:
                logger.warning(f"Invalid timeframe '{auto_trade_timeframe}', keeping current")
        if auto_trade_max_positions is not None:
            account.auto_trade_max_positions = max(1, min(100, int(auto_trade_max_positions)))
        if auto_trade_risk_pct is not None:
            account.auto_trade_risk_pct = max(0.1, min(25.0, float(auto_trade_risk_pct)))
        if auto_trade_mode is not None:
            if auto_trade_mode in SimulationEngine.VALID_MODES:
                account.auto_trade_mode = auto_trade_mode
            else:
                logger.warning(f"Invalid trade mode '{auto_trade_mode}', keeping current")
        if auto_trade_leverage is not None:
            account.auto_trade_leverage = max(1, min(200, int(auto_trade_leverage)))
        if auto_trade_margin_mode is not None:
            if auto_trade_margin_mode in SimulationEngine.VALID_MARGIN_MODES:
                account.auto_trade_margin_mode = auto_trade_margin_mode
            else:
                logger.warning(f"Invalid margin mode '{auto_trade_margin_mode}', keeping current")
        if auto_trade_amount_mode is not None:
            if auto_trade_amount_mode in SimulationEngine.VALID_AMOUNT_MODES:
                account.auto_trade_amount_mode = auto_trade_amount_mode
            else:
                logger.warning(f"Invalid amount mode '{auto_trade_amount_mode}', keeping current")
        if auto_trade_pine_script_id is not None:
            # Allow clearing with 0 or -1
            account.auto_trade_pine_script_id = auto_trade_pine_script_id if auto_trade_pine_script_id > 0 else None
        if margin_size_usdt is not None:
            account.margin_size_usdt = max(1.0, float(margin_size_usdt))
        if min_entry_gap_pct is not None:
            account.min_entry_gap_pct = max(0.5, min(20.0, float(min_entry_gap_pct)))
        if min_pump_pct is not None:
            account.min_pump_pct = max(1.0, min(500.0, float(min_pump_pct)))
        if min_confidence is not None:
            account.min_confidence = max(0.50, min(1.0, float(min_confidence)))
        if sniper_max_entries is not None:
            account.sniper_max_entries = max(1, min(10, int(sniper_max_entries)))
        await db.commit()
        await db.refresh(account)
        return account

    # ── Order Placement ─────────────────────────────────────

    @staticmethod
    async def place_order(
        db: AsyncSession,
        symbol: str,
        side: str,  # buy / sell
        amount: float,
        price: float,
        order_type: str = "market",
        signal_id: Optional[int] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        sl_type: Optional[str] = None,
        trade_type: str = "spot",
        margin_mode: Optional[str] = None,
        leverage: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Place a simulated order. Deducts/adds to balance, creates position."""
        account = await SimulationEngine.get_or_create_account(db)
        if not account.is_active:
            return {"success": False, "error": "Simulation account is not active"}

        notional = amount * price
        # For futures, only the margin is reserved; for spot, the full cost
        effective_leverage = max(1, leverage or 1) if trade_type == "futures" else 1
        margin_cost = notional / effective_leverage

        # Buy = deducting from balance
        if side == "buy" and margin_cost > account.balance:
            return {"success": False, "error": f"Insufficient balance: ${account.balance:.2f} < margin ${margin_cost:.2f}"}

        # Spot mode: selling requires an existing long position to close
        # (no naked short-selling in spot markets)
        if trade_type == "spot" and side == "sell":
            existing_long = await db.execute(
                select(SimPosition).where(
                    SimPosition.account_id == account.id,
                    SimPosition.symbol == symbol,
                    SimPosition.side == "long",
                    SimPosition.status == "open",
                )
            )
            if not existing_long.scalars().first():
                return {"success": False, "error": "Cannot short-sell in spot mode — no long position to close"}

        # Create order
        order = SimOrder(
            account_id=account.id,
            signal_id=signal_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            amount=amount,
            price=price,
            cost=notional,
            stop_loss=stop_loss,
            take_profit=take_profit,
            sl_type=sl_type,
            trade_type=trade_type,
            margin_mode=margin_mode,
            leverage=leverage,
            status="filled",
        )
        db.add(order)

        # Check for existing opposite position (close it)
        result = await db.execute(
            select(SimPosition).where(
                SimPosition.account_id == account.id,
                SimPosition.symbol == symbol,
                SimPosition.status == "open",
            )
        )
        existing = result.scalars().first()

        if existing and (
            (existing.side == "long" and side == "sell")
            or (existing.side == "short" and side == "buy")
        ):
            # Close existing position
            pnl = SimulationEngine._calc_pnl(existing, price)
            existing.status = "closed"
            existing.closed_at = _utcnow()
            existing.realized_pnl = pnl
            existing.current_price = price
            # Return the original margin + pnl
            pos_notional = existing.amount * existing.entry_price
            pos_leverage = max(1, existing.leverage or 1) if (getattr(existing, 'trade_type', 'spot') or 'spot') == 'futures' else 1
            pos_margin = pos_notional / pos_leverage
            account.balance += pos_margin + pnl
            account.total_trades += 1
            account.total_pnl += pnl
            if pnl >= 0:
                account.winning_trades += 1
            else:
                account.losing_trades += 1

            await db.commit()
            await db.refresh(account)
            await db.refresh(order)
            event_bus.emit(Topics.TRADE_UPDATE, {
                "event": "closed_position",
                "mode": "sim",
                "order_id": order.id,
                "symbol": symbol,
                "side": side,
                "pnl": round(pnl, 2),
                "balance": round(account.balance, 2),
            })
            return {
                "success": True,
                "action": "closed_position",
                "order_id": order.id,
                "pnl": round(pnl, 2),
                "balance": round(account.balance, 2),
            }

        # Open new position
        pos_side = "long" if side == "buy" else "short"
        # For futures: deduct margin (notional/leverage); for spot: deduct full cost
        if side == "buy":
            account.balance -= margin_cost
        elif trade_type == "futures":
            # Short futures also requires margin
            account.balance -= margin_cost

        position = SimPosition(
            account_id=account.id,
            order_id=order.id,
            signal_id=signal_id,
            symbol=symbol,
            side=pos_side,
            amount=amount,
            entry_price=price,
            current_price=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            trade_type=trade_type,
            margin_mode=margin_mode,
            leverage=leverage,
            status="open",
        )
        db.add(position)
        await db.commit()
        await db.refresh(account)
        await db.refresh(order)
        await db.refresh(position)

        logger.info(
            f"📝 SIM order: {side.upper()} {amount} {symbol} @ ${price:.2f} "
            f"| SL: {stop_loss} | TP: {take_profit} | Balance: ${account.balance:.2f}"
        )

        event_bus.emit(Topics.TRADE_UPDATE, {
            "event": "opened_position",
            "mode": "sim",
            "order_id": order.id,
            "position_id": position.id,
            "symbol": symbol,
            "side": pos_side,
            "amount": amount,
            "entry_price": price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "balance": round(account.balance, 2),
        })

        return {
            "success": True,
            "action": "opened_position",
            "order_id": order.id,
            "position_id": position.id,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "sl_type": sl_type,
            "balance": round(account.balance, 2),
        }

    # ── Position Management ─────────────────────────────────

    @staticmethod
    async def get_positions(db: AsyncSession, status: str = "open") -> List[Dict]:
        account = await SimulationEngine.get_or_create_account(db)
        query = select(SimPosition).where(SimPosition.account_id == account.id)
        if status != "all":
            query = query.where(SimPosition.status == status)
        query = query.order_by(desc(SimPosition.created_at))
        result = await db.execute(query)
        positions = result.scalars().all()

        # For open positions, fetch live prices and update PnL
        if status in ("open", "all"):
            await SimulationEngine._refresh_open_positions(db, account.id)

        return [SimulationEngine._pos_to_dict(p) for p in positions]

    @staticmethod
    async def get_orders(db: AsyncSession, limit: int = 50) -> List[Dict]:
        account = await SimulationEngine.get_or_create_account(db)
        result = await db.execute(
            select(SimOrder)
            .where(SimOrder.account_id == account.id)
            .order_by(desc(SimOrder.created_at))
            .limit(limit)
        )
        orders = result.scalars().all()
        return [
            {
                "id": o.id,
                "symbol": o.symbol,
                "side": o.side,
                "order_type": o.order_type,
                "amount": o.amount,
                "price": o.price,
                "cost": o.cost,
                "stop_loss": o.stop_loss,
                "take_profit": o.take_profit,
                "sl_type": o.sl_type,
                "trade_type": getattr(o, "trade_type", "spot") or "spot",
                "margin_mode": getattr(o, "margin_mode", None),
                "leverage": getattr(o, "leverage", None),
                "signal_id": o.signal_id,
                "status": o.status,
                "created_at": o.created_at.isoformat(),
            }
            for o in orders
        ]

    @staticmethod
    async def cancel_order(db: AsyncSession, order_id: int) -> Dict[str, Any]:
        """Cancel a sim order and close its associated position, refunding margin."""
        account = await SimulationEngine.get_or_create_account(db)

        result = await db.execute(
            select(SimOrder).where(
                SimOrder.id == order_id,
                SimOrder.account_id == account.id,
            )
        )
        order = result.scalar_one_or_none()
        if not order:
            return {"success": False, "error": "Order not found"}
        if order.status == "canceled":
            return {"success": False, "error": "Order already canceled"}

        order.status = "canceled"

        # Close the associated open position and refund margin
        pos_result = await db.execute(
            select(SimPosition).where(
                SimPosition.order_id == order_id,
                SimPosition.status == "open",
            )
        )
        position = pos_result.scalar_one_or_none()
        refunded = 0.0
        if position:
            # Fetch current price for PnL calculation
            try:
                from app.services import market_data

                current_price = (
                    await market_data.live_price(position.symbol, db=db)
                    or position.entry_price
                )
            except Exception:
                current_price = position.entry_price

            pnl = SimulationEngine._calc_pnl(position, current_price)
            position.status = "closed"
            position.closed_at = _utcnow()
            position.realized_pnl = pnl
            position.current_price = current_price

            # Refund reserved capital + PnL
            margin = SimulationEngine._position_margin(position)
            refunded = margin + pnl
            account.balance += refunded
            account.total_trades += 1
            account.total_pnl += pnl
            if pnl >= 0:
                account.winning_trades += 1
            else:
                account.losing_trades += 1

        await db.commit()
        await db.refresh(account)
        return {
            "success": True,
            "order_id": order_id,
            "refunded": round(refunded, 2),
            "balance": round(account.balance, 2),
        }

    @staticmethod
    async def close_position(db: AsyncSession, position_id: int) -> Dict[str, Any]:
        """Close an open sim position at current market price, refund margin ± PnL."""
        account = await SimulationEngine.get_or_create_account(db)

        result = await db.execute(
            select(SimPosition).where(
                SimPosition.id == position_id,
                SimPosition.account_id == account.id,
                SimPosition.status == "open",
            )
        )
        position = result.scalar_one_or_none()
        if not position:
            return {"success": False, "error": "Open position not found"}

        # Fetch current price
        try:
            from app.services import market_data

            current_price = (
                await market_data.live_price(position.symbol, db=db)
                or position.entry_price
            )
        except Exception:
            current_price = position.entry_price

        pnl = SimulationEngine._calc_pnl(position, current_price)
        position.status = "closed"
        position.closed_at = _utcnow()
        position.realized_pnl = pnl
        position.current_price = current_price

        # Refund reserved capital + PnL
        margin = SimulationEngine._position_margin(position)
        refunded = margin + pnl
        account.balance += refunded
        account.total_trades += 1
        account.total_pnl += pnl
        if pnl >= 0:
            account.winning_trades += 1
        else:
            account.losing_trades += 1

        # Also mark associated order as canceled
        if position.order_id:
            order_result = await db.execute(
                select(SimOrder).where(SimOrder.id == position.order_id)
            )
            order = order_result.scalar_one_or_none()
            if order and order.status != "canceled":
                order.status = "canceled"

        await db.commit()
        await db.refresh(account)
        event_bus.emit(Topics.TRADE_UPDATE, {
            "event": "closed_position",
            "mode": "sim",
            "position_id": position_id,
            "symbol": position.symbol,
            "side": position.side,
            "pnl": round(pnl, 2),
            "refunded": round(refunded, 2),
            "balance": round(account.balance, 2),
        })
        return {
            "success": True,
            "position_id": position_id,
            "symbol": position.symbol,
            "side": position.side,
            "pnl": round(pnl, 2),
            "refunded": round(refunded, 2),
            "balance": round(account.balance, 2),
        }

    @staticmethod
    async def close_all_positions(db: AsyncSession) -> Dict[str, Any]:
        """Close all open sim positions at current market prices."""
        account = await SimulationEngine.get_or_create_account(db)
        result = await db.execute(
            select(SimPosition).where(
                SimPosition.account_id == account.id,
                SimPosition.status == "open",
            )
        )
        positions = result.scalars().all()
        if not positions:
            return {"success": True, "closed": 0, "results": []}

        results = []
        for pos in positions:
            try:
                from app.services import market_data

                current_price = (
                    await market_data.live_price(pos.symbol, db=db) or pos.entry_price
                )
            except Exception:
                current_price = pos.entry_price

            pnl = SimulationEngine._calc_pnl(pos, current_price)
            pos.status = "closed"
            pos.closed_at = _utcnow()
            pos.realized_pnl = pnl
            pos.current_price = current_price

            margin = SimulationEngine._position_margin(pos)
            refunded = margin + pnl
            account.balance += refunded
            account.total_trades += 1
            account.total_pnl += pnl
            if pnl >= 0:
                account.winning_trades += 1
            else:
                account.losing_trades += 1

            results.append({
                "position_id": pos.id,
                "symbol": pos.symbol,
                "side": pos.side,
                "pnl": round(pnl, 2),
                "refunded": round(refunded, 2),
            })

        await db.commit()
        await db.refresh(account)
        return {
            "success": True,
            "closed": len(results),
            "results": results,
            "balance": round(account.balance, 2),
        }

    @staticmethod
    async def check_stop_loss_take_profit(db: AsyncSession) -> List[Dict]:
        """Check all open positions against current prices. Close those that hit SL/TP."""
        account = await SimulationEngine.get_or_create_account(db)
        result = await db.execute(
            select(SimPosition).where(
                SimPosition.account_id == account.id,
                SimPosition.status == "open",
            )
        )
        positions = result.scalars().all()
        closed = []

        for pos in positions:
            # Priced by asset class: crypto off the exchange, everything else
            # off the live MT5 account first. Asking the exchange for XAUUSD or
            # EURUSD only produces an ERROR log line before the fallback runs.
            current_price = None
            try:
                from app.services import market_data

                current_price = await market_data.live_price(pos.symbol, db=db)
            except Exception:
                pass

            if not current_price:
                # Try the plugin sniper price service (supports forex)
                try:
                    from plugins.TelegramSignalNewsPlugin.backend.services.sniper_service import _get_live_price
                    current_price = await _get_live_price(pos.symbol)
                except Exception:
                    pass

            if not current_price:
                continue

            pos.current_price = current_price
            hit = None

            if pos.side == "long":
                pos.unrealized_pnl = (current_price - pos.entry_price) * pos.amount
                if pos.stop_loss and current_price <= pos.stop_loss:
                    hit = "stop_loss"
                elif pos.take_profit and current_price >= pos.take_profit:
                    hit = "take_profit"
            else:  # short
                pos.unrealized_pnl = (pos.entry_price - current_price) * pos.amount
                if pos.stop_loss and current_price >= pos.stop_loss:
                    hit = "stop_loss"
                elif pos.take_profit and current_price <= pos.take_profit:
                    hit = "take_profit"

            if hit:
                pnl = SimulationEngine._calc_pnl(pos, current_price)
                pos.status = "closed"
                pos.closed_at = _utcnow()
                pos.realized_pnl = pnl
                refunded = SimulationEngine._position_margin(pos) + pnl
                account.balance += refunded
                account.total_trades += 1
                account.total_pnl += pnl
                if pnl >= 0:
                    account.winning_trades += 1
                else:
                    account.losing_trades += 1
                closed.append({
                    "position_id": pos.id,
                    "symbol": pos.symbol,
                    "side": pos.side,
                    "reason": hit,
                    "pnl": round(pnl, 2),
                    "refunded": round(refunded, 2),
                    "entry": pos.entry_price,
                    "exit": current_price,
                })
                logger.info(f"🎯 SIM {hit}: {pos.symbol} {pos.side} PnL=${pnl:.2f}")

        await db.commit()
        for c in closed:
            event_bus.emit(Topics.TRADE_UPDATE, {"event": "sl_tp_hit", "mode": "sim", **c})
        return closed

    # ── Backfill SL/TP for existing positions ───────────────

    @staticmethod
    async def backfill_sl_tp(db: AsyncSession) -> List[Dict[str, Any]]:
        """
        Find open sim positions that are missing stop_loss or take_profit
        and calculate smart SL/TP values for them (DB-only, no exchange calls).
        """
        account = await SimulationEngine.get_or_create_account(db)

        result = await db.execute(
            select(SimPosition).where(
                SimPosition.account_id == account.id,
                SimPosition.status == "open",
                or_(
                    SimPosition.stop_loss.is_(None),
                    SimPosition.take_profit.is_(None),
                ),
            )
        )
        positions = result.scalars().all()

        if not positions:
            return []

        backfilled: List[Dict[str, Any]] = []
        effective_timeframe = getattr(account, "auto_trade_timeframe", None) or "1h"

        for pos in positions:
            try:
                connector = exchange_manager.get_exchange(SupportedExchange.BITGET)
                if not connector:
                    logger.warning(f"[SIM-BACKFILL] No connector for OHLCV fetch, skipping {pos.symbol}")
                    continue

                ohlcv = await connector.get_ohlcv(
                    symbol=pos.symbol,
                    timeframe=effective_timeframe,
                    limit=200,
                )
                side = pos.side  # "long" or "short"
                entry = pos.entry_price or 0
                if entry <= 0:
                    continue

                sl_data = SmartStopLoss.calculate(ohlcv, side, entry)

                updated_fields = {}
                if pos.stop_loss is None and sl_data.get("stop_loss"):
                    pos.stop_loss = sl_data["stop_loss"]
                    updated_fields["stop_loss"] = sl_data["stop_loss"]
                if pos.take_profit is None and sl_data.get("take_profit"):
                    pos.take_profit = sl_data["take_profit"]
                    updated_fields["take_profit"] = sl_data["take_profit"]

                if updated_fields:
                    backfilled.append({
                        "symbol": pos.symbol,
                        "side": side,
                        "entry_price": entry,
                        "sl_type": sl_data.get("sl_type", "pct"),
                        **updated_fields,
                    })
                    logger.info(
                        f"[SIM-BACKFILL] {pos.symbol} {side}: "
                        f"SL={updated_fields.get('stop_loss')}, TP={updated_fields.get('take_profit')} "
                        f"(method={sl_data.get('sl_type')})"
                    )
            except Exception as e:
                logger.error(f"[SIM-BACKFILL] Error backfilling {pos.symbol}: {e}")

        if backfilled:
            await db.commit()
            logger.info(f"[SIM-BACKFILL] Backfilled SL/TP for {len(backfilled)} positions")

        return backfilled

    # ── Auto-Trade from Signals ─────────────────────────────

    @staticmethod
    async def auto_trade_cycle(db: AsyncSession) -> Dict[str, Any]:
        """
        Run one auto-trade cycle:
        1. Check SL/TP on open positions
        2. Pick up recent unprocessed signals from the DB
        3. Place orders for actionable signals (BUY/SELL)
        """
        account = await SimulationEngine.get_or_create_account(db)
        if not account.is_active or not account.auto_trade:
            return {"skipped": True, "reason": "Auto-trade not active"}

        effective_timeframe = getattr(account, "auto_trade_timeframe", None) or "1h"
        max_positions = max(1, int(getattr(account, "auto_trade_max_positions", 5) or 5))

        pairs = json.loads(account.auto_trade_pairs or "[]")
        if not pairs:
            return {"skipped": True, "reason": "No pairs configured"}

        # Per-cycle leverage cache to reduce exchange API calls
        _leverage_cache: Dict[str, int] = {}

        # 1. Check SL/TP
        closed = await SimulationEngine.check_stop_loss_take_profit(db)

        # 1b. Backfill missing SL/TP on open positions
        backfilled = await SimulationEngine.backfill_sl_tp(db)

        # 2. Count open positions
        result = await db.execute(
            select(func.count(SimPosition.id)).where(
                SimPosition.account_id == account.id,
                SimPosition.status == "open",
            )
        )
        open_count = result.scalar() or 0

        # 3. Pick up recent actionable signals from the DB instead of re-running
        #    the pipeline (which would hit the 15-min cooldown and produce no
        #    actionable signals). Each engine tracks its own executions via
        #    SimOrder/Trade rows so live and sim can consume the same signal.
        signal_window = _utcnow() - timedelta(minutes=20)
        sig_query = (
            select(Signal)
            .where(
                Signal.symbol.in_(pairs),
                Signal.action.in_([SignalAction.BUY, SignalAction.SELL]),
                Signal.status == SignalStatus.PENDING,
                Signal.created_at >= signal_window,
            )
            .order_by(Signal.created_at.desc())
        )
        sig_rows = (await db.execute(sig_query)).scalars().all()

        # Deduplicate: keep only the most recent signal per symbol
        seen_symbols: set = set()
        unique_signals: list = []
        for sig in sig_rows:
            if sig.symbol not in seen_symbols:
                seen_symbols.add(sig.symbol)
                unique_signals.append(sig)

        signal_ids = [sig.id for sig in unique_signals]
        consumed_signal_ids = set()
        if signal_ids:
            consumed_rows = await db.execute(
                select(SimOrder.signal_id).where(
                    SimOrder.account_id == account.id,
                    SimOrder.signal_id.in_(signal_ids),
                )
            )
            consumed_signal_ids = {
                signal_id for signal_id in consumed_rows.scalars().all() if signal_id is not None
            }

        if not unique_signals:
            return {
                "skipped": False,
                "sl_tp_closed": closed,
                "sl_tp_backfilled": backfilled,
                "orders_placed": [],
                "signals_analyzed": 0,
                "balance": round(account.balance, 2),
                "timeframe": effective_timeframe,
                "max_positions": max_positions,
                "reason": "No pending signals",
            }

        orders_placed = []
        signals_processed = 0
        for sig in unique_signals:
            if sig.id in consumed_signal_ids:
                continue

            signals_processed += 1
            symbol = sig.symbol
            action = sig.action.value  # "buy" or "sell"
            price = sig.price or 0
            confidence = sig.confidence or 0

            # Parse stored indicators for smart limit pricing
            try:
                indicators = json.loads(sig.indicators) if sig.indicators else {}
            except (json.JSONDecodeError, TypeError):
                indicators = {}

            # Skip low-confidence signals
            min_conf = getattr(account, "min_confidence", 0.90) or 0.90
            if confidence < min_conf:
                logger.info(f"[SIM] Skipping {symbol} {action}: low confidence {confidence:.2f} < {min_conf}")
                sig.status = SignalStatus.IGNORED
                continue

            # Check if we already have a position for this symbol
            existing = await db.execute(
                select(SimPosition).where(
                    SimPosition.account_id == account.id,
                    SimPosition.symbol == symbol,
                    SimPosition.status == "open",
                )
            )
            existing_pos = existing.scalars().first()

            side = action  # already "buy" or "sell"

            # DCA: allow up to 3 additional entries on existing same-direction position
            is_dca = False
            if existing_pos:
                same_direction = (
                    (existing_pos.side == "long" and side == "buy")
                    or (existing_pos.side == "short" and side == "sell")
                )
                if same_direction:
                    dca_result = await db.execute(
                        select(func.count(SimPosition.id)).where(
                            SimPosition.account_id == account.id,
                            SimPosition.symbol == symbol,
                            SimPosition.side == existing_pos.side,
                            SimPosition.status == "open",
                        )
                    )
                    dca_count = dca_result.scalar() or 0
                    if dca_count >= 4:  # 1 original + 3 DCA
                        logger.info(
                            f"[SIM] Max DCA entries reached ({dca_count}/4) for {symbol}; skipping"
                        )
                        sig.status = SignalStatus.IGNORED
                        continue
                    is_dca = True
                    logger.info(
                        f"[SIM] DCA entry {dca_count + 1}/4 for {symbol} {existing_pos.side}"
                    )

                    # DCA price gap check — ensure price has moved enough since last entry
                    min_entry_gap = getattr(account, 'min_entry_gap_pct', 2.0) or 2.0
                    if existing_pos.entry_price and price > 0:
                        gap_pct = abs(price - existing_pos.entry_price) / existing_pos.entry_price * 100
                        if gap_pct < min_entry_gap:
                            logger.info(
                                f"[SIM] DCA gap too small for {symbol}: "
                                f"{gap_pct:.2f}% < {min_entry_gap:.1f}% min; skipping"
                            )
                            sig.status = SignalStatus.IGNORED
                            continue

            # Enforce max positions for any signal that would open a brand new position.
            if not existing_pos and not is_dca and open_count >= max_positions:
                logger.info(
                    f"[SIM] Max positions reached ({open_count}/{max_positions}); skipping {symbol} {action}"
                )
                sig.status = SignalStatus.IGNORED
                continue

            # Calculate position size using fixed margin_size_usdt
            # margin_size = the exact margin we allocate per trade
            margin_size = getattr(account, 'margin_size_usdt', 10.0) or 10.0
            risk_amount = min(margin_size, account.balance)

            # For futures: use the configured leverage capped by the pair max.
            # Cache pair limits within this cycle to avoid repeated exchange calls.
            trade_type_pre = getattr(account, "auto_trade_mode", "spot") or "spot"
            pre_leverage = 1
            if trade_type_pre == "futures":
                configured_leverage = max(1, int(getattr(account, "auto_trade_leverage", 10) or 10))
                if symbol not in _leverage_cache:
                    try:
                        connector = exchange_manager.get_exchange(SupportedExchange.BITGET)
                        if connector:
                            _, pair_max_lever = await connector.get_max_leverage(symbol)
                            if isinstance(pair_max_lever, (int, float)) and 1 <= pair_max_lever <= 200:
                                _leverage_cache[symbol] = min(configured_leverage, int(pair_max_lever))
                            else:
                                _leverage_cache[symbol] = configured_leverage
                        else:
                            _leverage_cache[symbol] = configured_leverage
                    except Exception:
                        _leverage_cache[symbol] = configured_leverage
                pre_leverage = _leverage_cache[symbol]

            amount = (risk_amount * pre_leverage) / price if price > 0 else 0
            if amount <= 0 or risk_amount <= 0:
                continue

            # Smart limit entry — place slightly better than market for fills
            limit_price = SimulationEngine._smart_limit_price(
                price, side, indicators,
            )

            # ── Determine limit vs market order ──
            # Fetch current market price to decide order type
            current_market_price = price  # fallback to signal price
            try:
                from app.services import market_data

                mkt = await market_data.live_price(symbol, db=db)
                if mkt and mkt > 0:
                    current_market_price = mkt
            except Exception:
                pass

            # BUY: entry below market = favorable → limit; entry at/above market → market
            # SELL: entry above market = favorable → limit; entry at/below market → market
            use_limit = False
            if current_market_price > 0 and limit_price > 0:
                if side == "buy" and limit_price < current_market_price * 0.999:
                    use_limit = True
                elif side == "sell" and limit_price > current_market_price * 1.001:
                    use_limit = True

            sim_order_type = "limit" if use_limit else "market"
            sim_order_price = limit_price if use_limit else current_market_price

            # Smart stop-loss
            try:
                connector = exchange_manager.get_exchange(SupportedExchange.BITGET)
                ohlcv = await connector.get_ohlcv(
                    symbol=symbol,
                    timeframe=effective_timeframe,
                    limit=200,
                )
                sl_data = SmartStopLoss.calculate(
                    ohlcv,
                    "long" if side == "buy" else "short",
                    limit_price,
                )
            except Exception:
                sl_data = {
                    "stop_loss": SmartStopLoss.from_pct(limit_price, "long" if side == "buy" else "short"),
                    "take_profit": limit_price * (1.04 if side == "buy" else 0.96),
                    "sl_type": "pct",
                }

            # Determine trade type from account settings
            trade_type = getattr(account, "auto_trade_mode", "spot") or "spot"
            margin_mode = getattr(account, "auto_trade_margin_mode", None) if trade_type == "futures" else None
            leverage = None
            if trade_type == "futures":
                # Re-use cached leverage from earlier in this cycle
                if symbol in _leverage_cache:
                    leverage = _leverage_cache[symbol]
                    logger.info(f"[SIM] Using cached leverage for {symbol}: {leverage}x")
                else:
                    try:
                        connector = exchange_manager.get_exchange(SupportedExchange.BITGET)
                        configured_leverage = max(1, int(getattr(account, "auto_trade_leverage", 10) or 10))
                        if connector:
                            _, max_lever = await connector.get_max_leverage(symbol)
                            if isinstance(max_lever, (int, float)) and 1 <= max_lever <= 200:
                                leverage = min(configured_leverage, int(max_lever))
                            else:
                                leverage = configured_leverage
                        else:
                            leverage = configured_leverage
                        logger.info(f"[SIM] Using configured leverage for {symbol}: {leverage}x")
                    except Exception as e:
                        leverage = getattr(account, "auto_trade_leverage", 10) or 10
                        logger.warning(f"[SIM] Could not fetch max leverage for {symbol}: {e}, fallback={leverage}x")

            # ── AI Agent Trade Validation (if enabled for sim) ──
            is_new_position = not existing_pos and not is_dca
            # Skip AI validation for close signals from position reviews (AI already decided)
            is_position_review_close = False
            try:
                sig_raw = json.loads(sig.raw_data) if sig.raw_data else {}
                if sig_raw.get("source") == "position_review":
                    is_position_review_close = True
                    logger.info(
                        f"[SIM] 🤖 Position review close signal for {symbol} — "
                        f"skipping AI re-validation"
                    )
            except (json.JSONDecodeError, TypeError):
                pass
            try:
                from app.core.config import settings as app_settings
                sim_ai_enabled = app_settings.ENABLE_AI_AGENTS and getattr(account, "enable_ai", False)
                if (is_new_position or is_dca) and not is_position_review_close:
                    _trade_signal = {
                        "action": action,
                        "price": price,
                        "confidence": confidence,
                        "signal_id": sig.id,
                        "indicators": indicators,
                    }
                    _pos_context = {
                        "open_positions": open_count,
                        "max_positions": max_positions,
                        "available_balance": round(account.balance, 2),
                        "is_dca": is_dca,
                        "mode": "simulation",
                    }
                    trade_validation = None
                    if sim_ai_enabled:
                        from app.agents.orchestrator import AgentOrchestrator
                        trade_validation = await AgentOrchestrator.validate_trade(
                            db,
                            symbol=symbol,
                            signal=_trade_signal,
                            position_context=_pos_context,
                            timeframe=effective_timeframe,
                            auto_trade_ai_provider=getattr(account, "auto_trade_ai_provider", "orchestrator"),
                            tradingagents_llm_provider=getattr(account, "tradingagents_llm_provider", "openai"),
                            tradingagents_deep_think_llm=getattr(account, "tradingagents_deep_think_llm", "gpt-5.4"),
                            tradingagents_quick_think_llm=getattr(account, "tradingagents_quick_think_llm", "gpt-5.4-mini"),
                            tradingagents_backend_url=getattr(account, "tradingagents_backend_url", None),
                            tradingagents_max_debate_rounds=getattr(account, "tradingagents_max_debate_rounds", 2),
                            tradingagents_max_risk_discuss_rounds=getattr(account, "tradingagents_max_risk_discuss_rounds", 2),
                        )
                    else:
                        # AI disabled — use custom agents if enabled
                        from app.agents.custom_agents import are_custom_agents_enabled, custom_validate_trade
                        if are_custom_agents_enabled():
                            from app.agents.orchestrator import AgentOrchestrator
                            _ctx = await AgentOrchestrator._gather_context(symbol, effective_timeframe)
                            _ctx["signal"] = _trade_signal
                            _ctx["positions"] = _pos_context
                            trade_validation = await custom_validate_trade(
                                db, symbol, _trade_signal, _pos_context, _ctx,
                            )
                    if trade_validation and not trade_validation.get("approved", True):
                        logger.info(
                            f"[SIM] 🤖 {'Custom' if trade_validation.get('custom_agents') else 'AI'} agents REJECTED trade {action} {symbol}: "
                            f"{trade_validation.get('reasoning', 'N/A')}"
                        )
                        sig.status = SignalStatus.IGNORED
                        continue
                    elif trade_validation and trade_validation.get("approved"):
                        logger.info(
                            f"[SIM] 🤖 {'Custom' if trade_validation.get('custom_agents') else 'AI'} agents APPROVED trade {action} {symbol}"
                        )
            except Exception as e:
                logger.warning(f"[SIM] AI trade validation failed (non-blocking): {e}")

            order_result = await SimulationEngine.place_order(
                db=db,
                symbol=symbol,
                side=side,
                amount=amount,
                price=sim_order_price,
                order_type=sim_order_type,
                signal_id=sig.id,
                stop_loss=sl_data["stop_loss"],
                take_profit=sl_data["take_profit"],
                sl_type=sl_data.get("sl_type", "pct"),
                trade_type=trade_type,
                margin_mode=margin_mode,
                leverage=leverage,
            )
            if order_result.get("success"):
                sig.status = SignalStatus.EXECUTED
                sig.processed_at = _utcnow()
                orders_placed.append({
                    "symbol": symbol,
                    "action": action,
                    "price": price,
                    **order_result,
                })
                if order_result.get("action") == "opened_position":
                    open_count += 1
                elif order_result.get("action") == "closed_position":
                    open_count = max(0, open_count - 1)
            else:
                sig.status = SignalStatus.FAILED
                sig.error_message = order_result.get("error", "order failed")

        await db.commit()

        return {
            "skipped": False,
            "sl_tp_closed": closed,
            "sl_tp_backfilled": backfilled,
            "orders_placed": orders_placed,
            "signals_analyzed": signals_processed,
            "balance": round(account.balance, 2),
            "timeframe": effective_timeframe,
            "max_positions": max_positions,
        }

    # ── Helpers ─────────────────────────────────────────────

    @staticmethod
    def _smart_limit_price(
        market_price: float,
        side: str,
        indicators: Dict,
    ) -> float:
        """
        Calculate a smart limit entry price instead of market price.
        Uses BB, MA, and RSI from the signal pipeline to set a slightly
        better-than-market limit price for improved fills.
        """
        if market_price <= 0:
            return market_price

        bb_lower = indicators.get("bb_lower")
        bb_upper = indicators.get("bb_upper")
        bb_middle = indicators.get("bb_middle")
        ma5 = indicators.get("ma5")
        ma10 = indicators.get("ma10")
        rsi_val = indicators.get("rsi")

        if side == "buy":
            # BUY: set limit below market, near support
            candidates = [market_price * 0.998]  # 0.2% below market as baseline

            # If BB lower is close (within 1.5%), use it as a reference
            if bb_lower and bb_lower < market_price and bb_lower > market_price * 0.985:
                candidates.append(bb_lower * 1.001)  # just above lower BB

            # If short-term MA is below price but close, buy near it
            if ma5 and ma5 < market_price and ma5 > market_price * 0.99:
                candidates.append(ma5)

            # If RSI is very oversold, don't be too greedy — stay closer to market
            if rsi_val and rsi_val < 35:
                candidates.append(market_price * 0.999)

            # Pick the highest candidate (closest to market for best fill chance)
            limit = max(candidates)
        else:
            # SELL: set limit above market, near resistance
            candidates = [market_price * 1.002]  # 0.2% above market as baseline

            if bb_upper and bb_upper > market_price and bb_upper < market_price * 1.015:
                candidates.append(bb_upper * 0.999)

            if ma5 and ma5 > market_price and ma5 < market_price * 1.01:
                candidates.append(ma5)

            if rsi_val and rsi_val > 65:
                candidates.append(market_price * 1.001)

            limit = min(candidates)

        return smart_round(limit, market_price)

    @staticmethod
    def _calc_pnl(pos: SimPosition, exit_price: float) -> float:
        if pos.side == "long":
            return (exit_price - pos.entry_price) * pos.amount
        return (pos.entry_price - exit_price) * pos.amount

    @staticmethod
    def _pos_to_dict(p: SimPosition) -> Dict:
        margin = SimulationEngine._position_margin(p)
        unrealized = p.unrealized_pnl or 0
        realized = p.realized_pnl or 0
        # ROE% = PnL / margin * 100  (leverage-adjusted return)
        roe_pct = (unrealized / margin * 100) if margin > 0 and p.status == 'open' else 0
        realized_roe_pct = (realized / margin * 100) if margin > 0 and p.status == 'closed' else 0
        return {
            "id": p.id,
            "symbol": p.symbol,
            "side": p.side,
            "amount": p.amount,
            "entry_price": p.entry_price,
            "current_price": p.current_price,
            "stop_loss": p.stop_loss,
            "take_profit": p.take_profit,
            "trade_type": getattr(p, "trade_type", "spot") or "spot",
            "margin_mode": getattr(p, "margin_mode", None),
            "leverage": getattr(p, "leverage", None),
            "margin": round(margin, 2),
            "unrealized_pnl": round(unrealized, 2),
            "unrealized_roe_pct": round(roe_pct, 2),
            "realized_pnl": round(realized, 2) if p.realized_pnl else None,
            "realized_roe_pct": round(realized_roe_pct, 2) if p.realized_pnl else None,
            "status": p.status,
            "signal_id": p.signal_id,
            "created_at": p.created_at.isoformat(),
            "closed_at": p.closed_at.isoformat() if p.closed_at else None,
        }

    @staticmethod
    def account_to_dict(a: SimAccount, metrics: Optional[Dict[str, Any]] = None) -> Dict:
        win_rate = (
            round(a.winning_trades / a.total_trades * 100, 1)
            if a.total_trades > 0 else 0
        )
        payload = {
            "id": a.id,
            "name": a.name,
            "is_active": a.is_active,
            "balance": round(a.balance, 2),
            "initial_balance": round(a.initial_balance, 2),
            "total_pnl": round(a.total_pnl, 2),
            "total_pnl_pct": round(
                (a.total_pnl / a.initial_balance * 100) if a.initial_balance > 0 else 0, 2
            ),
            "total_trades": a.total_trades,
            "winning_trades": a.winning_trades,
            "losing_trades": a.losing_trades,
            "win_rate": win_rate,
            "auto_trade": a.auto_trade,
            "auto_trade_pairs": json.loads(a.auto_trade_pairs or "[]"),
            "auto_trade_timeframe": a.auto_trade_timeframe,
            "auto_trade_max_positions": a.auto_trade_max_positions,
            "auto_trade_risk_pct": a.auto_trade_risk_pct,
            "auto_trade_mode": getattr(a, "auto_trade_mode", "spot") or "spot",
            "auto_trade_leverage": getattr(a, "auto_trade_leverage", 10) or 10,
            "auto_trade_margin_mode": getattr(a, "auto_trade_margin_mode", "crossed") or "crossed",
            "auto_trade_amount_mode": getattr(a, "auto_trade_amount_mode", "quote") or "quote",
            "auto_trade_pine_script_id": getattr(a, "auto_trade_pine_script_id", None),
            "enable_ai": getattr(a, "enable_ai", False),
            "auto_trade_ai_provider": getattr(a, "auto_trade_ai_provider", "orchestrator") or "orchestrator",
            "tradingagents_llm_provider": getattr(a, "tradingagents_llm_provider", "openai") or "openai",
            "tradingagents_deep_think_llm": getattr(a, "tradingagents_deep_think_llm", "gpt-5.4") or "gpt-5.4",
            "tradingagents_quick_think_llm": getattr(a, "tradingagents_quick_think_llm", "gpt-5.4-mini") or "gpt-5.4-mini",
            "tradingagents_backend_url": getattr(a, "tradingagents_backend_url", None),
            "tradingagents_max_debate_rounds": getattr(a, "tradingagents_max_debate_rounds", 2) or 2,
            "tradingagents_max_risk_discuss_rounds": getattr(a, "tradingagents_max_risk_discuss_rounds", 2) or 2,
            "margin_size_usdt": getattr(a, "margin_size_usdt", 10.0) or 10.0,
            "min_entry_gap_pct": getattr(a, "min_entry_gap_pct", 2.0) or 2.0,
            "min_pump_pct": getattr(a, "min_pump_pct", 30.0) or 30.0,
            "min_confidence": getattr(a, "min_confidence", 0.90) or 0.90,
            "sniper_max_entries": getattr(a, "sniper_max_entries", 1) or 1,
            "created_at": a.created_at.isoformat(),
        }
        if metrics:
            payload.update(metrics)
        else:
            payload.update({
                "equity": round(a.balance, 2),
                "unrealized_pnl": 0.0,
                "reserved_margin": 0.0,
                "open_positions_count": 0,
            })
        return payload
