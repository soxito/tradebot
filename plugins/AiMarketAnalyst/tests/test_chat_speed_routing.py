"""A conversational turn answers fast unless the work earns a thinking model.

The failure this guards is the one the user reported: every Telegram and web
chat message took tens of seconds, because "why is gold up" was routed to a
reasoning model that thinks for a minute before writing a word — and, worse,
was sometimes pinned to a model no connected provider even served, so every
provider 400'd in turn before anything answered.
"""

from __future__ import annotations

import pytest

from plugins.AiMarketAnalyst.backend.services.ai_router import (
    FAST_CHAT_MODELS,
    TASK_MODEL_CHAINS,
    THINKING_SWITCHABLE_MODELS,
    _REASONING_MODELS,
    _fit_local_context,
    _is_human_chat,
    is_fast_model,
    resolve_chat_route,
    wants_deep_thinking,
)


# ── The depth classifier ──────────────────────────────────────────────────────

@pytest.mark.parametrize("message", [
    "what's my balance?",
    "price of gold",
    "why is gold up today?",
    "explain what an order block is",
    "hey, any news on BTC?",
    "what's the plan for the session",
    "how does the sniper decide entries",
])
def test_ordinary_questions_stay_fast(message):
    assert wants_deep_thinking(message) is False


@pytest.mark.parametrize("message", [
    "think deeply about this before answering",
    "take your time and give me a full analysis of BTC",
    "deep dive on the gold setup please",
    "be thorough here",
    "I want an in-depth look at my portfolio",
])
def test_the_user_can_always_ask_for_depth(message):
    assert wants_deep_thinking(message) is True


@pytest.mark.parametrize("message", [
    "run a full analysis on XAUUSD",
    "should I buy ETH here?",
    "give me a trade plan for gold",
    "backtest that idea",
    "what do all agents say about BTC",
])
def test_real_work_goes_deep_without_being_asked(message):
    assert wants_deep_thinking(message) is True


def test_a_long_brief_is_big_work_even_with_no_keywords():
    brief = (
        "Here is what I am seeing on the four hour: price swept the high from "
        "Tuesday, came back into the range, and has been building lower highs "
        "since London. My last two entries got stopped for a combined half a "
        "percent and I am wondering whether the approach still fits this "
        "regime or whether I am forcing trades in a chopping market. "
    )
    assert len(brief) >= 320
    assert wants_deep_thinking(brief) is True


def test_empty_input_is_not_deep():
    assert wants_deep_thinking("") is False
    assert wants_deep_thinking(None) is False


# ── Model selection ───────────────────────────────────────────────────────────

def test_no_fast_candidate_is_a_thinking_or_slow_model():
    """The whole point of the list — a "fast" model that reasons first is not."""
    for model in FAST_CHAT_MODELS:
        assert is_fast_model(model), f"{model} is not actually a fast model"


def test_fast_list_does_not_overlap_the_deep_chain():
    # A model whose thinking is switched off on fast turns is a different
    # model in practice, so it may serve both; nothing else may.
    overlap = set(FAST_CHAT_MODELS) & set(TASK_MODEL_CHAINS["deep_reasoning"])
    assert overlap <= THINKING_SWITCHABLE_MODELS


def test_reasoning_models_are_never_fast():
    for model in _REASONING_MODELS:
        assert is_fast_model(model) is False


class _P:
    """Stand-in for AILLMProvider — the fields the route resolver reads."""

    def __init__(self, models, default=None, task=None):
        self.id = id(self)
        self.assigned_task = task
        self.api_key = "k"
        self.base_url = "https://example.test/v1"
        self.models_json = list(models)
        self.default_model = default or models[0]
        self.enabled = True


DEEP = TASK_MODEL_CHAINS["deep_reasoning"][0]


def _router_with(providers, monkeypatch):
    from plugins.AiMarketAnalyst.backend.services import ai_router

    async def _fake_get(db, *, include_dedicated=False):
        return list(providers)

    monkeypatch.setattr(ai_router, "get_enabled_providers", _fake_get)


@pytest.mark.asyncio
async def test_a_quick_question_is_pinned_to_a_fast_model(monkeypatch):
    pool = [_P(["nvidia/nemotron-3.5-lightning-30b-a3b", "meta/llama-3.3-70b-instruct"])]
    _router_with(pool, monkeypatch)

    route = await resolve_chat_route(None, task="paul_chat", text="what's my balance?")

    assert route.deep is False
    assert route.model == "meta/llama-3.3-70b-instruct"
    assert is_fast_model(route.model)


@pytest.mark.asyncio
async def test_deep_work_gets_the_reasoning_model_when_the_pool_has_one(monkeypatch):
    pool = [_P([DEEP, "meta/llama-3.3-70b-instruct"])]
    _router_with(pool, monkeypatch)

    route = await resolve_chat_route(
        None, task="paul_chat", text="run a full analysis on BTC"
    )

    assert route.deep is True
    assert route.model == DEEP
    assert route.max_tokens >= 3000


@pytest.mark.asyncio
async def test_a_deep_turn_reaches_the_profile_reserved_for_reasoning(monkeypatch):
    """The strong models live on a dedicated profile the chat surfaces cannot
    otherwise see, so "run a full analysis" used to answer on a small chat
    model while the reasoning key sat idle."""
    pool = [
        _P(["meta/llama-3.3-70b-instruct"]),
        _P([DEEP], task="deep_reasoning"),
    ]
    _router_with(pool, monkeypatch)

    deep = await resolve_chat_route(
        None, task="paul_chat", text="run a full analysis on BTC"
    )
    fast = await resolve_chat_route(None, task="paul_chat", text="what's my balance?")

    assert deep.task == "deep_reasoning" and deep.model == DEEP
    # ...and a fast turn must NOT spend that reserved key.
    assert fast.task == "paul_chat" and fast.model == "meta/llama-3.3-70b-instruct"


@pytest.mark.asyncio
async def test_a_surface_with_its_own_profile_keeps_it_on_deep_turns(monkeypatch):
    """An explicit assignment is a decision, not a default to route around."""
    pool = [
        _P(["mistral-small-latest"], task="paul_chat"),
        _P([DEEP], task="deep_reasoning"),
    ]
    _router_with(pool, monkeypatch)

    route = await resolve_chat_route(
        None, task="paul_chat", text="run a full analysis on BTC"
    )

    assert route.task == "paul_chat"


@pytest.mark.asyncio
async def test_a_model_no_connected_provider_serves_is_never_pinned(monkeypatch):
    """The 400-in-a-loop bug: the deep chain lived on a profile this surface
    cannot see, and pinning it made every provider in the pool reject the call."""
    pool = [_P(["Meta-Llama-3.3-70B-Instruct"])]
    _router_with(pool, monkeypatch)

    route = await resolve_chat_route(
        None, task="telegram_chat", text="run a full analysis on BTC", surface="telegram"
    )

    assert route.deep is True
    assert route.model in (None, "Meta-Llama-3.3-70B-Instruct")
    assert route.model not in TASK_MODEL_CHAINS["deep_reasoning"]


@pytest.mark.asyncio
async def test_force_deep_overrides_the_classifier_both_ways(monkeypatch):
    pool = [_P([DEEP, "meta/llama-3.3-70b-instruct"])]
    _router_with(pool, monkeypatch)

    forced_fast = await resolve_chat_route(
        None, task="paul_chat", text="run a full analysis on BTC", force_deep=False
    )
    forced_deep = await resolve_chat_route(
        None, task="paul_chat", text="hi", force_deep=True
    )

    assert forced_fast.deep is False and is_fast_model(forced_fast.model)
    assert forced_deep.deep is True and forced_deep.model == DEEP


@pytest.mark.asyncio
async def test_telegram_gets_a_tighter_wall_clock_than_the_web_chat(monkeypatch):
    """No streaming there, so the same wait reads as a broken bot much sooner."""
    pool = [_P(["meta/llama-3.3-70b-instruct"])]
    _router_with(pool, monkeypatch)

    tg = await resolve_chat_route(
        None, task="telegram_chat", text="price of gold", surface="telegram"
    )
    web = await resolve_chat_route(
        None, task="paul_chat", text="price of gold", surface="web"
    )

    assert tg.budget_s < web.budget_s
    assert tg.max_retries <= 1  # an interactive turn cannot afford the backoff ladder


# ── Human chat routing ────────────────────────────────────────────────────────

def test_paul_and_telegram_chat_are_human_chat_and_agents_are_not():
    """Only these bypass the TRAINING_PROVIDER block and prefer the cloud."""
    assert _is_human_chat("jarvis_chat", "paul_chat")
    assert _is_human_chat("jarvis_chat", "deep_reasoning")  # a deep Paul turn
    assert _is_human_chat("telegram", "telegram_chat")
    assert not _is_human_chat("agent", None)
    assert not _is_human_chat("chat", "fast_agentic")


def test_local_fallback_prompt_fits_the_ollama_window():
    """Ollama 400s on a prompt longer than num_ctx instead of truncating it.

    Paul's system prompt alone is ~6k tokens against a 4k window, which is why
    the local fallback failed on exactly the turns that needed it.
    """
    system = {"role": "system", "content": "PERSONA-HEAD " + "context " * 6000}
    history = [{"role": "user", "content": f"old turn {i} " * 50} for i in range(20)]
    latest = {"role": "user", "content": "and what about bitcoin?"}

    fitted, reply = _fit_local_context([system, *history, latest], 4096, 1100)

    chars = sum(len(m["content"]) for m in fitted)
    assert chars / 3.2 + reply <= 4096
    assert fitted[0]["content"].startswith("PERSONA-HEAD")  # rules survive
    assert fitted[-1] == latest  # the question being asked is never cut
