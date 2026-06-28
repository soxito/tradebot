## Backend Target Architecture and DB Schema (Waves 1-3)

## Purpose
Define the backend target architecture for Waves 1-3 with a clear mapping between what is already implemented and what must be delivered next.

## Scope
- Runtime separation and lifecycle hardening.
- Strategy Lab execution and governance architecture.
- Plugin platform maturation.
- Relational schema evolution for strategy lifecycle and plugin operations.

## Current Baseline (Wave 1 Implemented)
### Runtime and Process Topology
- API service uses FastAPI lifespan startup/shutdown orchestration.
- Worker loops are centralized through a runtime orchestrator with startup flags.
- Dedicated worker process entrypoint exists for isolated loop execution.
- Docker compose includes separate backend and worker services.

### Plugin System Baseline
- Plugin discovery is manifest-driven using plugin.json.
- Plugin routers are auto-mounted through a loader.
- Plugin SQLAlchemy metadata can be initialized at startup.
- Strict and non-strict plugin mount behavior is configurable.

### Strategy Lab Baseline
- Strategy Lab entities exist:
  - strategy_lab_versions
  - strategy_lab_runs
  - strategy_lab_promotions
- Strategy Lab API exposes:
  - Version create/list/read/update
  - Run create/list/update
  - Promotion create/list

## Target Architecture by Wave
## Wave 1 - Foundation and Decoupling (Implemented)
### Goals
- Keep API startup side-effect controlled.
- Isolate background execution from request/response path.
- Introduce Strategy Lab domain skeleton and plugin bootstrap.

### Deliverables
- Startup flags to control worker autostart behavior.
- Dedicated worker runtime module and process runner.
- Plugin loader with auto-mount and table initialization.
- Strategy Lab MVP models and routes.

## Wave 2 - Strategy Lab Execution Core
### Goals
- Execute strategy versions through queued run lifecycle.
- Persist detailed run telemetry and artifacts.
- Expose run observability for frontend and operators.

### Target Components
- StrategyExecutionService
  - Consumes version configuration.
  - Creates and updates run lifecycle states.
  - Emits run events and artifacts.
- RunQueueAdapter
  - Enqueue/dequeue execution tasks.
  - Idempotency and retry support.
- RunTelemetryStore
  - Persists event timeline and execution diagnostics.

### API Surface Expansion
- GET /strategy-lab/runs/{run_id}/events
- GET /strategy-lab/runs/{run_id}/artifacts
- GET /strategy-lab/runs/{run_id}/diagnostics

## Wave 3 - Promotion Governance and Plugin Platform v1
### Goals
- Enforce policy-based promotion gates.
- Make promotions auditable and reversible.
- Elevate plugins to managed runtime units with health and permission controls.

### Target Components
- PromotionGateEngine
  - Evaluates run metrics against rule thresholds.
  - Produces pass/fail verdict objects.
- PromotionApprovalService
  - Two-step review workflow for live promotions.
  - Captures approver identity and reason.
- PluginRegistryService
  - Tracks install state, compatibility, permissions, and mount health.
- PluginHealthService
  - Exposes diagnostics and failure events for operations.

## Target Logical Architecture
### Control Plane
- FastAPI API routers for strategy, promotions, and plugin operations.
- AuthN/AuthZ checks for promotion and plugin admin operations.

### Execution Plane
- Dedicated worker process handling scheduled loops and strategy run jobs.
- Queue-backed task dispatch with retry and idempotency.

### Data Plane
- PostgreSQL as source of truth for strategy lifecycle and plugin metadata.
- Redis for queueing, caching, and transient coordination state.

### Plugin Plane
- Manifest discovery.
- Permission/capability validation.
- Managed mount lifecycle and health reporting.

### Observability Plane
- Structured logs.
- Prometheus metrics.
- Strategy run event timeline and plugin health endpoints.

## Relational Schema - Existing Tables
### strategy_lab_versions
| Column | Type | Notes |
| --- | --- | --- |
| id | int PK | version identifier |
| name | varchar(120) | indexed |
| description | text | optional |
| timeframe | varchar(20) | default 1h |
| pairs | text | JSON array payload |
| indicators | text | JSON array payload |
| parameters | text | JSON object payload |
| risk_constraints | text | JSON object payload |
| is_active | bool | default true |
| created_by | varchar(100) | optional |
| updated_by | varchar(100) | optional |
| created_at | datetime | default now |
| updated_at | datetime | auto update |

### strategy_lab_runs
| Column | Type | Notes |
| --- | --- | --- |
| id | int PK | run identifier |
| version_id | int | indexed |
| run_mode | varchar(30) | simulation or live |
| status | enum | queued/running/completed/failed |
| metrics | text | JSON object payload |
| notes | text | optional |
| started_at | datetime | default now |
| finished_at | datetime | optional |
| created_by | varchar(100) | optional |
| created_at | datetime | default now |
| updated_at | datetime | auto update |

### strategy_lab_promotions
| Column | Type | Notes |
| --- | --- | --- |
| id | int PK | promotion identifier |
| version_id | int | indexed |
| target | enum | simulation or live |
| approved_by | varchar(100) | optional |
| reason | text | optional |
| metadata_json | text | JSON object payload |
| created_at | datetime | default now |

## Relational Schema - Planned Additions (Wave 2)
### strategy_lab_run_events
| Column | Type | Notes |
| --- | --- | --- |
| id | bigint PK | event identifier |
| run_id | int FK -> strategy_lab_runs.id | indexed |
| event_type | varchar(50) | queued, started, checkpoint, completed, failed |
| event_payload | json/text | structured details |
| event_at | datetime | indexed |

### strategy_lab_run_artifacts
| Column | Type | Notes |
| --- | --- | --- |
| id | bigint PK | artifact identifier |
| run_id | int FK -> strategy_lab_runs.id | indexed |
| artifact_type | varchar(50) | equity_curve, trades, diagnostics, report |
| storage_uri | varchar(500) | pointer to object/blob/file |
| metadata_json | json/text | optional metadata |
| created_at | datetime | default now |

### strategy_lab_run_errors
| Column | Type | Notes |
| --- | --- | --- |
| id | bigint PK | error identifier |
| run_id | int FK -> strategy_lab_runs.id | indexed |
| error_code | varchar(80) | normalized failure code |
| error_message | text | human-readable failure |
| error_context | json/text | structured context |
| created_at | datetime | default now |

## Relational Schema - Planned Additions (Wave 3)
### strategy_lab_gate_results
| Column | Type | Notes |
| --- | --- | --- |
| id | bigint PK | gate result identifier |
| run_id | int FK -> strategy_lab_runs.id | indexed |
| gate_name | varchar(80) | sample_size, drawdown, sharpe, win_rate |
| passed | bool | gate verdict |
| score | float | optional numeric score |
| threshold | float | configured threshold |
| details_json | json/text | supporting evidence |
| evaluated_at | datetime | default now |

### strategy_lab_approvals
| Column | Type | Notes |
| --- | --- | --- |
| id | bigint PK | approval identifier |
| promotion_id | int FK -> strategy_lab_promotions.id | indexed |
| reviewer_id | varchar(100) | approver identity |
| decision | varchar(20) | approved or rejected |
| comment | text | optional reviewer rationale |
| reviewed_at | datetime | default now |

### plugin_registry
| Column | Type | Notes |
| --- | --- | --- |
| id | bigint PK | plugin registry identifier |
| slug | varchar(120) | unique, indexed |
| name | varchar(200) | display name |
| version | varchar(50) | installed version |
| state | varchar(30) | discovered, mounted, failed, disabled |
| permissions_json | json/text | declared permissions |
| capabilities_json | json/text | declared capabilities |
| compatibility_json | json/text | app/runtime compatibility |
| last_error | text | optional latest mount/init error |
| updated_at | datetime | auto update |

### plugin_health_events
| Column | Type | Notes |
| --- | --- | --- |
| id | bigint PK | plugin health event identifier |
| plugin_slug | varchar(120) | indexed |
| severity | varchar(20) | info, warning, error |
| event_code | varchar(80) | normalized health code |
| payload_json | json/text | diagnostic payload |
| created_at | datetime | indexed |

## Data Integrity and Indexing Recommendations
- Add FK constraints for all run/promotion dependent tables.
- Add composite index on strategy_lab_runs(version_id, created_at desc).
- Add composite index on strategy_lab_gate_results(run_id, gate_name).
- Add unique key on plugin_registry.slug.
- Add retention policy for high-volume event tables.

## Backward Compatibility Strategy
- Keep current Strategy Lab endpoints unchanged for MVP clients.
- Add new routes as additive extensions.
- Introduce schema migrations in non-breaking order:
  1. Create new tables.
  2. Backfill if needed.
  3. Activate new API responses.

## Non-Functional Targets
- Run state transition consistency under retries.
- Observable failures with structured diagnostics.
- Plugin failure isolation from core API uptime.
- Promotion auditability with immutable event trail.

## Delivery Checkpoints
- End of Wave 2:
  - Run queue + event + artifact persistence live.
  - Run diagnostics API available.
- End of Wave 3:
  - Promotion gate checks enforced.
  - Approval workflow active for live promotions.
  - Plugin registry and health telemetry available.
