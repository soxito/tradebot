"""Soup plugin — SQLAlchemy models (standalone, never touches core tables)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, Integer, String, DateTime, Text, JSON, Float
from sqlalchemy.orm import declarative_base

SoupBase = declarative_base()

class SoupFinetuneJob(SoupBase):
    __tablename__ = "soup_finetune_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # HF base model id, e.g. Qwen/Qwen2.5-0.5B-Instruct or meta-llama/Llama-3.1-8B-Instruct
    base_model = Column(String(256), nullable=False, default="Qwen/Qwen2.5-0.5B-Instruct")
    task = Column(String(64), nullable=False, default="sft")  # sft/dpo/grpo/...
    template = Column(String(64), nullable=True)  # soup template: chat|code|tool-calling etc
    # dataset
    dataset_path = Column(Text, nullable=True)
    dataset_format = Column(String(32), nullable=True, default="alpaca")  # alpaca/sharegpt/chatml/...
    val_split = Column(Float, nullable=True, default=0.1)
    # training hyperparams (persisted for reuse)
    epochs = Column(Integer, nullable=False, default=3)
    learning_rate = Column(Float, nullable=True, default=2e-5)
    batch_size = Column(String(16), nullable=True, default="auto")
    lora_r = Column(Integer, nullable=True, default=64)
    lora_alpha = Column(Integer, nullable=True, default=16)
    quantization = Column(String(16), nullable=True)  # 4bit/8bit/bf16 or None
    stream_layers = Column(Integer, nullable=True, default=0)  # bool as int
    # soup.yaml + output
    config_path = Column(Text, nullable=True)
    output_dir = Column(Text, nullable=True)
    log_path = Column(Text, nullable=True)
    pid = Column(Integer, nullable=True)
    # status tracking
    status = Column(String(32), nullable=False, default="pending")  # pending|running|completed|failed|cancelled
    error = Column(Text, nullable=True)
    # optional OHLCV experimental link
    ohlcv_symbol = Column(String(32), nullable=True)
    ohlcv_timeframe = Column(String(16), nullable=True)
    ohlcv_rows = Column(Integer, nullable=True)
    # meta
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    # extra bag for UI (disk size, hf model list, etc.)
    meta_json = Column(JSON, nullable=True)
