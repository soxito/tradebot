"""
Enhanced Sentiment Service
Fetches news from 30+ financial sources, scores sentiment,
stores every article to build a knowledge base for backtesting,
and powers the signal pipeline. Designed to run every 5 minutes.
"""
import hashlib
import json
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func

from app.models.database import SentimentScore, NewsArticle
from app.sentiment.analyzer import sentiment_analyzer
from app.sentiment.news_sources import fetch_all_news, NewsItem
from app.core.config import settings
from app.core.timezone import now_sast
from loguru import logger


# ── Source Reliability Tiers ─────────────────────────────────
# Tier 1 sources get higher weight in aggregation
SOURCE_TIER: Dict[str, int] = {
    "reuters_markets": 1, "reuters_business": 1,
    "bloomberg_markets": 1, "ft_markets": 1, "wsj_markets": 1,
    "cnbc_markets": 1, "cnbc_crypto": 1,
    "financialjuice": 1,
    "barrons": 2, "marketwatch": 2, "yahoo_finance": 2, "investing_com": 2,
    "investopedia_markets": 2,
    "coindesk": 2, "theblock": 2, "blockworks": 2,
    "coinmarketcap": 2, "coingecko_trending": 2, "coingecko_movers": 2,
    "forexlive": 2, "dailyfx": 2, "fxstreet": 2,
    "seeking_alpha": 2, "benzinga": 2,
    "cointelegraph": 3, "decrypt": 3, "bitcoinmagazine": 3,
    "cryptopanic": 3, "dailyhodl": 3, "zerohedge": 3,
    "alternative_fng": 1,
    "marketaux": 1,
    "coincap_movers": 2,
    "gnews": 2,
    "currents": 2,
    "florida_man": 3,
}


def _source_weight(source: str) -> float:
    """Map source tier to weight multiplier."""
    tier = SOURCE_TIER.get(source, 3)
    return {1: 1.0, 2: 0.7, 3: 0.45}.get(tier, 0.4)


class EnhancedSentimentService:
    """Improved sentiment pipeline with multi-source financial news."""

    # Symbols we track — each gets a sentiment score
    TRACKED_SYMBOLS = [
        "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX",
        "DOT", "LINK", "NEAR", "ARB", "OP", "MATIC", "LTC",
        "PEPE", "SHIB", "FLOKI", "WIF", "BONK",
    ]

    @classmethod
    async def run_full_cycle(
        cls,
        db: AsyncSession,
        max_age_hours: Optional[int] = None,
    ) -> Dict:
        """
        Full sentiment cycle — fetch, store articles, analyze, aggregate, store scores.
        Returns summary dict.
        """
        logger.info("🔄 [SENTIMENT] Starting full cycle...")
        start = now_sast()

        effective_max_age = max_age_hours or settings.SENTIMENT_MAX_AGE_HOURS
        try:
            effective_max_age = max(1, int(effective_max_age))
        except Exception:
            effective_max_age = 2

        logger.info(f"🕒 [SENTIMENT] Using freshness window: last {effective_max_age}h")

        # 1. Fetch news from all sources
        articles = await fetch_all_news(
            cryptopanic_api_key=settings.CRYPTOPANIC_API_KEY,
            coingecko_api_key=settings.COINGECKO_API_KEY,
            coinmarketcap_api_key=settings.COINMARKETCAP_API_KEY,
            marketaux_api_key=settings.MARKETAUX_API_KEY,
            gnews_api_key=settings.GNEWS_API_KEY,
            currents_api_key=settings.CURRENTS_API_KEY,
            max_age_hours=effective_max_age,
        )

        freshness_cutoff = now_sast() - timedelta(hours=effective_max_age)
        fresh_articles = [
            a for a in articles
            if (a.published_at or now_sast()) >= freshness_cutoff
        ]

        if len(fresh_articles) != len(articles):
            logger.info(
                f"🧹 [SENTIMENT] Freshness filter kept {len(fresh_articles)}/{len(articles)} articles"
            )
        articles = fresh_articles

        if not articles:
            logger.warning("[SENTIMENT] No articles fetched")
            return {
                "articles": 0,
                "total_articles": 0,
                "symbols_scored": 0,
                "elapsed_s": 0,
                "max_age_hours": effective_max_age,
            }

        # 2. Score each article and store in DB (knowledge base)
        new_stored = await cls._store_articles(db, articles)

        # 3. Group articles by symbol
        symbol_articles = cls._group_by_symbol(articles)

        # 4. Analyze and aggregate per symbol
        symbol_sentiments: Dict[str, Dict] = {}
        for symbol, items in symbol_articles.items():
            sentiment = cls._analyze_symbol(symbol, items)
            if sentiment:
                symbol_sentiments[symbol] = sentiment

        # 5. Compare with historical sentiment for shift detection
        for symbol, current in symbol_sentiments.items():
            shift = await cls._detect_sentiment_shift(db, symbol, current["score"])
            if shift:
                current["sentiment_shift"] = shift

        # 6. Save to DB
        if symbol_sentiments:
            await cls._save_scores(db, symbol_sentiments)

        elapsed = (now_sast() - start).total_seconds()
        logger.info(
            f"✅ [SENTIMENT] Cycle complete: {len(articles)} articles → "
            f"{new_stored} new stored → {len(symbol_sentiments)} symbols scored in {elapsed:.1f}s"
        )
        return {
            "articles": len(articles),
            "total_articles": len(articles),
            "articles_stored": new_stored,
            "symbols_scored": len(symbol_sentiments),
            "max_age_hours": effective_max_age,
            "symbols": {
                sym: {
                    "score": s["score"],
                    "confidence": s["confidence"],
                    "articles": s["article_count"],
                    "label": s["label"],
                    "sentiment_shift": s.get("sentiment_shift"),
                }
                for sym, s in symbol_sentiments.items()
            },
            "elapsed_s": round(elapsed, 1),
        }

    @classmethod
    def _group_by_symbol(cls, articles: List[NewsItem]) -> Dict[str, List[NewsItem]]:
        """Group articles by symbol, including _MARKET_ and _MACRO_ tags."""
        groups: Dict[str, List[NewsItem]] = {}

        # Collect market-wide and macro articles
        market_articles = []
        macro_articles = []

        for art in articles:
            is_market = "_MARKET_" in art.symbols
            is_macro = any(s.startswith("_") for s in art.symbols)

            if is_market or is_macro:
                market_articles.append(art)
                if is_macro:
                    macro_articles.append(art)

            for sym in art.symbols:
                if sym.startswith("_"):
                    continue
                if sym not in groups:
                    groups[sym] = []
                groups[sym].append(art)

        # For each tracked symbol, include market and macro context
        for sym in cls.TRACKED_SYMBOLS:
            if sym not in groups:
                groups[sym] = []
            # Add market-wide articles when the symbol has few direct mentions
            if len(groups[sym]) < 3:
                groups[sym].extend(market_articles[:5])
            # Always add macro news for BTC/ETH (they move on macro)
            if sym in ("BTC", "ETH"):
                groups[sym].extend(macro_articles[:8])

        return groups

    @classmethod
    def _analyze_symbol(cls, symbol: str, articles: List[NewsItem]) -> Optional[Dict]:
        """Analyze sentiment for a single symbol from its articles."""
        if not articles:
            return None

        now = now_sast()
        freshness_cutoff = now - timedelta(hours=max(1, int(settings.SENTIMENT_MAX_AGE_HOURS)))
        analyses = []
        weights = []
        latest_published_at: Optional[datetime] = None

        for art in articles:
            published_at = art.published_at or now
            if published_at < freshness_cutoff:
                continue

            text = f"{art.title}. {art.summary}" if art.summary else art.title
            result = sentiment_analyzer.analyze(text)

            # Weight = source_reliability * recency_decay * source_tier_weight
            age_hours = max(0.1, (now - published_at).total_seconds() / 3600)
            recency = 2 ** (-age_hours / 4)  # half-life 4 hours (aggressive)
            source_w = _source_weight(art.source)
            reliability = art.reliability

            weight = recency * source_w * reliability
            result["source"] = art.source
            result["weight"] = weight
            analyses.append(result)
            weights.append(weight)

            if latest_published_at is None or published_at > latest_published_at:
                latest_published_at = published_at

        if not analyses:
            return None

        # Normalize weights
        total_w = sum(weights)
        if total_w == 0:
            return None
        norm_weights = [w / total_w for w in weights]

        # Weighted aggregation
        agg_score = sum(a["score"] * w for a, w in zip(analyses, norm_weights))
        agg_magnitude = sum(a["magnitude"] * w for a, w in zip(analyses, norm_weights))
        agg_confidence = sum(a.get("confidence", 0) * w for a, w in zip(analyses, norm_weights))

        # Boost confidence if many sources agree on direction
        bullish_count = sum(1 for a in analyses if a["score"] > 0.05)
        bearish_count = sum(1 for a in analyses if a["score"] < -0.05)
        total = len(analyses)
        if total >= 3:
            dominant = max(bullish_count, bearish_count) / total
            if dominant > 0.7:
                agg_confidence = min(1.0, agg_confidence * 1.3)

        label = (
            "bullish" if agg_score > 0.05
            else "bearish" if agg_score < -0.05
            else "neutral"
        )

        # Source breakdown
        source_counts = {}
        for a in analyses:
            s = a["source"]
            source_counts[s] = source_counts.get(s, 0) + 1

        return {
            "score": round(agg_score, 4),
            "magnitude": round(agg_magnitude, 4),
            "confidence": round(agg_confidence, 4),
            "label": label,
            "article_count": len(analyses),
            "latest_article_at": latest_published_at.isoformat() if latest_published_at else None,
            "source_counts": source_counts,
            "bullish_ratio": round(bullish_count / total, 2) if total else 0,
            "bearish_ratio": round(bearish_count / total, 2) if total else 0,
        }

    @classmethod
    async def _save_scores(
        cls,
        db: AsyncSession,
        symbol_sentiments: Dict[str, Dict],
        valid_minutes: Optional[int] = None,
    ):
        """Save sentiment scores to DB. Valid for 5 minutes (pipeline runs every 5)."""
        if valid_minutes is None:
            valid_minutes = settings.SENTIMENT_SCORE_VALID_MINUTES
        valid_until = now_sast() + timedelta(minutes=valid_minutes)

        for symbol, data in symbol_sentiments.items():
            score = SentimentScore(
                symbol=symbol,
                score=data["score"],
                magnitude=data["magnitude"],
                news_score=data["score"],
                sources_count=data["article_count"],
                valid_until=valid_until,
                raw_data=json.dumps({
                    "confidence": data["confidence"],
                    "label": data["label"],
                    "latest_article_at": data.get("latest_article_at"),
                    "source_counts": data["source_counts"],
                    "bullish_ratio": data["bullish_ratio"],
                    "bearish_ratio": data["bearish_ratio"],
                }),
            )
            db.add(score)

        await db.commit()
        logger.info(f"💾 Saved {len(symbol_sentiments)} sentiment scores (valid {valid_minutes}min)")

    @classmethod
    async def _store_articles(
        cls, db: AsyncSession, articles: List[NewsItem],
    ) -> int:
        """
        Persist every unique article to the news_articles table.
        Deduplicates by title hash. Returns count of newly stored articles.
        """
        if not articles:
            return 0

        # Compute hashes for all articles
        hash_map: Dict[str, NewsItem] = {}
        for art in articles:
            h = hashlib.sha256(art.title.lower().strip().encode()).hexdigest()[:32]
            hash_map[h] = art

        # Check which hashes already exist
        existing = await db.execute(
            select(NewsArticle.title_hash).where(
                NewsArticle.title_hash.in_(list(hash_map.keys()))
            )
        )
        existing_hashes = {r[0] for r in existing.fetchall()}

        new_count = 0
        for h, art in hash_map.items():
            if h in existing_hashes:
                continue

            # Analyze sentiment for this article
            text = f"{art.title}. {art.summary}" if art.summary else art.title
            sa = sentiment_analyzer.analyze(text)

            row = NewsArticle(
                title=art.title,
                summary=art.summary[:2000] if art.summary else None,
                source=art.source,
                url=art.url,
                category=art.category,
                symbols=json.dumps(art.symbols) if art.symbols else None,
                reliability=art.reliability,
                sentiment_score=sa.get("score"),
                sentiment_magnitude=sa.get("magnitude"),
                sentiment_label=sa.get("label"),
                title_hash=h,
                published_at=art.published_at,
            )
            db.add(row)
            new_count += 1

        if new_count:
            await db.commit()
            logger.info(f"📰 Stored {new_count} new articles (skipped {len(hash_map) - new_count} dupes)")

        return new_count

    @classmethod
    async def _detect_sentiment_shift(
        cls, db: AsyncSession, symbol: str, current_score: float,
    ) -> Optional[Dict]:
        """
        Compare current sentiment against recent historical average.
        Detects significant shifts that could signal trend reversals.
        """
        now = now_sast()
        cutoff_recent = now - timedelta(hours=6)
        cutoff_baseline = now - timedelta(hours=48)

        # Get recent historical sentiment scores (6-48h ago)
        result = await db.execute(
            select(SentimentScore.score).where(
                SentimentScore.symbol == symbol,
                SentimentScore.created_at >= cutoff_baseline,
                SentimentScore.created_at < cutoff_recent,
            )
        )
        historical = [r[0] for r in result.fetchall()]
        if len(historical) < 3:
            return None

        hist_avg = sum(historical) / len(historical)
        delta = current_score - hist_avg

        if abs(delta) < 0.10:
            return None

        direction = "bullish_shift" if delta > 0 else "bearish_shift"
        magnitude = "strong" if abs(delta) > 0.30 else "moderate"

        shift = {
            "direction": direction,
            "magnitude": magnitude,
            "delta": round(delta, 4),
            "historical_avg": round(hist_avg, 4),
            "current": round(current_score, 4),
            "data_points": len(historical),
        }
        logger.info(
            f"📊 {symbol} sentiment shift: {direction} ({magnitude}), "
            f"Δ{delta:+.3f} vs {len(historical)}-point avg"
        )
        return shift

    @classmethod
    async def get_articles(
        cls,
        db: AsyncSession,
        symbol: Optional[str] = None,
        source: Optional[str] = None,
        category: Optional[str] = None,
        search: Optional[str] = None,
        hours: int = 24,
        limit: int = 100,
    ) -> List[Dict]:
        """Retrieve stored articles with optional filters."""
        cutoff = now_sast() - timedelta(hours=hours)
        stmt = select(NewsArticle).where(
            NewsArticle.fetched_at >= cutoff
        ).order_by(desc(NewsArticle.published_at))

        if source:
            stmt = stmt.where(NewsArticle.source == source)
        if category:
            stmt = stmt.where(NewsArticle.category == category)

        stmt = stmt.limit(limit)
        result = await db.execute(stmt)
        rows = result.scalars().all()

        search_terms = [term.lower() for term in (search or "").split() if term.strip()]

        articles = []
        for r in rows:
            syms = []
            if r.symbols:
                try:
                    syms = json.loads(r.symbols)
                except Exception:
                    pass
            if symbol and symbol.upper() not in syms:
                continue
            if search_terms:
                haystack = (
                    f"{r.title or ''} {r.summary or ''} {r.source or ''} "
                    f"{r.category or ''} {' '.join(syms)}"
                ).lower()
                if not any(term in haystack for term in search_terms):
                    continue
            articles.append({
                "id": r.id,
                "title": r.title,
                "summary": r.summary,
                "source": r.source,
                "url": r.url,
                "category": r.category,
                "symbols": syms,
                "reliability": r.reliability,
                "sentiment_score": r.sentiment_score,
                "sentiment_label": r.sentiment_label,
                "published_at": r.published_at.isoformat() if r.published_at else None,
                "fetched_at": r.fetched_at.isoformat() if r.fetched_at else None,
            })
        return articles

    @classmethod
    async def get_article_stats(cls, db: AsyncSession) -> Dict:
        """Get stats about stored news articles."""
        now = now_sast()
        total = await db.execute(select(func.count(NewsArticle.id)))
        total_count = total.scalar() or 0

        last_24h = await db.execute(
            select(func.count(NewsArticle.id)).where(
                NewsArticle.fetched_at >= now - timedelta(hours=24)
            )
        )
        count_24h = last_24h.scalar() or 0

        last_7d = await db.execute(
            select(func.count(NewsArticle.id)).where(
                NewsArticle.fetched_at >= now - timedelta(days=7)
            )
        )
        count_7d = last_7d.scalar() or 0

        sources = await db.execute(
            select(NewsArticle.source, func.count(NewsArticle.id)).group_by(
                NewsArticle.source
            ).order_by(desc(func.count(NewsArticle.id)))
        )
        source_counts = {r[0]: r[1] for r in sources.fetchall()}

        categories = await db.execute(
            select(NewsArticle.category, func.count(NewsArticle.id)).group_by(
                NewsArticle.category
            )
        )
        cat_counts = {r[0]: r[1] for r in categories.fetchall()}

        return {
            "total_articles": total_count,
            "last_24h": count_24h,
            "last_7d": count_7d,
            "sources": source_counts,
            "categories": cat_counts,
        }

    @classmethod
    async def get_latest(cls, db: AsyncSession, symbol: str) -> Optional[Dict]:
        """Get latest valid sentiment for a symbol."""
        now = now_sast()
        result = await db.execute(
            select(SentimentScore)
            .where(SentimentScore.symbol == symbol, SentimentScore.valid_until > now)
            .order_by(desc(SentimentScore.created_at))
            .limit(1)
        )
        row = result.scalar_one_or_none()
        if not row:
            return None
        extra = {}
        if row.raw_data:
            try:
                extra = json.loads(row.raw_data)
            except Exception:
                pass
        return {
            "symbol": row.symbol,
            "score": row.score,
            "magnitude": row.magnitude,
            "sources_count": row.sources_count,
            "created_at": row.created_at.isoformat(),
            "valid_until": row.valid_until.isoformat() if row.valid_until else None,
            **extra,
        }

    @classmethod
    async def get_all_latest(cls, db: AsyncSession) -> List[Dict]:
        """Get latest valid sentiments for all symbols."""
        now = now_sast()
        result = await db.execute(
            select(SentimentScore)
            .where(SentimentScore.valid_until > now)
            .order_by(desc(SentimentScore.created_at))
        )
        all_rows = result.scalars().all()
        seen = {}
        for row in all_rows:
            if row.symbol not in seen:
                extra = {}
                if row.raw_data:
                    try:
                        extra = json.loads(row.raw_data)
                    except Exception:
                        pass
                seen[row.symbol] = {
                    "symbol": row.symbol,
                    "score": row.score,
                    "magnitude": row.magnitude,
                    "sources_count": row.sources_count,
                    "created_at": row.created_at.isoformat(),
                    "valid_until": row.valid_until.isoformat() if row.valid_until else None,
                    **extra,
                }
        return list(seen.values())
