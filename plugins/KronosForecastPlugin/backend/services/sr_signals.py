"""
LuxAlgo "Support & Resistance Signals MTF" — Python port.

Replicates the core logic of the PineScript v5 indicator by LuxAlgo:
pivot-based S/R zones that flip on a confirmed breakout, plus the four signal
types the indicator draws — Breakout (B), Test (T), Retest (R) and Rejection —
and manipulation (liquidity-sweep) zones.

Reuses ``pivot_highs`` / ``pivot_lows`` / ``atr`` from core ``app.signals.technical``
(read-only) rather than reimplementing them. Toggle globally with
KRONOS_SR_SIGNALS_ENABLED=false.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
from loguru import logger


_LENGTH: int = 15
_SR_MARGIN: float = 2.0
_MANIP_MARGIN: float = 1.3
_MAX_ZONES: int = 2


def is_enabled() -> bool:
    """True unless KRONOS_SR_SIGNALS_ENABLED is set to a falsey value."""
    return os.getenv("KRONOS_SR_SIGNALS_ENABLED", "true").strip().lower() not in (
        "0", "false", "no", "off",
    )


@dataclass
class SRZone:
    level: float
    zone_top: float
    zone_bottom: float
    kind: str                 # "resistance" | "support"
    formed_unix: int
    broken: bool = False
    tested: bool = False
    retested: bool = False
    manipulated: bool = False


@dataclass
class SRSignalEvent:
    unix_time: int
    event_type: str           # see module docstring for the vocabulary
    price: float
    level_price: float
    zone_kind: str


@dataclass
class SRSignalsResult:
    enabled: bool = True
    support_zones: List[SRZone] = field(default_factory=list)
    resistance_zones: List[SRZone] = field(default_factory=list)
    recent_events: List[SRSignalEvent] = field(default_factory=list)
    current_event: Optional[SRSignalEvent] = None
    nearest_support: Optional[float] = None
    nearest_resistance: Optional[float] = None
    summary: str = ""
    error: Optional[str] = None


def compute_sr_signals(
    rows: List[list],
    *,
    length: int = _LENGTH,
    sr_margin: float = _SR_MARGIN,
    manipulation_margin: float = _MANIP_MARGIN,
    max_zones: int = _MAX_ZONES,
) -> SRSignalsResult:
    """Compute S/R zones + signal events from ccxt-shaped OHLCV rows.

    ``rows`` are ``[ts_ms, open, high, low, close, volume]`` in chronological
    order. Returns a result whose ``error`` is set when there is too little data.
    """
    if not is_enabled():
        return SRSignalsResult(enabled=False, summary="S&R signals disabled")

    need = length * 2 + 20
    if not rows or len(rows) < need:
        return SRSignalsResult(
            enabled=True, summary="Not enough bars for S&R signals",
            error=f"Need ≥{need} bars, got {len(rows) if rows else 0}",
        )

    try:
        from app.signals.technical import ohlcv_to_dataframe, pivot_highs, pivot_lows, atr

        df = ohlcv_to_dataframe(rows)
        n = len(df)
        opens = df["open"].to_numpy(dtype=float)
        highs = df["high"].to_numpy(dtype=float)
        lows = df["low"].to_numpy(dtype=float)
        closes = df["close"].to_numpy(dtype=float)
        vols = df["volume"].to_numpy(dtype=float)
        unix = (df["timestamp"].astype("int64").to_numpy() // 10**9).astype(int)

        atr_arr = atr(df, 17).to_numpy(dtype=float)
        vol_sma = df["volume"].rolling(17, min_periods=1).mean().to_numpy(dtype=float)

        ph = pivot_highs(df["high"], length, length).to_numpy(dtype=float)
        pl = pivot_lows(df["low"], length, length).to_numpy(dtype=float)

        # Zone half-width as a fraction of price, from the rolling range — this is
        # the LuxAlgo (pHST-pLST)/pHST * 0.17 * srMR width factor.
        roll_hi = df["high"].rolling(length, min_periods=1).max().to_numpy(dtype=float)
        roll_lo = df["low"].rolling(length, min_periods=1).min().to_numpy(dtype=float)

        res_zones: List[SRZone] = []   # newest first
        sup_zones: List[SRZone] = []
        events: List[SRSignalEvent] = []

        def _margin_frac(i: int) -> float:
            hi, lo = roll_hi[i], roll_lo[i]
            if hi <= 0:
                return 0.0
            return max(0.0, (hi - lo) / hi) * 0.17 * sr_margin

        for i in range(n):
            # ── Register a new pivot as a zone (deduped against the last one) ──
            if not np.isnan(ph[i]):
                price = float(ph[i])
                mf = _margin_frac(i)
                zone_bottom = price * (1 - mf)
                if not res_zones or abs(res_zones[0].level - price) / max(price, 1e-9) > mf:
                    res_zones.insert(0, SRZone(
                        level=price, zone_top=price, zone_bottom=zone_bottom,
                        kind="resistance", formed_unix=int(unix[i]),
                    ))
                    res_zones[:] = res_zones[:max_zones]
            if not np.isnan(pl[i]):
                price = float(pl[i])
                mf = _margin_frac(i)
                zone_top = price * (1 + mf)
                if not sup_zones or abs(sup_zones[0].level - price) / max(price, 1e-9) > mf:
                    sup_zones.insert(0, SRZone(
                        level=price, zone_top=zone_top, zone_bottom=price,
                        kind="support", formed_unix=int(unix[i]),
                    ))
                    sup_zones[:] = sup_zones[:max_zones]

            if i == 0:
                continue

            c, pc = closes[i], closes[i - 1]
            h, lw, o = highs[i], lows[i], opens[i]
            mf = _margin_frac(i)
            t = int(unix[i])

            # ── Resistance zone interactions ────────────────────────────────
            for rz in list(res_zones):
                if rz.broken:
                    continue
                brk = rz.zone_top * (1 + mf)
                if pc > brk and c > brk:
                    rz.broken = True
                    events.append(SRSignalEvent(t, "breakout_bull", c, rz.level, "resistance"))
                    # Flip to support (LuxAlgo behaviour).
                    sup_zones.insert(0, SRZone(
                        level=rz.zone_bottom, zone_top=rz.zone_top,
                        zone_bottom=rz.zone_bottom, kind="support", formed_unix=t,
                    ))
                    sup_zones[:] = sup_zones[:max_zones]
                elif h > rz.zone_bottom and c < rz.zone_top and not rz.tested:
                    rz.tested = True
                    events.append(SRSignalEvent(t, "test_bear", c, rz.level, "resistance"))
                elif (o > rz.zone_top and c < rz.zone_bottom and not rz.retested):
                    rz.retested = True
                    events.append(SRSignalEvent(t, "retest_bear", c, rz.level, "resistance"))
                # Manipulation: wick above the zone that closes back at/under it.
                if h > rz.zone_top and c <= rz.zone_top * (1 + mf * manipulation_margin) \
                        and not rz.manipulated and h - max(o, c) >= 1.618 * (atr_arr[i] or 0):
                    rz.manipulated = True
                    events.append(SRSignalEvent(t, "sweep_bear", h, rz.level, "resistance"))

            # ── Support zone interactions ───────────────────────────────────
            for sz in list(sup_zones):
                if sz.broken:
                    continue
                brk = sz.zone_bottom * (1 - mf)
                if pc < brk and c < brk:
                    sz.broken = True
                    events.append(SRSignalEvent(t, "breakout_bear", c, sz.level, "support"))
                    res_zones.insert(0, SRZone(
                        level=sz.zone_top, zone_top=sz.zone_top,
                        zone_bottom=sz.zone_bottom, kind="resistance", formed_unix=t,
                    ))
                    res_zones[:] = res_zones[:max_zones]
                elif lw < sz.zone_top and c > sz.zone_bottom and not sz.tested:
                    sz.tested = True
                    events.append(SRSignalEvent(t, "test_bull", c, sz.level, "support"))
                elif (o < sz.zone_bottom and c > sz.zone_top and not sz.retested):
                    sz.retested = True
                    events.append(SRSignalEvent(t, "retest_bull", c, sz.level, "support"))
                if lw < sz.zone_bottom and c >= sz.zone_bottom * (1 - mf * manipulation_margin) \
                        and not sz.manipulated and min(o, c) - lw >= 1.618 * (atr_arr[i] or 0):
                    sz.manipulated = True
                    events.append(SRSignalEvent(t, "sweep_bull", lw, sz.level, "support"))

            # ── Rejection: long shadow on high volume (LuxAlgo isLLS/isLUS) ──
            body_hi = max(o, c)
            body_lo = min(o, c)
            a = atr_arr[i] or 0.0
            high_vol = vol_sma[i] > 0 and vols[i] >= 1.618 * vol_sma[i]
            if high_vol and a > 0:
                if abs(lw - body_lo) >= 1.618 * a:
                    events.append(SRSignalEvent(t, "rejection_bull", lw, lw, "support"))
                if abs(h - body_hi) >= 1.618 * a:
                    events.append(SRSignalEvent(t, "rejection_bear", h, h, "resistance"))

        price = float(closes[-1])
        active_sup = [z for z in sup_zones if not z.broken]
        active_res = [z for z in res_zones if not z.broken]
        below = sorted((z for z in active_sup if z.level <= price), key=lambda z: price - z.level)
        above = sorted((z for z in active_res if z.level >= price), key=lambda z: z.level - price)
        nearest_support = below[0].level if below else None
        nearest_resistance = above[0].level if above else None

        last_t = int(unix[-1])
        current_event = next((e for e in reversed(events) if e.unix_time == last_t), None)

        parts = []
        if nearest_resistance is not None:
            parts.append(f"R {nearest_resistance:.6g}")
        if nearest_support is not None:
            parts.append(f"S {nearest_support:.6g}")
        if current_event:
            parts.append(current_event.event_type.replace("_", " "))
        summary = "S&R — " + (" · ".join(parts) if parts else "no active zones")

        return SRSignalsResult(
            enabled=True,
            support_zones=active_sup[:max_zones],
            resistance_zones=active_res[:max_zones],
            recent_events=events[-10:],
            current_event=current_event,
            nearest_support=nearest_support,
            nearest_resistance=nearest_resistance,
            summary=summary,
        )
    except Exception as exc:  # noqa: BLE001 — never break the forecast on an indicator
        logger.warning(f"[S&R] compute failed: {exc}")
        return SRSignalsResult(enabled=True, summary="S&R computation error", error=str(exc))
