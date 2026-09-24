"""The trading room's OBLIVION profiles — tuned per timeframe by backtest.

The global OBLIVION settings (``oblivion_settings``) still drive charts and the
other consumers. The room trades 2h and 15m with these overrides, found by a
grid search on the first 60% of each focused pair's history and kept only when
they held on the unseen 40% (see ``app.backtest.oblivion_backtest``):

* stops beyond ordinary noise (the engine's own tight stops were hit before
  0.5R on ~45% of trades — worse than chance);
* entry on a pullback of 0.5 ATR, not at the signal candle's close;
* a first target close to the entry (0.1R on 2h, 0.2R on 15m): once it is reached
  the stop jumps to TP1 and then trails behind each closed bar past TP3 until hit;
* the trend filter on, and on 2h VWAP plus a real higher timeframe (1d) —
  the default 1h "HTF" is *below* 2h and was never a higher-timeframe check;
* on 15m, only with OBLIVION's structural bias and only in the New York session.

Validation 2026-09-24 (focused pairs XAUUSD, CADJPY, BTCUSD, ETHUSD, costs
included; crypto from Bitget public history, 15m crypto over 7 months):
2h in-sample 93.7% / out-of-sample 94.7%, full 186 trades 94.1%;
15m 91.7% / 93.5%, full 91 trades 92.3%.

Win-rate pass 2026-09-24 (60-instrument universe, pairs approved on the first
60% of history, scored only on the unseen 40%): the whole book was 89.8% with
three losing days in ten. Trading 2h only with the daily trend and OBLIVION's
structure, 1h only with the structure, and 15m with wider stops took it to
91.5% at a 92% approval bar and 92.3% at 95% (80% of days at 90%+, worst day
-1.05R) — fewer trades, same or better R.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, Optional

from app.signals.oblivion import OblivionConfig

_SWING_CFG = {"min_risk_atr": 3.0, "max_risk_atr": 4.0, "tp1_rr": 0.1, "tp2_rr": 0.3, "tp3_rr": 1.5,
              "scalp_require_trend": True, "scalp_require_vwap": True, "signal_cooldown": 3}
_EXITS = {"entry_pullback_atr": 0.5, "entry_wait_bars": 6, "tp1_close": 0.0, "tp2_close": 0.0,
          # At TP1 the stop jumps to TP1; at TP2 (0.3R) the trail starts, 1 ATR
          # behind each closed bar, with no take-profit — it rides past TP3.
          "trail_atr": 1.0, "trail_from": 2, "floor_at_tp1": True, "hold_past_tp3": True}

ROOM_PROFILES: Dict[str, Dict[str, Any]] = {
    "4h": {
        "cfg": {**_SWING_CFG, "htf_timeframe": "1w"}, **_EXITS,
        "require_bias": False, "hours_utc": None,
        "validated": {"note": "per-pair approval decides; demo pairs CADJPY/GBPJPY/US30/GBPUSD/XAGUSD passed"},
    },
    "2h": {
        # Only with the daily EMA trend and OBLIVION's structural bias: unseen
        # win rate 86.9% → 91.4% (90% bar) and 90.0% → 94.4% (95% bar).
        "cfg": {**_SWING_CFG, "htf_timeframe": "1d"}, **_EXITS,
        "require_bias": True, "hours_utc": None, "trend_tf": "1d",
        "validated": {"out_of_sample_win_rate": 0.944, "approval_bar": 0.95,
                      "note": "60-instrument universe, pairs approved in-sample, scored on the unseen 40%"},
    },
    "1h": {
        # VWAP requirement off on 1h: +50% signals (1.8 → 2.7 a day across the
        # universe) at the same win rate (93.7% full / 94.1% unseen).
        # Only with OBLIVION's structural bias: unseen 89.5% → 91.9% at the 95% bar.
        "cfg": {**_SWING_CFG, "htf_timeframe": "4h", "scalp_require_vwap": False}, **_EXITS,
        "require_bias": True, "hours_utc": None,
        "validated": {"out_of_sample_win_rate": 0.919, "approval_bar": 0.95,
                      "note": "60-instrument universe, pairs approved in-sample, scored on the unseen 40%"},
    },
    "30m": {
        "cfg": {**_SWING_CFG, "tp1_rr": 0.2, "scalp_require_vwap": False, "htf_timeframe": "4h"}, **_EXITS,
        "trail_atr": 1.5, "require_bias": True, "hours_utc": None, "trend_tf": "4h",
        "validated": {"note": "30m/4h trend: few pairs pass (NZDCHF, NZDJPY)"},
    },
    "15m": {
        # Stops 4–5 ATR (not 3–4): unseen 85% → 94%, on few pairs — most 15m
        # pairs now fail approval and are simply not traded.
        "cfg": {**_SWING_CFG, "tp1_rr": 0.2, "scalp_require_vwap": False, "htf_timeframe": "4h",
                "min_risk_atr": 4.0, "max_risk_atr": 5.0}, **_EXITS,
        "trail_atr": 1.5,
        # 15m/4h: with OBLIVION's structural bias and the 4h EMA trend, any hour —
        # 0.4 → 1.7 trades a day across the universe at 95.4% (95.9% unseen).
        "require_bias": True, "hours_utc": None, "trend_tf": "4h",
        "validated": {"in_sample_win_rate": 0.944, "out_of_sample_win_rate": 0.959,
                      "note": "15m/4h trend, 39-instrument universe"},
    },
    "5m": {
        # 5m scalping with the 1h EMA trend: 2.1 trades a day at 95.4% (95.9% unseen).
        "cfg": {**_SWING_CFG, "tp1_rr": 0.2, "scalp_require_vwap": False, "htf_timeframe": "4h"}, **_EXITS,
        "trail_atr": 1.5, "require_bias": False, "hours_utc": None, "trend_tf": "1h",
        "validated": {"in_sample_win_rate": 0.949, "out_of_sample_win_rate": 0.959,
                      "note": "5m/1h trend, 39-instrument universe"},
    },
}

#: High-impact news: no new entries from PRE minutes before to COOLOFF after.
NEWS_PRE_MIN = 30
NEWS_COOLOFF_MIN = 120

#: The timeframes the room scans for OBLIVION signals, most important first.
ROOM_SCAN_TIMEFRAMES = ("4h", "2h", "1h", "30m", "15m", "5m")


def profile_for(timeframe: str) -> Optional[Dict[str, Any]]:
    return ROOM_PROFILES.get(str(timeframe or "").lower())


def cfg_for(timeframe: str) -> OblivionConfig:
    """The global settings with this timeframe's room overrides on top."""
    base = None
    try:
        from app.services.oblivion_settings import current_config

        base = current_config()
    except Exception:  # noqa: BLE001
        base = None
    cfg = base or OblivionConfig()
    prof = profile_for(timeframe)
    if not prof:
        return cfg
    data = {**asdict(cfg), **prof["cfg"]}
    return OblivionConfig(**{k: v for k, v in data.items() if k in OblivionConfig.__dataclass_fields__})


#: A stop closer than this fraction of price is a data artefact (flat bars with
#: ~0 ATR), not a trade: sized by risk, it becomes an enormous position that a
#: spread alone wipes out. A 5m AUDUSD backtest lost 1,185R on one such fill.
MIN_RISK_FRAC = 0.0004


def entry_plan(signal: Dict[str, Any], pullback_atr: float, rr: tuple) -> Optional[Dict[str, float]]:
    """The one entry plan both the backtest and the live room use.

    A limit ``pullback_atr`` ATRs back from the signal close toward its
    structural stop, and the TP ladder measured from that limit — so the order
    the broker holds carries exactly the levels the backtest traded.
    """
    try:
        entry, stop, atr = float(signal["entry"]), float(signal["stop_loss"]), float(signal.get("atr") or 0)
    except (KeyError, TypeError, ValueError):
        return None
    if atr <= 0:
        return None
    sgn = 1.0 if signal.get("action") == "buy" else -1.0
    limit = entry - sgn * pullback_atr * atr
    risk = (limit - stop) * sgn
    if risk <= 0 or risk < MIN_RISK_FRAC * abs(limit):
        return None
    return {"limit": round(limit, 8), "stop_loss": stop,
            "tp1": round(limit + sgn * rr[0] * risk, 8), "tp2": round(limit + sgn * rr[1] * risk, 8),
            "tp3": round(limit + sgn * rr[2] * risk, 8)}


def backtest_kwargs(timeframe: str) -> Dict[str, Any]:
    """``BacktestParams`` keywords that replay the room's profile for ``timeframe``."""
    prof = profile_for(timeframe)
    if not prof:
        return {}
    return {
        "cfg_overrides": dict(prof["cfg"]),
        "entry_pullback_atr": prof["entry_pullback_atr"], "entry_wait_bars": prof["entry_wait_bars"],
        "tp1_close": prof["tp1_close"], "tp2_close": prof["tp2_close"],
        "trail_atr": prof.get("trail_atr"), "trail_from": int(prof.get("trail_from") or 1),
        "floor_at_tp1": bool(prof.get("floor_at_tp1")),
        "news_pre_min": NEWS_PRE_MIN, "news_cooloff_min": NEWS_COOLOFF_MIN,
        "hold_past_tp3": bool(prof.get("hold_past_tp3")),
        "be_after_tp1": not prof.get("floor_at_tp1"), "trail_after_tp2": not prof.get("trail_atr"),
        "require_bias": bool(prof.get("require_bias")), "hours_utc": prof.get("hours_utc"),
        "trend_tf": prof.get("trend_tf"),
    }


def entry_filter_reason(timeframe: str, signal: Dict[str, Any], trend: Optional[int] = None) -> Optional[str]:
    """Why the profile's entry filters reject this signal, or None when it passes.

    ``trend`` is the higher timeframe's EMA 21/50 direction (+1/-1/0) when the
    profile trades only with it (``trend_tf``)."""
    import time as _time

    prof = profile_for(timeframe)
    if not prof:
        return None
    side = signal.get("action")
    if prof.get("trend_tf"):
        want = 1 if side == "buy" else -1
        if trend is None:
            return f"{prof['trend_tf']} trend unavailable"
        if trend != want:
            return f"against the {prof['trend_tf']} trend"
    if prof.get("require_bias") and signal.get("bias") is not None:
        if signal.get("bias") != (1 if side == "buy" else -1):
            return "against OBLIVION's structural bias"
    hours = prof.get("hours_utc")
    if hours and signal.get("time") is not None:
        hour = _time.gmtime(int(signal["time"])).tm_hour
        if hour not in hours:
            return f"outside the {timeframe} trading session ({min(hours):02d}:00–{max(hours) + 1:02d}:00 UTC)"
    return None
