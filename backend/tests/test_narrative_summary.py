"""The spoken analysis must never say more than the data supports.

These lock down the two ways prose over indicators goes wrong: claiming a
reading that was never computed (a missing volume feed becoming "volume is
drying up"), and letting absent indicators vote, which would let a short
history print as bearish conviction nobody actually cast.
"""

from __future__ import annotations

import pytest

from app.signals.narrative import (
    bias_votes, display_name, indicator_snapshot, narrative_summary,
)


def _candles(n: int = 220, *, drift: float = 0.0, start: float = 100.0,
             volume: float = 1000.0) -> list[list]:
    """A deterministic series; no randomness, so a failure is reproducible."""
    rows, price = [], start
    for i in range(n):
        price += drift + (0.6 if i % 2 else -0.5)
        rows.append([
            1_700_000_000_000 + i * 14_400_000,
            price, price + 1.0, price - 1.0, price, volume,
        ])
    return rows


# ── Structure of the output ──────────────────────────────────────────────────

def test_every_section_is_present_for_a_full_series():
    text = narrative_summary(
        _candles(), symbol="SOLUSDT", timeframe="4h", trend="ranging",
        swing_high=83.97, swing_low=65.92,
    )
    assert "👁‍🗨" in text and "4H timeframe for Solana" in text
    assert "⚖️" in text and "RSI" in text
    assert "Bollinger Bands" in text
    assert "🔔 Key Levels:" in text
    assert "$83.97" in text and "$65.92" in text
    assert "Bias:" in text


def test_the_trend_the_caller_computed_is_the_one_described():
    """A second opinion here would contradict the caller's own setup."""
    rows = _candles()
    assert "consolidation phase" in narrative_summary(
        rows, symbol="BTCUSDT", timeframe="1h", trend="ranging")
    assert "bullish structure" in narrative_summary(
        rows, symbol="BTCUSDT", timeframe="1h", trend="uptrend")
    assert "bearish control" in narrative_summary(
        rows, symbol="BTCUSDT", timeframe="1h", trend="downtrend")


def test_key_levels_are_omitted_when_the_caller_has_no_swings():
    text = narrative_summary(_candles(), symbol="BTCUSDT", timeframe="1h", trend="ranging")
    assert "🔔" not in text


def test_too_little_history_says_nothing_at_all():
    """Better silence than a read drawn from eight bars."""
    assert narrative_summary(_candles(8), symbol="BTCUSDT", timeframe="1h",
                             trend="ranging") == ""


# ── Nothing is invented ──────────────────────────────────────────────────────

def test_a_feed_without_volume_makes_no_claim_about_volume():
    """Many FX and metals feeds carry no volume; that is a fact about the feed."""
    text = narrative_summary(
        _candles(volume=0.0), symbol="XAUUSD", timeframe="4h", trend="ranging")
    assert "average)" not in text
    assert "drying up" not in text and "expanding" not in text
    # The rest of the read still lands.
    assert "Bollinger Bands" in text


def test_volume_is_described_only_in_the_direction_the_ratio_shows():
    rows = _candles()
    rows[-1][5] = 100.0                      # a tenth of the running average
    assert "drying up" in narrative_summary(
        rows, symbol="BTCUSDT", timeframe="1h", trend="ranging")

    rows[-1][5] = 9_000.0                    # far above it
    assert "expanding" in narrative_summary(
        rows, symbol="BTCUSDT", timeframe="1h", trend="ranging")


def test_snapshot_omits_what_it_cannot_compute():
    snap = indicator_snapshot(_candles(60))
    assert "rsi" in snap and "close" in snap
    assert "ema200" not in snap, "60 bars cannot support a 200-period average"


# ── The vote tally ───────────────────────────────────────────────────────────

def test_the_tally_always_adds_up():
    for drift in (-1.0, 0.0, 1.0):
        votes = bias_votes(indicator_snapshot(_candles(drift=drift)))
        assert votes["buy"] + votes["sell"] == votes["total"]
        assert votes["total"] <= 6


def test_indicators_with_no_data_abstain_rather_than_voting_sell():
    """An empty snapshot is unknown, not bearish."""
    votes = bias_votes({})
    assert (votes["buy"], votes["sell"], votes["total"]) == (0, 0, 0)
    assert votes["label"] == "Unknown"


def test_no_bias_line_is_printed_when_nothing_could_vote():
    """The line exists to summarise votes; with none it would be an empty claim."""
    text = narrative_summary(_candles(25), symbol="BTCUSDT", timeframe="1h",
                             trend="ranging")
    if text:
        assert "Bias:" not in text or "/0" not in text


#: (snapshot, expected buy count, expected label) — each hand-built so the
#: votes are countable by eye.
_ALL_BULL = {"close": 100.0, "ema50": 90.0, "ema200": 80.0,
             "rsi": 60.0, "macd_hist": 1.0, "pct_b": 0.8, "stoch": 60.0}
_ALL_BEAR = {"close": 70.0, "ema50": 90.0, "ema200": 100.0,
             "rsi": 40.0, "macd_hist": -1.0, "pct_b": 0.2, "stoch": 40.0}
#: RSI, price-vs-EMA50 and %B bullish; MACD, Stochastic and the EMA cross bearish.
_SPLIT = {"close": 100.0, "ema50": 90.0, "ema200": 100.0,
          "rsi": 60.0, "macd_hist": -1.0, "pct_b": 0.8, "stoch": 40.0}


@pytest.mark.parametrize(("snap", "buy", "expected"), [
    (_ALL_BULL, 6, "Bullish"),
    (_SPLIT, 3, "Neutral"),
    (_ALL_BEAR, 0, "Bearish"),
])
def test_the_label_follows_the_votes(snap, buy, expected):
    votes = bias_votes(snap)
    assert (votes["buy"], votes["total"]) == (buy, 6)
    assert votes["label"] == expected


# ── Naming ───────────────────────────────────────────────────────────────────

def test_known_tickers_are_named_and_unknown_ones_are_left_alone():
    assert display_name("SOLUSDT") == "Solana"
    assert display_name("XAUUSD") == "Gold"
    assert display_name("WOTSIT") == "WOTSIT"
