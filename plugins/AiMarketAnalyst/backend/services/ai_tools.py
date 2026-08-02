"""Tools the models may call to reach live data.

Why tools and not just context
------------------------------
Everything the assistant knew used to be pre-fetched and stuffed into the
prompt.  That works only for questions you anticipated: ask about an instrument
the builder didn't think to fetch and the model has nothing, and — because the
prompt told it never to quote an unlisted price — it refused.  Pre-fetching is
still the baseline (it works on every provider, including the ones with no tool
support), but a model that can *ask* for a price stops being limited to what
was guessed in advance.

Every tool returns a plain string, and never raises.  A failure is returned as
``ERROR: …`` so the model reads it as a fact about the world and can retry or
tell the user, rather than the whole turn collapsing into a 500.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
from typing import Any, Dict, List
from urllib.parse import urlparse

from loguru import logger

#: Tool output is capped so a chatty tool can't crowd the real conversation out
#: of the context window.  analyze_symbol gets more room — its value is detail.
#: web_search gets more again: it merges four sources, and a Wikipedia intro
#: that explains the actual concept is worth more room than a price line.
_MAX_RESULT_CHARS = 1_500
_MAX_ANALYSIS_CHARS = 2_500
_MAX_RESEARCH_CHARS = 4_000


TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "price_lookup",
            "description": (
                "Get the live price of ANY tradeable instrument: crypto "
                "(BTCUSDT, ETHUSDT), FX majors and crosses (GBPUSD, EURJPY), "
                "metals (XAUUSD gold, XAGUSD silver, XPTUSD), stock indices "
                "(US30, NAS100, US500, GER40, UK100, JPN225), energy (USOIL, "
                "UKOIL, NGAS) and softs (COCOA, COFFEE, WHEAT). Use this "
                "whenever you need a current price you have not been given. "
                "Never guess a price and never ask the user to supply one."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbols": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Instrument symbols, e.g. ['XAUUSD', 'GBPUSD'].",
                    }
                },
                "required": ["symbols"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the live internet about ANY subject whatsoever — "
                "mathematics, physics, chemistry, biology, medicine, history, "
                "law, philosophy, programming, geography, sport, a person, a "
                "company, current events, markets. Searches news, the open web, "
                "Wikipedia and expert Q&A sites together and returns real "
                "sources. Use it for anything recent, anything factual you want "
                "to verify, and anything you are less than certain about. "
                "Prefer calling this over answering from memory."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to search for."},
                    "limit": {
                        "type": "integer",
                        "description": "Max results per source (default 6, max 10).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": (
                "Save a fact, correction, preference or lesson to permanent "
                "long-term memory so you still know it in every future "
                "conversation, including after a restart. Use it whenever the "
                "user tells you something about themselves, corrects you, or "
                "you work out something worth keeping — in ANY subject, not "
                "just trading."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The fact to remember, stated so it makes sense on its own.",
                    },
                    "topic": {
                        "type": "string",
                        "description": "Subject area, e.g. 'mathematics', 'user-preference', 'trading'.",
                    },
                    "importance": {
                        "type": "number",
                        "description": "0.0–1.0. Use 0.9 for user corrections and standing preferences.",
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall",
            "description": (
                "Search your permanent long-term memory for what you have "
                "previously learned or been told about a subject. Use it before "
                "saying you do not know something, and whenever the user refers "
                "to an earlier conversation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to look up in memory."},
                    "limit": {
                        "type": "integer",
                        "description": "Max memories to return (default 6, max 15).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": (
                "Fetch and read a public web page as plain text. Use it to read "
                "an article a web_search result pointed at."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Public http(s) URL."}
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "youtube_research",
            "description": (
                "Fetch metadata (title, channel, description) for a YouTube "
                "video URL — market analysis videos, interviews, project "
                "announcements. Use it when the user shares or asks about a "
                "specific YouTube video."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "video_url": {
                        "type": "string",
                        "description": "A youtube.com or youtu.be video URL.",
                    }
                },
                "required": ["video_url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_activity",
            "description": (
                "Look up a GitHub repository's activity — description, stars, "
                "primary language, last push — or search repos by keyword. Use "
                "it to gauge real developer activity behind a crypto project or "
                "open-source tool, not just its price or hype."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "For mode='view': 'owner/repo' (e.g. 'bitcoin/bitcoin'). "
                            "For mode='search': free-text keywords."
                        ),
                    },
                    "mode": {
                        "type": "string",
                        "description": "'view' (default) for one repo, or 'search'.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_symbol",
            "description": (
                "Run full technical analysis on an instrument — trend, RSI, "
                "EMA50/200, ATR, swing levels and a proposed entry/stop/target. "
                "Works for crypto, FX, metals, indices and commodities. Use it "
                "when the user wants a trade idea or a read on an instrument, "
                "not just its price. This NEVER places an order."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "e.g. 'XAUUSD'."},
                    "timeframe": {
                        "type": "string",
                        "description": "1h, 4h or 1d (default 4h).",
                    },
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forecast_symbol",
            "description": (
                "Run the platform's Kronos ML forecasting engine on an "
                "instrument and get its predicted direction, % move, target "
                "price and confidence. THIS is how this platform predicts a "
                "price — when the user asks whether an instrument will rise or "
                "fall, what its target is, or how you would forecast it, call "
                "this and answer with the numbers instead of describing "
                "forecasting methods in the abstract. Works for crypto, FX, "
                "metals, indices and energy. This NEVER places an order."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "e.g. 'BTCUSDT'."},
                    "timeframe": {
                        "type": "string",
                        "description": "1m–1w candle size (default 1h).",
                    },
                },
                "required": ["symbol"],
            },
        },
    },
]

TOOL_NAMES = {t["function"]["name"] for t in TOOL_SCHEMAS}


# ── Text-directive fallback ──────────────────────────────────────────────────
# Several providers in the catalog are aggregator proxies with no tool support.
# Rather than leaving those models unable to fetch anything, they get the same
# capability through a text protocol: emit a directive, we execute it, and feed
# the result back for one more turn.

TOOL_DIRECTIVE_PROMPT = """
## Fetching live data and using your memory

You can reach the internet and your own permanent memory by emitting a
directive on its own line, exactly:

<<TOOL: price_lookup {"symbols": ["XAUUSD", "GBPUSD"]}>>
<<TOOL: web_search {"query": "how does CRISPR gene editing work", "limit": 6}>>
<<TOOL: recall {"query": "what the user told me about their risk limits"}>>
<<TOOL: remember {"content": "User prefers metric units.", "topic": "user-preference", "importance": 0.9}>>
<<TOOL: fetch_url {"url": "https://example.com/article"}>>
<<TOOL: youtube_research {"video_url": "https://www.youtube.com/watch?v=..."}>>
<<TOOL: github_activity {"query": "bitcoin/bitcoin", "mode": "view"}>>
<<TOOL: analyze_symbol {"symbol": "XAUUSD", "timeframe": "4h"}>>
<<TOOL: forecast_symbol {"symbol": "BTCUSDT", "timeframe": "1h"}>>

Emit directives ALONE, with no other text, and stop. The results come back in
the next message and you answer from them.

`web_search` works for EVERY subject on earth — mathematics, physics, biology,
medicine, history, law, philosophy, programming, geography, sport, current
events — not just markets. Use it whenever you need a fact you were not given
or are less than certain about. Never guess, and never tell the user you lack
internet access or that a topic is outside what you can look up: you have live
search for all of it.
""".strip()

_DIRECTIVE_RE = re.compile(r"<<TOOL:\s*(\w+)\s*(\{.*?\})\s*>>", re.DOTALL)


def parse_text_directives(content: str) -> List[Dict[str, Any]]:
    """Pull ``<<TOOL: name {...}>>`` directives out of a model reply.

    Malformed JSON is skipped rather than raised: a model that half-writes a
    directive should get the tools that *did* parse, not an error for all.
    """
    if not content:
        return []
    out: List[Dict[str, Any]] = []
    for name, raw_args in _DIRECTIVE_RE.findall(content):
        if name not in TOOL_NAMES:
            continue
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError:
            logger.debug("[AITools] skipping malformed directive for {}", name)
            continue
        if isinstance(args, dict):
            out.append({"name": name, "arguments": args})
    return out


def strip_directives(content: str) -> str:
    """Reply text with the directives removed, for display."""
    return _DIRECTIVE_RE.sub("", content or "").strip()


# ── URL safety ───────────────────────────────────────────────────────────────

_ALLOWED_PORTS = {80, 443, None}


def _url_is_safe(url: str) -> tuple[bool, str]:
    """Reject anything that could reach the private network.

    The model chooses this URL, and it often chooses it from a web page it just
    read — so the input is attacker-influenced by construction. Without this
    check a crafted page could point the tool at cloud metadata (169.254.169.254)
    or an internal admin service and have the contents read back into the chat.
    """
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return False, "unparseable URL"

    if parsed.scheme not in ("http", "https"):
        return False, f"scheme {parsed.scheme!r} not allowed (http/https only)"
    if not parsed.hostname:
        return False, "no host in URL"
    if parsed.port not in _ALLOWED_PORTS:
        return False, f"port {parsed.port} not allowed"

    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        return False, f"cannot resolve {parsed.hostname}"

    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        ):
            return False, f"{parsed.hostname} resolves to non-public address {ip}"
    return True, ""


# ── Execution ────────────────────────────────────────────────────────────────

def _truncate(text: str, limit: int = _MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [truncated at {limit} chars]"


async def _run_price_lookup(args: Dict[str, Any]) -> str:
    from app.services import market_data

    symbols = args.get("symbols") or []
    if isinstance(symbols, str):
        symbols = [symbols]
    if not symbols:
        return "ERROR: price_lookup needs at least one symbol."

    quotes = await market_data.get_quotes([str(s) for s in symbols[:10]])
    if not quotes:
        return (
            "ERROR: the price fetch failed for "
            f"{', '.join(str(s) for s in symbols)}. This is a transient failure, "
            "not a missing capability — say the fetch failed and offer to retry. "
            "Do NOT state that live prices are unavailable on this platform."
        )

    lines = []
    for sym, q in quotes.items():
        change = f" ({q.change_pct:+.2f}%/24h)" if q.change_pct is not None else ""
        age = f", {q.age_s}s old" if q.age_s >= 5 else ""
        lines.append(
            f"{sym} = {q.price:,.6g}{change} [{q.asset_class}, source {q.source}{age}]"
        )
    missing = [s for s in symbols if market_data.normalize_symbol(str(s)) not in quotes]
    if missing:
        lines.append(f"(fetch failed for: {', '.join(str(m) for m in missing)} — retry)")
    return _truncate("\n".join(lines))


async def _run_web_search(args: Dict[str, Any]) -> str:
    """Search every source at once — news, open web, encyclopedia, expert Q&A.

    This used to be news-only, which meant a question like "explain integration
    by parts" came back with whatever article happened to use those words this
    week. Reference and Q&A sources are what make non-current-events questions
    answerable at all.
    """
    from plugins.AgentPaulPlugin.backend.services import news_research

    query = str(args.get("query") or "").strip()
    if not query:
        return "ERROR: web_search needs a query."
    limit = max(1, min(int(args.get("limit") or 6), 10))

    results = await news_research.research(query, limit=limit)
    if not any(results.values()):
        return (
            f"No results for {query!r} right now — the search returned nothing. "
            "This is a transient failure, not a missing capability: say the "
            "search came back empty and answer from your own knowledge instead."
        )

    lines: List[str] = []
    for label, key in (
        ("REFERENCE", "reference"), ("WEB", "web"),
        ("EXPERT Q&A", "qa"), ("NEWS", "news"),
    ):
        for item in (results.get(key) or [])[:limit]:
            when = item.get("published_at")
            when_s = f" {when.strftime('%Y-%m-%d %H:%M')}" if when else ""
            summary = f" — {item['summary']}" if item.get("summary") else ""
            lines.append(
                f"[{label}: {item.get('source', '?')}]{when_s} "
                f"{item.get('title', '')}{summary}\n  {item.get('url', '')}"
            )
    return _truncate("\n".join(lines), _MAX_RESEARCH_CHARS)


async def _run_remember(args: Dict[str, Any]) -> str:
    """Persist a fact to long-term memory.

    Opens its own session rather than taking one: tool calls run concurrently
    inside the chat turn, and sharing the request's AsyncSession across them
    would interleave writes on a connection that is not safe for it.
    """
    from app.core.database import AsyncSessionLocal
    from plugins.AgentPaulPlugin.backend.services import knowledge_base

    content = str(args.get("content") or "").strip()
    if not content:
        return "ERROR: remember needs content."
    topic = str(args.get("topic") or "general").strip() or "general"
    try:
        importance = max(0.0, min(float(args.get("importance") or 0.8), 1.0))
    except (TypeError, ValueError):
        importance = 0.8

    async with AsyncSessionLocal() as session:
        await knowledge_base.record_knowledge(
            session, kind="insight", content=content[:2000],
            source="jarvis-memory", topic=topic, importance=importance,
        )
    logger.info("[AITools] remembered ({}): {}", topic, content[:80])
    return f"Saved to permanent memory under topic {topic!r}: {content[:200]}"


async def _run_recall(args: Dict[str, Any]) -> str:
    from app.core.database import AsyncSessionLocal
    from plugins.AgentPaulPlugin.backend.services import knowledge_base

    query = str(args.get("query") or "").strip()
    if not query:
        return "ERROR: recall needs a query."
    limit = max(1, min(int(args.get("limit") or 6), 15))

    async with AsyncSessionLocal() as session:
        hits = await knowledge_base.search_knowledge(session, query, limit=limit)
    if not hits:
        return (
            f"Nothing in long-term memory about {query!r} yet. "
            "Answer from your own knowledge, and consider calling `remember` "
            "if the user tells you something worth keeping."
        )

    # The knowledge table also holds ingested RSS headlines, which vastly
    # outnumber real memories and match on any shared keyword. Something the
    # user actually told you outranks a news item that happened to say
    # "diagnostics", so sort by provenance before recency.
    _RANK = {"jarvis-memory": 0, "self-learning": 1, "chat": 2}
    hits.sort(key=lambda h: _RANK.get(h.get("source") or "", 9))

    lines = [
        f"[{h.get('source') or '?'}{'/' + h['symbol'] if h.get('symbol') else ''}] "
        f"{(h.get('ts') or '')[:10]} {h.get('content', '')[:300]}"
        for h in hits
    ]
    return _truncate("\n".join(lines))


async def _run_fetch_url(args: Dict[str, Any]) -> str:
    from app.services import agent_reach_client
    from plugins.AgentPaulPlugin.backend.services import news_research

    url = str(args.get("url") or "").strip()
    if not url:
        return "ERROR: fetch_url needs a url."

    safe, why = _url_is_safe(url)
    if not safe:
        logger.warning("[AITools] blocked fetch_url {}: {}", url, why)
        return f"ERROR: refused to fetch that URL ({why})."

    # Agent-Reach's Jina Reader passthrough gives cleaner markdown than the
    # regex-based tag-stripper below; fall back to it when unavailable.
    reached = await agent_reach_client.web_read(url, max_chars=_MAX_RESULT_CHARS)
    if reached:
        return _truncate(reached)

    result = await news_research.research_url(url, max_chars=_MAX_RESULT_CHARS)
    if not result.get("ok"):
        return f"ERROR: could not fetch {url} ({result.get('error')})."
    return _truncate(str(result.get("text") or ""))


async def _run_youtube_research(args: Dict[str, Any]) -> str:
    from app.services import agent_reach_client

    url = str(args.get("video_url") or "").strip()
    if not url:
        return "ERROR: youtube_research needs a video_url."

    info = await agent_reach_client.youtube_research(url)
    if not info:
        return (
            "ERROR: YouTube research is not available (Agent-Reach not "
            "installed/enabled, or the video could not be fetched)."
        )
    lines = [
        f"title: {info.get('title', '')}",
        f"channel: {info.get('channel', '')}",
    ]
    if info.get("upload_date"):
        lines.append(f"uploaded: {info['upload_date']}")
    if info.get("duration_s"):
        lines.append(f"duration: {int(info['duration_s'])}s")
    if info.get("description"):
        lines.append(f"description: {info['description']}")
    return _truncate("\n".join(lines))


async def _run_github_activity(args: Dict[str, Any]) -> str:
    from app.services import agent_reach_client

    query = str(args.get("query") or "").strip()
    if not query:
        return "ERROR: github_activity needs a query."
    mode = str(args.get("mode") or "view").strip().lower()
    if mode not in ("view", "search"):
        mode = "view"

    result = await agent_reach_client.github_activity(query, mode=mode)
    if not result:
        return (
            "ERROR: GitHub research is not available (Agent-Reach/gh CLI not "
            "installed/enabled, or nothing matched that query)."
        )
    return _truncate(json.dumps(result, indent=None)[:_MAX_RESULT_CHARS])


async def _run_analyze_symbol(args: Dict[str, Any]) -> str:
    from app.api.jarvis import _analysis_from_series
    from app.services import market_data

    symbol = market_data.normalize_symbol(str(args.get("symbol") or ""))
    if not symbol:
        return "ERROR: analyze_symbol needs a symbol."
    timeframe = str(args.get("timeframe") or "4h").lower()

    ohlcv, ticker = await market_data.fetch_ohlcv_universal(
        symbol, timeframe=timeframe, limit=200
    )
    if len(ohlcv) < 20:
        return (
            f"ERROR: not enough candle history for {symbol} on {timeframe}. "
            "Try a different timeframe, or use the /analyze command for crypto."
        )
    result = await _analysis_from_series(symbol, ohlcv, ticker)
    return _truncate(result.detail or result.speech or "", _MAX_ANALYSIS_CHARS)


async def _run_forecast_symbol(args: Dict[str, Any]) -> str:
    """Kronos forecast, flattened to text the model can quote verbatim."""
    from app.services import market_data

    symbol = market_data.normalize_symbol(str(args.get("symbol") or ""))
    if not symbol:
        return "ERROR: forecast_symbol needs a symbol."
    timeframe = str(args.get("timeframe") or "1h").lower()

    try:
        from plugins.KronosForecastPlugin.backend.services.forecast_service import (
            run_forecast_cached,
        )
    except ImportError as ie:
        return f"ERROR: the Kronos forecast engine is not installed ({ie})."

    resp = await run_forecast_cached("bitget", symbol, timeframe)
    sig = resp.signal
    lines = [
        f"Kronos forecast — {symbol} ({timeframe}), engine={resp.engine}",
        f"anchor price: {resp.anchor_price:.6g}",
    ]
    if sig:
        lines += [
            f"direction: {sig.direction}",
            f"predicted change: {sig.pct_change:+.2f}%",
            f"target price: {sig.target_price:.6g}",
            f"confidence: {sig.confidence:.0%}",
            f"decision: {sig.decision}",
        ]
        if sig.summary:
            lines.append(f"summary: {sig.summary}")
        if sig.rationale:
            lines.append("rationale: " + "; ".join(sig.rationale[:4]))
    else:
        # A NO_TRADE is a real answer — the volume gate refused, and the model
        # must report that rather than inventing a direction from the price.
        lines.append("no directional signal was emitted for this instrument.")
    if resp.note:
        lines.append(f"note: {resp.note}")

    # The macro read that shaped this confidence. Pulled from the rationale so
    # the model can quote it instead of describing forecasting in the abstract.
    macro_lines = [
        r for r in (getattr(sig, "rationale", None) or [])
        if "DXY" in r or "VIX" in r or r.startswith("Macro")
    ]
    if macro_lines:
        lines.append("macro context:")
        lines += [f"  - {line}" for line in macro_lines[:4]]

    return _truncate("\n".join(lines), _MAX_ANALYSIS_CHARS)


_HANDLERS = {
    "price_lookup": _run_price_lookup,
    "web_search": _run_web_search,
    "remember": _run_remember,
    "recall": _run_recall,
    "fetch_url": _run_fetch_url,
    "youtube_research": _run_youtube_research,
    "github_activity": _run_github_activity,
    "analyze_symbol": _run_analyze_symbol,
    "forecast_symbol": _run_forecast_symbol,
}


async def execute_tool(name: str, arguments: Dict[str, Any] | str) -> str:
    """Run one tool call and return its result as text. Never raises.

    A tool that blows up returns ``ERROR: …`` so the model treats it as
    information — it can retry, try another instrument, or tell the user
    plainly — instead of the exception killing the whole conversation turn.
    """
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return f"ERROR: arguments for {name} were not valid JSON."
    if not isinstance(arguments, dict):
        return f"ERROR: arguments for {name} must be an object."

    handler = _HANDLERS.get(name)
    if handler is None:
        return f"ERROR: unknown tool {name!r}. Available: {', '.join(sorted(TOOL_NAMES))}."

    try:
        return await handler(arguments)
    except Exception as exc:  # noqa: BLE001 — a tool failure is data, not a crash
        logger.warning("[AITools] {} failed: {}", name, exc)
        return f"ERROR: {name} failed ({exc}). This is a transient failure — retry."
