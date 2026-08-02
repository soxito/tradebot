"""Tests for the tools the models call to reach live data.

Two properties matter most here and both are security- or reliability-critical:
``execute_tool`` must never raise (a tool failure has to reach the model as a
fact, not kill the turn), and ``fetch_url`` must never be talked into reaching
the private network — the model picks that URL, often from a page it just read.
"""

from __future__ import annotations

import pytest

from plugins.AiMarketAnalyst.backend.services import ai_tools


# ── Schemas ──────────────────────────────────────────────────────────────────

def test_every_schema_is_well_formed():
    for schema in ai_tools.TOOL_SCHEMAS:
        assert schema["type"] == "function"
        fn = schema["function"]
        assert fn["name"] and fn["description"]
        params = fn["parameters"]
        assert params["type"] == "object"
        for required in params.get("required", []):
            assert required in params["properties"], (
                f"{fn['name']} requires {required!r} but does not declare it"
            )


def test_price_lookup_advertises_every_asset_class():
    """The description is what makes the model reach for it instead of refusing."""
    desc = next(
        s["function"]["description"]
        for s in ai_tools.TOOL_SCHEMAS
        if s["function"]["name"] == "price_lookup"
    ).lower()
    for asset in ("xauusd", "gbpusd", "us30", "usoil", "btcusdt"):
        assert asset in desc, f"price_lookup does not mention {asset}"
    assert "never ask the user to supply one" in desc


# ── SSRF guard ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/admin",
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",   # cloud metadata
        "http://[::1]/",
        "file:///etc/passwd",
        "ftp://example.com/x",
        "http://0.0.0.0/",
        "not-a-url",
    ],
)
def test_fetch_url_refuses_non_public_targets(url):
    safe, why = ai_tools._url_is_safe(url)
    assert safe is False, f"{url} was allowed through the SSRF guard"
    assert why


def test_fetch_url_allows_ordinary_public_pages():
    safe, why = ai_tools._url_is_safe("https://example.com/article")
    assert safe is True, why


@pytest.mark.asyncio
async def test_fetch_url_blocks_before_making_a_request(monkeypatch):
    """The guard must run before the network call, not after."""
    called = False

    async def _boom(*a, **k):
        nonlocal called
        called = True
        return {"ok": True, "text": "secret"}

    import plugins.AgentPaulPlugin.backend.services.news_research as nr
    monkeypatch.setattr(nr, "research_url", _boom)

    out = await ai_tools.execute_tool("fetch_url", {"url": "http://169.254.169.254/"})
    assert out.startswith("ERROR:")
    assert called is False, "the SSRF guard let a request through"


# ── execute_tool never raises ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_tool_is_reported_not_raised():
    out = await ai_tools.execute_tool("no_such_tool", {})
    assert out.startswith("ERROR:")
    assert "price_lookup" in out, "the error should list what IS available"


@pytest.mark.asyncio
async def test_malformed_arguments_are_reported_not_raised():
    assert (await ai_tools.execute_tool("price_lookup", "{not json")).startswith("ERROR:")
    assert (await ai_tools.execute_tool("price_lookup", "[1,2]")).startswith("ERROR:")


@pytest.mark.asyncio
async def test_a_throwing_handler_becomes_an_error_string(monkeypatch):
    async def _explode(args):
        raise RuntimeError("upstream on fire")

    monkeypatch.setitem(ai_tools._HANDLERS, "price_lookup", _explode)
    out = await ai_tools.execute_tool("price_lookup", {"symbols": ["XAUUSD"]})
    assert out.startswith("ERROR:")
    assert "upstream on fire" in out


@pytest.mark.asyncio
async def test_failed_price_lookup_never_denies_the_capability(monkeypatch):
    """A failed fetch must not teach the model that it has no market access."""
    from app.services import market_data

    async def _none(symbols, **kw):
        return {}

    monkeypatch.setattr(market_data, "get_quotes", _none)
    out = await ai_tools.execute_tool("price_lookup", {"symbols": ["XAUUSD"]})
    assert "not a missing capability" in out.lower()
    assert "retry" in out.lower()


@pytest.mark.asyncio
async def test_price_lookup_formats_source_and_age(monkeypatch):
    from app.services import market_data

    async def _quotes(symbols, **kw):
        return {
            "XAUUSD": market_data.Quote(
                symbol="XAUUSD", price=2400.5, source="yahoo:GC=F",
                ts=0, asset_class="metal", age_s=12,
            )
        }

    monkeypatch.setattr(market_data, "get_quotes", _quotes)
    out = await ai_tools.execute_tool("price_lookup", {"symbols": ["XAUUSD"]})
    assert "XAUUSD" in out and "2,400.5" in out
    assert "yahoo:GC=F" in out and "12s old" in out


@pytest.mark.asyncio
async def test_tool_output_is_truncated(monkeypatch):
    async def _huge(args):
        return ai_tools._truncate("x" * 50_000)

    monkeypatch.setitem(ai_tools._HANDLERS, "web_search", _huge)
    out = await ai_tools.execute_tool("web_search", {"query": "gold"})
    assert len(out) < ai_tools._MAX_RESULT_CHARS + 200


# ── Text directives ──────────────────────────────────────────────────────────

def test_parses_a_directive():
    calls = ai_tools.parse_text_directives(
        'Let me check.\n<<TOOL: price_lookup {"symbols": ["XAUUSD"]}>>'
    )
    assert calls == [{"name": "price_lookup", "arguments": {"symbols": ["XAUUSD"]}}]


def test_parses_several_directives():
    calls = ai_tools.parse_text_directives(
        '<<TOOL: price_lookup {"symbols": ["XAUUSD"]}>>\n'
        '<<TOOL: web_search {"query": "gold outlook"}>>'
    )
    assert [c["name"] for c in calls] == ["price_lookup", "web_search"]


def test_malformed_directive_is_skipped_not_fatal():
    """A half-written directive must not cost the model its valid ones."""
    calls = ai_tools.parse_text_directives(
        '<<TOOL: price_lookup {broken json}>>\n'
        '<<TOOL: web_search {"query": "gold"}>>'
    )
    assert [c["name"] for c in calls] == ["web_search"]


def test_unknown_tool_directive_is_ignored():
    assert ai_tools.parse_text_directives('<<TOOL: rm_rf {"path": "/"}>>') == []


def test_strip_directives_leaves_prose():
    text = 'Checking now.\n<<TOOL: price_lookup {"symbols": ["XAUUSD"]}>>'
    assert ai_tools.strip_directives(text) == "Checking now."


def test_directive_prompt_tells_the_model_it_has_access():
    assert "never tell the user you lack" in ai_tools.TOOL_DIRECTIVE_PROMPT.lower()
