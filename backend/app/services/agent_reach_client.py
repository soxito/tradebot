"""
Agent-Reach research client — thin, best-effort wrapper around the upstream
tools that Agent-Reach (github.com/Panniantong/Agent-Reach) installs and
configures: Jina Reader (web read), Exa via mcporter (web search), yt-dlp
(YouTube) and the `gh` CLI (GitHub).

Why this shape
---------------
Agent-Reach is explicitly "not a wrapper" — it installs/configures upstream
tools and agents call them directly. So this client doesn't shell out to a
single `agent-reach` runtime binary; each channel shells out to whichever
upstream tool Agent-Reach's own documented usage shows for it. `AGENT_REACH_BIN`
still gates whether Agent-Reach itself is considered installed — a cheap first
check before any channel does real work — but each channel additionally
depends on its own upstream binary being present.

Every public function returns ``None`` (``[]``/``""`` for list/text-shaped
results) on any failure — flag off, binary missing, timeout, bad output — and
never raises, matching the "tools never raise" convention in
`plugins/AiMarketAnalyst/backend/services/ai_tools.py`. With
`AGENT_REACH_ENABLED=False` (the default), every function here returns
immediately with zero subprocess/network calls.

RSS is deliberately not wrapped here — `news_research.py` already talks to
`feedparser` in-process; shelling out for it would only add latency.

The exact CLI surface for `web_search` (mcporter/Exa) is Agent-Reach's
documented interface, not yet verified against a live install — nothing here
runs until a user installs Agent-Reach and sets `AGENT_REACH_ENABLED=True`.
If the real subcommand syntax differs, only `web_search` needs correcting.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import shutil
import socket
from typing import Any
from urllib.parse import urlparse

import httpx
from loguru import logger

from app.core.config import settings

_HEADERS = {"User-Agent": "Mozilla/5.0 (TradeBot Agent-Reach client)"}
_ALLOWED_PORTS = {80, 443, None}

# Short-TTL per-symbol cache so two agents researching the same symbol in the
# same analysis cycle (e.g. orchestrator Phase 1's market + sentiment
# analysts, run concurrently) collapse into one search call.
_SYMBOL_CACHE: dict[str, tuple[float, str]] = {}
_SYMBOL_CACHE_TTL = 60.0


def is_available() -> bool:
    """Cheap gate: feature flag + agent-reach CLI present. No subprocess call."""
    if not settings.AGENT_REACH_ENABLED:
        return False
    return shutil.which(settings.AGENT_REACH_BIN) is not None


def _url_is_safe(url: str) -> tuple[bool, str]:
    """Reject anything that could reach the private network.

    Duplicated from `ai_tools._url_is_safe` rather than imported: this module
    lives under `backend/app/services/`, which plugins import from — importing
    a plugin service here would invert that layering.
    """
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return False, "unparseable URL"
    if parsed.scheme not in ("http", "https"):
        return False, f"scheme {parsed.scheme!r} not allowed"
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


async def _run_cli(binary: str, args: list[str], timeout: float | None = None) -> tuple[bool, str]:
    """Run one upstream CLI tool as an argv list (never a shell string) and
    capture stdout. Never raises; a hung process is killed at the timeout.
    """
    exe = shutil.which(binary)
    if not exe:
        return False, f"{binary} not found on PATH"
    try:
        proc = await asyncio.create_subprocess_exec(
            exe, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout or settings.AGENT_REACH_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        return False, "timeout"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)

    if proc.returncode != 0:
        return False, (stderr or stdout).decode("utf-8", "replace")[:500]
    return True, stdout.decode("utf-8", "replace")


async def web_read(url: str, max_chars: int = 4000) -> str | None:
    """Read a public web page as clean text via Jina Reader.

    Jina Reader (r.jina.ai) is a public HTTPS passthrough, not a CLI tool, so
    this hits it directly rather than shelling out — one less moving part for
    a channel that's just an HTTP GET.
    """
    if not is_available():
        return None
    safe, why = _url_is_safe(url)
    if not safe:
        logger.warning("[AgentReach] refused web_read {}: {}", url, why)
        return None
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://r.jina.ai/{url}",
                headers=_HEADERS,
                timeout=settings.AGENT_REACH_TIMEOUT_S,
                follow_redirects=True,
            )
        if resp.status_code != 200:
            logger.debug("[AgentReach] web_read {} -> HTTP {}", url, resp.status_code)
            return None
        return resp.text[:max_chars]
    except Exception as exc:  # noqa: BLE001
        logger.debug("[AgentReach] web_read {} error: {}", url, exc)
        return None


async def web_search(query: str, limit: int = 6) -> list[dict[str, Any]] | None:
    """Semantic web search via Exa, routed through Agent-Reach's mcporter
    registration. Returns items shaped like news_research.py's result dicts
    so callers can merge them into the same rendering path.
    """
    if not is_available():
        return None
    q = (query or "").strip()
    if not q:
        return None
    ok, out = await _run_cli(
        "mcporter",
        ["call", "exa", "web_search", "--query", q, "--limit", str(max(1, min(limit, 10)))],
    )
    if not ok:
        logger.debug("[AgentReach] web_search {!r} failed: {}", q, out)
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        logger.debug("[AgentReach] web_search {!r} returned non-JSON output", q)
        return None
    results = data if isinstance(data, list) else (data.get("results") or [])
    items: list[dict[str, Any]] = []
    for r in results[:limit]:
        if not isinstance(r, dict):
            continue
        items.append({
            "source": r.get("source") or "Exa",
            "topic": "web",
            "title": (r.get("title") or "")[:200],
            "summary": (r.get("summary") or r.get("text") or "")[:500],
            "url": r.get("url") or "",
            "sentiment": 0.0,
            "published_at": None,
        })
    return items


async def youtube_research(url: str) -> dict[str, Any] | None:
    """Metadata (title, channel, description) for a YouTube video via yt-dlp."""
    if not is_available():
        return None
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        host = ""
    if not host.endswith(("youtube.com", "youtu.be")):
        logger.warning("[AgentReach] refused youtube_research: not a youtube host ({})", url)
        return None
    ok, out = await _run_cli("yt-dlp", ["--dump-json", "--no-warnings", url])
    if not ok:
        logger.debug("[AgentReach] youtube_research {} failed: {}", url, out)
        return None
    try:
        first_line = out.strip().splitlines()[0] if out.strip() else ""
        data = json.loads(first_line) if first_line else {}
    except (json.JSONDecodeError, IndexError):
        return None
    return {
        "title": data.get("title") or "",
        "channel": data.get("uploader") or data.get("channel") or "",
        "description": (data.get("description") or "")[:2000],
        "duration_s": data.get("duration"),
        "upload_date": data.get("upload_date"),
        "url": url,
    }


async def github_activity(
    query: str, mode: str = "view"
) -> dict[str, Any] | list[dict[str, Any]] | None:
    """Repo info (`mode="view"`, query="owner/repo") or repo search
    (`mode="search"`, free-text query) via the `gh` CLI.
    """
    if not is_available():
        return None
    q = (query or "").strip()[:200]
    if not q:
        return None

    if mode == "search":
        ok, out = await _run_cli(
            "gh",
            ["search", "repos", q, "--limit", "5", "--json",
             "fullName,description,stargazersCount,url,updatedAt"],
        )
    else:
        ok, out = await _run_cli(
            "gh",
            ["repo", "view", q, "--json",
             "name,description,stargazersCount,pushedAt,url,primaryLanguage"],
        )
    if not ok:
        logger.debug("[AgentReach] github_activity {} {!r} failed: {}", mode, q, out)
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


# Trading-symbol → search-query hints, so a research query about "BTC" reads
# "Bitcoin BTC latest news" instead of the bare, ambiguous ticker.
_SYMBOL_QUERY_HINTS = {
    "BTC": "Bitcoin BTC", "ETH": "Ethereum ETH", "SOL": "Solana SOL",
    "XRP": "XRP Ripple", "DOGE": "Dogecoin", "ADA": "Cardano ADA",
    "XAU": "gold price XAU", "EUR": "EUR euro", "GBP": "GBP pound sterling",
    "JPY": "JPY yen", "NAS": "Nasdaq", "SPX": "S&P 500",
}


def _search_query_for_symbol(symbol: str) -> str:
    sym = (symbol or "").upper()
    for base, hint in _SYMBOL_QUERY_HINTS.items():
        if base in sym:
            return f"{hint} latest news analysis"
    return f"{symbol} latest news analysis"


async def research_summary_for_symbol(symbol: str, token_budget: int = 800) -> str:
    """Compact, prompt-ready research block for one trading symbol.

    Backed by a 60s cache keyed by symbol so concurrent callers researching
    the same symbol in one analysis cycle share a single search call.
    """
    if not is_available():
        return ""
    sym = (symbol or "").strip().upper()
    if not sym:
        return ""

    now = asyncio.get_event_loop().time()
    cached = _SYMBOL_CACHE.get(sym)
    if cached and now - cached[0] < _SYMBOL_CACHE_TTL:
        return cached[1][:token_budget]

    items = await web_search(_search_query_for_symbol(sym), limit=5)
    if not items:
        _SYMBOL_CACHE[sym] = (now, "")
        return ""

    lines = [
        f"- [{it.get('source', '?')}] {it.get('title', '')} — {it.get('summary', '')}".rstrip(" —")
        for it in items if it.get("title")
    ]
    block = "\n".join(lines)[:token_budget]
    _SYMBOL_CACHE[sym] = (now, block)
    return block
