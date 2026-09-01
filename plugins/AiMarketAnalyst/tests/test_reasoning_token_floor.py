"""A structured answer is never clamped to the per-agent token ceiling.

Models that think before answering spend the first stretch of the budget on it,
and how much grows with the prompt. At the agent ceiling the JSON decision is cut
off mid-object: the provider returns 200 with a truncated body, the caller cannot
parse it, and a perfectly healthy key is recorded as having produced nothing. The
trading room then reports the seat as a local read.

The floor cannot be conditioned on recognising the model, because the model that
answers is frequently not the one that was asked for — a seat configured for a
model the provider does not carry is served by the provider's default. So every
structured agent call gets the room, whatever ends up serving it. It is a ceiling
rather than a spend: tokens bill as produced.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plugins.AiMarketAnalyst.backend.models import AIBase, AILLMProvider  # noqa: E402
from plugins.AiMarketAnalyst.backend.services import ai_router  # noqa: E402

REASONING_MODEL = "nvidia/nemotron-3.5-lightning-30b-a3b"
PLAIN_MODEL = "mistral-small-latest"


@pytest_asyncio.fixture()
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(AIBase.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


@pytest.fixture(autouse=True)
def _no_side_routes(monkeypatch):
    """Keep the call on the provider loop: no OpenManus hop, no compression."""
    monkeypatch.setenv("OPENMANUS_ENABLED", "false")
    monkeypatch.setattr(ai_router, "_headroom_compress", None)
    ai_router._endpoints_repaired = True
    ai_router._circuits.clear()
    yield
    ai_router._endpoints_repaired = False


async def _provider(db, model: str) -> AILLMProvider:
    p = AILLMProvider(
        provider_key="nvidia_nim",
        label="NVIDIA NIM",
        type="openai_compatible",
        api_key="nvapi-test",
        base_url="https://integrate.api.nvidia.com/v1",
        default_model=model,
        models_json=[model],
        enabled=True,
        priority=1,
    )
    db.add(p)
    await db.commit()
    return p


def _capture(monkeypatch) -> dict:
    """Record the budget the router hands the provider, and answer trivially."""
    seen: dict = {}

    async def _fake_call(**kwargs):
        seen.update(kwargs)
        message = {"role": "assistant", "content": '{"action": "hold"}'}
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        return message["content"], usage, kwargs["model"], message

    monkeypatch.setattr(ai_router, "_call_openai_compatible_msg", _fake_call)
    return seen


@pytest.mark.asyncio
async def test_a_reasoning_model_is_not_clamped_to_the_agent_ceiling(db, monkeypatch):
    await _provider(db, REASONING_MODEL)
    seen = _capture(monkeypatch)

    res = await ai_router.db_chat(
        db,
        [{"role": "user", "content": "read XAUUSD"}],
        max_tokens=3000,
        source="agent",
        json_mode=True,
    )

    assert res["ok"]
    # The structured floor, not the per-agent ceiling: how much a model spends
    # thinking grows with the prompt, and the ceiling exists to protect quota
    # from the scanner, not to cut a decision off mid-object.
    assert seen["max_tokens"] == ai_router._STRUCTURED_FLOOR


@pytest.mark.asyncio
async def test_the_floor_holds_even_when_the_caller_asks_for_less(db, monkeypatch):
    """A 800-token budget on a reasoning model buys nothing at all, not less."""
    await _provider(db, REASONING_MODEL)
    seen = _capture(monkeypatch)

    await ai_router.db_chat(
        db,
        [{"role": "user", "content": "read XAUUSD"}],
        max_tokens=800,
        source="agent",
        json_mode=True,
    )

    assert seen["max_tokens"] >= ai_router._MIN_REASONING_TOKENS


@pytest.mark.asyncio
async def test_a_plain_model_still_respects_the_agent_ceiling(db, monkeypatch):
    """The ceiling exists to protect free-tier quota — only reasoning escapes it."""
    await _provider(db, PLAIN_MODEL)
    seen = _capture(monkeypatch)

    settings = await ai_router.get_router_settings(db)
    settings.per_agent_max_tokens = 1000
    await db.commit()

    await ai_router.db_chat(
        db,
        [{"role": "user", "content": "read XAUUSD"}],
        max_tokens=5000,
        source="agent",
        json_mode=False,
    )

    assert seen["max_tokens"] == 3000  # the clamp's own prose floor, not the 5000 asked for


@pytest.mark.asyncio
async def test_a_structured_answer_gets_the_higher_floor(db, monkeypatch):
    """A truncated JSON object is unreadable, so it never gets the prose floor.

    Sized from the model that serves, not the one that was asked for: a seat
    configured for a model this provider does not carry is served by the
    provider's default, and budgeting for the absent name starves the one that
    actually replies.
    """
    await _provider(db, PLAIN_MODEL)
    seen = _capture(monkeypatch)

    settings = await ai_router.get_router_settings(db)
    settings.per_agent_max_tokens = 800
    await db.commit()

    await ai_router.db_chat(
        db,
        [{"role": "user", "content": "read XAUUSD"}],
        max_tokens=3000,
        source="agent",
        json_mode=True,
    )

    assert seen["max_tokens"] == ai_router._STRUCTURED_FLOOR
