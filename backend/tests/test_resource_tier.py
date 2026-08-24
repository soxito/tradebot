"""Tests for the resource-tier config scaling (Settings.apply_resource_tier)."""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.core import resource_tier as rt


def _settings(profile: str | None = None, **env_overrides) -> Settings:
    """Build a Settings without reading the repo .env, optionally with a tier."""
    kwargs = dict(_env_file=None)
    if profile is not None:
        env_overrides.setdefault("TRADEBOT_PROFILE", profile)
    kwargs.update(env_overrides)
    return Settings(**kwargs)


@pytest.mark.parametrize(
    "profile,expected_mult",
    [("minimal", 6.0), ("low", 4.0), ("medium", 2.0), ("high", 1.0), ("ultra", 1.0)],
)
def test_interval_multiplier_matches_tier(profile, expected_mult):
    assert rt.interval_multiplier(profile) == expected_mult


def test_minimal_scales_intervals_and_disables_noncritical():
    s = _settings("minimal")
    assert s.PERF_TIER == "minimal"
    # base 900 * 6
    assert s.RESEARCH_LOOP_INTERVAL_SECONDS == 5400
    # base 5 * 6
    assert s.PRICE_TICK_INTERVAL_SECONDS == 30
    # non-critical loop below min tier is disabled
    assert s.AUTO_START_SIGNAL_RESEARCH_QUEUE is False
    assert s.AUTO_START_PRICE_TICK_LOOP is False
    # concurrency capped
    assert s.SIGNAL_RESEARCH_CONCURRENCY == 1
    assert s.PRICE_TICK_MAX_SYMBOLS == 8


def test_high_leaves_defaults_untouched():
    s = _settings("high")
    assert s.PERF_TIER == "high"
    assert s.RESEARCH_LOOP_INTERVAL_SECONDS == 900
    assert s.PRICE_TICK_INTERVAL_SECONDS == 5
    assert s.AUTO_START_SIGNAL_RESEARCH_QUEUE is True


def test_no_tier_falls_back_up_to_high():
    s = _settings()  # no profile, no tier env
    assert s.PERF_TIER == "high"
    assert s.RESEARCH_LOOP_INTERVAL_SECONDS == 900


def test_pinned_env_value_survives_tier_scaling():
    # Explicitly set an interval — the validator must not override it.
    s = _settings("minimal", RESEARCH_LOOP_INTERVAL_SECONDS=120)
    assert s.RESEARCH_LOOP_INTERVAL_SECONDS == 120


def test_critical_intervals_scale_but_task_stays_autostartable():
    s = _settings("minimal")
    # position_monitor is critical: its interval scales (900*6) but it is never
    # gated off by tier.
    assert s.POSITION_MONITOR_INTERVAL_SECONDS == 5400
    assert rt.should_autostart("position_monitor", "minimal") is True
    assert rt.should_autostart("telegram_monitor", "minimal") is True
    assert rt.should_autostart("telegram_bot_polling", "minimal") is True


def test_signal_research_queue_gated_by_tier():
    assert rt.should_autostart("signal_research_queue", "low") is False
    assert rt.should_autostart("signal_research_queue", "medium") is True


def test_learning_and_vault_are_critical_always_on():
    # ML/knowledge tasks must run at every tier and never be tier-gated.
    for task_id in ("jarvis_learning", "vault_sync"):
        assert rt.is_critical(task_id) is True
        for tier in ("minimal", "low", "medium", "high", "ultra"):
            assert rt.should_autostart(task_id, tier) is True


def test_learning_and_vault_autostart_survive_minimal_profile():
    s = _settings("minimal")
    # Their intervals scale (x6) but the loops are never disabled.
    assert s.AUTO_START_VAULT_SYNC_LOOP is True
    assert s.AUTO_START_JARVIS_LEARNING_LOOP is True
    assert s.VAULT_SYNC_INTERVAL_SECONDS == 300 * 6
    assert s.JARVIS_LEARNING_INTERVAL_SECONDS == 900 * 6
