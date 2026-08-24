"""
Kronos Forecast Plugin — JARVIS analysis + brain learning.

Bridges the raw Kronos forecast to a natural-language JARVIS explanation,
enriches crypto symbols with a live market-cap snapshot, and stores every
analysis to the shared AI knowledge brain so JARVIS learns over time.

All cross-plugin / core imports are guarded so the Kronos plugin stays
standalone and removable — if the AI Analyst plugin or the core pair catalog
are absent, this degrades to a deterministic summary with zero side effects.
"""
from __future__ import annotations

import os
import time
from typing import Optional, Tuple

from loguru import logger

from plugins.KronosForecastPlugin.backend.schemas import (
    ForecastResponse,
    JarvisAnalysisResponse,
    MarketCapInfo,
    PositionInfo,
)

_SYSTEM_PROMPT = (
    "You are JARVIS, a precise crypto/markets forecasting analyst. You are given "
    "a Sox foundation-model price forecast plus live market context. Write a "
    "thorough, well-structured analysis — a minimum of 500 words — that a trader "
    "can act on immediately. Always cover ALL of the following sections in full; "
    "never truncate or abbreviate any section: "
    "(1) FORECAST DIRECTION & MAGNITUDE — explain the predicted % change, the "
    "model's reasoning, and what price levels matter most; "
    "(2) CONFIDENCE & UNCERTAINTY — interpret the confidence score, explain what "
    "the p10-p90 band width signals (tight = conviction, wide = high dispersion), "
    "and identify which scenarios could invalidate the forecast; "
    "(3) TARGET vs CURRENT PRICE — analyse the distance to the target, discuss "
    "what achieving or missing it implies for the broader trend, and note any key "
    "support/resistance levels in the band range; "
    "(4) VOLUME EVIDENCE — you are given a resolved VOLUME GATE. Quote its "
    "numbers exactly: rolling 24h volume, the last completed 1h volume, the "
    "relative volume vs the 24h hourly mean, the regime "
    "(DEAD/NORMAL/ELEVATED/CLIMACTIC) and the price-volume divergence. Explain "
    "what they mean for this move — rising price on rising relative volume is "
    "trend continuation, rising price on falling relative volume is exhaustion, "
    "and climactic volume against the move is reversal risk. For crypto also "
    "cover how market-cap size and rank shape liquidity risk. Never invent a "
    "volume number that is not in the input; "
    "(5) MACRO CONTEXT — the dollar index and VIX lines supplied under 'Why this "
    "direction was chosen' are a real input to this forecast's confidence, so say "
    "so in one or two sentences: quote the DXY and VIX figures given, say whether "
    "they support or oppose the move, and say plainly when the macro read did not "
    "apply to this instrument and why. Never invent a DXY or VIX number that is "
    "not in the input, and never claim macro was used when the input says it was "
    "not; "
    "(6) ACTIONABLE TAKEAWAY — give one specific, risk-aware recommendation with "
    "a clear entry rationale, stop-loss zone, and take-profit target derived from "
    "the forecast numbers. "
    "If an OPEN POSITION is provided, add a final section titled 'Position:' that "
    "gives a specific, forecast-aware recommendation — hold, add, reduce, or close "
    "— with concrete stop-loss and take-profit levels using the p10-p90 band, "
    "factoring the position side, entry price, current PnL, leverage, and "
    "liquidation price. "
    "Write in clear, flowing prose. Target 500-620 words total. "
    "Never fabricate numbers. Always finish every sentence you start."
)


def _fmt_usd(v: Optional[float]) -> str:
    """Compact USD formatter: $1.23T / $45.6B / $789.0M."""
    if v is None:
        return "n/a"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "n/a"
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return f"${v / div:.2f}{unit}"
    return f"${v:,.2f}"


async def _market_context(symbol: str) -> Optional[MarketCapInfo]:
    """Live market-cap snapshot for crypto symbols (guarded, best-effort)."""
    try:
        from app.services import pair_catalog  # core, read-only
    except Exception:
        return None
    try:
        snap = await pair_catalog.get_market_snapshot(symbol)
    except Exception as exc:
        logger.debug(f"[Kronos] market snapshot failed for {symbol}: {exc}")
        return None
    if not snap:
        return None
    return MarketCapInfo(
        symbol=symbol,
        name=snap.get("name"),
        market_cap=snap.get("market_cap"),
        market_cap_rank=snap.get("market_cap_rank"),
        volume_24h=snap.get("volume_24h"),
        price=snap.get("price"),
        price_change_24h=snap.get("price_change_24h"),
        is_crypto=True,
    )


async def _open_position(exchange: str, symbol: str) -> Optional[PositionInfo]:
    """Return the caller's open position on `symbol` for `exchange`, if any (guarded)."""
    try:
        from app.api.jarvis import get_all_positions  # core, read-only
    except Exception:
        return None
    try:
        positions = await get_all_positions(exchange=exchange)
    except Exception as exc:
        logger.debug(f"[Kronos] position fetch failed for {exchange}: {exc}")
        return None
    want = symbol.replace("/", "").upper()
    for p in positions or []:
        psym = str(getattr(p, "symbol", "") or "").replace("/", "").upper()
        if psym != want:
            continue
        try:
            return PositionInfo(
                exchange=str(getattr(p, "exchange", exchange)),
                symbol=str(getattr(p, "symbol", symbol)),
                side=str(getattr(p, "side", "long")).lower(),
                size=float(getattr(p, "size", 0.0) or 0.0),
                entry_price=float(getattr(p, "entry_price", 0.0) or 0.0),
                mark_price=float(getattr(p, "mark_price", 0.0) or 0.0),
                pnl=float(getattr(p, "pnl", 0.0) or 0.0),
                pnl_pct=float(getattr(p, "pnl_pct", 0.0) or 0.0),
                leverage=(float(getattr(p, "leverage")) if getattr(p, "leverage", None) not in (None, "") else None),
                liquidation_price=(
                    float(getattr(p, "liquidation_price"))
                    if getattr(p, "liquidation_price", None) not in (None, "")
                    else None
                ),
            )
        except Exception as exc:
            logger.debug(f"[Kronos] position parse failed: {exc}")
            return None
    return None


def _build_user_prompt(
    resp: ForecastResponse,
    market: Optional[MarketCapInfo],
    position: Optional[PositionInfo] = None,
) -> str:
    sig = resp.signal
    band_lo = resp.lower_band[-1].value if resp.lower_band else None
    band_hi = resp.upper_band[-1].value if resp.upper_band else None
    lines = [
        f"Symbol: {resp.symbol}  ({resp.exchange}, {resp.timeframe})",
        f"Engine: {resp.engine} ({resp.model_name})",
        f"Horizon: {resp.pred_len} candles, {resp.samples} sampled paths",
        f"Current (anchor) price: {resp.anchor_price:.6g}",
    ]
    if sig:
        lines += [
            f"Predicted direction: {sig.direction}",
            f"Predicted change: {sig.pct_change:+.2f}%",
            f"Target price: {sig.target_price:.6g}",
            f"Confidence: {sig.confidence * 100:.0f}%",
        ]
    if band_lo is not None and band_hi is not None:
        lines.append(f"End-of-horizon p10-p90 band: {band_lo:.6g} to {band_hi:.6g}")
    vol = resp.volume
    if vol is not None:
        lines += ["", "VOLUME GATE (this forecast is conditioned on it):",
                  f"  Status: {vol.status}  |  Source: {vol.source} ({vol.volume_unit} volume)"]
        if vol.status == "OK":
            lines += [
                f"  Rolling 24h volume: {vol.volume_24h:,.4g}",
                f"  Last completed 1h volume: {vol.volume_1h:,.4g} "
                f"(24h hourly mean {vol.hourly_mean_24h:,.4g})",
                f"  Relative volume: x{vol.relative_volume:.2f}"
                + (f"  |  z-score {vol.z_score:+.2f}" if vol.z_score is not None else ""),
                f"  Regime: {vol.regime}  |  Divergence: {vol.divergence} "
                f"over {vol.divergence_bars}h",
            ]
            if vol.reversal_risk:
                lines.append("  REVERSAL RISK: climactic volume is confirming the opposite move.")
        else:
            lines.append(f"  {vol.detail}")
    if sig and sig.rationale:
        lines += ["", "Why this direction was chosen:"] + [f"  - {r}" for r in sig.rationale]
    if resp.decision != "OK":
        lines += [
            "",
            f"DECISION: {resp.decision}. Do NOT recommend an entry. Explain to the "
            "trader exactly which volume precondition failed and what would have to "
            "change before a trade is justified.",
        ]
    if market:
        lines += [
            "",
            "Live market context (crypto):",
            f"  Name: {market.name or resp.symbol}",
            f"  Market cap: {_fmt_usd(market.market_cap)}"
            + (f" (rank #{market.market_cap_rank})" if market.market_cap_rank else ""),
            f"  24h volume: {_fmt_usd(market.volume_24h)}",
            f"  Price: {market.price if market.price is not None else 'n/a'}"
            + (
                f" ({market.price_change_24h:+.2f}% 24h)"
                if market.price_change_24h is not None
                else ""
            ),
        ]
    if position:
        lines += [
            "",
            "OPEN POSITION (analyse and advise on this):",
            f"  {position.side.upper()} {position.size} {position.symbol} on {position.exchange}",
            f"  Entry: {position.entry_price:.6g}  |  Mark: {position.mark_price:.6g}",
            f"  Unrealized PnL: {position.pnl:+.2f} ({position.pnl_pct:+.2f}%)",
            f"  Leverage: {position.leverage if position.leverage is not None else 'n/a'}"
            + (
                f"  |  Liquidation: {position.liquidation_price:.6g}"
                if position.liquidation_price is not None
                else ""
            ),
        ]
    lines.append("\nWrite the analysis now.")
    return "\n".join(lines)


async def _run_llm(user_prompt: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (analysis_text, provider, error) via the AI Analyst gateway (guarded).

    Implements two-attempt failover:
    1. Normal routing (circuit breakers active).
    2. If the first attempt returns no usable text, retry with bypass_circuits=True
       so providers blocked by the circuit breaker are tried again immediately.
    This prevents a single transient Groq/provider failure from permanently falling
    back to the deterministic summary for the next 2 minutes.
    """
    try:
        from app.core.database import AsyncSessionLocal
        from plugins.AiMarketAnalyst.backend.services.ai_router import (
            db_chat, resolve_model_for_task,
        )
    except Exception as exc:
        return None, None, f"AI gateway unavailable: {exc}"

    # Narrating a forecast is long-horizon reasoning, so take the deep-reasoning
    # chain. The two attempts below use different models from it where possible:
    # a model that returned nothing usable rarely does better on an immediate
    # repeat, but its stablemate often does.
    _chain = resolve_model_for_task("deep_reasoning")

    _msgs = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    _kwargs: dict = dict(
        max_tokens=1500,    # generous budget for 500-620 word thorough analysis
        temperature=0.38,   # slightly warmer → richer, more detailed prose
        agent_name="kronos-jarvis",
        agent_role="forecaster",
        source="forecaster",  # NOT "agent" — avoids per_agent_max_tokens DB cap
        preferred_providers=["nvidia", "github"],  # NVIDIA first, GitHub gpt-4o fallback
        bypass_circuits=True,  # always try NVIDIA/GitHub even if circuit was tripped
    )
    # NVIDIA's Nemotron reasoning model needs ~90s for a full 1500-token analysis,
    # far past the router's default 40s. Give it a real budget (the frontend
    # allows 120s) but keep the two attempts inside that window so a genuinely
    # stuck provider still fails back to the rule-based summary in time.
    _ATTEMPT_TIMEOUT = float(os.getenv("KRONOS_JARVIS_TIMEOUT_S", "105"))
    _start = time.monotonic()

    def _extract(result: dict) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """Extract (text, provider, error) from a db_chat result dict."""
        if not result.get("ok"):
            return None, result.get("provider"), result.get("error") or "AI call failed"
        content = result.get("content")
        if isinstance(content, dict):
            content = content.get("text") or content.get("content") or str(content)
        text = (content or "").strip()
        return (text or None), result.get("provider"), None

    # Groq (and some other providers) truncates responses to a few hundred tokens.
    # Reject any response shorter than this — it means the model was cut off.
    # 500 words × ~5 chars/word = ~2500 chars minimum for a proper analysis.
    _MIN_CHARS = 2500  # ~500 words; anything shorter is a truncated response

    # ── Attempt 1: NVIDIA only, circuit bypassed (──────────────────────────
    try:
        async with AsyncSessionLocal() as db:
            result1 = await db_chat(
                db, _msgs, timeout=_ATTEMPT_TIMEOUT,
                model_override=_chain[0] if _chain else None, **_kwargs,
            )
    except Exception as exc:
        logger.warning("[Kronos JARVIS] db_chat exception on attempt 1: {}", exc)
        result1 = {"ok": False, "error": f"AI call failed: {exc}"}

    text1, provider1, err1 = _extract(result1)
    if text1 and len(text1) >= _MIN_CHARS:
        return text1, provider1, None  # full, complete NVIDIA response

    # Either empty OR suspiciously short → retry
    if text1:
        first_issue = (
            f"response too short ({len(text1)} chars via {provider1})"
        )
        logger.info(
            "[Kronos JARVIS] Attempt 1 response too short ({} chars via {}) — "
            "retrying …",
            len(text1), provider1,
        )
    else:
        first_issue = err1 or "empty response from provider"
        logger.info(
            "[Kronos JARVIS] Attempt 1 yielded no content ({}); retrying …",
            first_issue,
        )

    # ── Attempt 2: retry NVIDIA (circuit already bypassed in _kwargs) ───────
    # Only retry if there is time left in the frontend's window — a first attempt
    # that timed out already consumed the budget, so retrying would just blow past
    # the client and still fail. Bound attempt 2 to whatever remains.
    _remaining = _ATTEMPT_TIMEOUT - (time.monotonic() - _start)
    if _remaining < 15.0:
        logger.info(
            "[Kronos JARVIS] No budget for a retry ({:.0f}s left) — using fallback.",
            _remaining,
        )
        return None, provider1, first_issue

    try:
        async with AsyncSessionLocal() as db:
            result2 = await db_chat(
                db, _msgs, timeout=min(_ATTEMPT_TIMEOUT, _remaining),
                model_override=_chain[1] if len(_chain) > 1 else (_chain[0] if _chain else None),
                **_kwargs,
            )
    except Exception as exc:
        logger.warning("[Kronos JARVIS] db_chat exception on attempt 2: {}", exc)
        result2 = {"ok": False, "error": f"retry failed: {exc}"}

    text2, provider2, err2 = _extract(result2)
    if text2 and len(text2) >= _MIN_CHARS:
        logger.info("[Kronos JARVIS] Attempt 2 succeeded via {}", provider2)
        return text2, provider2, None  # retry succeeded

    # Both attempts failed — surface a compact combined error
    combined = first_issue if (not err2 or err2 == first_issue) else f"{first_issue} | retry: {err2}"
    return None, provider2 or provider1, combined


async def _capture_to_vault(
    resp: ForecastResponse, analysis: str, market: Optional[MarketCapInfo]
) -> bool:
    """Write the analysis to the Obsidian knowledge vault AND register it in the
    vault DB so it appears on /vault (note list) and the /intelligence live feed.
    Guarded so the Kronos plugin stays standalone."""
    try:
        from datetime import datetime
        from sqlalchemy import select
        from app.core.database import AsyncSessionLocal
        from plugins.ObsidianKnowledgePlugin.backend.services.vault_writer import VaultWriter
        from plugins.ObsidianKnowledgePlugin.backend.services.obsidian_rest import get_bridge
        from plugins.ObsidianKnowledgePlugin.backend.models import VaultNote
    except Exception:
        return False

    sig = resp.signal
    cap = _fmt_usd(market.market_cap) if market else None
    if sig:
        summary = (
            f"Kronos {resp.symbol} {resp.timeframe}: {sig.direction} "
            f"{sig.pct_change:+.2f}% → {sig.target_price:.6g} "
            f"(conf {sig.confidence * 100:.0f}%)"
        )
    else:
        summary = f"Kronos forecast — {resp.symbol} {resp.timeframe}"
    tags = ["kronos", "forecast", resp.timeframe]
    if cap:
        tags.append("crypto")

    try:
        writer = VaultWriter()
        path, written, cs = writer.write_action_note(
            action_type="kronos-forecast",
            symbol=resp.symbol.replace("/", ""),
            summary=summary,
            detail=analysis,
            tags=tags,
            agent_role="forecaster",
            confidence=(sig.confidence if sig else None),
        )
        rel = str(path.relative_to(writer.root))
    except Exception as exc:
        logger.warning(f"[Kronos] vault write failed: {exc!r}")
        return False

    # Register in the vault DB so /vault + /intelligence feed pick it up.
    try:
        async with AsyncSessionLocal() as db:
            existing = (
                await db.execute(select(VaultNote).where(VaultNote.path == rel))
            ).scalar_one_or_none()
            now = datetime.utcnow()
            if existing:
                existing.checksum = cs
                existing.updated_at = now
            else:
                db.add(VaultNote(
                    path=rel,
                    note_type="kronos-forecast",
                    symbol=resp.symbol.replace("/", ""),
                    tags=tags,
                    checksum=cs,
                    created_at=now,
                    updated_at=now,
                ))
            await db.commit()
    except Exception as exc:
        logger.warning(f"[Kronos] vault DB register failed: {exc!r}")
        # File was written even if DB registration failed.

    # Best-effort live push to the Obsidian app.
    try:
        bridge = get_bridge()
        if getattr(bridge, "enabled", False):
            await bridge.push_note(rel, path.read_text(encoding="utf-8"))
    except Exception:
        pass

    return True


async def _learn(
    resp: ForecastResponse, analysis: str, market: Optional[MarketCapInfo]
) -> bool:
    """Persist the analysis to every JARVIS brain surface:
    the shared AI knowledge store (powers /intelligence) AND the Obsidian
    knowledge vault (powers /vault + the /intelligence live feed). Guarded so
    the plugin stays standalone."""
    stored = False

    # 1) Shared AI knowledge brain (AI Analyst) → /intelligence knowledge panel
    try:
        from app.core.database import AsyncSessionLocal
        from plugins.AiMarketAnalyst.backend.services import knowledge_service
        sig = resp.signal
        title = f"Kronos forecast — {resp.symbol} {resp.timeframe}"
        cap = _fmt_usd(market.market_cap) if market else None
        body = analysis
        if sig:
            # The macro clause goes in the structured header, not only in the
            # prose: this line is what recall and the knowledge panel match on,
            # and it survives even when the narrative came from the
            # deterministic fallback rather than the model.
            macro_note = next(
                (r for r in (sig.rationale or []) if r.startswith("Macro")), ""
            )
            body = (
                f"[{resp.engine}] {resp.symbol} {resp.timeframe}: {sig.direction} "
                f"{sig.pct_change:+.2f}% (conf {sig.confidence * 100:.0f}%), target "
                f"{sig.target_price:.6g} from {resp.anchor_price:.6g}"
                + (f", mcap {cap}" if cap else "")
                + (f", {macro_note.rstrip('.')}" if macro_note else "")
                + f".\n\n{analysis}"
            )
        async with AsyncSessionLocal() as db:
            await knowledge_service.store_knowledge(
                db,
                content=body,
                agent_role="forecaster",
                symbol=resp.symbol,
                kind="forecast",
                title=title,
                weight=1.5,
                source="kronos",
            )
        stored = True
    except Exception as exc:
        logger.warning(f"[Kronos] AI knowledge store failed: {exc!r}")

    # 2) Obsidian vault → /vault + /intelligence live feed
    captured = await _capture_to_vault(resp, analysis, market)

    return stored or captured


def _fallback_analysis(
    resp: ForecastResponse,
    market: Optional[MarketCapInfo],
    position: Optional[PositionInfo] = None,
) -> str:
    """Deterministic summary so the page always shows something useful."""
    sig = resp.signal
    if not sig:
        return f"No Kronos forecast is available for {resp.symbol} on {resp.timeframe}."
    vol = resp.volume

    # Volume failed its precondition → the only honest summary is why.
    if resp.decision == "NO_TRADE":
        parts = [
            f"NO_TRADE on {resp.symbol} ({resp.timeframe}). Volume is a hard "
            f"precondition for a Kronos direction call and it is "
            f"{(vol.status.lower() if vol else 'unavailable')}.",
        ]
        if vol and vol.detail:
            parts.append(vol.detail)
        parts.append(
            "No direction has been inferred from price alone. Re-run once a full "
            "rolling 24 hours of volume is available for this symbol."
        )
        return " ".join(parts)

    band_lo = resp.lower_band[-1].value if resp.lower_band else None
    band_hi = resp.upper_band[-1].value if resp.upper_band else None
    parts = [
        f"Kronos ({resp.engine}) forecasts {resp.symbol} {sig.direction} "
        f"{sig.pct_change:+.2f}% over the next {resp.pred_len}×{resp.timeframe}, "
        f"targeting {sig.target_price:.6g} from {resp.anchor_price:.6g} at "
        f"{sig.confidence * 100:.0f}% confidence.",
    ]
    if vol and vol.status == "OK":
        parts.append(
            f"Volume backs this read: 24h {_fmt_usd(vol.volume_24h)} equivalent, last "
            f"completed hour {vol.volume_1h:,.4g} vs an hourly mean of "
            f"{vol.hourly_mean_24h:,.4g} (×{vol.relative_volume:.2f}) — a "
            f"{vol.regime} regime with {vol.divergence} price/volume behaviour."
        )
        if vol.reversal_risk:
            parts.append(
                "Reversal risk: climactic volume is confirming the opposite move, so "
                "the confidence has been cut accordingly."
            )

    # Macro, on the deterministic path too. A NO_TRADE skips the LLM entirely,
    # so without this the one case where the trader most wants to know what was
    # considered would be the one case that never says.
    macro_lines = [
        line for line in (getattr(resp.signal, "rationale", None) or [])
        if "DXY" in line or "VIX" in line or line.startswith("Macro")
    ]
    if macro_lines:
        parts.append(" ".join(macro_lines))

    if resp.decision == "LOW_CONFIDENCE":
        parts.append(
            "This sits below the tradeable confidence floor once volume is factored "
            "in — treat it as information, not an entry."
        )
    if band_lo is not None and band_hi is not None:
        parts.append(
            f"The p10–p90 band ({band_lo:.6g}–{band_hi:.6g}) frames the uncertainty; "
            "a wider band means lower conviction."
        )
    if market and market.market_cap:
        parts.append(
            f"{market.name or resp.symbol} carries a {_fmt_usd(market.market_cap)} market cap"
            + (f" (rank #{market.market_cap_rank})" if market.market_cap_rank else "")
            + (f" on {_fmt_usd(market.volume_24h)} 24h volume" if market.volume_24h else "")
            + "; deeper liquidity supports the move, thin volume raises slippage risk."
        )
    adv = _fallback_position_advice(resp, position)
    if adv:
        parts.append("Position: " + adv)
    return " ".join(parts)


def _fallback_position_advice(
    resp: ForecastResponse, position: Optional[PositionInfo]
) -> Optional[str]:
    """Deterministic hold/reduce/close guidance when the LLM is unavailable."""
    if not position or not resp.signal:
        return None
    sig = resp.signal
    if resp.decision == "NO_TRADE":
        vol = resp.volume
        return (
            f"Volume context is {(vol.status.lower() if vol else 'unavailable')} for "
            f"{resp.symbol}, so there is no forecast to judge your {position.side} "
            f"({position.pnl:+.2f}%) against. Manage it on your own stop until volume "
            f"data returns — no direction is being inferred from price alone."
        )
    aligned = (
        (position.side == "long" and sig.direction == "up")
        or (position.side == "short" and sig.direction == "down")
    )
    band_lo = resp.lower_band[-1].value if resp.lower_band else None
    band_hi = resp.upper_band[-1].value if resp.upper_band else None
    tp = f"{sig.target_price:.6g}"
    if position.side == "long":
        sl = f"{band_lo:.6g}" if band_lo is not None else f"{position.entry_price:.6g}"
    else:
        sl = f"{band_hi:.6g}" if band_hi is not None else f"{position.entry_price:.6g}"
    if sig.direction == "flat":
        return (
            f"Your {position.side} ({position.pnl:+.2f}%) faces a flat forecast — "
            f"consider trimming and tightening the stop near {position.entry_price:.6g}."
        )
    if aligned:
        return (
            f"The forecast favours your {position.side} ({position.pnl:+.2f}% PnL) — "
            f"hold toward take-profit {tp}; trail the stop to {sl} to protect gains."
        )
    return (
        f"The forecast runs against your {position.side} ({position.pnl:+.2f}% PnL) — "
        f"consider reducing or closing; if held, set a tight stop near {sl}."
    )


def _extract_position_section(analysis: str) -> Optional[str]:
    """Pull the 'Position:' section out of the LLM analysis, if present."""
    if not analysis:
        return None
    low = analysis.lower()
    idx = low.rfind("position:")
    if idx == -1:
        return None
    section = analysis[idx + len("position:"):]
    # Strip leading markdown emphasis / punctuation / whitespace.
    section = section.lstrip("*_:# \n\r\t")
    return section.strip() or None


async def analyze_forecast(
    resp: ForecastResponse, *, learn: bool = True
) -> JarvisAnalysisResponse:
    """Produce a JARVIS explanation of the forecast, attach crypto market cap,
    detect any open position on the symbol, advise on it, and (optionally) store
    it to the knowledge brain."""
    market = await _market_context(resp.symbol)
    position = await _open_position(resp.exchange, resp.symbol)

    # Volume gate: a refused forecast gets a deterministic explanation, not an
    # LLM narration that could talk itself into a direction we did not compute.
    if resp.decision == "NO_TRADE":
        vol = resp.volume
        analysis = _fallback_analysis(resp, market, position)
        return JarvisAnalysisResponse(
            exchange=resp.exchange, symbol=resp.symbol, timeframe=resp.timeframe,
            engine=resp.engine, analysis=analysis,
            spoken=(resp.signal.summary if resp.signal
                    else f"No trade on {resp.symbol} — volume unavailable."),
            signal=resp.signal, market=market, position=position,
            position_advice=_fallback_position_advice(resp, position),
            volume=vol, decision="NO_TRADE", learned=False, provider=None,
            note=(
                f"NO_TRADE — volume is {(vol.status.lower() if vol else 'unavailable')} "
                f"for {resp.symbol}; no direction was inferred."
            ),
        )

    analysis, provider, err = await _run_llm(_build_user_prompt(resp, market, position))

    note: Optional[str] = None
    if analysis is None:
        analysis = _fallback_analysis(resp, market, position)
        # Build a concise, non-alarming note so the UI gives useful context
        # without showing raw error strings from the provider.
        if err:
            # Shorten provider error chains — keep max 120 chars
            short_err = err[:120] + ("…" if len(err) > 120 else "")
            note = f"AI unavailable ({short_err}) — rule-based summary shown."
        else:
            note = "AI provider returned no content — rule-based summary shown."
        learned = False
        position_advice = _fallback_position_advice(resp, position)
    else:
        learned = await _learn(resp, analysis, market) if learn else False
        position_advice = _extract_position_section(analysis) or (
            _fallback_position_advice(resp, position) if position else None
        )

    sig = resp.signal
    spoken = sig.summary if sig else f"No forecast available for {resp.symbol}."
    return JarvisAnalysisResponse(
        exchange=resp.exchange,
        symbol=resp.symbol,
        timeframe=resp.timeframe,
        engine=resp.engine,
        analysis=analysis,
        spoken=spoken,
        signal=sig,
        market=market,
        position=position,
        position_advice=position_advice,
        volume=resp.volume,
        decision=resp.decision,
        learned=learned,
        provider=provider,
        note=note,
    )
