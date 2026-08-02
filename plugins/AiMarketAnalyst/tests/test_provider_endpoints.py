"""Endpoint routing: retired hosts and the optional compression proxy.

Both bugs these cover presented identically to a user — a 401 that looks like a
bad API key — while the credential was fine and the request was simply going to
the wrong place.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plugins.AiMarketAnalyst.backend.models import AIBase, AILLMProvider  # noqa: E402
from plugins.AiMarketAnalyst.backend.services import ai_router  # noqa: E402
from plugins.AiMarketAnalyst.backend.services.provider_presets import get_preset  # noqa: E402


@pytest_asyncio.fixture()
async def db() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(AIBase.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


@pytest.fixture(autouse=True)
def _fresh_repair_flag():
    """The repair runs once per process; each test needs it armed."""
    ai_router._endpoints_repaired = False
    yield
    ai_router._endpoints_repaired = False


# ── GitHub Models moved off the Azure preview host ───────────────────────────

def test_the_github_preset_points_at_the_live_endpoint():
    preset = get_preset("github_models")
    assert preset["base_url"] == "https://models.github.ai/inference"
    # models.github.ai rejects bare ids — every model needs its publisher.
    assert all("/" in m for m in preset["models"])
    assert preset["default_model"] == "openai/gpt-4o"


def test_the_publisher_prefix_is_added_only_where_it_is_missing():
    assert ai_router._github_model_id("gpt-4o") == "openai/gpt-4o"
    assert ai_router._github_model_id("DeepSeek-R1") == "deepseek/DeepSeek-R1"
    # Already-prefixed and unknown ids are left exactly as they are.
    assert ai_router._github_model_id("openai/o3") == "openai/o3"
    assert ai_router._github_model_id("some-custom-model") == "some-custom-model"
    assert ai_router._github_model_id("") == ""


@pytest.mark.asyncio
async def test_a_provider_on_a_retired_host_repairs_itself(db):
    """The stored row is what actually gets called, so the stored row is fixed.

    Updating only the preset would leave every existing install 401ing against
    a dead host while its key was perfectly good.
    """
    db.add(AILLMProvider(
        provider_key="github_models", label="GitHub Models", type="openai_compatible",
        api_key="ghp_valid_token", base_url="https://models.inference.ai.azure.com",
        default_model="gpt-4o",
        models_json=json.dumps(["gpt-4o", "DeepSeek-R1", "openai/o3"]),
        enabled=True,
    ))
    await db.commit()

    assert await ai_router.repair_retired_endpoints(db) == 1

    # Read back through the ORM, which is how the API reads it. An earlier
    # version of this test used a raw table select, which hid a real bug: the
    # repair wrote json.dumps(...) into a JSON column, so the value came back as
    # a str and failed response validation — 500ing the whole providers page.
    row = (await db.execute(select(AILLMProvider))).scalars().one()
    assert row.base_url == "https://models.github.ai/inference"
    assert row.default_model == "openai/gpt-4o"
    assert isinstance(row.models_json, list), "models_json must never be double-encoded"
    assert row.models_json == ["openai/gpt-4o", "deepseek/DeepSeek-R1", "openai/o3"]
    # The key was never the problem and must not be touched.
    assert row.api_key == "ghp_valid_token"


def test_a_double_encoded_model_list_is_read_back_as_a_list():
    """One malformed row must not be able to take the providers page down."""
    assert ai_router.normalise_model_list(["a", "b"]) == ["a", "b"]
    # The shape that caused the outage.
    assert ai_router.normalise_model_list('["a", "b"]') == ["a", "b"]
    assert ai_router.normalise_model_list(None) == []
    assert ai_router.normalise_model_list("") == []
    assert ai_router.normalise_model_list("not json") == []
    assert ai_router.normalise_model_list('{"not": "a list"}') == []


@pytest.mark.asyncio
async def test_the_repair_leaves_healthy_providers_alone(db):
    db.add(AILLMProvider(
        provider_key="groq", label="Groq", type="openai_compatible",
        api_key="k", base_url="https://api.groq.com/openai/v1",
        default_model="llama-3.3-70b", enabled=True,
    ))
    await db.commit()

    assert await ai_router.repair_retired_endpoints(db) == 0
    # And it is once-per-process, so provider lookup does not pay for it.
    assert await ai_router.repair_retired_endpoints(db) == 0


# ── The compression proxy is optional ────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_proxy_is_not_used_when_it_is_down(monkeypatch):
    """Compression is an optimisation; losing it must not take a provider down."""
    ai_router._headroom_state = (0.0, False)

    class _DeadClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k):
            raise ConnectionError("connection refused")

    monkeypatch.setattr(ai_router.httpx, "AsyncClient", _DeadClient)
    assert await ai_router._headroom_available() is False


def test_only_openai_is_ever_routed_through_the_compression_proxy():
    """The proxy's single upstream is OpenAI.

    NVIDIA used to be routed through it, so an `nvapi-…` key was presented to
    OpenAI and rejected as "Incorrect API key provided" — a failure that named
    the wrong provider and could never be fixed by rotating the NVIDIA key.
    """
    source = Path(ai_router.__file__).read_text()
    routing = source.split("Determine routing", 1)[1].split("headers = {", 1)[0]

    assert 'is_openai = "openai.com" in base_url' in routing
    # Nothing may put a non-OpenAI provider back on the proxy.
    assert "is_nvidia" not in routing
    assert "if is_openai and await _headroom_available():" in routing


@pytest.mark.asyncio
async def test_the_proxy_probe_is_cached(monkeypatch):
    """One probe a minute, not one per model call."""
    calls = []

    class _LiveClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, **k):
            calls.append(url)
            class _R: status_code = 200
            return _R()

    monkeypatch.setattr(ai_router.httpx, "AsyncClient", _LiveClient)
    ai_router._headroom_state = (0.0, False)

    assert await ai_router._headroom_available() is True
    assert await ai_router._headroom_available() is True
    assert await ai_router._headroom_available() is True
    assert len(calls) == 1


# ── Retired vs. misconfigured ────────────────────────────────────────────────

class _Resp:
    def __init__(self, status: int, text: str):
        self.status_code = status
        self.text = text


class _HttpError(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(f"Client error '{status}' for url '...'")
        self.response = _Resp(status, text)


def test_a_retirement_brownout_is_recognised_as_retired_not_misconfigured():
    """GitHub Models answers 410 during its scheduled retirement.

    Both are config faults for circuit-breaker purposes, but only one of them
    is worth regenerating a token over.
    """
    retired = _HttpError(410, '{"error":{"code":"github_models_retirement_brownout",'
                              '"message":"GitHub Models is temporarily unavailable as '
                              'part of a scheduled retirement brownout."}}')
    assert ai_router.config_fault_status(retired) == 410
    assert ai_router.is_retired_upstream(retired) is True

    bad_key = _HttpError(401, '{"error":{"message":"Incorrect API key provided"}}')
    assert ai_router.config_fault_status(bad_key) == 401
    assert ai_router.is_retired_upstream(bad_key) is False

    assert ai_router.is_retired_upstream(RuntimeError("connection reset")) is False


# ── Model lists are audited claims, not wishes ───────────────────────────────

#: Ids removed after probing each provider's live /chat/completions. Each one
#: answered 404 (gone), 410 (retired by the platform), or "unavailable for
#: free". Listing a dead id is not free: the router picks it and burns a full
#: cascade attempt discovering it does not exist.
RETIRED_MODEL_IDS = {
    "groq": ["qwen/qwen3-32b", "meta-llama/llama-4-scout-17b-16e-instruct"],
    "openrouter": [
        "meta-llama/llama-3.3-70b-instruct:free",
        "qwen/qwen3-next-80b-a3b-instruct:free",
        "nousresearch/hermes-3-llama-3.1-405b:free",
        "qwen/qwen3-coder:free",
    ],
    "gemini": ["gemini-3.1-flash", "gemini-3-flash"],
    "sambanova": [
        "Meta-Llama-3.1-405B-Instruct", "DeepSeek-V3-0324", "DeepSeek-R1",
        "Llama-4-Scout-17B-16E-Instruct", "Llama-4-Maverick-17B-128E-Instruct",
        "Qwen3-32B", "Qwen3-72B",
    ],
    "nvidia": [
        "nvidia/llama-3.1-nemotron-ultra-253b-v1",
        "nvidia/llama-3.1-nemotron-70b-instruct",
        "nvidia/llama-3.1-nemotron-51b-instruct",
        "nvidia/nemotron-4-340b-instruct",
        "deepseek-ai/deepseek-r1-distill-llama-70b",
        "mistralai/mistral-large-2411",
        "qwen/qwen2.5-72b-instruct",
    ],
}


@pytest.mark.parametrize("provider_key", sorted(RETIRED_MODEL_IDS))
def test_models_verified_dead_are_not_offered(provider_key):
    listed = set(get_preset(provider_key)["models"])
    still_there = listed & set(RETIRED_MODEL_IDS[provider_key])
    assert not still_there, (
        f"{provider_key} lists model ids that do not resolve upstream: "
        f"{sorted(still_there)}"
    )


@pytest.mark.parametrize("provider_key", ["groq", "openrouter", "gemini", "sambanova", "nvidia"])
def test_each_provider_still_offers_something_and_defaults_to_it(provider_key):
    preset = get_preset(provider_key)
    assert preset["models"], f"{provider_key} was pruned down to nothing"
    # Pruning must never leave a default pointing at a removed id.
    assert preset["default_model"] in preset["models"]
