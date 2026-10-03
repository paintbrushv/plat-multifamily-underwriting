# Public backsolve API

`engine.backsolve.backsolve_price` owns the price search used by the CLI and
MCP adapters. It accepts canonical deal inputs, applies the selected house
policy, and re-evaluates tax, debt, equity, and Year-1 cash-on-cash return at
each candidate price. It does not write artifacts.

```python
from engine.backsolve import backsolve_price

result = backsolve_price(
    canonical,
    target_coc="0.07",
    policy={"version": "plat.backsolve-policy/1", "strategy": "cashflow",
            "year_built": 1984, "exit_cap_rate": "0.06"},
    benchmark={"rate": "0.04", "as_of": "2026-10-03", "source": "synthetic:example"},
    min_price="1000000.00", max_price="100000000.00",
)
```

The example rate is synthetic. Supply a reviewed benchmark value, observation
date, and source for each calculation. The API never fetches or assumes a
current Treasury rate. Rates are fractions; the umbrella's `target_coc_pct`
argument uses percent units (7 means 7%).

## Policy version 1

The caller must select `plat.backsolve-policy/1` and a strategy (`cashflow` or
`value_add`). This selects the existing engine house policy: vintage reserve,
repair/maintenance, vacancy, collection-loss, concession, and insurance floors.
It also selects the fee defaults in `engine.backsolve_policy.DEFAULTS`.
The result discloses resolved fees, benchmark, tax policy, inferred vintage,
and the house adjustments actually applied. Defaults are model assumptions,
not observed property facts or a claim of universal industry practice.

All fields in `DEFAULTS`, plus `year_built` and `exit_cap_rate`, can be explicitly
overridden. Unknown keys, unsupported versions, non-finite values, invalid
dates, invalid iteration limits, and sub-cent price bounds refuse before
underwriting. An exit cap must come from the policy or canonical inputs.
Optional market-vacancy evidence must be supplied in
`metadata.property_summary.market_vacancy_benchmark` with `rate`, `as_of`, and
`source`; the solver does not load an optional local market-data package.

## Results and boundaries

Contract: `plat.backsolve/1`. Results include `summary`, `case`, `results`,
`effective_assumptions`, and the tested `bracket`.

| Status | Meaning |
| --- | --- |
| `converged` | Feasible and infeasible bounds are one cent apart. |
| `ceiling_feasible` | The supplied upper bound is feasible; no higher price was tested. |
| `infeasible` | The lower bound misses the target; no solved case is returned. |
| `iteration_limit` | A feasible candidate exists, but the cent boundary is unresolved. |

The search assumes cash-on-cash return does not increase with price. Observed
material violations refuse. It does not establish a global optimum for an
arbitrary non-monotonic model. The search stays inside the supplied bounds.

## CLI and MCP

The CLI requires `--policy-version`, `--benchmark-5yr-treasury`,
`--benchmark-as-of`, and `--benchmark-source`. `--policy-json` supplies explicit
policy overrides; its version must match the selected version. An infeasible
or exhausted search writes its status and exits with code 2 so a caller cannot
treat it as a completed solve. Exit code 0 requires a converged boundary or a
feasible supplied ceiling.

The engine MCP tool `backsolve_deal_price` returns the same summary and
assumptions, excluding large cashflow arrays. The Platworks tool
`underwrite_backsolve` calls this public API and carries its status and summary.
The core result does not grant human approval or certify the source inputs.

Price bounds are limited to $1 trillion because the existing canonical case
serializes prices as JSON numbers. This limit keeps cent identities intact
through that boundary; operating money uses the separate string/cents contract.
