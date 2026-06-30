"""
Enhanced Signal Pipeline (OctoBot-inspired)
Combines multi-timeframe technical analysis (RSI, MACD, MA, Volume, BB, ADX, StochRSI)
from Bitget with sentiment data from 20+ news sources.
Uses evaluator-style scoring with strict multi-confirmation requirements.
Designed to run every 3 minutes via the scheduler.
"""
import json
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession

from app.exchanges.manager import exchange_manager, SupportedExchange
from app.core.config import settings
from app.core.timezone import now_sast
from app.signals.technical import (
    analyze as technical_analyze,
    ohlcv_to_dataframe,
    sma, ema, rsi as calc_rsi, macd as calc_macd,
    bollinger_bands, buy_sell_volume, volume_ma,
    adx as calc_adx, stochastic_rsi, atr as calc_atr,
)
from app.sentiment.enhanced_service import EnhancedSentimentService
from app.signals.service import SignalService
from app.models.schemas import SignalCreate
from app.models.database import SignalSource, SignalAction, BotStrategy, PineScript
from loguru import logger
import numpy as np
from sqlalchemy import select


EXCHANGE = SupportedExchange.BITGET

# Timeframes to analyze — lower TF for entry, higher for trend confirmation
MULTI_TF = ["5m", "15m", "1h", "4h"]

# Ordered timeframes for building confirmation sets
_TF_ORDER = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"]

# Default trading pairs
DEFAULT_PAIRS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT",
    "XRP/USDT", "ADA/USDT", "DOGE/USDT", "AVAX/USDT",
    "DOT/USDT", "LINK/USDT", "NEAR/USDT", "ARB/USDT",
    "OP/USDT", "MATIC/USDT",
]

# Signal cooldown — don't re-signal same pair within N minutes
SIGNAL_COOLDOWN_MINUTES = 15
_last_signal_time: Dict[str, datetime] = {}


def _prune_cooldown_cache():
    """Remove expired entries from the cooldown cache to prevent memory growth."""
    now = now_sast()
    cutoff = timedelta(minutes=SIGNAL_COOLDOWN_MINUTES * 2)
    expired = [k for k, v in _last_signal_time.items() if (now - v) > cutoff]
    for k in expired:
        del _last_signal_time[k]


def _ohlcv_rows_to_candles(rows: list) -> list[dict[str, Any]]:
    candles: list[dict[str, Any]] = []
    for row in rows or []:
        if isinstance(row, dict):
            candles.append(row)
            continue
        try:
            candles.append({
                "timestamp": row[0],
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]) if len(row) > 5 else 0.0,
            })
        except Exception:
            continue
    return candles


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _sentiment_is_recent(sentiment: Optional[Dict[str, Any]], max_age_hours: int) -> bool:
    if not sentiment:
        return False
    created_at = _parse_iso_datetime(sentiment.get("created_at"))
    if created_at is None:
        return False
    try:
        return (now_sast() - created_at) <= timedelta(hours=max(1, int(max_age_hours)))
    except Exception:
        return False


async def _get_sentiment_with_refresh(
    db: AsyncSession,
    symbol: str,
    sentiment_cache: Dict[str, Optional[Dict[str, Any]]],
    refresh_state: Dict[str, bool],
    *,
    refresh_if_stale: bool,
    max_age_hours: int,
) -> Optional[Dict[str, Any]]:
    normalized_symbol = symbol.upper()
    if normalized_symbol in sentiment_cache:
        return sentiment_cache[normalized_symbol]

    sentiment = await EnhancedSentimentService.get_latest(db, normalized_symbol)
    if sentiment and _sentiment_is_recent(sentiment, max_age_hours):
        sentiment_cache[normalized_symbol] = sentiment
        return sentiment

    if not refresh_if_stale or refresh_state.get("did_refresh"):
        sentiment_cache[normalized_symbol] = sentiment
        return sentiment

    try:
        logger.info("🔄 Refreshing sentiment snapshot for pipeline run")
        await EnhancedSentimentService.run_full_cycle(db, max_age_hours=max_age_hours)
    except Exception as exc:
        logger.warning(f"Pipeline sentiment refresh failed: {exc}")
    finally:
        refresh_state["did_refresh"] = True
        sentiment_cache.clear()

    refreshed = await EnhancedSentimentService.get_latest(db, normalized_symbol)
    sentiment_cache[normalized_symbol] = refreshed or sentiment
    return sentiment_cache[normalized_symbol]


# ────────────────────────────────────────────────────────────
# Multi-Timeframe Technical Analysis
# ────────────────────────────────────────────────────────────

async def analyze_multi_timeframe(symbol: str, primary_timeframe: str = "1h") -> Dict[str, Any]:
    """
    Run TA on the primary timeframe + one higher TF for trend confirmation.
    Uses the user-configured timeframe as the primary analysis frame.
    Returns per-timeframe scores + aggregated decision.
    """
    connector = exchange_manager.get_exchange(EXCHANGE)
    if not connector:
        return {"error": "Exchange not initialized", "symbol": symbol}

    # Build TF list: primary TF + one higher TF for trend confirmation
    primary_tf = primary_timeframe if primary_timeframe in _TF_ORDER else "1h"
    idx = _TF_ORDER.index(primary_tf)
    # Pick one higher TF for confirmation (skip if primary is already the highest)
    confirmation_tfs = []
    for higher_idx in range(idx + 1, len(_TF_ORDER)):
        candidate = _TF_ORDER[higher_idx]
        # Skip TFs too close to primary (e.g. 1h→2h is too close, prefer 4h)
        if higher_idx >= idx + 2:
            confirmation_tfs.append(candidate)
            break
    analysis_tfs = [primary_tf] + confirmation_tfs

    tf_results: Dict[str, Dict] = {}
    errors = []

    for tf in analysis_tfs:
        try:
            limit = 200 if tf in ("5m", "15m") else 200
            ohlcv = await connector.get_ohlcv(symbol=symbol, timeframe=tf, limit=limit)
            ta = technical_analyze(ohlcv, tf)
            if "error" in ta:
                errors.append(f"{tf}: {ta['error']}")
                continue
            tf_results[tf] = ta
        except Exception as e:
            errors.append(f"{tf}: {e}")

    if not tf_results:
        return {"error": f"No TA data: {errors}", "symbol": symbol}

    # ── Weighted aggregate across timeframes ──
    # Primary TF gets 65% weight, confirmation TF gets 35%
    if len(tf_results) == 1:
        norm = {tf: 1.0 for tf in tf_results}
    else:
        tfs_list = list(tf_results.keys())
        # First is primary, rest are confirmation
        norm = {}
        norm[tfs_list[0]] = 0.65
        for ctf in tfs_list[1:]:
            norm[ctf] = 0.35 / (len(tfs_list) - 1)

    agg_score = sum(ta["score"] * norm[tf] for tf, ta in tf_results.items())
    agg_confidence = sum(ta["confidence"] * norm[tf] for tf, ta in tf_results.items())

    # ── Detailed indicator extraction from primary timeframe ──
    primary = tf_results.get(primary_tf) or tf_results[list(tf_results.keys())[0]]

    # ── Multi-TF agreement (OctoBot-style: only act on consensus) ──
    directions = [
        "buy" if ta["score"] > 0.1 else "sell" if ta["score"] < -0.1 else "neutral"
        for ta in tf_results.values()
    ]
    buy_count = directions.count("buy")
    sell_count = directions.count("sell")
    neutral_count = directions.count("neutral")
    total_tf = len(directions)

    trend_alignment = 0.0
    agreement_met = False
    if total_tf >= 2:
        dominant = max(buy_count, sell_count)
        alignment = dominant / total_tf

        if alignment >= 0.75:
            # Strong multi-TF agreement — boost and mark as confirmed
            agreement_met = True
            if buy_count > sell_count:
                trend_alignment = 0.15
            else:
                trend_alignment = -0.15
            agg_score += trend_alignment
        elif alignment >= 0.5:
            # Moderate agreement — smaller boost
            agreement_met = True
            if buy_count > sell_count:
                trend_alignment = 0.08
            else:
                trend_alignment = -0.08
            agg_score += trend_alignment
        else:
            # No agreement — dampen score significantly
            agg_score *= 0.5
            trend_alignment = 0.0

    agg_score = max(-1.0, min(1.0, agg_score))

    # ── ADX trend gate from primary TF ──
    indicators = primary.get("indicators", {})
    adx_val = indicators.get("adx", 25)
    adx_gate = 1.0
    if adx_val is not None and adx_val < 20:
        # Weak trend — dampen score
        adx_gate = adx_val / 20
        agg_score *= adx_gate

    # ── Volume analysis (from primary TF) ──
    volume_score = 0.0
    vol_ratio = indicators.get("volume_ratio", 1.0)
    buy_ratio = indicators.get("buy_ratio", 0.5)

    if vol_ratio and buy_ratio:
        if vol_ratio > 2.0 and buy_ratio > 0.65:
            volume_score = 0.2
        elif vol_ratio > 1.5 and buy_ratio > 0.6:
            volume_score = 0.1
        elif vol_ratio > 2.0 and buy_ratio < 0.35:
            volume_score = -0.2
        elif vol_ratio > 1.5 and buy_ratio < 0.4:
            volume_score = -0.1

    # Volume must confirm direction for actionable signals
    volume_confirms = (volume_score > 0 and agg_score > 0) or (volume_score < 0 and agg_score < 0) or abs(volume_score) < 0.05

    # ── RSI divergence check across timeframes ──
    rsi_values = {}
    for tf, ta in tf_results.items():
        r = ta.get("indicators", {}).get("rsi")
        if r is not None:
            rsi_values[tf] = r

    rsi_signal = 0.0
    if len(rsi_values) >= 2:
        avg_rsi = sum(rsi_values.values()) / len(rsi_values)
        if avg_rsi < 30:
            rsi_signal = 0.15  # Multi-TF oversold
        elif avg_rsi > 70:
            rsi_signal = -0.15  # Multi-TF overbought
        elif avg_rsi < 40:
            rsi_signal = 0.05
        elif avg_rsi > 60:
            rsi_signal = -0.05

    ta_score = max(-1.0, min(1.0, agg_score + volume_score + rsi_signal))

    reasons = list(primary.get("reasons", []))
    if trend_alignment != 0:
        reasons.insert(0, f"Multi-TF trend aligned ({'bullish' if trend_alignment > 0 else 'bearish'}, {buy_count}B/{sell_count}S/{neutral_count}N)")
    elif total_tf >= 2 and not agreement_met:
        reasons.insert(0, f"Multi-TF DISAGREEMENT ({buy_count}B/{sell_count}S/{neutral_count}N) — score dampened")
    if adx_gate < 1.0:
        reasons.append(f"ADX gate ({adx_val:.0f}<20) — score dampened {adx_gate:.2f}x")
    if volume_score != 0:
        reasons.append(f"Volume {'confirms' if volume_confirms else 'DIVERGES'} ({vol_ratio:.1f}x, {buy_ratio:.0%} buy)")
    if rsi_signal != 0:
        avg_r = sum(rsi_values.values()) / len(rsi_values) if rsi_values else 50
        reasons.append(f"Multi-TF RSI {'oversold' if rsi_signal > 0 else 'overbought'} (avg {avg_r:.0f})")

    return {
        "symbol": symbol,
        "ta_score": round(ta_score, 4),
        "ta_confidence": round(agg_confidence, 4),
        "indicators": indicators,
        "timeframes": {
            tf: {"score": ta["score"], "action": ta["action"], "rsi": ta["indicators"].get("rsi"), "adx": ta["indicators"].get("adx")}
            for tf, ta in tf_results.items()
        },
        "volume_score": round(volume_score, 4),
        "volume_confirms": volume_confirms,
        "rsi_signal": round(rsi_signal, 4),
        "trend_alignment": round(trend_alignment, 4),
        "agreement_met": agreement_met,
        "adx_gate": round(adx_gate, 4),
        "reasons": reasons,
    }


# ────────────────────────────────────────────────────────────
# Combined Decision Engine
# ────────────────────────────────────────────────────────────

def compute_final_signal(
    ta_data: Dict,
    sentiment: Optional[Dict],
    btc_sentiment: Optional[Dict] = None,
    strategy_scores: Optional[List[Dict]] = None,
    priority_pine_score: Optional[Dict] = None,
) -> Dict[str, Any]:
    """
    Combine TA + Sentiment + Strategy scores into final signal decision.
    
    Stricter thresholds — only high-confidence signals pass.
    Requirements:
      - Multi-TF agreement must be met
      - Volume should confirm direction
      - ADX gate should not heavily dampen
      - Score must exceed ±0.35 (raised from ±0.25)
    
    strategy_scores: optional list of {"name": str, "score": float, "confidence": float}
    from active strategies for this symbol.
    priority_pine_score: optional single Pine Script score that gets 50% weight
    (selected by user in settings — overrides the 20% strategy weight).
    """
    ta_score = ta_data["ta_score"]
    ta_confidence = ta_data["ta_confidence"]
    reasons = list(ta_data["reasons"])
    agreement_met = ta_data.get("agreement_met", True)
    volume_confirms = ta_data.get("volume_confirms", True)
    adx_gate = ta_data.get("adx_gate", 1.0)

    # ── Sentiment Integration ──
    sent_score = 0.0
    sent_confidence = 0.0
    sent_label = "no_data"
    btc_score = 0.0
    btc_confidence = 0.0
    btc_label = "no_data"
    btc_confirms: Optional[bool] = None

    if sentiment and sentiment.get("score") is not None:
        sent_score = sentiment["score"]
        sent_confidence = sentiment.get("confidence", 0.3)
        sent_label = sentiment.get("label", "neutral")
        articles = sentiment.get("article_count", sentiment.get("sources_count", 0))

        # Sentiment alignment
        if (ta_score > 0 and sent_score > 0) or (ta_score < 0 and sent_score < 0):
            boost = abs(sent_score) * 0.25 * sent_confidence
            ta_score += boost if ta_score > 0 else -boost
            reasons.append(
                f"Sentiment aligns ({sent_label}, {sent_score:+.3f}, "
                f"{articles} articles) → +{boost:.3f}"
            )
        elif abs(sent_score) > 0.15:
            penalty = abs(sent_score) * 0.15 * sent_confidence
            ta_score -= penalty if ta_score > 0 else -penalty
            reasons.append(
                f"Sentiment conflicts ({sent_label}, {sent_score:+.3f}) → -{penalty:.3f}"
            )
        else:
            reasons.append(f"Sentiment neutral ({sent_score:+.3f})")
    else:
        reasons.append("No sentiment data — TA only")

    # ── Strategy Score Integration ──
    # Active strategies provide additional scoring from user-configured indicators
    strategy_boost = 0.0
    if strategy_scores:
        strat_sum = 0.0
        strat_weight = 0.0
        for ss in strategy_scores:
            s_score = ss.get("score", 0)
            s_conf = ss.get("confidence", 0.5)
            strat_sum += s_score * s_conf
            strat_weight += s_conf
        if strat_weight > 0:
            avg_strat = strat_sum / strat_weight
            # Strategy weight: 20% of final decision
            strategy_boost = avg_strat * 0.20
            ta_score += strategy_boost
            strat_names = ", ".join(s["name"] for s in strategy_scores)
            if (strategy_boost > 0 and ta_score > 0) or (strategy_boost < 0 and ta_score < 0):
                reasons.append(f"Strategies align ({strat_names}, avg={avg_strat:+.3f}) → +{abs(strategy_boost):.3f}")
            elif abs(avg_strat) > 0.15:
                reasons.append(f"Strategies conflict ({strat_names}, avg={avg_strat:+.3f}) → {strategy_boost:+.3f}")
            else:
                reasons.append(f"Strategies neutral ({strat_names}, avg={avg_strat:+.3f})")

    # ── Priority Pine Script Integration (50% weight — user's selected script) ──
    pine_boost = 0.0
    if priority_pine_score:
        p_score = priority_pine_score.get("score", 0)
        p_conf = priority_pine_score.get("confidence", 0.5)
        p_name = priority_pine_score.get("name", "Pine Script")
        # 50% weight — this is the user's chosen strategy
        pine_boost = p_score * p_conf * 0.50
        ta_score += pine_boost
        if (pine_boost > 0 and ta_score > 0) or (pine_boost < 0 and ta_score < 0):
            reasons.append(f"🌲 {p_name} confirms ({p_score:+.3f}) → +{abs(pine_boost):.3f}")
        elif abs(p_score) > 0.1:
            reasons.append(f"🌲 {p_name} conflicts ({p_score:+.3f}) → {pine_boost:+.3f}")
        else:
            reasons.append(f"🌲 {p_name} neutral ({p_score:+.3f})")

    # ── BTC Macro Sentiment Context (internet news) ──
    if btc_sentiment and btc_sentiment.get("score") is not None:
        btc_score = float(btc_sentiment.get("score", 0.0))
        btc_confidence = float(btc_sentiment.get("confidence", 0.35) or 0.35)
        btc_label = btc_sentiment.get("label", "neutral")
        direction_hint = 1 if ta_score > 0 else -1 if ta_score < 0 else 0
        if direction_hint != 0 and abs(btc_score) >= 0.05:
            btc_confirms = (btc_score > 0 and direction_hint > 0) or (btc_score < 0 and direction_hint < 0)
            if btc_confirms:
                btc_boost = min(0.12, abs(btc_score) * max(0.35, btc_confidence) * 0.16)
                ta_score += btc_boost if direction_hint > 0 else -btc_boost
                reasons.append(f"BTC news aligns ({btc_label}, {btc_score:+.3f}) → +{btc_boost:.3f}")
            else:
                btc_penalty = min(0.16, abs(btc_score) * max(0.35, btc_confidence) * 0.20)
                ta_score -= btc_penalty if direction_hint > 0 else -btc_penalty
                reasons.append(f"BTC news conflicts ({btc_label}, {btc_score:+.3f}) → -{btc_penalty:.3f}")

    final_score = max(-1.0, min(1.0, ta_score))

    # ── Decision thresholds (tighter for loss reduction) ──
    # Require agreement + reasonable ADX for actionable signals
    buy_threshold = 0.40
    sell_threshold = -0.40

    if final_score >= buy_threshold and agreement_met:
        action = "buy"
    elif final_score <= sell_threshold and agreement_met:
        action = "sell"
    elif final_score >= 0.55:
        # Very strong score overrides lack of TF agreement
        action = "buy"
        reasons.append("Strong score overrides TF disagreement")
    elif final_score <= -0.55:
        action = "sell"
        reasons.append("Strong score overrides TF disagreement")
    else:
        action = "hold"

    # ── Directional order-flow gating (buy/sell volume split) ──
    indicators = ta_data.get("indicators", {})
    buy_ratio_raw = indicators.get("buy_ratio") if isinstance(indicators, dict) else None
    try:
        buy_ratio = float(buy_ratio_raw) if buy_ratio_raw is not None else None
    except (TypeError, ValueError):
        buy_ratio = None

    order_flow_confirmed: Optional[bool] = None
    if action == "buy" and buy_ratio is not None:
        order_flow_confirmed = buy_ratio >= 0.56
    elif action == "sell" and buy_ratio is not None:
        order_flow_confirmed = buy_ratio <= 0.44

    if action != "hold":
        if order_flow_confirmed is False and abs(final_score) < 0.65:
            action = "hold"
            reasons.append("Order-flow split does not confirm direction — downgraded to hold")
        elif order_flow_confirmed is None and abs(final_score) < 0.55:
            action = "hold"
            reasons.append("Order-flow split unavailable — downgraded to hold")

    # Additional BTC headwind guard for marginal signals.
    if action != "hold" and btc_confirms is False and abs(btc_score) >= 0.30 and abs(final_score) < 0.70:
        action = "hold"
        reasons.append("Strong BTC macro headwind — downgraded to hold")

    # Downgrade if volume diverges on marginal signals
    if action != "hold" and not volume_confirms and abs(final_score) < 0.5:
        action = "hold"
        reasons.append("Volume divergence — downgraded to hold")

    confidence = min(1.0, abs(final_score) / 0.44)
    strength = (final_score + 1) / 2  # map [-1,1] → [0,1]

    return {
        "action": action,
        "score": round(final_score, 4),
        "confidence": round(confidence, 4),
        "strength": round(strength, 4),
        "reasons": reasons,
        "sentiment": {
            "score": sent_score,
            "label": sent_label,
            "confidence": sent_confidence,
            "btc_score": btc_score,
            "btc_label": btc_label,
            "btc_confidence": btc_confidence,
            "btc_confirms": btc_confirms,
        },
        "order_flow_confirmed": order_flow_confirmed,
    }


# ────────────────────────────────────────────────────────────
# Full Pipeline — runs every 3 minutes
# ────────────────────────────────────────────────────────────

async def run_signal_pipeline(
    db: AsyncSession,
    pairs: Optional[List[str]] = None,
    timeframe: Optional[str] = None,
    pine_script_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Full pipeline:
      1. Run multi-timeframe TA for each pair
      2. Get latest sentiment (already cached from sentiment cycle)
      3. Evaluate strategies + Pine Scripts
      4. Combine into final signals
      5. Save BUY/SELL signals to DB
    
    timeframe: override timeframe for Pine Script / strategy evaluation
    pine_script_id: when set, this specific script gets 50% weight (priority)
    
    Returns summary.
    """
    pairs = pairs or DEFAULT_PAIRS
    effective_tf = timeframe or "1h"
    logger.info(f"🤖 [SIGNAL PIPELINE] Running for {len(pairs)} pairs (tf={effective_tf}, pine_id={pine_script_id})...")
    start = now_sast()

    # Prune stale cooldown entries to prevent memory growth
    _prune_cooldown_cache()

    results = []
    signals_created = 0
    errors = 0

    # Load active strategies for strategy-based scoring
    active_strategies = []
    try:
        strat_result = await db.execute(
            select(BotStrategy).where(BotStrategy.is_active == True)
        )
        active_strategies = strat_result.scalars().all()
        if active_strategies:
            logger.info(f"📊 {len(active_strategies)} active strategies for signal scoring")
    except Exception as e:
        logger.warning(f"Failed to load active strategies: {e}")

    # Load active Pine Scripts for signal scoring
    active_pine_scripts = []
    priority_pine_script = None
    try:
        pine_result = await db.execute(
            select(PineScript).where(PineScript.is_active == True)
        )
        active_pine_scripts = pine_result.scalars().all()
        if active_pine_scripts:
            logger.info(f"🌲 {len(active_pine_scripts)} active Pine Scripts for signal scoring")
    except Exception as e:
        logger.warning(f"Failed to load active Pine Scripts: {e}")

    # Load priority Pine Script if specified (may not be in active list)
    if pine_script_id:
        try:
            pps_result = await db.execute(
                select(PineScript).where(PineScript.id == pine_script_id)
            )
            priority_pine_script = pps_result.scalar_one_or_none()
            if priority_pine_script:
                logger.info(f"🌲 Priority Pine Script: {priority_pine_script.name} (id={pine_script_id})")
                # Remove from active list to avoid double-counting
                active_pine_scripts = [ps for ps in active_pine_scripts if ps.id != pine_script_id]
        except Exception as e:
            logger.warning(f"Failed to load priority Pine Script {pine_script_id}: {e}")

    sentiment_cache: Dict[str, Optional[Dict[str, Any]]] = {}
    sentiment_refresh_state = {"did_refresh": False}
    refresh_if_stale = bool(getattr(settings, "SENTIMENT_AUTO_REFRESH_ON_READ", True))
    max_news_age_hours = max(1, int(getattr(settings, "SENTIMENT_MAX_AGE_HOURS", 2) or 2))

    for symbol in pairs:
        try:
            # 1. Multi-TF TA (using configured timeframe as primary)
            ta_data = await analyze_multi_timeframe(symbol, primary_timeframe=effective_tf)
            if "error" in ta_data:
                results.append({"symbol": symbol, "error": ta_data["error"]})
                errors += 1
                continue

            # 2. Get sentiment
            base_coin = symbol.split("/")[0]
            sentiment = await _get_sentiment_with_refresh(
                db,
                base_coin,
                sentiment_cache,
                sentiment_refresh_state,
                refresh_if_stale=refresh_if_stale,
                max_age_hours=max_news_age_hours,
            )
            btc_sentiment = sentiment if base_coin == "BTC" else await _get_sentiment_with_refresh(
                db,
                "BTC",
                sentiment_cache,
                sentiment_refresh_state,
                refresh_if_stale=refresh_if_stale,
                max_age_hours=max_news_age_hours,
            )

            # 2.5. Evaluate active strategies for this symbol
            strategy_scores = []
            for strat in active_strategies:
                try:
                    strat_pairs = json.loads(strat.pairs) if strat.pairs else []
                    if symbol not in strat_pairs:
                        continue
                    strat_indicators = json.loads(strat.indicators) if strat.indicators else []
                    if not strat_indicators:
                        continue
                    # Use the strategy evaluation logic inline
                    from app.signals.technical import (
                        ohlcv_to_dataframe as s_ohlcv_df,
                        rsi as s_rsi, macd as s_macd, bollinger_bands as s_bb,
                        ema as s_ema, stochastic_rsi as s_stoch_rsi,
                        adx as s_adx, sma as s_sma, buy_sell_volume as s_bsv,
                    )
                    strat_tf = strat.timeframe or "1h"
                    connector = exchange_manager.get_exchange(EXCHANGE)
                    if not connector:
                        continue
                    strat_ohlcv = await connector.get_ohlcv(symbol=symbol, timeframe=strat_tf, limit=200)
                    if len(strat_ohlcv) < 60:
                        continue
                    sdf = s_ohlcv_df(strat_ohlcv)
                    s_score_parts = []
                    s_weight_total = 0.0
                    for ind in strat_indicators:
                        if not ind.get("enabled", True):
                            continue
                        iname = ind["name"]
                        params = ind.get("params", {})
                        w = ind.get("weight", 1.0)
                        try:
                            if iname == "rsi":
                                rv = s_rsi(sdf, params.get("period", 14)).iloc[-1]
                                ob = params.get("overbought", 70)
                                os_ = params.get("oversold", 30)
                                if not np.isnan(rv):
                                    if rv < os_: s_score_parts.append(1.0 * w)
                                    elif rv > ob: s_score_parts.append(-1.0 * w)
                                    else: s_score_parts.append(0.0)
                                    s_weight_total += w
                            elif iname == "macd":
                                md = s_macd(sdf, params.get("fast", 12), params.get("slow", 26), params.get("signal", 9))
                                h = md["histogram"].iloc[-1]
                                hp = md["histogram"].iloc[-2] if len(md["histogram"]) > 1 else 0
                                if not np.isnan(h):
                                    if h > 0 and hp <= 0: s_score_parts.append(1.0 * w)
                                    elif h < 0 and hp >= 0: s_score_parts.append(-1.0 * w)
                                    elif h > 0: s_score_parts.append(0.5 * w)
                                    elif h < 0: s_score_parts.append(-0.5 * w)
                                    else: s_score_parts.append(0.0)
                                    s_weight_total += w
                            elif iname == "bollinger":
                                bbd = s_bb(sdf, params.get("period", 20), params.get("mult", 2.0))
                                pb = bbd["pct_b"].iloc[-1]
                                if not np.isnan(pb):
                                    if pb < 0.2: s_score_parts.append(1.0 * w)
                                    elif pb > 0.8: s_score_parts.append(-1.0 * w)
                                    else: s_score_parts.append(0.0)
                                    s_weight_total += w
                            elif iname == "ema_cross":
                                ef = s_ema(sdf["close"], params.get("fast", 50)).iloc[-1]
                                es = s_ema(sdf["close"], params.get("slow", 200)).iloc[-1]
                                if not np.isnan(ef) and not np.isnan(es):
                                    s_score_parts.append((1.0 if ef > es else -1.0) * w)
                                    s_weight_total += w
                            elif iname == "stoch_rsi":
                                sv = s_stoch_rsi(sdf, params.get("period", 14)).iloc[-1]
                                ob = params.get("overbought", 80)
                                os_ = params.get("oversold", 20)
                                if not np.isnan(sv):
                                    if sv < os_: s_score_parts.append(0.8 * w)
                                    elif sv > ob: s_score_parts.append(-0.8 * w)
                                    else: s_score_parts.append(0.0)
                                    s_weight_total += w
                            elif iname == "adx":
                                ad = s_adx(sdf, params.get("period", 14))
                                a = ad["adx"].iloc[-1]
                                pdi = ad["plus_di"].iloc[-1]
                                mdi = ad["minus_di"].iloc[-1]
                                if not np.isnan(a) and not np.isnan(pdi) and not np.isnan(mdi):
                                    direction = 1 if pdi > mdi else -1
                                    th = params.get("threshold", 25)
                                    s_score_parts.append((direction * 1.0 if a > th else 0.0) * w)
                                    s_weight_total += w
                            elif iname == "volume":
                                vm = s_sma(sdf["volume"], params.get("period", 20)).iloc[-1]
                                vr = sdf["volume"].iloc[-1] / vm if vm > 0 else 1
                                mult = params.get("mult", 1.5)
                                if vr > mult:
                                    br = s_bsv(sdf)["buy_ratio"].iloc[-1]
                                    if br > 0.6: s_score_parts.append(1.0 * w)
                                    elif br < 0.4: s_score_parts.append(-1.0 * w)
                                    else: s_score_parts.append(0.0)
                                else:
                                    s_score_parts.append(0.0)
                                s_weight_total += w
                        except Exception:
                            continue
                    if s_weight_total > 0:
                        ss = sum(s_score_parts) / s_weight_total
                        ss = max(-1.0, min(1.0, ss))
                        strategy_scores.append({
                            "name": strat.name,
                            "score": ss,
                            "confidence": min(1.0, abs(ss) / 0.5),
                        })
                except Exception as e:
                    logger.warning(f"Strategy {strat.name} eval failed for {symbol}: {e}")

            # 2.55. Evaluate PRIORITY Pine Script (selected in settings — gets 50% weight)
            priority_score = None
            if priority_pine_script:
                try:
                    from app.api.strategies import _parse_pinescript_to_indicators
                    p_indicators = _parse_pinescript_to_indicators(priority_pine_script.code or "")
                    if p_indicators:
                        connector = exchange_manager.get_exchange(EXCHANGE)
                        if connector:
                            p_ohlcv = await connector.get_ohlcv(symbol=symbol, timeframe=effective_tf, limit=200)
                            if len(p_ohlcv) >= 60:
                                from app.signals.technical import ohlcv_to_dataframe as p_odf
                                pdf = p_odf(p_ohlcv)
                                p_parts = []
                                p_wtotal = 0.0
                                for ind in p_indicators:
                                    if not ind.get("enabled", True):
                                        continue
                                    iname = ind["name"]
                                    params = ind.get("params", {})
                                    w = ind.get("weight", 1.0)
                                    try:
                                        if iname == "rsi":
                                            rv = s_rsi(pdf, params.get("period", 14)).iloc[-1]
                                            ob, os_ = params.get("overbought", 70), params.get("oversold", 30)
                                            if not np.isnan(rv):
                                                if rv < os_: p_parts.append(1.0 * w)
                                                elif rv > ob: p_parts.append(-1.0 * w)
                                                else: p_parts.append(0.0)
                                                p_wtotal += w
                                        elif iname == "macd":
                                            md = s_macd(pdf, params.get("fast", 12), params.get("slow", 26), params.get("signal", 9))
                                            h = md["histogram"].iloc[-1]
                                            hp = md["histogram"].iloc[-2] if len(md["histogram"]) > 1 else 0
                                            if not np.isnan(h):
                                                if h > 0 and hp <= 0: p_parts.append(1.0 * w)
                                                elif h < 0 and hp >= 0: p_parts.append(-1.0 * w)
                                                elif h > 0: p_parts.append(0.5 * w)
                                                elif h < 0: p_parts.append(-0.5 * w)
                                                else: p_parts.append(0.0)
                                                p_wtotal += w
                                        elif iname == "bollinger":
                                            bbd = s_bb(pdf, params.get("period", 20), params.get("mult", 2.0))
                                            pb = bbd["pct_b"].iloc[-1]
                                            if not np.isnan(pb):
                                                if pb < 0.2: p_parts.append(1.0 * w)
                                                elif pb > 0.8: p_parts.append(-1.0 * w)
                                                else: p_parts.append(0.0)
                                                p_wtotal += w
                                        elif iname == "ema_cross":
                                            ef = s_ema(pdf["close"], params.get("fast", 50)).iloc[-1]
                                            es = s_ema(pdf["close"], params.get("slow", 200)).iloc[-1]
                                            if not np.isnan(ef) and not np.isnan(es):
                                                p_parts.append((1.0 if ef > es else -1.0) * w)
                                                p_wtotal += w
                                        elif iname == "stoch_rsi":
                                            sv = s_stoch_rsi(pdf, params.get("period", 14)).iloc[-1]
                                            ob, os_ = params.get("overbought", 80), params.get("oversold", 20)
                                            if not np.isnan(sv):
                                                if sv < os_: p_parts.append(0.8 * w)
                                                elif sv > ob: p_parts.append(-0.8 * w)
                                                else: p_parts.append(0.0)
                                                p_wtotal += w
                                        elif iname == "adx":
                                            ad = s_adx(pdf, params.get("period", 14))
                                            a = ad["adx"].iloc[-1]
                                            pdi, mdi = ad["plus_di"].iloc[-1], ad["minus_di"].iloc[-1]
                                            if not np.isnan(a) and not np.isnan(pdi) and not np.isnan(mdi):
                                                direction = 1 if pdi > mdi else -1
                                                th = params.get("threshold", 25)
                                                p_parts.append((direction * 1.0 if a > th else 0.0) * w)
                                                p_wtotal += w
                                        elif iname == "volume":
                                            vm = s_sma(pdf["volume"], params.get("period", 20)).iloc[-1]
                                            vr = pdf["volume"].iloc[-1] / vm if vm > 0 else 1
                                            mult = params.get("mult", 1.5)
                                            if vr > mult:
                                                br = s_bsv(pdf)["buy_ratio"].iloc[-1]
                                                if br > 0.6: p_parts.append(1.0 * w)
                                                elif br < 0.4: p_parts.append(-1.0 * w)
                                                else: p_parts.append(0.0)
                                            else:
                                                p_parts.append(0.0)
                                            p_wtotal += w
                                    except Exception:
                                        continue
                                if p_wtotal > 0:
                                    ps_s = sum(p_parts) / p_wtotal
                                    ps_s = max(-1.0, min(1.0, ps_s))
                                    priority_score = {
                                        "name": f"[Priority Pine] {priority_pine_script.name}",
                                        "score": ps_s,
                                        "confidence": min(1.0, abs(ps_s) / 0.5),
                                    }
                                    logger.info(
                                        f"🌲 Priority Pine '{priority_pine_script.name}' → "
                                        f"{ps_s:+.3f} for {symbol} (tf={effective_tf})"
                                    )
                except Exception as e:
                    logger.warning(f"Priority Pine Script eval failed for {symbol}: {e}")

            # 2.6. Evaluate active Pine Scripts for this symbol
            for ps in active_pine_scripts:
                try:
                    import json as ps_json
                    ps_pairs = ps_json.loads(ps.pairs) if ps.pairs else []
                    if ps_pairs and symbol not in ps_pairs:
                        continue

                    # If Pine Script is linked to a strategy, check if that strategy was already evaluated
                    if ps.strategy_id:
                        already_has = any(
                            ss.get("name", "").startswith(f"[Strategy")
                            or ss.get("strategy_id") == ps.strategy_id
                            for ss in strategy_scores
                        )
                        if already_has:
                            continue

                    # Parse Pine Script code to extract indicators
                    from app.api.strategies import _parse_pinescript_to_indicators
                    ps_indicators = _parse_pinescript_to_indicators(ps.code or "")
                    if not ps_indicators:
                        continue

                    connector = exchange_manager.get_exchange(EXCHANGE)
                    if not connector:
                        continue

                    ps_ohlcv = await connector.get_ohlcv(symbol=symbol, timeframe=effective_tf, limit=200)
                    if len(ps_ohlcv) < 60:
                        continue

                    from app.signals.technical import ohlcv_to_dataframe as ps_ohlcv_df
                    psdf = ps_ohlcv_df(ps_ohlcv)

                    ps_score_parts = []
                    ps_weight_total = 0.0
                    for ind in ps_indicators:
                        if not ind.get("enabled", True):
                            continue
                        iname = ind["name"]
                        params = ind.get("params", {})
                        w = ind.get("weight", 1.0)
                        try:
                            if iname == "rsi":
                                rv = s_rsi(psdf, params.get("period", 14)).iloc[-1]
                                ob = params.get("overbought", 70)
                                os_ = params.get("oversold", 30)
                                if not np.isnan(rv):
                                    if rv < os_: ps_score_parts.append(1.0 * w)
                                    elif rv > ob: ps_score_parts.append(-1.0 * w)
                                    else: ps_score_parts.append(0.0)
                                    ps_weight_total += w
                            elif iname == "macd":
                                md = s_macd(psdf, params.get("fast", 12), params.get("slow", 26), params.get("signal", 9))
                                h = md["histogram"].iloc[-1]
                                hp = md["histogram"].iloc[-2] if len(md["histogram"]) > 1 else 0
                                if not np.isnan(h):
                                    if h > 0 and hp <= 0: ps_score_parts.append(1.0 * w)
                                    elif h < 0 and hp >= 0: ps_score_parts.append(-1.0 * w)
                                    elif h > 0: ps_score_parts.append(0.5 * w)
                                    elif h < 0: ps_score_parts.append(-0.5 * w)
                                    else: ps_score_parts.append(0.0)
                                    ps_weight_total += w
                            elif iname == "bollinger":
                                bbd = s_bb(psdf, params.get("period", 20), params.get("mult", 2.0))
                                pb = bbd["pct_b"].iloc[-1]
                                if not np.isnan(pb):
                                    if pb < 0.2: ps_score_parts.append(1.0 * w)
                                    elif pb > 0.8: ps_score_parts.append(-1.0 * w)
                                    else: ps_score_parts.append(0.0)
                                    ps_weight_total += w
                            elif iname == "ema_cross":
                                ef = s_ema(psdf["close"], params.get("fast", 50)).iloc[-1]
                                es = s_ema(psdf["close"], params.get("slow", 200)).iloc[-1]
                                if not np.isnan(ef) and not np.isnan(es):
                                    ps_score_parts.append((1.0 if ef > es else -1.0) * w)
                                    ps_weight_total += w
                            elif iname == "stoch_rsi":
                                sv = s_stoch_rsi(psdf, params.get("period", 14)).iloc[-1]
                                ob = params.get("overbought", 80)
                                os_ = params.get("oversold", 20)
                                if not np.isnan(sv):
                                    if sv < os_: ps_score_parts.append(0.8 * w)
                                    elif sv > ob: ps_score_parts.append(-0.8 * w)
                                    else: ps_score_parts.append(0.0)
                                    ps_weight_total += w
                            elif iname == "adx":
                                ad = s_adx(psdf, params.get("period", 14))
                                a = ad["adx"].iloc[-1]
                                pdi = ad["plus_di"].iloc[-1]
                                mdi = ad["minus_di"].iloc[-1]
                                if not np.isnan(a) and not np.isnan(pdi) and not np.isnan(mdi):
                                    direction = 1 if pdi > mdi else -1
                                    th = params.get("threshold", 25)
                                    ps_score_parts.append((direction * 1.0 if a > th else 0.0) * w)
                                    ps_weight_total += w
                            elif iname == "volume":
                                vm = s_sma(psdf["volume"], params.get("period", 20)).iloc[-1]
                                vr = psdf["volume"].iloc[-1] / vm if vm > 0 else 1
                                mult = params.get("mult", 1.5)
                                if vr > mult:
                                    br = s_bsv(psdf)["buy_ratio"].iloc[-1]
                                    if br > 0.6: ps_score_parts.append(1.0 * w)
                                    elif br < 0.4: ps_score_parts.append(-1.0 * w)
                                    else: ps_score_parts.append(0.0)
                                else:
                                    ps_score_parts.append(0.0)
                                ps_weight_total += w
                        except Exception:
                            continue

                    if ps_weight_total > 0:
                        ps_s = sum(ps_score_parts) / ps_weight_total
                        ps_s = max(-1.0, min(1.0, ps_s))
                        strategy_scores.append({
                            "name": f"[Pine] {ps.name}",
                            "score": ps_s,
                            "confidence": min(1.0, abs(ps_s) / 0.5),
                        })
                except Exception as e:
                    logger.warning(f"Pine Script '{ps.name}' eval failed for {symbol}: {e}")

            # 2.7. Evaluate JARVIS-generated Python strategies learned from
            # Sentiment, Telegram Signals, SMC, Insights, and the Brain Map.
            try:
                from plugins.AiMarketAnalyst.backend.services.jarvis_intelligence import evaluate_generated_strategies
                connector = exchange_manager.get_exchange(EXCHANGE)
                if connector:
                    jarvis_ohlcv = await connector.get_ohlcv(symbol=symbol, timeframe=effective_tf, limit=200)
                    jarvis_candles = _ohlcv_rows_to_candles(jarvis_ohlcv)
                    if len(jarvis_candles) >= 30:
                        jarvis_scores = await evaluate_generated_strategies(
                            db,
                            candles=jarvis_candles,
                            symbol=symbol,
                        )
                        for js in jarvis_scores:
                            strategy_scores.append({
                                "name": f"[JARVIS] {js.get('name', 'generated')}",
                                "score": float(js.get("score") or 0.0),
                                "confidence": float(js.get("confidence") or 0.0),
                                "reasoning": js.get("reasoning"),
                            })
                        if jarvis_scores:
                            logger.info(f"🧠 {len(jarvis_scores)} JARVIS generated strategies scored {symbol}")
            except Exception as e:
                logger.warning(f"JARVIS generated strategy eval failed for {symbol}: {e}")

            # 3. Combine TA + sentiment + strategies (baseline decision)
            decision = compute_final_signal(
                ta_data,
                sentiment,
                btc_sentiment,
                strategy_scores or None,
                priority_score,
            )

            # 3.5. AI Agent Signal Generation — when enabled, AI generates
            # the signal using the full agent pipeline (Market Analyst →
            # Signal Generator → Risk Manager).  The TA-only baseline from
            # step 3 is used as a fallback when AI is unavailable.
            agent_validation = None
            try:
                from app.core.config import settings as app_settings
                if app_settings.ENABLE_AI_AGENTS:
                    from app.agents.orchestrator import AgentOrchestrator
                    ai_result = await AgentOrchestrator.analyze_symbol(
                        db, symbol=symbol, timeframe=effective_tf,
                    )
                    ai_action = ai_result.get("final_action", "hold")
                    ai_confidence = ai_result.get("final_confidence", 0)
                    ai_reasoning = ai_result.get("final_reasoning", "")
                    ai_calls = ai_result.get("ai_calls", 0)

                    if ai_action in ("buy", "sell") and ai_confidence >= 0.5:
                        # AI produced an actionable signal — override baseline
                        decision["action"] = ai_action
                        decision["confidence"] = max(decision["confidence"], ai_confidence)
                        decision["reasons"].append(
                            f"AI agents generated {ai_action.upper()} "
                            f"(conf={ai_confidence:.0%}, calls={ai_calls}): "
                            f"{ai_reasoning[:120]}"
                        )
                        agent_validation = {"approved": True, "session_id": ai_result.get("session_id"), "ai_calls": ai_calls}
                        logger.info(
                            f"🤖 AI generated {ai_action.upper()} {symbol} "
                            f"(conf={ai_confidence:.0%})"
                        )
                    elif ai_action == "hold" and decision["action"] in ("buy", "sell"):
                        # AI says hold but TA suggested action
                        # Only let AI override if it has meaningful confidence
                        if ai_confidence >= 0.70:
                            decision["reasons"].append(
                                f"AI agents recommend HOLD (conf={ai_confidence:.0%}): "
                                f"{ai_reasoning[:120]}"
                            )
                            decision["action"] = "hold"
                            agent_validation = {"approved": False, "session_id": ai_result.get("session_id"), "ai_calls": ai_calls}
                            logger.info(
                                f"🤖 AI overrode {symbol} to HOLD — {ai_reasoning[:80]}"
                            )
                        else:
                            # AI hold confidence too low — keep TA signal, dampen slightly
                            decision["confidence"] *= 0.90
                            decision["reasons"].append(
                                f"AI agents lean HOLD (low conf={ai_confidence:.0%}) — TA signal kept with slight dampen"
                            )
                            agent_validation = {"approved": True, "session_id": ai_result.get("session_id"), "ai_calls": ai_calls, "dampened": True}
                            logger.info(
                                f"🤖 AI weak HOLD for {symbol} (conf={ai_confidence:.0%}) — keeping TA signal"
                            )
                    elif ai_action in ("buy", "sell") and decision["action"] == "hold":
                        # AI wants to trade but TA was neutral — trust AI
                        decision["action"] = ai_action
                        decision["confidence"] = ai_confidence
                        decision["reasons"].append(
                            f"AI agents generated {ai_action.upper()} despite neutral TA "
                            f"(conf={ai_confidence:.0%}): {ai_reasoning[:120]}"
                        )
                        agent_validation = {"approved": True, "session_id": ai_result.get("session_id"), "ai_calls": ai_calls}
                        logger.info(
                            f"🤖 AI generated {ai_action.upper()} {symbol} "
                            f"(TA was neutral, AI conf={ai_confidence:.0%})"
                        )
                    else:
                        decision["reasons"].append(
                            f"AI agents agree: HOLD (conf={ai_confidence:.0%})"
                        )
                        agent_validation = {"approved": False, "session_id": ai_result.get("session_id"), "ai_calls": ai_calls}
            except Exception as e:
                logger.warning(f"AI agent signal generation failed for {symbol} (falling back to TA): {e}")
                decision["reasons"].append(f"AI generation failed (TA fallback): {str(e)[:80]}")

            # 4. Save actionable signals (BUY/SELL only) with cooldown
            # Gate: require high confidence to limit low-quality signals
            # Read from live settings (user-configurable via Settings page)
            try:
                from app.models.database import LiveTradeSettings
                _ls_result = await db.execute(select(LiveTradeSettings).limit(1))
                _ls_row = _ls_result.scalar_one_or_none()
                MIN_SIGNAL_CONFIDENCE = getattr(_ls_row, "min_confidence", 0.90) if _ls_row else 0.90
                MIN_SIGNAL_CONFIDENCE = MIN_SIGNAL_CONFIDENCE or 0.90
            except Exception:
                MIN_SIGNAL_CONFIDENCE = 0.90
            signal_id = None
            if decision["action"] in ("buy", "sell") and decision["confidence"] < MIN_SIGNAL_CONFIDENCE:
                logger.info(
                    f"⛔ {symbol} {decision['action'].upper()} rejected: "
                    f"confidence {decision['confidence']:.0%} < {MIN_SIGNAL_CONFIDENCE:.0%}"
                )
                decision["reasons"].append(
                    f"Signal rejected: confidence {decision['confidence']:.0%} below {MIN_SIGNAL_CONFIDENCE:.0%} minimum"
                )
                decision["action"] = "hold"

            if decision["action"] in ("buy", "sell"):
                # Cooldown check: don't re-signal same pair too frequently
                now = now_sast()
                last_sig = _last_signal_time.get(symbol)
                if last_sig and (now - last_sig) < timedelta(minutes=SIGNAL_COOLDOWN_MINUTES):
                    logger.debug(f"⏳ Cooldown active for {symbol} — skipping ({SIGNAL_COOLDOWN_MINUTES}m)")
                    decision["action"] = "hold"
                    decision["reasons"].append(f"Cooldown: last signal {(now - last_sig).seconds // 60}m ago")
                else:
                    action_enum = SignalAction.BUY if decision["action"] == "buy" else SignalAction.SELL
                    sig = SignalCreate(
                        source=SignalSource.SYSTEM,
                        symbol=symbol,
                        action=action_enum,
                        price=ta_data["indicators"].get("price", 0),
                        timeframe=effective_tf,
                        strength=round(decision["strength"], 4),
                        confidence=round(decision["confidence"], 4),
                        raw_data=json.dumps({
                            "ta_score": ta_data["ta_score"],
                            "sentiment_score": decision["sentiment"]["score"],
                            "btc_sentiment_score": decision["sentiment"].get("btc_score"),
                            "btc_sentiment_label": decision["sentiment"].get("btc_label"),
                            "btc_sentiment_confirms": decision["sentiment"].get("btc_confirms"),
                            "final_score": decision["score"],
                            "timeframes": ta_data["timeframes"],
                            "volume_score": ta_data["volume_score"],
                            "volume_confirms": ta_data.get("volume_confirms", True),
                            "rsi_signal": ta_data["rsi_signal"],
                            "trend_alignment": ta_data["trend_alignment"],
                            "agreement_met": ta_data.get("agreement_met", True),
                            "adx_gate": ta_data.get("adx_gate", 1.0),
                            "order_flow_confirmed": decision.get("order_flow_confirmed"),
                            "reasons": decision["reasons"],
                            "agent_validated": agent_validation is not None and agent_validation.get("approved", False),
                            "agent_session_id": agent_validation.get("session_id") if agent_validation else None,
                        }),
                        indicators=json.dumps(ta_data["indicators"]),
                    )
                    saved = await SignalService.create_signal(db, sig)
                    signal_id = saved.id
                    signals_created += 1
                    _last_signal_time[symbol] = now

                    logger.info(
                        f"🤖 {decision['action'].upper()} {symbol} "
                        f"(score={decision['score']:+.3f} conf={decision['confidence']:.0%} "
                        f"sent={decision['sentiment']['label']} "
                        f"agree={'Y' if ta_data.get('agreement_met') else 'N'} "
                        f"adx_gate={ta_data.get('adx_gate', 1.0):.2f})"
                    )

            results.append({
                "symbol": symbol,
                "action": decision["action"],
                "score": decision["score"],
                "confidence": decision["confidence"],
                "ta_score": ta_data["ta_score"],
                "sentiment_score": decision["sentiment"]["score"],
                "sentiment_label": decision["sentiment"]["label"],
                "btc_sentiment_score": decision["sentiment"].get("btc_score", 0.0),
                "btc_sentiment_label": decision["sentiment"].get("btc_label", "no_data"),
                "order_flow_confirmed": decision.get("order_flow_confirmed"),
                "strategy_scores": strategy_scores if strategy_scores else None,
                "indicators": ta_data["indicators"],
                "timeframes": ta_data["timeframes"],
                "reasons": decision["reasons"],
                "signal_id": signal_id,
            })

        except Exception as e:
            logger.error(f"Pipeline error for {symbol}: {e}")
            results.append({"symbol": symbol, "error": str(e)})
            errors += 1

    elapsed = (now_sast() - start).total_seconds()
    logger.info(
        f"✅ [SIGNAL PIPELINE] Complete: {signals_created} signals, "
        f"{errors} errors in {elapsed:.1f}s"
    )

    return {
        "pairs_analyzed": len(pairs),
        "signals_created": signals_created,
        "errors": errors,
        "elapsed_s": round(elapsed, 1),
        "results": results,
    }
