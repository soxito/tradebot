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
