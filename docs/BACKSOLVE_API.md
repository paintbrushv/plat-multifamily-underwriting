# Public backsolve API

`engine.backsolve.backsolve_price` owns the price search used by the CLI and
MCP adapters. It accepts canonical deal inputs, applies the selected house
policy, and re-evaluates tax, debt, equity, and post-debt cash-on-cash return
(on the policy's `coc_basis`: Year 1, or the first stabilized year for
value-add) at each candidate price. It does not write artifacts.

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
repair/maintenance, vacancy, loss-to-lease, collection-loss, concession, and
insurance floors. It also selects the fee defaults in
`engine.backsolve_policy.DEFAULTS` and the cash-on-cash basis (`coc_basis`,
below).
The result discloses resolved fees, benchmark, tax policy, inferred vintage,
and the house adjustments actually applied. Defaults are model assumptions,
not observed property facts or a claim of universal industry practice.

### Analyst values win over policy defaults

The policy defaults never silently replace an analyst's value. For each
pricing-policy field (`sale_cost_percent`, `acquisition_fee_pct`,
`asset_management_fee_pct`, `disposition_fee_pct`,
`annual_partnership_expenses`, `partnership_closing_costs`,
`purchase_closing_cost_pct`, `loan_closing_costs`), the value priced is:

1. an explicit policy value (a key in `policy`, `--policy-json`, or a CLI flag
   that was given), labeled `house:backsolve_policy`;
2. else the canonical's analyst value: its `assumptions` entry has
   `source: "analyst"` (for example an ingest `--sale-cost`, an
   `--assumption-overrides` or sheet edit, or a value already in a hand-authored
   canonical that was resolved). It is priced as given and keeps its `analyst`
   label;
3. else `engine.backsolve_policy.DEFAULTS`, labeled `house:backsolve_policy`.

The exit cap follows the same rule: the policy's `exit_cap_rate` when given,
else the canonical's. A canonical with no `assumptions` section records no
provenance, so its values are not treated as analyst values and the defaults
apply; run it through the resolver (ingest, or `resolve_assumptions`) first to
have them honored. `effective_assumptions.policy_value_sources` says who set
each value: `explicit_policy`, `canonical_analyst`, `policy_default`, or (exit
cap only) `canonical`. A deal whose canonical carries no analyst value for
these fields prices exactly as before.

Floors compare annual dollars: opex lines are annualized from their
`calculation_type` first, and a floor replaces the analyst's matching line
rather than being added beside it. Concessions are read as `pct_rent` or
`free_months` (months / 12); any other concession type, or a percent-of-revenue
opex line that a floor must read, refuses. The property-level vacancy anchor
(`physical_vacancy_floor`, with `physical_vacancy_floor_source`) floors each
`physical_vacancy_curve` segment
separately and preserves the analyst's curve shape: a segment below the anchor
is raised to it, and a segment at or above it is kept as entered, so one
cohort's higher vacancy is never lowered or spread to other cohorts. The
resulting curve is reported as `physical_vacancy_curve_applied`, and
`physical_vacancy_rate_applied` is that priced curve's unit- and month-weighted
rate over the hold. Summary fields named `*_applied` (and
`replacement_reserve_per_unit`, the first priced reserve segment per unit, with
the curve in `replacement_reserves_applied`) report what was priced; the floors
are reported separately (`physical_vacancy_floor`,
`replacement_reserve_floor_per_unit`, `repairs_maintenance_floor_per_unit`).
Every analyst
input the policy replaced, including each raised vacancy segment, is listed in
`policy_overrides` with its original and applied value.

### Shared guardrails

The floors are defined once, in `engine/assumptions/guardrails.py` (ADR 0003
§5), and the assumption resolver uses the same module. Backsolve runs them in
`mode="pricing"` on the analyst's inputs. Floors only raise, per segment, as
`max(analyst value, floor)`; they never lower or reshape an input:

- vacancy, loss to lease, collection loss and reserves: each segment below its
  floor is raised to it, and every other segment keeps the analyst's value.
  Loss to lease is never re-derived from market vs in-place rent. A cohort
  whose market rent is at or below its in-place rent (including a rent the
  house box-score rebase capped) has no loss-to-lease gap, so the floor is not
  imposed on it (no raise, no underwrite warning, its rows kept); each such
  cohort is listed in `house_adjustments.guardrails.exemptions` with
  `status: "exempt_no_gap"`, its market and in-place rent, and the rates kept.
  A cohort with a gap and no loss-to-lease row gets one at the floor. A hold
  with no collection-loss curve or no reserves gets one row at the floor.
- concessions: the schedule and its later-year rows are kept. Each row that
  applies in analysis year 1 and is below the floor is raised to it (a row that
  runs past year 1 is split at the year-1 boundary, so its later months keep
  the analyst's rate). A `free_months` row is compared as months / 12 and keeps
  its type. With no year-1 row, one year-1 `pct_rent` row at the floor is
  added.
- insurance and R&M: `max` on the annualized line or bucket (below).

The loss-to-lease, collection-loss and concession floors are the max of the
vintage floor and the broker note (`underwriting_notes.loss_to_lease`,
`bad_debt`, `concessions`) when one is given. Each raise appears in
`policy_overrides` with `assumption`, `floor`, `floor_source`, `original` and
`applied` (opex floors also carry `original_annual` and `applied_annual`;
concession rows carry their `concession_id` and, when split, `split_at`).
`house_adjustments.guardrails` lists the profile and every finding. The only
input normalization left is the house box-score rebase (loss to lease and
vacancy rebuilt on the rent-roll floorplans).

The R&M floor tops the bucket up with one "Repairs & Maintenance Vintage Floor
Top-Up" line sized from the other R&M lines; a top-up line left by an earlier
pricing run never counts toward the floor, so re-pricing a priced canonical
after lowering R&M lands exactly on the floor (`replaced_top_up_annual`
reports the old line).

An explicit `insurance_per_unit_override` waives the insurance floor and is
priced as given. Below the floor, the waiver is disclosed: `policy_overrides`
gets `{input: "insurance_per_unit", action: "floor_waived_by_analyst_override",
floor, floor_source, original, applied}` and `house_adjustments.guardrails`
the matching finding.

The same floors in `mode="underwrite"` (ingest, `resolve_assumptions`) never
change a value; they add a `guardrail` record with `action: "warned"` and a
`GUARDRAIL_BELOW_FLOOR` validator warning.

The solved `case` is re-resolved in `mode="pricing"`: its `assumptions` section
holds the priced values, and each enforced floor is recorded on its entry
(`guardrail.action: "enforced"`, `guardrail.original` the pre-floor value, and
an `override` that pins the priced value). Only `assumptions` and
`deal_profile` change; every priced number is unchanged.

Values the pricing sets outside the floors are labeled by who set them, never
as analyst edits: fees, closing costs, sale cost and an exit cap the policy
set (an explicit policy value or a default, never an analyst value the
canonical carries) carry `source: "house:backsolve_policy"`; inputs the house box-score rebase rebuilt
(and an opex line pricing preparation set, such as a revenue-quality bridge
paired expense) carry `house:pricing_normalization`; a value a floor raised
carries the floor's source; the real estate tax line reassessed at the
solved price carries `house:property_tax_module`; an opex line a floor raised
carries the floor's source; and the policy's insurance override is `analyst`
with a note naming the policy field. Each pins the priced value with an
override whose note names the rule, and the labels survive a re-resolution.
The insurance waiver is also recorded on the `insurance_per_unit` entry
(`guardrail.action: "floor_waived_by_analyst_override"`).

### Cash-on-cash basis and value-add pricing

`coc_basis` selects the return the search targets: `"year_1"` (the Year-1
post-debt cash-on-cash, the default for `cashflow`) or `"stabilized_year"` (the
default for `value_add`). Both use free cash flow (levered cash flow less the
asset management fee and partnership expenses) over `total_equity_basis`. On
the stabilized-year basis the year is:

- for a deal with renovation programs or unit renovations, the first full
  12-month analysis year that starts after the last unit is renovated;
- otherwise `find_stabilized_year` (engine/modules/metrics.py) on the analysis
  years: the first of years 1-3 whose unit-weighted vacancy is within 0.5
  points of the hold's last-month vacancy and whose operating CapEx is below
  5% of price plus closing costs, else year 2.

The year must be a full year before the exit year, so sale proceeds never
count as operating cash; otherwise the solve refuses.

For `value_add`, the renovation budget (program units x cost per unit plus
`unit_renovations`, as the engine schedules them) is funded with equity at
close: the case carries `renovation_capex_funding: "equity_at_close"`, each
candidate adds the budget to `total_equity_basis` and `total_unlevered_basis`,
and the engine keeps it out of operating cash flow (each month shows it as
`equity_funded_capex`; `results.capex_funding` totals it). The IRR and equity
multiple series start from those bases, so they stay consistent with the
cash flows. A canonical that says `operating_cash_flow` is switched and the
change is listed in `policy_overrides`. With the budget funded at close,
cash-on-cash falls with price, so a renovation deal converges over a wide
bracket. Cashflow pricing keeps the canonical's funding as entered.

The summary discloses `coc_basis` (`basis`, `source`, `analysis_year`,
`months`, `rule`, `free_cf`, `equity_basis`, and `last_renovation_month` when
renovations set the year) and `renovation_funding` (`renovation_capex_funding`,
`renovation_budget`, `equity_funded_renovation_budget`). `cash_on_cash_persisted`
and `achieved_coc_pct` are on the selected basis; `cash_on_cash_year_1_persisted`
stays the Year-1 figure. The CLI takes `--coc-basis`.

All fields in `DEFAULTS`, plus `year_built`, `exit_cap_rate` and `coc_basis`,
can be explicitly overridden. Unknown keys, unsupported versions, an unknown
`coc_basis`, non-finite values, invalid dates, invalid iteration limits, and
sub-cent price bounds refuse before underwriting. An exit cap must come from the policy or canonical inputs.
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
policy overrides; its version must match the selected version. A fee flag
(`--sale-cost-percent`, `--acquisition-fee-pct`, ...) is an explicit policy
value only when given; omitted, the canonical's analyst value is priced, else
the policy default. An infeasible
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
