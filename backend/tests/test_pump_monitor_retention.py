"""Regression tests for pump monitor pumped-token retention filtering."""

import asyncio
import os
import tempfile
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.database import get_db
from app.core.timezone import now_sast
from app.main import app
from app.models.database import Base, PumpStatus, PumpToken


@pytest.fixture()
def pump_monitor_client():
    """Create an isolated API client + DB session factory for pump monitor tests."""
    db_fd, db_path = tempfile.mkstemp(prefix="pump_monitor_test_", suffix=".db")
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

    async def _setup_db():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def _override_get_db():
        async with session_factory() as session:
            yield session

    asyncio.run(_setup_db())
    app.dependency_overrides[get_db] = _override_get_db

    try:
        with TestClient(app) as client:
            yield client, session_factory
    finally:
        app.dependency_overrides.clear()

        async def _dispose_engine():
            await engine.dispose()

        asyncio.run(_dispose_engine())
        if os.path.exists(db_path):
            os.remove(db_path)


def test_pump_monitor_excludes_stale_pumped_rows_in_list_and_stats(pump_monitor_client):
    """Ensure stale PumpStatus.PUMPED rows are excluded from list + stats APIs."""
    client, session_factory = pump_monitor_client

    retention_hours = settings.PUMP_MONITOR_PUMPED_RETENTION_HOURS
    fresh_symbol = "FRESHPUMP"
    stale_symbol = "STALEPUMP"

    async def _seed_pumped_tokens():
        now = now_sast()
        fresh_detected_at = now - timedelta(hours=max(retention_hours - 1, 0), minutes=5)
        stale_detected_at = now - timedelta(hours=retention_hours + 2)

        fresh = PumpToken(
            coin_id="fresh-pump",
            symbol=fresh_symbol,
            name="Fresh Pump",
            price_at_detection=1.0,
            current_price=1.7,
            pump_score=0.92,
            peak_gain_pct=70.0,
            status=PumpStatus.PUMPED,
            detected_at=fresh_detected_at,
            updated_at=fresh_detected_at,
        )

        stale = PumpToken(
            coin_id="stale-pump",
            symbol=stale_symbol,
            name="Stale Pump",
            price_at_detection=1.0,
            current_price=1.4,
            pump_score=0.88,
            peak_gain_pct=40.0,
            status=PumpStatus.PUMPED,
            detected_at=stale_detected_at,
            updated_at=stale_detected_at,
        )

        async with session_factory() as session:
            session.add_all([fresh, stale])
            await session.commit()

    asyncio.run(_seed_pumped_tokens())

    # Default list endpoint should exclude stale pumped rows.
    response = client.get("/api/v1/pump-monitor/")
    assert response.status_code == 200
    payload = response.json()
    listed_symbols = {row["symbol"] for row in payload["tokens"]}

    assert fresh_symbol in listed_symbols
    assert stale_symbol not in listed_symbols

    # Explicit pumped status should still exclude stale pumped rows.
    response = client.get("/api/v1/pump-monitor/", params={"status": "pumped"})
    assert response.status_code == 200
    payload = response.json()
    pumped_symbols = {row["symbol"] for row in payload["tokens"]}

    assert fresh_symbol in pumped_symbols
    assert stale_symbol not in pumped_symbols

    # Stats should count only fresh pumped rows and recent_pumps should exclude stale.
    response = client.get("/api/v1/pump-monitor/stats")
    assert response.status_code == 200
    payload = response.json()

    assert payload["status_counts"].get("pumped", 0) == 1

    recent_pump_symbols = {row["symbol"] for row in payload["recent_pumps"]}
    assert fresh_symbol in recent_pump_symbols
    assert stale_symbol not in recent_pump_symbols
