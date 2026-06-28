"""Push Telegram NEWS messages into the core sentiment system.

NEWS-kind channel messages are scored with the core VADER/TextBlob analyzer and
written as core ``SentimentScore`` rows (tagged ``telegram_news``) so the
existing auto-trade decision engine factors channel news into its decisions.

Imports of core services are read-only integration points — removing this plugin
leaves core untouched (no telegram sentiment rows get written).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from plugins.TelegramSignalNewsPlugin.backend.models import (
    SourceKind,
    TelegramIngestMessage,
    TelegramNewsSentiment,
)
from plugins.TelegramSignalNewsPlugin.backend.timezone_utils import now_utc_naive


_QUOTE_SUFFIXES = ("USDT", "USDC", "USD", "BTC", "ETH", "EUR", "GBP", "PERP")

# Common non-crypto headline words that the greedy symbol regex picks up.
_STOPWORDS = {
    "US", "USA", "UK", "EU", "WAR", "PEACE", "DEAL", "MARKET", "MARKETS", "NEWS",
    "FED", "SEC", "CPI", "ETF", "GDP", "IRAN", "TRUMP", "BIDEN", "CEO", "AI",
    "BREAKING", "ALERT", "LIVE", "NOW", "TODAY", "BUY", "SELL", "LONG", "SHORT",
    "TP", "SL", "VIP", "PUMP", "DUMP", "BULL", "BEAR", "ATH", "FOMO", "DYOR",
    "TRILLIONAIRE", "BILLIONAIRE", "MILLION", "BILLION", "TRILLION", "PRICE",
    "BTCUSD", "DEFI", "NFT", "DAO", "P2P", "KYC", "USDT", "USD",
}

# Cache of valid tradeable base symbols (refreshed periodically)
_TRADEABLE_CACHE: set[str] = set()
_TRADEABLE_CACHE_AT: datetime | None = None
_TRADEABLE_TTL = timedelta(minutes=30)


def _base_symbol(sym: str) -> str:
    s = (sym or "").upper().strip().lstrip("#").replace("/", "")
    for suf in _QUOTE_SUFFIXES:
        if s.endswith(suf) and len(s) > len(suf):
            return s[: -len(suf)]
    return s


async def _get_tradeable_bases() -> set[str]:
    """Valid Bitget base symbols, cached. Empty set disables filtering."""
    global _TRADEABLE_CACHE, _TRADEABLE_CACHE_AT
    now = now_utc_naive()
    if _TRADEABLE_CACHE and _TRADEABLE_CACHE_AT and (now - _TRADEABLE_CACHE_AT) < _TRADEABLE_TTL:
        return _TRADEABLE_CACHE
    try:
        from app.signals.pump_detector import _get_exchange_tradeable_symbols

        bases = await _get_exchange_tradeable_symbols()
        if bases:
            _TRADEABLE_CACHE = bases
            _TRADEABLE_CACHE_AT = now
    except Exception as exc:  # noqa: BLE001
        logger.debug("Tradeable symbol fetch failed: {}", exc)
    return _TRADEABLE_CACHE


async def process_news_to_sentiment(db: AsyncSession, limit: int = 100) -> dict[str, Any]:
    """Score unprocessed NEWS messages and write core sentiment rows."""
    # Already-processed message ids
    done_res = await db.execute(select(TelegramNewsSentiment.ingest_message_id))
    done_ids = {row[0] for row in done_res.all()}

    news_res = await db.execute(
        select(TelegramIngestMessage)
        .where(TelegramIngestMessage.source_kind == SourceKind.NEWS)
        .order_by(desc(TelegramIngestMessage.created_at))
        .limit(limit)
    )
    messages = [m for m in news_res.scalars().all() if m.id not in done_ids]
    if not messages:
        return {"processed": 0, "sentiment_rows": 0}

    # Core sentiment analyzer + model (read-only integration)
    try:
        from app.sentiment.analyzer import SentimentAnalyzer
        from app.models.database import SentimentScore
    except Exception as exc:  # noqa: BLE001
        logger.warning("Core sentiment unavailable for Telegram news: {}", exc)
        return {"processed": 0, "sentiment_rows": 0, "error": str(exc)}

    analyzer = SentimentAnalyzer()
    valid_until = now_utc_naive() + timedelta(hours=2)
    tradeable = await _get_tradeable_bases()

    processed = 0
    rows_written = 0
    for msg in messages:
        text = (msg.raw_text or "").strip()
        if not text:
            db.add(_mark_processed(msg, [], None, None, "neutral", 0))
            processed += 1
            continue

        analysis = analyzer.analyze(text)
        score = float(analysis.get("score", 0.0))
        magnitude = float(analysis.get("magnitude", abs(score)))
        label = str(analysis.get("label", "neutral"))

        # Symbols from the message extraction (base form), de-duplicated and
        # filtered to REAL tradeable coins so news headline words (WAR, US, …)
        # never pollute the sentiment table.
        raw_syms = msg.symbols_json or []
        bases = []
        seen: set[str] = set()
        for s in raw_syms:
            b = _base_symbol(s)
            if not b or b in seen or not (1 <= len(b) <= 12):
                continue
            if b in _STOPWORDS:
                continue  # obvious headline word, never a tradeable coin
            if tradeable and b not in tradeable:
                continue  # not a tradeable coin — skip noise
            seen.add(b)
            bases.append(b)

        written_here = 0
        for base in bases:
            db.add(
                SentimentScore(
                    symbol=base,
                    score=score,
                    magnitude=magnitude,
                    news_score=score,
                    social_score=score,  # Telegram acts as a social/news blend
                    sources_count=1,
                    valid_until=valid_until,
                    raw_data=f"telegram_news:{msg.channel_source_id}:{label}",
                )
            )
            written_here += 1

        db.add(_mark_processed(msg, bases, score, magnitude, label, written_here))
        processed += 1
        rows_written += written_here

    await db.commit()
    return {"processed": processed, "sentiment_rows": rows_written}


def _mark_processed(msg, symbols, score, magnitude, label, pushed) -> TelegramNewsSentiment:
    return TelegramNewsSentiment(
        ingest_message_id=msg.id,
        channel_source_id=msg.channel_source_id,
        symbols_json=symbols,
        score=score,
        magnitude=magnitude,
        label=label,
        pushed_symbols=pushed,
    )
