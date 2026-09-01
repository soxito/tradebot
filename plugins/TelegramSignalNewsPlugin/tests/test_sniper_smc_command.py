"""/sniper MT5 SMC analysis command.

``/sniper`` with no arguments must keep returning the legacy rug-pull sniper
status; with a symbol it runs the same in-process SMC analysis the /mt5-live
page renders.
"""
from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace

import pytest
from sqlalchemy import Column, Integer
from sqlalchemy.orm import declarative_base

from plugins.TelegramSignalNewsPlugin.backend.services import command_service as cs


# ── Fakes ─────────────────────────────────────────────────────────────────────

_Base = declarative_base()


class _FakeMT5Account(_Base):
    """Real declarative class so ``select(MT5Account)`` builds a valid statement."""
    __tablename__ = "fake_mt5_accounts_for_sniper_test"
    id = Column(Integer, primary_key=True)


class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalars(self):
        return self

    def first(self):
        return self._obj

    def all(self):
        return [] if self._obj is None else [self._obj]


class _FakeDB:
    """Returns ``obj`` for every query — enough for both /sniper branches."""

    def __init__(self, obj=None):
        self.obj = obj

    async def execute(self, _stmt):
        return _FakeResult(self.obj)

    async def commit(self):
        pass


@pytest.fixture
def smc_calls(monkeypatch):
    """Stub out the MT5 models + router modules; record smc_analyze kwargs."""
    calls: list[dict] = []
    response = SimpleNamespace(
        symbol="XAUUSD", timeframe="H1", error=None, bias="bullish",
        momentum="expanding", last_price=2345.67, atr=4.2, atr_pct=0.18,
        rsi=58.1, volume_z=1.2, equilibrium=2340.0,
        range={"low": 2300.0, "high": 2380.0},
        structure_events=[{"type": "BOS", "direction": "bullish", "level": 2350.0}],
        liquidity={"buyside": [2381.0], "sellside": [2299.0]},
        zones=[SimpleNamespace(kind="bullish_ob"), SimpleNamespace(kind="bearish_fvg")],
        signals=[], kronos=None, ai=None,
    )

    async def _fake_smc_analyze(**kwargs):
        calls.append(kwargs)
        return response

    models_mod = types.ModuleType("plugins.MT5TradingPlugin.backend.models")
    models_mod.MT5Account = _FakeMT5Account
    router_mod = types.ModuleType("plugins.MT5TradingPlugin.backend.router")
    router_mod.smc_analyze = _fake_smc_analyze

    monkeypatch.setitem(sys.modules, "plugins.MT5TradingPlugin.backend.models", models_mod)
    monkeypatch.setitem(sys.modules, "plugins.MT5TradingPlugin.backend.router", router_mod)
    return SimpleNamespace(calls=calls, response=response)


# ── Argument parsing ──────────────────────────────────────────────────────────

def test_symbol_is_upper_cased():
    symbol, timeframe, err = cs._parse_sniper_args("xauusd")
    assert (symbol, timeframe, err) == ("XAUUSD", "H1", None)


def test_timeframe_defaults_to_h1():
    assert cs._parse_sniper_args("EURUSD")[1] == "H1"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1m", "M1"), ("m1", "M1"), ("5m", "M5"), ("15m", "M15"), ("M15", "M15"),
        ("30m", "M30"), ("1h", "H1"), ("H1", "H1"), ("4h", "H4"), ("h4", "H4"),
        ("1d", "D1"), ("d1", "D1"),
    ],
)
def test_timeframe_aliases_map_to_mt5_codes(raw, expected):
    symbol, timeframe, err = cs._parse_sniper_args(f"xauusd {raw}")
    assert err is None
    assert (symbol, timeframe) == ("XAUUSD", expected)


def test_unknown_timeframe_returns_usage_and_no_fallback():
    symbol, timeframe, err = cs._parse_sniper_args("XAUUSD 3h")
    assert symbol is None and timeframe is None
    assert "3h" in err
    for tf in ("1m", "5m", "15m", "30m", "1h", "4h", "1d"):
        assert tf in err


def test_extra_tokens_are_ignored():
    assert cs._parse_sniper_args("xauusd 4h junk")[:2] == ("XAUUSD", "H4")


# ── Routing: no args keeps the legacy status path ─────────────────────────────

def test_no_args_uses_legacy_status_path(monkeypatch):
    async def _boom(*_a, **_k):
        raise AssertionError("/sniper with no args must not run SMC analysis")

    monkeypatch.setattr(cs, "_sniper_smc", _boom)
    # A settings row with enabled=True drives the unchanged rug-pull status reply.
    # exec_bootstrap_v1=True marks the one-time executor bootstrap as done so
    # get_or_create_settings leaves the row (and this fake) untouched.
    db = _FakeDB(SimpleNamespace(
        enabled=True, symbol="PEPE/USDT", direction="long", entry_price=0.0000012,
        exec_bootstrap_v1=True,
    ))
    text, mode = asyncio.run(cs._handle_sniper("", db))
    assert mode == "HTML"
    assert "🎯 <b>Sniper</b>: 🟢 Active" in text
    assert "Live trades:" in text
    assert "SMC Sniper" not in text


def test_whitespace_only_args_use_legacy_status_path(monkeypatch):
    async def _boom(*_a, **_k):
        raise AssertionError("blank args must not run SMC analysis")

    monkeypatch.setattr(cs, "_sniper_smc", _boom)
    text, _ = asyncio.run(cs._handle_sniper("   ", _FakeDB()))
    assert "SMC Sniper" not in text


def test_args_route_to_smc(monkeypatch):
    async def _fake(args, _db):
        return f"routed:{args}", "HTML"

    monkeypatch.setattr(cs, "_sniper_smc", _fake)
    text, _ = asyncio.run(cs._handle_sniper("xauusd 1h", _FakeDB()))
    assert text == "routed:xauusd 1h"


# ── smc_analyze invocation ────────────────────────────────────────────────────

def test_calls_smc_analyze_with_normalized_args(smc_calls):
    asyncio.run(cs._handle_sniper("xauusd 1h", _FakeDB(_FakeMT5Account(id=7))))
    assert len(smc_calls.calls) == 1
    kwargs = smc_calls.calls[0]
    assert kwargs["symbol"] == "XAUUSD"
    assert kwargs["timeframe"] == "H1"
    assert kwargs["account_id"] == 7
    # FastAPI Query() defaults are unusable on a direct await — all passed explicitly.
    for key in ("count", "min_rr", "max_rr", "sl_buffer_atr", "min_confidence", "use_ai"):
        assert key in kwargs


def test_defaults_to_h1_when_timeframe_omitted(smc_calls):
    asyncio.run(cs._handle_sniper("EURUSD", _FakeDB(_FakeMT5Account(id=3))))
    assert smc_calls.calls[0]["timeframe"] == "H1"
    assert smc_calls.calls[0]["symbol"] == "EURUSD"


def test_unknown_timeframe_never_reaches_smc_analyze(smc_calls):
    text, _ = asyncio.run(cs._handle_sniper("XAUUSD 3h", _FakeDB(_FakeMT5Account(id=7))))
    assert smc_calls.calls == []
    assert "3h" in text and "1h" in text


def test_no_mt5_account_returns_error_not_raise(smc_calls):
    text, mode = asyncio.run(cs._handle_sniper("XAUUSD", _FakeDB(None)))
    assert mode == "HTML"
    assert text.startswith("❌")
    assert smc_calls.calls == []


def test_analysis_failure_returns_error_text(smc_calls, monkeypatch):
    async def _raise(**_k):
        raise RuntimeError("mt5 terminal offline")

    sys.modules["plugins.MT5TradingPlugin.backend.router"].smc_analyze = _raise
    text, _ = asyncio.run(cs._handle_sniper("XAUUSD", _FakeDB(_FakeMT5Account(id=1))))
    assert text.startswith("❌")
    assert "mt5 terminal offline" in text


# ── Message formatting ────────────────────────────────────────────────────────

def _full_response(**overrides):
    signal = SimpleNamespace(
        side="buy", order_type="buy_limit", entry=2320.5, stop_loss=2312.0,
        take_profit=2345.0, rr=2.3, confidence=0.72, zone_kind="bullish_ob",
        kronos_aligned=True, fusion_score=0.81,
    )
    base = dict(
        symbol="XAUUSD", timeframe="H1", error=None, bias="bullish",
        momentum="expanding", last_price=2345.67, atr=4.2, atr_pct=0.18,
        rsi=58.1, volume_z=1.2, equilibrium=2340.0,
        range={"low": 2300.0, "high": 2380.0},
        structure_events=[{"type": "BOS", "direction": "bullish", "level": 2350.0}],
        liquidity={"buyside": [2381.0, 2390.0], "sellside": [2299.0]},
        zones=[SimpleNamespace(kind="bullish_ob"), SimpleNamespace(kind="bearish_fvg")],
        signals=[signal, signal, signal, signal, signal],
        kronos={
            "engine": "kronos", "direction": "up", "pct_change": 0.42,
            "confidence": 0.68, "target_price": 2360.0, "anchor_price": 2345.0,
            "summary": "Model projects continuation into the buyside pool.",
        },
        ai={
            "available": True, "provider": "openai", "model": "gpt-4o",
            "bias_comment": "Bullish structure intact above equilibrium.",
            "market_read": "Price swept sellside liquidity and reclaimed the OB.",
            "rated_signals": [{"entry": 2320.5, "verdict": "take", "confidence": 0.7,
                               "note": "Cleanest discount entry."}],
            "top_pick_entry": 2320.5,
            "risk_warning": "High-impact CPI release in 3 hours.",
        },
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_message_contains_every_section():
    text = cs._fmt_sniper_analysis(_full_response())
    assert "XAUUSD H1" in text
    assert "BULLISH" in text                    # bias
    assert "Structure:" in text and "BOS" in text
    assert "order blocks" in text and "FVGs" in text
    assert "Liquidity:" in text
    assert "DISCOUNT" in text or "PREMIUM" in text
    assert "Top Setups" in text
    assert "SL" in text and "TP" in text and "R:R" in text and "Conf" in text
    assert "Kronos" in text and "68%" in text
    assert "aligned" in text and "0.81" in text  # kronos_aligned + fusion_score
    assert "AI Review" in text
    assert "Bullish structure intact" in text
    assert "CPI release" in text


def test_setup_list_is_capped_at_three():
    text = cs._fmt_sniper_analysis(_full_response())
    assert "(3 of 5)" in text
    assert "4. " not in text


def test_message_stays_under_telegram_limit():
    huge = _full_response()
    huge.ai = dict(huge.ai)
    huge.ai["market_read"] = "x" * 20_000
    huge.ai["bias_comment"] = "y" * 20_000
    huge.kronos = dict(huge.kronos)
    huge.kronos["summary"] = "z" * 20_000
    text = cs._fmt_sniper_analysis(huge)
    assert len(text) < 4096
    assert "…" in text


def test_premium_when_price_above_equilibrium():
    assert "PREMIUM" in cs._fmt_sniper_analysis(_full_response(last_price=2370.0))


def test_no_signals_renders_placeholder():
    text = cs._fmt_sniper_analysis(_full_response(signals=[]))
    assert "No qualifying setups" in text
    assert "Top Setups" not in text


def test_unavailable_ai_and_kronos_are_reported():
    text = cs._fmt_sniper_analysis(
        _full_response(kronos=None, ai={"available": False, "reason": "no providers"})
    )
    assert "no forecast available" in text
    assert "unavailable" in text and "no providers" in text


def test_engine_error_short_circuits():
    text = cs._fmt_sniper_analysis(_full_response(error="not enough candles"))
    assert text.startswith("❌")
    assert "not enough candles" in text


def test_ai_text_is_html_escaped():
    text = cs._fmt_sniper_analysis(
        _full_response(ai={"available": True, "provider": "p", "model": "m",
                           "bias_comment": "risk <b>high</b> & rising",
                           "market_read": "", "rated_signals": [],
                           "top_pick_entry": None, "risk_warning": ""})
    )
    assert "&lt;b&gt;high&lt;/b&gt;" in text
    assert "&amp; rising" in text


# ── Help text ─────────────────────────────────────────────────────────────────

def test_help_documents_the_new_form():
    text, _ = asyncio.run(cs._handle_help("", _FakeDB()))
    assert "/sniper XAUUSD 1h" in text


# ── Macro context reaches the Telegram card ──────────────────────────────────
# The card used to print no reason at all — only the AI block's prose — so every
# deterministic factor behind the score was invisible here.

def _resp_with(macro, reason="Discount, order block, macro DXY -0.20% risk-on (+0.31); RR 2.0; score 0.71"):
    class _Sig:
        side, order_type, entry, stop_loss, take_profit = "buy", "limit", 100.0, 98.0, 104.0
        rr, confidence, zone_kind = 2.0, 0.71, "order_block"
        kronos_aligned, fusion_score = True, 0.8

    _Sig.reason = reason

    class _Resp:
        symbol, timeframe, error = "XAUUSD", "1h", None
        bias, momentum, last_price = "bullish", "up", 4107.0
        atr, atr_pct, rsi, volume_z = 5.0, 0.1, 52.0, 0.3
        equilibrium, range = 4100.0, {"low": 4050.0, "high": 4150.0}
        structure_events, zones, liquidity = [], [], {}
        signals = [_Sig()]
        kronos, ai = None, None

    _Resp.macro = macro
    return _Resp()


def test_the_card_states_the_macro_read_when_it_applied():
    card = cs._fmt_sniper_analysis(_resp_with({
        "applied": True,
        "regime": "RISK_ON",
        "reason": "DXY -0.20% / VIX 15.9 risk-on (+0.31)",
        "lines": ["DXY 99.803 (-0.20%) is offered; USD is the quote leg of XAUUSD."],
    }))

    assert "Macro" in card
    assert "DXY" in card
    assert "quote leg" in card


def test_the_card_says_so_when_macro_did_not_apply():
    """A pair with no USD leg still signals — and still explains itself."""
    card = cs._fmt_sniper_analysis(_resp_with({
        "applied": False,
        "reason": "macro n/a (no USD leg)",
        "lines": [],
    }))

    assert "not applied" in card
    assert "no USD leg" in card


def test_the_per_signal_reason_now_reaches_the_user():
    card = cs._fmt_sniper_analysis(_resp_with({"applied": False, "reason": "x", "lines": []}))
    assert "order block" in card and "score 0.71" in card
