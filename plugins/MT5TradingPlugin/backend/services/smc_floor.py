"""
MT5 Trading Plugin — deterministic analysis floor (tier 3).

Pure Python. No network, no LLM, no DB. Given the output of
``SMCStrategyEngine.analyze()`` this builds the same ``ai``-shaped block the LLM
review produces, derived entirely from the engine's own numbers.

This is what makes the /mt5-live analysis endpoint structurally incapable of
returning empty, null, or an error: when every provider is unreachable, capped,
or returns malformed output, the caller falls through to ``build()`` and still
answers with a complete, valid, numerically-grounded signal object.

Every field here is computed from candle-derived values. Nothing is invented,
and nothing is a placeholder — the confidence reported is the engine's own
score for the setup, not an estimate of the LLM's opinion.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# Verdict thresholds over the engine's 0..1 confidence.
_TAKE_AT = 0.72
_WATCH_AT = 0.55

# ATR as a percentage of price above which the session is called volatile.
_VOLATILE_ATR_PCT = 1.5


def _fmt(value: Any, places: int = 2) -> str:
    try:
        return f"{float(value):.{places}f}"
    except (TypeError, ValueError):
        return "n/a"


def _verdict_for(confidence: float, rr: float, min_rr: float = 1.5) -> str:
    if confidence >= _TAKE_AT and rr >= min_rr:
        return "take"
    if confidence >= _WATCH_AT:
        return "watch"
    return "skip"


def _top_factors(sig: Dict[str, Any], limit: int = 3) -> List[str]:
    """Highest-contribution factors for a signal, as 'name +0.12' strings.

    Uses the Phase-2 numeric ``score_breakdown`` when present; falls back to the
    engine's ``confluence`` tags so the floor also works on older payloads.
    """
    breakdown = sig.get("score_breakdown") or {}
    factors = breakdown.get("factors") if isinstance(breakdown, dict) else None
    if isinstance(factors, list) and factors:
        ranked = sorted(
            (f for f in factors if isinstance(f, dict)),
            key=lambda f: abs(float(f.get("contribution") or 0.0)),
            reverse=True,
        )
        out = []
        for f in ranked[:limit]:
            contrib = float(f.get("contribution") or 0.0)
            out.append(f"{f.get('name', 'factor')} {contrib:+.3f}")
        return out
    confluence = sig.get("confluence") or []
    return [str(c) for c in confluence[:limit]]


def _market_read(analysis: Dict[str, Any]) -> str:
    bias = analysis.get("bias") or "unclear"
    momentum = analysis.get("momentum") or "normal"
    parts = [
        f"Structure bias {bias}, momentum {momentum}.",
        f"ATR {_fmt(analysis.get('atr'), 5)} ({_fmt(analysis.get('atr_pct'))}% of price),"
        f" RSI {_fmt(analysis.get('rsi'), 1)}, volume z-score {_fmt(analysis.get('volume_z'))}.",
    ]
    rng = analysis.get("range") or {}
    eq = analysis.get("equilibrium")
    last = analysis.get("last_price")
    if rng and eq is not None and last is not None:
        try:
            zone = "premium" if float(last) > float(eq) else "discount"
            parts.append(
                f"Price {_fmt(last, 5)} sits in the {zone} half of the dealing range "
                f"{_fmt(rng.get('low'), 5)}–{_fmt(rng.get('high'), 5)} (equilibrium {_fmt(eq, 5)})."
            )
        except (TypeError, ValueError):
            pass
    events = analysis.get("structure_events") or []
    if events:
        last_ev = events[-1]
        if isinstance(last_ev, dict):
            # _structure_bias emits the broken level as "level".
            parts.append(
                f"Most recent structure event: {last_ev.get('type', 'n/a')} "
                f"{last_ev.get('direction', '')} at "
                f"{_fmt(last_ev.get('level', last_ev.get('price')), 5)}."
            )
    return " ".join(parts)[:600]


def _risk_warning(analysis: Dict[str, Any], reason: str) -> str:
    bits = [
        "Deterministic engine output — no language-model review was available "
        f"({reason})."
    ]
    try:
        atr_pct = float(analysis.get("atr_pct") or 0.0)
        if atr_pct >= _VOLATILE_ATR_PCT:
            bits.append(
                f"ATR is {atr_pct:.2f}% of price — wide stops and slippage risk."
            )
    except (TypeError, ValueError):
        pass
    fb = analysis.get("false_breakout") or {}
    try:
        score = float(fb.get("false_break_score") or 0.0)
        if score >= 60:
            bits.append(
                f"False-break score {score:.0f}/100 on the latest sweep — "
                "recent breakout is likely a stop hunt."
            )
    except (TypeError, ValueError):
        pass
    return " ".join(bits)[:300]


def build(
    analysis: Dict[str, Any],
    *,
    reason: str = "all providers unavailable",
    min_rr: float = 1.5,
) -> Dict[str, Any]:
    """Build a complete, valid ``ai`` block from engine numbers alone.

    Always returns ``available=True`` with ``tier="deterministic"`` and
    ``is_degraded=True``. Handles the no-setups and engine-error cases by
    explaining them rather than returning nothing.
    """
    analysis = analysis if isinstance(analysis, dict) else {}
    signals: List[Dict[str, Any]] = [
        s for s in (analysis.get("signals") or []) if isinstance(s, dict)
    ]

    rated: List[Dict[str, Any]] = []
    for sig in signals:
        try:
            entry = round(float(sig.get("entry")), 6)
        except (TypeError, ValueError):
            continue
        confidence = float(sig.get("confidence") or 0.0)
        rr = float(sig.get("rr") or 0.0)
        verdict = _verdict_for(confidence, rr, min_rr)
        drivers = _top_factors(sig)
        note = (
            f"{sig.get('side', '?')} {sig.get('zone_kind', 'zone')} @ {_fmt(entry, 5)}, "
            f"RR {rr:.2f}, engine score {confidence:.2f}"
        )
        if drivers:
            note += f". Top factors: {', '.join(drivers)}"
        rated.append({
            "entry": entry,
            "verdict": verdict,
            "confidence": round(max(0.0, min(1.0, confidence)), 3),
            "note": note[:280],
        })

    top_pick = None
    top_conf = 0.0
    takeables = [r for r in rated if r["verdict"] == "take"]
    pool = takeables or rated
    if pool:
        best = max(pool, key=lambda r: r["confidence"])
        top_pick = best["entry"]
        top_conf = best["confidence"]

    engine_error = analysis.get("error")
    if engine_error:
        bias_comment = (
            f"Engine could not complete structural analysis: {engine_error}. "
            "No setup is proposed."
        )
        market_read = f"Insufficient data for a structural read ({engine_error})."
    elif not signals:
        bias_comment = (
            f"Structure bias is {analysis.get('bias') or 'unclear'} but no zone "
            "currently meets the entry, risk-reward and volume-confirmation "
            "requirements. Standing aside."
        )
        market_read = _market_read(analysis)
    else:
        bias_comment = (
            f"Structure bias {analysis.get('bias') or 'unclear'}; "
            f"{len(signals)} candidate setup(s) scored, "
            f"{len(takeables)} clearing the {_TAKE_AT:.2f} take threshold."
        )
        market_read = _market_read(analysis)

    return {
        "available": True,
        "bias_comment": bias_comment[:400],
        "market_read": market_read,
        "rated_signals": rated,
        "top_pick_entry": top_pick,
        "risk_warning": _risk_warning(analysis, reason),
        "provider_used": None,
        "tier": "deterministic",
        "confidence": round(top_conf, 3),
        "is_degraded": True,
        "reason": reason,
    }
