---
type: strategy
name: SMC Smart Money
updated: 2026-09-08
tags:
  - strategy
  - SMC-Smart-Money
---

# Strategy: SMC Smart Money

_No description yet._

## Signal Sources
_None documented_

## Related Communities
_None linked_

## Performance Notes
```dataview
TABLE symbol, action, pnl, date
FROM "decisions"
WHERE contains(tags, "SMC-Smart-Money")
SORT date DESC
LIMIT 20
```

## Journal
_Add strategy refinements here._
