"""Publish a finished trading-room meeting to Telegram, fully explained.

The room used to announce its own conclusions through the generic alert
service: one line of title, one line of reasoning, three key/value pairs. A
trader reading that has the verdict and none of the argument — no levels, no
chart, no sense of which seat objected — so the call was unactionable exactly
when it mattered.

This publishes what the desk actually produced: the board's verdict, every
seat's reasoning, the structural read, the plan's levels, the copyable signal
card, and the drawn chart those levels come from. It is the same material the
``/room`` command sends back on Telegram, built from the same helpers, so a
meeting the user convened and a meeting the worker ran read identically.
"""
from __future__ import annotations

import html
import time
from typing import Any, Dict, List, Optional

from loguru import logger

#: Telegram's hard limit is 4096; leave room for the trailing footer.
_MAX_BODY = 3900


def _esc(value: Any, limit: int = 400) -> str:
    from app.agents.orchestrator import reasoning_text

    return html.escape(reasoning_text(value).strip().replace("\n", " "))[:limit]


def _pct(value: Any) -> str:
    try:
        num = float(value or 0)
    except (TypeError, ValueError):
        return "—"
    return f"{num * 100:.0f}%" if num <= 1 else f"{num:.0f}%"


def _verdict_block(
    result: Dict[str, Any], consensus: Dict[str, Any], symbol: str, timeframe: str
) -> str:
    """The headline: what the board agreed, how strongly, and on what vote."""
    action = str(result.get("final_action") or "hold").upper()
    icon = {"BUY": "🟢", "SELL": "🔴"}.get(action, "⚪")
    tally = consensus.get("tally") or {}
    lines = [
        f"🏛 <b>The desk has agreed — {html.escape(symbol)} {html.escape(timeframe)}</b>",
        f"{icon} <b>{action}</b>  ·  confidence {_pct(result.get('final_confidence'))}"
        f"  ·  agreement {_pct(consensus.get('agreement'))}",
        f"Votes: buy {tally.get('buy', 0)} / sell {tally.get('sell', 0)} / "
        f"hold {tally.get('hold', 0)}  ·  {result.get('agents_used', 0)} seats, "
        f"{result.get('ai_calls', 0)} AI calls",
    ]
    return "\n".join(lines)


def _seats_block(result: Dict[str, Any]) -> str:
    """Every seat's call and the reason it gave — the argument, not the summary.

    This is the part the alert-service message never carried. A verdict with no
    dissent shown reads as unanimous even when it was not, and a trader who
    cannot see the risk seat's objection cannot decide whether they share it.
    """
    rows: List[str] = []
    for d in result.get("decisions") or []:
        name = _esc(d.get("agent_name") or d.get("agent_role") or "agent", 40)
        act = _esc(d.get("action") or "-", 20).upper()
        why = _esc(d.get("reasoning"), 320)
        trimmed = " <i>(answer trimmed)</i>" if d.get("reasoning_trimmed") else ""
        conf = _pct(d.get("confidence"))
        rows.append(f"• <b>{name}</b> — {act} ({conf}): {why}{trimmed}")
    if not rows:
        return ""
    return "🗣 <b>What each seat said</b>\n" + "\n".join(rows)


def _forecast_block(result: Dict[str, Any]) -> str:
    """What the Kronos forecast contributed, when the room had one."""
    fc = result.get("kronos_forecast") or {}
    if not fc:
        return ""
    direction = str(fc.get("direction") or "").lower()
    arrow = {"up": "↑", "down": "↓"}.get(direction, "→")
    horizon = html.escape(str(fc.get("horizon") or "—"))
    line = f"{arrow} {html.escape(direction or 'flat')} over {horizon}"
    if isinstance(fc.get("pct_change"), (int, float)):
        line += f"  ·  {float(fc['pct_change']):+.2f}%"
    line += f"  ·  confidence {_pct(fc.get('confidence'))}"
    parts = [
        f"🔮 <b>Forecast ({html.escape(str(fc.get('engine') or 'kronos'))})</b>",
        line,
    ]
    if rationale := fc.get("rationale"):
        parts.append(_esc(rationale, 300))
    return "\n".join(parts)


def _reasoning_block(result: Dict[str, Any]) -> str:
    body = _esc(result.get("final_reasoning"), 900)
    return f"🧠 <b>Why</b>\n<i>{body}</i>" if body else ""


#: Symbol+action → when it last went out. The board re-analyses a pinned pair
#: on its cadence and will reach the same conclusion for as long as the setup
#: lasts; sending that conclusion again every hour is how a channel becomes
#: noise. A genuinely new call — the other direction — is never suppressed.
_last_published: Dict[str, float] = {}
REPEAT_SILENCE_S = 3 * 3600.0


async def _worth_sending(result: Dict[str, Any], symbol: str, action: str) -> bool:
    """Is this call one the desk should interrupt someone for?

    Three ways it is not. It was asked for by a person, in which case whoever
    they asked is already replying to them — publishing here as well is what
    turned one ``/room XAUUSD`` into eight messages. It is about an instrument
    the desk is not pinned to, holding, or managing, which is how complete trade
    plans for parse noise reached the channel. Or it is the same call as last
    time, on the pair the board has been re-reading all afternoon.
    """
    from app.agents import scope

    if scope.is_user_initiated(result.get("trigger")):
        logger.debug(f"[RoomPublisher] {symbol} was asked for directly — the caller replies")
        return False

    key = f"{scope.normalise(symbol)}:{action}"
    last = _last_published.get(key)
    if last is not None and (time.time() - last) < REPEAT_SILENCE_S:
        logger.info(
            f"[RoomPublisher] {symbol} {action.upper()} already published "
            f"{(time.time() - last) / 60:.0f} min ago — not repeating it"
        )
        return False

    try:
        from app.core.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            active = await scope.is_active(db, symbol)
    except Exception as exc:  # noqa: BLE001 — never lose a call to a scope error
        logger.debug(f"[RoomPublisher] scope check failed for {symbol}: {exc}")
        active = True
    if not active:
        logger.info(
            f"[RoomPublisher] {symbol} is not pinned, held or being managed — "
            "keeping this one in the room rather than on the channel"
        )
        return False

    _last_published[key] = time.time()
    return True


def forget_published() -> None:
    """Clear the repeat-silence memory — for tests."""
    _last_published.clear()


async def publish_meeting(
    result: Dict[str, Any], consensus: Dict[str, Any]
) -> bool:
    """Send a completed meeting to the Telegram bot's notification chats.

    Only actionable outcomes go out — a room that reported every HOLD would
    train the user to ignore it — but an actionable one goes out in full.
    Returns whether anything was delivered. Never raises: a Telegram outage
    must not fail the meeting that produced the call.
    """
    action = str(result.get("final_action") or "hold").lower()
    if action not in {"buy", "sell"}:
        return False

    symbol = str(result.get("symbol") or "")
    timeframe = str(result.get("timeframe") or "1h")
    if not symbol:
        return False

    if not await _worth_sending(result, symbol, action):
        return False

    try:
        from plugins.TelegramSignalNewsPlugin.backend.services import notifications, room_bridge
    except Exception as exc:  # noqa: BLE001 — plugin-optional
        logger.debug(f"[RoomPublisher] telegram plugin unavailable: {exc}")
        return False

    # ── The plan and the chart, computed once so words and picture agree ──
    overlay = None
    chart: Optional[bytes] = None
    candles: List[list] = []
    try:
        from app.services import candles as candle_source

        candles = await candle_source.fetch(symbol, timeframe)
        price = float(result.get("price") or (candles[-1][4] if candles else 0) or 0)
        overlay, chart = await room_bridge.room_plan(symbol, timeframe, result, price)
    except Exception as exc:  # noqa: BLE001 — the verdict ships without a chart
        logger.warning(f"[RoomPublisher] plan/chart unavailable for {symbol}: {exc}")

    # ── Message 1: the verdict, the argument, the forecast, the reasoning ──
    blocks = [
        _verdict_block(result, consensus, symbol, timeframe),
        _forecast_block(result),
        _seats_block(result),
        _reasoning_block(result),
    ]
    if errors := result.get("errors"):
        blocks.append(f"⚠️ {len(errors)} agent error(s): {_esc(errors[0], 160)}")
    sent = await notifications.notify("\n\n".join(b for b in blocks if b)[:_MAX_BODY])

    # ── Message 2: the copyable signal card, and the order behind it ──
    # The card is the desk's signal; the mirror is the desk taking it. They go
    # together deliberately — a published call the room did not trade cannot be
    # checked against anything later.
    try:
        if built := await room_bridge.built_card_for(
            result, symbol, overlay, candles=candles,
        ):
            await notifications.notify(
                room_bridge.signal_card_module().render(built)[:_MAX_BODY]
            )
            if report := await room_bridge.trade_published_card(
                built, symbol, float(result.get("price") or 0) or None
            ):
                if report.get("status") == "placed":
                    await notifications.notify(f"✅ Taken — {_esc(report.get('reason'), 300)}")
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[RoomPublisher] signal card skipped for {symbol}: {exc}")

    # ── Message 3: the structural read and the levels drawn on the chart ──
    try:
        read = await room_bridge.market_read_text(symbol, timeframe, candles) if candles else ""
        levels = room_bridge.plan_levels_text(overlay)
        if body := "\n\n".join(b for b in (read, levels) if b):
            await notifications.notify(body[:_MAX_BODY])
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[RoomPublisher] market read skipped for {symbol}: {exc}")

    # ── Message 4: the chart the levels were taken from ──
    if chart:
        try:
            await notifications.notify_photo(
                chart, caption=f"{html.escape(symbol)} {html.escape(timeframe)} — "
                f"the desk's plan ({action.upper()})",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[RoomPublisher] chart not sent for {symbol}: {exc}")

    logger.info(
        f"[RoomPublisher] published {action.upper()} {symbol} {timeframe} to Telegram "
        f"(delivered={sent}, chart={'yes' if chart else 'no'})"
    )
    return sent
