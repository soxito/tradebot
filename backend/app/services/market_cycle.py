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
    history_cycles: int = 3,
) -> List[CycleWindow]:
    """Green/red boxes for chart overlays: the last few cycles + the live one.

    Past boxes are drawn from anchor to anchor (history, not projection); the
    live cycle's current phase extends to its projected turn and is flagged so
    the chart can dim it.
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
    # The live box extends past the last anchor's bear projection so the chart
    # shows where the current phase is heading even before the next bottom is
    # anchored.
    if bottoms:
        last = bottoms[-1]
        proj_top = last + timedelta(days=bull_days)
        proj_bottom = proj_top + timedelta(days=bear_days)
        if day >= last:
            if day < proj_top:
                out.append(CycleWindow(
                    start=last.isoformat(), end=proj_top.isoformat(),
                    phase="bull", projected=True,
                ))
            else:
                out.append(CycleWindow(
                    start=proj_top.isoformat(), end=proj_bottom.isoformat(),
                    phase="bear", projected=True,
                ))
    return out[-history_cycles * 2 + 2:]


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


def reset_cache() -> None:
    """Drop the cached snapshot — anchors or phase lengths just changed."""
    _cached["snap"] = None
    _cached["ts"] = 0.0


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
