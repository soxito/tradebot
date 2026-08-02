from __future__ import annotations

import sys
from pathlib import Path

# Both the repo root and ``backend/`` go on the path so tests import the same
# way the app does at runtime: ``app.*`` from backend, ``plugins.*`` from root.
REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPO_ROOT / "backend"
for root in (REPO_ROOT, BACKEND_ROOT):
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402


@pytest_asyncio.fixture
async def async_session():
    """A throwaway in-memory database with the full core schema created.

    SQLite rather than Postgres so the suite needs no running service; the
    models under test use no Postgres-specific types.
    """
    from app.models.database import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session

    await engine.dispose()
