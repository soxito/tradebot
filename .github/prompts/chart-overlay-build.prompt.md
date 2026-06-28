---
description: "Add or update TradingView chart overlays for plugins: order markers, SL/TP lines, position annotations, heatmap panels, equity curves."
agent: "TradeBot Architect"
argument-hint: "Describe the chart overlay to add (e.g., 'live MT5 order markers with SL/TP lines')"
---

Build chart overlay functionality for a tradebot plugin.

## Context

Use the `/tradebot-plugin-builder` skill for architecture rules. Existing chart: `frontend/src/components/TradingViewChart.tsx` (Lightweight Charts 4.1).

## Overlay Data Format

TradingView Lightweight Charts markers:
```json
{ "time": "2024-01-01", "position": "aboveBar", "color": "#2196F3", "shape": "circle", "text": "BUY" }
```

Price lines:
```json
{ "price": 2345.67, "color": "#4CAF50", "lineWidth": 1, "lineStyle": 2, "title": "TP 2345.67" }
```

## Performance Rules

- Delta updates only (add/remove/modify individual markers)
- Never call `setData()` on live charts — use `update()` for new candles
- Cap visible markers at 200 with "load more"
- Overlays are separate layers — never re-render indicator series
- Lazy-load heavy overlay data
- Throttle updates to max 1/second for non-critical data

## Steps

1. Create API endpoint that returns overlay data
2. Add overlay data fetching hook in plugin frontend
3. Register overlay layer with chart component
4. Handle live updates (polling or SSE)
5. Add toggle controls (Orders / Positions / SL-TP / Executions)
