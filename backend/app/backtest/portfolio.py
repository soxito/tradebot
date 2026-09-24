"""Portfolio backtest on the accounts the room actually trades.

The pair backtests answer "does OBLIVION work on this pair and timeframe?";
this one answers "what would the room have done to *my* accounts?". It takes
every pair/timeframe the room is allowed to trade (the approved book), the
trades each one's latest room-profile backtest made, keeps the ones an
account's broker lists, and replays them in time order on that account's real
equity with the live rules:

* risk per trade inside the band (1–5%), paced toward the portfolio day's
  minimum (default 10%) and at the band floor once it is banked;
* no new entry once the day's cap (default 20%) is banked — locked in;
* the daily win-rate floor (below 90%, only ≥97% pairs open);
* at most ``max_open_positions`` trades open at once.

A trade's result is its backtested R (costs included) times the dollars it
risked, booked when it closes. Margin and broker lot limits are not modelled —
on a large account the broker's maximum lot can cap what 5% risk asks for.
"""
from __future__ import annotations

import heapq
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from loguru import logger

from app.agents.target_pacer import day_risk

CRYPTO_BASES = ("BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "DOGE", "LTC", "AVAX", "LINK", "DOT")


@dataclass
class PortfolioParams:
    days: int = 60
    min_risk_pct: float = 1.0
    max_risk_pct: float = 5.0
    daily_min_pct: float = 10.0
    daily_max_pct: float = 20.0
    daily_floor: float = 0.90
    elite_win_rate: float = 0.97
    max_open: int = 30
    #: False = every trade at the band floor (the comparison book).
    pacing: bool = True


@dataclass
class BookTrade:
    entry_time: int
    exit_time: int
    r: float
    outcome: str
    symbol: str
    timeframe: str
    pair_win_rate: float


def _day(ts: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def simulate(trades: Sequence[BookTrade], start_equity: float, p: PortfolioParams,
             r_per_week: float) -> Dict[str, Any]:
    """Replay ``trades`` on one account. Pure."""
    events = sorted(trades, key=lambda t: (t.entry_time, t.exit_time))
    equity = start_equity
    peak, max_dd = start_equity, 0.0
    open_: List[Tuple[int, int, float, BookTrade, float]] = []  # (exit, seq, pnl, trade, risk%)
    days: Dict[str, Dict[str, Any]] = {}
    taken: List[Dict[str, Any]] = []
    skipped = {"daily cap": 0, "positions cap": 0, "daily floor": 0}
    seq = 0

    def day_row(d: str) -> Dict[str, Any]:
        if d not in days:
            days[d] = {"date": d, "start": round(equity, 2), "pnl": 0.0, "wins": 0, "losses": 0,
                       "opened": 0, "min_hit": False, "capped": False}
        return days[d]

    def settle(until: Optional[int]) -> None:
        nonlocal equity, peak, max_dd
        while open_ and (until is None or open_[0][0] <= until):
            xt, _, pnl, t, _risk = heapq.heappop(open_)
            row = day_row(_day(xt))
            equity += pnl
            row["pnl"] += pnl
            if t.outcome == "win":
                row["wins"] += 1
            elif t.outcome == "loss":
                row["losses"] += 1
            peak = max(peak, equity)
            if peak > 0:
                max_dd = max(max_dd, (peak - equity) / peak * 100.0)

    for t in events:
        settle(t.entry_time)
        if equity <= 0:
            break
        row = day_row(_day(t.entry_time))
        lo = row["start"] * p.daily_min_pct / 100.0 if p.daily_min_pct > 0 else 0.0
        cap = row["start"] * p.daily_max_pct / 100.0 if p.daily_max_pct > 0 else None
        if cap is not None and row["pnl"] >= cap:
            row["capped"] = True
            skipped["daily cap"] += 1
            continue
        if len(open_) >= p.max_open:
            skipped["positions cap"] += 1
            continue
        decided = row["wins"] + row["losses"]
        if (p.daily_floor > 0 and row["losses"] and row["wins"] / decided < p.daily_floor
                and t.pair_win_rate < p.elite_win_rate):
            skipped["daily floor"] += 1
            continue
        if not p.pacing or (lo > 0 and row["pnl"] >= lo):
            risk = p.min_risk_pct
        else:
            day_left = max(0.2, 1.0 - (t.entry_time % 86400) / 86400.0)
            risk = day_risk(remaining_usd=lo - row["pnl"], expected_r_left=r_per_week / 5.0 * day_left,
                            value=row["start"], min_pct=p.min_risk_pct, max_pct=p.max_risk_pct)
        risk_usd = equity * risk / 100.0
        pnl = t.r * risk_usd
        seq += 1
        heapq.heappush(open_, (t.exit_time, seq, pnl, t, risk))
        row["opened"] += 1
        taken.append({"symbol": t.symbol, "timeframe": t.timeframe, "entry_time": t.entry_time,
                      "exit_time": t.exit_time, "outcome": t.outcome, "r": round(t.r, 4),
                      "risk_pct": round(risk, 3), "risk_usd": round(risk_usd, 2), "pnl": round(pnl, 2)})
    settle(None)

    for row in days.values():
        start = row["start"] or 1.0
        row["pnl"] = round(row["pnl"], 2)
        row["pct"] = round(row["pnl"] / start * 100.0, 3)
        row["min_hit"] = p.daily_min_pct > 0 and row["pct"] >= p.daily_min_pct
        row["capped"] = row["capped"] or (p.daily_max_pct > 0 and row["pct"] >= p.daily_max_pct)
    rows = sorted(days.values(), key=lambda r: r["date"])
    active = [r for r in rows if r["wins"] + r["losses"]]
    wins = sum(r["wins"] for r in rows)
    losses = sum(r["losses"] for r in rows)
    gross_win = sum(t["pnl"] for t in taken if t["pnl"] > 0)
    gross_loss = -sum(t["pnl"] for t in taken if t["pnl"] < 0)
    pcts = [r["pct"] for r in active]
    return {
        "summary": {
            "start_equity": round(start_equity, 2), "end_equity": round(equity, 2),
            "net_profit": round(equity - start_equity, 2),
            "return_pct": round((equity - start_equity) / start_equity * 100.0, 3) if start_equity else 0.0,
            "gross_win": round(gross_win, 2), "gross_loss": round(gross_loss, 2),
            "trades": len(taken), "wins": wins, "losses": losses,
            "win_rate": round(wins / (wins + losses), 4) if wins + losses else None,
            "max_drawdown_pct": round(max_dd, 3),
            "days_with_closes": len(active),
            "days_min_hit": sum(1 for r in active if r["min_hit"]),
            "days_capped": sum(1 for r in active if r["capped"]),
            "losing_days": sum(1 for r in active if r["pnl"] < 0),
            "days_wr90": sum(1 for r in active if r["wins"] / (r["wins"] + r["losses"]) >= 0.90),
            "avg_day_pct": round(sum(pcts) / len(pcts), 3) if pcts else 0.0,
            "best_day_pct": max(pcts) if pcts else 0.0, "worst_day_pct": min(pcts) if pcts else 0.0,
            "avg_risk_pct": round(sum(t["risk_pct"] for t in taken) / len(taken), 3) if taken else 0.0,
            "skipped": skipped,
        },
        "days": rows,
        "trades": taken,
    }


# ── the approved book ─────────────────────────────────────────────────────


async def load_book(db: Any) -> Dict[str, Any]:
    """Every approved pair/timeframe and the closed trades of its latest backtest."""
    from sqlalchemy import select

    from app.models.database import BacktestRun, PairApproval

    rows = (await db.execute(select(PairApproval).where(PairApproval.approved == True))).scalars().all()  # noqa: E712
    by_run: Dict[int, List[Any]] = {}
    missing: List[str] = []
    for r in rows:
        if r.run_id:
            by_run.setdefault(int(r.run_id), []).append(r)
        else:
            missing.append(f"{r.symbol} {r.timeframe}")
    trades: List[BookTrade] = []
    combos: List[Dict[str, Any]] = []
    r_per_week: Dict[str, float] = {}
    for run_id, approvals in by_run.items():
        run = await db.get(BacktestRun, run_id)
        path = Path(run.result_path) if run is not None and run.result_path else None
        if path is None or not path.exists():
            missing += [f"{a.symbol} {a.timeframe}" for a in approvals]
            continue
        doc = json.loads(path.read_text())
        results = {k.upper(): v for k, v in (doc.get("results") or {}).items()}
        for a in approvals:
            res = results.get(a.symbol.upper())
            if not res:
                missing.append(f"{a.symbol} {a.timeframe}")
                continue
            closed = [t for t in (res.get("trades") or {}).get("oblivion", [])
                      if t.get("status") == "closed" and t.get("exit_time")]
            for t in closed:
                trades.append(BookTrade(int(t["entry_time"]), int(t["exit_time"]), float(t["r"]),
                                        str(t.get("outcome")), a.symbol, a.timeframe, float(a.win_rate or 0)))
            combos.append({"symbol": a.symbol, "timeframe": a.timeframe, "win_rate": a.win_rate,
                           "trades": len(closed), "run_id": run_id})
            if a.weeks and a.trades and (a.expectancy_r or 0) > 0:
                r_per_week[f"{a.symbol} {a.timeframe}"] = a.expectancy_r * a.trades / a.weeks
    return {"trades": trades, "combos": combos, "missing": missing, "r_per_week": r_per_week}


async def walk_forward_book(db: Any, cut: int, bar: float) -> Dict[str, Any]:
    """Pairs chosen only on what happened *before* ``cut`` — the honest book.

    Takes the latest room-profile run of every timeframe (all pairs, not only
    today's approvals), approves each pair/timeframe on its trades that closed
    before ``cut`` with the live rule (``pair_gate.judge``), and keeps its
    trades from ``cut`` on. Nothing after ``cut`` influences the choice.
    """
    from sqlalchemy import desc, select

    from app.agents.pair_gate import judge
    from app.models.database import BacktestRun
    from app.signals.oblivion_profiles import ROOM_SCAN_TIMEFRAMES

    trades: List[BookTrade] = []
    combos: List[Dict[str, Any]] = []
    r_per_week: Dict[str, float] = {}
    for tf in ROOM_SCAN_TIMEFRAMES:
        runs = (await db.execute(select(BacktestRun).where(
            BacktestRun.timeframe == tf, BacktestRun.status == "done").order_by(desc(BacktestRun.id)).limit(5))).scalars().all()
        run = next((r for r in runs if r.result_path and Path(r.result_path).exists()
                    and json.loads(r.params_json or "{}").get("use_room_profile", True)), None)
        if run is None:
            continue
        doc = json.loads(Path(run.result_path).read_text())
        for sym, res in (doc.get("results") or {}).items():
            closed = [t for t in (res.get("trades") or {}).get("oblivion", [])
                      if t.get("status") == "closed" and t.get("exit_time")]
            before = [t for t in closed if int(t["exit_time"]) < cut]
            w = sum(1 for t in before if t.get("outcome") == "win")
            lo = sum(1 for t in before if t.get("outcome") == "loss")
            avg_r = sum(float(t["r"]) for t in before) / len(before) if before else None
            ok, _ = judge({"trades": len(before), "win_rate": w / (w + lo) if w + lo else None, "avg_r": avg_r}, bar)
            if not ok:
                continue
            wr = w / (w + lo) if w + lo else 0.0
            after = [t for t in closed if int(t["entry_time"]) >= cut]
            for t in after:
                trades.append(BookTrade(int(t["entry_time"]), int(t["exit_time"]), float(t["r"]),
                                        str(t.get("outcome")), sym, tf, wr))
            first = min(int(t["entry_time"]) for t in before)
            weeks = max(1e-9, (cut - first) / 604800.0)
            r_per_week[f"{sym} {tf}"] = (avg_r or 0) * len(before) / weeks
            combos.append({"symbol": sym, "timeframe": tf, "win_rate": round(wr, 4), "trades": len(after),
                           "in_sample_trades": len(before), "run_id": run.id})
    return {"trades": trades, "combos": combos, "missing": [], "r_per_week": r_per_week}


async def account_symbols(acct: Any, venue: str, symbols: Sequence[str]) -> Dict[str, bool]:
    """Which of ``symbols`` this account's broker/exchange lists."""
    out: Dict[str, bool] = {}
    if venue == "crypto":
        for sym in symbols:
            key = sym.upper()
            out[sym] = key.endswith(("USDT", "USDC")) or any(key.startswith(b) for b in CRYPTO_BASES)
        return out
    from app.agents.execution import _mt5_symbol
    from plugins.MT5TradingPlugin.backend.services.mt5_client import mt5_client

    for sym in symbols:
        try:
            out[sym] = bool(await mt5_client.resolve_symbol(acct.login, acct.server, acct.password_encrypted,
                                                            _mt5_symbol(sym)))
        except Exception as exc:  # noqa: BLE001 — unreachable terminal: count it as not listed
            logger.debug(f"[portfolio-bt] {sym} on {acct.server}: {exc}")
            out[sym] = False
    return out


async def run_portfolio(db: Any, p: PortfolioParams) -> Dict[str, Any]:
    """Simulate the approved book on every account the room trades."""
    from app.agents.execution import get_settings, is_demo_account, mt5_targets
    from app.services.crypto_accounts import crypto_targets, is_demo

    from sqlalchemy import select

    from app.models.database import CryptoAccount
    from plugins.MT5TradingPlugin.backend.models import MT5Account

    s = await get_settings(db)
    book = await load_book(db)
    if not book["trades"]:
        return {"error": "no approved pair has backtest trades yet — run the pair backtests first", "params": asdict(p)}
    end = max(t.exit_time for t in book["trades"])
    start = end - p.days * 86400
    window = [t for t in book["trades"] if t.entry_time >= start]
    wf = await walk_forward_book(db, start, float(getattr(s, "pair_min_win_rate", 0.95) or 0.95))
    wf_window = [t for t in wf["trades"] if t.entry_time >= start]
    symbols = sorted({t.symbol for t in window} | {t.symbol for t in wf_window})

    # Every configured account: the ones the room trades now, and the rest as a what-if.
    trading = {("mt5", a.id) for a in (await mt5_targets(db, s))["targets"]}
    trading |= {("crypto", a.id) for a in (await crypto_targets(db, s))["targets"]}
    targets: List[Tuple[str, Any]] = [("mt5", a) for a in (await db.execute(select(MT5Account))).scalars().all()]
    try:
        targets += [("crypto", a) for a in (await db.execute(select(CryptoAccount))).scalars().all()]
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[portfolio-bt] crypto accounts unavailable: {exc}")
    accounts: List[Dict[str, Any]] = []
    for venue, acct in targets:
        equity = float(getattr(acct, "equity", 0) or getattr(acct, "balance", 0) or 0)
        demo = is_demo_account(acct) if venue == "mt5" else is_demo(acct)
        info = {"venue": venue, "id": acct.id, "name": getattr(acct, "name", None) or getattr(acct, "exchange", ""),
                "login": getattr(acct, "login", None), "server": getattr(acct, "server", None),
                "type": "demo" if demo else "live", "currency": getattr(acct, "currency", "USD"),
                "start_equity": round(equity, 2), "room_trades_it": (venue, acct.id) in trading}
        if equity <= 0:
            accounts.append({**info, "skipped": "no equity on the account — nothing to trade"})
            continue
        listed = await account_symbols(acct, venue, symbols)
        info["symbols"] = sorted(k for k, v in listed.items() if v)
        info["not_listed"] = sorted(k for k, v in listed.items() if not v)
        flat = PortfolioParams(**{**asdict(p), "pacing": False})
        row = dict(info)
        for key, src, trades in (("paced", book, window), ("wf_paced", wf, wf_window)):
            mine = [t for t in trades if listed.get(t.symbol)]
            if not mine:
                continue
            rpw = sum(v for k, v in src["r_per_week"].items() if listed.get(k.split(" ")[0]))
            row[key] = simulate(mine, equity, p, rpw)
            row[key.replace("paced", "flat")] = {"summary": simulate(mine, equity, flat, rpw)["summary"]}
            row.setdefault("r_per_week", round(rpw, 3))
        if "paced" not in row and "wf_paced" not in row:
            row["skipped"] = "the account lists none of the traded pairs"
        accounts.append(row)

    def total(book_key: str) -> Dict[str, Any]:
        rows = [a[book_key]["summary"] for a in accounts if book_key in a]
        if not rows:
            return {}
        st = sum(r["start_equity"] for r in rows)
        net = sum(r["net_profit"] for r in rows)
        w, l = sum(r["wins"] for r in rows), sum(r["losses"] for r in rows)
        return {"accounts": len(rows), "start_equity": round(st, 2), "end_equity": round(st + net, 2),
                "net_profit": round(net, 2), "return_pct": round(net / st * 100.0, 3) if st else 0.0,
                "gross_win": round(sum(r["gross_win"] for r in rows), 2),
                "gross_loss": round(sum(r["gross_loss"] for r in rows), 2),
                "trades": sum(r["trades"] for r in rows), "wins": w, "losses": l,
                "win_rate": round(w / (w + l), 4) if w + l else None}

    return {
        "kind": "portfolio", "params": asdict(p),
        "window": {"from": start, "to": end, "days": p.days},
        "book": {"combos": book["combos"], "missing": book["missing"], "trades_in_window": len(window)},
        "walk_forward": {"combos": wf["combos"], "trades_in_window": len(wf_window)},
        "accounts": accounts,
        "totals": {k: total(k) for k in ("paced", "flat", "wf_paced", "wf_flat")},
    }


async def params_from_settings(db: Any, days: int) -> PortfolioParams:
    from app.agents.execution import get_settings

    s = await get_settings(db)
    return PortfolioParams(
        days=days,
        min_risk_pct=float(getattr(s, "min_risk_pct", 1.0) or 1.0),
        max_risk_pct=float(getattr(s, "max_risk_pct", 5.0) or 5.0),
        daily_min_pct=float(getattr(s, "daily_target_min_pct", 10.0) or 0),
        daily_max_pct=float(getattr(s, "daily_target_max_pct", 20.0) or 0),
        daily_floor=float(getattr(s, "daily_min_win_rate", 0.9) or 0),
        elite_win_rate=float(getattr(s, "daily_recovery_min_wr", 0.97) or 0),
        max_open=int(getattr(s, "max_open_positions", 30) or 30),
    )
