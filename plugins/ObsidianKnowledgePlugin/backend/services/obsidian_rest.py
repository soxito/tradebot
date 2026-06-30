"""
ObsidianKnowledgePlugin — Obsidian Local REST API Bridge

When the community plugin `obsidian-local-rest-api` is running inside Obsidian,
this bridge pushes notes live so the user sees them in the Obsidian app instantly.

INSTALL: https://github.com/coddingtonbear/obsidian-local-rest-api
CONFIG:  OBSIDIAN_REST_URL=https://localhost:27124
         OBSIDIAN_REST_TOKEN=<token shown in plugin settings>

The bridge is **entirely optional**.  When Obsidian is not running or the REST
plugin is not installed, every method is a safe no-op.  The VaultWriter always
writes to disk first, so data is never lost.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger

from plugins.ObsidianKnowledgePlugin.backend.config import obsidian_settings


class ObsidianRestBridge:
    """Thin async wrapper around the obsidian-local-rest-api HTTP endpoints."""

    # API endpoint paths (obsidian-local-rest-api v2)
    _VAULT_PATH   = "/vault/{path}"
    _SEARCH_PATH  = "/search/simple/"
    _OPEN_PATH    = "/open/{path}"

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_token: Optional[str] = None,
    ):
        self.base_url  = (base_url or obsidian_settings.OBSIDIAN_REST_URL).rstrip("/")
        self.api_token = api_token or obsidian_settings.OBSIDIAN_REST_TOKEN
        self._available: Optional[bool] = None  # cached availability check

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_token)

    def _client(self) -> httpx.AsyncClient:
        """Return a configured httpx async client."""
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_token}",
                "Content-Type": "text/markdown",
            },
            verify=False,   # self-signed cert from local plugin
            timeout=5.0,
        )

    # ── Availability check ────────────────────────────────────────────────────

    async def is_available(self) -> bool:
        """Return True if the Obsidian REST API is reachable."""
        if not self.enabled:
            return False
        try:
            async with self._client() as client:
                resp = await client.get("/")
                self._available = resp.status_code < 400
                return self._available
        except Exception:
            self._available = False
            return False

    # ── Note operations ───────────────────────────────────────────────────────

    async def push_note(self, relative_path: str, content: str) -> bool:
        """
        Write/update a note in Obsidian via REST.

        Returns True on success, False on failure (caller already wrote to disk).
        """
        if not self.enabled:
            return False
        try:
            async with self._client() as client:
                url = f"/vault/{relative_path}"
                resp = await client.put(url, content=content.encode("utf-8"))
                if resp.status_code in (200, 201, 204):
                    logger.debug(f"[ObsidianREST] Pushed: {relative_path}")
                    return True
                logger.warning(f"[ObsidianREST] Push failed {resp.status_code}: {relative_path}")
                return False
        except Exception as exc:
            logger.debug(f"[ObsidianREST] Push error ({relative_path}): {exc}")
            return False

    async def pull_note(self, relative_path: str) -> Optional[str]:
        """
        Read a note's current content from Obsidian.

        Returns markdown content or None if not found / unavailable.
        """
        if not self.enabled:
            return None
        try:
            async with self._client() as client:
                resp = await client.get(f"/vault/{relative_path}")
                if resp.status_code == 200:
                    return resp.text
                return None
        except Exception:
            return None

    async def list_modified(self, since: Optional[datetime] = None) -> List[str]:
        """
        List notes currently in the Obsidian vault.

        The obsidian-local-rest-api returns ``{"files": ["path/to/note.md", ...]}``
        where each entry is a plain string path relative to the vault root.
        The ``since`` parameter is not filterable server-side; it is ignored here
        because we only need the full list for reconciliation.

        Returns list of relative paths as strings.
        """
        if not self.enabled:
            return []
        try:
            async with self._client() as client:
                resp = await client.get("/vault/")
                if resp.status_code != 200:
                    return []
                data = resp.json()
                # Handle {"files": [...]} format (obsidian-local-rest-api v1/v2)
                if isinstance(data, dict):
                    files = data.get("files", [])
                elif isinstance(data, list):
                    files = data
                else:
                    return []
                # Each entry is either a plain string path or a dict with a "path" key
                result = []
                for f in files:
                    if isinstance(f, str):
                        result.append(f)
                    elif isinstance(f, dict):
                        p = f.get("path") or f.get("name")
                        if p:
                            result.append(p)
                return result
        except Exception:
            return []

    async def search_vault(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """
        Simple text search using Obsidian's built-in search.

        Returns list of {path, score, excerpt} dicts.
        """
        if not self.enabled:
            return []
        try:
            async with self._client() as client:
                resp = await client.post(
                    self._SEARCH_PATH,
                    content=query.encode("utf-8"),
                    headers={"Content-Type": "text/plain"},
                )
                if resp.status_code != 200:
                    return []
                return resp.json()[:limit]
        except Exception:
            return []

    async def open_note(self, relative_path: str) -> bool:
        """
        Tell Obsidian to open a specific note in the app.

        Useful for deep-linking from the TradeBot UI into Obsidian.
        Returns True if the request was accepted.
        """
        if not self.enabled:
            return False
        try:
            async with self._client() as client:
                resp = await client.post(f"/open/{relative_path}")
                return resp.status_code in (200, 201, 204)
        except Exception:
            return False

    async def delete_note(self, relative_path: str) -> bool:
        """Delete a note from the Obsidian vault."""
        if not self.enabled:
            return False
        try:
            async with self._client() as client:
                resp = await client.delete(f"/vault/{relative_path}")
                return resp.status_code in (200, 204)
        except Exception:
            return False


# Module-level singleton — recreated if settings change
_bridge: Optional[ObsidianRestBridge] = None
_bridge_url: str = ""
_bridge_token: str = ""


def get_bridge() -> ObsidianRestBridge:
    """Return (or create) the module-level REST bridge singleton.

    Re-creates the bridge if OBSIDIAN_REST_URL or OBSIDIAN_REST_TOKEN changed
    since the last call — useful after hot-reloading .env without restart.
    """
    global _bridge, _bridge_url, _bridge_token
    current_url   = obsidian_settings.OBSIDIAN_REST_URL
    current_token = obsidian_settings.OBSIDIAN_REST_TOKEN
    if _bridge is None or current_url != _bridge_url or current_token != _bridge_token:
        _bridge       = ObsidianRestBridge(base_url=current_url, api_token=current_token)
        _bridge_url   = current_url
        _bridge_token = current_token
    return _bridge
