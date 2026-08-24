"""When the connected providers answer nothing, the seat reports a local read.

It used to fall through to ``OPENAI_API_KEY`` instead. That key is not a second
chance at the same models — it is a different vendor, outside every usage cap
and dedication rule the provider pool enforces, and one the user may never have
connected at all. When it was an NVIDIA key pointed at OpenAI it answered 401,
which tripped the *shared* circuit breaker and took the rest of the board down
with it: agents that had working providers were refused before they ever called
one.
"""

from __future__ import annotations

import asyncio

import pytest

from app.agents.base import BaseAgent
from plugins.AiMarketAnalyst.backend.services import ai_router


class _Settings:
    agents_use_providers = True


def _agent(name: str = "Sentiment Analyst") -> BaseAgent:
    return BaseAgent(
        agent_id=1,
        name=name,
        role="sentiment_analyst",
        system_prompt="you are a sentiment analyst",
        model="o3",
    )


@pytest.fixture()
def pool(monkeypatch):
    """A configured provider pool whose answer each test decides."""
    async def _settings(_db):
        return _Settings()

    async def _enabled(_db):
        return True

    monkeypatch.setattr(ai_router, "get_router_settings", _settings)
    monkeypatch.setattr(ai_router, "has_enabled_providers", _enabled)
    monkeypatch.setenv("OPENAI_API_KEY", "nvapi-test")


@pytest.fixture(autouse=True)
def _no_client(monkeypatch):
    """Any attempt to build the raw-key client fails the test loudly."""
    from app.agents import base

    def _boom():
        raise AssertionError("fell back to the raw OPENAI_API_KEY client")

    monkeypatch.setattr(base, "_get_client", _boom)
    base._AI_DECISION_CACHE.clear()
    yield
    base._AI_DECISION_CACHE.clear()


def _context(symbol: str) -> dict:
    return {"symbol": symbol, "timeframe": "1h", "price": 4400.0}


def test_an_unusable_provider_answer_does_not_reach_the_raw_key(pool, monkeypatch):
    async def _no_answer(*_a, **_kw):
        return {"ok": False, "content": None, "error": "Could not parse model JSON output"}

    monkeypatch.setattr(ai_router, "agent_chat", _no_answer)

    decision = asyncio.run(_agent().analyze(_context("XAUUSD"), db=object()))

    assert decision["ai_called"] is False
    assert decision["agent_role"] == "sentiment_analyst"


def test_a_provider_exception_does_not_reach_the_raw_key_either(pool, monkeypatch):
    async def _blow_up(*_a, **_kw):
        raise RuntimeError("provider pool unavailable")

    monkeypatch.setattr(ai_router, "agent_chat", _blow_up)

    decision = asyncio.run(_agent().analyze(_context("EURUSD"), db=object()))

    assert decision["ai_called"] is False


def test_a_provider_answer_is_returned_as_the_decision(pool, monkeypatch):
    async def _answer(*_a, **_kw):
        return {
            "ok": True,
            "content": {"action": "buy", "confidence": 0.7, "reasoning": "trend"},
            "provider": "NVIDIA NIM",
            "model": "nvidia/nemotron-3.5-lightning-30b-a3b",
            "usage": {"total_tokens": 900},
        }

    monkeypatch.setattr(ai_router, "agent_chat", _answer)

    decision = asyncio.run(_agent().analyze(_context("GBPUSD"), db=object()))

    assert decision["ai_called"] is True
    assert decision["provider"] == "NVIDIA NIM"
    assert decision["action"] == "buy"


def test_the_seat_asks_for_its_roles_model_not_the_seed_default():
    """``o3`` is the historical seed value; no connected provider serves it."""
    agent = _agent()
    assert agent._routed_model() != "o3"
    assert agent._routed_model() in ai_router.TASK_MODEL_CHAINS["fast_agentic"]
