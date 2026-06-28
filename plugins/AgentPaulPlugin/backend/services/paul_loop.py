"""
Agent Paul Plugin — PAUL Loop Service

Implements the PAUL discipline (Plan → Apply/Qualify → Unify) for trading:

  PLAN    — gather context + an actionable trade plan (AI orchestrator, with a
            local technical heuristic fallback when AI is disabled). Acceptance
            criteria are first-class (BDD Given/When/Then).
  QUALIFY — an independent policy gate re-checks the plan against settings and
            live risk limits before anything executes (Execute/Qualify loop).
  APPLY   — execute per the active authority mode:
              paper            → simulated fill, never touches the exchange
              tradebot_execute → core LiveTradeEngine places the order
              paul_execute     → autonomous execution via the same engine
            When approval is required for live modes, the decision is QUEUED.
  UNIFY   — reconcile planned vs actual, record outcome/PnL, close the loop.

This wraps existing core systems; it never modifies them.
"""
from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_sast
from plugins.AgentPaulPlugin.backend.models import (
    PaulSettings,
    PaulDecision,
    PaulMode,
    PaulProvenance,
    PaulQualify,
    PaulDecisionStatus,
)

# Authority modes that place real orders through the live engine.
_LIVE_MODES = {PaulMode.TRADEBOT_EXECUTE, PaulMode.PAUL_EXECUTE}


class PaulLoop:
    """Stateless orchestration of the PAUL trading loop."""

    # ── Settings ───────────────────────────────────────────

    @staticmethod
    async def get_settings(db: AsyncSession) -> PaulSettings:
        row = (await db.execute(select(PaulSettings).limit(1))).scalar_one_or_none()
        if row is None:
            row = PaulSettings()
            db.add(row)
            await db.commit()
            await db.refresh(row)
        return row

    @staticmethod
    async def update_settings(db: AsyncSession, data: Dict[str, Any]) -> PaulSettings:
        row = await PaulLoop.get_settings(db)
        for key, value in data.items():
            if value is None:
                continue
            if key == "mode":
                try:
                    value = PaulMode(value)
                except ValueError:
                    continue
            setattr(row, key, value)
        await db.commit()
        await db.refresh(row)
        return row

    # ── Status ─────────────────────────────────────────────

    @staticmethod
    async def status(db: AsyncSession) -> Dict[str, Any]:
        s = await PaulLoop.get_settings(db)
        from app.core.config import settings as core_settings

        async def _count(*statuses: PaulDecisionStatus) -> int:
            q = select(func.count(PaulDecision.id)).where(
                PaulDecision.status.in_(list(statuses))
            )
            return int((await db.execute(q)).scalar() or 0)

        queued = await _count(PaulDecisionStatus.QUEUED)
        executed_open = (
            await db.execute(
                select(func.count(PaulDecision.id)).where(
                    PaulDecision.status == PaulDecisionStatus.EXECUTED,
                    PaulDecision.outcome == "open",
                )
            )
        ).scalar() or 0
        total = int((await db.execute(select(func.count(PaulDecision.id)))).scalar() or 0)

        return {
            "enabled": s.enabled,
            "mode": s.mode.value,
            "require_approval": s.require_approval,
            "kill_switch": s.kill_switch,
            "ai_agents_enabled": core_settings.ENABLE_AI_AGENTS,
            "auto_trading_enabled": core_settings.ENABLE_AUTO_TRADING,
            "min_confidence": s.min_confidence,
            "risk_max_position_usdt": s.risk_max_position_usdt,
            "risk_max_open_positions": s.risk_max_open_positions,
            "max_queue_size": s.max_queue_size,
            "cooldown_minutes": s.cooldown_minutes,
            "queued_count": queued,
            "open_executed_count": int(executed_open),
            "total_decisions": total,
        }

    # ── PLAN ───────────────────────────────────────────────

    @staticmethod
    async def _plan(
        db: AsyncSession,
        symbol: str,
        timeframe: str,
        settings_row: PaulSettings,
        market: str = "crypto",
        account_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Produce an actionable trade plan with acceptance criteria."""
        from app.core.config import settings as core_settings
        from app.agents.orchestrator import AgentOrchestrator

        # ── MT5 market — plan via the MT5 SMC engine (XAUUSD, EURUSD, ...) ──
        if market == "mt5":
            acct = account_id or settings_row.mt5_default_account_id
            try:
                from plugins.AgentPaulPlugin.backend.services.mt5_adapter import (
                    plan_mt5,
                    MT5Unavailable,
                )

                plan = await plan_mt5(
                    db,
                    symbol,
                    timeframe,
                    settings_row.mt5_min_rr,
                    acct,
                    use_ai=core_settings.ENABLE_AI_AGENTS,
                )
            except MT5Unavailable as exc:
                plan = _mt5_hold_plan(acct, str(exc))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[Paul] MT5 plan failed for {symbol}: {exc}")
                plan = _mt5_hold_plan(acct, f"MT5 plan error: {exc}")
            plan.setdefault("volume", settings_row.mt5_default_volume)
            plan.setdefault("plan_json", {})
            plan["acceptance_criteria"] = _acceptance(symbol, plan["action"], settings_row)
            return plan

        action = "hold"
        confidence = 0.0
        reasoning = ""
        provenance = PaulProvenance.HEURISTIC
        signal_id: Optional[int] = None
        raw: Dict[str, Any] = {}

        # 1) Preferred path — AI agent orchestration (also persists a core Signal)
        if core_settings.ENABLE_AI_AGENTS:
            try:
                res = await AgentOrchestrator.analyze_symbol(
                    db, symbol, timeframe, trigger="manual"
                )
                raw = res or {}
                if res and not res.get("error") and res.get("final_action"):
                    action = str(res.get("final_action") or "hold").lower()
                    confidence = float(res.get("final_confidence") or 0.0)
                    reasoning = str(res.get("final_reasoning") or "")
                    signal_id = (res.get("signal") or {}).get("id")
                    provenance = PaulProvenance.AI
            except Exception as exc:  # noqa: BLE001 — fall back to heuristic
                logger.warning(f"[Paul] AI plan failed for {symbol}: {exc}")

        # 2) Gather market context for price + heuristic fallback
        ctx: Dict[str, Any] = {}
        try:
            ctx = await AgentOrchestrator._gather_context(symbol, timeframe)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[Paul] context gather failed for {symbol}: {exc}")

        price = float(ctx.get("current_price") or 0.0)
        technical = ctx.get("technical") or {}

        # 3) Heuristic fallback when AI produced nothing actionable
        if provenance == PaulProvenance.HEURISTIC and action == "hold":
            rsi = technical.get("rsi")
            try:
                rsi = float(rsi) if rsi is not None else None
            except (TypeError, ValueError):
                rsi = None
            if rsi is not None and price > 0:
                if rsi <= 30:
                    action, confidence = "buy", 0.62
                    reasoning = f"Heuristic: RSI {rsi:.1f} oversold → long bias."
                elif rsi >= 70:
                    action, confidence = "sell", 0.62
                    reasoning = f"Heuristic: RSI {rsi:.1f} overbought → short bias."
                else:
                    reasoning = f"Heuristic: RSI {rsi:.1f} neutral — no actionable edge."
            else:
                reasoning = reasoning or "No price/indicator data — holding."

        # 4) Derive entry / SL / TP and risk-reward
        entry = price if price > 0 else None
        stop_loss = take_profit = risk_reward = None
        if action in ("buy", "sell") and entry:
            if action == "buy":
                stop_loss = round(entry * 0.98, 8)
                take_profit = round(entry * 1.04, 8)
            else:
                stop_loss = round(entry * 1.02, 8)
                take_profit = round(entry * 0.96, 8)
            risk = abs(entry - stop_loss)
            reward = abs(take_profit - entry)
            risk_reward = round(reward / risk, 2) if risk else None

        acceptance = _acceptance(symbol, action, settings_row)

        return {
            "action": action,
            "confidence": confidence,
            "entry": entry,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "risk_reward": risk_reward,
            "reasoning": reasoning,
            "provenance": provenance,
            "signal_id": signal_id,
            "market": "crypto",
            "account_id": None,
            "volume": None,
            "acceptance_criteria": acceptance,
            "plan_json": {"orchestrator": raw, "ticker": ctx.get("ticker")},
        }

    # ── QUALIFY ────────────────────────────────────────────

    @staticmethod
    async def _qualify(
        db: AsyncSession, settings_row: PaulSettings, plan: Dict[str, Any], symbol: str
    ) -> Tuple[PaulQualify, str]:
        """Independent policy gate. Returns (status, notes)."""
        notes: List[str] = []
        action = plan["action"]

        if action not in ("buy", "sell"):
            return PaulQualify.PASS, "No actionable trade (hold)."

        if settings_row.kill_switch:
            return PaulQualify.BLOCKED, "Kill switch is ON — all execution blocked."

        # Confidence threshold
        if float(plan["confidence"] or 0) < settings_row.min_confidence:
            return (
                PaulQualify.BLOCKED,
                f"Confidence {plan['confidence']:.2f} < min {settings_row.min_confidence:.2f}.",
            )

        # Symbol allowlist
        allow = (settings_row.allowed_symbols or "").strip()
        if allow:
            allowed = {a.strip().upper() for a in allow.split(",") if a.strip()}
            variants = {symbol.upper(), symbol.replace("/", "").upper()}
            if variants.isdisjoint(allowed):
                return PaulQualify.BLOCKED, "Symbol not in allowlist."

        # Queue capacity
        queued = int(
            (
                await db.execute(
                    select(func.count(PaulDecision.id)).where(
                        PaulDecision.status == PaulDecisionStatus.QUEUED
                    )
                )
            ).scalar()
            or 0
        )
        if queued >= settings_row.max_queue_size:
            return PaulQualify.BLOCKED, f"Approval queue full ({queued}/{settings_row.max_queue_size})."

        # Open-position cap (executed + still open)
        open_exec = int(
            (
                await db.execute(
                    select(func.count(PaulDecision.id)).where(
                        PaulDecision.status == PaulDecisionStatus.EXECUTED,
                        PaulDecision.outcome == "open",
                    )
                )
            ).scalar()
            or 0
        )
        if open_exec >= settings_row.risk_max_open_positions:
            return (
                PaulQualify.BLOCKED,
                f"Open positions at cap ({open_exec}/{settings_row.risk_max_open_positions}).",
            )

        # Cooldown per symbol
        if settings_row.cooldown_minutes > 0:
            cutoff = now_sast() - timedelta(minutes=settings_row.cooldown_minutes)
            recent = (
                await db.execute(
                    select(func.count(PaulDecision.id)).where(
                        PaulDecision.symbol == symbol,
                        PaulDecision.status == PaulDecisionStatus.EXECUTED,
                        PaulDecision.created_at >= cutoff,
                    )
                )
            ).scalar() or 0
            if recent:
                return (
                    PaulQualify.BLOCKED,
                    f"Cooldown active — executed within {settings_row.cooldown_minutes}m.",
                )

        # Risk-reward (soft concern)
        rr = plan.get("risk_reward")
        if rr is not None and rr < 1.2:
            notes.append(f"Risk-reward {rr} below 1.2 target.")
            return PaulQualify.CONCERNS, " ".join(notes)

        return PaulQualify.PASS, "All policy checks passed."

    # ── APPLY ──────────────────────────────────────────────

    @staticmethod
    async def _apply(db: AsyncSession, decision: PaulDecision, settings_row: PaulSettings) -> None:
        """Execute the decision according to its authority mode."""
        mode = settings_row.mode

        if mode == PaulMode.PAPER:
            decision.status = PaulDecisionStatus.EXECUTED
            decision.outcome = "open"
            decision.execution_result = {
                "mode": "paper",
                "market": decision.market,
                "filled_price": decision.entry,
                "note": "Simulated paper fill — no exchange/broker order placed.",
            }
            return

        # ── MT5 live — place a resting limit order on the account ──
        if decision.market == "mt5":
            try:
                from plugins.AgentPaulPlugin.backend.services.mt5_adapter import execute_mt5

                res = await execute_mt5(db, decision)
            except Exception as exc:  # noqa: BLE001
                decision.status = PaulDecisionStatus.FAILED
                decision.error = str(exc)
                decision.execution_result = {"error": str(exc)}
                return
            if isinstance(res, dict) and res.get("error"):
                decision.status = PaulDecisionStatus.FAILED
                decision.error = str(res["error"])
                decision.execution_result = res
            else:
                decision.status = PaulDecisionStatus.EXECUTED
                decision.outcome = "open"
                decision.execution_result = res
            return

        # Live modes — route through the core LiveTradeEngine
        if not decision.signal_id:
            decision.signal_id = await PaulLoop._create_core_signal(db, decision)

        try:
            from app.trading.live import LiveTradeEngine

            res = await LiveTradeEngine.execute_signal(db, decision.signal_id)
        except Exception as exc:  # noqa: BLE001
            decision.status = PaulDecisionStatus.FAILED
            decision.error = str(exc)
            decision.execution_result = {"error": str(exc)}
            return

        if isinstance(res, dict) and res.get("error"):
            decision.status = PaulDecisionStatus.FAILED
            decision.error = str(res["error"])
            decision.execution_result = res
        else:
            decision.status = PaulDecisionStatus.EXECUTED
            decision.outcome = "open"
            decision.execution_result = res

    @staticmethod
    async def _create_core_signal(db: AsyncSession, decision: PaulDecision) -> int:
        """Create a core Signal row so the live engine can execute it."""
        from app.models.database import Signal, SignalSource, SignalAction, SignalStatus

        sig = Signal(
            source=SignalSource.SYSTEM,
            symbol=decision.symbol,
            action=SignalAction(decision.action),
            price=decision.entry,
            timeframe=decision.timeframe,
            strength=decision.confidence,
            confidence=decision.confidence,
            raw_data=json.dumps({"origin": "agent_paul", "decision_id": decision.id}),
            indicators=json.dumps(
                {
                    "stop_loss": decision.stop_loss,
                    "take_profit": decision.take_profit,
                    "risk_reward": decision.risk_reward,
                }
            ),
            status=SignalStatus.PENDING,
        )
        db.add(sig)
        await db.flush()
        return int(sig.id)

    # ── Orchestrate one decision (PLAN → QUALIFY → APPLY/QUEUE) ──

    @staticmethod
    async def decide(
        db: AsyncSession,
        symbol: str,
        timeframe: Optional[str],
        trigger: str,
        market: str = "crypto",
        account_id: Optional[int] = None,
    ) -> PaulDecision:
        s = await PaulLoop.get_settings(db)
        tf = timeframe or (s.mt5_timeframe if market == "mt5" else s.default_timeframe)
        session_id = str(uuid.uuid4())[:12]

        if not s.enabled:
            raise PaulDisabledError("Agent Paul is disabled. Enable it in settings.")

        plan = await PaulLoop._plan(db, symbol, tf, s, market, account_id)
        qualify_status, qualify_notes = await PaulLoop._qualify(db, s, plan, symbol)

        decision = PaulDecision(
            session_id=session_id,
            symbol=symbol,
            timeframe=tf,
            trigger=trigger,
            market=plan.get("market", market),
            account_id=plan.get("account_id", account_id),
            volume=plan.get("volume"),
            mode=s.mode,
            provenance=plan["provenance"],
            action=plan["action"],
            confidence=plan["confidence"],
            entry=plan["entry"],
            stop_loss=plan["stop_loss"],
            take_profit=plan["take_profit"],
            risk_reward=plan["risk_reward"],
            reasoning=plan["reasoning"],
            acceptance_criteria=plan["acceptance_criteria"],
            plan_json=plan["plan_json"],
            signal_id=plan["signal_id"],
            qualify_status=qualify_status,
            qualify_notes=qualify_notes,
            status=PaulDecisionStatus.PLANNED,
        )

        # Non-actionable or blocked → skip (loop still recorded)
        if plan["action"] not in ("buy", "sell") or qualify_status == PaulQualify.BLOCKED:
            decision.status = PaulDecisionStatus.SKIPPED
            db.add(decision)
            await db.commit()
            await db.refresh(decision)
            return decision

        db.add(decision)
        await db.flush()

        # Live + approval required → queue for a human
        if s.mode in _LIVE_MODES and s.require_approval:
            decision.status = PaulDecisionStatus.QUEUED
        else:
            await PaulLoop._apply(db, decision, s)

        await db.commit()
        await db.refresh(decision)
        return decision

    # ── Queue actions ──────────────────────────────────────

    @staticmethod
    async def approve(db: AsyncSession, decision_id: int) -> PaulDecision:
        decision = await PaulLoop._get(db, decision_id)
        if decision.status != PaulDecisionStatus.QUEUED:
            raise PaulStateError(f"Decision {decision_id} is not queued (status={decision.status.value}).")
        s = await PaulLoop.get_settings(db)
        decision.status = PaulDecisionStatus.APPROVED
        await PaulLoop._apply(db, decision, s)
        await db.commit()
        await db.refresh(decision)
        return decision

    @staticmethod
    async def reject(db: AsyncSession, decision_id: int) -> PaulDecision:
        decision = await PaulLoop._get(db, decision_id)
        decision.status = PaulDecisionStatus.REJECTED
        await db.commit()
        await db.refresh(decision)
        return decision

    @staticmethod
    async def execute(db: AsyncSession, decision_id: int) -> PaulDecision:
        """Force-apply a planned/queued/approved decision."""
        decision = await PaulLoop._get(db, decision_id)
        if decision.status in (PaulDecisionStatus.EXECUTED, PaulDecisionStatus.UNIFIED):
            raise PaulStateError(f"Decision {decision_id} already executed.")
        s = await PaulLoop.get_settings(db)
        await PaulLoop._apply(db, decision, s)
        await db.commit()
        await db.refresh(decision)
        return decision

    # ── UNIFY ──────────────────────────────────────────────

    @staticmethod
    async def unify(
        db: AsyncSession, decision_id: int, outcome: str, pnl: Optional[float], notes: Optional[str]
    ) -> PaulDecision:
        decision = await PaulLoop._get(db, decision_id)
        decision.outcome = outcome
        decision.outcome_pnl = pnl
        decision.unify_notes = notes
        decision.status = PaulDecisionStatus.UNIFIED
        decision.unified_at = now_sast()
        await db.commit()
        await db.refresh(decision)
        return decision

    # ── Queries ────────────────────────────────────────────

    @staticmethod
    async def _get(db: AsyncSession, decision_id: int) -> PaulDecision:
        row = (
            await db.execute(select(PaulDecision).where(PaulDecision.id == decision_id))
        ).scalar_one_or_none()
        if row is None:
            raise PaulNotFoundError(f"Decision {decision_id} not found.")
        return row

    @staticmethod
    async def list_decisions(
        db: AsyncSession, limit: int = 50, queued_only: bool = False
    ) -> List[PaulDecision]:
        q = select(PaulDecision).order_by(desc(PaulDecision.created_at))
        if queued_only:
            q = q.where(PaulDecision.status == PaulDecisionStatus.QUEUED)
        q = q.limit(limit)
        return list((await db.execute(q)).scalars().all())

    # ── Dev-workflow (PAUL framework) reference ────────────

    @staticmethod
    def loop_info() -> Dict[str, Any]:
        """Static PAUL framework reference for the in-app workflow tab."""
        return {
            "framework": "PAUL — Plan / Apply / Unify Loop",
            "source": "https://github.com/ChristopherKahler/paul",
            "summary": (
                "PAUL is a structured AI-assisted execution loop. Agent Paul maps "
                "that loop onto trading: every trade is planned with acceptance "
                "criteria, qualified against policy, applied through the chosen "
                "authority mode, and unified (reconciled) when closed."
            ),
            "loop": [
                {
                    "phase": "PLAN",
                    "trading": "Gather market context + AI/heuristic plan with BDD acceptance criteria, entry, SL, TP.",
                },
                {
                    "phase": "APPLY / QUALIFY",
                    "trading": "Independently re-check the plan against risk policy, then execute (paper / TradeBot / PAUL-direct) or queue for approval.",
                },
                {
                    "phase": "UNIFY",
                    "trading": "Reconcile planned vs actual, record outcome and PnL, close the loop. No orphan decisions.",
                },
            ],
            "modes": {
                "paper": "Simulated fills only — never touches the exchange.",
                "tradebot_execute": "PAUL advises; the core live-trade engine places the order.",
                "paul_execute": "PAUL executes autonomously through the same engine (no human gate).",
            },
            "commands": [
                "/paul:plan — create an executable plan",
                "/paul:apply — execute the approved plan",
                "/paul:unify — reconcile and close the loop",
            ],
        }


# ── Helpers ────────────────────────────────────────────────


def _acceptance(symbol: str, action: str, settings_row: PaulSettings) -> List[Dict[str, str]]:
    """PAUL-style BDD acceptance criteria, shared by crypto and MT5 plans."""
    return [
        {
            "id": "AC-1",
            "text": (
                f"Given a {action.upper()} plan for {symbol}, "
                f"When confidence >= {settings_row.min_confidence:.2f}, "
                f"Then the decision may proceed to Apply."
            ),
        },
        {
            "id": "AC-2",
            "text": "Given an actionable plan, When SL and TP are set, Then risk-reward must be >= 1.2.",
        },
        {
            "id": "AC-3",
            "text": (
                f"Given mode '{settings_row.mode.value}', When live execution is requested, "
                "Then all risk/policy checks in Qualify must pass."
            ),
        },
    ]


def _mt5_hold_plan(account_id: Optional[int], reason: str) -> Dict[str, Any]:
    """A non-actionable MT5 plan (no setup / account problem)."""
    return {
        "action": "hold",
        "confidence": 0.0,
        "entry": None,
        "stop_loss": None,
        "take_profit": None,
        "risk_reward": None,
        "reasoning": reason,
        "provenance": PaulProvenance.HEURISTIC,
        "signal_id": None,
        "market": "mt5",
        "account_id": account_id,
        "plan_json": {},
    }


# ── Errors ─────────────────────────────────────────────────


class PaulError(Exception):
    """Base Agent Paul error."""


class PaulDisabledError(PaulError):
    pass


class PaulNotFoundError(PaulError):
    pass


class PaulStateError(PaulError):
    pass
