"""
Neuroglancer Core Brain API — the federated brain.

  GET  /neuro/health            → core status
  GET  /neuro/overview          → federated snapshot (all brains + agents + repo + stats)
  GET  /neuro/repo              → git version (commit/branch/date)
  POST /neuro/repo/pull         → git fetch --ff-only
  GET  /neuro/search            → federated recall across every brain
  GET  /neuro/graph             → nodes/edges for MemoryGraph / neuro viewer
  GET  /neuro/trades            → recorded trade segments (all pairs, filterable)
  POST /neuro/trades/record     → record a trade segment
  GET  /neuro/analysis          → summary (win rate by pair/asset)
  POST /neuro/analysis/settle   → trigger one settle cycle now
  GET  /neuro/status            → loops + config
"""
from __future__ import annotations

import time
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select, desc, func

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.neuro_brain import is_enabled, repo_version, state_path, local_path, repo_url
from app.neuro_brain.state_store import ensure_store, stats as neuro_stats, search_local as neuro_search, list_segments, analysis_summary, record_trade_segment, prune_expired
from app.neuro_brain.federated_search import federated_search, brain_recall_context
import hashlib as _hl

router = APIRouter(prefix="/neuro", tags=["neuro"])


def _hermes_stats() -> Dict[str, Any]:
    try:
        from app.hermes_bridge import state_path as hermes_path
        from app.hermes_bridge.state_store import ensure_store as hermes_ensure
        p = Path(hermes_path()); hermes_ensure()
        episodes=ep24=0; fts=0
        if p.exists():
            fts=round(p.stat().st_size/1024,1)
            conn=sqlite3.connect(str(p))
            try:
                cur=conn.execute("SELECT COUNT(*) FROM hermes_episodes"); episodes=int(cur.fetchone()[0] or 0)
                cur=conn.execute("SELECT COUNT(*) FROM hermes_episodes WHERE ts>?", (time.time()-86400,)); ep24=int(cur.fetchone()[0] or 0)
            finally: conn.close()
        return {"episodes_total": episodes, "episodes_24h": ep24, "fts_kb": fts, "db_path": str(p)}
    except Exception: return {"episodes_total":0,"episodes_24h":0,"fts_kb":0,"db_path":""}

def _obsidian_stats() -> Dict[str, Any]:
    try:
        from pathlib import Path as _P
        root=_P(local_path()).parents[2] if Path(local_path()).exists() else _P(__file__).resolve().parents[3]
        # Check both actual layout (obsidian-vault/) and legacy (obsidian-vault/tradebot) plus env override
        candidates = [root/"obsidian-vault", root/"obsidian-vault"/"tradebot"]
        try:
            from app.core.config import settings as _s
            vp = getattr(_s, "OBSIDIAN_VAULT_PATH", "") or ""
            if vp:
                import os
                ap = _P(os.path.expanduser(vp))
                if ap.exists():
                    candidates.insert(0, ap)
        except Exception:
            pass
        for vault_root in candidates:
            if vault_root.exists():
                notes=len(list(vault_root.rglob("*.md")))
                if notes>0:
                    size=round(sum(p.stat().st_size for p in vault_root.rglob("*.md"))/1024,1)
                    return {"notes": notes, "kb": size, "path": str(vault_root)}
        # fallback DB count if filesystem empty but DB has notes
        try:
            import sqlite3
            from app.core.database import AsyncSessionLocal as _ASL
            # need sync check — use sqlite file check via DB query count would require async; approximate via filesystem
            pass
        except Exception:
            pass
    except Exception: pass
    return {"notes":0,"kb":0,"path":""}

def _agents_list() -> List[Dict[str,Any]]:
    try:
        from app.agents import room as room_state
        # persona defaults
        persona_map = {**room_state.PERSONAS, "ceo": room_state.CEO_PERSONA}
        out=[]
        for role, persona in persona_map.items():
            out.append({
                "role": role,
                "human_name": persona.get("human_name"),
                "title": persona.get("title"),
                "color": persona.get("color"),
                "seat": persona.get("seat", 99),
                "gender": persona.get("gender","male"),
                "connected": is_enabled(),
                "source": "persona+neuro",
            })
        out.sort(key=lambda a: a["seat"])
        return out
    except Exception: return []

@router.get("/health")
async def neuro_health() -> Dict[str, Any]:
    ensure_store()
    return {"enabled": is_enabled(), "federation": bool(getattr(settings,"NEUROGLANCER_FEDERATION_ENABLED",True)), "repo": repo_version(), "stats": neuro_stats()}

@router.get("/repo")
async def neuro_repo() -> Dict[str, Any]:
    return repo_version()

@router.post("/repo/pull")
async def neuro_repo_pull() -> Dict[str, Any]:
    from app.neuro_brain.repo_sync import pull_repo_once
    return await pull_repo_once()

@router.get("/overview")
async def neuro_overview() -> Dict[str, Any]:
    ensure_store()
    repo = repo_version()
    nstats = neuro_stats()
    hstats = _hermes_stats()
    ostats = _obsidian_stats()
    agents = _agents_list()
    # DB stats
    db_stats: Dict[str,Any]={}
    try:
        from app.models.database import AgentDecision, JarvisAnalysisJournal
        async with AsyncSessionLocal() as db:
            db_stats["agent_decisions"] = int((await db.execute(select(func.count(AgentDecision.id)))).scalar() or 0)
            try:
                db_stats["jarvis_journal"] = int((await db.execute(select(func.count(JarvisAnalysisJournal.id)))).scalar() or 0)
            except Exception: db_stats["jarvis_journal"]=0
            # ai providers
            try:
                from plugins.AiMarketAnalyst.backend.models import AILLMProvider
                db_stats["ai_providers"] = int((await db.execute(select(func.count(AILLMProvider.id)))).scalar() or 0)
            except Exception: db_stats["ai_providers"]=0
    except Exception: pass

    # features — what is linked
    enabled = is_enabled()
    features=[
        {"id":"neuro_core","label":"Neuro Core (google/neuroglancer)","desc":"WebGL volumetric brain + trade segmentation — CORE brain, all others federated through it.","enabled": enabled,"status":"active" if enabled and nstats["episodes_total"]>0 else ("ready" if enabled else "disabled"),"detail": f"{repo.get('commit') or '—'} {repo.get('branch') or ''} · {nstats['episodes_total']} episodes · {nstats['fts_kb']} KB"},
        {"id":"hermes","label":"Hermes Agent (NousResearch)","desc":"Episodic FTS5 + skills + user-model — now federated into neuro core.","enabled": bool(getattr(settings,"HERMES_ENABLED",False)),"status":"linked" if enabled else "standalone","detail": f"hermes {hstats['episodes_total']} episodes · {hstats['fts_kb']} KB"},
        {"id":"obsidian","label":"Obsidian Knowledge Vault","desc":"Vault notes (signals/decisions/strategies/communities/trades) — async exporter, now recalled via neuro.","enabled": True,"status":"linked","detail": f"{ostats['notes']} notes · {ostats['kb']} KB"},
        {"id":"ai_analyst","label":"AiMarketAnalyst Pool","desc":"Multi-provider LLM pool (Groq/NVIDIA/OpenAI…) — neuro brain calls via dynamic slot spread.","enabled": True,"status":"linked","detail": f"{db_stats.get('ai_providers',0)} providers · federation {enabled}"},
        {"id":"agent_decisions","label":"Agent Decisions + Jarvis Journal","desc":"Postgres scoring stays on PG — neuro recalls + mirrors into FTS for federated context.","enabled": True,"status":"linked","detail": f"{db_stats.get('agent_decisions',0)} decisions · {db_stats.get('jarvis_journal',0)} journal"},
        {"id":"trading_room","label":"Trading Room (7 seats + JARVIS chair)","desc":"Every seat recall-enriched by neuro core; every session ingested back into neuro.","enabled": enabled,"status":"active" if enabled else "standalone","detail": f"{len(agents)} personas · Paul/JARVIS/SOX one SOUL"},
        {"id":"hermes_bridge","label":"Hermes ↔ Neuro Bridge","desc":"Hermes episodes mirrored into neuro FTS so federated search sees one timeline.","enabled": enabled and bool(getattr(settings,"HERMES_AUTO_INGEST",True)),"status":"active" if enabled else "off","detail": "session_completed + jarvis turns → both stores"},
    ]

    # skills overview
    skills=[]
    try:
        from app.hermes_bridge.skill_registry import list_skills
        skills=list_skills()
    except Exception: pass

    # trade analysis summary
    trade_sum=analysis_summary(limit=500)

    return {
        "enabled": enabled,
        "federation": bool(getattr(settings,"NEUROGLANCER_FEDERATION_ENABLED",True)),
        "deciding": bool(getattr(settings,"NEUROGLANCER_DECIDING_ENABLED",True)),
        "config": {
            "repo_url": repo_url(),
            "local_path": local_path(),
            "branch": getattr(settings,"NEUROGLANCER_BRANCH","master"),
            "state_path": state_path(),
            "auto_pull": bool(getattr(settings,"NEUROGLANCER_AUTO_PULL",True)),
            "pull_interval_s": int(getattr(settings,"NEUROGLANCER_PULL_INTERVAL_S",21600)),
            "trade_loop_s": int(getattr(settings,"NEUROGLANCER_TRADE_LOOP_S",600)),
            "auto_analyse": bool(getattr(settings,"NEUROGLANCER_AUTO_ANALYSE",True)),
            "sync_interval_s": int(getattr(settings,"NEUROGLANCER_SYNC_INTERVAL_S",300)),
            "deciding_enabled": bool(getattr(settings,"NEUROGLANCER_DECIDING_ENABLED",True)),
        },
        "loops": _loops_status(),
        "repo": repo,
        "stats": nstats,
        "hermes": hstats,
        "obsidian": ostats,
        "db": db_stats,
        "features": features,
        "agents": agents,
        "skills_count": len(skills),
        "skills": skills[:20],
        "trades": trade_sum,
    }

@router.get("/search")
async def neuro_search_api(q: str = Query(..., min_length=2), symbol: Optional[str] = None, limit: int = Query(12, ge=1, le=30)):
    if not q.strip():
        return {"enabled": is_enabled(), "hits": [], "query": q}
    hits = await federated_search(q, symbol, limit)
    if not hits and is_enabled():
        hits = neuro_search(q, symbol, limit)
    return {"enabled": is_enabled(), "hits": hits, "query": q, "symbol": symbol, "federation": bool(getattr(settings,"NEUROGLANCER_FEDERATION_ENABLED",True))}

@router.get("/recall")
async def neuro_recall(symbol: str = Query(..., min_length=1), max_chars: int = Query(700, ge=100, le=2000)):
    ctx = await brain_recall_context(symbol, max_chars)
    return {"symbol": symbol, "context": ctx, "chars": len(ctx)}

@router.get("/graph")
async def neuro_graph(limit: int = Query(80, ge=10, le=300)) -> Dict[str, Any]:
    """Lightweight graph for MemoryGraph / neuro viewer: nodes = brains + recent segments + episodes, edges = federation links."""
    ensure_store()
    nodes: List[Dict[str,Any]]=[]; edges: List[Dict[str,Any]]=[]
    # brain supernodes
    brains=[
        ("neuro-core","Neuro Core",2),("hermes","Hermes",1),("obsidian","Obsidian Vault",1),("ai-analyst","Ai Analyst",1),("agents","Agent Decisions",1),("journal","Jarvis Journal",1),
    ]
    for bid,label,group in brains:
        nodes.append({"id": bid, "label": label, "group": group, "type":"brain", "val": 14 if bid=="neuro-core" else 10})
    # neuro episodes as segments (sample)
    try:
        conn=sqlite3.connect(str(Path(state_path())))
        conn.row_factory=sqlite3.Row
        try:
            kind_rows=conn.execute("SELECT kind, COUNT(*) c FROM neuro_episodes GROUP BY kind").fetchall()
            for r in kind_rows:
                k=r["kind"]; c=int(r["c"])
                nodes.append({"id": f"kind:{k}", "label": f"{k} ({c})", "group":5, "type":"kind", "val": min(10, 2+c**0.5)})
                edges.append({"source":"neuro-core","target":f"kind:{k}", "value": c})
            # recent symbols
            sym_rows=conn.execute("SELECT symbol, COUNT(*) c FROM neuro_episodes WHERE symbol IS NOT NULL GROUP BY symbol ORDER BY c DESC LIMIT 8").fetchall()
            for r in sym_rows:
                sym=r["symbol"]; c=int(r["c"])
                nodes.append({"id": f"sym:{sym}", "label": sym, "group":6, "type":"symbol", "val": 6})
                edges.append({"source":"neuro-core","target":f"sym:{sym}", "value": c})
            # federation links
            for bid,_,_ in brains[1:]:
                edges.append({"source":"neuro-core","target":bid, "value": 3})
        finally: conn.close()
    except Exception: pass
    # trade segments
    segs=list_segments(limit=12)
    for s in segs[:8]:
        sid=f"seg:{s['id']}"; nodes.append({"id": sid, "label": f"{s['symbol']} {s['side']} {s.get('outcome') or 'pending'}", "group": 7, "type":"segment", "meta": s})
        edges.append({"source": f"sym:{s['symbol']}" if any(n["id"]==f"sym:{s['symbol']}" for n in nodes) else "neuro-core", "target": sid, "value": 1})
    return {"nodes": nodes[:limit], "edges": edges, "counts": {"nodes": len(nodes), "edges": len(edges)}}

# ── Trades (record + analyse all pairs) ────────────────────────────────
class RecordTradeBody(BaseModel):
    symbol: str
    side: str  # long/short or buy/sell
    entry: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    confidence: Optional[float] = None
    price_at_signal: Optional[float] = None
    timeframe: Optional[str] = None
    asset_class: Optional[str] = None
    exchange: Optional[str] = None
    source: str = "neuro"
    agent_role: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None

@router.post("/trades/record")
async def neuro_trades_record(body: RecordTradeBody) -> Dict[str, Any]:
    side = body.side.lower()
    if side in ("buy","long","longs"): side="long"
    elif side in ("sell","short","shorts"): side="short"
    seg_id = record_trade_segment(
        symbol=body.symbol, side=side, entry=body.entry, stop_loss=body.stop_loss, take_profit=body.take_profit,
        confidence=body.confidence, price_at_signal=body.price_at_signal, timeframe=body.timeframe or "", asset_class=body.asset_class or "", exchange=body.exchange or "", source=body.source, agent_role=body.agent_role or "", meta=body.meta,
    )
    if seg_id is None:
        return {"ok": False, "reason": "neuro disabled or store failed"}
    return {"ok": True, "segment_id": seg_id, "symbol": body.symbol.upper(), "side": side}

@router.get("/trades")
async def neuro_trades_list(limit: int = Query(50, ge=1, le=200), symbol: Optional[str]=None, outcome: Optional[str]=None, asset_class: Optional[str]=None) -> Dict[str, Any]:
    segs=list_segments(limit=limit, symbol=symbol, outcome=outcome, asset_class=asset_class)
    return {"segments": segs, "count": len(segs), "enabled": is_enabled()}

@router.get("/analysis")
async def neuro_analysis(limit: int = Query(200, ge=20, le=1000)) -> Dict[str, Any]:
    ensure_store()
    s=analysis_summary(limit=limit)
    # also include hermes neuro stats for the card
    return {"enabled": is_enabled(), **s, "repo": repo_version(), "loops": _loops_status()}

@router.post("/analysis/settle")
async def neuro_analysis_settle(limit: int = Query(8, ge=1, le=20)) -> Dict[str, Any]:
    """One-off settle sweep (same logic as background loop, but on-demand)."""
    from app.neuro_brain.state_store import list_segments, mark_outcome
    pend=[s for s in list_segments(limit=40) if not s.get("outcome")]
    settled=0
    checked=0
    for seg in pend[:limit]:
        try:
            sym=seg.get("symbol") or ""; entry=float(seg.get("entry") or 0); sl=float(seg.get("stop_loss") or 0); tp=float(seg.get("take_profit") or 0)
            side=(seg.get("side") or "long").lower(); tf=seg.get("timeframe") or "1h"
            if not sym or not entry or not sl or not tp: continue
            checked+=1
            # candles via services
            candles=[]
            try:
                from app.services import market_data as md
                if hasattr(md,"fetch_candles"): candles=await md.fetch_candles(sym, tf)  # type: ignore
            except Exception: pass
            if not candles:
                try:
                    from app.services import candles as cs
                    candles=await cs.fetch(sym, tf)
                except Exception: candles=[]
            if not candles: continue
            hit=None
            for c in candles[-20:]:
                h,l=(float(c[2]),float(c[3])) if isinstance(c,(list,tuple)) else (float(c.get("high") or c.get("h") or 0), float(c.get("low") or c.get("l") or 0))
                if not h or not l: continue
                if side in ("long","buy"):
                    if l<=sl: hit="loss"; break
                    if h>=tp: hit="win"; break
                else:
                    if h>=sl: hit="loss"; break
                    if l<=tp: hit="win"; break
            if hit and mark_outcome(int(seg["id"]), hit):
                settled+=1
        except Exception: continue
    return {"checked": checked, "settled": settled, "summary": analysis_summary(limit=200)}

def _loops_status() -> Dict[str,Any]:
    try:
        from app.neuro_brain.repo_sync import trade_loop_status, pull_loop_status
        from app.neuro_brain.sync import sync_loop_status
        return {"trade_loop": trade_loop_status(), "pull_loop": pull_loop_status(), "sync_loop": sync_loop_status(), "deciding": bool(getattr(settings, "NEUROGLANCER_DECIDING_ENABLED", True))}
    except Exception:
        try:
            from app.neuro_brain.repo_sync import trade_loop_status, pull_loop_status
            return {"trade_loop": trade_loop_status(), "pull_loop": pull_loop_status()}
        except Exception: return {}

@router.get("/status")
async def neuro_status() -> Dict[str, Any]:
    ensure_store()
    return {"enabled": is_enabled(), "federation": bool(getattr(settings,"NEUROGLANCER_FEDERATION_ENABLED",True)), "repo": repo_version(), "stats": neuro_stats(), "loops": _loops_status(), "config": {"auto_pull": bool(getattr(settings,"NEUROGLANCER_AUTO_PULL",True)), "pull_interval_s": int(getattr(settings,"NEUROGLANCER_PULL_INTERVAL_S",21600)), "trade_loop_s": int(getattr(settings,"NEUROGLANCER_TRADE_LOOP_S",600))}}

@router.post("/sync")
async def neuro_sync(limit_per_brain: int = Query(30, ge=5, le=100)) -> Dict[str, Any]:
    """Manually trigger a full federated sync — mirrors every other brain into Neuro."""
    from app.neuro_brain.sync import sync_once
    return await sync_once(limit_per_brain=limit_per_brain)


@router.post("/decide")
async def neuro_decide_preview(
    symbol: str = Query(..., description="Symbol to review e.g. BTC/USDT"),
    action: str = Query(..., description="Tentative action buy/sell/hold"),
    confidence: float = Query(0.5, ge=0.0, le=1.0),
    timeframe: str = Query("1h"),
) -> Dict[str, Any]:
    """Preview what the deciding brain would do with a tentative verdict."""
    from app.neuro_brain.sync import deciding_review
    return await deciding_review(symbol=symbol, tentative_action=action, tentative_confidence=confidence, timeframe=timeframe)


@router.get("/viewer-state")
async def neuro_viewer_state(
    limit: int = Query(120, ge=10, le=300),
    include_demo: bool = Query(False, description="If true, also include demo FAFB reference layer"),
    hemibrain: bool = Query(True, description="If true, base viewer on Janelia Hemibrain v1.2 (em+segmentation+synapses+ROI) with app annotations overlaid — works exactly like https://www.janelia.org/project-team/flyem/hemibrain"),
) -> Dict[str, Any]:
    """
    Neuroglancer viewer state — **Hemibrain-faithful** by default.

    *With hemibrain=true (default)* the state is the canonical
    `gs://flyem-views/hemibrain/v1.2/base.json` (em + segmentation + mito +
    synapse + ROI, 8 nm voxels, 25k neurons — eLife 57443, CC-BY) **plus**
    two live annotation layers built from the app's *real* data:

      - brain-episodes (federated recall mirror, cyan)
      - trade-segments (all pairs, amber)

    So the viewer is *both* the true Hemibrain volume (FIB-SEM → flood-fill
    → DVID → neuPrint) *and* your TradeBot brain. With hemibrain=false it
    returns the minimal app-only state (previous behaviour).

    Load via https://neuroglancer-demo.appspot.com/#!<encoded-json>
    or http://localhost:1448/api/v1/neuro/viewer-state (CORS).
    """
    ensure_store()
    # Fetch recent app data
    try:
        from app.neuro_brain.state_store import list_segments
        segs = list_segments(limit=limit)
    except Exception:
        segs = []
    try:
        import sqlite3
        conn = sqlite3.connect(str(Path(state_path())))
        conn.row_factory = sqlite3.Row
        try:
            kind_rows = conn.execute("SELECT ts, kind, symbol, content FROM neuro_episodes ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
            episodes = [dict(r) for r in kind_rows]
        finally:
            conn.close()
    except Exception:
        episodes = []

    def _h(s: str, mod: int = 1000) -> int:
        if not s:
            return 0
        return int(_hl.md5(s.encode()).hexdigest()[:8], 16) % mod

    def _episode_point(ep: Dict[str, Any], idx: int):
        ts = float(ep.get("ts") or 0)
        x = (ts % 86400) / 86.4
        y = _h(ep.get("symbol") or f"nosym-{idx}", 1000)
        z = _h(ep.get("kind") or "unknown", 200) + (idx % 5) * 2
        x = max(5, min(995, x))
        y = max(5, min(995, y))
        z = max(5, min(395, z))
        return [float(x), float(y), float(z)]

    def _trade_point(seg: Dict[str, Any], idx: int):
        ts = float(seg.get("ts") or 0)
        x = (ts % 86400) / 86.4
        y = _h(seg.get("symbol") or f"trade-{idx}", 1000)
        conf = float(seg.get("confidence") or 0.5)
        z = 200 + conf * 150 + (idx % 3) * 5
        x = max(5, min(995, x))
        y = max(5, min(995, y))
        z = max(5, min(395, z))
        return [float(x), float(y), float(z)]

    annotations_brain = []
    for i, ep in enumerate(episodes[:limit]):
        pt = _episode_point(ep, i)
        kind = (ep.get("kind") or "episode")[:28]
        sym = (ep.get("symbol") or "")[:16]
        content = (ep.get("content") or "")[:120].replace("\n", " ")
        annotations_brain.append({
            "point": pt,
            "type": "point",
            "id": f"ep-{i}",
            "description": f"[{kind}] {sym} — {content}",
        })
    annotations_trades = []
    for i, seg in enumerate(segs[:limit]):
        pt = _trade_point(seg, i)
        sym = (seg.get("symbol") or "?")[:12]
        side = (seg.get("side") or "?")[:4]
        outcome = seg.get("outcome") or "pending"
        entry = seg.get("entry")
        tp = seg.get("take_profit")
        sl = seg.get("stop_loss")
        annotations_trades.append({
            "point": pt,
            "type": "point",
            "id": f"tr-{seg.get('id', i)}",
            "description": f"[{sym} {side.upper()} {outcome}] entry={entry} TP={tp} SL={sl}",
        })
    if not annotations_brain and not annotations_trades:
        annotations_brain = [{
            "point": [500, 500, 100],
            "type": "point",
            "id": "placeholder",
            "description": "No app data yet — run a Trading Room session, record a trade, or wait for auto-sync (POST /neuro/sync). This viewer will then show your real brain episodes + trade segments overlaid on the Hemibrain.",
        }]

    # Build the app layers (always)
    app_layers = [
        {
            "type": "annotation",
            "name": "brain-episodes",
            "source": {"url": "local://annotations"},
            "tool": "annotatePoint",
            "tab": "annotations",
            "annotationColor": "#22d3ee",
            "annotations": annotations_brain,
        },
        {
            "type": "annotation",
            "name": "trade-segments",
            "source": {"url": "local://annotations"},
            "tool": "annotatePoint",
            "tab": "annotations",
            "annotationColor": "#f59e0b",
            "annotations": annotations_trades,
        },
    ]
    if include_demo:
        app_layers.append({
            "type": "image",
            "name": "fafb-demo-reference (gallery)",
            "source": "precomputed://gs://neuroglancer-public-data/flyem_fib-25/image",
            "visible": False,
            "opacity": 0.15,
        })

    # Hemibrain-faithful merge: start from the canonical base.json (Janelia)
    # so the viewer works *exactly* like https://www.janelia.org/project-team/flyem/hemibrain
    # — FIB-SEM → flood-fill → DVID → synapse/ROI — plus our live points.
    if hemibrain:
        try:
            from app.neuro_brain.hemibrain import merged_viewer_state_with_app_annotations
            # Re-use the two-layer app state we just built as the app_state arg
            app_state = {
                "dimensions": {"x": [1, ""], "y": [1, ""], "z": [1, ""]},
                "position": [500, 500, 100],
                "crossSectionScale": 5,
                "projectionScale": 2048,
                "layers": app_layers,
                "layout": "4panel",
            }
            merged = merged_viewer_state_with_app_annotations(app_state, include_hemibrain_layers=True)
            return JSONResponse(content=merged, headers={"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, OPTIONS", "Access-Control-Allow-Headers": "*"})
        except Exception as e:
            # Fall back to app-only if hemibrain fetch fails (offline)
            pass

    # App-only fallback (hemibrain=false or fetch failed)
    state = {
        "dimensions": {"x": [1, ""], "y": [1, ""], "z": [1, ""]},
        "position": [500, 500, 100],
        "crossSectionScale": 5,
        "projectionScale": 2048,
        "projectionOrientation": [0.5, 0.5, 0.5, 0.5],
        "layers": app_layers,
        "layout": "4panel",
        "title": "TradeBot Core Brain — app data",
        "description": f"Live from {len(episodes)} episodes + {len(segs)} trade segments (real app data). Use the annotation tabs to inspect each point.",
    }
    return JSONResponse(content=state, headers={"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, OPTIONS", "Access-Control-Allow-Headers": "*"})


@router.get("/hemibrain")
async def neuro_hemibrain() -> Dict[str, Any]:
    """Hemibrain spec — exactly as https://www.janelia.org/project-team/flyem/hemibrain describes."""
    from app.neuro_brain.hemibrain import hemibrain_spec, rois, cell_type_families, pipeline, HEMIBRAIN_SPEC
    return {
        "spec": hemibrain_spec(),
        "rois": rois(),
        "cell_families": cell_type_families(),
        "pipeline": pipeline(),
        "layers": HEMIBRAIN_SPEC.copy(),
    }


@router.get("/rois")
async def neuro_rois(roi_id: Optional[str] = Query(None, description="Filter to one ROI e.g. MB(R), CX, AL(R)")) -> Dict[str, Any]:
    from app.neuro_brain.hemibrain import rois, roi_lookup
    if roi_id:
        one = roi_lookup(roi_id)
        if not one:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail=f"ROI {roi_id} not found")
        return {"roi": one}
    return {"rois": rois(), "count": len(rois())}


@router.get("/cell-types")
async def neuro_cell_types() -> Dict[str, Any]:
    from app.neuro_brain.hemibrain import cell_type_families, hemibrain_spec
    spec = hemibrain_spec()
    return {"families": cell_type_families(), "count_families": len(cell_type_families()), "neurons": spec["neurons"], "cell_types": spec["cell_types"], "hemibrain_paper": spec["paper"]}


@router.get("/pipeline")
async def neuro_pipeline() -> Dict[str, Any]:
    from app.neuro_brain.hemibrain import pipeline, HEMIBRAIN_SPEC
    return {"pipeline": pipeline(), "image": HEMIBRAIN_SPEC.get("pipeline_image"), "base_gs": HEMIBRAIN_SPEC["neuroglancer_base_gs"]}


@router.get("/neuprint/query")
async def neuro_neuprint_query(
    cypher: str = Query(..., description="Cypher-like query, e.g. MATCH (n:Neuron)-[e:ConnectsTo]->(m) RETURN n,m LIMIT 5"),
    limit: int = Query(25, ge=1, le=100),
) -> Dict[str, Any]:
    """
    neuPrint-like query over the TradeBot connectome — mimics https://neuprint.janelia.org
    Translates a tiny Cypher subset to our federated store / SQLite.
    Examples from Janelia docs:
      MATCH (n:Neuron) RETURN n LIMIT 5  →  returns recent episodes as neurons
      MATCH (n {roi:'MB(R)'})            →  filtered by ROI
    """
    from app.neuro_brain.hemibrain import neuprint_cypher_help, roi_for_symbol
    # Very small parser: look for roi, symbol, kind, limit in the cypher string
    q = (cypher or "").lower()
    import re
    # Extract roi:'XYZ' if present
    m = re.search(r"roi\s*:\s*'([^']+)'|roi\s*=\s*['\"]([^'\"]+)['\"]", cypher, re.IGNORECASE)
    roi_filter = (m.group(1) or m.group(2)) if m else None
    # Extract symbol hint
    m2 = re.search(r"symbol\s*:\s*'([^']+)'|symbol\s*=\s*['\"]([^'\"]+)['\"]", cypher, re.IGNORECASE)
    sym_filter = (m2.group(1) or m2.group(2)) if m2 else None
    # Extract kind
    m3 = re.search(r"kind\s*:\s*'([^']+)'", cypher, re.IGNORECASE)
    kind_filter = m3.group(1) if m3 else None

    # Federated search as the neuprint backend
    try:
        # If cypher mentions ConnectsTo, return adjacency rows
        if "connectsto" in q:
            from app.neuro_brain.hemibrain import compact_adjacency_rows
            rows = compact_adjacency_rows(limit=limit)
            # filter by roi if requested
            if roi_filter:
                rows = [r for r in rows if r.get("roi","").lower() == roi_filter.lower()]
            return {"cypher": cypher, "count": len(rows), "rows": rows, "help": neuprint_cypher_help()}
        # Otherwise treat as neuron lookup — return episodes as neurons
        import sqlite3
        from pathlib import Path as _P
        from app.neuro_brain import state_path as _sp
        p = _P(_sp())
        neurons: List[Dict[str, Any]] = []
        if p.exists():
            conn = sqlite3.connect(str(p))
            conn.row_factory = sqlite3.Row
            try:
                sql = "SELECT id, kind, symbol, content, ts FROM neuro_episodes WHERE 1=1"
                params: List[Any] = []
                if sym_filter:
                    sql += " AND symbol LIKE ?"
                    params.append(f"%{sym_filter}%")
                if kind_filter:
                    sql += " AND kind LIKE ?"
                    params.append(f"%{kind_filter}%")
                sql += " ORDER BY ts DESC LIMIT ?"
                params.append(limit)
                for r in conn.execute(sql, params).fetchall():
                    neurons.append({
                        "id": f"ep:{r['id']}",
                        "type": r["kind"],
                        "symbol": r["symbol"],
                        "roi": roi_for_symbol(r["symbol"] or "")["id"],
                        "content": (r["content"] or "")[:200],
                        "ts": r["ts"],
                    })
                # ROI filter post-hoc if requested without DB column
                if roi_filter:
                    neurons = [n for n in neurons if n["roi"].lower() == roi_filter.lower()]
            finally:
                conn.close()
        return {"cypher": cypher, "count": len(neurons), "neurons": neurons, "help": neuprint_cypher_help()}
    except Exception as e:
        return {"cypher": cypher, "error": str(e), "help": neuprint_cypher_help()}


@router.get("/adjacencies")
async def neuro_adjacencies(limit: int = Query(100, ge=10, le=500), format: str = Query("json", description="json or csv")) -> Any:
    """
    Compact connection matrix — hemibrain-style `exported-traced-adjacencies-v1.2`
    Derived from our own episodic → trade edges (pre_id → post_id, weight = confidence).
    With format=csv it streams a CSV like Janelia's https://storage.cloud.google.com/hemibrain/v1.2/exported-traced-adjacencies-v1.2.tar.gz
    """
    from app.neuro_brain.hemibrain import compact_adjacency_rows
    rows = compact_adjacency_rows(limit=limit)
    if format.lower() == "csv":
        import io, csv
        from fastapi.responses import StreamingResponse
        output = io.StringIO()
        w = csv.DictWriter(output, fieldnames=["pre_id","pre_type","pre_symbol","post_id","post_type","post_symbol","weight","roi","pre_conf","post_conf"])
        w.writeheader()
        for r in rows:
            w.writerow(r)
        output.seek(0)
        return StreamingResponse(iter([output.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=tradebot-adjacencies.csv", "Access-Control-Allow-Origin": "*"})
    return {"count": len(rows), "rows": rows, "hemibrain_source": "https://storage.cloud.google.com/hemibrain/v1.2/exported-traced-adjacencies-v1.2.tar.gz"}


@router.post("/prune")
async def neuro_prune(days: int = Query(90, ge=7, le=365)) -> Dict[str, Any]:
    n=prune_expired(days)
    return {"deleted": n, "days": days}

