"""
Ngrok Tunnel Service

Manages ngrok SDK session/listener lifecycle for backend and frontend tunnels.
OAuth (Google) is always enforced on every managed tunnel.

ngrok-python 1.x has an async API — all methods must be awaited directly.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from loguru import logger

from app.core.config import settings


@dataclass
class TunnelInfo:
    """Metadata about a running ngrok listener."""
    name: str           # "backend" or "frontend"
    local_addr: str     # e.g. "http://localhost:1448"
    public_url: str     # e.g. "https://abc123.ngrok-free.app"
    started_at: datetime = field(default_factory=datetime.utcnow)


class NgrokService:
    """
    Singleton service that wraps ngrok-python 1.x (async API).

    OAuth is always Google — no code path exists to create a listener
    without OAuth, and config updates that attempt to disable it are rejected
    by the API layer (see ngrok.py).
    """

    _instance: Optional["NgrokService"] = None

    def __new__(cls) -> "NgrokService":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialised = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialised:
            return
        self._initialised = True
        self._listeners: dict[str, any] = {}   # name → ngrok Listener
        self._tunnels: dict[str, TunnelInfo] = {}
        self._lock: Optional[asyncio.Lock] = None  # created lazily inside event loop
        self._state: str = "stopped"
        self._error: Optional[str] = None

    def _get_lock(self) -> asyncio.Lock:
        """Return a lock, creating it lazily the first time we're inside an event loop."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # ── Public API ─────────────────────────────────────────────────────────

    async def start(
        self,
        authtoken: str,
        backend_addr: str,
        frontend_addr: str,
    ) -> dict:
        """Start both backend and frontend tunnels with Google OAuth enforced."""
        async with self._get_lock():
            if self._state == "running":
                return {
                    "already_running": True,
                    "tunnels": self._tunnel_snapshots(),
                }

            self._state = "starting"
            self._error = None
            try:
                import ngrok  # local import so app starts without the package installed
            except ImportError:
                self._state = "error"
                self._error = "ngrok package not installed. Run: pip install ngrok"
                raise RuntimeError(self._error)

            try:
                for name, local_addr in [("backend", backend_addr), ("frontend", frontend_addr)]:
                    # ngrok.forward is async in ngrok-python 1.x — await it directly
                    listener = await ngrok.forward(
                        local_addr,
                        authtoken=authtoken,
                        oauth_provider="google",
                    )
                    public_url = listener.url()
                    self._listeners[name] = listener
                    self._tunnels[name] = TunnelInfo(
                        name=name, local_addr=local_addr, public_url=public_url
                    )
                    logger.info(f"🌐 ngrok [{name}] → {public_url}")

                self._state = "running"
                return {
                    "started": True,
                    "tunnels": self._tunnel_snapshots(),
                }
            except Exception as exc:
                self._state = "error"
                self._error = str(exc)
                logger.error(f"ngrok start failed: {exc}")
                await self._cleanup_listeners()
                raise

    async def stop(self) -> dict:
        """Stop all tunnels."""
        async with self._get_lock():
            if self._state not in ("running", "error"):
                return {"already_stopped": True}
            await self._cleanup_listeners()
            self._state = "stopped"
            return {"stopped": True}

    def status(self) -> dict:
        """Return a status snapshot (no lock required for reads)."""
        return {
            "state": self._state,
            "error": self._error,
            "tunnels": self._tunnel_snapshots(),
            "oauth_provider": settings.NGROK_OAUTH_PROVIDER,
            "oauth_enforced": True,
        }

    # ── Internal helpers ───────────────────────────────────────────────────

    async def _cleanup_listeners(self) -> None:
        """Close all active listeners."""
        for name, listener in list(self._listeners.items()):
            try:
                await listener.close()
                logger.info(f"ngrok listener [{name}] closed")
            except Exception as exc:
                logger.warning(f"ngrok listener [{name}] close error: {exc}")

        self._listeners.clear()
        self._tunnels.clear()

    def _tunnel_snapshots(self) -> list[dict]:
        return [
            {
                "name": t.name,
                "local_addr": t.local_addr,
                "public_url": t.public_url,
                "started_at": t.started_at.isoformat(),
            }
            for t in self._tunnels.values()
        ]


# Module-level singleton
ngrok_service = NgrokService()
