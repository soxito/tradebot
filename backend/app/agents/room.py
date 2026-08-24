"""
Trading Room — agent personas, live state registry and SSE event emitters.

The registry is process-local and intentionally ephemeral: it mirrors what the
agents are doing *right now* so a client that opens the room mid-session can
hydrate instantly instead of waiting for the next event.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from loguru import logger

from app.core.events import Topics, event_bus

# ── Personas ────────────────────────────────────────────────────────────────
# Human names + seat assignment for the 3D room. Keyed by agent role so the
# mapping survives renames of the DB rows.

CEO_PERSONA = {
    "role": "ceo",
    "human_name": "JARVIS",
    "title": "Chief Executive",
    "color": "#22d3ee",
    "seat": -1,
    "gender": "male",
}

# ``gender`` picks the body build and hair in the 3D room. It is only ever a
# rendering hint — nothing in the trading logic reads it.
PERSONAS: Dict[str, Dict[str, Any]] = {
    "market_analyst": {"human_name": "Sakhile", "title": "Market Analyst", "color": "#3b82f6", "seat": 0, "gender": "male"},
    "sentiment_analyst": {"human_name": "Lerato", "title": "Sentiment Analyst", "color": "#a855f7", "seat": 1, "gender": "female"},
    "signal_generator": {"human_name": "Naledi", "title": "Signal Generator", "color": "#22c55e", "seat": 2, "gender": "female"},
    "risk_manager": {"human_name": "Thabo", "title": "Risk Manager", "color": "#ef4444", "seat": 3, "gender": "male"},
    "trade_executor": {"human_name": "Puso", "title": "Trade Executor", "color": "#f97316", "seat": 4, "gender": "male"},
    "position_reviewer": {"human_name": "Kabelo", "title": "Position Reviewer", "color": "#eab308", "seat": 5, "gender": "male"},
    "strategy_optimizer": {"human_name": "Zanele", "title": "Strategy Optimizer", "color": "#14b8a6", "seat": 6, "gender": "female"},
}

# Starter standing instructions, one per role. These are a worked example of
# what the brief on the settings page is for: concrete, behavioural, and about
# *this desk's* judgement rather than the JSON contract the system prompt owns.
# They are only ever written into an empty brief, so nothing a user (or the
# self-improvement pass) has authored is overwritten.
DEFAULT_TASKS: Dict[str, str] = {
    "market_analyst": (
        "Lead with structure, not indicators. Name the higher-timeframe trend first, "
        "then say whether price is at a level where that trend can be traded — a swing "
        "high/low, an unmitigated order block, or a clean range edge.\n"
        "State support and resistance as levels you can point at on the chart, not "
        "round numbers. If price is mid-range with no level nearby, say so and call it "
        "neutral; a forced read is worse than no read.\n"
        "Flag divergence between price and volume explicitly. Treat a high-impact "
        "calendar print inside the next two hours as a reason to lower conviction.\n"
        "Weigh the BTC cycle context when present: a late-bull or bear season argues "
        "for lower conviction on longs and tighter invalidations; early-bull argues "
        "the opposite. Name the phase when it shaped your read."
    ),
    "sentiment_analyst": (
        "Separate what is being said from what is being done. A loud narrative with no "
        "follow-through in price is a fade candidate, not a confirmation.\n"
        "Weight sourced findings above speculative ones, and say which you are leaning "
        "on. Never let a single headline carry a call on its own.\n"
        "Call out crowding: when sentiment is unanimous, note the squeeze risk in the "
        "opposite direction. Age matters — discount anything older than a few hours "
        "unless it is a scheduled event still ahead."
    ),
    "signal_generator": (
        "Only produce a signal when the analyst's structure and the sentiment read "
        "point the same way, or when one is neutral. Genuine conflict means no trade.\n"
        "Every signal needs an invalidation level you can state before entry — where "
        "the idea is simply wrong. If you cannot name it, there is no signal.\n"
        "Target a reward-to-risk of at least 2:1 measured to the nearest opposing "
        "level, not to a hopeful extension. Prefer one high-quality setup over three "
        "marginal ones, and say plainly when the best action is to wait."
    ),
    "risk_manager": (
        "You are the brake, not the engine. Size from the stop distance and the real "
        "account equity in context — never a fixed lot.\n"
        "Reject anything that breaches the room's risk limits, correlates heavily with "
        "an open position, or lands inside a high-impact news window. Correlated pairs "
        "in the same direction are one position, not two.\n"
        "State the exact monetary risk and the resulting position size. If the stop is "
        "so wide that correct sizing rounds to nothing, reject the trade and say why."
    ),
    "trade_executor": (
        "Translate an approved signal into a precise order and nothing more. You do not "
        "revisit the thesis; you check it is still executable.\n"
        "Verify the entry is still valid at current price — if price has already run to "
        "the target or through the invalidation, stand down and say so.\n"
        "Always attach both stop loss and take profit. Prefer limit entries at the "
        "level over chasing; note the spread when it is wide enough to matter."
    ),
    "position_reviewer": (
        "Judge open positions on whether the original reason still holds, not on the "
        "current PnL. A profitable trade whose thesis has broken should still be closed.\n"
        "Recommend moving the stop to break-even once price has travelled roughly the "
        "initial risk in your favour. Never widen a stop to avoid a loss.\n"
        "Explicitly name the invalidation that would make you exit now, and re-check it "
        "against fresh news each review rather than assuming it still stands."
    ),
    "strategy_optimizer": (
        "Look for repeatable patterns across recent decisions, not one-off explanations "
        "for individual losses.\n"
        "Compare win rate by setup type, session and pair, and say which combinations "
        "are actually carrying performance. Recommend cutting what consistently loses.\n"
        "Check calibration: if confidence is no higher on winners than on losers, that "
        "is the first thing to fix. Prefer one specific, testable change per review "
        "over a list of adjustments nobody can evaluate."
    ),
    "ceo": (
        "Chair the desk. Weigh each seat's read by its track record, not its volume — "
        "a quiet analyst who is usually right outranks a loud one who is not.\n"
        "Only greenlight a trade when structure, sentiment and risk agree, or when the "
        "dissent is on timing rather than direction. State the single reason the trade "
        "lives or dies, and the level that proves it wrong.\n"
        "When the board is split or the sample is thin, hold and say what evidence would "
        "change your mind. Protecting capital outranks catching every move."
    ),
}


# Pipeline phase each role belongs to — drives the room's progress ring.
ROLE_PHASE: Dict[str, int] = {
    "market_analyst": 1,
    "sentiment_analyst": 1,
    "signal_generator": 2,
    "risk_manager": 3,
    "trade_executor": 4,
    "position_reviewer": 5,
    "strategy_optimizer": 5,
}


# Overrides loaded from room_agent_profiles, keyed by role. Refreshed whenever
# the settings page saves, so the 3D room and the emitters agree on who is who.
_persona_overrides: Dict[str, Dict[str, Any]] = {}


def set_persona_overrides(overrides: Dict[str, Dict[str, Any]]) -> None:
    global _persona_overrides
    _persona_overrides = overrides


def persona_for(role: str) -> Dict[str, Any]:
    """Persona for a role: user override first, then the built-in default."""
    base = (
        PERSONAS.get(role)
        or (CEO_PERSONA if role == CEO_PERSONA["role"] else None)
        or {
            "human_name": role.replace("_", " ").title(),
            "title": role.replace("_", " ").title(),
            "color": "#94a3b8",
            "seat": 7,
            "gender": "male",
        }
    )
    return {"role": role, **base, **_persona_overrides.get(role, {})}


# ── Live state registry ─────────────────────────────────────────────────────

IDLE = "idle"
ANALYZING = "analyzing"
PRESENTING = "presenting"
RESTING = "resting"
ERROR = "error"

_MAX_SESSIONS = 40

_agent_state: Dict[str, Dict[str, Any]] = {}
_sessions: List[Dict[str, Any]] = []
# Pairs the room is pinned to. Empty means free roaming. Order is preserved so
# the worker rotates through them predictably.
_focus_symbols: List[str] = []


def _set_state(role: str, **fields: Any) -> Dict[str, Any]:
    entry = _agent_state.setdefault(role, {"role": role, "state": IDLE})
    entry.update(fields)
    entry["updated_at"] = time.time()
    return entry


def _norm(symbol: Optional[str]) -> str:
    return (symbol or "").replace("/", "").strip().upper()


def get_focus_symbols() -> List[str]:
    """All pinned pairs (may be empty)."""
    return list(_focus_symbols)


def get_focus_symbol() -> Optional[str]:
    """First pinned pair, or None. Kept for single-pair callers (worker cadence)."""
    return _focus_symbols[0] if _focus_symbols else None


def is_focused(symbol: str) -> bool:
    """True when this pair is one of the pinned pairs (symbol-normalised)."""
    if not _focus_symbols:
        return False
    return _norm(symbol) in {_norm(s) for s in _focus_symbols}


def snapshot() -> Dict[str, Any]:
    """Everything a freshly-opened room needs to render the current picture."""
    return {
        "focus_symbol": get_focus_symbol(),
        "focus_symbols": list(_focus_symbols),
        "ceo": CEO_PERSONA,
        "agents": [
            {**persona_for(role), **state}
            for role, state in sorted(
                _agent_state.items(), key=lambda kv: persona_for(kv[0])["seat"]
            )
        ],
        "sessions": list(reversed(_sessions)),
        "server_time": time.time(),
    }


# ── Emitters ────────────────────────────────────────────────────────────────


async def set_focus(symbols: "Optional[str | List[str]]") -> None:
    """Pin the room to one or more pairs. None or [] resumes free roaming."""
    global _focus_symbols
    if symbols is None:
        items: List[str] = []
    elif isinstance(symbols, str):
        # Accept a single symbol or a comma-joined string (persisted form).
        items = [s for s in (p.strip() for p in symbols.split(",")) if s]
    else:
        items = [s.strip() for s in symbols if s and s.strip()]

    # Normalise + dedupe, preserving order.
    seen: set[str] = set()
    cleaned: List[str] = []
    for s in items:
        up = s.upper()
        key = _norm(up)
        if key and key not in seen:
            seen.add(key)
            cleaned.append(up)

    _focus_symbols = cleaned
    await event_bus.publish(
        Topics.ROOM_FOCUS,
        {"symbols": list(_focus_symbols), "symbol": get_focus_symbol()},
    )


async def session_started(session_id: str, symbol: str, timeframe: str, trigger: str) -> None:
    record = {
        "session_id": session_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "trigger": trigger,
        "started_at": time.time(),
        "status": "running",
        "decisions": [],
    }
    _sessions.append(record)
    del _sessions[:-_MAX_SESSIONS]
    await event_bus.publish(Topics.SESSION_STARTED, record)


async def agent_started(session_id: str, role: str, agent_name: str, symbol: str) -> None:
    state = _set_state(
        role,
        state=ANALYZING,
        agent_name=agent_name,
        symbol=symbol,
        session_id=session_id,
        phase=ROLE_PHASE.get(role, 0),
    )
    await event_bus.publish(
        Topics.AGENT_STARTED, {**persona_for(role), **state, "session_id": session_id}
    )


async def agent_completed(
    session_id: str,
    role: str,
    agent_name: str,
    symbol: str,
    decision: Dict[str, Any],
) -> None:
    summary = {
        "action": decision.get("action", "hold"),
        "confidence": decision.get("confidence", 0),
        "reasoning": decision.get("reasoning", ""),
        "ai_called": decision.get("ai_called", True),
        "completed_at": time.time(),
    }
    state = _set_state(
        role,
        state=PRESENTING,
        agent_name=agent_name,
        symbol=symbol,
        session_id=session_id,
        last_decision=summary,
    )
    for record in _sessions:
        if record["session_id"] == session_id:
            record["decisions"].append({"role": role, "agent_name": agent_name, **summary})
            break
    await event_bus.publish(
        Topics.AGENT_COMPLETED,
        {**persona_for(role), **state, "session_id": session_id, "decision": decision},
    )


async def agent_failed(session_id: str, role: str, agent_name: str, error: str) -> None:
    state = _set_state(role, state=ERROR, agent_name=agent_name, error=error, session_id=session_id)
    await event_bus.publish(
        Topics.AGENT_STATE, {**persona_for(role), **state, "session_id": session_id}
    )


async def agent_speaking(
    session_id: str,
    role: str,
    agent_name: str,
    symbol: str,
    decision: Dict[str, Any],
) -> None:
    """Announce that a seat is presenting its verdict out loud.

    ``agent.completed`` records the decision in history; this carries the
    *spoken* form — the reasoning the seat is arguing — so the room UI can put
    a speech bubble above the avatar and scroll it into a live transcript while
    the meeting is still running.
    """
    from app.agents.orchestrator import reasoning_text

    payload = {
        "session_id": session_id,
        "symbol": symbol,
        "action": decision.get("action", "hold"),
        "confidence": decision.get("confidence", 0),
        "text": reasoning_text(decision.get("reasoning") or "").strip(),
        "at": time.time(),
    }
    if not payload["text"]:
        return
    await event_bus.publish(
        Topics.AGENT_SPEAKING,
        {**persona_for(role), "agent_name": agent_name, **payload},
    )


async def chair_speaking(session_id: str, result: Dict[str, Any]) -> None:
    """JARVIS closes the meeting by speaking the board's verdict aloud."""
    from app.agents.orchestrator import reasoning_text

    payload = {
        "session_id": session_id,
        "symbol": result.get("symbol"),
        "action": result.get("final_action", "hold"),
        "confidence": result.get("final_confidence", 0),
        "text": reasoning_text(result.get("final_reasoning") or "").strip(),
        "at": time.time(),
        "chair": True,
    }
    if not payload["text"]:
        return
    persona = persona_for(CEO_PERSONA["role"])
    await event_bus.publish(
        Topics.AGENT_SPEAKING,
        {**persona, "agent_name": persona["human_name"], **payload},
    )


async def session_completed(session_id: str, result: Dict[str, Any]) -> None:
    for record in _sessions:
        if record["session_id"] == session_id:
            record["status"] = "complete"
            record["finished_at"] = time.time()
            record["final_action"] = result.get("final_action")
            record["final_confidence"] = result.get("final_confidence")
            record["final_reasoning"] = result.get("final_reasoning")
            break

    for role, state in _agent_state.items():
        if state.get("session_id") == session_id:
            _set_state(role, state=RESTING)

    consensus = consensus_from(result.get("decisions", []))
    await event_bus.publish(
        Topics.SESSION_COMPLETED,
        {
            "session_id": session_id,
            "symbol": result.get("symbol"),
            "timeframe": result.get("timeframe"),
            "final_action": result.get("final_action"),
            "final_confidence": result.get("final_confidence"),
            "final_reasoning": result.get("final_reasoning"),
            "agents_used": result.get("agents_used", 0),
            "consensus": consensus,
            # The same forecast and measured momentum the seats argued from, so
            # the room UI can show the evidence beside the verdict instead of
            # only the conclusion.
            "kronos_forecast": result.get("kronos_forecast"),
            "momentum": result.get("momentum"),
            "price": result.get("price"),
        },
    )
    await _dispatch_alert(result, consensus)


async def _dispatch_alert(result: Dict[str, Any], consensus: Dict[str, Any]) -> None:
    """Publish a completed meeting the board actually agreed on.

    Only actionable outcomes go out — a room that reported every HOLD would
    train the user to ignore it.

    Telegram gets the full account: the verdict, every seat's reasoning, the
    forecast the seats argued from, the plan's levels and the drawn chart. It
    used to get the generic alert line — a title, one sentence and three
    key/value pairs — which told a trader what the desk decided and nothing
    about why or where, so the call could not be acted on. Discord and email
    keep the summary form; they are notification channels, not trading ones.
    """
    action = str(result.get("final_action") or "hold").lower()
    if action not in {"buy", "sell"}:
        return

    published = False
    try:
        from app.services.room_publisher import publish_meeting

        published = await publish_meeting(result, consensus)
    except Exception as exc:  # noqa: BLE001 - a failed publish must not fail the session
        logger.warning(f"[room] full publish failed, falling back to alert: {exc}")

    try:
        from app.monitoring.alerts import AlertService

        tally = consensus.get("tally", {})
        details = {
            "confidence": f"{round(float(result.get('final_confidence') or 0) * 100)}%",
            "agreement": f"{round(consensus.get('agreement', 0) * 100)}%",
            "votes": f"buy {tally.get('buy', 0)} / sell {tally.get('sell', 0)} / hold {tally.get('hold', 0)}",
            "timeframe": result.get("timeframe"),
        }
        if published:
            # The bot already sent the full account to Telegram; sending the
            # summary there too would double every published call.
            await AlertService.send_discord(
                AlertService._format_message(
                    f"{result.get('symbol')} — agents agree: {action.upper()}",
                    str(result.get("final_reasoning") or ""), "WARNING", details,
                )
            )
            return
        await AlertService.notify(
            title=f"{result.get('symbol')} — agents agree: {action.upper()}",
            message=str(result.get("final_reasoning") or ""),
            level="WARNING",
            details=details,
        )
    except Exception as exc:  # noqa: BLE001 - a failed alert must not fail the session
        logger.debug(f"[room] alert dispatch skipped: {exc}")


def consensus_from(decisions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Vote tally across agent decisions, normalised to buy/sell/hold."""
    tally = {"buy": 0, "sell": 0, "hold": 0}
    weighted = {"buy": 0.0, "sell": 0.0, "hold": 0.0}
    for d in decisions:
        action = str(d.get("action", "hold")).lower()
        bucket = (
            "buy" if action in {"buy", "bullish", "approve", "execute"}
            else "sell" if action in {"sell", "bearish", "close"}
            else "hold"
        )
        tally[bucket] += 1
        try:
            weighted[bucket] += float(d.get("confidence") or 0)
        except (TypeError, ValueError):
            pass
    total = sum(tally.values()) or 1
    leader = max(tally, key=lambda k: (tally[k], weighted[k]))
    return {
        "tally": tally,
        "leader": leader,
        "agreement": tally[leader] / total,
        "weighted_confidence": weighted[leader] / tally[leader] if tally[leader] else 0.0,
    }
