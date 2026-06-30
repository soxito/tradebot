# Obsidian Brain Integration Plan

> **Goal**: Enhance the existing **Intelligence Brain** (`/intelligence`) with a live-linked
> Obsidian knowledge vault so agent decisions, signals, strategies, and codebase community
> maps are navigable in Obsidian AND visible as enriched context inside the TradeBot UI.

---

## 1. Current Brain Architecture

```
intelligence.tsx (front-end)
  └─ force-directed graph of graphify-out/graph.json
       3 515 nodes · 7 123 edges · 304 communities
       Node types: code modules, classes, functions, DB models
       Persistence: node positions in localStorage (brain.positions.v2)

backend/app/agents/
  ├─ memory.py        ← AgentDecision lookups + learning prompts
  ├─ orchestrator.py  ← 4-phase pipeline, calls OpenAI
  └─ specialists.py   ← Market / Sentiment / Signal / Risk / Executor agents

graphify-out/
  ├─ graph.json       ← live knowledge graph (3 515 nodes)
  ├─ GRAPH_REPORT.md  ← community hubs (readable)
  └─ cache/           ← AST + semantic embeddings
```

**Missing today**: There is no human-readable, editable, linked knowledge layer.
Agent decisions live only in the DB. Strategy rationale, market observations, and
research notes have no persistent home. Obsidian fills this gap.

---

## 2. What Obsidian Brings

| Feature | Benefit for TradeBot |
|---|---|
| Markdown vault with wikilinks `[[...]]` | Creates a human-readable web of notes mirroring graphify edges |
| Graph view | Second, human-curated graph alongside the code graph |
| Canvas | Infinite whiteboard for strategy brainstorming |
| Dataview plugin | Query trade notes like a database: `TABLE symbol, pnl WHERE type="Signal"` |
| Templater plugin | Auto-fill daily decision journals |
| REST API (community plugin) | Machine-readable bridge between TradeBot backend and vault |
| Local vault, open `.md` files | Zero lock-in; data survives beyond TradeBot itself |

---

## 3. Plugin Architecture

The integration follows the **existing plugin pattern** (same as `AiMarketAnalyst`, `AgentPaulPlugin`).

```
plugins/ObsidianKnowledgePlugin/
├─ plugin.json              ← manifest (slug, permissions, settings_keys)
├─ __init__.py
├─ backend/
│   ├─ __init__.py
│   ├─ models.py            ← VaultNote (SQLAlchemy)
│   ├─ schemas.py           ← Pydantic request/response models
│   ├─ router.py            ← FastAPI routes mounted under /api/v1/obsidian
│   ├─ config.py            ← OBSIDIAN_VAULT_PATH, OBSIDIAN_REST_URL
│   └─ services/
│       ├─ vault_writer.py  ← writes .md files into the vault
│       ├─ vault_reader.py  ← reads vault for agent context injection
│       ├─ obsidian_rest.py ← optional bridge to obsidian-local-rest-api
│       └─ sync_scheduler.py ← APScheduler job for periodic sync
├─ docs/
│   └─ SETUP.md
└─ tests/
    └─ test_vault_writer.py
```

### `plugin.json`

```json
{
  "name": "Obsidian Knowledge Vault",
  "slug": "obsidian-knowledge",
  "version": "1.0.0",
  "description": "Bi-directional sync between the TradeBot Brain and an Obsidian markdown vault. Exports agent decisions, signals, and graphify communities as wikilinked notes. Injects vault context into agent prompts.",
  "author": "TradeBot",
  "requires": { "python": ">=3.12", "tradebot": ">=0.1.0" },
  "service_provider": "backend.router",
  "provides": {
    "routes": ["backend/router.py"],
    "models": ["backend/models.py"],
    "migrations": ["backend/migrations/"],
    "pages": ["frontend/pages/"],
    "overlays": false,
    "scheduled_jobs": ["sync_vault_daily"]
  },
  "permissions": [
    "obsidian.vault.read",
    "obsidian.vault.write",
    "obsidian.decisions.export",
    "obsidian.agents.context"
  ],
  "settings_keys": [
    "obsidian.vault_path",
    "obsidian.rest_url",
    "obsidian.rest_token",
    "obsidian.auto_sync_interval_minutes",
    "obsidian.export_decisions",
    "obsidian.export_signals",
    "obsidian.export_graphify_communities",
    "obsidian.inject_context_into_agents"
  ]
}
```

---

## 4. Implementation Phases

### Phase 1 — Vault Foundation (2–3 days)

**Goal**: Write a well-structured Obsidian vault from the existing data.

#### 4.1 Vault Directory Structure

```
~/obsidian-vault/tradebot/       (configured via OBSIDIAN_VAULT_PATH)
│
├─ _index.md                     ← Dashboard note (Dataview queries)
├─ _daily/
│   └─ 2026-06-29.md             ← Daily journal (one per day)
│
├─ signals/
│   └─ BTC-USDT/
│       ├─ 2026-06-29-signal-001.md
│       └─ ...
│
├─ decisions/
│   └─ 2026-06-29-agent-market-BTC.md
│
├─ strategies/
│   ├─ SMC-Smart-Money.md
│   ├─ Rug-Pull-Sniper.md
│   └─ Pump-Monitor.md
│
├─ communities/                  ← Mirrored from graphify GRAPH_REPORT.md
│   ├─ Agent-Orchestration.md
│   ├─ Signal-Sniper-Service.md
│   ├─ LLM-Gateway-AI-Analyst.md
│   └─ ...  (one per graphify community)
│
└─ assets/
    └─ graph-snapshot.png
```

#### 4.2 `VaultWriter` Service

```python
# backend/services/vault_writer.py (pseudocode)

class VaultWriter:
    def __init__(self, vault_path: Path):
        self.vault_path = vault_path

    # ── Signal note ──────────────────────────────────────────
    async def write_signal_note(self, signal: Signal) -> Path:
        """
        Creates: signals/{symbol}/{date}-signal-{id}.md
        Frontmatter: symbol, action, confidence, source, timestamp
        Body: technical rationale, indicators, links to related notes
        """

    # ── Agent decision note ──────────────────────────────────
    async def write_decision_note(self, decision: AgentDecision) -> Path:
        """
        Creates: decisions/{date}-{agent}-{symbol}.md
        Frontmatter: agent_role, symbol, action, confidence, ai_called
        Body: reasoning text, market context snapshot, [[wikilinks]] to signal + strategy
        """

    # ── Daily journal ────────────────────────────────────────
    async def write_daily_note(self, date: date) -> Path:
        """
        Creates/updates: _daily/{date}.md
        Uses Dataview queries to auto-aggregate day's decisions + signals
        """

    # ── Graphify community note ──────────────────────────────
    async def write_community_note(self, community: GraphCommunity) -> Path:
        """
        Creates: communities/{community-name}.md
        Lists all nodes in the community with links, key files, purpose
        Mirrors the GRAPH_REPORT.md community structure
        """
```

#### 4.3 Markdown Note Format

```markdown
---
type: signal
symbol: BTC/USDT
action: buy
confidence: 0.82
source: smc_pivot
timestamp: 2026-06-29T14:30:00+02:00
strategy: "[[strategies/SMC-Smart-Money]]"
tags: [signal, BTC, long]
---

# Signal: BTC/USDT — BUY @ 2026-06-29 14:30 SAST

## Summary
**Action**: `BUY` | **Confidence**: 82% | **Source**: SMC Pivot

## Technical Context
- RSI: 42.3 (oversold)
- EMA 50/200 cross: bullish
- Order block: strong support @ $61 200

## Related
- [[decisions/2026-06-29-market-agent-BTC]] ← Market Agent decision
- [[strategies/SMC-Smart-Money]] ← Strategy used
- [[_daily/2026-06-29]] ← Today's journal
```

---

### Phase 2 — Backend API (2 days)

**Goal**: REST endpoints the frontend (and Obsidian REST plugin) can call.

#### Endpoints

```
GET  /api/v1/obsidian/status          → vault path, file count, last sync
GET  /api/v1/obsidian/notes           → paginated list of vault notes
GET  /api/v1/obsidian/notes/{path}    → read a single note (markdown)
POST /api/v1/obsidian/notes           → create/update a note
POST /api/v1/obsidian/sync            → trigger full re-sync now
GET  /api/v1/obsidian/graph           → vault graph as JSON (nodes=notes, edges=wikilinks)
POST /api/v1/obsidian/search          → full-text search across vault
GET  /api/v1/obsidian/context/{symbol} → collate recent notes for agent injection
```

#### `VaultNote` DB Model

```python
class VaultNote(Base):
    __tablename__ = "obsidian_vault_notes"

    id           = Column(Integer, primary_key=True)
    path         = Column(String, unique=True, nullable=False)  # relative to vault
    note_type    = Column(String)        # signal | decision | strategy | community | daily
    source_id    = Column(String)        # FK to Signal.id / AgentDecision.id etc.
    source_table = Column(String)        # "signals" | "agent_decisions" etc.
    symbol       = Column(String, index=True, nullable=True)
    tags         = Column(JSON, default=list)
    created_at   = Column(DateTime, default=func.now())
    updated_at   = Column(DateTime, onupdate=func.now())
    checksum     = Column(String)        # SHA-256 of file content for dirty-check
```

---

### Phase 3 — Obsidian REST Bridge (1 day)

**Goal**: When `obsidian-local-rest-api` plugin is running in Obsidian, enable
real-time, bi-directional sync (notes appear live in Obsidian as they are created).

#### Setup (one-time, optional)

1. Install [obsidian-local-rest-api](https://github.com/coddingtonbear/obsidian-local-rest-api) in Obsidian
2. Copy the API token into `.env`: `OBSIDIAN_REST_TOKEN=<token>`
3. Set `OBSIDIAN_REST_URL=https://localhost:27124`

#### `ObsidianRestBridge` Service

```python
class ObsidianRestBridge:
    """
    Thin wrapper around the obsidian-local-rest-api HTTP API.
    Falls back to direct file writes if Obsidian is not running.
    """
    async def push_note(self, path: str, content: str) -> bool: ...
    async def pull_note(self, path: str) -> str | None: ...
    async def list_modified(self, since: datetime) -> list[str]: ...
    async def search_vault(self, query: str) -> list[dict]: ...
    async def open_note(self, path: str) -> None:
        """Tell Obsidian to open this note in the app."""
```

#### Sync Strategy

```
TradeBot (new decision)
    │
    ▼
VaultWriter.write_decision_note()   ← always writes to disk
    │
    ├─ If OBSIDIAN_REST_URL set AND bridge reachable:
    │      ObsidianRestBridge.push_note()   ← Obsidian sees it live
    │
    └─ Scheduled job every N minutes:
           pull changes from Obsidian back
           (user edited a note → update DB tag/annotation)
```

---

### Phase 4 — Intelligence Page Enhancement (3 days)

**Goal**: Enhance the existing brain map UI with an Obsidian vault panel.

#### 4.1 New Tab: "Vault Graph"

Add a second tab to `intelligence.tsx` alongside the existing graphify brain:

```
┌──────────────────────────────────────────────────────────────────┐
│  Intelligence                                    [ Code Brain ] [ Vault Graph ] │
├──────────────────────────────────────────────────────────────────┤
│  [Vault Graph tab selected]                                      │
│                                                                  │
│  ┌────────────────────────────────┐   ┌──────────────────────┐  │
│  │  Vault Force Graph             │   │  Note Detail Panel   │  │
│  │                                │   │  ─────────────────   │  │
│  │  Nodes = vault notes           │   │  Title: BTC Signal   │  │
│  │  Edges = [[wikilinks]]         │   │  Type: signal        │  │
│  │  Color by type:                │   │  Symbol: BTC/USDT    │  │
│  │    🟠 signal                   │   │  Action: BUY 82%     │  │
│  │    🟣 decision                 │   │                      │  │
│  │    🟢 strategy                 │   │  [Open in Obsidian]  │  │
│  │    🔵 community               │   │  [Edit Note]         │  │
│  │                                │   │  [View Markdown]     │  │
│  └────────────────────────────────┘   └──────────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
```

#### 4.2 Code Brain Enhancements

When the user clicks a node in the **existing** graphify brain:
- Sidebar shows existing node info (community, file, edges)
- **NEW**: "📝 Linked Notes" section lists related vault notes
- **NEW**: "Create Note" button opens a modal pre-filled with the node's info

```typescript
// Additions to intelligence.tsx (right-panel section)

function NodeVaultPanel({ node }: { node: GraphNode }) {
  const [notes, setNotes] = useState<VaultNote[]>([])

  useEffect(() => {
    apiClient.get(`/api/v1/obsidian/notes?source=${node.id}`)
      .then(r => setNotes(r.data.notes))
  }, [node.id])

  return (
    <div className="mt-4 border-t border-white/10 pt-4">
      <h4 className="text-xs font-semibold text-white/40 uppercase mb-2">
        📝 Vault Notes
      </h4>
      {notes.map(n => (
        <NoteChip key={n.id} note={n} />
      ))}
      <CreateNoteButton nodeId={node.id} nodeLabel={node.label} />
    </div>
  )
}
```

#### 4.3 New Page: `/vault`

A lightweight knowledge-base browser page:

```
/vault
  ├─ Search bar (full-text across all notes)
  ├─ Filter: type (signal | decision | strategy | daily)
  ├─ Filter: symbol, date range
  ├─ Note list (sorted by updated_at desc)
  └─ Note detail panel (render markdown)
```

---

### Phase 5 — Agent Memory Enhancement (2 days)

**Goal**: Agents read vault notes as additional context, improving decision quality.

#### 5.1 `VaultReader` Service

```python
class VaultReader:
    async def get_context_for_symbol(
        self,
        symbol: str,
        limit: int = 5,
    ) -> str:
        """
        Returns a markdown string of recent vault notes for the symbol:
        - Latest signal notes (last 3)
        - Latest decision notes (last 3)
        - Strategy note for the active strategy

        This string is injected into agent prompts alongside the
        existing memory_prompt from agents/memory.py.
        """

    async def get_strategy_context(self, strategy_name: str) -> str:
        """Read strategies/{strategy_name}.md for agent context."""

    async def search_notes(self, query: str, limit: int = 5) -> list[str]:
        """BM25/grep search across vault for agent reasoning support."""
```

#### 5.2 Injection into `AgentOrchestrator`

```python
# In orchestrator.py — _build_context() method

async def _build_context(self, symbol: str, ...) -> dict:
    context = await self._gather_market_context(symbol, ...)

    # ── Existing memory ──────────────────────────────────
    memory_prompt = await build_memory_prompt(db, agent_role, symbol)

    # ── NEW: Obsidian vault context ──────────────────────
    if settings.OBSIDIAN_INJECT_CONTEXT:
        vault_context = await vault_reader.get_context_for_symbol(symbol)
        if vault_context:
            memory_prompt += f"\n\n## Vault Knowledge\n{vault_context}"

    context["memory_prompt"] = memory_prompt
    return context
```

#### 5.3 Auto-Export Successful Strategies

When a `Trade` closes with `pnl > 0`:

```python
# In trading/service.py — after closing a position

if trade.pnl > 0 and settings.OBSIDIAN_AUTO_EXPORT:
    await vault_writer.write_strategy_outcome_note(trade, agent_decisions)
```

---

## 5. Graphify → Obsidian Community Mirror

The graphify report lists **176 named communities** and 128 thin ones. This is the
richest knowledge structure in the project. The sync creates one note per community:

```markdown
---
type: community
community_id: 22
name: Agent Orchestration
node_count: 15
---

# Agent Orchestration

> Auto-generated from graphify graph. Last synced: 2026-06-29.

## Key Nodes
- [[backend/app/agents/orchestrator.py|AgentOrchestrator]]
- [[backend/app/agents/memory.py|get_past_decisions]]
- [[backend/app/agents/specialists.py|agent_from_db]]

## Community Purpose
Coordinates multiple AI agents for trade decisions. Runs the 4-phase pipeline:
Market → Sentiment → Signal → Risk → Executor.

## Related Communities
- [[communities/Base-AI-Agent-Framework]] ← parent framework
- [[communities/AI-Agent-Models]] ← data models
- [[communities/AI-Agent-Management-API]] ← REST API
```

This makes the codebase navigable in Obsidian's graph view alongside trade notes.

---

## 6. Settings Keys Added to `config.py`

```python
# In backend/app/core/config.py — class Settings

OBSIDIAN_VAULT_PATH: str = "~/obsidian-vault/tradebot"
OBSIDIAN_REST_URL: str = ""          # e.g. https://localhost:27124
OBSIDIAN_REST_TOKEN: str = ""
OBSIDIAN_AUTO_SYNC_MINUTES: int = 15
OBSIDIAN_EXPORT_DECISIONS: bool = True
OBSIDIAN_EXPORT_SIGNALS: bool = True
OBSIDIAN_EXPORT_COMMUNITIES: bool = True
OBSIDIAN_INJECT_CONTEXT: bool = False  # cautious default
```

---

## 7. Obsidian Vault Setup (one-time)

Create `obsidian-vault/.obsidian/` config with recommended plugins and settings:

```json
// obsidian-vault/.obsidian/community-plugins.json
["dataview", "templater-obsidian", "obsidian-local-rest-api", "obsidian-git"]
```

```json
// obsidian-vault/.obsidian/core-plugins.json  (enable graph view, search)
{ "graph": true, "global-search": true, "file-explorer": true, "canvas": true }
```

Provide a `SETUP.md` guide:

```markdown
# Obsidian Vault Setup

1. Download Obsidian from https://obsidian.md
2. Open vault at: ~/obsidian-vault/tradebot (or path in .env)
3. Install community plugins: Dataview, Templater, Local REST API, Obsidian Git
4. Copy REST API token to .env: OBSIDIAN_REST_TOKEN=<token>
5. Run first sync: POST /api/v1/obsidian/sync
6. Open intelligence.tsx → "Vault Graph" tab
```

---

## 8. File Delivery Sequence

| Order | File | Description |
|---|---|---|
| 1 | `plugins/ObsidianKnowledgePlugin/plugin.json` | Manifest |
| 2 | `plugins/ObsidianKnowledgePlugin/backend/config.py` | Settings keys |
| 3 | `plugins/ObsidianKnowledgePlugin/backend/models.py` | VaultNote model |
| 4 | `plugins/ObsidianKnowledgePlugin/backend/schemas.py` | Pydantic schemas |
| 5 | `plugins/ObsidianKnowledgePlugin/backend/services/vault_writer.py` | Core writer |
| 6 | `plugins/ObsidianKnowledgePlugin/backend/services/vault_reader.py` | Context reader |
| 7 | `plugins/ObsidianKnowledgePlugin/backend/services/obsidian_rest.py` | REST bridge |
| 8 | `plugins/ObsidianKnowledgePlugin/backend/services/sync_scheduler.py` | APScheduler job |
| 9 | `plugins/ObsidianKnowledgePlugin/backend/router.py` | FastAPI routes |
| 10 | `backend/app/core/config.py` | Add OBSIDIAN_* settings |
| 11 | `backend/app/agents/orchestrator.py` | Inject vault context |
| 12 | `frontend/src/pages/vault.tsx` | Vault browser page |
| 13 | `frontend/src/pages/intelligence.tsx` | Add vault panel + tab |
| 14 | `frontend/src/components/VaultNoteCard.tsx` | Reusable note card |
| 15 | `frontend/src/services/api.ts` | Add obsidian API calls |
| 16 | `obsidian-vault/SETUP.md` | User setup guide |
| 17 | `obsidian-vault/.obsidian/community-plugins.json` | Plugin config |

---

## 9. Risk & Mitigations

| Risk | Mitigation |
|---|---|
| Vault path doesn't exist or is wrong | `VaultWriter` auto-creates directories; health check endpoint warns |
| Obsidian REST not running | All REST calls are optional; file writes always happen first |
| Too many files (3 515 nodes) | Community notes only (176); individual node notes only on demand |
| Agent prompt too long with vault context | `vault_reader` uses BM25 relevance ranking + token budget cap |
| Disk usage | Only text `.md` files; negligible unless exporting every tick |
| Note overwrites user edits | Checksum-based dirty check; user edits always win |

---

## 10. Success Metrics

- [ ] Every new signal generates a vault note within 2 seconds
- [ ] Every agent decision generates a vault note within 2 seconds
- [ ] All 176 graphify communities have a corresponding `.md` note
- [ ] `GET /api/v1/obsidian/graph` returns a navigable note graph
- [ ] Intelligence page Vault Graph tab renders notes as linked nodes
- [ ] Agent prompts optionally include the last 5 relevant vault notes
- [ ] Obsidian opens and shows the vault with correct graph view links
- [ ] Daily note auto-generated at midnight SAST with Dataview summary

---

## 11. GOAP Execution Plan (Ordered Actions)

Following the Goal-Oriented Action Planning methodology:

```
STATE NOW:
  graphify_brain_exists = true
  obsidian_integration = false
  vault_on_disk = false
  agents_read_vault = false

GOAL STATE:
  vault_on_disk = true
  decisions_exported = true
  signals_exported = true
  communities_mirrored = true
  agents_read_vault = true
  ui_shows_vault_graph = true

ACTIONS (ordered by dependency):

  A1: create_plugin_scaffold           (cost: 1)  → vault_plugin_exists
  A2: write_vault_writer_service       (cost: 2)  → vault_writer_ready
      requires: vault_plugin_exists
  A3: write_vault_reader_service       (cost: 1)  → vault_reader_ready
      requires: vault_plugin_exists
  A4: write_obsidian_rest_bridge       (cost: 1)  → rest_bridge_ready
      requires: vault_plugin_exists
  A5: write_router_and_schemas         (cost: 2)  → api_endpoints_live
      requires: vault_writer_ready, vault_reader_ready
  A6: add_config_settings              (cost: 1)  → settings_ready
  A7: add_db_migration                 (cost: 1)  → vault_note_table_exists
      requires: vault_plugin_exists
  A8: sync_graphify_communities        (cost: 2)  → communities_mirrored
      requires: vault_writer_ready, settings_ready
  A9: patch_orchestrator_context       (cost: 1)  → agents_read_vault
      requires: vault_reader_ready, settings_ready
  A10: build_vault_browser_page        (cost: 2)  → vault_page_exists
      requires: api_endpoints_live
  A11: enhance_intelligence_page       (cost: 3)  → ui_shows_vault_graph
      requires: api_endpoints_live
  A12: write_obsidian_vault_config     (cost: 1)  → obsidian_ready_to_open
      requires: communities_mirrored
  A13: write_setup_docs                (cost: 1)  → documentation_complete

OPTIMAL PATH: A1→A6→A7→A2→A3→A4→A5→A8→A9→A10→A11→A12→A13
ESTIMATED TOTAL: ~4–5 focused dev days
```

---

*Generated: 2026-06-29 | Author: Claude goal-planner (GOAP mode)*
