"""
VibeTradingPlugin — HTTP Client

Thin async wrapper around the vibe-trading REST API running on :8899.
All calls fail-open: exceptions are caught and logged; callers receive
a graceful error dict instead of a 500.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional
from loguru import logger

try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False

from plugins.VibeTradingPlugin.backend.config import vibe_config


def _headers() -> Dict[str, str]:
    h: Dict[str, str] = {"Content-Type": "application/json"}
    if vibe_config.api_auth_key:
        h["Authorization"] = f"Bearer {vibe_config.api_auth_key}"
    return h


async def _get(path: str, params: Optional[Dict] = None, timeout: Optional[int] = None) -> Any:
    if not _HTTPX_AVAILABLE:
        return {"error": "httpx not installed"}
    url = vibe_config.url.rstrip("/") + path
    t = timeout or vibe_config.request_timeout
    try:
        async with httpx.AsyncClient(headers=_headers(), timeout=t) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            return r.json()
    except Exception as exc:
        logger.warning(f"[VibeTradingClient] GET {path} failed: {exc}")
        return {"error": str(exc)}


async def _post(path: str, json: Any = None, params: Optional[Dict] = None,
                timeout: Optional[int] = None) -> Any:
    if not _HTTPX_AVAILABLE:
        return {"error": "httpx not installed"}
    url = vibe_config.url.rstrip("/") + path
    t = timeout or vibe_config.request_timeout
    try:
        async with httpx.AsyncClient(headers=_headers(), timeout=t) as client:
            r = await client.post(url, json=json, params=params)
            r.raise_for_status()
            return r.json()
    except Exception as exc:
        logger.warning(f"[VibeTradingClient] POST {path} failed: {exc}")
        return {"error": str(exc)}


async def _delete(path: str, params: Optional[Dict] = None) -> Any:
    if not _HTTPX_AVAILABLE:
        return {"error": "httpx not installed"}
    url = vibe_config.url.rstrip("/") + path
    try:
        async with httpx.AsyncClient(headers=_headers(), timeout=vibe_config.request_timeout) as client:
            r = await client.delete(url, params=params)
            r.raise_for_status()
            return r.json()
    except Exception as exc:
        logger.warning(f"[VibeTradingClient] DELETE {path} failed: {exc}")
        return {"error": str(exc)}


# ── Public helpers ──────────────────────────────────────────────────────────

async def health() -> Dict:
    try:
        result = await asyncio.wait_for(_get("/health"), timeout=5)
        return result
    except asyncio.TimeoutError:
        return {"error": "timeout"}


async def list_runs() -> Any:
    return await _get("/runs")


async def get_run(run_id: str) -> Any:
    return await _get(f"/runs/{run_id}")


async def get_run_pine(run_id: str) -> Any:
    return await _get(f"/runs/{run_id}/pine")


async def create_session() -> Any:
    return await _post("/sessions")


async def session_message(session_id: str, content: str) -> Any:
    return await _post(f"/sessions/{session_id}/messages", json={"role": "user", "content": content},
                       timeout=vibe_config.long_timeout)


async def list_swarm_presets() -> Any:
    return await _get("/swarm/presets")


async def run_swarm(preset: str, variables: Dict) -> Any:
    return await _post("/swarm/runs", json={"preset": preset, "variables": variables},
                       timeout=vibe_config.long_timeout)


async def get_swarm_run(run_id: str) -> Any:
    return await _get(f"/swarm/runs/{run_id}")


async def list_alphas(params: Optional[Dict] = None) -> Any:
    return await _get("/alpha/list", params=params)


async def bench_alphas(payload: Dict) -> Any:
    return await _post("/alpha/bench", json=payload, timeout=vibe_config.long_timeout)


async def get_bench_stream(job_id: str) -> Any:
    """Fetch bench results (non-streaming fallback)."""
    return await _get(f"/alpha/bench/{job_id}/stream", timeout=vibe_config.long_timeout)


async def create_scheduled_run(prompt: str, schedule: str, config: Optional[Dict] = None) -> Any:
    payload: Dict = {"prompt": prompt, "schedule": schedule}
    if config:
        payload["config"] = config
    return await _post("/scheduled-runs", json=payload)


async def list_scheduled_runs() -> Any:
    return await _get("/scheduled-runs")


async def delete_scheduled_run(job_id: str) -> Any:
    return await _delete(f"/scheduled-runs/{job_id}")


async def research(prompt: str, symbol: Optional[str] = None) -> Any:
    """Start a one-shot research session and return the result."""
    session = await create_session()
    if "error" in session:
        return session
    sid = session.get("id")
    if not sid:
        return {"error": "no session id returned", "raw": session}
    full_prompt = f"[Symbol: {symbol}] {prompt}" if symbol else prompt
    return await session_message(sid, full_prompt)


async def backtest(symbol: str, strategy: str, timeframe: Optional[str] = None) -> Any:
    """Run a backtest via natural-language research prompt."""
    tf_hint = f" on {timeframe} timeframe" if timeframe else ""
    prompt = (
        f"Backtest the following strategy on {symbol}{tf_hint}: {strategy}. "
        "Show Sharpe ratio, max drawdown, total return, and export Pine Script."
    )
    return await research(prompt, symbol=symbol)
