"""Backtest engine, the room's tuned profiles, and the live ladder that mirrors them."""
from __future__ import annotations

from app.agents import oblivion_gate
from app.agents.trade_manager import ladder_step
from app.backtest.oblivion_backtest import BacktestParams, _Book, run_backtest
from app.services.candles import _conform


def _rows(n=600, step_ms=3_600_000):
    rows, price = [], 100.0
    for i in range(n):
        drift = 0.6 if (i // 40) % 2 == 0 else -0.5
        o = price
        c = price + drift + (0.3 if i % 3 else -0.4)
        rows.append([1_700_000_000_000 + i * step_ms, o, max(o, c) + 0.4, min(o, c) - 0.4, c, 1000.0])
        price = c
    return rows


def test_hourly_bars_labelled_2h_are_folded_into_real_2h_bars():
    folded = _conform(_rows(100), "2h")
    assert len(folded) in (50, 51)
    assert folded[1][0] - folded[0][0] == 7_200_000


def test_bars_already_at_the_timeframe_are_untouched():
    rows = _rows(50, 7_200_000)
    assert _conform(rows, "2h") == rows


def test_the_backtest_books_trades_with_consistent_accounting():
    res = run_backtest(_rows(), BacktestParams(symbol="XAUUSD", timeframe="1h"), with_agents=False)
    s = res["summary"]["oblivion"]
    closed = [t for t in res["trades"]["oblivion"] if t["status"] == "closed"]
    assert s["trades"] == len(closed)
    assert abs(sum(t["pnl"] for t in closed) - s["net_profit"]) < 0.01 * max(1, len(closed))
    assert s["wins"] + s["losses"] + s["breakeven"] == s["trades"]


def test_a_stop_is_hit_before_a_target_inside_one_bar():
    book = _Book("x", BacktestParams(symbol="XAUUSD", timeframe="1h", cost_pct=0.0))
    sig = {"action": "buy", "stop_loss": 98.0, "tp1": 101.0, "tp2": 102.0, "tp3": 103.0,
           "time": 1, "entry": 100.0}
    rows = [[0, 100, 100, 100, 100], [3_600_000, 100.0, 104.0, 97.0, 100.0]]
    book.rr = (0.5, 1.0, 1.5)
    book.open(sig, 1, rows, None)
    book.manage(1, rows, False)
    assert book.trades[-1]["outcome"] == "loss" and book.trades[-1]["r"] == -1.0


def test_tp1_banks_part_and_the_rest_exits_at_break_even_as_a_win():
    book = _Book("x", BacktestParams(symbol="XAUUSD", timeframe="1h", cost_pct=0.0))
    book.rr = (0.5, 1.0, 1.5)
    sig = {"action": "buy", "stop_loss": 98.0, "tp1": 101.0, "tp2": 102.0, "tp3": 103.0, "time": 1, "entry": 100.0}
    rows = [[0, 100, 100, 100, 100], [3_600_000, 100.0, 101.2, 99.5, 101.0], [7_200_000, 101.0, 101.0, 99.0, 99.5]]
    book.open(sig, 1, rows, None)
    book.manage(1, rows, False)
    book.manage(2, rows, False)
    t = book.trades[-1]
    assert t["exit_reason"] == "break-even stop" and t["outcome"] == "win" and t["r"] == 0.25


def test_the_2h_profile_plans_a_pullback_limit_with_targets_from_it():
    last = {"action": "buy", "kind": "scalp", "score": 7, "entry": 100.0, "stop_loss": 94.0,
            "tp1": 100.6, "tp2": 106.0, "tp3": 109.0, "time": 1, "bars_since": 1,
            "outcome": {"status": "open"}, "atr": 2.0, "bias": 1}
    read = {"signal": {"action": "wait"}, "last_signal": last, "news": {}}
    assert not oblivion_gate.verdict(read, 100.5, "2h", -1)["tradeable"], "against the daily trend"
    assert not oblivion_gate.verdict({**read, "last_signal": {**last, "bias": -1}}, 100.5, "2h", 1)["tradeable"]
    v = oblivion_gate.verdict(read, 100.5, "2h", 1)
    assert v["tradeable"] and v["entry_mode"] == "limit"
    assert v["limit"] == 99.0                      # 0.5 ATR back toward the stop
    assert round(v["tp1"], 6) == round(99.0 + 0.1 * 5.0, 6)   # 0.1R measured from the limit


def test_the_ladder_banks_tp1_then_tp2_and_only_moves_the_stop_forward():
    plan = {"side": "buy", "stage": 0, "tp1": 101.0, "tp2": 105.0, "tp1_close": 0.5, "tp2_close": 0.25}
    pos = {"price": 101.2, "volume": 1.0, "entry": 100.0, "sl": 95.0}
    step = ladder_step(plan, pos)
    assert step == {"stage": 1, "close": 0.5, "stop": 100.0, "why": "TP1"}   # fixed ladder: break-even
    plan["stage"] = 1
    step = ladder_step(plan, {**pos, "price": 105.5, "volume": 0.5})
    assert step["stage"] == 2 and step["close"] == 0.25 and step["stop"] == 101.0
    assert ladder_step({**plan, "stage": 0}, {**pos, "price": 100.5}) is None


def test_tp1_locks_the_stop_at_tp1_and_holds_the_whole_position():
    from app.agents.trade_manager import trail_stop

    plan = {"side": "buy", "stage": 0, "tp1": 101.0, "tp2": 105.0, "tp1_close": 0.0, "tp2_close": 0.0,
            "trail_atr": 1.0, "floor_at_tp1": True}
    step = ladder_step(plan, {"price": 101.1, "volume": 1.0, "entry": 100.0, "sl": 95.0})
    assert step == {"stage": 1, "close": 0.0, "stop": 101.0, "why": "TP1"}
    plan["stage"] = 1
    assert ladder_step(plan, {"price": 106.0, "volume": 1.0, "entry": 100.0, "sl": 101.0}) is None, "no TP2 partial"
    # Trails 1 ATR behind the close, never below TP1, never backward.
    assert trail_stop(plan, 101.0, 101.5, 1.0) is None            # trail 100.5 < TP1 floor
    assert trail_stop(plan, 101.0, 104.0, 1.0) == 103.0
    assert trail_stop(plan, 103.0, 103.5, 1.0) is None            # would move backward
    assert trail_stop({**plan, "side": "sell", "tp1": 99.0}, 99.0, 96.0, 1.0) == 97.0


def test_a_trade_rides_past_tp3_until_the_trail_is_hit():
    book = _Book("x", BacktestParams(symbol="XAUUSD", timeframe="1h", cost_pct=0.0, trail_atr=1.0,
                                    floor_at_tp1=True, hold_past_tp3=True, tp1_close=0.0, tp2_close=0.0,
                                    be_after_tp1=False, trail_after_tp2=False))
    book.rr = (0.1, 1.0, 1.5)
    book.atr = [1.0] * 10
    sig = {"action": "buy", "stop_loss": 98.0, "tp1": 100.2, "tp2": 102.0, "tp3": 103.0, "time": 1, "entry": 100.0}
    rows = [[i * 3_600_000, *bar] for i, bar in enumerate([
        (100, 100, 100, 100), (100, 100.5, 99.8, 100.4), (100.4, 104, 100.3, 103.8),
        (103.8, 106, 103.5, 105.8), (105.8, 106, 104.5, 104.6)])]
    book.open(sig, 1, rows, None)
    for i in range(1, 5):
        if book.pos:
            book.manage(i, rows, False)
    t = book.trades[-1]
    assert t["exit_reason"] == "trailed stop" and t["reached"] == 3
    assert t["r"] > 1.5, "held past TP3 and exited on the trail above it"


def test_a_stop_inside_the_noise_is_never_planned_or_sized():
    from app.signals.oblivion_profiles import entry_plan

    flat = {"action": "buy", "entry": 0.70028, "stop_loss": 0.70027, "atr": 0.00001}
    assert entry_plan(flat, 0.5, (0.2, 0.3, 1.5)) is None, "0.001% stop on flat bars"
    real = {"action": "buy", "entry": 0.7010, "stop_loss": 0.6990, "atr": 0.0006}
    assert entry_plan(real, 0.5, (0.2, 0.3, 1.5))["limit"] == 0.7007
