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


<!-- headroom:rtk-instructions -->
# RTK (Rust Token Killer) - Token-Optimized Commands

When running shell commands, **always prefix with `rtk`**. This reduces context
usage by 60-90% with zero behavior change. If rtk has no filter for a command,
it passes through unchanged — so it is always safe to use.

## Key Commands
```bash
# Git (59-80% savings)
rtk git status          rtk git diff            rtk git log

# Files & Search (60-75% savings)
rtk ls <path>           rtk read <file>         rtk grep <pattern>
rtk find <pattern>      rtk diff <file>

# Test (90-99% savings) — shows failures only
rtk pytest tests/       rtk cargo test          rtk test <cmd>

# Build & Lint (80-90% savings) — shows errors only
rtk tsc                 rtk lint                rtk cargo build
rtk prettier --check    rtk mypy                rtk ruff check

# Analysis (70-90% savings)
rtk err <cmd>           rtk log <file>          rtk json <file>
rtk summary <cmd>       rtk deps                rtk env

# GitHub (26-87% savings)
rtk gh pr view <n>      rtk gh run list         rtk gh issue list

# Infrastructure (85% savings)
rtk docker ps           rtk kubectl get         rtk docker logs <c>

# Package managers (70-90% savings)
rtk pip list            rtk pnpm install        rtk npm run <script>
```

## Rules
- In command chains, prefix each segment: `rtk git add . && rtk git commit -m "msg"`
- For debugging, use raw command without rtk prefix
- `rtk proxy <cmd>` runs command without filtering but tracks usage
<!-- /headroom:rtk-instructions -->
