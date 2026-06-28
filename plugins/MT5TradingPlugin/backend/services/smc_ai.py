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
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from plugins.AiMarketAnalyst.backend.services.ai_router import db_chat


_SYSTEM_PROMPT = (
    "You are an institutional Smart Money Concepts (SMC) trading analyst. "
    "You are given pre-computed market structure, order blocks, fair-value-gaps, "
    "liquidity pools, volume and momentum for a single instrument plus candidate "
    "resting limit-entry setups produced by a deterministic engine. "
    "Your role: review and rate each setup for sniper-quality, low-drawdown entries. "
    "Do NOT invent or adjust price levels. "
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

    return {
        "bias_comment": str(raw.get("bias_comment", ""))[:400],
        "market_read": str(raw.get("market_read", ""))[:600],
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
) -> Dict[str, Any]:
    """
    Ask the DB-backed AI router to review the SMC engine's analysis.

    Uses whichever provider is active in the AI Providers panel (same as
    telegram-signals and /agents). Always returns a dict with `available` so
    the frontend degrades gracefully when no provider is configured.
    """
    signals = analysis.get("signals", [])
    valid_entries = [float(s["entry"]) for s in signals if "entry" in s]

    if not signals:
        return {"available": False, "reason": "No setups to review"}

    # Compact, numbers-only context so the model cannot wander off-data.
    context = {
        "symbol": symbol,
        "timeframe": timeframe,
        "bias": analysis.get("bias"),
        "last_price": analysis.get("last_price"),
        "atr": analysis.get("atr"),
        "atr_pct": analysis.get("atr_pct"),
        "rsi": analysis.get("rsi"),
        "volume_z": analysis.get("volume_z"),
        "momentum": analysis.get("momentum"),
        "equilibrium": analysis.get("equilibrium"),
        "range": analysis.get("range"),
        "liquidity": analysis.get("liquidity"),
        "structure_events": (analysis.get("structure_events") or [])[-6:],
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

    try:
        result = await db_chat(
            db,
            messages,
            temperature=0.15,
            max_tokens=900,
            json_mode=True,
            agent_name="smc_sniper",
            agent_role="market_analyst",
            source="smc_sniper",
        )
    except Exception as exc:
        logger.warning(f"[MT5/SMC-AI] db_chat failed: {exc}")
        return {"available": False, "reason": f"AI call failed: {exc}"}

    if not result.get("ok"):
        return {"available": False, "reason": result.get("error", "All providers failed")}

    validated = _validate(result.get("content"), valid_entries)
    if validated is None:
        logger.warning("[MT5/SMC-AI] AI returned unparseable output: %s",
                       str(result.get("content", ""))[:200])
        return {"available": False, "reason": "AI returned malformed JSON"}

    validated["available"] = True
    validated["provider"] = result.get("provider")
    validated["model"] = result.get("model")
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
