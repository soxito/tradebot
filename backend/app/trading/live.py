"""
Live Auto-Trade Engine
Reads pending signals from the DB and places real orders via Bitget.
Mirrors the simulation auto_trade_cycle but executes on the exchange.
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List, cast

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from loguru import logger

from app.models.database import (
    LiveTradeSettings, Signal, SignalAction, SignalStatus, Trade,
)
from app.exchanges.manager import exchange_manager, SupportedExchange
from app.exchanges.bitget import BitgetConnector
from app.trading.simulation import SmartStopLoss, SimulationEngine
from app.core.config import settings
from app.utils.precision import smart_round
import numpy as np


from app.core.timezone import now_sast


def _utcnow():
    """Naive SAST datetime for DB compatibility."""
    return now_sast()


class LiveTradeEngine:
    """Engine that reads pending signals and executes live orders."""

    # ── Settings management ────────────────────────────────

    @staticmethod
    async def get_or_create_settings(db: AsyncSession) -> LiveTradeSettings:
        result = await db.execute(select(LiveTradeSettings).limit(1))
        row = result.scalar_one_or_none()
        if not row:
            row = LiveTradeSettings()
            db.add(row)
            await db.commit()
            await db.refresh(row)
        return row

    @staticmethod
    async def get_settings_snapshot(
        db: AsyncSession,
        settings_row: Optional[LiveTradeSettings] = None,
    ) -> Dict[str, Any]:
        s = settings_row or await LiveTradeEngine.get_or_create_settings(db)

        # Get live balance + positions from exchange for snapshot
        balance = 0.0
        equity = 0.0
        unrealized_pnl = 0.0
        open_positions_count = 0
        try:
            connector = cast(Optional[BitgetConnector], exchange_manager.get_exchange(SupportedExchange.BITGET))
            if connector:
                bal_data = await connector.get_futures_balance()
                for b in (bal_data or []):
                    balance += LiveTradeEngine._extract_available_margin(b)
                    equity += float(b.get("equity") or b.get("usdtEquity", 0))
                    unrealized_pnl += float(b.get("unrealizedPL", 0))
                pos_data = await connector.get_futures_positions()
                for p in (pos_data or []):
                    if float(p.get("total", 0)) > 0:
                        open_positions_count += 1
        except Exception as e:
            logger.warning(f"[LIVE] Could not fetch exchange data for snapshot: {e}")

        return {
            "id": s.id,
            "is_active": s.is_active,
            "auto_trade": s.auto_trade,
            "dry_run": bool(s.dry_run),
            "auto_trade_pairs": json.loads(s.auto_trade_pairs or "[]"),
            "auto_trade_timeframe": s.auto_trade_timeframe,
            "auto_trade_max_positions": s.auto_trade_max_positions,
            "auto_trade_risk_pct": s.auto_trade_risk_pct,
            "auto_trade_mode": s.auto_trade_mode,
            "auto_trade_amount_mode": s.auto_trade_amount_mode or "quote",
            "auto_trade_leverage": s.auto_trade_leverage,
            "auto_trade_margin_mode": s.auto_trade_margin_mode,
            "auto_trade_pine_script_id": s.auto_trade_pine_script_id,
            "max_position_size_usdt": s.max_position_size_usdt,
            "max_total_exposure_usdt": s.max_total_exposure_usdt,
            "margin_size_usdt": getattr(s, "margin_size_usdt", 10.0) or 10.0,
            "min_entry_gap_pct": getattr(s, "min_entry_gap_pct", 2.0) or 2.0,
            "min_pump_pct": getattr(s, "min_pump_pct", 30.0) or 30.0,
            "min_confidence": getattr(s, "min_confidence", 0.90) or 0.90,
            "sniper_max_entries": getattr(s, "sniper_max_entries", 1) or 1,
            "enable_ai": getattr(s, "enable_ai", True),
            "auto_trade_ai_provider": getattr(s, "auto_trade_ai_provider", "orchestrator") or "orchestrator",
            "tradingagents_llm_provider": getattr(s, "tradingagents_llm_provider", "openai") or "openai",
            "tradingagents_deep_think_llm": getattr(s, "tradingagents_deep_think_llm", "gpt-5.4") or "gpt-5.4",
            "tradingagents_quick_think_llm": getattr(s, "tradingagents_quick_think_llm", "gpt-5.4-mini") or "gpt-5.4-mini",
            "tradingagents_backend_url": getattr(s, "tradingagents_backend_url", None),
            "tradingagents_max_debate_rounds": getattr(s, "tradingagents_max_debate_rounds", 2) or 2,
            "tradingagents_max_risk_discuss_rounds": getattr(s, "tradingagents_max_risk_discuss_rounds", 2) or 2,
            "total_trades": s.total_trades,
            "winning_trades": s.winning_trades,
            "losing_trades": s.losing_trades,
            "balance": round(balance, 2),
            "equity": round(equity, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "open_positions_count": open_positions_count,
        }

    @staticmethod
    async def update_settings(db: AsyncSession, **kwargs) -> LiveTradeSettings:
        s = await LiveTradeEngine.get_or_create_settings(db)

        VALID_TIMEFRAMES = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"}
        VALID_MARGIN_MODES = {"crossed", "isolated"}
        VALID_TRADE_MODES = {"spot", "futures"}
        VALID_AMOUNT_MODES = {"quote", "base"}
        VALID_AI_PROVIDERS = {"orchestrator", "tradingagents"}

        for key, value in kwargs.items():
            if value is None:
                continue
            if key == "auto_trade_pairs" and isinstance(value, list):
                setattr(s, key, json.dumps(value))
            elif key == "auto_trade_max_positions":
                setattr(s, key, max(1, min(100, int(value))))
            elif key == "auto_trade_risk_pct":
                setattr(s, key, max(0.1, min(10.0, float(value))))
            elif key == "auto_trade_leverage":
                setattr(s, key, max(1, min(200, int(value))))
            elif key == "auto_trade_timeframe":
                if value in VALID_TIMEFRAMES:
                    setattr(s, key, value)
            elif key == "auto_trade_margin_mode":
                if value in VALID_MARGIN_MODES:
                    setattr(s, key, value)
            elif key == "auto_trade_mode":
                if value in VALID_TRADE_MODES:
                    setattr(s, key, value)
            elif key == "auto_trade_amount_mode":
                if value in VALID_AMOUNT_MODES:
                    setattr(s, key, value)
            elif key == "auto_trade_ai_provider":
                provider = str(value).strip().lower()
                if provider in VALID_AI_PROVIDERS:
                    setattr(s, key, provider)
            elif key in {"tradingagents_llm_provider", "tradingagents_deep_think_llm", "tradingagents_quick_think_llm"}:
                text_value = str(value).strip()
                if text_value:
                    setattr(s, key, text_value)
            elif key == "tradingagents_backend_url":
                text_value = str(value).strip() if value is not None else ""
                setattr(s, key, text_value or None)
            elif key in {"tradingagents_max_debate_rounds", "tradingagents_max_risk_discuss_rounds"}:
                setattr(s, key, max(1, min(6, int(value))))
            elif key == "max_position_size_usdt":
                setattr(s, key, max(10.0, float(value)))
            elif key == "max_total_exposure_usdt":
                setattr(s, key, max(50.0, float(value)))
            elif key == "margin_size_usdt":
                setattr(s, key, max(1.0, float(value)))
            elif key == "min_entry_gap_pct":
                setattr(s, key, max(0.5, min(20.0, float(value))))
            elif key == "min_pump_pct":
                setattr(s, key, max(1.0, min(500.0, float(value))))
            elif key == "min_confidence":
                setattr(s, key, max(0.50, min(1.0, float(value))))
            elif key == "sniper_max_entries":
                setattr(s, key, max(1, min(10, int(value))))
            elif key == "sniper_max_positions":
                setattr(s, key, max(1, min(20, int(value))))
            elif key == "dry_run":
                setattr(s, key, bool(value))
            elif hasattr(s, key):
                setattr(s, key, value)

        s.updated_at = _utcnow()
        await db.commit()
        await db.refresh(s)
        return s

    @staticmethod
    def _bitget_symbol(symbol: str) -> str:
        return symbol.replace("/", "").upper()

    @staticmethod
    def _hold_side_for_order(side: str) -> str:
        return "long" if side == "buy" else "short"

    @staticmethod
    def _extract_position_amount(position: Dict[str, Any]) -> float:
        for key in ("available", "total", "size"):
            value = position.get(key)
            if value not in (None, ""):
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
        return 0.0

    @staticmethod
    def _extract_position_price(position: Dict[str, Any]) -> float:
        for key in ("markPrice", "marketPrice", "averageOpenPrice", "openPriceAvg"):
            value = position.get(key)
            if value not in (None, ""):
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
        return 0.0

    @staticmethod
    def _extract_position_margin(position: Dict[str, Any]) -> float:
        for key in ("marginSize", "margin", "initialMargin"):
            value = position.get(key)
            if value not in (None, ""):
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
        return 0.0

    @staticmethod
    def _extract_available_margin(account: Dict[str, Any]) -> float:
        """
        Extract tradable futures margin from a Bitget account payload.
        Prefer openable-margin fields over raw `available`, which can be
        negative while crossed margin remains openable.
        """
        values: List[float] = []
        for key in (
            "crossedMaxAvailable",
            "unionAvailable",
            "isolatedMaxAvailable",
            "maxTransferOut",
            "available",
        ):
            raw = account.get(key)
            if raw in (None, ""):
                continue
            try:
                values.append(float(raw))
            except (TypeError, ValueError):
                continue

        if not values:
            return 0.0

        positives = [v for v in values if v > 0]
        if positives:
            return max(positives)
        return max(values)

    @staticmethod
    def _sum_available_margin(accounts: Optional[List[Dict[str, Any]]]) -> float:
        return sum(LiveTradeEngine._extract_available_margin(a) for a in (accounts or []))

    @staticmethod
    async def check_stop_loss_take_profit(
        db: AsyncSession,
        settings_row: Optional[LiveTradeSettings] = None,
        connector=None,
        open_positions: Optional[List[Dict[str, Any]]] = None,
        dry_run: bool = False,
    ) -> List[Dict[str, Any]]:
        connector = cast(Optional[BitgetConnector], connector or exchange_manager.get_exchange(SupportedExchange.BITGET))
        if not connector:
            return []

        tracked_result = await db.execute(
            select(Trade).where(
                Trade.exchange == "bitget",
                Trade.status == "open",
            )
        )
        tracked_trades = tracked_result.scalars().all()
        if not tracked_trades:
            return []

        if open_positions is None:
            pos_data = await connector.get_futures_positions()
            open_positions = [
                p for p in (pos_data or []) if LiveTradeEngine._extract_position_amount(p) > 0
            ]

        settings_ref = settings_row
        positions_by_key = {
            (LiveTradeEngine._bitget_symbol(p.get("symbol", "")), (p.get("holdSide", "") or "").lower()): p
            for p in open_positions
            if LiveTradeEngine._extract_position_amount(p) > 0
        }
        closed = []

        for trade in tracked_trades:
            if trade.stop_loss is None and trade.take_profit is None:
                continue

            hold_side = LiveTradeEngine._hold_side_for_order(trade.side)
            position = positions_by_key.get((LiveTradeEngine._bitget_symbol(trade.symbol), hold_side))
            if not position:
                continue

            current_price = LiveTradeEngine._extract_position_price(position)
            if current_price <= 0:
                try:
                    ticker = await connector.get_ticker(trade.symbol)
                    current_price = float(ticker.get("last") or ticker.get("close") or 0)
                except Exception:
                    current_price = 0.0
            if current_price <= 0:
                continue

            stop_hit = trade.stop_loss is not None and (
                (trade.side == "buy" and current_price <= trade.stop_loss)
                or (trade.side == "sell" and current_price >= trade.stop_loss)
            )
            target_hit = trade.take_profit is not None and (
                (trade.side == "buy" and current_price >= trade.take_profit)
                or (trade.side == "sell" and current_price <= trade.take_profit)
            )
            if not (stop_hit or target_hit):
                continue

            amount = LiveTradeEngine._extract_position_amount(position)
            if amount <= 0:
                continue

            close_side = "sell" if trade.side == "buy" else "buy"
            close_reason = "stop_loss" if stop_hit else "take_profit"
            pnl = float(position.get("unrealizedPL", 0) or 0)

            if dry_run:
                closed.append({
                    "symbol": trade.symbol,
                    "side": trade.side,
                    "amount": round(amount, 6),
                    "price": current_price,
                    "pnl": round(pnl, 2),
                    "reason": close_reason,
                    "dry_run": True,
                })
                continue

            close_order = await connector.create_futures_order(
                symbol=LiveTradeEngine._bitget_symbol(trade.symbol),
                margin_coin="USDT",
                side=close_side,
                order_type="market",
                size=str(round(amount, 6)),
                margin_mode=trade.margin_mode or "crossed",
                leverage=trade.leverage,
                trade_side="close",
            )

            if settings_ref is None:
                settings_ref = await LiveTradeEngine.get_or_create_settings(db)

            # Try to get actual fill data from exchange for accurate PnL
            actual_pnl = pnl
            actual_exit = current_price
            try:
                import asyncio
                await asyncio.sleep(0.3)
                fill = await connector.lookup_close_fill(
                    symbol=trade.symbol,
                    hold_side=LiveTradeEngine._hold_side_for_order(trade.side),
                )
                if fill and fill["exit_price"] > 0:
                    actual_pnl = fill["pnl"]
                    actual_exit = fill["exit_price"]
            except Exception:
                pass  # fall back to unrealizedPL snapshot

            settings_ref.total_trades = (settings_ref.total_trades or 0) + 1
            if actual_pnl >= 0:
                settings_ref.winning_trades = (settings_ref.winning_trades or 0) + 1
            else:
                settings_ref.losing_trades = (settings_ref.losing_trades or 0) + 1

            trade.status = "closed"
            trade.closed_at = _utcnow()
            trade.average_price = actual_exit
            trade.filled_amount = amount
            trade.pnl = actual_pnl
            try:
                open_response = json.loads(trade.raw_response) if trade.raw_response else None
            except (TypeError, ValueError):
                open_response = trade.raw_response
            trade.raw_response = json.dumps({
                "open": open_response,
                "close": close_order,
                "close_reason": close_reason,
            })

            db.add(
                Trade(
                    exchange="bitget",
                    exchange_order_id=close_order.get("orderId", ""),
                    signal_id=None,
                    symbol=trade.symbol,
                    side=close_side,
                    trade_side="close",
                    order_type="market",
                    amount=amount,
                    price=actual_exit,
                    filled_amount=amount,
                    average_price=actual_exit,
                    pnl=actual_pnl,
                    status="closed",
                    raw_response=json.dumps({
                        "close": close_order,
                        "close_reason": close_reason,
                        "linked_trade_id": trade.id,
                    }),
                    margin_mode=trade.margin_mode,
                    leverage=trade.leverage,
                    closed_at=_utcnow(),
                )
            )

            closed.append({
                "symbol": trade.symbol,
                "side": trade.side,
                "amount": round(amount, 6),
                "price": actual_exit,
                "pnl": round(actual_pnl, 2),
                "reason": close_reason,
            })

        return closed

    # ── Backfill SL/TP for existing positions ──────────────

    @staticmethod
    async def backfill_sl_tp(
        db: AsyncSession,
        connector=None,
        open_positions: Optional[List[Dict[str, Any]]] = None,
        timeframe: str = "1h",
        dry_run: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Find live positions tracked in the Trade DB that have no SL or TP,
        calculate smart SL/TP, place TPSL plan orders on Bitget, and update
        the Trade record.
        """
        connector = cast(
            Optional[BitgetConnector],
            connector or exchange_manager.get_exchange(SupportedExchange.BITGET),
        )
        if not connector:
            return []

        # Get trades missing SL or TP
        result = await db.execute(
            select(Trade).where(
                Trade.exchange == "bitget",
                Trade.status == "open",
                Trade.trade_side == "open",
            )
        )
        tracked_trades = result.scalars().all()
        needs_update = [
            t for t in tracked_trades if t.stop_loss is None or t.take_profit is None
        ]
        if not needs_update:
            return []

        # Get exchange positions if not provided
        if open_positions is None:
            pos_data = await connector.get_futures_positions()
            open_positions = [
                p for p in (pos_data or [])
                if LiveTradeEngine._extract_position_amount(p) > 0
            ]

        positions_by_key = {
            (
                LiveTradeEngine._bitget_symbol(p.get("symbol", "")),
                (p.get("holdSide", "") or "").lower(),
            ): p
            for p in open_positions
            if LiveTradeEngine._extract_position_amount(p) > 0
        }

        updated = []
        for trade in needs_update:
            hold_side = LiveTradeEngine._hold_side_for_order(trade.side)
            bitget_symbol = LiveTradeEngine._bitget_symbol(trade.symbol)
            position = positions_by_key.get((bitget_symbol, hold_side))
            if not position:
                continue

            amount = LiveTradeEngine._extract_position_amount(position)
            if amount <= 0:
                continue

            # Get entry price from position or trade
            entry_price = float(
                position.get("averageOpenPrice")
                or position.get("openPriceAvg")
                or trade.price
                or 0,
            )
            if entry_price <= 0:
                continue

            # Calculate smart SL/TP
            try:
                ohlcv = await connector.get_ohlcv(
                    symbol=trade.symbol, timeframe=timeframe, limit=200,
                )
                sl_data = SmartStopLoss.calculate(ohlcv, hold_side, entry_price)
            except Exception as e:
                logger.warning(
                    f"[LIVE] SL/TP backfill OHLCV failed for {trade.symbol}: {e}"
                )
                sl_data = {
                    "stop_loss": SmartStopLoss.from_pct(entry_price, hold_side),
                    "take_profit": entry_price * (1.04 if hold_side == "long" else 0.96),
                    "sl_type": "pct",
                }

            new_sl = sl_data.get("stop_loss")
            new_tp = sl_data.get("take_profit")
            if not new_sl and not new_tp:
                continue

            if dry_run:
                trade.stop_loss = trade.stop_loss or new_sl
                trade.take_profit = trade.take_profit or new_tp
                updated.append({
                    "symbol": trade.symbol,
                    "side": trade.side,
                    "sl": trade.stop_loss,
                    "tp": trade.take_profit,
                    "sl_type": sl_data.get("sl_type"),
                    "dry_run": True,
                })
                continue

            # Place TPSL on exchange for missing SL
            if trade.stop_loss is None and new_sl:
                try:
                    await connector.place_tpsl_order(
                        symbol=bitget_symbol,
                        margin_coin="USDT",
                        plan_type="loss_plan",
                        trigger_price=new_sl,
                        hold_side=hold_side,
                        size=str(round(amount, 6)),
                    )
                    trade.stop_loss = new_sl
                    logger.info(
                        f"[LIVE] Backfilled SL for {trade.symbol} {hold_side}: {new_sl}"
                    )
                except Exception as e:
                    # Still save SL in DB so internal check_stop_loss_take_profit can
                    # close the position if the price already moved past SL
                    trade.stop_loss = new_sl
                    logger.error(
                        f"[LIVE] Failed to place SL on exchange for {trade.symbol} "
                        f"(saved to DB for internal monitoring): "
                        + str(e).replace("{", "{{").replace("}", "}}")
                    )

            # Place TPSL on exchange for missing TP
            if trade.take_profit is None and new_tp:
                try:
                    await connector.place_tpsl_order(
                        symbol=bitget_symbol,
                        margin_coin="USDT",
                        plan_type="profit_plan",
                        trigger_price=new_tp,
                        hold_side=hold_side,
                        size=str(round(amount, 6)),
                    )
                    trade.take_profit = new_tp
                    logger.info(
                        f"[LIVE] Backfilled TP for {trade.symbol} {hold_side}: {new_tp}"
                    )
                except Exception as e:
                    # Still save TP in DB for internal monitoring
                    trade.take_profit = new_tp
                    logger.error(
                        f"[LIVE] Failed to place TP on exchange for {trade.symbol} "
                        f"(saved to DB for internal monitoring): "
                        + str(e).replace("{", "{{").replace("}", "}}")
                    )

            if trade.stop_loss or trade.take_profit:
                updated.append({
                    "symbol": trade.symbol,
                    "side": trade.side,
                    "sl": trade.stop_loss,
                    "tp": trade.take_profit,
                    "sl_type": sl_data.get("sl_type"),
                })

        if updated:
            await db.commit()
        return updated

    # ── Profit Runner: Remove TP + Trail SL for big winners ──────────

    @staticmethod
    async def run_profit_runner(
        db: AsyncSession,
        connector=None,
        open_positions: Optional[List[Dict[str, Any]]] = None,
        timeframe: str = "1h",
        tp_threshold_pct: float = 150.0,
        sl_lock_pct: float = 0.50,
        momentum_lock_pct: float = 0.70,
    ) -> List[Dict[str, Any]]:
        """
        For positions whose unrealised profit exceeds *tp_threshold_pct*:
          1. Cancel the existing take-profit trigger order (let it run).
          2. Move the stop-loss to lock in *sl_lock_pct* of the gain.
          3. If high momentum is detected (candle body > 2× ATR), tighten
             the SL to *momentum_lock_pct* of the gain instead.

        Returns a list of dicts describing what was changed.
        """
        connector = cast(
            Optional[BitgetConnector],
            connector or exchange_manager.get_exchange(SupportedExchange.BITGET),
        )
        if not connector:
            return []

        if open_positions is None:
            pos_data = await connector.get_futures_positions()
            open_positions = [
                p for p in (pos_data or [])
                if LiveTradeEngine._extract_position_amount(p) > 0
            ]

        results: List[Dict[str, Any]] = []

        for pos in open_positions:
            symbol_raw = pos.get("symbol", "")
            bitget_symbol = LiveTradeEngine._bitget_symbol(symbol_raw)
            hold_side = (pos.get("holdSide", "") or "").lower()
            if not hold_side or not bitget_symbol:
                continue

            entry_price = float(
                pos.get("averageOpenPrice")
                or pos.get("openPriceAvg")
                or 0,
            )
            mark_price = float(pos.get("markPrice") or pos.get("marketPrice") or 0)
            leverage = float(pos.get("leverage") or 1)
            amount = LiveTradeEngine._extract_position_amount(pos)
            if entry_price <= 0 or mark_price <= 0 or amount <= 0:
                continue

            # ── Profit % (including leverage) ──
            if hold_side == "long":
                profit_pct = (mark_price - entry_price) / entry_price * leverage * 100
            else:
                profit_pct = (entry_price - mark_price) / entry_price * leverage * 100

            if profit_pct < tp_threshold_pct:
                continue

            logger.info(
                f"[PROFIT RUNNER] {bitget_symbol} {hold_side} at +{profit_pct:.1f}% "
                f"(>{tp_threshold_pct}%) — evaluating TP removal & SL trail"
            )

            # ── Momentum detection via ATR ──
            lock_ratio = sl_lock_pct
            momentum_detected = False
            try:
                ccxt_symbol = symbol_raw if "/" in symbol_raw else (
                    symbol_raw.replace("USDT", "/USDT:USDT")
                    if symbol_raw.endswith("USDT") else symbol_raw
                )
                ohlcv = await connector.get_ohlcv(
                    symbol=ccxt_symbol, timeframe=timeframe, limit=50,
                )
                if ohlcv and len(ohlcv) >= 15:
                    import pandas as pd
                    from app.trading.simulation import ohlcv_to_dataframe
                    df = ohlcv_to_dataframe(ohlcv) if not hasattr(ohlcv, "columns") else ohlcv
                    tr = np.maximum(
                        df["high"] - df["low"],
                        np.maximum(
                            abs(df["high"] - df["close"].shift(1)),
                            abs(df["low"] - df["close"].shift(1)),
                        ),
                    )
                    atr_val = tr.rolling(14).mean().iloc[-1]
                    last_body = abs(float(df["close"].iloc[-1]) - float(df["open"].iloc[-1]))
                    if not np.isnan(atr_val) and atr_val > 0 and last_body > 2 * atr_val:
                        momentum_detected = True
                        lock_ratio = momentum_lock_pct
                        logger.info(
                            f"[PROFIT RUNNER] {bitget_symbol} HIGH MOMENTUM detected "
                            f"(body {last_body:.6f} > 2×ATR {atr_val:.6f}) — "
                            f"tightening SL lock to {lock_ratio*100:.0f}%"
                        )
            except Exception as e:
                logger.warning(
                    f"[PROFIT RUNNER] {bitget_symbol} momentum check failed (using default lock): {e}"
                )

            # ── Calculate new SL price to lock profits ──
            if hold_side == "long":
                price_gain = mark_price - entry_price
                new_sl = smart_round(entry_price + price_gain * lock_ratio, entry_price)
            else:
                price_gain = entry_price - mark_price
                new_sl = smart_round(entry_price - price_gain * lock_ratio, entry_price)

            # Sanity: SL must be on the correct side of current price
            if hold_side == "long" and new_sl >= mark_price:
                new_sl = smart_round(mark_price * 0.995, mark_price)
            elif hold_side == "short" and new_sl <= mark_price:
                new_sl = smart_round(mark_price * 1.005, mark_price)

            # ── Cancel existing TP orders (let profits run) ──
            tp_cancelled = 0
            try:
                pending = await connector.get_pending_tpsl_orders(symbol=bitget_symbol)
                for order in (pending or []):
                    order_side = (order.get("posSide") or order.get("holdSide") or "").lower()
                    if order_side and order_side != hold_side:
                        continue
                    plan_type = (order.get("planType") or "").lower()
                    oid = order.get("orderId", "")
                    if plan_type in ("profit_plan", "pos_profit") and oid:
                        try:
                            await connector.cancel_tpsl_order(
                                order_id=oid,
                                symbol=bitget_symbol,
                                plan_type=plan_type,
                            )
                            tp_cancelled += 1
                        except Exception as e:
                            logger.warning(
                                f"[PROFIT RUNNER] Failed to cancel TP {oid}: {e}"
                            )
            except Exception as e:
                logger.warning(f"[PROFIT RUNNER] Failed to fetch pending orders for {bitget_symbol}: {e}")

            # ── Place / replace SL at the new trailed level ──
            sl_placed = False
            try:
                replace_result = await connector.replace_tpsl_orders(
                    symbol=bitget_symbol,
                    hold_side=hold_side,
                    new_sl=new_sl,
                    new_tp=None,
                    margin_coin="USDT",
                    size=str(round(amount, 6)),
                )
                sl_placed = len(replace_result.get("placed", [])) > 0
            except Exception as e:
                logger.error(f"[PROFIT RUNNER] Failed to replace SL for {bitget_symbol}: {e}")

            # ── Update Trade DB record ──
            try:
                trade_side_db = "buy" if hold_side == "long" else "sell"
                trade_result = await db.execute(
                    select(Trade).where(
                        Trade.exchange == "bitget",
                        Trade.status == "open",
                        Trade.trade_side == "open",
                        Trade.side == trade_side_db,
                        Trade.symbol.ilike(f"%{symbol_raw.replace('USDT', '/USDT')}%"),
                    )
                )
                trade_record = trade_result.scalars().first()
                if trade_record:
                    trade_record.take_profit = None
                    trade_record.stop_loss = new_sl
            except Exception as e:
                logger.warning(f"[PROFIT RUNNER] DB update failed for {bitget_symbol}: {e}")

            lock_pct_actual = lock_ratio * 100
            entry = {
                "symbol": bitget_symbol,
                "hold_side": hold_side,
                "profit_pct": round(profit_pct, 1),
                "new_sl": new_sl,
                "lock_pct": lock_pct_actual,
                "tp_cancelled": tp_cancelled,
                "sl_placed": sl_placed,
                "momentum": momentum_detected,
            }
            results.append(entry)
            logger.info(
                f"[PROFIT RUNNER] {bitget_symbol} {hold_side} +{profit_pct:.1f}% — "
                f"TP removed ({tp_cancelled}), SL trailed to {new_sl} "
                f"(locking {lock_pct_actual:.0f}% of gain"
                f"{', HIGH MOMENTUM' if momentum_detected else ''})"
            )

        if results:
            await db.commit()
        return results

    # ── Backfill SL/TP for exchange positions (no DB trade required) ──

    @staticmethod
    async def backfill_exchange_positions_sl_tp(
        db: AsyncSession,
        connector=None,
        timeframe: str = "1h",
    ) -> List[Dict[str, Any]]:
        """
        Find exchange positions that have no TPSL plan orders, calculate
        smart SL/TP, and place TPSL on Bitget. Works even when there is no
        matching Trade record in the DB (e.g. manually opened positions).
        """
        connector = cast(
            Optional[BitgetConnector],
            connector or exchange_manager.get_exchange(SupportedExchange.BITGET),
        )
        if not connector:
            return []

        # Ensure precision cache is populated for correct price rounding
        await connector.get_max_leverage("BTCUSDT")

        pos_data = await connector.get_futures_positions()
        open_positions = [
            p for p in (pos_data or [])
            if LiveTradeEngine._extract_position_amount(p) > 0
        ]
        if not open_positions:
            return []

        updated: List[Dict[str, Any]] = []

        for position in open_positions:
            bitget_symbol = (position.get("symbol") or "").upper()
            hold_side = (position.get("holdSide") or "").lower()
            if not bitget_symbol or not hold_side:
                continue

            # Check if position already has SL/TP via trigger plan orders
            has_sl = False
            has_tp = False
            try:
                pending_orders = await connector.get_pending_tpsl_orders(
                    symbol=bitget_symbol,
                )
                for order in (pending_orders or []):
                    pt = (order.get("planType") or "").lower()
                    os_side = (order.get("posSide") or order.get("holdSide") or "").lower()
                    if os_side and os_side != hold_side:
                        continue
                    if pt in ("loss_plan", "pos_loss"):
                        has_sl = True
                    if pt in ("profit_plan", "pos_profit"):
                        has_tp = True
            except Exception:
                # If we can't check, assume no SL/TP and try to add them
                pass

            if has_sl and has_tp:
                continue

            amount = LiveTradeEngine._extract_position_amount(position)
            if amount <= 0:
                continue

            entry_price = float(
                position.get("averageOpenPrice")
                or position.get("openPriceAvg")
                or 0,
            )
            if entry_price <= 0:
                continue

            # Normalize symbol for OHLCV lookup
            display_symbol = bitget_symbol
            if "USDT" in bitget_symbol and "/" not in bitget_symbol:
                display_symbol = bitget_symbol.replace("USDT", "/USDT")

            # Calculate smart SL/TP
            try:
                ohlcv = await connector.get_ohlcv(
                    symbol=display_symbol, timeframe=timeframe, limit=200,
                )
                sl_data = SmartStopLoss.calculate(ohlcv, hold_side, entry_price)
            except Exception as e:
                logger.warning(
                    f"[LIVE] Exchange backfill OHLCV failed for {display_symbol}: {e}"
                )
                sl_data = {
                    "stop_loss": SmartStopLoss.from_pct(entry_price, hold_side),
                    "take_profit": entry_price * (1.04 if hold_side == "long" else 0.96),
                    "sl_type": "pct",
                }

            new_sl = sl_data.get("stop_loss")
            new_tp = sl_data.get("take_profit")
            placed_sl = False
            placed_tp = False

            # Place SL if missing
            if not has_sl and new_sl:
                try:
                    await connector.place_tpsl_order(
                        symbol=bitget_symbol,
                        margin_coin="USDT",
                        plan_type="loss_plan",
                        trigger_price=new_sl,
                        hold_side=hold_side,
                        size=str(round(amount, 6)),
                    )
                    placed_sl = True
                    logger.info(
                        f"[LIVE] Exchange backfill SL for {display_symbol} {hold_side}: {new_sl}"
                    )
                except Exception as e:
                    logger.error(
                        f"[LIVE] Failed to place SL for {display_symbol}: "
                        + str(e).replace("{", "{{").replace("}", "}}")
                    )

            # Place TP if missing
            if not has_tp and new_tp:
                try:
                    await connector.place_tpsl_order(
                        symbol=bitget_symbol,
                        margin_coin="USDT",
                        plan_type="profit_plan",
                        trigger_price=new_tp,
                        hold_side=hold_side,
                        size=str(round(amount, 6)),
                    )
                    placed_tp = True
                    logger.info(
                        f"[LIVE] Exchange backfill TP for {display_symbol} {hold_side}: {new_tp}"
                    )
                except Exception as e:
                    logger.error(
                        f"[LIVE] Failed to place TP for {display_symbol}: "
                        + str(e).replace("{", "{{").replace("}", "}}")
                    )

            # Also update matching Trade record if it exists
            if placed_sl or placed_tp:
                try:
                    result = await db.execute(
                        select(Trade).where(
                            Trade.exchange == "bitget",
                            Trade.status == "open",
                            Trade.trade_side == "open",
                            Trade.symbol == display_symbol,
                        )
                    )
                    trade = result.scalar_one_or_none()
                    if trade:
                        if placed_sl and trade.stop_loss is None:
                            trade.stop_loss = new_sl
                        if placed_tp and trade.take_profit is None:
                            trade.take_profit = new_tp
                except Exception:
                    pass

                updated.append({
                    "symbol": display_symbol,
                    "side": hold_side,
                    "sl": new_sl if placed_sl else None,
                    "tp": new_tp if placed_tp else None,
                    "sl_type": sl_data.get("sl_type"),
                })

        if updated:
            await db.commit()
        return updated

    # ── Backfill SL/TP for Open (Unfilled) Limit Orders ──────────

    @staticmethod
    async def backfill_open_orders_sl_tp(
        db: AsyncSession,
        connector=None,
        timeframe: str = "1h",
        dry_run: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Fetch open (pending) limit orders from Bitget, identify those without
        preset SL/TP, calculate smart SL/TP, and modify the order via
        Bitget's modify-order API to attach SL/TP.
        """
        connector = cast(
            Optional[BitgetConnector],
            connector or exchange_manager.get_exchange(SupportedExchange.BITGET),
        )
        if not connector:
            return []

        try:
            open_orders = await connector.get_futures_open_orders()
        except Exception as e:
            logger.warning(f"[LIVE] Failed to fetch open orders for SL/TP backfill: {e}")
            return []

        if not open_orders:
            return []

        # Filter orders that have no preset SL or TP and are open-side
        needs_sltp = [
            o for o in open_orders
            if o.get("tradeSide") == "open"
            and (not o.get("presetStopLossPrice") and not o.get("presetStopSurplusPrice"))
        ]
        if not needs_sltp:
            return []

        updated = []
        for order in needs_sltp:
            symbol_raw = order.get("symbol", "")  # e.g. "DOGEUSDT"
            order_id = order.get("orderId", "")
            order_price = float(order.get("price") or 0)
            side = order.get("side", "")  # buy or sell
            hold_side = "long" if side == "buy" else "short"

            if order_price <= 0 or not order_id:
                continue

            # Normalize symbol for OHLCV lookup (DOGEUSDT -> DOGE/USDT)
            display_symbol = symbol_raw
            if "USDT" in symbol_raw and "/" not in symbol_raw:
                display_symbol = symbol_raw.replace("USDT", "/USDT")

            # Calculate smart SL/TP
            try:
                ohlcv = await connector.get_ohlcv(
                    symbol=display_symbol, timeframe=timeframe, limit=200,
                )
                sl_data = SmartStopLoss.calculate(ohlcv, hold_side, order_price)
            except Exception as e:
                logger.warning(
                    f"[LIVE] Open order SL/TP OHLCV failed for {display_symbol}: {e}"
                )
                sl_data = {
                    "stop_loss": SmartStopLoss.from_pct(order_price, hold_side),
                    "take_profit": order_price * (1.04 if hold_side == "long" else 0.96),
                    "sl_type": "pct",
                }

            new_sl = sl_data.get("stop_loss")
            new_tp = sl_data.get("take_profit")
            if not new_sl and not new_tp:
                continue

            if dry_run:
                updated.append({
                    "symbol": display_symbol,
                    "order_id": order_id,
                    "side": side,
                    "price": order_price,
                    "sl": new_sl,
                    "tp": new_tp,
                    "dry_run": True,
                })
                continue

            try:
                await connector.modify_futures_order_tpsl(
                    symbol=symbol_raw,
                    order_id=order_id,
                    stop_loss=new_sl,
                    take_profit=new_tp,
                )
                updated.append({
                    "symbol": display_symbol,
                    "order_id": order_id,
                    "side": side,
                    "price": order_price,
                    "sl": new_sl,
                    "tp": new_tp,
                })
                logger.info(
                    f"[LIVE] Added SL/TP to open order {order_id} for {display_symbol}: "
                    f"SL={new_sl} TP={new_tp}"
                )

                # Also update the Trade DB record if it exists
                trade_result = await db.execute(
                    select(Trade).where(
                        Trade.exchange == "bitget",
                        Trade.exchange_order_id == order_id,
                        Trade.status == "open",
                    )
                )
                trade_record = trade_result.scalars().first()
                if trade_record:
                    if new_sl and trade_record.stop_loss is None:
                        trade_record.stop_loss = new_sl
                    if new_tp and trade_record.take_profit is None:
                        trade_record.take_profit = new_tp

            except Exception as e:
                logger.error(
                    f"[LIVE] Failed to add SL/TP to open order {order_id} for {display_symbol}: "
                    + str(e).replace("{", "{{").replace("}", "}}")
                )

        if updated and not dry_run:
            await db.commit()
        return updated

    # ── Auto-Trade Cycle ───────────────────────────────────

    @staticmethod
    async def auto_trade_cycle(db: AsyncSession) -> Dict[str, Any]:
        """
        One live auto-trade cycle:
        1. Check safety gates (ENABLE_AUTO_TRADING, is_active, auto_trade)
        2. Fetch exchange state (balance, open positions)
        3. Pick up recent PENDING signals
        4. Place real orders on Bitget
        """
        s = await LiveTradeEngine.get_or_create_settings(db)
        dry_run = bool(s.dry_run)

        # ── Safety gates ──
        if not settings.ENABLE_AUTO_TRADING and not dry_run:
            return {"skipped": True, "reason": "ENABLE_AUTO_TRADING is disabled in config"}
        if not s.is_active:
            return {"skipped": True, "reason": "Live trading not active"}
        if not s.auto_trade:
            return {"skipped": True, "reason": "Live auto-trade not enabled"}

        pairs = json.loads(s.auto_trade_pairs or "[]")
        if not pairs:
            return {"skipped": True, "reason": "No pairs configured"}

        max_positions = max(1, s.auto_trade_max_positions or 3)
        risk_pct = s.auto_trade_risk_pct or 1.0
        effective_timeframe = s.auto_trade_timeframe or "1h"
        margin_mode = s.auto_trade_margin_mode or "crossed"
        max_pos_size = s.max_position_size_usdt or 500.0
        max_exposure = s.max_total_exposure_usdt or 5000.0

        connector = cast(Optional[BitgetConnector], exchange_manager.get_exchange(SupportedExchange.BITGET))
        if not connector:
            return {"skipped": True, "reason": "Bitget connector not available"}

        synthetic_balance = max(max_exposure, max_pos_size * max_positions)

        # Per-cycle leverage cache
        _leverage_cache: dict = {}

        # ── Get exchange state and run the same SL/TP lifecycle as sim ──
        try:
            pos_data = await connector.get_futures_positions()
        except Exception as e:
            if dry_run:
                logger.warning(f"[LIVE][DRY-RUN] Failed to fetch positions, using empty state: {e}")
                pos_data = []
            else:
                logger.error(f"[LIVE] Failed to fetch positions: {e}")
                return {"skipped": True, "reason": f"Positions fetch failed: {e}"}

        open_positions = [
            p for p in (pos_data or []) if LiveTradeEngine._extract_position_amount(p) > 0
        ]
        closed = await LiveTradeEngine.check_stop_loss_take_profit(
            db,
            settings_row=s,
            connector=connector,
            open_positions=open_positions,
            dry_run=dry_run,
        )
        if closed and not dry_run:
            pos_data = await connector.get_futures_positions()
            open_positions = [
                p for p in (pos_data or []) if LiveTradeEngine._extract_position_amount(p) > 0
            ]

        # ── Backfill SL/TP for positions missing them ──
        try:
            backfilled = await LiveTradeEngine.backfill_sl_tp(
                db,
                connector=connector,
                open_positions=open_positions,
                timeframe=effective_timeframe,
                dry_run=dry_run,
            )
            if backfilled:
                logger.info(f"[LIVE] Backfilled SL/TP for {len(backfilled)} position(s)")
        except Exception as e:
            backfilled = []
            logger.warning(f"[LIVE] SL/TP backfill error (non-fatal): {e}")

        # ── Profit Runner: Remove TP + Trail SL for >150% winners ──
        profit_runner_results = []
        try:
            profit_runner_results = await LiveTradeEngine.run_profit_runner(
                db,
                connector=connector,
                open_positions=open_positions,
                timeframe=effective_timeframe,
            )
            if profit_runner_results:
                logger.info(
                    f"[LIVE] Profit runner updated {len(profit_runner_results)} position(s)"
                )
        except Exception as e:
            logger.warning(f"[LIVE] Profit runner error (non-fatal): {e}")

        # ── Backfill SL/TP for open (unfilled) limit orders ──
        orders_backfilled = []
        try:
            orders_backfilled = await LiveTradeEngine.backfill_open_orders_sl_tp(
                db,
                connector=connector,
                timeframe=effective_timeframe,
                dry_run=dry_run,
            )
            if orders_backfilled:
                logger.info(f"[LIVE] Added SL/TP to {len(orders_backfilled)} open order(s)")
        except Exception as e:
            logger.warning(f"[LIVE] Open order SL/TP backfill error (non-fatal): {e}")

        # ── AI Limit Order Optimization (better entries) ──
        limit_order_result = {}
        if not dry_run and settings.ENABLE_AI_AGENTS and getattr(s, "enable_ai", True):
            try:
                from app.agents.orchestrator import AgentOrchestrator
                limit_order_result = await AgentOrchestrator.analyze_limit_orders(db, min_age_minutes=5.0)
                adjusted_count = limit_order_result.get("orders_adjusted", 0)
                if adjusted_count > 0:
                    logger.info(f"[LIVE] 🎯 AI optimized {adjusted_count} limit order(s) for better entry")
            except Exception as e:
                logger.warning(f"[LIVE] Limit order optimization error (non-fatal): {e}")

        # ── AI Open Position SL/TP Optimization ──
        position_opt_result = {}
        if not dry_run and settings.ENABLE_AI_AGENTS and getattr(s, "enable_ai", True):
            try:
                from app.agents.orchestrator import AgentOrchestrator
                position_opt_result = await AgentOrchestrator.analyze_open_positions(db, min_age_minutes=10.0)
                pos_adjusted = position_opt_result.get("positions_adjusted", 0)
                if pos_adjusted > 0:
                    logger.info(f"[LIVE] 🔄 AI recalculated SL/TP for {pos_adjusted} position(s)")
            except Exception as e:
                logger.warning(f"[LIVE] Position SL/TP optimization error (non-fatal): {e}")

        try:
            bal_data = await connector.get_futures_balance()
            available_balance = LiveTradeEngine._sum_available_margin(bal_data)
        except Exception as e:
            if dry_run:
                available_balance = synthetic_balance
                logger.warning(
                    f"[LIVE][DRY-RUN] Failed to fetch balance, using synthetic balance "
                    f"{synthetic_balance:.2f}: {e}"
                )
            else:
                logger.error(f"[LIVE] Failed to fetch balance: {e}")
                return {"skipped": True, "reason": f"Balance fetch failed: {e}"}

        if available_balance <= 0 and not open_positions:
            if dry_run:
                available_balance = synthetic_balance
                logger.info(
                    f"[LIVE][DRY-RUN] No available balance from exchange, using synthetic balance {synthetic_balance:.2f}"
                )
            else:
                return {"skipped": True, "reason": "No available balance"}

        # ── AI Drawdown Circuit Breaker ──
        # If unrealized losses exceed 10% of equity, pause new entries
        try:
            equity = sum(float(b.get("equity") or b.get("usdtEquity", 0)) for b in (bal_data or []))
            unrealized = sum(float(b.get("unrealizedPL", 0)) for b in (bal_data or []))
            if equity > 0 and unrealized < 0:
                dd_pct = abs(unrealized) / equity * 100
                if dd_pct >= 10.0:
                    logger.warning(
                        f"[LIVE] ⚠️ DRAWDOWN CIRCUIT BREAKER: {dd_pct:.1f}% unrealized loss "
                        f"(${unrealized:.2f} / ${equity:.2f} equity) — skipping new entries"
                    )
                    return {
                        "skipped": True,
                        "reason": f"Drawdown circuit breaker: {dd_pct:.1f}% unrealized drawdown",
                        "drawdown_pct": round(dd_pct, 1),
                        "unrealized_pnl": round(unrealized, 2),
                        "equity": round(equity, 2),
                    }
        except Exception:
            pass  # Non-blocking: if we can't calculate, proceed

        open_count = len(open_positions)

        # ── Separate signal vs sniper position counts ──
        # Sniper trades have source='sniper' in the DB. Build a lookup
        # of exchange symbols that belong to sniper trades so signal-based
        # max_positions only counts signal positions.
        sniper_symbols_result = await db.execute(
            select(Trade.symbol).where(
                Trade.exchange == "bitget",
                Trade.status == "open",
                Trade.trade_side == "open",
                Trade.source == "sniper",
            )
        )
        sniper_symbols = {
            LiveTradeEngine._bitget_symbol(s) for s in sniper_symbols_result.scalars().all()
        }
        signal_position_count = sum(
            1 for p in open_positions
            if LiveTradeEngine._bitget_symbol(p.get("symbol", "")) not in sniper_symbols
        )
        sniper_position_count = open_count - signal_position_count

        total_exposure = sum(
            LiveTradeEngine._extract_position_margin(p) for p in open_positions
        )

        # ── Pick up recent actionable signals ──
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

        # Deduplicate: most recent per symbol
        seen: set = set()
        unique_signals = []
        for sig in sig_rows:
            if sig.symbol not in seen:
                seen.add(sig.symbol)
                unique_signals.append(sig)

        signal_ids = [sig.id for sig in unique_signals]
        consumed_signal_ids = set()
        if signal_ids:
            consumed_rows = await db.execute(
                select(Trade.signal_id).where(
                    Trade.exchange == "bitget",
                    Trade.signal_id.in_(signal_ids),
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
                "profit_runner": profit_runner_results,
                "orders_sl_tp_added": orders_backfilled,
                "orders_placed": [],
                "signals_analyzed": 0,
                "balance": round(available_balance, 2),
                "open_positions": open_count,
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
            action = sig.action.value
            price = sig.price or 0
            confidence = sig.confidence or 0

            try:
                indicators = json.loads(sig.indicators) if sig.indicators else {}
            except (json.JSONDecodeError, TypeError):
                indicators = {}

            min_conf = getattr(s, "min_confidence", 0.90) or 0.90
            if confidence < min_conf:
                logger.info(f"[LIVE] Skipping {symbol} {action}: low confidence {confidence:.2f} < {min_conf}")
                sig.status = SignalStatus.IGNORED
                continue

            side = "buy" if action == "buy" else "sell"
            hold_side = LiveTradeEngine._hold_side_for_order(side)
            bitget_symbol = LiveTradeEngine._bitget_symbol(symbol)

            same_direction_position = next(
                (
                    p for p in open_positions
                    if LiveTradeEngine._bitget_symbol(p.get("symbol", "")) == bitget_symbol
                    and (p.get("holdSide", "") or "").lower() == hold_side
                ),
                None,
            )
            is_dca = False
            if same_direction_position:
                # DCA: allow up to 3 additional entries on existing position (4 total)
                dca_result = await db.execute(
                    select(func.count(Trade.id)).where(
                        Trade.exchange == "bitget",
                        Trade.symbol == symbol,
                        Trade.side == side,
                        Trade.trade_side == "open",
                        Trade.status == "open",
                    )
                )
                dca_count = dca_result.scalar() or 0
                if dca_count >= 4:  # 1 original + 3 DCA
                    logger.info(
                        f"[LIVE] Max DCA entries reached ({dca_count}/4) for {symbol}; skipping"
                    )
                    sig.status = SignalStatus.IGNORED
                    continue
                is_dca = True
                logger.info(
                    f"[LIVE] DCA entry {dca_count + 1}/4 for {symbol} {hold_side}"
                )

                # DCA price gap check — ensure price has moved enough since last entry
                try:
                    last_entry = await db.execute(
                        select(Trade.entry_price).where(
                            Trade.exchange == "bitget",
                            Trade.symbol == symbol,
                            Trade.side == side,
                            Trade.trade_side == "open",
                            Trade.status == "open",
                        ).order_by(Trade.created_at.desc()).limit(1)
                    )
                    last_price = last_entry.scalar()
                    if last_price and price > 0:
                        gap_pct = abs(price - last_price) / last_price * 100
                        if gap_pct < min_entry_gap:
                            logger.info(
                                f"[LIVE] DCA gap too small for {symbol}: "
                                f"{gap_pct:.2f}% < {min_entry_gap:.1f}% min; skipping"
                            )
                            sig.status = SignalStatus.IGNORED
                            continue
                except Exception as e:
                    logger.warning(f"[LIVE] DCA gap check failed (non-blocking): {e}")

            opposite_position = next(
                (
                    p for p in open_positions
                    if LiveTradeEngine._bitget_symbol(p.get("symbol", "")) == bitget_symbol
                    and (p.get("holdSide", "") or "").lower() != hold_side
                ),
                None,
            )
            is_new_position = opposite_position is None and not is_dca

            if is_new_position and signal_position_count >= max_positions:
                logger.info(
                    f"[LIVE] Max signal positions reached ({signal_position_count}/{max_positions}, "
                    f"+{sniper_position_count} sniper); "
                    f"skipping {symbol} {action}"
                )
                sig.status = SignalStatus.IGNORED
                continue

            # ── AI Agent Trade Validation ──
            # Skip AI validation for close signals from position reviews (AI already decided)
            is_position_review_close = False
            try:
                sig_raw = json.loads(sig.raw_data) if sig.raw_data else {}
                if sig_raw.get("source") == "position_review":
                    is_position_review_close = True
                    logger.info(
                        f"[LIVE] 🤖 Position review close signal for {symbol} — "
                        f"skipping AI re-validation (reason: {sig_raw.get('reasoning', 'N/A')[:100]})"
                    )
            except (json.JSONDecodeError, TypeError):
                pass

            try:
                from app.core.config import settings as app_settings
                live_ai_enabled = app_settings.ENABLE_AI_AGENTS and getattr(s, "enable_ai", True)
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
                        "available_balance": round(available_balance, 2),
                        "total_exposure": round(total_exposure, 2),
                        "max_exposure": max_exposure,
                        "is_dca": is_dca,
                    }
                    trade_validation = None
                    if live_ai_enabled:
                        from app.agents.orchestrator import AgentOrchestrator
                        trade_validation = await AgentOrchestrator.validate_trade(
                            db,
                            symbol=symbol,
                            signal=_trade_signal,
                            position_context=_pos_context,
                            timeframe=effective_timeframe,
                            auto_trade_ai_provider=getattr(s, "auto_trade_ai_provider", "orchestrator"),
                            tradingagents_llm_provider=getattr(s, "tradingagents_llm_provider", "openai"),
                            tradingagents_deep_think_llm=getattr(s, "tradingagents_deep_think_llm", "gpt-5.4"),
                            tradingagents_quick_think_llm=getattr(s, "tradingagents_quick_think_llm", "gpt-5.4-mini"),
                            tradingagents_backend_url=getattr(s, "tradingagents_backend_url", None),
                            tradingagents_max_debate_rounds=getattr(s, "tradingagents_max_debate_rounds", 2),
                            tradingagents_max_risk_discuss_rounds=getattr(s, "tradingagents_max_risk_discuss_rounds", 2),
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
                            f"[LIVE] 🤖 {'Custom' if trade_validation.get('custom_agents') else 'AI'} agents REJECTED trade {action} {symbol}: "
                            f"{trade_validation.get('reasoning', 'N/A')}"
                        )
                        sig.status = SignalStatus.IGNORED
                        continue
                    elif trade_validation and trade_validation.get("approved"):
                        logger.info(
                            f"[LIVE] 🤖 {'Custom' if trade_validation.get('custom_agents') else 'AI'} agents APPROVED trade {action} {symbol}"
                        )
            except Exception as e:
                logger.warning(f"[LIVE] AI trade validation failed (non-blocking): {e}")

            # ── Position sizing — use fixed margin_size_usdt ──
            risk_amount = min(margin_size, available_balance, max_pos_size)

            if is_new_position and total_exposure + risk_amount > max_exposure:
                logger.info(
                    f"[LIVE] Exposure limit reached ({total_exposure:.0f}+{risk_amount:.0f}"
                    f" > {max_exposure:.0f}); skipping {symbol}"
                )
                sig.status = SignalStatus.IGNORED
                continue

            if symbol not in _leverage_cache:
                try:
                    _, pair_max_lever = await connector.get_max_leverage(symbol)
                    if isinstance(pair_max_lever, (int, float)) and 1 <= pair_max_lever <= 200:
                        _leverage_cache[symbol] = min(int(s.auto_trade_leverage or 10), int(pair_max_lever))
                    else:
                        _leverage_cache[symbol] = s.auto_trade_leverage or 10
                except Exception:
                    _leverage_cache[symbol] = s.auto_trade_leverage or 10

            leverage = _leverage_cache[symbol]
            limit_price = SimulationEngine._smart_limit_price(price, side, indicators)

            # ── Determine limit vs market order ──
            # Fetch current market price to decide order type
            try:
                ticker = await connector.get_ticker(symbol)
                current_market_price = float(ticker.get("last") or ticker.get("close") or 0)
            except Exception:
                current_market_price = 0.0

            # If signal entry is better than market → limit order; otherwise → market order
            # BUY: entry below market = favorable → limit; entry at/above market → market
            # SELL: entry above market = favorable → limit; entry at/below market → market
            use_limit = False
            if current_market_price > 0 and limit_price > 0:
                if side == "buy" and limit_price < current_market_price * 0.999:
                    use_limit = True
                elif side == "sell" and limit_price > current_market_price * 1.001:
                    use_limit = True

            order_type = "limit" if use_limit else "market"
            order_price = str(smart_round(limit_price, limit_price)) if use_limit else None

            if is_new_position or is_dca:
                notional = risk_amount * leverage
                amount = notional / price if price > 0 else 0
            else:
                amount = LiveTradeEngine._extract_position_amount(opposite_position)
                risk_amount = LiveTradeEngine._extract_position_margin(opposite_position)
            if amount <= 0:
                continue

            # Smart stop-loss
            try:
                ohlcv = await connector.get_ohlcv(
                    symbol=symbol, timeframe=effective_timeframe, limit=200,
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

            # ── Place the order ──
            trade_side = "open" if (is_new_position or is_dca) else "close"
            try:
                execution_price = (
                    LiveTradeEngine._extract_position_price(opposite_position)
                    if opposite_position else limit_price
                )
                pnl = float(opposite_position.get("unrealizedPL", 0) or 0) if opposite_position else None

                if dry_run:
                    if is_new_position or is_dca:
                        if is_new_position:
                            open_count += 1
                        total_exposure += risk_amount
                        available_balance = max(0.0, available_balance - risk_amount)
                    else:
                        open_count = max(0, open_count - 1)
                        total_exposure = max(0.0, total_exposure - risk_amount)
                        available_balance += risk_amount + float(pnl or 0)

                    orders_placed.append({
                        "symbol": symbol,
                        "side": side,
                        "amount": round(amount, 6),
                        "price": execution_price,
                        "leverage": leverage,
                        "order_id": None,
                        "trade_side": trade_side,
                        "order_type": order_type,
                        "sl": sl_data.get("stop_loss"),
                        "tp": sl_data.get("take_profit"),
                        "dry_run": True,
                    })
                    logger.info(
                        f"[LIVE][DRY-RUN] Planned {trade_side} {side} {amount:.6f} {symbol} "
                        f"@ {execution_price:.6f} ({order_type}) | leverage={leverage}x | margin={margin_mode}"
                    )
                    continue

                logger.info(
                    f"[LIVE] Placing {trade_side} {side} {amount:.6f} {symbol} "
                    f"@ {order_type}{' ' + str(order_price) if order_price else ''} | leverage={leverage}x | margin={margin_mode}"
                )
                order_result = await connector.create_futures_order(
                    symbol=bitget_symbol,
                    margin_coin="USDT",
                    side=side,
                    order_type=order_type,
                    size=str(round(amount, 6)),
                    price=order_price,
                    margin_mode=margin_mode,
                    leverage=leverage,
                    trade_side=trade_side,
                    stop_loss=None,
                    take_profit=None,
                )
                order_id = order_result.get("orderId", "")

                # ── Place explicit TPSL plan orders for open-side trades ──
                if is_new_position or is_dca:
                    new_sl = sl_data.get("stop_loss")
                    new_tp = sl_data.get("take_profit")
                    if new_sl:
                        try:
                            await connector.place_tpsl_order(
                                symbol=bitget_symbol,
                                margin_coin="USDT",
                                plan_type="loss_plan",
                                trigger_price=new_sl,
                                hold_side=hold_side,
                                size=str(round(amount, 6)),
                            )
                            logger.info(
                                f"[LIVE] SL placed for {symbol} {hold_side}: {new_sl}"
                            )
                        except Exception as e:
                            logger.error(
                                f"[LIVE] Failed to place SL on exchange for {symbol} "
                                f"(saved to DB for internal monitoring): "
                                + str(e).replace("{", "{{").replace("}", "}}")
                            )
                    if new_tp:
                        try:
                            await connector.place_tpsl_order(
                                symbol=bitget_symbol,
                                margin_coin="USDT",
                                plan_type="profit_plan",
                                trigger_price=new_tp,
                                hold_side=hold_side,
                                size=str(round(amount, 6)),
                            )
                            logger.info(
                                f"[LIVE] TP placed for {symbol} {hold_side}: {new_tp}"
                            )
                        except Exception as e:
                            logger.error(
                                f"[LIVE] Failed to place TP on exchange for {symbol} "
                                f"(saved to DB for internal monitoring): "
                                + str(e).replace("{", "{{").replace("}", "}}")
                            )

                if is_new_position or is_dca:
                    db.add(
                        Trade(
                            exchange="bitget",
                            exchange_order_id=order_id,
                            signal_id=sig.id,
                            symbol=symbol,
                            side=side,
                            trade_side="open",
                            order_type=order_type,
                            amount=amount,
                            price=execution_price,
                            stop_loss=sl_data.get("stop_loss"),
                            take_profit=sl_data.get("take_profit"),
                            margin_mode=margin_mode,
                            leverage=leverage,
                            status="open",
                            raw_response=json.dumps({
                                "order": order_result,
                                "planned_entry_price": limit_price,
                                "market_price": current_market_price,
                                "order_type": order_type,
                            }),
                        )
                    )
                else:
                    tracked_result = await db.execute(
                        select(Trade)
                        .where(
                            Trade.exchange == "bitget",
                            Trade.symbol == symbol,
                            Trade.status == "open",
                            Trade.side != side,
                        )
                        .order_by(Trade.created_at.desc())
                    )
                    tracked_open = tracked_result.scalars().first()
                    if tracked_open:
                        # Look up actual fill for real PnL/exit price
                        actual_pnl = pnl
                        actual_exit = execution_price
                        try:
                            import asyncio as _aio
                            await _aio.sleep(0.3)
                            fill = await connector.lookup_close_fill(
                                symbol=symbol,
                                hold_side=LiveTradeEngine._hold_side_for_order(tracked_open.side),
                            )
                            if fill and fill["exit_price"] > 0:
                                actual_pnl = fill["pnl"]
                                actual_exit = fill["exit_price"]
                        except Exception:
                            pass

                        tracked_open.status = "closed"
                        tracked_open.closed_at = _utcnow()
                        tracked_open.average_price = actual_exit
                        tracked_open.filled_amount = amount
                        tracked_open.pnl = actual_pnl
                        try:
                            previous_raw = json.loads(tracked_open.raw_response) if tracked_open.raw_response else {}
                        except (TypeError, ValueError):
                            previous_raw = {"open": tracked_open.raw_response}
                        tracked_open.raw_response = json.dumps({
                            **previous_raw,
                            "close": order_result,
                            "close_signal_id": sig.id,
                        })

                    db.add(
                        Trade(
                            exchange="bitget",
                            exchange_order_id=order_id,
                            signal_id=sig.id,
                            symbol=symbol,
                            side=side,
                            trade_side="close",
                            order_type=order_type,
                            amount=amount,
                            price=actual_exit if tracked_open else execution_price,
                            filled_amount=amount,
                            average_price=actual_exit if tracked_open else execution_price,
                            pnl=actual_pnl if tracked_open else pnl,
                            margin_mode=margin_mode,
                            leverage=leverage,
                            status="closed",
                            raw_response=json.dumps(order_result),
                            closed_at=_utcnow(),
                        )
                    )

                sig.status = SignalStatus.EXECUTED
                sig.processed_at = _utcnow()

                if is_new_position or is_dca:
                    if is_new_position:
                        open_count += 1
                    total_exposure += risk_amount
                    available_balance = max(0.0, available_balance - risk_amount)
                else:
                    open_count = max(0, open_count - 1)
                    total_exposure = max(0.0, total_exposure - risk_amount)
                    available_balance += risk_amount + float(pnl or 0)
                    s.total_trades = (s.total_trades or 0) + 1
                    if float(pnl or 0) >= 0:
                        s.winning_trades = (s.winning_trades or 0) + 1
                    else:
                        s.losing_trades = (s.losing_trades or 0) + 1

                orders_placed.append({
                    "symbol": symbol,
                    "side": side,
                    "amount": round(amount, 6),
                    "price": execution_price,
                    "leverage": leverage,
                    "order_id": order_id,
                    "trade_side": trade_side,
                    "sl": sl_data.get("stop_loss"),
                    "tp": sl_data.get("take_profit"),
                })

                logger.info(
                    f"[LIVE] ✅ Order placed: {side} {amount:.6f} {symbol} "
                    f"orderId={order_id}"
                )

            except Exception as e:
                err_str = str(e)[:500]
                err_safe = err_str.replace("{", "{{").replace("}", "}}")
                logger.error(f"[LIVE] ❌ Order failed for {symbol}: {err_safe}")
                sig.status = SignalStatus.FAILED
                sig.error_message = err_str

        await db.commit()

        return {
            "skipped": False,
            "dry_run": dry_run,
            "sl_tp_closed": closed,
            "sl_tp_backfilled": backfilled,
            "profit_runner": profit_runner_results,
            "orders_sl_tp_added": orders_backfilled,
            "orders_placed": orders_placed,
            "signals_analyzed": signals_processed,
            "balance": round(available_balance, 2),
            "open_positions": open_count,
            "timeframe": effective_timeframe,
            "max_positions": max_positions,
        }

    # ── Execute Single Signal ──────────────────────────────

    @staticmethod
    async def execute_signal(db: AsyncSession, signal_id: int) -> Dict[str, Any]:
        """
        Execute a single signal on live, using the same logic as auto_trade_cycle:
        - Reads leverage, margin mode, risk %, etc. from LiveTradeSettings
        - Determines limit vs market based on current price vs signal entry
        - Calculates smart SL/TP
        - Places order with correct leverage
        - Records Trade in DB
        """
        s = await LiveTradeEngine.get_or_create_settings(db)
        dry_run = bool(s.dry_run)

        if not settings.ENABLE_AUTO_TRADING and not dry_run:
            return {"error": "ENABLE_AUTO_TRADING is disabled in config"}

        connector = cast(
            Optional[BitgetConnector],
            exchange_manager.get_exchange(SupportedExchange.BITGET),
        )
        if not connector:
            return {"error": "Bitget connector not available"}

        # Fetch the signal
        sig = (
            await db.execute(select(Signal).where(Signal.id == signal_id))
        ).scalar_one_or_none()
        if not sig:
            return {"error": f"Signal {signal_id} not found"}

        symbol = sig.symbol
        action = sig.action.value
        price = sig.price or 0
        confidence = sig.confidence or 0

        try:
            indicators = json.loads(sig.indicators) if sig.indicators else {}
        except (json.JSONDecodeError, TypeError):
            indicators = {}

        side = "buy" if action == "buy" else "sell"
        hold_side = LiveTradeEngine._hold_side_for_order(side)
        bitget_symbol = LiveTradeEngine._bitget_symbol(symbol)

        risk_pct = s.auto_trade_risk_pct or 1.0
        margin_mode = s.auto_trade_margin_mode or "crossed"
        effective_timeframe = s.auto_trade_timeframe or "1h"
        max_pos_size = s.max_position_size_usdt or 500.0
        max_exposure = s.max_total_exposure_usdt or 5000.0
        margin_size = getattr(s, "margin_size_usdt", 10.0) or 10.0

        # ── Get exchange state ──
        try:
            pos_data = await connector.get_futures_positions()
        except Exception as e:
            return {"error": f"Failed to fetch positions: {e}"}

        open_positions = [
            p for p in (pos_data or [])
            if LiveTradeEngine._extract_position_amount(p) > 0
        ]

        try:
            bal_data = await connector.get_futures_balance()
            available_balance = LiveTradeEngine._sum_available_margin(bal_data)
        except Exception as e:
            return {"error": f"Failed to fetch balance: {e}"}

        # ── Check for existing position ──
        same_direction = next(
            (
                p for p in open_positions
                if LiveTradeEngine._bitget_symbol(p.get("symbol", "")) == bitget_symbol
                and (p.get("holdSide", "") or "").lower() == hold_side
            ),
            None,
        )
        is_dca = False
        if same_direction:
            # DCA: allow up to 3 additional entries on existing position (4 total)
            dca_result = await db.execute(
                select(func.count(Trade.id)).where(
                    Trade.exchange == "bitget",
                    Trade.symbol == symbol,
                    Trade.side == side,
                    Trade.trade_side == "open",
                    Trade.status == "open",
                )
            )
            dca_count = dca_result.scalar() or 0
            if dca_count >= 4:  # 1 original + 3 DCA
                return {"error": f"Max DCA entries reached ({dca_count}/4) for {symbol}"}
            is_dca = True
            logger.info(f"[LIVE][EXECUTE] DCA entry {dca_count + 1}/4 for {symbol} {hold_side}")

        opposite_position = next(
            (
                p for p in open_positions
                if LiveTradeEngine._bitget_symbol(p.get("symbol", "")) == bitget_symbol
                and (p.get("holdSide", "") or "").lower() != hold_side
            ),
            None,
        )
        is_new_position = opposite_position is None and not is_dca

        # ── Leverage (from settings, capped by exchange max) ──
        try:
            _, pair_max_lever = await connector.get_max_leverage(symbol)
            if isinstance(pair_max_lever, (int, float)) and 1 <= pair_max_lever <= 200:
                leverage = min(int(s.auto_trade_leverage or 10), int(pair_max_lever))
            else:
                leverage = s.auto_trade_leverage or 10
        except Exception:
            leverage = s.auto_trade_leverage or 10

        # ── Smart limit price ──
        limit_price = SimulationEngine._smart_limit_price(price, side, indicators)

        # ── Determine limit vs market order ──
        try:
            ticker = await connector.get_ticker(symbol)
            current_market_price = float(
                ticker.get("last") or ticker.get("close") or 0
            )
        except Exception:
            current_market_price = 0.0

        use_limit = False
        if current_market_price > 0 and limit_price > 0:
            if side == "buy" and limit_price < current_market_price * 0.999:
                use_limit = True
            elif side == "sell" and limit_price > current_market_price * 1.001:
                use_limit = True

        order_type = "limit" if use_limit else "market"
        order_price = str(smart_round(limit_price, limit_price)) if use_limit else None

        # ── Position sizing — use fixed margin_size_usdt ──
        if is_new_position or is_dca:
            risk_amount = min(margin_size, available_balance, max_pos_size)
            notional = risk_amount * leverage
            amount = notional / price if price > 0 else 0
        else:
            amount = LiveTradeEngine._extract_position_amount(opposite_position)
            risk_amount = LiveTradeEngine._extract_position_margin(opposite_position)

        if amount <= 0:
            return {"error": "Calculated order amount is zero"}

        # ── Smart stop-loss / take-profit ──
        try:
            ohlcv = await connector.get_ohlcv(
                symbol=symbol, timeframe=effective_timeframe, limit=200,
            )
            sl_data = SmartStopLoss.calculate(
                ohlcv,
                "long" if side == "buy" else "short",
                limit_price,
            )
        except Exception:
            sl_data = {
                "stop_loss": SmartStopLoss.from_pct(
                    limit_price, "long" if side == "buy" else "short"
                ),
                "take_profit": limit_price * (1.04 if side == "buy" else 0.96),
                "sl_type": "pct",
            }

        # ── Place the order ──
        trade_side = "open" if (is_new_position or is_dca) else "close"
        execution_price = (
            LiveTradeEngine._extract_position_price(opposite_position)
            if opposite_position
            else limit_price
        )
        pnl = (
            float(opposite_position.get("unrealizedPL", 0) or 0)
            if opposite_position
            else None
        )

        if dry_run:
            logger.info(
                f"[LIVE][DRY-RUN][EXECUTE] Planned {trade_side} {side} "
                f"{amount:.6f} {symbol} @ {execution_price:.6f} ({order_type}) "
                f"| leverage={leverage}x | margin={margin_mode} "
                f"| SL={sl_data.get('stop_loss')} | TP={sl_data.get('take_profit')}"
            )
            return {
                "success": True,
                "dry_run": True,
                "symbol": symbol,
                "side": side,
                "amount": round(amount, 6),
                "price": execution_price,
                "leverage": leverage,
                "order_type": order_type,
                "trade_side": trade_side,
                "sl": sl_data.get("stop_loss"),
                "tp": sl_data.get("take_profit"),
            }

        logger.info(
            f"[LIVE][EXECUTE] Placing {trade_side} {side} {amount:.6f} {symbol} "
            f"@ {order_type}{' ' + str(order_price) if order_price else ''} "
            f"| leverage={leverage}x | margin={margin_mode}"
            f" | SL={sl_data.get('stop_loss')} | TP={sl_data.get('take_profit')}"
        )
        order_result = await connector.create_futures_order(
            symbol=bitget_symbol,
            margin_coin="USDT",
            side=side,
            order_type=order_type,
            size=str(round(amount, 6)),
            price=order_price,
            margin_mode=margin_mode,
            leverage=leverage,
            trade_side=trade_side,
            stop_loss=None,
            take_profit=None,
        )
        order_id = order_result.get("orderId", "")

        # ── Place explicit TPSL plan orders for open-side trades ──
        if is_new_position or is_dca:
            new_sl = sl_data.get("stop_loss")
            new_tp = sl_data.get("take_profit")
            if new_sl:
                try:
                    await connector.place_tpsl_order(
                        symbol=bitget_symbol,
                        margin_coin="USDT",
                        plan_type="loss_plan",
                        trigger_price=new_sl,
                        hold_side=hold_side,
                        size=str(round(amount, 6)),
                    )
                    logger.info(
                        f"[LIVE][EXECUTE] SL placed for {symbol} {hold_side}: {new_sl}"
                    )
                except Exception as e:
                    logger.error(
                        f"[LIVE][EXECUTE] Failed to place SL on exchange for {symbol} "
                        f"(saved to DB for internal monitoring): "
                        + str(e).replace("{", "{{").replace("}", "}}")
                    )
            if new_tp:
                try:
                    await connector.place_tpsl_order(
                        symbol=bitget_symbol,
                        margin_coin="USDT",
                        plan_type="profit_plan",
                        trigger_price=new_tp,
                        hold_side=hold_side,
                        size=str(round(amount, 6)),
                    )
                    logger.info(
                        f"[LIVE][EXECUTE] TP placed for {symbol} {hold_side}: {new_tp}"
                    )
                except Exception as e:
                    logger.error(
                        f"[LIVE][EXECUTE] Failed to place TP on exchange for {symbol} "
                        f"(saved to DB for internal monitoring): "
                        + str(e).replace("{", "{{").replace("}", "}}")
                    )

        if is_new_position or is_dca:
            db.add(
                Trade(
                    exchange="bitget",
                    exchange_order_id=order_id,
                    signal_id=sig.id,
                    symbol=symbol,
                    side=side,
                    trade_side="open",
                    order_type=order_type,
                    amount=amount,
                    price=execution_price,
                    stop_loss=sl_data.get("stop_loss"),
                    take_profit=sl_data.get("take_profit"),
                    margin_mode=margin_mode,
                    leverage=leverage,
                    status="open",
                    raw_response=json.dumps({
                        "order": order_result,
                        "planned_entry_price": limit_price,
                        "market_price": current_market_price,
                        "order_type": order_type,
                    }),
                )
            )
        else:
            tracked_result = await db.execute(
                select(Trade)
                .where(
                    Trade.exchange == "bitget",
                    Trade.symbol == symbol,
                    Trade.status == "open",
                    Trade.side != side,
                )
                .order_by(Trade.created_at.desc())
            )
            tracked_open = tracked_result.scalars().first()
            if tracked_open:
                # Look up actual fill for real PnL/exit price
                actual_pnl2 = pnl
                actual_exit2 = execution_price
                try:
                    import asyncio as _aio2
                    await _aio2.sleep(0.3)
                    fill2 = await connector.lookup_close_fill(
                        symbol=symbol,
                        hold_side=LiveTradeEngine._hold_side_for_order(tracked_open.side),
                    )
                    if fill2 and fill2["exit_price"] > 0:
                        actual_pnl2 = fill2["pnl"]
                        actual_exit2 = fill2["exit_price"]
                except Exception:
                    pass

                tracked_open.status = "closed"
                tracked_open.closed_at = _utcnow()
                tracked_open.average_price = actual_exit2
                tracked_open.filled_amount = amount
                tracked_open.pnl = actual_pnl2
                try:
                    previous_raw = (
                        json.loads(tracked_open.raw_response)
                        if tracked_open.raw_response
                        else {}
                    )
                except (TypeError, ValueError):
                    previous_raw = {"open": tracked_open.raw_response}
                tracked_open.raw_response = json.dumps({
                    **previous_raw,
                    "close": order_result,
                    "close_signal_id": sig.id,
                })

            db.add(
                Trade(
                    exchange="bitget",
                    exchange_order_id=order_id,
                    signal_id=sig.id,
                    symbol=symbol,
                    side=side,
                    trade_side="close",
                    order_type=order_type,
                    amount=amount,
                    price=actual_exit2 if tracked_open else execution_price,
                    filled_amount=amount,
                    average_price=actual_exit2 if tracked_open else execution_price,
                    pnl=actual_pnl2 if tracked_open else pnl,
                    margin_mode=margin_mode,
                    leverage=leverage,
                    status="closed",
                    raw_response=json.dumps(order_result),
                    closed_at=_utcnow(),
                )
            )

        sig.status = SignalStatus.EXECUTED
        sig.processed_at = _utcnow()

        # Update stats for closed positions
        if not is_new_position and not is_dca:
            final_pnl = float((actual_pnl2 if tracked_open else pnl) or 0) if not (is_new_position or is_dca) else 0
            s.total_trades = (s.total_trades or 0) + 1
            if final_pnl >= 0:
                s.winning_trades = (s.winning_trades or 0) + 1
            else:
                s.losing_trades = (s.losing_trades or 0) + 1

        await db.commit()

        logger.info(
            f"[LIVE][EXECUTE] ✅ Order placed: {side} {amount:.6f} {symbol} "
            f"orderId={order_id} leverage={leverage}x "
            f"SL={sl_data.get('stop_loss')} TP={sl_data.get('take_profit')}"
        )

        return {
            "success": True,
            "dry_run": False,
            "symbol": symbol,
            "side": side,
            "amount": round(amount, 6),
            "price": execution_price,
            "leverage": leverage,
            "order_id": order_id,
            "order_type": order_type,
            "trade_side": trade_side,
            "sl": sl_data.get("stop_loss"),
            "tp": sl_data.get("take_profit"),
            "margin_mode": margin_mode,
        }
