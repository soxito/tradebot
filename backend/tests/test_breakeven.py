"""Break-even protection for single-target orders (trading room, scalp, SMC).

The Telegram sniper keys break-even off its TP3 milestone. Room orders carry
one take-profit, so the milestone is progress toward that target instead — but
the guarantee is identical: once a trade has proved itself it cannot turn into
a loss.
"""
from __future__ import annotations

import pytest

from app.trading.breakeven import DEFAULT_TRIGGER_FRACTION, breakeven_stop

ENTRY = 4300.0
TP_LONG = 4330.0       # 30 points of room
TP_SHORT = 4270.0
SL_LONG = 4290.0
SL_SHORT = 4310.0


def long_at(price, sl=SL_LONG):
    return breakeven_stop(side="buy", entry=ENTRY, current_price=price,
                          take_profit=TP_LONG, current_sl=sl)


def short_at(price, sl=SL_SHORT):
    return breakeven_stop(side="sell", entry=ENTRY, current_price=price,
                          take_profit=TP_SHORT, current_sl=sl)


# ── when it arms ────────────────────────────────────────────────────────────

def test_stop_is_left_alone_early_in_the_trade():
    assert long_at(4305.0) is None          # 17 % of the way
    assert long_at(4315.0) is None          # 50 %


def test_stop_moves_to_break_even_once_the_trade_is_working():
    trigger = ENTRY + (TP_LONG - ENTRY) * DEFAULT_TRIGGER_FRACTION
    assert long_at(trigger) == ENTRY
    assert long_at(4328.0) == ENTRY


def test_shorts_arm_on_the_same_progress_rule():
    assert short_at(4295.0) is None
    trigger = ENTRY - (ENTRY - TP_SHORT) * DEFAULT_TRIGGER_FRACTION
    assert short_at(trigger) == ENTRY


def test_a_losing_trade_is_never_moved():
    assert long_at(4292.0) is None
    assert short_at(4308.0) is None


# ── it never makes things worse ─────────────────────────────────────────────

def test_a_stop_already_past_break_even_is_not_pulled_back():
    """Moving it to entry would widen the risk on a protected position."""
    assert long_at(4328.0, sl=4310.0) is None
    assert short_at(4275.0, sl=4290.0) is None


def test_a_stop_exactly_at_break_even_is_left_alone():
    assert long_at(4328.0, sl=ENTRY) is None
    assert short_at(4275.0, sl=ENTRY) is None


def test_it_arms_even_when_no_stop_was_set():
    assert long_at(4328.0, sl=None) == ENTRY


# ── bad inputs ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kwargs", [
    dict(side="buy", entry=0, current_price=4328.0, take_profit=TP_LONG, current_sl=None),
    dict(side="buy", entry=ENTRY, current_price=0, take_profit=TP_LONG, current_sl=None),
    dict(side="buy", entry=ENTRY, current_price=4328.0, take_profit=None, current_sl=None),
])
def test_missing_inputs_do_nothing(kwargs):
    assert breakeven_stop(**kwargs) is None


def test_a_target_on_the_wrong_side_of_entry_does_nothing():
    """A mis-parsed order must not produce a nonsensical stop."""
    assert breakeven_stop(side="buy", entry=ENTRY, current_price=4328.0,
                          take_profit=4200.0, current_sl=None) is None


@pytest.mark.parametrize("side", ["buy", "long", "BUY", "Long"])
def test_long_aliases_are_understood(side):
    assert breakeven_stop(side=side, entry=ENTRY, current_price=4328.0,
                          take_profit=TP_LONG, current_sl=None) == ENTRY
