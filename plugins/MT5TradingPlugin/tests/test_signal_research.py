"""
Per-signal research queue: concurrency, degradation, provenance, provider policy.

Every collector and the model call itself are stubbed — these tests must never
hit a news feed, a candle provider or an AI provider. What is under test is the
queue's own contract:

  * at most ``concurrency`` signals are researched at a time
  * a dead news feed still yields a prediction (steps degrade, jobs do not fail)
  * a prediction with no resolvable source is speculative and cannot gate
  * the primary provider is only borrowed once the idle wait expires
  * the same signal is never queued twice while a job for it is in flight
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plugins.MT5TradingPlugin.backend.models import (  # noqa: E402
    MT5Base, ResearchFinding, SignalResearchJob,
)
from plugins.MT5TradingPlugin.backend.services import (  # noqa: E402
    agent_bus, research_loop, signal_research,
)


#: Records handed to the JARVIS brains during a test.
_LEARNED: list = []


@pytest_asyncio.fixture()
async def db(monkeypatch) -> AsyncSession:
    _LEARNED.clear()
    # Hermetic: no bus, no brains, no network, no model.
    async def _noop_publish(_msg):
        return None

    monkeypatch.setattr(agent_bus, "publish_finding", _noop_publish)
    monkeypatch.setattr(research_loop, "_fan_out", lambda _f: None)
    # The brains are a separate subsystem; capture the record instead of writing it.
    monkeypatch.setattr(signal_research, "_learn_prediction",
                        lambda *a, **k: _LEARNED.append((a, k)))
    # Candles, knowledge, feeds and the calendar are exercised by their own
    # modules' tests; here they are noise that would reach the network.
    monkeypatch.setattr(signal_research, "_candles", _fake_candles)
    monkeypatch.setattr(signal_research, "_pair_knowledge", _fake_knowledge)
    monkeypatch.setattr(signal_research, "_calendar_block", _fake_calendar)
    monkeypatch.setattr(signal_research, "_web_news", _fake_web_news)
    monkeypatch.setattr(signal_research, "IDLE_WAIT_S", 0.0)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MT5Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


# ── Stubs ────────────────────────────────────────────────────────────────────

async def _fake_candles(symbol, timeframe, limit=200):
    """A clean uptrend, enough bars for every indicator."""
    return [[i * 3_600_000, 100 + i, 101 + i, 99 + i, 100.5 + i, 1000.0] for i in range(120)]


async def _fake_knowledge(db, symbol):
    return f"# What we already know about {symbol}\n- nothing yet"


async def _fake_calendar(symbol):
    return "# Scheduled events\nNo high-impact economic events scheduled."


async def _fake_web_news(symbol, *, deep=2):
    return (f"# Live web news — {symbol}\n- [Reuters] headline\n  https://example.com/n",
            ["https://example.com/n"])


def _cascade_stub(content, *, provider="openai", record=None, ok=True):
    """Stand in for analysis_router.analyze_with_cascade."""
    async def _run(db, messages, *, validator, exclude=(), **kwargs):
        if record is not None:
            record.append(list(exclude))
        if not ok:
            return {"ok": False, "content": None, "provider_used": None,
                    "errors": ["no enabled provider available"], "attempts": []}
        validated = validator(content)
        return {
            "ok": validated is not None, "content": validated,
            "provider_used": provider, "tier": "primary", "model": "stub",
            "latency_ms": 1.0, "attempts": [provider],
            "errors": [] if validated is not None else ["schema validation failed"],
        }
    return _run


def _install_cascade(monkeypatch, runner):
    from plugins.AiMarketAnalyst.backend.services import analysis_router

    monkeypatch.setattr(analysis_router, "analyze_with_cascade", runner)


_GOOD_PREDICTION = {
    "verdict": "bullish",
    "confidence": 0.72,
    "horizon_hours": 12,
    "rationale": "Trend is up on every timeframe and the news flow is supportive.",
    "key_levels": {"support": 100.0, "resistance": 130.0, "invalidation": 98.0},
    "sources": ["https://example.com/n"],
}


async def _job(db, **kw) -> SignalResearchJob:
    base = dict(symbol="XAUUSD", source="telegram", signal_ref="telegram:1",
                direction="buy", entry=120.0, stop_loss=115.0, take_profit=135.0)
    base.update(kw)
    job = SignalResearchJob(status="queued", progress=0.0, steps=[],
                            queued_at=datetime.utcnow(), **base)
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job


# ── The pipeline ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_signal_is_researched_into_a_prediction(monkeypatch, db):
    _install_cascade(monkeypatch, _cascade_stub(_GOOD_PREDICTION))
    job = await _job(db)

    result = await signal_research.research_signal(db, job)

    assert result["status"] == "done"
    assert result["verdict"] == "bullish"
    assert job.progress == 1.0
    # Every declared step ran, in order.
    assert [s["name"] for s in job.steps] == list(signal_research.STEPS)

    finding = (await db.execute(select(ResearchFinding))).scalars().one()
    assert finding.kind == "prediction"
    assert finding.symbol == "XAUUSD"
    assert finding.speculative is False
    assert job.finding_id == finding.id


@pytest.mark.asyncio
async def test_a_dead_news_feed_still_yields_a_prediction(monkeypatch, db):
    async def _boom(symbol, *, deep=2):
        raise RuntimeError("feed down")

    monkeypatch.setattr(signal_research, "_web_news", _boom)
    _install_cascade(monkeypatch, _cascade_stub({**_GOOD_PREDICTION, "sources": []}))
    job = await _job(db)

    result = await signal_research.research_signal(db, job)

    assert result["status"] == "done"
    news_step = next(s for s in job.steps if s["name"] == "web_news")
    assert news_step["status"] == "error"
    assert "feed down" in news_step["detail"]


@pytest.mark.asyncio
async def test_a_sourceless_prediction_is_speculative_and_cannot_gate(monkeypatch, db):
    # The model cites nothing, and nothing it was shown carried a URL either.
    async def _no_urls(symbol, *, deep=2):
        return ("# Live web news\n- nothing", [])

    monkeypatch.setattr(signal_research, "_web_news", _no_urls)
    _install_cascade(monkeypatch, _cascade_stub({**_GOOD_PREDICTION, "sources": []}))
    job = await _job(db)

    await signal_research.research_signal(db, job)

    assert job.speculative is True
    finding = (await db.execute(select(ResearchFinding))).scalars().one()
    assert finding.speculative is True
    # Shown on the page, but excluded from anything that can gate a trade.
    assert await research_loop.gating_findings(db, symbol="XAUUSD") == []
    assert len(await research_loop.active_findings(db, symbol="XAUUSD")) == 1


@pytest.mark.asyncio
async def test_a_model_that_ignores_the_schema_fails_the_job(monkeypatch, db):
    _install_cascade(monkeypatch, _cascade_stub("not json at all"))
    job = await _job(db)

    result = await signal_research.research_signal(db, job)

    assert result["status"] == "failed"
    assert (await db.execute(select(ResearchFinding))).scalars().all() == []


def test_the_validator_rejects_anything_that_could_corrupt_a_row():
    v = signal_research.validate_prediction
    assert v('{"verdict": "sideways", "confidence": 0.5, "rationale": "x"}') is None
    assert v('{"verdict": "bullish", "confidence": 0.5}') is None          # no rationale
    assert v("plain text") is None
    assert v(None) is None

    # Fenced JSON is normal model output, not a schema violation.
    ok = v('```json\n{"verdict": "bearish", "confidence": 1.4, "horizon_hours": 9999, '
           '"rationale": "r", "sources": ["not-a-url", "https://a.test/x"]}\n```')
    assert ok["verdict"] == "bearish"
    assert ok["confidence"] == 1.0            # clamped
    assert ok["horizon_hours"] == 168         # clamped
    assert ok["sources"] == ["https://a.test/x"]   # unresolvable source dropped


def test_json_wrapped_in_prose_is_not_a_schema_violation():
    """Providers that ignore json_mode still produce usable output.

    Rejecting these costs a full cascade retry — and in practice it was the
    single largest source of failed jobs.
    """
    v = signal_research.validate_prediction

    prose = v('Here is my analysis:\n{"verdict": "neutral", "confidence": 0.4, '
              '"rationale": "Mixed signals."}\nHope that helps!')
    assert prose is not None and prose["verdict"] == "neutral"

    # Braces inside strings must not end the object early.
    nested = v('{"verdict": "bullish", "confidence": 0.6, '
               '"rationale": "Breakout above {resistance} held.", '
               '"key_levels": {"support": 1.2, "resistance": 1.4}}')
    assert nested is not None
    assert nested["key_levels"] == {"support": 1.2, "resistance": 1.4}
    assert "{resistance}" in nested["rationale"]

    assert v("I would rather not say.") is None


# ── Batching by pair ─────────────────────────────────────────────────────────

def test_every_signal_on_a_pair_becomes_one_batch():
    """The point of batching: one job per instrument, not one per signal."""
    batches = signal_research.group_by_pair([
        {"source": "telegram", "signal_ref": "telegram:1", "symbol": "XAUUSD",
         "direction": "buy", "entry": 4000.0, "stop_loss": 3980.0, "take_profit": 4050.0},
        {"source": "telegram", "signal_ref": "telegram:2", "symbol": "XAUUSD",
         "direction": "buy", "entry": 4010.0, "stop_loss": 3985.0, "take_profit": 4060.0},
        {"source": "smc", "signal_ref": "smc:7", "symbol": "XAUUSD",
         "direction": "sell", "entry": 4020.0, "stop_loss": 4040.0, "take_profit": 3960.0},
        {"source": "core", "signal_ref": "core:3", "symbol": "EURUSD",
         "direction": "sell", "entry": 1.1, "stop_loss": 1.12, "take_profit": 1.05},
    ])

    by_symbol = {b["symbol"]: b for b in batches}
    assert set(by_symbol) == {"XAUUSD", "EURUSD"}

    gold = by_symbol["XAUUSD"]
    assert len(gold["signals"]) == 3
    assert sorted(gold["signal_refs"]) == ["smc:7", "telegram:1", "telegram:2"]
    assert gold["signal_ref"] == "pair:XAUUSD"
    assert gold["direction"] == "buy"          # 2 buys vs 1 sell
    assert gold["source"] == "smc+telegram"    # both origins recorded


def test_a_pair_whose_signals_split_evenly_has_no_consensus():
    """A split is information, not an error — the research resolves it."""
    batch = signal_research.group_by_pair([
        {"source": "telegram", "signal_ref": "telegram:1", "symbol": "BTCUSD",
         "direction": "buy", "entry": 100.0},
        {"source": "smc", "signal_ref": "smc:1", "symbol": "BTCUSD",
         "direction": "sell", "entry": 102.0},
    ])[0]
    assert batch["direction"] is None


@pytest.mark.asyncio
async def test_a_new_signal_on_the_pair_overrides_the_cooldown(db):
    """New information is exactly what research is for."""
    job = await signal_research.enqueue(
        db, symbol="XAUUSD", source="telegram", signal_ref="pair:XAUUSD",
        signal_refs=["telegram:1"], signals=[{"source": "telegram", "direction": "buy"}],
    )
    job.status = "done"
    job.finished_at = datetime.utcnow()
    await db.commit()

    # Same signals → still cooling down.
    assert await signal_research.enqueue(
        db, symbol="XAUUSD", source="telegram", signal_ref="pair:XAUUSD",
        signal_refs=["telegram:1"],
    ) is None

    # A signal the last run never saw → research it again now.
    assert await signal_research.enqueue(
        db, symbol="XAUUSD", source="telegram", signal_ref="pair:XAUUSD",
        signal_refs=["telegram:1", "telegram:2"],
    ) is not None


@pytest.mark.asyncio
async def test_a_batch_is_researched_into_two_entries(monkeypatch, db):
    _install_cascade(monkeypatch, _cascade_stub({
        **_GOOD_PREDICTION,
        "entries": [
            {"label": "primary", "side": "buy", "entry": 100.0, "stop_loss": 96.0,
             "take_profit": 112.0, "rr": 99, "confidence": 0.7,
             "trigger": "retest of the 4h order block"},
            {"label": "secondary", "side": "buy", "entry": 97.0, "stop_loss": 94.0,
             "take_profit": 109.0, "confidence": 0.5},
        ],
    }))
    job = await _job(
        db, signal_ref="pair:XAUUSD", signal_count=3,
        signals=[
            {"source": "telegram", "direction": "buy", "entry": 100.0},
            {"source": "telegram", "direction": "buy", "entry": 101.0},
            {"source": "smc", "direction": "sell", "entry": 102.0},
        ],
    )

    await signal_research.research_signal(db, job)

    assert job.status == "done"
    assert [e["label"] for e in job.entries] == ["primary", "secondary"]
    primary = job.entries[0]
    assert primary["side"] == "buy"
    assert primary["stop_loss"] < primary["entry"] < primary["take_profit"]
    # rr is recomputed, never taken from the model: 12 reward / 4 risk = 3.0
    assert primary["rr"] == 3.0

    # The batch's signals reached the model.
    step = next(s for s in job.steps if s["name"] == "load_signal")
    assert "3 signal(s)" in step["detail"]


def test_the_gate_holds_out_for_two_entries():
    """One entry sends the cascade to the next provider, like a dead request."""
    gate = signal_research._TwoEntryGate()
    two = {
        **_GOOD_PREDICTION,
        "entries": [
            {"label": "primary", "side": "buy", "entry": 100.0, "stop_loss": 96.0,
             "take_profit": 112.0},
            {"label": "secondary", "side": "buy", "entry": 97.0, "stop_loss": 94.0,
             "take_profit": 109.0},
        ],
    }
    one = {
        **_GOOD_PREDICTION,
        "entries": [
            {"label": "primary", "side": "buy", "entry": 100.0, "stop_loss": 96.0,
             "take_profit": 112.0},
        ],
    }

    assert gate(one) is None, "a single entry is rejected, not accepted"
    assert len(gate.fallback["entries"]) == 1, "but it is remembered"
    assert gate(two) is not None, "two entries pass"


def test_the_gate_keeps_the_fullest_partial_it_saw():
    """Two providers, two thin answers — keep the one carrying more."""
    gate = signal_research._TwoEntryGate()
    gate({**_GOOD_PREDICTION, "entries": []})
    gate({
        **_GOOD_PREDICTION,
        "entries": [{"label": "primary", "side": "buy", "entry": 100.0,
                     "stop_loss": 96.0, "take_profit": 112.0}],
    })
    gate({**_GOOD_PREDICTION, "entries": []})

    assert len(gate.fallback["entries"]) == 1


def test_a_malformed_response_is_not_remembered_as_a_partial():
    gate = signal_research._TwoEntryGate()
    assert gate({"verdict": "not-a-verdict"}) is None
    assert gate.fallback is None


@pytest.mark.asyncio
async def test_a_one_entry_answer_still_reaches_the_page(monkeypatch, db):
    """A thin read beats no read — and it must not cost a second cascade."""
    calls: list[list[str]] = []
    _install_cascade(monkeypatch, _cascade_stub({
        **_GOOD_PREDICTION,
        "entries": [
            {"label": "primary", "side": "buy", "entry": 100.0, "stop_loss": 96.0,
             "take_profit": 112.0, "confidence": 0.6},
        ],
    }, record=calls))

    job = await _job(db)
    await signal_research.research_signal(db, job)

    assert job.status == "done"
    assert [e["label"] for e in job.entries] == ["primary"]
    assert len(calls) == 1, "a stingy model must not trigger a second cascade"


def test_an_entry_whose_stop_is_on_the_wrong_side_is_dropped():
    """It would be executed. Dropping beats guessing at someone's risk."""
    v = signal_research.validate_prediction
    ok = v({
        **_GOOD_PREDICTION,
        "entries": [
            # Buy with the stop ABOVE the entry — broken, drop it.
            {"label": "primary", "side": "buy", "entry": 100.0,
             "stop_loss": 104.0, "take_profit": 112.0},
            {"label": "secondary", "side": "sell", "entry": 100.0,
             "stop_loss": 104.0, "take_profit": 88.0},
        ],
    })
    assert [e["label"] for e in ok["entries"]] == ["secondary"]
    # The prediction itself survives — only the unusable entry is discarded.
    assert ok["verdict"] == "bullish"

    assert v({**_GOOD_PREDICTION, "entries": "not a list"})["entries"] == []
    assert v({**_GOOD_PREDICTION, "entries": [{"side": "buy"}]})["entries"] == []


@pytest.mark.asyncio
async def test_the_plan_is_readable_by_pair_and_expires_with_its_horizon(monkeypatch, db):
    """The shared read side for the sniper engines and every signal page."""
    from datetime import timedelta

    _install_cascade(monkeypatch, _cascade_stub({
        **_GOOD_PREDICTION,
        "horizon_hours": 6,
        "entries": [
            {"label": "primary", "side": "buy", "entry": 100.0, "stop_loss": 96.0,
             "take_profit": 112.0},
        ],
    }))
    job = await _job(db, symbol="XAUUSD", signal_ref="pair:XAUUSD")
    await signal_research.research_signal(db, job)

    plan = await signal_research.latest_plan(db, "XAUUSD")
    assert plan is not None
    assert plan["verdict"] == "bullish"
    assert len(plan["entries"]) == 1
    assert plan["entries"][0]["rr"] == 3.0

    # Batch lookup is the same data, keyed by symbol.
    plans = await signal_research.plans_for(db, ["XAUUSD", "EURUSD"])
    assert set(plans) == {"XAUUSD"}

    # Past its own horizon it is withheld: a stale call still reads as
    # authoritative, which is worse than having none.
    job.finished_at = datetime.utcnow() - timedelta(hours=7)
    await db.commit()
    assert await signal_research.latest_plan(db, "XAUUSD") is None


# ── Provider policy ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_idle_providers_are_preferred_over_the_primary(monkeypatch, db):
    async def _idle(_db):
        return ["groq"]

    monkeypatch.setattr(research_loop, "idle_provider_labels", _idle)

    from plugins.AiMarketAnalyst.backend.services import analysis_router

    async def _ranked(_db):
        return [SimpleNamespace(label="openai"), SimpleNamespace(label="groq")]

    monkeypatch.setattr(analysis_router, "rank_providers", _ranked)

    seen: list[list[str]] = []
    _install_cascade(monkeypatch, _cascade_stub(_GOOD_PREDICTION, record=seen))

    await signal_research.research_signal(db, await _job(db))

    # The busy primary is excluded; only the idle provider is left in play.
    assert seen == [["openai"]]


@pytest.mark.asyncio
async def test_the_primary_is_used_once_the_idle_wait_expires(monkeypatch, db):
    async def _none_idle(_db):
        return []

    monkeypatch.setattr(research_loop, "idle_provider_labels", _none_idle)

    seen: list[list[str]] = []
    _install_cascade(monkeypatch, _cascade_stub(_GOOD_PREDICTION, record=seen))

    await signal_research.research_signal(db, await _job(db))

    # Nothing was idle, the wait expired (0s in tests) — no exclusions, so the
    # full cascade runs. A research queue that never predicts is worse.
    assert seen == [[]]


@pytest.mark.asyncio
async def test_it_falls_back_to_every_provider_when_the_idle_ones_are_dead(monkeypatch, db):
    """Idle-first is a courtesy, not a reason to give up.

    Misconfigured providers were consuming the whole cascade, so a run could
    fail having never reached one that works.
    """
    async def _idle(_db):
        return ["groq"]

    monkeypatch.setattr(research_loop, "idle_provider_labels", _idle)

    from plugins.AiMarketAnalyst.backend.services import analysis_router

    async def _ranked(_db):
        return [SimpleNamespace(label="openai"), SimpleNamespace(label="groq")]

    monkeypatch.setattr(analysis_router, "rank_providers", _ranked)

    calls: list[list[str]] = []

    async def _run(db_, messages, *, validator, exclude=(), **kwargs):
        calls.append(list(exclude))
        if exclude:   # the idle-only pass — every idle provider is 401
            return {"ok": False, "content": None, "provider_used": None,
                    "errors": ["groq: HTTP 401 — needs configuring"], "attempts": ["groq"]}
        return {"ok": True, "content": validator(_GOOD_PREDICTION),
                "provider_used": "openai", "attempts": ["openai"], "errors": []}

    monkeypatch.setattr(analysis_router, "analyze_with_cascade", _run)

    result = await signal_research.research_signal(db, await _job(db))

    assert result["status"] == "done"
    assert result["provider_used"] == "openai"
    # First pass held the busy provider back; second pass used everything.
    assert calls == [["openai"], []]


def test_a_failure_summary_names_what_a_person_has_to_fix():
    summary = signal_research._summarise_failure([
        "NVIDIA NIM: HTTP 401 — needs configuring",
        "GitHub Models: HTTP 401 — needs configuring",
        "Cohere: timeout after 12.0s",
        "Cerebras: schema validation failed",
    ])
    assert "2 provider(s) rejected our credentials" in summary
    assert "NVIDIA NIM" in summary and "GitHub Models" in summary
    # Transient trouble is reported, but kept apart from the actionable part.
    assert "timeout" in summary and "schema validation failed" in summary

    assert signal_research._summarise_failure([]) == "no prediction returned"


def test_a_retired_provider_is_not_reported_as_a_bad_key():
    """No key or URL change recovers a service the vendor switched off."""
    summary = signal_research._summarise_failure([
        "GitHub Models: retired upstream (HTTP 410)",
        "Groq: timeout after 30.0s",
    ])
    assert "retired by their vendor and will not come back" in summary
    assert "GitHub Models" in summary
    assert "rejected our credentials" not in summary


# ── Queue mechanics ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_same_signal_is_not_queued_twice_while_in_flight(db):
    first = await signal_research.enqueue(db, symbol="EURUSD", source="telegram",
                                          signal_ref="telegram:7")
    second = await signal_research.enqueue(db, symbol="EURUSD", source="telegram",
                                           signal_ref="telegram:7")
    assert first is not None
    assert second is None

    # Once it reaches a terminal status the pair may be researched again — the
    # news around it has moved on.
    first.status = "done"
    await db.commit()
    third = await signal_research.enqueue(db, symbol="EURUSD", source="telegram",
                                          signal_ref="telegram:7")
    assert third is not None


@pytest.mark.asyncio
async def test_a_researched_signal_is_left_alone_until_its_cooldown_expires(db):
    """The runaway lock.

    Without a cooldown the sweep re-queues everything it can still see on every
    scan, so the same handful of signals get researched over and over — hundreds
    of model calls for no new information, which rate-limits every provider.
    """
    from datetime import timedelta

    job = await signal_research.enqueue(db, symbol="XAUUSD", source="smc",
                                        signal_ref="smc:1")
    job.status = "done"
    job.finished_at = datetime.utcnow()
    await db.commit()

    assert await signal_research.enqueue(db, symbol="XAUUSD", source="smc",
                                         signal_ref="smc:1") is None

    # A person asking for it explicitly still gets it.
    forced = await signal_research.enqueue(db, symbol="XAUUSD", source="smc",
                                           signal_ref="smc:1", force=True)
    assert forced is not None
    # Age every finished run for this signal, not just the newest — the
    # cooldown is judged on the most recent one.
    stale = datetime.utcnow() - timedelta(
        hours=signal_research.RESEARCH_COOLDOWN_HOURS + 1
    )
    forced.status = "done"
    forced.finished_at = stale
    job.finished_at = stale
    await db.commit()

    # Once the news has had time to move, it is fair game again.
    assert await signal_research.enqueue(db, symbol="XAUUSD", source="smc",
                                         signal_ref="smc:1") is not None


@pytest.mark.asyncio
async def test_a_failure_is_retried_sooner_than_a_success(db):
    from datetime import timedelta

    job = await signal_research.enqueue(db, symbol="EURUSD", source="smc",
                                        signal_ref="smc:9")
    job.status = "failed"
    job.finished_at = datetime.utcnow() - timedelta(
        minutes=signal_research.RETRY_COOLDOWN_MINUTES + 1
    )
    await db.commit()

    # Well inside the 6h success cooldown, but past the short retry window.
    assert await signal_research.enqueue(db, symbol="EURUSD", source="smc",
                                         signal_ref="smc:9") is not None


@pytest.mark.asyncio
async def test_the_sweep_stops_adding_to_a_backlog_it_cannot_clear(monkeypatch, db):
    async def _many(_db, *, limit=60):
        return [
            {"source": "smc", "signal_ref": f"smc:{i}", "symbol": f"SYM{i}USD",
             "direction": "buy", "entry": 1.0, "stop_loss": None, "take_profit": None}
            for i in range(200)
        ]

    monkeypatch.setattr(signal_research, "collect_pending_signals", _many)

    queued = await signal_research.scan_and_enqueue(db)
    assert queued == signal_research.MAX_QUEUE_DEPTH

    # A second sweep against a full queue adds nothing at all.
    assert await signal_research.scan_and_enqueue(db) == 0


@pytest.mark.asyncio
async def test_claim_never_hands_out_more_than_the_free_slots(db):
    for i in range(12):
        await signal_research.enqueue(db, symbol=f"SYM{i}USD", source="smc",
                                      signal_ref=f"smc:{i}")

    claimed = await signal_research._claim(db, 5)
    assert len(claimed) == 5

    # The claim is what enforces the cap: the rest stay queued and untouched.
    still_queued = (await db.execute(
        select(SignalResearchJob).where(SignalResearchJob.status == "queued")
    )).scalars().all()
    assert len(still_queued) == 7

    # A second claim with no free slots hands out nothing.
    assert await signal_research._claim(db, 0) == []


@pytest.mark.asyncio
async def test_jobs_abandoned_by_a_dead_worker_are_reclaimed(db):
    """Claims live in the database, so a reload would otherwise strand them.

    Without this sweep the row stays ``researching`` forever: nothing is working
    it, and the queue reports slots in use that no process owns.
    """
    from datetime import timedelta

    await signal_research.enqueue(db, symbol="BTCUSD", source="smc", signal_ref="smc:1")
    claimed = await signal_research._claim(db, 1)
    assert len(claimed) == 1

    # Fresh at first — a job that just started is not stale.
    assert await signal_research.requeue_stale(db, older_than_minutes=15) == 0

    job = await db.get(SignalResearchJob, claimed[0])
    job.started_at = datetime.utcnow() - timedelta(minutes=30)
    job.progress = 0.4
    await db.commit()

    assert await signal_research.requeue_stale(db, older_than_minutes=15) == 1
    await db.refresh(job)
    assert job.status == "queued"
    assert job.started_at is None
    assert job.progress == 0.0

    # A starting queue owns nothing, so a 0-minute sweep reclaims everything.
    await signal_research._claim(db, 1)
    assert await signal_research.requeue_stale(db, older_than_minutes=0) == 1


@pytest.mark.asyncio
async def test_stopping_the_queue_cancels_the_jobs_it_started():
    """Otherwise a reload waits on them and the process looks hung.

    Each in-flight job holds a session and up to a minute of network calls, so
    an uncancelled one keeps the event loop alive well past shutdown — the port
    stays bound, nothing serves, and it reads as a crash.
    """
    async def _forever():
        await asyncio.sleep(3600)

    signal_research._running = True
    signal_research._worker_task = asyncio.create_task(_forever())
    signal_research._scan_task = asyncio.create_task(_forever())
    job = asyncio.create_task(_forever())
    signal_research._inflight.add(job)

    try:
        assert signal_research.stop_signal_research_queue() is True
        await asyncio.sleep(0)
        assert job.cancelled() or job.done()
        assert signal_research._inflight == set()
    finally:
        for t in (job,):
            t.cancel()
        signal_research._running = False
        signal_research._inflight.clear()


@pytest.mark.asyncio
async def test_no_more_than_five_signals_are_researched_at_a_time(monkeypatch, db):
    """The concurrency contract, measured on the real worker loop."""
    peak = 0
    live = 0

    async def _slow_job(job_id: int):
        nonlocal peak, live
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.05)
        live -= 1

    monkeypatch.setattr(signal_research, "_run_job", _slow_job)
    monkeypatch.setattr(signal_research, "_POLL_S", 0.01)
    monkeypatch.setattr(signal_research, "_STARTUP_GRACE_S", 0.0)

    for i in range(20):
        await signal_research.enqueue(db, symbol=f"SYM{i}USD", source="smc",
                                      signal_ref=f"smc:{i}")

    # The worker opens its own sessions; point it at this test's engine.
    session_factory = async_sessionmaker(db.bind, expire_on_commit=False)

    class _Db:
        AsyncSessionLocal = session_factory

    monkeypatch.setitem(sys.modules, "app.core.database", _Db)

    signal_research._running = True
    signal_research._concurrency = 5
    worker = asyncio.create_task(signal_research._worker_loop())
    try:
        await asyncio.sleep(0.3)
    finally:
        # Let the loop exit on its own rather than cancelling it: a cancel
        # landing inside `async with AsyncSessionLocal()` leaves an aiosqlite
        # connection to be rolled back outside its greenlet at teardown.
        signal_research._running = False
        try:
            await asyncio.wait_for(worker, timeout=2)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            worker.cancel()
        # Drain the jobs the worker spawned before the fixture tears the engine
        # down underneath them.
        if signal_research._inflight:
            await asyncio.gather(*list(signal_research._inflight),
                                 return_exceptions=True)
        signal_research._inflight.clear()

    assert peak > 0, "the worker never claimed anything"
    assert peak <= 5, f"{peak} jobs ran at once — the concurrency cap leaked"


# ── What the JARVIS brains are taught ────────────────────────────────────────

def test_the_brains_are_taught_the_entries_not_just_a_headline_count():
    """The old fan-out summarised a whole tick as "Research tick: N finding(s)"
    with an EMPTY symbol, so the costed entries — the entire deliverable — never
    reached the brains, and nothing learned could be recalled by instrument."""
    captured = {}

    class _Job:
        symbol = "XAUUSD"
        signal_count = 3
        source = "telegram+smc"

    def _fake_learn(**kwargs):
        captured.update(kwargs)

    import app.api.jarvis as jarvis_api

    original = jarvis_api.jarvis_learn_all_brains
    jarvis_api.jarvis_learn_all_brains = _fake_learn
    try:
        signal_research._learn_prediction(
            _Job(),
            {"verdict": "bearish", "confidence": 0.72, "horizon_hours": 12,
             "rationale": "Structure and news both point lower.",
             "key_levels": {"support": 4000, "resistance": 4100, "invalidation": 4120}},
            [{"label": "primary", "side": "sell", "entry": 4080.0, "stop_loss": 4100.0,
              "take_profit": 4020.0, "rr": 3.0, "confidence": 0.7,
              "trigger": "retest of the 4h order block"}],
            ["https://example.com/a"],
            "NVIDIA NIM",
        )
    finally:
        jarvis_api.jarvis_learn_all_brains = original

    # Scoped to the instrument, so it is recallable by symbol.
    assert captured["symbol"] == "XAUUSD"
    assert captured["kind"] == "prediction"
    # The levels a person would actually act on are in the record.
    for fragment in ("4080", "4100", "4020", "3.0R", "SELL"):
        assert fragment in captured["detail"], f"{fragment!r} missing from the brain record"
    # And the reconciliation that produced them.
    assert "3 live signal(s)" in captured["detail"]
    assert "telegram+smc" in captured["detail"]
    assert "https://example.com/a" in captured["detail"]
    assert "bearish" in captured["tags"]


def test_a_speculative_prediction_is_learned_with_less_authority():
    """A call nothing verifiable backs must not be recalled as confidently as a
    sourced one."""
    weights = {}

    def _capture(key):
        def _fake(**kwargs):
            weights[key] = kwargs["importance"]
        return _fake

    class _Job:
        symbol = "EURUSD"
        signal_count = 1
        source = "telegram"

    pred = {"verdict": "bullish", "confidence": 0.8, "horizon_hours": 8,
            "rationale": "r", "key_levels": {}}

    import app.api.jarvis as jarvis_api
    original = jarvis_api.jarvis_learn_all_brains
    try:
        jarvis_api.jarvis_learn_all_brains = _capture("sourced")
        signal_research._learn_prediction(_Job(), pred, [], ["https://a.test/x"], "Groq")
        jarvis_api.jarvis_learn_all_brains = _capture("speculative")
        signal_research._learn_prediction(_Job(), pred, [], [], "Groq")
    finally:
        jarvis_api.jarvis_learn_all_brains = original

    assert weights["speculative"] < weights["sourced"]
