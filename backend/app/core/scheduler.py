"""
Background Scheduler
Runs sentiment/news every 5 minutes and signals every 3 minutes by default.
Also triggers simulation and live auto-trade cycles.

Dedicated auto-trade loops (separate from the scheduler) that persist
across frontend page reloads and can be started / stopped via API.
"""
import asyncio
import json
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


async def _fetch_one_price(symbol: str, db=None) -> tuple[str, float | None]:
    """Best-effort live price, asked of the source that actually carries it.

    Routed by asset class rather than "try the crypto exchange and see". Asking
    Bitget for XAU/USD is not merely a wasted call — the connector logs it at
    ERROR ("bitget does not have market symbol XAU/USD"), so every gold, FX and
    index symbol on the watchlist produced an error line on every tick.

    Non-crypto goes through ``market_data.get_quote``, which tries the live MT5
    account FIRST: the broker's bid/ask is the price this user's orders actually
    fill at, and a reference feed can sit a few pips away from it. That needs a
    session — without ``db`` the MT5 leg is skipped and it silently falls back
    to Yahoo, which is exactly what "use the source set in MT5 Live" rules out.
    """
    from app.services import market_data

    if market_data.classify(market_data.normalize_symbol(symbol)) != market_data.CRYPTO:
        try:
            quote = await market_data.get_quote(symbol, db=db)
            if quote and quote.price:
                return symbol, float(quote.price)
        except Exception:  # noqa: BLE001 — a tick is best-effort
            pass
        return symbol, None

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

            from app.services import market_data as _md

            crypto = [s for s in symbols
                      if _md.classify(_md.normalize_symbol(s)) == _md.CRYPTO]
            broker = [s for s in symbols if s not in set(crypto)]

            # Crypto needs no session, so it fans out. The rest run in sequence
            # on ONE session: their MT5 leg queries the database, and an
            # AsyncSession shared across concurrent coroutines is a race. The
            # broker list is short (the FX/metal/index watchlist) and the quote
            # layer caches, so serialising it costs nothing measurable.
            results = list(await asyncio.gather(
                *(_fetch_one_price(s) for s in crypto), return_exceptions=True
            ))
            if broker:
                async with AsyncSessionLocal() as tick_db:
                    for sym in broker:
                        try:
                            results.append(await _fetch_one_price(sym, tick_db))
                        except Exception as exc:  # noqa: BLE001
                            results.append(exc)
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


# ── JARVIS Learning Loop ───────────────────────────────────
# Settles the trade proposals JARVIS published against what price actually did,
# so its stated confidence is measured rather than asserted. Without this the
# assistant makes calls forever and never finds out whether any of them worked.

_jarvis_learning_task: asyncio.Task | None = None
_jarvis_learning_running = False
_jarvis_learning_started_at: str | None = None
_jarvis_learning_last_run: dict | None = None


async def _jarvis_learning_loop():
    """Settle unresolved JARVIS proposals on a timer."""
    global _jarvis_learning_running, _jarvis_learning_last_run

    interval = getattr(settings, "JARVIS_LEARNING_INTERVAL_SECONDS", 900)
    expiry_hours = getattr(settings, "JARVIS_JOURNAL_EXPIRY_HOURS", 72)
    max_per_cycle = getattr(settings, "JARVIS_LEARNING_MAX_SETTLE_PER_CYCLE", 40)
    logger.info(f"🎓 [JARVIS LEARNING] Started (every {interval}s)")

    while _jarvis_learning_running:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break
        if not _jarvis_learning_running:
            break

        try:
            from app.core.database import AsyncSessionLocal
            from app.services import analysis_journal, market_data

            settled = 0
            async with AsyncSessionLocal() as db:
                pending = await analysis_journal.unsettled(db, limit=max_per_cycle)
                if not pending:
                    continue

                # One candle fetch per (symbol, timeframe), shared across every
                # row that needs it. Fetching per row would mean 40 upstream
                # calls a cycle and a swift rate-limit ban.
                groups: dict[tuple[str, str], list] = {}
                for row in pending:
                    groups.setdefault((row.symbol, row.timeframe or "4h"), []).append(row)

                for (symbol, timeframe), rows in groups.items():
                    try:
                        ohlcv, _ticker = await market_data.fetch_ohlcv_universal(
                            symbol, timeframe=timeframe, limit=1000
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(f"🎓 [JARVIS LEARNING] {symbol} candles: {exc}")
                        continue
                    if not ohlcv:
                        continue
                    for row in rows:
                        verdict = analysis_journal.evaluate(
                            row, ohlcv, expiry_hours=expiry_hours
                        )
                        if verdict is not None:
                            await analysis_journal.settle(db, row, verdict)
                            settled += 1

                stats = await analysis_journal.learned_stats(db)

            _jarvis_learning_last_run = {
                "at": now_sast().isoformat(),
                "status": "ok",
                "settled_this_cycle": settled,
                "pending": len(pending),
                "win_rate": stats.get("win_rate"),
                "total_settled": stats.get("settled"),
            }
            if settled:
                logger.info(
                    f"🎓 [JARVIS LEARNING] Settled {settled} — lifetime "
                    f"{stats.get('settled')} calls, {stats.get('win_rate', 0):.0%} win rate"
                )
                # One summary per cycle, not one per outcome — otherwise the
                # knowledge vault fills with noise nobody can read.
                try:
                    from app.api.jarvis import jarvis_learn_all_brains

                    await jarvis_learn_all_brains(
                        kind="learning",
                        title="JARVIS proposal outcomes",
                        content=(
                            f"Settled {settled} proposals. Lifetime: "
                            f"{stats.get('wins')}W/{stats.get('losses')}L "
                            f"({stats.get('win_rate', 0):.0%}), avg "
                            f"{stats.get('avg_r', 0):+.2f}R, "
                            f"{stats.get('no_fill', 0)} never filled."
                        ),
                    )
                except Exception:  # noqa: BLE001 — fan-out is optional
                    pass
        except asyncio.CancelledError:
            break
        except Exception as e:
            err_safe = str(e).replace("{", "{{").replace("}", "}}")
            logger.error(f"🎓 [JARVIS LEARNING] Cycle error: {err_safe}")
            _jarvis_learning_last_run = {
                "at": now_sast().isoformat(), "status": "error", "error": str(e),
            }

    _jarvis_learning_running = False
    logger.info("🎓 [JARVIS LEARNING] Stopped")


def start_jarvis_learning_loop():
    """Start the JARVIS proposal-settlement loop (idempotent)."""
    global _jarvis_learning_task, _jarvis_learning_running, _jarvis_learning_started_at

    if _jarvis_learning_task is not None and not _jarvis_learning_task.done():
        logger.warning("JARVIS learning loop already running")
        return False

    _jarvis_learning_running = True
    _jarvis_learning_started_at = now_sast().isoformat()
    _jarvis_learning_task = asyncio.create_task(_jarvis_learning_loop())
    logger.info("🎓 JARVIS learning loop started")
    return True


def stop_jarvis_learning_loop():
    """Stop the JARVIS proposal-settlement loop."""
    global _jarvis_learning_running, _jarvis_learning_task, _jarvis_learning_started_at

    if not _jarvis_learning_running and (
        _jarvis_learning_task is None or _jarvis_learning_task.done()
    ):
        return False

    _jarvis_learning_running = False
    if _jarvis_learning_task:
        _jarvis_learning_task.cancel()
        _jarvis_learning_task = None
    _jarvis_learning_started_at = None
    logger.info("🎓 JARVIS learning loop stopped")
    return True


def get_jarvis_learning_status() -> dict:
    """Return the current state of the JARVIS learning loop."""
    return {
        "running": _jarvis_learning_running,
        "started_at": _jarvis_learning_started_at,
        "last_run": _jarvis_learning_last_run,
    }


# ── SMC Background Research Loop ───────────────────────────
# Pulls the economic calendar, news feeds and sentiment on a timer and writes
# the findings into the three memories. Uses IDLE AI providers only — never the
# provider currently serving /mt5-live analysis — so live latency is untouched.
# The loop itself lives in the MT5 plugin; these are the standard start/stop/
# status controls so it is managed exactly like every other worker here.

def start_research_loop(interval: int = 900):
    """Start the SMC background research loop (default: every 15 min)."""
    try:
        from plugins.MT5TradingPlugin.backend.services.research_loop import (
            start_research_loop as _start,
        )
    except Exception as e:  # noqa: BLE001 — plugin may be absent in a trimmed deploy
        logger.warning(f"Research loop unavailable: {e}")
        return False
    return _start(interval)


def stop_research_loop():
    """Stop the SMC background research loop."""
    try:
        from plugins.MT5TradingPlugin.backend.services.research_loop import (
            stop_research_loop as _stop,
        )
    except Exception:  # noqa: BLE001
        return False
    return _stop()


def get_research_loop_status() -> dict:
    """Return the current state of the SMC background research loop."""
    try:
        from plugins.MT5TradingPlugin.backend.services.research_loop import (
            get_research_loop_status as _status,
        )
    except Exception:  # noqa: BLE001
        return {"running": False, "available": False}
    return _status()


# ── Signal Research Queue ──────────────────────────────────
# Researches the app's OWN signals (Telegram, sniper, SMC, core) a bounded
# number at a time, producing per-pair predictions with visible progress. Runs
# alongside the ambient research loop above, not instead of it.

def start_signal_research_queue(concurrency: int = 5, scan_interval: int = 180):
    """Start the per-signal research queue (default: 5 signals at a time)."""
    try:
        from plugins.MT5TradingPlugin.backend.services.signal_research import (
            start_signal_research_queue as _start,
        )
    except Exception as e:  # noqa: BLE001 — plugin may be absent in a trimmed deploy
        logger.warning(f"Signal research queue unavailable: {e}")
        return False
    return _start(concurrency, scan_interval)


def stop_signal_research_queue():
    """Stop the per-signal research queue."""
    try:
        from plugins.MT5TradingPlugin.backend.services.signal_research import (
            stop_signal_research_queue as _stop,
        )
    except Exception:  # noqa: BLE001
        return False
    return _stop()


# ── Obsidian Vault Auto-Sync ───────────────────────────────
# Exports signals, decisions and communities into the Obsidian vault on a timer,
# so the knowledge base reflects what the desk has actually been doing rather
# than whenever somebody last remembered to press Sync.

def start_vault_sync_loop(interval: int = 300):
    """Start the Obsidian vault auto-sync loop (default: every 5 min)."""
    try:
        from plugins.ObsidianKnowledgePlugin.backend.services.sync_orchestrator import (
            start_vault_sync_loop as _start,
        )
    except Exception as e:  # noqa: BLE001 — plugin may be absent in a trimmed deploy
        logger.warning(f"Vault sync loop unavailable: {e}")
        return False
    return _start(interval)


def stop_vault_sync_loop():
    """Stop the Obsidian vault auto-sync loop."""
    try:
        from plugins.ObsidianKnowledgePlugin.backend.services.sync_orchestrator import (
            stop_vault_sync_loop as _stop,
        )
    except Exception:  # noqa: BLE001
        return False
    return _stop()


def get_vault_sync_status() -> dict:
    """Current state of the Obsidian vault auto-sync loop."""
    try:
        from plugins.ObsidianKnowledgePlugin.backend.services.sync_orchestrator import (
            get_vault_sync_status as _status,
        )
    except Exception:  # noqa: BLE001
        return {"running": False, "available": False}
    return _status()


# ── Bitcoin cycle detector ────────────────────────────────────────────────────
# Watches the 1064-day calendar for a phase turn. The calendar itself is
# deterministic — the loop's job is to notice the day the phase flips and tell
# every surface at once: an SSE event for the room, a SYSTEM signal tagged
# kind=cycle_transition for the feed, and an alert for the desk.

_cycle_task: asyncio.Task | None = None
_cycle_running = False
#: The last phase the detector announced, so a transition fires exactly once.
_cycle_last_phase: str | None = None


async def run_cycle_detector() -> dict | None:
    """Resolve the snapshot once; announce a transition when the phase changed.

    Returns the transition payload, or None when there was nothing to say.
    """
    global _cycle_last_phase

    from app.core.events import Topics, event_bus
    from app.services import market_cycle

    snap = await market_cycle.resolve_cycle_snapshot()
    if snap is None or not snap.ok:
        return None

    payload = {
        "phase": snap.phase,
        "previous_phase": _cycle_last_phase,
        "anchor": snap.anchor,
        "day_of_cycle": snap.day_of_cycle,
        "projected_top": snap.projected_top,
        "projected_bottom": snap.projected_bottom,
        "days_to_top": snap.days_to_top,
        "days_to_bottom": snap.days_to_bottom,
        "late_phase": snap.late_phase,
        "at": now_sast().isoformat(),
    }

    if _cycle_last_phase is not None and _cycle_last_phase != snap.phase:
        payload["transition"] = f"{_cycle_last_phase}->{snap.phase}"
        try:
            await event_bus.publish(Topics.CYCLE_TRANSITION, payload)
        except Exception as exc:  # noqa: BLE001 — telemetry must not break the tick
            logger.warning(f"[cycle] SSE publish failed: {exc}")

        await _emit_cycle_signal(payload)
        await _notify_cycle_transition(payload)

    _cycle_last_phase = snap.phase
    return payload


async def _emit_cycle_signal(payload: dict) -> None:
    """Store the turn as a SYSTEM signal so the feed and journal record it."""
    try:
        from app.models.database import SignalSource
        from app.models.schemas import SignalCreate
        from app.signals.service import SignalService

        transition = payload.get("transition") or ""
        async with AsyncSessionLocal() as db:
            signal = await SignalService.create_signal(
                db,
                SignalCreate(
                    source=SignalSource.SYSTEM,
                    symbol="BTCUSD",
                    action="hold",
                    price=0.0,
                    timeframe="1d",
                    strength=0.6,
                    confidence=0.6,
                    raw_data=json.dumps({"kind": "cycle_transition", **payload}),
                    indicators=json.dumps({
                        "cycle_phase": payload.get("phase"),
                        "day_of_cycle": payload.get("day_of_cycle"),
                        "projected_top": payload.get("projected_top"),
                        "projected_bottom": payload.get("projected_bottom"),
                    }),
                ),
            )
        logger.warning(f"🔄 [CYCLE] transition {transition} recorded as signal id={getattr(signal, 'id', '?')}")
    except Exception as exc:  # noqa: BLE001 — a failed record must not fail the tick
        logger.warning(f"[cycle] signal record failed: {exc}")


async def _notify_cycle_transition(payload: dict) -> None:
    """Tell the desk the season changed."""
    try:
        phase = str(payload.get("phase") or "").upper()
        await AlertService.notify(
            title=f"Bitcoin cycle: {phase} phase",
            message=(
                f"Cycle turned {payload.get('transition')} — day {payload.get('day_of_cycle')} "
                f"since the {payload.get('anchor')} bottom. Projected top "
                f"{payload.get('projected_top')} ({payload.get('days_to_top')}d), "
                f"projected bottom {payload.get('projected_bottom')} "
                f"({payload.get('days_to_bottom')}d)."
            ),
            level="WARNING",
            details={"kind": "cycle_transition", "phase": phase},
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[cycle] alert skipped: {exc}")


async def _cycle_loop(interval: int):
    global _cycle_running
    while _cycle_running:
        try:
            await run_cycle_detector()
            record_scheduler_cycle("cycle_detect", "ok")
        except Exception as exc:  # noqa: BLE001 — the loop outlives any single failure
            logger.error(f"Cycle detector error: {exc}")
            record_scheduler_cycle("cycle_detect", "error")
        await asyncio.sleep(max(300, interval))


def start_cycle_detector_loop(interval: int | None = None) -> bool:
    """Start watching the Bitcoin calendar. Idempotent."""
    global _cycle_task, _cycle_running
    if _cycle_task is not None and not _cycle_task.done():
        return True
    interval = interval or getattr(settings, "CYCLE_RECHECK_INTERVAL_SECONDS", 3600)
    _cycle_running = True
    _cycle_task = asyncio.create_task(_cycle_loop(int(interval)))
    logger.info(f"📅 Cycle detector started — checking every {interval}s")
    return True


def stop_cycle_detector_loop() -> bool:
    """Stop the cycle detector."""
    global _cycle_task, _cycle_running
    _cycle_running = False
    if _cycle_task:
        _cycle_task.cancel()
        _cycle_task = None
    return True


def get_cycle_detector_status() -> dict:
    """Detector state for the system monitor."""
    return {
        "running": _cycle_running,
        "interval_seconds": getattr(settings, "CYCLE_RECHECK_INTERVAL_SECONDS", 3600),
        "last_phase": _cycle_last_phase,
    }


# ── Whale watch loop ─────────────────────────────────────────────────────────
# Reads the curated whale registry every minute; when a transfer above the
# move threshold is new since the last tick, it lands on the wire as
# whale.move so the room and the page light up in near real time.

_whale_task: asyncio.Task | None = None
_whale_running = False
_whale_seen_txids: set[str] = set()


async def run_whale_watch() -> dict | None:
    """Resolve the whale snapshot once; announce new threshold transfers."""
    from app.core.events import Topics, event_bus
    from app.services import whale_watch

    snap = await whale_watch.resolve_whale_snapshot()
    if snap is None:
        return None

    announced: list[dict] = []
    for move in snap.moves:
        txid = str(move.get("txid") or "")
        if not txid or txid in _whale_seen_txids:
            continue
        _whale_seen_txids.add(txid)
        if len(_whale_seen_txids) > 2000:
            # Keep the newest memory bounded — drop half rather than all so a
            # burst of transfers can't re-announce everything at once.
            _whale_seen_txids.clear()
            _whale_seen_txids.update(str(m.get("txid")) for m in snap.moves[:100])
        announced.append(move)
        try:
            await event_bus.publish(Topics.WHALE_MOVE, {
                **move,
                "score": snap.score,
                "at": now_sast().isoformat(),
            })
        except Exception as exc:  # noqa: BLE001 — telemetry must not break the tick
            logger.warning(f"[whale] SSE publish failed: {exc}")

    return {"score": snap.score, "net_flow_7d_btc": snap.net_flow_7d_btc,
            "announced": len(announced)}


async def _whale_loop(interval: int):
    global _whale_running
    while _whale_running:
        try:
            await run_whale_watch()
        except Exception as exc:  # noqa: BLE001 — the loop outlives any failure
            logger.error(f"Whale watch error: {exc}")
        await asyncio.sleep(max(30, interval))


def start_whale_watch_loop(interval: int | None = None) -> bool:
    """Start reading the whale registry. Idempotent. Default cadence: 60s."""
    global _whale_task, _whale_running
    if _whale_task is not None and not _whale_task.done():
        return True
    interval = interval or 60
    _whale_running = True
    _whale_task = asyncio.create_task(_whale_loop(int(interval)))
    logger.info(f"🐋 Whale watch started — checking every {interval}s")
    return True


def stop_whale_watch_loop() -> bool:
    global _whale_task, _whale_running
    _whale_running = False
    if _whale_task:
        _whale_task.cancel()
        _whale_task = None
    return True


def get_whale_watch_status() -> dict:
    from app.services.whale_watch import BALANCE_TTL_S

    return {
        "running": _whale_running,
        "interval_seconds": 60,
        "cache_ttl_s": BALANCE_TTL_S,
        "tracked_transfers": len(_whale_seen_txids),
    }
