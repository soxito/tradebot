"""What the desk is actually working on right now.

The room used to publish a full call — four Telegram messages, chart included —
for whatever symbol its rotation happened to land on. The rotation is fed by
recent signal rows, and a signal channel's parse noise ends up in there too, so
the bot sent complete trade plans for instruments nobody holds, nobody asked
about, and in some cases that do not exist ("NOK"). Every one of those trains
the reader to stop looking at the ones that matter.

This module defines the set the desk is entitled to interrupt someone about:

  pinned      a pair the user pinned in the room — they asked for it
  held        a pair with an open position on a linked account
  live        a pair with an active published signal still being managed

Anything else is analysis: it belongs in the room UI and the logs, not in a
push notification. A pair the user asks about by name bypasses all of this,
because a question is its own justification for an answer.
"""
from __future__ import annotations

import time
from typing import Any, Optional, Set

from loguru import logger

#: The set changes on the scale of trades, not milliseconds; rebuilding it for
#: every publish decision would put four queries in front of every message.
_TTL_S = 60.0
_cache: tuple[float, Set[str]] | None = None


def normalise(symbol: Any) -> str:
    """One spelling for one instrument, on both sides of every comparison.

    Also folds the doubled quote leg ("LTCUSDTUSDT") that the old dispatcher
    left in the database — otherwise a position in LTCUSDT does not match a
    call on LTCUSDT and the pair reads as out of scope while it is being held.
    """
    out = str(symbol or "").upper().replace("/", "").replace(":", "").strip()
    for quote in ("USDT", "USDC", "USD"):
        doubled = quote + quote
        if out.endswith(doubled):
            out = out[: -len(quote)]
            break
    return out


def invalidate() -> None:
    """Drop the cached set — for tests, and after a position opens or closes."""
    global _cache
    _cache = None


async def active_symbols(db: Any) -> Set[str]:
    """Every instrument the desk is currently pinned to, holding, or managing."""
    global _cache
    if _cache and (time.time() - _cache[0]) < _TTL_S:
        return _cache[1]

    out: Set[str] = set()

    try:
        from app.agents import room

        out.update(normalise(s) for s in room.get_focus_symbols())
    except Exception as exc:  # noqa: BLE001 — each source is independent
        logger.debug(f"[scope] focus unavailable: {exc}")

    try:
        from sqlalchemy import select

        from app.models.database import RoomSettings

        # The saved pin as well as the live one: a worker that has not re-armed
        # yet, or a process that is not the API, still knows what the user asked
        # the desk to watch.
        saved = (await db.execute(
            select(RoomSettings.focus_symbol).where(RoomSettings.id == 1)
        )).scalar_one_or_none()
        out.update(normalise(part) for part in str(saved or "").split(",") if part.strip())
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[scope] saved focus unavailable: {exc}")

    try:
        from sqlalchemy import select

        from app.models.database import Trade

        rows = await db.execute(
            select(Trade.symbol).where(Trade.status == "open").limit(200)
        )
        out.update(normalise(r[0]) for r in rows if r[0])
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[scope] open trades unavailable: {exc}")

    try:
        from sqlalchemy import select

        from plugins.MT5TradingPlugin.backend.models import MT5Position

        rows = await db.execute(select(MT5Position.symbol).limit(200))
        out.update(normalise(r[0]) for r in rows if r[0])
    except Exception as exc:  # noqa: BLE001 — plugin-optional
        logger.debug(f"[scope] MT5 positions unavailable: {exc}")

    try:
        from sqlalchemy import select

        from plugins.TelegramSignalNewsPlugin.backend.models import (
            SignalStatus, TelegramParsedSignal, TelegramSniperTrade,
        )

        # Only signals the desk actually took. An ingested channel carries
        # dozens of live calls at any moment, most of them on instruments this
        # account has no position in and no intention of taking — treating those
        # as "ours" is how the scope grew to seventy symbols, most of them parse
        # noise, which is no scope at all.
        rows = await db.execute(
            select(TelegramParsedSignal.symbol)
            .join(TelegramSniperTrade,
                  TelegramSniperTrade.signal_id == TelegramParsedSignal.id)
            .where(TelegramParsedSignal.status == SignalStatus.ACTIVE)
            .limit(200)
        )
        out.update(normalise(r[0]) for r in rows if r[0])
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[scope] active signals unavailable: {exc}")

    out.discard("")
    # Whatever the sources produced, only real instruments survive: every table
    # feeding this one carries strings that came out of a parser.
    out = {s for s in out if _is_instrument(s)}
    _cache = (time.time(), out)
    return out


def _is_instrument(symbol: str) -> bool:
    """Can the platform price this string at all?"""
    try:
        from app.services import market_data

        return market_data.classify(market_data.normalize_symbol(symbol)) != market_data.UNKNOWN
    except Exception:  # noqa: BLE001
        return True


async def is_active(db: Any, symbol: str) -> bool:
    """Is this instrument one the desk is entitled to send a message about?"""
    return normalise(symbol) in await active_symbols(db)


def is_user_initiated(trigger: Optional[str]) -> bool:
    """Did a person ask for this analysis by name?

    ``telegram`` and ``manual`` mean someone typed the pair; those answers are
    delivered by whoever asked, and go out whether or not the pair is one the
    desk is otherwise working on.
    """
    return str(trigger or "").lower() in {"telegram", "manual", "api", "user"}
