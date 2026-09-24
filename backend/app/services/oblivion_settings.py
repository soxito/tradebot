"""Global OBLIVION MASTER v5 settings — one row, one cache, one field spec.

``FIELDS`` is the single source of truth: it validates writes here and it
renders the settings popup on the frontend (groups, labels, types, ranges,
hints), mirroring the Pine input groups 01–20.

Hot paths (the MT5 scalp bot every 10 s, every chart overlay request) call the
sync :func:`current_config`; it serves the cached ``OblivionConfig`` loaded at
startup and refreshed on every write, plus a periodic re-read so a change
made from another process lands within a minute.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.signals.oblivion import TRADE_MODES, OblivionConfig, _coerce, _KNOWN, oblivion_config_from_env

#: Re-read the row this often even without a local write (seconds).
REFRESH_S = 60.0

# ── field spec (group order = Pine groups) ────────────────────────────────────
# (key, label, type, extra) — type: int | float | bool | select | session | tz
_TZ_OPTIONS = ["Etc/UTC", "Europe/London", "America/New_York", "Africa/Johannesburg", "Asia/Tokyo"]
_HTF_OPTIONS = ["15m", "30m", "1h", "2h", "4h", "1d"]

FIELDS: List[Dict[str, Any]] = [
    # 01 master
    {"group": "01 Master", "key": "trade_mode", "label": "Trading Mode", "type": "select", "options": list(TRADE_MODES),
     "hint": "HYBRID fires sniper signals when they qualify and scalps otherwise."},
    {"group": "01 Master", "key": "confirmed_only", "label": "Confirmed Candle Only", "type": "bool"},
    {"group": "01 Master", "key": "allow_long", "label": "Allow BUY", "type": "bool"},
    {"group": "01 Master", "key": "allow_short", "label": "Allow SELL", "type": "bool"},
    {"group": "01 Master", "key": "avoid_duplicates", "label": "Avoid Duplicate Signals", "type": "bool",
     "hint": "No new signal in a direction while one in that direction is still open (no TP1 / SL yet)."},
    {"group": "01 Master", "key": "signal_cooldown", "label": "Signal Cooldown Bars", "type": "int", "min": 0, "max": 100},
    # 02 scalping
    {"group": "02 Scalping Engine", "key": "scalp_min_score", "label": "Minimum Scalping Score", "type": "int", "min": 3, "max": 20},
    {"group": "02 Scalping Engine", "key": "scalp_require_trend", "label": "Require EMA Trend", "type": "bool"},
    {"group": "02 Scalping Engine", "key": "scalp_require_vwap", "label": "Require VWAP Direction", "type": "bool"},
    # 03 sniper
    {"group": "03 Sniper Engine", "key": "sniper_min_score", "label": "Minimum Sniper Score", "type": "int", "min": 4, "max": 25},
    {"group": "03 Sniper Engine", "key": "sniper_require_htf", "label": "Require HTF Confirmation", "type": "bool"},
    {"group": "03 Sniper Engine", "key": "sniper_require_sweep", "label": "Require Liquidity Sweep", "type": "bool"},
    {"group": "03 Sniper Engine", "key": "sniper_require_zone", "label": "Require Premium / Discount Zone", "type": "bool"},
    {"group": "03 Sniper Engine", "key": "sniper_require_major_trend", "label": "Require EMA 200 Trend", "type": "bool",
     "hint": "v4 carry-over: a sniper must trade with the 200 EMA."},
    # 04 trend
    {"group": "04 Fast Trend", "key": "fast_ema", "label": "Fast EMA", "type": "int", "min": 2, "max": 50},
    {"group": "04 Fast Trend", "key": "medium_ema", "label": "Medium EMA", "type": "int", "min": 3, "max": 100},
    {"group": "04 Fast Trend", "key": "slow_ema", "label": "Slow EMA", "type": "int", "min": 10, "max": 200},
    {"group": "04 Fast Trend", "key": "show_ema", "label": "Show EMAs", "type": "bool"},
    {"group": "04 Fast Trend", "key": "use_major_trend", "label": "Use EMA 200 Major Trend", "type": "bool",
     "hint": "v4 carry-over: +1 to the side trading with the major trend."},
    {"group": "04 Fast Trend", "key": "major_ema", "label": "Major EMA", "type": "int", "min": 50, "max": 500},
    # 05 vwap
    {"group": "05 VWAP", "key": "use_vwap", "label": "Use VWAP", "type": "bool"},
    {"group": "05 VWAP", "key": "show_vwap", "label": "Show VWAP", "type": "bool"},
    # 06 volatility
    {"group": "06 Volatility", "key": "atr_length", "label": "ATR Length", "type": "int", "min": 5, "max": 100},
    {"group": "06 Volatility", "key": "movement_min_atr", "label": "Minimum Candle Movement ATR", "type": "float", "min": 0.01, "max": 5.0, "step": 0.01},
    # 08 volume
    {"group": "08 Volume Engine", "key": "use_volume", "label": "Use Volume", "type": "bool"},
    {"group": "08 Volume Engine", "key": "volume_length", "label": "Volume Average", "type": "int", "min": 5, "max": 200},
    {"group": "08 Volume Engine", "key": "volume_multiplier", "label": "High Volume Multiplier", "type": "float", "min": 0.5, "max": 5.0, "step": 0.05},
    {"group": "08 Volume Engine", "key": "very_high_multiplier", "label": "Very High Volume Multiplier", "type": "float", "min": 1.0, "max": 6.0, "step": 0.05,
     "hint": "v4 carry-over: an extra point on a climactic bar."},
    # 09 structure
    {"group": "09 Market Structure", "key": "micro_len", "label": "Micro Swing Length", "type": "int", "min": 2, "max": 20},
    {"group": "09 Market Structure", "key": "major_len", "label": "Major Swing Length", "type": "int", "min": 3, "max": 30},
    {"group": "09 Market Structure", "key": "show_bos", "label": "Show BOS / CHOCH", "type": "bool"},
    # 10 pd
    {"group": "10 Premium Discount", "key": "show_pd", "label": "Show Premium / Discount", "type": "bool"},
    {"group": "10 Premium Discount", "key": "pd_lookback", "label": "Dealing Range Lookback", "type": "int", "min": 20, "max": 500},
    # 11 fvg
    {"group": "11 Fair Value Gap", "key": "show_fvg", "label": "Show FVG", "type": "bool"},
    {"group": "11 Fair Value Gap", "key": "fvg_min_atr", "label": "Minimum FVG ATR", "type": "float", "min": 0.01, "max": 2.0, "step": 0.01},
    {"group": "11 Fair Value Gap", "key": "fvg_extension", "label": "FVG Extension", "type": "int", "min": 5, "max": 200},
    {"group": "11 Fair Value Gap", "key": "score_fvg_retest", "label": "Score FVG Retest", "type": "bool",
     "hint": "v4 carry-over: +1 while price sits inside an unfilled gap."},
    # 12 ob
    {"group": "12 Order Block", "key": "show_ob", "label": "Show Order Blocks", "type": "bool"},
    {"group": "12 Order Block", "key": "ob_lookback", "label": "OB Search", "type": "int", "min": 2, "max": 30},
    {"group": "12 Order Block", "key": "ob_extension", "label": "OB Extension", "type": "int", "min": 5, "max": 200},
    # 13 liquidity
    {"group": "13 Liquidity", "key": "liquidity_lookback", "label": "Liquidity Lookback", "type": "int", "min": 3, "max": 100},
    {"group": "13 Liquidity", "key": "sweep_recency", "label": "Sniper Sweep Recency (bars)", "type": "int", "min": 1, "max": 20,
     "hint": "A sweep within this many bars satisfies the sniper's sweep requirement."},
    # 14 ote
    {"group": "14 OTE", "key": "use_ote", "label": "Use OTE", "type": "bool"},
    {"group": "14 OTE", "key": "show_ote", "label": "Show OTE", "type": "bool"},
    {"group": "14 OTE", "key": "ote_618", "label": "OTE 0.618", "type": "float", "min": 0.5, "max": 0.9, "step": 0.001},
    {"group": "14 OTE", "key": "ote_705", "label": "OTE 0.705", "type": "float", "min": 0.5, "max": 0.9, "step": 0.001},
    {"group": "14 OTE", "key": "ote_786", "label": "OTE 0.786", "type": "float", "min": 0.5, "max": 0.9, "step": 0.001},
    {"group": "14 OTE", "key": "use_fib_zone", "label": "Score Fib 0.382–0.618 Zone", "type": "bool",
     "hint": "v4 carry-over: +1 in the retracement zone of the major swing."},
    # 15 htf
    {"group": "15 HTF Confirmation", "key": "use_htf", "label": "Use HTF Confirmation", "type": "bool"},
    {"group": "15 HTF Confirmation", "key": "htf_timeframe", "label": "Higher Timeframe", "type": "select", "options": _HTF_OPTIONS},
    {"group": "15 HTF Confirmation", "key": "htf_fast", "label": "HTF Fast EMA", "type": "int", "min": 2, "max": 200},
    {"group": "15 HTF Confirmation", "key": "htf_slow", "label": "HTF Slow EMA", "type": "int", "min": 5, "max": 300},
    # 16 sessions
    {"group": "16 Sessions", "key": "require_session", "label": "Require Active Session", "type": "bool"},
    {"group": "16 Sessions", "key": "score_session", "label": "Score Active Session (+1)", "type": "bool"},
    {"group": "16 Sessions", "key": "asia", "label": "Asia", "type": "session"},
    {"group": "16 Sessions", "key": "london", "label": "London", "type": "session"},
    {"group": "16 Sessions", "key": "new_york", "label": "New York", "type": "session"},
    {"group": "16 Sessions", "key": "session_tz", "label": "Timezone", "type": "select", "options": _TZ_OPTIONS},
    # 17 news
    {"group": "17 News Risk", "key": "news_enabled", "label": "Enable News Blackout", "type": "bool",
     "hint": "Hard gate: bots and Kronos execution refuse entries inside the window; room agents are told."},
    {"group": "17 News Risk", "key": "news_window", "label": "Manual News Window", "type": "session"},
    {"group": "17 News Risk", "key": "news_tz", "label": "News Timezone", "type": "select", "options": _TZ_OPTIONS},
    # 18 trade management
    {"group": "18 Trade Management", "key": "sl_atr_buffer", "label": "Structural SL ATR Buffer", "type": "float", "min": 0.0, "max": 3.0, "step": 0.05},
    {"group": "18 Trade Management", "key": "min_risk_atr", "label": "Minimum Risk ATR", "type": "float", "min": 0.1, "max": 5.0, "step": 0.05,
     "hint": "Stop floor. 0.25 sits inside one bar's noise — ≥ 1.0 ATR recommended for anything but M1 scalps."},
    {"group": "18 Trade Management", "key": "max_risk_atr", "label": "Maximum Risk ATR", "type": "float", "min": 0.5, "max": 10.0, "step": 0.1},
    {"group": "18 Trade Management", "key": "tp1_rr", "label": "TP1 R:R", "type": "float", "min": 0.1, "max": 10.0, "step": 0.1},
    {"group": "18 Trade Management", "key": "tp2_rr", "label": "TP2 R:R", "type": "float", "min": 0.1, "max": 10.0, "step": 0.1},
    {"group": "18 Trade Management", "key": "tp3_rr", "label": "TP3 R:R", "type": "float", "min": 0.1, "max": 10.0, "step": 0.1},
    {"group": "18 Trade Management", "key": "require_room_tp3", "label": "Require Room To TP3", "type": "bool",
     "hint": "Skip the signal when TP3 sits beyond the dealing-range extreme."},
    # 19 visuals
    {"group": "19 Visuals", "key": "show_levels", "label": "Show Entry / SL / TP", "type": "bool"},
    {"group": "19 Visuals", "key": "level_extension", "label": "Level Extension", "type": "int", "min": 5, "max": 200},
    {"group": "19 Visuals", "key": "show_labels", "label": "Show Signal Labels", "type": "bool"},
    {"group": "19 Visuals", "key": "show_sessions", "label": "Session Background", "type": "bool"},
    {"group": "19 Visuals", "key": "show_dashboard", "label": "Show Dashboard", "type": "bool"},
    # 20 outcomes
    {"group": "20 Outcomes", "key": "show_outcomes", "label": "Show TP / SL Outcomes", "type": "bool"},
]
_FIELD_BY_KEY = {f["key"]: f for f in FIELDS}
assert set(_FIELD_BY_KEY) <= _KNOWN, sorted(set(_FIELD_BY_KEY) - _KNOWN)


def defaults() -> Dict[str, Any]:
    return asdict(oblivion_config_from_env())


def validate(patch: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce + range-check a partial settings dict. Raises ValueError."""
    clean: Dict[str, Any] = {}
    errors: List[str] = []
    for key, raw in (patch or {}).items():
        spec = _FIELD_BY_KEY.get(key)
        if spec is None:
            if key in _KNOWN:
                val = _coerce(key, raw)
                if val is not None:
                    clean[key] = val
            continue  # unknown keys are ignored, not fatal
        t = spec["type"]
        val = _coerce(key, raw)
        if val is None:
            if t == "select":
                errors.append(f"{key}: {raw!r} is not one of {spec['options']}")
            else:
                errors.append(f"{key}: cannot read {raw!r}")
            continue
        if t in ("int", "float"):
            lo, hi = spec.get("min"), spec.get("max")
            if lo is not None and val < lo:
                errors.append(f"{key}: {val} is below the minimum {lo}")
                continue
            if hi is not None and val > hi:
                errors.append(f"{key}: {val} is above the maximum {hi}")
                continue
        elif t == "select" and val not in spec["options"]:
            errors.append(f"{key}: {val!r} is not one of {spec['options']}")
            continue
        elif t == "session":
            s = str(val).strip()
            ok = len(s) == 9 and s[4] == "-" and s[:4].isdigit() and s[5:].isdigit() \
                and int(s[:2]) < 24 and int(s[2:4]) < 60 and int(s[5:7]) < 24 and int(s[7:9]) < 60
            if not ok:
                errors.append(f"{key}: use HHMM-HHMM, got {val!r}")
                continue
            val = s
        clean[key] = val
    if errors:
        raise ValueError("; ".join(errors))
    if "scalp_min_score" in clean or "sniper_min_score" in clean:
        pass  # both minimums are independent; HYBRID handles either ordering
    return clean


# ── cache ─────────────────────────────────────────────────────────────────────
_state: Dict[str, Any] = {"cfg": None, "version": 0, "loaded_at": 0.0, "updated_at": None}


def current_config() -> Optional[OblivionConfig]:
    """Sync accessor for the hot paths. None until the first load."""
    cfg = _state.get("cfg")
    if cfg is not None and time.monotonic() - _state["loaded_at"] > REFRESH_S:
        _schedule_refresh()
    return cfg


def current_version() -> int:
    return int(_state.get("version") or 0)


def _schedule_refresh() -> None:
    """Kick a background re-read when an event loop is running; never blocks."""
    _state["loaded_at"] = time.monotonic()  # debounce
    try:
        import asyncio

        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_refresh_task())


async def _refresh_task() -> None:
    try:
        from app.core.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            await load(db)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Oblivion] settings refresh skipped: {}", exc)


def _apply(settings: Dict[str, Any], version: int, updated_at: Any) -> OblivionConfig:
    cfg = oblivion_config_from_env()
    for k, v in (settings or {}).items():
        if k in _KNOWN:
            val = _coerce(k, v)
            if val is not None:
                setattr(cfg, k, val)
    _state.update(cfg=cfg, version=int(version or 1), loaded_at=time.monotonic(), updated_at=updated_at)
    return cfg


async def _row(db: AsyncSession):
    from app.models.database import OblivionSettings

    row = (await db.execute(select(OblivionSettings).where(OblivionSettings.id == 1))).scalar_one_or_none()
    if row is None:
        row = OblivionSettings(id=1, settings=json.dumps(defaults()), version=1)
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


async def load(db: AsyncSession) -> Dict[str, Any]:
    """Read the row (creating it from defaults), refresh the cache, return the doc."""
    row = await _row(db)
    try:
        stored = json.loads(row.settings or "{}")
    except json.JSONDecodeError:
        stored = {}
    cfg = _apply(stored, row.version, row.updated_at)
    return document(cfg, row.version, row.updated_at)


async def update(db: AsyncSession, patch: Dict[str, Any]) -> Dict[str, Any]:
    """Validate + merge a partial patch, persist, refresh the cache, invalidate dependents."""
    clean = validate(patch)
    row = await _row(db)
    try:
        stored = json.loads(row.settings or "{}")
    except json.JSONDecodeError:
        stored = {}
    stored.update(clean)
    row.settings = json.dumps(stored)
    row.version = int(row.version or 1) + 1
    await db.commit()
    await db.refresh(row)
    cfg = _apply(stored, row.version, row.updated_at)
    _invalidate_dependents()
    logger.info("[Oblivion] settings v{} updated: {}", row.version, ", ".join(sorted(clean)))
    return document(cfg, row.version, row.updated_at)


async def reset(db: AsyncSession) -> Dict[str, Any]:
    row = await _row(db)
    row.settings = json.dumps(defaults())
    row.version = int(row.version or 1) + 1
    await db.commit()
    await db.refresh(row)
    cfg = _apply(defaults(), row.version, row.updated_at)
    _invalidate_dependents()
    logger.info("[Oblivion] settings reset to defaults (v{})", row.version)
    return document(cfg, row.version, row.updated_at)


def _invalidate_dependents() -> None:
    """Caches that embed an engine read must not outlive a settings change."""
    try:
        from plugins.KronosForecastPlugin.backend.services import forecast_service

        cache = getattr(forecast_service, "_cache", None)
        if hasattr(cache, "clear"):
            cache.clear()
    except Exception:  # noqa: BLE001
        pass


def document(cfg: OblivionConfig, version: int, updated_at: Any = None) -> Dict[str, Any]:
    return {
        "settings": asdict(cfg),
        "defaults": defaults(),
        "fields": FIELDS,
        "version": int(version or 1),
        "updated_at": updated_at.isoformat() if hasattr(updated_at, "isoformat") else updated_at,
        "engine_version": "5.0",
    }
