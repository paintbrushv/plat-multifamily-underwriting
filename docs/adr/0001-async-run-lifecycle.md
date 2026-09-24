# ADR 0001: Stack Async Run Lifecycle (Run ID + Queue Worker)

## Status

Accepted (MVP)

## Context

Excel clients and other callers should be able to submit an underwriting run and reliably retrieve artifacts and status. Synchronous HTTP execution risks timeouts, retries causing duplicate runs, and unclear provenance.

We also want first-class auditability: every run must write immutable artifacts (`inputs.json`, `outputs.json`, `validation.json`, `metadata.json`) and be listable by deal.

## Decision

Use an asynchronous "run lifecycle":

- `POST /api/runs` creates a `run_id`, persists inputs/validation artifacts, enqueues a message, and returns `202 Accepted`.
- A queue-triggered worker executes the engine and writes outputs/metadata.
- A run index record tracks status transitions: `queued -> running -> success|failed`.

## Consequences

Pros:
- Avoids Excel/HTTP timeout issues.
- Makes audit/history and reproducibility a first-class product feature.
- Enables predictable scaling via queue-driven workers.

Cons:
- Requires a status model and client polling.
- Adds storage and queue dependencies (Blob + Queue + Table).

## Notes

This ADR is the basis for the Azure wrapper described in `docs/azure_architecture.md`.
