# ADR 0003: Assumption Layer (Context-Driven, Inspectable Underwriting Assumptions)

## Status

Accepted. Slices 1-8 are implemented, and so are the product decisions that
followed them; see "Implementation status" at the end.

## Context

The engine should let any analyst underwrite a stabilized or value-add
multifamily deal out of the box. Assumptions should flex to the deal type,
MSA and submarket, and every assumption should be easy to inspect and change.

Today assumptions come from four unconnected places:

- hard-coded ingest fallbacks (`build_deal_from_documents`: 3% rent growth,
  0.5% collection loss);
- the house vintage framework (`engine/underwriting_policy.py`,
  `VINTAGE_FRAMEWORK_2026`, `default_*` functions, `revenue_guardrails`);
- backsolve policy defaults (`engine/backsolve_policy.DEFAULTS`) and the
  house-policy rewrite inside `engine/backsolve.py`;
- market context that is wired only partially (`engine/market_integration.py`
  points at a sibling `market-study-agent` path that no longer exists, and
  `engine.market_data.costar_cap_rates` is imported but not shipped).

Nothing records where a value came from. An override silently replaces a
default, and a default silently replaces analyst input (UPL-42, UPL-43,
UPL-44).

Market data feeders:

- **plat-market-study-agent** (file outputs): CoStar submarket parquet from
  `etl/transform_costar_excel.py` (vacancy, stabilized vacancy, asking and
  effective rent, YoY rent growth, cap rate and market cap rate, including
  CoStar forecast rows); 8-quarter rent-growth ensemble forecasts
  (`v2_ensemble_{tier}.csv` and `model_diagnostics_{tier}.json` with a
  confidence level); comp evidence (`comps.json` with face rent, effective
  rent and concession by unit type); and a standardized subject rent roll.
  Keyed by `metro_slug`, `submarket` and star tier. The CoStar inputs are paid
  and user-supplied.
- **plat-supply-demand** (file outputs): metro rent-growth forecasts with 80%
  and 95% intervals
  (`data/econometrics/forecasts/<metro>/<metro>_forecast_<vintage>.parquet`),
  stress scenarios, a concession spread and a market-cycle position. Keyed by
  `metro_id` / `msa_code`. These also need CoStar input; FRED/BLS feeds need
  keys.

Neither repo ships data, and neither covers opex, insurance or tax trends.
This repo is public, so licensed data can never live in it.

## Decision

### 1. `deal_profile` drives defaults

A new canonical section:

```json
"deal_profile": {
  "deal_type": "stabilized" | "value_add",
  "msa": {"cbsa_code": "19100", "name": "Dallas-Fort Worth-Arlington, TX", "metro_slug": "dallas_tx"},
  "submarket": {"name": "...", "source_id": "..."},
  "year_built": 1984,
  "asset_class": "A" | "B" | "C",
  "star_tier": "1_2" | "3" | "4_5"
}
```

- `deal_type` maps to the existing house `strategy`: `stabilized` → `cashflow`
  and `value_add` → `value_add`.
- Lease-up and distressed/turnaround are reserved values for later profiles.
  The resolver rejects them until those profiles exist.
- Any missing geography field lowers the precedence chain (§3); it never
  blocks a run.

### 2. Assumption registry with provenance

A new optional canonical section `assumptions` holds one entry per assumption
key:

```json
"assumptions": {
  "resolver_version": "plat.assumptions/1",
  "mode": "underwrite" | "pricing",
  "entries": {
    "rent_growth": {
      "value": [0.021, 0.028, 0.030, 0.030, 0.030],
      "unit": "decimal_yoy_by_analysis_year",
      "source": "market:plat-supply-demand",
      "basis": "dallas_tx metro forecast, vintage 2026-09-30, years 3+ glide to long-run",
      "as_of": "2026-09-30",
      "confidence": "medium",
      "candidates": [{"source": "house:long_run_rent_growth", "value": 0.025}],
      "default_value": [0.021, 0.028, 0.030, 0.030, 0.030],
      "override": null,
      "review_required": false
    }
  }
}
```

- `value` is what the engine uses. `default_value` is what the resolver
  produced before any override. `override` is `{value, note}` when the analyst
  changed it.
- `candidates` lists every source that was considered.
- `source` uses one vocabulary (`engine/assumptions/registry.py`
  `SOURCE_PATTERN`): `analyst`, `deal:<document>`, `market:<provider>`,
  `house:<rule>` and `engine_fallback`. The values the code emits:

| Source | Set by |
|---|---|
| `analyst` | An analyst override: `--assumption-overrides`, an edited sheet, a partial canonical patch, a value already in a hand-authored canonical, or backsolve's `insurance_per_unit_override` |
| `deal:rent_roll` | Rent-roll evidence (current vacancy, loss to lease) |
| `deal:t12` | T12 evidence (bad debt, concessions, opex lines, insurance, R&M) |
| `deal:om` | The offering memorandum (exit cap) and broker notes that raise a floor |
| `deal:comps` | Comp evidence (`renovation_premium`) |
| `deal:pca` | A property condition assessment (`capital_plan.pca`): immediate needs and recurring capex per unit |
| `market:<provider>` | A market-data provider: `plat_market_study_agent`, `plat_supply_demand`, `user_file`, `costar`, `yardi_matrix`, `realpage`, `synthetic` (tests); `market:supplied_benchmark` is the analyst-supplied `market_vacancy_benchmark` |
| `house:vintage_<bucket>` | `VINTAGE_FRAMEWORK_2026` for the year-built bucket (e.g. `house:vintage_1980s`) |
| `house:long_run_rent_growth` | The house long-run rent growth after the forecast horizon |
| `house:default_collection_loss` | The house default collection loss when the vintage is unknown |
| `house:exit_cap_spread` | The entry-anchored exit cap: the going-in cap rate at the modeled price plus the house exit spread (§3), with the rare steal adjustment |
| `house:replacement_reserve` | `HOUSE_REPLACEMENT_RESERVE_PER_UNIT`: the lender-style $250/unit annual replacement reserve below NOI, the floor of the capex charge; also recurring capex when the vintage is unknown |
| `house:recurring_capex_vintage` | `default_recurring_capex_per_unit`: the vintage economic CapEx band (low end stabilized, midpoint value-add, high end with deferred maintenance) |
| `house:deferred_capex` | `HOUSE_IMMEDIATE_CAPEX_DEFAULT`: nothing budgeted at close without PCA or analyst evidence |
| `house:deferred_capex_placeholder` | `default_deferred_capex_placeholder_per_unit`: deferred maintenance flagged with no PCA and no analyst figure, one year of the vintage economic CapEx band high end per unit at close (review_required) |
| `house:r_and_m_payroll_review` | The R&M floor's payroll review (`r_and_m_payroll_credit`): the payroll credit toward the R&M floor, $0 unless the T12 labels maintenance labor in payroll and payroll per unit is above `HOUSE_PAYROLL_BENCHMARK_PER_UNIT`. A disclosure: flagged only in the ambiguous case (§5) |
| `house:loan_sizing_noi_basis` | `HOUSE_LOAN_SIZING_NOI_BASIS`: agency loan sizing and DSCR on underwritten NCF, NOI less the below-NOI capex charge (§3) |
| `house:opex_growth_table` | The house per-category opex growth table (also the 3% real estate tax growth and the recurring capex growth, `HOUSE_RECURRING_CAPEX_GROWTH` = the R&M rate) |
| `house:property_tax_module` | `engine/property_tax.py`: the real estate tax line reassessed at the purchase price |
| `house:management_fee_pct_egr` | The house management fee as a percent of EGR |
| `house:year1_insurance_floor` | The $600/unit Year-1 insurance floor |
| `house:vacancy_anchor` | The property-level vacancy anchor when no single anchor names the source |
| `house:backsolve_policy` | `engine/backsolve_policy` values: fees, closing costs, sale cost, and in pricing any value the backsolve policy sets (an explicit policy value, or a default where the canonical has no analyst value) |
| `house:pricing_normalization` | A backsolve pricing normalization (§5): the house box-score rebase (loss to lease and vacancy rebuilt on the rent-roll floorplans), or an opex line backsolve's preparation sets (a revenue-quality bridge paired expense). The house floors never rewrite an input. |
| `engine_fallback` | An engine default when nothing else applies |

#### Overrides

`resolve_assumptions(overrides={key: value | {"value", "note"[, "cohort_overrides"]} | None})`:

- A value or `{"value", "note"}` sets the override; the entry keeps
  `default_value`, records `override`, and its value wins. Overrides already
  recorded in the canonical are kept on re-resolution.
- `None` (JSON `null`) **clears** the key's override: the entry returns to its
  resolver default. The assumptions sheet emits `{key: null}` when an analyst
  blanks a row's Override (§8). The default is the full chain, market data
  included: with a registry that has data for the key it is re-queried;
  without one (or with one that has none, such as the default registry
  without feeder files) the market-informed `default_value` and market
  candidates recorded at the last resolution are kept. An overridden entry
  records its default's source, basis, as-of and confidence on its `analyst`
  candidate (`default`), so clearing restores the pre-override value and
  source exactly. `runs/assumptions_sheet.py apply` and
  `runs/run_deal.py --assumption-overrides` use
  `engine.market_data.default_registry()` unless `--no-market-data` is given.
- `cohort_overrides: {cohort_id: value}` is supported only for the cohort keys
  `physical_vacancy` and `loss_to_lease`; any other key raises
  `AssumptionOverrideError`.
- `opex.<slug>.base` overrides rewrite the row's `base_value` in its own
  `calculation_type`: annual dollars for dollar-based rows, the percent for
  `percent_egr` / `percent_rent` rows. An override of `0` suppresses the line
  (kept at $0 and never re-added). `opex.<slug>.growth` overrides set the
  row's growth rate.

**The engine stays deterministic and never reads providers.** The resolver
runs at ingest or re-resolve time and writes resolved values into the
existing canonical fields (`growth_assumptions`, `physical_vacancy_curve`,
`loss_to_lease`, `concession_schedule`, `collection_loss_curve`,
`opex_table`, `exit_assumptions`, ...). `assumptions` is the provenance and
override surface, and the engine ignores it. Re-running the engine on the
same canonical gives identical output.

### 3. Precedence

The general order is: analyst override > deal evidence > submarket > MSA >
house default for (deal type, vintage) > engine fallback.

How each key combines sources:

| Key | Resolution |
|---|---|
| `rent_growth` | Years with forecast coverage from the submarket forecast, else the MSA forecast (8-quarter ensemble, annualized to analysis years). After the horizon, a linear glide over 2 years to the long-run rate (MSA long-run, else house long-run constant). Deal evidence is not used for growth. |
| `physical_vacancy` | Curve from current physical vacancy (rent roll) to stabilized vacancy over the profile's stabilization window. Stabilized = submarket stabilized vacancy if available, else house vintage typical. |
| `loss_to_lease` | Current LTL from the rent roll (market vs in-place) burns off over the lease term (12 months) to the house vintage LTL floor. |
| `concessions` | Year-1 rate from deal evidence (T12 concessions / GPR, or rent-roll specials), else submarket concession spread (asking − effective) / asking, else house vintage floor. Burns off to the submarket or house stabilized level. |
| `collection_loss` | T12 bad debt / GPR, else the house default (vintage `bad_debt_typical_low`). The vintage collection-loss floor is a guardrail (§5), not the default. |
| `other_income` | T12 run-rate programs (UPL-42), else the house per-unit default. |
| `opex.<category>.base` | T12 line (normalized to annual dollars), else the house per-unit default for the category. |
| `opex.<category>.growth` | House per-category growth table (§6), unless a provider supplies a market trend. |
| `insurance_per_unit`, `r_and_m_per_unit`, `turn_cost_per_unit` | Deal evidence (annualized), else house vintage defaults (R&M: the band high end with deferred maintenance, §5). Floors are guardrails (§5). `r_and_m_per_unit` is on the R&M floor basis: repairs & maintenance plus turnover / make-ready lines, never payroll (§5). |
| `replacement_reserve_per_unit` | Bucket 3. Analyst, else `HOUSE_REPLACEMENT_RESERVE_PER_UNIT` ($250/unit/year, `house:replacement_reserve`, review_required): the lender-style annual reserve below NOI, the same for every vintage. It is a floor, not an addition: the engine charges max(recurring capex, reserve) below NOI. It is also the pricing floor for reserves (§5). |
| `immediate_capex` | Bucket 1, total dollars at close (`capital_plan.immediate_capex.amount`). PCA immediate needs (`deal:pca`) > analyst > `house:deferred_capex_placeholder` when `deal_profile.deferred_maintenance` is set (one year of the vintage economic CapEx band high end per unit, `HOUSE_DEFERRED_CAPEX_PLACEHOLDER_YEARS`; review_required, and the validator warns `DEFERRED_CAPEX_PLACEHOLDER`: commission a PCA, lenders escrow its immediate repairs at 125%) > `house:deferred_capex` ($0, review_required; with the flag and an unknown vintage the validator warns `DEFERRED_CAPEX_UNBUDGETED`). Funded with equity at close (`funding: "equity_at_close"`, default) or a lender holdback (`"lender_holdback"`); never in NOI. It is separate from the renovation budget. |
| `recurring_capex_per_unit` | Bucket 2, year-1 $/unit/year (`capital_plan.recurring_capex.per_unit_annual`), below NOI. PCA (`deal:pca`) > analyst > `house:recurring_capex_vintage` (the vintage economic CapEx band: low end for a stabilized deal, midpoint for value-add, high end when `deal_profile.deferred_maintenance` is true) > the $250 reserve when the vintage is unknown (`house:replacement_reserve`, review_required). A PCA outranks an analyst value already in the canonical at a first resolution (the analyst value is kept as a candidate); an explicit override or later edit still wins and shows the PCA as its default. Built before 2000 with no PCA: `review_required` stays on (even under an override), the validator warns `CAPEX_PCA_MISSING` and the ingest gate adds the intake flag `capex_pca_missing_pre_2000_review`. The retired `economic_capex_per_unit` key is accepted as an alias, and its old opt-in `capex_schedule` row is migrated here (removed from the schedule, so it is charged once). |
| `recurring_capex_growth` | Bucket 2 growth (`capital_plan.recurring_capex.growth_rate`): `HOUSE_RECURRING_CAPEX_GROWTH` = `OPEX_GROWTH_REPAIRS_MAINTENANCE` (3%), `house:opex_growth_table`, applied from analysis year 2 like opex. |
| `exit_cap_rate` | Exit caps track entry caps. Analyst, else entry-anchored: the going-in cap rate at the price being modeled (engine year-1 NOI / purchase price; trailing T12 NOI only when the engine cannot run the canonical) plus `HOUSE_EXIT_CAP_SPREAD` (50 bps), `house:exit_cap_spread`, review_required. It is re-measured whenever the price changes: backsolve recomputes it at every candidate price unless the analyst or an explicit policy set it. Steal exception, which is rare: only when a submarket or MSA `market_cap_rate` (the exit-year value) is available and the going-in cap exceeds it by more than `HOUSE_STEAL_THRESHOLD` (150 bps) does the exit compress toward market + spread, never more than `HOUSE_MAX_EXIT_COMPRESSION` (100 bps) below going-in + spread; the basis says "Steal: going-in X vs market Y". Without market data there is never compression, and a going-in cap at or below market still exits at entry + spread. `HOUSE_EXIT_CAP_GOING_IN_MAX` (25%) guards the going-in cap. Only when there is no usable price to anchor to: the market cap rate, else the OM's exit cap, else the engine default. Every source is a candidate, and the entry's `detail` records the calculation and the market reference (`detail.market`), so a re-resolution without market data applies the same steal test. |
| `loan_sizing_noi_basis` | The cash flow agency loan sizing and DSCR use (top-level `loan_sizing_noi_basis`, categorical). Analyst, else `HOUSE_LOAN_SIZING_NOI_BASIS` = `ncf_after_capex_charge` (`house:loan_sizing_noi_basis`, review_required, never written): NOI less the below-NOI capex charge, max(recurring capex, lender reserve), as agency lenders size on NOI less replacement reserves. `ncf_after_lender_reserve` deducts the lender reserve only; `noi_before_capex` is NOI. The engine reports `metrics.dscr` on it with the NOI-based DSCR beside it (`metrics.dscr.noi_before_capex`); backsolve sizes every candidate price on it (an explicit policy `loan_sizing_noi_basis` wins over the canonical's). |
| `value_add.renovation_premium` | Comp evidence (renovated vs classic effective rent) if present. Otherwise it is required, and the run is flagged when it is missing. |

### 4. Curves, not scalars; granularity

- The engine stays monthly. Assumptions are entered per analysis year
  (vectors) or as start/stabilized/window triples. The resolver expands them
  into monthly segments.
- `growth_assumptions` gains an optional `annual_growth_rates`. Year 1 has no
  growth. Element *k* is the growth applied at the start of analysis year
  *k+2*, so the first increase (entering year 2) uses element 0, as the
  scalar does today. Elements past the hold are ignored; if the vector is
  shorter than the hold, its last value repeats. `growth_start_month` still
  shifts the start: years count from it. `annual_growth_rate` stays as the
  scalar shorthand (the resolver writes element 0 to it). The engine (UPL-44
  path) grows flat curves by the vector when it is present, else by the
  scalar; explicit time-varying curves stay authoritative.
- Assumptions are property-level by default. The cohort keys
  `physical_vacancy` and `loss_to_lease` may carry
  `cohort_overrides: {cohort_id: value}` for cohort-specific curves; no other
  key supports them.

### 5. Gates and guardrails

- **Blockers** are data-integrity failures only: the T12 doesn't reconcile
  (UPL-42), the rent roll doesn't reconcile to its own export summary,
  required tax millage is missing, or the deal has no unit cohorts.
  The validator emits an ERROR and the engine refuses to run.
- **Ingest reconciliation gates** (`metadata.ingest_gate`) tie each parsed
  document to the totals the export itself prints; the ingest CLI prints each
  check:
  - T12 (`t12_reconciliation`): parsed opex vs the statement's total
    operating expenses and parsed income vs its income boundary (total income,
    total revenue or effective gross income), each within 0.5%.
  - Rent roll (`rent_roll_reconciliation`): when the export carries its own
    summary (e.g. a Yardi "Summary Groups" block), parsed units and vacant
    units must match it exactly (non-revenue units may count either way) and
    the monthly market-rent total within 0.5%; when the export also totals its
    rent charge codes, the parsed in-place rent must tie within 0.5% too.
  - A check that fails is a blocker (`t12_<side>_does_not_reconcile`,
    `rent_roll_<check>_does_not_reconcile`) and the gate is BLOCKED. A
    document with no totals to tie to is UNVERIFIED: an intake sanity flag
    (`t12_totals_not_found_reconciliation_unverified`,
    `rent_roll_summary_not_found_reconciliation_unverified`), never a blocker.
- **Flags** are review prompts. They cover any entry from `house:*` or
  `engine_fallback` (except a disclosure source, `registry.REVIEW_ON_FINDING_SOURCES`:
  the R&M payroll review is flagged only in its ambiguous case), provider data
  with low confidence, and values outside the submarket or vintage typical
  band. The validator emits a WARNING and
  `review_required: true`.
- **House floors** (vintage vacancy, LTL, collection loss, concessions,
  insurance, R&M, reserves) are guardrails:
  - In `mode="underwrite"` they never change values; they add a warning.
  - In `mode="pricing"` (backsolve) they are enforced as
    `max(value, floor)` per segment and listed in `policy_overrides`.
  - The shape of an analyst curve is always preserved (UPL-43).

As implemented (slice 6), `engine/assumptions/guardrails.py` is the one place
that defines the floors for a (deal type → strategy, year built) profile. It
reuses the `underwriting_policy` vintage functions:

| Key | Floor | Checked against |
|---|---|---|
| `physical_vacancy` | Property-level anchor: max of the vintage floor, rent-roll vacant share (`house_box_score`), T12 vacancy / GPR, broker vacancy, and a supplied `market_vacancy_benchmark` | each `physical_vacancy_curve` segment |
| `loss_to_lease` | max(vintage LTL floor, broker LTL) | each `loss_to_lease` segment of a cohort whose market rent exceeds its in-place rent; such a cohort with no row counts as 0%. A cohort with no gap (market at or below in-place, including a rent the house box-score rebase capped) is exempt in both modes: no warning, no raise, its rows kept, and the exemption disclosed (`exempt_no_gap`) |
| `collection_loss` | max(vintage floor, broker bad debt) | each `collection_loss_curve` segment (no curve counts as 0) |
| `concessions` | max(vintage year-1 floor, broker concessions) | each `concession_schedule` row that applies in analysis year 1 (`free_months` read as months / 12); no year-1 row counts as 0% |
| `insurance_per_unit` | $600/unit (`DEFAULT_YEAR1_INSURANCE_PER_UNIT`); waived by an explicit analyst insurance override, and the waiver is disclosed | the matched insurance line, annualized |
| `r_and_m_per_unit` | vintage R&M per unit (the band excludes payroll) | R&M including turnover: the repairs & maintenance and turnover / make-ready lines, annualized (`underwriting_policy.R_AND_M_FLOOR_BASIS`, `canonical_ops.in_r_and_m_floor_basis`), plus the payroll credit of the payroll review (decision (j)). A payroll line is never in the basis, even one labeled as repairs, so a T12 that books turns separately is not topped up for R&M it carries. A shortfall is one top-up line sized from the other R&M lines less the payroll credit (an existing top-up line never counts toward the floor); the guardrail record's basis names the floor basis and the payroll review |
| `replacement_reserve_per_unit` | `HOUSE_REPLACEMENT_RESERVE_PER_UNIT` ($250/unit, `house:replacement_reserve`, any vintage) | each `replacement_reserves` segment (none counts as 0) |
| `recurring_capex_per_unit` | The vintage band pick for the deal (`house:recurring_capex_vintage`; none when the vintage is unknown). An analyst figure below it is kept and disclosed (`floor_waived_by_analyst_override`, with `waived_by`), as the insurance override is. A PCA figure below it is warned in underwrite (the record names the `evidence`) and raised to the floor in pricing, `max(PCA, floor)`, disclosed as `floor_applied_over_deal_evidence` (decision (i)). Any other value below it is warned (underwrite) or raised (pricing); a hold with none gets the floor in pricing | `capital_plan.recurring_capex.per_unit_annual` (none counts as 0) |

Broker notes and trailing actuals are anchors a caller passes in `context`;
they can only raise a floor. `apply_guardrails(canonical, *, mode, profile,
context=None)` returns `(canonical_copy, findings)`:

- `mode="underwrite"`: no value changes. Each value below its floor is a
  finding, recorded on `assumptions.entries[key].guardrail` with
  `status: "below_floor"`, `action: "warned"`, the floor, its source and the
  lowest value. The validator emits a `GUARDRAIL_BELOW_FLOOR` WARNING; never an
  ERROR.
- `mode="pricing"`: each value below its floor is raised to it, per segment for
  curves (segments at or above the floor keep the analyst's value) and with
  `max` for scalars. Every raise is a finding with original and applied values,
  appended to `policy_overrides` in backsolve. The entry records
  `action: "enforced"`, takes the floor's source, and its `override` pins the
  priced value (note "pinned at the house floor enforced by the pricing
  guardrail") so a later re-resolution keeps it. The pre-floor value stays in
  `guardrail.original`. An entry without an override always has
  `value == default_value`.

`resolve_assumptions(mode=...)` calls the same module through one hook, so an
ingested canonical carries underwrite warnings. Backsolve prices on the
analyst's curves and applies only the shared floors (decision (a), below):

- Floors only raise, per segment, as `max(analyst value, floor)`. They never
  lower or reshape the analyst's input.
- Loss to lease: never re-derived from market vs in-place rent or zeroed.
  Each segment of a cohort with a loss-to-lease gap (market rent above
  in-place) is floored, and such a cohort with no row gets one at the floor
  for the analysis period. A cohort with no gap is exempt (`exempt_no_gap`):
  its rows are kept as entered, and the exemption is disclosed on the
  `loss_to_lease` entry's basis and in `house_adjustments.guardrails.exemptions`.
- Collection loss: every segment is floored (never flattened to one row); a
  hold with no curve gets one row at the floor.
- Concessions: the schedule and its later-year rows are kept. A year-1 row
  below the floor is raised to it; a row that runs past year 1 is split at
  the year-1 boundary so its later months keep the analyst's rate. A
  `free_months` row is compared as months / 12 and keeps its type (a raised
  row carries the floor's months equivalent). With no year-1 row, one year-1
  `pct_rent` row at the floor is added.
- Reserves: `max(analyst, house reserve)` per segment, never forced down.
- Recurring capex: `max(value, vintage band)`; an analyst figure below the
  band is priced as given and disclosed in `policy_overrides` (the insurance
  waiver convention). A PCA figure below the band is raised to it and
  disclosed: `{input: "recurring_capex_per_unit", action:
  "floor_applied_over_deal_evidence", floor, floor_source, evidence, original,
  applied}`; the entry is pinned at the floor like an enforced one. The
  reserve floor never stacks on it.
- Vacancy, insurance and R&M are as described in the table above.

Its solved canonical is re-resolved in `mode="pricing"` so `assumptions`
matches the priced values and lists the enforced floors.

Value-add pricing basis (decision (b)): the backsolve policy's `coc_basis` is
`"year_1"` (default for the cashflow strategy) or `"stabilized_year"` (default
for value_add), and can be overridden. On the stabilized-year basis the target
is the post-debt cash-on-cash of the first stabilized analysis year:

- with renovation programs or unit renovations, the first full 12-month
  analysis year after the last unit is renovated;
- otherwise `find_stabilized_year` (engine/modules/metrics.py) on the analysis
  years, with each year's unit-weighted vacancy against the hold's last-month
  vacancy and its operating CapEx against price plus closing costs;
- the year must be a full year before the exit year, or the solve refuses.

For value_add the renovation budget (program units x cost per unit, plus
`unit_renovations`) is funded with equity at close: the priced canonical
carries `renovation_capex_funding: "equity_at_close"`, `total_equity_basis`
and `total_unlevered_basis` include the budget, and the engine keeps it out of
operating cash flow (shown as `equity_funded_capex`). The summary discloses
`coc_basis` and `renovation_funding`.

Every value backsolve's pricing sets outside the floors is labeled by who set
it, never as an `analyst` edit: an override with a note naming the rule pins
the priced value, and `restore_pricing_labels` keeps the label on
re-resolution.

| Set by | Entry source | Override note |
|---|---|---|
| The pricing policy: fees, purchase and partnership closing costs, loan closing costs, sale cost, an exit cap the policy carries (an explicit policy value, or a default where the canonical carries no analyst value) | `house:backsolve_policy` | "set by the backsolve pricing policy" |
| A pricing normalization (the house box-score rebase of loss to lease and vacancy; an opex line backsolve's preparation sets) | `house:pricing_normalization` | "rewritten by a backsolve pricing normalization" |
| The real estate tax reassessed at the priced price | `house:property_tax_module` | "reassessed by the property tax module at the priced purchase price" |
| An opex line a floor raised (insurance line, R&M top-up) | the floor's source | "opex line set by a house floor the pricing guardrail enforced" |
| The policy's `insurance_per_unit_override` | `analyst` | "analyst insurance_per_unit_override in the backsolve policy" |

The policy defaults never replace an analyst value. For each pricing-policy
key (`engine/backsolve.PRICING_POLICY_KEYS`: sale cost, fees, purchase,
partnership and loan closing costs) backsolve prices an explicit policy value
(`policy`, `--policy-json`, or a CLI flag that was given) when there is one,
else the canonical's value when its entry is `analyst`-sourced (an ingest
`--sale-cost`, an override, a resolved hand-authored value), else
`engine/backsolve_policy.DEFAULTS`. An analyst value is priced as given and
keeps its `analyst` label; only explicit policy values and defaults are
labeled `house:backsolve_policy`. The exit cap is the policy's, else the
analyst's, else entry-anchored and re-measured at every candidate price (§3,
decision (e)). A canonical with no `assumptions`
section records no provenance, so the defaults apply to it as before.
`effective_assumptions.policy_value_sources` discloses who set each value.
Earlier, the policy defaults replaced an analyst value (an ingest
`--sale-cost` of 3% was priced at 2% and relabeled `house:backsolve_policy`).

Every raise a floor makes is listed in `policy_overrides` with its original and
applied values, and the entry carries the floor's source and a `guardrail`
record with `action: "enforced"`, the floor and its `floor_source`. An entry
the pricing guardrail enforced keeps its guardrail label.

An explicit analyst insurance figure below the $600 floor is priced as given
(no price change) and disclosed: `policy_overrides` gets
`{input: "insurance_per_unit", action: "floor_waived_by_analyst_override",
floor, floor_source, original, applied}`, and the entry a `guardrail` record
with the same action. A later resolution honors a recorded waiver while the
insurance line still carries the pinned figure; the validator does not warn on
it.

### 6. Opex treatment

- Each opex line keeps its T12 provenance (`source_rows`) and is displayed in
  annual dollars and per unit, whatever its `calculation_type`.
- Growth defaults come from a house table per category, e.g. real estate tax
  from the property-tax reassessment module, insurance at a house trend,
  payroll at wage growth, utilities, R&M, and management fee as a percent of
  EGR with no growth. Every rate is a named, documented constant in
  `underwriting_policy.py`.

### 7. Market-data providers

A protocol lives in `engine/market_data/`:

```python
class MarketDataProvider(Protocol):
    name: str
    def available(self) -> bool: ...
    def get(self, metric: str, geo: Geo, *, as_of: date | None = None) -> Observation | None: ...
```

- An `Observation` carries `metric`, `geo`, `value` (a scalar or a per-period
  series), `unit`, `horizon`, `as_of`, `confidence`, and `provenance` (file
  path, vintage, content hash).
- Metrics v1: `rent_growth_forecast`, `vacancy_current`,
  `vacancy_stabilized`, `concession_rate`, `market_cap_rate`,
  `effective_rent_by_bed`, `renovation_premium`, `supply_risk`.

Providers:

- `PlatMarketStudyAgentProvider` reads that repo's output files from
  `PLAT_MARKET_STUDY_ROOT` (default: sibling `plat-market-study-agent`).
- `PlatSupplyDemandProvider` reads forecast parquet from
  `PLAT_SUPPLY_DEMAND_ROOT` (default: sibling `plat-supply-demand`).
- Neither one imports the feeder packages. Parquet reading needs the optional
  `market` extra (pandas, pyarrow); without it a provider reports
  `available() == False`.
- Placeholder providers for proprietary or paid sources (`CoStarProvider`,
  `YardiMatrixProvider`, `RealPageProvider`, `UserFileProvider` for an
  analyst's own CSV/JSON) implement the protocol and report unavailable until
  configured.
- A `SyntheticProvider` backs all tests.
- Provider order is configurable (`engine/market_data/config`). Default:
  user-configured paid providers, then market-study-agent, then
  supply-demand. For each metric, the first provider with data wins, and
  every result is recorded in `candidates`.
- The dangling `engine.market_data.costar_cap_rates` import is replaced by
  `market_cap_rate` through the registry.

### 8. Inspect and edit

- Every run writes an assumptions sheet: `assumptions.json` and an Excel
  "Assumptions" tab. It shows key, value, unit, source, basis, confidence,
  default value, override, and the delta.
- Analysts change assumptions by editing the sheet or by passing
  `--assumption-overrides overrides.json`. The resolver re-applies them, keeps
  `default_value`, and records the override. An Excel tab edited and read back
  round-trips through `excel_io`.

As implemented (slice 7, `engine/assumptions/sheet.py`):

- `sheet_rows(canonical)` builds one row per entry: `key`, `value`, `unit`,
  `source`, `basis`, `as_of`, `confidence`, `review_required`,
  `default_value`, `delta`, `override_value`, `override_note`,
  `override_cohorts`, `guardrail_status` and `extra`. Rows follow the
  registry order (`ASSUMPTION_KEYS`), then any other key A→Z. `delta` is
  `value − default_value` for numeric scalars, element-wise for numeric
  vectors of the same length, and null otherwise. Rows are built from the
  entry shape alone: vector and dict values are kept as is, an entry's
  `guardrail` (an object with `status`, or a plain status) fills
  `guardrail_status`, and every field the sheet has no column for (for
  example `guardrail` or `per_unit_annual`) is kept in `extra`. `candidates`
  stays in the canonical.
- The JSON sheet (`write_assumptions_json`, sheet version
  `plat.assumptions.sheet/2`) is
  `{sheet_version, resolver_version, mode, deal_id, instructions, columns, rows}`
  with no timestamps, so the same canonical always writes the same bytes.
  Each written row adds a read-only `exported_override` (the row's override
  value when the sheet was written) to the `sheet_rows` fields.
- The Excel tab "Assumptions" holds an Excel table `tbl_assumptions` with
  the same columns (the last one is "Exported Override"). Scalars are numbers
  (percent or dollar formats by unit), and vectors and dicts are JSON text.
  The Value, Override and Note columns are the editable ones, for every row
  including `opex.<slug>.base` (annual dollars, or the percent for a
  percent-of-revenue row). `read_assumption_overrides_xlsx` reads the table
  through `engine.excel_io`, and falls back to scanning the tab when an editor
  has dropped the table. Override cells take numbers, `2.5%` text or JSON text.
- Reading an edited sheet (`read_assumption_overrides` /
  `read_assumption_overrides_xlsx`) compares each row with its
  `exported_override`:
  - an Override changed from the export becomes
    `{key: {"value": override_value, "note": override_note}}`, and wins over
    an edited Value;
  - with the Override as exported, an edited Value (different from the
    exported override, or from `default_value` when there was none) becomes
    the override, so editing the Value column always works;
  - a blanked Override on a row that had one becomes `{key: null}`, which
    clears the override (§2), as does a Value set back to `default_value` with
    the Override blank;
  - an Override left as exported is passed on unchanged.

  A version-1 sheet (no `exported_override`) is read as before: a set Override
  wins, else a Value that differs from `default_value` becomes an override. A
  plain `{key: value | {"value", "note"} | null}` overrides file is passed
  through.
- Artifacts: `runs/ingest_deal.py` writes `<output stem>.assumptions.json`
  next to the output JSON, and `--assumption-overrides` takes a plain
  overrides JSON, an edited sheet JSON or an `.xlsx`.
  `engine.excel_output.write_results_to_workbook` adds the "Assumptions"
  tab, `engine.engine.run_from_files` writes `<outputs stem>.assumptions.json`,
  the backsolve CLI writes `assumptions.json` into its output folder, and
  `engine.api.handle_run_deal` returns the same payload as
  `assumptions_sheet`. Each happens only when the canonical has an
  `assumptions` section.
- `runs/assumptions_sheet.py export|apply` exports the sheet (JSON and/or
  Excel) and re-resolves a canonical with an edited sheet as overrides.

As implemented (the assumption register, UPL-48): one view of every
assumption, rendered on every analyst surface.

- `engine/assumptions/register.py` `build_register(canonical)` returns a
  deterministic, JSON-serializable document
  (`plat.assumptions.register/1`): the deal profile, summary counts (total,
  from deal evidence, from market, house defaults, engine fallbacks, edited by
  the user, needing review, guardrail warned / enforced / waived / exempt,
  missing rationales) and one row per entry with key, label, group, value and
  unit (plus a compact display of vectors and curves), the default value and
  its source (for an overridden entry, the default provenance kept on its
  `analyst` candidate), why (the entry basis and the policy rationale for the
  default's source), the candidates, the user edit (override value, note,
  `edited_by`, `edited_at`, whether it differs from the default and the
  delta: numeric, element-wise, or per field for curve objects), a value a
  pricing rule pinned (not a user edit), guardrail status, review flag,
  confidence, as-of and any other entry fields. It is generic: a key it does
  not know is grouped by prefix or token (else "Profile/Other") and labeled
  from the key. Rows are ordered by group, then the sheet key order.
- `engine/assumptions/rationale.py` maps each source (exact, or by the
  `house:vintage_` and `market:` prefixes) to a 1-3 sentence rationale. A
  source with none renders "No policy rationale recorded for this source yet"
  and is flagged; `tests/test_assumption_register.py` greps
  `engine/assumptions/*.py` and `engine/backsolve.py` for every source string
  and fails on one without a rationale.
- Edit metadata: an override may carry `edited_by` (a name) and `edited_at`
  (an ISO date). The resolver accepts and validates them
  (`registry.override_metadata`), keeps them on the override, and carries them
  through re-resolution; values and idempotence are unchanged.
  `sheet.stamp_overrides` gives new or changed overrides the caller's editor
  and date and lets an override left as recorded keep its own;
  `runs/assumptions_sheet.py apply --editor NAME [--edit-date D | --stamp-date]`
  uses it. Only `--stamp-date` reads the clock, in the CLI; resolution never
  does.
- `engine/assumptions/register_html.py` renders `assumptions.html`: one
  self-contained page (inline CSS and a few lines of vanilla JavaScript, no
  external assets) with the profile and counts, filter chips (All / Edited by
  user / Needs review / House defaults / Deal evidence / Market / Guardrail
  flags), and a table grouped by group with the columns Assumption | Value |
  Default (source) | Why this default | User edit | Flags, edited rows
  highlighted and the candidates expandable. All text is escaped. It supports
  light and dark (`prefers-color-scheme`), print and narrow screens, and has
  no timestamp beyond the dates in the data.
- The Excel "Assumptions" tab is the register in a workbook: it adds Group,
  Default Source, Why This Default, Edited By / At and Flags (read-only)
  columns, sorts rows by group, freezes the header, highlights edited rows and
  marks review flags. `tbl_assumptions`, the editable Override / Note / Value
  cells and `read_assumption_overrides_xlsx` are unchanged: the reader maps
  columns by header and returns rows in the sheet key order.
- Every run writes the page: `runs/ingest_deal.py` and
  `runs/assumptions_sheet.py apply` write `<output stem>.assumptions.html`,
  `runs/run_deal.py` writes `assumptions.html` into its output folder, and
  `engine.engine.run_from_files` writes `<outputs stem>.assumptions.html`,
  each next to the JSON sheet; `runs/assumptions_sheet.py html <canonical>
  [-o out.html]` writes it on demand. `engine.api.handle_assumption_register`
  returns the register JSON (optionally filtered, without candidates, or with
  the HTML), and the MCP tool `assumption_register` returns it without
  candidates by default to keep the response bounded.

### 9. Validation

- A synthetic deal library covers at least two stabilized and two value-add
  deals across vintages, with synthetic provider data.
- Tests assert that resolved assumptions fall in expected bands, that the
  full engine run passes, and that overrides round-trip.

As implemented (slice 8): `tests/fixtures/deals/<slug>/` holds four invented
deals (stabilized 1990s with market data, stabilized 2010s with none,
value-add 1970s with a renovation program, value-add 1980s with MSA-only data
and GL collisions), each a consistent rent roll and T12 plus a `deal.json`
with the profile, economics, analyst inputs, synthetic market data and the
expected bands. `build_fixtures.py` generates them deterministically.
`tests/test_deal_library.py` ingests each deal with no hand edits and checks
the ingest gate, sources and review flags, the bands, the engine run, the
underwrite guardrail warnings, backsolve on one deal of each type, a sheet
override round-trip, determinism, and a golden `expected_assumptions.json`
(regenerated only with `build_fixtures.py --update-goldens`).

## Implementation slices (stacked PRs)

1. ADR (this document).
2. Schema plus resolver v1: `deal_profile` and `assumptions` sections, and a
   resolver that gathers the existing house defaults and ingest fallbacks
   with provenance. No numeric behavior change.
3. Market-data providers: protocol, the two feeder providers, paid
   placeholders, synthetic provider, and the cap-rate import fix.
4. Curves: per-year rent growth vector, vacancy trend-to-stabilized, and
   LTL/concession burn-off.
5. Opex per-category growth defaults and normalized display.
6. Guardrail modes: warn in underwrite, enforce in pricing, disclose.
7. Assumptions sheet export and import (JSON and Excel) with overrides.
8. Synthetic deal library and expected-band tests.

## Implementation status

Slices 1-8 are implemented on the stacked PRs, and an independent audit's
gaps were filled: the sheet clears overrides and honors Value edits, the
override contract (null clears, cohort keys, opex base overrides) is in the
resolver, the R&M top-up is sized from the analyst lines on re-pricing, and
every value backsolve's pricing sets is labeled and disclosed. The four
decisions that were open with the product owner are decided and implemented:

1. **(a) Pricing follows the design rule (implemented, UPL-62).** Backsolve no
   longer rewrites loss to lease, concessions, collection loss or reserves
   before the floors. The house floors only raise, per segment, as
   `max(analyst value, floor)`, through the shared guardrails module (§5), so
   underwrite mode warns on the same floors. The legacy normalizations and
   their `policy_overrides` entries are gone; only the house box-score rebase
   is still labeled `house:pricing_normalization`. A cohort with no
   loss-to-lease gap (market rent at or below in-place) stays exempt from the
   loss-to-lease floor. This changes backsolve prices.
2. **(b) Value-add cash-on-cash basis (implemented, UPL-62).** Value-add
   pricing targets the post-debt cash-on-cash of the first stabilized
   analysis year, with the renovation budget funded by equity at close
   (`renovation_capex_funding: "equity_at_close"`); stabilized deals keep the
   Year-1 basis. The basis is the policy's `coc_basis`, defaulting by
   strategy, and is disclosed in the summary (§5). A value-add deal with a
   renovation program, whose Year-1 solve could refuse as non-monotonic,
   now converges over a wide price bracket. This changes value-add prices.
3. **(c) Real estate tax growth after reassessment (implemented, UPL-48).**
   The real estate tax row grows at `HOUSE_RE_TAX_GROWTH_AFTER_REASSESSMENT`
   (3%) from year 2 on the reassessed year-1 tax. The resolver writes it once
   into the row (`house:opex_growth_table`, overridable) and
   `engine/property_tax.py` keeps the row's growth rate, so it applies once.
4. **(d) Stabilized vacancy vs the vacancy floor (implemented, UPL-48).**
   The house stabilized vacancy is max(vintage typical-band midpoint, house
   vacancy floor) for the vintage and strategy, so a house-only deal is
   consistent with its own floor. A market stabilized vacancy is kept as
   resolved; the property-level vacancy anchor stays a guardrail (underwrite
   warns, pricing raises each segment below it).

Three later policy decisions (UPL-85, UPL-48), and the capex buckets that
completed (f):

5. **(e) Exit caps track entry caps.** The house exit cap is the going-in cap
   at the price being modeled plus 50 bps, re-measured at every backsolve
   price; only a rare, flagged steal (more than 150 bps over a known market cap
   rate) compresses it, by at most 100 bps (§3). Previously the going-in cap
   was measured once at the asking price, so a solved price far below the ask
   exited at a much lower cap than it entered at.
6. **(f) Replacement reserve vs economic CapEx.** The reserve is the house
   lender-style $250/unit below NOI (resolver default and pricing floor); the
   vintage economic CapEx is its own informational key that reaches the cash
   flows only when the analyst opts in (§3). Previously the reserve and its
   floor were the vintage economic CapEx (e.g. $1,000/unit for the 1980s).
   Superseded in part by (h): the vintage band now reaches the cash flows on
   every deal as recurring capex, with the reserve as its floor.
7. **(g) R&M floor basis.** The vintage R&M floor is compared with R&M plus
   turnover / make-ready, never payroll (§5). Previously only the R&M bucket
   was compared, so separately booked turns were topped up again.
8. **(h) Three capex buckets on every deal.** Older vintages need more
   capital every year, so every deal (stabilized and value-add) budgets it in
   three buckets (`capital_plan`, §3), replacing the informational
   `economic_capex_per_unit` of (f):
   1. Immediate / deferred capex at close: PCA > analyst > $0
      (`house:deferred_capex`), funded with equity at close (or a lender
      holdback). The engine adds it to the Day-0 bases (less
      `amount_in_stated_basis`, which backsolve's priced case states), so it
      lowers cash-on-cash and IRR; backsolve adds it to the total equity basis
      at every candidate price (a holdback upsizes the loan instead). It is
      shown beside, never inside, the renovation budget.
   2. Recurring capex by vintage: $/unit below NOI, grown at the R&M rate from
      analysis year 2; PCA > analyst > the vintage band (low / midpoint / high
      by deal type and `deal_profile.deferred_maintenance`); unknown vintage
      falls back to $250, flagged.
   3. The $250/unit lender reserve is a floor, not an addition: the engine's
      monthly charge below NOI is `max(recurring capex, reserve)`, carried in
      the cash flow's `replacement_reserves` line (the one place reserves have
      always entered), with `recurring_capex_by_vintage` and
      `replacement_reserve_floor` beside it and `results.capital_plan` showing
      the buckets, the charge by analysis year and which one binds.

   NOI stays before capex: year-1 NOI, the going-in cap rate, yield on cost
   and exit value (forward NOI / exit cap) are before capex; unlevered and
   levered cash flow, cash-on-cash, IRR and the equity multiple are after the
   below-NOI charge, with bucket 1 in the Day-0 outlay. DSCR and agency loan
   sizing were before capex too; decision (k) moves them to underwritten NCF. In pricing the vintage band is a floor
   (§5). The register shows the three buckets side by side
   (`build_register()["capital_plan"]` and a panel in `assumptions.html`).
   This lowers prices, most for older value-add deals.
9. **(i) Deferred maintenance minimums, a placeholder at close, and the band
   floor over a PCA.** `deal_profile.deferred_maintenance` used to move only
   recurring capex to the high end of the vintage band. Now:
   1. It raises the house floors and defaults for physical vacancy, collection
      loss, year-1 concessions and R&M (including turnover, excluding
      payroll), expressed against the vintage bands so the step scales with
      vintage (`engine/underwriting_policy.py`, "Deferred maintenance
      minimums"). "Floor" below is the minimum the same deal carries without
      the flag (the value-add floors already sit above the stabilized ones):

      | Metric | House minimum, 1980s stabilized, no flag | Deferred maintenance minimum (1980s stabilized) | Rule |
      |---|---|---|---|
      | Physical vacancy floor | 5.00% (stabilized default 5.75%, the typical midpoint) | 7.00% | max(`physical_vacancy_typical_high`, floor + 100 bp) |
      | Collection loss floor | 0.75% (default with no T12 bad debt: 1.00%) | 2.25% (also the default) | max(midpoint of the `bad_debt` typical band, floor + 50 bp) |
      | Year-1 concessions floor | 0.75% | 1.50% | max(midpoint of the `concessions` typical band, floor + 50 bp) |
      | R&M incl. turnover, ex payroll | $900/unit (band low end) | $1,700/unit | `r_and_m_high` |
      | Recurring capex | $1,000/unit (band low end) | $2,500/unit | `economic_capex_high` (unchanged) |
      | Economic vacancy (sum of the above + LTL 1.00%) | 7.50% | 11.75% (typical band 8%-14%) | disclosed by `deferred_maintenance_economic_vacancy`, never floored as a sum |

      The steps are `DEFERRED_MAINTENANCE_VACANCY_STEP` (100 bp),
      `DEFERRED_MAINTENANCE_COLLECTION_LOSS_STEP` and
      `DEFERRED_MAINTENANCE_CONCESSIONS_STEP` (50 bp each). For every vintage
      the deferred components sum to an economic vacancy inside the vintage
      economic-vacancy typical band and above the agency 5% minimum
      (`AGENCY_MIN_ECONOMIC_VACANCY`). The resolver, the guardrails (both
      modes) and backsolve's revenue floors read the flag. Sources: Fannie Mae
      Multifamily Guide Part II 203.01 (underwritten vacancy, concessions and
      bad debt at least the greater of the trailing 3-month collections gap and
      5% of GPR); Freddie Mac Guide ch. 17, 17.2(k) (vacancy and collection
      loss not less than 5%, adjusted upward when conditions require) and the
      Freddie SBL update of 2024-06-28 (an economic-vacancy allowance on top of
      physical vacancy for concessions or a delinquency or eviction history);
      Freddie Mac Guide ch. 62 (the PCA inspects at least half the down units
      and costs making them rent-ready); RealPage concessions report of
      2026-01-23 (Class C: 19.1% of units offering concessions, 10.5% average
      discount, the highest share of any class); NAA Income/Expense IQ 2024
      data, published 2025-12-22 (national R&M $1,098/unit, turnover cost
      +17.5% year on year). No public RealPage or Yardi Matrix series gives
      Class C bad debt as a percent of rent, so the collection-loss point is
      house judgment on the vintage band.
   2. With the flag and no PCA immediate-needs figure or analyst budget,
      immediate capex at close is a placeholder: one year
      (`HOUSE_DEFERRED_CAPEX_PLACEHOLDER_YEARS`) of the vintage
      `economic_capex_high` per unit, funded with equity at close
      (`house:deferred_capex_placeholder`, review_required). The validator
      warns `DEFERRED_CAPEX_PLACEHOLDER`, recommending a PCA; lenders escrow
      PCA immediate repairs at 125% of the estimate
      (`LENDER_PCA_REPAIR_ESCROW_FACTOR`; Fannie Mae Multifamily Guide 405.02
      completion/repair escrow guidance, Freddie Mac SBL priority repair
      escrow guidance). Without the flag nothing is budgeted
      (`house:deferred_capex`, $0).
   3. A PCA recurring capex below the vintage band pick is warned in
      underwrite (kept, with the evidence on the guardrail record) and, in
      pricing, raised to the band pick the house default uses,
      `max(PCA, floor)`, and disclosed as `floor_applied_over_deal_evidence`.
      A lender PCA sizes a lender replacement reserve for building systems
      and components over the loan term (Fannie Mae Guide 406.01; Freddie
      Mac SBL reserves of $200 / $250 / $300 per unit by needs rating),
      excluding routine items and discretionary upgrades, so on older stock
      it understates economic capex. This replaces the
      `floor_waived_by_deal_evidence` waiver, which is retired (still read
      from older canonicals). An analyst figure below the floor keeps the
      waiver.

   This lowers prices for flagged deals and for deals whose PCA recurring
   capex is below the band (library deal d: $1,500 raised to $1,750/unit).

10. **(j) The R&M floor's payroll review.** Payroll is payroll: oftentimes the
    whole site team's time is booked to payroll and the R&M lines are true
    repairs and maintenance, so by default payroll earns no credit toward the
    R&M floor. A credit needs evidence that maintenance labor is booked in
    payroll beyond normal staffing, both of:
    - payroll lines labeled as maintenance labor
      (`canonical_ops.is_maintenance_labor_label`: maintenance wages or
      salaries, maintenance or service technicians, repair staff, make-ready
      technicians, building engineers), read line by line from the T12 (the
      parser keeps each line's annual amount, `source_lines` on the T12 opex
      evidence); a T12 section taken whole that mixes maintenance labor with
      other payroll cannot be split and earns nothing;
    - total payroll per unit above `HOUSE_PAYROLL_BENCHMARK_PER_UNIT`
      ($2,000/unit/year: NAA Income/Expense IQ 2024 data, administrative plus
      payroll $2,323/unit nationally; NAA 2021 survey, salaries and personnel
      25% of garden opex, about $2,160/unit on the 2024 total of $8,657; NAA
      staffing best practice of one office and one maintenance employee per
      100 units).

    The credit is the lesser of the labeled maintenance payroll and the
    payroll above the benchmark. The floor test is R&M + turnover + credit;
    the top-up covers only the remaining shortfall. The review is always
    disclosed on the `r_and_m_payroll_credit` entry
    (`house:r_and_m_payroll_review`, unit $/unit/year; `detail` carries
    payroll per unit vs the benchmark, the labeled maintenance payroll lines,
    the credit and the resulting top-up), on the R&M guardrail record's basis
    and in backsolve's `policy_overrides` (`payroll_review`). It is
    `review_required` only in the ambiguous case: R&M is below the floor
    before any credit and payroll is above the benchmark. An analyst value for
    `r_and_m_payroll_credit` replaces the credit. A hand-entered wage line
    ("Maintenance Wages") is payroll and no longer counts toward the R&M
    basis. The synthetic library deals carry $1,150-$1,500/unit of payroll,
    below the benchmark, so no credit applies and their prices do not move.

11. **(k) Loan sizing on underwritten NCF.** Agency lenders size on NOI less
   replacement reserves, so the house sizes the DSCR constraint on NOI less
   the same below-NOI capex charge the cash flow deducts, max(recurring
   capex, lender reserve), and reports DSCR on that NCF
   (`metrics.dscr.basis`), with the NOI-based DSCR beside it
   (`metrics.dscr.noi_before_capex`). The basis is the overridable
   assumption `loan_sizing_noi_basis` (§3): `ncf_after_capex_charge`
   (`HOUSE_LOAN_SIZING_NOI_BASIS`), `ncf_after_lender_reserve` or
   `noi_before_capex`. Backsolve sizes every candidate price on it, from the
   projected year of the all-equity preview at that price, and discloses it
   (`summary.loan_sizing`); `results.capital_plan.metric_basis` names it. A
   charge that consumes the NOI sizes no loan rather than falling back to
   LTV. A DSCR-sized stabilized refinance (`refi_event`) uses the same basis:
   trailing NOI less the forward year's deduction from the refi date
   (`results.refi`: `noi_used`, `below_noi_deduction`, `sizing_cash_flow`,
   `sizing_noi_basis`, `deduction_window`); an LTV-sized refi values the
   property on NOI. The feasibility DSCR floors (1.10 hard, 1.20 and 1.30
   warn) are unchanged and apply to the reported basis, since agency DSCR
   minimums are stated on underwritten NCF; their reasons name the basis and
   the NOI-based DSCR. Where 1.25x DSCR binds the loan is smaller; where 65% LTV binds
   nothing changes. With a going-in cap below the debt constant (negative
   leverage) a smaller loan raises cash-on-cash, so a DSCR-bound solve can
   price higher.

Three decisions on deferred maintenance and PCA evidence:

## Consequences

Pros:

- An out-of-the-box run uses the best available context and says where every
  number came from.
- Defaults never silently override the analyst.
- Licensed data stays outside the public repo, behind provider configuration.

Cons:

- A larger canonical document.
- Providers depend on feeder file layouts; there is no versioned feeder
  contract yet. Provider tests pin the layouts with synthetic files.
- Lease-up and distressed profiles remain out of scope.
