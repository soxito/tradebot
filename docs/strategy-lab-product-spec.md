## Strategy Lab Product Specification

## Document Status
- Type: Product specification
- Scope: Strategy lifecycle from design to promotion
- Alignment: Current backend MVP plus Waves 2-3 expansion

## Product Vision
Strategy Lab is the controlled strategy lifecycle layer for TradeBot. It enables teams to:
- Define strategy versions with explicit parameters and risk constraints.
- Execute and evaluate runs in simulation or live-prep contexts.
- Promote only validated strategies through auditable governance.

## Problem Statement
Current trading systems often combine strategy definition, execution, and production rollout without traceable controls. This creates:
- Poor reproducibility of results.
- Unclear ownership of strategy changes.
- Risky promotion to live trading.

Strategy Lab solves this by adding versioned strategy artifacts, run history, and promotion governance.

## Goals
### Primary Goals
- Make strategy changes versioned and reviewable.
- Make run outcomes measurable and comparable.
- Make promotion to live explicit, auditable, and policy-driven.

### Secondary Goals
- Improve operator confidence before live deployment.
- Enable faster iteration with safer rollback paths.
- Prepare the platform for multi-strategy portfolio management.

## Non-Goals
- Building a full visual strategy builder in this phase.
- Supporting external third-party strategy marketplaces in this phase.
- Replacing existing signal/trading engines immediately.

## Users and Personas
### Quant Developer
- Needs to define and iterate strategy versions quickly.
- Needs deterministic comparison of runs and metrics.

### Risk Manager
- Needs confidence that promotion gates are enforced.
- Needs approval workflow and full audit trail.

### Trading Operator
- Needs clear state of what is active, pending, and promoted.
- Needs diagnostics for failed runs.

### Platform Admin
- Needs visibility into plugin capabilities and health.
- Needs operational control without manual code patching.

## Jobs To Be Done
- When I change strategy parameters, I want a new version so I can compare it against prior versions.
- When I run a strategy, I want persistent metrics and diagnostics so I can decide if it is promotable.
- When I promote to live, I want policy checks and approvals so production risk is controlled.

## Functional Scope by Wave
## Wave 1 (Current MVP)
### Included
- Strategy version creation and update.
- Strategy run creation and status updates.
- Promotion record creation with metadata.
- API routes for versions/runs/promotions.

### Current Endpoints
- POST /strategy-lab/versions
- GET /strategy-lab/versions
- GET /strategy-lab/versions/{id}
- PATCH /strategy-lab/versions/{id}
- POST /strategy-lab/runs
- GET /strategy-lab/runs
- PATCH /strategy-lab/runs/{id}/status
- POST /strategy-lab/promotions
- GET /strategy-lab/promotions

## Wave 2 (Execution Core)
### Included
- Queue-driven run execution.
- Run event timelines.
- Run artifacts and diagnostics persistence.
- Replay-ready run metadata.

### New Endpoints (Planned)
- GET /strategy-lab/runs/{id}/events
- GET /strategy-lab/runs/{id}/artifacts
- GET /strategy-lab/runs/{id}/diagnostics
- POST /strategy-lab/runs/{id}/retry

## Wave 3 (Governance and Promotion Controls)
### Included
- Gate engine for policy checks.
- Reviewer approvals for live promotions.
- Promotion decision history and evidence.
- Plugin registry integration for strategy runtime capabilities.

### New Endpoints (Planned)
- GET /strategy-lab/runs/{id}/gates
- POST /strategy-lab/promotions/{id}/approve
- POST /strategy-lab/promotions/{id}/reject
- GET /strategy-lab/promotions/{id}/approvals

## Core Domain Objects
### Strategy Version
- Identity: id, name
- Definition: timeframe, pairs, indicators, parameters
- Controls: risk_constraints, is_active
- Audit: created_by, updated_by, timestamps

### Strategy Run
- Identity: id, version_id
- Mode: simulation or live
- Lifecycle: queued, running, completed, failed
- Outcome: metrics, notes, started/finished timestamps

### Strategy Promotion
- Identity: id, version_id
- Target: simulation or live
- Governance: approved_by, reason
- Evidence: metadata_json, timestamps

## Lifecycle Workflows
## Workflow 1: Version Iteration
1. User creates new version from a strategy hypothesis.
2. User adjusts parameters/risk constraints.
3. Version is marked active for run eligibility.

## Workflow 2: Run Evaluation
1. User starts run from version.
2. System transitions run status through lifecycle.
3. Metrics and diagnostics are persisted.
4. User compares results against thresholds.

## Workflow 3: Promotion
1. User submits promotable version with run evidence.
2. Gate engine evaluates policy thresholds.
3. Reviewer approves or rejects.
4. Promotion action and rationale are logged.

## UX Expectations
### Strategy Versions View
- Table with filters (name, timeframe, active state, updated date).
- Inline compare action to inspect parameter differences.

### Runs View
- Real-time status chips.
- Metrics summary cards.
- Drill-down to event timeline and diagnostics.

### Promotions View
- Pending/approved/rejected tabs.
- Evidence bundle linked to run metrics and gate outcomes.

## Data and Analytics Requirements
### Required Metrics
- Sharpe ratio
- Max drawdown
- Win rate
- Profit factor
- Total return
- Number of trades

### Comparison Features
- Compare current run against:
  - Previous run of same version
  - Best historical run in same mode
  - Promotion gate thresholds

## Security and Governance
- Promotion-to-live requires privileged role.
- Approval/rejection must capture actor and timestamp.
- All state-changing operations are audit logged.
- Production secrets and webhook safety are enforced at startup.

## Non-Functional Requirements
### Reliability
- Run state transitions must be idempotent.
- Failed worker operations must be retryable with bounded policies.

### Observability
- Each run exposes timeline events and diagnostics.
- Promotion decisions expose gate verdict traceability.

### Performance
- List endpoints support pagination and filtering.
- Run details should load in sub-second range for recent runs.

### Operability
- Worker execution isolated from API request path.
- Plugin failures must not crash core API startup in non-strict mode.

## Success Metrics
### Product Metrics
- Percent of live promotions with complete gate evidence.
- Reduction in live strategy rollback incidents.
- Mean time from version creation to validated promotion.

### Operational Metrics
- Run failure rate by strategy and mode.
- Retry recovery success rate.
- Average run completion latency.

## Rollout Plan
## Phase A
- Adopt current Wave 1 endpoints in frontend with clear status UX.

## Phase B
- Introduce Wave 2 run events/artifacts and diagnostics pages.

## Phase C
- Enable Wave 3 gate checks and approval workflows for live target.

## Risks and Mitigations
### Risk: Run execution drift from version config
- Mitigation: store immutable version snapshot reference at run start.

### Risk: Promotion bypass through manual actions
- Mitigation: enforce gate checks server-side and role-based approval endpoints.

### Risk: High event volume impacts DB performance
- Mitigation: retention policies and indexing for event tables.

## Open Questions
- Which gate thresholds are globally fixed vs strategy-class specific?
- Should live promotions require one or two independent approvers?
- Should run artifacts be stored in DB vs object storage pointers?

## Acceptance Criteria
- Strategy versions can be created, listed, and updated through API.
- Runs can be started and status-updated with persisted metrics.
- Promotions are recorded with reason and actor metadata.
- For Wave 2+, run diagnostics and artifact retrieval are available.
- For Wave 3+, live promotions cannot complete without gate and approval flow.
