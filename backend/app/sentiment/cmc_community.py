"""
CoinMarketCap News & Community Sentiment Scraper

Scrapes headlines/news from CMC (server-side rendered JSON articles),
CMC community topic pages, and trending data to extract coin mentions
and sentiment scores that feed into the pump detector's social indicator.

Strategy:
- CMC /headlines/news/ page contains SSR JSON with article title + subtitle
  (rich summaries perfect for VADER sentiment analysis)
- CMC /community/topic/ pages have SSR title + description fields
- Community profile pages are fully JS-rendered (Gravity API) and cannot
  be scraped without a headless browser — we skip them.

The scraper runs on every pump-monitor cycle (120s) but caches results for
CMC_CACHE_TTL seconds to avoid excessive requests.
"""
import json
import re
import time
import aiohttp
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from loguru import logger

try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

    _vader = SentimentIntensityAnalyzer()
    # Crypto-specific lexicon boosts
    _vader.lexicon.update({
        "bullish": 3.0, "bearish": -3.0, "pump": 2.5, "dump": -2.5,
        "breakout": 2.5, "accumulating": 2.5, "accumulation": 2.0,
        "whales": 1.5, "whale": 1.0, "rally": 2.5, "surge": 3.0,
        "crash": -3.5, "plunge": -3.0, "scam": -4.0, "rug": -4.0,
        "momentum": 2.0, "support": 1.0, "resistance": -0.5,
        "rekt": -3.5, "fud": -2.5, "fomo": 1.5, "moon": 3.5,
        "exploded": 2.5, "pumping": 3.0, "dumping": -3.0,
        "correction": -1.5, "recovering": 1.5, "bounce": 1.5,
        "ath": 2.5, "buying": 2.0, "selling": -1.5,
    })
except ImportError:
    _vader = None
    logger.warning("[CMC] VADER not available — falling back to keyword sentiment")


# ── Configuration ───────────────────────────────────────────

# CMC news pages to scrape (SSR-rendered JSON articles)
CMC_NEWS_PAGES: List[str] = [
    "https://coinmarketcap.com/headlines/news/",
    "https://coinmarketcap.com/headlines/",
]

# Trending topics to scrape (slug as it appears in the URL)
CMC_TOPICS: List[str] = [
    "Saylor-Buys-Another-Billion-in-Bitcoin",
]

# Scrape settings
CMC_CACHE_TTL = 300          # Cache results for 5 minutes
CMC_REQUEST_TIMEOUT = 20     # Per-request timeout in seconds
CMC_MAX_ARTICLES = 30        # Max articles to process per news page

# Regex for extracting coin ticker mentions from post text
# Matches $BTC, $ETH, etc. plus standalone known tickers
_TICKER_RE = re.compile(
    r'\$([A-Z]{2,10})\b',
    re.IGNORECASE,
)

# JSON article object extraction from SSR HTML
# Matches {"title":"...","subtitle":"...",...} objects embedded in the page
_ARTICLE_RE = re.compile(
    r'"title"\s*:\s*"([^"]{10,300}?)"\s*,\s*"subtitle"\s*:\s*"([^"]*?)"'
    r'(?:\s*,\s*"sourceName"\s*:\s*"([^"]*?)")?',
)

# Bullish / bearish keyword lists for simple fallback scoring
_BULLISH_KW = frozenset({
    "bullish", "buying", "accumulating", "breakout", "rally", "surge",
    "pump", "moon", "ath", "support", "bounce", "recovery", "upside",
    "targets", "gains", "explosive", "buy", "long",
    "whales are accumulating", "momentum shifting bullish",
    "positive correlation", "upward trend", "inflows", "climbs",
    "strengthens", "jumps", "soars",
})
_BEARISH_KW = frozenset({
    "bearish", "dump", "crash", "plunge", "correction", "sell",
    "weakness", "rejection", "drop", "losing momentum", "breakdown",
    "short", "rug", "scam", "risk", "catastrophic", "reckoning",
    "deleveraging", "forced sell", "concern", "collapse", "crashes",
    "falls", "sinks", "slumps",
})


@dataclass
class CmcPost:
    """Parsed community post."""
    author: str
    text: str
    symbols: List[str]
    sentiment: float          # -1 to +1
    engagement: int           # upvotes or interactions estimate
    source_type: str          # "kol" or "topic"


@dataclass
class SymbolSentiment:
    """Aggregated sentiment for a single coin symbol."""
    symbol: str
    mention_count: int = 0
    total_sentiment: float = 0.0
    weighted_sentiment: float = 0.0
    total_weight: float = 0.0
    bullish_count: int = 0
    bearish_count: int = 0
    neutral_count: int = 0
    max_engagement: int = 0
    sources: List[str] = field(default_factory=list)

    @property
    def avg_sentiment(self) -> float:
        if self.total_weight > 0:
            return self.weighted_sentiment / self.total_weight
        if self.mention_count > 0:
            return self.total_sentiment / self.mention_count
        return 0.0

    @property
    def signal_label(self) -> str:
        s = self.avg_sentiment
        if s >= 0.25:
            return "bullish"
        elif s <= -0.25:
            return "bearish"
        return "neutral"


# ── Module-level cache ──────────────────────────────────────

_cache: Dict[str, SymbolSentiment] = {}
_cache_ts: float = 0.0


# ── Public API ──────────────────────────────────────────────

async def fetch_cmc_community_sentiment() -> Dict[str, SymbolSentiment]:
    """
    Fetch and aggregate sentiment from CMC news headlines and topics.

    Returns dict mapping symbol (e.g. "BTC") → SymbolSentiment.
    Results are cached for CMC_CACHE_TTL seconds.
    """
    global _cache, _cache_ts

    if _cache and (time.time() - _cache_ts) < CMC_CACHE_TTL:
        return _cache

    all_posts: List[CmcPost] = []

    async with aiohttp.ClientSession(
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/125.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=aiohttp.ClientTimeout(total=CMC_REQUEST_TIMEOUT),
    ) as session:
        # Scrape CMC news/headlines pages (SSR-rendered articles)
        for news_url in CMC_NEWS_PAGES:
            try:
                posts = await _scrape_news_page(session, news_url)
                all_posts.extend(posts)
                if posts:
                    logger.info(f"[CMC] {news_url}: scraped {len(posts)} articles")
            except Exception as e:
                logger.warning(f"[CMC] Failed to scrape {news_url}: {e}")

        # Scrape topics
        for topic_slug in CMC_TOPICS:
            try:
                posts = await _scrape_topic(session, topic_slug)
                all_posts.extend(posts)
                if posts:
                    logger.info(f"[CMC] topic/{topic_slug}: scraped {len(posts)} items")
            except Exception as e:
                logger.warning(f"[CMC] Failed to scrape topic {topic_slug}: {e}")

    if not all_posts:
        logger.warning("[CMC] No articles scraped from any CMC source")
        return _cache  # Return stale cache rather than empty

    # Aggregate per symbol
    result = _aggregate_sentiment(all_posts)

    symbols_found = len(result)
    total_posts = len(all_posts)
    bullish_count = sum(1 for s in result.values() if s.signal_label == "bullish")
    bearish_count = sum(1 for s in result.values() if s.signal_label == "bearish")

    logger.info(
        f"[CMC] Total: {total_posts} articles → {symbols_found} symbols | "
        f"Bullish: {bullish_count} Bearish: {bearish_count}"
    )

    _cache = result
    _cache_ts = time.time()
    return result


def get_cached_cmc_sentiment() -> Dict[str, SymbolSentiment]:
    """Return the current cached CMC sentiment without fetching."""
    return _cache


# ── Scraping: News Headlines ───────────────────────────────

async def _scrape_news_page(
    session: aiohttp.ClientSession, url: str
) -> List[CmcPost]:
    """
    Scrape CMC headlines/news page for SSR-rendered article JSON.

    CMC embeds article objects in the page HTML with title + subtitle
    fields that contain full article summaries — perfect for sentiment.
    """
    posts: List[CmcPost] = []

    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                logger.warning(f"[CMC] News page {url} returned {resp.status}")
                return posts
            html = await resp.text()
    except Exception as e:
        logger.warning(f"[CMC] HTTP error fetching news: {e}")
        return posts

    # Extract article objects from SSR-embedded JSON
    seen_titles: set = set()
    articles = _ARTICLE_RE.findall(html)

    for title, subtitle, source_name in articles[:CMC_MAX_ARTICLES]:
        # Decode JSON unicode escapes
        try:
            title = title.encode().decode('unicode_escape')
            subtitle = subtitle.encode().decode('unicode_escape') if subtitle else ""
        except (UnicodeDecodeError, UnicodeError):
            pass

        # Deduplicate by title
        title_key = title.strip().lower()
        if title_key in seen_titles or len(title_key) < 15:
            continue
        seen_titles.add(title_key)

        # Skip non-crypto fluff (UI labels, category names, etc.)
        if _is_boilerplate(title):
            continue

        # Combine title + subtitle for richer sentiment analysis
        full_text = f"{title}. {subtitle}" if subtitle else title

        symbols = _extract_symbols(full_text)
        sentiment = _score_text(full_text)

        source = source_name if source_name else "CMC News"

        posts.append(CmcPost(
            author=source,
            text=full_text[:500],
            symbols=symbols,
            sentiment=sentiment,
            engagement=1,  # No engagement data from SSR
            source_type="news",
        ))

    return posts


# ── Scraping: Topic Pages ──────────────────────────────────

async def _scrape_topic(
    session: aiohttp.ClientSession, topic_slug: str
) -> List[CmcPost]:
    """
    Scrape a CMC community topic page for SSR-rendered data.

    Topic pages have title + description fields in the SSR HTML.
    """
    url = f"https://coinmarketcap.com/community/topic/{topic_slug}/"
    posts: List[CmcPost] = []

    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                logger.warning(f"[CMC] Topic {topic_slug} returned {resp.status}")
                return posts
            html = await resp.text()
    except Exception as e:
        logger.warning(f"[CMC] HTTP error fetching topic {topic_slug}: {e}")
        return posts

    # Try to extract __NEXT_DATA__ JSON for structured data
    next_data_match = re.search(
        r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html, re.DOTALL,
    )
    if next_data_match:
        try:
            data = json.loads(next_data_match.group(1))
            # Walk the JSON tree looking for article/post content
            texts = _extract_texts_from_json(data, depth=0)
            for text in texts[:CMC_MAX_ARTICLES]:
                symbols = _extract_symbols(text)
                if not symbols:
                    continue
                sentiment = _score_text(text)
                posts.append(CmcPost(
                    author=f"topic:{topic_slug}",
                    text=text[:500],
                    symbols=symbols,
                    sentiment=sentiment,
                    engagement=1,
                    source_type="topic",
                ))
        except (json.JSONDecodeError, KeyError):
            pass

    # Also try regex extraction for title/description pairs in the HTML
    topic_articles = _ARTICLE_RE.findall(html)
    seen = {p.text for p in posts}
    for title, subtitle, source in topic_articles[:10]:
        try:
            title = title.encode().decode('unicode_escape')
            subtitle = subtitle.encode().decode('unicode_escape') if subtitle else ""
        except (UnicodeDecodeError, UnicodeError):
            pass

        full_text = f"{title}. {subtitle}" if subtitle else title
        if full_text[:500] in seen or len(title) < 15 or _is_boilerplate(title):
            continue
        seen.add(full_text[:500])

        symbols = _extract_symbols(full_text)
        sentiment = _score_text(full_text)
        posts.append(CmcPost(
            author=f"topic:{topic_slug}",
            text=full_text[:500],
            symbols=symbols,
            sentiment=sentiment,
            engagement=1,
            source_type="topic",
        ))

    return posts


def _extract_texts_from_json(obj, depth: int = 0) -> List[str]:
    """Recursively extract meaningful text from nested JSON structure."""
    if depth > 8:
        return []
    texts: List[str] = []
    if isinstance(obj, dict):
        # Look for title/description/content fields
        for key in ("title", "description", "content", "text", "body", "summary"):
            val = obj.get(key)
            if isinstance(val, str) and len(val) > 30:
                # Skip i18n translation strings
                if not val.startswith("{{") and "i18n" not in val.lower():
                    texts.append(val)
        for v in obj.values():
            texts.extend(_extract_texts_from_json(v, depth + 1))
    elif isinstance(obj, list):
        for item in obj:
            texts.extend(_extract_texts_from_json(item, depth + 1))
    return texts


def _is_boilerplate(title: str) -> bool:
    """Check if a title is UI boilerplate rather than a real article."""
    lower = title.lower().strip()
    boilerplate = [
        "cookie", "sign up", "log in", "accept all", "customize",
        "use coinmarketcap", "top community", "products", "learn",
        "price estimates", "portfolio tracker", "rehypo",
    ]
    if any(bp in lower for bp in boilerplate):
        return True
    # Skip emoji-only category labels (e.g. "🔥 BNB", "🔥 Memes")
    stripped = re.sub(r'[^\w\s]', '', lower).strip()
    if len(stripped) < 8:
        return True
    return False


# ── Symbol extraction ───────────────────────────────────────

# Standalone known majors mentioned without $ prefix
_KNOWN_SYMBOLS = frozenset({
    "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX",
    "DOT", "MATIC", "LINK", "UNI", "AAVE", "LTC", "NEAR",
    "APT", "SUI", "ARB", "OP", "FTM", "ATOM", "INJ", "TIA",
    "SEI", "JUP", "WLD", "TAO", "ONDO", "PENDLE", "ENA",
    "PEPE", "WIF", "BONK", "FLOKI", "SHIB", "MEME", "KAS",
    "XDC", "MATIC", "POL", "CRO",
})

# Coin name → ticker mappings for articles that use full names
_COIN_NAMES = {
    "bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL",
    "ripple": "XRP", "xrp": "XRP", "dogecoin": "DOGE",
    "cardano": "ADA", "avalanche": "AVAX", "polkadot": "DOT",
    "chainlink": "LINK", "litecoin": "LTC", "kaspa": "KAS",
    "bnb chain": "BNB", "bnb": "BNB", "binance": "BNB",
}

# Words that look like tickers but aren't
_FALSE_TICKERS = frozenset({
    "AI", "US", "CEO", "CFO", "ETF", "IPO", "SEC", "CPI", "GDP",
    "USD", "EUR", "GBP", "API", "RSS", "APP", "URL", "FAQ",
    "KOL", "CMC", "DEX", "NFT", "TVL", "ATH", "ATL", "ROI",
    "APR", "APY", "DCA", "DAO", "DAPP", "RWA", "KYC", "AML",
    "FDV", "MACD", "RSI", "EMA", "SMA", "MSTR", "NEW", "DOJ",
    "CAD", "OIL", "FOMO",
})


def _extract_symbols(text: str) -> List[str]:
    """Extract unique coin symbols mentioned in post text."""
    found: set = set()

    # Match $TICKER patterns
    for match in _TICKER_RE.finditer(text):
        ticker = match.group(1).upper()
        if ticker not in _FALSE_TICKERS and len(ticker) >= 2:
            found.add(ticker)

    # Scan for known majors mentioned by ticker without $ prefix
    text_upper = text.upper()
    for sym in _KNOWN_SYMBOLS:
        if re.search(rf'\b{sym}\b', text_upper):
            found.add(sym)

    # Scan for coin full names (e.g. "Bitcoin", "Ethereum", "Solana")
    text_lower = text.lower()
    for name, ticker in _COIN_NAMES.items():
        if name in text_lower:
            found.add(ticker)

    return sorted(found)

    return sorted(found)


# ── Sentiment scoring ───────────────────────────────────────

def _score_text(text: str) -> float:
    """Score post text sentiment from -1 (bearish) to +1 (bullish)."""
    if _vader:
        scores = _vader.polarity_scores(text)
        return scores["compound"]

    # Fallback: keyword counting
    text_lower = text.lower()
    bullish = sum(1 for kw in _BULLISH_KW if kw in text_lower)
    bearish = sum(1 for kw in _BEARISH_KW if kw in text_lower)
    total = bullish + bearish
    if total == 0:
        return 0.0
    return (bullish - bearish) / total


# ── Aggregation ─────────────────────────────────────────────

def _aggregate_sentiment(posts: List[CmcPost]) -> Dict[str, SymbolSentiment]:
    """Aggregate post-level sentiment into per-symbol summaries."""
    symbols: Dict[str, SymbolSentiment] = {}

    for post in posts:
        if not post.symbols:
            continue

        # Engagement-based weight: higher engagement posts matter more
        weight = 1.0 + min(4.0, post.engagement / 10.0)

        # News articles weigh more than topic posts (established sources)
        if post.source_type == "news":
            weight *= 1.2
        elif post.source_type == "topic":
            weight *= 1.0

        for sym in post.symbols:
            if sym not in symbols:
                symbols[sym] = SymbolSentiment(symbol=sym)

            entry = symbols[sym]
            entry.mention_count += 1
            entry.total_sentiment += post.sentiment
            entry.weighted_sentiment += post.sentiment * weight
            entry.total_weight += weight
            entry.max_engagement = max(entry.max_engagement, post.engagement)

            if post.sentiment >= 0.15:
                entry.bullish_count += 1
            elif post.sentiment <= -0.15:
                entry.bearish_count += 1
            else:
                entry.neutral_count += 1

            src = post.author
            if src not in entry.sources:
                entry.sources.append(src)

    return symbols
