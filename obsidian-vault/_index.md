---
type: index
updated: 2026-09-08
tags:
  - dashboard
---

# TradeBot Brain — Knowledge Dashboard

> Synced from TradeBot on 2026-09-08.
> Navigate: [[communities]] | [[strategies]] | [[_daily/2026-09-08]]

---

## Today's Activity
```dataview
TABLE symbol, action, confidence
FROM "signals"
WHERE contains(file.name, "2026-09-08")
SORT confidence DESC
LIMIT 10
```

## Recent Decisions (last 7 days)
```dataview
TABLE agent, symbol, action, confidence, ai_called
FROM "decisions"
SORT file.ctime DESC
LIMIT 20
```

## Winning Trades
```dataview
TABLE symbol, action, pnl, pnl_percent
FROM "trades"
WHERE contains(tags, "winner")
SORT date DESC
LIMIT 10
```

## Community Map
```dataview
TABLE node_count, synced
FROM "communities"
SORT node_count DESC
LIMIT 30
```

---
_This note is auto-generated. Edit the Journal section below._

## Journal
