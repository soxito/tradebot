"""Guards on how JARVIS turns a spoken command into an instrument.

The defect these exist for: the command dispatcher appended ``USDT`` to any
bare symbol, so ``analyze GBPUSD`` became ``GBPUSDUSDT``, missed the forex gate,
fell into the Bitget-only branch and died with "I couldn't find a
Bitget-tradeable pair for GBPUSD". Every FX pair, metal and index was
unreachable. These are source-level and behavioural guards against a repeat.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.services import market_data as md

_JARVIS = Path(__file__).resolve().parents[1] / "app" / "api" / "jarvis.py"


# ── Source guards ────────────────────────────────────────────────────────────

def test_dispatcher_never_appends_a_quote_suffix():
    """The exact line that broke FX/metals/indices must not come back.

    Canonicalisation belongs to market_data, which knows XAUUSD is already a
    whole instrument; the dispatcher's regexes are token finders only.
    """
    source = _JARVIS.read_text()
    offenders = [
        (i, line.strip())
        for i, line in enumerate(source.splitlines(), 1)
        if '+= "USDT"' in line or "+= 'USDT'" in line
    ]
    assert not offenders, (
        "jarvis.py re-introduced an unconditional USDT suffix — this is what "
        f"turned GBPUSD into GBPUSDUSDT: {offenders}"
    )


def test_analyze_symbol_has_a_universal_route():
    """_analyze_symbol must consult the two-tier guard, not is_forex_symbol alone."""
    tree = ast.parse(_JARVIS.read_text())
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_analyze_symbol"
    )
    body = ast.unparse(fn)
    assert "is_universal_symbol" in body, (
        "_analyze_symbol no longer routes through market_data.is_universal_symbol; "
        "FX crosses, indices and commodities will fall into the Bitget branch"
    )


def test_bitget_dead_end_tries_the_universal_resolver_first():
    """The 'no Bitget-tradeable pair' error must be a genuine last resort."""
    tree = ast.parse(_JARVIS.read_text())
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_analyze_symbol"
    )
    body = ast.unparse(fn)
    error_at = body.index("Bitget-tradeable pair")
    rescue_at = body.index("fetch_ohlcv_universal")
    assert rescue_at < error_at, (
        "the Bitget error is raised before the universal fallback is attempted"
    )


def test_analysis_reports_a_real_price_source():
    """price_source was hardcoded to 'yahoo_finance_live' whatever served it."""
    source = _JARVIS.read_text()
    assert '"price_source": "yahoo_finance_live"' not in source, (
        "price_source is hardcoded again — provenance must come from the ticker"
    )


# ── Behavioural ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "spoken,expected",
    [
        ("GBPUSD", "GBPUSD"),
        ("gbpusd", "GBPUSD"),
        ("XAUUSD", "XAUUSD"),
        ("XAGUSD", "XAGUSD"),
        ("US30", "US30"),
        ("USOIL", "USOIL"),
        ("EURGBP", "EURGBP"),
    ],
)
def test_spoken_non_crypto_symbols_survive_canonicalisation(spoken, expected):
    symbol, asset_class = md.canonicalize_for_analysis(spoken)
    assert symbol == expected
    assert asset_class != md.CRYPTO
    assert md.is_universal_symbol(symbol), (
        f"{expected} would not reach the universal route and would hit Bitget"
    )


@pytest.mark.parametrize("spoken", ["BTC", "BTCUSDT", "SOL", "ETHUSDT"])
def test_crypto_still_routes_to_the_catalog(spoken):
    """Crypto must NOT be diverted to Yahoo — the exchanges carry real volume."""
    symbol, asset_class = md.canonicalize_for_analysis(spoken)
    assert asset_class == md.CRYPTO
    assert not md.is_universal_symbol(symbol)
