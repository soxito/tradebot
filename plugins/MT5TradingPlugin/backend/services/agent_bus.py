"""
MT5 Trading Plugin — typed agent message bus.

A thin, typed layer over the existing ``app.core.events.EventBus`` (Redis
pub/sub with an in-memory fallback, already built and already used for SSE).
This adds two agent-to-agent topics and dataclass payloads so producers and
consumers share one schema instead of passing loose dicts.

The point of the bus is that agents communicate through shared messages plus
shared memory (``mt5_research_findings``) rather than each re-calling an AI
provider for the same question. The research loop publishes findings once;
OpenHuman agents consume them and publish strategy proposals back to Jarvis.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, List, Optional, Set

from loguru import logger


class AgentTopics:
    """Topic names for agent-to-agent traffic.

    Deliberately namespaced away from ``app.core.events.Topics`` so the SSE
    endpoint's topic set is unaffected — ``EventBus.subscribe`` filters against
    whatever set the caller passes, so new topics need no registration.
    """

    RESEARCH_FINDING = "agent.research.finding"
    STRATEGY_PROPOSAL = "agent.strategy.proposal"

    ALL: Set[str] = {RESEARCH_FINDING, STRATEGY_PROPOSAL}


@dataclass
class ResearchFindingMessage:
    """One piece of background research, with its provenance."""

    kind: str                      # calendar | news | sentiment | prediction
    headline: str
    symbol: Optional[str] = None
    body: str = ""
    source: Optional[str] = None
    source_url: Optional[str] = None
    confidence: float = 0.0
    #: True when no verifiable source URL backs this. A speculative finding must
    #: never gate a trade signal on its own.
    speculative: bool = True
    provider_used: Optional[str] = None
    published_at: Optional[str] = None
    decay_at: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "ResearchFindingMessage":
        known = {k: v for k, v in (raw or {}).items() if k in cls.__annotations__}
        return cls(**known)


@dataclass
class StrategyProposalMessage:
    """A strategy an OpenHuman agent compiled from shared memory."""

    symbol: str
    stance: str                    # bullish | bearish | neutral | stand_aside
    rationale: str
    #: Ids of the ResearchFinding rows this was compiled from — the audit trail.
    finding_ids: List[int] = field(default_factory=list)
    confidence: float = 0.0
    #: True when every supporting finding was speculative. Such a proposal is
    #: advisory only and must not gate a trade signal.
    speculative: bool = True
    agent: str = "openhuman"
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "StrategyProposalMessage":
        known = {k: v for k, v in (raw or {}).items() if k in cls.__annotations__}
        return cls(**known)


def _bus():
    """Resolve the shared EventBus lazily so this module imports standalone."""
    from app.core.events import event_bus

    return event_bus


async def publish_finding(message: ResearchFindingMessage) -> None:
    try:
        await _bus().publish(AgentTopics.RESEARCH_FINDING, message.to_dict())
    except Exception as exc:  # noqa: BLE001 — the bus is best-effort
        logger.debug(f"[agent_bus] finding publish skipped: {exc}")


async def publish_proposal(message: StrategyProposalMessage) -> None:
    try:
        await _bus().publish(AgentTopics.STRATEGY_PROPOSAL, message.to_dict())
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[agent_bus] proposal publish skipped: {exc}")


async def subscribe(
    topics: Optional[Set[str]] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Stream agent messages. Defaults to every agent topic."""
    async for payload in _bus().subscribe(topics or AgentTopics.ALL):
        yield payload


async def drain(topics: Set[str], *, timeout: float = 1.0,
                limit: int = 100) -> List[Dict[str, Any]]:
    """Collect messages already queued on `topics`, up to `timeout` seconds.

    Used by agents that run on a tick rather than holding a long-lived
    subscription.
    """
    out: List[Dict[str, Any]] = []
    try:
        stream = subscribe(topics)
        while len(out) < limit:
            try:
                out.append(await asyncio.wait_for(stream.__anext__(), timeout))
            except (asyncio.TimeoutError, StopAsyncIteration):
                break
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[agent_bus] drain ended: {exc}")
    return out
