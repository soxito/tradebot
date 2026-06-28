"""
Sentiment Analysis API Routes
"""
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
import aiohttp

from app.core.database import get_db
from app.core.config import settings
from app.sentiment.enhanced_service import EnhancedSentimentService
from app.sentiment.analyzer import sentiment_analyzer
from pydantic import BaseModel


router = APIRouter(prefix="/sentiment", tags=["sentiment"])


class TextAnalysisRequest(BaseModel):
    """Request for text sentiment analysis"""
    text: str


class SentimentResponse(BaseModel):
    """Sentiment score response"""
    symbol: str
    score: float
    magnitude: float
    confidence: Optional[float] = None
    sources_count: int
    created_at: str
    valid_until: Optional[str] = None


# ── Pipeline / Scheduler Status (must be before /{symbol}) ──

@router.get("/pipeline/status")
async def pipeline_status():
    """Get background scheduler status (sentiment every 5 min, signals every 3 min)."""
    from app.core.scheduler import get_scheduler_status
    return get_scheduler_status()


@router.post("/pipeline/run")
async def pipeline_manual_run(db: AsyncSession = Depends(get_db)):
    """Manually trigger one full pipeline cycle (sentiment + signals)."""
    from app.signals.pipeline import run_signal_pipeline

    try:
        sent_result = await EnhancedSentimentService.run_full_cycle(db)
        sig_result = await run_signal_pipeline(db)
        return {
            "status": "success",
            "sentiment": sent_result,
            "signals": sig_result,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Enhanced Sentiment ──

@router.get("/enhanced/all")
async def get_all_enhanced(db: AsyncSession = Depends(get_db)):
    """Get enhanced sentiment for all tracked symbols."""
    data = await EnhancedSentimentService.get_all_latest(db)
    return {"sentiments": data, "count": len(data)}


@router.get("/news/articles")
async def get_news_articles(
    symbol: Optional[str] = None,
    source: Optional[str] = None,
    category: Optional[str] = None,
    q: Optional[str] = None,
    hours: int = 24,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
):
    """
    Browse stored news articles from the knowledge base.
    Filters: symbol (BTC, ETH...), source (reuters, coindesk...),
    category (macro, crypto, forex, stocks, futures), q (text search), hours, limit.
    """
    articles = await EnhancedSentimentService.get_articles(
        db, symbol=symbol, source=source, category=category, search=q,
        hours=hours, limit=min(limit, 500),
    )
    return {"articles": articles, "count": len(articles)}


@router.get("/news/stats")
async def get_news_stats(db: AsyncSession = Depends(get_db)):
    """Get stats about stored news articles — total, per source, per category."""
    stats = await EnhancedSentimentService.get_article_stats(db)
    return stats


@router.get("/enhanced/{symbol}")
async def get_enhanced_sentiment(
    symbol: str,
    db: AsyncSession = Depends(get_db),
):
    """Get enhanced sentiment with source breakdown."""
    symbol = symbol.upper()
    data = await EnhancedSentimentService.get_latest(db, symbol)
    if not data:
        raise HTTPException(status_code=404, detail=f"No enhanced sentiment for {symbol}")
    return data


# ── Core Endpoints ──

@router.post("/analyze")
async def analyze_text(request: TextAnalysisRequest):
    """Analyze sentiment of provided text."""
    try:
        result = sentiment_analyzer.analyze(request.text)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/")
async def get_all_sentiments(db: AsyncSession = Depends(get_db)):
    """Get latest freshness-validated sentiment scores for all symbols."""
    sentiments = await EnhancedSentimentService.get_all_latest(db)
    return {"sentiments": sentiments, "count": len(sentiments)}


@router.post("/update")
async def update_sentiments(
    max_age_hours: int = Query(default=settings.SENTIMENT_MAX_AGE_HOURS, ge=1, le=48),
    db: AsyncSession = Depends(get_db),
):
    """
    Manually trigger enhanced sentiment update.
    Fetches latest news from 20+ sources and updates sentiment scores.
    """
    try:
        result = await EnhancedSentimentService.run_full_cycle(db, max_age_hours=max_age_hours)
        return {
            "status": "success",
            **result,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── CoinGecko Trending ──

@router.get("/trending/coins")
async def get_trending_coins():
    """Fetch trending coins from CoinGecko with market data."""
    api_key = settings.COINGECKO_API_KEY
    base = "https://api.coingecko.com/api/v3"
    headers = {"x-cg-demo-api-key": api_key} if api_key else {}

    coins: List[dict] = []
    async with aiohttp.ClientSession() as session:
        # Trending coins
        try:
            async with session.get(
                f"{base}/search/trending",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for c in data.get("coins", [])[:15]:
                        item = c.get("item", {})
                        price_data = item.get("data", {})
                        coins.append({
                            "id": item.get("id", ""),
                            "name": item.get("name", ""),
                            "symbol": item.get("symbol", "").upper(),
                            "market_cap_rank": item.get("market_cap_rank"),
                            "thumb": item.get("thumb", ""),
                            "small": item.get("small", ""),
                            "price_btc": item.get("price_btc", 0),
                            "price_usd": price_data.get("price", ""),
                            "price_change_24h": price_data.get("price_change_percentage_24h", {}).get("usd", 0),
                            "market_cap": price_data.get("market_cap", ""),
                            "total_volume": price_data.get("total_volume", ""),
                            "sparkline": price_data.get("sparkline", ""),
                            "score": c.get("score", 0),  # trending rank (0 = #1)
                        })
                else:
                    raise HTTPException(status_code=resp.status, detail=f"CoinGecko returned {resp.status}")
        except aiohttp.ClientError as e:
            raise HTTPException(status_code=502, detail=f"CoinGecko request failed: {e}")

        # Top gainers/losers
        gainers: List[dict] = []
        losers: List[dict] = []
        try:
            async with session.get(
                f"{base}/coins/top_gainers_losers",
                headers=headers,
                params={"vs_currency": "usd", "duration": "24h"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for coin in data.get("top_gainers", [])[:10]:
                        gainers.append({
                            "id": coin.get("id", ""),
                            "name": coin.get("name", ""),
                            "symbol": coin.get("symbol", "").upper(),
                            "price_usd": coin.get("usd", 0),
                            "price_change_24h": coin.get("usd_24h_change", 0),
                            "volume_24h": coin.get("usd_24h_vol", 0),
                            "market_cap": coin.get("market_cap", 0),
                            "thumb": coin.get("image", ""),
                        })
                    for coin in data.get("top_losers", [])[:10]:
                        losers.append({
                            "id": coin.get("id", ""),
                            "name": coin.get("name", ""),
                            "symbol": coin.get("symbol", "").upper(),
                            "price_usd": coin.get("usd", 0),
                            "price_change_24h": coin.get("usd_24h_change", 0),
                            "volume_24h": coin.get("usd_24h_vol", 0),
                            "market_cap": coin.get("market_cap", 0),
                            "thumb": coin.get("image", ""),
                        })
        except Exception:
            pass  # gainers/losers may require higher plan — non-fatal

    return {
        "trending": coins,
        "top_gainers": gainers,
        "top_losers": losers,
    }


# /{symbol} MUST be last — it's a wildcard catch-all
@router.get("/{symbol}")
async def get_symbol_sentiment(
    symbol: str,
    refresh_if_stale: bool = settings.SENTIMENT_AUTO_REFRESH_ON_READ,
    db: AsyncSession = Depends(get_db),
):
    """Get latest freshness-validated sentiment score for a specific symbol."""
    symbol = symbol.upper()

    sentiment = await EnhancedSentimentService.get_latest(db, symbol)

    if not sentiment and refresh_if_stale:
        # Try one immediate refresh when data is stale or missing.
        await EnhancedSentimentService.run_full_cycle(db)
        sentiment = await EnhancedSentimentService.get_latest(db, symbol)

    if not sentiment:
        raise HTTPException(
            status_code=404,
            detail=f"No recent sentiment data for {symbol}"
        )

    return sentiment
