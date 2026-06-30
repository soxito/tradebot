"""
JARVIS natural-language intent parser.

The frontend command interpreter (voiceCommands.ts) handles the common, exact
phrasings instantly client-side. When it misses, the page calls this endpoint
so a spoken command in natural language ("can you bring up the futures screen")
can still be mapped to a structured action and executed — instead of falling
straight through to chat.

Returns a structured action that mirrors the frontend ``VoiceAction`` shape, or
``{"type": "none"}`` when the utterance isn't an actionable command (so the
caller falls back to chat Q&A).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

from sqlalchemy.ext.asyncio import AsyncSession

from plugins.AiMarketAnalyst.backend.services.ai_router import db_chat

logger = logging.getLogger(__name__)

# App routes JARVIS can navigate to (kept in sync with frontend NAV_ROUTES).
NAV_ROUTES: list[tuple[str, str]] = [
    ("Dashboard", "/"),
    ("Trading", "/trading"),
    ("Futures", "/futures"),
    ("Signals", "/signals"),
    ("Telegram Signals", "/telegram-signals"),
    ("Trending", "/trending"),
    ("Strategies", "/strategies"),
    ("Sentiment", "/sentiment"),
    ("Pump Monitor", "/pump-monitor"),
    ("Rug Pulled", "/rug-pulled"),
    ("Sniper Signals", "/sniper-signals"),
    ("Smart Money Concepts", "/smart-money-concepts"),
    ("Delistings", "/delistings"),
    ("AI Agents", "/agents"),
    ("Custom Agents", "/custom-agents"),
    ("Agent Paul", "/agent-paul"),
    ("Intelligence", "/intelligence"),
    ("Insights", "/insights"),
    ("MT5 Live", "/mt5-live"),
    ("MT5 Replay", "/mt5-replay"),
    ("MT5 Copy Sim", "/mt5-copy-sim"),
    ("AI Analyst", "/ai-analysis"),
    ("Telegram", "/telegram"),
    ("AI Profiles", "/ai-agents-admin"),
    ("Trade History", "/history"),
    ("Settings", "/settings"),
]

# Action types the frontend executor (executeVoiceAction) knows how to perform.
ALLOWED_TYPES = {
    "navigate", "click", "type", "scroll", "back", "forward", "reload",
    "top", "bottom", "open_chat", "close_chat", "new_chat",
    "stop_listening", "repeat", "help",
    "set_field", "select_option", "toggle", "switch_tab", "set_timeframe",
    "submit_form", "cancel",
    "set_leverage", "set_amount", "close_position", "close_all",
    "none",
}

_VALID_PATHS = {p for _, p in NAV_ROUTES}


def _build_prompt(text: str, pathname: str) -> str:
    routes = "\n".join(f"  {label} -> {path}" for label, path in NAV_ROUTES)
    return f"""You are JARVIS's intent parser for a hands-free trading app.
Map the user's spoken command to ONE structured action. The user is currently on page: {pathname or '/'}.

Available action types and their fields:
- navigate  -> {{"type":"navigate","path":"/route","say":"Opening X."}}   (path MUST be one of the routes below)
- click     -> {{"type":"click","target":"button label"}}
- type      -> {{"type":"type","text":"text to type"}}
- set_field -> {{"type":"set_field","field":"field name","value":"value"}}
- select_option -> {{"type":"select_option","target":"option","field":"optional dropdown name"}}
- toggle    -> {{"type":"toggle","target":"switch label"}}
- switch_tab-> {{"type":"switch_tab","target":"tab name"}}
- set_timeframe -> {{"type":"set_timeframe","value":"15m"}}   (use 1m/5m/15m/1h/4h/1d/1w)
- submit_form / cancel / back / forward / reload / top / bottom / scroll(direction up|down)
- open_chat / close_chat / new_chat / stop_listening / repeat / help
- set_leverage -> {{"type":"set_leverage","value":"10"}}
- set_amount   -> {{"type":"set_amount","value":"100"}}
- close_position -> {{"type":"close_position"}}
- close_all      -> {{"type":"close_all"}}

Navigation routes:
{routes}

Rules:
- If the command is a pure question, statement, or anything NOT mapping to an action above, return {{"type":"none"}}.
- For navigate you MUST return one of the exact paths above.
- Add a short "say" confirmation only for navigate.
- Return ONLY minified JSON, no prose.

User command: "{text}"
JSON:"""


async def parse_intent(db: AsyncSession, text: str, pathname: str = "/") -> Dict[str, Any]:
    """Return a structured action dict, or {"type": "none"} when not actionable."""
    text = (text or "").strip()
    if not text:
        return {"type": "none"}

    try:
        result = await db_chat(
            db,
            [{"role": "user", "content": _build_prompt(text, pathname)}],
            temperature=0.0,
            json_mode=True,
        )
    except Exception as exc:  # noqa
        logger.debug(f"[JARVIS] intent parse error: {exc}")
        return {"type": "none"}

    if not result or not result.get("ok"):
        return {"type": "none"}

    raw = result.get("content") or result.get("text") or "{}"
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return {"type": "none"}
    if not isinstance(data, dict):
        return {"type": "none"}

    a_type = str(data.get("type", "none")).strip()
    if a_type not in ALLOWED_TYPES or a_type == "none":
        return {"type": "none"}

    # Validate navigation targets against the real route table.
    if a_type == "navigate":
        path = data.get("path")
        if path not in _VALID_PATHS:
            return {"type": "none"}

    # Pass through only the recognised fields.
    out: Dict[str, Any] = {"type": a_type}
    for key in ("path", "target", "field", "value", "text", "direction", "say"):
        if data.get(key) not in (None, ""):
            out[key] = data[key]
    return out
