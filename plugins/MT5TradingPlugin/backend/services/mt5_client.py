"""
MT5 REST Client — wraps the real mtapi-io REST API.

Real mtapi-io session model:
  1. GET /ConnectEx?user={login}&password={pwd}&server={server_name}
     → returns a session token string

  2. ALL subsequent requests use ?id={token}  (no per-request credentials)
     GET /AccountSummary?id={token}
     GET /OpenedOrders?id={token}
     GET /PriceHistoryEx?id={token}&symbol=XAUUSD&timeframe=60&bars=300

  3. GET /CheckConnect?id={token}  validates/auto-reconnects
  4. GET /Disconnect?id={token}    closes the session

Docker: docker run --rm -p 8090:80 mtapiio/mt5rest
Docs:   https://mt5.mtapi.io/index.html
"""
import asyncio
import os
import re
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
import httpx
from loguru import logger

from plugins.MT5TradingPlugin.backend.config import mt5_config


# ── Constants ─────────────────────────────────────────────────────────────────

MT5_MARKET_TYPES = {0, 1}           # 0=Buy position, 1=Sell position
MT5_POSITION_SIDE = {0: "buy", 1: "sell"}
MT5_PENDING_TYPE = {
    2: "buy_limit", 3: "sell_limit",
    4: "buy_stop",  5: "sell_stop",
    6: "buy_stop_limit", 7: "sell_stop_limit",
}

# Some mtapi-io builds report a STRING orderType (e.g. "SellLimit") and a
# "state" field instead of a numeric `type`. Map those to canonical strings.
MT5_ORDER_TYPE_STR = {
    "buy": "buy", "sell": "sell",
    "buylimit": "buy_limit", "selllimit": "sell_limit",
    "buystop": "buy_stop", "sellstop": "sell_stop",
    "buystoplimit": "buy_stop_limit", "sellstoplimit": "sell_stop_limit",
}
_PENDING_CANONICAL = set(MT5_PENDING_TYPE.values())


def normalize_order_type(o: dict) -> str:
    """Canonical order type from either numeric `type` or string `orderType`.

    Returns one of: buy, sell (market) or buy_limit/sell_limit/buy_stop/... (pending).
    Returns "" when it cannot be determined.
    """
    t = o.get("type")
    if isinstance(t, bool):
        t = None
    if isinstance(t, int):
        if t in MT5_POSITION_SIDE:
            return MT5_POSITION_SIDE[t]
        if t in MT5_PENDING_TYPE:
            return MT5_PENDING_TYPE[t]
    raw = o.get("orderType") or (o.get("orderInternal") or {}).get("type") or ""
    key = str(raw).lower().replace("_", "").replace(" ", "")
    return MT5_ORDER_TYPE_STR.get(key, "")


def is_pending_order(o: dict) -> bool:
    """True if the open order is a resting pending order (not a filled position)."""
    ct = normalize_order_type(o)
    if ct in _PENDING_CANONICAL:
        return True
    if ct in ("buy", "sell"):
        return False
    # Last resort: infer from state when type is unknown.
    return str(o.get("state", "")).lower() in ("placed", "pending")

TF_MAP: Dict[str, int] = {
    "M1":  1,   "M5":  5,   "M15": 15,  "M30": 30,
    "H1":  60,  "H4":  240, "D1":  1440,"W1":  10080,
    "1m":  1,   "5m":  5,   "15m": 15,  "30m": 30,
    "1h":  60,  "4h":  240, "1d":  1440,"1w":  10080,
}


# ── Server-name normalisation ─────────────────────────────────────────────────
# The mtapi bridge resolves the broker by its EXACT server name. A tiny typo
# (e.g. "MetaQoutes-Demo" — the 'uo' transposed to 'ou') yields a hard
# ``Server not found`` error at ConnectEx. These corrections run before every
# connect so a mistyped server resolves instead of failing. Case-insensitive
# substring fixes; the suffix ("-Demo", "-Live01", …) is preserved. Unknown
# names pass through unchanged.
_SERVER_TYPO_FIXES: Dict[str, str] = {
    "metaqoutes": "MetaQuotes",   # transposed 'uo' → 'ou' (most common)
    "meta-quotes": "MetaQuotes",
    "meta quotes": "MetaQuotes",
}


def normalize_server_name(server: str) -> str:
    """Correct well-known broker server-name typos before connecting.

    Fixes the most common transpositions (e.g. "MetaQoutes" → "MetaQuotes")
    while preserving the broker suffix. Returns the input unchanged when no
    known correction applies.
    """
    if not server:
        return server
    s = server.strip()
    for typo, correct in _SERVER_TYPO_FIXES.items():
        s = re.sub(re.escape(typo), correct, s, flags=re.IGNORECASE)
    # General 'Qoutes' → 'Quotes' transposition safety net (any *Qoutes* server).
    s = re.sub(r"qoutes", "Quotes", s, flags=re.IGNORECASE)
    return s


# ── Exceptions ────────────────────────────────────────────────────────────────

class MT5ClientError(Exception):
    def __init__(self, message: str, status_code: int = 0):
        self.status_code = status_code
        super().__init__(message)


# ── Token cache ───────────────────────────────────────────────────────────────

class TokenStore:
    """In-memory session token cache keyed by 'login@server'."""

    def __init__(self, check_interval_sec: int = 30):
        self._store: Dict[str, Dict] = {}
        self._interval = check_interval_sec

    def _key(self, login: str, server: str) -> str:
        return f"{login}@{server}"

    def set(self, login: str, server: str, token: str):
        self._store[self._key(login, server)] = {
            "token": token, "last_check": datetime.utcnow()
        }

    def get(self, login: str, server: str) -> Optional[str]:
        entry = self._store.get(self._key(login, server))
        return entry["token"] if entry else None

    def invalidate(self, login: str, server: str):
        self._store.pop(self._key(login, server), None)

    def needs_check(self, login: str, server: str) -> bool:
        entry = self._store.get(self._key(login, server))
        if not entry:
            return True
        return (datetime.utcnow() - entry["last_check"]).total_seconds() >= self._interval

    def refresh(self, login: str, server: str):
        k = self._key(login, server)
        if k in self._store:
            self._store[k]["last_check"] = datetime.utcnow()


# ── URL-error circuit breaker constants ───────────────────────────────────────
_URL_ERR_PHRASES = (
    "missing an 'http://'",
    "missing an 'https://'",
    "Request URL is missing",
    "Invalid URL",
    "No scheme",
    "no host",
)
_URL_CB_BACKOFF = 60   # seconds between "bad URL" retries (re-reads env each time)


# ── Client ────────────────────────────────────────────────────────────────────

class MT5Client:
    """Async HTTP client for mtapi-io using token-based session auth."""

    def __init__(self, base_url: Optional[str] = None):
        self._base_url_arg = base_url  # remember for self-healing
        self.base_url = self._resolve_url(base_url)
        self._http: Optional[httpx.AsyncClient] = None
        self._tokens = TokenStore()
        # Circuit breaker for "URL missing protocol" errors — avoids flooding
        # the log when MT5_API_URL is empty/schemeless in the environment.
        self._url_err_last: float = 0.0   # monotonic timestamp of last bad-URL event
        self._url_err_logged = False       # have we logged the actionable message?

    @staticmethod
    def _resolve_url(base_url: Optional[str]) -> str:
        """Return a valid absolute URL.  Falls back to env → default in that order."""
        raw = (base_url or "").strip()
        # Always go through the config validator: it adds a missing scheme and
        # rejects URLs pointing at TradeBot's own API port (a bad auto-detect
        # used to write those, turning every MT5 call into a self-404).
        try:
            from plugins.MT5TradingPlugin.backend.config import (
                _load_mt5_api_url, _validated_mt5_url,
            )
            resolved = _validated_mt5_url(raw) if raw else _load_mt5_api_url()
            return resolved.rstrip("/")
        except Exception:
            if raw.startswith("http://") or raw.startswith("https://"):
                return raw.rstrip("/")
            return "http://localhost:8092"

    def _maybe_reheal_url(self) -> bool:
        """
        Re-read MT5_API_URL from the environment.  Called after every
        bad-URL backoff window so a running process heals itself as soon
        as the operator corrects the env var (e.g. adds the right URL to .env
        and reloads the environment, or sets it in the shell).
        Returns True if the URL was fixed and the httpx client should be reset.
        """
        fresh = self._resolve_url(self._base_url_arg)
        if fresh == self.base_url:
            return False
        logger.info(f"[MT5] base_url healed: {self.base_url!r} → {fresh!r}")
        self.base_url = fresh
        # Force recreation of the httpx client with the new base URL
        if self._http and not self._http.is_closed:
            asyncio.ensure_future(self._http.aclose())
        self._http = None
        self._url_err_logged = False
        return True

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                base_url=self.base_url, timeout=httpx.Timeout(25.0)
            )
        return self._http

    async def close(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()

    async def _get(self, path: str, params: Optional[Dict] = None, retries: int = 1) -> Any:
        # ── Bad-URL circuit breaker ───────────────────────────────────────────
        import time as _time
        now = _time.monotonic()
        if self._url_err_last and (now - self._url_err_last) < _URL_CB_BACKOFF:
            raise MT5ClientError(
                f"MT5_API_URL is invalid — set it to http://host:8092 in .env "
                f"(retry in {int(_URL_CB_BACKOFF - (now - self._url_err_last))}s)",
                503,
            )
        if self._url_err_last:
            # Back-off window expired — try to self-heal from env
            self._maybe_reheal_url()
            self._url_err_last = 0.0   # reset so the next failure starts a fresh window

        http = await self._get_http()
        last: Exception = MT5ClientError("no attempt")
        for attempt in range(retries + 1):
            try:
                resp = await http.get(path, params=params)
                if resp.status_code >= 400:
                    raise MT5ClientError(
                        f"mtapi-io {resp.status_code}: {resp.text[:300]}",
                        status_code=resp.status_code,
                    )
                # ConnectEx returns text/plain; all other endpoints return JSON
                ct = resp.headers.get("content-type", "")
                if "json" in ct:
                    return resp.json()
                text = resp.text.strip()
                # Try JSON parse first (some endpoints return JSON with wrong CT)
                try:
                    import json as _json
                    return _json.loads(text)
                except ValueError:
                    return text  # Return raw string (e.g. session token from ConnectEx)
            except httpx.TimeoutException:
                last = MT5ClientError("mtapi-io timeout", 408)
                logger.warning(f"[MT5] timeout {path} attempt {attempt+1}")
            except httpx.ConnectError:
                last = MT5ClientError(
                    f"mtapi-io unreachable at {self.base_url}. "
                    "Run: docker run -d --name mt5rest --restart always "
                    "--platform linux/amd64 -p 8092:80 timurila/mt5rest", 503
                )
                logger.warning(f"[MT5] connection refused {path}")
            except MT5ClientError:
                raise
            except Exception as e:
                err_str = str(e)
                # Detect httpx "Request URL is missing http:// protocol" errors.
                # These flood the log when MT5_API_URL is empty/schemeless.
                # Log once with an actionable message, then suppress for _URL_CB_BACKOFF seconds.
                if any(phrase in err_str for phrase in _URL_ERR_PHRASES):
                    if not self._url_err_logged:
                        logger.error(
                            f"[MT5] MT5_API_URL={self.base_url!r} has no scheme — "
                            "all requests blocked. "
                            "Fix: add MT5_API_URL=http://localhost:8092 to your .env "
                            f"(suppressing further errors for {_URL_CB_BACKOFF}s)."
                        )
                        self._url_err_logged = True
                    self._url_err_last = _time.monotonic()
                    raise MT5ClientError(err_str, 503)
                last = MT5ClientError(err_str)
                logger.error(f"[MT5] {path}: {e}")
            if attempt < retries:
                await asyncio.sleep(1.5 ** attempt)
        raise last

    # ── Session ───────────────────────────────────────────────────────────────

    async def connect(self, login: str, server: str, password: str) -> str:
        """ConnectEx: establish session, return token string.

        Handles two common error codes automatically:
        * ``TOO_FREQUENT_FAIL_CONNECT`` — the bridge rate-limits repeated failed
          connections.  Parse the required wait (milliseconds) from the message
          and sleep once before a single retry.
        * ``Server not found: <name>`` — the supplied server name is unknown to
          the broker registry.  Query the Search API with the broker prefix from
          the server name, pick the closest demo/real match, and retry once with
          the corrected server string.
        """
        # Correct well-known broker-name typos (e.g. MetaQoutes → MetaQuotes)
        # before any network call so a trivial misspelling never hard-fails.
        server = normalize_server_name(server)

        async def _do_connect(srv: str) -> str:
            data = await self._get("/ConnectEx", params={
                "user": login, "password": password, "server": srv,
            })
            if isinstance(data, str):
                token = data.strip('"').strip()
            elif isinstance(data, dict):
                # Error dict — normalise and raise so callers can inspect the code.
                code = (data.get("code") or "").upper()
                msg  = data.get("message", repr(data))
                if code or "message" in data:
                    raise MT5ClientError(f"ConnectEx returned: {data!r}", 401)
                token = str(data.get("id") or data.get("token") or data.get("result", ""))
            else:
                token = str(data)

            if not token or len(token) < 3:
                raise MT5ClientError(f"ConnectEx returned: {data!r}", 401)
            if token.lower().startswith("error"):
                raise MT5ClientError(f"ConnectEx failed: {token}", 401)
            return token

        import re as _re

        try:
            token = await _do_connect(server)
        except MT5ClientError as first_err:
            raw = str(first_err)

            # ── Rate-limit backoff ────────────────────────────────────────────
            # e.g. "…Please wait 5000 milliseconds to try again…"
            wait_match = _re.search(r"wait (\d+) milliseconds", raw, _re.IGNORECASE)
            if "TOO_FREQUENT_FAIL_CONNECT" in raw or wait_match:
                wait_ms = int(wait_match.group(1)) if wait_match else 5000
                logger.info(
                    f"[MT5] {login}@{server} rate-limited — waiting {wait_ms}ms before retry"
                )
                await asyncio.sleep(wait_ms / 1000.0 + 0.2)
                try:
                    token = await _do_connect(server)
                except MT5ClientError as retry_err:
                    raw = str(retry_err)  # fall through to server-not-found check
                    raise retry_err if "server not found" not in raw.lower() else retry_err

            # ── Server-name auto-resolution ───────────────────────────────────
            # e.g. "Server not found: Exness-Demo" → search for real server names
            srv_missing = _re.search(r"server not found[:\s]+(\S+)", raw, _re.IGNORECASE)
            if srv_missing:
                bad_srv = (srv_missing.group(1).strip("\"',)") or server)
                # Derive company search term from the server string.
                # "Exness-Demo" → "Exness"; "ICMarkets-Live01" → "ICMarkets"
                company_guess = _re.split(r"[-_.]", bad_srv)[0]
                logger.info(
                    f"[MT5] Server not found '{bad_srv}' — searching broker registry for '{company_guess}'"
                )
                try:
                    candidates = await self.search_broker(company_guess)
                except Exception:
                    candidates = []

                # If the raw company term found nothing (often a typo), retry the
                # search with a typo-corrected term so misspelled names resolve.
                if not candidates:
                    fixed_guess = normalize_server_name(company_guess)
                    if fixed_guess.lower() != company_guess.lower():
                        logger.info(f"[MT5] Retrying broker search with corrected term '{fixed_guess}'")
                        try:
                            candidates = await self.search_broker(fixed_guess)
                        except Exception:
                            candidates = []

                # Pick the best matching server name:
                #   prefer an exact case-insensitive match, then a demo match
                #   if the supplied name contained 'demo', else any real server.
                want_demo = "demo" in server.lower()
                best: Optional[str] = None
                all_names: List[str] = []
                for broker_group in candidates:
                    for entry in (broker_group.get("results") or []):
                        srv_name = entry.get("name", "")
                        if srv_name:
                            all_names.append(srv_name)
                        if srv_name.lower() == bad_srv.lower():
                            best = srv_name   # exact match wins immediately
                            break
                        if best is None:
                            if want_demo and "demo" in srv_name.lower():
                                best = srv_name
                            elif not want_demo and "real" in srv_name.lower():
                                best = srv_name
                    if best and best.lower() == bad_srv.lower():
                        break  # exact match found, stop searching

                # Fuzzy fallback: no category match yet → closest name by edit
                # distance (handles typos the category rules miss).
                if best is None and all_names:
                    import difflib as _difflib
                    close = _difflib.get_close_matches(bad_srv, all_names, n=1, cutoff=0.6)
                    if close:
                        best = close[0]

                if best and best.lower() != server.lower():
                    logger.info(f"[MT5] Resolved server '{server}' → '{best}' — retrying")
                    try:
                        token = await _do_connect(best)
                        self._tokens.set(login, best, token)
                        logger.info(f"[MT5] Connected {login}@{best} (resolved) token={token[:10]}...")
                        return token
                    except MT5ClientError:
                        pass  # fall through and raise the original error

            raise first_err

        self._tokens.set(login, server, token)
        logger.info(f"[MT5] Connected {login}@{server} token={token[:10]}...")
        return token

    async def get_token(self, login: str, server: str, password: str) -> str:
        """Return a valid token, reconnecting if expired."""
        server = normalize_server_name(server)
        token = self._tokens.get(login, server)
        if token and not self._tokens.needs_check(login, server):
            return token
        if token:
            try:
                await self._get("/CheckConnect", params={"id": token})
                self._tokens.refresh(login, server)
                return token
            except Exception:
                logger.info(f"[MT5] Token stale for {login}@{server}, reconnecting")
                self._tokens.invalidate(login, server)
        return await self.connect(login, server, password)

    async def check_connection(self, login: str, server: str, password: str) -> bool:
        try:
            await self.connect(login, server, password)
            return True
        except MT5ClientError:
            return False

    async def disconnect(self, login: str, server: str, _password: str = ""):
        server = normalize_server_name(server)
        token = self._tokens.get(login, server)
        if token:
            try:
                await self._get("/Disconnect", params={"id": token})
            except Exception:
                pass
            self._tokens.invalidate(login, server)

    # ── Account ───────────────────────────────────────────────────────────────

    async def get_account_info(self, login: str, server: str, password: str) -> Dict:
        """AccountSummary + AccountDetails combined."""
        token = await self.get_token(login, server, password)
        summary = await self._get("/AccountSummary", params={"id": token})
        details: Dict = {}
        try:
            details = await self._get("/AccountDetails", params={"id": token})
        except Exception:
            pass
        return {
            "balance":     float(summary.get("balance", 0)),
            "equity":      float(summary.get("equity", 0)),
            "margin":      float(summary.get("margin", 0)),
            "freeMargin":  float(summary.get("freeMargin", 0)),
            "marginLevel": summary.get("marginLevel"),
            "profit":      float(summary.get("profit", 0)),
            "leverage":    int(summary.get("leverage") or details.get("leverage") or 100),
            "currency":    str(summary.get("currency") or details.get("currency") or "USD"),
            "company":     str(details.get("company", "")),
            "name":        str(details.get("name", "")),
            "server":      str(details.get("serverName", server)),
        }

    # ── Orders / Positions ────────────────────────────────────────────────────

    async def get_orders(self, login: str, server: str, password: str) -> List[Dict]:
        """All open orders (market positions + pending orders) from OpenedOrders."""
        token = await self.get_token(login, server, password)
        result = await self._get("/OpenedOrders", params={"id": token})
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for k in ("orders", "result", "data"):
                if isinstance(result.get(k), list):
                    return result[k]
        return []

    async def get_positions(self, login: str, server: str, password: str) -> List[Dict]:
        """Market positions only (filled Buy/Sell)."""
        return [o for o in await self.get_orders(login, server, password)
                if not is_pending_order(o)]

    async def get_pending_orders(self, login: str, server: str, password: str) -> List[Dict]:
        """Pending orders only (limit/stop, not yet filled)."""
        return [o for o in await self.get_orders(login, server, password)
                if is_pending_order(o)]

    async def get_deals(
        self, login: str, server: str, password: str,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> List[Dict]:
        """Closed order history from OrderHistory."""
        token = await self.get_token(login, server, password)
        params: Dict[str, Any] = {
            "id": token,
            "from": (date_from or datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%S"),
            "to":   (date_to   or datetime.utcnow()).strftime("%Y-%m-%dT%H:%M:%S"),
        }
        result = await self._get("/OrderHistory", params=params)
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for k in ("orders", "result", "data"):
                if isinstance(result.get(k), list):
                    return result[k]
        return []

    # ── Quotes ────────────────────────────────────────────────────────────────

    async def get_symbol_price(self, login: str, server: str, password: str, symbol: str) -> Dict:
        """Current bid/ask via GetQuote."""
        token = await self.get_token(login, server, password)
        data = await self._get("/GetQuote", params={"id": token, "symbol": symbol})
        if isinstance(data, dict):
            return {
                "bid": float(data.get("bid") or data.get("Bid") or 0),
                "ask": float(data.get("ask") or data.get("Ask") or 0),
            }
        return {"bid": 0.0, "ask": 0.0}

    # ── OHLCV ─────────────────────────────────────────────────────────────────

    async def get_candles(
        self, login: str, server: str, password: str,
        symbol: str, timeframe: str = "H1", count: int = 300,
    ) -> List[Dict]:
        """
        OHLCV bars via PriceHistoryEx (handles weekends/holidays automatically).
        Returns list of {time (unix sec), open, high, low, close, volume}.

        fromDate is computed per-timeframe so the API window contains roughly the
        last `count` bars.  Some brokers' mtapi-io builds ignore fromDate and
        return the oldest `bars` bars from the full account history; the post-fetch
        recency filter handles that case by discarding stale bars and letting the
        caller fall through to the exchange fallback.
        """
        token = await self.get_token(login, server, password)
        tf_min = TF_MAP.get(timeframe, TF_MAP.get(timeframe.upper(), 60))

        # Compute a generous lookback window based on timeframe×count.
        base_lookback_days = max(5, min(1825, int(count * tf_min / 1440 * 2.0) + 5))

        # If newest bar is older than this threshold, force frontend fallback.
        # Uses ~5 days across all timeframes to tolerate weekend market pauses.
        stale_periods = max(48, int((5 * 24 * 60) / max(tf_min, 1)))
        stale_max_age_sec = stale_periods * tf_min * 60

        async def _fetch_window(bars: int, lookback_days: int) -> List[Dict]:
            from_date = (datetime.utcnow() - timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%S")
            result = await self._get("/PriceHistoryEx", params={
                "id": token,
                "symbol": symbol,
                "timeframe": tf_min,
                "bars": bars,
                "fromDate": from_date,
            })

            raw: List[Dict] = []
            if isinstance(result, list):
                raw = result
            elif isinstance(result, dict):
                for k in ("bars", "data", "result"):
                    if isinstance(result.get(k), list):
                        raw = result[k]
                        break

            parsed: List[Dict] = []
            for b in raw:
                t = b.get("time") or b.get("openTime") or b.get("open_time") or 0
                if isinstance(t, str):
                    try:
                        t = int(datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp())
                    except ValueError:
                        t = 0
                elif isinstance(t, (int, float)):
                    if t > 1e10:
                        t = int(t / 1000)

                if not t:
                    continue

                parsed.append({
                    "time": int(t),
                    "open": float(b.get("openPrice") or b.get("open", 0)),
                    "high": float(b.get("highPrice") or b.get("high", 0)),
                    "low": float(b.get("lowPrice") or b.get("low", 0)),
                    "close": float(b.get("closePrice") or b.get("close", 0)),
                    "volume": b.get("tickVolume") or b.get("volume") or 0,
                })

            parsed.sort(key=lambda c: c["time"])
            return parsed

        attempts: List[tuple[int, int]] = []
        for mult in (1, 2, 4, 8):
            bars_req = min(5000, max(count, count * mult))
            lookback_days = max(5, min(1825, int(base_lookback_days * mult)))
            attempts.append((bars_req, lookback_days))
        attempts.append((min(5000, max(count * 12, 2500)), 1825))

        out: List[Dict] = []
        for bars_req, lookback_days in attempts:
            try:
                candidate = await _fetch_window(bars_req, lookback_days)
            except Exception as e:
                logger.warning(f"[MT5/candles] {symbol} {timeframe} bars={bars_req} lookback={lookback_days}d: {e}")
                continue

            if not candidate:
                continue

            newest_ts = int(candidate[-1]["time"])
            newest_age_sec = int(datetime.utcnow().timestamp()) - newest_ts

            # Keep freshest data seen across attempts.
            if not out or newest_ts > int(out[-1]["time"]):
                out = candidate

            # Stop early once we have acceptably fresh bars.
            if newest_age_sec <= stale_max_age_sec:
                out = candidate
                break

        if not out:
            return []

        now_ts = int(datetime.utcnow().timestamp())
        newest_ts = int(out[-1]["time"])
        newest_age_sec = now_ts - newest_ts

        # If latest bar is still stale, force frontend fallback.
        if newest_age_sec > stale_max_age_sec:
            logger.warning(
                f"[MT5/candles] stale bars for {symbol} {timeframe}: "
                f"newest_age={newest_age_sec}s threshold={stale_max_age_sec}s"
            )
            return []

        cutoff_ts = now_ts - (base_lookback_days * 86400)
        out = [b for b in out if b["time"] >= cutoff_ts]

        return out[-count:] if len(out) > count else out

    # ── Symbols ───────────────────────────────────────────────────────────────

    async def get_symbols(self, login: str, server: str, password: str) -> List[str]:
        """All tradeable symbols on this account."""
        token = await self.get_token(login, server, password)
        try:
            result = await self._get("/SymbolList", params={"id": token})
            items = result if isinstance(result, list) else result.get("symbols", [])
            return [
                s if isinstance(s, str) else s.get("name", s.get("symbol", ""))
                for s in items if s
            ]
        except MT5ClientError as e:
            logger.warning(f"[MT5] SymbolList: {e}")
            return []

    async def get_symbol_params(self, login: str, server: str, password: str, symbol: str) -> Dict:
        """Full symbol info (digits, tick size, contract size, etc.)."""
        token = await self.get_token(login, server, password)
        return await self._get("/SymbolParams", params={"id": token, "symbol": symbol})

    # ── Trading ───────────────────────────────────────────────────────────────

    async def place_order(
        self, login: str, server: str, password: str,
        symbol: str, order_type: str, volume: float, price: float,
        sl: Optional[float] = None, tp: Optional[float] = None,
        comment: Optional[str] = None,
    ) -> Dict:
        """OrderSendSafe: market or pending order."""
        token = await self.get_token(login, server, password)
        op_map = {
            "buy": "Buy", "sell": "Sell",
            "buy_limit": "BuyLimit",  "sell_limit": "SellLimit",
            "buy_stop":  "BuyStop",   "sell_stop":  "SellStop",
        }
        op = op_map.get(order_type.lower(), "Buy")
        params: Dict[str, Any] = {"id": token, "symbol": symbol, "operation": op, "volume": volume}
        if price and op not in ("Buy", "Sell"):
            params["price"] = price
        if sl is not None:
            params["stoploss"] = sl
        if tp is not None:
            params["takeprofit"] = tp
        if comment:
            params["comment"] = comment
        return await self._get("/OrderSendSafe", params=params)

    async def modify_order(
        self, login: str, server: str, password: str,
        ticket: int, price: Optional[float] = None,
        sl: Optional[float] = None, tp: Optional[float] = None,
    ) -> Dict:
        """OrderModifySafe: modify SL/TP (and price for pending orders)."""
        token = await self.get_token(login, server, password)
        params: Dict[str, Any] = {"id": token, "ticket": ticket}
        if price is not None: params["price"] = price
        if sl is not None:    params["stoploss"] = sl
        if tp is not None:    params["takeprofit"] = tp
        return await self._get("/OrderModifySafe", params=params)

    async def close_position(
        self, login: str, server: str, password: str,
        ticket: int, volume: Optional[float] = None,
    ) -> Dict:
        """OrderCloseSafe: close market position (full or partial)."""
        token = await self.get_token(login, server, password)
        params: Dict[str, Any] = {"id": token, "ticket": ticket}
        if volume is not None:
            params["volume"] = volume
        return await self._get("/OrderCloseSafe", params=params)

    async def cancel_order(self, login: str, server: str, password: str, ticket: int) -> Dict:
        """OrderCancelTask: cancel a pending order."""
        token = await self.get_token(login, server, password)
        return await self._get("/OrderCancelTask", params={"id": token, "ticket": ticket})

    # ── Stats ─────────────────────────────────────────────────────────────────

    async def get_equity_history(self, login: str, server: str, password: str) -> List[Dict]:
        """Equity curve time-series."""
        token = await self.get_token(login, server, password)
        try:
            result = await self._get("/EquityHistory", params={"id": token})
            items = result if isinstance(result, list) else result.get("points", result.get("data", []))
            return items if isinstance(items, list) else []
        except MT5ClientError as e:
            logger.warning(f"[MT5] EquityHistory: {e}")
            return []

    # ── Broker search ─────────────────────────────────────────────────────────

    async def search_broker(self, name: str) -> List[Dict]:
        """Find broker server names by company name (no auth required).

        The mtapi-io /Search endpoint expects ?company=<name>.
        Returns a list of { companyName, results: [{ name, access: [...] }] }.
        """
        try:
            result = await self._get("/Search", params={"company": name})
            return result if isinstance(result, list) else result.get("result", [])
        except Exception as e:
            logger.warning(f"[MT5] Search: {e}")
            return []

    async def ping(self) -> bool:
        try:
            await self._get("/Ping")
            return True
        except Exception:
            return False


# Singleton
mt5_client = MT5Client()
