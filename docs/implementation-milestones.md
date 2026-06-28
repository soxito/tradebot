## TradeBot Implementation Milestones

## Purpose
This document converts the current roadmap into a GitHub-issue style execution plan with milestone groupings, acceptance criteria, dependencies, and completion definitions.

## Milestone Overview
| Milestone | Theme | Status | Target Outcome |
| --- | --- | --- | --- |
| M1 | Architecture Hardening | Completed | API process is stable, workers are decoupled, plugin runtime is mountable |
| M2 | Strategy Lab Execution Core | Planned | Strategy versions can run end-to-end and persist measurable outcomes |
| M3 | Promotion Gates and Risk Controls | Planned | Promotion to simulation/live is policy-driven and auditable |
| M4 | Plugin Platform v1 | Planned | Plugins are installable, validated, and observable as first-class modules |

## Milestone M1 - Architecture Hardening (Completed)
### Scope
- Split worker runtime from API startup.
- Add manifest-driven plugin discovery and mount.
- Introduce Strategy Lab MVP entities and APIs.

### Completed Issue Set
- [x] TB-101: Add worker runtime orchestrator
  - Labels: backend, architecture, runtime
  - Acceptance Criteria:
    - Worker loops are started through a single runtime entrypoint.
    - Worker startup is controlled via config flags.
- [x] TB-102: Create dedicated worker process runner
  - Labels: backend, workers, ops
  - Acceptance Criteria:
    - Worker process can run independently from API service.
    - Graceful shutdown on SIGINT and SIGTERM.
- [x] TB-103: Add plugin manifest loader and router auto-mount
  - Labels: backend, plugins, extensibility
  - Acceptance Criteria:
    - Plugin discovery from plugin.json.
    - Router mount and plugin table initialization wired to app lifecycle.
- [x] TB-104: Add Strategy Lab MVP schema and routes
  - Labels: backend, strategy-lab, api
  - Acceptance Criteria:
    - Version/run/promotion models exist.
    - CRUD-style MVP endpoints available and registered.

### Definition of Done
- API boots without forcing worker loop startup.
- Worker process is independently deployable.
- Plugin mount behavior is configurable.
- Strategy Lab MVP endpoints are reachable.

## Milestone M2 - Strategy Lab Execution Core (Planned)
### Objective
Enable reproducible strategy execution runs with complete run lifecycle state transitions and result persistence.

### Issue Breakdown
- [ ] TB-201: Run execution service for strategy versions
  - Labels: backend, strategy-lab, execution
  - Dependencies: TB-104
  - Scope:
    - Introduce run executor service handling QUEUED -> RUNNING -> COMPLETED or FAILED.
    - Store deterministic metrics payloads per run mode.
  - Acceptance Criteria:
    - Runs transition through valid statuses only.
    - Failed runs contain structured error payloads.

- [ ] TB-202: Async run queue integration with worker process
  - Labels: backend, workers, queue
  - Dependencies: TB-101, TB-201
  - Scope:
    - Queue run jobs from API and consume in worker runtime.
    - Add idempotency key support for retries.
  - Acceptance Criteria:
    - Duplicate queue submissions do not create duplicate active runs.
    - Worker restarts can recover queued jobs safely.

- [ ] TB-203: Run artifacts and event timeline persistence
  - Labels: backend, strategy-lab, data
  - Dependencies: TB-201
  - Scope:
    - Add run events table and run artifacts table.
    - Persist intermediate checkpoints and final summary.
  - Acceptance Criteria:
    - Every run has traceable event timeline.
    - Artifacts can be fetched by run ID.

- [ ] TB-204: Strategy Lab run detail API expansion
  - Labels: api, strategy-lab
  - Dependencies: TB-203
  - Scope:
    - Add endpoints for run events, artifacts, and diagnostics.
    - Add pagination and filtering for run history.
  - Acceptance Criteria:
    - Consumers can reconstruct run history from API alone.

## Milestone M3 - Promotion Gates and Risk Controls (Planned)
### Objective
Make strategy promotion deterministic, reviewable, and policy-governed before any simulation or live activation.

### Issue Breakdown
- [ ] TB-301: Promotion gate policy engine
  - Labels: backend, strategy-lab, risk
  - Dependencies: TB-201, TB-203
  - Scope:
    - Introduce gate checks (min sample size, drawdown cap, win-rate floor, Sharpe threshold).
    - Persist gate verdict details.
  - Acceptance Criteria:
    - Promotion can only proceed if mandatory checks pass.
    - Gate failure reasons are machine-readable and human-readable.

- [ ] TB-302: Two-step approval workflow for live promotion
  - Labels: backend, governance, security
  - Dependencies: TB-301
  - Scope:
    - Require reviewer approval for live target promotions.
    - Capture approver identity, timestamp, and rationale.
  - Acceptance Criteria:
    - Live promotions are blocked without approval record.

- [ ] TB-303: Promotion rollback and supersession model
  - Labels: backend, strategy-lab, safety
  - Dependencies: TB-302
  - Scope:
    - Add rollback endpoint and automatic supersession metadata.
    - Ensure only one active live version per strategy scope.
  - Acceptance Criteria:
    - Rollback leaves immutable audit trail.
    - Previous active version can be restored safely.

- [ ] TB-304: Guardrails for live-run budget and exposure
  - Labels: trading, risk, backend
  - Dependencies: TB-303
  - Scope:
    - Enforce max concurrent live runs and per-strategy exposure limits.
    - Reject execution above configured limits.
  - Acceptance Criteria:
    - Guardrail violations fail fast before order placement.

## Milestone M4 - Plugin Platform v1 (Planned)
### Objective
Evolve plugin loading into a complete plugin platform with lifecycle management, health visibility, and policy controls.

### Issue Breakdown
- [ ] TB-401: Plugin registry schema and lifecycle metadata
  - Labels: plugins, backend, data
  - Dependencies: TB-103
  - Scope:
    - Persist plugin install state, version, capabilities, compatibility, and health status.
  - Acceptance Criteria:
    - Registry reflects mounted and failed plugins with reason codes.

- [ ] TB-402: Plugin capability contract and permission checks
  - Labels: plugins, security, architecture
  - Dependencies: TB-401
  - Scope:
    - Enforce declared permissions from plugin manifest.
    - Add startup checks for missing required capabilities.
  - Acceptance Criteria:
    - Unauthorized plugin actions are denied and logged.

- [ ] TB-403: Plugin health endpoint and diagnostics panel API
  - Labels: plugins, observability, api
  - Dependencies: TB-401
  - Scope:
    - Expose per-plugin load/mount status, last failure, and recovery guidance.
  - Acceptance Criteria:
    - Operator can identify plugin faults from API response only.

- [ ] TB-404: Plugin isolation hardening
  - Labels: plugins, runtime, hardening
  - Dependencies: TB-402
  - Scope:
    - Protect core API from plugin mount failures in strict and non-strict modes.
    - Add startup fallback behavior and degraded-mode reporting.
  - Acceptance Criteria:
    - Core app remains available when non-critical plugin fails.

## Suggested Epic Grouping for GitHub Projects
- Epic A: Strategy Lab Core (TB-201 to TB-204)
- Epic B: Promotion Governance (TB-301 to TB-304)
- Epic C: Plugin Platform (TB-401 to TB-404)

## Suggested Label Taxonomy
- Type: feature, chore, hardening, governance
- Domain: strategy-lab, plugins, workers, api, risk
- Priority: p0, p1, p2
- State: ready, blocked, needs-design

## Recommended Execution Order
1. TB-201 -> TB-202 -> TB-203 -> TB-204
2. TB-301 -> TB-302 -> TB-303 -> TB-304
3. TB-401 -> TB-402 -> TB-403 -> TB-404

## Milestone Exit Criteria
- M2 exits when run execution is automated and observable.
- M3 exits when promotion rules are enforced with immutable auditability.
- M4 exits when plugins are policy-checked and operationally diagnosable.
