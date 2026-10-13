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
    target_coc="0.08",
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
`value_add`). This selects the existing engine house policy: the house
replacement reserve ($250/unit, lender-style), recurring capex by vintage,
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

A canonical with no `assumptions` section records no provenance, so its values
are not treated as analyst values and the defaults apply; run it through the
resolver (ingest, or `resolve_assumptions`) first to have them honored.
`effective_assumptions.policy_value_sources` says who set each value:
`explicit_policy`, `canonical_analyst`, `policy_default`, or (exit cap only)
`canonical` or `entry_anchored`. A deal whose canonical carries no analyst
value for these fields prices exactly as before.

### Exit cap: tracks the entry cap

Exit caps should be roughly in line with entry caps (ADR 0003 §3). The exit
cap priced at each candidate price is, in order:

1. the policy's `exit_cap_rate` when given (`exit_cap_mode: "explicit_policy"`,
   labeled `house:backsolve_policy`);
2. else the canonical's analyst exit cap (its `exit_cap_rate` entry is
   `analyst`-sourced; `canonical_analyst`), priced as given;
3. else, for a canonical with no `assumptions` section, its exit cap as given
   (`canonical`; the resolver treats such a value as the analyst's);
4. else entry-anchored (`entry_anchored`): at every candidate price the going-in
   cap at that price (engine year-1 NOI / price, from the same all-equity
   preview that sizes the debt, so no extra engine run) plus
   `HOUSE_EXIT_CAP_SPREAD` (50 bps). The steal exception applies when the
   resolver recorded a market cap rate (`detail.market` on the canonical's
   `exit_cap_rate` entry): a going-in cap more than `HOUSE_STEAL_THRESHOLD`
   (150 bps) above it compresses the exit toward market + spread, by
   min(1, (excess - 150 bps) / (350 bps - 150 bps)) x
   `HOUSE_MAX_EXIT_COMPRESSION` (100 bps), where excess = going-in - market
   and 350 bps is `HOUSE_STEAL_FULL_COMPRESSION_EXCESS`: 0 at the threshold,
   growing linearly to 100 bps, capped beyond, and never below market +
   spread. Without a market rate there is never compression. A candidate whose going-in cap is
   outside `HOUSE_EXIT_CAP_GOING_IN_MAX` (an extreme bracket price) uses the
   market rate, else the canonical's exit cap.

`summary.exit_cap_calculation` discloses the solved exit cap: for
`entry_anchored`, `{mode, rule, purchase_price, year1_noi, going_in_cap_rate,
spread, anchored_exit_cap_rate, market_cap_rate, excess_over_market, steal,
steal_scale, compression, exit_cap_rate, market}` (also kept in the solved case's
`metadata.property_summary.exit_cap_calculation`); otherwise `{mode,
exit_cap_rate}`. `effective_assumptions.exit_cap_mode` names the mode. The
solved case's `exit_cap_rate` entry is the resolver's own default at the solved
price (`house:exit_cap_spread`, no override), so re-resolving it changes
nothing. The CLI summary prints the exit cap and how it was set.

### Exit-cap sensitivity grid

`summary.exit_cap_sensitivity` (also `results.exit_cap_sensitivity`) reports
the solved case at `HOUSE_EXIT_CAP_SENSITIVITY_OFFSETS_BPS` (-50, -25, 0, +25,
+50, +75, +100, +150 bps) around the solved exit cap: `{base_exit_cap_rate,
offsets_bps, rule, purchase_price, exit_month, forward_noi, exit_noi_window,
cash_on_cash_year_1, points}`, each point `{offset_bps, exit_cap_rate,
exit_value, sale_costs, net_sale_proceeds, unlevered_irr, levered_irr,
unlevered_em, levered_em, is_base}`. Each point pins its exit cap at the solved
price: an entry-anchored exit cap is not re-measured per point. The 0 bps point
is the solved case; year-1 cash-on-cash does not move with the exit cap and is
shown once. The CLI prints levered IRR across the grid.

Every point, like the solved case, is valued on the case's exit NOI
(`forward_noi` here; `results.metrics.exit.exit_noi`): by default forward NOI,
the projected NOI for the 12 months after the exit month (ADR 0003 decision
(n); `exit_noi_window` names the months), or the trailing 12 months when the
canonical sets `exit_assumptions.noi_window: trailing`. The exit NOI window
does not change cash-on-cash or the going-in cap, so a cash-on-cash solve
prices the same on either window; only the IRRs at the solved price move.

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
`replacement_reserve_floor_per_unit` (the $250/unit house reserve),
`repairs_maintenance_floor_per_unit`). `observed_repairs_maintenance_annual` is
on the R&M floor basis: repairs & maintenance plus turnover / make-ready,
payroll excluded.
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
- recurring capex (`capital_plan.recurring_capex.per_unit_annual`, ADR 0003
  §3): `max(value, vintage band)`, the band being the low end for `cashflow`,
  the midpoint for `value_add` and the high end when
  `deal_profile.deferred_maintenance` is set. A hold with none gets the floor
  (growing at the house R&M rate). An analyst figure below the floor is priced
  as given and disclosed, as the insurance override is: `policy_overrides`
  gets `{input: "recurring_capex_per_unit", action:
  "floor_waived_by_analyst_override", floor, floor_source, waived_by,
  original, applied}`. A PCA (`deal:pca`) figure below the floor is raised to
  it, `max(PCA, floor)`, and disclosed as `{input: "recurring_capex_per_unit",
  action: "floor_applied_over_deal_evidence", floor, floor_source, evidence,
  original, applied}`: a lender PCA sizes a lender reserve and understates
  economic capex on older stock. The reserve floor never stacks on it: the
  engine charges max(recurring capex, reserve) below NOI.
- deferred maintenance (`deal_profile.deferred_maintenance`): the vacancy,
  collection-loss and year-1 concession floors move to the upper part of the
  vintage bands (ADR 0003 decision (i)), and so does the R&M floor (its high
  end); `policy_summary.guardrails.profile.deferred_maintenance` is `true`.
- insurance and R&M: `max` on the annualized line or the R&M floor basis
  (below).

The loss-to-lease, collection-loss and concession floors are the max of the
vintage floor and the broker note (`underwriting_notes.loss_to_lease`,
`bad_debt`, `concessions`) when one is given. Each raise appears in
`policy_overrides` with `assumption`, `floor`, `floor_source`, `original` and
`applied` (opex floors also carry `original_annual` and `applied_annual`;
concession rows carry their `concession_id` and, when split, `split_at`).
`house_adjustments.guardrails` lists the profile and every finding. The only
input normalization left is the house box-score rebase (loss to lease and
vacancy rebuilt on the rent-roll floorplans).

The R&M floor compares the vintage R&M band, which excludes payroll, with R&M
including turnover: the repairs & maintenance and turnover / make-ready lines,
never payroll (a "Repairs Salaries" line is payroll). A T12 that books turns on
their own lines is therefore not topped up for R&M it already carries. Payroll
earns a credit toward the floor only through the payroll review (ADR 0003
decision (j)): payroll lines labeled as maintenance labor and total payroll per
unit above the house benchmark ($2,000/unit); the credit is the lesser of the
two, or the analyst's `r_and_m_payroll_credit`. The R&M finding in
`policy_overrides` carries `payroll_review` and `payroll_credit_annual`. A
shortfall tops the basis up with one "Repairs & Maintenance Vintage Floor
Top-Up" line sized from the other R&M lines less the credit; a top-up line left by an earlier
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

### Capital plan: the three capex buckets

Every candidate price carries the canonical's capital plan (ADR 0003 §3):

1. Immediate / deferred capex at close (`capital_plan.immediate_capex`):
   with `funding: "equity_at_close"` (the default) it is added to
   `total_equity_basis` and `total_unlevered_basis` at every candidate, like
   the renovation budget but as its own budget; with `"lender_holdback"` the
   loan commitment is upsized by it instead (equity unchanged) and it is in
   the unlevered basis. The priced case records
   `immediate_capex.amount_in_stated_basis`, so the engine does not add it
   again. It is never in NOI or the monthly cash flows. A deal flagged with
   deferred maintenance and no PCA or analyst figure carries the house
   placeholder (`house:deferred_capex_placeholder`: one year of the vintage
   capex band high end per unit), which is priced like any other amount.
2. Recurring capex by vintage reduces cash flow below NOI at every candidate,
   so it lowers cash-on-cash and the price.
3. The $250/unit replacement reserve is the floor of that charge.

`summary.capital_plan` (also `house_adjustments.capital_plan`) reports
`immediate_capex` (`amount`, `funding`, `included_in_total_equity_basis`,
`loan_upsized_by_holdback`), `renovation_budget`, `recurring_capex_per_unit`,
`recurring_capex_growth`, `recurring_capex_floor_per_unit`,
`replacement_reserve_per_unit`, `replacement_reserve_floor_per_unit`,
`binding_year_1` and `below_noi_capex_charge` (the engine's per-analysis-year
recurring capex, reserve, charge and the bucket that binds).

### Loan sizing on underwritten NCF

Agency lenders size on NOI less replacement reserves (ADR 0003 decision i).
At every candidate price the agency loan's 1.25x DSCR constraint is sized on
the projected year's NOI (all-equity preview at that price) less the
deduction its `loan_sizing_noi_basis` names:

| Basis | Sizing cash flow |
| --- | --- |
| `ncf_after_capex_charge` (house default, `HOUSE_LOAN_SIZING_NOI_BASIS`) | NOI - max(recurring capex, lender reserve), the charge the cash flow deducts |
| `ncf_after_lender_reserve` | NOI - the lender replacement reserve only |
| `noi_before_capex` | NOI |

The policy's `loan_sizing_noi_basis` wins, else the canonical's
`loan_sizing_noi_basis` (an analyst assumption), else the house default; an
explicit policy value is written into the priced case and labeled
`house:backsolve_policy`. A charge that consumes the NOI sizes no loan. The
65% LTV constraint is unchanged, so an LTV-bound candidate is unaffected.
`summary.loan_sizing` discloses `basis`, `source`, `noi_used`,
`below_noi_deduction`, `sizing_cash_flow`, `required_dscr`,
`sizing_method` and `loan_amount` at the solved price (also kept in the case's
`metadata.property_summary.loan_sizing`). `summary.minimum_dscr` is on the
same basis (`minimum_dscr_basis`), with `minimum_dscr_noi_before_capex`
beside it. The CLI takes `--loan-sizing-noi-basis`.

The summary discloses `coc_basis` (`basis`, `source`, `analysis_year`,
`months`, `rule`, `free_cf`, `equity_basis`, and `last_renovation_month` when
renovations set the year) and `renovation_funding` (`renovation_capex_funding`,
`renovation_budget`, `equity_funded_renovation_budget`). `cash_on_cash_persisted`
and `achieved_coc_pct` are on the selected basis; `cash_on_cash_year_1_persisted`
stays the Year-1 figure. The CLI takes `--coc-basis`.

All fields in `DEFAULTS`, plus `year_built`, `exit_cap_rate`, `coc_basis` and
`loan_sizing_noi_basis`, can be explicitly overridden. Unknown keys,
unsupported versions, an unknown `coc_basis` or `loan_sizing_noi_basis`,
non-finite values, invalid dates, invalid iteration limits, and
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
material violations refuse. The sized loan is rounded to the nearest $1,000, so
cash-on-cash can rise by about |CoC| x $1,000 / equity where the loan steps up;
a rise up to twice that is tolerated, and the solved price is a feasible cent
whose next cent is infeasible, within one loan rounding step of the true
boundary. It does not establish a global optimum for an arbitrary
non-monotonic model. The search stays inside the supplied bounds.

## CLI and MCP

The CLI requires `--policy-version`, `--benchmark-5yr-treasury`,
`--benchmark-as-of`, and `--benchmark-source`. `--policy-json` supplies explicit
policy overrides; its version must match the selected version. A fee flag
(`--sale-cost-percent`, `--acquisition-fee-pct`, ...) is an explicit policy
value only when given; omitted, the canonical's analyst value is priced, else
the policy default. An infeasible
or exhausted search writes its status and exits with code 2 so a caller cannot
treat it as a completed solve. Exit code 0 requires a converged boundary or a
feasible supplied ceiling. Every run prints a concise summary to stdout:
status, solved price, cash-on-cash basis and target, achieved cash-on-cash,
levered IRR, minimum DSCR, the number of house floors enforced, and the paths
of `backsolve_summary.json` and the other artifacts written.

The engine MCP tool `backsolve_deal_price` returns the same summary and
assumptions, excluding large cashflow arrays. The Platworks tool
`underwrite_backsolve` calls this public API and carries its status and summary.
The core result does not grant human approval or certify the source inputs.

Price bounds are limited to $1 trillion because the existing canonical case
serializes prices as JSON numbers. This limit keeps cent identities intact
through that boundary; operating money uses the separate string/cents contract.
