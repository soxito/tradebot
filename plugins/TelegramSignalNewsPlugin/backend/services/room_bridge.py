"""Run the trading room from Telegram and report back when it is done.

The room takes minutes, not seconds: seven agents, each an LLM call, plus a
vision pass when an image is involved. That is far longer than a bot reply can
wait, and the polling loop awaits each update in turn — so doing this inline
would freeze the bot for every other chat as well.

Everything here therefore runs detached: the command returns an acknowledgement
straight away, and this module sends the real answer (and any chart) when the
agents have finished. Each job opens its own DB session, because the request's
session is long closed by the time the room reports.
"""
from __future__ import annotations

import asyncio
import html
import re
from typing import Any

from loguru import logger

from app.services.text_format import format_for_telegram
from plugins.AiMarketAnalyst.backend.services.chart_render import PlanOverlay, render_plan_chart

#: Jobs in flight, kept only so the event loop cannot garbage-collect a task
#: mid-run (asyncio holds weak references to bare tasks).
_JOBS: set[asyncio.Task] = set()

#: One room job per chat at a time. The room is expensive and a second request
#: while the first is still sitting would double the spend and interleave two
#: sets of results in the same conversation.
_BUSY: set[str] = set()

#: A meeting that outlives this is wedged, not working. Every LLM call carries
#: its own timeout, so seven agents plus a chart should land well inside ten
#: minutes — anything longer means a hung feed or dead session, and the user
#: must be told rather than left staring at the acknowledgement forever.
ROOM_TIMEOUT = 10 * 60

_TIMEFRAMES = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d", "1w"}

#: Everything a chart might call a timeframe, mapped to what our feeds speak.
#: Screenshots come from TradingView (``4H``, ``30m``, ``1W``, and the numeric
#: ``240``), MT5 (``H4``, ``M30``, ``W1``) and plain English, so the label next
#: to the symbol is read in whichever dialect it was written. Guessing wrong
#: here is not a small error: reading a weekly chart as hourly would have the
#: room analyse a different market from the one on the user's screen.
_TF_ALIASES = {
    "m1": "1m", "m3": "3m", "m5": "5m", "m15": "15m", "m30": "30m",
    "h1": "1h", "h2": "2h", "h4": "4h", "h6": "6h", "h12": "12h",
    "d1": "1d", "w1": "1w",
    "60": "1h", "120": "2h", "240": "4h", "360": "6h", "720": "12h",
    "1": "1m", "3": "3m", "5": "5m", "15": "15m", "30": "30m",
    "d": "1d", "w": "1w", "daily": "1d", "weekly": "1w", "day": "1d", "week": "1w",
    "1hour": "1h", "4hour": "4h", "1day": "1d", "1week": "1w",
    "hourly": "1h", "1min": "1m", "5min": "5m", "15min": "15m", "30min": "30m",
}


def normalize_timeframe(raw: Any, default: str = "1h") -> str:
    """The candle size ``raw`` names, in the form our providers use.

    Returns ``default`` only when the label genuinely cannot be read — an
    unrecognised string is never passed through, because a timeframe our feeds
    reject would fail the whole analysis further down.
    """
    text = str(raw or "").strip().lower().replace(" ", "").replace("-", "")
    if not text:
        return default
    if text in _TIMEFRAMES:
        return text
    if (mapped := _TF_ALIASES.get(text)) in _TIMEFRAMES:
        return mapped
    # "XAUUSD 4H" / "4H chart" — take the first timeframe-shaped token present.
    # The digit-first form is scanned before the letter-first one, and in its
    # own pass: a single alternation lets the "d" of "xauusd" pair with the "4"
    # of "4h", consuming the digit so the real token can never be found.
    for pattern in (r"\d+[mhdw]", r"[mhdw]\d+"):
        for token in re.findall(pattern, text):
            if token in _TIMEFRAMES:
                return token
            if (mapped := _TF_ALIASES.get(token)) in _TIMEFRAMES:
                return mapped
    return default


def is_busy(chat_id: str) -> bool:
    return str(chat_id) in _BUSY


def spawn(coro, chat_id: str, token: str = "") -> None:
    """Run ``coro`` detached, releasing the per-chat lock when it finishes.

    A job that dies or wedges must tell the chat: silence after the
    acknowledgement is what made ``/room`` look stuck. The watchdog timeout
    guarantees the lock is released even when something downstream hangs
    without raising.
    """
    _BUSY.add(str(chat_id))

    async def _guarded() -> None:
        try:
            await asyncio.wait_for(coro, timeout=ROOM_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning(
                "[Room] background job timed out after {}s", ROOM_TIMEOUT
            )
            await _notify_failure(
                token, chat_id,
                "the agents ran out of time — try again in a moment",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a failed job must not kill the loop
            logger.exception("[Room] background job failed")
            await _notify_failure(token, chat_id, str(exc)[:300] or "unknown error")
        finally:
            _BUSY.discard(str(chat_id))

    task = asyncio.create_task(_guarded())
    _JOBS.add(task)
    task.add_done_callback(_JOBS.discard)


async def _notify_failure(token: str, chat_id: str, reason: str) -> None:
    """Tell the chat the meeting fell through instead of leaving them waiting."""
    if not token or not chat_id:
        return
    try:
        from plugins.TelegramSignalNewsPlugin.backend.services import bot_service

        await bot_service.send_message(
            token,
            chat_id,
            f"❌ The trading room could not finish — {html.escape(reason)}. "
            "Nothing was lost; run <code>/room</code> again to reconvene.",
        )
    except Exception:  # noqa: BLE001 — a failed notification must not mask the cause
        logger.exception("[Room] could not deliver the failure notice")


# ── Argument parsing ─────────────────────────────────────────────────────────

def parse_args(args: str) -> tuple[str | None, str, str]:
    """Split ``/room`` arguments into ``(symbol, timeframe, free_text)``.

    A bare instrument means "analyse this market"; anything else is a question
    for the agents.  Which strings count as an instrument is ``market_data``'s
    job, not a regex here: the old pattern listed seven currency bases, so
    ``/room CADJPY`` — and every other cross, index and commodity — was read as
    free text and answered by the chat model with a description of the pair
    instead of being sent to the board for a plan.

    Position decides how much benefit of the doubt a token gets.  While nothing
    but symbols and timeframes have been seen, a plain-English name resolves
    ("/room gold 4h"); once the text has turned into a sentence only an
    unmistakable spelling counts, so "/room why is gold selling off?" stays a
    question.
    """
    from app.services import market_data

    tokens = (args or "").split()
    symbol: str | None = None
    timeframe = "1h"
    rest: list[str] = []
    leading = True

    for tok in tokens:
        low = tok.lower()
        if low in _TIMEFRAMES:
            timeframe = low
            continue
        if symbol is None:
            found = market_data.symbol_from_token(tok, strict=not leading)
            if found:
                # Crypto keeps the separator the user typed ("ETH/USDT") — that
                # is the form the exchanges list.  Everything else takes the
                # resolved spelling, because "XAU/USD" is not a feed symbol.
                if ("/" in tok or "-" in tok) and market_data.classify(found) == market_data.CRYPTO:
                    symbol = tok.upper().replace("-", "/")
                else:
                    symbol = found
                continue
        leading = False
        rest.append(tok)

    return symbol, timeframe, " ".join(rest).strip()


# ── Turning agent output into something drawable ─────────────────────────────

def _decision(decisions: list[dict[str, Any]], role: str) -> dict[str, Any]:
    for d in decisions or []:
        if str(d.get("agent_role") or "").lower() == role:
            return d
    return {}


def _num(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


#: Fib ratios worth drawing on a small Telegram image. The full default set of
#: ten turns the chart into stripes; these are the ones a retracement is
#: actually read against.
_FIB_DRAWN = {0.382, 0.5, 0.618, 0.786}


def technical_context(candles: list[list]) -> dict[str, Any]:
    """Fib, support/resistance, EMAs and ATR computed from the candles.

    This is the market's own structure, not a trade call: it is drawn whatever
    the agents decided, exactly as the app's other charts show indicators
    regardless of verdict. Every number traces back to a real calculation in
    ``technical.py`` — nothing here is a guess.
    """
    from app.signals.technical import (
        atr, auto_fib_retracement, ema, ohlcv_to_dataframe, support_resistance_mtf,
    )

    try:
        df = ohlcv_to_dataframe(candles)
    except Exception as exc:  # noqa: BLE001 — context is never worth failing the chart for
        logger.warning("[Room] could not build dataframe for context: {}", exc)
        return {}

    out: dict[str, Any] = {}

    try:
        fib = auto_fib_retracement(df)
        out["fib_levels"] = [
            lv for lv in fib.get("levels") or [] if lv.get("ratio") in _FIB_DRAWN
        ]
        out["fib_golden_zone"] = fib.get("golden_zone")
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Room] fib skipped: {}", exc)

    price = float(df["close"].iloc[-1])
    try:
        sr = support_resistance_mtf(df)
        levels = sr.get("levels") or []
        # Nearest two on each side: the ones price is actually trading between.
        below = sorted(
            (l for l in levels if l["price"] < price), key=lambda l: price - l["price"]
        )[:2]
        above = sorted(
            (l for l in levels if l["price"] >= price), key=lambda l: l["price"] - price
        )[:2]
        out["support_zones"] = [{"low": l["zone_low"], "high": l["zone_high"]} for l in below]
        out["resistance_zones"] = [{"low": l["zone_low"], "high": l["zone_high"]} for l in above]
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Room] support/resistance skipped: {}", exc)

    try:
        out["ema"] = {
            period: [None if v != v else float(v) for v in ema(df["close"], period)]
            for period in (20, 50, 200)
            if len(df) >= period
        }
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Room] EMAs skipped: {}", exc)

    try:
        atr_val = float(atr(df, period=14).iloc[-1])
        if atr_val == atr_val and atr_val > 0:  # NaN-safe
            out["atr"] = atr_val
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Room] ATR skipped: {}", exc)

    return out


#: Minimum distance from entry to the FURTHEST take-profit, in pips. Gold at
#: 1 pip = 0.10 makes this 11.0 in price. A too-near final target is what has a
#: trade sitting open for hours to bank a few dollars and then handing them back
#: on the next swing — the ladder must at least reach far enough to be worth the
#: hold. Only the last rung is stretched; the near rungs still bank partials.
_MIN_FINAL_TP_PIPS = 110.0


def _extend_ladder_to_floor(
    targets: list[float], *, entry: float, is_long: bool, symbol: str,
) -> list[float]:
    """Ensure the furthest target clears the per-instrument minimum distance.

    The nearer rungs are left where they are — they are the partial-take levels,
    and moving them would break the R:R the seat sized them at. Only the final
    rung is pushed out to the floor when the whole ladder fell short of it, so a
    signal always has a target worth waiting for without inflating the ones in
    front of it.
    """
    # No symbol means no pip convention to floor against — leave the ladder as
    # given rather than stretch it by an arbitrary amount.
    if not targets or not entry or not symbol:
        return targets
    from app.services.market_data import pip_size

    floor = _MIN_FINAL_TP_PIPS * pip_size(symbol)
    ordered = sorted(targets, reverse=not is_long)
    furthest = ordered[-1]
    reach = (furthest - entry) if is_long else (entry - furthest)
    if reach >= floor:
        return targets
    ordered[-1] = entry + floor if is_long else entry - floor
    return ordered


def overlay_from_result(
    result: dict[str, Any],
    price: float,
    context: dict[str, Any] | None = None,
    symbol: str = "",
) -> PlanOverlay:
    """Read entry/stop/targets out of the agents' decisions.

    The executor quotes absolute prices and the signal generator quotes
    percentages; prefer the absolute numbers and only derive from percentages
    when that is all there is. Nothing is invented — a level the agents did not
    give simply stays None and goes undrawn.

    ``context`` carries the independently computed market structure (see
    :func:`technical_context`). Its fib/SR/EMA layers are drawn whatever the
    verdict, and its ATR supplies a stop and target when the agents committed
    to a direction but left the levels blank — a directional call with no risk
    on the chart is the one case where drawing nothing is worse than drawing
    the standard 1.5×/3× ATR frame the rest of the bot already uses.
    """
    decisions = result.get("decisions") or []
    action = str(result.get("final_action") or "hold").lower()
    executor = _decision(decisions, "trade_executor")
    signal = _decision(decisions, "signal_generator")

    # The signal generator now quotes an entry band and a ladder in absolute
    # prices. Prefer those: they are the levels the published card carries, and
    # a chart drawn from anything else would show the trader a different plan
    # from the one the message just gave them.
    zone = [z for z in (_num(v) for v in (signal.get("entry_zone") or [])) if z]
    signal_entry = sum(zone) / len(zone) if len(zone) >= 2 else None
    signal_targets = [t for t in (_num(v) for v in (signal.get("take_profits") or [])) if t]

    entry = _num(executor.get("entry")) or _num(executor.get("entry_price")) \
        or signal_entry or _num(signal.get("entry_price")) or (price or None)
    stop = _num(executor.get("stop_loss")) or _num(signal.get("stop_loss"))
    targets = [t for t in [_num(executor.get("take_profit"))] if t] or signal_targets

    # A HOLD verdict is the room declining to *add* risk, not a statement that
    # the chart has no shape. When the seats still lean a way, that direction
    # frames the levels — otherwise the chart goes out with an entry line and
    # nothing else on it, which tells a trader where to get in and never where
    # to get out.
    if action not in ("buy", "sell"):
        for seat in (signal, executor, _decision(decisions, "market_analyst")):
            leaning = str(seat.get("action") or "").lower()
            if leaning in ("buy", "sell"):
                action = leaning
                break
            if leaning in ("bullish", "bearish"):
                action = "buy" if leaning == "bullish" else "sell"
                break

    is_long = action == "buy"
    if entry and not stop and (sl_pct := _num(signal.get("stop_loss_pct"))):
        stop = entry * (1 - sl_pct / 100) if is_long else entry * (1 + sl_pct / 100)
    if entry and not targets and (tp_pct := _num(signal.get("take_profit_pct"))):
        targets = [entry * (1 + tp_pct / 100) if is_long else entry * (1 - tp_pct / 100)]

    context = context or {}
    # SL 1.5×ATR, TP 3×ATR — the same 2:1 frame mtf_cascade already sizes with.
    # Filled in per level rather than all-or-nothing: a plan that named targets
    # but no stop used to get neither, so the chart showed the reward and hid
    # the risk — the one asymmetry never worth drawing.
    if (atr_val := _num(context.get("atr"))) and entry and action in ("buy", "sell"):
        if not stop:
            stop = entry - atr_val * 1.5 if is_long else entry + atr_val * 1.5
        if not targets:
            # The first rung stays at 3× — 2:1 against the 1.5× stop, which is
            # the house minimum. Starting the ladder nearer would publish a
            # first target that breaks the risk rule the signal seat works to.
            targets = [
                entry + atr_val * step if is_long else entry - atr_val * step
                for step in (3.0, 4.5, 6.0)
            ]

    # The same floor the published card gets. Applied here too, and from the
    # same ATR, so the level drawn on the chart is the level in the message —
    # a plan whose picture and text disagree is worse than either alone.
    if entry and stop and action in ("buy", "sell") and atr_val:
        try:
            from app.trading import stop_quality

            assessment = stop_quality.assess(
                entry=entry, proposed_stop=stop, is_long=is_long, atr_value=atr_val,
            )
            if assessment and assessment.widened:
                stop = assessment.stop
        except Exception as exc:  # noqa: BLE001 — drawing must never fail on this
            logger.debug("[Room] stop-quality check skipped for the overlay: {}", exc)

    blocks: list[dict[str, Any]] = []
    if entry and stop:
        blocks.append({
            "low": min(entry, stop), "high": max(entry, stop),
            "kind": "bullish" if is_long else "bearish",
            "label": "Entry / risk zone",
        })

    # Stretch the furthest target to the minimum worth-holding distance, so a
    # gold plan never asks a trader to sit on a trade for an 8-point target.
    if targets and entry and action in ("buy", "sell"):
        targets = _extend_ladder_to_floor(
            targets, entry=entry, is_long=is_long, symbol=symbol,
        )

    projection: list[float] = []
    if entry and targets and action in ("buy", "sell"):
        final = targets[-1]
        projection = [entry + (final - entry) * f for f in (0.3, 0.62, 0.85, 1.0)]

    return PlanOverlay(
        direction="long" if is_long else "short" if action == "sell" else None,
        entry=entry, stop_loss=stop, take_profits=targets,
        order_blocks=blocks, projection=projection,
        ema=context.get("ema") or {},
        fib_levels=context.get("fib_levels") or [],
        fib_golden_zone=context.get("fib_golden_zone"),
        support_zones=context.get("support_zones") or [],
        resistance_zones=context.get("resistance_zones") or [],
    )


def _floored_stop(
    decision: dict[str, Any],
    *,
    entry: float | None,
    is_long: bool,
    candles: list[list] | None,
) -> dict[str, Any]:
    """The seat's plan with its stop pushed out to what the market justifies.

    A published stop closer to the entry than the pair's own hourly range is not
    protection — it is a level the next ordinary bar reaches. See
    :mod:`app.trading.stop_quality`; this is where that floor meets the message
    a trader actually copies.
    """
    if not entry or not candles:
        return decision
    try:
        from app.trading import stop_quality

        assessment = stop_quality.assess(
            entry=entry, proposed_stop=_num(decision.get("stop_loss")),
            is_long=is_long, candles=candles,
        )
    except Exception as exc:  # noqa: BLE001 — never block publishing on this
        logger.debug("[Room] stop-quality check skipped for the card: {}", exc)
        return decision
    if not assessment or not assessment.widened:
        return decision
    logger.info(
        "[Room] published stop widened {} → {:.6g} ({})",
        decision.get("stop_loss"), assessment.stop, assessment.reason,
    )
    return {**decision, "stop_loss": assessment.stop}


async def signal_card_for(
    result: dict[str, Any],
    symbol: str,
    overlay: PlanOverlay | None = None,
    candles: list[list] | None = None,
) -> str | None:
    """The Signal Generator's call, rendered as a publishable signal."""
    card = await built_card_for(result, symbol, overlay, candles)
    return signal_card_module().render(card) if card else None


def signal_card_module():
    from plugins.TelegramSignalNewsPlugin.backend.services import signal_card

    return signal_card


async def built_card_for(
    result: dict[str, Any],
    symbol: str,
    overlay: PlanOverlay | None = None,
    candles: list[list] | None = None,
):
    """The same card as a structure, so its levels can be acted on.

    Rendering was the only thing anyone could do with the published plan, which
    is why a signal could go out to the channel with no matching order anywhere:
    the numbers existed for exactly as long as it took to turn them into text.

    Returns None whenever there is nothing tradeable to publish — a HOLD, or a
    plan whose own levels contradict each other. The room's verdict message has
    already gone out by then, so silence here costs a trader nothing; a plan
    that cannot be filled as written costs them the trade.
    """
    from plugins.TelegramSignalNewsPlugin.backend.services import signal_card

    decision = _decision(result.get("decisions") or [], "signal_generator")
    if not decision:
        return None

    # The board and the card are read together, so they may not disagree. A
    # seat calling BUY under a room verdict of SELL is a debate to resolve
    # upstairs, not two contradictory instructions to hand a trader.
    side = str(decision.get("action") or "").lower()
    verdict = str(result.get("final_action") or "hold").lower()
    if verdict in ("buy", "sell") and side in ("buy", "sell") and side != verdict:
        logger.info(
            "[Room] signal card withheld — seat says {} under a {} verdict",
            side, verdict,
        )
        return None

    price = _num(result.get("price"))
    number = await _analysis_number()

    # Floor the seat's own ladder the same way the chart's is, so the copied
    # message and the drawn plan name the same furthest target rather than two.
    zone = [z for z in (_num(v) for v in (decision.get("entry_zone") or [])) if z]
    entry = (sum(zone) / len(zone)) if len(zone) >= 2 else _num(decision.get("entry_price"))
    raw_tps = [t for t in (_num(v) for v in (decision.get("take_profits") or [])) if t]
    if entry and raw_tps and side in ("buy", "sell"):
        decision = {
            **decision,
            "take_profits": _extend_ladder_to_floor(
                raw_tps, entry=entry, is_long=side == "buy", symbol=symbol,
            ),
        }
    if side in ("buy", "sell"):
        decision = _floored_stop(
            decision, entry=entry or price, is_long=side == "buy", candles=candles,
        )

    card = signal_card.build(
        decision, symbol=symbol, price=price, analysis_number=number,
    )
    if card is None and overlay is not None:
        # The seat had a direction but no levels — normal when the providers
        # are all rate-limited and it fell back to the local read. The chart is
        # already drawing a plan for exactly that case; publishing the same
        # levels means the picture and the message agree, instead of the trader
        # getting a chart full of lines and a message that says nothing.
        card = signal_card.build(
            {
                "action": decision.get("action"),
                # A single price, not a zero-width band: the card widens one
                # entry into something fillable, and a band of nothing is not.
                "entry_price": overlay.entry,
                "stop_loss": overlay.stop_loss,
                "take_profits": list(overlay.take_profits),
                "reaction_zone": _reaction_zone_from(overlay, decision),
            },
            symbol=symbol, price=price, analysis_number=number,
        )
    return card


async def trade_published_card(card, symbol: str, price: float | None = None) -> dict | None:
    """Put the card that just went out onto the accounts the room trades.

    Publishing and trading were two unconnected paths, so a signal on the
    channel did not imply an order anywhere — which makes the desk's record
    unverifiable in exactly the mode meant for verifying it. Best-effort: a
    broker problem must never stop the message the trader is waiting for.
    """
    if card is None:
        return None
    try:
        from app.agents import execution
        from app.core.database import AsyncSessionLocal

        is_long = str(card.side).lower() == "buy"
        # Size against where the market actually is, not the middle of a band
        # that price may already have left; the levels stay the published ones.
        entry = float(price or (card.entry_high if is_long else card.entry_low))
        async with AsyncSessionLocal() as db:
            return await execution.mirror_published_card(
                db, symbol=symbol, side=card.side, entry=entry,
                stop_loss=float(card.stop_loss), take_profits=list(card.take_profits),
            )
    except Exception as exc:  # noqa: BLE001 — the signal ships regardless
        logger.warning("[Room] could not mirror the published card for {}: {}", symbol, exc)
        return None


def _reaction_zone_from(overlay: PlanOverlay, decision: dict[str, Any]) -> dict | None:
    """The level to trade from on a pullback, taken from the drawn structure.

    The fib golden zone is where the chart says the retracement should hold, and
    the nearest opposing band is what it would then run at — the same two things
    a desk writes under "reaction area". Built only for a directional call, and
    only when the structure to build it from was actually found.
    """
    side = str(decision.get("action") or "").lower()
    if side not in ("buy", "sell"):
        return None
    zone = overlay.fib_golden_zone or {}
    low, high = _num(zone.get("low")), _num(zone.get("high"))
    if not (low and high):
        return None

    is_long = side == "buy"
    bands = overlay.resistance_zones if is_long else overlay.support_zones
    targets = [t for t in (_num((b or {}).get("high" if is_long else "low")) for b in bands) if t]
    if not targets and overlay.take_profits:
        targets = list(overlay.take_profits)
    if not targets:
        return None

    depth = (high - low) or max(high * 0.001, 1e-6)
    return {
        "side": side,
        "low": low,
        "high": high,
        "stop_loss": low - depth if is_long else high + depth,
        "take_profits": targets[:3],
        "note": (
            f"The {_fmt_level(low)} - {_fmt_level(high)} zone is the retracement "
            "the chart is holding above. Watching how price reacts there — a "
            "clear reaction and confirmation makes it the next high-probability "
            "entry; losing it invalidates this read."
        ),
    }


def _fmt_level(value: float) -> str:
    """Match the card's own number style, so one message reads as one voice."""
    return f"{value:.2f}".rstrip("0").rstrip(".")


async def _analysis_number() -> int | None:
    """Which analysis of the day this is, for the published heading.

    Counted from the journal rather than a counter of our own so a restart does
    not reset it mid-session. Best-effort: an unnumbered heading still reads
    fine, an exception here would cost the whole signal.
    """
    try:
        from datetime import datetime

        from sqlalchemy import func, select

        from app.core.database import AsyncSessionLocal
        from app.models.database import JarvisAnalysisJournal

        since = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        async with AsyncSessionLocal() as db:
            count = await db.scalar(
                select(func.count(JarvisAnalysisJournal.id))
                .where(JarvisAnalysisJournal.created_at >= since)
            )
        return int(count or 0) + 1
    except Exception as exc:  # noqa: BLE001 — the heading is decoration
        logger.debug("[Room] analysis number unavailable: {}", exc)
        return None


def format_result(result: dict[str, Any], symbol: str, timeframe: str) -> str:
    """Render the room's verdict as the Telegram HTML body.

    The markup is built here, so only the agent-supplied strings are escaped —
    running the finished text through a formatter would escape our own tags and
    the user would read literal ``&lt;b&gt;``.
    """
    from app.agents.orchestrator import reasoning_text

    def esc(value: Any, limit: int) -> str:
        # reasoning_text first: a model that answered with a nested object would
        # otherwise be rendered as a raw Python dict repr.
        return html.escape(reasoning_text(value).strip().replace("\n", " "))[:limit]

    if result.get("skipped"):
        return f"🏛 Room skipped <b>{esc(symbol, 40)}</b> — {esc(result.get('reason', 'unknown'), 80)}."

    action = str(result.get("final_action") or "hold").upper()
    icon = {"BUY": "🟢", "SELL": "🔴"}.get(action, "⚪")
    confidence = result.get("final_confidence") or 0
    try:
        conf_txt = f"{float(confidence) * 100:.0f}%" if float(confidence) <= 1 else f"{float(confidence):.0f}%"
    except (TypeError, ValueError):
        conf_txt = str(confidence)

    lines = [
        f"🏛 <b>Trading room — {esc(symbol, 40)} {esc(timeframe, 10)}</b>",
        f"{icon} Verdict: <b>{action}</b>  ·  confidence {conf_txt}",
        f"Agents: {result.get('agents_used', 0)}  ·  AI calls: {result.get('ai_calls', 0)}",
        "",
    ]

    for d in result.get("decisions") or []:
        name = esc(d.get("agent_name") or d.get("agent_role") or "agent", 40)
        act = esc(d.get("action") or "-", 20).upper()
        # One tight line per seat in the card; the full reasoning ships
        # separately via seat_reasoning_messages so nothing is chopped here.
        lines.append(f"• <b>{name}</b> — {act}: {esc(d.get('reasoning'), 220)}")

    if reasoning := esc(result.get("final_reasoning"), 600):
        lines += ["", f"<i>{reasoning}</i>"]

    if errors := result.get("errors"):
        lines += ["", f"⚠️ {len(errors)} agent error(s): {esc(errors[0], 140)}"]

    return "\n".join(lines)[:3900]


def seat_reasoning_messages(
    result: dict[str, Any], symbol: str, timeframe: str, limit: int = 3900
) -> list[str]:
    """The board's full reasoning, uncut, as follow-up message bodies.

    The verdict card keeps one summary line per seat; a trader acting on the
    call also needs what each seat actually said. Every field is rendered to
    its natural end — messages are split at sentence boundaries when a seat's
    reasoning would overflow Telegram's 4096-char ceiling, never mid-word.
    """
    from app.agents.orchestrator import reasoning_text

    def plain(value: Any) -> str:
        return html.unescape(reasoning_text(value)).strip()

    blocks: list[str] = []
    for d in result.get("decisions") or []:
        name = html.escape(plain(d.get("agent_name") or d.get("agent_role") or "Agent"))
        act = html.escape(str(d.get("action") or "-").upper())
        conf = d.get("confidence")
        try:
            conf_txt = f"{float(conf) * 100:.0f}%" if conf is not None else ""
        except (TypeError, ValueError):
            conf_txt = ""
        head = f"🪑 <b>{name}</b> — {act}" + (f" · {conf_txt}" if conf_txt else "")
        body = plain(d.get("reasoning"))
        if not body:
            continue
        blocks.append(f"{head}\n{html.escape(body)}")

    if ceo := plain(result.get("final_reasoning")):
        blocks.insert(0, f"🏛 <b>JARVIS — chair</b>\n{html.escape(ceo)}")

    if not blocks:
        return []

    header = f"📋 <b>Full meeting notes — {html.escape(str(symbol))} {html.escape(str(timeframe))}</b>"
    messages: list[str] = [header]
    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit - len(header) - 2:
            current = candidate
            continue
        # Overflow: split THIS block at sentence boundaries into the same slot.
        room = limit - len(current) - len("\n\n") if current else limit
        if room > 200 and current:
            first = _split_sentences(block, room)
            current = f"{current}\n\n{first}"
            block = block[len(first):].lstrip()
        if current:
            messages.append(current)
        # Remaining text of this block becomes fresh slots.
        while len(block) > limit:
            cut = _split_sentences(block, limit)
            messages.append(cut)
            block = block[len(cut):].lstrip()
        current = block or ""
    if current:
        messages.append(current)
    return messages


def _split_sentences(text: str, budget: int) -> str:
    """Longest prefix of ``text`` within ``budget`` that ends on a sentence
    boundary (or a hard cut when no boundary fits — never an infinite loop)."""
    if len(text) <= budget:
        return text
    window = text[:budget]
    for sep in (". ", "!\n", "?\n", ".\n", "! ", "? "):
        idx = window.rfind(sep)
        if idx > 0:
            return text[: idx + 1]
    # No sentence boundary inside the window — fall back to whitespace, then hard.
    sp = window.rfind(" ")
    return text[: sp if sp > int(budget * 0.5) else budget].rstrip()


def plan_levels_text(overlay: PlanOverlay | None) -> str:
    """The drawn levels, written out, so the message and the chart agree."""
    if overlay is None or not overlay.entry:
        return ""
    parts = [f"Entry <b>{overlay.entry:.6g}</b>"]
    if overlay.stop_loss:
        parts.append(f"SL <b>{overlay.stop_loss:.6g}</b>")
    for i, tp in enumerate(overlay.take_profits, start=1):
        parts.append(f"TP{i} <b>{tp:.6g}</b>")
    # Deliberately not "Key Levels": the structural read above this already
    # carries a block under that heading, and two of them in one message reads
    # as a formatting fault rather than as two different things.
    out = "🎯 <b>The plan on the chart</b>\n" + "  ·  ".join(parts)
    if zone := overlay.fib_golden_zone:
        out += f"\nFib golden zone {zone['low']:.6g} – {zone['high']:.6g}"
    if overlay.support_zones:
        s = overlay.support_zones[0]
        out += f"\nNearest support {s['low']:.6g} – {s['high']:.6g}"
    if overlay.resistance_zones:
        r = overlay.resistance_zones[0]
        out += f"\nNearest resistance {r['low']:.6g} – {r['high']:.6g}"
    return out


async def cycle_text(symbol: str, result: dict) -> str:
    """The Bitcoin 1064-day calendar + whale flow, as /room reads it.

    The season and the big money — the same snapshot the seats argued from
    (``context["btc_cycle"]`` / ``context["btc_whales"]``) — so the Telegram
    reader can judge the verdict against the regime it was made in.
    """
    ctx_cycle = result.get("btc_cycle") if isinstance(result.get("btc_cycle"), dict) else {}
    ctx_whales = result.get("btc_whales") if isinstance(result.get("btc_whales"), dict) else {}
    lines: list[str] = []

    if ctx_cycle.get("phase"):
        emoji = "🟢" if ctx_cycle["phase"] == "bull" else "🔴"
        lines.append(
            f"{emoji} <b>BTC cycle: {str(ctx_cycle['phase']).upper()}</b> — day "
            f"{ctx_cycle.get('day_of_cycle', '?')} since the {ctx_cycle.get('anchor', '?')} bottom"
        )
        top, bottom = ctx_cycle.get("projected_top"), ctx_cycle.get("projected_bottom")
        dtt, dtb = ctx_cycle.get("days_to_top"), ctx_cycle.get("days_to_bottom")
        if top and dtt is not None:
            lines.append(f"Projected top {top} ({dtt}d) · projected bottom {bottom} ({dtb}d)")
        if ctx_cycle.get("late_phase"):
            lines.append("⚠️ Caution window — a phase turn is close on the calendar")
    elif symbol.upper().startswith(("BTC", "ETH", "SOL")):
        lines.append("BTC cycle: unavailable right now")

    if ctx_whales.get("status") == "OK" and ctx_whales.get("applicable", True):
        score = str(ctx_whales.get("score", "")).upper()
        emoji = {"ACCUMULATING": "🟢", "DISTRIBUTING": "🔴", "NEUTRAL": "⚪️"}.get(score, "⚪️")
        lines.append(f"🐋 Whales: <b>{score or 'NO READ'}</b> — {ctx_whales.get('detail', '')}".rstrip())

    if not lines:
        return ""
    return "\n".join(lines)


async def market_read_text(symbol: str, timeframe: str, candles: list[list]) -> str:
    """The structural read of ``symbol`` — the same voice every surface uses.

    Built from the candles the chart was drawn from, so the words under the
    picture describe the picture rather than a second, later snapshot.
    """
    from app.signals.narrative import narrative_summary

    try:
        highs = [float(c[2]) for c in candles[-20:]]
        lows = [float(c[3]) for c in candles[-20:]]
        closes = [float(c[4]) for c in candles]
        ema50 = sum(closes[-50:]) / len(closes[-50:])
        trend = (
            "uptrend" if closes[-1] > ema50 * 1.001
            else "downtrend" if closes[-1] < ema50 * 0.999
            else "ranging"
        )
        return narrative_summary(
            candles, symbol=symbol, timeframe=timeframe, trend=trend,
            swing_high=max(highs), swing_low=min(lows),
        )
    except Exception as exc:  # noqa: BLE001 — prose never gates the verdict
        logger.debug("[Room] market read skipped for {}: {}", symbol, exc)
        return ""


# ── Jobs ─────────────────────────────────────────────────────────────────────

#: Enough history for a 200-period average to exist. Below this the EMA200
#: abstains from the bias tally and the fib swing is drawn from a shorter
#: window — the read stays honest either way, but it is thinner than it needs
#: to be when the extra bars cost one request.
_CANDLE_LIMIT = 220


async def _fetch_candles(symbol: str, timeframe: str, limit: int = _CANDLE_LIMIT) -> list[list] | None:
    """Candles for ``symbol`` from whichever feed actually covers it.

    Delegates to the shared resolver, which walks every source in the app —
    Yahoo (with CME volume, anchored to Swissquote for FX and metals), the
    forex provider, the credentialed exchanges, keyless public exchanges — and
    folds a finer timeframe up when the requested one is unserved. One path
    failing used to end the chart with "no candles available" on a pair that
    was trading perfectly well; now that message can only mean every source
    declined the symbol.
    """
    from app.services import candles as candle_source

    rows = await candle_source.fetch(symbol, timeframe, limit)
    return rows or None


async def _journal_plan(
    symbol: str, timeframe: str, overlay: PlanOverlay, result: dict[str, Any], price: float
) -> None:
    """Record the room's plan so its own calls can be followed up later.

    Without this the room could publish a level and never learn whether it
    held — the follow-up ("the zone we mapped is holding") has to be measured
    against something we actually wrote down at the time, not reconstructed
    afterwards from a chart that has since moved.
    """
    if not (overlay.entry and overlay.stop_loss and overlay.take_profits and overlay.direction):
        return
    try:
        from app.core.database import AsyncSessionLocal
        from app.services import analysis_journal, market_data

        async with AsyncSessionLocal() as db:
            await analysis_journal.record_proposal(
                db,
                source="trading_room",
                symbol=symbol,
                asset_class=market_data.classify(symbol),
                timeframe=timeframe,
                side=overlay.direction,
                entry=overlay.entry,
                stop_loss=overlay.stop_loss,
                take_profit=overlay.take_profits[0],
                tp2=overlay.take_profits[1] if len(overlay.take_profits) > 1 else None,
                confidence=result.get("final_confidence"),
                price_at_analysis=price,
                features={"verdict": result.get("final_action")},
            )
    except Exception as exc:  # noqa: BLE001 — a lost row must not cost the chart
        logger.debug("[Room] plan not journalled for {}: {}", symbol, exc)


async def room_plan(
    symbol: str, timeframe: str, result: dict[str, Any], price: float | None = None
) -> tuple[PlanOverlay | None, bytes | None]:
    """The room's plan for ``symbol`` as both levels and a drawn chart.

    Both come out of one call deliberately: a caller that describes the levels
    in words and shows the chart alongside must not compute them twice, or the
    text and the picture can quote different stops for the same analysis.
    """
    candles = await _fetch_candles(symbol, timeframe)
    if not candles:
        return None, None

    price = float(price or result.get("price") or candles[-1][4] or 0)
    overlay = overlay_from_result(result, price, technical_context(candles), symbol=symbol)
    chart = render_plan_chart(
        candles, symbol=symbol, timeframe=timeframe, overlay=overlay,
        subtitle=f"room verdict {str(result.get('final_action') or 'hold').upper()}",
    )
    await _journal_plan(symbol, timeframe, overlay, result, price)
    return overlay, chart


async def build_room_chart(
    symbol: str, timeframe: str, result: dict[str, Any], price: float | None = None
) -> bytes | None:
    """Just the chart, for callers with nothing to say about the levels."""
    _overlay, chart = await room_plan(symbol, timeframe, result, price)
    return chart


async def run_pair(token: str, chat_id: str, symbol: str, timeframe: str) -> None:
    """Convene the room on ``symbol`` and reply with the verdict and a chart."""
    from app.agents.orchestrator import AgentOrchestrator
    from app.core.database import AsyncSessionLocal
    from plugins.TelegramSignalNewsPlugin.backend.services import bot_service

    logger.info("[Room] Telegram requested {} {}", symbol, timeframe)
    async with AsyncSessionLocal() as db:
        # trigger="telegram" deliberately: the focus lock only gates the
        # automated triggers, so a pair the user explicitly asked for is never
        # silently ignored because another pair happens to be pinned.
        result = await AgentOrchestrator.analyze_symbol(
            db, symbol, timeframe=timeframe, trigger="telegram"
        )

        # How the plans we already published for this pair are tracking. Read
        # before the verdict goes out so a follow-up leads the message rather
        # than arriving as an afterthought under a fresh call.
        follow_up = ""
        try:
            from app.services.scenario_tracker import scenario_narrative, track_symbol

            follow_up = scenario_narrative(await track_symbol(db, symbol))
        except Exception as exc:  # noqa: BLE001 — history never gates the verdict
            logger.debug("[Room] scenario follow-up skipped for {}: {}", symbol, exc)
            from app.core.database import safe_rollback

            await safe_rollback(db)

    await bot_service.send_message(token, chat_id, format_result(result, symbol, timeframe))

    # Full uncut meeting notes — the card above is the verdict, this is what
    # every seat actually said. Split at sentence boundaries across messages.
    try:
        for note in seat_reasoning_messages(result, symbol, timeframe):
            await bot_service.send_message(token, chat_id, note)
    except Exception as exc:  # noqa: BLE001 — notes must never kill the chart flow
        logger.debug("[Room] seat reasoning notes skipped: {}", exc)

    candles = await _fetch_candles(symbol, timeframe)
    if not candles:
        await bot_service.send_message(
            token, chat_id,
            f"📉 Every price feed declined <b>{symbol}</b> {timeframe} just now "
            "— the verdict above stands, the chart could not be drawn. "
            "Try again shortly or ask for a different timeframe.",
        )
        return

    price = float(result.get("price") or candles[-1][4] or 0)
    overlay = overlay_from_result(result, price, technical_context(candles), symbol=symbol)
    chart = render_plan_chart(
        candles, symbol=symbol, timeframe=timeframe, overlay=overlay,
        subtitle=f"room verdict {str(result.get('final_action') or 'hold').upper()}",
    )
    await _journal_plan(symbol, timeframe, overlay, result, price)

    # The board says what the desk thinks; this says what to do about it. Sent
    # as its own message so it can be copied without the commentary around it,
    # and built after the overlay so it publishes the levels the chart draws
    # rather than a second set of its own.
    if built := await built_card_for(result, symbol, overlay, candles=candles):
        await bot_service.send_message(token, chat_id, signal_card_module().render(built))
        if report := await trade_published_card(built, symbol, price):
            await bot_service.send_message(token, chat_id, _execution_note(report))

    # The structural read, the season, the levels drawn on the chart, and any
    # follow-up — one message, so the words and the picture describe the same
    # analysis.
    blocks = [b for b in (
        await market_read_text(symbol, timeframe, candles),
        await cycle_text(symbol, result),
        plan_levels_text(overlay),
        follow_up,
    ) if b]
    if blocks:
        await bot_service.send_message(token, chat_id, "\n\n".join(blocks)[:3900])

    if chart:
        await bot_service.send_photo(
            token, chat_id, chart, caption=f"{symbol} {timeframe} — trading room plan"
        )


def _execution_note(report: dict) -> str:
    """One line on what happened to the order — where it went, or why it did not.

    Silence here is what made "is the desk actually trading this?" unanswerable
    without opening the terminal.
    """
    status = str(report.get("status") or "")
    reason = html.escape(str(report.get("reason") or ""))
    if status == "placed":
        return f"✅ <b>Taken</b> — {reason}"
    if status == "error":
        return f"⚠️ <b>Order failed</b> — {reason}"
    return f"⏸ <b>Not taken</b> — {reason}"


async def run_context(
    token: str,
    chat_id: str,
    question: str,
    *,
    image: tuple[bytes, str] | None = None,
) -> None:
    """Analyse a question and/or an image, escalating to the room for a pair.

    An image is read first: what the chart actually shows is what decides which
    market the room then convenes on, so a screenshot alone is enough to get a
    full agent review of that pair.
    """
    from app.core.database import AsyncSessionLocal
    from plugins.AiMarketAnalyst.backend.services import chart_annotate
    from plugins.AiMarketAnalyst.backend.services.ai_router import chat_for_task
    from plugins.AiMarketAnalyst.backend.services.vision import DEFAULT_CHART_PROMPT, read_image
    from plugins.TelegramSignalNewsPlugin.backend.services import bot_service
    vision = None
    if image is not None:
        img_bytes, mime = image
        async with AsyncSessionLocal() as db:
            vision = await read_image(
                img_bytes, mime, question or DEFAULT_CHART_PROMPT, db,
                source="telegram", agent_name="room-vision",
            )
        if vision is None:
            await bot_service.send_message(
                token, chat_id, "❌ I couldn't read that image — try resending it.")
            return
        if overlay := chart_annotate.annotate(img_bytes, vision.findings):
            await bot_service.send_photo(
                token, chat_id, overlay,
                caption=str(vision.findings.get("instrument") or "Marked-up chart"),
            )

    # A pair the image or the question names is worth the room's full attention.
    symbol = None
    if vision is not None:
        from app.services import market_data

        # The model is told to answer with the trading symbol, but it reads a
        # title for a living — normalise so "XAU/USD" or "GOLD" still resolves
        # to the pair our feeds price.
        raw_symbol = str(vision.findings.get("instrument") or "").strip()
        symbol = (market_data.normalize_symbol(raw_symbol) or raw_symbol) or None
    if not symbol:
        symbol, _tf, _rest = parse_args(question)

    if symbol:
        timeframe = "1h"
        if vision is not None:
            timeframe = normalize_timeframe(vision.findings.get("timeframe"))
        await bot_service.send_message(
            token, chat_id, f"🏛 Convening the room on <b>{symbol}</b> {timeframe}…")
        await run_pair(token, chat_id, symbol, timeframe)
        return

    # No tradable pair — answer the question itself with the deep model, giving
    # it whatever the image showed as observed fact.
    prompt = question or "Analyse this."
    if vision is not None:
        prompt = (
            "A vision model read an image the user sent as follows:\n\n"
            f"{vision.narrative}\n\n"
            f"Their question: {question or 'Analyse it as a trading chart.'}"
        )
    async with AsyncSessionLocal() as db:
        res = await chat_for_task(
            db,
            [{"role": "user", "content": prompt}],
            task="deep_reasoning", max_tokens=3000, temperature=0.4,
            agent_name="room-telegram", source="telegram",
        )
    body = str(res.get("content") or "").strip() if res.get("ok") else ""
    await bot_service.send_message(
        token, chat_id,
        format_for_telegram(body, limit=3900) if body
        else "❌ The room could not produce an answer — every model failed.",
    )
