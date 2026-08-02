"""Guards on JARVIS answering questions outside trading.

The reported failure was a flat "I'm sorry, Sir, but I can't help with that."
Two things produced it: a persona that framed JARVIS purely as a trading
assistant, and a research layer that only searched Google News — so a question
about maths or science arrived with no grounding and nothing but a trading
identity to answer from.

These tests pin the fix at the three points where it can silently regress: the
prompt text, the research sources, and the truncation guard that decides which
prompt rules actually reach the model.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from plugins.AgentPaulPlugin.backend.services import news_research, paul_chat
from plugins.AiMarketAnalyst.backend.services import ai_tools

_REPO = Path(__file__).resolve().parents[3]
_PAUL_CHAT = _REPO / "plugins/AgentPaulPlugin/backend/services/paul_chat.py"
_TELEGRAM = _REPO / "plugins/TelegramSignalNewsPlugin/backend/services/command_service.py"


# ── The prompt must claim universal scope ────────────────────────────────────

@pytest.mark.parametrize(
    "subject",
    ["Mathematics", "Science", "history", "philosophy", "programming", "medicine"],
)
def test_persona_names_non_trading_subjects(subject):
    """Naming the fields explicitly is what stops "not my domain" refusals."""
    assert subject in paul_chat._JARVIS_PERSONA, (
        f"the persona stopped claiming {subject!r} as in scope"
    )


@pytest.mark.parametrize(
    "phrase",
    [
        "NEVER refuse a question because it is",
        "SEARCH BEFORE YOU DECLINE",
        "A bare",  # "A bare 'I can't help with that' is never acceptable"
    ],
)
def test_persona_forbids_the_observed_refusal(phrase):
    assert phrase in paul_chat._JARVIS_PERSONA, f"persona dropped: {phrase!r}"


def test_persona_bans_the_exact_refusal_wording():
    assert "I can't help with that" in paul_chat._JARVIS_PERSONA, (
        "the refusal the user actually received is no longer named in the prompt"
    )


def test_telegram_prompt_also_claims_universal_scope():
    """Telegram runs its own prompt; both entry points must agree."""
    text = _TELEGRAM.read_text()
    assert "YOUR SCOPE IS EVERYTHING" in text
    assert "mathematics" in text


# ── Research must reach beyond news ──────────────────────────────────────────

def test_research_queries_more_than_news():
    """Google News alone cannot ground a maths or science question."""
    for fn in ("wiki_lookup", "general_web_search", "qa_search"):
        assert hasattr(news_research, fn), f"news_research lost {fn}()"


@pytest.mark.parametrize(
    "query,expected",
    [
        ("what is the latest news on gold", True),
        ("who won the match yesterday", True),
        ("explain integration by parts", False),
        ("what is the Krebs cycle", False),
    ],
)
def test_current_events_classifier(query, expected):
    assert news_research.is_current_events(query) is expected


def test_research_returns_all_four_source_buckets():
    """Callers index these keys directly; a rename would silently drop a source."""
    import asyncio

    result = asyncio.run(news_research.research("x"))  # too short → empty, no network
    assert set(result) == {"news", "web", "reference", "qa"}


def test_format_research_block_is_empty_when_nothing_found():
    """An empty block must not claim sources were found."""
    empty = {"news": [], "web": [], "reference": [], "qa": []}
    assert news_research.format_research_block("anything", empty) == ""


def test_format_research_block_labels_its_sources():
    block = news_research.format_research_block(
        "krebs cycle",
        {
            "news": [], "web": [], "qa": [],
            "reference": [{"source": "Wikipedia", "title": "Citric acid cycle",
                           "summary": "A series of reactions.", "url": "http://x"}],
        },
    )
    assert "LIVE RESEARCH FOR THIS QUESTION" in block
    assert "Citric acid cycle" in block
    assert "Wikipedia" in block


# ── Tools ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tool", ["web_search", "remember", "recall"])
def test_knowledge_tools_are_exposed(tool):
    assert tool in ai_tools.TOOL_NAMES
    assert tool in ai_tools._HANDLERS, f"{tool} is advertised but has no handler"


def test_every_advertised_tool_can_actually_run():
    assert ai_tools.TOOL_NAMES == set(ai_tools._HANDLERS)


def test_web_search_tool_advertises_non_market_subjects():
    schema = next(
        t for t in ai_tools.TOOL_SCHEMAS if t["function"]["name"] == "web_search"
    )
    description = schema["function"]["description"]
    for subject in ("mathematics", "history", "programming"):
        assert subject in description, f"web_search no longer offers {subject}"


def test_text_directive_protocol_covers_the_memory_tools():
    """Providers without native tool support reach tools only through this."""
    for tool in ("web_search", "remember", "recall"):
        assert tool in ai_tools.TOOL_DIRECTIVE_PROMPT


# ── Routing ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "message,wants_glossary",
    [
        ("set my SL at 2300", True),
        ("how is BTCUSDT doing", True),
        ("analyse XAUUSD", True),
        ("explain integration by parts", False),
        ("who wrote the Iliad", False),
        ("how do vaccines work", False),
    ],
)
def test_trading_glossary_is_only_loaded_when_relevant(message, wants_glossary):
    """6 kB of SMC jargon on a biology question is prompt space taken from the answer."""
    assert paul_chat._wants_trading_glossary(message, "/") is wants_glossary


@pytest.mark.parametrize(
    "message,topic",
    [
        ("what is the derivative of x squared", "mathematics"),
        ("explain the Krebs cycle", "science"),
        ("debug this python function", "programming"),
        ("summarise the French Revolution", "history"),
        ("set my SL at 2300", "trading"),
    ],
)
def test_learnings_are_tagged_by_subject(message, topic):
    """Everything used to be stored as "general", so recall could not target."""
    assert paul_chat._classify_topic(message) == topic


# ── The truncation guard must not eat the rules ──────────────────────────────

def test_prompt_head_covers_the_whole_persona():
    """The cap keeps a head slice; if it is shorter than the persona, the last
    rules in the persona are deleted before the model ever sees them."""
    head_size = max(8_000, len(paul_chat._JARVIS_PERSONA) + 2_000)
    assert head_size >= len(paul_chat._JARVIS_PERSONA), (
        "the persona is longer than the preserved head — operating rules would "
        "be silently truncated"
    )
    assert f"len(_JARVIS_PERSONA) + 2_000" in _PAUL_CHAT.read_text(), (
        "the head size went back to a flat constant; it must be derived from "
        "the persona length"
    )


def test_glossary_lives_outside_the_persona():
    """Keeping it inline is what pushed the behaviour rules past the head slice."""
    assert "Trading Terminology" not in paul_chat._JARVIS_PERSONA
    assert "Trading Terminology" in paul_chat._TRADING_GLOSSARY
