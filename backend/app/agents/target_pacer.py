"""The room's profit targets: weekly ($, a floor) and the portfolio's day.

The weekly minimum is a floor, never a cap. The day is a band on the whole
portfolio (every account the room trades, valued at 00:00 UTC): below the
minimum (default 10%) trades are paced inside the 1–5% risk band toward it;
once it is banked they drop to the band floor; at the cap (default 20%) no new
entries open until tomorrow, so a good day is locked in instead of overtraded.

Progress is the realised profit of the room's own trades this week (Monday
00:00 UTC) on every account it trades, plus the floating profit of its open
positions. Pacing compares what is left of the target with what the approved
pairs are expected to earn in the rest of the week (their backtested
R-per-week) and sizes trades up — never beyond ``target_max_multiplier`` ×
the normal size — when the week is behind. A target is an aim, not a
guarantee: a week can bring one signal or none.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from loguru import logger
from sqlalchemy import select

_cache: Dict[str, Any] = {"at": 0.0, "value": None}
_TTL_S = 60.0


def week_start_utc(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return start


def week_fraction_left(now: datetime | None = None) -> float:
    """Share of the trading week (Mon 00:00 – Sat 00:00 UTC) still ahead."""
    now = now or datetime.now(timezone.utc)
    start = week_start_utc(now)
    total = 5 * 86400.0
    elapsed = (now - start).total_seconds()
    return max(0.0, min(1.0, 1.0 - elapsed / total))


def multiplier(*, remaining_usd: float, expected_r_left: float, base_risk_usd: float, max_mult: float) -> float:
    """How much to size up so the expected rest-of-week profit covers what is left. Pure."""
    if remaining_usd <= 0 or base_risk_usd <= 0:
        return 1.0
    if expected_r_left <= 0:
        return max(1.0, max_mult)
    need = remaining_usd / (expected_r_left * base_risk_usd)
    return round(max(1.0, min(max_mult, need)), 3)


def portfolio_day(*, value_now: float, today_usd: float, min_pct: float, max_pct: float) -> Dict[str, Any]:
    """Today's portfolio band from the value now and today's profit. Pure."""
    start = value_now - today_usd
    if start <= 0:
        start = value_now
    lo = round(start * min_pct / 100.0, 2) if min_pct > 0 else 0.0
    cap = round(start * max_pct / 100.0, 2) if max_pct > 0 else None
    return {
        "day_start_value": round(start, 2), "portfolio_value": round(value_now, 2),
        "min_pct": min_pct, "max_pct": max_pct, "target_usd": lo, "cap_usd": cap,
        "progress_pct": round(today_usd / start * 100.0, 3) if start > 0 else 0.0,
        "min_hit": lo > 0 and today_usd >= lo,
        "capped": cap is not None and cap > 0 and today_usd >= cap,
    }


def day_risk(*, remaining_usd: float, expected_r_left: float, value: float, min_pct: float, max_pct: float) -> float:
    """Risk % whose expected R for the rest of the day covers what is left of
    the day's minimum, clamped to the band. Pure."""
    if remaining_usd <= 0 or value <= 0:
        return min_pct
    if expected_r_left <= 0:
        return max_pct
    need = remaining_usd / expected_r_left / value * 100.0
    return round(max(min_pct, min(max_pct, need)), 3)


def day_start_utc(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def day_fraction_left(now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return 0.0
    return max(0.0, 1.0 - (now - day_start_utc(now)).total_seconds() / 86400.0)


async def room_pnl_this_week(db: Any, accounts: List[Any], since: datetime | None = None) -> Dict[str, Any]:
    """Realised + floating profit of ROOM-tagged positions on ``accounts`` since ``since`` (default Monday)."""
    from app.trading.order_tags import SOURCE_ROOM, classify
    from plugins.MT5TradingPlugin.backend.models import MT5Deal, MT5Position

    start = (since or week_start_utc()).replace(tzinfo=None)
    realised = floating = 0.0
    by_account: Dict[int, float] = {}
    by_symbol: Dict[str, Dict[str, float]] = {}

    def _sym(raw: Any) -> str:
        from app.agents.scope import normalise

        s = normalise(raw)
        for suffix in ("M", ".R", "#"):
            if s.endswith(suffix) and len(s) > 6:
                s = s[: -len(suffix)]
        return s.lstrip("#")
    for acct in accounts:
        deals = (await db.execute(select(MT5Deal).where(MT5Deal.account_id == acct.id))).scalars().all()
        room_positions = {d.mt5_position_ticket for d in deals
                          if d.mt5_position_ticket and classify(d.comment)["source"] == SOURCE_ROOM}
        acc = 0.0
        for d in deals:
            if d.mt5_time and d.mt5_time >= start and d.mt5_position_ticket in room_positions:
                v = float(d.profit or 0) + float(d.swap or 0) + float(d.commission or 0) + float(d.fee or 0)
                acc += v
                by_symbol.setdefault(_sym(d.symbol), {"realised": 0.0, "floating": 0.0})["realised"] += v
        opened = (await db.execute(select(MT5Position).where(MT5Position.account_id == acct.id))).scalars().all()
        flo = 0.0
        for p in opened:
            if classify(p.comment)["source"] != SOURCE_ROOM:
                continue
            v = float(p.profit or 0) + float(p.swap or 0)
            flo += v
            by_symbol.setdefault(_sym(p.symbol), {"realised": 0.0, "floating": 0.0})["floating"] += v
        realised += acc
        floating += flo
        by_account[acct.id] = round(acc + flo, 2)
    return {"realised": round(realised, 2), "floating": round(floating, 2),
            "total": round(realised + floating, 2), "by_account": by_account,
            "by_symbol": {k: {"realised": round(v["realised"], 2), "floating": round(v["floating"], 2)}
                          for k, v in by_symbol.items()},
            "week_start": start.isoformat() + "Z"}


async def room_day_record(db: Any, accounts: List[Any], since: datetime | None = None) -> Dict[str, Any]:
    """Wins and losses of ROOM-tagged positions that closed since ``since`` (default 00:00 UTC).

    A position is closed when no open row remains for its ticket; its outcome is
    the sign of every deal booked on it (profit + swap + costs)."""
    from app.trading.order_tags import SOURCE_ROOM, classify
    from plugins.MT5TradingPlugin.backend.models import MT5Deal, MT5Position

    start = (since or day_start_utc()).replace(tzinfo=None)
    wins = losses = 0
    for acct in accounts:
        deals = (await db.execute(select(MT5Deal).where(MT5Deal.account_id == acct.id))).scalars().all()
        room = {d.mt5_position_ticket for d in deals
                if d.mt5_position_ticket and classify(d.comment)["source"] == SOURCE_ROOM}
        still_open = {getattr(p, "mt5_ticket", None) for p in (await db.execute(
            select(MT5Position).where(MT5Position.account_id == acct.id))).scalars().all()}
        per: Dict[int, List[Any]] = {}
        for d in deals:
            if d.mt5_position_ticket in room:
                per.setdefault(d.mt5_position_ticket, []).append(d)
        for ticket, ds in per.items():
            if ticket in still_open:
                continue
            last = max((d.mt5_time for d in ds if d.mt5_time), default=None)
            if last is None or last < start:
                continue
            pnl = sum(float(d.profit or 0) + float(d.swap or 0) + float(d.commission or 0) + float(d.fee or 0)
                      for d in ds)
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1
    decided = wins + losses
    return {"wins": wins, "losses": losses, "win_rate": round(wins / decided, 4) if decided else None,
            "since": start.isoformat() + "Z"}


async def portfolio_value(db: Any, s: Any, accounts: List[Any]) -> float:
    """Equity of every account the room trades: MT5 (demo + armed live) and crypto."""
    value = sum(float(getattr(a, "equity", 0) or getattr(a, "balance", 0) or 0) for a in accounts)
    try:
        from app.services.crypto_accounts import crypto_targets

        value += sum(float(getattr(a, "equity", 0) or getattr(a, "balance", 0) or 0)
                     for a in (await crypto_targets(db, s))["targets"])
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[target] crypto equity unavailable: {exc}")
    return value


async def banded_risk_pct(db: Any, s: Any, symbol: str, equity: float) -> float:
    """Risk % for an OBLIVION profile trade: the higher of the pair's weekly
    pacing and the portfolio day's pacing, the band floor once the day's
    minimum is banked."""
    min_pct = float(getattr(s, "min_risk_pct", 1.0) or 1.0)
    max_pct = float(getattr(s, "max_risk_pct", 5.0) or 5.0)
    weekly = await pair_risk_pct(db, s, symbol, equity)
    try:
        st = await state(db)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[target] day pacing unavailable: {exc}")
        return weekly
    d = st["daily"]
    if not d.get("min_pct"):
        return weekly
    if d.get("min_hit"):
        return min_pct
    daily = day_risk(remaining_usd=float(d.get("remaining_total_usd") or 0),
                     expected_r_left=float(st["expected"]["r_per_week"]) / 5.0 * max(float(d["day_left"]), 0.2),
                     value=float(d.get("day_start_value") or 0), min_pct=min_pct, max_pct=max_pct)
    return max(weekly, daily)


async def expected_r_per_week(db: Any) -> Dict[str, Any]:
    """R per week the approved, tradeable pairs earned in their backtests."""
    from app.agents import pair_gate, room
    from app.models.database import PairApproval

    focus = set(await pair_gate.room_symbols(db))
    rows = (await db.execute(select(PairApproval).where(PairApproval.approved == True))).scalars().all()  # noqa: E712
    total, parts = 0.0, []
    for r in rows:
        if focus and r.symbol not in focus:
            continue
        ok, _ = await pair_gate.supported(db, r.symbol)
        if not ok or not r.weeks or not r.trades or r.expectancy_r is None:
            continue
        per_week = r.expectancy_r * r.trades / r.weeks
        total += per_week
        parts.append({"symbol": r.symbol, "timeframe": r.timeframe, "r_per_week": round(per_week, 3),
                      "trades_per_week": round(r.trades / r.weeks, 2)})
    return {"r_per_week": round(total, 3), "pairs": parts}


async def state(db: Any, *, fresh: bool = False) -> Dict[str, Any]:
    """Target, progress and the pacing multiplier the next order is sized with."""
    if not fresh and _cache["value"] is not None and time.time() - _cache["at"] < _TTL_S:
        return _cache["value"]
    from app.agents.execution import get_settings, mt5_targets

    s = await get_settings(db)
    target = float(getattr(s, "weekly_target_usd", 0) or 0)
    try:
        accounts = (await mt5_targets(db, s))["targets"]
        pnl = await room_pnl_this_week(db, accounts)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[target] P&L unavailable: {exc}")
        accounts, pnl = [], {"realised": 0.0, "floating": 0.0, "total": 0.0, "by_account": {}}
    exp = await expected_r_per_week(db)
    left = week_fraction_left()
    base_risk = _base_risk_usd(s, accounts)
    max_mult = float(getattr(s, "target_max_multiplier", 2.0) or 2.0)
    remaining = max(0.0, target - pnl["realised"])
    # Open profit counts before sizing up: a week sitting on a winning book is
    # not behind, even if nothing has been banked yet.
    pace_remaining = max(0.0, target - pnl["total"])
    weekly_mult = 1.0
    if getattr(s, "target_pacing", True) and target > 0:
        weekly_mult = multiplier(remaining_usd=pace_remaining, expected_r_left=exp["r_per_week"] * left,
                                 base_risk_usd=base_risk, max_mult=max_mult)

    # ── the day: a band on the whole portfolio (or, with no band set, the old
    #    minimum as a % of the margin in use) ──
    margin_used = sum(float(getattr(a, "margin", 0) or 0) for a in accounts)
    daily_pct = float(getattr(s, "daily_target_margin_pct", 0) or 0)
    try:
        today = await room_pnl_this_week(db, accounts, since=day_start_utc())
    except Exception:  # noqa: BLE001
        today = {"realised": 0.0, "floating": 0.0, "total": 0.0}
    value_now = await portfolio_value(db, s, accounts)
    band = portfolio_day(value_now=value_now, today_usd=today["total"],
                         min_pct=float(getattr(s, "daily_target_min_pct", 10.0) or 0),
                         max_pct=float(getattr(s, "daily_target_max_pct", 20.0) or 0))
    daily_target = band["target_usd"] if band["min_pct"] > 0 else round(margin_used * daily_pct / 100.0, 2)
    day_left = day_fraction_left()
    daily_mult = 1.0
    if getattr(s, "target_pacing", True) and daily_target > 0:
        daily_mult = multiplier(remaining_usd=max(0.0, daily_target - today["total"]),
                                expected_r_left=exp["r_per_week"] / 5.0 * day_left,
                                base_risk_usd=base_risk, max_mult=max_mult)
    mult = max(weekly_mult, daily_mult)
    value = {
        "target_usd": target, "progress": pnl, "remaining_usd": round(remaining, 2),
        "progress_pct": round(min(1.0, pnl["realised"] / target), 4) if target > 0 else None,
        "hit": target > 0 and pnl["realised"] >= target, "week_left": round(left, 3),
        "daily": {"target_usd": daily_target, "margin_pct": daily_pct, "margin_in_use": round(margin_used, 2),
                  "progress": today, "remaining_usd": round(max(0.0, daily_target - today["realised"]), 2),
                  "hit": daily_target > 0 and today["realised"] >= daily_target, "day_left": round(day_left, 3),
                  "multiplier": daily_mult, **band, "target_usd": daily_target,
                  "remaining_total_usd": round(max(0.0, daily_target - today["total"]), 2)},
        "expected": exp, "base_risk_usd": round(base_risk, 2),
        "expected_week_usd": round(exp["r_per_week"] * base_risk, 2),
        "pacing": bool(getattr(s, "target_pacing", True)), "multiplier": mult, "weekly_multiplier": weekly_mult,
        "max_multiplier": max_mult,
        # The weekly minimum never stops the room; the day's cap does, until tomorrow.
        "entries_paused": False,
        "day_capped": band["capped"],
    }
    value["pairs"] = await _pair_rows(db, s, accounts, pnl, today, margin_used, daily_pct)
    if getattr(s, "target_per_pair", True) and value["pairs"]:
        value["per_pair"] = True
        value["book_target_usd"] = round(target * len(value["pairs"]), 2)
    _cache.update(at=time.time(), value=value)
    return value


async def _pair_rows(db: Any, s: Any, accounts: List[Any], week: Dict[str, Any], today: Dict[str, Any],
                     margin_used: float, daily_pct: float) -> List[Dict[str, Any]]:
    """Per traded pair: weekly minimum, progress, the risk % it is sized at, daily minimum."""
    from app.agents import pair_gate, room
    from app.agents.scope import normalise
    from plugins.MT5TradingPlugin.backend.models import MT5Position
    from app.trading.order_tags import SOURCE_ROOM, classify

    target = float(getattr(s, "weekly_target_usd", 0) or 0)
    if not getattr(s, "target_per_pair", True):
        target = target / max(1, len(room.get_focus_symbols()))
    # Margin share per pair ≈ its share of the room's open notional.
    notional: Dict[str, float] = {}
    try:
        for acct in accounts:
            for p in (await db.execute(select(MT5Position).where(MT5Position.account_id == acct.id))).scalars().all():
                if classify(p.comment)["source"] == SOURCE_ROOM:
                    notional[normalise(p.symbol)] = notional.get(normalise(p.symbol), 0.0) + \
                        abs(float(p.volume or 0) * float(p.price_open or 0))
    except Exception:  # noqa: BLE001
        pass
    total_notional = sum(notional.values()) or 0.0
    equity = 0.0
    if accounts:
        demo_id = getattr(s, "mt5_demo_account_id", None)
        acct = next((a for a in accounts if a.id == demo_id), accounts[0])
        equity = float(getattr(acct, "equity", 0) or getattr(acct, "balance", 0) or 0)
    from app.agents import pair_gate as _pg

    rows = []
    for sym in await _pg.room_symbols(db):
        ok, _ = await pair_gate.supported(db, sym)
        rpw = await pair_r_per_week(db, sym)
        if not ok or rpw <= 0:
            continue
        wk = week.get("by_symbol", {}).get(sym, {"realised": 0.0, "floating": 0.0})
        td = today.get("by_symbol", {}).get(sym, {"realised": 0.0, "floating": 0.0})
        share = notional.get(sym, 0.0) / total_notional if total_notional else 0.0
        daily = round(margin_used * share * daily_pct / 100.0, 2)
        risk = band_risk(target_usd=target, progress_usd=wk["realised"] + wk["floating"], r_per_week=rpw,
                         week_left=week_fraction_left(), equity=equity,
                         min_pct=float(getattr(s, "min_risk_pct", 1.0) or 1.0),
                         max_pct=float(getattr(s, "max_risk_pct", 5.0) or 5.0))
        rows.append({
            "symbol": sym, "weekly_target_usd": round(target, 2), "week": wk, "today": td,
            "hit": wk["realised"] >= target > 0, "r_per_week": round(rpw, 3),
            "expected_week_usd": round(rpw * equity * risk / 100.0, 2), "risk_pct": risk,
            "daily_target_usd": daily,
        })
    return rows


def _base_risk_usd(s: Any, accounts: List[Any]) -> float:
    """Dollar risk of one normal-size trade on the primary demo (or first) account."""
    if not accounts:
        return 0.0
    demo_id = getattr(s, "mt5_demo_account_id", None)
    acct = next((a for a in accounts if a.id == demo_id), accounts[0])
    equity = float(getattr(acct, "equity", 0) or getattr(acct, "balance", 0) or 0)
    risk = equity * float(getattr(s, "risk_pct", 1.0) or 1.0) / 100.0
    cap = float(getattr(s, "max_usd_risk_per_trade", 0) or 0)
    if cap <= 0 and equity > 300_000:
        cap = 9000.0
    return min(risk, cap) if cap > 0 else risk


def reset_cache() -> None:
    _cache.update(at=0.0, value=None)


async def pair_r_per_week(db: Any, symbol: str) -> float:
    """R per week this pair earned across every timeframe its backtests approved."""
    from app.agents.scope import normalise
    from app.models.database import PairApproval

    rows = (await db.execute(select(PairApproval).where(
        PairApproval.symbol == normalise(symbol), PairApproval.approved == True,  # noqa: E712
    ))).scalars().all()
    return sum(r.expectancy_r * r.trades / r.weeks for r in rows
               if r.weeks and r.trades and r.expectancy_r is not None and r.expectancy_r > 0)


def band_risk(*, target_usd: float, progress_usd: float, r_per_week: float, week_left: float,
              equity: float, min_pct: float, max_pct: float) -> float:
    """Risk % that makes the pair's expected rest-of-week R cover its remaining
    minimum, clamped to the band. Pure."""
    remaining = max(0.0, target_usd - progress_usd)
    if remaining <= 0 or equity <= 0:
        return min_pct
    if r_per_week <= 0:
        return min_pct
    expected_r = r_per_week * max(week_left, 0.2)
    need = remaining / expected_r / equity * 100.0
    return round(max(min_pct, min(max_pct, need)), 3)


async def pair_risk_pct(db: Any, s: Any, symbol: str, equity: float) -> float:
    from app.agents.execution import mt5_targets
    from app.agents.scope import normalise

    min_pct = float(getattr(s, "min_risk_pct", 1.0) or 1.0)
    max_pct = float(getattr(s, "max_risk_pct", 5.0) or 5.0)
    target = float(getattr(s, "weekly_target_usd", 0) or 0)
    if not getattr(s, "target_per_pair", True):
        from app.agents import room

        target = target / max(1, len(room.get_focus_symbols()))
    progress = 0.0
    try:
        pnl = await room_pnl_this_week(db, (await mt5_targets(db, s))["targets"])
        cell = pnl.get("by_symbol", {}).get(normalise(symbol), {})
        progress = float(cell.get("realised", 0)) + float(cell.get("floating", 0))
    except Exception:  # noqa: BLE001
        progress = 0.0
    return band_risk(target_usd=target, progress_usd=progress, r_per_week=await pair_r_per_week(db, symbol),
                     week_left=week_fraction_left(), equity=equity, min_pct=min_pct, max_pct=max_pct)
