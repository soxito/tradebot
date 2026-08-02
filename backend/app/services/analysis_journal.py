"""JARVIS's record of its own trade calls, and what price did next.

Why
---
The assistant produced confident proposals — "BUY XAUUSD at 4080, stop 4050,
target 4140, 72% confidence" — and then never found out whether any of them
worked. ``agent_decisions`` has outcome columns, but only a manual API call
ever fills them, so the stated confidence was a number the model made up and
was never held to.

This closes that loop the same way ``smc_memory`` already does for the MT5/SMC
path: record every proposal, let a background loop settle it against real
candles, then put the realised hit rate back in front of the model. The
feedback is deliberately about *calibration* rather than a weights table —
"your 80%-confidence FX calls actually win 52% of the time" is something a
model can act on and a human can check, whereas a tuned coefficient is neither.

Settlement is pessimistic by construction: an entry price never reaches is
``no_fill`` rather than a quiet drop, and a bar that spans both stop and target
resolves as a loss. Assuming the favourable fill is how backtests flatter
themselves, and a flattering memory is worse than none.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import SAST, now_sast
from app.models.database import JarvisAnalysisJournal

#: Below this many settled proposals, a win rate is noise. Reporting "60% over
#: 3 trades" would make the model more confidently wrong, not less.
MIN_SAMPLES = 8

#: A proposal that has neither filled nor resolved by now is closed out.
DEFAULT_EXPIRY_HOURS = 72

#: The injected block is capped hard — memory must not crowd out live data.
_MEMORY_BLOCK_CHARS = 800

_CONFIDENCE_BUCKETS = ((0.8, "80%+"), (0.7, "70–80%"), (0.6, "60–70%"), (0.0, "<60%"))


@dataclass(frozen=True)
class _Bar:
    """Minimal candle shape ``smc_memory.compute_excursions`` expects."""

    time: int
    open: float
    high: float
    low: float
    close: float


def _to_bars(ohlcv: Sequence[Sequence[float]]) -> List[_Bar]:
    bars: List[_Bar] = []
    for row in ohlcv:
        try:
            ts = int(row[0])
            bars.append(
                _Bar(
                    time=ts // 1000 if ts > 1e11 else ts,
                    open=float(row[1]), high=float(row[2]),
                    low=float(row[3]), close=float(row[4]),
                )
            )
        except (IndexError, TypeError, ValueError):
            continue
    return bars


# ── Recording ────────────────────────────────────────────────────────────────

async def record_proposal(
    db: AsyncSession,
    *,
    source: str,
    symbol: str,
    side: str,
    entry: float,
    stop_loss: float,
    take_profit: float,
    asset_class: str | None = None,
    timeframe: str = "4h",
    tp2: float | None = None,
    rr1: float | None = None,
    confidence: float | None = None,
    price_at_analysis: float | None = None,
    price_source: str | None = None,
    features: Dict[str, Any] | None = None,
) -> Optional[int]:
    """Persist one proposal. Best-effort — never propagates.

    Journalling must not be able to break the answer the user asked for: a
    failure here costs one row of future learning, whereas a raised exception
    would cost them their analysis.
    """
    try:
        row = JarvisAnalysisJournal(
            source=source,
            symbol=symbol.upper(),
            asset_class=asset_class,
            timeframe=timeframe,
            side="long" if str(side).lower() in ("long", "buy") else "short",
            entry=float(entry),
            stop_loss=float(stop_loss),
            take_profit=float(take_profit),
            tp2=float(tp2) if tp2 is not None else None,
            rr1=float(rr1) if rr1 is not None else None,
            confidence=float(confidence) if confidence is not None else None,
            price_at_analysis=(
                float(price_at_analysis) if price_at_analysis is not None else None
            ),
            price_source=price_source,
            features=json.dumps(features) if features else None,
        )
        db.add(row)
        await db.commit()
        return row.id
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Journal] record skipped for {}: {}", symbol, exc)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


# ── Settlement ───────────────────────────────────────────────────────────────

async def unsettled(
    db: AsyncSession, *, limit: int = 40, older_than_s: int = 1800
) -> List[JarvisAnalysisJournal]:
    """Proposals still awaiting an outcome, oldest first.

    ``older_than_s`` skips very recent rows: a proposal made a minute ago has no
    bars after it to settle against, so checking it just burns an API call.
    """
    # SAST, matching the column default — comparing against a UTC clock would
    # make every row look two hours in the future and settle nothing.
    cutoff = now_sast() - timedelta(seconds=older_than_s)
    rows = await db.execute(
        select(JarvisAnalysisJournal)
        .where(
            JarvisAnalysisJournal.outcome.is_(None),
            JarvisAnalysisJournal.created_at <= cutoff,
        )
        .order_by(JarvisAnalysisJournal.created_at.asc())
        .limit(limit)
    )
    return list(rows.scalars().all())


def evaluate(
    row: JarvisAnalysisJournal,
    ohlcv: Sequence[Sequence[float]],
    *,
    expiry_hours: int = DEFAULT_EXPIRY_HOURS,
    now: datetime | None = None,
) -> Optional[Dict[str, Any]]:
    """Decide a proposal's outcome from the candles after it was made.

    Returns ``None`` when it is still legitimately running — the row stays
    unsettled and is retried next pass, which is the same contract
    ``smc_memory.settle_signal`` uses.
    """
    from plugins.MT5TradingPlugin.backend.services.smc_memory import compute_excursions

    now = now or now_sast()
    # created_at is naive SAST; attach the offset before converting to epoch
    # so it lines up with the exchange timestamps on the candles.
    created_ts = int(row.created_at.replace(tzinfo=SAST).timestamp())
    expired = (now - row.created_at) >= timedelta(hours=expiry_hours)

    bars = [b for b in _to_bars(ohlcv) if b.time >= created_ts]
    if not bars:
        # No data yet. Only give up once the window has fully elapsed, otherwise
        # a temporary feed gap would be recorded as a real result.
        if expired:
            return {
                "outcome": "expired", "exit_reason": "no_data",
                "outcome_r": None, "mfe": None, "mae": None,
                "exit_price": None, "bars_to_outcome": 0,
            }
        return None

    is_long = row.side == "long"
    fill_index: Optional[int] = None
    for i, bar in enumerate(bars):
        if (is_long and bar.low <= row.entry) or (not is_long and bar.high >= row.entry):
            fill_index = i
            break

    if fill_index is None:
        # Price never came back to the proposed entry. Not a loss — but not a
        # success either, and counting it as neither would hide a real failure
        # mode, so it gets its own bucket.
        if expired:
            return {
                "outcome": "no_fill", "exit_reason": "no_fill",
                "outcome_r": None, "mfe": None, "mae": None,
                "exit_price": None, "bars_to_outcome": len(bars),
            }
        return None

    after = bars[fill_index:]
    ex = compute_excursions(
        side="buy" if is_long else "sell",
        entry=float(row.entry),
        stop_loss=float(row.stop_loss),
        take_profit=float(row.take_profit),
        candles=after,
        fill_time=after[0].time,
    )

    reason = ex.get("exit_reason")
    if reason == "tp":
        outcome = "win"
    elif reason == "sl":
        outcome = "loss"
    elif expired:
        r = float(ex.get("r_multiple") or 0.0)
        outcome = "win" if r > 0.1 else "loss" if r < -0.1 else "break_even"
        reason = "expiry"
    else:
        return None  # still running inside its window

    return {
        "outcome": outcome,
        "outcome_r": ex.get("r_multiple"),
        "mfe": ex.get("mfe"),
        "mae": ex.get("mae"),
        "exit_price": ex.get("exit_price"),
        "exit_reason": reason,
        "bars_to_outcome": len(after),
    }


async def settle(
    db: AsyncSession, row: JarvisAnalysisJournal, verdict: Dict[str, Any]
) -> None:
    """Write an outcome. Idempotent — an already-settled row is left alone."""
    if row.outcome is not None:
        return
    for field, value in verdict.items():
        setattr(row, field, value)
    row.settled_at = now_sast()
    await db.commit()


# ── Learning ─────────────────────────────────────────────────────────────────

def _bucket(confidence: Optional[float]) -> str:
    conf = float(confidence or 0.0)
    for floor, label in _CONFIDENCE_BUCKETS:
        if conf >= floor:
            return label
    return "<60%"


async def learned_stats(
    db: AsyncSession,
    *,
    symbols: Sequence[str] | None = None,
    asset_class: str | None = None,
) -> Dict[str, Any]:
    """Realised performance of settled proposals, bucketed by stated confidence."""
    query = select(JarvisAnalysisJournal).where(
        JarvisAnalysisJournal.outcome.isnot(None)
    )
    if symbols:
        query = query.where(
            JarvisAnalysisJournal.symbol.in_([s.upper() for s in symbols])
        )
    if asset_class:
        query = query.where(JarvisAnalysisJournal.asset_class == asset_class)
    rows = list((await db.execute(query)).scalars().all())

    # no_fill and expired-without-data never became trades, so they cannot win
    # or lose — they are counted and reported separately instead.
    scored = [r for r in rows if r.outcome in ("win", "loss", "break_even")]
    no_fill = sum(1 for r in rows if r.outcome == "no_fill")
    expired = sum(1 for r in rows if r.outcome == "expired")

    wins = sum(1 for r in scored if r.outcome == "win")
    r_values = [float(r.outcome_r) for r in scored if r.outcome_r is not None]

    buckets: Dict[str, Dict[str, Any]] = {}
    grouped: Dict[str, List[JarvisAnalysisJournal]] = defaultdict(list)
    for row in scored:
        grouped[_bucket(row.confidence)].append(row)
    for label, group in grouped.items():
        group_wins = sum(1 for r in group if r.outcome == "win")
        group_r = [float(r.outcome_r) for r in group if r.outcome_r is not None]
        buckets[label] = {
            "n": len(group),
            "win_rate": round(group_wins / len(group), 3) if group else 0.0,
            "avg_r": round(sum(group_r) / len(group_r), 3) if group_r else 0.0,
        }

    return {
        "settled": len(scored),
        "total": len(rows),
        "wins": wins,
        "losses": sum(1 for r in scored if r.outcome == "loss"),
        "win_rate": round(wins / len(scored), 3) if scored else 0.0,
        "avg_r": round(sum(r_values) / len(r_values), 3) if r_values else 0.0,
        "no_fill": no_fill,
        "expired": expired,
        "by_confidence": buckets,
        "by_macro": _macro_buckets(scored),
    }


#: A macro reading below this is a shrug, not a stance — bucketing it either way
#: would put coin-flips in both columns and wash the signal out.
_MACRO_ALIGNMENT_FLOOR = 0.1


def _macro_buckets(rows: Sequence[JarvisAnalysisJournal]) -> Dict[str, Dict[str, Any]]:
    """Did the macro read predict anything? Win rate split by its stance.

    This is what turns the macro weight from a number someone chose into one
    the record earns: if "macro backed it" does not out-perform "macro opposed
    it" over enough trades, that is evidence the factor is not worth its weight.
    """
    grouped: Dict[str, List[JarvisAnalysisJournal]] = defaultdict(list)
    for row in rows:
        try:
            features = json.loads(row.features) if row.features else {}
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(features, dict) or not features.get("macro_applied"):
            grouped["not_applied"].append(row)
            continue
        aligned = float(features.get("macro_aligned") or 0.0)
        if aligned > _MACRO_ALIGNMENT_FLOOR:
            grouped["supported"].append(row)
        elif aligned < -_MACRO_ALIGNMENT_FLOOR:
            grouped["opposed"].append(row)
        else:
            grouped["neutral"].append(row)

    out: Dict[str, Dict[str, Any]] = {}
    for label, group in grouped.items():
        group_wins = sum(1 for r in group if r.outcome == "win")
        group_r = [float(r.outcome_r) for r in group if r.outcome_r is not None]
        out[label] = {
            "n": len(group),
            "win_rate": round(group_wins / len(group), 3) if group else 0.0,
            "avg_r": round(sum(group_r) / len(group_r), 3) if group_r else 0.0,
        }
    return out


def memory_block(stats: Dict[str, Any], *, scope: str = "") -> Optional[str]:
    """Render learned stats for the prompt, or None when there isn't enough."""
    if stats.get("settled", 0) < MIN_SAMPLES:
        return None

    where = f" on {scope}" if scope else ""
    lines = [
        f"## Your Track Record{where} (realised, not self-assessed)",
        f"{stats['settled']} settled calls: {stats['wins']}W / {stats['losses']}L "
        f"({stats['win_rate']:.0%} win rate, avg {stats['avg_r']:+.2f}R).",
    ]

    for label, bucket in sorted(stats.get("by_confidence", {}).items(), reverse=True):
        if bucket["n"] >= 3:
            lines.append(
                f"  - Calls you rated {label}: {bucket['win_rate']:.0%} actually won "
                f"over {bucket['n']} trades (avg {bucket['avg_r']:+.2f}R)."
            )

    macro = stats.get("by_macro") or {}
    backed, against = macro.get("supported") or {}, macro.get("opposed") or {}
    if backed.get("n", 0) >= 3 and against.get("n", 0) >= 3:
        lines.append(
            f"  - With the dollar/VIX read backing the call: {backed['win_rate']:.0%} "
            f"won over {backed['n']} trades ({backed['avg_r']:+.2f}R). Against it: "
            f"{against['win_rate']:.0%} over {against['n']} ({against['avg_r']:+.2f}R)."
        )

    if stats.get("no_fill"):
        lines.append(
            f"  - {stats['no_fill']} proposals never filled — price never returned "
            "to the entry you set. Consider entries closer to current price."
        )

    lines.append(
        "Use this to calibrate: if a confidence band has underperformed, say so "
        "and state a lower confidence rather than repeating the same number."
    )
    block = "\n".join(lines)
    return block[:_MEMORY_BLOCK_CHARS]


async def memory_block_for(
    db: AsyncSession, symbols: Sequence[str] | None = None
) -> Optional[str]:
    """Track record for the instruments in play, falling back to overall."""
    try:
        if symbols:
            stats = await learned_stats(db, symbols=symbols)
            block = memory_block(stats, scope=", ".join(s.upper() for s in symbols[:3]))
            if block:
                return block
        return memory_block(await learned_stats(db))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Journal] memory block skipped: {}", exc)
        return None
