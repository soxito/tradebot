"""Deterministic agent analysis for when no LLM provider is reachable.

Every provider is rate-limited, and the shared ones are used hourly, so the
agents spent much of the day behind an open circuit breaker. What they returned
in that state was ``_safe_hold`` — ``action: hold, confidence: 0`` and a
reasoning string that only explained that the provider was missing. On the
dashboard that reads as the agents having stopped reporting: a row of blank
holds that say nothing about the market.

Nothing about that is necessary. The context handed to an agent already carries
a full local technical read — RSI, MACD, ADX/DI, EMAs, Bollinger position,
stochastic RSI, volume and buy/sell ratio — computed by
``app.signals.technical`` without any model involved, plus whatever the Jarvis
brain has recorded for the symbol. This module turns that into a real decision:
a direction, a calibrated confidence, and reasoning that quotes the actual
numbers it used.

The output is deliberately the same shape a model would have produced, so
callers cannot tell the difference structurally — only ``ai_called`` and
``source`` say the analysis was done locally.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# ── Indicator weights ───────────────────────────────────────────────────────
# Each evaluator contributes a signed score in [-1, 1]; the weights say how much
# a given read is worth. Trend and momentum dominate, volume only confirms.
WEIGHTS: Dict[str, float] = {
    "trend": 1.00,      # EMA50 vs EMA200
    "macd": 0.85,       # histogram sign and slope
    "adx": 0.70,        # directional movement, gated on trend strength
    "rsi": 0.65,        # overbought / oversold
    "bollinger": 0.50,  # position within the band
    "stoch_rsi": 0.45,
    "volume": 0.40,     # relative volume + buy/sell split
}

#: Below this the agents report "hold" rather than pick a side.
DECISION_THRESHOLD = 0.18

#: Confidence is capped without a model — a local read is a real opinion, but
#: it has no macro or news awareness, and should not outrank a full analysis.
MAX_LOCAL_CONFIDENCE = 0.72


def _num(value: Any) -> Optional[float]:
    """Coerce to float, or None for anything unusable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # drop NaN


def _indicators(context: Dict[str, Any]) -> Dict[str, Any]:
    """Pull the indicator block out of whichever shape the caller passed."""
    tech = context.get("technical")
    if isinstance(tech, dict):
        ind = tech.get("indicators")
        if isinstance(ind, dict) and ind:
            return ind
    ind = context.get("indicators")
    if isinstance(ind, dict) and ind:
        return ind
    # Multi-timeframe contexts: prefer the 1h read, else any populated one.
    mtf = context.get("multi_timeframe")
    if isinstance(mtf, dict):
        for tf in ("1h", "15m", "4h", "5m"):
            block = mtf.get(tf)
            if isinstance(block, dict) and isinstance(block.get("indicators"), dict):
                return block["indicators"]
    return {}


# ── Individual reads ────────────────────────────────────────────────────────
# Each returns (score in [-1, 1], human sentence) or None when the input is
# missing, so a sparse context simply carries fewer evaluators.

def _read_trend(ind: Dict[str, Any]) -> Optional[tuple[float, str]]:
    ema50, ema200 = _num(ind.get("ema50")), _num(ind.get("ema200"))
    if ema50 is None or ema200 is None or ema200 == 0:
        return None
    spread = (ema50 - ema200) / abs(ema200)
    score = max(-1.0, min(1.0, spread * 40))  # ±2.5 % spread saturates
    side = "above" if spread > 0 else "below"
    return score, f"EMA50 {side} EMA200 by {abs(spread) * 100:.2f}%"


def _read_macd(ind: Dict[str, Any]) -> Optional[tuple[float, str]]:
    hist = _num(ind.get("macd_histogram"))
    if hist is None:
        macd, sig = _num(ind.get("macd")), _num(ind.get("macd_signal"))
        if macd is None or sig is None:
            return None
        hist = macd - sig
    price = _num(ind.get("price")) or 1.0
    norm = hist / (abs(price) * 0.002) if price else hist  # 0.2 % of price saturates
    score = max(-1.0, min(1.0, norm))
    return score, f"MACD histogram {hist:+.4g} ({'bullish' if hist > 0 else 'bearish'})"


def _read_adx(ind: Dict[str, Any]) -> Optional[tuple[float, str]]:
    adx = _num(ind.get("adx"))
    plus, minus = _num(ind.get("plus_di")), _num(ind.get("minus_di"))
    if adx is None or plus is None or minus is None:
        return None
    if adx < 20:  # no trend worth trading — ADX contributes nothing
        return 0.0, f"ADX {adx:.1f} — no directional trend"
    strength = min(1.0, (adx - 20) / 30)  # ADX 50 saturates
    total = plus + minus
    bias = (plus - minus) / total if total else 0.0
    return bias * strength, (
        f"ADX {adx:.1f} with +DI {plus:.1f} / -DI {minus:.1f}"
    )


def _read_rsi(ind: Dict[str, Any]) -> Optional[tuple[float, str]]:
    rsi = _num(ind.get("rsi"))
    if rsi is None:
        return None
    # Mean-reverting: oversold is bullish, overbought bearish.
    score = max(-1.0, min(1.0, (50 - rsi) / 25))
    if rsi <= 30:
        note = f"RSI {rsi:.1f} — oversold"
    elif rsi >= 70:
        note = f"RSI {rsi:.1f} — overbought"
    else:
        note = f"RSI {rsi:.1f} — neutral"
    return score, note


def _read_bollinger(ind: Dict[str, Any]) -> Optional[tuple[float, str]]:
    pct_b = _num(ind.get("bb_pct_b"))
    if pct_b is None:
        return None
    score = max(-1.0, min(1.0, (0.5 - pct_b) * 2.5))
    if pct_b <= 0.0:
        note = f"price below the lower band (%B {pct_b:.2f})"
    elif pct_b >= 1.0:
        note = f"price above the upper band (%B {pct_b:.2f})"
    else:
        note = f"%B {pct_b:.2f} within the bands"
    return score, note


def _read_stoch_rsi(ind: Dict[str, Any]) -> Optional[tuple[float, str]]:
    sr = _num(ind.get("stoch_rsi"))
    if sr is None:
        return None
    scaled = sr / 100.0 if sr > 1.0 else sr  # accept 0-1 or 0-100
    score = max(-1.0, min(1.0, (0.5 - scaled) * 2.5))
    return score, f"Stoch RSI {scaled:.2f}"


def _read_volume(ind: Dict[str, Any]) -> Optional[tuple[float, str]]:
    ratio = _num(ind.get("volume_ratio"))
    buy_ratio = _num(ind.get("buy_ratio"))
    if ratio is None and buy_ratio is None:
        return None
    # Volume only confirms: it scales the buy/sell imbalance rather than
    # producing a direction of its own.
    conviction = min(1.0, (ratio or 1.0) / 2.0)
    bias = ((buy_ratio - 0.5) * 2) if buy_ratio is not None else 0.0
    parts = []
    if ratio is not None:
        parts.append(f"volume ×{ratio:.2f} of average")
    if buy_ratio is not None:
        parts.append(f"buy share {buy_ratio * 100:.0f}%")
    return max(-1.0, min(1.0, bias * conviction)), ", ".join(parts)


_READERS = {
    "trend": _read_trend,
    "macd": _read_macd,
    "adx": _read_adx,
    "rsi": _read_rsi,
    "bollinger": _read_bollinger,
    "stoch_rsi": _read_stoch_rsi,
    "volume": _read_volume,
}


# ── Memory vocabulary ───────────────────────────────────────────────────────

#: Phrases the desk already uses, harvested from the memory prompt so the local
#: reports read in the same language as everything else in the brain.
_VOCAB_PATTERNS = (
    r"\b(?:bullish|bearish|neutral)\s+(?:bias|structure|regime|continuation)\b",
    r"\b(?:accumulation|distribution|consolidation|breakout|breakdown)\b",
    r"\b(?:order block|liquidity sweep|fair value gap|market structure shift)\b",
    r"\b(?:higher high|lower low|higher low|lower high)s?\b",
    r"\b(?:overbought|oversold|divergence|momentum fading)\b",
)


def memory_vocabulary(memory_prompt: str, limit: int = 4) -> List[str]:
    """Terms the brain already uses for this symbol, in first-seen order.

    The agents are asked to speak the desk's language rather than invent their
    own, so the phrasing comes from what has already been written down.
    """
    if not memory_prompt:
        return []
    seen: List[str] = []
    lowered = memory_prompt.lower()
    for pattern in _VOCAB_PATTERNS:
        for match in re.findall(pattern, lowered):
            term = match if isinstance(match, str) else match[0]
            term = term.strip()
            if term and term not in seen:
                seen.append(term)
            if len(seen) >= limit:
                return seen
    return seen


def analyze_locally(
    *,
    role: str,
    context: Dict[str, Any],
    memory_prompt: str = "",
    reason: str = "",
) -> Dict[str, Any]:
    """Produce a real decision from the local technical read alone.

    Returns the same shape an LLM-backed agent returns. ``ai_called`` is False
    and ``source`` is ``"local_analysis"`` so callers can tell where it came
    from, but the action, confidence and reasoning are genuine analysis of the
    numbers in ``context`` — never a placeholder.
    """
    symbol = str(context.get("symbol") or "?").upper()
    ind = _indicators(context)

    scored: Dict[str, float] = {}
    notes: List[str] = []
    for key, reader in _READERS.items():
        try:
            got = reader(ind)
        except Exception:  # noqa: BLE001 — one bad field must not kill the read
            got = None
        if got is None:
            continue
        score, note = got
        scored[key] = score
        notes.append(note)

    if not scored:
        # Genuinely nothing to analyse — say so honestly rather than guess.
        return {
            "agent_name": context.get("agent_name", role),
            "agent_role": role,
            "action": "hold",
            "confidence": 0.0,
            "reasoning": (
                f"No technical data available for {symbol} — no indicators in "
                f"context, so no local read is possible."
                + (f" ({reason})" if reason else "")
            ),
            "ai_called": False,
            "source": "local_analysis",
            "evaluators": {},
            "degraded": True,
        }

    total_weight = sum(WEIGHTS[k] for k in scored)
    composite = sum(scored[k] * WEIGHTS[k] for k in scored) / total_weight

    if composite >= DECISION_THRESHOLD:
        action = "buy"
    elif composite <= -DECISION_THRESHOLD:
        action = "sell"
    else:
        action = "hold"

    # Confidence blends conviction with agreement: evaluators pulling in
    # opposite directions should not read as a strong call.
    agreeing = sum(1 for v in scored.values() if (v > 0) == (composite > 0) and v != 0)
    agreement = agreeing / len(scored) if scored else 0.0
    confidence = min(MAX_LOCAL_CONFIDENCE, abs(composite) * 0.75 + agreement * 0.25)
    if action == "hold":
        confidence = min(confidence, 0.35)

    vocab = memory_vocabulary(memory_prompt)
    lead = f"{symbol}: local technical read is {action.upper()}"
    if vocab:
        lead += f" — reads as {vocab[0]}"
    body = "; ".join(notes)
    tail = (
        f"Composite {composite:+.2f} across {len(scored)} evaluators "
        f"({agreeing}/{len(scored)} agreeing)."
    )
    why = f"{lead}. {body}. {tail}"
    if reason:
        why += f" Computed locally — {reason}."

    return {
        "agent_name": context.get("agent_name", role),
        "agent_role": role,
        "action": action,
        "confidence": round(confidence, 3),
        "reasoning": why,
        "ai_called": False,
        "source": "local_analysis",
        "evaluators": {k: round(v, 3) for k, v in scored.items()},
        "composite_score": round(composite, 4),
        "memory_terms": vocab,
        "degraded": False,
    }
