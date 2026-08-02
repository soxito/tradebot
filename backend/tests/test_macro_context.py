"""The macro factor must know where the dollar sits, and when to say nothing.

Two failure modes are worth more than all the rest, and both are pinned here:

  * applying the dollar to a pair it says nothing about (EURGBP), or applying it
    with the wrong sign (USDJPY reads opposite to EURUSD);
  * letting a failed fetch score as a headwind, which would quietly make every
    signal worse during an outage.
"""
from __future__ import annotations

import pytest

from app.services import macro_context as mc


def _bars(closes, *, start=1_760_000_000, step=86_400):
    """Daily candles, oldest first, in the shape yahoo_provider returns."""
    return [
        {"time": start + i * step, "open": c, "high": c, "low": c, "close": c, "volume": 0}
        for i, c in enumerate(closes)
    ]


def _now(bars):
    return bars[-1]["time"] + 3600


# ── currency composition ─────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        # USD on the quote side — a bid dollar prices these lower.
        ("EURUSD", "quote"), ("GBPUSD", "quote"), ("AUDUSD", "quote"),
        ("NZDUSD", "quote"), ("XAUUSD", "quote"), ("XAGUSD", "quote"),
        ("BTCUSDT", "quote"), ("ETHUSDT", "quote"), ("BTCUSD", "quote"),
        # USD on the base side — the same dollar prices these higher.
        ("USDJPY", "base"), ("USDCHF", "base"), ("USDCAD", "base"),
        ("USDZAR", "base"), ("USDMXN", "base"),
        # No dollar leg at all.
        ("EURGBP", "none"), ("GBPJPY", "none"), ("AUDNZD", "none"),
        ("XAUEUR", "none"), ("XAGEUR", "none"),
    ],
)
def test_usd_leg_knows_which_side_the_dollar_is_on(symbol, expected):
    assert mc.usd_leg(symbol) == expected


@pytest.mark.parametrize("symbol", ["US30", "NAS100", "US500", "USOIL", "UKOIL", "NGAS"])
def test_named_usd_denominated_instruments_are_quote_leg(symbol):
    """Indices and energy are dollar-priced without ever being a currency pair."""
    assert mc.usd_leg(symbol) == "quote"


@pytest.mark.parametrize("symbol", ["XTIUSD", "XBRUSD", "XNGUSD"])
def test_energy_spellings_are_not_mistaken_for_fx(symbol):
    """XTI is no currency — resolving these through the FX split would misread them."""
    assert mc.usd_leg(symbol) == "quote"


# ── the snapshot ─────────────────────────────────────────────────────────────

def test_a_rising_dollar_and_a_spiking_vix_read_risk_off():
    dxy = _bars([99.0 + i * 0.15 for i in range(20)])
    vix = _bars([14.0 + i * 0.9 for i in range(20)])
    snap = mc.build_macro_snapshot(dxy, vix, now=_now(dxy))

    assert snap.status == "OK"
    assert snap.regime == "RISK_OFF"
    assert snap.dxy_level == pytest.approx(101.85, abs=0.01)
    assert snap.dxy_change_pct is not None and snap.dxy_change_pct > 0


def test_a_falling_dollar_and_a_calm_vix_read_risk_on():
    dxy = _bars([102.0 - i * 0.15 for i in range(20)])
    vix = _bars([16.0 - i * 0.05 for i in range(20)])
    snap = mc.build_macro_snapshot(dxy, vix, now=_now(dxy))

    assert snap.status == "OK"
    assert snap.regime == "RISK_ON"


def test_a_flat_tape_is_neutral_not_a_direction():
    flat_dxy = _bars([100.0] * 20)
    flat_vix = _bars([VIX := 20.0] * 20)
    snap = mc.build_macro_snapshot(flat_dxy, flat_vix, now=_now(flat_dxy))

    assert snap.status == "OK"
    assert snap.regime == "NEUTRAL"


def test_bars_older_than_a_long_weekend_are_stale():
    dxy = _bars([100.0 + i * 0.1 for i in range(20)])
    vix = _bars([16.0] * 20)
    snap = mc.build_macro_snapshot(dxy, vix, now=_now(dxy) + mc.MAX_AGE_S + 3600)

    assert snap.status == "STALE"
    assert snap.regime == "UNKNOWN"


def test_no_usable_bars_is_unavailable_not_an_exception():
    snap = mc.build_macro_snapshot([], [])
    assert snap.status == "UNAVAILABLE"
    assert snap.detail


def test_nulls_in_the_series_are_skipped():
    """Yahoo returns nulls on holidays; they must not become zero closes."""
    rows = _bars([100.0, 100.5, 101.0])
    rows[1]["close"] = None
    snap = mc.build_macro_snapshot(rows, _bars([16.0, 16.2, 16.1]), now=_now(rows))
    assert snap.status == "OK"
    assert snap.dxy_level == pytest.approx(101.0)


# ── resolution against one instrument ────────────────────────────────────────

def _risk_off_snapshot():
    dxy = _bars([99.0 + i * 0.15 for i in range(20)])
    vix = _bars([14.0 + i * 0.9 for i in range(20)])
    return mc.build_macro_snapshot(dxy, vix, now=_now(dxy))


def test_the_dollar_reads_opposite_on_the_two_sides_of_a_pair():
    snap = _risk_off_snapshot()
    eurusd = mc.macro_bias("EURUSD", snap)
    usdjpy = mc.macro_bias("USDJPY", snap)

    assert eurusd.applicable and usdjpy.applicable
    assert eurusd.normalized < 0, "a bid dollar is a headwind for a EURUSD long"
    assert usdjpy.normalized > 0, "the same dollar is a tailwind for a USDJPY long"


def test_a_pair_with_no_dollar_leg_is_not_applicable():
    bias = mc.macro_bias("EURGBP", _risk_off_snapshot())

    assert bias.applicable is False
    assert bias.normalized == 0.0
    assert "no USD leg" in bias.reason
    # It must say so rather than going quiet — the user asked for the signal to
    # explain itself even when the factor sits out.
    assert any("does not apply" in line for line in bias.lines)


def test_crypto_carries_both_the_dollar_and_the_fear_gauge():
    bias = mc.macro_bias("BTCUSDT", _risk_off_snapshot())

    assert bias.applicable is True
    assert bias.normalized < 0
    assert any("DXY" in line for line in bias.lines)
    assert any("VIX" in line for line in bias.lines)


def test_an_fx_major_does_not_get_a_vix_line():
    """VIX is a risk-asset gauge; EURUSD is scored on the dollar alone."""
    bias = mc.macro_bias("EURUSD", _risk_off_snapshot())
    assert not any("VIX" in line for line in bias.lines)


@pytest.mark.parametrize(
    "snap",
    [None,
     mc.MacroSnapshot(status="UNAVAILABLE", detail="feed down"),
     mc.MacroSnapshot(status="STALE", detail="two weeks old")],
)
def test_an_unreadable_snapshot_is_no_opinion_never_a_headwind(snap):
    """The single most dangerous failure: an outage scoring as bearish."""
    bias = mc.macro_bias("XAUUSD", snap)

    assert bias.applicable is False
    assert bias.normalized == 0.0
    assert "unavailable" in bias.reason


def test_evidence_lines_are_safe_at_every_status():
    for snap in (None, mc.MacroSnapshot(status="UNAVAILABLE"), _risk_off_snapshot()):
        bias = mc.macro_bias("XAUUSD", snap)
        lines = mc.macro_evidence_lines(bias, snap)
        assert lines and all(isinstance(line, str) and line for line in lines)
    assert mc.macro_evidence_lines(None)


# ── the resolver ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_snapshot_is_cached_so_a_backtest_cannot_storm_the_feed():
    mc.reset_cache()
    calls = {"n": 0}

    async def _fetcher(symbol, timeframe, limit):
        calls["n"] += 1
        return _bars([100.0 + i * 0.05 for i in range(20)])

    first = await mc.resolve_macro_snapshot(fetcher=_fetcher, now=1_000_000.0)
    second = await mc.resolve_macro_snapshot(fetcher=_fetcher, now=1_000_060.0)

    assert first is second
    assert calls["n"] == 2, "one fetch per series, then served from cache"

    await mc.resolve_macro_snapshot(fetcher=_fetcher, now=1_000_000.0 + mc.CACHE_TTL_S + 1)
    assert calls["n"] == 4, "the cache expires"
    mc.reset_cache()


@pytest.mark.asyncio
async def test_a_throwing_fetcher_degrades_instead_of_propagating():
    mc.reset_cache()

    async def _boom(symbol, timeframe, limit):
        raise RuntimeError("yahoo down")

    snap = await mc.resolve_macro_snapshot(fetcher=_boom, now=2_000_000.0)
    assert snap.status == "UNAVAILABLE"
    assert "yahoo down" in snap.detail

    bias = await mc.resolve_macro_bias("XAUUSD", fetcher=_boom, now=2_000_000.0)
    assert bias.applicable is False and bias.normalized == 0.0
    mc.reset_cache()
