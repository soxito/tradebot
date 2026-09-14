"""Soup plugin — Pydantic schemas."""
from __future__ import annotations
from typing import Optional, List, Any, Dict
from pydantic import BaseModel, Field

class SoupStartRequest(BaseModel):
    base: str = Field(default="Qwen/Qwen2.5-0.5B-Instruct", description="HF base model id")
    task: str = Field(default="sft", description="Soup task: sft/dpo/grpo/ppo/... ")
    template: Optional[str] = Field(default=None, description="soup init template")
    dataset_path: Optional[str] = None  # if pre-uploaded, else auto from ohlcv/empty
    dataset_format: str = "alpaca"
    dataset_inline: Optional[List[Dict[str, Any]]] = None  # optional inline samples
    val_split: float = 0.1
    epochs: int = 3
    learning_rate: float = 2e-5
    batch_size: str = "auto"
    lora_r: int = 64
    lora_alpha: int = 16
    quantization: Optional[str] = "4bit"  # None/4bit/8bit/bf16
    stream_layers: bool = False
    # optional OHLCV experimental
    ohlcv_symbol: Optional[str] = None
    ohlcv_timeframe: str = "1h"
    ohlcv_limit: int = 2000
    output_name: Optional[str] = None

class SoupJobResponse(BaseModel):
    id: int
    base_model: str
    task: str
    status: str
    epochs: int
    quantization: Optional[str] = None
    stream_layers: bool = False
    output_dir: Optional[str] = None
    log_tail: Optional[str] = None
    error: Optional[str] = None
    disk_size_mb: float = 0.0
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    ohlcv_symbol: Optional[str] = None

class SoupStatusResponse(BaseModel):
    installed: bool
    soup_bin_exists: bool
    repo_cloned: bool
    version: Dict[str, Any]
    recommendation: Dict[str, Any]
    models: List[Dict[str, Any]] = []
    remote_commit: Optional[str] = None
    update_available: bool = False
    setup_command: str
    kronos_models: List[Dict[str, Any]] = []
    kronos_finetuned: List[Dict[str, Any]] = []
