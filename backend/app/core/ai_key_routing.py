"""Key → provider routing for the OpenAI-compatible clients.

Every OpenAI-compatible client in this codebase reads ``OPENAI_API_KEY``, but
that key is rarely an OpenAI key — it holds whichever free provider the user
connected (NVIDIA NIM, Groq, Cerebras, OpenRouter…). Sending it to
``api.openai.com`` returns a 401 that reads as "your OpenAI key is wrong", trips
the agents' circuit breaker, and makes the whole board fall back to local reads.

The key prefix is the source of truth for where the key may be sent. Nothing
here ever guesses in the other direction: an unrecognised prefix gets no base
URL rather than the OpenAI default.
"""
import os
from typing import List, Optional, Tuple

# Longer/more-specific prefixes first — "sk-or-" must beat "sk-".
_PROVIDER_BY_KEY_PREFIX: List[Tuple[str, str]] = [
    ("nvapi-", "https://integrate.api.nvidia.com/v1"),   # NVIDIA NIM
    ("sk-or-", "https://openrouter.ai/api/v1"),          # OpenRouter
    ("gsk_", "https://api.groq.com/openai/v1"),          # Groq
    ("csk-", "https://api.cerebras.ai/v1"),              # Cerebras
    ("sk-", "https://api.openai.com/v1"),                # OpenAI (keep last)
]

OPENAI_BASE_URL = "https://api.openai.com/v1"


def detect_base_url(api_key: str) -> Optional[str]:
    """The provider base URL this key belongs to, or None if unrecognised."""
    for prefix, base_url in _PROVIDER_BY_KEY_PREFIX:
        if api_key.startswith(prefix):
            return base_url
    return None


def is_openai_key(api_key: str) -> bool:
    """True only for a real OpenAI key — ``sk-or-`` is OpenRouter, not OpenAI."""
    return detect_base_url(api_key) == OPENAI_BASE_URL


def resolve_base_url(api_key: str, configured: Optional[str] = None) -> Optional[str]:
    """Base URL to call this key on.

    A ``configured`` URL is honoured only when it belongs to the same provider
    as the key; otherwise the key's own provider wins. Returns None when the key
    is unrecognised and nothing was configured — the caller then leaves the SDK
    to its own default rather than inventing one.
    """
    detected = detect_base_url(api_key)
    if configured and _same_provider(configured, api_key):
        return configured
    return detected


def _same_provider(url: str, api_key: str) -> bool:
    """Whether ``url`` is an endpoint this key can authenticate against."""
    detected = detect_base_url(api_key)
    if detected is None:
        return True  # unknown key — trust what was configured
    host_marker = {
        "https://integrate.api.nvidia.com/v1": "nvidia",
        "https://openrouter.ai/api/v1": "openrouter",
        "https://api.groq.com/openai/v1": "groq",
        "https://api.cerebras.ai/v1": "cerebras",
        OPENAI_BASE_URL: "openai.com",
    }[detected]
    return host_marker in url.lower()


def build_async_client(api_key: str, base_url: Optional[str]):
    """Construct an ``AsyncOpenAI`` pinned to ``base_url``.

    The SDK reads ``OPENAI_BASE_URL`` from the environment *even when a
    base_url argument is passed*, so it is unset for the duration of the
    constructor. Without this the env value silently wins and the key goes
    somewhere it cannot authenticate.
    """
    from openai import AsyncOpenAI  # imported here so the module stays importable

    original = os.environ.pop("OPENAI_BASE_URL", None)
    try:
        if base_url:
            return AsyncOpenAI(api_key=api_key, base_url=base_url)
        return AsyncOpenAI(api_key=api_key)
    finally:
        if original is not None:
            os.environ["OPENAI_BASE_URL"] = original
