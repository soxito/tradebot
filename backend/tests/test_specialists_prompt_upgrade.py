"""Prompts live in the DB: a stock prompt seeded before the OBLIVION section
landed must be recognised as stock, or existing installs keep the old text."""
from __future__ import annotations

import hashlib

import pytest

from app.agents import specialists as sp

ROLES = ("market_analyst", "signal_generator", "risk_manager", "trade_executor")

# raw / completeness-wrapped digests of the prompts as they were before 2026-09-21
PRE_OBLIVION = {
    "market_analyst": "887afe34e4bbe121e298b4ce9ddabd48caea21765bebfc653044a560f3b16fbe",
    "signal_generator": "ff884708feee56328b9974151c0e2c98f12982e337a298597a91c03811d38804",
    "risk_manager": "f6bdcd9fcfefb0abe1c6d10168c1eb93e667b13114367e1bf8eb9d40371d5670",
    "trade_executor": "ddd4039845f29e36548a5496d144bcc50259fd228b374a6ec3e5e2116bacf2e6",
}


@pytest.mark.parametrize("role", ROLES)
def test_current_default_mentions_oblivion_and_news_block(role):
    text = sp.DEFAULT_PROMPTS[role]
    assert "OBLIVION" in text
    assert "news" in text.lower()


@pytest.mark.parametrize("role", ROLES)
def test_pre_oblivion_digest_is_registered_as_legacy(role):
    assert PRE_OBLIVION[role] in sp.LEGACY_PROMPT_SHA256[role]


@pytest.mark.parametrize("role", ROLES)
def test_stock_text_without_the_oblivion_section_is_upgraded(role):
    current = sp.DEFAULT_PROMPTS[role]
    idx = current.find("OBLIVION")
    stripped = current[:idx].rstrip() + "\n"
    assert "OBLIVION" not in stripped
    assert sp.is_stock_prompt(role, stripped) is True
    assert sp.is_stock_prompt(role, sp.with_completeness(stripped)) is True


@pytest.mark.parametrize("role", ROLES)
def test_current_text_is_stock_and_a_user_edit_is_not(role):
    current = sp.DEFAULT_PROMPTS[role]
    assert sp.is_stock_prompt(role, sp.with_completeness(current)) is True
    assert sp.is_stock_prompt(role, "You are my own hand-written analyst. Only trade gold on Mondays.") is False


def test_digest_helper_matches_registered_form():
    # guard against someone re-hashing with a stripped/normalised string
    txt = sp.DEFAULT_PROMPTS["risk_manager"]
    assert hashlib.sha256(txt.encode("utf-8")).hexdigest() != PRE_OBLIVION["risk_manager"]
