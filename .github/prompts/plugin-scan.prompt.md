---
description: "Scan and inventory all installed tradebot plugins. Lists plugin manifests, routes, models, status, and integration points."
agent: "TradeBot Architect"
argument-hint: "Optional: specific plugin name to scan deeply"
---

Scan the tradebot plugin system and produce a structured inventory report.

## Steps

1. List all directories under `plugins/`
2. For each plugin, read `plugin.json` and report:
   - Name, version, slug, description
   - Provided routes, models, migrations
   - Permissions declared
   - Settings keys
   - Whether it has frontend pages/components
3. Check for any conflicts (duplicate table prefixes, overlapping route prefixes)
4. Check which plugins have tests and docs
5. Summarize integration points used (which core services each plugin connects to)

## Output Format

```
## Plugin Inventory

### {PluginName} v{version}
- **Status:** enabled/disabled
- **Routes:** {count} ({prefixes})
- **Models:** {count} ({table names})
- **Migrations:** {count} (latest: {name})
- **Frontend:** {pages count} pages, {components count} components
- **Tests:** {count} test files
- **Docs:** {present/missing}
- **Integrations:** {list of core services used}

### Conflicts
- {any detected conflicts or "None"}

### Missing
- {plugins referenced but not found, or "None"}
```
