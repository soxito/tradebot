---
type: decision
agent: position_reviewer
symbol: BTC/USDT
action: hold
confidence: 0.96
ai_called: True
date: 2026-09-06
tags:
  - decision
  - BTC-USDT
  - position_reviewer
  - hold
---

            # Decision: position_reviewer → BTC/USDT — HOLD @ 2026-09-06

            ## Summary
            **Agent**: `position_reviewer` | **Action**: `HOLD` | **Confidence**: 96%
            **AI Called**: Yes

            ## Reasoning
            The position has been open for over 64 days with no market data available, and its stop loss and
take profit are inverted relative to the long side, indicating a construction error. Combined with
150x leverage and an unknown current price, the risk is unmanageable; the original thesis cannot be
validated, so the position should be closed immediately.

            ## Related
            - [[_daily/2026-09-06]]
            - [[signals/BTC-USDT]] ← Recent signals for this symbol
