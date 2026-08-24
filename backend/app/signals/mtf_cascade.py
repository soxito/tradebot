"""
Multi-Timeframe Cascade Analyzer (Top-Down Approach)

The trading philosophy encoded here:

  ┌──────────────────────────────────────────────────────────┐
  │  4H  →  TREND DIRECTION  (macro bias gate)               │
  │  1H  →  TRADE SETUP      (aligned retracement/pattern)  │
  │  15M →  ENTRY TRIGGER    (RSI/MACD/StochRSI confirmation)│
  │  5M  →  ENTRY TIMING     (final candle-level precision)  │
  └──────────────────────────────────────────────────────────┘

  Rules:
  1. Start at the TOP (4H).  The 4H bias is the only direction
     we trade.  If 4H is bearish, all buy signals are blocked.
  2. Walk DOWN the TF ladder.  Each TF must agree with the
     bias before we step lower.
  3. Only pull the trigger when ALL four TFs cascade in the
     same direction — with the exact entry timed on the 5M.
  4. When TFs conflict → WAIT.  Missing a trade is always
     cheaper than a counter-trend loss.

Cascade states (returned in `cascade_state`):
  "no_data"         — exchange/TA fetch failed
  "conflict"        — higher and lower TFs oppose → HOLD
  "neutral"         — no clear direction at any TF → HOLD
  "bias_set"        — 4H has direction but 1H not aligned → WAIT
  "setup_forming"   — 4H + 1H aligned, 15M not confirmed → WAIT
  "entry_pending"   — 4H + 1H + 15M aligned, 5M not triggered → WAIT (imminent)
  "buy"             — full cascade confirmed → BUY now
  "sell"            — full cascade confirmed → SELL now
  "partial_buy"     — 3 of 4 TFs cascade buy (5M lagging) → BUY (lower conf)
  "partial_sell"    — 3 of 4 TFs cascade sell (5M lagging) → SELL (lower conf)
"""
from __future__ import annotations

import asyncio
from typing import Dict, Any, List, Optional, Tuple

import numpy as np
from loguru import logger

from app.exchanges.manager import exchange_manager, SupportedExchange
from app.signals.candle_source import get_ohlcv as cached_get_ohlcv
from app.signals.technical import (
    analyze as technical_analyze,
    ohlcv_to_dataframe,
    rsi as calc_rsi,
    macd as calc_macd,
    stochastic_rsi as calc_stoch_rsi,
    adx as calc_adx,
    atr as calc_atr,
    ema,
)

# ─── Constants ────────────────────────────────────────────────────────────────

EXCHANGE = SupportedExchange.BITGET

# Ordered from LOWEST to HIGHEST — entry precision first, trend last
ENTRY_TF = "5m"
CONFIRM_TF = "15m"
SETUP_TF = "1h"
TREND_TF = "4h"

CASCADE_TFS: List[str] = [TREND_TF, SETUP_TF, CONFIRM_TF, ENTRY_TF]

# Candles to fetch per TF
TF_LIMITS: Dict[str, int] = {
    "1m": 300, "3m": 300, "5m": 300, "15m": 300,
    "30m": 200, "1h": 200, "2h": 200, "4h": 200,
    "6h": 150, "12h": 150, "1d": 150,
}

# Confidence by number of aligned TFs
CASCADE_CONFIDENCE: Dict[int, float] = {4: 0.90, 3: 0.72, 2: 0.45, 1: 0.20, 0: 0.05}

# Score thresholds for classifying a single TF result
BULL_THRESHOLD = 0.12   # score above this → bullish TF
BEAR_THRESHOLD = -0.12  # score below this → bearish TF


# ─── Per-TF role analysis ─────────────────────────────────────────────────────

def _tf_direction(ta: Dict[str, Any]) -> str:
    """Return 'bull', 'bear', or 'neutral' for a single TF TA result."""
    score = ta.get("score", 0.0)
    if score >= BULL_THRESHOLD:
        return "bull"
    if score <= BEAR_THRESHOLD:
        return "bear"
    return "neutral"


def _trend_analysis(ta: Dict[str, Any]) -> Dict[str, Any]:
    """
    4H role: determine the macro bias.

    Strong signal requires:
    - ADX > 20 (trending market)
    - EMA relationship (price above/below key EMAs)
    - Clear score direction
    """
    direction = _tf_direction(ta)
    indicators = ta.get("indicators", {})
    adx_val = indicators.get("adx", 0.0) or 0.0
    score = ta.get("score", 0.0)

    # Trend strength qualifier
    trending = adx_val >= 20
    strong = adx_val >= 25

    # Absolute score magnitude → bias strength
    if abs(score) >= 0.4 and strong:
        strength = "strong"
    elif abs(score) >= 0.2 and trending:
        strength = "moderate"
    else:
        strength = "weak"

    # Weak trend with neutral score → no actionable bias
    if direction == "neutral" or (strength == "weak" and adx_val < 18):
        direction = "neutral"

    return {
        "role": "trend",
        "tf": TREND_TF,
        "direction": direction,
        "strength": strength,
        "adx": round(adx_val, 1),
        "score": round(score, 4),
        "rsi": indicators.get("rsi"),
        "reason": (
            f"4H {direction.upper()} | ADX={adx_val:.0f} ({strength}) | "
            f"score={score:+.3f}"
        ),
    }


def _setup_analysis(ta: Dict[str, Any], bias: str) -> Dict[str, Any]:
    """
    1H role: identify if a trade setup is forming and aligned with the 4H bias.

    A valid setup requires:
    - 1H direction matches bias (or 1H is pulling back but reversing toward bias)
    - RSI not at extremes against the trade direction
    - Price forming a recognizable entry area (near MA or key level)
    """
    direction = _tf_direction(ta)
    indicators = ta.get("indicators", {})
    score = ta.get("score", 0.0)
    rsi_val = indicators.get("rsi", 50.0) or 50.0
    adx_val = indicators.get("adx", 20.0) or 20.0

    # Alignment check: 1H must match 4H bias, OR be in a constructive pullback
    # (neutral 1H during a 4H bull trend is acceptable — it's a retracement)
    if bias == "bull":
        aligned = direction in ("bull", "neutral") and score > -0.25
        pullback = direction == "neutral" and rsi_val < 55
    elif bias == "bear":
        aligned = direction in ("bear", "neutral") and score < 0.25
        pullback = direction == "neutral" and rsi_val > 45
    else:
        # neutral 4H → accept any 1H direction, lower confidence
        aligned = direction != "neutral"
        pullback = False

    # RSI sanity: don't enter longs when 1H is overbought, shorts when oversold
    rsi_blocks = (bias == "bull" and rsi_val > 75) or (bias == "bear" and rsi_val < 25)

    effective_aligned = aligned and not rsi_blocks
    stage = "aligned" if effective_aligned else ("pullback" if pullback else "opposing")

    return {
        "role": "setup",
        "tf": SETUP_TF,
        "direction": direction,
        "aligned": effective_aligned,
        "stage": stage,
        "score": round(score, 4),
        "rsi": round(rsi_val, 1),
        "adx": round(adx_val, 1),
        "rsi_blocks": rsi_blocks,
        "reason": (
            f"1H {direction.upper()} | aligned={effective_aligned} | "
            f"RSI={rsi_val:.0f} | score={score:+.3f}"
        ),
    }


def _confirm_analysis(ta: Dict[str, Any], bias: str) -> Dict[str, Any]:
    """
    15M role: confirm the entry trigger is ready.

    Looks for momentum indicators crossing in the bias direction:
    - RSI emerging from oversold (<40 for bull) or overbought (>60 for bear)
    - MACD histogram turning positive (bull) or negative (bear)
    - StochRSI turning from extremes
    """
    direction = _tf_direction(ta)
    indicators = ta.get("indicators", {})
    score = ta.get("score", 0.0)
    rsi_val = indicators.get("rsi", 50.0) or 50.0
    macd_hist = indicators.get("macd_histogram", 0.0) or 0.0
    stoch_rsi = indicators.get("stoch_rsi", 50.0) or 50.0

    # RSI trigger: coming from extreme zone toward neutral
    if bias == "bull":
        rsi_trigger = rsi_val < 55  # fresh bullish momentum (was oversold, recovering)
        rsi_confirmed = rsi_val < 65 and rsi_val > 30  # in valid buy zone
        macd_confirms = macd_hist > 0
        stoch_confirms = stoch_rsi < 70  # not yet overbought on entry TF
    elif bias == "bear":
        rsi_trigger = rsi_val > 45
        rsi_confirmed = rsi_val > 35 and rsi_val < 70
        macd_confirms = macd_hist < 0
        stoch_confirms = stoch_rsi > 30
    else:
        rsi_trigger = abs(rsi_val - 50) < 15
        rsi_confirmed = True
        macd_confirms = True
        stoch_confirms = True

    # Count confirming signals
    confirms = sum([
        direction in ("bull" if bias == "bull" else "bear", "neutral"),
        rsi_trigger,
        macd_confirms,
        stoch_confirms,
    ])

    triggered = confirms >= 3  # require 3 of 4 sub-signals — prevents low-quality entries

    return {
        "role": "confirm",
        "tf": CONFIRM_TF,
        "direction": direction,
        "triggered": triggered,
        "confirms": confirms,
        "score": round(score, 4),
        "rsi": round(rsi_val, 1),
        "stoch_rsi": round(stoch_rsi, 1),
        "macd_hist": round(macd_hist, 6),
        "reason": (
            f"15M {direction.upper()} | triggered={triggered} ({confirms}/4) | "
            f"RSI={rsi_val:.0f} | StochRSI={stoch_rsi:.0f} | MACD_hist={macd_hist:+.5f}"
        ),
    }


def _entry_analysis(ta: Dict[str, Any], bias: str) -> Dict[str, Any]:
    """
    5M role: time the precise entry.

    Final gate — checks for:
    - Direction aligned with bias
    - Volume confirming (buy volume > average on bull, sell volume on bear)
    - Momentum not exhausted (RSI not at extreme against trade)
    - Candle structure (close > open for bull, close < open for bear)
    """
    direction = _tf_direction(ta)
    indicators = ta.get("indicators", {})
    score = ta.get("score", 0.0)
    rsi_val = indicators.get("rsi", 50.0) or 50.0
    vol_ratio = indicators.get("volume_ratio", 1.0) or 1.0
    buy_ratio = indicators.get("buy_ratio", 0.5) or 0.5

    # Volume trigger
    if bias == "bull":
        vol_trigger = vol_ratio > 1.3 and buy_ratio > 0.55
        rsi_ok = rsi_val < 70  # not yet exhausted
        direction_ok = direction in ("bull",)
    elif bias == "bear":
        vol_trigger = vol_ratio > 1.3 and buy_ratio < 0.45
        rsi_ok = rsi_val > 30
        direction_ok = direction in ("bear",)
    else:
        vol_trigger = vol_ratio > 1.2
        rsi_ok = True
        direction_ok = direction != "neutral"

    # A neutral 5M with vol trigger is acceptable for a partial cascade
    if not direction_ok and vol_trigger and bias != "neutral":
        direction_ok = True  # volume is giving the leading signal

    # Full fire requires direction + RSI ok + volume confirmation
    # (volume trigger required for full-cascade precision entry)
    fired = direction_ok and rsi_ok and vol_trigger

    return {
        "role": "entry",
        "tf": ENTRY_TF,
        "direction": direction,
        "fired": fired,
        "vol_trigger": vol_trigger,
        "score": round(score, 4),
        "rsi": round(rsi_val, 1),
        "vol_ratio": round(vol_ratio, 2),
        "buy_ratio": round(buy_ratio, 2),
        "reason": (
            f"5M {direction.upper()} | fired={fired} | "
            f"vol_ratio={vol_ratio:.1f}x | buy_ratio={buy_ratio:.0%} | "
            f"RSI={rsi_val:.0f}"
        ),
    }


# ─── Cascade Decision ─────────────────────────────────────────────────────────

def _build_cascade_decision(
    trend: Dict, setup: Dict, confirm: Dict, entry: Dict
) -> Tuple[str, float, str]:
    """
    Walk down the TF ladder and return (cascade_state, confidence, action).

    Returns:
      cascade_state: one of the documented states above
      confidence:    0.0 – 1.0
      action:        "buy" | "sell" | "hold" | "wait"
    """
    bias = trend["direction"]

    # ── No bias at all ──────────────────────────────────────
    if bias == "neutral":
        return "neutral", CASCADE_CONFIDENCE[0], "hold"

    # ── 4H has a direction — walk down ──────────────────────
    if not setup["aligned"]:
        # 1H opposes or is unrelated — not the right time
        return "conflict", CASCADE_CONFIDENCE[1], "hold"

    if not confirm["triggered"]:
        # 4H + 1H aligned but 15M not ready — setup is forming
        return "setup_forming", CASCADE_CONFIDENCE[2], "wait"

    if not entry["fired"]:
        # 4H + 1H + 15M aligned but 5M hasn't triggered — imminent
        # Publish a partial signal so the frontend can show "PENDING"
        action = bias  # "bull" → "buy" equivalent
        state = "partial_buy" if bias == "bull" else "partial_sell"
        return state, CASCADE_CONFIDENCE[3], ("buy" if bias == "bull" else "sell")

    # ── Full cascade ─────────────────────────────────────────
    full_state = "buy" if bias == "bull" else "sell"
    action = "buy" if bias == "bull" else "sell"
    conf = CASCADE_CONFIDENCE[4]

    # Downgrade confidence if trend is weak or setup was neutral
    if trend["strength"] == "weak":
        conf -= 0.15
    elif trend["strength"] == "moderate" and not entry["vol_trigger"]:
        conf -= 0.05

    return full_state, max(0.1, min(1.0, conf)), action


# ─── Main Entry Point ─────────────────────────────────────────────────────────

async def analyze_cascade(
    symbol: str,
    custom_tfs: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Run the full top-down 4-TF cascade for a symbol.

    Args:
        symbol:     Trading pair, e.g. "BTC/USDT"
        custom_tfs: Override the default [4h, 1h, 15m, 5m] ladder.
                    Must be exactly 4 TFs ordered from highest to lowest.

    Returns a rich dict with:
      cascade_state, cascade_action, cascade_confidence,
      bias (4H direction), per-TF role summaries, per-TF raw TA,
      trade parameters (atr-based SL/TP suggestions), reasons[]
    """
    connector = exchange_manager.get_exchange(EXCHANGE)
    if not connector:
        return {
            "symbol": symbol, "cascade_state": "no_data",
            "cascade_action": "hold", "cascade_confidence": 0.0,
            "error": "Exchange not initialized",
        }

    # Determine TF ladder
    tfs = custom_tfs if (custom_tfs and len(custom_tfs) == 4) else CASCADE_TFS
    trend_tf, setup_tf, confirm_tf, entry_tf = tfs

    # ── Fetch OHLCV + run TA for all 4 TFs in parallel ──────
    # The fetches share one cache, so a pair the pipeline already touched
    # costs zero network calls; the TA runs are CPU-bound and cheap.
    tf_raw: Dict[str, Any] = {}
    errors: List[str] = []

    async def _one_tf(tf: str) -> None:
        limit = TF_LIMITS.get(tf, 200)
        try:
            ohlcv = await cached_get_ohlcv(symbol=symbol, timeframe=tf, limit=limit)
            ta = technical_analyze(ohlcv, tf)
            if "error" in ta:
                errors.append(f"{tf}: {ta['error']}")
            else:
                tf_raw[tf] = ta
        except Exception as exc:
            errors.append(f"{tf}: {exc}")
            logger.debug(f"[MTF Cascade] {symbol} {tf} fetch failed: {exc}")

    await asyncio.gather(*(_one_tf(tf) for tf in tfs))

    if len(tf_raw) < 2:
        return {
            "symbol": symbol, "cascade_state": "no_data",
            "cascade_action": "hold", "cascade_confidence": 0.0,
            "error": f"Insufficient TF data: {errors}",
        }

    # ── Per-TF role analysis ─────────────────────────────────
    trend_ta  = tf_raw.get(trend_tf,   {})
    setup_ta  = tf_raw.get(setup_tf,   {})
    confirm_ta = tf_raw.get(confirm_tf, {})
    entry_ta   = tf_raw.get(entry_tf,   {})

    trend_analysis  = _trend_analysis(trend_ta)
    bias = trend_analysis["direction"]

    setup_analysis  = _setup_analysis(setup_ta,   bias)
    confirm_analysis = _confirm_analysis(confirm_ta, bias)
    entry_analysis  = _entry_analysis(entry_ta,   bias)

    # ── Cascade decision ─────────────────────────────────────
    state, confidence, action = _build_cascade_decision(
        trend_analysis, setup_analysis, confirm_analysis, entry_analysis
    )

    # ── Trade parameters from entry TF's ATR ─────────────────
    trade_params: Dict[str, Any] = {}
    if entry_ta:
        entry_indicators = entry_ta.get("indicators", {})
        atr_val = entry_indicators.get("atr")
        last_price = entry_indicators.get("close")
        if atr_val and last_price and last_price > 0:
            # SL = 1.5× ATR below entry for longs / above for shorts
            # TP = 3× ATR in direction (2:1 reward:risk minimum)
            sl_pct = round((atr_val * 1.5 / last_price) * 100, 3)
            tp_pct = round((atr_val * 3.0 / last_price) * 100, 3)
            trade_params = {
                "atr": round(atr_val, 6),
                "last_price": round(last_price, 6),
                "suggested_sl_pct": sl_pct,
                "suggested_tp_pct": tp_pct,
                "risk_reward": "2:1 (ATR-based)",
            }

    # ── Assemble reasoning ───────────────────────────────────
    reasons: List[str] = [
        f"[{trend_tf}  TREND]   {trend_analysis['reason']}",
        f"[{setup_tf}   SETUP]   {setup_analysis['reason']}",
        f"[{confirm_tf}  CONFIRM] {confirm_analysis['reason']}",
        f"[{entry_tf}   ENTRY]   {entry_analysis['reason']}",
    ]
    if errors:
        reasons.append(f"Fetch errors: {'; '.join(errors)}")

    # ── State-specific summary line ──────────────────────────
    state_summary = {
        "neutral":      "No clear trend direction at any timeframe — stand aside.",
        "conflict":     f"4H says {bias.upper()} but 1H is opposing — wait for alignment.",
        "bias_set":     f"4H {bias.upper()} trend established, but 1H setup not ready yet.",
        "setup_forming":f"4H+1H aligned ({bias.upper()}), waiting for 15M confirmation.",
        "entry_pending":f"4H+1H+15M cascade ready ({bias.upper()}), 5M entry not yet triggered.",
        "partial_buy":  "3/4 TFs cascade BUY — 5M lagging. Acceptable to enter at lower size.",
        "partial_sell": "3/4 TFs cascade SELL — 5M lagging. Acceptable to enter at lower size.",
        "buy":          "FULL CASCADE BUY — all 4 timeframes aligned. High confidence entry.",
        "sell":         "FULL CASCADE SELL — all 4 timeframes aligned. High confidence entry.",
        "no_data":      "Insufficient data to form a cascade decision.",
    }
    reasons.insert(0, state_summary.get(state, state))

    return {
        "symbol": symbol,
        "cascade_state": state,
        "cascade_action": action,           # "buy" | "sell" | "hold" | "wait"
        "cascade_confidence": round(confidence, 3),
        "bias": bias,                       # 4H macro direction
        "timeframes": {
            trend_tf:  {"role": "trend",   **_tf_snapshot(trend_ta,   trend_analysis)},
            setup_tf:  {"role": "setup",   **_tf_snapshot(setup_ta,   setup_analysis)},
            confirm_tf:{"role": "confirm", **_tf_snapshot(confirm_ta, confirm_analysis)},
            entry_tf:  {"role": "entry",   **_tf_snapshot(entry_ta,   entry_analysis)},
        },
        "trade_params": trade_params,
        "reasons": reasons,
        "tf_errors": errors,
    }


def _tf_snapshot(ta: Dict, role_info: Dict) -> Dict:
    """Compact per-TF summary for the final output dict."""
    ind = ta.get("indicators", {}) if ta else {}
    return {
        "direction": role_info.get("direction", "neutral"),
        "score": role_info.get("score", 0.0),
        "rsi": role_info.get("rsi") or ind.get("rsi"),
        "adx": role_info.get("adx") or ind.get("adx"),
        "action": ta.get("action", "hold") if ta else "hold",
        # volume fields — needed by sniper_grade
        "vol_ratio": ind.get("volume_ratio"),
        "buy_ratio": ind.get("buy_ratio"),
    }


# ─── Quick cascade score for pipeline integration ─────────────────────────────

# ─── Sniper Grade ───────────────────────────────────────────────────────────────────

def sniper_grade(cascade: Dict[str, Any]) -> Dict[str, Any]:
    """
    Rate a cascade result on a 0–5 scale (★ stars).

    A sniper entry is the tightest, highest-confidence trade setup:
      5★ — Full 4-TF cascade + volume spike + trend strength ADX>25
      4★ — Full cascade OR partial + volume + RSI/MACD divergence detected
      3★ — Partial cascade (3/4 TFs) + volume confirmation
      2★ — Partial cascade, no volume, or entry_pending state
      1★ — Bias + setup but no trigger yet
      0★ — Conflict, neutral, no_data

    Returns {"grade": int, "stars": str, "reasons": List[str]}
    """
    state = cascade.get("cascade_state", "no_data")
    conf = cascade.get("cascade_confidence", 0.0)
    bias = cascade.get("bias", "neutral")
    tfs = cascade.get("timeframes", {})
    trade_p = cascade.get("trade_params", {})

    points = 0
    grade_reasons: List[str] = []

    # ── TF alignment points ───────────────────────────────
    if state in ("buy", "sell"):
        points += 2
        grade_reasons.append("Full 4-TF cascade (+2)")
    elif state in ("partial_buy", "partial_sell"):
        points += 1
        grade_reasons.append("Partial 3-TF cascade (+1)")
    elif state == "entry_pending":
        points += 1
        grade_reasons.append("3-TF cascade, 5M pending (+1)")
    else:
        # No usable cascade
        return {"grade": 0, "stars": "☆☆☆☆☆", "reasons": [f"{state} — no trade"]}

    # ── Entry TF volume confirmation ─────────────────────
    entry_snap = tfs.get(ENTRY_TF, {})
    vol_ratio = entry_snap.get("vol_ratio", 1.0) or 1.0
    buy_ratio = entry_snap.get("buy_ratio", 0.5) or 0.5
    is_buy = state in ("buy", "partial_buy")
    vol_confirms = (
        (is_buy and vol_ratio >= 1.4 and buy_ratio >= 0.55) or
        (not is_buy and vol_ratio >= 1.4 and buy_ratio <= 0.45)
    )
    if vol_confirms:
        points += 1
        grade_reasons.append(f"Volume spike ({vol_ratio:.1f}x, {buy_ratio:.0%} {'buy' if is_buy else 'sell'}) (+1)")

    # ── Trend strength (ADX on 4H) ──────────────────────
    trend_snap = tfs.get(TREND_TF, {})
    adx_4h = trend_snap.get("adx") or 0.0
    if adx_4h >= 28:
        points += 1
        grade_reasons.append(f"Strong 4H trend ADX={adx_4h:.0f} (+1)")
    elif adx_4h >= 22:
        grade_reasons.append(f"Moderate 4H trend ADX={adx_4h:.0f} (+0)")
    else:
        grade_reasons.append(f"Weak 4H trend ADX={adx_4h:.0f} (⚠)")

    # ── R:R from ATR-based trade params ────────────────
    sl_pct = trade_p.get("suggested_sl_pct", 999)
    if sl_pct and sl_pct <= 1.2:
        # Tight SL relative to ATR = sniper-quality risk control
        points += 1
        grade_reasons.append(f"Tight ATR SL {sl_pct:.2f}% (+1)")

    grade = min(5, points)
    filled = "★" * grade
    empty = "☆" * (5 - grade)
    return {
        "grade": grade,
        "stars": filled + empty,
        "confidence": round(conf, 3),
        "bias": bias,
        "is_sniper": grade >= 4,
        "reasons": grade_reasons,
    }


def cascade_to_ta_score(cascade: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert a cascade result into the same shape that `compute_final_signal`
    expects from `analyze_multi_timeframe`, so the cascade can be a drop-in
    replacement without rewriting the pipeline.

    Returns a dict compatible with the `ta_data` argument of `compute_final_signal`.
    """
    action = cascade.get("cascade_action", "hold")
    conf = cascade.get("cascade_confidence", 0.0)
    state = cascade.get("cascade_state", "no_data")

    # Map action → numeric score
    if action == "buy":
        score = conf
    elif action == "sell":
        score = -conf
    else:
        # wait / hold — preserve direction lean from bias
        bias = cascade.get("bias", "neutral")
        score = conf * 0.2 if bias == "bull" else (-conf * 0.2 if bias == "bear" else 0.0)

    # agreement_met only if we have at least a partial cascade
    agreement_met = state in ("buy", "sell", "partial_buy", "partial_sell", "entry_pending")

    # Pick indicators from entry TF (most current)
    tfs = cascade.get("timeframes", {})
    entry_snap = tfs.get("5m") or tfs.get("15m") or {}
    indicators = {
        "rsi": entry_snap.get("rsi"),
        "adx": entry_snap.get("adx"),
    }

    return {
        "ta_score": round(score, 4),
        "ta_confidence": round(conf, 4),
        "indicators": indicators,
        "timeframes": {
            tf: {
                "score": info.get("score", 0.0),
                "action": info.get("action", "hold"),
                "rsi": info.get("rsi"),
                "adx": info.get("adx"),
            }
            for tf, info in tfs.items()
        },
        "volume_score": 0.0,
        "volume_confirms": agreement_met,
        "rsi_signal": 0.0,
        "trend_alignment": score * 0.15,
        "agreement_met": agreement_met,
        "adx_gate": 1.0,
        "reasons": cascade.get("reasons", []),
        # extra cascade fields passed through
        "cascade_state": state,
        "cascade_action": action,
        "cascade_confidence": conf,
        "bias": cascade.get("bias", "neutral"),
        "trade_params": cascade.get("trade_params", {}),
    }
