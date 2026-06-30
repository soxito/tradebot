"""
Agent Paul — News & Online Research

Gives JARVIS access to the public internet:
  * Aggregates a curated set of public RSS news feeds (crypto, stocks, macro).
  * Fetches arbitrary URLs for ad-hoc online research.
  * Scores headline sentiment (VADER if available, else a keyword fallback).

Ingested headlines are written into PaulKnowledge (kind='news') so the
assistant accumulates market knowledge over time and can ground predictions.

Everything is best-effort and short-timeout so a slow feed never blocks chat.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Optional

import httpx
from loguru import logger

try:
    import feedparser  # type: ignore
    _HAS_FEEDPARSER = True
except Exception:  # pragma: no cover
    _HAS_FEEDPARSER = False

try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer  # type: ignore
    _VADER = SentimentIntensityAnalyzer()
except Exception:  # pragma: no cover
    _VADER = None


# ── Curated public RSS feeds ───────────────────────────────
# All public, no auth required.
RSS_FEEDS: list[dict] = [
    # Crypto
    {"name": "CoinDesk", "topic": "crypto", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
    {"name": "Cointelegraph", "topic": "crypto", "url": "https://cointelegraph.com/rss"},
    {"name": "Decrypt", "topic": "crypto", "url": "https://decrypt.co/feed"},
    {"name": "Bitcoin Magazine", "topic": "crypto", "url": "https://bitcoinmagazine.com/feed"},
    {"name": "CryptoSlate", "topic": "crypto", "url": "https://cryptoslate.com/feed/"},
    # Stocks / markets
    {"name": "Yahoo Finance", "topic": "stocks", "url": "https://finance.yahoo.com/news/rssindex"},
    {"name": "CNBC Markets", "topic": "stocks", "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839069"},
    {"name": "MarketWatch", "topic": "stocks", "url": "https://feeds.marketwatch.com/marketwatch/topstories/"},
    {"name": "Investing.com", "topic": "stocks", "url": "https://www.investing.com/rss/news_25.rss"},
    # Macro / world
    {"name": "Reuters Business", "topic": "macro", "url": "https://www.reutersagency.com/feed/?best-topics=business-finance&post_type=best"},
    {"name": "CNBC Economy", "topic": "macro", "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"},
]

_HEADERS = {"User-Agent": "Mozilla/5.0 (TradeBot JARVIS NewsBot)"}

# In-process cache: {topic|all: (timestamp, items)}
_CACHE: dict[str, tuple[float, list[dict]]] = {}
_CACHE_TTL = 300.0  # 5 minutes


# Positive / negative keyword fallback (used if VADER missing)
_POS = {"surge", "rally", "soar", "gain", "bullish", "record", "jump", "rise",
        "boom", "outperform", "upgrade", "breakout", "adoption", "approval"}
_NEG = {"crash", "plunge", "drop", "fall", "bearish", "selloff", "fear", "ban",
        "hack", "lawsuit", "downgrade", "slump", "liquidation", "warning", "risk"}


def score_sentiment(text: str) -> float:
    """Return sentiment in [-1, 1]."""
    if not text:
        return 0.0
    if _VADER is not None:
        try:
            return float(_VADER.polarity_scores(text)["compound"])
        except Exception:
            pass
    low = text.lower()
    pos = sum(1 for w in _POS if w in low)
    neg = sum(1 for w in _NEG if w in low)
    if pos == neg == 0:
        return 0.0
    return max(-1.0, min(1.0, (pos - neg) / float(pos + neg)))


async def _fetch_feed(client: httpx.AsyncClient, feed: dict) -> list[dict]:
    """Fetch and parse one RSS feed → list of normalized items."""
    if not _HAS_FEEDPARSER:
        return []
    try:
        resp = await client.get(feed["url"], headers=_HEADERS, timeout=8.0, follow_redirects=True)
        if resp.status_code != 200:
            return []
        parsed = feedparser.parse(resp.content)
        items: list[dict] = []
        for entry in parsed.entries[:12]:
            title = getattr(entry, "title", "") or ""
            summary = re.sub(r"<[^>]+>", "", getattr(entry, "summary", "") or "")[:400]
            link = getattr(entry, "link", "") or ""
            published = None
            if getattr(entry, "published_parsed", None):
                try:
                    published = datetime(*entry.published_parsed[:6])
                except Exception:
                    published = None
            items.append({
                "source": feed["name"],
                "topic": feed["topic"],
                "title": title.strip(),
                "summary": summary.strip(),
                "url": link,
                "sentiment": score_sentiment(f"{title} {summary}"),
                "published_at": published,
            })
        return items
    except Exception as exc:
        logger.debug(f"[NEWS] feed {feed['name']} error: {exc}")
        return []


async def fetch_news(topic: Optional[str] = None, force: bool = False) -> list[dict]:
    """Aggregate news across feeds (optionally filtered by topic). Cached 5 min."""
    cache_key = topic or "all"
    now = asyncio.get_event_loop().time()
    if not force and cache_key in _CACHE:
        ts, items = _CACHE[cache_key]
        if now - ts < _CACHE_TTL:
            return items

    feeds = [f for f in RSS_FEEDS if (topic is None or f["topic"] == topic)]
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*[_fetch_feed(client, f) for f in feeds])
    items: list[dict] = [it for sub in results for it in sub]
    # Sort newest-ish first (published desc, fallback keeps feed order)
    items.sort(key=lambda x: x.get("published_at") or datetime.min, reverse=True)
    _CACHE[cache_key] = (now, items)
    return items


# Common symbol → keyword aliases for relevance filtering
_SYMBOL_ALIASES = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "ether", "eth"],
    "SOL": ["solana", "sol"],
    "XRP": ["ripple", "xrp"],
    "DOGE": ["dogecoin", "doge"],
    "ADA": ["cardano", "ada"],
    "XAU": ["gold", "xau"],
    "EUR": ["euro", "eur"],
    "USD": ["dollar", "usd", "fed", "federal reserve"],
    "GBP": ["pound", "sterling", "gbp"],
    "JPY": ["yen", "jpy", "boj"],
    "NAS": ["nasdaq"],
    "SPX": ["s&p", "sp500", "s&p 500"],
}


def _aliases_for(pair: str) -> list[str]:
    pair = (pair or "").upper()
    out: list[str] = []
    for base, words in _SYMBOL_ALIASES.items():
        if base in pair:
            out.extend(words)
    if not out:
        # fall back to raw tokens
        out = [t.lower() for t in re.split(r"[^A-Za-z0-9]", pair) if len(t) > 2]
    return out


async def news_for_symbol(pair: str, limit: int = 8) -> list[dict]:
    """Return news items relevant to a trading pair."""
    aliases = _aliases_for(pair)
    items = await fetch_news()
    relevant = [
        it for it in items
        if any(a in (it["title"] + " " + it["summary"]).lower() for a in aliases)
    ]
    return relevant[:limit]


async def research_url(url: str, max_chars: int = 4000) -> dict:
    """Fetch a public URL and return cleaned text for online research."""
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "Only http(s) URLs allowed."}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=_HEADERS, timeout=10.0, follow_redirects=True)
        if resp.status_code != 200:
            return {"ok": False, "error": f"HTTP {resp.status_code}"}
        html = resp.text
        # Strip scripts/styles then tags
        html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        return {"ok": True, "url": url, "text": text[:max_chars], "sentiment": score_sentiment(text[:2000])}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# In-process cache for live web-news searches: {query: (ts, items)}
_SEARCH_CACHE: dict[str, tuple[float, list[dict]]] = {}
_SEARCH_TTL = 180.0  # 3 minutes


async def web_news_search(query: str, limit: int = 8) -> list[dict]:
    """
    Live web news search for ANY topic (geopolitics, sports, tech, weather,
    a person, a company — anything), via Google News RSS. Public, no API key.

    Returns the most recent headlines + summaries so JARVIS can answer
    current-events questions instead of refusing. Best-effort, short timeout.
    """
    q = (query or "").strip()
    if not q or not _HAS_FEEDPARSER:
        return []
    key = q.lower()
    now = asyncio.get_event_loop().time()
    cached = _SEARCH_CACHE.get(key)
    if cached and now - cached[0] < _SEARCH_TTL:
        return cached[1][:limit]

    from urllib.parse import quote_plus
    url = f"https://news.google.com/rss/search?q={quote_plus(q)}&hl=en-US&gl=US&ceid=US:en"
    items: list[dict] = []
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=_HEADERS, timeout=8.0, follow_redirects=True)
        if resp.status_code == 200:
            parsed = feedparser.parse(resp.content)
            for entry in parsed.entries[: max(limit, 8)]:
                title = (getattr(entry, "title", "") or "").strip()
                summary = re.sub(r"<[^>]+>", "", getattr(entry, "summary", "") or "")[:300].strip()
                link = getattr(entry, "link", "") or ""
                source = ""
                try:
                    source = getattr(entry, "source", {}).get("title", "") or ""  # type: ignore
                except Exception:
                    source = ""
                published = None
                if getattr(entry, "published_parsed", None):
                    try:
                        published = datetime(*entry.published_parsed[:6])
                    except Exception:
                        published = None
                items.append({
                    "source": source or "Google News",
                    "topic": "web",
                    "title": title,
                    "summary": summary,
                    "url": link,
                    "sentiment": score_sentiment(f"{title} {summary}"),
                    "published_at": published,
                })
    except Exception as exc:
        logger.debug(f"[NEWS] web search '{q}' error: {exc}")

    _SEARCH_CACHE[key] = (now, items)
    return items[:limit]

