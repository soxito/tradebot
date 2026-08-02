"""`/forecast` and `/analyze` must reach a real engine for *every* pair a user
trades — all crypto, and every MT5 instrument class.

`test_forecast_fx_command` pinned the FX majors after they were fixed. The same
routing then quietly failed a further set of MT5 spellings, each for its own
reason, and every one of them landed on the crypto connectors and died on a
Bitget lookup:

  * metal crosses (XAUEUR)     — Yahoo has no ticker; the provider *synthesises*
                                 them from two USD legs, but the routing gate
                                 asked `resolve_ticker` alone and got None.
  * MT5 energy (XTIUSD)        — looks like an FX pair, but XTI is no currency.
  * softs (CORN, SOYBEAN)      — simply absent from the ticker table.
  * broker suffixes (EURUSD_i) — `_` was deleted rather than stripped, gluing
                                 the marker on: EURUSD_i → EURUSDI.

These tests pin the whole matrix, so a pair class cannot fall out of coverage
again without a failure naming it.
"""
from __future__ import annotations

import pytest

from app.exchanges import yahoo_provider as yp
from app.services import market_data as md
from plugins.KronosForecastPlugin.backend.services import forecast_service as fs
from plugins.TelegramSignalNewsPlugin.backend.services import command_service as cs


# Every MT5 instrument class, in the spelling Telegram sends.
MT5_PAIRS = [
    # FX majors
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "NZDUSD", "USDCAD",
    # FX crosses
    "EURGBP", "EURJPY", "GBPJPY", "AUDJPY", "CADJPY", "CHFJPY", "EURAUD",
    "GBPAUD", "AUDNZD", "NZDCAD",
    # FX exotics
    "USDZAR", "USDMXN", "USDTRY", "USDSGD", "USDNOK", "USDSEK", "USDPLN",
    "USDCNH", "EURTRY", "GBPZAR",
    # Metals — including the crosses Yahoo synthesises rather than lists
    "XAUUSD", "XAGUSD", "XPTUSD", "XPDUSD", "XAUEUR", "XAGEUR", "XAUGBP",
    # Indices
    "US30", "US500", "NAS100", "USTEC", "US2000", "GER40", "UK100", "FRA40",
    "EU50", "ESP35", "AUS200", "HK50", "JPN225",
    # Energy — both the friendly and the X-prefixed broker spellings
    "USOIL", "UKOIL", "NGAS", "XTIUSD", "XBRUSD", "XNGUSD",
    # Softs
    "COCOA", "COFFEE", "SUGAR", "COTTON", "WHEAT", "CORN", "SOYBEAN",
    # Broker-decorated forms
    "EURUSD.m", "EURUSD_i", "XAUUSD.raw", "US30.cash", "GBPUSDpro",
    "NAS100.ecn", "XAUUSD.micro",
]

CRYPTO_PAIRS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "ADAUSDT",
    "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "PEPEUSDT", "ARBUSDT",
    "BTC", "ETH", "SOL",
    # MT5 lists its crypto CFDs against USD — still crypto, still the connectors.
    "BTCUSD", "ETHUSD", "SOLUSD",
]


# ── the two gates that decide which engine a pair reaches ────────────────────

@pytest.mark.parametrize("typed", MT5_PAIRS)
def test_every_mt5_pair_routes_to_the_market_data_path(typed):
    """`/forecast` gate. False here means the crypto connectors get the pair,
    and no connector lists an MT5 instrument — the user gets an error."""
    assert fs._is_forex(cs._norm_sym(typed)) is True


@pytest.mark.parametrize("typed", MT5_PAIRS)
def test_every_mt5_pair_routes_to_the_universal_analysis_path(typed):
    """`/analyze` gate — must agree with the forecast gate above, or the two
    commands disagree about what the same symbol is."""
    assert md.is_universal_symbol(cs._norm_sym(typed)) is True


@pytest.mark.parametrize("typed", CRYPTO_PAIRS)
def test_crypto_stays_on_the_exchange_connectors(typed):
    """Crypto must not leak onto the Yahoo path: the connectors carry deeper
    history and real traded volume. This is the guard that a wider `supports()`
    could have broken — a crypto cross (BTCEUR) synthesises from a `-USD` leg
    exactly the way a metal cross does."""
    norm = cs._norm_sym(typed)
    assert fs._is_forex(norm) is False
    assert md.is_universal_symbol(norm) is False


@pytest.mark.parametrize("typed", ["BTCEUR", "ETHEUR", "BTCGBP"])
def test_crypto_crosses_are_not_mistaken_for_metal_crosses(typed):
    """`_cross_legs` synthesises BTCEUR the same way it does XAUEUR, so the
    non-crypto gate has to look at the *leg* it would use, not just whether one
    exists."""
    assert yp.supports(typed) is True            # the provider can build it
    assert yp.supports_non_crypto(typed) is False  # but it is not an MT5 pair


# ── broker decorations ───────────────────────────────────────────────────────

@pytest.mark.parametrize("typed,bare", [
    ("EURUSD.m", "EURUSD"), ("EURUSD_i", "EURUSD"), ("EURUSD_pro", "EURUSD"),
    ("XAUUSD.raw", "XAUUSD"), ("XAUUSD.micro", "XAUUSD"), ("US30.cash", "US30"),
    ("GBPUSDpro", "GBPUSD"), ("NAS100.ecn", "NAS100"),
    # A suffix the strip list has never seen — brokers invent their own.
    ("EURUSD.stp", "EURUSD"), ("XAUUSD.prime", "XAUUSD"),
])
def test_broker_decorations_are_stripped(typed, bare):
    assert yp.normalize(typed) == bare


# ── the pairs the provider must be able to build bars for ────────────────────

@pytest.mark.parametrize("typed", MT5_PAIRS)
def test_provider_can_serve_every_mt5_pair(typed):
    """Routing a pair to Yahoo is only useful if Yahoo can actually build it."""
    assert yp.supports(typed) is True


# ── timeframes ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tf", ["1m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d", "1w"])
def test_requested_timeframe_is_served_on_the_mt5_path(tf):
    """A timeframe with no Yahoo bar fell back to the *daily* Frankfurter series
    and then failed the volume gate — so `/forecast EURUSD 2h` answered NO_TRADE
    for a pair that forecasts fine at 1h. Every ccxt timeframe the command
    accepts must map to a bar the provider can fold."""
    yf = fs._YF_TIMEFRAME.get(tf)
    assert yf is not None, f"{tf} has no Yahoo timeframe"
    assert yf in yp._TF_MAP, f"{tf} maps to {yf}, which the provider does not serve"


def test_folded_timeframes_come_off_an_intraday_grid():
    """H2/H6/H12 are folded from 60m bars by `_bucket`, which is only valid on a
    UTC grid — daily bars are stamped at each exchange's own session open."""
    for tf in ("H2", "H4", "H6", "H12"):
        interval, _rng = yp._TF_MAP[tf]
        assert interval in yp._INTRADAY_INTERVALS


# ── nothing offers to execute an MT5 pair ────────────────────────────────────

@pytest.mark.parametrize("typed", MT5_PAIRS)
def test_no_mt5_pair_is_ever_offered_as_an_executable_order(typed):
    """Widening what routes to the MT5 path widens what must NOT get an execute
    button — a "LIVE $5" on CORN offers an order Bitget cannot fill."""
    assert fs.is_order_placeable(cs._norm_sym(typed)) is False


@pytest.mark.parametrize("typed", ["BTCUSDT", "ETHUSDT", "SOLUSDT", "PEPEUSDT"])
def test_crypto_stays_executable(typed):
    assert fs.is_order_placeable(cs._norm_sym(typed)) is True


# ── MT5 crypto CFD spelling reaches a real market ────────────────────────────

@pytest.mark.parametrize("typed,expected", [
    ("BTC/USD", "BTC/USDT"),
    ("ETH/USD", "ETH/USDT"),
    ("BTCUSD", "BTCUSDT"),
])
def test_mt5_crypto_cfd_spelling_falls_back_to_the_stablecoin_pair(typed, expected):
    """MT5 quotes crypto against USD; no connector has a BTC/USD market, so
    `/forecast BTCUSD` found no candles at all until the USDT spelling was tried
    as well."""
    variants = fs._symbol_variants(typed)
    assert expected in variants
    # the literal spelling is still tried first — a connector that does list it wins
    assert variants.index(typed.upper()) < variants.index(expected)


# ── The narrative inside the card is model-written prose ─────────────────────

@pytest.mark.asyncio
async def test_the_jarvis_narrative_is_converted_not_pasted_raw(monkeypatch):
    """`**bold**` showed as asterisks, and a bare `&` can make Telegram reject
    the whole message under parse_mode=HTML."""
    from plugins.TelegramSignalNewsPlugin.backend.services import command_service as cs

    class _Sig:
        direction, pct_change, confidence = "up", 2.57, 0.65
        target_price, anchor_price, summary = 64519.96, 62901.9, "up"
        decision, rationale = "OK", []

    class _Resp:
        engine, note, anchor_price = "kronos", None, 62901.9
        symbol, timeframe, exchange = "BTC/USDT", "1h", "bitget"
        signal = _Sig()

    class _Jarvis:
        analysis = "**DIRECTION & MAGNITUDE**\nKronos predicts **+2.57 %** for BTC."
        position_advice, position, market = None, None, None

    async def _fake_run(exchange, symbol, timeframe, **kw):
        return _Resp()

    async def _no_signals(*a, **k):
        raise RuntimeError("no signals in this test")

    async def _fake_analyze(resp, learn=False):
        return _Jarvis()

    from plugins.KronosForecastPlugin.backend.services import forecast_service, jarvis_analysis

    monkeypatch.setattr(forecast_service, "run_forecast_cached", _fake_run)
    monkeypatch.setattr(forecast_service, "generate_sniper_signals", _no_signals)
    monkeypatch.setattr(jarvis_analysis, "analyze_forecast", _fake_analyze)

    card, _mode, _kb = await cs._handle_forecast("BTCUSDT 1h", None)

    assert "**" not in card, "markdown asterisks reached the user"
    assert "DIRECTION &amp; MAGNITUDE" in card, "a bare & would break HTML parsing"
    assert "<b>+2.57 %</b>" in card


# ── the intent layer must cover the same matrix ──────────────────────────────
# Running the engine off a free-text question is only useful if the question
# can name the pair the way a user actually types it. A pair that fails here
# silently drops back to an essay — the exact failure this whole path exists to
# stop — so the matrix above is reused rather than re-listed.

@pytest.mark.parametrize("typed", MT5_PAIRS + CRYPTO_PAIRS)
def test_every_pair_is_recognised_in_a_prediction_question(typed):
    assert cs._forecast_request(f"predict {typed}") is not None, (
        f"'predict {typed}' names no instrument — the forecaster cannot run"
    )


@pytest.mark.parametrize("typed", MT5_PAIRS + CRYPTO_PAIRS)
def test_the_intent_layer_and_the_command_agree_on_the_pair(typed):
    """Whatever the question resolves to must route like the typed spelling.

    Both gates are asserted for the *resolved* symbol, so intent detection
    cannot quietly hand the engine something the routing then rejects.
    """
    symbol, _tf = cs._forecast_request(f"predict {typed}")
    normalised = cs._norm_sym(symbol)
    assert fs._is_forex(normalised) == fs._is_forex(cs._norm_sym(typed))
    assert md.is_universal_symbol(normalised) or not fs._is_forex(normalised)


@pytest.mark.parametrize(
    ("phrase", "symbol"),
    [
        ("bitcoin", "BTCUSDT"), ("ethereum", "ETHUSDT"), ("solana", "SOLUSDT"),
        ("gold", "XAUUSD"), ("silver", "XAGUSD"),
        ("oil", "USOIL"), ("brent", "UKOIL"), ("natural gas", "NGAS"),
        ("nasdaq", "NAS100"), ("dow", "US30"), ("the dax", "GER40"),
        ("ftse", "UK100"),
    ],
)
def test_plain_english_names_reach_the_right_instrument(phrase, symbol):
    """Users type "predict gold", not "predict XAUUSD"."""
    assert cs._forecast_request(f"can you predict {phrase}") == (symbol, "1h")


@pytest.mark.parametrize("timeframe", ["5m", "15m", "1h", "4h", "1d", "1w"])
def test_a_named_timeframe_survives_for_any_pair(timeframe):
    assert cs._forecast_request(f"forecast XAUUSD {timeframe}") == ("XAUUSD", timeframe)
    assert cs._forecast_request(f"forecast ETHUSDT {timeframe}") == ("ETHUSDT", timeframe)


def test_prose_and_code_are_not_mistaken_for_pairs():
    """The token regex now allows a broker's underscore tail (EURUSD_i), which
    must not turn snake_case in a code answer into an instrument."""
    assert md.extract_symbols("df['log_ret'] = np.log(c / c_prev); my_var_x") == []
    assert md.extract_symbols("please explain the krebs cycle") == []
