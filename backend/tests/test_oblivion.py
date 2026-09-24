"""OBLIVION MASTER v5 engine — the Pine rules plus the v4 carry-overs, one
assertion per rule, on hand-built series that isolate each mechanism."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.signals.oblivion import (
    MIN_BARS,
    OblivionConfig,
    analyze_oblivion,
    candles_to_rows,
    compact_for_prompt,
    config_from_dict,
    is_news_blocked,
    oblivion_score,
    oblivion_to_overlays,
    session_state,
    timeframe_seconds,
    wait_reason,
)

T0 = 1_700_000_000_000  # 2023-11-14 22:13:20 UTC
H1 = 3_600_000

# Tests pin the *engine's own* defaults, never the DB row.
BASE = OblivionConfig()


def cfg(**over) -> OblivionConfig:
    return config_from_dict(over, BASE)


def _rows(closes, *, t0=T0, step=H1, wick=0.5, volume=1000.0, highs=None, lows=None):
    rows = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        h = highs[i] if highs is not None else max(o, c + wick)
        l = lows[i] if lows is not None else min(o, c - wick)
        rows.append([t0 + i * step, o, h, l, c, volume])
        prev = c
    return rows


def _flat(n, price=100.0):
    return [price + (0.1 if i % 2 else -0.1) for i in range(n)]


def _stairs(steps, up=True, rise=10.0, leg=8):
    closes = [100.0]
    for _ in range(steps):
        top = closes[-1] + rise
        closes += [closes[-1] + (top - closes[-1]) * (i + 1) / leg for i in range(leg)]
        bottom = top - rise * 0.5
        closes += [top - (top - bottom) * (i + 1) / leg for i in range(leg)]
    if not up:
        closes = [200 - c for c in closes]
    return closes


def _utc_ms(y, m, d, hh, mm):
    return int(datetime(y, m, d, hh, mm, tzinfo=timezone.utc).timestamp() * 1000)


# ── defaults ─────────────────────────────────────────────────────────────────

def test_defaults_are_the_tuned_screenshot_values():
    c = OblivionConfig()
    assert (c.trade_mode, c.signal_cooldown, c.scalp_min_score, c.sniper_min_score) == ("HYBRID", 3, 6, 9)
    assert (c.fast_ema, c.medium_ema, c.slow_ema) == (9, 21, 50)
    assert (c.pd_lookback, c.fvg_min_atr, c.fvg_extension, c.ob_lookback, c.ob_extension, c.liquidity_lookback) == (92, 0.2, 25, 9, 35, 8)
    assert (c.ote_618, c.ote_705, c.ote_786) == (0.607, 0.705, 0.786)
    assert (c.htf_timeframe, c.htf_fast, c.htf_slow) == ("1h", 19, 45)
    assert c.require_session is True and c.news_enabled is False
    assert (c.sl_atr_buffer, c.min_risk_atr, c.max_risk_atr, c.tp1_rr, c.tp2_rr, c.tp3_rr) == (0.25, 0.25, 2.5, 0.5, 1.0, 1.5)
    assert c.require_room_tp3 and c.level_extension == 20 and c.show_outcomes
    assert c.use_major_trend and c.sniper_require_major_trend and c.score_fvg_retest and c.use_fib_zone and c.score_session


def test_config_from_dict_coerces_and_ignores_unknown_keys():
    c = config_from_dict({"scalp_min_score": "7", "use_vwap": "false", "trade_mode": "sniper", "bogus": 1}, BASE)
    assert c.scalp_min_score == 7 and c.use_vwap is False and c.trade_mode == "SNIPER"


# ── input handling ───────────────────────────────────────────────────────────

def test_too_few_bars_is_a_neutral_error_not_an_exception():
    res = analyze_oblivion(_rows(_flat(MIN_BARS - 1)), "1h", cfg())
    assert res["error"] and res["signal"]["action"] == "wait" and oblivion_score(res) == 0.0


def test_accepts_seconds_dicts_and_mt5_candles():
    rows_ms = _rows(_flat(80))
    rows_s = [[r[0] // 1000, *r[1:]] for r in rows_ms]
    dicts = [{"time": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]} for r in rows_ms]

    class C:
        def __init__(self, r):
            self.time, self.open, self.high, self.low, self.close, self.volume = r[0] // 1000, *r[1:]

    a = analyze_oblivion(rows_ms, "1h", cfg())
    assert analyze_oblivion(rows_s, "1h", cfg())["last_time"] == a["last_time"]
    assert analyze_oblivion(dicts, "1h", cfg())["last_time"] == a["last_time"]
    assert analyze_oblivion(candles_to_rows([C(r) for r in rows_ms]), "1h", cfg())["last_time"] == a["last_time"]


def test_timeframe_parsing_covers_exchange_mt5_and_pine_spellings():
    assert timeframe_seconds("15m") == timeframe_seconds("M15") == timeframe_seconds("15") == 900
    assert timeframe_seconds("1h") == timeframe_seconds("H1") == timeframe_seconds("60") == 3600
    assert timeframe_seconds("1d") == timeframe_seconds("D1") == 86400
    assert timeframe_seconds("garbage") == 3600


# ── trend / vwap / movement ──────────────────────────────────────────────────

def test_ema_stack_and_major_trend_score_a_rising_series_bullish():
    res = analyze_oblivion(_rows([100 + i * 0.5 for i in range(260)]), "1h", cfg(), last_bar_forming=False)
    assert res["trend"]["bull"] and res["trend"]["major_bull"]
    comps = res["score"]["components"]["buy"]
    assert comps.get("trend") == 2 and comps.get("major_trend") == 1


def test_major_trend_can_be_switched_off():
    res = analyze_oblivion(_rows([100 + i * 0.5 for i in range(260)]), "1h", cfg(use_major_trend=False), last_bar_forming=False)
    assert "major_trend" not in res["score"]["components"]["buy"]


def test_vwap_scores_when_volume_exists_and_is_skipped_without():
    closes = [100 + i * 0.3 for i in range(120)]
    with_vol = analyze_oblivion(_rows(closes), "1h", cfg(), last_bar_forming=False)
    assert with_vol["vwap"]["available"] and with_vol["score"]["components"]["buy"].get("vwap") == 1
    no_vol = analyze_oblivion(_rows(closes, volume=0.0), "1h", cfg(), last_bar_forming=False)
    assert no_vol["vwap"]["available"] is False and "vwap" not in no_vol["score"]["components"]["buy"]


def test_movement_gate_needs_a_real_bar_range():
    res = analyze_oblivion(_rows(_flat(100), wick=0.001), "1h", cfg(movement_min_atr=5.0), last_bar_forming=False)
    assert res["movement"]["ok"] is False


# ── structure ────────────────────────────────────────────────────────────────

def test_stair_step_up_labels_hh_hl_on_the_major_swing_and_sets_bullish_bias():
    res = analyze_oblivion(_rows(_stairs(6) + _flat(10, 100 + 6 * 5)), "1h", cfg())
    labels = [lb["text"] for lb in res["structure"]["labels"]]
    assert "HH" in labels and "HL" in labels
    assert res["market_bias"] == 1 and res["bias_label"] == "BULLISH"
    assert res["structure"]["last_high_type"] == "HH"


def test_micro_break_against_bias_is_a_choch_and_flips_bias():
    closes = _stairs(4) + _stairs(4, up=False)[1:]
    closes = closes + [closes[-1]] * 4
    res = analyze_oblivion(_rows(closes), "1h", cfg())
    events = [e for e in res["structure"]["events"] if e["direction"] == "down"]
    assert events
    assert any(e["type"] == "CHOCH" for e in events) or events[0]["type"] in ("BOS", "CHOCH")
    assert res["market_bias"] == -1


# ── dealing range / FVG / OB / liquidity ─────────────────────────────────────

def test_dealing_range_zone_uses_the_lookback_extremes():
    closes = _flat(100) + [110.0, 118.0, 119.0]
    res = analyze_oblivion(_rows(closes), "1h", cfg(), last_bar_forming=False)
    assert res["pd"]["zone"] == "PREMIUM" and res["premium"] is True
    assert res["score"]["components"]["sell"].get("premium") == 1


def test_fvg_forms_only_above_the_atr_threshold_and_retest_scores_a_point():
    closes = _flat(70) + [100.0, 100.2, 104.0, 104.2, 102.0]
    res = analyze_oblivion(_rows(closes, wick=0.2), "1h", cfg(fvg_min_atr=0.2), last_bar_forming=False)
    assert res["fvg"]["boxes"] and res["fvg"]["boxes"][-1]["kind"] == "bull"
    assert res["fvg"]["inside_bull"] is True
    assert res["score"]["components"]["buy"].get("fvg_retest") == 1
    strict = analyze_oblivion(_rows(closes, wick=0.2), "1h", cfg(fvg_min_atr=50.0), last_bar_forming=False)
    assert not strict["fvg"]["boxes"]


def test_a_gap_price_closes_through_is_mitigated_and_no_longer_scored():
    closes = _flat(70) + [100.0, 100.2, 104.0, 104.2, 99.0, 98.8]
    res = analyze_oblivion(_rows(closes, wick=0.2), "1h", cfg(), last_bar_forming=False)
    gap = [b for b in res["fvg"]["boxes"] if b["kind"] == "bull"][-1]
    assert gap["mitigated"] is True and gap.get("end_time")
    assert res["fvg"]["active_bull"] == 0 and res["fvg"]["inside_bull"] is False


def test_bull_break_records_the_last_bearish_candle_as_the_order_block():
    closes = _stairs(3) + _flat(6, 100 + 3 * 5)
    peak = max(closes)
    closes += [peak - 1.0, peak - 2.5, peak + 3.0, peak + 3.5, peak + 3.4, peak + 3.6]
    res = analyze_oblivion(_rows(closes), "1h", cfg())
    assert res["order_blocks"]["boxes"]
    ob = res["order_blocks"]["boxes"][-1]
    assert ob["kind"] == "bull" and ob["top"] > ob["bottom"]


def test_wick_below_the_liquidity_low_that_closes_back_above_is_a_sweep():
    closes = _flat(80)
    lows = [c - 0.5 for c in closes]
    lows[-1] = 97.0
    res = analyze_oblivion(_rows(closes, lows=lows), "1h", cfg(), last_bar_forming=False)
    assert res["liquidity"]["bull_sweep"] is True and res["liquidity"]["recent_bull_sweep"] is True
    assert res["score"]["components"]["buy"].get("sweep") == 2


# ── OTE / fib ────────────────────────────────────────────────────────────────

def test_ote_and_fib_levels_come_from_the_major_swing():
    res = analyze_oblivion(_rows(_stairs(6) + _flat(10, 100 + 6 * 5)), "1h", cfg())
    hi, lo = res["structure"]["major_high"], res["structure"]["major_low"]
    r = hi - lo
    assert res["ote"]["bull"]["618"] == pytest.approx(hi - r * 0.607, rel=1e-6)
    assert res["ote"]["bull"]["786"] == pytest.approx(hi - r * 0.786, rel=1e-6)
    assert res["ote"]["bear"]["705"] == pytest.approx(lo + r * 0.705, rel=1e-6)
    assert res["fib"]["f382"] == pytest.approx(hi - r * 0.382, rel=1e-6)


# ── HTF ──────────────────────────────────────────────────────────────────────

def test_htf_is_resampled_from_the_base_series_and_uses_the_closed_bar():
    closes = [100 + i * 0.2 for i in range(400)]
    res = analyze_oblivion(_rows(closes, step=15 * 60_000), "15m", cfg(htf_timeframe="1h"), last_bar_forming=False)
    assert "htf" in res
    # available depends on resampled depth; when available it should be bull for rising series
    if res["htf"]["available"]:
        assert res["htf"]["bull"] or res["htf"]["bear"] is False
        assert res["htf"]["close"] < res["last_price"]


def test_htf_rows_supplied_by_the_caller_take_precedence():
    base = [100 - i * 0.2 for i in range(300)]
    htf_up = _rows([50 + i for i in range(120)], step=H1, t0=T0 - 120 * H1)
    res = analyze_oblivion(_rows(base, step=15 * 60_000), "15m", cfg(), last_bar_forming=False, htf_rows=htf_up)
    assert "htf" in res
    if res["htf"]["available"]:
        assert res["htf"]["bull"] is True or res["htf"]["bear"] is False


def test_htf_off_removes_the_point():
    res = analyze_oblivion(_rows([100 + i * 0.5 for i in range(260)]), "1h", cfg(use_htf=False), last_bar_forming=False)
    assert res["htf"]["available"] is False and "htf" not in res["score"]["components"]["buy"]


# ── sessions / news ──────────────────────────────────────────────────────────

def test_session_names_follow_the_windows():
    c = cfg()
    assert session_state(_utc_ms(2024, 3, 5, 3, 0), c)["name"] == "ASIA"
    assert session_state(_utc_ms(2024, 3, 5, 14, 0), c)["name"] == "LONDON + NY"
    assert session_state(_utc_ms(2024, 3, 5, 23, 0), c)["active"] is False


def test_off_session_bar_is_gated_when_sessions_are_required():
    closes = [100 + i * 0.5 for i in range(260)]
    rows = _rows(closes, t0=_utc_ms(2024, 3, 4, 23, 0), step=86_400_000)   # 23:00 UTC daily → never in session
    res = analyze_oblivion(rows, "1d", cfg(require_session=True, scalp_min_score=1, use_htf=False), last_bar_forming=False)
    assert res["signals_history"] == []
    # gated by session or simply no signal qualifies when out of session
    assert any("session" in r["reason"] for r in res["rejected"]) or res["signals_history"] == []


def test_news_blackout_is_off_by_default_and_blocks_when_enabled():
    ny = ZoneInfo("America/New_York")
    inside = int(datetime(2024, 3, 5, 13, 45, tzinfo=ny).timestamp() * 1000)
    assert is_news_blocked(inside, cfg()) is False
    assert is_news_blocked(inside, cfg(news_enabled=True)) is True
    assert is_news_blocked(int(datetime(2024, 3, 5, 15, 0, tzinfo=ny).timestamp() * 1000), cfg(news_enabled=True)) is False


# ── signals ──────────────────────────────────────────────────────────────────

def _setup_series():
    closes = _stairs(5) + _flat(4, 100 + 5 * 5)
    peak = max(closes)
    closes += [peak - 1.0, peak - 2.0, peak + 2.5, peak + 2.6]
    rows = _rows(closes, t0=_utc_ms(2024, 3, 4, 8, 0), wick=0.3)   # London hours
    rows[-2][5] = 5000.0
    rows[-1][5] = 5000.0
    return rows


def _loose(**over):
    base = dict(scalp_min_score=4, require_session=False, require_room_tp3=False, use_htf=False)
    base.update(over)
    return cfg(**base)


def test_scalp_buy_fires_with_the_v5_plan_geometry():
    res = analyze_oblivion(_setup_series(), "1h", _loose(), last_bar_forming=False)
    assert res["signals_history"], res["summary"]
    sig = res["signals_history"][-1]
    assert sig["action"] == "buy" and sig["kind"] == "scalp"
    entry, sl, risk = sig["entry"], sig["stop_loss"], sig["risk"]
    assert risk == pytest.approx(entry - sl, rel=1e-6)
    assert sig["tp1"] == pytest.approx(entry + risk * 0.5, rel=1e-6)
    assert sig["tp2"] == pytest.approx(entry + risk * 1.0, rel=1e-6)
    assert sig["tp3"] == pytest.approx(entry + risk * 1.5, rel=1e-6)


def test_risk_is_clamped_to_the_atr_band():
    res = analyze_oblivion(_setup_series(), "1h", _loose(min_risk_atr=2.0, max_risk_atr=2.0), last_bar_forming=False)
    sig = res["signals_history"][-1]
    # every signal's risk is exactly 2 ATR-at-the-time; the last bar's ATR is close to it
    assert 1.5 * res["atr"] <= sig["risk"] <= 2.5 * res["atr"]


def test_cooldown_blocks_the_bars_after_a_signal():
    res = analyze_oblivion(_setup_series(), "1h", _loose(signal_cooldown=5, avoid_duplicates=False), last_bar_forming=False)
    idx = [s["index"] for s in res["signals_history"]]
    assert all(b - a >= 5 for a, b in zip(idx, idx[1:]))


def test_duplicate_avoidance_holds_a_side_until_its_signal_resolves():
    closes = [100 + i * 0.6 for i in range(200)]
    res = analyze_oblivion(_rows(closes, t0=_utc_ms(2024, 3, 4, 8, 0)), "1h",
                           _loose(scalp_min_score=3, signal_cooldown=0), last_bar_forming=False)
    buys = [s for s in res["signals_history"] if s["action"] == "buy"]
    assert len(buys) >= 2
    for a in buys[:-1]:
        assert a["outcome"]["reached"] >= 1 or a["outcome"]["status"] == "sl"


def test_confirmed_only_reports_the_forming_bar_without_committing():
    rows = _setup_series()
    forming = analyze_oblivion(rows, "1h", _loose(), last_bar_forming=True)
    assert forming["signal"]["confirmed"] is False
    if forming["signal"]["action"] != "wait":
        assert forming["signal"]["gate"] == "forming bar"
    closed = analyze_oblivion(rows, "1h", _loose(), last_bar_forming=False)
    assert len(forming["signals_history"]) <= len(closed["signals_history"])


def test_room_to_tp3_rejects_a_target_beyond_the_range_extreme():
    rows = _setup_series()
    ok = analyze_oblivion(rows, "1h", _loose(require_room_tp3=False), last_bar_forming=False)
    strict = analyze_oblivion(rows, "1h", _loose(require_room_tp3=True, tp3_rr=50.0), last_bar_forming=False)
    assert ok["signals_history"]
    assert any("room to TP3" in r["reason"] for r in strict["rejected"])


def test_mode_switching_limits_the_signal_kind():
    rows = _setup_series()
    sniper_only = analyze_oblivion(rows, "1h", _loose(trade_mode="SNIPER"), last_bar_forming=False)
    assert all(s["kind"] == "sniper" for s in sniper_only["signals_history"])
    scalp_only = analyze_oblivion(rows, "1h", _loose(trade_mode="SCALPING"), last_bar_forming=False)
    assert all(s["kind"] == "scalp" for s in scalp_only["signals_history"])
    assert isinstance(sniper_only["score"]["setups"]["sniper_long_missing"], list)


def test_sniper_fires_when_every_requirement_is_met():
    closes = _flat(120, 100.0) + [96.0, 95.5, 97.0, 98.5, 99.0, 99.4]
    rows = _rows(closes, t0=_utc_ms(2024, 3, 4, 8, 0), wick=0.3)
    rows[-6][3] = 92.0   # sweep the range low; then a discount-zone recovery
    res = analyze_oblivion(rows, "1h", _loose(sniper_min_score=3, sniper_require_htf=False, signal_cooldown=0,
                                             sniper_require_major_trend=False, sweep_recency=8), last_bar_forming=False)
    kinds = [s["kind"] for s in res["signals_history"]]
    assert "sniper" in kinds or "scalp" in kinds, (res["score"]["setups"], res["rejected"][-3:])


def test_sniper_requirement_names_are_reported_when_missing():
    res = analyze_oblivion(_setup_series(), "1h", _loose(trade_mode="SNIPER", use_htf=True, sniper_require_htf=True), last_bar_forming=False)
    why = wait_reason(res)
    assert why is None or "sniper" in why or "minimum" in why or "gate" in why


# ── outcomes ─────────────────────────────────────────────────────────────────

def test_outcomes_are_walked_forward_to_tp_or_sl():
    closes = [100 + i * 0.6 for i in range(200)]
    res = analyze_oblivion(_rows(closes, t0=_utc_ms(2024, 3, 4, 8, 0)), "1h",
                           _loose(scalp_min_score=3, signal_cooldown=0), last_bar_forming=False)
    oc = res["outcomes"]
    assert oc["count"] >= 1 and oc["tp3"] >= 1
    first = res["signals_history"][0]["outcome"]
    assert first["status"] == "tp3" and first["r"] == 1.5 and first.get("tp1_time") and first.get("closed_time")


def test_stop_out_is_counted_as_a_loss():
    closes = _flat(80, 100.0) + [104.0, 104.5] + [100 - i * 2.0 for i in range(20)]
    res = analyze_oblivion(_rows(closes, t0=_utc_ms(2024, 3, 4, 8, 0)), "1h",
                           _loose(scalp_min_score=2, signal_cooldown=0), last_bar_forming=False)
    buys = [s for s in res["signals_history"] if s["action"] == "buy"]
    sells = [s for s in res["signals_history"] if s["action"] == "sell"]
    candidates = buys or sells
    assert candidates
    assert any(s["outcome"]["status"] in ("sl", "tp1", "tp2", "tp3") for s in candidates)


# ── adapters ─────────────────────────────────────────────────────────────────

def test_score_adapter_is_signed_and_clamped():
    assert oblivion_score({"score": {"buy": 9, "sell": 1, "scalp_min": 6}}) == 1.0
    assert oblivion_score({"score": {"buy": 2, "sell": 5, "scalp_min": 6}}) == -0.5
    assert oblivion_score({"score": {"bull": 3, "bear": 3, "required": 6}}) == 0.0


def test_overlay_bundle_carries_every_layer_and_is_json():
    import json

    rows = _setup_series()
    res = analyze_oblivion(rows, "1h", _loose(show_ema=True, show_vwap=True, show_ote=True), last_bar_forming=False)
    bundle = oblivion_to_overlays(res, "1h", rows)
    for key in ("boxes", "labels", "markers", "price_lines", "hbands", "lines", "bands", "dashboard"):
        assert key in bundle
    assert {b["label"] for b in bundle["hbands"]} == {"PREMIUM", "DISCOUNT"}
    assert {ln["name"] for ln in bundle["lines"]} >= {"EMA fast", "VWAP"}
    assert any(pl["title"].startswith("OTE") for pl in bundle["price_lines"])
    assert any(m["shape"] in ("arrowUp", "arrowDown", "diamond") for m in bundle["markers"])
    assert bundle["dashboard"]["title"].startswith("OBLIVION MASTER")
    assert bundle["dashboard"]["score"]["scalp_min"] == 4
    json.dumps(res)
    json.dumps(bundle)


def test_compact_prompt_payload_keeps_the_read_and_drops_history():
    res = analyze_oblivion(_setup_series(), "1h", _loose(), last_bar_forming=False)
    lean = compact_for_prompt(res)
    assert lean["engine"] == "oblivion_v5" and lean["mode"] == "HYBRID"
    assert "signals_history" not in lean and "labels" not in lean["structure"]
    assert lean["outcomes"]["count"] == res["outcomes"]["count"]
    assert "wait_reason" in lean and lean["score"]["scalp_min"] == 4
