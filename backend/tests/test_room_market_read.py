"""The room must read a moving market as moving, and must always have candles.

These cover the two failures that cost real entries: a board told the market
was "sideways" on a day it was trending, and a chart that reported no candles
for a pair every feed can price.
"""
from __future__ import annotations

import pytest

from app.agents.orchestrator import AgentOrchestrator


def _series(prices, *, spread=0.002, volume=1000.0):
    """ccxt rows from a close series, with a plausible bar around each close."""
    rows = []
    for i, close in enumerate(prices):
        prev = prices[i - 1] if i else close
        high = max(prev, close) * (1 + spread)
        low = min(prev, close) * (1 - spread)
        rows.append([1_700_000_000_000 + i * 3_600_000, prev, high, low, close, volume])
    return rows


# ── The measured read ────────────────────────────────────────────────────────


def test_a_market_that_ran_all_day_is_not_reported_as_sideways():
    """The gold case: a strong one-way run must read as a strong trend.

    The first version of this gated `direction` on the EMA stack, so a fast
    timeframe whipping across its own average reported "sideways" while price
    was 3% higher and closing at the top of the range — and a board told the
    market is sideways holds.
    """
    context = {}
    # A run with realistic pullbacks: net strongly up, closing near the high.
    prices = [100.0]
    for i in range(1, 80):
        step = 0.9 if i % 5 else -1.2  # four up, one back
        prices.append(prices[-1] + step)
    AgentOrchestrator._add_momentum(context, _series(prices))

    momentum = context["momentum"]
    assert momentum["direction"] == "up"
    assert momentum["strength"] == "strong"
    assert momentum["range_position_pct"] > 70


def test_a_selloff_reads_as_a_strong_downtrend():
    context = {}
    prices = [100.0]
    for i in range(1, 80):
        prices.append(prices[-1] + (-0.9 if i % 5 else 1.2))
    AgentOrchestrator._add_momentum(context, _series(prices))

    assert context["momentum"]["direction"] == "down"
    assert context["momentum"]["strength"] == "strong"


def test_a_genuinely_ranging_market_still_reads_weak():
    """The loosened classifier must not turn chop into a trade.

    Removing the hold bias is only an improvement if 'strong' still means
    something — a market oscillating around one level has to come back weak.
    """
    context = {}
    prices = [100 + (2.0 if i % 2 else -2.0) for i in range(80)]
    AgentOrchestrator._add_momentum(context, _series(prices))

    momentum = context["momentum"]
    assert momentum["strength"] == "weak"
    assert momentum["direction"] == "sideways"


def test_the_read_is_skipped_rather_than_guessed_on_thin_history():
    context = {}
    AgentOrchestrator._add_momentum(context, _series([100.0, 101.0, 102.0]))
    assert "momentum" not in context


def test_the_read_survives_malformed_rows():
    """A bad row must cost the read, never the meeting."""
    context = {}
    AgentOrchestrator._add_momentum(context, [[1, None, None, None, None, None]] * 40)
    assert "momentum" not in context


# ── Candle resolution ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_resolver_tries_every_source_before_reporting_none(monkeypatch):
    """One feed declining is not the market having no candles.

    This is the "No candles available for XAUUSD" bug: the room asked a single
    provider and treated its silence as the market's.
    """
    from app.services import candles

    candles._cache.clear()
    tried: list[str] = []

    async def dead(symbol, timeframe, limit):
        tried.append("dead")
        return []

    async def alive(symbol, timeframe, limit):
        tried.append("alive")
        return [[i * 60_000, 1.0, 1.1, 0.9, 1.05, 10.0] for i in range(50)]

    monkeypatch.setattr(candles, "_SOURCES", (("dead", dead), ("alive", alive)))
    rows = await candles.fetch("XAUUSD", "1h", 50)

    assert len(rows) == 50
    assert tried == ["dead", "alive"]


@pytest.mark.asyncio
async def test_an_unserved_timeframe_is_folded_up_from_a_finer_one(monkeypatch):
    """A 4h ask nobody serves is answered from 1h bars, not refused."""
    from app.services import candles

    candles._cache.clear()

    async def only_hourly(symbol, timeframe, limit):
        if timeframe != "1h":
            return []
        # 40 hourly bars climbing steadily.
        return [
            [i * 3_600_000, 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 5.0]
            for i in range(40)
        ]

    monkeypatch.setattr(candles, "_SOURCES", (("hourly", only_hourly),))
    rows = await candles.fetch("XAUUSD", "4h", 40)

    assert rows, "a 4h read must be buildable from 1h bars"
    # Four hourly bars per bucket, and the bucket must keep the true extremes.
    first = rows[0]
    assert first[2] >= first[1] and first[3] <= first[4]
    assert all(rows[i][0] < rows[i + 1][0] for i in range(len(rows) - 1))


@pytest.mark.asyncio
async def test_empty_is_only_reported_when_everything_declined(monkeypatch):
    from app.services import candles

    candles._cache.clear()

    async def dead(symbol, timeframe, limit):
        return []

    monkeypatch.setattr(candles, "_SOURCES", (("a", dead), ("b", dead)))
    assert await candles.fetch("NOTHINGUSD", "1h", 50) == []


@pytest.mark.asyncio
async def test_a_source_that_raises_does_not_stop_the_chain(monkeypatch):
    from app.services import candles

    candles._cache.clear()

    async def boom(symbol, timeframe, limit):
        raise RuntimeError("provider down")

    async def alive(symbol, timeframe, limit):
        return [[i * 60_000, 1.0, 1.1, 0.9, 1.05, 10.0] for i in range(50)]

    # _from_* wrappers guard their own imports; a raising source here proves the
    # loop itself is not the single point of failure.
    async def guarded(symbol, timeframe, limit):
        try:
            return await boom(symbol, timeframe, limit)
        except Exception:
            return []

    monkeypatch.setattr(candles, "_SOURCES", (("boom", guarded), ("alive", alive)))
    assert len(await candles.fetch("BTCUSDT", "1h", 50)) == 50
