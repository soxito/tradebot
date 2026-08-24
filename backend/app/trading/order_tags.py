"""Order comments that identify which part of the app placed an MT5 order.

Every order the app sends carries a short tag in its MT5 comment so a position
can be traced back to what opened it, and so app orders can be told apart from
ones placed by hand in the terminal.

Format is ``<SOURCE>#<ref>`` — for example ``TG#154270`` for Telegram signal
154270, ``ROOM#42`` for a trading-room decision. MT5 truncates comments (most
brokers at 31 characters), so the tags stay deliberately terse and the
reference comes first after the prefix, where it survives truncation.

Anything that does not match a known tag is treated as **manual** — placed
directly in MetaTrader rather than by this app.

One platform limit shapes all of this: **an MT5 position's comment is fixed at
execution and cannot be changed afterwards.** ``OrderModifySafe`` (and MT5's
own ``TRADE_ACTION_SLTP``) carries price, stop-loss and take-profit only —
there is no comment field. Existing positions therefore cannot be re-labelled
on the broker; they are classified here instead, from the comment they already
carry, and the app stores that classification alongside the position.
"""
from __future__ import annotations

import re
from typing import Optional

#: Longest comment most MT5 brokers keep before truncating.
MT5_COMMENT_MAX = 31

# ── Sources ─────────────────────────────────────────────────────────────────
SOURCE_TELEGRAM = "telegram"
SOURCE_ROOM = "room"
SOURCE_SCALP = "scalp"
SOURCE_SMC = "smc"
SOURCE_MANUAL = "manual"

#: Tag written for each source. Keep these short and stable — changing one
#: makes every position already carrying it read as manual.
_TAG_BY_SOURCE = {
    SOURCE_TELEGRAM: "TG",
    SOURCE_ROOM: "ROOM",
    SOURCE_SCALP: "SCALP",
    SOURCE_SMC: "SMC",
}

#: Patterns that identify an app-placed order, newest scheme first. Legacy
#: comments are matched too so positions opened before the tags were unified
#: keep classifying correctly instead of flipping to "manual".
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (SOURCE_TELEGRAM, re.compile(r"^\s*TG#(\S+)", re.I)),
    (SOURCE_ROOM, re.compile(r"^\s*ROOM#(\S+)", re.I)),
    (SOURCE_SCALP, re.compile(r"^\s*SCALP#(\S+)", re.I)),
    (SOURCE_SMC, re.compile(r"^\s*SMC#(\S+)", re.I)),
    # ── legacy ──
    # ScalpBot#12 and its recovery variant ScalpBot-R#12.
    (SOURCE_SCALP, re.compile(r"^\s*ScalpBot(?:-R)?#(\S+)", re.I)),
    (SOURCE_ROOM, re.compile(r"^\s*room\b\s*(.*)", re.I)),
]


def build_comment(source: str, ref: Optional[str | int] = None, suffix: str = "") -> str:
    """Build the MT5 comment for an order this app is about to place.

    ``source`` is one of the ``SOURCE_*`` constants; ``ref`` is whatever makes
    the order traceable (a signal id, a decision id). The result is clipped to
    what MT5 keeps, so the caller never has to think about length.
    """
    tag = _TAG_BY_SOURCE.get(source)
    if tag is None:
        raise ValueError(f"unknown order source: {source!r}")
    comment = f"{tag}#{ref}" if ref is not None else tag
    if suffix:
        comment = f"{comment}-{suffix}"
    return comment[:MT5_COMMENT_MAX]


def classify(comment: Optional[str]) -> dict:
    """Work out what placed a position, from the comment MT5 stored.

    Returns ``{"origin": "app"|"manual", "source": str, "ref": str|None}``.
    A blank comment is manual: the app always tags what it sends.
    """
    text = (comment or "").strip()
    if not text:
        return {"origin": "manual", "source": SOURCE_MANUAL, "ref": None}
    for source, pattern in _PATTERNS:
        match = pattern.match(text)
        if match:
            ref = (match.group(1) or "").strip() or None
            return {"origin": "app", "source": source, "ref": ref}
    return {"origin": "manual", "source": SOURCE_MANUAL, "ref": None}


def is_app_order(comment: Optional[str]) -> bool:
    """True when this app placed the order, False when it was placed by hand."""
    return classify(comment)["origin"] == "app"


def describe(comment: Optional[str]) -> str:
    """Short human label for a position's origin, for UI and reports."""
    info = classify(comment)
    if info["origin"] == "manual":
        return "Manual order"
    labels = {
        SOURCE_TELEGRAM: "Telegram signal",
        SOURCE_ROOM: "Trading room",
        SOURCE_SCALP: "Scalp bot",
        SOURCE_SMC: "SMC strategy",
    }
    label = labels.get(info["source"], "App order")
    return f"{label} {info['ref']}" if info["ref"] else label
