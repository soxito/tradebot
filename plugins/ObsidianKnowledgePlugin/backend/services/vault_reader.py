"""
ObsidianKnowledgePlugin — VaultReader

Reads vault notes and surfaces them as context for agent prompts.

Strategy
────────
1. List all .md files matching criteria (symbol, note_type, date range).
2. Parse frontmatter to filter precisely.
3. Rank by recency and relevance (simple BM25-ish keyword matching).
4. Return a trimmed markdown string within the token budget.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from plugins.ObsidianKnowledgePlugin.backend.config import obsidian_settings


# ─── Frontmatter parser ───────────────────────────────────────────────────────

def _parse_frontmatter(content: str) -> Dict[str, Any]:
    """Extract YAML frontmatter (simple key: value, no nested structures)."""
    if not content.startswith("---"):
        return {}
    end = content.find("---", 3)
    if end == -1:
        return {}
    fm_text = content[3:end].strip()
    result: Dict[str, Any] = {}
    for line in fm_text.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            v = v.strip().strip('"').strip("'")
            result[k.strip()] = v
    return result


def _body_text(content: str) -> str:
    """Return content after the frontmatter block."""
    if not content.startswith("---"):
        return content
    end = content.find("---", 3)
    return content[end + 3:].strip() if end != -1 else content


# ─── BM25 scorer ─────────────────────────────────────────────────────────────

def _tokenize(text: str) -> List[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _bm25_score(query_tokens: List[str], doc_tokens: List[str], k1: float = 1.5, b: float = 0.75, avg_dl: float = 200.0) -> float:
    tf_map: Dict[str, int] = defaultdict(int)
    for t in doc_tokens:
        tf_map[t] += 1
    dl = len(doc_tokens)
    score = 0.0
    for qt in query_tokens:
        tf = tf_map.get(qt, 0)
        if tf == 0:
            continue
        tf_norm = (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avg_dl))
        score += tf_norm
    return score


# ─── VaultReader ─────────────────────────────────────────────────────────────

class VaultReader:
    """Reads vault notes for agent context injection and full-text search."""

    def __init__(self, vault_root: Optional[Path] = None):
        self.root = vault_root or obsidian_settings.vault_path

    # ── Public API ────────────────────────────────────────────────────────────

    def get_context_for_symbol(
        self,
        symbol: str,
        limit: int | None = None,
        token_budget: int | None = None,
    ) -> str:
        """
        Return recent vault notes for *symbol* as a markdown string ready to
        be appended to an agent system prompt.

        Collects: last 3 signal notes + last 3 decision notes + strategy note.
        Trims to token_budget (rough estimate: 1 token ≈ 4 chars).
        """
        limit = limit or obsidian_settings.OBSIDIAN_CONTEXT_NOTES_LIMIT
        budget = token_budget or obsidian_settings.OBSIDIAN_CONTEXT_TOKEN_BUDGET

        notes: List[str] = []

        for note_type in ("signal", "decision"):
            collected = self._collect_by_symbol(symbol, note_type, limit=3)
            notes.extend(collected)

        # Include the strategy note if it exists (best effort)
        strategy_notes = self._collect_by_type("strategy", limit=1)
        notes.extend(strategy_notes)

        if not notes:
            return ""

        # Trim to budget
        assembled = self._assemble(notes, budget)
        logger.debug(f"[VaultReader] Context for {symbol}: {len(notes)} notes, ~{len(assembled)//4} tokens")
        return assembled

    def search_notes(
        self,
        query: str,
        note_type: Optional[str] = None,
        symbol: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        Full-text BM25 search across vault .md files.

        Returns list of dicts with path, note_type, symbol, excerpt, score.
        """
        if not self.root.exists():
            return []

        query_tokens = _tokenize(query)
        results = []

        all_files = list(self.root.rglob("*.md"))
        for fpath in all_files:
            if fpath.name.startswith("_"):
                continue
            try:
                content = fpath.read_text(encoding="utf-8")
            except Exception:
                continue

            fm = _parse_frontmatter(content)
            nt = fm.get("type", "custom")
            sym = fm.get("symbol", None)

            if note_type and nt != note_type:
                continue
            if symbol and sym != symbol:
                continue

            body = _body_text(content)
            doc_tokens = _tokenize(body)
            score = _bm25_score(query_tokens, doc_tokens)
            if score < 0.1:
                continue

            # Excerpt: first 200 chars of body that contain a query token
            excerpt = self._excerpt(body, query_tokens)

            results.append({
                "path": str(fpath.relative_to(self.root)),
                "note_type": nt,
                "symbol": sym,
                "excerpt": excerpt,
                "score": round(score, 3),
            })

        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:limit]

    def list_notes(
        self,
        note_type: Optional[str] = None,
        symbol: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """List vault notes with frontmatter metadata (no body)."""
        if not self.root.exists():
            return []

        result = []
        for fpath in sorted(self.root.rglob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
            if len(result) >= limit:
                break
            try:
                content = fpath.read_text(encoding="utf-8")
            except Exception:
                continue

            fm = _parse_frontmatter(content)
            nt = fm.get("type", "custom")
            sym = fm.get("symbol")

            if note_type and nt != note_type:
                continue
            if symbol and sym != symbol:
                continue

            result.append({
                "path": str(fpath.relative_to(self.root)),
                "note_type": nt,
                "symbol": sym,
                "tags": fm.get("tags", []),
                "frontmatter": fm,
            })
        return result

    def read_note(self, path: str) -> Optional[str]:
        """Read raw markdown content of a note by relative path."""
        fpath = self.root / path
        if not fpath.exists():
            return None
        try:
            return fpath.read_text(encoding="utf-8")
        except Exception:
            return None

    def vault_graph(self) -> Dict[str, Any]:
        """
        Build a lightweight graph of vault notes linked by [[wikilinks]].

        Returns {nodes: [...], edges: [...]}.
        """
        if not self.root.exists():
            return {"nodes": [], "edges": []}

        nodes: Dict[str, Dict] = {}
        edges: List[Dict] = []
        link_re = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]")

        for fpath in self.root.rglob("*.md"):
            try:
                content = fpath.read_text(encoding="utf-8")
            except Exception:
                continue

            rel = str(fpath.relative_to(self.root))
            fm = _parse_frontmatter(content)
            nodes[rel] = {
                "id": rel,
                "label": fpath.stem,
                "note_type": fm.get("type", "custom"),
                "symbol": fm.get("symbol"),
                "tags": fm.get("tags", []),
            }

            body = _body_text(content)
            for match in link_re.finditer(body):
                target = match.group(1).strip()
                # Resolve target to a path
                target_key = f"{target}.md"
                if not target_key.startswith("/"):
                    edges.append({"source": rel, "target": target_key, "label": "links"})

        return {
            "nodes": list(nodes.values()),
            "edges": edges,
            "total_nodes": len(nodes),
            "total_edges": len(edges),
        }

    # ── Internals ─────────────────────────────────────────────────────────────

    def _collect_by_symbol(self, symbol: str, note_type: str, limit: int = 3) -> List[str]:
        """Collect recent notes for a symbol, newest first."""
        # Search under the symbol's subdirectory first for speed
        sym_dir = self.root / f"{note_type}s" / symbol.replace("/", "-")
        search_dirs = [sym_dir] if sym_dir.exists() else [self.root]

        candidates = []
        for sdir in search_dirs:
            for fpath in sorted(sdir.rglob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
                try:
                    content = fpath.read_text(encoding="utf-8")
                    fm = _parse_frontmatter(content)
                    if fm.get("symbol") == symbol and fm.get("type") == note_type:
                        candidates.append(content)
                        if len(candidates) >= limit:
                            break
                except Exception:
                    continue

        return candidates[:limit]

    def _collect_by_type(self, note_type: str, limit: int = 1) -> List[str]:
        notes = []
        type_dir = self.root / (note_type + "s")
        if not type_dir.exists():
            return []
        for fpath in sorted(type_dir.rglob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                content = fpath.read_text(encoding="utf-8")
                notes.append(content)
                if len(notes) >= limit:
                    break
            except Exception:
                continue
        return notes

    def _assemble(self, notes: List[str], token_budget: int) -> str:
        """Join notes into a single string, trimming to token_budget."""
        char_budget = token_budget * 4
        parts = []
        used = 0
        for note in notes:
            body = _body_text(note)[:600]  # cap per note
            if used + len(body) > char_budget:
                remaining = char_budget - used
                if remaining > 50:
                    parts.append(body[:remaining] + "…")
                break
            parts.append(body)
            used += len(body)
        return "\n\n---\n\n".join(parts)

    @staticmethod
    def _excerpt(body: str, query_tokens: List[str]) -> str:
        lower = body.lower()
        best_pos = 0
        for qt in query_tokens:
            pos = lower.find(qt)
            if pos != -1:
                best_pos = max(0, pos - 50)
                break
        return body[best_pos:best_pos + 200].strip()
