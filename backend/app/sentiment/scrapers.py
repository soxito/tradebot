"""
News Scrapers for Various Crypto Sources
"""
import aiohttp
import feedparser
from typing import List, Dict, Optional
from datetime import datetime, timedelta
from loguru import logger
from app.core.timezone import now_sast


class NewsArticle:
    """News article data class"""
    def __init__(
        self,
        title: str,
        content: str,
        source: str,
        url: str,
        published_at: Optional[datetime] = None,
        symbols: Optional[List[str]] = None,
    ):
        self.title = title
        self.content = content
        self.source = source
        self.url = url
        self.published_at = published_at or now_sast()
        self.symbols = symbols or []
    
    def to_dict(self) -> Dict:
        return {
            "title": self.title,
            "content": self.content,
            "source": self.source,
            "url": self.url,
            "published_at": self.published_at.isoformat(),
            "symbols": self.symbols,
        }


class RSSNewsScraper:
    """Scraper for RSS-based crypto news sources"""
    
    RSS_FEEDS = {
        "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "cointelegraph": "https://cointelegraph.com/rss",
        "theblock": "https://www.theblock.co/rss.xml",
        "decrypt": "https://decrypt.co/feed",
        "bitcoinmagazine": "https://bitcoinmagazine.com/.rss/full/",
    }
    
    @staticmethod
    async def fetch_feed(feed_url: str, source_name: str) -> List[NewsArticle]:
        """
        Fetch and parse RSS feed
        
        Args:
            feed_url: RSS feed URL
            source_name: Name of the news source
        
        Returns:
            List of news articles
        """
        try:
            feed = feedparser.parse(feed_url)
            articles = []
            
            for entry in feed.entries[:20]:  # Latest 20 articles
                # Extract crypto symbols from title/summary
                text = f"{entry.get('title', '')} {entry.get('summary', '')}"
                symbols = RSSNewsScraper._extract_symbols(text)
                
                article = NewsArticle(
                    title=entry.get("title", ""),
                    content=entry.get("summary", ""),
                    source=source_name,
                    url=entry.get("link", ""),
                    published_at=datetime(*entry.get("published_parsed", None)[:6]) if entry.get("published_parsed") else None,
                    symbols=symbols,
                )
                articles.append(article)
            
            logger.info(f"📰 Fetched {len(articles)} articles from {source_name}")
            return articles
        
        except Exception as e:
            logger.error(f"Error fetching RSS feed from {source_name}: {e}")
            return []
    
    @staticmethod
    def _extract_symbols(text: str) -> List[str]:
        """Extract cryptocurrency symbols from text"""
        symbols = []
        crypto_mentions = {
            "bitcoin": "BTC",
            "btc": "BTC",
            "ethereum": "ETH",
            "eth": "ETH",
            "solana": "SOL",
            "sol": "SOL",
            "cardano": "ADA",
            "ada": "ADA",
            "ripple": "XRP",
            "xrp": "XRP",
            "polkadot": "DOT",
            "dot": "DOT",
            "dogecoin": "DOGE",
            "doge": "DOGE",
            "avalanche": "AVAX",
            "avax": "AVAX",
        }
        
        text_lower = text.lower()
        for mention, symbol in crypto_mentions.items():
            if mention in text_lower and symbol not in symbols:
                symbols.append(symbol)
        
        return symbols
    
    @staticmethod
    async def fetch_all_feeds() -> List[NewsArticle]:
        """Fetch articles from all RSS feeds"""
        all_articles = []
        
        for source, url in RSSNewsScraper.RSS_FEEDS.items():
            articles = await RSSNewsScraper.fetch_feed(url, source)
            all_articles.extend(articles)
        
        logger.info(f"📚 Total articles fetched: {len(all_articles)}")
        return all_articles


class CryptoAPINewsScraper:
    """Scraper for crypto-specific API sources"""
    
    @staticmethod
    async def fetch_coingecko_news() -> List[NewsArticle]:
        """Fetch news from CoinGecko (requires API key for full access)"""
        # Basic implementation - would need API key for full functionality
        logger.info("CoinGecko news scraper placeholder")
        return []
    
    @staticmethod
    async def fetch_cryptopanic_news(api_key: str) -> List[NewsArticle]:
        """
        Fetch news from CryptoPanic API
        
        Args:
            api_key: CryptoPanic API key
        
        Returns:
            List of news articles
        """
        if not api_key:
            logger.warning("CryptoPanic API key not provided")
            return []
        
        try:
            url = f"https://cryptopanic.com/api/v1/posts/?auth_token={api_key}&public=true"
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        articles = []
                        
                        for post in data.get("results", [])[:20]:
                            # Determine symbols from currencies
                            symbols = [
                                curr["code"]
                                for curr in post.get("currencies", [])
                            ]
                            
                            article = NewsArticle(
                                title=post.get("title", ""),
                                content=post.get("title", ""),  # CryptoPanic doesn't provide full content
                                source="cryptopanic",
                                url=post.get("url", ""),
                                published_at=datetime.fromisoformat(post.get("published_at", "").replace("Z", "+00:00")),
                                symbols=symbols,
                            )
                            articles.append(article)
                        
                        logger.info(f"📰 Fetched {len(articles)} articles from CryptoPanic")
                        return articles
                    else:
                        logger.error(f"CryptoPanic API error: {response.status}")
                        return []
        
        except Exception as e:
            logger.error(f"Error fetching from CryptoPanic: {e}")
            return []


class NewsScraper:
    """Unified news scraper for all sources"""
    
    @staticmethod
    async def fetch_all_news(
        cryptopanic_api_key: Optional[str] = None
    ) -> List[NewsArticle]:
        """
        Fetch news from all available sources
        
        Args:
            cryptopanic_api_key: Optional CryptoPanic API key
        
        Returns:
            Combined list of news articles
        """
        all_articles = []
        
        # Fetch RSS feeds
        rss_articles = await RSSNewsScraper.fetch_all_feeds()
        all_articles.extend(rss_articles)
        
        # Fetch from CryptoPanic if API key provided
        if cryptopanic_api_key:
            cryptopanic_articles = await CryptoAPINewsScraper.fetch_cryptopanic_news(
                cryptopanic_api_key
            )
            all_articles.extend(cryptopanic_articles)
        
        # Remove duplicates based on URL
        unique_articles = {article.url: article for article in all_articles}.values()
        
        logger.info(f"📚 Total unique articles: {len(unique_articles)}")
        return list(unique_articles)
