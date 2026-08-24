# TradeBot Performance & Memory Overhaul

## Context

The app freezes the machine. Measured on this box (macOS, Apple Silicon, 16 GB RAM, 12 cores):
**swap at 20.3 GB used of 21.5 GB total, 225 M page-ins.** That is thrashing, not busy CPU.

Four root causes, all confirmed by reading the code:

1. **Sidecar sprawl that never gets cleaned up.** `start.py` launches 11 detached processes
   (Headroom, Obsidian, Postgres, Redis, OpenWA, MT5 bridge, vibe-trading, agentmemory, speech
   engine, uvicorn, `next dev`). `stop_all()` at `start.py:4543` kills only uvicorn + `next dev`
   + one Docker container. The other six are **orphaned on every restart**, and each launcher
   no-ops when it finds its port already open — so generations accumulate silently. On this
   16 GB machine the tier resolves to `high`, which means `speech_engine_local=True` and the
   MLX Parakeet-TDT + Qwen3-TTS models (1.7 GB on disk, 5.9 GB HF cache) load every run.

2. **Blocking the event loop.** `plugins/MT5TradingPlugin/backend/router.py:1381,1429,1467,1518`
   call `engine.analyze()` / `engine.backtest()` **synchronously inside `async def` handlers**.
   `smc_strategy.py:1586` runs ~540 full SMC re-derivations over 300-bar pure-Python windows per
   backtest. Nothing in the codebase offloads CPU work. So every CPU spike stalls all 11+
   background loops and every HTTP request at once — that is why it reads as a whole-app freeze.

3. **The resource tiering exists but the backend ignores it.** `start.py:_compute_settings()`
   (L297–434) already derives a good tier model, and `_apply_profile_override()` (L437) honours
   `TRADEBOT_PROFILE`. `start_backend()` injects `TRADEBOT_DB_POOL_SIZE`, `TRADEBOT_POLL_MULTIPLIER`,
   `TRADEBOT_ENABLE_CHARTS`. **`grep` across `backend/` returns zero hits for all of them.** None of
   the ~12 loop intervals in `config.py:231-279` scale with the machine.

4. **No visibility or control.** ~27 background loops (12 core + ~15 plugin) run with no unified
   registry. Plugin loops start *lazily on first HTTP request* to their router. There is no way to
   see what is running, what it costs, or to pause any of it.

5. **Telegram never starts on its own.** There is no startup hook — `plugins/TelegramSignalNewsPlugin/backend/router.py:125`
   appends `Depends(_ensure_monitor)` to the router, so the monitor only starts when a browser
   happens to hit a `/plugins/telegram/*` endpoint. Confirmed in `backend.log`: routers mounted at
   `00:40:03`, monitor started at `00:41:06` — **63 s later**, immediately after the first
   `GET /plugins/telegram/signals`. A headless `start.py` run leaves Telegram dormant indefinitely:
   no ingestion, no sniper, no bot commands. Additionally the connect UI
   (`frontend/src/pages/telegram.tsx:662`) fetches `auth/status` **once on mount with no polling**,
   so the "Connected" banner never appears on its own after connecting.

**Outcome wanted:** the app stops swapping the machine; every background task runs under one
managed, tier-aware supervisor; a System Monitor page shows CPU/memory per task and per sidecar
with pause controls; no feature silently dies; low-memory machines get a working profile.

**Decisions taken:** optional sidecars become opt-in below 32 GB RAM · control endpoints use
loopback-allow + `X-API-Key` off-localhost · full scope, with DB pooling landing last behind a
default-off flag.

---

## Phase 0 — Measure first

Nothing later is provable without a baseline.

- Extract the psutil block from `backend/app/api/jarvis.py:1967` into a new
  `backend/app/services/system_resources.py` (`host_snapshot()`, `process_snapshot()`,
  `service_tree(pid)`). Point `jarvis.py` at it so the JARVIS HUD and the new page can't drift.
- Add an **event-loop lag probe** — a 1 s task recording `actual_wake - expected_wake`, keeping
  p50/p95/max in a 5-min ring buffer. This is the decisive metric: expect p95 in *seconds* today
  while a backtest runs, target **< 50 ms** after Phase 2.
- Capture baseline to the scratchpad (10 min idle + 10 min under a backtest): `sysctl vm.swapusage`,
  `vm_stat` page-ins delta, `ps -A -o pid,rss,pcpu,comm | sort -rn -k2 | head -25`, total RSS of
  the TradeBot process set.

## Phase 1 — Stop the bleeding *(highest impact per effort; all in `start.py`)*

**1a. Sidecar registry + a `--stop` that actually stops.**
Add a `Sidecar(key, label, port, kind, min_tier, default_enabled, cmdline_marker, container)` table
near the port constants (L71–155), covering headroom/obsidian/postgres/redis/openwa/mt5rest/
vibe_trading/agentmemory/openmanus/speech_engine/backend/frontend. Every launcher
(`start_headroom_process` L1191, `start_openwa_gateway` L2994, `_start_vibe_trading_serve` L3479,
`_start_agentmemory_server` L4106, `ensure_speech_engine` L3611, `ensure_openmanus` L3824,
`start_backend` L4313, `start_frontend` L4471) writes `.tradebot/run/<key>.json`
`{pid, port, kind, started_at, cmdline_marker, container}`.

Rewrite `stop_all()` (L4543) to read that registry and reap everything, **reusing the existing
`_pid_alive()` and `_kill_pid_tree()` helpers**. Verify the recorded `cmdline_marker` still matches
`psutil.Process(pid).cmdline()` before every kill — guards against PID reuse after reboot. Kill whole
trees (`children(recursive=True)`): Next.js and OpenWA are multi-process. Fall back to port-owner
lookup for missing records; `docker stop` for `kind == "docker"`. **Drop the broad
`pkill -f "next dev"`** — it would kill an unrelated Next project. Flags: `--stop` reaps everything,
`--stop --keep-tools` preserves Headroom + Obsidian (inverting the hardcoded carve-out at L4571),
`--stop --hard` sweeps all known ports. Update `status()` (L4578) to list every sidecar.

**1b. Optional sidecars become opt-in below 32 GB.** Add `sidecar_enabled(key)` =
`TRADEBOT_ENABLE_<KEY>` env (explicit `0`/`1`) → else `tier >= SIDECAR_MIN_TIER[key]`. Wrap the
unconditional calls in `main()` (L4827, L4846–4874): `ensure_speech_engine`, `ensure_vibe_trading`,
`ensure_openhuman_deps`, `ensure_openmanus`, `_launch_obsidian`, `start_openwa_gateway`.
Raise the `speech_engine_local` RAM floor at `start.py:415` from 16 to **32 GB** — voice already
falls back to hosted NVIDIA/OpenAI in `backend/app/api/voice.py`, so nothing breaks.
`TRADEBOT_FORCE_SPEECH_ENGINE=1` (L520) stays as the escape hatch.
⚠️ `setup_integrations()` (L1331) currently **hard-exits** (`sys.exit(1)`, L4776) when Headroom or
Obsidian fail — that must become a warning when they are intentionally tier-disabled.

**1c. Kill the import-time model loads.** Remove the module-level warmup call at
`plugins/KronosForecastPlugin/backend/services/kronos_engine.py:428` (a daemon thread that loads a
102 M-param torch model + runs an MPS warmup inference *on import*). Expose `warmup()`, invoke on
first real forecast. Same for `backend/app/api/voice.py:447`. Make
`start.py:4264 warmup_kronos_and_openhuman()` tier-gated / `--warm` opt-in.

**1d. Reload, workers, and logs.** Default `--reload` **off** unless `TRADEBOT_RELOAD=1` (invert
L4407); at minimum drop `--reload-dir plugins` — today every plugin file save re-imports
torch/MLX/pandas and orphans the MT5 `auto_manage` OS thread.

⚠️ **`_workers` must be pinned to 1 at the same time.** `start.py:4409` sets
`_workers = _compute_settings()["backend_workers"]` = `min(4, physical//2)` on a ≥8-core/≥16 GB box,
and it is forced to 1 **only when `--reload` is on** (L4412). Turning reload off therefore silently
forks **4 uvicorn workers**, and every background loop in this app is an in-process singleton — that
would give 4 signal monitors, 4 Telegram bot-polling loops (duplicate replies to every command), 4
schedulers, and 4 processes contending on the single SQLite Telethon session file that
`telegram_provider.py:23 _SESSION_LOCK` can only serialise *within* one process. It would also
multiply RSS by 4 on the machine we're trying to unswap. **Set `backend_workers = 1`
unconditionally** until the loops move to the dedicated `app.workers.runner` process (which
`docker-compose.yml` already defines but `start.py` never launches). Leave a
`TRADEBOT_BACKEND_WORKERS` override for anyone running API-only.

`backend.log` is **73 MB**: note
`backend/app/core/logging.py` already rotates correctly, but that only covers `logs/tradebot.log` —
the 73 MB file is uvicorn stdout redirected at `start.py:4433` (and `frontend.log` at L4502). Rotate
there: if > 20 MB, rename to `.1`, keep 3 generations. Downgrade per-cycle "nothing happened" INFO
lines in `scheduler.py` to DEBUG.

## Phase 1.5 — Telegram starts with the app *(required, and a hard constraint on everything else)*

Telegram must be live and show its connect state as soon as `start.py` reports the service up —
not 63 seconds later when a browser happens to touch the right endpoint.

**1.5a. Start it in the lifespan.** Add an `AUTO_START_TELEGRAM_MONITOR` block to
`backend/app/main.py` alongside the existing `AUTO_START_*` blocks (L70–130), importing
`signal_monitor` and calling `ensure_started(AsyncSessionLocal)`. `ensure_started` is already
idempotent (`monitor_service.py:642`) and needs only a running event loop — no credentials, no DB
rows — so this is safe at boot. **Keep the router dependency at `router.py:125` as a harmless
fallback.** Under Phase 4 this becomes a registered supervisor task rather than a bespoke block.

**1.5b. Stop swallowing failures.** `_ensure_monitor` (`router.py:111-121`) is a bare
`try/except Exception: pass` — every startup failure is invisible today. Log at warning with the
exception, and record it on the supervisor's `last_error` so it surfaces on the monitor page. The
`except` must still never propagate: this is a **router-level dependency on every telegram request**,
so a raise would 500 the entire plugin.

**1.5c. Pin the Telethon session to an absolute path.** `telegram_provider.py:96` passes the bare
string `"tradebot_telegram"` to the Telethon client, which resolves it against `os.getcwd()`. The
live session is `backend/tradebot_telegram.session` (120 KB, current) purely because `start.py:4433`
launches uvicorn with `cwd=BACKEND_DIR`; a stale 28 KB copy sits at the repo root from an earlier
run. **Any change to how the process is launched silently swaps the session file and flips the UI
back to "Connect Telegram Account" with no error**, because `auth/status` (`router.py:269`) returns
`authenticated=False` on any exception. Resolve `session_name` to an absolute path under a single
data dir, in `_make_client()` (L96) and the three hard-coded `f"{session_name}.session"` checks at
L171, L196, L214. Migrate the existing `backend/` session to that path on first run, preferring the
newest file. This is a prerequisite for Phase 1's process changes, not an optional cleanup.

**1.5d. Make the connect UI self-updating.** `frontend/src/pages/telegram.tsx:219` fetches
`auth/status` once on mount (`useEffect` L446) and never again, so after connecting — or after the
backend restarts — the banner is stale until a manual refresh. Poll `auth/status` via the new
`useSmartPoll` hook (Phase 6) at ~15 s × `pollMultiplier()`, `document.hidden`-aware. Note the
server caches `get_account_info()` for 300 s (`telegram_provider.py:30 _ACCOUNT_INFO_TTL`) — the
auth mutation endpoints must invalidate that cache, or the UI will poll a stale "not connected" for
up to 5 minutes after a successful login. Surface bot state next to it: `BotControlPanel`
(`telegram.tsx:1391`) also loads `/bot/info` exactly once (L1450).

**1.5e. Show it in `start.py` and on the monitor page.** Add Telegram to the readiness summary
`start.py` prints, and to `status()` (L4578): monitor running / connected as `@handle` / bot polling
on-off. The monitor page gets the same as a first-class row.

⚠️ **Constraint on Phases 3 and 4:** the Telegram monitor and bot-polling loops are marked
`critical=True` — **never tier-disabled and never watchdog-paused**, on any machine. Tiering may
lengthen `MONITOR_INTERVAL_SECONDS` (60 s) and `POLL_INTERVAL_SECONDS` (300 s); it may not turn them
off. The bot-polling loop's **1 s** long-poll cadence (`monitor_service.py:809`) is the one worth
tuning — it should idle at 10 s when `TelegramBotConfig.polling_enabled` is false (which is the
default) rather than spinning, and the `getUpdates` long-poll timeout should carry the wait instead
of a tight sleep. Also note `provides.scheduled_jobs: ["telegram_poll_sources"]` in `plugin.json` is
**currently read by nothing** — `loader.py:_parse_manifest` ignores the key. Phase 4 makes it real.

## Phase 2 — Unblock the event loop

New `backend/app/core/offload.py`: a **shared bounded `ThreadPoolExecutor` + semaphore**.

Not `asyncio.to_thread` (uses the loop's default 16-thread executor — unbounded and shared, so 16
concurrent backtests mean 16 pandas working sets). Not `ProcessPoolExecutor` first: `SMCStrategyEngine`
carries DB-loaded `factor_weights` so everything must pickle, `fork()` on macOS with torch/MPS
resident in the parent is a known crash source, and +N interpreters of RSS is exactly what we're
fixing. The honest read: `analyze()` is heavy *pure Python*, so a thread pool won't help throughput —
but the goal is **loop responsiveness**, and the GIL releases every 5 ms, so the loop keeps running.

```python
async def run_cpu(fn, *args, name="cpu", heavy=False, timeout=None, **kw)
```
`heavy=True` acquires `_heavy_sem` (1 on low tier, 2 on high) so N simultaneous backtests queue
rather than spawn. Queue depth > 4 → `HTTPException(503, "backtest queue full")`, far better than a
90 s hang. Per-job `asyncio.wait_for` → 504, paired with a **cooperative cancel token** checked in
the `while` at `smc_strategy.py:1586` (threads can't be force-killed; this is a one-line change).
Report in-flight/queue-depth/cumulative-CPU to the supervisor as a pseudo-task.

Call sites in `plugins/MT5TradingPlugin/backend/router.py`: L1381 & L1467 →
`await run_cpu(engine.analyze, ..., name="smc.analyze")`; L1429 & L1518 →
`await run_cpu(engine.backtest, ..., name="smc.backtest", heavy=True, timeout=120)`.
Also add `le=1500` validation to `MT5BacktestRequest.count` (`schemas.py:442`).
Then sweep `grep -n "\.analyze(\|\.backtest(\|\.rolling("` across `backend/app/` and
`plugins/*/backend/`, filtered to `async def` bodies.

## Phase 3 — Tier-aware end to end

New `backend/app/core/resource_tier.py` as the single source of truth:
`TIER_ORDER`, `INTERVAL_MULTIPLIER = {minimal: 6.0, low: 4.0, medium: 2.0, high: 1.0, ultra: 1.0}`,
and `TASK_TIER_POLICY` (per task: `min_tier`, `autostart_tiers`, `critical`, `category`).

**Env contract** — in `start_backend()` (`start.py:4372`, beside the existing
`env.setdefault("TRADEBOT_DB_POOL_SIZE", …)`), add `TRADEBOT_TIER`, `TRADEBOT_PROFILE` (pass-through
so launcher and backend agree), `TRADEBOT_RAM_GB`, `TRADEBOT_PHYSICAL_CORES`, `TRADEBOT_SIDECARS`.
All via `setdefault` so `.env` still wins.

**Consumption** — in `backend/app/core/config.py`, add matching fields plus a
`@model_validator(mode="after") def apply_resource_tier(self)` ordered **before** `apply_desktop_mode`.
It resolves `PERF_TIER = TRADEBOT_PROFILE or TRADEBOT_TIER or "high"` (fall back *up* — never guess
low and silently disable features), multiplies the interval settings at L231–279, flips `AUTO_START_*`
off below each task's `min_tier`, and caps `SIGNAL_RESEARCH_CONCURRENCY` /
`JARVIS_LEARNING_MAX_SETTLE_PER_CYCLE` / price-tick symbol count.
**Only touches fields absent from `self.model_fields_set`**, so anything the user pinned in `.env`
survives. ⚠️ Verify that semantic against the installed pydantic-settings version; if env-sourced
fields don't land in `model_fields_set`, check `os.environ` directly instead.

Low-memory profile (indicative):

| Task | minimal | low | medium | high/ultra |
|---|---|---|---|---|
| scheduler signals/sentiment | 900/1800 | 600/1200 | 360/600 | 180/300 |
| price_tick | off | 15 s | 10 s | 5 s |
| signal_research_queue | off | off | 10/60 s, conc 1 | 2/30 s, conc 5 |
| research_loop / jarvis_learning | off | 3600 | 1800 | 900 |
| vault_sync | off (manual still works) | 1800 | 900 | 300 |
| pair_catalog refresh/full | 60 m/24 h | 30 m/12 h | 20 m/6 h | 15 m/6 h |
| sniper / pump | off | 600/900 | 300/600 | 60/120 |
| **position_monitor** | 1800 | 1800 | 1200 | 900 — **never off** |
| **live_auto_trade** | user setting only — **never tier-disabled** | | | 60 |
| MT5 scalp_bot / auto_manage | off | 30/180 | 20/120 | 10/60 |
| Paul subconscious | off | 1800 | 900 | 300 |
| **telegram monitor** tick/poll | 300/900 | 180/600 | 120/450 | 60/300 — **never off** |
| **telegram bot polling** | idle 10 s when disabled; long-poll when enabled — **never off** | | | |

**Safety carve-out:** `position_monitor`, `live_auto_trade`, `sniper`, `scalp_bot`,
`telegram_monitor` and `telegram_bot_polling` are `critical=True`. A paused position monitor with
open live positions is a financial risk, not a perf trade-off, and a paused Telegram monitor means
signals silently stop arriving. Tiering may slow them; it may never disable them, and the watchdog
may never touch them.

## Phase 4 — Unified task supervisor

**Adapter registry, not a rewrite.** The 12 loops in `scheduler.py` (54 KB) each already expose an
identical `start_X()` / `stop_X()` / `get_X_status()` triple returning `{running, started_at, last_run}`.
Wrapping them gets ~95 % of the value at ~5 % of the risk to requirement "nothing silently dies".

**Layer A — `backend/app/core/task_supervisor.py`**, zero changes to loop bodies:
`TaskSpec(id, name, category, description, source, default_interval_s, start, stop, status, critical,
autostart, min_tier)` and a `TaskSupervisor` with `register` / `bind` (late binding for lazy plugin
loops) / `start` / `stop` / `pause` / `resume` / `run_now` / `set_interval` / `claim` / `gate` /
`snapshot`. `interval(id)` = `user_override or (default × INTERVAL_MULTIPLIER[tier])`, clamped — this
is what makes the supervisor the single place intervals resolve.

**Layer B — ~5 mechanical lines per loop:**
```python
await supervisor.gate("research")            # cooperative pause point
async with supervisor.cycle("research"):     # times, counts, records errors + cpu_ms
    ...existing body unchanged...
await asyncio.sleep(supervisor.interval("research"))
```

**Three distinct stop levels** — this is what "pause without losing the task" means:
`paused` (task alive, `gate()` awaits an Event, state preserved, instant resume — the default UI
action) · `stopped` (calls `stop_X()`, frees the coroutine and its sessions) · interval override
(not a stop at all).

**Startup wiring:** rewrite `backend/app/workers/runtime.py` into the registration table and have
`start_background_workers()` iterate `supervisor.autostart_set()`. Then **delete
`backend/app/main.py:63-131`** — the six force-starts that currently defeat the `START_WORKERS_IN_API`
gate become per-task `autostart` properties.
⚠️ Acceptance gate: `GET /api/v1/tasks` before/after must show an **identical set of running tasks**
at `high` tier. Someone added those force-starts because features broke without them.

**Plugin registration — hybrid.** Declaratively, extend `provides.scheduled_jobs` in `plugin.json`
from strings to objects (`{id, name, category, default_interval_seconds, min_tier, autostart, module,
start, stop, status}`; parse bare strings as placeholders for back-compat). `PluginManifest` already
carries `provides` through `_parse_manifest` (`backend/app/plugins/loader.py:218`); add
`register_scheduled_jobs(supervisor)` called from the lifespan after `mount_routers`. That makes
plugin loops visible as `unbound` *before* they've ever started. At runtime, each plugin's
`ensure_started()` gains a guard — `if not supervisor.claim(id): return` then `supervisor.bind(...)` —
at `TelegramSignalNewsPlugin/backend/services/monitor_service.py:639`,
`AgentPaulPlugin/.../auto_fetch.py:35` + `subconscious.py:86`,
`MT5TradingPlugin/.../research_loop.py:601` + `signal_research.py:1645` + `scalp_bot_service.py:551`
+ `auto_manage_service.py:468`, `ObsidianKnowledgePlugin/.../sync_orchestrator.py:626`, WhatsApp.
⚠️ `_ensure_monitor` is a **router-level dependency on every telegram request**
(`TelegramSignalNewsPlugin/backend/router.py:125`) — `claim()` must never raise. Per Phase 1.5b the
bare `except: pass` becomes a logging `except` that still never propagates. And because Telegram is
`critical`, `claim("telegram.monitor")` must **always** return True regardless of tier — the guard
exists to bind and dedupe, not to gate. The MT5 `auto_manage` OS thread needs a `threading.Event`
gate, not the asyncio one.

**Paused state persists** to `data/task_state.json` (not the DB: must be readable before `init_db()`,
must survive the DB being down — exactly when you want loops paused). Store only deltas.
`TRADEBOT_TASK_STATE_RESET=1` escape hatch. Every `TaskInfo` carries
`paused_by: null|"user"|"tier"|"watchdog"`, and the page shows a persistent banner listing everything
paused — that's the "nothing dies silently" guard.

**Memory watchdog** — a `critical`, always-on 30 s supervisor task with hysteresis:
> 80 % mem or swap growing → halve effective tier at runtime, `evict_all()`, `gc.collect()`,
`torch.mps.empty_cache()`. > 90 % → auto-pause tasks in `research`/`learning`/`enrichment`
categories, tagged `paused_by="watchdog"`. < 70 % for 3 checks → restore. Never touches `critical`
tasks. Every action logged and surfaced.

## Phase 5 — System Monitor page

**Backend** — new `backend/app/api/tasks.py` and `backend/app/api/system.py`, registered in
`backend/app/api/routes.py` following the `monitoring_router` pattern:

```
GET   /api/v1/tasks                        -> {tier, profile, tasks[], generated_at}
GET   /api/v1/tasks/{id}                   -> TaskInfo + last 20 cycles
POST  /api/v1/tasks/{id}/pause|resume|stop|start|run-now
PATCH /api/v1/tasks/{id}                   -> {"interval_seconds": 600}
POST  /api/v1/tasks/pause-all?category=research
POST  /api/v1/tasks/preset/{battery_saver|balanced|full_power}
GET   /api/v1/system/resources             -> host + self + services[]
GET   /api/v1/system/caches
POST  /api/v1/system/services/{key}/stop
POST  /api/v1/system/profile?seconds=30    -> on-demand cProfile, top 30
GET   /api/v1/system/stream                -> SSE, 2 s
```

**Per-task attribution — measure what's measurable, label the rest.**
*CPU is real enough:* `time.thread_time()` deltas bracketing each cycle in `supervisor.cycle()`.
Since asyncio tasks share the loop thread this over-attributes work done during a task's `await`s,
so record **both** `wall_ms` and `cpu_ms` and surface `cpu_share_pct` explicitly as a *share*, not an
absolute. That answers "which loop is eating my CPU" without faking precision.
*Memory is not per-task measurable* — one heap, one process. Ship: process RSS over time (the graph
that matters), per-cycle `tracemalloc` deltas **behind a "Deep memory profiling" toggle** that
auto-disables after 5 min (it costs 2–3× on allocation), and a cheap always-on `gc.get_objects()`
count. Column header reads **"Alloc (sampled)"**, not "Memory". No invented numbers.
*No custom profiler* — `POST /system/profile` gives on-demand `cProfile` with zero steady-state cost.

**Sidecar breakdown** reads the **same `.tradebot/run/*.json` registry `start.py` writes** — one
source of truth. Per entry: `psutil.Process(pid)` + `children(recursive=True)`, summing RSS across
the tree (Next.js and OpenWA are multi-process; a single-PID number would be a lie). Docker →
`docker stats --no-stream --format json`, 3 s timeout, cached 10 s.

**Auth:** new `require_local_or_key` dependency in `backend/app/core/security.py` — allow
unauthenticated from loopback, require `X-API-Key` (new `TASKS_API_KEY` setting) otherwise, require
unconditionally when `ENVIRONMENT == "production"`. Leave the existing `verify_api_key` alone: it
compares against `SECRET_KEY`, which `config.py` **regenerates via `secrets.token_urlsafe(48)` on
every non-production boot**, so wiring it would break the UI on every restart. `GET`s stay open
(equivalent to the already-public `/jarvis/system-stats`).

**Frontend** — new `frontend/src/pages/system-monitor.tsx`; nav entry in the `navItems` array at
`frontend/src/components/Layout.tsx:62` (`icon: Activity`, above `/settings`); API methods appended
to the `apiClient` object in `frontend/src/services/api.ts`. Plain Tailwind matching house style
(`bg-gray-800/30 border border-gray-700 rounded-lg p-4`, `text-tradebot-accent`) — no component
library is used anywhere in this repo. Sections: stat strip (host CPU / host RAM with a red swap
sub-bar / backend RSS / active tasks, recharts sparklines off a 120-point client ring buffer) ·
processes table with per-row Stop · background-tasks table grouped by category with inline interval
editing and Pause/Resume/Run-now/Stop (critical tasks show a lock and confirm) · presets row ·
paused banner. Fed by SSE with a 5 s poll fallback, and **the page itself obeys `document.hidden`** —
a monitor page that burns CPU is a joke.

## Phase 6 — Targeted efficiency fixes

- **`pipeline.py` step 2.8** (`backend/app/signals/pipeline.py:1012-1035`) — hoist and reuse the
  OHLCV already fetched at L992 (this is the *sixth* redundant `get_ohlcv(limit=200)` for the same
  symbol/TF, × 14 pairs × every 180 s), and call
  `auto_fib_retracement(fib_df, levels=(0.5, 0.618), extend_lines=False)`. Add those kwargs to
  `backend/app/signals/technical.py:440` with defaults preserving current behaviour. Today it builds
  ~2000 per-bar dicts and reads only `["swing"]` and `["golden_zone"]`.
- **Shared TTL cache** — new `backend/app/core/cache.py` over `OrderedDict` (no new dep): bounded
  LRU + TTL plus a module-level `CACHES` registry. The registry is the point: it gives the monitor
  page a Caches table, the watchdog one `evict_all()` lever, and a cheap `cache_sweeper` task.
  Migrate the two holding big values first — `KronosForecastPlugin/.../forecast_service.py:826` and
  `MT5TradingPlugin/.../candle_feed.py:96` (candle *lists*, outer dict never pruned). The other
  eight (`api/market.py:40`, `exchanges/yahoo_provider.py:291`, `exchanges/metals_provider.py:59`,
  `services/market_data.py:116,121`, `services/pair_catalog.py:656`, `services/agent_reach_client.py:53`,
  `AiMarketAnalyst/.../market_data.py:13`, `AgentPaulPlugin/.../news_research.py:60`) are ~6 lines
  each — follow-ups, not blockers.
- **Unbounded queries** — `backend/app/api/agents.py:385` loads the whole `AgentDecision` table and
  aggregates in Python; replace with grouped SQL aggregates. `plugins/OpenHumanPlugin/backend/router.py:60`
  loads every row to `len()` it; use `select(func.count())`. Sweep
  `grep -rn "\.scalars()\.all()" backend/app plugins/*/backend | grep -v limit`.
- **Frontend polling** — new `frontend/src/hooks/useSmartPoll.ts` multiplying the base interval by
  `pollMultiplier()` (already implemented at `frontend/src/utils/devicePerformance.ts:201`), pausing
  on `document.hidden`, stopping entirely after 60 s hidden with refetch-on-show, and backing off on
  errors. Migrate the top offenders only: `jarvis-room.tsx` L1328 (**1.2 s** — raise the base to 3 s;
  sub-second REST is what the SSE `price_tick` stream exists for), L1181, L1123, L860, L1072;
  `mt5-live.tsx:742`; `MT5ScalpBotPanel.tsx:257`.
- **DB pooling — land this LAST, default off.** `backend/app/core/database.py:18-22`: drop
  `poolclass=NullPool`, add `pool_size=settings.DB_POOL_SIZE, max_overflow=4, pool_pre_ping=True,
  pool_recycle=1800`, fed from the already-injected `TRADEBOT_DB_POOL_SIZE`. **Keep `NullPool` for
  SQLite URLs** — aiosqlite + pooling is a file-lock footgun and the desktop build uses SQLite.
  ⚠️ NullPool currently *masks* sessions held across slow awaits — confirmed at
  `plugins/MT5TradingPlugin/backend/router.py:1391`, which opens `AsyncSessionLocal()` and awaits
  `ai_review(...)` (network I/O) inside it. Today that just opens another connection; with a pool it
  exhausts it. Audit every `async with AsyncSessionLocal()` block containing an AI/HTTP await first.
  Ship behind `TRADEBOT_DB_POOL_ENABLED=1`, **default off**, with pool-exhaustion logging.

---

## Verification

**Decisive metric — event-loop lag p95** (Phase 0 probe). Before: fire
`POST /api/v1/mt5/strategy/backtest` with `count=600` and watch p95 hit seconds while the whole app
hangs. After Phase 2: p95 stays **< 50 ms** and the 6th concurrent request gets a clean 503.
Load script: 5 concurrent backtests while polling `/api/v1/system/resources`.

**Restart hygiene** (proves the orphan fix): `python3 start.py` → `--stop` →
`lsof -nP -iTCP -sTCP:LISTEN | grep -E '8787|8790|8899|8900|2785|1448|3000|2886'` must be empty.
Repeat 3×; total system RSS must return to baseline each cycle, and `sysctl vm.swapusage` must stop
climbing.

**Tier test:** `TRADEBOT_PROFILE=minimal python3 start.py` on this box — assert via `GET /api/v1/tasks`
that the expected loops are stopped and intervals multiplied, that every page still renders, and
that **`telegram.monitor` and `telegram.bot_polling` are still running** (critical tasks survive the
lowest tier).

**Telegram startup test** (the explicit requirement — run with **no browser open**):
`python3 start.py`, then within ~10 s of the readiness banner, with zero page visits:
- `grep "Telegram signal monitor started" backend.log` must appear **before** any
  `GET /api/v1/plugins/telegram/*` line — today it appears 63 s after, immediately following one.
- `curl localhost:1448/api/v1/plugins/telegram/monitor/status` → running.
- `curl localhost:1448/api/v1/plugins/telegram/auth/status` → `authenticated: true` with the
  existing session, proving the absolute-path migration kept the 120 KB `backend/` session and did
  not silently fall back to the stale 28 KB root copy.
- `curl localhost:1448/api/v1/plugins/telegram/bot/info` → bot resolves.
- Then open `/telegram` and confirm the page shows the connected banner and bot state on first
  paint, and that it updates without a manual refresh after a disconnect/reconnect.
- Repeat the whole thing after `--stop` + restart, and once with `TRADEBOT_PROFILE=minimal`.
- `ps aux | grep "uvicorn app.main:app" | grep -v grep | wc -l` must be **1** — the worker-fork guard.

**Tests to add / run:**
- `backend/tests/test_task_supervisor.py` — pause/resume round-trip; a paused task does not execute
  its body; interval override applies; state-file round-trip; critical task refuses pause without
  `force`; unknown id → 404.
- `backend/tests/test_resource_tier.py` — table-driven per profile (mirrors `start.py --simulate`);
  asserts explicitly-set `.env` values are **not** overridden.
- `backend/tests/test_offload.py` — `heavy=True` serialises to `HEAVY_JOB_CONCURRENCY`; overflow →
  503; loop stays responsive (busy 1 s function + concurrent `asyncio.sleep(0.05)` finishes < 0.15 s).
- **Registry completeness test** (the "nothing silently dies" net) — every id in `TASK_TIER_POLICY`
  resolves to a registered adapter and vice-versa.
- `plugins/MT5TradingPlugin/tests/` (`pytest`, `conftest.py` present) — `test_smc_strategy.py`,
  `test_smc_scoring.py`, `test_signal_research.py` **must pass unchanged**; that is the proof
  `run_cpu` didn't alter results. Add `test_backtest_offload.py` asserting the awaited router result
  equals a direct `engine.backtest()`. Mirror the existing `test_scalp_pause_resume.py` for
  supervisor pause semantics.
- Frontend `vitest` — `system-monitor.test.tsx` (rows render from mocked `apiClient`; pause hits the
  right endpoint; banner appears) and `useSmartPoll.test.ts` (fake timers: scales with
  `NEXT_PUBLIC_POLL_MULTIPLIER`, stops on `document.hidden`). Re-run the full suite —
  `useDeepgramAgent` tests use fake timers and may be sensitive to interval changes.
- Playwright `frontend/e2e/system-monitor.spec.ts` — page loads, ≥1 task visible, pause toggles the
  state pill.
- `plugins/TelegramSignalNewsPlugin/tests/` — monitor autostarts from the lifespan with no HTTP
  request; `ensure_started` stays idempotent when both the lifespan and the router dependency call
  it; session path resolves absolutely and identically regardless of `os.getcwd()`;
  `telegram.monitor` cannot be paused by tier or watchdog.

## Risks

| Risk | Mitigation |
|---|---|
| **Disabling `--reload` forks 4 uvicorn workers** → 4 signal monitors, 4 Telegram bot loops replying twice to every command, 4× RSS, contention on one SQLite session | Pin `backend_workers = 1` in the same change (Phase 1d); assert with `pgrep -c` in the verification run |
| **Telethon session is cwd-relative** — any launch change silently reverts the UI to "Connect Telegram Account" with no error | Pin to an absolute path *before* touching process launch (Phase 1.5c); migrate the existing session preferring the newest file |
| Telegram tier-gated or watchdog-paused → signals stop silently | `critical=True`; `claim("telegram.*")` always returns True; explicit test at `TRADEBOT_PROFILE=minimal` |
| DB pooling exposes sessions held across slow awaits (confirmed: `MT5TradingPlugin/backend/router.py:1391`) | Land last, default off behind `TRADEBOT_DB_POOL_ENABLED=1`, audit first |
| Pausing a trading loop is a financial action | `position_monitor`/`live_auto_trade`/`sniper`/`scalp_bot` marked `critical`; watchdog never touches them; UI confirms |
| `--stop` killing the wrong PID after reboot | Verify recorded `cmdline_marker` before every kill; drop broad `pkill -f "next dev"` |
| Removing `main.py:63-131` force-starts silently disables a feature | Before/after `GET /api/v1/tasks` diff as the acceptance gate |
| `claim()` raising 500s every telegram endpoint (router-level dependency) | `claim()` never raises; preserve the existing `try/except: pass` |
| `model_fields_set` may not include env-sourced fields in the pinned pydantic-settings | Verify empirically; fall back to reading `os.environ` |
| Tier-gating Headroom/Obsidian hits the hard `sys.exit(1)` at `start.py:4776` | Convert to a warning when intentionally disabled |
