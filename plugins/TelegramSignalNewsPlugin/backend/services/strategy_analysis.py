"""Technical-analysis layer for sniper entry optimisation.

Given a signal's symbol/direction/levels and the live price, this fetches recent
OHLCV from Bitget (via the core exchange manager) and applies several strategies
to find the best possible entry to maximise the run to take-profit:

  • Support/resistance via swing pivots (TradingView-style ta.pivotlow/high)
  • RSI to avoid chasing overbought longs / oversold shorts
  • Bollinger lower/upper as dynamic value zones
  • Buy/sell volume split to detect strong *opposite-direction* pressure, which
    flags a likely better re-entry (we deepen the limit and warn).

All core helpers are imported read-only — nothing in core is modified.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger


@dataclass(slots=True)
class StrategyAnalysis:
    ok: bool
    recommend: str            # "enter" | "wait" | "skip"
    optimized_entry: float | None = None
    support: float | None = None
    resistance: float | None = None
    rsi: float | None = None
    bb_lower: float | None = None
    bb_upper: float | None = None
    opposite_volume: bool = False
    volume_ratio: float | None = None  # opposing/total over the recent window
    note: str = ""


async def _fetch_ohlcv(symbol: str, timeframe: str = "15m", limit: int = 200) -> list[list] | None:
    """Fetch OHLCV for volume/direction confirmation.

    Normalises bare symbols and tries spot BEFORE futures on Bitget, then falls
    back to Binance/OKX/Bybit. The Bitget connector auto-converts to futures
    format (CRV/USDT:USDT), which fails for spot-only pairs — so we bypass the
    helper and call ``exchange.fetch_ohlcv`` directly with the spot symbol.
    """
    try:
        from app.exchanges.manager import exchange_manager, SupportedExchange
    except Exception:
        return None

    # Build the set of slash-symbol candidates to try
    sym_up = (symbol or "").upper().strip()
    if "/" in sym_up:
        cands = [sym_up]
    else:
        base = sym_up
        cands = []
        for quote in ("USDT", "USDC", "USD", "BTC", "ETH"):
            if base.endswith(quote) and len(base) > len(quote):
                base = base[: -len(quote)]
                break
        for quote in ("USDT", "USDC"):
            c = f"{base}/{quote}"
            if c not in cands:
                cands.append(c)

    for cand in cands:
        # ── Bitget: spot first (direct exchange call), then futures ──
        bitget = exchange_manager.get_exchange(SupportedExchange.BITGET)
        if bitget is not None:
            try:
                data = await bitget.exchange.fetch_ohlcv(cand, timeframe, limit=limit)
                if data and len(data) >= 20:
                    return data
            except Exception:
                pass
            for quote in ("USDT", "USDC"):
                if cand.endswith(f"/{quote}"):
                    try:
                        data = await bitget.exchange.fetch_ohlcv(
                            f"{cand}:{quote}", timeframe, limit=limit
                        )
                        if data and len(data) >= 20:
                            return data
                    except Exception:
                        pass

        # ── Other exchanges ──────────────────────────────────────────────
        for ex_name in ("BINANCE", "OKX", "BYBIT"):
            ex = getattr(SupportedExchange, ex_name, None)
            if ex is None:
                continue
            conn = exchange_manager.get_exchange(ex)
            if conn is None:
                continue
            try:
                data = await conn.exchange.fetch_ohlcv(cand, timeframe, limit=limit)
                if data and len(data) >= 20:
                    return data
            except Exception:
                pass

    logger.debug("Strategy OHLCV fetch failed for all candidates of {}", symbol)
    return None


def _last_valid(series) -> float | None:
    try:
        s = series.dropna()
        if len(s) == 0:
            return None
        return float(s.iloc[-1])
    except Exception:
        return None


def _nearest_pivot(pivots, *, below: float | None = None, above: float | None = None) -> float | None:
    """Return the nearest non-NaN pivot value below/above a reference."""
    try:
        vals = [float(v) for v in pivots.dropna().tolist()]
    except Exception:
        return None
    if not vals:
        return None
    if below is not None:
        candidates = [v for v in vals if v < below]
        return max(candidates) if candidates else None
    if above is not None:
        candidates = [v for v in vals if v > above]
        return min(candidates) if candidates else None
    return None


async def analyze_entry(
    *,
    symbol: str,
    direction: str,
    live_price: float,
    stop_loss: float | None,
    fallback_entry: float,
    volume_window: int = 8,
    volume_spike_mult: float = 1.5,
) -> StrategyAnalysis:
    """Compute a TA-optimised sniper entry and opposite-volume warning."""
    direction = (direction or "").lower()
    ohlcv = await _fetch_ohlcv(symbol)
    if not ohlcv or len(ohlcv) < 50:
        # No data — fall back to the naive entry, proceed.
        return StrategyAnalysis(ok=False, recommend="enter", optimized_entry=fallback_entry,
                                note="No OHLCV; using offset entry")

    try:
        from app.signals.technical import (
            ohlcv_to_dataframe,
            rsi as rsi_fn,
            bollinger_bands,
            buy_sell_volume,
            pivot_highs,
            pivot_lows,
        )

        df = ohlcv_to_dataframe(ohlcv)
        rsi_val = _last_valid(rsi_fn(df, 14))
        bb = bollinger_bands(df, 20, 2.0)
        bb_lower = _last_valid(bb["lower"])
        bb_upper = _last_valid(bb["upper"])

        lows = pivot_lows(df["low"], left=5, right=5)
        highs = pivot_highs(df["high"], left=5, right=5)
        support = _nearest_pivot(lows, below=live_price)
        resistance = _nearest_pivot(highs, above=live_price)

        # ── Opposite-direction volume over the recent window ──
        bsv = buy_sell_volume(df)
        recent_buy = float(bsv["buy_volume"].tail(volume_window).sum())
        recent_sell = float(bsv["sell_volume"].tail(volume_window).sum())
        total = recent_buy + recent_sell
        avg_vol = float(df["volume"].tail(50).mean() or 0)
        recent_vol = float(df["volume"].tail(volume_window).mean() or 0)
        volume_spiking = avg_vol > 0 and recent_vol >= avg_vol * volume_spike_mult

        opposite_volume = False
        volume_ratio = None
        if total > 0:
            if direction == "long":
                volume_ratio = recent_sell / total
                opposite_volume = volume_ratio >= 0.6 and volume_spiking
            else:
                volume_ratio = recent_buy / total
                opposite_volume = volume_ratio >= 0.6 and volume_spiking
    except Exception as exc:  # noqa: BLE001
        logger.debug("Strategy compute failed for {}: {}", symbol, exc)
        return StrategyAnalysis(ok=False, recommend="enter", optimized_entry=fallback_entry,
                                note="TA error; using offset entry")

    notes: list[str] = []
    recommend = "enter"

    if direction == "long":
        # Best entry: a discount toward the nearest support / BB lower, but never
        # below the stop. Use the *highest* sensible value among the candidates so
        # the order is likely to fill on a normal pullback.
        candidates = [c for c in (support, bb_lower, fallback_entry) if c and c < live_price]
        optimized = max(candidates) if candidates else fallback_entry
        if stop_loss:
            optimized = max(optimized, stop_loss * 1.001)  # keep above SL
        optimized = min(optimized, live_price)

        if support:
            notes.append(f"support {support:.6g}")
        if rsi_val is not None:
            notes.append(f"RSI {rsi_val:.0f}")
            if rsi_val >= 75:
                recommend = "wait"
                notes.append("overbought — wait for pullback")
        if opposite_volume:
            recommend = "wait"
            optimized = min(optimized, live_price * 0.997)  # deepen on opposite pressure
            notes.append("⚠ high SELL volume — better re-entry likely lower")
    elif direction == "short":
        candidates = [c for c in (resistance, bb_upper, fallback_entry) if c and c > live_price]
        optimized = min(candidates) if candidates else fallback_entry
        if stop_loss:
            optimized = min(optimized, stop_loss * 0.999)  # keep below SL
        optimized = max(optimized, live_price)

        if resistance:
            notes.append(f"resistance {resistance:.6g}")
        if rsi_val is not None:
            notes.append(f"RSI {rsi_val:.0f}")
            if rsi_val <= 25:
                recommend = "wait"
                notes.append("oversold — wait for bounce")
        if opposite_volume:
            recommend = "wait"
            optimized = max(optimized, live_price * 1.003)
            notes.append("⚠ high BUY volume — better re-entry likely higher")
    else:
        return StrategyAnalysis(ok=False, recommend="skip", note="Unknown direction")

    return StrategyAnalysis(
        ok=True,
        recommend=recommend,
        optimized_entry=round(optimized, 10),
        support=support,
        resistance=resistance,
        rsi=round(rsi_val, 1) if rsi_val is not None else None,
        bb_lower=bb_lower,
        bb_upper=bb_upper,
        opposite_volume=opposite_volume,
        volume_ratio=round(volume_ratio, 2) if volume_ratio is not None else None,
        note=", ".join(notes),
    )


async def volume_snapshot(symbol: str, direction: str, *, window: int = 8, spike_mult: float = 1.5) -> dict:
    """Lightweight live volume read for the Volume Monitor (no RSI/BB/pivots).

    Returns current price + buy/sell pressure split and whether volume is
    spiking against the signal's direction.
    """
    direction = (direction or "").lower()
    ohlcv = await _fetch_ohlcv(symbol)
    if not ohlcv or len(ohlcv) < 20:
        return {"available": False}
    try:
        from app.signals.technical import ohlcv_to_dataframe, buy_sell_volume

        df = ohlcv_to_dataframe(ohlcv)
        bsv = buy_sell_volume(df)
        recent_buy = float(bsv["buy_volume"].tail(window).sum())
        recent_sell = float(bsv["sell_volume"].tail(window).sum())
        total = recent_buy + recent_sell
        buy_pct = (recent_buy / total) if total > 0 else 0.5
        avg_vol = float(df["volume"].tail(50).mean() or 0)
        recent_vol = float(df["volume"].tail(window).mean() or 0)
        spike = avg_vol > 0 and recent_vol >= avg_vol * spike_mult
        opposing = (recent_sell / total) if direction == "long" else (recent_buy / total)
        opposing = opposing if total > 0 else 0.0
        opposite_volume = opposing >= 0.6 and spike
        return {
            "available": True,
            "current_price": float(df["close"].iloc[-1]),
            "buy_pct": round(buy_pct * 100, 1),
            "sell_pct": round((1 - buy_pct) * 100, 1),
            "opposing_pct": round(opposing * 100, 1),
            "volume_spike": spike,
            "opposite_volume": opposite_volume,
            "vol_ratio": round(recent_vol / avg_vol, 2) if avg_vol > 0 else None,
        }
    except Exception as exc:  # noqa: BLE001
        logger.debug("volume_snapshot failed for {}: {}", symbol, exc)
        return {"available": False}

