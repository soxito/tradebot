"""The sniper must explain an empty panel.

Reported as "signals are no longer being generated for gold on H1". They were:
the engine built two buy setups off bullish FVGs, and the HTF gate correctly
refused them because the H4 bias was bearish. Both hard gates were a bare
``return None``, so a run that found real structure and rejected it on risk
grounds looked identical to one that found nothing — and identical to a broken
sniper.

These lock the behaviour that tells those cases apart. They deliberately do NOT
assert that signals exist: whether a tradeable setup is present depends on the
market, and manufacturing one by weakening a gate would be the actual bug.
"""

from __future__ import annotations

import pytest

from plugins.MT5TradingPlugin.backend.schemas import MT5CandleResponse
from plugins.MT5TradingPlugin.backend.services.smc_strategy import SMCStrategyEngine


def _series(n: int = 200, *, start: float = 4000.0, drift: float = 0.0,
            step: int = 3600, vol: float = 100.0):
    """A simple synthetic series with a controllable drift."""
    out, price = [], start
    for i in range(n):
        price += drift
        wig = 4.0 if i % 3 else 9.0
        out.append(MT5CandleResponse(
            time=1_700_000_000 + i * step,
            open=price, high=price + wig, low=price - wig,
            close=price + (1.5 if i % 2 else -1.5), volume=vol,
        ))
    return out


def _engine(**kw):
    base = dict(min_rr=2.0, max_rr=10.0, sl_buffer_atr=1.0, min_confidence=0.6,
                symbol="XAUUSD", contract_size=100.0)
    base.update(kw)
    return SMCStrategyEngine(**base)


def test_analysis_always_reports_a_rejections_field():
    """The UI reads this unconditionally; it must never be missing."""
    a = _engine().analyze(_series())
    assert "rejected_signals" in a
    assert isinstance(a["rejected_signals"], list)


_GATES = {"htf_bias", "volume", "confidence", "location", "risk_reward"}


def test_rejections_carry_the_gate_and_a_reason():
    """A bare count would not tell the trader what to do differently."""
    a = _engine().analyze(_series(drift=-1.2))
    for r in a["rejected_signals"]:
        assert r["gate"] in _GATES
        assert r["detail"], "a rejection with no explanation helps nobody"
        assert r["side"] in ("buy", "sell")
        assert isinstance(r["entry"], float)
        assert 0.0 <= r["confidence"] <= 1.0


def test_a_sell_zone_below_price_is_reported_not_silently_dropped():
    """The real case that started this: a volume-confirmed bearish FVG sitting
    *below* price. It is correctly unusable — a sell limit must rest above the
    market — but it used to vanish with no signal and no explanation."""
    from plugins.MT5TradingPlugin.backend.services.smc_strategy import Zone

    candles = _series(200)
    engine = _engine()
    prim = engine._primitives(candles)
    prim["htf_bias"] = None
    engine._rejected = []

    last = candles[-1].close
    below = Zone(kind="bearish_fvg", top=last - 60.0, bottom=last - 70.0,
                 index=len(candles) - 30, time=candles[-30].time)

    assert engine._build_signal(below, "sell", last, prim["atr"], prim) is None
    gates = {r["gate"] for r in engine._rejected}
    assert "location" in gates, f"the drop was still silent (gates: {gates})"
    detail = next(r["detail"] for r in engine._rejected if r["gate"] == "location")
    assert "sell limit" in detail or "discount" in detail


def test_rr_below_the_floor_is_reported_with_the_floor():
    from plugins.MT5TradingPlugin.backend.services.smc_strategy import Zone

    candles = _series(200)
    engine = _engine(min_rr=9.5)
    prim = engine._primitives(candles)
    prim["htf_bias"] = None
    engine._rejected = []

    last = candles[-1].close
    # A correctly-located sell zone just above price: passes location, and with a
    # 9.5 floor its risk:reward cannot possibly qualify.
    above = Zone(kind="bearish_ob", top=last + 12.0, bottom=last + 6.0,
                 index=len(candles) - 30, time=candles[-30].time)
    engine._build_signal(above, "sell", last, prim["atr"], prim)

    rr_rejects = [r for r in engine._rejected if r["gate"] == "risk_reward"]
    for r in rr_rejects:
        assert "9.5" in r["detail"] and "RR" in r["detail"]


def test_htf_gate_records_the_setups_it_blocks():
    """The exact gold-on-H1 case: counter-trend buys into a bearish HTF."""
    rising = _series(drift=+2.0)          # bullish entry timeframe → buy zones
    falling = _series(drift=-4.0, step=14400)  # bearish higher timeframe

    engine = _engine(min_confidence=0.0)
    gated = engine.analyze(rising, htf_candles=falling)

    htf_rejects = [r for r in gated["rejected_signals"] if r["gate"] == "htf_bias"]
    if htf_rejects:
        assert all("opposes" in r["detail"] for r in htf_rejects)
        assert all(r["side"] == "buy" for r in htf_rejects), (
            "a bearish HTF should only block buys"
        )


def test_ledger_does_not_leak_between_runs():
    """Stale rejections would explain the previous chart, not this one."""
    engine = _engine(min_confidence=0.99)   # reject nearly everything
    first = engine.analyze(_series(drift=-1.0))
    second = engine.analyze(_series(drift=-1.0))
    assert len(first["rejected_signals"]) == len(second["rejected_signals"]), (
        "rejections accumulated across analyses"
    )


def test_rejections_are_capped_and_ranked():
    """Highest-conviction near-misses first — those are the informative ones."""
    a = _engine(min_confidence=0.99).analyze(_series(drift=-1.0))
    rejects = a["rejected_signals"]
    assert len(rejects) <= 6
    confs = [r["confidence"] for r in rejects]
    assert confs == sorted(confs, reverse=True)


def test_a_confidence_rejection_names_the_floor():
    """'low conviction' is only actionable if the threshold is stated."""
    a = _engine(min_confidence=0.99).analyze(_series(drift=-1.0))
    conf_rejects = [r for r in a["rejected_signals"] if r["gate"] == "confidence"]
    for r in conf_rejects:
        assert "0.99" in r["detail"]


def test_short_history_still_returns_the_field():
    """The early-exit path must not omit a key the UI reads."""
    a = _engine().analyze(_series(n=10))
    assert a.get("error")
    assert a.get("signals") == []


def test_backtest_does_not_crash_on_the_ledger():
    """backtest() reaches _build_signal without going through analyze()."""
    result = _engine().backtest(_series(300, drift=-0.8), expiry_bars=12)
    assert "stats" in result or "error" in result
