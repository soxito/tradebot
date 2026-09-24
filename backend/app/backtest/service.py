"""Backtest jobs: run, store, report (PDF), and deliver to Telegram."""
from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from app.backtest.oblivion_backtest import BacktestParams, fetch_history, run_backtest

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "backtests"
_tasks: Dict[int, asyncio.Task] = {}
#: One run at a time: universe runs hold every pair's candles in memory, and
#: six at once pushed a nearly full disk into swap exhaustion.
_one_at_a_time: Optional[asyncio.Semaphore] = None


async def mark_interrupted() -> int:
    """Runs still 'running' at startup died with the previous process."""
    import os

    from sqlalchemy import update

    if os.environ.get("PYTEST_CURRENT_TEST"):
        return 0  # the test app shares the production database

    from app.core.database import AsyncSessionLocal
    from app.models.database import BacktestRun

    async with AsyncSessionLocal() as db:
        res = await db.execute(update(BacktestRun).where(BacktestRun.status == "running")
                               .values(status="error", error="interrupted by a backend restart"))
        await db.commit()
        return int(res.rowcount or 0)


def _totals(results: Dict[str, Any], start_balance: float) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for res in results.values():
        for book, s in (res.get("summary") or {}).items():
            t = out.setdefault(book, {"trades": 0, "wins": 0, "losses": 0, "breakeven": 0,
                                      "net_profit": 0.0, "r_sum": 0.0, "gross_win": 0.0, "gross_loss": 0.0,
                                      "pairs": 0})
            t["pairs"] += 1
            for k in ("trades", "wins", "losses", "breakeven"):
                t[k] += s[k]
            t["net_profit"] += s["net_profit"]
            t["r_sum"] += (s["avg_r"] or 0) * s["trades"]
            for trade in (res.get("trades") or {}).get(book, []):
                if trade.get("status") == "closed":
                    if trade["pnl"] > 0:
                        t["gross_win"] += trade["pnl"]
                    else:
                        t["gross_loss"] -= trade["pnl"]
    for t in out.values():
        decided = t["wins"] + t["losses"]
        t["win_rate"] = round(t["wins"] / decided, 4) if decided else None
        t["avg_r"] = round(t["r_sum"] / t["trades"], 4) if t["trades"] else None
        t["profit_factor"] = round(t["gross_win"] / t["gross_loss"], 3) if t["gross_loss"] > 0 else None
        t["net_profit"] = round(t["net_profit"], 2)
        t["gross_win"], t["gross_loss"] = round(t["gross_win"], 2), round(t["gross_loss"], 2)
        t["return_pct"] = round(t["net_profit"] / (start_balance * max(1, t["pairs"])) * 100, 2)
    return out


async def _traded_book(results: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    """Totals over the pairs the room would actually trade on this timeframe —
    the ones whose own backtest clears the approval bar — plus why the rest
    were left out. The all-pairs totals stay alongside for comparison."""
    from app.agents.pair_gate import MIN_WIN_RATE, judge

    bar = MIN_WIN_RATE
    try:
        from app.agents.execution import get_settings
        from app.core.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            bar = float(getattr(await get_settings(db), "pair_min_win_rate", MIN_WIN_RATE) or MIN_WIN_RATE)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[backtest] approval bar unavailable, using {bar}: {exc}")
    approved, rejected = {}, {}
    for sym, res in results.items():
        s = (res.get("summary") or {}).get("oblivion")
        ok, why = judge(s, bar) if s else (False, res.get("error") or "no result")
        (approved if ok else rejected)[sym] = why
    sub = {sym: results[sym] for sym in approved}
    totals = _totals(sub, float(params["start_balance"])) if sub else {}
    targets = target_stats(sub, float(params.get("weekly_target_usd") or 0),
                           float(params.get("daily_target_usd") or 0)) if sub else {}
    for book, t in totals.items():
        t["targets"] = targets.get(book)
    return {"bar": bar, "approved": approved, "rejected": rejected, "totals": totals}


def target_stats(results: Dict[str, Any], weekly_target: float, daily_target: float) -> Dict[str, Any]:
    """Per book: how many weeks / trading days of the backtest met the minimum targets."""
    import time as _time

    out: Dict[str, Any] = {}
    starts = [r.get("from") for r in results.values() if r.get("from")]
    ends = [r.get("to") for r in results.values() if r.get("to")]
    if not starts:
        return out
    first, last = min(starts), max(ends)
    week0 = first - ((first - 4 * 86400) % 604800)        # Monday 00:00 UTC (1970-01-05 was a Monday)
    books: Dict[str, List[Dict[str, Any]]] = {}
    for res in results.values():
        for book, trades in (res.get("trades") or {}).items():
            books.setdefault(book, []).extend(t for t in trades if t.get("status") == "closed" and t.get("exit_time"))
    for book, trades in books.items():
        weeks: Dict[int, float] = {}
        w = week0
        while w <= last:
            weeks[w] = 0.0
            w += 604800
        days: Dict[str, float] = {}
        day_wl: Dict[str, List[int]] = {}
        for t in trades:
            wk = t["exit_time"] - ((t["exit_time"] - week0) % 604800)
            weeks[wk] = weeks.get(wk, 0.0) + t["pnl"]
            day = _time.strftime("%Y-%m-%d", _time.gmtime(t["exit_time"]))
            days[day] = days.get(day, 0.0) + t["pnl"]
            wl = day_wl.setdefault(day, [0, 0])
            if t.get("outcome") == "win":
                wl[0] += 1
            elif t.get("outcome") == "loss":
                wl[1] += 1
        trading_days = sum(1 for d in range(int(first // 86400), int(last // 86400) + 1)
                           if _time.gmtime(d * 86400).tm_wday < 5)
        wv = list(weeks.values())
        out[book] = {
            "weekly_target": weekly_target, "weeks": len(wv),
            "weeks_hit": sum(1 for v in wv if weekly_target > 0 and v >= weekly_target),
            "avg_week": round(sum(wv) / len(wv), 2) if wv else 0.0,
            "best_week": round(max(wv), 2) if wv else 0.0, "worst_week": round(min(wv), 2) if wv else 0.0,
            "losing_weeks": sum(1 for v in wv if v < 0),
            "daily_target": daily_target, "trading_days": trading_days, "days_with_closes": len(days),
            "days_hit": sum(1 for v in days.values() if daily_target > 0 and v >= daily_target),
            "avg_day": round(sum(days.values()) / trading_days, 2) if trading_days else 0.0,
            # Days whose closed trades won at least 90% of the time (wins / (wins + losses)).
            "days_decided": sum(1 for w, l in day_wl.values() if w + l),
            "days_wr90": sum(1 for w, l in day_wl.values() if w + l and w / (w + l) >= 0.90),
            "losing_days": sum(1 for v in days.values() if v < 0),
        }
    return out


async def execute(run_id: int, symbols: List[str], params: Dict[str, Any], *, send_telegram: bool) -> None:
    """Queue behind any run in progress, then run it."""
    global _one_at_a_time
    if _one_at_a_time is None:
        _one_at_a_time = asyncio.Semaphore(1)
    async with _one_at_a_time:
        await _execute(run_id, symbols, params, send_telegram=send_telegram)


async def _execute(run_id: int, symbols: List[str], params: Dict[str, Any], *, send_telegram: bool) -> None:
    """Run the backtest for every pair, then store, report and deliver."""
    from app.agents import brain_log
    from app.core.database import AsyncSessionLocal
    from app.core.timezone import now_sast
    from app.models.database import BacktestRun
    from app.signals.oblivion_profiles import backtest_kwargs

    tf = params["timeframe"]
    results: Dict[str, Any] = {}
    error: Optional[str] = None
    news_events: List[Any] = []
    traded: Optional[Dict[str, Any]] = None
    try:
        for sym in symbols:
            rows = await fetch_history(sym, tf, int(params.get("bars") or 5000))
            kw = backtest_kwargs(tf) if params.get("use_room_profile", True) else {}
            if kw and rows:
                if not news_events:
                    from app.signals.news_gate import history

                    try:
                        first = int(rows[0][0] // 1000) if rows[0][0] > 1e11 else int(rows[0][0])
                        news_events = await history(first - 86400, int(__import__("time").time()) + 86400)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"[backtest] news history unavailable — run without the news gate: {exc}")
                kw["news_events"] = news_events
            p = BacktestParams(symbol=sym, timeframe=tf, start_balance=float(params["start_balance"]),
                               risk_pct=float(params["risk_pct"]), agents_rule=params.get("agents_rule", "opposite"),
                               **kw)
            res = await asyncio.to_thread(run_backtest, rows, p, with_agents=True)
            if res.get("error"):
                results[sym] = {"error": res["error"], "summary": {}, "trades": {}, "equity": {}}
                continue
            res["candles"] = [[int(r[0] // 1000) if r[0] > 1e11 else int(r[0]), r[1], r[2], r[3], r[4]]
                              for r in rows]
            results[sym] = res
        totals = _totals(results, float(params["start_balance"]))
        targets = target_stats(results, float(params.get("weekly_target_usd") or 0),
                               float(params.get("daily_target_usd") or 0))
        for book, t in totals.items():
            t["targets"] = targets.get(book)
        if params.get("use_room_profile", True) and backtest_kwargs(tf):
            traded = await _traded_book(results, params)
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"[backtest] run {run_id} failed")
        error, totals = str(exc)[:500], {}

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    result_path = DATA_DIR / f"run_{run_id}.json"
    pdf_path = DATA_DIR / f"oblivion_backtest_{run_id}_{tf}.pdf"
    params = {**params, "profile_label": f"room {tf} profile" if params.get("use_room_profile", True)
              and backtest_kwargs(tf) else "global OBLIVION settings"}
    doc = {"id": run_id, "params": params, "totals": totals, "results": results, "traded": traded}
    pdf_bytes: Optional[bytes] = None
    if not error:
        result_path.write_text(json.dumps(doc, default=str))
        try:
            from app.backtest.report import build_pdf

            pdf_bytes = await asyncio.to_thread(build_pdf, doc)
            pdf_path.write_bytes(pdf_bytes)
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[backtest] PDF for run {run_id} failed")
            error = f"report failed: {exc}"

    brain = {}
    if not error:
        brain = await brain_log.log_backtest(doc)
        if params.get("use_room_profile", True) and backtest_kwargs(tf):
            from app.agents import pair_gate

            async with AsyncSessionLocal() as db:
                await pair_gate.record_backtest(db, doc)
        logger.info(f"[backtest] run {run_id} stored in the AI brain: {brain}")

    sent = False
    if pdf_bytes and send_telegram:
        sent = await send_report(run_id, pdf_bytes, doc)

    async with AsyncSessionLocal() as db:
        row = await db.get(BacktestRun, run_id)
        if row is not None:
            row.status = "error" if error else "done"
            row.error = error
            row.summary_json = json.dumps({"totals": totals, "traded": traded, "pairs": {
                s: r.get("summary") for s, r in results.items()}}, default=str)
            row.result_path = str(result_path) if not error else None
            row.pdf_path = str(pdf_path) if pdf_bytes else None
            row.telegram_sent = sent
            row.finished_at = now_sast()
            await db.commit()
    if totals:
        line = "; ".join(f"{b}: {t['trades']} trades, {_fmt_pct(t['win_rate'])} win, net {t['net_profit']:+,.2f}"
                         for b, t in totals.items())
        await brain_log.record_decision(
            agent_name="Backtester", agent_role="backtest", symbol=",".join(symbols),
            action="backtest", confidence=float((totals.get("oblivion") or {}).get("win_rate") or 0),
            reasoning=f"Backtest #{run_id} {tf} ({params['profile_label']}): {line}",
            market_data={"run_id": run_id, "totals": totals}, ai_called=False, origin="backtest",
        )


def _fmt_pct(x: Any) -> str:
    return "n/a" if x is None else f"{float(x) * 100:.1f}%"


async def send_report(run_id: int, pdf_bytes: bytes, doc: Dict[str, Any]) -> bool:
    from plugins.TelegramSignalNewsPlugin.backend.services.notifications import notify_document

    p, totals = doc["params"], doc["totals"]
    traded = doc.get("traded") or {}
    lines = [f"📊 <b>OBLIVION backtest #{run_id}</b> — {p['timeframe']} · {len(doc['results'])} pairs",
             f"Start {p['start_balance']:,.0f} per pair · risk {p['risk_pct']}% · {p.get('profile_label')}"]
    ob = totals.get("oblivion") or {}
    if ob:
        lines.append(f"<b>All pairs</b>: profit {ob['gross_win']:+,.2f} · losses {-ob['gross_loss']:,.2f} · "
                     f"net <b>{ob['net_profit']:+,.2f}</b> · {ob['wins']}W / {ob['losses']}L "
                     f"({_fmt_pct(ob['win_rate'])})")
    tb = (traded.get("totals") or {}).get("oblivion")
    if traded:
        if tb:
            tg = tb.get("targets") or {}
            lines.append(f"<b>Traded book</b> ({len(traded['approved'])} pairs ≥ {traded['bar'] * 100:.0f}%): "
                         f"profit {tb['gross_win']:+,.2f} · losses {-tb['gross_loss']:,.2f} · net "
                         f"<b>{tb['net_profit']:+,.2f}</b> · {tb['wins']}W / {tb['losses']}L "
                         f"({_fmt_pct(tb['win_rate'])})"
                         + (f" · days ≥90% win: {tg['days_wr90']}/{tg['days_decided']}" if tg else ""))
        else:
            lines.append(f"<b>Traded book</b>: no pair cleared the {traded['bar'] * 100:.0f}% bar — nothing traded")
    for book, t in totals.items():
        if book == "oblivion":
            continue
        lines.append(f"OBLIVION + agents (all pairs): {t['wins']}W / {t['losses']}L · {_fmt_pct(t['win_rate'])} · "
                     f"net {t['net_profit']:+,.2f}")
    rejected = list((traded or {}).get("rejected") or [])
    if rejected:
        # Last and plain text: Telegram cuts captions at 1024 characters.
        lines.append(f"Not traded ({len(rejected)}): {', '.join(rejected[:12])}" + (" …" if len(rejected) > 12 else ""))
    return await notify_document(pdf_bytes, f"oblivion_backtest_{run_id}_{p['timeframe']}.pdf", "\n".join(lines))


async def start(symbols: List[str], params: Dict[str, Any], *, send_telegram: bool) -> int:
    from app.core.database import AsyncSessionLocal
    from app.models.database import BacktestRun

    async with AsyncSessionLocal() as db:
        row = BacktestRun(status="running", symbols=",".join(symbols), timeframe=params["timeframe"],
                          start_balance=float(params["start_balance"]), risk_pct=float(params["risk_pct"]),
                          params_json=json.dumps(params))
        db.add(row)
        await db.commit()
        await db.refresh(row)
        run_id = row.id
    task = asyncio.create_task(execute(run_id, symbols, params, send_telegram=send_telegram))
    _tasks[run_id] = task
    task.add_done_callback(lambda _t: _tasks.pop(run_id, None))
    return run_id


def load_result(path: Optional[str]) -> Optional[Dict[str, Any]]:
    if not path or not Path(path).exists():
        return None
    return json.loads(Path(path).read_text())


def default_params() -> Dict[str, Any]:
    return {k: v for k, v in asdict(BacktestParams(symbol="", timeframe="2h")).items()
            if k in ("start_balance", "risk_pct", "agents_rule")}


# ── portfolio backtests: the room's book on the real accounts ─────────────


async def start_portfolio(days: int, *, send_telegram: bool) -> int:
    from app.core.database import AsyncSessionLocal
    from app.models.database import BacktestRun

    params = {"kind": "portfolio", "days": days}
    async with AsyncSessionLocal() as db:
        row = BacktestRun(status="running", symbols="portfolio", timeframe="portfolio",
                          start_balance=0.0, risk_pct=0.0, params_json=json.dumps(params))
        db.add(row)
        await db.commit()
        await db.refresh(row)
        run_id = row.id

    async def _go() -> None:
        global _one_at_a_time
        if _one_at_a_time is None:
            _one_at_a_time = asyncio.Semaphore(1)
        async with _one_at_a_time:
            await _execute_portfolio(run_id, days, send_telegram=send_telegram)

    task = asyncio.create_task(_go())
    _tasks[run_id] = task
    task.add_done_callback(lambda _t: _tasks.pop(run_id, None))
    return run_id


async def _execute_portfolio(run_id: int, days: int, *, send_telegram: bool) -> None:
    from app.agents import brain_log
    from app.backtest.portfolio import params_from_settings, run_portfolio
    from app.core.database import AsyncSessionLocal
    from app.core.timezone import now_sast
    from app.models.database import BacktestRun

    error: Optional[str] = None
    doc: Dict[str, Any] = {}
    try:
        async with AsyncSessionLocal() as db:
            doc = await run_portfolio(db, await params_from_settings(db, days))
        error = doc.get("error")
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"[backtest] portfolio run {run_id} failed")
        error = str(exc)[:500]
    doc["id"] = run_id
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    result_path = DATA_DIR / f"run_{run_id}.json"
    pdf_path = DATA_DIR / f"oblivion_portfolio_{run_id}.pdf"
    pdf_bytes: Optional[bytes] = None
    if not error:
        result_path.write_text(json.dumps(doc, default=str))
        try:
            from app.backtest.report import build_portfolio_pdf

            pdf_bytes = await asyncio.to_thread(build_portfolio_pdf, doc)
            pdf_path.write_bytes(pdf_bytes)
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[backtest] portfolio PDF for run {run_id} failed")
            error = f"report failed: {exc}"
    sent = False
    if pdf_bytes and send_telegram:
        from plugins.TelegramSignalNewsPlugin.backend.services.notifications import notify_document

        sent = await notify_document(pdf_bytes, pdf_path.name, portfolio_caption(doc))
    async with AsyncSessionLocal() as db:
        row = await db.get(BacktestRun, run_id)
        if row is not None:
            row.status = "error" if error else "done"
            row.error = error
            row.summary_json = json.dumps({"kind": "portfolio", "totals": doc.get("totals"),
                                           "window": doc.get("window")}, default=str)
            row.result_path = str(result_path) if not error else None
            row.pdf_path = str(pdf_path) if pdf_bytes else None
            row.telegram_sent = sent
            row.finished_at = now_sast()
            await db.commit()
    if not error:
        await brain_log.record_decision(
            agent_name="Backtester", agent_role="backtest", symbol="PORTFOLIO", action="backtest",
            confidence=float(((doc.get("totals") or {}).get("wf_paced") or {}).get("win_rate") or 0),
            reasoning=portfolio_caption(doc).replace("<b>", "").replace("</b>", ""),
            market_data={"run_id": run_id, "totals": doc.get("totals"), "window": doc.get("window")},
            ai_called=False, origin="backtest",
        )


def portfolio_caption(doc: Dict[str, Any]) -> str:
    p, t = doc.get("params") or {}, doc.get("totals") or {}
    lines = [f"💼 <b>Portfolio backtest #{doc.get('id')}</b> — last {p.get('days')} days on your accounts",
             f"Day band {p.get('daily_min_pct'):g}–{p.get('daily_max_pct'):g}% · risk {p.get('min_risk_pct'):g}–"
             f"{p.get('max_risk_pct'):g}% per trade"]
    labels = {"wf_paced": "Honest (pairs chosen before the window), paced",
              "wf_flat": "Honest, flat band floor", "paced": "Today's approved pairs, paced"}
    for key, label in labels.items():
        x = t.get(key) or {}
        if x:
            lines.append(f"<b>{label}</b>: {x['start_equity']:,.0f} → {x['end_equity']:,.0f} "
                         f"(net {x['net_profit']:+,.0f}, {x['return_pct']:+.1f}%) · profit {x['gross_win']:,.0f} · "
                         f"losses {-x['gross_loss']:,.0f} · {x['wins']}W/{x['losses']}L ({_fmt_pct(x['win_rate'])})")
    for a in doc.get("accounts") or []:
        s = (a.get("wf_paced") or {}).get("summary")
        if s:
            lines.append(f"{a['name']} ({a['type']}): avg day {s['avg_day_pct']:+.2f}% · days ≥{p.get('daily_min_pct'):g}%: "
                         f"{s['days_min_hit']}/{s['days_with_closes']} · worst day {s['worst_day_pct']:+.2f}% · "
                         f"max DD {s['max_drawdown_pct']:.1f}%")
        elif a.get("skipped"):
            lines.append(f"{a['name']} ({a['type']}): {a['skipped']}")
    return "\n".join(lines)
