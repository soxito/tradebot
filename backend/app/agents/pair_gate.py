"""Which pinned pairs the room may actually trade.

Two questions per pair: can an account the room trades list it at all, and
did its own backtest on this timeframe earn it a place (≥90% win rate,
positive expectancy, enough trades)? Pairs that fail either are still read by
OBLIVION — they just never convene the board for an entry.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from sqlalchemy import select

MIN_WIN_RATE = 0.90
MIN_TRADES = 10

_support_cache: Dict[str, Tuple[float, bool, str]] = {}
_SUPPORT_TTL_S = 3600.0


def judge(summary: Dict[str, Any], min_win_rate: float = MIN_WIN_RATE) -> Tuple[bool, str]:
    """Approve a pair/timeframe from its backtest summary."""
    trades = int(summary.get("trades") or 0)
    wr = summary.get("win_rate")
    exp = summary.get("avg_r")
    if trades < MIN_TRADES:
        return False, f"only {trades} trades in the backtest (need {MIN_TRADES})"
    if wr is None or wr < min_win_rate:
        return False, f"win rate {0 if wr is None else wr * 100:.1f}% below {min_win_rate * 100:.0f}%"
    if exp is None or exp <= 0:
        return False, "no positive expectancy"
    return True, f"{wr * 100:.1f}% over {trades} trades, {exp:+.3f}R per trade"


async def record_backtest(db: Any, doc: Dict[str, Any]) -> None:
    """Store approvals from a room-profile backtest run."""
    from app.agents.scope import normalise
    from app.models.database import PairApproval

    from app.agents.execution import get_settings

    bar = float(getattr(await get_settings(db), "pair_min_win_rate", MIN_WIN_RATE) or MIN_WIN_RATE)
    tf = (doc.get("params") or {}).get("timeframe")
    for sym, res in (doc.get("results") or {}).items():
        s = (res.get("summary") or {}).get("oblivion")
        if not s or not tf:
            continue
        ok, why = judge(s, bar)
        weeks = max(1e-9, (float(res.get("to") or 0) - float(res.get("from") or 0)) / 604800.0)
        row = (await db.execute(select(PairApproval).where(
            PairApproval.symbol == normalise(sym), PairApproval.timeframe == tf,
        ))).scalar_one_or_none()
        if row is None:
            row = PairApproval(symbol=normalise(sym), timeframe=tf)
            db.add(row)
        row.trades, row.win_rate, row.expectancy_r = s["trades"], s.get("win_rate"), s.get("avg_r")
        row.net_profit, row.weeks, row.approved, row.reason = s.get("net_profit"), weeks, ok, why
        row.run_id = doc.get("id")
    await db.commit()


async def approval(db: Any, symbol: str, timeframe: str) -> Optional[Any]:
    from app.agents.scope import normalise
    from app.models.database import PairApproval

    return (await db.execute(select(PairApproval).where(
        PairApproval.symbol == normalise(symbol), PairApproval.timeframe == timeframe,
    ))).scalar_one_or_none()


async def supported(db: Any, symbol: str) -> Tuple[bool, str]:
    """Does any account the room trades list this instrument?"""
    from app.agents.scope import normalise

    key = normalise(symbol)
    hit = _support_cache.get(key)
    if hit and time.time() - hit[0] < _SUPPORT_TTL_S:
        return hit[1], hit[2]
    ok, where = False, "no account the room trades lists it"
    try:
        from app.agents.execution import _mt5_symbol, get_settings, mt5_targets
        from plugins.MT5TradingPlugin.backend.services.mt5_client import mt5_client

        s = await get_settings(db)
        for acct in (await mt5_targets(db, s))["targets"]:
            try:
                resolved = await mt5_client.resolve_symbol(acct.login, acct.server, acct.password_encrypted,
                                                           _mt5_symbol(symbol))
            except Exception:  # noqa: BLE001
                resolved = None
            if resolved:
                ok, where = True, f"{acct.server} as {resolved}"
                break
        if not ok and key.endswith(("USDT", "USDC")):
            from app.services.crypto_accounts import crypto_targets

            if (await crypto_targets(db, s))["targets"]:
                ok, where = True, "exchange account"
    except Exception as exc:  # noqa: BLE001 — unknown is not "unsupported"
        logger.debug(f"[pair-gate] support check for {symbol} failed: {exc}")
        return True, "unchecked"
    _support_cache[key] = (time.time(), ok, where)
    return ok, where


async def status(db: Any, symbols: List[str], timeframes: List[str]) -> List[Dict[str, Any]]:
    out = []
    for sym in symbols:
        sup, where = await supported(db, sym)
        for tf in timeframes:
            row = await approval(db, sym, tf)
            out.append({
                "symbol": sym, "timeframe": tf, "supported": sup, "venue": where,
                "approved": bool(row and row.approved), "tested": row is not None,
                "trades": row.trades if row else None, "win_rate": row.win_rate if row else None,
                "expectancy_r": row.expectancy_r if row else None,
                "reason": row.reason if row else "not backtested yet",
                "tradeable": bool(sup and row and row.approved),
            })
    return out


async def room_symbols(db: Any) -> List[str]:
    """Pinned pairs plus the OBLIVION trading universe (when switched on)."""
    from app.agents import room
    from app.agents.execution import get_settings
    from app.agents.scope import normalise

    out = {normalise(p) for p in room.get_focus_symbols() if p}
    s = await get_settings(db)
    if getattr(s, "universe_enabled", False):
        out |= {normalise(p) for p in str(getattr(s, "universe_symbols", "") or "").split(",") if p.strip()}
    return sorted(x for x in out if x)


async def rejudge_all(db: Any) -> int:
    """Re-apply the approval bar to every stored backtest result (no re-run needed)."""
    from app.agents.execution import get_settings
    from app.models.database import PairApproval

    bar = float(getattr(await get_settings(db), "pair_min_win_rate", MIN_WIN_RATE) or MIN_WIN_RATE)
    rows = (await db.execute(select(PairApproval))).scalars().all()
    for r in rows:
        ok, why = judge({"trades": r.trades, "win_rate": r.win_rate, "avg_r": r.expectancy_r}, bar)
        r.approved, r.reason = ok, why
    await db.commit()
    return len(rows)
