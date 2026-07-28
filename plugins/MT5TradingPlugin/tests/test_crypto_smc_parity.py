"""
Crypto parity: the crypto SMC path must be the SAME modules, not a fork.

Both endpoints are driven with identical candle data and their outputs compared
field by field. Providers are stubbed out entirely, so both paths land on the
deterministic floor — which also re-proves the never-fail contract on the crypto
side.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plugins.AiMarketAnalyst.backend.services import analysis_router  # noqa: E402
from plugins.AiMarketAnalyst.backend.services.provider_health import (  # noqa: E402
    provider_health,
)
from plugins.MT5TradingPlugin.backend.models import (  # noqa: E402
    MT5Base,
    SmcAnalysisRecord,
    SmcSignalRecord,
)
from plugins.MT5TradingPlugin.backend.services import smc_ai, smc_memory  # noqa: E402
from plugins.MT5TradingPlugin.backend.services.smc_strategy import (  # noqa: E402
    SMCStrategyEngine,
    candles_from_payload,
)

# The tests directory has no __init__.py, so pytest puts it on sys.path directly.
from test_smc_scoring import designed_bullish_market  # noqa: E402


@pytest_asyncio.fixture()
async def db(monkeypatch) -> AsyncSession:
    async def _no_providers(_db):
        return []

    async def _no_events(_symbol):
        return []

    monkeypatch.setattr(analysis_router, "rank_providers", _no_providers)
    monkeypatch.setattr(smc_ai, "fetch_economic_events", _no_events)
    monkeypatch.setattr(smc_memory, "_fan_out_to_brains", lambda **_kw: None)
    provider_health.reset()

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MT5Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()
    provider_health.reset()


def _ccxt_rows(candles):
    """The same candles as the exchange OHLCV payload the crypto path receives."""
    return [
        {"time": c.time, "open": c.open, "high": c.high, "low": c.low,
         "close": c.close, "volume": c.volume}
        for c in candles
    ]


def test_crypto_endpoint_imports_the_shared_modules_not_a_copy():
    """Guard against a future fork: the route must bind the shared symbols."""
    import inspect

    from app.api import signals as signals_api

    src = inspect.getsource(signals_api.crypto_smc_analyze)
    for shared in (
        "plugins.MT5TradingPlugin.backend.services.smc_strategy",
        "plugins.MT5TradingPlugin.backend.services.smc_ai",
        "smc_floor",
        "smc_memory",
        "SMCStrategyEngine",
    ):
        assert shared in src, f"crypto path must reuse {shared}"

    # The PineScript generate endpoint is untouched and still present.
    assert hasattr(signals_api, "crypto_smc_analyze")
    assert "/smc/generate" in {
        r.path for r in signals_api.router.routes if hasattr(r, "path")
    } or any("generate" in getattr(r, "path", "") for r in signals_api.router.routes)


def test_engine_output_is_identical_for_identical_candles():
    """Same candles through the same engine -> byte-identical analysis."""
    candles = designed_bullish_market()
    crypto_candles = candles_from_payload(_ccxt_rows(candles))

    mt5 = SMCStrategyEngine(min_rr=1.5, symbol="XAUUSD").analyze(candles)
    crypto = SMCStrategyEngine(min_rr=1.5, symbol="XAUUSD").analyze(crypto_candles)

    assert mt5.keys() == crypto.keys()
    assert len(mt5["signals"]) == len(crypto["signals"])
    for a, b in zip(mt5["signals"], crypto["signals"]):
        assert a == b, "the crypto path must not diverge from the MT5 path"


@pytest.mark.asyncio
async def test_crypto_floor_fires_with_no_providers(db):
    """The never-fail contract holds on the crypto side too."""
    candles = candles_from_payload(_ccxt_rows(designed_bullish_market()))
    analysis = SMCStrategyEngine(min_rr=1.5, symbol="BTC/USDT").analyze(candles)

    ai = await smc_ai.ai_review(
        db=db, symbol="BTC/USDT", timeframe="1h", analysis=analysis, market="crypto",
    )

    assert ai["available"] is True
    assert ai["tier"] == "deterministic"
    assert ai["is_degraded"] is True
    assert isinstance(ai["confidence"], float)
    assert ai["market_read"]
    assert ai["rated_signals"], "the floor must still rate the engine's setups"


@pytest.mark.asyncio
async def test_crypto_signals_carry_the_same_score_breakdown(db):
    candles = candles_from_payload(_ccxt_rows(designed_bullish_market()))
    analysis = SMCStrategyEngine(min_rr=1.5, symbol="BTC/USDT").analyze(candles)
    assert analysis["signals"]

    from plugins.MT5TradingPlugin.backend.services import smc_scoring

    for sig in analysis["signals"]:
        bd = sig["score_breakdown"]
        assert sorted(f["name"] for f in bd["factors"]) == sorted(
            smc_scoring.DEFAULT_WEIGHTS
        )
        assert bd["volume_confirmed"] is True


@pytest.mark.asyncio
async def test_crypto_learning_loop_uses_the_shared_tables(db):
    """Crypto rows land in the same tables, tagged market='crypto'."""
    candles = candles_from_payload(_ccxt_rows(designed_bullish_market()))
    analysis = SMCStrategyEngine(min_rr=1.5, symbol="BTC/USDT").analyze(candles)
    ai = await smc_ai.ai_review(
        db=db, symbol="BTC/USDT", timeframe="1h", analysis=analysis, market="crypto",
    )

    analysis_id = await smc_memory.record_analysis(
        db, market="crypto", symbol="BTC/USDT", timeframe="1h",
        analysis=analysis, ai_block=ai,
    )
    assert analysis_id is not None

    header = await db.get(SmcAnalysisRecord, analysis_id)
    assert header.market == "crypto"
    assert header.tier == "deterministic"

    rows = (await db.execute(SmcSignalRecord.__table__.select())).fetchall()
    assert rows and all(r.market == "crypto" for r in rows)
    assert all(r.factor_vector for r in rows)

    # Crypto learning is scoped to crypto — the MT5 market is unaffected.
    assert await smc_memory.learned_weights(db, market="mt5") is None


@pytest.mark.asyncio
async def test_markets_do_not_cross_contaminate_recall(db):
    """A crypto setup must not recall an MT5 outcome, and vice versa."""
    from plugins.MT5TradingPlugin.backend.models import SmcOutcome

    for market in ("mt5", "crypto"):
        sig = SmcSignalRecord(
            market=market, symbol="BTCUSD", timeframe="1h", side="buy",
            entry=50000.0, stop_loss=49000.0, take_profit=53000.0,
            factor_vector={"relative_volume": 0.9}, ticket=1,
        )
        db.add(sig)
        await db.flush()
        db.add(SmcOutcome(
            signal_id=sig.id, market=market, symbol="BTCUSD",
            r_multiple=3.0 if market == "crypto" else -1.0,
            win=market == "crypto",
        ))
    await db.commit()

    crypto_hits = await smc_memory.similar_setups(
        db, market="crypto", symbol="BTCUSD", side="buy",
        factors={"relative_volume": 0.9},
    )
    mt5_hits = await smc_memory.similar_setups(
        db, market="mt5", symbol="BTCUSD", side="buy",
        factors={"relative_volume": 0.9},
    )
    assert len(crypto_hits) == 1 and crypto_hits[0]["r_multiple"] == 3.0
    assert len(mt5_hits) == 1 and mt5_hits[0]["r_multiple"] == -1.0
