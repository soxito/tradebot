"""Levels drawn on a screenshot must land on the price they claim.

The calibration numbers here are read off a real TradingView mobile screenshot
of XAUUSD (590x1280): the axis prints 4,800.000 at y=141px and 4,000.000 at
y=755px, and the live quote 4,376.820 sits at y≈466px. If the mapper cannot
reproduce those positions from the axis alone, every plan level drawn on a
user's chart is in the wrong place — which is worse than drawing nothing.
"""

from __future__ import annotations

import io

import pytest

from plugins.AiMarketAnalyst.backend.services.chart_annotate import annotate, price_to_y

_H = 1280
_W = 590

#: The axis as the vision model is asked to report it: two far-apart labels
#: with their height as a percentage of the image.
_AXIS = {
    "top_price": 4800.0, "top_y_pct": 141 / _H * 100,
    "bottom_price": 4000.0, "bottom_y_pct": 755 / _H * 100,
}


def _screenshot(w: int = _W, h: int = _H) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (w, h), (13, 17, 28)).save(out, format="PNG")
    return out.getvalue()


# ── The price→pixel mapping ──────────────────────────────────────────────────

@pytest.mark.parametrize(("price", "expected_y"), [
    (4800.0, 141),   # the calibration points themselves
    (4000.0, 755),
    (4500.0, 371),   # printed on the real screenshot at y=371
    (4376.82, 466),  # the live quote chip
    (4400.0, 448),
])
def test_a_price_lands_where_the_real_chart_prints_it(price, expected_y):
    mapper = price_to_y({"axis": _AXIS}, _H)
    assert mapper is not None
    assert mapper(price) == pytest.approx(expected_y, abs=3)


def test_a_price_off_the_visible_scale_is_refused_not_clamped():
    """A level squashed onto the top edge would read as a real level there."""
    mapper = price_to_y({"axis": _AXIS}, _H)
    assert mapper(9000.0) is None
    assert mapper(1000.0) is None


def test_no_axis_means_no_mapper_rather_than_a_guess():
    assert price_to_y({}, _H, axis_only=True) is None
    assert price_to_y({"axis": {"top_price": 4800, "top_y_pct": 11}}, _H) is None


def test_a_flat_axis_is_rejected():
    """Two identical prices fix no scale."""
    axis = {"top_price": 4800, "top_y_pct": 10, "bottom_price": 4800, "bottom_y_pct": 60}
    assert price_to_y({"axis": axis}, _H) is None


def test_levels_alone_can_still_calibrate_a_plan():
    """No axis block, but priced levels — the fit is the fallback, not a guess."""
    findings = {"levels": [
        {"price": "4,800.000", "y_pct": 11.0},
        {"price": "4,000.000", "y_pct": 59.0},
    ]}
    mapper = price_to_y(findings, _H)
    assert mapper is not None
    assert mapper(4400.0) == pytest.approx(448, abs=8)
    # …but that fit must not be reused to move those same levels.
    assert price_to_y(findings, _H, axis_only=True) is None


# ── What actually gets drawn ─────────────────────────────────────────────────

def test_a_priced_level_is_placed_by_the_axis_not_the_eyeballed_position():
    """The model reads the number but estimates the height; trust the number."""
    findings = {
        "axis": _AXIS,
        # y_pct deliberately wrong by ~20% of the image height.
        "levels": [{"label": "Weak High", "price": "4400", "y_pct": 15.0,
                    "kind": "resistance"}],
    }
    out = annotate(_screenshot(), findings)
    assert out

    from PIL import Image

    img = Image.open(io.BytesIO(out))
    # The resistance line is red; find the rows carrying it.
    red_rows = [
        y for y in range(_H)
        if img.getpixel((_W // 2, y))[0] > 200 and img.getpixel((_W // 2, y))[1] < 120
    ]
    assert red_rows, "no level line was drawn"
    # It must sit at the calibrated 4400 (y≈448), not the claimed 15% (y=192).
    assert min(red_rows, key=lambda y: abs(y - 448)) == pytest.approx(448, abs=4)
    assert all(abs(y - 192) > 20 for y in red_rows)


def test_an_unpriced_level_still_uses_the_position_the_model_gave():
    findings = {"axis": _AXIS, "levels": [{"label": "CHoCH", "y_pct": 40.0}]}
    assert annotate(_screenshot(), findings)


def test_labels_that_would_collide_are_separated():
    """Several levels near the top would otherwise pile into one unreadable chip."""
    findings = {
        "axis": _AXIS,
        "levels": [
            {"label": "Strong High", "price": "4790", "kind": "resistance"},
            {"label": "Weak High", "price": "4785", "kind": "resistance"},
            {"label": "EQH", "price": "4780", "kind": "resistance"},
        ],
    }
    out = annotate(_screenshot(), findings)
    assert out

    from PIL import Image

    img = Image.open(io.BytesIO(out)).convert("RGB")
    # Each chip is a filled block on the left edge; count distinct bands.
    filled = [y for y in range(_H) if img.getpixel((12, y)) != (13, 17, 28)]
    bands, prev = 0, -99
    for y in filled:
        if y - prev > 1:
            bands += 1
        prev = y
    assert bands >= 3, "collided labels were drawn on top of each other"


def test_nothing_to_draw_returns_nothing():
    assert annotate(_screenshot(), {}) is None


def test_a_plan_is_not_drawn_when_the_axis_cannot_be_read():
    """Guessed placement on a real chart is worse than no plan line."""
    plan = {"proposed_entry": 4376.8, "sl": 4320.0, "tp1": 4440.0, "side": "long"}
    assert annotate(_screenshot(), {"levels": []}, plan) is None
