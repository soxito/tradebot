"""Graphify integration for the AI agents.

Two uses:
  * runtime — agents query the code/knowledge map to ground a task ("what
    connects to X") via ``query_map`` / ``build_graph_prompt``.
  * visualization — the Intelligence page reads ``graph_overview`` (communities,
    god nodes, counts, report markdown) and ``graph_full`` (all nodes+links for
    2D/3D force-directed visualization).

Parses ``graphify-out/graph.json`` (networkx node-link format) in-process and
caches it by file mtime, so there is no per-call subprocess cost.

Active-node tracking: build_graph_prompt() calls ``mark_node_active()`` so the
/intelligence page can pulse nodes that agents are currently using.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter, deque
from functools import lru_cache
from pathlib import Path
from typing import Any, List

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


def _derive_community_name(source_file: str, community_id: Any) -> str:
    """Derive a human-readable community name from source_file path.

    graph.json stores ``community`` as an integer cluster ID with no label.
    We map common path prefixes to meaningful names so the /intelligence
    brain map shows real groups instead of 'Uncategorized'.
    """
    sf = (source_file or "").replace("\\", "/")
    if not sf:
        return f"Community {community_id}"

    # ── Plugin communities ─────────────────────────────────────────────────
    if sf.startswith("plugins/MT5TradingPlugin"):
        return "MT5 Trading Plugin"
    if sf.startswith("plugins/TelegramSignalNewsPlugin"):
        return "Telegram Signal Plugin"
    if sf.startswith("plugins/AiMarketAnalyst"):
        return "AI Market Analyst Plugin"
    if sf.startswith("plugins/AgentPaulPlugin"):
        return "Agent Paul Plugin"

    # ── Backend sub-modules ────────────────────────────────────────────────
    if sf.startswith("backend/app/agents"):
        return "AI Agents"
    if sf.startswith("backend/app/api"):
        return "API Routes"
    if sf.startswith("backend/app/models"):
        return "Data Models"
    if sf.startswith("backend/app/signals"):
        return "Signals Engine"
    if sf.startswith("backend/app/trading"):
        return "Trading Engine"
    if sf.startswith("backend/app/sentiment"):
        return "Sentiment Analysis"
    if sf.startswith("backend/app/exchanges"):
        return "Exchange Connectors"
    if sf.startswith("backend/app/core"):
        return "Core Backend"
    if sf.startswith("backend/app/monitoring"):
        return "Monitoring"
    if sf.startswith("backend/app/workers"):
        return "Background Workers"
    if sf.startswith("backend/app/plugins"):
        return "Plugin Loader"
    if sf.startswith("backend/app"):
        return "Backend App"
    if sf.startswith("backend/tests"):
        return "Tests"
    if sf.startswith("backend/"):
        return "Backend Misc"

    # ── Frontend ────────────────────────────────────────────────────────────
    if sf.startswith("frontend/src/pages"):
        return "Frontend Pages"
    if sf.startswith("frontend/src/components"):
        return "Frontend Components"
    if sf.startswith("frontend/src/hooks"):
        return "Frontend Hooks"
    if sf.startswith("frontend/src/services"):
        return "Frontend Services"
    if sf.startswith("frontend/src/store"):
        return "Frontend Store"
    if sf.startswith("frontend/src/utils"):
        return "Frontend Utils"
    if sf.startswith("frontend/src"):
        return "Frontend"
    if sf.startswith("frontend/"):
        return "Frontend Config"

    # ── GitHub / config ────────────────────────────────────────────────────
    if sf.startswith(".github/agents"):
        return "Agent Configs"
    if sf.startswith(".github/prompts") or sf.startswith(".github/skills"):
        return "Prompt Templates"
    if sf.startswith(".github/workflows"):
        return "CI/CD Workflows"
    if sf.startswith(".github/instructions"):
        return "Instructions"
    if sf.startswith(".github/"):
        return "GitHub Config"

    # ── Top-level files / other ────────────────────────────────────────────
    if sf.startswith("scripts/"):
        return "Scripts"
    if sf.startswith("docs/"):
        return "Documentation"
    if sf.startswith("plugins/"):
        return "Other Plugins"

    # ── Root-level project files ───────────────────────────────────────────
    if sf.endswith(".py") and "/" not in sf:
        return "Root Scripts"        # start.py, etc.
    if sf.endswith(".sh") and "/" not in sf:
        return "Shell Scripts"       # run-local.sh, test-connection.sh
    if sf.endswith(".yml") or sf.endswith(".yaml"):
        return "Docker / Infra"      # docker-compose*.yml
    if sf.endswith(".md") and "/" not in sf:
        return "Project Docs"        # README.md, FIXES.md, etc.
    if sf.endswith(".json") and "/" not in sf:
        return "Config / JSON"       # package.json etc. at root

    return f"Cluster {community_id}"


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

    def _node_comm(n: dict) -> str:
        return _derive_community_name(n.get("source_file", ""), n.get("community", 0))

    god_nodes = [
        {
            "id": nid,
            "label": (label_by_id.get(nid) or {}).get("label", nid),
            "community": _node_comm(label_by_id.get(nid) or {}),
            "degree": deg,
            "file": (label_by_id.get(nid) or {}).get("source_file"),
        }
        for nid, deg in degree.most_common(top_n)
    ]

    comm_counter: Counter[str] = Counter()
    for n in nodes:
        comm_counter[_node_comm(n)] += 1
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
            {
                "label": n.get("label"),
                "community": _derive_community_name(n.get("source_file", ""), n.get("community", 0)),
                "file": n.get("source_file"),
            }
            for n in matched
        ],
        "neighbours": neighbours,
    }


def build_graph_prompt(term: str, limit: int = 6) -> str:
    """Compact code-map block for injection into an agent prompt."""
    res = query_map(term, limit=limit)
    if not res["matches"] and not res["neighbours"]:
        return ""
    # Mark these nodes as active for the live brain-map visualization
    matched_ids = [
        n.get("id") for n in (_graph().get("nodes") or [])
        if term.lower() in str(n.get("label", "")).lower()
    ][:limit]
    if matched_ids:
        mark_node_active(matched_ids)
    lines = [f"\n\n# Graphify code map for '{term}':"]
    for m in res["matches"][:limit]:
        lines.append(f"- {m['label']} ({m.get('community') or 'n/a'})")
    for nb in res["neighbours"][:limit]:
        lines.append(f"  · {nb['from']} —{nb['relation']}→ {nb['to']}")
    return "\n".join(lines)


# ── Active-node tracking (brain-map live pulse) ────────────────────────────

_active_lock = threading.Lock()
_active_deque: deque = deque(maxlen=500)  # (timestamp_float, node_id)


def mark_node_active(node_ids: list[str]) -> None:
    """Record that agents just accessed these node IDs."""
    now = time.time()
    with _active_lock:
        for nid in node_ids:
            _active_deque.append((now, nid))


def get_active_nodes(window_seconds: float = 90.0) -> list[str]:
    """Return node IDs accessed within the last *window_seconds*."""
    cutoff = time.time() - window_seconds
    with _active_lock:
        snapshot = list(_active_deque)
    seen: set[str] = set()
    result: list[str] = []
    for ts, nid in reversed(snapshot):
        if ts < cutoff:
            continue
        if nid not in seen:
            seen.add(nid)
            result.append(nid)
    return result


# ── Full graph for visualization ──────────────────────────────────────────

def graph_full(db_entities: list[dict] | None = None) -> dict[str, Any]:
    """Return all nodes + links trimmed for 2D/3D force-graph rendering.

    Adds optional synthetic 'db_entity' nodes for trades/signals/positions
    so the knowledge map includes live DB state.

    Optionally uses CuPy for degree-centrality computation if a GPU is present;
    falls back to Python Counter otherwise.
    """
    if not graph_available():
        return {"available": False, "nodes": [], "links": [], "node_count": 0, "link_count": 0}

    g = _graph()
    nodes_raw: list[dict] = g.get("nodes") or []
    links_raw: list[dict] = g.get("links") or []

    # Optional CuPy degree computation (CUDA GPU only)
    gpu_accelerated = False
    degree_map: dict[str, int] = {}
    try:
        import cupy as cp  # type: ignore
        # Build adjacency degree array via CuPy
        all_ids = [n.get("id") for n in nodes_raw]
        id_idx = {nid: i for i, nid in enumerate(all_ids)}
        src = [id_idx[e["source"]] for e in links_raw if e.get("source") in id_idx]
        tgt = [id_idx[e["target"]] for e in links_raw if e.get("target") in id_idx]
        if src:
            deg_arr = cp.zeros(len(all_ids), dtype=cp.int32)
            cp.add.at(deg_arr, cp.array(src, dtype=cp.int32), 1)
            cp.add.at(deg_arr, cp.array(tgt, dtype=cp.int32), 1)
            deg_cpu = cp.asnumpy(deg_arr).tolist()
            degree_map = {nid: deg_cpu[i] for i, nid in enumerate(all_ids)}
            gpu_accelerated = True
    except Exception:
        pass  # fall back to Counter below

    if not gpu_accelerated:
        cnt: Counter[str] = Counter()
        for e in links_raw:
            if e.get("source"):
                cnt[e["source"]] += 1
            if e.get("target"):
                cnt[e["target"]] += 1
        degree_map = dict(cnt)

    # Assign stable community → group int for color mapping
    # Use _derive_community_name since graph.json has integer community IDs, not names
    comm_names_set = set()
    for n in nodes_raw:
        comm_names_set.add(_derive_community_name(n.get("source_file", ""), n.get("community", 0)))
    comm_names = sorted(comm_names_set)
    comm_index = {name: i for i, name in enumerate(comm_names)}

    viz_nodes: list[dict] = []
    for n in nodes_raw:
        comm = _derive_community_name(n.get("source_file", ""), n.get("community", 0))
        viz_nodes.append({
            "id": n.get("id"),
            "label": n.get("label") or n.get("id"),
            "community": comm,
            "group": comm_index.get(comm, 0),
            "source_file": n.get("source_file"),
            "node_type": n.get("file_type") or "code",
            "degree": degree_map.get(n.get("id"), 0),
        })

    viz_links: list[dict] = []
    for e in links_raw:
        s, t = e.get("source"), e.get("target")
        if s and t:
            viz_links.append({"source": s, "target": t, "relation": e.get("relation")})

    # Synthetic DB entity nodes — pinned to a dedicated community
    if db_entities:
        db_group = len(comm_names)  # one past last real group
        for ent in db_entities:
            eid = f"db_{ent['type']}_{ent['id']}"
            viz_nodes.append({
                "id": eid,
                "label": ent.get("label", eid),
                "community": "DB Data",
                "group": db_group,
                "source_file": "database",
                "node_type": "db_entity",
                "degree": 0,
                "db_type": ent["type"],
                "db_id": ent["id"],
                "source": ent.get("source"),
            })
            # Link DB node to any matching graph node by symbol
            symbol = ent.get("symbol")
            if symbol:
                for n in nodes_raw:
                    if symbol.lower() in str(n.get("label", "")).lower():
                        viz_links.append({
                            "source": eid,
                            "target": n.get("id"),
                            "relation": "db_relates_to",
                        })
                        break

    return {
        "available": True,
        "nodes": viz_nodes,
        "links": viz_links,
        "node_count": len(viz_nodes),
        "link_count": len(viz_links),
        "communities": [{"name": name, "group": idx} for name, idx in comm_index.items()],
        "gpu_accelerated": gpu_accelerated,
        "graph_mtime": _mtime(),
    }
