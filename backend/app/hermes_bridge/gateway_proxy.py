"""
Hermes gateway proxy — bridges TradeBot EventBus ↔ Hermes gateway (Telegram/Discord).

Locked: immediate cutover (9.4), reuses AiMarketAnalyst pool (9.5).
TelegramSignalNewsPlugin becomes consumer, not bot owner.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from loguru import logger

from app.core.config import settings
from app.hermes_bridge import gateway_url, is_enabled


async def proxy_hermes_chat(
    text: str,
    session_key: str = "default",
    image: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Send a Paul/JARVIS chat turn to Hermes gateway for unified handling.
    Returns gateway response or None if sidecar unreachable (fallback to local).
    """
    if not is_enabled():
        return None
    try:
        import httpx
        payload: Dict[str, Any] = {"text": text, "session_key": session_key, "meta": meta or {}}
        if image:
            payload["image"] = image
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{gateway_url()}/v1/hermes/chat", json=payload)
            if r.status_code == 200:
                return r.json()
            logger.debug(f"[hermes] gateway chat {r.status_code}: {r.text[:200]}")
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[hermes] gateway chat unreachable: {exc}")
    return None


async def notify_gateway_session(result: Dict[str, Any], consensus: Optional[Dict[str, Any]] = None) -> None:
    """Fire-and-forget session completion to gateway (for Telegram/Discord delivery)."""
    if not is_enabled():
        return
    try:
        import httpx
        async with httpx.AsyncClient(timeout=6) as c:
            await c.post(f"{gateway_url()}/v1/hermes/session_completed", json={"result": result, "consensus": consensus or {}})
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[hermes] gateway session notify skipped: {exc}")


def health_check_sync() -> Dict[str, Any]:
    """Sync health for startup logging (no async)."""
    import socket
    from urllib.parse import urlparse
    try:
        u = urlparse(gateway_url())
        host = u.hostname or "127.0.0.1"
        port = u.port or 8011
        with socket.create_connection((host, port), timeout=1):
            return {"reachable": True, "url": gateway_url()}
    except Exception as exc:  # noqa: BLE001
        return {"reachable": False, "url": gateway_url(), "error": str(exc)[:120]}
