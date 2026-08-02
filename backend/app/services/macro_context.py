"""Macro context — the dollar and the fear gauge, resolved for one instrument.

A setup does not trade in isolation. A long on gold into a dollar that has been
bid for a week is a different trade from the same chart pattern with the dollar
falling, and the difference is not visible in the candles of the instrument
itself. This module reads two series — the US Dollar Index and the VIX — and
turns them into a single signed number a scoring model can weigh, plus the
sentences that say why.

Three rules follow from the fact that this is *context*, not a signal:

1. **It never blocks a trade.** The number it produces moves confidence; it does
   not veto. A second hard gate on top of the volume gate is how a signal
   pipeline goes quiet.
2. **It never applies where it does not belong.** The dollar says nothing about
   EURGBP, and pretending otherwise is worse than silence — so the bias reports
   itself as inapplicable and the caller redistributes its weight rather than
   scoring a zero.
3. **A failed fetch is no opinion, not a bearish one.** Every failure path
   returns ``applicable=False`` with ``normalized=0.0``. The most likely way for
   this module to make signals *worse* is for an outage to read as a headwind.

Shape follows ``KronosForecastPlugin.volume_context``: named constants carrying
the reason for their value, a pure builder that is unit-testable with synthetic
rows, an async resolver that owns the I/O, and rendering helpers that are safe to
call at any status.
"""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Sequence

from loguru import logger

from app.exchanges import yahoo_provider

MacroStatusT = Literal["OK", "UNAVAILABLE", "STALE"]
MacroRegimeT = Literal["RISK_ON", "RISK_OFF", "NEUTRAL", "UNKNOWN"]
UsdLegT = Literal["quote", "base", "none"]

#: The two series. Both are cash indices carrying no volume, which is why they
#: are read here and never routed through a volume gate.
DXY_SYMBOL = "DXY"
VIX_SYMBOL = "VIX"

#: Daily bars. The dollar's effect on a risk asset is a multi-day drift, not an
#: intraday tick — an hourly read is mostly noise, and 90 sessions is about a
#: quarter, enough for the trend to mean something.
LOOKBACK_DAYS = 90
TIMEFRAME = "D1"

#: Both series stop printing outside their own session, so over a weekend the
#: newest bar is legitimately two or three days old. Anything tighter than this
#: cries wolf every Sunday.
MAX_AGE_S = 4 * 24 * 3600

#: Daily bars change once a day. The cache exists because ``SMCStrategyEngine.
#: backtest`` re-enters ``analyze`` once per bar — without it a single backtest
#: would issue thousands of identical requests.
CACHE_TTL_S = 15 * 60

#: DXY moves in tenths of a percent. A day at ±0.5% is a decisive session for
#: the dollar, so that is where a full-strength reading sits.
DXY_FULL_MOVE_PCT = 0.5

#: The multi-session drift matters more than one close. Slope is measured over
#: the last two weeks and normalised against a move of this size per session.
DXY_SLOPE_BARS = 10
DXY_FULL_SLOPE = 0.0015          # 0.15% per session sustained = full strength

#: Weighting between "what the dollar did today" and "where it has been going".
#: The trend carries more because it is what survives a single noisy print.
DXY_CHANGE_WEIGHT = 0.4
DXY_TREND_WEIGHT = 0.6

#: VIX levels. Below 15 is a calm tape; above 25 is where correlations go to one
#: and risk assets stop trading on their own fundamentals. Between them the
#: reading scales rather than flipping at a threshold.
VIX_CALM = 15.0
VIX_STRESSED = 25.0

#: A VIX spike is more informative than its level: +20% in a session is a shock
#: even from a low base.
VIX_FULL_MOVE_PCT = 20.0
VIX_CHANGE_WEIGHT = 0.5
VIX_LEVEL_WEIGHT = 0.5

#: How the two gauges combine into one number for a risk asset. The dollar is
#: the more direct link for anything USD-priced; VIX is the broader risk tone.
DXY_COMPONENT_WEIGHT = 0.6
VIX_COMPONENT_WEIGHT = 0.4

#: Regime thresholds on the combined risk read.
REGIME_BAND = 0.25

#: Instrument classes VIX speaks to. A rising fear gauge pressures risk assets;
#: it says little about EURGBP.
_VIX_SENSITIVE_CLASSES = {"crypto", "index", "metal", "energy"}


# ── currency composition ─────────────────────────────────────────────────────

def usd_leg(symbol: str) -> UsdLegT:
    """Where the US dollar sits in *symbol*: its quote leg, its base leg, or absent.

    This is the whole of "is the factor compatible with this pair". A stronger
    dollar pushes EURUSD down and USDJPY up — the same macro reading with the
    sign reversed — and does neither to EURGBP.

    ``YAHOO_TICKERS`` is consulted before the six-letter currency split, exactly
    as ``market_data.classify`` does, because ``XTIUSD``/``XBRUSD``/``XNGUSD``
    look like FX pairs and are not.
    """
    from app.services import market_data

    s = market_data.normalize_symbol(symbol)
    if not s:
        return "none"

    # Named instruments first: indices, energy and softs are USD-denominated
    # without ever being a currency pair.
    if s in yahoo_provider.YAHOO_TICKERS:
        cls = market_data.classify(s)
        # A metal cross (XAUEUR) is priced in euros — the dollar leg is gone.
        if s[3:] and s[:3] in yahoo_provider._METAL_BASES and s[3:] != "USD":
            return "none"
        return "quote" if cls != market_data.UNKNOWN else "none"

    if len(s) == 6:
        base, quote = s[:3], s[3:]
        if base in yahoo_provider._CURRENCIES and quote in yahoo_provider._CURRENCIES:
            if quote == "USD":
                return "quote"
            if base == "USD":
                return "base"
            return "none"
        # Metals and crypto CFDs: XAUUSD, BTCUSD — USD-quoted, base is not a currency.
        if quote == "USD":
            return "quote"
        return "none"

    # Crypto pairs quoted in a dollar stablecoin move with the dollar the same
    # way a USD-quoted instrument does.
    for stable in ("USDT", "USDC", "USD"):
        if s.endswith(stable) and len(s) > len(stable):
            return "quote"

    return "none"


# ── statistics ───────────────────────────────────────────────────────────────
# Deliberately duplicated from KronosForecastPlugin.volume_context rather than
# imported: this is core, and core does not depend on a plugin. Ten lines is a
# cheaper price than that edge.

def _normalised_slope(values: Sequence[float]) -> Optional[float]:
    """Least-squares slope over the index, divided by the mean.

    Reads as "fraction of level gained per bar", so a dollar index near 100 and
    a VIX near 16 are directly comparable.
    """
    n = len(values)
    if n < 3:
        return None
    mean_v = sum(values) / n
    if mean_v <= 0:
        return None
    mean_t = (n - 1) / 2.0
    num = sum((i - mean_t) * (v - mean_v) for i, v in enumerate(values))
    den = sum((i - mean_t) ** 2 for i in range(n))
    if den <= 0:
        return None
    return (num / den) / mean_v


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _closes(rows: Sequence[Dict[str, Any]]) -> List[float]:
    """Finite closes, oldest first. Yahoo returns nulls on holidays."""
    out: List[float] = []
    for row in rows or []:
        try:
            close = float(row.get("close"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(close) and close > 0:
            out.append(close)
    return out


# ── the world ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MacroSnapshot:
    """What the dollar and the fear gauge are doing. Symbol-independent."""

    status: MacroStatusT
    regime: MacroRegimeT = "UNKNOWN"
    dxy_level: Optional[float] = None
    dxy_change_pct: Optional[float] = None
    dxy_slope: Optional[float] = None
    vix_level: Optional[float] = None
    vix_change_pct: Optional[float] = None
    as_of: Optional[int] = None
    age_s: int = 0
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "OK"


@dataclass(frozen=True)
class MacroBias:
    """The snapshot resolved for one instrument.

    ``normalized`` is signed **for the long side**: positive is a tailwind for a
    long, negative a headwind. A short reads the same number with the sign
    flipped, which is the caller's job — the same convention every other factor
    in ``smc_scoring`` uses.
    """

    applicable: bool
    normalized: float = 0.0
    regime: MacroRegimeT = "UNKNOWN"
    usd_leg: UsdLegT = "none"
    reason: str = ""
    lines: List[str] = field(default_factory=list)


def build_macro_snapshot(
    dxy_rows: Sequence[Dict[str, Any]],
    vix_rows: Sequence[Dict[str, Any]],
    *,
    now: Optional[float] = None,
) -> MacroSnapshot:
    """Two candle series → one macro reading. Pure and deterministic."""
    now_ts = time.time() if now is None else now
    dxy = _closes(dxy_rows)
    vix = _closes(vix_rows)

    if len(dxy) < 2 and len(vix) < 2:
        return MacroSnapshot(
            status="UNAVAILABLE",
            detail="Neither the dollar index nor the VIX returned usable bars.",
        )

    dxy_level = dxy[-1] if dxy else None
    dxy_change = ((dxy[-1] / dxy[-2] - 1.0) * 100.0) if len(dxy) >= 2 else None
    dxy_slope = _normalised_slope(dxy[-DXY_SLOPE_BARS:]) if len(dxy) >= 3 else None

    vix_level = vix[-1] if vix else None
    vix_change = ((vix[-1] / vix[-2] - 1.0) * 100.0) if len(vix) >= 2 else None

    as_of = _newest_time(dxy_rows, vix_rows)
    age_s = int(max(0.0, now_ts - as_of)) if as_of else 0

    status: MacroStatusT = "OK"
    detail = ""
    if as_of and age_s > MAX_AGE_S:
        status = "STALE"
        detail = (
            f"Newest macro bar is {age_s // 3600}h old — beyond the "
            f"{MAX_AGE_S // 3600}h a weekend explains."
        )

    risk = _risk_read(dxy_change, dxy_slope, vix_level, vix_change)
    regime: MacroRegimeT
    if status != "OK" or risk is None:
        regime = "UNKNOWN"
    elif risk >= REGIME_BAND:
        regime = "RISK_ON"
    elif risk <= -REGIME_BAND:
        regime = "RISK_OFF"
    else:
        regime = "NEUTRAL"

    if status == "OK" and not detail:
        detail = _describe(dxy_level, dxy_change, vix_level, vix_change)

    return MacroSnapshot(
        status=status,
        regime=regime,
        dxy_level=dxy_level,
        dxy_change_pct=dxy_change,
        dxy_slope=dxy_slope,
        vix_level=vix_level,
        vix_change_pct=vix_change,
        as_of=as_of,
        age_s=age_s,
        detail=detail,
    )


def _newest_time(*series: Sequence[Dict[str, Any]]) -> Optional[int]:
    times: List[int] = []
    for rows in series:
        for row in reversed(rows or []):
            try:
                times.append(int(row.get("time")))
                break
            except (TypeError, ValueError):
                continue
    return max(times) if times else None


def _dxy_pressure(change_pct: Optional[float], slope: Optional[float]) -> Optional[float]:
    """Dollar strength as [-1, 1]. Positive = the dollar is being bid."""
    parts: List[tuple[float, float]] = []
    if change_pct is not None:
        parts.append((_clamp(change_pct / DXY_FULL_MOVE_PCT), DXY_CHANGE_WEIGHT))
    if slope is not None:
        parts.append((_clamp(slope / DXY_FULL_SLOPE), DXY_TREND_WEIGHT))
    if not parts:
        return None
    total_w = sum(w for _, w in parts)
    return _clamp(sum(v * w for v, w in parts) / total_w)


def _vix_pressure(level: Optional[float], change_pct: Optional[float]) -> Optional[float]:
    """Fear as [-1, 1]. Positive = stress rising, a headwind for risk assets."""
    parts: List[tuple[float, float]] = []
    if level is not None:
        # Scaled across the calm→stressed band rather than flipped at a
        # threshold, so 24.9 and 25.1 are not different worlds.
        span = VIX_STRESSED - VIX_CALM
        parts.append((_clamp((level - VIX_CALM) / span * 2.0 - 1.0), VIX_LEVEL_WEIGHT))
    if change_pct is not None:
        parts.append((_clamp(change_pct / VIX_FULL_MOVE_PCT), VIX_CHANGE_WEIGHT))
    if not parts:
        return None
    total_w = sum(w for _, w in parts)
    return _clamp(sum(v * w for v, w in parts) / total_w)


def _risk_read(
    dxy_change: Optional[float],
    dxy_slope: Optional[float],
    vix_level: Optional[float],
    vix_change: Optional[float],
) -> Optional[float]:
    """Combined risk appetite as [-1, 1]. Positive = risk-on."""
    dxy_p = _dxy_pressure(dxy_change, dxy_slope)
    vix_p = _vix_pressure(vix_level, vix_change)
    parts: List[tuple[float, float]] = []
    if dxy_p is not None:
        parts.append((-dxy_p, DXY_COMPONENT_WEIGHT))   # strong dollar = risk-off
    if vix_p is not None:
        parts.append((-vix_p, VIX_COMPONENT_WEIGHT))   # rising fear = risk-off
    if not parts:
        return None
    total_w = sum(w for _, w in parts)
    return _clamp(sum(v * w for v, w in parts) / total_w)


def _describe(
    dxy_level: Optional[float],
    dxy_change: Optional[float],
    vix_level: Optional[float],
    vix_change: Optional[float],
) -> str:
    bits: List[str] = []
    if dxy_level is not None:
        move = f" ({dxy_change:+.2f}%)" if dxy_change is not None else ""
        bits.append(f"DXY {dxy_level:.3f}{move}")
    if vix_level is not None:
        move = f" ({vix_change:+.1f}%)" if vix_change is not None else ""
        bits.append(f"VIX {vix_level:.2f}{move}")
    return "  |  ".join(bits)


# ── the world, for one instrument ────────────────────────────────────────────

def macro_bias(symbol: str, snap: Optional[MacroSnapshot]) -> MacroBias:
    """Resolve a snapshot against one instrument.

    Returns ``applicable=False`` — with the reason spelled out — whenever the
    dollar has nothing to say about this pair or the snapshot could not be read.
    Callers must treat that as "score this setup without me", not as a zero.
    """
    leg = usd_leg(symbol)

    if snap is None or snap.status != "OK":
        detail = (snap.detail if snap else "") or "macro series unavailable"
        return MacroBias(
            applicable=False,
            usd_leg=leg,
            reason=f"macro unavailable ({detail})",
            lines=[f"Macro context unavailable — {detail}. Scored without it."],
        )

    from app.services import market_data

    asset_class = market_data.classify(symbol)
    dxy_p = _dxy_pressure(snap.dxy_change_pct, snap.dxy_slope)
    vix_p = _vix_pressure(snap.vix_level, snap.vix_change_pct)

    parts: List[tuple[float, float]] = []
    lines: List[str] = []

    if leg != "none" and dxy_p is not None:
        # USD on the quote side: a bid dollar prices the instrument lower.
        # USD on the base side: the same bid dollar prices it higher.
        signed = -dxy_p if leg == "quote" else dxy_p
        parts.append((signed, DXY_COMPONENT_WEIGHT))
        direction = "bid" if dxy_p > 0 else "offered"
        lines.append(
            f"DXY {snap.dxy_level:.3f} ({snap.dxy_change_pct:+.2f}%) is {direction}; "
            f"USD is the {leg} leg of {symbol}, so that is a "
            f"{'tailwind' if signed > 0 else 'headwind'} for longs."
        )

    if asset_class in _VIX_SENSITIVE_CLASSES and vix_p is not None:
        parts.append((-vix_p, VIX_COMPONENT_WEIGHT))
        lines.append(
            f"VIX {snap.vix_level:.2f} ({snap.vix_change_pct:+.1f}%) — "
            f"{'stress rising' if vix_p > 0 else 'stress easing'} on a "
            f"{asset_class} instrument."
        )

    if not parts:
        why = (
            "no USD leg" if leg == "none"
            else f"{asset_class or 'this instrument'} is not dollar- or risk-sensitive"
        )
        return MacroBias(
            applicable=False,
            regime=snap.regime,
            usd_leg=leg,
            reason=f"macro n/a ({why})",
            lines=[
                f"Macro context does not apply to {symbol} — {why}. "
                f"Scored on its own evidence, with no macro penalty."
            ],
        )

    total_w = sum(w for _, w in parts)
    normalized = _clamp(sum(v * w for v, w in parts) / total_w)
    stance = (
        "supportive" if normalized > 0.1
        else "a headwind" if normalized < -0.1
        else "neutral"
    )
    lines.append(f"Macro regime {snap.regime} → {stance} for a long ({normalized:+.2f}).")

    return MacroBias(
        applicable=True,
        normalized=normalized,
        regime=snap.regime,
        usd_leg=leg,
        reason=_short_reason(snap, normalized),
        lines=lines,
    )


def _short_reason(snap: MacroSnapshot, normalized: float) -> str:
    """One clause, for splicing into a signal's reason string."""
    bits: List[str] = []
    if snap.dxy_level is not None and snap.dxy_change_pct is not None:
        bits.append(f"DXY {snap.dxy_change_pct:+.2f}%")
    if snap.vix_level is not None:
        bits.append(f"VIX {snap.vix_level:.1f}")
    head = " / ".join(bits) if bits else "macro"
    return f"{head} {snap.regime.lower().replace('_', '-')} ({normalized:+.2f})"


def macro_evidence_lines(bias: Optional[MacroBias], snap: Optional[MacroSnapshot] = None) -> List[str]:
    """Plain-language evidence, safe to call at any status."""
    if bias is None:
        return ["Macro context was not consulted for this signal."]
    if bias.lines:
        lines = list(bias.lines)
    else:
        lines = [bias.reason or "Macro context carried no reading."]
    if snap is not None and snap.status == "OK" and snap.age_s > 24 * 3600:
        lines.append(
            f"Macro bars are {snap.age_s // 3600}h old — the cash sessions were closed."
        )
    return lines


# ── resolution ───────────────────────────────────────────────────────────────

CandleFetcher = Callable[[str, str, int], Awaitable[Sequence[Dict[str, Any]]]]

_cache: Optional[MacroSnapshot] = None
_cache_at: float = 0.0
_lock = asyncio.Lock()


async def _default_fetcher(symbol: str, timeframe: str, limit: int) -> Sequence[Dict[str, Any]]:
    """Yahoo daily bars.

    ``yahoo_provider.fetch_candles`` rather than ``market_data.fetch_ohlcv_universal``:
    the latter downloads ten years of dailies whatever ``limit`` says and makes a
    second call for a quote this module never uses.
    """
    return await yahoo_provider.fetch_candles(symbol, timeframe, limit)


async def resolve_macro_snapshot(
    *,
    fetcher: Optional[CandleFetcher] = None,
    now: Optional[float] = None,
    force: bool = False,
) -> MacroSnapshot:
    """The current macro reading, cached for ``CACHE_TTL_S``. Never raises."""
    global _cache, _cache_at

    clock = time.time() if now is None else now
    if not force and _cache is not None and (clock - _cache_at) < CACHE_TTL_S:
        return _cache

    async with _lock:
        # Another coroutine may have filled it while we waited.
        if not force and _cache is not None and (clock - _cache_at) < CACHE_TTL_S:
            return _cache

        fetch = fetcher or _default_fetcher
        try:
            dxy_rows, vix_rows = await asyncio.gather(
                fetch(DXY_SYMBOL, TIMEFRAME, LOOKBACK_DAYS),
                fetch(VIX_SYMBOL, TIMEFRAME, LOOKBACK_DAYS),
                return_exceptions=False,
            )
            snapshot = build_macro_snapshot(dxy_rows or [], vix_rows or [], now=now)
        except Exception as exc:  # noqa: BLE001 — an outage is context, not a crash
            logger.debug("[Macro] snapshot fetch failed: {}", exc)
            snapshot = MacroSnapshot(
                status="UNAVAILABLE",
                detail=f"macro fetch failed ({exc})",
            )

        _cache = snapshot
        _cache_at = clock
        return snapshot


async def resolve_macro_bias(
    symbol: str,
    *,
    fetcher: Optional[CandleFetcher] = None,
    now: Optional[float] = None,
) -> MacroBias:
    """Snapshot + per-instrument resolution in one call. Never raises."""
    try:
        snap = await resolve_macro_snapshot(fetcher=fetcher, now=now)
        return macro_bias(symbol, snap)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Macro] bias for {} failed: {}", symbol, exc)
        return MacroBias(
            applicable=False,
            reason=f"macro unavailable ({exc})",
            lines=["Macro context unavailable. Scored without it."],
        )


def reset_cache() -> None:
    """Drop the cached snapshot — for tests and for a forced refresh."""
    global _cache, _cache_at
    _cache = None
    _cache_at = 0.0
