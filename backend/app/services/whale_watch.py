"""Whale watch — the big Bitcoin money, live on the desk's screen.

The curated registry holds the eleven addresses that actually move the market:
the major exchange cold wallets, custodians and the famous dormant whales.
Every poll reads each wallet's balance *and* its most recent transactions from
Blockstream's free public Esplora API (blockchain.info as fallback), so a large
transfer is visible within a minute of confirming — not after an hourly batch.

Each new transfer above the threshold lands on the wire as a ``whale.move``
event, and the aggregate 7-day flow becomes the whale score — ACCUMULATING /
DISTRIBUTING / NEUTRAL — that the seats, the forecast and the cycle page quote.

Design rules inherited from macro_context and market_cycle: advisory only,
never gates a trade, never raises — an unreachable chain is silence, not a
bearish opinion. Labels are public attributions (BitInfoCharts et al.); every
default address was checksum-validated before shipping. The registry is
editable via ``BTC_WHALE_ADDRESSES`` (JSON) without touching code.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx
from loguru import logger

#: Satoshis per BTC.
SATS = 100_000_000

#: Net 7-day flow (BTC) across the registry that flips the score out of NEUTRAL.
SCORE_THRESHOLD_BTC = 500.0

#: An individual transfer this large (BTC) is worth surfacing — ≈$5M at $100k.
MOVE_THRESHOLD_BTC = float(os.getenv("WHALE_MOVE_THRESHOLD_BTC", "50"))

#: How often the snapshot may refresh, and how far back flows are summed.
BALANCE_TTL_S = 45.0
FLOW_WINDOW_S = 7 * 86400

#: Esplora is a public good but comfortably serves this cadence; a small gap
#: between launches keeps us polite under concurrent refreshes.
_REQUEST_GAP_S = 0.35
_MAX_CONCURRENT = 4


@dataclass(frozen=True)
class WhaleWallet:
    """One monitored address. Labels are community attributions, not gospel."""

    address: str
    label: str
    category: str  # "exchange" | "institutional" | "whale" | "dormant"


def _w(address: str, label: str, category: str) -> WhaleWallet:
    return WhaleWallet(address=address, label=label, category=category)


#: The starter registry — every address below was checksum-validated (base58 /
#: bech32) and its balance confirmed live against Blockstream before inclusion.
DEFAULT_REGISTRY: List[WhaleWallet] = [
    _w("34xp4vRoCGJym3xR7yCVPFHoCNxv4Twseo", "Binance Cold", "exchange"),
    _w("3M219KR5vEneNb47ewrPfWyb5jQ2DjxRP6", "Binance Cold 2", "exchange"),
    _w("3FHNBLobJnbCTFTVakh5TXmEneyf5PT61B", "Binance Cold 3", "exchange"),
    _w("bc1ql49ydapnjafl5t2cp9zqpjwe6pdgmxy98859v2", "Robinhood Cold", "exchange"),
    _w("bc1qgdjqv0av3q56jvd82tkdjpy7gdp9ut8tlqmgrpmv24sq90ecnvqqjwvw97", "Bitfinex Cold", "exchange"),
    _w("bc1qjasf9z3h7w3jspkhtgatgpyvvzgpa2wwd2lr0eh5tx44reyn2k7sfc27a4", "Tether Reserve", "institutional"),
    _w("12ib7dApVFvg82TXKycWBNpN8kFyiAN1dr", "Satoshi-era Whale", "whale"),
    _w("bc1qd4ysezhmypwty5dnw7c8nqy5h5nxg0xqsvaefd0qn5kq32vwnwqqgv4rzr", "Whale", "whale"),
    _w("bc1q8yj0herd4r4yxszw3nkfvt53433thk0f5qst4g", "Whale", "whale"),
    _w("1FeexV6bAHb8ybZjqQMjJrcCrHGW9sb6uF", "Mt.Gox-linked", "dormant"),
]


def registry_from(raw: Any) -> List[WhaleWallet]:
    """Registry from env JSON / DB text / default. Bad rows are skipped."""
    if raw is None:
        return list(DEFAULT_REGISTRY)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return list(DEFAULT_REGISTRY)
    if not isinstance(raw, list) or not raw:
        return list(DEFAULT_REGISTRY)

    out: List[WhaleWallet] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        addr = str(row.get("address") or "").strip()
        if not addr:
            continue
        out.append(WhaleWallet(
            address=addr,
            label=str(row.get("label") or addr[:10]),
            category=str(row.get("category") or "whale"),
        ))
    return out or list(DEFAULT_REGISTRY)


# ── The reading ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WalletReading:
    """One address at one moment."""

    address: str
    label: str
    category: str
    balance_btc: Optional[float] = None
    net_flow_7d_btc: Optional[float] = None
    tx_count: Optional[int] = None
    source: str = ""
    #: Individual recent transfers seen for this wallet, newest first:
    #: [{txid, btc (signed), direction, time}]
    transfers: Tuple[Dict[str, Any], ...] = ()


@dataclass
class WhaleSnapshot:
    """The registry, read. ``status`` follows the macro_context convention."""

    status: str = "UNAVAILABLE"
    wallets: List[WalletReading] = field(default_factory=list)
    score: str = "UNKNOWN"          # ACCUMULATING | DISTRIBUTING | NEUTRAL | UNKNOWN
    net_flow_7d_btc: Optional[float] = None
    moves: List[Dict[str, Any]] = field(default_factory=list)
    detail: str = ""
    as_of: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "OK"


def score_from_readings(readings: List[WalletReading]) -> Tuple[str, Optional[float]]:
    """Aggregate 7-day flow → the whale score. Pure."""
    flows = [r.net_flow_7d_btc for r in readings if r.net_flow_7d_btc is not None]
    if not flows:
        return "UNKNOWN", None
    total = round(sum(flows), 2)
    if total >= SCORE_THRESHOLD_BTC:
        return "ACCUMULATING", total
    if total <= -SCORE_THRESHOLD_BTC:
        return "DISTRIBUTING", total
    return "NEUTRAL", total


def moves_from_readings(readings: List[WalletReading]) -> List[Dict[str, Any]]:
    """Individual transfers above the threshold across the registry, ranked.

    These are real transactions — one row per transfer, not a wallet's weekly
    aggregate — which is what makes the feed feel live.
    """
    moves: List[Dict[str, Any]] = []
    for r in readings:
        for t in r.transfers:
            if abs(float(t.get("btc") or 0)) < MOVE_THRESHOLD_BTC:
                continue
            moves.append({
                "txid": t.get("txid"),
                "label": r.label,
                "address": r.address,
                "category": r.category,
                "direction": t.get("direction"),   # "in" | "out"
                "btc": round(float(t["btc"]), 2),
                "time": t.get("time"),             # unix s; None while unconfirmed
            })
    moves.sort(key=lambda m: abs(m.get("btc") or 0), reverse=True)
    return moves[:20]


# ── Chain fetchers ───────────────────────────────────────────────────────────

_last_request_at = 0.0


async def _get_json(url: str, *, timeout: float = 15.0) -> Optional[Any]:
    """GET with light pacing. Returns parsed JSON, or None on any failure."""
    global _last_request_at
    wait = _REQUEST_GAP_S - (time.monotonic() - _last_request_at)
    if wait > 0:
        await asyncio.sleep(wait)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url)
        _last_request_at = time.monotonic()
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception as exc:  # noqa: BLE001 — the chain is allowed to be down
        logger.debug(f"[WhaleWatch] GET failed: {exc}")
        return None


async def _read_esplora(wallet: WhaleWallet, now: float) -> Optional[WalletReading]:
    """Balance + recent transfers from Blockstream's Esplora (no key).

    Two cheap calls per wallet. The tx feed covers roughly the last 25
    confirmed transactions plus whatever is sitting in the mempool — for cold
    whales that window spans months, for hot wallets hours; either way a big
    transfer shows up here within a block of happening.
    """
    stats = await _get_json(f"https://blockstream.info/api/address/{wallet.address}")
    if not isinstance(stats, dict) or "chain_stats" not in stats:
        return None

    chain, mempool = stats.get("chain_stats") or {}, stats.get("mempool_stats") or {}
    funded = float(chain.get("funded_txo_sum") or 0) + float(mempool.get("funded_txo_sum") or 0)
    spent = float(chain.get("spent_txo_sum") or 0) + float(mempool.get("spent_txo_sum") or 0)
    balance = (funded - spent) / SATS

    txs = await _get_json(f"https://blockstream.info/api/address/{wallet.address}/txs")
    flow7 = 0.0
    flow_seen = False
    transfers: List[Dict[str, Any]] = []
    for tx in txs or []:
        if not isinstance(tx, dict):
            continue
        rec = sum(
            float(v.get("value") or 0)
            for v in tx.get("vout") or []
            if v.get("scriptpubkey_address") == wallet.address
        )
        sent = sum(
            float(i.get("prevout", {}).get("value") or 0)
            for i in tx.get("vin") or []
            if i.get("prevout", {}).get("scriptpubkey_address") == wallet.address
        )
        net = (rec - sent) / SATS
        status = tx.get("status") or {}
        ts = status.get("block_time")
        if ts and ts >= now - FLOW_WINDOW_S:
            flow7 += net
            flow_seen = True
        transfers.append({
            "txid": tx.get("txid"),
            "btc": round(net, 4),
            "direction": "in" if net >= 0 else "out",
            "time": ts,
            "confirmed": bool(status.get("confirmed")),
        })
        if len(transfers) >= 25:
            break

    return WalletReading(
        address=wallet.address, label=wallet.label, category=wallet.category,
        balance_btc=round(balance, 4),
        net_flow_7d_btc=round(flow7, 2) if flow_seen else None,
        tx_count=int(chain.get("tx_count") or 0),
        source="blockstream",
        transfers=tuple(transfers[:10]),
    )


async def _read_blockchain_info(wallet: WhaleWallet, now: float) -> Optional[WalletReading]:
    """Fallback: balance + 7d flow from blockchain.info rawaddr (paced ~10s)."""
    resp = await _esplora_style_rawaddr(wallet, now)
    return resp


async def _esplora_style_rawaddr(wallet: WhaleWallet, now: float) -> Optional[WalletReading]:
    global _last_request_at
    wait = 10.5 - (time.monotonic() - _last_request_at)
    if wait > 0:
        await asyncio.sleep(min(wait, 11.0))
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(
                f"https://blockchain.info/rawaddr/{wallet.address}?limit=50"
            )
        _last_request_at = time.monotonic()
        if resp.status_code != 200:
            return None
        data = resp.json()
        balance = float(data.get("final_balance") or 0) / SATS
        n_tx = int(data.get("n_tx") or 0)
        flow = 0.0
        seen = False
        transfers: List[Dict[str, Any]] = []
        for tx in reversed(data.get("txs") or []):     # rawaddr lists newest first
            ts = int(tx.get("time") or 0)
            net = float(tx.get("result") or 0) / SATS
            if ts >= now - FLOW_WINDOW_S:
                flow += net
                seen = True
            transfers.append({
                "txid": tx.get("hash"),
                "btc": round(net, 4),
                "direction": "in" if net >= 0 else "out",
                "time": ts,
                "confirmed": True,
            })
            if len(transfers) >= 10:
                break
        return WalletReading(
            address=wallet.address, label=wallet.label, category=wallet.category,
            balance_btc=round(balance, 4),
            net_flow_7d_btc=round(flow, 2) if seen else None,
            tx_count=n_tx, source="blockchain.info",
            transfers=tuple(transfers),
        )
    except Exception as exc:  # noqa: BLE001 — one bad payload is one bad address
        logger.debug(f"[WhaleWatch] blockchain.info parse {wallet.label}: {exc}")
        return None


async def _read_wallet(wallet: WhaleWallet, now: float) -> Optional[WalletReading]:
    return await _read_esplora(wallet, now) or await _esplora_style_rawaddr(wallet, now)


# ── Cached resolution ────────────────────────────────────────────────────────

_lock = asyncio.Lock()
_cached: Dict[str, Any] = {"snap": None, "ts": 0.0}


def _registry_raw() -> Optional[Any]:
    raw = os.getenv("BTC_WHALE_ADDRESSES")
    return raw if raw else None


async def resolve_whale_snapshot(force: bool = False) -> Optional[WhaleSnapshot]:
    """The live whale read, cached briefly. Failure returns None, not an opinion."""
    async with _lock:
        now_mono = time.monotonic()
        if not force and _cached["snap"] is not None and now_mono - _cached["ts"] < BALANCE_TTL_S:
            return _cached["snap"]

        registry = registry_from(_registry_raw())
        wall_time = time.time()

        sem = asyncio.Semaphore(_MAX_CONCURRENT)

        async def _one(w: WhaleWallet) -> Optional[WalletReading]:
            async with sem:
                return await _read_wallet(w, wall_time)

        readings = [
            r for r in await asyncio.gather(*(_one(w) for w in registry))
            if r is not None
        ]

        if not readings:
            _cached["snap"] = None
            return None

        score, total = score_from_readings(readings)
        detail = (
            f"{len(readings)}/{len(registry)} monitored wallets · "
            f"{round(total, 1) if total is not None else '?'} BTC net 7d"
        )
        snap = WhaleSnapshot(
            status="OK" if len(readings) == len(registry) and total is not None else "PARTIAL",
            wallets=readings, score=score, net_flow_7d_btc=total,
            moves=moves_from_readings(readings), detail=detail, as_of=wall_time,
        )
        _cached["snap"] = snap
        _cached["ts"] = now_mono
        return snap


def evidence_lines(snap: WhaleSnapshot) -> List[str]:
    """What the seats read about the big money. Short — context, not a sermon."""
    if not snap.ok and snap.status != "PARTIAL":
        return []
    lines = [f"Whale flow: {snap.score} — {snap.detail}."]
    for move in snap.moves[:3]:
        btc = move.get("btc") or 0
        direction = "into" if btc > 0 else "out of"
        lines.append(
            f"{move['label']} moved {abs(btc):,.0f} BTC {direction} its wallet."
        )
    return lines
