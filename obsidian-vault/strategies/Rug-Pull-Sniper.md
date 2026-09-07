---
type: strategy
name: Rug Pull Sniper
updated: 2026-09-07
tags:
  - strategy
  - Rug-Pull-Sniper
---

# Strategy: Rug Pull Sniper

_No description yet._

## Signal Sources
_None documented_

## Related Communities
_None linked_

## Performance Notes
```dataview
TABLE symbol, action, pnl, date
FROM "decisions"
WHERE contains(tags, "Rug-Pull-Sniper")
SORT date DESC
LIMIT 20
```

## Journal
_Add strategy refinements here._
