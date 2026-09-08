---
type: community
name: Telegram (c31)
node_count: 31
synced: 2026-09-08
tags:
  - community
  - Telegram-c31
---

            # Telegram (c31)

            > Auto-generated from graphify knowledge graph. Last synced: 2026-09-08.


            ## Key Nodes (31 total)
            - `models.py`
- `TelegramBase`
- `SourceKind`
- `TelegramChannelSource`
- `TelegramSubscribedCache`
- `TelegramIngestMessage`
- `TelegramPluginSettings`
- `SignalStatus`
- `TelegramParsedSignal`
- `SniperTradeStatus`
- `TelegramSniperSettings`
- `TelegramSniperTrade`
- `Telegram Signal & News Plugin SQLAlchemy models.`
- `Standalone base so plugin tables can be created independently.`
- `Cached snapshot of the user's subscribed Telegram channels.      Discovering cha`
- `Single-row settings table for Telegram plugin credentials.      One row (id=1) h`
- `Structured trading signal parsed from a Telegram channel message.      Created a`
- `Single-row settings for the Telegram sniper auto-trade engine.`
- `A sniper auto-trade derived from a parsed Telegram signal.      References the p`
- `monitor_service.py`
- `TelegramSignalMonitor`
- `.__init__()`
- `.is_running()`
- `.status()`
- `.ensure_started()`
- `.stop()`
- `Background monitor: polls enabled channels every minute and creates signals.`
- `Singleton background monitor. Idempotent start; safe to call repeatedly.`
- `Start the loop if not already running (called from a request handler).`
- `# IMPORTANT: only count TPs that are in the PROFIT direction from entry.`
- … and 1 more nodes


            ## Notes
            _Add human observations and links here._
