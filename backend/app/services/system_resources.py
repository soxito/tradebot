"""
System resource snapshots — single source of truth for host / process metrics.

Extracted from ``app/api/jarvis.py`` (the JARVIS HUD ``/system-stats`` endpoint)
so the HUD and the System Monitor page cannot drift. Everything degrades
gracefully to ``available: False`` when psutil is missing.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from loguru import logger

try:  # psutil is optional — the whole module no-ops without it
    import psutil  # type: ignore
    _HAVE_PSUTIL = True
except Exception:  # pragma: no cover
    psutil = None  # type: ignore
    _HAVE_PSUTIL = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def host_snapshot() -> Dict[str, Any]:
    """CPU / memory / swap for the whole machine (non-blocking)."""
    if not _HAVE_PSUTIL:
        return {"available": False, "reason": "psutil not installed", "fetched_at": _now()}
    try:
        cpu_percent = psutil.cpu_percent(interval=None)
        cpu_count = psutil.cpu_count(logical=True) or 0
        try:
            per_core = psutil.cpu_percent(interval=None, percpu=True)
        except Exception:
            per_core = []

        vm = psutil.virtual_memory()
        try:
            sw = psutil.swap_memory()
            swap_percent = float(sw.percent)
            swap_used = int(sw.used)
            swap_total = int(sw.total)
        except Exception:
            swap_percent, swap_used, swap_total = 0.0, 0, 0

        load_pct: Optional[float] = None
        try:
            la1 = os.getloadavg()[0]
            if cpu_count:
                load_pct = round(min(100.0, (la1 / cpu_count) * 100.0), 1)
        except (OSError, AttributeError):
            load_pct = None

        return {
            "available": True,
            "cpu_percent": round(float(cpu_percent), 1),
            "cpu_count": cpu_count,
            "per_core": [round(float(c), 1) for c in per_core],
            "load_percent": load_pct,
            "mem_percent": round(float(vm.percent), 1),
            "mem_used": int(vm.used),
            "mem_total": int(vm.total),
            "mem_available": int(vm.available),
            "swap_percent": swap_percent,
            "swap_used": swap_used,
            "swap_total": swap_total,
            "fetched_at": _now(),
        }
    except Exception as e:  # pragma: no cover - best effort
        logger.debug(f"[system_resources] host_snapshot error: {e}")
        return {"available": False, "reason": str(e), "fetched_at": _now()}


def process_snapshot(pid: Optional[int] = None) -> Dict[str, Any]:
    """RSS / CPU for one process (defaults to this backend). Includes children."""
    if not _HAVE_PSUTIL:
        return {"available": False, "reason": "psutil not installed"}
    try:
        p = psutil.Process(pid) if pid is not None else psutil.Process()
        with p.oneshot():
            rss = int(p.memory_info().rss)
            cpu = float(p.cpu_percent(interval=None))
            name = p.name()
            create_time = p.create_time()
        child_rss = 0
        n_children = 0
        try:
            for c in p.children(recursive=True):
                try:
                    child_rss += int(c.memory_info().rss)
                    n_children += 1
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        return {
            "available": True,
            "pid": p.pid,
            "name": name,
            "cpu_percent": round(cpu, 1),
            "rss": rss,
            "rss_tree": rss + child_rss,
            "children": n_children,
            "started_at": datetime.fromtimestamp(create_time, tz=timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"available": False, "reason": str(e)}


def service_tree(pid: int) -> Dict[str, Any]:
    """Aggregate RSS/CPU across a process and its whole child tree.

    Next.js dev and OpenWA are multi-process; a single-PID number would be a lie.
    """
    if not _HAVE_PSUTIL:
        return {"available": False, "reason": "psutil not installed"}
    try:
        p = psutil.Process(pid)
    except Exception as e:
        return {"available": False, "reason": str(e), "pid": pid, "alive": False}

    procs = [p]
    try:
        procs += p.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    total_rss = 0
    total_cpu = 0.0
    members: List[Dict[str, Any]] = []
    for proc in procs:
        try:
            with proc.oneshot():
                rss = int(proc.memory_info().rss)
                cpu = float(proc.cpu_percent(interval=None))
                name = proc.name()
            total_rss += rss
            total_cpu += cpu
            members.append({"pid": proc.pid, "name": name, "rss": rss, "cpu_percent": round(cpu, 1)})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    return {
        "available": True,
        "pid": pid,
        "alive": True,
        "rss": total_rss,
        "cpu_percent": round(total_cpu, 1),
        "process_count": len(members),
        "members": members,
    }
