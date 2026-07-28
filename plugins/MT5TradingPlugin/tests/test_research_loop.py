"""
Background research loop: idle-provider selection, source verification, decay.

Every collector is stubbed — these tests must never hit ForexFactory, a news
feed, or an AI provider. What is under test is the loop's own rules:
  * the PRIMARY analyst provider is never borrowed, busy or not
  * a finding with no resolvable source URL is stored speculative and can
    never gate a trade signal
  * findings decay
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plugins.AiMarketAnalyst.backend.services import (  # noqa: E402
    analysis_router,
    knowledge_service,
)
from plugins.AiMarketAnalyst.backend.services.provider_health import (  # noqa: E402
    provider_health,
)
from plugins.MT5TradingPlugin.backend.models import MT5Base, ResearchFinding  # noqa: E402
from plugins.MT5TradingPlugin.backend.services import (  # noqa: E402
    agent_bus,
    economic_calendar,
    research_loop,
)


@pytest_asyncio.fixture()
async def db(monkeypatch) -> AsyncSession:
    # Keep the tick hermetic: no bus, no brains, no OpenHuman, no network.
    async def _noop_publish(_msg):
        return None

    monkeypatch.setattr(agent_bus, "publish_finding", _noop_publish)
    monkeypatch.setattr(research_loop, "_fan_out", lambda _f: None)
    provider_health.reset()

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MT5Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()
    provider_health.reset()


def _finding(**kw) -> agent_bus.ResearchFindingMessage:
    base = dict(
        kind="news", headline="Fed holds rates", body="",
        source="Reuters", source_url="https://example.com/a",
        confidence=0.6, speculative=False,
    )
    base.update(kw)
    return agent_bus.ResearchFindingMessage(**base)


async def _no_reminder(_db):
    """The reminder reaches the calendar feeds and the AI knowledge store — the
    tick's own rules are what the cycle tests are about. Covered separately."""
    return 0


# ── Idle-provider selection ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_primary_provider_is_never_borrowed(monkeypatch, db):
    """The analyst provider is excluded even while completely idle."""
    async def _ranked(_db):
        return [
            SimpleNamespace(id=1, label="NVIDIA"),
            SimpleNamespace(id=2, label="Cerebras"),
            SimpleNamespace(id=3, label="Groq"),
        ]

    monkeypatch.setattr(analysis_router, "rank_providers", _ranked)
    monkeypatch.setattr(provider_health, "remote_in_flight", _zero)

    idle = await research_loop.idle_provider_labels(db)
    assert idle == ["Cerebras", "Groq"], "NVIDIA is PRIMARY and must be skipped"


@pytest.mark.asyncio
async def test_a_provider_serving_a_live_analysis_is_not_idle(monkeypatch, db):
    async def _ranked(_db):
        return [
            SimpleNamespace(id=1, label="NVIDIA"),
            SimpleNamespace(id=2, label="Cerebras"),
            SimpleNamespace(id=3, label="Groq"),
        ]

    monkeypatch.setattr(analysis_router, "rank_providers", _ranked)
    monkeypatch.setattr(provider_health, "remote_in_flight", _zero)

    # Cerebras is mid-request — exactly what analysis_router does before a call.
    provider_health.mark_start("Cerebras")
    assert await research_loop.idle_provider_labels(db) == ["Groq"]

    # Once it completes it becomes borrowable again.
    provider_health.mark_success("Cerebras", 120.0)
    assert await research_loop.idle_provider_labels(db) == ["Cerebras", "Groq"]


@pytest.mark.asyncio
async def test_no_providers_means_no_idle_providers(monkeypatch, db):
    async def _none(_db):
        return []

    monkeypatch.setattr(analysis_router, "rank_providers", _none)
    assert await research_loop.idle_provider_labels(db) == []


@pytest.mark.asyncio
async def test_sole_provider_is_the_primary_so_nothing_is_borrowable(monkeypatch, db):
    async def _one(_db):
        return [SimpleNamespace(id=1, label="OnlyOne")]

    monkeypatch.setattr(analysis_router, "rank_providers", _one)
    monkeypatch.setattr(provider_health, "remote_in_flight", _zero)
    assert await research_loop.idle_provider_labels(db) == []


async def _zero(_label: str) -> int:
    return 0


# ── Source verification ──────────────────────────────────────────────────────

def test_only_http_urls_count_as_a_verifiable_source():
    assert research_loop._has_source("https://reuters.com/x") is True
    assert research_loop._has_source("http://reuters.com/x") is True
    assert research_loop._has_source("") is False
    assert research_loop._has_source(None) is False
    assert research_loop._has_source("reuters.com/x") is False
    assert research_loop._has_source("javascript:alert(1)") is False


@pytest.mark.asyncio
async def test_sourceless_finding_is_stored_speculative_and_cannot_gate(db):
    await research_loop.store_findings(db, [
        _finding(headline="Sourced report", source_url="https://example.com/real"),
        _finding(headline="Rumour: surprise cut", source_url=None, speculative=True),
    ])

    active = await research_loop.active_findings(db)
    assert len(active) == 2, "speculative findings are still stored and visible"

    gating = await research_loop.gating_findings(db)
    assert len(gating) == 1
    assert gating[0].headline == "Sourced report"
    assert all(not f.speculative for f in gating)


@pytest.mark.asyncio
async def test_a_tick_of_only_speculative_findings_gates_nothing(db):
    await research_loop.store_findings(db, [
        _finding(headline=f"Unsourced {i}", source_url=None, speculative=True)
        for i in range(4)
    ])
    assert len(await research_loop.active_findings(db)) == 4
    assert await research_loop.gating_findings(db) == []


@pytest.mark.asyncio
async def test_stored_findings_keep_their_confidence_and_provenance(db):
    ids = await research_loop.store_findings(db, [
        _finding(kind="calendar", headline="US CPI", symbol="XAUUSD",
                 source="ForexFactory",
                 source_url="https://www.forexfactory.com/calendar",
                 confidence=0.9),
    ])
    assert len(ids) == 1
    row = await db.get(ResearchFinding, ids[0])
    assert row.kind == "calendar"
    assert row.symbol == "XAUUSD"
    assert row.confidence == pytest.approx(0.9)
    assert row.source_url.startswith("https://")
    assert row.speculative is False
    assert row.decay_at > datetime.utcnow()


# ── Decay ────────────────────────────────────────────────────────────────────

def test_decay_horizon_differs_by_kind():
    now = datetime(2026, 1, 1, 12, 0, 0)
    assert research_loop._decay_at("calendar", now) == now + timedelta(hours=72)
    assert research_loop._decay_at("news", now) == now + timedelta(hours=12)
    assert research_loop._decay_at("sentiment", now) == now + timedelta(hours=6)
    # Unknown kinds fall back to the news horizon rather than never decaying.
    assert research_loop._decay_at("mystery", now) == now + timedelta(hours=12)


@pytest.mark.asyncio
async def test_decayed_findings_drop_out_of_recall(db):
    db.add(ResearchFinding(
        kind="news", headline="Stale headline", source_url="https://example.com/old",
        confidence=0.6, speculative=False,
        decay_at=datetime.utcnow() - timedelta(hours=1),
    ))
    db.add(ResearchFinding(
        kind="news", headline="Fresh headline", source_url="https://example.com/new",
        confidence=0.6, speculative=False,
        decay_at=datetime.utcnow() + timedelta(hours=6),
    ))
    await db.commit()

    active = await research_loop.active_findings(db)
    assert [f.headline for f in active] == ["Fresh headline"]


# ── A full tick ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cycle_runs_and_reports_the_llm_step_as_skipped(monkeypatch, db):
    """No idle provider -> collection still happens, the LLM step does not."""
    async def _ranked(_db):
        return [SimpleNamespace(id=1, label="OnlyPrimary")]

    async def _calendar(_symbols):
        return [_finding(kind="calendar", headline="US CPI",
                         source_url="https://www.forexfactory.com/calendar")]

    async def _news():
        return [_finding(headline="Gold rallies", source_url="https://example.com/g"),
                _finding(headline="Unsourced chatter", source_url=None,
                         speculative=True)]

    async def _sentiment():
        return []

    async def _no_openhuman(_symbols):
        return []

    monkeypatch.setattr(analysis_router, "rank_providers", _ranked)
    monkeypatch.setattr(provider_health, "remote_in_flight", _zero)
    monkeypatch.setattr(research_loop, "collect_calendar", _calendar)
    monkeypatch.setattr(research_loop, "collect_news", _news)
    monkeypatch.setattr(research_loop, "collect_sentiment", _sentiment)
    monkeypatch.setattr(research_loop, "remind_agents_of_calendar", _no_reminder)

    result = await research_loop.run_research_cycle(db, symbols=["XAUUSD"])

    assert result["idle_providers"] == []
    assert result["llm_step"].startswith("skipped")
    assert result["collected"] == 3
    assert result["stored"] == 3
    assert result["verified"] == 2
    assert result["speculative"] == 1
    assert len(await research_loop.gating_findings(db)) == 2


@pytest.mark.asyncio
async def test_a_dead_source_does_not_stop_the_tick(monkeypatch, db):
    async def _ranked(_db):
        return [SimpleNamespace(id=1, label="P"), SimpleNamespace(id=2, label="Q")]

    async def _boom(*_a, **_kw):
        raise ConnectionError("feed down")

    async def _news():
        return [_finding(headline="Still collected",
                         source_url="https://example.com/ok")]

    async def _empty():
        return []

    monkeypatch.setattr(analysis_router, "rank_providers", _ranked)
    monkeypatch.setattr(provider_health, "remote_in_flight", _zero)
    monkeypatch.setattr(research_loop, "collect_calendar", _boom)
    monkeypatch.setattr(research_loop, "collect_news", _news)
    monkeypatch.setattr(research_loop, "collect_sentiment", _empty)
    monkeypatch.setattr(research_loop, "remind_agents_of_calendar", _no_reminder)

    result = await research_loop.run_research_cycle(db, symbols=["XAUUSD"])
    assert result["collected"] == 1
    assert result["idle_providers"] == ["Q"]
    assert result["llm_step"] == "ran"


# ── The economic calendar window ─────────────────────────────────────────────
#
# The window feeds three consumers (the /research page, the agent reminder, the
# SMC prompt). What is under test is the merge and the query — never the feeds.

def _raw_event(title, currency, impact, when, source="ForexFactory", **kw):
    return economic_calendar._event(
        title=title, currency=currency, impact=impact, when=when, source=source, **kw
    )


def _at(hours_from_now: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours_from_now)


def test_impact_not_wording_decides_what_is_a_fomo_date():
    """A keyword match on a low-impact release is not a volatility event."""
    assert _raw_event("Fed Press Conference", "USD", "high", _at(4))["is_fomo"]
    # "unemployment" is in HIGH_IMPACT_KEYWORDS, but this one does not move price.
    assert not _raw_event("Spanish Unemployment Rate", "EUR", "low", _at(4))["is_fomo"]
    assert not _raw_event("Flash Manufacturing PMI", "GBP", "medium", _at(4))["is_fomo"]


def test_query_filters_compose_and_stay_sorted():
    events = [
        _raw_event("US CPI y/y", "USD", "high", _at(30)),
        _raw_event("Fed Interest Rate Decision", "USD", "high", _at(5)),
        _raw_event("Retail Sales", "EUR", "medium", _at(10)),
        _raw_event("German Bond Auction", "EUR", "low", _at(400)),
    ]

    usd = economic_calendar.query_calendar(events, currencies=["usd"])
    assert [e["title"] for e in usd] == ["Fed Interest Rate Decision", "US CPI y/y"]

    assert len(economic_calendar.query_calendar(events, fomo_only=True)) == 2
    assert len(economic_calendar.query_calendar(events, impact="medium")) == 1
    # days bounds the future edge: the 400h event is ~17 days out.
    assert len(economic_calendar.query_calendar(events, days=7)) == 3


def test_past_events_are_kept_by_default_but_a_reminder_looks_forward():
    """Today's earlier releases stay visible with their actual; reminders don't."""
    events = [
        _raw_event("ECB Rate Decision", "EUR", "high", _at(-6), actual="2.5%"),
        _raw_event("US NFP", "USD", "high", _at(12)),
    ]
    assert len(economic_calendar.query_calendar(events)) == 2
    forward = economic_calendar.query_calendar(events, lookback_hours=0)
    assert [e["title"] for e in forward] == ["US NFP"]

    nearest = economic_calendar.next_event(events)
    assert nearest["title"] == "US NFP"


def test_hours_away_is_computed_at_query_time_not_cached():
    """The window outlives the countdown — a cached 'in 3h' would be a lie."""
    event = _raw_event("US CPI", "USD", "high", _at(5))
    assert "hours_away" not in event
    assert economic_calendar.query_calendar([event])[0]["hours_away"] == pytest.approx(
        5.0, abs=0.1
    )


def test_agent_block_states_absolute_utc_and_survives_an_empty_calendar():
    events = economic_calendar.query_calendar(
        [_raw_event("FOMC Statement", "USD", "high", _at(20), forecast="3.75")]
    )
    block = economic_calendar.format_for_agents(events)
    assert events[0]["time_utc"] in block  # absolute, never "in 20h"
    assert "forecast 3.75" in block
    assert "hours" not in block.split("factor them into")[-1].lower()

    assert "No high-impact economic events" in economic_calendar.format_for_agents([])


@pytest.mark.asyncio
async def test_tradingview_only_extends_past_the_forexfactory_week(monkeypatch):
    """Split by date, so one release can never appear twice under two names."""
    ff = [_raw_event("FOMC Statement", "USD", "high", _at(24))]
    tv = [
        # Same release, TradingView's wording — inside FF's week, must be dropped.
        _raw_event("Fed Interest Rate Decision", "USD", "high", _at(24),
                   source="TradingView"),
        _raw_event("US CPI y/y", "USD", "high", _at(240), source="TradingView"),
    ]

    async def _ff():
        return ff

    async def _tv(after):
        return tv

    monkeypatch.setattr(economic_calendar, "_fetch_forexfactory", _ff)
    monkeypatch.setattr(economic_calendar, "_fetch_tradingview", _tv)

    window = await economic_calendar.fetch_calendar_window(force=True)
    assert [e["title"] for e in window] == ["FOMC Statement", "US CPI y/y"]


@pytest.mark.asyncio
async def test_a_dead_calendar_feed_keeps_the_previous_window(monkeypatch):
    """A scheduled event from ten minutes ago is still the truth about Thursday."""
    async def _empty():
        return []

    async def _none(_after):
        return []

    monkeypatch.setattr(economic_calendar, "_fetch_forexfactory", _empty)
    monkeypatch.setattr(economic_calendar, "_fetch_tradingview", _none)

    before = list(economic_calendar._cache)
    assert await economic_calendar.fetch_calendar_window(force=True) == before


# ── The agent reminder ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reminder_writes_one_global_row_plus_the_watchlist(monkeypatch, db):
    """Null role and null symbol is what puts the block in every agent prompt."""
    written = []

    async def _store(_db, **kw):
        written.append(kw)
        return SimpleNamespace(id=len(written))

    async def _upcoming(limit=8, symbol=None):
        return economic_calendar.query_calendar(
            [_raw_event("FOMC Statement", "USD", "high", _at(20))]
        )

    monkeypatch.setattr(economic_calendar, "upcoming_fomo", _upcoming)
    monkeypatch.setattr(knowledge_service, "store_knowledge", _store)

    count = await research_loop.remind_agents_of_calendar(db)

    assert count == 1 + len(research_loop.DEFAULT_WATCHLIST)
    glob = written[0]
    assert glob["agent_role"] is None and glob["symbol"] is None
    assert glob["title"] == research_loop.CALENDAR_KNOWLEDGE_TITLE
    assert glob["weight"] >= 9.0
    assert "FOMC Statement" in glob["content"]
    # The fixed title is what makes store_knowledge overwrite instead of pile up.
    assert {w["title"] for w in written} == {research_loop.CALENDAR_KNOWLEDGE_TITLE}
    assert {w["symbol"] for w in written[1:]} == set(research_loop.DEFAULT_WATCHLIST)


@pytest.mark.asyncio
async def test_a_failing_reminder_never_kills_the_tick(monkeypatch, db):
    async def _boom(*_a, **_kw):
        raise RuntimeError("knowledge store down")

    monkeypatch.setattr(economic_calendar, "upcoming_fomo", _boom)
    assert await research_loop.remind_agents_of_calendar(db) == 0


# ── The typed bus ────────────────────────────────────────────────────────────

def test_agent_messages_round_trip_through_their_schema():
    msg = agent_bus.ResearchFindingMessage(
        kind="news", headline="H", source_url="https://x/y", confidence=0.7,
        speculative=False,
    )
    revived = agent_bus.ResearchFindingMessage.from_dict(msg.to_dict())
    assert revived == msg
    # Unknown keys from a future producer are ignored, not fatal.
    assert agent_bus.ResearchFindingMessage.from_dict(
        {**msg.to_dict(), "unexpected": 1}
    ) == msg

    proposal = agent_bus.StrategyProposalMessage(
        symbol="XAUUSD", stance="stand_aside", rationale="CPI pending",
        finding_ids=[1, 2], confidence=0.8, speculative=False,
    )
    assert agent_bus.StrategyProposalMessage.from_dict(
        proposal.to_dict()
    ) == proposal


def test_agent_topics_do_not_collide_with_the_sse_topics():
    from app.core.events import Topics

    assert not (agent_bus.AgentTopics.ALL & Topics.ALL)
