"""
Kronos Forecast Plugin — Pydantic Schemas (API contracts)
"""
from typing import Any, Dict, List, Optional, Literal
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

VolumeStatusT = Literal["OK", "UNAVAILABLE", "STALE", "INSUFFICIENT", "NOT_APPLICABLE"]
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


class HLC3KaufmanSignal(BaseModel):
    """HLC3/Kaufman adaptive MA state (PineScript v2 port). Toggleable."""
    enabled: bool = True
    signal: Literal["buy", "sell", "neutral"] = "neutral"
    bfma_current: Optional[float] = None      # daily HTF reference (cyan)
    bsma_current: Optional[float] = None      # KAMA EMA fast line (orange)
    bfma_prev: Optional[float] = None
    bsma_prev: Optional[float] = None
    crossover: bool = False                   # BUY cross on the latest bar
    crossunder: bool = False                  # SELL cross on the latest bar
    bars_since_cross: Optional[int] = None
    summary: str = ""
    error: Optional[str] = None


class SRZoneSchema(BaseModel):
    level: float
    zone_top: float
    zone_bottom: float
    kind: Literal["support", "resistance"]
    formed_unix: int
    broken: bool = False
    tested: bool = False
    retested: bool = False
    manipulated: bool = False


class SRSignalEventSchema(BaseModel):
    unix_time: int
    event_type: str
    price: float
    level_price: float
    zone_kind: Literal["support", "resistance"]


class SRSignalsSchema(BaseModel):
    """LuxAlgo Support & Resistance Signals MTF state (v5 port). Toggleable."""
    enabled: bool = True
    support_zones: List[SRZoneSchema] = Field(default_factory=list)
    resistance_zones: List[SRZoneSchema] = Field(default_factory=list)
    recent_events: List[SRSignalEventSchema] = Field(default_factory=list)
    current_event: Optional[SRSignalEventSchema] = None
    nearest_support: Optional[float] = None
    nearest_resistance: Optional[float] = None
    summary: str = ""
    error: Optional[str] = None


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

    # HLC3/Kaufman adaptive MA + LuxAlgo S&R Signals MTF (both toggleable).
    hlc3_kaufman: Optional[HLC3KaufmanSignal] = None
    sr_signals: Optional[SRSignalsSchema] = None

    # OBLIVION AI engine v4.0 read on the same candles the model saw (see
    # backend/app/signals/oblivion.py for the shape) and its chart overlay
    # bundle: boxes (FVG/OB), labels (HH/HL/LH/LL, BOS/CHOCH, LIQ), diamond
    # markers, fib + entry/SL/TP price lines, session bands, dashboard.
    oblivion: Optional[Dict[str, Any]] = None
    overlay_bundle: Optional[Dict[str, Any]] = None

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
    display_name: str = ""                        # short label for the engine chip
    finetuned_job_id: Optional[int] = None       # set when a finetuned model is loaded


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


class OblivionTfSignal(BaseModel):
    """One timeframe's OBLIVION read, fused with the Kronos forecast."""
    timeframe: str
    action: Literal["buy", "sell", "wait"] = "wait"
    confirmed: bool = False
    new: bool = False
    score: int = 0
    required: int = 6
    bull: int = 0
    bear: int = 0
    market_bias: int = 0
    bias_label: str = "NEUTRAL"
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    tp3: Optional[float] = None
    risk_reward: Optional[float] = None
    confidence: float = 0.0
    kronos_direction: Optional[str] = None
    kronos_pct_change: Optional[float] = None
    kronos_confidence: Optional[float] = None
    kronos_aligned: Optional[bool] = None
    vetoed_by: Optional[str] = None          # "kronos" | "news" | None
    news_blocked: bool = False
    session: str = "OFF"
    structure: str = "WAIT"
    last_signal: Optional[Dict[str, Any]] = None
    summary: str = ""
    error: Optional[str] = None


class OblivionConsensus(BaseModel):
    action: Literal["buy", "sell", "wait"] = "wait"
    score: float = 0.0                        # signed, -1..1, timeframe-weighted
    agreeing_tfs: List[str] = Field(default_factory=list)
    opposing_tfs: List[str] = Field(default_factory=list)
    vetoed_tfs: List[str] = Field(default_factory=list)
    note: str = ""


class OblivionMultiTfResponse(BaseModel):
    exchange: str
    symbol: str
    generated_at: int
    with_kronos: bool = True
    consensus: OblivionConsensus = Field(default_factory=OblivionConsensus)
    per_tf: List[OblivionTfSignal] = Field(default_factory=list)
    news_blocked: bool = False
    note: Optional[str] = None


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


# ── Fine-tuning ───────────────────────────────────────────────────────────────

FinetuneStatusT = Literal[
    "pending", "exporting", "running", "completed", "failed", "cancelled"
]


class FinetuneStartRequest(BaseModel):
    """Kick off a fine-tune on live OHLCV for one instrument/timeframe."""
    symbol: str
    exchange: str = "bitget"
    timeframe: str = "1h"
    model_name: Optional[str] = None            # user label e.g. "super-base"
    limit: int = Field(5000, ge=256, le=100000, description="Candles to export for training")
    lookback: int = Field(512, ge=64, le=2048)
    pred_len: int = Field(48, ge=1, le=512)
    tokenizer_epochs: int = Field(30, ge=1, le=200)
    predictor_epochs: int = Field(20, ge=1, le=200)
    batch_size: int = Field(32, ge=1, le=256)
    base_model: Optional[str] = None            # HF id/local path to start from
    use_pattern_features: bool = Field(True, description="Enrich training data with previous-candle pattern signals")


class FinetuneExportRequest(BaseModel):
    symbol: str
    exchange: str = "bitget"
    timeframe: str = "1h"
    limit: int = Field(3000, ge=256, le=100000)
    use_pattern_features: bool = Field(True, description="Enrich exported CSV with pattern features")


class FinetuneExportResponse(BaseModel):
    ok: bool
    path: Optional[str] = None
    rows: int = 0
    symbol: str
    exchange: str
    timeframe: str
    first_ts: Optional[str] = None
    last_ts: Optional[str] = None
    enriched: bool = False
    pattern_coverage: Optional[dict] = None
    error: Optional[str] = None


class FinetuneJobResponse(BaseModel):
    id: int
    symbol: str
    exchange: str
    timeframe: str
    status: FinetuneStatusT
    model_name: Optional[str] = None            # user label e.g. "super-base"
    symbols_combined: Optional[List[str]] = None  # for COMBINED models: source pairs
    lookback: int
    pred_len: int
    tokenizer_epochs: int
    predictor_epochs: int
    batch_size: int
    base_model: Optional[str] = None
    rows_exported: Optional[int] = None
    data_path: Optional[str] = None
    output_dir: Optional[str] = None
    log_path: Optional[str] = None
    pid: Optional[int] = None
    error: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    created_at: Optional[str] = None
    log_tail: Optional[str] = None              # last N lines of the training log
    disk_size_mb: float = 0.0                   # size of the checkpoint dir on disk


class FinetuneCombineResponse(BaseModel):
    ok: bool
    job_id: Optional[int] = None
    symbols_combined: List[str] = Field(default_factory=list)
    rows_merged: int = 0
    error: Optional[str] = None


class FinetuneSetupStatus(BaseModel):
    vendored: bool                              # finetune_csv pipeline present?
    model_vendored: bool                        # base model/ package present?
    setup_command: str                          # how to vendor it if missing
    finetune_dir: str


# ── Chart drawings (user overlays reused + refined by agents) ─────────────────

DrawingTypeT = Literal[
    "trendline", "ray", "hline", "vline", "rectangle", "fib", "text",
]
DrawingSourceT = Literal["user", "agent"]


class DrawingPoint(BaseModel):
    """A single anchor. ``time`` is unix seconds (lightweight-charts UTCTimestamp);
    ``price`` is on the instrument's price scale."""
    time: float
    price: float


class ChartDrawing(BaseModel):
    """One shape on the chart. Kept deliberately generic so new tools can be
    added on the client without a schema change."""
    id: str
    type: DrawingTypeT
    points: List[DrawingPoint] = Field(default_factory=list)
    color: str = "#22d3ee"
    width: int = 2
    text: Optional[str] = None
    source: DrawingSourceT = "user"
    locked: bool = False
    agent_note: Optional[str] = None            # why the agent drew/adjusted it
    refined_from: Optional[str] = None          # id of the user shape it sharpens


class DrawingsDoc(BaseModel):
    exchange: str
    symbol: str
    timeframe: str
    drawings: List[ChartDrawing] = Field(default_factory=list)
    updated_at: Optional[str] = None


class SaveDrawingsRequest(BaseModel):
    timeframe: str = "1h"
    drawings: List[ChartDrawing] = Field(default_factory=list)


class RefineDrawingsResponse(BaseModel):
    exchange: str
    symbol: str
    timeframe: str
    refined: List[ChartDrawing] = Field(default_factory=list)   # new agent shapes
    drawings: List[ChartDrawing] = Field(default_factory=list)  # full merged doc
    note: Optional[str] = None

