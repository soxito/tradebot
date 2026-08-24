"""Photos sent to the bot must be read, not silently dropped.

Before this, ``parse_and_execute`` bailed on any message without ``text``, so a
chart screenshot produced no reply at all. These cover the routing, the
chart-first/caption-overrides rule, and — most importantly — that the
authorization gate applies to images, since it used to sit *below* the check
that dropped them.
"""

from __future__ import annotations

import pytest

from plugins.AiMarketAnalyst.backend.services.vision import VisionRead
from plugins.TelegramSignalNewsPlugin.backend.services import command_service as cs


class _FakeDB:
    pass


def _photo_update(chat_id: str = "555", caption: str | None = None) -> dict:
    msg: dict = {
        "chat": {"id": int(chat_id)},
        "photo": [
            {"file_id": "small_id", "width": 90, "height": 51},
            {"file_id": "big_id", "width": 900, "height": 520},
        ],
    }
    if caption is not None:
        msg["caption"] = caption
    return {"message": msg}


@pytest.fixture
def stub_pipeline(monkeypatch):
    """Stub the network edges; record what each layer was handed."""
    seen: dict = {}

    async def fake_send_message(token, chat_id, text, *a, **kw):
        seen.setdefault("acks", []).append(text)
        return {"ok": True}

    async def fake_get_file(token, file_id):
        seen["file_id"] = file_id
        return "photos/file_9.jpg"

    async def fake_download_file(token, file_path, *, max_bytes=0):
        seen["file_path"] = file_path
        seen["max_bytes"] = max_bytes
        return b"\xff\xd8\xff\xe0fake-jpeg-bytes"

    async def fake_send_photo(token, chat_id, photo, *a, **kw):
        seen["sent_photo"] = photo
        seen["photo_caption"] = kw.get("caption", "")
        return {"ok": True}

    async def fake_read_chart(image_bytes, mime, question, db):
        seen["mime"] = mime
        seen["question"] = question
        seen["image_bytes"] = image_bytes
        return VisionRead(
            narrative="XAUUSD 1H, uptrend, support 128.40, resistance 133.10.",
            findings={
                "instrument": "XAUUSD",
                "bias": "bullish",
                "levels": [{"label": "support", "price": "128.40", "y_pct": 70,
                            "kind": "support"}],
            },
        )

    def fake_annotate(image_bytes, findings, plan=None):
        seen["annotated_findings"] = findings
        seen["annotated_plan"] = plan
        return b"fake-png-overlay"

    async def no_live_plan(instrument):
        # Default: no live plan, so these cases exercise the prose path. The
        # tests that care about a plan patch this themselves.
        seen["plan_asked_for"] = instrument
        return None

    async def fake_ai_fallback(text, db, *, chat_id="", hint=""):
        seen["agent_prompt"] = text
        return "ENRICHED ANSWER", "HTML", None

    from plugins.AiMarketAnalyst.backend.services import chart_annotate
    from plugins.TelegramSignalNewsPlugin.backend.services import bot_service, vision_service

    monkeypatch.setattr(bot_service, "send_message", fake_send_message)
    monkeypatch.setattr(bot_service, "send_photo", fake_send_photo)
    monkeypatch.setattr(bot_service, "get_file", fake_get_file)
    monkeypatch.setattr(bot_service, "download_file", fake_download_file)
    monkeypatch.setattr(vision_service, "read_chart", fake_read_chart)
    monkeypatch.setattr(chart_annotate, "annotate", fake_annotate)
    monkeypatch.setattr(cs, "_live_plan", no_live_plan)
    monkeypatch.setattr(cs, "_ai_fallback", fake_ai_fallback)
    return seen


# ── Extraction ───────────────────────────────────────────────────────────────

def test_extract_image_picks_the_largest_photo_size():
    """Telegram orders sizes ascending; the thumbnail is useless for a chart."""
    got = cs._extract_image(_photo_update()["message"])
    assert got == ("big_id", "image/jpeg")


def test_extract_image_accepts_an_image_document():
    """A chart sent as a *file* skips recompression — the better way to send one."""
    msg = {"document": {"file_id": "doc_id", "mime_type": "image/png"}}
    assert cs._extract_image(msg) == ("doc_id", "image/png")


def test_extract_image_ignores_non_image_documents():
    msg = {"document": {"file_id": "doc_id", "mime_type": "application/pdf"}}
    assert cs._extract_image(msg) is None


def test_extract_image_ignores_plain_text():
    assert cs._extract_image({"text": "hello"}) is None


# ── Routing ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bare_photo_is_analysed_and_answered(stub_pipeline):
    reply, mode, _ = await cs.parse_and_execute(
        _photo_update(), "tok", [], _FakeDB()
    )

    assert reply == "ENRICHED ANSWER"
    assert mode == "HTML"
    # Highest-resolution size was the one fetched
    assert stub_pipeline["file_id"] == "big_id"
    assert stub_pipeline["file_path"] == "photos/file_9.jpg"
    # An ack went out first — the chain is too slow to leave the user guessing
    assert any("Reading your image" in a for a in stub_pipeline["acks"])


@pytest.mark.asyncio
async def test_uncaptioned_photo_gets_the_chart_prompt(stub_pipeline):
    """Chart-first: no caption means analyse it as a trading chart."""
    from plugins.TelegramSignalNewsPlugin.backend.services import vision_service

    await cs.parse_and_execute(_photo_update(), "tok", [], _FakeDB())
    assert stub_pipeline["question"] == vision_service.DEFAULT_CHART_PROMPT


@pytest.mark.asyncio
async def test_caption_overrides_the_chart_prompt(stub_pipeline):
    await cs.parse_and_execute(
        _photo_update(caption="is this a good short?"), "tok", [], _FakeDB()
    )
    assert stub_pipeline["question"] == "is this a good short?"


@pytest.mark.asyncio
async def test_slash_caption_is_treated_as_a_question_not_a_command(stub_pipeline):
    """"/analyze this" must not fall through to the command dispatcher."""
    await cs.parse_and_execute(
        _photo_update(caption="/analyze this setup"), "tok", [], _FakeDB()
    )
    assert stub_pipeline["question"] == "analyze this setup"


@pytest.mark.asyncio
async def test_vision_read_is_fed_into_the_agent_chain(stub_pipeline):
    """The whole point: the image read reaches the live-context chain."""
    await cs.parse_and_execute(
        _photo_update(caption="where's the entry?"), "tok", [], _FakeDB()
    )
    prompt = stub_pipeline["agent_prompt"]
    assert "XAUUSD 1H, uptrend, support 128.40" in prompt
    assert "where's the entry?" in prompt


@pytest.mark.asyncio
async def test_raw_read_is_returned_when_enrichment_fails(monkeypatch, stub_pipeline):
    """A dead agent chain must not swallow a good image read."""
    async def dead_fallback(text, db, *, chat_id="", hint=""):
        return None, "HTML", None

    monkeypatch.setattr(cs, "_ai_fallback", dead_fallback)
    reply, _, _ = await cs.parse_and_execute(_photo_update(), "tok", [], _FakeDB())
    assert reply and "XAUUSD 1H" in reply


@pytest.mark.asyncio
async def test_unreadable_image_reports_instead_of_going_silent(monkeypatch, stub_pipeline):
    from plugins.TelegramSignalNewsPlugin.backend.services import vision_service

    async def no_read(image_bytes, mime, question, db):
        return None

    monkeypatch.setattr(vision_service, "read_chart", no_read)
    reply, _, _ = await cs.parse_and_execute(_photo_update(), "tok", [], _FakeDB())
    assert reply and "couldn't read that image" in reply.lower()


# ── Annotated reply ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_annotated_chart_is_sent_back_as_a_photo(stub_pipeline):
    """The structured findings must be drawn onto the user's own screenshot."""
    await cs.parse_and_execute(_photo_update(), "tok", [], _FakeDB())

    assert stub_pipeline["sent_photo"] == b"fake-png-overlay"
    # Drawn from the read, and captioned with what the model identified
    assert stub_pipeline["annotated_findings"]["levels"][0]["price"] == "128.40"
    assert "XAUUSD" in stub_pipeline["photo_caption"]


@pytest.mark.asyncio
async def test_the_live_plan_is_drawn_on_the_chart_and_explained(monkeypatch, stub_pipeline):
    """A screenshot should come back with the actual entry/stop/targets on it."""
    plan = {
        "side": "long", "proposed_entry": 4321.5, "sl": 4273.1,
        "tp1": 4392.3, "tp2": 4447.0, "rr1": 1.5, "rr2": 2.6,
        "current_price": 4376.2, "trend": "uptrend", "rsi": 54, "confidence": 0.67,
        "price_source": "swissquote-spot", "bias_reasons": "price and EMA50 above EMA200",
        "confirm_command": "execute XAUUSD long 5 lot at 4321.5",
    }

    async def live_plan(instrument):
        return plan

    monkeypatch.setattr(cs, "_live_plan", live_plan)
    reply, mode, _ = await cs.parse_and_execute(_photo_update(), "tok", [], _FakeDB())

    # Drawn on the image…
    assert stub_pipeline["annotated_plan"]["tp1"] == 4392.3
    # …and explained in the reply, levels and all.
    assert "4321.5" in reply and "4273.1" in reply and "4392.3" in reply
    assert "BUY" in reply and "The setup" in reply
    assert mode == "HTML"


@pytest.mark.asyncio
async def test_a_plan_reply_says_when_the_chart_read_disagrees(monkeypatch, stub_pipeline):
    """A bearish-looking chart under a long plan must be flagged, not glossed over."""
    from plugins.TelegramSignalNewsPlugin.backend.services import vision_service

    async def bearish_read(image_bytes, mime, question, db):
        return VisionRead(narrative="Bearish structure.",
                          findings={"instrument": "XAUUSD", "bias": "bearish"})

    async def live_plan(instrument):
        return {"side": "long", "proposed_entry": 100.0, "sl": 95.0, "tp1": 110.0,
                "tp2": 120.0, "current_price": 99.0, "confidence": 0.6}

    monkeypatch.setattr(vision_service, "read_chart", bearish_read)
    monkeypatch.setattr(cs, "_live_plan", live_plan)
    reply, _, _ = await cs.parse_and_execute(_photo_update(), "tok", [], _FakeDB())
    assert "differs from the live read" in reply


@pytest.mark.asyncio
async def test_no_photo_is_sent_when_there_is_nothing_to_draw(monkeypatch, stub_pipeline):
    """A non-chart image still gets a prose answer, just no empty overlay."""
    from plugins.TelegramSignalNewsPlugin.backend.services import vision_service

    async def prose_only(image_bytes, mime, question, db):
        return VisionRead(narrative="A photo of a cat. Not a chart.", findings={})

    monkeypatch.setattr(vision_service, "read_chart", prose_only)
    reply, _, _ = await cs.parse_and_execute(_photo_update(), "tok", [], _FakeDB())

    assert reply == "ENRICHED ANSWER"
    assert "sent_photo" not in stub_pipeline


# ── Authorization ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unauthorized_chat_cannot_spend_a_vision_call(stub_pipeline):
    """The gate used to sit below the check that dropped images entirely.

    Moving it up is what stops an unlisted chat from burning vision quota — so
    assert the download never even started, not merely that the reply was empty.
    """
    reply, _, _ = await cs.parse_and_execute(
        _photo_update(chat_id="999"), "tok", ["555"], _FakeDB()
    )
    assert reply is None
    assert "file_id" not in stub_pipeline
    assert "acks" not in stub_pipeline


@pytest.mark.asyncio
async def test_authorized_chat_still_passes_the_gate(stub_pipeline):
    reply, _, _ = await cs.parse_and_execute(
        _photo_update(chat_id="555"), "tok", ["555"], _FakeDB()
    )
    assert reply == "ENRICHED ANSWER"


@pytest.mark.asyncio
async def test_message_with_neither_text_nor_image_is_still_dropped(stub_pipeline):
    reply, _, _ = await cs.parse_and_execute(
        {"message": {"chat": {"id": 555}}}, "tok", [], _FakeDB()
    )
    assert reply is None
