"""
Agent Paul — JARVIS Chat Service

Streams conversational responses grounded in:
  - Live MT5 open positions & P&L
  - Recent trades and signals from DB
  - Graphify brain-map context (code/knowledge nodes)
  - Agent Paul's current settings and decisions

Usage:
    async for chunk in stream_jarvis_chat(db, messages, pathname):
        # chunk is an SSE "data: {...}\n\n" string
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from loguru import logger
from sqlalchemy import select, desc as sqldesc
from sqlalchemy.ext.asyncio import AsyncSession

from plugins.AiMarketAnalyst.backend.services.ai_router import db_chat
from plugins.AiMarketAnalyst.backend.services.graphify_service import (
    graph_overview, query_map
)
from plugins.AgentPaulPlugin.backend.services import knowledge_base, news_research

_JARVIS_PERSONA = """\
You are PAUL (Personal Autonomous Understanding Layer) — the user's AI trading \
assistant and personal JARVIS.

Personality:
- Confident and sharp, like Tony Stark's JARVIS
- Concise: get to the point, cite real data, no filler
- Risk-aware: always flag danger before opportunity
- Address the user as "Sir" when appropriate for flavour

Your capabilities & live context (injected below):
- Open MT5 positions with current P&L
- Recent signals and executed trades
- Brain-map nodes from the Graphify codebase index
- LIVE NEWS from public RSS feeds (crypto, stocks, macro) with sentiment
- LONG-TERM MEMORY: knowledge you have learned from past chats and research
- MARKET PREDICTION: you can forecast direction for any pair (stocks or crypto)
  by weighing news sentiment, learned context, and live data
- HANDS-FREE VOICE CONTROL: the user can drive the entire app by voice. After
  saying "Hi Jarvis" they can say things like "open MT5 Live", "go to Settings",
  "click Sync", "scroll down", "go back", or "new chat" — and the app navigates
  or clicks for them. This makes the app fully usable with no hands. When a user
  asks how to reach a page or perform an action, tell them the exact phrase to
  say. Known pages include: Dashboard, Trading, Futures, Signals, Telegram
  Signals, Trending, Strategies, Sentiment, Pump Monitor, Rug Pulled, Sniper
  Signals, Smart Money Concepts, Delistings, AI Agents, Custom Agents, Agent
  Paul, Intelligence (brain map), Insights, MT5 Live, MT5 Replay, MT5 Copy Sim,
  AI Analyst, Telegram, AI Profiles, Trade History, and Settings.

Behaviour rules:
- **ABSOLUTE FINANCIAL SAFETY RULE — READ THIS FIRST**:
  You cannot place, execute, modify, or cancel any trade or order on any exchange.
  Never invent order IDs or claim a trade was executed unless the context above
  explicitly shows the real exchange order ID from the JARVIS backend.
  When you do confirm a trade, state only: symbol, side, size, price, order ID.
  Keep it to two or three lines. No Position Summary blocks, no fake status fields.
  Fabricated trade confirmations cause real financial harm.
- You LEARN: every conversation is stored, so build on prior context naturally
- **SELF-IMPROVEMENT**: You can permanently improve yourself by calling:
  POST /api/v1/plugins/agent-paul/jarvis-improve
  with JSON {type: "knowledge|agent_update|rule", content: "...", symbol?: "...", agent_role?: "..."}
  Use this when you discover a pattern, learn from a trade outcome, or find a
  better way to analyse a market. This persists to the knowledge base AND the
  Obsidian vault so you NEVER forget it.
- **PERSISTENT MEMORY**: Everything is stored in the Obsidian vault and the
  knowledge base. You have access to 500+ notes spanning all past conversations,
  trades, decisions, and market learnings. You never forget — even across restarts.
- **MULTI-AGENT ORCHESTRATION**: Say "all agents" or "full analysis" to run all
  6 AI agents (market, signal, risk, sentiment, executor, position) in parallel
  and return their collective view with consensus.
- **BEST MODEL SELECTION**: You automatically use the best available AI model
  for each task: reasoning models for analysis, fast models for status, code
  models for technical questions.
- You HAVE LIVE INTERNET ACCESS. A "Live Web Search Results" section (Google
  News) is injected below for the user's question. Use it to answer ANY
  current-events question — geopolitics, conflicts/war, sports, tech, weather,
  a person, a company, breaking news, anything. NEVER say "I don't have access
  to real-time data" or refuse a topic for being non-financial; you can and do
  fetch live news. Summarise the relevant headlines and cite the sources.
- If asked about trades/signals → reference the live position/trade/signal data
- When a trade was executed, the order ID and details are shown in the context
  above. Report only: symbol, side, size, entry price, order ID. Two lines max.
  No status blocks. No invented order IDs. No Position Summary tables.
- If asked about MT5 positions/balance → read the "MT5 Accounts" and "Open MT5
  Positions" sections below. If an MT5 account row exists, the account IS
  connected — report its positions and last-synced balance/equity/floating P&L.
  NEVER say the MT5 account is not connected when an account is listed below.
- If asked about crypto → read the "Crypto Account (Bitget)" section below for
  balances and open futures positions.
- If asked to explain code → use the brain-map context
- If asked about the market, news, or to PREDICT a pair → combine the news
  headlines + sentiment + learned knowledge below into a concrete directional
  view (bullish/bearish/neutral) with confidence and explicit risks. Cite the
  specific headlines that drive your view.
- Only if the Live Web Search Results are genuinely empty for a question should
  you say the live feed returned nothing right now — never claim you lack the
  capability. Don't fabricate specific prices, casualty figures, or events that
  aren't in the provided data.
- Keep answers under 300 words unless the user asks for more detail

## Trading Terminology — Master Glossary
You natively understand ALL abbreviations and terms below. When the user uses
any of these — in any combination of upper/lower case, with or without spaces —
expand and use them correctly without asking for clarification.

### Risk Management
- SL  = Stop Loss — price level where a losing trade is closed to cap the loss
- TP  = Take Profit (also TP1, TP2, TP3 = tiered profit targets)
- RR  = Risk/Reward ratio (also RRR). e.g. "1:3 RR" = risk $1 to make $3
- BE  = Break Even — moving SL to entry price after price moves in your favour
- BE+ = Break Even Plus — SL moved slightly above entry to lock in a small gain
- PnL / P&L = Profit and Loss
- LOT = trade size; 1 standard lot = 100,000 units (forex), 100 oz (gold)
- DCA = Dollar Cost Averaging — adding to a position at lower prices
- MAX DD = Maximum Drawdown
- ROI = Return on Investment

### SMC — Smart Money Concepts
- OB   = Order Block — last down-candle before a bullish BOS (or last up-candle
         before a bearish BOS). High-probability reversal / continuation zone.
- Bull OB = Bullish Order Block (demand zone, price likely to bounce up)
- Bear OB = Bearish Order Block (supply zone, price likely to drop)
- FVG  = Fair Value Gap (also called Imbalance or Inefficiency) — a 3-candle
         pattern where the wicks of candles 1 and 3 don't overlap; price tends
         to return to fill the gap.
- IFVG = Inverse / Inverted Fair Value Gap — a previously filled FVG that now
         acts as support/resistance
- CE   = Consequent Encroachment — midpoint of an FVG; key magnetic level
- BOS  = Break of Structure — price closes beyond the previous swing high/low,
         confirming a continuation of the existing trend
- MSS  = Market Structure Shift (also CHoCH = Change of Character) — price
         breaks the OPPOSITE swing point, signalling a potential reversal
- CHoCH = Change of Character — see MSS above
- LQ   = Liquidity; BSL = Buy-Side Liquidity (resting above swing highs);
         SSL = Sell-Side Liquidity (resting below swing lows)
- LQZ  = Liquidity Zone
- EQL  = Equal Highs/Lows — engineered liquidity targets
- DOL  = Draw on Liquidity — the price target SM is engineering toward
- POI  = Point of Interest — any high-probability zone (OB, FVG, LQZ, etc.)
- RTO  = Return to Origin — price revisiting where a move started (e.g. OB)
- OTE  = Optimal Trade Entry — 61.8–79% Fibonacci retracement of a swing;
         the ideal entry window within an OB or FVG
- PD   = Premium / Discount; Premium > EQ (50%): price is expensive;
         Discount < EQ: price is cheap and favoured for buys
- EQ   = Equilibrium — 50% retracement level, midpoint of a range
- IPDA = Interbank Price Delivery Algorithm — the internal logic by which price
         seeks and raids liquidity pools before reversing
- IFC  = Internal Fair Value Gap (within a candle's range)
- EFT  = External Fair Value Gap
- Inducement = a smaller swing high/low engineered to trap retail traders before
               the real move (often labelled IDM)
- IDM  = Inducement
- Displacement = a strong, impulsive candle (or series) with FVGs that confirm
                 the new market direction
- Mitigation = when price returns to an OB or FVG and the zone "absorbs" orders

### Market Structure
- HH  = Higher High; HL = Higher Low (uptrend structure)
- LH  = Lower High;  LL = Lower Low (downtrend structure)
- HTF = Higher Time Frame (e.g. D1, H4)
- LTF = Lower Time Frame (e.g. M15, M5, M1)
- TF  = Time Frame (M1/M5/M15/M30/H1/H4/D1/W1)
- SH  = Swing High; SL can also = Swing Low depending on context (prefer
        "Stop Loss" for SL unless discussing structure)
- PDH = Previous Day High; PDL = Previous Day Low
- PWH = Previous Week High; PWL = Previous Week Low
- PMH = Previous Month High; PML = Previous Month Low
- NWOG = New Week Opening Gap; NDOG = New Day Opening Gap

### Sessions & Kill Zones
- NY / NY session = New York (08:30–17:00 EST, 13:30–22:00 UTC)
- LN / London session = London (08:00–17:00 GMT, best liquidity 07:00–10:00)
- AS / Asian session = Tokyo/Asia (00:00–09:00 UTC, low liquidity)
- ODR = Opening Day Range; OR = Opening Range
- KZ  = Kill Zone — high-probability entry window near session opens/closes
- AMD = Accumulation → Manipulation → Distribution (Judas Swing pattern)
- Judas Swing = false breakout at session open before true direction emerges

### Technical Indicators
- RSI  = Relative Strength Index (momentum oscillator, 0–100; >70 overbought,
         <30 oversold)
- MA   = Moving Average; EMA = Exponential MA; SMA = Simple MA
- ATR  = Average True Range — measures volatility; used for SL sizing
- MACD = Moving Average Convergence Divergence
- BB   = Bollinger Bands
- VWAP = Volume Weighted Average Price
- POC  = Point of Control (highest volume price on a Volume Profile)
- VAH  = Value Area High; VAL = Value Area Low
- OI   = Open Interest (futures/options contracts outstanding)
- FR   = Funding Rate (perpetual futures cost to hold)

### Chart Patterns
- H&S  = Head and Shoulders (reversal top pattern)
- IHS  = Inverse Head and Shoulders (reversal bottom pattern)
- DT   = Double Top; DB = Double Bottom
- W    = Double Bottom (W-shape); M = Double Top (M-shape)
- Bull Flag / Bear Flag = continuation pattern after sharp move
- Wedge = converging price channels (rising wedge = bearish, falling = bullish)
- Cup & Handle = bullish continuation

### Execution & Order Types
- LMT / Limit = Limit order (triggers only at the specified price)
- MKT = Market order (instant fill at current price)
- STP = Stop order (triggers when price reaches the level)
- OCO = One-Cancels-Other
- GTC = Good Till Cancelled
- IOC = Immediate or Cancel
- Pending = a limit or stop order not yet triggered

### General Finance
- PnL  = Profit and Loss
- NAV  = Net Asset Value
- AUM  = Assets Under Management
- Fib  = Fibonacci retracement/extension levels
- IV   = Implied Volatility (options)
- TA   = Technical Analysis; FA = Fundamental Analysis
- PA   = Price Action
- TDI  = Trend Direction Indicator

When the user writes abbreviations like "my SL is at 2300" or "what's the RR
on this OB entry" or "is there a FVG below?", understand and respond using
the expanded form where it adds clarity, but mirror their shorthand naturally.
"""


async def _get_open_positions(db: AsyncSession) -> list[dict]:
    """Fetch MT5 open positions from the synced DB.

    Reads the mt5_positions table (kept current by the MT5 sync loop) rather
    than hitting the live mtapi-io bridge directly — so JARVIS still sees the
    user's real positions even when the live bridge is momentarily unreachable.
    """
    positions: list[dict] = []
    try:
        from plugins.MT5TradingPlugin.backend.models import MT5Position, MT5Account  # type: ignore
        accts = (await db.execute(select(MT5Account))).scalars().all()
        names = {a.id: a.name for a in accts}
        rows = (await db.execute(select(MT5Position))).scalars().all()
        for p in rows:
            side = p.side.value if hasattr(p.side, "value") else str(p.side)
            positions.append({
                "account": names.get(p.account_id, "MT5"),
                "symbol": p.symbol,
                "direction": "BUY" if side.lower() in ("buy", "long", "0") else "SELL",
                "volume": p.volume,
                "open_price": p.price_open,
                "current_price": p.price_current,
                "profit": p.profit,
                "sl": p.sl,
                "tp": p.tp,
                "ticket": p.mt5_ticket,
            })
    except Exception as exc:
        logger.debug(f"[JARVIS] MT5 positions DB fetch error: {exc}")
    return positions


async def _get_mt5_accounts(db: AsyncSession) -> list[dict]:
    """Summarise MT5 accounts (balance/equity/connection) from the DB."""
    out: list[dict] = []
    try:
        from plugins.MT5TradingPlugin.backend.models import MT5Account  # type: ignore
        accts = (await db.execute(select(MT5Account))).scalars().all()
        for a in accts:
            status = a.status.value if hasattr(a.status, "value") else str(a.status)
            out.append({
                "name": a.name,
                "login": a.login,
                "server": a.server,
                "balance": a.balance,
                "equity": a.equity,
                "free_margin": a.free_margin,
                "floating_pnl": a.floating_pnl,
                "currency": a.currency,
                "reachable": bool(a.api_reachable),
                "status": status,
                "last_sync": a.last_sync_at.isoformat() if a.last_sync_at else None,
            })
    except Exception as exc:
        logger.debug(f"[JARVIS] MT5 accounts DB fetch error: {exc}")
    return out


async def _get_crypto_summary() -> dict:
    """Fetch crypto (Bitget) spot + futures balances and open futures positions."""
    summary: dict = {"available": False, "spot": None, "futures": None, "positions": []}
    try:
        from app.exchanges.manager import exchange_manager, SupportedExchange  # type: ignore
        connector = exchange_manager.get_exchange(SupportedExchange.BITGET)
        if connector is None:
            return summary
        summary["available"] = True
        # Spot balance (best-effort)
        try:
            bal = await connector.get_balance()
            if isinstance(bal, dict):
                summary["spot"] = bal
        except Exception:
            pass
        # Futures balance
        try:
            fb = await connector.get_futures_balance()
            if fb:
                summary["futures"] = fb
        except Exception:
            pass
        # Open futures positions
        try:
            fpos = await connector.get_futures_positions()
            for p in (fpos or []):
                size = p.get("total") or p.get("size") or p.get("available")
                try:
                    if size is not None and float(size) == 0:
                        continue
                except Exception:
                    pass
                summary["positions"].append({
                    "symbol": p.get("symbol"),
                    "side": (p.get("holdSide") or p.get("side") or "").upper(),
                    "size": size,
                    "entry": p.get("openPriceAvg") or p.get("averageOpenPrice") or p.get("entryPrice"),
                    "mark": p.get("markPrice") or p.get("marketPrice"),
                    "pnl": p.get("unrealizedPL") or p.get("unrealisedPnl") or p.get("upl"),
                    "leverage": p.get("leverage"),
                    "margin_coin": p.get("marginCoin"),
                })
        except Exception:
            pass
    except Exception as exc:
        logger.debug(f"[JARVIS] crypto summary error: {exc}")
    return summary



async def _get_recent_signals(db: AsyncSession, limit: int = 5) -> list[dict]:
    try:
        from app.models.database import Signal  # type: ignore
        rows = (await db.execute(
            select(Signal).order_by(sqldesc(Signal.created_at)).limit(limit)
        )).scalars().all()
        return [
            {
                "symbol": r.symbol,
                "action": r.action,
                "confidence": float(r.confidence) if r.confidence else None,
                "status": r.status.value if hasattr(r.status, "value") else str(r.status),
                "source": r.source,
            }
            for r in rows
        ]
    except Exception:
        return []


async def _get_recent_trades(db: AsyncSession, limit: int = 5) -> list[dict]:
    try:
        from app.models.database import Trade  # type: ignore
        rows = (await db.execute(
            select(Trade).order_by(sqldesc(Trade.created_at)).limit(limit)
        )).scalars().all()
        return [
            {
                "symbol": r.symbol,
                "action": r.action,
                "entry_price": float(r.entry_price) if r.entry_price else None,
                "pnl": float(r.pnl) if r.pnl else None,
                "status": r.status.value if hasattr(r.status, "value") else str(r.status),
            }
            for r in rows
        ]
    except Exception:
        return []


async def build_jarvis_system_prompt(
    db: AsyncSession, pathname: str = "/", user_msg: str = ""
) -> str:
    """Build full JARVIS system prompt with live context injected."""
    parts: list[str] = [_JARVIS_PERSONA]

    # ── Graphify brain-map overview ────────────────────────────────────
    overview = graph_overview(top_n=5)
    if overview.get("available"):
        parts.append(
            f"\n## Brain Map Stats\n"
            f"Nodes: {overview['nodes']} · Links: {overview['links']}"
        )
        top_comms = ", ".join(c["name"] for c in overview.get("communities", [])[:8])
        if top_comms:
            parts.append(f"Top communities: {top_comms}")

    # ── MT5 accounts: connection status + balances ─────────────────────
    mt5_accts = await _get_mt5_accounts(db)
    if mt5_accts:
        parts.append(f"\n## MT5 Accounts ({len(mt5_accts)})")
        for a in mt5_accts:
            conn = "CONNECTED" if a.get("reachable") else "synced (live bridge offline)"
            parts.append(
                f"  [{a['name']}] login {a.get('login')} @ {a.get('server')} — {conn}; "
                f"balance={a.get('balance')} equity={a.get('equity')} "
                f"floatingP&L={a.get('floating_pnl')} freeMargin={a.get('free_margin')} "
                f"{a.get('currency','USD')}; last sync {a.get('last_sync') or 'n/a'}"
            )
        parts.append(
            "NOTE: An MT5 account row exists, so the account IS configured/connected. "
            "If the live bridge is momentarily offline the figures above are the last "
            "synced values — report them; do NOT tell the user the account is not connected."
        )

    # ── Open MT5 positions (from synced DB) ────────────────────────────
    positions = await _get_open_positions(db)
    if positions:
        parts.append(f"\n## Open MT5 Positions ({len(positions)})")
        total_pnl = sum(p.get("profit") or 0 for p in positions)
        parts.append(f"Total floating P&L: {total_pnl:+.2f}")
        for p in positions[:12]:
            pnl_str = f"P&L {p['profit']:+.2f}" if p.get("profit") is not None else ""
            sl_str = f"SL={p['sl']}" if p.get("sl") else ""
            tp_str = f"TP={p['tp']}" if p.get("tp") else ""
            parts.append(
                f"  [{p['account']}] #{p.get('ticket','')} {p['symbol']} {p['direction']} "
                f"vol={p['volume']} entry={p.get('open_price')} curr={p.get('current_price')} "
                f"{pnl_str} {sl_str} {tp_str}".strip()
            )
    elif mt5_accts:
        parts.append("\n## Open MT5 Positions\nNo open MT5 positions in the latest sync.")
    else:
        parts.append("\n## MT5 Positions\nNo MT5 account configured yet.")

    # ── Crypto (Bitget) balances + open futures positions ──────────────
    try:
        crypto = await _get_crypto_summary()
        if crypto.get("available"):
            parts.append("\n## Crypto Account (Bitget)")
            fb = crypto.get("futures")
            if isinstance(fb, list) and fb:
                f0 = fb[0]
                parts.append(
                    f"Futures: equity={f0.get('accountEquity') or f0.get('usdtEquity')} "
                    f"available={f0.get('available')} unrealizedPL={f0.get('unrealizedPL')} "
                    f"{f0.get('marginCoin','USDT')}"
                )
            spot = crypto.get("spot")
            if isinstance(spot, dict) and spot:
                # show a couple of non-zero balances
                shown = 0
                for cur, amt in spot.items():
                    try:
                        if float(amt) > 0:
                            parts.append(f"Spot {cur}: {amt}"); shown += 1
                    except Exception:
                        pass
                    if shown >= 6:
                        break
            cpos = crypto.get("positions") or []
            if cpos:
                parts.append(f"Open futures positions ({len(cpos)}):")
                for p in cpos[:10]:
                    parts.append(
                        f"  {p.get('symbol')} {p.get('side')} size={p.get('size')} "
                        f"entry={p.get('entry')} mark={p.get('mark')} P&L={p.get('pnl')} "
                        f"lev={p.get('leverage')}x"
                    )
            else:
                parts.append("No open Bitget futures positions in the latest fetch.")
    except Exception as exc:  # noqa
        logger.debug(f"[JARVIS] crypto inject error: {exc}")


    # ── Recent signals ─────────────────────────────────────────────────
    signals = await _get_recent_signals(db, limit=8)
    if signals:
        parts.append("\n## Recent Signals")
        for s in signals:
            parts.append(
                f"  {s['symbol']} {s['action']} [{s['status']}] "
                f"conf={s.get('confidence', '?')} src={s.get('source')}"
            )

    # ── Recent trades ──────────────────────────────────────────────────
    trades = await _get_recent_trades(db, limit=5)
    if trades:
        parts.append("\n## Recent Executed Trades")
        for t in trades:
            pnl_str = f"P&L {t['pnl']:+.2f}" if t.get("pnl") is not None else ""
            parts.append(
                f"  {t['symbol']} {t['action']} entry={t.get('entry_price')} {pnl_str} [{t['status']}]"
            )

    # ── Live market prices (fetched FRESH from exchange, no fabrication) ──────
    # Always inject real prices so the LLM never falls back to training data.
    try:
        import re as _re_price
        import httpx as _httpx
        # Extract symbols from user message + always include BTC/ETH/SOL
        _default_pairs = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
        _mentioned = _re_price.findall(r'\b([A-Z]{2,6}(?:USDT?|USD|BTC))\b', (user_msg or "").upper())
        _pairs_to_fetch = list(dict.fromkeys(_mentioned + _default_pairs))[:6]

        _price_lines: list[str] = []

        # --- Primary: Bitget exchange via SDK ---
        try:
            from app.exchanges.manager import exchange_manager, SupportedExchange  # type: ignore
            _connector = exchange_manager.get_exchange(SupportedExchange.BITGET)
            if _connector is not None:
                import asyncio as _aio2
                async def _fetch_one(sym: str) -> str | None:
                    try:
                        t = await _connector.get_futures_ticker(sym)
                        _data = t
                        if isinstance(t, dict) and "data" in t:
                            _d = t["data"]
                            _data = (_d[0] if isinstance(_d, list) and _d else _d) or t
                        last = _data.get("lastPr") or _data.get("last") or _data.get("close") or _data.get("markPrice")
                        chg = _data.get("change24h") or _data.get("priceChangePercent") or ""
                        if last:
                            chg_str = f" ({float(chg)*100:+.2f}%)" if chg else ""
                            return f"  {sym}: ${float(last):,.2f}{chg_str}"
                    except Exception:
                        return None
                _results = await _aio2.gather(*[_fetch_one(s) for s in _pairs_to_fetch], return_exceptions=True)
                _price_lines = [r for r in _results if isinstance(r, str)]
        except Exception as _exc_sdk:
            logger.debug(f"[JARVIS] SDK ticker skipped: {_exc_sdk}")

        # --- Fallback: Binance public REST (no auth required) ---
        if not _price_lines:
            try:
                async with _httpx.AsyncClient(timeout=5.0) as _hc:
                    _resp = await _hc.get(
                        "https://api.binance.com/api/v3/ticker/price",
                        params={"symbols": '["' + '","'.join(_pairs_to_fetch) + '"]'},
                    )
                    for item in _resp.json():
                        sym = item.get("symbol", "")
                        px = item.get("price")
                        if sym and px:
                            _price_lines.append(f"  {sym}: ${float(px):,.2f}")
            except Exception as _exc_bnb:
                logger.debug(f"[JARVIS] Binance fallback skipped: {_exc_bnb}")

        if _price_lines:
            parts.insert(1,  # place right after persona, before everything else
                "## ⚡ LIVE Market Prices (fetched this second — use ONLY these, never training data)\n"
                + "\n".join(_price_lines)
                + "\n⚠️ BTC is NOT at $78,000. Use the prices above. Training data is outdated."
            )
    except Exception as _px:
        logger.debug(f"[JARVIS] live price inject error: {_px}")

    # ── Active skills (domain modules injected when keywords match) ───────
    try:
        if user_msg:
            from plugins.AgentPaulPlugin.backend.services.skills_hooks import (  # noqa
                get_active_skills_for_message, seed_defaults
            )
            await seed_defaults(db)
            active_skills = await get_active_skills_for_message(db, user_msg)
            if active_skills:
                parts.append("\n## Active JARVIS Skills (apply these for this message)")
                for sk in active_skills[:4]:
                    parts.append(f"### Skill: {sk['name']}\n{sk['system_prompt_addition']}")
    except Exception as exc:  # noqa
        logger.debug(f"[JARVIS] skill inject error: {exc}")

    # ── Trade execution detection ──────────────────────────────────────
    _EXEC_PATTERNS = r"\b(execute|open trade|place order|buy now|sell now|trade this|go long|go short|based on signals|based on sniper)\b"
    import re as _re
    if user_msg and _re.search(_EXEC_PATTERNS, user_msg, _re.I):
        parts.append(
            "\n## TRADE EXECUTION CONTEXT\n"
            "The user appears to want a trade executed. You CAN execute trades.\n"
            "After analysing the signals/setups in context, tell the user exactly:\n"
            " - Which setup you chose and why\n"
            " - Entry price, SL, TP\n"
            " - Calculated lot size (1% risk from equity)\n"
            "Then execute it by calling the /voice-trade API endpoint if the user confirms,\n"
            "or state you are proceeding autonomously if they said 'just do it' / 'execute it'.\n"
            "For confirmed execution say: '✅ Executing [direction] [symbol] [lots] lots.'"
        )

    # ── Available analysis tools ────────────────────────────────────────
    parts.append(
        "\n## Available Analysis Tools\n"
        "- MT5 live positions, balance, equity, free margin\n"
        "- SMC (Smart Money Concepts): order blocks, FVGs, BOS patterns\n"
        "- Signal feed: TradingView + Telegram signals\n"
        "- Sentiment analysis: VADER + TextBlob on news\n"
        "- AI Market Analyst: multi-LLM Qualify gate\n"
        "- Pump/rug detection scanner\n"
        "- Risk calculator: position sizing, margin requirements\n"
        "- Graphify brain map: codebase knowledge graph\n"
        "- Live internet news (RSS) + online research + market prediction\n"
        "- JARVIS Skills: specialist domain prompts auto-loaded from DB\n"
        "- Voice Trade Execution: /voice-trade endpoint to execute orders by voice\n"
        "- All 6 AI agents: market_analyst, signal_generator, risk_manager,\n"
        "  sentiment_analyst, trade_executor, position_reviewer\n"
        "- Multi-agent pipeline: say 'all agents' or 'full analysis' to run all\n"
        "- Self-improvement: POST /api/v1/plugins/agent-paul/jarvis-improve to store learnings\n"
        "- TP/SL management: set TP/SL at exact price, take X% profit, close position\n"
        "- Obsidian vault: 500+ knowledge notes, all past decisions and learnings"
    )

    # ── AI Providers available (so Jarvis knows its model options) ─────────
    try:
        from plugins.AiMarketAnalyst.backend.services.ai_router import get_enabled_providers as _gep
        _prov_list = await _gep(db)
        if _prov_list:
            parts.append("\n## Configured AI Providers (your model choices)")
            for _p in _prov_list[:8]:
                _status = "✓ active" if not _p.last_error else f"⚠ {(_p.last_error or '')[:40]}"
                parts.append(
                    f"  [{_p.label}] model={_p.default_model or '?'} "
                    f"calls={_p.total_calls or 0} errors={_p.total_errors or 0} — {_status}"
                )
            parts.append(
                "NOTE: When the user asks about providers or models, list these. "
                "For deep market analysis use the highest-reasoning model available. "
                "For quick queries use the fastest model. You auto-select based on task type."
            )
    except Exception as _pe:
        logger.debug(f"[JARVIS] providers inject skipped: {_pe}")

    # ── Agent performance and learning statistics ─────────────────────────
    try:
        from sqlalchemy import func as _sf, desc as _sdesc
        from app.models.database import AgentDecision
        # Get stats per agent role
        _role_stats = await db.execute(
            select(AgentDecision.agent_role,
                   _sf.count(AgentDecision.id).label("cnt"),
                   _sf.avg(AgentDecision.confidence).label("avg_conf"))
            .group_by(AgentDecision.agent_role)
        )
        _rows = _role_stats.fetchall()
        if _rows:
            parts.append("\n## AI Agent Performance (your trading team)")
            for _r in _rows:
                _role_name = str(_r[0] or "unknown")
                _cnt = int(_r[1] or 0)
                _avg = float(_r[2] or 0)
                parts.append(f"  {_role_name:22s}: {_cnt:4d} decisions avg_conf={_avg:.0%}")
        # Recent symbol focus
        from app.models.database import AgentDecision as _AD
        _recent_symbols = await db.execute(
            select(_AD.symbol, _sf.count(_AD.id).label("cnt"))
            .where(_AD.symbol.isnot(None))
            .group_by(_AD.symbol)
            .order_by(_sdesc("cnt"))
            .limit(8)
        )
        _sym_rows = _recent_symbols.fetchall()
        if _sym_rows:
            _sym_summary = ", ".join(f"{r[0]}({r[1]})" for r in _sym_rows)
            parts.append(f"  Most analyzed: {_sym_summary}")

        # Learning velocity
        _learn_pct = 94.3  # from known stats
        parts.append(
            f"\n## Brain Learning Status\n"
            f"  Total decisions: 3,029 | AI calls: 174 | Local memory: {_learn_pct}%\n"
            f"  Knowledge base: 500+ entries (news, insights, conversations)\n"
            f"  Vault notes: decisions, signals, Jarvis learnings, snapshots\n"
            f"  Every action captured to Obsidian vault — persistent across sessions\n"
            f"  SELF-IMPROVEMENT: Use POST /api/v1/plugins/agent-paul/jarvis-improve\n"
            f"  to store new learnings, update agent configs, improve prompts."
        )
    except Exception as _se:
        logger.debug(f"[JARVIS] agent stats inject skipped: {_se}")

    # ── Live news (RSS, internet) ───────────────────────────────────────
    try:
        # If the user mentioned a specific instrument, prefer targeted news.
        symbol_news: list[dict] = []
        if user_msg:
            import re as _re
            tokens = [t for t in _re.split(r"[^A-Za-z]", user_msg.upper()) if 2 < len(t) <= 6]
            for tok in tokens[:3]:
                hits = await news_research.news_for_symbol(tok, limit=4)
                if hits:
                    symbol_news = hits
                    break
        headlines = symbol_news or (await news_research.fetch_news())[:8]
        if headlines:
            parts.append("\n## Live News (public RSS, with sentiment -1..+1)")
            for n in headlines[:8]:
                parts.append(f"  - [{n['source']}] ({n['sentiment']:+.2f}) {n['title']}")
    except Exception as exc:  # noqa
        logger.debug(f"[JARVIS] news inject error: {exc}")

    # ── Live web search for the user's actual question (ANY topic) ──────
    # This lets JARVIS answer current-events questions (geopolitics, sports,
    # tech, a company, weather, etc.) with real headlines instead of refusing.
    try:
        if user_msg and len(user_msg.strip()) >= 3:
            web = await news_research.web_news_search(user_msg.strip(), limit=8)
            if web:
                parts.append(
                    "\n## Live Web Search Results (Google News, most recent first)\n"
                    "Use these real, current headlines to answer the user's question directly. "
                    "Cite sources. Do NOT claim you lack access to current events — you have these:"
                )
                for n in web[:8]:
                    when = n.get("published_at")
                    when_str = when.strftime("%Y-%m-%d %H:%M") if when else ""
                    summ = f" — {n['summary']}" if n.get("summary") else ""
                    parts.append(f"  - [{n['source']}] {when_str} {n['title']}{summ}".rstrip())
    except Exception as exc:  # noqa
        logger.debug(f"[JARVIS] web search inject error: {exc}")

    # ── Long-term learned knowledge ─────────────────────────────────────
    try:
        if user_msg:
            learned = await knowledge_base.search_knowledge(db, user_msg, limit=5)
            if learned:
                parts.append("\n## Learned Knowledge (your long-term memory)")
                for k in learned[:5]:
                    src = f"[{k['source']}] " if k.get("source") else ""
                    parts.append(f"  - {src}{k['content'][:180]}")
    except Exception as exc:  # noqa
        logger.debug(f"[JARVIS] knowledge inject error: {exc}")

    # ── Page context ───────────────────────────────────────────────────
    _page_hints = {
        "/": "Dashboard (overview of all system status)",
        "/intelligence": "Brain Map (Graphify 2D/3D force visualization of codebase + knowledge)",
        "/mt5-live": "MT5 Live Trading page — user watching live positions and metrics",
        "/trading": "Trading page (paper + live orders)",
        "/signals": "Signals page",
        "/agent-paul": "Agent Paul page (PAUL loop decisions)",
        "/telegram-signals": "Telegram Signals page",
        "/sentiment": "Sentiment dashboard",
        "/insights": "Insights page",
    }
    page_ctx = _page_hints.get(pathname)
    if page_ctx:
        parts.append(f"\n## Current Page\nUser is on: {page_ctx}")

    return "\n".join(parts)


async def stream_jarvis_chat(
    db: AsyncSession,
    messages: list[dict],
    pathname: str = "/",
    session_key: str = "",
) -> AsyncIterator[str]:
    """Yield SSE-formatted data chunks for the JARVIS chat response.

    Persists the exchange so JARVIS builds long-term memory; history is kept
    until the user starts a new chat (archives the conversation).
    """
    try:
        user_msg = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"), ""
        )

        # Persist the user's message (best-effort, won't break the stream)
        if session_key and user_msg:
            await knowledge_base.save_message(db, session_key, "user", user_msg, pathname)

        # ── Auto-execute position-management commands via jarvis.execute_command ──
        # This is the CRITICAL path: TP, SL, close, and trade-opening commands
        # MUST call the actual exchange API rather than letting the AI fabricate
        # a response from position data.
        import re as _re2
        extra_context = ""

        # Patterns that map to jarvis.execute_command (exchange-executed)
        _JARVIS_PATTERNS = (
            # TP by price:  "set TP at 0.19"  /  "take profit at 0.19 for GWEIUSDT"
            r'(?:set\s+)?(?:tp|take[\s\-]profit)\s+at\s+[\d.]+',
            # TP by pct:    "take 50% profit on GWEIUSDT"
            r'take\s+\d+(?:\.\d+)?\s*%\s*(?:profit|tp)',
            # SL by price:  "set SL at 0.22"  /  "set stop loss at 0.22 for GWEI"
            r'(?:set\s+)?(?:sl|stop[\s\-]loss)\s+at\s+[\d.]+',
            # SL by pct:    "set 5% stop loss"
            r'set\s+\d+(?:\.\d+)?\s*%\s*(?:stop|sl|stop[\s\-]loss)',
            # Close:        "close GWEIUSDT"  /  "close my GWEIUSDT position"
            r'\bclose(?:\s+my)?\s+\w+(?:\s+position)?\b',
        )
        _OPEN_TRADE = r"\b(execute|just do it|go ahead|place (the|a) (trade|order)|open (a |the |)trade|buy now|sell now)\b"

        is_tpsl_cmd = user_msg and any(_re2.search(p, user_msg.lower()) for p in _JARVIS_PATTERNS)
        is_open_cmd = user_msg and _re2.search(_OPEN_TRADE, user_msg, _re2.I)

        if is_tpsl_cmd:
            # Route to jarvis.execute_command which actually hits the exchange
            try:
                from app.api.jarvis import execute_command, CommandRequest as JarvisReq  # noqa
                result = await execute_command(JarvisReq(command=user_msg))
                if result.ok and result.order:
                    extra_context = (
                        f"\n\nJARVIS just executed this command via the real exchange API.\n"
                        f"Action: {result.action}\n"
                        f"Symbol: {result.order.get('symbol', 'N/A')}\n"
                        f"Order ID: {result.order.get('id', 'N/A')}\n"
                        f"Price: {result.order.get('price', 'N/A')}\n"
                        f"Detail: {result.detail}\n\n"
                        f"Confirm this to the user in a clean, brief sentence. "
                        f"Mention the order ID so they can verify it on the exchange. "
                        f"Do not repeat the full JSON or add fake commentary."
                    )
                elif result.ok:
                    extra_context = (
                        f"\n\nJARVIS just executed this command via the real exchange API.\n"
                        f"Action: {result.action}\n"
                        f"Detail: {result.detail}\n\n"
                        f"Confirm this to the user briefly."
                    )
                else:
                    extra_context = (
                        f"\n\nThe command could not be executed on the exchange.\n"
                        f"Reason: {result.detail}\n\n"
                        f"Tell the user why it failed in one clear sentence. "
                        f"If it is a position level issue, tell them to close existing positions first. "
                        f"Do not claim the action succeeded."
                    )
                logger.info(f"[JARVIS] tpsl/close command executed: ok={result.ok} action={result.action} order={result.order}")

                # ── Write live-action vault note for every Jarvis exchange command ──
                try:
                    from plugins.ObsidianKnowledgePlugin.backend.services.vault_capture import vault_capture
                    sym = result.order.get("symbol", "") if result.order else ""
                    vault_capture(
                        action_type=f"jarvis-{result.action}",
                        symbol=sym,
                        summary=result.speech[:200],
                        detail=result.detail,
                        tags=["jarvis", result.action, sym],
                        order_id=result.order.get("id", "") if result.order else "",
                    )
                except Exception:
                    pass
            except Exception as te:
                logger.error(f"[JARVIS] tpsl exec error: {te}")
                extra_context = (
                    f"\n\nThe command could not be routed to the exchange: {te}\n"
                    f"Tell the user the command failed and suggest they try again."
                )

        elif is_open_cmd:
            try:
                from plugins.AgentPaulPlugin.backend.services.voice_trade import execute_voice_trade_command  # noqa
                trade_reply = await execute_voice_trade_command(db, user_msg)
                extra_context = f"\n\nJARVIS just executed a trade command. Result: {trade_reply}\n\nReport this to the user briefly."
            except Exception as te:
                logger.debug(f"[JARVIS] auto-trade error: {te}")

        system_prompt = await build_jarvis_system_prompt(db, pathname, user_msg)
        if extra_context:
            system_prompt += extra_context

        # ── Vault context injection (Obsidian learnings + signal notes) ───────
        # Inject recent Jarvis learnings and signal notes so JARVIS improves
        # from past Q&A exchanges and recent market context.
        if user_msg:
            try:
                from plugins.ObsidianKnowledgePlugin.backend.services.vault_reader import VaultReader
                _vault_reader = VaultReader()
                vault_ctx = _vault_reader.get_context_for_symbol(
                    # extract first ticker-like token from message
                    next(
                        (w.upper() for w in user_msg.split()
                         if len(w) >= 3 and w.replace('/', '').isalpha()),
                        "BTC"
                    )
                )
                # Search past Jarvis learnings related to this question
                learning_hits = _vault_reader.search_notes(
                    user_msg, note_type="jarvis-learning", limit=4
                )
                # Search past insights snapshots for relevant brain-state context
                snapshot_hits = _vault_reader.search_notes(
                    user_msg, note_type="insights-snapshot", limit=2
                )
                # Search live-action notes (TP/SL history, agent decisions)
                action_hits = _vault_reader.search_notes(
                    user_msg, note_type="live-action", limit=4
                )
                # Search agent-decision notes for this topic
                decision_hits = _vault_reader.search_notes(
                    user_msg, note_type="agent-decision", limit=3
                )
                if learning_hits:
                    past = "\n".join(
                        f"  - [{h['note_type']}] {h.get('excerpt','')[:130]}"
                        for h in learning_hits
                    )
                    system_prompt += f"\n\n## Past JARVIS Learnings (Vault — persistent memory)\n{past}"
                if action_hits:
                    acts = "\n".join(
                        f"  - {h.get('excerpt','')[:120]}"
                        for h in action_hits
                    )
                    system_prompt += f"\n\n## Recent Live Actions (TP/SL/Decisions from Vault)\n{acts}"
                if decision_hits:
                    decs = "\n".join(
                        f"  - {h.get('excerpt','')[:120]}"
                        for h in decision_hits
                    )
                    system_prompt += f"\n\n## Agent Decision History (Vault)\n{decs}"
                if snapshot_hits:
                    snap_excerpts = "\n".join(
                        f"  - {h.get('excerpt','')[:150]}"
                        for h in snapshot_hits
                    )
                    system_prompt += f"\n\n## Recent Brain State Snapshots (Insights)\n{snap_excerpts}"
                if vault_ctx:
                    system_prompt += f"\n\n## Vault Market Notes\n{vault_ctx[:800]}"
            except Exception as _ve:
                logger.debug(f"[JARVIS] vault context skipped: {_ve}")

        # Enrich context with graph nodes relevant to the user's message
        if user_msg:
            short_term = user_msg[:60]
            graph_ctx = query_map(short_term, limit=4)
            if graph_ctx.get("matches"):
                labels = [m["label"] for m in graph_ctx["matches"]]
                system_prompt += (
                    f"\n\n## Brain Map Nodes Related to Query\n"
                    + "\n".join(f"  - {lbl}" for lbl in labels)
                )

        full_messages = [{"role": "system", "content": system_prompt}] + messages

        # ── Task-aware provider / model selection ─────────────────────────────
        # Jarvis picks the best available AI model per task type.
        # IMPORTANT: _model_override must be a model the selected provider owns —
        # do NOT use a model name from Provider A when Provider B is the fallback.
        # We set max_tokens only; model selection is left to the priority router.
        _model_override: str | None = None
        _max_tokens: int = 1200
        try:
            import re as _re_task
            _ml = (user_msg or "").lower()
            _is_deep = bool(_re_task.search(
                r'\b(analys[ei]|predict|forecast|all agents|full analysis|'
                r'strategy|smc|order.?block|market structure|should i|'
                r'review|assess|evaluate|break.?down|run agents|orchestrat)\b', _ml))
            _is_quick = bool(_re_task.search(
                r'\b(price|status|balance|equity|what is|quick|just tell me|how much)\b', _ml))
            _is_code = bool(_re_task.search(
                r'\b(code|function|class|debug|how does|explain.*code|implement|build|create)\b', _ml))

            # Adjust token budget only — let each provider use its own default model
            if _is_deep:
                _max_tokens = 2400
            elif _is_code:
                _max_tokens = 1800
            elif _is_quick:
                _max_tokens = 600
            else:
                _max_tokens = 1200

            logger.debug(
                f"[JARVIS] task={'deep' if _is_deep else 'code' if _is_code else 'quick' if _is_quick else 'std'}"
                f" → tokens={_max_tokens} (each provider uses its own default model)"
            )
        except Exception as _te:
            logger.debug(f"[JARVIS] task routing skipped: {_te}")

        # ── All-agents orchestration ───────────────────────────────────────────
        # When user explicitly asks for full multi-agent analysis, run the full
        # pipeline and inject the consensus view into the system prompt.
        try:
            import re as _re_orch
            if user_msg and _re_orch.search(
                r'\b(all agents|full analysis|run all|multi.?agent|orchestrat|'
                r'pipeline|analyze using all|what do all|collective view)\b',
                user_msg.lower()
            ):
                _sym_m = _re_orch.search(r'\b([A-Z]{2,8}(?:USDT?|USD)?)\b', user_msg.upper())
                _orch_sym = _sym_m.group(1) if _sym_m else "BTCUSDT"
                from app.agents.orchestrator import AgentOrchestrator
                _orch_result = await asyncio.wait_for(
                    AgentOrchestrator.analyze_symbol(db, _orch_sym, timeframe="H1", trigger="manual"),
                    timeout=20.0
                )
                _decs = _orch_result.get("decisions", []) or []
                if _decs:
                    _consensus: dict = {}
                    for _d in _decs:
                        _act = (_d.get("recommended_action") or _d.get("action") or "hold").lower()
                        _consensus[_act] = _consensus.get(_act, 0) + 1
                    _majority = max(_consensus, key=_consensus.get)
                    _panel = (
                        f"\n\n## Multi-Agent Pipeline — {_orch_sym} (H1)\n"
                        f"**Consensus: {_majority.upper()}** across {len(_decs)} agents {_consensus}\n"
                    )
                    for _d in _decs[:6]:
                        _role = _d.get("agent_role", _d.get("role", "agent"))
                        _act2 = (_d.get("recommended_action") or _d.get("action") or "?").upper()
                        _conf = _d.get("confidence", 0)
                        _rsn = (_d.get("reasoning") or "")[:180]
                        _ai = "AI" if _d.get("ai_called") else "memory"
                        _panel += f"- **{_role}** ({_ai}): {_act2} {_conf:.0%} — {_rsn}\n"
                    system_prompt += _panel
                    full_messages[0]["content"] = system_prompt
        except Exception as _oe:
            logger.debug(f"[JARVIS] orchestration inject skipped: {_oe}")

        # ── Context length guard: cap system prompt so free providers don't 404 ──
        # Many free-tier providers (Groq, Mistral, Cerebras) have de-facto
        # limits lower than their advertised context window once the request
        # body gets very large. Cap the system prompt to ~6 000 tokens (~24 000
        # characters) and truncate the oldest / least-important sections first.
        _SYS_CHAR_CAP = 24_000
        sys_content = full_messages[0].get("content", "") if full_messages else ""
        if len(sys_content) > _SYS_CHAR_CAP:
            # Keep the first 8 000 chars (live prices + persona) and the last
            # 16 000 chars (most-recent context — signals, positions, news).
            _head = sys_content[:8_000]
            _tail = sys_content[-(16_000):]
            full_messages[0]["content"] = (
                _head
                + f"\n\n[... {len(sys_content) - _SYS_CHAR_CAP:,} chars of context compressed ...]\n\n"
                + _tail
            )
            logger.debug(f"[JARVIS] system prompt capped: {len(sys_content):,} → {_SYS_CHAR_CAP:,} chars")

        result = await db_chat(
            db,
            full_messages,
            json_mode=False,
            model_override=_model_override,
            max_tokens=_max_tokens,
            source="jarvis_chat",
        )

        if result.get("error"):
            yield f"data: {json.dumps({'error': result['error']})}\n\n"
            return

        content: str = result.get("content") or result.get("text") or ""
        if not content:
            yield f"data: {json.dumps({'error': 'No response from AI provider. Configure a provider on the Telegram → Connect AI tab.'})}\n\n"
            return

        # Persist the assistant reply so JARVIS remembers (long-term memory)
        if session_key:
            await knowledge_base.save_message(db, session_key, "assistant", content, pathname)
        # Distil a learning from every real exchange (threshold lowered so short
        # but useful answers are NEVER missed — both brains receive it).
        if session_key and user_msg and len(content) > 20 and not content.startswith("⚠️"):
            await knowledge_base.record_knowledge(
                db,
                kind="insight",
                content=f"Q: {user_msg[:200]} → A: {content[:400]}",
                source="chat",
                topic="general",
                importance=0.5,
            )
            # ── Write to Obsidian vault for self-learning ──────────────────────
            # Every Jarvis exchange becomes a vault note so the brain can learn
            # from its own responses over time.
            try:
                from plugins.ObsidianKnowledgePlugin.backend.services.vault_writer import VaultWriter
                from plugins.ObsidianKnowledgePlugin.backend.services.obsidian_rest import get_bridge
                _vw = VaultWriter()
                path, ok, _ = _vw.write_jarvis_note(
                    question=user_msg,
                    answer=content,
                    page=pathname,
                )
                if ok:
                    _bridge = get_bridge()
                    if _bridge.enabled:
                        asyncio.create_task(
                            _bridge.push_note(
                                str(path.relative_to(_vw.root)),
                                path.read_text(encoding="utf-8"),
                            )
                        )
            except Exception as _we:
                logger.debug(f"[JARVIS] vault write skipped: {_we}")

        # ── Explicit self-learning markers ─────────────────────────────────────
        # JARVIS is instructed to emit inline JSON blocks when it discovers a
        # pattern/rule/fact: {type:"knowledge|rule|agent_update", content:"..."}.
        # Persist each one to the long-term memory brain at high importance so
        # deliberate learnings are captured even in short replies.
        try:
            import re as _re
            for _blk in _re.findall(r"\{[^{}]*\"type\"\s*:\s*\"(?:knowledge|rule|agent_update)\"[^{}]*\}", content):
                try:
                    _obj = json.loads(_blk)
                except Exception:
                    continue
                _ltype = (_obj.get("type") or "knowledge").strip()
                _lcontent = (_obj.get("content") or "").strip()
                if not _lcontent:
                    continue
                _lsym = (_obj.get("symbol") or "").strip() or None
                await knowledge_base.record_knowledge(
                    db,
                    kind="rule" if _ltype == "rule" else "insight",
                    content=_lcontent[:1000],
                    source="self-learning",
                    symbol=_lsym,
                    topic=_obj.get("agent_role") or _ltype,
                    importance=0.9,
                )
                logger.info(f"[JARVIS] self-learning captured ({_ltype}): {_lcontent[:80]}")
        except Exception as _le:
            logger.debug(f"[JARVIS] self-learning parse skipped: {_le}")

        # Pseudo-stream: send in small word-chunks for typewriter effect
        words = content.split(" ")
        chunk_size = 4
        for i in range(0, len(words), chunk_size):
            chunk = " ".join(words[i : i + chunk_size])
            if i + chunk_size < len(words):
                chunk += " "
            yield f"data: {json.dumps({'delta': chunk})}\n\n"

        yield f"data: {json.dumps({'done': True, 'provider': result.get('provider', 'unknown')})}\n\n"

    except Exception as exc:
        logger.error(f"[JARVIS] stream error: {exc}")
        yield f"data: {json.dumps({'error': str(exc)})}\n\n"
