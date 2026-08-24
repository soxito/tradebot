"""
Strategies & Pine Script API Routes
- CRUD for bot signal strategies (indicator combos, thresholds, actions)
- CRUD for Pine Script templates (auto-generated from strategy config)
- Strategy-based signal analysis: run strategy indicators on OHLCV data
"""
import json
import math
import re
from typing import Optional, List, Tuple, Dict, Any
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from app.core.database import get_db
from app.core.timezone import now_sast
from app.models.database import BotStrategy, PineScript
from app.exchanges.manager import exchange_manager, SupportedExchange
from app.signals.technical import (
    ohlcv_to_dataframe, sma, ema, rsi, macd, bollinger_bands,
    buy_sell_volume, adx, stochastic_rsi, atr,
    support_resistance_mtf, pivot_highs, pivot_lows,
    auto_fib_retracement, zigzag_pivots, fib_confluence_score,
)
import numpy as np
import pandas as pd
from loguru import logger

router = APIRouter(prefix="/strategies", tags=["strategies"])


# ──────────── Request / Response schemas ────────────

class IndicatorConfig(BaseModel):
    name: str  # rsi, macd, bollinger, ema_cross, stoch_rsi, adx, volume
    enabled: bool = True
    params: dict = {}  # e.g. {"period": 14, "overbought": 70, "oversold": 30}
    weight: float = 1.0  # weighting in combined score


class StrategyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = ""
    pairs: List[str] = Field(default_factory=lambda: ["BTC/USDT"])
    timeframe: str = "1h"
    indicators: List[IndicatorConfig] = Field(default_factory=list)
    buy_threshold: float = Field(default=0.25, ge=-1.0, le=1.0)
    sell_threshold: float = Field(default=-0.25, ge=-1.0, le=1.0)
    stop_loss_pct: float = Field(default=2.0, ge=0.0, le=50.0)
    take_profit_pct: float = Field(default=4.0, ge=0.0, le=100.0)
    trade_type: str = "spot"  # spot / futures
    leverage: int = Field(default=1, ge=1, le=125)
    is_active: bool = False


class StrategyUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    pairs: Optional[List[str]] = None
    timeframe: Optional[str] = None
    indicators: Optional[List[IndicatorConfig]] = None
    buy_threshold: Optional[float] = None
    sell_threshold: Optional[float] = None
    stop_loss_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None
    trade_type: Optional[str] = None
    leverage: Optional[int] = None
    is_active: Optional[bool] = None


class PineScriptCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = ""
    strategy_id: Optional[int] = None
    script_type: str = "indicator"  # indicator / strategy
    code: str = ""
    pairs: List[str] = Field(default_factory=lambda: ["BTC/USDT"])
    is_active: bool = False


class PineScriptUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    script_type: Optional[str] = None
    code: Optional[str] = None
    pairs: Optional[List[str]] = None
    is_active: Optional[bool] = None


def _strategy_to_dict(s: BotStrategy) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "description": s.description,
        "pairs": json.loads(s.pairs) if s.pairs else [],
        "timeframe": s.timeframe,
        "indicators": json.loads(s.indicators) if s.indicators else [],
        "buy_threshold": s.buy_threshold,
        "sell_threshold": s.sell_threshold,
        "stop_loss_pct": s.stop_loss_pct,
        "take_profit_pct": s.take_profit_pct,
        "trade_type": s.trade_type,
        "leverage": s.leverage,
        "is_active": s.is_active,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


def _pine_to_dict(p: PineScript) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "strategy_id": p.strategy_id,
        "script_type": p.script_type,
        "code": p.code,
        "pairs": json.loads(p.pairs) if p.pairs else [],
        "is_active": p.is_active or False,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


# ──────────── Strategy CRUD ────────────

@router.get("/")
async def list_strategies(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(BotStrategy).order_by(BotStrategy.updated_at.desc()))
    return [_strategy_to_dict(s) for s in result.scalars().all()]


@router.post("/")
async def create_strategy(req: StrategyCreate, db: AsyncSession = Depends(get_db)):
    s = BotStrategy(
        name=req.name,
        description=req.description,
        pairs=json.dumps(req.pairs),
        timeframe=req.timeframe,
        indicators=json.dumps([ind.dict() for ind in req.indicators]),
        buy_threshold=req.buy_threshold,
        sell_threshold=req.sell_threshold,
        stop_loss_pct=req.stop_loss_pct,
        take_profit_pct=req.take_profit_pct,
        trade_type=req.trade_type,
        leverage=req.leverage,
        is_active=req.is_active,
    )
    db.add(s)
    await db.commit()
    await db.refresh(s)
    logger.info(f"Created strategy: {s.name} (id={s.id})")
    return _strategy_to_dict(s)


@router.get("/{strategy_id}")
async def get_strategy(strategy_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(BotStrategy).where(BotStrategy.id == strategy_id))
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return _strategy_to_dict(s)


@router.put("/{strategy_id}")
async def update_strategy(strategy_id: int, req: StrategyUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(BotStrategy).where(BotStrategy.id == strategy_id))
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Strategy not found")

    if req.name is not None:
        s.name = req.name
    if req.description is not None:
        s.description = req.description
    if req.pairs is not None:
        s.pairs = json.dumps(req.pairs)
    if req.timeframe is not None:
        s.timeframe = req.timeframe
    if req.indicators is not None:
        s.indicators = json.dumps([ind.dict() for ind in req.indicators])
    if req.buy_threshold is not None:
        s.buy_threshold = req.buy_threshold
    if req.sell_threshold is not None:
        s.sell_threshold = req.sell_threshold
    if req.stop_loss_pct is not None:
        s.stop_loss_pct = req.stop_loss_pct
    if req.take_profit_pct is not None:
        s.take_profit_pct = req.take_profit_pct
    if req.trade_type is not None:
        s.trade_type = req.trade_type
    if req.leverage is not None:
        s.leverage = req.leverage
    if req.is_active is not None:
        s.is_active = req.is_active
    s.updated_at = now_sast()

    await db.commit()
    await db.refresh(s)
    return _strategy_to_dict(s)


@router.delete("/{strategy_id}")
async def delete_strategy(strategy_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(BotStrategy).where(BotStrategy.id == strategy_id))
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Strategy not found")
    await db.delete(s)
    await db.commit()
    return {"ok": True}


# ──────────── Pine Script CRUD ────────────

@router.get("/pinescripts/all")
async def list_pinescripts(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PineScript).order_by(PineScript.updated_at.desc()))
    return [_pine_to_dict(p) for p in result.scalars().all()]


@router.post("/pinescripts")
async def create_pinescript(req: PineScriptCreate, db: AsyncSession = Depends(get_db)):
    p = PineScript(
        name=req.name,
        description=req.description,
        strategy_id=req.strategy_id,
        script_type=req.script_type,
        code=req.code,
        pairs=json.dumps(req.pairs),
        is_active=req.is_active,
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    logger.info(f"Created Pine Script: {p.name} (id={p.id})")
    return _pine_to_dict(p)


@router.get("/pinescripts/{script_id}")
async def get_pinescript(script_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PineScript).where(PineScript.id == script_id))
    p = result.scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Pine Script not found")
    return _pine_to_dict(p)


@router.put("/pinescripts/{script_id}")
async def update_pinescript(script_id: int, req: PineScriptUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PineScript).where(PineScript.id == script_id))
    p = result.scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Pine Script not found")
    if req.name is not None:
        p.name = req.name
    if req.description is not None:
        p.description = req.description
    if req.script_type is not None:
        p.script_type = req.script_type
    if req.code is not None:
        p.code = req.code
    if req.pairs is not None:
        p.pairs = json.dumps(req.pairs)
    if req.is_active is not None:
        p.is_active = req.is_active
    p.updated_at = now_sast()
    await db.commit()
    await db.refresh(p)
    return _pine_to_dict(p)


@router.delete("/pinescripts/{script_id}")
async def delete_pinescript(script_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PineScript).where(PineScript.id == script_id))
    p = result.scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Pine Script not found")
    await db.delete(p)
    await db.commit()
    return {"ok": True}


# ──────────── Pine Script Code Parser ────────────

def _resolve_variable(code: str, var_name: str) -> Optional[int]:
    """Try to resolve a variable's numeric default from input.int() or assignment."""
    # Check input.int(default, ...) or input.int(N, ...)
    pattern = rf'{re.escape(var_name)}\s*=\s*input\.int\s*\(\s*(\d+)'
    m = re.search(pattern, code, re.IGNORECASE)
    if m:
        return int(m.group(1))
    # Check simple assignment: varName = N
    pattern = rf'{re.escape(var_name)}\s*=\s*(\d+)'
    m = re.search(pattern, code, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def _parse_pinescript_to_indicators(code: str) -> List[dict]:
    """
    Parse Pine Script v5/v6 code and extract indicator configs in the same
    format as BotStrategy indicators. Supports:
      - ta.rsi, ta.macd, ta.bb, ta.ema, ta.sma, ta.stoch, ta.dmi, ta.atr, volume
    Also resolves variable parameters via input.int() defaults.
    Returns list of IndicatorConfig-style dicts.
    """
    indicators = []
    code_lower = code.lower()

    # Smart Money Concepts [LuxAlgo] is a complex script that relies on
    # Pine-only objects (boxes/labels/lines). Route it to a dedicated visual
    # approximation path for the custom chart renderer.
    smc_hint = bool(
        re.search(
            r"smart\s+money\s+concepts|luxalgo\s*[-\]]\s*smart\s+money\s+concepts",
            code,
            re.IGNORECASE,
        )
    )
    if smc_hint:
        return [
            {
                "name": "smc_lux",
                "enabled": True,
                "weight": 1.4,
                "params": {
                    "pivot_left": 5,
                    "pivot_right": 5,
                    "eq_threshold_atr": 0.12,
                    "strong_weak_lookback": 140,
                    "max_zones": 6,
                },
            }
        ]

    # RSI: ta.rsi(source, period) — period can be number or variable
    rsi_matches = re.findall(r'ta\.rsi\s*\(\s*[^,]+,\s*(\w+)\s*\)', code, re.IGNORECASE)
    rsi_period = None
    for m in rsi_matches:
        if m.isdigit():
            rsi_period = int(m)
            break
        resolved = _resolve_variable(code, m)
        if resolved:
            rsi_period = resolved
            break
    if rsi_period:
        ob, os_ = 70, 30
        ob_match = re.search(r'(?:overbought|ob)\s*[=:]\s*(\d+)', code, re.IGNORECASE)
        os_match = re.search(r'(?:oversold|os)\s*[=:]\s*(\d+)', code, re.IGNORECASE)
        if ob_match: ob = int(ob_match.group(1))
        if os_match: os_ = int(os_match.group(1))
        rsi_gt = re.search(r'rsi\w*\s*>\s*(\d+)', code, re.IGNORECASE)
        rsi_lt = re.search(r'rsi\w*\s*<\s*(\d+)', code, re.IGNORECASE)
        if rsi_gt: ob = int(rsi_gt.group(1))
        if rsi_lt: os_ = int(rsi_lt.group(1))
        indicators.append({
            "name": "rsi", "enabled": True, "weight": 1.0,
            "params": {"period": rsi_period, "overbought": ob, "oversold": os_},
        })

    # MACD: ta.macd(source, fast, slow, signal) — all numeric
    macd_matches = re.findall(
        r'ta\.macd\s*\(\s*[^,]+,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)',
        code, re.IGNORECASE,
    )
    if macd_matches:
        fast, slow, sig = int(macd_matches[0][0]), int(macd_matches[0][1]), int(macd_matches[0][2])
        indicators.append({
            "name": "macd", "enabled": True, "weight": 1.0,
            "params": {"fast": fast, "slow": slow, "signal": sig},
        })

    # Bollinger Bands: ta.bb(source, period, mult)
    bb_matches = re.findall(
        r'ta\.bb\s*\(\s*[^,]+,\s*(\d+)\s*,\s*([\d.]+)\s*\)',
        code, re.IGNORECASE,
    )
    if bb_matches:
        period, mult = int(bb_matches[0][0]), float(bb_matches[0][1])
        indicators.append({
            "name": "bollinger", "enabled": True, "weight": 1.0,
            "params": {"period": period, "mult": mult},
        })

    # EMA: ta.ema(source, length) — length can be number or variable
    ema_raw = re.findall(r'ta\.ema\s*\(\s*[^,]+,\s*(\w+)\s*\)', code, re.IGNORECASE)
    ema_periods = set()
    for m in ema_raw:
        if m.isdigit():
            ema_periods.add(int(m))
        else:
            resolved = _resolve_variable(code, m)
            if resolved:
                ema_periods.add(resolved)
    ema_periods = sorted(ema_periods)
    if len(ema_periods) >= 2:
        indicators.append({
            "name": "ema_cross", "enabled": True, "weight": 1.0,
            "params": {"fast": ema_periods[0], "slow": ema_periods[-1]},
        })
    elif len(ema_periods) == 1:
        indicators.append({
            "name": "ema_cross", "enabled": True, "weight": 1.0,
            "params": {"fast": ema_periods[0], "slow": 200},
        })

    # SMA: ta.sma(source, length) — used for moving average overlays
    sma_raw = re.findall(r'ta\.sma\s*\(\s*(?!volume)[^,]+,\s*(\w+)\s*\)', code, re.IGNORECASE)
    sma_periods = set()
    for m in sma_raw:
        if m.isdigit():
            sma_periods.add(int(m))
        else:
            resolved = _resolve_variable(code, m)
            if resolved:
                sma_periods.add(resolved)
    # If SMA pairs found (and no EMA already), treat as EMA cross equivalent
    sma_periods = sorted(sma_periods)
    if len(sma_periods) >= 2 and not ema_periods:
        indicators.append({
            "name": "ema_cross", "enabled": True, "weight": 1.0,
            "params": {"fast": sma_periods[0], "slow": sma_periods[-1]},
        })

    # Stochastic RSI: ta.stoch(...)
    stoch_matches = re.findall(r'ta\.stoch\s*\(', code, re.IGNORECASE)
    if stoch_matches:
        period = 14
        stoch_period = re.findall(r'ta\.stoch\s*\([^)]*?(\d+)', code, re.IGNORECASE)
        if stoch_period:
            period = int(stoch_period[0])
        indicators.append({
            "name": "stoch_rsi", "enabled": True, "weight": 1.0,
            "params": {"period": period, "overbought": 80, "oversold": 20},
        })

    # ADX: ta.dmi(period, ...) or ta.adx(...)
    adx_matches = re.findall(r'ta\.(?:dmi|adx)\s*\(\s*(\w+)', code, re.IGNORECASE)
    adx_period = None
    for m in adx_matches:
        if m.isdigit():
            adx_period = int(m)
            break
        resolved = _resolve_variable(code, m)
        if resolved:
            adx_period = resolved
            break
    if adx_period:
        threshold = 25
        th_match = re.search(r'adx\w*\s*>\s*(\d+)', code, re.IGNORECASE)
        if th_match: threshold = int(th_match.group(1))
        indicators.append({
            "name": "adx", "enabled": True, "weight": 1.0,
            "params": {"period": adx_period, "threshold": threshold},
        })

    # ATR: ta.atr(period) — treat as volatility context, maps to volume-like scoring
    atr_matches = re.findall(r'ta\.atr\s*\(\s*(\w+)\s*\)', code, re.IGNORECASE)
    # ATR detected but not directly mapped — we note it for context

    # Support & Resistance / Pivot: ta.pivothigh, ta.pivotlow, or S/R keywords
    has_pivot = bool(re.search(r'ta\.pivot(?:high|low)\s*\(', code, re.IGNORECASE))
    has_sr = bool(re.search(r'support.*resistance|resistance.*support|sr_?mtf|s_?r_?level', code, re.IGNORECASE))
    if has_pivot or has_sr:
        left = 5
        right = 5
        left_m = re.findall(r'ta\.pivot(?:high|low)\s*\([^,]*,\s*(\w+)', code, re.IGNORECASE)
        right_m = re.findall(r'ta\.pivot(?:high|low)\s*\([^,]*,\s*\w+\s*,\s*(\w+)', code, re.IGNORECASE)
        if left_m:
            v = left_m[0]
            left = int(v) if v.isdigit() else (_resolve_variable(code, v) or 5)
        if right_m:
            v = right_m[0]
            right = int(v) if v.isdigit() else (_resolve_variable(code, v) or 5)
        indicators.append({
            "name": "support_resistance", "enabled": True, "weight": 1.2,
            "params": {"pivot_left": left, "pivot_right": right, "max_levels": 8},
        })

    # Auto Fib Retracement: zigzag(deviation, depth) or fib retracement keywords
    has_zigzag = bool(re.search(r'zigzag|zig_zag', code, re.IGNORECASE))
    has_fib = bool(re.search(r'fib(?:onacci)?[\s._]?retrac|fib[\s._]?level|golden[\s._]?zone', code, re.IGNORECASE))
    if has_zigzag or has_fib:
        deviation = 5.0
        depth = 10
        dev_m = re.search(r'deviation["\']?\s*[:=,]\s*([\d.]+)', code, re.IGNORECASE)
        if dev_m:
            deviation = float(dev_m.group(1))
        depth_m = re.search(r'depth["\']?\s*[:=,]\s*(\d+)', code, re.IGNORECASE)
        if depth_m:
            depth = int(depth_m.group(1))
        indicators.append({
            "name": "fib_retracement", "enabled": True, "weight": 1.0,
            "params": {"deviation_pct": deviation, "depth": depth, "extend_lines": True},
        })

    # Volume: ta.sma(volume, N) or any volume reference
    vol_matches = re.findall(r'ta\.sma\s*\(\s*volume\s*,\s*(\d+)\s*\)', code, re.IGNORECASE)
    has_volume_ref = bool(re.search(r'\bvolume\b', code, re.IGNORECASE))
    if vol_matches or has_volume_ref:
        period = int(vol_matches[0]) if vol_matches else 20
        mult = 1.5
        mult_match = re.search(r'volume\s*>\s*\w+\s*\*\s*([\d.]+)', code, re.IGNORECASE)
        if mult_match: mult = float(mult_match.group(1))
        # Only add volume indicator if there's meaningful volume logic, not just casual mention
        if vol_matches or re.search(r'volume\s*[><=]', code, re.IGNORECASE):
            indicators.append({
                "name": "volume", "enabled": True, "weight": 1.0,
                "params": {"period": period, "mult": mult},
            })

    return indicators


def _constant_line_data(
    timestamps: pd.Series,
    value: float,
    start_idx: int = 0,
    end_idx: Optional[int] = None,
) -> List[dict]:
    if end_idx is None:
        end_idx = len(timestamps) - 1
    if len(timestamps) == 0:
        return []
    start_idx = max(0, min(start_idx, len(timestamps) - 1))
    end_idx = max(start_idx, min(end_idx, len(timestamps) - 1))
    out = []
    for i in range(start_idx, end_idx + 1):
        out.append({"time": int(timestamps.iloc[i].timestamp()), "value": round(float(value), 6)})
    return out


def _zone_overlay(
    name: str,
    timestamps: pd.Series,
    top: float,
    bottom: float,
    start_idx: int,
    color: str,
    stripes: int = 6,
) -> Optional[dict]:
    if len(timestamps) == 0:
        return None
    if top < bottom:
        top, bottom = bottom, top
    if top <= bottom:
        return None
    main_data = _constant_line_data(timestamps, top, start_idx)
    if not main_data:
        return None
    extra_lines = []
    for i in range(1, max(1, stripes) + 1):
        lvl = top - ((top - bottom) * (i / (stripes + 1)))
        extra_lines.append(
            {
                "name": f"{name} fill {i}",
                "color": color,
                "data": _constant_line_data(timestamps, lvl, start_idx),
            }
        )
    extra_lines.append(
        {
            "name": f"{name} bottom",
            "color": color,
            "data": _constant_line_data(timestamps, bottom, start_idx),
        }
    )
    return {
        "name": name,
        "type": "line",
        "pane": "main",
        "color": color,
        "lineWidth": 2,
        "lineStyle": 0,
        "data": main_data,
        "extra_lines": extra_lines,
    }


def _evaluate_smc_lux(
    df: pd.DataFrame,
    timestamps: pd.Series,
    params: Dict[str, Any],
) -> Tuple[List[dict], List[dict], float, Dict[str, Any]]:
    overlays: List[dict] = []
    markers: List[dict] = []
    values: Dict[str, Any] = {}

    if len(df) < 40:
        return overlays, markers, 0.0, values

    pl = int(params.get("pivot_left", 5))
    pr = int(params.get("pivot_right", 5))
    eq_threshold_atr = float(params.get("eq_threshold_atr", 0.12))
    lookback = int(params.get("strong_weak_lookback", 140))
    max_zones = int(params.get("max_zones", 6))

    high_pivots = pivot_highs(df["high"], left=pl, right=pr)
    low_pivots = pivot_lows(df["low"], left=pl, right=pr)
    atr_vals = atr(df, 14)
    if atr_vals.isna().all():
        atr_vals = (df["high"] - df["low"]).rolling(14).mean().fillna(method="bfill").fillna(method="ffill")

    high_points: List[Tuple[int, float]] = []
    low_points: List[Tuple[int, float]] = []
    for i in range(len(df)):
        hv = high_pivots.iloc[i]
        lv = low_pivots.iloc[i]
        if not np.isnan(hv):
            high_points.append((i, float(hv)))
        if not np.isnan(lv):
            low_points.append((i, float(lv)))

    # Equal highs / lows (EQH/EQL)
    for i in range(1, len(high_points)):
        i1, p1 = high_points[i - 1]
        i2, p2 = high_points[i]
        if i2 - i1 < max(2, pr):
            continue
        atr_ref = float(atr_vals.iloc[i2]) if not np.isnan(atr_vals.iloc[i2]) else 0.0
        tol = max(atr_ref * eq_threshold_atr, abs(p2) * 0.00025)
        if abs(p1 - p2) <= tol:
            lvl = (p1 + p2) / 2
            overlays.append(
                {
                    "name": f"EQH ({lvl:.2f})",
                    "type": "line",
                    "pane": "main",
                    "color": "rgba(239,68,68,0.75)",
                    "lineWidth": 1,
                    "lineStyle": 2,
                    "data": _constant_line_data(timestamps, lvl, i1, i2),
                }
            )
            mid = i1 + (i2 - i1) // 2
            markers.append(
                {
                    "time": int(timestamps.iloc[mid].timestamp()),
                    "position": "aboveBar",
                    "color": "#ef4444",
                    "shape": "circle",
                    "text": "EQH",
                }
            )

    for i in range(1, len(low_points)):
        i1, p1 = low_points[i - 1]
        i2, p2 = low_points[i]
        if i2 - i1 < max(2, pr):
            continue
        atr_ref = float(atr_vals.iloc[i2]) if not np.isnan(atr_vals.iloc[i2]) else 0.0
        tol = max(atr_ref * eq_threshold_atr, abs(p2) * 0.00025)
        if abs(p1 - p2) <= tol:
            lvl = (p1 + p2) / 2
            overlays.append(
                {
                    "name": f"EQL ({lvl:.2f})",
                    "type": "line",
                    "pane": "main",
                    "color": "rgba(20,184,166,0.75)",
                    "lineWidth": 1,
                    "lineStyle": 2,
                    "data": _constant_line_data(timestamps, lvl, i1, i2),
                }
            )
            mid = i1 + (i2 - i1) // 2
            markers.append(
                {
                    "time": int(timestamps.iloc[mid].timestamp()),
                    "position": "belowBar",
                    "color": "#14b8a6",
                    "shape": "circle",
                    "text": "EQL",
                }
            )

    # Structure breaks (BOS/CHoCH) + zone-style overlays
    trend_bias = 0
    used_high_breaks = set()
    used_low_breaks = set()
    last_high: Optional[Tuple[int, float]] = None
    last_low: Optional[Tuple[int, float]] = None
    zones_created = 0

    for i in range(1, len(df)):
        hp = high_pivots.iloc[i]
        lp = low_pivots.iloc[i]
        if not np.isnan(hp):
            last_high = (i, float(hp))
        if not np.isnan(lp):
            last_low = (i, float(lp))

        close_now = float(df["close"].iloc[i])
        close_prev = float(df["close"].iloc[i - 1])
        atr_now = float(atr_vals.iloc[i]) if not np.isnan(atr_vals.iloc[i]) else max(close_now * 0.002, 1e-6)

        if last_high and last_high[0] not in used_high_breaks and i > (last_high[0] + pr):
            level = last_high[1]
            if close_prev <= level and close_now > level:
                tag = "CHoCH" if trend_bias == -1 else "BOS"
                trend_bias = 1
                used_high_breaks.add(last_high[0])
                overlays.append(
                    {
                        "name": f"{tag} Bull ({level:.2f})",
                        "type": "line",
                        "pane": "main",
                        "color": "rgba(20,184,166,0.95)",
                        "lineWidth": 2,
                        "lineStyle": 2,
                        "data": _constant_line_data(timestamps, level, last_high[0], i),
                    }
                )
                markers.append(
                    {
                        "time": int(timestamps.iloc[i].timestamp()),
                        "position": "belowBar",
                        "color": "#14b8a6",
                        "shape": "arrowUp",
                        "text": tag,
                    }
                )
                if last_low and last_low[0] < i and zones_created < max_zones:
                    zone_low = last_low[1]
                    zone_high = zone_low + max(atr_now * 0.9, abs(close_now - zone_low) * 0.3)
                    zone = _zone_overlay(
                        "Bullish OB",
                        timestamps,
                        zone_high,
                        zone_low,
                        last_low[0],
                        "rgba(37,99,235,0.38)",
                        stripes=6,
                    )
                    if zone:
                        overlays.append(zone)
                        zones_created += 1

        if last_low and last_low[0] not in used_low_breaks and i > (last_low[0] + pr):
            level = last_low[1]
            if close_prev >= level and close_now < level:
                tag = "CHoCH" if trend_bias == 1 else "BOS"
                trend_bias = -1
                used_low_breaks.add(last_low[0])
                overlays.append(
                    {
                        "name": f"{tag} Bear ({level:.2f})",
                        "type": "line",
                        "pane": "main",
                        "color": "rgba(239,68,68,0.95)",
                        "lineWidth": 2,
                        "lineStyle": 2,
                        "data": _constant_line_data(timestamps, level, last_low[0], i),
                    }
                )
                markers.append(
                    {
                        "time": int(timestamps.iloc[i].timestamp()),
                        "position": "aboveBar",
                        "color": "#ef4444",
                        "shape": "arrowDown",
                        "text": tag,
                    }
                )
                if last_high and last_high[0] < i and zones_created < max_zones:
                    zone_high = last_high[1]
                    zone_low = zone_high - max(atr_now * 0.9, abs(zone_high - close_now) * 0.3)
                    zone = _zone_overlay(
                        "Bearish OB",
                        timestamps,
                        zone_high,
                        zone_low,
                        last_high[0],
                        "rgba(185,28,28,0.34)",
                        stripes=6,
                    )
                    if zone:
                        overlays.append(zone)
                        zones_created += 1

    # Strong/weak highs & lows over recent window
    window = min(max(40, lookback), len(df))
    start_idx = max(0, len(df) - window)
    recent_highs = df["high"].iloc[start_idx:]
    recent_lows = df["low"].iloc[start_idx:]

    strong_high_idx = int(recent_highs.idxmax())
    strong_low_idx = int(recent_lows.idxmin())
    strong_high = float(df["high"].iloc[strong_high_idx])
    strong_low = float(df["low"].iloc[strong_low_idx])

    overlays.append(
        {
            "name": "Weak High",
            "type": "line",
            "pane": "main",
            "color": "rgba(239,68,68,0.65)",
            "lineWidth": 2,
            "lineStyle": 0,
            "data": _constant_line_data(timestamps, strong_high, strong_high_idx),
        }
    )
    overlays.append(
        {
            "name": "Strong Low",
            "type": "line",
            "pane": "main",
            "color": "rgba(20,184,166,0.7)",
            "lineWidth": 2,
            "lineStyle": 0,
            "data": _constant_line_data(timestamps, strong_low, strong_low_idx),
        }
    )

    markers.append(
        {
            "time": int(timestamps.iloc[-1].timestamp()),
            "position": "aboveBar",
            "color": "#ef4444",
            "shape": "square",
            "text": "Weak H",
        }
    )
    markers.append(
        {
            "time": int(timestamps.iloc[-1].timestamp()),
            "position": "belowBar",
            "color": "#14b8a6",
            "shape": "square",
            "text": "Strong L",
        }
    )

    score = 0.0
    if trend_bias == 1:
        score += 0.45
    elif trend_bias == -1:
        score -= 0.45
    if markers:
        last_structure = next((m for m in reversed(markers) if m["text"] in {"BOS", "CHoCH"}), None)
        if last_structure:
            if last_structure["text"] == "CHoCH":
                score += 0.12 if trend_bias == 1 else -0.12
            else:
                score += 0.08 if trend_bias == 1 else -0.08
    score = max(-1.0, min(1.0, score))

    values.update(
        {
            "smc_trend_bias": trend_bias,
            "smc_pivot_highs": len(high_points),
            "smc_pivot_lows": len(low_points),
            "smc_structure_events": sum(1 for m in markers if m["text"] in {"BOS", "CHoCH"}),
            "smc_eq_signals": sum(1 for m in markers if m["text"] in {"EQH", "EQL"}),
            "smc_zone_count": sum(1 for o in overlays if o["name"] in {"Bullish OB", "Bearish OB"}),
            "smc_weak_high": round(strong_high, 6),
            "smc_strong_low": round(strong_low, 6),
        }
    )

    return overlays, markers, score, values


# ──────────── Evaluation Request Schema ────────────

class StrategyEvalRequest(BaseModel):
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"
    exchange: str = "bitget"
    limit: int = Field(default=200, ge=60, le=500)


# ──────────── Pine Script Evaluation ────────────

@router.post("/pinescripts/{script_id}/evaluate")
async def evaluate_pinescript(
    script_id: int,
    req: StrategyEvalRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Evaluate a Pine Script on live OHLCV data.
    - If the script has a linked strategy_id, delegates to that strategy's evaluation.
    - Otherwise, parses the Pine code to extract indicators and evaluates them.
    Returns same format as strategy evaluation: overlay_series, markers, score, etc.
    """
    result = await db.execute(select(PineScript).where(PineScript.id == script_id))
    p = result.scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Pine Script not found")

    # If linked to a strategy, delegate to the strategy evaluation
    if p.strategy_id:
        strat_result = await db.execute(
            select(BotStrategy).where(BotStrategy.id == p.strategy_id)
        )
        strat = strat_result.scalar_one_or_none()
        if strat:
            eval_result = await evaluate_strategy(p.strategy_id, req, db)
            eval_result["pine_script_id"] = script_id
            eval_result["pine_script_name"] = p.name
            return eval_result

    # Parse the Pine Script code to extract indicators
    indicators_cfg = _parse_pinescript_to_indicators(p.code or "")
    if not indicators_cfg:
        raise HTTPException(
            status_code=400,
            detail="Could not extract any indicators from Pine Script code. "
                   "Supported: ta.rsi, ta.macd, ta.bb, ta.ema, ta.stoch, ta.dmi, volume"
        )

    # Fetch OHLCV
    exchange_enum = SupportedExchange(req.exchange)
    connector = exchange_manager.get_exchange(exchange_enum)
    if not connector:
        raise HTTPException(status_code=503, detail=f"Exchange {req.exchange} not connected")

    try:
        ohlcv = await connector.get_ohlcv(
            symbol=req.symbol, timeframe=req.timeframe, limit=req.limit
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch OHLCV: {e}")

    if len(ohlcv) < 60:
        raise HTTPException(status_code=400, detail=f"Not enough candles ({len(ohlcv)}), need 60+")

    df = ohlcv_to_dataframe(ohlcv)

    # Re-use the same evaluation logic as strategy evaluate
    # We construct a temporary "strategy" from parsed indicators
    def safe(v):
        if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
            return None
        return round(float(v), 6)

    def series_to_list(ts: pd.Series, timestamps: pd.Series) -> list:
        out = []
        for i in range(len(ts)):
            v = ts.iloc[i]
            if v is not None and not (isinstance(v, float) and np.isnan(v)):
                t = int(timestamps.iloc[i].timestamp())
                out.append({"time": t, "value": round(float(v), 6)})
        return out

    overlay_series = []
    markers = []
    score_parts = []
    weight_total = 0.0
    indicator_values = {}
    timestamps = df["timestamp"]

    latest_price = safe(df["close"].iloc[-1])
    latest_volume = safe(df["volume"].iloc[-1])
    base_vol_ma = sma(df["volume"], 20)
    base_vol_ma_latest = safe(base_vol_ma.iloc[-1]) if len(base_vol_ma) else None
    base_volume_ratio = None
    base_buy_ratio = None
    if base_vol_ma_latest and base_vol_ma_latest > 0 and latest_volume is not None:
        base_volume_ratio = safe(float(latest_volume) / float(base_vol_ma_latest))

    try:
        bsv = buy_sell_volume(df)
        if len(bsv):
            base_buy_ratio = safe(bsv["buy_ratio"].iloc[-1])
    except Exception:
        base_buy_ratio = None

    indicator_values["last_price"] = latest_price
    indicator_values["latest_volume"] = latest_volume
    indicator_values["volume_ma20"] = base_vol_ma_latest
    if base_volume_ratio is not None:
        indicator_values["volume_ratio"] = base_volume_ratio
    if base_buy_ratio is not None:
        indicator_values["buy_ratio"] = base_buy_ratio

    for ind in indicators_cfg:
        if not ind.get("enabled", True):
            continue
        iname = ind["name"]
        params = ind.get("params", {})
        w = ind.get("weight", 1.0)

        try:
            if iname == "rsi":
                period = params.get("period", 14)
                ob = params.get("overbought", 70)
                os_ = params.get("oversold", 30)
                rsi_vals = rsi(df, period)
                latest_rsi = safe(rsi_vals.iloc[-1])
                indicator_values["rsi"] = latest_rsi
                overlay_series.append({
                    "name": f"RSI({period})", "type": "line", "pane": "rsi",
                    "color": "#a855f7", "data": series_to_list(rsi_vals, timestamps),
                    "levels": [
                        {"value": ob, "color": "#ef4444", "label": f"OB ({ob})"},
                        {"value": os_, "color": "#22c55e", "label": f"OS ({os_})"},
                    ],
                })
                if latest_rsi is not None:
                    if latest_rsi < os_: score_parts.append(1.0 * w)
                    elif latest_rsi > ob: score_parts.append(-1.0 * w)
                    else: score_parts.append(0.0)
                    weight_total += w

            elif iname == "macd":
                fast = params.get("fast", 12)
                slow = params.get("slow", 26)
                signal_p = params.get("signal", 9)
                macd_data = macd(df, fast, slow, signal_p)
                macd_line = macd_data["macd"]
                signal_line = macd_data["signal"]
                histogram = macd_data["histogram"]
                indicator_values["macd"] = safe(macd_line.iloc[-1])
                indicator_values["macd_signal"] = safe(signal_line.iloc[-1])
                indicator_values["macd_histogram"] = safe(histogram.iloc[-1])
                overlay_series.append({
                    "name": f"MACD({fast},{slow},{signal_p})", "type": "histogram",
                    "pane": "macd", "data": series_to_list(macd_line, timestamps),
                    "signal_data": series_to_list(signal_line, timestamps),
                    "histogram_data": [
                        {"time": int(timestamps.iloc[i].timestamp()),
                         "value": round(float(histogram.iloc[i]), 6),
                         "color": "#22c55e" if histogram.iloc[i] >= 0 else "#ef4444"}
                        for i in range(len(histogram)) if not np.isnan(histogram.iloc[i])
                    ],
                    "color": "#3b82f6", "signal_color": "#f59e0b",
                })
                h = histogram.iloc[-1]
                h_prev = histogram.iloc[-2] if len(histogram) > 1 else 0
                if not np.isnan(h):
                    if h > 0 and h_prev <= 0: score_parts.append(1.0 * w)
                    elif h < 0 and h_prev >= 0: score_parts.append(-1.0 * w)
                    elif h > 0 and h > h_prev: score_parts.append(0.5 * w)
                    elif h < 0 and h < h_prev: score_parts.append(-0.5 * w)
                    else: score_parts.append(0.0)
                    weight_total += w

            elif iname == "bollinger":
                period = params.get("period", 20)
                mult = params.get("mult", 2.0)
                bb = bollinger_bands(df, period, mult)
                indicator_values["bb_upper"] = safe(bb["upper"].iloc[-1])
                indicator_values["bb_middle"] = safe(bb["middle"].iloc[-1])
                indicator_values["bb_lower"] = safe(bb["lower"].iloc[-1])
                indicator_values["bb_pct_b"] = safe(bb["pct_b"].iloc[-1])
                overlay_series.extend([
                    {"name": f"BB Upper({period},{mult})", "type": "line", "pane": "main",
                     "color": "rgba(239,68,68,0.5)", "lineWidth": 1,
                     "data": series_to_list(bb["upper"], timestamps)},
                    {"name": f"BB Middle({period})", "type": "line", "pane": "main",
                     "color": "rgba(156,163,175,0.5)", "lineWidth": 1, "lineStyle": 2,
                     "data": series_to_list(bb["middle"], timestamps)},
                    {"name": f"BB Lower({period},{mult})", "type": "line", "pane": "main",
                     "color": "rgba(34,197,94,0.5)", "lineWidth": 1,
                     "data": series_to_list(bb["lower"], timestamps)},
                ])
                pct_b = bb["pct_b"].iloc[-1]
                if not np.isnan(pct_b):
                    if pct_b < 0.2: score_parts.append(1.0 * w)
                    elif pct_b > 0.8: score_parts.append(-1.0 * w)
                    else: score_parts.append(0.0)
                    weight_total += w

            elif iname == "ema_cross":
                fast_p = params.get("fast", 50)
                slow_p = params.get("slow", 200)
                ema_fast = ema(df["close"], fast_p)
                ema_slow = ema(df["close"], slow_p)
                indicator_values[f"ema{fast_p}"] = safe(ema_fast.iloc[-1])
                indicator_values[f"ema{slow_p}"] = safe(ema_slow.iloc[-1])
                overlay_series.extend([
                    {"name": f"EMA {fast_p}", "type": "line", "pane": "main",
                     "color": "#3b82f6", "lineWidth": 2,
                     "data": series_to_list(ema_fast, timestamps)},
                    {"name": f"EMA {slow_p}", "type": "line", "pane": "main",
                     "color": "#f59e0b", "lineWidth": 2,
                     "data": series_to_list(ema_slow, timestamps)},
                ])
                for i in range(1, len(ema_fast)):
                    if np.isnan(ema_fast.iloc[i]) or np.isnan(ema_slow.iloc[i]):
                        continue
                    if np.isnan(ema_fast.iloc[i-1]) or np.isnan(ema_slow.iloc[i-1]):
                        continue
                    if ema_fast.iloc[i] > ema_slow.iloc[i] and ema_fast.iloc[i-1] <= ema_slow.iloc[i-1]:
                        markers.append({"time": int(timestamps.iloc[i].timestamp()),
                            "position": "belowBar", "color": "#22c55e",
                            "shape": "arrowUp", "text": "EMA Cross ▲"})
                    elif ema_fast.iloc[i] < ema_slow.iloc[i] and ema_fast.iloc[i-1] >= ema_slow.iloc[i-1]:
                        markers.append({"time": int(timestamps.iloc[i].timestamp()),
                            "position": "aboveBar", "color": "#ef4444",
                            "shape": "arrowDown", "text": "EMA Cross ▼"})
                ef = ema_fast.iloc[-1]
                es = ema_slow.iloc[-1]
                if not np.isnan(ef) and not np.isnan(es):
                    score_parts.append((1.0 if ef > es else -1.0) * w)
                    weight_total += w

            elif iname == "stoch_rsi":
                period = params.get("period", 14)
                ob = params.get("overbought", 80)
                os_ = params.get("oversold", 20)
                stoch_vals = stochastic_rsi(df, period, period)
                indicator_values["stoch_rsi"] = safe(stoch_vals.iloc[-1])
                overlay_series.append({
                    "name": f"StochRSI({period})", "type": "line", "pane": "stochrsi",
                    "color": "#06b6d4", "data": series_to_list(stoch_vals, timestamps),
                    "levels": [
                        {"value": ob, "color": "#ef4444", "label": f"OB ({ob})"},
                        {"value": os_, "color": "#22c55e", "label": f"OS ({os_})"},
                    ],
                })
                sv = stoch_vals.iloc[-1]
                if not np.isnan(sv):
                    if sv < os_: score_parts.append(0.8 * w)
                    elif sv > ob: score_parts.append(-0.8 * w)
                    else: score_parts.append(0.0)
                    weight_total += w

            elif iname == "adx":
                period = params.get("period", 14)
                threshold = params.get("threshold", 25)
                adx_data = adx(df, period)
                adx_vals = adx_data["adx"]
                plus_di = adx_data["plus_di"]
                minus_di = adx_data["minus_di"]
                indicator_values["adx"] = safe(adx_vals.iloc[-1])
                indicator_values["plus_di"] = safe(plus_di.iloc[-1])
                indicator_values["minus_di"] = safe(minus_di.iloc[-1])
                overlay_series.append({
                    "name": f"ADX({period})", "type": "line", "pane": "adx",
                    "color": "#eab308", "data": series_to_list(adx_vals, timestamps),
                    "extra_lines": [
                        {"name": "+DI", "color": "#22c55e", "data": series_to_list(plus_di, timestamps)},
                        {"name": "-DI", "color": "#ef4444", "data": series_to_list(minus_di, timestamps)},
                    ],
                    "levels": [{"value": threshold, "color": "#6b7280", "label": f"Threshold ({threshold})"}],
                })
                a = adx_vals.iloc[-1]
                pd_val = plus_di.iloc[-1]
                md_val = minus_di.iloc[-1]
                if not np.isnan(a) and not np.isnan(pd_val) and not np.isnan(md_val):
                    direction = 1 if pd_val > md_val else -1
                    score_parts.append((direction * 1.0 if a > threshold else 0.0) * w)
                    weight_total += w

            elif iname == "volume":
                period = params.get("period", 20)
                mult = params.get("mult", 1.5)
                vol_ma = sma(df["volume"], period)
                indicator_values["vol_ma"] = safe(vol_ma.iloc[-1])
                vol_ratio = df["volume"].iloc[-1] / vol_ma.iloc[-1] if vol_ma.iloc[-1] > 0 else 1
                indicator_values["volume_ratio"] = safe(vol_ratio)
                bsv = buy_sell_volume(df)
                indicator_values["buy_ratio"] = safe(bsv["buy_ratio"].iloc[-1])
                if vol_ratio > mult:
                    br = bsv["buy_ratio"].iloc[-1]
                    if br > 0.6: score_parts.append(1.0 * w)
                    elif br < 0.4: score_parts.append(-1.0 * w)
                    else: score_parts.append(0.0)
                else:
                    score_parts.append(0.0)
                weight_total += w

            elif iname == "smc_lux":
                smc_overlays, smc_markers, smc_score, smc_values = _evaluate_smc_lux(df, timestamps, params)
                overlay_series.extend(smc_overlays)
                markers.extend(smc_markers)
                indicator_values.update(smc_values)
                score_parts.append(smc_score * w)
                weight_total += w

            elif iname == "support_resistance":
                pl = params.get("pivot_left", 5)
                pr = params.get("pivot_right", 5)
                max_lvl = params.get("max_levels", 8)
                sr = support_resistance_mtf(df, pivot_left=pl, pivot_right=pr, max_levels=max_lvl, include_lines=True)
                indicator_values["sr_levels"] = len(sr["levels"])
                indicator_values["sr_support_count"] = sum(1 for l in sr["levels"] if l["type"] == "support")
                indicator_values["sr_resistance_count"] = sum(1 for l in sr["levels"] if l["type"] == "resistance")
                # Add support lines to overlay
                for sl in sr["support_lines"]:
                    alpha = min(1.0, sl["strength"] / 4)
                    overlay_series.append({
                        "name": f"Support ({sl['price']:.2f}) x{sl['strength']}",
                        "type": "line", "pane": "main",
                        "color": f"rgba(34,197,94,{max(0.3, alpha)})",
                        "lineWidth": min(3, sl["strength"]),
                        "lineStyle": 2,
                        "data": sl["data"],
                    })
                for rl in sr["resistance_lines"]:
                    alpha = min(1.0, rl["strength"] / 4)
                    overlay_series.append({
                        "name": f"Resistance ({rl['price']:.2f}) x{rl['strength']}",
                        "type": "line", "pane": "main",
                        "color": f"rgba(239,68,68,{max(0.3, alpha)})",
                        "lineWidth": min(3, rl["strength"]),
                        "lineStyle": 2,
                        "data": rl["data"],
                    })
                # Add buy/sell markers from S/R signals
                for sig in sr["signals"]:
                    if sig["type"] == "buy":
                        markers.append({
                            "time": sig["time"],
                            "position": "belowBar",
                            "color": "#22c55e",
                            "shape": "arrowUp",
                            "text": f"S/R Buy ▲",
                        })
                    else:
                        markers.append({
                            "time": sig["time"],
                            "position": "aboveBar",
                            "color": "#ef4444",
                            "shape": "arrowDown",
                            "text": f"S/R Sell ▼",
                        })
                # Score: based on latest signal proximity
                if sr["signals"]:
                    latest_sig = sr["signals"][-1]
                    if latest_sig["type"] == "buy":
                        score_parts.append(0.8 * w)
                    else:
                        score_parts.append(-0.8 * w)
                else:
                    # Score based on price proximity to S/R
                    cp = float(df["close"].iloc[-1])
                    nearest_support = None
                    nearest_resist = None
                    for lvl in sr["levels"]:
                        if lvl["type"] == "support" and lvl["price"] < cp:
                            if nearest_support is None or lvl["price"] > nearest_support:
                                nearest_support = lvl["price"]
                        elif lvl["type"] == "resistance" and lvl["price"] > cp:
                            if nearest_resist is None or lvl["price"] < nearest_resist:
                                nearest_resist = lvl["price"]
                    if nearest_support and nearest_resist:
                        range_size = nearest_resist - nearest_support
                        if range_size > 0:
                            position = (cp - nearest_support) / range_size
                            score_parts.append(((0.5 - position) * 1.0) * w)
                        else:
                            score_parts.append(0.0)
                    elif nearest_support and not nearest_resist:
                        score_parts.append(0.3 * w)
                    elif nearest_resist and not nearest_support:
                        score_parts.append(-0.3 * w)
                    else:
                        score_parts.append(0.0)
                weight_total += w

            elif iname == "fib_retracement":
                dev = params.get("deviation_pct", 5.0)
                fib_depth = params.get("depth", 10)
                level_cfg = params.get("levels")
                extend = params.get("extend_lines", True)
                fib = auto_fib_retracement(df, deviation_pct=dev, depth=fib_depth, levels=level_cfg, extend_lines=extend)
                swing = fib.get("swing")
                indicator_values["fib_swing_direction"] = swing["direction"] if swing else None
                indicator_values["fib_golden_low"] = fib.get("golden_zone", {}).get("low") if fib.get("golden_zone") else None
                indicator_values["fib_golden_high"] = fib.get("golden_zone", {}).get("high") if fib.get("golden_zone") else None
                if fib.get("lines"):
                    primary = fib["lines"][0]
                    overlay_series.append({
                        "name": "Auto Fib Retracement",
                        "type": "line", "pane": "main",
                        "color": primary["color"],
                        "lineWidth": 1,
                        "lineStyle": 2,
                        "data": primary["data"],
                        "extra_lines": [
                            {
                                "name": f"Fib {ln['ratio'] * 100:.1f}%",
                                "color": ln["color"],
                                "data": ln["data"],
                            }
                            for ln in fib["lines"][1:]
                        ],
                    })
                cp = float(df["close"].iloc[-1])
                fib_conf = fib_confluence_score(cp, fib, swing["direction"] if swing else None)
                if fib_conf and swing:
                    direction_sign = 1 if swing["direction"] == "up" else -1
                    score_parts.append(direction_sign * fib_conf * w)
                else:
                    score_parts.append(0.0)
                weight_total += w

        except Exception as e:
            logger.warning(f"Pine Script indicator {iname} failed: {e}")
            continue

    combined_score = sum(score_parts) / weight_total if weight_total > 0 else 0.0
    combined_score = max(-1.0, min(1.0, combined_score))

    buy_th = 0.25
    sell_th = -0.25
    action = "hold"
    if combined_score >= buy_th:
        action = "buy"
    elif combined_score <= sell_th:
        action = "sell"

    confidence = min(1.0, abs(combined_score) / 0.5)

    return {
        "pine_script_id": script_id,
        "pine_script_name": p.name,
        "strategy_id": p.strategy_id,
        "strategy_name": p.name,
        "symbol": req.symbol,
        "timeframe": req.timeframe,
        "action": action,
        "score": round(combined_score, 4),
        "confidence": round(confidence, 4),
        "price": latest_price,
        "buy_threshold": buy_th,
        "sell_threshold": sell_th,
        "overlay_series": overlay_series,
        "markers": sorted(markers, key=lambda m: m["time"]),
        "indicator_values": indicator_values,
        "candles_analyzed": len(df),
        "parsed_indicators": [ind["name"] for ind in indicators_cfg],
    }


# ──────────── Strategy Evaluation (run indicators on OHLCV) ────────────

@router.post("/{strategy_id}/evaluate")
async def evaluate_strategy(
    strategy_id: int,
    req: StrategyEvalRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Run a strategy's configured indicators on live OHLCV data.
    Returns:
      - overlay_series: indicator line data for chart overlay (EMA, BB bands, etc.)
      - markers: buy/sell signal points on the chart
      - score: combined strategy score from latest candle
      - indicator_values: latest indicator readings
    """
    result = await db.execute(select(BotStrategy).where(BotStrategy.id == strategy_id))
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Strategy not found")

    indicators_cfg = json.loads(s.indicators) if s.indicators else []
    if not indicators_cfg:
        raise HTTPException(status_code=400, detail="Strategy has no indicators configured")

    # Fetch OHLCV from exchange
    exchange_enum = SupportedExchange(req.exchange)
    connector = exchange_manager.get_exchange(exchange_enum)
    if not connector:
        raise HTTPException(status_code=503, detail=f"Exchange {req.exchange} not connected")

    try:
        ohlcv = await connector.get_ohlcv(
            symbol=req.symbol, timeframe=req.timeframe, limit=req.limit
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch OHLCV: {e}")

    if len(ohlcv) < 60:
        raise HTTPException(status_code=400, detail=f"Not enough candles ({len(ohlcv)}), need 60+")

    df = ohlcv_to_dataframe(ohlcv)

    def safe(v):
        if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
            return None
        return round(float(v), 6)

    def series_to_list(ts: pd.Series, timestamps: pd.Series) -> list:
        """Convert a pandas Series to [{time, value}] for lightweight-charts."""
        out = []
        for i in range(len(ts)):
            v = ts.iloc[i]
            if v is not None and not (isinstance(v, float) and np.isnan(v)):
                t = int(timestamps.iloc[i].timestamp())
                out.append({"time": t, "value": round(float(v), 6)})
        return out

    overlay_series = []  # Line series for chart
    markers = []         # Buy/sell markers
    score_parts = []     # For combined scoring
    weight_total = 0.0
    indicator_values = {}
    timestamps = df["timestamp"]

    latest_price = safe(df["close"].iloc[-1])
    latest_volume = safe(df["volume"].iloc[-1])
    base_vol_ma = sma(df["volume"], 20)
    base_vol_ma_latest = safe(base_vol_ma.iloc[-1]) if len(base_vol_ma) else None
    base_volume_ratio = None
    base_buy_ratio = None
    if base_vol_ma_latest and base_vol_ma_latest > 0 and latest_volume is not None:
        base_volume_ratio = safe(float(latest_volume) / float(base_vol_ma_latest))

    try:
        bsv = buy_sell_volume(df)
        if len(bsv):
            base_buy_ratio = safe(bsv["buy_ratio"].iloc[-1])
    except Exception:
        base_buy_ratio = None

    indicator_values["last_price"] = latest_price
    indicator_values["latest_volume"] = latest_volume
    indicator_values["volume_ma20"] = base_vol_ma_latest
    if base_volume_ratio is not None:
        indicator_values["volume_ratio"] = base_volume_ratio
    if base_buy_ratio is not None:
        indicator_values["buy_ratio"] = base_buy_ratio

    for ind in indicators_cfg:
        if not ind.get("enabled", True):
            continue
        iname = ind["name"]
        params = ind.get("params", {})
        w = ind.get("weight", 1.0)

        try:
            if iname == "rsi":
                period = params.get("period", 14)
                ob = params.get("overbought", 70)
                os_ = params.get("oversold", 30)
                rsi_vals = rsi(df, period)
                latest_rsi = safe(rsi_vals.iloc[-1])
                indicator_values["rsi"] = latest_rsi

                # RSI is a separate pane indicator — include as data but not overlay
                overlay_series.append({
                    "name": f"RSI({period})",
                    "type": "line",
                    "pane": "rsi",
                    "color": "#a855f7",
                    "data": series_to_list(rsi_vals, timestamps),
                    "levels": [
                        {"value": ob, "color": "#ef4444", "label": f"OB ({ob})"},
                        {"value": os_, "color": "#22c55e", "label": f"OS ({os_})"},
                    ],
                })

                # Score
                if latest_rsi is not None:
                    if latest_rsi < os_:
                        score_parts.append(1.0 * w)
                    elif latest_rsi > ob:
                        score_parts.append(-1.0 * w)
                    else:
                        score_parts.append(0.0)
                    weight_total += w

            elif iname == "macd":
                fast = params.get("fast", 12)
                slow = params.get("slow", 26)
                signal_p = params.get("signal", 9)
                macd_data = macd(df, fast, slow, signal_p)
                macd_line = macd_data["macd"]
                signal_line = macd_data["signal"]
                histogram = macd_data["histogram"]

                indicator_values["macd"] = safe(macd_line.iloc[-1])
                indicator_values["macd_signal"] = safe(signal_line.iloc[-1])
                indicator_values["macd_histogram"] = safe(histogram.iloc[-1])

                overlay_series.append({
                    "name": f"MACD({fast},{slow},{signal_p})",
                    "type": "histogram",
                    "pane": "macd",
                    "data": series_to_list(macd_line, timestamps),
                    "signal_data": series_to_list(signal_line, timestamps),
                    "histogram_data": [
                        {
                            "time": int(timestamps.iloc[i].timestamp()),
                            "value": round(float(histogram.iloc[i]), 6),
                            "color": "#22c55e" if histogram.iloc[i] >= 0 else "#ef4444",
                        }
                        for i in range(len(histogram))
                        if not np.isnan(histogram.iloc[i])
                    ],
                    "color": "#3b82f6",
                    "signal_color": "#f59e0b",
                })

                # Score
                h = histogram.iloc[-1]
                h_prev = histogram.iloc[-2] if len(histogram) > 1 else 0
                if not np.isnan(h):
                    if h > 0 and h_prev <= 0:
                        score_parts.append(1.0 * w)
                    elif h < 0 and h_prev >= 0:
                        score_parts.append(-1.0 * w)
                    elif h > 0 and h > h_prev:
                        score_parts.append(0.5 * w)
                    elif h < 0 and h < h_prev:
                        score_parts.append(-0.5 * w)
                    else:
                        score_parts.append(0.0)
                    weight_total += w

            elif iname == "bollinger":
                period = params.get("period", 20)
                mult = params.get("mult", 2.0)
                bb = bollinger_bands(df, period, mult)
                indicator_values["bb_upper"] = safe(bb["upper"].iloc[-1])
                indicator_values["bb_middle"] = safe(bb["middle"].iloc[-1])
                indicator_values["bb_lower"] = safe(bb["lower"].iloc[-1])
                indicator_values["bb_pct_b"] = safe(bb["pct_b"].iloc[-1])

                overlay_series.extend([
                    {
                        "name": f"BB Upper({period},{mult})",
                        "type": "line",
                        "pane": "main",
                        "color": "rgba(239,68,68,0.5)",
                        "lineWidth": 1,
                        "data": series_to_list(bb["upper"], timestamps),
                    },
                    {
                        "name": f"BB Middle({period})",
                        "type": "line",
                        "pane": "main",
                        "color": "rgba(156,163,175,0.5)",
                        "lineWidth": 1,
                        "lineStyle": 2,  # dashed
                        "data": series_to_list(bb["middle"], timestamps),
                    },
                    {
                        "name": f"BB Lower({period},{mult})",
                        "type": "line",
                        "pane": "main",
                        "color": "rgba(34,197,94,0.5)",
                        "lineWidth": 1,
                        "data": series_to_list(bb["lower"], timestamps),
                    },
                ])

                # Score
                pct_b = bb["pct_b"].iloc[-1]
                if not np.isnan(pct_b):
                    if pct_b < 0.2:
                        score_parts.append(1.0 * w)
                    elif pct_b > 0.8:
                        score_parts.append(-1.0 * w)
                    else:
                        score_parts.append(0.0)
                    weight_total += w

            elif iname == "ema_cross":
                fast_p = params.get("fast", 50)
                slow_p = params.get("slow", 200)
                ema_fast = ema(df["close"], fast_p)
                ema_slow = ema(df["close"], slow_p)

                indicator_values[f"ema{fast_p}"] = safe(ema_fast.iloc[-1])
                indicator_values[f"ema{slow_p}"] = safe(ema_slow.iloc[-1])

                overlay_series.extend([
                    {
                        "name": f"EMA {fast_p}",
                        "type": "line",
                        "pane": "main",
                        "color": "#3b82f6",
                        "lineWidth": 2,
                        "data": series_to_list(ema_fast, timestamps),
                    },
                    {
                        "name": f"EMA {slow_p}",
                        "type": "line",
                        "pane": "main",
                        "color": "#f59e0b",
                        "lineWidth": 2,
                        "data": series_to_list(ema_slow, timestamps),
                    },
                ])

                # Generate cross markers
                for i in range(1, len(ema_fast)):
                    if np.isnan(ema_fast.iloc[i]) or np.isnan(ema_slow.iloc[i]):
                        continue
                    if np.isnan(ema_fast.iloc[i-1]) or np.isnan(ema_slow.iloc[i-1]):
                        continue
                    if ema_fast.iloc[i] > ema_slow.iloc[i] and ema_fast.iloc[i-1] <= ema_slow.iloc[i-1]:
                        markers.append({
                            "time": int(timestamps.iloc[i].timestamp()),
                            "position": "belowBar",
                            "color": "#22c55e",
                            "shape": "arrowUp",
                            "text": f"EMA Cross ▲",
                        })
                    elif ema_fast.iloc[i] < ema_slow.iloc[i] and ema_fast.iloc[i-1] >= ema_slow.iloc[i-1]:
                        markers.append({
                            "time": int(timestamps.iloc[i].timestamp()),
                            "position": "aboveBar",
                            "color": "#ef4444",
                            "shape": "arrowDown",
                            "text": f"EMA Cross ▼",
                        })

                # Score
                ef = ema_fast.iloc[-1]
                es = ema_slow.iloc[-1]
                if not np.isnan(ef) and not np.isnan(es):
                    if ef > es:
                        score_parts.append(1.0 * w)
                    else:
                        score_parts.append(-1.0 * w)
                    weight_total += w

            elif iname == "stoch_rsi":
                period = params.get("period", 14)
                ob = params.get("overbought", 80)
                os_ = params.get("oversold", 20)
                stoch_vals = stochastic_rsi(df, period, period)
                indicator_values["stoch_rsi"] = safe(stoch_vals.iloc[-1])

                overlay_series.append({
                    "name": f"StochRSI({period})",
                    "type": "line",
                    "pane": "stochrsi",
                    "color": "#06b6d4",
                    "data": series_to_list(stoch_vals, timestamps),
                    "levels": [
                        {"value": ob, "color": "#ef4444", "label": f"OB ({ob})"},
                        {"value": os_, "color": "#22c55e", "label": f"OS ({os_})"},
                    ],
                })

                sv = stoch_vals.iloc[-1]
                if not np.isnan(sv):
                    if sv < os_:
                        score_parts.append(0.8 * w)
                    elif sv > ob:
                        score_parts.append(-0.8 * w)
                    else:
                        score_parts.append(0.0)
                    weight_total += w

            elif iname == "adx":
                period = params.get("period", 14)
                threshold = params.get("threshold", 25)
                adx_data = adx(df, period)
                adx_vals = adx_data["adx"]
                plus_di = adx_data["plus_di"]
                minus_di = adx_data["minus_di"]
                indicator_values["adx"] = safe(adx_vals.iloc[-1])
                indicator_values["plus_di"] = safe(plus_di.iloc[-1])
                indicator_values["minus_di"] = safe(minus_di.iloc[-1])

                overlay_series.append({
                    "name": f"ADX({period})",
                    "type": "line",
                    "pane": "adx",
                    "color": "#eab308",
                    "data": series_to_list(adx_vals, timestamps),
                    "extra_lines": [
                        {"name": "+DI", "color": "#22c55e", "data": series_to_list(plus_di, timestamps)},
                        {"name": "-DI", "color": "#ef4444", "data": series_to_list(minus_di, timestamps)},
                    ],
                    "levels": [
                        {"value": threshold, "color": "#6b7280", "label": f"Threshold ({threshold})"},
                    ],
                })

                a = adx_vals.iloc[-1]
                pd_val = plus_di.iloc[-1]
                md_val = minus_di.iloc[-1]
                if not np.isnan(a) and not np.isnan(pd_val) and not np.isnan(md_val):
                    direction = 1 if pd_val > md_val else -1
                    if a > threshold:
                        score_parts.append(direction * 1.0 * w)
                    else:
                        score_parts.append(0.0)
                    weight_total += w

            elif iname == "volume":
                period = params.get("period", 20)
                mult = params.get("mult", 1.5)
                vol_ma = sma(df["volume"], period)
                indicator_values["vol_ma"] = safe(vol_ma.iloc[-1])

                vol_ratio = df["volume"].iloc[-1] / vol_ma.iloc[-1] if vol_ma.iloc[-1] > 0 else 1
                indicator_values["volume_ratio"] = safe(vol_ratio)
                bsv = buy_sell_volume(df)
                indicator_values["buy_ratio"] = safe(bsv["buy_ratio"].iloc[-1])

                # Volume surge score
                if vol_ratio > mult:
                    br = bsv["buy_ratio"].iloc[-1]
                    if br > 0.6:
                        score_parts.append(1.0 * w)
                    elif br < 0.4:
                        score_parts.append(-1.0 * w)
                    else:
                        score_parts.append(0.0)
                else:
                    score_parts.append(0.0)
                weight_total += w

            elif iname == "smc_lux":
                smc_overlays, smc_markers, smc_score, smc_values = _evaluate_smc_lux(df, timestamps, params)
                overlay_series.extend(smc_overlays)
                markers.extend(smc_markers)
                indicator_values.update(smc_values)
                score_parts.append(smc_score * w)
                weight_total += w

            elif iname == "support_resistance":
                pl = params.get("pivot_left", 5)
                pr = params.get("pivot_right", 5)
                max_lvl = params.get("max_levels", 8)
                sr = support_resistance_mtf(df, pivot_left=pl, pivot_right=pr, max_levels=max_lvl, include_lines=True)
                indicator_values["sr_levels"] = len(sr["levels"])
                indicator_values["sr_support_count"] = sum(1 for l in sr["levels"] if l["type"] == "support")
                indicator_values["sr_resistance_count"] = sum(1 for l in sr["levels"] if l["type"] == "resistance")
                # Add support lines
                for sl_line in sr["support_lines"]:
                    alpha = min(1.0, sl_line["strength"] / 4)
                    overlay_series.append({
                        "name": f"Support ({sl_line['price']:.2f}) x{sl_line['strength']}",
                        "type": "line", "pane": "main",
                        "color": f"rgba(34,197,94,{max(0.3, alpha)})",
                        "lineWidth": min(3, sl_line["strength"]),
                        "lineStyle": 2,
                        "data": sl_line["data"],
                    })
                for rl_line in sr["resistance_lines"]:
                    alpha = min(1.0, rl_line["strength"] / 4)
                    overlay_series.append({
                        "name": f"Resistance ({rl_line['price']:.2f}) x{rl_line['strength']}",
                        "type": "line", "pane": "main",
                        "color": f"rgba(239,68,68,{max(0.3, alpha)})",
                        "lineWidth": min(3, rl_line["strength"]),
                        "lineStyle": 2,
                        "data": rl_line["data"],
                    })
                for sig in sr["signals"]:
                    if sig["type"] == "buy":
                        markers.append({
                            "time": sig["time"], "position": "belowBar",
                            "color": "#22c55e", "shape": "arrowUp",
                            "text": "S/R Buy ▲",
                        })
                    else:
                        markers.append({
                            "time": sig["time"], "position": "aboveBar",
                            "color": "#ef4444", "shape": "arrowDown",
                            "text": "S/R Sell ▼",
                        })
                # Score
                if sr["signals"]:
                    latest_sig = sr["signals"][-1]
                    score_parts.append((0.8 if latest_sig["type"] == "buy" else -0.8) * w)
                else:
                    cp = float(df["close"].iloc[-1])
                    nearest_support = None
                    nearest_resist = None
                    for lvl in sr["levels"]:
                        if lvl["type"] == "support" and lvl["price"] < cp:
                            if nearest_support is None or lvl["price"] > nearest_support:
                                nearest_support = lvl["price"]
                        elif lvl["type"] == "resistance" and lvl["price"] > cp:
                            if nearest_resist is None or lvl["price"] < nearest_resist:
                                nearest_resist = lvl["price"]
                    if nearest_support and nearest_resist:
                        range_size = nearest_resist - nearest_support
                        if range_size > 0:
                            position = (cp - nearest_support) / range_size
                            score_parts.append(((0.5 - position) * 1.0) * w)
                        else:
                            score_parts.append(0.0)
                    elif nearest_support:
                        score_parts.append(0.3 * w)
                    elif nearest_resist:
                        score_parts.append(-0.3 * w)
                    else:
                        score_parts.append(0.0)
                weight_total += w

            elif iname == "fib_retracement":
                dev = params.get("deviation_pct", 5.0)
                fib_depth = params.get("depth", 10)
                level_cfg = params.get("levels")
                extend = params.get("extend_lines", True)
                fib = auto_fib_retracement(df, deviation_pct=dev, depth=fib_depth, levels=level_cfg, extend_lines=extend)
                swing = fib.get("swing")
                indicator_values["fib_swing_direction"] = swing["direction"] if swing else None
                indicator_values["fib_golden_low"] = fib.get("golden_zone", {}).get("low") if fib.get("golden_zone") else None
                indicator_values["fib_golden_high"] = fib.get("golden_zone", {}).get("high") if fib.get("golden_zone") else None
                if fib.get("lines"):
                    primary = fib["lines"][0]
                    overlay_series.append({
                        "name": "Auto Fib Retracement",
                        "type": "line", "pane": "main",
                        "color": primary["color"],
                        "lineWidth": 1,
                        "lineStyle": 2,
                        "data": primary["data"],
                        "extra_lines": [
                            {
                                "name": f"Fib {ln['ratio'] * 100:.1f}%",
                                "color": ln["color"],
                                "data": ln["data"],
                            }
                            for ln in fib["lines"][1:]
                        ],
                    })
                cp = float(df["close"].iloc[-1])
                fib_conf = fib_confluence_score(cp, fib, swing["direction"] if swing else None)
                if fib_conf and swing:
                    direction_sign = 1 if swing["direction"] == "up" else -1
                    score_parts.append(direction_sign * fib_conf * w)
                else:
                    score_parts.append(0.0)
                weight_total += w

        except Exception as e:
            logger.warning(f"Strategy indicator {iname} failed: {e}")
            continue

    # Combined score
    combined_score = sum(score_parts) / weight_total if weight_total > 0 else 0.0
    combined_score = max(-1.0, min(1.0, combined_score))

    # Generate buy/sell markers based on thresholds (scan historical candles)
    buy_th = s.buy_threshold or 0.25
    sell_th = s.sell_threshold or -0.25

    action = "hold"
    if combined_score >= buy_th:
        action = "buy"
    elif combined_score <= sell_th:
        action = "sell"

    confidence = min(1.0, abs(combined_score) / 0.5)

    return {
        "strategy_id": strategy_id,
        "strategy_name": s.name,
        "symbol": req.symbol,
        "timeframe": req.timeframe,
        "action": action,
        "score": round(combined_score, 4),
        "confidence": round(confidence, 4),
        "buy_threshold": buy_th,
        "sell_threshold": sell_th,
        "overlay_series": overlay_series,
        "markers": sorted(markers, key=lambda m: m["time"]),
        "indicator_values": indicator_values,
        "candles_analyzed": len(df),
    }


# ──────────── Strategy-based Signal for Pipeline Integration ────────────

@router.post("/{strategy_id}/signal")
async def generate_strategy_signal(
    strategy_id: int,
    symbol: str = Query("BTC/USDT"),
    timeframe: str = Query("1h"),
    exchange: str = Query("bitget"),
    db: AsyncSession = Depends(get_db),
):
    """
    Generate a trading signal using this strategy's indicator config.
    Can be combined with the main signal pipeline for more accurate signals.
    """
    eval_req = StrategyEvalRequest(
        symbol=symbol, timeframe=timeframe, exchange=exchange
    )
    eval_result = await evaluate_strategy(strategy_id, eval_req, db)

    return {
        "strategy_id": strategy_id,
        "strategy_name": eval_result["strategy_name"],
        "symbol": symbol,
        "timeframe": timeframe,
        "action": eval_result["action"],
        "score": eval_result["score"],
        "confidence": eval_result["confidence"],
        "indicator_values": eval_result["indicator_values"],
        "source": "strategy",
    }


# ──────────── Pine Script Generator ────────────

@router.post("/{strategy_id}/generate-pinescript")
async def generate_pinescript_from_strategy(
    strategy_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Auto-generate a TradingView Pine Script from a bot strategy config."""
    result = await db.execute(select(BotStrategy).where(BotStrategy.id == strategy_id))
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Strategy not found")

    indicators = json.loads(s.indicators) if s.indicators else []
    pairs = json.loads(s.pairs) if s.pairs else ["BTC/USDT"]

    code = _build_pinescript(
        name=s.name,
        indicators=indicators,
        buy_threshold=s.buy_threshold,
        sell_threshold=s.sell_threshold,
        stop_loss_pct=s.stop_loss_pct,
        take_profit_pct=s.take_profit_pct,
        timeframe=s.timeframe,
    )

    # Save as a new PineScript record
    p = PineScript(
        name=f"{s.name} – Generated",
        description=f"Auto-generated from strategy #{s.id}",
        strategy_id=s.id,
        script_type="strategy",
        code=code,
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)

    return _pine_to_dict(p)


def _build_pinescript(
    name: str,
    indicators: list,
    buy_threshold: float,
    sell_threshold: float,
    stop_loss_pct: float,
    take_profit_pct: float,
    timeframe: str,
) -> str:
    """Build a compilable Pine Script v5 strategy from indicator config."""
    lines: list[str] = []
    lines.append(f'//@version=5')
    lines.append(f'strategy("{name}", overlay=true, default_qty_type=strategy.percent_of_equity, default_qty_value=100)')
    lines.append('')

    # Build indicator variables + scoring
    score_parts: list[str] = []
    weight_total = 0.0
    indicator_blocks: list[str] = []

    for ind in indicators:
        if not ind.get("enabled", True):
            continue
        iname = ind["name"]
        params = ind.get("params", {})
        w = ind.get("weight", 1.0)
        weight_total += w

        if iname == "rsi":
            period = params.get("period", 14)
            ob = params.get("overbought", 70)
            os_ = params.get("oversold", 30)
            indicator_blocks.append(f'// RSI')
            indicator_blocks.append(f'rsiVal = ta.rsi(close, {period})')
            indicator_blocks.append(f'rsiScore = rsiVal < {os_} ? 1.0 : rsiVal > {ob} ? -1.0 : 0.0')
            score_parts.append(f'rsiScore * {w}')

        elif iname == "macd":
            fast = params.get("fast", 12)
            slow = params.get("slow", 26)
            signal = params.get("signal", 9)
            indicator_blocks.append(f'// MACD')
            indicator_blocks.append(f'[macdLine, signalLine, hist] = ta.macd(close, {fast}, {slow}, {signal})')
            indicator_blocks.append(f'macdScore = hist > 0 and hist > hist[1] ? 1.0 : hist < 0 and hist < hist[1] ? -1.0 : 0.0')
            score_parts.append(f'macdScore * {w}')

        elif iname == "bollinger":
            period = params.get("period", 20)
            mult = params.get("mult", 2.0)
            indicator_blocks.append(f'// Bollinger Bands')
            indicator_blocks.append(f'[bbMid, bbUp, bbLow] = ta.bb(close, {period}, {mult})')
            indicator_blocks.append(f'bbPctB = (close - bbLow) / (bbUp - bbLow)')
            indicator_blocks.append(f'bbScore = bbPctB < 0.2 ? 1.0 : bbPctB > 0.8 ? -1.0 : 0.0')
            score_parts.append(f'bbScore * {w}')

        elif iname == "ema_cross":
            fast = params.get("fast", 50)
            slow = params.get("slow", 200)
            indicator_blocks.append(f'// EMA Cross')
            indicator_blocks.append(f'emaFast = ta.ema(close, {fast})')
            indicator_blocks.append(f'emaSlow = ta.ema(close, {slow})')
            indicator_blocks.append(f'emaCrossScore = emaFast > emaSlow ? 1.0 : emaFast < emaSlow ? -1.0 : 0.0')
            score_parts.append(f'emaCrossScore * {w}')

        elif iname == "stoch_rsi":
            period = params.get("period", 14)
            ob = params.get("overbought", 80)
            os_ = params.get("oversold", 20)
            indicator_blocks.append(f'// Stochastic RSI')
            indicator_blocks.append(f'stochRsiK = ta.stoch(ta.rsi(close, {period}), ta.rsi(close, {period}), ta.rsi(close, {period}), {period})')
            indicator_blocks.append(f'stochRsiScore = stochRsiK < {os_} ? 1.0 : stochRsiK > {ob} ? -1.0 : 0.0')
            score_parts.append(f'stochRsiScore * {w}')

        elif iname == "adx":
            period = params.get("period", 14)
            threshold = params.get("threshold", 25)
            indicator_blocks.append(f'// ADX')
            indicator_blocks.append(f'[diPlus, diMinus, adxVal] = ta.dmi({period}, {period})')
            indicator_blocks.append(f'adxScore = adxVal > {threshold} ? (diPlus > diMinus ? 1.0 : -1.0) : 0.0')
            score_parts.append(f'adxScore * {w}')

        elif iname == "volume":
            period = params.get("period", 20)
            mult = params.get("mult", 1.5)
            indicator_blocks.append(f'// Volume Surge')
            indicator_blocks.append(f'volMA = ta.sma(volume, {period})')
            indicator_blocks.append(f'volSurge = volume > volMA * {mult}')
            indicator_blocks.append(f'volScore = volSurge ? (close > open ? 1.0 : -1.0) : 0.0')
            score_parts.append(f'volScore * {w}')

        elif iname == "support_resistance":
            pl = params.get("pivot_left", 5)
            pr = params.get("pivot_right", 5)
            indicator_blocks.append(f'// Support & Resistance (Pivot-based)')
            indicator_blocks.append(f'pivotHigh = ta.pivothigh(high, {pl}, {pr})')
            indicator_blocks.append(f'pivotLow  = ta.pivotlow(low, {pl}, {pr})')
            indicator_blocks.append(f'var float lastResistance = na')
            indicator_blocks.append(f'var float lastSupport = na')
            indicator_blocks.append(f'if not na(pivotHigh)')
            indicator_blocks.append(f'    lastResistance := pivotHigh')
            indicator_blocks.append(f'if not na(pivotLow)')
            indicator_blocks.append(f'    lastSupport := pivotLow')
            indicator_blocks.append(f'plot(lastResistance, "Resistance", color=color.new(color.red, 30), style=plot.style_linebr, linewidth=2)')
            indicator_blocks.append(f'plot(lastSupport, "Support", color=color.new(color.green, 30), style=plot.style_linebr, linewidth=2)')
            indicator_blocks.append(f'srScore = close > lastSupport and close < lastResistance ? (close - lastSupport) / (lastResistance - lastSupport) : close <= lastSupport ? 1.0 : -1.0')
            indicator_blocks.append(f'srScore := (0.5 - srScore) * 2.0')
            score_parts.append(f'srScore * {w}')

        elif iname == "fib_retracement":
            dev = params.get("deviation_pct", 5.0)
            fib_depth = params.get("depth", 10)
            indicator_blocks.append(f'// Auto Fib Retracement (ZigZag deviation={dev}%, depth={fib_depth})')
            indicator_blocks.append(f'fibPH = ta.pivothigh(high, {fib_depth}, {fib_depth})')
            indicator_blocks.append(f'fibPL = ta.pivotlow(low, {fib_depth}, {fib_depth})')
            indicator_blocks.append(f'var float fibSwingHigh = na')
            indicator_blocks.append(f'var float fibSwingLow = na')
            indicator_blocks.append(f'if not na(fibPH)')
            indicator_blocks.append(f'    fibSwingHigh := fibPH')
            indicator_blocks.append(f'if not na(fibPL)')
            indicator_blocks.append(f'    fibSwingLow := fibPL')
            indicator_blocks.append(f'fibHeight = fibSwingHigh - fibSwingLow')
            indicator_blocks.append(f'fibGoldenLow = fibSwingHigh - fibHeight * 0.618')
            indicator_blocks.append(f'fibGoldenHigh = fibSwingHigh - fibHeight * 0.5')
            indicator_blocks.append(f'plot(fibGoldenLow, "Fib 61.8%", color=color.new(color.teal, 30))')
            indicator_blocks.append(f'plot(fibGoldenHigh, "Fib 50%", color=color.new(color.green, 30))')
            indicator_blocks.append(f'fibInZone = close >= fibGoldenLow and close <= fibGoldenHigh')
            indicator_blocks.append(f'fibScore = fibInZone ? (close > open ? 1.0 : -1.0) : 0.0')
            score_parts.append(f'fibScore * {w}')

    lines.extend(indicator_blocks)
    lines.append('')

    # Combined score
    if weight_total > 0 and score_parts:
        score_expr = ' + '.join(score_parts)
        lines.append(f'combinedScore = ({score_expr}) / {weight_total}')
    else:
        lines.append('combinedScore = 0.0')

    lines.append('')

    # Entry / exit conditions
    lines.append(f'// ─── Entry / Exit ───')
    lines.append(f'buySignal  = combinedScore >= {buy_threshold}')
    lines.append(f'sellSignal = combinedScore <= {sell_threshold}')
    lines.append('')

    # Strategy orders
    lines.append(f'if buySignal')
    lines.append(f'    strategy.entry("Long", strategy.long)')
    if stop_loss_pct > 0 or take_profit_pct > 0:
        sl = f'strategy.position_avg_price * (1 - {stop_loss_pct / 100})' if stop_loss_pct > 0 else 'na'
        tp = f'strategy.position_avg_price * (1 + {take_profit_pct / 100})' if take_profit_pct > 0 else 'na'
        lines.append(f'    strategy.exit("Long TP/SL", "Long", stop={sl}, limit={tp})')
    lines.append('')
    lines.append(f'if sellSignal')
    lines.append(f'    strategy.close("Long")')
    lines.append('')

    # Webhook alert placeholders
    lines.append('// ─── Webhook Alerts ───')
    lines.append('alertcondition(buySignal, title="Buy Signal", message=\'{"action":"buy","symbol":"{{ticker}}","price":{{close}},"timeframe":"' + timeframe + '"}\')')
    lines.append('alertcondition(sellSignal, title="Sell Signal", message=\'{"action":"sell","symbol":"{{ticker}}","price":{{close}},"timeframe":"' + timeframe + '"}\')')

    # Plot overlay helpers
    lines.append('')
    lines.append('// ─── Visual Overlay ───')
    lines.append('plotshape(buySignal, title="Buy", location=location.belowbar, color=color.green, style=shape.triangleup, size=size.small)')
    lines.append('plotshape(sellSignal, title="Sell", location=location.abovebar, color=color.red, style=shape.triangledown, size=size.small)')
    lines.append(f'bgcolor(combinedScore >= {buy_threshold} ? color.new(color.green, 90) : combinedScore <= {sell_threshold} ? color.new(color.red, 90) : na)')

    return '\n'.join(lines)


# ──────────── Multi-Evaluate: evaluate multiple scripts/strategies at once ────────────

class MultiEvalRequest(BaseModel):
    selections: List[str] = Field(..., min_length=1)  # ["strategy:1", "pine:3", ...]
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"
    exchange: str = "bitget"
    limit: int = Field(default=200, ge=60, le=500)


@router.post("/evaluate-multi")
async def evaluate_multi(
    req: MultiEvalRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Evaluate multiple strategies and/or pine scripts at once.
    Merges overlay_series, markers, and computes a combined score.
    Selections format: ["strategy:ID", "pine:ID", ...]
    """
    merged_overlays = []
    merged_markers = []
    all_scores = []
    all_indicator_values = {}
    eval_results = []

    eval_req = StrategyEvalRequest(
        symbol=req.symbol,
        timeframe=req.timeframe,
        exchange=req.exchange,
        limit=req.limit,
    )

    for sel in req.selections:
        try:
            if sel.startswith("strategy:"):
                sid = int(sel.split(":")[1])
                result = await evaluate_strategy(sid, eval_req, db)
            elif sel.startswith("pine:"):
                pid = int(sel.split(":")[1])
                result = await evaluate_pinescript(pid, eval_req, db)
            else:
                continue

            name = result.get("strategy_name") or result.get("pine_script_name", sel)
            merged_overlays.extend(result.get("overlay_series", []))
            merged_markers.extend(result.get("markers", []))
            score = result.get("score", 0)
            all_scores.append(score)

            # Merge indicator values with prefix
            for k, v in result.get("indicator_values", {}).items():
                all_indicator_values[f"{name}:{k}"] = v

            eval_results.append({
                "selection": sel,
                "name": name,
                "score": score,
                "action": result.get("action", "hold"),
                "confidence": result.get("confidence", 0),
            })

        except HTTPException:
            continue
        except Exception as e:
            logger.warning(f"Multi-eval failed for {sel}: {e}")
            continue

    # Combined score = weighted average (equal weights)
    combined_score = sum(all_scores) / len(all_scores) if all_scores else 0.0
    combined_score = max(-1.0, min(1.0, combined_score))

    action = "hold"
    if combined_score >= 0.25:
        action = "buy"
    elif combined_score <= -0.25:
        action = "sell"

    confidence = min(1.0, abs(combined_score) / 0.5)

    # Deduplicate markers by time+position (keep unique signals)
    seen_markers = set()
    unique_markers = []
    for m in sorted(merged_markers, key=lambda x: x["time"]):
        key = (m["time"], m["position"], m.get("text", ""))
        if key not in seen_markers:
            seen_markers.add(key)
            unique_markers.append(m)

    return {
        "selections": req.selections,
        "symbol": req.symbol,
        "timeframe": req.timeframe,
        "action": action,
        "score": round(combined_score, 4),
        "confidence": round(confidence, 4),
        "overlay_series": merged_overlays,
        "markers": unique_markers,
        "indicator_values": all_indicator_values,
        "eval_results": eval_results,
        "candles_analyzed": req.limit,
    }
