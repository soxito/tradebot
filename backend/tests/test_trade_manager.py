"""Real-time management: margin protection, taking profit when the market
turns, and stops that only ever move toward profit."""
from __future__ import annotations

import pytest

from app.agents import trade_manager as tm


@pytest.fixture(autouse=True)
def _clean():
    tm.reset_state()
    yield
    tm.reset_state()


def _pos(key, profit, volume=1.0):
    return {"key": key, "profit": profit, "volume": volume}


def test_below_critical_margin_the_worst_loser_is_cut_first():
    plan = tm.margin_plan(level=120, balance=10_000, warn_pct=300, critical_pct=150, max_drawdown_pct=50,
                          positions=[_pos("a", -50), _pos("b", -400), _pos("c", 90)])
    assert plan["action"] == "reduce"
    assert plan["cuts"] == [("b", 0.5)]


def test_below_the_warning_level_entries_stop_but_nothing_is_closed():
    plan = tm.margin_plan(level=250, balance=10_000, warn_pct=300, critical_pct=150, max_drawdown_pct=50,
                          positions=[_pos("a", -10)])
    assert plan["action"] == "warn" and plan["cuts"] == []


def test_the_drawdown_cap_closes_every_room_position():
    plan = tm.margin_plan(level=900, balance=10_000, warn_pct=300, critical_pct=150, max_drawdown_pct=5,
                          positions=[_pos("a", -300), _pos("b", -250)])
    assert plan["action"] == "flatten" and {k for k, _ in plan["cuts"]} == {"a", "b"}


def test_a_min_lot_position_is_closed_rather_than_split():
    plan = tm.margin_plan(level=100, balance=10_000, warn_pct=300, critical_pct=150, max_drawdown_pct=50,
                          positions=[_pos("a", -10, volume=0.01)])
    assert plan["cuts"] == [("a", 0.01)]


def _shift(**over):
    base = dict(side="buy", entry=100.0, price=103.0, stop=98.0, volume=1.0, opposite_15m=False,
                opposite_5m=False, structure_broke=False, momentum_against=False, sudden_move=False,
                fraction=0.5, already_partial=False)
    base.update(over)
    return tm.shift_plan(**base)


def test_an_opposite_15m_signal_banks_a_winning_trade():
    assert _shift(opposite_15m=True)["action"] == "close"


def test_a_5m_turn_banks_half_and_locks_break_even():
    plan = _shift(opposite_5m=True)
    assert plan["action"] == "take_partial" and plan["volume"] == 0.5
    assert plan["new_stop"] == 100.0


def test_a_momentum_flip_alone_is_not_enough_without_a_sudden_move():
    assert _shift(momentum_against=True)["action"] == "hold"
    assert _shift(momentum_against=True, sudden_move=True)["action"] == "take_partial"


def test_a_losing_trade_is_left_to_the_guardian():
    assert _shift(price=99.0, opposite_15m=True)["action"] == "hold"


def test_the_stop_never_moves_backward():
    plan = _shift(stop=101.0, opposite_5m=True)
    assert plan["new_stop"] is None, "a stop already beyond entry is not pulled back to it"
    assert _shift(stop=101.0, opposite_5m=True, already_partial=True)["action"] == "hold"


def test_shorts_mirror_longs():
    plan = _shift(side="sell", entry=100.0, price=97.0, stop=102.0, opposite_5m=True)
    assert plan["action"] == "take_partial" and plan["new_stop"] == 100.0


def test_a_sudden_adverse_move_is_measured_in_atr():
    samples = [(0, 100.0), (1, 101.0), (2, 99.4)]
    assert tm.adverse_move(samples, is_long=True, atr=1.0, mult=1.5)
    assert not tm.adverse_move(samples, is_long=True, atr=2.0, mult=1.5)
    assert tm.adverse_move([(0, 100.0), (1, 98.0), (2, 99.6)], is_long=False, atr=1.0, mult=1.5)


@pytest.mark.asyncio
async def test_margin_crisis_cuts_the_worst_room_position_and_blocks_entries(monkeypatch):
    from plugins.MT5TradingPlugin.backend.services import mt5_client as client_mod

    calls = []

    class _Client:
        async def get_account_info(self, **_):
            return {"balance": 10_000, "equity": 9_000, "margin": 7_000, "marginLevel": 128}

        async def get_positions(self, **_):
            return [
                {"ticket": 1, "symbol": "XAUUSD", "type": 0, "lots": 1.0, "openPrice": 100,
                 "currentPrice": 99, "profit": -100, "comment": "ROOM#1"},
                {"ticket": 2, "symbol": "EURUSD", "type": 0, "lots": 2.0, "openPrice": 1.1,
                 "currentPrice": 1.09, "profit": -600, "comment": "ROOM#2"},
                {"ticket": 3, "symbol": "EURUSD", "type": 0, "lots": 5.0, "openPrice": 1.1,
                 "currentPrice": 1.0, "profit": -9000, "comment": ""},  # the user's own
            ]

        async def close_position(self, **kw):
            calls.append(("close", kw["ticket"], kw.get("volume")))

        async def modify_order(self, **kw):
            calls.append(("modify", kw["ticket"], kw.get("sl")))

    async def _has(db, account_id):
        return True

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(client_mod, "mt5_client", _Client())
    monkeypatch.setattr(tm, "_mt5_has_room_positions", _has)
    monkeypatch.setattr(tm, "_announce_once", _noop)
    monkeypatch.setattr(tm, "_atr", _noop)

    async def _calm(symbol, side):
        return {"opposite_15m": False, "opposite_5m": False,
                "structure_broke": False, "momentum_against": False}

    monkeypatch.setattr(tm, "market_shift", _calm)

    class _Acct:
        id, login, server, password_encrypted = 5, "1", "Demo", "x"

    class _S:
        margin_warn_pct, margin_critical_pct, max_account_drawdown_pct = 300, 150, 50
        shift_atr_mult, shift_take_profit_fraction = 1.5, 0.5

    acts = await tm.manage_mt5_account(None, _Acct(), _S(), send=True)
    assert ("close", 2, 1.0) in calls, "the worst ROOM loser is halved"
    assert all(c[1] != 3 for c in calls), "a manual position is never touched"
    assert tm.entries_blocked("mt5", 5)
    assert acts
