---
type: community
name: Pump Monitor (c56)
node_count: 23
synced: 2026-09-14
tags:
  - community
  - Pump-Monitor-c56
---

            # Pump Monitor (c56)

            > Auto-generated from graphify knowledge graph. Last synced: 2026-09-14.


            ## Key Nodes (23 total)
            - `trigger_cycle()`
- `run_once()`
- `Manually trigger a full pump monitor cycle (scan + signal creation).`
- `Run one pump monitor cycle manually.`
- `pump_detector.py`
- `_get_exchange_tradeable_symbols()`
- `_fetch_coingecko_markets()`
- `_fetch_trending_coins()`
- `_extract_btc_context()`
- `_score_pump_potential()`
- `scan_for_pumps()`
- `_create_pump_signal()`
- `PumpToken`
- `run_pump_monitor_cycle()`
- `Pre-Pump Detector v2 — Deep Analysis Engine  Major improvements over v1:  - BTC`
- `Get set of symbols tradeable on the configured exchange (e.g. {'BTC', 'ETH', ...`
- `Fetch coins from CoinGecko sorted by volume for pump detection.      Now fetches`
- `Fetch trending coin IDs from CoinGecko search/trending endpoint.`
- `Extract BTC, ETH market data for context.      Returns dict with BTC/ETH 1h%, 24`
- `Deep multi-factor pump scoring with 8 indicators.      Returns None if coin shou`
- `Scan CoinGecko for tokens showing pre-pump signals.      v2: Extracts BTC market`
- `Create a BUY signal for a confirmed pre-pump token.      Signal confidence is de`
- `Full pump monitor cycle:     1. Scan for new pre-pump tokens (with BTC context)`


            ## Notes
            _Add human observations and links here._
