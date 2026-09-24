# ADR 0002: Stack Authentication Strategy (Function Keys -> Entra ID)

## Status

Accepted (phased)

## Context

For solo development and quick internal iteration, function keys are the fastest way to stand up and test the Stack Run Service. However, for 2+ colleagues, distributing shared secrets in workbooks/scripts is risky and not aligned with an M365 shop.

We want:
- No shared secrets embedded in Excel workbooks for team usage.
- Per-user identity in run metadata.
- Straightforward onboarding/offboarding via IT-friendly controls.

## Decision

Adopt a phased approach:

1) MVP: Function keys for initial end-to-end verification.
2) Team-ready: Entra ID authentication (EasyAuth) on the Function App, with authorization based on:
   - Entra group membership, or
   - App roles assigned to users/groups.

## Consequences

Pros:
- Fast weekend build without blocking on SSO plumbing.
- Clean migration path to SSO without changing the engine.
- Enables per-user audit trails and centralized access management.

Cons:
- Two auth modes to support during transition.
- Excel SSO is easiest with an Office Add-in; Office Scripts/VBA may limit auth options.
