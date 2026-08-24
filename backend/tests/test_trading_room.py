"""The trading room's state registry and vote tally.

The room is what the 3D board and the agent panels read, so a wrong tally or a
seat left in the wrong state is visible to the user immediately.
"""
from __future__ import annotations

import pytest

from app.agents import room
from app.core.events import Topics


@pytest.fixture(autouse=True)
def clean_room(monkeypatch):
    """Each test gets an empty registry and a captured event bus."""
    monkeypatch.setattr(room, "_agent_state", {})
    monkeypatch.setattr(room, "_sessions", [])
    monkeypatch.setattr(room, "_focus_symbols", [])

    published: list[tuple[str, dict]] = []

    async def _capture(topic, data):
        published.append((topic, data))

    monkeypatch.setattr(room.event_bus, "publish", _capture)
    return published


def test_every_pipeline_role_has_a_named_persona():
    for role in ("market_analyst", "signal_generator", "risk_manager",
                 "sentiment_analyst", "trade_executor", "position_reviewer",
                 "strategy_optimizer"):
        persona = room.persona_for(role)
        assert persona["human_name"]
        assert persona["seat"] >= 0

    seats = [p["seat"] for p in room.PERSONAS.values()]
    assert len(seats) == len(set(seats)), "two agents cannot share a chair"


def test_unknown_role_still_gets_a_seat():
    persona = room.persona_for("weather_forecaster")
    assert persona["human_name"] == "Weather Forecaster"
    assert persona["color"]
    # The 3D room builds a body from this, so it can never come back missing.
    assert persona["gender"] in {"male", "female"}


def test_every_persona_declares_a_gender():
    for role in room.PERSONAS:
        assert room.persona_for(role)["gender"] in {"male", "female"}
    assert room.CEO_PERSONA["gender"] in {"male", "female"}


def test_saved_gender_overrides_the_default(monkeypatch):
    """What the settings page saves is what the room renders."""
    monkeypatch.setattr(room, "_persona_overrides", {})
    assert room.persona_for("risk_manager")["gender"] == "male"

    room.set_persona_overrides({"risk_manager": {"gender": "female"}})
    persona = room.persona_for("risk_manager")
    assert persona["gender"] == "female"
    # An override of one field must not blank the others.
    assert persona["human_name"] == "Thabo"
    assert persona["seat"] == 3


@pytest.mark.asyncio
async def test_pipeline_walks_a_seat_through_its_states(clean_room):
    await room.session_started("s1", "BTCUSDT", "1h", "manual")
    await room.agent_started("s1", "market_analyst", "Market Analyst", "BTCUSDT")
    assert room.snapshot()["agents"][0]["state"] == room.ANALYZING

    await room.agent_completed(
        "s1", "market_analyst", "Market Analyst", "BTCUSDT",
        {"action": "bullish", "confidence": 0.8, "reasoning": "trend up"},
    )
    seat = room.snapshot()["agents"][0]
    assert seat["state"] == room.PRESENTING
    assert seat["last_decision"]["action"] == "bullish"

    await room.session_completed("s1", {"symbol": "BTCUSDT", "final_action": "buy", "decisions": []})
    assert room.snapshot()["agents"][0]["state"] == room.RESTING

    topics = [t for t, _ in clean_room]
    assert topics == [
        Topics.SESSION_STARTED,
        Topics.AGENT_STARTED,
        Topics.AGENT_COMPLETED,
        Topics.SESSION_COMPLETED,
    ]


@pytest.mark.asyncio
async def test_completed_decisions_attach_to_their_session(clean_room):
    await room.session_started("s2", "ETHUSDT", "1h", "room")
    await room.agent_completed(
        "s2", "risk_manager", "Risk Manager", "ETHUSDT",
        {"action": "approve", "confidence": 0.6, "reasoning": "size ok"},
    )
    session = room.snapshot()["sessions"][0]
    assert session["session_id"] == "s2"
    assert [d["role"] for d in session["decisions"]] == ["risk_manager"]


@pytest.mark.asyncio
async def test_agent_speaking_carries_the_spoken_reasoning(clean_room, monkeypatch):
    """The debate view needs who said what — persona, vote and the words."""
    # Persona names may be overridden from the settings page (and a sibling
    # test writes one); the default name is what this assertion pins.
    monkeypatch.setattr(room, "_persona_overrides", {})
    await room.agent_speaking(
        "s9", "signal_generator", "Naledi", "XAUUSD",
        {"action": "buy", "confidence": 0.7, "reasoning": "demand zone held"},
    )
    topic, data = clean_room[-1]
    assert topic == Topics.AGENT_SPEAKING
    assert data["session_id"] == "s9"
    assert data["role"] == "signal_generator"
    assert data["human_name"] == "Naledi"
    assert data["text"] == "demand zone held"
    assert data["action"] == "buy"


@pytest.mark.asyncio
async def test_agent_speaking_is_silent_without_words(clean_room):
    """A decision with nothing to say must not emit an empty bubble."""
    await room.agent_speaking(
        "s10", "risk_manager", "Thabo", "BTCUSDT",
        {"action": "hold", "confidence": 0.5, "reasoning": ""},
    )
    assert clean_room == [], "no reasoning → no speaking event"


@pytest.mark.asyncio
async def test_chair_closes_the_meeting_out_loud(clean_room, monkeypatch):
    monkeypatch.setattr(room, "_persona_overrides", {})
    await room.chair_speaking(
        "s11",
        {"symbol": "BTCUSDT", "final_action": "buy",
         "final_confidence": 0.82, "final_reasoning": "board agrees"},
    )
    topic, data = clean_room[-1]
    assert topic == Topics.AGENT_SPEAKING
    assert data["chair"] is True
    assert data["human_name"] == "JARVIS"
    assert data["text"] == "board agrees"
    assert data["confidence"] == 0.82


def test_ceo_persona_resolves_for_the_chair():
    """chair_speaking publishes under the JARVIS persona, not a generic seat."""
    persona = room.persona_for("ceo")
    assert persona["human_name"] in {"JARVIS"} or persona["seat"] == -1
    assert persona["seat"] == room.CEO_PERSONA["seat"]


@pytest.mark.asyncio
async def test_failed_agent_is_marked_not_silently_idle(clean_room):
    await room.agent_failed("s3", "trade_executor", "Trade Executor", "rate limited")
    seat = room.snapshot()["agents"][0]
    assert seat["state"] == room.ERROR
    assert seat["error"] == "rate limited"


@pytest.mark.asyncio
async def test_focus_is_broadcast_and_readable(clean_room):
    await room.set_focus("XAUUSD")
    assert room.get_focus_symbol() == "XAUUSD"
    assert room.get_focus_symbols() == ["XAUUSD"]
    assert clean_room[-1] == (Topics.ROOM_FOCUS, {"symbols": ["XAUUSD"], "symbol": "XAUUSD"})


@pytest.mark.asyncio
async def test_focus_accepts_multiple_pairs(clean_room):
    await room.set_focus(["XAUUSD", "btcusdt", "XAUUSD"])
    assert room.get_focus_symbols() == ["XAUUSD", "BTCUSDT"]  # deduped, normalised
    assert room.is_focused("BTC/USDT")
    assert not room.is_focused("ETHUSDT")


def test_consensus_maps_role_vocabularies_onto_buy_sell_hold():
    consensus = room.consensus_from([
        {"action": "bullish", "confidence": 0.8},   # market analyst
        {"action": "buy", "confidence": 0.7},       # signal generator
        {"action": "approve", "confidence": 0.9},   # risk manager
        {"action": "hold", "confidence": 0.4},
    ])
    assert consensus["tally"] == {"buy": 3, "sell": 0, "hold": 1}
    assert consensus["leader"] == "buy"
    assert consensus["agreement"] == pytest.approx(0.75)
    assert consensus["weighted_confidence"] == pytest.approx(0.8)


def test_consensus_of_nothing_does_not_divide_by_zero():
    consensus = room.consensus_from([])
    assert consensus["agreement"] == 0
    assert consensus["weighted_confidence"] == 0


def test_session_history_is_bounded():
    for i in range(room._MAX_SESSIONS + 15):
        room._sessions.append({"session_id": str(i), "decisions": []})
        del room._sessions[:-room._MAX_SESSIONS]
    assert len(room._sessions) == room._MAX_SESSIONS


# ── Background worker symbol selection ──────────────────────────────────────


@pytest.fixture
def worker(monkeypatch):
    from app.workers import room_worker

    monkeypatch.setattr(room_worker, "_last_analyzed", {})
    monkeypatch.setattr(room_worker, "_rotation_index", 0)
    monkeypatch.setattr(room_worker, "_focus_rotation", 0)
    monkeypatch.setattr(room_worker, "_cooldown_s", 1800)
    return room_worker


@pytest.mark.asyncio
async def test_focused_pair_wins_over_signals(worker, monkeypatch, async_session):
    monkeypatch.setattr(worker.room, "get_focus_symbols", lambda: ["XAUUSD"])
    assert await worker._pick_symbol(async_session) == ("XAUUSD", "focus")


@pytest.mark.asyncio
async def test_multiple_focus_pairs_rotate(worker, monkeypatch, async_session):
    monkeypatch.setattr(worker.room, "get_focus_symbols", lambda: ["XAUUSD", "BTCUSDT"])
    first, r1 = await worker._pick_symbol(async_session)
    second, r2 = await worker._pick_symbol(async_session)
    assert r1 == r2 == "focus"
    assert {first, second} == {"XAUUSD", "BTCUSDT"}


@pytest.mark.asyncio
async def test_focused_pair_on_cooldown_does_not_fall_through_to_rotation(
    worker, monkeypatch, async_session
):
    """A pinned pair means the room stays on it — not that it wanders off."""
    import time

    monkeypatch.setattr(worker.room, "get_focus_symbols", lambda: ["XAUUSD"])
    worker._last_analyzed["XAUUSD"] = time.time()
    symbol, reason = await worker._pick_symbol(async_session)
    assert symbol is None
    assert reason == "focus_waiting"


@pytest.mark.asyncio
async def test_rotation_advances_and_skips_cooled_down_pairs(worker, monkeypatch, async_session):
    import time

    monkeypatch.setattr(worker.room, "get_focus_symbols", lambda: [])
    first, reason = await worker._pick_symbol(async_session)
    assert reason == "rotation"

    worker._last_analyzed[first] = time.time()
    second, _ = await worker._pick_symbol(async_session)
    assert second != first
