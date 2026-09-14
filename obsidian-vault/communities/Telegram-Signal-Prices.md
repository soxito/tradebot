---
type: community
name: Telegram Signal Prices
node_count: 40
synced: 2026-09-14
tags:
  - community
  - Telegram-Signal-Prices
---

            # Telegram Signal Prices

            > Auto-generated from graphify knowledge graph. Last synced: 2026-09-14.


            ## Key Nodes (40 total)
            - `TelegramNewsSentiment`
- `Tracks which NEWS ingest messages have been scored and pushed into the     core`
- `rebuild_signals()`
- `get_prices_endpoint()`
- `process_outcome_endpoint()`
- `Re-scan stored messages and (re)create signals. Idempotent.`
- `Current live prices for active-signal symbols (for the UI).`
- `Manually process a signal outcome / close message (for testing or webhooks).`
- `forex_price_service.py`
- `_fetch_rates()`
- `_parse_pair()`
- `is_forex_pair()`
- `get_forex_price()`
- `Live Forex price fetcher (no API key required).  Uses the free, CDN-hosted curre`
- `Fetch (and cache) the latest rates from the free currency API.`
- `Split 'EURUSD' → ('EUR', 'USD').  Returns None if not a Forex pair.`
- `Return True when the symbol looks like a Forex pair (e.g. EURUSD, XAUUSD).`
- `Return the current mid-price for *symbol* (e.g. 'EURUSD', 'XAUUSD', 'GBPJPY').`
- `_signal_dedupe_hash()`
- `create_signals_from_messages()`
- `reconcile_active_signals_from_live_price()`
- `run_monitor_cycle()`
- `._loop()`
- `Scan recent ingest messages, create entry signals, and apply outcomes.      Idem`
- `Update trailing stop-loss and promote ACTIVE signals to TP_HIT / SL_HIT.      Tr`
- `One monitor tick.      When `do_poll` is True: poll enabled channels from Telegr`
- `news_sentiment_service.py`
- `_base_symbol()`
- `_get_tradeable_bases()`
- `process_news_to_sentiment()`
- … and 10 more nodes


            ## Notes
            _Add human observations and links here._
