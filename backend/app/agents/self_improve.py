"""
Self-improvement — agents rewrite their own standing instructions from results.

Every decision an agent makes is stored with the outcome it eventually earned
(win / loss / break-even, plus realised PnL). This module turns that history
into a score, hands the score back to the agent, and asks it to rewrite the
standing instructions it runs on so the next decision is better than the last.

Two deliberate constraints:

* Only ``RoomAgentProfile.tasks`` — the free-text "standing instructions"
  appended to the prompt each run — is ever rewritten. The ``system_prompt``
  carries the strict JSON contract the orchestrator parses; letting a model
  edit that would eventually break decision parsing for everyone.
* Every rewrite is written to ``agent_instruction_revisions`` before it takes
  effect, so a change that makes things worse can be read and reverted rather
  than silently compounding.

An agent with too little outcome history is left alone. Rewriting instructions
from four trades is noise-fitting, not learning.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Agent, AgentDecision, AgentInstructionRevision, RoomAgentProfile

# Below this many *resolved* decisions the sample is too small to learn from.
MIN_DECISIONS = 12
# How far back to look when scoring.
LOOKBACK = 120
# Instructions are a brief, not an essay — long ones crowd out the market data.
MAX_INSTRUCTION_CHARS = 1200


@dataclass
class AgentScore:
    """How an agent has actually performed, from its own decision history."""

    role: str
    agent_id: int
    agent_name: str
    total: int = 0
    resolved: int = 0
    wins: int = 0
    losses: int = 0
    break_even: int = 0
    total_pnl: float = 0.0
    # Mean confidence on decisions that won vs lost — the calibration signal.
    avg_conf_win: float = 0.0
    avg_conf_loss: float = 0.0
    worst: List[Dict[str, Any]] = field(default_factory=list)
    best: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def win_rate(self) -> Optional[float]:
        decided = self.wins + self.losses
        return (self.wins / decided) if decided else None

    @property
    def avg_pnl(self) -> Optional[float]:
        return (self.total_pnl / self.resolved) if self.resolved else None

    @property
    def enough_history(self) -> bool:
        return self.resolved >= MIN_DECISIONS

    def summary(self) -> str:
        """The scorecard as the agent will read it."""
        wr = f"{self.win_rate:.0%}" if self.win_rate is not None else "n/a"
        pnl = f"{self.avg_pnl:+.4f}" if self.avg_pnl is not None else "n/a"
        lines = [
            f"Decisions reviewed: {self.resolved} resolved of {self.total} total",
            f"Win rate: {wr}  ({self.wins}W / {self.losses}L / {self.break_even}BE)",
            f"Average PnL per resolved decision: {pnl}",
        ]
        if self.avg_conf_win or self.avg_conf_loss:
            lines.append(
                f"Mean confidence when right: {self.avg_conf_win:.2f} — when wrong: {self.avg_conf_loss:.2f}"
            )
            if self.avg_conf_loss >= self.avg_conf_win:
                lines.append(
                    "You are no more confident when you are right than when you are "
                    "wrong. Your confidence is not currently informative."
                )
        if self.worst:
            lines.append("\nYour costliest recent calls:")
            for d in self.worst:
                lines.append(
                    f"  - {d['symbol']} {d['action']} @ conf {d['confidence']:.2f} "
                    f"→ {d['outcome']} ({d['pnl']:+.4f}) — {d['reasoning']}"
                )
        if self.best:
            lines.append("\nYour best recent calls:")
            for d in self.best:
                lines.append(
                    f"  - {d['symbol']} {d['action']} @ conf {d['confidence']:.2f} "
                    f"→ {d['outcome']} ({d['pnl']:+.4f}) — {d['reasoning']}"
                )
        return "\n".join(lines)


def _brief(row: AgentDecision) -> Dict[str, Any]:
    reasoning = (row.reasoning or "").strip().replace("\n", " ")
    return {
        "symbol": row.symbol,
        "action": row.action,
        "confidence": float(row.confidence or 0.0),
        "outcome": row.outcome,
        "pnl": float(row.outcome_pnl or 0.0),
        "reasoning": (reasoning[:160] + "…") if len(reasoning) > 160 else reasoning,
    }


async def score_agent(db: AsyncSession, agent: Agent, lookback: int = LOOKBACK) -> AgentScore:
    """Turn one agent's recent decision history into a scorecard."""
    rows = (
        await db.execute(
            select(AgentDecision)
            .where(AgentDecision.agent_role == agent.role)
            .order_by(desc(AgentDecision.id))
            .limit(lookback)
        )
    ).scalars().all()

    score = AgentScore(role=agent.role, agent_id=agent.id, agent_name=agent.name, total=len(rows))
    conf_wins: List[float] = []
    conf_losses: List[float] = []
    resolved: List[AgentDecision] = []

    for r in rows:
        if not r.outcome:
            continue
        resolved.append(r)
        score.resolved += 1
        score.total_pnl += float(r.outcome_pnl or 0.0)
        if r.outcome == "win":
            score.wins += 1
            conf_wins.append(float(r.confidence or 0.0))
        elif r.outcome == "loss":
            score.losses += 1
            conf_losses.append(float(r.confidence or 0.0))
        else:
            score.break_even += 1

    score.avg_conf_win = sum(conf_wins) / len(conf_wins) if conf_wins else 0.0
    score.avg_conf_loss = sum(conf_losses) / len(conf_losses) if conf_losses else 0.0

    by_pnl = sorted(resolved, key=lambda r: float(r.outcome_pnl or 0.0))
    score.worst = [_brief(r) for r in by_pnl[:3]]
    score.best = [_brief(r) for r in reversed(by_pnl[-3:])]
    return score


REWRITE_SYSTEM_PROMPT = """\
You improve a trading agent's standing instructions using its measured results.

You will be given the agent's role, its current standing instructions, and a
scorecard built from its own past decisions and their real outcomes.

Rewrite the standing instructions so the agent makes better decisions. Be
concrete and behavioural: name the conditions to check, the patterns that cost
money, and when to stand down. Do not restate the role or repeat generic
trading advice. Keep what is demonstrably working.

Rules:
- Under 1000 characters. A brief, not an essay.
- Never instruct the agent to ignore or override risk limits.
- Never specify an output format; something else owns that.
- If the scorecard shows confidence is poorly calibrated, address it directly.

Respond with valid JSON only:
{"instructions": "<the rewritten standing instructions>",
 "rationale": "<one or two sentences on what you changed and why>"}
"""


async def propose_instructions(
    db: AsyncSession,
    agent: Agent,
    score: AgentScore,
    current: Optional[str],
) -> Optional[Dict[str, str]]:
    """Ask the connected providers for better instructions. None if unavailable."""
    try:
        from plugins.AiMarketAnalyst.backend.services.ai_router import agent_chat, has_enabled_providers
    except Exception as exc:  # noqa: BLE001 - plugin-optional
        logger.debug(f"[self-improve] ai_router unavailable: {exc}")
        return None

    if not await has_enabled_providers(db):
        logger.info("[self-improve] no enabled providers — skipping rewrite")
        return None

    user_prompt = (
        f"Agent role: {agent.role} ({agent.name})\n\n"
        f"Current standing instructions:\n{current or '(none set)'}\n\n"
        f"Scorecard:\n{score.summary()}\n"
    )

    try:
        res = await agent_chat(
            db,
            system_prompt=REWRITE_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            max_tokens=700,
            agent_name=f"self-improve:{agent.role}",
            agent_role=agent.role,
            source="self_improve",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[self-improve] provider call failed for {agent.role}: {exc}")
        return None

    if not res.get("ok"):
        logger.info(f"[self-improve] provider returned no result for {agent.role}")
        return None

    content = res.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            logger.warning(f"[self-improve] unparsable rewrite for {agent.role}")
            return None
    if not isinstance(content, dict):
        return None

    instructions = (content.get("instructions") or "").strip()
    if not instructions:
        return None

    return {
        "instructions": instructions[:MAX_INSTRUCTION_CHARS],
        "rationale": (content.get("rationale") or "").strip()[:500],
    }


async def improve_agent(
    db: AsyncSession,
    agent: Agent,
    *,
    force: bool = False,
) -> Dict[str, Any]:
    """Score one agent and rewrite its standing instructions if warranted."""
    score = await score_agent(db, agent)

    if not score.enough_history and not force:
        return {
            "role": agent.role,
            "changed": False,
            "reason": f"only {score.resolved} resolved decisions (need {MIN_DECISIONS})",
            "resolved": score.resolved,
        }

    profile = (
        await db.execute(
            select(RoomAgentProfile).where(RoomAgentProfile.agent_id == agent.id)
        )
    ).scalar_one_or_none()
    current = profile.tasks if profile else None

    proposal = await propose_instructions(db, agent, score, current)
    if not proposal:
        return {"role": agent.role, "changed": False, "reason": "no proposal available"}

    if proposal["instructions"].strip() == (current or "").strip():
        return {"role": agent.role, "changed": False, "reason": "instructions unchanged"}

    # Record the evidence *before* the change takes effect.
    db.add(
        AgentInstructionRevision(
            agent_id=agent.id,
            role=agent.role,
            previous_instructions=current,
            new_instructions=proposal["instructions"],
            rationale=proposal["rationale"],
            decisions_reviewed=score.resolved,
            win_rate=score.win_rate,
            avg_pnl=score.avg_pnl,
        )
    )

    if profile is None:
        from app.agents import room

        persona = room.persona_for(agent.role)
        profile = RoomAgentProfile(
            agent_id=agent.id,
            human_name=persona["human_name"],
            title=persona["title"],
            color=persona["color"],
            seat=persona["seat"],
        )
        db.add(profile)
    profile.tasks = proposal["instructions"]

    await db.commit()
    logger.info(
        f"🧠 [self-improve] {agent.role}: instructions rewritten "
        f"(win rate {score.win_rate:.0%} over {score.resolved})"
        if score.win_rate is not None
        else f"🧠 [self-improve] {agent.role}: instructions rewritten"
    )

    return {
        "role": agent.role,
        "changed": True,
        "resolved": score.resolved,
        "win_rate": score.win_rate,
        "avg_pnl": score.avg_pnl,
        "rationale": proposal["rationale"],
        "instructions": proposal["instructions"],
    }


async def improve_all(db: AsyncSession, *, force: bool = False) -> List[Dict[str, Any]]:
    """Run the improvement pass across every active agent."""
    agents = (
        await db.execute(select(Agent).where(Agent.is_active == True))  # noqa: E712
    ).scalars().all()

    results = []
    for agent in agents:
        try:
            results.append(await improve_agent(db, agent, force=force))
        except Exception as exc:  # noqa: BLE001 - one bad agent must not stop the rest
            logger.warning(f"[self-improve] {agent.role} failed: {exc}")
            await db.rollback()
            results.append({"role": agent.role, "changed": False, "reason": str(exc)})

    # Refresh the in-memory personas so the next analysis uses the new text.
    try:
        from app.api.agents import refresh_persona_overrides

        await refresh_persona_overrides(db)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[self-improve] persona refresh failed: {exc}")

    return results


async def revert_revision(db: AsyncSession, revision_id: int) -> Dict[str, Any]:
    """Put back the instructions a revision replaced."""
    rev = await db.get(AgentInstructionRevision, revision_id)
    if rev is None:
        return {"reverted": False, "reason": "revision not found"}

    profile = (
        await db.execute(
            select(RoomAgentProfile).where(RoomAgentProfile.agent_id == rev.agent_id)
        )
    ).scalar_one_or_none()
    if profile is None:
        return {"reverted": False, "reason": "agent has no room profile"}

    profile.tasks = rev.previous_instructions
    rev.applied = False
    await db.commit()

    try:
        from app.api.agents import refresh_persona_overrides

        await refresh_persona_overrides(db)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[self-improve] persona refresh failed: {exc}")

    logger.info(f"↩️  [self-improve] reverted revision {revision_id} for {rev.role}")
    return {"reverted": True, "role": rev.role, "restored": rev.previous_instructions}
