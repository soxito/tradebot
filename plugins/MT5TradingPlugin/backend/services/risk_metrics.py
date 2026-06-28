"""
MT5 Risk Metrics Service

Computes R:R ratios, exposure heatmaps, and PnL distribution.
Results are cached (short TTL) for dashboard performance.
"""
from typing import List, Dict, Optional
from collections import defaultdict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from plugins.MT5TradingPlugin.backend.models import MT5Position, MT5Deal, MT5Order
from plugins.MT5TradingPlugin.backend.schemas import (
    RiskRatioResponse, ExposureHeatmapCell, PnLHeatmapCell,
    ChartPriceLine, ChartMarker, MT5OverlayResponse,
)


WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class MT5RiskMetricsService:
    """
    Computes risk overlay data for positions, orders, and deal history.
    
    R:R calculation:
    - Risk = |entry - SL| in price units
    - Reward = |TP - entry| in price units
    - R:R = Reward / Risk
    
    Heatmaps:
    - Exposure: notional by symbol × side
    - PnL: average PnL distribution by hour and weekday
    """

    @staticmethod
    def calc_rr(entry: float, sl: Optional[float], tp: Optional[float]) -> Dict:
        """Calculate risk/reward for a single position."""
        risk_pips = abs(entry - sl) if sl else None
        reward_pips = abs(tp - entry) if tp else None
        rr_ratio = None
        if risk_pips and reward_pips and risk_pips > 0:
            rr_ratio = round(reward_pips / risk_pips, 2)
        return {
            "risk_pips": round(risk_pips, 5) if risk_pips else None,
            "reward_pips": round(reward_pips, 5) if reward_pips else None,
            "rr_ratio": rr_ratio,
        }

    @staticmethod
    async def get_positions_rr(
        db: AsyncSession, account_ids: List[int]
    ) -> List[RiskRatioResponse]:
        """Compute R:R for all open positions across given accounts."""
        result = await db.execute(
            select(MT5Position).where(MT5Position.account_id.in_(account_ids))
        )
        positions = result.scalars().all()
        rr_list = []
        for p in positions:
            metrics = MT5RiskMetricsService.calc_rr(p.price_open, p.sl, p.tp)
            rr_list.append(RiskRatioResponse(
                position_id=p.id,
                symbol=p.symbol,
                side=p.side.value if hasattr(p.side, 'value') else str(p.side),
                entry=p.price_open,
                sl=p.sl,
                tp=p.tp,
                **metrics,
            ))
        return rr_list

    @staticmethod
    async def get_exposure_heatmap(
        db: AsyncSession, account_ids: List[int]
    ) -> List[ExposureHeatmapCell]:
        """Build symbol × side exposure heatmap from open positions."""
        result = await db.execute(
            select(MT5Position).where(MT5Position.account_id.in_(account_ids))
        )
        positions = result.scalars().all()
        cells = []
        for p in positions:
            # Notional = volume × current price (or open price as fallback)
            price = p.price_current or p.price_open
            notional = p.volume * price
            cells.append(ExposureHeatmapCell(
                symbol=p.symbol,
                side=p.side.value if hasattr(p.side, 'value') else str(p.side),
                notional=round(notional, 2),
                margin_used=0,  # would need margin info from account
                account_id=p.account_id,
            ))
        return cells

    @staticmethod
    async def get_pnl_heatmap(
        db: AsyncSession, account_ids: List[int], bucket: str = "hour"
    ) -> List[PnLHeatmapCell]:
        """
        Build PnL heatmap from deal history.
        
        bucket="hour": group by hour (0-23)
        bucket="weekday": group by weekday (Mon-Sun)
        """
        result = await db.execute(
            select(MT5Deal).where(
                MT5Deal.account_id.in_(account_ids),
                MT5Deal.symbol.isnot(None),
                MT5Deal.mt5_time.isnot(None),
            )
        )
        deals = result.scalars().all()

        buckets: Dict[str, List[float]] = defaultdict(list)
        for d in deals:
            if d.mt5_time is None:
                continue
            if bucket == "hour":
                key = f"{d.mt5_time.hour:02d}:00"
            else:
                key = WEEKDAY_NAMES[d.mt5_time.weekday()]
            buckets[key].append(d.profit)

        cells = []
        for key, profits in sorted(buckets.items()):
            wins = sum(1 for p in profits if p > 0)
            cells.append(PnLHeatmapCell(
                bucket=key,
                avg_pnl=round(sum(profits) / len(profits), 2) if profits else 0,
                win_rate=round(wins / len(profits) * 100, 1) if profits else 0,
                trade_count=len(profits),
            ))
        return cells

    # ── Chart Overlay Builder ──────────────────────────────

    @staticmethod
    async def build_overlay(
        db: AsyncSession, account_ids: List[int], symbol: Optional[str] = None
    ) -> MT5OverlayResponse:
        """
        Build chart overlay data for TradingView Lightweight Charts.
        
        Returns orders as dashed price lines, positions as solid lines,
        SL/TP as colored lines, and recent deals as markers.
        
        Overlays are delta-based: the frontend should diff against previous
        state and only add/remove changed items (never full redraw).
        """
        overlay = MT5OverlayResponse()

        # Orders as dashed blue lines
        order_q = select(MT5Order).where(MT5Order.account_id.in_(account_ids))
        if symbol:
            order_q = order_q.where(MT5Order.symbol == symbol)
        orders = (await db.execute(order_q)).scalars().all()

        for o in orders:
            is_buy = "buy" in (o.order_type.value if hasattr(o.order_type, 'value') else str(o.order_type))
            overlay.orders.append(ChartPriceLine(
                price=o.price,
                color="#2196F3" if is_buy else "#FF5722",
                lineWidth=1,
                lineStyle=2,  # dashed
                title=f"{'BUY' if is_buy else 'SELL'} Limit {o.volume}",
            ))
            if o.sl:
                overlay.sl_tp_lines.append(ChartPriceLine(
                    price=o.sl, color="#F44336", lineWidth=1, lineStyle=1,
                    title=f"SL {o.sl}",
                ))
            if o.tp:
                overlay.sl_tp_lines.append(ChartPriceLine(
                    price=o.tp, color="#4CAF50", lineWidth=1, lineStyle=1,
                    title=f"TP {o.tp}",
                ))

        # Positions as solid lines
        pos_q = select(MT5Position).where(MT5Position.account_id.in_(account_ids))
        if symbol:
            pos_q = pos_q.where(MT5Position.symbol == symbol)
        positions = (await db.execute(pos_q)).scalars().all()

        for p in positions:
            is_buy = (p.side.value if hasattr(p.side, 'value') else str(p.side)) == "buy"
            overlay.positions.append(ChartPriceLine(
                price=p.price_open,
                color="#2196F3" if is_buy else "#FF5722",
                lineWidth=2,
                lineStyle=0,  # solid
                title=f"{'LONG' if is_buy else 'SHORT'} {p.volume} @ {p.price_open}",
            ))
            if p.sl:
                overlay.sl_tp_lines.append(ChartPriceLine(
                    price=p.sl, color="#F44336", lineWidth=1, lineStyle=1,
                    title=f"SL {p.sl}",
                ))
            if p.tp:
                overlay.sl_tp_lines.append(ChartPriceLine(
                    price=p.tp, color="#4CAF50", lineWidth=1, lineStyle=1,
                    title=f"TP {p.tp}",
                ))

        # Recent deals as chart markers (cap at 200 for performance)
        deal_q = (
            select(MT5Deal)
            .where(MT5Deal.account_id.in_(account_ids), MT5Deal.symbol.isnot(None))
            .order_by(MT5Deal.mt5_time.desc())
            .limit(200)
        )
        if symbol:
            deal_q = deal_q.where(MT5Deal.symbol == symbol)
        deals = (await db.execute(deal_q)).scalars().all()

        for d in deals:
            if d.mt5_time is None:
                continue
            is_buy = (d.deal_type.value if hasattr(d.deal_type, 'value') else str(d.deal_type)) == "buy"
            overlay.execution_markers.append(ChartMarker(
                time=d.mt5_time.isoformat(),
                position="belowBar" if is_buy else "aboveBar",
                color="#4CAF50" if d.profit >= 0 else "#F44336",
                shape="arrowUp" if is_buy else "arrowDown",
                text=f"{'B' if is_buy else 'S'} {d.volume or ''} P:{d.profit:+.2f}",
            ))

        return overlay
