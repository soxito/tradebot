"""Whale watch API — the big Bitcoin money for the page and the agents.

Three reads, all advisory:
  • ``GET /whale/holders``   — the registry with balances and 7-day flows
  • ``GET /whale/transfers`` — the moves that cleared the threshold, ranked
  • ``GET /whale/score``     — the aggregate ACCUMULATING / DISTRIBUTING read
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

router = APIRouter(prefix="/whale", tags=["whale-watch"])


def _snapshot_payload(snap) -> dict:
    return {
        "ok": snap.ok or snap.status == "PARTIAL",
        "status": snap.status,
        "score": snap.score,
        "net_flow_7d_btc": snap.net_flow_7d_btc,
        "detail": snap.detail,
        "as_of": snap.as_of,
        "holders": [
            {
                "address": r.address,
                "label": r.label,
                "category": r.category,
                "balance_btc": r.balance_btc,
                "net_flow_7d_btc": r.net_flow_7d_btc,
                "tx_count": r.tx_count,
                "source": r.source,
            }
            for r in sorted(
                snap.wallets,
                key=lambda r: r.balance_btc if r.balance_btc is not None else -1,
                reverse=True,
            )
        ],
        "transfers": snap.moves,
    }


@router.get("/holders")
async def whale_holders() -> dict:
    """The monitored wallets, richest first, with their 7-day flows."""
    from app.services import whale_watch

    snap = await whale_watch.resolve_whale_snapshot()
    if snap is None:
        return {"ok": False, "detail": "whale feed unreachable", "status": "UNKNOWN", "score": "UNKNOWN", "net_flow_7d_btc": None, "holders": [], "transfers": []}
    return _snapshot_payload(snap)


@router.get("/transfers")
async def whale_transfers(min_btc: Optional[float] = Query(None, ge=1)) -> dict:
    """The individual transfers above the threshold (or a custom one)."""
    from app.services import whale_watch

    snap = await whale_watch.resolve_whale_snapshot()
    if snap is None:
        return {"ok": False, "transfers": []}
    moves = snap.moves
    if min_btc is not None:
        moves = [m for m in moves if abs(m.get("btc") or 0) >= min_btc]
    return {"ok": True, "transfers": moves}


@router.get("/score")
async def whale_score() -> dict:
    """The aggregate read the seats and the forecast quote."""
    from app.services import whale_watch

    snap = await whale_watch.resolve_whale_snapshot()
    if snap is None:
        return {"ok": False, "score": "UNKNOWN"}
    return {
        "ok": True,
        "score": snap.score,
        "net_flow_7d_btc": snap.net_flow_7d_btc,
        "detail": snap.detail,
        "evidence": whale_watch.evidence_lines(snap),
    }
