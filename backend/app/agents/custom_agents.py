"""
Custom Rule-Based Agents — deterministic replacements for OpenAI agents.

When AI (OpenAI) is unavailable (quota exhausted, API key missing, circuit breaker open),
these agents provide the same decision pipeline using:
  1. Technical indicators (RSI, MACD, Bollinger, ATR, EMAs)
  2. Historical trade outcomes from agent_decisions (learning)
  3. News sentiment scores
  4. Volume analysis and trend detection

Each agent mirrors the role & output schema of its AI counterpart so it can
plug directly into the AgentOrchestrator pipeline.
"""
import json
from typing import Dict, Any, List, Optional
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, and_, func

from app.models.database import AgentDecision, Trade, SentimentScore


# ═══════════════════════════════════════════════════════════════
# Setting — enable/disable custom agents as fallback
# ═══════════════════════════════════════════════════════════════

_custom_agents_enabled = True


def set_custom_agents_enabled(enabled: bool):
    global _custom_agents_enabled
    _custom_agents_enabled = enabled


def are_custom_agents_enabled() -> bool:
    return _custom_agents_enabled


def get_custom_agent_status() -> dict:
    return {
        "enabled": _custom_agents_enabled,
        "type": "rule_based",
        "description": "Deterministic agents using TA + trade history learning",
    }


# ═══════════════════════════════════════════════════════════════
# Helper: Fetch learning data from past decisions
# ═══════════════════════════════════════════════════════════════

async def _get_learning_context(
    db: AsyncSession, symbol: str, role: str, limit: int = 50
) -> Dict[str, Any]:
    """
    Query past agent decisions for this symbol+role to learn from outcomes.
    Returns win_rate, best_action, avg_confidence, recent patterns.
    """
    rows = (await db.execute(
        select(AgentDecision).where(
            and_(
                AgentDecision.symbol == symbol,
                AgentDecision.agent_role == role,
                AgentDecision.outcome.isnot(None),
            )
        ).order_by(desc(AgentDecision.created_at)).limit(limit)
    )).scalars().all()

    if not rows:
        return {"has_history": False, "total": 0}

    wins = [r for r in rows if r.outcome == "win"]
    losses = [r for r in rows if r.outcome == "loss"]
    total = len(rows)
    win_rate = len(wins) / total if total else 0

    # Action stats
    action_stats: Dict[str, Dict] = {}
    for r in rows:
        act = r.action
        if act not in action_stats:
            action_stats[act] = {"wins": 0, "losses": 0, "total": 0, "pnl": 0.0}
        action_stats[act]["total"] += 1
        if r.outcome == "win":
            action_stats[act]["wins"] += 1
        elif r.outcome == "loss":
            action_stats[act]["losses"] += 1
        action_stats[act]["pnl"] += r.outcome_pnl or 0

    for act, stats in action_stats.items():
        stats["win_rate"] = stats["wins"] / stats["total"] if stats["total"] else 0

    # Best performing action
    best_action = max(action_stats.items(), key=lambda x: x[1]["win_rate"])[0] if action_stats else "hold"

    return {
        "has_history": True,
        "total": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 3),
        "action_stats": action_stats,
        "best_action": best_action,
        "total_pnl": sum(r.outcome_pnl or 0 for r in rows),
    }


async def _get_recent_trades(db: AsyncSession, symbol: str, limit: int = 10) -> List[Dict]:
    """Get recent trades for a symbol to understand position context."""
    rows = (await db.execute(
        select(Trade).where(
            Trade.symbol == symbol,
        ).order_by(desc(Trade.created_at)).limit(limit)
    )).scalars().all()
    return [
        {
            "side": t.side,
            "status": t.status,
            "pnl": t.pnl,
            "pnl_pct": t.pnl_percentage,
            "price": t.price,
        }
        for t in rows
    ]


async def _get_sentiment(db: AsyncSession, symbol: str) -> Optional[float]:
    """Get latest sentiment score for the base coin."""
    base = symbol.split("/")[0] if "/" in symbol else symbol
    row = (await db.execute(
        select(SentimentScore).where(
            SentimentScore.symbol == base
        ).order_by(desc(SentimentScore.created_at)).limit(1)
    )).scalar_one_or_none()
    return row.score if row else None


# ═══════════════════════════════════════════════════════════════
# Custom Market Analyst — pure technical analysis
# ═══════════════════════════════════════════════════════════════

async def custom_market_analyst(
    db: AsyncSession, context: Dict[str, Any], symbol: str
) -> Dict[str, Any]:
    """
    Rule-based market analyst using technical indicators.
    Mirrors the AI Market Analyst output schema.
    """
    ta = context.get("technical", {})
    if isinstance(ta, str):
        try:
            ta = json.loads(ta)
        except Exception:
            ta = {}

    indicators = ta.get("indicators", {})
    signals_data = ta.get("signals", {})

    # Extract indicators
    rsi = indicators.get("rsi_14", 50)
    macd = indicators.get("macd", 0)
    macd_signal = indicators.get("macd_signal", 0)
    macd_hist = indicators.get("macd_histogram", 0)
    bb_upper = indicators.get("bb_upper", 0)
    bb_lower = indicators.get("bb_lower", 0)
    bb_mid = indicators.get("bb_middle", 0)
    ema_9 = indicators.get("ema_9", 0)
    ema_21 = indicators.get("ema_21", 0)
    sma_50 = indicators.get("sma_50", 0)
    sma_200 = indicators.get("sma_200", 0)
    adx = indicators.get("adx", 0)
    atr = indicators.get("atr", 0)
    price = context.get("current_price", 0)

    # Learning from history
    learning = await _get_learning_context(db, symbol, "market_analyst")

    # ── Scoring system ──
    bull_score = 0
    bear_score = 0
    reasons = []

    # RSI
    if rsi < 30:
        bull_score += 2
        reasons.append(f"RSI oversold ({rsi:.1f})")
    elif rsi < 40:
        bull_score += 1
        reasons.append(f"RSI low ({rsi:.1f})")
    elif rsi > 70:
        bear_score += 2
        reasons.append(f"RSI overbought ({rsi:.1f})")
    elif rsi > 60:
        bear_score += 1
        reasons.append(f"RSI high ({rsi:.1f})")

    # MACD
    if macd > macd_signal and macd_hist > 0:
        bull_score += 2
        reasons.append("MACD bullish crossover")
    elif macd < macd_signal and macd_hist < 0:
        bear_score += 2
        reasons.append("MACD bearish crossover")

    # EMA
    if ema_9 > 0 and ema_21 > 0:
        if ema_9 > ema_21:
            bull_score += 1
            reasons.append("EMA 9 > EMA 21 (bullish)")
        else:
            bear_score += 1
            reasons.append("EMA 9 < EMA 21 (bearish)")

    # Bollinger position
    if price and bb_lower and bb_upper:
        if price < bb_lower:
            bull_score += 1
            reasons.append("Price below lower BB (oversold)")
        elif price > bb_upper:
            bear_score += 1
            reasons.append("Price above upper BB (overbought)")

    # Golden/Death cross
    if sma_50 > 0 and sma_200 > 0:
        if sma_50 > sma_200:
            bull_score += 1
            reasons.append("Golden cross (SMA50 > SMA200)")
        else:
            bear_score += 1
            reasons.append("Death cross (SMA50 < SMA200)")

    # ADX trend strength
    strength = "weak"
    if adx > 40:
        strength = "strong"
    elif adx > 25:
        strength = "moderate"

    # Determine action
    diff = bull_score - bear_score
    if diff >= 2:
        action = "bullish"
    elif diff <= -2:
        action = "bearish"
    else:
        action = "neutral"

    # Confidence based on how decisive the signals are
    total_signals = bull_score + bear_score
    confidence = min(0.95, abs(diff) / max(total_signals, 1) * 0.8 + 0.2) if total_signals > 0 else 0.3

    # Learning adjustment
    if learning.get("has_history") and learning["total"] >= 10:
        if learning["win_rate"] > 0.6:
            confidence = min(0.95, confidence + 0.05)
        elif learning["win_rate"] < 0.4:
            confidence = max(0.2, confidence - 0.10)

    # Trend direction
    trend = "sideways"
    if action == "bullish":
        trend = "uptrend"
    elif action == "bearish":
        trend = "downtrend"

    # Support/resistance from Bollinger
    support = bb_lower or (price * 0.97 if price else 0)
    resistance = bb_upper or (price * 1.03 if price else 0)

    return {
        "action": action,
        "confidence": round(confidence, 3),
        "trend": trend,
        "strength": strength,
        "key_levels": {"support": support, "resistance": resistance},
        "reasoning": f"[CUSTOM AGENT] {'; '.join(reasons[:4])}. ADX={adx:.0f} ({strength})",
        "risk_factors": [r for r in reasons if "overbought" in r or "bearish" in r][:3],
        "agent_name": "Custom Market Analyst",
        "agent_role": "market_analyst",
        "ai_called": False,
        "custom_agent": True,
    }


# ═══════════════════════════════════════════════════════════════
# Custom Sentiment Analyst — uses DB sentiment + news data
# ═══════════════════════════════════════════════════════════════

async def custom_sentiment_analyst(
    db: AsyncSession, context: Dict[str, Any], symbol: str
) -> Dict[str, Any]:
    """
    Rule-based sentiment analyst using stored sentiment scores and CMC data.
    """
    sentiment_score = await _get_sentiment(db, symbol)
    learning = await _get_learning_context(db, symbol, "sentiment_analyst")

    # Default neutral
    action = "neutral"
    confidence = 0.4
    reasons = []

    if sentiment_score is not None:
        if sentiment_score > 0.3:
            action = "bullish"
            confidence = min(0.85, 0.5 + sentiment_score * 0.4)
            reasons.append(f"Sentiment score bullish ({sentiment_score:.3f})")
        elif sentiment_score < -0.2:
            action = "bearish"
            confidence = min(0.85, 0.5 + abs(sentiment_score) * 0.4)
            reasons.append(f"Sentiment score bearish ({sentiment_score:.3f})")
        else:
            reasons.append(f"Sentiment neutral ({sentiment_score:.3f})")
    else:
        reasons.append("No sentiment data available")

    # CMC community sentiment
    try:
        from app.sentiment.cmc_community import get_cached_cmc_sentiment
        cmc = get_cached_cmc_sentiment()
        base = symbol.split("/")[0] if "/" in symbol else symbol
        if cmc and base in cmc:
            cmc_data = cmc[base]
            if cmc_data.avg_sentiment > 0.2:
                if action != "bullish":
                    confidence = max(confidence - 0.1, 0.2)
                else:
                    confidence = min(0.90, confidence + 0.05)
                reasons.append(f"CMC community bullish ({cmc_data.avg_sentiment:.3f}, {cmc_data.mention_count} mentions)")
            elif cmc_data.avg_sentiment < -0.1:
                if action != "bearish":
                    confidence = max(confidence - 0.1, 0.2)
                else:
                    confidence = min(0.90, confidence + 0.05)
                reasons.append(f"CMC community bearish ({cmc_data.avg_sentiment:.3f})")
    except Exception:
        pass

    # Learning adjustment
    if learning.get("has_history") and learning["total"] >= 10:
        if learning["win_rate"] > 0.6:
            confidence = min(0.95, confidence + 0.05)

    return {
        "action": action,
        "confidence": round(confidence, 3),
        "sentiment_score": sentiment_score or 0,
        "fear_greed": "neutral",
        "key_narratives": reasons,
        "catalyst_risk": "medium",
        "reasoning": f"[CUSTOM AGENT] {'; '.join(reasons[:3])}",
        "agent_name": "Custom Sentiment Analyst",
        "agent_role": "sentiment_analyst",
        "ai_called": False,
        "custom_agent": True,
    }


# ═══════════════════════════════════════════════════════════════
# Custom Signal Generator — combines market + sentiment analysis
# ═══════════════════════════════════════════════════════════════

async def custom_signal_generator(
    db: AsyncSession,
    context: Dict[str, Any],
    symbol: str,
    market_decision: Dict[str, Any],
    sentiment_decision: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Rule-based signal generator. Combines market analyst + sentiment
    analyst outputs to produce a buy/sell/hold signal.
    """
    learning = await _get_learning_context(db, symbol, "signal_generator")
    recent_trades = await _get_recent_trades(db, symbol)

    ta = context.get("technical", {})
    if isinstance(ta, str):
        try:
            ta = json.loads(ta)
        except Exception:
            ta = {}
    indicators = ta.get("indicators", {})

    market_action = market_decision.get("action", "neutral")
    market_conf = market_decision.get("confidence", 0.5)
    sentiment_action = sentiment_decision.get("action", "neutral")
    sentiment_conf = sentiment_decision.get("confidence", 0.5)

    price = context.get("current_price", 0)
    atr = indicators.get("atr", 0)
    rsi = indicators.get("rsi_14", 50)

    # ── Decision logic ──
    bull_signals = 0
    bear_signals = 0
    reasons = []

    # Market analyst vote
    if market_action == "bullish":
        bull_signals += 2
        reasons.append(f"Market: bullish ({market_conf:.0%})")
    elif market_action == "bearish":
        bear_signals += 2
        reasons.append(f"Market: bearish ({market_conf:.0%})")

    # Sentiment vote
    if sentiment_action == "bullish":
        bull_signals += 1
        reasons.append(f"Sentiment: bullish ({sentiment_conf:.0%})")
    elif sentiment_action == "bearish":
        bear_signals += 1
        reasons.append(f"Sentiment: bearish ({sentiment_conf:.0%})")

    # RSI extremes boost
    if rsi < 30:
        bull_signals += 1
        reasons.append(f"RSI extreme oversold ({rsi:.0f})")
    elif rsi > 70:
        bear_signals += 1
        reasons.append(f"RSI extreme overbought ({rsi:.0f})")

    # Learning — boost the historically winning action
    if learning.get("has_history") and learning["total"] >= 15:
        best = learning.get("best_action", "hold")
        wr = learning.get("action_stats", {}).get(best, {}).get("win_rate", 0)
        if wr > 0.55:
            if best == "buy":
                bull_signals += 1
                reasons.append(f"History favors buy ({wr:.0%} WR)")
            elif best == "sell":
                bear_signals += 1
                reasons.append(f"History favors sell ({wr:.0%} WR)")

    # Recent losing streak detection — reduce confidence
    recent_losses = sum(1 for t in recent_trades[:5] if t.get("pnl", 0) and t["pnl"] < 0)

    # Decision
    diff = bull_signals - bear_signals
    if diff >= 2:
        action = "buy"
    elif diff <= -2:
        action = "sell"
    else:
        action = "hold"

    # Confidence calculation
    total = bull_signals + bear_signals
    base_conf = min(0.90, abs(diff) / max(total, 1) * 0.7 + 0.3) if total > 0 else 0.3

    # Weighted with analyst confidences
    confidence = base_conf * 0.5 + market_conf * 0.3 + sentiment_conf * 0.2

    # Penalize if recent losses
    if recent_losses >= 3:
        confidence = max(0.2, confidence - 0.15)
        reasons.append(f"Recent losing streak ({recent_losses}/5)")

    # Only signal if confidence >= 0.65
    if action in ("buy", "sell") and confidence < 0.65:
        action = "hold"
        reasons.append(f"Confidence too low ({confidence:.0%}) — holding")

    # Entry levels
    sl_pct = max(2.0, min(5.0, (atr / price * 100) * 1.5)) if price and atr else 3.0
    tp_pct = sl_pct * 2.0  # 1:2 risk/reward

    return {
        "action": action,
        "confidence": round(min(0.95, confidence), 3),
        "entry_price": price,
        "stop_loss_pct": round(sl_pct, 2),
        "take_profit_pct": round(tp_pct, 2),
        "timeframe": context.get("timeframe", "1h"),
        "reasoning": f"[CUSTOM AGENT] {'; '.join(reasons[:4])}",
        "conditions": reasons,
        "agent_name": "Custom Signal Generator",
        "agent_role": "signal_generator",
        "ai_called": False,
        "custom_agent": True,
    }


# ═══════════════════════════════════════════════════════════════
# Custom Risk Manager — position sizing and risk checks
# ═══════════════════════════════════════════════════════════════

async def custom_risk_manager(
    db: AsyncSession,
    context: Dict[str, Any],
    symbol: str,
    proposed_trade: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Rule-based risk manager. Checks position limits, exposure,
    drawdown, and recent trade patterns to approve/reject trades.
    """
    learning = await _get_learning_context(db, symbol, "risk_manager")
    positions = context.get("positions", {})

    open_positions = positions.get("open_positions", 0)
    max_positions = positions.get("max_positions", 3)
    available_balance = positions.get("available_balance", 0)
    total_exposure = positions.get("total_exposure", 0)
    max_exposure = positions.get("max_exposure", 5000)
    is_dca = positions.get("is_dca", False)

    trade_action = proposed_trade.get("action", "hold")
    trade_conf = proposed_trade.get("confidence", 0.5)

    reasons = []
    warnings = []
    action = "approve"
    risk_score = 0.3

    # ── Hard rejections ──

    # Max positions
    if not is_dca and open_positions >= max_positions:
        action = "reject"
        reasons.append(f"Max positions reached ({open_positions}/{max_positions})")
        risk_score = 0.9

    # Exposure limit
    if total_exposure >= max_exposure * 0.9:
        action = "reject"
        reasons.append(f"Exposure near max ({total_exposure:.0f}/{max_exposure:.0f} USDT)")
        risk_score = 0.85

    # Low balance
    if available_balance < 5:
        action = "reject"
        reasons.append(f"Insufficient balance ({available_balance:.2f} USDT)")
        risk_score = 0.95

    # Low confidence
    if trade_conf < 0.65:
        action = "reject"
        reasons.append(f"Signal confidence too low ({trade_conf:.0%})")
        risk_score = 0.7

    # ── Soft checks (warnings) ──

    # Recent losing streak
    recent_trades = await _get_recent_trades(db, symbol)
    recent_losses = sum(1 for t in recent_trades[:5] if t.get("pnl", 0) and t["pnl"] < 0)
    if recent_losses >= 3:
        warnings.append(f"Recent losing streak ({recent_losses}/5 losses)")
        risk_score = min(1.0, risk_score + 0.15)
        if recent_losses >= 4:
            action = "reject"
            reasons.append("Severe losing streak — 4+ consecutive losses")

    # Learning: if historically this action loses more than wins
    if learning.get("has_history") and learning["total"] >= 10:
        act_stats = learning.get("action_stats", {}).get(trade_action, {})
        act_wr = act_stats.get("win_rate", 0.5)
        if act_wr < 0.35 and act_stats.get("total", 0) >= 5:
            action = "reject"
            reasons.append(f"History shows {trade_action} has {act_wr:.0%} win rate for {symbol}")
            risk_score = min(1.0, risk_score + 0.2)

    # ── Position sizing ──
    position_size_pct = 2.0  # conservative default
    if trade_conf > 0.80:
        position_size_pct = 3.0
    if learning.get("win_rate", 0) > 0.6:
        position_size_pct = min(5.0, position_size_pct + 1.0)

    confidence = 0.75 if action == "approve" else 0.85  # higher confidence when rejecting

    return {
        "action": action,
        "confidence": round(confidence, 3),
        "position_size_pct": position_size_pct,
        "max_leverage": 10,
        "risk_score": round(risk_score, 3),
        "warnings": warnings,
        "reasoning": f"[CUSTOM AGENT] {'; '.join(reasons + warnings) or 'All checks passed'}",
        "agent_name": "Custom Risk Manager",
        "agent_role": "risk_manager",
        "ai_called": False,
        "custom_agent": True,
    }


# ═══════════════════════════════════════════════════════════════
# Custom Trade Executor — order type and timing
# ═══════════════════════════════════════════════════════════════

async def custom_trade_executor(
    db: AsyncSession,
    context: Dict[str, Any],
    symbol: str,
    proposed_trade: Dict[str, Any],
    risk_review: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Rule-based trade executor. Determines order type, limit price, and timing.
    """
    price = context.get("current_price", 0)
    ta = context.get("technical", {})
    if isinstance(ta, str):
        try:
            ta = json.loads(ta)
        except Exception:
            ta = {}
    indicators = ta.get("indicators", {})

    ticker = context.get("ticker", {})
    bid = ticker.get("bid", price)
    ask = ticker.get("ask", price)
    spread_pct = ((ask - bid) / bid * 100) if bid and ask and bid > 0 else 0

    atr = indicators.get("atr", 0)
    trade_action = proposed_trade.get("action", "hold")
    sl_pct = proposed_trade.get("stop_loss_pct", 3.0)
    tp_pct = proposed_trade.get("take_profit_pct", 6.0)

    # Order type decision
    if spread_pct > 0.15:
        order_type = "limit"
        # Place limit at a better price
        if trade_action == "buy":
            limit_price = round(bid * 1.001, 8)  # slightly above bid
        else:
            limit_price = round(ask * 0.999, 8)  # slightly below ask
    else:
        order_type = "market"
        limit_price = None

    # Stop-loss and take-profit
    if trade_action == "buy":
        stop_loss = round(price * (1 - sl_pct / 100), 8) if price else None
        take_profit = round(price * (1 + tp_pct / 100), 8) if price else None
    else:
        stop_loss = round(price * (1 + sl_pct / 100), 8) if price else None
        take_profit = round(price * (1 - tp_pct / 100), 8) if price else None

    return {
        "action": "execute",
        "confidence": 0.80,
        "order_type": order_type,
        "limit_price": limit_price,
        "size": risk_review.get("position_size_pct", 2.0),
        "leverage": min(risk_review.get("max_leverage", 10), 10),
        "margin_mode": "crossed",
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "timing": "immediate",
        "reasoning": f"[CUSTOM AGENT] {order_type.upper()} order, "
                     f"SL={sl_pct:.1f}% TP={tp_pct:.1f}%, spread={spread_pct:.3f}%",
        "agent_name": "Custom Trade Executor",
        "agent_role": "trade_executor",
        "ai_called": False,
        "custom_agent": True,
    }


# ═══════════════════════════════════════════════════════════════
# Custom Position Reviewer — checks open positions for reversals
# ═══════════════════════════════════════════════════════════════

async def custom_position_reviewer(
    db: AsyncSession,
    context: Dict[str, Any],
    symbol: str,
    position: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Rule-based position reviewer. Checks for reversal signals
    in open positions using TA indicators.
    """
    ta = context.get("technical", {})
    if isinstance(ta, str):
        try:
            ta = json.loads(ta)
        except Exception:
            ta = {}
    indicators = ta.get("indicators", {})

    rsi = indicators.get("rsi_14", 50)
    macd = indicators.get("macd", 0)
    macd_signal = indicators.get("macd_signal", 0)
    price = context.get("current_price", 0)
    ema_9 = indicators.get("ema_9", 0)
    ema_21 = indicators.get("ema_21", 0)

    hold_side = position.get("holdSide", "long")
    entry_price = float(position.get("openPriceAvg", 0) or 0)
    unrealized_pnl = float(position.get("unrealizedPL", 0) or 0)
    roe_pct = float(position.get("achievedProfits", 0) or 0)

    reversal_signals = 0
    reasons = []
    action = "hold"

    if hold_side == "long":
        # Look for bearish reversal
        if rsi > 75:
            reversal_signals += 1
            reasons.append(f"RSI overbought ({rsi:.0f})")
        if macd < macd_signal:
            reversal_signals += 1
            reasons.append("MACD bearish crossover")
        if ema_9 and ema_21 and ema_9 < ema_21:
            reversal_signals += 1
            reasons.append("EMA death cross (9 < 21)")
        if entry_price and price and price < entry_price * 0.97:
            reversal_signals += 1
            reasons.append(f"Price -3% from entry")
    else:  # short
        # Look for bullish reversal
        if rsi < 25:
            reversal_signals += 1
            reasons.append(f"RSI oversold ({rsi:.0f})")
        if macd > macd_signal:
            reversal_signals += 1
            reasons.append("MACD bullish crossover")
        if ema_9 and ema_21 and ema_9 > ema_21:
            reversal_signals += 1
            reasons.append("EMA golden cross (9 > 21)")
        if entry_price and price and price > entry_price * 1.03:
            reversal_signals += 1
            reasons.append(f"Price +3% from entry (against short)")

    if reversal_signals >= 3:
        action = "close"
        urgency = "high"
    elif reversal_signals >= 2:
        action = "adjust"
        urgency = "medium"
    else:
        urgency = "low"

    return {
        "action": action,
        "confidence": min(0.90, 0.4 + reversal_signals * 0.15),
        "urgency": urgency,
        "reversal_signals": reversal_signals,
        "reasoning": f"[CUSTOM AGENT] {'; '.join(reasons) or 'No reversal signals'}",
        "agent_name": "Custom Position Reviewer",
        "agent_role": "position_reviewer",
        "ai_called": False,
        "custom_agent": True,
    }


# ═══════════════════════════════════════════════════════════════
# Full pipeline — mirrors AgentOrchestrator.validate_trade
# ═══════════════════════════════════════════════════════════════

async def custom_validate_trade(
    db: AsyncSession,
    symbol: str,
    signal: Dict[str, Any],
    position_context: Dict[str, Any],
    context: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Full custom agent pipeline replacing AgentOrchestrator.validate_trade.
    Same output format: {approved, reasoning, decisions, session_id, order_params, confidence}
    """
    import uuid
    session_id = f"custom-{str(uuid.uuid4())[:8]}"

    logger.info(f"[Custom Agents:{session_id}] Validating trade for {symbol}")

    decisions = []

    # Phase 1: Market + Sentiment (parallel-equivalent)
    market = await custom_market_analyst(db, context, symbol)
    sentiment = await custom_sentiment_analyst(db, context, symbol)
    decisions.extend([market, sentiment])

    # Phase 2: Signal Generator — combines market + sentiment
    signal_dec = await custom_signal_generator(db, context, symbol, market, sentiment)
    decisions.append(signal_dec)

    # Phase 3: Risk Manager — validate the custom signal generator output
    # Merge the original pipeline signal with our generator's output for richer context
    merged_signal = {
        **signal,
        "action": signal_dec.get("action", signal.get("action", "hold")),
        "confidence": signal_dec.get("confidence", signal.get("confidence", 0)),
        "stop_loss_pct": signal_dec.get("stop_loss_pct", 3.0),
        "take_profit_pct": signal_dec.get("take_profit_pct", 6.0),
    }
    risk_context = {**context, "positions": position_context}
    risk = await custom_risk_manager(db, risk_context, symbol, merged_signal)
    decisions.append(risk)

    # Phase 4: Trade Executor (if approved) — receives generator's SL/TP via merged_signal
    order_params = None
    if risk.get("action") in ("approve", "modify"):
        executor = await custom_trade_executor(db, context, symbol, merged_signal, risk)
        decisions.append(executor)
        order_params = executor

    # Store decisions
    for dec in decisions:
        db.add(AgentDecision(
            agent_id=0,  # custom agent, no DB agent row
            agent_name=dec.get("agent_name", "Custom Agent"),
            agent_role=dec.get("agent_role", "custom"),
            symbol=symbol,
            action=dec.get("action", "hold"),
            confidence=dec.get("confidence", 0),
            reasoning=dec.get("reasoning", ""),
            market_data=json.dumps(dec, default=str),
            session_id=session_id,
            ai_called=False,
            memory_context_used=0,
        ))
    await db.commit()

    # Determine approval
    approved = True
    reasoning_parts = []

    if risk.get("action") == "reject":
        approved = False
        reasoning_parts.append(f"Risk Manager: {risk.get('reasoning', 'rejected')[:120]}")

    if order_params and order_params.get("action") in ("cancel", "wait"):
        approved = False
        reasoning_parts.append(f"Executor: {order_params.get('reasoning', 'cancelled')[:120]}")

    # If signal generator said hold, don't approve
    if signal_dec.get("action") == "hold":
        approved = False
        reasoning_parts.append(f"Signal Generator: hold ({signal_dec.get('reasoning', '')[:80]})")

    logger.info(
        f"[Custom Agents:{session_id}] Result: {'APPROVED' if approved else 'REJECTED'} "
        f"for {symbol}: {' | '.join(reasoning_parts) or 'All checks passed'}"
    )

    return {
        "approved": approved,
        "session_id": session_id,
        "confidence": signal_dec.get("confidence", 0),
        "reasoning": " | ".join(reasoning_parts) if reasoning_parts else "Custom agents approved trade",
        "decisions": decisions,
        "order_params": order_params,
        "custom_agents": True,
    }
