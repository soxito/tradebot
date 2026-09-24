"""OBLIVION as the trading room's main engine.

The room no longer treats OBLIVION as one voice among many. This module is the
single way the room touches the engine:

* :func:`read_pair`   runs ``analyze_oblivion`` on the pair's own candles and
                      stores the read in ``oblivion_reads`` (the ledger that
                      proves the engine saw that pair), mirrored to the AI brain.
* :func:`verdict`     turns a read into a trade mandate — or a hard block.
* :func:`enforce`     applies the mandate to the board: agents may confirm or
                      veto the engine's side, never flip it.
* :func:`latest_read` is what execution checks before any order goes out.

A signal is only tradeable once its bar has *closed*. While the current bar is
forming, a signal that confirmed on the bar before it shows up as
``last_signal`` — that is the fresh signal the room acts on.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from loguru import logger
from sqlalchemy import desc, select

from app.agents.scope import normalise
from app.core.timezone import now_sast
from app.models.database import OblivionRead

#: A confirmed signal stays actionable for this many closed bars after it fired.
FRESH_BARS = 2
#: One slow feed must not stall a scan of every monitored pair.
FETCH_TIMEOUT_S = 40.0

_TF_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600,
    "2h": 7200, "4h": 14400, "1d": 86400, "1w": 604800,
}


def tf_seconds(timeframe: str) -> int:
    return _TF_SECONDS.get(str(timeframe or "").lower(), 3600)


def _num(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


# ── Candles ─────────────────────────────────────────────────────────────────


async def fetch_rows(symbol: str, timeframe: str, limit: int = 240) -> List[List[Any]]:
    """The same candles the board's context is built from, per instrument class."""
    from app.services import market_data

    rows: List[List[Any]] = []
    try:
        if market_data.is_universal_symbol(symbol):
            rows, _ = await market_data.fetch_ohlcv_universal(symbol, timeframe=timeframe, limit=limit)
        else:
            from app.exchanges.manager import SupportedExchange, exchange_manager

            connector = exchange_manager.get_exchange(SupportedExchange.BITGET)
            if connector:
                try:
                    rows = await connector.get_ohlcv(symbol=symbol, timeframe=timeframe, limit=limit)
                except Exception as exc:  # noqa: BLE001 — one venue, not the market
                    logger.debug(f"[oblivion-gate] venue candles failed for {symbol}: {exc}")
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[oblivion-gate] primary candles failed for {symbol}: {exc}")
    if not rows:
        try:
            from app.services import candles as candle_source

            rows = await candle_source.fetch(symbol, timeframe, limit)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[oblivion-gate] no candles for {symbol} {timeframe}: {exc}")
            rows = []
    return list(rows or [])


# ── Verdict ─────────────────────────────────────────────────────────────────


def verdict(res: Optional[Dict[str, Any]], price: Optional[float] = None,
            timeframe: Optional[str] = None, trend: Optional[int] = None) -> Dict[str, Any]:
    """What the engine says the room may do right now.

    Returns ``{tradeable, hard_block, action, kind, score, entry, stop_loss,
    tp1, tp2, tp3, signal_time, confirmed, news_blocked, reason}``.
    ``tradeable`` means a fresh, confirmed, still-valid signal exists;
    anything else is a hard block for new entries.
    """
    out: Dict[str, Any] = {
        "tradeable": False, "hard_block": True, "action": "wait", "kind": None,
        "score": None, "entry": None, "stop_loss": None, "tp1": None, "tp2": None,
        "tp3": None, "signal_time": None, "confirmed": False, "news_blocked": False,
        "reason": "",
    }
    if not isinstance(res, dict) or not res or res.get("error"):
        out["reason"] = "no OBLIVION read for this pair" + (f" ({res.get('error')})" if isinstance(res, dict) and res.get("error") else "")
        return out

    news = res.get("news") or {}
    out["news_blocked"] = bool(news.get("blocked"))
    sig = res.get("signal") or {}
    last = res.get("last_signal") or {}

    candidate: Optional[Dict[str, Any]] = None
    if sig.get("confirmed") and sig.get("action") in ("buy", "sell") and sig.get("entry") is not None:
        candidate = sig
    elif (
        last.get("action") in ("buy", "sell") and last.get("entry") is not None
        and last.get("bars_since") is not None and int(last["bars_since"]) <= FRESH_BARS
    ):
        candidate = last

    if candidate is None:
        pending = str(sig.get("action") or "wait")
        why = res.get("wait_reason")
        if pending in ("buy", "sell"):
            out["reason"] = f"OBLIVION {pending.upper()} is still forming — it must hold at the bar close"
        else:
            out["reason"] = "OBLIVION has no confirmed signal" + (f" — {why}" if why else "")
        if last.get("action") in ("buy", "sell") and last.get("bars_since") is not None:
            out["reason"] += f" (last {str(last['action']).upper()} was {last['bars_since']} bars ago)"
        return out

    side = str(candidate["action"])
    out.update(
        action=side, kind=candidate.get("kind"), score=candidate.get("score"),
        entry=_num(candidate.get("entry")), stop_loss=_num(candidate.get("stop_loss")),
        tp1=_num(candidate.get("tp1")), tp2=_num(candidate.get("tp2")), tp3=_num(candidate.get("tp3")),
        signal_time=candidate.get("time"), confirmed=True, atr=_num(candidate.get("atr")),
    )
    # The room's tuned profile enters on a pullback, with the targets measured
    # from that fill — the plan the backtest validated.
    from app.signals.oblivion_profiles import cfg_for, profile_for

    prof = profile_for(timeframe or "")
    if prof:
        from app.signals.oblivion_profiles import entry_plan

        cfg = cfg_for(timeframe or "")
        plan = entry_plan(candidate, prof["entry_pullback_atr"], (cfg.tp1_rr, cfg.tp2_rr, cfg.tp3_rr))
        if plan:
            out.update(
                limit=plan["limit"], entry_mode="limit", wait_bars=prof["entry_wait_bars"],
                tp1=plan["tp1"], tp2=plan["tp2"], tp3=plan["tp3"],
                tp1_close=prof["tp1_close"], tp2_close=prof["tp2_close"], timeframe=timeframe,
                trail_atr=prof.get("trail_atr"), trail_from=int(prof.get("trail_from") or 1),
                floor_at_tp1=bool(prof.get("floor_at_tp1")),
                hold_past_tp3=bool(prof.get("hold_past_tp3")),
            )
    label = f"OBLIVION {str(candidate.get('kind') or 'signal')} {side.upper()} (score {candidate.get('score')})"

    if out["news_blocked"]:
        out["reason"] = f"{label} — but the news window is active ({news.get('window')}), no new entries"
        return out

    from app.signals.oblivion_profiles import entry_filter_reason

    if (why := entry_filter_reason(timeframe or "", candidate, trend)) is not None:
        out["reason"] = f"{label} — filtered by the room's {timeframe} profile: {why}"
        return out

    outcome = (candidate.get("outcome") or {}).get("status")
    if outcome and outcome != "open":
        out["reason"] = f"{label} already resolved ({outcome}) — nothing left to enter"
        return out

    px = _num(price)
    if px:
        stop, tp1 = out["stop_loss"], out["tp1"]
        is_long = side == "buy"
        if stop is not None and ((is_long and px <= stop) or (not is_long and px >= stop)):
            out["reason"] = f"{label} — price {px} is already through its stop {stop}"
            return out
        if out.get("entry_mode") != "limit" and tp1 is not None and ((is_long and px >= tp1) or (not is_long and px <= tp1)):
            out["reason"] = f"{label} — price {px} already reached TP1 {tp1}; entering now is chasing"
            return out

    forming = str(sig.get("action") or "wait")
    if candidate is last and forming in ("buy", "sell") and forming != side and sig.get("new"):
        out["reason"] = f"{label} — but the forming bar is building an opposite {forming.upper()} setup"
        return out

    out["tradeable"] = True
    out["hard_block"] = False
    out["reason"] = (
        f"{label} confirmed — "
        + (f"limit {out['limit']} (pullback)" if out.get("entry_mode") == "limit" else f"entry {out['entry']}")
        + f", SL {out['stop_loss']}, TP1 {out['tp1']}, TP3 {out['tp3']}"
    )
    return out


def mandate(gate: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The plan the board is asked to confirm or veto, or None when blocked."""
    if not gate or not gate.get("tradeable"):
        return None
    return {k: gate.get(k) for k in (
        "action", "kind", "score", "entry", "limit", "entry_mode", "stop_loss", "tp1", "tp2", "tp3", "signal_time",
    )}


def abstained(decision: Optional[Dict[str, Any]]) -> bool:
    """A seat that produced no usable answer has not voted.

    A veto needs a named reason: a decision with no reasoning, or no confidence
    at all, is a failed model call, not a judgement against the engine.
    """
    if not decision:
        return True
    reasoning = decision.get("reasoning")
    if isinstance(reasoning, (list, dict)):
        reasoning = " ".join(str(v) for v in (reasoning.values() if isinstance(reasoning, dict) else reasoning))
    if not str(reasoning or "").strip():
        return True
    try:
        return float(decision.get("confidence") or 0) <= 0
    except (TypeError, ValueError):
        return True


def engine_confidence(gate: Dict[str, Any]) -> float:
    """OBLIVION's own conviction, on the 0-1 scale the execution gates use."""
    try:
        return round(min(0.95, max(0.0, float(gate.get("score") or 0) / 12.0)), 3)
    except (TypeError, ValueError):
        return 0.0


def enforce(action: str, gate: Optional[Dict[str, Any]]) -> Tuple[str, Optional[str], str]:
    """Apply the engine's mandate to the board's action.

    Returns ``(action, reason, origin)``. ``reason`` is None when the board's
    call stands. Agents may confirm the engine's side or veto it (hold); they
    may never trade without a signal, against it, or through the news window.
    """
    act = str(action or "hold").lower()
    if act not in ("buy", "sell"):
        return act, None, "board"
    if not gate or gate.get("hard_block") or not gate.get("tradeable"):
        return "hold", f"OBLIVION gate: {(gate or {}).get('reason') or 'no confirmed signal'}", "oblivion_gate"
    if act != gate.get("action"):
        return "hold", (
            f"OBLIVION mandate is {str(gate.get('action')).upper()} — the board's {act.upper()} "
            "cannot flip the engine's direction"
        ), "oblivion_override_blocked"
    return act, None, "board"


# ── Ledger ──────────────────────────────────────────────────────────────────


def _hash(parts: Sequence[Any]) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:20]


async def store_read(symbol: str, timeframe: str, source: str, res: Dict[str, Any],
                 compact: Dict[str, Any], gate: Dict[str, Any], price: Optional[float],
                 bar_time: Optional[int], session_id: Optional[str]) -> Optional[int]:
    from app.agents import brain_log
    from app.core.database import AsyncSessionLocal

    if brain_log.suppressed():
        return None
    try:
        from app.services.oblivion_settings import current_version

        version = current_version()
    except Exception:  # noqa: BLE001
        version = None

    sig = res.get("signal") or {}
    sc = res.get("score") or {}
    read_hash = _hash([symbol, timeframe, bar_time, gate.get("action"), gate.get("kind"),
                       gate.get("signal_time"), gate.get("tradeable"), sig.get("action"),
                       sc.get("buy"), sc.get("sell"), version])
    try:
        async with AsyncSessionLocal() as db:
            prev = (await db.execute(
                select(OblivionRead.read_hash)
                .where(OblivionRead.symbol == symbol, OblivionRead.timeframe == timeframe)
                .order_by(desc(OblivionRead.id)).limit(1)
            )).scalar_one_or_none()
            row = OblivionRead(
                symbol=symbol, timeframe=timeframe, source=source, analyzed_at=now_sast(),
                bar_time=bar_time, price=price,
                signal_action=gate.get("action") if gate.get("confirmed") else sig.get("action"),
                signal_kind=gate.get("kind") or sig.get("kind"),
                signal_new=bool(sig.get("new")), signal_confirmed=bool(gate.get("confirmed")),
                signal_time=gate.get("signal_time"),
                score_buy=_num(sc.get("buy", sc.get("bull"))), score_sell=_num(sc.get("sell", sc.get("bear"))),
                entry=gate.get("entry"), stop_loss=gate.get("stop_loss"),
                tp1=gate.get("tp1"), tp2=gate.get("tp2"), tp3=gate.get("tp3"),
                market_bias=str(res.get("bias_label") or res.get("market_bias") or ""),
                news_blocked=bool(gate.get("news_blocked")), tradeable=bool(gate.get("tradeable")),
                verdict_reason=str(gate.get("reason") or "")[:1000], settings_version=version,
                read_hash=read_hash, session_id=session_id,
                compact_json=json.dumps(compact, default=str)[:60000],
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            brain_log.log_oblivion_read(row, changed=(prev != read_hash))
            return row.id
    except Exception as exc:  # noqa: BLE001 — the read is still returned to the caller
        logger.warning(f"[oblivion-gate] could not store read for {symbol} {timeframe}: {exc}")
        return None


async def read_pair(
    symbol: str,
    timeframe: str,
    *,
    source: str,
    rows: Optional[Sequence[Sequence[Any]]] = None,
    session_id: Optional[str] = None,
    persist: bool = True,
) -> Dict[str, Any]:
    """Run OBLIVION on one pair and record that it did.

    Returns ``{symbol, timeframe, result, compact, verdict, price, read_id, bars}``.
    ``result`` carries ``error`` when there were not enough candles.
    """
    from app.signals.oblivion import analyze_oblivion, compact_for_prompt

    sym = normalise(symbol)
    tf = str(timeframe or "1h").lower()
    if rows is not None:
        data = list(rows)
    else:
        try:
            data = await asyncio.wait_for(fetch_rows(symbol, tf), timeout=FETCH_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning(f"[oblivion-gate] candle fetch for {sym} {tf} timed out after {FETCH_TIMEOUT_S}s")
            data = []
    out: Dict[str, Any] = {"symbol": sym, "timeframe": tf, "bars": len(data), "read_id": None,
                           "result": None, "compact": None, "price": None}
    if len(data) < 60:
        res = {"error": f"only {len(data)} bars — OBLIVION needs 60"}
        out.update(result=res, verdict=verdict(res))
        return out
    try:
        from app.signals.oblivion_profiles import cfg_for

        res = analyze_oblivion(data, tf, cfg_for(tf))
    except Exception as exc:  # noqa: BLE001
        res = {"error": f"engine failed: {exc}"}
    if res.get("error"):
        out.update(result=res, verdict=verdict(res))
        return out

    price = _num(data[-1][4])
    compact = compact_for_prompt(res)
    gate = verdict(compact, price, tf, await trend_sign(symbol, tf))
    bar_time = None
    try:
        raw = float(data[-1][0])
        bar_time = int(raw // 1000) if raw > 1e11 else int(raw)
    except (TypeError, ValueError, IndexError):
        pass
    out.update(result=res, compact=compact, verdict=gate, price=price)
    if persist:
        out["read_id"] = await store_read(sym, tf, source, res, compact, gate, price, bar_time, session_id)
    return out


async def trend_sign(symbol: str, timeframe: str) -> Optional[int]:
    """EMA 21 vs 50 on the profile's trend timeframe, last *closed* bar — the
    same rule the backtest's ``trend_tf`` filter applies. None when unused."""
    from app.signals.oblivion_profiles import profile_for

    prof = profile_for(timeframe)
    ttf = (prof or {}).get("trend_tf")
    if not ttf:
        return None
    try:
        rows = await asyncio.wait_for(fetch_rows(symbol, ttf, 160), timeout=FETCH_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        return None
    closes = [float(r[4]) for r in rows[:-1]]
    if len(closes) < 60:
        return None

    def ema(vals, n):
        k, out = 2 / (n + 1), None
        for v in vals:
            out = v if out is None else v * k + out * (1 - k)
        return out

    fast, slow = ema(closes, 21), ema(closes, 50)
    return 1 if fast > slow else -1 if fast < slow else 0


async def latest_read(symbol: str, timeframe: str, max_age_s: float, db: Any = None) -> Optional[OblivionRead]:
    """The freshest stored read of this pair on this timeframe, or None."""
    sym = normalise(symbol)
    tf = str(timeframe or "1h").lower()
    since = now_sast() - timedelta(seconds=max_age_s)
    stmt = (
        select(OblivionRead)
        .where(OblivionRead.symbol == sym, OblivionRead.timeframe == tf, OblivionRead.analyzed_at >= since)
        .order_by(desc(OblivionRead.id)).limit(1)
    )
    if db is not None:
        return (await db.execute(stmt)).scalar_one_or_none()
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as s:
        return (await s.execute(stmt)).scalar_one_or_none()
