"""Telegram must reach the web chat's standard of live context.

The bug this covers: Telegram's free-text path sent a bare persona with no
prices, no news and no tools, so the same question that answered fine in the
browser produced "the current platform does not provide a live gold-price feed"
here. And a failed symbol lookup rendered a dead-end ❌ instead of an answer.
"""

from __future__ import annotations

import pytest

from plugins.TelegramSignalNewsPlugin.backend.services import command_service as cs


class _FakeDB:
    pass


# ── System prompt ────────────────────────────────────────────────────────────

def test_prompt_asserts_live_data_for_every_asset_class():
    prompt = cs._jarvis_system_prompt()
    for asset in ("XAUUSD", "XAGUSD", "US30", "NAS100", "USOIL"):
        assert asset in prompt, f"{asset} is not advertised to the model"
    assert "You HAVE live market data for EVERY asset class" in prompt


def test_prompt_bans_the_refusals_users_actually_saw():
    lowered = cs._jarvis_system_prompt().lower()
    assert "does not provide a live gold-price feed" in lowered
    assert "never ask the user to supply a price" in lowered


def test_prompt_carries_each_optional_block():
    prompt = cs._jarvis_system_prompt(
        "POSITIONS HERE",
        price_block="PRICES HERE",
        news_block="NEWS HERE",
        learned="LEARNED HERE",
    )
    for marker in ("POSITIONS HERE", "PRICES HERE", "NEWS HERE", "LEARNED HERE"):
        assert marker in prompt


def test_prompt_works_with_no_blocks_at_all():
    """A failed context fetch must thin the prompt, not break it."""
    assert cs._jarvis_system_prompt() and cs._jarvis_system_prompt(None)


# ── Live context injection ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fallback_injects_price_for_a_mentioned_fx_symbol(monkeypatch):
    captured = {}

    async def _fake_execute(req):
        class _R:
            ok, action, detail, speech = False, "unknown", "", ""
        return _R()

    async def _fake_chat(db, messages, **kw):
        captured["system"] = messages[0]["content"]
        return {"ok": True, "content": "GBPUSD is at 1.2700, Sir."}

    async def _fake_price_block(symbols, **kw):
        return f"## LIVE Market Prices\n  GBPUSD: 1.2700 [yahoo:GBPUSD=X]\n(for {symbols})"

    import app.api.jarvis as jarvis
    from app.services import market_data
    from plugins.AiMarketAnalyst.backend.services import ai_router

    monkeypatch.setattr(jarvis, "execute_command", _fake_execute)
    monkeypatch.setattr(ai_router, "chat_with_tools", _fake_chat)
    monkeypatch.setattr(market_data, "price_block", _fake_price_block)
    monkeypatch.setattr(cs, "_news_context", lambda t, s: _none())
    monkeypatch.setattr(cs, "_learned_context", lambda d, s: _none())

    reply, _mode, _kb = await cs._ai_fallback("whats GBPUSD doing", _FakeDB())

    assert reply and "1.2700" in reply
    assert "GBPUSD: 1.2700" in captured["system"]
    assert "yahoo:GBPUSD=X" in captured["system"]


async def _none():
    return None


@pytest.mark.asyncio
async def test_context_failure_still_produces_a_reply(monkeypatch):
    """A dead news feed must not cost the user their answer."""
    async def _fake_execute(req):
        class _R:
            ok, action, detail, speech = False, "unknown", "", ""
        return _R()

    async def _boom(*a, **k):
        raise RuntimeError("network down")

    async def _fake_chat(db, messages, **kw):
        return {"ok": True, "content": "Here is what I can tell you, Sir."}

    import app.api.jarvis as jarvis
    from app.services import market_data
    from plugins.AiMarketAnalyst.backend.services import ai_router

    monkeypatch.setattr(jarvis, "execute_command", _fake_execute)
    monkeypatch.setattr(ai_router, "chat_with_tools", _fake_chat)
    monkeypatch.setattr(market_data, "price_block", _boom)

    reply, _mode, _kb = await cs._ai_fallback("whats gold doing", _FakeDB())
    assert reply and "Here is what I can tell you" in reply


# ── The ❌ dead end ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_failed_analyze_does_not_render_the_bitget_error(monkeypatch):
    """This is the exact message the user reported."""
    async def _fake_execute(req):
        class _R:
            ok = False
            action = "analyze"
            detail = "I couldn't find a Bitget-tradeable pair for GBPUSD, Sir."
            speech = detail
        return _R()

    async def _fake_fallback(text, db, *, chat_id="", hint=""):
        assert "Bitget" in hint, "the upstream failure should reach the model as a hint"
        return "GBPUSD is trading at 1.2700, Sir.", "HTML", None

    import app.api.jarvis as jarvis
    monkeypatch.setattr(jarvis, "execute_command", _fake_execute)
    monkeypatch.setattr(cs, "_ai_fallback", _fake_fallback)

    reply, _mode = await cs._jarvis_command("analyze GBPUSD", _FakeDB())

    assert "❌" not in reply
    assert "Bitget-tradeable" not in reply
    assert "1.2700" in reply


@pytest.mark.asyncio
async def test_a_failed_order_still_surfaces_its_error(monkeypatch):
    """Only analysis degrades to chat — silently rerouting a trade would be worse."""
    async def _fake_execute(req):
        class _R:
            ok, action = False, "execute"
            detail = speech = "Insufficient margin"
        return _R()

    import app.api.jarvis as jarvis
    monkeypatch.setattr(jarvis, "execute_command", _fake_execute)

    reply, _mode = await cs._jarvis_command("execute BTC long 5", _FakeDB())
    assert "❌" in reply and "Insufficient margin" in reply


@pytest.mark.asyncio
async def test_analysis_without_a_session_still_reports_cleanly(monkeypatch):
    """No db (older call sites) must not crash the handoff."""
    async def _fake_execute(req):
        class _R:
            ok, action = False, "analyze"
            detail = speech = "nope"
        return _R()

    import app.api.jarvis as jarvis
    monkeypatch.setattr(jarvis, "execute_command", _fake_execute)

    reply, _mode = await cs._jarvis_command("analyze WAT")
    assert "❌" in reply
