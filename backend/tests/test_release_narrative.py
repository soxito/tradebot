"""A release read must never contradict the numbers printed above it.

Two failure modes are covered here specifically: summarising a mixed batch as
if it were unanimous, and interpreting a release whose direction we have no
rule for. Both produce confident text that the figures do not support, which
is worse than saying less.
"""

from __future__ import annotations

import pytest

from app.signals.release_narrative import parse_value, read_event, release_narrative


def _event(title, actual, forecast, previous="0.0%", currency="USD"):
    return {"title": title, "currency": currency, "actual": actual,
            "forecast": forecast, "previous": previous, "date": "2026-08-13"}


# ── Parsing ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("raw", "expected"), [
    ("4.2%", 4.2), ("197K", 197_000.0), ("-0.3%", -0.3), ("1.2B", 1.2e9),
    ("2,150", 2150.0), (0.5, 0.5),
])
def test_feed_values_are_read_in_their_own_units(raw, expected):
    assert parse_value(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", [None, "", "-", "—", "n/a"])
def test_a_blank_value_is_not_a_zero(raw):
    """Reading a missing figure as 0 would invent a catastrophic miss."""
    assert parse_value(raw) is None


# ── What counts as a result ──────────────────────────────────────────────────

def test_an_event_that_has_not_printed_is_not_reported():
    """A forecast is not a result."""
    assert read_event(_event("Core CPI m/m", None, "0.3%")) is None
    assert release_narrative([_event("Core CPI m/m", None, "0.3%")]) == ""


def test_an_event_with_no_forecast_has_nothing_to_beat():
    assert read_event(_event("Core CPI m/m", "0.2%", None)) is None


# ── Direction ────────────────────────────────────────────────────────────────

def test_a_hot_inflation_print_is_currency_positive():
    read = read_event(_event("Core CPI m/m", "0.5%", "0.3%"))
    assert read["surprise"] == "above" and read["bias"] > 0


def test_a_soft_inflation_print_is_currency_negative():
    read = read_event(_event("Core PPI m/m", "0.2%", "0.3%"))
    assert read["surprise"] == "below" and read["bias"] < 0


def test_rising_unemployment_is_currency_negative_despite_being_a_beat():
    """More people out of work is not a strong economy — direction is inverted."""
    read = read_event(_event("Unemployment Rate", "4.5%", "4.1%"))
    assert read["surprise"] == "above" and read["bias"] < 0


def test_the_weekly_claims_print_is_recognised_however_it_is_worded():
    for title in ("Unemployment Claims", "Initial Jobless Claims", "Continuing Claims"):
        read = read_event(_event(title, "209K", "202K"))
        assert read["bias"] < 0, f"{title} should read as currency-negative"


def test_a_release_with_no_rule_is_reported_without_an_interpretation():
    read = read_event(_event("Natural Gas Storage", "36B", "31B"))
    assert read["interpretable"] is False
    assert read["bias"] == 0
    text = release_narrative([_event("Natural Gas Storage", "36B", "31B")])
    assert "depends on the detail" in text
    assert "Bullish" not in text and "Bearish" not in text


# ── The summary must match its own list ──────────────────────────────────────

def test_a_unanimous_batch_may_be_called_unanimous():
    text = release_narrative([
        _event("Core PPI m/m", "0.2%", "0.3%"),
        _event("PPI m/m", "0.0%", "0.2%"),
    ])
    assert "Every reading came in below forecast." in text
    assert "USD Bearish" in text and "Gold Bullish" in text


def test_a_mixed_batch_is_never_summarised_as_unanimous():
    """The regression: claims printed above while PPI printed below."""
    text = release_narrative([
        _event("Core PPI m/m", "0.2%", "0.3%"),
        _event("Unemployment Claims", "209K", "202K"),
    ])
    assert "Every reading" not in text


def test_opposing_prints_are_reported_as_a_split_not_forced_to_a_side():
    text = release_narrative([
        _event("Core CPI m/m", "0.5%", "0.3%"),      # hawkish
        _event("Unemployment Rate", "4.5%", "4.1%"),  # dovish
    ])
    assert "USD Neutral" in text
    assert "USD Bullish" not in text and "USD Bearish" not in text


def test_gold_is_read_opposite_the_dollar():
    hot = release_narrative([_event("Core CPI m/m", "0.5%", "0.3%")])
    assert "USD Bullish" in hot and "Gold Bearish" in hot


def test_only_the_requested_currency_is_read():
    text = release_narrative([
        _event("Core CPI m/m", "0.5%", "0.3%", currency="EUR"),
    ], currency="USD")
    assert text == ""


def test_a_long_batch_is_trimmed_to_the_releases_that_carry_a_read():
    events = [_event(f"Natural Gas Storage {i}", "36B", "31B") for i in range(8)]
    events.append(_event("Core CPI m/m", "0.5%", "0.3%"))
    text = release_narrative(events, limit=3)
    assert "Core CPI m/m" in text
    assert text.count("• ") <= 3
