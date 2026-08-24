"""A profile dedicated to a task must serve that task and nothing else.

The point is quota isolation. A slow vision read sharing a key with the chat
path spends the same rate limit, and the first symptom is the *other* feature
timing out — which is exactly the failure that motivated this. Dedication is
only real if it holds in both directions, so both are asserted here.
"""

from __future__ import annotations

import pytest

from plugins.AiMarketAnalyst.backend.services.ai_router import (
    TASK_MODEL_CHAINS,
    _apply_task_dedication,
    _assert_chains_are_disjoint,
    models_for_dedicated_profile,
)


class _P:
    """Stand-in for AILLMProvider — only the two fields the filter reads."""

    def __init__(self, pid: int, task: str | None = None):
        self.id = pid
        self.assigned_task = task

    def __repr__(self) -> str:  # keeps assertion output readable
        return f"P({self.id}, {self.assigned_task!r})"


def _ids(providers) -> list[int]:
    return [p.id for p in providers]


def test_task_call_uses_only_its_dedicated_profile():
    pool = [_P(1), _P(2, "vision_analysis"), _P(3)]
    assert _ids(_apply_task_dedication(pool, "vision_analysis")) == [2]


def test_dedicated_profile_is_held_out_of_general_calls():
    """The half that actually reserves the quota."""
    pool = [_P(1), _P(2, "vision_analysis"), _P(3)]
    assert _ids(_apply_task_dedication(pool, None)) == [1, 3]


def test_a_task_never_spills_onto_another_tasks_profile():
    """Failover must not quietly borrow a key reserved for something else."""
    pool = [_P(1, "deep_reasoning"), _P(2, "vision_analysis")]
    assert _ids(_apply_task_dedication(pool, "vision_analysis")) == [2]
    assert _ids(_apply_task_dedication(pool, "deep_reasoning")) == [1]


def test_task_with_no_dedicated_profile_falls_back_to_the_shared_pool():
    """Nothing needs configuring for the router to keep working."""
    pool = [_P(1), _P(2, "vision_analysis")]
    assert _ids(_apply_task_dedication(pool, "fast_agentic")) == [1]


def test_empty_string_assignment_counts_as_unassigned():
    """A cleared field can arrive as '' rather than NULL depending on the write."""
    pool = [_P(1, ""), _P(2, "vision_analysis")]
    assert _ids(_apply_task_dedication(pool, None)) == [1]
    assert _ids(_apply_task_dedication(pool, "fast_agentic")) == [1]


# ── Dedicated profiles never share a model ───────────────────────────────────

def test_task_chains_share_no_model():
    """Dedicated profiles are narrowed to their chain, so chains must be disjoint.

    A model in two chains would be installed on two keys, which quietly undoes
    the isolation the whole feature exists to provide.
    """
    seen: dict[str, str] = {}
    for task, models in TASK_MODEL_CHAINS.items():
        for m in models:
            assert m not in seen, f"{m} is in both {seen.get(m)} and {task}"
            seen[m] = task


def test_overlapping_chains_are_rejected_at_import(monkeypatch):
    """The guard must actually fire — a passing disjointness test is not proof."""
    monkeypatch.setitem(TASK_MODEL_CHAINS, "fast_agentic", ["z-ai/glm-5.2"])
    with pytest.raises(ValueError, match="disjoint"):
        _assert_chains_are_disjoint()


def test_dedicated_profile_carries_exactly_its_chain():
    """What the assign endpoint writes into models_json."""
    for task, chain in TASK_MODEL_CHAINS.items():
        assert models_for_dedicated_profile(task) == chain
    assert models_for_dedicated_profile("not-a-task") == []


def test_a_provider_offering_the_chain_is_narrowed_to_it():
    """An NVIDIA profile holding the full catalogue keeps only its task's models."""
    catalogue = TASK_MODEL_CHAINS["deep_reasoning"] + TASK_MODEL_CHAINS["vision_analysis"]
    got = models_for_dedicated_profile("deep_reasoning", catalogue)
    assert got == TASK_MODEL_CHAINS["deep_reasoning"]
    assert not set(got) & set(TASK_MODEL_CHAINS["vision_analysis"])


def test_a_vendor_without_the_chain_keeps_its_own_models():
    """Mistral has no NVIDIA model ids, but must still be able to hold a task.

    Forcing the chain here would dedicate a profile to a task it cannot serve,
    which is what blocked spreading load off NVIDIA.
    """
    mistral = ["mistral-small-latest", "open-mistral-nemo", "ministral-8b-latest"]
    assert models_for_dedicated_profile("fast_agentic", mistral) == mistral


def test_narrowing_keeps_chain_order_not_catalogue_order():
    """The chain is a preference order — the fast model must stay first."""
    chain = TASK_MODEL_CHAINS["vision_analysis"]
    shuffled = list(reversed(chain))
    assert models_for_dedicated_profile("vision_analysis", shuffled) == chain


# ── Surface tasks (jarvis / paul / telegram) ─────────────────────────────────

def test_chat_surfaces_are_offerable_but_pin_no_models():
    """They can be dedicated, but must not lock a vendor-specific model list."""
    for surface in ("jarvis_chat", "paul_chat", "telegram_chat"):
        assert surface in TASK_MODEL_CHAINS, f"{surface} is not offerable in the UI"
        assert TASK_MODEL_CHAINS[surface] == [], f"{surface} should pin no models"


def test_an_unset_surface_falls_back_to_any_available_provider():
    """Undedicated, a surface must behave exactly as it did before this feature."""
    pool = [_P(1), _P(2), _P(3, "vision_analysis")]
    assert _ids(_apply_task_dedication(pool, "telegram_chat")) == [1, 2]


def test_a_dedicated_surface_uses_only_its_profile():
    pool = [_P(1), _P(2, "paul_chat"), _P(3, "vision_analysis")]
    assert _ids(_apply_task_dedication(pool, "paul_chat")) == [2]
    # and the surface's profile is off-limits to everything else
    assert _ids(_apply_task_dedication(pool, None)) == [1]


def test_a_dedicated_surface_keeps_its_providers_own_models():
    """No chain means "whatever this profile serves", not "no models"."""
    mistral = ["mistral-small-latest", "open-mistral-nemo"]
    assert models_for_dedicated_profile("telegram_chat", mistral) == mistral


# ── Brain network ────────────────────────────────────────────────────────────

def test_every_brain_role_is_offerable_and_required():
    """The five roles that run concurrently each need their own key."""
    from plugins.AiMarketAnalyst.backend.services.ai_router import TASK_META, required_tasks

    roles = [
        "brain_consolidator", "brain_indexer", "brain_critic",
        "brain_researcher", "brain_news_organiser",
    ]
    for role in roles:
        assert role in TASK_MODEL_CHAINS, f"{role} cannot be assigned in the UI"
        assert TASK_META[role]["required"] is True, f"{role} should require a key"
        assert TASK_META[role]["group"] == "brain"
    assert set(required_tasks()) == set(roles)


def test_chat_surfaces_are_not_required():
    """Only the brains are required — surfaces must stay optional."""
    from plugins.AiMarketAnalyst.backend.services.ai_router import TASK_META

    for surface in ("jarvis_chat", "paul_chat", "telegram_chat"):
        assert TASK_META[surface]["required"] is False


def test_every_task_has_metadata():
    """A new chain must not ship without deciding whether it needs a key."""
    from plugins.AiMarketAnalyst.backend.services.ai_router import TASK_META

    assert set(TASK_META) == set(TASK_MODEL_CHAINS)
    for task, m in TASK_META.items():
        assert m["group"] in {"work", "surface", "brain"}, task
        assert isinstance(m["required"], bool), task
        assert m.get("label"), task


def test_brain_roles_do_not_share_a_profile():
    """Each role resolves to its own profile, never another role's."""
    pool = [_P(1, "brain_consolidator"), _P(2, "brain_critic"), _P(3)]
    assert _ids(_apply_task_dedication(pool, "brain_consolidator")) == [1]
    assert _ids(_apply_task_dedication(pool, "brain_critic")) == [2]
    # An unconfigured role falls back to the shared pool rather than stealing one
    assert _ids(_apply_task_dedication(pool, "brain_indexer")) == [3]


def test_everything_dedicated_leaves_a_general_call_with_nothing():
    """Better an explicit empty list than silently using a reserved key.

    db_chat turns this into "No AI providers configured", which is the honest
    answer — the alternative is spending the vision key on a chat turn.
    """
    pool = [_P(1, "vision_analysis"), _P(2, "deep_reasoning")]
    assert _apply_task_dedication(pool, None) == []
