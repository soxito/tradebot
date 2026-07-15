"""OpenManusPlugin — MCP client for OpenManus SSE server.

Handles:
  - Health check (GET /health or HEAD to SSE endpoint)
  - MCP tool call via JSON-RPC POST to /messages
  - Session ID negotiation from SSE handshake
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

import httpx
from loguru import logger

from plugins.OpenManusPlugin.backend.config import openmanus_config

# Cached session id from the last SSE handshake.
# A real MCP-SSE client would maintain a persistent EventSource connection;
# here we use a short-lived session_id obtained once and reused until invalid.
_session_id: str | None = None
_session_id_lock = asyncio.Lock()


async def _negotiate_session() -> str | None:
    """Attempt to obtain an MCP SSE session_id.

    Opens the SSE endpoint, reads the first 'endpoint' event which carries
    a ?session_id= query param, then closes the stream.
    Returns the session_id string or None on failure.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            async with client.stream("GET", openmanus_config.sse_url) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if line.startswith("data:"):
                        data = line[5:].strip()
                        # OpenManus SSE sends the messages endpoint URI as data
                        # e.g. data: /messages?session_id=<uuid>
                        if "session_id=" in data:
                            sid = data.split("session_id=")[-1].split("&")[0].strip()
                            return sid
                        break  # if first data event doesn't have session_id, stop
    except Exception as exc:  # noqa: BLE001
        logger.debug("[OpenManus] SSE session negotiation failed: {}", exc)
    return None


async def _get_session() -> str | None:
    """Return a cached session_id, re-negotiating if necessary."""
    global _session_id
    async with _session_id_lock:
        if _session_id is None:
            _session_id = await _negotiate_session()
        return _session_id


async def mcp_health() -> bool:
    """Return True if the OpenManus MCP server responds."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{openmanus_config.sse_base_url}/health")
            return resp.status_code < 400
    except Exception:  # noqa: BLE001
        pass
    # Fallback: try HEAD on SSE endpoint
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.head(openmanus_config.sse_url)
            return resp.status_code < 500
    except Exception:  # noqa: BLE001
        return False


async def mcp_list_tools() -> list[dict[str, Any]]:
    """Call the MCP tools/list method and return available tools."""
    return await _mcp_call(method="tools/list", params={})


async def mcp_run_agent(task: str, context: list[dict] | None = None) -> dict[str, Any]:
    """Call the run_agent tool on the OpenManus MCP server.

    Maps a plain-text task + optional conversation context to an OpenManus
    agent execution. Returns the raw MCP response dict.
    """
    params = {
        "name": "run_agent",
        "arguments": {
            "task": task,
            **({"context": context} if context else {}),
        },
    }
    return await _mcp_call(method="tools/call", params=params)


async def mcp_chat(messages: list[dict[str, str]]) -> dict[str, Any]:
    """Route an OpenAI-style chat message list to OpenManus.

    Converts the messages array to a single task string (last user message)
    with conversation history as context, then calls run_agent.
    """
    # Extract the final user message as the task
    user_msgs = [m for m in messages if m.get("role") == "user"]
    task = user_msgs[-1]["content"] if user_msgs else ""

    # Pass full history as context so OpenManus agents have conversation
    context = [{"role": m.get("role", "user"), "content": str(m.get("content", ""))}
               for m in messages[:-1]] if len(messages) > 1 else None

    return await mcp_run_agent(task=task, context=context)


async def _mcp_call(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Make a JSON-RPC 2.0 call to the OpenManus MCP messages endpoint.

    Handles session negotiation automatically.
    """
    global _session_id

    session = await _get_session()

    msg_url = openmanus_config.messages_url
    if session:
        msg_url = f"{msg_url}?session_id={session}"

    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": method,
        "params": params,
    }

    try:
        async with httpx.AsyncClient(timeout=openmanus_config.timeout_s) as client:
            resp = await client.post(
                msg_url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code == 404 and session:
                # Session expired — clear and retry once without session_id
                logger.debug("[OpenManus] session_id expired, renegotiating")
                _session_id = None
                new_session = await _get_session()
                retry_url = (
                    f"{openmanus_config.messages_url}?session_id={new_session}"
                    if new_session
                    else openmanus_config.messages_url
                )
                resp = await client.post(
                    retry_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[OpenManus] MCP call '{}' failed: {}", method, exc)
        return {"error": str(exc)}
