"""FX and metals candles must sit on the broker's price scale, not Yahoo's.

``yahoo_provider`` has no spot-metal ticker, so it charts gold off ``GC=F``, the
front-month COMEX future. A future carries cost of carry: measured on one tick,
``GC=F`` closed 4437.30 while Swissquote's XAU/USD closed 4376.19 — $61/oz,
1.38% apart. The chart's Friday close therefore sat $61 above where the pair
actually closed on the broker, and every level read off it (entry, stop, target)
was wrong by the same amount.

``anchor_ohlcv_to_swissquote`` rescales the series so its last close *is* the
Swissquote quote. The correction is multiplicative because the basis is a
percentage of price — and because a ratio leaves every bar's return, the only
thing the forecast model reads, exactly unchanged.

Companion to ``test_metals_spot``, which makes the same demand of the quote path.
"""

from __future__ import annotations

import pytest

from app.exchanges import forex_provider as fp


#: [ts_ms, open, high, low, close, volume] — a rising three-bar series.
ROWS = [
    [1_000_000, 100.0, 110.0, 95.0, 105.0, 12.0],
    [1_003_600, 105.0, 115.0, 102.0, 112.0, 34.0],
    [1_007_200, 112.0, 120.0, 108.0, 118.0, 56.0],
]


def _returns(rows):
    return [rows[i][4] / rows[i - 1][4] for i in range(1, len(rows))]


@pytest.fixture
def quote(monkeypatch):
    """Pin the Swissquote mid; None means 'Swissquote does not carry this pair'."""

    def _set(value):
        async def _fake(_symbol):
            return value

        monkeypatch.setattr(fp, "_fetch_swissquote_price", _fake)

    return _set


# ── The correction ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_last_close_becomes_the_swissquote_price(quote):
    quote(116.0)
    out = await fp.anchor_ohlcv_to_swissquote("XAUUSD", ROWS)
    assert out[-1][4] == pytest.approx(116.0)


@pytest.mark.asyncio
async def test_gold_basis_is_removed_at_realistic_magnitude(quote):
    """The measured case: a 1.38% futures premium, gone."""
    rows = [[1_000_000, 4430.0, 4445.0, 4420.0, 4437.30, 900.0]]
    quote(4376.185)
    out = await fp.anchor_ohlcv_to_swissquote("XAUUSD", rows)
    assert out[-1][4] == pytest.approx(4376.185)
    assert rows[-1][4] - out[-1][4] == pytest.approx(61.115, abs=1e-3)


@pytest.mark.asyncio
async def test_returns_survive_the_rescale(quote):
    """A constant offset would distort every bar's return; a ratio does not."""
    quote(116.0)
    out = await fp.anchor_ohlcv_to_swissquote("XAUUSD", ROWS)
    assert _returns(out) == pytest.approx(_returns(ROWS), rel=1e-12)


@pytest.mark.asyncio
async def test_bar_shape_and_volume_are_left_alone(quote):
    quote(116.0)
    out = await fp.anchor_ohlcv_to_swissquote("XAUUSD", ROWS)
    assert [r[0] for r in out] == [r[0] for r in ROWS]      # timestamps
    assert [r[5] for r in out] == [r[5] for r in ROWS]      # volume is not a price
    for r in out:                                           # low ≤ open/close ≤ high
        assert r[3] <= r[1] <= r[2]
        assert r[3] <= r[4] <= r[2]
    assert ROWS[-1][4] == 118.0                             # caller's rows untouched


# ── When it must not fire ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_implausible_basis_is_refused(quote):
    """A 40x gap is a symbol-mapping bug, not a basis.

    Rescaling there would dress another instrument's series up as this one —
    plausible-looking candles hiding the fault. Better to leave the rows as
    fetched and log, so the mismatch stays visible.
    """
    quote(4376.0)  # gold-scale quote against a 118-scale series
    assert await fp.anchor_ohlcv_to_swissquote("XAUUSD", ROWS) == ROWS


@pytest.mark.asyncio
async def test_pairs_swissquote_does_not_quote_pass_through(quote):
    quote(None)
    assert await fp.anchor_ohlcv_to_swissquote("US30", ROWS) == ROWS


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [[], [[1_000_000, 0.0, 0.0, 0.0, 0.0, 0.0]]])
async def test_degenerate_input_is_returned_as_is(quote, rows):
    quote(116.0)
    assert await fp.anchor_ohlcv_to_swissquote("XAUUSD", rows) == rows


# ── The dict-bar shape yahoo_provider emits ──────────────────────────────────

BARS = [
    {"time": 1_000, "open": 100.0, "high": 110.0, "low": 95.0, "close": 105.0, "volume": 12.0},
    {"time": 4_600, "open": 112.0, "high": 120.0, "low": 108.0, "close": 118.0, "volume": 56.0},
]


@pytest.mark.asyncio
async def test_dict_bars_anchor_the_same_way(quote):
    quote(116.0)
    out = await fp.anchor_bars_to_swissquote("XAUUSD", BARS)
    assert out[-1]["close"] == pytest.approx(116.0)
    assert out[0]["close"] / out[-1]["close"] == pytest.approx(105.0 / 118.0, rel=1e-12)
    assert [b["volume"] for b in out] == [12.0, 56.0]
    assert [b["time"] for b in out] == [1_000, 4_600]
    assert BARS[-1]["close"] == 118.0


@pytest.mark.asyncio
async def test_dict_bars_pass_through_when_unquoted(quote):
    quote(None)
    assert await fp.anchor_bars_to_swissquote("US30", BARS) == BARS


# ── Which instruments the Yahoo choke point re-bases ─────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "symbol,ticker,anchored",
    [
        ("EURUSD", "EURUSD=X", True),    # spot FX
        ("XAUUSD", "GC=F", True),        # metal, charted off its future
        ("XAGUSD", "SI=F", True),
        ("XAUEUR", None, True),          # cross synthesised from two USD legs
        ("US30", "YM=F", False),         # index CFD is quoted off this future already
        ("USOIL", "CL=F", False),
        ("BTCUSD", "BTC-USD", False),    # crypto belongs to the exchange, not the broker
    ],
)
async def test_only_fx_and_metals_are_rebased(monkeypatch, symbol, ticker, anchored):
    """Swissquote quotes BTC/USD as a CFD and would answer for it — crypto must
    still be excluded here, or re-basing would overrule the exchange the user
    trades it on."""
    from app.exchanges import yahoo_provider as yp

    called = []

    async def _fake(sym, bars):
        called.append(sym)
        return [dict(b, close=1.0) for b in bars]

    monkeypatch.setattr(fp, "anchor_bars_to_swissquote", _fake)
    out = await yp._to_swissquote_scale(symbol, ticker, BARS)
    assert bool(called) is anchored
    assert (out[-1]["close"] == 1.0) is anchored
