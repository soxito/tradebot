"""Tests for the Deepgram cost-aware fallback budget guard and STT endpoint."""

import importlib
import json
import os
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def budget(tmp_path, monkeypatch):
    """Provide the budget module backed by an isolated temp usage store."""
    store = tmp_path / "deepgram-usage.json"
    monkeypatch.setenv("DEEPGRAM_USAGE_PATH", str(store))

    from app.services import deepgram_budget as mod
    importlib.reload(mod)
    return mod, store


def test_cost_for_seconds_uses_rate(budget):
    mod, _ = budget
    from app.core.config import settings

    # 60s at the configured per-minute rate == one minute of spend.
    assert mod.cost_for_seconds(60) == pytest.approx(settings.DEEPGRAM_STT_RATE_PER_MIN)
    assert mod.cost_for_seconds(0) == 0.0
    assert mod.cost_for_seconds(-5) == 0.0


def test_record_usage_accumulates_and_summary_math(budget):
    mod, store = budget
    from app.core.config import settings

    mod.record_usage(120)  # 2 minutes
    summary = mod.summary()

    expected = 2 * settings.DEEPGRAM_STT_RATE_PER_MIN
    assert summary["month_spend"] == pytest.approx(expected, abs=1e-4)
    assert summary["day_spend"] == pytest.approx(expected, abs=1e-4)
    assert summary["remaining"] == pytest.approx(settings.DEEPGRAM_MONTHLY_CAP_USD - expected, abs=1e-4)
    assert summary["monthly_cap"] == settings.DEEPGRAM_MONTHLY_CAP_USD
    # Store file persisted.
    assert json.loads(store.read_text())["month"]["spend"] > 0


def test_can_spend_blocks_when_month_cap_reached(budget):
    mod, store = budget
    now = datetime.now(timezone.utc)
    # Seed the store already at the monthly cap.
    store.write_text(json.dumps({
        "total_spend": 60.0,
        "first_use": now.isoformat(),
        "month": {"period": now.strftime("%Y-%m"), "spend": 60.0},
        "day": {"period": now.strftime("%Y-%m-%d"), "spend": 0.5},
    }))
    assert mod.can_spend(8) is False


def test_can_spend_blocks_when_day_cap_reached(budget):
    mod, store = budget
    now = datetime.now(timezone.utc)
    store.write_text(json.dumps({
        "total_spend": 5.0,
        "first_use": now.isoformat(),
        "month": {"period": now.strftime("%Y-%m"), "spend": 5.0},
        "day": {"period": now.strftime("%Y-%m-%d"), "spend": 5.0},
    }))
    assert mod.can_spend(8) is False


def test_can_spend_allows_when_under_caps(budget):
    mod, _ = budget
    assert mod.can_spend(8) is True


def test_month_and_day_rollover_resets_counters(budget):
    mod, store = budget
    # Old period far in the past → counters must reset on next read.
    store.write_text(json.dumps({
        "total_spend": 10.0,
        "first_use": "2020-01-01T00:00:00+00:00",
        "month": {"period": "2020-01", "spend": 59.0},
        "day": {"period": "2020-01-01", "spend": 4.9},
    }))
    summary = mod.summary()
    assert summary["month_spend"] == 0.0
    assert summary["day_spend"] == 0.0
    # Total credit spend is cumulative across periods, so it is preserved.
    assert summary["total_spend"] == pytest.approx(10.0)


def test_fallback_disabled_blocks_spend(budget, monkeypatch):
    mod, _ = budget
    from app.core.config import settings
    monkeypatch.setattr(settings, "DEEPGRAM_FALLBACK_ENABLED", False)
    assert mod.can_spend(1) is False


# ── Endpoint behaviour ────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Minimal stand-in for httpx.AsyncClient that returns a canned transcript."""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, **kwargs):
        return _FakeResponse(200, {
            "metadata": {"duration": 4.0},
            "results": {"channels": [{"alternatives": [
                {"transcript": "analyse gold for sniper entries", "confidence": 0.94}
            ]}]},
        })


@pytest.fixture()
def stt_client(tmp_path, monkeypatch):
    store = tmp_path / "deepgram-usage.json"
    monkeypatch.setenv("DEEPGRAM_USAGE_PATH", str(store))

    from app.services import deepgram_budget as mod
    importlib.reload(mod)

    from app.core.config import settings
    monkeypatch.setattr(settings, "DEEPGRAM_API_KEY", "test-key")

    import app.api.voice as voice
    monkeypatch.setattr(voice.httpx, "AsyncClient", _FakeAsyncClient)

    from app.main import app
    with TestClient(app) as client:
        yield client, store, settings


def test_stt_endpoint_transcribes_and_records_usage(stt_client):
    client, store, settings = stt_client
    audio = b"\x00" * 4096  # > 1KB so it is not treated as empty
    resp = client.post(
        "/api/v1/voice/deepgram/stt",
        files={"file": ("clip.webm", audio, "audio/webm")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["used_deepgram"] is True
    assert body["text"] == "analyse gold for sniper entries"
    assert body["confidence"] == pytest.approx(0.94)
    assert body["budget"]["month_spend"] > 0


def test_stt_endpoint_returns_false_when_capped(stt_client, monkeypatch):
    client, store, settings = stt_client
    # Force the monthly cap reached.
    now = datetime.now(timezone.utc)
    store.write_text(json.dumps({
        "total_spend": 60.0,
        "first_use": now.isoformat(),
        "month": {"period": now.strftime("%Y-%m"), "spend": 60.0},
        "day": {"period": now.strftime("%Y-%m-%d"), "spend": 0.0},
    }))
    resp = client.post(
        "/api/v1/voice/deepgram/stt",
        files={"file": ("clip.webm", b"\x00" * 4096, "audio/webm")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["used_deepgram"] is False
    assert body["reason"] == "budget_capped"


def test_stt_endpoint_skips_empty_clip(stt_client):
    client, store, settings = stt_client
    resp = client.post(
        "/api/v1/voice/deepgram/stt",
        files={"file": ("clip.webm", b"tiny", "audio/webm")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["used_deepgram"] is False
    assert body["reason"] == "empty_audio"


def test_usage_endpoint_reports_math(stt_client):
    client, store, settings = stt_client
    now = datetime.now(timezone.utc)
    store.write_text(json.dumps({
        "total_spend": 1.0,
        "first_use": now.isoformat(),
        "month": {"period": now.strftime("%Y-%m"), "spend": 0.5},
        "day": {"period": now.strftime("%Y-%m-%d"), "spend": 0.1},
    }))
    resp = client.get("/api/v1/voice/deepgram/usage")
    assert resp.status_code == 200
    body = resp.json()
    assert body["month_spend"] == pytest.approx(0.5)
    assert body["day_spend"] == pytest.approx(0.1)
    assert body["remaining"] == pytest.approx(settings.DEEPGRAM_MONTHLY_CAP_USD - 0.5)
