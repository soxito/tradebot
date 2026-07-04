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
    return {
        "symbol": resp.symbol,
        "timeframe": resp.timeframe,
        "engine": resp.engine,
        "direction": sig.direction if sig else "flat",
        "pct_change": sig.pct_change if sig else 0.0,
        "confidence": sig.confidence if sig else 0.0,
        "target_price": sig.target_price if sig else resp.anchor_price,
        "spoken": sig.summary if sig else f"No forecast available for {resp.symbol}.",
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
