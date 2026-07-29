"""
Main FastAPI Application Entry Point
"""
# ── Windows asyncio policy fix ────────────────────────────────────────────────
# asyncpg requires SelectorEventLoop on Windows (not the default ProactorEventLoop).
# Must be set BEFORE any asyncio or uvicorn code runs; has no effect on other OSes.
import sys as _sys
if _sys.platform == "win32":
    import asyncio as _asyncio
    _asyncio.set_event_loop_policy(_asyncio.WindowsSelectorEventLoopPolicy())

import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from app.core.config import settings
from app.core.database import init_db, engine
from app.core.logging import configure_logging
from app.api.routes import api_router
from app.plugins.loader import PluginLoader
from app.workers.runtime import start_background_workers, stop_background_workers
from app.monitoring.metrics import APP_INFO, record_request


plugin_loader = PluginLoader(
    plugins_dir=settings.PLUGINS_DIR,
    strict_mode=settings.PLUGIN_STRICT_MODE,
)
mounted_plugin_slugs: list[str] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events"""
    configure_logging()
    APP_INFO.labels(version="0.1.0", environment=settings.ENVIRONMENT).set(1)
    logger.info("🚀 TradeBot starting up...")
    logger.info(f"Environment: {settings.ENVIRONMENT}")
    logger.info(f"Debug mode: {settings.DEBUG}")
    logger.info(f"CORS origins: {settings.cors_origins_list}")
    if settings.ENABLE_AUTO_TRADING:
        logger.warning("⚠️  ENABLE_AUTO_TRADING is ON — live orders will be placed if auto-trade is active")
    
    # Initialize database
    try:
        await init_db()
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")

    # Initialize plugin database tables
    if settings.PLUGIN_AUTO_MOUNT:
        try:
            initialized_plugins = await plugin_loader.init_plugin_tables(engine)
            if initialized_plugins:
                logger.info(
                    f"✅ Plugin tables initialized: {', '.join(initialized_plugins)}"
                )
        except Exception as e:
            logger.warning(f"Plugin tables init skipped: {e}")

    started_workers = start_background_workers(allow_in_api=False)
    logger.info(f"Background worker startup result: {started_workers}")

    # Always keep the crypto-pair catalog fresh, even in API-only mode, so JARVIS
    # has real coin names + live market cap/volume and can resolve token names to
    # tradeable Bitget pairs. The loop is idempotent (won't double-start) and does
    # a one-time full sync when the catalog is empty.
    if settings.AUTO_START_PAIR_CATALOG_SYNC_LOOP and not started_workers.get("pair_catalog_sync_loop"):
        try:
            from app.core.scheduler import start_pair_catalog_sync_loop
            start_pair_catalog_sync_loop()
        except Exception as e:
            logger.warning(f"Pair catalog sync loop failed to start: {e}")

    # SMC background research loop (economic calendar + news + sentiment) backing
    # /research. Started here too so the feed is live in API-only mode — the
    # worker autostart above is skipped when START_WORKERS_IN_API=False.
    if settings.AUTO_START_RESEARCH_LOOP and not started_workers.get("research_loop"):
        try:
            from app.core.scheduler import start_research_loop
            start_research_loop(settings.RESEARCH_LOOP_INTERVAL_SECONDS)
        except Exception as e:
            logger.warning(f"Research loop failed to start: {e}")

    # Realtime price-tick fan-out for SSE subscribers (idempotent; self-throttles
    # to zero work when no client is connected).
    if settings.AUTO_START_PRICE_TICK_LOOP:
        try:
            from app.core.scheduler import start_price_tick_loop
            start_price_tick_loop()
        except Exception as e:
            logger.warning(f"Price tick loop failed to start: {e}")

    # ngrok hybrid auto-start — only when explicitly enabled via config/DB
    try:
        from app.core.database import AsyncSessionLocal
        from app.models.database import NgrokConfig
        from sqlalchemy import select as _sql_select

        async def _maybe_start_ngrok():
            try:
                async with AsyncSessionLocal() as _db:
                    row = (await _db.execute(_sql_select(NgrokConfig).where(NgrokConfig.id == 1))).scalar_one_or_none()
                    enable = (row.enable_on_start if row and row.enable_on_start is not None else settings.NGROK_AUTO_START)
                    if not enable:
                        return
                    authtoken = (row.authtoken_override if row and row.authtoken_override else settings.NGROK_AUTHTOKEN)
                    if not authtoken:
                        logger.warning("ngrok auto-start enabled but NGROK_AUTHTOKEN is not set — skipping")
                        return
                    backend_addr = (row.backend_addr_override if row and row.backend_addr_override else settings.NGROK_BACKEND_ADDR)
                    frontend_addr = (row.frontend_addr_override if row and row.frontend_addr_override else settings.NGROK_FRONTEND_ADDR)
                    from app.services.ngrok_service import ngrok_service
                    await ngrok_service.start(authtoken=authtoken, backend_addr=backend_addr, frontend_addr=frontend_addr)
            except Exception as _ngrok_err:
                logger.warning(f"ngrok auto-start failed (non-fatal): {_ngrok_err}")

        await _maybe_start_ngrok()
    except Exception as e:
        logger.warning(f"ngrok startup check error (non-fatal): {e}")

    yield

    # Shutdown logic here
    # Stop ngrok tunnels gracefully
    try:
        from app.services.ngrok_service import ngrok_service as _ngrok
        if _ngrok.status().get("state") == "running":
            await _ngrok.stop()
            logger.info("ngrok tunnels stopped")
    except Exception:
        pass
    stop_background_workers()
    try:
        from app.core.scheduler import stop_price_tick_loop
        stop_price_tick_loop()
    except Exception:
        pass
    logger.info("🛑 TradeBot shutting down...")


app = FastAPI(
    title="TradeBot API",
    description="Crypto trading bot with sentiment analysis and TradingView integration",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    duration = time.perf_counter() - start
    route = request.scope.get("route")
    route_path = getattr(route, "path", request.url.path)
    record_request(request.method, route_path, response.status_code, duration)
    response.headers["X-Process-Time"] = f"{duration:.4f}"
    return response

# Include API routes
app.include_router(api_router, prefix=settings.API_V1_PREFIX)


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "version": "0.1.0",
        "environment": settings.ENVIRONMENT,
        "plugins": mounted_plugin_slugs,
    }


@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "message": "TradeBot API",
        "version": "0.1.0",
        "docs": "/docs" if settings.DEBUG else "disabled",
    }


@app.get("/cors-test")
async def cors_test():
    """CORS test endpoint - returns simple response with timestamp"""
    from app.core.timezone import now_sast
    return {
        "status": "CORS is working!",
        "timestamp": now_sast().isoformat(),
        "allowed_origins": settings.cors_origins_list,
    }


# ── Plugin Loader ──────────────────────────────────────────

if settings.PLUGIN_AUTO_MOUNT:
    mounted_plugin_slugs = plugin_loader.mount_routers(
        app,
        api_prefix=settings.API_V1_PREFIX,
    )
else:
    logger.info("Plugin auto-mount disabled (PLUGIN_AUTO_MOUNT=False)")
