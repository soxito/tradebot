"""
VibeTradingPlugin — Signal Exporter

Translates TradeBot's core signals and positions into context strings
that are injected into Vibe-Trading research/swarm sessions so the AI
agents understand the live state of the portfolio.

Uses only public interfaces — never imports core internals beyond the
exchange_manager and signal service.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from loguru import logger


async def build_context_from_signals(symbol: Optional[str] = None) -> str:
    """
    Fetch recent signals + open sim positions for the given symbol (or all)
    and return a compact context string suitable for prepending to a
    Vibe-Trading research prompt.
    """
    lines: List[str] = []

    # Recent signals
    try:
        from app.signals.service import SignalService
        from app.core.database import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            service = SignalService(db)
            params: Dict = {"limit": 10}
            if symbol:
                params["symbol"] = symbol
            signals = await service.list_signals(**params)
            if signals:
                lines.append("## Recent TradeBot Signals")
                for s in signals[:5]:
                    sym = getattr(s, "symbol", "?")
                    action = getattr(s, "action", "?")
                    confidence = getattr(s, "confidence", None)
                    conf_str = f" ({confidence:.0%})" if confidence else ""
                    lines.append(f"- {sym} {action}{conf_str}")
    except Exception as exc:
        logger.debug(f"[SignalExporter] signals fetch skipped: {exc}")

    # Open positions (simulation account)
    try:
        from app.trading.simulation import SimulationEngine
        from app.core.database import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            engine = SimulationEngine(db)
            positions = await engine.get_open_positions()
            relevant = [p for p in positions if (not symbol or getattr(p, "symbol", "").startswith(symbol.split("/")[0]))]
            if relevant:
                lines.append("## Open Sim Positions")
                for p in relevant[:5]:
                    sym = getattr(p, "symbol", "?")
                    side = getattr(p, "side", "?")
                    pnl = getattr(p, "unrealized_pnl", 0)
                    lines.append(f"- {sym} {side} PnL: {pnl:+.2f}")
    except Exception as exc:
        logger.debug(f"[SignalExporter] positions fetch skipped: {exc}")

    if not lines:
        return ""

    return "\n".join(lines) + "\n\n"


async def build_swarm_variables(symbol: str, preset: str) -> Dict[str, Any]:
    """
    Build the `variables` dict for a Vibe-Trading swarm run,
    enriched with TradeBot market context.
    """
    variables: Dict[str, Any] = {
        "asset": symbol,
        "topic": f"{symbol} trading analysis",
    }

    # Add Kronos forecast summary if available
    try:
        from plugins.KronosForecastPlugin.backend.services.forecast_service import run_forecast_cached
        from app.core.database import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            fc = await run_forecast_cached(db=db, exchange="bitget", symbol=symbol)
            if fc and fc.signal:
                variables["kronos_signal"] = (
                    f"{fc.signal.direction} {fc.signal.pct_change:+.1f}% "
                    f"confidence={fc.signal.confidence:.0%}"
                )
    except Exception:
        pass

    return variables
