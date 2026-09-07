"""
Kronos Forecast Plugin — Database Models

Standalone tables, all prefixed `kronos_`. No cross-plugin foreign keys.
The loader auto-creates these via metadata.create_all on startup (class name
ends with `Base` so PluginLoader picks up its metadata).
"""
from datetime import datetime

from sqlalchemy import Column, Integer, String, Float, DateTime, JSON, Boolean
from sqlalchemy.orm import declarative_base

KronosBase = declarative_base()


class KronosForecast(KronosBase):
    """
    A logged forecast run. Stored so we can (a) cache/replay recent forecasts
    and (b) later score prediction accuracy against realized candles.
    """

    __tablename__ = "kronos_forecasts"

    id = Column(Integer, primary_key=True, autoincrement=True)

    exchange = Column(String(32), nullable=False, index=True)
    symbol = Column(String(64), nullable=False, index=True)
    timeframe = Column(String(8), nullable=False)

    # Model / sampling parameters used
    model_name = Column(String(128), nullable=False)
    lookback = Column(Integer, nullable=False)
    pred_len = Column(Integer, nullable=False)
    samples = Column(Integer, nullable=False)
    temperature = Column(Float, nullable=False, default=1.0)
    top_p = Column(Float, nullable=False, default=0.9)

    # Whether the real Kronos model produced this, or the heuristic fallback
    engine = Column(String(24), nullable=False, default="kronos")  # kronos | heuristic

    # Anchor price at forecast time (last close of the lookback window)
    anchor_price = Column(Float, nullable=True)
    anchor_time = Column(Integer, nullable=True)  # unix seconds of last known candle

    # Derived directional signal
    direction = Column(String(8), nullable=True)   # up | down | flat
    pct_change = Column(Float, nullable=True)       # predicted % change over horizon
    confidence = Column(Float, nullable=True)       # 0..1

    # Full forecast payload (predicted candles + confidence bands)
    forecast_json = Column(JSON, nullable=True)

    scored = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class KronosFinetuneJob(KronosBase):
    """A fine-tuning run: export live OHLCV → CSV → train tokenizer + predictor.

    Standalone table; no cross-plugin foreign keys. The subprocess trainer
    writes progress to ``log_path`` and its checkpoints under ``output_dir``.
    """

    __tablename__ = "kronos_finetune_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)

    exchange = Column(String(32), nullable=False)
    symbol = Column(String(64), nullable=False, index=True)
    timeframe = Column(String(8), nullable=False)

    # Optional user label for the produced model, e.g. "super-base".
    model_name = Column(String(128), nullable=True)

    # pending | exporting | running | completed | failed | cancelled
    status = Column(String(16), nullable=False, default="pending", index=True)

    # Training parameters
    lookback = Column(Integer, nullable=False, default=512)
    pred_len = Column(Integer, nullable=False, default=48)
    tokenizer_epochs = Column(Integer, nullable=False, default=30)
    predictor_epochs = Column(Integer, nullable=False, default=20)
    batch_size = Column(Integer, nullable=False, default=32)
    base_model = Column(String(128), nullable=True)      # pretrained start point

    # Paths + process
    data_path = Column(String(512), nullable=True)
    config_path = Column(String(512), nullable=True)
    log_path = Column(String(512), nullable=True)
    output_dir = Column(String(512), nullable=True)      # finetuned checkpoints
    pid = Column(Integer, nullable=True)

    rows_exported = Column(Integer, nullable=True)
    error = Column(String(1024), nullable=True)

    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class KronosChartDrawing(KronosBase):
    """User-authored (and agent-refined) chart drawings for a chart tab.

    One row per (exchange, symbol, timeframe). ``drawings`` holds the full list
    of shapes — both ``source='user'`` shapes the trader draws and
    ``source='agent'`` shapes the analysis pipeline refines — as a JSON array so
    the trading-room agents can read them as context and write back sharper
    levels. Standalone table; no cross-plugin foreign keys.
    """

    __tablename__ = "kronos_chart_drawings"

    id = Column(Integer, primary_key=True, autoincrement=True)

    exchange = Column(String(32), nullable=False, index=True)
    symbol = Column(String(64), nullable=False, index=True)
    timeframe = Column(String(8), nullable=False, index=True)

    # Full list of shapes (user + agent). Each item is a dict:
    # {id, type, points:[{time,price}], color, width, text?, source, locked?,
    #  agent_note?, refined_from?}
    drawings = Column(JSON, nullable=False, default=list)

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

