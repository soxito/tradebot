"""Tests for app.core.task_supervisor."""
from __future__ import annotations

import asyncio

import pytest

from app.core import task_supervisor as ts
from app.core.task_supervisor import TaskSpec, TaskSupervisor


def _spec(task_id="demo", critical=False, interval=60.0, **kw) -> TaskSpec:
    return TaskSpec(id=task_id, name=task_id, default_interval_s=interval,
                    critical=critical, **kw)


@pytest.fixture
def sup(tmp_path, monkeypatch):
    # Isolate the state file so tests don't touch the real data/task_state.json.
    monkeypatch.setattr(ts, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(ts, "_STATE_FILE", tmp_path / "task_state.json")
    return TaskSupervisor()


def test_pause_resume_round_trip(sup):
    sup.register(_spec("demo"))
    assert sup.task_info("demo")["paused"] is False
    res = sup.pause("demo")
    assert res["ok"] and sup.task_info("demo")["paused"] is True
    assert sup.task_info("demo")["paused_by"] == "user"
    sup.resume("demo")
    assert sup.task_info("demo")["paused"] is False
    assert sup.task_info("demo")["paused_by"] is None


def test_critical_refuses_pause_without_force(sup):
    sup.register(_spec("crit", critical=True))
    res = sup.pause("crit")
    assert res["ok"] is False
    assert sup.task_info("crit")["paused"] is False
    forced = sup.pause("crit", force=True)
    assert forced["ok"] is True
    assert sup.task_info("crit")["paused"] is True


def test_interval_override_and_clamp(sup):
    sup.register(_spec("demo", interval=100.0))
    sup.set_interval("demo", 250.0)
    assert sup.task_info("demo")["interval_seconds"] == 250.0
    # clamp: absurd override is clamped to the max
    sup.set_interval("demo", 10 ** 9)
    assert sup.task_info("demo")["interval_seconds"] == ts._MAX_INTERVAL_S
    # clearing the override restores tier-scaled base
    sup.set_interval("demo", None)
    assert sup.task_info("demo")["interval_override"] is None


def test_unknown_task(sup):
    assert sup.task_info("nope") is None
    assert sup.pause("nope")["ok"] is False
    assert sup.resume("nope")["ok"] is False


@pytest.mark.asyncio
async def test_gate_blocks_when_paused_and_releases_on_resume(sup):
    sup.register(_spec("demo"))
    sup.pause("demo")
    gate = asyncio.create_task(sup.gate("demo"))
    await asyncio.sleep(0.05)
    assert not gate.done()  # paused → gate blocks
    sup.resume("demo")
    await asyncio.wait_for(gate, timeout=1.0)  # resume releases


@pytest.mark.asyncio
async def test_cycle_records_metrics(sup):
    sup.register(_spec("demo"))
    async with sup.cycle("demo"):
        await asyncio.sleep(0.01)
    info = sup.task_info("demo")
    assert info["cycle_count"] == 1
    assert info["recent_cycles"] and info["recent_cycles"][-1]["wall_ms"] >= 0


def test_state_file_round_trip(sup, tmp_path, monkeypatch):
    sup.register(_spec("demo"))
    sup.pause("demo")
    sup.set_interval("demo", 123.0)
    assert (tmp_path / "task_state.json").exists()

    # A fresh supervisor sharing the same file must reload the paused delta.
    fresh = TaskSupervisor()
    fresh.register(_spec("demo"))
    info = fresh.task_info("demo")
    assert info["paused"] is True
    assert info["interval_override"] == 123.0


def test_claim_dedupes_non_critical_but_not_critical(sup):
    sup.register(_spec("demo"))
    sup.register(_spec("crit", critical=True))
    assert sup.claim("demo") is True
    assert sup.claim("demo") is False  # already claimed
    assert sup.claim("crit") is True
    assert sup.claim("crit") is True   # critical always claims


def test_registry_completeness(sup):
    """Every registered core task id must resolve to a tier policy entry."""
    from app.core import resource_tier as rt
    from app.core import scheduler  # noqa: F401 — ensure importable
    from app.core.task_registry import register_core_tasks

    # Use the module singleton for this check (that's what registration targets).
    ts.supervisor._specs.clear()
    ts.supervisor._state.clear()
    register_core_tasks()
    for tid in ts.supervisor._specs:
        assert tid in rt.TASK_TIER_POLICY, f"{tid} missing from TASK_TIER_POLICY"
