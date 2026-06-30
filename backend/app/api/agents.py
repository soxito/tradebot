"""
AI Agent API Routes — CRUD for agents + orchestration triggers.
Optional: Guarded by ENABLE_AI_AGENTS setting. CRUD always works,
analysis endpoints require the toggle to be ON.
"""
import json
import os
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func
from loguru import logger

from app.core.database import get_db
from app.core.config import settings
from app.models.database import Agent, AgentDecision
from app.agents.specialists import DEFAULT_AGENTS
from app.agents.orchestrator import AgentOrchestrator
from app.agents.memory import get_learning_stats
from app.core.timezone import now_sast

router = APIRouter(prefix="/agents", tags=["agents"])


# ─── Pydantic Schemas ───────────────────────────────────────


class AgentCreate(BaseModel):
    name: str
    role: str
    description: Optional[str] = None
    system_prompt: str
    model: str = "fable-5-high"
    temperature: float = 0.3
    max_tokens: int = 2000
    is_active: bool = True
    pairs: Optional[str] = None


class AgentUpdate(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    model: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    is_active: Optional[bool] = None
    pairs: Optional[str] = None


class AnalyzeRequest(BaseModel):
    symbol: str
    timeframe: str = "1h"


class AnalyzeMultipleRequest(BaseModel):
    symbols: List[str]
    timeframe: str = "1h"


# ─── Agent CRUD ──────────────────────────────────────────────


@router.get("")
async def list_agents(db: AsyncSession = Depends(get_db)):
    """List all agents."""
    result = await db.execute(select(Agent).order_by(Agent.id))
    agents = result.scalars().all()
    return {
        "agents": [
            {
                "id": a.id,
                "name": a.name,
                "role": a.role,
                "description": a.description,
                "system_prompt": a.system_prompt,
                "model": a.model,
                "temperature": a.temperature,
                "max_tokens": a.max_tokens,
                "is_active": a.is_active,
                "pairs": a.pairs,
                "created_at": str(a.created_at) if a.created_at else None,
                "updated_at": str(a.updated_at) if a.updated_at else None,
            }
            for a in agents
        ]
    }


@router.post("")
async def create_agent(data: AgentCreate, db: AsyncSession = Depends(get_db)):
    """Create a new agent."""
    agent = Agent(
        name=data.name,
        role=data.role,
        description=data.description,
        system_prompt=data.system_prompt,
        model=data.model,
        temperature=data.temperature,
        max_tokens=data.max_tokens,
        is_active=data.is_active,
        pairs=data.pairs,
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return {"agent": {"id": agent.id, "name": agent.name, "role": agent.role}}


@router.put("/{agent_id}")
async def update_agent(agent_id: int, data: AgentUpdate, db: AsyncSession = Depends(get_db)):
    """Update an existing agent."""
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = result.scalars().first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(agent, field, value)

    await db.commit()
    return {"agent": {"id": agent.id, "name": agent.name, "role": agent.role, "is_active": agent.is_active}}


@router.delete("/{agent_id}")
async def delete_agent(agent_id: int, db: AsyncSession = Depends(get_db)):
    """Delete an agent."""
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = result.scalars().first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    await db.delete(agent)
    await db.commit()
    return {"deleted": agent_id}


@router.post("/{agent_id}/toggle")
async def toggle_agent(agent_id: int, db: AsyncSession = Depends(get_db)):
    """Toggle agent active/inactive."""
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = result.scalars().first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    agent.is_active = not agent.is_active
    await db.commit()
    return {"id": agent.id, "is_active": agent.is_active}


@router.post("/seed-defaults")
async def seed_default_agents(db: AsyncSession = Depends(get_db)):
    """Create default agents, adding any missing roles."""
    result = await db.execute(select(Agent))
    existing = result.scalars().all()
    existing_roles = {a.role for a in existing}

    created = []
    for spec in DEFAULT_AGENTS:
        if spec["role"] not in existing_roles:
            agent = Agent(**spec)
            db.add(agent)
            created.append(spec["name"])

    if created:
        await db.commit()

    return {
        "message": f"Created {len(created)} agent(s)" if created else f"All {len(existing)} agents already exist",
        "agents": created,
        "existing": len(existing),
    }


# ─── Status & Toggle ─────────────────────────────────────────


@router.get("/status")
async def agent_status(db: AsyncSession = Depends(get_db)):
    """Check agent system status including AI toggle, circuit breaker, and learning stats."""
    from app.agents.base import get_ai_status
    from app.agents.custom_agents import get_custom_agent_status
    api_key = os.getenv("OPENAI_API_KEY", "")
    result = await db.execute(select(Agent))
    agents = result.scalars().all()
    active = [a for a in agents if a.is_active]

    learning = await get_learning_stats(db)
    ai_status = get_ai_status()

    return {
        "ai_enabled": settings.ENABLE_AI_AGENTS,
        "openai_configured": bool(api_key),
        "ai_available": ai_status["available"],
        "circuit_breaker": {
            "open": ai_status["circuit_breaker_open"],
            "reason": ai_status.get("circuit_breaker_reason", ""),
            "remaining_s": ai_status.get("circuit_breaker_remaining_s", 0),
        },
        "custom_agents": get_custom_agent_status(),
        "model": os.getenv("OPENAI_MODEL", "fable-5-high"),
        "total_agents": len(agents),
        "active_agents": len(active),
        "roles": list(set(a.role for a in active)),
        "memory_lookback": settings.AI_MEMORY_LOOKBACK,
        "min_memory_for_local": settings.AI_MIN_MEMORY_FOR_LOCAL,
        "local_confidence_threshold": settings.AI_LOCAL_CONFIDENCE_THRESHOLD,
        "learning": learning,
    }


@router.post("/toggle")
async def toggle_ai_agents():
    """Toggle ENABLE_AI_AGENTS on/off at runtime (does not persist to .env)."""
    settings.ENABLE_AI_AGENTS = not settings.ENABLE_AI_AGENTS
    logger.info(f"[Agents] AI agents {'ENABLED' if settings.ENABLE_AI_AGENTS else 'DISABLED'}")
    return {"ai_enabled": settings.ENABLE_AI_AGENTS}


@router.post("/custom/toggle")
async def toggle_custom_agents():
    """Toggle custom rule-based agents on/off. When on, they replace AI when unavailable."""
    from app.agents.custom_agents import are_custom_agents_enabled, set_custom_agents_enabled
    new_state = not are_custom_agents_enabled()
    set_custom_agents_enabled(new_state)
    logger.info(f"[Agents] Custom agents {'ENABLED' if new_state else 'DISABLED'}")
    return {"custom_agents_enabled": new_state}


@router.get("/custom/status")
async def custom_agent_status(db: AsyncSession = Depends(get_db)):
    """Get detailed status of custom rule-based agents including learning stats."""
    from app.agents.custom_agents import get_custom_agent_status
    status = get_custom_agent_status()

    # Get learning stats per role
    roles = ["market_analyst", "sentiment_analyst", "signal_generator", "risk_manager", "trade_executor", "position_reviewer"]
    role_stats = {}
    for role in roles:
        role_stats[role] = await get_learning_stats(db, role=role)

    # Count custom agent decisions
    custom_count = (await db.execute(
        select(AgentDecision).where(
            AgentDecision.ai_called == False,
            AgentDecision.agent_name.like("Custom%"),
        ).with_only_columns(func.count(AgentDecision.id))
    )).scalar() or 0

    return {
        **status,
        "total_custom_decisions": custom_count,
        "role_learning": role_stats,
        "agents": [
            {"name": "Custom Market Analyst", "role": "market_analyst", "type": "Technical Analysis (RSI, MACD, EMA, BB, ADX)"},
            {"name": "Custom Sentiment Analyst", "role": "sentiment_analyst", "type": "DB Sentiment + CMC Community"},
            {"name": "Custom Signal Generator", "role": "signal_generator", "type": "Combined TA + Sentiment + Learning"},
            {"name": "Custom Risk Manager", "role": "risk_manager", "type": "Position Limits + Exposure + Streak Detection"},
            {"name": "Custom Trade Executor", "role": "trade_executor", "type": "Order Sizing + Spread Detection"},
            {"name": "Custom Position Reviewer", "role": "position_reviewer", "type": "Reversal Detection (RSI, MACD, EMA)"},
        ],
    }


@router.post("/custom/test")
async def test_custom_agents(
    symbol: str = Query("BTC/USDT"),
    db: AsyncSession = Depends(get_db),
):
    """Run the custom agent pipeline on a symbol for testing (no trade executed)."""
    from app.agents.custom_agents import custom_validate_trade
    context = await AgentOrchestrator._gather_context(symbol)
    signal_data = {"action": "buy", "price": context.get("current_price", 0), "confidence": 0.80}
    position_context = {"open_positions": 0, "max_positions": 3, "available_balance": 100, "total_exposure": 0, "max_exposure": 5000, "is_dca": False}
    result = await custom_validate_trade(db, symbol, signal_data, position_context, context)
    return result


# ─── Orchestration Endpoints ────────────────────────────────


@router.post("/analyze")
async def analyze_symbol_endpoint(data: AnalyzeRequest, db: AsyncSession = Depends(get_db)):
    """Run the full agent pipeline for a single symbol.
    Requires ENABLE_AI_AGENTS=true. Returns graceful error if disabled."""
    if not settings.ENABLE_AI_AGENTS:
        return {
            "error": "AI agents are disabled. Toggle them on in the Agents page.",
            "ai_enabled": False,
            "symbol": data.symbol,
        }

    try:
        result = await AgentOrchestrator.analyze_symbol(
            db=db,
            symbol=data.symbol,
            timeframe=data.timeframe,
            trigger="manual",
        )
        return result
    except Exception as e:
        logger.error(f"[Agents] Analysis failed for {data.symbol}: {e}")
        return {
            "error": f"Analysis failed: {str(e)}",
            "symbol": data.symbol,
            "ai_enabled": True,
        }


@router.post("/analyze-multiple")
async def analyze_multiple(data: AnalyzeMultipleRequest, db: AsyncSession = Depends(get_db)):
    """Run the agent pipeline for multiple symbols."""
    if not settings.ENABLE_AI_AGENTS:
        return {
            "error": "AI agents are disabled.",
            "ai_enabled": False,
        }

    try:
        results = await AgentOrchestrator.analyze_multiple(
            db=db,
            symbols=data.symbols,
            timeframe=data.timeframe,
        )
        return {"results": results}
    except Exception as e:
        logger.error(f"[Agents] Multi-analysis failed: {e}")
        return {"error": str(e)}


# ─── Decision History & Outcomes ─────────────────────────────


@router.get("/decisions")
async def list_decisions(
    limit: int = Query(50, le=200),
    symbol: Optional[str] = None,
    session_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """List recent agent decisions with outcome data."""
    query = select(AgentDecision).order_by(desc(AgentDecision.created_at)).limit(limit)
    if symbol:
        query = query.where(AgentDecision.symbol == symbol)
    if session_id:
        query = query.where(AgentDecision.session_id == session_id)

    result = await db.execute(query)
    rows = result.scalars().all()

    return {
        "decisions": [
            {
                "id": d.id,
                "agent_id": d.agent_id,
                "agent_name": d.agent_name,
                "agent_role": d.agent_role,
                "symbol": d.symbol,
                "action": d.action,
                "confidence": d.confidence,
                "reasoning": d.reasoning,
                "signal_id": d.signal_id,
                "session_id": d.session_id,
                "outcome": d.outcome,
                "outcome_pnl": d.outcome_pnl,
                "ai_called": d.ai_called,
                "memory_context_used": d.memory_context_used,
                "created_at": str(d.created_at) if d.created_at else None,
            }
            for d in rows
        ]
    }


@router.get("/decisions/stats")
async def get_decision_stats(db: AsyncSession = Depends(get_db)):
    """
    Aggregate decision statistics broken down by agent_role and action.
    Returned as a summary for the Insights → AI Decisions tab.
    """
    from sqlalchemy import case, Float

    result = await db.execute(select(AgentDecision))
    decisions = result.scalars().all()

    by_role: dict = {}
    by_action: dict = {}
    by_symbol: dict = {}

    for d in decisions:
        role = d.agent_role or "unknown"
        action = (d.action or "hold").lower()
        symbol = d.symbol or "unknown"

        by_role.setdefault(role, {"count": 0, "ai_calls": 0, "local": 0, "wins": 0})
        by_role[role]["count"] += 1
        if d.ai_called:
            by_role[role]["ai_calls"] += 1
        else:
            by_role[role]["local"] += 1
        if d.outcome == "win":
            by_role[role]["wins"] += 1

        by_action[action] = by_action.get(action, 0) + 1

        by_symbol.setdefault(symbol, {"count": 0, "buy": 0, "sell": 0, "hold": 0})
        by_symbol[symbol]["count"] += 1
        if action in ("buy", "long"):
            by_symbol[symbol]["buy"] += 1
        elif action in ("sell", "short"):
            by_symbol[symbol]["sell"] += 1
        else:
            by_symbol[symbol]["hold"] += 1

    # Top 10 symbols by decision count
    top_symbols = sorted(by_symbol.items(), key=lambda x: -x[1]["count"])[:10]

    return {
        "total": len(decisions),
        "by_role": by_role,
        "by_action": by_action,
        "top_symbols": [{"symbol": s, **v} for s, v in top_symbols],
    }


class RecordOutcomeRequest(BaseModel):
    outcome: str  # win, loss, break_even
    pnl: Optional[float] = None


@router.patch("/decisions/{decision_id}/outcome")
async def record_decision_outcome(
    decision_id: int,
    data: RecordOutcomeRequest,
    db: AsyncSession = Depends(get_db),
):
    """Record the outcome of an agent decision for learning."""
    if data.outcome not in ("win", "loss", "break_even"):
        raise HTTPException(status_code=400, detail="outcome must be: win, loss, break_even")

    result = await db.execute(select(AgentDecision).where(AgentDecision.id == decision_id))
    decision = result.scalars().first()
    if not decision:
        raise HTTPException(status_code=404, detail="Decision not found")

    decision.outcome = data.outcome
    decision.outcome_pnl = data.pnl
    decision.outcome_recorded_at = now_sast()
    await db.commit()

    # ── Live vault capture: record outcome ────────────────────────────────────
    try:
        from plugins.ObsidianKnowledgePlugin.backend.services.vault_capture import vault_capture
        pnl_str = f" PnL={data.pnl:+.4f}" if data.pnl else ""
        vault_capture(
            action_type="decision-outcome",
            symbol=getattr(decision, "symbol", "") or "",
            summary=f"Outcome: {data.outcome.upper()}{pnl_str} | {getattr(decision,'agent_role','?')} → {getattr(decision,'action','?')}",
            detail=f"Decision ID {decision_id} recorded as {data.outcome}",
            tags=["outcome", data.outcome, getattr(decision, "symbol", "")],
            agent_role=getattr(decision, "agent_role", ""),
        )
    except Exception:
        pass

    return {
        "id": decision.id,
        "outcome": decision.outcome,
        "outcome_pnl": decision.outcome_pnl,
    }


@router.patch("/decisions/session/{session_id}/outcome")
async def record_session_outcome(
    session_id: str,
    data: RecordOutcomeRequest,
    db: AsyncSession = Depends(get_db),
):
    """Record outcome for ALL decisions in a session at once."""
    if data.outcome not in ("win", "loss", "break_even"):
        raise HTTPException(status_code=400, detail="outcome must be: win, loss, break_even")

    result = await db.execute(
        select(AgentDecision).where(AgentDecision.session_id == session_id)
    )
    rows = result.scalars().all()
    if not rows:
        raise HTTPException(status_code=404, detail="Session not found")

    for d in rows:
        d.outcome = data.outcome
        d.outcome_pnl = data.pnl
        d.outcome_recorded_at = now_sast()

    await db.commit()
    return {"session_id": session_id, "updated": len(rows), "outcome": data.outcome}


@router.get("/learning-stats")
async def learning_stats(
    symbol: Optional[str] = None,
    role: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Get learning statistics: accuracy, win rate, local vs API decisions."""
    stats = await get_learning_stats(db, symbol=symbol, role=role)
    return stats


@router.get("/decisions/{session_id}")
async def get_session_decisions(session_id: str, db: AsyncSession = Depends(get_db)):
    """Get all decisions from a specific orchestration session."""
    result = await db.execute(
        select(AgentDecision)
        .where(AgentDecision.session_id == session_id)
        .order_by(AgentDecision.id)
    )
    rows = result.scalars().all()
    if not rows:
        raise HTTPException(status_code=404, detail="Session not found")

    return {
        "session_id": session_id,
        "symbol": rows[0].symbol,
        "decisions": [
            {
                "id": d.id,
                "agent_name": d.agent_name,
                "agent_role": d.agent_role,
                "action": d.action,
                "confidence": d.confidence,
                "reasoning": d.reasoning,
                "outcome": d.outcome,
                "outcome_pnl": d.outcome_pnl,
                "ai_called": d.ai_called,
                "memory_context_used": d.memory_context_used,
                "created_at": str(d.created_at) if d.created_at else None,
            }
            for d in rows
        ],
    }


# ─── Position Monitor Endpoints ─────────────────────────────

@router.post("/position-monitor/start")
async def start_position_monitor_endpoint(interval: int = 900):
    """Start the position monitor loop. Default interval: 900s (15 minutes)."""
    if not settings.ENABLE_AI_AGENTS:
        raise HTTPException(status_code=400, detail="AI agents are disabled")
    from app.core.scheduler import start_position_monitor
    started = start_position_monitor(interval)
    if not started:
        return {"status": "already_running"}
    return {"status": "started", "interval_seconds": interval}


@router.post("/position-monitor/stop")
async def stop_position_monitor_endpoint():
    """Stop the position monitor loop."""
    from app.core.scheduler import stop_position_monitor
    stopped = stop_position_monitor()
    if not stopped:
        return {"status": "not_running"}
    return {"status": "stopped"}


@router.get("/position-monitor/status")
async def position_monitor_status():
    """Get position monitor loop status."""
    from app.core.scheduler import get_position_monitor_status
    return get_position_monitor_status()


@router.post("/position-monitor/run")
async def run_position_monitor(
    min_hold_hours: float = 2.0,
    db: AsyncSession = Depends(get_db),
):
    """Manually trigger a position review cycle."""
    if not settings.ENABLE_AI_AGENTS:
        raise HTTPException(status_code=400, detail="AI agents are disabled")
    result = await AgentOrchestrator.analyze_positions(db, min_hold_hours=min_hold_hours)
    return result


# ─── AI Strategy Generation & Chart Analysis ────────────────

STRATEGY_AI_PROMPT = """You are an expert crypto trading strategy designer.
You create optimal trading strategies based on market conditions, technical indicators, and risk tolerance.

You MUST respond with valid JSON in this exact format:
{
  "name": "Strategy Name",
  "description": "Brief strategy description",
  "timeframe": "1h",
  "indicators": [
    {"name": "rsi", "enabled": true, "params": {"period": 14, "overbought": 70, "oversold": 30}, "weight": 1.0},
    {"name": "macd", "enabled": true, "params": {"fast": 12, "slow": 26, "signal": 9}, "weight": 1.2}
  ],
  "buy_threshold": 0.25,
  "sell_threshold": -0.25,
  "stop_loss_pct": 2.0,
  "take_profit_pct": 4.0,
  "trade_type": "futures",
  "leverage": 5,
  "reasoning": "Explanation of why these settings work"
}

Available indicators (use these names exactly):
- rsi: RSI (params: period, overbought, oversold)
- macd: MACD (params: fast, slow, signal)
- bollinger: Bollinger Bands (params: period, mult)
- ema_cross: EMA Crossover (params: fast, slow)
- stoch_rsi: Stochastic RSI (params: period, overbought, oversold)
- adx: ADX (params: period, threshold)
- volume: Volume Surge (params: period, mult)

Strategy design principles:
1. Use 3-5 complementary indicators (avoid overlapping signals)
2. Weight indicators that suit the timeframe higher
3. Aggressive thresholds for scalping (short TFs), conservative for swing trading
4. Adjust SL/TP based on asset volatility
5. Higher leverage only for high-confidence setups with tight SL
6. Trending markets: prefer EMA cross, MACD, ADX
7. Ranging markets: prefer RSI, Bollinger Bands, Stochastic RSI
8. Risk:reward ratio must be at least 1:1.5
9. Include ALL 7 indicators in the response — set enabled=false for those not used"""

CHART_ANALYSIS_PROMPT = """You are an expert crypto chart analyst and trading advisor.
You analyze real-time chart data (OHLCV, technical indicators) and provide actionable trading insights.

You MUST respond with valid JSON:
{
  "market_structure": "trending_up" | "trending_down" | "ranging" | "breakout" | "breakdown",
  "confidence": 0.0-1.0,
  "key_levels": {
    "support": [price1, price2],
    "resistance": [price1, price2]
  },
  "recommended_action": "buy" | "sell" | "wait",
  "entry_zone": {"low": price, "high": price},
  "stop_loss": price,
  "take_profit_targets": [price1, price2, price3],
  "indicator_signals": {
    "rsi": "oversold|overbought|neutral",
    "macd": "bullish_cross|bearish_cross|neutral",
    "bollinger": "squeeze|expansion|upper_touch|lower_touch",
    "ema": "bullish|bearish|neutral",
    "volume": "increasing|decreasing|spike"
  },
  "strategy_suggestions": [
    "Specific strategy recommendation 1",
    "Specific strategy recommendation 2"
  ],
  "risk_assessment": "low|medium|high",
  "reasoning": "Detailed analysis of chart patterns and indicator confluence"
}

Focus on price action patterns, indicator confluence, volume confirmation, key S/R levels, and risk:reward quality."""


class GenerateStrategyRequest(BaseModel):
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"
    trade_type: str = "futures"
    risk_level: str = "medium"


class ImproveStrategyRequest(BaseModel):
    strategy: dict
    goals: Optional[str] = None


class AnalyzeChartRequest(BaseModel):
    symbol: str
    timeframe: str = "1h"


async def _call_openai_json(system_prompt: str, user_message: str) -> dict:
    """Call OpenAI with JSON response format."""
    from app.agents.base import _openai_available, _get_client, _circuit_is_open, _is_quota_or_auth_error, _trip_circuit

    if not _openai_available():
        raise HTTPException(status_code=503, detail="OpenAI not configured — AI features unavailable")

    if _circuit_is_open():
        raise HTTPException(status_code=503, detail="OpenAI quota exhausted — AI temporarily unavailable. Retrying automatically.")

    client = _get_client()
    model = os.getenv("OPENAI_MODEL", "o3")
    is_reasoning = model.startswith(("o1", "o3", "o4"))
    extra: dict = {}
    if is_reasoning:
        extra["max_completion_tokens"] = 4000
    else:
        extra["max_tokens"] = 4000
        extra["temperature"] = 0.3

    try:
        resp = await client.chat.completions.create(
            model=model,
            **extra,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        )
        raw = resp.choices[0].message.content or "{}"
        return json.loads(raw)
    except HTTPException:
        raise
    except Exception as e:
        if _is_quota_or_auth_error(e):
            _trip_circuit(str(e)[:200])
            raise HTTPException(status_code=503, detail="OpenAI quota exhausted — AI temporarily unavailable")
        raise


@router.post("/generate-strategy")
async def generate_strategy_endpoint(data: GenerateStrategyRequest):
    """Use AI to generate a new trading strategy."""
    if not settings.ENABLE_AI_AGENTS:
        raise HTTPException(status_code=400, detail="AI agents are disabled")
    try:
        user_msg = json.dumps({
            "task": "generate_new_strategy",
            "target_symbol": data.symbol,
            "timeframe": data.timeframe,
            "trade_type": data.trade_type,
            "risk_level": data.risk_level,
        })
        result = await _call_openai_json(STRATEGY_AI_PROMPT, user_msg)
        return {"strategy": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[AI Strategy] Generation failed: {e}")
        raise HTTPException(status_code=503, detail=f"AI generation failed: {str(e)[:200]}")


@router.post("/improve-strategy")
async def improve_strategy_endpoint(data: ImproveStrategyRequest):
    """Use AI to improve an existing trading strategy."""
    if not settings.ENABLE_AI_AGENTS:
        raise HTTPException(status_code=400, detail="AI agents are disabled")
    try:
        user_msg = json.dumps({
            "task": "improve_existing_strategy",
            "current_strategy": data.strategy,
            "improvement_goals": data.goals or "Optimize for better risk-adjusted returns",
        }, default=str)
        prompt = STRATEGY_AI_PROMPT + (
            "\n\nYou are improving an existing strategy. Analyze the current config, "
            "identify weaknesses, and return an improved version. Keep what works well "
            "and fix what doesn't. Explain your changes in the reasoning field."
        )
        result = await _call_openai_json(prompt, user_msg)
        return {"strategy": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[AI Strategy] Improvement failed: {e}")
        raise HTTPException(status_code=503, detail=f"AI improvement failed: {str(e)[:200]}")


@router.post("/analyze-chart")
async def analyze_chart_endpoint(data: AnalyzeChartRequest):
    """Use AI to analyze chart data and provide trading insights."""
    if not settings.ENABLE_AI_AGENTS:
        raise HTTPException(status_code=400, detail="AI agents are disabled")
    try:
        context = await AgentOrchestrator._gather_context(data.symbol, data.timeframe)
        user_msg = json.dumps({
            "symbol": data.symbol,
            "timeframe": data.timeframe,
            "market_data": context,
        }, default=str)
        result = await _call_openai_json(CHART_ANALYSIS_PROMPT, user_msg)
        result["symbol"] = data.symbol
        result["timeframe"] = data.timeframe
        return {"analysis": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[AI Chart] Analysis failed: {e}")
        raise HTTPException(status_code=503, detail=f"AI analysis failed: {str(e)[:200]}")
