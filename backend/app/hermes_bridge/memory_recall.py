"""
Hermes memory recall — unified prompt injection for Trading Room + JARVIS.

Replaces scattered `build_memory_prompt` / `_brain_recall_context` builders
with a single FTS5 + Postgres hybrid. Recall-only: scoring stays on
Postgres AgentDecision (per locked plan).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from loguru import logger

from app.hermes_bridge import is_enabled
from app.hermes_bridge.state_store import search_local


async def recall_for_symbol(symbol: str, role: str, limit: int = 6, max_chars: int = 600) -> str:
    """
    Returns a compact context block to prepend to the agent system prompt.
    Empty string when hermes disabled or no hits.
    """
    if not is_enabled():
        return ""
    sym = (symbol or "").strip().upper()
    if not sym:
        return ""
    # Hybrid: FTS5 recall + Postgres recent decisions are merged by caller
    # (orchestrator keeps Postgres memory_prompt; this adds episodic hits)
    query = f"{sym} {role}".replace("/", " ")
    try:
        # Prefer sidecar search
        hits = await _search_via_gateway(query, sym, limit)
        if not hits:
            hits = search_local(query, sym, limit)
        if not hits:
            return ""
        parts: List[str] = []
        consumed = 0
        for h in hits:
            c = (h.get("content") or "")[:220]
            if not c:
                continue
            parts.append(c)
            consumed += len(c)
            if consumed >= max_chars:
                break
        if not parts:
            return ""
        joined = "\n─\n".join(parts[:3])
        return f"\n[HERMES MEMORY — {sym} / {role}]\n{joined}\n[END HERMES MEMORY]\n"
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[hermes] recall skipped: {exc}")
        return ""


async def _search_via_gateway(query: str, symbol: str, limit: int) -> List[Dict[str, Any]]:
    try:
        from app.hermes_bridge import gateway_url
        import httpx
        async with httpx.AsyncClient(timeout=4) as c:
            r = await c.get(f"{gateway_url()}/v1/hermes/search", params={"q": query, "symbol": symbol, "limit": limit})
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and isinstance(data.get("hits"), list):
                    return data["hits"]
                if isinstance(data, list):
                    return data
    except Exception:
        pass
    return []


def format_episodic_block(hits: List[Dict[str, Any]], symbol: str, role: str, max_chars: int = 600) -> str:
    if not hits:
        return ""
    parts = [(h.get("content") or "")[:220] for h in hits if h.get("content")]
    if not parts:
        return ""
    joined = "\n─\n".join(parts[:3])
    return f"\n[HERMES MEMORY — {symbol} / {role}]\n{joined[:max_chars]}\n[END HERMES MEMORY]\n"
