"""Trailing + break-even lock in a profit and never widen the risk again.

Break-even alone stops at entry; a trade that then runs another $20 gives all of
it back on a reversal because the stop never followed. The trail is the second
half — once a trade is clearly in profit its stop follows price up, banking a
rising fraction of the gain. The two run as one ``protective_stop`` call, which
must always pick the more protective level and never move a stop backward.
"""
from __future__ import annotations

import pytest

from app.trading.trailing import (
    DEFAULT_ARM_FRACTION,
    protective_stop,
    trailing_stop,
)

ENTRY = 4300.0
TP_LONG = 4340.0        # 40 points of room
TP_SHORT = 4260.0


def _long_trail(price, sl):
    return trailing_stop(side="buy", entry=ENTRY, current_price=price,
                         take_profit=TP_LONG, current_sl=sl)


def _short_trail(price, sl):
    return trailing_stop(side="sell", entry=ENTRY, current_price=price,
                         take_profit=TP_SHORT, current_sl=sl)


# ── when the trail arms ──────────────────────────────────────────────────────

def test_the_trail_stays_out_until_the_trade_has_proved_itself():
    # Armed at 50% of the way to target; 40% is still too early.
    assert _long_trail(ENTRY + 40 * 0.4, sl=ENTRY) is None


def test_the_trail_locks_half_the_gain_once_armed():
    # 75% of the way: price 4330, excursion 30, lock half → 4315.
    assert _long_trail(4330.0, sl=ENTRY) == pytest.approx(4315.0)


def test_the_trail_runs_the_other_way_for_a_short():
    assert _short_trail(4270.0, sl=ENTRY) == pytest.approx(4285.0)


# ── it never gives ground back ───────────────────────────────────────────────

def test_the_stop_is_never_moved_backward():
    """A dip in price after the trail set must not re-widen the stop."""
    high = _long_trail(4332.0, sl=ENTRY)          # locks 4316
    assert high == pytest.approx(4316.0)
    # Price pulls back to 4326; the trail would compute 4313 — lower — so holds.
    assert _long_trail(4326.0, sl=high) is None


def test_the_trailed_stop_never_sits_past_the_current_price():
    """A stop beyond price would close the trade instantly — never returned."""
    # lock_fraction 0.5 keeps the stop half-way back, so this is structural, but
    # assert it directly at a near-target price.
    stop = _long_trail(4339.0, sl=ENTRY)
    assert stop is None or stop < 4339.0


# ── break-even and trail together ────────────────────────────────────────────

def _protect(price, sl):
    return protective_stop(side="buy", entry=ENTRY, current_price=price,
                           take_profit=TP_LONG, current_sl=sl)


def test_early_profit_gets_break_even_before_the_trail_arms():
    # 66% (break-even trigger) is below the 50%... actually break-even arms at
    # 0.66 and trail at 0.50, so at 0.55 only the trail-arm threshold is passed
    # but its lock (half of 22 = 11 → 4311) already beats entry.
    price = ENTRY + 40 * 0.55
    assert _protect(price, sl=ENTRY - 10) == pytest.approx(4311.0)


def test_the_more_protective_of_the_two_always_wins():
    # Deep in profit: break-even returns entry (4300), trail returns 4315.
    # protective_stop must pick the trail — the higher stop on a long.
    assert _protect(4330.0, sl=ENTRY - 10) == pytest.approx(4315.0)


def test_nothing_moves_once_neither_can_improve_the_stop():
    # Stop already at 4316; price 4328 → trail 4314 (worse), break-even 4300.
    assert _protect(4328.0, sl=4316.0) is None


def test_arm_fraction_is_the_documented_default():
    assert DEFAULT_ARM_FRACTION == 0.5
