"""A failing provider must sit out for as long as its failure deserves.

The provider list felt broken because every failure was treated alike: a hard
"out of credit" and a one-off blip both came back in two minutes, so dead
providers kept taking a slot in front of working ones and every call paid for
them again.
"""

from __future__ import annotations

import time

import pytest

from plugins.AiMarketAnalyst.backend.services import ai_router as r


class _Resp:
    def __init__(self, status: int, headers: dict | None = None):
        self.status_code = status
        self.headers = headers or {}


class _Err(Exception):
    def __init__(self, status: int | None = None, headers: dict | None = None, msg: str = "boom"):
        super().__init__(msg)
        self.response = _Resp(status, headers) if status else None


@pytest.fixture(autouse=True)
def _clean():
    r._circuits.clear()
    r._cb_failures.clear()
    yield
    r._circuits.clear()
    r._cb_failures.clear()


def _open_for(pid: int) -> float:
    return r._circuits.get(pid, 0.0) - time.time()


def test_out_of_credit_sits_out_far_longer_than_a_blip():
    r._cb_trip(1, exc=_Err(402))
    r._cb_trip(2, exc=_Err(msg="connection reset"))
    assert _open_for(1) > 600
    assert _open_for(2) < 600


def test_rate_limit_backs_off_further_each_time():
    """A free tier at its daily cap answers 429 all day; stop hammering it."""
    seen = []
    for _ in range(4):
        r._cb_trip(9, exc=_Err(429))
        seen.append(_open_for(9))
        r._circuits.pop(9, None)  # isolate each step from the max() floor
    assert seen == sorted(seen)
    assert seen[-1] > seen[0]


def test_an_explicit_retry_after_is_honoured():
    r._cb_trip(3, exc=_Err(429, {"retry-after": "240"}))
    assert 230 < _open_for(3) < 250


def test_a_nonsense_retry_after_falls_back_to_the_ladder():
    r._cb_trip(4, exc=_Err(429, {"retry-after": "soon"}))
    assert _open_for(4) > 0


def test_success_clears_the_breaker_and_the_escalation():
    """A recovered provider must not stay backed off for the old penalty."""
    for _ in range(3):
        r._cb_trip(5, exc=_Err(429))
    assert r._cb_open(5)

    r._cb_reset(5)

    assert not r._cb_open(5)
    assert 5 not in r._cb_failures
    r._cb_trip(5, exc=_Err(429))
    assert _open_for(5) == pytest.approx(r._CB_LADDER[0], abs=2)


def test_a_later_trip_never_shortens_an_open_breaker():
    r._cb_trip(6, exc=_Err(402))       # long
    long_until = r._circuits[6]
    r._cb_trip(6, exc=_Err(msg="timed out"))  # short
    assert r._circuits[6] == long_until


# ── Timeouts ─────────────────────────────────────────────────────────────────

def test_big_models_get_a_deadline_they_can_actually_meet():
    """40s on a 550B model is a guaranteed timeout that then trips the breaker."""
    assert r.timeout_for_model("nvidia/nemotron-3-ultra-550b-a55b") > r._TIMEOUT
    assert r.timeout_for_model("thinkingmachines/inkling") > r._TIMEOUT
    assert r.timeout_for_model("z-ai/glm-5.2") > r._TIMEOUT


def test_ordinary_models_keep_the_standard_deadline():
    assert r.timeout_for_model("gpt-4o-mini") == r._TIMEOUT
    assert r.timeout_for_model(None) == r._TIMEOUT


def test_an_explicit_timeout_always_wins():
    assert r.timeout_for_model("thinkingmachines/inkling", 7.0) == 7.0


# ── Every provider backing off at once ───────────────────────────────────────

@pytest.mark.asyncio
async def test_a_pool_entirely_in_cooldown_is_still_tried(monkeypatch):
    """A breaker is a backoff hint; all of them together is a lockout.

    Observed: SambaNova 429-ing into a 900s cooldown while NVIDIA timed out into
    a 240s one left the trading room with no provider at all — "AI calls: 0",
    every seat on a local read, and a bot that looked like it had stopped
    thinking. One pass through the breakers beats that; a provider that really
    is down just re-trips on the way past.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from plugins.AiMarketAnalyst.backend.models import AIBase, AILLMProvider
    from plugins.AiMarketAnalyst.backend.services import ai_router

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(AIBase.metadata.create_all)

    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        for pid in (1, 2):
            db.add(AILLMProvider(
                id=pid, provider_key="nvidia_nim", label=f"P{pid}",
                type="openai_compatible", api_key="nvapi-test",
                base_url="https://integrate.api.nvidia.com/v1",
                default_model="m", models_json=["m"], enabled=True, priority=pid,
            ))
        await db.commit()

        monkeypatch.setenv("OPENMANUS_ENABLED", "false")
        monkeypatch.setattr(ai_router, "_headroom_compress", None)
        ai_router._endpoints_repaired = True
        # Both providers deep in cooldown, as they were during the incident.
        ai_router._circuits.update({1: time.time() + 900, 2: time.time() + 240})

        called: list[str] = []

        async def _served(**kwargs):
            called.append(kwargs["model"])
            return '{"action": "buy"}', {
                "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
            }, kwargs["model"], {"content": '{"action": "buy"}'}

        monkeypatch.setattr(ai_router, "_call_openai_compatible_msg", _served)

        res = await ai_router.db_chat(
            db, [{"role": "user", "content": "read XAUUSD"}],
            source="agent", json_mode=True,
        )

    ai_router._circuits.clear()
    await engine.dispose()

    assert res["ok"], f"pool stayed locked out: {res.get('error')}"
    assert called, "no provider was tried"
