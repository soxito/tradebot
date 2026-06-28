"""
Sentiment Aggregation Service
Combines multiple sentiment sources and stores results
"""
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc

from app.models.database import SentimentScore
from app.sentiment.analyzer import sentiment_analyzer
from app.sentiment.scrapers import NewsScraper
from app.core.config import settings
from app.core.timezone import now_sast
from loguru import logger


class SentimentService:
    """Service for aggregating and storing sentiment data"""
    
    @staticmethod
    async def fetch_and_analyze_news() -> Dict[str, Dict]:
        """
        Fetch news and perform sentiment analysis
        
        Returns:
            Dictionary of sentiment scores by symbol
        """
        # Fetch news from all sources
        articles = await NewsScraper.fetch_all_news(
            cryptopanic_api_key=settings.CRYPTOPANIC_API_KEY
        )
        
        if not articles:
            logger.warning("No news articles fetched")
            return {}
        
        # Group articles by symbol
        symbol_articles = {}
        for article in articles:
            for symbol in article.symbols:
                if symbol not in symbol_articles:
                    symbol_articles[symbol] = []
                symbol_articles[symbol].append(article)
        
        # Analyze sentiment for each symbol
        symbol_sentiments = {}
        
        for symbol, symbol_arts in symbol_articles.items():
            # Analyze each article
            analyses = []
            for article in symbol_arts:
                text = f"{article.title} {article.content}"
                analysis = sentiment_analyzer.analyze(text)
                analysis["source"] = article.source
                analysis["published_at"] = article.published_at
                analyses.append(analysis)
            
            # Aggregate sentiment with recency weighting
            # More recent articles get higher weights
            now = now_sast()
            weights = []
            for analysis in analyses:
                age_hours = (now - analysis["published_at"]).total_seconds() / 3600
                # Exponential decay: weight halves every 12 hours
                weight = 2 ** (-age_hours / 12)
                weights.append(weight)
            
            # Normalize weights
            total_weight = sum(weights)
            weights = [w / total_weight for w in weights]
            
            # Calculate aggregated sentiment
            aggregated = sentiment_analyzer.aggregate_sentiment(analyses, weights)
            aggregated["articles_count"] = len(analyses)
            aggregated["sources"] = list(set(a["source"] for a in analyses))
            
            symbol_sentiments[symbol] = aggregated
            
            logger.info(
                f"💰 {symbol}: sentiment={aggregated['score']:.2f}, "
                f"confidence={aggregated['confidence']:.2f}, "
                f"articles={len(analyses)}"
            )
        
        return symbol_sentiments
    
    @staticmethod
    async def save_sentiment_scores(
        db: AsyncSession,
        symbol_sentiments: Dict[str, Dict],
        valid_hours: int = 1
    ) -> List[SentimentScore]:
        """
        Save sentiment scores to database
        
        Args:
            db: Database session
            symbol_sentiments: Dictionary of sentiment scores by symbol
            valid_hours: Hours until sentiment score expires
        
        Returns:
            List of created sentiment scores
        """
        scores = []
        valid_until = now_sast() + timedelta(hours=valid_hours)
        
        for symbol, sentiment in symbol_sentiments.items():
            score = SentimentScore(
                symbol=symbol,
                score=sentiment["score"],
                magnitude=sentiment["magnitude"],
                news_score=sentiment["score"],  # For now, only news sentiment
                sources_count=sentiment.get("articles_count", 0),
                valid_until=valid_until,
                raw_data=str(sentiment),
            )
            
            db.add(score)
            scores.append(score)
        
        await db.commit()
        
        logger.info(f"💾 Saved {len(scores)} sentiment scores to database")
        return scores
    
    @staticmethod
    async def get_latest_sentiment(
        db: AsyncSession,
        symbol: str
    ) -> Optional[SentimentScore]:
        """
        Get latest valid sentiment score for a symbol
        
        Args:
            db: Database session
            symbol: Crypto symbol (e.g., BTC, ETH)
        
        Returns:
            Latest sentiment score or None
        """
        now = now_sast()
        result = await db.execute(
            select(SentimentScore)
            .where(SentimentScore.symbol == symbol)
            .where(SentimentScore.valid_until > now)
            .order_by(desc(SentimentScore.created_at))
            .limit(1)
        )
        return result.scalar_one_or_none()
    
    @staticmethod
    async def get_all_latest_sentiments(
        db: AsyncSession
    ) -> List[SentimentScore]:
        """
        Get latest valid sentiment scores for all symbols
        
        Args:
            db: Database session
        
        Returns:
            List of sentiment scores
        """
        now = now_sast()
        result = await db.execute(
            select(SentimentScore)
            .where(SentimentScore.valid_until > now)
            .order_by(desc(SentimentScore.created_at))
        )
        
        # Get unique by symbol (latest for each)
        all_scores = result.scalars().all()
        unique_scores = {}
        for score in all_scores:
            if score.symbol not in unique_scores:
                unique_scores[score.symbol] = score
        
        return list(unique_scores.values())
    
    @staticmethod
    async def update_all_sentiments(db: AsyncSession) -> Dict[str, Dict]:
        """
        Fetch news, analyze sentiment, and save to database
        
        Args:
            db: Database session
        
        Returns:
            Dictionary of sentiment scores by symbol
        """
        logger.info("🔄 Starting sentiment update...")
        
        # Fetch and analyze
        symbol_sentiments = await SentimentService.fetch_and_analyze_news()
        
        if symbol_sentiments:
            # Save to database
            await SentimentService.save_sentiment_scores(db, symbol_sentiments)
        
        logger.info("✅ Sentiment update complete")
        return symbol_sentiments
