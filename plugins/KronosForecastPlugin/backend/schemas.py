"""
Kronos Forecast Plugin — Pydantic Schemas (API contracts)
"""
from typing import List, Optional, Literal
from pydantic import BaseModel, Field


class ForecastCandle(BaseModel):
    """A single predicted candle. `time` is unix seconds (lightweight-charts format)."""
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class BandPoint(BaseModel):
    """Confidence band value at a future timestamp."""
    time: int
    value: float


class OverlayLinePoint(BaseModel):
    time: int
    value: float


class OverlaySeries(BaseModel):
    """Matches the frontend TradingViewChart `IndicatorOverlaySeries` shape."""
    name: str
    type: Literal["line"] = "line"
    pane: str = "main"
    color: str
    lineWidth: int = 2
    lineStyle: int = 0
    data: List[OverlayLinePoint] = Field(default_factory=list)


class OverlayMarker(BaseModel):
    """Matches the frontend TradingViewChart `IndicatorMarker` shape."""
    time: int
    position: Literal["belowBar", "aboveBar", "inBar"]
    color: str
    shape: Literal["arrowUp", "arrowDown", "circle", "square"]
    text: str


class ForecastSignal(BaseModel):
    direction: Literal["up", "down", "flat"]
    pct_change: float          # predicted % change over the horizon
    confidence: float          # 0..1
    target_price: float        # predicted close at end of horizon
    anchor_price: float        # last known close
    summary: str               # short spoken-friendly summary for JARVIS


class ForecastResponse(BaseModel):
    exchange: str
    symbol: str
    timeframe: str
    engine: Literal["kronos", "heuristic", "unavailable"]
    model_name: str
    lookback: int
    pred_len: int
    samples: int

    anchor_time: int
    anchor_price: float

    forecast: List[ForecastCandle] = Field(default_factory=list)
    upper_band: List[BandPoint] = Field(default_factory=list)
    lower_band: List[BandPoint] = Field(default_factory=list)

    signal: Optional[ForecastSignal] = None

    # Ready-to-render overlays + markers for any chart in the app
    overlays: List[OverlaySeries] = Field(default_factory=list)
    markers: List[OverlayMarker] = Field(default_factory=list)

    # Historical candles used to run this forecast (lightweight-charts format).
    # Populated for forex/metals where the exchange has no data; empty for crypto
    # (the frontend fetches crypto OHLCV directly for better resolution).
    candles: List[ForecastCandle] = Field(default_factory=list)

    note: Optional[str] = None


class MarketCapInfo(BaseModel):
    """Live crypto market context used to frame the forecast."""
    symbol: str
    name: Optional[str] = None
    market_cap: Optional[float] = None
    market_cap_rank: Optional[int] = None
    volume_24h: Optional[float] = None
    price: Optional[float] = None
    price_change_24h: Optional[float] = None
    is_crypto: bool = True


class PositionInfo(BaseModel):
    """An open position on the analysed symbol (futures/swap)."""
    exchange: str
    symbol: str
    side: str                            # long | short
    size: float
    entry_price: float
    mark_price: float
    pnl: float
    pnl_pct: float
    leverage: Optional[float] = None
    liquidation_price: Optional[float] = None


class JarvisAnalysisResponse(BaseModel):
    """JARVIS natural-language analysis of a Kronos forecast, enriched with
    crypto market cap and (optionally) stored to the shared AI knowledge brain."""
    exchange: str
    symbol: str
    timeframe: str
    engine: str
    analysis: str                       # detailed JARVIS explanation
    spoken: str                         # short speech-friendly line
    signal: Optional[ForecastSignal] = None
    market: Optional[MarketCapInfo] = None
    position: Optional[PositionInfo] = None       # open position on this symbol
    position_advice: Optional[str] = None         # forecast-aware suggestion
    learned: bool = False               # stored to the brain?
    provider: Optional[str] = None      # which LLM answered
    note: Optional[str] = None


class ForecastStatus(BaseModel):
    available: bool
    engine: Literal["kronos", "heuristic", "unavailable"]
    model_name: str
    tokenizer_name: str
    device: str
    max_context: int
    detail: str


class BatchForecastRequest(BaseModel):
    exchange: str = "bitget"
    timeframe: str = "1h"
    symbols: List[str] = Field(default_factory=list)
    lookback: Optional[int] = None
    pred_len: Optional[int] = None
    samples: Optional[int] = None


class SniperSignal(BaseModel):
    """A ready-to-execute entry derived from the Kronos forecast direction and
    confidence bands, enriched with JARVIS-style reasoning."""
    id: str
    side: Literal["long", "short"]
    order_kind: Literal["market", "limit"]      # market = enter now, limit = wait for pullback
    label: str                                   # short human label e.g. "Market entry"
    entry: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: Optional[float] = None
    risk_reward: float                           # R:R to TP1
    confidence: float                            # 0..1 (from the model)
    leverage: int
    reasons: List[str] = Field(default_factory=list)


class SniperSignalsResponse(BaseModel):
    exchange: str
    symbol: str
    timeframe: str
    engine: str
    anchor_price: float
    direction: Literal["up", "down", "flat"]
    pct_change: float = 0.0
    confidence: float = 0.0
    signals: List[SniperSignal] = Field(default_factory=list)
    note: Optional[str] = None

