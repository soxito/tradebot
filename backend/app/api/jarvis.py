"""
JARVIS Assistant API
====================
Positions monitor + voice-command executor for all connected crypto exchanges.

Endpoints:
  GET  /jarvis/positions              → all open positions (all exchanges)
  POST /jarvis/command                → parse & execute a voice command
  GET  /jarvis/portfolio              → portfolio summary (total PnL, equity)
  POST /jarvis/voice-brain/sync       → persist voice fingerprint + vocabulary to vault
  GET  /jarvis/voice-brain/load       → restore voice fingerprint + vocabulary from vault
  POST /jarvis/voice-brain/identify   → compare submitted frequency bands → confidence score
"""
from __future__ import annotations

import json
import re
import asyncio
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Dict, List, Optional

from fastapi import APIRouter, Query
from loguru import logger
from pydantic import BaseModel

from app.exchanges.manager import exchange_manager, SupportedExchange
from app.exchanges.forex_provider import is_forex_symbol, fetch_ohlcv as forex_fetch_ohlcv

router = APIRouter(prefix="/jarvis", tags=["jarvis"])


# ── Triple-brain learning capture ────────────────────────────────────────────
# Every JARVIS action, command, and chat response is persisted to THREE brains:
#   1. Obsidian vault file + VaultNote DB row  → /vault list + /intelligence live feed
#   2. AI Analyst knowledge store              → /intelligence knowledge panel
#   3. PaulKnowledge long-term memory          → recalled into future chat context
# Fire-and-forget: never blocks the response.
def jarvis_brain_capture(
    action: str,
    symbol: str = "",
    summary: str = "",
    detail: str = "",
    tags: Optional[List[str]] = None,
    order_id: str = "",
    importance: float = 0.5,
) -> None:
    """Backward-compatible wrapper — delegates to jarvis_learn_all_brains."""
    jarvis_learn_all_brains(
        action=action, symbol=symbol, summary=summary, detail=detail,
        tags=tags, order_id=order_id, importance=importance,
    )


async def _knowledge_capture(
    action: str, symbol: str, summary: str, detail: str, importance: float
) -> None:
    """Inner coroutine — writes the action to the PaulKnowledge brain."""
    try:
        from app.core.database import AsyncSessionLocal
        from plugins.AgentPaulPlugin.backend.services import knowledge_base

        text = f"JARVIS {action}" + (f" {symbol}" if symbol else "")
        text += f": {summary}".rstrip(": ")
        if detail:
            text += f" — {detail[:300]}"
        async with AsyncSessionLocal() as db:
            await knowledge_base.record_knowledge(
                db,
                kind="insight",
                content=text[:1000],
                source="jarvis-command",
                symbol=symbol or None,
                topic=action,
                importance=importance,
            )
    except Exception as e:  # pragma: no cover - best effort
        logger.debug(f"[JARVIS brain] knowledge write skipped: {e}")


async def _ai_analyst_capture(
    action: str, symbol: str, summary: str, detail: str,
    kind: str = "insight", importance: float = 0.5
) -> None:
    """Write to the AI Analyst knowledge brain (powers /intelligence panel)."""
    try:
        from app.core.database import AsyncSessionLocal
        from plugins.AiMarketAnalyst.backend.services import knowledge_service
        title = f"JARVIS {action}" + (f" — {symbol}" if symbol else "")
        body = summary
        if detail:
            body += f"\n\n{detail[:600]}"
        async with AsyncSessionLocal() as db:
            await knowledge_service.store_knowledge(
                db,
                content=body[:1200],
                agent_role="jarvis",
                symbol=symbol or None,
                kind=kind,
                title=title,
                weight=max(1.3, importance * 2.0),  # floor at 1.3 to stay visible
                source="jarvis",
            )
    except Exception as e:
        logger.debug(f"[JARVIS brain] AI-analyst store skipped: {e}")


async def _vault_capture_with_db(
    action: str, symbol: str, summary: str, detail: str,
    tags: Optional[List[str]] = None, order_id: str = "",
) -> None:
    """Write a vault file AND register a VaultNote DB row so the note appears
    in the /vault list and the /intelligence live feed."""
    try:
        from datetime import datetime as _dt
        from sqlalchemy import select
        from app.core.database import AsyncSessionLocal
        from plugins.ObsidianKnowledgePlugin.backend.services.vault_writer import VaultWriter
        from plugins.ObsidianKnowledgePlugin.backend.services.obsidian_rest import get_bridge
        from plugins.ObsidianKnowledgePlugin.backend.models import VaultNote
    except Exception:
        return

    try:
        writer = VaultWriter()
        path, written, cs = writer.write_action_note(
            action_type=f"jarvis-{action}",
            symbol=symbol,
            summary=summary[:200],
            detail=detail,
            tags=tags or ["jarvis", action],
            agent_role="jarvis",
            order_id=order_id or "",
        )
        rel = str(path.relative_to(writer.root))
    except Exception as exc:
        logger.debug(f"[JARVIS brain] vault write skipped: {exc}")
        return

    # Register in DB so /vault + /intelligence live feed pick it up.
    try:
        async with AsyncSessionLocal() as db:
            existing = (
                await db.execute(select(VaultNote).where(VaultNote.path == rel))
            ).scalar_one_or_none()
            now = _dt.utcnow()
            if existing:
                existing.checksum = cs
                existing.updated_at = now
            else:
                db.add(VaultNote(
                    path=rel,
                    note_type=f"jarvis-{action}",
                    symbol=symbol or None,
                    tags=tags or ["jarvis", action],
                    checksum=cs,
                    created_at=now,
                    updated_at=now,
                ))
            await db.commit()
    except Exception as exc:
        logger.debug(f"[JARVIS brain] vault DB register skipped: {exc}")

    # Best-effort live push to Obsidian app.
    try:
        bridge = get_bridge()
        if getattr(bridge, "enabled", False):
            await bridge.push_note(rel, path.read_text(encoding="utf-8"))
    except Exception:
        pass


def jarvis_learn_all_brains(
    action: str,
    symbol: str = "",
    summary: str = "",
    detail: str = "",
    tags: Optional[List[str]] = None,
    order_id: str = "",
    importance: float = 0.5,
    kind: str = "insight",
) -> None:
    """Persist any JARVIS event to ALL three knowledge brains:
    vault (file + DB row), AI Analyst store, and PaulKnowledge.
    Fire-and-forget — never blocks the caller."""
    try:
        loop = asyncio.get_event_loop()
        if not loop.is_running():
            return
        _tags = list(set((tags or []) + ["jarvis", action] + ([symbol] if symbol else [])))
        loop.create_task(
            _vault_capture_with_db(action, symbol, summary, detail, _tags, order_id or "")
        )
        loop.create_task(
            _ai_analyst_capture(action, symbol, summary, detail, kind, importance)
        )
        loop.create_task(
            _knowledge_capture(action, symbol, summary, detail, importance)
        )
    except Exception as e:
        logger.debug(f"[JARVIS brain] learn_all_brains scheduling skipped: {e}")


# ── Voice brain models ────────────────────────────────────────────────────────

class VoiceProfile(BaseModel):
    bands: List[float]                   # 12 frequency-band energies (0–1)
    bandStdDev: Optional[List[float]] = None
    centroid: float = 0.0
    sessions: int = 0
    calibratedAt: Optional[float] = None


class VoiceBrainSyncRequest(BaseModel):
    vocabulary: Dict[str, int]           # {word: count}
    profile: Optional[VoiceProfile] = None
    sessions: int = 0


class VoiceBrainIdentifyRequest(BaseModel):
    bands: List[float]                   # current frame's 12-band energies
    centroid: Optional[float] = None


# ── Voice brain: vault note paths ─────────────────────────────────────────────
# These notes are PERMANENT — never deleted, only updated with new data.

def _voice_vault_path() -> Path:
    """Return the fixed vault path for the voice identity note."""
    try:
        from plugins.ObsidianKnowledgePlugin.backend.config import obsidian_settings
        return obsidian_settings.vault_path / "voice-memory" / "voice-profile.md"
    except Exception:
        return Path.home() / ".jarvis" / "voice-profile.md"


def _voice_data_path() -> Path:
    """Return the machine-readable JSON data file (next to the vault note)."""
    return _voice_vault_path().with_suffix(".json")


# ── Voice binary comparison engine ────────────────────────────────────────────

def _band_match_confidence(current: List[float], stored: VoiceProfile) -> float:
    """
    Compare a real-time frequency-band frame against the stored voice profile.

    Returns 0–1 where 1.0 means every band is within 1σ of the profile mean.
    Uses per-band standard-deviation tolerances so natural voice variation
    (mic distance, time of day, cold) does not reject the real user, while
    TV / background noise with a different spectral shape scores near zero.
    """
    if not stored.bands or not current:
        return 1.0  # no profile yet — accept everything
    bands_n = min(len(current), len(stored.bands))
    std_dev = stored.bandStdDev or [0.25] * bands_n   # generous fallback
    score = 0.0
    for i in range(bands_n):
        dev       = abs(current[i] - stored.bands[i])
        tolerance = std_dev[i] * 3.0 + 0.05           # 3σ window + fixed floor
        score    += max(0.0, 1.0 - dev / tolerance)
    return score / bands_n


# ── Voice Brain endpoints ─────────────────────────────────────────────────────

@router.post("/voice-brain/sync")
async def voice_brain_sync(req: VoiceBrainSyncRequest):
    """
    Persist voice fingerprint + vocabulary to the Obsidian vault.

    The vault note is written at voice-memory/voice-profile.md and is NEVER
    deleted — only merged (new counts always accumulate, never decrease).
    A machine-readable JSON sidecar is written alongside the note so the
    load endpoint can restore exact data without parsing markdown.
    """
    now        = datetime.now(timezone.utc)
    data_path  = _voice_data_path()
    note_path  = _voice_vault_path()

    # ── Load existing data (merge-in new counts) ──────────────────────────────
    existing: Dict[str, Any] = {}
    if data_path.exists():
        try:
            existing = json.loads(data_path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}

    # Merge vocabulary: take the MAX count per word (accumulate, never shrink)
    merged_vocab: Dict[str, int] = dict(existing.get("vocabulary", {}))
    for word, count in req.vocabulary.items():
        merged_vocab[word] = max(merged_vocab.get(word, 0), count)

    # Merge profile: if a new profile is supplied, blend it with the stored one
    # using an exponential moving average so old learning doesn't vanish.
    stored_profile = existing.get("profile", None)
    if req.profile and req.profile.bands:
        if stored_profile and stored_profile.get("bands"):
            alpha   = 0.10   # 10 % new, 90 % old — conservative, stable
            s_bands = stored_profile["bands"]
            n_bands = req.profile.bands
            n       = min(len(s_bands), len(n_bands))
            merged_bands = [s_bands[i] * (1 - alpha) + n_bands[i] * alpha for i in range(n)]
            # Update std-dev similarly
            s_std = stored_profile.get("bandStdDev") or [0.1] * n
            n_std = req.profile.bandStdDev or [0.1] * n
            merged_std  = [s_std[i] * (1 - alpha) + n_std[i] * alpha for i in range(n)]
            merged_centroid = (
                stored_profile.get("centroid", 0) * (1 - alpha) +
                req.profile.centroid * alpha
            )
            new_profile = {
                "bands":       merged_bands,
                "bandStdDev":  merged_std,
                "centroid":    merged_centroid,
                "sessions":    existing.get("sessions", 0) + 1,
                "calibratedAt": now.timestamp(),
            }
        else:
            # First save — store as-is
            new_profile = req.profile.model_dump()
            new_profile["sessions"] = 1
    else:
        new_profile = stored_profile

    total_words = len(merged_vocab)
    top_words   = sorted(merged_vocab.items(), key=lambda x: -x[1])[:50]
    sessions    = (existing.get("sessions", 0) + 1) if req.profile else existing.get("sessions", 0)

    # ── Write JSON sidecar (machine-readable, never deleted) ──────────────────
    data_out = {
        "vocabulary": merged_vocab,
        "profile":    new_profile,
        "sessions":   sessions,
        "total_words": total_words,
        "updated":    now.isoformat(),
    }
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_text(json.dumps(data_out, ensure_ascii=False), encoding="utf-8")
    logger.info(f"[JARVIS voice-brain] synced: {total_words} words, sessions={sessions}")

    # ── Write human-readable vault note ───────────────────────────────────────
    band_table = ""
    if new_profile and new_profile.get("bands"):
        b  = new_profile["bands"]
        sd = new_profile.get("bandStdDev", ["-"] * len(b))
        band_labels = [
            "<80Hz", "80-160", "160-320", "320-640",
            "640Hz-1.3k", "1.3-2.5k", "2.5-5k", "5-10k",
            "10-16k", "16-20k", "20k+", "ultra",
        ]
        def _fmt(v: Any) -> str:
            try: return f"{float(v):.4f}"
            except (TypeError, ValueError): return "-"

        rows = "\n".join(
            f"| {band_labels[i] if i < len(band_labels) else i} "
            f"| {b[i]:.4f} "
            f"| {_fmt(sd[i])} |"
            for i in range(len(b))
        )
        band_table = (
            "\n## Voice Frequency Fingerprint\n"
            "| Band | Mean Energy | Std Dev |\n"
            "| ---- | ----------- | ------- |\n"
            + rows
            + f"\n\nSpectral centroid: **{new_profile.get('centroid', 0):.4f}**  "
            f"Sessions: **{sessions}**\n"
        )

    top_table = "\n".join(
        f"| {w} | {c} |" for w, c in top_words
    )

    note = (
        f"---\n"
        f"type: voice-identity\n"
        f"updated: {now.isoformat()}\n"
        f"sessions: {sessions}\n"
        f"words_learned: {total_words}\n"
        f"tags:\n  - jarvis\n  - voice\n  - identity\n"
        f"---\n\n"
        f"# JARVIS Voice Identity\n\n"
        f"> This note is the permanent voice memory for JARVIS.  "
        f"It is **never deleted** — only improved over time as you speak.\n\n"
        f"Updated: {now.strftime('%Y-%m-%d %H:%M UTC')}  "
        f"| Words learned: **{total_words}**  "
        f"| Voice sessions: **{sessions}**\n"
        f"{band_table}\n"
        f"## Top Learned Words (speech vocabulary)\n\n"
        f"| Word | Times Spoken |\n"
        f"| ---- | ------------ |\n"
        f"{top_table}\n\n"
        f"## How This Works\n\n"
        f"JARVIS builds a 12-band frequency fingerprint of your voice from the Web Audio API.\n"
        f"Each time you speak a confirmed command, the fingerprint is updated with a 10 % blend\n"
        f"(exponential moving average) so your natural day-to-day variation is captured without\n"
        f"overwriting previous learning. The vocabulary table accumulates word counts across all\n"
        f"sessions — words you say often get higher recognition priority.\n\n"
        f"*Machine-readable data is stored alongside this note in `voice-profile.json`.*\n"
    )

    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(note, encoding="utf-8")

    return {
        "ok": True,
        "words_total": total_words,
        "sessions": sessions,
        "vault_path": str(note_path),
        "top_words": dict(top_words[:10]),
    }


@router.get("/voice-brain/load")
async def voice_brain_load():
    """
    Load voice fingerprint + vocabulary from the permanent vault note.
    Returns stored data for merging with the browser's local state.
    """
    data_path = _voice_data_path()
    if not data_path.exists():
        return {"ok": True, "vocabulary": {}, "profile": None, "sessions": 0}
    try:
        data = json.loads(data_path.read_text(encoding="utf-8"))
        return {"ok": True, **data}
    except Exception as e:
        logger.warning(f"[JARVIS voice-brain] load error: {e}")
        return {"ok": False, "vocabulary": {}, "profile": None, "sessions": 0}


@router.post("/voice-brain/identify")
async def voice_brain_identify(req: VoiceBrainIdentifyRequest):
    """
    Voice binary engine — compare a real-time frequency-band frame against
    the stored voice profile and return an identification confidence score.

    Returns:
      confidence: 0.0–1.0  (≥0.55 = likely the stored speaker)
      match: bool
      sessions: int         (how many sessions have improved the model)
    """
    data_path = _voice_data_path()
    if not data_path.exists():
        return {"confidence": 1.0, "match": True, "sessions": 0,
                "message": "No profile stored yet — accepting all speakers."}
    try:
        data    = json.loads(data_path.read_text(encoding="utf-8"))
        raw     = data.get("profile")
        if not raw or not raw.get("bands"):
            return {"confidence": 1.0, "match": True, "sessions": 0}
        profile = VoiceProfile(**raw)
        conf    = _band_match_confidence(req.bands, profile)
        return {
            "confidence": round(conf, 4),
            "match":      conf >= 0.55,
            "sessions":   data.get("sessions", 0),
        }
    except Exception as e:
        logger.warning(f"[JARVIS voice-brain] identify error: {e}")
        return {"confidence": 1.0, "match": True, "sessions": 0}


# ── Response / Request models ──────────────────────────────────────────────────

class Position(BaseModel):
    exchange: str
    symbol: str          # normalised, e.g. "BTCUSDT"
    raw_symbol: str      # as returned by exchange, e.g. "BTC/USDT:USDT"
    side: str            # "long" | "short"
    size: float
    entry_price: float
    mark_price: float
    pnl: float
    pnl_pct: float
    leverage: Optional[float] = None
    margin_mode: Optional[str] = None
    notional: Optional[float] = None
    liquidation_price: Optional[float] = None


class PortfolioSummary(BaseModel):
    total_positions: int
    total_pnl: float
    total_notional: float
    positions: List[Position]


# ── Unified monitor models ─────────────────────────────────────────────────────

class CryptoAccountSummary(BaseModel):
    exchange: str
    currency: str = "USDT"
    total: float = 0.0   # total equity / wallet balance
    free: float = 0.0    # available (not used as margin)
    used: float = 0.0    # margin in use


class MT5AccountSummary(BaseModel):
    account_id: int
    name: str
    login: str
    server: str
    balance: float
    equity: float
    floating_pnl: float
    margin: float
    free_margin: float
    currency: str
    leverage: int
    positions: List[Dict[str, Any]] = []
    position_count: int = 0


class UnifiedMonitorResponse(BaseModel):
    # Crypto (all exchanges)
    crypto_positions: List[Position] = []
    crypto_accounts: List[CryptoAccountSummary] = []   # per-exchange balances
    crypto_total_pnl: float = 0.0
    crypto_total_notional: float = 0.0
    # MT5 (all accounts)
    mt5_accounts: List[MT5AccountSummary] = []
    mt5_total_balance: float = 0.0
    mt5_total_equity: float = 0.0
    mt5_total_floating_pnl: float = 0.0
    mt5_position_count: int = 0
    # Grand total
    total_position_count: int = 0
    total_pnl: float = 0.0
    fetched_at: str = ""


class PositionAnalysis(BaseModel):
    ticket: int
    symbol: str
    side: str
    account_id: int
    analysis_text: str
    has_suggestion: bool
    sl_suggestion: Optional[float] = None
    tp_suggestion: Optional[float] = None
    ai_verdict: Optional[str] = None
    analyzed_at: str = ""


class AnalyzePositionsResponse(BaseModel):
    account_id: int
    positions_analyzed: int
    analyses: List[PositionAnalysis] = []
    summary: str = ""
    analyzed_at: str = ""


class CommandRequest(BaseModel):
    command: str
    exchange: Optional[str] = None  # e.g. "bybit"; if None → auto-detect


class CommandResult(BaseModel):
    ok: bool
    action: str
    detail: str
    speech: str          # human-readable sentence for TTS
    order: Optional[Dict[str, Any]] = None


# ── Helpers ────────────────────────────────────────────────────────────────────

def _norm_symbol(raw: str) -> str:
    """
    'BTC/USDT:USDT' → 'BTCUSDT'
    'GWEI/USDT'     → 'GWEIUSDT'
    """
    base = raw.split(":")[0]          # strip :SETTLE suffix
    return base.replace("/", "")


def _match_symbol(query: str, raw: str) -> bool:
    """Return True if the user's query matches the exchange symbol."""
    q = query.upper().replace("/", "").replace(":", "").replace(" ", "")
    r = raw.upper().split(":")[0].replace("/", "")   # BTC/USDT:USDT → BTCUSDT
    return q == r or q in r or r.startswith(q)


def _safe_float(val, default: float = 0.0) -> float:
    try:
        v = float(val or 0)
        return v if v == v else default   # NaN guard
    except Exception:
        return default


# ── Extension version / update check ─────────────────────────────────────────

# Fallback version only — the real version is ALWAYS read live from
# jarvis-extension/manifest.json (see _ext_version()). Keep this in sync so a
# missing manifest never advertises a stale version.
_EXT_VERSION = "3.6.7"
_EXT_RELEASED = "2026-07-05"
_EXT_CHANGELOG = [
    "Fix mic hand-off: in-page JARVIS takes over when the extension speech engine stalls (no more stuck 'Starting…')",
    "Stable mic ownership: stop page<->extension flapping that left voice deaf",
    "Chart-page wake watchdog keeps voice listening alive on heavy WebGL pages",
    "Fix read-aloud silently dropped when pageSpeaking stuck true",
    "Fix accounts not shown in popup — lastUnifiedData now cached in background and used for instant account balance display on popup open and in 10s auto-refresh",
    "Fix accounts loading + trades not read aloud",
    "bump to v3.6.2",
    "JARVIS Memory Tree — the assistant now folds news, positions and trades into a scored, hierarchical long-term memory every 15 minutes",
    "SuperContext — on the first message of a chat JARVIS auto-sweeps its memory, news and brain-map for your exact question and pre-loads the answer",
    "Goals & Todos — set a durable goal and JARVIS builds a kanban with you and works it in the background (read-only research, never auto-trades)",
    "Proactive memory alerts — the extension now surfaces newly-learned high-importance facts as desktop notifications",
    "Camera mouth-movement now gates hearing in real time — JARVIS only listens while it sees your lips move, so its own TTS voice can never be self-transcribed while the camera is live",
    "Unknown-face lockout restored — a stranger can't drive JARVIS, but only once you've enrolled your own face (unenrolled never blocks)",
    "TTS self-hearing fixed for real — the page now passes the exact words it speaks so JARVIS never transcribes its own AI voice",
    "Fix: enabling Face Vision no longer stops JARVIS from hearing you (face is now additive-only, never mutes voice)",
    "JARVIS no longer transcribes its own voice while reading to you (self-echo guard + echo-tail window)",
    "Background sound and faint/other-room voices no longer wake JARVIS — only real near-mic user speech",
    "Face Vision toggle now reliably turns the camera on/off and remembers its state",
    "Face Vision camera now opens in a tab so the browser reliably asks for permission",
    "Live camera preview with lip/face overlay in the extension tab and JARVIS Room",
    "Enroll your face from the camera tab or the Room; popup mirrors live status",
    "Read-aloud on change now uses real coin names (BTCUSDT → Bitcoin)",
    "Correct up/down direction + change from the previous reading",
    "3D JARVIS robot avatar on every page",
    "Universal voice — extension speaks with your chosen chat voice",
    "Avatar style picker (cyan/purple/gold/crimson/emerald)",
    "Robot reacts to voice: listening, thinking, talking animations",
    "Unified monitor: crypto + MT5 accounts in real time",
    "15-minute automatic position analysis with AI/SMC",
    "On-demand analysis from popup with JARVIS speech",
    "Auto-update detection when TradeBot opens",
]


def _ext_dir() -> Optional[Path]:
    """
    Locate the ``jarvis-extension`` directory robustly.

    Walks up from this file until it finds a folder containing a
    ``jarvis-extension`` directory (project root), so the lookup never breaks if
    the module is moved. Returns ``None`` if it cannot be found.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "jarvis-extension"
        if candidate.is_dir():
            return candidate
    return None


def _ext_version() -> str:
    """Return the live extension version from manifest.json (fallback constant)."""
    import json as _json
    ext_dir = _ext_dir()
    if ext_dir is not None:
        manifest_path = ext_dir / "manifest.json"
        try:
            if manifest_path.exists():
                v = _json.loads(manifest_path.read_text()).get("version")
                if v:
                    return str(v)
        except Exception:
            pass
    return _EXT_VERSION


@router.get("/extension-version")
async def get_extension_version():
    """
    Returns the latest JARVIS extension version info.

    The extension polls this endpoint on TradeBot startup and every 24 hours.
    If the installed version differs from `version`, a banner is shown.
    """
    # Read version directly from manifest so backend and ZIP always agree
    manifest_version = _ext_version()

    return {
        "version": manifest_version,
        "released_at": _EXT_RELEASED,
        "changelog": _EXT_CHANGELOG,
        "install_path": "/jarvis-extension",
        "download_url": f"/api/v1/jarvis/extension-download",
        "download_versioned_url": f"/api/v1/jarvis/extension-download?v={manifest_version}",
        "instructions": "Reload the extension in chrome://extensions after updating the files.",
    }


@router.get("/extension-download")
async def download_extension(v: Optional[str] = None):
    """
    Download the latest JARVIS extension as a versioned ZIP file.

    Always packages the CURRENT files from the jarvis-extension/ directory,
    so the downloaded ZIP is always up to date. The `v` query param is ignored
    server-side (it exists purely to bust browser caches when version changes).

    Filename format: jarvis-extension-v{version}.zip
    """
    import io as _io
    import zipfile as _zipfile
    from fastapi.responses import StreamingResponse

    ext_dir = _ext_dir()
    if ext_dir is None or not ext_dir.exists():
        from fastapi import HTTPException
        raise HTTPException(404, "Extension directory not found")

    # Read version from manifest for the filename (always the live value)
    version = _ext_version()

    # Build the ZIP in memory from current files
    buf = _io.BytesIO()
    with _zipfile.ZipFile(buf, "w", _zipfile.ZIP_DEFLATED) as zf:
        for item in sorted(ext_dir.rglob("*")):
            if item.is_file():
                # Skip hidden files and caches
                if any(part.startswith(".") for part in item.parts):
                    continue
                arcname = item.relative_to(ext_dir)
                zf.write(item, arcname)

    buf.seek(0)
    filename = f"jarvis-extension-v{version}.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Extension-Version": version,
            "Cache-Control": "no-store, no-cache, must-revalidate",
        },
    )


# ── System resource stats (CPU / RAM) ─────────────────────────────────────────

@router.get("/system-stats")
async def get_system_stats():
    """
    Live host resource usage for the JARVIS Room HUD.

    Returns CPU %, per-core load, memory (used/total/percent), swap and the
    process footprint. Used by the room to show the operator how much of the
    machine the app is consuming so resources can be shared fairly.
    Degrades gracefully (``available: False``) when psutil is missing.
    """
    try:
        import psutil  # noqa
    except Exception:
        return {
            "available": False,
            "reason": "psutil not installed",
            "cpu_percent": 0.0,
            "cpu_count": 0,
            "mem_percent": 0.0,
            "mem_used": 0,
            "mem_total": 0,
        }

    try:
        # interval=None → non-blocking, returns usage since the previous call.
        cpu_percent = psutil.cpu_percent(interval=None)
        cpu_count = psutil.cpu_count(logical=True) or 0
        try:
            per_core = psutil.cpu_percent(interval=None, percpu=True)
        except Exception:
            per_core = []

        vm = psutil.virtual_memory()
        try:
            sw = psutil.swap_memory()
            swap_percent = float(sw.percent)
            swap_used = int(sw.used)
            swap_total = int(sw.total)
        except Exception:
            swap_percent, swap_used, swap_total = 0.0, 0, 0

        # This backend process's own footprint.
        proc_cpu = 0.0
        proc_mem = 0
        try:
            p = psutil.Process()
            proc_cpu = float(p.cpu_percent(interval=None))
            proc_mem = int(p.memory_info().rss)
        except Exception:
            pass

        # 1-minute load average (per-core normalised), where supported.
        load_pct = None
        try:
            import os as _os
            la1 = _os.getloadavg()[0]
            if cpu_count:
                load_pct = round(min(100.0, (la1 / cpu_count) * 100.0), 1)
        except Exception:
            load_pct = None

        return {
            "available": True,
            "cpu_percent": round(float(cpu_percent), 1),
            "cpu_count": cpu_count,
            "per_core": [round(float(c), 1) for c in per_core],
            "load_percent": load_pct,
            "mem_percent": round(float(vm.percent), 1),
            "mem_used": int(vm.used),
            "mem_total": int(vm.total),
            "mem_available": int(vm.available),
            "swap_percent": swap_percent,
            "swap_used": swap_used,
            "swap_total": swap_total,
            "proc_cpu_percent": round(proc_cpu, 1),
            "proc_mem": proc_mem,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:  # pragma: no cover - best effort
        logger.debug(f"[JARVIS] system-stats error: {e}")
        return {"available": False, "reason": str(e), "cpu_percent": 0.0, "mem_percent": 0.0}


# ── Crypto pair catalog endpoints ──────────────────────────────────────────────
# Backed by app/services/pair_catalog.py (the `crypto_pairs` table). These let
# JARVIS, the extension and the frontend use REAL coin names and resolve spoken
# token names/tickers to a tradeable Bitget pair.

@router.get("/pairs")
async def list_pairs(
    q: Annotated[Optional[str], Query(description="Search by symbol / ticker / name")] = None,
    limit: Annotated[int, Query(description="Max rows to return")] = 50,
):
    """Searchable catalog list (symbol, name, market cap, 24h volume, rank)."""
    try:
        from app.services import pair_catalog
        rows = await pair_catalog.search_pairs(q or "", limit=limit)
        return {"ok": True, "count": len(rows), "pairs": rows}
    except Exception as e:
        logger.warning(f"[JARVIS] /pairs error: {e}")
        return {"ok": False, "count": 0, "pairs": [], "error": str(e)}


@router.get("/pairs/names")
async def pair_names():
    """
    Compact ``{symbol: name}`` map for the extension + frontend.

    Keyed by BOTH ``BTC/USDT`` and ``BTCUSDT`` so monitor payloads (which use the
    glued form) map straight to a coin name.
    """
    try:
        from app.services import pair_catalog
        names = await pair_catalog.get_name_map()
        return {"ok": True, "count": len(names), "names": names}
    except Exception as e:
        logger.warning(f"[JARVIS] /pairs/names error: {e}")
        return {"ok": False, "count": 0, "names": {}, "error": str(e)}


@router.get("/pairs/resolve")
async def resolve_pair(
    q: Annotated[str, Query(description="Token name, ticker or symbol to resolve")],
):
    """
    Resolve a token/name/symbol to a tradeable Bitget pair with live metadata,
    or return ``ok:false`` with the closest suggestion when it isn't found.
    """
    try:
        from app.services import pair_catalog
        pair, suggestion = await pair_catalog.resolve_with_suggestion(q)
        if pair is None:
            return {"ok": False, "query": q, "suggestion": suggestion}

        result = pair_catalog.pair_to_dict(pair, full=True)
        # Overlay a fresh (cached ≤60s) live market snapshot.
        try:
            snap = await pair_catalog.get_market_snapshot(pair.symbol)
            if snap:
                for k in ("market_cap", "market_cap_rank", "volume_24h", "price", "price_change_24h", "name"):
                    if snap.get(k) is not None:
                        result[k] = snap[k]
        except Exception:
            pass
        result["ok"] = True
        result["query"] = q
        return result
    except Exception as e:
        logger.warning(f"[JARVIS] /pairs/resolve error: {e}")
        return {"ok": False, "query": q, "error": str(e)}


# ── Positions endpoint ─────────────────────────────────────────────────────────

@router.get("/positions", response_model=List[Position])
async def get_all_positions(
    exchange: Annotated[Optional[str], Query(description="Filter by exchange name")] = None,
):
    """
    Return all open futures/swap positions across every initialised exchange.
    Only positions with contracts > 0 are included.
    """
    all_positions: List[Position] = []

    ex_list: List[SupportedExchange] = exchange_manager.get_all_exchanges()

    if exchange:
        try:
            single = SupportedExchange(exchange.lower())
            ex_list = [single] if single in ex_list else []
        except ValueError:
            ex_list = []

    for ex_enum in ex_list:
        connector = exchange_manager.get_exchange(ex_enum)
        if not connector:
            continue
        ex_name = ex_enum.value
        try:
            raw_list = await connector.exchange.fetch_positions()
        except BaseException as e:
            if _is_network_error(e):
                logger.warning(f"[JARVIS] {ex_name} unreachable (DNS/network): {e}")
            else:
                logger.warning(f"[JARVIS] fetch_positions({ex_name}): {e}")
            continue

        for p in raw_list:
            contracts = _safe_float(p.get("contracts"))
            if contracts <= 0:
                continue

            raw_sym = p.get("symbol", "")
            entry   = _safe_float(p.get("entryPrice"))
            mark    = _safe_float(p.get("markPrice")) or entry
            pnl     = _safe_float(p.get("unrealizedPnl"))
            pnl_pct = _safe_float(p.get("percentage"))

            all_positions.append(Position(
                exchange=ex_name,
                symbol=_norm_symbol(raw_sym),
                raw_symbol=raw_sym,
                side=str(p.get("side") or "long").lower(),
                size=contracts,
                entry_price=entry,
                mark_price=mark,
                pnl=pnl,
                pnl_pct=pnl_pct,
                leverage=p.get("leverage"),
                margin_mode=p.get("marginMode"),
                notional=p.get("notional"),
                liquidation_price=p.get("liquidationPrice"),
            ))

    return all_positions


@router.get("/portfolio", response_model=PortfolioSummary)
async def get_portfolio():
    """Aggregate portfolio snapshot — total PnL, total notional, all positions."""
    positions = await get_all_positions()
    return PortfolioSummary(
        total_positions=len(positions),
        total_pnl=sum(p.pnl for p in positions),
        total_notional=sum(_safe_float(p.notional) for p in positions),
        positions=positions,
    )


@router.get("/unified-monitor", response_model=UnifiedMonitorResponse)
async def get_unified_monitor(
    sync: bool = Query(default=False, description="Sync live MT5 balance/positions before returning (slower)"),
):
    """
    Unified real-time monitor for all configured crypto exchanges AND MT5 accounts.

    Returns:
    - crypto_positions: all open futures/swap positions across every exchange
    - mt5_accounts: all MT5 accounts with balance, equity, and open positions
    - Grand totals: combined PnL, position count

    The JARVIS extension polls this endpoint every 10 seconds (sync=false, fast,
    cached). The popup's manual refresh button calls it with sync=true to pull
    live balance/positions from the mtapi-io bridge.
    """
    now = datetime.now(timezone.utc).isoformat()

    # ── Crypto positions ─────────────────────────────────────────────────────
    crypto_positions: List[Position] = []
    try:
        crypto_positions = await get_all_positions()
    except Exception as exc:
        logger.warning(f"[JARVIS unified-monitor] crypto positions failed: {exc}")

    # ── Crypto exchange balances ──────────────────────────────────────────────
    # Fetch USDT balance from every connected exchange so the popup shows real
    # wallet/equity alongside MT5 accounts. Each exchange is isolated — one
    # failure never drops the others.
    crypto_accounts: List[CryptoAccountSummary] = []
    try:
        ex_list_bal = exchange_manager.get_all_exchanges()
        for ex_enum in ex_list_bal:
            connector = exchange_manager.get_exchange(ex_enum)
            if not connector:
                continue
            try:
                bal = await connector.get_balance(currency="USDT")
                # CCXT returns {currency: {free, used, total}} or {free, used, total}
                if isinstance(bal, dict):
                    usdt = bal.get("USDT") or bal  # ccxt full vs single-currency
                    crypto_accounts.append(CryptoAccountSummary(
                        exchange=ex_enum.value.capitalize(),
                        currency="USDT",
                        total=float(usdt.get("total") or 0),
                        free=float(usdt.get("free") or 0),
                        used=float(usdt.get("used") or 0),
                    ))
            except Exception as bal_exc:
                logger.debug(f"[JARVIS unified-monitor] balance fetch skipped for {ex_enum.value}: {bal_exc}")
    except Exception as exc:
        logger.debug(f"[JARVIS unified-monitor] crypto balance fetch failed: {exc}")

    # ── MT5 accounts + positions ─────────────────────────────────────────────
    mt5_accounts: List[MT5AccountSummary] = []
    mt5_total_balance = 0.0
    mt5_total_equity = 0.0
    mt5_total_floating = 0.0
    mt5_pos_count = 0

    try:
        from app.core.database import AsyncSessionLocal
        from plugins.MT5TradingPlugin.backend.models import MT5Account, MT5Position
        from sqlalchemy import select as sa_select

        async with AsyncSessionLocal() as db:
            accounts_result = await db.execute(sa_select(MT5Account))
            mt5_accts = accounts_result.scalars().all()

            logger.debug(f"[JARVIS unified-monitor] found {len(mt5_accts)} MT5 account(s) in DB")


            for acct in mt5_accts:
                # Each account is isolated — one failure never drops the others.
                try:
                    # Optional live sync (only when ?sync=true — the manual refresh
                    # button). The 10s auto-poll never syncs, keeping it fast.
                    if sync and acct.api_reachable:
                        try:
                            from plugins.MT5TradingPlugin.backend.services.sync_service import MT5SyncService
                            await MT5SyncService.sync_account(db, acct)
                            await db.commit()
                            await db.refresh(acct)
                        except Exception as sync_exc:
                            logger.debug(f"[JARVIS unified-monitor] live sync skipped for acct {acct.id}: {sync_exc}")

                    # Read cached account data (fast). Live balance/equity is
                    # synced separately by the MT5 plugin when the mtapi-io bridge
                    # is up — we never block the auto-poll on a broker round-trip.
                    pos_result = await db.execute(
                        sa_select(MT5Position).where(MT5Position.account_id == acct.id)
                    )
                    mt5_positions = pos_result.scalars().all()

                    pos_list = [
                        {
                            "ticket": p.mt5_ticket,
                            "symbol": p.symbol,
                            "side": p.side.value if hasattr(p.side, "value") else str(p.side),
                            "volume": float(p.volume or 0),
                            "price_open": float(p.price_open or 0),
                            "price_current": float(p.price_current or 0),
                            "sl": float(p.sl) if p.sl else None,
                            "tp": float(p.tp) if p.tp else None,
                            "profit": float(p.profit or 0),
                            "swap": float(p.swap or 0),
                        }
                        for p in mt5_positions
                    ]

                    floating = float(acct.floating_pnl or 0) or sum(p["profit"] for p in pos_list)
                    acct_balance = float(acct.balance or 0)
                    acct_equity = float(acct.equity or 0)

                    mt5_accounts.append(MT5AccountSummary(
                        account_id=acct.id,
                        name=acct.name or f"Account {acct.id}",
                        login=str(acct.login or ""),
                        server=acct.server or "",
                        balance=acct_balance,
                        equity=acct_equity,
                        floating_pnl=floating,
                        margin=float(acct.margin or 0),
                        free_margin=float(acct.free_margin or 0),
                        currency=acct.currency or "USD",
                        leverage=int(acct.leverage or 1),
                        positions=pos_list,
                        position_count=len(pos_list),
                    ))
                    mt5_total_balance += acct_balance
                    mt5_total_equity += acct_equity
                    mt5_total_floating += floating
                    mt5_pos_count += len(pos_list)
                except Exception as acct_exc:
                    logger.warning(
                        f"[JARVIS unified-monitor] account {acct.id} ({acct.name}) failed: {acct_exc}"
                    )
                    # Still surface the account with cached/zero data so the user
                    # SEES it exists (the whole point — never hide a connected account).
                    try:
                        mt5_accounts.append(MT5AccountSummary(
                            account_id=acct.id,
                            name=acct.name or f"Account {acct.id}",
                            login=str(acct.login or ""),
                            server=acct.server or "",
                            balance=float(acct.balance or 0),
                            equity=float(acct.equity or 0),
                            floating_pnl=float(acct.floating_pnl or 0),
                            margin=float(acct.margin or 0),
                            free_margin=float(acct.free_margin or 0),
                            currency=acct.currency or "USD",
                            leverage=int(acct.leverage or 1),
                            positions=[],
                            position_count=0,
                        ))
                        mt5_total_balance += float(acct.balance or 0)
                        mt5_total_equity += float(acct.equity or 0)
                    except Exception:
                        pass
    except Exception as exc:
        import traceback
        logger.warning(
            f"[JARVIS unified-monitor] MT5 data failed: {exc}\n{traceback.format_exc()}"
        )

    crypto_pnl = sum(p.pnl for p in crypto_positions)
    total_pnl = crypto_pnl + mt5_total_floating
    total_positions = len(crypto_positions) + mt5_pos_count

    return UnifiedMonitorResponse(
        crypto_positions=crypto_positions,
        crypto_accounts=crypto_accounts,
        crypto_total_pnl=crypto_pnl,
        crypto_total_notional=sum(_safe_float(p.notional) for p in crypto_positions),
        mt5_accounts=mt5_accounts,
        mt5_total_balance=mt5_total_balance,
        mt5_total_equity=mt5_total_equity,
        mt5_total_floating_pnl=mt5_total_floating,
        mt5_position_count=mt5_pos_count,
        total_position_count=total_positions,
        total_pnl=total_pnl,
        fetched_at=now,
    )


@router.get("/analyze-positions", response_model=AnalyzePositionsResponse)
async def analyze_open_positions(
    account_id: int = Query(..., description="MT5 account ID"),
    speak: bool = Query(default=True, description="Include spoken summary for JARVIS TTS"),
):
    """
    Run SMC + AI analysis on ALL open MT5 positions for the given account.

    Called automatically every 15 minutes by the JARVIS extension alarm,
    and on-demand when the user clicks 'Analyze Now' in the popup.

    Returns per-position analysis with TP/SL suggestions and an AI verdict,
    plus a spoken summary that JARVIS can read aloud.
    """
    now = datetime.now(timezone.utc)
    now_str = now.isoformat()
    analyses: List[PositionAnalysis] = []

    try:
        from app.core.database import AsyncSessionLocal
        from plugins.MT5TradingPlugin.backend.models import MT5Account, MT5Position
        from plugins.MT5TradingPlugin.backend.services.smc_strategy import (
            SMCStrategyEngine, Candle
        )
        from plugins.MT5TradingPlugin.backend.services.mt5_client import MT5Client
        from sqlalchemy import select as sa_select

        async with AsyncSessionLocal() as db:
            acct = await db.get(MT5Account, account_id)
            if not acct:
                return AnalyzePositionsResponse(
                    account_id=account_id,
                    positions_analyzed=0,
                    summary=f"Account {account_id} not found.",
                    analyzed_at=now_str,
                )

            pos_result = await db.execute(
                sa_select(MT5Position).where(MT5Position.account_id == account_id)
            )
            positions = pos_result.scalars().all()

            if not positions:
                return AnalyzePositionsResponse(
                    account_id=account_id,
                    positions_analyzed=0,
                    summary="No open positions to analyze.",
                    analyzed_at=now_str,
                )

            client = MT5Client(acct)
            analyzed_symbols = set()

            for pos in positions:
                sym = pos.symbol
                if sym in analyzed_symbols:
                    # Already analyzed this symbol (duplicate positions)
                    continue
                analyzed_symbols.add(sym)

                try:
                    # Fetch H1 candles for SMC analysis
                    raw_candles = await client.get_candles(sym, "H1", 200)
                    candles = [
                        Candle(
                            time=int(c.get("time", 0)),
                            open=float(c.get("open", 0)),
                            high=float(c.get("high", 0)),
                            low=float(c.get("low", 0)),
                            close=float(c.get("close", 0)),
                            volume=float(c.get("volume", 0)),
                        )
                        for c in (raw_candles or [])
                        if c.get("close")
                    ]

                    if len(candles) < 40:
                        analyses.append(PositionAnalysis(
                            ticket=pos.mt5_ticket or 0,
                            symbol=sym,
                            side=pos.side.value if hasattr(pos.side, "value") else str(pos.side),
                            account_id=account_id,
                            analysis_text=f"Insufficient data for {sym} (< 40 bars).",
                            has_suggestion=False,
                            analyzed_at=now_str,
                        ))
                        continue

                    engine = SMCStrategyEngine(
                        symbol=sym,
                        min_rr=2.0,
                        max_rr=10.0,
                        min_confidence=0.55,
                        account_balance=float(acct.balance or 0),
                    )
                    result = engine.analyze(candles)
                    bias = result.get("bias", "neutral")
                    signals = result.get("signals", [])
                    fb = result.get("false_breakout", {})

                    pos_side = pos.side.value if hasattr(pos.side, "value") else str(pos.side)
                    profit = float(pos.profit or 0)
                    pnl_sign = "+" if profit >= 0 else ""

                    # Build analysis text
                    parts = [f"{sym} ({pos_side.upper()}) — Bias: {bias}."]
                    if fb.get("false_break_score", 0) >= 60:
                        if fb.get("sweep_high"):
                            parts.append("⚠ Sweep of highs detected — potential reversal risk.")
                        elif fb.get("sweep_low"):
                            parts.append("⚠ Sweep of lows detected — watch for continuation.")

                    sl_sug = tp_sug = None
                    ai_verdict = None
                    has_sug = False

                    if signals:
                        top = signals[0]
                        aligned = (
                            (pos_side in ("buy", "long") and top["side"] == "buy") or
                            (pos_side in ("sell", "short") and top["side"] == "sell")
                        )
                        if aligned:
                            tp_sug = top.get("take_profit")
                            sl_sug = top.get("stop_loss")
                            has_sug = bool(tp_sug or sl_sug)
                            conf_pct = int((top.get("confidence", 0)) * 100)
                            ai_verdict = f"{conf_pct}% confidence — {top.get('reason', '')[:80]}"
                            parts.append(
                                f"SMC signal aligned: RR {top.get('rr', 0):.1f}. "
                                f"Suggested TP: {tp_sug:.2f if tp_sug else 'N/A'}, "
                                f"SL: {sl_sug:.2f if sl_sug else 'N/A'}."
                            )
                        else:
                            parts.append(
                                f"SMC signal opposing current position "
                                f"({top['side'].upper()} setup in {bias} market)."
                            )

                    parts.append(f"Current P&L: {pnl_sign}{profit:.2f} {acct.currency or 'USD'}.")

                    analyses.append(PositionAnalysis(
                        ticket=pos.mt5_ticket or 0,
                        symbol=sym,
                        side=pos_side,
                        account_id=account_id,
                        analysis_text=" ".join(parts),
                        has_suggestion=has_sug,
                        sl_suggestion=sl_sug,
                        tp_suggestion=tp_sug,
                        ai_verdict=ai_verdict,
                        analyzed_at=now_str,
                    ))
                except Exception as exc:
                    logger.warning(f"[JARVIS analyze] {sym} failed: {exc}")
                    analyses.append(PositionAnalysis(
                        ticket=pos.mt5_ticket or 0,
                        symbol=sym,
                        side="unknown",
                        account_id=account_id,
                        analysis_text=f"Analysis failed for {sym}: {str(exc)[:80]}",
                        has_suggestion=False,
                        analyzed_at=now_str,
                    ))

        # Build spoken summary
        with_suggestions = [a for a in analyses if a.has_suggestion]
        if analyses:
            summary_parts = [f"Analysis complete for {len(analyses)} position(s)."]
            for a in analyses:
                summary_parts.append(a.analysis_text)
            if with_suggestions:
                summary_parts.append(
                    f"{len(with_suggestions)} position(s) have TP/SL suggestions."
                )
            summary = " ".join(summary_parts)
        else:
            summary = "No positions were analyzed."

        # Capture to JARVIS brain
        jarvis_brain_capture(
            "analyze-positions",
            summary=f"Analyzed {len(analyses)} positions for account {account_id}",
            detail=summary[:500],
            importance=0.6,
        )

        return AnalyzePositionsResponse(
            account_id=account_id,
            positions_analyzed=len(analyses),
            analyses=analyses,
            summary=summary,
            analyzed_at=now_str,
        )

    except Exception as exc:
        logger.error(f"[JARVIS analyze-positions] failed: {exc}")
        return AnalyzePositionsResponse(
            account_id=account_id,
            positions_analyzed=0,
            summary=f"Analysis failed: {str(exc)[:120]}",
            analyzed_at=now_str,
        )


# ── Command endpoint ───────────────────────────────────────────────────────────

@router.post("/command", response_model=CommandResult)
async def execute_command(req: CommandRequest):
    """
    Parse and execute a Jarvis voice command.

    Supported patterns
    ──────────────────
    • "take 1000% profit on GWEIUSDT"      → set TP at entry × 11
    • "take profit at 0.025 on GWEIUSDT"   → set TP at absolute price
    • "set stop loss at 5% on ETHUSDT"     → set SL at entry × 0.95
    • "close BTCUSDT" / "close my BTCUSDT position"
    • "what are my positions" / "show positions"
    • "how is BTCUSDT doing"               → status for that symbol
    """
    cmd = (req.command or "").strip().lower()
    logger.info(f"[JARVIS] command received: {cmd!r}")
    ex = req.exchange
    try:
        result = await _dispatch(cmd, ex)
        # ── Brain capture for EVERY request ───────────────────────────────────
        # Trades, TP/SL edits, analysis and position reviews get a rich capture;
        # everything else (errors, queries, chit-chat, unknown commands) still
        # gets logged so NOTHING JARVIS is ever asked is lost to the brains.
        _CAPTURE_ACTIONS = (
            "set_tp", "set_sl", "close", "execute",
            "analyze", "position_status", "list_positions",
        )
        captured = False
        if result.ok and result.action in _CAPTURE_ACTIONS:
            try:
                sym = result.order.get("symbol", "") if result.order else ""
                if not sym:
                    import re as _re_sym
                    # Only match real trading pairs (must end in USD/USDT) so
                    # English words like "WHAT" are never mistaken for a symbol.
                    _m = _re_sym.search(r"\b([A-Z]{2,8}USDT?)\b", (cmd or "").upper())
                    sym = _m.group(1) if _m else ""
                # Trades/edits are higher-importance learnings than read-only queries.
                _imp = 0.8 if result.action in ("set_tp", "set_sl", "close", "execute") else 0.4
                jarvis_brain_capture(
                    action=result.action,
                    symbol=sym,
                    summary=(result.speech or result.detail or "")[:200],
                    detail=result.detail or "",
                    tags=["jarvis", result.action, sym],
                    order_id=result.order.get("id", "") if result.order else "",
                    importance=_imp,
                )
                captured = True
            except Exception:
                pass
        # Catch-all: log every remaining request (failures, queries, unknown, …)
        if not captured:
            try:
                import re as _re_sym2
                _m2 = _re_sym2.search(r"\b([A-Z]{2,8}USDT?)\b", (cmd or "").upper())
                _sym2 = _m2.group(1) if _m2 else ""
                jarvis_learn_all_brains(
                    action=result.action or "command",
                    symbol=_sym2,
                    summary=(cmd or "")[:200],
                    detail=(result.speech or result.detail or "")[:600],
                    tags=["jarvis", "request", result.action or "command",
                          "ok" if result.ok else "error"],
                    importance=0.3 if result.ok else 0.35,
                )
            except Exception:
                pass
        return result
    except BaseException as e:
        friendly = "Sorry Sir, an internal error occurred."
        logger.error(f"[JARVIS] unhandled error in execute_command: {e}")
        return CommandResult(ok=False, action="error", detail=friendly, speech=friendly)


async def _dispatch(cmd: str, ex: Optional[str]) -> CommandResult:  # noqa: C901
    """Inner dispatcher — all pattern matching happens here."""

    # ── execute / open / place a new position ─────────────────────────────────
    # Must come FIRST so the AI page-handler never gets a chance to hallucinate.
    #
    # Handles forms like:
    #   execute VELVETUSDT short 2 lot at 1.7000; set SL 1.7500; TP1 1.5500; TP2 1.4500
    #   open BTCUSDT long 0.5 contracts at market
    #   short ETHUSDT 1 at 3200; sl 3400; tp 2800
    #   go long SOLUSDT 10
    _exe_m = (
        re.search(
            r'(?:execute|open|place|trade|enter)\s+'
            r'(\w+)\s+'                                       # symbol
            r'(long|short|buy|sell)\s+'                       # side
            r'\$?(\d+(?:\.\d+)?)\s*(?:lots?|contracts?|x)?\s*'  # size ($ OK)
            r'(?:at\s+\$?([\d.]+|market))?',                  # optional price
            cmd,
        )
        or re.search(                                         # "short SYMBOL 2 at 1.70"
            r'(?:^|\b)(long|short|buy|sell)\s+'
            r'(\w{3,15})\s+'
            r'\$?(\d+(?:\.\d+)?)\s*(?:lots?|contracts?|x)?\s*'
            r'(?:at\s+\$?([\d.]+|market))?',
            cmd,
        )
        or re.search(                                         # "go long SYMBOL 2"
            r'go\s+(long|short)\s+(?:on\s+)?(\w{3,15})\s+\$?(\d+(?:\.\d+)?)',
            cmd,
        )
    )
    if _exe_m:
        # Group indices differ between the three patterns above.
        # Detect by checking if group[1] is a direction word.
        _SIDES = {"long", "short", "buy", "sell"}
        g = _exe_m.groups()
        if g[1] and g[1].lower() in _SIDES:
            # Pattern 1: execute SYMBOL SIDE SIZE [at PRICE]
            sym_raw, side_raw, size_raw = g[0], g[1], g[2]
            price_raw = g[3] if len(g) > 3 else None
        else:
            # Pattern 2/3: SIDE SYMBOL SIZE [at PRICE]  or  go SIDE SYMBOL SIZE
            side_raw, sym_raw, size_raw = g[0], g[1], g[2]
            price_raw = g[3] if len(g) > 3 else None

        symbol_exec  = sym_raw.upper()
        side_exec    = side_raw.lower().replace("buy", "long").replace("sell", "short")
        # Strip any leading '$' from size/price (e.g. "$2 lots" → 2)
        size_exec    = float(str(size_raw).lstrip("$"))
        price_str_raw = str(price_raw).lstrip("$") if price_raw else None
        price_exec   = None if (not price_str_raw or price_str_raw.lower() == "market") else float(price_str_raw)

        # ── Extract SL ──────────────────────────────────────────────────────
        sl_m   = re.search(r'(?:set\s+)?(?:sl|stop[\s-]?loss)[;:\s]+([\d.]+)', cmd)
        sl_val = float(sl_m.group(1)) if sl_m else None

        # ── Extract TP1 / TP ────────────────────────────────────────────────
        tp1_m   = re.search(r'tp1?[;:\s]+([\d.]+)', cmd)
        tp1_val = float(tp1_m.group(1)) if tp1_m else None

        # ── Extract TP2 ──────────────────────────────────────────────────────
        tp2_m   = re.search(r'tp2[;:\s]+([\d.]+)', cmd)
        tp2_val = float(tp2_m.group(1)) if tp2_m else None

        return await _execute_order(
            symbol=symbol_exec,
            side=side_exec,
            size=size_exec,
            price=price_exec,
            sl_price=sl_val,
            tp1_price=tp1_val,
            tp2_price=tp2_val,
            ex_name=ex,
        )

    # ── analyse ALL open positions with news context ───────────────────────────
    # MUST be checked BEFORE the general _ana_m block.  Without this guard,
    # "analyse current positions" matches the _ana_m pattern and JARVIS tries
    # to resolve "CURRENT" as a Bitget trading pair — returning a nonsense
    # "Did you mean CETUS?" error instead of the intended portfolio review.
    #
    # Catches phrases like:
    #   "analyse current positions"         "analyze my positions"
    #   "with coming news analyse positions" "how will today's news impact my positions"
    #   "news impact on positions"          "positions and today's news"
    _NEWS_POS_PAT = re.compile(
        r'(?:'
        # "analyse [my [current]] positions"  — allow up to 3 modifier words
        # e.g. "analyse my current open positions"
        r'(?:analys[ei]|analyze|assess|review|check)\s+'
            r'(?:(?:my|all|open|current|the|latest|active|live|existing|today[\w]*)\s+){0,3}positions?'
        # "news impact on [my] positions"
        r'|(?:news|headlines?|market\s+news)\s+(?:impact|affect|effect)\s+'
            r'(?:on\s+)?(?:(?:my|current|open|the)\s+){0,2}positions?'
        # "how will today's news impact my positions"
        r'|how\s+will\s+(?:\w+\s+){0,5}news\s+(?:impact|affect)\s+'
            r'(?:(?:my|current|open|the)\s+){0,2}positions?'
        # "positions and/with today's news"
        r'|positions?\s+(?:and|with|given|considering)\s+(?:\w+\s+){0,3}news'
        r')',
        re.IGNORECASE,
    )
    if _NEWS_POS_PAT.search(cmd):
        return await _analyze_positions_with_news(cmd)

    # ── market analysis / monitor / sniper commands ────────────────────────────
    # These MUST be intercepted here so the AI page-handler CANNOT hallucinate
    # a fake execution.  We do real on-chain analysis and PROPOSE a trade —
    # the user must then say the explicit execute command to actually place it.
    _ana_m = (
        re.search(
            r'(?:monitor|watch|analyze|analyse|scan|sniper?|check)\s+'
            r'(\w{2,12})',
            cmd,
        )
        or re.search(
            r'find\s+(?:(?:more|a|some)\s+)?(?:buy|sell|long|short)\s+entr(?:y|ies)'
            r'(?:.*(?:for|on|in)\s+(\w{2,12}))?',
            cmd,
        )
    )
    if _ana_m:
        # Extract symbol — prefer explicit mention in command
        _sym_m = re.search(
            r'\b((?:BTC|ETH|SOL|BNB|XRP|DOGE|ADA|MATIC|AVAX|DOT|LINK|GWEI|VELVET|'
            r'PEPE|SHIB|WIF|BONK|FLOKI|[A-Z]{2,10})USDT?)\b',
            cmd.upper(),
        )
        if not _sym_m:
            # try first word after keyword
            if _ana_m.lastindex and _ana_m.group(_ana_m.lastindex):
                sym_candidate = _ana_m.group(_ana_m.lastindex).upper()
                if not sym_candidate.endswith("USDT"):
                    sym_candidate += "USDT"
            else:
                sym_candidate = ""
        else:
            sym_candidate = _sym_m.group(1)
            if not sym_candidate.endswith("USDT"):
                sym_candidate += "USDT"

        if sym_candidate:
            return await _analyze_symbol(sym_candidate, cmd, ex, deep=_wants_deep_research(cmd))
        return CommandResult(
            ok=False, action="analyze",
            detail="Which symbol should I analyse? E.g. 'monitor SOLUSDT'",
            speech="Which symbol should I analyse, Sir?",
        )

    # ── take / set TP by percentage ───────────────────────────────────────────
    # (re.search patterns below — keep separated so each can fail independently)
    m = re.search(
        r'(?:take|set)\s+(?:a\s+)?(\d+(?:\.\d+)?)\s*%\s*'
        r'(?:profit|return|roi|tp|take[\s-]profit)(?:\s+on\s+(\w+))?',
        cmd,
    )
    if m:
        pct    = float(m.group(1))
        symbol = (m.group(2) or "").upper()
        if not symbol:
            return _err("set_tp", "Could not determine symbol from command")
        return await _set_tp_pct(symbol, pct, ex)

    # ── set TP at absolute price ───────────────────────────────────────────────
    m = re.search(
        r'(?:set\s+)?(?:tp|take[\s-]profit)\s+at\s+([\d.]+)(?:\s+(?:on|for)\s+(\w+))?',
        cmd,
    )
    if m:
        price  = float(m.group(1))
        symbol = (m.group(2) or "").upper()
        return await _set_tp_price(symbol, price, ex)

    # ── set SL by percentage ───────────────────────────────────────────────────
    m = re.search(
        r'set\s+(?:a\s+)?(?:stop[\s-]loss|sl)\s+at\s+(\d+(?:\.\d+)?)\s*%(?:\s+(?:on|for)\s+(\w+))?',
        cmd,
    )
    if m:
        pct    = float(m.group(1))
        symbol = (m.group(2) or "").upper()
        return await _set_sl_pct(symbol, pct, ex)

    # ── set SL at absolute price ───────────────────────────────────────────────
    m = re.search(
        r'(?:set\s+)?(?:stop[\s-]loss|sl)\s+at\s+([\d.]+)(?:\s+(?:on|for)\s+(\w+))?',
        cmd,
    )
    if m:
        price  = float(m.group(1))
        symbol = (m.group(2) or "").upper()
        return await _set_sl_price(symbol, price, ex)

    # ── close position ─────────────────────────────────────────────────────────
    m = re.search(r'close(?:\s+my)?\s+(\w+)(?:\s+position)?', cmd)
    if m:
        symbol = m.group(1).upper()
        return await _close_position(symbol, ex)

    # ── list all positions ─────────────────────────────────────────────────────
    if re.search(r'(?:show|list|what(?:\s+are)?|get)\s+(?:my\s+)?(?:open\s+)?positions?', cmd):
        return await _list_positions()

    # ── status for a specific symbol ───────────────────────────────────────────
    m = re.search(r'how\s+is\s+(\w+)(?:\s+doing)?', cmd)
    if m:
        symbol = m.group(1).upper()
        return await _position_status(symbol, ex)

    return CommandResult(
        ok=False, action="unknown",
        detail=f"Command not recognised: {cmd!r}",
        speech=f"Sorry Sir, I didn't understand that command.",
    )


# ── Internal helpers ───────────────────────────────────────────────────────────

def _err(action: str, msg: str) -> CommandResult:
    return CommandResult(ok=False, action=action, detail=msg, speech=msg)


async def _execute_order(
    symbol: str,
    side: str,        # "long" | "short"
    size: float,
    price: Optional[float],   # None → market order
    sl_price: Optional[float],
    tp1_price: Optional[float],
    tp2_price: Optional[float],
    ex_name: Optional[str],
) -> CommandResult:
    """
    Place a new futures position on Bitget (or configured exchange).

    Uses the native Bitget SDK so the order IS actually submitted to the exchange.
    Preset TP1 and SL are attached to the entry order.  TP2 is placed as a
    separate TPSL plan order after the entry order is submitted.
    """
    # ── Find a connector ──────────────────────────────────────────────────────
    ex_list: List[SupportedExchange] = exchange_manager.get_all_exchanges()
    if ex_name:
        try:
            single = SupportedExchange(ex_name.lower())
            ex_list = [single] if single in ex_list else []
        except ValueError:
            ex_list = []

    if not ex_list:
        return _err("execute", "No exchange configured — check your API credentials.")

    connector = exchange_manager.get_exchange(ex_list[0])
    if not connector:
        return _err("execute", f"Exchange {ex_list[0].value} not initialised.")

    nc = getattr(connector, "native_client", None)
    if not nc:
        return _err("execute", "Native Bitget client not available. Check BITGET_PASSPHRASE.")

    # ── Map side to Bitget API direction ──────────────────────────────────────
    # long position = buy to open;  short position = sell to open
    bitget_side  = "buy" if side == "long" else "sell"
    close_side   = "sell" if side == "long" else "buy"
    order_type   = "limit" if price else "market"
    size_str     = _fmt_size(size)
    price_str    = _round_price(price) if price else None
    sl_str       = _round_price(sl_price) if sl_price else None
    tp1_str      = _round_price(tp1_price) if tp1_price else None

    logger.info(
        f"[JARVIS] execute_order: {symbol} {side} {size_str} @ "
        f"{price_str or 'market'} | SL={sl_str} TP1={tp1_str}"
    )

    # ── Error codes where we auto-retry with progressively smaller sizes ─────
    # 40921 = exceeds max position level for tier (existing positions near limit)
    # 40762 = exceeds available balance
    # 45110 = below Bitget's 5 USDT minimum notional
    # 40809 = size out of allowed range
    _SIZE_ERROR_CODES = {'40921', '40762', '45110', '40809', '40810'}

    async def _place_entry(use_size: str, note: str = "") -> tuple:
        """Try to place the entry order. Returns (result_dict, order_id, note)."""
        r = await nc.place_futures_order(
            symbol=symbol,
            margin_coin="USDT",
            side=bitget_side,
            order_type=order_type,
            size=use_size,
            price=price_str,
            trade_side="open",
            preset_stop_loss_price=sl_str,
            preset_stop_surplus_price=tp1_str,
        )
        oid = (r.get("data") or {}).get("orderId", "unknown")
        return r, oid, note

    result    = None
    order_id  = "unknown"
    auto_note = ""   # set if we auto-resized

    try:
        result, order_id, auto_note = await _place_entry(size_str)
    except BaseException as e:
        err_msg  = _friendly_exchange_error(e)
        raw      = str(e)
        code_m   = re.search(r'\[(\d+)\]', raw)
        err_code = code_m.group(1) if code_m else ""

        if err_code not in _SIZE_ERROR_CODES:
            logger.error(f"[JARVIS] execute_order failed [{err_code}]: {e}")
            return _err("execute", err_msg)

        # ── Auto-size: fetch equity, try halved / 1 % / minimum ──────────────
        logger.warning(f"[JARVIS] size error [{err_code}] — attempting auto-resize")

        equity = 0.0
        try:
            bal = await nc.get_futures_accounts(product_type="USDT-FUTURES")
            for acc in (bal.get("data") or []):
                eq = float(acc.get("equity") or acc.get("usdtEquity") or 0)
                if eq > equity:
                    equity = eq
        except BaseException as be:
            logger.warning(f"[JARVIS] could not fetch equity: {be}")

        # Candidate sizes: half, quarter, 1% portfolio, minimum=1
        ref_price = price or 1.0
        pct1_size = max(1.0, math.floor(equity * 0.01 / ref_price)) if equity > 0 else 1.0
        raw_candidates = [math.floor(size / 2), math.floor(size / 4), pct1_size, 1.0]
        candidates: List[float] = sorted(
            {c for c in raw_candidates if 1.0 <= c < size},
            reverse=True,
        ) or [1.0]

        last_err = err_msg
        placed   = False
        for candidate in candidates:
            cand_str = _fmt_size(candidate)
            logger.info(f"[JARVIS] auto-resize: trying {cand_str} contracts")
            try:
                result, order_id, _ = await _place_entry(cand_str)
                size = candidate
                auto_note = (
                    f"\n⚠️ Requested {size_str} contracts failed ({err_msg}). "
                    f"Auto-resized to **{cand_str} contracts**"
                    + (f" (≈1% of {equity:.2f} USDT equity)" if equity > 0 else "")
                    + "."
                )
                logger.info(f"[JARVIS] auto-sized order placed: {order_id} ({cand_str} contracts)")
                placed = True
                break
            except BaseException as e2:
                last_err = _friendly_exchange_error(e2)
                logger.warning(f"[JARVIS] size {cand_str} also failed: {e2}")

        if not placed:
            if err_code == '40921':
                friendly = (
                    f"{symbol} position level is at its maximum for your account tier. "
                    f"Your existing {symbol} positions are filling the tier's notional limit. "
                    f"Close some existing {symbol} positions first, then retry. "
                    f"(Bitget [{err_code}])"
                )
            else:
                friendly = (
                    f"Could not place order even at minimum size (1 contract). "
                    f"Last error: {last_err}"
                )
            return _err("execute", friendly)

    logger.info(f"[JARVIS] entry order placed: {order_id}")

    # ── TP2 placed as a fire-and-forget background task ───────────────────────
    # We do NOT await this — response returns immediately after the entry order.
    tp2_id = ""
    if tp2_price:
        tp2_str  = _round_price(tp2_price)
        tp2_size = _fmt_size(max(1.0, size / 2))
        _nc = nc  # capture for closure

        async def _bg_tp2() -> None:
            try:
                r2 = await _nc.place_futures_tpsl_order(
                    symbol=symbol,
                    margin_coin="USDT",
                    plan_type="pos_profit",
                    trigger_price=tp2_str,
                    size=tp2_size,
                    side=close_side,
                    trigger_type="fill_price",
                )
                oid2 = (r2.get("data") or {}).get("orderId", "")
                logger.info(f"[JARVIS] TP2 plan order placed in background: {oid2}")
            except BaseException as e2:
                logger.warning(f"[JARVIS] TP2 background placement failed: {e2}")

        asyncio.create_task(_bg_tp2())  # fire-and-forget
        tp2_id = "pending"

    # ── Build confirmation message (keep short for fast TTS) ─────────────────
    price_label = f"at {price}" if price else "at market"
    sl_part  = f", SL {sl_price}" if sl_price else ""
    tp1_part = f", TP1 {tp1_price}" if tp1_price else ""
    tp2_part = f", TP2 {tp2_price}" if tp2_price else ""
    speech = (
        f"{symbol} {side} {size} {price_label} — submitted. "
        f"ID {order_id}.{sl_part}{tp1_part}{tp2_part}"
    )
    detail = (
        f"Order {order_id} | {symbol} {side} {size}x {price_label}"
        + (f" | SL={sl_price}" if sl_price else "")
        + (f" | TP1={tp1_price}" if tp1_price else "")
        + (f" | TP2={tp2_price}" + (f" id={tp2_id}" if tp2_id else " (pending)") if tp2_price else "")
        + auto_note
    )

    return CommandResult(
        ok=True, action="execute",
        detail=detail,
        speech=speech,
        order={
            "id": order_id, "symbol": symbol, "side": side,
            "size": size, "price": price,
            "sl": sl_price, "tp1": tp1_price,
            "tp2": tp2_price, "tp2_id": tp2_id,
        },
    )


def _ema(closes: List[float], period: int) -> float:
    """Exponential Moving Average over `closes` list (last value returned)."""
    if len(closes) < period:
        return closes[-1] if closes else 0.0
    k = 2.0 / (period + 1)
    val = sum(closes[:period]) / period
    for c in closes[period:]:
        val = c * k + val * (1 - k)
    return val


def _rsi(closes: List[float], period: int = 14) -> float:
    """RSI(period) using Wilder smoothing, returns 0–100."""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return round(100 - (100 / (1 + rs)), 2)


# ── Deep-research helpers (volume · news · AI narrative) ─────────────────────
# These power JARVIS's rich, human, multi-tool pair analysis.  Each is fully
# self-contained and NEVER raises — a failure just omits that data section so
# the core proposal is always returned.

def _wants_deep_research(cmd: str) -> bool:
    """True when the user asked JARVIS to go deep — search news, scrape, research."""
    s = (cmd or "").lower()
    return bool(re.search(
        r"\b(news|headline|sentiment|research|deep|thorough|everything|"
        r"in[\s-]?depth|full|scrape|search|internet|web|fundament)\w*",
        s,
    ))


async def _crypto_volume_analysis(connector, ccxt_sym: str, ohlcv: list) -> Optional[Dict[str, Any]]:
    """Compute buy/sell volume pressure from OHLCV plus 24h ticker volume.

    Returns None on any failure (volume section simply omitted)."""
    try:
        vols   = [float(c[5]) for c in ohlcv if len(c) > 5]
        closes = [float(c[4]) for c in ohlcv]
        opens  = [float(c[1]) for c in ohlcv]
        if len(vols) < 5:
            return None
        last_vol = vols[-1]
        avg_vol  = sum(vols[-20:]) / min(len(vols), 20)
        # Up-candle vs down-candle volume over the last 20 candles → pressure proxy.
        buy_vol = sell_vol = 0.0
        for o, c, v in zip(opens[-20:], closes[-20:], vols[-20:]):
            if c >= o:
                buy_vol += v
            else:
                sell_vol += v
        tot = buy_vol + sell_vol
        buy_pct  = round(buy_vol / tot * 100, 1) if tot else 50.0
        sell_pct = round(100 - buy_pct, 1)

        quote_vol_24h = None
        try:
            ticker = await connector.exchange.fetch_ticker(f"{ccxt_sym}:USDT")
        except Exception:
            try:
                ticker = await connector.exchange.fetch_ticker(ccxt_sym)
            except Exception:
                ticker = None
        if ticker:
            quote_vol_24h = ticker.get("quoteVolume") or ticker.get("baseVolume")

        spike = round(last_vol / avg_vol, 2) if avg_vol else 1.0
        return {
            "buy_pressure_pct": buy_pct,
            "sell_pressure_pct": sell_pct,
            "last_candle_volume": round(last_vol, 4),
            "avg_volume_20": round(avg_vol, 4),
            "volume_spike_x": spike,
            "quote_volume_24h": quote_vol_24h,
        }
    except Exception as e:  # pragma: no cover
        logger.debug(f"[JARVIS] volume analysis skipped: {e}")
        return None


async def _fetch_pair_news(base: str, coin_name: Optional[str], deep: bool) -> Dict[str, Any]:
    """Fetch recent news for a token and (when deep) trigger a live internet scrape
    that stores fresh articles in the DB so JARVIS learns from captured data.

    Returns {articles, count, avg_sentiment, sentiment_label, scraped}."""
    result: Dict[str, Any] = {
        "articles": [], "count": 0, "avg_sentiment": 0.0,
        "sentiment_label": "neutral", "scraped": False,
    }
    try:
        from app.core.database import AsyncSessionLocal
        from app.sentiment.enhanced_service import EnhancedSentimentService

        async with AsyncSessionLocal() as db:
            articles = await EnhancedSentimentService.get_articles(
                db, symbol=base, hours=48, limit=15
            )
            # DEEP: if stored coverage is thin, scrape the live internet sources,
            # store + score them (learning), then re-query for this token.
            if deep and len(articles) < 4:
                try:
                    await asyncio.wait_for(
                        EnhancedSentimentService.run_full_cycle(db, max_age_hours=48),
                        timeout=30,
                    )
                    result["scraped"] = True
                    articles = await EnhancedSentimentService.get_articles(
                        db, symbol=base, hours=48, limit=15
                    )
                except Exception as e:
                    logger.debug(f"[JARVIS] live news scrape skipped: {e}")

            # Fallback: obscure tokens are rarely tagged by exact symbol, so do a
            # broad text search on the coin name/base so the user still gets any
            # relevant headlines they explicitly asked for.
            if not articles:
                term = (coin_name or base or "").strip()
                if term:
                    try:
                        articles = await EnhancedSentimentService.get_articles(
                            db, search=term, hours=48, limit=8
                        )
                    except Exception:
                        articles = []

        scores = [
            a.get("sentiment_score") for a in articles
            if isinstance(a.get("sentiment_score"), (int, float))
        ]
        avg = round(sum(scores) / len(scores), 3) if scores else 0.0
        label = "bullish" if avg > 0.1 else "bearish" if avg < -0.1 else "neutral"
        result.update({
            "articles": articles, "count": len(articles),
            "avg_sentiment": avg, "sentiment_label": label,
        })
    except Exception as e:  # pragma: no cover
        logger.debug(f"[JARVIS] news fetch skipped: {e}")
    return result


async def _find_open_position(symbol: str) -> Optional[Dict[str, Any]]:
    """Return the user's open position for `symbol` (any exchange) as a compact
    dict, or None. Matches on the normalised base+quote so BTCUSDT ≡ BTC/USDT."""
    try:
        want = symbol.upper().replace("/", "").replace(":USDT", "")
        want_base = want.replace("USDT", "").replace("USDC", "")
        positions = await get_all_positions()
        for p in positions:
            have = (p.symbol or "").upper().replace("/", "")
            have_base = have.replace("USDT", "").replace("USDC", "")
            if have == want or have_base == want_base:
                return {
                    "exchange": p.exchange,
                    "symbol": p.symbol,
                    "side": p.side,
                    "size": p.size,
                    "entry_price": p.entry_price,
                    "mark_price": p.mark_price,
                    "pnl": p.pnl,
                    "pnl_pct": p.pnl_pct,
                    "leverage": p.leverage,
                    "liquidation_price": p.liquidation_price,
                }
    except Exception as e:  # pragma: no cover
        logger.debug(f"[JARVIS] open-position lookup skipped: {e}")
    return None


async def _compose_ai_narrative(brief: str) -> Optional[str]:
    """Ask the multi-provider AI router (OpenAI-preferred failover) to turn the raw
    research brief into a natural, human, decisive analysis.  Returns None if no
    AI provider is available so the caller falls back to the template."""
    try:
        from app.core.database import AsyncSessionLocal
        from plugins.AiMarketAnalyst.backend.services.ai_router import db_chat

        async with AsyncSessionLocal() as db:
            resp = await db_chat(
                db,
                [
                    {
                        "role": "system",
                        "content": (
                            "You are JARVIS, an elite crypto trading analyst speaking to your "
                            "principal (address him as 'Sir'). Write a natural, confident, human "
                            "analysis — never robotic or list-only. Weave the technicals, volume "
                            "flow, the Sox ML forecast and the news/sentiment into one coherent "
                            "read of the pair, then give a clear directional bias and the key risk. "
                            "If the brief says the user ALREADY HOLDS AN OPEN POSITION on this pair, "
                            "give a direct recommendation on that position — hold, add, reduce, close, "
                            "or move the stop / take-profit — and say why, referencing his live PnL. "
                            "Be specific with the numbers you were given. Do NOT invent data you "
                            "were not given. Keep it to 4-8 tight sentences."
                        ),
                    },
                    {"role": "user", "content": brief},
                ],
                temperature=0.4,
                max_tokens=650,
                agent_name="jarvis-deep-analysis",
                source="jarvis",
            )
            if resp.get("ok") and resp.get("content"):
                return str(resp["content"]).strip()
    except Exception as e:  # pragma: no cover
        logger.debug(f"[JARVIS] AI narrative skipped: {e}")
    return None


async def _analyze_symbol(symbol: str, original_cmd: str, ex_name: Optional[str], deep: bool = False) -> CommandResult:
    """
    Real-data market analysis for `symbol`.

    For crypto pairs: Fetches 4H OHLCV from Bitget.
    For forex/metals (XAUUSD, XAGUSD, EURUSD …): Fetches from Yahoo Finance
    via the ForexProvider so that live gold/silver prices are always current.

    IMPORTANT: This function NEVER places orders.  It returns a proposal
    with the exact Jarvis command the user must say to execute it.
    """
    # ── Route: Forex / metals (XAUUSD, XAGUSD, etc.) ─────────────────────────
    if is_forex_symbol(symbol):
        try:
            ohlcv, forex_ticker = await forex_fetch_ohlcv(symbol, timeframe="4h", limit=200)
        except Exception as e:
            if _is_network_error(e):
                return _err("analyze", "Network unreachable — cannot fetch live gold/forex price.")
            return _err("analyze", f"Forex price fetch failed: {e}")

        if not ohlcv or len(ohlcv) < 20:
            return _err("analyze", f"Not enough historical data for {symbol}.")

        closes  = [float(c[4]) for c in ohlcv]
        highs   = [float(c[2]) for c in ohlcv]
        lows    = [float(c[3]) for c in ohlcv]
        current = closes[-1]
        buy_pct  = forex_ticker.get("buy_pct", 0)
        sell_pct = forex_ticker.get("sell_pct", 0)
        buy_vol  = forex_ticker.get("buy_volume", 0)
        sell_vol = forex_ticker.get("sell_volume", 0)

        ema50  = _ema(closes, 50)
        ema200 = _ema(closes, 200)
        rsi    = _rsi(closes, 14)

        swing_high = max(highs[-20:])
        swing_low  = min(lows[-20:])

        if current > ema200 and ema50 > ema200:
            trend, bias = "uptrend", "long"
        elif current < ema200 and ema50 < ema200:
            trend, bias = "downtrend", "short"
        else:
            trend = "ranging"
            bias  = "long" if current > ema200 else "short"

        if rsi > 70:
            rsi_label, bias = "overbought", "short"
        elif rsi < 30:
            rsi_label, bias = "oversold", "long"
        else:
            rsi_label = f"neutral ({rsi:.0f})"

        # Volume flow bias overrides when strong
        if buy_pct >= 60:
            bias = "long"
        elif sell_pct >= 60:
            bias = "short"

        dp = _price_dp(current)
        if bias == "long":
            entry      = round(swing_low * 1.001,  dp)
            sl         = round(swing_low * 0.985,  dp)
            tp1        = round(current * 1.02,     dp)
            tp2        = round(swing_high * 0.99,  dp)
            side, side_label = "long", "BUY"
        else:
            entry      = round(swing_high * 0.999, dp)
            sl         = round(swing_high * 1.015, dp)
            tp1        = round(current * 0.98,     dp)
            tp2        = round(swing_low * 1.01,   dp)
            side, side_label = "short", "SHORT"

        risk    = abs(entry - sl)
        reward1 = abs(tp1 - entry) if bias == "long" else abs(entry - tp1)
        reward2 = abs(tp2 - entry) if bias == "long" else abs(entry - tp2)
        rr1     = round(reward1 / risk, 1) if risk > 0 else 0
        rr2     = round(reward2 / risk, 1) if risk > 0 else 0

        confirm_cmd = (
            f"execute {symbol} {side} 5 lot at {entry}; "
            f"set SL {sl}; TP1 {tp1}; TP2 {tp2}"
        )

        volume_line = (
            f"Live Volume Split: BUY {buy_pct:.0f}% ({buy_vol:,.0f}) / "
            f"SELL {sell_pct:.0f}% ({sell_vol:,.0f})\n"
        )

        detail = (
            f"{symbol} | {trend.upper()} | RSI {rsi:.0f} ({rsi_label})\n"
            f"EMA50={ema50:.4g}  EMA200={ema200:.4g}  Current={current:.4g}\n"
            f"Swing Hi={swing_high:.4g}  Swing Lo={swing_low:.4g}\n"
            f"{volume_line}"
            f"\nPROPOSED {side_label} SETUP (LIVE YAHOO FINANCE DATA — NOT EXECUTED)\n"
            f"Entry : {entry}  |  SL : {sl}  |  TP1 : {tp1} (R:R {rr1}x)  |  TP2 : {tp2} (R:R {rr2}x)\n"
            f"\nTo execute say:\n  \"{confirm_cmd}\""
        )

        speech = (
            f"{symbol} live analysis: {trend}, RSI {rsi:.0f}, {rsi_label}. "
            f"Current price {current}. Buy pressure {buy_pct:.0f}%, sell pressure {sell_pct:.0f}%. "
            f"Proposed {side_label.lower()} entry at {entry}, SL {sl}, TP1 {tp1}. "
            f"This is a proposal — say the execute command to confirm, Sir."
        )

        return CommandResult(
            ok=True, action="analyze",
            detail=detail,
            speech=speech,
            order={
                "symbol": symbol, "side": side, "proposed_entry": entry,
                "sl": sl, "tp1": tp1, "tp2": tp2,
                "current_price": current,
                "rsi": rsi, "trend": trend,
                "ema50": round(ema50, 6), "ema200": round(ema200, 6),
                "buy_volume_pct": buy_pct, "sell_volume_pct": sell_pct,
                "buy_volume": buy_vol, "sell_volume": sell_vol,
                "price_source": "yahoo_finance_live",
                "confirm_command": confirm_cmd,
                "WARNING": "NOT EXECUTED — say the confirm_command to place the order",
            },
        )

    # ── Route: Crypto symbols via Bitget ─────────────────────────────────────
    # Resolve the token → canonical Bitget pair + REAL coin name so JARVIS can
    # talk about "Bitcoin" (not "BTCUSDT") and never surfaces a raw ccxt
    # "does not have market" error for a token that simply needs resolving.
    coin_name: Optional[str] = None
    _input_token = symbol  # the raw token before canonicalisation
    try:
        from app.services import pair_catalog
        resolved_pair, suggestion = await pair_catalog.resolve_with_suggestion(symbol)
        if resolved_pair is None:
            token = symbol.replace("USDT", "").replace("USDC", "") or symbol
            if suggestion:
                msg = f"I couldn't find a Bitget pair for {token}. Did you mean {suggestion}?"
            else:
                msg = f"I couldn't find a Bitget-tradeable pair for {token}, Sir."
            return CommandResult(ok=False, action="analyze", detail=msg, speech=msg)
        coin_name = resolved_pair.name or resolved_pair.base
        # Canonical glued form for downstream ccxt normalisation (e.g. BTCUSDT).
        symbol = f"{resolved_pair.base}{resolved_pair.quote}"
        # Learn the user's bare token as an alias so it resolves instantly next
        # time (learn_alias no-ops when it equals the symbol/base/name).
        try:
            await pair_catalog.learn_alias(pair_catalog._strip_quote(_input_token), resolved_pair.symbol)
        except Exception:
            pass
    except Exception as e:
        logger.debug(f"[JARVIS] pair resolution skipped: {e}")

    ex_list: List[SupportedExchange] = exchange_manager.get_all_exchanges()
    if ex_name:
        try:
            single = SupportedExchange(ex_name.lower())
            ex_list = [single] if single in ex_list else []
        except ValueError:
            ex_list = []

    connector = exchange_manager.get_exchange(ex_list[0]) if ex_list else None
    if not connector:
        return _err("analyze", "No exchange configured for analysis.")

    # Normalise: SOLUSDT → SOL/USDT  (ccxt format)
    base   = symbol.replace("USDT", "").replace("USDC", "")
    ccxt_sym = f"{base}/USDT"
    # Spoken/display label: the real coin name when known, else the symbol.
    display_name = coin_name or symbol

    # ── Fetch 4H OHLCV (200 candles) ─────────────────────────────────────────
    try:
        ohlcv = await connector.exchange.fetch_ohlcv(
            f"{ccxt_sym}:USDT", timeframe="4h", limit=200
        )
    except Exception:
        try:
            ohlcv = await connector.exchange.fetch_ohlcv(
                ccxt_sym, timeframe="4h", limit=200
            )
        except BaseException as e:
            if _is_network_error(e):
                return _err("analyze", "Exchange unreachable — check network.")
            return _err("analyze", _friendly_exchange_error(e))

    if not ohlcv or len(ohlcv) < 20:
        return _err("analyze", f"Not enough data for {symbol}.")

    closes  = [float(c[4]) for c in ohlcv]
    highs   = [float(c[2]) for c in ohlcv]
    lows    = [float(c[3]) for c in ohlcv]
    current = closes[-1]

    # ── Technical indicators ──────────────────────────────────────────────────
    ema50  = _ema(closes, 50)
    ema200 = _ema(closes, 200)
    rsi    = _rsi(closes, 14)

    # Recent swing high / low (last 20 candles)
    swing_high = max(highs[-20:])
    swing_low  = min(lows[-20:])

    # Trend determination
    if current > ema200 and ema50 > ema200:
        trend = "uptrend"
        bias  = "long"
    elif current < ema200 and ema50 < ema200:
        trend = "downtrend"
        bias  = "short"
    else:
        trend = "ranging"
        bias  = "long" if current > ema200 else "short"

    # Overbought/oversold
    if rsi > 70:
        rsi_label = "overbought"
        bias = "short"   # override
    elif rsi < 30:
        rsi_label = "oversold"
        bias = "long"    # override
    else:
        rsi_label = f"neutral ({rsi:.0f})"

    # ── Propose entry, SL, TP levels ─────────────────────────────────────────
    if bias == "long":
        # Enter near recent low / swing support with TP at swing high
        entry   = round(swing_low * 1.001, _price_dp(current))   # just above support
        sl      = round(swing_low * 0.985, _price_dp(current))   # 1.5% below support
        tp1     = round(current  * 1.02,  _price_dp(current))    # +2% from current
        tp2     = round(swing_high * 0.99, _price_dp(current))   # near swing high
        side    = "long"
        side_label = "BUY"
    else:
        entry   = round(swing_high * 0.999, _price_dp(current))
        sl      = round(swing_high * 1.015, _price_dp(current))
        tp1     = round(current  * 0.98,   _price_dp(current))
        tp2     = round(swing_low * 1.01,  _price_dp(current))
        side    = "short"
        side_label = "SHORT"

    # Risk/reward
    if bias == "long":
        risk    = abs(entry - sl)
        reward1 = abs(tp1 - entry)
        reward2 = abs(tp2 - entry)
    else:
        risk    = abs(sl - entry)
        reward1 = abs(entry - tp1)
        reward2 = abs(entry - tp2)

    rr1 = round(reward1 / risk, 1) if risk > 0 else 0
    rr2 = round(reward2 / risk, 1) if risk > 0 else 0

    # ── Build response ────────────────────────────────────────────────────────
    confirm_cmd = (
        f"execute {symbol} {side} 5 lot at {entry}; "
        f"set SL {sl}; TP1 {tp1}; TP2 {tp2}"
    )

    detail = (
        f"{display_name} ({symbol}) | {trend.upper()} | RSI {rsi:.0f} ({rsi_label})\n"
        f"EMA50={ema50:.4g}  EMA200={ema200:.4g}  Current={current:.4g}\n"
        f"Swing Hi={swing_high:.4g}  Swing Lo={swing_low:.4g}\n"
        f"\nPROPOSED {side_label} SETUP (REAL DATA — NOT EXECUTED)\n"
        f"Entry : {entry}  |  SL : {sl}  |  TP1 : {tp1} (R:R {rr1}x)  |  TP2 : {tp2} (R:R {rr2}x)\n"
        f"\nTo execute say:\n  \"{confirm_cmd}\""
    )

    speech = (
        f"{display_name} analysis: {trend}, RSI {rsi:.0f}, {rsi_label}. "
        f"Proposed {side_label.lower()} entry at {entry}, SL {sl}, "
        f"TP1 {tp1}. "
        f"This is a proposal — say the execute command to confirm, Sir."
    )

    # ── Kronos ML forecast (optional plugin — graceful, never breaks) ────────
    kronos_info = None
    try:
        from plugins.KronosForecastPlugin.backend.services import forecast_service as _kronos
        _ex_id = ex_list[0].value if ex_list else "bitget"
        _fc = await _kronos.run_forecast_cached(_ex_id, ccxt_sym, "4h", pred_len=12)
        if _fc and _fc.signal:
            s = _fc.signal
            kronos_info = {
                "direction": s.direction, "pct_change": s.pct_change,
                "confidence": s.confidence, "target_price": s.target_price,
                "engine": _fc.engine,
            }
            detail += (
                f"\n\nSOX ML FORECAST ({_fc.engine}) — next 12×4h\n"
                f"Direction: {s.direction.upper()}  |  {s.pct_change:+.2f}%  "
                f"|  Target {s.target_price:.4g}  |  {int(s.confidence*100)}% confidence"
            )
            speech += (
                f" Sox forecasts {s.direction} {s.pct_change:+.1f} percent "
                f"over the next two days at {int(s.confidence*100)} percent confidence."
            )
    except Exception as _ke:
        logger.debug(f"[JARVIS] Kronos forecast skipped: {_ke}")

    # ── Volume flow + News/sentiment + AI narrative (deep research) ──────────
    # Always add volume + stored news; only trigger a fresh internet scrape when
    # the user explicitly asked for news/research (keyword-gated for speed).
    volume_info = await _crypto_volume_analysis(connector, ccxt_sym, ohlcv)
    news_info = await _fetch_pair_news(base, coin_name, deep)
    position_info = await _find_open_position(symbol)

    if volume_info:
        detail += (
            f"\n\nVOLUME FLOW — buy {volume_info['buy_pressure_pct']:.0f}% / "
            f"sell {volume_info['sell_pressure_pct']:.0f}%"
            f"  (last candle {volume_info['volume_spike_x']:.1f}× the 20-bar average"
            + (f", 24h vol {volume_info['quote_volume_24h']:,.0f}"
               if isinstance(volume_info.get('quote_volume_24h'), (int, float)) else "")
            + ")"
        )
        speech += (
            f" Volume is {volume_info['buy_pressure_pct']:.0f} percent buy-side."
        )

    news_lines: List[str] = []
    for a in (news_info.get("articles") or [])[:4]:
        sc = a.get("sentiment_score")
        lbl = a.get("sentiment_label") or (
            "BULLISH" if (sc or 0) > 0.1 else "BEARISH" if (sc or 0) < -0.1 else "NEUTRAL"
        )
        src = a.get("source") or ""
        title = (a.get("title") or "")[:130]
        if title:
            news_lines.append(f"[{str(lbl).upper()}] {title}" + (f" — {src}" if src else ""))
    if news_lines:
        detail += (
            f"\n\nNEWS & SENTIMENT ({news_info['count']} headlines, "
            f"{news_info['sentiment_label'].upper()})\n" + "\n".join(f"• {l}" for l in news_lines)
        )
    elif deep:
        detail += "\n\nNEWS & SENTIMENT — no fresh headlines found for this pair."

    # ── Your open position on this pair (if any) ────────────────────────────
    pos_brief = ""
    if position_info:
        _pdir = str(position_info.get("side", "")).upper()
        _pnl = position_info.get("pnl") or 0
        _pnl_pct = position_info.get("pnl_pct") or 0
        _arrow = "▲" if _pnl >= 0 else "▼"
        detail += (
            f"\n\nYOUR OPEN POSITION — {_pdir} {position_info.get('size')} @ "
            f"{position_info.get('entry_price')} (mark {position_info.get('mark_price')})\n"
            f"PnL {_arrow} {abs(_pnl):.2f} USDT ({_pnl_pct:+.2f}%)"
            + (f"  ·  liq {position_info.get('liquidation_price')}"
               if position_info.get("liquidation_price") else "")
        )
        pos_brief = (
            f"\nUSER ALREADY HOLDS AN OPEN POSITION on this pair: {_pdir} size "
            f"{position_info.get('size')} entered at {position_info.get('entry_price')}, "
            f"mark {position_info.get('mark_price')}, live PnL {_pnl:+.2f} USDT ({_pnl_pct:+.2f}%)"
            + (f", leverage {position_info.get('leverage')}x" if position_info.get("leverage") else "")
            + (f", liquidation {position_info.get('liquidation_price')}"
               if position_info.get("liquidation_price") else "")
            + ". Advise specifically what to do with THIS position."
        )

    # ── AI-composed human narrative (the natural JARVIS voice) ──────────────
    _kronos_line = ""
    if kronos_info:
        _kronos_line = (
            f"Sox ML forecast: {kronos_info['direction']} "
            f"{kronos_info['pct_change']:+.2f}% (target {kronos_info['target_price']:.6g}, "
            f"{int(kronos_info['confidence'] * 100)}% confidence)."
        )
    brief = (
        f"Pair: {display_name} ({symbol}) on {ex_list[0].value if ex_list else 'bitget'}, 4h chart.\n"
        f"Price {current:.6g}. Trend {trend}. RSI {rsi:.0f} ({rsi_label}). "
        f"EMA50 {ema50:.6g}, EMA200 {ema200:.6g}. "
        f"Swing high {swing_high:.6g}, swing low {swing_low:.6g}.\n"
        + (f"Volume: buy {volume_info['buy_pressure_pct']:.0f}% / sell {volume_info['sell_pressure_pct']:.0f}%, "
           f"last candle {volume_info['volume_spike_x']:.1f}x avg.\n" if volume_info else "")
        + (_kronos_line + "\n" if _kronos_line else "")
        + (f"News sentiment: {news_info['sentiment_label']} across {news_info['count']} recent headlines.\n"
           if news_info['count'] else "News: no fresh headlines for this pair.\n")
        + (("Headlines:\n" + "\n".join(f"- {l}" for l in news_lines) + "\n") if news_lines else "")
        + f"My proposed setup: {side_label} — entry {entry}, SL {sl}, TP1 {tp1} (R:R {rr1}x), "
        f"TP2 {tp2} (R:R {rr2}x)."
        + pos_brief
    )
    narrative = await _compose_ai_narrative(brief)

    if narrative:
        levels_block = (
            f"PROPOSED {side_label} SETUP (real data — NOT executed)\n"
            f"Entry {entry}  |  SL {sl}  |  TP1 {tp1} (R:R {rr1}x)  |  TP2 {tp2} (R:R {rr2}x)\n"
            f"To execute say:  \"{confirm_cmd}\""
        )
        detail = f"{narrative}\n\n{levels_block}"
        speech = narrative[:520].replace("\n", " ")

    # ── Learn: persist this research + narrative to all three brains ────────
    try:
        jarvis_learn_all_brains(
            action="deep_analysis" if deep else "analysis",
            symbol=symbol,
            summary=(narrative or speech or "")[:200],
            detail=detail[:1200],
            tags=["jarvis", "analysis", base, trend],
            importance=0.6 if deep else 0.45,
        )
    except Exception:
        pass

    return CommandResult(
        ok=True, action="analyze",
        detail=detail,
        speech=speech,
        order={
            "symbol": symbol, "side": side, "proposed_entry": entry,
            "sl": sl, "tp1": tp1, "tp2": tp2,
            "rsi": rsi, "trend": trend, "ema50": round(ema50, 6),
            "ema200": round(ema200, 6), "confirm_command": confirm_cmd,
            "kronos": kronos_info,
            "volume": volume_info,
            "news": [
                {
                    "title": a.get("title"),
                    "source": a.get("source"),
                    "url": a.get("url"),
                    "sentiment_score": a.get("sentiment_score"),
                    "sentiment_label": a.get("sentiment_label"),
                }
                for a in (news_info.get("articles") or [])[:6]
            ],
            "news_count": news_info.get("count", 0),
            "sentiment_label": news_info.get("sentiment_label"),
            "sentiment_score": news_info.get("avg_sentiment"),
            "position": position_info,
            "narrative": narrative,
            "deep": deep,
            "WARNING": "NOT EXECUTED — say the confirm_command to place the order",
        },
    )


def _price_dp(price: float) -> int:
    """Return appropriate decimal places for a price (e.g. 73.8 → 3, 0.00023 → 7)."""
    if price == 0:
        return 4
    mag = math.floor(math.log10(abs(price)))
    if mag >= 3:
        return 1
    if mag >= 1:
        return 3
    if mag >= -1:
        return 4
    return max(4, 2 - mag)


def _is_network_error(e: BaseException) -> bool:
    """Return True for DNS / socket / network errors so we can show a friendly message."""
    msg = str(e).lower()
    network_signals = (
        "nodename nor servname",   # macOS/Linux DNS failure
        "name or service not known",
        "getaddrinfo failed",
        "errno 8",
        "errno 11001",             # Windows DNS
        "connection refused",
        "timed out",
        "network error",
        "cannot connect",
        "ssl:",
    )
    return any(s in msg for s in network_signals)


async def _find_position(symbol: str, ex_name: Optional[str]):
    """Find a position and its connector by normalised symbol.

    Returns (connector, raw_position_dict) or (None, None) if not found.
    Never raises — network/exchange errors are swallowed with a warning.
    """
    ex_list: List[SupportedExchange] = exchange_manager.get_all_exchanges()
    if ex_name:
        try:
            single = SupportedExchange(ex_name.lower())
            ex_list = [single] if single in ex_list else []
        except ValueError:
            ex_list = []

    for ex_enum in ex_list:
        connector = exchange_manager.get_exchange(ex_enum)
        if not connector:
            continue
        try:
            raw_list = await connector.exchange.fetch_positions()
            for p in raw_list:
                if _safe_float(p.get("contracts")) <= 0:
                    continue
                if _match_symbol(symbol, p.get("symbol", "")):
                    return connector, p
        except BaseException as e:   # BaseException catches OSError, asyncio errors, etc.
            if _is_network_error(e):
                logger.warning(
                    f"[JARVIS] {ex_enum.value} unreachable (DNS/network): {e}"
                )
            else:
                logger.warning(f"[JARVIS] _find_position({ex_enum.value}): {e}")
    return None, None


def _friendly_exchange_error(e: BaseException) -> str:
    """Parse a raw exchange error and return a human-readable string."""
    raw = str(e)
    try:
        import json as _json
        # ccxt wraps responses as "bitget {'code':'...','msg':'...'}"
        # strip leading exchange name if present
        json_start = raw.find("{")
        if json_start != -1:
            d = _json.loads(raw[json_start:])
            msg = d.get("msg") or d.get("message") or d.get("error")
            if msg:
                return str(msg)
    except Exception:
        pass
    return raw


def _round_price(price: float, decimals: int = 5) -> str:
    """Round a price to `decimals` places and return as string.

    Defaults to 5 dp (Bitget USDT-FUTURES standard for sub-$1 contracts).
    """
    rounded = round(price, decimals)
    fmt = f"{{:.{decimals}f}}".format(rounded).rstrip("0").rstrip(".")
    return fmt or "0"


def _fmt_size(size: float) -> str:
    """Format contract size for Bitget: '211' not '211.0'."""
    i = int(size)
    return str(i) if float(i) == size else str(size)


def _bitget_margin_mode(raw: Optional[str]) -> str:
    """Normalise margin mode for Bitget native API."""
    if not raw:
        return "crossed"
    return "isolated" if str(raw).lower() == "isolated" else "crossed"

def _bitget_sym(raw_sym: str) -> str:
    """'GWEI/USDT:USDT' → 'GWEIUSDT'  (Bitget native API format)."""
    return raw_sym.split(":")[0].replace("/", "")


async def _set_tp_pct(symbol: str, pct: float, ex_name: Optional[str]) -> CommandResult:
    """Set take-profit for a percentage ROI.

    For LONG:  TP = entry × (1 + pct/100)  — price goes up
    For SHORT: TP = entry × (1 - pct/100÷leverage) — price goes down
               Uses position leverage; capped so tp_price > 0.
    """
    connector, pos = await _find_position(symbol, ex_name)
    if pos is None:
        return _err("set_tp", f"No open position found for {symbol}")

    entry    = _safe_float(pos.get("entryPrice"))
    mark     = _safe_float(pos.get("markPrice")) or entry   # use mark for trigger distance check
    side     = str(pos.get("side") or "long").lower()
    leverage = _safe_float(pos.get("leverage")) or 10.0

    if entry <= 0:
        return _err("set_tp", f"Cannot determine entry price for {symbol}")

    if side == "short":
        # For SHORT: profitable when price drops. TP must be BELOW mark.
        # pct is interpreted as target ROI on margin → price_drop = pct/leverage.
        price_drop_pct = pct / leverage
        tp_price = round(mark * (1 - price_drop_pct / 100), 8)
        if price_drop_pct >= 100 or tp_price <= 0:
            msg = (
                f"A {pct:.0f}% ROI on a {leverage:.0f}x short would require the "
                f"price to drop {price_drop_pct:.0f}% — not achievable. "
                f"Try something under {leverage * 90:.0f}%."
            )
            return CommandResult(ok=False, action="set_tp", detail=msg, speech=msg)
    else:
        price_rise_pct = pct / leverage
        tp_price = round(mark * (1 + price_rise_pct / 100), 8)

    return await _set_tp_price(symbol, tp_price, ex_name, _connector=connector, _pos=pos)


async def _set_tp_price(
    symbol: str,
    price: float,
    ex_name: Optional[str],
    *,
    _connector=None,
    _pos=None,
) -> CommandResult:
    """Place a take-profit TPSL order at an absolute price.

    Uses connector.place_tpsl_order (mark_price, no extraneous side param).
    """
    if _connector is None:
        _connector, _pos = await _find_position(symbol, ex_name)
    if _pos is None:
        return _err("set_tp", f"No open position found for {symbol}")

    raw_sym    = _pos.get("symbol", symbol)
    info       = _pos.get("info", {})
    side       = str(_pos.get("side") or "long").lower()
    size       = abs(_safe_float(_pos.get("contracts")))
    close_side = "sell" if side == "long" else "buy"

    # Hedge-mode detection: holdSide is in the ccxt info dict
    hold_side_raw = info.get("holdSide") or _pos.get("holdSide")
    is_hedge      = bool(hold_side_raw)
    hold_side     = str(hold_side_raw or side).lower()
    plan_type     = "profit_plan" if is_hedge else "pos_profit"

    # ── Bitget connector.place_tpsl_order (proven, mark_price, no side param) ──
    if hasattr(_connector, "place_tpsl_order"):
        try:
            bsym   = _bitget_sym(raw_sym)
            result = await _connector.place_tpsl_order(
                symbol        = bsym,
                margin_coin   = "USDT",
                plan_type     = plan_type,
                trigger_price = float(price),
                hold_side     = hold_side,
                size          = _fmt_size(size) if is_hedge else None,
            )
            oid    = result.get("orderId", "") or result.get("clientOid", "")
            speech = (
                f"Take profit set at {price} for {symbol}. Order ID {oid}."
                if oid else f"Take profit set at {price} for {symbol}."
            )
            return CommandResult(
                ok=True, action="set_tp",
                detail=f"TP @ {price} for {symbol} ({size} contracts, {close_side})",
                speech=speech,
                order={"id": oid, "price": price, "symbol": symbol},
            )
        except BaseException as e:
            err_msg = _friendly_exchange_error(e)
            logger.error(f"[JARVIS] set_tp (bitget connector) failed: {e}")
            return _err("set_tp", err_msg)

    # ── Generic ccxt fallback ──────────────────────────────────────────────────
    try:
        order = await _connector.exchange.create_order(
            symbol=raw_sym, type="TAKE_PROFIT_MARKET", side=close_side, amount=size,
            params={"stopPrice": price, "reduceOnly": True, "workingType": "MARK_PRICE"},
        )
        speech = f"Take profit set at {price} for {symbol}."
        return CommandResult(
            ok=True, action="set_tp",
            detail=f"TP @ {price} for {symbol} ({size} contracts, {close_side})",
            speech=speech,
            order={"id": order.get("id"), "price": price, "symbol": symbol},
        )
    except BaseException as e:
        err_msg = _friendly_exchange_error(e)
        logger.error(f"[JARVIS] set_tp (ccxt) failed: {e}")
        return _err("set_tp", err_msg)


async def _set_sl_pct(symbol: str, pct: float, ex_name: Optional[str]) -> CommandResult:
    """Set stop-loss at a percentage loss.

    For LONG:  SL = entry × (1 - pct/100)  — price drops, stops out
    For SHORT: SL = entry × (1 + pct/100÷leverage) — price rises, stops out
    """
    connector, pos = await _find_position(symbol, ex_name)
    if pos is None:
        return _err("set_sl", f"No open position found for {symbol}")

    entry    = _safe_float(pos.get("entryPrice"))
    mark     = _safe_float(pos.get("markPrice")) or entry
    side     = str(pos.get("side") or "long").lower()
    leverage = _safe_float(pos.get("leverage")) or 10.0

    if entry <= 0:
        return _err("set_sl", f"Cannot determine entry price for {symbol}")

    if side == "short":
        price_rise_pct = pct / leverage
        sl_price = round(mark * (1 + price_rise_pct / 100), 8)  # SL above mark for short
    else:
        price_drop_pct = pct / leverage
        sl_price = round(mark * (1 - price_drop_pct / 100), 8)  # SL below mark for long
        if sl_price <= 0:
            sl_price = round(mark * 0.001, 8)

    return await _set_sl_price(symbol, sl_price, ex_name, _connector=connector, _pos=pos)


async def _set_sl_price(
    symbol: str,
    price: float,
    ex_name: Optional[str],
    *,
    _connector=None,
    _pos=None,
) -> CommandResult:
    """Place a stop-loss TPSL order at an absolute price.

    Uses connector.place_tpsl_order (mark_price, no extraneous side param).
    Validates the SL price direction before calling the exchange.
    """
    if _connector is None:
        _connector, _pos = await _find_position(symbol, ex_name)
    if _pos is None:
        return _err("set_sl", f"No open position found for {symbol}")

    raw_sym    = _pos.get("symbol", symbol)
    info       = _pos.get("info", {})
    side       = str(_pos.get("side") or "long").lower()
    size       = abs(_safe_float(_pos.get("contracts")))
    close_side = "sell" if side == "long" else "buy"
    mark       = _safe_float(_pos.get("markPrice")) or _safe_float(_pos.get("entryPrice")) or 0

    # Validate SL direction relative to current mark price
    if mark > 0:
        if side == "long" and price >= mark:
            msg = (f"SL price {price} must be BELOW current mark {mark:.5f} for a long. "
                   f"Try a price under {mark:.5f}.")
            return CommandResult(ok=False, action="set_sl", detail=msg, speech=msg)
        if side == "short" and price <= mark:
            msg = (f"SL price {price} must be ABOVE current mark {mark:.5f} for a short. "
                   f"Try a price over {mark:.5f}.")
            return CommandResult(ok=False, action="set_sl", detail=msg, speech=msg)

    # Hedge-mode detection
    hold_side_raw = info.get("holdSide") or _pos.get("holdSide")
    is_hedge      = bool(hold_side_raw)
    hold_side     = str(hold_side_raw or side).lower()
    plan_type     = "loss_plan" if is_hedge else "pos_loss"

    # ── Bitget connector.place_tpsl_order (proven, mark_price, no side param) ──
    if hasattr(_connector, "place_tpsl_order"):
        try:
            bsym   = _bitget_sym(raw_sym)
            result = await _connector.place_tpsl_order(
                symbol        = bsym,
                margin_coin   = "USDT",
                plan_type     = plan_type,
                trigger_price = float(price),
                hold_side     = hold_side,
                size          = _fmt_size(size) if is_hedge else None,
            )
            oid    = result.get("orderId", "") or result.get("clientOid", "")
            speech = (
                f"Stop loss set at {price} for {symbol}. Order ID {oid}."
                if oid else f"Stop loss set at {price} for {symbol}."
            )
            return CommandResult(
                ok=True, action="set_sl",
                detail=f"SL @ {price} for {symbol} ({size} contracts, {close_side})",
                speech=speech,
                order={"id": oid, "price": price, "symbol": symbol},
            )
        except BaseException as e:
            err_msg = _friendly_exchange_error(e)
            logger.error(f"[JARVIS] set_sl (bitget connector) failed: {e}")
            return _err("set_sl", err_msg)

    # ── Generic ccxt fallback ──────────────────────────────────────────────────
    try:
        order = await _connector.exchange.create_order(
            symbol=raw_sym, type="STOP_MARKET", side=close_side, amount=size,
            params={"stopPrice": price, "reduceOnly": True, "workingType": "MARK_PRICE"},
        )
        speech = f"Stop loss set at {price} for {symbol}."
        return CommandResult(
            ok=True, action="set_sl",
            detail=f"SL @ {price} for {symbol} ({size} contracts, {close_side})",
            speech=speech,
            order={"id": order.get("id"), "price": price, "symbol": symbol},
        )
    except BaseException as e:
        err_msg = _friendly_exchange_error(e)
        logger.error(f"[JARVIS] set_sl (ccxt) failed: {e}")
        return _err("set_sl", err_msg)


async def _close_position(symbol: str, ex_name: Optional[str]) -> CommandResult:
    """Market-close an open position."""
    connector, pos = await _find_position(symbol, ex_name)
    if pos is None:
        return _err("close", f"No open position found for {symbol}")

    raw_sym    = pos.get("symbol", symbol)
    side       = str(pos.get("side") or "long").lower()
    size       = abs(_safe_float(pos.get("contracts")))
    close_side = "sell" if side == "long" else "buy"
    pnl        = _safe_float(pos.get("unrealizedPnl"))

    # ── Bitget native client ──────────────────────────────────────────────────
    # For one-way mode, the native API's `tradeSide:'close'` parameter conflicts
    # with unilateral position handling.  Use ccxt directly — it has built-in
    # Bitget swap handling (position mode + reduceOnly resolution).
    # Skip native client for close and go straight to ccxt.

    # ── ccxt close (handles one-way + hedge mode automatically) ──────────────
    try:
        order = await connector.exchange.create_order(
            symbol=raw_sym,
            type="market",
            side=close_side,
            amount=size,
            params={
                "reduceOnly": True,
                "positionSide": "one_way" if not pos.get("holdSide") else side.upper(),
            },
        )
        sign   = "profit" if pnl >= 0 else "loss"
        speech = (
            f"{symbol} position closed. "
            f"{sign.capitalize()} of {abs(pnl):.2f} USDT."
        )
        return CommandResult(
            ok=True, action="close",
            detail=f"Closed {symbol} {size} @ market | PnL {pnl:+.2f} USDT",
            speech=speech,
            order={"id": order.get("id"), "symbol": symbol},
        )
    except BaseException as e:
        err_msg = _friendly_exchange_error(e)
        logger.error(f"[JARVIS] close (ccxt) failed: {e}")
        return _err("close", err_msg)


async def _list_positions() -> CommandResult:
    positions = await get_all_positions()
    if not positions:
        return CommandResult(
            ok=True, action="list_positions",
            detail="No open positions.",
            speech="You have no open positions, Sir.",
        )
    lines = []
    for p in positions:
        sign = "up" if p.pnl >= 0 else "down"
        lines.append(
            f"{p.symbol} {p.side.upper()} | "
            f"entry {p.entry_price} → mark {p.mark_price} | "
            f"PnL {p.pnl:+.2f} USDT ({p.pnl_pct:+.2f}%)"
        )
    speech_parts = [f"{p.symbol} is {('up' if p.pnl>=0 else 'down')} {abs(p.pnl_pct):.1f}%" for p in positions]
    speech = f"You have {len(positions)} open position{'s' if len(positions)>1 else ''}. " + ", ".join(speech_parts) + "."
    return CommandResult(
        ok=True, action="list_positions",
        detail="\n".join(lines),
        speech=speech,
    )


async def _analyze_positions_with_news(cmd: str) -> CommandResult:
    """
    Fetch every open position + recent news articles, match headlines to each
    position by token symbol, then call the AI router for a real qualitative
    impact analysis.  Falls back to a structured table if AI is unavailable.

    Called when the user says things like:
      "analyse current positions"
      "with coming news analyse my positions"
      "how will today's news impact my open positions"
    """
    # 1. Fetch all open positions ───────────────────────────────────────────
    positions = await get_all_positions()
    if not positions:
        msg = "You have no open positions, Sir. Nothing to analyse against the news."
        return CommandResult(ok=True, action="news_position_analysis", detail=msg, speech=msg)

    # 2. Fetch recent news articles from the DB (each article already has
    #    a pre-parsed 'symbols' list, 'sentiment_score', 'sentiment_label')
    articles: List[Dict[str, Any]] = []
    try:
        from app.core.database import AsyncSessionLocal
        from app.sentiment.enhanced_service import EnhancedSentimentService
        async with AsyncSessionLocal() as db:
            articles = await EnhancedSentimentService.get_articles(db, hours=24, limit=50)
    except Exception as e:
        logger.warning(f"[JARVIS] news fetch for position analysis failed: {e}")

    # 3. Match articles to each position by base-token symbol ───────────────
    #    An article's 'symbols' field is a list like ["BTC", "ETH", "PEPE"].
    position_bases: Dict[str, str] = {}   # base → full symbol  e.g. "UNI" → "UNIUSDT"
    for p in positions:
        base = p.symbol.replace("USDT", "").replace("USDC", "").replace("/", "")
        position_bases[base.upper()] = p.symbol

    pos_articles: Dict[str, List[Dict]] = {b: [] for b in position_bases}
    general_articles: List[Dict] = []

    for art in articles:
        syms_raw: List[str] = art.get("symbols") or []
        syms_up = [s.upper() for s in syms_raw]
        matched_bases = [b for b in position_bases if b in syms_up]
        if matched_bases:
            for b in matched_bases:
                pos_articles[b].append(art)
        else:
            general_articles.append(art)

    # 4. Build a compact prompt for the AI ──────────────────────────────────
    position_prompt_lines: List[str] = []
    for p in positions:
        base = p.symbol.replace("USDT", "").replace("USDC", "").replace("/", "").upper()
        pnl_arrow = "▲" if p.pnl >= 0 else "▼"
        line = (
            f"- {base} {p.side.upper()} | "
            f"entry ${p.entry_price:.6g} → mark ${p.mark_price:.6g} | "
            f"PnL {pnl_arrow} {abs(p.pnl):.2f} USDT ({p.pnl_pct:+.2f}%)"
        )
        arts = pos_articles.get(base, [])[:3]
        for a in arts:
            score = a.get("sentiment_score")
            label = (
                a.get("sentiment_label")
                or ("BULLISH" if (score or 0) > 0.1 else "BEARISH" if (score or 0) < -0.1 else "NEUTRAL")
            )
            line += f"\n  [{label}] {(a.get('title') or '')[:120]}"
        if not arts:
            line += "\n  (no specific headlines today)"
        position_prompt_lines.append(line)

    general_headlines_text = ""
    if general_articles:
        general_headlines_text = "\n\nGeneral market headlines:\n" + "\n".join(
            f"- {(a.get('title') or '')[:120]}" for a in general_articles[:6]
        )

    total_arts = len(articles)
    prompt_body = (
        "My open trading positions with today's matching news:\n"
        + "\n".join(position_prompt_lines)
        + general_headlines_text
        + f"\n\n({total_arts} total headlines from the last 24 hours)\n\n"
        "Task: For EACH position, write one clear sentence explaining how today's news "
        "may help or hurt that trade.  For positions with no specific news, briefly "
        "note if the general headlines are bullish or bearish for the overall market. "
        "End with a 1-sentence overall portfolio risk note.  Be direct and concise."
    )

    # 5. Ask the AI router for a real qualitative analysis ──────────────────
    ai_detail: Optional[str] = None
    try:
        from app.core.database import AsyncSessionLocal
        from plugins.AiMarketAnalyst.backend.services.ai_router import db_chat

        async with AsyncSessionLocal() as db:
            resp = await db_chat(
                db,
                [
                    {
                        "role": "system",
                        "content": (
                            "You are JARVIS, a sharp trading assistant. "
                            "Give factual, direct analysis — no filler phrases."
                        ),
                    },
                    {"role": "user", "content": prompt_body},
                ],
                max_tokens=700,
                temperature=0.25,
                agent_name="jarvis-news-position-analysis",
                source="jarvis",
            )
            if resp.get("ok") and resp.get("content"):
                ai_detail = str(resp["content"]).strip()
    except Exception as e:
        logger.warning(f"[JARVIS] AI analysis for news/positions failed: {e}")

    # 6. Return AI response when available ──────────────────────────────────
    if ai_detail:
        n_pos = len(positions)
        speech = (
            f"News impact analysis for your {n_pos} open position{'s' if n_pos > 1 else ''}, Sir. "
            + ai_detail[:480].replace("\n", " ")
        )
        return CommandResult(
            ok=True, action="news_position_analysis",
            detail=ai_detail,
            speech=speech,
        )

    # 7. Structured fallback (AI unavailable) ───────────────────────────────
    detail_parts: List[str] = []
    for p in positions:
        base = p.symbol.replace("USDT", "").replace("USDC", "").replace("/", "").upper()
        arts = pos_articles.get(base, [])
        pnl_arrow = "▲" if p.pnl >= 0 else "▼"
        line = (
            f"{p.symbol} {p.side.upper()} | "
            f"entry {p.entry_price:.6g} → mark {p.mark_price:.6g} | "
            f"PnL {pnl_arrow} {abs(p.pnl):.2f} USDT ({p.pnl_pct:+.2f}%)"
        )
        if arts:
            for a in arts[:2]:
                score = a.get("sentiment_score")
                label = (
                    a.get("sentiment_label")
                    or ("BULLISH" if (score or 0) > 0.1 else "BEARISH" if (score or 0) < -0.1 else "NEUTRAL")
                )
                line += f"\n  [{label}] {(a.get('title') or '')[:100]}"
        else:
            line += "\n  No specific news today"
        detail_parts.append(line)

    if general_articles:
        detail_parts.append(
            "\nGeneral market headlines:\n"
            + "\n".join(f"  • {(a.get('title') or '')[:100]}" for a in general_articles[:5])
        )

    detail = (
        f"News Impact — {len(positions)} positions · {total_arts} headlines (last 24 h)\n\n"
        + "\n\n".join(detail_parts)
    )
    speech = (
        f"I matched {total_arts} headlines against your {len(positions)} positions. "
        "AI analysis is unavailable — check the details panel for the headline breakdown."
    )
    return CommandResult(ok=True, action="news_position_analysis", detail=detail, speech=speech)


async def _position_status(symbol: str, ex_name: Optional[str]) -> CommandResult:
    # Resolve the token → real coin name (so JARVIS says "Bitcoin", not "BTC").
    coin_name = symbol
    try:
        from app.services import pair_catalog
        rp = await pair_catalog.resolve(symbol)
        if rp is not None:
            coin_name = rp.name or rp.base
    except Exception:
        pass

    try:
        connector, pos = await _find_position(symbol, ex_name)
    except BaseException as e:
        friendly = "Exchange connection failed — please check your network."
        return CommandResult(ok=True, action="position_status", detail=friendly, speech=friendly)

    if pos is None:
        # No open position — give a live market update instead of a dead-end,
        # using the catalog's cached market cap / volume / price snapshot.
        try:
            from app.services import pair_catalog
            snap = await pair_catalog.get_market_snapshot(symbol)
        except Exception:
            snap = None
        if snap and snap.get("price") is not None:
            chg = snap.get("price_change_24h")
            cap = snap.get("market_cap")
            vol = snap.get("volume_24h")
            dir_txt = ""
            if chg is not None:
                dir_txt = f" {'up' if chg >= 0 else 'down'} {abs(chg):.2f} percent over 24 hours"
            speech = (
                f"{coin_name} is trading at {snap['price']:.6g}{dir_txt}. "
                + (f"Market cap {_fmt_usd_short(cap)}. " if cap else "")
                + (f"24 hour volume {_fmt_usd_short(vol)}. " if vol else "")
                + "You have no open position on it, Sir."
            )
            detail = (
                f"{coin_name} ({snap.get('symbol', symbol)}) | price {snap['price']:.6g}"
                + (f" | 24h {chg:+.2f}%" if chg is not None else "")
                + (f" | mcap {_fmt_usd_short(cap)}" if cap else "")
                + (f" | vol {_fmt_usd_short(vol)}" if vol else "")
                + " | no open position"
            )
            return CommandResult(ok=True, action="position_status", detail=detail, speech=speech)
        return CommandResult(
            ok=True, action="position_status",
            detail=f"No open position found for {coin_name}",
            speech=f"You have no open position on {coin_name}, Sir.",
        )

    entry   = _safe_float(pos.get("entryPrice"))
    mark    = _safe_float(pos.get("markPrice")) or entry
    pnl     = _safe_float(pos.get("unrealizedPnl"))
    pnl_pct = _safe_float(pos.get("percentage"))
    side    = str(pos.get("side") or "long")
    direction = "up" if pnl >= 0 else "down"
    speech = (
        f"{coin_name} {side} position is {direction} {abs(pnl_pct):.2f} percent. "
        f"PnL {'plus' if pnl>=0 else 'minus'} {abs(pnl):.2f} USDT. "
        f"Entry {entry:.6g}, current {mark:.6g}."
    )
    return CommandResult(
        ok=True, action="position_status",
        detail=f"{coin_name} ({symbol}) {side} | entry {entry} | mark {mark} | PnL {pnl:+.2f} USDT ({pnl_pct:+.2f}%)",
        speech=speech,
    )


def _fmt_usd_short(v: Optional[float]) -> str:
    """Human-readable short USD, e.g. 1.17T, 42.7B, 903M, 12.3K."""
    try:
        n = float(v or 0)
    except Exception:
        return "$0"
    a = abs(n)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"${n / div:.2f}{suf}"
    return f"${n:.0f}"
