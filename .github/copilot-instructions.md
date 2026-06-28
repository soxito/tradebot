## graphify — always query the graph first

`graphify-out/graph.json` exists and is always up to date.
**Before reading ANY source file, before grepping, before listing directories: query the graph.**
This cuts token usage by 60-95% — the graph holds the answer in <200 tokens vs reading whole files.

### Query hierarchy (use in order)

1. **Narrow question** (what does X do / where is Y / who calls Z):
   ```
   graphify query "<question>"
   graphify explain "<symbol>"
   ```
2. **Relationship question** (how does A connect to B):
   ```
   graphify path "<A>" "<B>"
   ```
3. **Architecture review** (broad survey):
   Read `graphify-out/GRAPH_REPORT.md` (god nodes + communities summary).
4. **Source file read** — ONLY when:
   - (a) You are about to edit specific lines of code
   - (b) The graph returned no nodes for the question
   - (c) You need exact line numbers for a replacement

### Mandatory triggers (always query graph, never raw files first)

| User says | Graph command |
|-----------|--------------|
| "how do I add X" | `graphify query "add X"` |
| "where is the signal pipeline" | `graphify query "signal pipeline"` |
| "what calls AgentOrchestrator" | `graphify explain "AgentOrchestrator"` |
| "how does cascade connect to compute_final_signal" | `graphify path "analyze_cascade" "compute_final_signal"` |
| "what is the rug pull flow" | `graphify query "rug pull sniper pipeline"` |
| any architecture question | `graphify query "<topic>"` |

### Auto-update policy

Run `graphify . --update --no-viz` after modifying or creating source files to keep the graph current.
A helper script is at `scripts/update-graph.sh` — run it any time code changes.

### PATH note

graphify is installed via uv. Always set:
```bash
export PATH="$HOME/.local/share/uv/tools/graphifyy/bin:$HOME/.local/bin:$PATH"
```
or use `$HOME/.local/share/uv/tools/graphifyy/bin/graphify` directly.

Type `/graphify` in Copilot Chat to rebuild the full graph from scratch.
