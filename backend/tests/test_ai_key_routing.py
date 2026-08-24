"""A key is only ever sent to the provider it belongs to.

``OPENAI_API_KEY`` is a misnomer here: it holds whichever free provider the user
connected — usually an ``nvapi-`` NVIDIA NIM key. Sent to api.openai.com it
comes back 401 "Incorrect API key provided", which trips the agents' shared
circuit breaker and drops every seat on the trading room board to a local read
for the next five minutes. One misrouted key, a whole board of blank holds.

The headroom proxy is the subtle half: it is OpenAI-shaped and it picks its own
upstream (api.openai.com), so pointing a non-OpenAI key at it fails exactly the
same way while looking locally correct.
"""

from __future__ import annotations

import pytest

from app.core.ai_key_routing import (
    detect_base_url,
    is_openai_key,
    resolve_base_url,
)

NVIDIA = "https://integrate.api.nvidia.com/v1"
OPENAI = "https://api.openai.com/v1"


@pytest.mark.parametrize(
    "key, expected",
    [
        ("nvapi-abc", NVIDIA),
        ("sk-or-v1-abc", "https://openrouter.ai/api/v1"),
        ("gsk_abc", "https://api.groq.com/openai/v1"),
        ("csk-abc", "https://api.cerebras.ai/v1"),
        ("sk-abc", OPENAI),
    ],
)
def test_each_key_prefix_names_its_own_endpoint(key, expected):
    assert detect_base_url(key) == expected


def test_openrouter_key_is_not_an_openai_key():
    """``sk-or-`` starts with ``sk-`` — prefix order is what keeps them apart."""
    assert is_openai_key("sk-abc")
    assert not is_openai_key("sk-or-v1-abc")
    assert not is_openai_key("nvapi-abc")


def test_an_unknown_key_gets_no_endpoint_invented_for_it():
    assert detect_base_url("mystery-key") is None
    assert resolve_base_url("mystery-key") is None
    # …but a configured URL is still trusted, since nothing contradicts it.
    assert resolve_base_url("mystery-key", "https://example.test/v1") == "https://example.test/v1"


def test_a_configured_url_for_the_wrong_provider_loses_to_the_key():
    assert resolve_base_url("nvapi-abc", OPENAI) == NVIDIA


def test_a_configured_url_for_the_right_provider_is_kept():
    same_vendor = "https://integrate.api.nvidia.com/v1"
    assert resolve_base_url("nvapi-abc", same_vendor) == same_vendor


class TestAgentClient:
    """``_get_client`` is what actually put the NVIDIA key in front of OpenAI."""

    @staticmethod
    def _client(monkeypatch, key: str):
        from app.agents import base

        monkeypatch.setenv("OPENAI_API_KEY", key)
        monkeypatch.setenv("HEADROOM_PROXY_URL", "http://localhost:8787")
        monkeypatch.setenv("HEADROOM_OPENAI_BASE_URL", NVIDIA)
        # Set in .env and read by the SDK even when base_url is passed.
        monkeypatch.setenv("OPENAI_BASE_URL", "http://headroom-proxy:8787/v1")
        return base._get_client()

    def test_nvidia_key_goes_to_nvidia_not_the_proxy(self, monkeypatch):
        client = self._client(monkeypatch, "nvapi-test")
        assert "integrate.api.nvidia.com" in str(client.base_url)

    def test_a_real_openai_key_still_gets_the_compression_proxy(self, monkeypatch):
        client = self._client(monkeypatch, "sk-test")
        assert "localhost:8787" in str(client.base_url)

    def test_openrouter_key_is_not_mistaken_for_openai(self, monkeypatch):
        client = self._client(monkeypatch, "sk-or-v1-test")
        assert "openrouter.ai" in str(client.base_url)

    def test_the_env_base_url_is_restored_afterwards(self, monkeypatch):
        """It is unset around the constructor; leaking that would break callers."""
        import os

        self._client(monkeypatch, "nvapi-test")
        assert os.environ["OPENAI_BASE_URL"] == "http://headroom-proxy:8787/v1"


def test_voice_declines_a_non_openai_key(monkeypatch):
    """whisper-1 / tts-1 are OpenAI-only, so a NIM key is no key at all here.

    Returning None sends the caller to the local engine instead of a 401.
    """
    import asyncio

    from app.api import voice

    monkeypatch.setattr(voice.settings, "OPENAI_API_KEY", "nvapi-test", raising=False)
    assert asyncio.run(voice.get_client()) is None
