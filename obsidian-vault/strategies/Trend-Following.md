---
type: strategy
name: Trend Following
updated: 2026-09-14
tags:
  - strategy
  - Trend-Following
---

# Strategy: Trend Following

_No description yet._

## Signal Sources
_None documented_

## Related Communities
_None linked_

## Performance Notes
```dataview
TABLE symbol, action, pnl, date
FROM "decisions"
WHERE contains(tags, "Trend-Following")
SORT date DESC
LIMIT 20
```

## Journal
_Add strategy refinements here._
