# plat-multifamily-underwriting

Deterministic multifamily underwriting engine: rent roll / T12 ingestion,
a schema-validated canonical deal format, monthly cashflow + waterfall math,
and a statute-cited property-tax regime module. **Numbers come from the
engine, not from a model.** The same inputs always produce the same outputs.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## What this is

| Layer | Job |
|---|---|
| `engine/modules/` | Deterministic cashflow math: revenue, opex, debt, capex, renovations, fund waterfall, metrics (IRR, EM, DSCR, CoC, cap rates). |
| `engine/ingest/` | Rent roll, T12, and OM parsers -> canonical deal JSON. Refuses to invent missing values. |
| `engine/validator.py` | Schema v0.1 conformance + reference integrity + cohort-overlap gates. |
| `engine/property_tax.py`, `engine/tax_regimes.py` | Millage-based property tax projection with per-regime statute citations. |
| `engine/api.py` | Framework-free JSON request handlers (`handle_run_deal`, `handle_assumption_register`, portfolio, refi-vs-sell). |
| `engine/mcp_server.py` | Installed `plat.underwriting.mcp/1` stdio adapter for validation, small summary metrics, feasibility, and the assumption register. |
| `runs/ingest_deal.py` | Canonical intake CLI: documents or agent-authored JSON -> validated deal JSON. |
| `runs/run_deal.py` | Run CLI: canonical deal JSON -> validation, engine results, assumptions sheet, workbook, key metrics. |
| `engine/assumptions/`, `runs/assumptions_sheet.py` | Assumption layer (ADR 0003): resolved assumptions with provenance, the editable assumptions sheet (JSON and Excel), and the assumption register (`assumptions.html`). |

Quick start — run the intake CLI on the synthetic fixtures:

```bash
python runs/ingest_deal.py --rent-roll tests/fixtures/sample_rent_roll.csv \
  --t12 tests/fixtures/sample_t12.csv --start 2026-05 --end 2031-04 \
  --property-id "Sample Deal" --output output/sample.json --validate
```

### Assumptions sheet

Ingest resolves every underwriting assumption with its source, basis and
default (ADR 0003), and writes the sheet next to the output:
`output/sample.assumptions.json` for the command above. Inspect or edit it in
JSON or Excel, then re-resolve:

```bash
python runs/assumptions_sheet.py export output/sample.json --xlsx output/sample.assumptions.xlsx
# edit the Override / Note columns of the "Assumptions" tab (or a row's value in the JSON)
python runs/assumptions_sheet.py apply output/sample.json output/sample.assumptions.xlsx \
  --output output/sample.reresolved.json
```

Each edited row becomes an analyst override. The entry keeps its
`default_value`, records the override, and shows the delta. The same edited
file also works as `runs/ingest_deal.py --assumption-overrides <file.json|file.xlsx>`.
Underwriting workbooks written by `engine.excel_output` carry the same
"Assumptions" tab.

### Assumption register

The assumption register is the place to see every assumption the deal is
underwritten on, the house default behind it, why that is the default, and
what the analyst changed. Ingest, `run_deal.py` and `assumptions_sheet.py
apply` write it next to their outputs as `assumptions.html` (one
self-contained page: no network, works offline, prints cleanly, follows the
system light or dark theme), and `assumptions_sheet.py html <canonical.json>
[-o out.html]` writes it for any resolved canonical.

How to read it:

- The header shows the deal profile (deal type, MSA, submarket, vintage) and
  counts: assumptions from deal evidence, from market data, house defaults,
  engine fallbacks, user edits, rows needing review and guardrail flags.
- Chips filter the table: All, Edited by user, Needs review, House defaults,
  Deal evidence, Market, Guardrail flags.
- The table is grouped (Revenue, Vacancy & Credit, Opex, Capital/Capex, Exit,
  Debt/Fees, Profile/Other). Each row shows the value the model uses (vectors
  and curves compactly, such as `start 8.0% → 6.5% over 24 mo`), the default
  and its source, **why this default** (a plain-English policy rationale for
  that source plus how the value was computed), the user's edit (value, note,
  who and when, and the change from the default) and its flags. "Candidates
  considered" expands to every value the resolver weighed.
- Highlighted rows were edited by the user. "Needs review" marks house
  defaults and engine fallbacks; a guardrail flag means a value sits below a
  house floor (warned when underwriting, raised when pricing).

How to edit: the page is read-only. Change a value in the "Assumptions" tab of
the workbook or in `assumptions.json` (see above) and apply it; the register
is rewritten with the edit. The same tab carries the register's Group,
Default Source, Why This Default, Edited By / At and Flags columns, sorted by
group, with edited rows highlighted. To record who made an edit, pass
`--editor NAME` to `apply`, with `--edit-date YYYY-MM-DD` or `--stamp-date`
(today) for the date; an override file may also carry `edited_by` and
`edited_at` itself. Nothing else reads the clock, so the same canonical always
produces the same page. The register is also available as JSON from
`engine.api.handle_assumption_register` and the MCP tool
`assumption_register`.

## Underwrite a deal end to end

The walkthrough uses the synthetic deal-library documents in
`tests/fixtures/deals/a_stabilized_1990s/`. Rates are decimals (`0.0575` is
5.75%).

**1. Ingest** the rent roll and T12 with the deal economics, the property-tax
millage, the deal profile and the market geography:

```bash
D=tests/fixtures/deals/a_stabilized_1990s
python runs/ingest_deal.py --property-id SYN-A \
  --rent-roll $D/rent_roll.csv --t12 $D/t12.csv --start 2026-11 --end 2031-10 \
  --purchase-price 33120000 --ltv 0.60 --rate 0.0575 --amort 30 --io-months 24 \
  --exit-cap 0.0575 --millage 20 --assessment-ratio 0.7 \
  --deal-type stabilized --year-built 1994 --asset-class B --star-tier 3 \
  --msa "Larkspur Synthetic MSA" --metro-slug larkspur_sx --submarket "Larkspur North" \
  --output output/deal.json --validate
```

- `--millage` is the combined millage in mills per $1,000 of assessed value;
  `--assessment-ratio` (default 1.00) is assessed value / purchase price. The
  engine taxes `purchase price x ratio x mills / 1000`. A deal with a purchase
  price and no millage fails validation with `PROPERTY_TAX_MILLAGE_MISSING`,
  and `run_underwriting` refuses it.
- `--deal-type` is `stabilized` or `value_add`; with `--year-built` it picks
  the house vintage defaults. `--msa`, `--metro-slug`, `--submarket` and
  `--star-tier` form the geography market-data providers are asked for.
- Omit `--exit-cap` and the exit is still modeled at the analysis end: the
  resolver fills the cap rate (a market exit-year cap rate when a provider has
  one, otherwise a house fallback flagged for review). Ingest never invents
  one. A null `exit_cap_rate` is an unresolved placeholder: validation warns
  (`EXIT_CAP_RATE_PLACEHOLDER`) and the engine refuses to run it.
- Add `--no-market-data` to resolve from deal evidence and house policy only.
  Without feeder data or a user file (see [Market data](#market-data)), the
  rows market data would fill use house fallbacks: rent growth and
  stabilized vacancy here, and concessions or the exit cap when the deal has
  no T12 evidence or analyst value for them. Their source is `house:*` and
  the sheet flags them for review (`review_required: true`).
- `--validate` prints the validation report and exits 1 when it fails
  (warnings alone exit 0); the output JSON is still written so you can fix it.
- Capital is budgeted on every deal in three buckets (ADR 0003 §3), printed
  after ingest:
  1. **Immediate / deferred capex at close** (`capital_plan.immediate_capex`):
     one-time capital for deferred maintenance or PCA immediate needs, funded
     with equity at close (`--immediate-capex-funding lender_holdback` for a
     holdback). It raises the total equity basis, so it lowers cash-on-cash,
     IRR and the backsolve price; it never enters NOI. The value-add
     renovation budget stays its own budget. With `--deferred-maintenance` and
     no PCA or analyst figure, the house budgets a placeholder of one year of
     the vintage capex band high end per unit, flagged for review
     (`DEFERRED_CAPEX_PLACEHOLDER`: commission a PCA).
  2. **Recurring capex by vintage** (`capital_plan.recurring_capex`): $/unit a
     year below NOI, grown at the R&M rate (3%). The default is the vintage
     economic CapEx band: the low end for a stabilized deal, the midpoint for
     value-add, the high end with `--deferred-maintenance`.
  3. **Lender replacement reserve**: $250/unit a year below NOI. It is a
     floor, not an addition: the charge below NOI each month is
     max(recurring capex, reserve).

  Precedence is PCA > analyst > house: `--pca pca.json`
  (`{"immediate_capex", "recurring_capex_per_unit", "as_of", "reference"}`)
  beats `--immediate-capex` / `--recurring-capex-per-unit`. A deal built
  before 2000 without a PCA is flagged for review (`CAPEX_PCA_MISSING`). A PCA
  recurring capex below the vintage band is warned at ingest and raised to the
  band in backsolve pricing. NOI, the going-in cap rate and exit value are
  before capex; cash flow, cash-on-cash, IRR and the equity multiple are after
  it. DSCR and agency loan sizing use underwritten NCF, NOI less the same
  charge, as agency lenders do (`loan_sizing_noi_basis`: `ncf_after_capex_charge`
  by default, or `ncf_after_lender_reserve` / `noi_before_capex` as an
  override); the NOI-based DSCR is reported beside it
  (`metrics.dscr.noi_before_capex`). A DSCR-sized refinance (`refi_event`) and
  the feasibility DSCR floors use the same basis; the refinance sizes on
  forward NOI, the 12 months from the refi month (reading the projected year
  past the analysis end when it runs past it), less the same year's charge,
  and an LTV-sized refinance values the property on that forward NOI
  (`results.refi.noi_window`; `refi_event.noi_window: trailing` opts into the
  trailing `noi_months`).
- `--deferred-maintenance` also raises the house minimums for physical
  vacancy, collection loss, year-1 concessions and R&M to the upper part of
  their vintage bands (for a 1980s stabilized deal: 7.0%, 2.25%, 1.5% and
  $1,700/unit), in the defaults, the underwrite warnings and the pricing
  floors (ADR 0003 decision (i)).
- The R&M floor reviews payroll before it tops R&M up: payroll is payroll and
  earns no credit unless the T12 labels maintenance labor in payroll and
  payroll per unit is above the house benchmark ($2,000/unit). The review is
  shown on the `r_and_m_payroll_credit` row of the assumptions sheet, flagged
  when R&M is short and payroll is high; override the row to set the credit
  (ADR 0003 decision (j)).

  Every run also reports an exit-cap sensitivity grid
  (`results.exit_cap_sensitivity`, and an `Exit_Cap_Sensitivity` sheet with
  `--xlsx`): the base case at -50, -25, 0, +25, +50, +75, +100 and +150 bps
  around the exit cap it ran with (`HOUSE_EXIT_CAP_SENSITIVITY_OFFSETS_BPS`),
  each point pinned, with exit value, net sale proceeds, IRR and equity
  multiples per point.

  The exit is valued on forward NOI, as a buyer prices it: the projected NOI
  for the 12 months after the exit month over the exit cap (ADR 0003
  decision (n)). The engine projects that year past the analysis end for NOI
  only, with the hold's rent growth, curves at their terminal values and
  opex growth; the hold's cash flows, DSCR and cash-on-cash still end at the
  exit. `metrics.exit.exit_noi_window` shows the months used, and
  `exit_assumptions.noi_window: trailing` (the `exit_noi_window` row of the
  assumptions sheet) values the exit on the 12 months ending at the exit
  month instead.

**2. Review** `output/deal.assumptions.json` (one row per assumption: value,
unit, source, basis, default, override, delta, guardrail). Rows sourced from
`house:*` or `engine_fallback` are flagged for review. For Excel, run
`python runs/assumptions_sheet.py export output/deal.json --xlsx output/deal.assumptions.xlsx`.

**3. Override and run.** Edit the sheet's Override/Note columns, or write a
plain overrides file:

```json
{
  "rent_growth": {"value": [0.02, 0.03, 0.03, 0.03], "note": "submarket view"},
  "collection_loss": {"value": 0.015, "note": "T12 bad debt understated"}
}
```

```bash
python runs/assumptions_sheet.py apply output/deal.json output/overrides.json \
  --output output/deal.v2.json
python runs/run_deal.py output/deal.v2.json --output-dir output/deal_run --xlsx
```

An overridden row keeps its `default_value`. To drop an override, blank its
Override cell (or set the key to `null` in the overrides file) and apply
again: the row returns to its full default, market data included, with its
source:

```bash
echo '{"rent_growth": null}' > output/clear.json
python runs/assumptions_sheet.py apply output/deal.v2.json output/clear.json \
  --output output/deal.v3.json --no-market-data
```

`apply` re-resolves with the market-data registry
(`engine.market_data.default_registry()`), like ingest; `--no-market-data`
skips it. Either way, market values already in the canonical are kept when
the registry has no data for them.

`run_deal.py` validates (exit 1 with the report on any ERROR), runs the
engine and writes `canonical.json`, `validation.json`, `results.json`,
`assumptions.json`, `assumptions.html` (the assumption register) and, with
`--xlsx`, `underwriting.xlsx` (with the Assumptions tab). It prints NOI year 1, unlevered and levered IRR, equity
multiples, minimum DSCR and exit value. It can also apply overrides itself:
`--assumption-overrides output/overrides.json [--no-market-data]`. Exit codes:
0 ok, 1 validation failure, 2 bad input.

**4. Backsolve** the price that hits a target cash-on-cash return (house
pricing policy; the house floors are enforced and listed in the summary):

```bash
python -m engine.backsolve --canonical-json output/deal.json \
  --output-dir output/deal_backsolve --policy-version plat.backsolve-policy/1 \
  --strategy cashflow --year-built 1994 --target-coc 0.08 \
  --benchmark-5yr-treasury 0.04 --benchmark-as-of 2026-09-30 --benchmark-source "analyst entry"
```

It writes `backsolve_summary.json`, the priced canonical, its results and
`assumptions.json` into the output folder, and prints the status, solved
price, cash-on-cash basis and target, achieved cash-on-cash, levered IRR,
minimum DSCR, the number of floors enforced and those paths. It exits 2 when
the search is infeasible or exhausted. Fees, closing costs and the sale cost
are the canonical's analyst values when it has them (the ingest `--sale-cost`,
an override), else the policy defaults; a flag such as `--sale-cost-percent`
overrides both (see `docs/BACKSOLVE_API.md`).

### Market data

Providers are read at ingest/re-resolve time only; the engine never calls
them. Neither feeder ships data, and this repository never holds licensed data.

| Variable | Meaning |
|---|---|
| `PLAT_MARKET_STUDY_ROOT` | plat-market-study-agent outputs (default: sibling `../plat-market-study-agent`). |
| `PLAT_SUPPLY_DEMAND_ROOT` | plat-supply-demand forecast parquet (default: sibling `../plat-supply-demand`). |
| `PLAT_MARKET_DATA_PROVIDERS` | Provider order, comma-separated, e.g. `user_file,plat_market_study_agent,plat_supply_demand`. Default: configured user/paid providers, then the two feeders. |
| `PLAT_MARKET_DATA_USER_FILE` | An analyst's own CSV/JSON of observations (`user_file` provider). |

Without feeder data or a user file, the resolver uses house fallbacks and the
assumptions sheet flags those rows for review (`house:*` source,
`review_required: true`).

**Feed your own market data** with the `user_file` provider: point
`PLAT_MARKET_DATA_USER_FILE` at a JSON (or CSV) file of observations. Each
observation has a `metric` (see `engine/market_data/README.md`), a `value`
(a decimal rate, or an object keyed by quarter such as `"2027Q3"` for a
series), a `geo` (any of `cbsa_code`, `metro_slug`, `submarket`,
`star_tier`; every field set must match the deal profile), and optional
`as_of` and `confidence` (`high`, `medium`, `low`; default `medium`). A
minimal file for the walkthrough deal:

```json
{
  "observations": [
    {"metric": "rent_growth_long_run", "geo": {"metro_slug": "larkspur_sx"}, "value": 0.028, "as_of": "2026-09-30"},
    {"metric": "vacancy_stabilized", "geo": {"metro_slug": "larkspur_sx", "submarket": "Larkspur North"},
     "value": 0.055, "as_of": "2026-09-30", "confidence": "high"},
    {"metric": "market_cap_rate", "geo": {"metro_slug": "larkspur_sx"}, "value": 0.0585, "as_of": "2026-09-30"}
  ]
}
```

Save it as `output/my_market.json` and re-run step 1 with it (drop
`--exit-cap` to let the market cap rate fill the exit):

```bash
PLAT_MARKET_DATA_USER_FILE=output/my_market.json python runs/ingest_deal.py --property-id SYN-A \
  --rent-roll $D/rent_roll.csv --t12 $D/t12.csv --start 2026-11 --end 2031-10 \
  --purchase-price 33120000 --ltv 0.60 --rate 0.0575 --amort 30 --io-months 24 \
  --millage 20 --assessment-ratio 0.7 \
  --deal-type stabilized --year-built 1994 --asset-class B --star-tier 3 \
  --msa "Larkspur Synthetic MSA" --metro-slug larkspur_sx --submarket "Larkspur North" \
  --output output/deal_mkt.json --validate
```

The rent growth, physical vacancy and exit cap rows now show
`market:user_file`, with the file's `as_of` and confidence. Once configured,
`user_file` comes first in the default provider order. For each metric the
first provider with data wins, and every provider's observation is kept as a
candidate. Set `PLAT_MARKET_DATA_PROVIDERS` to choose the order or the subset:
`PLAT_MARKET_DATA_PROVIDERS=user_file` uses only your file, and
`plat_market_study_agent,user_file` prefers the feeder and falls back to your
file.

Parquet reading needs `pip install -e ".[market]"`; without it the feeder
providers report unavailable. Paid sources plug in through the placeholder
providers in `engine/market_data/providers/placeholders.py`
(`CoStarProvider`, `YardiMatrixProvider`, `RealPageProvider`, configured by
`PLAT_COSTAR_CREDENTIALS`, `PLAT_YARDI_MATRIX_CREDENTIALS`,
`PLAT_REALPAGE_CREDENTIALS`). They report unavailable until a licensed
adapter implements `available()` and `get(metric, geo, as_of=...)`. See
`engine/market_data/README.md` for the metrics and the provider contract.

### Value-add renovation programs

Ingest with `--deal-type value_add` and add the programs with
`--partial-json` (the `renovation_programs` schema in
`engine/schemas/deal_schema_v0_1.json`). `target_cohort` is a cohort id from
the ingested rent roll (e.g. `1br1ba`); the renovated units move to
`output_cohort` at `rent_premium_monthly` over the classic rent:

```json
{
  "renovation_programs": [
    {
      "program_id": "interior_1br",
      "program_name": "1BR interior renovation",
      "target_cohort": "1br1ba",
      "output_cohort": "1br1ba_reno",
      "renovation_cost_per_unit": 11500,
      "rent_premium_monthly": 175.0,
      "downtime_days": 30,
      "strategy": "proactive",
      "start_month": "2027-01",
      "monthly_pace": 3,
      "max_units": 50
    }
  ]
}
```

`strategy` is `proactive` (renovate at `monthly_pace`) or `on_turnover`
(renovate units as leases turn). Optional fields: `end_month`, `max_units`,
`min_monthly_pace`, `post_renovation_market_rent` and
`seasonal_downtime_multipliers`. A `--partial-json` file may also carry
`metadata.property_summary.property_tax_policy` instead of `--millage`.

For the optional MCP adapter, install `plat-multifamily-underwriting[mcp]`
and run `python -m engine.mcp_server` from any working directory. It exposes
three read-only tools and does not persist a deal or emit workbook artifacts.
Callers should pin the producer version and package contents before relying
on its results. The feasibility response evaluates IRR, DSCR, and LTV; a
missing purchase price or debt commitment leaves LTV unavailable and fails
that gate.

## Honest limitations

- **Synthetic-only validation.** Every fixture in this repository is
  synthetic. No live offering memorandum, rent roll, or T12 is shipped, and
  parser coverage is not claimed beyond the synthetic fixtures and formats
  in `tests/`.
- **Property tax regimes require human review.** `engine/tax_regimes.py` and
  `docs/TAX_REGIME_SCHEDULES.md` cite statutes, but rates, assessment
  ratios, and revaluation cadences must be reviewed by a qualified
  professional per jurisdiction before production use.
- **No invented numbers.** Missing millage, missing rent, unknown unit
  status -> blockers and `None`, never silent zeros.
- **LLM OM extraction is opt-in.** The default install parses nothing with a
  hosted model. The `om-llm` extra enables the Anthropic-based OM extractor;
  the core engine never imports it.

## Layout

```
engine/          deterministic underwriting modules + canonical schema
engine/schemas/  deal_schema_v0_1.json (canonical deal format)
engine/ingest/   rent roll / T12 / OM parsers
tests/           synthetic fixtures only
runs/            ingest_deal.py intake, assumptions_sheet.py export/apply, run_deal.py run
docs/            specs, module docs, tax regime schedules
```

## Contributing

Synthetic fixtures only — never commit real deal documents, property names,
tenant IDs, or credentials. See [CONTRIBUTING.md](CONTRIBUTING.md) and
[SECURITY.md](SECURITY.md). DCO sign-off required.

## License

Apache-2.0. See [LICENSE](LICENSE).

## v0.1 packaging candidate

The MCP adapter uses MCP 2.3–2.x. Install the `[mcp]` extra and run `plat-underwriting-mcp` (stdio).

CI builds wheel/source archives, tests a fresh wheel environment, and validates runtime/package versions. A release tag must match `pyproject.toml`, use an unused PyPI version, and pass the full CI workflow before the tested artifacts can be uploaded. Candidate versions are not published by this change.
