"""HTTP client for the TradingAgents sidecar service.

Used where a *synchronous* result is needed (pre-trade validation): start a
run on the sidecar, poll until it settles, return the final snapshot. The
interactive UI path uses the proxy router's SSE stream instead.
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx
from loguru import logger

from app.core.config import settings

_POLL_INTERVAL_S = 5.0
_DEFAULT_MAX_WAIT_S = 45 * 60


class TradingAgentsSidecarError(RuntimeError):
    """Sidecar unreachable or rejected the run."""


def _url(path: str) -> str:
    return f"{settings.TRADINGAGENTS_SERVICE_URL.rstrip('/')}{path}"


async def health() -> dict[str, Any]:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(_url("/health"), timeout=5)
            resp.raise_for_status()
            data = resp.json()
        return {"ok": True, **data}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:300]}


async def start_run(payload: dict[str, Any]) -> dict[str, Any]:
    """Start a run; returns {run_id, ticker, mapped_ticker, trade_date}."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _url("/api/runs"),
            json=payload,
            timeout=settings.TRADINGAGENTS_PROXY_TIMEOUT_START,
        )
    if resp.status_code == 429:
        raise TradingAgentsSidecarError(str(resp.json().get("detail") or "sidecar busy"))
    if resp.status_code >= 400:
        raise TradingAgentsSidecarError(f"start failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()


async def get_run(run_id: str) -> dict[str, Any] | None:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                _url(f"/api/runs/{run_id}"),
                timeout=settings.TRADINGAGENTS_PROXY_TIMEOUT_READ,
            )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[TradingAgents] get_run({run_id}) failed: {exc}")
        return None


async def run_analysis_blocking(
    payload: dict[str, Any],
    max_wait_s: float = _DEFAULT_MAX_WAIT_S,
    on_phase=None,
) -> dict[str, Any]:
    """Start a run and block (async-friendly polling) until it finishes.

    Returns the final sidecar snapshot (status done/error, result payload).
    Raises :class:`TradingAgentsSidecarError` if the run cannot be started.
    """
    started = await start_run(payload)
    run_id = started["run_id"]
    deadline = asyncio.get_event_loop().time() + max_wait_s
    last_phase: str | None = None

    while True:
        await asyncio.sleep(_POLL_INTERVAL_S)
        snapshot = await get_run(run_id)
        if snapshot is None:
            raise TradingAgentsSidecarError(f"run {run_id} vanished from sidecar")
        phase = snapshot.get("phase")
        if on_phase and phase and phase != last_phase:
            last_phase = phase
            try:
                on_phase(phase)
            except Exception:  # noqa: BLE001 - progress hooks are best-effort
                pass
        if snapshot.get("status") in ("done", "error"):
            snapshot.setdefault("run_id", run_id)
            return snapshot
        if asyncio.get_event_loop().time() > deadline:
            raise TradingAgentsSidecarError(f"run {run_id} timed out after {int(max_wait_s)}s")
