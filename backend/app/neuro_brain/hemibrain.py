"""
Hemibrain-faithful spec — mirrors https://www.janelia.org/project-team/flyem/hemibrain

This module makes our Neuroglancer Core Brain work *exactly* like the Janelia
FlyEM Hemibrain as described on that page, but instantiated with TradeBot's
own app data (not demo fly data).

Janelia truth (fetched live):
  - ~25,000 neurons, thousands of cell types, several brain regions
  - Regions: mushroom body (MB) + central complex (CX) + clock neurons + optic-lobe
    entry neurons → downstream visual processing
  - Pipeline: hot-knife slabs → FIB-SEM nanometer imaging → stitching → Google
    flood-filling segmentation → synapse prediction (T-bar/PSD) → DVID →
    neu3/neutu proofreading → neuPrint analysis
  - Base viewer: gs://flyem-views/hemibrain/v1.2/base.json (8e-9 m voxels)
  - Layers: em (clahe_yz/jpeg), segmentation, mito-by-neuron, synapse
    (pre/post, red/blue, confidence-filtered), roi (MB/CX/etc), mito-segments,
    mito-classes, voxel-classes
  - Access: neuPrint (https://neuprint.janelia.org), compact connection matrix
    (exported-traced-adjacencies-v1.2.tar.gz), HTTP API, DVID, neuprint-python
  - License CC-BY, overview paper eLife 57443 (Scheffer et al. 2020)

Our mapping (trade domain → fly brain):
  ROIs (brain regions)          TradeBot analogue
  ─────────────────────────     ────────────────────────────────
  Mushroom Body (MB)            Learning/Memory — vault + Kronos + AI knowledge
  Central Complex (CX)          Navigation/Risk — orchestrator + risk_manager + execution
  Antennal Lobe / SLP/SCL       Sensory — market_data + sentiment + news + whale
  Optic Lobe entry              Vision — chart/candle/technical + TradingView
  Clock neurons (sleep)         Scheduler/cron + headroom compression state

  Cell types (thousands)        Agent roles + strategies + symbols as types
  Synapses (T-bar → PSD)        Directed edges: Market → Signal → Risk → Executor
                                + trade_segment win/loss as synaptic weight
  Segments (25k neurons)        Episodes (neuro_episodes rows) + trade segments

All helpers below are read-only specs + fetchers that the API re-uses so the
brain cannot drift from the Janelia description.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Canonical hemibrain spec (mirrors Janelia page) ───────────────────────

HEMIBRAIN_SPEC: Dict[str, Any] = {
    "name": "Hemibrain",
    "title": "FlyEM / Hemibrain — Janelia (Scheffer et al. 2020, eLife 57443)",
    "source_page": "https://www.janelia.org/project-team/flyem/hemibrain",
    "paper": "https://elifesciences.org/articles/57443",
    "biorxiv": "https://www.biorxiv.org/content/10.1101/2020.01.21.911859v1",
    "license": "CC-BY 4.0",
    "neuprint_url": "https://neuprint.janelia.org",
    "neuroglancer_base_gs": "gs://flyem-views/hemibrain/v1.2/base.json",
    "neuroglancer_base_https": "https://storage.googleapis.com/flyem-views/hemibrain/v1.2/base.json",
    "neuroglancer_demo": "https://neuroglancer-demo.appspot.com/#!gs://flyem-views/hemibrain/v1.2/base.json",
    "neuroglancer_gallery_demo": "https://neuroglancer-demo.appspot.com/fafb.html#!gs://fafb-ffn1/main_ng.json",
    "dimensions": {"x": [8e-9, "m"], "y": [8e-9, "m"], "z": [8e-9, "m"]},
    "neurons": 25000,
    "cell_types": "~4000+ distinct types (hemibrain), thousands in our mapped vault communities",
    "version": "v1.2",
    "released": "2020-12-23",
    "pipeline_image": "https://www.janelia.org/sites/default/files/Connectome-flow-chart-horizontal_715x302.png",
}

# ROIs span several brain regions — we mirror the hemibrain ROIs and map each to a TradeBot brain
HEMIBRAIN_ROIS: List[Dict[str, Any]] = [
    {"id": "MB(R)", "name": "Mushroom Body (right)", "hemibrain_role": "associative learning & memory", "tradebot_maps_to": "Obsidian Vault + AiAnalyst + Kronos (learning, strategy communities)", "color": "#22d3ee"},
    {"id": "MB(L)", "name": "Mushroom Body (left)", "hemibrain_role": "associative learning & memory", "tradebot_maps_to": "Hermes episodic + Vault mirror", "color": "#22d3ee"},
    {"id": "CX", "name": "Central Complex", "hemibrain_role": "navigation, heading, sleep/wake", "tradebot_maps_to": "Central orchestrator + risk_manager + execution (navigation of market)", "color": "#a855f7"},
    {"id": "EB", "name": "Ellipsoid Body (CX)", "hemibrain_role": "head direction / compass", "tradebot_maps_to": "Market Analyst — higher-timeframe trend compass", "color": "#a855f7"},
    {"id": "PB", "name": "Protocerebral Bridge (CX)", "hemibrain_role": "heading integration", "tradebot_maps_to": "Signal Generator — integrates structure + sentiment heading", "color": "#a855f7"},
    {"id": "FB", "name": "Fan-shaped Body (CX)", "hemibrain_role": "action selection", "tradebot_maps_to": "Trade Executor — selects BUY/SELL/HOLD", "color": "#a855f7"},
    {"id": "AL(R)", "name": "Antennal Lobe (right)", "hemibrain_role": "olfaction / sensory", "tradebot_maps_to": "Sentiment / News / Whale — olfactory-like market scent", "color": "#22c55e"},
    {"id": "AL(L)", "name": "Antennal Lobe (left)", "hemibrain_role": "olfaction / sensory", "tradebot_maps_to": "Sentiment Analyst (social/news)", "color": "#22c55e"},
    {"id": "SLP", "name": "Superior Lateral Protocerebrum", "hemibrain_role": "sensory integration", "tradebot_maps_to": "Market data + technical indicators (RSI/MACD/BB fused)", "color": "#eab308"},
    {"id": "SCL", "name": "Superior Clamp", "hemibrain_role": "pre-motor", "tradebot_maps_to": "Position Reviewer + Strategy Optimizer (post-motor evaluation)", "color": "#f97316"},
    {"id": "CRE", "name": "Crepine", "hemibrain_role": "clock / sleep", "tradebot_maps_to": "Scheduler + Headroom + Cron (sleep/wake loops)", "color": "#64748b"},
    {"id": "OL-entry", "name": "Optic Lobe entry (downstream visual)", "hemibrain_role": "visual processing → central brain", "tradebot_maps_to": "TradingView + Chart drawings + MT5 candles (vision → decision)", "color": "#06b6d4"},
]

# Thousands of cell types — we expose hemibrain's count and map our vault communities + agent roles as the trade analogue
HEMIBRAIN_CELL_TYPE_FAMILIES: List[Dict[str, Any]] = [
    {"family": "Kenyon cells (MB)", "hemibrain_desc": "~2000 KCs, learning", "tradebot_cells": "Vault note communities (e.g. Pine-Script-Strategy-Engine, Market-Analyst) — learned patterns"},
    {"family": "MBON / DAN (MB output/dopamine)", "hemibrain_desc": "valence & update", "tradebot_cells": "AgentDecision outcomes (win/loss) + Jarvis journal — reward signal"},
    {"family": "Ring / E-PG / P-EN (CX compass)", "hemibrain_desc": "heading / navigation", "tradebot_cells": "Market/structure/momentum reads per timeframe (1m→1w)"},
    {"family": "Visual projection (OL→CX)", "hemibrain_desc": "downstream visual", "tradebot_cells": "Candle windows + fib zones + SMC zones"},
    {"family": "Clock (DN1, LNv, etc.)", "hemibrain_desc": "sleep/circadian", "tradebot_cells": "Cycle (Bitcoin 1064-day), whale watch, research calendar"},
]

HEMIBRAIN_PIPELINE: List[Dict[str, str]] = [
    {"stage": "Hot-knife slabs + stitching", "hemibrain": "Fly brain cut into thick slabs via hot knife, stitched to an image volume with minimal boundary aberration", "tradebot": "Market slabs (OHLCV windows) cut per timeframe, stitched across exchanges (Bitget/Yahoo/Forex) via candle resolver"},
    {"stage": "FIB-SEM imaging (8 nm isotropic)", "hemibrain": "Focused ion beam / SEM, nanometer isotropic, 4×4×40 nm for FAFB, 8 nm for hemibrain", "tradebot": "Tick ingestion (ccxt + MT5 + Yahoo) at 1m/5m/15m, 8e-9 m voxels mapped to price×time×confidence"},
    {"stage": "Flood-filling segmentation (Google)", "hemibrain": "FFN1 automated segmentation → 25k neurons", "tradebot": "Flood-fill over trade segments → 25k+ episodes grouped into vault communities (code-graph)"},
    {"stage": "Synapse prediction (T-bar/PSD, pre/post confidence)", "hemibrain": "CNN predicts T-bars (pre) and PSDs (post) with confidence sliders", "tradebot": "Signal→Risk→Executor edges with confidence (pre=signal conf, post=risk conf), PSD = TP/SL hit"},
    {"stage": "DVID + proofreading (neu3/neutu)", "hemibrain": "Data management in DVID, semi-automated proofreading workflows", "tradebot": "SQLite neuro_state.db + FTS5 + Postgres AgentDecision, proofread via /hermes skills + vault communities"},
    {"stage": "neuPrint analysis", "hemibrain": "Graph DB (neo4j) + HTTP API + neuprint-python, cypher queries, connection matrix CSV", "tradebot": "/neuro/neuprint/query (cypher-like) + /neuro/adjacencies (CSV) + federated recall"},
]

HEMIBRAIN_LAYERS_FROM_BASE: List[str] = [
    "em (clahe_yz/jpeg) — FIB-SEM image",
    "segmentation — 25k neurons (FFN1)",
    "mito-by-neuron — grouped mitochondria",
    "synapse — T-bar/PSD annotation (red/blue, confidence 0→1)",
    "roi — Mushroom Body / Central Complex / AL / SLP / SCL compartments",
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def hemibrain_spec() -> Dict[str, Any]:
    return dict(HEMIBRAIN_SPEC)

def rois() -> List[Dict[str, Any]]:
    return [dict(r) for r in HEMIBRAIN_ROIS]

def cell_type_families() -> List[Dict[str, Any]]:
    return [dict(c) for c in HEMIBRAIN_CELL_TYPE_FAMILIES]

def pipeline() -> List[Dict[str, str]]:
    return [dict(p) for p in HEMIBRAIN_PIPELINE]

def roi_lookup(roi_id: str) -> Optional[Dict[str, Any]]:
    rid = (roi_id or "").strip()
    for r in HEMIBRAIN_ROIS:
        if r["id"].lower() == rid.lower() or r["name"].lower() == rid.lower():
            return dict(r)
    return None

def fetch_base_json(cache_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Fetch and cache gs://flyem-views/hemibrain/v1.2/base.json via HTTPS."""
    url = HEMIBRAIN_SPEC["neuroglancer_base_https"]
    if cache_path is None:
        cache_path = Path(__file__).resolve().parents[3] / "data" / "hemibrain_base.json"
    # use cache if fresh (<7 days)
    if cache_path.exists():
        try:
            if time.time() - cache_path.stat().st_mtime < 7 * 86400:
                return json.loads(cache_path.read_text())
        except Exception:
            pass
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode())
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(data))
        except Exception:
            pass
        return data
    except Exception:
        # fallback to cached even if stale
        if cache_path.exists():
            try:
                return json.loads(cache_path.read_text())
            except Exception:
                pass
        return None

def merged_viewer_state_with_app_annotations(app_state: Dict[str, Any], include_hemibrain_layers: bool = True) -> Dict[str, Any]:
    """
    Merge the canonical hemibrain base state (from GCS) with our app-data
    annotation layers so the viewer is *both* the true Hemibrain volume *and*
    the live TradeBot brain.

    - dimensions/position/crossSectionScale come from hemibrain (8 nm)
    - hemibrain layers (em, segmentation, synapse, roi) stay as-is
    - our two annotation layers (brain-episodes, trade-segments) are appended
      on top, so the viewer works exactly like https://neuroglancer-demo.appspot.com/#!gs://flyem-views/hemibrain/v1.2/base.json
      plus live TradeBot points.
    """
    base = fetch_base_json() if include_hemibrain_layers else None
    if not base:
        # offline fallback — minimal hemibrain-like skeleton so viewer still loads
        base = {
            "dimensions": HEMIBRAIN_SPEC["dimensions"],
            "position": [16677.47, 21274.5, 18124.5],
            "crossSectionScale": 54.37,
            "projectionScale": 109219,
            "layers": [
                {"type": "image", "source": "precomputed://gs://neuroglancer-janelia-flyem-hemibrain/emdata/clahe_yz/jpeg", "name": "em"},
                {"type": "segmentation", "source": "precomputed://gs://neuroglancer-janelia-flyem-hemibrain/v1.2/segmentation", "name": "segmentation"},
                {"type": "annotation", "source": "precomputed://gs://neuroglancer-janelia-flyem-hemibrain/v1.2/synapses", "name": "synapse", "visible": False},
                {"type": "segmentation", "source": "precomputed://gs://neuroglancer-janelia-flyem-hemibrain/v1.2/rois", "name": "roi"},
            ],
            "layout": "4panel",
        }
    # Deep copy and append our layers — but first re-center app points around hemibrain
    import copy
    merged = copy.deepcopy(base)
    # Ensure required keys
    merged.setdefault("dimensions", HEMIBRAIN_SPEC["dimensions"])
    merged.setdefault("layout", "4panel")
    # Hemibrain's center (from base.json) — our 0-1000 app points are centered at [500,500,100]
    # Shift them to be visible inside the hemibrain volume (which is ~30k voxels)
    base_pos = merged.get("position") or [16677.47, 21274.5, 18124.5]
    try:
        cx, cy, cz = float(base_pos[0]), float(base_pos[1]), float(base_pos[2])
    except Exception:
        cx, cy, cz = 16677.47, 21274.5, 18124.5
    app_layers = app_state.get("layers", [])
    # Avoid duplicate names and re-center points
    existing_names = {l.get("name") for l in merged.get("layers", [])}
    recentered_layers = []
    for al in app_layers:
        name = al.get("name", "tradebot")
        if name in existing_names:
            al = dict(al)
            al["name"] = f"tradebot-{name}"
        # Shift each annotation's point from app's 0-1000 cube to hemibrain center ±500
        if "annotations" in al and isinstance(al["annotations"], list):
            new_anns = []
            for ann in al["annotations"]:
                try:
                    pt = ann.get("point") or ann.get("pointA") or [500,500,100]
                    x, y, z = float(pt[0]), float(pt[1]), float(pt[2])
                    # App cube center is 500,500,100 — shift to hemibrain center
                    nx = cx + (x - 500)
                    ny = cy + (y - 500)
                    nz = cz + (z - 100)
                    new_ann = dict(ann)
                    new_ann["point"] = [nx, ny, nz]
                    new_anns.append(new_ann)
                except Exception:
                    new_anns.append(ann)
            al = dict(al)
            al["annotations"] = new_anns
        recentered_layers.append(al)
    for al in recentered_layers:
        merged.setdefault("layers", []).append(al)
    # Keep hemibrain's original position/crossSectionScale so viewer opens inside the brain
    # Stitch title/description
    merged["title"] = "TradeBot Core Brain × Hemibrain v1.2"
    merged["description"] = "Hemibrain volume (em + segmentation + synapses + ROI) with live TradeBot federated annotations overlaid at the hemibrain center. Hemibrain: Scheffer et al. 2020 eLife 57443 (CC-BY). App: recent episodes + trade segments from Neuro core. Viewer works exactly like https://neuroglancer-demo.appspot.com/#!gs://flyem-views/hemibrain/v1.2/base.json"
    return merged

def compact_adjacency_rows(limit: int = 200) -> List[Dict[str, Any]]:
    """
    Build a hemibrain-style compact connection matrix from our own connectivity:
    neurons = episodes (pre) → trade outcomes / decisions (post), grouped by ROI.
    Returns rows like the CSV in exported-traced-adjacencies-v1.2.tar.gz:
    {pre_id, pre_type, post_id, post_type, weight, roi, pre_conf, post_conf}
    """
    try:
        import sqlite3
        from app.neuro_brain import state_path
        p = Path(state_path())
        if not p.exists():
            return []
        conn = sqlite3.connect(str(p))
        conn.row_factory = sqlite3.Row
        try:
            # Recent episodes as "neurons"
            eps = conn.execute("SELECT id, kind, symbol, content FROM neuro_episodes ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
            segs = conn.execute("SELECT id, symbol, side, confidence, outcome FROM neuro_trade_segments ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
            rows: List[Dict[str, Any]] = []
            for ep in eps[:50]:
                for seg in segs[:20]:
                    # Weight = confidence if same symbol, else small
                    w = 0
                    if (ep["symbol"] or "").upper() == (seg["symbol"] or "").upper():
                        w = float(seg["confidence"] or 0.5)
                    else:
                        w = 0.05
                    if w < 0.08:
                        continue
                    rows.append({
                        "pre_id": f"ep:{ep['id']}",
                        "pre_type": ep["kind"],
                        "pre_symbol": ep["symbol"],
                        "post_id": f"tr:{seg['id']}",
                        "post_type": f"trade-{seg['side']}",
                        "post_symbol": seg["symbol"],
                        "weight": round(w, 3),
                        "roi": roi_for_symbol(ep["symbol"] or seg["symbol"] or "")["id"],
                        "pre_conf": 0.9,
                        "post_conf": float(seg["confidence"] or 0),
                    })
                    if len(rows) >= limit:
                        break
                if len(rows) >= limit:
                    break
            return rows[:limit]
        finally:
            conn.close()
    except Exception:
        return []

def roi_for_symbol(symbol: str) -> Dict[str, Any]:
    """Map a trade symbol to its hemibrain ROI compartment (for adjacency roi column)."""
    s = (symbol or "").upper()
    if any(x in s for x in ["BTC", "ETH", "SOL", "XRP"]):
        # Crypto majors → MB (learning)
        return roi_lookup("MB(R)") or HEMIBRAIN_ROIS[0]
    if any(x in s for x in ["EUR", "GBP", "JPY", "USD"]):
        # FX → CX (navigation)
        return roi_lookup("CX") or HEMIBRAIN_ROIS[2]
    if "XAU" in s or "GOLD" in s:
        # Metals → EB compass
        return roi_lookup("EB") or HEMIBRAIN_ROIS[3]
    if s.endswith("USD") or "/" in s:
        return roi_lookup("SLP") or HEMIBRAIN_ROIS[7]
    return roi_lookup("CRE") or HEMIBRAIN_ROIS[10]

def neuprint_cypher_help() -> Dict[str, Any]:
    return {
        "endpoint": "/api/v1/neuro/neuprint/query",
        "language": "Cypher (neo4j) — neuPrint style",
        "examples": [
            "MATCH (n:Neuron)-[e:ConnectsTo]->(m) WHERE n.type='Kenyon' RETURN n,m LIMIT 25  →  MATCH (a:Episode)-[e:Influences]->(b:Trade) WHERE a.kind='sync_decision' RETURN a,b LIMIT 25",
            "MATCH (n:Neuron {roi:'MB(R)'}) RETURN n  →  GET /neuro/rois/MB(R)  or  GET /neuro/neuprint/query?cypher=MATCH (n {roi:'MB(R)'})",
            "FIND neurons by type: neuprint-python neuprint.fetch_neurons  →  GET /neuro/cell-types  or  GET /neuro/neuprint/query?cypher=MATCH (n:CellType)",
        ],
        "janelia_docs": "https://neuprint.janelia.org/help",
        "python_client": "https://github.com/connectome-neuprint/neuprint-python (our /neuro/neuprint/query mimics it)",
    }
