"""
ObsidianKnowledgePlugin — VaultWriter

Writes markdown notes to the Obsidian vault directory.

Design principles
─────────────────
• Every note has YAML frontmatter so Dataview can query it.
• Wikilinks [[...]] mirror relationships in the graphify graph.
• Checksum guards: a note is only overwritten when content changes.
• Directory structure is created on demand (mkdirs).
• All paths are relative to vault_root — portable across machines.
"""
from __future__ import annotations

import hashlib
import json
import re
import textwrap
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from plugins.ObsidianKnowledgePlugin.backend.config import obsidian_settings


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _slug(text: str) -> str:
    """Convert text to a filesystem-safe slug."""
    text = re.sub(r"[/\\]", "-", str(text))
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text).strip("-")
    return text[:80]


def _yaml_str(value: Any) -> str:
    if isinstance(value, str):
        if any(c in value for c in (':', '"', "'", "\n", "#")):
            return f'"{value}"'
        return value
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return str(value)


def _frontmatter(fields: Dict[str, Any]) -> str:
    lines = ["---"]
    for k, v in fields.items():
        if isinstance(v, list):
            lines.append(f"{k}:")
            for item in v:
                lines.append(f"  - {_yaml_str(item)}")
        elif v is not None:
            lines.append(f"{k}: {_yaml_str(v)}")
    lines.append("---")
    return "\n".join(lines)


def _checksum(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _write(path: Path, content: str) -> tuple[bool, str]:
    """Write content to path only if it changed. Returns (written, checksum)."""
    cs = _checksum(content)
    if path.exists():
        existing_cs = _checksum(path.read_text(encoding="utf-8"))
        if existing_cs == cs:
            return False, cs
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True, cs


# ─── VaultWriter ─────────────────────────────────────────────────────────────

class VaultWriter:
    """Writes structured markdown notes to the Obsidian vault."""

    def __init__(self, vault_root: Optional[Path] = None):
        self.root = vault_root or obsidian_settings.vault_path
        self.root.mkdir(parents=True, exist_ok=True)

    # ── Internal note path helpers ────────────────────────────────────────────

    def _signal_path(self, symbol: str, note_date: date, note_id: Any) -> Path:
        sym = _slug(symbol)
        return self.root / "signals" / sym / f"{note_date}-signal-{note_id}.md"

    def _decision_path(self, agent: str, symbol: str, note_date: date, note_id: Any) -> Path:
        sym = _slug(symbol)
        ag = _slug(agent)
        return self.root / "decisions" / f"{note_date}-{ag}-{sym}-{note_id}.md"

    def _daily_path(self, d: date) -> Path:
        return self.root / "_daily" / f"{d}.md"

    def _community_path(self, name: str) -> Path:
        return self.root / "communities" / f"{_slug(name)}.md"

    def _strategy_path(self, name: str) -> Path:
        return self.root / "strategies" / f"{_slug(name)}.md"

    def _index_path(self) -> Path:
        return self.root / "_index.md"

    # ── Signal note ───────────────────────────────────────────────────────────

    def write_signal_note(self, signal: Any) -> tuple[Path, bool, str]:
        """
        Write a note for a Signal row.

        Returns (path, was_written, checksum).
        """
        sig_date = (
            signal.timestamp.date()
            if hasattr(signal, "timestamp") and signal.timestamp
            else date.today()
        )
        path = self._signal_path(signal.symbol or "UNKNOWN", sig_date, signal.id)

        action    = getattr(signal, "action", "hold") or "hold"
        source    = getattr(signal, "source", "") or ""
        confidence = getattr(signal, "confidence", None)
        indicators = getattr(signal, "indicators", None) or {}
        raw_data  = getattr(signal, "raw_data", None) or {}

        sym_slug  = _slug(signal.symbol or "UNKNOWN")
        strat_link = f"[[strategies/SMC-Smart-Money]]"

        fm = _frontmatter({
            "type": "signal",
            "symbol": signal.symbol,
            "action": action,
            "confidence": confidence,
            "source": source,
            "timestamp": str(getattr(signal, "timestamp", "")),
            "tags": ["signal", sym_slug, action],
        })

        conf_pct  = f"{confidence * 100:.0f}%" if confidence is not None else "N/A"
        indicators_md = "\n".join(
            f"- **{k}**: {v}" for k, v in (indicators or {}).items()
        ) or "_No indicator data_"

        body = textwrap.dedent(f"""\
            # Signal: {signal.symbol} — {action.upper()} @ {sig_date}

            ## Summary
            **Action**: `{action.upper()}` | **Confidence**: {conf_pct} | **Source**: `{source}`

            ## Technical Indicators
            {indicators_md}

            ## Related
            - {strat_link}
            - [[_daily/{sig_date}]]
        """)

        content = fm + "\n\n" + body
        written, cs = _write(path, content)
        if written:
            logger.debug(f"[ObsidianVault] Wrote signal note: {path.relative_to(self.root)}")
        return path, written, cs

    # ── Agent decision note ───────────────────────────────────────────────────

    def write_decision_note(self, decision: Any) -> tuple[Path, bool, str]:
        """Write a note for an AgentDecision row."""
        dec_date = (
            decision.created_at.date()
            if hasattr(decision, "created_at") and decision.created_at
            else date.today()
        )
        agent_role = getattr(decision, "agent_role", "unknown") or "unknown"
        symbol     = getattr(decision, "symbol", "UNKNOWN") or "UNKNOWN"
        action     = getattr(decision, "recommended_action", "hold") or "hold"
        confidence = getattr(decision, "confidence", None)
        reasoning  = getattr(decision, "reasoning", "") or ""
        ai_called  = getattr(decision, "ai_called", False)
        path = self._decision_path(agent_role, symbol, dec_date, decision.id)

        sym_slug = _slug(symbol)
        fm = _frontmatter({
            "type": "decision",
            "agent": agent_role,
            "symbol": symbol,
            "action": action,
            "confidence": confidence,
            "ai_called": ai_called,
            "date": str(dec_date),
            "tags": ["decision", sym_slug, agent_role, action],
        })

        conf_pct = f"{confidence * 100:.0f}%" if confidence is not None else "N/A"
        reasoning_md = reasoning[:1500] if reasoning else "_No reasoning recorded_"

        # Wrap long lines in reasoning
        reasoning_md = "\n".join(
            textwrap.fill(line, width=100) if len(line) > 100 else line
            for line in reasoning_md.splitlines()
        )

        body = textwrap.dedent(f"""\
            # Decision: {agent_role} → {symbol} — {action.upper()} @ {dec_date}

            ## Summary
            **Agent**: `{agent_role}` | **Action**: `{action.upper()}` | **Confidence**: {conf_pct}
            **AI Called**: {'Yes' if ai_called else 'No (local memory)'}

            ## Reasoning
            {reasoning_md}

            ## Related
            - [[_daily/{dec_date}]]
            - [[signals/{sym_slug}]] ← Recent signals for this symbol
        """)

        content = fm + "\n\n" + body
        written, cs = _write(path, content)
        if written:
            logger.debug(f"[ObsidianVault] Wrote decision note: {path.relative_to(self.root)}")
        return path, written, cs

    # ── Daily note ────────────────────────────────────────────────────────────

    def write_daily_note(self, d: Optional[date] = None) -> tuple[Path, bool, str]:
        """Write / refresh the daily journal note."""
        d = d or date.today()
        path = self._daily_path(d)

        fm = _frontmatter({
            "type": "daily",
            "date": str(d),
            "tags": ["daily", str(d)],
        })

        body = textwrap.dedent(f"""\
            # Daily Journal — {d}

            ## Signals Today
            ```dataview
            TABLE symbol, action, confidence
            FROM "signals"
            WHERE contains(file.name, "{d}")
            SORT confidence DESC
            ```

            ## Agent Decisions Today
            ```dataview
            TABLE agent, symbol, action, confidence, ai_called
            FROM "decisions"
            WHERE date = date("{d}")
            SORT file.ctime DESC
            ```

            ## Notes
            _Add your observations here._
        """)

        content = fm + "\n\n" + body
        written, cs = _write(path, content)
        return path, written, cs

    # ── Graphify community note ───────────────────────────────────────────────

    def write_community_note(
        self,
        community_name: str,
        nodes: List[Dict[str, Any]],
        related_communities: Optional[List[str]] = None,
        description: str = "",
    ) -> tuple[Path, bool, str]:
        """Write a community note mirroring a graphify community."""
        path = self._community_path(community_name)
        today = date.today()

        fm = _frontmatter({
            "type": "community",
            "name": community_name,
            "node_count": len(nodes),
            "synced": str(today),
            "tags": ["community", _slug(community_name)],
        })

        # Build node list limited to 30 entries
        node_lines = []
        for node in nodes[:30]:
            src  = node.get("src", "")
            name = node.get("name", node.get("id", "?"))
            loc  = node.get("loc", "")
            if src:
                node_lines.append(f"- [[{src}|{name}]]" + (f" `{loc}`" if loc else ""))
            else:
                node_lines.append(f"- `{name}`")
        if len(nodes) > 30:
            node_lines.append(f"- … and {len(nodes) - 30} more nodes")

        related_lines = ""
        if related_communities:
            related_lines = "\n## Related Communities\n" + "\n".join(
                f"- [[communities/{_slug(c)}|{c}]]"
                for c in related_communities[:10]
            )

        body = textwrap.dedent(f"""\
            # {community_name}

            > Auto-generated from graphify knowledge graph. Last synced: {today}.
            {('> ' + description) if description else ''}

            ## Key Nodes ({len(nodes)} total)
            {chr(10).join(node_lines)}
            {related_lines}

            ## Notes
            _Add human observations and links here._
        """)

        content = fm + "\n\n" + body
        written, cs = _write(path, content)
        if written:
            logger.debug(f"[ObsidianVault] Wrote community note: {community_name}")
        return path, written, cs

    # ── Strategy note ─────────────────────────────────────────────────────────

    def write_strategy_note(
        self,
        name: str,
        description: str = "",
        signals_used: Optional[List[str]] = None,
        communities: Optional[List[str]] = None,
    ) -> tuple[Path, bool, str]:
        """Write / refresh a strategy reference note."""
        path = self._strategy_path(name)
        today = date.today()

        fm = _frontmatter({
            "type": "strategy",
            "name": name,
            "updated": str(today),
            "tags": ["strategy", _slug(name)],
        })

        sig_links = "\n".join(
            f"- `{s}`" for s in (signals_used or [])
        ) or "_None documented_"

        comm_links = "\n".join(
            f"- [[communities/{_slug(c)}|{c}]]" for c in (communities or [])
        ) or "_None linked_"

        body = textwrap.dedent(f"""\
            # Strategy: {name}

            {description or '_No description yet._'}

            ## Signal Sources
            {sig_links}

            ## Related Communities
            {comm_links}

            ## Performance Notes
            ```dataview
            TABLE symbol, action, pnl, date
            FROM "decisions"
            WHERE contains(tags, "{_slug(name)}")
            SORT date DESC
            LIMIT 20
            ```

            ## Journal
            _Add strategy refinements here._
        """)

        content = fm + "\n\n" + body
        written, cs = _write(path, content)
        return path, written, cs

    # ── Trade outcome note ────────────────────────────────────────────────────

    def write_trade_outcome_note(self, trade: Any) -> tuple[Path, bool, str]:
        """Write a note when a trade closes — captures real PnL."""
        trade_date = (
            trade.closed_at.date()
            if hasattr(trade, "closed_at") and trade.closed_at
            else date.today()
        )
        symbol  = getattr(trade, "symbol", "UNKNOWN") or "UNKNOWN"
        action  = getattr(trade, "side", "unknown") or "unknown"
        pnl     = getattr(trade, "pnl", None)
        pnl_pct = getattr(trade, "pnl_percent", None)

        path = (
            self.root / "trades"
            / _slug(symbol)
            / f"{trade_date}-trade-{trade.id}.md"
        )

        pnl_str = f"{pnl:+.4f} USDT" if pnl is not None else "N/A"
        pnl_pct_str = f"{pnl_pct:+.2f}%" if pnl_pct is not None else ""
        tag = "winner" if (pnl or 0) > 0 else "loser"

        fm = _frontmatter({
            "type": "trade",
            "symbol": symbol,
            "action": action,
            "pnl": pnl,
            "pnl_percent": pnl_pct,
            "date": str(trade_date),
            "tags": ["trade", _slug(symbol), tag],
        })

        body = textwrap.dedent(f"""\
            # Trade: {symbol} {action.upper()} — {trade_date}

            **PnL**: {pnl_str} {pnl_pct_str}

            ## Related
            - [[signals/{_slug(symbol)}]]
            - [[_daily/{trade_date}]]
        """)

        content = fm + "\n\n" + body
        written, cs = _write(path, content)
        if written:
            logger.info(f"[ObsidianVault] Wrote trade note: {symbol} PnL={pnl_str}")
        return path, written, cs

    # ── Generic live-action note ──────────────────────────────────────────────

    def write_action_note(
        self,
        action_type: str,
        symbol: str,
        summary: str,
        detail: str = "",
        tags: Optional[List[str]] = None,
        agent_role: str = "",
        confidence: Optional[float] = None,
        order_id: str = "",
    ) -> tuple[Path, bool, str]:
        """
        Write a live-action note for any Jarvis / agent event:
          agent-decision, signal, trade, tp-set, sl-set, close, etc.

        Creates: live-actions/{date}/{type}-{timestamp}.md
        Designed to be called fire-and-forget from any pipeline.
        """
        import time as _time
        today = date.today()
        ts = int(_time.time() * 1000) % 1_000_000   # 6-digit ms tail for uniqueness
        slug = _slug(f"{action_type}-{symbol or 'system'}-{ts}")
        path = self.root / "live-actions" / str(today) / f"{slug}.md"

        auto_tags = list(set(tags or []) | {action_type, str(today)})
        if symbol:
            auto_tags.append(_slug(symbol))
        if agent_role:
            auto_tags.append(_slug(agent_role))

        fm = _frontmatter({
            "type": "live-action",
            "action_type": action_type,
            "symbol": symbol or None,
            "agent_role": agent_role or None,
            "confidence": confidence,
            "order_id": order_id or None,
            "date": str(today),
            "tags": auto_tags,
        })

        conf_str = f" | conf={confidence:.0%}" if confidence is not None else ""
        body = textwrap.dedent(f"""\
            # {action_type.replace('-', ' ').title()}: {symbol or 'System'}{conf_str}

            **{summary}**

            {detail[:600] if detail else ''}

            ## Context
            - **Type**: `{action_type}`
            - **Symbol**: {symbol or 'N/A'}
            - **Agent**: {agent_role or 'N/A'}
            - **Date**: {today}
            {f'- **Order ID**: {order_id}' if order_id else ''}

            ## Related
            - [[_daily/{today}]]
            - [[_index]]
        """)

        content = fm + "\n\n" + body
        written, cs = _write(path, content)
        if written:
            logger.debug(f"[ObsidianVault] Live action: {action_type} {symbol} → {path.name}")
        return path, written, cs

    def write_insights_snapshot(
        self,
        decisions: list,
        news_articles: list,
        sentiments: list,
        learning_stats: dict,
        pipeline_status: dict,
        paul_knowledge_stats: dict,
    ) -> tuple[Path, bool, str]:
        """
        Write a comprehensive daily brain snapshot from the Insights page.

        Captures: AI decisions, news headlines, sentiment, learning stats, and
        knowledge base metrics into a single structured vault note.

        Created at: insights/_daily-snapshot/{date}.md
        """
        today = date.today()
        path = self.root / "insights" / f"{today}-brain-snapshot.md"

        fm = _frontmatter({
            "type": "insights-snapshot",
            "date": str(today),
            "total_decisions": len(decisions),
            "total_news": len(news_articles),
            "total_sentiments": len(sentiments),
            "tags": ["insights", "snapshot", str(today), "brain"],
        })

        # ── Learning stats summary ────────────────────────────────────────────
        ls = learning_stats or {}
        learn_md = textwrap.dedent(f"""\
            ## AI Learning Stats
            | Metric | Value |
            |--------|-------|
            | Total Decisions | {ls.get('total_decisions', 0):,} |
            | AI Calls | {ls.get('ai_calls', 0):,} |
            | Local Decisions | {ls.get('local_decisions', 0):,} ({ls.get('local_pct', 0):.1f}%) |
            | Win Rate | {ls.get('win_rate', 0):.1f}% |
            | Total PnL | {ls.get('total_pnl', 0):.4f} USDT |
        """)

        # ── Knowledge base stats ──────────────────────────────────────────────
        pk = paul_knowledge_stats or {}
        knowledge_md = textwrap.dedent(f"""\
            ## Knowledge Base (JARVIS)
            | Type | Count |
            |------|-------|
            | Total Knowledge | {pk.get('knowledge_total', 0):,} |
            | News Items | {pk.get('news_items', 0):,} |
            | Insights | {pk.get('insights', 0):,} |
            | Messages Learned | {pk.get('messages_learned', 0):,} |
        """)

        # ── Recent AI decisions ────────────────────────────────────────────────
        dec_lines = []
        for d in decisions[:15]:
            action = d.get("action", "?")
            symbol = d.get("symbol", "?")
            role = d.get("agent_role", "?")
            conf = d.get("confidence", 0)
            ai_called = "🤖" if d.get("ai_called") else "🧠"
            dec_lines.append(f"| {symbol} | {action} | {conf:.0%} | {role} | {ai_called} |")
        decisions_md = ""
        if dec_lines:
            decisions_md = (
                "## Recent AI Decisions (last 15)\n"
                "| Symbol | Action | Confidence | Agent | Source |\n"
                "|--------|--------|-----------|-------|--------|\n"
                + "\n".join(dec_lines) + "\n"
            )

        # ── Sentiment overview ────────────────────────────────────────────────
        sent_lines = []
        for s in sentiments[:10]:
            score = s.get("score", 0)
            label = s.get("label", "neutral")
            symbol = s.get("symbol", "?")
            bar = "🟢" if score > 0.05 else ("🔴" if score < -0.05 else "🟡")
            sent_lines.append(f"| {symbol} | {bar} {label} | {score:.3f} | {s.get('sources_count',0)} |")
        sentiment_md = ""
        if sent_lines:
            sentiment_md = (
                "## Market Sentiment\n"
                "| Symbol | Sentiment | Score | Sources |\n"
                "|--------|-----------|-------|---------|\n"
                + "\n".join(sent_lines) + "\n"
            )

        # ── News headlines ────────────────────────────────────────────────────
        news_lines = []
        for a in news_articles[:10]:
            title = a.get("title", "")[:80]
            source = a.get("source", "?")
            sentiment = a.get("sentiment_label", "neutral") or "neutral"
            news_lines.append(f"- [{title}] ({source}) — *{sentiment}*")
        news_md = ""
        if news_lines:
            news_md = "## Latest News Headlines\n" + "\n".join(news_lines) + "\n"

        # ── Pipeline status ───────────────────────────────────────────────────
        ps = pipeline_status or {}
        pipe_md = f"## Pipeline Status\nRunning: {'Yes' if ps.get('running') else 'No'} | Last run: {ps.get('last_run', 'N/A')}\n"

        body = textwrap.dedent(f"""\
            # Brain Snapshot — {today}

            > Full capture of TradeBot intelligence state from /insights page.
            > Auto-generated by JARVIS self-learning system.

        """) + learn_md + "\n" + knowledge_md + "\n" + decisions_md + sentiment_md + news_md + pipe_md + textwrap.dedent(f"""\

            ## Related
            - [[_daily/{today}]]
            - [[_index]]
        """)

        content = fm + "\n\n" + body
        written, cs = _write(path, content)
        if written:
            logger.info(f"[ObsidianVault] Wrote insights snapshot: {path.name}")
        return path, written, cs

    def write_jarvis_note(
        self,
        question: str,
        answer: str,
        page: str = "/",
        tags: Optional[List[str]] = None,
    ) -> tuple[Path, bool, str]:
        """Write a JARVIS Q&A exchange for self-learning.

        Creates:  jarvis-learning/{date}-{slug}.md
        Each note feeds future context injection so JARVIS improves over time.
        """
        import re as _re
        today = date.today()
        # Slug from first 6 words of question
        slug = _re.sub(r"[^\w\s-]", "", question[:40].lower())
        slug = _re.sub(r"\s+", "-", slug).strip("-")[:40] or "exchange"
        path = self.root / "jarvis-learning" / f"{today}-{slug}.md"

        # Auto-tag from page and question keywords
        auto_tags = ["jarvis-learning", str(today)]
        if tags:
            auto_tags.extend(tags)
        for kw in ("signal", "mt5", "trade", "telegram", "strategy", "sentiment", "rug", "pump"):
            if kw in question.lower() or kw in answer.lower():
                auto_tags.append(kw)

        fm = _frontmatter({
            "type": "jarvis-learning",
            "date": str(today),
            "page": page,
            "question_preview": question[:120],
            "tags": list(set(auto_tags)),
        })

        # Trim answer for storage (keep first 800 chars)
        trimmed_answer = answer[:800] + ("…" if len(answer) > 800 else "")

        body = textwrap.dedent(f"""\
            # JARVIS Learning — {today}

            ## Question
            > {question}

            ## Answer
            {trimmed_answer}

            ## Context
            - **Page**: `{page}`
            - **Captured**: {today}

            ## Related
            - [[_daily/{today}]]
            - [[_index]]
        """)

        content = fm + "\n\n" + body
        written, cs = _write(path, content)
        if written:
            logger.debug(f"[ObsidianVault] Wrote Jarvis learning: {path.name}")
        return path, written, cs

    def write_index_note(self) -> tuple[Path, bool, str]:
        """Write the main _index.md dashboard with Dataview queries."""
        path = self._index_path()
        today = date.today()

        fm = _frontmatter({
            "type": "index",
            "updated": str(today),
            "tags": ["dashboard"],
        })

        body = textwrap.dedent(f"""\
            # TradeBot Brain — Knowledge Dashboard

            > Synced from TradeBot on {today}.
            > Navigate: [[communities]] | [[strategies]] | [[_daily/{today}]]

            ---

            ## Today's Activity
            ```dataview
            TABLE symbol, action, confidence
            FROM "signals"
            WHERE contains(file.name, "{today}")
            SORT confidence DESC
            LIMIT 10
            ```

            ## Recent Decisions (last 7 days)
            ```dataview
            TABLE agent, symbol, action, confidence, ai_called
            FROM "decisions"
            SORT file.ctime DESC
            LIMIT 20
            ```

            ## Winning Trades
            ```dataview
            TABLE symbol, action, pnl, pnl_percent
            FROM "trades"
            WHERE contains(tags, "winner")
            SORT date DESC
            LIMIT 10
            ```

            ## Community Map
            ```dataview
            TABLE node_count, synced
            FROM "communities"
            SORT node_count DESC
            LIMIT 30
            ```

            ---
            _This note is auto-generated. Edit the Journal section below._

            ## Journal
        """)

        content = fm + "\n\n" + body
        written, cs = _write(path, content)
        return path, written, cs
