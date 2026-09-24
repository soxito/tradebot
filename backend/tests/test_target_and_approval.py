"""Pairs trade only when their backtest earns it; the weekly target sizes up within a cap."""
from __future__ import annotations

from datetime import datetime, timezone

from app.agents import pair_gate, target_pacer


def test_a_pair_is_approved_only_by_a_strong_enough_backtest():
    assert pair_gate.judge({"trades": 40, "win_rate": 0.95, "avg_r": 0.08})[0]
    assert not pair_gate.judge({"trades": 40, "win_rate": 0.88, "avg_r": 0.08})[0]
    assert not pair_gate.judge({"trades": 5, "win_rate": 1.0, "avg_r": 0.2})[0], "too few trades"
    assert not pair_gate.judge({"trades": 40, "win_rate": 0.95, "avg_r": -0.01})[0], "must make money"


def test_pacing_sizes_up_when_behind_but_never_past_the_cap():
    # $3000 left, 0.3R expected for the rest of the week at $6000 a trade = $1800 → 1.67x
    assert target_pacer.multiplier(remaining_usd=3000, expected_r_left=0.3, base_risk_usd=6000, max_mult=2) == 1.667
    assert target_pacer.multiplier(remaining_usd=9000, expected_r_left=0.3, base_risk_usd=6000, max_mult=2) == 2.0
    assert target_pacer.multiplier(remaining_usd=500, expected_r_left=0.3, base_risk_usd=6000, max_mult=2) == 1.0
    assert target_pacer.multiplier(remaining_usd=0, expected_r_left=0.3, base_risk_usd=6000, max_mult=2) == 1.0


def test_the_week_runs_monday_to_friday_utc():
    wed_noon = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
    assert target_pacer.week_start_utc(wed_noon) == datetime(2026, 9, 21, tzinfo=timezone.utc)
    assert target_pacer.week_fraction_left(wed_noon) == 0.5
    assert target_pacer.week_fraction_left(datetime(2026, 9, 26, 10, tzinfo=timezone.utc)) == 0.0


def test_backtest_scores_weeks_and_days_against_the_minimums():
    from app.backtest.service import target_stats

    mon = 1_790_000_000 - (1_790_000_000 - 4 * 86400) % 604800  # a Monday 00:00 UTC
    trades = [{"status": "closed", "exit_time": mon + 3600, "pnl": 2500.0},
              {"status": "closed", "exit_time": mon + 86400 + 3600, "pnl": 900.0},
              {"status": "closed", "exit_time": mon + 7 * 86400 + 3600, "pnl": -200.0}]
    res = {"XAUUSD": {"from": mon, "to": mon + 11 * 86400, "trades": {"oblivion": trades}}}
    t = target_stats(res, weekly_target=3000, daily_target=1000)["oblivion"]
    assert t["weeks"] == 2 and t["weeks_hit"] == 1 and t["losing_weeks"] == 1
    assert t["days_hit"] == 1


def test_the_risk_band_sizes_each_pair_for_its_weekly_minimum():
    band = dict(r_per_week=0.3, week_left=1.0, equity=4_000_000, min_pct=1.0, max_pct=5.0)
    # $15k at 0.3R/week on $4M → 1.25%
    assert target_pacer.band_risk(target_usd=15000, progress_usd=0, **band) == 1.25
    assert target_pacer.band_risk(target_usd=150000, progress_usd=0, **band) == 5.0, "never above the band"
    assert target_pacer.band_risk(target_usd=15000, progress_usd=20000, **band) == 1.0, "met → the band floor"
    assert target_pacer.band_risk(target_usd=15000, progress_usd=0, **{**band, "week_left": 0.5}) == 2.5


def test_news_blocks_before_and_through_the_cool_off():
    from app.signals.news_gate import blocking_event, currencies_for

    events = [(10_000, "USD", "CPI"), (50_000, "JPY", "BoJ")]
    assert set(currencies_for("CADJPY")) == {"CAD", "JPY"} and currencies_for("XAUUSD") == ["USD"]
    assert blocking_event(events, ["USD"], 10_000 - 29 * 60, 30, 120) is not None
    assert blocking_event(events, ["USD"], 10_000 + 119 * 60, 30, 120)[3] == 10_000 + 120 * 60
    assert blocking_event(events, ["USD"], 10_000 + 121 * 60, 30, 120) is None
    assert blocking_event(events, ["CAD"], 10_000, 30, 120) is None, "other currencies are not blocked"


def test_the_trail_starts_at_tp2_and_the_stop_holds_tp1_until_then():
    from app.agents.trade_manager import ladder_step, trail_stop

    plan = {"side": "buy", "stage": 1, "tp1": 100.1, "tp2": 100.3, "trail_atr": 1.0, "trail_from": 2,
            "floor_at_tp1": True, "tp1_close": 0.0, "tp2_close": 0.0}
    assert trail_stop(plan, 100.1, 104.0, 1.0) is None, "no trail before TP2"
    step = ladder_step(plan, {"price": 100.35, "volume": 1.0, "entry": 100.0, "sl": 100.1})
    assert step == {"stage": 2, "close": 0.0, "stop": None, "why": "TP2"}
    assert trail_stop({**plan, "stage": 2}, 100.1, 104.0, 1.0) == 103.0


import pytest


@pytest.mark.asyncio
async def test_an_unbacktested_or_unapproved_pair_is_never_traded(monkeypatch):
    from app.agents import execution, pair_gate

    class _Row:
        approved, reason = False, "win rate 82.7% below 90%"

    async def _none(db, s, tf):
        return None

    async def _bad(db, s, tf):
        return _Row()

    monkeypatch.setattr(pair_gate, "approval", _none)
    with pytest.raises(execution.Blocked, match="not been backtested"):
        await execution._room_entry_gates(None, "US30", "2h")
    monkeypatch.setattr(pair_gate, "approval", _bad)
    with pytest.raises(execution.Blocked, match="not approved"):
        await execution._room_entry_gates(None, "US30", "2h")


def test_a_yen_cross_is_sized_in_dollars_not_yen():
    from plugins.TelegramSignalNewsPlugin.backend.services.sniper_service import affordable_mt5_lot

    common = dict(equity=1_000_000, free_margin=1_000_000, leverage=100, risk_pct=1.0, entry=210.0,
                  stop_loss=209.0, symbol="GBPJPY", floor_lot=0.01, max_risk_pct=5.0)
    lots, _ = affordable_mt5_lot(**common, quote_to_usd=1 / 150.0)
    # 1 JPY × 100,000 × (1/150) = $666.67 per lot → $10,000 buys 15 lots
    assert lots == 15.0
    legacy, _ = affordable_mt5_lot(**common)
    assert legacy == 0.1, "without conversion the yen loss was counted as dollars"


def test_new_orders_keep_the_margin_level_above_the_warning_floor():
    from app.agents.execution import _margin_headroom

    class A:
        equity, margin, free_margin = 1_000_000.0, 200_000.0, 800_000.0

    class S:
        margin_warn_pct = 300.0

    # 1,000,000 / 3 = 333,333 total margin allowed; 133,333 left; ÷0.8 for the sizer
    assert round(_margin_headroom(A(), S())) == 166_667
    A.margin = 400_000.0
    assert _margin_headroom(A(), S()) == 0.0, "already below 300% — nothing new"


@pytest.mark.asyncio
async def test_below_the_daily_floor_only_the_strongest_pairs_may_open(monkeypatch):
    from types import SimpleNamespace

    from app.agents import execution, pair_gate

    s = SimpleNamespace(daily_min_win_rate=0.90, daily_recovery_min_wr=0.97)
    record = {"wins": 5, "losses": 1, "win_rate": 5 / 6}

    async def _targets(db, st):
        return {"targets": []}

    async def _day(db, accounts, since=None):
        return record

    async def _row(db, sym, tf):
        return SimpleNamespace(win_rate={"XAUUSD": 0.98, "US30": 0.92}[sym])

    monkeypatch.setattr(execution, "mt5_targets", _targets)
    monkeypatch.setattr(target_pacer, "room_day_record", _day)
    monkeypatch.setattr(pair_gate, "approval", _row)
    await execution._daily_floor(None, s, "XAUUSD", "2h")          # 98% backtest → may still trade
    with pytest.raises(execution.Blocked, match="below the 90% daily floor"):
        await execution._daily_floor(None, s, "US30", "2h")
    record.update(wins=9, losses=1, win_rate=0.9)                   # back at the floor → everyone trades
    await execution._daily_floor(None, s, "US30", "2h")
    record.update(wins=0, losses=0, win_rate=None)                  # nothing closed yet
    await execution._daily_floor(None, s, "US30", "2h")
    await execution._daily_floor(None, SimpleNamespace(daily_min_win_rate=0, daily_recovery_min_wr=0.97),
                                 "US30", "2h")                      # floor off


@pytest.mark.asyncio
async def test_the_report_totals_the_traded_book_and_every_pair(monkeypatch):
    from types import SimpleNamespace

    from app.agents import execution
    from app.backtest import service

    async def _settings(db):
        return SimpleNamespace(pair_min_win_rate=0.90)

    monkeypatch.setattr(execution, "get_settings", _settings)

    def _res(pnls):
        trades = [{"status": "closed", "pnl": p, "exit_time": 1_790_000_000 + i * 3600,
                   "outcome": "win" if p > 0 else "loss"} for i, p in enumerate(pnls)]
        wins = sum(1 for p in pnls if p > 0)
        return {"from": 1_790_000_000, "to": 1_790_000_000 + 30 * 86400, "trades": {"oblivion": trades},
                "summary": {"oblivion": {"trades": len(pnls), "wins": wins, "losses": len(pnls) - wins,
                                         "breakeven": 0, "net_profit": sum(pnls),
                                         "avg_r": 0.1 if sum(pnls) > 0 else -0.1,
                                         "win_rate": wins / len(pnls)}}}

    results = {"GOOD": _res([10.0] * 19 + [-50.0]), "BAD": _res([10.0] * 6 + [-50.0] * 4)}
    params = {"start_balance": 10_000, "weekly_target_usd": 0, "daily_target_usd": 0}
    all_pairs = service._totals(results, 10_000)["oblivion"]
    assert all_pairs["gross_win"] == 250.0 and all_pairs["gross_loss"] == 250.0 and all_pairs["net_profit"] == 0.0
    traded = await service._traded_book(results, params)
    assert list(traded["approved"]) == ["GOOD"] and "BAD" in traded["rejected"]
    t = traded["totals"]["oblivion"]
    assert (t["gross_win"], t["gross_loss"], t["net_profit"], t["win_rate"]) == (190.0, 50.0, 140.0, 0.95)
