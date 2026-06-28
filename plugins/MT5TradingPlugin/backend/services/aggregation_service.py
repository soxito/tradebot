"""
MT5 Aggregation Service

Builds weighted snapshots across account groups.
Caches results for fast dashboard reads.
"""
from typing import Optional, List, Dict
from datetime import datetime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from plugins.MT5TradingPlugin.backend.models import (
    MT5Account, MT5AccountGroup, MT5AccountGroupMember,
    MT5AccountSnapshot, MT5Position, MT5Order,
)


class MT5AggregationService:
    """
    Aggregation across multiple MT5 accounts within a group.
    
    Weighted sums: each member has a weight (default 1.0).
    This enables flexible portfolio views (e.g. 50% weight for demo accounts).
    """

    @staticmethod
    async def get_group_accounts(
        db: AsyncSession, group_id: int
    ) -> List[Dict]:
        """Get all accounts in a group with their weights."""
        members = await db.execute(
            select(MT5AccountGroupMember).where(
                MT5AccountGroupMember.group_id == group_id
            )
        )
        member_list = members.scalars().all()

        result = []
        for m in member_list:
            account = await db.get(MT5Account, m.account_id)
            if account:
                result.append({
                    "account": account,
                    "weight": m.weight,
                })
        return result

    @staticmethod
    async def build_group_snapshot(
        db: AsyncSession, group_id: int
    ) -> Optional[MT5AccountSnapshot]:
        """
        Build a weighted snapshot of the entire group.
        
        Weighted aggregation: each metric = sum(account.metric * member.weight)
        This allows partial allocation views without affecting individual accounts.
        """
        members = await MT5AggregationService.get_group_accounts(db, group_id)
        if not members:
            return None

        total_equity = 0.0
        total_balance = 0.0
        total_margin = 0.0
        total_free_margin = 0.0
        total_floating_pnl = 0.0

        for m in members:
            acc = m["account"]
            w = m["weight"]
            total_equity += acc.equity * w
            total_balance += acc.balance * w
            total_margin += acc.margin * w
            total_free_margin += acc.free_margin * w
            total_floating_pnl += acc.floating_pnl * w

        snapshot = MT5AccountSnapshot(
            group_id=group_id,
            time=datetime.utcnow(),
            equity=round(total_equity, 2),
            balance=round(total_balance, 2),
            margin=round(total_margin, 2),
            free_margin=round(total_free_margin, 2),
            floating_pnl=round(total_floating_pnl, 2),
            margin_level=round(total_equity / total_margin * 100, 2) if total_margin > 0 else None,
        )
        db.add(snapshot)
        await db.commit()
        await db.refresh(snapshot)
        return snapshot

    @staticmethod
    async def build_account_snapshot(
        db: AsyncSession, account_id: int
    ) -> Optional[MT5AccountSnapshot]:
        """Take a point-in-time snapshot of a single account."""
        account = await db.get(MT5Account, account_id)
        if not account:
            return None

        snapshot = MT5AccountSnapshot(
            account_id=account_id,
            time=datetime.utcnow(),
            equity=account.equity,
            balance=account.balance,
            margin=account.margin,
            free_margin=account.free_margin,
            floating_pnl=account.floating_pnl,
            margin_level=account.margin_level,
        )
        db.add(snapshot)
        await db.commit()
        await db.refresh(snapshot)
        return snapshot

    @staticmethod
    async def get_group_positions(
        db: AsyncSession, group_id: int
    ) -> List:
        """Get all open positions across the group."""
        members = await MT5AggregationService.get_group_accounts(db, group_id)
        account_ids = [m["account"].id for m in members]
        if not account_ids:
            return []

        result = await db.execute(
            select(MT5Position).where(MT5Position.account_id.in_(account_ids))
        )
        return result.scalars().all()

    @staticmethod
    async def get_group_orders(
        db: AsyncSession, group_id: int
    ) -> List:
        """Get all pending orders across the group."""
        members = await MT5AggregationService.get_group_accounts(db, group_id)
        account_ids = [m["account"].id for m in members]
        if not account_ids:
            return []

        result = await db.execute(
            select(MT5Order).where(MT5Order.account_id.in_(account_ids))
        )
        return result.scalars().all()

    @staticmethod
    async def get_equity_curve(
        db: AsyncSession,
        group_id: Optional[int] = None,
        account_id: Optional[int] = None,
        limit: int = 200,
    ) -> List[Dict]:
        """
        Get equity curve data points for TradingView chart.
        
        Returns windowed results (last N points) — charts must not hang.
        Format: [{time: "2024-01-01T00:00:00", value: 12345.67}, ...]
        """
        query = select(MT5AccountSnapshot).order_by(MT5AccountSnapshot.time.desc()).limit(limit)
        if group_id:
            query = query.where(MT5AccountSnapshot.group_id == group_id)
        elif account_id:
            query = query.where(MT5AccountSnapshot.account_id == account_id)

        result = await db.execute(query)
        snapshots = result.scalars().all()

        # Reverse to chronological order for chart
        return [
            {"time": s.time.isoformat(), "value": s.equity}
            for s in reversed(snapshots)
        ]
