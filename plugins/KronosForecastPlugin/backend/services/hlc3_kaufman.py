"""
HLC3/Kaufman Adaptive Moving Average indicator.

Python port of the PineScript v2 "HLC3/Kaufman Strategy" by Marco.

Two lines are produced on the chart:
  bfma (cyan)   — EMA(1) of the daily HLC3 shifted by 1 day (slow HTF reference)
  bsma (orange) — EMA(sloma=20) of the KAMA seeded from hlc3 (fast adaptive)

Signal:
  BUY  — bfma crosses above bsma
  SELL — bfma crosses below bsma

Toggle globally with KRONOS_HLC3_KAUFMAN_ENABLED=false. Standalone: reuses
nothing from core, imports only numpy/pandas already in the plugin's deps.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd
from loguru import logger


_KAMA_LENGTH: int = 5
_KAMA_FAST_END: float = 2.5
_KAMA_SLOW_END: float = 20.0
_SLOW_MA_PERIOD: int = 20
_DAILY_SHIFT: int = 1

_TF_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200,
    "1d": 86400, "3d": 259200, "1w": 604800,
}


def is_enabled() -> bool:
    """True unless KRONOS_HLC3_KAUFMAN_ENABLED is set to a falsey value."""
    return os.getenv("KRONOS_HLC3_KAUFMAN_ENABLED", "true").strip().lower() not in (
        "0", "false", "no", "off",
    )


@dataclass
class HLC3KaufmanResult:
    """Computed HLC3/Kaufman output for one symbol/timeframe."""
    enabled: bool = True
    signal: str = "neutral"            # "buy" | "sell" | "neutral"
    bfma_current: Optional[float] = None
    bsma_current: Optional[float] = None
    bfma_prev: Optional[float] = None
    bsma_prev: Optional[float] = None
    crossover: bool = False            # True on the BUY signal bar
    crossunder: bool = False           # True on the SELL signal bar
    bars_since_cross: Optional[int] = None
    bfma_series: List[Tuple[int, float]] = field(default_factory=list)
    bsma_series: List[Tuple[int, float]] = field(default_factory=list)
    summary: str = ""
    error: Optional[str] = None


def _kama(
    hlc3: pd.Series,
    length: int = _KAMA_LENGTH,
    fast_end: float = _KAMA_FAST_END,
    slow_end: float = _KAMA_SLOW_END,
) -> pd.Series:
    """Kaufman Adaptive Moving Average, matching the PineScript recursion."""
    nfastend = 2.0 / (fast_end + 1.0)
    nslowend = 2.0 / (slow_end + 1.0)

    vals = hlc3.to_numpy(dtype=float)
    n = len(vals)

    xvnoise = np.abs(np.diff(vals, prepend=vals[0]))
    nsignal = np.abs(vals - np.concatenate([np.full(length, vals[0]), vals[:-length]]))
    nnoise = np.convolve(xvnoise, np.ones(length), mode="full")[:n]
    with np.errstate(invalid="ignore", divide="ignore"):
        nefratio = np.where(nnoise != 0, nsignal / nnoise, 0.0)
    nefratio = np.clip(nefratio, 0.0, 1.0)
    nsmooth = np.power(nefratio * (nfastend - nslowend) + nslowend, 2)

    kama = np.empty(n)
    kama[0] = vals[0]
    for i in range(1, n):
        kama[i] = kama[i - 1] + nsmooth[i] * (vals[i] - kama[i - 1])
    return pd.Series(kama, index=hlc3.index)


def _ema(series: pd.Series, period: int) -> pd.Series:
    if period <= 1:
        return series
    return series.ewm(span=period, adjust=False, min_periods=1).mean()


def _resample_daily_hlc3(rows: List[list], shift: int = _DAILY_SHIFT) -> pd.Series:
    """Daily HLC3 shifted by `shift` days, forward-filled to the bar timestamps."""
    ts_ms = np.array([int(r[0]) for r in rows])
    highs = np.array([float(r[2]) for r in rows])
    lows = np.array([float(r[3]) for r in rows])
    closes = np.array([float(r[4]) for r in rows])
    hlc3 = (highs + lows + closes) / 3.0

    timestamps = pd.to_datetime(ts_ms, unit="ms", utc=True)
    df = pd.DataFrame({"hlc3": hlc3}, index=timestamps)

    daily = df["hlc3"].resample("1D").last().dropna()
    if len(daily) == 0:
        return pd.Series(np.full(len(rows), np.nan), index=timestamps)
    daily_shifted = daily.shift(shift)
    return daily_shifted.reindex(timestamps, method="ffill")


def compute_hlc3_kaufman(
    rows: List[list],
    timeframe: str = "1h",
    *,
    length: int = _KAMA_LENGTH,
    fast_end: float = _KAMA_FAST_END,
    slow_end: float = _KAMA_SLOW_END,
    slow_ma_period: int = _SLOW_MA_PERIOD,
    daily_shift: int = _DAILY_SHIFT,
    max_series_points: int = 300,
) -> HLC3KaufmanResult:
    """Compute the HLC3/Kaufman indicator from ccxt-shaped OHLCV rows.

    ``rows`` are ``[ts_ms, open, high, low, close, volume]`` in chronological
    order. Returns a result whose ``error`` is set (and series empty) when there
    are too few bars, so callers can attach it unconditionally.
    """
    if not is_enabled():
        return HLC3KaufmanResult(enabled=False, summary="HLC3/Kaufman disabled")

    need = max(length + 5, slow_ma_period + 5)
    if not rows or len(rows) < need:
        return HLC3KaufmanResult(
            enabled=True, summary="Not enough bars for HLC3/Kaufman",
            error=f"Need ≥{need} bars, got {len(rows) if rows else 0}",
        )

    try:
        ts_ms = np.array([int(r[0]) for r in rows])
        highs = np.array([float(r[2]) for r in rows])
        lows = np.array([float(r[3]) for r in rows])
        closes = np.array([float(r[4]) for r in rows])
        hlc3_arr = (highs + lows + closes) / 3.0
        timestamps = pd.to_datetime(ts_ms, unit="ms", utc=True)
        hlc3_series = pd.Series(hlc3_arr, index=timestamps, dtype=float)

        kama_series = _kama(hlc3_series, length=length, fast_end=fast_end, slow_end=slow_end)
        bsma_series = _ema(kama_series, slow_ma_period)

        bar_s = _TF_SECONDS.get(timeframe.lower(), 3600)
        if bar_s >= 86400:
            bfma_series = _ema(hlc3_series.shift(daily_shift), 1)
        else:
            daily_fwd = _resample_daily_hlc3(rows, shift=daily_shift)
            bfma_series = _ema(daily_fwd, 1)

        bfma_arr = bfma_series.to_numpy(dtype=float)
        bsma_arr = bsma_series.to_numpy(dtype=float)

        def _valid(i: int) -> bool:
            return not np.isnan(bfma_arr[i]) and not np.isnan(bsma_arr[i])

        crossover = bool(
            len(bfma_arr) >= 2 and _valid(-1) and _valid(-2)
            and bfma_arr[-1] > bsma_arr[-1] and bfma_arr[-2] <= bsma_arr[-2]
        )
        crossunder = bool(
            len(bfma_arr) >= 2 and _valid(-1) and _valid(-2)
            and bfma_arr[-1] < bsma_arr[-1] and bfma_arr[-2] >= bsma_arr[-2]
        )

        bfma_cur = float(bfma_arr[-1]) if not np.isnan(bfma_arr[-1]) else None
        bsma_cur = float(bsma_arr[-1]) if not np.isnan(bsma_arr[-1]) else None
        bfma_prev = float(bfma_arr[-2]) if len(bfma_arr) >= 2 and not np.isnan(bfma_arr[-2]) else None
        bsma_prev = float(bsma_arr[-2]) if len(bsma_arr) >= 2 and not np.isnan(bsma_arr[-2]) else None

        if crossover:
            signal = "buy"
        elif crossunder:
            signal = "sell"
        elif bfma_cur is not None and bsma_cur is not None:
            signal = "buy" if bfma_cur > bsma_cur else "sell"
        else:
            signal = "neutral"

        bars_since = None
        for i in range(len(bfma_arr) - 2, max(0, len(bfma_arr) - 100) - 1, -1):
            j = i - 1
            if j < 0 or np.isnan(bfma_arr[i]) or np.isnan(bsma_arr[i]) \
                    or np.isnan(bfma_arr[j]) or np.isnan(bsma_arr[j]):
                continue
            crossed = (
                (bfma_arr[i] > bsma_arr[i] and bfma_arr[j] <= bsma_arr[j]) or
                (bfma_arr[i] < bsma_arr[i] and bfma_arr[j] >= bsma_arr[j])
            )
            if crossed:
                bars_since = len(bfma_arr) - 1 - i
                break

        unix_s = (ts_ms / 1000).astype(int)
        n_pts = len(unix_s)
        start = max(0, n_pts - max_series_points)

        def _to_series(arr: np.ndarray) -> List[Tuple[int, float]]:
            out: List[Tuple[int, float]] = []
            for i in range(start, n_pts):
                v = float(arr[i])
                if not np.isnan(v):
                    out.append((int(unix_s[i]), round(v, 8)))
            return out

        sig_icon = "🟢 BUY" if signal == "buy" else ("🔴 SELL" if signal == "sell" else "⚪ NEUTRAL")
        bfma_txt = f"{bfma_cur:.6g}" if bfma_cur is not None else "n/a"
        bsma_txt = f"{bsma_cur:.6g}" if bsma_cur is not None else "n/a"
        cross_txt = " (cross now!)" if (crossover or crossunder) else (
            f" ({bars_since} bars ago)" if bars_since is not None else ""
        )
        summary = (
            f"HLC3/Kaufman {sig_icon}{cross_txt} — daily HTF {bfma_txt} vs KAMA EMA {bsma_txt}"
        )

        return HLC3KaufmanResult(
            enabled=True, signal=signal,
            bfma_current=bfma_cur, bsma_current=bsma_cur,
            bfma_prev=bfma_prev, bsma_prev=bsma_prev,
            crossover=crossover, crossunder=crossunder, bars_since_cross=bars_since,
            bfma_series=_to_series(bfma_arr), bsma_series=_to_series(bsma_arr),
            summary=summary,
        )
    except Exception as exc:  # noqa: BLE001 — an indicator must never break the forecast
        logger.warning(f"[HLC3/Kaufman] compute failed: {exc}")
        return HLC3KaufmanResult(
            enabled=True, summary="HLC3/Kaufman computation error", error=str(exc),
        )
