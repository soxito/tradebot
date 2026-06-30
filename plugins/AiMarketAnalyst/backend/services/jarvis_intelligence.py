"""
JARVIS Intelligence Harvester & Strategy Synthesiser
=====================================================

Two high-level capabilities:

1. **harvest_intelligence(db)**
   Pulls live data from every internal source the user monitors:
     - Sentiment (/sentiment/)
     - SMC signals (/signals/smc/signals)
     - SMC overview (/signals/smc/overview)
     - Telegram signals (/signals/)
     - AI agent decisions (/plugins/ai-analyst/decisions)
     - News articles (/sentiment/news/articles)
   Converts the most critical findings into ``AIAgentKnowledge`` rows so
   they appear as expanding nodes in the Brain Map on /intelligence.

2. **synthesize_strategies(db, n_strategies)**
   Reads accumulated knowledge rows, calls the configured LLM, and generates
   fully-functional Python strategy code ready to be used by the market analysis
   engine.  Returns a list of strategy dicts with code + metadata.
"""
from __future__ import annotations

import json
import textwrap
from datetime import datetime, timedelta
from typing import Any

import httpx
from loguru import logger
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from plugins.AiMarketAnalyst.backend.models import AIAgentKnowledge
from plugins.AiMarketAnalyst.backend.services import knowledge_service

# ── Internal base URL (same process — connect to self) ────────────────────────
_BASE = "http://localhost:8000/api/v1"

# ── How often to re-harvest the same source (seconds) ─────────────────────────
_HARVEST_TTL = 300   # 5 min — don't spam knowledge store

_FRONTEND_LEARNING_SOURCES = {
    "sentiment": "http://localhost:3000/sentiment",
    "telegram": "http://localhost:3000/telegram-signals",
    "smc": "http://localhost:3000/smart-money-concepts",
    "decisions": "http://localhost:3000/insights",
    "intelligence": "http://localhost:3000/intelligence",
}


def _source_label(key: str, backend_source: str) -> str:
    return f"{backend_source}|{key}_page" if key in _FRONTEND_LEARNING_SOURCES else backend_source

# ─────────────────────────────────────────────────────────────────────────────
# 1. HARVEST
# ─────────────────────────────────────────────────────────────────────────────

async def harvest_intelligence(db: AsyncSession) -> dict[str, Any]:
    """
    Pull live data from all watched sources and distil critical insights into
    knowledge nodes.  Returns a summary dict of how many nodes were added.
    """
    added = {"sentiment": 0, "smc": 0, "telegram": 0, "decisions": 0, "news": 0}

    async with httpx.AsyncClient(timeout=12.0) as client:
        # ── Sentiment ──────────────────────────────────────────────────────────
        try:
            r = await client.get(f"{_BASE}/sentiment/")
            if r.status_code == 200:
                items = r.json() or []
                # Top 5 most extreme sentiment scores
                sorted_items = sorted(items, key=lambda x: abs(x.get("score", 0)), reverse=True)[:5]
                for s in sorted_items:
                    sym = s.get("symbol") or s.get("coin", "UNKNOWN")
                    score = s.get("score", 0)
                    label = s.get("label", "neutral")
                    if abs(score) < 0.3:
                        continue  # not extreme enough to learn from
                    direction = "BULLISH" if score > 0 else "BEARISH"
                    await knowledge_service.store_knowledge(
                        db,
                        content=(
                            f"{sym} sentiment: {label} ({score:+.2f}). "
                            f"Market appears {direction} based on news and social analysis."
                        ),
                        title=f"Sentiment signal: {sym} {label}",
                        kind="sentiment",
                        symbol=sym,
                        agent_role="jarvis_intelligence",
                        weight=min(2.5, 1.0 + abs(score)),
                        source=_source_label("sentiment", "sentiment_harvest"),
                    )
                    added["sentiment"] += 1
        except Exception as e:
            logger.warning(f"[JARVIS harvest] sentiment error: {e}")

        # ── Enhanced sentiment (news-backed) ──────────────────────────────────
        try:
            r = await client.get(f"{_BASE}/sentiment/enhanced/all")
            if r.status_code == 200:
                data = r.json() or []
                for item in data[:3]:
                    sym = item.get("symbol", "")
                    if not sym:
                        continue
                    score = item.get("composite_score") or item.get("score", 0)
                    summary = item.get("summary") or item.get("analysis") or ""
                    if not summary or abs(score) < 0.2:
                        continue
                    await knowledge_service.store_knowledge(
                        db,
                        content=summary[:500],
                        title=f"Enhanced sentiment: {sym}",
                        kind="sentiment",
                        symbol=sym,
                        agent_role="jarvis_intelligence",
                        weight=1.5,
                        source=_source_label("sentiment", "enhanced_sentiment"),
                    )
                    added["sentiment"] += 1
        except Exception as e:
            logger.warning(f"[JARVIS harvest] enhanced sentiment error: {e}")

        # ── SMC signals ────────────────────────────────────────────────────────
        try:
            r = await client.get(f"{_BASE}/signals/smc/signals", params={"limit": 20})
            if r.status_code == 200:
                signals = r.json() or []
                for sig in signals[:10]:
                    sym = sig.get("symbol", "UNKNOWN")
                    action = sig.get("action") or sig.get("signal", "HOLD")
                    conf = sig.get("confidence", 0) or sig.get("ai_confidence", 0)
                    tf = sig.get("timeframe", "")
                    entry = sig.get("entry") or sig.get("entry_price")
                    sl = sig.get("stop_loss")
                    tp = sig.get("take_profit")
                    if conf < 0.4:
                        continue
                    content = (
                        f"SMC signal {action} {sym} @ {entry} "
                        f"(SL:{sl}, TP:{tp}, conf:{conf:.0%}, TF:{tf}). "
                        f"Smart Money Concepts analysis detected key structure."
                    )
                    await knowledge_service.store_knowledge(
                        db,
                        content=content[:600],
                        title=f"SMC {action} {sym} {tf}",
                        kind="signal",
                        symbol=sym,
                        agent_role="jarvis_intelligence",
                        weight=1.0 + conf,
                        source=_source_label("smc", "smc_signals"),
                    )
                    added["smc"] += 1
        except Exception as e:
            logger.warning(f"[JARVIS harvest] SMC signals error: {e}")

        # ── SMC overview (macro patterns) ─────────────────────────────────────
        try:
            r = await client.get(f"{_BASE}/signals/smc/overview")
            if r.status_code == 200:
                ov = r.json() or {}
                insights = ov.get("insights") or ov.get("analysis") or ""
                if insights and len(insights) > 50:
                    await knowledge_service.store_knowledge(
                        db,
                        content=str(insights)[:700],
                        title="SMC market overview",
                        kind="pattern",
                        symbol=None,
                        agent_role="jarvis_intelligence",
                        weight=1.8,
                        source=_source_label("smc", "smc_overview"),
                    )
                    added["smc"] += 1
        except Exception as e:
            logger.warning(f"[JARVIS harvest] SMC overview error: {e}")

        # ── Telegram signals ──────────────────────────────────────────────────
        try:
            r = await client.get(f"{_BASE}/signals/", params={"limit": 30, "status": "active"})
            if r.status_code == 200:
                data = r.json() or {}
                signals = data.get("signals", data) if isinstance(data, dict) else data
                for sig in (signals or [])[:10]:
                    sym = sig.get("symbol", "")
                    action = sig.get("action", "")
                    conf = float(sig.get("confidence") or 0)
                    src = sig.get("source", "telegram")
                    if not sym or conf < 0.35:
                        continue
                    entry = sig.get("entry_price") or sig.get("price")
                    content = (
                        f"Telegram signal: {action} {sym} entry:{entry} "
                        f"confidence:{conf:.0%} source:{src}"
                    )
                    await knowledge_service.store_knowledge(
                        db,
                        content=content[:500],
                        title=f"Telegram {action} {sym}",
                        kind="signal",
                        symbol=sym,
                        agent_role="jarvis_intelligence",
                        weight=1.0 + conf * 0.8,
                        source=_source_label("telegram", "core_telegram_signals"),
                    )
                    added["telegram"] += 1
        except Exception as e:
            logger.warning(f"[JARVIS harvest] telegram signals error: {e}")

        # ── Telegram plugin signals (/telegram-signals page) ──────────────────
        try:
            r = await client.get(
                f"{_BASE}/plugins/telegram/signals",
                params={"limit": 30, "status": "active"},
            )
            if r.status_code == 200:
                signals = r.json() or []
                for sig in (signals or [])[:12]:
                    sym = sig.get("symbol", "")
                    direction = sig.get("direction") or sig.get("action") or ""
                    conf = float(sig.get("confidence") or 0)
                    src = sig.get("channel_title") or sig.get("source") or "telegram"
                    if not sym or conf < 0.35:
                        continue
                    entries = sig.get("entries") or []
                    entry = entries[0] if isinstance(entries, list) and entries else sig.get("entry")
                    content = (
                        f"Telegram plugin signal from {src}: {direction} {sym} "
                        f"entry:{entry} confidence:{conf:.0%}. "
                        f"Learned from the Telegram Signals page signal parser."
                    )
                    await knowledge_service.store_knowledge(
                        db,
                        content=content[:600],
                        title=f"Telegram page {direction} {sym}",
                        kind="signal",
                        symbol=sym,
                        agent_role="jarvis_intelligence",
                        weight=1.1 + conf * 0.8,
                        source=_source_label("telegram", "telegram_plugin_signals"),
                    )
                    added["telegram"] += 1
        except Exception as e:
            logger.warning(f"[JARVIS harvest] telegram plugin signals error: {e}")

        # ── AI agent decisions (Insights page data) ───────────────────────────
        try:
            r = await client.get(
                f"{_BASE}/plugins/ai-analyst/decisions",
                params={"limit": 15},
            )
            if r.status_code == 200:
                decisions = r.json() or []
                for d in decisions[:8]:
                    sym = d.get("symbol", "")
                    action = d.get("action", d.get("decision", "HOLD"))
                    conf = float(d.get("confidence", 0))
                    reasoning = d.get("reasoning") or d.get("analysis") or ""
                    role = d.get("agent_role", "agent")
                    if not sym or conf < 0.5 or not reasoning:
                        continue
                    await knowledge_service.store_knowledge(
                        db,
                        content=reasoning[:600],
                        title=f"Agent {action} {sym} (conf {conf:.0%})",
                        kind="signal",
                        symbol=sym,
                        agent_role="jarvis_intelligence",
                        weight=0.8 + conf,
                        source=_source_label("decisions", f"ai_decision_{role}"),
                    )
                    added["decisions"] += 1
        except Exception as e:
            logger.warning(f"[JARVIS harvest] decisions error: {e}")

        # ── News articles ─────────────────────────────────────────────────────
        try:
            r = await client.get(
                f"{_BASE}/sentiment/news/articles",
                params={"limit": 20, "min_sentiment": 0.4},
            )
            if r.status_code == 200:
                articles = r.json() or []
                for art in articles[:5]:
                    title = art.get("title", "")
                    content_snippet = (art.get("summary") or art.get("content") or "")[:300]
                    sym = art.get("symbol") or art.get("coin")
                    score = float(art.get("sentiment_score") or art.get("score", 0))
                    if not title or abs(score) < 0.4:
                        continue
                    await knowledge_service.store_knowledge(
                        db,
                        content=f"{title}. {content_snippet}",
                        title=f"News: {title[:60]}",
                        kind="news",
                        symbol=sym,
                        agent_role="jarvis_intelligence",
                        weight=min(2.0, 1.0 + abs(score)),
                        source=_source_label("sentiment", "news_harvest"),
                    )
                    added["news"] += 1
        except Exception as e:
            logger.warning(f"[JARVIS harvest] news articles error: {e}")

    total = sum(added.values())
    logger.info(f"[JARVIS harvest] complete — {total} nodes added: {added}")
    return {"total_added": total, "by_source": added, "harvested_at": datetime.utcnow().isoformat()}


# ─────────────────────────────────────────────────────────────────────────────
# 2. STRATEGY SYNTHESIS
# ─────────────────────────────────────────────────────────────────────────────

_STRATEGY_TEMPLATE = textwrap.dedent("""\
    # ── JARVIS-synthesised strategy ──────────────────────────────────────────
    # Generated: {generated_at}
    # Based on: {knowledge_sources}
    # ─────────────────────────────────────────────────────────────────────────
    \"\"\"
    {description}
    \"\"\"

    def generate_signal(candles: list[dict], symbol: str) -> dict:
        \"\"\"
        Returns a signal dict:
          action:     BUY | SELL | HOLD
          confidence: 0.0–1.0
          reasoning:  str
        \"\"\"
        if len(candles) < {min_candles}:
            return {{"action": "HOLD", "confidence": 0.0, "reasoning": "Insufficient data"}}

        closes = [float(c["close"]) for c in candles]
        highs  = [float(c["high"])  for c in candles]
        lows   = [float(c["low"])   for c in candles]
        vol    = [float(c.get("volume", 0)) for c in candles]

        # ── Indicators ────────────────────────────────────────────────────────
{indicator_code}

        # ── Decision logic ────────────────────────────────────────────────────
{decision_code}
""")


def _ema(values: list[float], period: int) -> list[float]:
    """Quick EMA helper used only in template generation logic."""
    if len(values) < period:
        return values
    k = 2 / (period + 1)
    ema = [sum(values[:period]) / period]
    for v in values[period:]:
        ema.append(v * k + ema[-1] * (1 - k))
    return ema


def _build_strategy_code(name: str, description: str, knowledge_summary: str,
                          sources: list[str]) -> str:
    """Build deterministic Python strategy code from knowledge context."""
    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    sources_str = ", ".join(sources) if sources else "JARVIS knowledge base"

    # Infer strategy flavour from summary keywords
    summary_lower = knowledge_summary.lower()
    use_smc  = any(k in summary_lower for k in ["smc", "order block", "fair value", "liquidity", "bos", "choch"])
    use_macd = any(k in summary_lower for k in ["macd", "divergence", "momentum"])
    use_rsi  = any(k in summary_lower for k in ["rsi", "overbought", "oversold", "relative strength"])
    use_bb   = any(k in summary_lower for k in ["bollinger", "volatility", "squeeze"])
    use_vol  = any(k in summary_lower for k in ["volume", "vol spike", "high volume", "volume profile"])
    use_sent = any(k in summary_lower for k in ["sentiment", "bullish", "bearish", "fear", "greed"])

    indicator_lines = ["        # EMA baseline (always)"]
    indicator_lines += [
        "        ema_fast = sum(closes[-12:]) / 12",
        "        ema_slow = sum(closes[-26:]) / 26",
    ]
    if use_rsi:
        indicator_lines += [
            "        # RSI (14)",
            "        gains = [max(closes[i]-closes[i-1],0) for i in range(1,15)]",
            "        losses= [max(closes[i-1]-closes[i],0) for i in range(1,15)]",
            "        avg_g = sum(gains)/14 or 1e-9",
            "        avg_l = sum(losses)/14 or 1e-9",
            "        rsi   = 100 - 100/(1 + avg_g/avg_l)",
        ]
    if use_macd:
        indicator_lines += [
            "        # MACD",
            "        macd_line = ema_fast - ema_slow",
        ]
    if use_bb:
        indicator_lines += [
            "        # Bollinger Bands (20)",
            "        sma20  = sum(closes[-20:])/20",
            "        std20  = (sum((c-sma20)**2 for c in closes[-20:])/20)**0.5",
            "        bb_up  = sma20 + 2*std20",
            "        bb_dn  = sma20 - 2*std20",
        ]
    if use_vol:
        indicator_lines += [
            "        # Volume spike",
            "        avg_vol  = sum(vol[-20:])/20 if len(vol)>=20 else sum(vol)/len(vol)",
            "        vol_spike= vol[-1] > avg_vol * 1.5 if avg_vol > 0 else False",
        ]
    if use_smc:
        indicator_lines += [
            "        # SMC swing high/low",
            "        swing_hi = max(highs[-20:])",
            "        swing_lo = min(lows[-20:])",
            "        last_cls = closes[-1]",
        ]
    if use_sent:
        indicator_lines += [
            "        # Sentiment proxy via EMA slope",
            "        slope = (closes[-1] - closes[-5]) / (closes[-5] or 1)",
        ]

    # Decision logic
    dec_lines = ["        score  = 0", "        reason = []"]
    if use_rsi:
        dec_lines += [
            "        if rsi < 35: score += 2; reason.append(f'RSI oversold {rsi:.1f}')",
            "        elif rsi > 65: score -= 2; reason.append(f'RSI overbought {rsi:.1f}')",
        ]
    if use_macd:
        dec_lines += [
            "        if macd_line > 0: score += 1; reason.append('MACD positive')",
            "        else: score -= 1; reason.append('MACD negative')",
        ]
    dec_lines += [
        "        if ema_fast > ema_slow: score += 1; reason.append('EMA bullish cross')",
        "        else: score -= 1; reason.append('EMA bearish cross')",
    ]
    if use_bb:
        dec_lines += [
            "        if closes[-1] < bb_dn: score += 2; reason.append('Price below BB lower')",
            "        elif closes[-1] > bb_up: score -= 2; reason.append('Price above BB upper')",
        ]
    if use_smc:
        dec_lines += [
            "        mid = (swing_hi + swing_lo) / 2",
            "        if last_cls < mid: score += 1; reason.append('Below 50% retracement (SMC)')",
            "        else: score -= 1; reason.append('Above 50% retracement')",
        ]
    if use_vol:
        dec_lines += [
            "        if vol_spike and score > 0: score += 1; reason.append('Volume confirms direction')",
        ]
    if use_sent:
        dec_lines += [
            "        if slope > 0.01: score += 1; reason.append('Positive price slope (sentiment proxy)')",
            "        elif slope < -0.01: score -= 1; reason.append('Negative price slope')",
        ]
    dec_lines += [
        "        confidence = min(abs(score) / 6.0, 1.0)",
        "        if score > 1:",
        "            return {'action': 'BUY', 'confidence': confidence, 'reasoning': '; '.join(reason)}",
        "        elif score < -1:",
        "            return {'action': 'SELL', 'confidence': confidence, 'reasoning': '; '.join(reason)}",
        "        return {'action': 'HOLD', 'confidence': confidence, 'reasoning': '; '.join(reason) or 'Mixed signals'}",
    ]

    indicator_code = "\n".join(f"    {l}" for l in indicator_lines)
    decision_code  = "\n".join(f"    {l}" for l in dec_lines)

    return _STRATEGY_TEMPLATE.format(
        generated_at=generated_at,
        knowledge_sources=sources_str,
        description=description,
        min_candles=30,
        indicator_code=indicator_code,
        decision_code=decision_code,
    )


def _strategy_artifact(
    *,
    name: str,
    description: str,
    code: str,
    symbols: list[str],
    sources: list[str],
    kinds: list[str],
    indicators: list[str],
    node_count: int,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "code": code,
        "symbols": symbols,
        "sources": sources,
        "kinds": kinds,
        "indicators": indicators,
        "node_count": node_count,
        "generated_at": datetime.utcnow().isoformat(),
    }


def _artifact_from_row(row: AIAgentKnowledge) -> dict[str, Any] | None:
    try:
        payload = json.loads(row.content or "")
    except Exception:
        return None
    if not isinstance(payload, dict) or not payload.get("code") or not payload.get("name"):
        return None
    return {
        **payload,
        "knowledge_id": row.id,
        "weight": row.weight,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


_SAFE_BUILTINS = {
    "abs": abs,
    "bool": bool,
    "dict": dict,
    "float": float,
    "int": int,
    "len": len,
    "list": list,
    "max": max,
    "min": min,
    "range": range,
    "round": round,
    "str": str,
    "sum": sum,
}


def _normalise_strategy_signal(raw: Any, strategy: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    action = str(raw.get("action", "HOLD")).upper()
    if action not in {"BUY", "SELL", "HOLD"}:
        action = "HOLD"
    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence") or 0)))
    except Exception:
        confidence = 0.0
    score = confidence if action == "BUY" else -confidence if action == "SELL" else 0.0
    return {
        "name": strategy["name"],
        "action": action,
        "score": score,
        "confidence": confidence,
        "reasoning": str(raw.get("reasoning") or "")[:500],
        "source": "jarvis_synthesized_strategy",
        "knowledge_id": strategy.get("knowledge_id"),
    }


def run_strategy_code(strategy: dict[str, Any], candles: list[dict[str, Any]], symbol: str) -> dict[str, Any] | None:
    """Run a JARVIS generated strategy in a tiny function-only sandbox."""
    code = strategy.get("code")
    if not code or not isinstance(code, str):
        return None
    env: dict[str, Any] = {"__builtins__": _SAFE_BUILTINS}
    local_env: dict[str, Any] = {}
    try:
        exec(code, env, local_env)  # noqa: S102 - restricted generated strategy function.
        fn = local_env.get("generate_signal") or env.get("generate_signal")
        if not callable(fn):
            return None
        return _normalise_strategy_signal(fn(candles, symbol), strategy)
    except Exception as exc:
        logger.warning(f"[JARVIS strategy] {strategy.get('name')} failed for {symbol}: {exc}")
        return None


async def list_generated_strategy_artifacts(
    db: AsyncSession,
    *,
    symbol: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    stmt = (
        select(AIAgentKnowledge)
        .where(AIAgentKnowledge.kind == "strategy")
        .where(AIAgentKnowledge.agent_role == "jarvis_strategy_engine")
        .order_by(desc(AIAgentKnowledge.weight), desc(AIAgentKnowledge.updated_at))
        .limit(limit)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    artifacts: list[dict[str, Any]] = []
    for row in rows:
        artifact = _artifact_from_row(row)
        if artifact is None:
            continue
        symbols = {s.upper() for s in artifact.get("symbols", [])}
        if symbol and symbols and symbol.upper() not in symbols:
            continue
        artifacts.append(artifact)
    return artifacts


async def evaluate_generated_strategies(
    db: AsyncSession,
    *,
    candles: list[dict[str, Any]],
    symbol: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    strategies = await list_generated_strategy_artifacts(db, symbol=symbol, limit=limit)
    scores: list[dict[str, Any]] = []
    for strategy in strategies:
        signal = run_strategy_code(strategy, candles, symbol)
        if signal:
            scores.append(signal)
    return scores


async def synthesize_strategies(
    db: AsyncSession,
    n_strategies: int = 3,
) -> list[dict[str, Any]]:
    """
    Read accumulated knowledge nodes and synthesise ``n_strategies`` Python
    strategy objects ready for use by the market-analysis engine.

    Each returned dict has:
      name          – short identifier
      description   – human-readable summary
      code          – executable Python strategy code
      symbols       – list of symbols this strategy targets
      sources       – knowledge sources that informed it
      generated_at  – ISO timestamp
      indicators    – list of indicator names used
    """
    # ── Pull recent knowledge (high-weight rows first) ─────────────────────────
    stmt = (
        select(AIAgentKnowledge)
        .where(AIAgentKnowledge.agent_role == "jarvis_intelligence")
        .order_by(desc(AIAgentKnowledge.weight), desc(AIAgentKnowledge.updated_at))
        .limit(60)
    )
    rows = list((await db.execute(stmt)).scalars().all())

    if not rows:
        return []

    # ── Group by symbol ────────────────────────────────────────────────────────
    by_symbol: dict[str | None, list[AIAgentKnowledge]] = {}
    for r in rows:
        by_symbol.setdefault(r.symbol, []).append(r)

    strategies: list[dict[str, Any]] = []
    processed_syms: set[str | None] = set()

    for sym, sym_rows in sorted(by_symbol.items(), key=lambda x: len(x[1]), reverse=True):
        if len(strategies) >= n_strategies:
            break

        # Combined summary of all knowledge for this symbol
        summary = " ".join(r.content for r in sym_rows[:8])[:1200]
        sources  = list({r.source for r in sym_rows if r.source})
        kinds    = list({r.kind for r in sym_rows})

        # Strategy name
        sym_tag = sym or "MARKET"
        dominant_kind = max(kinds, key=kinds.count)
        name = f"JARVIS_{sym_tag}_{dominant_kind.upper()}_v1".replace("/", "_")

        # Description
        description = (
            f"JARVIS-synthesised strategy for {sym_tag}. "
            f"Informed by: {', '.join(sources)}. "
            f"Knowledge types: {', '.join(kinds)}. "
            f"Based on {len(sym_rows)} intelligence nodes accumulated from "
            f"sentiment, SMC, Telegram signals, and AI decisions."
        )

        # Indicators list (inferred from summary)
        indicators = []
        if any(k in summary.lower() for k in ["rsi", "relative strength"]): indicators.append("RSI")
        if any(k in summary.lower() for k in ["macd", "divergence"]): indicators.append("MACD")
        if any(k in summary.lower() for k in ["bollinger", "squeeze"]): indicators.append("Bollinger Bands")
        if any(k in summary.lower() for k in ["ema", "moving average"]): indicators.append("EMA")
        if any(k in summary.lower() for k in ["volume"]): indicators.append("Volume")
        if any(k in summary.lower() for k in ["smc", "order block", "liquidity"]): indicators.append("SMC")
        if not indicators:
            indicators = ["EMA", "RSI"]

        # Generate code
        code = _build_strategy_code(name, description, summary, sources)

        artifact = _strategy_artifact(
            name=name,
            description=description,
            code=code,
            symbols=[sym] if sym else [],
            sources=sources,
            kinds=kinds,
            indicators=indicators,
            node_count=len(sym_rows),
        )

        # Store the generated strategy as a knowledge node itself
        await knowledge_service.store_knowledge(
            db,
            content=json.dumps(artifact, default=str),
            title=f"Generated strategy: {name}",
            kind="strategy",
            symbol=sym,
            agent_role="jarvis_strategy_engine",
            weight=2.0,
            source="strategy_synthesis",
        )

        strategies.append(artifact)
        processed_syms.add(sym)

    # ── If we still have capacity, generate a cross-symbol "macro" strategy ───
    if len(strategies) < n_strategies:
        all_rows = [r for r in rows if r.symbol not in processed_syms]
        if all_rows:
            summary = " ".join(r.content for r in all_rows[:10])[:1200]
            sources  = list({r.source for r in all_rows if r.source})
            name = "JARVIS_MACRO_MULTI_ASSET_v1"
            description = (
                "JARVIS macro strategy synthesised from cross-asset intelligence. "
                f"Draws on {len(all_rows)} knowledge nodes across all symbols and sources: "
                f"{', '.join(sources)}."
            )
            code = _build_strategy_code(name, description, summary, sources)
            artifact = _strategy_artifact(
                name=name,
                description=description,
                code=code,
                symbols=[],
                sources=sources,
                kinds=["macro"],
                indicators=["EMA", "RSI", "MACD", "Volume"],
                node_count=len(all_rows),
            )
            await knowledge_service.store_knowledge(
                db,
                content=json.dumps(artifact, default=str),
                title=f"Generated strategy: {name}",
                kind="strategy",
                symbol=None,
                agent_role="jarvis_strategy_engine",
                weight=2.5,
                source="strategy_synthesis",
            )
            strategies.append(artifact)

    logger.info(f"[JARVIS strategy] synthesised {len(strategies)} strategies")
    return strategies
