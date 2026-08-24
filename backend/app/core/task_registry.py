"""
Task registration table — binds the supervisor to the core scheduler loops.

Registration is observational: it does not change what actually starts (the
lifespan still drives startup), it only makes every loop visible + controllable
through the supervisor and the System Monitor page. ``default_interval_s`` is the
**base** (un-scaled) cadence — the supervisor re-applies the tier multiplier, so
the reported interval matches the already-tier-scaled value the loops read from
settings.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from loguru import logger

from app.core import resource_tier as rt
from app.core.task_supervisor import TaskSpec, supervisor


def _spec(
    task_id: str,
    name: str,
    default_interval_s: float,
    *,
    source: str = "core",
    description: str = "",
    start: Optional[Callable] = None,
    stop: Optional[Callable] = None,
    status: Optional[Callable] = None,
) -> TaskSpec:
    return TaskSpec(
        id=task_id,
        name=name,
        category=rt.task_category(task_id),
        description=description,
        source=source,
        default_interval_s=default_interval_s,
        critical=rt.is_critical(task_id),
        min_tier=rt.task_min_tier(task_id),
        start=start,
        stop=stop,
        status=status,
    )


def register_core_tasks() -> None:
    """Register every core scheduler loop as a supervised adapter task."""
    try:
        from app.core import scheduler as s
    except Exception as e:  # noqa: BLE001 — never block startup
        logger.warning(f"[supervisor] core task registration skipped: {e}")
        return

    # Base (un-scaled) intervals, mirroring the config class defaults.
    table = [
        _spec("scheduler", "Signals + sentiment scheduler", 180,
              start=s.start_scheduler, stop=s.stop_scheduler, status=s.get_scheduler_status),
        _spec("sim_auto_trade", "Simulated auto-trade loop", 60,
              start=s.start_auto_trade_loop, stop=s.stop_auto_trade_loop,
              status=s.get_auto_trade_loop_status),
        _spec("live_auto_trade", "Live auto-trade loop", 60,
              start=s.start_live_auto_trade_loop, stop=s.stop_live_auto_trade_loop,
              status=s.get_live_auto_trade_loop_status),
        _spec("position_monitor", "Open-position monitor", 900,
              start=s.start_position_monitor, stop=s.stop_position_monitor,
              status=s.get_position_monitor_status),
        _spec("sniper", "Sniper entry loop", 60,
              start=s.start_sniper_loop, stop=s.stop_sniper_loop,
              status=s.get_sniper_loop_status),
        _spec("pump_monitor", "Pump monitor loop", 120,
              start=s.start_pump_monitor_loop, stop=s.stop_pump_monitor_loop,
              status=s.get_pump_monitor_status),
        _spec("pair_catalog_sync", "Crypto pair catalog sync", 900,
              start=s.start_pair_catalog_sync_loop, stop=s.stop_pair_catalog_sync_loop,
              status=s.get_pair_catalog_status),
        _spec("price_tick", "Realtime price-tick fan-out", 5,
              start=s.start_price_tick_loop, stop=s.stop_price_tick_loop,
              status=s.get_price_tick_status),
        _spec("jarvis_learning", "JARVIS learning loop", 900,
              start=s.start_jarvis_learning_loop, stop=s.stop_jarvis_learning_loop,
              status=s.get_jarvis_learning_status),
        _spec("research_loop", "SMC background research", 900,
              start=s.start_research_loop, stop=s.stop_research_loop,
              status=s.get_research_loop_status),
        _spec("vault_sync", "Obsidian vault sync", 300,
              start=s.start_vault_sync_loop, stop=s.stop_vault_sync_loop,
              status=s.get_vault_sync_status),
    ]

    # signal_research_queue has start/stop but no status getter.
    try:
        table.append(_spec(
            "signal_research_queue", "Per-signal research queue", 180,
            start=lambda: s.start_signal_research_queue(), stop=s.stop_signal_research_queue,
        ))
    except AttributeError:
        pass

    for spec in table:
        supervisor.register(spec)

    logger.info(f"[supervisor] registered {len(table)} core tasks")
