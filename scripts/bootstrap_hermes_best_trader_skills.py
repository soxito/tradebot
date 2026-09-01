#!/usr/bin/env python3
"""
Bootstrap Hermes Best-Trader skills — A+A (crypto + all FX, 1 skill per pair).

Creates 78 stock skills (14 crypto + 64 FX) under hermes_skills/{slug}-best-trader/
linked to all 7 specialists + JARVIS chair. Idempotent: existing stock skills are
kept (use --force to regenerate). Evolution (B) appends ## Learned (auto).
Surfaces in /hermes + orchestrator chair injection + JARVIS /skill chat.

Usage:
  python scripts/bootstrap_hermes_best_trader_skills.py        # bootstrap missing only
  python scripts/bootstrap_hermes_best_trader_skills.py --force
  python scripts/bootstrap_hermes_best_trader_skills.py --dry-run
  python scripts/bootstrap_hermes_best_trader_skills.py --check  # exit 1 if missing
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Dict, List

# ── Catalogue — mirrors frontend/src/constants/tradingPairs.ts:19 PAIR_GROUPS ──
PAIR_GROUPS: List[Dict] = [
    {"label": "Metals", "symbols": ["XAUUSD", "XAGUSD", "XPTUSD", "XPDUSD", "XAUEUR", "XAUGBP", "XAUAUD", "XAGEUR"]},
    {"label": "FX Majors", "symbols": ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"]},
    {"label": "FX Crosses — EUR", "symbols": ["EURGBP", "EURJPY", "EURCHF", "EURAUD", "EURCAD", "EURNZD"]},
    {"label": "FX Crosses — GBP", "symbols": ["GBPJPY", "GBPCHF", "GBPAUD", "GBPCAD", "GBPNZD"]},
    {"label": "FX Crosses — AUD / NZD / CAD / CHF", "symbols": ["AUDJPY", "AUDCHF", "AUDCAD", "AUDNZD", "NZDJPY", "NZDCHF", "NZDCAD", "CADJPY", "CADCHF", "CHFJPY"]},
    {"label": "FX Exotics", "symbols": ["USDZAR", "USDMXN", "USDTRY", "USDSEK", "USDNOK", "USDDKK", "USDPLN", "USDHUF", "USDCZK", "USDSGD", "USDHKD", "USDCNH", "USDTHB", "USDILS", "EURZAR", "EURTRY", "EURSEK", "EURNOK", "EURPLN", "EURHUF", "EURCZK", "GBPZAR", "GBPTRY", "GBPSEK", "GBPNOK", "GBPSGD", "AUDSGD", "CHFSGD", "NZDSGD", "ZARJPY", "SGDJPY", "TRYJPY", "MXNJPY", "NOKSEK", "NOKJPY", "SEKJPY"]},
    {"label": "Indices", "symbols": ["US30", "US500", "NAS100", "US2000", "GER40", "UK100", "FRA40", "EU50", "ESP35", "ITA40", "SUI20", "AUS200", "JPN225", "HK50", "CHINA50"]},
    {"label": "Energy & Softs", "symbols": ["USOIL", "UKOIL", "NGAS", "COCOA", "COFFEE", "SUGAR", "COTTON", "WHEAT"]},
    {"label": "Crypto", "symbols": ["BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "BNBUSD", "ADAUSD", "DOGEUSD", "AVAXUSD", "LTCUSD", "LINKUSD", "DOTUSD", "TRXUSD", "BCHUSD", "XLMUSD"]},
]

LINKED_AGENTS = ["market_analyst", "sentiment_analyst", "signal_generator", "risk_manager", "trade_executor", "position_reviewer", "strategy_optimizer"]

# Asset class mapping for detail
def asset_class_for(symbol: str, group_label: str) -> str:
    if group_label == "Crypto":
        return "crypto"
    if group_label == "FX Majors":
        return "forex-major"
    if group_label.startswith("FX Crosses"):
        return "forex-cross"
    if group_label == "FX Exotics":
        return "forex-exotic"
    return "forex"

GROUP_DESCRIPTIONS = {
    "crypto": "crypto — 24/7, BTC-cycle aware, high ATR, funding/whale sensitive",
    "forex-major": "forex major — London/NY session, DXY-correlated, low spread",
    "forex-cross": "forex cross — session overlap, carry/beta aware, medium spread",
    "forex-exotic": "forex exotic — widened spread, low liquidity, event-sensitive",
}

PLAYBOOK_SNIPPETS = {
    "crypto": """## Why this pair moves
BTC dominance, funding, on-chain flows and US session liquidity drive it. Check BTC 1064-day cycle bias (bull/bear) before sizing — late-bull = tighter stops.

## Sessions & volatility
24/7. Highest volatility NY open + BTC breakouts. ATR expansion >=1.30 = volatile regime — require pullback to level, not chase mid-range.

## Levels
Fib golden zone (0.5–0.618) + nearest order block / FVG + HTF demand/supply. Validate entry within 0.6×ATR of level, stop 0.8–2.5×ATR outside noise.""",
    "forex-major": """## Why this pair moves
DXY, rate divergence and risk-on/off drive it. EURUSD ↔ DXY inverse, USDJPY ↔ US yields. Check correlated open positions — same-direction correlated pairs count as one.

## Sessions & volatility
London (08:00–10:00 UTC) and NY open (13:00–15:00 UTC) carry the move. Spread typically <1 pip; ATR expansion >=1.25 on news = wait for retest.

## Levels
Broken level retest + fib + H1/H4 order block. EUR/GBP/JPY crosses validate DXY move; never chase mid-range between levels.""",
    "forex-cross": """## Why this pair moves
Rate spread + carry + risk beta vs majors. Crosses amplify DXY via both legs; check both USD pairs before calling direction.

## Sessions & volatility
Best at London/NY overlap. Spread 1–3 pips — use limit at level, not market. Volatile expansion = limit at EMA20/fib 0.5–0.618 pullback.

## Levels
Cross-specific OB/FVG + channel edges. Cross must align with both USD legs; conflict = hold.""",
    "forex-exotic": """## Why this pair moves
EM risk + rate carry + commodity link (ZAR/AUD, TRY/MXN). Gap risk overnight, widened spread 5–30 pips normal.

## Sessions & volatility
Thin liquidity outside London/NY — slippage high. Only trade at named level with limit; never market mid-range. ATR expansion >1.35 = wait.

## Levels
Daily/weekly OB + broken level retest. Stop must clear day's range; reduce size 50% vs majors due to spread VA.""",
}

RISK_SNIPPET = """## Risk & sizing (all pairs)
- Risk 1% equity per trade (desk default). Scale 0.65→1%, 0.80→3%, 0.90→5%.
- SL 0.8–2.5×ATR, entry within 0.6×ATR of level, R:R ≥1:1.5 to first TP.
- Max 3 correlated same-direction positions; daily drawdown >5% = stand down.
- Volatile expansion (ATR >=1.25× avg, ADX>30) mid-range between levels = HOLD with resting order at level — never chase (XAU 2026-08-28 wipeout shape).
- Every BUY/SELL needs invalidation price before entry. If you cannot name it, there is no trade."""

INVEST_SNIPPET = """## Chair & desk linking
This skill is owned by the full desk — chair **JARVIS (ceo, seat -1)** + 7 seats: Sakhile (market_analyst), Lerato (sentiment_analyst), Naledi (signal_generator), Thabo (risk_manager), Puso (trade_executor), Kabelo (position_reviewer), Zanele (strategy_optimizer). It is injected into every `orchestrator._gather_context` for this symbol and recalled via `hermes/search` (FTS5) + `GET /jarvis/skill?symbol=`. PaulChat `/skill SYMBOL` surfaces it verbatim.
"""

EVOLUTION_MARKER = "<!-- auto-learned:start -->"
EVOLUTION_END_MARKER = "<!-- auto-learned:end -->"

def slug_for(symbol: str) -> str:
    return f"{symbol.lower()}-best-trader"

def frontmatter(symbol: str, asset_class: str, group_label: str) -> str:
    desc = GROUP_DESCRIPTIONS.get(asset_class, asset_class)
    agents = ", ".join(LINKED_AGENTS)
    return f"""---
name: {slug_for(symbol)}
description: Best-trader playbook for {symbol} — {desc}
symbol: {symbol}
asset_class: {asset_class}
group: {group_label}
linked_agents: [{agents}]
jarvis_role: ceo
jarvis_name: JARVIS
version: 1
source: best-trader-bootstrap
---"""

def skill_body(symbol: str, asset_class: str, group_label: str) -> str:
    snippet = PLAYBOOK_SNIPPETS.get(asset_class, PLAYBOOK_SNIPPETS["forex-major"])
    fm = frontmatter(symbol, asset_class, group_label)
    desc = GROUP_DESCRIPTIONS.get(asset_class, asset_class)
    return f"""{fm}

# {symbol} — Best Trader ({group_label})

> Stock playbook — chair JARVIS + 7-seat desk. 1 skill per pair, long+short. Execution gated by `RoomSettings.execution_enabled` (paper trades always, live requires gate). Auto-harvested wins coexist as `{{{{symbol}}}}-{{{{action}}}}-{{{{ts}}}}` session skills.

**Symbol:** {symbol}
**Asset class:** {asset_class} — {desc}
**Linked agents:** {", ".join(LINKED_AGENTS)} (all 7 specialists)
**JARVIS chair:** ceo · JARVIS (seat -1, via AiMarketAnalyst pool, SOUL.md: JARVIS/Paul/SOX merged)
**Group:** {group_label}
**Source:** `frontend/src/constants/tradingPairs.ts:PAIR_GROUPS` → bootstrap A+A

{snippet}

{RISK_SNIPPET}

{INVEST_SNIPPET}

## Entry quality gate (non-negotiable)
Every BUY/SELL needs price AT a structural level (fib golden zone 0.5–0.618, order block, FVG, demand/supply base, channel edge, broken level retest). Mid-range between levels = right idea, wrong price → HOLD + resting limit at level. Volatile expansion inverts impulse: wait for pullback into EMA20/fib/OB or confirmed break+retest; market order mid-air = chase.

## When to apply
When {symbol} shows the same structure + momentum read (EMA stack, range position, ATR expansion, 60-bar drive/efficiency) and consensus re-forms (≥0.4 agreement or strong momentum). Forecast `kronos_forecast` agreeing with structure raises confidence — but only at a level.

## Execution gate
Paper executes always; live executes only when `RoomSettings.execution_enabled=true`. Never bypassed. FTS5 is recall-only — scoring stays on Postgres `AgentDecision`.

## Learned (auto)
{EVOLUTION_MARKER}
_No learned adjustments yet — will be appended after 12+ resolved decisions for {symbol} (self-improve loop, win-rate & avg PnL)._
{EVOLUTION_END_MARKER}
"""

def resolve_skills_dirs() -> List[Path]:
    """All hermes_skills roots we should ensure (dual write)."""
    root = Path(__file__).resolve().parents[1]
    candidates: List[Path] = []
    # project root
    candidates.append(root / "hermes_skills")
    # backend/hermes_skills
    candidates.append(root / "backend" / "hermes_skills")
    # HERMES_SKILLS_PATH env
    env_sp = os.getenv("HERMES_SKILLS_PATH", "").strip()
    if env_sp:
        candidates.append(Path(env_sp).expanduser())
    # DATA_DIR hermes_skills
    for k in ("DATA_DIR", "TRADEBOT_DATA_DIR"):
        v = os.getenv(k, "").strip()
        if v:
            candidates.append(Path(v).expanduser() / "hermes_skills")
    # dedupe preserve order
    seen = set()
    out: List[Path] = []
    for p in candidates:
        rp = p.resolve() if p.exists() else p
        if str(rp) not in seen:
            seen.add(str(rp))
            out.append(p)
    return out

def desired_skills() -> List[Dict]:
    """A+A scope: Crypto + all FX groups, 1 per symbol."""
    wanted: List[Dict] = []
    for g in PAIR_GROUPS:
        label = g["label"]
        is_crypto = label == "Crypto"
        is_fx = label.startswith("FX")
        if not (is_crypto or is_fx):
            continue
        ac_default = asset_class_for("", label)
        for sym in g["symbols"]:
            ac = asset_class_for(sym, label)
            wanted.append({"symbol": sym, "group": label, "asset_class": ac, "slug": slug_for(sym)})
    # dedupe by symbol (keep first)
    seen = set()
    uniq: List[Dict] = []
    for w in wanted:
        if w["symbol"] not in seen:
            seen.add(w["symbol"])
            uniq.append(w)
    return sorted(uniq, key=lambda x: (x["asset_class"], x["symbol"]))

def write_skill(base: Path, entry: Dict, force: bool = False) -> str:
    """Write one skill under base/slug/. Returns created|kept|overwritten|skipped."""
    slug = entry["slug"]
    skill_dir = base / slug
    skill_md = skill_dir / "SKILL.md"
    meta_json = skill_dir / "metadata.json"
    body = skill_body(entry["symbol"], entry["asset_class"], entry["group"])
    meta = {
        "symbol": entry["symbol"],
        "asset_class": entry["asset_class"],
        "group": entry["group"],
        "slug": slug,
        "linked_agents": LINKED_AGENTS,
        "jarvis": {"role": "ceo", "human_name": "JARVIS", "seat": -1, "model": "via AiMarketAnalyst pool"},
        "version": 1,
        "source": "best-trader-bootstrap",
        "created_at": time.time(),
        "updated_at": time.time(),
        "evolved_at": None,
        "decisions_reviewed": 0,
        "win_rate": None,
        "avg_pnl": None,
        "path": str(skill_dir),
    }
    if skill_dir.exists() and skill_md.exists():
        existing = skill_md.read_text(encoding="utf-8", errors="ignore")
        # keep user evolutions if already has Learned block with real content
        is_stock = "best-trader-bootstrap" in existing or entry["slug"] in existing
        if not force and is_stock and EVOLUTION_MARKER in existing and "No learned adjustments yet" not in existing:
            # has real learned evolution — preserve Learned block
            # still ensure metadata exists
            if not meta_json.exists():
                meta_json.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            return "kept-evolved"
        if not force:
            return "kept"
        # force: preserve Learned block if it exists and is not placeholder
        learned_block = None
        if EVOLUTION_MARKER in existing:
            try:
                learned_block = existing.split(EVOLUTION_MARKER)[1].split(EVOLUTION_END_MARKER)[0]
                if learned_block and "No learned adjustments yet" not in learned_block:
                    # splice preserved learned into new body
                    body = body.replace(
                        f"{EVOLUTION_MARKER}\n_No learned adjustments yet — will be appended after 12+ resolved decisions for {entry['symbol']} (self-improve loop, win-rate & avg PnL)._",
                        f"{EVOLUTION_MARKER}{learned_block}",
                    )
            except Exception:
                pass
        skill_md.write_text(body, encoding="utf-8")
        # preserve created_at if meta exists
        if meta_json.exists():
            try:
                old = json.loads(meta_json.read_text(encoding="utf-8"))
                meta["created_at"] = old.get("created_at", meta["created_at"])
                # preserve evolved stats
                for k in ("evolved_at", "decisions_reviewed", "win_rate", "avg_pnl"):
                    if old.get(k) is not None:
                        meta[k] = old[k]
            except Exception:
                pass
        meta["updated_at"] = time.time()
        meta_json.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return "overwritten"
    # create
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md.write_text(body, encoding="utf-8")
    meta_json.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return "created"

def main() -> int:
    ap = argparse.ArgumentParser(description="Bootstrap Hermes Best-Trader skills (A+A)")
    ap.add_argument("--force", action="store_true", help="regenerate existing stock skills (preserves Learned)")
    ap.add_argument("--dry-run", action="store_true", help="print plan only, write nothing")
    ap.add_argument("--check", action="store_true", help="exit 1 if any desired skill missing")
    args = ap.parse_args()

    desired = desired_skills()
    dirs = resolve_skills_dirs()
    primary = dirs[0]  # project root hermes_skills

    # Summary
    print(f"[bootstrap] A+A — crypto + all FX: {len(desired)} skills desired")
    for g in PAIR_GROUPS:
        if g["label"] == "Crypto" or g["label"].startswith("FX"):
            print(f"  {g['label']}: {len(g['symbols'])}")
    print(f"[bootstrap] Primary dir: {primary} (dry_run={args.dry_run}, force={args.force})")
    print(f"[bootstrap] All target dirs: {', '.join(str(d) for d in dirs)}")

    if args.check:
        missing = []
        for e in desired:
            if not (primary / e["slug"] / "SKILL.md").exists():
                missing.append(e["symbol"])
        if missing:
            print(f"[check] MISSING {len(missing)}: {', '.join(missing[:20])}{' ...' if len(missing)>20 else ''}")
            return 1
        print(f"[check] all {len(desired)} present ✓")
        return 0

    if args.dry_run:
        for e in desired:
            exists = (primary / e["slug"] / "SKILL.md").exists()
            print(f"  {'keep' if exists else 'create'} {e['slug']} ({e['symbol']} {e['asset_class']})")
        return 0

    totals: Dict[str, int] = {}
    for base in dirs:
        try:
            base.mkdir(parents=True, exist_ok=True)
        except Exception as ex:
            print(f"[warn] mkdir {base} failed: {ex}")
            continue
        for e in desired:
            try:
                res = write_skill(base, e, force=args.force)
                totals[res] = totals.get(res, 0) + 1
            except Exception as ex:
                print(f"[error] {e['symbol']} @ {base}: {ex}")
                totals["error"] = totals.get("error", 0) + 1

    # Deduplicate totals display (we wrote to N dirs, so divide by N for unique perspective)
    n_dirs = len([d for d in dirs if d.exists()])
    print(f"[bootstrap] done across {n_dirs} dirs: {totals} (per-dir counts)")
    # Per-symbol unique summary from primary
    created = kept = evolved = 0
    for e in desired:
        p = primary / e["slug"] / "SKILL.md"
        if not p.exists():
            continue
        txt = p.read_text(encoding="utf-8", errors="ignore")
        if EVOLUTION_MARKER in txt and "No learned adjustments yet" not in txt:
            evolved += 1
        else:
            kept += 1
    print(f"[bootstrap] unique in {primary}: {len(desired)} desired, {kept} stock, {evolved} evolved")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
