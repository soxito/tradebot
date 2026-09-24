"""Global OBLIVION settings: the field spec validates writes, the row merges
partial patches, and the sync cache the hot paths read reflects a save."""
from __future__ import annotations

import pytest

from app.services import oblivion_settings as svc
from app.signals.oblivion import OblivionConfig, analyze_oblivion


@pytest.fixture(autouse=True)
def _clean_cache():
    before = dict(svc._state)
    svc._state.update(cfg=None, version=0, loaded_at=0.0, updated_at=None)
    yield
    svc._state.update(before)


# ── field spec ───────────────────────────────────────────────────────────────

def test_every_field_maps_to_a_real_config_key_and_has_a_group():
    known = set(OblivionConfig().to_dict())
    for f in svc.FIELDS:
        assert f["key"] in known, f["key"]
        assert f["group"] and f["label"] and f["type"] in ("int", "float", "bool", "select", "session")
        if f["type"] in ("int", "float"):
            assert f.get("min") is not None and f.get("max") is not None, f["key"]
        if f["type"] == "select":
            assert f.get("options")


def test_the_spec_covers_the_knobs_the_screenshots_show():
    keys = {f["key"] for f in svc.FIELDS}
    assert {"trade_mode", "signal_cooldown", "scalp_min_score", "sniper_min_score", "sniper_require_htf",
            "sniper_require_sweep", "sniper_require_zone", "fast_ema", "medium_ema", "slow_ema", "use_vwap",
            "atr_length", "movement_min_atr", "volume_multiplier", "micro_len", "major_len", "pd_lookback",
            "fvg_min_atr", "fvg_extension", "ob_lookback", "ob_extension", "liquidity_lookback",
            "ote_618", "ote_705", "ote_786", "htf_timeframe", "htf_fast", "htf_slow", "require_session",
            "news_enabled", "news_window", "sl_atr_buffer", "min_risk_atr", "max_risk_atr",
            "tp1_rr", "tp2_rr", "tp3_rr", "require_room_tp3", "level_extension", "show_outcomes"} <= keys


def test_defaults_match_the_engine_defaults():
    d = svc.defaults()
    assert d["scalp_min_score"] == 6 and d["sniper_min_score"] == 9 and d["trade_mode"] == "HYBRID"
    assert d["pd_lookback"] == 92 and d["htf_fast"] == 19 and d["tp3_rr"] == 1.5


# ── validation ───────────────────────────────────────────────────────────────

def test_validate_coerces_strings_to_the_field_type():
    clean = svc.validate({"scalp_min_score": "7", "use_vwap": "false", "movement_min_atr": "0.3"})
    assert clean == {"scalp_min_score": 7, "use_vwap": False, "movement_min_atr": 0.3}


def test_validate_rejects_out_of_range_and_bad_enums():
    with pytest.raises(ValueError, match="above the maximum"):
        svc.validate({"scalp_min_score": 99})
    with pytest.raises(ValueError, match="below the minimum"):
        svc.validate({"atr_length": 1})
    with pytest.raises(ValueError, match="not one of"):
        svc.validate({"trade_mode": "YOLO"})


def test_validate_checks_session_windows():
    assert svc.validate({"news_window": "1330-1430"})["news_window"] == "1330-1430"
    with pytest.raises(ValueError, match="HHMM-HHMM"):
        svc.validate({"news_window": "13:30-14:30"})
    with pytest.raises(ValueError, match="HHMM-HHMM"):
        svc.validate({"asia": "9999-0000"})


def test_validate_ignores_unknown_keys_without_failing():
    assert svc.validate({"bogus": 1, "scalp_min_score": 5}) == {"scalp_min_score": 5}


# ── row + cache ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_first_load_creates_the_row_from_defaults(async_session):
    doc = await svc.load(async_session)
    assert doc["version"] == 1
    assert doc["settings"]["scalp_min_score"] == 6
    assert doc["defaults"]["scalp_min_score"] == 6
    assert doc["fields"] and doc["engine_version"] == "5.0"
    # the sync accessor the scalp bot / chart overlays use is now warm
    assert isinstance(svc.current_config(), OblivionConfig)
    assert svc.current_config().scalp_min_score == 6


@pytest.mark.asyncio
async def test_update_merges_bumps_the_version_and_refreshes_the_cache(async_session):
    await svc.load(async_session)
    doc = await svc.update(async_session, {"scalp_min_score": 8, "trade_mode": "SNIPER"})
    assert doc["version"] == 2
    assert doc["settings"]["scalp_min_score"] == 8 and doc["settings"]["trade_mode"] == "SNIPER"
    assert doc["settings"]["sniper_min_score"] == 9          # untouched keys survive
    assert svc.current_config().scalp_min_score == 8         # hot path sees it immediately
    assert svc.current_version() == 2

    again = await svc.update(async_session, {"sniper_min_score": 11})
    assert again["settings"]["scalp_min_score"] == 8 and again["settings"]["sniper_min_score"] == 11
    assert again["version"] == 3


@pytest.mark.asyncio
async def test_a_rejected_patch_changes_nothing(async_session):
    await svc.load(async_session)
    before = svc.current_config().scalp_min_score
    with pytest.raises(ValueError):
        await svc.update(async_session, {"scalp_min_score": 99})
    assert svc.current_config().scalp_min_score == before
    assert (await svc.load(async_session))["version"] == 1


@pytest.mark.asyncio
async def test_reset_restores_the_defaults(async_session):
    await svc.load(async_session)
    await svc.update(async_session, {"scalp_min_score": 8, "news_enabled": True})
    doc = await svc.reset(async_session)
    assert doc["settings"]["scalp_min_score"] == 6 and doc["settings"]["news_enabled"] is False
    assert svc.current_config().scalp_min_score == 6


@pytest.mark.asyncio
async def test_the_engine_uses_the_saved_settings_without_being_passed_a_config(async_session):
    import random

    await svc.load(async_session)
    rng = random.Random(3)
    rows, p = [], 100.0
    for i in range(300):
        o = p
        p += 0.4 + rng.uniform(-1.2, 1.2)
        rows.append([1_700_000_000_000 + i * 3_600_000, o, max(o, p) + 0.5, min(o, p) - 0.5, p, 1000.0])

    await svc.update(async_session, {"trade_mode": "SNIPER", "scalp_min_score": 5})
    res = analyze_oblivion(rows, "1h")          # no cfg argument — reads the global row
    assert res["mode"] == "SNIPER"
    assert res["score"]["scalp_min"] == 5
    assert res["config"]["trade_mode"] == "SNIPER"
