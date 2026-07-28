"""Telegram Sniper auto-trade engine.

Takes active parsed signals, re-analyses each against the live market price to
find a better ("sniper") entry, then executes paper trades into the core
simulation account so they appear on the /trading page.

Flow per signal (idempotent, one sniper trade per signal):
  1. Re-analyse: validate the signal isn't stale, pick the nearest unhit TP,
     and compute an optimised limit entry that improves the fill.
  2. If the live price already satisfies the sniper entry → place immediately.
     Otherwise create a PENDING plan and wait for price to come to us.
  3. On later ticks, pending plans are re-checked: fill when triggered, or mark
     MISSED if price hits the stop / first target first or the TTL expires.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from plugins.TelegramSignalNewsPlugin.backend.models import (
    SignalStatus,
    SniperTradeStatus,
    TelegramParsedSignal,
    TelegramSniperSettings,
    TelegramSniperTrade,
)
from plugins.TelegramSignalNewsPlugin.backend.services.strategy_analysis import (
    analyze_entry,
    volume_snapshot,
    _fetch_ohlcv as _fetch_ta_ohlcv,
)
from plugins.TelegramSignalNewsPlugin.backend.timezone_utils import now_utc_naive


def _utcnow() -> datetime:
    return now_utc_naive()


# ── Volume gate ──────────────────────────────────────────────────────────────
# Volume is a hard precondition for every sniper entry. The context is resolved
# from the same exchange OHLCV the TA layer already uses; when it cannot be
# established the signal is recorded as NO_TRADE rather than sniped on price.

#: 15m × 200 bars = ~50 hours, comfortably more than the rolling 24h the volume
#: context requires, and it is the series `analyze_entry` already pulls.
_VOL_TF = "15m"
_VOL_LIMIT = 200


async def resolve_volume(symbol: str) -> Any:
    """Resolve the :class:`VolumeContext` for a telegram signal symbol.

    Returns a context whose ``status`` is OK / STALE / INSUFFICIENT /
    UNAVAILABLE. Never raises — a failure to resolve is itself a NO_TRADE.
    """
    from plugins.KronosForecastPlugin.backend.services import volume_context as volctx

    try:
        rows = await _fetch_ta_ohlcv(symbol, _VOL_TF, _VOL_LIMIT)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Sniper volume fetch failed for {}: {}", symbol, exc)
        rows = None
    return await volctx.resolve_volume_context(
        symbol=symbol, timeframe=_VOL_TF, rows=rows, fetcher=None,
    )


def volume_gate_note(ctx: Any) -> str:
    """One-line, DB-safe summary of the volume evidence behind a decision."""
    from plugins.KronosForecastPlugin.backend.services import volume_context as volctx

    if ctx is None:
        return "NO_TRADE · volume context unresolved"
    if ctx.status != "OK":
        return f"NO_TRADE · volume {ctx.status} · {ctx.detail}"[:400]
    return " · ".join(volctx.volume_evidence_lines(ctx))[:400]


def volume_supports(direction: str, ctx: Any) -> tuple[bool, str]:
    """Apply the direction rules to a resolved volume context.

    Same rules as the Kronos forecast:
      • rising price + rising relative volume  → continuation, supports the side
      • rising price + falling relative volume → exhaustion, weakens the side
      • climactic volume against the move      → reversal risk, blocks the side

    Returns ``(supported, reason)``. ``supported`` False means the entry must
    not auto-execute.
    """
    from plugins.KronosForecastPlugin.backend.services import volume_context as volctx

    if ctx is None or ctx.status != "OK":
        return False, f"volume {(ctx.status.lower() if ctx else 'unresolved')}"

    # A long maps to the forecast's "up", a short to "down".
    fdir = "up" if (direction or "").lower() == "long" else "down"
    if volctx.is_reversal_risk(fdir, ctx):
        return False, (
            f"CLIMACTIC volume (x{ctx.relative_volume:.2f}) is confirming the "
            f"opposite move ({ctx.divergence}) — reversal risk"
        )
    if ctx.regime == "DEAD":
        return False, (
            f"DEAD volume regime (x{ctx.relative_volume:.2f} of the 24h hourly "
            f"mean) — too thin to trust the direction"
        )
    exhaustion = "EXHAUSTION_UP" if fdir == "up" else "EXHAUSTION_DOWN"
    if ctx.divergence == exhaustion:
        return False, (
            f"{ctx.divergence} — price is still moving but volume is fading "
            f"({ctx.volume_slope_norm:+.2%}/h); the move is running dry"
        )
    confirmation = "CONFIRMED_UP" if fdir == "up" else "CONFIRMED_DOWN"
    if ctx.divergence == confirmation:
        return True, (
            f"{ctx.regime} volume x{ctx.relative_volume:.2f} and {ctx.divergence} "
            f"— participation is behind the move"
        )
    return True, f"{ctx.regime} volume x{ctx.relative_volume:.2f}, {ctx.divergence}"


@dataclass(slots=True)
class SniperPlan:
    ok: bool
    reason: str
    sniper_entry: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    risk_reward: float | None = None
    trigger_now: bool = False


async def get_or_create_settings(db: AsyncSession) -> TelegramSniperSettings:
    result = await db.execute(select(TelegramSniperSettings).limit(1))
    settings = result.scalars().first()
    if settings is None:
        settings = TelegramSniperSettings()
        db.add(settings)
        await db.commit()
        await db.refresh(settings)
    return settings


async def _get_live_price(symbol: str) -> float | None:
    """Fetch the live last price for a telegram signal symbol.

    Handles every symbol format telegram channels send (bare 'CRV', glued
    'CRVUSDT', already-slashed 'CRV/USDT'). Tries:
      1. Bitget SPOT  (CRV/USDT)
      2. Bitget FUTURES (CRV/USDT:USDT)
      3. Binance SPOT
      4. OKX, Bybit
    Returns the first valid non-zero price found.
    """
    try:
        from app.exchanges.manager import exchange_manager, SupportedExchange
    except Exception:
        return None

    cands = _symbol_candidates(symbol)
    if not cands:
        return None

    for cand in cands:
        # ── Bitget: try spot first, then futures ──────────────────────────
        bitget = exchange_manager.get_exchange(SupportedExchange.BITGET)
        if bitget is not None:
            # Spot (skip the automatic swap conversion by calling exchange directly)
            try:
                ticker = await bitget.exchange.fetch_ticker(cand)
                price = ticker.get("last") or ticker.get("close")
                if price:
                    return float(price)
            except Exception:
                pass
            # Futures  (CRV/USDT:USDT)
            for quote in ("USDT", "USDC"):
                if cand.endswith(f"/{quote}"):
                    futures_sym = f"{cand}:{quote}"
                    try:
                        ticker = await bitget.exchange.fetch_ticker(futures_sym)
                        price = ticker.get("last") or ticker.get("close")
                        if price:
                            return float(price)
                    except Exception:
                        pass

        # ── Other exchanges: Binance, OKX, Bybit ─────────────────────────
        for ex_name in ("BINANCE", "OKX", "BYBIT"):
            ex = getattr(SupportedExchange, ex_name, None)
            if ex is None:
                continue
            conn = exchange_manager.get_exchange(ex)
            if conn is None:
                continue
            try:
                ticker = await conn.exchange.fetch_ticker(cand)
                price = ticker.get("last") or ticker.get("close")
                if price:
                    return float(price)
            except Exception:
                pass

    logger.debug("Sniper live price fetch failed for all candidates of {}", symbol)
    return None


def _symbol_candidates(symbol: str) -> list[str]:
    """Return tradeable-pair candidates for a (possibly bare) telegram symbol."""
    s = (symbol or "").upper().strip()
    if not s:
        return []
    if "/" in s:
        return [s]
    quotes = ("USDT", "USDC", "USD", "BTC", "ETH")
    # XXXUSDT -> XXX/USDT
    for q in quotes:
        if s.endswith(q) and len(s) > len(q):
            return [f"{s[:-len(q)]}/{q}", f"{s[:-len(q)]}/USDT"]
    # bare base -> try common quotes
    return [f"{s}/USDT", f"{s}/USDC"]


def normalize_symbol(symbol: str) -> str:
    """Best tradeable-pair form of a telegram symbol (first candidate)."""
    cands = _symbol_candidates(symbol)
    return cands[0] if cands else (symbol or "")


async def _count_open_positions(db: AsyncSession, mode: str) -> int:
    """Count PLACED sniper trades executed on a given target ('sandbox' | 'live')."""
    res = await db.execute(
        select(func.count(TelegramSniperTrade.id)).where(
            TelegramSniperTrade.status == SniperTradeStatus.PLACED,
            TelegramSniperTrade.executed_mode.ilike(f"%{mode}%"),
        )
    )
    return int(res.scalar() or 0)


def reanalyze_signal(
    *,
    direction: str,
    signal_entry: float | None,
    stop_loss: float | None,
    take_profits: list[float],
    live_price: float,
    offset_pct: float,
    min_rr: float,
    default_sl_pct: float = 5.0,
) -> SniperPlan:
    """Compute an optimised sniper entry, or reject the signal.

    For a LONG we try to fill on a dip *below* the reference price; for a SHORT
    we try to fill on a pop *above* it. We also verify the trade still has room
    to the nearest unhit take-profit and an acceptable reward/risk ratio. When
    the signal has no numeric stop, a protective stop is derived from
    ``default_sl_pct`` so every trade is risk-managed.
    """
    direction = (direction or "").lower()
    if direction not in {"long", "short"}:
        return SniperPlan(ok=False, reason="Unknown direction")

    # Sanity-check: if the parsed signal_entry is wildly different from the live
    # price (more than 50% away), treat it as a bad parse and fall back to live.
    if signal_entry and live_price and signal_entry > 0:
        ratio = signal_entry / live_price
        if ratio < 0.1 or ratio > 10:
            signal_entry = None  # use live_price as ref instead

    ref = signal_entry if signal_entry and signal_entry > 0 else live_price
    offset = max(0.0, offset_pct) / 100.0
    sl_pct = max(0.0, default_sl_pct) / 100.0
    tps = [t for t in (take_profits or []) if t and t > 0]

    if direction == "long":
        # Stale guards
        if stop_loss and live_price <= stop_loss:
            return SniperPlan(ok=False, reason="Price already at/below stop loss")
        targets = sorted(t for t in tps if t > live_price)
        if tps and not targets:
            return SniperPlan(ok=False, reason="All take-profits already passed")
        take_profit = targets[0] if targets else None
        # Optimised entry: the better (lower) of signal entry and live, minus offset.
        sniper_entry = min(ref, live_price) * (1.0 - offset)
        sniper_entry = min(sniper_entry, live_price)  # never above live
        trigger_now = live_price <= sniper_entry
        # Fallback protective stop below entry
        if not stop_loss:
            stop_loss = round(sniper_entry * (1.0 - sl_pct), 10)
    else:  # short
        if stop_loss and live_price >= stop_loss:
            return SniperPlan(ok=False, reason="Price already at/above stop loss")
        targets = sorted((t for t in tps if t < live_price), reverse=True)
        if tps and not targets:
            return SniperPlan(ok=False, reason="All take-profits already passed")
        take_profit = targets[0] if targets else None
        sniper_entry = max(ref, live_price) * (1.0 + offset)
        sniper_entry = max(sniper_entry, live_price)  # never below live
        trigger_now = live_price >= sniper_entry
        # Fallback protective stop above entry
        if not stop_loss:
            stop_loss = round(sniper_entry * (1.0 + sl_pct), 10)

    # Reward / risk
    risk_reward = None
    if stop_loss and take_profit and sniper_entry:
        risk = abs(sniper_entry - stop_loss)
        reward = abs(take_profit - sniper_entry)
        if risk > 0:
            risk_reward = reward / risk
            if risk_reward < min_rr:
                return SniperPlan(
                    ok=False,
                    reason=f"Poor reward/risk {risk_reward:.2f} < {min_rr:.2f}",
                )

    return SniperPlan(
        ok=True,
        reason="Sniper entry planned",
        sniper_entry=round(sniper_entry, 10),
        stop_loss=stop_loss,
        take_profit=take_profit,
        risk_reward=round(risk_reward, 2) if risk_reward else None,
        trigger_now=trigger_now,
    )


async def _ai_entry_opinion(
    *,
    symbol: str,
    direction: str,
    signal_entry: float | None,
    sniper_entry: float | None,
    stop_loss: float | None,
    take_profit: float | None,
    live_price: float,
    rsi: float | None,
    support: float | None,
    resistance: float | None,
    opposite_volume: bool,
    volume_ratio: float | None = None,
    volume_ctx: Any = None,
) -> dict | None:
    """Ask the configured AI providers (AiMarketAnalyst) to validate the entry.

    Runs ONLY for telegram signals (this is the intended token consumer), and
    explicitly confirms the signal's DIRECTION against order-flow volume before
    approving. Returns {decision: enter|wait|skip, entry, confidence,
    direction_confirmed, info, note} or None if no AI is configured / available.
    Fully graceful — if the AI plugin is absent the sniper proceeds on TA alone.
    """
    try:
        from app.core.database import AsyncSessionLocal
        from plugins.AiMarketAnalyst.backend.services.ai_router import db_chat, parse_json_content
    except Exception:
        return None

    sys_prompt = (
        "You are a precise crypto futures entry strategist for Telegram signals. "
        "Given a signal plus live market data, decide the BEST limit entry to "
        "maximise the run to take-profit while protecting risk. "
        "CRITICAL: first CONFIRM the signal DIRECTION using volume. volume_context "
        "carries measured evidence — 24h volume, the last completed 1h volume, "
        "relative_volume vs the 24h hourly mean, the regime "
        "(DEAD/NORMAL/ELEVATED/CLIMACTIC) and the price-volume divergence. Rising "
        "price on rising relative volume is continuation; rising price on falling "
        "relative volume is exhaustion; climactic volume against the move is "
        "reversal risk. A DEAD regime or an exhaustion divergence in the signal's "
        "own direction must set direction_confirmed=false. "
        "If high_opposite_volume is true or volume_ratio (opposing/total) is >= 0.6, "
        "the pair is likely moving AGAINST the signal — set direction_confirmed=false "
        "and prefer 'skip' (or 'wait' for a much safer entry). Only 'enter' when "
        "volume supports the direction. Respond with STRICT JSON: "
        '{"decision":"enter|wait|skip","entry":<number>,"confidence":<0-1>,'
        '"direction_confirmed":<true|false>,"info":"<1-sentence pair read>",'
        '"note":"<short reason>"}. '
        "Also weigh sox_ml_forecast when present (an ML K-line forecast with "
        "direction/pct_change/confidence over the next candles): if it strongly "
        "opposes the signal direction with decent confidence, prefer 'wait' or 'skip'."
    )
    kronos_fc = None
    try:
        from plugins.KronosForecastPlugin.backend.services import forecast_service as _kronos
        _ksym = symbol if "/" in symbol else symbol.replace("USDT", "/USDT").replace("USDC", "/USDC")
        _kfc = await _kronos.run_forecast_cached("bitget", _ksym, "15m", pred_len=12)
        if _kfc and _kfc.signal:
            _ks = _kfc.signal
            kronos_fc = {
                "direction": _ks.direction,
                "pct_change": round(_ks.pct_change, 3),
                "confidence": round(_ks.confidence, 3),
                "engine": _kfc.engine,
            }
    except Exception:  # noqa: BLE001
        kronos_fc = None
    user = json.dumps(
        {
            "symbol": symbol,
            "direction": direction,
            "signal_entry": signal_entry,
            "proposed_sniper_entry": sniper_entry,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "live_price": live_price,
            "rsi": rsi,
            "support": support,
            "resistance": resistance,
            "high_opposite_volume": opposite_volume,
            "volume_ratio": volume_ratio,
            # Resolved volume gate (measured, never estimated): 24h volume, the
            # last completed hour, relative volume, regime and divergence.
            "volume_context": (volume_ctx.model_dump() if volume_ctx is not None else None),
            "sox_ml_forecast": kronos_fc,
        },
        default=str,
    )
    try:
        async with AsyncSessionLocal() as ai_db:
            res = await db_chat(
                ai_db,
                [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user},
                ],
                temperature=0.1,
                max_tokens=220,
                json_mode=True,
                agent_name="telegram_sniper",
                agent_role="sniper_entry",
                source="telegram",
            )
        if not res.get("ok"):
            return None
        parsed = parse_json_content(res.get("content"))
        if not isinstance(parsed, dict):
            return None
        parsed["_provider"] = res.get("provider")
        return parsed
    except Exception:  # noqa: BLE001
        return None


async def _agent_confirm(symbol: str, direction: str) -> dict | None:
    """Confirm the signal direction with the CORE AI agent pipeline.

    Runs the full Market → Sentiment → Signal → Risk agent pipeline (which is
    routed through the providers connected on /telegram-signals) with the
    'telegram' token trigger so it spends only on telegram signals. Returns
    {confirmed, action, confidence, note} or None if agents are unavailable.
    """
    try:
        from app.core.database import AsyncSessionLocal
        from app.agents.orchestrator import AgentOrchestrator
    except Exception:
        return None
    try:
        async with AsyncSessionLocal() as adb:
            res = await AgentOrchestrator.analyze_symbol(adb, symbol, "1h", trigger="telegram")
        if not isinstance(res, dict) or res.get("token_skipped") or res.get("error"):
            return None
        action = str(res.get("final_action") or "hold").lower()
        try:
            conf = float(res.get("final_confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        want = "buy" if direction.lower() == "long" else "sell"
        confirmed = action == want and conf >= 0.5
        note = f"agents:{action} {round(conf * 100)}%"
        reasoning = res.get("final_reasoning")
        if reasoning:
            note += f" — {str(reasoning)[:110]}"
        return {"confirmed": confirmed, "action": action, "confidence": conf, "note": note}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Agent confirmation failed for {}: {}", symbol, exc)
        return None


async def _execute_live(
    db: AsyncSession,
    *,
    symbol: str,
    direction: str,
    entry: float,
    stop_loss: float | None,
    take_profit: float | None,
    leverage: int,
) -> dict[str, Any]:
    """Place a REAL order on live via the core LiveTradeEngine.

    Creates a transient core Signal then delegates to LiveTradeEngine.execute_signal
    which enforces leverage, margin, risk %, exposure caps and the dry_run flag.
    """
    try:
        import json as _json
        from app.models.database import Signal, SignalSource, SignalAction, SignalStatus
        from app.trading.live import LiveTradeEngine
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"Live engine unavailable: {exc}"}

    symbol = normalize_symbol(symbol)
    side = "buy" if direction.lower() == "long" else "sell"
    try:
        sig = Signal(
            source=SignalSource.SYSTEM.value,
            symbol=symbol,
            action=SignalAction.BUY.value if side == "buy" else SignalAction.SELL.value,
            price=entry or 0,
            timeframe="1h",
            strength=0.8,
            confidence=0.8,
            status=SignalStatus.PENDING.value,
            raw_data=_json.dumps({"source": "telegram_sniper", "direction": direction}, default=str),
            indicators=_json.dumps(
                {"stop_loss": stop_loss, "take_profit": take_profit, "leverage": leverage}, default=str
            ),
        )
        db.add(sig)
        await db.flush()
        res = await LiveTradeEngine.execute_signal(db, sig.id)
        if isinstance(res, dict) and res.get("error"):
            return {"success": False, "error": res["error"], "signal_id": sig.id}
        order_id = None
        if isinstance(res, dict):
            order_id = res.get("order_id") or (res.get("order") or {}).get("id") or res.get("id")
        return {"success": True, "order_id": str(order_id) if order_id else None, "raw": res, "signal_id": sig.id}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)[:200]}


async def _store_trade_knowledge(
    db: AsyncSession,
    *,
    symbol: str,
    direction: str,
    mode: str,
    entry: float | None,
    confirmed: bool,
    note: str,
) -> None:
    """Persist what was learnt about a telegram trade so agents can reference it."""
    try:
        from plugins.AiMarketAnalyst.backend.services import knowledge_service
    except Exception:
        return
    try:
        status = "confirmed" if confirmed else "manual/unconfirmed"
        await knowledge_service.store_knowledge(
            db,
            content=f"Telegram {direction.upper()} {symbol} → {mode} @ {entry} · {status} · {note[:150]}",
            agent_role="sniper_entry",
            symbol=symbol,
            kind="outcome",
            title=f"Telegram trade {symbol}",
            weight=1.5 if confirmed else 1.0,
            source="telegram_sniper",
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("store_trade_knowledge skipped: {}", exc)


async def _place_sim_order(
    db: AsyncSession,
    *,
    symbol: str,
    direction: str,
    entry: float,
    stop_loss: float | None,
    take_profit: float | None,
    size_usdt: float,
    leverage: int,
    margin_mode: str,
    trade_type: str,
) -> dict[str, Any]:
    """Execute the sniper trade into the core simulation account."""
    from app.trading.simulation import SimulationEngine

    symbol = normalize_symbol(symbol)
    side = "buy" if direction == "long" else "sell"
    amount = size_usdt / entry if entry > 0 else 0.0
    if amount <= 0:
        return {"success": False, "error": "Invalid amount"}

    return await SimulationEngine.place_order(
        db=db,
        symbol=symbol,
        side=side,
        amount=amount,
        price=entry,
        order_type="limit",
        stop_loss=stop_loss,
        take_profit=take_profit,
        sl_type="signal",
        trade_type=trade_type,
        margin_mode=margin_mode,
        leverage=leverage if trade_type == "futures" else None,
    )


async def run_sniper_cycle(db: AsyncSession) -> dict[str, Any]:
    """One sniper tick: re-analyse new signals and fill pending plans."""
    settings = await get_or_create_settings(db)
    if not settings.enabled:
        return {"enabled": False, "placed": 0, "pending": 0, "skipped": 0, "missed": 0}

    placed = pending = skipped = missed = 0

    allowed = settings.allowed_channel_ids_json or None

    # Count currently-open sniper positions (placed and not yet closed elsewhere),
    # tracked separately for sandbox (demo) and live so each cap is independent.
    open_sandbox = await _count_open_positions(db, "sandbox")
    open_live = await _count_open_positions(db, "live")
    open_positions = open_sandbox + open_live

    # ── 1. Re-check existing PENDING plans ───────────────────────────────
    pend_res = await db.execute(
        select(TelegramSniperTrade).where(TelegramSniperTrade.status == SniperTradeStatus.PENDING)
    )
    for trade in pend_res.scalars().all():
        live = await _get_live_price(trade.symbol)
        if live is None:
            pending += 1
            continue

        # Expire by TTL
        age_min = (_utcnow() - trade.created_at).total_seconds() / 60.0
        if age_min > settings.pending_ttl_minutes:
            trade.status = SniperTradeStatus.MISSED
            trade.reason = "Pending entry expired (TTL)"
            trade.updated_at = _utcnow()
            missed += 1
            continue

        d = trade.direction
        # Missed if SL or first TP reached before our entry
        if trade.stop_loss and (
            (d == "long" and live <= trade.stop_loss) or (d == "short" and live >= trade.stop_loss)
        ):
            trade.status = SniperTradeStatus.MISSED
            trade.reason = "Stop loss reached before entry filled"
            trade.updated_at = _utcnow()
            missed += 1
            continue
        if trade.take_profit and (
            (d == "long" and live >= trade.take_profit) or (d == "short" and live <= trade.take_profit)
        ):
            trade.status = SniperTradeStatus.MISSED
            trade.reason = "Target reached before entry filled"
            trade.updated_at = _utcnow()
            missed += 1
            continue

        triggered = (
            (d == "long" and live <= (trade.sniper_entry or live))
            or (d == "short" and live >= (trade.sniper_entry or live))
        )
        if triggered:
            # Volume gate re-checked at fill time: the context that justified the
            # plan may have gone stale or turned against the trade while pending.
            fill_ctx = await resolve_volume(trade.symbol)
            fill_ok, fill_why = volume_supports(d, fill_ctx)
            if not fill_ok:
                trade.volume_confirmed = False
                trade.reason = f"Fill blocked — {fill_why} · {volume_gate_note(fill_ctx)}"
                trade.updated_at = _utcnow()
                pending += 1
                continue
            trade.volume_confirmed = True
        if triggered and open_positions < settings.max_positions:
            result = await _place_sim_order(
                db,
                symbol=trade.symbol,
                direction=d,
                entry=trade.sniper_entry or live,
                stop_loss=trade.stop_loss,
                take_profit=trade.take_profit,
                size_usdt=trade.position_size_usdt or settings.position_size_usdt,
                leverage=trade.leverage or settings.leverage,
                margin_mode=settings.margin_mode,
                trade_type=settings.trade_type,
            )
            if result.get("success"):
                trade.status = SniperTradeStatus.PLACED
                trade.reason = "Pending entry triggered and filled"
                trade.sim_order_id = result.get("order_id")
                trade.updated_at = _utcnow()
                open_positions += 1
                placed += 1
            else:
                trade.status = SniperTradeStatus.FAILED
                trade.reason = f"Fill failed: {result.get('error')}"
                trade.updated_at = _utcnow()
        else:
            pending += 1

    # ── 2. Plan brand-new ACTIVE signals not yet sniped ──────────────────
    sniped_ids_res = await db.execute(select(TelegramSniperTrade.signal_id))
    sniped_ids = {row[0] for row in sniped_ids_res.all()}

    sig_q = (
        select(TelegramParsedSignal)
        .where(TelegramParsedSignal.status == SignalStatus.ACTIVE)
        .order_by(TelegramParsedSignal.created_at.desc())
        .limit(50)
    )
    sig_res = await db.execute(sig_q)
    for sig in sig_res.scalars().all():
        if sig.id in sniped_ids:
            continue
        if allowed is not None and sig.channel_source_id not in allowed:
            continue
        if (sig.confidence or 0) < settings.min_confidence:
            db.add(_skip_record(sig, settings, "Below confidence threshold"))
            skipped += 1
            continue

        live = await _get_live_price(sig.symbol)
        if live is None:
            # Cannot price it (symbol may not be on Bitget) — skip, don't retry forever
            db.add(_skip_record(sig, settings, "No live price for symbol"))
            skipped += 1
            continue

        # ── Volume gate: resolved BEFORE any entry is planned. A signal whose
        # volume cannot be established is recorded as NO_TRADE, never sniped on
        # price alone. ───────────────────────────────────────────────────────
        vol_ctx = await resolve_volume(sig.symbol)
        if vol_ctx.status != "OK":
            rec = _skip_record(
                sig, settings,
                f"NO_TRADE — volume {vol_ctx.status.lower()}: {vol_ctx.detail}",
                live=live,
            )
            rec.volume_confirmed = False
            db.add(rec)
            skipped += 1
            continue
        vol_ok, vol_why = volume_supports(sig.direction, vol_ctx)
        vol_note = volume_gate_note(vol_ctx)

        plan = reanalyze_signal(
            direction=sig.direction,
            signal_entry=sig.entry,
            stop_loss=sig.stop_loss,
            take_profits=sig.take_profits_json or [],
            live_price=live,
            offset_pct=settings.sniper_offset_pct,
            min_rr=settings.min_risk_reward,
        )
        if not plan.ok:
            db.add(_skip_record(sig, settings, plan.reason, live=live))
            skipped += 1
            continue

        # ── Strategy/TA refinement of the entry (best entry to maximise TP) ──
        ta_note = ""
        ta_rsi = None
        ta_support = None
        ta_resistance = None
        ta_volume_warning = False
        ta_volume_ratio = None
        if settings.reanalyze:
            try:
                ta = await analyze_entry(
                    symbol=sig.symbol,
                    direction=sig.direction,
                    live_price=live,
                    stop_loss=plan.stop_loss,
                    fallback_entry=plan.sniper_entry or live,
                )
                ta_note = ta.note
                ta_rsi = ta.rsi
                ta_support = ta.support
                ta_resistance = ta.resistance
                ta_volume_warning = ta.opposite_volume
                ta_volume_ratio = ta.volume_ratio
                if ta.ok and ta.optimized_entry:
                    # Blend: take the better TA-derived entry, keep it valid vs SL.
                    if sig.direction.lower() == "long":
                        plan.sniper_entry = min(plan.sniper_entry or live, ta.optimized_entry)
                        plan.trigger_now = live <= plan.sniper_entry
                    else:
                        plan.sniper_entry = max(plan.sniper_entry or live, ta.optimized_entry)
                        plan.trigger_now = live >= plan.sniper_entry
                # If strategy says wait (overbought/oversold/opposite volume), hold as pending
                if ta.recommend == "wait":
                    plan.trigger_now = False
            except Exception:  # noqa: BLE001 — TA must never break sniping
                pass

        # ── AI opinion (multi-provider, optional & graceful) ──
        ai_note = ""
        if settings.reanalyze:
            ai = await _ai_entry_opinion(
                symbol=sig.symbol,
                direction=sig.direction,
                signal_entry=sig.entry,
                sniper_entry=plan.sniper_entry,
                stop_loss=plan.stop_loss,
                take_profit=plan.take_profit,
                live_price=live,
                rsi=ta_rsi,
                support=ta_support,
                resistance=ta_resistance,
                opposite_volume=ta_volume_warning,
                volume_ratio=ta_volume_ratio,
                volume_ctx=vol_ctx,
            )
            if ai:
                decision = str(ai.get("decision", "")).lower()
                ai_entry = ai.get("entry")
                conf = ai.get("confidence")
                provider = ai.get("_provider", "AI")
                ai_note = f"AI({provider}): {decision}"
                if conf is not None:
                    ai_note += f" {round(float(conf) * 100)}%"
                # ── Volume direction confirmation ──
                # If the AI says the direction isn't confirmed by volume, the
                # pair is likely moving against the signal → don't enter.
                dir_confirmed = ai.get("direction_confirmed")
                if dir_confirmed is False:
                    ta_volume_warning = True
                    decision = "skip"
                    ai_note += " · direction NOT confirmed by volume"
                if ai.get("info"):
                    ai_note += f" — {ai['info']}"
                elif ai.get("note"):
                    ai_note += f" — {ai['note']}"
                # Apply AI's refined entry if it's valid vs direction/SL
                if isinstance(ai_entry, (int, float)) and ai_entry > 0:
                    if sig.direction.lower() == "long" and ai_entry <= live:
                        plan.sniper_entry = float(ai_entry)
                        plan.trigger_now = live <= plan.sniper_entry
                    elif sig.direction.lower() == "short" and ai_entry >= live:
                        plan.sniper_entry = float(ai_entry)
                        plan.trigger_now = live >= plan.sniper_entry
                if decision == "skip":
                    db.add(_skip_record(sig, settings, f"AI advised skip · {ai_note}", live=live))
                    skipped += 1
                    continue
                if decision == "wait":
                    plan.trigger_now = False

        # ── Confirmation gate: volume context + order-flow TA + core AI agents ──
        # The VolumeContext is authoritative: a signal whose regime/divergence
        # argues against its own direction can never auto-execute.
        volume_confirmed = (not ta_volume_warning) and vol_ok
        ai_confirmed: bool | None = None
        agent_note = ""
        if settings.require_ai_confirmation:
            agent_confirm = await _agent_confirm(sig.symbol, sig.direction)
            if agent_confirm is not None:
                ai_confirmed = bool(agent_confirm.get("confirmed"))
                agent_note = agent_confirm.get("note", "")
            else:
                ai_confirmed = False  # agents unavailable → not auto-confirmed
                agent_note = "AI agents unavailable"
        overall_confirmed = volume_confirmed and (ai_confirmed if settings.require_ai_confirmation else True)

        trade = TelegramSniperTrade(
            signal_id=sig.id,
            channel_title=sig.channel_title,
            symbol=sig.symbol,
            direction=sig.direction,
            leverage=min(_leverage_int(sig.leverage) or settings.leverage, settings.leverage),
            signal_entry=sig.entry,
            sniper_entry=plan.sniper_entry,
            live_price_at_plan=live,
            stop_loss=plan.stop_loss,
            take_profit=plan.take_profit,
            position_size_usdt=settings.position_size_usdt,
            risk_reward=plan.risk_reward,
            status=SniperTradeStatus.PENDING,
            reason=(
                plan.reason
                + f" · VOLUME: {vol_why} · {vol_note}"
                + (f" · {ta_note}" if ta_note else "")
                + (f" · {ai_note}" if ai_note else "")
                + (f" · {agent_note}" if agent_note else "")
            ),
            # entry_strategy is String(200) — keep the volume verdict first so it
            # survives truncation, since it is the gating reason.
            entry_strategy=(
                f"vol:{vol_ctx.regime}x{vol_ctx.relative_volume:.2f}/{vol_ctx.divergence}"
                + (f" | {ta_note}" if ta_note else "")
                + (f" | {ai_note}" if ai_note else "")
            )[:200],
            rsi=ta_rsi,
            support=ta_support,
            resistance=ta_resistance,
            volume_warning=ta_volume_warning,
            ai_confirmed=ai_confirmed,
            ai_confirmation_note=agent_note or None,
            volume_confirmed=volume_confirmed,
            executed_mode=None,
        )

        # Auto-execute when confirmed (volume + AI agents). With
        # execute_immediately the order is placed at market right away (so it
        # shows on /trading); otherwise it waits for the optimised limit entry.
        ready = overall_confirmed and (settings.execute_immediately or plan.trigger_now)
        can_sandbox = settings.execute_sandbox and open_sandbox < settings.max_positions_sandbox
        can_live = settings.execute_live and open_live < settings.max_positions_live
        if ready and (can_sandbox or can_live):
            entry_px = live if settings.execute_immediately else (plan.sniper_entry or live)
            placed_any = False
            extra_reason = ""
            # Sandbox (simulation account on /trading)
            if can_sandbox:
                result = await _place_sim_order(
                    db,
                    symbol=sig.symbol,
                    direction=sig.direction,
                    entry=entry_px,
                    stop_loss=plan.stop_loss,
                    take_profit=plan.take_profit,
                    size_usdt=settings.position_size_usdt,
                    leverage=trade.leverage,
                    margin_mode=settings.margin_mode,
                    trade_type=settings.trade_type,
                )
                if result.get("success"):
                    trade.sim_order_id = result.get("order_id")
                    open_sandbox += 1
                    placed_any = True
                else:
                    extra_reason += f" · sandbox fail: {result.get('error')}"
            elif settings.execute_sandbox:
                extra_reason += " · sandbox cap reached"
            # Live (REAL money — opt-in)
            if can_live:
                live_res = await _execute_live(
                    db,
                    symbol=sig.symbol,
                    direction=sig.direction,
                    entry=entry_px,
                    stop_loss=plan.stop_loss,
                    take_profit=plan.take_profit,
                    leverage=trade.leverage or settings.leverage,
                )
                if live_res.get("success"):
                    trade.live_order_id = live_res.get("order_id")
                    open_live += 1
                    placed_any = True
                else:
                    extra_reason += f" · live fail: {live_res.get('error')}"
            elif settings.execute_live:
                extra_reason += " · live cap reached"

            if placed_any:
                modes = []
                if trade.sim_order_id:
                    modes.append("sandbox")
                if trade.live_order_id:
                    modes.append("live")
                trade.executed_mode = "+".join(modes) or "none"
                trade.status = SniperTradeStatus.PLACED
                trade.reason = f"Auto-executed ({trade.executed_mode}) — confirmed{extra_reason}"
                placed += 1
                await _store_trade_knowledge(
                    db, symbol=sig.symbol, direction=sig.direction,
                    mode=trade.executed_mode, entry=entry_px, confirmed=True,
                    note=(agent_note or ai_note or "confirmed"),
                )
            else:
                trade.status = SniperTradeStatus.FAILED
                trade.reason = f"Execution failed{extra_reason}"
        else:
            # Not confirmed / not ready → leave PENDING for MANUAL execution.
            if not overall_confirmed:
                blockers = []
                if not vol_ok:
                    blockers.append(f"volume gate: {vol_why}")
                elif not volume_confirmed:
                    blockers.append("order-flow volume opposes direction")
                if settings.require_ai_confirmation and not ai_confirmed:
                    blockers.append("AI agents did not confirm")
                trade.reason = (trade.reason or "") + " · awaiting manual exec (" + ", ".join(blockers) + ")"
            pending += 1

        db.add(trade)

    await db.commit()
    return {
        "enabled": True,
        "placed": placed,
        "pending": pending,
        "skipped": skipped,
        "missed": missed,
        "open_positions": open_positions,
    }


async def execute_sniper_trade(
    db: AsyncSession,
    trade_id: int,
    *,
    mode: str = "sandbox",
    force: bool = False,
) -> dict[str, Any]:
    """Manually execute a sniper trade on sandbox and/or live.

    mode: 'sandbox' | 'live' | 'both'. ``force`` bypasses the AI/volume
    confirmation gate (for signals the agents didn't auto-confirm).
    """
    settings = await get_or_create_settings(db)
    trade = await db.get(TelegramSniperTrade, trade_id)
    if trade is None:
        return {"ok": False, "error": "Trade not found"}
    if trade.status == SniperTradeStatus.PLACED:
        return {"ok": False, "error": "Trade already placed"}

    # Volume gate — re-resolved at execution time, not trusted from the plan.
    # A forced execution still records the evidence so the trade is never
    # unexplained, but the user's explicit override is honoured.
    exec_ctx = await resolve_volume(trade.symbol)
    exec_ok, exec_why = volume_supports(trade.direction, exec_ctx)
    trade.volume_confirmed = exec_ok
    if not force:
        if not exec_ok:
            trade.reason = f"Execution blocked — {exec_why} · {volume_gate_note(exec_ctx)}"
            trade.updated_at = _utcnow()
            await db.commit()
            return {
                "ok": False,
                "error": f"NO_TRADE — {exec_why}. Use force to override.",
                "volume": exec_ctx.model_dump(),
            }
        if trade.ai_confirmed is False:
            return {"ok": False, "error": "AI agents did not confirm — use force to override"}

    entry_px = trade.sniper_entry or trade.live_price_at_plan or 0
    if entry_px <= 0:
        entry_px = (await _get_live_price(trade.symbol)) or 0
    if entry_px <= 0:
        return {"ok": False, "error": "No live price available for symbol"}

    do_sandbox = mode in ("sandbox", "both")
    do_live = mode in ("live", "both")
    placed_modes: list[str] = []
    errors: list[str] = []

    if do_sandbox:
        r = await _place_sim_order(
            db, symbol=trade.symbol, direction=trade.direction, entry=entry_px,
            stop_loss=trade.stop_loss, take_profit=trade.take_profit,
            size_usdt=trade.position_size_usdt or settings.position_size_usdt,
            leverage=trade.leverage or settings.leverage,
            margin_mode=settings.margin_mode, trade_type=settings.trade_type,
        )
        if r.get("success"):
            trade.sim_order_id = r.get("order_id")
            placed_modes.append("sandbox")
        else:
            errors.append(f"sandbox: {r.get('error')}")

    if do_live:
        r = await _execute_live(
            db, symbol=trade.symbol, direction=trade.direction, entry=entry_px,
            stop_loss=trade.stop_loss, take_profit=trade.take_profit,
            leverage=trade.leverage or settings.leverage,
        )
        if r.get("success"):
            trade.live_order_id = r.get("order_id")
            placed_modes.append("live")
        else:
            errors.append(f"live: {r.get('error')}")

    if placed_modes:
        trade.status = SniperTradeStatus.PLACED
        trade.executed_mode = "+".join(placed_modes)
        trade.reason = (
            f"Manually executed ({trade.executed_mode})"
            + (" [forced]" if force else "")
            + f" · VOLUME: {exec_why} · {volume_gate_note(exec_ctx)}"
        )
        trade.updated_at = _utcnow()
        await _store_trade_knowledge(
            db, symbol=trade.symbol, direction=trade.direction,
            mode=trade.executed_mode, entry=entry_px, confirmed=not force, note="manual exec",
        )
        await db.commit()
        return {"ok": True, "executed_mode": trade.executed_mode, "errors": errors}

    await db.commit()
    return {"ok": False, "error": "; ".join(errors) or "Nothing placed"}


async def execute_parsed_signal(
    db: AsyncSession,
    signal_id: int,
    *,
    mode: str = "sandbox",
    force: bool = True,
) -> dict[str, Any]:
    """Execute a parsed telegram signal directly (from the Active Signals tab).

    Finds or creates a sniper trade for the signal, plans the entry, then places
    it at the current market price on the requested target(s). ``force`` defaults
    to True because this is an explicit user action.
    """
    sig = await db.get(TelegramParsedSignal, signal_id)
    if sig is None:
        return {"ok": False, "error": "Signal not found"}

    settings = await get_or_create_settings(db)
    existing = await db.scalar(
        select(TelegramSniperTrade).where(TelegramSniperTrade.signal_id == signal_id)
    )
    if existing is not None and existing.status == SniperTradeStatus.PLACED:
        return {"ok": False, "error": "Signal already executed"}

    live = await _get_live_price(sig.symbol)
    if not live:
        return {"ok": False, "error": "No live price available for symbol"}

    plan = reanalyze_signal(
        direction=sig.direction,
        signal_entry=sig.entry,
        stop_loss=sig.stop_loss,
        take_profits=sig.take_profits_json or [],
        live_price=live,
        offset_pct=settings.sniper_offset_pct,
        min_rr=settings.min_risk_reward,
    )
    if not plan.ok:
        if not force:
            return {"ok": False, "error": plan.reason or "Could not plan entry"}
        # Forced manual execution → place at market with the signal's own levels.
        tps = sig.take_profits_json or []
        plan = SniperPlan(
            ok=True,
            reason=f"Forced manual execution ({plan.reason})",
            sniper_entry=live,
            stop_loss=sig.stop_loss,
            take_profit=(float(tps[0]) if tps else None),
            risk_reward=None,
            trigger_now=True,
        )

    if existing is None:
        existing = TelegramSniperTrade(
            signal_id=signal_id,
            channel_title=sig.channel_title,
            symbol=sig.symbol,
            direction=sig.direction,
            leverage=min(_leverage_int(sig.leverage) or settings.leverage, settings.leverage),
            signal_entry=sig.entry,
            sniper_entry=live,  # market — execute now
            live_price_at_plan=live,
            stop_loss=plan.stop_loss,
            take_profit=plan.take_profit,
            position_size_usdt=settings.position_size_usdt,
            risk_reward=plan.risk_reward,
            status=SniperTradeStatus.PENDING,
            reason="Manual execution from Active Signals",
        )
        db.add(existing)
        await db.flush()
    else:
        # Execute at current market price
        existing.sniper_entry = live

    return await execute_sniper_trade(db, existing.id, mode=mode, force=force)


async def auto_close_positions_for_signal(
    db: AsyncSession,
    signal_id: int,
    reason: str = "Opposite direction detected — auto-closed",
) -> dict[str, Any]:
    """Close all sandbox AND live positions linked to a sniper trade signal.

    Called automatically when an opposite-direction message is detected so the
    user is protected from losses even when they're not at the computer.

    Returns a summary of what was closed.
    """
    from sqlalchemy import select

    result: dict[str, Any] = {
        "sandbox_closed": [],
        "live_closed": [],
        "errors": [],
    }

    # ── 1. Find all PLACED sniper trades for this signal ─────────────────
    trades_res = await db.execute(
        select(TelegramSniperTrade).where(
            TelegramSniperTrade.signal_id == signal_id,
            TelegramSniperTrade.status == SniperTradeStatus.PLACED,
        )
    )
    placed_trades = list(trades_res.scalars().all())

    if not placed_trades:
        return result

    # ── 2. Close sandbox (simulation) positions ───────────────────────────
    try:
        from app.models.database import SimPosition
        from app.trading.simulation import SimulationEngine

        for trade in placed_trades:
            if not trade.sim_order_id:
                continue
            # Find the open sim position created by this order
            pos_res = await db.execute(
                select(SimPosition).where(
                    SimPosition.order_id == trade.sim_order_id,
                    SimPosition.status == "open",
                )
            )
            pos = pos_res.scalar_one_or_none()
            if pos:
                close_res = await SimulationEngine.close_position(db, pos.id)
                if close_res.get("success"):
                    result["sandbox_closed"].append({
                        "symbol": pos.symbol,
                        "pnl": close_res.get("pnl"),
                        "trade_id": trade.id,
                    })
                    trade.status = SniperTradeStatus.SKIPPED
                    trade.reason = reason
                    trade.updated_at = _utcnow()
                else:
                    result["errors"].append(f"Sandbox close failed for {pos.symbol}: {close_res.get('error')}")
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"Sandbox close error: {exc}")

    # ── 3. Close live (real exchange) positions ───────────────────────────
    try:
        from app.exchanges.manager import exchange_manager, SupportedExchange

        for trade in placed_trades:
            if not trade.live_order_id:
                continue
            sym = normalize_symbol(trade.symbol)
            conn = exchange_manager.get_exchange(SupportedExchange.BITGET)
            if not conn:
                continue
            # Use ccxt to close reduce-only at market
            side_close = "sell" if trade.direction.lower() == "long" else "buy"
            try:
                # Fetch open futures position for the symbol
                positions = await conn.get_futures_positions()
                sym_positions = [
                    p for p in (positions or [])
                    if normalize_symbol(p.get("symbol", "")) == sym
                    and (p.get("holdSide", "")).lower() == ("long" if trade.direction.lower() == "long" else "short")
                ]
                for pos_data in sym_positions:
                    try:
                        amount = float(pos_data.get("available") or pos_data.get("total") or 0)
                        if amount <= 0:
                            continue
                        # Market close (reduce-only)
                        resp = await conn.exchange.create_order(
                            sym, "market", side_close, amount,
                            params={"reduceOnly": True, "holdSide": pos_data.get("holdSide")},
                        )
                        result["live_closed"].append({
                            "symbol": sym,
                            "amount": amount,
                            "order_id": (resp or {}).get("id"),
                            "trade_id": trade.id,
                        })
                        trade.status = SniperTradeStatus.SKIPPED
                        trade.reason = reason + " (live)"
                        trade.updated_at = _utcnow()
                    except Exception as inner_exc:  # noqa: BLE001
                        result["errors"].append(f"Live close order failed for {sym}: {inner_exc}")
            except Exception as exc2:  # noqa: BLE001
                result["errors"].append(f"Live position fetch failed for {sym}: {exc2}")
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"Live close error: {exc}")

    await db.commit()
    logger.info(
        "[AutoClose] signal={} sandbox={} live={} errors={}",
        signal_id,
        len(result["sandbox_closed"]),
        len(result["live_closed"]),
        len(result["errors"]),
    )
    return result


async def get_signal_prices(db: AsyncSession, symbols: list[str]) -> dict[str, float | None]:
    """Batch current prices for active-signal symbols (for the UI).

    Automatically routes Forex pairs (EURUSD, XAUUSD, etc.) through the
    Forex price service instead of the crypto exchange manager.
    """
    if not symbols:
        return {}

    requested = [s for s in symbols if s]
    if not requested:
        return {}

    # Enforce active-only pricing: do not resolve prices for TP/SL/closed signals.
    active_rows = await db.execute(
        select(TelegramParsedSignal.symbol, TelegramParsedSignal.market_type).where(
            TelegramParsedSignal.status == SignalStatus.ACTIVE,
            TelegramParsedSignal.symbol.in_(requested),
        )
    )
    active_map: dict[str, str] = {
        row[0]: (row[1] or "crypto") for row in active_rows.all() if row and row[0]
    }

    # Lazy import to avoid circular deps
    try:
        from plugins.TelegramSignalNewsPlugin.backend.services.forex_price_service import (
            get_forex_price,
            is_forex_pair,
        )
        _forex_available = True
    except Exception:  # noqa: BLE001
        _forex_available = False

    async def _resolve_price(sym: str) -> tuple[str, float | None]:
        mt = active_map[sym]
        try:
            if _forex_available and (mt == "forex" or is_forex_pair(sym)):
                price = await asyncio.wait_for(get_forex_price(sym), timeout=2.5)
            else:
                price = await asyncio.wait_for(_get_live_price(sym), timeout=2.5)
            return sym, price
        except Exception:
            return sym, None

    deduped = [sym for i, sym in enumerate(requested) if sym in active_map and sym not in requested[:i]]
    if not deduped:
        return {}

    results = await asyncio.gather(*(_resolve_price(sym) for sym in deduped))
    return {sym: price for sym, price in results}


async def analyze_signal_full(db: AsyncSession, signal_id: int) -> dict[str, Any]:
    """Full on-demand analysis of one telegram signal.

    Combines the core AI agent pipeline (routed through the connected providers),
    exchange volume/order-flow, and optimised buy/sell sniper entries, then
    returns a report with an execute / monitor / skip decision.
    """
    sig = await db.get(TelegramParsedSignal, signal_id)
    if sig is None:
        return {"ok": False, "error": "Signal not found"}
    settings = await get_or_create_settings(db)
    live = await _get_live_price(sig.symbol)
    if not live:
        return {"ok": False, "error": "No live price available for symbol"}

    direction = (sig.direction or "").lower()
    want = "buy" if direction == "long" else "sell"

    # ── Volume gate first: no analysis emits a tradeable decision without it ──
    vol_ctx = await resolve_volume(sig.symbol)
    vol_ok, vol_why = volume_supports(sig.direction, vol_ctx)

    # ── Volume + TA (with optimised entry for the signal's direction) ──
    ta = await analyze_entry(
        symbol=sig.symbol,
        direction=sig.direction,
        live_price=live,
        stop_loss=sig.stop_loss,
        fallback_entry=sig.entry or live,
    )
    volume_confirms = not ta.opposite_volume

    # ── Core AI agent pipeline (full Market→Sentiment→Signal→Risk) ──
    ai_report: dict[str, Any] | None = None
    try:
        from app.core.database import AsyncSessionLocal
        from app.agents.orchestrator import AgentOrchestrator
        async with AsyncSessionLocal() as adb:
            res = await AgentOrchestrator.analyze_symbol(
                adb, normalize_symbol(sig.symbol), "1h", trigger="telegram"
            )
        if isinstance(res, dict) and not res.get("error"):
            ai_report = {
                "final_action": (res.get("final_action") or "hold"),
                "final_confidence": res.get("final_confidence") or 0,
                "reasoning": res.get("final_reasoning") or "",
                "ai_calls": res.get("ai_calls") or 0,
                "decisions": [
                    {
                        "role": d.get("agent_role"),
                        "action": d.get("action"),
                        "confidence": d.get("confidence"),
                        "reasoning": d.get("reasoning"),
                        "provider": d.get("provider"),
                    }
                    for d in (res.get("decisions") or [])
                ],
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("Full analysis AI step failed for {}: {}", sig.symbol, exc)

    ai_action = (ai_report or {}).get("final_action")
    ai_conf = float((ai_report or {}).get("final_confidence") or 0)
    ai_confirms = ai_action == want and ai_conf >= 0.5

    # ── Sniper plan for the signal's direction ──
    plan = reanalyze_signal(
        direction=sig.direction,
        signal_entry=sig.entry,
        stop_loss=sig.stop_loss,
        take_profits=sig.take_profits_json or [],
        live_price=live,
        offset_pct=settings.sniper_offset_pct,
        min_rr=settings.min_risk_reward,
    )

    # ── Buy/sell sniper entry suggestions (both directions) ──
    buy_entry = ta.support if (ta.support and ta.support < live) else round(live * 0.995, 8)
    sell_entry = ta.resistance if (ta.resistance and ta.resistance > live) else round(live * 1.005, 8)

    # ── Decision ──
    # The volume gate outranks everything else: without a resolved context there
    # is no tradeable call to make, whatever the agents say.
    if vol_ctx.status != "OK":
        decision, reason = "no_trade", (
            f"Volume is a hard precondition and it is {vol_ctx.status.lower()} "
            f"for {sig.symbol}. {vol_ctx.detail}"
        )
    elif not vol_ok:
        decision, reason = "skip", f"Volume argues against the signal: {vol_why}."
    elif volume_confirms and ai_confirms:
        decision, reason = "execute", (
            f"AI agents and volume both confirm the direction — {vol_why}."
        )
    elif not volume_confirms:
        decision, reason = "monitor", "Volume is pushing against the signal — wait for confirmation."
    elif ai_action in ("hold", "wait", None):
        decision, reason = "monitor", "AI agents are neutral — keep monitoring for a cleaner setup."
    elif ai_action and ai_action != want:
        decision, reason = "skip", f"AI agents lean the opposite way ({ai_action})."
    else:
        decision, reason = "monitor", "Mixed signals — monitor before committing."

    # Persist a short insight
    await _store_trade_knowledge(
        db, symbol=sig.symbol, direction=sig.direction, mode="analysis",
        entry=live, confirmed=(decision == "execute"),
        note=f"{decision}: {reason[:120]}",
    )

    return {
        "ok": True,
        "signal": {
            "id": sig.id,
            "symbol": sig.symbol,
            "direction": sig.direction,
            "entry": sig.entry,
            "stop_loss": sig.stop_loss,
            "take_profits": sig.take_profits_json or [],
            "leverage": sig.leverage,
            "channel_title": sig.channel_title,
        },
        "current_price": live,
        # The resolved volume gate — 24h volume, last 1h, relative volume,
        # regime, divergence and why it argues for or against the direction.
        "volume_context": vol_ctx.model_dump(),
        "volume_gate": {
            "supported": vol_ok,
            "reason": vol_why,
            "evidence": volume_gate_note(vol_ctx),
        },
        "volume": {
            "opposite_volume": ta.opposite_volume,
            "volume_ratio": ta.volume_ratio,
            "volume_confirms": volume_confirms,
            "rsi": ta.rsi,
            "support": ta.support,
            "resistance": ta.resistance,
            "recommend": ta.recommend,
            "note": ta.note,
        },
        "ai_agents": ai_report,
        "ai_confirms": ai_confirms,
        "sniper_entries": {
            "primary": {
                "direction": sig.direction,
                "ok": plan.ok,
                "entry": plan.sniper_entry,
                "stop_loss": plan.stop_loss,
                "take_profit": plan.take_profit,
                "risk_reward": plan.risk_reward,
                "trigger_now": plan.trigger_now,
                "reason": plan.reason,
            },
            "buy": {"entry": buy_entry, "note": "near support / -0.5% pullback"},
            "sell": {"entry": sell_entry, "note": "near resistance / +0.5% bounce"},
        },
        "decision": decision,
        "decision_reason": reason,
    }


async def volume_monitor_snapshot(db: AsyncSession, *, limit: int = 25) -> dict[str, Any]:
    """Live volume read for all active signals (for the Volume Monitor tab)."""
    res = await db.execute(
        select(TelegramParsedSignal)
        .where(TelegramParsedSignal.status == SignalStatus.ACTIVE)
        .order_by(TelegramParsedSignal.created_at.desc())
        .limit(limit)
    )
    signals = list(res.scalars().all())
    items: list[dict[str, Any]] = []
    for sig in signals:
        snap = await volume_snapshot(sig.symbol, sig.direction)
        cur = snap.get("current_price")
        dist = None
        if cur and sig.entry:
            dist = round((cur - sig.entry) / sig.entry * 100, 2)
        items.append({
            "signal_id": sig.id,
            "symbol": sig.symbol,
            "direction": sig.direction,
            "entry": sig.entry,
            "channel_title": sig.channel_title,
            "current_price": cur,
            "distance_pct": dist,
            "available": snap.get("available", False),
            "buy_pct": snap.get("buy_pct"),
            "sell_pct": snap.get("sell_pct"),
            "opposing_pct": snap.get("opposing_pct"),
            "volume_spike": snap.get("volume_spike", False),
            "opposite_volume": snap.get("opposite_volume", False),
            "vol_ratio": snap.get("vol_ratio"),
        })
    return {"items": items, "count": len(items)}


async def reanalyze_skipped_signals(db: AsyncSession) -> dict[str, Any]:
    """Re-examine SKIPPED sniper trades and active signals with no sniper trade.

    Called every ``settings.skipped_reanalyze_minutes`` by the monitor loop.
    If conditions have improved (price moved into range, volume confirms,
    AI confirms) the signal is promoted to PENDING / auto-executed.
    """
    settings = await get_or_create_settings(db)
    cadence = int(getattr(settings, "skipped_reanalyze_minutes", 15) or 15)
    if cadence <= 0:
        return {"skipped_reanalysis": "disabled"}

    promoted = reconsidered = 0

    # ── 1. Signals with a SKIPPED sniper trade ──
    skipped_res = await db.execute(
        select(TelegramSniperTrade, TelegramParsedSignal)
        .join(TelegramParsedSignal, TelegramSniperTrade.signal_id == TelegramParsedSignal.id)
        .where(
            TelegramSniperTrade.status == SniperTradeStatus.SKIPPED,
            TelegramParsedSignal.status == SignalStatus.ACTIVE,
            # Only look at trades skipped in the last 24 hours
            TelegramSniperTrade.updated_at >= _utcnow() - timedelta(hours=24),
        )
        .limit(20)
    )
    for trade, sig in skipped_res.all():
        reconsidered += 1
        live = await _get_live_price(sig.symbol)
        if not live:
            continue
        plan = reanalyze_signal(
            direction=sig.direction,
            signal_entry=sig.entry,
            stop_loss=sig.stop_loss,
            take_profits=sig.take_profits_json or [],
            live_price=live,
            offset_pct=settings.sniper_offset_pct,
            min_rr=settings.min_risk_reward,
        )
        if not plan.ok:
            continue
        # Volume gate — a skipped signal is only re-promoted when volume can be
        # established AND supports its direction.
        vol_ctx = await resolve_volume(sig.symbol)
        vol_ok, vol_why = volume_supports(sig.direction, vol_ctx)
        if not vol_ok:
            trade.volume_confirmed = False
            trade.reason = f"Still NO_TRADE — {vol_why} · {volume_gate_note(vol_ctx)}"
            trade.updated_at = _utcnow()
            continue
        # Quick order-flow read (no agent call — saves tokens)
        vol = await volume_snapshot(sig.symbol, sig.direction)
        if vol.get("opposite_volume"):
            continue
        # Upgrade the existing skipped trade back to PENDING
        trade.sniper_entry = plan.sniper_entry
        trade.stop_loss = plan.stop_loss
        trade.take_profit = plan.take_profit
        trade.risk_reward = plan.risk_reward
        trade.live_price_at_plan = live
        trade.volume_confirmed = True
        trade.status = SniperTradeStatus.PENDING
        trade.reason = (
            f"Re-promoted after {cadence}min re-analysis: {plan.reason} "
            f"· VOLUME: {vol_why} · {volume_gate_note(vol_ctx)}"
        )
        trade.updated_at = _utcnow()
        promoted += 1

    # ── 2. Active signals with NO sniper trade at all ──
    # (They may have been skipped by the confidence filter originally)
    sniped_ids_res = await db.execute(select(TelegramSniperTrade.signal_id))
    sniped_ids = {row[0] for row in sniped_ids_res.all()}
    unsniped_res = await db.execute(
        select(TelegramParsedSignal)
        .where(
            TelegramParsedSignal.status == SignalStatus.ACTIVE,
            TelegramParsedSignal.id.notin_(sniped_ids),
            TelegramParsedSignal.created_at >= _utcnow() - timedelta(hours=12),
        )
        .limit(10)
    )
    for sig in unsniped_res.scalars().all():
        reconsidered += 1
        live = await _get_live_price(sig.symbol)
        if not live:
            continue
        plan = reanalyze_signal(
            direction=sig.direction,
            signal_entry=sig.entry,
            stop_loss=sig.stop_loss,
            take_profits=sig.take_profits_json or [],
            live_price=live,
            offset_pct=settings.sniper_offset_pct,
            min_rr=settings.min_risk_reward,
        )
        if not plan.ok:
            continue
        vol_ctx = await resolve_volume(sig.symbol)
        vol_ok, vol_why = volume_supports(sig.direction, vol_ctx)
        if not vol_ok:
            continue  # no volume, no queue — nothing to snipe
        trade = TelegramSniperTrade(
            signal_id=sig.id,
            channel_title=sig.channel_title,
            symbol=sig.symbol,
            direction=sig.direction,
            leverage=min(_leverage_int(sig.leverage) or settings.leverage, settings.leverage),
            signal_entry=sig.entry,
            sniper_entry=plan.sniper_entry,
            live_price_at_plan=live,
            stop_loss=plan.stop_loss,
            take_profit=plan.take_profit,
            position_size_usdt=settings.position_size_usdt,
            risk_reward=plan.risk_reward,
            status=SniperTradeStatus.PENDING,
            volume_confirmed=True,
            reason=(
                f"Re-queued after {cadence}min re-analysis "
                f"· VOLUME: {vol_why} · {volume_gate_note(vol_ctx)}"
            ),
        )
        db.add(trade)
        promoted += 1

    await db.commit()
    return {"promoted": promoted, "reconsidered": reconsidered}


import re as _re

# ── Whale-volume message format (Binance Whale Volume Signals) ───────────
# 💰 #FETUSDT LONG 🟢
# Long Volume     : $13k (%0.067)
# 24h Total Volume: $20m
# Sequence        : 1 [$13k (%0.067)]
# Price           : 0.1725000
#
# The parser extracts symbol, direction, sequence count (🔴×N = strength),
# and current price (more accurate than exchange lookup for that instant).
_WHALE_SYM = _re.compile(r"#([A-Z0-9]{2,20}(?:USDT|USD|BTC|ETH)?)", _re.IGNORECASE)
_WHALE_DIR_LONG = _re.compile(r"\blong\b|🟢", _re.IGNORECASE)
_WHALE_DIR_SHORT = _re.compile(r"\bshort\b|🔴", _re.IGNORECASE)
_WHALE_PRICE = _re.compile(r"price\s*:\s*([\d.]+)", _re.IGNORECASE)
_WHALE_SEQ = _re.compile(r"sequence\s*:\s*(\d+)", _re.IGNORECASE)
_WHALE_VOL_USD = _re.compile(r"(?:long|short)\s+volume\s*:\s*\$([\d.,]+)([kmb]?)", _re.IGNORECASE)


def _parse_whale_volume_message(text: str) -> dict | None:
    """Parse a Binance Whale Volume Signals message into structured data.

    Returns {symbol, direction, price, sequence, volume_usd} or None.
    ``sequence`` is the consecutive alert count — higher means stronger whale
    pressure. ``direction`` is 'long' or 'short'.
    """
    sym_m = _WHALE_SYM.search(text)
    if not sym_m:
        return None
    raw_sym = sym_m.group(1).upper()
    # Normalise: FETUSDT → FETUSDT, FET → FETUSDT
    if not raw_sym.endswith(("USDT", "USD", "BTC", "ETH", "USDC")):
        raw_sym += "USDT"

    is_long = bool(_WHALE_DIR_LONG.search(text))
    is_short = bool(_WHALE_DIR_SHORT.search(text))
    if is_short and not is_long:
        direction = "short"
    elif is_long and not is_short:
        direction = "long"
    else:
        direction = None  # ambiguous — skip

    price_m = _WHALE_PRICE.search(text)
    price = float(price_m.group(1)) if price_m else None

    seq_m = _WHALE_SEQ.search(text)
    sequence = int(seq_m.group(1)) if seq_m else 1

    vol_m = _WHALE_VOL_USD.search(text)
    vol_usd: float | None = None
    if vol_m:
        val = float(vol_m.group(1).replace(",", ""))
        suffix = vol_m.group(2).lower()
        mult = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get(suffix, 1)
        vol_usd = val * mult

    return {
        "symbol": raw_sym,
        "direction": direction,
        "price": price,
        "sequence": sequence,
        "volume_usd": vol_usd,
    }


def _parse_volume_signal_from_text(text: str) -> list[str]:
    """Extract token symbols from a generic volume-alert message (fallback parser).

    Handles bare tickers, #SYMBOL, and XXXUSDT glued formats.
    """
    found: list[str] = []
    seen: set[str] = set()

    # Pattern 1: #SYMBOL or $SYMBOL
    for m in _re.finditer(r"[#$]([A-Z0-9]{2,15})\b", text.upper()):
        tok = m.group(1)
        if tok not in {"LONG", "SHORT", "USDT", "USD", "BTC", "ETH", "BNB"}:
            key = tok + "USDT"
            if key not in seen:
                seen.add(key)
                found.append(key)

    # Pattern 2: bare XXXUSDT or XXX/USDT
    for m in _re.finditer(r"\b([A-Z0-9]{2,12})(USDT|/USDT)\b", text.upper()):
        key = m.group(1) + "USDT"
        if key not in seen:
            seen.add(key)
            found.append(key)

    # Pattern 3: bare ticker before volume/pump/spike
    for m in _re.finditer(r"\b([A-Z]{2,10})\s+(?:volume|pump|spike|surge|alert)", text.upper()):
        key = m.group(1) + "USDT"
        if key not in seen:
            seen.add(key)
            found.append(key)

    return found[:8]


async def process_volume_channel_message(db: AsyncSession, message_text: str) -> dict[str, Any]:
    """Handle a message from the designated volume-alert channel.

    Supports the Binance Whale Volume Signals format:
      💰 #FETUSDT LONG 🟢
      Long Volume: $13k (%0.067)
      Sequence: 1 [$13k (%0.067)]
      Price: 0.1725000

    Also handles generic volume-alert text. For each parsed symbol:
    1. Try to match active AND skipped signals with the same base token.
    2. Confirm the whale direction aligns with the signal direction.
    3. Use the whale message's own price if available (most accurate).
    4. Re-plan the entry and auto-execute on sandbox if conditions pass.
    """
    settings = await get_or_create_settings(db)

    # ── Try the rich Binance Whale parser first ───────────────────────────
    whale = _parse_whale_volume_message(message_text)
    if whale and whale["symbol"] and whale["direction"]:
        symbols_info = [whale]
    else:
        # Fall back to generic symbol extraction
        generic = _parse_volume_signal_from_text(message_text)
        if not generic:
            return {"symbols_found": [], "reassessed": 0, "triggered": []}
        symbols_info = [{"symbol": s, "direction": None, "price": None,
                         "sequence": 1, "volume_usd": None} for s in generic]

    reassessed = 0
    triggered: list[str] = []

    for info in symbols_info:
        sym = (info["symbol"] or "").upper()
        whale_direction = info.get("direction")  # 'long' | 'short' | None
        whale_price = info.get("price")          # live price from message
        whale_seq = int(info.get("sequence") or 1)

        # Minimum sequence threshold (configurable in future; default = 1 for now)
        # A sequence of 3+ means repeated strong whale pressure — highest confidence
        base = sym.replace("USDT", "").replace("USDC", "").replace("/USDT", "")

        # ── Find matching signals (active + skipped) ──────────────────────
        sigs_res = await db.execute(
            select(TelegramParsedSignal)
            .where(
                TelegramParsedSignal.symbol.ilike(f"%{base}%"),
                TelegramParsedSignal.status == SignalStatus.ACTIVE,
            )
            .limit(3)
        )
        sigs = list(sigs_res.scalars().all())

        # Also check recently-skipped sniper trades for the same token
        skipped_res = await db.execute(
            select(TelegramSniperTrade, TelegramParsedSignal)
            .join(TelegramParsedSignal, TelegramSniperTrade.signal_id == TelegramParsedSignal.id)
            .where(
                TelegramSniperTrade.status == SniperTradeStatus.SKIPPED,
                TelegramParsedSignal.symbol.ilike(f"%{base}%"),
                TelegramSniperTrade.updated_at >= _utcnow() - timedelta(hours=24),
            )
            .limit(2)
        )
        for trade, sig in skipped_res.all():
            if sig not in sigs:
                sigs.append(sig)

        if not sigs:
            continue

        for sig in sigs:
            reassessed += 1

            # Direction check: whale direction must not oppose the signal direction
            sig_dir = (sig.direction or "").lower()
            if whale_direction and whale_direction != sig_dir:
                logger.info(
                    "[WhaleVolume] {} whale={} signal={} — directions oppose, skip",
                    sig.symbol, whale_direction, sig_dir,
                )
                continue

            # Use whale price if available (freshest), else exchange lookup
            live = whale_price or await _get_live_price(sig.symbol)
            if not live:
                continue

            # ── Volume gate ──────────────────────────────────────────────────
            # A whale alert is not a substitute for volume context: the message
            # reports one actor's flow, the context reports the whole tape.
            vol_ctx = await resolve_volume(sig.symbol)
            vol_ok, vol_why = volume_supports(sig_dir, vol_ctx)
            if not vol_ok:
                logger.info(
                    "[WhaleVolume] {} NO_TRADE — {} (seq={})",
                    sig.symbol, vol_why, whale_seq,
                )
                continue

            # Volume snapshot (lightweight — confirms whale message direction)
            vol = await volume_snapshot(sig.symbol, sig_dir)
            if vol.get("opposite_volume") and whale_seq < 2:
                # High opposite-volume AND only 1 sequence: too risky
                logger.info("[WhaleVolume] {} opp_vol + seq={} — skip", sig.symbol, whale_seq)
                continue

            # Re-plan sniper entry
            plan = reanalyze_signal(
                direction=sig_dir,
                signal_entry=sig.entry,
                stop_loss=sig.stop_loss,
                take_profits=sig.take_profits_json or [],
                live_price=live,
                offset_pct=settings.sniper_offset_pct,
                min_rr=settings.min_risk_reward,
            )
            if not plan.ok:
                logger.debug("[WhaleVolume] {} plan failed: {}", sig.symbol, plan.reason)
                continue

            # Don't double-execute an already-active trade
            existing = await db.scalar(
                select(TelegramSniperTrade).where(
                    TelegramSniperTrade.signal_id == sig.id,
                    TelegramSniperTrade.status.in_([SniperTradeStatus.PENDING, SniperTradeStatus.PLACED]),
                )
            )
            if existing:
                continue

            entry_px = live if settings.execute_immediately else (plan.sniper_entry or live)
            reason_note = (
                f"WhaleVol seq={whale_seq}"
                + (f" ${info['volume_usd']:,.0f}" if info.get("volume_usd") else "")
                + f" · VOLUME: {vol_why} · {volume_gate_note(vol_ctx)}"
                + (f" | {message_text[:60]}" if not whale else "")
            )
            trade_obj = TelegramSniperTrade(
                signal_id=sig.id,
                channel_title=sig.channel_title,
                symbol=sig.symbol,
                direction=sig_dir,
                leverage=min(_leverage_int(sig.leverage) or settings.leverage, settings.leverage),
                signal_entry=sig.entry,
                sniper_entry=entry_px,
                live_price_at_plan=live,
                stop_loss=plan.stop_loss,
                take_profit=plan.take_profit,
                position_size_usdt=settings.position_size_usdt,
                risk_reward=plan.risk_reward,
                status=SniperTradeStatus.PENDING,
                volume_confirmed=True,
                ai_confirmation_note=(
                    f"Whale volume sequence={whale_seq} · {volume_gate_note(vol_ctx)}"
                ),
                reason=reason_note,
            )
            db.add(trade_obj)
            await db.flush()

            # Execute on sandbox
            placed_mode: list[str] = []
            if settings.execute_sandbox:
                result = await _place_sim_order(
                    db, symbol=sig.symbol, direction=sig_dir, entry=entry_px,
                    stop_loss=plan.stop_loss, take_profit=plan.take_profit,
                    size_usdt=settings.position_size_usdt,
                    leverage=trade_obj.leverage, margin_mode=settings.margin_mode,
                    trade_type=settings.trade_type,
                )
                if result.get("success"):
                    trade_obj.sim_order_id = result.get("order_id")
                    placed_mode.append("sandbox")
            if settings.execute_live:
                live_res = await _execute_live(
                    db, symbol=sig.symbol, direction=sig_dir, entry=entry_px,
                    stop_loss=plan.stop_loss, take_profit=plan.take_profit,
                    leverage=trade_obj.leverage or settings.leverage,
                )
                if live_res.get("success"):
                    trade_obj.live_order_id = live_res.get("order_id")
                    placed_mode.append("live")
            if placed_mode:
                trade_obj.status = SniperTradeStatus.PLACED
                trade_obj.executed_mode = "+".join(placed_mode)
            triggered.append(sig.symbol)
            logger.info(
                "[WhaleVolume] {} seq={} → {}", sig.symbol, whale_seq,
                trade_obj.executed_mode or "PENDING"
            )

    await db.commit()
    return {
        "whale": whale,
        "symbols_found": [i["symbol"] for i in symbols_info],
        "reassessed": reassessed,
        "triggered": triggered,
    }


def _skip_record(sig, settings, reason: str, live: float | None = None) -> TelegramSniperTrade:
    return TelegramSniperTrade(
        signal_id=sig.id,
        channel_title=sig.channel_title,
        symbol=sig.symbol,
        direction=sig.direction,
        leverage=_leverage_int(sig.leverage),
        signal_entry=sig.entry,
        sniper_entry=None,
        live_price_at_plan=live,
        stop_loss=sig.stop_loss,
        take_profit=None,
        position_size_usdt=settings.position_size_usdt,
        status=SniperTradeStatus.SKIPPED,
        reason=reason,
    )


def _leverage_int(value: str | None) -> int | None:
    if not value:
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    try:
        return int(digits) if digits else None
    except ValueError:
        return None
