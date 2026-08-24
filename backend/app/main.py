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

    # Event-loop lag probe — the decisive freeze metric. Started first so it
    # captures the whole startup window.
    try:
        from app.core.loop_monitor import loop_monitor
        loop_monitor.start()
    except Exception as e:
        logger.warning(f"Loop monitor failed to start: {e}")

    # Initialize database
    try:
        await init_db()
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")

    # Restore the trading room's custom agent names/seats so a restart doesn't
    # silently revert the board to the built-in personas.
    try:
        from app.core.database import AsyncSessionLocal
        from app.api.agents import refresh_persona_overrides
        async with AsyncSessionLocal() as db:
            await refresh_persona_overrides(db)
    except Exception as e:
        logger.debug(f"Room persona overrides not loaded: {e}")

    # Bring un-customised agent instructions up to the current defaults. Prompts
    # move into the DB the first time an install seeds them, so an improvement
    # to the shipped text would otherwise never reach a running deployment —
    # which is how a board kept answering "hold" long after the instruction that
    # told it to had been rewritten. Anything a user edited is left alone.
    try:
        from app.agents.specialists import upgrade_stock_prompts
        from app.core.database import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            await upgrade_stock_prompts(db)
    except Exception as e:
        logger.debug(f"Agent prompt upgrade skipped: {e}")

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

    # Per-signal research queue backing /research → Signal Research. Started here
    # too, same reasoning as the loop above: in API-only mode this is the only
    # thing that turns incoming signals into predictions.
    if settings.AUTO_START_SIGNAL_RESEARCH_QUEUE and not started_workers.get(
        "signal_research_queue"
    ):
        try:
            from app.core.scheduler import start_signal_research_queue
            start_signal_research_queue(
                settings.SIGNAL_RESEARCH_CONCURRENCY,
                settings.SIGNAL_RESEARCH_SCAN_SECONDS,
            )
        except Exception as e:
            logger.warning(f"Signal research queue failed to start: {e}")

    # Obsidian vault auto-sync. Started here too so the vault stays current in
    # API-only mode, same reasoning as the loops above.
    if settings.AUTO_START_VAULT_SYNC_LOOP and not started_workers.get("vault_sync_loop"):
        try:
            from app.core.scheduler import start_vault_sync_loop
            start_vault_sync_loop(settings.VAULT_SYNC_INTERVAL_SECONDS)
        except Exception as e:
            logger.warning(f"Vault sync loop failed to start: {e}")

    # JARVIS learning loop — settles published proposals against real candles so
    # the assistant's confidence is measured rather than asserted. Started here
    # too so it runs in API-only mode, same reasoning as the research loop above.
    if settings.AUTO_START_JARVIS_LEARNING_LOOP and not started_workers.get(
        "jarvis_learning_loop"
    ):
        try:
            from app.core.scheduler import start_jarvis_learning_loop
            start_jarvis_learning_loop()
        except Exception as e:
            logger.warning(f"JARVIS learning loop failed to start: {e}")

    # Realtime price-tick fan-out for SSE subscribers (idempotent; self-throttles
    # to zero work when no client is connected).
    if settings.AUTO_START_PRICE_TICK_LOOP:
        try:
            from app.core.scheduler import start_price_tick_loop
            start_price_tick_loop()
        except Exception as e:
            logger.warning(f"Price tick loop failed to start: {e}")

    # Bitcoin 1064-day cycle detector — watches the calendar for a phase turn
    # and announces it to the room, the signal feed and the alert channels.
    try:
        from app.core.scheduler import start_cycle_detector_loop
        start_cycle_detector_loop(getattr(settings, "CYCLE_RECHECK_INTERVAL_SECONDS", 3600))
    except Exception as e:
        logger.warning(f"Cycle detector failed to start: {e}")

    # Whale watch — reads the curated BTC whale registry so the desk sees the
    # big money accumulate or distribute before it becomes a candle.
    try:
        from app.core.scheduler import start_whale_watch_loop
        start_whale_watch_loop()
    except Exception as e:
        logger.warning(f"Whale watch failed to start: {e}")

    # Telegram signal monitor — start it with the app, not 63 s later when a
    # browser happens to hit a /plugins/telegram/* endpoint. ensure_started is
    # idempotent and needs only a running loop (no credentials, no DB rows). The
    # monitor + bot polling are critical: they must be live at boot regardless of
    # tier so signals never silently stop arriving.
    try:
        from plugins.TelegramSignalNewsPlugin.backend.services.monitor_service import (
            signal_monitor,
        )
        from app.core.database import AsyncSessionLocal as _TgSession
        signal_monitor.ensure_started(_TgSession)
        logger.info("✅ Telegram signal monitor started (lifespan autostart)")
    except Exception as e:  # never fatal — plugin may be absent
        logger.warning(f"Telegram monitor autostart skipped: {e}")

    # Unified task supervisor — register every background loop as a supervised
    # adapter (observational: does not change what starts) and launch the memory
    # watchdog so the app throttles itself under pressure instead of swapping.
    try:
        from app.core.task_supervisor import supervisor
        from app.core.task_registry import register_core_tasks
        register_core_tasks()
        # Critical plugin loops: Telegram monitor + bot polling.
        try:
            from plugins.TelegramSignalNewsPlugin.backend.services.monitor_service import (
                signal_monitor as _tg,
            )
            from app.core.database import AsyncSessionLocal as _TgSession2
            from app.core.task_supervisor import TaskSpec
            from app.core import resource_tier as _rt
            supervisor.register(TaskSpec(
                id="telegram_monitor", name="Telegram signal monitor", source="plugin",
                category=_rt.task_category("telegram_monitor"), default_interval_s=60,
                critical=True, min_tier=_rt.task_min_tier("telegram_monitor"),
                start=lambda: _tg.ensure_started(_TgSession2), stop=_tg.stop,
                status=lambda: {"running": _tg.is_running()},
            ))
            supervisor.register(TaskSpec(
                id="telegram_bot_polling", name="Telegram bot polling", source="plugin",
                category=_rt.task_category("telegram_bot_polling"), default_interval_s=10,
                critical=True, min_tier=_rt.task_min_tier("telegram_bot_polling"),
                start=lambda: _tg.start_bot_polling(_TgSession2), stop=_tg.stop_bot_polling,
            ))
        except Exception as e:
            logger.debug(f"Telegram task registration skipped: {e}")
        supervisor.start_watchdog()
    except Exception as e:
        logger.warning(f"Task supervisor init skipped: {e}")

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
    try:
        from app.core.loop_monitor import loop_monitor
        await loop_monitor.stop()
    except Exception:
        pass
    try:
        from app.core.task_supervisor import supervisor
        await supervisor.stop_watchdog()
    except Exception:
        pass
    try:
        from app.core.offload import shutdown as _offload_shutdown
        _offload_shutdown()
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


if not settings.STATIC_DIR:
    # Skipped in the desktop app, where "/" serves the bundled UI instead
    # (see the StaticFiles mount at the bottom of this module).
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


# ── Bundled frontend (desktop app only) ────────────────────
#
# The desktop build exports the Next.js app to static files and serves them from
# this process, so UI and API share one origin. That removes CORS entirely and —
# because the port is chosen at launch — lets the frontend discover the API from
# `window.location` instead of a port baked in at build time.
#
# Mounted last so every API route and plugin router above it wins the match.

if settings.STATIC_DIR:
    from pathlib import Path as _Path
    from fastapi.staticfiles import StaticFiles
    from starlette.exceptions import HTTPException
    from starlette.responses import FileResponse

    _static_root = _Path(settings.STATIC_DIR)

    if not (_static_root / "index.html").is_file():
        logger.error(
            f"TRADEBOT_STATIC_DIR={_static_root} has no index.html — "
            "the frontend export is missing or incomplete; UI will not load"
        )
    else:
        class _ExportedSPA(StaticFiles):
            """StaticFiles that serves the app shell for unmatched page routes.

            The Next.js export writes `out/<route>/index.html` per page, and
            StaticFiles redirects `/trading` → `/trading/` on its own, so most
            navigation already works. This covers the remainder: a hard reload
            of a client-side route that has no exported directory would
            otherwise return the API's JSON 404 instead of the app.

            Only extensionless paths fall back. A missing `.js` or `.css` must
            still 404 — answering those with HTML turns a broken build into a
            confusing MIME-type error in the console instead of a clear miss.
            """

            async def get_response(self, path: str, scope):
                try:
                    return await super().get_response(path, scope)
                except HTTPException as exc:
                    if exc.status_code != 404 or _Path(path).suffix:
                        raise
                return FileResponse(_static_root / "index.html")

        app.mount("/", _ExportedSPA(directory=_static_root, html=True), name="ui")
        logger.info(f"🖥️  Serving bundled frontend from {_static_root}")
