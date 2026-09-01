"""The cycle chart payload — candles reach as far back as the setting asks."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import market_cycle


@pytest.fixture(autouse=True)
def _fresh_monthly_cache():
    """Each test starts with an empty monthly cache — one test's fetch must
    not answer another's."""
    market_cycle._monthly_cache.update(bars=None, years=0, ts=0.0)
    yield
    market_cycle._monthly_cache.update(bars=None, years=0, ts=0.0)


def test_monthly_bar_limit_follows_years():
    """years × 12 bars (+ the forming-month buffer) is the whole ask."""
    from unittest.mock import patch

    captured = {}

    async def fake_fetch(symbol, tf, limit=0):
        captured["limit"] = limit
        return [{"time": 1, "close": 1.0}] * limit

    with patch.object(market_cycle, "_settings") as cfg, \
         patch("app.exchanges.yahoo_provider.fetch_candles", new=fake_fetch):
        cfg().CYCLE_CACHE_TTL_SECONDS = 0
        import asyncio

        bars = asyncio.run(market_cycle.resolve_monthly_bars(years=15))

    assert captured["limit"] == 15 * 12 + market_cycle._MONTHLY_BUFFER
    assert len(bars) == captured["limit"]


def test_monthly_years_clamped():
    """Out-of-range years cannot ask Yahoo for an absurd span."""
    from unittest.mock import patch

    captured = {}

    async def fake_fetch(symbol, tf, limit=0):
        captured["limit"] = limit
        return []

    with patch.object(market_cycle, "_settings") as cfg, \
         patch("app.exchanges.yahoo_provider.fetch_candles", new=fake_fetch):
        cfg().CYCLE_CACHE_TTL_SECONDS = 0
        import asyncio

        asyncio.run(market_cycle.resolve_monthly_bars(years=999))

    assert captured["limit"] == 20 * 12 + market_cycle._MONTHLY_BUFFER


def test_monthly_failure_is_silence():
    """A dead feed returns [] — the calendar renders without candles."""
    from unittest.mock import patch

    async def boom(symbol, tf, limit=0):
        raise RuntimeError("yahoo down")

    with patch.object(market_cycle, "_settings") as cfg, \
         patch("app.exchanges.yahoo_provider.fetch_candles", new=boom):
        cfg().CYCLE_CACHE_TTL_SECONDS = 0
        import asyncio

        bars = asyncio.run(market_cycle.resolve_monthly_bars(years=15))
    assert bars == []
