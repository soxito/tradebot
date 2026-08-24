"""Zones described as levels to watch must be ones price has not passed.

A zone behind price is history. Presenting it as a level to watch invites an
entry against a move that already happened, which is the specific mistake this
module exists to avoid.
"""

from __future__ import annotations

from app.signals.zone_narrative import zone_narrative, zones_ahead

#: Two gaps and an order block overhead, a gap and an order block below.
_ZONES = [
    {"kind": "bearish_fvg", "top": 4404.0, "bottom": 4398.0, "index": 90},
    {"kind": "bearish_fvg", "top": 4426.0, "bottom": 4420.0, "index": 80},
    {"kind": "bearish_ob", "top": 4444.0, "bottom": 4437.0, "index": 70},
    {"kind": "bullish_ob", "top": 4360.0, "bottom": 4350.0, "index": 60},
    {"kind": "bullish_fvg", "top": 4300.0, "bottom": 4290.0, "index": 50},
]


def test_zones_are_ordered_by_how_soon_price_reaches_them():
    ahead = zones_ahead(_ZONES, 4390.0, limit=3)
    assert [z["low"] for z in ahead["above"]] == [4398.0, 4420.0, 4437.0]
    assert [z["high"] for z in ahead["below"]] == [4360.0, 4300.0]


def test_a_zone_price_has_already_passed_is_not_offered_as_a_level():
    """Above 4404, that gap is behind price and must not read as resistance."""
    ahead = zones_ahead(_ZONES, 4410.0)
    assert all(z["low"] > 4410.0 for z in ahead["above"])
    assert 4398.0 not in [z["low"] for z in ahead["above"]]
    # It is now below price, where it belongs.
    assert 4404.0 in [z["high"] for z in ahead["below"]]


def test_a_zone_price_is_inside_is_claimed_by_neither_side():
    """It is being tested right now; calling it support or resistance pre-judges it."""
    ahead = zones_ahead(_ZONES, 4401.0)
    every = ahead["above"] + ahead["below"]
    assert all(not (z["low"] <= 4401.0 <= z["high"]) for z in every)


def test_order_blocks_are_named_supply_and_demand_gaps_are_reaction_levels():
    text = zone_narrative(_ZONES, 4390.0, timeframe="h1", limit=3)
    assert "Supply Zone: 4,437.00 - 4,444.00" in text
    assert "Demand Zone: 4,350.00 - 4,360.00" in text
    assert "Reaction Zone: 4,398.00 - 4,404.00" in text


def test_the_next_zone_up_is_only_reached_by_breaking_the_one_before_it():
    text = zone_narrative(_ZONES, 4390.0, timeframe="h1", limit=3)
    assert "Break & hold above 4,404.00" in text
    assert "If 4,404.00 is broken, this becomes the next zone" in text


def test_overlapping_zones_are_described_as_the_one_level_they_are():
    """The engine finds OBs and FVGs separately; the same shelf comes back twice."""
    overlapping = [
        {"kind": "bearish_fvg", "top": 4410.0, "bottom": 4400.0},
        {"kind": "bearish_ob", "top": 4415.0, "bottom": 4405.0},
    ]
    ahead = zones_ahead(overlapping, 4390.0)
    assert len(ahead["above"]) == 1
    assert (ahead["above"][0]["low"], ahead["above"][0]["high"]) == (4400.0, 4415.0)
    # An order block overlapping a gap is still an order block.
    assert ahead["above"][0]["kind"] == "bearish_ob"


def test_zones_that_do_not_touch_stay_separate():
    ahead = zones_ahead([
        {"kind": "bearish_fvg", "top": 4405.0, "bottom": 4400.0},
        {"kind": "bearish_fvg", "top": 4430.0, "bottom": 4425.0},
    ], 4390.0)
    assert len(ahead["above"]) == 2


def test_nothing_is_said_when_there_is_nothing_ahead():
    assert zone_narrative([], 4390.0) == ""
    assert zone_narrative(_ZONES, 0) == ""


def test_malformed_zones_are_skipped_rather_than_crashing():
    zones = _ZONES + [{"kind": "bearish_ob", "top": None, "bottom": "x"}]
    assert zone_narrative(zones, 4390.0)


def test_inverted_bounds_are_read_the_right_way_up():
    """top/bottom swapped must not silently drop the zone."""
    ahead = zones_ahead([{"kind": "bearish_ob", "top": 4398.0, "bottom": 4404.0}], 4390.0)
    assert [(z["low"], z["high"]) for z in ahead["above"]] == [(4398.0, 4404.0)]
