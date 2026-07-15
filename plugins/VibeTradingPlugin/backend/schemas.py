"""
VibeTradingPlugin — Pydantic Schemas
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional
from pydantic import BaseModel


# ── Status ─────────────────────────────────────────────────────────────────

class VibeTradingStatus(BaseModel):
    reachable: bool
    version: Optional[str] = None
    sidecar_running: bool = False
    message: Optional[str] = None


# ── Runs ───────────────────────────────────────────────────────────────────

class RunRow(BaseModel):
    id: int
    remote_run_id: Optional[str]
    run_type: str
    symbol: Optional[str]
    prompt: Optional[str]
    status: str
    result_summary: Optional[str]
    pine_script: Optional[str]
    created_at: str
    updated_at: str


# ── Research ───────────────────────────────────────────────────────────────

class ResearchRequest(BaseModel):
    prompt: str
    symbol: Optional[str] = None


class ResearchResponse(BaseModel):
    run_id: Optional[int] = None
    remote_run_id: Optional[str] = None
    status: str
    result: Optional[Any] = None


# ── Backtest ───────────────────────────────────────────────────────────────

class BacktestRequest(BaseModel):
    symbol: str
    strategy: str
    timeframe: Optional[str] = None


class BacktestResponse(BaseModel):
    run_id: Optional[int] = None
    remote_run_id: Optional[str] = None
    status: str
    result: Optional[Any] = None
    pine_script: Optional[str] = None


# ── Swarm ──────────────────────────────────────────────────────────────────

class SwarmRequest(BaseModel):
    preset: str
    symbol: str
    extra: Optional[Dict[str, Any]] = None


class SwarmResponse(BaseModel):
    run_id: Optional[int] = None
    remote_run_id: Optional[str] = None
    status: str
    result: Optional[Any] = None


# ── Alpha Zoo ──────────────────────────────────────────────────────────────

class AlphaBenchRequest(BaseModel):
    zoo: str = "gtja191"
    universe: str = "csi300"
    period: Optional[str] = None
    top: int = 20


# ── Shadow Account ─────────────────────────────────────────────────────────

class ShadowAccountResponse(BaseModel):
    run_id: Optional[int] = None
    remote_run_id: Optional[str] = None
    status: str
    result: Optional[Any] = None


# ── Scheduled Research ─────────────────────────────────────────────────────

class ScheduleCreate(BaseModel):
    prompt: str
    schedule: str           # cron expression or ms interval
    symbol: Optional[str] = None
    config: Optional[Dict[str, Any]] = None


class ScheduleRow(BaseModel):
    id: int
    remote_job_id: Optional[str]
    prompt: str
    schedule: str
    symbol: Optional[str]
    active: bool
    created_at: str


# ── Signal Enrich ──────────────────────────────────────────────────────────

class EnrichSignalRequest(BaseModel):
    symbol: str
    timeframe: Optional[str] = "1h"


class EnrichSignalResponse(BaseModel):
    symbol: str
    kronos_summary: Optional[str] = None
    smc_bias: Optional[str] = None
    vibe_research: Optional[str] = None
    combined_note: Optional[str] = None
