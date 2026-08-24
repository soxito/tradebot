"""Telegram providers for channel metadata and message polling."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import inspect
import re
from typing import Any, Literal, Protocol

import httpx

from plugins.TelegramSignalNewsPlugin.backend.config import TelegramPluginConfig
from plugins.TelegramSignalNewsPlugin.backend.timezone_utils import now_sast

# In-memory store: phone_number -> phone_code_hash (cleared on complete/disconnect)
_PENDING_AUTH_CODES: dict[str, str] = {}

# Process-wide lock serialising ALL Telethon session access. The SQLite session
# file cannot be opened by two coroutines at once (causes "database is locked"),
# so every connect/operate/disconnect block must hold this lock.
_SESSION_LOCK = asyncio.Lock()

# Short-lived cache of account info keyed by session name. /auth/status is hit on
# every page load; without this it would connect to Telethon each time (slow and
# fights the poll lock). TTL keeps the page fast while staying reasonably fresh.
import time as _time
_ACCOUNT_INFO_CACHE: dict[str, tuple[float, dict | None]] = {}
_ACCOUNT_INFO_TTL = 300.0  # seconds


# ── Canonical Telethon session path ────────────────────────────────────────────
# The session name was passed bare to Telethon and resolved against os.getcwd(),
# so any change to how the backend was launched silently swapped the session file
# and flipped the UI back to "Connect Telegram Account" with no error. Pin it to
# one absolute data dir and migrate the newest existing (cwd-relative) session in
# once so the live login survives.
import os as _os
import shutil as _shutil
from pathlib import Path as _Path

_REPO_ROOT = _Path(__file__).resolve().parents[4]
_SESSION_DIR = _REPO_ROOT / "data" / "telegram"
_resolved_session_paths: dict[str, str] = {}


def _resolve_session_path(session_name: str) -> str:
    """Absolute Telethon session base (no .session suffix); cached, migrates once."""
    cached = _resolved_session_paths.get(session_name)
    if cached is not None:
        return cached
    if _os.path.isabs(session_name):
        _resolved_session_paths[session_name] = session_name
        return session_name
    target_dir = _SESSION_DIR
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        target_dir = _REPO_ROOT / "backend"
    target = target_dir / session_name
    target_session = _Path(f"{target}.session")
    if not target_session.exists():
        candidates = [
            _REPO_ROOT / "backend" / f"{session_name}.session",
            _REPO_ROOT / f"{session_name}.session",
        ]
        existing = [p for p in candidates if p.exists()]
        if existing:
            newest = max(existing, key=lambda p: p.stat().st_mtime)
            try:
                _shutil.copy2(newest, target_session)
                journal = newest.with_name(newest.name + "-journal")
                if journal.exists():
                    _shutil.copy2(journal, _Path(f"{target}.session-journal"))
            except Exception:
                pass
    result = str(target)
    _resolved_session_paths[session_name] = result
    return result


CORE_METHODS_URL = "https://core.telegram.org/methods"
METHOD_TEST_MODE_BINDING: Literal["binding"] = "binding"
METHOD_TEST_MODE_INVOKE_READONLY: Literal["invoke_readonly"] = "invoke_readonly"
READONLY_METHOD_ALLOWLIST = frozenset(
    {
        "help.getAppConfig",
        "help.getCdnConfig",
        "help.getConfig",
        "help.getNearestDc",
    }
)


@dataclass(slots=True)
class TelegramChannelInfo:
    title: str
    handle: str
    channel_id: str | None


@dataclass(slots=True)
class TelegramMessageItem:
    message_id: str
    posted_at: datetime | None
    text: str
    author_name: str | None


class TelegramProvider(Protocol):
    name: str

    def is_available(self) -> bool: ...

    async def ping(self) -> str: ...

    async def resolve_channel(self, channel_ref: str) -> TelegramChannelInfo: ...

    async def list_subscribed_channels(self, limit: int = 100) -> list[TelegramChannelInfo]: ...

    async def fetch_recent_messages(
        self,
        channel_ref: str,
        limit: int,
        min_message_id: str | None = None,
    ) -> list[TelegramMessageItem]: ...


class TelethonProvider:
    name = "telethon"

    def __init__(self, cfg: TelegramPluginConfig):
        self._cfg = cfg

    def is_available(self) -> bool:
        if not (self._cfg.api_id and self._cfg.api_hash):
            return False
        try:
            self._get_client_cls()
        except Exception:
            return False
        return True

    # ── internal helper ───────────────────────────────────────────────────
    def _session_base(self) -> str:
        """Absolute session base path — never cwd-relative (see _resolve_session_path)."""
        return _resolve_session_path(self._cfg.session_name)

    def _make_client(self):
        """Return an uninitialised TelegramClient (not yet connected)."""
        client_cls = self._get_client_cls()
        return client_cls(self._session_base(), self._cfg.api_id, self._cfg.api_hash)

    @asynccontextmanager
    async def _connected_client(self):
        """Acquire the global session lock, connect a client, disconnect on exit.

        Serialises Telethon SQLite session access to avoid "database is locked".
        """
        async with _SESSION_LOCK:
            client = self._make_client()
            await client.connect()
            try:
                yield client
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass

    async def _resolve_entity(self, client, channel_ref: str):
        """Resolve a channel/group entity from a username (@x) or a bare numeric id.

        Telethon's get_entity() treats a bare numeric string as a username lookup,
        which fails for channels referenced only by id. We try several marked-id
        forms and finally fall back to scanning dialogs (which forces the
        access_hash into the session cache so future calls succeed).
        """
        ref = (channel_ref or "").strip()
        if not ref:
            raise RuntimeError("Empty channel reference")

        # Username handle — resolve directly
        if ref.startswith("@") or not re.fullmatch(r"-?\d+", ref):
            return await client.get_entity(ref)

        raw = int(ref)
        candidates: list[Any] = []
        try:
            from telethon.tl.types import PeerChannel, PeerChat

            if raw > 0:
                # Raw channel id (no -100 prefix) — try common marked forms
                candidates = [
                    PeerChannel(raw),
                    int(f"-100{raw}"),
                    raw,
                    PeerChat(raw),
                ]
            else:
                candidates = [raw]
        except Exception:
            candidates = [raw]

        for cand in candidates:
            try:
                return await client.get_entity(cand)
            except Exception:
                continue

        # Fallback: scan dialogs to find the matching id (caches access_hash)
        async for dialog in client.iter_dialogs(limit=None):
            ent = getattr(dialog, "entity", None)
            if ent is not None and getattr(ent, "id", None) == raw:
                return ent

        raise RuntimeError(f"Channel id {ref} not found in your Telegram dialogs")

    # ── auth helpers ─────────────────────────────────────────────────────

    async def ping(self) -> str:
        """Quick connectivity check: returns account first_name via get_me()."""
        import os
        session_path = f"{self._session_base()}.session"
        if not os.path.exists(session_path):
            raise RuntimeError("Telethon: no session file — not authenticated")
        async with self._connected_client() as client:
            me = await client.get_me()
            if me is None:
                return "Telethon: connected (no account info)"
            name = getattr(me, "first_name", None) or getattr(me, "username", None) or str(me.id)
            return f"Telethon: connected as {name}"

    async def resolve_channel(self, channel_ref: str) -> TelegramChannelInfo:
        async with self._connected_client() as client:
            entity = await self._resolve_entity(client, channel_ref)
            username = getattr(entity, "username", None)
            handle = f"@{username}" if username else channel_ref
            channel_id = str(getattr(entity, "id", "")) or None
            title = getattr(entity, "title", None) or handle
            return TelegramChannelInfo(title=title, handle=handle, channel_id=channel_id)

    async def is_authenticated(self) -> bool:
        """Return True only if a valid session file exists and the session is authorised."""
        import os
        if not (self._cfg.api_id and self._cfg.api_hash):
            return False
        # Fast-path: no session file means definitely not authenticated
        session_path = f"{self._session_base()}.session"
        if not os.path.exists(session_path):
            return False
        try:
            async with self._connected_client() as client:
                return await client.is_user_authorized()
        except Exception:
            return False

    async def get_account_info(self, force: bool = False) -> dict | None:
        """Return basic info about the authenticated account, or None.

        Cached for `_ACCOUNT_INFO_TTL` seconds so the /telegram page (which polls
        auth status on load) doesn't connect to Telethon on every request.
        """
        import os
        if not (self._cfg.api_id and self._cfg.api_hash):
            return None
        session_path = f"{self._session_base()}.session"
        if not os.path.exists(session_path):
            _ACCOUNT_INFO_CACHE.pop(self._session_base(), None)
            return None

        # Serve from cache when fresh
        cached = _ACCOUNT_INFO_CACHE.get(self._session_base())
        if not force and cached is not None and (_time.time() - cached[0]) < _ACCOUNT_INFO_TTL:
            return cached[1]

        try:
            async with self._connected_client() as client:
                if not await client.is_user_authorized():
                    _ACCOUNT_INFO_CACHE[self._session_base()] = (_time.time(), None)
                    return None
                me = await client.get_me()
                if me is None:
                    _ACCOUNT_INFO_CACHE[self._session_base()] = (_time.time(), None)
                    return None
                info = {
                    "id": getattr(me, "id", None),
                    "phone": getattr(me, "phone", None),
                    "first_name": getattr(me, "first_name", None),
                    "username": getattr(me, "username", None),
                }
                _ACCOUNT_INFO_CACHE[self._session_base()] = (_time.time(), info)
                return info
        except Exception:
            # Don't poison the cache on transient errors; return last good value
            return cached[1] if cached is not None else None

    async def start_auth(self, phone_number: str) -> str:
        """Send OTP to phone_number; returns phone_code_hash (does NOT call start())."""
        async with self._connected_client() as client:
            result = await client.send_code_request(phone_number)
            phone_code_hash = result.phone_code_hash
            _PENDING_AUTH_CODES[phone_number] = phone_code_hash
            return phone_code_hash

    async def complete_auth(
        self,
        phone_number: str,
        phone_code_hash: str,
        code: str,
        password: str | None = None,
    ) -> dict:
        """Complete OTP auth; handles 2FA if `password` supplied.
        Session is saved to disk by Telethon after sign_in.
        """
        async with self._connected_client() as client:
            try:
                user = await client.sign_in(
                    phone=phone_number,
                    code=code,
                    phone_code_hash=phone_code_hash,
                )
            except Exception as exc:
                exc_name = type(exc).__name__
                if "SessionPasswordNeeded" in exc_name or "two-step" in str(exc).lower():
                    if password:
                        user = await client.sign_in(password=password)
                    else:
                        raise RuntimeError("2FA_REQUIRED") from exc
                else:
                    raise
            _PENDING_AUTH_CODES.pop(phone_number, None)
            me = user if user else await client.get_me()
            info = {
                "id": getattr(me, "id", None),
                "phone": getattr(me, "phone", None),
                "first_name": getattr(me, "first_name", None),
                "username": getattr(me, "username", None),
            }
            # Refresh the cache so /auth/status reflects the new login immediately
            _ACCOUNT_INFO_CACHE[self._session_base()] = (_time.time(), info)
            return info

    async def disconnect(self) -> None:
        """Log out and remove the local session file."""
        import os
        _ACCOUNT_INFO_CACHE.pop(self._session_base(), None)
        try:
            async with self._connected_client() as client:
                if await client.is_user_authorized():
                    await client.log_out()
        except Exception:
            pass
        # Remove the .session file so is_authenticated() returns False cleanly
        for ext in (".session", ".session-journal"):
            path = f"{self._session_base()}{ext}"
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass

    async def list_subscribed_channels(self, limit: int = 100) -> list[TelegramChannelInfo]:
        channels: list[TelegramChannelInfo] = []
        async with self._connected_client() as client:
            # Only keep channels and supergroups — skip private DMs (User entities)
            async for dialog in client.iter_dialogs(limit=None):
                entity = getattr(dialog, "entity", None)
                if entity is None:
                    continue

                if type(entity).__name__ == "User":
                    continue

                username = getattr(entity, "username", None)
                if username:
                    handle = f"@{username}"
                else:
                    entity_id = getattr(entity, "id", None)
                    handle = str(entity_id).strip() if entity_id is not None else ""

                if not handle:
                    continue

                title = getattr(entity, "title", None) or getattr(dialog, "name", None) or handle
                channel_id = str(getattr(entity, "id", "")) or None
                channels.append(TelegramChannelInfo(title=title, handle=handle, channel_id=channel_id))

        return _dedupe_channels(channels, limit)

    async def fetch_recent_messages(
        self,
        channel_ref: str,
        limit: int,
        min_message_id: str | None = None,
    ) -> list[TelegramMessageItem]:
        min_id_int = _as_int(min_message_id)
        async with self._connected_client() as client:
            entity = await self._resolve_entity(client, channel_ref)
            messages = await client.get_messages(entity, limit=limit)

        items: list[TelegramMessageItem] = []
        for msg in reversed(messages):
            text = getattr(msg, "message", None)
            if not text:
                continue
            msg_id = str(getattr(msg, "id", ""))
            if not msg_id:
                continue
            msg_id_int = _as_int(msg_id)
            if min_id_int is not None and msg_id_int is not None and msg_id_int <= min_id_int:
                continue
            items.append(
                TelegramMessageItem(
                    message_id=msg_id,
                    posted_at=_coerce_datetime(getattr(msg, "date", None)),
                    text=text,
                    author_name=None,
                )
            )
        return items

    @staticmethod
    def _get_client_cls():
        try:
            from telethon import TelegramClient
        except ImportError as exc:
            raise RuntimeError(
                "Telethon is not installed. Install it with 'pip install telethon'."
            ) from exc
        return TelegramClient

    def inspect_method_binding_details(self, method_name: str) -> tuple[bool, str | None, str, int]:
        request_cls, binding, error, required_count = self._resolve_request_binding(method_name)
        if error:
            return False, None, error, 0
        return (
            True,
            binding,
            f"Resolved binding with {required_count} required arg(s)",
            required_count,
        )

    def _resolve_request_binding(
        self,
        method_name: str,
    ) -> tuple[type[Any] | None, str | None, str | None, int]:
        """Resolve a Telethon request class for a core API method."""
        if not method_name or "." not in method_name:
            return None, None, "Unsupported method format", 0

        namespace, method = method_name.split(".", 1)
        if not namespace or not method:
            return None, None, "Invalid method format", 0

        try:
            self._get_client_cls()
        except Exception as exc:
            return None, None, f"Telethon unavailable: {exc}", 0

        try:
            module = __import__(f"telethon.tl.functions.{namespace}", fromlist=["*"])
        except ModuleNotFoundError:
            return None, None, f"Namespace '{namespace}' not found", 0
        except Exception as exc:
            return None, None, f"Unable to import namespace '{namespace}': {exc}", 0

        class_name = f"{_to_pascal_case(method)}Request"
        request_cls = getattr(module, class_name, None)
        if request_cls is None:
            return None, None, f"Request class '{class_name}' not found", 0

        binding = f"{module.__name__}.{class_name}"
        required_count = 0
        try:
            sig = inspect.signature(request_cls.__init__)
            required_params = [
                param
                for param in list(sig.parameters.values())[1:]
                if param.kind
                in {
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.KEYWORD_ONLY,
                }
                and param.default is inspect.Parameter.empty
            ]
            required_count = len(required_params)
        except Exception:
            required_count = 0

        return request_cls, binding, None, required_count

    def inspect_method_binding(self, method_name: str) -> tuple[bool, str | None, str]:
        """Check whether a core Telegram method has a Telethon request binding.

        This validates method coverage without invoking remote side effects.
        """
        ok, binding, message, _required_count = self.inspect_method_binding_details(method_name)
        return ok, binding, message

    async def invoke_readonly_method(self, method_name: str) -> tuple[bool, str]:
        """Invoke an allowlisted read-only Telegram method with strict safeguards."""
        request_cls, binding, error, required_count = self._resolve_request_binding(method_name)
        if error:
            return False, error
        if required_count > 0:
            return False, f"Invocation blocked: method requires {required_count} arg(s)"
        if request_cls is None:
            return False, "Invocation blocked: request binding unavailable"

        try:
            request = request_cls()
        except Exception as exc:
            return False, f"Invocation blocked: failed to instantiate request ({exc})"

        client_cls = self._get_client_cls()
        try:
            async with client_cls(self._session_base(), self._cfg.api_id, self._cfg.api_hash) as client:
                await client(request)
        except Exception as exc:
            return False, f"Invocation failed: {exc}"

        return True, f"Invoked safely via {binding}"


class BotApiProvider:
    name = "bot_api"

    def __init__(self, cfg: TelegramPluginConfig):
        self._cfg = cfg

    def is_available(self) -> bool:
        return bool(self._cfg.bot_token)

    async def ping(self) -> str:
        """Quick connectivity check via Bot API getMe."""
        data = await self._request("getMe", {})
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "Bot API error"))
        result = data.get("result") or {}
        username = result.get("username") or result.get("first_name") or str(result.get("id", "?"))
        return f"Bot API: connected as @{username}"

    async def resolve_channel(self, channel_ref: str) -> TelegramChannelInfo:
        data = await self._request("getChat", {"chat_id": channel_ref})
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "Failed to resolve chat"))

        chat = data.get("result") or {}
        username = chat.get("username")
        handle = f"@{username}" if username else channel_ref
        return TelegramChannelInfo(
            title=chat.get("title") or handle,
            handle=handle,
            channel_id=str(chat.get("id")) if chat.get("id") is not None else None,
        )

    async def list_subscribed_channels(self, limit: int = 100) -> list[TelegramChannelInfo]:
        data = await self._request(
            "getUpdates",
            {
                "limit": min(max(limit, 1), 100),
                "allowed_updates": ["channel_post", "edited_channel_post"],
            },
        )
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "Failed to fetch updates"))

        channels: list[TelegramChannelInfo] = []
        for item in data.get("result") or []:
            message = item.get("channel_post") or item.get("edited_channel_post")
            if not isinstance(message, dict):
                continue

            chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
            username = str(chat.get("username") or "").strip()
            chat_id = chat.get("id")

            handle = f"@{username}" if username else (str(chat_id).strip() if chat_id is not None else "")
            if not handle:
                continue

            title = str(chat.get("title") or handle).strip() or handle
            channel_id = str(chat_id).strip() if chat_id is not None else None
            channels.append(TelegramChannelInfo(title=title, handle=handle, channel_id=channel_id))

        return _dedupe_channels(channels, limit)

    async def fetch_recent_messages(
        self,
        channel_ref: str,
        limit: int,
        min_message_id: str | None = None,
    ) -> list[TelegramMessageItem]:
        min_id_int = _as_int(min_message_id)
        data = await self._request(
            "getUpdates",
            {
                "limit": min(limit, 100),
                "allowed_updates": ["channel_post", "edited_channel_post"],
            },
        )
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "Failed to fetch updates"))

        channel_key = channel_ref.lower().lstrip("@")
        matches: list[TelegramMessageItem] = []
        for item in data.get("result") or []:
            message = item.get("channel_post") or item.get("edited_channel_post")
            if not message:
                continue

            chat = message.get("chat") or {}
            username = str(chat.get("username") or "").lower()
            chat_id = str(chat.get("id") or "")
            if channel_key not in {username, chat_id}:
                continue

            text = message.get("text") or message.get("caption")
            if not text:
                continue

            msg_id_int = _as_int(message.get("message_id"))
            if min_id_int is not None and msg_id_int is not None and msg_id_int <= min_id_int:
                continue

            posted_at = _parse_timestamp(message.get("date"))
            matches.append(
                TelegramMessageItem(
                    message_id=str(message.get("message_id")),
                    posted_at=posted_at,
                    text=text,
                    author_name=chat.get("title") or chat.get("username"),
                )
            )

        return matches[-limit:]

    async def _request(self, method: str, payload: dict) -> dict:
        if not self._cfg.bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")

        url = f"https://api.telegram.org/bot{self._cfg.bot_token}/{method}"
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            return response.json()


class TelegramMcpProvider:
    name = "telegram_mcp"

    def __init__(self, cfg: TelegramPluginConfig):
        self._cfg = cfg

    def is_available(self) -> bool:
        return bool(self._cfg.mcp_chat_id)

    async def ping(self) -> str:
        """Quick connectivity check: fetch 1 message via MCP to verify credentials."""
        if not self._cfg.mcp_chat_id:
            raise RuntimeError("TELEGRAM_MCP_CHAT_ID is not configured")
        payload = await self._request_messages(limit=1)
        msg_count = len(payload.get("messages") or [])
        return f"MCP: connected (chat_id={self._cfg.mcp_chat_id}, received {msg_count} message(s))"

    async def resolve_channel(self, channel_ref: str) -> TelegramChannelInfo:
        if not self._cfg.mcp_chat_id:
            raise RuntimeError("TELEGRAM_MCP_CHAT_ID is not configured")

        # Probe the MCP endpoint so verify_on_create can fail early on bad config.
        await self._request_messages(limit=1)
        handle = channel_ref or str(self._cfg.mcp_chat_id)
        return TelegramChannelInfo(
            title=handle,
            handle=handle,
            channel_id=str(self._cfg.mcp_chat_id),
        )

    async def list_subscribed_channels(self, limit: int = 100) -> list[TelegramChannelInfo]:
        payload = await self._request_messages(limit=limit)
        rows = self._extract_message_rows(payload)
        channels: list[TelegramChannelInfo] = []

        for row in rows:
            if not isinstance(row, dict):
                continue

            chat = row.get("chat") if isinstance(row.get("chat"), dict) else {}
            username = str(chat.get("username") or "").strip()
            chat_id = chat.get("id")
            title = str(chat.get("title") or "").strip()

            handle = f"@{username}" if username else (str(chat_id).strip() if chat_id is not None else "")
            if not handle:
                continue

            channels.append(
                TelegramChannelInfo(
                    title=title or handle,
                    handle=handle,
                    channel_id=str(chat_id).strip() if chat_id is not None else None,
                )
            )

        if not channels and self._cfg.mcp_chat_id:
            fallback = str(self._cfg.mcp_chat_id).strip()
            channels.append(
                TelegramChannelInfo(
                    title=fallback,
                    handle=fallback,
                    channel_id=fallback,
                )
            )

        return _dedupe_channels(channels, limit)

    async def fetch_recent_messages(
        self,
        channel_ref: str,
        limit: int,
        min_message_id: str | None = None,
    ) -> list[TelegramMessageItem]:
        if not self._cfg.mcp_chat_id:
            raise RuntimeError("TELEGRAM_MCP_CHAT_ID is not configured")

        min_id_int = _as_int(min_message_id)
        payload = await self._request_messages(limit=limit)
        rows = self._extract_message_rows(payload)

        target = (channel_ref or "").strip().lower().lstrip("@")
        items: list[TelegramMessageItem] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if not self._matches_channel(row, target):
                continue

            text = self._extract_text(row)
            if not text:
                continue

            message_id = self._extract_message_id(row)
            if not message_id:
                continue

            msg_id_int = _as_int(message_id)
            if min_id_int is not None and msg_id_int is not None and msg_id_int <= min_id_int:
                continue

            items.append(
                TelegramMessageItem(
                    message_id=message_id,
                    posted_at=_coerce_datetime(
                        row.get("date") or row.get("timestamp") or row.get("created_at")
                    ),
                    text=text,
                    author_name=self._extract_author(row),
                )
            )

        items.sort(key=lambda item: _as_int(item.message_id) or 0)
        return items[-max(1, limit) :]

    async def _request_messages(self, limit: int) -> dict[str, Any] | list[Any]:
        if not self._cfg.mcp_chat_id:
            raise RuntimeError("TELEGRAM_MCP_CHAT_ID is not configured")

        url = f"{self._cfg.mcp_server_url.rstrip('/')}/api/messages"
        headers = {"X-Chat-Id": self._cfg.mcp_chat_id}
        params = {"limit": max(1, min(int(limit), 50))}
        timeout = max(5, int(self._cfg.mcp_timeout_seconds))

        async with httpx.AsyncClient(timeout=float(timeout)) as client:
            response = await client.get(url, headers=headers, params=params)
            response.raise_for_status()
            payload = response.json()

        if isinstance(payload, dict) and payload.get("error"):
            raise RuntimeError(str(payload.get("error")))
        return payload

    @staticmethod
    def _extract_message_rows(payload: dict[str, Any] | list[Any]) -> list[Any]:
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return []
        for key in ("messages", "result", "data", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return []

    @staticmethod
    def _extract_text(row: dict[str, Any]) -> str | None:
        text = row.get("text") or row.get("message") or row.get("caption")
        if isinstance(text, str):
            text = text.strip()
        return text or None

    @staticmethod
    def _extract_message_id(row: dict[str, Any]) -> str | None:
        for key in ("message_id", "id", "update_id"):
            value = row.get(key)
            if value is not None:
                text = str(value).strip()
                if text:
                    return text
        return None

    @staticmethod
    def _extract_author(row: dict[str, Any]) -> str | None:
        chat = row.get("chat") if isinstance(row.get("chat"), dict) else {}
        sender = row.get("from") if isinstance(row.get("from"), dict) else {}
        candidates = [
            chat.get("title"),
            chat.get("username"),
            sender.get("username"),
            sender.get("first_name"),
            row.get("author"),
            row.get("author_name"),
            row.get("sender_name"),
        ]
        for value in candidates:
            text = str(value).strip() if value is not None else ""
            if text:
                return text
        return None

    @staticmethod
    def _matches_channel(row: dict[str, Any], target: str) -> bool:
        if not target:
            return True

        chat = row.get("chat") if isinstance(row.get("chat"), dict) else {}
        if not chat:
            return True

        candidates = {
            _normalize_candidate(chat.get("username")),
            _normalize_candidate(chat.get("id")),
            _normalize_candidate(chat.get("title")),
        }
        candidates.discard(None)
        if not candidates:
            return True
        return target in candidates


class TelegramProviderRegistry:
    def __init__(self, cfg: TelegramPluginConfig):
        self._cfg = cfg
        self._providers: list[TelegramProvider] = [
            TelethonProvider(cfg),
            BotApiProvider(cfg),
            TelegramMcpProvider(cfg),
        ]
        self._core_methods_cache: list[str] = []

    def status(self) -> list[dict[str, str | bool]]:
        rows: list[dict[str, str | bool]] = []
        for provider in self._providers:
            available = provider.is_available()
            rows.append(
                {
                    "name": provider.name,
                    "available": available,
                    "configured": available,
                }
            )
        return rows

    async def resolve_channel(self, channel_ref: str, provider_hint: str = "auto") -> tuple[TelegramChannelInfo, str]:
        errors: list[str] = []
        for provider in self._iter_candidates(provider_hint):
            try:
                info = await provider.resolve_channel(channel_ref)
                return info, provider.name
            except Exception as exc:
                errors.append(f"{provider.name}: {exc}")
        raise RuntimeError("Unable to resolve Telegram channel. " + " | ".join(errors))

    async def list_subscribed_channels(
        self,
        limit: int,
        provider_hint: str = "auto",
    ) -> tuple[list[TelegramChannelInfo], str]:
        errors: list[str] = []
        first_empty_provider: str | None = None
        for provider in self._iter_candidates(provider_hint):
            try:
                rows = await provider.list_subscribed_channels(limit=max(1, limit))
                deduped = _dedupe_channels(rows, limit)
                if deduped:
                    return deduped, provider.name
                if provider_hint != "auto":
                    return deduped, provider.name
                if first_empty_provider is None:
                    first_empty_provider = provider.name
            except Exception as exc:
                errors.append(f"{provider.name}: {exc}")
        if first_empty_provider is not None:
            return [], first_empty_provider
        if not errors:
            raise RuntimeError("Unable to list subscribed Telegram channels. No available providers are configured.")
        raise RuntimeError("Unable to list subscribed Telegram channels. " + " | ".join(errors))

    async def test_connection(self, provider_hint: str = "auto") -> list[dict]:
        """Try to ping each available provider.

        Returns a list of {provider, ok, message} dicts so the caller can
        report partial success (e.g. MCP works, Telethon not configured).
        """
        results: list[dict] = []
        for provider in self._providers:
            if not provider.is_available():
                results.append({"provider": provider.name, "ok": False, "message": "Not configured"})
                continue
            try:
                msg = await provider.ping()
                results.append({"provider": provider.name, "ok": True, "message": msg})
            except Exception as exc:
                results.append({"provider": provider.name, "ok": False, "message": str(exc)})
        return results

    async def get_core_methods(self, refresh: bool = False) -> list[str]:
        if self._core_methods_cache and not refresh:
            return list(self._core_methods_cache)

        methods = await _fetch_core_method_names()
        if not methods:
            raise RuntimeError("No Telegram core methods discovered from core.telegram.org/methods")

        self._core_methods_cache = methods
        return list(self._core_methods_cache)

    async def build_core_methods_catalog(
        self,
        refresh: bool = False,
        limit: int | None = None,
    ) -> dict[str, Any]:
        names = await self.get_core_methods(refresh=refresh)
        if limit is not None:
            names = names[: max(1, limit)]

        telethon_provider = self._get_telethon_provider()
        methods: list[dict[str, Any]] = []
        for method_name in names:
            supported: list[str] = []
            binding: str | None = None
            notes = ""

            if telethon_provider is not None:
                ok, resolved_binding, message = telethon_provider.inspect_method_binding(method_name)
                if ok:
                    supported.append("telethon")
                    binding = resolved_binding
                notes = message

            methods.append(
                {
                    "name": method_name,
                    "namespace": method_name.split(".", 1)[0] if "." in method_name else None,
                    "provider_supported": supported,
                    "binding": binding,
                    "notes": notes or None,
                }
            )

        return {
            "source_url": CORE_METHODS_URL,
            "total_methods": len(names),
            "fetched_at": now_sast(),
            "methods": methods,
        }

    async def test_core_methods(
        self,
        provider_hint: str = "auto",
        refresh: bool = False,
        limit: int | None = None,
        mode: Literal["binding", "invoke_readonly"] = METHOD_TEST_MODE_BINDING,
    ) -> dict[str, Any]:
        mode_value = (mode or METHOD_TEST_MODE_BINDING).strip().lower()
        if mode_value not in {METHOD_TEST_MODE_BINDING, METHOD_TEST_MODE_INVOKE_READONLY}:
            raise ValueError("Unsupported test mode. Use 'binding' or 'invoke_readonly'.")

        names = await self.get_core_methods(refresh=refresh)
        if limit is not None:
            names = names[: max(1, limit)]

        provider_name, telethon_provider = self._resolve_methods_provider(provider_hint)
        results: list[dict[str, Any]] = []

        for method_name in names:
            if provider_name != "telethon" or telethon_provider is None:
                results.append(
                    {
                        "method": method_name,
                        "provider": provider_name,
                        "ok": False,
                        "status": "unsupported",
                        "message": "Method testing is currently supported via Telethon only",
                    }
                )
                continue

            if mode_value == METHOD_TEST_MODE_INVOKE_READONLY:
                if method_name not in READONLY_METHOD_ALLOWLIST:
                    results.append(
                        {
                            "method": method_name,
                            "provider": "telethon",
                            "ok": False,
                            "status": "unsupported",
                            "message": "Live invocation blocked: method not in read-only allowlist",
                        }
                    )
                    continue

                ok, _binding, inspect_message, required_count = telethon_provider.inspect_method_binding_details(method_name)
                if not ok:
                    results.append(
                        {
                            "method": method_name,
                            "provider": "telethon",
                            "ok": False,
                            "status": "unsupported",
                            "message": inspect_message,
                        }
                    )
                    continue

                if required_count > 0:
                    results.append(
                        {
                            "method": method_name,
                            "provider": "telethon",
                            "ok": False,
                            "status": "unsupported",
                            "message": f"Live invocation blocked: method requires {required_count} arg(s)",
                        }
                    )
                    continue

                invoked_ok, invoke_message = await telethon_provider.invoke_readonly_method(method_name)
                results.append(
                    {
                        "method": method_name,
                        "provider": "telethon",
                        "ok": invoked_ok,
                        "status": "supported" if invoked_ok else "error",
                        "message": invoke_message,
                    }
                )
                continue

            ok, _binding, message = telethon_provider.inspect_method_binding(method_name)
            results.append(
                {
                    "method": method_name,
                    "provider": "telethon",
                    "ok": ok,
                    "status": "supported" if ok else "unsupported",
                    "message": message,
                }
            )

        passed = sum(1 for row in results if row["ok"])
        failed = sum(1 for row in results if row["status"] == "error")
        unsupported = sum(1 for row in results if row["status"] == "unsupported")

        return {
            "source_url": CORE_METHODS_URL,
            "provider": provider_name,
            "mode": mode_value,
            "readonly_allowlist": sorted(READONLY_METHOD_ALLOWLIST),
            "summary": {
                "total_methods": len(names),
                "tested_methods": len(results),
                "passed": passed,
                "failed": failed,
                "unsupported": unsupported,
            },
            "results": results,
        }

    async def fetch_recent_messages(
        self,
        channel_ref: str,
        limit: int,
        min_message_id: str | None,
        provider_hint: str = "auto",
    ) -> tuple[list[TelegramMessageItem], str]:
        errors: list[str] = []
        for provider in self._iter_candidates(provider_hint):
            try:
                rows = await provider.fetch_recent_messages(
                    channel_ref=channel_ref,
                    limit=limit,
                    min_message_id=min_message_id,
                )
                return rows, provider.name
            except Exception as exc:
                errors.append(f"{provider.name}: {exc}")
        raise RuntimeError("Unable to fetch Telegram messages. " + " | ".join(errors))

    def _iter_candidates(self, provider_hint: str):
        provider_hint = (provider_hint or "auto").lower()
        if provider_hint != "auto":
            for provider in self._providers:
                if provider.name == provider_hint and provider.is_available():
                    yield provider
            return
        for provider in self._providers:
            if provider.is_available():
                yield provider

    def _get_telethon_provider(self) -> TelethonProvider | None:
        for provider in self._providers:
            if isinstance(provider, TelethonProvider):
                return provider
        return None

    def _resolve_methods_provider(self, provider_hint: str) -> tuple[str, TelethonProvider | None]:
        hint = (provider_hint or "auto").lower()
        telethon_provider = self._get_telethon_provider()

        if hint == "telethon":
            return "telethon", telethon_provider

        if hint != "auto":
            return hint, telethon_provider

        if telethon_provider is not None and telethon_provider.is_available():
            return "telethon", telethon_provider

        for provider in self._providers:
            if provider.is_available():
                return provider.name, telethon_provider
        return "auto", telethon_provider


async def _fetch_core_method_names() -> list[str]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(CORE_METHODS_URL)
        response.raise_for_status()
    return _parse_core_methods_html(response.text)


def _parse_core_methods_html(html: str) -> list[str]:
    names = {
        match.strip()
        for match in re.findall(r'href=["\'](?:https?://core\.telegram\.org)?/method/([a-zA-Z0-9_.]+)["\']', html)
        if match and "." in match
    }
    return sorted(names)


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _parse_timestamp(value: object) -> datetime | None:
    as_int = _as_int(value)
    if as_int is None:
        return None
    return datetime.fromtimestamp(as_int, timezone.utc).replace(tzinfo=None)


def _coerce_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    if isinstance(value, str):
        text = value.strip()
        if text:
            if text.endswith("Z"):
                text = f"{text[:-1]}+00:00"
            try:
                parsed = datetime.fromisoformat(text)
                if parsed.tzinfo is not None:
                    return parsed.astimezone(timezone.utc).replace(tzinfo=None)
                return parsed
            except ValueError:
                pass
    return _parse_timestamp(value)


def _normalize_candidate(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower().lstrip("@")
    return text or None


def _dedupe_channels(channels: list[TelegramChannelInfo], limit: int) -> list[TelegramChannelInfo]:
    unique: dict[str, TelegramChannelInfo] = {}
    for channel in channels:
        key = _normalize_candidate(channel.handle) or _normalize_candidate(channel.channel_id)
        if not key or key in unique:
            continue
        unique[key] = channel
    rows = sorted(unique.values(), key=lambda item: (item.title.lower(), item.handle.lower()))
    return rows[: max(1, limit)]


def _to_pascal_case(value: str) -> str:
    chunks = [chunk for chunk in value.replace("-", "_").split("_") if chunk]
    if len(chunks) > 1:
        return "".join(chunk[:1].upper() + chunk[1:] for chunk in chunks)
    if not value:
        return value
    return value[:1].upper() + value[1:]
