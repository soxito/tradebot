"""
ObsidianKnowledgePlugin — Vault Fire-and-Forget Utility

Provides a single function `vault_capture()` that can be called from anywhere
in the backend without blocking the calling coroutine.  It creates an asyncio
task that writes a vault note and pushes it to Obsidian.

Usage (from any async backend code):
    from plugins.ObsidianKnowledgePlugin.backend.services.vault_capture import vault_capture
    vault_capture(
        action_type="agent-decision",
        symbol="BTCUSDT",
        summary="Market analyst: BUY @ 85% confidence",
        detail=reasoning[:400],
        agent_role="market_analyst",
        confidence=0.85,
    )
"""
from __future__ import annotations

import asyncio
from typing import Optional

from loguru import logger


def vault_capture(
    action_type: str,
    symbol: str = "",
    summary: str = "",
    detail: str = "",
    tags: Optional[list] = None,
    agent_role: str = "",
    confidence: Optional[float] = None,
    order_id: str = "",
) -> None:
    """
    Fire-and-forget vault write.  Creates an asyncio task so the caller is
    never blocked.  Safe to call from any async context.

    The task writes the action note to disk and pushes it live to Obsidian
    via the REST bridge if configured.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(
                _write_and_push(
                    action_type=action_type,
                    symbol=symbol,
                    summary=summary,
                    detail=detail,
                    tags=tags,
                    agent_role=agent_role,
                    confidence=confidence,
                    order_id=order_id,
                )
            )
    except Exception as e:
        logger.debug(f"[VaultCapture] task scheduling skipped: {e}")


async def _write_and_push(
    action_type: str,
    symbol: str,
    summary: str,
    detail: str,
    tags: Optional[list],
    agent_role: str,
    confidence: Optional[float],
    order_id: str,
) -> None:
    """Inner coroutine — runs in background."""
    try:
        from plugins.ObsidianKnowledgePlugin.backend.services.vault_writer import VaultWriter
        from plugins.ObsidianKnowledgePlugin.backend.services.obsidian_rest import get_bridge

        writer = VaultWriter()
        path, written, cs = writer.write_action_note(
            action_type=action_type,
            symbol=symbol,
            summary=summary,
            detail=detail,
            tags=tags or [],
            agent_role=agent_role,
            confidence=confidence,
            order_id=order_id,
        )

        if written:
            bridge = get_bridge()
            if bridge.enabled:
                rel = str(path.relative_to(writer.root))
                await bridge.push_note(rel, path.read_text(encoding="utf-8"))

    except Exception as e:
        logger.debug(f"[VaultCapture] write/push failed: {e}")
