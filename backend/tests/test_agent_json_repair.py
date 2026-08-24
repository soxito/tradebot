"""Regression tests: truncated LLM JSON recovery + uncut Telegram meeting notes.

The room used to lose entire decisions when a model hit its token ceiling
mid-JSON ({"raw": ...} fallback) and chop every seat's reasoning at arbitrary
character counts in the Telegram card.
"""
import pytest
from app.agents.base import BaseAgent, _repair_truncated_json


class _Bare(BaseAgent):
    def __init__(self):  # noqa: D107 - test shim
        super().__init__(
            agent_id="t-1", name="t", role="market_analyst",
            system_prompt="s", model="gpt-4o-mini",
        )


REPAIR_CASES = [
    # (truncated input, should recover to a dict)
    ('{"action": "buy", "confidence": 0.7, "reasoning": "Trend supports lon', True),
    ('{"action": "sell", "risk": {"stop": 1.05,', True),
    ('{"a": 1, "b": [1, 2', True),
    ('{"action": "buy", "confidence"', True),
    ('{"action": "hold", "x": {"y": "z"}', True),
    ('{"a": {"b": {"c": [1, {"d": "e', True),
    ('{"key": ', True),          # bare dangling key → {}
    ('complete json {"a": 1}', False),  # never opened with a brace
    ('{"a": 1, "b": }', False),         # garbage mid-object
]


@pytest.mark.parametrize("text,should_recover", REPAIR_CASES)
def test_repair_truncated_json(text, should_recover):
    out = _repair_truncated_json(text)
    assert (out is not None) == should_recover


def test_repair_keeps_emitted_fields():
    """The whole point: a ceiling-clipped decision survives with its fields."""
    r = _repair_truncated_json(
        '{"action": "buy", "entry": 65000.5, "stop_loss": 64200, '
        '"reasoning": "Structure and forecast align; entering dem'
    )
    assert r is not None
    assert r["action"] == "buy"
    assert r["entry"] == 65000.5
    assert r["stop_loss"] == 64200


def test_parse_response_repairs_instead_of_raw_fallback():
    agent = _Bare()
    decision = agent.parse_response('{"action": "sell", "reasoning": "cut off mid sent')
    assert decision["action"] == "sell"
    assert "raw" not in decision or decision.get("reasoning")


def test_parse_response_valid_json_untouched():
    agent = _Bare()
    raw = '{"action": "hold", "confidence": 0.5}'
    assert agent.parse_response(raw) == {"action": "hold", "confidence": 0.5}


# ── seat_reasoning_messages ──────────────────────────────────────────────────

from plugins.TelegramSignalNewsPlugin.backend.services.room_bridge import (
    format_result,
    seat_reasoning_messages,
)


def _sample_result(long_reasoning_len=400):
    return {
        "final_action": "buy",
        "final_confidence": 0.82,
        "final_reasoning": "C" * 100,
        "agents_used": 2,
        "ai_calls": 2,
        "decisions": [
            {
                "agent_name": "Sakhile",
                "agent_role": "market_analyst",
                "action": "bullish",
                "confidence": 0.9,
                "reasoning": "R" * long_reasoning_len,
            },
            {
                "agent_name": "Thabo",
                "agent_role": "risk_manager",
                "action": "approve",
                "confidence": 0.7,
                "reasoning": "Risk within limits.",
            },
        ],
    }


def test_seat_notes_include_full_reasoning_not_cut():
    msgs = seat_reasoning_messages(_sample_result(400), "BTC/USDT", "1h")
    joined = "\n".join(msgs)
    assert "R" * 400 in joined, "long reasoning must ship complete"
    assert "Risk within limits." in joined
    assert all(len(m) <= 4096 for m in msgs)


def test_seat_notes_split_huge_reasoning_at_sentences():
    huge = ". ".join(f"Sentence number {i} says something" for i in range(200)) + "."
    result = _sample_result()
    result["decisions"][0]["reasoning"] = huge
    msgs = seat_reasoning_messages(result, "XAUUSD", "H1")
    joined = "\n".join(msgs)
    # Every sentence survives; no message exceeds Telegram's limit.
    for i in range(200):
        assert f"Sentence number {i}" in joined
    for m in msgs:
        assert len(m) <= 4096
        if len(m) == 4096:
            continue
    # Cuts land on sentence boundaries where possible — no mid-word chops like
    # "...somethingS" would indicate.
    assert "somethingS" not in joined


def test_format_result_card_stays_compact():
    card = format_result(_sample_result(5000), "BTC/USDT", "4h")
    assert len(card) <= 3900
