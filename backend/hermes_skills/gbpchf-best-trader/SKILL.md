---
name: gbpchf-best-trader
description: Best-trader playbook for GBPCHF — forex cross — session overlap, carry/beta aware, medium spread
symbol: GBPCHF
asset_class: forex-cross
group: FX Crosses — GBP
linked_agents: [market_analyst, sentiment_analyst, signal_generator, risk_manager, trade_executor, position_reviewer, strategy_optimizer]
jarvis_role: ceo
jarvis_name: JARVIS
version: 1
source: best-trader-bootstrap
---

# GBPCHF — Best Trader (FX Crosses — GBP)

> Stock playbook — chair JARVIS + 7-seat desk. 1 skill per pair, long+short. Execution gated by `RoomSettings.execution_enabled` (paper trades always, live requires gate). Auto-harvested wins coexist as `{{symbol}}-{{action}}-{{ts}}` session skills.

**Symbol:** GBPCHF
**Asset class:** forex-cross — forex cross — session overlap, carry/beta aware, medium spread
**Linked agents:** market_analyst, sentiment_analyst, signal_generator, risk_manager, trade_executor, position_reviewer, strategy_optimizer (all 7 specialists)
**JARVIS chair:** ceo · JARVIS (seat -1, via AiMarketAnalyst pool, SOUL.md: JARVIS/Paul/SOX merged)
**Group:** FX Crosses — GBP
**Source:** `frontend/src/constants/tradingPairs.ts:PAIR_GROUPS` → bootstrap A+A

## Why this pair moves
Rate spread + carry + risk beta vs majors. Crosses amplify DXY via both legs; check both USD pairs before calling direction.

## Sessions & volatility
Best at London/NY overlap. Spread 1–3 pips — use limit at level, not market. Volatile expansion = limit at EMA20/fib 0.5–0.618 pullback.

## Levels
Cross-specific OB/FVG + channel edges. Cross must align with both USD legs; conflict = hold.

## Risk & sizing (all pairs)
- Risk 1% equity per trade (desk default). Scale 0.65→1%, 0.80→3%, 0.90→5%.
- SL 0.8–2.5×ATR, entry within 0.6×ATR of level, R:R ≥1:1.5 to first TP.
- Max 3 correlated same-direction positions; daily drawdown >5% = stand down.
- Volatile expansion (ATR >=1.25× avg, ADX>30) mid-range between levels = HOLD with resting order at level — never chase (XAU 2026-08-28 wipeout shape).
- Every BUY/SELL needs invalidation price before entry. If you cannot name it, there is no trade.

## Chair & desk linking
This skill is owned by the full desk — chair **JARVIS (ceo, seat -1)** + 7 seats: Sakhile (market_analyst), Lerato (sentiment_analyst), Naledi (signal_generator), Thabo (risk_manager), Puso (trade_executor), Kabelo (position_reviewer), Zanele (strategy_optimizer). It is injected into every `orchestrator._gather_context` for this symbol and recalled via `hermes/search` (FTS5) + `GET /jarvis/skill?symbol=`. PaulChat `/skill SYMBOL` surfaces it verbatim.


## Entry quality gate (non-negotiable)
Every BUY/SELL needs price AT a structural level (fib golden zone 0.5–0.618, order block, FVG, demand/supply base, channel edge, broken level retest). Mid-range between levels = right idea, wrong price → HOLD + resting limit at level. Volatile expansion inverts impulse: wait for pullback into EMA20/fib/OB or confirmed break+retest; market order mid-air = chase.

## When to apply
When GBPCHF shows the same structure + momentum read (EMA stack, range position, ATR expansion, 60-bar drive/efficiency) and consensus re-forms (≥0.4 agreement or strong momentum). Forecast `kronos_forecast` agreeing with structure raises confidence — but only at a level.

## Execution gate
Paper executes always; live executes only when `RoomSettings.execution_enabled=true`. Never bypassed. FTS5 is recall-only — scoring stays on Postgres `AgentDecision`.

## Learned (auto)
<!-- auto-learned:start -->
_No learned adjustments yet — will be appended after 12+ resolved decisions for GBPCHF (self-improve loop, win-rate & avg PnL)._
<!-- auto-learned:end -->
