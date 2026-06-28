# Telegram Signal & News Plugin

Standalone plugin that ingests Telegram channels as configurable `signals` or `news` sources.

## API Base

`/api/v1/plugins/telegram`

## Endpoints

- `GET /status`
- `GET /channels?user_id=0&source_kind=signals|news`
- `POST /channels`
- `PATCH /channels/{source_id}`
- `DELETE /channels/{source_id}`
- `POST /channels/{source_id}/preview`
- `POST /poll`
- `GET /messages?user_id=0&source_kind=signals|news&channel_source_id={id}&limit=50`
- `GET /presets?source_kind=signals|news`
- `POST /presets`
- `PATCH /presets/{preset_id}`
- `DELETE /presets/{preset_id}`
- `POST /presets/{preset_id}/apply`

## Environment Variables

- `TELEGRAM_API_ID`
- `TELEGRAM_API_HASH`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_MCP_CHAT_ID` (chat id used by telegram-mcp)
- `TELEGRAM_MCP_SERVER_URL` (default: `https://telegram-mcp.furkankucuk.net`)
- `TELEGRAM_MCP_TIMEOUT_SECONDS` (default: `20`)
- `TELEGRAM_PLUGIN_POLL_LIMIT` (default: `50`)
- `TELEGRAM_PLUGIN_POLL_INTERVAL_SECONDS` (default: `300`)
- `TELEGRAM_PLUGIN_ENABLE_LLM_FALLBACK` (default: `false`)
- `TELEGRAM_PLUGIN_LLM_MODEL` (default: `gpt-4o-mini`)
- `TELEGRAM_PLUGIN_LLM_TIMEOUT_SECONDS` (default: `20`)
- `OPENAI_API_KEY` (used only when LLM fallback enabled)

## Provider Behavior

Provider selection per channel:
- `telethon`: MTProto read access (`telethon` must be installed).
- `bot_api`: Uses Bot API (`getChat`, `getUpdates`) and requires bot token and proper channel access.
- `telegram_mcp`: Pulls messages from a telegram-mcp server (`/api/messages`) using `TELEGRAM_MCP_CHAT_ID`.
- `auto`: Tries available providers in order and falls back automatically.

## Extraction Strategy

1. Rules-first extraction for direction, symbols, and levels.
2. Optional OpenAI fallback when confidence is low.
3. Server-side schema validation for all extraction results.

## Data Model Prefix

All plugin tables use the `telegram_` prefix and have no foreign keys to core tables.
