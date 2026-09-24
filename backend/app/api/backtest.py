"""Backtesting OBLIVION signals and the trading room's agents."""
from __future__ import annotations

import json
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.database import BacktestRun

router = APIRouter(prefix="/backtest", tags=["backtest"])

TIMEFRAMES = ("5m", "15m", "30m", "1h", "2h", "4h", "1d")


class BacktestRequest(BaseModel):
    symbols: Optional[List[str]] = None          # default: the room's pinned pairs
    timeframe: str = "2h"
    start_balance: float = Field(10_000.0, gt=0, le=1e9)
    risk_pct: float = Field(1.0, gt=0, le=10)
    #: None = 20000 bars for 15m and below (≈7 months of crypto), 5000 otherwise.
    bars: Optional[int] = Field(None, ge=300, le=30000)
    agents_rule: Literal["opposite", "strict", "none"] = "opposite"
    use_room_profile: bool = True
    send_telegram: bool = True
    #: Minimum targets to score the weeks/days against; None = the room's own.
    weekly_target_usd: Optional[float] = Field(None, ge=0)
    daily_target_usd: Optional[float] = Field(None, ge=0)


def _row(r: BacktestRun) -> dict:
    return {
        "id": r.id, "status": r.status, "symbols": r.symbols.split(","), "timeframe": r.timeframe,
        "start_balance": r.start_balance, "risk_pct": r.risk_pct,
        "params": json.loads(r.params_json or "{}"), "summary": json.loads(r.summary_json or "{}"),
        "has_pdf": bool(r.pdf_path), "telegram_sent": r.telegram_sent, "error": r.error,
        "created_at": str(r.created_at), "finished_at": str(r.finished_at) if r.finished_at else None,
    }


#: A whole universe in one run, so its report totals every pair together.
MAX_SYMBOLS = 80


@router.get("/options")
async def options():
    """Form defaults: the room's pinned pairs and the tuned profiles."""
    from app.agents import room
    from app.signals.oblivion_profiles import ROOM_PROFILES

    return {"timeframes": TIMEFRAMES, "focus_symbols": room.get_focus_symbols(),
            "profiles": {tf: p["validated"] for tf, p in ROOM_PROFILES.items()}}


@router.post("/run")
async def run_backtest(req: BacktestRequest):
    from app.agents import room
    from app.backtest import service

    tf = req.timeframe.strip().lower()
    if tf not in TIMEFRAMES:
        raise HTTPException(status_code=400, detail=f"timeframe must be one of {list(TIMEFRAMES)}")
    symbols = [s.strip().upper().replace("/", "") for s in (req.symbols or room.get_focus_symbols()) if s.strip()]
    if not symbols:
        raise HTTPException(status_code=400, detail="no pairs — pin pairs in the trading room or pass symbols")
    bars = req.bars or (20000 if tf in ("5m", "15m", "30m") else 5000)
    weekly, daily = req.weekly_target_usd, req.daily_target_usd
    if weekly is None or daily is None:
        from app.agents import target_pacer
        from app.core.database import AsyncSessionLocal

        try:
            async with AsyncSessionLocal() as db:
                state = await target_pacer.state(db, fresh=True)
            weekly = state["target_usd"] if weekly is None else weekly
            daily = state["daily"]["target_usd"] if daily is None else daily
        except Exception:  # noqa: BLE001
            weekly, daily = weekly or 0.0, daily or 0.0
    params = {"timeframe": tf, "start_balance": req.start_balance, "risk_pct": req.risk_pct, "bars": bars,
              "agents_rule": req.agents_rule, "use_room_profile": req.use_room_profile,
              "weekly_target_usd": weekly, "daily_target_usd": daily}
    symbols = symbols[:MAX_SYMBOLS]
    run_id = await service.start(symbols, params, send_telegram=req.send_telegram)
    return {"id": run_id, "status": "running", "symbols": symbols}


class PortfolioRequest(BaseModel):
    days: int = Field(60, ge=7, le=365)
    send_telegram: bool = True


@router.post("/portfolio")
async def run_portfolio_backtest(req: PortfolioRequest):
    """The room's traded book replayed on every configured MT5 / crypto account."""
    from app.backtest import service

    run_id = await service.start_portfolio(req.days, send_telegram=req.send_telegram)
    return {"id": run_id, "status": "running", "kind": "portfolio"}


@router.get("/runs")
async def list_runs(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(BacktestRun).order_by(desc(BacktestRun.id)).limit(30))).scalars().all()
    return [_row(r) for r in rows]


@router.get("/runs/{run_id}")
async def get_run(run_id: int, db: AsyncSession = Depends(get_db)):
    from app.backtest.service import load_result

    r = await db.get(BacktestRun, run_id)
    if r is None:
        raise HTTPException(status_code=404, detail="backtest not found")
    out = _row(r)
    doc = load_result(r.result_path)
    if doc and doc.get("kind") == "portfolio":
        out["portfolio"] = {**doc, "accounts": [
            {**a, **{k: {**a[k], "trades": a[k]["trades"][-400:]} for k in ("paced", "wf_paced") if k in a}}
            for a in doc.get("accounts") or []]}
    elif doc:
        out["results"] = {
            sym: {k: v for k, v in res.items() if k != "candles"} for sym, res in doc["results"].items()
        }
        out["totals"] = doc.get("totals")
        out["traded"] = doc.get("traded")
    return out


@router.get("/runs/{run_id}/candles/{symbol}")
async def run_candles(run_id: int, symbol: str, db: AsyncSession = Depends(get_db)):
    from app.backtest.service import load_result

    r = await db.get(BacktestRun, run_id)
    doc = load_result(r.result_path if r else None)
    res = (doc or {}).get("results", {}).get(symbol.upper())
    if not res:
        raise HTTPException(status_code=404, detail="no candles for that pair in this run")
    return {"symbol": symbol.upper(), "candles": res.get("candles") or []}


@router.get("/runs/{run_id}/pdf")
async def run_pdf(run_id: int, db: AsyncSession = Depends(get_db)):
    from pathlib import Path

    r = await db.get(BacktestRun, run_id)
    if r is None or not r.pdf_path or not Path(r.pdf_path).exists():
        raise HTTPException(status_code=404, detail="no report for this run")
    return Response(Path(r.pdf_path).read_bytes(), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{Path(r.pdf_path).name}"'})


@router.post("/runs/{run_id}/cancel")
async def cancel(run_id: int, db: AsyncSession = Depends(get_db)):
    """Stop a queued or running backtest."""
    from app.backtest import service

    r = await db.get(BacktestRun, run_id)
    if r is None:
        raise HTTPException(status_code=404, detail="no such run")
    if r.status != "running":
        raise HTTPException(status_code=409, detail=f"run is already {r.status}")
    task = service._tasks.get(run_id)
    if task is not None:
        task.cancel()
    r.status, r.error = "error", "cancelled"
    await db.commit()
    return {"cancelled": True}


@router.post("/runs/{run_id}/telegram")
async def resend(run_id: int, db: AsyncSession = Depends(get_db)):
    from pathlib import Path

    from app.backtest.service import load_result, send_report

    r = await db.get(BacktestRun, run_id)
    if r is None or not r.pdf_path or not Path(r.pdf_path).exists():
        raise HTTPException(status_code=404, detail="no report for this run")
    doc = load_result(r.result_path) or {}
    if doc.get("kind") == "portfolio":
        from app.backtest.service import portfolio_caption
        from plugins.TelegramSignalNewsPlugin.backend.services.notifications import notify_document

        sent = await notify_document(Path(r.pdf_path).read_bytes(), Path(r.pdf_path).name, portfolio_caption(doc))
    else:
        sent = await send_report(run_id, Path(r.pdf_path).read_bytes(), doc)
    r.telegram_sent = r.telegram_sent or sent
    await db.commit()
    if not sent:
        raise HTTPException(status_code=502, detail="Telegram did not accept the report — check the bot token and chat")
    return {"sent": True}


@router.post("/runs/{run_id}/brain")
async def store_in_brain(run_id: int, db: AsyncSession = Depends(get_db)):
    """(Re)store a finished run's results and every trade in the AI brain."""
    from app.agents import brain_log
    from app.backtest.service import load_result

    r = await db.get(BacktestRun, run_id)
    doc = load_result(r.result_path if r else None)
    if not doc:
        raise HTTPException(status_code=404, detail="no stored result for this run")
    return await brain_log.log_backtest(doc)
