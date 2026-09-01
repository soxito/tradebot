"""The market-structure story — the SMC flow told candle by candle.

The reference analysis reads a chart as a twelve-beat narrative: initial
trend → liquidity sweep → accumulation → demand reaction → CHoCH → BOS →
FVG creation → expansion → liquidity target → premium-zone approach →
current reaction → summary. Every beat answers *why* price moved, not just
whether the candle was bullish — that is the difference between describing
a chart and reading one.

This module rebuilds that story from what ``SMCStrategyEngine.analyze``
already found, for any instrument and timeframe. It invents nothing: a beat
with no evidence behind it is skipped, and the flow line is composed from
the beats that survived. Output feeds three surfaces — the SMC page's story
panel, the room seats' evidence lines, and the Kronos forecast prompt.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .smc_strategy import Candle


def _fmt(value: Any) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    a = abs(v)
    if a >= 1000:
        return f"{v:,.2f}"
    if a >= 1:
        return f"{v:.4f}".rstrip("0").rstrip(".")
    return f"{v:.6f}".rstrip("0").rstrip(".")


def _strong_body(c: Candle) -> float:
    """Body as a fraction of the bar's range — 1.0 is a marubozu."""
    rng = c.high - c.low
    return abs(c.close - c.open) / rng if rng > 0 else 0.0


def _is_bull(c: Candle) -> bool:
    return c.close > c.open


def build_narrative(
    candles: List[Candle],
    analysis: Dict[str, Any],
) -> Dict[str, Any]:
    """Analysis + candles → the ordered market-structure story.

    Returns ``{"steps": [...], "flow": "...", "summary": "..."}``. Steps are
    ``{"step", "title", "detail", "reason", "time", "index"}`` — time/index
    locate the beat on the chart. Pure: reads, never fetches.
    """
    if len(candles) < 40:
        return {"steps": [], "flow": "", "summary": ""}

    events = analysis.get("structure_events") or []
    bias = analysis.get("bias") or "neutral"
    rng = analysis.get("range") or {}
    sweeps = analysis.get("false_breakout") or {}
    liquidity = analysis.get("liquidity") or {}
    zones = analysis.get("zones") or []
    last_price = float(analysis.get("last_price") or candles[-1].close)

    bull = bias == "bullish"
    # The story pivots on the most recent change of character: everything
    # before it is the old trend, everything after is the new one.
    choch = next(
        (e for e in reversed(events) if e.get("type") == "CHoCH"), None)
    pivot_idx = int(choch["index"]) if choch else len(candles) // 2

    steps: List[Dict[str, Any]] = []

    def add(title: str, detail: str, reason: str, *, idx: Optional[int] = None) -> None:
        c = candles[idx] if idx is not None and 0 <= idx < len(candles) else None
        steps.append({
            "step": len(steps) + 1,
            "title": title,
            "detail": detail,
            "reason": reason,
            "time": c.time if c else None,
            "index": idx if c else None,
        })

    # 1 — the trend before the turn.
    old_dir = "bearish" if (choch and choch.get("direction") == "bullish") else (
        "bullish" if (choch and choch.get("direction") == "bearish") else
        ("bearish" if bull else "bullish"))
    seg = candles[:max(pivot_idx, 5)]
    drift = (seg[-1].close / seg[0].open - 1.0) * 100.0 if seg and seg[0].open else 0.0
    add(
        f"Initial {'Bearish' if old_dir == 'bearish' else 'Bullish'} Candles",
        f"The move into this structure ran {'down' if old_dir == 'bearish' else 'up'} "
        f"{drift:+.1f}% across the first {len(seg)} bars.",
        ("Sellers controlled the market and pushed price toward lower-liquidity "
         "areas" if old_dir == "bearish" else
         "Buyers controlled the market and pushed price toward buy-side liquidity"),
        idx=max(pivot_idx, 5) - 1,
    )

    # 2 — the liquidity sweep (stop hunt) near the turn.
    swept = (sweeps.get("swept_lows") or []) if bull else (sweeps.get("swept_highs") or [])
    if swept:
        lvl = swept[-1]
        add(
            "Liquidity Sweep Candles",
            f"Price spiked through the {'low' if bull else 'high'} at {_fmt(lvl)} "
            "and closed back inside.",
            (f"Smart Money collected sell-side liquidity below {_fmt(lvl)} and "
             "absorbed selling pressure before the reversal" if bull else
             f"Smart Money collected buy-side liquidity above {_fmt(lvl)} and "
             "absorbed buying before the reversal"),
        )

    # 3 — accumulation: small-bodied balance bars between the pivot and now.
    post = candles[pivot_idx:]
    if len(post) >= 3:
        small = [c for c in post if _strong_body(c) < 0.4]
        if len(small) >= 2:
            add(
                "Accumulation Phase Candles",
                f"{len(small)} small-body bars held a range after the turn — "
                "balance between buyers and sellers.",
                "Institutional traders were building positions while waiting for "
                "the structure to confirm",
                idx=candles.index(small[-1]) if small[-1] in candles else None,
            )

    # 4 — the reaction that launched the new leg.
    strong = [c for c in post if _strong_body(c) >= 0.6 and
              (_is_bull(c) if bull else not _is_bull(c))]
    if strong:
        c0 = strong[0]
        add(
            f"{'Bullish' if bull else 'Bearish'} Reaction Candles From "
            f"{'Demand' if bull else 'Supply'} Zone",
            f"A strong {'bullish' if bull else 'bearish'} bar at {_fmt(c0.low)}–"
            f"{_fmt(c0.high)} confirmed the {'demand' if bull else 'supply'} area held.",
            (f"Buyers stepped in with strength and shifted momentum" if bull else
             "Sellers stepped in with strength and shifted momentum"),
            idx=candles.index(strong[0]) if strong[0] in candles else None,
        )

    # 5 + 6 — CHoCH, then the BOS chain that confirmed continuation.
    if choch:
        prot = choch.get("protected_low") or choch.get("protected_high")
        add(
            "CHoCH (Change of Character) Candles",
            f"Price broke the prior swing at {_fmt(choch.get('level'))} and flipped "
            f"the structure {'bullish' if choch.get('direction') == 'bullish' else 'bearish'}.",
            (f"Selling pressure weakened and buyers took control"
             f"{f'; the protected low sits at {_fmt(prot)}' if prot else ''}"
             if choch.get("direction") == "bullish" else
             f"Buying pressure weakened and sellers took control"
             f"{f'; the protected high sits at {_fmt(prot)}' if prot else ''}"),
        )
    boses = [e for e in events if e.get("type") == "BOS"]
    if boses:
        last_bos = boses[-1]
        add(
            f"{'Bullish' if last_bos.get('direction') == 'bullish' else 'Bearish'} "
            "BOS Candles",
            f"{len(boses)} break(s) of structure confirmed — the latest through "
            f"{_fmt(last_bos.get('level'))}.",
            "Break of Structure confirmed continuation and stronger participation "
            "on the new leg",
        )

    # 7 — fair value gaps created on the expansion.
    gap_kind = "bullish_fvg" if bull else "bearish_fvg"
    fvgs = [z for z in zones if z.get("kind") == gap_kind]
    if fvgs:
        z0 = fvgs[-1]
        add(
            "FVG Formation Candles",
            f"Fast expansion left a fair-value gap at {_fmt(z0.get('bottom'))}–"
            f"{_fmt(z0.get('top'))}.",
            "Strong institutional buying created price imbalance, leaving an area "
            "price may return to mitigate" if bull else
            "Strong institutional selling created price imbalance, leaving an area "
            "price may return to mitigate",
        )

    # 8 — trend continuation: closes respecting the new structure.
    if boses and len(post) >= 2:
        tail = candles[max(int(boses[-1]["index"]), 0):]
        respected = sum(
            1 for i in range(1, len(tail))
            if (_is_bull(tail[i]) and tail[i].close >= tail[i - 1].close)
            or (not _is_bull(tail[i]) and tail[i].close <= tail[i - 1].close)
        )
        if tail:
            add(
                "Trend Continuation Candles",
                f"Price extended while holding the {'bullish' if bull else 'bearish'} "
                f"structure ({respected}/{max(len(tail) - 1, 1)} bars respected it).",
                (f"Buyers maintained control and protected previous demand zones" if bull
                 else "Sellers maintained control and capped every pullback"),
                idx=len(candles) - 1,
            )

    # 9 — the liquidity pool price is walking toward.
    pools = (liquidity.get("buyside") or []) if bull else (liquidity.get("sellside") or [])
    ahead = [p for p in pools if (float(p) > last_price if bull else float(p) < last_price)]
    if ahead:
        target = min(ahead) if bull else max(ahead)
        add(
            "Liquidity Target Near " + ("Highs" if bull else "Lows"),
            f"Resting liquidity sits at {_fmt(target)} — the magnet price is "
            "drifting toward.",
            (f"Market is targeting buy-side liquidity above {_fmt(target)} before "
             "any deeper reaction" if bull else
             f"Market is targeting sell-side liquidity below {_fmt(target)} before "
             "any deeper reaction"),
        )

    # 10 — where price sits in the dealing range (premium/discount).
    if rng.get("low") is not None and rng.get("high") is not None:
        lo, hi = float(rng["low"]), float(rng["high"])
        pct = (last_price - lo) / (hi - lo) if hi > lo else 0.5
        premium = pct >= 0.5
        add(
            f"{'Premium' if premium else 'Discount'} Zone Approach",
            f"Price sits at {pct * 100:.0f}% of the dealing range "
            f"({_fmt(lo)}–{_fmt(hi)}) — the {'upper/supply' if premium else 'lower/demand'} half.",
            ("Price entered a high-value zone where profit-taking and seller "
             "reactions can appear" if premium else
             "Price sits in a discount zone where entries are favoured and "
             "buyers have the edge"),
        )

    # 11 — what the newest bars are doing at that zone.
    tail3 = candles[-3:]
    if tail3:
        hesitating = all(_strong_body(c) < 0.5 for c in tail3)
        add(
            "Current Reaction Candles",
            ("The newest bars show hesitation — small bodies near the active zone."
             if hesitating else
             f"The newest bars still carry momentum toward "
             f"{'higher prices' if _is_bull(tail3[-1]) else 'lower prices'}."),
            (f"Sellers may defend this zone — supply and liquidity concentrate "
             "here" if hesitating and premium else
             "Watch whether the zone holds; the reaction decides the next leg"),
            idx=len(candles) - 1,
        )

    # 12 — the summary: the flow line + the lesson.
    flow = " → ".join(s["title"].replace(" Candles", "") for s in steps)
    summary = (
        "Every candle tells a story through its location, structure, and "
        "liquidity. Professional analysis focuses on why price moved, not only "
        "whether the candle was bullish or bearish."
    )
    steps.append({
        "step": len(steps) + 1,
        "title": "Market Structure Summary",
        "detail": flow or "No confirmed structure on this timeframe.",
        "reason": summary,
        "time": None,
        "index": None,
    })

    return {"steps": steps, "flow": flow, "summary": summary}


def evidence_lines(narrative: Dict[str, Any], limit: int = 3) -> List[str]:
    """The story as short seat-readable lines — the last beats, not all twelve.

    The seats need the *state* of the structure (where price is, what just
    confirmed), not the full history; the earliest beats are context the
    chart already shows.
    """
    steps = [s for s in (narrative or {}).get("steps", []) if s.get("title") != "Market Structure Summary"]
    out = []
    for s in steps[-limit:]:
        out.append(f"{s['title'].replace(' Candles', '')}: {s['detail']}")
    return out
