# PRD: Stack Run Service MVP (Azure)

## Summary

Provide the Stack Run Service: a cloud execution wrapper around the deterministic underwriting engine so non-technical colleagues can run scenarios without local Python, while preserving a complete, auditable run history.

This PRD covers only the "single run" service (not the Phase 3 exploration engine).

## Goals

- Accept canonical deal inputs and produce deterministic outputs using the existing engine.
- Persist run artifacts for auditability and reproducibility.
- Enable listing and retrieval of prior runs by deal.
- Support a path from "single user / internal" to "team / SSO" without changing engine logic.

## Non-goals (MVP)

- Implement waterfall logic.
- Implement probabilistic modeling or Monte Carlo.
- Build an investor-facing web dashboard.
- Perfect Excel UX (button/add-in) in the same weekend as backend stabilization.

## Users

- Primary: internal underwriting analysts who prefer Excel.
- Secondary: partners/leaders reviewing outputs and run provenance.
- Future: LPs/lenders consuming investor-grade artifacts (Phase 2+).

## Functional Requirements

### FR1: Create run (async)

Endpoint: `POST /api/runs`

- Accept a canonical deal payload (JSON).
- If `metadata.run_id` is missing/blank, assign a server-generated `run_id`.
- Validate with the engine validator.
- Persist artifacts:
  - `inputs.json` (final inputs used)
  - `validation.json` (PASS/FAIL + issues)
- Enqueue a job for execution.
- Return `202 Accepted` with `{deal_id, run_id, status}`.

### FR2: Execute run (worker)

- Read stored `inputs.json`.
- Run `engine.engine.run_underwriting(inputs)`.
- Persist:
  - `outputs.json`
  - `metadata.json` (timestamps, versions, identity if available)
- Update run status in an index record.

### FR3: Get run status/result

Endpoint: `GET /api/runs/{run_id}`

- Return status and artifact locations.
- Optionally include small "headline metrics" inline (not full cashflow tables).

### FR4: List runs by deal

Endpoint: `GET /api/deals/{deal_id}/runs?top=50`

- Return recent runs for a deal with status and key metadata.

## Security Requirements

- MVP: function keys acceptable for initial testing.
- Team-ready: Entra ID auth required (EasyAuth), with group or app-role authorization.
- Store no shared secrets in Excel workbooks for team usage.

## Data & Artifacts

Artifacts per run:
- `inputs.json`
- `outputs.json`
- `validation.json`
- `metadata.json`

Storage layout:
- Blob path: `stack-runs/runs/{deal_id}/{run_id}/...`
- Index row: Table Storage with `PartitionKey=deal_id`, `RowKey=run_id`

## Reliability & Observability

- Worker retries should not corrupt artifacts; idempotency by `run_id`.
- Log correlation via `run_id`.
- Application Insights enabled for API and worker.

## Acceptance Criteria (MVP)

- Posting a known-good example deal produces a completed run with all artifacts in Blob.
- Invalid inputs return `validation.json` issues and do not execute.
- Listing runs shows the newly created run and its status.
- Re-running the stored `inputs.json` reproduces the same `outputs.json` (engine determinism).

## References

- `docs/azure_architecture.md`
- `docs/underwriting_engine_prd.md`
- `docs/output_contract_v0_1.md`
- `docs/weekend_build_cloud_mvp.md`
