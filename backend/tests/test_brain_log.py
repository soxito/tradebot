"""Every decision goes to the AI brain — and a test run never writes to the real one."""
from __future__ import annotations

import pytest

from app.agents import brain_log


def test_a_decision_is_added_with_its_origin_and_mirrored(monkeypatch):
    added, mirrored = [], []

    class _DB:
        def add(self, row):
            added.append(row)

    monkeypatch.setattr(brain_log, "ALLOW_UNDER_TEST", True)
    monkeypatch.setattr(brain_log, "_ingest_sync", lambda *a, **k: mirrored.append(a))
    row = brain_log.log_decision(
        _DB(), agent_name="OBLIVION Gate", agent_role="oblivion_gate", symbol="XAUUSD",
        action="hold", reasoning="no confirmed signal", origin="oblivion_gate",
        market_data={"tradeable": False}, oblivion_read_id=12,
    )
    assert added == [row]
    assert row.origin == "oblivion_gate" and row.oblivion_read_id == 12
    assert row.market_data == '{"tradeable": false}'
    assert mirrored and mirrored[0][0] == "decision"


def test_the_production_brain_is_untouched_under_test(monkeypatch):
    mirrored = []
    monkeypatch.setattr(brain_log, "_ingest_sync", lambda *a, **k: mirrored.append(a))

    class _DB:
        def add(self, row):
            pass

    brain_log.log_decision(_DB(), agent_name="x", agent_role="x", symbol="X", action="hold")
    assert mirrored == []


@pytest.mark.asyncio
async def test_record_decision_is_a_no_op_under_test():
    assert await brain_log.record_decision(agent_name="x", agent_role="x", symbol="X", action="hold") is None


def test_a_failed_mirror_is_counted_not_raised(monkeypatch):
    def _boom(**_):
        raise RuntimeError("disk full")

    monkeypatch.setattr("app.neuro_brain.is_enabled", lambda: True)
    monkeypatch.setattr("app.neuro_brain.state_store.ingest_local", _boom)
    before = brain_log.health()["mirror_failed"]
    brain_log._ingest_sync("decision", "x", "X", "", "t", {})
    assert brain_log.health()["mirror_failed"] == before + 1


def test_every_backtest_trade_becomes_a_settled_brain_segment(monkeypatch):
    segs, outcomes, episodes = [], [], []
    monkeypatch.setattr("app.neuro_brain.is_enabled", lambda: True)
    monkeypatch.setattr("app.neuro_brain.state_store.record_trade_segment",
                        lambda **kw: segs.append(kw) or len(segs))
    monkeypatch.setattr("app.neuro_brain.state_store.mark_outcome", lambda sid, o, pnl=None: outcomes.append((sid, o, pnl)))
    monkeypatch.setattr("app.neuro_brain.state_store.ingest_local", lambda **kw: episodes.append(kw) or True)
    doc = {"id": 9, "params": {"timeframe": "2h", "profile_label": "room 2h profile"}, "results": {"XAUUSD": {
        "summary": {"oblivion": {"trades": 2, "wins": 1, "losses": 1, "win_rate": 0.5, "net_profit": 5.0,
                                 "max_drawdown_pct": 1.0, "avg_r": 0.1}},
        "trades": {"oblivion": [
            {"status": "closed", "action": "buy", "fill": 100, "initial_stop": 95, "tp3": 110, "outcome": "win", "pnl": 20, "r": 0.2},
            {"status": "closed", "action": "sell", "fill": 100, "initial_stop": 105, "tp3": 90, "outcome": "loss", "pnl": -15, "r": -1.0},
            {"status": "vetoed", "action": "buy"},
        ]}}}}
    counts = brain_log._backtest_sync(doc)
    assert counts == {"segments": 2, "episodes": 1}
    assert all(s["source"] == "backtest" for s in segs)
    assert [o[1] for o in outcomes] == ["win", "loss"]
