"""OBLIVION signals on every timeframe, confirmed by the Kronos forecast.

For each timeframe the engine (``app.signals.oblivion``) is run on the same
feed the Kronos page draws (``forecast_service._fetch_ohlcv``), then fused
with that timeframe's cached forecast: Oblivion leads — its BUY / SELL and
Entry / SL / TP1-3 are the plan — and Kronos confirms (confidence lift when
aligned, veto when it opposes decisively). The news window vetoes everything.

The consensus is a timeframe-weighted vote of the non-vetoed calls.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

from loguru import logger

from plugins.KronosForecastPlugin.backend.schemas import (
    OblivionConsensus, OblivionMultiTfResponse, OblivionTfSignal,
)
from plugins.KronosForecastPlugin.backend.services import forecast_service

#: Every timeframe the page offers, lowest first. Weights favour the higher
#: ones: a daily call outranks a one-minute one in the consensus.
ALL_TFS: List[str] = ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"]
TF_WEIGHT: Dict[str, float] = {"1m": 1, "5m": 2, "15m": 3, "30m": 4, "1h": 5, "4h": 6, "1d": 7, "1w": 8}
#: Bars fetched per timeframe — enough for EMA200 + swing history.
LOOKBACK = 300


async def _one_tf(exchange: str, symbol: str, tf: str, with_kronos: bool) -> OblivionTfSignal:
    from app.signals.oblivion import analyze_oblivion

    try:
        rows = await forecast_service._fetch_ohlcv(exchange, symbol, tf, LOOKBACK)
    except Exception as exc:  # noqa: BLE001
        return OblivionTfSignal(timeframe=tf, error=f"candles: {exc}")
    if not rows or len(rows) < 60:
        return OblivionTfSignal(timeframe=tf, error=f"only {len(rows or [])} candles")

    res = analyze_oblivion(rows, tf)
    if res.get("error"):
        return OblivionTfSignal(timeframe=tf, error=str(res["error"]))

    sig = res.get("signal") or {}
    sc = res.get("score") or {}
    news = res.get("news") or {}
    action = str(sig.get("action") or "wait")
    if action not in ("buy", "sell"):
        action = "wait"

    kronos_sig = None
    if with_kronos:
        try:
            fc = await forecast_service.run_forecast_cached(exchange, symbol, tf)
            kronos_sig = fc.signal
        except Exception as exc:  # noqa: BLE001 — the engine's call stands alone
            logger.debug(f"[Kronos/Oblivion] forecast skipped for {symbol} {tf}: {exc}")

    conf, aligned, vetoed = forecast_service.fuse_oblivion_with_kronos(sig, sc, kronos_sig)
    if action != "wait" and bool(news.get("blocked")):
        vetoed = "news"

    entry, sl, tp1 = sig.get("entry"), sig.get("stop_loss"), sig.get("tp1")
    rr = None
    if entry is not None and sl is not None and tp1 is not None and abs(entry - sl) > 0:
        rr = round(abs(tp1 - entry) / abs(entry - sl), 2)

    return OblivionTfSignal(
        timeframe=tf,
        action=action,
        confirmed=bool(sig.get("confirmed")),
        new=bool(sig.get("new")),
        score=int(sig.get("score") or 0),
        required=int(sc.get("required") or 6),
        bull=int(sc.get("buy", sc.get("bull")) or 0),
        bear=int(sc.get("sell", sc.get("bear")) or 0),
        market_bias=int(res.get("market_bias") or 0),
        bias_label=str(res.get("bias_label") or "NEUTRAL"),
        entry=entry, stop_loss=sl, tp1=tp1, tp2=sig.get("tp2"), tp3=sig.get("tp3"),
        risk_reward=rr,
        confidence=conf,
        kronos_direction=getattr(kronos_sig, "direction", None),
        kronos_pct_change=getattr(kronos_sig, "pct_change", None),
        kronos_confidence=getattr(kronos_sig, "confidence", None),
        kronos_aligned=aligned,
        vetoed_by=vetoed,
        news_blocked=bool(news.get("blocked")),
        session=str((res.get("session") or {}).get("name") or "OFF"),
        structure=str((res.get("structure") or {}).get("now") or "WAIT"),
        last_signal=res.get("last_signal"),
        summary=str(res.get("summary") or ""),
    )


def consensus_of(per_tf: List[OblivionTfSignal]) -> OblivionConsensus:
    """Timeframe-weighted vote over non-vetoed BUY / SELL calls."""
    total = 0.0
    signed = 0.0
    buys: List[str] = []
    sells: List[str] = []
    vetoed: List[str] = []
    for t in per_tf:
        if t.error:
            continue
        w = TF_WEIGHT.get(t.timeframe, 1.0)
        total += w
        if t.vetoed_by:
            vetoed.append(t.timeframe)
            continue
        if t.action == "buy":
            signed += w * max(0.25, t.confidence)
            buys.append(t.timeframe)
        elif t.action == "sell":
            signed -= w * max(0.25, t.confidence)
            sells.append(t.timeframe)
    score = round(signed / total, 3) if total > 0 else 0.0
    if score >= 0.15 and len(buys) > len(sells):
        action, agreeing, opposing = "buy", buys, sells
    elif score <= -0.15 and len(sells) > len(buys):
        action, agreeing, opposing = "sell", sells, buys
    else:
        action, agreeing, opposing = "wait", [], buys + sells
    if action == "wait":
        note = "No timeframe consensus — the engine is waiting on most timeframes." if not (buys or sells) else \
            f"Mixed: BUY on {', '.join(buys) or '—'} vs SELL on {', '.join(sells) or '—'}."
    else:
        note = f"{action.upper()} on {', '.join(agreeing)}" + (f"; against {', '.join(opposing)}" if opposing else "") + \
            (f"; vetoed on {', '.join(vetoed)}" if vetoed else "") + "."
    return OblivionConsensus(action=action, score=score, agreeing_tfs=agreeing, opposing_tfs=opposing,
                             vetoed_tfs=vetoed, note=note)


async def oblivion_multi_tf(
    exchange: str,
    symbol: str,
    timeframes: Optional[List[str]] = None,
    with_kronos: bool = True,
) -> OblivionMultiTfResponse:
    tfs = [t for t in (timeframes or ALL_TFS) if t in forecast_service._TF_SECONDS] or ALL_TFS
    results = await asyncio.gather(
        *(_one_tf(exchange, symbol, tf, with_kronos) for tf in tfs), return_exceptions=True,
    )
    per_tf: List[OblivionTfSignal] = []
    for tf, r in zip(tfs, results):
        if isinstance(r, Exception):
            per_tf.append(OblivionTfSignal(timeframe=tf, error=str(r)))
        else:
            per_tf.append(r)
    news_blocked = any(t.news_blocked for t in per_tf if not t.error)
    note = None
    if news_blocked:
        note = "OBLIVION news window is active — no entries are executed inside it."
    return OblivionMultiTfResponse(
        exchange=exchange, symbol=symbol, generated_at=int(time.time()), with_kronos=with_kronos,
        consensus=consensus_of(per_tf), per_tf=per_tf, news_blocked=news_blocked, note=note,
    )


# ── Standalone OBLIVION sniper signals (market + limit, crypto + MT5) ───────

def _oblivion_fmt(v: float) -> str:
    if v >= 1000:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:.4f}".rstrip("0").rstrip(".")
    return f"{v:.6g}"


def _oblivion_limit_entry(obl: dict, symbol: str) -> float | None:
    """Better-price limit entry inside the nearest OB/FVG or via ATR offset.

    For BUY: nearest bullish OB/FVG below the market entry, else entry - 0.35*ATR.
    For SELL: nearest bearish OB/FVG above, else entry + 0.35*ATR.
    Returns None when oblivion has no signal.
    """
    sig = obl.get("signal") or {}
    action = str(sig.get("action") or "wait")
    entry = sig.get("entry")
    if action not in ("buy", "sell") or entry is None:
        return None
    try:
        entry = float(entry)
    except (TypeError, ValueError):
        return None
    atr = obl.get("atr")
    try:
        atr = float(atr) if atr is not None else 0.0
    except (TypeError, ValueError):
        atr = 0.0

    # Search nearest zone
    try:
        boxes = []
        for b in (obl.get("fvg") or {}).get("boxes") or []:
            boxes.append(b)
        for b in (obl.get("order_blocks") or {}).get("boxes") or []:
            boxes.append(b)
        # Filter by kind matching side
        want = "bull" if action == "buy" else "bear"
        cands = [b for b in boxes if str(b.get("kind") or "").startswith(want)]
        # Most recent first (boxes already in engine order; but pick nearest price)
        best = None
        best_dist = None
        for b in cands:
            try:
                top = float(b["top"]); bot = float(b["bottom"])
                mid = (top + bot) / 2.0
                # For buy, zone must be below entry (discount) and above SL
                sl = float(sig.get("stop_loss") or entry)
                if action == "buy":
                    if mid < entry and mid > sl:
                        dist = entry - mid
                        if best_dist is None or dist < best_dist:
                            best_dist = dist; best = mid
                else:
                    if mid > entry and mid < sl:
                        dist = mid - entry
                        if best_dist is None or dist < best_dist:
                            best_dist = dist; best = mid
            except (TypeError, ValueError, KeyError):
                continue
        if best is not None:
            return round(float(best), 8)
    except Exception:
        pass

    # Fallback ATR offset
    if atr and atr > 0:
        offset = atr * 0.35
        if action == "buy":
            return round(entry - offset, 8)
        else:
            return round(entry + offset, 8)
    return round(entry, 8)


def build_oblivion_standalone_signals(
    obl: dict,
    symbol: str,
    max_leverage: Optional[int] = None,
) -> List[Any]:
    """Build market + limit SniperSignals from a standalone OBLIVION result.

    Independent of Kronos — uses the engine's own Entry/SL/TP1-3, ATR and
    zones to produce 1-click market and limit entries that work on both
    crypto (Bitget) and MT5 (FX/metals) via the existing order routing.
    """
    from plugins.KronosForecastPlugin.backend.schemas import SniperSignal

    if not obl or obl.get("error"):
        return []
    sig = obl.get("signal") or {}
    action = str(sig.get("action") or "wait")
    if action not in ("buy", "sell"):
        return []
    if sig.get("entry") is None or sig.get("stop_loss") is None or sig.get("tp1") is None:
        return []
    # News filter is hard — no entries inside the window
    if bool((obl.get("news") or {}).get("blocked")):
        return []

    try:
        entry_m = float(sig["entry"])
        sl = float(sig["stop_loss"])
        tp1 = float(sig["tp1"])
        tp2 = float(sig.get("tp2") or tp1)
        tp3 = sig.get("tp3")
        risk_m = abs(entry_m - sl)
        if risk_m <= 0:
            return []
        # Direction check sanity
        if action == "buy" and not (tp1 > entry_m and sl < entry_m):
            return []
        if action == "sell" and not (tp1 < entry_m and sl > entry_m):
            return []
    except (TypeError, ValueError):
        return []

    sc = obl.get("score") or {}
    req = int(sc.get("required") or 6)
    pts = int(sig.get("score") or (sc.get("buy", sc.get("bull", 0)) if action == "buy" else sc.get("sell", sc.get("bear", 0))))
    # Confidence like fuse: pts/12, but ensure at least 0.45 for actionable signals
    base_conf = max(0.0, min(1.0, pts / 12.0))
    # Lift if structure is BOS/CHOCH etc? Keep simple
    conf = round(base_conf, 3)
    # Leather: if below required, still allow but mark lower confidence (already pts/12)

    # Leverage: exchange max when known, else confidence-scaled
    if max_leverage and max_leverage > 0:
        lev = int(max_leverage)
    else:
        lev = 10 if conf >= 0.7 else (5 if conf >= 0.5 else 3)

    side = "long" if action == "buy" else "short"

    # Market entry (immediate)
    rr_m = round(abs(tp1 - entry_m) / risk_m, 2) if risk_m else 0.0
    reasons_m = [
        f"OBLIVION v5 {str(sig.get('kind') or 'signal').upper()} {action.upper()} on {obl.get('timeframe', '1h')}: buy {sc.get('buy', sc.get('bull',0))}/sell {sc.get('sell', sc.get('bear',0))} (scalp ≥{sc.get('scalp_min', req)}, sniper ≥{sc.get('sniper_min', 9)}), bias {obl.get('bias_label','NEUTRAL')}, {(obl.get('structure') or {}).get('now','WAIT')}, HTF {(obl.get('htf') or {}).get('now','N/A')}, session {(obl.get('session') or {}).get('name','OFF')}.",
        f"Market entry { _oblivion_fmt(entry_m)}  SL {_oblivion_fmt(sl)} ({risk_m:.6g} risk, {100*risk_m/entry_m:.2f}%)  TP1 {_oblivion_fmt(tp1)} (1R)  TP2 {_oblivion_fmt(tp2)} (2R)" + (f"  TP3 {_oblivion_fmt(float(tp3))} (3R)" if tp3 else ""),
        f"R:R {rr_m}  Score {pts}/{req}  Conf {conf:.0%}" + ("  (confirmed bar)" if sig.get("confirmed") else "  (forming — place as limit or wait for close)"),
    ]
    if obl.get("summary"):
        reasons_m.append(str(obl["summary"]))

    signals: List[Any] = []
    signals.append(SniperSignal(
        id=f"{symbol}-{side}-oblivion-market", side=side, order_kind="market",
        label=f"Oblivion {action.upper()} Market", entry=round(entry_m,8), stop_loss=round(sl,8),
        take_profit_1=round(tp1,8), take_profit_2=round(tp2,8),
        risk_reward=rr_m, confidence=conf, leverage=lev, reasons=reasons_m,
    ))

    # Limit entry (better price inside OB/FVG or ATR offset)
    entry_l = _oblivion_limit_entry(obl, symbol)
    if entry_l is not None and entry_l != entry_m:
        # Recompute risk/reward for limit
        risk_l = abs(entry_l - sl)
        if risk_l > 0 and ( (action == "buy" and tp1 > entry_l) or (action == "sell" and tp1 < entry_l) ):
            rr_l = round(abs(tp1 - entry_l) / risk_l, 2)
            reasons_l = [
                f"OBLIVION v5 {action.upper()} LIMIT on {obl.get('timeframe','1h')}: buy {sc.get('buy', sc.get('bull',0))}/sell {sc.get('sell', sc.get('bear',0))} (scalp ≥{sc.get('scalp_min', req)}), bias {obl.get('bias_label','NEUTRAL')}.",
                f"Limit { _oblivion_fmt(entry_l)}  SL {_oblivion_fmt(sl)}  TP1 {_oblivion_fmt(tp1)}  TP2 {_oblivion_fmt(tp2)}" + (f"  TP3 {_oblivion_fmt(float(tp3))}" if tp3 else ""),
                f"R:R {rr_l} (better than market {rr_m})  Conf {conf:.0%}  — waits for pullback into OB/FVG/discount.",
            ]
            if obl.get("summary"):
                reasons_l.append(str(obl["summary"]))
            signals.append(SniperSignal(
                id=f"{symbol}-{side}-oblivion-limit", side=side, order_kind="limit",
                label=f"Oblivion {action.upper()} Limit", entry=round(entry_l,8), stop_loss=round(sl,8),
                take_profit_1=round(tp1,8), take_profit_2=round(tp2,8),
                risk_reward=rr_l, confidence=conf, leverage=lev, reasons=reasons_l,
            ))

    return signals


async def generate_oblivion_sniper_signals(
    exchange: str,
    symbol: str,
    timeframe: str = "1h",
    with_kronos_fusion: bool = True,
) -> Any:
    """Standalone OBLIVION sniper: no Kronos dependency, works for every pair.

    Fetches candles via the universal resolver, runs the engine, optionally
    fuses Kronos confidence (when available) to lift/trim confidence, then
    returns market+limit SniperSignals with 1-click order support for both
    crypto (Bitget) and MT5 (FX/metals/indices) via the existing routing.
    """
    from app.services import candles as candle_source
    from app.signals.oblivion import analyze_oblivion
    from plugins.KronosForecastPlugin.backend.schemas import SniperSignalsResponse

    # Fetch via universal resolver so every asset class works (Yahoo+MT5+crypto)
    candles = await candle_source.fetch(symbol, timeframe, limit=300)
    if not candles or len(candles) < 60:
        return SniperSignalsResponse(
            exchange=exchange, symbol=symbol, timeframe=timeframe, engine="oblivion",
            anchor_price=0.0, direction="flat", pct_change=0.0, confidence=0.0,
            signals=[], decision="NO_TRADE", rationale=[], note=f"Only {len(candles or [])} candles — need 60 for OBLIVION.",
        )

    obl = analyze_oblivion(candles, timeframe)
    if obl.get("error"):
        return SniperSignalsResponse(
            exchange=exchange, symbol=symbol, timeframe=timeframe, engine="oblivion",
            anchor_price=float(candles[-1][4]), direction="flat", pct_change=0.0, confidence=0.0,
            signals=[], decision="NO_TRADE", rationale=[], note=str(obl.get("error")),
        )

    # News window blocks
    if bool((obl.get("news") or {}).get("blocked")):
        return SniperSignalsResponse(
            exchange=exchange, symbol=symbol, timeframe=timeframe, engine="oblivion",
            anchor_price=float(obl.get("last_price") or candles[-1][4]), direction="flat", pct_change=0.0, confidence=0.0,
            signals=[], decision="NO_TRADE", rationale=[], note=f"NEWS WINDOW — oblivion blocked ({obl.get('news',{}).get('window')} {obl.get('news',{}).get('tz')})",
        )

    sig = obl.get("signal") or {}
    action = str(sig.get("action") or "wait")
    if action not in ("buy","sell"):
        return SniperSignalsResponse(
            exchange=exchange, symbol=symbol, timeframe=timeframe, engine="oblivion",
            anchor_price=float(obl.get("last_price") or candles[-1][4]), direction="flat", pct_change=0.0, confidence=float((obl.get("score") or {}).get("bull",0))/12,
            signals=[], decision="NO_TRADE", rationale=[str(obl.get("summary") or "OBLIVION WAIT — no qualifying score")], note="OBLIVION WAIT — score below required.",
        )

    # Optional Kronos fusion for confidence lift (does not veto standalone — just adjusts)
    conf_extra = None
    if with_kronos_fusion:
        try:
            fc = await forecast_service.run_forecast_cached(exchange, symbol, timeframe)
            from plugins.KronosForecastPlugin.backend.services.forecast_service import fuse_oblivion_with_kronos
            sc = obl.get("score") or {}
            conf, aligned, vetoed = fuse_oblivion_with_kronos(sig, sc, fc.signal)
            if vetoed == "kronos":
                # Strong opposing Kronos with high confidence — surface note but still offer signals (user chooses)
                pass
            # Patch confidence into obl copy for builder? Builder recomputes from pts/12, so we override after
            conf_extra = (conf, aligned, vetoed, fc.signal)
        except Exception:
            conf_extra = None

    # Build signals
    try:
        max_lev = await forecast_service._max_leverage(exchange, symbol)
    except Exception:
        max_lev = None

    signals = build_oblivion_standalone_signals(obl, symbol, max_lev)

    # If fusion gave higher confidence, patch
    if conf_extra is not None and signals:
        fused_conf, aligned, vetoed, ks = conf_extra
        for s in signals:
            s.confidence = fused_conf
            # Annotate fusion in reasons
            if ks is not None and hasattr(ks, 'direction'):
                tag = "aligned" if aligned else "opposed" if aligned is False else "n/a"
                s.reasons.append(f"Kronos {ks.direction.upper()} {ks.pct_change:+.2f}% ({int(ks.confidence*100)}%) {tag} — oblivion confidence {'lifted' if aligned else 'trimmed' if aligned is False else 'unchanged'}.")

    direction = "up" if action == "buy" else "down"
    anchor = float(obl.get("last_price") or candles[-1][4] or 0)
    pct = 0.0
    # Estimate pct from entry vs anchor? For oblivion, entry is at close, so pct ~0; we can compute risk-based
    # Use 1R as proxy: (tp1 - entry)/entry *100
    try:
        e = float(sig.get("entry") or anchor); t1 = float(sig.get("tp1") or anchor)
        pct = (t1 - e)/e*100 if e else 0.0
        if action == "sell":
            pct = -abs(pct)
        else:
            pct = abs(pct)
    except Exception:
        pct = 0.0

    conf_val = float(signals[0].confidence) if signals else (float(pts)/12 if (pts:= (obl.get("score") or {}).get("bull" if action=="buy" else "bear",0)) else 0.0)
    return SniperSignalsResponse(
        exchange=exchange, symbol=symbol, timeframe=timeframe, engine="oblivion",
        anchor_price=round(anchor,8), direction=direction, pct_change=round(pct,2), confidence=round(conf_val,3),
        signals=signals, decision="OK" if signals else "NO_TRADE",
        rationale=[str(obl.get("summary") or "")], note=None if signals else "OBLIVION WAIT — no signal at required score.",
    )
