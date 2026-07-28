"""
Learning-loop tests: outcome arithmetic, similarity recall, weight recalibration.

Runs against a real in-memory SQLite database using the plugin's own MT5Base
metadata, so the additive tables are exercised as they will be created at
startup by ``create_all`` — no mocked session, no Postgres required.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plugins.MT5TradingPlugin.backend.models import (  # noqa: E402
    MT5Base,
    SmcFactorWeight,
    SmcOutcome,
    SmcSignalRecord,
)
from plugins.MT5TradingPlugin.backend.services import smc_memory, smc_scoring  # noqa: E402
from plugins.MT5TradingPlugin.backend.services.smc_strategy import Candle  # noqa: E402


@pytest_asyncio.fixture()
async def db(monkeypatch) -> AsyncSession:
    # The brain fan-out reaches into the core app; keep these tests hermetic.
    monkeypatch.setattr(smc_memory, "_fan_out_to_brains", lambda **_kw: None)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MT5Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


def _bars(rows, start_time: int = 1_000, step: int = 60) -> list[Candle]:
    return [
        Candle(time=start_time + i * step, open=o, high=h, low=l, close=c, volume=1.0)
        for i, (o, h, l, c) in enumerate(rows)
    ]


def _breakdown(**normalized: float) -> dict:
    """A minimal score_breakdown carrying the given normalized factor values."""
    return {
        "total": 0.7,
        "raw_total": 0.7,
        "volume_confirmed": True,
        "factors": [
            {"name": k, "family": smc_scoring.FACTOR_FAMILY.get(k, "other"),
             "raw_value": v, "normalized": v,
             "weight": smc_scoring.DEFAULT_WEIGHTS.get(k, 0.0),
             "contribution": v * smc_scoring.DEFAULT_WEIGHTS.get(k, 0.0)}
            for k, v in normalized.items()
        ],
    }


# ── Excursion arithmetic ─────────────────────────────────────────────────────

def test_winning_buy_reports_exact_r_multiple_and_excursions():
    # entry 100, stop 98 -> risk 2. TP 106 -> +3R.
    bars = _bars([
        (100, 101, 99.0, 100.5),   # MAE 1.0 (0.5R)
        (100, 104, 100.0, 103.0),
        (103, 107, 102.0, 106.5),  # touches TP 106
    ])
    out = smc_memory.compute_excursions(
        side="buy", entry=100.0, stop_loss=98.0, take_profit=106.0,
        candles=bars, fill_time=1_000,
    )
    assert out["exit_reason"] == "tp"
    assert out["win"] is True
    assert out["r_multiple"] == pytest.approx(3.0)
    assert out["mae"] == pytest.approx(1.0)
    assert out["mae_r"] == pytest.approx(0.5)
    assert out["mfe"] == pytest.approx(7.0)     # best high 107 - entry 100
    assert out["mfe_r"] == pytest.approx(3.5)
    assert out["time_to_target_s"] == 120       # resolved on the third bar


def test_losing_buy_reports_minus_one_r():
    bars = _bars([(100, 101, 99.5, 100.0), (100, 100.5, 97.0, 97.5)])
    out = smc_memory.compute_excursions(
        side="buy", entry=100.0, stop_loss=98.0, take_profit=106.0,
        candles=bars, fill_time=1_000,
    )
    assert out["exit_reason"] == "sl"
    assert out["win"] is False
    assert out["r_multiple"] == pytest.approx(-1.0)


def test_sell_side_excursions_are_mirrored():
    # entry 100, stop 102 -> risk 2. TP 94 -> +3R.
    bars = _bars([(100, 101, 98.0, 99.0), (99, 99.5, 93.5, 94.0)])
    out = smc_memory.compute_excursions(
        side="sell", entry=100.0, stop_loss=102.0, take_profit=94.0,
        candles=bars, fill_time=1_000,
    )
    assert out["exit_reason"] == "tp"
    assert out["r_multiple"] == pytest.approx(3.0)
    assert out["mae"] == pytest.approx(1.0)     # high 101 against a short
    assert out["mfe_r"] == pytest.approx(3.25)  # low 93.5 -> 6.5 / 2


def test_ambiguous_bar_resolves_pessimistically():
    """A bar touching both stop and target must be scored as the loss."""
    bars = _bars([(100, 107, 97.0, 101.0)])
    out = smc_memory.compute_excursions(
        side="buy", entry=100.0, stop_loss=98.0, take_profit=106.0,
        candles=bars, fill_time=1_000,
    )
    assert out["exit_reason"] == "sl"
    assert out["r_multiple"] == pytest.approx(-1.0)


def test_unresolved_trade_is_marked_expiry_not_a_win():
    bars = _bars([(100, 101, 99.5, 100.5), (100.5, 101.5, 100.0, 101.0)])
    out = smc_memory.compute_excursions(
        side="buy", entry=100.0, stop_loss=98.0, take_profit=106.0,
        candles=bars, fill_time=1_000,
    )
    assert out["exit_reason"] == "expiry"
    assert out["r_multiple"] == pytest.approx(0.5)


def test_zero_risk_setup_is_rejected():
    out = smc_memory.compute_excursions(
        side="buy", entry=100.0, stop_loss=100.0, take_profit=110.0,
        candles=_bars([(100, 111, 99, 110)]),
    )
    assert out["r_multiple"] == 0.0
    assert out["exit_reason"] == "open"


# ── Similarity ───────────────────────────────────────────────────────────────

def test_factor_vector_extracts_normalized_values():
    bd = _breakdown(relative_volume=0.8, structure_aligned=1.0)
    assert smc_memory.factor_vector(bd) == {
        "relative_volume": 0.8, "structure_aligned": 1.0
    }
    assert smc_memory.factor_vector(None) == {}


def test_similarity_ranks_identical_profiles_highest():
    target = {"relative_volume": 0.9, "structure_aligned": 1.0, "wick_rejection": 0.2}
    identical = dict(target)
    opposite = {"relative_volume": -0.9, "structure_aligned": -1.0, "wick_rejection": -0.2}
    unrelated = {"atr_momentum": 1.0}

    assert smc_memory.similarity(target, identical) == pytest.approx(1.0)
    assert smc_memory.similarity(target, opposite) == pytest.approx(-1.0)
    assert smc_memory.similarity(target, unrelated) == pytest.approx(0.0)
    assert smc_memory.similarity(target, {}) == 0.0


@pytest.mark.asyncio
async def test_similar_setups_returns_realised_results_ranked(db):
    now = datetime.utcnow()
    profiles = {
        "near": {"relative_volume": 0.85, "structure_aligned": 1.0},
        "far": {"relative_volume": -0.9, "structure_aligned": -1.0},
    }
    for name, vec in profiles.items():
        sig = SmcSignalRecord(
            market="mt5", symbol="XAUUSD", timeframe="H1", side="buy",
            zone_kind="bullish_fvg", entry=2000.0, stop_loss=1990.0,
            take_profit=2030.0, rr=3.0, confidence=0.7,
            factor_vector=vec, volume_confirmed=True, ticket=1,
            created_at=now - timedelta(hours=1),
        )
        db.add(sig)
        await db.flush()
        db.add(SmcOutcome(
            signal_id=sig.id, market="mt5", symbol="XAUUSD",
            r_multiple=2.5 if name == "near" else -1.0,
            win=name == "near", mfe_r=3.0, mae_r=0.4, exit_reason="tp",
        ))
    # An untraded signal must never be recalled — it teaches nothing.
    db.add(SmcSignalRecord(
        market="mt5", symbol="XAUUSD", timeframe="H1", side="buy",
        entry=2001.0, stop_loss=1991.0, take_profit=2031.0,
        factor_vector={"relative_volume": 0.85}, created_at=now,
    ))
    await db.commit()

    hits = await smc_memory.similar_setups(
        db, market="mt5", symbol="XAUUSD", side="buy",
        factors={"relative_volume": 0.9, "structure_aligned": 1.0}, limit=5,
    )
    assert len(hits) == 2, "only setups with a realised outcome are recalled"
    assert hits[0]["similarity"] > hits[1]["similarity"]
    assert hits[0]["r_multiple"] == 2.5 and hits[0]["win"] is True
    assert hits[1]["r_multiple"] == -1.0


@pytest.mark.asyncio
async def test_outcome_summary_aggregates_realised_performance(db):
    for r, win in [(2.0, True), (2.0, True), (-1.0, False), (-1.0, False)]:
        sig = SmcSignalRecord(
            market="mt5", symbol="EURUSD", timeframe="H1", side="buy",
            entry=1.1, stop_loss=1.09, take_profit=1.13, factor_vector={},
        )
        db.add(sig)
        await db.flush()
        db.add(SmcOutcome(signal_id=sig.id, market="mt5", symbol="EURUSD",
                          r_multiple=r, win=win, mfe_r=abs(r), mae_r=0.3))
    await db.commit()

    summary = await smc_memory.outcome_summary(db, market="mt5", symbol="EURUSD")
    assert summary["trades"] == 4
    assert summary["wins"] == 2
    assert summary["win_rate"] == pytest.approx(0.5)
    assert summary["avg_r"] == pytest.approx(0.5)
    # An instrument with no history returns an empty dict, not fabricated stats.
    assert await smc_memory.outcome_summary(db, market="mt5", symbol="GBPUSD") == {}


# ── Weight recalibration ─────────────────────────────────────────────────────

def test_a_factor_that_precedes_losses_loses_weight():
    stats = {
        "relative_volume": {"win_mean": 0.9, "loss_mean": 0.1},   # positive edge
        "wick_rejection": {"win_mean": 0.1, "loss_mean": 0.9},    # negative edge
    }
    tuned = smc_memory.recalibrated_weights(stats)

    d = smc_scoring.DEFAULT_WEIGHTS
    assert tuned["wick_rejection"] / d["wick_rejection"] < 1.0
    assert tuned["relative_volume"] / d["relative_volume"] > 1.0
    # The loser must fall relative to the winner, not just in absolute terms.
    assert (tuned["wick_rejection"] / d["wick_rejection"]
            < tuned["relative_volume"] / d["relative_volume"])


def test_recalibrated_weights_stay_normalised_and_bounded():
    extreme = {f: {"win_mean": -1.0, "loss_mean": 1.0}
               for f in smc_scoring.DEFAULT_WEIGHTS}
    tuned = smc_memory.recalibrated_weights(extreme)
    assert sum(tuned.values()) == pytest.approx(1.0, abs=1e-4)
    assert all(v > 0 for v in tuned.values()), "no factor is switched off entirely"
    assert set(tuned) == set(smc_scoring.DEFAULT_WEIGHTS)


def test_no_outcome_history_leaves_the_defaults_alone():
    assert smc_memory.recalibrated_weights({}) == pytest.approx(
        smc_scoring.DEFAULT_WEIGHTS, rel=1e-6
    )


@pytest.mark.asyncio
async def test_recalibration_needs_a_minimum_sample(db):
    sig = SmcSignalRecord(
        market="mt5", symbol="XAUUSD", timeframe="H1", side="buy",
        entry=2000.0, stop_loss=1990.0, take_profit=2030.0,
        factor_vector={"wick_rejection": 1.0},
    )
    db.add(sig)
    await db.flush()
    db.add(SmcOutcome(signal_id=sig.id, market="mt5", symbol="XAUUSD",
                      r_multiple=-1.0, win=False))
    await db.commit()

    weights = await smc_memory.recalibrate_weights(db, market="mt5")
    assert weights == smc_scoring.DEFAULT_WEIGHTS, "1 trade is not a sample"
    assert await smc_memory.learned_weights(db, market="mt5") is None


@pytest.mark.asyncio
async def test_recalibration_persists_learned_weights_from_real_outcomes(db):
    # 12 closed trades: wick_rejection scored high on every loser, low on every
    # winner. relative_volume does the opposite.
    for i in range(12):
        win = i % 2 == 0
        sig = SmcSignalRecord(
            market="mt5", symbol="XAUUSD", timeframe="H1", side="buy",
            entry=2000.0 + i, stop_loss=1990.0 + i, take_profit=2030.0 + i,
            factor_vector={
                "relative_volume": 1.0 if win else 0.0,
                "wick_rejection": 0.0 if win else 1.0,
                "structure_aligned": 0.5,
            },
        )
        db.add(sig)
        await db.flush()
        db.add(SmcOutcome(signal_id=sig.id, market="mt5", symbol="XAUUSD",
                          r_multiple=2.0 if win else -1.0, win=win))
    await db.commit()

    tuned = await smc_memory.recalibrate_weights(db, market="mt5")
    d = smc_scoring.DEFAULT_WEIGHTS
    assert tuned["wick_rejection"] < d["wick_rejection"]
    assert tuned["relative_volume"] > d["relative_volume"]
    # A factor with no realised edge is untouched apart from renormalisation.
    assert tuned["structure_aligned"] == pytest.approx(d["structure_aligned"], rel=0.05)

    # Persisted and readable back for the next scoring pass.
    stored = await smc_memory.learned_weights(db, market="mt5", symbol="XAUUSD")
    assert stored is not None
    assert stored["wick_rejection"] == pytest.approx(tuned["wick_rejection"])
    rows = (await db.execute(
        SmcFactorWeight.__table__.select()
    )).fetchall()
    assert len(rows) == len(smc_scoring.DEFAULT_WEIGHTS)
    assert all(r.sample_count == 12 for r in rows)


# ── End-to-end: analysis -> placement -> settlement -> recall ────────────────

@pytest.mark.asyncio
async def test_full_loop_records_analysis_settles_and_recalls_it(db):
    analysis = {
        "bias": "bullish", "htf_bias": "bullish", "last_price": 2010.0,
        "atr": 4.0, "rsi": 45.0, "volume_z": 1.2, "momentum": "expanding",
        "signals": [{
            "side": "buy", "entry": 2000.0, "stop_loss": 1990.0,
            "take_profit": 2030.0, "rr": 3.0, "confidence": 0.74,
            "zone_kind": "bullish_fvg", "confluence": ["fair_value_gap"],
            "score_breakdown": _breakdown(relative_volume=0.9, structure_aligned=1.0),
        }],
    }
    ai_block = {"provider_used": "NVIDIA", "tier": "primary", "is_degraded": False,
                "market_read": "discount retrace"}

    analysis_id = await smc_memory.record_analysis(
        db, market="mt5", symbol="XAUUSD", timeframe="H1",
        analysis=analysis, ai_block=ai_block,
    )
    assert analysis_id is not None

    stored = (await db.execute(SmcSignalRecord.__table__.select())).fetchall()
    assert len(stored) == 1
    assert stored[0].factor_vector == {"relative_volume": 0.9, "structure_aligned": 1.0}
    assert stored[0].volume_confirmed is True

    # The setup is placed…
    signal_id = await smc_memory.attach_ticket_to_signal(
        db, market="mt5", symbol="XAUUSD", side="buy", entry=2000.0, ticket=555,
    )
    assert signal_id is not None
    assert await smc_memory.unsettled_signals(db, market="mt5")

    # …price fills the limit and runs to target.
    signal = await db.get(SmcSignalRecord, signal_id)
    placed_at = int(signal.created_at.timestamp())
    bars = [
        Candle(time=placed_at + 60, open=2010, high=2012, low=1999, close=2005, volume=1),
        Candle(time=placed_at + 120, open=2005, high=2031, low=2004, close=2029, volume=1),
    ]
    assert await smc_memory.settle_signal(db, signal, bars) is not None
    assert await smc_memory.unsettled_signals(db, market="mt5") == []

    outcome = (await db.execute(SmcOutcome.__table__.select())).fetchall()[0]
    assert outcome.win is True
    assert outcome.r_multiple == pytest.approx(3.0)
    assert outcome.ticket == 555
    assert outcome.time_to_target_s == 60

    # A new, near-identical setup now recalls that realised result.
    hits = await smc_memory.similar_setups(
        db, market="mt5", symbol="XAUUSD", side="buy",
        factors={"relative_volume": 0.9, "structure_aligned": 1.0},
    )
    assert len(hits) == 1
    assert hits[0]["similarity"] == pytest.approx(1.0)
    assert hits[0]["r_multiple"] == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_unfilled_limit_is_never_settled(db):
    sig = SmcSignalRecord(
        market="mt5", symbol="XAUUSD", timeframe="H1", side="buy",
        entry=2000.0, stop_loss=1990.0, take_profit=2030.0,
        factor_vector={}, ticket=99, created_at=datetime.utcnow(),
    )
    db.add(sig)
    await db.commit()

    placed_at = int(sig.created_at.timestamp())
    # Price never trades down to the resting buy limit.
    bars = [Candle(time=placed_at + 60 * i, open=2050, high=2060, low=2040,
                   close=2055, volume=1) for i in range(1, 5)]
    assert await smc_memory.settle_signal(db, sig, bars) is None
    assert (await db.execute(SmcOutcome.__table__.select())).fetchall() == []


@pytest.mark.asyncio
async def test_hand_placed_order_matches_no_stored_signal(db):
    assert await smc_memory.attach_ticket_to_signal(
        db, market="mt5", symbol="XAUUSD", side="buy", entry=1234.5, ticket=7,
    ) is None
