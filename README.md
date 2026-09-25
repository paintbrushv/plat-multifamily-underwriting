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
| `runs/ingest_deal.py` | Canonical intake CLI: documents or agent-authored JSON -> validated deal JSON. |

Quick start — run the intake CLI on the synthetic fixtures:

```bash
python runs/ingest_deal.py --rent-roll tests/fixtures/sample_rent_roll.csv \
  --t12 tests/fixtures/sample_t12.csv --start 2026-05 --end 2031-04 \
  --property-id "Sample Deal" --output output/sample.json --validate
```

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
runs/            ingest_deal.py intake CLI
docs/            specs, module docs, tax regime schedules
```

## Contributing

Synthetic fixtures only — never commit real deal documents, property names,
tenant IDs, or credentials. See [CONTRIBUTING.md](CONTRIBUTING.md) and
[SECURITY.md](SECURITY.md). DCO sign-off required.

## License

Apache-2.0. See [LICENSE](LICENSE).
