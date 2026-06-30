"""
Agent Paul — Market Predictor

Combines everything JARVIS knows to forecast direction for any pair:
  * Latest news sentiment (RSS) relevant to the pair
  * Live MT5 position context (if any) and recent signals
  * Learned knowledge from past chats/research

Produces a structured directional bias with confidence and rationale via the
configured AI provider, and records the prediction into long-term knowledge.
"""
from __future__ import annotations

import json
from statistics import mean
from typing import Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from plugins.AiMarketAnalyst.backend.services.ai_router import db_chat
from plugins.AgentPaulPlugin.backend.services import news_research, knowledge_base


_PREDICT_SYSTEM = """\
You are PAUL, an elite quantitative market analyst. Given news headlines with \
sentiment, learned context, and any live position data, produce a disciplined \
directional forecast for the requested instrument.

Return ONLY valid JSON with this exact shape:
{
  "pair": "<symbol>",
  "bias": "bullish" | "bearish" | "neutral",
  "confidence": <0..1>,
  "horizon": "intraday" | "swing" | "position",
  "rationale": "<=80 words, cite the strongest news/context drivers>",
  "key_drivers": ["<short>", "<short>"],
  "risks": ["<short>", "<short>"],
  "invalidation": "<what would flip this view>"
}

Rules:
- Be conservative; if signal is weak return bias "neutral" with low confidence.
- Never invent prices. Never give financial-advice disclaimers — just analysis.
- Weight recent, high-sentiment-magnitude news most heavily.
"""


async def predict_pair(db: AsyncSession, pair: str) -> dict:
    """Generate and persist a directional prediction for a pair."""
    pair = (pair or "").upper().strip()
    if not pair:
        return {"ok": False, "error": "No pair provided."}

    # 1) News + sentiment
    news = await news_research.news_for_symbol(pair, limit=8)
    if not news:
        # fall back to general market mood
        news = (await news_research.fetch_news())[:8]
    avg_sent = round(mean([n["sentiment"] for n in news]), 3) if news else 0.0

    # 2) Learned knowledge
    learned = await knowledge_base.search_knowledge(db, pair, limit=6, symbol=pair)

    # 3) Build context block
    news_lines = "\n".join(
        f"  - [{n['source']}] ({n['sentiment']:+.2f}) {n['title']}" for n in news[:8]
    ) or "  (no fresh headlines)"
    learned_lines = "\n".join(f"  - {k['content'][:160]}" for k in learned[:6]) or "  (none yet)"

    user_block = (
        f"Instrument: {pair}\n"
        f"Aggregate news sentiment: {avg_sent:+.2f} (range -1..+1)\n\n"
        f"Recent headlines:\n{news_lines}\n\n"
        f"Learned context:\n{learned_lines}\n\n"
        f"Produce the JSON forecast now."
    )

    messages = [
        {"role": "system", "content": _PREDICT_SYSTEM},
        {"role": "user", "content": user_block},
    ]

    try:
        result = await db_chat(db, messages, json_mode=True)
    except Exception as exc:
        logger.error(f"[PREDICT] db_chat error: {exc}")
        return {"ok": False, "error": str(exc), "news_sentiment": avg_sent}

    if result.get("error"):
        return {"ok": False, "error": result["error"], "news_sentiment": avg_sent}

    raw = result.get("content") or result.get("text") or ""
    forecast: dict
    try:
        forecast = json.loads(raw)
    except Exception:
        # Best-effort: derive a heuristic forecast from sentiment alone
        bias = "bullish" if avg_sent > 0.15 else "bearish" if avg_sent < -0.15 else "neutral"
        forecast = {
            "pair": pair,
            "bias": bias,
            "confidence": round(min(0.6, abs(avg_sent) + 0.2), 2),
            "horizon": "swing",
            "rationale": (raw[:200] or "Derived from aggregate news sentiment."),
            "key_drivers": [n["title"][:60] for n in news[:2]],
            "risks": ["Sentiment-only fallback — model output unparseable."],
            "invalidation": "Sharp reversal in headline sentiment.",
        }

    forecast["news_sentiment"] = avg_sent
    forecast["sources"] = [{"source": n["source"], "title": n["title"], "url": n["url"]} for n in news[:5]]
    forecast["provider"] = result.get("provider", "unknown")
    forecast["ok"] = True

    # Persist as knowledge for future grounding
    await knowledge_base.record_knowledge(
        db,
        kind="research",
        content=f"Forecast {pair}: {forecast.get('bias')} "
                f"(conf {forecast.get('confidence')}). {forecast.get('rationale', '')}",
        source="predictor",
        title=f"Forecast {pair} {forecast.get('bias')}",
        symbol=pair,
        topic="prediction",
        sentiment=avg_sent,
        importance=0.7,
    )

    return forecast


async def ingest_news_to_knowledge(db: AsyncSession, max_items: int = 40) -> int:
    """Pull fresh RSS news and store into long-term knowledge. Returns count."""
    items = await news_research.fetch_news(force=True)
    stored = 0
    for it in items[:max_items]:
        await knowledge_base.record_knowledge(
            db,
            kind="news",
            content=(it["title"] + " — " + it["summary"])[:1000],
            source=it["source"],
            title=it["title"],
            url=it["url"],
            topic=it["topic"],
            sentiment=it["sentiment"],
            published_at=it.get("published_at"),
            importance=0.4 + min(0.4, abs(it["sentiment"])),
        )
        stored += 1
    return stored
