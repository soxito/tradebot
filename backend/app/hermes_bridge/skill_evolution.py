"""
Best-trader skill evolution (B) — self-improve for hermes_skills/{symbol}-best-trader.

Mirrors app/agents/self_improve.py but operates on Skill files, not RoomAgentProfile.
Every skill keeps its stock playbook forever; only the `## Learned (auto)` block
between `<!-- auto-learned:start -->` and `<!-- auto-learned:end -->` is rewritten
from measured outcomes. No stock prompt is overwritten.

Gate: 12+ resolved AgentDecision rows for that symbol (same as agent self-improve).
Else evolution is skipped (avoids noise-fitting four trades).

Scoring stays on Postgres AgentDecision — FTS5 remains recall-only.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

# Keep in sync with self_improve thresholds
MIN_DECISIONS = 12
LOOKBACK = 120
MAX_LEARNED_CHARS = 900

EV_START = "<!-- auto-learned:start -->"
EV_END = "<!-- auto-learned:end -->"


@dataclass
class SkillScore:
    symbol: str
    slug: str
    total: int = 0
    resolved: int = 0
    wins: int = 0
    losses: int = 0
    break_even: int = 0
    total_pnl: float = 0.0
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
        wr = f"{self.win_rate:.0%}" if self.win_rate is not None else "n/a"
        pnl = f"{self.avg_pnl:+.4f}" if self.avg_pnl is not None else "n/a"
        lines = [
            f"Symbol: {self.symbol} ({self.slug})",
            f"Decisions reviewed: {self.resolved} resolved of {self.total} total",
            f"Win rate: {wr}  ({self.wins}W / {self.losses}L / {self.break_even}BE)",
            f"Average PnL per resolved decision: {pnl}",
        ]
        if self.avg_conf_win or self.avg_conf_loss:
            lines.append(f"Mean confidence when right: {self.avg_conf_win:.2f} — when wrong: {self.avg_conf_loss:.2f}")
            if self.avg_conf_loss >= self.avg_conf_win:
                lines.append("Confidence is not informative (no higher when right). Calibrate it.")
        if self.worst:
            lines.append("\nCostliest recent calls:")
            for d in self.worst:
                lines.append(f"  - {d['symbol']} {d['action']} @ conf {d['confidence']:.2f} → {d['outcome']} ({d['pnl']:+.4f}) — {d['reasoning']}")
        if self.best:
            lines.append("\nBest recent calls:")
            for d in self.best:
                lines.append(f"  - {d['symbol']} {d['action']} @ conf {d['confidence']:.2f} → {d['outcome']} ({d['pnl']:+.4f}) — {d['reasoning']}")
        return "\n".join(lines)


def _brief(row) -> Dict[str, Any]:
    reasoning = (row.reasoning or "").strip().replace("\n", " ")
    return {
        "symbol": row.symbol,
        "action": row.action,
        "confidence": float(row.confidence or 0.0),
        "outcome": row.outcome,
        "pnl": float(row.outcome_pnl or 0.0),
        "reasoning": (reasoning[:140] + "…") if len(reasoning) > 140 else reasoning,
    }


def _norm(sym: str) -> str:
    return (sym or "").replace("/", "").replace(" ", "").strip().upper()


async def score_skill(db: AsyncSession, symbol: str, lookback: int = LOOKBACK) -> Optional[SkillScore]:
    """Score one best-trader skill from its symbol's AgentDecision history."""
    norm = _norm(symbol)
    if not norm:
        return None
    slug = f"{norm.lower()}-best-trader"
    try:
        from app.models.database import AgentDecision
        # Match both normalized and original forms (EURUSD vs EUR/USD)
        rows = (
            await db.execute(
                select(AgentDecision)
                .where(AgentDecision.symbol == norm)
                .order_by(desc(AgentDecision.id))
                .limit(lookback)
            )
        ).scalars().all()
        # Also include rows stored as EUR/USD if normalized didn't hit
        if not rows:
            # try with slash
            slash = f"{norm[:3]}/{norm[3:]}" if len(norm) in (6, 7, 8) else norm
            rows = (
                await db.execute(
                    select(AgentDecision)
                    .where(AgentDecision.symbol == slash)
                    .order_by(desc(AgentDecision.id))
                    .limit(lookback)
                )
            ).scalars().all()
        score = SkillScore(symbol=norm, slug=slug, total=len(rows))
        conf_wins: List[float] = []
        conf_losses: List[float] = []
        resolved: List[Any] = []
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
    except Exception as ex:
        logger.debug(f"[skill-evolve] score {symbol} skipped: {ex}")
        return None


REWRITE_SKILL_PROMPT = """\
You improve a Hermes best-trader skill's Learned block from measured trading results.

You will be given the skill's symbol, its current Learned block, and a scorecard built
from real AgentDecisions for that symbol (win/loss/outcome, PnL, best/worst calls).

Rewrite ONLY the Learned block (the content between the auto-learned markers). Be
concrete and behavioural: name the conditions that cost money, the patterns that
worked, when to stand down, session/timing tweaks, level quality tweaks, and
confidence calibration. Keep what is demonstrably working. Do not repeat the stock
playbook verbatim; supplement it.

Rules:
- Under 800 characters. A brief, not an essay.
- Plain markdown, 2–5 bullet lines or 2–3 sentences. No frontmatter, no code fences.
- Never instruct to ignore risk limits or execution gate.
- If sample is mixed, say what to watch next rather than overfitting.
- If confidence is poorly calibrated, address it directly.

Respond with valid JSON only:
{"learned": "<the rewritten Learned block markdown>",
 "rationale": "<one sentence on what you changed and why>"}
"""


async def propose_learned(
    db: AsyncSession,
    symbol: str,
    skill_entry: Dict[str, Any],
    score: SkillScore,
) -> Optional[Dict[str, str]]:
    """Ask the connected providers for a better Learned block. None if unavailable."""
    try:
        from plugins.AiMarketAnalyst.backend.services.ai_router import has_enabled_providers, agent_chat
    except Exception as exc:
        logger.debug(f"[skill-evolve] ai_router unavailable: {exc}")
        return None
    try:
        if not await has_enabled_providers(db):
            logger.info("[skill-evolve] no enabled providers — skipping")
            return None
    except Exception:
        return None

    # Current learned block
    cur_learned = ""
    try:
        p = Path(skill_entry["path"]) / "SKILL.md"
        if p.exists():
            txt = p.read_text(encoding="utf-8")
            if EV_START in txt and EV_END in txt:
                cur_learned = txt.split(EV_START)[1].split(EV_END)[0].strip()
    except Exception:
        cur_learned = ""

    user_prompt = (
        f"Skill: {symbol} ({skill_entry.get('slug') or skill_entry.get('name')}) — {skill_entry.get('asset_class') or ''} {skill_entry.get('group') or ''}\n"
        f"Current Learned block:\n{cur_learned or '(empty — first evolution)'}\n\n"
        f"Scorecard:\n{score.summary()}\n"
    )
    # Also include frontmatter-derived playbook hint (short)
    try:
        fm_desc = (skill_entry.get("frontmatter") or {}).get("description") or ""
        if fm_desc:
            user_prompt += f"\nStock playbook hint: {fm_desc[:200]}\n"
    except Exception:
        pass

    try:
        res = await agent_chat(
            db,
            system_prompt=REWRITE_SKILL_PROMPT,
            user_prompt=user_prompt,
            max_tokens=500,
            agent_name=f"skill-evolve:{symbol}",
            agent_role="skill_evolution",
            source="skill_evolution",
        )
    except Exception as exc:
        logger.warning(f"[skill-evolve] provider call failed for {symbol}: {exc}")
        return None

    if not res.get("ok"):
        logger.info(f"[skill-evolve] provider returned no result for {symbol}")
        return None
    content = res.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            logger.warning(f"[skill-evolve] unparsable rewrite for {symbol}: {content[:200]}")
            return None
    if not isinstance(content, dict):
        return None
    learned = (content.get("learned") or "").strip()
    if not learned:
        return None
    return {
        "learned": learned[:MAX_LEARNED_CHARS],
        "rationale": (content.get("rationale") or "").strip()[:400],
    }


async def evolve_skill(
    db: AsyncSession,
    symbol: str,
    *,
    force: bool = False,
) -> Dict[str, Any]:
    """Score one best-trader skill and rewrite its Learned block if warranted."""
    norm = _norm(symbol)
    slug = f"{norm.lower()}-best-trader"
    # Resolve skill dir
    try:
        from app.hermes_bridge.skill_registry import get_skill_for_symbol, _all_skills_dirs
    except Exception as ex:
        return {"symbol": norm, "changed": False, "reason": f"registry import failed: {ex}"}

    entry = get_skill_for_symbol(norm)
    if not entry:
        return {"symbol": norm, "changed": False, "reason": "skill not found (run bootstrap)"}

    skill_path = Path(entry["path"])
    skill_md = skill_path / "SKILL.md"
    meta_p = skill_path / "metadata.json"
    if not skill_md.exists():
        return {"symbol": norm, "changed": False, "reason": "SKILL.md missing"}

    score = await score_skill(db, norm)
    if score is None:
        return {"symbol": norm, "changed": False, "reason": "score failed"}
    if not score.enough_history and not force:
        return {"symbol": norm, "changed": False, "reason": f"only {score.resolved} resolved (need {MIN_DECISIONS})", "resolved": score.resolved, "win_rate": score.win_rate}

    proposal = await propose_learned(db, norm, entry, score)
    if not proposal:
        return {"symbol": norm, "changed": False, "reason": "no proposal available", "resolved": score.resolved, "win_rate": score.win_rate}

    # Patch SKILL.md — replace between markers
    try:
        txt = skill_md.read_text(encoding="utf-8")
        if EV_START not in txt or EV_END not in txt:
            return {"symbol": norm, "changed": False, "reason": "Learned markers not found in SKILL.md"}
        # Avoid no-op: same text
        cur_block = txt.split(EV_START)[1].split(EV_END)[0].strip()
        if proposal["learned"].strip() == cur_block.strip():
            return {"symbol": norm, "changed": False, "reason": "learned unchanged", "resolved": score.resolved}
        new_txt = txt.split(EV_START)[0] + EV_START + "\n" + proposal["learned"].strip() + "\n" + EV_END + txt.split(EV_END)[1]
        # Also add/update a commented provenance line right after END for audit
        provenance = f"\n<!-- evolved {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} win_rate={score.win_rate:.0% if score.win_rate is not None else 'n/a'} resolved={score.resolved} avg_pnl={score.avg_pnl:+.4f if score.avg_pnl is not None else 'n/a'} rationale: {proposal['rationale'][:120]} -->\n"
        # Insert provenance after EV_END marker
        if "<!-- evolved" not in new_txt:
            new_txt = new_txt.replace(EV_END, EV_END + provenance, 1)
        skill_md.write_text(new_txt, encoding="utf-8")
        # Mirror to other hermes_skills roots (dual write consistency)
        for base in _all_skills_dirs():
            other_md = base / slug / "SKILL.md"
            if other_md != skill_md and other_md.exists():
                try:
                    other_md.write_text(new_txt, encoding="utf-8")
                except Exception:
                    pass
    except Exception as ex:
        logger.warning(f"[skill-evolve] write failed for {norm}: {ex}")
        return {"symbol": norm, "changed": False, "reason": f"write failed: {ex}"}

    # Patch metadata.json
    try:
        meta: Dict[str, Any] = {}
        if meta_p.exists():
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
        meta.update({
            "evolved_at": time.time(),
            "decisions_reviewed": score.resolved,
            "win_rate": score.win_rate,
            "avg_pnl": score.avg_pnl,
            "updated_at": time.time(),
            "last_rationale": proposal["rationale"],
        })
        meta_p.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        # Mirror metadata to other roots
        for base in _all_skills_dirs():
            other_meta = base / slug / "metadata.json"
            if other_meta != meta_p and other_meta.exists():
                try:
                    # keep created_at from original other_meta
                    om = json.loads(other_meta.read_text(encoding="utf-8"))
                    om.update({k: meta[k] for k in ("evolved_at", "decisions_reviewed", "win_rate", "avg_pnl", "updated_at", "last_rationale")})
                    other_meta.write_text(json.dumps(om, indent=2), encoding="utf-8")
                except Exception:
                    pass
    except Exception as ex:
        logger.debug(f"[skill-evolve] meta patch skipped for {norm}: {ex}")

    # Ingest evolution event into FTS for recall (best-effort)
    try:
        from app.hermes_bridge.state_store import _ingest_local
        _ingest_local(
            kind="skill_evolution",
            symbol=norm,
            content=f"[{norm} skill evolved] win_rate={score.win_rate:.0% if score.win_rate else 'n/a'} resolved={score.resolved} rationale: {proposal['rationale'][:200]}",
            meta={"symbol": norm, "slug": slug, "win_rate": score.win_rate, "resolved": score.resolved, "rationale": proposal["rationale"]},
            session_id=slug,
        )
    except Exception as ex:
        logger.debug(f"[skill-evolve] FTS ingest skipped for {norm}: {ex}")

    logger.info(f"🧠 [skill-evolve] {norm}: Learned block evolved (win rate {score.win_rate:.0% if score.win_rate else 'n/a'} over {score.resolved})")
    return {
        "symbol": norm,
        "slug": slug,
        "changed": True,
        "resolved": score.resolved,
        "win_rate": score.win_rate,
        "avg_pnl": score.avg_pnl,
        "rationale": proposal["rationale"],
        "learned": proposal["learned"],
    }


async def evolve_all(db: AsyncSession, *, force: bool = False, limit: int = 8) -> List[Dict[str, Any]]:
    """Evolve up to limit best-trader skills that have enough history."""
    try:
        from app.hermes_bridge.skill_registry import list_skills
        skills = [s for s in list_skills() if s.get("is_best_trader")]
    except Exception as ex:
        logger.debug(f"[skill-evolve] list_skills failed: {ex}")
        return []
    # Prioritize symbols with most resolved (most to learn from), then alphabetically
    results: List[Dict[str, Any]] = []
    for s in skills[: max(limit * 3, limit)]:  # scan a bit more than limit to find eligible
        if len(results) >= limit:
            break
        sym = s.get("symbol") or s["name"].replace("-best-trader", "").upper()
        try:
            r = await evolve_skill(db, sym, force=force)
            # Only count changed or with real reason; skip "only N resolved" quietly unless force
            if r.get("changed") or force or "only" not in (r.get("reason") or ""):
                results.append(r)
            elif r.get("changed") is False and "only" in (r.get("reason") or ""):
                # still record as not-eligible for debug but not in returned list unless force
                pass
        except Exception as ex:
            logger.warning(f"[skill-evolve] {sym} failed: {ex}")
            try:
                await db.rollback()
            except Exception:
                pass
            results.append({"symbol": sym, "changed": False, "reason": str(ex)})
        if len(results) >= limit and not force:
            break
    return results
