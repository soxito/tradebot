"""The per-task model fallback chains JARVIS routes analysis through.

These are the chains `_task_chat` walks in order, so a wrong entry costs a real
attempt against a real provider before anything useful happens.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for path in (str(REPO_ROOT), str(REPO_ROOT / "backend")):
    if path not in sys.path:
        sys.path.insert(0, path)

from app.api.jarvis import _JARVIS_TASK_MODELS, _JARVIS_TASK_LABELS  # noqa: E402

NEMOTRON = "nvidia/nemotron-3-super-120b-a12b"

#: Tasks the desk wants served by Nemotron first, with everything else behind it.
NEMOTRON_PRIMARY_TASKS = ("market_analysis", "news_context", "synthesis", "news_position")


@pytest.mark.parametrize("task", NEMOTRON_PRIMARY_TASKS)
def test_nemotron_leads_the_chain(task):
    provider, _model = _JARVIS_TASK_MODELS[task][0]
    assert provider == "nvidia", (
        f"{_JARVIS_TASK_LABELS[task]} should start on NVIDIA, got {provider}"
    )


@pytest.mark.parametrize("task", sorted(_JARVIS_TASK_MODELS))
def test_every_task_keeps_a_fallback_on_another_provider(task):
    """A chain that is one provider deep has no fallback at all.

    Every provider goes down eventually — rate limits, capacity, retirement —
    so each task needs somewhere else to land.
    """
    chain = _JARVIS_TASK_MODELS[task]
    assert len(chain) >= 2, f"{task} has no fallback"
    assert len({provider for provider, _ in chain}) >= 2, (
        f"{task} falls back only within {chain[0][0]} — one outage takes the task down"
    )


@pytest.mark.parametrize("task", sorted(_JARVIS_TASK_MODELS))
def test_no_chain_routes_through_a_retired_provider(task):
    """GitHub Models answers HTTP 410 on every endpoint — it is gone.

    Leaving it in a chain costs an attempt that can only ever fail.
    """
    providers = {provider for provider, _ in _JARVIS_TASK_MODELS[task]}
    assert "github" not in providers, (
        f"{task} still routes through GitHub Models, which has been retired"
    )


def test_news_position_can_still_reach_a_million_token_window():
    """Nemotron is 128K. Mapping every headline onto every open position can
    exceed that on a big book, and a context overflow is just another provider
    error — so a 1M-context model has to sit behind it to catch the fall."""
    chain = _JARVIS_TASK_MODELS["news_position"]
    assert chain[0][0] == "nvidia"
    assert any(provider == "gemini" for provider, _ in chain[1:]), (
        "news_position has no large-context fallback for an over-long prompt"
    )


def test_every_task_has_a_label_for_the_ui():
    assert set(_JARVIS_TASK_MODELS) == set(_JARVIS_TASK_LABELS)


# ── The primary is NVIDIA's deepest model ────────────────────────────────────

ULTRA = "nvidia/nemotron-3-ultra-550b-a55b"


@pytest.mark.parametrize("task", NEMOTRON_PRIMARY_TASKS)
def test_the_deepest_nemotron_leads_with_the_120b_right_behind_it(task):
    """Ultra 550B first, Super 120B second.

    A 550B is likelier to be at capacity (HTTP 529), and the 120B is the same
    family — verified to return parseable JSON — so it is the cheapest possible
    fall.
    """
    chain = _JARVIS_TASK_MODELS[task]
    assert chain[0] == ("nvidia", ULTRA), (
        f"{_JARVIS_TASK_LABELS[task]} should lead on Nemotron Ultra, got {chain[0]}"
    )
    assert chain[1] == ("nvidia", NEMOTRON), (
        f"{_JARVIS_TASK_LABELS[task]} has no Nemotron Super behind the 550B"
    )


# ── Vault auto-sync ──────────────────────────────────────────────────────────

def test_vault_sync_timestamps_carry_their_timezone():
    """A naive UTC string is read as LOCAL time by the browser.

    That is not cosmetic: on a UTC+2 machine a sync from two minutes ago
    rendered as "2h 2m ago", which makes a perfectly healthy loop look stalled.
    """
    from datetime import datetime

    from plugins.ObsidianKnowledgePlugin.backend.services import sync_orchestrator as so

    stamp = so._utc_now_iso()
    parsed = datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None, f"{stamp!r} has no timezone — JS will read it as local"

    so.record_manual_sync({"written": 3, "skipped": 1, "errors": 0, "duration_ms": 12})
    status = so.get_vault_sync_status()
    assert datetime.fromisoformat(status["last_run"]["at"]).tzinfo is not None
    assert status["last_run"]["trigger"] == "manual"
    assert status["last_run"]["written"] == 3


@pytest.mark.asyncio
async def test_the_sync_interval_will_not_go_below_a_minute():
    """A full vault walk every few seconds is churn, not freshness."""
    from plugins.ObsidianKnowledgePlugin.backend.services import sync_orchestrator as so

    try:
        assert so.start_vault_sync_loop(1) is True
        assert so._sync_interval == 60, "sub-minute intervals must be floored"
        assert so.get_vault_sync_status()["running"] is True
    finally:
        so.stop_vault_sync_loop()
    assert so.get_vault_sync_status()["running"] is False
