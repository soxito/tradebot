# Obsidian Vault Setup — TradeBot Integration

## Quick Start

1. **Download Obsidian** from https://obsidian.md (free)
2. **Open this directory as a vault** in Obsidian:
   - Click "Open folder as vault"
   - Select this folder (`~/obsidian-vault/tradebot`)
3. **Install community plugins** (Settings → Community plugins):
   - [Dataview](https://github.com/blacksmithgu/obsidian-dataview) — query notes like a database
   - [Templater](https://github.com/SilentVoid13/Templater) — auto-fill note templates
   - [Local REST API](https://github.com/coddingtonbear/obsidian-local-rest-api) — for live sync
   - [Obsidian Git](https://github.com/denolehov/obsidian-git) — auto-commit to git
4. **Enable Local REST API**:
   - Copy the API token from plugin settings
   - Add to your `.env`: `OBSIDIAN_REST_TOKEN=<token>`
   - The plugin listens on `https://localhost:27124` (self-signed cert)
5. **First sync** — run from TradeBot UI or call:
   ```
   POST http://localhost:8000/api/v1/plugins/obsidian-knowledge/sync
   ```
6. **Explore the vault** in Obsidian's graph view (Ctrl+G / Cmd+G)

## Environment Variables

Add to `backend/.env`:

```env
OBSIDIAN_VAULT_PATH=~/obsidian-vault/tradebot
OBSIDIAN_REST_URL=https://localhost:27124
OBSIDIAN_REST_TOKEN=<paste token from Obsidian Local REST API plugin>
OBSIDIAN_AUTO_SYNC_MINUTES=15
OBSIDIAN_EXPORT_DECISIONS=true
OBSIDIAN_EXPORT_SIGNALS=true
OBSIDIAN_EXPORT_COMMUNITIES=true
OBSIDIAN_INJECT_CONTEXT=false   # set true to enrich agent prompts with vault notes
```

## Vault Structure

```
obsidian-vault/tradebot/
├─ _index.md           ← Dashboard with Dataview queries
├─ _daily/             ← Auto daily journals (one per day)
├─ signals/            ← One note per trading signal
├─ decisions/          ← One note per agent decision
├─ strategies/         ← Strategy reference notes
├─ communities/        ← Graphify code communities (176 notes)
└─ trades/             ← Closed trade outcome notes
```

## TradeBot UI

Navigate to **Intelligence → Vault** (in the sidebar) to:
- Browse all vault notes
- Full-text search
- Trigger sync
- Click any node in the Brain graph to see linked vault notes

## Dataview Examples

Show today's signals:
```dataview
TABLE symbol, action, confidence
FROM "signals"
WHERE contains(file.name, date(today))
SORT confidence DESC
```

Show agent performance by symbol:
```dataview
TABLE symbol, count(rows) AS decisions, round(average(confidence), 2) AS avg_confidence
FROM "decisions"
GROUP BY symbol
SORT count(rows) DESC
```

Show community map:
```dataview
TABLE node_count
FROM "communities"
SORT node_count DESC
LIMIT 20
```
