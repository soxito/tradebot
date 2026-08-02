"""
AI Market Analyst — three-tier analysis router for /mt5-live.

Contract: this module NEVER raises and NEVER returns a partial success. It
either hands back validated model output, or reports ``ok=False`` so the caller
can drop to its own deterministic floor.

  tier "primary"  — the single healthiest enabled provider (live health score:
                    rolling success rate + p95 latency + last-failure age)
  tier "cascade"  — the remaining providers, health-ranked, tried in order

Every attempt is hard-capped at ``hard_timeout`` seconds (default 12), and the
whole cascade is capped at ``total_budget`` seconds so the caller always answers
well inside the frontend's 60 s HTTP timeout. Providers are reused, not
reimplemented: provider rows, keys, circuit breaker, quota caps and the HTTP
call itself all come from ``ai_router``.

A response that fails the caller's schema validator is treated exactly like a
transport error — record the failure and cascade to the next provider. Schema
failure is never a crash.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from plugins.AiMarketAnalyst.backend.models import AIUsageRecord
from plugins.AiMarketAnalyst.backend.services import ai_router
from plugins.AiMarketAnalyst.backend.services.provider_health import provider_health

# Hard per-attempt cap required by the analysis path (the shared
# ``ai_router._TIMEOUT`` of 40 s stays untouched for every other caller).
HARD_TIMEOUT_S = 12.0
# Whole-cascade wall budget. Frontend timeout is 60 s (services/api.ts).
TOTAL_BUDGET_S = 36.0

TIER_PRIMARY = "primary"
TIER_CASCADE = "cascade"
TIER_DETERMINISTIC = "deterministic"


async def rank_providers(db: AsyncSession) -> List[Any]:
    """Enabled, credentialed, non-tripped, non-capped providers, healthiest first."""
    settings = await ai_router.get_router_settings(db)
    providers = await ai_router.get_enabled_providers(db)

    now = datetime.utcnow()
    usable = []
    for p in providers:
        if not p.api_key or not p.base_url:
            continue
        if ai_router._cb_open(p.id):
            continue
        ai_router._reset_usage_windows(p, now)
        if ai_router._is_capped(p, settings.reserve_pct):
            continue
        usable.append(p)
    try:
        await db.commit()  # persist any usage-window rollovers
    except Exception:  # noqa: BLE001
        await db.rollback()

    ranked_labels = provider_health.rank([p.label for p in usable])
    by_label: Dict[str, Any] = {}
    for p in usable:
        by_label.setdefault(p.label, p)
    return [by_label[lbl] for lbl in ranked_labels if lbl in by_label]


async def primary_label(db: AsyncSession) -> Optional[str]:
    """Label of the provider that would serve the next analysis call, if any."""
    ranked = await rank_providers(db)
    return ranked[0].label if ranked else None


async def analyze_with_cascade(
    db: AsyncSession,
    messages: List[Dict[str, str]],
    *,
    validator: Callable[[Any], Optional[Any]],
    temperature: float = 0.15,
    max_tokens: int = 900,
    json_mode: bool = True,
    hard_timeout: float = HARD_TIMEOUT_S,
    total_budget: float = TOTAL_BUDGET_S,
    exclude: Iterable[str] = (),
    agent_name: str = "smc_sniper",
    agent_role: str = "market_analyst",
    source: str = "smc_sniper",
) -> Dict[str, Any]:
    """Run `messages` through the health-ranked provider cascade.

    ``validator`` receives the raw model content and returns the validated
    object, or ``None`` to reject it (which cascades to the next provider).

    Returns ``{ok, content, provider_used, tier, model, latency_ms, attempts,
    errors}``. Never raises.
    """
    started = time.monotonic()
    blocked = set(exclude)
    attempts: List[str] = []
    errors: List[str] = []

    try:
        ranked = await rank_providers(db)
    except Exception as exc:  # noqa: BLE001 — DB down must not break analysis
        logger.warning(f"[analysis_router] provider lookup failed: {exc}")
        return {
            "ok": False, "content": None, "provider_used": None,
            "tier": TIER_DETERMINISTIC, "model": None, "latency_ms": 0,
            "attempts": [], "errors": [f"provider lookup failed: {exc}"],
        }

    ranked = [p for p in ranked if p.label not in blocked]
    if not ranked:
        return {
            "ok": False, "content": None, "provider_used": None,
            "tier": TIER_DETERMINISTIC, "model": None, "latency_ms": 0,
            "attempts": [], "errors": ["no enabled provider available"],
        }

    for idx, p in enumerate(ranked):
        elapsed = time.monotonic() - started
        if elapsed >= total_budget:
            errors.append(f"cascade budget {total_budget:.0f}s exhausted")
            break

        tier = TIER_PRIMARY if idx == 0 else TIER_CASCADE
        model = p.default_model or "gpt-4o-mini"
        attempts.append(p.label)
        budget_left = min(hard_timeout, total_budget - elapsed)

        provider_health.mark_start(p.label)
        await provider_health.publish_inflight(p.label, +1)
        call_started = time.monotonic()
        try:
            content, usage, routed_via = await asyncio.wait_for(
                ai_router._call_openai_compatible(
                    base_url=p.base_url,
                    api_key=p.api_key,
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    json_mode=json_mode,
                ),
                timeout=budget_left,
            )
            latency_ms = (time.monotonic() - call_started) * 1000.0

            validated = validator(content)
            if validated is None:
                # Malformed / schema-violating output — same treatment as a
                # transport error: cascade, never crash.
                provider_health.mark_failure(p.label)
                await provider_health.publish_inflight(p.label, -1)
                errors.append(f"{p.label}: schema validation failed")
                await _record_usage(db, p, model, usage, agent_name, agent_role,
                                    source, success=False,
                                    error="schema validation failed")
                continue

            provider_health.mark_success(p.label, latency_ms)
            await provider_health.publish_inflight(p.label, -1)
            await _record_usage(db, p, routed_via or model, usage, agent_name,
                                agent_role, source, success=True)
            return {
                "ok": True,
                "content": validated,
                "provider_used": p.label,
                "tier": tier,
                "model": routed_via or model,
                "latency_ms": round(latency_ms, 1),
                "attempts": attempts,
                "errors": errors,
            }

        except asyncio.TimeoutError:
            provider_health.mark_failure(p.label)
            await provider_health.publish_inflight(p.label, -1)
            errors.append(f"{p.label}: timeout after {budget_left:.1f}s")
            ai_router._cb_trip(p.id)
            await _record_usage(db, p, model, None, agent_name, agent_role,
                                source, success=False, error="timeout")
            continue

        except Exception as exc:  # noqa: BLE001 — any provider error cascades
            provider_health.mark_failure(p.label)
            await provider_health.publish_inflight(p.label, -1)
            msg = str(exc)[:300]
            # 401/403/404/410 will fail identically until a key or URL is fixed.
            # Sitting those out for longer is what lets the cascade reach the
            # providers that CAN answer, instead of spending its budget
            # rediscovering the same misconfiguration on every request.
            fault = ai_router.config_fault_status(exc)
            if fault is not None:
                # A service the vendor has switched off is not a key you can
                # fix. Saying "needs configuring" about a retired endpoint sends
                # people off regenerating tokens that were never the problem.
                if ai_router.is_retired_upstream(exc):
                    errors.append(f"{p.label}: retired upstream (HTTP {fault})")
                    logger.warning(
                        f"[analysis_router] {p.label} reports it has been retired by its "
                        f"provider — no key or URL change will bring it back; use "
                        f"another provider"
                    )
                else:
                    errors.append(f"{p.label}: HTTP {fault} — needs configuring")
                    logger.warning(
                        f"[analysis_router] {p.label} returned {fault}; skipping it for "
                        f"{ai_router._CB_CONFIG_COOLDOWN / 60:.0f} min — check its API key/base URL"
                    )
                ai_router._cb_trip(p.id, ai_router._CB_CONFIG_COOLDOWN)
            else:
                errors.append(f"{p.label}: {msg}")
                ai_router._cb_trip(p.id)
            await _record_usage(db, p, model, None, agent_name, agent_role,
                                source, success=False, error=msg)
            continue

    return {
        "ok": False,
        "content": None,
        "provider_used": None,
        "tier": TIER_DETERMINISTIC,
        "model": None,
        "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
        "attempts": attempts,
        "errors": errors or ["all providers failed"],
    }


async def _record_usage(
    db: AsyncSession,
    provider: Any,
    model: str,
    usage: Optional[Dict[str, int]],
    agent_name: str,
    agent_role: str,
    source: str,
    *,
    success: bool,
    error: Optional[str] = None,
) -> None:
    """Mirror ``db_chat``'s bookkeeping so the AI Providers panel stays accurate.

    Best-effort: a DB problem here must never fail the analysis.
    """
    try:
        if success:
            provider.total_calls = (provider.total_calls or 0) + 1
            provider.daily_calls = (provider.daily_calls or 0) + 1
            provider.monthly_calls = (provider.monthly_calls or 0) + 1
            provider.status = "ok"
            provider.last_error = None
            provider.last_model_used = model
            provider.last_tested_at = datetime.utcnow()
        else:
            provider.total_errors = (provider.total_errors or 0) + 1
            provider.status = "error"
            provider.last_error = (error or "")[:300]

        db.add(AIUsageRecord(
            provider_id=provider.id,
            provider_label=provider.label,
            agent_name=agent_name,
            agent_role=agent_role,
            model=model,
            source=source,
            prompt_tokens=(usage or {}).get("prompt_tokens", 0),
            completion_tokens=(usage or {}).get("completion_tokens", 0),
            total_tokens=(usage or {}).get("total_tokens", 0),
            success=success,
        ))
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[analysis_router] usage record skipped: {exc}")
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
