"""The market-structure story — beats built from evidence, silence otherwise."""
from __future__ import annotations

from plugins.MT5TradingPlugin.backend.services import smc_narrative
from plugins.MT5TradingPlugin.backend.services.smc_narrative import build_narrative
from plugins.MT5TradingPlugin.backend.services.smc_strategy import Candle


def _c(i, o, h, l, c, v=100.0):
    return Candle(time=1_700_000_000 + i * 3600, open=o, high=h, low=l, close=c, volume=v)


def _bullish_run():
    """A V: sell-off, base, then a structured rally that breaks swings."""
    rows = []
    price = 120.0
    # Initial bearish leg.
    for i in range(20):
        rows.append(_c(i, price, price + 2, price - 4, price - 3))
        price -= 3
    # Base / accumulation.
    for i in range(20, 32):
        rows.append(_c(i, price + 0.5, price + 1.5, price - 1.5, price - 0.5))
    # Bullish expansion with higher highs.
    for i in range(32, 60):
        rows.append(_c(i, price, price + 4, price - 1, price + 3))
        price += 3
    return rows


def _analysis(rows, bias="bullish"):
    return {
        "bias": bias,
        "momentum": "expanding",
        "last_price": rows[-1].close,
        "range": {"low": min(r.low for r in rows), "high": max(r.high for r in rows)},
        "structure_events": [
            {"index": 30, "time": rows[30].time, "type": "CHoCH", "direction": "bullish",
             "level": 100.0, "protected_low": 95.0},
            {"index": 40, "time": rows[40].time, "type": "BOS", "direction": "bullish",
             "level": 110.0},
        ],
        "zones": [
            {"kind": "bullish_fvg", "top": 105.0, "bottom": 100.0, "index": 41,
             "time": rows[41].time},
        ],
        "liquidity": {"buyside": [200.0, 220.0], "sellside": [80.0]},
        "false_breakout": {"swept_lows": [96.0], "swept_highs": [],
                           "is_sweep_bar": False, "sweep_direction": "down",
                           "rejection_wick": 0.6, "volume_on_sweep": 1.2,
                           "false_break_score": 60.0},
    }


def test_bullish_story_tells_the_full_flow():
    rows = _bullish_run()
    story = build_narrative(rows, _analysis(rows))
    titles = [s["title"] for s in story["steps"]]

    assert any("Initial Bearish" in t for t in titles)
    assert any("Liquidity Sweep" in t for t in titles)
    assert any("Accumulation" in t for t in titles)
    assert any("CHoCH" in t for t in titles)
    assert any("BOS" in t for t in titles)
    assert any("FVG" in t for t in titles)
    assert any("Liquidity Target" in t for t in titles)
    assert any("Premium" in t or "Discount" in t for t in titles)
    assert titles[-1] == "Market Structure Summary"

    # Steps are numbered and ordered.
    assert [s["step"] for s in story["steps"]] == list(range(1, len(story["steps"]) + 1))
    # The flow line joins the beats.
    assert "→" in story["flow"]


def test_every_step_answers_why():
    """Each beat carries a reason — the story explains, not just describes."""
    rows = _bullish_run()
    story = build_narrative(rows, _analysis(rows))
    for s in story["steps"]:
        assert s["detail"].strip()
        assert s["reason"].strip()


def _bearish_analysis(rows):
    a = _analysis(rows, bias="bearish")
    a["structure_events"] = [
        {"index": 30, "time": rows[30].time, "type": "CHoCH", "direction": "bearish",
         "level": 100.0, "protected_high": 130.0},
        {"index": 40, "time": rows[40].time, "type": "BOS", "direction": "bearish",
         "level": 90.0},
    ]
    a["zones"] = [
        {"kind": "bearish_fvg", "top": 95.0, "bottom": 90.0, "index": 41,
         "time": rows[41].time},
    ]
    a["false_breakout"]["swept_lows"] = []
    a["false_breakout"]["swept_highs"] = [135.0]
    return a


def test_bearish_story_mirrors_direction():
    rows = _bullish_run()
    story = build_narrative(rows, _bearish_analysis(rows))
    titles = [s["title"] for s in story["steps"]]
    assert any("Initial Bullish" in t for t in titles)


def test_short_series_is_silence():
    rows = _bullish_run()
    story = build_narrative(rows[:20], _analysis(rows))
    assert story == {"steps": [], "flow": "", "summary": ""}


def test_evidence_lines_are_short_and_last_beats():
    rows = _bullish_run()
    story = build_narrative(rows, _analysis(rows))
    lines = smc_narrative.evidence_lines(story, limit=3)
    assert 1 <= len(lines) <= 3
    assert all(len(line) < 200 for line in lines)
