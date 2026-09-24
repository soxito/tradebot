"""OBLIVION MASTER v5 — Scalper + Sniper. Python port of the Pine v6 strategy.

One bar-by-bar pass over OHLCV that mirrors the Pine state machine:

* fast trend  EMA 9 / 21 / 50 stack, plus the v4 major-trend filter (EMA 200)
* VWAP        day-anchored, skipped when the feed carries no volume
* volatility  ATR, minimum candle movement gate
* candle      body ratio, displacement, rejection wicks
* volume      high volume (×1.15), very high (×1.75, v4), expansion
* structure   micro (3) and major (8) pivots, BOS / CHOCH, market bias,
              HH / HL / LH / LL labels on the major swing (v4)
* dealing range premium / equilibrium / discount over ``pd_lookback``
* FVG         formation (+1) and unfilled-gap retest (+1, v4), mitigation
* order block found on micro breaks, retired once price closes through it
* liquidity   sweeps of the previous ``liquidity_lookback`` high / low
* OTE         0.607 / 0.705 / 0.786 of the major swing; fib 0.382–0.618 (v4)
* HTF         EMA fast / slow on a higher timeframe, last *closed* bar
* sessions / news window
* two scores  scalp (min 6) and sniper (min 9 + hard requirements),
              trading mode SCALPING / SNIPER / HYBRID
* gates       confirmed bar, cooldown, duplicate avoidance, session, news
* plan        structural SL ± ATR buffer, risk clamped to [min, max] ATR,
              TP1/2/3 ladder, room-to-TP3 check
* outcomes    every signal walked forward to TP1 / TP2 / TP3 / SL

Everything downstream (room context, MT5 bots, Kronos, Telegram image, chart
overlays) consumes the dict this module returns; the keys the v4 consumers
read (``score.bull/bear/required``, ``signal``, ``last_signal``,
``signals_history``, ``dashboard``, ``session``, ``news``, ``fib``,
``structure``, ``fvg``, ``order_blocks``, ``liquidity``, ``summary``) are kept.

Configuration is global: :func:`current_config` (the settings service's
cached copy of the DB row) unless a caller passes one. Never raises on bad
input — too few bars returns an ``{"error": ...}`` payload with a neutral
``signal``.
"""
from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from app.signals.technical import atr as _atr, ema as _ema, ohlcv_to_dataframe, pivot_highs, pivot_lows

MIN_BARS = 60
ENGINE_VERSION = "5.0"
TRADE_MODES = ("SCALPING", "SNIPER", "HYBRID")


# ─────────────────────────────────────────────────────────────────────────────
# Config — defaults are the user's tuned inputs (screenshots, 2026-09-22)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class OblivionConfig:
    # 01 master
    trade_mode: str = "HYBRID"
    confirmed_only: bool = True
    allow_long: bool = True
    allow_short: bool = True
    avoid_duplicates: bool = True
    signal_cooldown: int = 3
    # 02 scalping engine
    scalp_min_score: int = 6
    scalp_require_trend: bool = False
    scalp_require_vwap: bool = False
    # 03 sniper engine
    sniper_min_score: int = 9
    sniper_require_htf: bool = True
    sniper_require_sweep: bool = True
    sniper_require_zone: bool = True
    sniper_require_major_trend: bool = True     # v4 carry-over (EMA 200)
    # 04 fast trend
    fast_ema: int = 9
    medium_ema: int = 21
    slow_ema: int = 50
    show_ema: bool = False
    use_major_trend: bool = True                # v4 carry-over
    major_ema: int = 200
    # 05 vwap
    use_vwap: bool = True
    show_vwap: bool = False
    # 06 volatility
    atr_length: int = 14
    movement_min_atr: float = 0.2
    # 08 volume
    use_volume: bool = True
    volume_length: int = 20
    volume_multiplier: float = 1.15
    very_high_multiplier: float = 1.75          # v4 carry-over
    # 09 market structure
    micro_len: int = 3
    major_len: int = 8
    show_bos: bool = True
    # 10 premium / discount
    show_pd: bool = True
    pd_lookback: int = 92
    # 11 fair value gap
    show_fvg: bool = True
    fvg_min_atr: float = 0.2
    fvg_extension: int = 25
    score_fvg_retest: bool = True               # v4 carry-over
    # 12 order block
    show_ob: bool = True
    ob_lookback: int = 9
    ob_extension: int = 35
    # 13 liquidity
    liquidity_lookback: int = 8
    sweep_recency: int = 3                      # sniper: sweep within N bars
    # 14 OTE
    use_ote: bool = True
    show_ote: bool = False
    ote_618: float = 0.607
    ote_705: float = 0.705
    ote_786: float = 0.786
    use_fib_zone: bool = True                   # v4 carry-over (0.382–0.618)
    # 15 HTF confirmation
    use_htf: bool = True
    htf_timeframe: str = "1h"
    htf_fast: int = 19
    htf_slow: int = 45
    # 16 sessions
    require_session: bool = True
    score_session: bool = True                  # v4 carry-over (+1)
    asia: str = "0000-0900"
    london: str = "0700-1600"
    new_york: str = "1300-2200"
    session_tz: str = "Etc/UTC"
    # 17 news
    news_enabled: bool = False
    news_window: str = "1330-1430"
    news_tz: str = "America/New_York"
    # 18 trade management
    sl_atr_buffer: float = 0.25
    min_risk_atr: float = 0.25
    max_risk_atr: float = 2.5
    tp1_rr: float = 0.5
    tp2_rr: float = 1.0
    tp3_rr: float = 1.5
    require_room_tp3: bool = True
    # 19 visuals
    show_levels: bool = True
    level_extension: int = 20
    show_labels: bool = True
    show_sessions: bool = True
    show_dashboard: bool = True
    # 20 outcomes
    show_outcomes: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    # v4 names some consumers still read
    @property
    def minimum_score(self) -> int:
        return self.scalp_min_score

    @property
    def block_news(self) -> bool:
        return self.news_enabled


_KNOWN = {f.name for f in fields(OblivionConfig)}
_TYPES = {f.name: str(f.type) for f in fields(OblivionConfig)}


def _coerce(name: str, value: Any) -> Any:
    """Coerce a settings value to the dataclass field's type; None on failure."""
    t = _TYPES.get(name, "str")
    try:
        if "bool" in t:
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if "int" in t:
            return int(round(float(value)))
        if "float" in t:
            return float(value)
        s = str(value).strip()
        if name == "trade_mode":
            # None (not a silent fall back to HYBRID): the settings validator
            # must be able to reject a typo, and config_from_dict skips None,
            # which leaves the base value in place.
            s = s.upper()
            return s if s in TRADE_MODES else None
        return s
    except (TypeError, ValueError):
        return None


def oblivion_config_from_env() -> OblivionConfig:
    """Dataclass defaults with the ``OBLIVION_*`` env knobs layered on top.

    This is the bottom layer; the DB-backed global settings sit above it (see
    :func:`current_config`).
    """
    cfg = OblivionConfig()
    env_map = {
        "OBLIVION_MIN_SCORE": "scalp_min_score",
        "OBLIVION_SCALP_MIN_SCORE": "scalp_min_score",
        "OBLIVION_SNIPER_MIN_SCORE": "sniper_min_score",
        "OBLIVION_TRADE_MODE": "trade_mode",
        "OBLIVION_NEWS_WINDOW": "news_window",
        "OBLIVION_NEWS_TZ": "news_tz",
        "OBLIVION_BLOCK_NEWS": "news_enabled",
        "OBLIVION_NEWS_ENABLED": "news_enabled",
        "OBLIVION_SESSION_TZ": "session_tz",
        "OBLIVION_HTF": "htf_timeframe",
    }
    for env, key in env_map.items():
        raw = os.getenv(env)
        if raw:
            val = _coerce(key, raw)
            if val is not None:
                setattr(cfg, key, val)
    return cfg


def current_config() -> OblivionConfig:
    """The global settings (DB row, cached by the settings service).

    Falls back to env + defaults when the service is unavailable (tests,
    standalone scripts), so the engine always has a config.
    """
    try:
        from app.services.oblivion_settings import current_config as _svc

        cfg = _svc()
        if cfg is not None:
            return cfg
    except Exception:  # noqa: BLE001 — settings must never break the engine
        pass
    return oblivion_config_from_env()


def config_from_dict(data: Optional[Dict[str, Any]], base: Optional[OblivionConfig] = None) -> OblivionConfig:
    """Layer a partial dict over ``base`` (default: the global config)."""
    cfg = OblivionConfig(**asdict(base)) if base is not None else current_config()
    if not data:
        return cfg
    for k, v in data.items():
        if k in _KNOWN and v is not None:
            val = _coerce(k, v)
            if val is not None:
                setattr(cfg, k, val)
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# Time helpers
# ─────────────────────────────────────────────────────────────────────────────

_TF_UNIT = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def timeframe_seconds(tf: str) -> int:
    """``15m`` / ``M15`` / ``1h`` / ``H1`` / ``60`` / ``1d`` / ``D1`` / ``1w`` → seconds."""
    if not tf:
        return 3600
    low = str(tf).strip().lower()
    try:
        if low.isdigit():                                      # Pine "60" = minutes
            return int(low) * 60
        if low[0] in _TF_UNIT and low[1:].isdigit():          # M15, H1, D1, W1
            return int(low[1:]) * _TF_UNIT[low[0]]
        if low[-1] in _TF_UNIT and low[:-1].isdigit():        # 15m, 1h, 1d, 1w
            return int(low[:-1]) * _TF_UNIT[low[-1]]
        if low in ("1mn", "mn1", "1mo"):
            return 30 * 86400
    except Exception:  # noqa: BLE001
        pass
    return 3600


def _parse_window(window: str) -> Tuple[int, int]:
    """``"1330-1430"`` → (810, 870) minutes-of-day."""
    try:
        a, b = window.split("-")
        return int(a[:2]) * 60 + int(a[2:4]), int(b[:2]) * 60 + int(b[2:4])
    except Exception:  # noqa: BLE001
        return 0, 0


def _in_window(minute_of_day: int, window: str) -> bool:
    start, end = _parse_window(window)
    if start == end:
        return False
    if start < end:
        return start <= minute_of_day < end
    return minute_of_day >= start or minute_of_day < end   # overnight window


def _minute_of_day(ts_ms: int, tz: str) -> int:
    try:
        zone = ZoneInfo(tz)
    except Exception:  # noqa: BLE001
        zone = timezone.utc
    dt = datetime.fromtimestamp(int(ts_ms) / 1000, tz=zone)
    return dt.hour * 60 + dt.minute


def session_state(ts_ms: int, cfg: OblivionConfig) -> Dict[str, Any]:
    """Which sessions a bar's open time falls in, named like the dashboard."""
    mod = _minute_of_day(ts_ms, cfg.session_tz)
    asia = _in_window(mod, cfg.asia)
    london = _in_window(mod, cfg.london)
    ny = _in_window(mod, cfg.new_york)
    overlap = london and ny
    name = "LONDON + NY" if overlap else "NEW YORK" if ny else "LONDON" if london else "ASIA" if asia else "OFF"
    return {"name": name, "asia": asia, "london": london, "new_york": ny, "overlap": overlap,
            "active": asia or london or ny}


def is_news_window(ts_ms: int, cfg: Optional[OblivionConfig] = None) -> bool:
    cfg = cfg or current_config()
    if not cfg.news_enabled:
        return False
    return _in_window(_minute_of_day(ts_ms, cfg.news_tz), cfg.news_window)


def is_news_blocked(ts_ms: int, cfg: Optional[OblivionConfig] = None) -> bool:
    """True when new entries must be refused (news blackout enabled + in window)."""
    cfg = cfg or current_config()
    return bool(cfg.news_enabled and is_news_window(ts_ms, cfg))


# ─────────────────────────────────────────────────────────────────────────────
# Input coercion
# ─────────────────────────────────────────────────────────────────────────────

def _to_dataframe(rows_or_df: Union[pd.DataFrame, Sequence[Any]]) -> pd.DataFrame:
    if isinstance(rows_or_df, pd.DataFrame):
        df = rows_or_df
        if "timestamp" not in df.columns:
            raise ValueError("DataFrame needs a timestamp column")
        return df.reset_index(drop=True)
    rows: List[List[float]] = []
    for r in rows_or_df or []:
        if isinstance(r, dict):
            t = r.get("timestamp", r.get("time", r.get("t")))
            o, h, l, c = r.get("open"), r.get("high"), r.get("low"), r.get("close")
            v = r.get("volume", 0.0)
        else:
            if len(r) < 5:
                continue
            t, o, h, l, c = r[0], r[1], r[2], r[3], r[4]
            v = r[5] if len(r) > 5 else 0.0
        if t is None or o is None or c is None:
            continue
        t = float(t)
        if t < 1e11:  # seconds → ms
            t *= 1000.0
        rows.append([int(t), float(o), float(h), float(l), float(c), float(v or 0.0)])
    return ohlcv_to_dataframe(rows)


def _ts_ms(df: pd.DataFrame) -> np.ndarray:
    ts = df["timestamp"]
    if str(ts.dtype).startswith("datetime64"):
        return (pd.to_datetime(ts, utc=True).astype("int64") // 1_000_000).to_numpy()
    vals = ts.to_numpy(dtype=float)
    if len(vals) and vals[0] < 1e11:
        vals = vals * 1000.0
    return vals.astype(np.int64)


def _f(x: Any, nd: int = 6) -> Optional[float]:
    if x is None:
        return None
    try:
        xf = float(x)
    except Exception:  # noqa: BLE001
        return None
    if math.isnan(xf) or math.isinf(xf):
        return None
    return round(xf, nd)


def candles_to_rows(candles: Sequence[Any]) -> List[List[float]]:
    """MT5-plugin ``Candle`` objects (time in seconds) → ccxt rows in ms."""
    rows: List[List[float]] = []
    for cd in candles:
        t = getattr(cd, "time", None)
        if t is None:
            continue
        t = float(t)
        if t < 1e11:
            t *= 1000.0
        rows.append([int(t), float(cd.open), float(cd.high), float(cd.low), float(cd.close),
                     float(getattr(cd, "volume", 0.0) or 0.0)])
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# HTF helper
# ─────────────────────────────────────────────────────────────────────────────

def _htf_series(ts: np.ndarray, c: np.ndarray, base_sec: int, htf_sec: int, fast: int, slow: int,
                htf_rows: Optional[Sequence[Any]] = None) -> Dict[str, np.ndarray]:
    """Per-base-bar HTF close / fast EMA / slow EMA of the last *closed* HTF bar.

    Mirrors ``request.security(..., close[1] / ema[1], lookahead_on)``: on every
    chart bar you see the previous completed higher-timeframe bar. Real HTF
    rows are used when supplied; otherwise the base series is bucketed into
    HTF candles. When the base timeframe is not finer than the HTF, the base
    series (shifted one bar) is its own HTF.
    """
    n = len(ts)
    out_c = np.full(n, np.nan)
    out_f = np.full(n, np.nan)
    out_s = np.full(n, np.nan)

    if htf_rows:
        hdf = _to_dataframe(htf_rows)
        hts = _ts_ms(hdf)
        hc = hdf["close"].to_numpy(dtype=float)
        hf = _ema(hdf["close"], fast).to_numpy(dtype=float)
        hs = _ema(hdf["close"], slow).to_numpy(dtype=float)
        j = 0
        for i in range(n):
            while j + 1 < len(hts) and hts[j + 1] + htf_sec * 1000 <= ts[i]:
                j += 1
            if hts[j] + htf_sec * 1000 <= ts[i]:
                out_c[i], out_f[i], out_s[i] = hc[j], hf[j], hs[j]
        return {"close": out_c, "fast": out_f, "slow": out_s}

    if base_sec >= htf_sec:
        cs = pd.Series(c)
        f = _ema(cs, fast).to_numpy(dtype=float)
        s = _ema(cs, slow).to_numpy(dtype=float)
        out_c[1:], out_f[1:], out_s[1:] = c[:-1], f[:-1], s[:-1]
        return {"close": out_c, "fast": out_f, "slow": out_s}

    bucket = (ts // (htf_sec * 1000)).astype(np.int64)
    closes: List[float] = []
    idx_of_bar = np.zeros(n, dtype=np.int64)
    last_b = None
    for i in range(n):
        if bucket[i] != last_b:
            closes.append(c[i])
            last_b = bucket[i]
        else:
            closes[-1] = c[i]
        idx_of_bar[i] = len(closes) - 1
    cs = pd.Series(closes)
    f = _ema(cs, fast).to_numpy(dtype=float)
    s = _ema(cs, slow).to_numpy(dtype=float)
    carr = np.asarray(closes, dtype=float)
    for i in range(n):
        k = idx_of_bar[i] - 1
        if k >= 0:
            out_c[i], out_f[i], out_s[i] = carr[k], f[k], s[k]
    return {"close": out_c, "fast": out_f, "slow": out_s}


# ─────────────────────────────────────────────────────────────────────────────
# Engine
# ─────────────────────────────────────────────────────────────────────────────

def _mode_min(cfg: OblivionConfig) -> int:
    return cfg.sniper_min_score if cfg.trade_mode == "SNIPER" else cfg.scalp_min_score


def _empty(timeframe: str, cfg: OblivionConfig, error: str) -> Dict[str, Any]:
    req = _mode_min(cfg)
    return {
        "engine": "oblivion", "version": ENGINE_VERSION, "mode": cfg.trade_mode,
        "timeframe": timeframe, "bars": 0, "error": error,
        "score": {"buy": 0, "sell": 0, "bull": 0, "bear": 0, "required": req,
                  "scalp_min": cfg.scalp_min_score, "sniper_min": cfg.sniper_min_score,
                  "components": {"buy": {}, "sell": {}, "bull": {}, "bear": {}}},
        "market_bias": 0, "bias_label": "NEUTRAL",
        "news": {"enabled": cfg.news_enabled, "in_window": False, "blocked": False,
                 "window": cfg.news_window, "tz": cfg.news_tz},
        "signal": {"action": "wait", "kind": None, "new": False, "confirmed": False, "score": 0},
        "last_signal": None, "signals_history": [], "rejected": [],
        "outcomes": {"count": 0, "tp1": 0, "tp2": 0, "tp3": 0, "sl": 0, "open": 0, "win_rate": None, "avg_r": None},
        "summary": f"OBLIVION {timeframe}: {error}", "config": cfg.to_dict(),
    }


def analyze_oblivion(
    rows_or_df: Union[pd.DataFrame, Sequence[Any]],
    timeframe: str = "1h",
    cfg: Optional[OblivionConfig] = None,
    *,
    last_bar_forming: bool = True,
    htf_rows: Optional[Sequence[Any]] = None,
    full_history: bool = False,
) -> Dict[str, Any]:
    """Run OBLIVION MASTER v5 over a candle series.

    ``rows_or_df`` — ccxt rows ``[[ts_ms, o, h, l, c, v], …]`` (seconds accepted),
    provider dicts, or an ``ohlcv_to_dataframe`` frame. ``htf_rows`` — optional
    real higher-timeframe candles; otherwise the base series is resampled.

    ``last_bar_forming`` — the final row is the live, unclosed candle. Its
    setup is reported but, like ``barstate.isconfirmed`` in Pine, it does not
    commit a signal when ``confirmed_only`` is on.
    """
    cfg = cfg or current_config()
    try:
        df = _to_dataframe(rows_or_df)
    except Exception as exc:  # noqa: BLE001
        return _empty(timeframe, cfg, f"bad candles: {exc}")
    n = len(df)
    if n < MIN_BARS:
        return _empty(timeframe, cfg, f"need at least {MIN_BARS} candles, got {n}")

    ts = _ts_ms(df)
    o = df["open"].to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    l = df["low"].to_numpy(dtype=float)
    c = df["close"].to_numpy(dtype=float)
    v = df["volume"].to_numpy(dtype=float)
    has_volume_series = bool(np.nansum(v) > 0)
    tf_sec = timeframe_seconds(timeframe)
    htf_sec = timeframe_seconds(cfg.htf_timeframe)

    # ── vectorised indicators ────────────────────────────────────────────────
    atr_s = _atr(df, cfg.atr_length).to_numpy(dtype=float)
    ema_f = _ema(df["close"], cfg.fast_ema).to_numpy(dtype=float)
    ema_m = _ema(df["close"], cfg.medium_ema).to_numpy(dtype=float)
    ema_s = _ema(df["close"], cfg.slow_ema).to_numpy(dtype=float)
    ema_major = _ema(df["close"], cfg.major_ema).to_numpy(dtype=float)
    vol_avg = pd.Series(v).rolling(cfg.volume_length, min_periods=cfg.volume_length).mean().to_numpy(dtype=float)
    ph_micro = pivot_highs(df["high"], cfg.micro_len, cfg.micro_len).to_numpy(dtype=float)
    pl_micro = pivot_lows(df["low"], cfg.micro_len, cfg.micro_len).to_numpy(dtype=float)
    ph_major = pivot_highs(df["high"], cfg.major_len, cfg.major_len).to_numpy(dtype=float)
    pl_major = pivot_lows(df["low"], cfg.major_len, cfg.major_len).to_numpy(dtype=float)
    range_hi = pd.Series(h).rolling(cfg.pd_lookback, min_periods=1).max().to_numpy(dtype=float)
    range_lo = pd.Series(l).rolling(cfg.pd_lookback, min_periods=1).min().to_numpy(dtype=float)
    htf = _htf_series(ts, c, tf_sec, htf_sec, cfg.htf_fast, cfg.htf_slow, htf_rows) if cfg.use_htf else None

    vwap = np.full(n, np.nan)
    if cfg.use_vwap and has_volume_series:
        day = ts // 86_400_000
        cum_pv = cum_v = 0.0
        last_day = None
        for i in range(n):
            if day[i] != last_day:
                cum_pv = cum_v = 0.0
                last_day = day[i]
            cum_pv += (h[i] + l[i] + c[i]) / 3.0 * v[i]
            cum_v += v[i]
            if cum_v > 0:
                vwap[i] = cum_pv / cum_v

    # ── Pine `var` state ─────────────────────────────────────────────────────
    micro_high = micro_low = math.nan
    major_high = major_low = math.nan
    prev_major_high = prev_major_low = math.nan
    last_high_type = last_low_type = "NONE"
    market_bias = 0
    bull_fvgs: List[Dict[str, Any]] = []
    bear_fvgs: List[Dict[str, Any]] = []
    bull_ob: Optional[Dict[str, Any]] = None
    bear_ob: Optional[Dict[str, Any]] = None
    last_signal_bar = -10_000
    open_signals: List[Dict[str, Any]] = []
    fib382 = fib500 = fib618 = math.nan
    b618 = b705 = b786 = s618 = s705 = s786 = math.nan

    swing_labels: List[Dict[str, Any]] = []
    structure_events: List[Dict[str, Any]] = []
    fvg_boxes: List[Dict[str, Any]] = []
    ob_boxes: List[Dict[str, Any]] = []
    sweeps: List[Dict[str, Any]] = []
    signals_history: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    session_names: List[str] = []
    sweep_bars_bull: List[int] = []
    sweep_bars_bear: List[int] = []
    last: Dict[str, Any] = {}

    def t_of(i: int) -> int:
        return int(ts[i] // 1000)

    for i in range(n):
        close, open_, high, low = c[i], o[i], h[i], l[i]
        a = atr_s[i] if not math.isnan(atr_s[i]) else 0.0
        confirmed_bar = not (last_bar_forming and i == n - 1)
        prev_close = c[i - 1] if i > 0 else close

        # ── outcomes of open signals on this bar (before any new signal) ──
        for sg in open_signals:
            if sg["index"] >= i:
                continue
            is_buy = sg["action"] == "buy"
            oc = sg["outcome"]
            hit_sl = (low <= sg["stop_loss"]) if is_buy else (high >= sg["stop_loss"])
            if hit_sl and oc["status"] in ("open", "tp1", "tp2"):
                oc.update(status="sl", closed_index=i, closed_time=t_of(i), bars=i - sg["index"], r=-1.0)
                continue
            for k, key in ((3, "tp3"), (2, "tp2"), (1, "tp1")):
                hit = (high >= sg[key]) if is_buy else (low <= sg[key])
                if hit and oc["reached"] < k:
                    oc["reached"] = k
                    oc["status"] = key
                    oc[f"{key}_index"] = i
                    oc[f"{key}_time"] = t_of(i)
                    oc["r"] = float(getattr(cfg, f"tp{k}_rr"))
                    if k == 3:
                        oc.update(closed_index=i, closed_time=t_of(i), bars=i - sg["index"])
                    break
        open_signals = [s for s in open_signals if s["outcome"]["status"] not in ("sl", "tp3")]

        # trend
        bull_trend = ema_f[i] > ema_m[i] > ema_s[i]
        bear_trend = ema_f[i] < ema_m[i] < ema_s[i]
        major_bull = cfg.use_major_trend and close > ema_major[i]
        major_bear = cfg.use_major_trend and close < ema_major[i]

        # vwap
        vwap_ok = cfg.use_vwap and not math.isnan(vwap[i])
        bull_vwap = vwap_ok and close > vwap[i]
        bear_vwap = vwap_ok and close < vwap[i]

        # volatility / movement
        movement_ratio = (high - low) / a if a > 0 else 0.0
        movement = movement_ratio >= cfg.movement_min_atr

        # candle
        body = abs(close - open_)
        rng = high - low
        upper_wick = high - max(open_, close)
        lower_wick = min(open_, close) - low
        bull_candle, bear_candle = close > open_, close < open_
        body_ratio = body / rng if rng > 0 else 0.0
        strong_bull = bull_candle and body_ratio >= 0.55
        strong_bear = bear_candle and body_ratio >= 0.55
        bull_disp = strong_bull and i > 0 and close > h[i - 1]
        bear_disp = strong_bear and i > 0 and close < l[i - 1]
        bull_rej = bull_candle and lower_wick > body * 1.25
        bear_rej = bear_candle and upper_wick > body * 1.25

        # volume
        va = vol_avg[i]
        has_volume = has_volume_series and not math.isnan(va) and va > 0
        high_volume = has_volume and v[i] > va * cfg.volume_multiplier
        very_high_volume = has_volume and v[i] > va * cfg.very_high_multiplier
        vol_expansion = has_volume and i > 0 and v[i] > v[i - 1] and v[i] > va

        # structure — pivots confirm `len` bars late
        pm = i - cfg.micro_len
        if pm >= 0 and not math.isnan(ph_micro[pm]):
            micro_high = ph_micro[pm]
        if pm >= 0 and not math.isnan(pl_micro[pm]):
            micro_low = pl_micro[pm]
        pM = i - cfg.major_len
        if pM >= 0 and not math.isnan(ph_major[pM]):
            pv = ph_major[pM]
            last_high_type = "HH" if math.isnan(prev_major_high) or pv > prev_major_high else "LH"
            prev_major_high = pv
            major_high = pv
            swing_labels.append({"time": t_of(pM), "price": _f(pv), "text": last_high_type, "side": "high"})
        if pM >= 0 and not math.isnan(pl_major[pM]):
            pv = pl_major[pM]
            last_low_type = "LL" if math.isnan(prev_major_low) or pv <= prev_major_low else "HL"
            prev_major_low = pv
            major_low = pv
            swing_labels.append({"time": t_of(pM), "price": _f(pv), "text": last_low_type, "side": "low"})

        bull_break = not math.isnan(micro_high) and close > micro_high and prev_close <= micro_high
        bear_break = not math.isnan(micro_low) and close < micro_low and prev_close >= micro_low
        bull_major_break = not math.isnan(major_high) and close > major_high and prev_close <= major_high
        bear_major_break = not math.isnan(major_low) and close < major_low and prev_close >= major_low
        if bull_major_break:
            market_bias = 1
        if bear_major_break:
            market_bias = -1
        bull_choch = bull_break and market_bias == -1
        bear_choch = bear_break and market_bias == 1
        if bull_break:
            market_bias = 1
            structure_events.append({"time": t_of(i), "price": _f(low), "level": _f(micro_high),
                                     "type": "CHOCH" if bull_choch else "BOS", "direction": "up",
                                     "scope": "major" if bull_major_break else "micro"})
        if bear_break:
            market_bias = -1
            structure_events.append({"time": t_of(i), "price": _f(high), "level": _f(micro_low),
                                     "type": "CHOCH" if bear_choch else "BOS", "direction": "down",
                                     "scope": "major" if bear_major_break else "micro"})

        # dealing range
        r_hi, r_lo = range_hi[i], range_lo[i]
        r_size = r_hi - r_lo
        equilibrium = r_lo + r_size * 0.5
        in_premium = close >= equilibrium and close <= r_hi
        in_discount = close >= r_lo and close < equilibrium
        in_eq = r_lo + r_size * 0.45 <= close <= r_lo + r_size * 0.55

        # FVG — formation, retest, mitigation
        bull_fvg = i >= 2 and low > h[i - 2] and (low - h[i - 2]) >= a * cfg.fvg_min_atr
        bear_fvg = i >= 2 and high < l[i - 2] and (l[i - 2] - high) >= a * cfg.fvg_min_atr
        if bull_fvg:
            g = {"time": t_of(i - 2), "index": i, "top": low, "bottom": h[i - 2], "kind": "bull"}
            bull_fvgs.append(g)
            fvg_boxes.append({"time": g["time"], "top": _f(g["top"]), "bottom": _f(g["bottom"]),
                              "kind": "bull", "extend_bars": cfg.fvg_extension, "mitigated": False})
        if bear_fvg:
            g = {"time": t_of(i - 2), "index": i, "top": l[i - 2], "bottom": high, "kind": "bear"}
            bear_fvgs.append(g)
            fvg_boxes.append({"time": g["time"], "top": _f(g["top"]), "bottom": _f(g["bottom"]),
                              "kind": "bear", "extend_bars": cfg.fvg_extension, "mitigated": False})
        for g in bull_fvgs:
            if g["index"] != i and close < g["bottom"]:
                g["dead"] = True
        for g in bear_fvgs:
            if g["index"] != i and close > g["top"]:
                g["dead"] = True
        for g in bull_fvgs + bear_fvgs:
            if g.get("dead") and not g.get("marked"):
                g["marked"] = True
                for b in fvg_boxes:
                    if b["time"] == g["time"] and b["kind"] == g["kind"]:
                        b["mitigated"] = True
                        b["end_time"] = t_of(i)
        bull_fvgs = [g for g in bull_fvgs if not g.get("dead")][-10:]
        bear_fvgs = [g for g in bear_fvgs if not g.get("dead")][-10:]
        inside_bull_fvg = any(g["index"] != i and low <= g["top"] and high >= g["bottom"] for g in bull_fvgs)
        inside_bear_fvg = any(g["index"] != i and high >= g["bottom"] and low <= g["top"] for g in bear_fvgs)

        # order blocks on micro breaks; retired when price closes through
        if bull_break:
            for k in range(1, cfg.ob_lookback + 1):
                j = i - k
                if j < 0:
                    break
                if c[j] < o[j]:
                    bull_ob = {"time": t_of(i), "top": h[j], "bottom": l[j], "index": i}
                    ob_boxes.append({"time": t_of(i), "top": _f(h[j]), "bottom": _f(l[j]), "kind": "bull",
                                     "extend_bars": cfg.ob_extension, "mitigated": False})
                    break
        if bear_break:
            for k in range(1, cfg.ob_lookback + 1):
                j = i - k
                if j < 0:
                    break
                if c[j] > o[j]:
                    bear_ob = {"time": t_of(i), "top": h[j], "bottom": l[j], "index": i}
                    ob_boxes.append({"time": t_of(i), "top": _f(h[j]), "bottom": _f(l[j]), "kind": "bear",
                                     "extend_bars": cfg.ob_extension, "mitigated": False})
                    break
        if bull_ob and bull_ob["index"] != i and close < bull_ob["bottom"]:
            for b in ob_boxes:
                if b["time"] == bull_ob["time"] and b["kind"] == "bull":
                    b["mitigated"] = True
                    b["end_time"] = t_of(i)
            bull_ob = None
        if bear_ob and bear_ob["index"] != i and close > bear_ob["top"]:
            for b in ob_boxes:
                if b["time"] == bear_ob["time"] and b["kind"] == "bear":
                    b["mitigated"] = True
                    b["end_time"] = t_of(i)
            bear_ob = None
        inside_bull_ob = bool(bull_ob and bull_ob["bottom"] <= close <= bull_ob["top"])
        inside_bear_ob = bool(bear_ob and bear_ob["bottom"] <= close <= bear_ob["top"])

        # liquidity
        lb = cfg.liquidity_lookback
        if i >= lb:
            liq_high = float(np.max(h[i - lb:i]))
            liq_low = float(np.min(l[i - lb:i]))
        else:
            liq_high = liq_low = math.nan
        bull_sweep = not math.isnan(liq_low) and low < liq_low and close > liq_low
        bear_sweep = not math.isnan(liq_high) and high > liq_high and close < liq_high
        if bull_sweep:
            sweeps.append({"time": t_of(i), "price": _f(low), "level": _f(liq_low), "direction": "up"})
            sweep_bars_bull.append(i)
        if bear_sweep:
            sweeps.append({"time": t_of(i), "price": _f(high), "level": _f(liq_high), "direction": "down"})
            sweep_bars_bear.append(i)
        recent_bull_sweep = bool(sweep_bars_bull) and i - sweep_bars_bull[-1] < cfg.sweep_recency
        recent_bear_sweep = bool(sweep_bars_bear) and i - sweep_bars_bear[-1] < cfg.sweep_recency

        # OTE + fib zone from the major swing
        have_swing = not math.isnan(major_high) and not math.isnan(major_low) and major_high > major_low
        if have_swing:
            r = major_high - major_low
            b618, b705, b786 = major_high - r * cfg.ote_618, major_high - r * cfg.ote_705, major_high - r * cfg.ote_786
            s618, s705, s786 = major_low + r * cfg.ote_618, major_low + r * cfg.ote_705, major_low + r * cfg.ote_786
            if market_bias == 1:
                fib382, fib500, fib618 = major_high - r * 0.382, major_high - r * 0.5, major_high - r * 0.618
            else:
                fib382, fib500, fib618 = major_low + r * 0.382, major_low + r * 0.5, major_low + r * 0.618
        in_bull_ote = cfg.use_ote and have_swing and b786 <= close <= b618
        in_bear_ote = cfg.use_ote and have_swing and s618 <= close <= s786
        fib_upper = max(fib382, fib618) if not math.isnan(fib382) else math.nan
        fib_lower = min(fib382, fib618) if not math.isnan(fib382) else math.nan
        in_fib_zone = cfg.use_fib_zone and not math.isnan(fib_upper) and fib_lower <= close <= fib_upper

        # HTF
        htf_avail = htf is not None and not math.isnan(htf["slow"][i])
        htf_bull = htf_avail and htf["close"][i] > htf["fast"][i] > htf["slow"][i]
        htf_bear = htf_avail and htf["close"][i] < htf["fast"][i] < htf["slow"][i]

        # sessions / news
        sess = session_state(int(ts[i]), cfg)
        session_names.append(sess["name"])
        session_ok = (not cfg.require_session) or sess["active"]
        in_news = is_news_window(int(ts[i]), cfg)
        news_blocked = bool(cfg.news_enabled and in_news)

        # ── score (section 18 + v4 carry-overs) ──────────────────────────────
        buy_c: Dict[str, int] = {}
        sell_c: Dict[str, int] = {}

        def add(side: Dict[str, int], key: str, pts: int) -> None:
            side[key] = side.get(key, 0) + pts

        if bull_trend:
            add(buy_c, "trend", 2)
        if bear_trend:
            add(sell_c, "trend", 2)
        if major_bull:
            add(buy_c, "major_trend", 1)
        if major_bear:
            add(sell_c, "major_trend", 1)
        if bull_vwap:
            add(buy_c, "vwap", 1)
        if bear_vwap:
            add(sell_c, "vwap", 1)
        if bull_break:
            add(buy_c, "bos", 2)
        if bear_break:
            add(sell_c, "bos", 2)
        if bull_major_break:
            add(buy_c, "major_bos", 1)
        if bear_major_break:
            add(sell_c, "major_bos", 1)
        if bull_choch:
            add(buy_c, "choch", 2)
        if bear_choch:
            add(sell_c, "choch", 2)
        if bull_sweep:
            add(buy_c, "sweep", 2)
        if bear_sweep:
            add(sell_c, "sweep", 2)
        if bull_disp:
            add(buy_c, "displacement", 2)
        if bear_disp:
            add(sell_c, "displacement", 2)
        if bull_rej:
            add(buy_c, "rejection", 1)
        if bear_rej:
            add(sell_c, "rejection", 1)
        if cfg.use_volume:
            if high_volume and bull_candle:
                add(buy_c, "volume", 1)
            if high_volume and bear_candle:
                add(sell_c, "volume", 1)
            if very_high_volume and bull_candle:
                add(buy_c, "volume", 1)
            if very_high_volume and bear_candle:
                add(sell_c, "volume", 1)
            if vol_expansion and bull_candle:
                add(buy_c, "vol_expansion", 1)
            if vol_expansion and bear_candle:
                add(sell_c, "vol_expansion", 1)
        if bull_fvg:
            add(buy_c, "fvg", 1)
        if bear_fvg:
            add(sell_c, "fvg", 1)
        if cfg.score_fvg_retest:
            if inside_bull_fvg:
                add(buy_c, "fvg_retest", 1)
            if inside_bear_fvg:
                add(sell_c, "fvg_retest", 1)
        if inside_bull_ob:
            add(buy_c, "ob", 1)
        if inside_bear_ob:
            add(sell_c, "ob", 1)
        if in_discount:
            add(buy_c, "discount", 1)
        if in_premium:
            add(sell_c, "premium", 1)
        if in_fib_zone and in_discount:
            add(buy_c, "fib", 1)
        if in_fib_zone and in_premium:
            add(sell_c, "fib", 1)
        if in_bull_ote:
            add(buy_c, "ote", 1)
        if in_bear_ote:
            add(sell_c, "ote", 1)
        if htf_bull:
            add(buy_c, "htf", 1)
        if htf_bear:
            add(sell_c, "htf", 1)
        if movement and bull_candle:
            add(buy_c, "movement", 1)
        if movement and bear_candle:
            add(sell_c, "movement", 1)
        if cfg.score_session and sess["active"]:
            add(buy_c, "session", 1)
            add(sell_c, "session", 1)
        buy_score = sum(buy_c.values())
        sell_score = sum(sell_c.values())

        # ── raw setups ───────────────────────────────────────────────────────
        scalp_long = (buy_score >= cfg.scalp_min_score and buy_score > sell_score
                      and (not cfg.scalp_require_trend or bull_trend)
                      and (not cfg.scalp_require_vwap or not vwap_ok or bull_vwap))
        scalp_short = (sell_score >= cfg.scalp_min_score and sell_score > buy_score
                       and (not cfg.scalp_require_trend or bear_trend)
                       and (not cfg.scalp_require_vwap or not vwap_ok or bear_vwap))

        def sniper_ok(side: str) -> Tuple[bool, List[str]]:
            miss: List[str] = []
            if side == "buy":
                if buy_score < cfg.sniper_min_score or buy_score <= sell_score:
                    miss.append("score")
                if cfg.sniper_require_htf and not htf_bull:
                    miss.append("htf")
                if cfg.sniper_require_sweep and not recent_bull_sweep:
                    miss.append("sweep")
                if cfg.sniper_require_zone and not (in_discount or in_bull_ote):
                    miss.append("zone")
                if cfg.sniper_require_major_trend and cfg.use_major_trend and not major_bull:
                    miss.append("major_trend")
            else:
                if sell_score < cfg.sniper_min_score or sell_score <= buy_score:
                    miss.append("score")
                if cfg.sniper_require_htf and not htf_bear:
                    miss.append("htf")
                if cfg.sniper_require_sweep and not recent_bear_sweep:
                    miss.append("sweep")
                if cfg.sniper_require_zone and not (in_premium or in_bear_ote):
                    miss.append("zone")
                if cfg.sniper_require_major_trend and cfg.use_major_trend and not major_bear:
                    miss.append("major_trend")
            return (not miss), miss

        sniper_long, sniper_long_miss = sniper_ok("buy")
        sniper_short, sniper_short_miss = sniper_ok("sell")

        mode = cfg.trade_mode
        if mode == "HYBRID":
            long_kind = "sniper" if sniper_long else "scalp" if scalp_long else None
            short_kind = "sniper" if sniper_short else "scalp" if scalp_short else None
        elif mode == "SNIPER":
            long_kind = "sniper" if sniper_long else None
            short_kind = "sniper" if sniper_short else None
        else:
            long_kind = "scalp" if scalp_long else None
            short_kind = "scalp" if scalp_short else None

        # ── gates ────────────────────────────────────────────────────────────
        def gate(side: str) -> Optional[str]:
            if side == "buy" and not cfg.allow_long:
                return "long disabled"
            if side == "sell" and not cfg.allow_short:
                return "short disabled"
            if not session_ok:
                return "outside active sessions"
            if news_blocked:
                return "news blackout"
            if cfg.confirmed_only and not confirmed_bar:
                return "forming bar"
            if i - last_signal_bar < cfg.signal_cooldown:
                return f"cooldown ({i - last_signal_bar} of {cfg.signal_cooldown} bars)"
            if cfg.avoid_duplicates and any(
                s["action"] == side and s["outcome"]["reached"] == 0 for s in open_signals
            ):
                return f"duplicate — a {side.upper()} is still open"
            return None

        pending_action = "buy" if long_kind else "sell" if short_kind else "wait"
        pending_kind = long_kind or short_kind
        fired = None
        plan: Dict[str, Any] = {}
        for side, kind in (("buy", long_kind), ("sell", short_kind)):
            if not kind:
                continue
            why = gate(side)
            entry = close
            if why is None:
                if side == "buy":
                    struct = micro_low if (not math.isnan(micro_low) and micro_low < entry) else major_low
                    sl = (struct - cfg.sl_atr_buffer * a) if not math.isnan(struct) else entry - a
                    risk = entry - sl
                else:
                    struct = micro_high if (not math.isnan(micro_high) and micro_high > entry) else major_high
                    sl = (struct + cfg.sl_atr_buffer * a) if not math.isnan(struct) else entry + a
                    risk = sl - entry
                if a > 0:
                    risk = max(risk, cfg.min_risk_atr * a)
                    risk = min(risk, cfg.max_risk_atr * a)
                if risk <= 0:
                    why = "no risk distance"
                else:
                    sgn = 1 if side == "buy" else -1
                    sl = entry - sgn * risk
                    tp1, tp2, tp3 = (entry + sgn * risk * cfg.tp1_rr, entry + sgn * risk * cfg.tp2_rr,
                                     entry + sgn * risk * cfg.tp3_rr)
                    if cfg.require_room_tp3:
                        obstacle = r_hi if side == "buy" else r_lo
                        blocked = (tp3 > obstacle > entry) if side == "buy" else (tp3 < obstacle < entry)
                        if blocked:
                            why = f"no room to TP3 before the range {'high' if side == 'buy' else 'low'} {_f(obstacle)}"
                    if why is None:
                        plan = {"entry": entry, "stop_loss": sl, "risk": risk, "tp1": tp1, "tp2": tp2, "tp3": tp3,
                                "structural": struct if not math.isnan(struct) else None}
            if why is not None:
                rejected.append({"index": i, "time": t_of(i), "side": side, "kind": kind, "reason": why,
                                 "score": buy_score if side == "buy" else sell_score})
                continue
            fired = (side, kind)
            break

        if fired:
            side, kind = fired
            last_signal_bar = i
            rec = {
                "time": t_of(i), "index": i, "action": side, "kind": kind,
                "score": buy_score if side == "buy" else sell_score,
                "entry": _f(plan["entry"]), "stop_loss": _f(plan["stop_loss"]), "risk": _f(plan["risk"]),
                "tp1": _f(plan["tp1"]), "tp2": _f(plan["tp2"]), "tp3": _f(plan["tp3"]),
                "structural_level": _f(plan["structural"]),
                "components": dict(buy_c if side == "buy" else sell_c),
                "score_buy": buy_score, "score_sell": sell_score, "bias": market_bias,
                "atr": _f(a),
                "outcome": {"status": "open", "reached": 0, "r": 0.0},
            }
            signals_history.append(rec)
            open_signals.append(rec)

        if i == n - 1:
            last = {
                "confirmed_bar": confirmed_bar, "bull_trend": bull_trend, "bear_trend": bear_trend,
                "major_bull": major_bull, "major_bear": major_bear,
                "vwap_ok": vwap_ok, "bull_vwap": bull_vwap, "bear_vwap": bear_vwap, "vwap": vwap[i],
                "movement": movement, "movement_ratio": movement_ratio,
                "has_volume": has_volume, "high_volume": high_volume, "very_high_volume": very_high_volume,
                "vol_expansion": vol_expansion, "vol_ratio": (v[i] / va) if has_volume else None,
                "strong_bull": strong_bull, "strong_bear": strong_bear, "bull_disp": bull_disp, "bear_disp": bear_disp,
                "bull_rej": bull_rej, "bear_rej": bear_rej,
                "bull_break": bull_break, "bear_break": bear_break, "bull_major_break": bull_major_break,
                "bear_major_break": bear_major_break, "bull_choch": bull_choch, "bear_choch": bear_choch,
                "range_hi": r_hi, "range_lo": r_lo, "equilibrium": equilibrium,
                "in_premium": in_premium, "in_discount": in_discount, "in_eq": in_eq,
                "bull_fvg": bull_fvg, "bear_fvg": bear_fvg, "inside_bull_fvg": inside_bull_fvg, "inside_bear_fvg": inside_bear_fvg,
                "bull_fvgs": list(bull_fvgs), "bear_fvgs": list(bear_fvgs),
                "bull_ob": bull_ob, "bear_ob": bear_ob, "inside_bull_ob": inside_bull_ob, "inside_bear_ob": inside_bear_ob,
                "liq_high": liq_high, "liq_low": liq_low, "bull_sweep": bull_sweep, "bear_sweep": bear_sweep,
                "recent_bull_sweep": recent_bull_sweep, "recent_bear_sweep": recent_bear_sweep,
                "b618": b618, "b705": b705, "b786": b786, "s618": s618, "s705": s705, "s786": s786,
                "in_bull_ote": in_bull_ote, "in_bear_ote": in_bear_ote,
                "fib_upper": fib_upper, "fib_lower": fib_lower, "in_fib_zone": in_fib_zone,
                "htf_avail": htf_avail, "htf_bull": htf_bull, "htf_bear": htf_bear,
                "htf_close": htf["close"][i] if htf is not None else math.nan,
                "htf_fast": htf["fast"][i] if htf is not None else math.nan,
                "htf_slow": htf["slow"][i] if htf is not None else math.nan,
                "session": sess, "session_ok": session_ok, "in_news": in_news, "news_blocked": news_blocked,
                "buy_c": buy_c, "sell_c": sell_c, "buy_score": buy_score, "sell_score": sell_score,
                "scalp_long": scalp_long, "scalp_short": scalp_short, "sniper_long": sniper_long, "sniper_short": sniper_short,
                "sniper_long_miss": sniper_long_miss, "sniper_short_miss": sniper_short_miss,
                "pending_action": pending_action, "pending_kind": pending_kind, "fired": fired,
                "atr": a, "micro_high": micro_high, "micro_low": micro_low, "major_high": major_high, "major_low": major_low,
                "bars_since_signal": i - last_signal_bar if last_signal_bar > -10_000 else None,
                "open_signals": [s["action"] for s in open_signals],
            }

    # ── assemble ─────────────────────────────────────────────────────────────
    last_time = int(ts[-1] // 1000)
    last_price = float(c[-1])
    last_sig = signals_history[-1] if signals_history else None
    if last_sig is not None:
        last_sig = {**last_sig, "bars_since": (n - 1) - last_sig["index"]}

    if last["fired"]:
        side, kind = last["fired"]
        signal = {
            "action": side, "kind": kind, "new": True, "confirmed": True,
            "score": last["buy_score"] if side == "buy" else last["sell_score"],
            "entry": last_sig["entry"], "stop_loss": last_sig["stop_loss"], "risk": last_sig["risk"],
            "tp1": last_sig["tp1"], "tp2": last_sig["tp2"], "tp3": last_sig["tp3"],
            "time": last_time, "blocked_by_news": False,
        }
    else:
        pa, pk = last["pending_action"], last["pending_kind"]
        signal = {
            "action": pa, "kind": pk, "new": pa != "wait", "confirmed": False,
            "score": last["buy_score"] if pa == "buy" else last["sell_score"] if pa == "sell" else max(last["buy_score"], last["sell_score"]),
            "entry": None, "stop_loss": None, "risk": None, "tp1": None, "tp2": None, "tp3": None,
            "time": last_time, "blocked_by_news": bool(pa != "wait" and last["news_blocked"]),
        }
        if pa != "wait":
            a = last["atr"]
            if pa == "buy":
                struct = last["micro_low"] if (not math.isnan(last["micro_low"]) and last["micro_low"] < last_price) else last["major_low"]
                risk = (last_price - (struct - cfg.sl_atr_buffer * a)) if not math.isnan(struct) else a
            else:
                struct = last["micro_high"] if (not math.isnan(last["micro_high"]) and last["micro_high"] > last_price) else last["major_high"]
                risk = ((struct + cfg.sl_atr_buffer * a) - last_price) if not math.isnan(struct) else a
            if a > 0:
                risk = min(max(risk, cfg.min_risk_atr * a), cfg.max_risk_atr * a)
            if risk > 0:
                sgn = 1 if pa == "buy" else -1
                signal.update({"entry": _f(last_price), "stop_loss": _f(last_price - sgn * risk), "risk": _f(risk),
                               "tp1": _f(last_price + sgn * risk * cfg.tp1_rr), "tp2": _f(last_price + sgn * risk * cfg.tp2_rr),
                               "tp3": _f(last_price + sgn * risk * cfg.tp3_rr)})
            last_rej = [r for r in rejected if r["index"] == n - 1]
            if last_rej:
                signal["gate"] = last_rej[-1]["reason"]

    resolved = [s for s in signals_history if s["outcome"]["status"] == "sl" or s["outcome"]["reached"] >= 1]
    wins = [s for s in resolved if s["outcome"]["reached"] >= 1]
    outcomes = {
        "count": len(signals_history),
        "tp1": sum(1 for s in signals_history if s["outcome"]["reached"] >= 1),
        "tp2": sum(1 for s in signals_history if s["outcome"]["reached"] >= 2),
        "tp3": sum(1 for s in signals_history if s["outcome"]["reached"] >= 3),
        "sl": sum(1 for s in signals_history if s["outcome"]["status"] == "sl"),
        "open": sum(1 for s in signals_history if s["outcome"]["status"] == "open"),
        "win_rate": round(len(wins) / len(resolved), 3) if resolved else None,
        "avg_r": round(sum(float(s["outcome"].get("r") or 0.0) for s in resolved) / len(resolved), 3) if resolved else None,
        "scalp": sum(1 for s in signals_history if s["kind"] == "scalp"),
        "sniper": sum(1 for s in signals_history if s["kind"] == "sniper"),
    }

    bias_label = "BULLISH" if market_bias == 1 else "BEARISH" if market_bias == -1 else "NEUTRAL"
    structure_now = ("CHOCH ↑" if last["bull_choch"] else "CHOCH ↓" if last["bear_choch"]
                     else "BOS ↑" if last["bull_break"] else "BOS ↓" if last["bear_break"] else "WAIT")
    volume_level = ("VERY HIGH" if last["very_high_volume"] else "HIGH" if last["high_volume"]
                    else "NORMAL" if last["has_volume"] else "N/A")
    fvg_now = "BULL FVG" if last["inside_bull_fvg"] else "BEAR FVG" if last["inside_bear_fvg"] else "NONE"
    liq_now = "BUY SWEEP" if last["bull_sweep"] else "SELL SWEEP" if last["bear_sweep"] else "NONE"
    zone_now = "EQUILIBRIUM" if last["in_eq"] else "DISCOUNT" if last["in_discount"] else "PREMIUM" if last["in_premium"] else "EQUILIBRIUM"
    ote_now = "BULL OTE" if last["in_bull_ote"] else "BEAR OTE" if last["in_bear_ote"] else "NONE"
    htf_now = ("BULL" if last["htf_bull"] else "BEAR" if last["htf_bear"] else "MIXED") if last["htf_avail"] else "N/A"
    vwap_now = ("ABOVE" if last["bull_vwap"] else "BELOW" if last["bear_vwap"] else "AT") if last["vwap_ok"] else "N/A"
    trend_now = "BULL" if last["bull_trend"] else "BEAR" if last["bear_trend"] else "MIXED"
    sig_label = "BUY" if signal["action"] == "buy" else "SELL" if signal["action"] == "sell" else "WAIT"
    if signal.get("kind") and signal["action"] != "wait":
        sig_label += f" ({signal['kind']})"

    bands: List[Dict[str, Any]] = []
    for i, name in enumerate(session_names):
        t0 = int(ts[i] // 1000)
        if bands and bands[-1]["name"] == name and bands[-1]["end"] == t0:
            bands[-1]["end"] = t0 + tf_sec
        else:
            bands.append({"start": t0, "end": t0 + tf_sec, "name": name})
    bands = [b for b in bands if b["name"] != "OFF"][-60:]

    req = _mode_min(cfg)
    side_c = last["sell_c"] if signal["action"] == "sell" or (signal["action"] == "wait" and last["sell_score"] > last["buy_score"]) else last["buy_c"]
    reasons = [f"{k}+{pts}" for k, pts in side_c.items()]
    wr = f"{outcomes['win_rate'] * 100:.0f}%" if outcomes["win_rate"] is not None else "n/a"
    summary = (
        f"OBLIVION v5 {cfg.trade_mode} {timeframe}: {sig_label} buy {last['buy_score']} / sell {last['sell_score']} "
        f"(scalp ≥{cfg.scalp_min_score}, sniper ≥{cfg.sniper_min_score}) — bias {bias_label}, trend {trend_now}, HTF {htf_now}, "
        f"VWAP {vwap_now}, {zone_now.lower()}, OTE {ote_now}, structure {structure_now}, FVG {fvg_now}, liquidity {liq_now}, "
        f"session {last['session']['name']}, volume {volume_level}, news {'BLOCKED' if last['news_blocked'] else 'clear'}, "
        f"win rate {wr} over {outcomes['count']} signals."
    )
    if signal["action"] != "wait" and signal.get("entry") is not None:
        summary += (f" {sig_label.upper()} {'confirmed' if signal['confirmed'] else 'forming'}: entry {signal['entry']}, "
                    f"SL {signal['stop_loss']}, TP1 {signal['tp1']}, TP2 {signal['tp2']}, TP3 {signal['tp3']}.")
        if signal.get("gate"):
            summary += f" Gate: {signal['gate']}."
    elif last_sig is not None:
        summary += (f" Last {last_sig['action'].upper()} ({last_sig['kind']}) {last_sig['bars_since']} bars ago: entry {last_sig['entry']}, "
                    f"SL {last_sig['stop_loss']}, TP1 {last_sig['tp1']}, TP2 {last_sig['tp2']}, TP3 {last_sig['tp3']} — "
                    f"outcome {last_sig['outcome']['status'].upper()}.")

    def _ob_payload(ob: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        return {"top": _f(ob["top"]), "bottom": _f(ob["bottom"]), "time": ob["time"]} if ob else None

    def _gap(gaps: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        return {"top": _f(gaps[-1]["top"]), "bottom": _f(gaps[-1]["bottom"]), "time": gaps[-1]["time"]} if gaps else None

    result: Dict[str, Any] = {
        "engine": "oblivion", "version": ENGINE_VERSION, "mode": cfg.trade_mode,
        "timeframe": timeframe, "bars": n, "last_time": last_time, "last_price": _f(last_price),
        "last_bar_forming": bool(last_bar_forming), "atr": _f(last["atr"]),
        "score": {
            "buy": last["buy_score"], "sell": last["sell_score"],
            "bull": last["buy_score"], "bear": last["sell_score"],
            "required": req, "scalp_min": cfg.scalp_min_score, "sniper_min": cfg.sniper_min_score,
            "components": {"buy": last["buy_c"], "sell": last["sell_c"], "bull": last["buy_c"], "bear": last["sell_c"]},
            "reasons": reasons,
            "setups": {"scalp_long": last["scalp_long"], "scalp_short": last["scalp_short"],
                       "sniper_long": last["sniper_long"], "sniper_short": last["sniper_short"],
                       "sniper_long_missing": last["sniper_long_miss"], "sniper_short_missing": last["sniper_short_miss"]},
        },
        "trend": {"ema_fast": _f(ema_f[-1]), "ema_medium": _f(ema_m[-1]), "ema_slow": _f(ema_s[-1]), "ema_major": _f(ema_major[-1]),
                  "bull": bool(last["bull_trend"]), "bear": bool(last["bear_trend"]),
                  "major_bull": bool(last["major_bull"]), "major_bear": bool(last["major_bear"]), "now": trend_now},
        "vwap": {"value": _f(last["vwap"]), "available": bool(last["vwap_ok"]), "now": vwap_now,
                 "bull": bool(last["bull_vwap"]), "bear": bool(last["bear_vwap"])},
        "movement": {"ratio": _f(last["movement_ratio"], 3), "ok": bool(last["movement"])},
        "market_bias": market_bias, "bias_label": bias_label,
        "structure": {
            "micro_high": _f(last["micro_high"]), "micro_low": _f(last["micro_low"]),
            "major_high": _f(last["major_high"]), "major_low": _f(last["major_low"]),
            "last_swing_high": _f(last["major_high"]), "last_swing_low": _f(last["major_low"]),
            "last_high_type": last_high_type, "last_low_type": last_low_type, "now": structure_now,
            "labels": swing_labels[-40:], "events": structure_events[-24:],
        },
        "pd": {"range_high": _f(last["range_hi"]), "range_low": _f(last["range_lo"]), "equilibrium": _f(last["equilibrium"]),
               "zone": zone_now, "lookback": cfg.pd_lookback},
        "fvg": {"bull": _gap(last["bull_fvgs"]), "bear": _gap(last["bear_fvgs"]), "boxes": fvg_boxes[-24:],
                "new_bull": bool(last["bull_fvg"]), "new_bear": bool(last["bear_fvg"]),
                "inside_bull": bool(last["inside_bull_fvg"]), "inside_bear": bool(last["inside_bear_fvg"]), "now": fvg_now,
                "active_bull": len(last["bull_fvgs"]), "active_bear": len(last["bear_fvgs"])},
        "order_blocks": {"bull": _ob_payload(last["bull_ob"]), "bear": _ob_payload(last["bear_ob"]), "boxes": ob_boxes[-16:],
                         "inside_bull": bool(last["inside_bull_ob"]), "inside_bear": bool(last["inside_bear_ob"])},
        "liquidity": {"prev_high": _f(last["liq_high"]), "prev_low": _f(last["liq_low"]),
                      "bull_sweep": bool(last["bull_sweep"]), "bear_sweep": bool(last["bear_sweep"]),
                      "recent_bull_sweep": bool(last["recent_bull_sweep"]), "recent_bear_sweep": bool(last["recent_bear_sweep"]),
                      "sweeps": sweeps[-20:], "now": liq_now},
        "ote": {"bull": {"618": _f(last["b618"]), "705": _f(last["b705"]), "786": _f(last["b786"])} if not math.isnan(last["b618"]) else None,
                "bear": {"618": _f(last["s618"]), "705": _f(last["s705"]), "786": _f(last["s786"])} if not math.isnan(last["s618"]) else None,
                "in_bull": bool(last["in_bull_ote"]), "in_bear": bool(last["in_bear_ote"]), "now": ote_now},
        "fib": {"f382": _f(fib382), "f500": _f(fib500), "f618": _f(fib618), "upper": _f(last["fib_upper"]), "lower": _f(last["fib_lower"]),
                "in_zone": bool(last["in_fib_zone"]), "swing_high": _f(last["major_high"]), "swing_low": _f(last["major_low"])},
        "equilibrium": _f(last["equilibrium"]), "premium": bool(last["in_premium"]), "discount": bool(last["in_discount"]),
        "htf": {"timeframe": cfg.htf_timeframe, "available": bool(last["htf_avail"]), "bull": bool(last["htf_bull"]), "bear": bool(last["htf_bear"]),
                "close": _f(last["htf_close"]), "fast": _f(last["htf_fast"]), "slow": _f(last["htf_slow"]), "now": htf_now},
        "session": {**last["session"], "ok": bool(last["session_ok"]), "required": cfg.require_session, "bands": bands},
        "news": {"enabled": cfg.news_enabled, "in_window": bool(last["in_news"]), "blocked": bool(last["news_blocked"]),
                 "window": cfg.news_window, "tz": cfg.news_tz},
        "volume": {"level": volume_level, "ratio": _f(last["vol_ratio"], 3), "expansion": bool(last["vol_expansion"])},
        "candle": {"strong_bull": bool(last["strong_bull"]), "strong_bear": bool(last["strong_bear"]),
                   "bull_displacement": bool(last["bull_disp"]), "bear_displacement": bool(last["bear_disp"]),
                   "bull_rejection": bool(last["bull_rej"]), "bear_rejection": bool(last["bear_rej"])},
        "gates": {"session_ok": bool(last["session_ok"]), "news_ok": not bool(last["news_blocked"]),
                  "bars_since_signal": last["bars_since_signal"], "cooldown": cfg.signal_cooldown,
                  "open_signals": last["open_signals"], "confirmed_only": cfg.confirmed_only},
        "signal": signal, "last_signal": last_sig, "signals_history": signals_history if full_history else signals_history[-12:],
        "rejected": rejected[-12:], "outcomes": outcomes,
        "dashboard": {
            "Mode": cfg.trade_mode,
            "Buy Score": str(last["buy_score"]), "Sell Score": str(last["sell_score"]),
            "Scalp Min": str(cfg.scalp_min_score), "Sniper Min": str(cfg.sniper_min_score),
            "Market Bias": bias_label, "Trend": trend_now, "HTF": htf_now, "VWAP": vwap_now,
            "Zone": zone_now, "OTE": ote_now, "Structure": structure_now, "FVG": fvg_now, "Liquidity": liq_now,
            "Session": last["session"]["name"], "Volume": volume_level,
            "News": "BLOCKED" if last["news_blocked"] else "CLEAR",
            "Signal": sig_label, "Win Rate": wr,
        },
        "summary": summary, "config": cfg.to_dict(),
    }
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Adapters
# ─────────────────────────────────────────────────────────────────────────────

def oblivion_score(result_or_rows: Union[Dict[str, Any], pd.DataFrame, Sequence[Any]], timeframe: str = "1h",
                   cfg: Optional[OblivionConfig] = None) -> float:
    """Signed fusion score in [-1, 1]: ``(buy − sell) / scalp_min``."""
    res = result_or_rows if isinstance(result_or_rows, dict) else analyze_oblivion(result_or_rows, timeframe, cfg)
    sc = res.get("score") or {}
    req = max(1, int(sc.get("scalp_min") or sc.get("required") or 6))
    buy = sc.get("buy", sc.get("bull")) or 0
    sell = sc.get("sell", sc.get("bear")) or 0
    raw = (int(buy) - int(sell)) / req
    return round(max(-1.0, min(1.0, raw)), 3)


def wait_reason(res: Dict[str, Any]) -> Optional[str]:
    """Why the engine is not firing right now, from its own rules."""
    sig = res.get("signal") or {}
    action = str(sig.get("action") or "wait")
    if action in ("buy", "sell") and sig.get("confirmed"):
        return None
    if sig.get("gate"):
        return str(sig["gate"])
    if action in ("buy", "sell"):
        return "the setup is on the forming bar — it must hold at the close"
    sc = res.get("score") or {}
    setups = sc.get("setups") or {}
    buy, sell = int(sc.get("buy") or 0), int(sc.get("sell") or 0)
    mode = res.get("mode") or "HYBRID"
    smin, nmin = int(sc.get("scalp_min") or 6), int(sc.get("sniper_min") or 9)
    if mode == "SNIPER":
        miss = setups.get("sniper_long_missing") if buy >= sell else setups.get("sniper_short_missing")
        if miss:
            return f"sniper requirements missing: {', '.join(miss)} (buy {buy} / sell {sell}, need {nmin})"
    if buy == sell and buy >= smin:
        return f"both sides score {buy} — a side must beat the other, not just clear the minimum"
    if max(buy, sell) < smin:
        return f"neither side has reached the scalp minimum ({buy} / {sell} vs {smin}) — more confluence is needed"
    return f"no side clears its gate (buy {buy} / sell {sell}; scalp ≥{smin}, sniper ≥{nmin})"


def compact_for_prompt(result: Dict[str, Any]) -> Dict[str, Any]:
    """Lean copy for LLM context: drops chart-only history, keeps the read."""
    if not result or result.get("error"):
        return {k: result.get(k) for k in ("engine", "version", "mode", "timeframe", "error", "signal", "summary") if k in result}
    structure = result.get("structure") or {}
    fvg = result.get("fvg") or {}
    ob = result.get("order_blocks") or {}
    liq = result.get("liquidity") or {}
    session = result.get("session") or {}
    struct_keys = ("now", "micro_high", "micro_low", "major_high", "major_low",
                   "last_swing_high", "last_swing_low", "last_high_type", "last_low_type")
    return {
        "engine": "oblivion_v5", "version": result.get("version"), "mode": result.get("mode"),
        "timeframe": result.get("timeframe"), "summary": result.get("summary"),
        "score": result.get("score"), "market_bias": result.get("market_bias"), "bias_label": result.get("bias_label"),
        "trend": result.get("trend"), "vwap": result.get("vwap"), "movement": result.get("movement"),
        "structure": {**{k: structure.get(k) for k in struct_keys}, "recent_events": (structure.get("events") or [])[-5:]},
        "pd": result.get("pd"),
        "fvg": {k: fvg.get(k) for k in ("bull", "bear", "inside_bull", "inside_bear", "now", "active_bull", "active_bear")},
        "order_blocks": {k: ob.get(k) for k in ("bull", "bear", "inside_bull", "inside_bear")},
        "liquidity": {k: liq.get(k) for k in ("prev_high", "prev_low", "bull_sweep", "bear_sweep", "recent_bull_sweep", "recent_bear_sweep", "now")},
        "ote": result.get("ote"), "fib": result.get("fib"), "htf": result.get("htf"),
        "equilibrium": result.get("equilibrium"), "premium": result.get("premium"), "discount": result.get("discount"),
        "session": {k: session.get(k) for k in ("name", "active", "overlap", "ok", "required")},
        "news": result.get("news"), "volume": result.get("volume"), "candle": result.get("candle"),
        "gates": result.get("gates"), "signal": result.get("signal"), "last_signal": result.get("last_signal"),
        "rejected": (result.get("rejected") or [])[-3:], "outcomes": result.get("outcomes"),
        "wait_reason": wait_reason(result), "dashboard": result.get("dashboard"),
    }


_C = {
    "green": "#22c55e", "red": "#ef4444", "lime": "#84cc16", "aqua": "#22d3ee", "orange": "#f97316",
    "yellow": "#eab308", "white": "#f8fafc", "blue": "#3b82f6", "gray": "#9ca3af", "fuchsia": "#d946ef",
}


def oblivion_to_overlays(result: Dict[str, Any], timeframe: Optional[str] = None,
                         rows: Optional[Sequence[Any]] = None) -> Dict[str, Any]:
    """Engine result → the generic chart ``OverlayBundle``.

    ``boxes`` (FVG / OB, with ``mitigated``), ``labels`` (HH/HL/LH/LL,
    BOS/CHOCH, sweeps), ``markers`` (scalp arrows, sniper diamonds, outcome
    ticks), ``price_lines`` (fib, OTE, EQ, plan levels), ``hbands`` (premium /
    discount), ``lines`` (VWAP / EMAs when ``rows`` are supplied and shown),
    ``bands`` (sessions), ``dashboard``.
    """
    if not result or result.get("error"):
        return {"boxes": [], "labels": [], "markers": [], "price_lines": [], "bands": [], "hbands": [], "lines": [],
                "dashboard": None, "summary": (result or {}).get("summary")}
    cfg = config_from_dict(result.get("config") or {}, OblivionConfig())
    tf_sec = timeframe_seconds(timeframe or result.get("timeframe") or "1h")
    boxes: List[Dict[str, Any]] = []
    if cfg.show_fvg:
        for b in (result.get("fvg") or {}).get("boxes") or []:
            bull = b.get("kind") == "bull"
            boxes.append({"time": b["time"], "end_time": b.get("end_time") or b["time"] + (b.get("extend_bars", 25) + 2) * tf_sec,
                          "top": b["top"], "bottom": b["bottom"],
                          "fill": "rgba(34,197,94,0.12)" if bull else "rgba(239,68,68,0.12)",
                          "border": "rgba(34,197,94,0.5)" if bull else "rgba(239,68,68,0.5)",
                          "label": "FVG", "kind": "fvg_bull" if bull else "fvg_bear", "mitigated": bool(b.get("mitigated"))})
    if cfg.show_ob:
        for b in (result.get("order_blocks") or {}).get("boxes") or []:
            bull = b.get("kind") == "bull"
            boxes.append({"time": b["time"], "end_time": b.get("end_time") or b["time"] + b.get("extend_bars", 35) * tf_sec,
                          "top": b["top"], "bottom": b["bottom"],
                          "fill": "rgba(59,130,246,0.10)" if bull else "rgba(249,115,22,0.10)",
                          "border": "rgba(59,130,246,0.55)" if bull else "rgba(249,115,22,0.55)",
                          "label": "OB", "kind": "ob_bull" if bull else "ob_bear", "mitigated": bool(b.get("mitigated"))})

    labels: List[Dict[str, Any]] = []
    if cfg.show_labels:
        for s in (result.get("structure") or {}).get("labels") or []:
            good = s["text"] in ("HH", "HL")
            labels.append({"time": s["time"], "price": s["price"], "text": s["text"], "color": _C["white"],
                           "bg": _C["green"] if good else _C["red"], "position": "above" if s["side"] == "high" else "below", "size": "tiny"})
    if cfg.show_bos:
        for e in (result.get("structure") or {}).get("events") or []:
            up = e["direction"] == "up"
            labels.append({"time": e["time"], "price": e["price"], "text": f"{e['type']} {'↑' if up else '↓'}",
                           "color": _C["white"], "bg": _C["green"] if up else _C["red"],
                           "position": "below" if up else "above", "size": "small" if e.get("scope") == "major" else "tiny"})
    for s in (result.get("liquidity") or {}).get("sweeps") or []:
        up = s["direction"] == "up"
        labels.append({"time": s["time"], "price": s["price"], "text": "SW ↑" if up else "SW ↓", "color": "#0f172a",
                       "bg": _C["aqua"] if up else _C["orange"], "position": "below" if up else "above", "size": "tiny"})

    markers: List[Dict[str, Any]] = []
    for sg in result.get("signals_history") or []:
        buy = sg["action"] == "buy"
        sniper = sg.get("kind") == "sniper"
        markers.append({"time": sg["time"], "position": "belowBar" if buy else "aboveBar",
                        "shape": "diamond" if sniper else ("arrowUp" if buy else "arrowDown"),
                        "color": _C["lime"] if buy else _C["red"], "text": ("BUY" if buy else "SELL") + (" ★" if sniper else ""),
                        "kind": sg.get("kind"),
                        "detail": f"{sg.get('kind', '')} entry {sg['entry']} SL {sg['stop_loss']} TP1 {sg['tp1']} TP2 {sg['tp2']} TP3 {sg['tp3']} score {sg['score']}"})
        oc = sg.get("outcome") or {}
        if cfg.show_outcomes:
            for k in (1, 2, 3):
                t = oc.get(f"tp{k}_time")
                if t:
                    markers.append({"time": t, "position": "aboveBar" if buy else "belowBar", "shape": "circle",
                                    "color": _C["green"], "text": f"TP{k} ✓", "kind": "outcome"})
            if oc.get("status") == "sl" and oc.get("closed_time"):
                markers.append({"time": oc["closed_time"], "position": "belowBar" if buy else "aboveBar", "shape": "circle",
                                "color": _C["red"], "text": "SL ✗", "kind": "outcome"})

    price_lines: List[Dict[str, Any]] = []
    fib = result.get("fib") or {}
    if fib.get("f382") is not None:
        price_lines += [{"price": fib["f382"], "color": _C["yellow"], "style": "dotted", "width": 1, "title": "Fib 0.382"},
                        {"price": fib["f500"], "color": _C["orange"], "style": "dashed", "width": 1, "title": "Fib 0.5"},
                        {"price": fib["f618"], "color": _C["aqua"], "style": "dotted", "width": 1, "title": "Fib 0.618"}]
    ote = result.get("ote") or {}
    if cfg.show_ote:
        for side, col in (("bull", _C["green"]), ("bear", _C["red"])):
            z = ote.get(side)
            if z:
                for k in ("618", "705", "786"):
                    price_lines.append({"price": z[k], "color": col, "style": "dotted", "width": 1, "title": f"OTE .{k} {side}"})
    plan = result.get("signal") if (result.get("signal") or {}).get("entry") is not None else result.get("last_signal")
    if plan and plan.get("entry") is not None and cfg.show_levels:
        tag = ("BUY" if plan.get("action") == "buy" else "SELL") + (" ★" if plan.get("kind") == "sniper" else "")
        ft = plan.get("time")
        price_lines += [
            {"price": plan["entry"], "color": _C["white"], "style": "solid", "width": 2, "title": f"{tag} Entry", "from_time": ft},
            {"price": plan["stop_loss"], "color": _C["red"], "style": "solid", "width": 2, "title": "SL", "from_time": ft},
            {"price": plan["tp1"], "color": _C["green"], "style": "solid", "width": 1, "title": "TP1", "from_time": ft},
            {"price": plan["tp2"], "color": _C["green"], "style": "solid", "width": 2, "title": "TP2", "from_time": ft},
            {"price": plan["tp3"], "color": _C["green"], "style": "solid", "width": 2, "title": "TP3", "from_time": ft},
        ]

    hbands: List[Dict[str, Any]] = []
    pd_ = result.get("pd") or {}
    if cfg.show_pd and pd_.get("range_high") is not None:
        hbands += [
            {"top": pd_["range_high"], "bottom": pd_["equilibrium"], "color": "rgba(239,68,68,0.06)", "label": "PREMIUM"},
            {"top": pd_["equilibrium"], "bottom": pd_["range_low"], "color": "rgba(34,197,94,0.06)", "label": "DISCOUNT"},
        ]
        price_lines.append({"price": pd_["equilibrium"], "color": _C["yellow"], "style": "dashed", "width": 1, "title": "EQ"})

    lines: List[Dict[str, Any]] = []
    if rows and (cfg.show_ema or (cfg.show_vwap and cfg.use_vwap)):
        try:
            df = _to_dataframe(rows)
            tsx = (_ts_ms(df) // 1000).tolist()
            if cfg.show_ema:
                for period, col, name in ((cfg.fast_ema, _C["aqua"], "EMA fast"), (cfg.medium_ema, _C["yellow"], "EMA medium"),
                                          (cfg.slow_ema, _C["white"], "EMA slow"), (cfg.major_ema, _C["orange"], "EMA major")):
                    vals = _ema(df["close"], period).to_numpy(dtype=float)
                    lines.append({"name": name, "color": col, "width": 1,
                                  "points": [{"time": t, "value": float(x)} for t, x in zip(tsx, vals) if not math.isnan(x)]})
            if cfg.show_vwap and cfg.use_vwap:
                vv = df["volume"].to_numpy(dtype=float)
                if np.nansum(vv) > 0:
                    hh, ll, cc = df["high"].to_numpy(dtype=float), df["low"].to_numpy(dtype=float), df["close"].to_numpy(dtype=float)
                    day = (_ts_ms(df) // 86_400_000)
                    pts: List[Dict[str, float]] = []
                    cum_pv = cum_v = 0.0
                    last_day = None
                    for i in range(len(cc)):
                        if day[i] != last_day:
                            cum_pv = cum_v = 0.0
                            last_day = day[i]
                        cum_pv += (hh[i] + ll[i] + cc[i]) / 3 * vv[i]
                        cum_v += vv[i]
                        if cum_v > 0:
                            pts.append({"time": tsx[i], "value": cum_pv / cum_v})
                    lines.append({"name": "VWAP", "color": _C["fuchsia"], "width": 2, "points": pts})
        except Exception:  # noqa: BLE001 — lines are decoration
            pass

    band_color = {"ASIA": "rgba(59,130,246,0.07)", "LONDON": "rgba(234,179,8,0.07)",
                  "NEW YORK": "rgba(239,68,68,0.07)", "LONDON + NY": "rgba(249,115,22,0.09)"}
    bands = [{"start": b["start"], "end": b["end"], "label": b["name"], "color": band_color.get(b["name"], "rgba(148,163,184,0.05)")}
             for b in (result.get("session") or {}).get("bands") or []] if cfg.show_sessions else []

    dash = result.get("dashboard") or {}
    sc = result.get("score") or {}
    good = {"BULLISH", "BULL", "ABOVE", "DISCOUNT", "BULL OTE", "BULL FVG", "BUY SWEEP", "CLEAR"}
    bad = {"BEARISH", "BEAR", "BELOW", "PREMIUM", "BEAR OTE", "BEAR FVG", "SELL SWEEP", "BLOCKED"}

    def colour(k: str, val: str) -> str:
        if k == "Buy Score":
            return _C["lime"]
        if k == "Sell Score":
            return _C["red"]
        if k in ("Scalp Min", "Sniper Min"):
            return _C["yellow"]
        if k == "Signal":
            return _C["lime"] if val.startswith("BUY") else _C["red"] if val.startswith("SELL") else _C["gray"]
        if k == "Structure":
            return _C["lime"] if "↑" in val else _C["red"] if "↓" in val else _C["gray"]
        if k == "Volume":
            return _C["yellow"] if val == "VERY HIGH" else _C["orange"] if val == "HIGH" else _C["gray"]
        if val in good:
            return _C["lime"]
        if val in bad:
            return _C["red"]
        return _C["white"]

    dashboard = {"title": "OBLIVION MASTER v5", "rows": [[k, val, colour(k, val)] for k, val in dash.items()],
                 "score": {"buy": sc.get("buy"), "sell": sc.get("sell"), "scalp_min": sc.get("scalp_min"), "sniper_min": sc.get("sniper_min"),
                           "bull": sc.get("buy"), "bear": sc.get("sell"), "required": sc.get("required")}}
    return {"boxes": boxes, "labels": labels, "markers": markers, "price_lines": price_lines, "hbands": hbands, "lines": lines,
            "bands": bands, "dashboard": dashboard if cfg.show_dashboard else None, "summary": result.get("summary"),
            "outcomes": result.get("outcomes")}


__all__ = [
    "OblivionConfig", "TRADE_MODES", "ENGINE_VERSION", "oblivion_config_from_env", "current_config", "config_from_dict",
    "analyze_oblivion", "oblivion_score", "wait_reason", "compact_for_prompt", "oblivion_to_overlays",
    "is_news_blocked", "is_news_window", "session_state", "timeframe_seconds", "candles_to_rows", "MIN_BARS",
]
