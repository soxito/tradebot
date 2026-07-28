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


# ── volume gate ─────────────────────────────────────────────────────────────
# Volume is a hard precondition for every forecast / analysis / sniper entry.
# See services/volume_context.py for how each field is derived.

VolumeStatusT = Literal["OK", "UNAVAILABLE", "STALE", "INSUFFICIENT"]
VolumeRegimeT = Literal["DEAD", "NORMAL", "ELEVATED", "CLIMACTIC", "UNKNOWN"]
VolumeDivergenceT = Literal[
    "CONFIRMED_UP", "EXHAUSTION_UP", "CONFIRMED_DOWN", "EXHAUSTION_DOWN",
    "NEUTRAL", "UNKNOWN",
]
#: OK = tradeable, LOW_CONFIDENCE = direction known but under the floor,
#: NO_TRADE = volume could not be established, so no direction is inferred.
DecisionT = Literal["OK", "LOW_CONFIDENCE", "NO_TRADE"]


class VolumeContext(BaseModel):
    """Resolved volume evidence for one symbol. Every value is measured from
    real OHLCV returned by the existing integrations — nothing is estimated."""
    status: VolumeStatusT
    symbol: str
    source: str                                   # e.g. "ohlcv:15m", "ohlcv:1h"
    # base    — the exchange's own traded volume in the base asset (crypto)
    # tick    — MT5 broker tick count (price updates), not traded size
    # futures — the matching CME contract's volume, the published proxy for spot
    #           FX/metals flow (spot FX is OTC and prints no volume anywhere)
    volume_unit: Literal["base", "tick", "futures", "unknown"] = "base"

    volume_24h: Optional[float] = None            # rolling 24h traded volume
    volume_1h: Optional[float] = None             # most recent COMPLETED hour
    hourly_mean_24h: Optional[float] = None       # volume_24h / 24
    relative_volume: Optional[float] = None       # volume_1h / hourly_mean_24h
    z_score: Optional[float] = None               # 1h vs the 23 prior hours

    regime: VolumeRegimeT = "UNKNOWN"
    divergence: VolumeDivergenceT = "UNKNOWN"
    divergence_bars: int = 0                      # hourly bars in the window
    price_change_pct: Optional[float] = None      # over the divergence window
    volume_slope_norm: Optional[float] = None     # slope ÷ mean volume, per bar
    reversal_risk: bool = False                   # climactic volume vs the move

    hours_covered: int = 0
    last_bar_time: Optional[int] = None           # unix seconds, hour start
    age_seconds: Optional[int] = None
    detail: str = ""


class ForecastSignal(BaseModel):
    # "no_trade" is emitted when volume could not be resolved — the direction is
    # deliberately not inferred from price alone.
    direction: Literal["up", "down", "flat", "no_trade"]
    pct_change: float          # predicted % change over the horizon
    confidence: float          # 0..1
    target_price: float        # predicted close at end of horizon
    anchor_price: float        # last known close
    summary: str               # short spoken-friendly summary for JARVIS
    decision: DecisionT = "OK"
    rationale: List[str] = Field(default_factory=list)   # why this direction


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

    # Volume evidence this forecast was gated on. Always present; a non-OK
    # status means `decision` is NO_TRADE and no direction was inferred.
    volume: Optional[VolumeContext] = None
    decision: DecisionT = "OK"

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
    volume: Optional[VolumeContext] = None        # volume gate evidence
    decision: DecisionT = "OK"
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

    # Volume evidence carried on every emitted entry (required — no signal may
    # be shown without it).
    volume_24h: Optional[float] = None
    volume_1h: Optional[float] = None
    relative_volume: Optional[float] = None
    volume_regime: VolumeRegimeT = "UNKNOWN"
    volume_divergence: VolumeDivergenceT = "UNKNOWN"


class SniperSignalsResponse(BaseModel):
    exchange: str
    symbol: str
    timeframe: str
    engine: str
    anchor_price: float
    direction: Literal["up", "down", "flat", "no_trade"]
    pct_change: float = 0.0
    confidence: float = 0.0
    signals: List[SniperSignal] = Field(default_factory=list)
    volume: Optional[VolumeContext] = None
    decision: DecisionT = "OK"
    rationale: List[str] = Field(default_factory=list)
    note: Optional[str] = None

