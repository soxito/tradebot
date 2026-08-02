"""Runtime orchestration for background workers."""
from __future__ import annotations

from loguru import logger

from app.core.config import settings
from app.core.scheduler import (
    start_auto_trade_loop,
    start_live_auto_trade_loop,
    start_pair_catalog_sync_loop,
    start_position_monitor,
    start_pump_monitor_loop,
    start_jarvis_learning_loop,
    start_research_loop,
    start_scheduler,
    start_signal_research_queue,
    start_vault_sync_loop,
    start_sniper_loop,
    stop_auto_trade_loop,
    stop_live_auto_trade_loop,
    stop_pair_catalog_sync_loop,
    stop_position_monitor,
    stop_pump_monitor_loop,
    stop_jarvis_learning_loop,
    stop_research_loop,
    stop_scheduler,
    stop_signal_research_queue,
    stop_vault_sync_loop,
    stop_sniper_loop,
)


def start_background_workers(allow_in_api: bool = False) -> dict[str, bool]:
    """Start configured background workers.

    When `allow_in_api` is False, startup obeys `START_WORKERS_IN_API` and returns
    without side effects in API-only mode.
    """
    if not allow_in_api and not settings.START_WORKERS_IN_API:
        logger.info(
            "Background worker autostart is disabled in API mode "
            "(START_WORKERS_IN_API=False)."
        )
        return {"enabled": False}

    started: dict[str, bool] = {"enabled": True}

    if settings.AUTO_START_SCHEDULER:
        start_scheduler()
        started["scheduler"] = True
    else:
        started["scheduler"] = False

    if settings.AUTO_START_SIM_AUTO_TRADE_LOOP:
        started["simulation_auto_trade_loop"] = start_auto_trade_loop(
            settings.SIM_AUTO_TRADE_LOOP_INTERVAL_SECONDS
        )
    else:
        started["simulation_auto_trade_loop"] = False

    if settings.AUTO_START_LIVE_AUTO_TRADE_LOOP:
        if not settings.ENABLE_AUTO_TRADING:
            logger.warning(
                "AUTO_START_LIVE_AUTO_TRADE_LOOP is enabled while "
                "ENABLE_AUTO_TRADING is disabled."
            )
        started["live_auto_trade_loop"] = start_live_auto_trade_loop(
            settings.LIVE_AUTO_TRADE_LOOP_INTERVAL_SECONDS
        )
    else:
        started["live_auto_trade_loop"] = False

    if settings.AUTO_START_POSITION_MONITOR_LOOP:
        started["position_monitor_loop"] = start_position_monitor(
            settings.POSITION_MONITOR_INTERVAL_SECONDS
        )
    else:
        started["position_monitor_loop"] = False

    if settings.AUTO_START_SNIPER_LOOP:
        started["sniper_loop"] = start_sniper_loop(
            settings.SNIPER_LOOP_INTERVAL_SECONDS
        )
    else:
        started["sniper_loop"] = False

    if settings.AUTO_START_PUMP_MONITOR_LOOP:
        started["pump_monitor_loop"] = start_pump_monitor_loop(
            settings.PUMP_MONITOR_LOOP_INTERVAL_SECONDS
        )
    else:
        started["pump_monitor_loop"] = False

    if settings.AUTO_START_PAIR_CATALOG_SYNC_LOOP:
        started["pair_catalog_sync_loop"] = start_pair_catalog_sync_loop()
    else:
        started["pair_catalog_sync_loop"] = False

    if settings.AUTO_START_RESEARCH_LOOP:
        started["research_loop"] = start_research_loop(
            settings.RESEARCH_LOOP_INTERVAL_SECONDS
        )
    else:
        started["research_loop"] = False

    if settings.AUTO_START_SIGNAL_RESEARCH_QUEUE:
        started["signal_research_queue"] = start_signal_research_queue(
            settings.SIGNAL_RESEARCH_CONCURRENCY,
            settings.SIGNAL_RESEARCH_SCAN_SECONDS,
        )
    else:
        started["signal_research_queue"] = False

    if settings.AUTO_START_VAULT_SYNC_LOOP:
        started["vault_sync_loop"] = start_vault_sync_loop(
            settings.VAULT_SYNC_INTERVAL_SECONDS
        )
    else:
        started["vault_sync_loop"] = False

    if settings.AUTO_START_JARVIS_LEARNING_LOOP:
        started["jarvis_learning_loop"] = start_jarvis_learning_loop()
    else:
        started["jarvis_learning_loop"] = False

    return started


def stop_background_workers() -> None:
    """Stop all managed background workers."""
    stop_scheduler()
    stop_auto_trade_loop()
    stop_live_auto_trade_loop()
    stop_position_monitor()
    stop_sniper_loop()
    stop_pump_monitor_loop()
    stop_pair_catalog_sync_loop()
    stop_research_loop()
    stop_signal_research_queue()
    stop_vault_sync_loop()
    stop_jarvis_learning_loop()
