"""
X (Twitter) Whale Account Monitor API
--------------------------------------
Configure and control the background loop that polls market-moving X accounts
and injects breaking tweets into the sentiment pipeline.

Config + custom accounts are persisted in data/x_monitor_config.json so they
survive restarts without needing a DB migration.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import aiohttp
from fastapi import APIRouter, HTTPException, Query
from loguru import logger
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.timezone import now_sast

router = APIRouter(prefix="/x-monitor", tags=["x-monitor"])

# ── Persistence ───────────────────────────────────────────────────────────────
_CONFIG_PATH = Path(__file__).parents[3] / "data" / "x_monitor_config.json"

# ── Default built-in accounts ─────────────────────────────────────────────────
_BUILTIN_ACCOUNTS: Dict[str, Dict[str, Any]] = {
    "realDonaldTrump":  {"display": "Donald Trump",           "symbols": ["BTC", "USD", "_MACRO_"],             "reliability": 0.95, "tier": 1, "builtin": True},
    "elonmusk":         {"display": "Elon Musk",              "symbols": ["BTC", "DOGE", "ETH", "_MARKET_"],    "reliability": 0.95, "tier": 1, "builtin": True},
    "saylor":           {"display": "Michael Saylor",         "symbols": ["BTC"],                               "reliability": 0.92, "tier": 1, "builtin": True},
    "GaryGensler":      {"display": "Gary Gensler (SEC)",     "symbols": ["_REGULATION_", "BTC", "ETH"],        "reliability": 0.95, "tier": 1, "builtin": True},
    "SECGov":           {"display": "SEC Official",           "symbols": ["_REGULATION_", "_MARKET_"],          "reliability": 0.95, "tier": 1, "builtin": True},
    "federalreserve":   {"display": "Federal Reserve",        "symbols": ["_MACRO_", "USD"],                    "reliability": 0.98, "tier": 1, "builtin": True},
    "POTUS":            {"display": "White House",            "symbols": ["_MACRO_", "_REGULATION_"],           "reliability": 0.90, "tier": 1, "builtin": True},
    "VitalikButerin":   {"display": "Vitalik Buterin",        "symbols": ["ETH", "_MARKET_"],                   "reliability": 0.90, "tier": 1, "builtin": True},
    "cz_binance":       {"display": "CZ Binance",             "symbols": ["BNB", "BTC", "_MARKET_"],            "reliability": 0.88, "tier": 2, "builtin": True},
    "brian_armstrong":  {"display": "Brian Armstrong",        "symbols": ["BTC", "ETH", "_MARKET_"],            "reliability": 0.85, "tier": 2, "builtin": True},
    "CathieDWood":      {"display": "Cathie Wood",            "symbols": ["BTC", "ETH", "_MARKET_"],            "reliability": 0.85, "tier": 2, "builtin": True},
    "novogratz":        {"display": "Mike Novogratz",         "symbols": ["BTC", "ETH", "_MARKET_"],            "reliability": 0.82, "tier": 2, "builtin": True},
    "tyler":            {"display": "Tyler Winklevoss",       "symbols": ["BTC", "ETH", "_MARKET_"],            "reliability": 0.82, "tier": 2, "builtin": True},
    "APompliano":       {"display": "Anthony Pompliano",      "symbols": ["BTC", "_MARKET_"],                   "reliability": 0.80, "tier": 2, "builtin": True},
    "RayDalio":         {"display": "Ray Dalio",              "symbols": ["_MACRO_", "BTC"],                    "reliability": 0.88, "tier": 2, "builtin": True},
    "GoldmanSachs":     {"display": "Goldman Sachs",          "symbols": ["_MACRO_", "_MARKET_"],               "reliability": 0.90, "tier": 2, "builtin": True},
    "jpmorgan":         {"display": "JPMorgan",               "symbols": ["_MACRO_", "_MARKET_"],               "reliability": 0.90, "tier": 2, "builtin": True},
    "PeterSchiff":      {"display": "Peter Schiff",           "symbols": ["BTC", "USD", "_MACRO_"],             "reliability": 0.80, "tier": 3, "builtin": True},
    "jimcramer":        {"display": "Jim Cramer (inverse)",   "symbols": ["_MARKET_"],                          "reliability": 0.70, "tier": 3, "builtin": True},
}

# ── In-memory runtime state ───────────────────────────────────────────────────
_home_since_id: Optional[str] = None   # cursor for home-timeline polling
_config: Dict[str, Any] = {
    "enabled": True,
    "interval_seconds": 120,
}
_custom_accounts: Dict[str, Dict[str, Any]] = {}
_paused_accounts: set = set()
_tweet_cache: List[Dict[str, Any]] = []   # last 50 tweets
_last_seen: Dict[str, str] = {}           # username → last tweet id
_loop_task: Optional[asyncio.Task] = None
_loop_running = False
_last_run: Optional[str] = None
_total_tweets = 0


def _load_persisted() -> None:
    global _config, _custom_accounts, _paused_accounts
    if not _CONFIG_PATH.exists():
        return
    try:
        saved = json.loads(_CONFIG_PATH.read_text())
        _config.update(saved.get("config", {}))
        _custom_accounts = saved.get("custom_accounts", {})
        _paused_accounts = set(saved.get("paused_accounts", []))
    except Exception as exc:
        logger.warning(f"[X Monitor] Failed to load config: {exc}")


def _save_persisted() -> None:
    try:
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CONFIG_PATH.write_text(json.dumps({
            "config": _config,
            "custom_accounts": _custom_accounts,
            "paused_accounts": list(_paused_accounts),
        }, indent=2))
    except Exception as exc:
        logger.warning(f"[X Monitor] Failed to save config: {exc}")


_load_persisted()


def _all_accounts() -> Dict[str, Dict[str, Any]]:
    merged = {**_BUILTIN_ACCOUNTS}
    merged.update(_custom_accounts)
    return merged


def _active_accounts() -> Dict[str, Dict[str, Any]]:
    return {u: d for u, d in _all_accounts().items() if u not in _paused_accounts}


# ── OAuth 1.0a signing (free-tier user-context auth) ────────────────────────
def _oauth1_header(
    method: str, url: str, params: Dict[str, str],
    api_key: str, api_secret: str, token: str, token_secret: str,
) -> str:
    """Return an OAuth 1.0a Authorization header with HMAC-SHA1 signature."""
    oauth: Dict[str, str] = {
        "oauth_consumer_key":     api_key,
        "oauth_nonce":            uuid.uuid4().hex,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp":        str(int(time.time())),
        "oauth_token":            token,
        "oauth_version":          "1.0",
    }
    all_params = {**params, **oauth}
    param_str = "&".join(
        f"{quote(k, safe='')}={quote(str(v), safe='')}"
        for k, v in sorted(all_params.items())
    )
    base = f"{method.upper()}&{quote(url, safe='')}&{quote(param_str, safe='')}"
    signing_key = f"{quote(api_secret, safe='')}&{quote(token_secret, safe='')}"
    sig = base64.b64encode(
        hmac.new(signing_key.encode(), base.encode(), hashlib.sha1).digest()
    ).decode()
    oauth["oauth_signature"] = sig
    parts = ", ".join(
        f'{k}="{quote(str(v), safe="")}"' for k, v in sorted(oauth.items())
    )
    return f"OAuth {parts}"


async def _poll_home_timeline(
    session: aiohttp.ClientSession,
    user_id: str,
    api_key: str, api_secret: str, token: str, token_secret: str,
) -> List[Dict[str, Any]]:
    """Fetch home timeline via v1.1 and return tweets from monitored accounts.

    v1.1 works with OAuth 1.0a without project enrollment (free tier).
    Accounts being monitored must be followed by the authenticated Twitter user.
    """
    global _home_since_id

    url = "https://api.twitter.com/1.1/statuses/home_timeline.json"
    params: Dict[str, str] = {
        "count":            "200",
        "tweet_mode":       "extended",  # avoid truncation
        "exclude_replies":  "true",
    }
    if _home_since_id:
        params["since_id"] = _home_since_id

    auth = _oauth1_header("GET", url, params, api_key, api_secret, token, token_secret)
    try:
        async with session.get(
            url,
            headers={"Authorization": auth},
            params=params,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status == 401:
                logger.warning("[X Monitor] OAuth 1.0a: access token rejected (401) — check TWITTER_ACCESS_TOKEN")
                return []
            if resp.status == 403:
                logger.warning("[X Monitor] OAuth 1.0a: permission denied (403) — ensure App has Read permission and tokens are regenerated")
                return []
            if resp.status == 429:
                logger.warning("[X Monitor] Home timeline rate limited (429)")
                return []
            if resp.status != 200:
                body_text = await resp.text()
                logger.debug(f"[X Monitor] Home timeline HTTP {resp.status}: {body_text[:120]}")
                return []
            tweets = await resp.json()
    except Exception as exc:
        logger.debug(f"[X Monitor] Home timeline fetch error: {exc}")
        return []

    if not tweets:
        return []

    _home_since_id = str(tweets[0]["id"])

    active_lower = {u.lower(): (u, m) for u, m in _active_accounts().items()}

    items: List[Dict[str, Any]] = []
    for tweet in tweets:
        uname = tweet.get("user", {}).get("screen_name", "").lower()
        if uname not in active_lower:
            continue
        orig_uname, meta = active_lower[uname]
        items.append({
            "username":    orig_uname,
            "display":     meta.get("display", orig_uname),
            "symbols":     meta.get("symbols", []),
            "tier":        meta.get("tier", 3),
            "reliability": meta.get("reliability", 0.75),
            "tweet_id":    str(tweet["id"]),
            "text":        tweet.get("full_text") or tweet.get("text", ""),
            "created_at":  tweet.get("created_at", ""),
            "fetched_at":  now_sast().isoformat(),
        })
    return items


# ── Twitter v2 polling ────────────────────────────────────────────────────────
async def _poll_account(
    session: aiohttp.ClientSession,
    username: str,
    meta: Dict[str, Any],
    bearer_token: str,
    max_age_minutes: int = 5,
) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {
        "tweet.fields": "created_at,text",
        "exclude": "retweets,replies",
        "max_results": 5,
    }
    if username in _last_seen:
        params["since_id"] = _last_seen[username]

    try:
        async with session.get(
            f"https://api.twitter.com/2/users/by/username/{username}/tweets",
            headers={"Authorization": f"Bearer {bearer_token}"},
            params=params,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            remaining = resp.headers.get("x-rate-limit-remaining", "999")
            if int(remaining) < 5:
                logger.warning(f"[X Monitor] Rate limit low ({remaining} remaining) — skipping remaining accounts")
                raise StopIteration("rate_limit")
            if resp.status == 429:
                logger.warning("[X Monitor] Rate limited by Twitter API")
                return []
            if resp.status == 401:
                logger.warning("[X Monitor] Twitter Bearer token invalid (401)")
                return []
            if resp.status != 200:
                logger.debug(f"[X Monitor] {username}: HTTP {resp.status}")
                return []
            payload = await resp.json()

        tweets = payload.get("data", [])
        if not tweets:
            return []

        _last_seen[username] = tweets[0]["id"]
        cutoff = now_sast().replace(tzinfo=None) if True else None
        items = []
        for tweet in tweets:
            created_raw = tweet.get("created_at", "")
            items.append({
                "username": username,
                "display": meta.get("display", username),
                "symbols": meta.get("symbols", []),
                "tier": meta.get("tier", 3),
                "reliability": meta.get("reliability", 0.75),
                "tweet_id": tweet["id"],
                "text": tweet.get("text", ""),
                "created_at": created_raw,
                "fetched_at": now_sast().isoformat(),
            })
        return items

    except StopIteration:
        raise
    except Exception as exc:
        logger.debug(f"[X Monitor] {username} fetch error: {exc}")
        return []


async def _run_one_poll() -> int:
    global _total_tweets, _last_run, _tweet_cache

    if not _active_accounts():
        return 0

    headers = {"User-Agent": "TradeBotXMonitor/1.0"}
    new_items: List[Dict[str, Any]] = []

    # Prefer OAuth 1.0a home-timeline (free tier) over app-only Bearer (Basic tier)
    s = settings
    use_oauth1 = all([s.TWITTER_API_KEY, s.TWITTER_API_SECRET,
                      s.TWITTER_ACCESS_TOKEN, s.TWITTER_ACCESS_TOKEN_SECRET,
                      s.TWITTER_USER_ID])

    async with aiohttp.ClientSession(headers=headers) as session:
        if use_oauth1:
            new_items = await _poll_home_timeline(
                session, s.TWITTER_USER_ID,
                s.TWITTER_API_KEY, s.TWITTER_API_SECRET,
                s.TWITTER_ACCESS_TOKEN, s.TWITTER_ACCESS_TOKEN_SECRET,
            )
        elif s.TWITTER_BEARER_TOKEN:
            for username, meta in _active_accounts().items():
                try:
                    items = await _poll_account(session, username, meta, s.TWITTER_BEARER_TOKEN)
                    new_items.extend(items)
                except StopIteration:
                    break
                except Exception:
                    pass
        else:
            return 0

    if new_items:
        _tweet_cache = (new_items + _tweet_cache)[:50]
        _total_tweets += len(new_items)
        logger.info(f"🐦 [X MONITOR] {len(new_items)} new tweets from whale accounts")

        # Publish to event bus so agents see them immediately
        try:
            from app.core.events import event_bus, Topics
            for item in new_items:
                event_bus.publish(Topics.SENTIMENT_UPDATE, {
                    **item,
                    "source": f"x_whale_{item['username']}",
                    "category": "social",
                    "is_breaking_social": True,
                })
        except Exception:
            pass

    _last_run = now_sast().isoformat()
    return len(new_items)


async def _monitor_loop() -> None:
    global _loop_running
    interval = max(60, int(_config.get("interval_seconds", 120)))
    logger.info(f"🐦 [X MONITOR] Starting — polling {len(_active_accounts())} accounts every {interval}s")
    await asyncio.sleep(5)
    while _loop_running:
        try:
            await _run_one_poll()
        except Exception as exc:
            logger.warning(f"[X Monitor] poll error: {exc}")
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break
    _loop_running = False
    logger.info("🐦 [X MONITOR] Stopped")


def start_x_monitor_loop() -> bool:
    global _loop_task, _loop_running
    if not settings.TWITTER_BEARER_TOKEN:
        logger.info("[X Monitor] No TWITTER_BEARER_TOKEN set — loop not started")
        return False
    if _loop_task and not _loop_task.done():
        return False
    _loop_running = True
    _loop_task = asyncio.create_task(_monitor_loop())
    return True


def stop_x_monitor_loop() -> bool:
    global _loop_running, _loop_task
    _loop_running = False
    if _loop_task:
        _loop_task.cancel()
    _loop_task = None
    return True


# ── Request models ────────────────────────────────────────────────────────────
class ConfigUpdate(BaseModel):
    interval_seconds: Optional[int] = Field(None, ge=60, le=3600)
    enabled: Optional[bool] = None


class AccountAdd(BaseModel):
    username: str
    display_name: Optional[str] = None
    symbols: Optional[List[str]] = []
    tier: Optional[int] = Field(2, ge=1, le=3)
    reliability: Optional[float] = Field(0.80, ge=0.0, le=1.0)


class AccountUpdate(BaseModel):
    display_name: Optional[str] = None
    symbols: Optional[List[str]] = None
    tier: Optional[int] = Field(None, ge=1, le=3)
    reliability: Optional[float] = Field(None, ge=0.0, le=1.0)
    active: Optional[bool] = None


# ── Routes ────────────────────────────────────────────────────────────────────
@router.get("/config")
async def get_config():
    s = settings
    oauth1_set = all([s.TWITTER_API_KEY, s.TWITTER_API_SECRET,
                      s.TWITTER_ACCESS_TOKEN, s.TWITTER_ACCESS_TOKEN_SECRET, s.TWITTER_USER_ID])
    return {
        "bearer_token_set": bool(s.TWITTER_BEARER_TOKEN),
        "oauth1_set": oauth1_set,
        "auth_mode": "oauth1" if oauth1_set else ("bearer" if s.TWITTER_BEARER_TOKEN else "none"),
        "interval_seconds": _config.get("interval_seconds", 120),
        "enabled": _config.get("enabled", True),
        "active_account_count": len(_active_accounts()),
        "total_account_count": len(_all_accounts()),
    }
@router.put("/config")
async def update_config(body: ConfigUpdate):
    if body.interval_seconds is not None:
        _config["interval_seconds"] = body.interval_seconds
    if body.enabled is not None:
        _config["enabled"] = body.enabled
    _save_persisted()
    return {"ok": True, "config": _config}


@router.get("/accounts")
async def list_accounts():
    all_acc = _all_accounts()
    result = []
    for username, meta in all_acc.items():
        result.append({
            "username": username,
            "display_name": meta.get("display", username),
            "symbols": meta.get("symbols", []),
            "tier": meta.get("tier", 3),
            "reliability": meta.get("reliability", 0.75),
            "builtin": meta.get("builtin", False),
            "active": username not in _paused_accounts,
        })
    result.sort(key=lambda x: (x["tier"], x["username"]))
    return {"accounts": result, "total": len(result)}


@router.post("/accounts")
async def add_account(body: AccountAdd):
    username = body.username.lstrip("@").strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username required")
    if username in _BUILTIN_ACCOUNTS:
        raise HTTPException(status_code=409, detail="Account is already a built-in; update it instead")
    _custom_accounts[username] = {
        "display": body.display_name or username,
        "symbols": body.symbols or [],
        "tier": body.tier or 2,
        "reliability": body.reliability or 0.80,
        "builtin": False,
    }
    _save_persisted()
    return {"ok": True, "username": username}


@router.put("/accounts/{username}")
async def update_account(username: str, body: AccountUpdate):
    username = username.lstrip("@")
    all_acc = _all_accounts()
    if username not in all_acc:
        raise HTTPException(status_code=404, detail="Account not found")

    is_builtin = username in _BUILTIN_ACCOUNTS

    if body.active is not None:
        if body.active:
            _paused_accounts.discard(username)
        else:
            _paused_accounts.add(username)

    if not is_builtin:
        entry = _custom_accounts.setdefault(username, {})
        if body.display_name is not None:
            entry["display"] = body.display_name
        if body.symbols is not None:
            entry["symbols"] = body.symbols
        if body.tier is not None:
            entry["tier"] = body.tier
        if body.reliability is not None:
            entry["reliability"] = body.reliability

    _save_persisted()
    return {"ok": True}


@router.delete("/accounts/{username}")
async def delete_account(username: str):
    username = username.lstrip("@")
    if username in _BUILTIN_ACCOUNTS:
        raise HTTPException(status_code=400, detail="Built-in accounts cannot be deleted; pause them instead")
    if username not in _custom_accounts:
        raise HTTPException(status_code=404, detail="Custom account not found")
    _custom_accounts.pop(username, None)
    _paused_accounts.discard(username)
    _save_persisted()
    return {"ok": True}


@router.get("/status")
async def get_status():
    s = settings
    oauth1_set = all([s.TWITTER_API_KEY, s.TWITTER_API_SECRET,
                      s.TWITTER_ACCESS_TOKEN, s.TWITTER_ACCESS_TOKEN_SECRET, s.TWITTER_USER_ID])
    return {
        "running": _loop_running,
        "bearer_token_set": bool(s.TWITTER_BEARER_TOKEN),
        "oauth1_set": oauth1_set,
        "auth_mode": "oauth1" if oauth1_set else ("bearer" if s.TWITTER_BEARER_TOKEN else "none"),
        "last_run": _last_run,
        "total_tweets_seen": _total_tweets,
        "active_accounts": len(_active_accounts()),
        "cached_tweets": len(_tweet_cache),
    }
@router.post("/start")
async def start_loop():
    s = settings
    oauth1_set = all([s.TWITTER_API_KEY, s.TWITTER_API_SECRET,
                      s.TWITTER_ACCESS_TOKEN, s.TWITTER_ACCESS_TOKEN_SECRET, s.TWITTER_USER_ID])
    if not oauth1_set and not s.TWITTER_BEARER_TOKEN:
        raise HTTPException(status_code=400, detail="No Twitter credentials set — add OAuth 1.0a keys or TWITTER_BEARER_TOKEN to .env")
    started = start_x_monitor_loop()
    return {"ok": True, "started": started, "already_running": not started}


@router.post("/stop")
async def stop_loop():
    stop_x_monitor_loop()
    return {"ok": True}


@router.get("/feed")
async def get_feed(limit: int = Query(20, ge=1, le=50)):
    return {"tweets": _tweet_cache[:limit], "total": len(_tweet_cache)}


@router.post("/poll-now")
async def poll_now():
    """Trigger a single poll immediately (for testing / manual refresh)."""
    if not settings.TWITTER_BEARER_TOKEN:
        raise HTTPException(status_code=400, detail="TWITTER_BEARER_TOKEN is not set")
    count = await _run_one_poll()
    return {"ok": True, "new_tweets": count}


@router.get("/test-connection")
async def test_connection():
    """
    Verify the active auth mode.
    OAuth 1.0a (free tier): checks /2/users/me with signed request.
    App-only Bearer (Basic tier): checks /2/users/by/username/twitter.
    Returns {ok, mode, note/error}.
    """
    s = settings
    use_oauth1 = all([s.TWITTER_API_KEY, s.TWITTER_API_SECRET,
                      s.TWITTER_ACCESS_TOKEN, s.TWITTER_ACCESS_TOKEN_SECRET,
                      s.TWITTER_USER_ID])

    if use_oauth1:
        url = "https://api.twitter.com/1.1/account/verify_credentials.json"
        params: Dict[str, str] = {}
        auth = _oauth1_header(
            "GET", url, params,
            s.TWITTER_API_KEY, s.TWITTER_API_SECRET,
            s.TWITTER_ACCESS_TOKEN, s.TWITTER_ACCESS_TOKEN_SECRET,
        )
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, headers={"Authorization": auth},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    body = await resp.json()
            if resp.status == 200:
                return {
                    "ok": True, "mode": "oauth1",
                    "username": body.get("screen_name", ""),
                    "name": body.get("name", ""),
                    "note": "OAuth 1.0a v1.1 — home-timeline monitoring active (free tier)",
                }
            if resp.status == 401:
                return {"ok": False, "mode": "oauth1", "error": "Access token rejected (401) — regenerate in the Developer Portal"}
            if resp.status == 403:
                return {"ok": False, "mode": "oauth1", "error": "Permission denied (403) — set App permissions to Read in the Developer Portal then regenerate tokens"}
            detail = body.get("errors", [{}])[0].get("message", "") if isinstance(body, dict) else ""
            return {"ok": False, "mode": "oauth1", "error": f"Twitter API error {resp.status}: {detail}"}
        except asyncio.TimeoutError:
            return {"ok": False, "mode": "oauth1", "error": "Connection timed out"}
        except Exception as exc:
            return {"ok": False, "mode": "oauth1", "error": str(exc)}

    bearer = s.TWITTER_BEARER_TOKEN
    if not bearer:
        return {"ok": False, "error": "No Twitter credentials set — add OAuth 1.0a keys or TWITTER_BEARER_TOKEN to .env"}

    # /2/users/me requires user context; use a public user lookup that accepts app-only Bearer.
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.twitter.com/2/users/by/username/twitter",
                headers={"Authorization": f"Bearer {bearer}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                body = await resp.json()

        if resp.status == 200:
            return {"ok": True, "mode": "bearer", "note": "app-only auth verified — timeline monitoring is ready"}
        if resp.status == 401:
            return {"ok": False, "mode": "bearer", "error": "Bearer token is invalid or expired (HTTP 401)"}
        if resp.status == 403:
            return {"ok": False, "mode": "bearer", "error": "Bearer token active but no read access (HTTP 403) — your Twitter Developer App needs Basic tier or above"}
        if resp.status == 429:
            return {"ok": False, "mode": "bearer", "error": "Rate limited (HTTP 429) — try again in a few minutes"}
        detail = body.get("detail") or body.get("title") or body.get("errors", [{}])[0].get("message", "")
        return {"ok": False, "mode": "bearer", "error": f"Twitter API error {resp.status}: {detail}"}

    except asyncio.TimeoutError:
        return {"ok": False, "mode": "bearer", "error": "Connection timed out — check your network connection"}
    except Exception as exc:
        return {"ok": False, "mode": "bearer", "error": str(exc)}
