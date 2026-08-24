"""
Resource tier — single source of truth for how hard the app works.

``start.py`` derives a tier from the machine (RAM, cores) and passes it to the
backend via the env contract (``TRADEBOT_TIER`` / ``TRADEBOT_PROFILE``). Every
loop interval and autostart flag scales from here, so a 16 GB laptop stops
swapping while a 64 GB workstation runs everything at full cadence.

Critical tasks (open-position monitoring, live auto-trade, the Telegram signal
monitor and bot polling) are **never** disabled by tier — tiering may only slow
them. Pausing a position monitor with open live positions is a financial risk,
and a paused Telegram monitor means signals silently stop arriving.
"""
from __future__ import annotations

from typing import Dict, List, Optional

# Ordered weakest → strongest.
TIER_ORDER: List[str] = ["minimal", "low", "medium", "high", "ultra"]

# Multiply every base loop interval by this. Weak machines poll far less often.
INTERVAL_MULTIPLIER: Dict[str, float] = {
    "minimal": 6.0,
    "low": 4.0,
    "medium": 2.0,
    "high": 1.0,
    "ultra": 1.0,
}

_DEFAULT_TIER = "high"


def normalize_tier(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    v = str(value).strip().lower()
    return v if v in TIER_ORDER else None


def resolve_tier(profile: Optional[str] = None, tier: Optional[str] = None) -> str:
    """Resolve the effective tier. Falls back *up* to ``high`` — never guess low
    and silently disable features."""
    return normalize_tier(profile) or normalize_tier(tier) or _DEFAULT_TIER


def tier_index(tier: str) -> int:
    try:
        return TIER_ORDER.index(normalize_tier(tier) or _DEFAULT_TIER)
    except ValueError:
        return TIER_ORDER.index(_DEFAULT_TIER)


def tier_at_least(tier: str, minimum: str) -> bool:
    return tier_index(tier) >= tier_index(minimum)


def interval_multiplier(tier: str) -> float:
    return INTERVAL_MULTIPLIER.get(normalize_tier(tier) or _DEFAULT_TIER, 1.0)


def lower_tier(tier: str) -> str:
    """One step weaker (used by the memory watchdog to throttle under pressure)."""
    idx = max(0, tier_index(tier) - 1)
    return TIER_ORDER[idx]


# Per-task policy. ``min_tier`` gates autostart; ``critical`` tasks ignore the
# gate and the watchdog. ``category`` groups tasks for the watchdog and the UI.
# This mirrors the low-memory profile table in the overhaul plan.
TASK_TIER_POLICY: Dict[str, Dict[str, object]] = {
    # ── Core scheduler loops ──────────────────────────────────────────────
    "scheduler": {"min_tier": "minimal", "critical": False, "category": "core"},
    "sentiment_pipeline": {"min_tier": "low", "critical": False, "category": "research"},
    "signals_pipeline": {"min_tier": "minimal", "critical": False, "category": "core"},
    "price_tick": {"min_tier": "low", "critical": False, "category": "realtime"},
    "signal_research_queue": {"min_tier": "medium", "critical": False, "category": "research"},
    "research_loop": {"min_tier": "low", "critical": False, "category": "research"},
    # Learning + knowledge tasks are critical: they feed the app's ML/knowledge
    # base and must run at every tier — never tier-gated, never watchdog-paused.
    "jarvis_learning": {"min_tier": "minimal", "critical": True, "category": "learning"},
    "vault_sync": {"min_tier": "minimal", "critical": True, "category": "enrichment"},
    "pair_catalog_sync": {"min_tier": "minimal", "critical": False, "category": "enrichment"},
    # ── Trading loops — critical carve-outs ───────────────────────────────
    "position_monitor": {"min_tier": "minimal", "critical": True, "category": "trading"},
    "live_auto_trade": {"min_tier": "minimal", "critical": True, "category": "trading"},
    "sim_auto_trade": {"min_tier": "low", "critical": False, "category": "trading"},
    "sniper": {"min_tier": "low", "critical": True, "category": "trading"},
    "pump_monitor": {"min_tier": "low", "critical": False, "category": "trading"},
    # ── Plugin loops ──────────────────────────────────────────────────────
    "telegram_monitor": {"min_tier": "minimal", "critical": True, "category": "trading"},
    "telegram_bot_polling": {"min_tier": "minimal", "critical": True, "category": "trading"},
    "mt5_scalp_bot": {"min_tier": "low", "critical": False, "category": "trading"},
    "mt5_auto_manage": {"min_tier": "low", "critical": False, "category": "trading"},
    "paul_subconscious": {"min_tier": "low", "critical": False, "category": "learning"},
}


def is_critical(task_id: str) -> bool:
    policy = TASK_TIER_POLICY.get(task_id)
    return bool(policy and policy.get("critical"))


def task_min_tier(task_id: str) -> str:
    policy = TASK_TIER_POLICY.get(task_id)
    return str(policy.get("min_tier", "minimal")) if policy else "minimal"


def task_category(task_id: str) -> str:
    policy = TASK_TIER_POLICY.get(task_id)
    return str(policy.get("category", "other")) if policy else "other"


def should_autostart(task_id: str, tier: str) -> bool:
    """Whether a task should autostart at the given tier. Critical tasks always
    do; everything else requires ``tier >= min_tier``."""
    if is_critical(task_id):
        return True
    return tier_at_least(tier, task_min_tier(task_id))
