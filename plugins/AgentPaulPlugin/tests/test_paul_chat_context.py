"""What Paul is shown decides what Paul says.

Two defects these guard:

* The prompt cap cut the middle out of a 35k-char prompt, dropping the live
  MT5 positions block — "how many positions do I have" was answered from the
  two rows that survived in the tail (3 instead of 11).
* Stored chat transcripts were injected as "established" memory, so a past
  answer was re-sent word for word to a different question.
"""

from __future__ import annotations

from plugins.AgentPaulPlugin.backend.services.paul_chat import (
    _is_transcript_memory,
    _shed_sections,
)


def _prompt() -> str:
    return (
        "PERSONA\n## HOW TO TALK\nrules"
        "\n## Open MT5 Positions (11)\n" + "row\n" * 11
        + "\n## Brain Map Stats\n" + "x" * 4000
        + "\n## Available Analysis Tools\n" + "y" * 4000
        + "\n## Current Page\nDashboard"
    )


def test_low_value_sections_go_before_live_account_data():
    prompt = _prompt()
    shed = _shed_sections(prompt, len(prompt) - 5000)

    assert "## Open MT5 Positions (11)" in shed
    assert shed.count("row\n") == 11
    assert "## Brain Map Stats" not in shed
    assert "## Current Page" in shed  # the tail is kept too, not just the head


def test_nothing_is_dropped_when_it_already_fits():
    prompt = _prompt()
    assert _shed_sections(prompt, len(prompt)) == prompt


def test_transcripts_and_command_logs_are_not_memory():
    assert _is_transcript_memory({"source": "chat", "content": "Q: hi → A: Doing well"})
    assert _is_transcript_memory({"source": "jarvis-command", "content": "JARVIS close OUT: No open position"})
    assert _is_transcript_memory({"source": "jarvis-command", "content": "JARVIS chat IT: Q: how is it going"})


def test_real_learnings_are_kept():
    assert not _is_transcript_memory({"source": "jarvis-command", "content": "JARVIS research: CPI beat forecast"})
    assert not _is_transcript_memory({"source": "self-learning", "content": "User risks 1% per trade"})
    assert not _is_transcript_memory({"source": "remember", "content": "User trades the London open"})
