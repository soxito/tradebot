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
    "adx": 0.90,        # directional movement — raised after 2026-08-28 miss where -DI> +DI was ignored and a BUY fired into a heavy sell-off
    "rsi": 0.65,        # overbought / oversold
    "bollinger": 0.35,  # position within the band — lowered: %B near lower band in a downtrend is breakdown, not bounce
    "stoch_rsi": 0.40,
    "volume": 0.65,     # relative volume + buy/sell split — raised: 61% sell flow must veto a weak bullish EMA spread
}

#: Below this the agents report "hold" rather than pick a side.
#: Raised from 0.18 to 0.32 after the 2026-08-28 incident: composite +0.28 from a weak EMA spread (1.03%) was taking BUY into a market where the 4h trend, ADX -DI and selling volume all pointed down. A higher bar forces real confluence.
DECISION_THRESHOLD = 0.32

#: Confidence is capped without a model — a local read is a real opinion, but
#: it has no macro or news awareness, and should not outrank a full analysis.
#: Lowered 0.72 → 0.55 so a local fallback can never masquerade as a high-conviction AI call and bypass the risk manager's 0.50 gate.
MAX_LOCAL_CONFIDENCE = 0.55


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
    if adx < 15:  # lowered from 20 — ADX 18-20 with strong DI divergence is tradable, especially on metals during heavy selling
        return 0.0, f"ADX {adx:.1f} — no directional trend"
    # Stronger scaling: ADX 30 already 0.75, ADX 25 at 0.5, so a 24.7 ADX with -DI dominance is not dismissed as 0.15 strength.
    # Post-mortem 2026-08-28: ADX 24.7 +DI 11 / -DI 26 was scored -0.06 and ignored, leaving a BUY into a sell-off.
    strength = min(1.0, (adx - 15) / 20)  # ADX 35 saturates (was 50)
    total = plus + minus
    bias = (plus - minus) / total if total else 0.0
    # Amplify when DI divergence is decisive (>40% spread): TrendFactory wants strong DI to count more than weak ADX.
    di_spread = abs(plus - minus) / total if total else 0.0
    if di_spread > 0.35:
        strength = min(1.0, strength * 1.35)
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


def _volatility_regime(context: Dict[str, Any], ind: Dict[str, Any]) -> tuple[bool, str]:
    """Is this a volatile expansion that demands waiting for a level?"""
    mom = context.get("momentum") or {}
    atr_exp = _num(mom.get("atr_expansion"))
    if atr_exp is not None and atr_exp >= 1.35:
        return True, f"ATR expansion {atr_exp:.2f}x (volatile breakout, wait for retest)"
    if atr_exp is not None and atr_exp >= 1.20:
        return True, f"ATR expansion {atr_exp:.2f}x (elevated, need level)"
    # Fallback: BB bandwidth or ATR percentile via technical scoring note
    bb = _num(ind.get("bb_pct_b"))
    # If price is pinned to band edge during expansion, that's also volatile chase risk
    if bb is not None and (bb <= 0.08 or bb >= 0.92) and atr_exp is not None and atr_exp >= 1.10:
        return True, f"ATR {atr_exp:.2f}x with price at band edge %B {bb:.2f} (chase risk)"
    return False, ""


def _has_structural_level(context: Dict[str, Any], price: Optional[float]) -> tuple[bool, str]:
    """Is price at/near a tradeable structural level the desk would enter from?"""
    if price is None or price <= 0:
        return False, ""
    atr = _num(_indicators(context).get("atr"))
    # distance that counts as "at the level" — 0.6 ATR or 0.4% of price, whichever larger
    tol = max((atr or 0) * 0.6, price * 0.004) if atr else price * 0.006
    # 1) Fib golden zone
    try:
        fib = context.get("fib") or {}
        gz = fib.get("golden_zone") or {}
        low, high = _num(gz.get("low")), _num(gz.get("high"))
        if low and high:
            # inside golden zone or within tol of its edge
            if (low - tol) <= price <= (high + tol):
                return True, f"fib golden zone {low:.2f}-{high:.2f}"
            # also show distance
            dist = min(abs(price - low), abs(price - high))
            if dist <= tol * 1.2:
                return True, f"near fib golden zone {low:.2f}-{high:.2f} ({dist:.2f} away)"
    except Exception:
        pass
    # 2) SMC zones (order blocks / FVGs the chart draws)
    try:
        for key in ("smc_zones", "smc_structure"):
            blk = context.get(key)
            if isinstance(blk, dict):
                # smc_structure evidence sometimes carries zone prices in steps
                pass
        zones = context.get("smc_zones") or []
        if isinstance(zones, list):
            for z in zones:
                if not isinstance(z, dict):
                    continue
                zl, zh = _num(z.get("low")), _num(z.get("high"))
                if zl is None or zh is None:
                    zl = zh = _num(z.get("price")) or _num(z.get("level"))
                if zl is None or zh is None:
                    continue
                lo, hi = min(zl, zh), max(zl, zh)
                if (lo - tol) <= price <= (hi + tol):
                    return True, f"SMC {z.get('type','zone')} {lo:.2f}-{hi:.2f}"
    except Exception:
        pass
    # 3) Supply/demand + channel compact payload
    try:
        sd = context.get("sd_channels") or {}
        if isinstance(sd, dict):
            # zones compact often has supply/demand lists with price
            for bucket in ("demand", "supply", "zones", "supply_demand"):
                lst = sd.get(bucket)
                if isinstance(lst, list):
                    for z in lst:
                        if not isinstance(z, dict):
                            continue
                        zp = _num(z.get("price")) or _num(z.get("level")) or _num(z.get("center"))
                        if zp and abs(price - zp) <= tol:
                            return True, f"SD {bucket} {zp:.2f}"
                elif isinstance(lst, dict):
                    for arr in lst.values():
                        if isinstance(arr, list):
                            for z in arr:
                                if isinstance(z, dict):
                                    zp = _num(z.get("price")) or _num(z.get("center"))
                                    if zp and abs(price - zp) <= tol:
                                        return True, f"SD level {zp:.2f}"
    except Exception:
        pass
    return False, ""


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

    # ── Post-mortem veto: heavy selling into a weak bullish EMA must not fire BUY ──
    # On 2026-08-28 the local composite was +0.28 from EMA50 just 1% above EMA200, while ADX -DI dominated (+DI 11 / -DI 26), MACD was bearish and sell flow was 61-71%. The board's own Market Analyst correctly called bearish 0.65, but 4/5 seats ran the same local formula and out-voted it. This veto detects that pattern: a small positive composite with strong bearish DI + bearish MACD + sell-heavy volume.
    veto_note = ""
    try:
        adx_v = scored.get("adx", 0.0)
        macd_v = scored.get("macd", 0.0)
        vol_v = scored.get("volume", 0.0)
        if composite > 0 and composite < 0.40 and adx_v < -0.20 and macd_v < -0.10 and vol_v < -0.10:
            veto_note = (
                f" VETO: weak bullish composite {composite:+.2f} overridden by bearish structure"
                f" (ADX {adx_v:.2f}, MACD {macd_v:.2f}, volume {vol_v:.2f}) — forced to HOLD."
            )
            composite = min(composite, 0.15)  # drag below threshold so action becomes hold
    except Exception:
        pass

    # ── Volatility + level quality gate — the "wait for the right moment" ──
    # The desk's edge is at levels (fib golden zone, order block, FVG, channel edge),
    # not in no-man's land mid-range. In volatile expansion (ATR 1.2-1.35x) a market
    # order in the middle of the range is a chase that gets wicked. This gate forces
    # the signal_generator to HOLD with a named level to wait for, rather than invent
    # a market entry that has no structure behind it.
    level_note = ""
    vol_note = ""
    try:
        is_volatile, vnote = _volatility_regime(context, ind)
        price = _num(ind.get("price")) or _num(context.get("current_price"))
        has_level, lnote = _has_structural_level(context, price)
        mom = context.get("momentum") or {}
        range_pos = _num(mom.get("range_position_pct"))
        bb = _num(ind.get("bb_pct_b"))
        atr_exp = _num(mom.get("atr_expansion"))

        # Effective threshold rises in volatile regime — need real confluence, not a lean
        effective_threshold = DECISION_THRESHOLD
        if is_volatile and role == "signal_generator":
            effective_threshold = max(DECISION_THRESHOLD, 0.38)
            vol_note = f" | volatile regime {vnote} — bar raised to {effective_threshold:.2f}"

        # Mid-range with no level is never an entry, volatile or not
        mid_range = False
        if bb is not None and 0.28 <= bb <= 0.72:
            # %B 0.28-0.72 is the middle half of the Bollinger envelope — no edge
            if range_pos is not None and 30 <= range_pos <= 70:
                mid_range = True
            elif range_pos is None and not has_level:
                mid_range = True

        if role == "signal_generator" and abs(composite) >= DECISION_THRESHOLD and mid_range and not has_level:
            # Downgrade a would-be trade that has no level to wait
            veto_note += (
                f" LEVEL WAIT: price mid-range (%B {bb:.2f} range {range_pos if range_pos is not None else '—'}%)"
                f" with no structural level nearby — waiting for pullback to"
                f" {lnote or 'fib golden zone / order block / demand'}. {vnote}"
            )
            # pull composite just inside hold
            if composite > 0:
                composite = min(composite, effective_threshold - 0.02)
            else:
                composite = max(composite, -effective_threshold + 0.02)

        # Even with a level, volatile chase needs confirmation — if action would have
        # fired at mid-range without volume confirmation, make it wait
        if role == "signal_generator" and is_volatile and not has_level and abs(composite) < 0.48:
            veto_note += f" VOLATILE WAIT: {vnote} at %B {bb} without level — resting order at level, not market chase."
            if composite > 0:
                composite = min(composite, effective_threshold - 0.01)
            else:
                composite = max(composite, -effective_threshold + 0.01)

        # Use effective threshold for final decision when volatile
        if is_volatile and role == "signal_generator":
            if composite >= effective_threshold:
                action = "buy"
            elif composite <= -effective_threshold:
                action = "sell"
            else:
                action = "hold"
            level_note = vol_note
        else:
            if composite >= DECISION_THRESHOLD:
                action = "buy"
            elif composite <= -DECISION_THRESHOLD:
                action = "sell"
            else:
                action = "hold"
    except Exception:
        # Never let the waiting logic break the read
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
    if veto_note:
        why += veto_note
    if level_note:
        why += level_note
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
