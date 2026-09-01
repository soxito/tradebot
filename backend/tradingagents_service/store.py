"""Thread-safe in-memory run store.

Each run accumulates an ordered list of events (state snapshots, agent
messages, terminal result) that the SSE endpoint replays and live-tails.
Runs are kept in memory only; the main backend persists durable history.
"""
from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

MAX_RUNS_KEPT = 100
MAX_EVENTS_PER_RUN = 5000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Run:
    """One TradingAgents analysis run and its event log."""

    def __init__(self, ticker: str, trade_date: str, config: dict[str, Any]):
        self.id = f"ta-{uuid.uuid4().hex[:12]}"
        self.ticker = ticker
        self.trade_date = trade_date
        self.config = config
        self.status = "running"  # running | done | error
        self.phase = "queued"
        self.created_at = _now_iso()
        self.finished_at: str | None = None
        self.error: str | None = None
        self.result: dict[str, Any] | None = None
        self.events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def append_event(self, type_: str, payload: Any) -> int:
        with self._lock:
            if len(self.events) >= MAX_EVENTS_PER_RUN:
                return len(self.events)
            seq = len(self.events)
            self.events.append({"seq": seq, "type": type_, "ts": _now_iso(), "data": payload})
            return seq

    def event_count(self) -> int:
        with self._lock:
            return len(self.events)

    def snapshot(self, include_events: bool = False) -> dict[str, Any]:
        with self._lock:
            data = {
                "id": self.id,
                "ticker": self.ticker,
                "trade_date": self.trade_date,
                "config": self.config,
                "status": self.status,
                "phase": self.phase,
                "created_at": self.created_at,
                "finished_at": self.finished_at,
                "error": self.error,
                "result": self.result,
                "event_count": len(self.events),
            }
            if include_events:
                data["events"] = [dict(e) for e in self.events]
            return data


class RunStore:
    """Bounded registry of runs, newest-first."""

    def __init__(self) -> None:
        self._runs: OrderedDict[str, Run] = OrderedDict()
        self._lock = threading.Lock()

    def create(self, ticker: str, trade_date: str, config: dict[str, Any]) -> Run:
        run = Run(ticker, trade_date, config)
        with self._lock:
            self._runs[run.id] = run
            while len(self._runs) > MAX_RUNS_KEPT:
                self._runs.popitem(last=False)
        return run

    def get(self, run_id: str) -> Run | None:
        with self._lock:
            return self._runs.get(run_id)

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            runs = list(reversed(self._runs.values()))[:limit]
        return [r.snapshot() for r in runs]

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for r in self._runs.values() if r.status == "running")


store = RunStore()
