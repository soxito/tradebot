"""
ObsidianKnowledgePlugin — Sync Orchestrator

Coordinates a full vault sync: signals + decisions + communities.
Designed to be called both:
  • On-demand via POST /plugins/obsidian-knowledge/sync
  • Periodically via FastAPI startup background task
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from plugins.ObsidianKnowledgePlugin.backend.config import obsidian_settings
from plugins.ObsidianKnowledgePlugin.backend.models import VaultNote
from plugins.ObsidianKnowledgePlugin.backend.services.vault_writer import VaultWriter
from plugins.ObsidianKnowledgePlugin.backend.services.obsidian_rest import get_bridge


# ─── Community extractor ─────────────────────────────────────────────────────

def _find_graphify_pair() -> tuple[Optional[Path], Optional[Path]]:
    """
    Find a consistent (graph.json, GRAPH_REPORT.md) pair from graphify-out.

    Prefers an *archive* sub-directory where both files were written in the same
    graphify run (so community IDs match names).  Falls back to the top-level
    files only when no archive is available, but warns that they may be stale.

    Returns (graph_path, report_path) — either or both can be None.
    """
    import re as _re

    # Try: graphify-out/  then  ../graphify-out/
    for base in (Path("graphify-out"), Path("../graphify-out")):
        if not base.exists():
            continue

        # Collect archive subdirectories (YYYY-MM-DD format)
        archives = sorted(
            [d for d in base.iterdir()
             if d.is_dir() and _re.match(r"\d{4}-\d{2}-\d{2}", d.name)],
            reverse=True,      # newest first
        )

        for archive in archives:
            gp = archive / "graph.json"
            rp = archive / "GRAPH_REPORT.md"
            if gp.exists() and rp.exists():
                return gp, rp

        # Fall back to top-level files
        gp_top = base / "graph.json"
        rp_top = base / "GRAPH_REPORT.md"
        if gp_top.exists():
            if not rp_top.exists():
                logger.warning(
                    "[ObsidianSync] Using top-level graph.json without a matching "
                    "GRAPH_REPORT.md — community names may be stale."
                )
            return gp_top, (rp_top if rp_top.exists() else None)

    return None, None


def _infer_community_name(comm_id: int, node_list: List[Dict[str, Any]], known_names: Dict[int, str]) -> Optional[str]:
    """
    Infer a human-readable name for a community from its node labels.

    Strategy (highest priority first):
    1. Known name from GRAPH_REPORT.md
    2. Source file name (most common .py/.tsx/.ts file in nodes)
    3. Class/module prefix from labels (e.g. "UsageService" → "Usage Service")
    4. Skip micro-communities or fully anonymous ones
    """
    if comm_id in known_names:
        return known_names[comm_id]

    # Skip micro-communities (too small to be meaningful)
    if len(node_list) < 3:
        return None

    labels = [n.get("name", n.get("label", "")) for n in node_list]
    srcs   = [n.get("src", "") for n in node_list if n.get("src")]

    # ── Anonymous builtins filter ───────────────────────────────────────────────
    SKIP_LABELS = {"str", "int", "float", "bool", "list", "dict", "Any",
                   "None", "Exception", "BaseModel", "DeclarativeBase",
                   "Enum", "datetime", "Optional", "List", "Dict"}
    real_labels = [l for l in labels if l not in SKIP_LABELS and len(l) > 2
                   and not l.startswith("//") and not l.startswith("http")]
    if len(real_labels) < 2:
        return None

    from collections import Counter
    import re as _re

    # ── Strategy 1: Derive from source file names ──────────────────────────────
    if srcs:
        file_names = Counter()
        for s in srcs:
            p = Path(s)
            # Strip extension and common suffixes
            stem = p.stem
            if stem in ("__init__", "index", "main", "router", "models", "schemas",
                        "utils", "types", "config", "constants", "helpers"):
                # Use parent directory instead
                parent_parts = p.parts
                if len(parent_parts) >= 2:
                    stem = parent_parts[-2].replace("_", " ").replace("-", " ")
            file_names[stem] += 1

        if file_names:
            top_file, top_count = file_names.most_common(1)[0]
            if top_count >= max(1, len(srcs) * 0.25):
                # Convert snake_case filename to Title Case
                name_parts = _re.split(r"[_\-\.]", top_file)
                readable = " ".join(p.title() for p in name_parts if p)
                if readable and readable not in ("Py", "Tsx", "Ts", "Js"):
                    return f"{readable} (c{comm_id})"

    # ── Strategy 2: Derive from class/module names in labels ──────────────────
    # Extract CamelCase class names or module files from labels
    class_votes: Counter = Counter()
    file_votes: Counter = Counter()

    for label in real_labels:
        # If label is a .py/.tsx file → use stem
        if _re.search(r"\.(py|tsx?|js)$", label):
            stem = Path(label).stem
            if stem not in ("__init__", "index", "main", "router", "models",
                            "schemas", "utils", "types", "config"):
                file_votes[stem] += 1
            continue

        # Strip method prefixes (._method → skip; ClassName.method → use ClassName)
        if label.startswith(".") or label.startswith("_"):
            continue

        # CamelCase class name (e.g., UsageService, AgentMemory)
        if _re.match(r"^[A-Z][A-Za-z0-9]+$", label):
            # Split camel case: "UsageService" → ["Usage", "Service"]
            parts = _re.sub(r"([a-z])([A-Z])", r"\1 \2", label)
            # Trim trailing generic suffixes for grouping
            core = parts.rstrip()
            if len(core) > 3:
                class_votes[core] += 1

    # Use file votes first, then class votes
    for votes in (file_votes, class_votes):
        if votes:
            top, count = votes.most_common(1)[0]
            if count >= max(1, len(real_labels) * 0.15):
                name_parts = _re.split(r"[_\-]", top)
                readable = " ".join(p.title() for p in name_parts if p)
                if readable:
                    return f"{readable} (c{comm_id})"

    # ── Strategy 3: Label keyword scan ────────────────────────────────────────
    DOMAIN_KEYWORDS = [
        ("MT5",        "MT5"),    ("Telegram",   "Telegram"),
        ("Paul",       "Agent Paul"),  ("Rug",   "Rug Pull"),
        ("Pump",       "Pump Monitor"), ("Sniper", "Sniper"),
        ("Signal",     "Signal"),  ("Sentiment",  "Sentiment"),
        ("Trade",      "Trading"), ("Exchange",   "Exchange"),
        ("Bitget",     "Bitget"),  ("Bybit",      "Bybit"),
        ("Agent",      "AI Agent"), ("LLM",       "LLM"),
        ("Strategy",   "Strategy"), ("Worker",    "Worker"),
        ("Monitor",    "Monitor"), ("Chart",      "Chart"),
        ("Frontend",   "Frontend"), ("Router",    "Router"),
        ("Simulation", "Simulation"), ("Position", "Position"),
        ("Order",      "Order"),   ("Balance",    "Balance"),
        ("News",       "News"),    ("Pine",       "Pine Script"),
        ("Indicator",  "Indicator"), ("Knowledge", "Knowledge"),
        ("Memory",     "Memory"),  ("Usage",      "Usage"),
    ]
    kw_votes: Counter = Counter()
    all_text = " ".join(labels).lower()
    for kw, display in DOMAIN_KEYWORDS:
        cnt = all_text.count(kw.lower())
        if cnt > 0:
            kw_votes[display] += cnt

    if kw_votes:
        top_display, count = kw_votes.most_common(1)[0]
        if count >= 2:
            second = kw_votes.most_common(2)
            if len(second) > 1 and second[1][1] >= count * 0.5:
                return f"{top_display} & {second[1][0]} (c{comm_id})"
            return f"{top_display} (c{comm_id})"

    return None  # Skip completely anonymous communities


def _load_graphify_communities() -> List[Dict[str, Any]]:
    """
    Parse the most consistent graphify graph.json+GRAPH_REPORT.md pair and
    group nodes by community id.

    Community names come from the authoritative ``### Community N - "Name"``
    sections in GRAPH_REPORT.md that was generated in the *same* graphify run
    as graph.json.  Falls back to "Community N" for unnamed ones.

    Returns list of {name, nodes, community_id} dicts.
    """
    graph_path, report_path = _find_graphify_pair()

    if graph_path is None:
        logger.warning("[ObsidianSync] graphify-out/graph.json not found — skipping communities")
        return []

    try:
        data = json.loads(graph_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error(f"[ObsidianSync] Failed to parse graph.json: {exc}")
        return []

    # ── Step 1: Parse community names from the paired GRAPH_REPORT.md ────────
    community_names: Dict[int, str] = {}
    if report_path and report_path.exists():
        import re as _re
        text = report_path.read_text(encoding="utf-8")
        # Format:  ### Community 18 - "Agent Orchestration"
        name_re = _re.compile(r'^###\s+Community\s+(\d+)\s+-\s+"(.+?)"', _re.MULTILINE)
        for m in name_re.finditer(text):
            cid = int(m.group(1))
            name = m.group(2).strip()
            if name and not name.startswith("Community "):
                community_names[cid] = name

        logger.debug(
            f"[ObsidianSync] Loaded {len(community_names)} community names "
            f"from {report_path}"
        )

    # ── Step 2: Group graph.json nodes by community ID ───────────────────────
    nodes = data.get("nodes", [])
    raw_communities: Dict[int, List[Dict[str, Any]]] = {}

    for node in nodes:
        comm_id = node.get("community", node.get("group", 0))
        raw_communities.setdefault(comm_id, []).append({
            "name": node.get("label", node.get("id", "")),
            "src":  node.get("src", ""),
            "loc":  node.get("loc", ""),
            "id":   node.get("id", node.get("label", "")),
        })

    # ── Step 3: Assign names (known first, auto-inferred for the rest) ────────
    communities: List[Dict[str, Any]] = []
    for comm_id, node_list in raw_communities.items():
        if comm_id in community_names:
            # Authoritative name from GRAPH_REPORT.md
            name = community_names[comm_id]
        else:
            # Try to infer a meaningful name from node labels/sources
            name = _infer_community_name(comm_id, node_list, community_names)
            if name is None:
                # Skip completely anonymous micro-communities
                continue

        communities.append({
            "community_id": comm_id,
            "name":         name,
            "nodes":        node_list,
        })

    logger.debug(
        f"[ObsidianSync] {len(communities)} communities total "
        f"({len(community_names)} named, "
        f"{len(communities) - len(community_names)} auto-inferred)"
    )
    return communities


# ─── Main sync function ───────────────────────────────────────────────────────

async def run_sync(
    db: AsyncSession,
    export_decisions: bool = True,
    export_signals: bool = True,
    export_communities: bool = True,
    limit: int = 100,
) -> Dict[str, Any]:
    """
    Full vault sync.  Returns a result dict:
        {written, skipped, errors, duration_ms, details}
    """
    t0 = time.perf_counter()
    written = skipped = errors = 0
    details: List[str] = []

    writer = VaultWriter()
    bridge = get_bridge()

    # Ensure daily note exists for today
    try:
        path, ok, cs = writer.write_daily_note(date.today())
        if ok:
            written += 1
            await _upsert_note_record(db, path, "daily", writer.root, None, None, None, cs)
            await _try_push(bridge, path, writer.root)
        else:
            skipped += 1
    except Exception as exc:
        errors += 1
        details.append(f"Daily note error: {exc}")

    # Ensure index note exists
    try:
        path, ok, cs = writer.write_index_note()
        if ok:
            written += 1
    except Exception as exc:
        errors += 1
        details.append(f"Index note error: {exc}")

    # ── Signals ───────────────────────────────────────────────────────────────
    if export_signals and obsidian_settings.OBSIDIAN_EXPORT_SIGNALS:
        try:
            from app.models.database import Signal
            result = await db.execute(
                select(Signal).order_by(desc(Signal.timestamp)).limit(limit)
            )
            signals = result.scalars().all()
            for sig in signals:
                try:
                    path, ok, cs = writer.write_signal_note(sig)
                    if ok:
                        written += 1
                        await _upsert_note_record(
                            db, path, "signal", writer.root,
                            str(sig.id), "signals", sig.symbol, cs
                        )
                        await _try_push(bridge, path, writer.root)
                    else:
                        skipped += 1
                except Exception as exc:
                    errors += 1
                    details.append(f"Signal {getattr(sig, 'id', '?')}: {exc}")
        except Exception as exc:
            errors += 1
            details.append(f"Signals export error: {exc}")

    # ── Agent decisions ───────────────────────────────────────────────────────
    if export_decisions and obsidian_settings.OBSIDIAN_EXPORT_DECISIONS:
        try:
            from app.models.database import AgentDecision
            result = await db.execute(
                select(AgentDecision).order_by(desc(AgentDecision.created_at)).limit(limit)
            )
            decisions = result.scalars().all()
            for dec in decisions:
                try:
                    path, ok, cs = writer.write_decision_note(dec)
                    if ok:
                        written += 1
                        await _upsert_note_record(
                            db, path, "decision", writer.root,
                            str(dec.id), "agent_decisions",
                            getattr(dec, "symbol", None), cs
                        )
                        await _try_push(bridge, path, writer.root)
                    else:
                        skipped += 1
                except Exception as exc:
                    errors += 1
                    details.append(f"Decision {getattr(dec, 'id', '?')}: {exc}")
        except Exception as exc:
            errors += 1
            details.append(f"Decisions export error: {exc}")

    # ── Graphify communities ──────────────────────────────────────────────────
    if export_communities and obsidian_settings.OBSIDIAN_EXPORT_COMMUNITIES:
        communities = _load_graphify_communities()
        for comm in communities:
            if not comm["nodes"]:
                continue
            try:
                path, ok, cs = writer.write_community_note(
                    community_name=comm["name"],
                    nodes=comm["nodes"],
                )
                if ok:
                    written += 1
                    await _upsert_note_record(
                        db, path, "community", writer.root,
                        str(comm["community_id"]), "graphify_communities", None, cs
                    )
                    await _try_push(bridge, path, writer.root)
                else:
                    skipped += 1
            except Exception as exc:
                errors += 1
                details.append(f"Community '{comm['name']}': {exc}")

        # Write default strategy notes
        for strat_name in ["SMC Smart Money", "Rug Pull Sniper", "Pump Monitor", "Trend Following"]:
            try:
                path, ok, cs = writer.write_strategy_note(strat_name)
                if ok:
                    written += 1
            except Exception:
                pass

    await db.commit()

    duration_ms = round((time.perf_counter() - t0) * 1000, 1)
    logger.info(
        f"[ObsidianSync] Complete — written={written} skipped={skipped} errors={errors} "
        f"duration={duration_ms}ms"
    )

    return {
        "written": written,
        "skipped": skipped,
        "errors": errors,
        "duration_ms": duration_ms,
        "details": details[:20],  # cap for response size
    }


# ─── Background periodic sync task ───────────────────────────────────────────

async def periodic_sync_task(db_factory: Any, interval_minutes: int = 15) -> None:
    """
    Long-running background coroutine that syncs the vault periodically.
    Call from FastAPI startup:

        asyncio.create_task(periodic_sync_task(AsyncSessionLocal))
    """
    while True:
        await asyncio.sleep(interval_minutes * 60)
        try:
            async with db_factory() as db:
                await run_sync(db, limit=50)
        except Exception as exc:
            logger.error(f"[ObsidianSync] Periodic sync failed: {exc}")


# ─── DB helpers ──────────────────────────────────────────────────────────────

async def _upsert_note_record(
    db: AsyncSession,
    path: Path,
    note_type: str,
    vault_root: Path,
    source_id: Optional[str],
    source_table: Optional[str],
    symbol: Optional[str],
    checksum: str,
) -> None:
    """Insert or update the VaultNote tracking record."""
    rel_path = str(path.relative_to(vault_root))
    result = await db.execute(
        select(VaultNote).where(VaultNote.path == rel_path)
    )
    existing = result.scalar_one_or_none()
    now = datetime.utcnow()

    if existing:
        existing.checksum = checksum
        existing.updated_at = now
        existing.symbol = symbol
    else:
        db.add(VaultNote(
            path=rel_path,
            note_type=note_type,
            source_id=source_id,
            source_table=source_table,
            symbol=symbol,
            checksum=checksum,
            created_at=now,
            updated_at=now,
        ))


async def _try_push(bridge: Any, path: Path, vault_root: Path) -> None:
    """Try to push a note to Obsidian REST API (non-blocking best effort)."""
    if not bridge.enabled:
        return
    try:
        rel = str(path.relative_to(vault_root))
        content = path.read_text(encoding="utf-8")
        await bridge.push_note(rel, content)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# Auto-sync loop
# ═══════════════════════════════════════════════════════════════════════════════
#
# The vault is only useful if it reflects what the desk has actually been doing,
# and a sync nobody remembers to press is a vault that quietly goes stale. This
# runs one on a timer and, just as importantly, records WHEN it last ran.
#
# `/status` derives its `last_sync_at` from max(VaultNote.updated_at), which is
# the last time a note changed — not the last time a sync ran. A cycle that
# finds nothing new leaves that timestamp untouched, so the page would keep
# showing an ever-older time while syncing perfectly happily every 5 minutes.
# `_last_run` below is the honest answer to "when did this last run".

_sync_task: Optional[asyncio.Task] = None
_sync_running = False
_sync_interval = 300  # 5 minutes
_sync_started_at: Optional[str] = None
_last_run: Optional[Dict[str, Any]] = None


def _utc_now_iso() -> str:
    """An ISO timestamp the browser cannot misread.

    `datetime.utcnow().isoformat()` yields a NAIVE string, and `new Date(...)`
    in JS parses that as LOCAL time — so on a UTC+2 machine a sync from two
    minutes ago rendered as "2h 2m ago". Offset-aware output removes the guess.
    """
    return datetime.now(timezone.utc).isoformat()


def get_vault_sync_status() -> Dict[str, Any]:
    """Loop state for the vault page: running, cadence, and the last real run."""
    next_due = None
    if _sync_running and _last_run and _last_run.get("at"):
        try:
            last = datetime.fromisoformat(_last_run["at"])
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            next_due = (last + timedelta(seconds=_sync_interval)).isoformat()
        except (TypeError, ValueError):
            next_due = None
    return {
        "running": _sync_running,
        "interval_seconds": _sync_interval,
        "started_at": _sync_started_at,
        "last_run": _last_run,
        "next_run_at": next_due,
    }


async def _run_one_cycle() -> Dict[str, Any]:
    """One sync against its own session, recorded whether or not it changed anything."""
    global _last_run
    from app.core.database import AsyncSessionLocal

    started = _utc_now_iso()
    try:
        async with AsyncSessionLocal() as db:
            result = await run_sync(db=db)
        _last_run = {
            "at": started,
            "status": "ok" if result.get("errors", 0) == 0 else "partial",
            "written": result.get("written", 0),
            "skipped": result.get("skipped", 0),
            "errors": result.get("errors", 0),
            "duration_ms": result.get("duration_ms", 0),
            "trigger": "auto",
        }
        logger.info(
            f"🗂️  [VAULT SYNC] {_last_run['written']} written, "
            f"{_last_run['skipped']} unchanged, {_last_run['errors']} error(s)"
        )
    except Exception as exc:  # noqa: BLE001 — a bad cycle must not kill the loop
        _last_run = {
            "at": started, "status": "error",
            "error": str(exc)[:500], "trigger": "auto",
        }
        logger.error(f"🗂️  [VAULT SYNC] cycle failed: {exc}")
    return _last_run


def record_manual_sync(result: Dict[str, Any]) -> None:
    """Record a sync the user pressed, so the page shows it as the latest run."""
    global _last_run
    _last_run = {
        "at": _utc_now_iso(),
        "status": "ok" if result.get("errors", 0) == 0 else "partial",
        "written": result.get("written", 0),
        "skipped": result.get("skipped", 0),
        "errors": result.get("errors", 0),
        "duration_ms": result.get("duration_ms", 0),
        "trigger": "manual",
    }


async def _sync_loop() -> None:
    global _sync_running
    logger.info(f"🗂️  [VAULT SYNC] auto-sync started — every {_sync_interval}s")
    # Let the app finish booting before the first pass; a sync walks the whole
    # vault and competes with startup for the event loop.
    try:
        await asyncio.sleep(20)
    except asyncio.CancelledError:
        _sync_running = False
        return

    while _sync_running:
        await _run_one_cycle()
        try:
            await asyncio.sleep(_sync_interval)
        except asyncio.CancelledError:
            break
    _sync_running = False
    logger.info("🗂️  [VAULT SYNC] auto-sync stopped")


def start_vault_sync_loop(interval: int = 300) -> bool:
    global _sync_task, _sync_running, _sync_interval, _sync_started_at

    if _sync_task is not None and not _sync_task.done():
        logger.warning("Vault sync loop already running")
        return False

    # Floor of 60s: a full vault walk every few seconds is pure churn.
    _sync_interval = max(60, int(interval or 300))
    _sync_running = True
    _sync_started_at = _utc_now_iso()
    _sync_task = asyncio.create_task(_sync_loop())
    logger.info(f"🗂️  Vault auto-sync started (every {_sync_interval}s)")
    return True


def stop_vault_sync_loop() -> bool:
    global _sync_running, _sync_task, _sync_started_at

    if not _sync_running and (_sync_task is None or _sync_task.done()):
        logger.warning("Vault sync loop is not running")
        return False

    _sync_running = False
    if _sync_task:
        _sync_task.cancel()
    _sync_task = None
    _sync_started_at = None
    logger.info("🗂️  Vault auto-sync stopped")
    return True
