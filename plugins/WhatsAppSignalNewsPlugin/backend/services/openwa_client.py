"""OpenWA Client wrapper for WhatsApp plugin.

Wraps the openwa-sdk AsyncOpenWAClient with error handling, retries,
and TradeBot-specific helpers.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger

from plugins.WhatsAppSignalNewsPlugin.backend.config import whatsapp_plugin_config, WhatsAppPluginConfig


class OpenWAClientError(Exception):
    """Base exception for OpenWA client errors."""

    def __init__(self, message: str, status_code: Optional[int] = None, response: Optional[Dict] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class OpenWAAuthError(OpenWAClientError):
    """Authentication/authorization error."""

    pass


class OpenWANotFoundError(OpenWAClientError):
    """Resource not found."""

    pass


class OpenWAClient:
    """Async wrapper around openwa-sdk for TradeBot integration.

    Provides high-level methods for session management, message retrieval,
    and webhook handling.
    """

    def __init__(self, config: Optional[WhatsAppPluginConfig] = None):
        self.config = config or whatsapp_plugin_config
        self._client: Optional[httpx.AsyncClient] = None
        self._sdk_client = None  # openwa-sdk AsyncOpenWAClient (lazy init)

    async def _get_http_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.config.openwa_base_url,
                timeout=httpx.Timeout(30.0, connect=10.0),
                headers={
                    "Authorization": f"Bearer {self.config.openwa_api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def _get_sdk_client(self):
        """Get or create openwa-sdk client."""
        if self._sdk_client is None:
            try:
                from openwa import AsyncOpenWAClient
                self._sdk_client = AsyncOpenWAClient(
                    base_url=self.config.openwa_base_url,
                    api_key=self.config.openwa_api_key,
                )
            except ImportError:
                logger.warning("openwa-sdk not installed, using raw HTTP client")
                self._sdk_client = False  # Mark as unavailable
        return self._sdk_client

    async def close(self):
        """Close HTTP clients."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        if self._sdk_client and self._sdk_client is not False:
            try:
                await self._sdk_client.close()
            except Exception:
                pass

    # ────────────────────────────────────────────────────────────────
    # Low-level HTTP methods (fallback when SDK unavailable)
    # ────────────────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[Dict] = None,
        params: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Make HTTP request with error handling."""
        client = await self._get_http_client()
        try:
            response = await client.request(method, path, json=json, params=params)
            if response.status_code >= 400:
                error_data = {}
                try:
                    error_data = response.json()
                except Exception:
                    error_data = {"detail": response.text}

                if response.status_code == 401:
                    raise OpenWAAuthError(
                        f"Authentication failed: {error_data.get('detail', 'Invalid API key')}",
                        status_code=response.status_code,
                        response=error_data,
                    )
                elif response.status_code == 404:
                    raise OpenWANotFoundError(
                        f"Resource not found: {error_data.get('detail', path)}",
                        status_code=response.status_code,
                        response=error_data,
                    )
                else:
                    raise OpenWAClientError(
                        f"API error: {error_data.get('detail', response.text)}",
                        status_code=response.status_code,
                        response=error_data,
                    )
            return response.json()
        except httpx.RequestError as e:
            raise OpenWAClientError(f"Request failed: {e}")

    # ────────────────────────────────────────────────────────────────
    # Session Management
    # ────────────────────────────────────────────────────────────────

    async def create_session(self, name: str) -> Dict[str, Any]:
        """Create a new WhatsApp session."""
        sdk = await self._get_sdk_client()
        if sdk:
            return await sdk.sessions.create(name)
        return await self._request("POST", "/api/sessions", json={"name": name})

    async def start_session(self, session_id: str) -> Dict[str, Any]:
        """Start a WhatsApp session."""
        sdk = await self._get_sdk_client()
        if sdk:
            return await sdk.sessions.start(session_id)
        return await self._request("POST", f"/api/sessions/{session_id}/start")

    async def stop_session(self, session_id: str) -> Dict[str, Any]:
        """Stop a WhatsApp session."""
        sdk = await self._get_sdk_client()
        if sdk:
            return await sdk.sessions.stop(session_id)
        return await self._request("POST", f"/api/sessions/{session_id}/stop")

    async def delete_session(self, session_id: str) -> Dict[str, Any]:
        """Delete a WhatsApp session."""
        sdk = await self._get_sdk_client()
        if sdk:
            return await sdk.sessions.delete(session_id)
        return await self._request("DELETE", f"/api/sessions/{session_id}")

    async def get_session(self, session_id: str) -> Dict[str, Any]:
        """Get session details."""
        sdk = await self._get_sdk_client()
        if sdk:
            return await sdk.sessions.get(session_id)
        return await self._request("GET", f"/api/sessions/{session_id}")

    async def list_sessions(self) -> List[Dict[str, Any]]:
        """List all sessions."""
        sdk = await self._get_sdk_client()
        if sdk:
            return await sdk.sessions.list()
        return await self._request("GET", "/api/sessions")

    async def get_session_status(self, session_id: str) -> Dict[str, Any]:
        """Get session status."""
        sdk = await self._get_sdk_client()
        if sdk:
            return await sdk.sessions.status(session_id)
        return await self._request("GET", f"/api/sessions/{session_id}/status")

    async def get_qr_code(self, session_id: str) -> Dict[str, Any]:
        """Get QR code for session authentication."""
        sdk = await self._get_sdk_client()
        if sdk:
            return await sdk.sessions.qr(session_id)
        return await self._request("GET", f"/api/sessions/{session_id}/qr")

    # ────────────────────────────────────────────────────────────────
    # Messages
    # ────────────────────────────────────────────────────────────────

    async def get_messages(
        self,
        session_id: str,
        chat_id: str,
        limit: int = 50,
        before: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get messages from a chat."""
        sdk = await self._get_sdk_client()
        if sdk:
            return await sdk.messages.list(session_id, chat_id, limit=limit, before=before)
        params = {"limit": limit}
        if before:
            params["before"] = before
        return await self._request("GET", f"/api/sessions/{session_id}/messages/{chat_id}", params=params)

    async def send_text(
        self,
        session_id: str,
        chat_id: str,
        text: str,
        reply_to: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send a text message."""
        sdk = await self._get_sdk_client()
        if sdk:
            return await sdk.messages.send_text(session_id, chat_id, text, reply_to=reply_to)
        return await self._request(
            "POST",
            f"/api/sessions/{session_id}/messages",
            json={"chatId": chat_id, "text": text, "replyTo": reply_to},
        )

    async def send_media(
        self,
        session_id: str,
        chat_id: str,
        media_url: str,
        caption: Optional[str] = None,
        media_type: str = "image",
    ) -> Dict[str, Any]:
        """Send a media message."""
        sdk = await self._get_sdk_client()
        if sdk:
            return await sdk.messages.send_media(session_id, chat_id, media_url, caption, media_type)
        return await self._request(
            "POST",
            f"/api/sessions/{session_id}/messages/media",
            json={"chatId": chat_id, "mediaUrl": media_url, "caption": caption, "type": media_type},
        )

    # ────────────────────────────────────────────────────────────────
    # Chats & Contacts
    # ────────────────────────────────────────────────────────────────

    async def list_chats(
        self,
        session_id: str,
        limit: int = 100,
        only_groups: bool = False,
    ) -> List[Dict[str, Any]]:
        """List chats for a session."""
        sdk = await self._get_sdk_client()
        if sdk:
            return await sdk.chats.list(session_id, limit=limit, only_groups=only_groups)
        params = {"limit": limit, "onlyGroups": str(only_groups).lower()}
        return await self._request("GET", f"/api/sessions/{session_id}/chats", params=params)

    async def get_chat(self, session_id: str, chat_id: str) -> Dict[str, Any]:
        """Get chat details."""
        sdk = await self._get_sdk_client()
        if sdk:
            return await sdk.chats.get(session_id, chat_id)
        return await self._request("GET", f"/api/sessions/{session_id}/chats/{chat_id}")

    async def list_contacts(self, session_id: str) -> List[Dict[str, Any]]:
        """List contacts."""
        sdk = await self._get_sdk_client()
        if sdk:
            return await sdk.contacts.list(session_id)
        return await self._request("GET", f"/api/sessions/{session_id}/contacts")

    # ────────────────────────────────────────────────────────────────
    # Groups
    # ────────────────────────────────────────────────────────────────

    async def list_groups(self, session_id: str) -> List[Dict[str, Any]]:
        """List groups."""
        sdk = await self._get_sdk_client()
        if sdk:
            return await sdk.groups.list(session_id)
        return await self._request("GET", f"/api/sessions/{session_id}/groups")

    async def get_group_info(self, session_id: str, group_id: str) -> Dict[str, Any]:
        """Get group info."""
        sdk = await self._get_sdk_client()
        if sdk:
            return await sdk.groups.get(session_id, group_id)
        return await self._request("GET", f"/api/sessions/{session_id}/groups/{group_id}")

    async def get_group_participants(self, session_id: str, group_id: str) -> List[Dict[str, Any]]:
        """Get group participants."""
        sdk = await self._get_sdk_client()
        if sdk:
            return await sdk.groups.participants(session_id, group_id)
        return await self._request("GET", f"/api/sessions/{session_id}/groups/{group_id}/participants")

    # ────────────────────────────────────────────────────────────────
    # Webhooks
    # ────────────────────────────────────────────────────────────────

    async def create_webhook(
        self,
        session_id: str,
        url: str,
        events: List[str],
        secret: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a webhook subscription."""
        sdk = await self._get_sdk_client()
        if sdk:
            return await sdk.webhooks.create(session_id, url, events, secret)
        return await self._request(
            "POST",
            f"/api/sessions/{session_id}/webhooks",
            json={"url": url, "events": events, "secret": secret},
        )

    async def list_webhooks(self, session_id: str) -> List[Dict[str, Any]]:
        """List webhooks."""
        sdk = await self._get_sdk_client()
        if sdk:
            return await sdk.webhooks.list(session_id)
        return await self._request("GET", f"/api/sessions/{session_id}/webhooks")

    async def delete_webhook(self, session_id: str, webhook_id: str) -> Dict[str, Any]:
        """Delete a webhook."""
        sdk = await self._get_sdk_client()
        if sdk:
            return await sdk.webhooks.delete(session_id, webhook_id)
        return await self._request("DELETE", f"/api/sessions/{session_id}/webhooks/{webhook_id}")

    # ────────────────────────────────────────────────────────────────
    # Health & Info
    # ────────────────────────────────────────────────────────────────

    async def health_check(self) -> Dict[str, Any]:
        """Check OpenWA Gateway health."""
        try:
            return await self._request("GET", "/health")
        except Exception as e:
            return {"status": "unhealthy", "error": str(e)}

    async def get_version(self) -> Dict[str, Any]:
        """Get OpenWA Gateway version."""
        try:
            return await self._request("GET", "/version")
        except Exception as e:
            return {"error": str(e)}


class OpenWAClientManager:
    """Manages OpenWA client instances per configuration."""

    _instances: Dict[str, OpenWAClient] = {}

    @classmethod
    def get_client(cls, config: Optional[WhatsAppPluginConfig] = None) -> OpenWAClient:
        """Get or create client for config."""
        key = config.openwa_base_url if config else "default"
        if key not in cls._instances:
            cls._instances[key] = OpenWAClient(config)
        return cls._instances[key]

    @classmethod
    async def close_all(cls):
        """Close all client instances."""
        for client in cls._instances.values():
            await client.close()
        cls._instances.clear()