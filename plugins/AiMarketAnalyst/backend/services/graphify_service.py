"""Graphify integration for the AI agents.

Two uses:
  * runtime — agents query the code/knowledge map to ground a task ("what
    connects to X") via ``query_map`` / ``build_graph_prompt``.
  * visualization — the Intelligence page reads ``graph_overview`` (communities,
    god nodes, counts, report markdown).

Parses ``graphify-out/graph.json`` (networkx node-link format) in-process and
caches it by file mtime, so there is no per-call subprocess cost.
"""
from __future__ import annotations

import json
import os
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any

# repo root: services -> backend -> AiMarketAnalyst -> plugins -> <root>
_REPO_ROOT = Path(__file__).resolve().parents[4]
_GRAPH_PATH = _REPO_ROOT / "graphify-out" / "graph.json"
_REPORT_PATH = _REPO_ROOT / "graphify-out" / "GRAPH_REPORT.md"


def graph_available() -> bool:
    return _GRAPH_PATH.exists()


def _mtime() -> float:
    try:
        return _GRAPH_PATH.stat().st_mtime
    except OSError:
        return 0.0


@lru_cache(maxsize=1)
def _load_cached(mtime: float) -> dict[str, Any]:
    """Load graph.json keyed by mtime so edits invalidate the cache."""
    if not _GRAPH_PATH.exists():
        return {"nodes": [], "links": []}
    with open(_GRAPH_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _graph() -> dict[str, Any]:
    return _load_cached(_mtime())


def graph_overview(top_n: int = 12) -> dict[str, Any]:
    """Counts, community breakdown, and highest-degree 'god' nodes."""
    g = _graph()
    nodes = g.get("nodes") or []
    links = g.get("links") or []
    if not nodes:
        return {"available": False, "nodes": 0, "links": 0, "communities": [], "god_nodes": []}

    degree: Counter[str] = Counter()
    for e in links:
        s, t = e.get("source"), e.get("target")
        if s is not None:
            degree[s] += 1
        if t is not None:
            degree[t] += 1

    label_by_id = {n.get("id"): n for n in nodes}
    god_nodes = [
        {
            "id": nid,
            "label": (label_by_id.get(nid) or {}).get("label", nid),
            "community": (label_by_id.get(nid) or {}).get("community_name"),
            "degree": deg,
            "file": (label_by_id.get(nid) or {}).get("source_file"),
        }
        for nid, deg in degree.most_common(top_n)
    ]

    comm_counter: Counter[str] = Counter()
    for n in nodes:
        comm_counter[n.get("community_name") or "Uncategorized"] += 1
    communities = [
        {"name": name, "nodes": count}
        for name, count in comm_counter.most_common(20)
    ]

    report = ""
    if _REPORT_PATH.exists():
        try:
            report = _REPORT_PATH.read_text(encoding="utf-8")[:8000]
        except OSError:
            report = ""

    return {
        "available": True,
        "nodes": len(nodes),
        "links": len(links),
        "communities": communities,
        "god_nodes": god_nodes,
        "report_md": report,
        "graph_mtime": _mtime(),
    }


def query_map(term: str, limit: int = 8) -> dict[str, Any]:
    """Find nodes matching ``term`` and return their immediate neighbours.

    Used by agents at runtime to map what a symbol/file connects to.
    """
    g = _graph()
    nodes = g.get("nodes") or []
    links = g.get("links") or []
    if not nodes or not term:
        return {"term": term, "matches": [], "neighbours": []}

    t = term.lower()
    matched = [
        n for n in nodes
        if t in str(n.get("label", "")).lower()
        or t in str(n.get("norm_label", "")).lower()
        or t in str(n.get("source_file", "")).lower()
    ][:limit]
    matched_ids = {n.get("id") for n in matched}

    label_by_id = {n.get("id"): n for n in nodes}
    neighbours: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for e in links:
        s, tgt = e.get("source"), e.get("target")
        hit_id = s if s in matched_ids else (tgt if tgt in matched_ids else None)
        if hit_id is None:
            continue
        other = tgt if hit_id == s else s
        key = (hit_id, other, e.get("relation"))
        if key in seen:
            continue
        seen.add(key)
        neighbours.append({
            "from": (label_by_id.get(hit_id) or {}).get("label", hit_id),
            "relation": e.get("relation"),
            "to": (label_by_id.get(other) or {}).get("label", other),
        })
        if len(neighbours) >= limit * 4:
            break

    return {
        "term": term,
        "matches": [
            {"label": n.get("label"), "community": n.get("community_name"), "file": n.get("source_file")}
            for n in matched
        ],
        "neighbours": neighbours,
    }


def build_graph_prompt(term: str, limit: int = 6) -> str:
    """Compact code-map block for injection into an agent prompt."""
    res = query_map(term, limit=limit)
    if not res["matches"] and not res["neighbours"]:
        return ""
    lines = [f"\n\n# Graphify code map for '{term}':"]
    for m in res["matches"][:limit]:
        lines.append(f"- {m['label']} ({m.get('community') or 'n/a'})")
    for nb in res["neighbours"][:limit]:
        lines.append(f"  · {nb['from']} —{nb['relation']}→ {nb['to']}")
    return "\n".join(lines)
