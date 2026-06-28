"""
Autonomous Signal Generator
Generates trading signals by combining technical analysis + sentiment data.
Does NOT depend on TradingView webhooks — runs analysis internally.
"""
import json
from typing import List, Dict, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession

from app.exchanges.manager import exchange_manager, SupportedExchange
from app.signals.technical import analyze as technical_analyze
from app.signals.mtf_cascade import analyze_cascade, cascade_to_ta_score, sniper_grade
from app.sentiment.service import SentimentService
from app.signals.service import SignalService
from app.models.schemas import SignalCreate
from app.models.database import SignalSource, SignalAction, Signal
from loguru import logger


class SignalGenerator:
    """Generates signals from TA + sentiment for configured pairs."""

    EXCHANGE = SupportedExchange.BITGET  # primary exchange

    @classmethod
    async def analyze_pair(
        cls,
        symbol: str,
        timeframe: str = "1h",
        exchange: SupportedExchange = None,
    ) -> Dict[str, Any]:
        """
        Run cascade multi-timeframe analysis for a single pair.
        Returns analysis result (does NOT save to DB).
        """
        cascade = await analyze_cascade(symbol)
        ta_data = cascade_to_ta_score(cascade)
        grade = sniper_grade(cascade)
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "exchange": (exchange or cls.EXCHANGE).value,
            "cascade": cascade,
            "technical": {
                "score": ta_data["ta_score"],
                "confidence": ta_data["ta_confidence"],
                "action": cascade.get("cascade_action", "hold"),
                "indicators": ta_data["indicators"],
                "reasons": ta_data["reasons"],
            },
            "sniper": grade,
        }

    @classmethod
    async def generate_signal(
        cls,
        db: AsyncSession,
        symbol: str,
        timeframe: str = "1h",
        exchange: SupportedExchange = None,
    ) -> Dict[str, Any]:
        """
        Analyze pair, combine with sentiment, and save signal to DB.
        Returns the full analysis + created signal.
        """
        result = await cls.analyze_pair(symbol, timeframe, exchange)
        if "error" in result:
            return result

        ta = result["technical"]

        # Get sentiment for the base coin (e.g., BTC from BTC/USDT)
        base_coin = symbol.split("/")[0] if "/" in symbol else symbol.replace("USDT", "")
        sentiment = await SentimentService.get_latest_sentiment(db, base_coin)
        sentiment_score = sentiment.score if sentiment else 0.0
        sentiment_label = (
            "bullish" if sentiment_score > 0.1
            else "bearish" if sentiment_score < -0.1
            else "neutral"
        ) if sentiment else "no_data"

        # Combine TA score with sentiment
        combined_score = ta["score"]
        reasons = list(ta["reasons"])

        if sentiment:
            # Sentiment alignment boosts confidence
            if (ta["score"] > 0 and sentiment_score > 0) or (ta["score"] < 0 and sentiment_score < 0):
                boost = abs(sentiment_score) * 0.2
                combined_score = max(-1, min(1, combined_score + (boost if combined_score > 0 else -boost)))
                reasons.append(f"Sentiment aligns ({sentiment_label}, {sentiment_score:+.2f}) +{boost:.2f}")
            elif abs(sentiment_score) > 0.2:
                penalty = abs(sentiment_score) * 0.15
                combined_score = max(-1, min(1, combined_score - (penalty if combined_score > 0 else -penalty)))
                reasons.append(f"Sentiment conflicts ({sentiment_label}, {sentiment_score:+.2f}) -{penalty:.2f}")
            else:
                reasons.append(f"Sentiment neutral ({sentiment_score:+.2f})")
        else:
            reasons.append("No sentiment data available")

        # Determine final action — use cascade when available, else fall back to score
        cascade_action = result.get("cascade", {}).get("cascade_action", "")
        cascade_state = result.get("cascade", {}).get("cascade_state", "")
        _cascade_ok_states = {"buy", "sell", "partial_buy", "partial_sell"}
        if cascade_action == "buy" and cascade_state in _cascade_ok_states:
            action = SignalAction.BUY
        elif cascade_action == "sell" and cascade_state in _cascade_ok_states:
            action = SignalAction.SELL
        elif combined_score >= 0.40:
            action = SignalAction.BUY
        elif combined_score <= -0.40:
            action = SignalAction.SELL
        else:
            action = SignalAction.HOLD

        confidence = min(1.0, abs(combined_score) / 0.5)
        strength = (combined_score + 1) / 2

        # Save signal to DB
        signal_data = SignalCreate(
            source=SignalSource.SYSTEM,
            symbol=symbol,
            action=action,
            price=ta["indicators"]["price"],
            timeframe=timeframe,
            strength=round(strength, 4),
            confidence=round(confidence, 4),
            raw_data=json.dumps({
                "ta_score": ta["score"],
                "sentiment_score": sentiment_score,
                "combined_score": combined_score,
                "reasons": reasons,
            }),
            indicators=json.dumps(ta["indicators"]),
        )

        signal = await SignalService.create_signal(db, signal_data)
        logger.info(
            f"🤖 Generated signal: {action.value.upper()} {symbol} "
            f"(score={combined_score:+.2f}, conf={confidence:.0%})"
        )

        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "exchange": (exchange or cls.EXCHANGE).value,
            "action": action.value,
            "score": round(combined_score, 4),
            "confidence": round(confidence, 4),
            "strength": round(strength, 4),
            "reasons": reasons,
            "indicators": ta["indicators"],
            "sentiment": {
                "score": sentiment_score,
                "label": sentiment_label,
                "has_data": sentiment is not None,
            },
            "signal_id": signal.id,
        }

    @classmethod
    async def generate_signals_batch(
        cls,
        db: AsyncSession,
        symbols: List[str],
        timeframe: str = "1h",
        exchange: SupportedExchange = None,
    ) -> Dict[str, Any]:
        """
        Generate signals for multiple pairs.
        Returns list of results.
        """
        results = []
        generated = 0
        errors = 0

        for symbol in symbols:
            try:
                result = await cls.generate_signal(db, symbol, timeframe, exchange)
                results.append(result)
                if "error" not in result:
                    generated += 1
                else:
                    errors += 1
            except Exception as e:
                logger.error(f"Signal generation failed for {symbol}: {e}")
                results.append({"symbol": symbol, "error": str(e)})
                errors += 1

        return {
            "total": len(symbols),
            "generated": generated,
            "errors": errors,
            "timeframe": timeframe,
            "results": results,
        }
