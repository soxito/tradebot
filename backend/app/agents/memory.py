"""
Agent Decision Memory — enables agents to learn from past decisions.

Key capabilities:
1. Retrieve relevant past decisions for a symbol + role
2. Compute win/loss stats to gauge historical accuracy
3. Generate a "memory prompt" injected into agent context so OpenAI sees history
4. Make local decisions (without calling OpenAI) when enough memory exists
"""
import json
from typing import Dict, Any, List, Optional, Tuple
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func, and_

from app.models.database import AgentDecision
from app.core.config import settings


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


def _extract_learning_features(market_data: Any) -> Dict[str, Any]:
    payload = _as_dict(market_data)
    pipeline_signal = _as_dict(payload.get("pipeline_signal"))
    signal_payload = _as_dict(payload.get("signal"))
    raw_data = _as_dict(pipeline_signal.get("raw_data"))
    if not raw_data:
        raw_data = _as_dict(signal_payload.get("raw_data"))

    volume_context = _as_dict(payload.get("volume_context"))
    if not volume_context:
        volume_context = _as_dict(pipeline_signal.get("volume_context"))
    if not volume_context:
        volume_context = _as_dict(signal_payload.get("volume_context"))
    if not volume_context:
        volume_context = _as_dict(raw_data.get("volume_context"))

    btc_news_context = _as_dict(payload.get("btc_news_context"))
    if not btc_news_context:
        btc_news_context = _as_dict(pipeline_signal.get("btc_news_context"))
    if not btc_news_context:
        btc_news_context = _as_dict(signal_payload.get("btc_news_context"))
    if not btc_news_context:
        btc_news_context = _as_dict(raw_data.get("btc_news_context"))

    order_flow_confirmed = _as_optional_bool(payload.get("order_flow_confirmed"))
    if order_flow_confirmed is None:
        order_flow_confirmed = _as_optional_bool(pipeline_signal.get("order_flow_confirmed"))
    if order_flow_confirmed is None:
        order_flow_confirmed = _as_optional_bool(signal_payload.get("order_flow_confirmed"))
    if order_flow_confirmed is None:
        order_flow_confirmed = _as_optional_bool(raw_data.get("order_flow_confirmed"))
    if order_flow_confirmed is None:
        order_flow_confirmed = _as_optional_bool(volume_context.get("directional_confirmed"))

    btc_news_confirms = _as_optional_bool(payload.get("btc_sentiment_confirms"))
    if btc_news_confirms is None:
        btc_news_confirms = _as_optional_bool(payload.get("btc_news_confirms"))
    if btc_news_confirms is None:
        btc_news_confirms = _as_optional_bool(pipeline_signal.get("btc_sentiment_confirms"))
    if btc_news_confirms is None:
        btc_news_confirms = _as_optional_bool(signal_payload.get("btc_sentiment_confirms"))
    if btc_news_confirms is None:
        btc_news_confirms = _as_optional_bool(raw_data.get("btc_sentiment_confirms"))
    if btc_news_confirms is None:
        btc_news_confirms = _as_optional_bool(btc_news_context.get("confirms"))

    btc_score = btc_news_context.get("score")
    if btc_score is None:
        btc_score = payload.get("btc_sentiment_score")
    if btc_score is None:
        btc_score = pipeline_signal.get("btc_sentiment_score")
    if btc_score is None:
        btc_score = signal_payload.get("btc_sentiment_score")

    btc_label = btc_news_context.get("label")
    if not btc_label:
        btc_label = payload.get("btc_sentiment_label")
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


async def get_past_decisions(
    db: AsyncSession,
    symbol: str,
    role: str,
    limit: int | None = None,
) -> List[Dict[str, Any]]:
    """Fetch recent decisions for a symbol + agent role, ordered newest first."""
    lookback = limit or settings.AI_MEMORY_LOOKBACK
    result = await db.execute(
        select(AgentDecision)
        .where(
            and_(
                AgentDecision.symbol == symbol,
                AgentDecision.agent_role == role,
            )
        )
        .order_by(desc(AgentDecision.created_at))
        .limit(lookback)
    )
    rows = result.scalars().all()
    parsed: List[Dict[str, Any]] = []
    for row in rows:
        features = _extract_learning_features(getattr(row, "market_data", None))
        parsed.append(
            {
                "action": row.action,
                "confidence": row.confidence,
                "reasoning": row.reasoning,
                "outcome": row.outcome,
                "outcome_pnl": row.outcome_pnl,
                "ai_called": row.ai_called,
                "created_at": str(row.created_at) if row.created_at else None,
                "order_flow_confirmed": features.get("order_flow_confirmed"),
                "btc_news_confirms": features.get("btc_news_confirms"),
                "btc_score": features.get("btc_score"),
                "btc_label": features.get("btc_label"),
                "volume_ratio": features.get("volume_ratio"),
            }
        )
    return parsed


async def get_learning_stats(
    db: AsyncSession,
    symbol: Optional[str] = None,
    role: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute learning statistics: accuracy, win rate, local vs API decisions."""
    filters = []
    if symbol:
        filters.append(AgentDecision.symbol == symbol)
    if role:
        filters.append(AgentDecision.agent_role == role)

    base = select(AgentDecision)
    if filters:
        base = base.where(and_(*filters))

    result = await db.execute(base)
    rows = result.scalars().all()

    total = len(rows)
    if total == 0:
        return {
            "total_decisions": 0, "with_outcome": 0,
            "wins": 0, "losses": 0, "break_even": 0,
            "win_rate": 0, "accuracy": 0,
            "ai_calls": 0, "local_decisions": 0,
            "local_pct": 0,
        }

    with_outcome = [r for r in rows if r.outcome]
    wins = sum(1 for r in with_outcome if r.outcome == "win")
    losses = sum(1 for r in with_outcome if r.outcome == "loss")
    break_even = sum(1 for r in with_outcome if r.outcome == "break_even")
    ai_calls = sum(1 for r in rows if r.ai_called)
    local = total - ai_calls
    total_pnl = sum(r.outcome_pnl or 0 for r in with_outcome)

    return {
        "total_decisions": total,
        "with_outcome": len(with_outcome),
        "wins": wins,
        "losses": losses,
        "break_even": break_even,
        "win_rate": round(wins / len(with_outcome), 3) if with_outcome else 0,
        "accuracy": round((wins + break_even) / len(with_outcome), 3) if with_outcome else 0,
        "total_pnl": round(total_pnl, 4),
        "ai_calls": ai_calls,
        "local_decisions": local,
        "local_pct": round(local / total * 100, 1) if total else 0,
    }


def build_memory_prompt(past_decisions: List[Dict[str, Any]]) -> str:
    """Build a concise memory summary string injected into the agent user prompt."""
    if not past_decisions:
        return ""

    with_outcome = [d for d in past_decisions if d.get("outcome")]
    if not with_outcome:
        return f"\n\n[MEMORY] You have {len(past_decisions)} prior analyses for this symbol but no outcome feedback yet."

    wins = sum(1 for d in with_outcome if d["outcome"] == "win")
    losses = sum(1 for d in with_outcome if d["outcome"] == "loss")
    total = len(with_outcome)
    win_rate = round(wins / total * 100, 1) if total else 0

    def _summarize_feature(records: List[Dict[str, Any]], title: str) -> Optional[str]:
        if not records:
            return None
        record_wins = sum(1 for d in records if d.get("outcome") == "win")
        record_losses = sum(1 for d in records if d.get("outcome") == "loss")
        win_pct = round(record_wins / len(records) * 100, 1)
        return f"{title}: {record_wins}W/{record_losses}L over {len(records)} trades ({win_pct}% win rate)"

    feature_lines = [
        _summarize_feature([d for d in with_outcome if d.get("order_flow_confirmed") is True], "Order-flow confirmed"),
        _summarize_feature([d for d in with_outcome if d.get("order_flow_confirmed") is False], "Order-flow conflicted"),
        _summarize_feature([d for d in with_outcome if d.get("btc_news_confirms") is True], "BTC news aligned"),
        _summarize_feature([d for d in with_outcome if d.get("btc_news_confirms") is False], "BTC news conflicted"),
    ]
    feature_lines = [line for line in feature_lines if line]

    # Summarize recent outcomes
    recent = with_outcome[:10]
    examples = []
    for d in recent:
        examples.append(
            f"  - {d['action'].upper()} (conf={d.get('confidence', '?')}) → "
            f"{d['outcome']} (PnL: {d.get('outcome_pnl', 'N/A')})"
        )

    return (
        f"\n\n[MEMORY — LEARN FROM YOUR PAST DECISIONS]\n"
        f"You have {total} past decisions with outcomes for this symbol.\n"
        f"Win rate: {win_rate}% ({wins}W / {losses}L / {total - wins - losses}BE)\n"
        + ("Feature performance:\n" + "\n".join(f"  - {line}" for line in feature_lines) + "\n" if feature_lines else "")
        + f"Recent outcomes:\n"
        + "\n".join(examples)
        + "\n"
        + f"Use these results to improve your analysis. Repeat what worked, avoid what didn't."
    )


def try_local_decision(
    past_decisions: List[Dict[str, Any]],
    role: str,
) -> Optional[Dict[str, Any]]:
    """
    Attempt to make a decision locally from memory without calling OpenAI.
    Returns a decision dict if confident enough, or None to fall back to OpenAI.

    Strategy:
    - Need at least AI_MIN_MEMORY_FOR_LOCAL decisions with outcomes
    - Look at recent winning pattern for this role
    - If win rate for a particular action is above threshold, replicate it
    """
    with_outcome = [d for d in past_decisions if d.get("outcome")]
    min_needed = settings.AI_MIN_MEMORY_FOR_LOCAL
    threshold = settings.AI_LOCAL_CONFIDENCE_THRESHOLD

    if len(with_outcome) < min_needed:
        return None

    # Group by action and compute win rates
    action_stats: Dict[str, Dict[str, Any]] = {}
    for d in with_outcome:
        act = d["action"]
        if act not in action_stats:
            action_stats[act] = {"wins": 0, "total": 0, "avg_conf": 0, "pnl": 0}
        stats = action_stats[act]
        stats["total"] += 1
        if d["outcome"] == "win":
            stats["wins"] += 1
        stats["pnl"] += d.get("outcome_pnl") or 0
        stats["avg_conf"] += d.get("confidence") or 0

    # Compute win rates
    for act, stats in action_stats.items():
        stats["win_rate"] = stats["wins"] / stats["total"] if stats["total"] else 0
        stats["avg_conf"] = stats["avg_conf"] / stats["total"] if stats["total"] else 0

    # For signal_generator / trade_executor: look for actionable signals
    if role in ("signal_generator", "trade_executor", "risk_manager"):
        # Most recent 10 decisions — what's the dominant winning action?
        recent_wins = [d for d in with_outcome[:15] if d["outcome"] == "win"]
        if len(recent_wins) >= 5:
            from collections import Counter
            action_counts = Counter(d["action"] for d in recent_wins)
            best_action, count = action_counts.most_common(1)[0]
            recent_win_rate = count / min(15, len(with_outcome))

            if recent_win_rate >= threshold:
                avg_conf = sum(d.get("confidence", 0.5) for d in recent_wins) / len(recent_wins)
                reasoning_bits = [d.get("reasoning", "") for d in recent_wins[:3] if d.get("reasoning")]
                reasoning_summary = "; ".join(r[:80] for r in reasoning_bits) if reasoning_bits else "Based on historical pattern"
                order_flow_confirmed_wins = sum(1 for d in recent_wins if d.get("order_flow_confirmed") is True)
                btc_aligned_wins = sum(1 for d in recent_wins if d.get("btc_news_confirms") is True)
                feature_reinforcement: List[str] = []
                if order_flow_confirmed_wins:
                    feature_reinforcement.append(
                        f"{order_flow_confirmed_wins}/{len(recent_wins)} wins had confirmed order-flow"
                    )
                if btc_aligned_wins:
                    feature_reinforcement.append(
                        f"{btc_aligned_wins}/{len(recent_wins)} wins had BTC-news alignment"
                    )
                feature_hint = (" " + "; ".join(feature_reinforcement) + ".") if feature_reinforcement else ""

                logger.info(
                    f"[Memory] Local decision for {role}: {best_action} "
                    f"(recent_win_rate={recent_win_rate:.0%}, based on {len(recent_wins)} wins)"
                )
                return {
                    "action": best_action,
                    "confidence": round(min(avg_conf, recent_win_rate), 3),
                    "reasoning": f"[LOCAL MEMORY] {reasoning_summary}. "
                                 f"Based on {len(recent_wins)} recent winning decisions "
                                 f"with {recent_win_rate:.0%} win rate.{feature_hint}",
                    "from_memory": True,
                }

    # For analyst roles: pick action with best win rate
    if role in ("market_analyst", "sentiment_analyst"):
        best_action = None
        best_wr = 0
        for act, stats in action_stats.items():
            if stats["total"] >= 5 and stats["win_rate"] > best_wr:
                best_wr = stats["win_rate"]
                best_action = act

        if best_action and best_wr >= threshold:
            logger.info(
                f"[Memory] Local decision for {role}: {best_action} "
                f"(win_rate={best_wr:.0%})"
            )
            return {
                "action": best_action,
                "confidence": round(best_wr, 3),
                "reasoning": f"[LOCAL MEMORY] Historical pattern shows {best_action} "
                             f"has {best_wr:.0%} win rate over {action_stats[best_action]['total']} decisions.",
                "from_memory": True,
            }

    return None
