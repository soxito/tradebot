"""
Kronos Forecast Plugin — API Router

All routes are mounted at /api/v1/plugins/kronos by the plugin loader.
"""
from typing import Optional

from fastapi import APIRouter, Query
from loguru import logger

from plugins.KronosForecastPlugin.backend.schemas import (
    ForecastResponse, ForecastStatus, BatchForecastRequest, JarvisAnalysisResponse,
    SniperSignalsResponse,
    FinetuneStartRequest, FinetuneExportRequest, FinetuneExportResponse,
    FinetuneJobResponse, FinetuneSetupStatus,
)
from plugins.KronosForecastPlugin.backend.services import forecast_service, jarvis_analysis
from plugins.KronosForecastPlugin.backend.config import KRONOS_MODELS

router = APIRouter(prefix="/plugins/kronos", tags=["Kronos Forecast"])


def _normalize_symbol(symbol: str) -> str:
    """Accept URL-safe symbols (BTCUSDT) and convert to CCXT form (BTC/USDT)."""
    if "/" in symbol:
        return symbol
    upper = symbol.upper()
    for quote in ("USDT", "USDC", "USD", "BTC", "ETH"):
        if upper.endswith(quote) and len(upper) > len(quote):
            return f"{upper[:-len(quote)]}/{quote}"
    return upper


@router.get("/status", response_model=ForecastStatus)
async def get_status() -> ForecastStatus:
    """Report whether the real Kronos model is loaded, the device, and fallback state."""
    return forecast_service.get_status()


@router.get("/models")
async def list_models():
    """Return the full published Kronos model catalogue, annotated with whether
    each variant's weights are already downloaded locally (so the UI can show
    which models are installed vs. will download on first use)."""
    from plugins.KronosForecastPlugin.backend.services.kronos_engine import kronos_engine
    cached = kronos_engine._cached_repos()
    models = [
        {**m, "installed": (m["id"] in cached and m["tokenizer"] in cached)}
        for m in KRONOS_MODELS
    ]
    return {
        "models": models,
        "current": forecast_service.get_status().model_name,
        "installed_count": sum(1 for m in models if m["installed"]),
        "total": len(models),
    }


@router.post("/models/install-all")
async def install_all_models():
    """Download every published Kronos model + tokenizer into the local cache so
    any variant can be selected instantly (no heuristic fallback). Runs in the
    background — poll GET /plugins/kronos/models to watch install progress."""
    from plugins.KronosForecastPlugin.backend.services.kronos_engine import kronos_engine
    import asyncio

    already = kronos_engine._cached_repos()
    pending = [
        m for m in KRONOS_MODELS
        if not (m["id"] in already and m["tokenizer"] in already)
    ]

    async def _do_install():
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: kronos_engine.download_all(KRONOS_MODELS))
        logger.success("[Kronos] install-all complete")

    if pending:
        asyncio.create_task(_do_install())

    return {
        "ok": True,
        "status": "installing" if pending else "already_installed",
        "pending": [m["id"] for m in pending],
        "total": len(KRONOS_MODELS),
        "message": (
            f"Downloading {len(pending)} model(s) in the background — "
            "poll GET /plugins/kronos/models to watch progress."
            if pending else "All Kronos models are already installed."
        ),
    }



@router.post("/model/switch")
async def switch_model(model_id: str = Query(..., description="HF model ID e.g. NeoQuasar/Kronos-base")):
    """Hot-swap the loaded Kronos model variant.
    Loads in the background so large models (Kronos-base ~400MB) don't time out.
    Poll /plugins/kronos/status to confirm when the switch completes."""
    from plugins.KronosForecastPlugin.backend.services.kronos_engine import kronos_engine
    import asyncio
    # Find matching entry in catalogue
    entry = next((m for m in KRONOS_MODELS if m["id"] == model_id), None)
    if entry is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"Unknown model: {model_id}. Known: {[m['id'] for m in KRONOS_MODELS]}")

    async def _do_switch():
        loop = asyncio.get_event_loop()
        ok = await loop.run_in_executor(
            None,
            lambda: kronos_engine.load_model(entry["id"], entry["tokenizer"], entry["max_context"]),
        )
        if ok:
            # Clear forecast cache on success
            try:
                from plugins.KronosForecastPlugin.backend.services import forecast_service
                if hasattr(forecast_service, "_cache"):
                    forecast_service._cache.clear()
            except Exception:
                pass

    asyncio.create_task(_do_switch())
    return {"ok": True, "model": entry["id"], "status": "loading", "message": "Switching in background — poll /plugins/kronos/status to confirm"}


@router.get("/forecast/{exchange}/{symbol}", response_model=ForecastResponse)
async def get_forecast(
    exchange: str,
    symbol: str,
    timeframe: str = Query("1h", description="1m,5m,15m,1h,4h,1d..."),
    lookback: Optional[int] = Query(None, ge=16, le=512, description="History candles fed to the model"),
    pred_len: Optional[int] = Query(None, ge=1, le=120, description="Future candles to predict"),
    samples: Optional[int] = Query(None, ge=1, le=50, description="Stochastic paths for confidence bands"),
    temperature: Optional[float] = Query(None, ge=0.1, le=2.0),
    top_p: Optional[float] = Query(None, ge=0.1, le=1.0),
) -> ForecastResponse:
    """Full forecast: predicted candles, confidence bands, signal, and chart overlays."""
    return await forecast_service.run_forecast_cached(
        exchange, _normalize_symbol(symbol), timeframe,
        lookback=lookback, pred_len=pred_len, samples=samples,
        temperature=temperature, top_p=top_p,
    )


@router.get("/overlay/{exchange}/{symbol}")
async def get_overlay(
    exchange: str,
    symbol: str,
    timeframe: str = Query("1h"),
    pred_len: Optional[int] = Query(None, ge=1, le=120),
    samples: Optional[int] = Query(None, ge=1, le=50),
):
    """Lightweight payload for dropping a forecast onto an existing chart:
    just `overlays`, `markers`, and the `signal`."""
    resp = await forecast_service.run_forecast_cached(
        exchange, _normalize_symbol(symbol), timeframe,
        pred_len=pred_len, samples=samples,
    )
    return {
        "exchange": resp.exchange,
        "symbol": resp.symbol,
        "timeframe": resp.timeframe,
        "engine": resp.engine,
        "overlays": [o.model_dump() for o in resp.overlays],
        "markers": [m.model_dump() for m in resp.markers],
        "signal": resp.signal.model_dump() if resp.signal else None,
    }


@router.get("/jarvis/{symbol}")
async def jarvis_forecast(
    symbol: str,
    exchange: str = Query("bitget"),
    timeframe: str = Query("1h"),
    pred_len: Optional[int] = Query(None, ge=1, le=120),
):
    """Compact, speech-friendly forecast for JARVIS voice/chat."""
    resp = await forecast_service.run_forecast_cached(
        exchange, _normalize_symbol(symbol), timeframe, pred_len=pred_len,
    )
    sig = resp.signal
    vol = resp.volume
    return {
        "symbol": resp.symbol,
        "timeframe": resp.timeframe,
        "engine": resp.engine,
        "direction": sig.direction if sig else "flat",
        "pct_change": sig.pct_change if sig else 0.0,
        "confidence": sig.confidence if sig else 0.0,
        "target_price": sig.target_price if sig else resp.anchor_price,
        "spoken": sig.summary if sig else f"No forecast available for {resp.symbol}.",
        # Volume evidence + why the direction was chosen — a forecast is never
        # shown without it (see services/volume_context.py).
        "decision": resp.decision,
        "volume": vol.model_dump() if vol else None,
        "rationale": sig.rationale if sig else [],
    }


@router.post("/analyze/{exchange}/{symbol}", response_model=JarvisAnalysisResponse)
async def jarvis_analyze(
    exchange: str,
    symbol: str,
    timeframe: str = Query("1h"),
    pred_len: Optional[int] = Query(None, ge=1, le=120),
    samples: Optional[int] = Query(None, ge=1, le=50),
    temperature: Optional[float] = Query(None, ge=0.1, le=2.0),
    learn: bool = Query(True, description="Store this analysis to the JARVIS brain"),
) -> JarvisAnalysisResponse:
    """JARVIS-narrated analysis of the Kronos forecast enriched with crypto
    market cap. Learns from every run by storing the analysis to the shared AI
    knowledge brain (deduped + reinforced by symbol/timeframe)."""
    resp = await forecast_service.run_forecast_cached(
        exchange, _normalize_symbol(symbol), timeframe,
        pred_len=pred_len, samples=samples, temperature=temperature,
    )
    return await jarvis_analysis.analyze_forecast(resp, learn=learn)


@router.get("/sniper/{exchange}/{symbol}", response_model=SniperSignalsResponse)
async def get_sniper_signals(
    exchange: str,
    symbol: str,
    timeframe: str = Query("1h"),
    pred_len: Optional[int] = Query(None, ge=1, le=120),
    samples: Optional[int] = Query(None, ge=1, le=50),
) -> SniperSignalsResponse:
    """Executable sniper entries derived from the Kronos forecast direction and
    confidence bands: entry, stop-loss, take-profits, R:R and the reasons why."""
    return await forecast_service.generate_sniper_signals(
        exchange, _normalize_symbol(symbol), timeframe,
        pred_len=pred_len, samples=samples,
    )


@router.get("/tradability/{exchange}/{symbol}")
async def get_tradability(exchange: str, symbol: str):
    """Live account context for placing a sniper order on a pair: openable
    margin, max leverage, contract min size/precision, existing position, and
    the max-open-remaining (notional + margin) for the symbol."""
    return await forecast_service.get_pair_tradability(exchange, _normalize_symbol(symbol))


@router.get("/mt5-accounts")
async def list_mt5_accounts():
    """Return all linked live MT5 accounts with equity/margin for sniper sizing display.

    Shows every LIVE account regardless of ``api_reachable`` so users always
    see their accounts — a disconnected account is shown with an ``online``
    flag of False so the UI can grey it out rather than hide it entirely.
    """
    try:
        from app.core.database import AsyncSessionLocal
        from sqlalchemy import select
        from plugins.MT5TradingPlugin.backend.models import MT5Account, MT5AccountType
    except ImportError:
        return {"available": False, "accounts": []}

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(MT5Account)
            .where(MT5Account.account_type == MT5AccountType.LIVE)
            .order_by(MT5Account.id)
        )
        rows = result.scalars().all()
        return {
            "available": True,
            "accounts": [
                {
                    "id": a.id,
                    "name": a.name,
                    "server": a.server,
                    "equity": float(a.equity or 0),
                    "balance": float(a.balance or 0),
                    "free_margin": float(a.free_margin or 0),
                    "leverage": int(a.leverage or 100),
                    "currency": a.currency or "USD",
                    "online": bool(a.api_reachable),
                }
                for a in rows
            ],
        }


@router.post("/execute-mt5")
async def execute_mt5_sniper(
    symbol: str = Query(..., description="Instrument, e.g. XAUUSD or EURUSD"),
    side: str = Query(..., description="buy | sell"),
    entry: float = Query(...),
    stop_loss: float = Query(...),
    take_profit: Optional[float] = Query(None),
    timeframe: str = Query("1h"),
    dry_run: bool = Query(False, description="True → compute lots without placing orders"),
):
    """Execute a Kronos sniper entry on all linked live MT5 accounts.

    Uses the same lot-sizing logic as the Telegram sniper service:
    - Reads room risk % from RoomSettings (default 1 %)
    - Calls ``affordable_mt5_lot`` per account (risk-budget × free-margin cap)
    - Floors at broker minimum (0.01 lots)
    - Sizes independently per account using each account's equity
    - dry_run=true → returns the computed lots without sending to the broker
    """
    from app.core.database import AsyncSessionLocal

    sym = _normalize_symbol(symbol)
    side_lower = side.lower()
    if side_lower not in {"buy", "sell"}:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="side must be 'buy' or 'sell'")
    direction = "long" if side_lower == "buy" else "short"

    try:
        from plugins.TelegramSignalNewsPlugin.backend.services.sniper_service import (
            _get_room_settings,
            _get_live_mt5_accounts,
            _execute_mt5 as _do_execute_mt5,
            affordable_mt5_lot,
            _equity_in_quote,
        )
        from plugins.TelegramSignalNewsPlugin.backend.services.forex_price_service import _parse_pair
        from app.trading.order_tags import SOURCE_ROOM, build_comment
    except ImportError as exc:
        return {"ok": False, "error": f"Required plugin unavailable: {exc}", "placed": [], "errors": []}

    async with AsyncSessionLocal() as db:
        room = await _get_room_settings(db)
        risk_pct = float(getattr(room, "risk_pct", 1.0) or 1.0)
        max_risk_pct = 5.0  # hard ceiling: never risk more than 5 % on a single lot

        # _get_live_mt5_accounts filters by api_reachable; also do a direct fallback
        # so we always attempt reachable accounts first, then any remaining live ones.
        accounts = await _get_live_mt5_accounts(db, None)
        if not accounts:
            # Fallback: query all LIVE accounts regardless of api_reachable
            try:
                from sqlalchemy import select as _sa_select
                from plugins.MT5TradingPlugin.backend.models import MT5Account as _MT5A, MT5AccountType as _AT
                result = await db.execute(
                    _sa_select(_MT5A).where(_MT5A.account_type == _AT.LIVE).order_by(_MT5A.id)
                )
                accounts = list(result.scalars().all())
            except Exception:
                pass
        if not accounts:
            return {
                "ok": False,
                "error": "No live MT5 accounts found — add one under /mt5-live",
                "placed": [], "errors": [],
            }

        pp = _parse_pair(sym)
        quote_ccy = pp[1] if pp else "USD"

        placed: list = []
        errors: list = []

        for acct in accounts:
            equity_native = float(getattr(acct, "equity", 0) or getattr(acct, "balance", 0) or 0)
            acct_ccy = (getattr(acct, "currency", None) or "USD").upper()
            equity_q = await _equity_in_quote(equity_native, acct_ccy, quote_ccy)
            eff_equity = equity_q if equity_q is not None else 0.0
            free_native = getattr(acct, "free_margin", None)
            free_q = (
                await _equity_in_quote(float(free_native), acct_ccy, quote_ccy)
                if free_native else None
            )

            lot, size_note = affordable_mt5_lot(
                equity=eff_equity,
                free_margin=free_q,
                leverage=getattr(acct, "leverage", None),
                risk_pct=risk_pct,
                entry=entry,
                stop_loss=stop_loss,
                symbol=sym,
                floor_lot=0.01,
                max_risk_pct=max_risk_pct,
                small_account_mode=True,
            )

            acct_label = getattr(acct, "name", None) or f"acct {acct.id}"
            ccy_note = (
                f" ({acct_ccy} {equity_native:g}→{quote_ccy} {eff_equity:g})"
                if acct_ccy != quote_ccy else ""
            )

            if lot is None:
                errors.append({"account": acct_label, "account_id": acct.id, "reason": size_note})
                continue

            if dry_run:
                placed.append({
                    "account": acct_label,
                    "account_id": acct.id,
                    "lot": lot,
                    "size_note": size_note + ccy_note,
                    "equity": equity_native,
                    "currency": acct_ccy,
                    "risk_pct": risk_pct,
                    "dry_run": True,
                })
                continue

            comment = build_comment(SOURCE_ROOM, f"KR-{sym[:6]}")
            res = await _do_execute_mt5(
                db,
                symbol=sym,
                direction=direction,
                stop_loss=stop_loss,
                take_profit=take_profit,
                lot_size=lot,
                account_id=acct.id,
                comment=comment,
            )
            if res.get("success"):
                placed.append({
                    "account": acct_label,
                    "account_id": acct.id,
                    "lot": lot,
                    "size_note": size_note + ccy_note,
                    "order_id": res.get("order_id"),
                    "equity": equity_native,
                    "currency": acct_ccy,
                    "risk_pct": risk_pct,
                    "venue": "mt5",
                })
            elif res.get("skipped"):
                # Symbol not listed on this broker — a clean skip, not a failure.
                errors.append({
                    "account": acct_label,
                    "account_id": acct.id,
                    "reason": res.get("error", f"{sym} not listed on this broker — skipped"),
                    "skipped": True,
                })
            else:
                errors.append({
                    "account": acct_label,
                    "account_id": acct.id,
                    "reason": str(res.get("error", "unknown"))[:200],
                })

        if not dry_run:
            await db.commit()

    return {
        "ok": len(placed) > 0,
        "symbol": sym,
        "side": side_lower,
        "entry": entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "risk_pct": risk_pct,
        "placed": placed,
        "errors": errors,
        "dry_run": dry_run,
    }


@router.post("/execute")
async def execute_sniper_order(
    symbol: str = Query(..., description="Instrument, e.g. ETHUSD or XAUUSD"),
    side: str = Query(..., description="buy | sell"),
    entry: float = Query(...),
    stop_loss: float = Query(...),
    take_profit: float = Query(...),
    timeframe: str = Query("1h"),
):
    """Route a Kronos sniper entry to every configured venue (Bitget, Exness/MT5,
    or both) using the same room execution engine as the trading desk.

    Works for any instrument — crypto CFDs (ETHUSD, BTCUSD), FX (EURUSD),
    metals (XAUUSD) — by delegating venue selection to ``venues_for``.
    Respects room ``dry_run``: True → demo only, False → demo + live.
    """
    import json
    from app.core.database import AsyncSessionLocal
    from app.agents.execution import _route, get_settings
    from app.models.database import Signal, SignalAction, SignalSource, SignalStatus

    sym = _normalize_symbol(symbol)
    action = side.lower()
    if action not in {"buy", "sell"}:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="side must be 'buy' or 'sell'")

    async with AsyncSessionLocal() as db:
        s = await get_settings(db)
        if not s.execution_enabled:
            return {"ok": False, "placed": [], "reason": "execution is switched off in room settings"}

        # Signal row so _crypto_fill (Bitget path) can read the levels.
        sig = Signal(
            source=SignalSource.SYSTEM,
            symbol=sym,
            action=SignalAction.BUY if action == "buy" else SignalAction.SELL,
            price=entry, timeframe=timeframe,
            strength=0.8, confidence=0.8,
            status=SignalStatus.PENDING,
            raw_data=json.dumps({"source": "kronos_sniper"}),
            indicators=json.dumps({"stop_loss": stop_loss, "take_profit": take_profit}),
        )
        db.add(sig)
        await db.flush()

        orders = await _route(
            db, s,
            symbol=sym, action=action,
            price=entry, stop_loss=stop_loss, take_profit=take_profit,
            level_source="kronos_sniper", signal_id=sig.id,
        )
        await db.commit()

    placed = [o for o in orders if o.get("status") == "placed"]
    errors = [o for o in orders if o.get("status") == "error"]
    return {
        "symbol": sym,
        "side": action,
        "placed": placed,
        "errors": errors,
        "orders": orders,
        "ok": len(placed) > 0,
    }


@router.post("/batch")
async def batch_forecast(req: BatchForecastRequest):
    """Forecast several symbols at once (sequential to stay within model memory)."""
    results = []
    for sym in req.symbols[:25]:
        try:
            resp = await forecast_service.run_forecast_cached(
                req.exchange, _normalize_symbol(sym), req.timeframe,
                lookback=req.lookback, pred_len=req.pred_len, samples=req.samples,
            )
            results.append(resp.signal.model_dump() | {"symbol": resp.symbol, "engine": resp.engine}
                           if resp.signal else {"symbol": resp.symbol, "engine": resp.engine})
        except Exception as exc:
            logger.warning(f"[Kronos] batch forecast failed for {sym}: {exc}")
            results.append({"symbol": sym, "error": str(exc)})
    return {"count": len(results), "results": results}


# ── Fine-tuning ───────────────────────────────────────────────────────────────

@router.get("/finetune/setup-status", response_model=FinetuneSetupStatus)
async def finetune_setup_status() -> FinetuneSetupStatus:
    """Whether the finetune_csv pipeline is vendored, and how to install it."""
    from plugins.KronosForecastPlugin.backend.services import finetune_service
    return FinetuneSetupStatus(**finetune_service.setup_status())


@router.post("/finetune/export", response_model=FinetuneExportResponse)
async def finetune_export(req: FinetuneExportRequest) -> FinetuneExportResponse:
    """Export live OHLCV to a Kronos-format CSV (preview) without training."""
    from pathlib import Path
    from plugins.KronosForecastPlugin.backend.services import (
        finetune_data_exporter as exporter, finetune_service,
    )
    sym = _normalize_symbol(req.symbol)
    out = Path(finetune_service._WORK_DIR) / "preview" / f"{sym.replace('/', '')}_{req.timeframe}.csv"
    try:
        summary = await exporter.export_training_data(req.exchange, sym, req.timeframe, req.limit, out)
        return FinetuneExportResponse(ok=True, **summary)
    except Exception as exc:
        return FinetuneExportResponse(
            ok=False, symbol=sym, exchange=req.exchange, timeframe=req.timeframe,
            error=str(exc),
        )


@router.post("/finetune/start", response_model=FinetuneJobResponse)
async def finetune_start(req: FinetuneStartRequest) -> FinetuneJobResponse:
    """Export data + start tokenizer→predictor fine-tuning as a background job."""
    from fastapi import HTTPException
    from plugins.KronosForecastPlugin.backend.services import finetune_service
    req.symbol = _normalize_symbol(req.symbol)
    try:
        job = await finetune_service.finetune_manager.start(req)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return FinetuneJobResponse(**job)


@router.get("/finetune/jobs")
async def finetune_jobs():
    """List recent fine-tune jobs (newest first)."""
    from plugins.KronosForecastPlugin.backend.services import finetune_service
    return {"jobs": await finetune_service.finetune_manager.list()}


@router.get("/finetune/jobs/{job_id}", response_model=FinetuneJobResponse)
async def finetune_job(job_id: int) -> FinetuneJobResponse:
    """One fine-tune job with a tail of its live training log."""
    from fastapi import HTTPException
    from plugins.KronosForecastPlugin.backend.services import finetune_service
    job = await finetune_service.finetune_manager.get(job_id, log_lines=50)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Fine-tune job {job_id} not found")
    return FinetuneJobResponse(**job)


@router.delete("/finetune/jobs/{job_id}")
async def finetune_cancel(job_id: int):
    """Cancel a running fine-tune job (SIGTERM the trainer, mark cancelled)."""
    from plugins.KronosForecastPlugin.backend.services import finetune_service
    ok = await finetune_service.finetune_manager.cancel(job_id)
    return {"ok": ok, "job_id": job_id}


@router.post("/finetune/load/{job_id}")
async def finetune_load(job_id: int):
    """Hot-swap the engine to a completed job's finetuned model."""
    from fastapi import HTTPException
    from plugins.KronosForecastPlugin.backend.services import finetune_service
    try:
        return await finetune_service.finetune_manager.load_finetuned(job_id)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
