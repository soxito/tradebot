"""
Agent Orchestrator — coordinates multiple AI agents for trade decisions.

Memory-aware pipeline:
  1. Gather market context (OHLCV, indicators, sentiment, positions)
  2. For each agent, check decision memory first:
     a. If enough historical data → try local decision (no OpenAI call)
     b. Otherwise call OpenAI with memory context injected
  3. Run the 4-phase pipeline (Market+Sentiment → Signal → Risk → Executor)
  4. Store every decision with ai_called flag for learning
  5. Gracefully skip if ENABLE_AI_AGENTS is False or OpenAI is unavailable
"""
import json
import uuid
import asyncio
from copy import deepcopy
from typing import Dict, Any, List, Optional
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.database import Agent, AgentDecision, Signal, SignalAction, SignalSource, SignalStatus, Trade, SimPosition, SimAccount
from app.agents.specialists import agent_from_db
from app.agents.memory import get_past_decisions, build_memory_prompt, try_local_decision
from app.signals.technical import analyze as technical_analyze
from app.exchanges.manager import exchange_manager, SupportedExchange
from app.exchanges.forex_provider import is_forex_symbol, fetch_ohlcv as forex_fetch_ohlcv
from app.services import market_data


def _is_quota_error_decision(decision: Dict[str, Any]) -> bool:
    """Check if a decision is a quota/billing error that should NOT be stored."""
    if not decision.get("error"):
        return False
    reasoning = str(decision.get("reasoning", "")).lower()
    _quota_phrases = ["insufficient_quota", "exceeded your current quota", "quota", "billing", "429", "rate limit"]
    return any(phrase in reasoning for phrase in _quota_phrases)
from app.core.timezone import now_sast
from app.core.config import settings


class AgentOrchestrator:
    """Coordinate multiple AI agents for collaborative trade decisions."""

    @staticmethod
    def _as_dict(value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                return {}
        return {}

    @staticmethod
    def _as_optional_bool(value: Any) -> Optional[bool]:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "y"}:
                return True
            if normalized in {"false", "0", "no", "n"}:
                return False
        return None

    @staticmethod
    def _normalize_trade_action(value: Any) -> str:
        if value is None:
            return ""
        normalized = str(value).strip().lower()
        action_map = {
            "long": "buy",
            "short": "sell",
            "bull": "buy",
            "bullish": "buy",
            "bear": "sell",
            "bearish": "sell",
            "neutral": "hold",
            "approve": "buy",
            "approved": "buy",
            "cancel": "reject",
            "rejected": "reject",
            "deny": "reject",
        }
        if normalized in action_map:
            return action_map[normalized]
        if normalized in {"buy", "sell", "hold", "wait", "reject"}:
            return normalized
        if "buy" in normalized or "long" in normalized:
            return "buy"
        if "sell" in normalized or "short" in normalized:
            return "sell"
        if "reject" in normalized or "cancel" in normalized or "deny" in normalized:
            return "reject"
        if "hold" in normalized or "wait" in normalized:
            return "hold"
        return normalized

    @staticmethod
    def _parse_trade_validation_decision(
        signal_action: Any,
        decision_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        approved_flag = decision_payload.get("approved")
        requested_action = AgentOrchestrator._normalize_trade_action(signal_action)
        decision_action = AgentOrchestrator._normalize_trade_action(
            decision_payload.get("action")
            or decision_payload.get("decision")
            or decision_payload.get("final_decision")
        )

        if isinstance(approved_flag, bool):
            approved = approved_flag
        elif decision_action == "reject":
            approved = False
        elif decision_action in {"hold", "wait"}:
            approved = False
        elif requested_action in {"buy", "sell"} and decision_action in {"buy", "sell"}:
            approved = decision_action == requested_action
        else:
            approved = True

        reasoning = (
            decision_payload.get("reasoning")
            or decision_payload.get("summary")
            or decision_payload.get("rationale")
            or "TradingAgents validation completed"
        )

        confidence = decision_payload.get("confidence")
        try:
            confidence_val = float(confidence) if confidence is not None else 0.0
        except (TypeError, ValueError):
            confidence_val = 0.0

        if not approved and requested_action in {"buy", "sell"} and decision_action in {"buy", "sell"} and decision_action != requested_action:
            reasoning = f"Direction mismatch: requested {requested_action}, validator suggested {decision_action}. {reasoning}"

        return {
            "approved": approved,
            "decision_action": decision_action or "reject",
            "reasoning": str(reasoning),
            "confidence": max(0.0, min(1.0, confidence_val)),
        }

    @staticmethod
    def _extract_learning_features(payload: Dict[str, Any]) -> Dict[str, Any]:
        container = AgentOrchestrator._as_dict(payload)
        pipeline_signal = AgentOrchestrator._as_dict(container.get("pipeline_signal"))
        signal_payload = AgentOrchestrator._as_dict(container.get("signal"))
        raw_data = AgentOrchestrator._as_dict(pipeline_signal.get("raw_data"))
        if not raw_data:
            raw_data = AgentOrchestrator._as_dict(signal_payload.get("raw_data"))

        volume_context = AgentOrchestrator._as_dict(container.get("volume_context"))
        if not volume_context:
            volume_context = AgentOrchestrator._as_dict(pipeline_signal.get("volume_context"))
        if not volume_context:
            volume_context = AgentOrchestrator._as_dict(signal_payload.get("volume_context"))
        if not volume_context:
            volume_context = AgentOrchestrator._as_dict(raw_data.get("volume_context"))

        btc_news_context = AgentOrchestrator._as_dict(container.get("btc_news_context"))
        if not btc_news_context:
            btc_news_context = AgentOrchestrator._as_dict(pipeline_signal.get("btc_news_context"))
        if not btc_news_context:
            btc_news_context = AgentOrchestrator._as_dict(signal_payload.get("btc_news_context"))
        if not btc_news_context:
            btc_news_context = AgentOrchestrator._as_dict(raw_data.get("btc_news_context"))

        sentiment = AgentOrchestrator._as_dict(container.get("sentiment"))
        if not sentiment:
            sentiment = AgentOrchestrator._as_dict(pipeline_signal.get("sentiment"))
        if not sentiment:
            sentiment = AgentOrchestrator._as_dict(signal_payload.get("sentiment"))

        order_flow_confirmed = AgentOrchestrator._as_optional_bool(container.get("order_flow_confirmed"))
        if order_flow_confirmed is None:
            order_flow_confirmed = AgentOrchestrator._as_optional_bool(pipeline_signal.get("order_flow_confirmed"))
        if order_flow_confirmed is None:
            order_flow_confirmed = AgentOrchestrator._as_optional_bool(signal_payload.get("order_flow_confirmed"))
        if order_flow_confirmed is None:
            order_flow_confirmed = AgentOrchestrator._as_optional_bool(raw_data.get("order_flow_confirmed"))
        if order_flow_confirmed is None:
            order_flow_confirmed = AgentOrchestrator._as_optional_bool(volume_context.get("directional_confirmed"))

        btc_news_confirms = AgentOrchestrator._as_optional_bool(container.get("btc_sentiment_confirms"))
        if btc_news_confirms is None:
            btc_news_confirms = AgentOrchestrator._as_optional_bool(container.get("btc_news_confirms"))
        if btc_news_confirms is None:
            btc_news_confirms = AgentOrchestrator._as_optional_bool(sentiment.get("btc_confirms"))
        if btc_news_confirms is None:
            btc_news_confirms = AgentOrchestrator._as_optional_bool(pipeline_signal.get("btc_sentiment_confirms"))
        if btc_news_confirms is None:
            btc_news_confirms = AgentOrchestrator._as_optional_bool(signal_payload.get("btc_sentiment_confirms"))
        if btc_news_confirms is None:
            btc_news_confirms = AgentOrchestrator._as_optional_bool(raw_data.get("btc_sentiment_confirms"))
        if btc_news_confirms is None:
            btc_news_confirms = AgentOrchestrator._as_optional_bool(btc_news_context.get("confirms"))

        btc_score = btc_news_context.get("score")
        if btc_score is None:
            btc_score = sentiment.get("btc_score")
        if btc_score is None:
            btc_score = container.get("btc_sentiment_score")
        if btc_score is None:
            btc_score = pipeline_signal.get("btc_sentiment_score")
        if btc_score is None:
            btc_score = signal_payload.get("btc_sentiment_score")

        btc_label = btc_news_context.get("label")
        if not btc_label:
            btc_label = sentiment.get("btc_label")
        if not btc_label:
            btc_label = container.get("btc_sentiment_label")
        if not btc_label:
            btc_label = pipeline_signal.get("btc_sentiment_label")
        if not btc_label:
            btc_label = signal_payload.get("btc_sentiment_label")

        volume_ratio = volume_context.get("volume_ratio")

        return {
            "order_flow_confirmed": order_flow_confirmed,
            "btc_news_confirms": btc_news_confirms,
            "btc_score": btc_score,
            "btc_label": btc_label,
            "volume_ratio": volume_ratio,
        }

    @staticmethod
    def _augment_market_data(base_data: Dict[str, Any], source_payload: Dict[str, Any]) -> Dict[str, Any]:
        enriched = dict(base_data)
        learning_features = AgentOrchestrator._extract_learning_features(source_payload)
        if learning_features:
            enriched["learning_features"] = learning_features
            for key, value in learning_features.items():
                if value is not None:
                    enriched[key] = value
        return enriched

    @staticmethod
    async def _gather_context(
        symbol: str,
        timeframe: str = "1h",
        exchange: SupportedExchange = SupportedExchange.BITGET,
        db: Optional[AsyncSession] = None,
    ) -> Dict[str, Any]:
        """Build market context dict for agents.

        For forex/metals symbols (XAUUSD, XAGUSD, EURUSD, etc.) that are not
        available on Bitget, data is fetched from Yahoo Finance via the
        ForexProvider module — giving live, real-time prices.
        """
        context: Dict[str, Any] = {"symbol": symbol, "timeframe": timeframe}

        # ── Branch: FX, metals, indices, energy, softs ───────────────────────
        # Two-tier guard via market_data: is_forex_symbol alone knows only a few
        # majors plus gold, so every cross, index and commodity used to fall
        # through to the crypto branch and fail there.
        if market_data.is_universal_symbol(symbol):
            try:
                ohlcv, forex_ticker = await market_data.fetch_ohlcv_universal(
                    symbol, timeframe=timeframe, limit=200
                )
                if ohlcv:
                    ta = technical_analyze(ohlcv, timeframe)
                    context["technical"] = ta
                    context["recent_candles"] = [
                        {"time": c[0], "open": c[1], "high": c[2], "low": c[3],
                         "close": c[4], "volume": c[5]}
                        for c in ohlcv[-5:]
                    ]
                    context["current_price"] = ohlcv[-1][4]
                    context["ticker"] = forex_ticker  # includes buy_volume, sell_volume
                    context["price_source"] = forex_ticker.get("source", "forex_provider")
                    logger.info(
                        f"[Orchestrator] {symbol} — live price "
                        f"{context['current_price']:.4g} via {context['price_source']}"
                    )
            except Exception as e:
                logger.warning(f"[Orchestrator] Forex OHLCV failed for {symbol}: {e}")
                context["technical"] = {"error": str(e)}

            # Sentiment stub (no exchange needed)
            try:
                base_coin = symbol.replace("USD", "").replace("/", "")
                context["sentiment"] = {
                    "symbol": base_coin,
                    "note": "Forex/metals — use pipeline_signal.sentiment for detail",
                }
            except Exception:
                pass

            return context

        # ── Branch: Crypto symbols via Bitget ─────────────────────────────────
        connector = exchange_manager.get_exchange(exchange)
        if not connector:
            return {"error": f"Exchange {exchange.value} not initialized", "symbol": symbol}

        # OHLCV + TA
        try:
            ohlcv = await connector.get_ohlcv(symbol=symbol, timeframe=timeframe, limit=200)
            ta = technical_analyze(ohlcv, timeframe)
            context["technical"] = ta
            # Last few candles for price context
            if ohlcv and len(ohlcv) >= 5:
                context["recent_candles"] = [
                    {"time": c[0], "open": c[1], "high": c[2], "low": c[3], "close": c[4], "volume": c[5]}
                    for c in ohlcv[-5:]
                ]
                context["current_price"] = ohlcv[-1][4]
        except Exception as e:
            logger.warning(f"[Orchestrator] OHLCV/TA failed for {symbol}: {e}")
            context["technical"] = {"error": str(e)}

        # Sentiment (basic — detailed sentiment comes from the pipeline context)
        try:
            base_coin = symbol.split("/")[0]
            context["sentiment"] = {"symbol": base_coin, "note": "Use pipeline_signal.sentiment for detailed data"}
        except Exception as e:
            logger.warning(f"[Orchestrator] Sentiment setup failed for {symbol}: {e}")
            context["sentiment"] = {"error": str(e)}

        # Ticker — the crypto connector only for crypto. A gold or FX symbol
        # sent there answers with an ERROR log and no data; the universal
        # resolver serves it from the live MT5 account instead.
        try:
            from app.services import market_data

            is_crypto = (
                market_data.classify(market_data.normalize_symbol(symbol))
                == market_data.CRYPTO
            )
            if is_crypto:
                ticker = await connector.get_ticker(symbol)
                context["ticker"] = {
                    "last": ticker.get("last"),
                    "bid": ticker.get("bid"),
                    "ask": ticker.get("ask"),
                    "high": ticker.get("high"),
                    "low": ticker.get("low"),
                    "volume": ticker.get("quoteVolume"),
                    "change_pct": ticker.get("percentage"),
                    "source": "bitget",
                }
            else:
                # No OHLC/volume from a broker quote — say so rather than
                # implying zeros the agent might reason from.
                quote = await market_data.get_quote(symbol, db=db)
                context["ticker"] = {
                    "last": quote.price if quote else None,
                    "bid": quote.bid if quote else None,
                    "ask": quote.ask if quote else None,
                    "high": None,
                    "low": None,
                    "volume": None,
                    "change_pct": quote.change_pct if quote else None,
                    "source": quote.source if quote else "unavailable",
                }
        except Exception:
            pass

        return context

    @staticmethod
    async def _build_knowledge_graph_prompt(db: AsyncSession, role: str, symbol: str) -> str:
        """Build the stored-knowledge + Graphify code-map block for an agent prompt.

        Plugin-optional: returns '' if the AI Market Analyst plugin is absent or
        disabled, so the core keeps working standalone.
        """
        extra = ""
        try:
            from plugins.AiMarketAnalyst.backend.services.ai_router import get_router_settings
            from plugins.AiMarketAnalyst.backend.services import knowledge_service, graphify_service
        except Exception:
            return ""
        try:
            settings = await get_router_settings(db)
            # Stored knowledge (DB) — always injected when present
            rows = await knowledge_service.query_knowledge(db, agent_role=role, symbol=symbol, limit=6)
            extra += knowledge_service.build_knowledge_prompt(rows)
            # Graphify code/knowledge map (runtime) — gated by the toggle
            if settings.graphify_enabled and graphify_service.graph_available():
                base_coin = symbol.split("/")[0]
                extra += graphify_service.build_graph_prompt(base_coin, limit=5)
        except Exception as e:
            logger.debug(f"[Orchestrator] knowledge/graph prompt skipped: {e}")
        return extra

    @staticmethod
    async def _store_decision_knowledge(
        db: AsyncSession,
        symbol: str,
        timeframe: str,
        action: str,
        confidence: Any,
        reasoning: str,
    ) -> None:
        """Persist a concise insight so agents can reference it on future tasks.

        Plugin-optional and best-effort; never blocks the pipeline.
        """
        if not reasoning:
            return
        try:
            from plugins.AiMarketAnalyst.backend.services import knowledge_service
        except Exception:
            return
        try:
            conf = float(confidence or 0)
            snippet = reasoning.strip()[:280]
            await knowledge_service.store_knowledge(
                db,
                content=f"{action.upper()} @ {timeframe} (conf {conf:.2f}) — {snippet}",
                agent_role="signal_generator",
                symbol=symbol,
                kind="outcome",
                title=f"Last {symbol} {timeframe} call",
                weight=1.0 + conf,  # higher-confidence decisions weigh more
                source="orchestrator",
            )
        except Exception as e:
            logger.debug(f"[Orchestrator] store_decision_knowledge skipped: {e}")

        # ── Live vault capture (fire-and-forget) ──────────────────────────────
        # Write every significant agent decision to the Obsidian vault so the
        # Intelligence brain map and Vault page learn from it in real time.
        try:
            from plugins.ObsidianKnowledgePlugin.backend.services.vault_capture import vault_capture
            conf_val = float(confidence or 0)
            if conf_val >= 0.4:  # only capture meaningful decisions
                vault_capture(
                    action_type="agent-decision",
                    symbol=symbol,
                    summary=f"{action.upper()} @ {timeframe} | conf={conf_val:.0%}",
                    detail=reasoning.strip()[:400],
                    tags=["agent-decision", symbol, action.lower(), timeframe],
                    agent_role="signal_generator",
                    confidence=conf_val,
                )
        except Exception:
            pass

    @staticmethod
    async def _run_agent_with_memory(
        db: AsyncSession,
        agent,
        context: Dict[str, Any],
        symbol: str,
    ) -> Dict[str, Any]:
        """
        Run a single agent with memory awareness:
        1. Fetch past decisions for this symbol + role
        2. Inject stored knowledge + Graphify map
        3. Try local decision from memory (no LLM call)
        4. If not confident enough, route through connected providers (or OpenAI)
        """
        past = await get_past_decisions(db, symbol, agent.role)
        memory_prompt = build_memory_prompt(past)
        memory_count = len([d for d in past if d.get("outcome")])

        # Inject stored knowledge + Graphify code map (plugin-optional)
        memory_prompt += await AgentOrchestrator._build_knowledge_graph_prompt(db, agent.role, symbol)

        # ── Obsidian vault context (plugin-optional) ───────────────────────────
        # When OBSIDIAN_INJECT_CONTEXT=true, recent vault notes for this symbol
        # are appended to the memory prompt — enriching agent reasoning with
        # human-curated strategy notes and historical signal journals.
        if settings.OBSIDIAN_INJECT_CONTEXT:
            try:
                from plugins.ObsidianKnowledgePlugin.backend.services.vault_reader import VaultReader
                vault_reader = VaultReader()
                vault_context = vault_reader.get_context_for_symbol(symbol)
                if vault_context:
                    memory_prompt += (
                        "\n\n## Obsidian Vault Knowledge\n"
                        "_(Recent notes from the linked knowledge vault)_\n\n"
                        + vault_context
                    )
            except Exception as _vault_exc:
                logger.debug(f"[Orchestrator] Vault context skipped: {_vault_exc}")

        # ── Agent-Reach live research (plugin-optional) ────────────────────────
        # market_analyst/sentiment_analyst only: the other 4 roles already see
        # this indirectly via signal_context["agent_analyses"], so injecting it
        # again downstream would just add latency without new information.
        if settings.AGENT_REACH_INJECT_CONTEXT and agent.role in ("market_analyst", "sentiment_analyst"):
            try:
                from app.services import agent_reach_client
                research_summary = await agent_reach_client.research_summary_for_symbol(
                    symbol, token_budget=settings.AGENT_REACH_CONTEXT_TOKEN_BUDGET,
                )
                if research_summary:
                    memory_prompt += "\n\n## Agent-Reach Live Research\n" + research_summary
            except Exception as _ar_exc:
                logger.debug(f"[Orchestrator] Agent-Reach context skipped: {_ar_exc}")

        # Try local decision first (no LLM call)
        local = try_local_decision(past, agent.role)
        if local is not None:
            local["memory_context_used"] = memory_count
            return await agent.analyze(context, local_decision=local)

        # Route through connected providers (db passed) → falls back to OpenAI
        decision = await agent.analyze(context, memory_prompt=memory_prompt, db=db)
        decision["memory_context_used"] = memory_count
        return decision

    @staticmethod
    async def _background_ai_allowed(db: AsyncSession) -> bool:
        """Whether background (scanner) analyses may spend AI tokens.

        In the default 'telegram_only' token mode, the continuous pair-scanner
        does NOT spend tokens — the free daily tier is reserved for
        telegram-signal and manual analyses. Plugin-optional; if the AI Market
        Analyst plugin is absent, background AI is allowed (legacy behaviour).
        """
        try:
            from plugins.AiMarketAnalyst.backend.services.ai_router import get_router_settings
        except Exception:
            return True
        try:
            s = await get_router_settings(db)
            return getattr(s, "agent_token_mode", "telegram_only") != "telegram_only"
        except Exception:
            return True

    @staticmethod
    async def analyze_symbol(
        db: AsyncSession,
        symbol: str,
        timeframe: str = "1h",
        trigger: str = "scanner",
    ) -> Dict[str, Any]:
        """
        Run the full agent pipeline for a single symbol.
        Returns the orchestration result with all agent decisions.

        Respects ENABLE_AI_AGENTS — returns early if disabled.
        ``trigger`` controls token spend: 'scanner' (background) is skipped in
        the default telegram-only token mode; 'manual' and 'telegram' always run.
        Falls back gracefully if OpenAI is unavailable.
        """
        # ── Guard: AI agents must be enabled ──
        if not settings.ENABLE_AI_AGENTS:
            return {
                "error": "AI agents are disabled. Enable ENABLE_AI_AGENTS in settings.",
                "symbol": symbol,
                "ai_enabled": False,
            }

        # ── Guard: Token budget — skip background scans in telegram-only mode ──
        # Reserves the free daily/monthly tier for telegram-signal & manual
        # analyses so the continuous pair-scanner never drains it.
        if trigger == "scanner" and not await AgentOrchestrator._background_ai_allowed(db):
            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "ai_enabled": True,
                "token_skipped": True,
                "final_action": "hold",
                "final_confidence": 0,
                "final_reasoning": "AI skipped — telegram-only token mode (background scan preserves free tier)",
                "ai_calls": 0,
                "decisions": [],
            }

        # ── Guard: Circuit breaker — skip entire pipeline if OpenAI is down ──
        from app.agents.base import _circuit_is_open, _circuit_reason
        if _circuit_is_open():
            logger.debug(f"[Orchestrator] Skipping {symbol} — AI circuit breaker open: {_circuit_reason}")
            return {
                "error": f"AI circuit breaker open: {_circuit_reason}",
                "symbol": symbol,
                "ai_enabled": True,
                "circuit_breaker": True,
                "final_action": "hold",
                "final_confidence": 0,
                "final_reasoning": "AI unavailable — circuit breaker tripped",
                "ai_calls": 0,
            }

        session_id = str(uuid.uuid4())[:12]
        logger.info(f"[Orchestrator:{session_id}] Starting analysis for {symbol} ({timeframe})")

        # ── Load active agents from DB ──
        result = await db.execute(select(Agent).where(Agent.is_active == True))
        agents_rows = result.scalars().all()

        if not agents_rows:
            return {"error": "No active agents configured", "symbol": symbol}

        agents_by_role: Dict[str, Any] = {}
        for row in agents_rows:
            # If agent has pair restriction, check
            if row.pairs:
                allowed = [p.strip() for p in row.pairs.split(",")]
                if symbol not in allowed and symbol.replace("/", "") not in allowed:
                    continue
            agents_by_role.setdefault(row.role, []).append(agent_from_db(row))

        # ── Gather market context ──
        context = await AgentOrchestrator._gather_context(symbol, timeframe, db=db)
        if "error" in context and "technical" not in context:
            return {"error": context["error"], "symbol": symbol}

        decisions: List[Dict[str, Any]] = []
        all_errors: List[str] = []

        # ── Phase 1: Market Analyst + Sentiment Analyst (parallel) ──
        phase1_tasks = []
        phase1_agents = []
        for role in ("market_analyst", "sentiment_analyst"):
            for agent in agents_by_role.get(role, []):
                phase1_tasks.append(
                    AgentOrchestrator._run_agent_with_memory(db, agent, context, symbol)
                )
                phase1_agents.append(agent)

        if phase1_tasks:
            phase1_results = await asyncio.gather(*phase1_tasks, return_exceptions=True)
            for agent, res in zip(phase1_agents, phase1_results):
                if isinstance(res, Exception):
                    all_errors.append(f"{agent.name}: {res}")
                    continue
                decisions.append(res)
                db.add(AgentDecision(
                    agent_id=agent.agent_id,
                    agent_name=agent.name,
                    agent_role=agent.role,
                    symbol=symbol,
                    action=res.get("action", "hold"),
                    confidence=res.get("confidence", 0),
                    reasoning=res.get("reasoning", ""),
                    market_data=json.dumps(res, default=str),
                    session_id=session_id,
                    ai_called=res.get("ai_called", True),
                    memory_context_used=res.get("memory_context_used", 0),
                ))

        # Build enriched context for signal generator
        signal_context = {**context}
        signal_context["agent_analyses"] = decisions

        # ── Phase 2: Signal Generator ──
        signal_decision = None
        for agent in agents_by_role.get("signal_generator", []):
            try:
                signal_decision = await AgentOrchestrator._run_agent_with_memory(
                    db, agent, signal_context, symbol
                )
                decisions.append(signal_decision)
                db.add(AgentDecision(
                    agent_id=agent.agent_id,
                    agent_name=agent.name,
                    agent_role=agent.role,
                    symbol=symbol,
                    action=signal_decision.get("action", "hold"),
                    confidence=signal_decision.get("confidence", 0),
                    reasoning=signal_decision.get("reasoning", ""),
                    market_data=json.dumps(signal_decision, default=str),
                    session_id=session_id,
                    ai_called=signal_decision.get("ai_called", True),
                    memory_context_used=signal_decision.get("memory_context_used", 0),
                ))
            except Exception as e:
                all_errors.append(f"Signal Generator: {e}")
            break  # use first active signal generator

        # ── Phase 3: Risk Manager (if BUY or SELL) ──
        risk_decision = None
        action = (signal_decision or {}).get("action", "hold")
        if action in ("buy", "sell") and agents_by_role.get("risk_manager"):
            risk_context = {**signal_context, "proposed_trade": signal_decision}
            for agent in agents_by_role["risk_manager"]:
                try:
                    risk_decision = await AgentOrchestrator._run_agent_with_memory(
                        db, agent, risk_context, symbol
                    )
                    decisions.append(risk_decision)
                    db.add(AgentDecision(
                        agent_id=agent.agent_id,
                        agent_name=agent.name,
                        agent_role=agent.role,
                        symbol=symbol,
                        action=risk_decision.get("action", "reject"),
                        confidence=risk_decision.get("confidence", 0),
                        reasoning=risk_decision.get("reasoning", ""),
                        market_data=json.dumps(risk_decision, default=str),
                        session_id=session_id,
                        ai_called=risk_decision.get("ai_called", True),
                        memory_context_used=risk_decision.get("memory_context_used", 0),
                    ))
                except Exception as e:
                    all_errors.append(f"Risk Manager: {e}")
                break

        # ── Phase 4: Trade Executor (if approved) ──
        exec_decision = None
        risk_action = (risk_decision or {}).get("action", "reject")
        if risk_action in ("approve", "modify") and agents_by_role.get("trade_executor"):
            exec_context = {
                **signal_context,
                "proposed_trade": signal_decision,
                "risk_review": risk_decision,
            }
            for agent in agents_by_role["trade_executor"]:
                try:
                    exec_decision = await AgentOrchestrator._run_agent_with_memory(
                        db, agent, exec_context, symbol
                    )
                    decisions.append(exec_decision)
                    db.add(AgentDecision(
                        agent_id=agent.agent_id,
                        agent_name=agent.name,
                        agent_role=agent.role,
                        symbol=symbol,
                        action=exec_decision.get("action", "cancel"),
                        confidence=exec_decision.get("confidence", 0),
                        reasoning=exec_decision.get("reasoning", ""),
                        market_data=json.dumps(exec_decision, default=str),
                        session_id=session_id,
                        ai_called=exec_decision.get("ai_called", True),
                        memory_context_used=exec_decision.get("memory_context_used", 0),
                    ))
                except Exception as e:
                    all_errors.append(f"Trade Executor: {e}")
                break

        # ── Save signal to DB if actionable ──
        saved_signal = None
        final_action = action
        if risk_action == "reject":
            final_action = "hold"

        if final_action in ("buy", "sell"):
            confidence = signal_decision.get("confidence", 0) if signal_decision else 0
            sig = Signal(
                source=SignalSource.SYSTEM.value,
                symbol=symbol,
                action=SignalAction.BUY.value if final_action == "buy" else SignalAction.SELL.value,
                price=context.get("current_price", 0),
                timeframe=timeframe,
                strength=confidence,
                confidence=confidence,
                status=SignalStatus.PENDING.value,
                raw_data=json.dumps({
                    "session_id": session_id,
                    "agent_decisions": [d.get("reasoning", "") for d in decisions],
                    "risk_review": risk_decision,
                    "execution_plan": exec_decision,
                }, default=str),
                indicators=json.dumps({
                    "sl_pct": signal_decision.get("stop_loss_pct", 2),
                    "tp_pct": signal_decision.get("take_profit_pct", 4),
                    "risk_score": (risk_decision or {}).get("risk_score", 5),
                    "position_size_pct": (risk_decision or {}).get("position_size_pct", 2),
                    "max_leverage": (risk_decision or {}).get("max_leverage", 10),
                }, default=str),
            )
            db.add(sig)
            await db.flush()
            saved_signal = {"id": sig.id, "action": final_action, "symbol": symbol}

            # Update agent decisions with signal_id
            for ad in await db.execute(
                select(AgentDecision).where(AgentDecision.session_id == session_id)
            ):
                for row in ad.scalars():
                    row.signal_id = sig.id

        await db.commit()

        ai_calls = sum(1 for d in decisions if d.get("ai_called", True))
        local_calls = len(decisions) - ai_calls

        # Derive confidence & reasoning from Signal Generator decision
        final_confidence = signal_decision.get("confidence", 0) if signal_decision else 0
        final_reasoning = signal_decision.get("reasoning", "") if signal_decision else ""

        # ── Persist an insight to the agent knowledge store (plugin-optional) ──
        await AgentOrchestrator._store_decision_knowledge(
            db, symbol, timeframe, final_action, final_confidence, final_reasoning
        )

        return {
            "session_id": session_id,
            "symbol": symbol,
            "timeframe": timeframe,
            "final_action": final_action,
            "final_confidence": final_confidence,
            "final_reasoning": final_reasoning,
            "signal": saved_signal,
            "decisions": decisions,
            "errors": all_errors,
            "agents_used": len(decisions),
            "ai_calls": ai_calls,
            "local_decisions": local_calls,
            "ai_enabled": True,
        }

    @staticmethod
    async def analyze_multiple(
        db: AsyncSession,
        symbols: List[str],
        timeframe: str = "1h",
        trigger: str = "manual",
    ) -> List[Dict[str, Any]]:
        """Run orchestration for multiple symbols sequentially."""
        results = []
        for symbol in symbols:
            try:
                r = await AgentOrchestrator.analyze_symbol(db, symbol, timeframe, trigger=trigger)
                results.append(r)
            except Exception as e:
                logger.error(f"[Orchestrator] Failed for {symbol}: {e}")
                results.append({"symbol": symbol, "error": str(e)})
        return results

    # ────────────────────────────────────────────────────────────
    # Signal Validation — called by the signal pipeline
    # ────────────────────────────────────────────────────────────

    @staticmethod
    async def validate_signal(
        db: AsyncSession,
        symbol: str,
        signal_data: Dict[str, Any],
        timeframe: str = "1h",
    ) -> Dict[str, Any]:
        """
        Validate a signal generated by the main pipeline using AI agents.
        Runs Signal Generator + Risk Manager for confirmation.
        Does NOT create signals in DB — only returns validation result.

        Returns: {approved: bool, confidence: float, reasoning: str, decisions: [...]}
        """
        if not settings.ENABLE_AI_AGENTS:
            from app.agents.custom_agents import are_custom_agents_enabled, custom_validate_trade
            if are_custom_agents_enabled():
                context = await AgentOrchestrator._gather_context(symbol, timeframe, db=db)
                context["pipeline_signal"] = signal_data
                pos_ctx = {"open_positions": 0, "max_positions": 5, "available_balance": 1000, "total_exposure": 0, "max_exposure": 5000, "is_dca": False}
                return await custom_validate_trade(db, symbol, signal_data, pos_ctx, context)
            return {"approved": True, "reason": "AI agents disabled — auto-approved"}

        from app.agents.base import _circuit_is_open
        if _circuit_is_open():
            from app.agents.custom_agents import are_custom_agents_enabled, custom_validate_trade
            if are_custom_agents_enabled():
                context = await AgentOrchestrator._gather_context(symbol, timeframe, db=db)
                context["pipeline_signal"] = signal_data
                pos_ctx = {"open_positions": 0, "max_positions": 5, "available_balance": 1000, "total_exposure": 0, "max_exposure": 5000, "is_dca": False}
                return await custom_validate_trade(db, symbol, signal_data, pos_ctx, context)
            return {"approved": True, "reason": "AI circuit breaker open — auto-approved"}

        session_id = f"val-{str(uuid.uuid4())[:8]}"
        logger.info(f"[Orchestrator:{session_id}] Validating {signal_data.get('action', '?')} signal for {symbol}")

        result = await db.execute(select(Agent).where(Agent.is_active == True))
        agents_rows = result.scalars().all()
        if not agents_rows:
            return {"approved": True, "reason": "No active agents — auto-approved"}

        agents_by_role: Dict[str, Any] = {}
        for row in agents_rows:
            if row.pairs:
                allowed = [p.strip() for p in row.pairs.split(",")]
                if symbol not in allowed and symbol.replace("/", "") not in allowed:
                    continue
            agents_by_role.setdefault(row.role, []).append(agent_from_db(row))

        # Build context from signal data + fresh market data
        context = await AgentOrchestrator._gather_context(symbol, timeframe, db=db)
        context["pipeline_signal"] = signal_data

        decisions: List[Dict[str, Any]] = []

        # Phase 1: Signal Generator validation
        for agent in agents_by_role.get("signal_generator", [])[:1]:
            try:
                decision = await AgentOrchestrator._run_agent_with_memory(
                    db, agent, context, symbol
                )
                decisions.append(decision)
                market_data = AgentOrchestrator._augment_market_data({"validation": True, **decision}, context)
                db.add(AgentDecision(
                    agent_id=agent.agent_id,
                    agent_name=agent.name,
                    agent_role=agent.role,
                    symbol=symbol,
                    action=decision.get("action", "hold"),
                    confidence=decision.get("confidence", 0),
                    reasoning=decision.get("reasoning", ""),
                    market_data=json.dumps(market_data, default=str),
                    session_id=session_id,
                    ai_called=decision.get("ai_called", True),
                    memory_context_used=decision.get("memory_context_used", 0),
                ))
            except Exception as e:
                logger.warning(f"[Orchestrator] Signal validation agent error: {e}")

        # Phase 2: Risk Manager validation
        agent_action = decisions[0].get("action", "hold") if decisions else signal_data.get("action", "hold")
        if agent_action in ("buy", "sell"):
            risk_context = {**context, "proposed_trade": decisions[0] if decisions else signal_data}
            for agent in agents_by_role.get("risk_manager", [])[:1]:
                try:
                    risk_decision = await AgentOrchestrator._run_agent_with_memory(
                        db, agent, risk_context, symbol
                    )
                    decisions.append(risk_decision)
                    market_data = AgentOrchestrator._augment_market_data({"validation": True, **risk_decision}, risk_context)
                    db.add(AgentDecision(
                        agent_id=agent.agent_id,
                        agent_name=agent.name,
                        agent_role=agent.role,
                        symbol=symbol,
                        action=risk_decision.get("action", "reject"),
                        confidence=risk_decision.get("confidence", 0),
                        reasoning=risk_decision.get("reasoning", ""),
                        market_data=json.dumps(market_data, default=str),
                        session_id=session_id,
                        ai_called=risk_decision.get("ai_called", True),
                        memory_context_used=risk_decision.get("memory_context_used", 0),
                    ))
                except Exception as e:
                    logger.warning(f"[Orchestrator] Risk validation agent error: {e}")

        await db.commit()

        # Determine approval
        approved = True
        reasoning_parts = []

        if decisions:
            sig_decision = decisions[0]
            sig_action = sig_decision.get("action", "hold")
            sig_confidence = sig_decision.get("confidence", 0)

            # Signal Generator disagrees with pipeline
            pipeline_action = signal_data.get("action", "hold")
            if sig_action != pipeline_action and sig_action == "hold":
                approved = False
                reasoning_parts.append(f"Signal Generator recommends HOLD (conf={sig_confidence:.2f})")

            # Risk Manager rejects
            if len(decisions) > 1:
                risk = decisions[1]
                risk_action = risk.get("action", "reject")
                if risk_action == "reject":
                    approved = False
                    reasoning_parts.append(f"Risk Manager rejected: {risk.get('reasoning', 'N/A')[:100]}")
                elif risk_action == "modify":
                    reasoning_parts.append(f"Risk Manager modified: {risk.get('reasoning', 'N/A')[:100]}")

        ai_calls = sum(1 for d in decisions if d.get("ai_called", True))
        return {
            "approved": approved,
            "session_id": session_id,
            "confidence": decisions[0].get("confidence", 0) if decisions else 0,
            "reasoning": " | ".join(reasoning_parts) if reasoning_parts else "Agents approved",
            "decisions": decisions,
            "ai_calls": ai_calls,
            "risk_params": decisions[1] if len(decisions) > 1 else None,
        }

    # ────────────────────────────────────────────────────────────
    # Trade Validation — called before auto-trade execution
    # ────────────────────────────────────────────────────────────

    @staticmethod
    async def validate_trade(
        db: AsyncSession,
        symbol: str,
        signal: Dict[str, Any],
        position_context: Dict[str, Any],
        timeframe: str = "1h",
        auto_trade_ai_provider: str = "orchestrator",
        tradingagents_llm_provider: str = "openai",
        tradingagents_deep_think_llm: str = "gpt-5.4",
        tradingagents_quick_think_llm: str = "gpt-5.4-mini",
        tradingagents_backend_url: Optional[str] = None,
        tradingagents_max_debate_rounds: int = 2,
        tradingagents_max_risk_discuss_rounds: int = 2,
    ) -> Dict[str, Any]:
        """
        Final validation before placing a trade order.
        Runs Risk Manager + Trade Executor with position/balance context.
        Falls back to custom rule-based agents when AI is unavailable.

        Returns: {approved: bool, order_params: {...}, reasoning: str}
        """
        if not settings.ENABLE_AI_AGENTS:
            # Check if custom agents are enabled as fallback
            from app.agents.custom_agents import are_custom_agents_enabled, custom_validate_trade
            if are_custom_agents_enabled():
                context = await AgentOrchestrator._gather_context(symbol, timeframe, db=db)
                context["signal"] = signal
                context["positions"] = position_context
                return await custom_validate_trade(db, symbol, signal, position_context, context)
            return {"approved": True, "reason": "AI agents disabled — auto-approved"}

        from app.agents.base import _circuit_is_open
        if _circuit_is_open():
            # Use custom agents when circuit breaker is open
            from app.agents.custom_agents import are_custom_agents_enabled, custom_validate_trade
            if are_custom_agents_enabled():
                context = await AgentOrchestrator._gather_context(symbol, timeframe, db=db)
                context["signal"] = signal
                context["positions"] = position_context
                return await custom_validate_trade(db, symbol, signal, position_context, context)
            return {"approved": True, "reason": "AI circuit breaker open — auto-approved"}

        provider = str(auto_trade_ai_provider or "orchestrator").strip().lower()
        if provider == "tradingagents":
            ta_session_id = f"trade-ta-{str(uuid.uuid4())[:8]}"
            logger.info(f"[Orchestrator:{ta_session_id}] Validating trade for {symbol} via TradingAgents")

            try:
                from tradingagents.graph.trading_graph import TradingAgentsGraph

                max_debate_rounds = max(1, min(6, int(tradingagents_max_debate_rounds or 2)))
                max_risk_discuss_rounds = max(1, min(6, int(tradingagents_max_risk_discuss_rounds or 2)))

                ta_config: Any
                try:
                    from tradingagents.config import TradingAgentsConfig

                    ta_config = TradingAgentsConfig(
                        llm_provider=str(tradingagents_llm_provider or "openai"),
                        deep_think_llm=str(tradingagents_deep_think_llm or "gpt-5.4"),
                        quick_think_llm=str(tradingagents_quick_think_llm or "gpt-5.4-mini"),
                        max_debate_rounds=max_debate_rounds,
                        max_risk_discuss_rounds=max_risk_discuss_rounds,
                        max_recur_limit=30,
                    )
                except Exception:
                    # Compatibility path for older TradingAgents builds exposing DEFAULT_CONFIG.
                    from tradingagents.default_config import DEFAULT_CONFIG

                    ta_config = deepcopy(DEFAULT_CONFIG)
                    overrides = {
                        "llm_provider": tradingagents_llm_provider,
                        "deep_think_llm": tradingagents_deep_think_llm,
                        "quick_think_llm": tradingagents_quick_think_llm,
                        "backend_url": tradingagents_backend_url,
                        "max_debate_rounds": max_debate_rounds,
                        "max_risk_discuss_rounds": max_risk_discuss_rounds,
                    }
                    for key, value in overrides.items():
                        if value is not None and key in ta_config:
                            ta_config[key] = value

                graph = TradingAgentsGraph(debug=False, config=ta_config)
                company_name = (
                    symbol.replace("/USDT", "")
                    .replace("/USD", "")
                    .replace("/", "")
                    .replace(":", "")
                    .strip()
                )
                if not company_name:
                    company_name = symbol

                trade_date = now_sast().strftime("%Y-%m-%d")
                ta_result = await asyncio.to_thread(
                    graph.propagate,
                    company_name,
                    trade_date,
                )

                raw_decision: Any = ta_result
                if isinstance(ta_result, (tuple, list)):
                    if len(ta_result) > 1:
                        raw_decision = ta_result[1]
                    elif len(ta_result) == 1:
                        raw_decision = ta_result[0]

                if hasattr(raw_decision, "model_dump"):
                    raw_decision = raw_decision.model_dump()
                elif hasattr(raw_decision, "dict"):
                    raw_decision = raw_decision.dict()

                if isinstance(raw_decision, str):
                    try:
                        raw_decision = json.loads(raw_decision)
                    except Exception:
                        raw_decision = {"decision": raw_decision}

                if isinstance(raw_decision, dict):
                    signal_hint = raw_decision.get("signal")
                    if (
                        isinstance(signal_hint, str)
                        and not raw_decision.get("decision")
                        and not raw_decision.get("action")
                        and not raw_decision.get("final_decision")
                    ):
                        raw_decision["decision"] = signal_hint

                if not isinstance(raw_decision, dict):
                    raw_decision = {"decision": str(raw_decision)}

                parsed = AgentOrchestrator._parse_trade_validation_decision(signal.get("action"), raw_decision)
                decision_action = "approve" if parsed["approved"] else "reject"
                decision = {
                    "agent_name": "TradingAgents",
                    "agent_role": "tradingagents",
                    "provider": "tradingagents",
                    "action": decision_action,
                    "confidence": parsed["confidence"],
                    "reasoning": parsed["reasoning"],
                    "raw_decision": raw_decision,
                    "session_id": ta_session_id,
                }

                return {
                    "approved": parsed["approved"],
                    "session_id": ta_session_id,
                    "reasoning": parsed["reasoning"],
                    "decisions": [decision],
                    "order_params": None,
                    "provider": "tradingagents",
                }
            except Exception as e:
                logger.warning(f"[Orchestrator:{ta_session_id}] TradingAgents validation failed, falling back to orchestrator: {e}")

        session_id = f"trade-{str(uuid.uuid4())[:8]}"
        logger.info(f"[Orchestrator:{session_id}] Validating trade execution for {symbol}")

        result = await db.execute(select(Agent).where(Agent.is_active == True))
        agents_rows = result.scalars().all()
        if not agents_rows:
            return {"approved": True, "reason": "No active agents — auto-approved"}

        agents_by_role: Dict[str, Any] = {}
        for row in agents_rows:
            if row.pairs:
                allowed = [p.strip() for p in row.pairs.split(",")]
                if symbol not in allowed and symbol.replace("/", "") not in allowed:
                    continue
            agents_by_role.setdefault(row.role, []).append(agent_from_db(row))

        context = await AgentOrchestrator._gather_context(symbol, timeframe, db=db)
        context["signal"] = signal
        context["positions"] = position_context

        decisions: List[Dict[str, Any]] = []

        # Risk Manager
        for agent in agents_by_role.get("risk_manager", [])[:1]:
            try:
                decision = await AgentOrchestrator._run_agent_with_memory(
                    db, agent, context, symbol
                )
                decisions.append(decision)
                market_data = AgentOrchestrator._augment_market_data({"trade_validation": True, **decision}, context)
                db.add(AgentDecision(
                    agent_id=agent.agent_id,
                    agent_name=agent.name,
                    agent_role=agent.role,
                    symbol=symbol,
                    action=decision.get("action", "reject"),
                    confidence=decision.get("confidence", 0),
                    reasoning=decision.get("reasoning", ""),
                    market_data=json.dumps(market_data, default=str),
                    session_id=session_id,
                    ai_called=decision.get("ai_called", True),
                    memory_context_used=decision.get("memory_context_used", 0),
                ))
            except Exception as e:
                logger.warning(f"[Orchestrator] Trade Risk Manager error: {e}")

        # Trade Executor
        if decisions and decisions[0].get("action") in ("approve", "modify"):
            exec_context = {**context, "risk_review": decisions[0]}
            for agent in agents_by_role.get("trade_executor", [])[:1]:
                try:
                    exec_decision = await AgentOrchestrator._run_agent_with_memory(
                        db, agent, exec_context, symbol
                    )
                    decisions.append(exec_decision)
                    market_data = AgentOrchestrator._augment_market_data({"trade_validation": True, **exec_decision}, exec_context)
                    db.add(AgentDecision(
                        agent_id=agent.agent_id,
                        agent_name=agent.name,
                        agent_role=agent.role,
                        symbol=symbol,
                        action=exec_decision.get("action", "cancel"),
                        confidence=exec_decision.get("confidence", 0),
                        reasoning=exec_decision.get("reasoning", ""),
                        market_data=json.dumps(market_data, default=str),
                        session_id=session_id,
                        ai_called=exec_decision.get("ai_called", True),
                        memory_context_used=exec_decision.get("memory_context_used", 0),
                    ))
                except Exception as e:
                    logger.warning(f"[Orchestrator] Trade Executor error: {e}")

        await db.commit()

        # Determine approval
        approved = True
        reasoning_parts = []

        if decisions:
            risk = decisions[0]
            if risk.get("action") == "reject":
                approved = False
                reasoning_parts.append(f"Risk Manager rejected: {risk.get('reasoning', 'N/A')[:100]}")

            if len(decisions) > 1:
                executor = decisions[1]
                if executor.get("action") == "cancel":
                    approved = False
                    reasoning_parts.append(f"Trade Executor cancelled: {executor.get('reasoning', 'N/A')[:100]}")
                elif executor.get("action") == "wait":
                    approved = False
                    reasoning_parts.append(f"Trade Executor: wait — {executor.get('timing', 'N/A')}")

        return {
            "approved": approved,
            "session_id": session_id,
            "reasoning": " | ".join(reasoning_parts) if reasoning_parts else "Trade approved",
            "decisions": decisions,
            "order_params": decisions[1] if len(decisions) > 1 else None,
        }

    # ────────────────────────────────────────────────────────────
    # Position Monitor — analyzes open positions
    # ────────────────────────────────────────────────────────────

    @staticmethod
    async def analyze_positions(
        db: AsyncSession,
        min_hold_hours: float = 2.0,
    ) -> Dict[str, Any]:
        """
        Analyze open positions that have been held for at least min_hold_hours
        since their last agent review (or since entry if never reviewed).

        Uses the Position Reviewer + Market Analyst agents.
        Returns list of position reviews with HOLD/CLOSE/ADJUST decisions.
        """
        use_custom_agents = False
        if not settings.ENABLE_AI_AGENTS:
            from app.agents.custom_agents import are_custom_agents_enabled
            if not are_custom_agents_enabled():
                return {"skipped": True, "reason": "AI agents disabled and custom agents not enabled"}
            use_custom_agents = True

        from app.agents.base import _circuit_is_open
        if not use_custom_agents and _circuit_is_open():
            from app.agents.custom_agents import are_custom_agents_enabled
            if not are_custom_agents_enabled():
                return {"skipped": True, "reason": "AI circuit breaker open and custom agents not enabled"}
            use_custom_agents = True

        session_id = f"pos-{str(uuid.uuid4())[:8]}"
        logger.info(f"[Orchestrator:{session_id}] Starting position analysis (min_hold={min_hold_hours}h, custom={use_custom_agents})")

        from datetime import timedelta
        now = now_sast()
        review_threshold = now - timedelta(hours=min_hold_hours)

        # Fetch open trades
        result = await db.execute(
            select(Trade).where(
                Trade.exchange == "bitget",
                Trade.status == "open",
                Trade.trade_side == "open",
            )
        )
        open_trades = result.scalars().all()
        if not open_trades:
            return {"skipped": False, "positions_reviewed": 0, "reason": "No open positions"}

        # Filter: only review positions older than threshold
        # Check last agent review via AgentDecision with session_id starting with "pos-"
        trades_to_review = []
        for trade in open_trades:
            # Check last position review for this trade's symbol
            last_review = await db.execute(
                select(AgentDecision.created_at)
                .where(
                    AgentDecision.symbol == trade.symbol,
                    AgentDecision.agent_role == "position_reviewer",
                    AgentDecision.session_id.like("pos-%"),
                )
                .order_by(AgentDecision.created_at.desc())
                .limit(1)
            )
            last_review_time = last_review.scalar_one_or_none()

            # If never reviewed, use trade creation time
            reference_time = last_review_time or trade.created_at
            if reference_time and reference_time < review_threshold:
                trades_to_review.append(trade)

        if not trades_to_review:
            return {
                "skipped": False,
                "positions_reviewed": 0,
                "reason": f"No positions need review (all reviewed within {min_hold_hours}h)",
            }

        # Load active agents
        agent_result = await db.execute(select(Agent).where(Agent.is_active == True))
        agents_rows = agent_result.scalars().all()
        agents_by_role: Dict[str, Any] = {}
        for row in agents_rows:
            agents_by_role.setdefault(row.role, []).append(agent_from_db(row))

        # Get exchange positions for current prices/PnL
        from app.exchanges.bitget import BitgetConnector
        from typing import cast
        connector = cast(
            Optional[BitgetConnector],
            exchange_manager.get_exchange(SupportedExchange.BITGET),
        )

        exchange_positions = []
        if connector:
            try:
                pos_data = await connector.get_futures_positions()
                exchange_positions = [
                    p for p in (pos_data or []) if float(p.get("total", 0)) > 0
                ]
            except Exception as e:
                logger.warning(f"[Orchestrator] Failed to fetch positions: {e}")

        reviews = []
        actions_taken = []

        for trade in trades_to_review:
            symbol = trade.symbol
            bitget_sym = symbol.replace("/", "").upper()

            # Find matching exchange position
            hold_side = "long" if trade.side == "buy" else "short"
            exchange_pos = next(
                (p for p in exchange_positions
                 if p.get("symbol", "").upper().replace("USDT_UMCBL", "USDT") == bitget_sym
                 and (p.get("holdSide", "") or "").lower() == hold_side),
                None,
            )

            current_price = 0.0
            unrealized_pnl = 0.0
            if exchange_pos:
                current_price = float(exchange_pos.get("markPrice", 0) or exchange_pos.get("marketPrice", 0) or 0)
                unrealized_pnl = float(exchange_pos.get("unrealizedPL", 0) or 0)

            if current_price <= 0 and connector:
                try:
                    ticker = await connector.get_ticker(symbol)
                    current_price = float(ticker.get("last", 0) or 0)
                except Exception:
                    pass

            entry_price = trade.price or 0
            hold_duration_hours = (now - trade.created_at).total_seconds() / 3600 if trade.created_at else 0

            pnl_pct = 0.0
            if entry_price > 0 and current_price > 0:
                if hold_side == "long":
                    pnl_pct = ((current_price - entry_price) / entry_price) * 100
                else:
                    pnl_pct = ((entry_price - current_price) / entry_price) * 100

            # Build position context
            position_context = {
                "symbol": symbol,
                "side": trade.side,
                "hold_side": hold_side,
                "entry_price": entry_price,
                "current_price": current_price,
                "unrealized_pnl": unrealized_pnl,
                "pnl_pct": round(pnl_pct, 2),
                "hold_duration_hours": round(hold_duration_hours, 1),
                "stop_loss": trade.stop_loss,
                "take_profit": trade.take_profit,
                "leverage": trade.leverage,
                "amount": trade.amount,
                "trade_id": trade.id,
            }

            # Gather market context — multi-timeframe for reversal detection
            context = await AgentOrchestrator._gather_context(symbol, "1h", db=db)
            context["position"] = position_context

            # Add multi-TF analysis for reversal detection
            multi_tf_data = {}
            for tf in ("5m", "15m", "4h"):
                try:
                    tf_ohlcv = await connector.get_ohlcv(symbol=symbol, timeframe=tf, limit=100) if connector else None
                    if tf_ohlcv and len(tf_ohlcv) >= 20:
                        tf_ta = technical_analyze(tf_ohlcv, tf)
                        multi_tf_data[tf] = {
                            "score": tf_ta.get("score"),
                            "action": tf_ta.get("action"),
                            "indicators": {
                                k: tf_ta.get("indicators", {}).get(k)
                                for k in ("rsi", "macd_histogram", "macd", "macd_signal", "adx", "plus_di", "minus_di", "ema50", "ema200", "stoch_rsi", "price", "bb_pct_b", "bb_upper", "bb_lower", "volume_ratio", "buy_ratio")
                            },
                            "reasons": tf_ta.get("reasons", [])[:5],
                        }
                except Exception as e:
                    logger.debug(f"[Orchestrator] Multi-TF {tf} for {symbol}: {e}")
            if multi_tf_data:
                context["multi_timeframe"] = multi_tf_data

            decisions = []

            if use_custom_agents:
                # ── Custom agents path ──
                from app.agents.custom_agents import custom_market_analyst, custom_position_reviewer
                try:
                    market_dec = await custom_market_analyst(db, context, symbol)
                    decisions.append(market_dec)
                    db.add(AgentDecision(
                        agent_id=0,
                        agent_name=market_dec.get("agent_name", "Custom Market Analyst"),
                        agent_role="market_analyst",
                        symbol=symbol,
                        action=market_dec.get("action", "neutral"),
                        confidence=market_dec.get("confidence", 0),
                        reasoning=market_dec.get("reasoning", ""),
                        market_data=json.dumps({"position_review": True, **market_dec}, default=str),
                        session_id=session_id,
                        ai_called=False,
                        memory_context_used=0,
                    ))
                except Exception as e:
                    logger.warning(f"[Orchestrator] Custom Market Analyst error for {symbol}: {e}")

                try:
                    review = await custom_position_reviewer(
                        db, context, symbol, exchange_pos or {"holdSide": hold_side, "openPriceAvg": entry_price}
                    )
                    decisions.append(review)
                    db.add(AgentDecision(
                        agent_id=0,
                        agent_name=review.get("agent_name", "Custom Position Reviewer"),
                        agent_role="position_reviewer",
                        symbol=symbol,
                        action=review.get("action", "hold"),
                        confidence=review.get("confidence", 0),
                        reasoning=review.get("reasoning", ""),
                        market_data=json.dumps({"position_review": True, **review}, default=str),
                        session_id=session_id,
                        ai_called=False,
                        memory_context_used=0,
                    ))
                except Exception as e:
                    logger.warning(f"[Orchestrator] Custom Position Reviewer error for {symbol}: {e}")
            else:
                # ── AI agents path ──
                # Market Analyst for current conditions
                for agent in agents_by_role.get("market_analyst", [])[:1]:
                    try:
                        decision = await AgentOrchestrator._run_agent_with_memory(
                            db, agent, context, symbol
                        )
                        decisions.append(decision)
                        db.add(AgentDecision(
                            agent_id=agent.agent_id,
                            agent_name=agent.name,
                            agent_role=agent.role,
                            symbol=symbol,
                            action=decision.get("action", "neutral"),
                            confidence=decision.get("confidence", 0),
                            reasoning=decision.get("reasoning", ""),
                            market_data=json.dumps({"position_review": True, **decision}, default=str),
                            session_id=session_id,
                            ai_called=decision.get("ai_called", True),
                            memory_context_used=decision.get("memory_context_used", 0),
                        ))
                    except Exception as e:
                        logger.warning(f"[Orchestrator] Market Analyst error for {symbol}: {e}")

                # Position Reviewer
                review_context = {**context, "market_analysis": decisions[0] if decisions else {}}
                for agent in agents_by_role.get("position_reviewer", [])[:1]:
                    try:
                        review = await AgentOrchestrator._run_agent_with_memory(
                            db, agent, review_context, symbol
                        )
                        decisions.append(review)
                        db.add(AgentDecision(
                            agent_id=agent.agent_id,
                            agent_name=agent.name,
                            agent_role=agent.role,
                            symbol=symbol,
                            action=review.get("action", "hold"),
                            confidence=review.get("confidence", 0),
                            reasoning=review.get("reasoning", ""),
                            market_data=json.dumps({"position_review": True, **review}, default=str),
                            session_id=session_id,
                            ai_called=review.get("ai_called", True),
                            memory_context_used=review.get("memory_context_used", 0),
                        ))
                    except Exception as e:
                        logger.warning(f"[Orchestrator] Position Reviewer error for {symbol}: {e}")

            # Process position review decision
            review_decision = decisions[-1] if decisions else {}
            review_action = review_decision.get("action", "hold")

            review_entry = {
                "trade_id": trade.id,
                "symbol": symbol,
                "side": trade.side,
                "hold_duration_hours": round(hold_duration_hours, 1),
                "pnl_pct": round(pnl_pct, 2),
                "unrealized_pnl": round(unrealized_pnl, 4),
                "review_action": review_action,
                "confidence": review_decision.get("confidence", 0),
                "reasoning": review_decision.get("reasoning", ""),
                "urgency": review_decision.get("urgency", "low"),
                "adjusted_sl": review_decision.get("adjusted_sl"),
                "adjusted_tp": review_decision.get("adjusted_tp"),
                "partial_close_pct": review_decision.get("partial_close_pct"),
                "decisions": decisions,
                "session_id": session_id,
            }
            reviews.append(review_entry)

            # If action is close, create a close signal (any urgency — AI decided to close)
            if review_action == "close":
                close_action = SignalAction.SELL if trade.side == "buy" else SignalAction.BUY
                # Ensure close signals always pass the 0.3 confidence threshold
                close_confidence = max(review_decision.get("confidence", 0.5), 0.5)
                close_sig = Signal(
                    source=SignalSource.SYSTEM.value,
                    symbol=symbol,
                    action=close_action.value,
                    price=current_price,
                    timeframe="1h",
                    strength=close_confidence,
                    confidence=close_confidence,
                    status=SignalStatus.PENDING.value,
                    raw_data=json.dumps({
                        "source": "position_review",
                        "session_id": session_id,
                        "trade_id": trade.id,
                        "review_action": review_action,
                        "urgency": review_decision.get("urgency", "low"),
                        "reasoning": review_decision.get("reasoning", ""),
                        "hold_duration_hours": round(hold_duration_hours, 1),
                        "pnl_pct": round(pnl_pct, 2),
                    }, default=str),
                )
                db.add(close_sig)
                actions_taken.append({
                    "symbol": symbol,
                    "action": "close_signal_created",
                    "signal_action": close_action.value,
                })
                logger.info(
                    f"[Orchestrator:{session_id}] Position review → CLOSE signal for {symbol} "
                    f"(PnL={pnl_pct:+.1f}%, held {hold_duration_hours:.1f}h)"
                )

            # If action is adjust, update SL/TP on the trade record AND on exchange
            if review_action == "adjust":
                new_sl = review_decision.get("adjusted_sl")
                new_tp = review_decision.get("adjusted_tp")
                if new_sl and new_sl > 0:
                    trade.stop_loss = new_sl
                if new_tp and new_tp > 0:
                    trade.take_profit = new_tp
                actions_taken.append({
                    "symbol": symbol,
                    "action": "sl_tp_adjusted",
                    "new_sl": new_sl,
                    "new_tp": new_tp,
                })
                logger.info(
                    f"[Orchestrator:{session_id}] Position review → ADJUST {symbol} "
                    f"SL={new_sl} TP={new_tp}"
                )
                # Execute SL/TP replacement on exchange
                if connector and (new_sl or new_tp):
                    try:
                        replace_result = await connector.replace_tpsl_orders(
                            symbol=symbol,
                            hold_side=hold_side,
                            new_sl=float(new_sl) if new_sl else None,
                            new_tp=float(new_tp) if new_tp else None,
                        )
                        actions_taken.append({
                            "symbol": symbol,
                            "action": "exchange_tpsl_replaced",
                            "cancelled": len(replace_result.get("cancelled", [])),
                            "placed": len(replace_result.get("placed", [])),
                        })
                        logger.info(
                            f"[Orchestrator:{session_id}] Exchange TPSL replaced for {symbol}: "
                            f"{replace_result}"
                        )
                    except Exception as e:
                        logger.error(
                            f"[Orchestrator:{session_id}] Failed to replace TPSL on exchange "
                            f"for {symbol}: {e}"
                        )

        await db.commit()

        logger.info(
            f"[Orchestrator:{session_id}] Position analysis complete: "
            f"{len(reviews)} reviewed, {len(actions_taken)} actions taken"
        )

        return {
            "skipped": False,
            "session_id": session_id,
            "positions_reviewed": len(reviews),
            "actions_taken": actions_taken,
            "reviews": reviews,
        }

    # ── Sim Position Analysis ───────────────────────────────────

    @staticmethod
    async def analyze_sim_positions(
        db: AsyncSession,
        min_hold_hours: float = 0.016,
    ) -> Dict[str, Any]:
        """
        Analyze open simulation positions using AI agents.
        Only runs if the sim account has enable_ai=True.
        """
        if not settings.ENABLE_AI_AGENTS:
            return {"skipped": True, "reason": "AI agents disabled"}

        from app.agents.base import _circuit_is_open
        if _circuit_is_open():
            return {"skipped": True, "reason": "AI circuit breaker open"}

        # Check sim account AI setting
        acct_result = await db.execute(select(SimAccount).limit(1))
        sim_account = acct_result.scalar_one_or_none()
        if not sim_account or not getattr(sim_account, "enable_ai", False):
            return {"skipped": True, "reason": "Sim AI disabled"}

        session_id = f"sim-pos-{str(uuid.uuid4())[:8]}"
        logger.info(f"[Orchestrator:{session_id}] Starting sim position analysis")

        from datetime import timedelta
        now = now_sast()
        review_threshold = now - timedelta(hours=min_hold_hours)

        # Fetch open sim positions
        result = await db.execute(
            select(SimPosition).where(SimPosition.status == "open")
        )
        open_positions = result.scalars().all()
        if not open_positions:
            return {"skipped": False, "positions_reviewed": 0, "reason": "No open sim positions"}

        # Filter by last review time
        positions_to_review = []
        for pos in open_positions:
            last_review = await db.execute(
                select(AgentDecision.created_at)
                .where(
                    AgentDecision.symbol == pos.symbol,
                    AgentDecision.agent_role == "position_reviewer",
                    AgentDecision.session_id.like("sim-pos-%"),
                )
                .order_by(AgentDecision.created_at.desc())
                .limit(1)
            )
            last_review_time = last_review.scalar_one_or_none()
            reference_time = last_review_time or pos.created_at
            if reference_time and reference_time < review_threshold:
                positions_to_review.append(pos)

        if not positions_to_review:
            return {"skipped": False, "positions_reviewed": 0, "reason": "All reviewed recently"}

        # Load agents
        agent_result = await db.execute(select(Agent).where(Agent.is_active == True))
        agents_by_role: Dict[str, Any] = {}
        for row in agent_result.scalars().all():
            agents_by_role.setdefault(row.role, []).append(agent_from_db(row))

        # Fetch current prices from exchange
        connector = exchange_manager.get_exchange(SupportedExchange.BITGET)

        reviews = []
        actions_taken = []

        for pos in positions_to_review:
            symbol = pos.symbol
            current_price = pos.current_price or 0.0

            # Try to get live price
            if connector:
                try:
                    ticker = await connector.get_ticker(symbol)
                    current_price = float(ticker.get("last", 0) or 0) or current_price
                except Exception:
                    pass

            entry_price = pos.entry_price or 0
            hold_hours = (now - pos.created_at).total_seconds() / 3600 if pos.created_at else 0
            leverage = pos.leverage or 1

            pnl_pct = 0.0
            if entry_price > 0 and current_price > 0:
                if pos.side == "long":
                    pnl_pct = ((current_price - entry_price) / entry_price) * 100 * leverage
                else:
                    pnl_pct = ((entry_price - current_price) / entry_price) * 100 * leverage

            position_context = {
                "symbol": symbol,
                "side": "buy" if pos.side == "long" else "sell",
                "hold_side": pos.side,
                "entry_price": entry_price,
                "current_price": current_price,
                "pnl_pct": round(pnl_pct, 2),
                "hold_duration_hours": round(hold_hours, 1),
                "stop_loss": pos.stop_loss,
                "take_profit": pos.take_profit,
                "leverage": leverage,
                "amount": pos.amount,
                "mode": "simulation",
            }

            context = await AgentOrchestrator._gather_context(symbol, "1h", db=db)
            context["position"] = position_context

            # Add multi-TF analysis for reversal detection (sim)
            sim_multi_tf = {}
            for tf in ("5m", "15m", "4h"):
                try:
                    tf_ohlcv = await connector.get_ohlcv(symbol=symbol, timeframe=tf, limit=100) if connector else None
                    if tf_ohlcv and len(tf_ohlcv) >= 20:
                        tf_ta = technical_analyze(tf_ohlcv, tf)
                        sim_multi_tf[tf] = {
                            "score": tf_ta.get("score"),
                            "action": tf_ta.get("action"),
                            "indicators": {
                                k: tf_ta.get("indicators", {}).get(k)
                                for k in ("rsi", "macd_histogram", "macd", "macd_signal", "adx", "plus_di", "minus_di", "ema50", "ema200", "stoch_rsi", "price", "bb_pct_b", "bb_upper", "bb_lower", "volume_ratio", "buy_ratio")
                            },
                            "reasons": tf_ta.get("reasons", [])[:5],
                        }
                except Exception as e:
                    logger.debug(f"[Orchestrator] Sim Multi-TF {tf} for {symbol}: {e}")
            if sim_multi_tf:
                context["multi_timeframe"] = sim_multi_tf

            decisions = []

            # Market Analyst
            for agent in agents_by_role.get("market_analyst", [])[:1]:
                try:
                    decision = await AgentOrchestrator._run_agent_with_memory(db, agent, context, symbol)
                    decisions.append(decision)
                    db.add(AgentDecision(
                        agent_id=agent.agent_id, agent_name=agent.name, agent_role=agent.role,
                        symbol=symbol, action=decision.get("action", "neutral"),
                        confidence=decision.get("confidence", 0),
                        reasoning=decision.get("reasoning", ""),
                        market_data=json.dumps({"sim_position_review": True, **decision}, default=str),
                        session_id=session_id,
                        ai_called=decision.get("ai_called", True),
                        memory_context_used=decision.get("memory_context_used", 0),
                    ))
                except Exception as e:
                    logger.warning(f"[Orchestrator] Sim Market Analyst error for {symbol}: {e}")

            # Position Reviewer
            review_context = {**context, "market_analysis": decisions[0] if decisions else {}}
            for agent in agents_by_role.get("position_reviewer", [])[:1]:
                try:
                    review = await AgentOrchestrator._run_agent_with_memory(db, agent, review_context, symbol)
                    decisions.append(review)
                    db.add(AgentDecision(
                        agent_id=agent.agent_id, agent_name=agent.name, agent_role=agent.role,
                        symbol=symbol, action=review.get("action", "hold"),
                        confidence=review.get("confidence", 0),
                        reasoning=review.get("reasoning", ""),
                        market_data=json.dumps({"sim_position_review": True, **review}, default=str),
                        session_id=session_id,
                        ai_called=review.get("ai_called", True),
                        memory_context_used=review.get("memory_context_used", 0),
                    ))
                except Exception as e:
                    logger.warning(f"[Orchestrator] Sim Position Reviewer error for {symbol}: {e}")

            review_decision = decisions[-1] if decisions else {}
            review_action = review_decision.get("action", "hold")

            reviews.append({
                "position_id": pos.id,
                "symbol": symbol,
                "side": pos.side,
                "hold_duration_hours": round(hold_hours, 1),
                "pnl_pct": round(pnl_pct, 2),
                "review_action": review_action,
                "confidence": review_decision.get("confidence", 0),
                "reasoning": review_decision.get("reasoning", ""),
            })

            # CLOSE: create close signal for sim
            if review_action == "close" and review_decision.get("urgency") in ("high", "medium"):
                close_action = SignalAction.SELL if pos.side == "long" else SignalAction.BUY
                db.add(Signal(
                    source=SignalSource.SYSTEM.value, symbol=symbol,
                    action=close_action.value, price=current_price,
                    timeframe="1h",
                    strength=review_decision.get("confidence", 0.5),
                    confidence=review_decision.get("confidence", 0.5),
                    status=SignalStatus.PENDING.value,
                    raw_data=json.dumps({
                        "source": "sim_position_review", "session_id": session_id,
                        "position_id": pos.id, "review_action": review_action,
                        "reasoning": review_decision.get("reasoning", ""),
                        "pnl_pct": round(pnl_pct, 2),
                    }, default=str),
                ))
                actions_taken.append({"symbol": symbol, "action": "sim_close_signal_created"})
                logger.info(
                    f"[Orchestrator:{session_id}] Sim position → CLOSE {symbol} "
                    f"(PnL={pnl_pct:+.1f}%, held {hold_hours:.1f}h)"
                )

            # ADJUST: update sim position SL/TP
            if review_action == "adjust":
                new_sl = review_decision.get("adjusted_sl")
                new_tp = review_decision.get("adjusted_tp")
                if new_sl and new_sl > 0:
                    pos.stop_loss = new_sl
                if new_tp and new_tp > 0:
                    pos.take_profit = new_tp
                actions_taken.append({"symbol": symbol, "action": "sim_sl_tp_adjusted", "new_sl": new_sl, "new_tp": new_tp})
                logger.info(f"[Orchestrator:{session_id}] Sim position → ADJUST {symbol} SL={new_sl} TP={new_tp}")

        await db.commit()
        logger.info(f"[Orchestrator:{session_id}] Sim position analysis: {len(reviews)} reviewed, {len(actions_taken)} actions")

        return {
            "skipped": False, "session_id": session_id,
            "positions_reviewed": len(reviews),
            "actions_taken": actions_taken, "reviews": reviews,
        }

    # ── Limit Order Optimization ────────────────────────────────

    @staticmethod
    async def analyze_limit_orders(
        db: AsyncSession,
        min_age_minutes: float = 5.0,
    ) -> Dict[str, Any]:
        """
        Analyze pending limit orders and recommend better entry prices
        based on current market conditions & TA.

        Uses Market Analyst to evaluate whether the current limit price
        is still optimal or if a better entry is available.
        Returns list of order reviews with KEEP/ADJUST/CANCEL decisions.
        """
        if not settings.ENABLE_AI_AGENTS:
            return {"skipped": True, "reason": "AI agents disabled"}

        from app.agents.base import _circuit_is_open
        if _circuit_is_open():
            return {"skipped": True, "reason": "AI circuit breaker open"}

        session_id = f"lo-{str(uuid.uuid4())[:8]}"
        logger.info(f"[Orchestrator:{session_id}] Starting limit order optimization")

        from app.exchanges.bitget import BitgetConnector
        from typing import cast
        connector = cast(
            Optional[BitgetConnector],
            exchange_manager.get_exchange(SupportedExchange.BITGET),
        )
        if not connector:
            return {"skipped": True, "reason": "Bitget connector not available"}

        # Fetch all pending limit orders
        try:
            open_orders = await connector.get_futures_open_orders()
        except Exception as e:
            logger.error(f"[Orchestrator:{session_id}] Failed to fetch open orders: {e}")
            return {"skipped": True, "reason": str(e)}

        if not open_orders:
            return {"skipped": False, "orders_reviewed": 0, "reason": "No pending limit orders"}

        # Filter to limit orders only (not market), and respect minimum age
        now = now_sast()
        limit_orders = []
        for order in open_orders:
            order_type = (order.get("orderType") or "").lower()
            if order_type != "limit":
                continue
            create_ts = order.get("cTime") or order.get("createTime")
            if create_ts:
                try:
                    from datetime import datetime, timezone, timedelta
                    created = datetime.fromtimestamp(int(create_ts) / 1000, tz=timezone(timedelta(hours=2)))
                    age_min = (now - created).total_seconds() / 60
                    if age_min < min_age_minutes:
                        continue
                except Exception:
                    pass
            limit_orders.append(order)

        if not limit_orders:
            return {"skipped": False, "orders_reviewed": 0, "reason": "No limit orders old enough to review"}

        # Load market_analyst agent
        agent_result = await db.execute(select(Agent).where(Agent.is_active == True, Agent.role == "market_analyst"))
        analyst_agents = agent_result.scalars().all()
        if not analyst_agents:
            return {"skipped": True, "reason": "No active market_analyst agent"}

        analyst = agent_from_db(analyst_agents[0])

        # Ensure precision cache is loaded
        await connector.get_max_leverage("BTCUSDT")

        reviews = []
        adjustments = []

        for order in limit_orders:
            bitget_sym = (order.get("symbol") or "").upper()
            display_sym = bitget_sym.replace("USDT", "/USDT")
            order_id = order.get("orderId", "")
            side = (order.get("side") or "").lower()
            order_price = float(order.get("price") or 0)
            size = order.get("size") or order.get("baseVolume") or "0"
            sl_price = order.get("presetStopLossPrice") or order.get("stopLoss")
            tp_price = order.get("presetStopSurplusPrice") or order.get("takeProfit")

            if not bitget_sym or order_price <= 0:
                continue

            # Gather market context
            try:
                context = await AgentOrchestrator._gather_context(display_sym, "1h")
            except Exception as e:
                logger.warning(f"[Orchestrator:{session_id}] Context failed for {display_sym}: {e}")
                continue

            current_price = context.get("current_price", 0)
            if current_price <= 0:
                continue

            # Add multi-timeframe context for better entry analysis
            multi_tf = {}
            for tf in ("5m", "15m"):
                try:
                    tf_ohlcv = await connector.get_ohlcv(symbol=display_sym, timeframe=tf, limit=100)
                    if tf_ohlcv and len(tf_ohlcv) >= 20:
                        tf_ta = technical_analyze(tf_ohlcv, tf)
                        multi_tf[tf] = {
                            "score": tf_ta.get("score"),
                            "action": tf_ta.get("action"),
                            "indicators": {
                                k: tf_ta.get("indicators", {}).get(k)
                                for k in ("rsi", "macd_histogram", "bb_lower", "bb_upper", "bb_middle",
                                           "ema50", "ema200", "adx", "stoch_rsi", "volume_ratio",
                                           "ma5", "ma10", "price")
                            },
                        }
                except Exception:
                    pass

            # Build order-specific context for AI
            order_context = {
                **context,
                "multi_timeframe": multi_tf,
                "pending_order": {
                    "order_id": order_id,
                    "symbol": display_sym,
                    "side": side,
                    "order_price": order_price,
                    "current_market_price": current_price,
                    "size": size,
                    "price_distance_pct": round(
                        ((order_price - current_price) / current_price) * 100, 3
                    ),
                    "stop_loss": float(sl_price) if sl_price and float(sl_price) > 0 else None,
                    "take_profit": float(tp_price) if tp_price and float(tp_price) > 0 else None,
                },
                "task": (
                    "Analyze this PENDING LIMIT ORDER and decide if the entry price should be adjusted "
                    "for a better fill based on current market conditions. Consider: support/resistance levels, "
                    "Bollinger Bands, recent price action, volume, momentum indicators. "
                    "If market has moved significantly since the order was placed, recommend a new price. "
                    "Respond with JSON: {\"action\": \"keep|adjust|cancel\", \"confidence\": 0.0-1.0, "
                    "\"new_price\": <float or null>, \"reasoning\": \"...\"}"
                ),
            }

            try:
                decision = await AgentOrchestrator._run_agent_with_memory(
                    db, analyst, order_context, display_sym,
                )
                db.add(AgentDecision(
                    agent_id=analyst.agent_id,
                    agent_name=analyst.name,
                    agent_role=analyst.role,
                    symbol=display_sym,
                    action=decision.get("action", "keep"),
                    confidence=decision.get("confidence", 0),
                    reasoning=decision.get("reasoning", ""),
                    market_data=json.dumps({"limit_order_review": True, "order_id": order_id, **decision}, default=str),
                    session_id=session_id,
                    ai_called=decision.get("ai_called", True),
                    memory_context_used=decision.get("memory_context_used", 0),
                ))
            except Exception as e:
                logger.warning(f"[Orchestrator:{session_id}] AI analysis failed for {display_sym} order {order_id}: {e}")
                continue

            action = decision.get("action", "keep")
            new_price = decision.get("new_price")
            confidence = decision.get("confidence", 0)

            review_entry = {
                "order_id": order_id,
                "symbol": display_sym,
                "side": side,
                "current_price": current_price,
                "order_price": order_price,
                "action": action,
                "new_price": new_price,
                "confidence": confidence,
                "reasoning": decision.get("reasoning", ""),
            }
            reviews.append(review_entry)

            # Execute adjustment if AI recommends it with sufficient confidence
            if action == "adjust" and new_price and confidence >= 0.5:
                try:
                    adjusted_price = float(new_price)

                    # Recalculate SL/TP for the new entry price
                    new_sl: Optional[float] = None
                    new_tp: Optional[float] = None
                    try:
                        from app.trading.simulation import SmartStopLoss
                        ohlcv = await connector.get_ohlcv(symbol=display_sym, timeframe="1h", limit=200)
                        hold_side = "long" if side == "buy" else "short"
                        sl_data = SmartStopLoss.calculate(ohlcv, hold_side, adjusted_price)
                        new_sl = sl_data.get("stop_loss")
                        new_tp = sl_data.get("take_profit")
                        logger.info(
                            f"[Orchestrator:{session_id}] Recalculated SL/TP for {display_sym} "
                            f"@ {adjusted_price}: SL={new_sl} TP={new_tp}"
                        )
                    except Exception as sltp_err:
                        logger.warning(
                            f"[Orchestrator:{session_id}] SL/TP recalc failed for {display_sym}, "
                            f"keeping existing: {sltp_err}"
                        )
                        new_sl = float(sl_price) if sl_price and float(sl_price) > 0 else None
                        new_tp = float(tp_price) if tp_price and float(tp_price) > 0 else None

                    result = await connector.modify_limit_order_price(
                        symbol=bitget_sym,
                        order_id=order_id,
                        new_price=adjusted_price,
                        size=str(size),
                        stop_loss=new_sl,
                        take_profit=new_tp,
                    )

                    new_order_id = result.get("orderId") or result.get("order_id") or order_id

                    # Update the Trade DB record with new price + order ID + SL/TP
                    try:
                        trade_result = await db.execute(
                            select(Trade).where(
                                Trade.exchange_order_id == order_id,
                                Trade.status == "open",
                            )
                        )
                        trade_record = trade_result.scalars().first()
                        if trade_record:
                            trade_record.price = adjusted_price
                            trade_record.exchange_order_id = str(new_order_id)
                            if new_sl:
                                trade_record.stop_loss = new_sl
                            if new_tp:
                                trade_record.take_profit = new_tp
                            logger.info(
                                f"[Orchestrator:{session_id}] Trade DB updated: "
                                f"{display_sym} price={adjusted_price} "
                                f"orderId={order_id}→{new_order_id} SL={new_sl} TP={new_tp}"
                            )
                        else:
                            logger.warning(
                                f"[Orchestrator:{session_id}] No Trade record found for "
                                f"order {order_id} — DB not updated"
                            )
                    except Exception as db_err:
                        logger.error(
                            f"[Orchestrator:{session_id}] Failed to update Trade DB "
                            f"for {order_id}: {db_err}"
                        )

                    adjustments.append({
                        "order_id": order_id,
                        "new_order_id": new_order_id,
                        "symbol": display_sym,
                        "old_price": order_price,
                        "new_price": adjusted_price,
                        "new_sl": new_sl,
                        "new_tp": new_tp,
                        "result": result,
                    })
                    logger.info(
                        f"[Orchestrator:{session_id}] Limit order adjusted: {display_sym} "
                        f"{order_price} → {adjusted_price} (conf={confidence:.2f})"
                    )
                except Exception as e:
                    logger.error(
                        f"[Orchestrator:{session_id}] Failed to modify order {order_id}: {e}"
                    )
                    review_entry["error"] = str(e)

            elif action == "cancel" and confidence >= 0.7:
                try:
                    await connector.cancel_futures_order(
                        symbol=bitget_sym,
                        margin_coin="USDT",
                        order_id=order_id,
                    )

                    # Mark Trade DB record as canceled
                    try:
                        trade_result = await db.execute(
                            select(Trade).where(
                                Trade.exchange_order_id == order_id,
                                Trade.status == "open",
                            )
                        )
                        trade_record = trade_result.scalars().first()
                        if trade_record:
                            trade_record.status = "canceled"
                            trade_record.closed_at = now
                            logger.info(
                                f"[Orchestrator:{session_id}] Trade DB marked canceled: "
                                f"{display_sym} order {order_id}"
                            )
                    except Exception as db_err:
                        logger.error(
                            f"[Orchestrator:{session_id}] Failed to cancel Trade DB "
                            f"for {order_id}: {db_err}"
                        )

                    adjustments.append({
                        "order_id": order_id,
                        "symbol": display_sym,
                        "action": "cancelled",
                    })
                    logger.info(
                        f"[Orchestrator:{session_id}] Limit order cancelled: {display_sym} "
                        f"@ {order_price} (conf={confidence:.2f})"
                    )
                except Exception as e:
                    logger.error(f"[Orchestrator:{session_id}] Failed to cancel order {order_id}: {e}")

        await db.commit()

        logger.info(
            f"[Orchestrator:{session_id}] Limit order optimization: "
            f"{len(reviews)} reviewed, {len(adjustments)} adjusted"
        )

        return {
            "skipped": False,
            "session_id": session_id,
            "orders_reviewed": len(reviews),
            "orders_adjusted": len(adjustments),
            "reviews": reviews,
            "adjustments": adjustments,
        }

    # ── Open-position SL/TP AI recalculation ──

    @staticmethod
    async def analyze_open_positions(
        db: AsyncSession,
        min_age_minutes: float = 10.0,
    ) -> Dict[str, Any]:
        """
        AI-powered SL/TP recalculation for open (filled) positions.

        Uses the Market Analyst to evaluate whether the current SL/TP levels
        are still optimal given changing market conditions. If conditions have
        shifted, it recalculates smart SL/TP and replaces them on the exchange.
        """
        if not settings.ENABLE_AI_AGENTS:
            return {"skipped": True, "reason": "AI agents disabled"}

        from app.agents.base import _circuit_is_open
        if _circuit_is_open():
            return {"skipped": True, "reason": "AI circuit breaker open"}

        session_id = f"pos-{str(uuid.uuid4())[:8]}"
        logger.info(f"[Orchestrator:{session_id}] Starting open position SL/TP optimization")

        from app.exchanges.bitget import BitgetConnector
        from app.trading.simulation import SmartStopLoss
        from typing import cast

        connector = cast(
            Optional[BitgetConnector],
            exchange_manager.get_exchange(SupportedExchange.BITGET),
        )
        if not connector:
            return {"skipped": True, "reason": "Bitget connector not available"}

        # Fetch open positions
        try:
            pos_data = await connector.get_futures_positions()
        except Exception as e:
            logger.error(f"[Orchestrator:{session_id}] Failed to fetch positions: {e}")
            return {"skipped": True, "reason": str(e)}

        open_positions = [
            p for p in (pos_data or [])
            if float(p.get("total") or p.get("available") or 0) > 0
        ]
        if not open_positions:
            return {"skipped": False, "positions_reviewed": 0, "reason": "No open positions"}

        # Filter by age — only review positions older than min_age_minutes
        now = now_sast()
        eligible = []
        for pos in open_positions:
            create_ts = pos.get("cTime") or pos.get("createTime")
            if create_ts:
                try:
                    from datetime import datetime, timezone, timedelta
                    created = datetime.fromtimestamp(int(create_ts) / 1000, tz=timezone(timedelta(hours=2)))
                    age_min = (now - created).total_seconds() / 60
                    if age_min < min_age_minutes:
                        continue
                except Exception:
                    pass
            eligible.append(pos)

        if not eligible:
            return {"skipped": False, "positions_reviewed": 0, "reason": "No positions old enough to review"}

        # Load market analyst
        agent_result = await db.execute(
            select(Agent).where(Agent.is_active == True, Agent.role == "market_analyst")
        )
        analyst_agents = agent_result.scalars().all()
        if not analyst_agents:
            return {"skipped": True, "reason": "No active market_analyst agent"}

        analyst = agent_from_db(analyst_agents[0])
        await connector.get_max_leverage("BTCUSDT")  # Warm precision cache

        # Fetch current TPSL orders once — indexed by (symbol, holdSide)
        try:
            all_tpsl = await connector.get_pending_tpsl_orders()
        except Exception:
            all_tpsl = []
        tpsl_map: Dict[tuple, Dict[str, Any]] = {}
        for order in (all_tpsl or []):
            sym = (order.get("symbol") or "").upper()
            hs = (order.get("posSide") or order.get("holdSide") or "").lower()
            pt = (order.get("planType") or "").lower()
            trigger = float(order.get("triggerPrice") or 0)
            key = (sym, hs)
            if key not in tpsl_map:
                tpsl_map[key] = {}
            if pt in ("loss_plan", "pos_loss") and trigger > 0:
                tpsl_map[key]["sl"] = trigger
            elif pt in ("profit_plan", "pos_profit") and trigger > 0:
                tpsl_map[key]["tp"] = trigger

        reviews: List[Dict[str, Any]] = []
        adjustments: List[Dict[str, Any]] = []

        for pos in eligible:
            bitget_sym = (pos.get("symbol") or "").upper()
            display_sym = bitget_sym.replace("USDT", "/USDT")
            hold_side = (pos.get("holdSide") or "").lower()
            entry_price = float(pos.get("openPriceAvg") or 0)
            size = float(pos.get("total") or pos.get("available") or 0)

            if not bitget_sym or entry_price <= 0 or size <= 0:
                continue

            current_sl = tpsl_map.get((bitget_sym, hold_side), {}).get("sl")
            current_tp = tpsl_map.get((bitget_sym, hold_side), {}).get("tp")

            # Also check Trade DB for SL/TP if not found on exchange
            if not current_sl or not current_tp:
                try:
                    side_val = "buy" if hold_side == "long" else "sell"
                    tr = await db.execute(
                        select(Trade).where(
                            Trade.exchange == "bitget",
                            Trade.status == "open",
                            Trade.trade_side == "open",
                            Trade.symbol == display_sym,
                            Trade.side == side_val,
                        ).order_by(Trade.created_at.desc())
                    )
                    trade_rec = tr.scalars().first()
                    if trade_rec:
                        if not current_sl and trade_rec.stop_loss:
                            current_sl = trade_rec.stop_loss
                        if not current_tp and trade_rec.take_profit:
                            current_tp = trade_rec.take_profit
                except Exception:
                    pass

            # Gather market context
            try:
                context = await AgentOrchestrator._gather_context(display_sym, "1h")
            except Exception as e:
                logger.warning(f"[Orchestrator:{session_id}] Context failed for {display_sym}: {e}")
                continue

            current_price = context.get("current_price", 0)
            if current_price <= 0:
                continue

            # Multi-timeframe context
            multi_tf: Dict[str, Any] = {}
            for tf in ("5m", "15m"):
                try:
                    tf_ohlcv = await connector.get_ohlcv(symbol=display_sym, timeframe=tf, limit=100)
                    if tf_ohlcv and len(tf_ohlcv) >= 20:
                        tf_ta = technical_analyze(tf_ohlcv, tf)
                        multi_tf[tf] = {
                            "score": tf_ta.get("score"),
                            "action": tf_ta.get("action"),
                            "indicators": {
                                k: tf_ta.get("indicators", {}).get(k)
                                for k in ("rsi", "macd_histogram", "bb_lower", "bb_upper", "bb_middle",
                                           "ema50", "ema200", "adx", "stoch_rsi", "volume_ratio",
                                           "ma5", "ma10", "price")
                            },
                        }
                except Exception:
                    pass

            # Calculate what SmartStopLoss would suggest NOW
            smart_sl: Optional[float] = None
            smart_tp: Optional[float] = None
            try:
                ohlcv = await connector.get_ohlcv(symbol=display_sym, timeframe="1h", limit=200)
                sl_data = SmartStopLoss.calculate(ohlcv, hold_side, entry_price)
                smart_sl = sl_data.get("stop_loss")
                smart_tp = sl_data.get("take_profit")
            except Exception:
                pass

            # Calculate distance between current and suggested SL/TP
            sl_distance_pct = None
            tp_distance_pct = None
            if current_sl and smart_sl and current_sl > 0:
                sl_distance_pct = round(((smart_sl - current_sl) / current_sl) * 100, 3)
            if current_tp and smart_tp and current_tp > 0:
                tp_distance_pct = round(((smart_tp - current_tp) / current_tp) * 100, 3)

            position_context = {
                **context,
                "multi_timeframe": multi_tf,
                "open_position": {
                    "symbol": display_sym,
                    "side": hold_side,
                    "entry_price": entry_price,
                    "current_price": current_price,
                    "size": size,
                    "unrealized_pnl": float(pos.get("unrealizedPL") or 0),
                    "leverage": pos.get("leverage"),
                    "current_sl": current_sl,
                    "current_tp": current_tp,
                    "suggested_sl": smart_sl,
                    "suggested_tp": smart_tp,
                    "sl_distance_pct": sl_distance_pct,
                    "tp_distance_pct": tp_distance_pct,
                    "pnl_pct": round(
                        ((current_price - entry_price) / entry_price) * 100
                        if hold_side == "long" else
                        ((entry_price - current_price) / entry_price) * 100,
                        3,
                    ) if entry_price > 0 else 0,
                },
                "task": (
                    "Analyze this OPEN POSITION and decide if the SL/TP levels should be adjusted "
                    "based on current market conditions. Compare current SL/TP vs the suggested smart levels. "
                    "Consider: support/resistance shifts, Bollinger Bands, momentum changes, ATR-based levels, "
                    "whether the trade is in profit/loss, and multi-timeframe alignment. "
                    "Respond with JSON: {\"action\": \"keep|adjust\", \"confidence\": 0.0-1.0, "
                    "\"new_sl\": <float or null>, \"new_tp\": <float or null>, \"reasoning\": \"...\"}"
                ),
            }

            try:
                decision = await AgentOrchestrator._run_agent_with_memory(
                    db, analyst, position_context, display_sym,
                )
                db.add(AgentDecision(
                    agent_id=analyst.agent_id,
                    agent_name=analyst.name,
                    agent_role=analyst.role,
                    symbol=display_sym,
                    action=decision.get("action", "keep"),
                    confidence=decision.get("confidence", 0),
                    reasoning=decision.get("reasoning", ""),
                    market_data=json.dumps({
                        "position_sltp_review": True,
                        "hold_side": hold_side,
                        **decision,
                    }, default=str),
                    session_id=session_id,
                    ai_called=decision.get("ai_called", True),
                    memory_context_used=decision.get("memory_context_used", 0),
                ))
            except Exception as e:
                logger.warning(
                    f"[Orchestrator:{session_id}] AI position analysis failed for "
                    f"{display_sym} {hold_side}: {e}"
                )
                continue

            action = decision.get("action", "keep")
            new_sl_val = decision.get("new_sl")
            new_tp_val = decision.get("new_tp")
            confidence = decision.get("confidence", 0)

            review_entry: Dict[str, Any] = {
                "symbol": display_sym,
                "side": hold_side,
                "entry_price": entry_price,
                "current_price": current_price,
                "current_sl": current_sl,
                "current_tp": current_tp,
                "suggested_sl": smart_sl,
                "suggested_tp": smart_tp,
                "action": action,
                "ai_new_sl": new_sl_val,
                "ai_new_tp": new_tp_val,
                "confidence": confidence,
                "reasoning": decision.get("reasoning", ""),
            }
            reviews.append(review_entry)

            if action == "adjust" and confidence >= 0.5:
                # Use AI-provided values, fall back to smart calculation
                final_sl = float(new_sl_val) if new_sl_val else smart_sl
                final_tp = float(new_tp_val) if new_tp_val else smart_tp

                # Sanity check: SL must be on the correct side of entry
                if final_sl:
                    if hold_side == "long" and final_sl >= entry_price:
                        final_sl = smart_sl   # Fallback
                    elif hold_side == "short" and final_sl <= entry_price:
                        final_sl = smart_sl

                if not final_sl and not final_tp:
                    logger.info(
                        f"[Orchestrator:{session_id}] No valid new SL/TP for "
                        f"{display_sym} {hold_side}, skipping"
                    )
                    continue

                # Check if there's actually a meaningful change
                sl_changed = final_sl and (not current_sl or abs(final_sl - current_sl) / max(current_sl, 1) > 0.002)
                tp_changed = final_tp and (not current_tp or abs(final_tp - current_tp) / max(current_tp, 1) > 0.002)

                if not sl_changed and not tp_changed:
                    logger.info(
                        f"[Orchestrator:{session_id}] SL/TP change for {display_sym} "
                        f"{hold_side} too small (<0.2%), skipping"
                    )
                    continue

                try:
                    result = await connector.replace_tpsl_orders(
                        symbol=bitget_sym,
                        hold_side=hold_side,
                        new_sl=final_sl if sl_changed else None,
                        new_tp=final_tp if tp_changed else None,
                        margin_coin="USDT",
                    )

                    # Update Trade DB records
                    try:
                        side_val = "buy" if hold_side == "long" else "sell"
                        trade_result = await db.execute(
                            select(Trade).where(
                                Trade.exchange == "bitget",
                                Trade.status == "open",
                                Trade.trade_side == "open",
                                Trade.symbol == display_sym,
                                Trade.side == side_val,
                            )
                        )
                        for t in trade_result.scalars().all():
                            if sl_changed and final_sl:
                                t.stop_loss = final_sl
                            if tp_changed and final_tp:
                                t.take_profit = final_tp
                    except Exception as db_err:
                        logger.error(
                            f"[Orchestrator:{session_id}] Failed to update Trade DB "
                            f"for {display_sym} {hold_side}: {db_err}"
                        )

                    adj_entry: Dict[str, Any] = {
                        "symbol": display_sym,
                        "side": hold_side,
                        "result": result,
                    }
                    if sl_changed:
                        adj_entry["old_sl"] = current_sl
                        adj_entry["new_sl"] = final_sl
                    if tp_changed:
                        adj_entry["old_tp"] = current_tp
                        adj_entry["new_tp"] = final_tp
                    adjustments.append(adj_entry)

                    logger.info(
                        f"[Orchestrator:{session_id}] Position SL/TP adjusted: "
                        f"{display_sym} {hold_side} "
                        f"SL={current_sl}→{final_sl if sl_changed else '(same)'} "
                        f"TP={current_tp}→{final_tp if tp_changed else '(same)'} "
                        f"(conf={confidence:.2f})"
                    )
                except Exception as e:
                    logger.error(
                        f"[Orchestrator:{session_id}] Failed to replace TPSL for "
                        f"{display_sym} {hold_side}: {e}"
                    )
                    review_entry["error"] = str(e)

        await db.commit()

        logger.info(
            f"[Orchestrator:{session_id}] Position SL/TP optimization: "
            f"{len(reviews)} reviewed, {len(adjustments)} adjusted"
        )

        return {
            "skipped": False,
            "session_id": session_id,
            "positions_reviewed": len(reviews),
            "positions_adjusted": len(adjustments),
            "reviews": reviews,
            "adjustments": adjustments,
        }
