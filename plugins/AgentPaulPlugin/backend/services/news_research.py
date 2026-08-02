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
import os
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


# ── General knowledge research (any subject, not just news) ──────────────────
# Google News answers "what happened", and nothing else. Ask it to explain
# integration by parts or the Krebs cycle and it returns whatever article
# happened to use those words this week — which is why non-current-events
# questions had no grounding at all. The sources below answer the other kind
# of question: reference lookups and general web results, no API key needed.

_REF_CACHE: dict[str, tuple[float, list[dict]]] = {}
_REF_TTL = 1800.0  # 30 min — encyclopedic facts don't churn like headlines


async def wiki_lookup(query: str, limit: int = 3) -> list[dict]:
    """Wikipedia intro extracts for a query — maths, science, history, people.

    One request: ``generator=search`` finds the articles and ``prop=extracts``
    returns their intros in the same response, so a lookup costs one round trip
    rather than one search plus N summary fetches.
    """
    q = (query or "").strip()
    if not q:
        return []
    key = f"wiki::{q.lower()}"
    now = asyncio.get_event_loop().time()
    cached = _REF_CACHE.get(key)
    if cached and now - cached[0] < _REF_TTL:
        return cached[1][:limit]

    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": q,
        "gsrlimit": str(max(1, min(limit, 5))),
        "prop": "extracts|info",
        "exintro": "1",
        "explaintext": "1",
        "exlimit": "5",
        "inprop": "url",
    }
    items: list[dict] = []
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://en.wikipedia.org/w/api.php",
                params=params, headers=_HEADERS, timeout=8.0, follow_redirects=True,
            )
        if resp.status_code == 200:
            pages = (resp.json().get("query") or {}).get("pages") or {}
            # The API returns pages keyed by id in arbitrary order; `index`
            # carries the search ranking, so sort by it to keep best-first.
            for page in sorted(pages.values(), key=lambda p: p.get("index", 99)):
                extract = re.sub(r"\s+", " ", page.get("extract") or "").strip()
                if not extract:
                    continue
                items.append({
                    "source": "Wikipedia",
                    "topic": "reference",
                    "title": page.get("title") or "",
                    "summary": extract[:900],
                    "url": page.get("fullurl") or "",
                    "sentiment": 0.0,
                    "published_at": None,
                })
    except Exception as exc:
        logger.debug(f"[RESEARCH] wiki '{q}' error: {exc}")

    _REF_CACHE[key] = (now, items)
    return items[:limit]


async def _brave_search(query: str, limit: int) -> list[dict]:
    """Real SERP via the Brave Search API, when a key is configured."""
    key = os.getenv("BRAVE_SEARCH_API_KEY", "").strip()
    if not key:
        return []
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": max(1, min(limit, 20))},
                headers={"Accept": "application/json", "X-Subscription-Token": key},
                timeout=9.0,
            )
        if resp.status_code != 200:
            logger.debug(f"[RESEARCH] brave HTTP {resp.status_code}")
            return []
        results = ((resp.json().get("web") or {}).get("results") or [])
        return [
            {
                "source": r.get("profile", {}).get("name") or "Brave",
                "topic": "web",
                "title": (r.get("title") or "")[:200],
                "summary": re.sub(r"<[^>]+>", "", r.get("description") or "")[:500],
                "url": r.get("url") or "",
                "sentiment": 0.0,
                "published_at": None,
            }
            for r in results[:limit]
        ]
    except Exception as exc:
        logger.debug(f"[RESEARCH] brave '{query}' error: {exc}")
        return []


async def _tavily_search(query: str, limit: int) -> list[dict]:
    """Real SERP via Tavily, when a key is configured."""
    key = os.getenv("TAVILY_API_KEY", "").strip()
    if not key:
        return []
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": key, "query": query,
                    "max_results": max(1, min(limit, 10)),
                    "search_depth": "basic",
                },
                timeout=12.0,
            )
        if resp.status_code != 200:
            logger.debug(f"[RESEARCH] tavily HTTP {resp.status_code}")
            return []
        return [
            {
                "source": (r.get("url") or "").split("/")[2] if "//" in (r.get("url") or "") else "Tavily",
                "topic": "web",
                "title": (r.get("title") or "")[:200],
                "summary": (r.get("content") or "")[:500],
                "url": r.get("url") or "",
                "sentiment": 0.0,
                "published_at": None,
            }
            for r in (resp.json().get("results") or [])[:limit]
        ]
    except Exception as exc:
        logger.debug(f"[RESEARCH] tavily '{query}' error: {exc}")
        return []


async def _ddg_instant_answer(query: str) -> list[dict]:
    """DuckDuckGo's Instant Answer abstract — keyless, good for definitions."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://api.duckduckgo.com/",
                params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
                headers=_HEADERS, timeout=8.0, follow_redirects=True,
            )
        # DDG answers this endpoint with 202, not 200, and labels the body
        # application/x-javascript — so check the class, not the exact code.
        if resp.status_code // 100 != 2:
            logger.debug(f"[RESEARCH] ddg instant HTTP {resp.status_code}")
            return []
        data = resp.json()
    except Exception as exc:
        logger.debug(f"[RESEARCH] ddg instant '{query}' error: {exc}")
        return []

    items: list[dict] = []
    abstract = (data.get("AbstractText") or "").strip()
    if abstract:
        items.append({
            "source": data.get("AbstractSource") or "DuckDuckGo",
            "topic": "reference",
            "title": data.get("Heading") or query,
            "summary": abstract[:900],
            "url": data.get("AbstractURL") or "",
            "sentiment": 0.0,
            "published_at": None,
        })
    answer = (data.get("Answer") or "").strip()
    if answer:
        items.append({
            "source": data.get("AnswerType") or "DuckDuckGo",
            "topic": "reference", "title": f"Direct answer: {query}"[:200],
            "summary": answer[:500], "url": data.get("AbstractURL") or "",
            "sentiment": 0.0, "published_at": None,
        })
    for rel in (data.get("RelatedTopics") or [])[:4]:
        text = (rel.get("Text") or "").strip() if isinstance(rel, dict) else ""
        if text:
            items.append({
                "source": "DuckDuckGo", "topic": "reference",
                "title": text[:90], "summary": text[:500],
                "url": rel.get("FirstURL") or "",
                "sentiment": 0.0, "published_at": None,
            })
    return items


# Which Stack Exchange site to ask, by subject. Maths and physics questions get
# far better answers from their own sites than from Stack Overflow.
_SE_SITES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(integral|derivative|matrix|theorem|proof|algebra|calculus|"
                r"equation|probability|topolog|geometr|number theory)\b", re.I), "math"),
    (re.compile(r"\b(quantum|relativity|thermodynamic|particle|photon|entropy|"
                r"momentum|physics|electromagnet)\b", re.I), "physics"),
    (re.compile(r"\b(molecule|reaction|compound|chemistry|acid|enzyme|bond)\b", re.I), "chemistry"),
    (re.compile(r"\b(dna|protein|cell|gene|biolog|organism|evolution|neuron)\b", re.I), "biology"),
    (re.compile(r"\b(statistic|regression|bayes|p.value|distribution|variance)\b", re.I), "stats"),
]


async def qa_search(query: str, limit: int = 3) -> list[dict]:
    """Stack Exchange search — worked answers for technical questions.

    Wikipedia explains what a thing *is*; these sites explain how to *do* it,
    which is what "how do I solve this integral" actually needs.
    """
    q = (query or "").strip()
    if not q:
        return []
    site = "stackoverflow"
    for pattern, se_site in _SE_SITES:
        if pattern.search(q):
            site = se_site
            break

    key = f"qa::{site}::{q.lower()}"
    now = asyncio.get_event_loop().time()
    cached = _REF_CACHE.get(key)
    if cached and now - cached[0] < _REF_TTL:
        return cached[1][:limit]

    items: list[dict] = []
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://api.stackexchange.com/2.3/search/advanced",
                params={
                    "order": "desc", "sort": "relevance", "q": q,
                    "site": site, "pagesize": str(max(1, min(limit, 5))),
                    "filter": "!nNPvSNdWme", "answers": "1",
                },
                headers=_HEADERS, timeout=9.0, follow_redirects=True,
            )
        if resp.status_code == 200:
            for row in (resp.json().get("items") or [])[:limit]:
                title = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), row.get("title") or "")
                items.append({
                    "source": f"{site}.stackexchange",
                    "topic": "qa",
                    "title": title[:200],
                    "summary": (
                        f"score {row.get('score', 0)}, {row.get('answer_count', 0)} answers"
                        f"{', accepted' if row.get('is_answered') else ''}"
                    ),
                    "url": row.get("link") or "",
                    "sentiment": 0.0,
                    "published_at": None,
                })
    except Exception as exc:
        logger.debug(f"[RESEARCH] stackexchange '{q}' error: {exc}")

    _REF_CACHE[key] = (now, items)
    return items[:limit]


async def general_web_search(query: str, limit: int = 6) -> list[dict]:
    """Open-web search for ANY subject.

    Prefers a real SERP when ``BRAVE_SEARCH_API_KEY`` or ``TAVILY_API_KEY`` is
    set, since a keyed API returns ranked pages across the whole web. Without a
    key it falls back to DuckDuckGo's Instant Answer API — narrower, but it is
    a documented endpoint that answers reliably, unlike scraping a results page
    (both DDG Lite and Mojeek serve bot challenges to server-side callers, and
    working around those is not something this should attempt).
    """
    q = (query or "").strip()
    if not q:
        return []
    key = f"web::{q.lower()}"
    now = asyncio.get_event_loop().time()
    cached = _REF_CACHE.get(key)
    if cached and now - cached[0] < _REF_TTL:
        return cached[1][:limit]

    items = await _brave_search(q, limit)
    if not items:
        items = await _tavily_search(q, limit)
    if not items:
        items = await _ddg_instant_answer(q)

    # De-dup by URL, keeping the earlier (higher-ranked) entry.
    seen: set[str] = set()
    deduped: list[dict] = []
    for it in items:
        marker = (it.get("url") or it.get("title") or "").lower()
        if marker and marker in seen:
            continue
        seen.add(marker)
        deduped.append(it)

    _REF_CACHE[key] = (now, deduped)
    return deduped[:limit]


async def agent_reach_web_search(query: str, limit: int = 6) -> list[dict]:
    """Extra web-search source via Agent-Reach's Exa/mcporter channel.

    Off (returns []) unless AGENT_REACH_ENABLED is set — see
    backend/app/services/agent_reach_client.py for the client itself. Cached
    alongside the other reference sources.
    """
    q = (query or "").strip()
    if not q:
        return []
    from app.core.config import settings
    if not settings.AGENT_REACH_ENABLED:
        return []

    key = f"agentreach::{q.lower()}"
    now = asyncio.get_event_loop().time()
    cached = _REF_CACHE.get(key)
    if cached and now - cached[0] < _REF_TTL:
        return cached[1][:limit]

    from app.services import agent_reach_client
    items = await agent_reach_client.web_search(q, limit=limit) or []
    _REF_CACHE[key] = (now, items)
    return items[:limit]


# Words that mean "tell me what happened", as opposed to "explain this to me".
_CURRENT_EVENTS_RE = re.compile(
    r"\b(news|latest|breaking|today|yesterday|this (?:week|month|morning)|"
    r"right now|currently|update|happening|happened|price|stock|market|election|"
    r"score|won|winner|died|announced|released|launch(?:ed)?|recent|2\d{3})\b",
    re.IGNORECASE,
)


def is_current_events(query: str) -> bool:
    """True when a question is about the present rather than settled knowledge."""
    return bool(_CURRENT_EVENTS_RE.search(query or ""))


async def research(query: str, *, limit: int = 6) -> dict[str, list[dict]]:
    """Search every source at once and return them separately.

    News, open web, encyclopedia and Q&A run concurrently — the classifier only
    decides emphasis, never exclusion, because "what is the Fed doing about
    inflation" is legitimately both a current-events and a reference question,
    and picking one source for it would drop half the answer.

    ``return_exceptions=True`` matters here: one source being down (rate limit,
    DNS blip) must degrade the answer, not fail the whole chat turn.
    """
    q = (query or "").strip()
    if len(q) < 3:
        return {"news": [], "web": [], "reference": [], "qa": []}

    current = is_current_events(q)
    news, web, agent_reach_web, wiki, qa = await asyncio.gather(
        web_news_search(q, limit=limit if current else 3),
        general_web_search(q, limit=limit),
        agent_reach_web_search(q, limit=limit),
        wiki_lookup(q, limit=2 if current else 3),
        qa_search(q, limit=2 if current else 3),
        return_exceptions=True,
    )

    def _ok(result) -> list[dict]:
        if isinstance(result, list):
            return result
        logger.debug(f"[RESEARCH] source failed for '{q}': {result}")
        return []

    # Agent-Reach results join the same "web" bucket as the brave/tavily/ddg
    # cascade — no-op (returns []) unless AGENT_REACH_ENABLED is set, so
    # research()'s output is unchanged until a user opts in.
    web_combined = _ok(web) + _ok(agent_reach_web)

    return {
        "news": _ok(news), "web": web_combined,
        "reference": _ok(wiki), "qa": _ok(qa),
    }


def format_research_block(query: str, results: dict[str, list[dict]]) -> str:
    """Render research results as a prompt section. Empty string if nothing hit."""
    sections: list[tuple[str, str, list[dict]]] = [
        ("reference", "Reference (Wikipedia / encyclopedic)", results.get("reference") or []),
        ("web", "Live Web Search Results", results.get("web") or []),
        ("qa", "Expert Q&A (Stack Exchange — worked solutions)", results.get("qa") or []),
        ("news", "Live News (most recent first)", results.get("news") or []),
    ]
    if not any(items for _, _, items in sections):
        return ""

    out = [
        "\n## LIVE RESEARCH FOR THIS QUESTION",
        f"Searched the internet just now for: {query[:160]!r}",
        "These are real, current sources. Answer from them and cite them. "
        "You are NOT working from stale training data — do not say you lack "
        "internet access or cannot look something up.",
    ]
    for _, heading, items in sections:
        if not items:
            continue
        out.append(f"\n### {heading}")
        for item in items[:6]:
            when = item.get("published_at")
            when_s = f" {when.strftime('%Y-%m-%d %H:%M')}" if when else ""
            summary = f" — {item['summary']}" if item.get("summary") else ""
            url = f"\n    {item['url']}" if item.get("url") else ""
            out.append(
                f"  - [{item.get('source', '?')}]{when_s} {item.get('title', '')}"
                f"{summary}{url}"
            )
    return "\n".join(out)

