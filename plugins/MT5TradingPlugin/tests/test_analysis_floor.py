"""
Proof that the /mt5-live analysis path cannot return empty, null, or an error.

Covers the three tiers:
  * primary       — healthiest provider answers
  * cascade       — primary returns malformed output, a fallback answers
  * deterministic — no provider reachable at all, engine-only floor fires

The floor test is the important one: with every provider disabled AND every
provider call raising, ``ai_review`` must still return a complete, valid block.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plugins.AiMarketAnalyst.backend.services import ai_router, analysis_router  # noqa: E402
from plugins.AiMarketAnalyst.backend.services.provider_health import (  # noqa: E402
    provider_health,
)
from plugins.MT5TradingPlugin.backend.services import smc_ai, smc_floor  # noqa: E402


# ── Fixtures ─────────────────────────────────────────────────────────────────

ANALYSIS = {
    "bias": "bullish",
    "last_price": 2345.60,
    "atr": 4.21,
    "atr_pct": 0.18,
    "rsi": 41.2,
    "volume_z": 1.35,
    "momentum": "expanding",
    "equilibrium": 2340.0,
    "range": {"low": 2300.0, "high": 2380.0},
    "structure_events": [{"type": "BOS", "price": 2352.1, "index": 180}],
    "liquidity": {"buyside": [2381.0], "sellside": [2299.0]},
    "zones": [],
    "signals": [
        {
            "side": "buy", "order_type": "buy_limit", "entry": 2330.5,
            "stop_loss": 2322.0, "take_profit": 2352.0, "rr": 2.53,
            "confidence": 0.81, "zone_kind": "bullish_fvg",
            "confluence": ["fair_value_gap", "discount", "structure_aligned"],
            "score_breakdown": {
                "total": 0.81,
                "volume_confirmed": True,
                "factors": [
                    {"name": "relative_volume", "raw_value": 1.9, "weight": 0.20, "contribution": 0.18},
                    {"name": "structure_aligned", "raw_value": 1.0, "weight": 0.15, "contribution": 0.15},
                    {"name": "fvg_in_discount", "raw_value": 1.0, "weight": 0.12, "contribution": 0.12},
                ],
            },
        },
        {
            "side": "buy", "order_type": "buy_limit", "entry": 2318.0,
            "stop_loss": 2310.0, "take_profit": 2340.0, "rr": 2.75,
            "confidence": 0.58, "zone_kind": "bullish_ob",
            "confluence": ["order_block", "deep_discount"],
        },
    ],
}


class FakeDB:
    """Minimal AsyncSession stand-in: usage bookkeeping must not touch a real DB."""

    def __init__(self) -> None:
        self.added = []

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


def _provider(pid: int, label: str, model: str = "test-model"):
    return SimpleNamespace(
        id=pid, label=label, api_key="k", base_url="https://example.invalid/v1",
        default_model=model, priority=pid, total_calls=0, daily_calls=0,
        monthly_calls=0, total_errors=0, status="ok", last_error=None,
        last_model_used=None, last_tested_at=None,
    )


VALID_LLM_JSON = json.dumps({
    "bias_comment": "Bullish structure intact.",
    "market_read": "Price is in discount with expanding volume.",
    "rated_signals": [
        {"entry": 2330.5, "verdict": "take", "confidence": 0.78, "note": "FVG in discount"},
    ],
    "top_pick_entry": 2330.5,
    "risk_warning": "Watch the London open.",
})


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Reset shared state and block all network for every test in this module."""
    provider_health.reset()
    ai_router._circuits.clear()

    async def _no_events(_symbol):
        return []

    monkeypatch.setattr(smc_ai, "fetch_economic_events", _no_events)
    yield
    provider_health.reset()
    ai_router._circuits.clear()


def _assert_complete_block(ai: dict) -> None:
    """Every contract field the frontend relies on must be present and typed."""
    assert ai is not None
    assert ai["available"] is True
    assert isinstance(ai["bias_comment"], str) and ai["bias_comment"]
    assert isinstance(ai["market_read"], str) and ai["market_read"]
    assert isinstance(ai["rated_signals"], list)
    assert ai["tier"] in ("primary", "cascade", "deterministic")
    assert isinstance(ai["confidence"], float)
    assert isinstance(ai["is_degraded"], bool)
    for rated in ai["rated_signals"]:
        assert rated["verdict"] in ("take", "skip", "watch")
        assert 0.0 <= rated["confidence"] <= 1.0
        assert isinstance(rated["entry"], float)


# ── Tier 3: the deterministic floor ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_floor_fires_with_zero_providers_configured(monkeypatch):
    """All AI providers disabled -> engine-only output, never empty."""
    async def _none(_db):
        return []

    monkeypatch.setattr(analysis_router, "rank_providers", _none)

    ai = await smc_ai.ai_review(
        db=FakeDB(), symbol="XAUUSD", timeframe="H1", analysis=ANALYSIS,
    )

    _assert_complete_block(ai)
    assert ai["tier"] == "deterministic"
    assert ai["is_degraded"] is True
    assert ai["provider_used"] is None
    # The floor reports the engine's own top score, not a fabricated number.
    assert ai["confidence"] == 0.81
    assert len(ai["rated_signals"]) == 2
    assert ai["top_pick_entry"] == 2330.5


@pytest.mark.asyncio
async def test_floor_fires_when_every_provider_raises(monkeypatch):
    """Providers configured but all unreachable -> still a complete signal."""
    async def _providers(_db):
        return [_provider(1, "NVIDIA"), _provider(2, "Cerebras"), _provider(3, "Groq")]

    async def _boom(**_kwargs):
        raise ConnectionError("network unreachable")

    monkeypatch.setattr(analysis_router, "rank_providers", _providers)
    monkeypatch.setattr(ai_router, "_call_openai_compatible", _boom)

    ai = await smc_ai.ai_review(
        db=FakeDB(), symbol="XAUUSD", timeframe="H1", analysis=ANALYSIS,
    )

    _assert_complete_block(ai)
    assert ai["tier"] == "deterministic"
    assert ai["is_degraded"] is True
    # All three were attempted before the floor fired.
    assert "NVIDIA" in ai["reason"] and "Groq" in ai["reason"]


@pytest.mark.asyncio
async def test_floor_handles_no_setups_without_erroring(monkeypatch):
    async def _none(_db):
        return []

    monkeypatch.setattr(analysis_router, "rank_providers", _none)

    empty = dict(ANALYSIS, signals=[])
    ai = await smc_ai.ai_review(
        db=FakeDB(), symbol="XAUUSD", timeframe="H1", analysis=empty,
    )

    _assert_complete_block(ai)
    assert ai["rated_signals"] == []
    assert ai["confidence"] == 0.0
    assert "standing aside" in ai["bias_comment"].lower()


@pytest.mark.asyncio
async def test_floor_handles_engine_error(monkeypatch):
    async def _none(_db):
        return []

    monkeypatch.setattr(analysis_router, "rank_providers", _none)

    broken = {"error": "Not enough candles", "signals": []}
    ai = await smc_ai.ai_review(
        db=FakeDB(), symbol="XAUUSD", timeframe="H1", analysis=broken,
    )

    _assert_complete_block(ai)
    assert "Not enough candles" in ai["bias_comment"]


# ── Tier 1 and 2 ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_primary_provider_answers(monkeypatch):
    async def _providers(_db):
        return [_provider(1, "NVIDIA"), _provider(2, "Cerebras")]

    async def _ok(**_kwargs):
        return VALID_LLM_JSON, {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}, None

    monkeypatch.setattr(analysis_router, "rank_providers", _providers)
    monkeypatch.setattr(ai_router, "_call_openai_compatible", _ok)

    ai = await smc_ai.ai_review(
        db=FakeDB(), symbol="XAUUSD", timeframe="H1", analysis=ANALYSIS,
    )

    _assert_complete_block(ai)
    assert ai["tier"] == "primary"
    assert ai["provider_used"] == "NVIDIA"
    assert ai["is_degraded"] is False


@pytest.mark.asyncio
async def test_malformed_output_cascades_instead_of_crashing(monkeypatch):
    """Schema failure must cascade to the next provider, not raise."""
    calls: list[str] = []

    async def _by_provider(*, base_url, api_key, model, messages, temperature,
                           max_tokens, json_mode):
        calls.append(model)
        if model == "bad":
            return "I think you should buy, honestly.", {}, None
        return VALID_LLM_JSON, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}, None

    async def _providers_named(_db):
        return [_provider(1, "BadJSON", "bad"), _provider(2, "GoodJSON", "good")]

    monkeypatch.setattr(analysis_router, "rank_providers", _providers_named)
    monkeypatch.setattr(ai_router, "_call_openai_compatible", _by_provider)

    ai = await smc_ai.ai_review(
        db=FakeDB(), symbol="XAUUSD", timeframe="H1", analysis=ANALYSIS,
    )

    _assert_complete_block(ai)
    assert calls == ["bad", "good"]
    assert ai["tier"] == "cascade"
    assert ai["provider_used"] == "GoodJSON"
    assert ai["is_degraded"] is True


@pytest.mark.asyncio
async def test_hard_timeout_is_enforced_per_attempt(monkeypatch):
    """A hanging provider must be abandoned at the hard cap, not waited on."""
    import asyncio

    async def _providers(_db):
        return [_provider(1, "Hanging")]

    async def _hang(**_kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(analysis_router, "rank_providers", _providers)
    monkeypatch.setattr(ai_router, "_call_openai_compatible", _hang)

    result = await analysis_router.analyze_with_cascade(
        FakeDB(), [{"role": "user", "content": "x"}],
        validator=lambda raw: raw, hard_timeout=0.2, total_budget=1.0,
    )

    assert result["ok"] is False
    assert result["tier"] == "deterministic"
    assert "timeout" in result["errors"][0]


# ── The floor in isolation ───────────────────────────────────────────────────

def test_floor_build_is_pure_and_needs_no_network():
    ai = smc_floor.build(ANALYSIS, reason="unit test")
    _assert_complete_block(ai)
    assert ai["tier"] == "deterministic"
    # Highest-contribution factors are surfaced numerically in the note.
    assert "relative_volume +0.180" in ai["rated_signals"][0]["note"]


def test_floor_verdicts_follow_confidence_thresholds():
    ai = smc_floor.build(ANALYSIS, reason="unit test")
    by_entry = {r["entry"]: r for r in ai["rated_signals"]}
    assert by_entry[2330.5]["verdict"] == "take"   # 0.81 >= 0.72
    assert by_entry[2318.0]["verdict"] == "watch"  # 0.58 in [0.55, 0.72)


def test_validator_rejects_substanceless_output():
    """`{}` and entry-less ratings must be rejected so the router cascades."""
    assert smc_ai._validate("{}", [2330.5]) is None
    assert smc_ai._validate("not json at all", [2330.5]) is None
    # Ratings for a price the engine never produced are dropped -> nothing left.
    hallucinated = json.dumps({
        "market_read": "looks good",
        "rated_signals": [{"entry": 9999.0, "verdict": "take", "confidence": 0.9}],
    })
    assert smc_ai._validate(hallucinated, [2330.5]) is None
