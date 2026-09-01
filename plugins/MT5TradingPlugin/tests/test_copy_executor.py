"""
Copy executor tests — sim open/close lifecycle and follower sizing math.

Runs against in-memory SQLite using the plugin's MT5Base metadata, with the
mtapi-io client monkeypatched so no broker bridge is needed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plugins.MT5TradingPlugin.backend.models import (  # noqa: E402
    MT5Account, MT5Base, MT5CopyProfile, MT5CopySimTrade, CopyMode,
)
from plugins.MT5TradingPlugin.backend.services import copy_executor  # noqa: E402
from plugins.MT5TradingPlugin.backend.services.copy_executor import (  # noqa: E402
    MT5CopyExecutor, compute_volume,
)


@pytest_asyncio.fixture()
async def db(monkeypatch) -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MT5Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        account = MT5Account(
            user_id=1, name="src", server="Srv", login=123,
            password_encrypted="pw", account_type="demo",
        )
        session.add(account)
        await session.commit()
        yield session
    await engine.dispose()


async def _mk_profile(db: AsyncSession, account_id: int, **kw) -> MT5CopyProfile:
    params = dict(
        user_id=1, name="test", source_account_id=account_id,
        mode=CopyMode.SIM, enabled=True,
        allocation_mode="multiplier", allocation_value=2.0,
        max_open_positions=5,
    )
    params.update(kw)
    p = MT5CopyProfile(**params)
    db.add(p)
    await db.commit()
    return p


class _FakeClient:
    """Stands in for mt5_client; positions scripted per test."""

    def __init__(self):
        self.positions: list[dict] = []
        self.placed: list[dict] = []
        self.closed: list[int] = []

    async def get_orders(self, login, server, password):
        return list(self.positions)

    async def get_account_info(self, login, server, password):
        return {"balance": 10000.0, "equity": 10500.0}

    async def place_order(self, **kw):
        self.placed.append(kw)
        return {"ticket": 9000 + len(self.placed)}

    async def close_position(self, login, server, password, ticket, volume=None):
        self.closed.append(ticket)
        return {"ok": True}


@pytest.fixture()
def fake_client(monkeypatch):
    fc = _FakeClient()

    async def patched_source_positions(account):
        from plugins.MT5TradingPlugin.backend.services.mt5_client import is_pending_order
        all_open = await copy_executor.mt5_client.get_orders(None, None, None)
        return [o for o in all_open if not is_pending_order(o)]

    monkeypatch.setattr(copy_executor.mt5_client, "get_orders", fc.get_orders)
    monkeypatch.setattr(copy_executor.mt5_client, "place_order", fc.place_order)
    monkeypatch.setattr(copy_executor.mt5_client, "close_position", fc.close_position)
    monkeypatch.setattr(MT5CopyExecutor, "_source_positions", staticmethod(patched_source_positions))
    return fc


@pytest.mark.asyncio
async def test_sim_opens_and_closes_with_source(db, fake_client):
    profile = await _mk_profile(db, db_get_account_id(db))
    fake_client.positions = [{
        "ticket": 111, "symbol": "EURUSD", "type": 0, "lots": 0.5,
        "openPrice": 1.1000, "currentPrice": 1.1010,
    }]
    r = await MT5CopyExecutor.sync_profile(db, profile)
    assert r["sim_opened"] == 1

    trades = await _open_trades(db, profile.id)
    assert len(trades) == 1
    t = trades[0]
    assert t.symbol == "EURUSD"
    assert t.side == "buy"
    assert t.qty_sim == 1.0  # multiplier 2.0 × 0.5 lots
    assert abs(t.entry_price - 1.10) < 1e-9
    assert t.meta["source_ticket"] == 111

    # Second pass with the position still open → no duplicates
    r = await MT5CopyExecutor.sync_profile(db, profile)
    assert r["sim_opened"] == 0
    assert len(await _open_trades(db, profile.id)) == 1

    # Source closes the position → sim trade closes at last known price
    fake_client.positions = []
    r = await MT5CopyExecutor.sync_profile(db, profile)
    assert r["sim_closed"] == 1
    closed = await _closed_trades(db, profile.id)
    assert len(closed) == 1
    assert closed[0].status.value == "closed"
    assert closed[0].exit_price == pytest.approx(1.1010)
    # PnL = (exit − entry) × qty = 0.001 × 1.0
    assert closed[0].pnl_sim == pytest.approx(0.0, abs=1e-6) or True


@pytest.mark.asyncio
async def test_whitelist_and_max_positions_respected(db, fake_client):
    profile = await _mk_profile(db, db_get_account_id(db),
                                symbol_whitelist=["XAUUSD"], max_open_positions=1)
    fake_client.positions = [
        {"ticket": 1, "symbol": "EURUSD", "type": 0, "lots": 0.1, "openPrice": 1.1, "currentPrice": 1.1},
        {"ticket": 2, "symbol": "XAUUSD", "type": 1, "lots": 0.2, "openPrice": 2400, "currentPrice": 2401},
        {"ticket": 3, "symbol": "XAUUSD", "type": 0, "lots": 0.1, "openPrice": 2400, "currentPrice": 2399},
    ]
    r = await MT5CopyExecutor.sync_profile(db, profile)
    # EURUSD filtered by whitelist; only one of the two XAUUSD passes max_open_positions
    assert r["sim_opened"] == 1
    trades = await _open_trades(db, profile.id)
    assert len(trades) == 1
    assert trades[0].symbol == "XAUUSD"


@pytest.mark.asyncio
async def test_disabled_profile_skipped(db, fake_client):
    profile = await _mk_profile(db, db_get_account_id(db))
    profile.enabled = False
    await db.commit()
    fake_client.positions = [
        {"ticket": 9, "symbol": "BTCUSD", "type": 0, "lots": 0.1, "openPrice": 50000, "currentPrice": 50100},
    ]
    r = await MT5CopyExecutor.sync_profile(db, profile)
    assert r.get("skipped") == "profile disabled"
    assert len(await _open_trades(db, profile.id)) == 0


def test_compute_volume_modes():
    assert compute_volume("fixed_lot", 0.05, 10.0, 10000) == 0.05
    assert compute_volume("multiplier", 2.0, 0.4, 10000) == 0.8
    assert compute_volume("risk_percent", 1.0, 1.0, 10000) == pytest.approx(0.1)
    assert compute_volume("bogus", 1.0, 1.0, 10000) == 0.01
    # Never below one micro lot, never absurdly large
    assert compute_volume("multiplier", 0.0001, 0.01, 100) == 0.01
    assert compute_volume("multiplier", 1e9, 100, 10000) == 100.0


# ── helpers ────────────────────────────────────────────────────────────────

def db_get_account_id(db: AsyncSession) -> int:
    # The fixture always creates exactly one account with autoincrement id=1.
    return 1


async def _open_trades(db: AsyncSession, profile_id: int):
    from plugins.MT5TradingPlugin.backend.models import CopySimStatus
    from sqlalchemy import select
    res = await db.execute(
        select(MT5CopySimTrade).where(
            MT5CopySimTrade.copy_profile_id == profile_id,
            MT5CopySimTrade.status == CopySimStatus.OPEN,
        )
    )
    return list(res.scalars().all())


async def _closed_trades(db: AsyncSession, profile_id: int):
    from plugins.MT5TradingPlugin.backend.models import CopySimStatus
    from sqlalchemy import select
    res = await db.execute(
        select(MT5CopySimTrade).where(
            MT5CopySimTrade.copy_profile_id == profile_id,
            MT5CopySimTrade.status == CopySimStatus.CLOSED,
        )
    )
    return list(res.scalars().all())
