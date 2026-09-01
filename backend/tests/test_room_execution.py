"""The gates between an agent opinion and a real order.

Every test here is a way money could leave the account by accident. The default
posture must be inert: execution off, dry run on, nothing sent.
"""
from __future__ import annotations

import pytest

from app.agents import execution
from app.models.database import RoomSettings


@pytest.fixture(autouse=True)
def quiet_bus(monkeypatch):
    published: list[tuple[str, dict]] = []

    async def _capture(topic, data):
        published.append((topic, data))

    monkeypatch.setattr(execution.event_bus, "publish", _capture)
    monkeypatch.setattr(execution, "_today", "")
    monkeypatch.setattr(execution, "_trades_today", 0)
    return published


def _result(**over):
    base = {
        "symbol": "XAUUSD",
        "final_action": "buy",
        "final_confidence": 0.9,
        "decisions": [],
        "signal": {"id": 1},
        "price": 2000.0,
    }
    base.update(over)
    return base


def _consensus(agreement=1.0):
    return {"tally": {"buy": 4, "sell": 0, "hold": 0}, "leader": "buy",
            "agreement": agreement, "weighted_confidence": 0.9}


@pytest.mark.asyncio
async def test_defaults_are_inert(async_session):
    """A fresh install must not be able to place a trade."""
    s = await execution.get_settings(async_session)
    assert s.execution_enabled is False
    assert s.dry_run is True
    assert s.allow_crypto is False
    assert s.allow_mt5 is False


@pytest.mark.asyncio
async def test_execution_off_blocks_everything(async_session):
    report = await execution.execute_decision(async_session, _result(), _consensus())
    assert report["status"] == "skipped"
    assert "switched off" in report["reason"]


@pytest.mark.asyncio
async def test_hold_is_never_traded(async_session):
    s = await execution.get_settings(async_session)
    s.execution_enabled = True
    await async_session.commit()

    report = await execution.execute_decision(
        async_session, _result(final_action="hold"), _consensus()
    )
    assert report["status"] == "skipped"
    assert "no tradeable action" in report["reason"]


@pytest.mark.asyncio
async def test_weak_consensus_is_refused(async_session):
    s = await execution.get_settings(async_session)
    s.execution_enabled = True
    s.min_consensus = 0.7
    await async_session.commit()

    report = await execution.execute_decision(async_session, _result(), _consensus(agreement=0.5))
    assert report["status"] == "skipped"
    assert "consensus" in report["reason"]


@pytest.mark.asyncio
async def test_low_confidence_is_refused(async_session):
    s = await execution.get_settings(async_session)
    s.execution_enabled = True
    s.min_confidence = 0.7
    await async_session.commit()

    report = await execution.execute_decision(
        async_session, _result(final_confidence=0.4), _consensus()
    )
    assert report["status"] == "skipped"
    assert "confidence" in report["reason"]


@pytest.mark.asyncio
async def test_daily_cap_stops_a_runaway_loop(async_session, monkeypatch):
    s = await execution.get_settings(async_session)
    s.execution_enabled = True
    s.max_trades_per_day = 2
    await async_session.commit()

    import time
    monkeypatch.setattr(execution, "_today", time.strftime("%Y-%m-%d"))
    monkeypatch.setattr(execution, "_trades_today", 2)

    report = await execution.execute_decision(async_session, _result(), _consensus())
    assert report["status"] == "skipped"
    assert "daily cap" in report["reason"]


@pytest.mark.asyncio
async def test_a_dry_run_still_fills_on_paper(async_session, monkeypatch):
    """Dry run routes to the paper account — it does not mute the desk.

    An order that is only ever logged proves the plumbing and nothing else. The
    point of the dry run is a real record on an account where being wrong is
    free, so the fill has to actually happen.
    """
    async def _filled(db, s, **kw):
        return {"venue": "sim", "role": "paper", "status": "placed",
                "reason": "sim order filled"}

    monkeypatch.setattr(execution, "_sim_fill", _filled)
    s = await execution.get_settings(async_session)
    s.execution_enabled = True
    s.allow_sim = True
    s.dry_run = True
    await async_session.commit()

    report = await execution.execute_decision(async_session, _result(), _consensus())
    assert report["status"] == "placed"
    assert report["order"]["venue"] == "sim"
    assert [o["venue"] for o in report["order"]["orders"]] == ["sim"]


@pytest.mark.asyncio
async def test_no_enabled_venue_places_nothing(async_session):
    s = await execution.get_settings(async_session)
    s.execution_enabled = True
    s.allow_sim = False
    await async_session.commit()

    report = await execution.execute_decision(async_session, _result(), _consensus())
    assert report["status"] == "skipped"
    assert "no venue" in report["reason"]


# ── Position sizing ─────────────────────────────────────────────────────────


def test_risk_sizing_matches_the_stop_distance():
    # $10k at 1% = $100 risk. Gold is 100oz/lot, so a $10 stop costs $1000/lot.
    assert execution.mt5_volume_for_risk(
        equity=10_000, risk_pct=1.0, entry=2000, stop_loss=1990, symbol="XAUUSD"
    ) == pytest.approx(0.10)


def test_a_wider_stop_buys_a_smaller_position():
    tight = execution.mt5_volume_for_risk(
        equity=10_000, risk_pct=1.0, entry=2000, stop_loss=1990, symbol="XAUUSD")
    wide = execution.mt5_volume_for_risk(
        equity=10_000, risk_pct=1.0, entry=2000, stop_loss=1950, symbol="XAUUSD")
    assert wide < tight


def test_zero_stop_distance_is_refused_not_divided_by():
    with pytest.raises(execution.Blocked):
        execution.mt5_volume_for_risk(
            equity=10_000, risk_pct=1.0, entry=2000, stop_loss=2000, symbol="XAUUSD")


def test_volume_never_rounds_below_the_broker_minimum():
    """A tiny account must produce a valid lot, not 0.0 (which brokers reject)."""
    assert execution.mt5_volume_for_risk(
        equity=50, risk_pct=1.0, entry=2000, stop_loss=1900, symbol="XAUUSD") == 0.01


# ── SL/TP derivation ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_levels_bracket_the_entry_on_a_buy():
    sl, tp, _ = await execution._levels_from(
        [{"stop_loss_pct": 2, "take_profit_pct": 4}], 100.0, "buy")
    assert sl == pytest.approx(98.0)
    assert tp == pytest.approx(104.0)


@pytest.mark.asyncio
async def test_levels_invert_for_a_sell():
    sl, tp, _ = await execution._levels_from(
        [{"stop_loss_pct": 2, "take_profit_pct": 4}], 100.0, "sell")
    assert sl == pytest.approx(102.0)
    assert tp == pytest.approx(96.0)


@pytest.mark.asyncio
async def test_risk_manager_adjustment_overrides_the_signal():
    sl, _, _ = await execution._levels_from(
        [{"stop_loss_pct": 5}, {"adjusted_sl_pct": 1}], 100.0, "buy"
    )
    assert sl == pytest.approx(99.0)


# ── The levels the order actually carries ────────────────────────────────────

def _decisions(**over):
    signal = {
        "agent_role": "signal_generator",
        "action": "buy",
        "entry_zone": [4498.35, 4505.50],
        "stop_loss": 4475.0,
        "take_profits": [4515.0, 4525.0, 4535.0, 4545.0],
    }
    signal.update(over)
    return [signal]


@pytest.mark.asyncio
async def test_the_order_carries_the_levels_the_board_agreed(monkeypatch):
    """Not a flat 2%/4% band — on gold that is a 90-point stop for a 26-point plan."""
    monkeypatch.setattr(
        "app.services.candles.fetch", _no_candles(monkeypatch), raising=False,
    )
    stop, take_profit, source = await execution._levels_from(
        _decisions(), price=4501.9, action="buy", symbol="",
    )
    assert stop == 4475.0
    assert take_profit == 4545.0, "the ladder's furthest rung, not the nearest"
    assert "board levels" in source


@pytest.mark.asyncio
async def test_a_trade_is_held_for_the_far_target_not_taken_off_at_the_first():
    _, take_profit, _ = await execution._levels_from(
        _decisions(take_profits=[4515.0, 4525.0]), price=4501.9, action="buy", symbol="",
    )
    assert take_profit == 4525.0


@pytest.mark.asyncio
async def test_percentages_still_fill_in_what_the_board_left_blank():
    stop, take_profit, source = await execution._levels_from(
        [{"agent_role": "signal_generator", "action": "buy"}],
        price=100.0, action="buy", symbol="",
    )
    assert stop == pytest.approx(98.0)
    assert take_profit == pytest.approx(104.0)
    assert "percentage" in source


def _no_candles(monkeypatch):
    async def _fetch(*a, **kw):
        return []
    return _fetch


# ── Which account takes the trade ────────────────────────────────────────────

@pytest.fixture
def _accounts(monkeypatch):
    """Two MT5 accounts, addressed by id the way ``db.get`` would."""
    class _Acct:
        def __init__(self, id, login):
            self.id, self.login, self.server = id, login, "Srv"
            self.password_encrypted = "x"

    demo, live = _Acct(5, "demo-login"), _Acct(6, "live-login")

    async def _get(model, ident):
        return {5: demo, 6: live}.get(ident)

    return demo, live, _get


@pytest.mark.asyncio
async def test_a_dry_run_can_never_reach_the_live_account(async_session, _accounts, monkeypatch):
    """The one guarantee this routing exists to make."""
    demo, live, _get = _accounts
    monkeypatch.setattr(async_session, "get", _get)

    s = await execution.get_settings(async_session)
    s.dry_run = True
    s.mt5_demo_account_id, s.mt5_account_id = 5, 6

    routing = await execution.mt5_targets(async_session, s)
    assert routing["targets"] == [demo]
    assert live not in routing["targets"]
    assert "not touched" in routing["note"]


@pytest.mark.asyncio
async def test_armed_the_demo_takes_the_trade_alongside_the_live_account(
    async_session, _accounts, monkeypatch
):
    """The demo stays a running mirror, so there is always something to watch."""
    demo, live, _get = _accounts
    monkeypatch.setattr(async_session, "get", _get)

    s = await execution.get_settings(async_session)
    s.dry_run = False
    s.mt5_demo_account_id, s.mt5_account_id = 5, 6

    routing = await execution.mt5_targets(async_session, s)
    assert routing["targets"] == [demo, live], "demo first, and both of them"


@pytest.mark.asyncio
async def test_one_account_in_both_slots_is_one_order(async_session, _accounts, monkeypatch):
    demo, _live, _get = _accounts
    monkeypatch.setattr(async_session, "get", _get)

    s = await execution.get_settings(async_session)
    s.dry_run = False
    s.mt5_demo_account_id, s.mt5_account_id = 5, 5

    routing = await execution.mt5_targets(async_session, s)
    assert routing["targets"] == [demo]


@pytest.mark.asyncio
async def test_a_dry_run_with_no_demo_account_says_so_rather_than_trading_live(
    async_session, _accounts, monkeypatch
):
    _demo, _live, _get = _accounts
    monkeypatch.setattr(async_session, "get", _get)

    s = await execution.get_settings(async_session)
    s.execution_enabled = True
    s.allow_mt5 = True
    s.dry_run = True
    s.mt5_demo_account_id, s.mt5_account_id = None, 6
    await async_session.commit()

    report = await execution.execute_decision(async_session, _result(), _consensus())
    assert report["status"] == "skipped"
    assert "no MT5 demo account" in report["reason"]
    assert all(o["status"] != "placed" for o in report["order"]["orders"])


@pytest.mark.asyncio
async def test_the_same_decision_is_not_placed_twice(async_session):
    """The board decides and the card publishes — two paths, one trade."""
    execution.forget_orders()
    assert not execution.ordered_recently("XAUUSD")
    execution._note_order("XAU/USD")
    assert execution.ordered_recently("XAUUSD"), "spelling must not defeat the guard"
    execution.forget_orders()


@pytest.mark.asyncio
async def test_a_published_signal_is_taken_on_the_demo_account(
    async_session, _accounts, monkeypatch
):
    """Every card the agents send has an order behind it — that is the record."""
    demo, _live, _get = _accounts
    monkeypatch.setattr(async_session, "get", _get)
    execution.forget_orders()

    sent: list[dict] = []

    async def _place(db, account, s, **kw):
        sent.append({"account": account, **kw})
        return {"status": "placed", "ticket": "T1", "role": "demo", **kw}

    monkeypatch.setattr(execution, "_place_on", _place)

    s = await execution.get_settings(async_session)
    s.execution_enabled, s.allow_mt5, s.dry_run = True, True, True
    s.mt5_demo_account_id, s.mt5_account_id = 5, 6
    await async_session.commit()

    report = await execution.mirror_published_card(
        async_session, symbol="XAUUSD", side="buy", entry=4501.9,
        stop_loss=4475.0, take_profits=[4515.0, 4525.0, 4535.0],
    )
    assert report["status"] == "placed"
    assert [o["account"] for o in sent] == [demo], "the live account stays untouched"
    assert sent[0]["take_profit"] == 4535.0, "held for the far target"


@pytest.mark.asyncio
async def test_a_card_is_not_traded_twice_when_the_board_already_placed_it(
    async_session, _accounts, monkeypatch
):
    _demo, _live, _get = _accounts
    monkeypatch.setattr(async_session, "get", _get)
    execution.forget_orders()
    execution._note_order("XAUUSD")

    s = await execution.get_settings(async_session)
    s.execution_enabled, s.allow_mt5 = True, True
    await async_session.commit()

    report = await execution.mirror_published_card(
        async_session, symbol="XAUUSD", side="buy", entry=4501.9,
        stop_loss=4475.0, take_profits=[4515.0],
    )
    assert report["status"] == "skipped"
    assert "doubling up" in report["reason"]
    execution.forget_orders()


# ── Which venues take the trade ──────────────────────────────────────────────

def _policy(**over):
    """A settings object with only the fields the router reads."""
    class _S:
        allow_mt5 = True
        allow_crypto = True
        allow_sim = True
        mt5_account_id = 6
        mt5_demo_account_id = 5
        dry_run = True
        risk_pct = 1.0
        max_leverage = 10
    s = _S()
    for k, v in over.items():
        setattr(s, k, v)
    return s


def test_a_crypto_pair_goes_to_the_exchange_not_the_broker():
    """The old if/elif gave MT5 every trade and left the exchange idle."""
    venues = execution.venues_for("BTCUSDT", _policy())
    assert "crypto" in venues and "mt5" not in venues


def test_gold_and_the_crosses_go_to_the_broker():
    for symbol in ("XAUUSD", "CADJPY", "US30"):
        venues = execution.venues_for(symbol, _policy())
        assert "mt5" in venues and "crypto" not in venues, symbol


def test_a_crypto_pair_falls_back_to_the_broker_when_the_exchange_is_off():
    venues = execution.venues_for("BTCUSDT", _policy(allow_crypto=False))
    assert venues[:1] == ["mt5"]


def test_the_paper_account_records_every_trade_whatever_else_runs():
    assert "sim" in execution.venues_for("XAUUSD", _policy())
    assert "sim" in execution.venues_for("BTCUSDT", _policy())
    assert "sim" not in execution.venues_for("XAUUSD", _policy(allow_sim=False))


def test_nothing_is_routed_when_no_venue_is_enabled():
    flat = _policy(allow_mt5=False, allow_crypto=False, allow_sim=False)
    assert execution.venues_for("XAUUSD", flat) == []


@pytest.mark.asyncio
async def test_the_paper_venue_can_actually_place_an_order(async_session, monkeypatch):
    """It called a method that does not exist, so it had never once filled."""
    from app.trading.simulation import SimulationEngine

    assert not hasattr(SimulationEngine, "execute_signal"), (
        "the engine's entry point is place_order — if this changes, _sim_fill must too"
    )

    seen: dict = {}

    class _Account:
        balance, is_active = 10_000.0, True

    async def _account(db):
        return _Account()

    async def _place_order(**kw):
        seen.update(kw)
        return {"success": True, "order_id": 7}

    monkeypatch.setattr(SimulationEngine, "get_or_create_account", _account)
    monkeypatch.setattr(SimulationEngine, "place_order", _place_order)

    out = await execution._sim_fill(
        async_session, _policy(), symbol="XAUUSD", action="buy",
        price=4500.0, stop_loss=4450.0, take_profit=4600.0,
    )
    assert out["status"] == "placed"
    assert seen["side"] == "buy" and seen["stop_loss"] == 4450.0
    # Metals are sized at 0.45× risk (post-mortem 2026-08-28: 1% on a large demo caps at $4.5k on gold).
    # 0.45% of 10,000 = 45 risked over a 50-point stop is 0.9 units, not "whatever fits".
    assert seen["amount"] == pytest.approx(0.9, rel=0.01)


@pytest.mark.asyncio
async def test_crypto_is_left_alone_in_a_dry_run(async_session):
    """The exchange has no demo, so a dry run must not reach real crypto."""
    out = await execution._crypto_fill(
        async_session, _policy(dry_run=True), symbol="BTCUSDT", signal_id=1,
    )
    assert out["status"] == "skipped"
    assert "no demo" in out["reason"]
