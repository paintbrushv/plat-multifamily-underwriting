# ADR 0003: Assumption Layer (Context-Driven, Inspectable Underwriting Assumptions)

## Status

Accepted. Slices 1-8 are implemented; see "Implementation status" at the end
for the decisions that remain open.

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
| `market:<provider>` | A market-data provider: `plat_market_study_agent`, `plat_supply_demand`, `user_file`, `costar`, `yardi_matrix`, `realpage`, `synthetic` (tests); `market:supplied_benchmark` is the analyst-supplied `market_vacancy_benchmark` |
| `house:vintage_<bucket>` | `VINTAGE_FRAMEWORK_2026` for the year-built bucket (e.g. `house:vintage_1980s`) |
| `house:long_run_rent_growth` | The house long-run rent growth after the forecast horizon |
| `house:default_collection_loss` | The house default collection loss when the vintage is unknown |
| `house:exit_cap_spread` | Going-in cap rate plus the house exit spread |
| `house:opex_growth_table` | The house per-category opex growth table (also the 3% real estate tax growth) |
| `house:property_tax_module` | `engine/property_tax.py`: the real estate tax line reassessed at the purchase price |
| `house:management_fee_pct_egr` | The house management fee as a percent of EGR |
| `house:year1_insurance_floor` | The $600/unit Year-1 insurance floor |
| `house:vacancy_anchor` | The property-level vacancy anchor when no single anchor names the source |
| `house:backsolve_policy` | `engine/backsolve_policy` values: fees, closing costs, sale cost, and in pricing any value the backsolve policy sets (an explicit exit cap) |
| `house:pricing_normalization` | A backsolve pricing normalization (§5): loss to lease re-derived, concessions as one year-1 schedule, collection loss at the worst rate, reserves at the vintage reserve, the house box-score rebase |
| `engine_fallback` | An engine default when nothing else applies |

#### Overrides

`resolve_assumptions(overrides={key: value | {"value", "note"[, "cohort_overrides"]} | None})`:

- A value or `{"value", "note"}` sets the override; the entry keeps
  `default_value`, records `override`, and its value wins. Overrides already
  recorded in the canonical are kept on re-resolution.
- `None` (JSON `null`) **clears** the key's override: the entry returns to its
  resolver default. The assumptions sheet emits `{key: null}` when an analyst
  blanks a row's Override (§8).
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
| `insurance_per_unit`, `r_and_m_per_unit`, `replacement_reserve_per_unit`, `turn_cost_per_unit` | Deal evidence (annualized), else house vintage defaults. Floors are guardrails (§5). |
| `exit_cap_rate` | Analyst, else submarket market cap rate (forecast row at exit if present), else MSA, else the going-in cap rate plus the house spread. |
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
  (UPL-42), required tax millage is missing, or the deal has no unit cohorts.
  The validator emits an ERROR and the engine refuses to run.
- **Flags** are review prompts. They cover any entry from `house:*` or
  `engine_fallback`, provider data with low confidence, and values outside the
  submarket or vintage typical band. The validator emits a WARNING and
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
| `loss_to_lease` | max(vintage LTL floor, broker LTL) | each `loss_to_lease` segment whose cohort market rent exceeds in-place rent |
| `collection_loss` | max(vintage floor, broker bad debt) | each `collection_loss_curve` segment (no curve counts as 0) |
| `concessions` | max(vintage year-1 floor, broker concessions) | the year-1 rate (highest `pct_rent` / `free_months` row) |
| `insurance_per_unit` | $600/unit (`DEFAULT_YEAR1_INSURANCE_PER_UNIT`); waived by an explicit analyst insurance override, and the waiver is disclosed | the matched insurance line, annualized |
| `r_and_m_per_unit` | vintage R&M per unit | the R&M bucket, annualized; a shortfall is one top-up line sized from the other R&M lines (an existing top-up line never counts toward the floor) |
| `replacement_reserve_per_unit` | vintage reserve per unit | each `replacement_reserves` segment (none counts as 0) |

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
ingested canonical carries underwrite warnings. Backsolve keeps its pricing
normalizations (LTL re-derived from market vs in-place rent, collection loss
flattened to its worst rate, concessions as one year-1 schedule, reserves set
to the vintage reserve), then enforces the shared floors. Its solved canonical
is re-resolved in `mode="pricing"` so `assumptions` matches the priced values
and lists the enforced floors.

Every value backsolve's pricing sets outside the floors is labeled by who set
it, never as an `analyst` edit: an override with a note naming the rule pins
the priced value, and `restore_pricing_labels` keeps the label on
re-resolution.

| Set by | Entry source | Override note |
|---|---|---|
| The pricing policy: fees, purchase and partnership closing costs, loan closing costs, sale cost, an exit cap the policy carries | `house:backsolve_policy` | "set by the backsolve pricing policy" |
| A pricing normalization (LTL, concessions, collection loss, reserves, the house box-score rebase) | `house:pricing_normalization` | "rewritten by a backsolve pricing normalization" |
| The real estate tax reassessed at the priced price | `house:property_tax_module` | "reassessed by the property tax module at the priced purchase price" |
| An opex line a floor raised (insurance line, R&M top-up) | the floor's source | "opex line set by a house floor the pricing guardrail enforced" |
| The policy's `insurance_per_unit_override` | `analyst` | "analyst insurance_per_unit_override in the backsolve policy" |

Each normalization that changes an input is listed in `policy_overrides` as
`{input, normalization, source: "house:pricing_normalization", basis, original,
applied}`. An entry the pricing guardrail enforced keeps its guardrail label.

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
every value backsolve's pricing sets is labeled and disclosed. Known open
decisions, each pending with the product owner:

1. **Removing the pricing normalizations.** Backsolve still rewrites loss to
   lease, concessions, collection loss and reserves before the floors (now
   labeled `house:pricing_normalization` and listed in `policy_overrides`).
   Whether pricing should keep the analyst's curves and apply only the shared
   floors is undecided; removing them changes prices.
2. **Value-add backsolve cash-on-cash basis.** Value-add deals are priced on
   the same Year-1 post-debt cash-on-cash as stabilized deals; whether the
   basis should be a stabilized-year or renovation-adjusted return is open.
3. **Real estate tax growth after reassessment.** The property-tax module
   reassesses the year-1 tax at the purchase price; the growth applied after
   reassessment (house table rate vs the row's own rate vs none) needs a
   decision.
4. **Stabilized vacancy vs the vacancy floor.** A market or vintage-typical
   stabilized vacancy can sit below the property-level vacancy anchor;
   underwrite warns and pricing raises it. Whether the stabilized target
   should be floored at resolution is open.

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
