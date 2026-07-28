"""
MT5 Trading Plugin — SMC learning loop.

Persists every analysis, every emitted signal and every realised outcome, then
feeds that history back into the next analysis two ways:

  1. ``similar_setups()`` retrieves the closest historical setups by factor
     profile and returns their realised results, which ``smc_ai`` injects into
     the prompt as grounded context. New signals therefore reference what
     actually happened last time this shape appeared.
  2. ``recalibrate_weights()`` re-derives factor weights from realised P&L —
     factors that scored high before losers lose weight, factors that scored
     high before winners gain it — and ``learned_weights()`` hands those back to
     ``smc_scoring`` on the next scoring pass.

Every write also fans out through ``jarvis_learn_all_brains`` so the existing
three memories (Obsidian vault, AI Analyst knowledge store, PaulKnowledge)
remain the system of record. No new memory store is introduced.

All DB work is best-effort: a failure here degrades learning, never the
analysis response.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from loguru import logger
from sqlalchemy import Integer, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from plugins.MT5TradingPlugin.backend.models import (
    SmcAnalysisRecord,
    SmcFactorWeight,
    SmcOutcome,
    SmcSignalRecord,
)
from plugins.MT5TradingPlugin.backend.services import smc_scoring

#: Outcomes required before learned weights replace the defaults. Below this the
#: sample is too small to be anything but noise.
MIN_SAMPLES_FOR_RECALIBRATION = 10
#: How hard a factor's realised edge moves its weight.
LEARNING_RATE = 0.5
#: Weights stay within this band of their default so one bad streak cannot
#: switch off a structurally sound factor entirely.
MIN_WEIGHT_MULTIPLIER = 0.25
MAX_WEIGHT_MULTIPLIER = 2.00


# ── Pure helpers (no I/O — unit-testable) ────────────────────────────────────

def factor_vector(score_breakdown: Optional[Dict[str, Any]]) -> Dict[str, float]:
    """Flatten a breakdown to ``{factor: normalized}``.

    Normalized values are weight-independent, which is what both similarity
    search and weight recalibration need — using contributions would make
    recalibration circular (weights feeding back into their own inputs).
    """
    if not isinstance(score_breakdown, dict):
        return {}
    out: Dict[str, float] = {}
    for f in score_breakdown.get("factors") or []:
        if isinstance(f, dict) and f.get("name") is not None:
            try:
                out[str(f["name"])] = float(f.get("normalized") or 0.0)
            except (TypeError, ValueError):
                continue
    return out


def similarity(a: Dict[str, float], b: Dict[str, float]) -> float:
    """Cosine similarity over the union of factor names, in [-1, 1].

    Returns 0.0 when either side has no magnitude, so an all-zero profile never
    looks like a match for everything.
    """
    keys = set(a) | set(b)
    if not keys:
        return 0.0
    dot = sum(a.get(k, 0.0) * b.get(k, 0.0) for k in keys)
    na = sum(a.get(k, 0.0) ** 2 for k in keys) ** 0.5
    nb = sum(b.get(k, 0.0) ** 2 for k in keys) ** 0.5
    if na <= 0 or nb <= 0:
        return 0.0
    return max(-1.0, min(1.0, dot / (na * nb)))


def compute_excursions(
    *,
    side: str,
    entry: float,
    stop_loss: float,
    take_profit: float,
    candles: Sequence[Any],
    fill_time: Optional[int] = None,
) -> Dict[str, Any]:
    """Realised MFE / MAE / R multiple / time-to-target from the bars after fill.

    ``candles`` must be the bars from the fill onward. When both the stop and
    the target are touched inside the same bar the stop is assumed first — the
    pessimistic reading, so learning is never flattered by ambiguous bars.
    """
    risk = abs(entry - stop_loss)
    result: Dict[str, Any] = {
        "mfe": 0.0, "mae": 0.0, "mfe_r": 0.0, "mae_r": 0.0,
        "r_multiple": 0.0, "win": False, "exit_price": None,
        "exit_reason": "open", "time_to_target_s": None,
    }
    if risk <= 0 or not candles:
        return result

    is_buy = side == "buy"
    best = 0.0   # favourable excursion in price
    worst = 0.0  # adverse excursion in price
    exit_price: Optional[float] = None
    exit_reason = "open"
    exit_time: Optional[int] = None

    for c in candles:
        high, low = float(c.high), float(c.low)
        if is_buy:
            best = max(best, high - entry)
            worst = max(worst, entry - low)
            hit_sl = low <= stop_loss
            hit_tp = high >= take_profit
        else:
            best = max(best, entry - low)
            worst = max(worst, high - entry)
            hit_sl = high >= stop_loss
            hit_tp = low <= take_profit

        if hit_sl:  # pessimistic: stop resolves first on an ambiguous bar
            exit_price, exit_reason = stop_loss, "sl"
            exit_time = int(getattr(c, "time", 0) or 0)
            break
        if hit_tp:
            exit_price, exit_reason = take_profit, "tp"
            exit_time = int(getattr(c, "time", 0) or 0)
            break

    if exit_price is None:
        # Never resolved inside the window — mark to the last close.
        exit_price = float(candles[-1].close)
        exit_reason = "expiry"
        exit_time = int(getattr(candles[-1], "time", 0) or 0)

    signed = (exit_price - entry) if is_buy else (entry - exit_price)
    result.update({
        "mfe": round(best, 6),
        "mae": round(worst, 6),
        "mfe_r": round(best / risk, 4),
        "mae_r": round(worst / risk, 4),
        "r_multiple": round(signed / risk, 4),
        "win": signed > 0,
        "exit_price": round(exit_price, 6),
        "exit_reason": exit_reason,
    })
    if fill_time is not None and exit_time:
        result["time_to_target_s"] = max(0, int(exit_time) - int(fill_time))
    return result


def recalibrated_weights(
    stats: Dict[str, Dict[str, float]],
    defaults: Optional[Dict[str, float]] = None,
) -> Dict[str, float]:
    """Derive factor weights from realised win/loss factor profiles.

    ``stats`` maps factor -> {"win_mean", "loss_mean"}: the mean normalized
    value of that factor across winning and losing setups. A factor that scored
    higher before losers than before winners has a negative edge and loses
    weight. Weights are renormalised to sum to 1.0 so confidence stays on the
    same 0..1 scale as the defaults.
    """
    base = dict(defaults or smc_scoring.DEFAULT_WEIGHTS)
    tuned: Dict[str, float] = {}
    for factor, default_w in base.items():
        s = stats.get(factor) or {}
        edge = float(s.get("win_mean", 0.0)) - float(s.get("loss_mean", 0.0))
        mult = 1.0 + LEARNING_RATE * edge
        mult = max(MIN_WEIGHT_MULTIPLIER, min(MAX_WEIGHT_MULTIPLIER, mult))
        tuned[factor] = default_w * mult

    total = sum(tuned.values())
    if total <= 0:
        return base
    return {k: round(v / total, 6) for k, v in tuned.items()}


# ── Persistence ──────────────────────────────────────────────────────────────

async def record_analysis(
    db: AsyncSession,
    *,
    market: str,
    symbol: str,
    timeframe: str,
    analysis: Dict[str, Any],
    ai_block: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    """Persist the analysis header and every signal it produced.

    Returns the analysis row id, or None if persistence failed (never raises).
    """
    ai_block = ai_block or {}
    signals = [s for s in (analysis.get("signals") or []) if isinstance(s, dict)]
    try:
        row = SmcAnalysisRecord(
            market=market,
            symbol=symbol,
            timeframe=timeframe,
            bias=analysis.get("bias"),
            htf_bias=analysis.get("htf_bias"),
            last_price=analysis.get("last_price"),
            atr=analysis.get("atr"),
            rsi=analysis.get("rsi"),
            volume_z=analysis.get("volume_z"),
            momentum=analysis.get("momentum"),
            signal_count=len(signals),
            provider_used=ai_block.get("provider_used"),
            tier=ai_block.get("tier"),
            is_degraded=bool(ai_block.get("is_degraded")),
        )
        db.add(row)
        await db.flush()

        for sig in signals:
            bd = sig.get("score_breakdown") or {}
            db.add(SmcSignalRecord(
                analysis_id=row.id,
                market=market,
                symbol=symbol,
                timeframe=timeframe,
                side=sig.get("side", ""),
                zone_kind=sig.get("zone_kind"),
                entry=float(sig.get("entry") or 0.0),
                stop_loss=float(sig.get("stop_loss") or 0.0),
                take_profit=float(sig.get("take_profit") or 0.0),
                rr=sig.get("rr"),
                confidence=sig.get("confidence"),
                bias=analysis.get("bias"),
                htf_bias=analysis.get("htf_bias"),
                score_breakdown=bd,
                factor_vector=factor_vector(bd),
                confluence=sig.get("confluence") or [],
                volume_confirmed=bool(bd.get("volume_confirmed")),
            ))
        await db.commit()
    except Exception as exc:  # noqa: BLE001 — learning must never break analysis
        logger.warning(f"[smc_memory] record_analysis skipped: {exc}")
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None

    _fan_out_to_brains(
        action="smc-analysis",
        symbol=symbol,
        summary=(
            f"{market.upper()} {symbol} {timeframe}: bias {analysis.get('bias')}, "
            f"{len(signals)} setup(s), tier {ai_block.get('tier')}"
        ),
        detail=(ai_block.get("market_read") or "")[:600],
        importance=0.45,
        kind="analysis",
    )
    return row.id


async def link_signal_to_ticket(
    db: AsyncSession, signal_id: int, ticket: int
) -> None:
    """Record that a stored signal was actually placed as `ticket`."""
    try:
        row = await db.get(SmcSignalRecord, signal_id)
        if row is not None:
            row.ticket = ticket
            await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[smc_memory] link_signal_to_ticket skipped: {exc}")
        await db.rollback()


async def attach_ticket_to_signal(
    db: AsyncSession,
    *,
    market: str,
    symbol: str,
    side: str,
    entry: float,
    ticket: int,
    max_age_hours: int = 48,
) -> Optional[int]:
    """Link a freshly placed order to the stored signal it came from.

    Matches the most recent unlinked signal for this symbol/side whose entry is
    within a tick of the placed price. Returns the signal id, or None when no
    stored signal corresponds (e.g. a hand-placed order).
    """
    try:
        cutoff = datetime.utcnow() - timedelta(hours=max_age_hours)
        rows = (
            await db.execute(
                select(SmcSignalRecord)
                .where(
                    SmcSignalRecord.market == market,
                    SmcSignalRecord.symbol == symbol,
                    SmcSignalRecord.side == side,
                    SmcSignalRecord.ticket.is_(None),
                    SmcSignalRecord.created_at >= cutoff,
                )
                .order_by(SmcSignalRecord.created_at.desc())
                .limit(50)
            )
        ).scalars().all()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[smc_memory] attach_ticket_to_signal skipped: {exc}")
        return None

    tol = max(abs(entry) * 1e-4, 1e-6)
    match = next((r for r in rows if abs(float(r.entry) - entry) <= tol), None)
    if match is None:
        return None
    try:
        match.ticket = int(ticket)
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[smc_memory] ticket link commit skipped: {exc}")
        await db.rollback()
        return None
    return match.id


async def unsettled_signals(
    db: AsyncSession, *, market: str = "mt5", symbol: Optional[str] = None,
    limit: int = 50,
) -> List[SmcSignalRecord]:
    """Signals that were actually placed but have no recorded outcome yet."""
    try:
        settled = select(SmcOutcome.signal_id)
        q = (
            select(SmcSignalRecord)
            .where(
                SmcSignalRecord.market == market,
                SmcSignalRecord.ticket.is_not(None),
                SmcSignalRecord.id.not_in(settled),
            )
            .order_by(SmcSignalRecord.created_at.asc())
            .limit(limit)
        )
        if symbol:
            q = q.where(SmcSignalRecord.symbol == symbol)
        return list((await db.execute(q)).scalars().all())
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[smc_memory] unsettled_signals skipped: {exc}")
        return []


async def settle_signal(
    db: AsyncSession, signal: SmcSignalRecord, candles: Sequence[Any],
) -> Optional[int]:
    """Compute and store the realised outcome for one placed signal.

    Walks forward from the bar that filled the resting limit. Returns None when
    the limit never filled or the trade is still open — those stay unsettled and
    are retried on the next sync.
    """
    if not candles or not signal.created_at:
        return None
    placed_at = int(signal.created_at.timestamp())
    entry = float(signal.entry)

    fill_idx: Optional[int] = None
    for i, c in enumerate(candles):
        if int(getattr(c, "time", 0) or 0) < placed_at:
            continue
        # A resting buy limit fills when price trades down through it.
        if signal.side == "buy" and float(c.low) <= entry:
            fill_idx = i
            break
        if signal.side == "sell" and float(c.high) >= entry:
            fill_idx = i
            break
    if fill_idx is None:
        return None

    after = candles[fill_idx:]
    excursions = compute_excursions(
        side=signal.side,
        entry=entry,
        stop_loss=float(signal.stop_loss),
        take_profit=float(signal.take_profit),
        candles=after,
        fill_time=int(getattr(after[0], "time", 0) or 0),
    )
    if excursions["exit_reason"] == "expiry":
        return None  # still running — settle it on a later pass

    return await record_outcome(
        db, signal_id=signal.id, excursions=excursions, ticket=signal.ticket,
    )


async def record_outcome(
    db: AsyncSession,
    *,
    signal_id: int,
    excursions: Dict[str, Any],
    pnl: float = 0.0,
    ticket: Optional[int] = None,
    closed_at: Optional[datetime] = None,
) -> Optional[int]:
    """Persist the realised result of a signal. Idempotent per signal."""
    try:
        signal = await db.get(SmcSignalRecord, signal_id)
        if signal is None:
            return None
        existing = (
            await db.execute(
                select(SmcOutcome).where(SmcOutcome.signal_id == signal_id)
            )
        ).scalar_one_or_none()

        row = existing or SmcOutcome(signal_id=signal_id)
        row.market = signal.market
        row.symbol = signal.symbol
        row.ticket = ticket if ticket is not None else signal.ticket
        row.mfe = float(excursions.get("mfe") or 0.0)
        row.mae = float(excursions.get("mae") or 0.0)
        row.mfe_r = float(excursions.get("mfe_r") or 0.0)
        row.mae_r = float(excursions.get("mae_r") or 0.0)
        row.r_multiple = float(excursions.get("r_multiple") or 0.0)
        row.win = bool(excursions.get("win"))
        row.exit_price = excursions.get("exit_price")
        row.exit_reason = excursions.get("exit_reason")
        row.time_to_target_s = excursions.get("time_to_target_s")
        row.pnl = float(pnl or 0.0)
        row.closed_at = closed_at or datetime.utcnow()
        if existing is None:
            db.add(row)
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[smc_memory] record_outcome skipped: {exc}")
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None

    _fan_out_to_brains(
        action="smc-outcome",
        symbol=signal.symbol,
        summary=(
            f"{signal.symbol} {signal.side} closed "
            f"{'WIN' if row.win else 'LOSS'} at {row.r_multiple:+.2f}R "
            f"(MFE {row.mfe_r:.2f}R, MAE {row.mae_r:.2f}R)"
        ),
        detail=f"entry {signal.entry} sl {signal.stop_loss} tp {signal.take_profit}; "
               f"exit {row.exit_price} via {row.exit_reason}",
        importance=0.75,   # realised results matter more than opinions
        kind="outcome",
    )
    return row.id


# ── Retrieval ────────────────────────────────────────────────────────────────

async def similar_setups(
    db: AsyncSession,
    *,
    market: str,
    symbol: str,
    side: str,
    factors: Dict[str, float],
    limit: int = 3,
    candidate_pool: int = 200,
) -> List[Dict[str, Any]]:
    """Closest historical setups by factor profile, with their realised results.

    Only setups that were actually traded (i.e. have an outcome row) are
    returned — an untraded signal teaches nothing. Ordered by similarity.
    """
    if not factors:
        return []
    try:
        rows = (
            await db.execute(
                select(SmcSignalRecord, SmcOutcome)
                .join(SmcOutcome, SmcOutcome.signal_id == SmcSignalRecord.id)
                .where(
                    SmcSignalRecord.market == market,
                    SmcSignalRecord.symbol == symbol,
                    SmcSignalRecord.side == side,
                )
                .order_by(SmcSignalRecord.created_at.desc())
                .limit(candidate_pool)
            )
        ).all()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[smc_memory] similar_setups skipped: {exc}")
        return []

    scored: List[Tuple[float, Dict[str, Any]]] = []
    for signal, outcome in rows:
        sim = similarity(factors, signal.factor_vector or {})
        scored.append((sim, {
            "similarity": round(sim, 4),
            "when": signal.created_at.isoformat() if signal.created_at else None,
            "timeframe": signal.timeframe,
            "side": signal.side,
            "zone_kind": signal.zone_kind,
            "confidence": signal.confidence,
            "rr": signal.rr,
            "r_multiple": outcome.r_multiple,
            "win": outcome.win,
            "mfe_r": outcome.mfe_r,
            "mae_r": outcome.mae_r,
            "exit_reason": outcome.exit_reason,
            "time_to_target_s": outcome.time_to_target_s,
        }))

    scored.sort(key=lambda t: t[0], reverse=True)
    return [item for _sim, item in scored[:limit]]


async def outcome_summary(
    db: AsyncSession, *, market: str, symbol: str, side: Optional[str] = None
) -> Dict[str, Any]:
    """Aggregate realised performance for this instrument (and optionally side)."""
    try:
        q = (
            select(
                func.count(SmcOutcome.id),
                func.sum(func.cast(SmcOutcome.win, Integer)),
                func.avg(SmcOutcome.r_multiple),
                func.avg(SmcOutcome.mfe_r),
                func.avg(SmcOutcome.mae_r),
            )
            .select_from(SmcOutcome)
            .join(SmcSignalRecord, SmcSignalRecord.id == SmcOutcome.signal_id)
            .where(SmcSignalRecord.market == market, SmcSignalRecord.symbol == symbol)
        )
        if side:
            q = q.where(SmcSignalRecord.side == side)
        total, wins, avg_r, avg_mfe, avg_mae = (await db.execute(q)).one()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[smc_memory] outcome_summary skipped: {exc}")
        return {}

    total = int(total or 0)
    if not total:
        return {}
    return {
        "trades": total,
        "wins": int(wins or 0),
        "win_rate": round((wins or 0) / total, 4),
        "avg_r": round(float(avg_r or 0.0), 4),
        "avg_mfe_r": round(float(avg_mfe or 0.0), 4),
        "avg_mae_r": round(float(avg_mae or 0.0), 4),
    }


# ── Weight recalibration ─────────────────────────────────────────────────────

async def recalibrate_weights(
    db: AsyncSession, *, market: str = "mt5", symbol_class: str = "*"
) -> Dict[str, float]:
    """Re-derive factor weights from realised outcomes and persist them.

    Returns the weights actually in force afterwards — the defaults when there
    are not yet enough closed trades to learn from.
    """
    defaults = dict(smc_scoring.DEFAULT_WEIGHTS)
    try:
        q = (
            select(SmcSignalRecord.factor_vector, SmcOutcome.win)
            .join(SmcOutcome, SmcOutcome.signal_id == SmcSignalRecord.id)
            .where(SmcSignalRecord.market == market)
        )
        if symbol_class != "*":
            q = q.where(SmcSignalRecord.symbol == symbol_class)
        rows = (await db.execute(q)).all()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[smc_memory] recalibrate_weights skipped: {exc}")
        return defaults

    if len(rows) < MIN_SAMPLES_FOR_RECALIBRATION:
        return defaults

    sums: Dict[str, Dict[str, float]] = {}
    counts = {"win": 0, "loss": 0}
    for vector, win in rows:
        bucket = "win" if win else "loss"
        counts[bucket] += 1
        for factor, value in (vector or {}).items():
            entry = sums.setdefault(factor, {"win": 0.0, "loss": 0.0})
            entry[bucket] += float(value or 0.0)

    stats = {
        factor: {
            "win_mean": (v["win"] / counts["win"]) if counts["win"] else 0.0,
            "loss_mean": (v["loss"] / counts["loss"]) if counts["loss"] else 0.0,
        }
        for factor, v in sums.items()
    }
    tuned = recalibrated_weights(stats, defaults)

    try:
        for factor, weight in tuned.items():
            s = stats.get(factor, {})
            existing = (
                await db.execute(
                    select(SmcFactorWeight).where(
                        SmcFactorWeight.market == market,
                        SmcFactorWeight.symbol_class == symbol_class,
                        SmcFactorWeight.factor == factor,
                    )
                )
            ).scalar_one_or_none()
            row = existing or SmcFactorWeight(
                market=market, symbol_class=symbol_class, factor=factor,
            )
            row.weight = weight
            row.default_weight = defaults.get(factor, 0.0)
            row.sample_count = len(rows)
            row.win_contribution = round(s.get("win_mean", 0.0), 6)
            row.loss_contribution = round(s.get("loss_mean", 0.0), 6)
            if existing is None:
                db.add(row)
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[smc_memory] weight persist skipped: {exc}")
        await db.rollback()
        return defaults

    _fan_out_to_brains(
        action="smc-recalibration",
        symbol=symbol_class if symbol_class != "*" else "",
        summary=(
            f"Recalibrated SMC factor weights from {len(rows)} closed trades "
            f"({counts['win']}W/{counts['loss']}L)"
        ),
        detail="; ".join(
            f"{k} {defaults[k]:.3f}->{tuned[k]:.3f}"
            for k in sorted(tuned, key=lambda x: abs(tuned[x] - defaults.get(x, 0)),
                            reverse=True)[:6]
        ),
        importance=0.8,
        kind="calibration",
    )
    return tuned


async def learned_weights(
    db: AsyncSession, *, market: str = "mt5", symbol: str = ""
) -> Optional[Dict[str, float]]:
    """Weights currently in force: symbol-specific if present, else global.

    Returns None when nothing has been learned yet, so the engine keeps its
    defaults rather than being handed a half-populated map.
    """
    for scope in ([symbol] if symbol else []) + ["*"]:
        try:
            rows = (
                await db.execute(
                    select(SmcFactorWeight).where(
                        SmcFactorWeight.market == market,
                        SmcFactorWeight.symbol_class == scope,
                    )
                )
            ).scalars().all()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[smc_memory] learned_weights skipped: {exc}")
            return None
        if rows:
            return {r.factor: float(r.weight) for r in rows}
    return None


# ── Fan-out to the existing three memories ───────────────────────────────────

def _fan_out_to_brains(
    *, action: str, symbol: str, summary: str, detail: str,
    importance: float, kind: str,
) -> None:
    """Write through to vault + AI Analyst store + PaulKnowledge.

    Imported lazily because ``app.api.jarvis`` pulls in a large slice of the
    core app; the learning loop must stay importable in isolation (and in tests).
    """
    try:
        from app.api.jarvis import jarvis_learn_all_brains

        jarvis_learn_all_brains(
            action=action, symbol=symbol, summary=summary, detail=detail,
            tags=["smc", "sniper", action], importance=importance, kind=kind,
        )
    except Exception as exc:  # noqa: BLE001 — best effort by design
        logger.debug(f"[smc_memory] brain fan-out skipped: {exc}")
