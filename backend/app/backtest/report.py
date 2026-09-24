"""PDF report for a backtest run: summary, equity curves and every trade."""
from __future__ import annotations

import io
import time
from typing import Any, Dict, List

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

_BOOKS = {"oblivion": "OBLIVION", "agents": "OBLIVION + agents"}
_GREEN, _RED, _GREY = colors.HexColor("#15803d"), colors.HexColor("#b91c1c"), colors.HexColor("#64748b")


def _t(ts: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(ts)))
    except (TypeError, ValueError):
        return "—"


def _pct(x: Any) -> str:
    return "—" if x is None else f"{float(x) * 100:.1f}%"


def _money(x: Any) -> str:
    return "—" if x is None else f"{float(x):,.2f}"


def _grid(data: List[List[Any]], widths: List[float] | None = None, font: int = 8) -> Table:
    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, -1), "Helvetica", font),
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", font),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f5f9")]),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return table


def _equity_png(results: Dict[str, Any], start_balance: float) -> bytes:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 3.2), dpi=110)
    for sym, res in results.items():
        for book, curve in (res.get("equity") or {}).items():
            if not curve:
                continue
            xs = [time.gmtime(c[0]) for c in curve]
            xs = [time.mktime(x) / 86400 for x in xs]
            ax.plot(xs, [c[1] for c in curve], label=f"{sym} · {_BOOKS.get(book, book)}",
                    linestyle="-" if book == "oblivion" else "--", linewidth=1.2)
    ax.axhline(start_balance, color="#94a3b8", linewidth=0.8)
    ax.set_ylabel("Balance")
    ax.set_xticks([])
    ax.legend(fontsize=6, ncol=4, loc="upper left")
    ax.set_title("Equity per pair (each pair starts from the same balance)", fontsize=9)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def build_pdf(run: Dict[str, Any]) -> bytes:
    """``run`` = {id, params, totals, results{symbol: backtest result}}."""
    styles = getSampleStyleSheet()
    h1, h2, body = styles["Title"], styles["Heading2"], styles["BodyText"]
    body.fontSize = 8.5
    params, totals, results = run["params"], run["totals"], run["results"]
    traded = run.get("traded") or {}
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm,
                            topMargin=10 * mm, bottomMargin=10 * mm,
                            title=f"OBLIVION backtest #{run.get('id')}")
    story: List[Any] = [
        Paragraph(f"OBLIVION backtest #{run.get('id')} — {params['timeframe']}", h1),
        Paragraph(
            f"Pairs ({len(results)}): <b>{', '.join(results)}</b> · Start balance {_money(params['start_balance'])} per pair · "
            f"Risk {params['risk_pct']}% per trade · Profile: {params.get('profile_label', 'custom')} · "
            f"Agents rule: {params.get('agents_rule')} · Generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}",
            body),
        Paragraph(
            "Entries fill at the next bar's open or on the pullback limit, stops are hit before targets inside a "
            "bar, and a typical spread per pair is charged on entry and exit. On the room profiles a trade that "
            "reaches TP1 has its stop locked at TP1 and then trails behind each closed bar, riding past TP3 until "
            "the trail is hit; any exit above the entry counts as a win.", body),
        Spacer(1, 4 * mm),
        Paragraph("Profit and loss across all pairs", h2),
    ]
    head = ["Scope", "Book", "Pairs", "Trades", "Wins", "Losses", "B/E", "Win rate", "Total profit",
            "Total losses", "Net P&L", "Return", "Avg R", "Profit factor", "Days ≥90% win"]
    rows = [head]
    pnl_styles = []

    def _book_rows(scope: str, book_totals: Dict[str, Any]) -> None:
        for book, t in book_totals.items():
            tg = t.get("targets") or {}
            rows.append([scope, _BOOKS.get(book, book), t.get("pairs"), t["trades"], t["wins"], t["losses"],
                         t["breakeven"], _pct(t["win_rate"]), _money(t.get("gross_win")),
                         _money(-float(t.get("gross_loss") or 0) or 0.0), _money(t["net_profit"]),
                         f"{t['return_pct']:.2f}%", f"{t['avg_r']:.3f}" if t.get("avg_r") is not None else "—",
                         f"{t['profit_factor']:.2f}" if t.get("profit_factor") else "—",
                         f"{tg['days_wr90']}/{tg['days_decided']}" if tg.get("days_decided") else "—"])
            n = len(rows) - 1
            pnl_styles.extend([("TEXTCOLOR", (8, n), (8, n), _GREEN), ("TEXTCOLOR", (9, n), (9, n), _RED),
                           ("TEXTCOLOR", (10, n), (10, n), _GREEN if t["net_profit"] >= 0 else _RED),
                           ("FONT", (10, n), (10, n), "Helvetica-Bold", 8)])

    if traded:
        _book_rows("Traded book", traded.get("totals") or {})
    _book_rows("All pairs tested", totals)
    table = _grid(rows)
    table.setStyle(TableStyle(pnl_styles))
    story.append(table)
    if traded:
        story.append(Paragraph(
            f"<b>Traded book</b> = the pairs the room is allowed to trade on {params['timeframe']}: their own "
            f"backtest won at least {float(traded.get('bar') or 0) * 100:.0f}% of decided trades over 10+ trades "
            f"with a positive expectancy. <b>All pairs tested</b> includes the pairs that failed that bar and "
            f"are never traded live.", body))
        if traded.get("rejected"):
            story.append(Paragraph("Not traded: " + "; ".join(
                f"{sym} ({why})" for sym, why in traded["rejected"].items()), body))
    trows = [["Book", "Weekly min", "Weeks hit", "Avg week", "Best / worst week", "Losing weeks",
              "Daily min", "Days hit", "Avg day"]]
    for book, t in totals.items():
        tg = t.get("targets") or {}
        if tg:
            trows.append([_BOOKS.get(book, book), _money(tg["weekly_target"]), f"{tg['weeks_hit']}/{tg['weeks']}",
                          _money(tg["avg_week"]), f"{_money(tg['best_week'])} / {_money(tg['worst_week'])}",
                          tg["losing_weeks"], _money(tg["daily_target"]), f"{tg['days_hit']}/{tg['trading_days']}",
                          _money(tg["avg_day"])])
    if len(trows) > 1:
        story += [Spacer(1, 4 * mm), Paragraph("Minimum targets — how often the history met them", h2), _grid(trows)]
    story += [Spacer(1, 4 * mm), Paragraph("Per pair", h2)]
    rows = [["Pair", "Traded", "Book", "Bars", "From", "To", "Trades", "Wins", "Losses", "Win rate",
             "Profit", "Losses $", "Net profit", "End balance", "Max DD", "Avg R"]]
    approved = traded.get("approved") or {}
    for sym, res in results.items():
        for book, s in (res.get("summary") or {}).items():
            closed = [t for t in (res.get("trades") or {}).get(book, []) if t.get("status") == "closed"]
            gain = sum(t["pnl"] for t in closed if t["pnl"] > 0)
            loss = sum(t["pnl"] for t in closed if t["pnl"] <= 0)
            rows.append([sym, ("yes" if sym in approved else "no") if traded else "—", _BOOKS.get(book, book),
                         res.get("bars"), _t(res.get("from"))[:10], _t(res.get("to"))[:10],
                         s["trades"], s["wins"], s["losses"], _pct(s["win_rate"]), _money(gain), _money(loss),
                         _money(s["net_profit"]), _money(s["end_balance"]), f"{s['max_drawdown_pct']:.2f}%",
                         f"{s['avg_r']:.3f}" if s.get("avg_r") is not None else "—"])
    story.append(_grid(rows))
    try:
        story += [Spacer(1, 4 * mm), Image(io.BytesIO(_equity_png(results, params["start_balance"])),
                                             width=260 * mm, height=83 * mm)]
    except Exception:  # noqa: BLE001 — a chart failure must not lose the report
        pass

    for sym, res in results.items():
        for book, trades in (res.get("trades") or {}).items():
            closed = [t for t in trades if t.get("status") == "closed"]
            story += [PageBreak(), Paragraph(f"{sym} — {_BOOKS.get(book, book)}: {len(closed)} trades", h2)]
            rows = [["#", "Side", "Kind", "Signal", "Entry time", "Fill", "Stop", "TP1", "TP3", "Exit time",
                     "Exit", "Reached", "R", "P&L", "Balance"]]
            styles_extra = []
            for n, t in enumerate(closed, 1):
                rows.append([n, t["action"].upper(), t.get("kind") or "", _t(t.get("signal_time")),
                             _t(t.get("entry_time")), f"{t.get('fill', 0):.5g}", f"{t.get('initial_stop', 0):.5g}",
                             f"{t.get('tp1', 0):.5g}", f"{t.get('tp3', 0):.5g}", _t(t.get("exit_time")),
                             t.get("exit_reason", ""), t.get("reached", 0), f"{t['r']:+.3f}",
                             _money(t["pnl"]), _money(t.get("equity_after"))])
                colour = _GREEN if t["outcome"] == "win" else _RED if t["outcome"] == "loss" else _GREY
                styles_extra.append(("TEXTCOLOR", (12, n), (13, n), colour))
            if len(rows) == 1:
                story.append(Paragraph("No trades.", body))
                continue
            table = _grid(rows, font=7)
            table.setStyle(TableStyle(styles_extra))
            story.append(table)
            vetoed = [t for t in trades if t.get("status") in ("vetoed", "missed", "skipped")]
            if vetoed:
                story.append(Paragraph(
                    f"{sum(1 for t in vetoed if t['status'] == 'vetoed')} signals vetoed by the agents, "
                    f"{sum(1 for t in vetoed if t['status'] == 'missed')} pullback entries never filled, "
                    f"{sum(1 for t in vetoed if t['status'] == 'skipped')} skipped (opened past the stop/TP1).", body))
    doc.build(story)
    return buf.getvalue()


# ── portfolio report ──────────────────────────────────────────────────────

_PF_BOOKS = {
    "wf_paced": "Honest: pairs chosen before the window · paced to the day target",
    "wf_flat": "Honest: pairs chosen before the window · band floor only",
    "paced": "Today's approved pairs (chosen on this history) · paced",
    "flat": "Today's approved pairs · band floor only",
}


def _day_curve_png(days: List[Dict[str, Any]], lo: float, hi: float) -> bytes:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 2.8), dpi=110)
    pcts = [d["pct"] for d in days]
    ax.bar(range(len(pcts)), pcts, color=["#15803d" if x >= 0 else "#b91c1c" for x in pcts])
    if lo > 0:
        ax.axhline(lo, color="#0ea5e9", linewidth=0.9, linestyle="--", label=f"day minimum {lo:g}%")
    if hi > 0:
        ax.axhline(hi, color="#a855f7", linewidth=0.9, linestyle=":", label=f"day cap {hi:g}%")
    ax.set_ylabel("% of the day's start value")
    ax.set_xticks([])
    ax.legend(fontsize=7, loc="upper left")
    ax.set_title("Honest book, paced — each bar is one day", fontsize=9)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def build_portfolio_pdf(doc: Dict[str, Any]) -> bytes:
    styles = getSampleStyleSheet()
    h1, h2, body = styles["Title"], styles["Heading2"], styles["BodyText"]
    body.fontSize = 8.5
    p, w, totals = doc["params"], doc["window"], doc.get("totals") or {}
    buf = io.BytesIO()
    pdf = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm,
                            topMargin=10 * mm, bottomMargin=10 * mm, title=f"OBLIVION portfolio backtest #{doc.get('id')}")
    story: List[Any] = [
        Paragraph(f"OBLIVION portfolio backtest #{doc.get('id')} — your accounts, last {p['days']} days", h1),
        Paragraph(
            f"Window {_t(w['from'])[:10]} → {_t(w['to'])[:10]} · risk {p['min_risk_pct']:g}–{p['max_risk_pct']:g}% "
            f"per trade · day minimum {p['daily_min_pct']:g}% of the portfolio, cap {p['daily_max_pct']:g}% (no new "
            f"entries after it) · daily win-rate floor {p['daily_floor'] * 100:.0f}% · max {p['max_open']} open · "
            f"Generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}", body),
        Paragraph(
            "Every trade is one the room's OBLIVION profiles took in the pair backtests (costs and the news gate "
            "included), replayed in time order on each account's real equity: its result in R times the dollars it "
            "risked. <b>Honest</b> rows choose the pairs only on what happened before the window — that is the "
            "result to expect. <b>Today's approved pairs</b> were chosen on this same history and read high. "
            "Margin and broker lot limits are not modelled.", body),
        Spacer(1, 3 * mm), Paragraph("Whole portfolio", h2),
    ]
    rows = [["Book", "Accounts", "Start value", "End value", "Total profit", "Total losses", "Net P&L", "Return",
             "Trades", "Wins", "Losses", "Win rate"]]
    colour = []
    for key, label in _PF_BOOKS.items():
        t = totals.get(key) or {}
        if not t:
            continue
        rows.append([Paragraph(label, body), t["accounts"], _money(t["start_equity"]), _money(t["end_equity"]),
                     _money(t["gross_win"]), _money(-float(t["gross_loss"]) or 0.0), _money(t["net_profit"]),
                     f"{t['return_pct']:+.2f}%", t["trades"], t["wins"], t["losses"], _pct(t["win_rate"])])
        n = len(rows) - 1
        colour += [("TEXTCOLOR", (4, n), (4, n), _GREEN), ("TEXTCOLOR", (5, n), (5, n), _RED),
                   ("TEXTCOLOR", (6, n), (6, n), _GREEN if t["net_profit"] >= 0 else _RED)]
    table = _grid(rows, widths=[70 * mm] + [None] * 11)
    table.setStyle(TableStyle(colour))
    story.append(table)

    story += [Spacer(1, 3 * mm), Paragraph("Per account — the day target", h2)]
    rows = [["Account", "Room trades it", "Start equity", "Book", "Net P&L", "Avg day", f"Days ≥{p['daily_min_pct']:g}%",
             f"Days capped {p['daily_max_pct']:g}%", "Losing days", "Best / worst day", "Max DD", "Avg risk"]]
    spans: List[Any] = []
    for a in doc.get("accounts") or []:
        name = f"{a['name']} {a.get('login') or ''} ({a['type']})"
        if a.get("skipped"):
            rows.append([name, "yes" if a.get("room_trades_it") else "no", _money(a["start_equity"]),
                         a["skipped"], "", "", "", "", "", "", "", ""])
            spans.append(("SPAN", (3, len(rows) - 1), (11, len(rows) - 1)))
            continue
        for key in ("wf_paced", "wf_flat", "paced"):
            s = (a.get(key) or {}).get("summary")
            if not s:
                continue
            rows.append([name, "yes" if a.get("room_trades_it") else "no (what-if)", _money(s["start_equity"]),
                         {"wf_paced": "honest, paced", "wf_flat": "honest, floor", "paced": "approved, paced"}[key],
                         _money(s["net_profit"]), f"{s['avg_day_pct']:+.2f}%",
                         f"{s['days_min_hit']}/{s['days_with_closes']}", f"{s['days_capped']}", s["losing_days"],
                         f"{s['best_day_pct']:+.2f}% / {s['worst_day_pct']:+.2f}%", f"{s['max_drawdown_pct']:.2f}%",
                         f"{s['avg_risk_pct']:.2f}%"])
    table = _grid(rows)
    table.setStyle(TableStyle(spans))
    story.append(table)
    lead = next((a for a in doc.get("accounts") or [] if a.get("wf_paced")), None)
    if lead:
        try:
            story += [Spacer(1, 3 * mm), Image(io.BytesIO(_day_curve_png(lead["wf_paced"]["days"], p["daily_min_pct"],
                                                                          p["daily_max_pct"])),
                                               width=260 * mm, height=73 * mm)]
        except Exception:  # noqa: BLE001
            pass

    for a in doc.get("accounts") or []:
        book = a.get("wf_paced")
        if not book:
            continue
        story += [PageBreak(), Paragraph(f"{a['name']} {a.get('login') or ''} — honest book, paced: every day", h2)]
        rows = [["Day", "Start value", "P&L", "% of start", "Wins", "Losses", "Opened", "Minimum hit", "Capped"]]
        for d in book["days"]:
            rows.append([d["date"], _money(d["start"]), _money(d["pnl"]), f"{d['pct']:+.2f}%", d["wins"], d["losses"],
                         d["opened"], "yes" if d["min_hit"] else "", "yes" if d["capped"] else ""])
        story.append(_grid(rows, font=7))
        story += [PageBreak(), Paragraph(f"{a['name']} {a.get('login') or ''} — honest book, paced: every trade", h2)]
        rows = [["#", "Pair", "TF", "Entry", "Exit", "Outcome", "R", "Risk %", "Risked", "P&L"]]
        extra = []
        for n, t in enumerate(book["trades"], 1):
            rows.append([n, t["symbol"], t["timeframe"], _t(t["entry_time"]), _t(t["exit_time"]), t["outcome"],
                         f"{t['r']:+.3f}", f"{t['risk_pct']:.2f}%", _money(t["risk_usd"]), _money(t["pnl"])])
            extra.append(("TEXTCOLOR", (9, n), (9, n), _GREEN if t["pnl"] >= 0 else _RED))
        table = _grid(rows, font=7)
        table.setStyle(TableStyle(extra))
        story.append(table)
    pdf.build(story)
    return buf.getvalue()
