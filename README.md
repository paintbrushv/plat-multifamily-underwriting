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
| `engine/api.py` | Framework-free JSON request handlers (`handle_run_deal`, portfolio, refi-vs-sell). |
| `engine/mcp_server.py` | Installed `plat.underwriting.mcp/1` stdio adapter for validation, small summary metrics, and feasibility. |
| `runs/ingest_deal.py` | Canonical intake CLI: documents or agent-authored JSON -> validated deal JSON. |
| `runs/run_deal.py` | Run CLI: canonical deal JSON -> validation, engine results, assumptions sheet, workbook, key metrics. |
| `engine/assumptions/`, `runs/assumptions_sheet.py` | Assumption layer (ADR 0003): resolved assumptions with provenance, and the editable assumptions sheet (JSON and Excel). |

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
- `--validate` prints the validation report and exits 1 when it fails
  (warnings alone exit 0); the output JSON is still written so you can fix it.

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

`run_deal.py` validates (exit 1 with the report on any ERROR), runs the
engine and writes `canonical.json`, `validation.json`, `results.json`,
`assumptions.json` and, with `--xlsx`, `underwriting.xlsx` (with the
Assumptions tab). It prints NOI year 1, unlevered and levered IRR, equity
multiples, minimum DSCR and exit value. It can also apply overrides itself:
`--assumption-overrides output/overrides.json [--no-market-data]`. Exit codes:
0 ok, 1 validation failure, 2 bad input.

**4. Backsolve** the price that hits a target cash-on-cash return (house
pricing policy; the house floors are enforced and listed in the summary):

```bash
python -m engine.backsolve --canonical-json output/deal.json \
  --output-dir output/deal_backsolve --policy-version plat.backsolve-policy/1 \
  --strategy cashflow --year-built 1994 --target-coc 0.07 \
  --benchmark-5yr-treasury 0.04 --benchmark-as-of 2026-09-30 --benchmark-source "analyst entry"
```

It writes `backsolve_summary.json`, the priced canonical, its results and
`assumptions.json` into the output folder, and prints the status, solved
price, cash-on-cash basis and target, achieved cash-on-cash, levered IRR,
minimum DSCR, the number of floors enforced and those paths. It exits 2 when
the search is infeasible or exhausted.

### Market data

Providers are read at ingest/re-resolve time only; the engine never calls
them. Neither feeder ships data, and this repository never holds licensed data.

| Variable | Meaning |
|---|---|
| `PLAT_MARKET_STUDY_ROOT` | plat-market-study-agent outputs (default: sibling `../plat-market-study-agent`). |
| `PLAT_SUPPLY_DEMAND_ROOT` | plat-supply-demand forecast parquet (default: sibling `../plat-supply-demand`). |
| `PLAT_MARKET_DATA_PROVIDERS` | Provider order, comma-separated, e.g. `user_file,plat_market_study_agent,plat_supply_demand`. Default: configured user/paid providers, then the two feeders. |
| `PLAT_MARKET_DATA_USER_FILE` | An analyst's own CSV/JSON of observations (`user_file` provider). |

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
