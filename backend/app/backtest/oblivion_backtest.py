"""Backtest OBLIVION signals — and the trading room's agents on top of them.

The engine is causal (verified: every signal is identical whether or not later
bars exist), so one ``analyze_oblivion(..., full_history=True)`` pass over the
series yields exactly the signals the live room would have seen, bar by bar.

Two books are run side by side on the same candles:

* ``oblivion`` — every OBLIVION signal is taken;
* ``agents``   — the signal is taken only when the room's agents confirm it
                 (the deterministic local read the room uses as its seats'
                 fallback; a historical LLM replay is not possible).

Trades are managed the way the room manages them live: entry at the next bar's
open, the engine's stop, a partial close at each target rung, the stop moved to
break-even after TP1 and to TP1 after TP2, and a winning trade closed when the
engine confirms the opposite signal. Within a bar the stop is assumed to be hit
before any target — the pessimistic reading of an OHLC bar.
"""
from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.signals.oblivion import OblivionConfig, analyze_oblivion

WARMUP_BARS = 210

#: Cost per side as a fraction of price — typical retail spread + commission.
#: Gold ~0.35 USD round trip, CADJPY ~2 pips, crypto CFDs wider.
DEFAULT_COST_PCT = {"XAUUSD": 0.00004, "XAGUSD": 0.0001, "CADJPY": 0.00009,
                    "BTCUSD": 0.00015, "ETHUSD": 0.0002}
FALLBACK_COST_PCT = 0.0001


def cost_for(symbol: str) -> float:
    key = (symbol or "").upper().replace("/", "").replace("USDT", "USD")
    return DEFAULT_COST_PCT.get(key, FALLBACK_COST_PCT)


@dataclass
class BacktestParams:
    symbol: str
    timeframe: str
    start_balance: float = 10_000.0
    risk_pct: float = 1.0
    #: Fractions of the position closed at TP1 / TP2; the rest runs to TP3.
    tp1_close: float = 0.5
    tp2_close: float = 0.25
    be_after_tp1: bool = True
    trail_after_tp2: bool = True
    close_on_opposite: bool = True
    #: Cost per side as a fraction of price; None = the pair's typical spread.
    cost_pct: Optional[float] = None
    #: Bars after which an unresolved trade is closed at the bar's close.
    max_hold_bars: Optional[int] = None
    kinds: tuple = ("scalp", "sniper")
    min_score: int = 0
    #: Only trade in the direction of OBLIVION's structural market bias.
    require_bias: bool = False
    #: UTC hours a signal may fire in (None = any).
    hours_utc: Optional[tuple] = None
    #: Higher-timeframe OBLIVION signals ``[(known_at_s, action), ...]`` the
    #: signal must agree with — the 2h call leads, this timeframe times it.
    confluence: Optional[List[Tuple[int, str]]] = None
    confluence_max_age_s: int = 8 * 7200
    #: Only trade with the trend of this higher timeframe (EMA 21 vs 50 on its
    #: last *closed* bar, built from these candles). None = off.
    trend_tf: Optional[str] = None
    #: Skip signals when ATR is outside [min, max] × its 200-bar median (None = off).
    atr_ratio_min: Optional[float] = None
    atr_ratio_max: Optional[float] = None
    #: none | opposite (veto only a local read on the other side) | strict (must agree)
    agents_rule: str = "opposite"
    #: Enter on a pullback: a limit this many ATRs back toward the stop, valid
    #: for ``entry_wait_bars`` bars (0 = market at the next bar's open).
    #: After TP1: trail the stop this many ATRs behind the close (None = the
    #: fixed ladder), never below TP1 when ``floor_at_tp1``, and let the trade
    #: run past TP3 when ``hold_past_tp3`` — the trail is then the only exit.
    trail_atr: Optional[float] = None
    #: The trail starts after this rung (1 = TP1, 2 = TP2); until then the stop
    #: sits at the TP1 floor.
    trail_from: int = 1
    floor_at_tp1: bool = False
    hold_past_tp3: bool = False
    entry_pullback_atr: float = 0.0
    entry_wait_bars: int = 3
    #: High-impact events [(ts, currency, title)]; no entries from ``news_pre_min``
    #: before to ``news_cooloff_min`` after one on the pair's currencies.
    news_events: Optional[List[Tuple[int, str, str]]] = None
    news_pre_min: int = 30
    news_cooloff_min: int = 60
    cfg_overrides: Dict[str, Any] = field(default_factory=dict)


def _cfg(overrides: Dict[str, Any]) -> OblivionConfig:
    base = None
    try:
        from app.services.oblivion_settings import current_config

        base = current_config()
    except Exception:  # noqa: BLE001
        base = None
    cfg = base or OblivionConfig()
    data = {**asdict(cfg), **(overrides or {})}
    return OblivionConfig(**{k: v for k, v in data.items() if k in OblivionConfig.__dataclass_fields__})


def _agents_verdict(rows: Sequence[Sequence[float]], i: int, sig: Dict[str, Any],
                    cfg: OblivionConfig, timeframe: str, symbol: str) -> Dict[str, Any]:
    """The room's local seat read at bar ``i``, from the candles up to it only."""
    from app.agents.local_analysis import analyze_locally
    from app.signals.technical import analyze as technical_analyze

    window = [list(r) for r in rows[max(0, i - 299): i + 1]]
    try:
        technical = technical_analyze(window, timeframe)
    except Exception:  # noqa: BLE001
        technical = {}
    context = {
        "symbol": symbol,
        "technical": technical,
        "current_price": float(rows[i][4]),
        "oblivion": {
            "mode": cfg.trade_mode,
            "score": {"buy": sig.get("score_buy"), "sell": sig.get("score_sell"),
                      "scalp_min": cfg.scalp_min_score, "sniper_min": cfg.sniper_min_score},
            "market_bias": sig.get("bias"),
            "bias_label": {1: "BULLISH", -1: "BEARISH"}.get(sig.get("bias"), "NEUTRAL"),
            "signal": {"action": sig["action"], "kind": sig.get("kind"), "new": True, "confirmed": True,
                       "score": sig.get("score"), "entry": sig.get("entry"),
                       "stop_loss": sig.get("stop_loss"), "tp1": sig.get("tp1")},
            "news": {"blocked": False},
        },
    }
    try:
        d = analyze_locally(role="signal_generator", context=context)
    except Exception as exc:  # noqa: BLE001
        return {"action": "hold", "confidence": 0.0, "reasoning": f"local read failed: {exc}"}
    return {"action": str(d.get("action") or "hold").lower(), "confidence": float(d.get("confidence") or 0),
            "reasoning": str(d.get("reasoning") or "")[:400]}


def _agents_confirm(rule: str, action: str, verdict: Dict[str, Any]) -> bool:
    got = verdict.get("action")
    if rule == "none":
        return True
    if rule == "strict":
        return got == action
    opposite = "sell" if action == "buy" else "buy"
    return got != opposite


def _ts_s(value: Any) -> int:
    v = float(value)
    return int(v // 1000) if v > 1e11 else int(v)


def _news_block(p: "BacktestParams", t: int):
    if not p.news_events:
        return None
    from app.signals.news_gate import blocking_event, currencies_for

    return blocking_event(p.news_events, currencies_for(p.symbol), t, p.news_pre_min, p.news_cooloff_min)


def passes_filters(s: Dict[str, Any], p: BacktestParams) -> bool:
    """The room's entry filters on top of the engine's own rules."""
    if s["index"] < WARMUP_BARS or s.get("kind") not in p.kinds or (s.get("score") or 0) < p.min_score:
        return False
    if p.require_bias and s.get("bias") != (1 if s["action"] == "buy" else -1):
        return False
    if p.hours_utc is not None:
        hour = time.gmtime(int(s["time"])).tm_hour
        if hour not in p.hours_utc:
            return False
    if p.confluence is not None:
        known = int(s["time"]) + _tf_s(p.timeframe)
        lead = None
        for at, action in p.confluence:
            if at > known:
                break
            lead = (at, action)
        if lead is None or lead[1] != s["action"] or known - lead[0] > p.confluence_max_age_s:
            return False
    return True


def atr_series(rows: Sequence[Sequence[float]], n: int = 14) -> List[float]:
    """Wilder ATR per bar, from that bar and the ones before it only."""
    atr: List[float] = []
    prev_close = None
    value = None
    for r in rows:
        h, l, c = float(r[2]), float(r[3]), float(r[4])
        tr = h - l if prev_close is None else max(h - l, abs(h - prev_close), abs(l - prev_close))
        value = tr if value is None else (value * (n - 1) + tr) / n
        atr.append(value)
        prev_close = c
    return atr


def atr_ratio(rows: Sequence[Sequence[float]], n: int = 14, window: int = 200) -> List[float]:
    """ATR(n) divided by its trailing median — >1 is a hotter-than-usual market."""
    import statistics

    atr = atr_series(rows, n)
    out: List[float] = []
    for i, a in enumerate(atr):
        past = atr[max(0, i - window): i + 1]
        med = statistics.median(past) if past else a
        out.append(a / med if med else 1.0)
    return out


def htf_trend(rows: Sequence[Sequence[float]], base_tf: str, trend_tf: str) -> List[int]:
    """Per base bar: +1/-1/0 from EMA21 vs EMA50 of the last closed ``trend_tf`` bar."""
    step = _tf_s(trend_tf) * 1000
    closes: List[float] = []
    starts: List[int] = []
    for r in rows:
        start = int(r[0]) - int(r[0]) % step
        if not starts or start != starts[-1]:
            starts.append(start)
            closes.append(float(r[4]))
        else:
            closes[-1] = float(r[4])

    def _ema(values: List[float], n: int) -> List[float]:
        k, out, prev = 2 / (n + 1), [], None
        for v in values:
            prev = v if prev is None else v * k + prev * (1 - k)
            out.append(prev)
        return out

    fast, slow = _ema(closes, 21), _ema(closes, 50)
    sign = [0 if j < 50 else (1 if fast[j] > slow[j] else -1 if fast[j] < slow[j] else 0) for j in range(len(closes))]
    out: List[int] = []
    j = 0
    for r in rows:
        start = int(r[0]) - int(r[0]) % step
        while j + 1 < len(starts) and starts[j + 1] <= start:
            j += 1
        # The bucket this bar belongs to is still forming — use the one before it.
        out.append(sign[j - 1] if j >= 1 else 0)
    return out


def _tf_s(tf: str) -> int:
    from app.services.candles import timeframe_seconds

    return int(timeframe_seconds(tf))


def htf_leads(rows: Sequence[Sequence[float]], timeframe: str, p: "BacktestParams") -> List[Tuple[int, str]]:
    """The higher timeframe's filtered OBLIVION signals, as ``(known_at, action)``."""
    cfg = _cfg(p.cfg_overrides)
    res = analyze_oblivion([list(r) for r in rows], timeframe, cfg, last_bar_forming=False, full_history=True)
    step = _tf_s(timeframe)
    return [(int(sg["time"]) + step, sg["action"]) for sg in res.get("signals_history") or []
            if passes_filters(sg, p)]


class _Book:
    def __init__(self, name: str, p: BacktestParams):
        self.name, self.p = name, p
        self.equity = p.start_balance
        self.peak = p.start_balance
        self.max_dd = 0.0
        self.pos: Optional[Dict[str, Any]] = None
        self.pending: Optional[Dict[str, Any]] = None
        self.rr = (0.5, 1.0, 1.5)
        self.atr: List[float] = []
        self.trades: List[Dict[str, Any]] = []
        self.curve: List[List[float]] = []

    def open(self, sig: Dict[str, Any], i: int, rows, agents: Optional[Dict[str, Any]],
             price: Optional[float] = None, levels: Optional[Dict[str, float]] = None) -> None:
        o = float(rows[i][1]) if price is None else float(price)
        is_long = sig["action"] == "buy"
        stop = float(sig["stop_loss"])
        slip = o * self.p.cost_pct
        fill = o + slip if is_long else o - slip
        risk = (fill - stop) if is_long else (stop - fill)
        # Targets are measured from the actual fill, so "TP1 = x R" holds for
        # a pullback entry as much as for a market one.
        sgn = 1.0 if is_long else -1.0
        if levels:
            # A resting limit: the broker holds exactly the planned levels.
            tp1, tp2, tp3 = levels["tp1"], levels["tp2"], levels["tp3"]
        else:
            tp1, tp2, tp3 = (fill + sgn * rr * risk for rr in self.rr) if risk > 0 else (sig["tp1"], sig["tp2"], sig["tp3"])
        # The bar opened through the stop or past the engine's TP1: not an entry.
        sig_tp1 = float(sig["tp1"])
        if risk <= 0 or (is_long and o >= sig_tp1) or (not is_long and o <= sig_tp1):
            self.trades.append({**self._base(sig, i, rows, agents), "status": "skipped",
                                "exit_reason": "opened beyond the stop or TP1", "r": 0.0, "pnl": 0.0})
            return
        from app.signals.oblivion_profiles import MIN_RISK_FRAC

        # A stop inside a few spreads (flat, ~0-ATR data) or a blown account: never sized.
        if risk < max(MIN_RISK_FRAC * fill, 8 * slip) or self.equity <= 0:
            self.trades.append({**self._base(sig, i, rows, agents), "status": "skipped",
                                "exit_reason": "stop too close to size" if self.equity > 0 else "account blown",
                                "r": 0.0, "pnl": 0.0})
            return
        self.pos = {
            **self._base(sig, i, rows, agents), "status": "open", "is_long": is_long,
            "fill": fill, "stop": stop, "initial_stop": stop, "risk": risk,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "remaining": 1.0, "realised_r": 0.0, "reached": 0,
            "risk_amount": self.equity * self.p.risk_pct / 100.0, "fills": [],
        }

    def _base(self, sig, i, rows, agents) -> Dict[str, Any]:
        return {
            "signal_time": sig["time"], "entry_time": int(rows[i][0] // 1000) if rows[i][0] > 1e11 else int(rows[i][0]),
            "action": sig["action"], "kind": sig.get("kind"), "score": sig.get("score"),
            "entry": sig.get("entry"), "stop_loss": sig.get("stop_loss"),
            "tp1": sig.get("tp1"), "tp2": sig.get("tp2"), "tp3": sig.get("tp3"),
            "agents": agents, "entry_index": i,
        }

    def _close_part(self, frac: float, price: float, why: str, t: int) -> None:
        pos = self.pos
        frac = min(frac, pos["remaining"])
        if frac <= 0:
            return
        slip = price * self.p.cost_pct
        exit_px = price - slip if pos["is_long"] else price + slip
        move = (exit_px - pos["fill"]) if pos["is_long"] else (pos["fill"] - exit_px)
        pos["realised_r"] += frac * move / pos["risk"]
        pos["remaining"] -= frac
        pos["fills"].append({"time": t, "price": round(price, 6), "fraction": round(frac, 3), "reason": why})

    def manage(self, i: int, rows, opposite_signal: bool) -> None:
        pos = self.pos
        if pos is None:
            return
        _, o, h, l, c = (float(x) for x in rows[i][:5])
        t = int(rows[i][0] // 1000) if rows[i][0] > 1e11 else int(rows[i][0])
        lng = pos["is_long"]
        hit_stop = (l <= pos["stop"]) if lng else (h >= pos["stop"])
        if hit_stop:
            if pos["reached"] == 0:
                reason = "stop loss"
            elif self.p.floor_at_tp1 and abs(pos["stop"] - pos["tp1"]) < 1e-12:
                reason = "TP1 floor stop"
            elif abs(pos["stop"] - pos["fill"]) < 1e-12:
                reason = "break-even stop"
            else:
                reason = "trailed stop"
            self._close_part(pos["remaining"], pos["stop"], reason, t)
            return self._finish(i, rows, reason)
        for k, key, frac in ((1, "tp1", self.p.tp1_close), (2, "tp2", self.p.tp2_close), (3, "tp3", 1.0)):
            if pos["reached"] >= k:
                continue
            level = pos[key]
            if (h >= level) if lng else (l <= level):
                pos["reached"] = k
                if not (k == 3 and self.p.hold_past_tp3):
                    self._close_part(pos["remaining"] if k == 3 else frac, level, key.upper(), t)
                if k == 1 and self.p.be_after_tp1:
                    pos["stop"] = pos["fill"]
                if k == 1 and self.p.floor_at_tp1:
                    pos["stop"] = pos["tp1"]
                if k == 2 and self.p.trail_after_tp2:
                    pos["stop"] = pos["tp1"]
                if pos["remaining"] <= 1e-9:
                    return self._finish(i, rows, key.upper())
        if self.p.trail_atr and pos["reached"] >= max(1, self.p.trail_from) and i < len(self.atr) and self.atr[i] > 0:
            trail = c - self.p.trail_atr * self.atr[i] if lng else c + self.p.trail_atr * self.atr[i]
            floor = pos["tp1"] if self.p.floor_at_tp1 else pos["stop"]
            best = max(trail, floor, pos["stop"]) if lng else min(trail, floor, pos["stop"])
            pos["stop"] = best
        in_profit = (c > pos["fill"]) if lng else (c < pos["fill"])
        if opposite_signal and self.p.close_on_opposite and in_profit:
            self._close_part(pos["remaining"], c, "opposite OBLIVION signal", t)
            return self._finish(i, rows, "opposite OBLIVION signal")
        if self.p.max_hold_bars and i - pos["entry_index"] >= self.p.max_hold_bars:
            self._close_part(pos["remaining"], c, "time exit", t)
            return self._finish(i, rows, "time exit")

    def _finish(self, i: int, rows, reason: str) -> None:
        pos = self.pos
        r = pos["realised_r"]
        pnl = r * pos["risk_amount"]
        self.equity += pnl
        self.peak = max(self.peak, self.equity)
        if self.peak > 0:
            self.max_dd = max(self.max_dd, (self.peak - self.equity) / self.peak)
        t = int(rows[i][0] // 1000) if rows[i][0] > 1e11 else int(rows[i][0])
        outcome = "win" if r > 0.001 else "loss" if r < -0.001 else "breakeven"
        self.trades.append({
            **{k: v for k, v in pos.items() if k not in ("is_long", "remaining", "realised_r", "risk_amount")},
            "status": "closed", "exit_time": t, "exit_reason": reason, "bars": i - pos["entry_index"],
            "r": round(r, 4), "pnl": round(pnl, 2), "outcome": outcome, "equity_after": round(self.equity, 2),
        })
        self.curve.append([t, round(self.equity, 2)])
        self.pos = None

    def summary(self) -> Dict[str, Any]:
        closed = [t for t in self.trades if t["status"] == "closed"]
        wins = [t for t in closed if t["outcome"] == "win"]
        losses = [t for t in closed if t["outcome"] == "loss"]
        be = [t for t in closed if t["outcome"] == "breakeven"]
        gross_win = sum(t["pnl"] for t in closed if t["pnl"] > 0)
        gross_loss = -sum(t["pnl"] for t in closed if t["pnl"] < 0)
        decided = len(wins) + len(losses)
        return {
            "book": self.name, "trades": len(closed), "wins": len(wins), "losses": len(losses),
            "breakeven": len(be), "skipped": sum(1 for t in self.trades if t["status"] == "skipped"),
            "win_rate": round(len(wins) / decided, 4) if decided else None,
            "win_rate_incl_be": round(len(wins) / len(closed), 4) if closed else None,
            "net_profit": round(self.equity - self.p.start_balance, 2),
            "return_pct": round((self.equity / self.p.start_balance - 1) * 100, 2),
            "end_balance": round(self.equity, 2),
            "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
            "avg_r": round(sum(t["r"] for t in closed) / len(closed), 4) if closed else None,
            "expectancy_r": round(sum(t["r"] for t in closed) / len(closed), 4) if closed else None,
            "max_drawdown_pct": round(self.max_dd * 100, 2),
            "tp1_reached": sum(1 for t in closed if (t.get("reached") or 0) >= 1),
            "tp2_reached": sum(1 for t in closed if (t.get("reached") or 0) >= 2),
            "tp3_reached": sum(1 for t in closed if (t.get("reached") or 0) >= 3),
            "news_deferred": sum(1 for t in self.trades if t.get("news")),
            "open_at_end": self.pos is not None,
        }


def run_backtest(rows: Sequence[Sequence[float]], p: BacktestParams, *, with_agents: bool = True) -> Dict[str, Any]:
    """Replay OBLIVION over ``rows`` for both books. Pure — no I/O."""
    started = time.time()
    if p.cost_pct is None:
        p.cost_pct = cost_for(p.symbol)
    cfg = _cfg(p.cfg_overrides)
    rows = [list(r) for r in rows]
    res = analyze_oblivion(rows, p.timeframe, cfg, last_bar_forming=False, full_history=True)
    if res.get("error"):
        return {"error": res["error"], "params": asdict(p)}
    signals = [s for s in res.get("signals_history") or [] if passes_filters(s, p)]
    if p.atr_ratio_min is not None or p.atr_ratio_max is not None:
        ratio = atr_ratio(rows)
        lo = p.atr_ratio_min if p.atr_ratio_min is not None else 0.0
        hi = p.atr_ratio_max if p.atr_ratio_max is not None else float("inf")
        signals = [s for s in signals if lo <= ratio[s["index"]] <= hi]
    if p.trend_tf:
        trend = htf_trend(rows, p.timeframe, p.trend_tf)
        signals = [s for s in signals if trend[s["index"]] == (1 if s["action"] == "buy" else -1)]
    by_index: Dict[int, Dict[str, Any]] = {}
    for s in signals:
        by_index[s["index"]] = s

    books = {"oblivion": _Book("oblivion", p)}
    if with_agents:
        books["agents"] = _Book("agents", p)
    for b in books.values():
        b.rr = (cfg.tp1_rr, cfg.tp2_rr, cfg.tp3_rr)
        if p.trail_atr:
            b.atr = atr_series(rows)
    agent_reads: Dict[int, Dict[str, Any]] = {}

    n = len(rows)
    for i in range(WARMUP_BARS + 1, n):
        prev = by_index.get(i - 1)
        for name, book in books.items():
            if book.pos is not None:
                opposite = prev is not None and prev["action"] != book.pos["action"]
                book.manage(i, rows, opposite)
            if book.pos is None and book.pending is not None:
                pend = book.pending
                bar_t = _ts_s(rows[i][0])
                news_now = _news_block(p, bar_t)
                if news_now is not None and bar_t >= pend.get("activate_at", 0):
                    book.trades.append({**book._base(pend["sig"], i, rows, pend["agents"]), "status": "missed",
                                        "exit_reason": f"cancelled for high-impact news ({news_now[2]})",
                                        "r": 0.0, "pnl": 0.0, "news": True})
                    book.pending = None
                    continue
                if bar_t < pend.get("activate_at", 0):
                    continue
                if (prev is not None and prev["action"] != pend["sig"]["action"]) or i > pend["expires"]:
                    book.trades.append({**book._base(pend["sig"], i, rows, pend["agents"]), "status": "missed",
                                        "exit_reason": "pullback entry never filled", "r": 0.0, "pnl": 0.0})
                    book.pending = None
                else:
                    o, lo, hi = float(rows[i][1]), float(rows[i][3]), float(rows[i][2])
                    lim = pend["limit"]
                    long_ = pend["sig"]["action"] == "buy"
                    px = (o if o <= lim else lim if lo <= lim else None) if long_ else \
                         (o if o >= lim else lim if hi >= lim else None)
                    if px is not None:
                        book.pending = None
                        book.open(pend["sig"], i, rows, pend["agents"], price=px, levels=pend.get("levels"))
                        if book.pos is not None:
                            book.manage(i, rows, False)
                        continue
            if book.pos is None and book.pending is None and prev is not None:
                agents = None
                if name == "agents":
                    if (i - 1) not in agent_reads:
                        agent_reads[i - 1] = _agents_verdict(rows, i - 1, prev, cfg, p.timeframe, p.symbol)
                    agents = agent_reads[i - 1]
                    if not _agents_confirm(p.agents_rule, prev["action"], agents):
                        book.trades.append({**book._base(prev, i, rows, agents), "status": "vetoed",
                                            "exit_reason": f"agents: {agents['action']}", "r": 0.0, "pnl": 0.0})
                        continue
                if p.entry_pullback_atr > 0:
                    from app.signals.oblivion_profiles import entry_plan

                    plan = entry_plan(prev, p.entry_pullback_atr, book.rr)
                    if plan:
                        pending = {"sig": prev, "limit": plan["limit"], "levels": plan,
                                   "expires": i - 1 + p.entry_wait_bars, "agents": agents}
                        # A signal inside a news window waits for the cool-off;
                        # its order is placed only if it is still fresh then.
                        blocked = _news_block(p, _ts_s(rows[i][0]))
                        if blocked is not None:
                            pending["activate_at"] = blocked[3]
                            pending["news"] = True
                            step = _tf_s(p.timeframe)
                            bars_waited = max(0, (blocked[3] - _ts_s(rows[i][0])) // step)
                            pending["expires"] = i - 1 + p.entry_wait_bars + int(bars_waited)
                        book.pending = pending
                        continue
                if _news_block(p, _ts_s(rows[i][0])) is not None:
                    book.trades.append({**book._base(prev, i, rows, agents), "status": "missed",
                                        "exit_reason": "high-impact news window", "r": 0.0, "pnl": 0.0, "news": True})
                    continue
                book.open(prev, i, rows, agents)
                if book.pos is not None:
                    book.manage(i, rows, False)

    first = rows[WARMUP_BARS][0]
    last = rows[-1][0]
    return {
        "symbol": p.symbol, "timeframe": p.timeframe, "params": asdict(p),
        "bars": n, "from": int(first // 1000) if first > 1e11 else int(first),
        "to": int(last // 1000) if last > 1e11 else int(last),
        "signals": len(signals), "engine_outcomes": res.get("outcomes"),
        "summary": {name: b.summary() for name, b in books.items()},
        "trades": {name: b.trades for name, b in books.items()},
        "equity": {name: b.curve for name, b in books.items()},
        "elapsed_s": round(time.time() - started, 2),
    }


async def fetch_history(symbol: str, timeframe: str, bars: int = 5000) -> List[list]:
    """As much history as the feeds hold, for the backtest window.

    The live feeds cap crypto at ~1000 bars, which on 15m is ten days — not a
    backtest. Crypto is then paged from a public exchange (BTC/USDT stands in
    for the BTCUSD CFD: same market, a spread apart).
    """
    from app.services import candles, market_data

    try:
        is_crypto = market_data.classify(market_data.normalize_symbol(symbol)) == market_data.CRYPTO
    except Exception:  # noqa: BLE001
        is_crypto = False
    if is_crypto:
        # One source, every time: the live feed returns 1000 or 5000 bars from
        # run to run, and a different exchange is a different set of signals.
        paged = await _paged_crypto(symbol, timeframe, bars)
        if len(paged) >= min(bars, 1000):
            return paged
    rows = [list(r) for r in (await candles.fetch(symbol, timeframe, bars)) or []]
    if len(rows) < bars * 0.8 and timeframe in ("2h", "4h"):
        # Build the timeframe from 1h bars when the feed holds more of those.
        from app.services.candles import _resample

        factor = _tf_s(timeframe) // 3600
        finer = [list(r) for r in (await candles.fetch(symbol, "1h", bars * factor)) or []]
        folded = _resample(finer, "1h", timeframe) if finer else []
        if len(folded) > len(rows):
            rows = folded[-bars:]
    return rows


async def _paged_crypto(symbol: str, timeframe: str, bars: int) -> List[list]:
    import ccxt.async_support as ccxt_async

    base = (symbol or "").upper().replace("/", "")
    for quote in ("USDT", "USDC", "USD"):
        if base.endswith(quote):
            base = base[: -len(quote)]
            break
    pair = f"{base}/USDT"
    step = _tf_s(timeframe) * 1000
    since = int(time.time() * 1000) - bars * step
    out: Dict[int, list] = {}
    for name in ("bitget", "binance", "bybit"):
        ex = getattr(ccxt_async, name)({"enableRateLimit": True})
        try:
            cursor = since
            while cursor < int(time.time() * 1000) - step:
                chunk = await ex.fetch_ohlcv(pair, timeframe, since=cursor, limit=1000)
                if not chunk:
                    break
                for r in chunk:
                    out[int(r[0])] = [int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5] or 0)]
                nxt = int(chunk[-1][0]) + step
                if nxt <= cursor:
                    break
                cursor = nxt
        except Exception:  # noqa: BLE001 — try the next exchange
            out = {}
        finally:
            try:
                await ex.close()
            except Exception:  # noqa: BLE001
                pass
        if len(out) >= bars * 0.8:
            break
    return [out[k] for k in sorted(out)][-bars:]


def split_sample(rows: Sequence[Sequence[float]], train_frac: float = 0.6):
    """In-sample / out-of-sample halves that share the warm-up bars."""
    cut = int(len(rows) * train_frac)
    return list(rows[:cut]), list(rows[max(0, cut - WARMUP_BARS):])


def _finite(x: Any) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False
