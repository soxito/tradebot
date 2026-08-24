"""
MT5 Trading Plugin — per-signal research queue.

``research_loop`` collects the *ambient* background: the economic calendar, the
news feeds, the Fear & Greed index. It never looks at the signals the app itself
produces, and it never asks a model anything — which is why the ``prediction``
kind it declares was always empty.

This module closes that loop. Every signal the app creates or ingests — Telegram
parsed signals, Telegram sniper trades, SMC sniper setups, core ``signals`` rows
— becomes a ``SignalResearchJob``. A bounded worker pool researches at most
``DEFAULT_CONCURRENCY`` of them at a time before moving on to the next token, and
each job walks a fixed seven-step pipeline that is written back to the row as it
goes, so the Research page can render live progress instead of a spinner.

What a job actually reads, in order:

  1. ``load_signal``     — the signal itself: direction, entry, stop, target
  2. ``price_history``   — real candles across 1h/4h/1d, reduced to a movement
                           summary (trend, RSI, ATR, swings, distance to levels)
  3. ``pair_knowledge``  — what we already know about this instrument: stored
                           agent knowledge, realised SMC outcomes, the JARVIS
                           journal's measured track record
  4. ``stored_research`` — findings the background loop already collected from
                           the configured providers, for this pair and globally
  5. ``web_news``        — a live web news search plus the RSS aggregate, with
                           the top articles fetched in full (SSRF-guarded)
  6. ``calendar``        — upcoming high-impact events for this instrument
  7. ``predict``         — one JSON-mode model call over all of the above

Two rules carried over from ``research_loop``:

  * **Idle-first providers.** A job prefers a provider that is not serving a
    live analysis, waiting up to ``IDLE_WAIT_S`` for one. If none frees up it
    falls back to the normal cascade rather than never producing a prediction.
  * **Sources or speculation.** A prediction with no resolvable source URL is
    stored ``speculative=True`` and can never gate a trade signal.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from loguru import logger
from sqlalchemy import func, select

from plugins.MT5TradingPlugin.backend.models import ResearchFinding, SignalResearchJob
from plugins.MT5TradingPlugin.backend.services import agent_bus, research_loop

#: How many signals are researched simultaneously before the queue moves on to
#: the next token. Every job holds a model slot and a handful of HTTP fetches,
#: so this is the knob that keeps research from starving live analysis.
DEFAULT_CONCURRENCY = 5

#: How often the queue sweeps the signal tables for work.
SCAN_INTERVAL_S = 180

#: How long a job waits for a provider that is not serving live analysis before
#: falling back to the full cascade. Module-level so tests can zero it.
IDLE_WAIT_S = 45.0

#: Per-provider and whole-cascade budgets for the prediction step.
#:
#: Far more generous than the live-analysis defaults (12s / 36s) on purpose:
#: nothing is waiting on a research job, and those defaults exhausted the
#: cascade after about three providers — so a run could be defeated by two dead
#: endpoints and one slow one, having never reached a provider that works. A
#: research prompt is also much longer than a live one, and Cohere was being cut
#: off mid-answer at exactly 12s. These let a job work through the entire
#: provider list until something returns a usable analysis.
PREDICT_TIMEOUT_S = 30.0
PREDICT_BUDGET_S = 240.0

#: Only signals this fresh are worth researching — an eight-hour-old entry has
#: already played out.
SIGNAL_LOOKBACK_HOURS = 24

#: How long a signal is left alone after it has been researched.
#:
#: Without this the sweep re-queues every signal it can still see, every scan,
#: forever: the dedupe below only blocks a signal that is *currently* in flight,
#: so a finished one becomes eligible again the moment it lands. In practice
#: that meant the same 22 signals were researched 10 times over inside half an
#: hour — hundreds of model calls and thousands of HTTP fetches for no new
#: information, which rate-limited every provider and wedged the process.
RESEARCH_COOLDOWN_HOURS = 6

#: Failures get a much shorter leash than successes: a 429 or a dead feed is
#: worth another go soon, a delivered prediction is not.
RETRY_COOLDOWN_MINUTES = 30

#: Stop sweeping while this much work is already waiting. The backlog is the
#: symptom of a queue that cannot keep up; adding to it only makes it worse.
MAX_QUEUE_DEPTH = 40

#: Consecutive job failures before the worker stops claiming for a while. Every
#: provider being rate-limited is the common cause, and hammering through it
#: turns a transient limit into a sustained one.
FAILURE_BACKOFF_THRESHOLD = 3
FAILURE_BACKOFF_S = 120.0

#: A job claimed but not finished within this long has lost its owner — the
#: process was reloaded, killed, or crashed mid-run. Claims live in the database,
#: so without this sweep those rows sit in ``researching`` forever, and the board
#: reports slots that nothing is actually working.
STALE_MINUTES = 15

#: Timeframes the price-movement summary is built from.
TIMEFRAMES: Tuple[str, ...] = ("1h", "4h", "1d")

#: The pipeline. Order matters: it is the progress denominator and the checklist
#: the UI renders.
STEPS: Tuple[str, ...] = (
    "load_signal", "price_history", "pair_knowledge",
    "stored_research", "web_news", "calendar", "predict",
)

TERMINAL_STATUSES = {"done", "failed", "skipped"}
VERDICTS = {"bullish", "bearish", "neutral", "stand_aside"}

#: Predictions decay on the same horizon research_loop gives the kind.
_PREDICTION_KIND = "prediction"

#: Horizon used when a plan somehow carries none, so a stale plan still expires.
DECAY_FALLBACK_HOURS = 8


# ── Small numeric helpers (kept local: this module must not import the whole
#    jarvis API surface just to average a list) ────────────────────────────────

def _ema(values: Sequence[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def _rsi(closes: Sequence[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(len(closes) - period, len(closes)):
        delta = closes[i] - closes[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return 100.0 - (100.0 / (1.0 + rs))


def _atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
         period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    trs: List[float] = []
    for i in range(len(closes) - period, len(closes)):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    return sum(trs) / len(trs) if trs else None


def _pct(a: float, b: float) -> float:
    return ((a - b) / b * 100.0) if b else 0.0


# ── Candles ──────────────────────────────────────────────────────────────────

async def _candles(symbol: str, timeframe: str, limit: int = 200) -> List[List[float]]:
    """``[[ts_ms, o, h, l, c, v], …]`` for any instrument, or ``[]``.

    Tries the universal (FX / metals / indices / commodities) resolver first,
    then the crypto exchange feed. Returns empty rather than raising: an
    unpriceable symbol degrades the prediction, it does not fail the job.
    """
    try:
        from app.services import market_data

        if market_data.is_universal_symbol(symbol):
            ohlcv, _ticker = await market_data.fetch_ohlcv_universal(
                symbol, timeframe=timeframe, limit=limit
            )
            if ohlcv:
                return [[float(x) for x in row[:6]] for row in ohlcv]
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[signal-research] universal candles {symbol}/{timeframe}: {exc}")

    try:
        from plugins.MT5TradingPlugin.backend.services.candle_feed import _exchange_candles

        tf_map = {"1h": "H1", "4h": "H4", "1d": "D1", "15m": "M15", "5m": "M5"}
        rows = await _exchange_candles(symbol, tf_map.get(timeframe, "H1"), limit)
        return [
            [float(c["time"]) * 1000, c["open"], c["high"], c["low"], c["close"], c["volume"]]
            for c in rows
        ]
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[signal-research] exchange candles {symbol}/{timeframe}: {exc}")
        return []


def _movement_block(symbol: str, series: Dict[str, List[List[float]]],
                    job: SignalResearchJob) -> str:
    """Reduce raw candles to the price behaviour a model can reason about."""
    lines = [f"# Price movement — {symbol}"]
    last_close: Optional[float] = None

    for tf, ohlcv in series.items():
        if len(ohlcv) < 20:
            lines.append(f"- {tf}: no usable history")
            continue
        closes = [c[4] for c in ohlcv]
        highs = [c[2] for c in ohlcv]
        lows = [c[3] for c in ohlcv]
        close = closes[-1]
        last_close = last_close or close

        ema20, ema50 = _ema(closes, 20), _ema(closes, 50)
        rsi = _rsi(closes)
        atr = _atr(highs, lows, closes)
        if ema20 is None or ema50 is None or rsi is None or atr is None:
            lines.append(f"- {tf}: close {close:.6g} | insufficient indicator history")
            continue

        window = min(len(closes), 60)
        swing_hi, swing_lo = max(highs[-window:]), min(lows[-window:])
        span = swing_hi - swing_lo
        position = ((close - swing_lo) / span * 100.0) if span > 0 else 0.0
        trend = "up" if ema20 > ema50 else "down" if ema20 < ema50 else "flat"
        lines.append(
            f"- {tf}: close {close:.6g} | trend {trend} "
            f"(ema20 {ema20:.6g} vs ema50 {ema50:.6g}) | "
            f"RSI {rsi:.1f} | ATR {atr:.6g} | "
            f"last {window} bars {_pct(close, closes[-window]):+.2f}% | "
            f"range {swing_lo:.6g}–{swing_hi:.6g} (now {position:.0f}% of range)"
        )

    if last_close and job.entry:
        lines.append(
            f"- Signal levels vs live: entry {job.entry:.6g} "
            f"({_pct(last_close, job.entry):+.2f}% away)"
            + (f", stop {job.stop_loss:.6g}" if job.stop_loss else "")
            + (f", target {job.take_profit:.6g}" if job.take_profit else "")
        )
    return "\n".join(lines)


def _fib_block(symbol: str, series: Dict[str, List[List[float]]]) -> str:
    """Where price sits in the latest swing's fib retracement, per timeframe.

    A signal that enters inside the 0.5–0.618 golden zone in the direction of the
    swing is a materially different proposition from one chasing an extension,
    and the model cannot infer that from the movement block's flat range figures.
    """
    from app.signals.technical import (
        auto_fib_retracement,
        fib_confluence_score,
        ohlcv_to_dataframe,
    )

    lines = [f"# Auto fib retracement — {symbol}"]
    for tf, ohlcv in series.items():
        if len(ohlcv) < 20:
            lines.append(f"- {tf}: no usable history")
            continue
        try:
            df = ohlcv_to_dataframe(ohlcv)
            fib = auto_fib_retracement(df, levels=(0.5, 0.618), extend_lines=False)
            swing = fib.get("swing")
            if not swing:
                lines.append(f"- {tf}: no confirmed swing")
                continue
            close = float(df["close"].iloc[-1])
            zone = fib.get("golden_zone") or {}
            conf = fib_confluence_score(close, fib, swing["direction"])
            inside = bool(zone and zone.get("low") <= close <= zone.get("high"))
            bias = "bullish" if swing["direction"] == "up" else "bearish"
            where = (
                f"in the golden zone (strength {conf:.2f})" if inside and conf is not None
                else "outside the golden zone"
            )
            lines.append(
                f"- {tf}: {bias} swing {swing['start_price']:.6g}→{swing['end_price']:.6g} | "
                f"close {close:.6g} {where} "
                f"(0.5–0.618 = {zone.get('low', 0):.6g}–{zone.get('high', 0):.6g})"
            )
        except Exception as exc:  # noqa: BLE001 - research must never fail on one timeframe
            logger.debug(f"[signal-research] fib {symbol}/{tf}: {exc}")
            lines.append(f"- {tf}: unavailable")
    return "\n".join(lines)


# ── Signal collection ────────────────────────────────────────────────────────

def _norm(symbol: str) -> str:
    try:
        from app.services import market_data

        return market_data.normalize_symbol(symbol) or (symbol or "").upper()
    except Exception:  # noqa: BLE001
        return (symbol or "").upper()


async def collect_pending_signals(db, *, limit: int = 60) -> List[Dict[str, Any]]:
    """Every live signal the app produced or ingested, newest first.

    Each source is isolated: a plugin that is not installed, or a table that
    does not exist yet, contributes nothing instead of emptying the sweep.
    """
    since = datetime.utcnow() - timedelta(hours=SIGNAL_LOOKBACK_HOURS)
    out: List[Dict[str, Any]] = []

    # 1. Telegram parsed signals — what the channels posted.
    try:
        from plugins.TelegramSignalNewsPlugin.backend.models import (
            SignalStatus as TgStatus, TelegramParsedSignal,
        )

        rows = (await db.execute(
            select(TelegramParsedSignal)
            .where(
                TelegramParsedSignal.created_at >= since,
                TelegramParsedSignal.status.in_([TgStatus.ACTIVE, TgStatus.FILLED]),
            )
            .order_by(TelegramParsedSignal.created_at.desc())
            .limit(limit)
        )).scalars().all()
        for r in rows:
            tps = r.take_profits_json or []
            out.append({
                "source": "telegram", "signal_ref": f"telegram:{r.id}",
                "symbol": _norm(r.symbol),
                "direction": "buy" if str(r.direction).lower() in ("long", "buy") else "sell",
                "entry": r.entry, "stop_loss": r.stop_loss,
                "take_profit": float(tps[0]) if tps else None,
            })
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[signal-research] telegram signals skipped: {exc}")

    # 2. Telegram sniper trades — what the sniper planned off those signals.
    try:
        from plugins.TelegramSignalNewsPlugin.backend.models import (
            SniperTradeStatus, TelegramSniperTrade,
        )

        rows = (await db.execute(
            select(TelegramSniperTrade)
            .where(
                TelegramSniperTrade.created_at >= since,
                TelegramSniperTrade.status.in_(
                    [SniperTradeStatus.PENDING, SniperTradeStatus.PLACED]
                ),
            )
            .order_by(TelegramSniperTrade.created_at.desc())
            .limit(limit)
        )).scalars().all()
        for r in rows:
            out.append({
                "source": "sniper", "signal_ref": f"sniper:{r.id}",
                "symbol": _norm(r.symbol),
                "direction": "buy" if str(r.direction).lower() in ("long", "buy") else "sell",
                "entry": r.sniper_entry or r.signal_entry,
                "stop_loss": r.stop_loss, "take_profit": r.take_profit,
            })
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[signal-research] sniper trades skipped: {exc}")

    # 3. SMC sniper setups the plugin emitted itself.
    try:
        from plugins.MT5TradingPlugin.backend.models import SmcSignalRecord

        rows = (await db.execute(
            select(SmcSignalRecord)
            .where(SmcSignalRecord.created_at >= since)
            .order_by(SmcSignalRecord.created_at.desc())
            .limit(limit)
        )).scalars().all()
        for r in rows:
            out.append({
                "source": "smc", "signal_ref": f"smc:{r.id}",
                "symbol": _norm(r.symbol), "direction": r.side,
                "entry": r.entry, "stop_loss": r.stop_loss, "take_profit": r.take_profit,
            })
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[signal-research] smc signals skipped: {exc}")

    # 4. Core signals table — everything else the app generates.
    try:
        from app.models.database import Signal, SignalStatus

        rows = (await db.execute(
            select(Signal)
            .where(Signal.created_at >= since, Signal.status == SignalStatus.PENDING)
            .order_by(Signal.created_at.desc())
            .limit(limit)
        )).scalars().all()
        for r in rows:
            action = str(getattr(r.action, "value", r.action) or "").lower()
            out.append({
                "source": "core", "signal_ref": f"core:{r.id}",
                "symbol": _norm(r.symbol),
                "direction": "buy" if "buy" in action or "long" in action else "sell",
                "entry": r.price, "stop_loss": None, "take_profit": None,
            })
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[signal-research] core signals skipped: {exc}")

    return [s for s in out if s.get("symbol")][:limit]


# ── Queue ────────────────────────────────────────────────────────────────────

async def _in_cooldown(db, signal_ref: str,
                       *, new_refs: Optional[Sequence[str]] = None) -> bool:
    """True while this pair's last research run is still recent enough to stand.

    A signal the last run never saw ends the cooldown immediately: the whole
    point of batching is that the newcomer changes the picture for the ones
    already there.
    """
    try:
        last = (await db.execute(
            select(SignalResearchJob)
            .where(
                SignalResearchJob.signal_ref == signal_ref,
                SignalResearchJob.status.in_(list(TERMINAL_STATUSES)),
                SignalResearchJob.finished_at.isnot(None),
            )
            .order_by(SignalResearchJob.finished_at.desc())
            .limit(1)
        )).scalars().first()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[signal-research] cooldown lookup skipped: {exc}")
        return False

    if last is None or last.finished_at is None:
        return False

    if new_refs and not set(new_refs).issubset(set(last.signal_refs or [])):
        return False   # a signal the last run never saw

    window = (
        timedelta(hours=RESEARCH_COOLDOWN_HOURS) if last.status == "done"
        else timedelta(minutes=RETRY_COOLDOWN_MINUTES)
    )
    return datetime.utcnow() - last.finished_at < window


def pair_key(symbol: str) -> str:
    """The natural key for a pair's research. One job per instrument."""
    return f"pair:{_norm(symbol)}"


def _consensus(signals: Sequence[Dict[str, Any]]) -> Optional[str]:
    """Majority direction across the batch, or None when they truly disagree.

    A split is not a failure — it is the most interesting input the research
    gets, and resolving it is most of the value of batching.
    """
    buys = sum(1 for s in signals if str(s.get("direction") or "").lower() == "buy")
    sells = sum(1 for s in signals if str(s.get("direction") or "").lower() == "sell")
    if buys > sells:
        return "buy"
    if sells > buys:
        return "sell"
    return None


def group_by_pair(signals: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fold every live signal into one batch per instrument.

    Three Telegram calls and an SMC setup on XAUUSD are four opinions about one
    market. Researched separately that is four times the model spend and four
    answers nobody reconciled; researched together they corroborate or
    contradict each other, and the spread between their entries is what the two
    entry plans are built from.
    """
    batches: Dict[str, Dict[str, Any]] = {}
    for sig in signals:
        symbol = _norm(str(sig.get("symbol") or ""))
        if not symbol:
            continue
        batch = batches.setdefault(symbol, {
            "symbol": symbol, "signals": [], "sources": set(),
        })
        batch["signals"].append(sig)
        batch["sources"].add(str(sig.get("source") or "manual"))

    out: List[Dict[str, Any]] = []
    for batch in batches.values():
        members = batch["signals"]
        # Median-ish reference levels, used only as the row's summary; the real
        # numbers come out of the research as `entries`.
        entries = [s["entry"] for s in members if s.get("entry")]
        stops = [s["stop_loss"] for s in members if s.get("stop_loss")]
        targets = [s["take_profit"] for s in members if s.get("take_profit")]
        out.append({
            "symbol": batch["symbol"],
            "source": "+".join(sorted(batch["sources"]))[:40],
            "signal_ref": pair_key(batch["symbol"]),
            "signal_refs": [s["signal_ref"] for s in members if s.get("signal_ref")],
            "signals": members,
            "direction": _consensus(members),
            "entry": sum(entries) / len(entries) if entries else None,
            "stop_loss": sum(stops) / len(stops) if stops else None,
            "take_profit": sum(targets) / len(targets) if targets else None,
        })
    return out


async def enqueue(db, *, symbol: str, source: str = "manual",
                  signal_ref: Optional[str] = None, direction: Optional[str] = None,
                  entry: Optional[float] = None, stop_loss: Optional[float] = None,
                  take_profit: Optional[float] = None,
                  signal_refs: Optional[Sequence[str]] = None,
                  signals: Optional[Sequence[Dict[str, Any]]] = None,
                  force: bool = False) -> Optional[SignalResearchJob]:
    """Queue one PAIR's batch. ``None`` when it is already queued or running, or
    was researched too recently to be worth repeating.

    Dedupe lives here rather than in a DB constraint: a pair SHOULD be
    researchable again once the news around it has moved on — but that is hours
    later, not on the next sweep. Two things override the cooldown: ``force``
    (a deliberate, user-initiated re-run) and a signal the last run never saw,
    because new information is exactly what research is for.
    """
    symbol = _norm(symbol)
    if not symbol:
        return None
    ref = signal_ref or pair_key(symbol)
    members = list(signals or [])
    refs = list(signal_refs or [])

    try:
        existing = (await db.execute(
            select(SignalResearchJob).where(
                SignalResearchJob.signal_ref == ref,
                SignalResearchJob.status.notin_(list(TERMINAL_STATUSES)),
            ).limit(1)
        )).scalars().first()
        if existing is not None:
            return None

        if not force and await _in_cooldown(db, ref, new_refs=refs):
            return None

        job = SignalResearchJob(
            symbol=symbol, source=source, signal_ref=ref, direction=direction,
            entry=entry, stop_loss=stop_loss, take_profit=take_profit,
            signal_refs=refs, signals=members,
            signal_count=max(1, len(members)),
            status="queued", stage="queued", progress=0.0, steps=[],
            queued_at=datetime.utcnow(),
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)
        return job
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[signal-research] enqueue failed for {symbol}: {exc}")
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


async def scan_and_enqueue(db, *, limit: int = 60) -> int:
    """Sweep every signal source and queue whatever is genuinely new."""
    try:
        backlog = int((await db.execute(
            select(func.count(SignalResearchJob.id))
            .where(SignalResearchJob.status == "queued")
        )).scalar() or 0)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[signal-research] backlog check skipped: {exc}")
        backlog = 0

    if backlog >= MAX_QUEUE_DEPTH:
        logger.info(
            f"🔬 [SIGNAL RESEARCH] scan skipped — {backlog} already waiting "
            f"(cap {MAX_QUEUE_DEPTH})"
        )
        return 0

    signals = await collect_pending_signals(db, limit=limit)
    batches = group_by_pair(signals)

    queued = 0
    for batch in batches:
        if backlog + queued >= MAX_QUEUE_DEPTH:
            break
        if await enqueue(db, **batch) is not None:
            queued += 1
    if queued:
        logger.info(
            f"🔬 [SIGNAL RESEARCH] queued {queued} pair(s) "
            f"from {len(signals)} signal(s)"
        )
    return queued


async def requeue_stale(db, *, older_than_minutes: int = STALE_MINUTES) -> int:
    """Return abandoned ``researching`` rows to the queue.

    Pass ``0`` to reclaim everything, which is what a freshly started queue
    wants: this process holds no jobs, so anything still marked in-flight
    belongs to a process that is gone.
    """
    cutoff = datetime.utcnow() - timedelta(minutes=max(0, older_than_minutes))
    try:
        rows = (await db.execute(
            select(SignalResearchJob).where(
                SignalResearchJob.status == "researching",
                SignalResearchJob.started_at <= cutoff,
            )
        )).scalars().all()
        for job in rows:
            job.status = "queued"
            job.stage = "requeued"
            job.progress = 0.0
            job.steps = []
            job.started_at = None
            job.error = "reclaimed after its worker went away"
        if rows:
            await db.commit()
            logger.info(f"🔬 [SIGNAL RESEARCH] reclaimed {len(rows)} abandoned job(s)")
        return len(rows)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[signal-research] stale sweep skipped: {exc}")
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0


async def _claim(db, count: int) -> List[int]:
    """Move up to `count` queued jobs into ``researching`` and return their ids.

    Claiming in the database (not in memory) is what makes the concurrency cap
    real: nothing can start a job that is not sitting in a claimed row.
    """
    if count <= 0:
        return []
    try:
        rows = (await db.execute(
            select(SignalResearchJob)
            .where(SignalResearchJob.status == "queued")
            .order_by(SignalResearchJob.queued_at.asc())
            .limit(count)
        )).scalars().all()
        ids: List[int] = []
        for job in rows:
            job.status = "researching"
            job.stage = "starting"
            job.started_at = datetime.utcnow()
            ids.append(job.id)
        if ids:
            await db.commit()
        return ids
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[signal-research] claim skipped: {exc}")
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return []


# ── Progress bookkeeping ─────────────────────────────────────────────────────

async def _save(db, job: SignalResearchJob) -> None:
    try:
        db.add(job)
        await db.commit()
    except Exception as exc:  # noqa: BLE001 — progress is telemetry, never fatal
        logger.debug(f"[signal-research] progress write skipped: {exc}")
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass


async def _begin_step(db, job: SignalResearchJob, name: str) -> float:
    # Reassign rather than append in place: SQLAlchemy only sees a JSON column
    # as dirty when the attribute itself is replaced.
    steps = list(job.steps or [])
    steps.append({"name": name, "status": "running", "detail": "", "ms": 0})
    job.steps = steps
    job.stage = name
    job.progress = round((len(steps) - 1) / len(STEPS), 3)
    await _save(db, job)
    return time.monotonic()


async def _end_step(db, job: SignalResearchJob, started: float,
                    status: str, detail: str = "") -> None:
    steps = list(job.steps or [])
    if steps:
        steps[-1] = {
            **steps[-1], "status": status,
            "detail": str(detail)[:400],
            "ms": int((time.monotonic() - started) * 1000),
        }
    job.steps = steps
    job.progress = round(len(steps) / len(STEPS), 3)
    await _save(db, job)


# ── Context gathering ────────────────────────────────────────────────────────

async def _pair_knowledge(db, symbol: str) -> str:
    """What we already know about this instrument, from three memories."""
    parts: List[str] = [f"# What we already know about {symbol}"]

    try:
        from plugins.AiMarketAnalyst.backend.services.knowledge_service import (
            build_knowledge_prompt, query_knowledge,
        )

        rows = await query_knowledge(db, symbol=symbol, limit=8)
        block = build_knowledge_prompt(rows)
        if block:
            parts.append(block.strip())
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[signal-research] knowledge skipped for {symbol}: {exc}")

    try:
        from app.services import market_data
        from plugins.MT5TradingPlugin.backend.services import smc_memory

        market = "crypto" if market_data.classify(symbol) == "crypto" else "mt5"
        stats = await smc_memory.outcome_summary(db, market=market, symbol=symbol)
        if stats:
            parts.append(
                f"Realised SMC record on {symbol}: {stats['trades']} settled, "
                f"win rate {stats['win_rate'] * 100:.0f}%, avg {stats['avg_r']:+.2f}R "
                f"(avg MFE {stats['avg_mfe_r']:.2f}R / MAE {stats['avg_mae_r']:.2f}R)."
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[signal-research] smc memory skipped for {symbol}: {exc}")

    try:
        from app.services import analysis_journal

        block = await analysis_journal.memory_block_for(db, [symbol])
        if block:
            parts.append(block.strip())
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[signal-research] journal skipped for {symbol}: {exc}")

    return "\n\n".join(parts) if len(parts) > 1 else f"No prior record for {symbol} yet."


async def _stored_research(db, symbol: str) -> Tuple[str, List[str]]:
    """Findings the background loop already collected, plus their source URLs."""
    urls: List[str] = []
    lines: List[str] = ["# Research already on file (background loop)"]
    try:
        rows = await research_loop.active_findings(db, symbol=symbol, limit=10)
        rows += [
            r for r in await research_loop.active_findings(db, limit=14)
            if r.symbol is None
        ]
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[signal-research] stored findings skipped: {exc}")
        rows = []

    seen: Set[int] = set()
    for r in rows:
        if r.id in seen:
            continue
        seen.add(r.id)
        flag = "" if not r.speculative else " [unverified]"
        lines.append(f"- [{r.kind}]{flag} {r.headline} — {r.source_url or 'no source'}")
        if r.source_url:
            urls.append(r.source_url)

    if len(lines) == 1:
        lines.append("- nothing on file yet")
    return "\n".join(lines), urls


async def _web_news(symbol: str, *, deep: int = 2) -> Tuple[str, List[str]]:
    """Live web search + the RSS aggregate, with the top hits read in full.

    This is the same Google-News-RSS search the agents' ``web_search`` tool uses
    (``ai_tools._run_web_search`` → ``news_research.web_news_search``); it is
    called directly here so the article URLs survive as provenance rather than
    being flattened into a text blob.
    """
    from plugins.AgentPaulPlugin.backend.services import news_research

    items: List[Dict[str, Any]] = []
    for query in (f"{symbol} price analysis", f"{symbol} forecast news"):
        try:
            items += await news_research.web_news_search(query, limit=5)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[signal-research] web search '{query}' failed: {exc}")
    try:
        items += await news_research.news_for_symbol(symbol, limit=6)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[signal-research] feed news failed for {symbol}: {exc}")

    # Agent-Reach (Exa) — off unless AGENT_REACH_ENABLED. Same item shape as the
    # sources above, so it dedups/reads-in-full/provenances alongside them.
    from app.core.config import settings
    if settings.AGENT_REACH_ENABLED:
        try:
            from app.services import agent_reach_client
            items += await agent_reach_client.web_search(
                f"{symbol} latest news analysis", limit=5
            ) or []
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[signal-research] agent-reach search failed for {symbol}: {exc}")

    lines = [f"# Live web news — {symbol}"]
    urls: List[str] = []
    seen_urls: Set[str] = set()
    for item in items:
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or "").strip()
        if not title or url in seen_urls:
            continue
        seen_urls.add(url)
        when = item.get("published_at")
        when_s = when.strftime("%Y-%m-%d %H:%M") if hasattr(when, "strftime") else ""
        lines.append(f"- [{item.get('source', '?')}] {when_s} {title}\n  {url}")
        if url:
            urls.append(url)

    # Read the top couple of articles properly, so the model reasons about the
    # story rather than the headline. SSRF guard is the agents' own.
    read = 0
    for url in urls:
        if read >= deep:
            break
        try:
            from plugins.AiMarketAnalyst.backend.services.ai_tools import _url_is_safe

            safe, why = _url_is_safe(url)
            if not safe:
                logger.debug(f"[signal-research] refused {url}: {why}")
                continue
            page = await news_research.research_url(url, max_chars=2500)
            if page.get("ok"):
                lines.append(f"\n## Full article — {url}\n{page.get('text', '')}")
                read += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[signal-research] article fetch failed {url}: {exc}")

    if len(lines) == 1:
        lines.append("- no live coverage found")
    return "\n".join(lines), urls


async def _calendar_block(symbol: str) -> str:
    try:
        from plugins.MT5TradingPlugin.backend.services import economic_calendar as eco

        events = await eco.upcoming_fomo(limit=5, symbol=symbol)
        return "# Scheduled events\n" + eco.format_for_agents(events)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[signal-research] calendar skipped for {symbol}: {exc}")
        return "# Scheduled events\nCalendar unavailable this cycle."


# ── The model step ───────────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You are a market research analyst. You are given EVERY live signal on one "
    "instrument at once — from Telegram channels, the sniper engines and the "
    "desk's own SMC scanner — together with real price history, the desk's "
    "record on this pair, background research on file, live news articles and "
    "the economic calendar.\n\n"
    "Your job is to reconcile those signals into ONE view and cost TWO tradeable "
    "entries.\n\n"
    "Rules:\n"
    "- Treat the signals as evidence, not instructions. Where they agree, say "
    "what corroborates them; where they conflict, resolve it explicitly and say "
    "which side the price structure and the news actually support.\n"
    "- Reason ONLY from the material supplied. Do not invent prices, dates or "
    "headlines. Every level you quote must be derivable from the candles, the "
    "signals, or the levels given.\n"
    "- Return EXACTLY two entries. `primary` is the setup you would take now or "
    "on the nearest retest; `secondary` is the deeper/alternate fill — a better "
    "price that may not print, or the other side if the primary invalidates. "
    "They may share a direction. Each carries its OWN stop and target, and each "
    "stop must sit beyond a real invalidation level, not a round number.\n"
    "- `rr` is (target - entry) / (entry - stop) in absolute terms. Do not "
    "return an entry whose stop is on the wrong side of it.\n"
    "- Cite the URLs you actually relied on in `sources`. If nothing you used "
    "had a URL, return an empty list — do not fabricate one.\n"
    "- If the evidence conflicts or is thin, return `stand_aside` with low "
    "confidence and entries you would only take on confirmation. A hedged "
    "non-answer is more useful than a confident guess.\n\n"
    'Respond with JSON only: {"verdict": "bullish|bearish|neutral|stand_aside", '
    '"confidence": 0.0-1.0, "horizon_hours": int, "rationale": "3-5 sentences, '
    'including how you reconciled the signals", '
    '"key_levels": {"support": number|null, "resistance": number|null, '
    '"invalidation": number|null}, '
    '"entries": [{"label": "primary", "side": "buy|sell", "entry": number, '
    '"stop_loss": number, "take_profit": number, "rr": number, '
    '"confidence": 0.0-1.0, "trigger": "what has to happen to take it", '
    '"rationale": "one sentence"}, {"label": "secondary", ...}], '
    '"sources": ["url", ...]}'
)


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """The JSON object inside a model response, however it was wrapped.

    Not every provider honours ``json_mode``: some return a fenced block, some
    prepend "Here is my analysis:". Rejecting those costs a full cascade retry
    for output that was actually fine, so the object is dug out by brace
    matching before the schema is judged.
    """
    body = (text or "").strip()
    if not body:
        return None

    fence = body.find("```")
    if fence != -1:
        rest = body[fence + 3:]
        # Drop an optional language tag on the opening fence.
        if "\n" in rest:
            first, remainder = rest.split("\n", 1)
            rest = remainder if first.strip().isalpha() or not first.strip() else rest
        close = rest.find("```")
        body = (rest[:close] if close != -1 else rest).strip()

    start = body.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(body)):
        ch = body[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(body[start:i + 1])
                except (TypeError, ValueError):
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


def validate_prediction(content: Any) -> Optional[Dict[str, Any]]:
    """Schema gate for the model's output.

    Returning ``None`` makes ``analyze_with_cascade`` treat the response exactly
    like a transport error and try the next provider, so a model that ignores
    the format costs a retry rather than a corrupt row.
    """
    raw = content
    if isinstance(raw, str):
        raw = _extract_json(raw)
    if not isinstance(raw, dict):
        return None

    verdict = str(raw.get("verdict") or "").strip().lower()
    if verdict not in VERDICTS:
        return None
    rationale = str(raw.get("rationale") or "").strip()
    if not rationale:
        return None

    try:
        confidence = min(1.0, max(0.0, float(raw.get("confidence", 0.0))))
    except (TypeError, ValueError):
        return None
    try:
        horizon = int(raw.get("horizon_hours") or 8)
    except (TypeError, ValueError):
        horizon = 8

    levels = raw.get("key_levels")
    sources = [
        str(u).strip() for u in (raw.get("sources") or [])
        if research_loop._has_source(str(u).strip())
    ]
    return {
        "verdict": verdict,
        "confidence": confidence,
        "horizon_hours": max(1, min(horizon, 168)),
        "rationale": rationale[:2000],
        "key_levels": levels if isinstance(levels, dict) else {},
        "entries": _clean_entries(raw.get("entries"), confidence),
        "sources": sources[:8],
    }


def _clean_entries(raw: Any, fallback_confidence: float) -> List[Dict[str, Any]]:
    """Keep only entry plans that are actually tradeable.

    An entry whose stop sits on the profitable side of it is not a conservative
    plan, it is a broken one — and it would be executed. Anything that fails
    that check is dropped rather than corrected, because guessing the model's
    intent about someone's risk is worse than returning one good entry.
    Never raises: a malformed `entries` costs the entries, not the prediction.
    """
    out: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return out

    for index, item in enumerate(raw[:2]):
        if not isinstance(item, dict):
            continue
        side = str(item.get("side") or "").strip().lower()
        if side not in ("buy", "sell"):
            continue
        try:
            entry = float(item["entry"])
            stop = float(item["stop_loss"])
            target = float(item["take_profit"])
        except (KeyError, TypeError, ValueError):
            continue
        if entry <= 0 or stop <= 0 or target <= 0:
            continue
        # Stop below and target above for a buy; the reverse for a sell.
        if side == "buy" and not (stop < entry < target):
            continue
        if side == "sell" and not (target < entry < stop):
            continue

        risk = abs(entry - stop)
        reward = abs(target - entry)
        try:
            confidence = min(1.0, max(0.0, float(item.get("confidence", fallback_confidence))))
        except (TypeError, ValueError):
            confidence = fallback_confidence

        out.append({
            "label": str(item.get("label") or ("primary" if index == 0 else "secondary"))[:20],
            "side": side,
            "entry": entry,
            "stop_loss": stop,
            "take_profit": target,
            # Recomputed, never trusted: a model that says 3R about a 0.8R setup
            # is the one number a person would act on without checking.
            "rr": round(reward / risk, 2) if risk else None,
            "confidence": confidence,
            "trigger": str(item.get("trigger") or "")[:200],
            "rationale": str(item.get("rationale") or "")[:400],
        })
    return out


class _TwoEntryGate:
    """Validator that holds out for two costed entries.

    The prompt asks for exactly two — a fill now and the deeper alternate — but
    models routinely return one, or none on a ``stand_aside``, and a card that
    says "no tradeable entry" tells the desk nothing it can act on. Rejecting
    makes ``analyze_with_cascade`` move to the next provider, which is the same
    path a transport error takes, so a stingy model costs a retry instead of the
    answer.

    What it will not do is invent the missing entry. An entry, a stop and a
    target are numbers someone sizes real risk against; a fabricated second leg
    is worse than an honest single. So the best partial answer seen along the
    way is kept, and :func:`_predict` falls back to it when no provider produces
    two — one more pass would cost another full cascade for the same words.
    """

    def __init__(self) -> None:
        self.fallback: Optional[Dict[str, Any]] = None

    def __call__(self, content: Any) -> Optional[Dict[str, Any]]:
        pred = validate_prediction(content)
        if pred is None:
            return None
        if len(pred.get("entries") or []) >= 2:
            return pred
        best = len(self.fallback.get("entries") or []) if self.fallback else -1
        if len(pred.get("entries") or []) > best:
            self.fallback = pred
        return None


async def _provider_exclusions(db) -> Tuple[List[str], str]:
    """Labels to keep out of the cascade, and the policy that decided them.

    Idle-first: a provider that is currently serving a live /mt5-live request is
    not borrowed. If nothing frees up within ``IDLE_WAIT_S`` the exclusion set is
    dropped, because a research queue that never produces a prediction while the
    desk is busy is a research queue that never produces a prediction.
    """
    from plugins.AiMarketAnalyst.backend.services import analysis_router

    deadline = time.monotonic() + IDLE_WAIT_S
    while True:
        try:
            idle = await research_loop.idle_provider_labels(db)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[signal-research] idle lookup failed: {exc}")
            idle = []
        if idle:
            try:
                ranked = await analysis_router.rank_providers(db)
            except Exception:  # noqa: BLE001
                return [], "idle-first"
            idle_set = set(idle)
            return [p.label for p in ranked if p.label not in idle_set], "idle-first"

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return [], "primary-fallback"
        await asyncio.sleep(min(5.0, remaining))


def _has_answer(result: Dict[str, Any]) -> bool:
    """A cascade result the caller can actually persist.

    ``ok`` alone is not enough: a pass can report success with no content, and
    the job would be marked done with nothing to store.
    """
    return bool(result.get("ok") and result.get("content"))


async def _predict(db, job: SignalResearchJob, context: str) -> Dict[str, Any]:
    from plugins.AiMarketAnalyst.backend.services import analysis_router

    exclude, policy = await _provider_exclusions(db)
    user = (
        f"Instrument: {job.symbol}\n"
        f"Signals in this batch: {job.signal_count or 1} "
        f"(sources: {job.source})\n\n"
        f"{context}\n\n"
        f"Reconcile every signal above into one view of {job.symbol}, then give "
        f"exactly two entry plans."
    )
    # One gate across both passes, so a single-entry answer from an idle
    # provider is still available if the all-providers pass fares no better.
    gate = _TwoEntryGate()

    result = await analysis_router.analyze_with_cascade(
        db,
        [{"role": "system", "content": _SYSTEM_PROMPT},
         {"role": "user", "content": user}],
        validator=gate,
        temperature=0.2,
        max_tokens=800,
        json_mode=True,
        hard_timeout=PREDICT_TIMEOUT_S,
        total_budget=PREDICT_BUDGET_S,
        exclude=exclude,
        agent_name="signal_research",
        agent_role="market_analyst",
        source="signal_research",
    )

    # A rejected-for-one-entry pass is not a dead cascade: the providers
    # answered, they just would not cost a second leg. Escalating to every
    # provider would spend a whole second cascade to be told the same thing —
    # `stand_aside` verdicts legitimately carry no entries at all — so take the
    # best read now and let the card say how thin it is.
    if not _has_answer(result) and gate.fallback is not None:
        logger.info(
            f"[signal-research] {job.symbol}: no provider costed two entries; "
            f"keeping the best {len(gate.fallback.get('entries') or [])}-entry read"
        )
        return {
            "ok": True,
            "content": gate.fallback,
            "provider_used": result.get("provider_used"),
            "policy": f"{policy} → partial-entries",
            "errors": list(result.get("errors") or []),
        }

    # Idle-first is a courtesy to live analysis, not a reason to give up. If
    # every idle provider was dead or misconfigured, the ones we politely held
    # back are the only ones left — try them before reporting no analysis.
    if not result.get("ok") and exclude:
        logger.info(
            f"[signal-research] {job.symbol}: idle providers exhausted "
            f"({'; '.join(str(e) for e in (result.get('errors') or []))[:200]}) — "
            "retrying across every provider"
        )
        retry = await analysis_router.analyze_with_cascade(
            db,
            [{"role": "system", "content": _SYSTEM_PROMPT},
             {"role": "user", "content": user}],
            validator=gate,
            temperature=0.2,
            max_tokens=800,
            json_mode=True,
            hard_timeout=PREDICT_TIMEOUT_S,
            total_budget=PREDICT_BUDGET_S,
            agent_name="signal_research",
            agent_role="market_analyst",
            source="signal_research",
        )
        # `ok` without content is not an answer — the caller would mark the job
        # done with nothing to persist.
        if _has_answer(retry):
            retry["policy"] = f"{policy} → all-providers"
            return retry
        # Keep both passes' errors: which providers were skipped and why is the
        # whole diagnostic when nothing answers.
        retry["errors"] = list(result.get("errors") or []) + list(retry.get("errors") or [])
        result = retry
        policy = f"{policy} → all-providers"

    # The all-providers pass may itself have turned up a partial read.
    if not _has_answer(result) and gate.fallback is not None:
        return {
            "ok": True,
            "content": gate.fallback,
            "provider_used": result.get("provider_used"),
            "policy": f"{policy} → partial-entries",
            "errors": list(result.get("errors") or []),
        }

    result["policy"] = policy
    return result


# ── Persistence ──────────────────────────────────────────────────────────────

def _learn_prediction(job: SignalResearchJob, pred: Dict[str, Any],
                      entries: Sequence[Dict[str, Any]],
                      sources: Sequence[str],
                      provider_used: Optional[str]) -> None:
    """Teach all three JARVIS brains what this pair's research concluded.

    Deliberately NOT ``research_loop._fan_out``: that summarises a whole tick as
    "Research tick: N finding(s)" with an EMPTY symbol and no levels, so the one
    thing worth remembering — the costed entries — never reached the brains, and
    nothing that was learned could be recalled by instrument.

    Here the record is symbol-scoped and carries the entries, the reconciliation
    and the provenance, so a later "what do we think about XAUUSD" can answer
    with the actual plan rather than a headline count.
    """
    try:
        from app.api.jarvis import jarvis_learn_all_brains

        confidence = float(pred.get("confidence") or 0.0)
        verdict = str(pred.get("verdict") or "unknown")
        levels = pred.get("key_levels") or {}

        entry_lines = [
            f"- {e['label']}: {e['side'].upper()} @ {e['entry']:.6g} | "
            f"SL {e['stop_loss']:.6g} | TP {e['take_profit']:.6g} | "
            f"{e['rr']}R | confidence {e['confidence'] * 100:.0f}%"
            + (f" | trigger: {e['trigger']}" if e.get("trigger") else "")
            for e in entries
        ] or ["- no tradeable entry from this research"]

        summary = (
            f"{job.symbol}: {verdict} ({confidence * 100:.0f}%) from "
            f"{job.signal_count or 1} signal(s) [{job.source}]"
            + (f" — {len(entries)} entry plan(s)" if entries else " — no entry")
        )
        detail = "\n".join([
            f"Verdict: {verdict} at {confidence * 100:.0f}% confidence, "
            f"horizon {pred.get('horizon_hours')}h.",
            f"Reconciled from {job.signal_count or 1} live signal(s) on {job.symbol} "
            f"({job.source}).",
            "",
            "Entries:", *entry_lines,
            "",
            f"Key levels: support {levels.get('support')} | "
            f"resistance {levels.get('resistance')} | "
            f"invalidation {levels.get('invalidation')}",
            "",
            f"Reasoning: {pred.get('rationale', '')}",
            "",
            ("Sources:\n" + "\n".join(f"- {u}" for u in sources)) if sources
            else "Sources: none — this prediction is speculative and cannot gate a trade.",
            f"Model: {provider_used or 'unknown'}",
        ])

        jarvis_learn_all_brains(
            action="signal-research",
            symbol=job.symbol,
            summary=summary,
            detail=detail,
            tags=[
                "research", "prediction", "entries", verdict,
                *[s for s in (job.source or "").split("+") if s],
            ],
            # Weighted by the model's own confidence, and held back when nothing
            # verifiable backs it — a speculative call should not be recalled
            # with the same authority as a sourced one.
            importance=round(min(0.9, 0.35 + confidence * 0.5) * (0.6 if not sources else 1.0), 3),
            kind="prediction",
        )
    except Exception as exc:  # noqa: BLE001 — learning is never worth failing a job
        logger.debug(f"[signal-research] brain fan-out skipped: {exc}")


async def _persist_prediction(db, job: SignalResearchJob, pred: Dict[str, Any],
                              provider_used: Optional[str],
                              context_urls: Sequence[str]) -> Optional[int]:
    """Store the prediction as a ResearchFinding and publish it.

    Provenance follows ``research_loop``'s rule exactly: a prediction whose
    sources are all unresolvable is ``speculative`` and therefore excluded from
    ``gating_findings`` — it is shown on the page but can never gate a trade.
    """
    sources = list(pred.get("sources") or [])
    if not sources:
        # The model cited nothing usable; fall back to what it was actually shown.
        sources = [u for u in context_urls if research_loop._has_source(u)][:3]
    speculative = not any(research_loop._has_source(u) for u in sources)

    levels = pred.get("key_levels") or {}
    entries = list(pred.get("entries") or [])
    # Entry, stop and target on every line, spelled out. This is the part of the
    # card a person acts on, so it never abbreviates to "see rationale".
    entry_lines = "\n".join(
        f"- {e['label']}: {e['side'].upper()} entry {e['entry']:.6g} | "
        f"SL {e['stop_loss']:.6g} | TP {e['take_profit']:.6g} | "
        f"{e['rr']}R | conf {e['confidence'] * 100:.0f}%"
        + (f"\n  trigger: {e['trigger']}" if e.get("trigger") else "")
        + (f"\n  why: {e['rationale']}" if e.get("rationale") else "")
        for e in entries
    ) or "- no tradeable entry: no provider returned a plan whose stop sat on the risk side of its entry"
    if len(entries) == 1:
        entry_lines += (
            "\n- secondary: none — no provider costed a second plan for this batch"
        )
    # Entries lead. The card shows the first lines of this text before it is
    # opened, and the plan is what a person needs at a glance — the reasoning is
    # what they read after deciding it is worth reading.
    body = (
        f"Entries:\n{entry_lines}\n\n"
        f"{pred['rationale']}\n\n"
        f"horizon {pred['horizon_hours']}h | "
        f"support {levels.get('support')} | resistance {levels.get('resistance')} | "
        f"invalidation {levels.get('invalidation')}\n"
        f"compiled from {job.signal_count or 1} signal(s) [{job.source}]\n"
        + ("sources:\n" + "\n".join(f"- {u}" for u in sources) if sources else "no sources")
    )
    message = agent_bus.ResearchFindingMessage(
        kind=_PREDICTION_KIND,
        symbol=job.symbol,
        headline=f"{job.symbol}: {pred['verdict']} ({pred['confidence'] * 100:.0f}%)"[:400],
        body=body,
        source="signal-research",
        source_url=sources[0] if sources else None,
        confidence=float(pred["confidence"]),
        speculative=speculative,
        provider_used=provider_used,
        decay_at=research_loop._decay_at(_PREDICTION_KIND).isoformat(),
    )

    finding_id: Optional[int] = None
    try:
        row = ResearchFinding(
            kind=message.kind, symbol=message.symbol, headline=message.headline,
            body=message.body, source=message.source, source_url=message.source_url,
            confidence=message.confidence, speculative=message.speculative,
            provider_used=message.provider_used,
            decay_at=research_loop._decay_at(_PREDICTION_KIND),
        )
        db.add(row)
        await db.flush()
        finding_id = row.id
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[signal-research] finding write failed: {exc}")
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass

    await agent_bus.publish_finding(message)
    _learn_prediction(job, pred, entries, sources, provider_used)

    job.verdict = pred["verdict"]
    job.verdict_confidence = float(pred["confidence"])
    job.horizon_hours = int(pred["horizon_hours"])
    job.rationale = pred["rationale"]
    job.entries = entries
    job.sources = sources
    job.speculative = speculative
    job.finding_id = finding_id
    job.provider_used = provider_used
    return finding_id


# ── One job ──────────────────────────────────────────────────────────────────

def _summarise_failure(errors: Optional[Sequence[Any]]) -> str:
    """Turn a cascade's error list into something a person can act on.

    Raw, it is a wall of provider URLs and MDN links in which the one useful
    fact — that three providers are rejecting the API key — is invisible. The
    distinction that matters is misconfiguration (fix a key) versus a provider
    having a bad minute (wait).
    """
    items = [str(e) for e in (errors or []) if str(e).strip()]
    if not items:
        return "no prediction returned"

    needs_config, retired, transient = [], [], []
    for item in items:
        label = item.split(":", 1)[0].strip()
        if "needs configuring" in item:
            needs_config.append(label)
        elif "retired upstream" in item:
            retired.append(label)
        else:
            transient.append(item.split("\n", 1)[0][:140])

    parts: List[str] = []
    if needs_config:
        unique = list(dict.fromkeys(needs_config))
        parts.append(
            f"{len(unique)} provider(s) rejected our credentials — check the API key "
            f"and base URL for: {', '.join(unique)}"
        )
    if retired:
        unique = list(dict.fromkeys(retired))
        parts.append(
            f"{len(unique)} provider(s) have been retired by their vendor and will not "
            f"come back — replace: {', '.join(unique)}"
        )
    if transient:
        parts.append("; ".join(dict.fromkeys(transient)))
    return " | ".join(parts)[:2000]


def _signals_block(job: SignalResearchJob) -> str:
    """Every signal on this pair, laid out so the model can reconcile them."""
    members = list(job.signals or [])
    lines = [f"# Live signals on {job.symbol} ({len(members) or 1} in this batch)"]

    if not members:
        lines.append(
            f"- {job.source}: {job.direction or 'unspecified'} "
            f"entry={job.entry} stop={job.stop_loss} target={job.take_profit}"
        )
    else:
        for i, s in enumerate(members, 1):
            lines.append(
                f"{i}. [{s.get('source', '?')}] {str(s.get('direction') or '?').upper()} "
                f"entry={s.get('entry')} stop={s.get('stop_loss')} "
                f"target={s.get('take_profit')}"
            )
        buys = sum(1 for s in members if str(s.get("direction") or "").lower() == "buy")
        sells = len(members) - buys
        lines.append(
            f"\nDirection split: {buys} buy / {sells} sell — "
            + (f"consensus {job.direction.upper()}." if job.direction
               else "NO consensus; resolve this explicitly.")
        )
        entries = [s["entry"] for s in members if s.get("entry")]
        if len(entries) > 1:
            lines.append(
                f"Their entries span {min(entries):.6g} to {max(entries):.6g} — "
                "that spread is where the two entry plans should live."
            )
    return "\n".join(lines)


async def research_signal(db, job: SignalResearchJob) -> Dict[str, Any]:
    """Run the seven-step pipeline for one signal. Never raises.

    Every step is individually guarded: a dead news feed or an unpriceable
    symbol degrades the prediction, it does not fail the job. Only the model
    step can mark the job failed, because without it there is no prediction.
    """
    job.status = "researching"
    job.started_at = job.started_at or datetime.utcnow()
    job.steps = []
    await _save(db, job)

    context: List[str] = []
    context_urls: List[str] = []

    # 1 ── every live signal on this pair, together
    t = await _begin_step(db, job, "load_signal")
    context.append(_signals_block(job))
    members = list(job.signals or [])
    await _end_step(
        db, job, t, "done",
        f"{len(members) or 1} signal(s) on {job.symbol}"
        + (f", consensus {job.direction}" if job.direction else ", signals disagree"),
    )

    # 2 ── price movement across timeframes
    t = await _begin_step(db, job, "price_history")
    series: Dict[str, List[List[float]]] = {}
    for tf in TIMEFRAMES:
        series[tf] = await _candles(job.symbol, tf, limit=200)
    bars = sum(len(v) for v in series.values())
    context.append(_movement_block(job.symbol, series, job))
    context.append(_fib_block(job.symbol, series))
    await _end_step(db, job, t, "done" if bars else "empty", f"{bars} bars across {len(TIMEFRAMES)} timeframes")

    # 3 ── what we already know about the pair
    t = await _begin_step(db, job, "pair_knowledge")
    try:
        block = await _pair_knowledge(db, job.symbol)
        context.append(block)
        await _end_step(db, job, t, "done", f"{len(block)} chars")
    except Exception as exc:  # noqa: BLE001
        await _end_step(db, job, t, "error", str(exc))

    # 4 ── research the background loop already collected
    t = await _begin_step(db, job, "stored_research")
    try:
        block, urls = await _stored_research(db, job.symbol)
        context.append(block)
        context_urls += urls
        await _end_step(db, job, t, "done", f"{len(urls)} sourced finding(s)")
    except Exception as exc:  # noqa: BLE001
        await _end_step(db, job, t, "error", str(exc))

    # 5 ── live web + provider feeds
    t = await _begin_step(db, job, "web_news")
    try:
        block, urls = await _web_news(job.symbol)
        context.append(block)
        context_urls += urls
        await _end_step(db, job, t, "done", f"{len(urls)} article(s)")
    except Exception as exc:  # noqa: BLE001
        await _end_step(db, job, t, "error", str(exc))

    # 6 ── scheduled events
    t = await _begin_step(db, job, "calendar")
    block = await _calendar_block(job.symbol)
    context.append(block)
    await _end_step(db, job, t, "done")

    # 7 ── the prediction
    t = await _begin_step(db, job, "predict")
    try:
        result = await _predict(db, job, "\n\n".join(context))
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "errors": [str(exc)], "policy": "n/a"}

    if result.get("ok") and result.get("content"):
        await _persist_prediction(db, job, result["content"],
                                  result.get("provider_used"), context_urls)
        job.status = "done"
        job.stage = "done"
        job.error = None
        await _end_step(
            db, job, t, "done",
            f"{job.verdict} via {result.get('provider_used')} ({result.get('policy')})",
        )
    else:
        job.status = "failed"
        job.stage = "failed"
        job.error = _summarise_failure(result.get("errors"))
        await _end_step(db, job, t, "error", job.error)

    job.progress = 1.0
    job.finished_at = datetime.utcnow()
    await _save(db, job)

    return {
        "id": job.id, "symbol": job.symbol, "status": job.status,
        "verdict": job.verdict, "confidence": job.verdict_confidence,
        "speculative": job.speculative, "provider_used": job.provider_used,
        "error": job.error,
    }


async def _run_job(job_id: int) -> None:
    """Own session per job — concurrent jobs must never share an AsyncSession."""
    global _consecutive_failures
    from app.core.database import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            job = await db.get(SignalResearchJob, job_id)
            if job is None:
                return
            result = await research_signal(db, job)
            # Feeds the worker's backoff: a run of failures is almost always
            # every provider rate-limiting at once, and pushing through it keeps
            # the limit alive.
            if result.get("status") == "done":
                _consecutive_failures = 0
            else:
                _consecutive_failures += 1
        return
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error(f"🔬 [SIGNAL RESEARCH] job {job_id} crashed: {exc}")
        try:
            async with AsyncSessionLocal() as db:
                job = await db.get(SignalResearchJob, job_id)
                if job is not None:
                    job.status = "failed"
                    job.error = str(exc)[:2000]
                    job.finished_at = datetime.utcnow()
                    await db.commit()
        except Exception:  # noqa: BLE001
            pass


# ── Loop control (mirrors the research-loop pattern) ─────────────────────────

_worker_task: Optional[asyncio.Task] = None
_scan_task: Optional[asyncio.Task] = None
_running = False
_concurrency = DEFAULT_CONCURRENCY
_scan_interval = SCAN_INTERVAL_S
_started_at: Optional[str] = None
_last_scan: Optional[Dict[str, Any]] = None
_inflight: Set[asyncio.Task] = set()
#: Jobs that have failed back-to-back. Drives the worker's backoff.
_consecutive_failures = 0

#: How often the worker looks for claimable work while there IS work. Short, so
#: a job that finishes frees its slot immediately.
_POLL_S = 2.0

#: How far the poll backs off once the queue comes up empty. An idle queue used
#: to issue a SELECT every two seconds forever — 30 queries a minute, for the
#: entire life of the process, that find nothing. With SQL echo on that alone
#: writes tens of kilobytes a minute to stdout, and a stdout nobody is draining
#: fills its pipe and blocks the whole process.
_IDLE_POLL_S = 30.0

#: Wait this long after start before claiming anything. The queue is started
#: from the app's lifespan, so without a grace period it immediately floods a
#: still-booting process with five jobs' worth of HTTP and model calls — the API
#: then takes far longer to answer its first request, which reads as a hang.
_STARTUP_GRACE_S = 20.0


def active_count() -> int:
    """Jobs currently being researched in this process."""
    return len(_inflight)


def _reset_failures() -> None:
    global _consecutive_failures
    _consecutive_failures = 0


async def _worker_loop() -> None:
    global _running
    from app.core.database import AsyncSessionLocal

    logger.info(f"🔬 [SIGNAL RESEARCH] worker started — {_concurrency} concurrent")
    # Let the app finish coming up before competing with it for the event loop.
    try:
        await asyncio.sleep(_STARTUP_GRACE_S)
    except asyncio.CancelledError:
        return

    # This process owns nothing yet, so anything still flagged in-flight was
    # abandoned by a previous one (a reload, a crash). Take it all back.
    reclaim_cutoff_minutes = 0
    last_sweep = 0.0
    idle = False

    while _running:
        try:
            now = time.monotonic()
            if now - last_sweep >= 60.0:
                async with AsyncSessionLocal() as db:
                    await requeue_stale(db, older_than_minutes=reclaim_cutoff_minutes)
                reclaim_cutoff_minutes = STALE_MINUTES
                last_sweep = now

            # Everything failing usually means every provider is rate-limited.
            # Claiming more work would only extend the limit, so stand down and
            # let it clear.
            if _consecutive_failures >= FAILURE_BACKOFF_THRESHOLD and not _inflight:
                logger.warning(
                    f"🔬 [SIGNAL RESEARCH] {_consecutive_failures} failures in a row — "
                    f"pausing {FAILURE_BACKOFF_S:.0f}s"
                )
                await asyncio.sleep(FAILURE_BACKOFF_S)
                _reset_failures()
                continue

            free = _concurrency - len(_inflight)
            if free > 0:
                async with AsyncSessionLocal() as db:
                    ids = await _claim(db, free)
                for job_id in ids:
                    task = asyncio.create_task(_run_job(job_id))
                    _inflight.add(task)
                    task.add_done_callback(_inflight.discard)
                # Poll fast while there is work to pick up, slowly when the
                # queue is empty. The scan loop is what refills it, so an idle
                # worker has nothing to learn 30 times a minute.
                idle = not ids and not _inflight
            else:
                idle = False
        except asyncio.CancelledError:
            break
        except Exception as exc:  # noqa: BLE001
            logger.error(f"🔬 [SIGNAL RESEARCH] worker error: {exc}")
            idle = True

        try:
            await asyncio.sleep(_IDLE_POLL_S if idle else _POLL_S)
        except asyncio.CancelledError:
            break
    logger.info("🔬 [SIGNAL RESEARCH] worker stopped")


async def _scan_loop() -> None:
    global _last_scan
    from app.core.database import AsyncSessionLocal

    # Same reasoning as the worker: sweeping four signal tables while the app is
    # still starting up only delays the first request it can answer.
    try:
        await asyncio.sleep(_STARTUP_GRACE_S)
    except asyncio.CancelledError:
        return

    while _running:
        try:
            async with AsyncSessionLocal() as db:
                queued = await scan_and_enqueue(db)
            _last_scan = {"at": datetime.utcnow().isoformat(), "queued": queued}
        except asyncio.CancelledError:
            break
        except Exception as exc:  # noqa: BLE001
            logger.error(f"🔬 [SIGNAL RESEARCH] scan error: {exc}")
            _last_scan = {"at": datetime.utcnow().isoformat(), "error": str(exc)}

        try:
            await asyncio.sleep(_scan_interval)
        except asyncio.CancelledError:
            break


def start_signal_research_queue(concurrency: int = DEFAULT_CONCURRENCY,
                                scan_interval: int = SCAN_INTERVAL_S) -> bool:
    global _worker_task, _scan_task, _running, _concurrency, _scan_interval, _started_at

    if _worker_task is not None and not _worker_task.done():
        logger.warning("Signal research queue already running")
        return False

    # Floor of 1, ceiling of 10: the point of the cap is to leave provider
    # capacity for live analysis, and an unbounded value defeats it.
    _concurrency = max(1, min(int(concurrency or DEFAULT_CONCURRENCY), 10))
    _scan_interval = max(60, int(scan_interval or SCAN_INTERVAL_S))
    _reset_failures()
    _running = True
    _started_at = datetime.utcnow().isoformat()
    _worker_task = asyncio.create_task(_worker_loop())
    _scan_task = asyncio.create_task(_scan_loop())
    logger.info(
        f"🔬 Signal research queue started "
        f"(concurrency={_concurrency}, scan={_scan_interval}s)"
    )
    return True


def stop_signal_research_queue() -> bool:
    global _running, _worker_task, _scan_task, _started_at

    if not _running and (_worker_task is None or _worker_task.done()):
        logger.warning("Signal research queue is not running")
        return False

    _running = False
    for task in (_worker_task, _scan_task):
        if task:
            task.cancel()
    # Cancel the jobs themselves too. Each holds a database session and up to a
    # minute of HTTP and model calls (with retry backoff), so leaving them to
    # finish is what makes a reload or a shutdown hang instead of exiting —
    # the port stays bound, nothing serves, and it looks like a crash.
    # Whatever they were working on is reclaimed by `requeue_stale` on the next
    # start, so cancelling loses no work.
    for task in list(_inflight):
        task.cancel()
    _inflight.clear()
    _worker_task = None
    _scan_task = None
    _started_at = None
    logger.info("🔬 Signal research queue stopped")
    return True


async def queue_status(db=None) -> Dict[str, Any]:
    """Live queue state for the Research page header."""
    counts: Dict[str, int] = {}
    if db is not None:
        try:
            # COUNT in the database. The page polls this every couple of seconds
            # while work is moving, and materialising every row — each carrying a
            # step log — just to call len() on it does not scale past a few
            # hundred jobs.
            since = datetime.utcnow() - timedelta(hours=24)
            for status in ("queued", "researching", "done", "failed"):
                q = select(func.count(SignalResearchJob.id)).where(
                    SignalResearchJob.status == status
                )
                if status in ("done", "failed"):
                    q = q.where(SignalResearchJob.finished_at >= since)
                counts[status] = int((await db.execute(q)).scalar() or 0)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[signal-research] status counts skipped: {exc}")

    return {
        "running": _running,
        "concurrency": _concurrency,
        "active": active_count(),
        "consecutive_failures": _consecutive_failures,
        "cooldown_hours": RESEARCH_COOLDOWN_HOURS,
        "scan_interval_seconds": _scan_interval,
        "started_at": _started_at,
        "last_scan": _last_scan,
        "queued": counts.get("queued", 0),
        "researching": counts.get("researching", 0),
        "done_24h": counts.get("done", 0),
        "failed_24h": counts.get("failed", 0),
        "steps": list(STEPS),
    }


async def latest_plan(db, symbol: str) -> Optional[Dict[str, Any]]:
    """The current research plan for one instrument, or None.

    This is the read side every consumer shares — the sniper engines, the SMC
    setup builder and all four signal pages — so a pair's entries mean the same
    thing everywhere they appear.

    A plan past its own ``horizon_hours`` is not returned: a four-hour call on a
    pair that has since moved is worse than no call, because it still looks
    authoritative.
    """
    symbol = _norm(symbol)
    if not symbol:
        return None
    try:
        job = (await db.execute(
            select(SignalResearchJob)
            .where(
                SignalResearchJob.symbol == symbol,
                SignalResearchJob.status == "done",
                SignalResearchJob.finished_at.isnot(None),
            )
            .order_by(SignalResearchJob.finished_at.desc())
            .limit(1)
        )).scalars().first()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[signal-research] plan lookup skipped for {symbol}: {exc}")
        return None

    if job is None or job.finished_at is None:
        return None
    age_h = (datetime.utcnow() - job.finished_at).total_seconds() / 3600.0
    if age_h > float(job.horizon_hours or DECAY_FALLBACK_HOURS):
        return None

    return {
        "symbol": job.symbol,
        "verdict": job.verdict,
        "confidence": job.verdict_confidence,
        "horizon_hours": job.horizon_hours,
        "rationale": job.rationale,
        "entries": job.entries or [],
        "sources": job.sources or [],
        "speculative": bool(job.speculative),
        "signal_count": job.signal_count or 1,
        "signal_sources": job.source,
        "provider_used": job.provider_used,
        "job_id": job.id,
        "researched_at": job.finished_at.isoformat(),
        "age_hours": round(age_h, 2),
    }


async def plans_for(db, symbols: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """`latest_plan` for many instruments at once.

    Keyed by the CALLER'S spelling, not the normalised one. Normalisation
    handles broker decorations and doubled quotes ("XAUUSD.m", "GBPUSDUSDT"), so
    a client that had to reproduce it would drift out of step with the server.
    Echoing the requested string back means a page looks plans up with exactly
    the symbol it already has. The list pages render dozens of pairs, so this is
    one request rather than one per row.
    """
    out: Dict[str, Dict[str, Any]] = {}
    resolved: Dict[str, Optional[Dict[str, Any]]] = {}

    for requested in list(dict.fromkeys(s for s in symbols if s))[:80]:
        canonical = _norm(requested)
        if not canonical:
            continue
        if canonical not in resolved:
            resolved[canonical] = await latest_plan(db, canonical)
        plan = resolved[canonical]
        if plan:
            out[requested] = plan
    return out


def serialize_job(job: SignalResearchJob) -> Dict[str, Any]:
    return {
        "id": job.id,
        "symbol": job.symbol,
        "source": job.source,
        "signal_ref": job.signal_ref,
        "signal_refs": job.signal_refs or [],
        "signals": job.signals or [],
        "signal_count": job.signal_count or 1,
        "entries": job.entries or [],
        "direction": job.direction,
        "entry": job.entry,
        "stop_loss": job.stop_loss,
        "take_profit": job.take_profit,
        "status": job.status,
        "stage": job.stage,
        "progress": float(job.progress or 0.0),
        "steps": job.steps or [],
        "verdict": job.verdict,
        "verdict_confidence": job.verdict_confidence,
        "horizon_hours": job.horizon_hours,
        "rationale": job.rationale,
        "sources": job.sources or [],
        "speculative": bool(job.speculative),
        "finding_id": job.finding_id,
        "provider_used": job.provider_used,
        "error": job.error,
        "queued_at": job.queued_at.isoformat() if job.queued_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


async def list_jobs(db, *, status: Optional[str] = None, symbol: Optional[str] = None,
                    limit: int = 40) -> List[Dict[str, Any]]:
    q = select(SignalResearchJob).order_by(SignalResearchJob.queued_at.desc()).limit(limit)
    if status:
        q = q.where(SignalResearchJob.status.in_([s.strip() for s in status.split(",") if s.strip()]))
    if symbol:
        q = q.where(SignalResearchJob.symbol == _norm(symbol))
    try:
        return [serialize_job(j) for j in (await db.execute(q)).scalars().all()]
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[signal-research] list_jobs skipped: {exc}")
        return []
