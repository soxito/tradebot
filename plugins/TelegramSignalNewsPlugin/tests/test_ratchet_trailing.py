"""Unit tests for the TP3-activated ratcheting trailing stop.

The trail must guarantee two things once the milestone (TP3) prints:

1. The position can never lose again — the stop is floored at break-even.
2. Profit keeps being protected as price runs — the stop ratchets forward
   with the peak, never loosening, until the channel's final target.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import os  # noqa: E402

os.environ.setdefault("KRONOS_WARMUP", "0")

from plugins.TelegramSignalNewsPlugin.backend.services.monitor_service import (  # noqa: E402
    _ratchet_trailing_stop,
)

PCT = 0.015  # 1.5 % default tp_trail_pct


def _step(**kw):
    kw.setdefault("trail_pct", PCT)
    return _ratchet_trailing_stop(**kw)


# ── LONG ─────────────────────────────────────────────────────────────────────

def test_long_trail_at_tp3_is_never_below_break_even():
    # Entry 100, price just printed TP3 at 110. Even a deep 1.5% trail from a
    # low peak cannot put the stop under entry — the trade can no longer lose.
    peak, stop = _step(is_long=True, live=100.5, peak=None, entry_price=100.0,
                       milestone_tp=110.0, current_trail=None)
    assert peak == 100.5
    assert stop >= 100.0


def test_long_trail_ratchets_up_with_the_peak_and_never_loosens():
    peak, stop = _step(is_long=True, live=112.0, peak=None, entry_price=100.0,
                       milestone_tp=110.0, current_trail=None)
    assert stop == peak * (1 - PCT)

    # Pullback: live drops to 108 but the stop must NOT follow it down.
    peak2, stop2 = _step(is_long=True, live=108.0, peak=peak, entry_price=100.0,
                         milestone_tp=110.0, current_trail=stop)
    assert peak2 == peak            # peak only tracks new highs
    assert stop2 == stop            # stop frozen at its tightest


def test_long_new_high_raises_the_stop():
    peak, stop = _step(is_long=True, live=115.0, peak=112.0, entry_price=100.0,
                       milestone_tp=110.0, current_trail=110.32)
    assert peak == 115.0
    assert stop == 115.0 * (1 - PCT)


def test_long_without_entry_falls_back_to_milestone_tp():
    # No entry parsed → break-even floor is the milestone TP itself.
    _, stop = _step(is_long=True, live=111.0, peak=None, entry_price=None,
                    milestone_tp=110.0, current_trail=None)
    assert stop >= 110.0


def test_long_stop_stays_below_live_so_brokers_accept_it():
    # Price collapsed back near the trail in one tick — the candidate stop is
    # clamped just under live instead of sitting above it.
    _, stop = _step(is_long=True, live=101.0, peak=None, entry_price=100.0,
                    milestone_tp=110.0, current_trail=None)
    assert stop < 101.0


# ── SHORT ────────────────────────────────────────────────────────────────────

def test_short_trail_at_tp3_is_never_above_break_even():
    # Short from 100; TP3 printed at 90 while price is at 90.5.
    peak, stop = _step(is_long=False, live=90.5, peak=None, entry_price=100.0,
                       milestone_tp=90.0, current_trail=None)
    assert stop <= 100.0


def test_short_trail_ratchets_down_with_the_peak_and_never_loosens():
    peak, stop = _step(is_long=False, live=89.0, peak=None, entry_price=100.0,
                       milestone_tp=90.0, current_trail=None)
    assert stop == peak * (1 + PCT)

    # Bounce: live rises to 92 but neither peak nor stop may loosen.
    peak2, stop2 = _step(is_long=False, live=92.0, peak=peak, entry_price=100.0,
                         milestone_tp=90.0, current_trail=stop)
    assert peak2 == peak
    assert stop2 == stop


def test_short_new_low_tightens_the_stop():
    peak, stop = _step(is_long=False, live=85.0, peak=88.0, entry_price=100.0,
                       milestone_tp=90.0, current_trail=89.32)
    assert peak == 85.0
    assert stop == 85.0 * (1 + PCT)


def test_short_stop_stays_above_live_so_brokers_accept_it():
    _, stop = _step(is_long=False, live=99.0, peak=None, entry_price=100.0,
                    milestone_tp=90.0, current_trail=None)
    assert stop > 99.0
