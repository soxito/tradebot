"""
AI Market Analyst — Agent Runtime

Orchestrates the full analysis cycle:
1. Load agent profile + user settings
2. Fetch market data → compute indicators
3. Build system + user prompts
4. Call OpenAI → parse structured output
5. Validate via risk policy
6. Store decision as immutable audit record
7. Optionally place limit order via MT5 gateway
"""
import json
from datetime import datetime, timezone
from typing import Dict, Optional
from loguru import logger
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from plugins.AiMarketAnalyst.backend.models import (
    AIAgent, AITradeSettings, AITradeDecision, AIMarketSnapshot,
    DecisionType, DecisionDirection, DecisionStatus,
)
from plugins.AiMarketAnalyst.backend.schemas import AIModelOutput
from plugins.AiMarketAnalyst.backend.services.llm_gateway import call_model
from plugins.AiMarketAnalyst.backend.services import ai_router
from plugins.AiMarketAnalyst.backend.services.indicator_engine import compute_indicators
from plugins.AiMarketAnalyst.backend.services.market_data import fetch_ohlcv, cache_snapshot
from plugins.AiMarketAnalyst.backend.services.risk_policy import validate_decision
from plugins.AiMarketAnalyst.backend.services.mt5_order_gateway import place_limit_order
from plugins.AiMarketAnalyst.backend.services.overlay_service import build_overlay
from plugins.AiMarketAnalyst.backend.config import ai_analyst_config


_DEFAULT_SYSTEM = """You are an expert forex/crypto market analyst.
Analyze the provided market data and indicators. Return a JSON object matching this EXACT schema:
{
  "action": "analyze" | "propose_limit",
  "direction": "buy" | "sell" | "none",
  "confidence": 0-100,
  "levels": {"entry": float|null, "sl": float|null, "tp": float|null},
  "timeframe": "string",
  "signals": [{"name": "string", "value": "string", "weight": 0-1}],
  "rationale": "string",
  "invalidation": "string",
  "notes": ["string"]
}
Rules:
- If unsure, set direction="none" and confidence below 40.
- Always include a stop-loss when proposing a trade.
- Never fabricate indicator values.
- Be concise in rationale.
"""


async def run_analysis(
    *,
    db: AsyncSession,
    symbol: str,
    timeframe: str = "H1",
    action: str = "analyze",
    user_id: str = "default",
) -> Dict:
    """Analyze symbol and return decision + overlay."""

    normalized_user_id = _normalize_user_id(user_id)

    # 1. Load settings + agent
    settings = await _get_settings(db, normalized_user_id)
    agent = await _get_agent(db, settings.selected_agent_id)

    if agent and not agent.is_enabled:
        return {"error": "Selected agent is disabled", "decision": None, "overlay": None}

    if agent and agent.instruments_json:
        allowed_symbols = {s.upper() for s in agent.instruments_json}
        if symbol.upper() not in allowed_symbols:
            return {
                "error": f"Symbol {symbol} is not allowed for this agent",
                "decision": None,
                "overlay": None,
            }

    if agent and agent.timeframes_json:
        allowed_timeframes = {t.upper() for t in agent.timeframes_json}
        if timeframe.upper() not in allowed_timeframes:
            return {
                "error": f"Timeframe {timeframe} is not allowed for this agent",
                "decision": None,
                "overlay": None,
            }

    allowed_actions = agent.allowed_actions if agent else "analyze"
    if action == "propose_limit" and allowed_actions == "analyze":
        return {
            "error": "Agent is restricted to analyze-only",
            "decision": None,
            "overlay": None,
        }

    # 2. Fetch market data
    tf_map = {"M1": "1m", "M5": "5m", "M15": "15m", "M30": "30m",
              "H1": "1h", "H4": "4h", "D1": "1d", "W1": "1w"}
    ccxt_tf = tf_map.get(timeframe, "1h")
    candles = await fetch_ohlcv(symbol, timeframe=ccxt_tf, limit=100)

    if not candles:
        return {"error": "No market data available", "decision": None, "overlay": None}

    # 3. Compute indicators
    indicators_list = (
        agent.indicators_json if agent and agent.indicators_json
        else ai_analyst_config.default_indicators
    )
    indicators = compute_indicators(candles, indicators_list)

    # 4. Build prompts
    system_prompt = (agent.system_prompt if agent and agent.system_prompt else _DEFAULT_SYSTEM)
    user_prompt = json.dumps({
        "symbol": symbol,
        "timeframe": timeframe,
        "action": action,
        "candles_count": len(candles),
        "latest_candle": candles[-1] if candles else None,
        "indicators": indicators,
    }, default=str)

    # 5. Call model — prefer the user's connected AI providers (DB-configured,
    #    usage-capped to protect free tiers); fall back to the env gateway.
    model_name = agent.model if agent else ai_analyst_config.default_model
    reasoning = agent.reasoning_effort if agent else ai_analyst_config.default_reasoning_effort
    max_tokens = agent.max_output_tokens if agent and agent.max_output_tokens else ai_analyst_config.default_max_tokens

    result: dict
    used_connected_provider = False
    try:
        if await ai_router.has_enabled_providers(db):
            ai_res = await ai_router.agent_chat(
                db,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=max_tokens,
            )
            used_connected_provider = True
            if ai_res.get("ok") and ai_res.get("content") is not None:
                result = {"content": ai_res["content"], "error": None}
            else:
                result = {"error": ai_res.get("error") or "Connected provider failed", "content": None}
        else:
            result = await call_model(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=model_name,
                reasoning_effort=reasoning,
                max_tokens=max_tokens,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[AgentRuntime] Connected provider call failed, falling back to gateway: {exc}")
        used_connected_provider = False
        result = await call_model(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model_name,
            reasoning_effort=reasoning,
            max_tokens=max_tokens,
        )

    # If connected providers returned an error, fall back to the env gateway once
    if used_connected_provider and result.get("error"):
        logger.info(f"[AgentRuntime] Connected provider error ({result['error']}); trying env gateway")
        result = await call_model(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model_name,
            reasoning_effort=reasoning,
            max_tokens=max_tokens,
        )

    if result.get("error"):
        return {"error": result["error"], "decision": None, "overlay": None}

    content = result["content"]

    # 6. Parse & validate model output
    try:
        model_out = AIModelOutput(**content)
    except Exception as exc:
        logger.warning(f"[AgentRuntime] Model output validation failed: {exc}")
        return {"error": f"Invalid model output: {exc}", "decision": None, "overlay": None}

    # 7. Risk policy check
    direction = model_out.direction
    entry = model_out.levels.get("entry")
    sl = model_out.levels.get("sl")
    tp = model_out.levels.get("tp")

    lot_size = settings.lot_size if settings else None
    risk_check = validate_decision(
        action=action,
        allowed_actions=allowed_actions,
        trade_mode=settings.mode if settings else "both",
        trading_hours=settings.trading_hours_json if settings else None,
        direction=direction,
        entry=entry,
        sl=sl,
        tp=tp,
        lot_size=lot_size,
        lot_min=settings.lot_min or ai_analyst_config.min_lot_size,
        lot_max=settings.lot_max or ai_analyst_config.max_lot_size,
        paper_mode=settings.paper_mode if settings else True,
        auto_place=settings.auto_place if settings else False,
    )

    if model_out.action != action:
        risk_check["blocked_reasons"].append(
            f"Model action {model_out.action} does not match request {action}"
        )
        risk_check["allowed"] = False

    status = DecisionStatus.DRAFTED if risk_check["allowed"] else DecisionStatus.BLOCKED

    # 8. Store market snapshot
    snapshot = AIMarketSnapshot(
        symbol=symbol,
        timeframe=timeframe,
        indicators_json=indicators,
        ohlcv_json=candles[-5:] if len(candles) >= 5 else candles,
    )
    db.add(snapshot)
    await db.flush()

    # 9. Store decision (immutable audit record)
    decision = AITradeDecision(
        user_id=normalized_user_id,
        ai_agent_id=agent.id if agent else None,
        agent_version=agent.version if agent else 0,
        symbol=symbol,
        timeframe=timeframe,
        decision_type=DecisionType.PROPOSE if action == "propose_limit" else DecisionType.ANALYZE,
        direction=DecisionDirection.BUY if direction == "buy" else (
            DecisionDirection.SELL if direction == "sell" else DecisionDirection.NONE
        ),
        entry_price=entry,
        sl_price=sl,
        tp_price=tp,
        confidence=model_out.confidence,
        rationale=model_out.rationale,
        invalidation=model_out.invalidation,
        signals_json=[s.model_dump() for s in model_out.signals],
        status=status,
        blocked_reasons_json=risk_check["blocked_reasons"] if not risk_check["allowed"] else None,
        structured_json=model_out.model_dump(),
        request_payload_json={"symbol": symbol, "timeframe": timeframe, "action": action},
        response_payload_json={
            "provider": result.get("provider"),
            "model": result.get("model"),
            "usage": result.get("usage"),
            "response_id": result.get("response_id"),
        },
    )
    db.add(decision)
    await db.commit()
    await db.refresh(decision)

    # 10. Build overlay
    overlay = build_overlay(
        direction=direction,
        entry_price=entry,
        sl_price=sl,
        tp_price=tp,
        confidence=model_out.confidence,
        status=status.value,
    )

    # 11. Cache snapshot for overlay reuse
    cache_snapshot(symbol, timeframe, indicators)

    return {
        "decision": {
            "id": decision.id,
            "symbol": symbol,
            "timeframe": timeframe,
            "direction": direction,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "confidence": model_out.confidence,
            "rationale": model_out.rationale,
            "invalidation": model_out.invalidation,
            "signals": [s.model_dump() for s in model_out.signals],
            "status": status.value,
            "blocked_reasons": risk_check["blocked_reasons"],
            "warnings": risk_check["warnings"],
            "provider": result.get("provider"),
            "model": result.get("model"),
            "usage": result.get("usage"),
        },
        "overlay": overlay.model_dump(),
        "error": None,
    }


async def place_decision(
    *,
    db: AsyncSession,
    decision_id: int,
    user_id: str = "default",
) -> Dict:
    """Place a previously proposed limit order via MT5."""
    normalized_user_id = _normalize_user_id(user_id)
    decision = await db.get(AITradeDecision, decision_id)
    if not decision:
        return {"error": "Decision not found"}
    if decision.status != DecisionStatus.DRAFTED:
        return {"error": f"Decision is {decision.status.value}, not drafted"}

    settings = await _get_settings(db, normalized_user_id)
    agent = await _get_agent(db, decision.ai_agent_id)

    allowed_actions = agent.allowed_actions if agent else "analyze"
    risk_check = validate_decision(
        action="place",
        allowed_actions=allowed_actions,
        trade_mode=settings.mode if settings else "both",
        trading_hours=settings.trading_hours_json if settings else None,
        direction=decision.direction.value if decision.direction else "none",
        entry=decision.entry_price,
        sl=decision.sl_price,
        tp=decision.tp_price,
        lot_size=settings.lot_size if settings else None,
        lot_min=settings.lot_min or ai_analyst_config.min_lot_size,
        lot_max=settings.lot_max or ai_analyst_config.max_lot_size,
        paper_mode=settings.paper_mode if settings else True,
        auto_place=settings.auto_place if settings else False,
    )

    if not risk_check["allowed"]:
        decision.status = DecisionStatus.BLOCKED
        decision.blocked_reasons_json = risk_check["blocked_reasons"]
        await db.commit()
        return {"error": "; ".join(risk_check["blocked_reasons"]) or "Blocked by risk policy"}

    if settings.paper_mode:
        decision.status = DecisionStatus.MT5_ACCEPTED
        decision.mt5_order_id = "PAPER"
        await db.commit()
        return {"success": True, "paper": True, "mt5_ticket": "PAPER"}

    if not settings.mt5_account_id:
        return {"error": "No MT5 account configured in settings"}

    result = await place_limit_order(
        mt5_account_id=settings.mt5_account_id,
        symbol=decision.symbol,
        direction=decision.direction.value,
        lot_size=settings.lot_size or 0.01,
        entry_price=decision.entry_price,
        sl_price=decision.sl_price,
        tp_price=decision.tp_price,
    )

    if result["success"]:
        decision.status = DecisionStatus.MT5_ACCEPTED
        decision.mt5_order_id = result["mt5_ticket"]
    else:
        decision.status = DecisionStatus.MT5_REJECTED
        decision.blocked_reasons_json = [result["error"]]

    await db.commit()
    return result


# ── Helpers ────────────────────────────────────────────────

def _normalize_user_id(user_id: str) -> int:
    try:
        return int(user_id)
    except (TypeError, ValueError):
        return 0


async def _get_settings(db: AsyncSession, user_id: int) -> AITradeSettings:
    """Get or create default settings."""
    result = await db.execute(
        select(AITradeSettings).where(AITradeSettings.user_id == user_id)
    )
    settings = result.scalar_one_or_none()
    if not settings:
        settings = AITradeSettings(user_id=user_id)
        db.add(settings)
        await db.flush()
    return settings


async def _get_agent(db: AsyncSession, agent_id: Optional[int]) -> Optional[AIAgent]:
    """Load selected agent, or the first enabled one."""
    if agent_id:
        return await db.get(AIAgent, agent_id)
    result = await db.execute(
        select(AIAgent).where(AIAgent.is_enabled == True).limit(1)
    )
    return result.scalar_one_or_none()
