"""Regression tests for sniper pump-intake flow and dedup lifecycle behavior."""

import os
import tempfile

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.models.database import Base, RugPullStatus, RugPullToken
from app.signals import rug_pull_detector as detector
from app.trading.live import LiveTradeEngine


@pytest_asyncio.fixture()
async def rug_pull_session_factory():
    """Create an isolated async DB session factory for sniper-cycle tests."""
    db_fd, db_path = tempfile.mkstemp(prefix="rug_pull_sniper_", suffix=".db")
    os.close(db_fd)

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        poolclass=NullPool,
    )
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    try:
        yield session_factory
    finally:
        await engine.dispose()
        if os.path.exists(db_path):
            os.remove(db_path)


def _patch_live_settings(monkeypatch, *, sniper_max_entries=1, min_entry_gap_pct=2.0, min_pump_pct=30.0):
    class _Settings:
        pass

    settings = _Settings()
    settings.sniper_max_entries = sniper_max_entries
    settings.min_entry_gap_pct = min_entry_gap_pct
    settings.min_pump_pct = min_pump_pct

    async def _fake_get_or_create_settings(_db):
        return settings

    monkeypatch.setattr(
        LiveTradeEngine,
        "get_or_create_settings",
        staticmethod(_fake_get_or_create_settings),
    )


@pytest.mark.asyncio
async def test_sniper_cycle_exposes_pump_intake_payload(monkeypatch, rug_pull_session_factory):
    """Sniper cycle should surface intake telemetry when pre-scan succeeds."""
    expected_intake = {
        "new": [{"coin_id": "new-coin"}],
        "existing": ["existing-coin"],
        "total_pumped": 2,
        "filtered_out": 0,
    }

    async def _fake_scan_for_pumps(_db):
        return expected_intake

    monkeypatch.setattr(detector, "scan_for_pumps", _fake_scan_for_pumps)
    _patch_live_settings(monkeypatch)

    async with rug_pull_session_factory() as session:
        result = await detector.run_sniper_cycle(session)

    assert result["pump_intake"] == expected_intake
    assert result["scanned"] == 0
    assert result["declining"] == 0
    assert result["details"] == []


@pytest.mark.asyncio
async def test_sniper_cycle_continues_when_pump_intake_fails(monkeypatch, rug_pull_session_factory):
    """Sniper cycle must continue with defaults if intake pre-scan raises."""
    async def _boom(_db):
        raise RuntimeError("intake failure")

    exception_messages = []

    def _capture_exception(message, *args, **kwargs):
        exception_messages.append(message)

    monkeypatch.setattr(detector, "scan_for_pumps", _boom)
    monkeypatch.setattr(detector.logger, "exception", _capture_exception)
    _patch_live_settings(monkeypatch)

    async with rug_pull_session_factory() as session:
        result = await detector.run_sniper_cycle(session)

    assert result["pump_intake"] == {
        "new": [],
        "existing": [],
        "total_pumped": 0,
        "filtered_out": 0,
        "cleanup_removed": 0,
    }
    assert result["scanned"] == 0
    assert any("Pump intake scan failed" in msg for msg in exception_messages)


@pytest.mark.asyncio
async def test_scan_for_pumps_dedups_full_active_lifecycle(monkeypatch, rug_pull_session_factory):
    """Repeated scans should not create duplicate rows across active sniper statuses."""
    markets = [
        {
            "id": "watch-coin",
            "symbol": "watch",
            "name": "Watch",
            "current_price": 1.0,
            "price_change_percentage_24h": 80.0,
        },
        {
            "id": "entry-coin",
            "symbol": "entry",
            "name": "Entry",
            "current_price": 1.0,
            "price_change_percentage_24h": 82.0,
        },
        {
            "id": "short-coin",
            "symbol": "short",
            "name": "Short",
            "current_price": 1.0,
            "price_change_percentage_24h": 84.0,
        },
        {
            "id": "cool-coin",
            "symbol": "cool",
            "name": "Cool",
            "current_price": 1.0,
            "price_change_percentage_24h": 86.0,
        },
        {
            "id": "new-coin",
            "symbol": "new",
            "name": "New",
            "current_price": 2.5,
            "price_change_percentage_24h": 95.0,
        },
    ]

    async def _fake_fetch_markets_sorted_by_gain(min_pump_pct=detector.MIN_PUMP_PCT):
        _ = min_pump_pct
        return markets

    async def _fake_tradeable_symbols():
        return {"WATCH", "ENTRY", "SHORT", "COOL", "NEW"}

    monkeypatch.setattr(detector, "_fetch_markets_sorted_by_gain", _fake_fetch_markets_sorted_by_gain)
    monkeypatch.setattr(detector, "_get_bitget_tradeable_symbols", _fake_tradeable_symbols)
    _patch_live_settings(monkeypatch)

    async with rug_pull_session_factory() as session:
        session.add_all(
            [
                RugPullToken(
                    coin_id="watch-coin",
                    symbol="WATCH",
                    name="Watch",
                    price_at_detection=1.0,
                    price_change_24h=100.0,
                    current_price=1.0,
                    peak_price=1.0,
                    status=RugPullStatus.WATCHING,
                ),
                RugPullToken(
                    coin_id="entry-coin",
                    symbol="ENTRY",
                    name="Entry",
                    price_at_detection=1.0,
                    price_change_24h=100.0,
                    current_price=1.0,
                    peak_price=1.0,
                    status=RugPullStatus.ENTRY_READY,
                ),
                RugPullToken(
                    coin_id="short-coin",
                    symbol="SHORT",
                    name="Short",
                    price_at_detection=1.0,
                    price_change_24h=100.0,
                    current_price=1.0,
                    peak_price=1.0,
                    status=RugPullStatus.SHORTED,
                ),
                RugPullToken(
                    coin_id="cool-coin",
                    symbol="COOL",
                    name="Cool",
                    price_at_detection=1.0,
                    price_change_24h=100.0,
                    current_price=1.0,
                    peak_price=1.0,
                    status=RugPullStatus.COOLING,
                ),
            ]
        )
        await session.commit()

        first = await detector.scan_for_pumps(session)
        second = await detector.scan_for_pumps(session)

        assert set(first["existing"]) == {"watch-coin", "entry-coin", "short-coin", "cool-coin"}
        assert {row["coin_id"] for row in first["new"]} == {"new-coin"}

        assert second["new"] == []
        assert {"watch-coin", "entry-coin", "short-coin", "cool-coin", "new-coin"}.issubset(
            set(second["existing"])
        )

        new_count = (
            await session.execute(
                select(func.count())
                .select_from(RugPullToken)
                .where(RugPullToken.coin_id == "new-coin")
            )
        ).scalar_one()

        tracked_count = (
            await session.execute(
                select(func.count())
                .select_from(RugPullToken)
                .where(
                    RugPullToken.coin_id.in_(
                        ["watch-coin", "entry-coin", "short-coin", "cool-coin", "new-coin"]
                    )
                )
            )
        ).scalar_one()

        assert new_count == 1
        assert tracked_count == 5


@pytest.mark.asyncio
async def test_run_sniper_cycle_repeated_does_not_duplicate_token(monkeypatch, rug_pull_session_factory):
    """Repeated sniper cycles should not create duplicate lifecycle rows for one coin."""
    markets = [
        {
            "id": "cycle-coin",
            "symbol": "cycle",
            "name": "Cycle",
            "current_price": 3.0,
            "price_change_percentage_24h": 99.0,
        }
    ]

    async def _fake_fetch_markets_sorted_by_gain(min_pump_pct=detector.MIN_PUMP_PCT):
        _ = min_pump_pct
        return markets

    async def _fake_tradeable_symbols():
        return {"CYCLE"}

    monkeypatch.setattr(detector, "_fetch_markets_sorted_by_gain", _fake_fetch_markets_sorted_by_gain)
    monkeypatch.setattr(detector, "_get_bitget_tradeable_symbols", _fake_tradeable_symbols)
    _patch_live_settings(monkeypatch)

    async with rug_pull_session_factory() as session:
        first = await detector.run_sniper_cycle(session)
        second = await detector.run_sniper_cycle(session)

        token_count = (
            await session.execute(
                select(func.count())
                .select_from(RugPullToken)
                .where(RugPullToken.coin_id == "cycle-coin")
            )
        ).scalar_one()

    assert {row["coin_id"] for row in first["pump_intake"]["new"]} == {"cycle-coin"}
    assert second["pump_intake"]["new"] == []
    assert "cycle-coin" in second["pump_intake"]["existing"]
    assert token_count == 1