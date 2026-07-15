"""
OpenHumanPlugin — Memory Sync Service

Pushes TradeBot data (signals, forecasts, positions) into the
agentmemory store so OpenHuman's brain contains current market context.

All calls are best-effort; errors are logged but never raise.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple
from loguru import logger

from plugins.OpenHumanPlugin.backend.services.openhuman_client import add_memory


async def push_signal(signal: Any) -> Tuple[bool, Optional[str]]:
    """Push a single signal dict/model to agentmemory."""
    try:
        sym = getattr(signal, "symbol", None) or (signal.get("symbol") if isinstance(signal, dict) else None)
        action = getattr(signal, "action", None) or (signal.get("action") if isinstance(signal, dict) else None)
        conf = getattr(signal, "confidence", None) or (signal.get("confidence") if isinstance(signal, dict) else None)
        content = f"TradeBot signal: {sym} {action}"
        if conf is not None:
            content += f" confidence={conf:.0%}"
        tags = ["tradebot", "signal"]
        if sym:
            tags.append(sym.replace("/", "").lower())
        result = await add_memory(content, tags=tags)
        ok = "error" not in result
        return ok, result.get("id") if isinstance(result, dict) else None
    except Exception as exc:
        logger.debug(f"[MemorySync] push_signal failed: {exc}")
        return False, None


async def push_forecast(symbol: str, forecast_result: Any) -> bool:
    """Push a Kronos forecast summary to agentmemory."""
    try:
        if hasattr(forecast_result, "signal") and forecast_result.signal:
            sig = forecast_result.signal
            content = (
                f"Kronos forecast for {symbol}: {sig.direction} "
                f"{sig.pct_change:+.1f}% confidence={sig.confidence:.0%}. {sig.summary}"
            )
        else:
            content = f"Kronos forecast for {symbol}: {str(forecast_result)[:200]}"
        tags = ["tradebot", "kronos", "forecast", symbol.replace("/", "").lower()]
        result = await add_memory(content, tags=tags)
        return "error" not in result
    except Exception as exc:
        logger.debug(f"[MemorySync] push_forecast failed: {exc}")
        return False


async def sync_recent_signals(limit: int = 10) -> Dict[str, int]:
    """Pull recent signals from core and push to agentmemory."""
    synced = failed = 0
    try:
        from app.signals.service import SignalService
        from app.core.database import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            svc = SignalService(db)
            signals = await svc.list_signals(limit=limit)
            for sig in signals:
                ok, _ = await push_signal(sig)
                if ok:
                    synced += 1
                else:
                    failed += 1
    except Exception as exc:
        logger.warning(f"[MemorySync] sync_recent_signals failed: {exc}")
    return {"synced": synced, "failed": failed}


async def push_jarvis_brain_state(
    symbol: str,
    content: str,
) -> bool:
    """Push JARVIS multi-brain consolidated output to OpenHuman agentmemory.

    Called by _brain_openhuman_sync() after every JARVIS analysis cycle so
    the OpenHuman plugin stays aware of the current market cognitive state.
    Best-effort — never raises.
    """
    try:
        tags = ["tradebot", "jarvis-brain", "market-state"]
        if symbol and symbol != "market":
            tags.append(symbol.replace("/", "").lower())
        result = await add_memory(
            f"JARVIS brain state [{symbol}]: {content}",
            tags=tags,
        )
        return "error" not in result
    except Exception as exc:
        logger.debug(f"[MemorySync] push_jarvis_brain_state failed: {exc}")
        return False


async def sync_recent_forecasts(symbols: Optional[List[str]] = None) -> Dict[str, int]:
    """Re-run Kronos forecast for active symbols and push summaries to agentmemory."""
    synced = failed = 0
    active_syms = symbols or ["BTC/USDT", "ETH/USDT"]
    for sym in active_syms:
        try:
            from plugins.KronosForecastPlugin.backend.services.forecast_service import run_forecast_cached
            from app.core.database import AsyncSessionLocal
            async with AsyncSessionLocal() as db:
                fc = await asyncio.wait_for(
                    run_forecast_cached(db=db, exchange="bitget", symbol=sym),
                    timeout=30,
                )
            ok = await push_forecast(sym, fc)
            if ok:
                synced += 1
            else:
                failed += 1
        except Exception as exc:
            logger.debug(f"[MemorySync] forecast sync failed for {sym}: {exc}")
            failed += 1
    return {"synced": synced, "failed": failed}
