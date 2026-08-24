"""
Economic calendar: backfilling ForexFactory's missing ``actual`` from TradingView.

ForexFactory's JSON feed has no ``actual`` column — only forecast and previous
— so released numbers are stitched in from the TradingView pull. Everything at
a shared timestamp is a chance to publish the wrong number under the right
name: NFP, the unemployment rate and average hourly earnings all print at
13:30. What is under test is that the matcher:

  * pairs the same release across two naming conventions
  * refuses a m/m row a y/y number, and a level a percentage
  * refuses anything it cannot corroborate, leaving ``actual`` blank
  * prints what it does fill in the style of the row's own values

No network: every event is built by hand.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plugins.MT5TradingPlugin.backend.services import economic_calendar as ec  # noqa: E402

WHEN = datetime(2026, 8, 7, 12, 30, tzinfo=timezone.utc)


def ff(title, *, currency="USD", forecast=None, previous=None, when=WHEN):
    return ec._event(
        title=title, currency=currency, impact="high", when=when,
        source="ForexFactory", forecast=forecast, previous=previous, actual=None,
    )


def tv(title, *, currency="USD", forecast=None, previous=None, actual=None, when=WHEN):
    return ec._event(
        title=title, currency=currency, impact="high", when=when,
        source="TradingView", forecast=forecast, previous=previous, actual=actual,
    )


def test_pairs_the_same_release_across_naming_conventions():
    rows = [ff("Unemployment Rate", forecast="4.2%", previous="4.2%")]
    filled = ec._backfill_actuals(rows, [tv("Unemployment Rate", previous="4.2%", actual="4.1%")])

    assert filled == 1
    assert rows[0]["actual"] == "4.1%"


def test_house_style_differences_are_not_identity():
    """"Non-Farm Employment Change" and "Non Farm Payrolls" are one release."""
    rows = [ff("Non-Farm Employment Change", forecast="85K", previous="57K")]
    ec._backfill_actuals(rows, [tv("Non Farm Payrolls", previous="20K", actual="-23K")])

    assert rows[0]["actual"] == "-23K"


def test_neighbours_at_the_same_minute_do_not_bleed():
    """The 13:30 crowd — every row must take its own number or none."""
    rows = [
        ff("Non-Farm Employment Change", forecast="85K", previous="57K"),
        ff("Unemployment Rate", forecast="4.2%", previous="4.2%"),
        ff("Average Hourly Earnings m/m", forecast="0.3%", previous="0.3%"),
    ]
    ec._backfill_actuals(rows, [
        tv("Non Farm Payrolls", previous="20K", actual="-23K"),
        tv("Unemployment Rate", previous="4.2%", actual="4.1%"),
        tv("Average Hourly Earnings MoM", previous="0.3%", actual="0.1%"),
        tv("Average Hourly Earnings YoY", previous="3.4%", actual="3.2%"),
    ])

    assert [r["actual"] for r in rows] == ["-23K", "4.1%", "0.1%"]


def test_a_period_tag_the_other_side_lacks_blocks_the_match():
    """FF's "y/y" is a percentage change; TradingView's untagged row is a level."""
    rows = [ff("Challenger Job Cuts y/y", previous="-4.5%")]
    filled = ec._backfill_actuals(rows, [tv("Challenger Job Cuts", previous="45.8K", actual="33.4K")])

    assert filled == 0
    assert rows[0]["actual"] is None


def test_a_percentage_never_takes_a_level():
    rows = [ff("Retail Sales m/m", forecast="0.1%", previous="0.2%")]
    filled = ec._backfill_actuals(rows, [tv("Retail Sales MoM", previous="120.4", actual="121.9")])

    assert filled == 0
    assert rows[0]["actual"] is None


def test_an_unrelated_release_is_left_alone():
    rows = [ff("Crude Oil Inventories", previous="-7.2M")]
    filled = ec._backfill_actuals(rows, [tv("EIA Distillate Stocks Change", previous="1.1M", actual="2.4M")])

    assert filled == 0
    assert rows[0]["actual"] is None


def test_a_disagreeing_previous_vetoes_a_loose_name_match():
    """Close names plus a different prior print is two series, not one."""
    rows = [ff("Manufacturing PMI", currency="CHF", previous="54.3")]
    filled = ec._backfill_actuals(
        rows, [tv("Manufacturing PMI Employment", currency="CHF", previous="48.1", actual="49.0")]
    )

    assert filled == 0


def test_actual_is_printed_in_the_rows_own_style():
    rows = [
        ff("Unemployment Claims", forecast="203K", previous="197K"),
        ff("Trade Balance", forecast="-73.0B", previous="-77.6B"),
    ]
    ec._backfill_actuals(rows, [
        tv("Initial Jobless Claims", previous="198K", actual="199.4K"),
        tv("Balance of Trade", previous="-77.6B", actual="-73.28B"),
    ])

    assert rows[0]["actual"] == "199K"
    assert rows[1]["actual"] == "-73.3B"


def test_forexfactory_keeps_its_own_forecast_and_previous():
    """TradingView fills gaps; it never overwrites what ForexFactory published."""
    rows = [ff("Unemployment Rate", forecast="4.2%", previous="4.2%")]
    ec._backfill_actuals(rows, [tv("Unemployment Rate", forecast="4.3%", previous="4.1%", actual="4.1%")])

    assert (rows[0]["forecast"], rows[0]["previous"]) == ("4.2%", "4.2%")


def test_a_blank_forecast_is_filled_from_tradingview():
    rows = [ff("Unemployment Rate", forecast=None, previous="4.2%")]
    ec._backfill_actuals(rows, [tv("Unemployment Rate", forecast="4.2%", previous="4.2%", actual="4.1%")])

    assert rows[0]["forecast"] == "4.2%"


def test_a_different_minute_is_a_different_event():
    rows = [ff("Unemployment Rate", previous="4.2%")]
    filled = ec._backfill_actuals(rows, [
        tv("Unemployment Rate", previous="4.2%", actual="4.1%",
           when=datetime(2026, 8, 7, 13, 30, tzinfo=timezone.utc)),
    ])

    assert filled == 0


def test_a_different_currency_is_a_different_event():
    rows = [ff("Unemployment Rate", currency="CAD", previous="6.5%")]
    filled = ec._backfill_actuals(rows, [tv("Unemployment Rate", currency="USD", previous="4.2%", actual="4.1%")])

    assert filled == 0


def test_tradingview_values_carry_their_unit():
    """The feed splits number from unit; the page shows one string."""
    raw = {"unit": "%", "scale": None}
    assert ec._join_unit(4.25, ec._tv_suffix(raw)) == "4.25%"

    raw = {"unit": None, "scale": "K"}
    assert ec._join_unit(197.0, ec._tv_suffix(raw)) == "197K"

    # ForexFactory drops the currency symbol and keeps the magnitude.
    raw = {"unit": "$", "scale": "B"}
    assert ec._join_unit(-73.3, ec._tv_suffix(raw)) == "-73.3B"


def test_a_fraction_survives_a_whole_number_style():
    """"-36" must not round a 0.4 print to "0"."""
    assert ec._format_like(0.4, ["-36"]) == "0.4"
    assert ec._format_like(-35.0, ["-36"]) == "-35"


@pytest.mark.asyncio
async def test_a_rate_limited_forexfactory_does_not_drop_its_week(monkeypatch):
    """A 429 must not hand this week's rows over to TradingView's naming."""
    cached = ff("Non-Farm Employment Change", forecast="85K", previous="57K")
    cached["actual"] = "-23K"
    monkeypatch.setattr(ec, "_cache", [cached])
    monkeypatch.setattr(ec, "_cache_at", 0.0)

    async def _rate_limited():
        return []

    async def _tv_only():
        return [tv("Non Farm Payrolls", previous="20K", actual="-23K",
                   when=datetime(2026, 8, 14, 12, 30, tzinfo=timezone.utc))]

    monkeypatch.setattr(ec, "_fetch_forexfactory", _rate_limited)
    monkeypatch.setattr(ec, "_fetch_tradingview", _tv_only)

    window = await ec.fetch_calendar_window(force=True)
    titles = [e["title"] for e in window]

    assert "Non-Farm Employment Change" in titles, "the cached week survives a 429"
    assert window[0]["actual"] == "-23K", "and keeps the value already backfilled"


def test_the_biggest_release_is_recognised_in_either_vocabulary():
    """A rate-limited ForexFactory leaves TradingView naming the dates."""
    assert ec.matches_high_impact_keyword("Non-Farm Employment Change") is True
    assert ec.matches_high_impact_keyword("Non Farm Payrolls") is True
    assert ec.matches_high_impact_keyword("Unemployment Claims") is True
    assert ec.matches_high_impact_keyword("Initial Jobless Claims") is True
    assert ec.matches_high_impact_keyword("CPI m/m") is True
    assert ec.matches_high_impact_keyword("Inflation Rate MoM") is True
    assert ec.matches_high_impact_keyword("Bank Holiday") is False


def test_agent_block_leads_with_the_released_number():
    events = [{
        "time_utc": "2026-08-07 12:30", "currency": "USD",
        "title": "Non-Farm Employment Change",
        "actual": "-23K", "forecast": "85K", "previous": "57K",
    }]
    block = ec.format_for_agents(events)

    assert "ACTUAL -23K" in block
    assert block.index("ACTUAL") < block.index("forecast")
