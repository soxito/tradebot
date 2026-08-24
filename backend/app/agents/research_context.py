"""
Research context — what the /research page knows, handed to the agents.

The research loop already collects the economic calendar, news, sentiment and
per-signal trade plans into ``mt5_research_findings``; the Research page reads
them, but the trading room never did. Agents were reasoning from price alone
while a high-impact NFP print sat unread in the same database.

This module is the bridge. Everything is lazily imported and guarded so a
deploy without the MT5 plugin still boots and simply contributes nothing.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

# Keep the prompt payload small — agents pay per token, and a wall of headlines
# crowds out the price action they also need to see.
MAX_FINDINGS = 8
MAX_EVENTS = 6


def _research_loop():
    try:
        from plugins.MT5TradingPlugin.backend.services import research_loop

        return research_loop
    except Exception as exc:  # noqa: BLE001 - plugin-optional
        logger.debug(f"[research-context] research_loop unavailable: {exc}")
        return None


def _signal_research():
    try:
        from plugins.MT5TradingPlugin.backend.services import signal_research

        return signal_research
    except Exception as exc:  # noqa: BLE001 - plugin-optional
        logger.debug(f"[research-context] signal_research unavailable: {exc}")
        return None


def _economic_calendar():
    try:
        from plugins.MT5TradingPlugin.backend.services import economic_calendar

        return economic_calendar
    except Exception as exc:  # noqa: BLE001 - plugin-optional
        logger.debug(f"[research-context] economic_calendar unavailable: {exc}")
        return None


def _finding_brief(row: Any) -> Dict[str, Any]:
    """A finding trimmed to what actually changes a trading decision."""
    return {
        "kind": row.kind,
        "headline": row.headline,
        "confidence": round(float(row.confidence or 0.0), 2),
        # Speculative findings carry no source URL. They are passed through so
        # the agent can weigh them, but flagged so they never read as fact.
        "speculative": bool(row.speculative),
        "source": row.source,
        "published_at": row.published_at.isoformat() if row.published_at else None,
    }


async def gather_research(
    db: AsyncSession,
    symbol: str,
    *,
    include_calendar: bool = True,
) -> Dict[str, Any]:
    """Everything the research subsystem knows about one instrument.

    Never raises: research is an enrichment, and a failure here must not take
    down an analysis that can still run on price and sentiment alone.
    """
    out: Dict[str, Any] = {"available": False}

    loop = _research_loop()
    if loop is not None:
        try:
            rows = await loop.active_findings(db, symbol=symbol, limit=MAX_FINDINGS)
            findings = [_finding_brief(r) for r in rows]
            out["findings"] = findings
            out["verified_count"] = sum(1 for f in findings if not f["speculative"])
            out["speculative_count"] = sum(1 for f in findings if f["speculative"])
            out["available"] = True
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[research-context] findings failed for {symbol}: {exc}")

    sr = _signal_research()
    if sr is not None:
        try:
            plan = await sr.latest_plan(db, symbol)
            if plan:
                # The reconciled verdict across every live signal on the pair —
                # the single most decision-relevant thing research produces.
                out["plan"] = plan
                out["available"] = True
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[research-context] plan failed for {symbol}: {exc}")

    if include_calendar:
        cal = _economic_calendar()
        if cal is not None:
            try:
                # upcoming_fomo is the calendar's own symbol-aware lookup: the
                # high-impact prints near enough to matter for this instrument.
                rows = await cal.upcoming_fomo(limit=MAX_EVENTS, symbol=symbol)
                events = [_event_brief(r) for r in (rows or [])]
                if events:
                    out["calendar"] = events
                    out["available"] = True
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[research-context] calendar failed for {symbol}: {exc}")

    return out


def _event_brief(row: Dict[str, Any]) -> Dict[str, Any]:
    """One calendar print, trimmed to what an agent needs to time around it."""
    when = row.get("event_time") or row.get("date") or row.get("time")
    return {
        "title": row.get("title") or row.get("event"),
        "currency": row.get("currency"),
        "impact": (row.get("impact") or "unknown"),
        "hours_away": row.get("hours_away"),
        "at": when.isoformat() if hasattr(when, "isoformat") else when,
    }


def summarise_for_prompt(research: Dict[str, Any]) -> Optional[str]:
    """Render the research block as compact text for an agent's prompt.

    Returns None when there is nothing to say, so callers can leave the prompt
    untouched rather than injecting an empty "RESEARCH: none" section that
    still costs tokens and invites the model to comment on it.
    """
    if not research or not research.get("available"):
        return None

    lines: List[str] = []

    plan = research.get("plan")
    if plan:
        verdict = plan.get("verdict") if isinstance(plan, dict) else None
        if verdict:
            lines.append(f"Research verdict: {verdict}")
        bias = plan.get("bias") if isinstance(plan, dict) else None
        if bias:
            lines.append(f"Research bias: {bias}")

    events = research.get("calendar") or []
    if events:
        lines.append("Upcoming high-impact events:")
        for e in events:
            away = e.get("hours_away")
            when = f"in {away}h" if away is not None else f"@ {e.get('at')}"
            lines.append(f"  - [{e.get('impact')}] {e.get('currency') or ''} {e.get('title')} {when}")

    findings = research.get("findings") or []
    if findings:
        lines.append("Recent research findings:")
        for f in findings:
            tag = "SPECULATIVE" if f["speculative"] else f"conf {f['confidence']}"
            lines.append(f"  - ({f['kind']}, {tag}) {f['headline']}")

    if not lines:
        return None

    lines.append(
        "Weigh these against the price action. Speculative items may inform "
        "sizing or timing but must not be the sole reason for a trade."
    )
    return "\n".join(lines)
