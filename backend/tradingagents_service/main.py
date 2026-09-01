"""TradingAgents sidecar — HTTP + SSE API.

Endpoints:
    GET  /health                     liveness + active run count
    POST /api/runs                   start an analysis run
    GET  /api/runs                   list recent runs
    GET  /api/runs/{id}              one run (optionally with all events)
    GET  /api/runs/{id}/stream       SSE: live progress, dialogue, result
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from pydantic import BaseModel, Field

from . import runner
from .store import store

# Load the workspace .env so provider keys (OPENAI_API_KEY etc.) resolve
# no matter which directory the sidecar is launched from.
_WORKSPACE_ENV = Path(__file__).resolve().parents[2] / ".env"
_BACKEND_ENV = Path(__file__).resolve().parents[1] / ".env"
for env_path in (_WORKSPACE_ENV, _BACKEND_ENV):
    if env_path.exists():
        load_dotenv(env_path, override=False)

PORT = int(os.getenv("TRADINGAGENTS_SERVICE_PORT", "8010"))

app = FastAPI(title="TradingAgents Sidecar", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class RunRequest(BaseModel):
    ticker: str = Field(..., description="Symbol, e.g. AAPL, BTC/USDT, 0700.HK")
    trade_date: str | None = Field(None, description="YYYY-MM-DD; defaults to today")
    llm_provider: str | None = None
    deep_think_llm: str | None = None
    quick_think_llm: str | None = None
    reasoning_effort: str | None = None
    response_language: str | None = None
    max_debate_rounds: int | None = None
    max_risk_discuss_rounds: int | None = None
    api_key: str | None = Field(None, description="Optional per-request provider key")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "tradingagents-sidecar",
        "active_runs": store.active_count(),
        "provider": os.getenv("TRADINGAGENTS_LLM_PROVIDER", "openai"),
    }


@app.get("/api/providers")
async def providers() -> dict[str, Any]:
    """Which provider keys are configured (booleans only, never values)."""
    from .runner import KEY_PREFIX_EXPECTATIONS, PROVIDER_KEY_VARS

    available: dict[str, bool] = {}
    for provider, var in PROVIDER_KEY_VARS.items():
        key = (os.getenv(var) or "").strip()
        ok = bool(key)
        check = KEY_PREFIX_EXPECTATIONS.get(provider)
        if ok and check and not check(key):
            ok = False  # key exists but belongs to a different vendor
        available[provider] = ok
    available["ollama"] = True  # local, no key needed
    return {"providers": available}


@app.post("/api/runs")
async def create_run(req: RunRequest) -> dict[str, Any]:
    from datetime import datetime, timezone

    trade_date = (req.trade_date or "").strip()
    if not trade_date:
        try:
            from app.core.timezone import now_sast  # type: ignore[import-not-found]

            trade_date = now_sast().strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001 - standalone service may lack the backend package
            trade_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    config = runner.build_config(req.model_dump())
    try:
        run = runner.start_run(req.ticker, trade_date, config, req.model_dump())
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return {"run_id": run.id, "ticker": req.ticker, "mapped_ticker": runner.map_ticker(req.ticker), "trade_date": trade_date}


@app.get("/api/runs")
async def list_runs(limit: int = 50) -> dict[str, Any]:
    return {"runs": store.list(min(max(limit, 1), 100))}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str, include_events: bool = False) -> dict[str, Any]:
    run = store.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    return run.snapshot(include_events=include_events)


@app.get("/api/runs/{run_id}/stream")
async def stream_run(run_id: str):
    """SSE feed: replays buffered events, then tails until the run ends."""
    run = store.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")

    async def generator():
        cursor = 0
        yield {
            "event": "connected",
            "data": json.dumps({"run_id": run.id, "status": run.status}),
        }
        while True:
            events = run.events
            total = len(events)
            while cursor < total:
                event = events[cursor]
                cursor += 1
                yield {
                    "event": event["type"],
                    "data": json.dumps(event, default=str),
                }
            if run.status in ("done", "error") and cursor >= total:
                break
            await asyncio.sleep(0.25)
        yield {
            "event": "end",
            "data": json.dumps({"status": run.status, "error": run.error}, default=str),
        }

    from sse_starlette.sse import EventSourceResponse

    return EventSourceResponse(
        generator(),
        ping=15,
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


def main() -> None:
    import uvicorn

    logger.remove()
    logger.add(lambda msg: print(msg.rstrip(), flush=True), level="INFO")
    host = os.getenv("TRADINGAGENTS_SERVICE_HOST", "127.0.0.1")
    logger.info(f"TradingAgents sidecar starting on {host}:{PORT}")
    uvicorn.run(app, host=host, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
