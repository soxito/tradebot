"""
Main FastAPI Application Entry Point
"""
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
    
    yield
    
    # Shutdown logic here
    stop_background_workers()
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
