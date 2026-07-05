"""
Background Scheduler
Runs sentiment/news every 5 minutes and signals every 3 minutes by default.
Also triggers simulation and live auto-trade cycles.

Dedicated auto-trade loops (separate from the scheduler) that persist
across frontend page reloads and can be started / stopped via API.
"""
import asyncio
from datetime import datetime
from loguru import logger

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.timezone import now_sast
from app.monitoring.alerts import AlertService
from app.monitoring.metrics import record_scheduler_cycle
from app.sentiment.enhanced_service import EnhancedSentimentService
from app.signals.pipeline import run_signal_pipeline
from app.signals.trending_sync import sync_trending_pairs
from app.signals.rug_pull_detector import run_rug_pull_cycle, run_sniper_cycle
from app.trading.simulation import SimulationEngine
from app.trading.live import LiveTradeEngine
from app.models.database import SignalMonitorPair, LiveTradeSettings
from sqlalchemy import select


_scheduler_task: asyncio.Task | None = None
_last_run: dict = {"sentiment": None, "signals": None, "auto_trade": None, "live_auto_trade": None}
_running = False

SCHEDULER_TICK_SECONDS = 60
SENTIMENT_INTERVAL_SECONDS = max(60, int(settings.SENTIMENT_PIPELINE_INTERVAL_SECONDS))
SIGNALS_INTERVAL_SECONDS = max(60, int(settings.SIGNALS_PIPELINE_INTERVAL_SECONDS))

# ── Dedicated Auto-Trade Loop ───────────────────────────────
_auto_trade_task: asyncio.Task | None = None
_auto_trade_running = False
_auto_trade_interval = 60  # seconds between cycles
_auto_trade_last_run: dict | None = None
_auto_trade_started_at: str | None = None


async def _run_sentiment_cycle(db, cycle_start: datetime):
    """Run sentiment/news refresh and update scheduler state."""
    global _last_run
    try:
        sent_result = await EnhancedSentimentService.run_full_cycle(db)
        _last_run["sentiment"] = {
            "at": cycle_start.isoformat(),
            "articles": sent_result.get("total_articles", 0),
            "symbols_scored": sent_result.get("symbols_scored", 0),
            "status": "ok",
        }
        logger.info(
            f"✅ Sentiment: {sent_result.get('total_articles', 0)} articles, "
            f"{sent_result.get('symbols_scored', 0)} symbols"
        )
        record_scheduler_cycle("sentiment", "ok")
    except Exception as e:
        logger.error(f"❌ Sentiment cycle failed: {e}")
        record_scheduler_cycle("sentiment", "error")
        _last_run["sentiment"] = {
            "at": cycle_start.isoformat(),
            "status": "error",
            "error": str(e),
        }
        await AlertService.notify(
            title="Sentiment cycle failed",
            message="Scheduled sentiment run failed",
            level="ERROR",
            details={"error": str(e)},
        )


async def _run_cycle(run_sentiment: bool = True):
    """Single scheduler cycle: optional sentiment + signals + auto-trade."""
    global _last_run
    cycle_start = now_sast()
    logger.info("⏰ [SCHEDULER] Cycle starting...")

    async with AsyncSessionLocal() as db:
        # ── 1. Sentiment Cycle ──
        if run_sentiment:
            await _run_sentiment_cycle(db, cycle_start)
        else:
            logger.debug("⏭️ Sentiment cycle skipped for this tick")

        # ── 2. Trending Pairs Sync ──
        try:
            trending_result = await sync_trending_pairs(db)
            _last_run["trending_sync"] = {
                "at": cycle_start.isoformat(),
                "added": len(trending_result.get("added", [])),
                "removed": len(trending_result.get("removed", [])),
                "total_trending": trending_result.get("total_trending", 0),
                "status": "ok",
            }
            record_scheduler_cycle("trending_sync", "ok")
        except Exception as e:
            logger.error(f"❌ Trending sync failed: {e}")
            record_scheduler_cycle("trending_sync", "error")
            _last_run["trending_sync"] = {
                "at": cycle_start.isoformat(),
                "status": "error",
                "error": str(e),
            }

        # ── 2b. Rug Pull Scan (tokens pumped above configured threshold) ──
        try:
            rp_result = await run_rug_pull_cycle(db)
            scan = rp_result.get("scan", {})
            updates = rp_result.get("updates", {})
            _last_run["rug_pull_scan"] = {
                "at": cycle_start.isoformat(),
                "new_tokens": len(scan.get("new", [])),
                "updated": updates.get("updated", 0),
                "dumped": len(updates.get("dumped", [])),
                "status": "ok",
            }
            if scan.get("new"):
                logger.info(f"🚨 [RUG PULL] {len(scan['new'])} new pump tokens detected")
            record_scheduler_cycle("rug_pull_scan", "ok")
        except Exception as e:
            logger.error(f"❌ Rug pull scan failed: {e}")
            record_scheduler_cycle("rug_pull_scan", "error")
            _last_run["rug_pull_scan"] = {
                "at": cycle_start.isoformat(),
                "status": "error",
                "error": str(e),
            }

        # ── 3. Signal Pipeline (use DB-configured pairs + timeframe) ──
        try:
            rows = (await db.execute(
                select(SignalMonitorPair.symbol).where(SignalMonitorPair.is_active == True)
            )).scalars().all()
            custom_pairs = list(rows) if rows else None

            # Load the configured timeframe from live settings
            configured_tf = "1h"
            try:
                lts_result = await db.execute(select(LiveTradeSettings).limit(1))
                lts = lts_result.scalar_one_or_none()
                if lts and lts.auto_trade_timeframe:
                    configured_tf = lts.auto_trade_timeframe
            except Exception:
                pass

            sig_result = await run_signal_pipeline(db, pairs=custom_pairs, timeframe=configured_tf)
            _last_run["signals"] = {
                "at": cycle_start.isoformat(),
                "pairs_analyzed": sig_result.get("pairs_analyzed", 0),
                "signals_created": sig_result.get("signals_created", 0),
                "elapsed_s": sig_result.get("elapsed_s", 0),
                "status": "ok",
            }
            logger.info(
                f"✅ Signals: {sig_result.get('signals_created', 0)} signals "
                f"from {sig_result.get('pairs_analyzed', 0)} pairs"
            )
            record_scheduler_cycle("signals", "ok")
        except Exception as e:
            logger.error(f"❌ Signal pipeline failed: {e}")
            record_scheduler_cycle("signals", "error")
            _last_run["signals"] = {
                "at": cycle_start.isoformat(),
                "status": "error",
                "error": str(e),
            }
            await AlertService.notify(
                title="Signal pipeline failed",
                message="Scheduled signal pipeline run failed",
                level="ERROR",
                details={"error": str(e)},
            )

        # ── 4. Simulation Auto-Trade ──
        try:
            auto_result = await SimulationEngine.auto_trade_cycle(db)
            _last_run["auto_trade"] = {
                "at": cycle_start.isoformat(),
                "status": "ok",
                **{k: v for k, v in auto_result.items() if k != "skipped"},
                "skipped": auto_result.get("skipped", False),
            }
            if not auto_result.get("skipped"):
                logger.info(
                    f"✅ Auto-trade: {len(auto_result.get('orders_placed', []))} orders"
                )
            record_scheduler_cycle("simulation_auto_trade", "ok")
        except Exception as e:
            logger.error(f"❌ Auto-trade cycle failed: {e}")
            record_scheduler_cycle("simulation_auto_trade", "error")
            _last_run["auto_trade"] = {
                "at": cycle_start.isoformat(),
                "status": "error",
                "error": str(e),
            }

        # ── 5. Live Auto-Trade ──
        try:
            live_result = await LiveTradeEngine.auto_trade_cycle(db)
            _last_run["live_auto_trade"] = {
                "at": cycle_start.isoformat(),
                "status": "ok",
                **{k: v for k, v in live_result.items() if k != "skipped"},
                "skipped": live_result.get("skipped", False),
            }
            if not live_result.get("skipped"):
                logger.info(
                    f"✅ Live auto-trade: {len(live_result.get('orders_placed', []))} orders"
                )
            record_scheduler_cycle("live_auto_trade", "ok")
        except Exception as e:
            logger.error(f"❌ Live auto-trade cycle failed: {e}")
            record_scheduler_cycle("live_auto_trade", "error")
            _last_run["live_auto_trade"] = {
                "at": cycle_start.isoformat(),
                "status": "error",
                "error": str(e),
            }
            await AlertService.notify(
                title="Live auto-trade cycle failed",
                message="Scheduled live auto-trade cycle failed",
                level="ERROR",
                details={"error": str(e)},
            )

    elapsed = (now_sast() - cycle_start).total_seconds()
    logger.info(f"⏰ [SCHEDULER] Cycle complete in {elapsed:.1f}s")


async def _run_sentiment_only():
    """Run only the sentiment/news refresh path."""
    cycle_start = now_sast()
    logger.info("⏰ [SCHEDULER] Sentiment-only tick starting...")
    async with AsyncSessionLocal() as db:
        await _run_sentiment_cycle(db, cycle_start)
    elapsed = (now_sast() - cycle_start).total_seconds()
    logger.info(f"⏰ [SCHEDULER] Sentiment-only tick complete in {elapsed:.1f}s")


async def _scheduler_loop():
    """Tick loop that runs signals every 3m and sentiment every 5m."""
    global _running
    _running = True
    # Small initial delay to let server start up
    await asyncio.sleep(10)

    last_signals_run: datetime | None = None
    last_sentiment_run: datetime | None = None

    while _running:
        now = now_sast()
        signals_due = (
            last_signals_run is None
            or (now - last_signals_run).total_seconds() >= SIGNALS_INTERVAL_SECONDS
        )
        sentiment_due = (
            last_sentiment_run is None
            or (now - last_sentiment_run).total_seconds() >= SENTIMENT_INTERVAL_SECONDS
        )

        try:
            if signals_due:
                await _run_cycle(run_sentiment=sentiment_due)
                finished_at = now_sast()
                last_signals_run = finished_at
                if sentiment_due:
                    last_sentiment_run = finished_at
            elif sentiment_due:
                await _run_sentiment_only()
                last_sentiment_run = now_sast()
        except Exception as e:
            logger.error(f"Scheduler loop error: {e}")

        await asyncio.sleep(SCHEDULER_TICK_SECONDS)


def start_scheduler():
    """Start the background scheduler task."""
    global _scheduler_task
    if _scheduler_task is not None and not _scheduler_task.done():
        logger.warning("Scheduler already running")
        return
    _scheduler_task = asyncio.create_task(_scheduler_loop())
    logger.info(
        "⏰ Scheduler started — "
        f"signals every {SIGNALS_INTERVAL_SECONDS}s, "
        f"sentiment every {SENTIMENT_INTERVAL_SECONDS}s "
        f"(tick {SCHEDULER_TICK_SECONDS}s)"
    )


def stop_scheduler():
    """Stop the background scheduler."""
    global _running, _scheduler_task
    _running = False
    if _scheduler_task:
        _scheduler_task.cancel()
        _scheduler_task = None
    logger.info("⏰ Scheduler stopped")


def get_scheduler_status() -> dict:
    """Get current scheduler state."""
    return {
        "running": _running,
        "interval_seconds": SIGNALS_INTERVAL_SECONDS,
        "tick_seconds": SCHEDULER_TICK_SECONDS,
        "signals_interval_seconds": SIGNALS_INTERVAL_SECONDS,
        "sentiment_interval_seconds": SENTIMENT_INTERVAL_SECONDS,
        "last_run": _last_run,
    }


# ── Dedicated Auto-Trade Loop ──────────────────────────────

async def _auto_trade_loop():
    """Persistent loop that runs auto-trade cycles at _auto_trade_interval."""
    global _auto_trade_running, _auto_trade_last_run

    _auto_trade_running = True
    logger.info(
        f"🤖 [AUTO-TRADE LOOP] Started — every {_auto_trade_interval}s"
    )

    while _auto_trade_running:
        cycle_start = now_sast()
        try:
            async with AsyncSessionLocal() as db:
                try:
                    result = await SimulationEngine.auto_trade_cycle(db)
                except asyncio.CancelledError:
                    raise
                except Exception as inner_e:
                    raise inner_e
            _auto_trade_last_run = {
                "at": cycle_start.isoformat(),
                "status": "ok",
                **{k: v for k, v in result.items() if k != "skipped"},
                "skipped": result.get("skipped", False),
            }
            if not result.get("skipped"):
                logger.info(
                    f"🤖 [AUTO-TRADE LOOP] "
                    f"{len(result.get('orders_placed', []))} orders placed"
                )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"🤖 [AUTO-TRADE LOOP] Cycle error: {e}")
            _auto_trade_last_run = {
                "at": cycle_start.isoformat(),
                "status": "error",
                "error": str(e),
            }

        # Wait for the next tick — break early if stopped
        try:
            await asyncio.sleep(_auto_trade_interval)
        except asyncio.CancelledError:
            break

    _auto_trade_running = False
    logger.info("🤖 [AUTO-TRADE LOOP] Stopped")


def start_auto_trade_loop(interval: int = 60):
    """Start the persistent auto-trade loop (survives page refreshes)."""
    global _auto_trade_task, _auto_trade_interval, _auto_trade_running, _auto_trade_started_at

    if _auto_trade_task is not None and not _auto_trade_task.done():
        logger.warning("Auto-trade loop already running")
        return False

    _auto_trade_interval = max(10, interval)  # minimum 10s safety
    _auto_trade_running = True
    _auto_trade_started_at = now_sast().isoformat()
    _auto_trade_task = asyncio.create_task(_auto_trade_loop())
    logger.info(f"🤖 Auto-trade loop started (interval={_auto_trade_interval}s)")
    return True


def stop_auto_trade_loop():
    """Stop the persistent auto-trade loop."""
    global _auto_trade_running, _auto_trade_task, _auto_trade_started_at

    if not _auto_trade_running and (_auto_trade_task is None or _auto_trade_task.done()):
        logger.warning("Auto-trade loop is not running")
        return False

    _auto_trade_running = False
    if _auto_trade_task:
        _auto_trade_task.cancel()
        _auto_trade_task = None
    _auto_trade_started_at = None
    logger.info("🤖 Auto-trade loop stopped")
    return True


def get_auto_trade_loop_status() -> dict:
    """Return the current state of the dedicated auto-trade loop."""
    return {
        "running": _auto_trade_running,
        "interval_seconds": _auto_trade_interval,
        "started_at": _auto_trade_started_at,
        "last_run": _auto_trade_last_run,
    }


# ── Dedicated LIVE Auto-Trade Loop ─────────────────────────

_live_auto_trade_task: asyncio.Task | None = None
_live_auto_trade_running = False
_live_auto_trade_interval = 60
_live_auto_trade_last_run: dict | None = None
_live_auto_trade_started_at: str | None = None


async def _live_auto_trade_loop():
    """Persistent loop that runs live auto-trade cycles."""
    global _live_auto_trade_running, _live_auto_trade_last_run

    _live_auto_trade_running = True
    logger.info(
        f"🔴 [LIVE AUTO-TRADE LOOP] Started — every {_live_auto_trade_interval}s"
    )

    while _live_auto_trade_running:
        cycle_start = now_sast()
        try:
            async with AsyncSessionLocal() as db:
                try:
                    result = await LiveTradeEngine.auto_trade_cycle(db)
                except asyncio.CancelledError:
                    raise
                except Exception as inner_e:
                    raise inner_e
            _live_auto_trade_last_run = {
                "at": cycle_start.isoformat(),
                "status": "ok",
                **{k: v for k, v in result.items() if k != "skipped"},
                "skipped": result.get("skipped", False),
            }
            if not result.get("skipped"):
                logger.info(
                    f"🔴 [LIVE AUTO-TRADE LOOP] "
                    f"{len(result.get('orders_placed', []))} orders placed"
                )
        except asyncio.CancelledError:
            break
        except Exception as e:
            err_safe = str(e).replace("{", "{{").replace("}", "}}")
            logger.exception(f"🔴 [LIVE AUTO-TRADE LOOP] Cycle error: {err_safe}")
            _live_auto_trade_last_run = {
                "at": cycle_start.isoformat(),
                "status": "error",
                "error": str(e),
            }

        try:
            await asyncio.sleep(_live_auto_trade_interval)
        except asyncio.CancelledError:
            break

    _live_auto_trade_running = False
    logger.info("🔴 [LIVE AUTO-TRADE LOOP] Stopped")


def start_live_auto_trade_loop(interval: int = 60):
    """Start the persistent live auto-trade loop."""
    global _live_auto_trade_task, _live_auto_trade_interval
    global _live_auto_trade_running, _live_auto_trade_started_at

    if _live_auto_trade_task is not None and not _live_auto_trade_task.done():
        logger.warning("Live auto-trade loop already running")
        return False

    _live_auto_trade_interval = max(30, interval)  # minimum 30s for live
    _live_auto_trade_running = True
    _live_auto_trade_started_at = now_sast().isoformat()
    _live_auto_trade_task = asyncio.create_task(_live_auto_trade_loop())
    logger.info(f"🔴 Live auto-trade loop started (interval={_live_auto_trade_interval}s)")
    return True


def stop_live_auto_trade_loop():
    """Stop the persistent live auto-trade loop."""
    global _live_auto_trade_running, _live_auto_trade_task, _live_auto_trade_started_at

    if not _live_auto_trade_running and (_live_auto_trade_task is None or _live_auto_trade_task.done()):
        logger.warning("Live auto-trade loop is not running")
        return False

    _live_auto_trade_running = False
    if _live_auto_trade_task:
        _live_auto_trade_task.cancel()
        _live_auto_trade_task = None
    _live_auto_trade_started_at = None
    logger.info("🔴 Live auto-trade loop stopped")
    return True


def get_live_auto_trade_loop_status() -> dict:
    """Return the current state of the live auto-trade loop."""
    return {
        "running": _live_auto_trade_running,
        "interval_seconds": _live_auto_trade_interval,
        "started_at": _live_auto_trade_started_at,
        "last_run": _live_auto_trade_last_run,
    }


# ── Position Monitor Loop (AI agent position reviews) ──────

_position_monitor_task: asyncio.Task | None = None
_position_monitor_running = False
_position_monitor_interval = 900  # 15 minutes
_position_monitor_last_run: dict | None = None
_position_monitor_started_at: str | None = None


async def _position_monitor_loop():
    """Persistent loop that reviews open positions every minute using AI agents."""
    global _position_monitor_running, _position_monitor_last_run

    _position_monitor_running = True
    interval_label = (
        f"{_position_monitor_interval}s"
        if _position_monitor_interval < 3600
        else f"{_position_monitor_interval / 3600:.1f}h"
    )
    logger.info(f"🔍 [POSITION MONITOR] Started — every {interval_label}")

    while _position_monitor_running:
        cycle_start = now_sast()
        try:
            # min_hold_hours matches the interval so positions are re-reviewed each cycle
            min_hold = max(_position_monitor_interval / 3600, 0.015)  # at least ~1 min
            async with AsyncSessionLocal() as db:
                from app.agents.orchestrator import AgentOrchestrator
                result = await AgentOrchestrator.analyze_positions(db, min_hold_hours=min_hold)

                # Also analyze sim positions if sim AI is enabled
                sim_result = {"skipped": True}
                try:
                    sim_result = await AgentOrchestrator.analyze_sim_positions(db, min_hold_hours=min_hold)
                except Exception as e:
                    logger.warning(f"🔍 [POSITION MONITOR] Sim analysis error: {e}")

            _position_monitor_last_run = {
                "at": cycle_start.isoformat(),
                "status": "ok",
                "positions_reviewed": result.get("positions_reviewed", 0),
                "actions_taken": len(result.get("actions_taken", [])),
                "skipped": result.get("skipped", False),
                "sim_positions_reviewed": sim_result.get("positions_reviewed", 0),
                "sim_actions_taken": len(sim_result.get("actions_taken", [])),
            }

            live_reviewed = result.get("positions_reviewed", 0)
            sim_reviewed = sim_result.get("positions_reviewed", 0)
            if live_reviewed or sim_reviewed:
                logger.info(
                    f"🔍 [POSITION MONITOR] Live: {live_reviewed} reviewed, "
                    f"Sim: {sim_reviewed} reviewed"
                )
            record_scheduler_cycle("position_monitor", "ok")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"🔍 [POSITION MONITOR] Cycle error: {e}")
            _position_monitor_last_run = {
                "at": cycle_start.isoformat(),
                "status": "error",
                "error": str(e),
            }
            record_scheduler_cycle("position_monitor", "error")

        try:
            await asyncio.sleep(_position_monitor_interval)
        except asyncio.CancelledError:
            break

    _position_monitor_running = False
    logger.info("🔍 [POSITION MONITOR] Stopped")


def start_position_monitor(interval: int = 900):
    """Start the position monitoring loop (default: every 900 seconds / 15 minutes)."""
    global _position_monitor_task, _position_monitor_interval
    global _position_monitor_running, _position_monitor_started_at

    if _position_monitor_task is not None and not _position_monitor_task.done():
        logger.warning("Position monitor already running")
        return False

    _position_monitor_interval = max(30, interval)  # minimum 30 seconds
    _position_monitor_running = True
    _position_monitor_started_at = now_sast().isoformat()
    _position_monitor_task = asyncio.create_task(_position_monitor_loop())
    logger.info(
        f"🔍 Position monitor started (interval={_position_monitor_interval}s / "
        f"{_position_monitor_interval / 3600:.1f}h)"
    )
    return True


def stop_position_monitor():
    """Stop the position monitoring loop."""
    global _position_monitor_running, _position_monitor_task, _position_monitor_started_at

    if not _position_monitor_running and (_position_monitor_task is None or _position_monitor_task.done()):
        logger.warning("Position monitor is not running")
        return False

    _position_monitor_running = False
    if _position_monitor_task:
        _position_monitor_task.cancel()
        _position_monitor_task = None
    _position_monitor_started_at = None
    logger.info("🔍 Position monitor stopped")
    return True


def get_position_monitor_status() -> dict:
    """Return the current state of the position monitor loop."""
    return {
        "running": _position_monitor_running,
        "interval_seconds": _position_monitor_interval,
        "started_at": _position_monitor_started_at,
        "last_run": _position_monitor_last_run,
    }


# ── Rug Pull Sniper Loop (60s fast scan + auto-trade) ──────

_sniper_task: asyncio.Task | None = None
_sniper_running = False
_sniper_interval = 60  # seconds — scan every minute
_sniper_last_run: dict | None = None
_sniper_started_at: str | None = None


async def _sniper_loop():
    """Persistent loop that scans rug pull tokens every 60s for buying power decline
    and auto-executes sniper short entries via live trading."""
    global _sniper_running, _sniper_last_run

    _sniper_running = True
    logger.info(
        f"🎯 [SNIPER LOOP] Started — scanning every {_sniper_interval}s"
    )

    while _sniper_running:
        cycle_start = now_sast()
        try:
            async with AsyncSessionLocal() as db:
                try:
                    result = await run_sniper_cycle(db)
                except asyncio.CancelledError:
                    raise
                except Exception as inner_e:
                    raise inner_e

            _sniper_last_run = {
                "at": cycle_start.isoformat(),
                "status": "ok",
                "scanned": result.get("scanned", 0),
                "declining": result.get("declining", 0),
                "signals_created": result.get("signals_created", 0),
                "trades_executed": result.get("trades_executed", 0),
                "positions_monitored": result.get("positions_monitored", 0),
                "profits_taken": result.get("profits_taken", 0),
                "re_entries": result.get("re_entries", 0),
            }
            declining = result.get("declining", 0)
            trades = result.get("trades_executed", 0)
            profits = result.get("profits_taken", 0)
            monitoring = result.get("positions_monitored", 0)
            if declining > 0 or trades > 0 or profits > 0:
                logger.info(
                    f"🎯 [SNIPER LOOP] {result.get('scanned', 0)} scanned | "
                    f"{declining} declining | {trades} trades | "
                    f"{monitoring} monitoring | {profits} profits"
                )
        except asyncio.CancelledError:
            break
        except Exception as e:
            err_safe = str(e).replace("{", "{{").replace("}", "}}")
            logger.error(f"🎯 [SNIPER LOOP] Cycle error: {err_safe}")
            _sniper_last_run = {
                "at": cycle_start.isoformat(),
                "status": "error",
                "error": str(e),
            }
            record_scheduler_cycle("sniper_loop", "error")

        try:
            await asyncio.sleep(_sniper_interval)
        except asyncio.CancelledError:
            break

    _sniper_running = False
    logger.info("🎯 [SNIPER LOOP] Stopped")


def start_sniper_loop(interval: int = 60):
    """Start the rug pull sniper loop (default: every 60s)."""
    global _sniper_task, _sniper_interval, _sniper_running, _sniper_started_at

    if _sniper_task is not None and not _sniper_task.done():
        logger.warning("Sniper loop already running")
        return False

    _sniper_interval = max(30, interval)  # minimum 30s for safety
    _sniper_running = True
    _sniper_started_at = now_sast().isoformat()
    _sniper_task = asyncio.create_task(_sniper_loop())
    logger.info(f"🎯 Sniper loop started (interval={_sniper_interval}s)")
    return True


def stop_sniper_loop():
    """Stop the rug pull sniper loop."""
    global _sniper_running, _sniper_task, _sniper_started_at

    if not _sniper_running and (_sniper_task is None or _sniper_task.done()):
        logger.warning("Sniper loop is not running")
        return False

    _sniper_running = False
    if _sniper_task:
        _sniper_task.cancel()
        _sniper_task = None
    _sniper_started_at = None
    logger.info("🎯 Sniper loop stopped")
    return True


def get_sniper_loop_status() -> dict:
    """Return the current state of the sniper loop."""
    return {
        "running": _sniper_running,
        "interval_seconds": _sniper_interval,
        "started_at": _sniper_started_at,
        "last_run": _sniper_last_run,
    }


# ── Pre-Pump Monitor Loop ──────────────────────────────────

_pump_monitor_task: asyncio.Task | None = None
_pump_monitor_running = False
_pump_monitor_interval = 120  # seconds — scan every 2 minutes
_pump_monitor_last_run: dict | None = None
_pump_monitor_started_at: str | None = None


async def _pump_monitor_loop():
    """Persistent loop that scans for pre-pump tokens every N seconds."""
    global _pump_monitor_running, _pump_monitor_last_run
    from app.signals.pump_detector import run_pump_monitor_cycle

    _pump_monitor_running = True
    logger.info(
        f"🚀 [PUMP MONITOR LOOP] Started — scanning every {_pump_monitor_interval}s"
    )

    while _pump_monitor_running:
        cycle_start = now_sast()
        try:
            async with AsyncSessionLocal() as db:
                result = await run_pump_monitor_cycle(db)

            scan = result.get("scan", {})
            _pump_monitor_last_run = {
                "at": cycle_start.isoformat(),
                "status": "ok",
                "new": len(scan.get("new", [])),
                "updated": len(scan.get("updated", [])),
                "total_scanned": scan.get("total_scanned", 0),
                "signals_created": result.get("signals_created", 0),
                "pumped_count": result.get("pumped_count", 0),
            }
            new_count = len(scan.get("new", []))
            signals = result.get("signals_created", 0)
            pumped = result.get("pumped_count", 0)
            if new_count > 0 or signals > 0 or pumped > 0:
                logger.info(
                    f"🚀 [PUMP MONITOR LOOP] New: {new_count} | "
                    f"Signals: {signals} | Pumped: {pumped}"
                )
        except asyncio.CancelledError:
            break
        except Exception as e:
            err_safe = str(e).replace("{", "{{").replace("}", "}}")
            logger.error(f"🚀 [PUMP MONITOR LOOP] Cycle error: {err_safe}")
            _pump_monitor_last_run = {
                "at": cycle_start.isoformat(),
                "status": "error",
                "error": str(e),
            }
            record_scheduler_cycle("pump_monitor_loop", "error")

        try:
            await asyncio.sleep(_pump_monitor_interval)
        except asyncio.CancelledError:
            break

    _pump_monitor_running = False
    logger.info("🚀 [PUMP MONITOR LOOP] Stopped")


def start_pump_monitor_loop(interval: int = 120):
    """Start the pump monitor loop (default: every 120s)."""
    global _pump_monitor_task, _pump_monitor_interval, _pump_monitor_running, _pump_monitor_started_at

    if _pump_monitor_task is not None and not _pump_monitor_task.done():
        logger.warning("Pump monitor loop already running")
        return False

    _pump_monitor_interval = max(60, interval)  # minimum 60s
    _pump_monitor_running = True
    _pump_monitor_started_at = now_sast().isoformat()
    _pump_monitor_task = asyncio.create_task(_pump_monitor_loop())
    logger.info(f"🚀 Pump monitor loop started (interval={_pump_monitor_interval}s)")
    return True


def stop_pump_monitor_loop():
    """Stop the pump monitor loop."""
    global _pump_monitor_running, _pump_monitor_task, _pump_monitor_started_at

    if not _pump_monitor_running and (_pump_monitor_task is None or _pump_monitor_task.done()):
        logger.warning("Pump monitor loop is not running")
        return False

    _pump_monitor_running = False
    if _pump_monitor_task:
        _pump_monitor_task.cancel()
        _pump_monitor_task = None
    _pump_monitor_started_at = None
    logger.info("🚀 Pump monitor loop stopped")
    return True


def get_pump_monitor_status() -> dict:
    """Return the current state of the pump monitor loop."""
    return {
        "running": _pump_monitor_running,
        "interval_seconds": _pump_monitor_interval,
        "started_at": _pump_monitor_started_at,
        "last_run": _pump_monitor_last_run,
    }


# ── Crypto Pair Catalog Sync Loop ──────────────────────────

_pair_catalog_task: asyncio.Task | None = None
_pair_catalog_running = False
_pair_catalog_last_run: dict | None = None
_pair_catalog_started_at: str | None = None


async def _pair_catalog_sync_loop():
    """
    Keep the crypto-pair catalog fresh.

    Runs a fast market-cap/volume refresh every ``refresh_minutes`` and a slower
    full enrich (names + lightweight profiles) every ``full_hours``. On the very
    first tick it does a full sync if the catalog is empty so JARVIS has names +
    live metadata as soon as possible. Fully self-healing and non-blocking.
    """
    global _pair_catalog_running, _pair_catalog_last_run
    from app.services import pair_catalog

    refresh_minutes = max(5, int(getattr(settings, "PAIR_CATALOG_REFRESH_MINUTES", 15)))
    full_hours = max(1, int(getattr(settings, "PAIR_CATALOG_FULL_SYNC_HOURS", 6)))
    refresh_seconds = refresh_minutes * 60
    full_seconds = full_hours * 3600

    _pair_catalog_running = True
    logger.info(
        f"🪙 [PAIR CATALOG] Started — refresh every {refresh_minutes}m, "
        f"full enrich every {full_hours}h"
    )

    # Small startup delay so it never competes with critical boot work.
    try:
        await asyncio.sleep(15)
    except asyncio.CancelledError:
        _pair_catalog_running = False
        return

    last_full = 0.0
    import time as _time

    # First tick: full sync when empty, otherwise a quick refresh.
    try:
        empty = await pair_catalog.catalog_is_empty()
        result = await pair_catalog.sync_catalog(full=empty)
        last_full = _time.time() if empty else 0.0
        _pair_catalog_last_run = {"at": now_sast().isoformat(), "status": "ok", **result}
    except asyncio.CancelledError:
        _pair_catalog_running = False
        return
    except Exception as e:
        logger.error(f"🪙 [PAIR CATALOG] Initial sync error: {e}")
        _pair_catalog_last_run = {"at": now_sast().isoformat(), "status": "error", "error": str(e)}

    while _pair_catalog_running:
        try:
            await asyncio.sleep(refresh_seconds)
        except asyncio.CancelledError:
            break

        if not _pair_catalog_running:
            break

        do_full = (_time.time() - last_full) >= full_seconds
        try:
            result = await pair_catalog.sync_catalog(full=do_full)
            if do_full:
                last_full = _time.time()
            _pair_catalog_last_run = {"at": now_sast().isoformat(), "status": "ok", **result}
        except asyncio.CancelledError:
            break
        except Exception as e:
            err_safe = str(e).replace("{", "{{").replace("}", "}}")
            logger.error(f"🪙 [PAIR CATALOG] Cycle error: {err_safe}")
            _pair_catalog_last_run = {"at": now_sast().isoformat(), "status": "error", "error": str(e)}

    _pair_catalog_running = False
    logger.info("🪙 [PAIR CATALOG] Stopped")


def start_pair_catalog_sync_loop():
    """Start the crypto-pair catalog sync loop (idempotent)."""
    global _pair_catalog_task, _pair_catalog_running, _pair_catalog_started_at

    if _pair_catalog_task is not None and not _pair_catalog_task.done():
        logger.warning("Pair catalog sync loop already running")
        return False

    _pair_catalog_running = True
    _pair_catalog_started_at = now_sast().isoformat()
    _pair_catalog_task = asyncio.create_task(_pair_catalog_sync_loop())
    logger.info("🪙 Pair catalog sync loop started")
    return True


def stop_pair_catalog_sync_loop():
    """Stop the crypto-pair catalog sync loop."""
    global _pair_catalog_running, _pair_catalog_task, _pair_catalog_started_at

    if not _pair_catalog_running and (_pair_catalog_task is None or _pair_catalog_task.done()):
        return False

    _pair_catalog_running = False
    if _pair_catalog_task:
        _pair_catalog_task.cancel()
        _pair_catalog_task = None
    _pair_catalog_started_at = None
    logger.info("🪙 Pair catalog sync loop stopped")
    return True


def get_pair_catalog_status() -> dict:
    """Return the current state of the pair catalog sync loop."""
    return {
        "running": _pair_catalog_running,
        "started_at": _pair_catalog_started_at,
        "last_run": _pair_catalog_last_run,
    }


# ── Realtime Price-Tick Fan-Out Loop (SSE) ──────────────────
_price_tick_task: asyncio.Task | None = None
_price_tick_running = False
_price_tick_last_run: dict | None = None
_price_tick_started_at: str | None = None


async def _collect_active_symbols() -> list[str]:
    """Symbols worth streaming prices for: open sim positions + pending signals."""
    from app.models.database import SimPosition, Signal, SignalStatus

    symbols: set[str] = set()
    async with AsyncSessionLocal() as db:
        try:
            sim = await db.execute(
                select(SimPosition.symbol).where(SimPosition.status == "open").distinct()
            )
            symbols.update(s for s in sim.scalars().all() if s)
        except Exception:
            pass
        try:
            sig = await db.execute(
                select(Signal.symbol)
                .where(Signal.status == SignalStatus.PENDING)
                .distinct()
            )
            symbols.update(s for s in sig.scalars().all() if s)
        except Exception:
            pass
    return list(symbols)


async def _fetch_one_price(symbol: str) -> tuple[str, float | None]:
    """Best-effort live price: Bitget ticker first, sniper FX price fallback."""
    from app.exchanges.manager import exchange_manager, SupportedExchange

    try:
        connector = exchange_manager.get_exchange(SupportedExchange.BITGET)
        if connector:
            ticker = await connector.get_ticker(symbol)
            price = ticker.get("last") or ticker.get("close")
            if price:
                return symbol, float(price)
    except Exception:
        pass
    try:
        from plugins.TelegramSignalNewsPlugin.backend.services.sniper_service import _get_live_price
        price = await _get_live_price(symbol)
        if price:
            return symbol, float(price)
    except Exception:
        pass
    return symbol, None


async def _price_tick_loop():
    """
    Broadcast live prices for actively-watched symbols to SSE subscribers.

    Skips fetching entirely when no client is connected (subscriber_count == 0),
    so the loop is effectively free while the dashboard is closed.
    """
    global _price_tick_running, _price_tick_last_run
    from app.core.events import event_bus, Topics

    interval = max(2, int(getattr(settings, "PRICE_TICK_INTERVAL_SECONDS", 5)))
    max_symbols = max(1, int(getattr(settings, "PRICE_TICK_MAX_SYMBOLS", 30)))

    _price_tick_running = True
    logger.info(f"📈 [PRICE TICK] Started — every {interval}s (max {max_symbols} symbols)")

    try:
        await asyncio.sleep(10)  # let boot settle
    except asyncio.CancelledError:
        _price_tick_running = False
        return

    while _price_tick_running:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break
        if not _price_tick_running:
            break

        # Only do work when someone is actually listening.
        if event_bus.subscriber_count() <= 0:
            continue

        try:
            symbols = (await _collect_active_symbols())[:max_symbols]
            if not symbols:
                continue

            results = await asyncio.gather(
                *(_fetch_one_price(s) for s in symbols), return_exceptions=True
            )
            prices = {
                sym: px
                for r in results
                if isinstance(r, tuple)
                for sym, px in [r]
                if px is not None
            }
            if prices:
                await event_bus.publish(Topics.PRICE_TICK, {"prices": prices})
                _price_tick_last_run = {
                    "at": now_sast().isoformat(),
                    "status": "ok",
                    "symbols": len(prices),
                }
        except asyncio.CancelledError:
            break
        except Exception as e:
            err_safe = str(e).replace("{", "{{").replace("}", "}}")
            logger.error(f"📈 [PRICE TICK] Cycle error: {err_safe}")
            _price_tick_last_run = {"at": now_sast().isoformat(), "status": "error", "error": str(e)}

    _price_tick_running = False
    logger.info("📈 [PRICE TICK] Stopped")


def start_price_tick_loop():
    """Start the realtime price-tick fan-out loop (idempotent)."""
    global _price_tick_task, _price_tick_running, _price_tick_started_at

    if _price_tick_task is not None and not _price_tick_task.done():
        logger.warning("Price tick loop already running")
        return False

    _price_tick_running = True
    _price_tick_started_at = now_sast().isoformat()
    _price_tick_task = asyncio.create_task(_price_tick_loop())
    logger.info("📈 Price tick loop started")
    return True


def stop_price_tick_loop():
    """Stop the realtime price-tick fan-out loop."""
    global _price_tick_running, _price_tick_task, _price_tick_started_at

    if not _price_tick_running and (_price_tick_task is None or _price_tick_task.done()):
        return False

    _price_tick_running = False
    if _price_tick_task:
        _price_tick_task.cancel()
        _price_tick_task = None
    _price_tick_started_at = None
    logger.info("📈 Price tick loop stopped")
    return True


def get_price_tick_status() -> dict:
    """Return the current state of the price-tick loop."""
    return {
        "running": _price_tick_running,
        "started_at": _price_tick_started_at,
        "last_run": _price_tick_last_run,
    }
