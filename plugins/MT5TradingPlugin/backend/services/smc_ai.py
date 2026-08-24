"""
MT5 Trading Plugin — AI market-analysis overlay for the SMC sniper engine.

Uses the same DB-backed AI router (`ai_router.db_chat`) as the telegram-signals
sniper analysis and the /agents page. All provider configuration, usage tracking,
free-tier limits, and load-balancing strategy (priority / round-robin / least-used)
come from the providers configured at /ai-analyst → AI Providers — not env vars.

Graceful fallback: if no provider is configured, `available=False` is returned
and the engine-only result is shown without breaking the analysis flow.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from plugins.AiMarketAnalyst.backend.services.ai_router import db_chat
from plugins.AiMarketAnalyst.backend.services import analysis_router
from plugins.MT5TradingPlugin.backend.services import smc_floor, smc_memory


def _structure_window() -> int:
    """How many recent market-structure events the AI reviews.

    Floored at 28 so the model always weighs the current candle against at least
    the last ~28 closed candles' structure, honouring AGENT_ANALYSIS_CANDLES
    when the core app config is importable. No hard upper cap.
    """
    try:
        from app.core.config import settings  # type: ignore
        return max(28, int(getattr(settings, "AGENT_ANALYSIS_CANDLES", 120) or 120))
    except Exception:
        return 28



_SYSTEM_PROMPT = (
    "You are an institutional Smart Money Concepts (SMC) trading analyst. "
    "You are given pre-computed market structure, order blocks, fair-value-gaps, "
    "liquidity pools, volume and momentum for a single instrument plus candidate "
    "resting limit-entry setups produced by a deterministic engine. "
    "Your role: review and rate each setup for sniper-quality, low-drawdown entries. "
    "Do NOT invent or adjust price levels. "
    "You are also given `similar_past_setups`: the closest historical setups on "
    "this instrument by factor profile, each with its REALISED result (R multiple, "
    "MFE, MAE, exit reason), and `realised_performance` aggregating this "
    "instrument's actual win rate and average R. These are measured outcomes, not "
    "forecasts. Weight your verdicts by them: if near-identical setups have "
    "historically lost, say so in the note and rate accordingly. "
    "Respond with STRICT JSON only, no prose, matching this schema exactly:\n"
    '{"bias_comment": str, "market_read": str, '
    '"rated_signals": [{"entry": number, "verdict": "take"|"skip"|"watch", '
    '"confidence": number (0.0-1.0), "note": str}], '
    '"top_pick_entry": number|null, "risk_warning": str}'
)


def _clamp01(x: Any) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, v))


def _validate(raw: Any, valid_entries: List[float]) -> Optional[Dict[str, Any]]:
    """Validate and sanitise LLM JSON against the expected schema.

    Key safety rule: only accept ratings whose entry is within 0.01% of a real
    engine-computed entry. This prevents the model from hallucinating price levels.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            # Try to extract first {...} block (some models wrap JSON in text)
            start = raw.find("{")
            end = raw.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    raw = json.loads(raw[start:end + 1])
                except json.JSONDecodeError:
                    return None
            else:
                return None
    if not isinstance(raw, dict):
        return None

    rated_in = raw.get("rated_signals")
    rated_out: List[Dict[str, Any]] = []
    if isinstance(rated_in, list):
        for item in rated_in:
            if not isinstance(item, dict):
                continue
            try:
                entry = round(float(item.get("entry")), 6)
            except (TypeError, ValueError):
                continue
            # Only accept ratings that map to a real engine entry (no hallucinated levels).
            if not any(abs(entry - e) <= max(abs(e) * 1e-4, 1e-6) for e in valid_entries):
                continue
            verdict = str(item.get("verdict", "watch")).lower()
            if verdict not in ("take", "skip", "watch"):
                verdict = "watch"
            rated_out.append({
                "entry": entry,
                "verdict": verdict,
                "confidence": _clamp01(item.get("confidence")),
                "note": str(item.get("note", ""))[:280],
            })

    top = raw.get("top_pick_entry")
    try:
        top_val = round(float(top), 6) if top is not None else None
        if top_val is not None and not any(abs(top_val - e) <= max(abs(e) * 1e-4, 1e-6) for e in valid_entries):
            top_val = None
    except (TypeError, ValueError):
        top_val = None

    market_read = str(raw.get("market_read", "")).strip()[:600]
    bias_comment = str(raw.get("bias_comment", "")).strip()[:400]

    # Substance check. A model that answers `{}`, or that rates none of the real
    # engine entries, has told us nothing — reject so the router cascades to the
    # next provider instead of surfacing an empty panel as a success.
    if not rated_out and valid_entries:
        return None
    if not market_read and not bias_comment:
        return None

    return {
        "bias_comment": bias_comment,
        "market_read": market_read,
        "rated_signals": rated_out,
        "top_pick_entry": top_val,
        "risk_warning": str(raw.get("risk_warning", ""))[:300],
    }


def _extract_content(result: Dict[str, Any]) -> Any:
    """Pull the model text/JSON out of the gateway result shape (kept for compat)."""
    if not isinstance(result, dict):
        return None
    for key in ("content", "text", "output", "message"):
        if key in result and result[key]:
            return result[key]
    return None


async def ai_review(
    *,
    db: AsyncSession,
    symbol: str,
    timeframe: str,
    analysis: Dict[str, Any],
    kronos_forecast: Optional[Dict[str, Any]] = None,
    market: str = "mt5",
) -> Dict[str, Any]:
    """
    Ask the DB-backed AI router to review the SMC engine's analysis.

    Uses whichever provider is active in the AI Providers panel (same as
    telegram-signals and /agents). Always returns a dict with `available` so
    the frontend degrades gracefully when no provider is configured.

    ``kronos_forecast`` is the Sox ML K-line forecast computed by the router
    from the SAME candles the SMC engine analysed (so it works for any symbol,
    including XAUUSD). It is folded into the model's context as a secondary
    confirmation signal.
    """
    signals = analysis.get("signals", [])
    valid_entries = [float(s["entry"]) for s in signals if "entry" in s]

    # No setups (or the engine could not analyse at all) still gets a complete,
    # valid block explaining why — never an empty AI panel.
    if not signals or analysis.get("error"):
        return smc_floor.build(
            analysis,
            reason=analysis.get("error") or "no candidate setups to review",
        )

    # Sox ML K-line forecast (supplied by the router from the chart's candles).
    kronos_fc = None
    if isinstance(kronos_forecast, dict) and kronos_forecast.get("direction"):
        kronos_fc = {
            "direction": kronos_forecast.get("direction"),
            "pct_change": kronos_forecast.get("pct_change"),
            "confidence": kronos_forecast.get("confidence"),
            "engine": kronos_forecast.get("engine"),
        }

    # Compact, numbers-only context so the model cannot wander off-data.
    # Enrich with upcoming economic events for this symbol so the AI can
    # flag imminent high-impact news (CPI, FOMC, NFP, interest rates, etc.)
    eco_events: List[Dict[str, Any]] = []
    try:
        eco_events = await fetch_economic_events(symbol)
    except Exception:
        pass

    # ── Grounded history ─────────────────────────────────────────────────────
    # For the top candidate, pull the closest historical setups by factor
    # profile along with what they actually returned, plus this instrument's
    # realised performance. These are measured results, not opinions — they let
    # the model reason from what happened rather than from priors.
    prior_setups: List[Dict[str, Any]] = []
    realised: Dict[str, Any] = {}
    try:
        top = signals[0]
        prior_setups = await smc_memory.similar_setups(
            db,
            market=market,
            symbol=symbol,
            side=top.get("side", ""),
            factors=smc_memory.factor_vector(top.get("score_breakdown")),
            limit=3,
        )
        realised = await smc_memory.outcome_summary(
            db, market=market, symbol=symbol, side=top.get("side")
        )
    except Exception as exc:  # noqa: BLE001 — history is an enhancement
        logger.debug(f"[MT5/SMC-AI] prior-setup recall skipped: {exc}")

    context = {
        "symbol": symbol,
        "timeframe": timeframe,
        "bias": analysis.get("bias"),
        "htf_bias": analysis.get("htf_bias"),
        # Realised outcomes of the most similar prior setups on this instrument.
        "similar_past_setups": prior_setups,
        "realised_performance": realised,
        "last_price": analysis.get("last_price"),
        "atr": analysis.get("atr"),
        "atr_pct": analysis.get("atr_pct"),
        "rsi": analysis.get("rsi"),
        "volume_z": analysis.get("volume_z"),
        "momentum": analysis.get("momentum"),
        "sox_ml_forecast": kronos_fc,
        "equilibrium": analysis.get("equilibrium"),
        "range": analysis.get("range"),
        "liquidity": analysis.get("liquidity"),
        "structure_events": (analysis.get("structure_events") or [])[-_structure_window():],
        "economic_events": eco_events[:6],
        "candidate_signals": [
            {
                "side": s["side"],
                "entry": s["entry"],
                "stop_loss": s["stop_loss"],
                "take_profit": s["take_profit"],
                "rr": s["rr"],
                "confidence": s["confidence"],
                "zone_kind": s["zone_kind"],
                "confluence": s.get("confluence", []),
            }
            for s in signals
        ],
    }

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(context, separators=(",", ":"))},
    ]

    # Tier 1/2: health-ranked provider cascade with a 12 s hard cap per attempt.
    # A schema-invalid response is rejected by `validator` and cascades to the
    # next provider rather than surfacing as a failure.
    try:
        result = await analysis_router.analyze_with_cascade(
            db,
            messages,
            validator=lambda raw: _validate(raw, valid_entries),
            temperature=0.15,
            max_tokens=900,
            json_mode=True,
        )
    except Exception as exc:  # noqa: BLE001 — the cascade must never propagate
        logger.warning(f"[MT5/SMC-AI] cascade raised unexpectedly: {exc}")
        result = {"ok": False, "errors": [str(exc)]}

    if not result.get("ok"):
        # Tier 3: deterministic floor. Always a complete, valid block.
        reason = "; ".join(result.get("errors") or ["all providers failed"])[:200]
        logger.warning(f"[MT5/SMC-AI] falling back to deterministic floor: {reason}")
        return smc_floor.build(analysis, reason=reason)

    validated: Dict[str, Any] = dict(result["content"])
    rated = validated.get("rated_signals") or []
    top_conf = max((float(r.get("confidence") or 0.0) for r in rated), default=0.0)

    validated["available"] = True
    validated["provider"] = result.get("provider_used")
    validated["provider_used"] = result.get("provider_used")
    validated["model"] = result.get("model")
    validated["tier"] = result.get("tier")
    validated["confidence"] = round(top_conf, 3)
    validated["is_degraded"] = result.get("tier") != analysis_router.TIER_PRIMARY
    validated["latency_ms"] = result.get("latency_ms")
    return validated


_BACKTEST_SYSTEM_PROMPT = (
    "You are an institutional trading-strategy analyst reviewing the backtest of "
    "a Smart Money Concepts (SMC) sniper strategy. You are given aggregate stats "
    "and a sample of trades (entry, outcome, R multiple, confluence tags). "
    "Assess edge quality, consistency, drawdown behaviour and what drove wins vs "
    "losses. Be concise, concrete and honest — never inflate results. "
    "Respond with STRICT JSON only, matching this schema exactly:\n"
    '{"verdict": "strong"|"workable"|"weak", "summary": str, '
    '"strengths": [str], "weaknesses": [str], "recommendations": [str]}'
)


async def ai_backtest_review(
    *,
    db: AsyncSession,
    symbol: str,
    timeframe: str,
    stats: Dict[str, Any],
    trades: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Ask the AI router to analyse a completed backtest and suggest improvements."""
    if not stats or stats.get("total", 0) == 0:
        return {"available": False, "reason": "No trades to analyse"}

    # Summarise winners vs losers by confluence so the model can explain edge.
    def _tag_counts(outcome: str) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for t in trades:
            if t.get("outcome") == outcome:
                for c in t.get("confluence", []) or []:
                    counts[c] = counts.get(c, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1])[:8])

    context = {
        "symbol": symbol,
        "timeframe": timeframe,
        "stats": {
            k: stats.get(k) for k in (
                "total", "wins", "losses", "breakevens", "win_rate",
                "profit_factor", "expectancy_r", "total_r", "max_drawdown_r",
                "avg_win_r", "avg_loss_r", "starting_balance", "ending_balance",
                "net_profit_pct", "max_drawdown_currency", "trading_days",
                "avg_daily_pct",
            ) if stats.get(k) is not None
        },
        "sample_trades": [
            {
                "side": t.get("side"), "outcome": t.get("outcome"),
                "r": t.get("r_multiple"), "conf": t.get("confidence"),
                "trailed": t.get("trailed"),
            }
            for t in trades[-20:]
        ],
        "winning_setup_tags": _tag_counts("win"),
        "losing_setup_tags": _tag_counts("loss"),
    }

    messages = [
        {"role": "system", "content": _BACKTEST_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(context, separators=(",", ":"))},
    ]

    try:
        result = await db_chat(
            db, messages, temperature=0.2, max_tokens=700, json_mode=True,
            agent_name="smc_backtest", agent_role="strategy_analyst", source="smc_backtest",
        )
    except Exception as exc:
        logger.warning(f"[MT5/SMC-AI] backtest db_chat failed: {exc}")
        return {"available": False, "reason": f"AI call failed: {exc}"}

    if not result.get("ok"):
        return {"available": False, "reason": result.get("error", "All providers failed")}

    content = result.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            s, e = content.find("{"), content.rfind("}")
            if s != -1 and e > s:
                try:
                    content = json.loads(content[s:e + 1])
                except json.JSONDecodeError:
                    return {"available": False, "reason": "AI returned malformed JSON"}
            else:
                return {"available": False, "reason": "AI returned malformed JSON"}
    if not isinstance(content, dict):
        return {"available": False, "reason": "AI returned unexpected output"}

    return {
        "available": True,
        "verdict": str(content.get("verdict", "")),
        "summary": str(content.get("summary", "")),
        "strengths": [str(x) for x in (content.get("strengths") or [])][:5],
        "weaknesses": [str(x) for x in (content.get("weaknesses") or [])][:5],
        "recommendations": [str(x) for x in (content.get("recommendations") or [])][:5],
        "provider": result.get("provider"),
        "model": result.get("model"),
    }


# ── Economic Calendar helper ──────────────────────────────────────────────────
# Symbol-scoped view over the shared calendar window. The window itself (fetch,
# two-week horizon, cache) lives in ``economic_calendar`` so the /research page,
# the agent reminder and this prompt all read one fetch instead of three.


async def fetch_economic_events(symbol: str, lookback_hours: int = 48,
                                lookahead_hours: int = 72) -> List[Dict[str, Any]]:
    """
    Return upcoming and recent high-impact economic events for the currencies in
    ``symbol``.  Uses the ForexFactory public JSON calendar (no API key required).

    Results are sorted nearest-first and limited to 10 to stay token-efficient
    for the AI context window.  Always fails gracefully — never raises.
    """
    from plugins.MT5TradingPlugin.backend.services import economic_calendar as eco

    key = (symbol or "").upper().replace("/", "")
    currencies = eco.ECO_CURRENCY_MAP.get(key, [key[:3], key[3:6]])

    try:
        window = await eco.fetch_calendar_window()
    except Exception as e:  # noqa: BLE001 — scheduled risk is advisory, never fatal
        logger.debug(f"[economic_events] window unavailable: {e}")
        return []

    events = eco.query_calendar(
        window,
        currencies=currencies,
        impact="high",
        days=max(1, lookahead_hours // 24 + 1),
        lookback_hours=lookback_hours,
    )

    # The title-keyword narrowing this caller has always applied on top of the
    # impact filter. The calendar page deliberately does not — it shows every
    # high-impact event — but the AI context stays token-tight.
    out = [
        {
            "title":      e["title"],
            "currency":   e["currency"],
            "impact":     "high",
            "time_utc":   e["time_utc"],
            "hours_away": e["hours_away"],
            "forecast":   e["forecast"],
            "previous":   e["previous"],
            "actual":     e["actual"],
        }
        for e in events
        if eco.matches_high_impact_keyword(e["title"])
        and -lookback_hours <= e["hours_away"] <= lookahead_hours
    ]
    out.sort(key=lambda x: abs(x["hours_away"]))
    return out[:10]
