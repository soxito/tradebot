"""A prediction question must run the forecaster, not describe forecasting.

The bug this covers: "based mathematics how can we predict bitcoin" returned an
essay on ARIMA, GARCH and LSTMs, ending with "I can run a live forecast for
BTCUSDT right now — would you like me to execute that?".  Nothing ran, and the
"yes" that followed went back through free-text chat and produced another
paragraph.  Both halves are fixed here: the question runs Kronos, and a bare
confirmation executes the command that was offered.
"""

from __future__ import annotations

import pytest

from plugins.TelegramSignalNewsPlugin.backend.services import command_service as cs


class _FakeDB:
    pass


@pytest.fixture(autouse=True)
def _clean_state():
    cs._PENDING_OFFER.clear()
    cs._CHAT_HISTORY.clear()
    yield
    cs._PENDING_OFFER.clear()
    cs._CHAT_HISTORY.clear()


# ── Intent detection ─────────────────────────────────────────────────────────

def test_the_reported_question_is_a_forecast_request():
    assert cs._forecast_request(
        "Jarvis based mathematics how can we predict bitcoin"
    ) == ("BTCUSDT", "1h")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("forecast gold for me", ("XAUUSD", "1h")),
        ("what's next for ETHUSDT on 4h", ("ETHUSDT", "4h")),
        ("where is GBPUSD going this week", ("GBPUSD", "1h")),
        ("give me a price target for XAUUSD 15m", ("XAUUSD", "15m")),
    ],
)
def test_prediction_phrasings_resolve_symbol_and_timeframe(text, expected):
    assert cs._forecast_request(text) == expected


@pytest.mark.parametrize("text", [
    "can you predict the future?",          # no instrument named
    "what is my portfolio pnl",             # not a prediction
    "close my BTC position",                # an order, never a forecast
    "who won the 1998 world cup",
    "what's the outlook on my BTCUSDT position",   # about the book, not a forecast
    "should I hold my XAUUSD trade — what's the price target",
])
def test_non_forecast_messages_are_left_alone(text):
    assert cs._forecast_request(text) is None


def test_a_pure_prediction_asks_for_no_method_note():
    assert not cs._METHOD_RE.search("forecast bitcoin")
    assert cs._METHOD_RE.search("based mathematics how can we predict bitcoin")


# ── The forecaster actually runs ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_prediction_question_runs_kronos_instead_of_chatting(monkeypatch):
    ran = {}

    async def _fake_forecast(args, db):
        ran["args"] = args
        return "🔮 Kronos Forecast — BTC/USDT (1h)\nUP +1.2%", "HTML", {"inline_keyboard": []}

    async def _no_chat(*a, **k):  # pragma: no cover — must never be reached
        raise AssertionError("free-text chat ran instead of the forecaster")

    monkeypatch.setattr(cs, "_handle_forecast", _fake_forecast)
    monkeypatch.setattr(cs, "_method_note", lambda t, d: _none())
    from plugins.AiMarketAnalyst.backend.services import ai_router
    monkeypatch.setattr(ai_router, "chat_with_tools", _no_chat)

    reply, _mode, markup = await cs._ai_fallback(
        "how can we predict bitcoin", _FakeDB(), chat_id="42"
    )

    assert ran["args"] == "BTCUSDT 1h"
    assert reply and "Kronos Forecast" in reply
    assert markup is not None


async def _none():
    return None


@pytest.mark.asyncio
async def test_a_how_question_gets_the_method_above_the_live_numbers(monkeypatch):
    async def _fake_forecast(args, db):
        return "🔮 Kronos Forecast — BTC/USDT (1h)", "HTML", None

    async def _fake_note(text, db):
        return "- Kronos is an LSTM over log-returns."

    monkeypatch.setattr(cs, "_handle_forecast", _fake_forecast)
    monkeypatch.setattr(cs, "_method_note", _fake_note)

    reply, _mode, _kb = await cs._ai_fallback(
        "based mathematics how can we predict bitcoin", _FakeDB(), chat_id="42"
    )

    assert reply.index("log-returns") < reply.index("Kronos Forecast"), (
        "the written method should introduce the live forecast, not replace it"
    )


@pytest.mark.asyncio
async def test_a_failed_forecast_falls_through_to_chat(monkeypatch):
    """A dead engine must not cost the user their answer."""
    async def _boom(args, db):
        raise RuntimeError("kronos offline")

    async def _fake_execute(req):
        class _R:
            ok, action, detail, speech = False, "unknown", "", ""
        return _R()

    async def _fake_chat(db, messages, **kw):
        return {"ok": True, "content": "Here is the read on Bitcoin, Sir."}

    import app.api.jarvis as jarvis
    from plugins.AiMarketAnalyst.backend.services import ai_router

    monkeypatch.setattr(cs, "_handle_forecast", _boom)
    monkeypatch.setattr(jarvis, "execute_command", _fake_execute)
    monkeypatch.setattr(ai_router, "chat_with_tools", _fake_chat)
    monkeypatch.setattr(cs, "_news_context", lambda t, s: _none())
    monkeypatch.setattr(cs, "_learned_context", lambda d, s: _none())

    reply, _mode, _kb = await cs._ai_fallback("predict bitcoin", _FakeDB())
    assert reply and "read on Bitcoin" in reply


# ── "Yes" honours the offered command ────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "yes", "Yes please", "yep", "sure", "do it", "run it", "go ahead",
    "execute that", "ok", "proceed", "Yes, Sir.",
])
def test_bare_confirmations_are_affirmative(text):
    assert cs._is_affirmative(text)


@pytest.mark.parametrize("text", [
    "no", "not now", "yes but what about gold?",
    "yes and also show me my positions and the news for ETH",
    "why?",
])
def test_anything_carrying_a_new_question_is_not(text):
    assert not cs._is_affirmative(text)


def test_only_read_only_commands_are_parked_for_a_yes():
    cs._remember_offer("42", "I can run /forecast BTCUSDT for you.")
    assert cs._PENDING_OFFER["42"] == ("forecast", "BTCUSDT")

    # An order must never be executable by a one-word confirmation.
    cs._remember_offer("42", "Send /order live long BTCUSDT 100 to take it.")
    assert "42" not in cs._PENDING_OFFER


@pytest.mark.asyncio
async def test_yes_executes_the_command_that_was_offered(monkeypatch):
    dispatched = {}

    async def _fake_dispatch(cmd, args, db):
        dispatched["call"] = (cmd, args)
        return "🔮 forecast output", "HTML", None

    async def _no_fallback(*a, **k):  # pragma: no cover
        raise AssertionError("the confirmation went back through chat")

    cs._PENDING_OFFER["42"] = ("forecast", "BTCUSDT")
    monkeypatch.setattr(cs, "_dispatch", _fake_dispatch)
    monkeypatch.setattr(cs, "_ai_fallback", _no_fallback)

    update = {"message": {"chat": {"id": 42}, "text": "yes"}}
    reply, _mode, _kb = await cs.parse_and_execute(update, "tok", [], _FakeDB())

    assert dispatched["call"] == ("forecast", "BTCUSDT")
    assert reply == "🔮 forecast output"
    assert "42" not in cs._PENDING_OFFER, "the offer should be consumed once"


@pytest.mark.asyncio
async def test_yes_with_nothing_pending_is_ordinary_chat(monkeypatch):
    async def _fake_fallback(text, db, *, chat_id="", hint=""):
        return "Yes what, Sir?", "HTML", None

    monkeypatch.setattr(cs, "_ai_fallback", _fake_fallback)

    update = {"message": {"chat": {"id": 42}, "text": "yes"}}
    reply, _mode, _kb = await cs.parse_and_execute(update, "tok", [], _FakeDB())
    assert reply == "Yes what, Sir?"


# ── The thread remembers what it is about ────────────────────────────────────
# Second report: the follow-up turn came back as bullets on ARIMA/ARIMAX with
# no numbers at all, because it named no instrument and the model chose not to
# call the tool that turn.

def test_a_follow_up_carries_the_symbol_from_the_thread():
    cs._CHAT_HISTORY["42"] = [
        {"role": "user", "content": "how can we predict bitcoin"},
        {"role": "assistant", "content": "Kronos projects BTCUSDT up +2.2%."},
    ]
    assert cs._forecast_request("how would you forecast it", "42") == ("BTCUSDT", "1h")


def test_without_a_thread_there_is_still_nothing_to_forecast():
    assert cs._forecast_request("how would you forecast it", "42") is None


# ── The engine's numbers are not optional ────────────────────────────────────

@pytest.mark.asyncio
async def test_a_method_answer_with_no_numbers_gets_the_live_card(monkeypatch):
    async def _fake_forecast(args, db):
        assert args == "BTCUSDT 1h"
        return "🔮 <b>Kronos Forecast — BTC/USDT (1h)</b>", "HTML", {"inline_keyboard": []}

    monkeypatch.setattr(cs, "_handle_forecast", _fake_forecast)

    reply, markup = await cs._attach_forecast(
        "- Fit an ARIMA(p,d,q) model to the log-returns r(t).",
        "how do I predict bitcoin",
        _FakeDB(),
        chat_id="42",
    )

    assert "ARIMA" in reply and "Kronos Forecast" in reply
    assert markup is not None


@pytest.mark.asyncio
async def test_the_card_is_not_escaped_when_it_is_appended(monkeypatch):
    """The narrative is converted to HTML; the card already is HTML."""
    async def _fake_forecast(args, db):
        return "🔮 <b>Kronos Forecast</b> <code>64,253.8</code>", "HTML", None

    monkeypatch.setattr(cs, "_handle_forecast", _fake_forecast)

    reply, _markup = await cs._attach_forecast(
        "Fit **ARIMA** to the log-returns of BTCUSDT.",
        "predict bitcoin", _FakeDB(), chat_id="42",
    )

    assert "<b>Kronos Forecast</b>" in reply, "the card's own tags must survive"
    assert "&lt;b&gt;" not in reply
    assert "**ARIMA**" not in reply, "the narrative should still be converted"


@pytest.mark.asyncio
async def test_the_card_is_attached_even_when_the_model_ran_the_tool(monkeypatch):
    """Quoting numbers in a bullet is not the same as handing over the card.

    The reported reply buried "+2.08 %, target 64 164.5" in an "Example" line
    with no entries and no buttons. The two cannot disagree: both come from one
    cached forecast response.
    """
    async def _fake_forecast(args, db):
        return "🔮 <b>Kronos Forecast</b> +2.08%", "HTML", {"inline_keyboard": []}

    monkeypatch.setattr(cs, "_handle_forecast", _fake_forecast)

    reply, markup = await cs._attach_forecast(
        "Kronos projects BTCUSDT up +2.08%, target 64,164.5, Sir.",
        "predict bitcoin", _FakeDB(), chat_id="42",
    )
    assert "Kronos Forecast" in reply
    assert markup is not None, "the user must get the tappable entries"


@pytest.mark.asyncio
async def test_an_answer_about_something_else_is_left_alone(monkeypatch):
    async def _never(args, db):  # pragma: no cover
        raise AssertionError("a forecast was attached to an unrelated answer")

    monkeypatch.setattr(cs, "_handle_forecast", _never)

    reply, markup = await cs._attach_forecast(
        "The Krebs cycle oxidises acetyl-CoA, Sir.",
        "explain the krebs cycle", _FakeDB(), chat_id="42",
    )
    assert "Krebs" in reply and markup is None


@pytest.mark.asyncio
async def test_a_dead_engine_leaves_the_explanation_intact(monkeypatch):
    async def _boom(args, db):
        raise RuntimeError("kronos offline")

    monkeypatch.setattr(cs, "_handle_forecast", _boom)

    reply, markup = await cs._attach_forecast(
        "- Fit ARIMA to the log-returns of BTCUSDT.",
        "predict bitcoin", _FakeDB(), chat_id="42",
    )
    assert "ARIMA" in reply and markup is None


# ── Natural phrasings, not just the word "forecast" ──────────────────────────

@pytest.mark.parametrize("text", [
    "what is next for US30",        # the uncontracted form matched nothing
    "what's next for US30",
    "whats next for US30",
    "will US30 rise this week",
    "will US30 break 45000",
    "how high can US30 go",
    "where is US30 heading",
])
def test_a_prediction_can_be_asked_in_plain_english(text):
    assert cs._forecast_request(text) == ("US30", "1h")


@pytest.mark.parametrize("text", [
    "will you remember my risk limits",     # "will" but nothing to forecast
    "what is next on the roadmap",
    "US30 is an index of thirty stocks",    # a statement, not a question
])
def test_plain_english_does_not_over_trigger(text):
    assert cs._forecast_request(text) is None


@pytest.mark.asyncio
async def test_the_attached_card_uses_the_timeframe_that_was_asked_for(monkeypatch):
    seen = {}

    async def _fake_forecast(args, db):
        seen["args"] = args
        return "🔮 <b>Kronos Forecast</b>", "HTML", None

    monkeypatch.setattr(cs, "_handle_forecast", _fake_forecast)

    await cs._attach_forecast(
        "Fit ARIMA to the log-returns.",
        "predict XAUUSD on 4h", _FakeDB(), chat_id="42",
    )
    assert seen["args"] == "XAUUSD 4h"


# ── Volume-gated instruments still answer ────────────────────────────────────

@pytest.mark.asyncio
async def test_a_volume_gated_index_returns_the_card_not_an_essay(monkeypatch):
    """GER40/UK100 carry no traded volume, so Kronos issues NO_TRADE by design.

    That is a real answer with a live anchor price and a route onward, so the
    auto-run must return it rather than falling through to a chat paragraph.
    """
    gated = (
        "🔮 <b>Kronos Forecast — GER40 (1h)</b>\n"
        "➡️ <b>NO_TRADE  |  +0.00%  |  0% confidence</b>\n"
        "⚠️ <i>volume is a hard precondition and it is unavailable for GER40</i>\n"
        "ℹ️ No entries: use <code>/analyze GER40</code> for the technical read."
    )

    async def _fake_forecast(args, db):
        return gated, "HTML", {"inline_keyboard": []}

    monkeypatch.setattr(cs, "_handle_forecast", _fake_forecast)

    reply, _mode, markup = await cs._forecast_reply(
        "predict GER40", "GER40", "1h", _FakeDB(), chat_id="42"
    )

    assert reply and "NO_TRADE" in reply
    assert "/analyze GER40" in reply, "a gated pair must still have somewhere to go"
    assert markup is not None


def test_the_prompt_forbids_asking_for_an_instrument_it_already_has():
    """The reported closer: "if you have a specific instrument … I can run the
    Kronos forecast instantly" — written directly under a BTC/USDT forecast it
    had just run."""
    prompt = cs._jarvis_system_prompt().lower()
    assert "never close by asking the user to name an instrument" in prompt
    assert "it has already run" in prompt


@pytest.mark.parametrize("reply_text", [
    "Kronos projects BTCUSDT up +2.08%, Sir.",          # "projects", no trigger word
    "The Kronos engine puts BTCUSDT higher.",
    "My price target for BTCUSDT is 64,164.",
    "A Monte-Carlo over BTCUSDT paths gives a positive drift.",
    "The outlook for BTCUSDT is constructive.",
])
@pytest.mark.asyncio
async def test_forecast_talk_is_recognised_however_it_is_worded(reply_text, monkeypatch):
    async def _fake_forecast(args, db):
        return "🔮 <b>Kronos Forecast</b>", "HTML", {"inline_keyboard": []}

    monkeypatch.setattr(cs, "_handle_forecast", _fake_forecast)

    reply, markup = await cs._attach_forecast(
        reply_text, "predict bitcoin", _FakeDB(), chat_id="42"
    )
    assert "Kronos Forecast" in reply and markup is not None
