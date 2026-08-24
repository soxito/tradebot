"""The screenshot path must never invent a chart or a price.

Two properties matter here and are easy to regress:

* the overlay is drawn on the *user's* pixels, so the image comes back the same
  size it went in — anything else means it was regenerated;
* a model that reports no levels produces no overlay, rather than an empty
  annotated image implying an analysis that never happened.
"""

from __future__ import annotations

import io

import pytest

from plugins.AiMarketAnalyst.backend.services import chart_annotate
from plugins.AiMarketAnalyst.backend.services.ai_router import (
    is_reasoning_model,
    resolve_model_for_task,
)
from plugins.AiMarketAnalyst.backend.services.vision import _split_findings


def _chart(width: int = 640, height: int = 400) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, format="PNG")
    return buf.getvalue()


# ── Annotation ───────────────────────────────────────────────────────────────

def test_overlay_keeps_the_original_dimensions():
    """Drawn on, not regenerated — a resized image means FLUX-style invention."""
    from PIL import Image

    src = _chart(823, 517)
    out = chart_annotate.annotate(
        src, {"levels": [{"label": "support", "y_pct": 60, "kind": "support"}]}
    )
    assert out is not None
    assert Image.open(io.BytesIO(out)).size == (823, 517)


def test_no_findings_produces_no_image():
    assert chart_annotate.annotate(_chart(), {}) is None
    assert chart_annotate.annotate(_chart(), {"levels": [], "regions": []}) is None


@pytest.mark.parametrize("bad", ["abc", None, 140, -5, ""])
def test_unusable_positions_are_skipped_not_guessed(bad):
    """A level the model could not place must be dropped, never defaulted to 0."""
    assert chart_annotate.annotate(_chart(), {"levels": [{"label": "x", "y_pct": bad}]}) is None


def test_a_level_without_a_price_still_draws():
    """price=None is the honest answer for an unreadable axis; it must not crash."""
    out = chart_annotate.annotate(
        _chart(), {"levels": [{"label": "range high", "price": None, "y_pct": 25}]}
    )
    assert out is not None


# ── Placing a real price on someone else's screenshot ────────────────────────

def test_the_axis_labels_calibrate_the_price_scale():
    """Two axis readings fix the scale, so any price lands where it belongs."""
    to_y = chart_annotate.price_to_y(
        {"axis": {"top_price": 4700, "top_y_pct": 10,
                  "bottom_price": 3500, "bottom_y_pct": 90}}, 1000)
    assert to_y(4700) == 100
    assert to_y(3500) == 900
    assert to_y(4100) == pytest.approx(500, abs=2)   # midpoint


def test_priced_levels_calibrate_when_the_axis_was_not_read():
    to_y = chart_annotate.price_to_y(
        {"levels": [{"price": "4400", "y_pct": 20}, {"price": "4000", "y_pct": 60}]}, 1000)
    assert to_y(4200) == pytest.approx(400, abs=2)


@pytest.mark.parametrize("findings", [
    {},
    {"levels": [{"price": "100", "y_pct": 10}]},                       # one point
    {"levels": [{"price": "100", "y_pct": 10}, {"price": "100", "y_pct": 60}]},  # same price
    {"levels": [{"price": None, "y_pct": 10}, {"price": None, "y_pct": 60}]},    # no prices
])
def test_an_unreadable_scale_refuses_rather_than_guesses(findings):
    """Guessing the scale would put a stop at the wrong height on a real chart."""
    assert chart_annotate.price_to_y(findings, 1000) is None


def test_a_price_off_the_visible_chart_is_not_drawn():
    to_y = chart_annotate.price_to_y(
        {"axis": {"top_price": 4700, "top_y_pct": 10,
                  "bottom_price": 3500, "bottom_y_pct": 90}}, 1000)
    assert to_y(99999) is None


def test_the_plan_is_drawn_when_the_axis_is_known():
    out = chart_annotate.annotate(
        _chart(), {"axis": {"top_price": 4700, "top_y_pct": 10,
                            "bottom_price": 3500, "bottom_y_pct": 90}},
        plan={"side": "long", "proposed_entry": 4321.5, "sl": 4273.1, "tp1": 4392.3},
    )
    assert out and out[:8] == b"\x89PNG\r\n\x1a\n"


def test_no_plan_lines_without_a_calibrated_axis():
    """Better to send the read alone than to place levels by guesswork."""
    assert chart_annotate.annotate(
        _chart(), {}, plan={"side": "long", "proposed_entry": 4321.5, "sl": 4273.1},
    ) is None


def test_malformed_entries_do_not_break_the_overlay():
    out = chart_annotate.annotate(
        _chart(),
        {"levels": ["not-a-dict", {"label": "ok", "y_pct": 50}], "regions": ["nope"]},
    )
    assert out is not None


# ── Findings parsing ─────────────────────────────────────────────────────────

def test_findings_block_is_split_off_the_prose():
    prose, findings = _split_findings(
        'Resistance is at the highs.\n```json\n{"bias": "bearish", "levels": []}\n```'
    )
    assert prose == "Resistance is at the highs."
    assert findings["bias"] == "bearish"


def test_answer_without_a_block_is_returned_whole():
    prose, findings = _split_findings("This is a photo of a cat, not a chart.")
    assert prose == "This is a photo of a cat, not a chart."
    assert findings == {}


def test_unparseable_block_does_not_lose_the_answer():
    """A truncated JSON tail must not swallow the prose the user needs to read."""
    prose, findings = _split_findings("Real answer.\n```json\n{oops not json\n```")
    assert "Real answer." in prose
    assert findings == {}


# ── Task routing ─────────────────────────────────────────────────────────────

def test_vision_task_resolves_to_vision_capable_models_only():
    """Routing a screenshot to a text-only model returns nothing usable."""
    text_only = {"z-ai/glm-5.2", "nvidia/nemotron-3.5-lightning-30b-a3b"}
    assert set(resolve_model_for_task("vision_analysis")).isdisjoint(text_only)


def test_each_task_has_a_chain_and_unknown_tasks_fall_through():
    for task in ("vision_analysis", "fast_agentic", "deep_reasoning"):
        assert resolve_model_for_task(task), f"{task} has no models"
    assert resolve_model_for_task("not-a-task") == []


def test_reasoning_models_are_flagged_wherever_they_appear_in_a_chain():
    """They emit reasoning before content, so callers must raise the budget.

    This deliberately does not require the *first* model of each chain to be a
    reasoning model. The vision chain leads with a dedicated vision model
    precisely because the reasoning ones could not serve it: Inkling spent its
    whole budget on reasoning_content and returned empty content, and Muse
    Glimmer took 138s against a 120s deadline.
    """
    from plugins.AiMarketAnalyst.backend.services.ai_router import _REASONING_MODELS

    for task in ("vision_analysis", "fast_agentic", "deep_reasoning"):
        for model in resolve_model_for_task(task):
            if model in _REASONING_MODELS:
                assert is_reasoning_model(model), f"{model} must get the bigger budget"
    assert not is_reasoning_model("gpt-4o")


def test_vision_chain_leads_with_a_model_that_answers_in_time():
    """The failure this covers: every vision read timing out or coming back empty.

    Both leaders were measured serving a real phone screenshot with the full
    findings prompt (17.4s and 63.1s); the slow reasoning model is allowed in
    the chain but never first.
    """
    chain = resolve_model_for_task("vision_analysis")
    assert chain[0] == "meta/llama-3.2-11b-vision-instruct"
    assert "thinkingmachines/inkling" not in chain, (
        "Inkling returns empty content for this prompt — it must not be routed here"
    )
