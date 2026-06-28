"""
Multi-Source Financial News Scraper
Sources: Financial Juice, Reuters, CNBC, Bloomberg, Financial Times,
         MarketWatch, Investing.com, Yahoo Finance, WSJ, Barron's,
         CoinDesk, Cointelegraph, TheBlock, Decrypt, CryptoPanic, CoinGecko
"""
import re
import asyncio
import aiohttp
import feedparser
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
from app.core.timezone import now_sast, SAST
from loguru import logger


# ────────────────────────────────────────────────────────────
# Data Structures
# ────────────────────────────────────────────────────────────

class NewsItem:
    """Normalized news item from any source."""
    __slots__ = (
        "title", "summary", "source", "url", "published_at",
        "symbols", "reliability", "category",
    )

    def __init__(
        self,
        title: str,
        summary: str,
        source: str,
        url: str,
        published_at: Optional[datetime] = None,
        symbols: Optional[List[str]] = None,
        reliability: float = 0.5,
        category: str = "general",
    ):
        self.title = title
        self.summary = summary
        self.source = source
        self.url = url
        self.published_at = published_at or now_sast()
        self.symbols = symbols or []
        self.reliability = reliability  # 0-1, how trustworthy is this source
        self.category = category  # general, macro, crypto, regulation, earnings

    def to_dict(self) -> Dict:
        return {
            "title": self.title,
            "summary": self.summary,
            "source": self.source,
            "url": self.url,
            "published_at": self.published_at.isoformat(),
            "symbols": self.symbols,
            "reliability": self.reliability,
            "category": self.category,
        }


# ────────────────────────────────────────────────────────────
# Symbol Extraction — extended mapping
# ────────────────────────────────────────────────────────────

CRYPTO_KEYWORDS: Dict[str, str] = {
    "bitcoin": "BTC", "btc": "BTC",
    "ethereum": "ETH", "eth": "ETH", "ether": "ETH",
    "solana": "SOL", "sol": "SOL",
    "cardano": "ADA", "ada": "ADA",
    "ripple": "XRP", "xrp": "XRP",
    "polkadot": "DOT", "dot": "DOT",
    "dogecoin": "DOGE", "doge": "DOGE",
    "avalanche": "AVAX", "avax": "AVAX",
    "bnb": "BNB", "binance coin": "BNB",
    "chainlink": "LINK", "link": "LINK",
    "near protocol": "NEAR", "near": "NEAR",
    "arbitrum": "ARB", "arb": "ARB",
    "optimism": "OP",
    "polygon": "MATIC", "matic": "MATIC",
    "litecoin": "LTC", "ltc": "LTC",
    "tron": "TRX", "trx": "TRX",
    "cosmos": "ATOM", "atom": "ATOM",
    "uniswap": "UNI", "uni": "UNI",
    "aave": "AAVE",
    "maker": "MKR",
    "sui": "SUI",
    "aptos": "APT", "apt": "APT",
    "pepe": "PEPE",
    "shiba": "SHIB", "shib": "SHIB", "shiba inu": "SHIB",
    "floki": "FLOKI",
    "bonk": "BONK",
    "dogwifhat": "WIF", "wif": "WIF",
    "filecoin": "FIL", "fil": "FIL",
    "render": "RNDR",
    "injective": "INJ",
    "sei": "SEI",
    "stacks": "STX",
    "crypto": "_MARKET_",  # General crypto sentiment
    "cryptocurrency": "_MARKET_",
    "digital asset": "_MARKET_",
    "defi": "_MARKET_",
}

# Macro keywords that affect ALL crypto
MACRO_KEYWORDS = {
    "federal reserve": "_MACRO_",
    "fed rate": "_MACRO_",
    "interest rate": "_MACRO_",
    "inflation": "_MACRO_",
    "cpi": "_MACRO_",
    "ppi": "_MACRO_",
    "gdp": "_MACRO_",
    "employment": "_MACRO_",
    "nonfarm": "_MACRO_",
    "treasury": "_MACRO_",
    "dollar index": "_MACRO_",
    "dxy": "_MACRO_",
    "risk-on": "_MACRO_",
    "risk-off": "_MACRO_",
    "quantitative": "_MACRO_",
    "sec": "_REGULATION_",
    "cftc": "_REGULATION_",
    "regulation": "_REGULATION_",
    "etf": "_REGULATION_",
    "spot etf": "_REGULATION_",
    "bitcoin etf": "BTC",
    "ethereum etf": "ETH",
    # Stocks / equity market context
    "stock market": "_MACRO_",
    "stocks": "_MACRO_",
    "equities": "_MACRO_",
    "nasdaq": "_MACRO_",
    "s&p 500": "_MACRO_",
    "dow jones": "_MACRO_",
    "wall street": "_MACRO_",
    "earnings": "_MACRO_",
    # Geopolitical / war context
    "war": "_MACRO_",
    "conflict": "_MACRO_",
    "geopolitical": "_MACRO_",
    "middle east": "_MACRO_",
    "ukraine": "_MACRO_",
    "russia": "_MACRO_",
    "israel": "_MACRO_",
    "iran": "_MACRO_",
}


MULTI_MARKET_QUERY = (
    "crypto OR cryptocurrency OR bitcoin OR ethereum OR altcoin OR "
    "stocks OR equities OR nasdaq OR s&p 500 OR "
    "war OR conflict OR geopolitical OR ukraine OR russia OR israel OR iran"
)


def extract_symbols(text: str) -> Tuple[List[str], List[str]]:
    """
    Extract crypto symbols and macro tags from text.
    Returns: (symbols, tags) — tags are _MARKET_, _MACRO_, _REGULATION_
    """
    text_lower = text.lower()
    symbols = []
    tags = []

    for keyword, sym in {**CRYPTO_KEYWORDS, **MACRO_KEYWORDS}.items():
        if keyword in text_lower:
            if sym.startswith("_"):
                if sym not in tags:
                    tags.append(sym)
            elif sym not in symbols:
                symbols.append(sym)

    return symbols, tags


def _merge_symbols_and_tags(
    symbols: List[str],
    tags: List[str],
    include_market_fallback: bool = True,
) -> List[str]:
    """Merge direct symbols with sentiment tags used by the aggregator."""
    merged = list(dict.fromkeys(symbols))
    for tag in tags:
        if tag in {"_MARKET_", "_MACRO_", "_REGULATION_"} and tag not in merged:
            merged.append(tag)
    if not merged and include_market_fallback and "_MARKET_" in tags:
        return ["_MARKET_"]
    return merged


def _category_from_tags(tags: List[str], default: str = "crypto") -> str:
    """Promote category when macro/regulatory context is explicitly present."""
    if "_MACRO_" in tags or "_REGULATION_" in tags:
        return "macro"
    return default


def _as_sast_naive(value: datetime) -> datetime:
    """Convert timezone-aware datetimes to naive SAST for DB compatibility."""
    if value.tzinfo is None:
        return value
    return value.astimezone(SAST).replace(tzinfo=None)


def _parse_iso_datetime(value: str) -> Optional[datetime]:
    """Parse ISO datetime strings and normalize to naive SAST."""
    if not value:
        return None

    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized.replace("Z", "+00:00")

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    return _as_sast_naive(parsed)


# ────────────────────────────────────────────────────────────
# RSS Feed Scraper (works for most sources)
# ────────────────────────────────────────────────────────────

# Source configs: (url, reliability, category, max_articles)
RSS_SOURCES: Dict[str, Dict] = {
    # ── Tier 1: Major Financial News (highest reliability) ──
    "reuters_markets": {
        "url": "https://www.reutersagency.com/feed/?taxonomy=best-sectors&post_type=best",
        "reliability": 0.95,
        "category": "macro",
        "max": 15,
    },
    "reuters_business": {
        "url": "https://feeds.reuters.com/reuters/businessNews",
        "reliability": 0.95,
        "category": "macro",
        "max": 15,
    },
    "cnbc_markets": {
        "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
        "reliability": 0.90,
        "category": "macro",
        "max": 15,
    },
    "cnbc_crypto": {
        "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=33002080",
        "reliability": 0.90,
        "category": "crypto",
        "max": 15,
    },
    "bloomberg_markets": {
        "url": "https://feeds.bloomberg.com/markets/news.rss",
        "reliability": 0.95,
        "category": "macro",
        "max": 15,
    },
    "ft_markets": {
        "url": "https://www.ft.com/rss/home",
        "reliability": 0.93,
        "category": "macro",
        "max": 10,
    },
    "wsj_markets": {
        "url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
        "reliability": 0.93,
        "category": "macro",
        "max": 15,
    },
    "barrons": {
        "url": "https://feeds.barrons.com/marketcurrents/xml",
        "reliability": 0.88,
        "category": "macro",
        "max": 10,
    },
    "marketwatch": {
        "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
        "reliability": 0.85,
        "category": "macro",
        "max": 15,
    },
    "yahoo_finance": {
        "url": "https://finance.yahoo.com/news/rssindex",
        "reliability": 0.80,
        "category": "macro",
        "max": 15,
    },
    "investing_com": {
        "url": "https://www.investing.com/rss/news.rss",
        "reliability": 0.80,
        "category": "macro",
        "max": 15,
    },
    "investopedia_markets": {
        "url": "https://www.investopedia.com/feedbuilder/feed/getfeed/?feedName=rss_headline",
        "reliability": 0.85,
        "category": "macro",
        "max": 15,
    },

    # ── Tier 2b: Forex / Futures / Stocks ──
    "forexlive": {
        "url": "https://www.forexlive.com/feed/news",
        "reliability": 0.80,
        "category": "forex",
        "max": 15,
    },
    "dailyfx": {
        "url": "https://www.dailyfx.com/feeds/market-news",
        "reliability": 0.78,
        "category": "forex",
        "max": 15,
    },
    "fxstreet": {
        "url": "https://www.fxstreet.com/rss",
        "reliability": 0.78,
        "category": "forex",
        "max": 15,
    },
    "seeking_alpha": {
        "url": "https://seekingalpha.com/market_currents.xml",
        "reliability": 0.80,
        "category": "stocks",
        "max": 15,
    },
    "benzinga": {
        "url": "https://www.benzinga.com/feed",
        "reliability": 0.78,
        "category": "stocks",
        "max": 15,
    },
    "zerohedge": {
        "url": "https://feeds.feedburner.com/zerohedge/feed",
        "reliability": 0.65,
        "category": "macro",
        "max": 10,
    },

    # ── Tier 2: Crypto-Specific News ──
    "coindesk": {
        "url": "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "reliability": 0.85,
        "category": "crypto",
        "max": 20,
    },
    "cointelegraph": {
        "url": "https://cointelegraph.com/rss",
        "reliability": 0.82,
        "category": "crypto",
        "max": 20,
    },
    "theblock": {
        "url": "https://www.theblock.co/rss.xml",
        "reliability": 0.85,
        "category": "crypto",
        "max": 15,
    },
    "decrypt": {
        "url": "https://decrypt.co/feed",
        "reliability": 0.78,
        "category": "crypto",
        "max": 15,
    },
    "bitcoinmagazine": {
        "url": "https://bitcoinmagazine.com/.rss/full/",
        "reliability": 0.80,
        "category": "crypto",
        "max": 10,
    },
    "blockworks": {
        "url": "https://blockworks.co/feed",
        "reliability": 0.82,
        "category": "crypto",
        "max": 15,
    },
    "dailyhodl": {
        "url": "https://dailyhodl.com/feed/",
        "reliability": 0.70,
        "category": "crypto",
        "max": 10,
    },
}


async def _fetch_rss(
    source_name: str,
    config: Dict,
    session: aiohttp.ClientSession,
    max_age_hours: int = 6,
) -> List[NewsItem]:
    """Fetch and parse a single RSS feed."""
    url = config["url"]
    reliability = config.get("reliability", 0.5)
    category = config.get("category", "general")
    max_articles = config.get("max", 15)
    cutoff = now_sast() - timedelta(hours=max_age_hours)

    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            if resp.status != 200:
                logger.warning(f"RSS {source_name}: HTTP {resp.status}")
                return []
            raw = await resp.text()

        feed = feedparser.parse(raw)
        items: List[NewsItem] = []

        for entry in feed.entries[:max_articles]:
            title = entry.get("title", "").strip()
            summary = entry.get("summary", entry.get("description", "")).strip()
            if not title:
                continue

            # Parse published date
            pub_dt = None
            if entry.get("published_parsed"):
                try:
                    pub_dt = _as_sast_naive(datetime(*entry.published_parsed[:6], tzinfo=timezone.utc))
                except Exception:
                    pass
            elif entry.get("updated_parsed"):
                try:
                    pub_dt = _as_sast_naive(datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc))
                except Exception:
                    pass
            if pub_dt and pub_dt < cutoff:
                continue  # skip old articles

            # Clean HTML from summary
            if summary and "<" in summary:
                summary = BeautifulSoup(summary, "html.parser").get_text(separator=" ")
            summary = summary[:500] if summary else ""

            full_text = f"{title} {summary}"
            symbols, tags = extract_symbols(full_text)

            items.append(NewsItem(
                title=title,
                summary=summary,
                source=source_name,
                url=entry.get("link", ""),
                published_at=pub_dt,
                symbols=_merge_symbols_and_tags(symbols, tags),
                reliability=reliability,
                category=category,
            ))

        logger.info(f"📰 {source_name}: {len(items)} articles")
        return items

    except asyncio.TimeoutError:
        logger.warning(f"RSS {source_name}: timeout")
        return []
    except Exception as e:
        logger.warning(f"RSS {source_name}: {e}")
        return []


# ────────────────────────────────────────────────────────────
# Financial Juice Scraper (HTML scraping — no RSS available)
# ────────────────────────────────────────────────────────────

async def fetch_financial_juice(
    session: aiohttp.ClientSession,
    max_age_hours: int = 6,
) -> List[NewsItem]:
    """
    Scrape Financial Juice headlines.
    Financial Juice is a real-time financial news aggregator.
    Falls back gracefully if blocked.
    """
    url = "https://www.financialjuice.com/home"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml",
    }
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                logger.warning(f"Financial Juice: HTTP {resp.status}")
                return []
            html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")
        items: List[NewsItem] = []

        # Financial Juice uses div.feed-item or similar structures
        for el in soup.select(".feed-item, .news-item, article, .headline-item")[:25]:
            title_el = el.select_one("h2, h3, .title, .headline, a")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            if not title or len(title) < 10:
                continue

            link = ""
            a_tag = el.select_one("a[href]")
            if a_tag:
                link = a_tag.get("href", "")
                if link.startswith("/"):
                    link = f"https://www.financialjuice.com{link}"

            summary_el = el.select_one("p, .summary, .description")
            summary = summary_el.get_text(strip=True) if summary_el else ""

            full_text = f"{title} {summary}"
            symbols, tags = extract_symbols(full_text)

            items.append(NewsItem(
                title=title,
                summary=summary[:500],
                source="financialjuice",
                url=link,
                published_at=now_sast(),  # FJ doesn't always have timestamps
                symbols=_merge_symbols_and_tags(symbols, tags),
                reliability=0.88,
                category="macro",
            ))

        logger.info(f"📰 financialjuice: {len(items)} headlines")
        return items

    except Exception as e:
        logger.warning(f"Financial Juice scraper: {e}")
        return []


# ────────────────────────────────────────────────────────────
# CryptoPanic API
# ────────────────────────────────────────────────────────────

async def fetch_cryptopanic(
    session: aiohttp.ClientSession,
    api_key: str,
    max_age_hours: int = 6,
) -> List[NewsItem]:
    """Fetch from CryptoPanic API."""
    if not api_key:
        return []

    cutoff = now_sast() - timedelta(hours=max_age_hours)

    try:
        url = f"https://cryptopanic.com/api/v1/posts/?auth_token={api_key}&public=true&kind=news"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            if resp.status != 200:
                logger.warning(f"CryptoPanic: HTTP {resp.status}")
                return []
            data = await resp.json()

        items: List[NewsItem] = []
        for post in data.get("results", [])[:25]:
            title = post.get("title", "")
            symbols_raw = [c["code"] for c in post.get("currencies", [])]
            pub_str = post.get("published_at", "")
            pub_dt = _parse_iso_datetime(pub_str)

            # Keep only recent posts
            if not pub_dt or pub_dt < cutoff:
                continue

            # Also extract from title
            extra_symbols, tags = extract_symbols(title)
            all_symbols = list(set(symbols_raw + extra_symbols))

            items.append(NewsItem(
                title=title,
                summary=title,
                source="cryptopanic",
                url=post.get("url", ""),
                published_at=pub_dt,
                symbols=_merge_symbols_and_tags(all_symbols, tags, include_market_fallback=False),
                reliability=0.75,
                category="crypto",
            ))

        logger.info(f"📰 cryptopanic: {len(items)} articles")
        return items

    except Exception as e:
        logger.warning(f"CryptoPanic: {e}")
        return []


# ────────────────────────────────────────────────────────────
# CoinGecko — Trending + News
# ────────────────────────────────────────────────────────────

async def fetch_coingecko_news(
    session: aiohttp.ClientSession,
    api_key: str,
) -> List[NewsItem]:
    """
    Fetch trending coins + status updates from CoinGecko.
    Uses Demo-tier base URL with x-cg-demo-api-key header.
    """
    if not api_key:
        return []

    base = "https://api.coingecko.com/api/v3"
    headers = {"x-cg-demo-api-key": api_key}
    items: List[NewsItem] = []

    # 1. Trending coins — market sentiment signal
    try:
        async with session.get(
            f"{base}/search/trending",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=12),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                trending_coins = data.get("coins", [])[:7]
                if trending_coins:
                    names = [c["item"]["name"] for c in trending_coins if "item" in c]
                    syms = []
                    for c in trending_coins:
                        sym = c.get("item", {}).get("symbol", "").upper()
                        if sym:
                            syms.append(sym)

                    title = f"CoinGecko Trending: {', '.join(names[:5])} are trending now"
                    all_symbols = list(set(syms))
                    # Also extract from names
                    for name in names:
                        extra_s, _ = extract_symbols(name)
                        all_symbols.extend(extra_s)
                    all_symbols = list(set(all_symbols))

                    items.append(NewsItem(
                        title=title,
                        summary=f"Top trending coins on CoinGecko: {', '.join(names)}. High search volume indicates market interest.",
                        source="coingecko_trending",
                        url="https://www.coingecko.com/en/trending",
                        published_at=now_sast(),
                        symbols=all_symbols if all_symbols else ["_MARKET_"],
                        reliability=0.85,
                        category="crypto",
                    ))

                # Individual trending coin items for per-symbol sentiment
                for c in trending_coins:
                    coin = c.get("item", {})
                    name = coin.get("name", "")
                    sym = coin.get("symbol", "").upper()
                    price_change = coin.get("data", {}).get("price_change_percentage_24h", {}).get("usd", 0)
                    market_cap_rank = coin.get("data", {}).get("market_cap_rank") or coin.get("item", {}).get("market_cap_rank", "N/A")

                    if not name:
                        continue

                    direction = "up" if price_change and price_change > 0 else "down"
                    title = f"{name} ({sym}) is trending on CoinGecko — {direction} {abs(price_change or 0):.1f}% in 24h"
                    extra_s, _ = extract_symbols(name)
                    coin_symbols = list(set([sym] + extra_s)) if sym else extra_s

                    items.append(NewsItem(
                        title=title,
                        summary=f"{name} ranked #{market_cap_rank} by market cap, is trending with high search volume.",
                        source="coingecko_trending",
                        url=f"https://www.coingecko.com/en/coins/{coin.get('id', '')}",
                        published_at=now_sast(),
                        symbols=coin_symbols if coin_symbols else ["_MARKET_"],
                        reliability=0.80,
                        category="crypto",
                    ))
            else:
                logger.warning(f"CoinGecko trending: HTTP {resp.status}")
    except Exception as e:
        logger.warning(f"CoinGecko trending: {e}")

    # 2. Top gainers & losers (sentiment signals)
    try:
        async with session.get(
            f"{base}/coins/top_gainers_losers",
            headers=headers,
            params={"vs_currency": "usd", "duration": "24h"},
            timeout=aiohttp.ClientTimeout(total=12),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                for category, label in [("top_gainers", "gainer"), ("top_losers", "loser")]:
                    for coin in data.get(category, [])[:5]:
                        name = coin.get("name", "")
                        sym = coin.get("symbol", "").upper()
                        pct = coin.get("usd_24h_change", 0)
                        if not name:
                            continue
                        extra_s, _ = extract_symbols(name)
                        coin_symbols = list(set([sym] + extra_s)) if sym else extra_s

                        title = f"{name} ({sym}) is a top {label}: {'+'if pct > 0 else ''}{pct:.1f}% in 24h"
                        items.append(NewsItem(
                            title=title,
                            summary=f"{name} is among the top {label}s on CoinGecko with a {pct:.1f}% change.",
                            source="coingecko_movers",
                            url=f"https://www.coingecko.com/en/coins/{coin.get('id', '')}",
                            published_at=now_sast(),
                            symbols=coin_symbols if coin_symbols else ["_MARKET_"],
                            reliability=0.80,
                            category="crypto",
                        ))
            elif resp.status != 403:  # 403 = plan limitation, ignore silently
                logger.warning(f"CoinGecko gainers/losers: HTTP {resp.status}")
    except Exception as e:
        logger.warning(f"CoinGecko gainers/losers: {e}")

    logger.info(f"📰 coingecko: {len(items)} items")
    return items


# ────────────────────────────────────────────────────────────
# CoinMarketCap — Latest News Headlines
# ────────────────────────────────────────────────────────────

async def fetch_coinmarketcap_news(
    session: aiohttp.ClientSession,
    api_key: str,
) -> List[NewsItem]:
    """
    Fetch market movers from CoinMarketCap (free tier compatible).
    Uses listings/latest sorted by 24h change to generate sentiment signals.
    """
    if not api_key:
        return []

    items: List[NewsItem] = []
    base = "https://pro-api.coinmarketcap.com/v1"
    headers = {"X-CMC_PRO_API_KEY": api_key, "Accept": "application/json"}

    # Top gainers
    for sort_dir, label in [("desc", "gainer"), ("asc", "loser")]:
        try:
            async with session.get(
                f"{base}/cryptocurrency/listings/latest",
                headers=headers,
                params={
                    "start": "1",
                    "limit": "10",
                    "sort": "percent_change_24h",
                    "sort_dir": sort_dir,
                    "convert": "USD",
                    "volume_24h_min": "100000",  # filter out dust
                },
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"CoinMarketCap {label}s: HTTP {resp.status}")
                    continue
                data = await resp.json()

                for coin in data.get("data", [])[:8]:
                    name = coin.get("name", "")
                    sym = coin.get("symbol", "").upper()
                    quote = coin.get("quote", {}).get("USD", {})
                    pct_24h = quote.get("percent_change_24h", 0)
                    pct_7d = quote.get("percent_change_7d", 0)
                    vol = quote.get("volume_24h", 0)
                    rank = coin.get("cmc_rank", "N/A")

                    if not name:
                        continue

                    extra_s, _ = extract_symbols(name)
                    coin_symbols = list(set([sym] + extra_s)) if sym else extra_s

                    title = (
                        f"{name} ({sym}) CMC #{rank}: "
                        f"{'+'if pct_24h > 0 else ''}{pct_24h:.1f}% 24h, "
                        f"{'+'if pct_7d > 0 else ''}{pct_7d:.1f}% 7d"
                    )
                    summary = (
                        f"{name} ranked #{rank} on CoinMarketCap. "
                        f"24h change: {pct_24h:+.1f}%, 7d change: {pct_7d:+.1f}%, "
                        f"24h volume: ${vol:,.0f}."
                    )
                    items.append(NewsItem(
                        title=title,
                        summary=summary,
                        source="coinmarketcap",
                        url=f"https://coinmarketcap.com/currencies/{coin.get('slug', '')}",
                        published_at=now_sast(),
                        symbols=coin_symbols if coin_symbols else ["_MARKET_"],
                        reliability=0.82,
                        category="crypto",
                    ))
        except Exception as e:
            logger.warning(f"CoinMarketCap {label}s: {e}")

    logger.info(f"📰 coinmarketcap: {len(items)} articles")
    return items


# ────────────────────────────────────────────────────────────
# Alternative.me Fear & Greed Index
# ────────────────────────────────────────────────────────────

async def fetch_alternative_fng(
    session: aiohttp.ClientSession,
    max_age_hours: int = 24,
) -> List[NewsItem]:
    """Fetch crypto fear/greed sentiment snapshots."""
    cutoff = now_sast() - timedelta(hours=max_age_hours)

    try:
        async with session.get(
            "https://api.alternative.me/fng/?limit=5&format=json",
            timeout=aiohttp.ClientTimeout(total=12),
        ) as resp:
            if resp.status != 200:
                logger.warning(f"Alternative.me FNG: HTTP {resp.status}")
                return []
            data = await resp.json()

        items: List[NewsItem] = []
        for point in data.get("data", []):
            timestamp = point.get("timestamp")
            if not timestamp:
                continue

            try:
                pub_dt = _as_sast_naive(datetime.fromtimestamp(int(timestamp), tz=timezone.utc))
            except Exception:
                continue

            if pub_dt < cutoff:
                continue

            value = point.get("value", "")
            classification = point.get("value_classification", "Unknown")
            time_until_update = point.get("time_until_update")

            summary = f"Alternative.me Fear & Greed Index is {value} ({classification})."
            if time_until_update:
                summary += f" Next update in ~{time_until_update} seconds."

            items.append(NewsItem(
                title=f"Crypto Fear & Greed Index: {value} ({classification})",
                summary=summary,
                source="alternative_fng",
                url="https://alternative.me/crypto/fear-and-greed-index/",
                published_at=pub_dt,
                symbols=["BTC", "ETH", "_MARKET_"],
                reliability=0.90,
                category="macro",
            ))

        logger.info(f"📰 alternative_fng: {len(items)} updates")
        return items

    except Exception as e:
        logger.warning(f"Alternative.me FNG: {e}")
        return []


# ────────────────────────────────────────────────────────────
# MarketAux News API
# ────────────────────────────────────────────────────────────

async def fetch_marketaux_news(
    session: aiohttp.ClientSession,
    api_key: str,
    max_age_hours: int = 6,
) -> List[NewsItem]:
    """Fetch entity-tagged news from MarketAux."""
    if not api_key:
        return []

    cutoff = now_sast() - timedelta(hours=max_age_hours)

    try:
        async with session.get(
            "https://api.marketaux.com/v1/news/all",
            params={
                "api_token": api_key,
                "language": "en",
                "limit": 50,
                "sort": "published_desc",
                "filter_entities": "true",
            },
            timeout=aiohttp.ClientTimeout(total=12),
        ) as resp:
            if resp.status != 200:
                logger.warning(f"MarketAux: HTTP {resp.status}")
                return []
            payload = await resp.json()

        items: List[NewsItem] = []
        for article in payload.get("data", [])[:40]:
            title = (article.get("title") or "").strip()
            description = (article.get("description") or article.get("snippet") or "").strip()
            if not title:
                continue

            pub_dt = _parse_iso_datetime(article.get("published_at", ""))
            if not pub_dt or pub_dt < cutoff:
                continue

            # Use provider entities first, then fallback to keyword extraction.
            entity_symbols: List[str] = []
            for entity in article.get("entities", []) or []:
                raw_symbol = (entity.get("symbol") or "").upper().strip()
                if not raw_symbol:
                    continue
                symbol = raw_symbol.split(".")[0]
                if symbol and len(symbol) <= 12:
                    entity_symbols.append(symbol)

            merged_text = f"{title}. {description}" if description else title
            extracted_symbols, tags = extract_symbols(merged_text)

            all_symbols = sorted(set(entity_symbols + extracted_symbols))
            if not all_symbols and "_MARKET_" in tags:
                all_symbols = ["_MARKET_"]

            sentiment_score = article.get("sentiment_score")
            sentiment_hint = ""
            if sentiment_score is not None:
                try:
                    sentiment_hint = f" MarketAux sentiment score: {float(sentiment_score):.2f}."
                except Exception:
                    pass

            summary = (description or title)[:500]
            if sentiment_hint:
                summary = f"{summary}{sentiment_hint}"[:500]

            items.append(NewsItem(
                title=title,
                summary=summary,
                source="marketaux",
                url=article.get("url", ""),
                published_at=pub_dt,
                symbols=_merge_symbols_and_tags(all_symbols, tags),
                reliability=0.88,
                category=_category_from_tags(tags, default="crypto"),
            ))

        logger.info(f"📰 marketaux: {len(items)} articles")
        return items

    except Exception as e:
        logger.warning(f"MarketAux: {e}")
        return []


# ────────────────────────────────────────────────────────────
# CoinCap Movers (real-time no-key market pulse)
# ────────────────────────────────────────────────────────────

async def fetch_coincap_movers(
    session: aiohttp.ClientSession,
) -> List[NewsItem]:
    """Create sentiment items from real-time CoinCap 24h movers."""
    try:
        async with session.get(
            "https://api.coincap.io/v2/assets",
            params={"limit": 30},
            timeout=aiohttp.ClientTimeout(total=12),
        ) as resp:
            if resp.status != 200:
                logger.warning(f"CoinCap movers: HTTP {resp.status}")
                return []
            payload = await resp.json()

        assets = payload.get("data", [])
        if not assets:
            return []

        # Highest absolute movers are strongest sentiment signals.
        ranked = sorted(
            assets,
            key=lambda a: abs(float(a.get("changePercent24Hr") or 0.0)),
            reverse=True,
        )[:8]

        now = now_sast()
        items: List[NewsItem] = []
        for asset in ranked:
            name = (asset.get("name") or "").strip()
            symbol = (asset.get("symbol") or "").upper().strip()
            if not name or not symbol:
                continue

            try:
                pct = float(asset.get("changePercent24Hr") or 0.0)
            except Exception:
                pct = 0.0

            title = f"{name} ({symbol}) moved {pct:+.1f}% in 24h on CoinCap"
            summary = (
                f"CoinCap real-time market pulse: {name} ({symbol}) is "
                f"{pct:+.1f}% over the last 24h."
            )

            items.append(NewsItem(
                title=title,
                summary=summary,
                source="coincap_movers",
                url=f"https://coincap.io/assets/{(asset.get('id') or '').lower()}",
                published_at=now,
                symbols=[symbol, "_MARKET_"],
                reliability=0.76,
                category="crypto",
            ))

        logger.info(f"📰 coincap_movers: {len(items)} items")
        return items

    except Exception as e:
        logger.warning(f"CoinCap movers: {e}")
        return []


# ────────────────────────────────────────────────────────────
# GNews API
# ────────────────────────────────────────────────────────────

async def fetch_gnews(
    session: aiohttp.ClientSession,
    api_key: str,
    max_age_hours: int = 6,
) -> List[NewsItem]:
    """Fetch recent crypto headlines from GNews."""
    if not api_key:
        return []

    cutoff = now_sast() - timedelta(hours=max_age_hours)

    try:
        async with session.get(
            "https://gnews.io/api/v4/search",
            params={
                "q": MULTI_MARKET_QUERY,
                "lang": "en",
                "max": 25,
                "sortby": "publishedAt",
                "token": api_key,
            },
            timeout=aiohttp.ClientTimeout(total=12),
        ) as resp:
            if resp.status != 200:
                logger.warning(f"GNews: HTTP {resp.status}")
                return []
            payload = await resp.json()

        items: List[NewsItem] = []
        for article in payload.get("articles", []):
            title = (article.get("title") or "").strip()
            description = (article.get("description") or "").strip()
            if not title:
                continue

            pub_dt = _parse_iso_datetime(article.get("publishedAt", ""))
            if not pub_dt or pub_dt < cutoff:
                continue

            text = f"{title}. {description}" if description else title
            symbols, tags = extract_symbols(text)
            symbols = _merge_symbols_and_tags(symbols, tags)

            items.append(NewsItem(
                title=title,
                summary=(description or title)[:500],
                source="gnews",
                url=article.get("url", ""),
                published_at=pub_dt,
                symbols=symbols,
                reliability=0.72,
                category=_category_from_tags(tags, default="crypto"),
            ))

        logger.info(f"📰 gnews: {len(items)} articles")
        return items

    except Exception as e:
        logger.warning(f"GNews: {e}")
        return []


# ────────────────────────────────────────────────────────────
# Currents API
# ────────────────────────────────────────────────────────────

async def fetch_currents_news(
    session: aiohttp.ClientSession,
    api_key: str,
    max_age_hours: int = 6,
) -> List[NewsItem]:
    """Fetch recent global news from Currents and keep crypto-relevant items."""
    if not api_key:
        return []

    cutoff = now_sast() - timedelta(hours=max_age_hours)

    try:
        async with session.get(
            "https://api.currentsapi.services/v1/search",
            params={
                "keywords": MULTI_MARKET_QUERY,
                "language": "en",
                "apiKey": api_key,
            },
            timeout=aiohttp.ClientTimeout(total=12),
        ) as resp:
            if resp.status != 200:
                logger.warning(f"Currents: HTTP {resp.status}")
                return []
            payload = await resp.json()

        items: List[NewsItem] = []
        for article in payload.get("news", [])[:30]:
            title = (article.get("title") or "").strip()
            description = (article.get("description") or "").strip()
            if not title:
                continue

            pub_dt = _parse_iso_datetime(article.get("published", ""))
            if not pub_dt or pub_dt < cutoff:
                continue

            text = f"{title}. {description}" if description else title
            symbols, tags = extract_symbols(text)
            symbols = _merge_symbols_and_tags(symbols, tags)

            items.append(NewsItem(
                title=title,
                summary=(description or title)[:500],
                source="currents",
                url=article.get("url", ""),
                published_at=pub_dt,
                symbols=symbols,
                reliability=0.70,
                category=_category_from_tags(tags, default="crypto"),
            ))

        logger.info(f"📰 currents: {len(items)} articles")
        return items

    except Exception as e:
        logger.warning(f"Currents: {e}")
        return []


# ────────────────────────────────────────────────────────────
# Florida Man API (public, no key)
# ────────────────────────────────────────────────────────────

async def fetch_florida_man_news(
    session: aiohttp.ClientSession,
    max_age_hours: int = 24,
) -> List[NewsItem]:
    """Fetch topical headlines from the Florida Man open dataset."""
    try:
        now = now_sast()
        day_count = 2 if max_age_hours > 24 else 1
        days = [now - timedelta(days=offset) for offset in range(day_count)]
        all_items: List[NewsItem] = []

        for day in days:
            endpoint = (
                "https://juliayxhuang.github.io/florida-man-api/"
                f"api/{day:%m}/{day:%d}.json"
            )
            async with session.get(endpoint, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    logger.warning(f"FloridaMan API ({day:%m-%d}): HTTP {resp.status}")
                    continue
                payload = await resp.json()

            if not isinstance(payload, list):
                continue

            for article in payload[:20]:
                title = (article.get("title") or "").strip()
                if not title:
                    continue

                original_date = (article.get("date") or "").strip()
                keywords = article.get("keywords") or []
                keyword_text = " ".join(k for k in keywords if isinstance(k, str))
                text = f"{title}. {keyword_text}".strip()

                symbols, tags = extract_symbols(text)
                merged_symbols = _merge_symbols_and_tags(
                    symbols,
                    tags,
                    include_market_fallback=False,
                )

                summary_parts = ["Florida Man dataset headline"]
                if original_date:
                    summary_parts.append(f"original_date={original_date}")
                if keyword_text:
                    summary_parts.append(f"keywords={keyword_text[:180]}")

                all_items.append(NewsItem(
                    title=title,
                    summary=" | ".join(summary_parts)[:500],
                    source="florida_man",
                    url=article.get("url", ""),
                    # Florida Man is day-of-year indexed; treat fetch time as current context.
                    published_at=now,
                    symbols=merged_symbols,
                    reliability=0.20,
                    category=_category_from_tags(tags, default="oddities"),
                ))

        logger.info(f"📰 florida_man: {len(all_items)} headlines")
        return all_items

    except Exception as e:
        logger.warning(f"FloridaMan API: {e}")
        return []


# ────────────────────────────────────────────────────────────
# Master Fetcher — All Sources in Parallel
# ────────────────────────────────────────────────────────────

async def fetch_all_news(
    cryptopanic_api_key: str = "",
    coingecko_api_key: str = "",
    coinmarketcap_api_key: str = "",
    marketaux_api_key: str = "",
    gnews_api_key: str = "",
    currents_api_key: str = "",
    max_age_hours: int = 6,
) -> List[NewsItem]:
    """
    Fetch news from ALL sources concurrently.
    Returns deduplicated list sorted by published date.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; TradeBotScraper/1.0)",
        "Accept": "text/html,application/xml,application/rss+xml,*/*",
    }
    async with aiohttp.ClientSession(headers=headers) as session:
        tasks = []
        source_tasks = 0

        # RSS feeds — all in parallel
        for name, config in RSS_SOURCES.items():
            tasks.append(_fetch_rss(name, config, session, max_age_hours))
            source_tasks += 1

        # Financial Juice
        tasks.append(fetch_financial_juice(session, max_age_hours))
        source_tasks += 1

        # CryptoPanic
        if cryptopanic_api_key:
            tasks.append(fetch_cryptopanic(session, cryptopanic_api_key, max_age_hours=max_age_hours))
            source_tasks += 1

        # CoinGecko trending + movers
        if coingecko_api_key:
            tasks.append(fetch_coingecko_news(session, coingecko_api_key))
            source_tasks += 1

        # CoinMarketCap news headlines
        if coinmarketcap_api_key:
            tasks.append(fetch_coinmarketcap_news(session, coinmarketcap_api_key))
            source_tasks += 1

        # MarketAux gives entity-tagged articles with sentiment fields.
        if marketaux_api_key:
            tasks.append(fetch_marketaux_news(session, marketaux_api_key, max_age_hours=max_age_hours))
            source_tasks += 1

        if gnews_api_key:
            tasks.append(fetch_gnews(session, gnews_api_key, max_age_hours=max_age_hours))
            source_tasks += 1

        if currents_api_key:
            tasks.append(fetch_currents_news(session, currents_api_key, max_age_hours=max_age_hours))
            source_tasks += 1

        # Florida Man API (public, no key)
        tasks.append(fetch_florida_man_news(session, max_age_hours=max_age_hours))
        source_tasks += 1

        # No-key market sentiment enrichers.
        tasks.append(fetch_alternative_fng(session, max_age_hours=max(max_age_hours, 24)))
        tasks.append(fetch_coincap_movers(session))
        source_tasks += 2

        results = await asyncio.gather(*tasks, return_exceptions=True)

    all_items: List[NewsItem] = []
    for result in results:
        if isinstance(result, list):
            all_items.extend(result)
        elif isinstance(result, Exception):
            logger.warning(f"News fetch error: {result}")

    # Deduplicate by title similarity (exact match on normalized title)
    seen_titles = set()
    unique: List[NewsItem] = []
    for item in all_items:
        normalized = re.sub(r"\s+", " ", item.title.lower().strip())
        if normalized not in seen_titles:
            seen_titles.add(normalized)
            unique.append(item)

    # Sort by recency
    unique.sort(key=lambda x: x.published_at or datetime.min, reverse=True)

    logger.info(
        f"📚 Total: {len(all_items)} raw → {len(unique)} unique articles "
        f"from {source_tasks} sources"
    )
    return unique
