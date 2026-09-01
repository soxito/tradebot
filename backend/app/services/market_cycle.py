"""Bitcoin 1064-day market cycle — the desk's calendar read of the four-year wave.

The pattern (verified on every completed cycle since launch): from each cycle
bottom, Bitcoin rises for ≈1064 days into its top, then bleeds ≈365 days into
the next bottom. The bottoms 2015-01-14, 2018-12-15 and 2022-11-21 each start
a green box; the top lands 1064 days later, the next bottom 365 days after
that. Because alts follow BTC, the read applies market-wide to crypto.

This module is the same shape as ``macro_context``: named constants, a pure
snapshot builder (unit-testable against fixed dates), a cached async resolver
that fetches ten years of BTC dailies in one Yahoo call for validation and
live price, and a per-symbol bias that is advisory only — an unavailable
calendar is silence, never a trade block.

Everything here projects; nothing here predicts intraday. The Kronos forecast
owns the path, the seats own the verdict — the cycle owns the season.
"""
from __future__ import annotations

import asyncio
import statistics
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from loguru import logger

# ── Constants ───────────────────────────────────────────────────────────────

#: Bottom → top. The number on the chart the pattern was read from.
BULL_DAYS = 1064
#: Top → next bottom.
BEAR_DAYS = 365

#: Cycle bottoms the calendar is anchored to. The 2011 bottom predates usable
#: Yahoo dailies, so validation starts from 2015. Editable at runtime via
#: room settings; this is the fallback when nothing is persisted.
DEFAULT_ANCHORS: Tuple[str, ...] = ("2015-01-14", "2018-12-15", "2022-11-21")

#: How close a realised top/bottom must land to the projection (days) to count
#: as a pattern hit.
HIT_TOLERANCE_DAYS = 45

#: The late-bull caution window — this many days before the projected top the
#: bias starts leaning defensive (tops are a process, not a date).
LATE_PHASE_WINDOW_DAYS = 90

#: BTC dailies for the cycle read. One Yahoo D1 call returns ten years, which
#: covers the three anchored cycles plus the live one.
_BTC_SYMBOL = "BTCUSD"
_BARS_LIMIT = 4000

#: Monthly candles for the cycle screen. Yahoo's 1mo interval reaches back to
#: Bitcoin's first prints, so the bar count is whatever the configured years
#: ask for (plus a small buffer for the forming month).
_MONTHLY_BUFFER = 3

CyclePhaseT = str  # "bull" | "bear"

#: Halving dates inside the covered window — shown on the cycle calendar.
HALVINGS: Tuple[str, ...] = ("2016-07-09", "2020-05-11", "2024-04-20", "2028-04-16")


# ── The snapshot ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CycleWindow:
    """One green or red box: bottom → top (bull) or top → bottom (bear)."""

    start: str          # ISO date the phase began
    end: str            # ISO date the phase is projected/known to end
    phase: CyclePhaseT
    projected: bool     # True when the end date is a projection, not history


@dataclass(frozen=True)
class CycleSnapshot:
    """Where the calendar says the market is today. Symbol-independent."""

    phase: CyclePhaseT
    anchor: str                      # ISO date of the cycle bottom in force
    day_of_cycle: int                # days since that bottom (0 = bottom day)
    phase_day: int                   # days into the current phase
    phase_days_total: int            # 1064 in bull, 365 in bear
    phase_pct: float                 # 0..1 through the phase
    projected_top: str               # ISO date this cycle's top is due
    projected_bottom: str            # ISO date the next bottom is due
    days_to_top: int
    days_to_bottom: int
    late_phase: bool                 # inside the caution window before a turn
    price: Optional[float] = None    # latest BTC close, when bars were supplied
    cycle_high: Optional[float] = None
    cycle_low: Optional[float] = None
    validation: Dict[str, Any] = field(default_factory=dict)
    as_of: str = ""                  # ISO date the snapshot was built for

    @property
    def ok(self) -> bool:
        return bool(self.anchor)


@dataclass(frozen=True)
class CycleBias:
    """The snapshot resolved for one instrument — the macro_bias convention.

    ``normalized`` is signed **for the long side**: positive is a tailwind,
    negative a headwind. Non-crypto instruments are simply not applicable —
    gold and forex do not follow the Bitcoin calendar.
    """

    applicable: bool
    normalized: float = 0.0
    phase: CyclePhaseT = "bear"
    day_of_cycle: int = 0
    reason: str = ""
    lines: List[str] = field(default_factory=list)


def _parse_anchor(value: str) -> Optional[date]:
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except (TypeError, ValueError):
        return None


def parse_anchors(raw: Any) -> List[date]:
    """Anchor dates from whatever the caller had — env JSON, DB text, list."""
    if raw is None:
        items: Sequence[Any] = DEFAULT_ANCHORS
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            items = DEFAULT_ANCHORS
        else:
            import json

            try:
                items = json.loads(text)
                if not isinstance(items, (list, tuple)):
                    items = [items]
            except ValueError:
                items = [p for p in text.split(",") if p.strip()]
    elif isinstance(raw, (list, tuple)):
        items = raw
    else:
        items = DEFAULT_ANCHORS

    out: List[date] = []
    for item in items:
        parsed = _parse_anchor(item if isinstance(item, str) else str(item))
        if parsed:
            out.append(parsed)
    return sorted(out)


def build_cycle_snapshot(
    anchors: Any,
    *,
    today: Optional[date] = None,
    bars: Optional[Sequence[Dict[str, Any]]] = None,
    bull_days: int = BULL_DAYS,
    bear_days: int = BEAR_DAYS,
) -> CycleSnapshot:
    """Calendar + optional dailies → where the cycle stands. Pure.

    The last anchor starts the live cycle; earlier anchors become history the
    validation block scores. With no bars the calendar still resolves — the
    pattern is a date law first, a price observation second.
    """
    day = today or datetime.now(timezone.utc).date()
    bottoms = parse_anchors(anchors)

    # The live cycle starts at the last anchor on or before today; an anchor
    # in the future would mean the calendar has run past its own history.
    past = [a for a in bottoms if a <= day]
    if not past:
        return CycleSnapshot(
            phase="bear", anchor="", day_of_cycle=0, phase_day=0,
            phase_days_total=bull_days, phase_pct=0.0,
            projected_top="", projected_bottom="",
            days_to_top=0, days_to_bottom=0, late_phase=False,
            as_of=day.isoformat(),
        )
    anchor = past[-1]
    day_of_cycle = (day - anchor).days

    in_bull = day_of_cycle < bull_days
    phase: CyclePhaseT = "bull" if in_bull else "bear"
    phase_day = day_of_cycle if in_bull else day_of_cycle - bull_days
    phase_total = bull_days if in_bull else bear_days
    phase_pct = max(0.0, min(1.0, phase_day / max(1, phase_total)))

    projected_top = anchor + timedelta(days=bull_days)
    projected_bottom = projected_top + timedelta(days=bear_days)
    days_to_top = (projected_top - day).days
    days_to_bottom = (projected_bottom - day).days
    # Caution window before whichever turn is next: late bull leans defensive
    # ahead of the top, late bear starts watching for the new bottom.
    late = 0 <= min(days_to_top if in_bull else days_to_bottom, 10**6) <= LATE_PHASE_WINDOW_DAYS

    snap = CycleSnapshot(
        phase=phase,
        anchor=anchor.isoformat(),
        day_of_cycle=day_of_cycle,
        phase_day=max(0, phase_day),
        phase_days_total=phase_total,
        phase_pct=phase_pct,
        projected_top=projected_top.isoformat(),
        projected_bottom=projected_bottom.isoformat(),
        days_to_top=days_to_top,
        days_to_bottom=days_to_bottom,
        late_phase=late,
        as_of=day.isoformat(),
        validation=validate_pattern(bottoms, bars=bars, bull_days=bull_days, bear_days=bear_days),
    )

    if bars:
        closes = _bar_series(bars)
        if closes:
            start_ts = _date_to_ts(anchor)
            cycle_bars = [c for c in closes if c[0] >= start_ts]
            if cycle_bars:
                snap = replace_price_stats(
                    snap,
                    price=cycle_bars[-1][1],
                    cycle_high=max(c[1] for c in cycle_bars),
                    cycle_low=min(c[1] for c in cycle_bars),
                )
    return snap


def replace_price_stats(
    snap: CycleSnapshot, *, price: Optional[float], cycle_high: Optional[float], cycle_low: Optional[float]
) -> CycleSnapshot:
    """Frozen-dataclass update for the price block (keeps the builder pure)."""
    return CycleSnapshot(
        phase=snap.phase, anchor=snap.anchor, day_of_cycle=snap.day_of_cycle,
        phase_day=snap.phase_day, phase_days_total=snap.phase_days_total,
        phase_pct=snap.phase_pct, projected_top=snap.projected_top,
        projected_bottom=snap.projected_bottom, days_to_top=snap.days_to_top,
        days_to_bottom=snap.days_to_bottom, late_phase=snap.late_phase,
        price=price, cycle_high=cycle_high, cycle_low=cycle_low,
        validation=snap.validation, as_of=snap.as_of,
    )


# ── Validation: did history land where the calendar said? ───────────────────


def _bar_series(bars: Sequence[Dict[str, Any]]) -> List[Tuple[int, float]]:
    """[(day_start_ts, close)] oldest first, junk dropped."""
    out: List[Tuple[int, float]] = []
    for row in bars or []:
        try:
            ts = int(row.get("time"))
            close = float(row.get("close"))
        except (TypeError, ValueError):
            continue
        if close > 0:
            out.append((ts, close))
    out.sort(key=lambda x: x[0])
    return out


def _date_to_ts(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


def _ts_to_date(ts: int) -> date:
    return datetime.fromtimestamp(ts, tz=timezone.utc).date()


def _extreme_date(
    bars: Sequence[Tuple[int, float]], start: date, end: date, *, high: bool
) -> Optional[date]:
    """Date of the cycle's highest close (or lowest) inside [start, end]."""
    lo, hi = _date_to_ts(start), _date_to_ts(end)
    window = [(ts, c) for ts, c in bars if lo <= ts <= hi]
    if not window:
        return None
    best = max(window, key=lambda x: x[1]) if high else min(window, key=lambda x: x[1])
    return _ts_to_date(best[0])


def validate_pattern(
    bottoms: Sequence[date],
    *,
    bars: Optional[Sequence[Dict[str, Any]]] = None,
    bull_days: int = BULL_DAYS,
    bear_days: int = BEAR_DAYS,
) -> Dict[str, Any]:
    """Score every completed cycle: projected vs realised top and bottom.

    Returns per-cycle rows (projected date, actual date, error in days, hit)
    plus a hit-rate. The hit-rate is the confidence the room quotes when it
    says the calendar expects a turn — a pattern that only worked once is a
    coincidence, not a season.
    """
    rows: List[Dict[str, Any]] = []
    closes = _bar_series(bars or [])

    ordered = sorted(bottoms)
    for i, bottom in enumerate(ordered):
        proj_top = bottom + timedelta(days=bull_days)
        proj_bottom = proj_top + timedelta(days=bear_days)
        row: Dict[str, Any] = {
            "bottom": bottom.isoformat(),
            "projected_top": proj_top.isoformat(),
            "projected_bottom": proj_bottom.isoformat(),
        }
        if closes:
            # The realised top is the highest close between this bottom and the
            # next anchor (or the projection when this is the newest cycle).
            horizon_end = ordered[i + 1] if i + 1 < len(ordered) else proj_bottom
            actual_top = _extreme_date(closes, bottom, horizon_end, high=True)
            if actual_top:
                err = (actual_top - proj_top).days
                row.update(
                    actual_top=actual_top.isoformat(),
                    top_error_days=err,
                    top_hit=abs(err) <= HIT_TOLERANCE_DAYS,
                )
            # Realised bottom: lowest close in the bear window after the top.
            if i + 1 < len(ordered):
                actual_bottom = _extreme_date(closes, proj_top, ordered[i + 1], high=False)
                if actual_bottom:
                    err = (actual_bottom - proj_bottom).days
                    row.update(
                        actual_bottom=actual_bottom.isoformat(),
                        bottom_error_days=err,
                        bottom_hit=abs(err) <= HIT_TOLERANCE_DAYS,
                    )
        rows.append(row)

    hits = [r for r in rows if r.get("top_hit")]
    bottom_hits = [r for r in rows if r.get("bottom_hit")]
    scored = [r for r in rows if "top_hit" in r]
    rate = (len(hits) / len(scored)) if scored else None
    return {
        "cycles": rows,
        "top_hit_rate": rate,
        "bottom_hit_rate": (len(bottom_hits) / len(scored)) if scored else None,
        "tolerance_days": HIT_TOLERANCE_DAYS,
    }


# ── Day expectations: what this calendar day did in past cycles ─────────────


def day_expectation(
    bottoms: Sequence[date],
    bars: Sequence[Dict[str, Any]],
    offset: int,
    *,
    bull_days: int = BULL_DAYS,
    bear_days: int = BEAR_DAYS,
    horizon_days: int = 7,
) -> Dict[str, Any]:
    """What the same day-of-cycle did in every prior cycle.

    ``offset`` is days since a cycle bottom. For each anchored cycle that has
    data at that offset, take the forward return over ``horizon_days`` and the
    max drawdown in the following month. The average of those is what the
    calendar expects *today* — "day 1006 of the cycle" rather than any single
    date's noise.
    """
    closes = _bar_series(bars)
    if not closes or offset < 0:
        return {"offset": offset, "samples": 0}

    returns: List[float] = []
    drawdowns: List[float] = []
    for bottom in sorted(bottoms):
        base_ts = _date_to_ts(bottom + timedelta(days=offset))
        base = next((c for ts, c in closes if ts >= base_ts), None)
        if base is None or base <= 0:
            continue
        target_ts = _date_to_ts(bottom + timedelta(days=offset + horizon_days))
        target = next((c for ts, c in closes if ts >= target_ts), None)
        if target is None or target <= 0:
            continue
        returns.append((target / base - 1.0) * 100.0)

        # Worst peak-to-trough inside the following 30 days.
        end_ts = _date_to_ts(bottom + timedelta(days=offset + horizon_days + 30))
        window = [c for ts, c in closes if base_ts <= ts <= end_ts]
        if len(window) >= 3:
            peak = window[0]
            worst = 0.0
            for c in window[1:]:
                worst = min(worst, (c / peak - 1.0) * 100.0)
                peak = max(peak, c)
            drawdowns.append(worst)

    if not returns:
        return {"offset": offset, "samples": 0}

    out: Dict[str, Any] = {
        "offset": offset,
        "horizon_days": horizon_days,
        "samples": len(returns),
        "avg_return_pct": round(statistics.fmean(returns), 2),
        "best_return_pct": round(max(returns), 2),
        "worst_return_pct": round(min(returns), 2),
    }
    if len(returns) >= 2:
        out["median_return_pct"] = round(statistics.median(returns), 2)
    if drawdowns:
        out["avg_max_drawdown_pct"] = round(statistics.fmean(drawdowns), 2)
    return out


# ── Cycle alignment: every cycle's path on one axis ─────────────────────────


def _close_on_or_after(closes: List[Tuple[int, float]], day: date) -> Optional[Tuple[int, float]]:
    """First bar printed on or after `day` (a bottom can land on a weekend)."""
    lo = _date_to_ts(day)
    for ts, close in closes:
        if ts >= lo:
            return (ts, close)
    return None


def cycle_analogs(
    bottoms: Sequence[date],
    bars: Sequence[Dict[str, Any]],
    *,
    today: Optional[date] = None,
    bull_days: int = BULL_DAYS,
    bear_days: int = BEAR_DAYS,
) -> Dict[str, Any]:
    """Every cycle's price path, normalised and aligned by day-of-cycle.

    Each cycle becomes ``[(day_offset, pct_from_bottom)]`` — the 2015 cycle's
    day 400 sits on the same x as the current cycle's day 400. This is the
    "compare previous movement with current" view: where the live path bends
    against the ghosts is where the pattern says attention is due.
    """
    day = today or datetime.now(timezone.utc).date()
    closes = _bar_series(bars)
    if not closes:
        return {"cycles": [], "current": None}

    paths: List[Dict[str, Any]] = []
    for bottom in sorted(bottoms):
        base = _close_on_or_after(closes, bottom)
        if base is None or base[1] <= 0:
            continue
        end = min(bottom + timedelta(days=bull_days + bear_days), day)
        points = []
        for ts, close in closes:
            if ts < base[0] or ts > _date_to_ts(end):
                continue
            offset = (_ts_to_date(ts) - bottom).days
            points.append([offset, round((close / base[1] - 1.0) * 100.0, 2)])
        if len(points) >= 2:
            paths.append({
                "bottom": bottom.isoformat(),
                "live": bottom + timedelta(days=bull_days + bear_days) > day,
                "points": points,
            })

    # The live cycle: from the last anchor on/before today to the newest bar.
    current = None
    past = [a for a in sorted(bottoms) if a <= day]
    if past:
        anchor = past[-1]
        base = _close_on_or_after(closes, anchor)
        if base and base[1] > 0:
            cap = _date_to_ts(day)
            points = [
                [(_ts_to_date(ts) - anchor).days, round((close / base[1] - 1.0) * 100.0, 2)]
                for ts, close in closes
                if base[0] <= ts <= cap
            ]
            if len(points) >= 2:
                current = {"bottom": anchor.isoformat(), "points": points}

    return {"cycles": paths, "current": current, "bull_days": bull_days, "bear_days": bear_days}


def expectation_series(
    bottoms: Sequence[date],
    bars: Sequence[Dict[str, Any]],
    start_offset: int,
    *,
    days: int = 30,
    bull_days: int = BULL_DAYS,
    bear_days: int = BEAR_DAYS,
) -> List[Dict[str, Any]]:
    """The next `days` days of the cycle, as history scored them.

    One row per day-of-cycle ahead: what the *same day* did in every prior
    cycle over the following day. This is the daily prediction table — not a
    promise, a base rate with samples attached.
    """
    out: List[Dict[str, Any]] = []
    for offset in range(max(0, start_offset), max(0, start_offset) + max(1, days)):
        row = day_expectation(bottoms, bars, offset, horizon_days=1, bull_days=bull_days, bear_days=bear_days)
        row["avg_return_pct"] = row.get("avg_return_pct")
        out.append(row)
    return out


# ── Calendar grid for the cycle page ────────────────────────────────────────

def build_cycle_calendar(
    anchors: Any,
    *,
    year: int,
    month: int,
    today: Optional[date] = None,
    bull_days: int = BULL_DAYS,
    bear_days: int = BEAR_DAYS,
) -> List[Dict[str, Any]]:
    """One month of days, each tagged with its phase and cycle position.

    The page paints green/red boxes from this; ``projected`` marks days the
    calendar has not lived through yet, so the UI can dim them.
    """
    day = today or datetime.now(timezone.utc).date()
    bottoms = parse_anchors(anchors)
    if not bottoms:
        return []

    first = date(year, month, 1)
    nxt = date(year + (month == 12), (month % 12) + 1, 1)
    out: List[Dict[str, Any]] = []
    d = first
    while d < nxt:
        past = [a for a in bottoms if a <= d]
        if not past:
            d += timedelta(days=1)
            continue
        anchor = past[-1]
        offset = (d - anchor).days
        in_bull = offset < bull_days
        phase: CyclePhaseT = "bull" if in_bull else "bear"
        proj_top = anchor + timedelta(days=bull_days)
        proj_bottom = proj_top + timedelta(days=bear_days)
        out.append({
            "date": d.isoformat(),
            "weekday": d.weekday(),           # 0=Mon … 6=Sun
            "phase": phase,
            "day_of_cycle": offset,
            "phase_pct": round(max(0.0, min(1.0, (offset if in_bull else offset - bull_days)
                                             / (bull_days if in_bull else bear_days))), 3),
            "projected": d > day,
            "is_top": d == proj_top,
            "is_bottom": d == proj_top + timedelta(days=bear_days) or d == anchor,
            "is_anchor": d == anchor,
            "is_today": d == day,
            "is_halving": d.isoformat() in HALVINGS,
            "days_to_top": (proj_top - d).days,
            "days_to_bottom": (proj_bottom - d).days,
        })
        d += timedelta(days=1)
    return out


def build_windows(
    anchors: Any,
    *,
    today: Optional[date] = None,
    bull_days: int = BULL_DAYS,
    bear_days: int = BEAR_DAYS,
    history_cycles: int = 3,  # kept for API compatibility; no longer trims output
) -> List[CycleWindow]:
    """Green/red boxes for chart overlays: every configured cycle + the live one.

    All historical anchor cycles are returned so the chart can show the full
    pattern history. Each phase is marked projected=True when its end date is
    still in the future.
    """
    day = today or datetime.now(timezone.utc).date()
    bottoms = sorted(parse_anchors(anchors))
    out: List[CycleWindow] = []
    for i, bottom in enumerate(bottoms):
        proj_top = bottom + timedelta(days=bull_days)
        proj_bottom = proj_top + timedelta(days=bear_days)
        out.append(CycleWindow(
            start=bottom.isoformat(), end=proj_top.isoformat(), phase="bull",
            projected=proj_top > day,
        ))
        out.append(CycleWindow(
            start=proj_top.isoformat(),
            end=(bottoms[i + 1] if i + 1 < len(bottoms) else proj_bottom).isoformat(),
            phase="bear",
            projected=(bottoms[i + 1] if i + 1 < len(bottoms) else proj_bottom) > day,
        ))
    return out


# ── Per-symbol bias ──────────────────────────────────────────────────────────

#: Bases that ride the Bitcoin cycle. Alts follow BTC; everything else
#: (metals, FX, indices) has no business reading this calendar.
_CYCLE_BASES = {"BTC", "SOL", "ETH", "XRP", "DOGE", "ADA", "AVAX", "LINK", "BNB", "LTC"}


def cycle_applies(symbol: str) -> bool:
    """True for instruments the Bitcoin calendar plausibly drives."""
    s = (symbol or "").upper().replace("/", "").replace("-", "").replace(":", "")
    if s.endswith("USDT"):
        base = s[:-4]
    elif s.endswith("USD"):
        base = s[:-3]
    else:
        base = s
    return base in _CYCLE_BASES


def cycle_bias(
    symbol: str,
    snap: Optional[CycleSnapshot],
) -> CycleBias:
    """The cycle resolved for one instrument. Advisory; never gates.

    Early/mid bull is a tailwind, the caution window before the top fades it,
    bear is a headwind that eases as the projected bottom nears (accumulation
    season). Non-crypto gets ``applicable=False`` and silence.
    """
    if snap is None or not snap.ok or not cycle_applies(symbol):
        return CycleBias(applicable=False)

    if snap.phase == "bull":
        if snap.late_phase:
            normalized, reason = 0.1, (
                f"late bull — day {snap.day_of_cycle} of the cycle, projected top "
                f"{snap.projected_top} ({snap.days_to_top}d away); season turning"
            )
        else:
            normalized, reason = 0.5, (
                f"bull phase — day {snap.day_of_cycle} since the {snap.anchor} bottom, "
                f"projected top {snap.projected_top}"
            )
    else:
        if snap.late_phase:
            normalized, reason = 0.0, (
                f"late bear — projected bottom {snap.projected_bottom} "
                f"({snap.days_to_bottom}d); accumulation season approaching"
            )
        else:
            normalized, reason = -0.5, (
                f"bear phase — {snap.days_to_bottom}d to the projected bottom "
                f"{snap.projected_bottom}; cycle headwind for longs"
            )

    lines = [
        f"BTC cycle: {snap.phase.upper()} — day {snap.day_of_cycle} of the cycle "
        f"(anchor {snap.anchor}).",
        f"Projected top {snap.projected_top} · projected bottom {snap.projected_bottom}.",
    ]
    if snap.validation.get("top_hit_rate") is not None:
        rate = float(snap.validation["top_hit_rate"])
        lines.append(
            f"Pattern history: tops landed within {snap.validation.get('tolerance_days', HIT_TOLERANCE_DAYS)}d "
            f"of projection in {round(rate * 100)}% of scored cycles."
        )
    return CycleBias(
        applicable=True,
        normalized=normalized,
        phase=snap.phase,
        day_of_cycle=snap.day_of_cycle,
        reason=reason,
        lines=lines,
    )


# ── Evidence lines for the room context ─────────────────────────────────────


def evidence_lines(snap: CycleSnapshot) -> List[str]:
    """What every seat reads about the season. Kept short — context, not a sermon."""
    if not snap.ok:
        return []
    lines = [
        f"Cycle phase: {snap.phase.upper()} — day {snap.day_of_cycle} since the "
        f"{snap.anchor} bottom ({round(snap.phase_pct * 100)}% through the "
        f"{'accumulation/markup' if snap.phase == 'bull' else 'distribution'} phase).",
        f"Projected top: {snap.projected_top} ({snap.days_to_top}d) · "
        f"projected bottom: {snap.projected_bottom} ({snap.days_to_bottom}d).",
    ]
    if snap.price and snap.cycle_high:
        off_high = (snap.price / snap.cycle_high - 1.0) * 100.0
        lines.append(f"BTC {round(snap.price):,.0f} — {round(off_high, 1)}% off the cycle high.")
    return lines


# ── Async resolution (cached, never raises) ─────────────────────────────────

_lock = asyncio.Lock()
_cached: Dict[str, Any] = {"snap": None, "bars": None, "ts": 0.0}
_monthly_cache: Dict[str, Any] = {"bars": None, "years": 0, "ts": 0.0}
_early_monthly_cache: Dict[str, Any] = {"bars": None, "ts": 0.0}
_early_daily_cache: Dict[str, Any] = {"bars": None, "ts": 0.0}
# Per-timeframe full-history cache for /cycle/candles: {tf: {bars, ts}}
_btc_tf_cache: Dict[str, Dict[str, Any]] = {}

# CoinMetrics free community API — no key required, carries BTC/USD from 2010-07.
# page_size 10000 covers 2010→now (~5900 daily) in one call; if the API paginates,
# the fetcher below follows next_page_token.
_COINMETRICS_URL = (
    "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
    "?assets=btc&metrics=PriceUSD&frequency=1d&page_size=10000"
    "&start_time=2010-07-01&end_time={end}"
)
_COINMETRICS_CACHE_TTL = 86400  # 24 h — early history never changes


def reset_cache() -> None:
    """Drop the cached snapshot — anchors or phase lengths just changed."""
    _cached["snap"] = None
    _cached["ts"] = 0.0


async def _fetch_early_btc_monthly(yahoo_first_ts: int) -> List[Dict[str, Any]]:
    """Monthly OHLCV bars for the period before Yahoo's BTC-USD coverage.

    Fetches daily closes from CoinMetrics (free, no key) for 2010-07-18 up to
    the day before ``yahoo_first_ts``, then aggregates to monthly candles using
    the same {time, open, high, low, close, volume=0} shape the Yahoo bars use.
    Returns [] on any error so the chart degrades gracefully.
    """
    now_mono = time.monotonic()
    async with _lock:
        cached = _early_monthly_cache.get("bars")
        if cached is not None and now_mono - _early_monthly_cache["ts"] < _COINMETRICS_CACHE_TTL:
            return cached

    import httpx

    end_date = datetime.utcfromtimestamp(yahoo_first_ts).strftime("%Y-%m-%d")
    url = _COINMETRICS_URL.format(end=end_date)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; TradeBot/1.0)"})
            resp.raise_for_status()
            rows = resp.json().get("data", [])
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[Cycle] CoinMetrics early history unavailable: {exc}")
        return []

    # Aggregate daily closes → monthly OHLCV keyed by (year, month)
    months: Dict[tuple, List[float]] = {}
    for row in rows:
        price_str = row.get("PriceUSD")
        if not price_str:
            continue
        try:
            price = float(price_str)
        except (ValueError, TypeError):
            continue
        t = row.get("time", "")[:7]  # "YYYY-MM"
        if not t:
            continue
        y, m = int(t[:4]), int(t[5:7])
        months.setdefault((y, m), []).append(price)

    bars: List[Dict[str, Any]] = []
    sorted_months = sorted(months.keys())
    prev_close: Optional[float] = None
    for y, m in sorted_months:
        closes = months[(y, m)]
        if not closes:
            continue
        # First of month at midnight UTC
        ts = int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp())
        o = prev_close if prev_close is not None else closes[0]
        h = max(closes)
        lo = min(closes)
        c = closes[-1]
        bars.append({"time": ts, "open": o, "high": h, "low": lo, "close": c, "volume": 0.0})
        prev_close = c

    async with _lock:
        _early_monthly_cache.update(bars=bars, ts=time.monotonic())
    logger.debug(f"[Cycle] CoinMetrics backfill: {len(bars)} early monthly candles ({end_date})")
    return bars


async def _fetch_early_btc_daily(yahoo_first_ts: int) -> List[Dict[str, Any]]:
    """Daily OHLCV bars for 2010-07-17 → yahoo_first_ts (pre-Yahoo gap).

    Same CoinMetrics source as monthly but kept as daily candles
    (open=prev close, high/low/close from PriceUSD, volume 0). Used to
    backfill D1/W1 and to synthesize pre-Bitget H1/H4.
    """
    now_mono = time.monotonic()
    async with _lock:
        cached = _early_daily_cache.get("bars")
        if cached is not None and now_mono - _early_daily_cache["ts"] < _COINMETRICS_CACHE_TTL:
            # Filter to requested cutoff without refetching
            return [b for b in cached if b["time"] < yahoo_first_ts]

    import httpx

    end_date = datetime.utcfromtimestamp(yahoo_first_ts).strftime("%Y-%m-%d")
    url = _COINMETRICS_URL.format(end=end_date)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; TradeBot/1.0)"})
            resp.raise_for_status()
            rows = resp.json().get("data", [])
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[Cycle] CoinMetrics daily backfill unavailable: {exc}")
        return []

    # Build sorted daily closes
    daily: List[tuple[int, float]] = []
    for row in rows:
        price_str = row.get("PriceUSD")
        if not price_str:
            continue
        try:
            price = float(price_str)
        except (ValueError, TypeError):
            continue
        t = row.get("time", "")[:10]
        if not t:
            continue
        try:
            d = date.fromisoformat(t)
        except ValueError:
            continue
        if price <= 0:
            continue
        ts = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
        daily.append((ts, price))
    daily.sort(key=lambda x: x[0])
    # Deduplicate same day (keep last)
    dedup: Dict[int, float] = {}
    for ts, p in daily:
        dedup[ts] = p
    sorted_days = sorted(dedup.items())
    bars: List[Dict[str, Any]] = []
    prev_close: Optional[float] = None
    for ts, close in sorted_days:
        o = prev_close if prev_close is not None else close
        h = max(o, close)
        lo = min(o, close)
        bars.append({"time": ts, "open": o, "high": h, "low": lo, "close": close, "volume": 0.0})
        prev_close = close
    # Cache full daily range
    async with _lock:
        _early_daily_cache.update(bars=bars, ts=time.monotonic())
    logger.debug(f"[Cycle] CoinMetrics daily backfill: {len(bars)} daily candles ({end_date})")
    return [b for b in bars if b["time"] < yahoo_first_ts]


def _daily_to_weekly(daily_bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Aggregate daily bars → weekly (Mon 00:00 UTC) OHLCV."""
    if not daily_bars:
        return []
    buckets: Dict[int, List[Dict[str, Any]]] = {}
    for b in daily_bars:
        ts = int(b["time"])
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        # Monday 00:00 UTC of that week
        monday = dt - timedelta(days=dt.weekday())
        bucket_ts = int(datetime(monday.year, monday.month, monday.day, tzinfo=timezone.utc).timestamp())
        buckets.setdefault(bucket_ts, []).append(b)
    out: List[Dict[str, Any]] = []
    for bucket_ts in sorted(buckets.keys()):
        group = sorted(buckets[bucket_ts], key=lambda x: x["time"])
        o = group[0]["open"]
        c = group[-1]["close"]
        h = max(x["high"] for x in group)
        lo = min(x["low"] for x in group)
        out.append({"time": bucket_ts, "open": o, "high": h, "low": lo, "close": c, "volume": 0.0})
    return out


def _synthesize_hourly_from_daily(daily_bars: List[Dict[str, Any]], interval_hours: int = 1) -> List[Dict[str, Any]]:
    """Synthesize H1/H4 bars from daily candles for pre-Bitget gap.

    Each daily candle is expanded into 24/6 hourly points with flat OHLC
    (= daily close) and volume 0, stamped at 00:00,01:00… UTC. Marked synthetic
    so the chart can display a badge. The values are approximate but the
    timeline is continuous from genesis.
    """
    if not daily_bars or interval_hours not in (1, 4):
        return []
    out: List[Dict[str, Any]] = []
    step = interval_hours * 3600
    per_day = 24 // interval_hours
    for b in daily_bars:
        day_ts = int(b["time"])
        close = float(b["close"])
        o = float(b["open"])
        h = float(b["high"])
        lo = float(b["low"])
        for i in range(per_day):
            ts = day_ts + i * step
            # For hourly synthetic, flatten to daily close; keep daily high/low only on first bucket
            out.append({
                "time": ts,
                "open": close if i > 0 else o,
                "high": h if i == 0 else close,
                "low": lo if i == 0 else close,
                "close": close,
                "volume": 0.0,
                "synthetic": True,
            })
    return out


async def _fetch_yahoo_btc_full(tf: str, want: int) -> List[Dict[str, Any]]:
    """Yahoo BTC D1/W1 full history via period1/period2 (avoids range=max bug).

    yahoo_provider's range=max for 1d returns monthly (144 points) — the period API
    returns true daily 4367+ points from 2014-09-17. This helper uses the period
    endpoint directly for BTC D1/W1 and parses like _fetch_series.
    """
    import httpx
    ticker = "BTC-USD"
    interval = "1d" if tf == "D1" else "1wk" if tf == "W1" else "1mo"
    # Yahoo daily/weekly starts 2014-09-17; ask from that genesis to now
    period1 = 1410912000  # 2014-09-17 00:00 UTC — first Yahoo BTC daily
    period2 = int(datetime.now(timezone.utc).timestamp()) + 86400
    YF_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
    _HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TradeBot/1.0)"}
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            r = await client.get(
                f"{YF_BASE}/{ticker}",
                params={"period1": period1, "period2": period2, "interval": interval, "includePrePost": "false"},
                headers=_HEADERS,
            )
            r.raise_for_status()
            result = (r.json().get("chart") or {}).get("result") or []
            if not result:
                return []
            node = result[0]
            stamps = node.get("timestamp") or []
            quote = ((node.get("indicators") or {}).get("quote") or [{}])[0]
            opens = quote.get("open") or []
            highs = quote.get("high") or []
            lows = quote.get("low") or []
            closes = quote.get("close") or []
            vols = quote.get("volume") or []
            bars: List[Dict[str, Any]] = []
            for i, ts in enumerate(stamps):
                try:
                    o = opens[i]; h = highs[i]; lo = lows[i]; c = closes[i]
                except IndexError:
                    continue
                if None in (o, h, lo, c):
                    continue
                v = vols[i] if i < len(vols) else None
                bars.append({"time": int(ts), "open": float(o), "high": float(h), "low": float(lo), "close": float(c), "volume": float(v) if v is not None else 0.0})
            bars.sort(key=lambda x: x["time"])
            # Trim to want most recent
            if len(bars) > want:
                bars = bars[-want:]
            return bars
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[Cycle] Yahoo period fetch {tf} failed: {exc}")
        return []


async def resolve_monthly_bars(years: Optional[int] = None, force: bool = False) -> List[Dict[str, Any]]:
    """Monthly BTC candles for the cycle screen — as far back as configured.

    Yahoo's monthly interval carries BTC from 2014-09. CoinMetrics backfills
    the gap back to 2010-07, giving the full exchange-traded history.
    Cached per year-span; failure is an empty list, never an exception.
    """
    cfg = _settings()
    want = max(1, min(20, int(years or getattr(cfg, "CYCLE_HISTORY_YEARS", 0) or 15)))
    async with _lock:
        now = time.monotonic()
        hit = (
            not force
            and _monthly_cache["bars"]
            and _monthly_cache["years"] == want
            and now - _monthly_cache["ts"] < max(60, int(getattr(cfg, "CYCLE_CACHE_TTL_SECONDS", 900) or 900))
        )
        if hit:
            return _monthly_cache["bars"]

    from app.exchanges import yahoo_provider

    limit = want * 12 + _MONTHLY_BUFFER
    try:
        bars = await yahoo_provider.fetch_candles(_BTC_SYMBOL, "MN1", limit=limit)
    except Exception as exc:  # noqa: BLE001 — the screen must never break on a feed
        logger.debug(f"[Cycle] BTC monthlies unavailable: {exc}")
        bars = []
    bars = (bars or [])[-limit:]

    # Prepend pre-Yahoo history from CoinMetrics when the Yahoo series is capped.
    # Yahoo's BTC-USD starts 2014-09; anything beyond that needs the backfill.
    if bars:
        early = await _fetch_early_btc_monthly(bars[0]["time"])
        if early:
            # Deduplicate: keep early bars strictly before the first Yahoo bar.
            cutoff = bars[0]["time"]
            early = [b for b in early if b["time"] < cutoff]
            bars = early + bars

    async with _lock:
        if bars:
            _monthly_cache.update(bars=bars, years=want, ts=time.monotonic())
    return bars


# ── Full-history BTC candles for every timeframe (the cycle chart's data layer)

_TF_GENESIS_LIMIT: Dict[str, int] = {
    "MN1": 300,     # 195 months from 2010
    "W1": 1200,     # 842 weeks
    "D1": 7000,     # 5,900 days
    "H4": 40000,    # 35k 4h
    "H1": 150000,   # 141k hours
}
_TF_API_TIMELINE_MAP: Dict[str, str] = {"MN1": "MN1", "W1": "W1", "D1": "D1", "H4": "H4", "H1": "H1"}


async def _fetch_bitget_paginated(timeframe: str, limit: int) -> List[Dict[str, Any]]:
    """Bitget OHLCV paginated back to earliest available (used for H1/H4 genesis).

    Bitget's CCXT fetch_ohlcv caps at 1500 per call; H1 genesis needs ~95 pages.
    Returns [{time: sec, open, high, low, close, volume}] sorted oldest-first.
    On any failure returns whatever was collected so the chart can still draw.
    Pages backward from now to handle pre-exchange gap: early since=2010 returns [] on Bitget.
    """
    import asyncio as _asyncio
    # Map display TF to CCXT TF
    ccxt_tf = "1h" if timeframe == "H1" else "4h" if timeframe == "H4" else timeframe.lower()
    genesis_ms = int(datetime(2010, 7, 17, tzinfo=timezone.utc).timestamp() * 1000)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    tf_ms = {"H1": 3600_000, "H4": 14_400_000}.get(timeframe, 3600_000)
    collected: List[Dict[str, Any]] = []
    seen_start: set[int] = set()
    # Page backward from now: start at most-recent window, then step back
    # This avoids the "since=2010 returns [] and we break" trap.
    since = max(genesis_ms, now_ms - 1500 * tf_ms)
    pages = 0
    max_pages = max(1, (limit // 1500) + 5) if limit else 120
    # Track earliest timestamp seen to know when we reached genesis
    earliest_seen_ms: int | None = None
    # Reuse cached public instance pattern to avoid repeated load-markets
    try:
        from app.api.exchanges import get_exchange_for_public_data
        from app.exchanges.manager import SupportedExchange
        # Try credentialed connector first
        from app.exchanges.manager import exchange_manager
        connector = exchange_manager.get_exchange(SupportedExchange.BITGET)
        if connector is not None:
            exchange = connector.exchange
        else:
            exchange = await get_exchange_for_public_data(SupportedExchange.BITGET)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[Cycle] Bitget exchange unavailable for paginated fetch: {exc}")
        return []
    # Page backward: each fetch pulls a ~1500-bar window ending near "since+window".
    # Starting from the most-recent window and stepping the window start back
    # collects the full Bitget history without needing to know its earliest date.
    while pages < max_pages and since >= genesis_ms:
        try:
            rows = await _asyncio.wait_for(
                exchange.fetch_ohlcv(symbol="BTC/USDT:USDT", timeframe=ccxt_tf, since=since, limit=1500),
                timeout=22,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[Cycle] Bitget paginated H1/H4 page {pages} failed: {exc}")
            # Step further back and retry — don't abort whole history on one blip
            since -= 1500 * tf_ms
            pages += 1
            if pages % 5 == 0:
                await _asyncio.sleep(0.2)
            continue
        if not rows:
            # Empty window — Bitget has no data in this slice (pre-listing gap).
            # Step further back to look for earlier history.
            since -= 1500 * tf_ms
            pages += 1
            if since < genesis_ms:
                break
            if pages % 10 == 0:
                await _asyncio.sleep(0.15)
            continue
        # rows are [ms, o,h,l,c,v]
        for r in rows:
            try:
                ts_ms = int(r[0])
                if ts_ms in seen_start:
                    continue
                seen_start.add(ts_ms)
                collected.append({
                    "time": ts_ms // 1000,
                    "open": float(r[1]),
                    "high": float(r[2]),
                    "low": float(r[3]),
                    "close": float(r[4]),
                    "volume": float(r[5]) if len(r) > 5 and r[5] is not None else 0.0,
                })
                if earliest_seen_ms is None or ts_ms < earliest_seen_ms:
                    earliest_seen_ms = ts_ms
            except (TypeError, ValueError, IndexError):
                continue
        pages += 1
        # Step the window start back before the earliest candle in this batch
        earliest_in_batch = min(int(r[0]) for r in rows)
        since = earliest_in_batch - 1500 * tf_ms - tf_ms
        if earliest_seen_ms is not None and earliest_seen_ms <= genesis_ms + tf_ms:
            break
        # Be nice to rate limits
        if pages % 5 == 0:
            await _asyncio.sleep(0.15)
        if len(collected) >= limit:
            break
    collected.sort(key=lambda x: x["time"])
    # Trim to limit most recent if oversized
    if len(collected) > limit:
        collected = collected[-limit:]
    logger.debug(f"[Cycle] Bitget paginated {timeframe}: {len(collected)} candles in {pages} pages")
    return collected


async def resolve_btc_candles(
    timeframe: str = "D1",
    limit: Optional[int] = None,
    force: bool = False,
) -> List[Dict[str, Any]]:
    """Full-history BTC candles for the cycle chart — genesis-aware.

    * MN1/W1/D1 → Yahoo `max` + CoinMetrics daily backfill → true 2010 genesis.
    * H4/H1 → Bitget paginated (real) + synthetic daily→hourly for 2010→Bitget-start gap → continuous 2010 line.
    Cached per timeframe (TTL = CYCLE_CACHE_TTL_SECONDS). limit=0 or None means "full genesis".
    """
    tf = (timeframe or "D1").upper().strip()
    # Normalize aliases
    if tf in ("1M", "MN", "MONTH"): tf = "MN1"
    if tf not in _TF_GENESIS_LIMIT:
        tf = "D1"
    genesis_limit = _TF_GENESIS_LIMIT[tf]
    want = genesis_limit if not limit or limit <= 0 else min(int(limit), max(genesis_limit, int(limit)))
    # Allow callers to ask larger than genesis (e.g., H1 150k) — cap at 150k
    want = max(1, min(150000, want))
    cfg = _settings()
    ttl = max(60, int(getattr(cfg, "CYCLE_CACHE_TTL_SECONDS", 900) or 900))
    async with _lock:
        hit = _btc_tf_cache.get(tf)
        if not force and hit and hit.get("bars") and (time.monotonic() - hit["ts"]) < ttl and len(hit["bars"]) >= min(want, 100):
            cached = hit["bars"]
            # Return slice of cached if caller asked smaller window
            return cached[-want:] if len(cached) > want else cached
    # ── MN1 ──
    if tf == "MN1":
        bars = await resolve_monthly_bars(years=20, force=force)
        # resolve_monthly_bars already returns ~195 monthly from 2010
        bars = bars[-want:] if len(bars) > want else bars
        async with _lock:
            _btc_tf_cache[tf] = {"bars": bars, "ts": time.monotonic()}
        return bars
    # ── W1 / D1 via Yahoo period + CoinMetrics daily backfill ──
    # Yahoo's range=max for 1d returns monthly (broken) — use period API for true daily/weekly.
    if tf in ("D1", "W1"):
        try:
            bars = await _fetch_yahoo_btc_full(tf, want)
            # Fallback to legacy provider if period fetch empty (rate-limit etc.)
            if not bars:
                from app.exchanges import yahoo_provider as _yp_fb
                bars = await _yp_fb.fetch_candles(_BTC_SYMBOL, tf, limit=want) or []
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[Cycle] BTC {tf} Yahoo period unavailable: {exc}")
            bars = []
        # Prepend CoinMetrics daily gap. Yahoo BTC-D1/W1 starts 2014-09, so everything before needs backfill.
        if bars:
            try:
                daily_gap = await _fetch_early_btc_daily(bars[0]["time"])
                if daily_gap:
                    if tf == "D1":
                        cutoff = bars[0]["time"]
                        gap = [b for b in daily_gap if b["time"] < cutoff]
                        bars = gap + bars
                    elif tf == "W1":
                        # Aggregate daily gap → weekly, then prepend
                        cutoff = bars[0]["time"]
                        daily_cut = [b for b in daily_gap if b["time"] < cutoff]
                        weekly_gap = _daily_to_weekly(daily_cut)
                        weekly_gap = [b for b in weekly_gap if b["time"] < cutoff]
                        bars = weekly_gap + bars
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[Cycle] BTC {tf} backfill failed: {exc}")
        else:
            # Yahoo empty — serve pure daily backfill as fallback (at least 2010-2014)
            try:
                # Use far-future cutoff to get all daily
                daily_all = await _fetch_early_btc_daily(int(datetime.now(timezone.utc).timestamp()) + 86400)
                if daily_all:
                    if tf == "D1":
                        bars = daily_all[-want:]
                    else:
                        bars = _daily_to_weekly(daily_all)[-want:]
            except Exception:  # noqa: BLE001
                bars = []
        # Deduplicate by time (CoinMetrics vs Yahoo overlap at boundary)
        seen: Dict[int, Dict[str, Any]] = {}
        for b in bars:
            seen[int(b["time"])] = b
        bars = [seen[k] for k in sorted(seen.keys())]
        bars = bars[-want:] if len(bars) > want else bars
        async with _lock:
            _btc_tf_cache[tf] = {"bars": bars, "ts": time.monotonic()}
        return bars
    # ── H1 / H4 via Bitget paginated + synthetic gap ──
    # Fetch real Bitget history first
    bitget_bars = await _fetch_bitget_paginated(tf, limit=want)
    # Determine gap between genesis (2010-07-17) and first Bitget bar
    genesis_ts = int(datetime(2010, 7, 17, tzinfo=timezone.utc).timestamp())
    gap_end_ts = bitget_bars[0]["time"] if bitget_bars else int(datetime.now(timezone.utc).timestamp())
    synthetic: List[Dict[str, Any]] = []
    if gap_end_ts > genesis_ts + 3600:
        try:
            # Fetch daily up to gap_end, synthesize hourly/4h
            daily_gap = await _fetch_early_btc_daily(gap_end_ts)
            # Also need Yahoo daily for 2014- gap_end if gap extends beyond CoinMetrics cutoff
            if daily_gap and len(daily_gap) < (gap_end_ts - genesis_ts) // 86400 - 5:
                # Top up daily gap from Yahoo D1 if needed (covers 2014- gap_end) — use period fetch (range=max is broken)
                try:
                    y_daily = await _fetch_yahoo_btc_full("D1", 7000)
                    if y_daily:
                        # Merge daily_gap (CoinMetrics) + y_daily filtered to gap window
                        y_filtered = [b for b in y_daily if genesis_ts <= b["time"] < gap_end_ts]
                        # Deduplicate
                        merged: Dict[int, Dict[str, Any]] = {b["time"]: b for b in daily_gap}
                        for b in y_filtered:
                            merged.setdefault(int(b["time"]), b)
                        daily_gap = [merged[k] for k in sorted(merged.keys())]
                except Exception:  # noqa: BLE001
                    pass
            interval = 1 if tf == "H1" else 4
            synthetic = _synthesize_hourly_from_daily(daily_gap, interval_hours=interval)
            synthetic = [b for b in synthetic if b["time"] < gap_end_ts]
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[Cycle] BTC {tf} synthetic gap failed: {exc}")
            synthetic = []
    combined = synthetic + bitget_bars
    # Deduplicate again and sort
    seen2: Dict[int, Dict[str, Any]] = {}
    for b in combined:
        seen2[int(b["time"])] = b
    combined = [seen2[k] for k in sorted(seen2.keys())]
    combined = combined[-want:] if len(combined) > want else combined
    async with _lock:
        _btc_tf_cache[tf] = {"bars": combined, "ts": time.monotonic()}
    return combined


async def cached_bars() -> List[Dict[str, Any]]:
    """The cached BTC dailies, resolving them first if needed."""
    async with _lock:
        if _cached["bars"]:
            return _cached["bars"]
    await resolve_cycle_snapshot()
    async with _lock:
        return _cached["bars"] or []


def _settings() -> Any:
    from app.core.config import settings

    return settings


async def _fetch_btc_bars() -> List[Dict[str, Any]]:
    """Ten years of BTC dailies in one cached Yahoo call."""
    from app.exchanges import yahoo_provider

    try:
        return await yahoo_provider.fetch_candles(_BTC_SYMBOL, "D1", limit=_BARS_LIMIT)
    except Exception as exc:  # noqa: BLE001 — the calendar must never break a trade
        logger.debug(f"[Cycle] BTC dailies unavailable: {exc}")
        return []


async def resolve_cycle_snapshot(
    force: bool = False,
    *,
    anchors_override: Any = None,
    bull_days: Optional[int] = None,
    bear_days: Optional[int] = None,
) -> Optional[CycleSnapshot]:
    """The live snapshot, cached. Failure returns None, not an opinion."""
    cfg = _settings()
    ttl = max(60, int(getattr(cfg, "CYCLE_CACHE_TTL_SECONDS", 900) or 900))
    async with _lock:
        now = time.monotonic()
        if not force and _cached["snap"] is not None and now - _cached["ts"] < ttl:
            return _cached["snap"]

        anchors = anchors_override
        if anchors is None:
            anchors = await _anchors_from_settings()
        bars = await _fetch_btc_bars()
        snap = build_cycle_snapshot(
            anchors or DEFAULT_ANCHORS,
            bars=bars or None,
            bull_days=int(bull_days or getattr(cfg, "CYCLE_BULL_DAYS", BULL_DAYS) or BULL_DAYS),
            bear_days=int(bear_days or getattr(cfg, "CYCLE_BEAR_DAYS", BEAR_DAYS) or BEAR_DAYS),
        )
        if not snap.ok:
            return None
        _cached["snap"] = snap
        _cached["bars"] = bars
        _cached["ts"] = now
        return snap


async def _anchors_from_settings() -> Optional[Any]:
    """Persisted anchors from room settings; None falls back to defaults."""
    try:
        from app.agents.execution import get_settings
        from app.core.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            s = await get_settings(db)
            raw = getattr(s, "cycle_anchors", None)
            return raw if raw else None
    except Exception as exc:  # noqa: BLE001 — defaults are fine when the DB sleeps
        logger.debug(f"[Cycle] anchors from settings unavailable: {exc}")
        return None


async def resolve_cycle_bias(symbol: str) -> CycleBias:
    """Bias for one symbol; the signals path calls this beside macro bias."""
    snap = await resolve_cycle_snapshot()
    return cycle_bias(symbol, snap)


async def resolve_cycle_calendar(
    year: Optional[int] = None,
    month: Optional[int] = None,
    *,
    anchors_override: Any = None,
) -> Dict[str, Any]:
    """Everything the cycle page's calendar needs for one month."""
    cfg = _settings()
    now = datetime.now(timezone.utc).date()
    y, m = year or now.year, month or now.month

    async with _lock:
        bars = _cached["bars"]
    if not bars:
        await resolve_cycle_snapshot()
        bars = _cached["bars"] or []

    anchors = anchors_override if anchors_override is not None else await _anchors_from_settings()
    anchors = anchors or DEFAULT_ANCHORS
    bottoms = parse_anchors(anchors)
    grid = build_cycle_calendar(
        anchors, year=y, month=m, bull_days=int(getattr(cfg, "CYCLE_BULL_DAYS", BULL_DAYS)),
        bear_days=int(getattr(cfg, "CYCLE_BEAR_DAYS", BEAR_DAYS)),
    )
    today_row = next((g for g in grid if g["is_today"]), None)
    expectation = (
        day_expectation(
            bottoms, bars, today_row["day_of_cycle"],
            bull_days=int(getattr(cfg, "CYCLE_BULL_DAYS", BULL_DAYS)),
            bear_days=int(getattr(cfg, "CYCLE_BEAR_DAYS", BEAR_DAYS)),
        )
        if today_row and bars
        else {"offset": today_row["day_of_cycle"] if today_row else None, "samples": 0}
    )
    return {
        "year": y,
        "month": m,
        "days": grid,
        "today_expectation": expectation,
        "halvings": list(HALVINGS),
    }
