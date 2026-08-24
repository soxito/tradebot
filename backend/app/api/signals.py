"""
Signals and TradingView Webhook API Routes
"""
import json
import math
from datetime import datetime, timedelta
from typing import Optional, List, Any, Dict, cast
from fastapi import APIRouter, Depends, HTTPException, Request, Header
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, desc, func, and_, or_

from app.core.database import get_db
from app.core.config import settings
from app.core.security import validate_tradingview_webhook
from app.core.timezone import now_sast
from app.models.schemas import TradingViewWebhook, SignalCreate, SignalResponse
from app.models.database import (
    SignalStatus,
    SignalMonitorPair,
    Signal,
    SignalSource,
    SignalAction,
    PineScript,
    RugPullToken,
    PumpToken,
    Trade,
)
from app.signals.service import SignalService
from app.signals.generator import SignalGenerator
from app.signals.pipeline import run_signal_pipeline, analyze_multi_timeframe, DEFAULT_PAIRS
from app.signals.mtpc_strategy import analyze_mtpc
from app.signals.ar_atr_strategy import analyze_ar_atr
from app.exchanges.manager import SupportedExchange
from app.monitoring.alerts import AlertService
from app.monitoring.metrics import record_signal_created
from app.api.strategies import StrategyEvalRequest, evaluate_pinescript
from app.agents.orchestrator import AgentOrchestrator
from app.sentiment.enhanced_service import EnhancedSentimentService
from loguru import logger


router = APIRouter(prefix="/signals", tags=["signals"])


# ---------- Request schemas ----------

class GenerateSignalsRequest(BaseModel):
    symbols: List[str]
    timeframe: str = "1h"
    exchange: str = "bitget"


class AnalyzeRequest(BaseModel):
    timeframe: str = "1h"
    exchange: str = "bitget"


class GenerateSmcSignalRequest(BaseModel):
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"
    exchange: str = "bitget"
    limit: int = 200
    script_id: Optional[int] = None
    use_ai_agents: bool = True
    use_insights: bool = True
    persist_signal: bool = True
    stop_loss_pct: float = 2.0
    take_profit_pct: float = 4.0
    remove_old_signals: bool = False
    remove_old_scope: str = "symbol_timeframe"
    entry_mode: str = "best_limit"
    selected_entry_label: Optional[str] = None
    custom_entry_price: Optional[float] = None
    refresh_news_if_stale: bool = True


def _normalize_symbol(symbol: str) -> str:
    value = symbol.strip().upper()
    if "/" in value:
        return value
    if value.endswith("USDT"):
        return f"{value[:-4]}/USDT"
    return value


def _safe_json(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="ignore")
    if not isinstance(value, str):
        return {}
    try:
        return json.loads(value)
    except Exception:
        return {}


def _clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(max_value, value))


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except (TypeError, ValueError):
        return None


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        # Handle trailing "Z" values from API/JSON timestamps.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _sentiment_is_recent(sentiment: Optional[dict], max_age_hours: int) -> bool:
    if not sentiment:
        return False
    created_at = _parse_iso_datetime(sentiment.get("created_at"))
    if created_at is None:
        return False
    try:
        age = now_sast() - created_at
        return age <= timedelta(hours=max(1, int(max_age_hours)))
    except Exception:
        return False


async def _get_sentiment_with_refresh(
    db: AsyncSession,
    symbol: str,
    *,
    max_age_hours: int,
    refresh_if_stale: bool,
    refresh_state: Dict[str, bool],
) -> Optional[dict]:
    normalized_symbol = symbol.upper()
    sentiment = await EnhancedSentimentService.get_latest(db, normalized_symbol)

    if sentiment and _sentiment_is_recent(sentiment, max_age_hours):
        return sentiment

    if not refresh_if_stale or refresh_state.get("did_refresh"):
        return sentiment

    try:
        await EnhancedSentimentService.run_full_cycle(db, max_age_hours=max_age_hours)
    except Exception as exc:
        logger.warning(f"Sentiment refresh failed for {normalized_symbol}: {exc}")
    finally:
        refresh_state["did_refresh"] = True

    refreshed = await EnhancedSentimentService.get_latest(db, normalized_symbol)
    return refreshed or sentiment


def _enum_to_str(value: Any, default: str) -> str:
    if value is None:
        return default
    return str(getattr(value, "value", value))


def _to_iso(value: Any) -> Optional[str]:
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else None


async def _resolve_smc_script(db: AsyncSession, script_id: Optional[int]) -> PineScript:
    if script_id is not None:
        result = await db.execute(select(PineScript).where(PineScript.id == script_id))
        script = result.scalar_one_or_none()
        if not script:
            raise HTTPException(status_code=404, detail=f"Pine Script {script_id} not found")
        return script

    result = await db.execute(
        select(PineScript)
        .where(func.lower(PineScript.name).like("%smart money concepts%"))
        .order_by(desc(PineScript.updated_at))
        .limit(1)
    )
    script = result.scalar_one_or_none()
    if not script:
        raise HTTPException(status_code=404, detail="Smart Money Concepts script not found")
    return script


def _resolve_levels(
    action: str,
    price: Optional[float],
    indicator_values: dict,
    stop_loss_pct: float,
    take_profit_pct: float,
) -> tuple[Optional[float], Optional[float]]:
    stop_loss = indicator_values.get("stop_loss")
    take_profit = indicator_values.get("take_profit")

    if price is None:
        return stop_loss, take_profit

    if stop_loss is None:
        if action == SignalAction.BUY.value:
            stop_loss = round(price * (1 - (stop_loss_pct / 100)), 8)
        elif action == SignalAction.SELL.value:
            stop_loss = round(price * (1 + (stop_loss_pct / 100)), 8)

    if take_profit is None:
        if action == SignalAction.BUY.value:
            take_profit = round(price * (1 + (take_profit_pct / 100)), 8)
        elif action == SignalAction.SELL.value:
            take_profit = round(price * (1 - (take_profit_pct / 100)), 8)

    return stop_loss, take_profit


VALID_REMOVE_SCOPES = {"symbol_timeframe", "symbol", "all"}
VALID_ENTRY_MODES = {"best_limit", "conservative", "balanced", "aggressive", "market", "custom", "candidate"}


def _normalize_remove_scope(scope: Optional[str]) -> str:
    value = str(scope or "symbol_timeframe").strip().lower()
    return value if value in VALID_REMOVE_SCOPES else "symbol_timeframe"


def _normalize_entry_mode(mode: Optional[str]) -> str:
    value = str(mode or "best_limit").strip().lower()
    return value if value in VALID_ENTRY_MODES else "best_limit"


def _build_entry_candidates(action: str, market_price: Optional[float], indicators: Dict[str, Any]) -> List[dict]:
    if market_price is None or market_price <= 0:
        return []

    side = action.lower()
    if side not in {SignalAction.BUY.value, SignalAction.SELL.value}:
        return []

    market = float(market_price)
    volume_ratio = _safe_float(indicators.get("volume_ratio")) or 0.0
    bb_lower = _safe_float(indicators.get("bb_lower"))
    bb_upper = _safe_float(indicators.get("bb_upper"))
    ma5 = _safe_float(indicators.get("ma5"))
    ma10 = _safe_float(indicators.get("ma10"))
    vwap = _safe_float(indicators.get("vwap"))

    raw_candidates: List[tuple[str, str, float, str]] = [("market_now", "Market Now", market, "Current market reference")]
    if side == SignalAction.BUY.value:
        raw_candidates.extend(
            [
                ("conservative_limit", "Conservative Limit", market * 0.999, "0.10% pullback for faster fill"),
                ("balanced_limit", "Balanced Limit", market * 0.9975, "0.25% pullback for balanced risk/reward"),
                ("aggressive_limit", "Aggressive Limit", market * 0.995, "0.50% pullback for better entry"),
            ]
        )
        if bb_lower and market * 0.96 < bb_lower < market:
            raw_candidates.append(("support_bb", "BB Lower Support", bb_lower, "Bollinger lower band support"))
        if ma5 and market * 0.98 < ma5 < market:
            raw_candidates.append(("support_ma5", "MA5 Support", ma5, "Short-term moving average support"))
        if ma10 and market * 0.97 < ma10 < market:
            raw_candidates.append(("support_ma10", "MA10 Support", ma10, "Trend support from MA10"))
        if vwap and market * 0.98 < vwap < market:
            raw_candidates.append(("support_vwap", "VWAP Support", vwap, "Volume-weighted support"))
    else:
        raw_candidates.extend(
            [
                ("conservative_limit", "Conservative Limit", market * 1.001, "0.10% bounce for faster fill"),
                ("balanced_limit", "Balanced Limit", market * 1.0025, "0.25% bounce for balanced risk/reward"),
                ("aggressive_limit", "Aggressive Limit", market * 1.005, "0.50% bounce for better short entry"),
            ]
        )
        if bb_upper and market < bb_upper < market * 1.04:
            raw_candidates.append(("resistance_bb", "BB Upper Resistance", bb_upper, "Bollinger upper-band rejection"))
        if ma5 and market < ma5 < market * 1.02:
            raw_candidates.append(("resistance_ma5", "MA5 Resistance", ma5, "Short-term moving average resistance"))
        if ma10 and market < ma10 < market * 1.03:
            raw_candidates.append(("resistance_ma10", "MA10 Resistance", ma10, "Trend resistance from MA10"))
        if vwap and market < vwap < market * 1.02:
            raw_candidates.append(("resistance_vwap", "VWAP Resistance", vwap, "Volume-weighted resistance"))

    dedup: Dict[str, dict] = {}
    target_distance = 0.0035
    for label, title, candidate_price, reason in raw_candidates:
        if candidate_price <= 0:
            continue

        if side == SignalAction.BUY.value:
            favorable_distance = max(0.0, (market - candidate_price) / market)
            is_limit = candidate_price < market * 0.999
        else:
            favorable_distance = max(0.0, (candidate_price - market) / market)
            is_limit = candidate_price > market * 1.001

        if label == "market_now":
            score = 0.2
        else:
            distance_fit = max(0.0, 1.0 - abs(favorable_distance - target_distance) / target_distance)
            fill_probability = max(0.0, 1.0 - favorable_distance / 0.015)
            structural_bonus = 0.12 if any(k in label for k in ("support", "resistance", "bb", "vwap")) else 0.0
            volume_bonus = 0.06 if volume_ratio >= 1.15 else (0.03 if volume_ratio >= 1.0 else 0.0)
            score = _clamp(0.28 + (distance_fit * 0.42) + (fill_probability * 0.16) + structural_bonus + volume_bonus, 0.0, 1.0)

        key = f"{label}:{round(candidate_price, 8)}"
        existing = dedup.get(key)
        if existing and float(existing.get("score") or 0.0) >= score:
            continue

        dedup[key] = {
            "label": label,
            "title": title,
            "price": round(candidate_price, 8),
            "score": round(score, 4),
            "is_limit": is_limit,
            "distance_pct": round(favorable_distance * 100, 3),
            "reason": reason,
        }

    sorted_candidates = sorted(
        dedup.values(),
        key=lambda item: (float(item.get("score") or 0.0), 1 if item.get("is_limit") else 0),
        reverse=True,
    )
    for rank, candidate in enumerate(sorted_candidates, start=1):
        candidate["rank"] = rank
    return sorted_candidates[:8]


def _select_entry_candidate(
    action: str,
    market_price: Optional[float],
    candidates: List[dict],
    entry_mode: str,
    selected_entry_label: Optional[str],
    custom_entry_price: Optional[float],
) -> dict:
    market = _safe_float(market_price)
    mode = _normalize_entry_mode(entry_mode)
    side = action.lower()

    if market is None or market <= 0:
        return {
            "label": "market_now",
            "title": "Market Now",
            "price": market_price,
            "mode": mode,
            "is_limit": False,
            "reason": "No valid market reference",
        }

    by_label = {str(c.get("label")): c for c in candidates}
    limit_candidates = [c for c in candidates if bool(c.get("is_limit"))]

    def favorable_distance(item: dict) -> float:
        candidate_price = _safe_float(item.get("price"))
        if candidate_price is None or candidate_price <= 0:
            return 0.0
        if side == SignalAction.BUY.value:
            return max(0.0, (market - candidate_price) / market)
        return max(0.0, (candidate_price - market) / market)

    if mode == "custom":
        custom_price = _safe_float(custom_entry_price)
        if custom_price and custom_price > 0:
            return {
                "label": "custom_entry",
                "title": "Custom Entry",
                "price": round(custom_price, 8),
                "mode": mode,
                "is_limit": (custom_price < market * 0.999) if side == SignalAction.BUY.value else (custom_price > market * 1.001),
                "reason": "User-defined entry price",
            }
        mode = "best_limit"

    if mode == "candidate" and selected_entry_label and selected_entry_label in by_label:
        selected = by_label[selected_entry_label]
        return {
            **selected,
            "mode": mode,
            "reason": selected.get("reason") or "Selected candidate",
        }

    if mode == "market":
        return {
            "label": "market_now",
            "title": "Market Now",
            "price": round(market, 8),
            "mode": mode,
            "is_limit": False,
            "reason": "Immediate execution at market reference",
        }

    if mode in {"conservative", "balanced", "aggressive"} and limit_candidates:
        target = {
            "conservative": 0.0012,
            "balanced": 0.0028,
            "aggressive": 0.005,
        }[mode]
        selected = min(limit_candidates, key=lambda item: abs(favorable_distance(item) - target))
        return {
            **selected,
            "mode": mode,
            "reason": selected.get("reason") or f"{mode.capitalize()} limit profile",
        }

    if limit_candidates:
        selected = max(limit_candidates, key=lambda item: float(item.get("score") or 0.0))
    elif candidates:
        selected = max(candidates, key=lambda item: float(item.get("score") or 0.0))
    else:
        selected = {
            "label": "market_now",
            "title": "Market Now",
            "price": round(market, 8),
            "score": 0.2,
            "is_limit": False,
            "reason": "No candidate levels available",
        }

    return {
        **selected,
        "mode": "best_limit",
        "reason": selected.get("reason") or "Highest score candidate",
    }


def _serialize_signal(signal: Signal) -> dict:
    raw_data = _safe_json(getattr(signal, "raw_data", None))
    indicators = _safe_json(getattr(signal, "indicators", None))
    raw_volume_context = raw_data.get("volume_context")
    volume_context: dict[str, Any] = raw_volume_context if isinstance(raw_volume_context, dict) else {}
    raw_btc_news_context = raw_data.get("btc_news_context")
    btc_news_context: dict[str, Any] = raw_btc_news_context if isinstance(raw_btc_news_context, dict) else {}
    entry_quality = raw_data.get("entry_quality")
    decision_reasons = entry_quality.get("reasons") if isinstance(entry_quality, dict) else None
    order_flow_confirmed = raw_data.get("order_flow_confirmed")
    if order_flow_confirmed is None:
        order_flow_confirmed = volume_context.get("directional_confirmed")
    return {
        "id": getattr(signal, "id", None),
        "source": _enum_to_str(getattr(signal, "source", None), "unknown"),
        "symbol": getattr(signal, "symbol", None),
        "action": _enum_to_str(getattr(signal, "action", None), "hold"),
        "price": getattr(signal, "price", None),
        "entry_price": getattr(signal, "price", None) or raw_data.get("entry_price"),
        "timeframe": getattr(signal, "timeframe", None),
        "strength": getattr(signal, "strength", None),
        "confidence": getattr(signal, "confidence", None),
        "status": _enum_to_str(getattr(signal, "status", None), "pending"),
        "created_at": _to_iso(getattr(signal, "created_at", None)),
        "updated_at": _to_iso(getattr(signal, "updated_at", None)),
        "raw_data": raw_data,
        "indicators": indicators,
        "stop_loss": indicators.get("stop_loss") or raw_data.get("stop_loss"),
        "take_profit": indicators.get("take_profit") or raw_data.get("take_profit"),
        "entry_quality": entry_quality,
        "volume_context": volume_context,
        "btc_news_context": btc_news_context,
        "volume_ratio": volume_context.get("volume_ratio") or indicators.get("volume_ratio"),
        "order_flow_confirmed": order_flow_confirmed,
        "decision_reasons": decision_reasons,
        "ai_reasoning": (raw_data.get("ai") or {}).get("final_reasoning"),
    }


# ---------- Autonomous signal generation ----------

@router.post("/generate")
async def generate_signals(
    req: GenerateSignalsRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Generate signals for configured pairs using technical analysis + sentiment.
    No TradingView dependency.
    """
    if not req.symbols:
        raise HTTPException(status_code=400, detail="symbols list is required")

    try:
        exchange_enum = SupportedExchange(req.exchange.lower())
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unsupported exchange: {req.exchange}")

    try:
        result = await SignalGenerator.generate_signals_batch(
            db=db,
            symbols=req.symbols,
            timeframe=req.timeframe,
            exchange=exchange_enum,
        )
        return result
    except Exception as e:
        logger.error(f"Signal generation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/analysis/{symbol}")
async def get_analysis(
    symbol: str,
    timeframe: str = "1h",
    exchange: str = "bitget",
):
    """
    Run technical analysis for a symbol without creating a signal.
    Good for preview / charting.
    """
    # symbol comes URL-encoded, e.g. BTC%2FUSDT → BTC/USDT, or BTCUSDT → BTC/USDT
    if "/" not in symbol and symbol.upper().endswith("USDT"):
        symbol = symbol[:-4] + "/USDT"

    try:
        exchange_enum = SupportedExchange(exchange.lower())
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unsupported exchange: {exchange}")

    result = await SignalGenerator.analyze_pair(symbol, timeframe, exchange_enum)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    return result


@router.get("/zones/{symbol}")
async def get_zones(
    symbol: str,
    timeframe: str = "1h",
    limit: int = 220,
):
    """
    Chart-overlay payload: fibonacci bands, supply/demand zones, trendline
    channels and pivot-cluster S/R levels for one symbol — everything the
    frontend needs to draw the desk's zone read in one request.

    Symbol accepts BTC/USDT, BTCUSDT, XAUUSD (universal resolver serves
    non-crypto instruments).
    """
    from app.services.candles import fetch as fetch_candles
    from app.signals.technical import (
        auto_fib_retracement,
        ohlcv_to_dataframe,
        support_resistance_mtf,
    )
    from app.signals.zones import analyze_zones

    if "/" not in symbol and symbol.upper().endswith("USDT"):
        symbol = symbol[:-4] + "/USDT"

    ohlcv = await fetch_candles(symbol, timeframe, max(60, min(limit, 500)))
    if not ohlcv:
        raise HTTPException(status_code=404, detail=f"No candles for {symbol} {timeframe}")

    df = ohlcv_to_dataframe(ohlcv)
    zones_data = analyze_zones(df)

    fib_bands: list[dict] = []
    try:
        fib = auto_fib_retracement(df, levels=(0.236, 0.382, 0.5, 0.618, 0.786), extend_lines=False)
        swing = fib.get("swing") or {}
        for lvl in fib.get("levels", []):
            fib_bands.append({
                "ratio": lvl.get("ratio"),
                "price": lvl.get("price"),
                "in_golden_zone": bool(
                    0.382 <= float(lvl.get("ratio") or 0) <= 0.618
                ),
            })
        fib_swing = {
            "direction": swing.get("direction"),
            "high": swing.get("end_price") if swing.get("direction") == "up" else swing.get("start_price"),
            "low": swing.get("start_price") if swing.get("direction") == "up" else swing.get("end_price"),
        }
    except Exception:
        fib_swing = None

    s_r_levels: list[dict] = []
    try:
        sr = support_resistance_mtf(df, include_lines=False)
        s_r_levels = sr.get("levels", [])
    except Exception:
        pass

    sd = zones_data.get("supply_demand", {})
    ch = zones_data.get("channels", {})

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "price": sd.get("price"),
        "fib": {"swing": fib_swing, "bands": fib_bands},
        "supply_zones": [z for z in sd.get("zones", []) if z["type"] == "supply"],
        "demand_zones": [z for z in sd.get("zones", []) if z["type"] == "demand"],
        "channels": ch.get("channels", []),
        "s_r_levels": s_r_levels,
    }


@router.post("/tradingview/webhook", response_model=SignalResponse)
async def tradingview_webhook(
    request: Request,
    payload: TradingViewWebhook,
    db: AsyncSession = Depends(get_db),
    x_webhook_signature: Optional[str] = Header(None),
):
    """
    TradingView webhook endpoint
    Receives and processes trading signals from TradingView alerts
    
    Example TradingView alert message:
    ```json
    {
        "action": "buy",
        "symbol": "BTC/USDT",
        "price": {{close}},
        "timeframe": "{{interval}}",
        "indicator_values": {
            "rsi": {{rsi}},
            "macd": {{macd}}
        }
    }
    ```
    """
    # Validate webhook signature
    body = await request.body()
    validate_tradingview_webhook(body, x_webhook_signature)
    
    # Validate payload
    payload_dict = payload.dict()
    if not SignalService.validate_tradingview_payload(payload_dict):
        raise HTTPException(status_code=400, detail="Invalid webhook payload")
    
    logger.info(f"📡 TradingView webhook received: {payload.action} {payload.symbol}")
    
    try:
        # Process and store signal
        signal = await SignalService.process_tradingview_signal(db, payload_dict)
        record_signal_created("tradingview", payload.action.value)
        await AlertService.notify(
            title="TradingView signal received",
            message=f"{payload.action.upper()} {payload.symbol} on {payload.timeframe}",
            level="INFO",
            details={"source": "tradingview", "price": payload.price},
        )
        
        return signal
    except Exception as e:
        logger.error(f"Error processing TradingView webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Enhanced Pipeline Endpoints (must be before /{signal_id}) ──

@router.get("/analysis/multi/{symbol}")
async def get_multi_timeframe_analysis(symbol: str):
    """
    Run multi-timeframe TA (5m, 15m, 1h, 4h) for a symbol.
    Returns per-timeframe scores + aggregated decision.
    """
    if "/" not in symbol and symbol.upper().endswith("USDT"):
        symbol = symbol[:-4] + "/USDT"

    result = await analyze_multi_timeframe(symbol)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.get("/ar-atr/{symbol}")
async def get_ar_atr_analysis(
    symbol:        str,
    timeframe:     str   = "1h",
    exchange:      str   = "bitget",
    st_period:     int   = 10,
    st_mult:       float = 3.0,
    adx_threshold: float = 25.0,
    sl_mult:       float = 1.5,
    trail_mult:    float = 2.0,
):
    """
    Run the AR-ATR Trend Multi-Confirmation strategy on a single timeframe.

    Four confirmations must all fire for a signal:
      1. SuperTrend (direction + dynamic support/resistance)
      2. ADX ≥ threshold (trend strength, default 25)
      3. Volume > Volume MA-20 (institutional participation)
      4. MACD Histogram positive+rising (bull) or negative+falling (bear)

    Stop loss = entry ± ATR × sl_mult.
    Take profit = trailing stop that trails at ATR × trail_mult
                  (only moves in your favour; also exits on SuperTrend flip).

    State: "signal" | "watch" | "no_signal" | "blocked" | "no_data"
    """
    if "/" not in symbol and symbol.upper().endswith("USDT"):
        symbol = symbol[:-4] + "/USDT"

    try:
        exch = SupportedExchange(exchange.lower())
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unsupported exchange: {exchange}")

    result = await analyze_ar_atr(
        symbol         = symbol,
        timeframe      = timeframe,
        exchange       = exch,
        st_period      = st_period,
        st_mult        = st_mult,
        adx_threshold  = adx_threshold,
        sl_mult        = sl_mult,
        trail_mult     = trail_mult,
    )

    if result.get("state") == "no_data" and "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.get("/mtpc/{symbol}")
async def get_mtpc_analysis(symbol: str, exchange: str = "bitget"):
    """
    Run the Multi-Timeframe Trend-Pullback Confluence (MTPC) strategy for a symbol.

    The strategy evaluates 3 timeframes (4H trend → 1H pullback zone → 15M trigger):
      - 4H: macro trend via 50 EMA + ADX
      - 1H: Fibonacci 38.2–61.8% retrace zone + S/R levels + 20 MA
      - 15M: entry trigger (engulfing / pin bar / RSI reversal)

    Returns signal state ("signal" | "setup_only" | "blocked" | "no_data"),
    confluence score (0–5), and full trade parameters (entry, SL, TP1, TP2).
    """
    if "/" not in symbol and symbol.upper().endswith("USDT"):
        symbol = symbol[:-4] + "/USDT"

    try:
        exch = SupportedExchange(exchange.lower())
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unsupported exchange: {exchange}")

    result = await analyze_mtpc(symbol, exchange=exch)
    if result.get("mtpc_state") == "no_data" and "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/pipeline/run")
async def run_pipeline(
    db: AsyncSession = Depends(get_db),
):
    """Manually trigger one full signal pipeline cycle (multi-TF TA + sentiment)."""
    try:
        result = await run_signal_pipeline(db)
        return result
    except Exception as e:
        logger.error(f"Pipeline run error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/smc/generate")
async def generate_smc_signal(
    req: GenerateSmcSignalRequest,
    db: AsyncSession = Depends(get_db),
):
    """Generate a Smart Money Concepts signal and enrich it with AI + insights."""
    symbol = _normalize_symbol(req.symbol)
    if "/" not in symbol:
        raise HTTPException(status_code=400, detail="symbol must be in BASE/QUOTE format")

    try:
        SupportedExchange(req.exchange.lower())
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unsupported exchange: {req.exchange}")

    script = await _resolve_smc_script(db, req.script_id)
    base_coin = symbol.split("/")[0]
    refresh_state = {"did_refresh": False}
    max_news_age_hours = max(1, int(getattr(settings, "SENTIMENT_MAX_AGE_HOURS", 2) or 2))

    eval_req = StrategyEvalRequest(
        symbol=symbol,
        timeframe=req.timeframe,
        exchange=req.exchange.lower(),
        limit=req.limit,
    )
    eval_result = await cast(Any, evaluate_pinescript)(cast(int, script.id), eval_req, db)

    smc_action = str(eval_result.get("action", SignalAction.HOLD.value)).lower()
    smc_score = float(eval_result.get("score") or 0.0)
    smc_confidence = float(eval_result.get("confidence") or 0.0)
    indicator_values = dict(eval_result.get("indicator_values") or {})

    price = (
        _safe_float(eval_result.get("price"))
        or _safe_float(indicator_values.get("entry_price"))
        or _safe_float(indicator_values.get("last_price"))
        or _safe_float(indicator_values.get("close"))
    )
    if price is not None:
        indicator_values["entry_price"] = price

    ai_data: dict = {}
    if req.use_ai_agents:
        try:
            ai_data = await AgentOrchestrator.analyze_symbol(db, symbol, req.timeframe, trigger="manual")
        except Exception as exc:
            logger.warning(f"SMC AI analysis failed for {symbol}: {exc}")
            ai_data = {"error": str(exc)}

    insights_data: Optional[dict] = None
    btc_insights_data: Optional[dict] = None
    if req.use_insights:
        try:
            insights_data = await _get_sentiment_with_refresh(
                db,
                base_coin,
                max_age_hours=max_news_age_hours,
                refresh_if_stale=req.refresh_news_if_stale,
                refresh_state=refresh_state,
            )
            btc_insights_data = insights_data if base_coin == "BTC" else await _get_sentiment_with_refresh(
                db,
                "BTC",
                max_age_hours=max_news_age_hours,
                refresh_if_stale=req.refresh_news_if_stale,
                refresh_state=refresh_state,
            )
        except Exception as exc:
            logger.warning(f"SMC insights lookup failed for {symbol}: {exc}")

    ai_action = str(ai_data.get("final_action", "")).lower() if ai_data else ""
    ai_confidence = float(ai_data.get("final_confidence") or 0.0) if ai_data else 0.0
    if ai_action == SignalAction.BUY.value:
        ai_score = ai_confidence
    elif ai_action == SignalAction.SELL.value:
        ai_score = -ai_confidence
    else:
        ai_score = 0.0

    insights_score = float((insights_data or {}).get("score") or 0.0)
    btc_score = float((btc_insights_data or {}).get("score") or 0.0)
    btc_confidence = _clamp(float((btc_insights_data or {}).get("confidence") or 0.35), 0.0, 1.0)
    btc_label = str((btc_insights_data or {}).get("label") or "no_data")
    btc_articles = int((btc_insights_data or {}).get("article_count") or (btc_insights_data or {}).get("sources_count") or 0)

    direction_hint = (
        1 if smc_action == SignalAction.BUY.value
        else -1 if smc_action == SignalAction.SELL.value
        else 1 if ai_score > 0
        else -1 if ai_score < 0
        else 1 if smc_score > 0
        else -1 if smc_score < 0
        else 0
    )

    btc_news_adjustment = 0.0
    btc_news_confirms: Optional[bool] = None
    if req.use_insights and btc_insights_data and direction_hint != 0 and abs(btc_score) >= 0.05:
        btc_news_confirms = (btc_score > 0 and direction_hint > 0) or (btc_score < 0 and direction_hint < 0)
        if btc_news_confirms:
            btc_news_adjustment = min(0.12, abs(btc_score) * max(0.35, btc_confidence) * 0.16)
            ai_score_adjustment = btc_news_adjustment if direction_hint > 0 else -btc_news_adjustment
        else:
            btc_news_adjustment = min(0.16, abs(btc_score) * max(0.35, btc_confidence) * 0.20)
            ai_score_adjustment = -btc_news_adjustment if direction_hint > 0 else btc_news_adjustment
    else:
        ai_score_adjustment = 0.0

    fused_score = _clamp(
        (smc_score * 0.7) + (ai_score * 0.2) + (insights_score * 0.1) + ai_score_adjustment,
        -1.0,
        1.0,
    )

    if fused_score >= 0.2:
        final_action = SignalAction.BUY.value
    elif fused_score <= -0.2:
        final_action = SignalAction.SELL.value
    elif smc_action in {SignalAction.BUY.value, SignalAction.SELL.value}:
        final_action = smc_action
    else:
        final_action = SignalAction.HOLD.value

    fused_confidence = _clamp(
        max(smc_confidence, abs(fused_score), ai_confidence * 0.8),
        0.0,
        1.0,
    )

    volume_ratio = _safe_float(indicator_values.get("volume_ratio"))
    buy_ratio = _safe_float(indicator_values.get("buy_ratio"))
    order_flow_bias = _clamp((buy_ratio - 0.5) * 2.0, -1.0, 1.0) if buy_ratio is not None else None
    latest_volume = _safe_float(indicator_values.get("latest_volume") or indicator_values.get("volume"))
    volume_ma = _safe_float(indicator_values.get("vol_ma") or indicator_values.get("volume_ma20"))

    volume_confirmed = volume_ratio is not None and volume_ratio >= 1.15
    directional_volume_confirmed: Optional[bool] = None
    if final_action == SignalAction.BUY.value and buy_ratio is not None:
        directional_volume_confirmed = buy_ratio >= 0.60
    elif final_action == SignalAction.SELL.value and buy_ratio is not None:
        directional_volume_confirmed = buy_ratio <= 0.40

    confidence_penalty = 0.0
    if final_action in {SignalAction.BUY.value, SignalAction.SELL.value}:
        if volume_ratio is not None:
            if volume_ratio < 1.00:
                confidence_penalty += 0.28
            elif volume_ratio < 1.15:
                confidence_penalty += 0.16
        if directional_volume_confirmed is False:
            confidence_penalty += 0.22
        elif directional_volume_confirmed is None:
            confidence_penalty += 0.12
        if final_action == SignalAction.BUY.value and order_flow_bias is not None and order_flow_bias < 0:
            confidence_penalty += min(0.30, abs(order_flow_bias) * 0.28)
        elif final_action == SignalAction.SELL.value and order_flow_bias is not None and order_flow_bias > 0:
            confidence_penalty += min(0.30, abs(order_flow_bias) * 0.28)
        if btc_news_confirms is False and abs(btc_score) >= 0.20:
            confidence_penalty += min(0.22, abs(btc_score) * max(0.4, btc_confidence) * 0.25)
        if price is None:
            confidence_penalty += 0.35

    fused_confidence = _clamp(fused_confidence - confidence_penalty, 0.0, 1.0)

    btc_headwind = (
        final_action in {SignalAction.BUY.value, SignalAction.SELL.value}
        and btc_news_confirms is False
        and abs(btc_score) >= 0.30
        and fused_confidence < 0.72
    )

    if final_action in {SignalAction.BUY.value, SignalAction.SELL.value} and (
        price is None
        or fused_confidence < 0.45
        or not volume_confirmed
        or directional_volume_confirmed is not True
        or btc_headwind
    ):
        final_action = SignalAction.HOLD.value

    market_price = price
    entry_mode = _normalize_entry_mode(req.entry_mode)
    entry_candidates: List[dict] = []
    selected_entry = {
        "label": "market_now",
        "title": "Market Now",
        "price": market_price,
        "mode": entry_mode,
        "is_limit": False,
        "reason": "Signal uses market reference",
    }
    if final_action in {SignalAction.BUY.value, SignalAction.SELL.value} and market_price is not None and market_price > 0:
        entry_candidates = _build_entry_candidates(final_action, market_price, indicator_values)
        selected_entry = _select_entry_candidate(
            action=final_action,
            market_price=market_price,
            candidates=entry_candidates,
            entry_mode=entry_mode,
            selected_entry_label=req.selected_entry_label,
            custom_entry_price=req.custom_entry_price,
        )
        selected_entry_price = _safe_float(selected_entry.get("price"))
        if selected_entry_price and selected_entry_price > 0:
            price = selected_entry_price
            indicator_values["entry_price"] = price

    entry_quality_label = "wait"
    entry_quality_reasons: List[str] = []
    if price is None:
        entry_quality_label = "invalid"
        entry_quality_reasons.append("Missing entry price from strategy evaluation")
    elif final_action == SignalAction.HOLD.value:
        entry_quality_label = "wait"
        entry_quality_reasons.append("Signal did not pass confidence and volume confirmation gates")
    else:
        quality_score = fused_confidence
        if volume_ratio is not None:
            if volume_ratio >= 1.25:
                quality_score += 0.08
                entry_quality_reasons.append("Strong volume confirmation")
            elif volume_ratio >= 1.10:
                quality_score += 0.04
                entry_quality_reasons.append("Volume confirms entry")
            elif volume_ratio < 1.00:
                quality_score -= 0.12
                entry_quality_reasons.append("Below-average volume lowers entry quality")

        if directional_volume_confirmed is False:
            quality_score -= 0.12
            entry_quality_reasons.append("Buy/sell volume split does not confirm trade direction")
        elif directional_volume_confirmed is None:
            quality_score -= 0.08
            entry_quality_reasons.append("Buy/sell volume split unavailable")

        if btc_news_confirms is True and abs(btc_score) >= 0.10:
            quality_score += 0.05
            entry_quality_reasons.append("BTC news sentiment aligns with trade direction")
        elif btc_news_confirms is False and abs(btc_score) >= 0.10:
            quality_score -= 0.10
            entry_quality_reasons.append("BTC news sentiment conflicts with trade direction")

        quality_score = _clamp(quality_score, 0.0, 1.0)
        if quality_score >= 0.75:
            entry_quality_label = "good"
        elif quality_score >= 0.58:
            entry_quality_label = "fair"
        else:
            entry_quality_label = "weak"

        if selected_entry.get("is_limit"):
            entry_quality_reasons.append(
                f"Selected {selected_entry.get('title') or selected_entry.get('label')} as limit entry"
            )
        else:
            entry_quality_reasons.append("Using market-adjacent entry for fill certainty")

    volume_context = {
        "latest_volume": latest_volume,
        "volume_ma": volume_ma,
        "volume_ratio": volume_ratio,
        "buy_ratio": buy_ratio,
        "order_flow_bias": order_flow_bias,
        "volume_confirmed": volume_confirmed,
        "directional_confirmed": directional_volume_confirmed,
    }

    btc_news_context = {
        "score": btc_score,
        "label": btc_label,
        "confidence": btc_confidence,
        "article_count": btc_articles,
        "confirms": btc_news_confirms,
        "adjustment": btc_news_adjustment,
    }

    stop_loss, take_profit = _resolve_levels(
        final_action,
        price,
        indicator_values,
        req.stop_loss_pct,
        req.take_profit_pct,
    )
    if final_action == SignalAction.HOLD.value:
        stop_loss = None
        take_profit = None
        indicator_values.pop("stop_loss", None)
        indicator_values.pop("take_profit", None)
    else:
        if stop_loss is not None:
            indicator_values["stop_loss"] = stop_loss
        if take_profit is not None:
            indicator_values["take_profit"] = take_profit

    payload = {
        "strategy": "smart_money_concepts",
        "script_id": script.id,
        "script_name": script.name,
        "symbol": symbol,
        "timeframe": req.timeframe,
        "smc_action": smc_action,
        "smc_score": smc_score,
        "smc_confidence": smc_confidence,
        "final_action": final_action,
        "final_score": fused_score,
        "final_confidence": fused_confidence,
        "market_price": market_price,
        "entry_price": price,
        "entry_mode": entry_mode,
        "selected_entry": selected_entry,
        "entry_candidates": entry_candidates,
        "entry_quality": {
            "label": entry_quality_label,
            "reasons": entry_quality_reasons,
        },
        "volume_context": volume_context,
        "btc_news_context": btc_news_context,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "ai": ai_data if req.use_ai_agents else None,
        "insights": insights_data if req.use_insights else None,
        "btc_insights": btc_insights_data if req.use_insights else None,
        "origin": "smc",
    }

    cleanup_removed = 0
    remove_scope = _normalize_remove_scope(req.remove_old_scope)
    created_signal: Optional[Signal] = None
    if req.persist_signal:
        if req.remove_old_signals:
            cleanup_query = delete(Signal).where(Signal.source == SignalSource.SMC)
            if remove_scope == "symbol_timeframe":
                cleanup_query = cleanup_query.where(
                    Signal.symbol == symbol,
                    Signal.timeframe == req.timeframe,
                )
            elif remove_scope == "symbol":
                cleanup_query = cleanup_query.where(Signal.symbol == symbol)

            cleanup_result = await db.execute(cleanup_query)
            cleanup_removed = int(getattr(cleanup_result, "rowcount", 0) or 0)

        if final_action == SignalAction.BUY.value:
            signal_action = SignalAction.BUY
        elif final_action == SignalAction.SELL.value:
            signal_action = SignalAction.SELL
        else:
            signal_action = SignalAction.HOLD

        created_signal = await SignalService.create_signal(
            db,
            SignalCreate(
                source=SignalSource.SMC,
                symbol=symbol,
                action=signal_action,
                price=price,
                timeframe=req.timeframe,
                strength=min(1.0, abs(fused_score)),
                confidence=fused_confidence,
                raw_data=json.dumps(payload),
                indicators=json.dumps(indicator_values),
            ),
        )

        if signal_action == SignalAction.HOLD:
            created_signal = await SignalService.update_signal_status(
                db,
                cast(int, created_signal.id),
                SignalStatus.IGNORED,
            )

        record_signal_created(SignalSource.SMC.value, final_action)

    return {
        "source": SignalSource.SMC.value,
        "symbol": symbol,
        "timeframe": req.timeframe,
        "script": {"id": script.id, "name": script.name},
        "action": final_action,
        "confidence": fused_confidence,
        "score": fused_score,
        "price": market_price,
        "market_price": market_price,
        "entry_price": price,
        "entry_mode": entry_mode,
        "selected_entry": selected_entry,
        "entry_candidates": entry_candidates,
        "entry_quality": {
            "label": entry_quality_label,
            "reasons": entry_quality_reasons,
        },
        "volume_context": volume_context,
        "btc_news_context": btc_news_context,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "ai_agents": ai_data if req.use_ai_agents else None,
        "insights": insights_data if req.use_insights else None,
        "btc_insights": btc_insights_data if req.use_insights else None,
        "remove_scope": remove_scope,
        "cleanup_removed": cleanup_removed,
        "signal": _serialize_signal(created_signal) if created_signal else None,
        "saved": created_signal is not None,
    }


@router.get("/smc/signals")
async def get_smc_signals(
    limit: int = 100,
    status: Optional[SignalStatus] = None,
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """List SMC-generated signals with parsed metadata."""
    query = select(Signal).where(Signal.source == SignalSource.SMC)
    if status:
        query = query.where(Signal.status == status)
    if symbol:
        query = query.where(Signal.symbol == _normalize_symbol(symbol))
    if timeframe:
        query = query.where(Signal.timeframe == timeframe)

    rows = (
        await db.execute(query.order_by(desc(Signal.created_at)).limit(limit))
    ).scalars().all()

    signals = [_serialize_signal(row) for row in rows]
    return {"signals": signals, "count": len(signals)}


# ── Crypto SMC sniper (shares every module with the MT5 path) ────────────────
# This endpoint runs the SAME SMCStrategyEngine, the SAME smc_scoring factor
# model, the SAME three-tier analysis router + deterministic floor, and the SAME
# learning loop as /plugins/mt5/strategy/analyze — only the candle source
# differs (ccxt instead of the MT5 bridge). Nothing is forked.
#
# The PineScript-based /smc/generate above is deliberately left untouched.

#: Entry timeframe -> the higher timeframe whose bias gates it (ccxt notation).
_CRYPTO_HTF_FOR = {"1m": "15m", "5m": "1h", "15m": "4h", "30m": "4h", "1h": "4h"}


async def _crypto_candles(exchange: str, symbol: str, timeframe: str, limit: int):
    """Fetch ccxt candles and adapt them to the SMC engine's Candle type."""
    from app.api.exchanges import get_ohlcv
    from plugins.MT5TradingPlugin.backend.services.smc_strategy import (
        candles_from_payload,
    )

    payload = await get_ohlcv(
        exchange=SupportedExchange(exchange), symbol=symbol,
        timeframe=timeframe, limit=limit,
    )
    rows = (payload or {}).get("data") or []
    return candles_from_payload(rows)


@router.get("/smc/analyze")
async def crypto_smc_analyze(
    symbol: str,
    exchange: str = "bitget",
    timeframe: str = "1h",
    count: int = 400,
    min_rr: float = 1.5,
    max_rr: float = 3.0,
    sl_buffer_atr: float = 1.0,
    min_confidence: float = 0.6,
    use_ai: bool = True,
    db: AsyncSession = Depends(get_db),
):
    """Smart Money Concepts sniper analysis for a crypto pair.

    Same never-fail contract as the MT5 endpoint: the response always carries a
    complete `ai` block. When every provider is unreachable the deterministic
    SMC floor answers, so this can never return empty, null, or an error.
    """
    from plugins.MT5TradingPlugin.backend.services import smc_floor, smc_memory
    from plugins.MT5TradingPlugin.backend.services.smc_ai import ai_review
    from plugins.MT5TradingPlugin.backend.services.smc_strategy import (
        SMCStrategyEngine,
        contract_size_for_symbol,
    )

    try:
        candles = await _crypto_candles(exchange, symbol, timeframe, count)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[crypto/SMC] candle fetch failed for {symbol}: {exc}")
        candles = []

    # Higher-timeframe gate — same rule as MT5, optional and non-blocking.
    htf_candles = None
    htf_tf = _CRYPTO_HTF_FOR.get(timeframe.lower())
    if htf_tf:
        try:
            htf_candles = await _crypto_candles(exchange, symbol, htf_tf, 300)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[crypto/SMC] HTF {htf_tf} skipped for {symbol}: {exc}")

    weights = await smc_memory.learned_weights(db, market="crypto", symbol=symbol)
    engine = SMCStrategyEngine(
        min_rr=min_rr, max_rr=max_rr, sl_buffer_atr=sl_buffer_atr,
        min_confidence=min_confidence, symbol=symbol,
        contract_size=contract_size_for_symbol(symbol),
        factor_weights=weights,
    )
    # Same dollar/VIX context the MT5 path scores with — this endpoint runs the
    # same engine on crypto candles, and the two must not diverge.
    from app.services.macro_context import resolve_macro_bias

    macro = await resolve_macro_bias(symbol)
    analysis = engine.analyze(candles, htf_candles=htf_candles, macro=macro)

    if use_ai:
        try:
            ai_block = await ai_review(
                db=db, symbol=symbol, timeframe=timeframe,
                analysis=analysis, market="crypto",
            )
        except Exception as exc:  # noqa: BLE001 — last line of defence
            logger.warning(f"[crypto/SMC] AI review raised, using floor: {exc}")
            ai_block = smc_floor.build(analysis, reason=str(exc)[:200])
    else:
        ai_block = smc_floor.build(analysis, reason="AI review disabled by request")

    await smc_memory.record_analysis(
        db, market="crypto", symbol=symbol, timeframe=timeframe,
        analysis=analysis, ai_block=ai_block,
    )

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "exchange": exchange,
        "ai": ai_block,
        "provider_used": ai_block.get("provider_used"),
        "tier": ai_block.get("tier"),
        "is_degraded": bool(ai_block.get("is_degraded")),
        **analysis,
    }


@router.get("/smc/overview")
async def get_smc_overview(
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
    limit: int = 100,
    rug_limit: int = 50,
    sniper_limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    """SMC dashboard data: SMC signals + sniper positions + rug-pull watchlist."""
    normalized_symbol = _normalize_symbol(symbol) if symbol else None
    base_symbol = normalized_symbol.split("/")[0] if normalized_symbol else None
    lookback_hours = max(int(getattr(settings, "PUMP_MONITOR_PUMPED_RETENTION_HOURS", 24)), 1)
    recent_cutoff = now_sast() - timedelta(hours=lookback_hours)

    smc_query = select(Signal).where(Signal.source == SignalSource.SMC)
    if normalized_symbol:
        smc_query = smc_query.where(Signal.symbol == normalized_symbol)
    if timeframe:
        smc_query = smc_query.where(Signal.timeframe == timeframe)

    smc_rows = (
        await db.execute(smc_query.order_by(desc(Signal.created_at)).limit(limit))
    ).scalars().all()
    smc_signals = [_serialize_signal(row) for row in smc_rows]
    smc_signal_ids = {row.id for row in smc_rows}

    base_sniper_query = select(Trade).where(
        Trade.source == "sniper",
        Trade.created_at >= recent_cutoff,
    )
    sniper_query = base_sniper_query
    if normalized_symbol:
        sniper_query = sniper_query.where(Trade.symbol == normalized_symbol)
    sniper_rows = (
        await db.execute(sniper_query.order_by(desc(Trade.created_at)).limit(sniper_limit))
    ).scalars().all()
    if normalized_symbol and not sniper_rows:
        sniper_rows = (
            await db.execute(base_sniper_query.order_by(desc(Trade.created_at)).limit(sniper_limit))
        ).scalars().all()

    sniper_positions = []
    for trade in sniper_rows:
        sniper_positions.append(
            {
                "id": trade.id,
                "symbol": trade.symbol,
                "side": trade.side,
                "status": trade.status,
                "amount": trade.amount,
                "price": trade.price,
                "average_price": trade.average_price,
                "stop_loss": trade.stop_loss,
                "take_profit": trade.take_profit,
                "pnl": trade.pnl,
                "pnl_percentage": trade.pnl_percentage,
                "signal_id": trade.signal_id,
                "created_at": _to_iso(getattr(trade, "created_at", None)),
                "closed_at": _to_iso(getattr(trade, "closed_at", None)),
                "source": "smc_signal" if trade.signal_id in smc_signal_ids else "sniper_engine",
            }
        )

    if not sniper_positions and smc_rows:
        for row in smc_rows[:sniper_limit]:
            action = _enum_to_str(getattr(row, "action", None), "hold").lower()
            if action not in {"buy", "sell"}:
                continue
            raw_data = _safe_json(getattr(row, "raw_data", None))
            sniper_positions.append(
                {
                    "id": -cast(int, row.id),
                    "symbol": row.symbol,
                    "side": action,
                    "status": _enum_to_str(getattr(row, "status", None), "pending"),
                    "amount": None,
                    "price": row.price,
                    "average_price": row.price,
                    "stop_loss": raw_data.get("stop_loss"),
                    "take_profit": raw_data.get("take_profit"),
                    "pnl": None,
                    "pnl_percentage": None,
                    "signal_id": cast(int, row.id),
                    "created_at": _to_iso(getattr(row, "created_at", None)),
                    "closed_at": None,
                    "source": "smc_signal",
                }
            )

    rug_recent_filter = or_(
        and_(RugPullToken.updated_at.is_not(None), RugPullToken.updated_at >= recent_cutoff),
        and_(RugPullToken.updated_at.is_(None), RugPullToken.detected_at >= recent_cutoff),
    )
    base_rug_query = select(RugPullToken).where(rug_recent_filter)
    rug_query = base_rug_query
    if base_symbol:
        rug_query = rug_query.where(func.upper(RugPullToken.symbol) == base_symbol)
    rug_rows = (
        await db.execute(rug_query.order_by(desc(RugPullToken.updated_at), desc(RugPullToken.detected_at)).limit(rug_limit))
    ).scalars().all()
    if base_symbol and not rug_rows:
        rug_rows = (
            await db.execute(
                base_rug_query.order_by(desc(RugPullToken.updated_at), desc(RugPullToken.detected_at)).limit(rug_limit)
            )
        ).scalars().all()

    rug_tokens = []
    for token in rug_rows:
        rug_tokens.append(
            {
                "id": token.id,
                "coin_id": token.coin_id,
                "symbol": token.symbol,
                "name": token.name,
                "status": _enum_to_str(getattr(token, "status", None), "watching"),
                "price_at_detection": token.price_at_detection,
                "current_price": token.current_price,
                "price_change_24h": token.price_change_24h,
                "price_change_since_detection": token.price_change_since_detection,
                "risk_score": token.risk_score,
                "recommended_entry": token.recommended_entry,
                "recommended_sl": token.recommended_sl,
                "recommended_tp": token.recommended_tp,
                "detected_at": _to_iso(getattr(token, "detected_at", None)),
                "updated_at": _to_iso(getattr(token, "updated_at", None)),
            }
        )

    if not rug_tokens:
        pump_recent_filter = or_(
            and_(PumpToken.updated_at.is_not(None), PumpToken.updated_at >= recent_cutoff),
            and_(PumpToken.updated_at.is_(None), PumpToken.detected_at >= recent_cutoff),
        )
        pump_rows = (
            await db.execute(
                select(PumpToken)
                .where(pump_recent_filter)
                .order_by(desc(PumpToken.updated_at), desc(PumpToken.detected_at))
                .limit(rug_limit)
            )
        ).scalars().all()
        for token in pump_rows:
            rug_tokens.append(
                {
                    "id": -cast(int, token.id),
                    "coin_id": token.coin_id,
                    "symbol": token.symbol,
                    "name": token.name,
                    "status": f"pump_{_enum_to_str(getattr(token, 'status', None), 'detected')}",
                    "price_at_detection": token.price_at_detection,
                    "current_price": token.current_price,
                    "price_change_24h": token.price_change_24h,
                    "price_change_since_detection": token.gain_since_detection,
                    "risk_score": token.pump_score,
                    "recommended_entry": token.current_price,
                    "recommended_sl": None,
                    "recommended_tp": None,
                    "detected_at": _to_iso(getattr(token, "detected_at", None)),
                    "updated_at": _to_iso(getattr(token, "updated_at", None)),
                }
            )

    return {
        "source": SignalSource.SMC.value,
        "symbol": normalized_symbol,
        "timeframe": timeframe,
        "window_hours": lookback_hours,
        "smc": {"signals": smc_signals, "count": len(smc_signals)},
        "sniper": {"positions": sniper_positions, "count": len(sniper_positions)},
        "rug_pulls": {"tokens": rug_tokens, "count": len(rug_tokens)},
        "totals": {
            "smc_signals": len(smc_signals),
            "sniper_positions": len(sniper_positions),
            "rug_pull_tokens": len(rug_tokens),
        },
    }


# ── CRUD ──

@router.post("/", response_model=SignalResponse)
async def create_signal(
    signal: SignalCreate,
    db: AsyncSession = Depends(get_db),
):
    """Create a manual signal"""
    try:
        created_signal = await SignalService.create_signal(db, signal)
        record_signal_created(signal.source.value, signal.action.value)
        return created_signal
    except Exception as e:
        logger.error(f"Error creating signal: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/", response_model=list[SignalResponse])
async def get_signals(
    limit: int = 100,
    status: Optional[SignalStatus] = None,
    symbol: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Get signals with optional filters"""
    try:
        signals = await SignalService.get_signals(db, limit, status, symbol)
        return signals
    except Exception as e:
        logger.error(f"Error fetching signals: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Signal Monitor Pairs (user-configured) ──

class MonitorPairsRequest(BaseModel):
    symbols: List[str]


@router.get("/monitor-pairs/list")
async def get_monitor_pairs(db: AsyncSession = Depends(get_db)):
    """Get all configured signal monitoring pairs."""
    rows = (await db.execute(
        select(SignalMonitorPair).where(SignalMonitorPair.is_active == True).order_by(SignalMonitorPair.created_at)
    )).scalars().all()
    pairs = [{"symbol": r.symbol, "source": str(getattr(r, "source", ""))} for r in rows]
    symbols = [r.symbol for r in rows]
    trending = [r.symbol for r in rows if str(getattr(r, "source", "")) == "trending"]
    user = [r.symbol for r in rows if str(getattr(r, "source", "")) == "user"]
    return {
        "pairs": symbols,
        "pair_details": pairs,
        "count": len(symbols),
        "trending_count": len(trending),
        "user_count": len(user),
        "defaults": DEFAULT_PAIRS,
    }


@router.post("/monitor-pairs/set")
async def set_monitor_pairs(req: MonitorPairsRequest, db: AsyncSession = Depends(get_db)):
    """Replace user-configured monitor pairs (trending pairs are preserved)."""
    valid = [s.strip() for s in req.symbols if s.strip() and "/" in s.strip()]
    if len(valid) > 500:
        raise HTTPException(status_code=400, detail="Maximum 500 pairs")

    # Only remove user pairs — trending pairs are managed by the sync
    await db.execute(
        delete(SignalMonitorPair).where(SignalMonitorPair.source == "user")
    )

    # Insert new user pairs (skip if symbol already exists as trending)
    existing = set(
        (await db.execute(select(SignalMonitorPair.symbol))).scalars().all()
    )
    added = []
    for sym in valid:
        if sym not in existing:
            db.add(SignalMonitorPair(symbol=sym, is_active=True, source="user"))
            added.append(sym)
            existing.add(sym)
    await db.commit()

    return {"pairs": added, "count": len(added)}


@router.post("/monitor-pairs/add")
async def add_monitor_pair(req: MonitorPairsRequest, db: AsyncSession = Depends(get_db)):
    """Add one or more pairs to monitoring as user pairs (ignores duplicates)."""
    added = []
    for sym in req.symbols:
        sym = sym.strip()
        if not sym or "/" not in sym:
            continue
        exists = (await db.execute(
            select(SignalMonitorPair).where(SignalMonitorPair.symbol == sym)
        )).scalar_one_or_none()
        if not exists:
            db.add(SignalMonitorPair(symbol=sym, is_active=True, source="user"))
            added.append(sym)
    await db.commit()
    return {"added": added, "count": len(added)}


@router.delete("/monitor-pairs/remove")
async def remove_monitor_pair(req: MonitorPairsRequest, db: AsyncSession = Depends(get_db)):
    """Remove one or more pairs from monitoring."""
    for sym in req.symbols:
        await db.execute(delete(SignalMonitorPair).where(SignalMonitorPair.symbol == sym.strip()))
    await db.commit()
    return {"removed": [s.strip() for s in req.symbols]}


# /{signal_id} must be last — it's a wildcard
@router.get("/{signal_id}", response_model=SignalResponse)
async def get_signal(
    signal_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get a specific signal by ID"""
    signal = await SignalService.get_signal(db, signal_id)
    if not signal:
        raise HTTPException(status_code=404, detail="Signal not found")
    return signal


@router.put("/{signal_id}/status")
async def update_signal_status(
    signal_id: int,
    status: SignalStatus,
    error_message: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Update signal processing status"""
    signal = await SignalService.update_signal_status(
        db, signal_id, status, error_message
    )
    if not signal:
        raise HTTPException(status_code=404, detail="Signal not found")
    return signal
