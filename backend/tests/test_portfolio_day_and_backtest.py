"""The portfolio day band (10% minimum, 20% cap) and the account-level backtest."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agents import target_pacer
from app.backtest.portfolio import BookTrade, PortfolioParams, simulate

DAY = 1_790_035_200  # a Tuesday 00:00 UTC


def test_the_day_band_is_measured_on_the_portfolio_at_midnight():
    d = target_pacer.portfolio_day(value_now=1_150_000, today_usd=150_000, min_pct=10, max_pct=20)
    assert d["day_start_value"] == 1_000_000 and d["target_usd"] == 100_000 and d["cap_usd"] == 200_000
    assert d["min_hit"] and not d["capped"] and d["progress_pct"] == 15.0
    assert target_pacer.portfolio_day(value_now=1_200_000, today_usd=200_000, min_pct=10, max_pct=20)["capped"]
    assert target_pacer.portfolio_day(value_now=1_000_000, today_usd=0, min_pct=10, max_pct=0)["cap_usd"] is None


def test_day_pacing_stays_inside_the_risk_band():
    # 10% of $1M left with 0.84R expected today → 11.9% → clamped to 5%
    assert target_pacer.day_risk(remaining_usd=100_000, expected_r_left=0.84, value=1_000_000, min_pct=1, max_pct=5) == 5.0
    assert target_pacer.day_risk(remaining_usd=10_000, expected_r_left=0.84, value=1_000_000, min_pct=1, max_pct=5) == 1.19
    assert target_pacer.day_risk(remaining_usd=0, expected_r_left=0.84, value=1_000_000, min_pct=1, max_pct=5) == 1.0


@pytest.mark.asyncio
async def test_no_new_entry_once_the_day_is_capped(monkeypatch):
    from app.agents import execution, pair_gate

    async def _row(db, sym, tf):
        return SimpleNamespace(approved=True, reason="ok", win_rate=0.99)

    async def _settings(db):
        return SimpleNamespace(news_pre_min=30, news_cooloff_min=120, daily_min_win_rate=0)

    async def _state(db, fresh=False):
        return {"entries_paused": False, "day_capped": True, "daily": {"progress_pct": 21.3, "max_pct": 20}}

    async def _no_news(*a, **k):
        return None

    from app.signals import news_gate

    monkeypatch.setattr(pair_gate, "approval", _row)
    monkeypatch.setattr(execution, "get_settings", _settings)
    monkeypatch.setattr(target_pacer, "state", _state)
    monkeypatch.setattr(news_gate, "live_block", _no_news)
    with pytest.raises(execution.Blocked, match="20% daily cap"):
        await execution._room_entry_gates(None, "XAUUSD", "2h")


def _t(h: float, r: float, outcome: str = "win", wr: float = 0.95, hours: float = 1.0) -> BookTrade:
    return BookTrade(int(DAY + h * 3600), int(DAY + (h + hours) * 3600), r, outcome, "XAUUSD", "1h", wr)


def test_the_backtest_stops_opening_trades_once_the_day_is_capped():
    p = PortfolioParams(daily_min_pct=10, daily_max_pct=20, min_risk_pct=5, max_risk_pct=5, pacing=False, daily_floor=0)
    # +1R at 5% each: 4 wins bank 21.5% compounded → the 5th and 6th are not taken
    out = simulate([_t(i * 2, 1.0) for i in range(6)], 100_000, p, r_per_week=5)
    s = out["summary"]
    assert s["trades"] == 4 and s["skipped"]["daily cap"] == 2 and s["days_capped"] == 1


def test_past_the_minimum_trades_drop_to_the_band_floor():
    p = PortfolioParams(daily_min_pct=10, daily_max_pct=0, min_risk_pct=1, max_risk_pct=5, pacing=True, daily_floor=0)
    out = simulate([_t(0, 3.0), _t(2, 0.1), _t(4, 0.1)], 100_000, p, r_per_week=5)
    risks = [t["risk_pct"] for t in out["trades"]]
    assert risks[0] == 5.0            # far from the minimum: top of the band
    assert risks[1] == 1.0            # +15% banked: the floor


def test_below_the_daily_floor_only_strong_pairs_open_in_the_backtest():
    p = PortfolioParams(daily_min_pct=0, daily_max_pct=0, pacing=False, daily_floor=0.9, elite_win_rate=0.97)
    trades = [_t(0, -1.0, "loss"), _t(2, 0.2, wr=0.95), _t(3, 0.2, wr=0.98)]
    out = simulate(trades, 100_000, p, r_per_week=5)
    assert out["summary"]["skipped"]["daily floor"] == 1
    assert [t["r"] for t in out["trades"]] == [-1.0, 0.2]


def test_losses_and_profit_add_up_to_the_net():
    p = PortfolioParams(daily_min_pct=0, daily_max_pct=0, pacing=False, daily_floor=0)
    out = simulate([_t(0, 1.0), _t(26, -1.0, "loss"), _t(50, 0.5)], 100_000, p, r_per_week=5)
    s = out["summary"]
    assert round(s["gross_win"] - s["gross_loss"], 2) == s["net_profit"]
    assert s["losing_days"] == 1 and s["wins"] == 2 and s["losses"] == 1
