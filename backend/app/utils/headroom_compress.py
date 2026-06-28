"""
Headroom context-compression wrapper.

Compresses OpenAI-format message lists before they are sent to the LLM,
cutting token usage 60-95% with no change to answer quality.

Install:  pip install "headroom-ai[all]"
Docs:     https://github.com/headroomlabs-ai/headroom

Env vars (all optional):
  HEADROOM_ENABLED=true|false   — master switch (default: true when installed)
  HEADROOM_LOG_SAVINGS=true     — log token savings per call (default: false)

Falls back silently to the original messages if headroom is not installed
or raises any error during compression.
"""
from __future__ import annotations

import os
from typing import List, Dict, Any

from loguru import logger

# ── Detect installation once at import time ──────────────────────────────────
try:
    from headroom import compress as _headroom_compress  # type: ignore

    _HEADROOM_AVAILABLE = True
except ImportError:
    _headroom_compress = None  # type: ignore
    _HEADROOM_AVAILABLE = False
    logger.debug(
        "[Headroom] headroom-ai not installed — context compression disabled. "
        "Run: pip install 'headroom-ai[all]' to enable."
    )


def compress_messages(
    messages: List[Dict[str, Any]],
    *,
    caller: str = "",
) -> List[Dict[str, Any]]:
    """
    Compress an OpenAI-format messages list using headroom.

    Args:
        messages: List of {"role": ..., "content": ...} dicts.
        caller:   Optional label for log lines (e.g. agent name).

    Returns:
        Compressed messages list, or the original list if compression is
        unavailable / disabled / fails.
    """
    if not _HEADROOM_AVAILABLE:
        return messages

    if os.getenv("HEADROOM_ENABLED", "true").lower() in {"0", "false", "no"}:
        return messages

    try:
        result = _headroom_compress(messages)

        # headroom may return a CompressResult object instead of a plain list.
        # Extract the messages list from whatever it returns.
        if isinstance(result, list):
            compressed = result
        elif hasattr(result, "messages"):
            compressed = result.messages
        elif hasattr(result, "__iter__"):
            compressed = list(result)
        else:
            # Unknown return type — fall back to originals
            logger.debug(f"[Headroom] Unexpected return type {type(result).__name__!r} — skipping")
            return messages

        # Ensure the result is a valid list of dicts (OpenAI message format)
        if not isinstance(compressed, list) or not compressed:
            return messages

        if os.getenv("HEADROOM_LOG_SAVINGS", "false").lower() in {"1", "true", "yes"}:
            orig_chars = sum(len(str(m.get("content", ""))) for m in messages)
            comp_chars = sum(len(str(m.get("content", ""))) for m in compressed)
            pct = 100 * (1 - comp_chars / orig_chars) if orig_chars else 0
            tag = f"[{caller}] " if caller else ""
            logger.info(
                f"[Headroom] {tag}compressed {orig_chars:,} → {comp_chars:,} chars "
                f"({pct:.0f}% reduction)"
            )

        return compressed
    except Exception as exc:
        logger.debug(f"[Headroom] Compression skipped ({exc!r}); sending original messages.")
        return messages
