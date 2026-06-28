"""
MT5 Trade Replay Service (Backtesting Bridge)

Converts real MT5 deals into a replay run with performance metrics.
Does NOT modify core backtesting code — uses adapter pattern.
"""
from typing import Optional, List
from datetime import datetime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from plugins.MT5TradingPlugin.backend.models import (
    MT5Deal, MT5ReplayRun, MT5ReplayTrade,
    ReplayRunStatus,
)


class MT5ReplayService:
    """
    Converts real MT5 trade history into a replay format with metrics.
    
    Adapter pattern: transforms MT5 deals into standardized ReplayTrade records,
    then computes performance metrics (PnL, drawdown, win rate, Sharpe).
    This allows evaluating real trading performance using backtest-style analytics.
    """

    @staticmethod
    async def create_replay(
        db: AsyncSession,
        user_id: int,
        account_id: Optional[int] = None,
        group_id: Optional[int] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        symbol_filter: Optional[List[str]] = None,
    ) -> MT5ReplayRun:
        """Create a new replay run from MT5 deal history."""
        run = MT5ReplayRun(
            user_id=user_id,
            account_id=account_id,
            group_id=group_id,
            date_from=date_from or datetime(2020, 1, 1),
            date_to=date_to or datetime.utcnow(),
            symbol_filter=symbol_filter,
            status=ReplayRunStatus.QUEUED,
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        return run

    @staticmethod
    async def execute_replay(db: AsyncSession, run_id: int) -> MT5ReplayRun:
        """
        Execute a queued replay run:
        1. Fetch matching deals
        2. Pair entries/exits into trades
        3. Compute metrics
        4. Build equity curve
        """
        run = await db.get(MT5ReplayRun, run_id)
        if not run:
            raise ValueError(f"Replay run {run_id} not found")

        run.status = ReplayRunStatus.RUNNING
        await db.commit()

        try:
            # Build deal query
            query = select(MT5Deal).where(
                MT5Deal.mt5_time.isnot(None),
                MT5Deal.symbol.isnot(None),
            ).order_by(MT5Deal.mt5_time.asc())

            if run.account_id:
                query = query.where(MT5Deal.account_id == run.account_id)

            if run.date_from:
                query = query.where(MT5Deal.mt5_time >= run.date_from)
            if run.date_to:
                query = query.where(MT5Deal.mt5_time <= run.date_to)

            result = await db.execute(query)
            deals = result.scalars().all()

            # Filter by symbols if specified
            if run.symbol_filter:
                deals = [d for d in deals if d.symbol in run.symbol_filter]

            # Convert deals to replay trades
            # Group by position ticket to pair entries with exits
            position_groups = {}
            for d in deals:
                if d.deal_type.value in ("balance", "credit", "commission"):
                    continue
                pos_id = d.mt5_position_ticket or d.mt5_ticket
                if pos_id not in position_groups:
                    position_groups[pos_id] = []
                position_groups[pos_id].append(d)

            trades = []
            for pos_id, pos_deals in position_groups.items():
                if not pos_deals:
                    continue
                entry_deal = pos_deals[0]
                exit_deal = pos_deals[-1] if len(pos_deals) > 1 else None

                total_pnl = sum(d.profit + d.commission + d.swap for d in pos_deals)
                total_fees = sum(abs(d.commission) + abs(d.fee) for d in pos_deals)

                trade = MT5ReplayTrade(
                    replay_run_id=run.id,
                    time=entry_deal.mt5_time,
                    symbol=entry_deal.symbol or "",
                    side=entry_deal.deal_type.value if hasattr(entry_deal.deal_type, 'value') else str(entry_deal.deal_type),
                    qty=entry_deal.volume or 0,
                    entry_price=entry_deal.price or 0,
                    exit_price=exit_deal.price if exit_deal else None,
                    pnl=round(total_pnl, 2),
                    fees=round(total_fees, 2),
                    meta={"position_ticket": pos_id, "deal_count": len(pos_deals)},
                )
                db.add(trade)
                trades.append(trade)

            # Compute performance metrics
            total_pnl = sum(t.pnl for t in trades)
            wins = [t for t in trades if t.pnl > 0]
            win_rate = len(wins) / len(trades) * 100 if trades else 0

            # Build equity curve
            equity = 0.0
            peak = 0.0
            max_dd = 0.0
            equity_curve = []
            for t in trades:
                equity += t.pnl
                peak = max(peak, equity)
                dd = peak - equity
                max_dd = max(max_dd, dd)
                equity_curve.append({
                    "time": t.time.isoformat() if t.time else "",
                    "value": round(equity, 2),
                })

            # Sharpe ratio approximation (daily returns)
            import statistics
            returns = [t.pnl for t in trades]
            sharpe = None
            if len(returns) > 1:
                mean_r = statistics.mean(returns)
                std_r = statistics.stdev(returns)
                if std_r > 0:
                    sharpe = round(mean_r / std_r * (252 ** 0.5), 2)

            run.total_trades = len(trades)
            run.total_pnl = round(total_pnl, 2)
            run.max_drawdown = round(max_dd, 2)
            run.win_rate = round(win_rate, 1)
            run.sharpe_ratio = sharpe
            run.equity_curve = equity_curve
            run.status = ReplayRunStatus.COMPLETED
            await db.commit()

            logger.info(f"[MT5Replay] Run {run.id} completed: "
                       f"{len(trades)} trades, PnL={total_pnl:.2f}, WR={win_rate:.1f}%")
            return run

        except Exception as e:
            run.status = ReplayRunStatus.FAILED
            run.error_message = str(e)
            await db.commit()
            logger.error(f"[MT5Replay] Run {run.id} failed: {e}")
            raise
