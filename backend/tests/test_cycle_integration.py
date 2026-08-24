"""Bitcoin cycle integration — transition announcements and auto risk reduction.

The calendar maths are covered in test_market_cycle; here it is the wiring:
the detector announcing a phase turn exactly once, the signal it leaves in the
feed, and the sizing hook shrinking risk inside the projected-bear window.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.core.scheduler import run_cycle_detector, _cycle_last_phase
from app.services import market_cycle as mc

import app.core.scheduler as scheduler


@pytest.fixture(autouse=True)
def _clean_detector_state(monkeypatch):
    monkeypatch.setattr(scheduler, "_cycle_last_phase", None)


@pytest.fixture
def anchored_snapshot(monkeypatch):
    """Resolve the snapshot from fixed anchors without touching Yahoo.

    ``today`` is pinned to a known bull day so the assertions hold whatever
    the real calendar says when the suite runs.
    """

    async def _fake_resolver(*args, **kwargs):
        return mc.build_cycle_snapshot(
            kwargs.get("anchors_override") or mc.DEFAULT_ANCHORS,
            today=date(2025, 8, 23),   # day 1006 — late bull
            bull_days=kwargs.get("bull_days") or mc.BULL_DAYS,
            bear_days=kwargs.get("bear_days") or mc.BEAR_DAYS,
        )

    monkeypatch.setattr(mc, "resolve_cycle_snapshot", _fake_resolver)
    return _fake_resolver


@pytest.mark.asyncio
async def test_first_tick_records_phase_without_announcing(anchored_snapshot, monkeypatch):
    """A fresh detector has no previous phase — it observes, it does not declare."""
    published = []

    async def _capture(topic, data):
        published.append((topic, data))

    from app.core.events import Topics, event_bus

    monkeypatch.setattr(event_bus, "publish", _capture)
    payload = await run_cycle_detector()
    assert payload is not None
    assert payload["phase"] == "bull"       # 2025-08-23 is pinned late bull
    assert published == [], "no transition on the first observation"
    assert scheduler._cycle_last_phase == "bull"


@pytest.mark.asyncio
async def test_phase_flip_announces_exactly_once(anchored_snapshot, monkeypatch):
    published = []

    async def _capture(topic, data):
        published.append((topic, data))

    from app.core.events import Topics, event_bus

    monkeypatch.setattr(event_bus, "publish", _capture)
    # Keep the announcement test hermetic: no signal rows, no alerts.
    async def _no_signal(payload):
        return None

    async def _no_alert(payload):
        return None

    monkeypatch.setattr(scheduler, "_emit_cycle_signal", _no_signal)
    monkeypatch.setattr(scheduler, "_notify_cycle_transition", _no_alert)

    # Tick 1: bull recorded. Tick 2 (simulated next phase): bear announced.
    scheduler._cycle_last_phase = "bull"
    real_build = mc.build_cycle_snapshot

    def _bear_snapshot(*args, **kwargs):
        snap = real_build(*args, **kwargs)
        # Force the bear side of the calendar: a day past the projected top.
        from app.services.market_cycle import CycleSnapshot

        return CycleSnapshot(
            phase="bear", anchor=snap.anchor, day_of_cycle=snap.day_of_cycle,
            phase_day=10, phase_days_total=mc.BEAR_DAYS, phase_pct=0.03,
            projected_top=snap.projected_top, projected_bottom=snap.projected_bottom,
            days_to_top=-10, days_to_bottom=mc.BEAR_DAYS - 10, late_phase=False,
            as_of=snap.as_of,
        )

    monkeypatch.setattr(mc, "build_cycle_snapshot", _bear_snapshot)
    payload = await run_cycle_detector()
    assert payload["transition"] == "bull->bear"
    transitions = [t for t, _ in published if t == Topics.CYCLE_TRANSITION]
    assert transitions == [Topics.CYCLE_TRANSITION]

    # Tick 3: same phase again — silence, not a repeat announcement.
    published.clear()
    payload = await run_cycle_detector()
    assert payload["phase"] == "bear"
    assert published == []


# ── Auto risk reduction ──────────────────────────────────────────────────────


class _FakeSettings:
    def __init__(self, *, auto=True, mult=0.5, risk=2.0):
        self.risk_pct = risk
        self.cycle_auto_risk = auto
        self.cycle_risk_multiplier = mult
        self.cycle_bull_days = mc.BULL_DAYS
        self.cycle_bear_days = mc.BEAR_DAYS


@pytest.mark.asyncio
async def test_bear_phase_shrinks_risk(monkeypatch):
    from app.agents.execution import effective_risk_pct

    async def _bear(*args, **kwargs):
        return mc.build_cycle_snapshot(mc.DEFAULT_ANCHORS, today=date(2026, 2, 1))

    monkeypatch.setattr(mc, "resolve_cycle_snapshot", _bear)
    s = _FakeSettings()
    assert await effective_risk_pct(s, "BTCUSD") == pytest.approx(1.0)  # 2.0 * 0.5


@pytest.mark.asyncio
async def test_late_bull_shrinks_risk_but_mid_bull_does_not(monkeypatch):
    from app.agents.execution import effective_risk_pct

    async def _late_bull(*args, **kwargs):
        return mc.build_cycle_snapshot(mc.DEFAULT_ANCHORS, today=date(2025, 8, 23))

    async def _mid_bull(*args, **kwargs):
        return mc.build_cycle_snapshot(mc.DEFAULT_ANCHORS, today=date(2024, 6, 1))

    s = _FakeSettings(risk=2.0)
    monkeypatch.setattr(mc, "resolve_cycle_snapshot", _late_bull)
    assert await effective_risk_pct(s, "BTCUSD") == pytest.approx(1.0)

    monkeypatch.setattr(mc, "resolve_cycle_snapshot", _mid_bull)
    assert await effective_risk_pct(s, "BTCUSD") == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_auto_risk_off_or_non_crypto_leaves_risk_alone(monkeypatch):
    from app.agents.execution import effective_risk_pct

    async def _bear(*args, **kwargs):
        return mc.build_cycle_snapshot(mc.DEFAULT_ANCHORS, today=date(2026, 2, 1))

    monkeypatch.setattr(mc, "resolve_cycle_snapshot", _bear)
    assert await effective_risk_pct(_FakeSettings(auto=False), "BTCUSD") == pytest.approx(2.0)
    assert await effective_risk_pct(_FakeSettings(), "XAUUSD") == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_calendar_outage_leaves_risk_untouched(monkeypatch):
    """A cycle that fails to resolve must never size trades."""
    from app.agents.execution import effective_risk_pct

    async def _broken(*args, **kwargs):
        raise RuntimeError("yahoo down")

    monkeypatch.setattr(mc, "resolve_cycle_snapshot", _broken)
    assert await effective_risk_pct(_FakeSettings(), "BTCUSD") == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_multiplier_cannot_raise_risk(monkeypatch):
    """The settings API caps the multiplier at 1.0; defence in depth here too."""
    from app.agents.execution import effective_risk_pct

    async def _bear(*args, **kwargs):
        return mc.build_cycle_snapshot(mc.DEFAULT_ANCHORS, today=date(2026, 2, 1))

    monkeypatch.setattr(mc, "resolve_cycle_snapshot", _bear)
    assert await effective_risk_pct(_FakeSettings(mult=0.5), "BTCUSD") == pytest.approx(1.0)
