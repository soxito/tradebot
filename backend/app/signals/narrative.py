"""Turn computed indicators into the room's spoken-analysis voice.

Every sentence here is chosen by a real number: the structure read comes from
the trend, the momentum read from MACD and RSI, the volatility read from the
Bollinger bands, volume and Stochastic, and the bias tally from six binary
votes over those same indicators. Nothing is phrased more confidently than the
data supports, and an indicator that could not be computed is left out rather
than filled in — a missing volume feed must read as silence, not as "volume is
drying up".

The tally is deliberately six votes, so "Buy 4/6 | Sell 2/6" always sums and
the reader can see exactly how close the call was.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd
from loguru import logger

from app.signals.technical import (
    bollinger_bands, ema, macd, ohlcv_to_dataframe, rsi, sma, stochastic_rsi,
)

#: Tickers worth naming in full. Anything absent is shown as its own symbol,
#: which reads fine ("The 4H timeframe for XAUUSD…").
_DISPLAY_NAMES = {
    "BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana", "XRP": "XRP",
    "ADA": "Cardano", "DOGE": "Dogecoin", "BNB": "BNB", "AVAX": "Avalanche",
    "LINK": "Chainlink", "MATIC": "Polygon", "DOT": "Polkadot",
    "XAU": "Gold", "XAG": "Silver", "USOIL": "Oil", "NAS100": "the Nasdaq",
    "US500": "the S&P 500", "US30": "the Dow",
}

_TIMEFRAME_NAMES = {
    "1m": "1-minute", "5m": "5-minute", "15m": "15-minute", "30m": "30-minute",
    "1h": "1H", "2h": "2H", "4h": "4H", "6h": "6H", "12h": "12H",
    "1d": "Daily", "d1": "Daily", "1w": "Weekly", "w1": "Weekly",
}


def display_name(symbol: str) -> str:
    """A human name for ``symbol`` when one is known, else the symbol itself."""
    sym = (symbol or "").upper()
    base = sym.split("/")[0].replace("USDT", "").replace("USD", "") or sym
    return _DISPLAY_NAMES.get(base, sym)


def _fmt_price(value: float) -> str:
    a = abs(value)
    if a >= 1000:
        return f"${value:,.2f}"
    if a >= 1:
        return f"${value:.3f}".rstrip("0").rstrip(".")
    return f"${value:.6f}".rstrip("0").rstrip(".")


def _last(series: pd.Series) -> Optional[float]:
    """The final value of ``series``, or None when it is absent or NaN."""
    if series is None or len(series) == 0:
        return None
    value = series.iloc[-1]
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value == value else None  # NaN-safe


def indicator_snapshot(ohlcv: List[List]) -> Dict[str, Any]:
    """Latest MACD/RSI/Bollinger/Stochastic/volume readings from ``ohlcv``.

    Keys are omitted, never zero-filled, when a series is too short to compute
    — the caller distinguishes "flat" from "unknown" on that basis.
    """
    out: Dict[str, Any] = {}
    try:
        df = ohlcv_to_dataframe(ohlcv)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Narrative] could not read candles: {}", exc)
        return out

    if len(df) < 20:
        return out

    close = df["close"]
    out["close"] = float(close.iloc[-1])

    if (value := _last(rsi(df, 14))) is not None:
        out["rsi"] = value
    if (value := _last(macd(df)["histogram"])) is not None:
        out["macd_hist"] = value
        # Scaled against price, so "near zero" means the same thing on gold at
        # 4,000 as on a token at 0.02.
        out["macd_hist_pct"] = abs(value) / out["close"] * 100 if out["close"] else 0.0
    if (value := _last(bollinger_bands(df)["pct_b"])) is not None:
        out["pct_b"] = value
    if (value := _last(stochastic_rsi(df))) is not None:
        out["stoch"] = value
    if (value := _last(ema(close, 50))) is not None:
        out["ema50"] = value
    if (value := _last(ema(close, 200))) is not None and len(df) >= 200:
        out["ema200"] = value

    vol_ma = _last(sma(df["volume"], 5))
    latest_vol = float(df["volume"].iloc[-1])
    # A feed with no volume at all (many FX/metals sources) must not read as
    # "volume is drying up" — that would be an observation about our data
    # source dressed up as one about the market.
    if vol_ma and vol_ma > 0 and latest_vol > 0:
        out["vol_ratio"] = latest_vol / vol_ma

    return out


def bias_votes(snap: Dict[str, Any]) -> Dict[str, Any]:
    """Binary votes over the snapshot, tallied as ``buy``/``sell`` out of ``total``.

    An indicator with no data abstains and leaves the tally, shrinking the
    denominator rather than being counted against the market: a series too
    short for EMA200 must never read as a sell vote, or a thin history would
    print as bearish conviction nobody actually cast.
    """
    close = snap.get("close")
    votes: List[tuple[str, Optional[bool]]] = [
        ("RSI", None if (v := snap.get("rsi")) is None else v > 50),
        ("MACD", None if (v := snap.get("macd_hist")) is None else v > 0),
        ("Bollinger", None if (v := snap.get("pct_b")) is None else v > 0.5),
        ("Stochastic", None if (v := snap.get("stoch")) is None else v > 50),
        (
            "EMA trend",
            None if (snap.get("ema50") is None or snap.get("ema200") is None)
            else snap["ema50"] > snap["ema200"],
        ),
        (
            "Price vs EMA50",
            None if (snap.get("ema50") is None or close is None)
            else close > snap["ema50"],
        ),
    ]

    buy = sum(1 for _, v in votes if v is True)
    sell = sum(1 for _, v in votes if v is False)
    total = buy + sell

    if total == 0:
        label, icon = "Unknown", "⚪"
    elif buy / total >= 0.66:
        label, icon = "Bullish", "🟢"
    elif sell / total >= 0.66:
        label, icon = "Bearish", "🔴"
    else:
        label, icon = "Neutral", "🟡"
    return {"buy": buy, "sell": sell, "total": total, "label": label, "icon": icon,
            "votes": [(name, v) for name, v in votes]}


def narrative_summary(
    ohlcv: List[List],
    *,
    symbol: str,
    timeframe: str,
    trend: str,
    swing_high: Optional[float] = None,
    swing_low: Optional[float] = None,
) -> str:
    """The full spoken-analysis block, or "" when there is too little data.

    ``trend`` and the swing levels are supplied by the caller so this reads the
    same structure the caller's own trade proposal was built from, rather than
    computing a second opinion that could contradict it on the same screen.
    """
    snap = indicator_snapshot(ohlcv)
    if not snap:
        return ""

    name = display_name(symbol)
    tf = _TIMEFRAME_NAMES.get((timeframe or "").lower(), (timeframe or "").upper())
    paragraphs: List[str] = []

    # ── Structure ────────────────────────────────────────────────────────────
    if trend == "uptrend":
        structure = (
            f"👁‍🗨 The {tf} timeframe for {name} is holding a bullish structure. "
            "Price is carving higher lows, and each pullback is being bought "
            "before it can break the trend."
        )
    elif trend == "downtrend":
        structure = (
            f"👁‍🗨 The {tf} timeframe for {name} remains under bearish control. "
            "Price is printing lower highs, and rallies are being sold into "
            "rather than sustained."
        )
    else:
        structure = (
            f"👁‍🗨 The {tf} timeframe for {name} is currently caught in a "
            "consolidation phase. Price is chopping between dynamic levels, and "
            "we are waiting for a clear break of structure."
        )
    paragraphs.append(structure)

    # ── Momentum ─────────────────────────────────────────────────────────────
    momentum: List[str] = []
    hist_pct = snap.get("macd_hist_pct")
    hist = snap.get("macd_hist")
    if hist is not None:
        if hist_pct is not None and hist_pct < 0.05:
            momentum.append(
                "⚖️ Momentum indicators are flattening out. MACD is hovering "
                "around the zero line"
            )
        elif hist > 0:
            momentum.append("⚖️ Momentum is turning up. MACD is building above the zero line")
        else:
            momentum.append("⚖️ Momentum is rolling over. MACD is sitting below the zero line")

    if (rsi_val := snap.get("rsi")) is not None:
        if rsi_val > 70:
            rsi_read = f"RSI is stretched at {rsi_val:.1f}, warning of overbought conditions"
        elif rsi_val < 30:
            rsi_read = f"RSI is depressed at {rsi_val:.1f}, flagging oversold conditions"
        else:
            rsi_read = (
                f"RSI is sitting at {rsi_val:.1f}, reflecting indecision among "
                "buyers and sellers"
            )
        momentum.append(rsi_read if momentum else f"⚖️ {rsi_read[0].upper()}{rsi_read[1:]}")
    if momentum:
        paragraphs.append(", and ".join(momentum) + ".")

    # ── Volatility and participation ─────────────────────────────────────────
    volatility: List[str] = []
    if (pct_b := snap.get("pct_b")) is not None:
        if pct_b > 1:
            volatility.append(
                "From a volatility standpoint, price is pressing above the upper "
                "Bollinger Band."
            )
        elif pct_b < 0:
            volatility.append(
                "From a volatility standpoint, price is pressing below the lower "
                "Bollinger Band."
            )
        else:
            volatility.append(
                "From a volatility standpoint, price is trading within the "
                "Bollinger Bands."
            )
    if (vol_ratio := snap.get("vol_ratio")) is not None:
        if vol_ratio < 0.7:
            volatility.append(
                f"Volume is currently drying up ({vol_ratio:.1f}x average), so keep "
                "an eye out for a sudden volatility spike."
            )
        elif vol_ratio > 1.3:
            volatility.append(
                f"Volume is expanding ({vol_ratio:.1f}x average), so this move has "
                "real participation behind it."
            )
        else:
            volatility.append(f"Volume is in line with average ({vol_ratio:.1f}x).")
    if (stoch := snap.get("stoch")) is not None:
        state = "overbought" if stoch > 80 else "oversold" if stoch < 20 else "neutral"
        volatility.append(f"The Stochastic is at {stoch:.1f} ({state}).")
    if volatility:
        paragraphs.append(" ".join(volatility))

    # ── Key levels ───────────────────────────────────────────────────────────
    if isinstance(swing_high, (int, float)) and isinstance(swing_low, (int, float)):
        paragraphs.append(
            "🔔 Key Levels:\n"
            f"Break above {_fmt_price(float(swing_high))} ignites upside. "
            f"Losing {_fmt_price(float(swing_low))} opens doors for correction."
        )

    # ── Bias ─────────────────────────────────────────────────────────────────
    bias = bias_votes(snap)
    if bias["total"]:
        paragraphs.append(
            f"{bias['icon']} Bias: {bias['label']} "
            f"(Buy {bias['buy']}/{bias['total']} | Sell {bias['sell']}/{bias['total']})"
        )

    return "\n\n".join(paragraphs)
