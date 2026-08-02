"""Guards on the prompt text that caused JARVIS to refuse live prices.

The original block listed crypto only, then instructed: "NEVER quote any price
not listed above. If asked for a price not listed, say 'I don't have that live
price right now.'" Since gold, FX and indices could never appear in that list,
the refusal was the prompt working exactly as written. These tests pin the
replacement wording so a revert is caught in CI rather than in a user's chat.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_PAUL_CHAT = _REPO / "plugins/AgentPaulPlugin/backend/services/paul_chat.py"
_TELEGRAM = _REPO / "plugins/TelegramSignalNewsPlugin/backend/services/command_service.py"
_MARKET_DATA = _REPO / "backend/app/services/market_data.py"


def _prose(path: Path) -> str:
    """Source with quoting and line-wrapping flattened away.

    Prompt sentences are wrapped across adjacent string literals for
    readability, so a raw substring search would fail on formatting rather than
    on meaning. These tests are about what the model is told, not how the
    literal is typeset.
    """
    text = path.read_text()
    for ch in ('"', "'", "\\n"):
        text = text.replace(ch, " ")
    return " ".join(text.split())


@pytest.mark.parametrize(
    "banned",
    [
        "NEVER quote any price not listed above",
        "I don't have that live price right now",
        "please check your exchange for the latest price",
    ],
)
def test_paul_prompt_no_longer_forbids_unlisted_prices(banned):
    """These made every non-crypto instrument permanently unanswerable."""
    assert banned not in _PAUL_CHAT.read_text(), (
        f"paul_chat.py reinstated a blanket price refusal: {banned!r}"
    )


def test_paul_prompt_asserts_multi_asset_market_data():
    text = _PAUL_CHAT.read_text()
    assert "LIVE MARKET DATA FOR EVERY ASSET CLASS" in text
    assert "price_lookup" in text, "the prompt must point at the fetch tool"


@pytest.mark.parametrize(
    "phrase",
    [
        "does not provide a live gold-price feed",
        "toolset does not include a live price feed",
        "cannot pull the exact spot price",
    ],
)
def test_prompts_explicitly_ban_the_observed_refusals(phrase):
    """The exact sentences users reported are named so the model can't emit them."""
    assert phrase in _prose(_PAUL_CHAT), f"paul_chat.py stopped banning {phrase!r}"
    assert phrase in _prose(_TELEGRAM), f"command_service.py stopped banning {phrase!r}"


def test_telegram_prompt_dropped_the_figures_only_clause():
    """'work only from the figures provided below' invited refusal when empty."""
    assert "work only from the figures provided below" not in _TELEGRAM.read_text()


def test_market_data_block_never_denies_the_capability():
    """Even the zero-results branch must call it a failed fetch, not a gap."""
    text = _MARKET_DATA.read_text()
    assert "NOT a missing capability" in text
    assert "Never tell them the platform lacks a live feed" in text


def test_paul_chat_no_longer_filters_prices_to_usdt():
    """The '/USDT' filter is what structurally excluded gold, FX and indices."""
    text = _PAUL_CHAT.read_text()
    assert '"/USDT" in sym' not in text
    assert 'endswith("USDT")' not in text
