"""
Copy-trading supervisor — the trading-room's control layer over copy profiles.

When ``RoomSettings.manage_copy_profiles`` is on, this runs every copy-worker
cycle and exercises FULL CONTROL over all copy trading accounts:

  - reviews each enabled profile's performance (PnL, win rate, drawdown)
  - disables any profile whose paper-balance drawdown breaches the limit
  - disables followers stuck in persistent error states
  - logs every action as an AgentDecision so the room shows what it did

Every action is conservative: the supervisor only ever turns things OFF.
Re-enabling is always a human decision.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

SUPERVISOR_AGENT_NAME = "Copy Supervisor"
SUPERVISOR_AGENT_ROLE = "copy_supervisor"


async def _get_room_settings(db: AsyncSession):
    from app.models.database import RoomSettings

    result = await db.execute(select(RoomSettings).where(RoomSettings.id == 1))
    return result.scalar_one_or_none()


async def _log_decision(
    db: AsyncSession, *, symbol: str, action: str,
    confidence: float, reasoning: str, market_data: Optional[str] = None,
) -> None:
    """Persist a supervisor decision so the room/UI can show what happened."""
    from app.models.database import AgentDecision

    db.add(AgentDecision(
        agent_id=0,
        agent_name=SUPERVISOR_AGENT_NAME,
        agent_role=SUPERVISOR_AGENT_ROLE,
        symbol=symbol[:50],
        action=action,
        confidence=confidence,
        reasoning=reasoning,
        market_data=market_data,
        ai_called=False,  # deterministic rules, not an LLM call
        session_id=f"copy-supervisor",
    ))
    await db.commit()
    logger.info(f"[CopySupervisor] {symbol}: {action} — {reasoning}")


async def _profile_stats(db: AsyncSession, profile_id: int) -> dict:
    """Closed-trade stats + peak-to-now drawdown for one profile."""
    from plugins.MT5TradingPlugin.backend.services.copy_sim_engine import (
        MT5CopySimEngine,
    )

    perf = await MT5CopySimEngine.get_profile_performance(db, profile_id)

    # Drawdown needs the trade sequence, not just totals.
    from plugins.MT5TradingPlugin.backend.models import MT5CopySimTrade, CopySimStatus

    result = await db.execute(
        select(MT5CopySimTrade)
        .where(
            MT5CopySimTrade.copy_profile_id == profile_id,
            MT5CopySimTrade.status == CopySimStatus.CLOSED,
        )
        .order_by(MT5CopySimTrade.exit_time.asc().nulls_last())
        .limit(500)
    )
    trades = result.scalars().all()

    equity = 10000.0  # default starting paper balance
    peak = equity
    max_dd = 0.0
    for t in trades:
        equity += t.pnl_sim or 0.0
        peak = max(peak, equity)
        if peak > 0:
            dd = (peak - equity) / peak * 100.0
            max_dd = max(max_dd, dd)

    perf["drawdown_pct"] = round(max_dd, 2)
    return perf


async def run_once(db: AsyncSession) -> dict:
    """One supervision pass. Cheap no-op when the gate is off."""
    settings = await _get_room_settings(db)
    if settings is None or not getattr(settings, "manage_copy_profiles", False):
        return {"supervised": False}

    from plugins.MT5TradingPlugin.backend.models import MT5CopyProfile

    result = await db.execute(select(MT5CopyProfile))
    profiles = result.scalars().all()
    actions: list[dict] = []
    dd_limit = float(getattr(settings, "copy_max_drawdown_pct", 20.0))

    for profile in profiles:
        label = f"profile:{profile.name}"

        # ── Rule 1: drawdown breach → disable ──
        if profile.enabled:
            stats = await _profile_stats(db, profile.id)
            if stats["total_trades"] >= 5 and stats["drawdown_pct"] > dd_limit:
                profile.enabled = False
                await db.commit()
                await _log_decision(
                    db, symbol=label, action="disable_profile", confidence=0.9,
                    reasoning=(
                        f"Disabled copy profile '{profile.name}': drawdown "
                        f"{stats['drawdown_pct']}% exceeds limit {dd_limit}% "
                        f"({stats['total_trades']} closed trades, PnL {stats['total_pnl']}). "
                        "Human review required to re-enable."
                    ),
                    market_data=str(stats),
                )
                actions.append({"profile": profile.name, "action": "disabled_drawdown"})
                continue

            # ── Rule 2: poor win rate over a meaningful sample → disable ──
            if stats["total_trades"] >= 20 and stats["win_rate"] < 25.0 and stats["total_pnl"] < 0:
                profile.enabled = False
                await db.commit()
                await _log_decision(
                    db, symbol=label, action="disable_profile", confidence=0.75,
                    reasoning=(
                        f"Disabled copy profile '{profile.name}': win rate "
                        f"{stats['win_rate']}% over {stats['total_trades']} trades with "
                        f"net PnL {stats['total_pnl']} — strategy not viable."
                    ),
                    market_data=str(stats),
                )
                actions.append({"profile": profile.name, "action": "disabled_winrate"})
                continue

            # Healthy profile → periodic review note (every pass is too noisy;
            # only log when something notable changed is ideal, but a light note
            # keeps the room transparent without spamming).
            if stats["total_trades"] > 0 and stats["drawdown_pct"] > dd_limit * 0.6:
                await _log_decision(
                    db, symbol=label, action="hold", confidence=0.6,
                    reasoning=(
                        f"Watching '{profile.name}': drawdown {stats['drawdown_pct']}% "
                        f"approaching limit {dd_limit}%."
                    ),
                    market_data=str(stats),
                )

        # ── Rule 3: followers stuck in error state → disable follower ──
        from plugins.MT5TradingPlugin.backend.models import MT5CopyFollower

        fres = await db.execute(
            select(MT5CopyFollower).where(MT5CopyFollower.copy_profile_id == profile.id)
        )
        for follower in fres.scalars().all():
            stale_error = (
                follower.last_error
                and follower.last_sync_at
                and follower.last_sync_at < datetime.utcnow() - timedelta(minutes=30)
            )
            if follower.enabled and stale_error:
                follower.enabled = False
                await db.commit()
                await _log_decision(
                    db, symbol=label, action="disable_follower", confidence=0.8,
                    reasoning=(
                        f"Disabled follower account #{follower.account_id} on "
                        f"'{profile.name}': execution errors persisting >30min "
                        f"({follower.last_error})."
                    ),
                )
                actions.append({
                    "profile": profile.name,
                    "account_id": follower.account_id,
                    "action": "follower_disabled_errors",
                })

    return {"supervised": True, "actions": actions}
