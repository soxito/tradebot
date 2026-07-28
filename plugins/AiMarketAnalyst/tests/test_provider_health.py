"""Unit tests for the live provider health registry used by the analysis router."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plugins.AiMarketAnalyst.backend.services.provider_health import (  # noqa: E402
    ProviderHealth,
    _FAILURE_DECAY_S,
    _p95,
)


@pytest.fixture()
def health() -> ProviderHealth:
    return ProviderHealth()


def test_p95_nearest_rank():
    assert _p95([]) == 0.0
    assert _p95([100.0]) == 100.0
    # 20 samples 1..20 -> ceil(0.95*20) = 19th smallest = 19
    assert _p95([float(i) for i in range(1, 21)]) == 19.0


def test_unknown_provider_scores_perfect(health):
    """A newly configured provider must be tried, not ranked last forever."""
    assert health.health_score("brand-new") == 1.0
    assert health.success_rate("brand-new") == 1.0


def test_success_rate_tracks_outcomes(health):
    for _ in range(3):
        health.mark_start("p")
        health.mark_success("p", 100.0)
    health.mark_start("p")
    health.mark_failure("p")
    assert health.success_rate("p") == pytest.approx(0.75)


def test_failure_lowers_score_and_recovers_with_age(health):
    health.mark_start("p")
    health.mark_failure("p")
    fresh = health.health_score("p")

    # Backdate the failure past the decay window; score must recover.
    health._stat("p").last_failure_ts = time.time() - (_FAILURE_DECAY_S + 1)
    aged = health.health_score("p")
    assert aged > fresh


def test_slow_provider_ranks_below_fast_provider(health):
    for _ in range(10):
        health.mark_start("fast")
        health.mark_success("fast", 200.0)
        health.mark_start("slow")
        health.mark_success("slow", 15_000.0)

    assert health.health_score("fast") > health.health_score("slow")
    assert health.rank(["slow", "fast"]) == ["fast", "slow"]


def test_rank_prefers_reliable_over_unreliable(health):
    for _ in range(10):
        health.mark_start("good")
        health.mark_success("good", 500.0)
    for _ in range(10):
        health.mark_start("bad")
        health.mark_failure("bad")

    assert health.rank(["bad", "good"]) == ["good", "bad"]


def test_rank_ties_preserve_caller_order(health):
    """With no history, ranking must fall back to the DB priority order."""
    assert health.rank(["a", "b", "c"]) == ["a", "b", "c"]


def test_in_flight_counter_increments_and_clears(health):
    health.mark_start("p")
    health.mark_start("p")
    assert health.in_flight("p") == 2
    health.mark_success("p", 10.0)
    assert health.in_flight("p") == 1
    health.mark_failure("p")
    assert health.in_flight("p") == 0
    # Never goes negative even on an unbalanced release.
    health.mark_failure("p")
    assert health.in_flight("p") == 0


@pytest.mark.asyncio
async def test_idle_providers_excludes_busy_and_excluded(health, monkeypatch):
    # No Redis in the test environment — force the local-only path.
    monkeypatch.setattr(health, "remote_in_flight", _zero)

    health.mark_start("busy")           # in-flight, not idle
    health.mark_start("done")
    health.mark_success("done", 5.0)    # released, idle again

    idle = await health.idle_providers(
        ["busy", "done", "primary", "spare"], exclude=["primary"]
    )
    assert idle == ["done", "spare"]


async def _zero(_label: str) -> int:
    return 0


def test_snapshot_reports_all_terms(health):
    health.mark_start("p")
    health.mark_success("p", 250.0)
    snap = {s.label: s for s in health.snapshot(["p"])}["p"]
    assert snap.samples == 1
    assert snap.success_rate == 1.0
    assert snap.p95_latency_ms == 250.0
    assert snap.in_flight == 0
    assert snap.last_failure_age_s is None
