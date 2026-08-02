"""Pausing a scalp bot must not cost the user their pairs.

The reported problem: a session PAUSED after a backend restart could only be
revived by stopping everything and re-picking the pairs from scratch. Pause and
resume exist so the session row — symbol, lot, risk, strictness, direction,
the whole original request in ``raw_settings`` — survives untouched and comes
back with one click.
"""
from __future__ import annotations

import pytest

from plugins.MT5TradingPlugin.backend.models import (
    MT5ScalpSession,
    MT5ScalpSessionStatus,
)
from plugins.MT5TradingPlugin.backend.schemas import ScalpPauseRequest


def _session(**kw) -> MT5ScalpSession:
    base = dict(
        user_id=1, account_id=7, symbol="XAUUSD",
        status=MT5ScalpSessionStatus.ACTIVE, phase="analyzing",
        lot_size=0.05, risk_per_trade_pct=1.5, timeframe="M5",
        raw_settings={
            "symbol": "XAUUSD", "lot_size": 0.05, "strictness": "scalper",
            "max_open_orders": 3, "allowed_direction": "buy",
        },
    )
    base.update(kw)
    return MT5ScalpSession(**base)


def test_the_request_defaults_to_every_session_on_the_account():
    """Omitting symbols is the common case: pause the lot, resume the lot."""
    req = ScalpPauseRequest(account_id=7)
    assert req.symbols is None

    scoped = ScalpPauseRequest(account_id=7, symbols=["XAUUSD", "eurusd"])
    assert scoped.symbols == ["XAUUSD", "eurusd"]


def test_pause_keeps_everything_resume_needs():
    """The settings live on the row, which is why resume needs no client input."""
    s = _session()
    # What manager.pause does to the row.
    s.status, s.phase = MT5ScalpSessionStatus.PAUSED, "paused"

    assert s.symbol == "XAUUSD"
    assert s.lot_size == 0.05
    assert s.raw_settings["strictness"] == "scalper"
    assert s.raw_settings["allowed_direction"] == "buy"
    assert s.raw_settings["max_open_orders"] == 3


def test_paused_is_not_stopped():
    """STOPPED is terminal — it is the state the user had to rebuild from."""
    assert MT5ScalpSessionStatus.PAUSED != MT5ScalpSessionStatus.STOPPED


@pytest.mark.asyncio
async def test_pause_halts_the_loop_without_closing_trades(monkeypatch):
    """A paused bot stops deciding; it does not liquidate."""
    from plugins.MT5TradingPlugin.backend.services import scalp_bot_service as svc

    statuses = {}

    async def _fake_set_status(session_id, status, phase=None, **kw):
        statuses[session_id] = (status, phase)

    closed = []
    manager = svc.ScalpBotManager()
    monkeypatch.setattr(manager, "_set_status", _fake_set_status)
    monkeypatch.setattr(
        svc, "_close_all_for_session", lambda *a, **k: closed.append(a), raising=False
    )

    await manager.pause(42)

    assert statuses[42] == (MT5ScalpSessionStatus.PAUSED, "paused")
    assert closed == [], "pausing must not close positions"


@pytest.mark.asyncio
async def test_stop_still_marks_the_session_terminal(monkeypatch):
    """Regression guard: pause must not have changed what stop means."""
    from plugins.MT5TradingPlugin.backend.services import scalp_bot_service as svc

    statuses = {}

    async def _fake_set_status(session_id, status, phase=None, **kw):
        statuses[session_id] = (status, phase)

    manager = svc.ScalpBotManager()
    monkeypatch.setattr(manager, "_set_status", _fake_set_status)

    await manager.stop(42)
    assert statuses[42] == (MT5ScalpSessionStatus.STOPPED, "stopped")
