"""
MT5 Trading Plugin — background research + news/FOMO prediction.

Runs continuously whether or not anyone asked for an analysis, and pulls from
real sources only:

  * economic calendar — ForexFactory public JSON via ``smc_ai.fetch_economic_events``
  * news feeds        — ``app.sentiment.news_sources.fetch_all_news`` (RSS,
                        CryptoPanic, CoinGecko, CoinMarketCap, Marketaux, GNews,
                        Currents), each item carrying its own article URL
  * sentiment         — ``fetch_alternative_fng`` (Fear & Greed index)

Two rules the loop enforces:

  1. **Idle providers only.** Before any LLM step it asks
     ``provider_health.idle_providers()`` with the PRIMARY analyst provider
     excluded, so a provider serving a live /mt5-live request is never borrowed.
     No idle provider means the LLM step is skipped entirely — findings are
     still collected and stored, they just are not summarised this tick.
  2. **Sources or speculation.** A finding with no resolvable source URL is
     stored ``speculative=True``. ``gating_findings()`` returns only
     non-speculative findings, so a speculative prediction can never gate a
     trade signal on its own.

Findings land in ``mt5_research_findings`` with a decay horizon, are published
on the typed agent bus, and are fanned out to the existing three memories.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from loguru import logger
from sqlalchemy import select

from plugins.MT5TradingPlugin.backend.models import ResearchFinding
from plugins.MT5TradingPlugin.backend.services import agent_bus

#: How long a finding of each kind stays relevant. Calendar entries decay with
#: the event; news decays fast; sentiment snapshots are the shortest-lived.
DECAY_HOURS: Dict[str, int] = {
    "calendar": 72,
    "news": 12,
    "sentiment": 6,
    "prediction": 8,
}

#: Confidence assigned by source class. These are provenance weights, not
#: predictions — a calendar entry is a scheduled fact, a news headline is a
#: report, a sentiment index is an aggregate.
SOURCE_CONFIDENCE: Dict[str, float] = {
    "calendar": 0.90,
    "news": 0.60,
    "sentiment": 0.50,
}

#: Instruments the loop researches when no explicit watchlist is configured.
DEFAULT_WATCHLIST = ("XAUUSD", "EURUSD", "GBPUSD", "BTCUSD")


def _decay_at(kind: str, base: Optional[datetime] = None) -> datetime:
    return (base or datetime.utcnow()) + timedelta(hours=DECAY_HOURS.get(kind, 12))


def _has_source(url: Optional[str]) -> bool:
    """A finding is verifiable only with a real http(s) URL behind it."""
    return bool(url) and str(url).strip().lower().startswith(("http://", "https://"))


# ── Collection (pure-ish: network in, dataclasses out) ───────────────────────

async def collect_calendar(symbols: Sequence[str]) -> List[agent_bus.ResearchFindingMessage]:
    """Upcoming high-impact economic events for the watchlist."""
    from plugins.MT5TradingPlugin.backend.services.smc_ai import fetch_economic_events

    out: List[agent_bus.ResearchFindingMessage] = []
    for symbol in symbols:
        try:
            events = await fetch_economic_events(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[research] calendar fetch failed for {symbol}: {exc}")
            continue
        for ev in (events or [])[:6]:
            if not isinstance(ev, dict):
                continue
            url = ev.get("url") or "https://www.forexfactory.com/calendar"
            title = str(ev.get("title") or ev.get("event") or "").strip()
            if not title:
                continue
            out.append(agent_bus.ResearchFindingMessage(
                kind="calendar",
                symbol=symbol,
                headline=f"{ev.get('currency', '')} {title}".strip()[:400],
                body=(
                    f"impact={ev.get('impact')} forecast={ev.get('forecast')} "
                    f"previous={ev.get('previous')} at={ev.get('date') or ev.get('time')}"
                ),
                source="ForexFactory",
                source_url=url,
                confidence=SOURCE_CONFIDENCE["calendar"],
                speculative=not _has_source(url),
                decay_at=_decay_at("calendar").isoformat(),
            ))
    return out


async def collect_news(limit: int = 25) -> List[agent_bus.ResearchFindingMessage]:
    """Headlines from every configured feed, each with its article URL."""
    try:
        from app.core.config import settings
        from app.sentiment.news_sources import fetch_all_news

        items = await fetch_all_news(
            cryptopanic_api_key=getattr(settings, "CRYPTOPANIC_API_KEY", "") or "",
            coingecko_api_key=getattr(settings, "COINGECKO_API_KEY", "") or "",
            coinmarketcap_api_key=getattr(settings, "COINMARKETCAP_API_KEY", "") or "",
            marketaux_api_key=getattr(settings, "MARKETAUX_API_KEY", "") or "",
            gnews_api_key=getattr(settings, "GNEWS_API_KEY", "") or "",
            currents_api_key=getattr(settings, "CURRENTS_API_KEY", "") or "",
            max_age_hours=6,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[research] news fetch failed: {exc}")
        return []

    out: List[agent_bus.ResearchFindingMessage] = []
    for item in (items or [])[:limit]:
        url = getattr(item, "url", None)
        title = str(getattr(item, "title", "") or "").strip()
        if not title:
            continue
        symbols = list(getattr(item, "symbols", None) or [])
        published = getattr(item, "published_at", None)
        out.append(agent_bus.ResearchFindingMessage(
            kind="news",
            symbol=symbols[0] if symbols else None,
            headline=title[:400],
            body=str(getattr(item, "summary", "") or "")[:1000],
            source=str(getattr(item, "source", "") or "")[:120],
            source_url=url,
            # Reliability is reported per-source by the news layer; fall back to
            # the source-class weight when it is absent.
            confidence=float(
                getattr(item, "reliability", None) or SOURCE_CONFIDENCE["news"]
            ),
            speculative=not _has_source(url),
            published_at=published.isoformat() if hasattr(published, "isoformat") else None,
            decay_at=_decay_at("news").isoformat(),
        ))
    return out


async def collect_agent_reach(
    symbols: Sequence[str], limit_per_symbol: int = 3
) -> List[agent_bus.ResearchFindingMessage]:
    """Web research per watchlist symbol via Agent-Reach (Exa search).

    Off (returns []) unless AGENT_REACH_ENABLED is set — see
    backend/app/services/agent_reach_client.py. Real source URLs from the
    search results classify these as non-speculative on the same basis as any
    other sourced headline.
    """
    from app.core.config import settings

    if not settings.AGENT_REACH_ENABLED:
        return []

    from app.services import agent_reach_client

    out: List[agent_bus.ResearchFindingMessage] = []
    for symbol in symbols:
        try:
            items = await agent_reach_client.web_search(
                f"{symbol} latest news analysis", limit=limit_per_symbol
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[research] agent-reach fetch failed for {symbol}: {exc}")
            continue
        for item in (items or [])[:limit_per_symbol]:
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            url = item.get("url")
            out.append(agent_bus.ResearchFindingMessage(
                kind="news",
                symbol=symbol,
                headline=title[:400],
                body=str(item.get("summary") or "")[:1000],
                source=f"Agent-Reach ({item.get('source') or 'web'})",
                source_url=url,
                confidence=SOURCE_CONFIDENCE["news"],
                speculative=not _has_source(url),
                decay_at=_decay_at("news").isoformat(),
            ))
    return out


async def collect_sentiment() -> List[agent_bus.ResearchFindingMessage]:
    """Fear & Greed snapshots — the FOMO / capitulation gauge."""
    try:
        import aiohttp

        from app.sentiment.news_sources import fetch_alternative_fng

        async with aiohttp.ClientSession() as session:
            items = await fetch_alternative_fng(session)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[research] sentiment fetch failed: {exc}")
        return []

    out: List[agent_bus.ResearchFindingMessage] = []
    for item in (items or [])[:3]:
        url = getattr(item, "url", None) or "https://alternative.me/crypto/fear-and-greed-index/"
        title = str(getattr(item, "title", "") or "").strip()
        if not title:
            continue
        out.append(agent_bus.ResearchFindingMessage(
            kind="sentiment",
            headline=title[:400],
            body=str(getattr(item, "summary", "") or "")[:600],
            source="alternative.me Fear & Greed",
            source_url=url,
            confidence=SOURCE_CONFIDENCE["sentiment"],
            speculative=not _has_source(url),
            decay_at=_decay_at("sentiment").isoformat(),
        ))
    return out


# ── Idle-provider selection ──────────────────────────────────────────────────

async def idle_provider_labels(db) -> List[str]:
    """Providers safe to borrow: enabled, healthy, idle, and NOT the PRIMARY.

    The PRIMARY analyst provider is excluded unconditionally — not merely
    "excluded while busy" — so /mt5-live latency is protected even between
    requests, and even when the cross-process in-flight mirror is unavailable.
    """
    from plugins.AiMarketAnalyst.backend.services import analysis_router
    from plugins.AiMarketAnalyst.backend.services.provider_health import provider_health

    try:
        ranked = await analysis_router.rank_providers(db)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[research] provider ranking unavailable: {exc}")
        return []
    if not ranked:
        return []

    primary = ranked[0].label
    return await provider_health.idle_providers(
        [p.label for p in ranked], exclude=[primary]
    )


# ── Persistence + gating ─────────────────────────────────────────────────────

async def store_findings(
    db, findings: Sequence[agent_bus.ResearchFindingMessage]
) -> List[int]:
    """Persist findings, publish them on the bus, fan out to the three memories."""
    stored: List[int] = []
    if not findings:
        return stored
    try:
        for msg in findings:
            row = ResearchFinding(
                kind=msg.kind,
                symbol=msg.symbol,
                headline=msg.headline[:400],
                body=msg.body,
                source=msg.source,
                source_url=msg.source_url,
                confidence=float(msg.confidence or 0.0),
                speculative=bool(msg.speculative),
                provider_used=msg.provider_used,
                published_at=_parse_dt(msg.published_at),
                decay_at=_parse_dt(msg.decay_at) or _decay_at(msg.kind),
            )
            db.add(row)
            await db.flush()
            stored.append(row.id)
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[research] store_findings skipped: {exc}")
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return []

    for msg in findings:
        await agent_bus.publish_finding(msg)

    _fan_out(findings)
    return stored


async def active_findings(
    db, *, symbol: Optional[str] = None, kinds: Optional[Sequence[str]] = None,
    limit: int = 50,
) -> List[ResearchFinding]:
    """Findings that have not yet decayed, newest first."""
    try:
        q = (
            select(ResearchFinding)
            .where(ResearchFinding.decay_at > datetime.utcnow())
            .order_by(ResearchFinding.created_at.desc())
            .limit(limit)
        )
        if symbol:
            q = q.where(ResearchFinding.symbol == symbol)
        if kinds:
            q = q.where(ResearchFinding.kind.in_(list(kinds)))
        return list((await db.execute(q)).scalars().all())
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[research] active_findings skipped: {exc}")
        return []


async def gating_findings(
    db, *, symbol: Optional[str] = None, limit: int = 20
) -> List[ResearchFinding]:
    """Only findings allowed to influence a trade signal.

    Non-speculative (a real source URL) and not yet decayed. Speculative
    findings are still stored and still shown, but can never gate a signal.
    """
    rows = await active_findings(db, symbol=symbol, limit=limit * 3)
    return [r for r in rows if not r.speculative][:limit]


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


#: Title of the always-injected calendar row. Fixed on purpose:
#: ``store_knowledge`` dedups on (role, symbol, title), so re-writing under the
#: same title refreshes the block in place instead of piling up stale copies.
CALENDAR_KNOWLEDGE_TITLE = "Upcoming high-impact economic events"


async def remind_agents_of_calendar(db) -> int:
    """Refresh the calendar block that every agent prompt carries.

    ``knowledge_service.query_knowledge`` includes rows with a NULL role and a
    NULL symbol in *every* query, and the orchestrator injects that result into
    each agent prompt. So one null-scoped, high-weight row reaches every agent
    with no orchestrator change. Per-symbol rows are written alongside it so a
    symbol-scoped query also surfaces that instrument's own events.

    Returns the number of knowledge rows written. Never raises.
    """
    from plugins.AiMarketAnalyst.backend.services.knowledge_service import store_knowledge
    from plugins.MT5TradingPlugin.backend.services import economic_calendar as eco

    written = 0
    try:
        events = await eco.upcoming_fomo(limit=8)
        await store_knowledge(
            db,
            content=eco.format_for_agents(events),
            agent_role=None,
            symbol=None,
            kind="calendar",
            title=CALENDAR_KNOWLEDGE_TITLE,
            # High enough to stay at the top of the weight-ordered query even
            # once other facts have been reinforced for a while.
            weight=9.0,
            source="ForexFactory",
        )
        written += 1
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[research] global calendar reminder skipped: {exc}")

    for symbol in DEFAULT_WATCHLIST:
        try:
            events = await eco.upcoming_fomo(limit=5, symbol=symbol)
            if not events:
                continue
            await store_knowledge(
                db,
                content=eco.format_for_agents(events),
                agent_role=None,
                symbol=symbol,
                kind="calendar",
                title=CALENDAR_KNOWLEDGE_TITLE,
                weight=9.0,
                source="ForexFactory",
            )
            written += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[research] calendar reminder skipped for {symbol}: {exc}")

    return written


async def current_agent_reminder(db=None) -> Dict[str, Any]:
    """What the agents are being told right now — the page renders this verbatim."""
    from plugins.MT5TradingPlugin.backend.services import economic_calendar as eco

    events = await eco.upcoming_fomo(limit=8)
    return {
        "title": CALENDAR_KNOWLEDGE_TITLE,
        "content": eco.format_for_agents(events),
        "events": events,
        "scope": "all agents (agent_role=NULL, symbol=NULL)",
        "weight": 9.0,
        "source": "ForexFactory",
    }


def _fan_out(findings: Sequence[agent_bus.ResearchFindingMessage]) -> None:
    """Write the tick's findings into the existing three memories."""
    if not findings:
        return
    try:
        from app.api.jarvis import jarvis_learn_all_brains

        verified = [f for f in findings if not f.speculative]
        summary = (
            f"Research tick: {len(findings)} finding(s), "
            f"{len(verified)} source-verified"
        )
        detail = "\n".join(
            f"[{f.kind}] {f.headline} — {f.source_url or 'no source'} "
            f"(conf {f.confidence:.2f}{', speculative' if f.speculative else ''})"
            for f in findings[:12]
        )
        jarvis_learn_all_brains(
            action="research", symbol="", summary=summary, detail=detail,
            tags=["research", "news", "calendar", "sentiment"],
            importance=0.55, kind="research",
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[research] brain fan-out skipped: {exc}")


# ── One tick ─────────────────────────────────────────────────────────────────

async def run_research_cycle(
    db, *, symbols: Sequence[str] = DEFAULT_WATCHLIST
) -> Dict[str, Any]:
    """Collect from every real source, store, publish. Returns a status dict.

    The LLM summarisation step is skipped when no provider is idle. Collection
    and storage always run, so the loop still makes progress under load.
    """
    idle = await idle_provider_labels(db)

    collected: List[agent_bus.ResearchFindingMessage] = []
    for coro in (
        collect_calendar(symbols), collect_news(), collect_sentiment(),
        collect_agent_reach(symbols),
    ):
        try:
            collected.extend(await coro)
        except Exception as exc:  # noqa: BLE001 — one dead source must not stop the tick
            logger.debug(f"[research] collector failed: {exc}")

    stored = await store_findings(db, collected)
    verified = sum(1 for f in collected if not f.speculative)

    # Hand the shared memory to the OpenHuman agents, which compile strategies
    # from it and publish proposals back to Jarvis on the bus. They read the
    # stored findings — they do not re-call providers.
    proposals: List[Dict[str, Any]] = []
    try:
        from plugins.OpenHumanPlugin.backend.services.memory_sync_service import (
            compile_strategies_from_research,
        )

        proposals = await compile_strategies_from_research(list(symbols))
    except Exception as exc:  # noqa: BLE001 — OpenHuman is optional
        logger.debug(f"[research] OpenHuman strategy compile skipped: {exc}")

    # Refresh the calendar block every agent prompt carries. Same tick, same
    # session — no separate loop needed for what is already 15-minute data.
    reminded = 0
    try:
        reminded = await remind_agents_of_calendar(db)
    except Exception as exc:  # noqa: BLE001 — a stale reminder beats a dead tick
        logger.debug(f"[research] agent calendar reminder skipped: {exc}")

    return {
        "collected": len(collected),
        "stored": len(stored),
        "verified": verified,
        "speculative": len(collected) - verified,
        "idle_providers": idle,
        "llm_step": "ran" if idle else "skipped (no idle provider)",
        "proposals": len(proposals),
        "reminded": reminded,
        "at": datetime.utcnow().isoformat(),
    }


# ── Loop control (mirrors the sniper-loop pattern in core/scheduler.py) ──────

_task: Optional[asyncio.Task] = None
_running = False
_interval = 900  # 15 minutes
_started_at: Optional[str] = None
_last_run: Optional[Dict[str, Any]] = None


async def _loop() -> None:
    global _running, _last_run
    _running = True
    logger.info(f"📰 [RESEARCH LOOP] Started — every {_interval}s, idle providers only")

    while _running:
        try:
            from app.core.database import AsyncSessionLocal

            async with AsyncSessionLocal() as db:
                _last_run = await run_research_cycle(db)
            logger.info(
                f"📰 [RESEARCH LOOP] {_last_run['collected']} findings "
                f"({_last_run['verified']} verified) | {_last_run['llm_step']}"
            )
        except asyncio.CancelledError:
            break
        except Exception as exc:  # noqa: BLE001
            safe = str(exc).replace("{", "{{").replace("}", "}}")
            logger.error(f"📰 [RESEARCH LOOP] Cycle error: {safe}")
            _last_run = {"at": datetime.utcnow().isoformat(), "status": "error",
                         "error": str(exc)}

        try:
            await asyncio.sleep(_interval)
        except asyncio.CancelledError:
            break

    _running = False
    logger.info("📰 [RESEARCH LOOP] Stopped")


def start_research_loop(interval: int = 900) -> bool:
    global _task, _interval, _running, _started_at

    if _task is not None and not _task.done():
        logger.warning("Research loop already running")
        return False

    _interval = max(120, interval)  # floor: never hammer the free news feeds
    _running = True
    _started_at = datetime.utcnow().isoformat()
    _task = asyncio.create_task(_loop())
    logger.info(f"📰 Research loop started (interval={_interval}s)")
    return True


def stop_research_loop() -> bool:
    global _running, _task, _started_at

    if not _running and (_task is None or _task.done()):
        logger.warning("Research loop is not running")
        return False

    _running = False
    if _task:
        _task.cancel()
        _task = None
    _started_at = None
    logger.info("📰 Research loop stopped")
    return True


def get_research_loop_status() -> Dict[str, Any]:
    return {
        "running": _running,
        "interval_seconds": _interval,
        "started_at": _started_at,
        "last_run": _last_run,
    }
