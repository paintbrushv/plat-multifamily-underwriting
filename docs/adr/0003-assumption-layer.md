# ADR 0003: Assumption Layer (Context-Driven, Inspectable Underwriting Assumptions)

## Status

Proposed

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
      "candidates": [{"source": "house:long_run", "value": 0.025}],
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
- `source` uses one vocabulary: `analyst`, `deal:rent_roll`, `deal:t12`,
  `market:<provider>`, `house:<rule>` (e.g. `house:vintage_1980s`), and
  `engine_fallback`.

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
| `rent_growth` | Years 1–2 from the submarket forecast, else the MSA forecast (8-quarter ensemble, annualized to analysis years). Years 3+ glide to the long-run rate (MSA long-run, else house long-run constant). Deal evidence is not used for growth. |
| `physical_vacancy` | Curve from current physical vacancy (rent roll) to stabilized vacancy over the profile's stabilization window. Stabilized = submarket stabilized vacancy if available, else house vintage typical. |
| `loss_to_lease` | Current LTL from the rent roll (market vs in-place) burns off over the lease term (12 months) to the house vintage LTL floor. |
| `concessions` | Year-1 rate from deal evidence (T12 concessions / GPR, or rent-roll specials), else submarket concession spread (asking − effective) / asking, else house vintage floor. Burns off to the submarket or house stabilized level. |
| `collection_loss` | T12 bad debt / GPR, else house vintage floor. |
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
- `growth_assumptions` gains an optional `annual_growth_rates`. Index 0 is the
  first analysis year, which the engine treats as year 1 with no growth;
  index *n* is the rate applied entering year *n+1*. `annual_growth_rate`
  stays as the scalar shorthand. The engine (UPL-44 path) grows flat curves by
  the vector when it is present.
- Assumptions are property-level by default. An entry may carry
  `cohort_overrides: {cohort_id: value}` for cohort-specific curves.

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

### 9. Validation

- A synthetic deal library covers at least two stabilized and two value-add
  deals across vintages, with synthetic provider data.
- Tests assert that resolved assumptions fall in expected bands, that the
  full engine run passes, and that overrides round-trip.

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
