"""The OBLIVION image and card the Telegram bot sends must be built from one
engine result, render without error, and never reach Telegram from here."""
from __future__ import annotations

import asyncio
import io
import random

import pytest

from app.signals.oblivion import analyze_oblivion
from plugins.TelegramSignalNewsPlugin.backend.services import bot_service, room_bridge

PIL = pytest.importorskip("PIL")


def _rows(n=400, step=3_600_000, seed=5):
    rng = random.Random(seed)
    rows, p = [], 2000.0
    for i in range(n):
        o = p
        p += (0.8 if (i // 40) % 2 == 0 else -0.8) + rng.uniform(-3, 3)
        c = p
        rows.append([1_700_000_000_000 + i * step, o, max(o, c) + rng.uniform(0, 2), min(o, c) - rng.uniform(0, 2), c,
                     1000 + rng.uniform(-200, 800)])
    return rows


def test_render_produces_a_png_of_the_designed_size():
    from plugins.AiMarketAnalyst.backend.services.oblivion_chart import H, W, render_oblivion_chart

    rows = _rows()
    obl = analyze_oblivion(rows, "1h")
    png = render_oblivion_chart(rows, obl, symbol="TEST/USDT", timeframe="1h")
    assert png and png[:8] == b"\x89PNG\r\n\x1a\n"
    img = PIL.Image.open(io.BytesIO(png))
    assert img.size == (W, H)


def test_render_computes_the_engine_itself_when_not_given_one():
    from plugins.AiMarketAnalyst.backend.services.oblivion_chart import render_oblivion_chart

    assert render_oblivion_chart(_rows(), None, symbol="TEST/USDT", timeframe="1h")


def test_render_refuses_a_short_series():
    from plugins.AiMarketAnalyst.backend.services.oblivion_chart import render_oblivion_chart

    assert render_oblivion_chart(_rows(30), None, symbol="TEST/USDT", timeframe="1h") is None


def test_caption_and_card_quote_the_same_result():
    rows = _rows()
    obl = analyze_oblivion(rows, "1h")
    cap = room_bridge.oblivion_caption("TEST/USDT", "1h", obl)
    card = room_bridge.oblivion_card("TEST/USDT", "1h", obl)
    sc = obl["score"]
    assert f"buy {sc['buy']} / sell {sc['sell']}" in cap
    assert f"<code>{sc['buy']}</code> / sell <code>{sc['sell']}</code>" in card
    assert f"scalp ≥{sc['scalp_min']}" in cap and f"sniper ≥{sc['sniper_min']}" in cap
    assert obl["bias_label"] in cap and obl["bias_label"] in card
    plan = obl["signal"] if obl["signal"].get("entry") is not None else obl["last_signal"]
    if plan:
        assert room_bridge._fmt_level(plan["entry"]) in card
        assert room_bridge._fmt_level(plan["stop_loss"]) in card


def test_send_oblivion_analysis_sends_photo_then_card(monkeypatch):
    sent = []

    async def fake_msg(token, chat_id, text, mode="HTML", markup=None, **kw):
        sent.append(("msg", text, markup))

    async def fake_photo(token, chat_id, data, caption="", **kw):
        sent.append(("photo", caption, data))

    async def fake_candles(symbol, timeframe, limit=220):
        return _rows()

    monkeypatch.setattr(bot_service, "send_message", fake_msg)
    monkeypatch.setattr(bot_service, "send_photo", fake_photo)
    monkeypatch.setattr(room_bridge, "_fetch_candles", fake_candles)

    obl = asyncio.run(room_bridge.send_oblivion_analysis("t", "c", "TEST/USDT", "1h", deep=False))
    assert obl and not obl.get("error")
    kinds = [s[0] for s in sent]
    assert kinds == ["photo", "msg"]
    assert sent[0][2][:4] == b"\x89PNG"
    assert "OBLIVION MASTER v5" in sent[0][1]
    assert "OBLIVION MASTER v5" in sent[1][1]


def test_send_is_silent_without_candles(monkeypatch):
    sent = []

    async def fake_msg(*a, **k):
        sent.append("msg")

    async def fake_photo(*a, **k):
        sent.append("photo")

    async def no_candles(symbol, timeframe, limit=220):
        return []

    monkeypatch.setattr(bot_service, "send_message", fake_msg)
    monkeypatch.setattr(bot_service, "send_photo", fake_photo)
    monkeypatch.setattr(room_bridge, "_fetch_candles", no_candles)
    assert asyncio.run(room_bridge.send_oblivion_analysis("t", "c", "NOPE", "1h", deep=False)) is None
    assert sent == []


def test_narrative_gate_rejects_loops_and_unquoted_numbers():
    obl = {"score": {"bull": 3, "bear": 1}, "signal": {"action": "wait"},
           "last_signal": {"entry": 81312.77, "stop_loss": 80589.3}}
    loop = "bull 3 bear 1 " + " ".join(f"10-bar high/low at {80589 + i}.3" for i in range(60))
    assert room_bridge.narrative_is_faithful(loop, obl) is False
    wrong = ("The engine reads bull 5 / bear 1 with the last long at 81,312.77 and the stop at 80,589.3, " * 3)
    assert room_bridge.narrative_is_faithful(wrong, obl) is False  # wrong score
    good = ("OBLIVION reads bull 3 / bear 1 on the hour, below the minimum, so it waits. The last BUY fired at 81,312.77 "
            "with the stop at 80,589.3 under the swing low; price has since rallied into premium above the fib 0.382, "
            "structure stays bullish after the BOS, and there is no fresh FVG or sweep to lean on for a new entry yet.")
    assert room_bridge.narrative_is_faithful(good, obl) is True
