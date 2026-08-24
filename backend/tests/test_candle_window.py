"""Closed candles are separated from the one still printing, and measured.

The bar a feed returns last is usually incomplete. Mixing it in with the closed
ones lets an agent call a breakout on a high that has not settled — so the
split is the property worth pinning down, along with the guarantee that a
movement read is always produced rather than an empty answer.
"""

from __future__ import annotations

import pytest

from app.signals.candle_window import (
    movement_summary,
    split_closed,
    timeframe_seconds,
)

_H = 3600
#: A realistic feed epoch. Millisecond stamps are told apart from second ones
#: by magnitude, so a toy value like 1_000_000 would be misread as seconds.
_NOW = 1_700_000_000


def _bars(specs, *, start: int, step: int = _H):
    """[(open, high, low, close, volume)] → ccxt ms rows."""
    return [
        [(start + i * step) * 1000, o, h, l, c, v]
        for i, (o, h, l, c, v) in enumerate(specs)
    ]


def _flat(n: int, *, start: int, step: int = _H):
    return _bars([(100, 101, 99, 100, 10)] * n, start=start, step=step)


# ── Splitting ────────────────────────────────────────────────────────────────

def test_the_last_bar_is_held_back_while_its_period_is_still_running():
    now = _NOW
    # Final bar opened 10 minutes ago on an hourly chart — not closed yet.
    rows = _flat(5, start=now - 4 * _H - 600)
    closed, forming = split_closed(rows, "1h", now=now)
    assert len(closed) == 4
    assert forming is not None and forming is rows[-1]


def test_a_bar_whose_period_has_elapsed_counts_as_closed():
    now = _NOW
    rows = _flat(5, start=now - 5 * _H)
    closed, forming = split_closed(rows, "1h", now=now)
    assert len(closed) == 5
    assert forming is None, "a finished bar must not be withheld as forming"


def test_a_stale_feed_has_no_forming_candle():
    """A closed market is a real state, not a missing one."""
    closed, forming = split_closed(_flat(30, start=_NOW - 400 * _H), "1h",
                                   now=_NOW)
    assert len(closed) == 30 and forming is None


def test_timeframe_length_drives_the_split():
    now = _NOW
    rows = _flat(3, start=now - 2 * _H - 60)
    # The final bar is 60s old: closed on a 1m chart, still open on a 4h one.
    assert split_closed(rows, "1m", now=now)[1] is None
    assert split_closed(rows, "4h", now=now)[1] is not None


def test_second_and_millisecond_stamps_are_both_understood():
    now = _NOW
    ms = _flat(3, start=now - 3 * _H)
    secs = [[r[0] // 1000, *r[1:]] for r in ms]
    assert len(split_closed(ms, "1h", now=now)[0]) == 3
    assert len(split_closed(secs, "1h", now=now)[0]) == 3


def test_empty_input_is_not_a_crash():
    assert split_closed([], "1h") == ([], None)


@pytest.mark.parametrize(("tf", "secs"), [
    ("1m", 60), ("15m", 900), ("1h", 3600), ("4h", 14400), ("1d", 86400),
    ("1w", 604800), ("nonsense", 3600),
])
def test_timeframe_seconds(tf, secs):
    assert timeframe_seconds(tf) == secs


# ── Measuring the move ───────────────────────────────────────────────────────

def test_a_read_is_always_produced_even_on_a_thin_window():
    """A shallow window must be reported as shallow, never as no signal."""
    out = movement_summary(_flat(4, start=0))
    assert out["candles"] == 4
    assert out["enough_history"] is False
    assert "note" in out


def test_twenty_eight_candles_clears_the_depth_floor():
    assert movement_summary(_flat(28, start=0))["enough_history"] is True
    assert movement_summary(_flat(27, start=0))["enough_history"] is False


def test_no_candles_reports_that_plainly():
    out = movement_summary([])
    assert out["candles"] == 0 and out["enough_history"] is False


def test_the_window_extremes_and_net_move_are_measured_across_every_candle():
    rows = _bars([(100, 110, 90, 105, 5), (105, 120, 100, 118, 5),
                  (118, 125, 80, 95, 5)], start=0)
    out = movement_summary(rows)
    assert out["window_high"] == 125 and out["window_low"] == 80
    assert out["net_change"] == pytest.approx(-5)      # first open 100 → last close 95
    assert out["up_candles"] == 2 and out["down_candles"] == 1


def test_the_run_in_progress_is_counted_from_the_right_edge():
    rows = _bars([(100, 105, 95, 90, 5),    # down
                  (90, 105, 85, 100, 5),    # up
                  (100, 115, 95, 110, 5),   # up
                  (110, 125, 105, 120, 5)], start=0)  # up
    out = movement_summary(rows)
    assert out["streak"] == 3 and out["streak_direction"] == "up"


def test_an_advancing_market_reads_as_higher_highs_and_higher_lows():
    rising = _bars([(100 + i, 105 + i, 95 + i, 104 + i, 5) for i in range(30)], start=0)
    assert movement_summary(rising)["structure"] == "higher highs and higher lows"


def test_a_declining_market_reads_as_lower_highs_and_lower_lows():
    falling = _bars([(200 - i, 205 - i, 195 - i, 196 - i, 5) for i in range(30)], start=0)
    assert movement_summary(falling)["structure"] == "lower highs and lower lows"


# ── The forming candle, weighed against the closed window ────────────────────

def test_the_forming_candle_is_measured_against_the_closed_window():
    closed = _bars([(100, 110, 90, 105, 10)] * 30, start=0)
    forming = [999, 105, 130, 104, 128, 40]     # breaks the window high on 4x volume
    out = movement_summary(closed, forming)
    cur = out["current_vs_window"]
    assert cur["breaks_window_high"] is True
    assert cur["breaks_window_low"] is False
    assert cur["volume_vs_avg"] == pytest.approx(4.0)
    assert cur["direction"] == "up"
    assert "still forming" in cur["note"]


def test_the_forming_candle_is_never_counted_as_a_closed_one():
    closed = _bars([(100, 110, 90, 105, 10)] * 30, start=0)
    out = movement_summary(closed, [999, 105, 500, 104, 480, 40])
    assert out["candles"] == 30
    assert out["window_high"] == 110, "a forming high leaked into the closed window"


def test_no_forming_candle_means_no_current_block():
    assert "current_vs_window" not in movement_summary(_flat(30, start=0))
