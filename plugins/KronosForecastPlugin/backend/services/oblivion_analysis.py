"""OBLIVION deep analysis — LLM-powered like Kronos but for the engine's own read.

Uses the same AI gateway as Kronos (ai_router.db_chat / deep_reasoning chain)
to produce a 500-word trader-ready breakdown covering the 0-12 score,
structure, FVG/OB, liquidity, fib/premium, session, volume, news and the
Entry/SL/TP1-3 plan. Falls back to a deterministic summary when the LLM is
unavailable so Telegram always has something to show.

All imports are guarded — the plugin stays removable.
"""
from __future__ import annotations

import os
import time
from typing import Optional, Tuple

from loguru import logger

from plugins.KronosForecastPlugin.backend.schemas import MarketCapInfo, PositionInfo, JarvisAnalysisResponse, ForecastSignal, VolumeContext
from plugins.KronosForecastPlugin.backend.config import KRONOS_PAPER_KNOWLEDGE

_OBLIVION_KNOWLEDGE = (
    "You are analyzing OBLIVION MASTER v5 — a Pine v6 strategy port that runs two "
    "scorers at once over the same candles. Each side (buy / sell) collects 0-12 "
    "points from: trend (EMA 9/21/50 stack, +2), major trend (EMA 200, +1), VWAP "
    "(+1), micro break of structure (+2), major break (+1), CHOCH (+2), liquidity "
    "sweep (+2), displacement (+2), rejection wick (+1), volume (high ×1.15 and very "
    "high ×1.75, +1 each), volume expansion (+1), a fresh FVG (+1) and a retest of an "
    "unfilled FVG (+1), price inside an order block (+1), the dealing-range zone "
    "(discount for buys / premium for sells, +1), the fib 0.382-0.618 zone (+1), the "
    "OTE 0.607-0.786 band (+1), HTF agreement (+1), a real candle range (+1) and an "
    "active session (+1). A SCALP fires at score >= scalp_min (6) beating the other "
    "side. A SNIPER needs score >= sniper_min (9) AND every hard requirement: HTF "
    "trend, a recent liquidity sweep, the premium/discount zone, and the EMA 200 "
    "major trend — score.setups.*_missing names whichever is absent. trade_mode is "
    "SCALPING, SNIPER or HYBRID (sniper preferred, scalp otherwise). Entry = the "
    "close; SL = the structural swing +/- an ATR buffer with risk clamped to the "
    "min/max ATR band; TP1/TP2/TP3 = 0.5R / 1R / 1.5R. Gates that hold a setup back: "
    "confirmed-bar only, a cooldown, a duplicate still open, session, news window, "
    "and 'no room to TP3' before the range extreme — signal.gate names the one in "
    "force. outcomes is the engine's own scorecard over this window (TP1/TP2/TP3 "
    "hits, stop-outs, win rate). Never treat WAIT as bearish — read signal.gate and "
    "score.setups to say what is actually missing."
)

_SYSTEM_PROMPT = (
    "You are JARVIS, the OBLIVION analyst. You are given a full OBLIVION MASTER v5 "
    "engine read plus live market context. "
    + _OBLIVION_KNOWLEDGE
    + " Kronos context may be fused when available — note when an agreeing Kronos "
    "forecast lifts confidence or a decisively opposing one (≥0.45) would veto. "
    "Write a thorough, well-structured analysis — minimum 500 words — that a trader "
    "can act on immediately. Always cover ALL sections in full; never truncate: "
    "(1) SIGNAL & SCORE — explain buy vs sell points, the scalp and sniper minimums, which "
    "components fired and why; "
    "(2) TREND & STRUCTURE — EMA 21/50/200 read, swing HH/HL/LH/LL, BOS/CHOCH and "
    "market bias; "
    "(3) ZONES & LIQUIDITY — FVG/OB boxes, premium/discount vs equilibrium, "
    "0.382/0.5/0.618 fib zone, and sweep of 10-bar high/low; "
    "(4) SESSION & VOLUME & NEWS — session name, volume level/ratio, and whether "
    "the news window is blocking; "
    "(5) PLAN — entry, SL (structural vs ATR), TP1-3 at 1R/2R/3R and risk in %; "
    "(6) CONFLUENCE & INVALIDATION — what confirms the move and what level would "
    "invalidate it. "
    "If an OPEN POSITION is provided, add a final 'Position:' section with "
    "hold/reduce/close guidance using the entry/SL/TP and PnL. "
    "Target 500-620 words, clear prose, never fabricate numbers, finish every sentence."
)


def _fmt_usd(v: Optional[float]) -> str:
    if v is None:
        return "n/a"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "n/a"
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return f"${v / div:.2f}{unit}"
    return f"${v:,.2f}"


async def _market_context(symbol: str) -> Optional[MarketCapInfo]:
    try:
        from app.services import market_data, pair_catalog
    except Exception:
        return None
    # Market-cap context only means something for a crypto asset: for gold,
    # FX and indices the catalogue would match some token that shares a name
    # ("Gold", "US30") and quote its market cap under the wrong instrument.
    try:
        if market_data.is_universal_symbol(symbol):
            return None
    except Exception:  # noqa: BLE001
        pass
    try:
        snap = await pair_catalog.get_market_snapshot(symbol)
    except Exception as exc:
        logger.debug(f"[Oblivion] market snapshot failed for {symbol}: {exc}")
        return None
    if not snap:
        return None
    return MarketCapInfo(
        symbol=symbol, name=snap.get("name"), market_cap=snap.get("market_cap"),
        market_cap_rank=snap.get("market_cap_rank"), volume_24h=snap.get("volume_24h"),
        price=snap.get("price"), price_change_24h=snap.get("price_change_24h"), is_crypto=True,
    )


async def _open_position(exchange: str, symbol: str) -> Optional[PositionInfo]:
    try:
        from app.api.jarvis import get_all_positions
    except Exception:
        return None
    try:
        positions = await get_all_positions(exchange=exchange)
    except Exception as exc:
        logger.debug(f"[Oblivion] position fetch failed for {exchange}: {exc}")
        return None
    want = symbol.replace("/", "").upper()
    for p in positions or []:
        psym = str(getattr(p, "symbol", "") or "").replace("/", "").upper()
        if psym != want:
            continue
        try:
            return PositionInfo(
                exchange=str(getattr(p, "exchange", exchange)), symbol=str(getattr(p, "symbol", symbol)),
                side=str(getattr(p, "side", "long")).lower(), size=float(getattr(p, "size", 0.0) or 0.0),
                entry_price=float(getattr(p, "entry_price", 0.0) or 0.0), mark_price=float(getattr(p, "mark_price", 0.0) or 0.0),
                pnl=float(getattr(p, "pnl", 0.0) or 0.0), pnl_pct=float(getattr(p, "pnl_pct", 0.0) or 0.0),
                leverage=(float(getattr(p, "leverage")) if getattr(p, "leverage", None) not in (None, "") else None),
                liquidation_price=(float(getattr(p, "liquidation_price")) if getattr(p, "liquidation_price", None) not in (None, "") else None),
            )
        except Exception as exc:
            logger.debug(f"[Oblivion] position parse failed: {exc}")
            return None
    return None


def wait_reason(obl: dict) -> Optional[str]:
    """Why the engine is not firing — the engine's own rules, not a guess."""
    try:
        from app.signals.oblivion import wait_reason as _engine_wait_reason

        return _engine_wait_reason(obl)
    except Exception:  # noqa: BLE001
        sc = obl.get("score") or {}
        buy = int(sc.get("buy", sc.get("bull")) or 0)
        sell = int(sc.get("sell", sc.get("bear")) or 0)
        need = int(sc.get("scalp_min") or sc.get("required") or 6)
        return f"neither side has reached the minimum ({buy} / {sell} vs {need})"

def _build_user_prompt(obl: dict, symbol: str, timeframe: str, market: Optional[MarketCapInfo], position: Optional[PositionInfo], kronos_sig: Optional[ForecastSignal] = None) -> str:
    sig = obl.get("signal") or {}
    sc = obl.get("score") or {}
    trend = obl.get("trend") or {}
    struct = obl.get("structure") or {}
    fvg = obl.get("fvg") or {}
    ob = obl.get("order_blocks") or {}
    liq = obl.get("liquidity") or {}
    fib = obl.get("fib") or {}
    sess = obl.get("session") or {}
    vol = obl.get("volume") or {}
    news = obl.get("news") or {}
    # Core lines
    lines = [
        f"Symbol: {symbol}  ({timeframe})  Engine: OBLIVION MASTER v5  Mode: {obl.get('mode', 'HYBRID')}",
        f"Last price: {obl.get('last_price', 'n/a')}  ATR: {obl.get('atr', 'n/a')}  Equilibrium: {obl.get('equilibrium', 'n/a')}",
        f"Score: buy {sc.get('buy', sc.get('bull',0))}/sell {sc.get('sell', sc.get('bear',0))} "
        f"(scalp >={sc.get('scalp_min', 6)}, sniper >={sc.get('sniper_min', 9)})  Reasons: {', '.join(sc.get('reasons',[]) or [])}",
        f"Setups: {sc.get('setups', {})}",
        f"HTF: {obl.get('htf', {})}   VWAP: {obl.get('vwap', {})}   Dealing range: {obl.get('pd', {})}   OTE: {obl.get('ote', {})}",
        f"Outcomes on this window: {obl.get('outcomes', {})}",
        f"Trend: EMA21 {trend.get('ema_fast')} EMA50 {trend.get('ema_slow')} EMA200 {trend.get('ema_major')}  bull={trend.get('bull')} bear={trend.get('bear')}",
        f"Bias: {obl.get('bias_label')} ({obl.get('market_bias',0)})  Structure now: {struct.get('now')}  last swing high {struct.get('last_swing_high')} ({struct.get('last_high_type')})  low {struct.get('last_swing_low')} ({struct.get('last_low_type')})",
        f"FVG: {fvg.get('now')}  bull {fvg.get('bull')} bear {fvg.get('bear')}  inside_bull {fvg.get('inside_bull')} inside_bear {fvg.get('inside_bear')}  boxes {len(fvg.get('boxes',[]) or [])}",
        f"OB: bull {ob.get('bull')} bear {ob.get('bear')}  inside_bull {ob.get('inside_bull')} inside_bear {ob.get('inside_bear')}  boxes {len(ob.get('boxes',[]) or [])}",
        f"Liquidity: {liq.get('now')}  sweeps {len(liq.get('sweeps',[]) or [])}  prev_high {liq.get('prev_high')} prev_low {liq.get('prev_low')}",
        f"Fib: 0.382 {fib.get('f382')} 0.5 {fib.get('f500')} 0.618 {fib.get('f618')}  upper {fib.get('upper')} lower {fib.get('lower')} in_zone {fib.get('in_zone')} swing {fib.get('swing_high')}-{fib.get('swing_low')}",
        f"Premium {obl.get('premium')} Discount {obl.get('discount')}  Session {sess.get('name')} active {sess.get('active')} overlap {sess.get('overlap')}  Volume {vol.get('level')} ratio {vol.get('ratio')}  News blocked {news.get('blocked')} window {news.get('window')} {news.get('tz')}",
        f"Candle: strong_bull {obl.get('candle',{}).get('strong_bull')} strong_bear {obl.get('candle',{}).get('strong_bear')} disp {obl.get('candle',{}).get('bull_displacement')}/{obl.get('candle',{}).get('bear_displacement')} rej {obl.get('candle',{}).get('bull_rejection')}/{obl.get('candle',{}).get('bear_rejection')}",
        f"Signal: action {sig.get('action')} new {sig.get('new')} confirmed {sig.get('confirmed')} score {sig.get('score')}  Entry {sig.get('entry')} SL {sig.get('stop_loss')} TP1 {sig.get('tp1')} TP2 {sig.get('tp2')} TP3 {sig.get('tp3')} risk {sig.get('risk')} blocked_by_news {sig.get('blocked_by_news')}",
        f"Summary: {obl.get('summary','')}",
    ]
    if (why := wait_reason(obl)):
        lines.append(f"Why WAIT (engine rule, quote this — do not invent another reason): {why}")
    last = obl.get("last_signal") or {}
    if last.get("entry") is not None:
        lines.append(
            f"Last committed signal: {str(last.get('action','')).upper()} {last.get('bars_since','?')} bars ago — "
            f"Entry {last.get('entry')} SL {last.get('stop_loss')} TP1 {last.get('tp1')} TP2 {last.get('tp2')} TP3 {last.get('tp3')}"
        )
    if kronos_sig is not None:
        lines += ["", f"Kronos fusion: direction {kronos_sig.direction} {kronos_sig.pct_change:+.2f}% conf {kronos_sig.confidence:.2f} target {kronos_sig.target_price:.6g} decision {kronos_sig.decision}"]
        if kronos_sig.rationale:
            lines += ["  " + r for r in kronos_sig.rationale[:4]]
    if market:
        lines += ["", f"Market cap: {_fmt_usd(market.market_cap)} rank {market.market_cap_rank} 24h vol {_fmt_usd(market.volume_24h)} price {market.price}"]
    if position:
        lines += ["", f"OPEN POSITION: {position.side.upper()} {position.size} {position.symbol} entry {position.entry_price:.6g} mark {position.mark_price:.6g} PnL {position.pnl:+.2f} ({position.pnl_pct:+.2f}%) lev {position.leverage} liq {position.liquidation_price}"]
    lines.append("\nWrite the deep analysis now.")
    return "\n".join(lines)


async def _run_llm(user_prompt: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Ask the desk's deep-reasoning chain for the narrative.

    Same route the trading room uses (``chat_for_task`` → task profile or the
    default chain, local Ollama included), so whatever answers the room can
    answer here. Returns ``(text, provider, error)``; quality is judged by
    :func:`narrative_is_faithful`, not by length.
    """
    try:
        from app.core.database import AsyncSessionLocal
        from plugins.AiMarketAnalyst.backend.services.ai_router import chat_for_task, db_chat
    except Exception as exc:  # noqa: BLE001
        return None, None, f"AI gateway unavailable: {exc}"
    msgs = [{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": user_prompt}]
    timeout = float(os.getenv("KRONOS_JARVIS_TIMEOUT_S", "105"))
    common = dict(temperature=0.38, agent_name="oblivion-jarvis", agent_role="forecaster", source="forecaster")

    def _extract(result: dict):
        if not result or not result.get("ok"):
            return None, (result or {}).get("provider"), (result or {}).get("error") or "AI call failed"
        content = result.get("content")
        if isinstance(content, dict):
            content = content.get("text") or content.get("content") or str(content)
        text = (content or "").strip()
        return (text or None), result.get("provider"), None

    start = time.monotonic()
    try:
        async with AsyncSessionLocal() as db:
            r1 = await chat_for_task(db, msgs, task="deep_reasoning", max_tokens=1500, timeout=timeout, **common)
    except Exception as exc:  # noqa: BLE001
        r1 = {"ok": False, "error": f"AI call failed: {exc}"}
    t1, p1, e1 = _extract(r1)
    if t1 and len(t1) >= 200:
        return t1, p1, None
    first_issue = f"response too short ({len(t1) if t1 else 0} chars via {p1})" if t1 else (e1 or "empty response")
    remaining = timeout - (time.monotonic() - start)
    if remaining < 15:
        return None, p1, first_issue
    # One more try on the plain provider chain, in case the task profile is
    # the thing that failed.
    try:
        async with AsyncSessionLocal() as db:
            r2 = await db_chat(db, msgs, max_tokens=1500, timeout=min(timeout, remaining), **common)
    except Exception as exc:  # noqa: BLE001
        r2 = {"ok": False, "error": f"retry failed: {exc}"}
    t2, p2, e2 = _extract(r2)
    if t2 and len(t2) >= 200:
        return t2, p2, None
    combined = first_issue if (not e2 or e2 == first_issue) else f"{first_issue} | retry: {e2}"
    return None, p2 or p1, combined


def narrative_is_faithful(text: str, obl: dict) -> bool:
    """Does a model-written narrative describe *this* engine read?

    It must be a real paragraph, quote the bull / bear score, quote the plan's
    entry and stop when there is a plan, and not degenerate into repetition
    (a small model looping "10-bar high/low at …" is the failure seen live).
    """
    body = (text or "").strip()
    if len(body) < 200:
        return False
    import re as _re
    # Repetition is measured with the numbers blanked out: a looping model
    # varies the figure each time ("…at 80589.3 …at 80590.3") and would
    # otherwise look novel.
    words = _re.sub(r"\d[\d.,]*", "#", body).split()
    if len(words) >= 40:
        grams = [" ".join(words[i:i + 5]) for i in range(len(words) - 4)]
        if len(set(grams)) / len(grams) < 0.6:
            return False
    sc = obl.get("score") or {}
    flat = body.replace(",", "").replace("*", "")
    low = flat.lower()
    for word, val in (("bull", sc.get("bull", 0)), ("bear", sc.get("bear", 0))):
        near = _re.search(rf"{word}\w*[^0-9\n]{{0,40}}\b{int(val)}\b", low) or _re.search(rf"\b{int(val)}\b[^0-9\n]{{0,25}}{word}", low)
        if not near:
            return False
    sig = obl.get("signal") or {}
    active = sig.get("entry") is not None
    plan = sig if active else (obl.get("last_signal") or {})
    quoted = 0
    wanted = 0
    for key in ("entry", "stop_loss"):
        v = plan.get(key)
        if v is None:
            continue
        wanted += 1
        needle = f"{float(v):.2f}".rstrip("0").rstrip(".")
        if needle in flat or f"{float(v):.1f}" in flat or f"{float(v):.0f}" in flat:
            quoted += 1
    # An active plan must be quoted in full; a stale last signal is context,
    # so naming either its entry or its stop shows the model read it.
    if wanted and (quoted < wanted if active else quoted == 0):
        return False
    return True


def _px(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "n/a"
    return f"{f:,.2f}" if abs(f) >= 1000 else f"{f:.4f}".rstrip("0").rstrip(".") if abs(f) >= 1 else f"{f:.6g}"


def _fallback_analysis(obl: dict, symbol: str, timeframe: str, market: Optional[MarketCapInfo], position: Optional[PositionInfo]) -> str:
    """The engine's own read, written out — every number from the result dict.

    This is what a trader sees whenever no model answer passes the gate, so it
    has to stand on its own: what fired, why the engine is (not) trading, the
    levels, and what would change the picture.
    """
    sig = obl.get("signal") or {}
    sc = obl.get("score") or {}
    comps = sc.get("components") or {}
    buy = int(sc.get("buy", sc.get("bull")) or 0)
    sell = int(sc.get("sell", sc.get("bear")) or 0)
    smin = int(sc.get("scalp_min") or sc.get("required") or 6)
    nmin = int(sc.get("sniper_min") or 9)
    kind = str((obl.get("signal") or {}).get("kind") or "")
    action = str(sig.get("action") or "wait").upper()
    struct = obl.get("structure") or {}
    trend = obl.get("trend") or {}
    fvg = obl.get("fvg") or {}
    ob = obl.get("order_blocks") or {}
    liq = obl.get("liquidity") or {}
    fib = obl.get("fib") or {}
    sess = obl.get("session") or {}
    vol = obl.get("volume") or {}
    news = obl.get("news") or {}
    last = obl.get("last_signal") or {}

    def chips(d: dict) -> str:
        return ", ".join(f"{k} +{v}" for k, v in (d or {}).items()) or "nothing"

    out = []
    # 1. signal & score
    head = f"SIGNAL & SCORE — {symbol} {timeframe}: {action}" + (f" ({kind})" if kind and action != "WAIT" else "")
    if action != "WAIT" and not sig.get("confirmed"):
        head += " on the forming bar (not committed until the close)"
    out.append(head + ".")
    out.append(f"Buy {buy} / sell {sell} — scalp needs {smin}, sniper needs {nmin} plus HTF, a recent sweep, "
               f"the premium/discount zone and the EMA 200 trend. Buy points: {chips(comps.get('buy') or comps.get('bull'))}. "
               f"Sell points: {chips(comps.get('sell') or comps.get('bear'))}.")
    setups = sc.get("setups") or {}
    miss = setups.get("sniper_long_missing") if buy >= sell else setups.get("sniper_short_missing")
    if miss and action == "WAIT":
        out.append(f"Sniper is short of: {', '.join(miss)}.")
    if (why := wait_reason(obl)):
        out.append(f"Why no trade: {why}.")
    # 2. trend & structure
    ema = f"EMA21 {_px(trend.get('ema_fast'))}, EMA50 {_px(trend.get('ema_slow'))}, EMA200 {_px(trend.get('ema_major'))}"
    tlabel = "bullish (fast above slow, price above EMA200)" if trend.get("bull") else "bearish (fast below slow, price below EMA200)" if trend.get("bear") else "mixed — no full EMA alignment"
    out.append(f"\nTREND & STRUCTURE — EMA stack is {tlabel}: {ema}. Market bias is {obl.get('bias_label', 'NEUTRAL')} from the last break ({struct.get('now', 'WAIT')} on this bar); "
               f"last swing high {_px(struct.get('last_swing_high'))} ({struct.get('last_high_type', '—')}), last swing low {_px(struct.get('last_swing_low'))} ({struct.get('last_low_type', '—')}).")
    # 3. zones & liquidity
    zone_bits = []
    if fvg.get("inside_bull"):
        z = fvg.get("bull") or {}
        zone_bits.append(f"price is inside the bullish FVG {_px(z.get('bottom'))}–{_px(z.get('top'))}")
    if fvg.get("inside_bear"):
        z = fvg.get("bear") or {}
        zone_bits.append(f"price is inside the bearish FVG {_px(z.get('bottom'))}–{_px(z.get('top'))}")
    if not zone_bits:
        b, r = fvg.get("bull") or {}, fvg.get("bear") or {}
        parts = []
        if b:
            parts.append(f"bullish FVG {_px(b.get('bottom'))}–{_px(b.get('top'))}")
        if r:
            parts.append(f"bearish FVG {_px(r.get('bottom'))}–{_px(r.get('top'))}")
        zone_bits.append("price is outside the open gaps" + (f" ({'; '.join(parts)})" if parts else " — none open"))
    if ob.get("inside_bull") or ob.get("inside_bear"):
        z = ob.get("bull") if ob.get("inside_bull") else ob.get("bear")
        zone_bits.append(f"and sitting in the {'bullish' if ob.get('inside_bull') else 'bearish'} order block {_px((z or {}).get('bottom'))}–{_px((z or {}).get('top'))}")
    liq_txt = ("a bullish liquidity sweep just printed (wick below the 10-bar low, close back above)" if liq.get("bull_sweep")
               else "a bearish liquidity sweep just printed (wick above the 10-bar high, close back below)" if liq.get("bear_sweep")
               else f"no sweep on this bar; liquidity rests at {_px(liq.get('prev_high'))} above and {_px(liq.get('prev_low'))} below")
    out.append(f"\nZONES & LIQUIDITY — {'; '.join(zone_bits)}. Liquidity: {liq_txt}.")
    # 4. fib, premium/discount, session, volume, news
    pd = "discount (below equilibrium)" if obl.get("discount") else "premium (above equilibrium)" if obl.get("premium") else "at equilibrium"
    fib_txt = (f"auto-fib of the last swing: 0.382 {_px(fib.get('f382'))}, 0.5 {_px(fib.get('f500'))}, 0.618 {_px(fib.get('f618'))} — "
               f"price is {'inside' if fib.get('in_zone') else 'outside'} the 0.382–0.618 zone") if fib.get("f382") is not None else "no confirmed swing for a fib yet"
    out.append(f"\nCONTEXT — {pd}; {fib_txt}. Session {sess.get('name', 'OFF')}{' (London/NY overlap)' if sess.get('overlap') else ''}; volume {str(vol.get('level') or 'N/A').lower()}"
               f"{f' ({float(vol.get('ratio')):.2f}× the 20-bar average)' if vol.get('ratio') else ''}; news window {'ACTIVE — entries blocked' if news.get('blocked') else 'clear'}.")
    # 5. plan
    plan = sig if sig.get("entry") is not None else last
    if plan.get("entry") is not None:
        e, sl_ = float(plan["entry"]), float(plan["stop_loss"])
        risk = abs(e - sl_)
        label = "PLAN" if plan is sig else (
            f"LAST SIGNAL ({str(last.get('action', '')).upper()}"
            f"{', ' + str(last.get('kind')) if last.get('kind') else ''}, {last.get('bars_since', '?')} bars ago)")
        # The ladder is configurable — quote the ratios actually in force.
        conf = obl.get("config") or {}
        rr1, rr2, rr3 = conf.get("tp1_rr", 0.5), conf.get("tp2_rr", 1.0), conf.get("tp3_rr", 1.5)
        buf, lo_atr, hi_atr = conf.get("sl_atr_buffer", 0.25), conf.get("min_risk_atr", 0.25), conf.get("max_risk_atr", 2.5)
        out.append(f"\n{label} — entry {_px(e)}, stop {_px(sl_)} (structural swing ± {buf}×ATR, risk clamped to "
                   f"{lo_atr}–{hi_atr}×ATR; risk {_px(risk)} = {risk / e * 100:.2f}%), "
                   f"TP1 {_px(plan.get('tp1'))} ({rr1}R), TP2 {_px(plan.get('tp2'))} ({rr2}R), TP3 {_px(plan.get('tp3'))} ({rr3}R).")
        oc_plan = (plan.get("outcome") or {}).get("status")
        if oc_plan and plan is not sig:
            out.append(f"It resolved: {str(oc_plan).upper()}.")
        if sig.get("blocked_by_news"):
            out.append("The entry is blocked by the news window.")
    else:
        out.append("\nPLAN — none: no committed signal in the window.")
    # 6. what changes the picture
    nxt = []
    if buy < smin:
        nxt.append(f"buyers need {smin - buy} more point(s) — a BOS above {_px(struct.get('last_swing_high'))} is worth +2, a sweep of {_px(liq.get('prev_low'))} +2")
    if sell < smin:
        nxt.append(f"sellers need {smin - sell} more point(s) — a BOS below {_px(struct.get('last_swing_low'))} is worth +2, a sweep of {_px(liq.get('prev_high'))} +2")
    if last.get("action") and action == "WAIT" and max(buy, sell) >= smin:
        nxt.append(f"the active {str(last.get('action')).upper()} must be answered by the other side before a new {str(last.get('action')).upper()} can print")
    if nxt:
        out.append("\nWHAT CHANGES IT — " + "; ".join(nxt) + ".")
    oc = obl.get("outcomes") or {}
    if oc.get("count"):
        wr = f"{oc['win_rate'] * 100:.0f}%" if oc.get("win_rate") is not None else "n/a"
        out.append(f"\nTRACK RECORD on this window — {oc['count']} signals ({oc.get('scalp', 0)} scalp / {oc.get('sniper', 0)} sniper): "
                   f"TP1 {oc.get('tp1', 0)}, TP2 {oc.get('tp2', 0)}, TP3 {oc.get('tp3', 0)}, stopped {oc.get('sl', 0)}, "
                   f"still open {oc.get('open', 0)} — win rate {wr}.")
    if market and market.market_cap:
        out.append(f"\n{market.name or symbol}: market cap {_fmt_usd(market.market_cap)} (rank {market.market_cap_rank}), 24h volume {_fmt_usd(market.volume_24h)}.")
    if position:
        sig_act = str(sig.get("action") or "wait")
        side = position.side.lower()
        aligned = (side == "long" and sig_act == "buy") or (side == "short" and sig_act == "sell")
        if sig_act == "wait":
            out.append(f"\nOPEN POSITION — your {side.upper()} is {position.pnl:+.2f}% with the engine on WAIT; the structural stop is the swing on the other side of your entry {_px(position.entry_price)}.")
        elif aligned:
            out.append(f"\nOPEN POSITION — the engine agrees with your {side} ({position.pnl:+.2f}%): hold toward TP1 {_px(sig.get('tp1'))}, stop {_px(sig.get('stop_loss'))}.")
        else:
            out.append(f"\nOPEN POSITION — the engine is against your {side} ({position.pnl:+.2f}%): reduce, stop {_px(sig.get('stop_loss'))}.")
    return "\n".join(out)


async def analyze_oblivion(symbol: str, timeframe: str = "1h", exchange: str = "bitget", obl: Optional[dict] = None) -> JarvisAnalysisResponse:
    """Deep LLM analysis for a standalone OBLIVION read. Always returns a response (fallback when LLM fails)."""
    # Fetch obl if not supplied
    if obl is None:
        try:
            from app.services import candles as candle_source
            from app.signals.oblivion import analyze_oblivion as _ana
            candles = await candle_source.fetch(symbol, timeframe, limit=300)
            obl = _ana(candles, timeframe) if candles else {"error": "no candles"}
        except Exception as exc:
            obl = {"error": str(exc), "signal": {"action": "wait"}, "score": {"bull":0,"bear":0,"required":6}}

    # Enrich with market + position
    market = await _market_context(symbol)
    # Try to find position on both venues; prefer exchange then MT5
    position = await _open_position(exchange, symbol)
    if position is None and exchange.lower() != "mt5":
        position = await _open_position("mt5", symbol)

    # Try to fuse Kronos for extra context (best-effort)
    kronos_sig = None
    try:
        from plugins.KronosForecastPlugin.backend.services.forecast_service import run_forecast_cached
        fc = await run_forecast_cached(exchange, symbol, timeframe)
        kronos_sig = fc.signal
    except Exception:
        kronos_sig = None

    # If error/no signal, return deterministic
    if obl.get("error") or (obl.get("signal") or {}).get("action") == "wait" and not obl.get("last_signal"):
        # Still run fallback for WAIT case but don't waste LLM
        analysis = _fallback_analysis(obl, symbol, timeframe, market, position)
        vol = None
        return JarvisAnalysisResponse(
            exchange=exchange, symbol=symbol, timeframe=timeframe, engine="oblivion",
            analysis=analysis, spoken=obl.get("summary") or f"OBLIVION {symbol} {timeframe}: WAIT",
            signal=None, market=market, position=position, position_advice=None, volume=vol, decision="NO_TRADE", learned=False, provider=None, note="OBLIVION WAIT — deterministic summary."
        )

    prompt = _build_user_prompt(obl, symbol, timeframe, market, position, kronos_sig)
    analysis, provider, err = await _run_llm(prompt)
    note = None
    if analysis is None:
        analysis = _fallback_analysis(obl, symbol, timeframe, market, position)
        why = err[:120] if err else "no content"
        if "keyed" in why:
            why = "no cloud provider is free for live analysis (TRAINING_PROVIDER reserves it) and the local model gave no usable answer"
        note = f"Engine read shown — {why}."
        provider = None
        learned = False
    elif not narrative_is_faithful(analysis, obl):
        # The model's words must describe *this* read — quote the score and
        # the plan, and not loop. Otherwise the engine speaks for itself.
        logger.info("[Oblivion] narrative from {} rejected for {} {} — rule-based summary shown", provider, symbol, timeframe)
        analysis = _fallback_analysis(obl, symbol, timeframe, market, position)
        note = f"Engine read shown — the {provider or 'model'} answer did not quote this read (score / plan) so it was discarded."
        provider = None
        learned = False
    else:
        learned = False
        # Optionally store to knowledge? Could add later
    # Build synthetic ForecastSignal-like for UI? Use obl signal as basis
    sig = obl.get("signal") or {}
    # Map to ForecastSignal shape for compatibility? Keep None and let caller use obl directly
    return JarvisAnalysisResponse(
        exchange=exchange, symbol=symbol, timeframe=timeframe, engine="oblivion",
        analysis=analysis, spoken=obl.get("summary") or analysis[:200],
        signal=None, market=market, position=position, position_advice=None, volume=None, decision="OK" if sig.get("action") in ("buy","sell") else "NO_TRADE",
        learned=learned, provider=provider, note=note,
    )
