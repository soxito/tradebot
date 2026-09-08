---
type: community
name: Position & Trading (c35)
node_count: 30
synced: 2026-09-08
tags:
  - community
  - Position-Trading-c35
---

            # Position & Trading (c35)

            > Auto-generated from graphify knowledge graph. Last synced: 2026-09-08.


            ## Key Nodes (30 total)
            - `live.py`
- `_utcnow()`
- `LiveTradeEngine`
- `.get_or_create_settings()`
- `.get_settings_snapshot()`
- `.update_settings()`
- `._bitget_symbol()`
- `._hold_side_for_order()`
- `._extract_position_amount()`
- `._extract_position_price()`
- `._extract_position_margin()`
- `._extract_available_margin()`
- `._sum_available_margin()`
- `.check_stop_loss_take_profit()`
- `.backfill_sl_tp()`
- `.run_profit_runner()`
- `.backfill_exchange_positions_sl_tp()`
- `.backfill_open_orders_sl_tp()`
- `.auto_trade_cycle()`
- `.execute_signal()`
- `Live Auto-Trade Engine Reads pending signals from the DB and places real orders`
- `Naive SAST datetime for DB compatibility.`
- `Engine that reads pending signals and executes live orders.`
- `Extract tradable futures margin from a Bitget account payload.         Prefer op`
- `Find live positions tracked in the Trade DB that have no SL or TP,         calcu`
- `For positions whose unrealised profit exceeds *tp_threshold_pct*:           1. C`
- `Find exchange positions that have no TPSL plan orders, calculate         smart S`
- `Fetch open (pending) limit orders from Bitget, identify those without         pr`
- `One live auto-trade cycle:         1. Check safety gates (ENABLE_AUTO_TRADING, i`
- `Execute a single signal on live, using the same logic as auto_trade_cycle:`


            ## Notes
            _Add human observations and links here._
