"""The price scale is measured from the image, not estimated by the model.

A vision model reads the axis *numbers* correctly and then misjudges where they
sit — the estimate it returns for a label's height is routinely several percent
out. On a phone screenshot of gold, one percent is 1280 × 1% ≈ 13px ≈ $8, so a
stop drawn from that estimate is a stop at a price the chart never showed.

These tests hold the pixels to the standard the user asked for: a line claiming
a price lands on that price's row, within a couple of pixels.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plugins.AiMarketAnalyst.backend.services import chart_axis  # noqa: E402
from plugins.AiMarketAnalyst.backend.services.chart_annotate import price_to_y  # noqa: E402
from plugins.AiMarketAnalyst.tests.chart_fixtures import chart_png  # noqa: E402

#: The shapes users actually send: a phone screenshot, a desktop capture, a
#: light-theme chart, and one with no live-quote chip on the axis.
LAYOUTS = {
    "phone": {},
    "desktop": dict(
        width=2000, height=942, strip_px=110, first_label_y=120,
        label_gap=93, n_labels=8, top_price=4550.0, step=50.0, quote_price=4402.13,
    ),
    "light": dict(light=True),
    "no_quote_chip": dict(quote_price=None),
    "tall_phone": dict(
        width=828, height=1792, strip_px=120, first_label_y=200,
        label_gap=150, n_labels=9, step=5.0,
    ),
}


@pytest.mark.parametrize("layout", sorted(LAYOUTS))
def test_every_label_is_found_where_it_is_printed(layout):
    png, truth = chart_png(**LAYOUTS[layout])
    rows = chart_axis.label_rows(png)
    assert rows is not None, "no axis ladder found"
    assert len(rows) == len(truth)
    for found, expected in zip(sorted(rows), sorted(truth.values())):
        assert found == pytest.approx(expected, abs=2)


@pytest.mark.parametrize("layout", sorted(LAYOUTS))
def test_a_price_maps_to_the_row_the_chart_prints_it_on(layout):
    kwargs = LAYOUTS[layout]
    png, truth = chart_png(**kwargs)
    height = kwargs.get("height", 1280)

    mapper = price_to_y(
        {"axis_labels": sorted(truth, reverse=True)}, height, image_bytes=png,
    )
    assert mapper is not None, "the axis in the image was not usable"
    for price, row in truth.items():
        assert mapper(price) == pytest.approx(row, abs=2)


def test_a_price_between_labels_lands_between_them():
    """The whole point is levels that are not on a printed gridline."""
    png, truth = chart_png()
    mapper = price_to_y({"axis_labels": sorted(truth, reverse=True)}, 1280, image_bytes=png)
    # Labels every $10 = 106px, so $4 below 4450 is 4450's row + 42px.
    assert mapper(4446.0) == pytest.approx(truth[4450.0] + 42, abs=3)


def test_the_measured_axis_beats_a_wrong_estimate_from_the_model():
    """This is the reported bug: good numbers, badly placed, drawn anyway."""
    png, truth = chart_png()
    findings = {
        "axis_labels": sorted(truth, reverse=True),
        # The same two prices the model read, both placed ~7% too low — the
        # systematic kind of error these estimates actually have.
        "axis": {
            "top_price": 4460.0, "top_y_pct": 19.5,        # truly 12.5%
            "bottom_price": 4390.0, "bottom_y_pct": 77.5,  # truly 70.5%
        },
    }
    measured = price_to_y(findings, 1280, image_bytes=png)
    estimated = price_to_y(findings, 1280)

    assert measured(4430.0) == pytest.approx(truth[4430.0], abs=2)
    # The estimate is what users were seeing: off by a visible margin.
    assert abs(estimated(4430.0) - truth[4430.0]) > 40


def test_values_that_do_not_match_the_image_are_refused():
    """A label list with a rung missing must not be stretched onto the ladder."""
    png, truth = chart_png()
    partial = sorted(truth, reverse=True)[:4]
    assert chart_axis.calibrate(png, partial) is None


def test_the_scale_always_runs_the_way_the_chart_does():
    """Higher price, smaller y — whichever order the labels arrive in."""
    png, truth = chart_png()
    for order in (sorted(truth), sorted(truth, reverse=True)):
        fit = chart_axis.calibrate(png, order)
        assert fit is not None
        slope, _ = fit
        assert slope < 0


def test_labels_that_are_not_a_ladder_are_refused():
    png, _ = chart_png()
    assert chart_axis.calibrate(png, [4460.0, 4457.3, 4390.0, 4111.0]) is None


# ── Partial label lists: what the vision models actually return ──────────────

def test_a_partial_label_list_is_matched_when_the_quote_chip_places_it():
    """Measured: llama-3.2-11b reads the top four labels and stops.

    Four labels fit the eight-rung ladder in five different places, all of them
    perfectly — the ladder is regular, so sliding by a rung fits just as well.
    The chip is the one row whose price is known, so it picks the right one.
    """
    png, truth = chart_png()
    top_four = sorted(truth, reverse=True)[:4]

    fit = chart_axis.calibrate(png, top_four, anchor_price=4395.2)
    assert fit is not None
    slope, intercept = fit
    for price, row in truth.items():
        assert (slope * price + intercept) / 100 * 1280 == pytest.approx(row, abs=2)


def test_a_partial_list_with_no_anchor_is_refused_not_guessed():
    """Off by one rung is $10 of gold — a stop in the wrong place."""
    png, truth = chart_png()
    assert chart_axis.calibrate(png, sorted(truth, reverse=True)[:4]) is None


def test_a_partial_list_from_the_middle_of_the_axis_is_placed():
    png, truth = chart_png()
    middle = sorted(truth, reverse=True)[3:6]

    fit = chart_axis.calibrate(png, middle, anchor_price=4395.2)
    assert fit is not None
    slope, intercept = fit
    assert (slope * 4460.0 + intercept) / 100 * 1280 == pytest.approx(truth[4460.0], abs=2)


def test_an_anchor_that_matches_no_alignment_is_refused():
    """A wrong live price must not be forced onto the nearest rung."""
    png, truth = chart_png()
    top_four = sorted(truth, reverse=True)[:4]
    assert chart_axis.calibrate(png, top_four, anchor_price=1234.5) is None


def test_a_non_chart_image_yields_nothing():
    from PIL import Image
    import io

    buf = io.BytesIO()
    Image.new("RGB", (600, 400), (128, 128, 128)).save(buf, format="PNG")
    assert chart_axis.label_rows(buf.getvalue()) is None
    assert chart_axis.calibrate(buf.getvalue(), [100.0, 90.0]) is None


def test_the_live_quote_chip_is_located_but_kept_out_of_the_ladder():
    png, truth = chart_png()          # chip at 4395.2, between two labels
    rows = chart_axis.label_rows(png)
    tag = chart_axis.price_tag_row(png)
    assert tag == pytest.approx(846, abs=3)
    assert all(abs(r - tag) > 10 for r in rows), "the chip was counted as a label"


# ── The plan is only drawn on a scale the chart itself agrees with ───────────

def _plan(entry: float, current: float) -> dict:
    return {
        "side": "buy", "proposed_entry": entry, "sl": entry - 20,
        "tp1": entry + 20, "tp2": entry + 40, "current_price": current,
    }


def test_a_plan_is_drawn_when_the_scale_matches_the_quote_chip():
    from plugins.AiMarketAnalyst.backend.services.chart_annotate import annotate

    png, truth = chart_png()                 # chip at 4395.2
    findings = {"axis_labels": sorted(truth, reverse=True)}
    assert annotate(png, findings, plan=_plan(4420.0, 4395.2)) is not None


def test_a_scale_the_quote_chip_contradicts_draws_no_plan():
    """The reported bug's worst form: lines drawn confidently in the wrong place."""
    from plugins.AiMarketAnalyst.backend.services.chart_annotate import annotate

    png, truth = chart_png()
    # Only the model's estimate is available, and it is 7% low — so the live
    # price would be drawn nowhere near the chip the chart prints it at.
    findings = {
        "axis": {
            "top_price": 4460.0, "top_y_pct": 19.5,
            "bottom_price": 4390.0, "bottom_y_pct": 77.5,
        },
    }
    assert annotate(png, findings, plan=_plan(4420.0, 4395.2)) is None
