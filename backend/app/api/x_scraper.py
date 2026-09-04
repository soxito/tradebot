"""
X Account Social Link Scraper — backed by ScrapeGraphAI.
Visits each monitored whale account's public X/Twitter profile and extracts
external social-media links (website, LinkedIn, GitHub, Telegram, Discord …).
Version-checks PyPI on every backend start; offers one-click install from the UI.
"""
from __future__ import annotations

import asyncio
import importlib.metadata
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel

from app.core.config import settings

router = APIRouter(prefix="/x-scraper", tags=["x-scraper"])

_RESULTS_PATH   = Path(__file__).parents[3] / "data" / "x_scraper_results.json"
_PACKAGE_NAME   = "scrapegraphai"
_PYPI_URL       = f"https://pypi.org/pypi/{_PACKAGE_NAME}/json"

# ── runtime state ──────────────────────────────────────────────────────────────
_installed_version:  Optional[str] = None
_latest_version:     Optional[str] = None
_update_available:   bool           = False
_last_version_check: Optional[str] = None
_results: Dict[str, Any]           = {}   # username → scraped payload


# ── helpers ────────────────────────────────────────────────────────────────────

def _detect_installed() -> Optional[str]:
    try:
        return importlib.metadata.version(_PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        return None


async def _fetch_latest_pypi() -> Optional[str]:
    try:
        import urllib.request, json as _j
        def _req():
            with urllib.request.urlopen(_PYPI_URL, timeout=8) as r:
                return _j.loads(r.read())["info"]["version"]
        return await asyncio.get_event_loop().run_in_executor(None, _req)
    except Exception as exc:
        logger.debug(f"[X Scraper] PyPI check failed: {exc}")
        return None


def _load_results() -> None:
    global _results
    if _RESULTS_PATH.exists():
        try:
            _results = json.loads(_RESULTS_PATH.read_text())
        except Exception:
            _results = {}


def _save_results() -> None:
    _RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _RESULTS_PATH.write_text(json.dumps(_results, indent=2))


def _build_llm_config() -> dict:
    """Build ScrapeGraphAI LLM config, preferring dedicated scraper keys."""
    s = settings
    api_key  = s.SCRAPEGRAPH_AI_API_KEY  or s.OPENAI_API_KEY  or s.NVIDIA_API_KEY
    base_url = s.SCRAPEGRAPH_AI_BASE_URL or s.OPENAI_BASE_URL or "https://integrate.api.nvidia.com/v1"
    model    = s.SCRAPEGRAPH_AI_MODEL    or s.OPENAI_MODEL    or "nvidia/llama-3.3-nemotron-super-49b-v1"

    # ScrapeGraphAI / LiteLLM: OpenAI-compat APIs need the "openai/" prefix
    if not any(model.startswith(p) for p in ("openai/", "anthropic/", "groq/", "google/", "ollama/")):
        model = f"openai/{model}"

    return {
        "llm": {"model": model, "api_key": api_key, "base_url": base_url},
        "verbose":           False,
        "headless":          True,
        "playwright_fetcher": True,   # X/Twitter is JS-rendered
        "max_images":        0,       # skip images to reduce token usage
    }


def _provider_display() -> str:
    s = settings
    if s.SCRAPEGRAPH_AI_API_KEY:
        return "Dedicated Scraper Key"
    if s.OPENAI_BASE_URL and "nvidia" in s.OPENAI_BASE_URL:
        return "NVIDIA NIM (system)"
    if s.GROQ_API_KEY and not s.OPENAI_API_KEY:
        return "Groq (system)"
    if s.OPENAI_API_KEY:
        return "OpenAI (system)"
    return "System default"


def _active_model() -> str:
    return settings.SCRAPEGRAPH_AI_MODEL or settings.OPENAI_MODEL or ""


# ── startup ────────────────────────────────────────────────────────────────────

async def startup_check() -> None:
    """Detect installed version and check PyPI for updates — called once at boot."""
    global _installed_version, _latest_version, _update_available, _last_version_check
    _load_results()
    _installed_version = _detect_installed()
    _latest_version    = await _fetch_latest_pypi()
    _update_available  = bool(
        _installed_version and _latest_version and _installed_version != _latest_version
    )
    _last_version_check = datetime.now(timezone.utc).isoformat()
    if _installed_version:
        logger.info(
            f"[X Scraper] {_PACKAGE_NAME} v{_installed_version} ready"
            + (f" — update to {_latest_version} available" if _update_available else "")
        )
    else:
        logger.info(f"[X Scraper] {_PACKAGE_NAME} not installed — basic stdlib scraper active")


# ── BASIC scraper (stdlib only, no extra packages required) ───────────────────

_SYNDICATION_URL = "https://cdn.syndication.twimg.com/timeline/profile?screen_name={}&count=3"
_BASIC_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TradeBot/1.0)", "Accept": "application/json, text/html"}


def _resolve_tco(short_url: str) -> Optional[str]:
    try:
        req = urllib.request.Request(short_url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=6) as r:
            final = r.geturl()
        if any(d in final for d in ("twitter.com", "t.co", "x.com")):
            return None
        return final
    except Exception:
        return None


async def _scrape_one_basic(username: str) -> Dict[str, Any]:
    """Scrape profile links using Twitter's public syndication API — zero extra deps."""
    def _fetch() -> Dict[str, Any]:
        links: List[str] = []
        website: Optional[str] = None
        bio: Optional[str] = None
        url = _SYNDICATION_URL.format(username)
        try:
            req = urllib.request.Request(url, headers=_BASIC_HEADERS)
            with urllib.request.urlopen(req, timeout=12) as r:
                raw = r.read().decode("utf-8", errors="replace")
        except Exception as exc:
            return {"error": f"Fetch failed: {exc}", "links": [], "website": None, "bio": None}
        seen: set = set()
        for tco in re.findall(r'https://t\.co/[\w]+', raw)[:8]:
            if tco in seen:
                continue
            seen.add(tco)
            resolved = _resolve_tco(tco)
            if resolved and resolved not in links:
                links.append(resolved)
                if not website:
                    website = resolved
        for u in re.findall(r'https?://(?!(?:t\.co|twitter\.com|x\.com|pic\.twitter))[^\s"\'<>\\]+', raw)[:5]:
            u = u.rstrip(').,;"\']\u200b')
            if u and u not in links:
                links.append(u)
                if not website:
                    website = u
        for pattern in [r'"description"\s*:\s*"([^"]{10,})"', r'"description_html"\s*:\s*"([^"]{10,})"']:
            m = re.search(pattern, raw)
            if m:
                bio = m.group(1).replace("\\n", " ").replace('\\"', '"').strip()
                break
        return {"links": links[:10], "website": website, "bio": bio}

    result = await asyncio.get_event_loop().run_in_executor(None, _fetch)
    result.update({"username": username, "scraped_at": datetime.now(timezone.utc).isoformat(), "mode": "basic"})
    return result


# ── scraping logic ─────────────────────────────────────────────────────────────

_SCRAPE_PROMPT = (
    "Extract all external social media links, website URLs, LinkedIn, GitHub, "
    "Telegram, Discord, YouTube, and any other non-Twitter URLs mentioned in "
    "this profile bio or page. Return JSON only: "
    '{"links": ["https://..."], "website": "<primary URL or null>", "bio": "<profile bio text>"}'
)


async def _scrape_one(username: str) -> Dict[str, Any]:
    """Use AI mode (scrapegraphai) if installed, else fall back to basic stdlib scraper."""
    if not _installed_version:
        return await _scrape_one_basic(username)

    cfg = _build_llm_config()

    def _run_sync():
        from scrapegraphai.graphs import SmartScraperGraph  # type: ignore
        sg = SmartScraperGraph(
            prompt=_SCRAPE_PROMPT,
            source=f"https://x.com/{username}",
            config=cfg,
        )
        return sg.run()

    try:
        result = await asyncio.get_event_loop().run_in_executor(None, _run_sync)
        if not isinstance(result, dict):
            result = {"links": [], "website": None, "bio": str(result)}
        result.update({"username": username, "scraped_at": datetime.now(timezone.utc).isoformat(), "mode": "ai"})
        return result
    except Exception as exc:
        logger.warning(f"[X Scraper AI] @{username}: {exc} — falling back to basic")
        return await _scrape_one_basic(username)


# ── REST endpoints ─────────────────────────────────────────────────────────────

@router.get("/status")
async def get_status():
    global _installed_version
    _installed_version = _detect_installed()
    return {
        "installed":          True,                      # basic mode always works
        "ai_mode":            bool(_installed_version),
        "installed_version":  _installed_version,
        "latest_version":     _latest_version,
        "update_available":   _update_available,
        "last_version_check": _last_version_check,
        "provider":           _provider_display(),
        "model":              _active_model(),
        "dedicated_key_set":  bool(settings.SCRAPEGRAPH_AI_API_KEY),
        "results_count":      len(_results),
        "last_install_error": _last_install_error,
        "python_executable":  sys.executable,
        "scrape_mode":        "ai" if _installed_version else "basic",
    }


@router.post("/install")
async def install_or_update():
    """Install scrapegraphai — tries 3 strategies until one succeeds."""
    global _installed_version, _latest_version, _update_available, _last_version_check, _last_install_error
    _last_install_error = None

    # Three increasingly permissive strategies
    strategies = [
        [sys.executable, "-m", "pip", "install", "--upgrade", _PACKAGE_NAME],
        [sys.executable, "-m", "pip", "install", "--upgrade", "--pre", _PACKAGE_NAME],
        [sys.executable, "-m", "pip", "install", "--upgrade", f"{_PACKAGE_NAME}>=1.0,<2.0"],
    ]

    last_output = ""
    for attempt, cmd in enumerate(strategies, 1):
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await proc.communicate()
            last_output = out.decode(errors="replace")
            if proc.returncode == 0:
                break
            logger.debug(f"[X Scraper] install attempt {attempt} failed: {last_output[-200:]}")
        except Exception as exc:
            last_output = str(exc)

    # playwright optional — non-fatal
    try:
        pw_proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", "--upgrade", "playwright",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        await pw_proc.communicate()
        await asyncio.create_subprocess_exec(
            sys.executable, "-m", "playwright", "install", "chromium",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception:
        pass

    _installed_version  = _detect_installed()
    _latest_version     = await _fetch_latest_pypi()
    _update_available   = False
    _last_version_check = datetime.now(timezone.utc).isoformat()

    if not _installed_version:
        _last_install_error = (
            f"All {len(strategies)} install strategies failed.\n"
            f"Python: {sys.executable}\n"
            f"Last pip output:\n{last_output[-800:]}"
        )
        return {"ok": False, "error": _last_install_error}

    return {"ok": True, "installed_version": _installed_version}


@router.post("/refresh-version")
async def refresh_version():
    global _installed_version, _latest_version, _update_available, _last_version_check
    _installed_version  = _detect_installed()
    _latest_version     = await _fetch_latest_pypi()
    _update_available   = bool(
        _installed_version and _latest_version and _installed_version != _latest_version
    )
    _last_version_check = datetime.now(timezone.utc).isoformat()
    return {
        "installed_version": _installed_version,
        "latest_version":    _latest_version,
        "update_available":  _update_available,
    }


class ScraperConfigBody(BaseModel):
    api_key:  Optional[str] = None
    base_url: Optional[str] = None
    model:    Optional[str] = None


@router.put("/config")
async def update_config(body: ScraperConfigBody):
    """Live-patch scraper credentials into the running settings object."""
    if body.api_key  is not None: settings.SCRAPEGRAPH_AI_API_KEY  = body.api_key
    if body.base_url is not None: settings.SCRAPEGRAPH_AI_BASE_URL = body.base_url
    if body.model    is not None: settings.SCRAPEGRAPH_AI_MODEL    = body.model
    return {"ok": True, "provider": _provider_display(), "model": _active_model()}


@router.get("/results")
async def get_results():
    return {"results": list(_results.values()), "total": len(_results)}


@router.post("/scrape/{username}")
async def scrape_account(username: str):
    # Works in basic (stdlib) mode too — no scrapegraphai required
    result = await _scrape_one(username)
    _results[username] = result
    _save_results()
    return result


@router.post("/scrape-all")
async def scrape_all():
    """Queue a background task to scrape every active monitored account."""
    from app.api.x_monitor import _active_accounts  # type: ignore
    accounts = list(_active_accounts().keys())

    async def _run():
        for uname in accounts:
            _results[uname] = await _scrape_one(uname)
        _save_results()

    asyncio.create_task(_run())
    return {"ok": True, "queued": len(accounts), "mode": "ai" if _installed_version else "basic"}


_load_results()
