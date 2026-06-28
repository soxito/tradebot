"""Usage aggregation for the AI provider + agent token dashboards.

Reads the append-only ``AIUsageRecord`` log and the provider call counters to
produce the numbers shown on the /telegram-signals provider tab and the /agents
page: per-provider tokens, per-agent tokens, monthly remaining, and the
Headroom compression savings used by the Intelligence page.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from plugins.AiMarketAnalyst.backend.models import AILLMProvider, AIUsageRecord


def _month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _remaining(used: int | None, limit: int | None) -> int | None:
    if limit is None:
        return None
    return max(0, limit - int(used or 0))


async def _tokens_since(db: AsyncSession, since: datetime) -> int:
    val = await db.scalar(
        select(func.coalesce(func.sum(AIUsageRecord.total_tokens), 0)).where(AIUsageRecord.ts >= since)
    )
    return int(val or 0)


async def provider_usage(db: AsyncSession) -> dict[str, Any]:
    """Per-provider + overall usage with monthly remaining (call + token based)."""
    now = datetime.utcnow()
    month0 = _month_start(now)
    day0 = _day_start(now)

    res = await db.execute(
        select(AILLMProvider).order_by(AILLMProvider.priority.asc(), AILLMProvider.id.asc())
    )
    providers = list(res.scalars().all())

    # token sums per provider for the current month/day
    month_tokens = dict(
        (pid, int(tok or 0))
        for pid, tok in (
            await db.execute(
                select(AIUsageRecord.provider_id, func.sum(AIUsageRecord.total_tokens))
                .where(AIUsageRecord.ts >= month0)
                .group_by(AIUsageRecord.provider_id)
            )
        ).all()
    )

    items: list[dict[str, Any]] = []
    tot_daily = tot_monthly = tot_calls = tot_errors = 0
    tot_month_tokens = 0
    tot_monthly_limit: int | None = 0
    for p in providers:
        ptok = month_tokens.get(p.id, 0)
        tot_daily += p.daily_calls or 0
        tot_monthly += p.monthly_calls or 0
        tot_calls += p.total_calls or 0
        tot_errors += p.total_errors or 0
        tot_month_tokens += ptok
        if p.monthly_limit is None:
            tot_monthly_limit = None
        elif tot_monthly_limit is not None:
            tot_monthly_limit += p.monthly_limit
        items.append({
            "id": p.id,
            "label": p.label,
            "enabled": p.enabled,
            "status": p.status,
            "daily_calls": p.daily_calls or 0,
            "daily_limit": p.daily_limit,
            "daily_remaining": _remaining(p.daily_calls, p.daily_limit),
            "monthly_calls": p.monthly_calls or 0,
            "monthly_limit": p.monthly_limit,
            "monthly_remaining": _remaining(p.monthly_calls, p.monthly_limit),
            "month_tokens": ptok,
            "total_calls": p.total_calls or 0,
            "total_errors": p.total_errors or 0,
            "last_model_used": p.last_model_used,
        })

    return {
        "providers": items,
        "totals": {
            "daily_calls": tot_daily,
            "monthly_calls": tot_monthly,
            "monthly_limit": tot_monthly_limit,
            "monthly_remaining": _remaining(tot_monthly, tot_monthly_limit),
            "month_tokens": tot_month_tokens,
            "today_tokens": await _tokens_since(db, day0),
            "total_calls": tot_calls,
            "total_errors": tot_errors,
        },
    }


async def agent_usage(db: AsyncSession) -> dict[str, Any]:
    """Per-agent token + call usage for the current month (for the /agents page)."""
    now = datetime.utcnow()
    month0 = _month_start(now)

    rows = (
        await db.execute(
            select(
                AIUsageRecord.agent_role,
                func.count(AIUsageRecord.id),
                func.coalesce(func.sum(AIUsageRecord.total_tokens), 0),
                func.coalesce(func.sum(AIUsageRecord.prompt_tokens), 0),
                func.coalesce(func.sum(AIUsageRecord.completion_tokens), 0),
            )
            .where(AIUsageRecord.ts >= month0, AIUsageRecord.agent_role.isnot(None))
            .group_by(AIUsageRecord.agent_role)
            .order_by(func.sum(AIUsageRecord.total_tokens).desc())
        )
    ).all()

    agents = [
        {
            "agent_role": role,
            "calls": int(calls or 0),
            "total_tokens": int(total or 0),
            "prompt_tokens": int(prompt or 0),
            "completion_tokens": int(completion or 0),
        }
        for role, calls, total, prompt, completion in rows
    ]
    return {
        "agents": agents,
        "month_total_tokens": sum(a["total_tokens"] for a in agents),
        "month_total_calls": sum(a["calls"] for a in agents),
    }


async def headroom_stats(db: AsyncSession, days: int = 30) -> dict[str, Any]:
    """Aggregate Headroom compression savings for the Intelligence page."""
    since = datetime.utcnow() - timedelta(days=days)
    row = (
        await db.execute(
            select(
                func.count(AIUsageRecord.id),
                func.coalesce(func.sum(AIUsageRecord.orig_chars), 0),
                func.coalesce(func.sum(AIUsageRecord.comp_chars), 0),
            ).where(AIUsageRecord.ts >= since)
        )
    ).one()
    calls, orig, comp = int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)
    saved = max(0, orig - comp)
    pct = (saved / orig * 100) if orig else 0.0
    # ~4 chars/token heuristic for an approximate token-savings figure
    approx_tokens_saved = round(saved / 4)
    return {
        "window_days": days,
        "calls": calls,
        "orig_chars": orig,
        "comp_chars": comp,
        "chars_saved": saved,
        "reduction_pct": round(pct, 1),
        "approx_tokens_saved": approx_tokens_saved,
    }
