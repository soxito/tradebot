"""How the plans we published are actually tracking, in the room's own voice.

The journal already records every proposal and settles it against real candles.
What it could not do is answer the question a reader actually asks the day
after: *did the level we called hold, and how much of the move we mapped has
happened?* That is what this adds — progress measured as the share of the
entry-to-target distance price has already covered.

The tone follows the data, never the other way round. A plan running to
schedule is allowed to sound pleased; one that was stopped out says so plainly,
and one price never reached is reported as untriggered rather than quietly
dropped. Claiming a call worked when it did not is the single most damaging
thing this file could do, because the reader would size the next trade on it.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Optional, Sequence

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import SAST, now_sast
from app.models.database import JarvisAnalysisJournal

#: Plans older than this are history, not "the scenario we mapped out".
DEFAULT_LOOKBACK_HOURS = 96

#: Below this share of the move, "price reacted at our level" is wishful: a
#: touch that goes nowhere is a touch, not a reaction.
_REACTION_FLOOR_PCT = 12.0


def _pct(value: float) -> str:
    return f"{value:.0f}%"


def _fmt(value: float) -> str:
    a = abs(value)
    if a >= 1000:
        return f"{value:,.2f}"
    if a >= 1:
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return f"{value:.6f}".rstrip("0").rstrip(".")


def progress_of(
    row: JarvisAnalysisJournal, ohlcv: Sequence[Sequence[float]]
) -> Optional[Dict[str, Any]]:
    """What has happened to one published plan since it was made.

    Returns None when the candles cannot place it at all — an unknown outcome
    must not be reported as a neutral one.
    """
    from app.services.analysis_journal import _to_bars

    entry, stop, target = float(row.entry), float(row.stop_loss), float(row.take_profit)
    span = abs(target - entry)
    if span <= 0:
        return None

    created_ts = int(row.created_at.replace(tzinfo=SAST).timestamp())
    bars = [b for b in _to_bars(ohlcv) if b.time >= created_ts]
    if not bars:
        return None

    is_long = row.side == "long"

    # Settled rows already carry the verdict; re-deriving it from a fresh candle
    # window would let a later reversal quietly rewrite a recorded loss.
    if row.outcome in ("win", "loss", "break_even", "no_fill", "expired"):
        done = {
            "win": 100.0, "loss": 0.0, "break_even": 0.0,
            "no_fill": 0.0, "expired": None,
        }[row.outcome]
        pct = done if done is not None else _travelled(row, bars, is_long, entry, span)
        return {
            "symbol": row.symbol, "side": row.side, "timeframe": row.timeframe,
            "entry": entry, "stop_loss": stop, "take_profit": target,
            "status": row.outcome, "filled": row.outcome not in ("no_fill",),
            "pct_complete": round(min(max(pct or 0.0, 0.0), 100.0), 1),
            "settled": True,
        }

    filled = any(
        (b.low <= entry if is_long else b.high >= entry) for b in bars
    )
    if not filled:
        return {
            "symbol": row.symbol, "side": row.side, "timeframe": row.timeframe,
            "entry": entry, "stop_loss": stop, "take_profit": target,
            "status": "waiting", "filled": False, "pct_complete": 0.0,
            "settled": False,
        }

    pct = _travelled(row, bars, is_long, entry, span)
    breached = any((b.low <= stop if is_long else b.high >= stop) for b in bars)
    return {
        "symbol": row.symbol, "side": row.side, "timeframe": row.timeframe,
        "entry": entry, "stop_loss": stop, "take_profit": target,
        "status": "invalidated" if breached else "running",
        "filled": True,
        "pct_complete": round(min(max(pct, 0.0), 100.0), 1),
        "settled": False,
    }


def _travelled(row, bars, is_long: bool, entry: float, span: float) -> float:
    """Share of the entry-to-target distance price has covered, at its best."""
    best = 0.0
    for b in bars:
        best = max(best, (b.high - entry) if is_long else (entry - b.low))
    return best / span * 100.0


async def recent_plans(
    db: AsyncSession,
    symbol: str,
    *,
    hours: int = DEFAULT_LOOKBACK_HOURS,
    limit: int = 3,
) -> List[JarvisAnalysisJournal]:
    """The plans published for ``symbol`` recently, newest first."""
    cutoff = now_sast() - timedelta(hours=hours)
    rows = await db.execute(
        select(JarvisAnalysisJournal)
        .where(
            JarvisAnalysisJournal.symbol == symbol.upper(),
            JarvisAnalysisJournal.created_at >= cutoff,
        )
        .order_by(JarvisAnalysisJournal.created_at.desc())
        .limit(limit)
    )
    return list(rows.scalars().all())


async def track_symbol(
    db: AsyncSession, symbol: str, *, hours: int = DEFAULT_LOOKBACK_HOURS, limit: int = 3
) -> List[Dict[str, Any]]:
    """Progress on every recent plan for ``symbol``. Never raises."""
    from app.services import market_data

    try:
        rows = await recent_plans(db, symbol, hours=hours, limit=limit)
    except Exception as exc:  # noqa: BLE001 — history is context, never a gate
        logger.debug("[Scenario] could not read plans for {}: {}", symbol, exc)
        return []
    if not rows:
        return []

    out: List[Dict[str, Any]] = []
    for row in rows:
        try:
            ohlcv, _ticker = await market_data.fetch_ohlcv_universal(
                row.symbol, timeframe=row.timeframe or "1h", limit=200
            )
            if state := progress_of(row, ohlcv):
                out.append(state)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[Scenario] {} progress skipped: {}", row.symbol, exc)
    return out


def scenario_narrative(states: Sequence[Dict[str, Any]]) -> str:
    """The follow-up on plans already published, or "" when there are none."""
    if not states:
        return ""

    lines: List[str] = []
    for state in states:
        symbol = state["symbol"]
        side = "long" if state["side"] == "long" else "short"
        pct = state["pct_complete"]
        status = state["status"]

        if status == "waiting":
            lines.append(
                f"⏳ {symbol}: price has not yet returned to our {side} entry at "
                f"{_fmt(state['entry'])}. The plan is still untriggered — nothing "
                "to claim either way."
            )
        elif status == "win":
            lines.append(
                f"✔️✔️ {symbol}: the {side} we mapped ran the full distance to "
                f"{_fmt(state['take_profit'])}. 100% of the plan completed."
            )
        elif status in ("loss", "invalidated"):
            lines.append(
                f"❌ {symbol}: price went through our invalidation at "
                f"{_fmt(state['stop_loss'])}. That scenario is dead — the level "
                "did not hold, and the next read starts from scratch."
            )
        elif status in ("break_even", "expired", "no_fill"):
            lines.append(
                f"➖ {symbol}: the {side} plan closed out at "
                f"{_pct(pct)} of the mapped move without resolving either way."
            )
        elif pct >= _REACTION_FLOOR_PCT:
            lines.append(
                f"✔️ {symbol} has reached and reacted at the zone we mapped at "
                f"{_fmt(state['entry'])}, and is following the scenario.\n"
                f"🎯 {_pct(pct)} of the plan has now been completed, with "
                f"{_fmt(state['take_profit'])} still the target."
            )
        else:
            lines.append(
                f"👁‍🗨 {symbol}: our {side} from {_fmt(state['entry'])} has "
                f"triggered but has only covered {_pct(pct)} of the mapped move. "
                "Too early to call it either way."
            )

    # The room journals a plan on every run, so the same untriggered scenario is
    # read back two or three times and rendered into identical sentences. Three
    # copies of "price has not yet returned to our entry" carry no more than one
    # and read like a stuck process, so only distinct lines are kept — in the
    # order they were first told.
    return "\n\n".join(dict.fromkeys(lines))
