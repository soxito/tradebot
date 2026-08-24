"""The desk stays with the trade after the meeting ends.

Two failures are being guarded against, and they pull in opposite directions:
a stop taken by a liquidity sweep on a trade that was right, and a position held
into a genuine reversal because nothing re-read the market after publication.
The fixtures here are shaped like both.
"""

from __future__ import annotations

import pytest

from app.agents import guard_read
from app.trading import stop_quality


def _bar(o, h, l, c, t=0):
    return [t, o, h, l, c, 100.0]


def _series(prices, *, spread=1.0, start=0):
    """A candle series from a list of closes, each bar a small real range."""
    rows = []
    prev = prices[0]
    for i, close in enumerate(prices):
        high = max(prev, close) + spread
        low = min(prev, close) - spread
        rows.append(_bar(prev, high, low, close, start + i * 60_000))
        prev = close
    return rows


def _zigzag(n, start_price, leg, pullback, *, up=True):
    """A trend that actually breathes: legs with pullbacks, so it has pivots.

    A strictly monotone series has no swing points at all, and swings are what
    both structure and the protective level are read from — a fixture without
    them tests nothing that a real chart would exercise.
    """
    prices = [start_price]
    sign = 1 if up else -1
    while len(prices) < n:
        for _ in range(4):
            prices.append(prices[-1] + sign * leg)
        for _ in range(2):
            prices.append(prices[-1] - sign * pullback)
    # Finish on an impulse, so the series ends having actually gone somewhere
    # rather than mid-pullback — the shape a live chart is read in.
    prices = prices[:n] + [prices[min(len(prices), n) - 1] + sign * leg * (i + 1)
                           for i in range(4)]
    return _series(prices)


def _uptrend(n=80, start_price=100.0, step=1.0):
    return _zigzag(n, start_price, step * 2, step, up=True)


def _downtrend(n=80, start_price=180.0, step=1.0):
    return _zigzag(n, start_price, step * 2, step, up=False)


# ── Stop quality ─────────────────────────────────────────────────────────────

def test_a_stop_inside_one_average_bar_is_pushed_out():
    """The gold plan's failure mode: a stop closer than the market's own range."""
    candles = _uptrend(60, 4400.0, 2.0)   # ~4 wide bars, ATR well above 1
    assessment = stop_quality.assess(
        entry=4520.0, proposed_stop=4519.0, is_long=True, candles=candles,
    )
    assert assessment is not None
    assert assessment.widened
    assert assessment.stop < 4519.0, "a stop inside the noise must be moved out"


def test_a_stop_that_already_clears_the_floor_is_left_alone():
    candles = _uptrend(60, 4400.0, 2.0)
    assessment = stop_quality.assess(
        entry=4520.0, proposed_stop=4400.0, is_long=True, candles=candles,
    )
    assert assessment is not None
    assert not assessment.widened
    assert assessment.stop == 4400.0


def test_a_stop_on_the_wrong_side_of_entry_is_replaced_not_propagated():
    candles = _uptrend(60, 4400.0, 2.0)
    assessment = stop_quality.assess(
        entry=4520.0, proposed_stop=4530.0, is_long=True, candles=candles,
    )
    assert assessment is not None
    assert assessment.stop < 4520.0


def test_widening_a_stop_does_not_widen_the_loss():
    """Sizing derives volume from the stop distance, which is what makes this safe."""
    from app.agents.execution import mt5_volume_for_risk

    tight = mt5_volume_for_risk(
        equity=10_000, risk_pct=1.0, entry=4500, stop_loss=4490, symbol="XAUUSD",
    )
    wide = mt5_volume_for_risk(
        equity=10_000, risk_pct=1.0, entry=4500, stop_loss=4470, symbol="XAUUSD",
    )
    assert wide < tight
    # Same money at risk, to within one broker lot step: a stop three times as
    # far away is carried on a third of the size, not at three times the loss.
    assert wide * 30 <= tight * 10 + 0.01 * 30


# ── Telling a sweep from a break ─────────────────────────────────────────────

def test_a_trade_far_from_its_stop_is_left_alone():
    up = _uptrend()
    verdict = guard_read.assess(
        side="buy", entry=150.0, stop=100.0, take_profits=[200.0], price=149.0,
        ltf_candles=up, htf_candles=up,
    )
    assert verdict.action == "hold"


def test_a_stop_about_to_be_swept_is_moved_and_the_position_cut():
    """Structure intact, price on the stop — the level is the target, not the trade."""
    up = _uptrend()
    price = up[-1][4]
    verdict = guard_read.assess(
        side="buy", entry=price + 4, stop=price - 0.5, take_profits=[price + 40],
        price=price, ltf_candles=up, htf_candles=up,
    )
    assert verdict.verdict == "sweep_risk"
    assert verdict.action == "widen_stop"
    assert verdict.suggested_stop < price - 0.5, "the stop must move away from the raid"
    assert 0 < verdict.reduce_fraction < 1, "risk is held constant by cutting size"


def test_the_widened_stop_is_capped_so_a_loss_stays_measured():
    up = _uptrend()
    price = up[-1][4]
    entry, stop = price + 4, price - 0.5
    verdict = guard_read.assess(
        side="buy", entry=entry, stop=stop, take_profits=[price + 40], price=price,
        ltf_candles=up, htf_candles=up,
    )
    original_risk = entry - stop
    assert (entry - verdict.suggested_stop) <= original_risk * guard_read.MAX_WIDEN_MULTIPLE


def test_a_genuine_reversal_closes_the_trade_rather_than_widening_it():
    """The opposite case: the higher timeframe has actually turned over."""
    down = _downtrend()
    price = down[-1][4]
    verdict = guard_read.assess(
        side="buy", entry=price + 30, stop=price - 0.5, take_profits=[price + 60],
        price=price, ltf_candles=down, htf_candles=down,
    )
    assert verdict.verdict == "invalidated"
    assert verdict.action == "close"


def test_a_working_trade_is_never_closed_early():
    """The other half of the ask: do not take a winner off while it is running."""
    up = _uptrend()
    price = up[-1][4]
    verdict = guard_read.assess(
        side="buy", entry=price - 30, stop=price - 40, take_profits=[price + 30],
        price=price, ltf_candles=up, htf_candles=up,
    )
    assert verdict.verdict == "working"
    assert verdict.action in {"hold", "advance_stop"}
    assert verdict.action != "close"


def test_securing_a_winner_only_ever_moves_the_stop_forward():
    up = _uptrend()
    price = up[-1][4]
    verdict = guard_read.assess(
        side="buy", entry=price - 30, stop=price - 40, take_profits=[price + 10],
        price=price, ltf_candles=up, htf_candles=up,
    )
    if verdict.suggested_stop is not None:
        assert verdict.suggested_stop > price - 40


# ── Structure primitives ─────────────────────────────────────────────────────

def test_a_wick_through_a_level_reads_as_a_sweep_and_a_close_through_it_does_not():
    level = 100.0
    swept = _uptrend(40, 90.0, 0.5) + [_bar(101.0, 101.5, 95.0, 100.8)]
    broken = _uptrend(40, 90.0, 0.5) + [_bar(101.0, 101.2, 95.0, 95.5)]
    assert guard_read.looks_like_sweep(swept, is_long=True, level=level)
    assert not guard_read.looks_like_sweep(broken, is_long=True, level=level)


def test_a_chopping_market_is_reported_as_neutral_not_as_a_reversal():
    """A wrong bias reading here closes a good trade, so disagreement means neutral."""
    chop = _series([100 + (3 if i % 2 else -3) for i in range(60)])
    assert guard_read.structure_bias(chop) == "neutral"


# ── The trade this was built for ─────────────────────────────────────────────

def _gold_fixture():
    """Recorded XAUUSD bars around the published plan that was stopped out.

    The signal: buy 4498.35–4505.50, stop 4475, targets 4515 / 4525 / 4535 /
    4545. Price swept to 4451.41 — taking the stop — and then traded to 4541.41
    within three hours, through the third target. The recording is what the
    guard has to get right.
    """
    import json
    from pathlib import Path

    raw = json.loads((Path(__file__).parent / "fixtures_xauusd_sweep.json").read_text())
    return raw["m15"], raw["h4"]


def test_the_gold_sweep_is_survived_and_the_targets_are_reached():
    """Replay the real trade bar by bar, acting on every verdict as it comes."""
    m15, h4 = _gold_fixture()
    entry, stop = 4501.9, 4475.0
    targets = [4515.0, 4525.0, 4535.0, 4545.0]

    live_stop = stop
    widened_at = None
    stopped_out_at = None
    best_price = entry

    # The trade starts when price is first inside the published band — replaying
    # from the start of the recording would "stop out" on bars from the day
    # before the plan existed.
    start = next(
        i for i, b in enumerate(m15)
        if i > 40 and 4498.35 <= b[4] <= 4505.50
    )

    for i, bar in enumerate(m15):
        if i < start:
            continue
        low, close = bar[3], bar[4]
        if low <= live_stop:
            stopped_out_at = live_stop
            break
        best_price = max(best_price, bar[2])
        verdict = guard_read.assess(
            side="buy", entry=entry, stop=live_stop, take_profits=targets,
            price=close,
            ltf_candles=m15[: i + 1],
            # Only bars that had actually closed by this point — no lookahead.
            htf_candles=[h for h in h4 if h[0] <= bar[0]],
        )
        if verdict.action in {"widen_stop", "advance_stop"} and verdict.suggested_stop:
            if verdict.action == "widen_stop":
                if widened_at is not None:
                    continue          # only ever once, as in the live guard
                widened_at = verdict.suggested_stop
            live_stop = verdict.suggested_stop

    assert widened_at is not None, "the guard has to notice the stop is in the raid zone"
    assert widened_at < 4451.41, (
        "the widened stop must sit under the sweep low, or the trade dies anyway"
    )
    assert stopped_out_at is None or stopped_out_at > entry, (
        "the only acceptable exit is a trailed stop in profit, not the original loss"
    )
    assert best_price >= 4535.0, "the move the analysis called for was reached"


def test_the_gold_plan_would_not_have_been_published_with_that_stop_untouched():
    """The stop was inside the noise; the floor pushes it out before it ships."""
    _, h4 = _gold_fixture()
    assessment = stop_quality.assess(
        entry=4501.9, proposed_stop=4475.0, is_long=True, candles=h4,
    )
    assert assessment is not None
    assert assessment.widened
    assert assessment.stop < 4475.0


# ── Acting on the verdict ────────────────────────────────────────────────────

class _FakeClient:
    """Records what would have been sent to the broker."""

    def __init__(self):
        self.modified: list[dict] = []
        self.closed: list[dict] = []

    async def modify_order(self, **kw):
        self.modified.append(kw)
        return {"ok": True}

    async def close_position(self, **kw):
        self.closed.append(kw)
        return {"ok": True}


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _account():
    return _Obj(id=1, login="1", server="Demo", password_encrypted="x")


def _position(**over):
    base = dict(
        mt5_ticket=99, symbol="XAUUSD", side="buy", volume=0.30,
        price_open=4501.9, price_current=4477.0, sl=4475.0, tp=4545.0,
        comment="ROOM#42",
    )
    base.update(over)
    return _Obj(**base)


@pytest.fixture(autouse=True)
def _clean_guard_state():
    from app.agents import guardian

    guardian.reset_state()
    yield
    guardian.reset_state()


@pytest.mark.asyncio
async def test_a_widened_stop_is_sent_with_the_matching_partial_close():
    from app.agents import guardian

    client = _FakeClient()
    verdict = guard_read.GuardVerdict(
        verdict="sweep_risk", action="widen_stop", suggested_stop=4440.0,
        reduce_fraction=2 / 3, reasons=["sweep"],
    )
    report = await guardian._apply_mt5(
        _account(), _position(), verdict, key="k", send=True, client=client,
    )
    assert report["stop"] == 4440.0
    assert client.modified and client.modified[0]["sl"] == 4440.0
    assert client.closed and client.closed[0]["volume"] == pytest.approx(0.20)


@pytest.mark.asyncio
async def test_a_stop_is_only_ever_widened_once_per_position():
    from app.agents import guardian

    client = _FakeClient()
    verdict = guard_read.GuardVerdict(
        verdict="sweep_risk", action="widen_stop", suggested_stop=4440.0,
        reduce_fraction=0.5, reasons=["sweep"],
    )
    await guardian._apply_mt5(_account(), _position(), verdict, key="k",
                              send=True, client=client)
    guardian._last_action.clear()   # cooldown expired; the widen ban must not
    again = await guardian._apply_mt5(_account(), _position(), verdict, key="k",
                                       send=True, client=client)
    assert again is None
    assert len(client.modified) == 1


@pytest.mark.asyncio
async def test_with_execution_off_the_change_is_prepared_and_not_sent():
    """The room may analyse a position it is not allowed to act on."""
    from app.agents import guardian

    client = _FakeClient()
    verdict = guard_read.GuardVerdict(
        verdict="invalidated", action="close", reasons=["structure broke"],
    )
    report = await guardian._apply_mt5(
        _account(), _position(), verdict, key="k", send=False, client=client,
    )
    assert report["action"] == "close" and report["sent"] is False
    assert not client.closed and not client.modified


@pytest.mark.asyncio
async def test_a_position_too_small_to_cut_keeps_its_size():
    """0.01 lots cannot be split — widen the stop rather than closing the trade."""
    from app.agents import guardian

    client = _FakeClient()
    verdict = guard_read.GuardVerdict(
        verdict="sweep_risk", action="widen_stop", suggested_stop=4440.0,
        reduce_fraction=0.67, reasons=["sweep"],
    )
    report = await guardian._apply_mt5(
        _account(), _position(volume=0.01), verdict, key="k",
        send=True, client=client,
    )
    assert report["closed_volume"] == 0.0
    assert not client.closed
    assert client.modified, "the stop still moves — that is the protection"


def test_only_positions_this_app_opened_are_managed():
    from app.trading.order_tags import is_app_order

    assert is_app_order("ROOM#42")
    assert is_app_order("TG#154270")
    assert not is_app_order("my own trade")


def test_a_locked_in_gain_is_never_given_back_by_widening():
    """Once a target has printed and the stop protects profit, it stays put."""
    from app.agents.guardian import may_widen

    assert may_widen(already_widened=False, trailing_sl=None, tp_reached_count=0)
    # A stop pulled up to protect a printed target is the trade's profit.
    assert not may_widen(already_widened=False, trailing_sl=4510.0, tp_reached_count=0)
    assert not may_widen(already_widened=False, trailing_sl=None, tp_reached_count=3)
    # And never twice on the same trade.
    assert not may_widen(already_widened=True, trailing_sl=None, tp_reached_count=0)


# ── Nothing here may reach a real phone ──────────────────────────────────────

@pytest.fixture(autouse=True)
def _no_telegram(monkeypatch):
    """Belt to the bot service's braces.

    These fixtures describe positions that do not exist. A message about one is
    indistinguishable from the desk malfunctioning on a real trade — and that is
    not hypothetical: before this fixture existed, a run of this file sent
    "Stop moved behind the sweep — XAUUSD #99" to the user's phone four times.
    """
    sent: list[str] = []

    async def _capture(text):
        sent.append(text)

    from app.agents import guardian

    monkeypatch.setattr(guardian, "_announce", _capture)
    return sent


def test_the_bot_service_refuses_to_send_from_a_test_run():
    """The single chokepoint every outbound Telegram call passes through."""
    from plugins.TelegramSignalNewsPlugin.backend.services import bot_service, notifications

    assert bot_service._under_test() is True
    assert notifications._under_test() is True


@pytest.mark.asyncio
async def test_a_fixture_position_cannot_produce_a_real_message():
    from plugins.TelegramSignalNewsPlugin.backend.services import bot_service, notifications

    assert await notifications.notify("fixture — must never be delivered") is False
    result = await bot_service.send_message("fake-token", "1", "fixture")
    assert result["ok"] is False and "test environment" in result["description"]


# ── The exchange side of the book ────────────────────────────────────────────

class _FakeConnector:
    """Records what would have been sent to the exchange."""

    def __init__(self):
        self.tpsl: list[dict] = []
        self.orders: list[dict] = []
        self.exchange = self

    async def place_tpsl_order(self, **kw):
        self.tpsl.append(kw)
        return {"orderId": "1"}

    async def create_order(self, **kw):
        self.orders.append(kw)
        return {"id": "2"}


def _trade(**over):
    base = dict(
        id=1, symbol="BTCUSDT", side="buy", average_price=96_000.0, price=96_000.0,
        stop_loss=95_500.0, take_profit=100_000.0, amount=0.3,
    )
    base.update(over)
    return _Obj(**base)


def _live_pos(**over):
    base = {"symbol": "BTCUSDT", "holdSide": "long", "total": "0.3"}
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_a_swept_crypto_stop_is_moved_and_the_position_cut(async_session):
    from app.agents import guardian

    connector = _FakeConnector()
    verdict = guard_read.GuardVerdict(
        verdict="sweep_risk", action="widen_stop", suggested_stop=94_000.0,
        reduce_fraction=2 / 3, reasons=["sweep"],
    )
    report = await guardian._apply_crypto(
        async_session, connector, _trade(), _live_pos(), verdict, key="c", send=True,
    )
    assert report["stop"] == 94_000.0
    assert connector.tpsl[0]["plan_type"] == "pos_loss"
    assert connector.tpsl[0]["trigger_price"] == 94_000.0
    # The cut is reduce-only, so it can shrink the position and never flip it.
    assert connector.orders[0]["params"]["reduceOnly"] is True
    assert connector.orders[0]["side"] == "sell"
    assert connector.orders[0]["amount"] == pytest.approx(0.2)


@pytest.mark.asyncio
async def test_a_broken_crypto_setup_is_closed_reduce_only(async_session):
    from app.agents import guardian

    connector = _FakeConnector()
    verdict = guard_read.GuardVerdict(
        verdict="invalidated", action="close", reasons=["structure broke"],
    )
    report = await guardian._apply_crypto(
        async_session, connector, _trade(), _live_pos(), verdict, key="c", send=True,
    )
    assert report["closed_size"] == pytest.approx(0.3)
    assert connector.orders[0]["params"]["reduceOnly"] is True
    assert not connector.tpsl


@pytest.mark.asyncio
async def test_a_short_is_closed_by_buying_it_back(async_session):
    from app.agents import guardian

    connector = _FakeConnector()
    verdict = guard_read.GuardVerdict(verdict="invalidated", action="close", reasons=["x"])
    await guardian._apply_crypto(
        async_session, connector, _trade(side="sell"),
        _live_pos(holdSide="short"), verdict, key="c", send=True,
    )
    assert connector.orders[0]["side"] == "buy"


@pytest.mark.asyncio
async def test_with_execution_off_the_exchange_is_not_touched(async_session):
    from app.agents import guardian

    connector = _FakeConnector()
    verdict = guard_read.GuardVerdict(
        verdict="invalidated", action="close", reasons=["structure broke"],
    )
    report = await guardian._apply_crypto(
        async_session, connector, _trade(), _live_pos(), verdict, key="c", send=False,
    )
    assert report["sent"] is False
    assert not connector.orders and not connector.tpsl


@pytest.mark.asyncio
async def test_a_dry_run_never_manages_real_crypto(async_session, monkeypatch):
    """The exchange has no demo — so in a dry run the room leaves it alone."""
    from app.agents import guardian

    called = False

    async def _crypto(db, *, send=True):
        nonlocal called
        called = True
        return []

    async def _settings(db):
        class _S:
            execution_enabled, dry_run = True, True
            mt5_account_id = mt5_demo_account_id = None
        return _S()

    monkeypatch.setattr(guardian, "guard_crypto_positions", _crypto)
    monkeypatch.setattr("app.agents.execution.get_settings", _settings)

    await guardian.guard_cycle(async_session)
    assert called is False


def test_the_position_size_is_read_from_whichever_field_the_venue_uses():
    from app.agents.guardian import _position_size

    assert _position_size({"total": "0.3"}) == pytest.approx(0.3)
    assert _position_size({"available": 1.5}) == pytest.approx(1.5)
    assert _position_size({"contracts": -2}) == pytest.approx(2.0)
    assert _position_size({"total": "0"}) == 0.0
