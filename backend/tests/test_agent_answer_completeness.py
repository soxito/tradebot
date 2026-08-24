"""An agent's answer must never reach the user as half a sentence.

Models that narrate before emitting JSON run past the token budget; the answer
then arrived either as an unparsable blob (the decision lost entirely) or as a
read that stopped mid-word. Both were visible to the user as agents that had
"stopped making sense".
"""
from __future__ import annotations

import pytest

from plugins.AiMarketAnalyst.backend.services.ai_router import parse_json_content


def test_a_decision_cut_off_mid_sentence_is_still_recovered():
    cut = (
        '{"action":"buy","confidence":0.72,"stop_loss":4380,'
        '"reasoning":"Gold broke the range on expanding volume. The stack is'
    )
    parsed = parse_json_content(cut)

    assert parsed is not None, "a truncated answer still holds a real decision"
    assert parsed["action"] == "buy"
    assert parsed["confidence"] == 0.72
    assert parsed["stop_loss"] == 4380
    assert parsed["_truncated"] is True


def test_the_recovered_reasoning_ends_on_a_finished_sentence():
    """Never publish a half word. Trim back to the last full stop instead."""
    cut = (
        '{"action":"sell","reasoning":"Price rejected the high. Volume confirmed '
        'the fade. Momentum is now turn'
    )
    parsed = parse_json_content(cut)

    assert parsed["reasoning"] == "Price rejected the high. Volume confirmed the fade."
    assert not parsed["reasoning"].endswith("turn")


def test_a_truncated_array_keeps_the_targets_it_did_name():
    parsed = parse_json_content('{"action":"buy","take_profits":[4440,4470,')
    assert parsed["take_profits"] == [4440, 4470]


def test_a_key_with_no_value_is_dropped_rather_than_guessed():
    parsed = parse_json_content('{"action":"buy","confidence":0.8,"reasoning":')
    assert parsed["action"] == "buy"
    assert "reasoning" not in parsed


def test_a_complete_answer_is_not_marked_truncated():
    parsed = parse_json_content('{"action":"hold","reasoning":"Mid range."}')
    assert parsed == {"action": "hold", "reasoning": "Mid range."}


def test_fenced_json_still_parses_cleanly():
    parsed = parse_json_content('```json\n{"action":"buy","confidence":0.6}\n```')
    assert parsed["action"] == "buy"
    assert "_truncated" not in parsed


def test_prose_with_no_object_is_still_a_failure():
    """Recovery must not invent a decision out of an answer that had none."""
    assert parse_json_content("I think gold looks strong here, but") is None
    assert parse_json_content("") is None


# ── Budgets ──────────────────────────────────────────────────────────────────


def test_every_agent_gets_room_to_finish_its_answer():
    """The seeded 2000-token ceiling is what cut published reads mid-sentence."""
    from app.agents.base import _MIN_AGENT_BUDGET, _budget_for

    assert _budget_for("some/chat-model", 2000) >= _MIN_AGENT_BUDGET
    # A larger explicit choice is still respected.
    assert _budget_for("some/chat-model", 20000) == 20000


def test_a_reasoning_model_gets_its_larger_budget():
    """A model that narrates before answering needs the wider ceiling."""
    from app.agents.base import _REASONING_BUDGET, _budget_for

    assert _budget_for("z-ai/glm-5.2", 2000) >= _REASONING_BUDGET


# ── Prompt upgrades ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_un_customised_prompts_are_brought_up_to_date(async_session):
    """Improving the shipped instructions must reach an install that has seeded.

    Prompts move into the DB on first seed, so the board went on following the
    old "when in doubt, lean neutral" text long after it was rewritten.
    """
    from app.agents.specialists import (
        DEFAULT_PROMPTS, upgrade_stock_prompts, with_completeness,
    )
    from app.models.database import Agent

    legacy = (
        "You are an expert multi-asset market analyst covering crypto, FX, "
        "metals, indices, energy and softs.\nBe conservative."
    )
    stock = Agent(
        name="Market Analyst", role="market_analyst",
        system_prompt=DEFAULT_PROMPTS["market_analyst"], model="o3",
    )
    edited = Agent(
        name="Risk Manager", role="risk_manager",
        system_prompt=legacy, model="o3",
    )
    async_session.add_all([stock, edited])
    await async_session.commit()

    await upgrade_stock_prompts(async_session)
    await async_session.refresh(stock)
    await async_session.refresh(edited)

    # The stock prompt gains the output-discipline clause.
    assert "OUTPUT DISCIPLINE" in stock.system_prompt
    # Someone's own writing is never overwritten.
    assert edited.system_prompt == legacy


@pytest.mark.asyncio
async def test_the_upgrade_is_idempotent(async_session):
    from app.agents.specialists import DEFAULT_PROMPTS, upgrade_stock_prompts
    from app.models.database import Agent

    async_session.add(Agent(
        name="Signal Generator", role="signal_generator",
        system_prompt=DEFAULT_PROMPTS["signal_generator"], model="o3",
    ))
    await async_session.commit()

    assert await upgrade_stock_prompts(async_session) == 1
    assert await upgrade_stock_prompts(async_session) == 0


def test_the_shipped_instructions_no_longer_lean_on_hold():
    """The lines that made the board answer 'hold' into a trending market."""
    from app.agents.specialists import (
        MARKET_ANALYST_PROMPT, RISK_MANAGER_PROMPT, SIGNAL_GENERATOR_PROMPT,
    )

    assert "when in doubt, lean toward" not in MARKET_ANALYST_PROMPT
    assert "When in doubt, REJECT." not in RISK_MANAGER_PROMPT
    assert "only at confidence >= 0.70" not in SIGNAL_GENERATOR_PROMPT
    # And the evidence they must now weigh is named.
    assert "momentum" in MARKET_ANALYST_PROMPT
    assert "kronos_forecast" in MARKET_ANALYST_PROMPT
    assert "momentum" in SIGNAL_GENERATOR_PROMPT
    # And the three ways a directional read used to be talked back into a hold.
    assert "A SHORT IS A TRADE" in SIGNAL_GENERATOR_PROMPT
    assert "WHAT COUNTS AS CONFLICT" in SIGNAL_GENERATOR_PROMPT
    assert "NO ENTRY HERE IS NOT NO TRADE" in SIGNAL_GENERATOR_PROMPT
    assert "YOUR OWN CONFIDENCE IS NOT A REASON" in SIGNAL_GENERATOR_PROMPT


def test_overbought_is_not_treated_as_a_bearish_call():
    """An elevated RSI in a live uptrend is what a trend looks like.

    The sentiment seat was answering "bearish" while saying in the same breath
    that the trend was intact, and the signal seat then counted that as
    directional conflict and stood aside.
    """
    from app.agents.specialists import SENTIMENT_ANALYST_PROMPT, SIGNAL_GENERATOR_PROMPT

    assert "OVERBOUGHT IS NOT BEARISH" in SENTIMENT_ANALYST_PROMPT
    assert "not a vote for the opposite direction" in SIGNAL_GENERATOR_PROMPT


def test_every_shipped_prompt_carries_the_output_discipline_clause():
    """The clause is what stops a published read ending mid-sentence."""
    from app.agents.specialists import DEFAULT_PROMPTS, with_completeness

    for role, prompt in DEFAULT_PROMPTS.items():
        assert "OUTPUT DISCIPLINE" in with_completeness(prompt), role


def test_the_pre_baked_indicator_verdict_is_labelled_not_adopted():
    """`technical.action` is a mechanical score, not the desk's answer.

    `app.signals.technical.analyze` emits its own action/confidence, and the
    seats were quoting it verbatim — "technical confidence 0.3176 is below the
    0.55 threshold" — as the reason for declining a trending market. That
    scorer never sees structure, the forecast or the momentum read.
    """
    from app.agents.orchestrator import AgentOrchestrator
    from app.agents.specialists import MARKET_ANALYST_PROMPT, SIGNAL_GENERATOR_PROMPT

    context = {"technical": {"action": "hold", "confidence": 0.31, "indicators": {}}}
    AgentOrchestrator._label_technical(context)
    assert "MECHANICAL INDICATOR COMPOSITE" in context["technical"]["provenance"]
    # The fields the local no-AI fallback needs are untouched.
    assert context["technical"]["action"] == "hold"
    assert context["technical"]["confidence"] == 0.31

    for prompt in (MARKET_ANALYST_PROMPT, SIGNAL_GENERATOR_PROMPT):
        assert "mechanical indicator score" in prompt


def test_a_block_with_no_verdict_is_left_alone():
    from app.agents.orchestrator import AgentOrchestrator

    context = {"technical": {"error": "no data"}}
    AgentOrchestrator._label_technical(context)
    assert "provenance" not in context["technical"]
