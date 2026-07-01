"""
Crypto Pair Catalog service.

Single source of truth that maps every **Bitget-tradeable** pair to its real
coin **name** and live market metadata (market cap / 24h volume / price) plus a
lightweight CoinGecko profile (description, categories, links).

Design:
  • Bitget ccxt markets define what is *tradeable* (the canonical symbol list).
  • CoinGecko enriches those rows with names, market cap, 24h volume, rank,
    price, 24h change, and a lightweight profile.
  • Everything is persisted to the ``crypto_pairs`` table so JARVIS can talk
    about coins by name and resolve spoken names/tickers to a tradeable pair.

The sync degrades gracefully: if CoinGecko is unavailable we still upsert the
Bitget tradeable list (keeping any last-known enrichment), and if Bitget is
unavailable we simply keep the previous catalog.

Public entry points used by the app:
  • ``sync_catalog(full=...)``     — refresh the catalog (used by the worker loop
                                     and the one-time startup sync).
  • ``resolve(query)``             — (Step 2) name/ticker/symbol → pair.
  • ``get_market_snapshot(symbol)``— (Step 2) cached live market cap/volume/price.
  • ``learn_alias(alias, symbol)`` — (Step 2) persist a user-said alias.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

import aiohttp
from loguru import logger
from sqlalchemy import select, func

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.timezone import now_sast
from app.models.database import CryptoPair


# ── Configuration ──────────────────────────────────────────────────────────
COINGECKO_BASE = "https://api.coingecko.com/api/v3"
COINGECKO_TIMEOUT = 20

# How many /coins/markets pages (250 each) to pull for enrichment. Top ~1500
# coins by market cap comfortably covers every Bitget listing.
_MARKETS_PAGES = 6
# How many /coins/{id} lightweight profiles to enrich per full sync (throttled).
_PROFILE_BATCH = 30
_PROFILE_SLEEP = 1.5  # seconds between profile calls (rate-limit friendly)

# Quotes we treat as tradeable for the catalog (USDT-margined markets).
_CATALOG_QUOTES = ("USDT",)

# In-process caches
_coins_list_cache: Dict[str, Any] = {"data": None, "ts": 0.0}
_COINS_LIST_TTL = 6 * 3600  # 6h

# Prevent overlapping syncs
_sync_lock = asyncio.Lock()
_last_full_sync_ts: float = 0.0


def _cg_headers() -> Dict[str, str]:
    key = settings.COINGECKO_API_KEY
    return {"x-cg-demo-api-key": key} if key else {}


# ── Bitget tradeable markets ────────────────────────────────────────────────

async def _load_bitget_pairs() -> List[Dict[str, str]]:
    """
    Return the list of Bitget-tradeable pairs as ``{symbol, base, quote}``.

    Uses a keyless public ccxt Bitget (swap) client so the catalog does not
    depend on configured API credentials. Symbols are normalised to the
    canonical ``BASE/QUOTE`` form (e.g. ``BTC/USDT``), de-duplicated by base.
    """
    try:
        import ccxt.async_support as ccxt
    except Exception as e:  # pragma: no cover - ccxt always present in prod
        logger.warning(f"[pair_catalog] ccxt unavailable: {e}")
        return []

    exchange = ccxt.bitget({
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    })
    pairs: Dict[str, Dict[str, str]] = {}
    try:
        markets = await exchange.load_markets()
        for m in markets.values():
            try:
                if not m.get("active", True):
                    continue
                base = (m.get("base") or "").upper()
                quote = (m.get("quote") or "").upper()
                if not base or quote not in _CATALOG_QUOTES:
                    continue
                symbol = f"{base}/{quote}"
                # First occurrence wins (dedupe swap/spot variants of same base).
                if symbol not in pairs:
                    pairs[symbol] = {"symbol": symbol, "base": base, "quote": quote}
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"[pair_catalog] Failed to load Bitget markets: {e}")
        return []
    finally:
        try:
            await exchange.close()
        except Exception:
            pass

    logger.info(f"[pair_catalog] Loaded {len(pairs)} tradeable Bitget pairs")
    return list(pairs.values())


# ── CoinGecko enrichment ─────────────────────────────────────────────────────

async def _fetch_coingecko_markets() -> Dict[str, Dict[str, Any]]:
    """
    Fetch top coins by market cap and index them by lowercased ticker.

    On a ticker collision (many coins share e.g. "uni"), the highest-market-cap
    coin wins because the pages are ordered ``market_cap_desc`` and we keep the
    first occurrence.

    Returns ``{ticker_lower: coin_dict}``. Empty dict on failure.
    """
    headers = _cg_headers()
    by_symbol: Dict[str, Dict[str, Any]] = {}
    async with aiohttp.ClientSession() as session:
        for page in range(1, _MARKETS_PAGES + 1):
            try:
                async with session.get(
                    f"{COINGECKO_BASE}/coins/markets",
                    headers=headers,
                    params={
                        "vs_currency": "usd",
                        "order": "market_cap_desc",
                        "per_page": 250,
                        "page": page,
                        "sparkline": "false",
                        "price_change_percentage": "24h",
                    },
                    timeout=aiohttp.ClientTimeout(total=COINGECKO_TIMEOUT),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if not data:
                            break
                        for coin in data:
                            sym = str(coin.get("symbol") or "").lower()
                            if sym and sym not in by_symbol:
                                by_symbol[sym] = coin
                    elif resp.status == 429:
                        logger.warning("[pair_catalog] CoinGecko rate limited (markets)")
                        break
                    else:
                        logger.warning(
                            f"[pair_catalog] CoinGecko markets page {page} → {resp.status}"
                        )
                        break
            except Exception as e:
                logger.warning(f"[pair_catalog] CoinGecko markets page {page} error: {e}")
                break
            # Gentle pacing between pages
            await asyncio.sleep(0.3)

    return by_symbol


async def _fetch_coins_list() -> Dict[str, List[Dict[str, str]]]:
    """
    Fetch and cache CoinGecko ``/coins/list`` indexed by lowercased ticker.

    Used as a name/id fallback for coins that are not in the top-markets set.
    Returns ``{ticker_lower: [{id, name}, ...]}``.
    """
    now = time.time()
    if _coins_list_cache["data"] is not None and (now - _coins_list_cache["ts"]) < _COINS_LIST_TTL:
        return _coins_list_cache["data"]

    by_symbol: Dict[str, List[Dict[str, str]]] = {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{COINGECKO_BASE}/coins/list",
                headers=_cg_headers(),
                timeout=aiohttp.ClientTimeout(total=COINGECKO_TIMEOUT),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for coin in data:
                        sym = str(coin.get("symbol") or "").lower()
                        if not sym:
                            continue
                        by_symbol.setdefault(sym, []).append({
                            "id": coin.get("id"),
                            "name": coin.get("name"),
                        })
                else:
                    logger.warning(f"[pair_catalog] CoinGecko /coins/list → {resp.status}")
    except Exception as e:
        logger.warning(f"[pair_catalog] CoinGecko /coins/list error: {e}")

    if by_symbol:
        _coins_list_cache["data"] = by_symbol
        _coins_list_cache["ts"] = now
    return by_symbol


async def _fetch_coin_profile(coingecko_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetch a lightweight profile for a single coin: description, categories, links.

    Returns ``{description, categories, links}`` or ``None`` on failure.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{COINGECKO_BASE}/coins/{coingecko_id}",
                headers=_cg_headers(),
                params={
                    "localization": "false",
                    "tickers": "false",
                    "market_data": "false",
                    "community_data": "false",
                    "developer_data": "false",
                    "sparkline": "false",
                },
                timeout=aiohttp.ClientTimeout(total=COINGECKO_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
    except Exception as e:
        logger.debug(f"[pair_catalog] profile fetch {coingecko_id} error: {e}")
        return None

    desc = ""
    try:
        desc = (data.get("description") or {}).get("en") or ""
        desc = desc.strip()[:2000]
    except Exception:
        desc = ""

    categories = [c for c in (data.get("categories") or []) if c]

    links_src = data.get("links") or {}
    homepage = ""
    try:
        homepage = next((h for h in (links_src.get("homepage") or []) if h), "")
    except Exception:
        homepage = ""
    whitepaper = links_src.get("whitepaper") or ""
    explorer = ""
    try:
        explorer = next((h for h in (links_src.get("blockchain_site") or []) if h), "")
    except Exception:
        explorer = ""

    links = {
        "homepage": homepage,
        "whitepaper": whitepaper,
        "explorer": explorer,
        "twitter": (links_src.get("twitter_screen_name") or ""),
    }
    return {"description": desc, "categories": categories, "links": links}


# ── Sync orchestration ───────────────────────────────────────────────────────

async def sync_catalog(full: bool = True) -> Dict[str, Any]:
    """
    Refresh the crypto-pair catalog from Bitget + CoinGecko.

    Args:
        full: When True, also fetches lightweight profiles for a throttled batch
              of pairs that don't have one yet. When False, only refreshes the
              tradeable list + market cap / volume / price (fast, used often).

    Returns a small summary dict. Never raises — degrades gracefully.
    """
    global _last_full_sync_ts

    if _sync_lock.locked():
        logger.info("[pair_catalog] Sync already in progress — skipping")
        return {"skipped": True}

    async with _sync_lock:
        started = time.time()
        summary: Dict[str, Any] = {
            "inserted": 0, "updated": 0, "enriched": 0,
            "tradeable": 0, "coingecko_ok": False,
        }

        # 1. Bitget tradeable list (source of truth for what JARVIS can trade).
        bitget_pairs = await _load_bitget_pairs()
        summary["tradeable"] = len(bitget_pairs)

        # 2. CoinGecko enrichment (best effort — keep last-known values on fail).
        by_symbol = await _fetch_coingecko_markets()
        coins_list = await _fetch_coins_list() if not by_symbol or full else {}
        summary["coingecko_ok"] = bool(by_symbol)

        if not bitget_pairs and not by_symbol:
            logger.warning("[pair_catalog] No data from Bitget or CoinGecko — catalog unchanged")
            return summary

        # 3. Upsert rows.
        async with AsyncSessionLocal() as db:
            existing_rows = (await db.execute(select(CryptoPair))).scalars().all()
            existing: Dict[str, CryptoPair] = {r.symbol: r for r in existing_rows}

            # Mark everything not-tradeable first; re-enable the ones still listed.
            listed_symbols = {p["symbol"] for p in bitget_pairs}

            for p in bitget_pairs:
                sym = p["symbol"]
                base_l = p["base"].lower()
                cg = by_symbol.get(base_l)

                row = existing.get(sym)
                is_new = row is None
                if is_new:
                    row = CryptoPair(symbol=sym, base=p["base"], quote=p["quote"])
                    db.add(row)
                    existing[sym] = row

                row.tradeable = True
                row.base = p["base"]
                row.quote = p["quote"]

                if cg:
                    row.coingecko_id = cg.get("id") or row.coingecko_id
                    row.name = cg.get("name") or row.name
                    row.market_cap = cg.get("market_cap")
                    row.market_cap_rank = cg.get("market_cap_rank")
                    row.volume_24h = cg.get("total_volume")
                    row.price = cg.get("current_price")
                    row.price_change_24h = cg.get("price_change_percentage_24h")
                elif not row.name:
                    # Fallback: try /coins/list for a name/id, else the ticker itself.
                    listed = coins_list.get(base_l) if coins_list else None
                    if listed:
                        row.coingecko_id = row.coingecko_id or listed[0].get("id")
                        row.name = row.name or listed[0].get("name")
                    if not row.name:
                        row.name = p["base"]

                row.updated_at = now_sast()
                if is_new:
                    summary["inserted"] += 1
                else:
                    summary["updated"] += 1

            # Rows no longer listed on Bitget → mark not tradeable (keep for names).
            for sym, row in existing.items():
                if sym not in listed_symbols and row.tradeable:
                    row.tradeable = False
                    row.updated_at = now_sast()

            await db.commit()

        # 4. Lightweight profile enrichment (throttled), only on full syncs.
        if full:
            summary["enriched"] = await _enrich_profiles(limit=_PROFILE_BATCH)
            _last_full_sync_ts = time.time()

        summary["elapsed"] = round(time.time() - started, 1)
        logger.info(
            f"[pair_catalog] Sync done: +{summary['inserted']} new, "
            f"{summary['updated']} updated, {summary['enriched']} profiles "
            f"({summary['tradeable']} tradeable) in {summary['elapsed']}s"
        )
        return summary


async def _enrich_profiles(limit: int = _PROFILE_BATCH) -> int:
    """
    Fetch lightweight profiles for up to ``limit`` pairs missing one.

    Prioritises the highest-market-cap coins so JARVIS knows the big names
    first. Throttled to respect CoinGecko rate limits. Returns count enriched.
    """
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(CryptoPair)
                .where(CryptoPair.coingecko_id.isnot(None))
                .where(CryptoPair.enriched_at.is_(None))
                .order_by(CryptoPair.market_cap_rank.asc().nullslast())
                .limit(limit)
            )
        ).scalars().all()
        targets = [(r.symbol, r.coingecko_id) for r in rows]

    enriched = 0
    for symbol, cg_id in targets:
        profile = await _fetch_coin_profile(cg_id)
        if profile is not None:
            async with AsyncSessionLocal() as db:
                row = (
                    await db.execute(select(CryptoPair).where(CryptoPair.symbol == symbol))
                ).scalar_one_or_none()
                if row is not None:
                    row.description = profile["description"] or row.description
                    row.categories = profile["categories"] or row.categories
                    row.links = profile["links"] or row.links
                    row.enriched_at = now_sast()
                    await db.commit()
                    enriched += 1
        await asyncio.sleep(_PROFILE_SLEEP)

    return enriched


async def catalog_is_empty() -> bool:
    """Return True when the catalog table has no rows (for startup seeding)."""
    try:
        async with AsyncSessionLocal() as db:
            count = (await db.execute(select(func.count(CryptoPair.id)))).scalar() or 0
            return count == 0
    except Exception as e:
        logger.debug(f"[pair_catalog] catalog_is_empty check failed: {e}")
        return False


# ── Serialisation ────────────────────────────────────────────────────────────

def pair_to_dict(row: CryptoPair, *, full: bool = False) -> Dict[str, Any]:
    """Serialise a CryptoPair row to a plain dict for API responses."""
    data: Dict[str, Any] = {
        "symbol": row.symbol,
        "base": row.base,
        "quote": row.quote,
        "name": row.name or row.base,
        "coingecko_id": row.coingecko_id,
        "market_cap": row.market_cap,
        "market_cap_rank": row.market_cap_rank,
        "volume_24h": row.volume_24h,
        "price": row.price,
        "price_change_24h": row.price_change_24h,
        "tradeable": bool(row.tradeable),
    }
    if full:
        data["description"] = row.description
        data["categories"] = row.categories or []
        data["links"] = row.links or {}
        data["aliases"] = row.aliases or []
        data["updated_at"] = row.updated_at.isoformat() if row.updated_at else None
    return data


# ── Query normalisation helpers ──────────────────────────────────────────────

def _norm(s: Optional[str]) -> str:
    return (s or "").strip().lower()


def _strip_quote(query: str) -> str:
    """
    Return the bare base token (lowercased) from a query, stripping a trailing
    quote and any slash/swap suffix. ``BITCOINUSDT`` → ``bitcoin``, ``BTC/USDT``
    → ``btc``. Returns "" when nothing is stripped/identical to the input.
    """
    qu = (query or "").strip().upper()
    if not qu:
        return ""
    if ":" in qu:
        qu = qu.split(":", 1)[0]
    if "/" in qu:
        qu = qu.split("/", 1)[0]
    else:
        for quote in ("USDT", "USDC"):
            if qu.endswith(quote) and len(qu) > len(quote):
                qu = qu[: -len(quote)]
                break
    return qu.lower()


def _candidate_symbols(query: str) -> List[str]:
    """
    Derive plausible canonical ``BASE/QUOTE`` symbols from a raw query.

    Handles: ``BTC/USDT``, ``BTC-USDT``, ``BTCUSDT``, ``btc``, ``BTC/USDT:USDT``.
    """
    q = query.strip().upper()
    if not q:
        return []
    # Strip ccxt swap suffix ("BTC/USDT:USDT" -> "BTC/USDT")
    if ":" in q:
        q = q.split(":", 1)[0]
    q = q.replace("-", "/")
    out: List[str] = []
    if "/" in q:
        out.append(q)
        base = q.split("/", 1)[0]
    else:
        base = q
        # Bare ticker glued to a quote, e.g. "BTCUSDT"
        for quote in ("USDT", "USDC"):
            if q.endswith(quote) and len(q) > len(quote):
                base = q[: -len(quote)]
                break
    if base:
        for quote in _CATALOG_QUOTES + ("USDC",):
            out.append(f"{base}/{quote}")
    # de-dupe preserving order
    seen: set = set()
    uniq: List[str] = []
    for s in out:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


# ── Resolution ───────────────────────────────────────────────────────────────

async def resolve(query: str) -> Optional[CryptoPair]:
    """
    Resolve a symbol / ``BASE/QUOTE`` / ticker / coin name / learned alias to a
    tradeable :class:`CryptoPair`, or ``None`` when there is no confident match.

    Match order (all case-insensitive), preferring tradeable + highest market cap:
      1. exact canonical symbol (``BTC/USDT``, ``BTCUSDT``, ``BTC/USDT:USDT``)
      2. base ticker (``btc``)
      3. exact coin name (``bitcoin``)
      4. learned alias
    """
    q = _norm(query)
    if not q:
        return None

    # Also derive the bare base token, so a "glued" form produced upstream
    # (e.g. dispatch turns "bitcoin" into "BITCOINUSDT") still matches a coin
    # name/ticker. terms = distinct lowercased tokens to try for name/ticker.
    stripped = _strip_quote(query)
    terms = {q}
    if stripped:
        terms.add(stripped)

    async with AsyncSessionLocal() as db:
        # 1. Canonical symbol forms.
        cand = _candidate_symbols(query)
        if cand:
            rows = (
                await db.execute(
                    select(CryptoPair).where(CryptoPair.symbol.in_(cand))
                )
            ).scalars().all()
            best = _pick_best(rows, cand)
            if best:
                return best

        # 2. Base ticker (e.g. "btc" / "btcusdt").
        rows = (
            await db.execute(
                select(CryptoPair)
                .where(func.lower(CryptoPair.base).in_(list(terms)))
                .order_by(CryptoPair.tradeable.desc(), CryptoPair.market_cap.desc().nullslast())
            )
        ).scalars().all()
        if rows:
            return rows[0]

        # 3. Exact coin name (e.g. "bitcoin" / "bitcoinusdt").
        rows = (
            await db.execute(
                select(CryptoPair)
                .where(func.lower(CryptoPair.name).in_(list(terms)))
                .order_by(CryptoPair.tradeable.desc(), CryptoPair.market_cap.desc().nullslast())
            )
        ).scalars().all()
        if rows:
            return rows[0]

        # 4. Learned alias (aliases is a JSON list of lowercased strings).
        aliased = (
            await db.execute(
                select(CryptoPair).where(CryptoPair.aliases.isnot(None))
            )
        ).scalars().all()
        for row in aliased:
            try:
                if terms & {_norm(a) for a in (row.aliases or [])}:
                    return row
            except Exception:
                continue

    return None


def _pick_best(rows: List[CryptoPair], ordered_symbols: List[str]) -> Optional[CryptoPair]:
    """Choose the best row: prefer the earliest candidate symbol, tradeable, cap."""
    if not rows:
        return None
    by_symbol = {r.symbol: r for r in rows}
    for sym in ordered_symbols:
        if sym in by_symbol:
            return by_symbol[sym]
    rows_sorted = sorted(
        rows,
        key=lambda r: (0 if r.tradeable else 1, -(r.market_cap or 0)),
    )
    return rows_sorted[0]


async def suggest(query: str, limit: int = 1) -> Optional[str]:
    """
    Return the closest coin name/ticker for a query that didn't resolve, so the
    reply can say "did you mean X?". Uses fuzzy matching over names + tickers.
    """
    import difflib

    q = _norm(query)
    if not q:
        return None
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(CryptoPair.name, CryptoPair.base)
                .where(CryptoPair.tradeable.is_(True))
            )
        ).all()

    choices: Dict[str, str] = {}
    for name, base in rows:
        if name:
            choices[_norm(name)] = name
        if base:
            choices.setdefault(_norm(base), base)

    if not choices:
        return None
    matches = difflib.get_close_matches(q, list(choices.keys()), n=limit, cutoff=0.6)
    if matches:
        return choices[matches[0]]
    return None


async def resolve_with_suggestion(query: str) -> tuple[Optional[CryptoPair], Optional[str]]:
    """Resolve a query; when it fails, also return the closest suggestion name."""
    pair = await resolve(query)
    if pair is not None:
        return pair, None
    return None, await suggest(query)


# ── Live market snapshot (cached ~60s) ───────────────────────────────────────

_snapshot_cache: Dict[str, Dict[str, Any]] = {}
_SNAPSHOT_TTL = 60  # seconds


async def _fetch_market_by_id(coingecko_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single coin's live market data from CoinGecko by id."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{COINGECKO_BASE}/coins/markets",
                headers=_cg_headers(),
                params={
                    "vs_currency": "usd",
                    "ids": coingecko_id,
                    "price_change_percentage": "24h",
                },
                timeout=aiohttp.ClientTimeout(total=COINGECKO_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data[0] if data else None
    except Exception as e:
        logger.debug(f"[pair_catalog] live market fetch {coingecko_id} error: {e}")
        return None


async def get_market_snapshot(symbol: str) -> Optional[Dict[str, Any]]:
    """
    Return a live-ish (cached ≤60s) market snapshot for a pair:
    ``{symbol, name, market_cap, market_cap_rank, volume_24h, price, price_change_24h}``.

    Refreshes from CoinGecko when the cache is stale; falls back to the stored
    catalog values if CoinGecko is unavailable (graceful degradation).
    """
    pair = await resolve(symbol)
    if pair is None:
        return None

    now = time.time()
    cached = _snapshot_cache.get(pair.symbol)
    if cached and (now - cached["_ts"]) < _SNAPSHOT_TTL:
        return {k: v for k, v in cached.items() if k != "_ts"}

    # Base snapshot from the stored catalog row (always available).
    snap: Dict[str, Any] = {
        "symbol": pair.symbol,
        "name": pair.name or pair.base,
        "market_cap": pair.market_cap,
        "market_cap_rank": pair.market_cap_rank,
        "volume_24h": pair.volume_24h,
        "price": pair.price,
        "price_change_24h": pair.price_change_24h,
    }

    # Refresh live values from CoinGecko when we know the id.
    if pair.coingecko_id:
        live = await _fetch_market_by_id(pair.coingecko_id)
        if live:
            snap.update({
                "name": live.get("name") or snap["name"],
                "market_cap": live.get("market_cap", snap["market_cap"]),
                "market_cap_rank": live.get("market_cap_rank", snap["market_cap_rank"]),
                "volume_24h": live.get("total_volume", snap["volume_24h"]),
                "price": live.get("current_price", snap["price"]),
                "price_change_24h": live.get("price_change_percentage_24h", snap["price_change_24h"]),
            })

    _snapshot_cache[pair.symbol] = {**snap, "_ts": now}
    return snap


# ── Alias learning ───────────────────────────────────────────────────────────

async def learn_alias(alias: str, symbol: str) -> bool:
    """
    Persist a user-said alias so future commands resolve it instantly.

    ``symbol`` may be any resolvable form; the alias is stored (lowercased,
    de-duplicated) on the matching pair. Returns True when a new alias was added.
    """
    a = _norm(alias)
    if not a:
        return False
    pair = await resolve(symbol)
    if pair is None:
        return False
    # Don't learn an alias that already resolves trivially (symbol/base/name).
    if a in {_norm(pair.symbol), _norm(pair.base), _norm(pair.name)}:
        return False

    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(select(CryptoPair).where(CryptoPair.symbol == pair.symbol))
        ).scalar_one_or_none()
        if row is None:
            return False
        aliases = list(row.aliases or [])
        existing = {_norm(x) for x in aliases}
        if a in existing:
            return False
        aliases.append(a)
        row.aliases = aliases
        row.updated_at = now_sast()
        await db.commit()
    logger.info(f"[pair_catalog] Learned alias '{a}' → {pair.symbol}")
    return True


# ── Name map + search (for endpoints) ────────────────────────────────────────

async def get_name_map() -> Dict[str, str]:
    """
    Return a compact ``{symbol: name}`` map for the extension + frontend, keyed
    by BOTH canonical (``BTC/USDT``) and glued (``BTCUSDT``) symbol forms so
    monitor payloads (which use ``BTCUSDT``) resolve to names directly.
    """
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(CryptoPair.symbol, CryptoPair.base, CryptoPair.quote, CryptoPair.name)
                .where(CryptoPair.tradeable.is_(True))
            )
        ).all()
    out: Dict[str, str] = {}
    for symbol, base, quote, name in rows:
        nm = name or base
        if not nm:
            continue
        out[symbol] = nm                        # "BTC/USDT"
        out[f"{base}{quote}"] = nm              # "BTCUSDT"
    return out


async def search_pairs(query: str = "", limit: int = 50) -> List[Dict[str, Any]]:
    """Return catalog rows (compact) matching an optional query, ranked by cap."""
    q = _norm(query)
    async with AsyncSessionLocal() as db:
        stmt = select(CryptoPair).where(CryptoPair.tradeable.is_(True))
        if q:
            like = f"%{q}%"
            stmt = stmt.where(
                func.lower(CryptoPair.symbol).like(like)
                | func.lower(CryptoPair.base).like(like)
                | func.lower(CryptoPair.name).like(like)
            )
        stmt = stmt.order_by(CryptoPair.market_cap.desc().nullslast()).limit(max(1, min(500, limit)))
        rows = (await db.execute(stmt)).scalars().all()
    return [pair_to_dict(r) for r in rows]
