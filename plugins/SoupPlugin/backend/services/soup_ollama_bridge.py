"""Soup → Ollama bridge: build training data from Brain + trades, trigger Soup fine-tune → GGUF → ollama create."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

ROOT = Path(__file__).resolve().parents[4]

# ── Paths ─────────────────────────────────────────────────────────────────
OllamaBridgeDataDir = (ROOT / "data" / "ollama_training").resolve()
OllamaBridgeDataDir.mkdir(parents=True, exist_ok=True)

DEFAULT_MIN_SAMPLES = int(os.getenv("OLLAMA_MIN_SAMPLES_FOR_TRAIN", "200"))
OLLAMA_PORT = int(os.getenv("OLLAMA_PORT", "11434"))
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "127.0.0.1")
OLLAMA_URL = os.getenv("OLLAMA_URL", f"http://{OLLAMA_HOST}:{OLLAMA_PORT}").rstrip("/")

TRADEBOT_LOCAL_MODEL = "tradebot-local:latest"
TRADEBOT_LOCAL_MODEL_ALT = "tradebot-local"


def _read_env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


# ── Dataset builders ───────────────────────────────────────────────────────
def _sharegpt_row(system: str, user: str, assistant: str) -> Dict[str, Any]:
    return {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
            {"role": "assistant", "content": assistant},
        ]
    }


#: Soup tokenizes system+user+assistant together and right-truncates at
#: data.max_length. If system+user already fills the budget, the assistant
#: target is wiped and soup aborts ("no causal-loss target remains"). Content
#: is mixed: numeric OHLCV CSV and CJK coin names tokenize at ~1 token/char,
#: so we size char caps assuming the WORST case (1 char ≈ 1 token) to guarantee
#: the assistant always survives regardless of the row's content.
_APPROX_CHARS_PER_TOKEN = 1.0
_ASST_TOKEN_RESERVE = 256   # tokens always kept for the target
_SYS_TOKEN_RESERVE = 80     # tokens for the system message + chat-template glue


def _char_budgets() -> Tuple[int, int]:
    """(max_user_chars, max_asst_chars) derived from SOUP_MAX_LENGTH."""
    max_len = int(os.getenv("SOUP_MAX_LENGTH", "2048"))
    asst_tokens = min(_ASST_TOKEN_RESERVE, max(96, max_len // 3))
    user_tokens = max(128, max_len - asst_tokens - _SYS_TOKEN_RESERVE)
    return int(user_tokens * _APPROX_CHARS_PER_TOKEN), int(asst_tokens * _APPROX_CHARS_PER_TOKEN)


def _fit_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Bound a ShareGPT row so tokenization never leaves an empty target.

    Returns the (possibly truncated) row, or None if it has no usable target.
    Over-long prompts keep the *most recent* context (tail) since that's what
    matters for OHLCV forecasting and recent-news reasoning; the assistant is
    truncated from the *head* but never below a usable minimum.
    """
    msgs = row.get("messages") or []
    if len(msgs) < 3:
        return None
    asst = str(msgs[2].get("content") or "").strip()
    if len(asst) < 5:  # empty/near-empty target → would fail causal-loss
        return None
    max_user_chars, max_asst_chars = _char_budgets()
    usr = str(msgs[1].get("content") or "")
    if len(usr) > max_user_chars:
        msgs[1]["content"] = usr[-max_user_chars:]
    if len(asst) > max_asst_chars:
        msgs[2]["content"] = asst[:max_asst_chars]
    return row


def _brain_rows(limit: int = 500) -> List[Dict[str, Any]]:
    """Knowledge + episodic traces from Neuro/Hermes/flat files."""
    rows: List[Dict[str, Any]] = []
    # 1) Federated Brain episodes — best-effort import, never fail the whole export
    try:
        from app.neuro_brain.federated_search import federated_search  # type: ignore

        # Use a few generic recall prompts to pull diverse episodes
        for q in ("BTC trade decision", "risk management note", "market research"):
            try:
                import asyncio

                hits = asyncio.run(federated_search(q, limit=20))  # type: ignore
                for h in (hits or [])[:20]:
                    txt = (h.get("text") or h.get("content") or "")[:1800]
                    if len(txt) < 80:
                        continue
                    rows.append(
                        _sharegpt_row(
                            "You are TradeBot — concise trading assistant.",
                            f"Recall knowledge: {txt[:400]}",
                            txt[:900],
                        )
                    )
            except Exception:
                continue
            if len(rows) >= limit:
                break
    except Exception:
        pass

    # 2) Hermes episodes (SQLite FTS) — lightweight scan
    try:
        hermes_db = ROOT / "data" / "hermes_state.db"
        if hermes_db.exists():
            import sqlite3

            con = sqlite3.connect(str(hermes_db))
            cur = con.cursor()
            # table may be named episodes / memory; be tolerant
            for tbl in ("episodes", "memory", "hermes_state"):
                try:
                    cur.execute(f"SELECT content, summary FROM {tbl} LIMIT 200")
                    for content, summary in cur.fetchall():
                        txt = (summary or content or "")[:1200]
                        if len(txt) < 80:
                            continue
                        rows.append(
                            _sharegpt_row(
                                "You are a trading memory assistant.",
                                "Summarise this past market episode for future decisions.",
                                txt[:900],
                            )
                        )
                    break
                except Exception:
                    continue
            con.close()
    except Exception:
        pass

    # 3) News + pair catalog text
    try:
        from sqlalchemy import select

        # Lazily import DB; if not ready, skip
        pass
    except Exception:
        pass

    return rows[:limit]


async def _brain_rows_async(limit: int = 400) -> List[Dict[str, Any]]:
    """Knowledge + episodic traces from the federated Brain (properly awaited)."""
    rows: List[Dict[str, Any]] = []
    try:
        from app.neuro_brain.federated_search import federated_search  # type: ignore

        queries = (
            "BTC trade decision", "ETH setup", "risk management note", "market research",
            "smart money concept", "news impact", "sentiment shift", "liquidity sweep",
            "trend reversal", "breakout continuation", "macro DXY VIX", "trade outcome lesson",
        )
        for q in queries:
            if len(rows) >= limit:
                break
            try:
                hits = await federated_search(q, limit=25)
            except Exception:
                continue
            for h in (hits or []):
                txt = (h.get("text") or h.get("content") or h.get("summary") or "")[:1800]
                if len(txt) < 80:
                    continue
                rows.append(_sharegpt_row(
                    "You are TradeBot — recall and apply stored trading knowledge.",
                    f"What do we know about: {q}?",
                    txt[:1000],
                ))
    except Exception as exc:
        logger.debug(f"[ollama-bridge] brain rows skipped: {exc}")
    # Hermes episodic memory (SQLite) — additive
    try:
        rows.extend(_brain_rows(limit=max(50, limit // 4)))
    except Exception:
        pass
    return rows[:limit]


def _enum(x: Any) -> str:
    return str(getattr(x, "value", x) or "")


async def _trade_rows(limit: int = 600) -> List[Dict[str, Any]]:
    """Every stored data source → supervised ShareGPT pairs.

    Covers agent decisions, Jarvis journal, trades, signals, sentiment, news,
    and the rug-pull / pre-pump sniper tables — so training sees all app data.
    """
    rows: List[Dict[str, Any]] = []
    try:
        from app.core.database import AsyncSessionLocal
        from app.models.database import (  # type: ignore
            AgentDecision, Trade, JarvisAnalysisJournal, Signal, SentimentScore,
            NewsArticle, RugPullToken, PumpToken,
        )
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            # Agent decisions that have an outcome (settled)
            try:
                q = select(AgentDecision).where(AgentDecision.outcome.isnot(None)).order_by(AgentDecision.created_at.desc()).limit(limit)
                for d in (await db.execute(q)).scalars().all():
                    try:
                        md = json.loads(d.market_data or "{}")
                    except Exception:
                        md = {}
                    pnl = d.outcome_pnl
                    user = (
                        f"Symbol {d.symbol} — role {d.agent_role} — action {d.action} "
                        f"(confidence {d.confidence:.2f}) — context {json.dumps(md)[:900]}"
                    )
                    assistant = json.dumps({
                        "action": d.action,
                        "confidence": round(float(d.confidence or 0), 2),
                        "reasoning": (d.reasoning or "")[:800],
                        "outcome": d.outcome or "unknown",
                        "outcome_pnl": pnl,
                    })
                    rows.append(_sharegpt_row("You are a trading agent. Output JSON with action, confidence, reasoning, outcome.", user, assistant))
            except Exception:
                pass

            # Jarvis journal — closed trades with R
            try:
                q2 = select(JarvisAnalysisJournal).where(JarvisAnalysisJournal.outcome.isnot(None)).order_by(JarvisAnalysisJournal.created_at.desc()).limit(limit)
                for j in (await db.execute(q2)).scalars().all():
                    user = (
                        f"Jarvis proposal {j.symbol} {j.side} entry {j.entry} "
                        f"SL {j.stop_loss} TP {j.take_profit} — mfe {j.mfe} mae {j.mae} outcome {j.outcome}"
                    )
                    rows.append(_sharegpt_row(
                        "You are a trading execution assistant.",
                        user,
                        f"Entry {j.entry}, SL {j.stop_loss}, TP {j.take_profit} — outcome {j.outcome} R {j.outcome_r}",
                    ))
            except Exception:
                pass

            # Raw trades (open/closed) — market context
            try:
                q3 = select(Trade).order_by(Trade.created_at.desc()).limit(limit)
                for t in (await db.execute(q3)).scalars().all():
                    rows.append(_sharegpt_row(
                        "You are a trade logger.",
                        f"Trade {t.symbol} {t.side}/{t.order_type} qty {t.amount} price {t.price} status {t.status} pnl {t.pnl}",
                        f"Logged trade {t.symbol} {t.side} at {t.price} status {t.status}",
                    ))
            except Exception:
                pass

            # Every signal — teach the model the app's signal shape
            try:
                q4 = select(Signal).order_by(Signal.created_at.desc()).limit(limit)
                for s in (await db.execute(q4)).scalars().all():
                    ind = (s.indicators or s.raw_data or "")[:600]
                    user = (
                        f"Market context for {s.symbol} on {s.timeframe or '?'} at price {s.price} "
                        f"(source {_enum(s.source)}). Indicators: {ind}"
                    )
                    assistant = json.dumps({
                        "action": _enum(s.action),
                        "strength": round(float(s.strength or 0), 2),
                        "confidence": round(float(s.confidence or 0), 2),
                        "timeframe": s.timeframe,
                    })
                    rows.append(_sharegpt_row("You are TradeBot's signal engine. Output JSON with action, strength, confidence, timeframe.", user, assistant))
            except Exception:
                pass

            # Sentiment scores
            try:
                q5 = select(SentimentScore).order_by(SentimentScore.created_at.desc()).limit(limit)
                for sc in (await db.execute(q5)).scalars().all():
                    label = "bullish" if (sc.score or 0) > 0.15 else "bearish" if (sc.score or 0) < -0.15 else "neutral"
                    user = (
                        f"Assess sentiment for {sc.symbol}. news={sc.news_score} social={sc.social_score} "
                        f"technical={sc.technical_score} ({sc.sources_count} sources)."
                    )
                    rows.append(_sharegpt_row(
                        "You are TradeBot's sentiment analyst. Output the aggregate sentiment.",
                        user,
                        f"{label} — score {float(sc.score or 0):.2f}, magnitude {float(sc.magnitude or 0):.2f}.",
                    ))
            except Exception:
                pass

            # News feed — knowledge base
            try:
                q6 = select(NewsArticle).order_by(NewsArticle.fetched_at.desc()).limit(limit)
                for n in (await db.execute(q6)).scalars().all():
                    syms = ""
                    try:
                        syms = ", ".join(json.loads(n.symbols or "[]"))
                    except Exception:
                        syms = n.symbols or ""
                    user = f"News [{n.source}/{n.category}]: {n.title}. {(n.summary or '')[:600]}"
                    assistant = (
                        f"Impact: {n.sentiment_label or 'neutral'} "
                        f"(score {float(n.sentiment_score or 0):.2f}) for {syms or 'the market'}. "
                        f"Reliability {float(n.reliability or 0):.2f}."
                    )
                    rows.append(_sharegpt_row("You are TradeBot's market news analyst.", user, assistant))
            except Exception:
                pass

            # Rug-pull sniper
            try:
                q7 = select(RugPullToken).order_by(RugPullToken.detected_at.desc()).limit(limit)
                for rp in (await db.execute(q7)).scalars().all():
                    user = (
                        f"{rp.symbol} pumped {rp.price_change_24h:.0f}% to {rp.price_at_detection}. "
                        f"mcap {rp.market_cap} vol {rp.volume_24h} rank {rp.market_cap_rank}. Analyse rug-pull risk."
                    )
                    assistant = (
                        f"Risk {rp.risk_score}. Short entry {rp.recommended_entry}, SL {rp.recommended_sl}, "
                        f"TP {rp.recommended_tp}. {(rp.ai_analysis or '')[:400]}"
                    )
                    rows.append(_sharegpt_row("You are TradeBot's rug-pull sniper.", user, assistant))
            except Exception:
                pass

            # Pre-pump detector
            try:
                q8 = select(PumpToken).order_by(PumpToken.id.desc()).limit(limit)
                for pt in (await db.execute(q8)).scalars().all():
                    user = (
                        f"{pt.symbol}: 1h {pt.price_change_1h}% 24h {pt.price_change_24h}% "
                        f"vol-spike {pt.volume_change_pct}%. scores volume={pt.volume_spike_score} "
                        f"accel={pt.price_accel_score} social={pt.social_score} momentum={pt.momentum_score}."
                    )
                    rows.append(_sharegpt_row(
                        "You are TradeBot's pre-pump detector. Judge if a pump is building.",
                        user,
                        f"{pt.symbol}: status {_enum(pt.status)}, BTC-relative {pt.btc_relative_score}, breakout {pt.ath_breakout_score}.",
                    ))
            except Exception:
                pass
    except Exception as exc:
        logger.debug(f"[ollama-bridge] db rows skipped: {exc}")
    # Each source is capped at `limit`; keep them all up to a generous total so
    # every data source is represented in training (override: OLLAMA_TRAIN_MAX_ROWS).
    total_cap = max(limit, int(os.getenv("OLLAMA_TRAIN_MAX_ROWS", "5000")))
    return rows[:total_cap]


def _ohlcv_rows(limit: int = 200) -> List[Dict[str, Any]]:
    """OHLCV windows → ShareGPT (reuses soup_ohlcv_exporter cascade where available)."""
    try:
        from plugins.SoupPlugin.backend.services.soup_ohlcv_exporter import export_ohlcv_to_jsonl  # type: ignore

        # The exporter returns a path; we read it and convert to rows
        # For schedule we just materialise a small preview dataset (BTC 1h)
        # to avoid hammering providers on every training run.
        return []
    except Exception:
        return []


import asyncio as _asyncio


async def _obsidian_rows(limit: int = 500) -> List[Dict[str, Any]]:
    """Obsidian vault markdown → training pairs (decisions, strategies, learning, insights)."""
    rows: List[Dict[str, Any]] = []
    vault = ROOT / "obsidian-vault"
    if not vault.exists():
        return rows
    # ordered by richness: learning posts have structured Q/A; decisions have outcomes
    folders = ["jarvis-learning", "decisions", "insights", "strategies", "_daily", "live-actions", "voice-memory"]
    for folder in folders:
        folder_path = vault / folder
        if not folder_path.exists():
            continue
        mds = sorted(folder_path.rglob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:200]
        for md in mds:
            if len(rows) >= limit:
                break
            try:
                text = md.read_text(errors="ignore")
                # strip YAML frontmatter
                if text.startswith("---"):
                    end = text.find("\n---", 4)
                    text = (text[end + 4:] if end > 0 else text).strip()
                if len(text) < 80:
                    continue
                title = md.stem.replace("-", " ").replace("_", " ")
                # extract structured Q/A blocks (jarvis-learning pattern)
                if "## Question" in text and "## Answer" in text:
                    q = text.split("## Answer")[0].split("## Question")[-1].strip()[:400]
                    a = text.split("## Answer")[-1].split("##")[0].strip()[:900]
                    if q and a:
                        rows.append(_sharegpt_row(
                            "You are TradeBot's learning memory. Answer from past experience.",
                            q, a,
                        ))
                        continue
                rows.append(_sharegpt_row(
                    "You are TradeBot — learn from past decisions and experiences.",
                    f"What do we know about: {title[:300]}?",
                    text[:900],
                ))
            except Exception:
                continue
    return rows[:limit]


def _interaction_buffer_rows(limit: int = 400) -> List[Dict[str, Any]]:
    """Real interactions recorded when the local LLM was used — drained into training."""
    buf = OllamaBridgeDataDir / "interaction_buffer.jsonl"
    if not buf.exists():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        with open(buf, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    # Archive the buffer so the same interactions aren't included twice
    try:
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        buf.rename(OllamaBridgeDataDir / f"interaction_buffer_{ts}.jsonl")
    except Exception:
        pass
    return rows[:limit]


async def _web_research_rows(limit: int = 50) -> List[Dict[str, Any]]:
    """Fetch latest crypto/macro news from public APIs for self-improvement training."""
    rows: List[Dict[str, Any]] = []
    import httpx
    sources = [
        # CryptoCompare public news (no key required)
        "https://min-api.cryptocompare.com/data/v2/news/?categories=BTC,ETH,Forex,Macro&lang=EN",
        # CoinDesk RSS as JSON via a public bridge (best-effort)
        "https://api.rss2json.com/v1/api.json?rss_url=https://www.coindesk.com/arc/outboundfeeds/rss/",
        # CoinTelegraph RSS
        "https://api.rss2json.com/v1/api.json?rss_url=https://cointelegraph.com/rss",
        # ForexLive / macro
        "https://api.rss2json.com/v1/api.json?rss_url=https://www.forexlive.com/feed/",
    ]
    for url in sources:
        if len(rows) >= limit:
            break
        try:
            async with httpx.AsyncClient(timeout=8.0) as c:
                r = await c.get(url, headers={"User-Agent": "TradeBot/self-improve"})
                if r.status_code != 200:
                    continue
                data = r.json()
                articles = data.get("Data") or data.get("items") or []
                for art in articles[:15]:
                    title = str(art.get("title") or "").strip()
                    body = str(art.get("body") or art.get("description") or "")[:600].strip()
                    if len(title) < 15:
                        continue
                    rows.append(_sharegpt_row(
                        "You are TradeBot's news analyst. Assess market impact.",
                        f"Analyse this news: {title}",
                        body or f"News: {title}. Assess the trading significance and likely market impact.",
                    ))
        except Exception:
            continue
    return rows[:limit]


async def _ohlcv_symbol_rows(
    symbols: List[str],
    timeframe: str = "1h",
    limit: int = 1500,
    max_windows_per_symbol: int = 300,
    lookback: int = 24,
    pred_len: int = 8,
) -> List[Dict[str, Any]]:
    """Per-symbol OHLCV → ShareGPT forecasting rows via the Kronos cascade.

    Best-effort per symbol: a failed fetch (e.g. provider down for an index)
    skips that symbol instead of failing the whole training run.
    lookback/pred_len are kept small so prompt+target fit under the training
    max_length (2048) — otherwise the assistant target gets truncated to nothing
    and soup aborts tokenization.
    """
    rows: List[Dict[str, Any]] = []
    try:
        from plugins.SoupPlugin.backend.services.soup_ohlcv_exporter import export_ohlcv_to_jsonl  # type: ignore
    except Exception:
        return rows
    for sym in symbols:
        try:
            out = OllamaBridgeDataDir / f"ohlcv_{sym.replace('/', '_')}_{timeframe}.jsonl"
            res = await export_ohlcv_to_jsonl(symbol=sym, timeframe=timeframe, limit=limit, output_path=out, lookback=lookback, pred_len=pred_len)
            p = Path(res.get("path") or out)
            if not p.exists():
                continue
            n = 0
            with p.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                        n += 1
                    except Exception:
                        continue
                    if n >= max_windows_per_symbol:
                        break
            logger.info(f"[ollama-bridge] OHLCV {sym} {timeframe}: +{n} rows")
        except Exception as exc:
            logger.warning(f"[ollama-bridge] OHLCV skip {sym}: {exc}")
            continue
    return rows


async def _pair_catalog_rows(limit: int = 5000) -> List[Dict[str, Any]]:
    """Teach the model about *every* tradeable pair on the app (crypto_pairs).

    OHLCV forecasting can only cover a bounded set of liquid symbols, but this
    gives the model name/symbol/rank/category awareness of all pairs so "train
    for all pairs on the app" is honoured cheaply from DB metadata.
    """
    rows: List[Dict[str, Any]] = []
    try:
        from app.core.database import AsyncSessionLocal  # type: ignore
        from app.models.database import CryptoPair  # type: ignore
        from sqlalchemy import select
    except Exception as exc:
        logger.debug(f"[ollama-bridge] pair-catalog import skipped: {exc}")
        return rows
    try:
        async with AsyncSessionLocal() as db:
            q = select(CryptoPair).where(CryptoPair.tradeable == True).order_by(  # noqa: E712
                CryptoPair.market_cap_rank.asc().nullslast()
            ).limit(limit)
            for p in (await db.execute(q)).scalars().all():
                name = p.name or p.base
                cats = ", ".join(p.categories or []) if isinstance(p.categories, list) else ""
                rank = f"rank #{p.market_cap_rank}" if p.market_cap_rank else "unranked"
                mc = f"${p.market_cap:,.0f} mcap" if p.market_cap else "mcap n/a"
                user = f"What is the {p.symbol} pair on TradeBot?"
                asst = (
                    f"{p.symbol} is the tradeable pair for {name} ({p.base}), quoted in {p.quote}. "
                    f"{rank}, {mc}."
                    + (f" Categories: {cats}." if cats else "")
                    + (f" {(p.description or '')[:240]}" if p.description else "")
                ).strip()
                rows.append(_sharegpt_row(
                    "You are TradeBot. You know every pair the app can trade on Bitget.",
                    user, asst,
                ))
        logger.info(f"[ollama-bridge] pair-catalog: +{len(rows)} pair-knowledge rows")
    except Exception as exc:
        logger.warning(f"[ollama-bridge] pair-catalog skipped: {exc}")
    return rows


#: Liquid symbols we actually export OHLCV forecasting windows for. Everything
#: else on the app is covered by _pair_catalog_rows (name/metadata knowledge).
#: Crypto majors use Bitget; the four macro symbols use the Yahoo/Swissquote
#: cascade in the OHLCV exporter.
LIQUID_OHLCV_SYMBOLS: List[str] = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT", "ADA/USDT",
    "DOGE/USDT", "AVAX/USDT", "LINK/USDT", "DOT/USDT", "TRX/USDT", "TON/USDT",
    "LTC/USDT", "MATIC/USDT", "NEAR/USDT", "ATOM/USDT", "UNI/USDT", "AAVE/USDT",
    "OP/USDT", "ARB/USDT", "SUI/USDT", "APT/USDT", "INJ/USDT", "SEI/USDT",
    "TIA/USDT", "FIL/USDT", "ETC/USDT", "XLM/USDT", "HBAR/USDT", "RUNE/USDT",
    # Macro (forex / metals / index) — cascade handles these
    "CADJPY", "XAUUSD", "US30", "XAGUSD",
]


def build_sharegpt_dataset(
    out_path: Optional[Path] = None,
    brain_limit: int = 300,
    trade_limit: int = 600,
    ohlcv_limit: int = 100,
    min_rows: int = 40,
) -> Tuple[Path, int]:
    """
    Synchronously build a ShareGPT JSONL file from Brain+trades.
    Returns (path, num_rows). Raises if below min_rows when checked by caller.
    """
    rows: List[Dict[str, Any]] = []
    # brain rows (sync)
    rows.extend(_brain_rows(limit=brain_limit))
    # trade rows (async, run in new loop if needed)
    try:
        trade_part = _asyncio.run(_trade_rows(limit=trade_limit))
        rows.extend(trade_part)
    except RuntimeError:
        # already inside an event loop — schedule but don't block
        try:
            loop = _asyncio.get_event_loop()
            if loop.is_running():
                # can't block — skip trade rows for this non-async caller; the
                # async variant build_sharegpt_dataset_async should be used instead
                pass
            else:
                rows.extend(loop.run_until_complete(_trade_rows(limit=trade_limit)))
        except Exception:
            pass
    except Exception:
        pass
    rows.extend(_ohlcv_rows(limit=ohlcv_limit))

    # dedup + trim
    seen = set()
    uniq: List[Dict[str, Any]] = []
    for r in rows:
        key = json.dumps(r, sort_keys=True)
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    rows = uniq

    # ensure at least fallback instructional rows so training never fails on empty DB
    if len(rows) < min_rows:
        fallback = [
            _sharegpt_row("You are TradeBot.", "What does a buy signal mean?", "A buy signal indicates the model expects price to rise based on indicators and context."),
            _sharegpt_row("You are TradeBot.", "How should risk be managed?", "Size positions by % of equity, use stop-loss at structure/ATR, cap exposure and leverage."),
            _sharegpt_row("You are TradeBot.", "Explain trade outcome tracking.", "Each decision is settled against live candles; realized PnL and outcome populate via the learning loop."),
        ]
        rows.extend(fallback)

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_path = out_path or (OllamaBridgeDataDir / f"tradebot-ollama-{ts}.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    logger.info(f"[ollama-bridge] wrote {len(rows)} ShareGPT rows → {out_path}")
    return out_path, len(rows)


async def build_sharegpt_dataset_async(
    out_path: Optional[Path] = None,
    brain_limit: int = 400,
    trade_limit: int = 800,
    symbols: Optional[List[str]] = None,
    ohlcv_timeframe: str = "1h",
    ohlcv_limit: int = 1500,
    ohlcv_windows_per_symbol: int = 300,
    ohlcv_lookback: int = 24,
    ohlcv_pred_len: int = 8,
    include_pair_catalog: bool = False,
    pair_catalog_limit: int = 5000,
) -> Tuple[Path, int]:
    rows: List[Dict[str, Any]] = []
    # Brain knowledge (awaited) + every stored DB source
    rows.extend(await _brain_rows_async(limit=brain_limit))
    rows.extend(await _trade_rows(limit=trade_limit))
    # Obsidian vault: previous decisions, strategies, jarvis-learning, insights
    rows.extend(await _obsidian_rows(limit=500))
    # Interaction buffer: real usage of the local LLM since last training run
    rows.extend(_interaction_buffer_rows(limit=400))
    # Web self-improvement: latest crypto/macro news (best-effort) — nightly self-improve via internet
    rows.extend(await _web_research_rows(limit=50))
    # Every tradeable pair on the app as name/metadata knowledge (all 2000+)
    if include_pair_catalog:
        rows.extend(await _pair_catalog_rows(limit=pair_catalog_limit))
    # Per-symbol OHLCV forecasting windows (BTC, ETH, forex, metals, indices…)
    if symbols:
        rows.extend(await _ohlcv_symbol_rows(
            symbols, timeframe=ohlcv_timeframe, limit=ohlcv_limit,
            max_windows_per_symbol=ohlcv_windows_per_symbol,
            lookback=ohlcv_lookback, pred_len=ohlcv_pred_len,
        ))
    seen = set()
    uniq: List[Dict[str, Any]] = []
    for r in rows:
        r = _fit_row(r)          # bound prompt / drop empty-target rows
        if r is None:
            continue
        key = json.dumps(r, sort_keys=True)
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    if len(uniq) < 20:
        uniq.extend(
            [
                _sharegpt_row("You are TradeBot.", "What is a sell signal?", "A sell signal expects price to fall; size and risk must still be validated."),
            ]
        )
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_path = out_path or (OllamaBridgeDataDir / f"tradebot-ollama-{ts}.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for r in uniq:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return out_path, len(uniq)


# ── Training trigger → Ollama import ───────────────────────────────────────
def trigger_soup_finetune_and_import(
    dataset_path: Path,
    base_model: Optional[str] = None,
    task: str = "sft",
    epochs: int = 1,
) -> Dict[str, Any]:
    """
    Run `soup train` on dataset_path (ShareGPT JSONL), then export GGUF and
    `ollama create tradebot-local:latest`. Best-effort; returns status dict.
    Requires soup venv installed.
    """
    from plugins.SoupPlugin.backend.services.soup_utils import SOUP_BIN, SOUP_PY_BIN  # type: ignore

    soup_bin = str(SOUP_BIN) if SOUP_BIN.exists() else (shutil.which("soup") or "")
    if not soup_bin:
        return {"ok": False, "error": "soup binary not found — run setup_soup.sh"}

    rec = {}
    try:
        from plugins.SoupPlugin.backend.services.soup_utils import recommendation  # type: ignore

        rec = recommendation()
    except Exception:
        rec = {}

    # Default the fine-tune base to the HF id of the configured Ollama model so
    # the resulting tradebot-local matches what the app serves as fallback.
    if base_model:
        base = base_model
    else:
        try:
            from app.services.ollama_service import default_soup_base_hf  # type: ignore
            base = default_soup_base_hf()
        except Exception:
            base = os.getenv("SOUP_DEFAULT_BASE", "Qwen/Qwen2.5-0.5B-Instruct")
    quant = rec.get("quantization")  # "4bit"/None → soup training.quantization
    stream = bool(rec.get("stream_layers"))
    # Force a concrete small batch on Apple/16GB — soup's batch_size="auto" tunes
    # up until MPS OOMs at step 0. Env override wins.
    batch = os.getenv("SOUP_BATCH_SIZE") or rec.get("batch_size") or "auto"
    lora_r = int(rec.get("lora_r") or 16)
    # Activation memory scales with max_length; keep it just above the largest
    # row (24-candle OHLCV ≈ 1500 tok) so MPS fits. Env override wins.
    max_length = int(os.getenv("SOUP_MAX_LENGTH", "2048"))
    # Build soup config (schema mirrors the working manual path in soup_service.py).
    work = Path(os.getenv("SOUP_WORK_DIR", str(ROOT / "data" / "soup_jobs"))) / f"ollama-{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    work.mkdir(parents=True, exist_ok=True)
    output_dir = work / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = work / "soup.yaml"
    # build_sharegpt_dataset_async emits {"messages": [...]} → soup calls this "chatml".
    cfg: Dict[str, Any] = {
        "base": base,
        "task": task,
        "data": {"train": str(dataset_path), "format": "chatml", "max_length": max_length},
        "training": {
            "epochs": int(epochs),
            "lr": 2e-5,
            "batch_size": batch,
            "lora": {"r": lora_r, "alpha": 16},
        },
        "output": str(output_dir),
    }
    # GGUF export needs a HF-format model → force the transformers backend
    # (MLX weights don't convert cleanly). bitsandbytes 4bit is unsupported on
    # Apple MPS, and a small base fits in RAM without it, so only quantize where
    # CUDA + bitsandbytes is available. stream_layers is transformers-only.
    import platform as _plat
    is_apple = _plat.system().lower() == "darwin"
    cfg["backend"] = "transformers"
    if quant and not is_apple:
        cfg["training"]["quantization"] = quant
        if stream:
            cfg["training"]["stream_layers"] = True
    else:
        # bitsandbytes 4bit fails on Apple MPS; a small base trains fine full-precision.
        cfg["training"]["quantization"] = "none"
    try:
        import yaml as _yaml
        cfg_path.write_text(_yaml.safe_dump(cfg, sort_keys=False))
    except Exception:
        import json as _js
        cfg_path.write_text(_js.dumps(cfg, indent=2))

    # Run soup train (-y: skip the interactive 'Start training?' prompt — no TTY here)
    cmd = [soup_bin, "train", "--config", str(cfg_path), "--yes"]
    logger.info(f"[ollama-bridge] running: {' '.join(cmd)} (work {work})")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600 * 4, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "soup train timed out after 4h", "work_dir": str(work)}
    (work / "soup_train.log").write_text((r.stdout or "") + "\n" + (r.stderr or ""))
    if r.returncode != 0:
        err_txt = (r.stderr or r.stdout or "")
        # Causal-loss abort → retry with tighter max_length (20% reduction) once
        if "no causal-loss" in err_txt.lower() or "causal-loss" in err_txt.lower():
            try:
                new_max = max(1024, int(max_length * 0.8))
                logger.warning(f"[ollama-bridge] causal-loss abort, retrying with max_length {new_max}")
                cfg["data"]["max_length"] = new_max
                cfg_path.write_text(_yaml.safe_dump(cfg, sort_keys=False) if "_yaml" in locals() else json.dumps(cfg, indent=2))
                r2 = subprocess.run(cmd, capture_output=True, text=True, timeout=3600 * 4, stdin=subprocess.DEVNULL)
                (work / "soup_train_retry.log").write_text((r2.stdout or "") + "\n" + (r2.stderr or ""))
                if r2.returncode == 0:
                    r = r2
                else:
                    return {"ok": False, "error": f"soup train failed (retry): {(r2.stderr or r2.stdout or '')[:800]}", "work_dir": str(work), "logs": str(work / "soup_train.log")}
            except Exception as _e:
                return {"ok": False, "error": f"soup train failed: {err_txt[:800]} (retry error: {_e})", "work_dir": str(work), "logs": str(work / "soup_train.log")}
        else:
            return {"ok": False, "error": f"soup train failed: {err_txt[:800]}", "work_dir": str(work), "logs": str(work / "soup_train.log")}

    # Export the trained model → GGUF (soup export --model <dir> --format gguf --base <hf>)
    export_quant = os.getenv("OLLAMA_GGUF_QUANT", "q4_k_m")
    gguf_path = work / f"tradebot-local.{export_quant}.gguf"
    created = False
    export_err = ""
    gguf_export_cmd = [
        soup_bin, "export",
        "--model", str(output_dir),
        "--format", "gguf",
        "--quant", export_quant,
        "--output", str(gguf_path),
        "--base", base,
    ]
    try:
        er = subprocess.run(gguf_export_cmd, capture_output=True, text=True, timeout=3600, stdin=subprocess.DEVNULL)
        if er.returncode != 0:
            export_err = (er.stderr or er.stdout or "")[:800]
            (work / "soup_export.log").write_text(export_err)
    except subprocess.TimeoutExpired:
        export_err = "soup export timed out after 60m"
    except Exception as exc:
        export_err = str(exc)

    # soup may name the file <base>.<quant>.gguf — locate whatever gguf landed in work/
    if not gguf_path.exists():
        cands = sorted(work.glob("*.gguf"), key=lambda p: p.stat().st_mtime, reverse=True)
        if cands:
            gguf_path = cands[0]

    # Replace the served Ollama model with the freshly trained one (ollama create overwrites the tag).
    if gguf_path.exists():
        try:
            from app.services.ollama_service import ollama_create_from_gguf  # type: ignore
            created = bool(ollama_create_from_gguf(
                TRADEBOT_LOCAL_MODEL, gguf_path,
                modelfile_extra='SYSTEM "You are TradeBot local LLM — trading assistant."',
                timeout=600.0,
            ))
            if created:
                logger.info(f"[ollama-bridge] {TRADEBOT_LOCAL_MODEL} replaced with freshly trained GGUF ✓")
        except Exception as exc:
            export_err = f"ollama create failed: {exc}"
            logger.warning(f"[ollama-bridge] {export_err}")
    else:
        logger.warning(f"[ollama-bridge] GGUF export failed: {export_err} — trained model kept at {output_dir}")

    # Prune old datasets keeping last N
    try:
        keep_n = int(os.getenv("OLLAMA_KEEP_LAST_N", "2"))
        jsonls = sorted(OllamaBridgeDataDir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in jsonls[keep_n:]:
            old.unlink(missing_ok=True)
    except Exception:
        pass

    # ok reflects the user-visible goal: the Ollama model was actually replaced.
    result: Dict[str, Any] = {
        "ok": bool(created),
        "work_dir": str(work),
        "output_dir": str(output_dir),
        "gguf": str(gguf_path) if gguf_path.exists() else None,
        "ollama_created": created,
        "model": TRADEBOT_LOCAL_MODEL if created else None,
    }
    if not created:
        result["error"] = f"trained OK but GGUF export / ollama replace failed: {export_err or 'gguf not produced'}"
    return result


def should_train(now: Optional[datetime] = None, last_train_file: Optional[Path] = None) -> Tuple[bool, str]:
    """Gate: enough new samples and enough time since last train + nightly window."""
    now = now or datetime.utcnow()
    # Time gate: default 24h interval
    interval_h = int(os.getenv("OLLAMA_TRAIN_INTERVAL_HOURS", "24"))
    last = last_train_file or (OllamaBridgeDataDir / ".last_ollama_train")
    if last.exists():
        try:
            ts = datetime.fromisoformat(last.read_text().strip())
            if now - ts < timedelta(hours=interval_h):
                return False, f"last train {ts.isoformat()} < {interval_h}h ago"
        except Exception:
            pass
    # New-data gate: need at least OLLAMA_TRAIN_MIN_NEW_ROWS new interaction rows OR vault newer than last train
    min_new = int(os.getenv("OLLAMA_TRAIN_MIN_NEW_ROWS", "100"))
    if min_new > 0 and last.exists():
        try:
            ts = datetime.fromisoformat(last.read_text().strip())
            # count interaction buffer lines since last train
            buf = OllamaBridgeDataDir / "interaction_buffer.jsonl"
            new_rows = 0
            if buf.exists():
                # quick line count
                with open(buf, encoding="utf-8") as f:
                    for _ in f:
                        new_rows += 1
                # also count archived buffers newer than ts
                for arch in OllamaBridgeDataDir.glob("interaction_buffer_*.jsonl"):
                    try:
                        if datetime.utcfromtimestamp(arch.stat().st_mtime) > ts:
                            with open(arch, encoding="utf-8") as af:
                                for _ in af:
                                    new_rows += 1
                    except: pass
            # vault freshness: any md newer than last train counts as new data
            vault_new = False
            try:
                vault = ROOT / "obsidian-vault"
                if vault.exists():
                    for md in vault.rglob("*.md"):
                        try:
                            if datetime.utcfromtimestamp(md.stat().st_mtime) > ts:
                                vault_new = True
                                break
                        except: continue
            except: pass
            if new_rows < min_new and not vault_new:
                return False, f"only {new_rows} new interaction rows < {min_new} and no new vault data — skipping"
        except Exception:
            pass
    # Nightly window: 02:00-06:00 UTC (configurable via OLLAMA_TRAIN_WINDOW_START/END)
    window_start = int(os.getenv("OLLAMA_TRAIN_WINDOW_START", "2"))
    window_end = int(os.getenv("OLLAMA_TRAIN_WINDOW_END", "6"))
    if 0 <= window_start < 24 and 0 <= window_end < 24 and window_start != window_end:
        hour = now.hour
        # handle wrap-around (e.g. 22-06)
        if window_start < window_end:
            in_window = window_start <= hour < window_end
        else:
            in_window = hour >= window_start or hour < window_end
        # if not in window and training is not forced, defer
        force = os.getenv("TRADEBOT_FORCE_OLLAMA_TRAIN", "").strip().lower() in ("1","true","yes","on")
        if not in_window and not force:
            return False, f"outside nightly window {window_start:02d}:00-{window_end:02d}:00 UTC (now {hour:02d}:00) — deferred"
    return True, "due"


def mark_trained(path: Optional[Path] = None) -> None:
    p = path or (OllamaBridgeDataDir / ".last_ollama_train")
    try:
        p.write_text(datetime.utcnow().isoformat())
    except Exception:
        pass
