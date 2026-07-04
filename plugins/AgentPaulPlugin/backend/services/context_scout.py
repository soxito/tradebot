"""
Agent Paul — SuperContext scout (OpenHuman-style first-turn context bundle)

On the first message of a conversation, deterministically sweeps JARVIS's
Memory Tree, long-term knowledge, live news and the graphify brain-map for the
user's *actual* question, then assembles a bounded, compressed context bundle
that is prepended to the system prompt — so JARVIS answers the first message
already knowing the relevant background (no "let me look that up" round-trip).

Read-only. Fails soft: any sub-source that errors is simply skipped.
"""
from __future__ import annotations

import re
from typing import Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from plugins.AgentPaulPlugin.backend.services import (
    memory_tree, knowledge_base, news_research,
)

_MAX_BUNDLE_CHARS = 2400  # keep the bundle bounded so it never dominates the prompt


def _extract_symbols(text: str) -> list[str]:
    up = (text or "").upper()
    pairs = re.findall(r"\b([A-Z]{2,6}(?:USDT?|USD|BTC))\b", up)
    bare = [t for t in re.split(r"[^A-Z]+", up) if 2 < len(t) <= 6]
    seen: list[str] = []
    for s in pairs + bare:
        if s not in seen:
            seen.append(s)
    return seen[:3]


async def build_context_bundle(db: AsyncSession, user_msg: str) -> Optional[str]:
    """Return a Markdown context bundle for the user's message, or None."""
    if not user_msg or len(user_msg.strip()) < 3:
        return None

    sections: list[str] = []

    # 1. Memory Tree (importance-weighted, message-specific)
    try:
        mem = await memory_tree.search(db, user_msg, limit=5)
        if mem:
            lines = [f"  - ({m['importance']:.2f}) {m['summary']}" for m in mem if m.get("summary")]
            if lines:
                sections.append("### From memory (most important & relevant)\n" + "\n".join(lines))
    except Exception as exc:  # noqa
        logger.debug(f"[Scout] memory sweep skipped: {exc}")

    # 2. Long-term learned knowledge
    try:
        learned = await knowledge_base.search_knowledge(db, user_msg, limit=4)
        if learned:
            lines = [f"  - {k['content'][:160]}" for k in learned if k.get("content")]
            if lines:
                sections.append("### Learned knowledge\n" + "\n".join(lines))
    except Exception as exc:  # noqa
        logger.debug(f"[Scout] knowledge sweep skipped: {exc}")

    # 3. Targeted live news for any mentioned instrument
    try:
        for sym in _extract_symbols(user_msg):
            hits = await news_research.news_for_symbol(sym, limit=3)
            if hits:
                lines = [f"  - ({n['sentiment']:+.2f}) {n['title']}" for n in hits]
                sections.append(f"### Recent news on {sym}\n" + "\n".join(lines))
                break
    except Exception as exc:  # noqa
        logger.debug(f"[Scout] news sweep skipped: {exc}")

    # 4. Graphify brain-map nodes relevant to the question
    try:
        from plugins.AiMarketAnalyst.backend.services.graphify_service import query_map
        hits = query_map(user_msg, top_n=4)
        nodes = (hits or {}).get("nodes") if isinstance(hits, dict) else None
        if nodes:
            lines = [f"  - {n.get('label') or n.get('id')}" for n in nodes[:4]]
            sections.append("### Brain-map nodes\n" + "\n".join(lines))
    except Exception as exc:  # noqa
        logger.debug(f"[Scout] graphify sweep skipped: {exc}")

    if not sections:
        return None

    bundle = (
        "## 🧭 Context Bundle (assembled by the SuperContext scout for THIS question)\n"
        "The scout swept your memory, knowledge, news and brain-map for the user's "
        "message. Use this as pre-loaded background — answer directly, cite it when "
        "relevant, and don't claim you lack context.\n\n"
        + "\n\n".join(sections)
    )
    if len(bundle) > _MAX_BUNDLE_CHARS:
        bundle = bundle[:_MAX_BUNDLE_CHARS].rstrip() + "\n…(context truncated)"
    return bundle
