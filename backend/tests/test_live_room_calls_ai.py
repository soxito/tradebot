"""A room someone typed must think, not remember.

Two shortcuts keep the background scanner off the token budget: a decision
recalled from past outcomes, and a per-symbol cache that replays an answer for
an hour. Both are right for the scanner and wrong for a person.

Observed on a `/room XAUUSD 1h`: three of four seats answered from cache — one
of them 3313 seconds old — and the board reported "AI calls: 0" underneath text
a model had written 55 minutes earlier, about a different price. The user read
it as the bot having stopped using AI, which is exactly what it looks like.
"""

from __future__ import annotations

import asyncio

import pytest

from app.agents import base
from app.agents.base import BaseAgent
from plugins.AiMarketAnalyst.backend.services import ai_router


class _Settings:
    agents_use_providers = True


def _agent() -> BaseAgent:
    return BaseAgent(
        agent_id=1, name="Signal Generator", role="signal_generator",
        system_prompt="you generate signals", model="o3",
    )


@pytest.fixture(autouse=True)
def pool(monkeypatch):
    """A working provider pool, and a clean cache for each test."""
    async def _settings(_db):
        return _Settings()

    async def _enabled(_db):
        return True

    monkeypatch.setattr(ai_router, "get_router_settings", _settings)
    monkeypatch.setattr(ai_router, "has_enabled_providers", _enabled)
    base._AI_DECISION_CACHE.clear()
    yield
    base._AI_DECISION_CACHE.clear()


@pytest.fixture()
def calls(monkeypatch):
    """Record every call that reaches the provider router."""
    seen: list[dict] = []

    async def _answer(_db, **kwargs):
        seen.append(kwargs)
        return {
            "ok": True,
            "content": {"action": "buy", "confidence": 0.8, "reasoning": "fresh"},
            "provider": "NVIDIA NIM", "model": "m", "usage": {"total_tokens": 100},
        }

    monkeypatch.setattr(ai_router, "agent_chat", _answer)
    return seen


def _run(agent, **kw):
    return asyncio.run(agent.analyze({"symbol": "XAUUSD", "price": 4391.0},
                                     db=object(), **kw))


def test_a_scheduled_run_reuses_the_cached_answer(calls):
    """The saving the cache exists for is left intact."""
    agent = _agent()
    _run(agent)
    second = _run(agent)

    assert len(calls) == 1
    assert second["ai_called"] is False
    assert second["from_cache"] is True


def test_a_live_run_calls_the_model_even_with_a_fresh_cache(calls):
    agent = _agent()
    _run(agent)                      # warms the cache
    decision = _run(agent, live=True)

    assert len(calls) == 2, "answered from cache on a run someone is waiting for"
    assert decision["ai_called"] is True
    assert decision.get("from_cache") is not True


def test_a_live_run_steps_over_a_providers_backoff(calls):
    """A cooldown protects a budget from the scanner, not from the user."""
    _run(_agent(), live=True)

    assert calls[0]["bypass_circuits"] is True


def test_a_scheduled_run_respects_the_backoff(calls):
    _run(_agent())

    assert calls[0]["bypass_circuits"] is False


def test_a_live_answer_is_still_cached_for_the_scanner(calls):
    """Freshness is about reading the cache, not refusing to fill it."""
    agent = _agent()
    _run(agent, live=True)
    _run(agent)

    assert len(calls) == 1


class TestWhichRunsAreLive:
    """The trigger decides, and /room arrives as ``telegram``."""

    def test_the_telegram_room_command_is_live(self):
        from app.agents.orchestrator import LIVE_TRIGGERS

        assert "telegram" in LIVE_TRIGGERS
        assert "manual" in LIVE_TRIGGERS

    def test_the_background_triggers_are_not(self):
        from app.agents.orchestrator import LIVE_TRIGGERS

        assert not {"scanner", "room", "signal"} & LIVE_TRIGGERS


def test_the_memory_shortcut_is_skipped_on_a_live_run(monkeypatch):
    """`try_local_decision` answers from past outcomes without any model call.

    Driven through ``_run_agent_inner`` rather than asserted on the expression,
    because the flag has to *arrive* there — it is reached through two wrappers,
    and an earlier version of this change threaded it into only the first.
    """
    from app.agents import orchestrator

    recalled = {"action": "hold", "confidence": 0.9, "reasoning": "recalled"}
    monkeypatch.setattr(orchestrator, "try_local_decision", lambda past, role: recalled)

    async def _past(*_a, **_kw):
        return []

    monkeypatch.setattr(orchestrator, "get_past_decisions", _past)
    monkeypatch.setattr(orchestrator, "build_memory_prompt", lambda past: "")

    async def _no_extras(*_a, **_kw):
        return ""

    monkeypatch.setattr(
        orchestrator.AgentOrchestrator, "_build_knowledge_graph_prompt",
        staticmethod(_no_extras),
    )

    seen: list[dict] = []

    async def _analyze(self, context, memory_prompt="", local_decision=None, db=None, live=False):
        seen.append({"local": local_decision is not None, "live": live})
        return {"action": "buy", "ai_called": local_decision is None}

    monkeypatch.setattr(BaseAgent, "analyze", _analyze)

    scheduled = asyncio.run(orchestrator.AgentOrchestrator._run_agent_inner(
        object(), _agent(), {"symbol": "XAUUSD"}, "XAUUSD",
    ))
    live = asyncio.run(orchestrator.AgentOrchestrator._run_agent_inner(
        object(), _agent(), {"symbol": "XAUUSD"}, "XAUUSD", live=True,
    ))

    assert seen[0]["local"] is True, "the scanner lost its memory shortcut"
    assert scheduled["ai_called"] is False
    assert seen[1]["local"] is False, "a live run answered from memory"
    assert seen[1]["live"] is True, "the flag never reached the agent"
    assert live["ai_called"] is True


class TestATypedRoomIsNeverTurnedAway:
    """The dedupe and focus lock exist for repeating work, not for a person.

    Both used to return the same stub — no decisions, no levels, "AI calls: 0" —
    which is indistinguishable from the room having nothing to say.
    """

    @staticmethod
    def _run(monkeypatch, trigger, *, inflight="XAUUSD", focus=("EURUSD",)):
        from app.agents import orchestrator as o

        ran: list[str] = []

        async def _pipeline(db, symbol, timeframe, trig):
            ran.append(trig)
            return {"symbol": symbol, "final_action": "buy", "ai_calls": 4}

        monkeypatch.setattr(
            o.AgentOrchestrator, "_run_full_pipeline", staticmethod(_pipeline),
        )
        monkeypatch.setattr(o.room, "get_focus_symbols", lambda: list(focus))
        monkeypatch.setattr(o.room, "is_focused", lambda s: s in focus)
        o._inflight_symbols.add(o._norm_symbol(inflight))
        try:
            result = asyncio.run(o.AgentOrchestrator.analyze_symbol(
                object(), "XAUUSD", "1h", trigger=trigger,
            ))
        finally:
            o._inflight_symbols.discard(o._norm_symbol(inflight))
        return result, ran

    def test_a_typed_room_runs_alongside_the_worker(self, monkeypatch):
        result, ran = self._run(monkeypatch, "telegram")

        assert ran == ["telegram"], "the user's run was dropped as a duplicate"
        assert result["ai_calls"] == 4

    def test_the_scanner_still_defers_to_the_meeting_in_progress(self, monkeypatch):
        result, ran = self._run(monkeypatch, "scanner")

        assert ran == []
        assert result["reason"] == "already_in_session"

    def test_a_typed_pair_is_not_blocked_by_the_focus_lock(self, monkeypatch):
        result, ran = self._run(monkeypatch, "telegram", inflight="NONE")

        assert ran == ["telegram"], "a pair asked for by name was focus-locked out"

    def test_the_scanner_still_respects_the_focus_lock(self, monkeypatch):
        result, ran = self._run(monkeypatch, "scanner", inflight="NONE")

        assert ran == []
        assert result["reason"] == "focus_locked"
