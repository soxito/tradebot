"""
Kronos Forecast Plugin — Forecast Service

Orchestrates a forecast end-to-end:
  1. Pull recent OHLCV from the existing exchange connectors (core, read-only).
  2. Build the pandas frame Kronos expects.
  3. Run the model (or a transparent heuristic fallback) off the event loop.
  4. Aggregate sample paths into a mean forecast + confidence bands.
  5. Derive a forward-looking signal and ready-to-render chart overlays.
  6. Optionally log the forecast for later accuracy scoring.

Reuses core `exchange_manager` by ID only — no cross-boundary imports of core
internals beyond the public manager, and no core files are modified.
"""
from __future__ import annotations

import asyncio
import math
import os
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from loguru import logger

from app.exchanges.manager import exchange_manager, SupportedExchange

from plugins.KronosForecastPlugin.backend.config import kronos_config
from plugins.KronosForecastPlugin.backend.services.kronos_engine import kronos_engine
from plugins.KronosForecastPlugin.backend.schemas import (
    ForecastResponse, ForecastCandle, BandPoint, ForecastSignal,
    OverlaySeries, OverlayLinePoint, OverlayMarker, ForecastStatus,
    SniperSignal, SniperSignalsResponse,
)

# ── timeframe helpers ───────────────────────────────────────────

_TF_SECONDS: Dict[str, int] = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200,
    "1d": 86400, "3d": 259200, "1w": 604800,
}


def _tf_seconds(timeframe: str) -> int:
    return _TF_SECONDS.get(timeframe.lower(), 3600)


# ── OHLCV fetch (reuse core exchange manager + forex/metals fallback) ───────

def _is_forex(symbol: str) -> bool:
    """Return True if the symbol should be fetched via the forex provider."""
    try:
        from app.exchanges.forex_provider import is_forex_symbol
        return is_forex_symbol(symbol)
    except Exception:
        return False


async def _fetch_forex_ohlcv(symbol: str, timeframe: str, limit: int) -> List[list]:
    """Fetch OHLCV for forex/metals from the forex_provider (Yahoo Finance / CoinGecko).
    Returns ccxt-compatible rows: [timestamp_ms, open, high, low, close, volume].
    """
    try:
        from app.exchanges.forex_provider import fetch_ohlcv as forex_fetch_ohlcv
        rows, _ticker = await forex_fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        return rows
    except Exception as exc:
        logger.warning(f"[Kronos] forex OHLCV failed for {symbol}: {exc}")
        return []


_OHLCV_QUOTES = ("USDT", "USDC", "USD", "BTC", "ETH", "EUR", "DAI", "TUSD")


def _symbol_variants(symbol: str) -> List[str]:
    """Return de-duplicated symbol spellings to try (with and without the
    BASE/QUOTE slash), since connectors expect different formats
    (e.g. ccxt wants 'BTC/USDT', some callers pass 'BTCUSDT')."""
    s = (symbol or "").strip().upper()
    variants: List[str] = []

    def _add(v: str) -> None:
        if v and v not in variants:
            variants.append(v)

    _add(s)
    if "/" in s:
        _add(s.replace("/", ""))
    else:
        for q in _OHLCV_QUOTES:
            if s.endswith(q) and len(s) > len(q):
                _add(f"{s[:-len(q)]}/{q}")
                break
    return variants


# Keyless public exchanges tried as a last resort so a forecast always has
# candles even when NO exchange is configured with API keys (the usual cause of
# "No OHLCV data available"). ccxt public OHLCV endpoints need no credentials.
_PUBLIC_CCXT_EXCHANGES = ("binance", "bybit", "okx", "kucoin", "gateio", "mexc")


async def _fetch_public_ccxt_ohlcv(symbol: str, timeframe: str, limit: int) -> List[list]:
    """Fetch OHLCV from a keyless public ccxt exchange (no API keys required).

    Creates a short-lived async client per exchange, tries each symbol spelling,
    and always closes the client to avoid leaking aiohttp sessions.
    """
    try:
        import ccxt.async_support as ccxt_async  # ccxt is a core backend dep
    except Exception as exc:
        logger.warning(f"[Kronos] ccxt not available for public OHLCV fallback: {exc}")
        return []

    variants = [v for v in _symbol_variants(symbol) if "/" in v]  # ccxt needs BASE/QUOTE
    if not variants:
        return []

    for ex_id in _PUBLIC_CCXT_EXCHANGES:
        if not hasattr(ccxt_async, ex_id):
            continue
        client = None
        try:
            client = getattr(ccxt_async, ex_id)({"enableRateLimit": True, "timeout": 15000})
            for sym in variants:
                try:
                    rows = await client.fetch_ohlcv(sym, timeframe=timeframe, limit=limit)
                    if rows and len(rows) >= 5:
                        logger.info(f"[Kronos] Public OHLCV resolved via {ex_id} for {sym} {timeframe}")
                        return rows
                except Exception as e:
                    logger.debug(f"[Kronos] public {ex_id} {sym} {timeframe} failed: {e}")
                    continue
        except Exception as e:
            logger.debug(f"[Kronos] could not init public ccxt {ex_id}: {e}")
        finally:
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    pass
    return []


async def _fetch_ohlcv(exchange: str, symbol: str, timeframe: str, limit: int) -> List[list]:
    # Route forex / metals through the dedicated provider first.
    if _is_forex(symbol):
        rows = await _fetch_forex_ohlcv(symbol, timeframe, limit)
        if rows:
            return rows

    # Build the ordered list of exchanges to try: the requested one first, then
    # every other initialised exchange as a fallback. A symbol the primary
    # exchange doesn't list (which otherwise yields "No OHLCV data available")
    # can still resolve on another exchange that does.
    candidates: List = []
    try:
        req = exchange_manager.get_exchange(SupportedExchange(exchange))
        if req is not None:
            candidates.append(req)
    except (ValueError, Exception):
        pass
    for ex_id in exchange_manager.get_all_exchanges():
        ex = exchange_manager.get_exchange(ex_id)
        if ex is not None and ex not in candidates:
            candidates.append(ex)
    if not candidates:
        logger.warning(f"[Kronos] No exchanges initialised — trying keyless public OHLCV for {symbol}")
        return await _fetch_public_ccxt_ohlcv(symbol, timeframe, limit)

    variants = _symbol_variants(symbol)
    last_err: Optional[Exception] = None
    for ex in candidates:
        ex_name = getattr(ex, "exchange_name", ex.__class__.__name__)
        for sym in variants:
            try:
                rows = await ex.get_ohlcv(sym, timeframe, limit)
                if rows and len(rows) >= 5:
                    return rows
            except Exception as e:  # try the next spelling / exchange
                last_err = e
                logger.debug(f"[Kronos] OHLCV {ex_name} {sym} {timeframe} failed: {e}")
                continue
    # Last resort: keyless public ccxt fetch (no API keys / configured exchanges needed).
    public_rows = await _fetch_public_ccxt_ohlcv(symbol, timeframe, limit)
    if public_rows:
        return public_rows
    if last_err:
        logger.warning(
            f"[Kronos] OHLCV unavailable for {symbol} {timeframe} on all "
            f"{len(candidates)} exchange(s); last error: {last_err}"
        )
    return []


def _rows_to_frame(rows: List[list]) -> Tuple[pd.DataFrame, pd.Series]:
    """Return (df[open,high,low,close,volume,amount], x_timestamp) from ccxt rows."""
    recs = []
    ts = []
    for r in rows:
        t = int(r[0] / 1000)  # ms -> s
        ts.append(datetime.fromtimestamp(t, tz=timezone.utc).replace(tzinfo=None))
        o, h, l, c = float(r[1]), float(r[2]), float(r[3]), float(r[4])
        v = float(r[5]) if len(r) > 5 else 0.0
        recs.append({"open": o, "high": h, "low": l, "close": c,
                     "volume": v, "amount": v * c})
    df = pd.DataFrame(recs)
    return df, pd.Series(ts)


def _future_timestamps(last_unix: int, step: int, n: int) -> Tuple[pd.Series, List[int]]:
    unix = [last_unix + step * (i + 1) for i in range(n)]
    dt = pd.Series([datetime.fromtimestamp(u, tz=timezone.utc).replace(tzinfo=None) for u in unix])
    return dt, unix


# ── aggregation ─────────────────────────────────────────────────

def _aggregate(
    paths: List[pd.DataFrame], future_unix: List[int]
) -> Tuple[List[ForecastCandle], List[BandPoint], List[BandPoint]]:
    """Mean OHLCV forecast + p10/p90 close bands from sample paths."""
    closes = np.array([p["close"].to_numpy()[: len(future_unix)] for p in paths])  # [S, H]
    opens = np.array([p["open"].to_numpy()[: len(future_unix)] for p in paths])
    highs = np.array([p["high"].to_numpy()[: len(future_unix)] for p in paths])
    lows = np.array([p["low"].to_numpy()[: len(future_unix)] for p in paths])
    vols = np.array([
        (p["volume"].to_numpy()[: len(future_unix)] if "volume" in p else np.zeros(len(future_unix)))
        for p in paths
    ])

    mean_close = closes.mean(axis=0)
    mean_open = opens.mean(axis=0)
    mean_high = highs.mean(axis=0)
    mean_low = lows.mean(axis=0)
    mean_vol = vols.mean(axis=0)

    p10 = np.percentile(closes, 10, axis=0)
    p90 = np.percentile(closes, 90, axis=0)

    forecast = [
        ForecastCandle(
            time=future_unix[i], open=float(mean_open[i]), high=float(mean_high[i]),
            low=float(mean_low[i]), close=float(mean_close[i]), volume=float(mean_vol[i]),
        )
        for i in range(len(future_unix))
    ]
    upper = [BandPoint(time=future_unix[i], value=float(p90[i])) for i in range(len(future_unix))]
    lower = [BandPoint(time=future_unix[i], value=float(p10[i])) for i in range(len(future_unix))]
    return forecast, upper, lower


def _direction_confidence(
    paths: List[pd.DataFrame], anchor: float, horizon: int
) -> Tuple[str, float, float, float]:
    """Return (direction, pct_change, confidence, target_price)."""
    finals = np.array([float(p["close"].to_numpy()[: horizon][-1]) for p in paths])
    target = float(finals.mean())
    pct = (target - anchor) / anchor * 100.0 if anchor else 0.0

    if pct > 0.15:
        direction = "up"
    elif pct < -0.15:
        direction = "down"
    else:
        direction = "flat"

    # Agreement: fraction of sample paths ending on the same side as the mean move
    if direction == "up":
        agree = float((finals > anchor).mean())
    elif direction == "down":
        agree = float((finals < anchor).mean())
    else:
        agree = float((np.abs(finals - anchor) / anchor < 0.0015).mean()) if anchor else 0.5

    # Dispersion penalty: wider spread (relative to price) => lower confidence
    spread = float(finals.std() / anchor) if anchor else 0.0
    confidence = max(0.0, min(1.0, 0.6 * agree + 0.4 * max(0.0, 1.0 - spread * 40)))
    return direction, pct, confidence, target


# ── heuristic fallback (no model) ───────────────────────────────

def _heuristic_paths(df: pd.DataFrame, pred_len: int, samples: int) -> List[pd.DataFrame]:
    """Geometric random walk seeded from recent drift + volatility.
    Used only when the Kronos model is not installed, so the UI/JARVIS still work."""
    close = df["close"].to_numpy()
    if len(close) < 3:
        last = float(close[-1]) if len(close) else 1.0
        flat = pd.DataFrame({"open": [last] * pred_len, "high": [last] * pred_len,
                             "low": [last] * pred_len, "close": [last] * pred_len,
                             "volume": [0.0] * pred_len})
        return [flat.copy() for _ in range(samples)]

    log_ret = np.diff(np.log(np.clip(close, 1e-9, None)))
    drift = float(np.mean(log_ret[-100:]))
    vol = float(np.std(log_ret[-100:])) or 1e-4
    last = float(close[-1])
    last_vol = float(df["volume"].to_numpy()[-20:].mean()) if "volume" in df else 0.0

    rng = np.random.default_rng(42)
    paths: List[pd.DataFrame] = []
    for _ in range(max(1, samples)):
        prices = []
        p = last
        for _h in range(pred_len):
            shock = rng.normal(drift, vol)
            p = p * math.exp(shock)
            prices.append(p)
        prices = np.array(prices)
        # Cheap OHLC envelope around the close path
        highs = prices * (1 + vol / 2)
        lows = prices * (1 - vol / 2)
        opens = np.concatenate([[last], prices[:-1]])
        paths.append(pd.DataFrame({
            "open": opens, "high": np.maximum(highs, np.maximum(opens, prices)),
            "low": np.minimum(lows, np.minimum(opens, prices)), "close": prices,
            "volume": np.full(pred_len, last_vol),
        }))
    return paths


# ── overlays ────────────────────────────────────────────────────

def _heuristic_note(reason: Optional[str] = None) -> str:
    """Actionable message shown when a forecast falls back to the heuristic
    engine, surfacing the underlying reason."""
    # Model is loaded but this particular request still fell back — a transient
    # inference error, not a missing model. Don't tell the user to reinstall.
    if kronos_engine.available:
        detail = f" ({reason})" if reason else ""
        return f"Heuristic forecast — Kronos was momentarily unavailable for this request{detail}. Retry the forecast."
    err = kronos_engine.load_error
    base = "Heuristic forecast — Kronos model weights not installed."
    if err:
        base = f"Heuristic forecast — Kronos unavailable ({err})."
    from plugins.KronosForecastPlugin.backend.services.kronos_engine import kronos_setup_command
    return base + f" Run: {kronos_setup_command()}"


def _build_overlays(
    forecast: List[ForecastCandle], upper: List[BandPoint], lower: List[BandPoint],
    anchor_unix: int, anchor_price: float, direction: str,
) -> Tuple[List[OverlaySeries], List[OverlayMarker]]:
    up = direction == "up"
    line_color = "#a855f7"  # kronos purple
    band_color = "rgba(168,85,247,0.4)"

    def pts(seq, get):
        # Anchor each line at the last real candle so it connects to price history
        out = [OverlayLinePoint(time=anchor_unix, value=anchor_price)]
        out.extend(OverlayLinePoint(time=get(x)[0], value=get(x)[1]) for x in seq)
        return out

    forecast_line = OverlaySeries(
        name="Kronos Forecast", color=line_color, lineWidth=2, lineStyle=0,
        data=pts(forecast, lambda c: (c.time, c.close)),
    )
    upper_line = OverlaySeries(
        name="Kronos Upper (p90)", color=band_color, lineWidth=1, lineStyle=2,
        data=pts(upper, lambda b: (b.time, b.value)),
    )
    lower_line = OverlaySeries(
        name="Kronos Lower (p10)", color=band_color, lineWidth=1, lineStyle=2,
        data=pts(lower, lambda b: (b.time, b.value)),
    )

    markers: List[OverlayMarker] = []
    if forecast:
        markers.append(OverlayMarker(
            time=forecast[-1].time,
            position="aboveBar" if up else "belowBar",
            color="#22c55e" if up else ("#ef4444" if direction == "down" else "#eab308"),
            shape="arrowUp" if up else ("arrowDown" if direction == "down" else "circle"),
            text="Kronos",
        ))
    return [forecast_line, upper_line, lower_line], markers


# ── public API ──────────────────────────────────────────────────

def get_status() -> ForecastStatus:
    available = kronos_engine.available
    loading = kronos_engine.loading
    # Report the model actually loaded (reflects hot-swaps), falling back to the
    # configured default when nothing is loaded yet.
    model_name = kronos_engine.active_model or kronos_config.model_name
    tokenizer_name = kronos_engine.active_tokenizer or kronos_config.tokenizer_name
    max_context = kronos_engine.active_max_context or kronos_config.max_context
    if available:
        detail = "Kronos model loaded"
    elif loading:
        detail = f"Kronos model warming up ({model_name})…"
    else:
        detail = kronos_engine.load_error or "heuristic fallback"
    return ForecastStatus(
        available=True,  # the endpoint always works (heuristic fallback)
        # Report kronos while warming up too — forecasts wait for the load and
        # will run on the real model, so the badge shouldn't flash "heuristic".
        engine="kronos" if (available or loading) else "heuristic",
        model_name=model_name,
        tokenizer_name=tokenizer_name,
        device=kronos_config.device,
        max_context=max_context,
        detail=detail,
    )


async def run_forecast(
    exchange: str,
    symbol: str,
    timeframe: str = "1h",
    lookback: Optional[int] = None,
    pred_len: Optional[int] = None,
    samples: Optional[int] = None,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
) -> ForecastResponse:
    lookback = min(lookback or kronos_config.default_lookback, kronos_config.max_lookback)
    pred_len = pred_len or kronos_config.default_pred_len
    samples = samples or kronos_config.default_samples
    temperature = temperature if temperature is not None else kronos_config.temperature
    top_p = top_p if top_p is not None else kronos_config.top_p

    # Report the model actually loaded into the engine (reflects hot-swaps),
    # not the static config default.
    active_model_name = kronos_engine.active_model or kronos_config.model_name

    rows = await _fetch_ohlcv(exchange, symbol, timeframe, lookback)
    if not rows or len(rows) < 5:
        return ForecastResponse(
            exchange=exchange, symbol=symbol, timeframe=timeframe, engine="unavailable",
            model_name=active_model_name, lookback=lookback, pred_len=pred_len,
            samples=samples, anchor_time=0, anchor_price=0.0,
            note=f"No OHLCV data for {symbol} ({timeframe}) on any connected exchange — check the symbol or try another timeframe.",
        )

    df, x_ts = _rows_to_frame(rows)
    anchor_price = float(df["close"].iloc[-1])
    anchor_unix = int(rows[-1][0] / 1000)
    step = _tf_seconds(timeframe)
    y_ts, future_unix = _future_timestamps(anchor_unix, step, pred_len)

    loop = asyncio.get_event_loop()
    engine = "kronos"
    heuristic_reason: Optional[str] = None
    # Wrap inference in asyncio.wait_for so a hung MPS forward pass can never
    # stall the request indefinitely. Timeout matches the lock timeout in the
    # engine (+30s grace so the lock error fires first).
    _infer_timeout = int(os.getenv("KRONOS_INFER_TIMEOUT_S", "120")) + 30
    try:
        # predict_paths loads the model on first use *inside this worker thread*
        # (never the event loop, so a ~60s cold-load can't freeze the backend)
        # and raises if the model is genuinely unavailable → heuristic fallback.
        paths = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: kronos_engine.predict_paths(
                    df, x_ts, y_ts, pred_len,
                    temperature=temperature, top_p=top_p, samples=samples,
                ),
            ),
            timeout=_infer_timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(
            f"[Kronos] inference timed out after {_infer_timeout}s, using heuristic"
        )
        engine = "heuristic"
        heuristic_reason = f"Inference timed out after {_infer_timeout}s (MPS hang?)"
        paths = _heuristic_paths(df, pred_len, samples)
    except Exception as exc:
        logger.warning(f"[Kronos] inference unavailable, using heuristic: {exc}")
        engine = "heuristic"
        heuristic_reason = str(exc)
        paths = _heuristic_paths(df, pred_len, samples)

    forecast, upper, lower = _aggregate(paths, future_unix)
    direction, pct, confidence, target = _direction_confidence(paths, anchor_price, pred_len)
    overlays, markers = _build_overlays(forecast, upper, lower, anchor_unix, anchor_price, direction)

    horizon_label = f"{pred_len}×{timeframe}"
    arrow = "▲" if direction == "up" else ("▼" if direction == "down" else "→")
    summary = (
        f"Kronos projects {symbol} {direction} {arrow} {pct:+.2f}% over the next {horizon_label} "
        f"(target {target:.6g}, {int(confidence * 100)}% confidence)."
    )
    signal = ForecastSignal(
        direction=direction, pct_change=round(pct, 4), confidence=round(confidence, 3),
        target_price=target, anchor_price=anchor_price, summary=summary,
    )

    # Convert historical rows to ForecastCandle objects so the frontend can draw
    # them when the exchange has no data (forex/metals).
    historical_candles: List[ForecastCandle] = []
    if _is_forex(symbol):
        for r in rows:
            try:
                historical_candles.append(ForecastCandle(
                    time=int(r[0] / 1000),
                    open=float(r[1]), high=float(r[2]),
                    low=float(r[3]),  close=float(r[4]),
                    volume=float(r[5]) if len(r) > 5 else 0.0,
                ))
            except Exception:
                pass

    resp = ForecastResponse(
        exchange=exchange, symbol=symbol, timeframe=timeframe, engine=engine,
        model_name=active_model_name, lookback=lookback, pred_len=pred_len,
        samples=samples, anchor_time=anchor_unix, anchor_price=anchor_price,
        forecast=forecast, upper_band=upper, lower_band=lower, signal=signal,
        overlays=overlays, markers=markers, candles=historical_candles,
        note=None if engine == "kronos" else _heuristic_note(heuristic_reason),
    )

    # Best-effort log (never breaks the response)
    try:
        await _log_forecast(resp)
    except Exception as exc:
        logger.debug(f"[Kronos] forecast log skipped: {exc}")

    return resp


async def _log_forecast(resp: ForecastResponse) -> None:
    from app.core.database import AsyncSessionLocal
    from plugins.KronosForecastPlugin.backend.models import KronosForecast

    async with AsyncSessionLocal() as session:
        row = KronosForecast(
            exchange=resp.exchange, symbol=resp.symbol, timeframe=resp.timeframe,
            model_name=resp.model_name, lookback=resp.lookback, pred_len=resp.pred_len,
            samples=resp.samples, temperature=kronos_config.temperature, top_p=kronos_config.top_p,
            engine=resp.engine, anchor_price=resp.anchor_price, anchor_time=resp.anchor_time,
            direction=resp.signal.direction if resp.signal else None,
            pct_change=resp.signal.pct_change if resp.signal else None,
            confidence=resp.signal.confidence if resp.signal else None,
            forecast_json={"forecast": [c.model_dump() for c in resp.forecast]},
            created_at=datetime.utcnow(),
        )
        session.add(row)
        await session.commit()


# Lightweight in-process cache so JARVIS + charts sharing a symbol don't recompute
_cache: Dict[str, Tuple[float, ForecastResponse]] = {}
# In-flight forecasts, keyed the same as _cache. The forecast page fires
# /forecast + /sniper + /analyze for the same symbol at once; since inference is
# serialised, coalescing them onto one task avoids running it 3× back-to-back.
_inflight: Dict[str, "asyncio.Future[ForecastResponse]"] = {}


async def forecast_from_rows(
    rows: List[list],
    *,
    symbol: str,
    timeframe: str = "1h",
    exchange: str = "mt5",
    pred_len: Optional[int] = None,
    samples: Optional[int] = None,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
) -> Optional[ForecastResponse]:
    """Forecast directly from caller-supplied OHLCV rows (ccxt ``[ms, o, h, l, c, v]``).

    Lets surfaces that already hold candles (e.g. the MT5 SMC sniper chart) run
    Kronos on the exact series they display — so it works for ANY symbol,
    including instruments not listed on a crypto exchange (XAUUSD, indices, FX).
    Returns ``None`` if there isn't enough data.
    """
    if not rows or len(rows) < 5:
        return None
    pred_len = pred_len or kronos_config.default_pred_len
    samples = samples or kronos_config.default_samples
    temperature = temperature if temperature is not None else kronos_config.temperature
    top_p = top_p if top_p is not None else kronos_config.top_p

    df, x_ts = _rows_to_frame(rows)
    anchor_price = float(df["close"].iloc[-1])
    anchor_unix = int(rows[-1][0] / 1000)
    step = _tf_seconds(timeframe)
    y_ts, future_unix = _future_timestamps(anchor_unix, step, pred_len)

    loop = asyncio.get_event_loop()
    engine = "kronos"
    try:
        if kronos_engine.available:
            paths = await loop.run_in_executor(
                None,
                lambda: kronos_engine.predict_paths(
                    df, x_ts, y_ts, pred_len,
                    temperature=temperature, top_p=top_p, samples=samples,
                ),
            )
        else:
            engine = "heuristic"
            paths = _heuristic_paths(df, pred_len, samples)
    except Exception as exc:
        logger.warning(f"[Kronos] inference (rows) failed, using heuristic: {exc}")
        engine = "heuristic"
        paths = _heuristic_paths(df, pred_len, samples)

    forecast, upper, lower = _aggregate(paths, future_unix)
    direction, pct, confidence, target = _direction_confidence(paths, anchor_price, pred_len)
    overlays, markers = _build_overlays(forecast, upper, lower, anchor_unix, anchor_price, direction)
    horizon_label = f"{pred_len}\u00d7{timeframe}"
    arrow = "\u25b2" if direction == "up" else ("\u25bc" if direction == "down" else "\u2192")
    summary = (
        f"Kronos projects {symbol} {direction} {arrow} {pct:+.2f}% over the next {horizon_label} "
        f"(target {target:.6g}, {int(confidence * 100)}% confidence)."
    )
    signal = ForecastSignal(
        direction=direction, pct_change=round(pct, 4), confidence=round(confidence, 3),
        target_price=target, anchor_price=anchor_price, summary=summary,
    )
    return ForecastResponse(
        exchange=exchange, symbol=symbol, timeframe=timeframe, engine=engine,
        model_name=kronos_config.model_name, lookback=len(rows), pred_len=pred_len,
        samples=samples, anchor_time=anchor_unix, anchor_price=anchor_price,
        forecast=forecast, upper_band=upper, lower_band=lower, signal=signal,
        overlays=overlays, markers=markers,
        note=None if engine == "kronos" else _heuristic_note(),
    )


async def run_forecast_cached(exchange: str, symbol: str, timeframe: str, **kwargs) -> ForecastResponse:
    key = f"{exchange}:{symbol}:{timeframe}:{kwargs.get('pred_len')}:{kwargs.get('samples')}"
    now = time.time()
    hit = _cache.get(key)
    if hit and (now - hit[0]) < kronos_config.cache_ttl_s:
        return hit[1]

    # Coalesce concurrent identical requests onto a single computation.
    existing = _inflight.get(key)
    if existing is not None:
        return await existing

    async def _compute() -> ForecastResponse:
        resp = await run_forecast(exchange, symbol, timeframe, **kwargs)
        # Only cache real Kronos forecasts. Heuristic/unavailable results are
        # transient — the model may still be cold-loading (or downloading weights
        # on first use). Caching them would pin the "weights not installed"
        # fallback for the whole TTL and keep serving it after the model is ready.
        if resp.engine == "kronos":
            _cache[key] = (time.time(), resp)
        return resp

    task: "asyncio.Future[ForecastResponse]" = asyncio.ensure_future(_compute())
    _inflight[key] = task
    try:
        return await task
    finally:
        _inflight.pop(key, None)


# ── sniper signals (executable entries from the forecast) ───────

def _fmt_price(v: float) -> str:
    """Human price formatting for reasons (keeps precision for small coins)."""
    if v >= 1000:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:.4f}".rstrip("0").rstrip(".")
    return f"{v:.6g}"


async def _max_leverage(exchange: str, symbol: str) -> Optional[int]:
    """Best-effort lookup of the exchange's maximum leverage for a pair.

    Uses the connector's ``get_max_leverage`` (Bitget exposes it) and returns
    the max leg of the (min, max) tuple. Returns None for forex/metals or when
    the exchange/connector doesn't support it, so callers fall back gracefully.
    """
    if _is_forex(symbol):
        return None
    try:
        exch = exchange_manager.get_exchange(SupportedExchange(exchange))
    except (ValueError, Exception):
        exch = None
    if exch is None:
        available = exchange_manager.get_all_exchanges()
        exch = exchange_manager.get_exchange(available[0]) if available else None
    if exch is None:
        return None
    getter = getattr(exch, "get_max_leverage", None)
    if getter is None:
        return None
    try:
        _min_l, max_l = await getter(symbol)
        return int(max_l) if max_l and int(max_l) > 0 else None
    except Exception as exc:
        logger.debug(f"[Kronos] max leverage lookup failed for {symbol}: {exc}")
        return None


def _pick_float(d: dict, keys: Tuple[str, ...]) -> float:
    """First parseable positive-or-any float among the given keys, else 0.0."""
    for k in keys:
        raw = d.get(k)
        if raw in (None, ""):
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return 0.0


def _available_margin(accounts: Optional[List[dict]], margin_coin: str = "USDT") -> float:
    """Openable margin (USDT) from a Bitget futures account payload. Prefers
    openable fields over raw `available` (which can be negative under cross)."""
    best = 0.0
    for a in accounts or []:
        mc = str(a.get("marginCoin", "") or "").upper()
        if mc and mc != margin_coin.upper():
            continue
        vals: List[float] = []
        for k in ("crossedMaxAvailable", "unionAvailable", "isolatedMaxAvailable", "maxTransferOut", "available"):
            raw = a.get(k)
            if raw in (None, ""):
                continue
            try:
                vals.append(float(raw))
            except (TypeError, ValueError):
                continue
        if not vals:
            continue
        positives = [v for v in vals if v > 0]
        cand = max(positives) if positives else max(vals)
        best = max(best, cand)
    return best


async def get_pair_tradability(exchange: str, symbol: str) -> dict:
    """Live account context for placing an order on a pair: openable margin,
    max leverage, contract min size/precision, any existing position, and the
    resulting max-open-remaining (notional + margin) for the symbol.

    Read-only; used by the sniper panel to size orders against what's actually
    available on the exchange. Returns tradable=False for non-crypto pairs.
    """
    result: dict = {
        "exchange": exchange, "symbol": symbol, "tradable": False,
        "margin_coin": "USDT", "available_margin": 0.0, "max_leverage": None,
        "min_trade_size": None, "size_precision": None, "min_notional": None,
        "current_position": None, "max_open_margin": 0.0, "max_open_notional": 0.0,
        "note": None,
    }
    if _is_forex(symbol):
        result["note"] = "Order placement is only available for crypto USDT futures pairs."
        return result

    try:
        exch = exchange_manager.get_exchange(SupportedExchange(exchange))
    except (ValueError, Exception):
        exch = None
    if exch is None or not hasattr(exch, "get_futures_balance"):
        result["note"] = f"{exchange} does not support futures order placement."
        return result

    compact = symbol.replace("/", "").upper()

    # Max leverage (this also refreshes the connector's precision cache).
    try:
        _min_l, max_l = await exch.get_max_leverage(symbol)
        result["max_leverage"] = int(max_l) if max_l else None
    except Exception as exc:
        logger.debug(f"[Kronos] tradability max-leverage failed: {exc}")

    # Contract min size + volume precision.
    try:
        pcache = getattr(exch, "_precision_cache", {}) or {}
        prec = pcache.get(compact) or pcache.get(symbol)
        if prec:
            result["min_trade_size"] = float(prec.get("minTradeNum", 0) or 0) or None
            result["size_precision"] = int(prec.get("volumePlace", 4) or 4)
    except Exception:
        pass

    # Openable margin (USDT).
    try:
        accounts = await exch.get_futures_balance()
        result["available_margin"] = round(_available_margin(accounts), 2)
    except Exception as exc:
        result["note"] = f"Could not read Bitget balance: {exc}"

    # Existing position on this pair (so max-open accounts for what's already used).
    try:
        positions = await exch.get_futures_positions()
        for p in positions or []:
            psym = str(p.get("symbol", "")).replace("/", "").upper()
            if psym != compact:
                continue
            size = _pick_float(p, ("total", "available", "size"))
            if size <= 0:
                continue
            mark = _pick_float(p, ("markPrice", "marketPrice", "openPriceAvg", "averageOpenPrice"))
            margin = _pick_float(p, ("marginSize", "margin", "initialMargin"))
            result["current_position"] = {
                "side": p.get("holdSide") or p.get("side"),
                "size": size,
                "margin": round(margin, 2) if margin else None,
                "notional": round(size * mark, 2) if mark else None,
            }
            break
    except Exception as exc:
        logger.debug(f"[Kronos] tradability position lookup failed: {exc}")

    avail = result["available_margin"] or 0.0
    lev = result["max_leverage"] or 1
    result["max_open_margin"] = round(avail, 2)
    result["max_open_notional"] = round(avail * lev, 2)
    result["tradable"] = avail > 0
    return result



def build_sniper_signals(resp: ForecastResponse, max_leverage: Optional[int] = None) -> List[SniperSignal]:
    """Turn a Kronos forecast into concrete, executable sniper entries.

    Uses the mean-reversion anchor, the predicted target, and the p10/p90
    confidence bands to place a stop-loss beyond the predicted range and take
    profits at the mean target (TP1) and the far band (TP2). Emits both a
    "market" (enter now) and a "limit" (wait-for-pullback) idea per direction.

    ``max_leverage`` (when supplied) is the exchange's maximum leverage for the
    pair — sniper entries use it directly so the suggestion matches the highest
    leverage Bitget allows for the symbol. When it is unavailable the leverage
    falls back to a conservative confidence-scaled value.
    """
    sig = resp.signal
    if sig is None or not resp.forecast:
        return []

    anchor = float(resp.anchor_price)
    if anchor <= 0:
        return []
    direction = sig.direction
    conf = float(sig.confidence)

    lows = [b.value for b in resp.lower_band] or [c.low for c in resp.forecast]
    highs = [b.value for b in resp.upper_band] or [c.high for c in resp.forecast]
    if not lows or not highs:
        return []
    band_low = min(lows)
    band_high = max(highs)
    near_low = lows[0]
    near_high = highs[0]
    target = float(sig.target_price)
    horizon = f"{resp.pred_len}×{resp.timeframe}"

    # Use the exchange's max leverage for the pair when known; otherwise fall
    # back to a conservative confidence-scaled suggestion.
    if max_leverage and max_leverage > 0:
        leverage = int(max_leverage)
    else:
        leverage = 10 if conf >= 0.7 else (5 if conf >= 0.5 else 3)
    buf = 0.003  # 0.3% stop buffer beyond the predicted band

    signals: List[SniperSignal] = []

    def _rr(entry: float, sl: float, tp: float) -> float:
        risk = abs(entry - sl)
        return round(abs(tp - entry) / risk, 2) if risk > 0 else 0.0

    if direction == "up":
        sl = band_low * (1 - buf)
        tp1 = max(target, anchor)
        tp2 = band_high
        entries = [
            ("market", "Market entry", anchor),
            ("limit", "Limit — buy the dip", min(near_low, anchor)),
        ]
        for kind, label, entry in entries:
            if entry <= sl or tp1 <= entry:
                continue
            rr = _rr(entry, sl, tp1)
            reasons = [
                f"Kronos projects {resp.symbol} UP {sig.pct_change:+.2f}% over {horizon} "
                f"({int(conf * 100)}% path agreement).",
                f"Predicted mean target {_fmt_price(target)} sits above current {_fmt_price(anchor)} — bullish drift.",
                f"Stop {_fmt_price(sl)} is placed below the p10 floor ({_fmt_price(band_low)}), "
                f"so a break there invalidates the up-thesis.",
                f"TP1 {_fmt_price(tp1)} = model mean; TP2 {_fmt_price(tp2)} = p90 stretch. R:R ≈ {rr}.",
            ]
            if kind == "limit":
                reasons.append(
                    f"Limit at {_fmt_price(entry)} waits for a dip to the near-term p10 for a better entry."
                )
            signals.append(SniperSignal(
                id=f"{resp.symbol}-long-{kind}", side="long", order_kind=kind, label=label,
                entry=round(entry, 8), stop_loss=round(sl, 8), take_profit_1=round(tp1, 8),
                take_profit_2=round(tp2, 8), risk_reward=rr, confidence=round(conf, 3),
                leverage=leverage, reasons=reasons,
            ))

    elif direction == "down":
        sl = band_high * (1 + buf)
        tp1 = min(target, anchor)
        tp2 = band_low
        entries = [
            ("market", "Market entry", anchor),
            ("limit", "Limit — sell the bounce", max(near_high, anchor)),
        ]
        for kind, label, entry in entries:
            if entry >= sl or tp1 >= entry:
                continue
            rr = _rr(entry, sl, tp1)
            reasons = [
                f"Kronos projects {resp.symbol} DOWN {sig.pct_change:+.2f}% over {horizon} "
                f"({int(conf * 100)}% path agreement).",
                f"Predicted mean target {_fmt_price(target)} sits below current {_fmt_price(anchor)} — bearish drift.",
                f"Stop {_fmt_price(sl)} is placed above the p90 ceiling ({_fmt_price(band_high)}), "
                f"so a break there invalidates the down-thesis.",
                f"TP1 {_fmt_price(tp1)} = model mean; TP2 {_fmt_price(tp2)} = p10 stretch. R:R ≈ {rr}.",
            ]
            if kind == "limit":
                reasons.append(
                    f"Limit at {_fmt_price(entry)} waits for a bounce to the near-term p90 for a better short."
                )
            signals.append(SniperSignal(
                id=f"{resp.symbol}-short-{kind}", side="short", order_kind=kind, label=label,
                entry=round(entry, 8), stop_loss=round(sl, 8), take_profit_1=round(tp1, 8),
                take_profit_2=round(tp2, 8), risk_reward=rr, confidence=round(conf, 3),
                leverage=leverage, reasons=reasons,
            ))

    return signals


async def generate_sniper_signals(
    exchange: str, symbol: str, timeframe: str = "1h", **kwargs
) -> SniperSignalsResponse:
    """Run (cached) a forecast and derive executable sniper entries from it."""
    resp = await run_forecast_cached(exchange, symbol, timeframe, **kwargs)
    max_lev = await _max_leverage(exchange, symbol)
    signals = build_sniper_signals(resp, max_leverage=max_lev)
    note = None
    if not signals:
        if resp.signal and resp.signal.direction == "flat":
            note = (
                "Kronos sees no directional edge (flat forecast) — no high-conviction "
                "sniper entry. Wait for a clearer signal or a different timeframe."
            )
        else:
            note = resp.note or "No forecast data available to build sniper signals."
    return SniperSignalsResponse(
        exchange=resp.exchange, symbol=resp.symbol, timeframe=resp.timeframe,
        engine=resp.engine,
        anchor_price=resp.anchor_price,
        direction=(resp.signal.direction if resp.signal else "flat"),
        pct_change=(resp.signal.pct_change if resp.signal else 0.0),
        confidence=(resp.signal.confidence if resp.signal else 0.0),
        signals=signals, note=note,
    )
