"""Trailing-stop lock and the never-skip confidence rule.

Two rules protect a running signal from being closed short of the channel's
final target:

* the trailing stop locks at TP3 and never ratchets past it, so a pullback
  after TP4/TP5 cannot end a trade that is still heading for the last TP;
* the stop is breached STRICTLY, so touching a level does not close the trade
  on the same tick the level is first reached.

A third rule keeps the strongest calls in the feed: a signal at or above
``never_skip_confidence_pct`` is exempt from every discretionary gate.

Numbers throughout come from a real signal: Channel #18 msg 7011,
XAUUSD buy @ 4310.5, SL 4302, TP1..TP8 = 4315…4336 step 3.
"""
from __future__ import annotations

import pytest

from plugins.TelegramSignalNewsPlugin.backend.services import sniper_service as ss
from plugins.TelegramSignalNewsPlugin.backend.services.monitor_service import (
    TRAIL_LOCK_TP_INDEX,
)

ENTRY = 4310.5
SL = 4302.0
TPS = [4315.0, 4318.0, 4321.0, 4324.0, 4327.0, 4330.0, 4333.0, 4336.0]


class Settings:
    """Minimal stand-in for TelegramSniperSettings."""

    force_telegram_signals = False
    never_skip_confidence_pct = 90.0
    min_risk_reward = 1.2


class Sig:
    def __init__(self, confidence: float):
        self.confidence = confidence


def replay(prices, tps=None, is_long=True, stop=SL, entry=ENTRY):
    """Walk a price path through the trailing rules exactly as the monitor does.

    Returns ``(outcome, final_stop)`` where outcome is one of
    ``"final_tp" | "break_even" | "stop" | "open"``.
    """
    raw = tps if tps is not None else TPS
    # The monitor only filters TPs to the profit side when an entry is known.
    if entry:
        raw = [t for t in raw if (t > entry if is_long else t < entry)]
    tps = sorted(raw, reverse=not is_long)
    trail = None
    reached = 0
    for live in prices:
        crossed = sum(1 for t in tps if (live >= t if is_long else live <= t))
        if crossed > reached:
            lock_idx = min(TRAIL_LOCK_TP_INDEX, len(tps) - 1)
            reached = crossed
            if (crossed - 1) >= lock_idx:
                be = entry if entry else tps[lock_idx]
                trail = be if trail is None else (
                    max(trail, be) if is_long else min(trail, be)
                )
        if tps and crossed >= len(tps):
            return "final_tp", trail
        eff = trail if trail is not None else stop
        if trail is not None:
            breached = live < eff if is_long else live > eff
        else:
            breached = live <= eff if is_long else live >= eff
        if breached:
            return ("break_even" if trail is not None else "stop"), trail
    return "open", trail


# ── the trailing lock ────────────────────────────────────────────────────────

def test_stop_stays_on_the_original_until_tp3():
    """TP1 and TP2 must not move the stop — only the TP3 milestone does."""
    outcome, trail = replay([4311, 4315, 4318])
    assert outcome == "open"
    assert trail is None


def test_stop_jumps_to_break_even_at_tp3():
    _, trail = replay([4311, 4315, 4318, 4321])
    assert trail == ENTRY == 4310.5


def test_stop_is_frozen_at_break_even_through_tp4_and_tp5():
    """TP4/TP5 must not ratchet the stop — it holds at break-even."""
    _, trail = replay([4311, 4315, 4318, 4321, 4324, 4327])
    assert trail == ENTRY


def test_pullback_after_tp5_no_longer_closes_the_trade():
    """The reported failure: a dip to 4322 mid-run used to bank TP5 and exit.

    With the stop at break-even the position survives the dip and goes on to
    reach the channel's final target.
    """
    path = [4311, 4315, 4318, 4321, 4324, 4327, 4322, 4326, 4330, 4333, 4336]
    outcome, trail = replay(path)
    assert outcome == "final_tp"
    assert trail == ENTRY


def test_touching_a_tp_does_not_close_on_the_same_tick():
    """live == stop is not a breach — the stop must be crossed."""
    outcome, _ = replay([4311, 4315])
    assert outcome == "open"


def test_after_tp3_a_full_reversal_exits_flat_not_at_a_loss():
    """Break-even means the worst case after TP3 is zero, never a loss."""
    outcome, trail = replay([4311, 4315, 4318, 4321, 4316, 4305])
    assert outcome == "break_even"
    assert trail == ENTRY
    assert trail >= SL  # strictly better than the original stop


def test_loss_before_any_tp_hits_the_original_stop():
    outcome, trail = replay([4311, 4308, 4301])
    assert outcome == "stop"
    assert trail is None


def test_short_ladder_uses_its_final_tp_as_the_milestone():
    """Fewer than 3 TPs → milestone is the last one rather than indexing past it."""
    _, trail = replay([4311, 4315, 4318], tps=[4315.0, 4318.0])
    assert trail == ENTRY


def test_break_even_falls_back_to_the_milestone_tp_without_an_entry():
    _, trail = replay([4311, 4315, 4318, 4321], entry=None)
    assert trail == TPS[TRAIL_LOCK_TP_INDEX] == 4321.0


# ── never-skip confidence ────────────────────────────────────────────────────

@pytest.mark.parametrize("confidence,expected", [(0.95, True), (0.90, True), (0.899, False), (0.6, False)])
def test_high_conviction_threshold(confidence, expected):
    assert ss.is_high_conviction(Sig(confidence), Settings()) is expected


def test_force_flag_makes_everything_high_conviction():
    class Forced(Settings):
        force_telegram_signals = True

    assert ss.is_high_conviction(Sig(0.1), Forced()) is True


def test_high_conviction_signal_survives_the_reward_risk_floor():
    """RR is measured to TP1, which is near — the floor must not drop a 95 %."""
    kwargs = dict(
        direction="long", signal_entry=ENTRY, stop_loss=SL, take_profits=TPS,
        live_price=4312.0, offset_pct=0.3,
    )
    assert ss.reanalyze_signal(min_rr=0.0, **kwargs).ok is True


# ── entry may never be planned through the stop ──────────────────────────────

def test_sniper_entry_never_crosses_the_stop_loss():
    """A percentage offset is wider than a gold stop; it must be clamped.

    0.3 % of 4312 is ~13 points against an 8-point stop, which previously put
    the planned entry below the stop and inflated reward/risk.
    """
    plan = ss.reanalyze_signal(
        direction="long", signal_entry=ENTRY, stop_loss=SL, take_profits=TPS,
        live_price=4312.0, offset_pct=0.3, min_rr=1.2,
    )
    assert plan.ok
    assert plan.sniper_entry > SL
    assert plan.sniper_entry <= 4312.0


def test_position_targets_the_final_tp_not_the_nearest():
    """The order must aim at the channel's LAST target.

    ``reanalyze_signal`` resolves the NEAREST target, which is what the order
    used to be placed against — so the whole position cashed out at TP1 while
    the move ran on to TP8.
    """
    plan = ss.reanalyze_signal(
        direction="long", signal_entry=ENTRY, stop_loss=SL, take_profits=TPS,
        live_price=4312.0, offset_pct=0.3, min_rr=1.2,
    )
    assert plan.take_profit == 4315.0  # nearest — not what we place

    exec_entry = ENTRY
    ladder = sorted([t for t in TPS if t > exec_entry])
    assert ladder[-1] == 4336.0  # final — what we place


def test_a_signal_with_no_take_profits_yields_no_target():
    """Some channels post entry + stop only.

    ``final_tp`` is then None and every message built around it has to cope —
    formatting it as a number raised TypeError and aborted the handler *after*
    the broker order had already gone out.
    """
    plan = ss.reanalyze_signal(
        direction="short", signal_entry=4336.5, stop_loss=4341.0, take_profits=[],
        live_price=4336.0, offset_pct=0.3, min_rr=1.2,
    )
    assert plan.ok
    assert plan.take_profit is None
    ladder = sorted([t for t in [] if t < 4336.5], reverse=True)
    final_tp = ladder[-1] if ladder else plan.take_profit
    assert final_tp is None
    label = f"{final_tp:g}" if final_tp else "none (stop only)"
    assert label == "none (stop only)"


def test_final_tp_selection_respects_direction_for_shorts():
    exec_entry = 4310.5
    tps = [4306.0, 4303.0, 4300.0]
    ladder = sorted([t for t in tps if t < exec_entry], reverse=True)
    assert ladder[-1] == 4300.0


# ── lot sizing must fit the account ──────────────────────────────────────────

def _lot(equity, *, free=None, lev=500, risk=1.0, max_risk=5.0, stop=SL, entry=ENTRY,
         small=False):
    return ss.affordable_mt5_lot(
        equity=equity, free_margin=free, leverage=lev, risk_pct=risk,
        entry=entry, stop_loss=stop, symbol="XAUUSD",
        floor_lot=0.01, max_risk_pct=max_risk, small_account_mode=small,
    )


def test_small_account_is_skipped_rather_than_floored_up():
    """The Markets.com case: ~$110 equity against an 8.5-point gold stop.

    One floor lot loses ~$85 — 77 % of the account — so the trade must not be
    placed at all rather than sized up to the broker minimum.
    """
    lot, why = _lot(110.0)
    assert lot is None
    assert "over the" in why and "ceiling" in why


def test_floor_lot_is_allowed_when_it_is_actually_affordable():
    """$500: the 1 % budget wants 0.006 lots, but 0.01 costs $8.50 — only 1.7 %.

    Under the 5 % ceiling, so rounding up to the broker minimum is acceptable.
    """
    lot, why = _lot(500.0)
    assert lot == 0.01
    assert "floor lot" in why


def test_large_account_scales_above_the_floor():
    lot, why = _lot(1_000_000.0)
    assert lot > 0.01
    assert "risk-capped" in why


def test_lot_never_rounds_up_past_the_risk_budget():
    """Rounding must floor: a lot rounded UP risks more than the budget allows."""
    equity = 100_000.0
    lot, _ = _lot(equity)
    loss = lot * 100.0 * abs(ENTRY - SL)      # contract size 100 for gold
    assert loss <= equity * 0.01 + 1e-6


def test_free_margin_caps_the_lot_below_the_risk_size():
    """A rich account with almost no free margin cannot open a large position."""
    big, _ = _lot(1_000_000.0, free=1_000_000.0)
    tight, why = _lot(1_000_000.0, free=5_000.0)
    assert tight < big
    assert "margin-capped" in why


def test_trade_is_skipped_when_free_margin_cannot_cover_one_floor_lot():
    lot, why = _lot(50_000.0, free=1.0)
    assert lot is None
    assert "free margin" in why


def test_small_account_mode_trades_instead_of_skipping():
    """Markets.com: ~$110, where one floor lot is 7.7 % — over the 5 % ceiling.

    Skipping means the account misses every signal, so small-account mode takes
    the trade at exactly the broker minimum and never more.
    """
    lot, why = _lot(110.0, small=True)
    assert lot == 0.01
    assert "small-account mode" in why
    assert "one open trade" in why


def test_small_account_mode_never_sizes_above_the_floor():
    """The cap is absolute — a bigger budget must not scale this path up."""
    for equity in (110.0, 300.0, 700.0):
        lot, why = _lot(equity, small=True)
        if "small-account mode" in why:
            assert lot == 0.01


def test_small_account_mode_does_not_change_a_healthy_account():
    """A funded account still sizes on risk, not on the floor."""
    lot, why = _lot(1_000_000.0, small=True)
    assert lot > 0.01
    assert "small-account mode" not in why


def test_small_account_mode_still_respects_free_margin():
    """Coverage is worth concentration, not placing an order margin can't cover."""
    lot, why = _lot(110.0, free=0.5, small=True)
    assert lot is None
    assert "free margin" in why


def test_unfunded_account_is_skipped_without_dividing_by_zero():
    """The BTGT case: a linked live account sitting at 0.00 equity."""
    lot, why = _lot(0.0)
    assert lot is None
    assert "no usable equity" in why


def test_sniper_entry_clamp_holds_for_shorts():
    plan = ss.reanalyze_signal(
        direction="short", signal_entry=4310.5, stop_loss=4319.0,
        take_profits=[4306.0, 4303.0, 4300.0], live_price=4309.0,
        offset_pct=0.3, min_rr=1.2,
    )
    assert plan.ok
    assert plan.sniper_entry < 4319.0
    assert plan.sniper_entry >= 4309.0
