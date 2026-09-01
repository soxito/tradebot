"""Unit tests for the agent-decision settlement loop.

This is the missing half of the room's learning loop: buy/sell decisions must
be resolved into win/loss from closed trades in BOTH order books, and seats
with enough resolved evidence become eligible for the self-improve pass.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest  # noqa: E402
pytest.importorskip("sqlalchemy")

os.environ.setdefault("KRONOS_WARMUP", "0")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.models.database import (  # noqa: E402
    AgentDecision,
    SimPosition,
    Trade,
)
from app.services.decision_settlement import (
    _outcome_for,
    settle_agent_decisions,
)


def _dec(symbol: str, action: str, hours_ago: float) -> AgentDecision:
    return AgentDecision(
        agent_id=1,
        agent_name="Naledi",
        agent_role="signal_generator",
        symbol=symbol,
        action=action,
        confidence=0.7,
        created_at=datetime.utcnow() - timedelta(hours=hours_ago),
    )


def _live_close(symbol: str, side: str, pnl: float, *, opened_h: float, closed_h: float) -> Trade:
    return Trade(
        exchange="bitget",
        symbol=symbol,
        side=side,
        trade_side="open",
        order_type="market",
        amount=1.0,
        status="closed",
        pnl=pnl,
        created_at=datetime.utcnow() - timedelta(hours=opened_h),
        closed_at=datetime.utcnow() - timedelta(hours=closed_h),
    )


def _sim_close(symbol: str, side: str, pnl: float, *, opened_h: float, closed_h: float) -> SimPosition:
    return SimPosition(
        account_id=1,
        symbol=symbol,
        side=side,
        amount=1.0,
        entry_price=100.0,
        status="closed",
        realized_pnl=pnl,
        created_at=datetime.utcnow() - timedelta(hours=opened_h),
        closed_at=datetime.utcnow() - timedelta(hours=closed_h),
    )


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def scalar_one_or_none(self):
        return None


class _FakeDB:
    """Minimal AsyncSession stand-in: returns queued result sets in order."""

    def __init__(self, results):
        self._results = list(results)
        self.committed = 0

    async def execute(self, *_a, **_k):
        return _FakeResult(self._results.pop(0) if self._results else [])

    async def commit(self):
        self.committed += 1

    async def rollback(self):
        pass


def test_outcome_for_sign_of_pnl():
    assert _outcome_for(10.0) == "win"
    assert _outcome_for(-3.5) == "loss"
    assert _outcome_for(0.0) == "break_even"
    assert _outcome_for(None) == "break_even"


@pytest.mark.asyncio
async def test_buy_decision_settles_from_closed_live_trade():
    """A BUY decision matched by a closed long trade → win with its PnL."""
    dec = _dec("BTCUSDT", "buy", hours_ago=2)
    # Opened 1.9h ago, closed 1h ago — inside the decision's match window.
    trade = _live_close("BTC/USDT", "buy", pnl=25.0, opened_h=1.9, closed_h=1.0)
    db = _FakeDB([[dec], [trade], []])
    stats = await settle_agent_decisions(db)
    assert stats["settled"] == 1
    assert dec.outcome == "win"
    assert dec.outcome_pnl == 25.0
    assert db.committed == 1


@pytest.mark.asyncio
async def test_sell_decision_settles_from_closed_sim_position():
    dec = _dec("XAUUSD", "sell", hours_ago=3)
    pos = _sim_close("xauusd", "short", pnl=-12.5, opened_h=2.8, closed_h=0.5)
    db = _FakeDB([[dec], [], [pos]])
    stats = await settle_agent_decisions(db)
    assert stats["settled"] == 1
    assert dec.outcome == "loss"
    assert dec.outcome_pnl == -12.5


@pytest.mark.asyncio
async def test_direction_mismatch_never_settles():
    """A SELL decision must not absorb the outcome of a losing long."""
    dec = _dec("ETHUSDT", "sell", hours_ago=2)
    losing_long = _live_close("ETHUSDT", "buy", pnl=-40.0, opened_h=1.5, closed_h=1.0)
    db = _FakeDB([[dec], [losing_long], []])
    stats = await settle_agent_decisions(db)
    assert stats["settled"] == 0
    assert dec.outcome is None


@pytest.mark.asyncio
async def test_trade_opened_before_the_decision_does_not_match():
    dec = _dec("SOLUSDT", "buy", hours_ago=10)
    older_trade = _live_close("SOLUSDT", "buy", pnl=99.0, opened_h=12.0, closed_h=11.0)
    db = _FakeDB([[dec], [older_trade], []])
    stats = await settle_agent_decisions(db)
    assert stats["settled"] == 0
    assert dec.outcome is None


@pytest.mark.asyncio
async def test_hold_and_already_resolved_decisions_are_ignored():
    hold = _dec("ADAUSDT", "hold", hours_ago=1)
    resolved = _dec("DOTUSDT", "buy", hours_ago=2)
    resolved.outcome = "win"
    trade = _live_close("DOTUSDT", "buy", pnl=5.0, opened_h=1.5, closed_h=1.0)
    db = _FakeDB([[hold, resolved], [trade], []])
    stats = await settle_agent_decisions(db)
    assert stats["settled"] == 0
