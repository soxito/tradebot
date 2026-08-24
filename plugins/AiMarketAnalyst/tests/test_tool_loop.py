"""Tests for the tool-calling loop and the plumbing it depends on.

Three of these guard failures that would be invisible in production rather than
loud: OpenManus silently swallowing every tool call, Headroom corrupting the
tool_call_id pairing, and a tool-rejecting provider getting knocked offline by
its own 400.
"""

from __future__ import annotations

import inspect

import pytest

from plugins.AiMarketAnalyst.backend.services import ai_router


# ── Back-compat ──────────────────────────────────────────────────────────────

def test_call_openai_compatible_still_returns_three_values():
    """jarvis.py, analysis_router.py and four MT5 test monkeypatches unpack 3.

    The tool-aware variant is a separate function precisely so this stays true;
    widening this one would break callers that never asked for tools.
    """
    source = inspect.getsource(ai_router._call_openai_compatible)
    assert "-> tuple[str, dict[str, int], str | None]:" in source
    assert "return content, usage, routed_via" in source


def test_the_tool_aware_sibling_exists_and_returns_the_message():
    source = inspect.getsource(ai_router._call_openai_compatible_msg)
    assert "return content, usage, routed_via, message" in source


def test_db_chat_tool_kwargs_are_optional():
    """Every existing caller must keep working untouched."""
    params = inspect.signature(ai_router.db_chat).parameters
    for name in ("tools", "tool_choice", "bypass_openmanus"):
        assert name in params, f"db_chat lost the {name} kwarg"
        assert params[name].default in (None, False), f"{name} is not optional"


# ── OpenManus bypass ─────────────────────────────────────────────────────────

def test_openmanus_is_skipped_for_tool_calls():
    """OpenManus returns early and has no tool support.

    Without this guard a tool call would look implemented, return a plausible
    answer, and never actually fetch anything — with nothing logged as an error.
    """
    source = inspect.getsource(ai_router.db_chat)
    assert "not bypass_openmanus and not tools" in source


def test_chat_with_tools_always_bypasses_openmanus():
    source = inspect.getsource(ai_router.chat_with_tools)
    assert source.count("bypass_openmanus=True") >= 3, (
        "every db_chat call inside the loop must bypass OpenManus"
    )


# ── Headroom compression ─────────────────────────────────────────────────────

def test_tool_messages_are_recognised_as_plumbing():
    assert ai_router._is_tool_plumbing({"role": "tool", "content": "x"}) is True
    assert ai_router._is_tool_plumbing(
        {"role": "assistant", "tool_calls": [{"id": "c1"}]}
    ) is True
    assert ai_router._is_tool_plumbing({"role": "user", "content": "hi"}) is False


def test_headroom_compression_skips_tool_messages():
    """A rewritten tool result breaks tool_call_id pairing → an opaque 400."""
    source = inspect.getsource(ai_router.db_chat)
    assert "_is_tool_plumbing" in source
    assert "compressible" in source


def test_compression_splice_preserves_message_order():
    """Reproduces the splice with a compressor that rewrites content."""
    messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "what is gold"},
        {"role": "assistant", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "XAUUSD = 2400"},
        {"role": "user", "content": "and silver"},
    ]

    plumbing = {i for i, m in enumerate(messages) if ai_router._is_tool_plumbing(m)}
    compressible = [m for i, m in enumerate(messages) if i not in plumbing]
    compressed = [{**m, "content": "C:" + str(m.get("content", ""))} for m in compressible]

    it = iter(compressed)
    out = [messages[i] if i in plumbing else next(it) for i in range(len(messages))]

    assert [m["role"] for m in out] == [m["role"] for m in messages]
    assert out[2] is messages[2], "the tool_calls turn was rewritten"
    assert out[3] is messages[3], "the tool result was rewritten"
    assert out[3]["tool_call_id"] == "c1"
    assert out[1]["content"].startswith("C:"), "ordinary turns should compress"


# ── Tool-support detection ───────────────────────────────────────────────────

def test_providers_are_assumed_tool_capable_until_proven_otherwise():
    ai_router._tool_support.clear()
    assert ai_router._supports_tools(123) is True
    ai_router._tool_support[123] = False
    assert ai_router._supports_tools(123) is False
    ai_router._tool_support.clear()


def test_tool_rejection_is_handled_without_tripping_the_circuit():
    """Dropping tools happens before raise_for_status, so no breaker trip.

    Otherwise the first tool-bearing call would take every non-supporting
    provider offline for the full 120s cooldown.
    """
    source = inspect.getsource(ai_router._call_openai_compatible_msg)
    drop_at = source.index('current_payload.pop("tools", None)')
    raise_at = source.index("resp.raise_for_status()")
    assert drop_at < raise_at, "tools are dropped after raise_for_status"
    assert "_tool_support[provider_id] = False" in source


# ── The loop ─────────────────────────────────────────────────────────────────

class _FakeDB:
    pass


@pytest.mark.asyncio
async def test_loop_executes_tools_and_returns_the_final_answer(monkeypatch):
    turns = []

    async def _fake_db_chat(db, messages, **kw):
        turns.append(list(messages))
        if len(turns) == 1:
            return {
                "ok": True, "content": "", "tools_supported": True,
                "message": {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": "c1",
                        "function": {"name": "price_lookup",
                                     "arguments": '{"symbols": ["XAUUSD"]}'},
                    }],
                },
                "tool_calls": [{
                    "id": "c1",
                    "function": {"name": "price_lookup",
                                 "arguments": '{"symbols": ["XAUUSD"]}'},
                }],
            }
        return {"ok": True, "content": "Gold is 2400.", "tools_supported": True,
                "message": {"role": "assistant", "content": "Gold is 2400."},
                "tool_calls": []}

    async def _fake_exec(name, args):
        return "XAUUSD = 2400 [yahoo:GC=F]"

    monkeypatch.setattr(ai_router, "db_chat", _fake_db_chat)
    from plugins.AiMarketAnalyst.backend.services import ai_tools
    monkeypatch.setattr(ai_tools, "execute_tool", _fake_exec)

    out = await ai_router.chat_with_tools(_FakeDB(), [{"role": "user", "content": "gold?"}])

    assert out["ok"] and out["content"] == "Gold is 2400."
    assert out["tools_used"] == ["price_lookup"]
    second = turns[1]
    assert second[-1]["role"] == "tool"
    assert second[-1]["tool_call_id"] == "c1"


@pytest.mark.asyncio
async def test_loop_stops_at_max_iterations(monkeypatch):
    """A model that keeps asking for tools must not loop forever."""
    calls = {"n": 0}

    async def _always_tools(db, messages, **kw):
        calls["n"] += 1
        return {
            "ok": True, "content": "", "tools_supported": True,
            "message": {"role": "assistant", "tool_calls": [
                {"id": f"c{calls['n']}",
                 "function": {"name": "price_lookup", "arguments": "{}"}}]},
            "tool_calls": [
                {"id": f"c{calls['n']}",
                 "function": {"name": "price_lookup", "arguments": "{}"}}],
        }

    monkeypatch.setattr(ai_router, "db_chat", _always_tools)
    from plugins.AiMarketAnalyst.backend.services import ai_tools
    monkeypatch.setattr(ai_tools, "execute_tool", lambda n, a: _ok())

    out = await ai_router.chat_with_tools(
        _FakeDB(), [{"role": "user", "content": "x"}], max_iterations=2
    )
    assert out["ok"]
    assert calls["n"] <= 4, f"loop ran {calls['n']} times with max_iterations=2"


async def _ok():
    return "ok"


@pytest.mark.asyncio
async def test_unsupported_provider_falls_back_to_directives(monkeypatch):
    """Providers with no tool support still get to fetch, via text directives."""
    turns = []

    async def _fake_db_chat(db, messages, **kw):
        turns.append(list(messages))
        if len(turns) == 1:
            return {
                "ok": True, "tools_supported": False,
                "content": '<<TOOL: price_lookup {"symbols": ["XAUUSD"]}>>',
                "tool_calls": [],
            }
        return {"ok": True, "tools_supported": False,
                "content": "Gold is 2400, Sir.", "tool_calls": []}

    async def _fake_exec(name, args):
        return "XAUUSD = 2400"

    monkeypatch.setattr(ai_router, "db_chat", _fake_db_chat)
    from plugins.AiMarketAnalyst.backend.services import ai_tools
    monkeypatch.setattr(ai_tools, "execute_tool", _fake_exec)

    out = await ai_router.chat_with_tools(_FakeDB(), [{"role": "user", "content": "gold?"}])

    assert out["content"] == "Gold is 2400, Sir."
    assert out["tools_used"] == ["price_lookup"]
    assert len(turns) == 2, "the directive path must cost exactly one extra call"
    assert "XAUUSD = 2400" in turns[1][-1]["content"]


@pytest.mark.asyncio
async def test_no_tool_calls_returns_immediately(monkeypatch):
    calls = {"n": 0}

    async def _plain(db, messages, **kw):
        calls["n"] += 1
        return {"ok": True, "content": "Hello, Sir.", "tools_supported": True,
                "tool_calls": []}

    monkeypatch.setattr(ai_router, "db_chat", _plain)
    out = await ai_router.chat_with_tools(_FakeDB(), [{"role": "user", "content": "hi"}])
    assert out["content"] == "Hello, Sir."
    assert calls["n"] == 1, "an ordinary reply should cost exactly one call"


@pytest.mark.asyncio
async def test_provider_error_is_returned_not_retried_forever(monkeypatch):
    async def _fail(db, messages, **kw):
        return {"ok": False, "error": "no providers"}

    monkeypatch.setattr(ai_router, "db_chat", _fail)
    out = await ai_router.chat_with_tools(_FakeDB(), [{"role": "user", "content": "hi"}])
    assert out["ok"] is False and out["error"] == "no providers"


# ── Inline (text-emitted) tool calls: Nemotron / Qwen / Hermes ───────────────

def test_parse_inline_tool_calls_reads_the_chatml_keyvalue_shape():
    """The exact markup a user saw leak instead of an analysis must parse."""
    from plugins.AiMarketAnalyst.backend.services import ai_tools

    raw = (
        "<tool_call>analyze_symbol"
        "<arg_key>symbol</arg_key><arg_value>GBPUSD</arg_value>"
        "<arg_key>timeframe</arg_key><arg_value>1h</arg_value></tool_call>"
    )
    assert ai_tools.parse_inline_tool_calls(raw) == [
        {"name": "analyze_symbol", "arguments": {"symbol": "GBPUSD", "timeframe": "1h"}}
    ]


def test_parse_inline_tool_calls_reads_the_json_shape_and_coerces_types():
    from plugins.AiMarketAnalyst.backend.services import ai_tools

    js = '<tool_call>{"name": "analyze_symbol", "arguments": {"symbol": "GBPUSD"}}</tool_call>'
    assert ai_tools.parse_inline_tool_calls(js) == [
        {"name": "analyze_symbol", "arguments": {"symbol": "GBPUSD"}}
    ]
    # A truncated call (no closing tag) with an array value keeps the list type.
    trunc = '<tool_call>price_lookup<arg_key>symbols</arg_key><arg_value>["XAUUSD", "GBPUSD"]</arg_value>'
    assert ai_tools.parse_inline_tool_calls(trunc) == [
        {"name": "price_lookup", "arguments": {"symbols": ["XAUUSD", "GBPUSD"]}}
    ]


def test_unknown_inline_tool_is_ignored():
    from plugins.AiMarketAnalyst.backend.services import ai_tools

    assert ai_tools.parse_inline_tool_calls(
        "<tool_call>drop_table<arg_key>x</arg_key><arg_value>1</arg_value></tool_call>"
    ) == []


def test_strip_inline_tool_calls_cleans_leaked_markup():
    from plugins.AiMarketAnalyst.backend.services import ai_tools

    leaked = "Here is my read, Sir. <tool_call>analyze_symbol<arg_key>symbol</arg_key><arg_value>GBPUSD</arg_value></tool_call>"
    assert ai_tools.strip_inline_tool_calls(leaked) == "Here is my read, Sir."
    assert ai_tools.strip_inline_tool_calls("Plain answer.") == "Plain answer."


@pytest.mark.asyncio
async def test_loop_runs_a_text_emitted_tool_call_instead_of_leaking_it(monkeypatch):
    """A provider that advertises tools but emits the call as text content.

    This reproduces the reported bug: NVIDIA/Nemotron returns
    ``<tool_call>analyze_symbol…</tool_call>`` in ``content`` with an empty
    native ``tool_calls`` list. The loop must execute it and answer, never
    hand the raw markup back.
    """
    turns = []
    raw_call = (
        "<tool_call>analyze_symbol"
        "<arg_key>symbol</arg_key><arg_value>GBPUSD</arg_value>"
        "<arg_key>timeframe</arg_key><arg_value>1h</arg_value></tool_call>"
    )

    async def _fake_db_chat(db, messages, **kw):
        turns.append(list(messages))
        if len(turns) == 1:
            return {"ok": True, "content": raw_call, "tools_supported": True,
                    "message": {"role": "assistant", "content": raw_call},
                    "tool_calls": []}
        return {"ok": True, "content": "GBPUSD is grinding up, Sir.",
                "tools_supported": True,
                "message": {"role": "assistant", "content": "GBPUSD is grinding up, Sir."},
                "tool_calls": []}

    captured = {}

    async def _fake_exec(name, args):
        captured["call"] = (name, args)
        return "GBPUSD 4h: bullish, RSI 58"

    monkeypatch.setattr(ai_router, "db_chat", _fake_db_chat)
    from plugins.AiMarketAnalyst.backend.services import ai_tools
    monkeypatch.setattr(ai_tools, "execute_tool", _fake_exec)

    out = await ai_router.chat_with_tools(
        _FakeDB(), [{"role": "user", "content": "take on GBPUSD?"}]
    )

    assert captured["call"] == ("analyze_symbol", {"symbol": "GBPUSD", "timeframe": "1h"})
    assert out["ok"] and out["content"] == "GBPUSD is grinding up, Sir."
    assert "<tool_call>" not in out["content"]
    assert out["tools_used"] == ["analyze_symbol"]


@pytest.mark.asyncio
async def test_unparsed_inline_markup_is_stripped_before_returning(monkeypatch):
    """An unknown/malformed inline call the loop cannot run must not leak."""
    async def _fake_db_chat(db, messages, **kw):
        return {"ok": True,
                "content": "Done, Sir. <tool_call>mystery_tool<arg_key>x</arg_key><arg_value>1</arg_value></tool_call>",
                "tools_supported": True,
                "message": {"role": "assistant", "content": "..."},
                "tool_calls": []}

    monkeypatch.setattr(ai_router, "db_chat", _fake_db_chat)
    out = await ai_router.chat_with_tools(_FakeDB(), [{"role": "user", "content": "hi"}])
    assert out["content"] == "Done, Sir."
    assert "<tool_call>" not in out["content"]
