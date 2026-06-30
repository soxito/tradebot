"""
API Routes
"""
from fastapi import APIRouter
from app.api.exchanges import router as exchanges_router
from app.api.signals import router as signals_router
from app.api.sentiment import router as sentiment_router
from app.api.trading import router as trading_router
from app.api.simulation import router as simulation_router
from app.api.strategies import router as strategies_router
from app.api.live_trade import router as live_trade_router
from app.api.monitoring import router as monitoring_router
from app.api.agents import router as agents_router
from app.api.rug_pulls import router as rug_pulls_router
from app.api.pump_monitor import router as pump_monitor_router
from app.api.strategy_lab import router as strategy_lab_router
from app.api.voice import router as voice_router
from app.api.jarvis import router as jarvis_router
from app.api.provider_relay import router as provider_relay_router

api_router = APIRouter()

# Include sub-routers
api_router.include_router(jarvis_router)
api_router.include_router(exchanges_router)
api_router.include_router(signals_router)
api_router.include_router(sentiment_router)
api_router.include_router(trading_router)
api_router.include_router(simulation_router)
api_router.include_router(strategies_router)
api_router.include_router(live_trade_router)
api_router.include_router(monitoring_router)
api_router.include_router(agents_router)
api_router.include_router(rug_pulls_router)
api_router.include_router(pump_monitor_router)
api_router.include_router(strategy_lab_router)
api_router.include_router(voice_router)
api_router.include_router(provider_relay_router)


@api_router.get("/status")
async def api_status():
    """API status check"""
    return {
        "api": "online",
        "version": "1.0",
        "modules": {
            "exchanges": "ready",
            "sentiment": "ready",
            "signals": "ready",
            "trading": "ready",
            "strategy_lab": "ready",
        }
    }
