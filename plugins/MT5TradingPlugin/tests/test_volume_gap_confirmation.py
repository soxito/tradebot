"""A gap in the volume feed is not evidence of thin trading.

Yahoo's 60m gold series carries ~15 zero-volume bars in 400 (thin hours,
contract rollover, holidays); its 15m series carries none. A zone forming on
one of those bars scored ``rel_vol = 0 / mean = 0`` and was hard-rejected by the
volume gate exactly as if it had genuinely traded on no participation — which is
why H1 gold could publish no setups while M15 on the same instrument published
several.

The engine already draws this distinction at the instrument level: an instrument
publishing no volume at all is scored ``structure_only`` rather than banned.
These tests hold it to the same reasoning one bar at a time.
"""

from __future__ import annotations

import pytest

from plugins.MT5TradingPlugin.backend.services import smc_scoring as S


class _Bar:
    def __init__(self, o, h, l, c, v):
        self.open, self.high, self.low, self.close, self.volume = o, h, l, c, v


def _bars(vols):
    return [_Bar(100.0, 101.0, 99.0, 100.5, v) for v in vols]


# ── The gap itself ───────────────────────────────────────────────────────────

def test_zero_volume_bar_in_a_volumed_series_is_treated_as_unknown():
    """rel_vol of 0 here means "not reported", not "nobody traded"."""
    candles = _bars([1000.0] * 30 + [0.0])
    idx = len(candles) - 1

    assert S.relative_volume(candles, idx) == 0.0
    assert S.rolling_mean_volume(candles, idx) > 0, "the series does report volume"

    b = S.score_signal(
        side="sell", zone_index=idx, zone_kind="bearish_ob",
        zone_top=101.0, zone_bottom=99.0, entry=100.0, rr=2.0,
        min_rr=1.5, max_rr=3.0, candles=candles, atr=1.0,
        bias="bearish", events=[], sweeps={}, equilibrium=100.0,
        range_low=95.0, range_high=105.0, choch_index=-1, htf_bias=None,
    )
    assert b.structure_only is True, "a feed gap should fall back to structure scoring"
    assert b.volume_confirmed is True, (
        "a missing measurement must not be scored as a failed one — this is what "
        "suppressed every H1 gold setup"
    )
    assert b.volume_data_available is False


def test_a_genuinely_thin_bar_is_still_rejected():
    """The gate must keep catching real low-participation breaks."""
    candles = _bars([1000.0] * 30 + [50.0])
    idx = len(candles) - 1

    b = S.score_signal(
        side="sell", zone_index=idx, zone_kind="bearish_ob",
        zone_top=101.0, zone_bottom=99.0, entry=100.0, rr=2.0,
        min_rr=1.5, max_rr=3.0, candles=candles, atr=1.0,
        bias="bearish", events=[], sweeps={}, equilibrium=100.0,
        range_low=95.0, range_high=105.0, choch_index=-1, htf_bias=None,
    )
    assert b.structure_only is False, "0.05x volume is measured, not missing"
    assert b.volume_confirmed is False, "a thin zone bar must still be gated out"


def test_an_above_average_bar_still_confirms():
    candles = _bars([1000.0] * 30 + [2500.0])
    idx = len(candles) - 1

    b = S.score_signal(
        side="sell", zone_index=idx, zone_kind="bearish_ob",
        zone_top=101.0, zone_bottom=99.0, entry=100.0, rr=2.0,
        min_rr=1.5, max_rr=3.0, candles=candles, atr=1.0,
        bias="bearish", events=[], sweeps={}, equilibrium=100.0,
        range_low=95.0, range_high=105.0, choch_index=-1, htf_bias=None,
    )
    assert b.volume_confirmed is True
    assert b.structure_only is False


def test_an_instrument_with_no_volume_at_all_is_unchanged():
    """The pre-existing structure_only behaviour must survive."""
    candles = _bars([0.0] * 31)
    idx = len(candles) - 1

    b = S.score_signal(
        side="sell", zone_index=idx, zone_kind="bearish_ob",
        zone_top=101.0, zone_bottom=99.0, entry=100.0, rr=2.0,
        min_rr=1.5, max_rr=3.0, candles=candles, atr=1.0,
        bias="bearish", events=[], sweeps={}, equilibrium=100.0,
        range_low=95.0, range_high=105.0, choch_index=-1, htf_bias=None,
    )
    assert b.structure_only is True
    assert b.volume_confirmed is True


# ── The same reasoning on the break-of-structure bar ──────────────────────────

def test_bos_gate_abstains_on_a_gap_but_condemns_a_thin_break():
    volumed = _bars([1000.0] * 30 + [0.0])
    ok, ratio = S.bos_volume_confirmed(volumed, len(volumed) - 1)
    assert ok is True, "a break bar with no reported volume cannot be judged thin"
    assert ratio == 0.0, "and it earns no credit either"

    thin = _bars([1000.0] * 30 + [100.0])
    ok, ratio = S.bos_volume_confirmed(thin, len(thin) - 1)
    assert ok is False, "a measured thin break is still the classic false break"

    strong = _bars([1000.0] * 30 + [2000.0])
    ok, _ = S.bos_volume_confirmed(strong, len(strong) - 1)
    assert ok is True


def test_bos_gate_still_rejects_an_out_of_range_index():
    assert S.bos_volume_confirmed(_bars([1000.0] * 5), 99) == (False, 0.0)
    assert S.bos_volume_confirmed(_bars([1000.0] * 5), -1) == (False, 0.0)
