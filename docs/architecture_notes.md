# Architecture Notes (v0.1)

## Determinism

- Computation outputs are deterministic functions of `inputs.json`.
- Run metadata (timestamps, host info) belongs in `metadata.json`, not in computed outputs.

## Module Boundaries

- Validation is a strict precondition of computation.
- The time grid is authoritative; all monthly tables resolve against it.
- Revenue is computed in two parts:
  - Base rent (market -> LTL -> in-place -> vacancy -> collection loss)
  - Revenue programs (adoption + capacity + collection loss)

