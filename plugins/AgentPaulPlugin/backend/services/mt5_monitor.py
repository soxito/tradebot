"""
Agent Paul — MT5 Live Position Monitor

Background asyncio task that polls MT5 open positions every 30 s,
detects meaningful changes, and stores dismissible alerts in-memory.

Alert types:
  new_trade      — a new ticket appeared
  trade_closed   — a ticket disappeared
  sl_approach    — price within 10 % of SL distance
  tp_approach    — price within 15 % of TP distance
  pnl_change     — profit moved by more than USD 10 in one poll

Callers:
  start_monitor()     — start background polling
  stop_monitor()      — stop background polling
  get_alerts()        — list all pending alerts
  mark_alert_read()   — dismiss one alert
  get_monitor_status() — status dict
"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from typing import Any, Optional

from loguru import logger

_POLL_INTERVAL = 30  # seconds between position polls
_MAX_ALERTS = 150    # rolling window of kept alerts

_alerts: deque[dict] = deque(maxlen=_MAX_ALERTS)
_last_positions: dict[str, dict] = {}   # ticket_str → snapshot
_monitor_task: Optional[asyncio.Task] = None
_running = False


# ── helpers ──────────────────────────────────────────────────────────────────


def _alert(kind: str, message: str, data: dict | None = None) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "type": kind,
        "message": message,
        "data": data or {},
        "ts": time.time(),
        "read": False,
    }


# ── poll ──────────────────────────────────────────────────────────────────────


async def _poll_once() -> None:
    try:
        from plugins.MT5TradingPlugin.backend.services.mt5_client import mt5_client  # type: ignore
        if mt5_client is None:
            return

        accounts = await mt5_client.get_accounts()
        if not accounts:
            return

        current: dict[str, dict] = {}

        for acct in accounts:
            acct_id = acct.get("id")
            try:
                positions = await mt5_client.get_positions(acct_id) or []
                for pos in positions:
                    ticket = str(pos.get("ticket", ""))
                    if not ticket:
                        continue
                    current[ticket] = {
                        "ticket": ticket,
                        "account": acct.get("name", "—"),
                        "account_id": acct_id,
                        "symbol": pos.get("symbol"),
                        "direction": "BUY" if pos.get("type") == 0 else "SELL",
                        "volume": pos.get("volume"),
                        "profit": pos.get("profit"),
                        "price_open": pos.get("price_open"),
                        "price_current": pos.get("price_current"),
                        "sl": pos.get("sl"),
                        "tp": pos.get("tp"),
                    }
            except Exception as exc:
                logger.debug(f"[MT5Monitor] Account {acct_id} error: {exc}")

        # ── new trades ─────────────────────────────────────────────────────
        for ticket, pos in current.items():
            if ticket not in _last_positions:
                _alerts.append(_alert(
                    "new_trade",
                    f"🟢 New {pos['direction']} trade: {pos['symbol']} "
                    f"vol={pos['volume']} entry={pos.get('price_open')}",
                    pos,
                ))

        # ── closed trades ──────────────────────────────────────────────────
        for ticket, pos in _last_positions.items():
            if ticket not in current:
                pnl = pos.get("profit")
                pnl_str = f"P&L {pnl:+.2f}" if pnl is not None else ""
                _alerts.append(_alert(
                    "trade_closed",
                    f"🔴 Trade closed: {pos['symbol']} {pos['direction']} {pnl_str}",
                    pos,
                ))

        # ── change detection ───────────────────────────────────────────────
        for ticket, pos in current.items():
            if ticket not in _last_positions:
                continue
            prev = _last_positions[ticket]
            price = pos.get("price_current") or 0.0
            open_price = pos.get("price_open") or price

            # SL approach
            sl = pos.get("sl") or 0.0
            if sl and open_price and price:
                sl_dist = abs(price - sl)
                trade_range = abs(price - open_price) or 0.0001
                if 0 < sl_dist < trade_range * 0.10:
                    _alerts.append(_alert(
                        "sl_approach",
                        f"⚠️ SL approach: {pos['symbol']} {pos['direction']} "
                        f"price={price:.5f} SL={sl:.5f}",
                        pos,
                    ))

            # TP approach
            tp = pos.get("tp") or 0.0
            if tp and open_price and price:
                tp_dist = abs(price - tp)
                tp_range = abs(tp - open_price) or 0.0001
                if 0 < tp_dist / tp_range < 0.15:
                    _alerts.append(_alert(
                        "tp_approach",
                        f"🎯 TP approach: {pos['symbol']} {pos['direction']} "
                        f"price={price:.5f} TP={tp:.5f}",
                        pos,
                    ))

            # Large P&L swing
            prev_pnl = prev.get("profit") or 0.0
            curr_pnl = pos.get("profit") or 0.0
            swing = curr_pnl - prev_pnl
            if abs(swing) >= 10.0:
                arrow = "↑" if swing > 0 else "↓"
                _alerts.append(_alert(
                    "pnl_change",
                    f"💰 P&L {arrow} {swing:+.2f}: {pos['symbol']} "
                    f"now {curr_pnl:+.2f}",
                    pos,
                ))

        _last_positions.clear()
        _last_positions.update(current)

    except ImportError:
        logger.debug("[MT5Monitor] MT5 plugin not available — monitoring disabled")
    except Exception as exc:
        logger.error(f"[MT5Monitor] Unexpected error: {exc}")


# ── loop ──────────────────────────────────────────────────────────────────────


async def _monitor_loop() -> None:
    global _running
    logger.info("[MT5Monitor] Position monitor started")
    while _running:
        await _poll_once()
        await asyncio.sleep(_POLL_INTERVAL)
    logger.info("[MT5Monitor] Position monitor stopped")


# ── public API ────────────────────────────────────────────────────────────────


def start_monitor() -> bool:
    global _monitor_task, _running
    if _running:
        return False
    _running = True
    try:
        loop = asyncio.get_event_loop()
        _monitor_task = loop.create_task(_monitor_loop())
        return True
    except RuntimeError:
        _running = False
        return False


def stop_monitor() -> bool:
    global _running
    if not _running:
        return False
    _running = False
    if _monitor_task and not _monitor_task.done():
        _monitor_task.cancel()
    return True


def get_alerts(unread_only: bool = False) -> list[dict]:
    alerts = list(_alerts)
    if unread_only:
        alerts = [a for a in alerts if not a["read"]]
    return sorted(alerts, key=lambda a: a["ts"], reverse=True)


def mark_alert_read(alert_id: str) -> bool:
    for a in _alerts:
        if a["id"] == alert_id:
            a["read"] = True
            return True
    return False


def clear_all_alerts() -> int:
    n = len(_alerts)
    _alerts.clear()
    return n


def get_monitor_status() -> dict[str, Any]:
    return {
        "running": _running,
        "tracked_positions": len(_last_positions),
        "pending_alerts": sum(1 for a in _alerts if not a["read"]),
        "total_alerts": len(_alerts),
        "poll_interval_seconds": _POLL_INTERVAL,
    }
