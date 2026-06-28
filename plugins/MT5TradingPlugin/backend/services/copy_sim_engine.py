"""
MT5 Copy-Trading Simulation Engine

Simulates copying trades from a source account to a paper wallet.
NO real trade execution — paper ledger only.
"""
from typing import Optional, List
from datetime import datetime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from plugins.MT5TradingPlugin.backend.models import (
    MT5Deal, MT5CopyProfile, MT5CopySimTrade, CopySimStatus,
)


class MT5CopySimEngine:
    """
    Simulates copy-trading from a source account/group.
    
    When new deals appear on the source, this engine creates simulated
    follower trades with volume adjusted by the profile's allocation rules.
    
    Allocation modes:
    - fixed_lot: use allocation_value as lot size directly
    - risk_percent: compute lot from risk% of paper equity
    - multiplier: multiply source lot by allocation_value
    
    DISCLAIMER: Simulation only — not financial advice.
    """

    @staticmethod
    def compute_sim_volume(
        profile: MT5CopyProfile,
        source_volume: float,
    ) -> float:
        """Compute simulated lot size based on allocation mode."""
        if profile.allocation_mode == "fixed_lot":
            return profile.allocation_value
        elif profile.allocation_mode == "multiplier":
            return round(source_volume * profile.allocation_value, 2)
        elif profile.allocation_mode == "risk_percent":
            # Risk-based: allocation_value% of paper equity per trade
            risk_amount = profile.paper_equity * (profile.allocation_value / 100)
            # Simplified: assume $10/pip for standard lot
            return round(max(0.01, risk_amount / 1000), 2)
        return 0.01

    @staticmethod
    async def process_new_deals(
        db: AsyncSession, profile: MT5CopyProfile, new_deals: List[MT5Deal]
    ) -> List[MT5CopySimTrade]:
        """
        Process new source deals and create simulated copy trades.
        
        Only processes buy/sell deals (ignores balance/credit/commission).
        Respects max_open_positions and symbol_whitelist constraints.
        """
        if not profile.enabled:
            return []

        created_trades = []

        # Check current open sim positions
        open_count_result = await db.execute(
            select(MT5CopySimTrade).where(
                MT5CopySimTrade.copy_profile_id == profile.id,
                MT5CopySimTrade.status == CopySimStatus.OPEN,
            )
        )
        open_count = len(open_count_result.scalars().all())

        for deal in new_deals:
            # Skip non-trade deals
            if deal.deal_type.value not in ("buy", "sell"):
                continue

            # Check symbol whitelist
            if profile.symbol_whitelist and deal.symbol not in profile.symbol_whitelist:
                continue

            # Check max open positions
            if open_count >= profile.max_open_positions:
                logger.debug(f"[CopySim] Profile {profile.id}: max positions reached")
                break

            sim_volume = MT5CopySimEngine.compute_sim_volume(
                profile, deal.volume or 0.01
            )

            sim_trade = MT5CopySimTrade(
                copy_profile_id=profile.id,
                source_deal_id=deal.id,
                symbol=deal.symbol or "",
                side=deal.deal_type.value,
                qty_sim=sim_volume,
                entry_time=deal.mt5_time,
                entry_price=deal.price or 0,
                status=CopySimStatus.OPEN,
                meta={
                    "source_ticket": deal.mt5_ticket,
                    "source_volume": deal.volume,
                    "allocation_mode": profile.allocation_mode,
                },
            )
            db.add(sim_trade)
            created_trades.append(sim_trade)
            open_count += 1

        if created_trades:
            await db.commit()
            logger.info(f"[CopySim] Profile {profile.id}: created {len(created_trades)} sim trades")

        return created_trades

    @staticmethod
    async def close_sim_trade(
        db: AsyncSession, trade_id: int, exit_price: float
    ) -> Optional[MT5CopySimTrade]:
        """Close a simulated trade with given exit price and compute PnL."""
        trade = await db.get(MT5CopySimTrade, trade_id)
        if not trade or trade.status != CopySimStatus.OPEN:
            return None

        trade.exit_time = datetime.utcnow()
        trade.exit_price = exit_price

        # Simple PnL: (exit - entry) * volume for buy, inverse for sell
        if trade.side == "buy":
            trade.pnl_sim = round((exit_price - (trade.entry_price or 0)) * trade.qty_sim, 2)
        else:
            trade.pnl_sim = round(((trade.entry_price or 0) - exit_price) * trade.qty_sim, 2)

        trade.status = CopySimStatus.CLOSED

        # Update paper balance
        profile = await db.get(MT5CopyProfile, trade.copy_profile_id)
        if profile:
            profile.paper_balance += trade.pnl_sim
            profile.paper_equity = profile.paper_balance  # simplified

        await db.commit()
        return trade

    @staticmethod
    async def get_profile_performance(
        db: AsyncSession, profile_id: int
    ) -> dict:
        """Get summary performance metrics for a copy profile."""
        result = await db.execute(
            select(MT5CopySimTrade).where(
                MT5CopySimTrade.copy_profile_id == profile_id,
                MT5CopySimTrade.status == CopySimStatus.CLOSED,
            )
        )
        closed_trades = result.scalars().all()

        total_pnl = sum(t.pnl_sim for t in closed_trades)
        wins = [t for t in closed_trades if t.pnl_sim > 0]

        return {
            "total_trades": len(closed_trades),
            "total_pnl": round(total_pnl, 2),
            "win_rate": round(len(wins) / len(closed_trades) * 100, 1) if closed_trades else 0,
            "avg_pnl": round(total_pnl / len(closed_trades), 2) if closed_trades else 0,
        }
