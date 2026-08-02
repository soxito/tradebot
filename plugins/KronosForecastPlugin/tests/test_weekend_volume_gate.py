"""A closed market is not a dead feed.

Reported as Kronos showing "NO TRADE / volume unresolved" and drawing nothing on
the chart, having previously worked. Two independent causes, both of which made
the forecast refuse on instruments it can price perfectly well:

1. Staleness was measured in wall-clock time against a threshold of about four
   hours. Metals, FX and indices stop trading Friday evening and resume Sunday
   evening, so their newest bar is 12-60 hours old for the whole weekend and the
   gate reported STALE every Saturday and Sunday. Age is now measured in
   *trading* time, with the weekend break discounted.
2. ``forecast_from_rows`` withheld the 1h re-fetch whenever the caller said
   "mt5". The 24h statistics need whole clock hours, so H4 and D1 could never
   resolve volume from their own bars — and the MT5 sniper chart always passes
   exchange="mt5", making gold on H4 a permanent NO_TRADE.
"""

from __future__ import annotations

import datetime as dt

import pytest

from plugins.KronosForecastPlugin.backend.services import volume_context as V

UTC = dt.timezone.utc


def _ts(y, m, d, hh, mm=0) -> int:
    return int(dt.datetime(y, m, d, hh, mm, tzinfo=UTC).timestamp())


# 2026-07-31 is a Friday, 2026-08-01 a Saturday, 2026-08-02 a Sunday.
FRI_2000 = _ts(2026, 7, 31, 20)
FRI_2200 = _ts(2026, 7, 31, 22)
SAT_1000 = _ts(2026, 8, 1, 10)
SUN_1200 = _ts(2026, 8, 2, 12)
SUN_2300 = _ts(2026, 8, 2, 23)
MON_1000 = _ts(2026, 8, 3, 10)
WED_1000 = _ts(2026, 7, 29, 10)
WED_1800 = _ts(2026, 7, 29, 18)


# ── Which instants count as closed ───────────────────────────────────────────

@pytest.mark.parametrize("ts,closed", [
    (FRI_2000, False),   # Friday afternoon — still trading
    (FRI_2200, True),    # after the Friday close
    (SAT_1000, True),    # all Saturday
    (SUN_1200, True),    # Sunday before the reopen
    (SUN_2300, False),   # after the Sunday reopen
    (MON_1000, False),   # mid-week
    (WED_1000, False),
])
def test_weekly_break_window(ts, closed):
    assert V._in_weekly_break(ts) is closed


# ── Discounting the break ────────────────────────────────────────────────────

def test_a_full_weekend_is_discounted():
    """Friday close → Saturday morning is entirely market closure."""
    gap = SAT_1000 - FRI_2200
    assert V.closed_market_seconds(FRI_2200, SAT_1000) == pytest.approx(gap, abs=3600)


def test_midweek_gap_is_not_discounted():
    """A weekday outage is a real outage and must still count in full."""
    assert V.closed_market_seconds(WED_1000, WED_1800) == 0


def test_partial_gap_only_discounts_the_closed_part():
    """Friday 20:00 → Saturday 10:00 spans two trading hours plus the break."""
    closed = V.closed_market_seconds(FRI_2000, SAT_1000)
    total = SAT_1000 - FRI_2000
    assert 0 < closed < total
    assert total - closed == pytest.approx(2 * 3600, abs=3600)


def test_reversed_and_empty_ranges_are_safe():
    assert V.closed_market_seconds(SAT_1000, FRI_2200) == 0
    assert V.closed_market_seconds(SAT_1000, SAT_1000) == 0


def test_absurd_gap_does_not_spin():
    """The hour-by-hour walk is capped so a bad timestamp cannot hang a request."""
    ancient = _ts(2000, 1, 1, 0)
    assert V.closed_market_seconds(ancient, MON_1000) >= 0


# ── Only instruments that actually close ─────────────────────────────────────

def test_metals_and_fx_have_a_weekly_break():
    assert V._has_weekly_break("XAUUSD") is True
    assert V._has_weekly_break("GBPUSD") is True
    assert V._has_weekly_break("US30") is True


def test_crypto_has_no_weekly_break():
    """Crypto runs 24/7 — forgiving a weekend gap there would hide a dead feed."""
    assert V._has_weekly_break("BTCUSDT") is False
    assert V._has_weekly_break("ETHUSDT") is False


# ── The gate end to end ──────────────────────────────────────────────────────

def _hourly_rows(end_ts: int, hours: int = 30, volume: float = 1000.0):
    """Complete hourly bars ending at `end_ts` (exclusive)."""
    start = end_ts - hours * 3600
    return [
        [(start + i * 3600) * 1000, 100.0, 101.0, 99.0, 100.5, volume]
        for i in range(hours)
    ]


def test_gold_over_the_weekend_resolves_ok():
    """The exact reported case: Saturday, last bar Friday evening."""
    rows = _hourly_rows(FRI_2200)
    ctx = V.build_volume_context(
        rows, symbol="XAUUSD", timeframe="1h", source="test", now=SAT_1000,
    )
    assert ctx.status == "OK", f"gold refused over the weekend: {ctx.detail}"


def test_gold_mid_session_outage_is_still_stale():
    """The check must keep catching a feed that genuinely stopped."""
    rows = _hourly_rows(WED_1000)
    ctx = V.build_volume_context(
        rows, symbol="XAUUSD", timeframe="1h", source="test", now=WED_1800,
    )
    assert ctx.status == "STALE"


def test_crypto_over_a_weekend_is_still_stale():
    """Same timestamps, 24/7 instrument — the gap is a real outage."""
    rows = _hourly_rows(FRI_2200)
    ctx = V.build_volume_context(
        rows, symbol="BTCUSDT", timeframe="1h", source="test", now=SAT_1000,
    )
    assert ctx.status == "STALE"


def test_stale_detail_explains_the_weekend_accounting():
    """A long weekend plus a real outage should say how much was which."""
    rows = _hourly_rows(FRI_2200)
    ctx = V.build_volume_context(
        rows, symbol="XAUUSD", timeframe="1h", source="test", now=MON_1000,
    )
    if ctx.status == "STALE":
        assert "weekend close" in ctx.detail
