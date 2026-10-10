# engine.market_data

Market-data providers for ADR 0003 section 7. Providers return
`Observation`s with provenance. A resolver uses them at ingest/resolve
time. `run_underwriting` never calls a provider, so the engine stays
deterministic.

## Metrics (v1)

| metric | unit | value shape | meaning |
| --- | --- | --- | --- |
| `rent_growth_forecast` | `decimal_yoy` | series by quarter (`2026Q3`) | forecast year-over-year effective rent growth |
| `rent_growth_long_run` | `decimal_yoy` | scalar | long-run (terminal) annual effective rent growth for the MSA |
| `vacancy_current` | `decimal` | scalar | latest actual physical vacancy |
| `vacancy_stabilized` | `decimal` | scalar | latest actual stabilized vacancy |
| `concession_rate` | `decimal_of_asking` | scalar | (asking - effective) / asking rent |
| `market_cap_rate` | `decimal` | series by quarter (actuals, then forecast) | market cap rate |
| `effective_rent_by_bed` | `usd_per_unit_month` | by bed (`studio`, `1br`, `2br`, `3br`) | effective monthly rent per unit |
| `renovation_premium` | `usd_per_unit_month` | scalar | monthly rent premium of a renovated unit over a classic unit |
| `supply_risk` | `decimal_of_inventory` | scalar | units under construction / inventory units |

All rates are decimals (`0.055` means 5.5%). Confidence is `high`, `medium`
or `low`.

## Which provider serves what

| provider | name | metrics | source |
| --- | --- | --- | --- |
| `UserFileProvider` | `user_file` | any | an analyst's CSV/JSON (`PLAT_MARKET_DATA_USER_FILE`) |
| `CoStarProvider`, `YardiMatrixProvider`, `RealPageProvider` | `costar`, `yardi_matrix`, `realpage` | none yet | placeholders; always unavailable |
| `PlatMarketStudyAgentProvider` | `plat_market_study_agent` | all but `renovation_premium` and `rent_growth_long_run` | `PLAT_MARKET_STUDY_ROOT` output files |
| `PlatSupplyDemandProvider` | `plat_supply_demand` | `rent_growth_forecast` | `PLAT_SUPPLY_DEMAND_ROOT` forecast parquet |
| `SyntheticProvider` | `synthetic` | any | in-memory (tests) |

Default order: configured user/paid providers, then
`plat_market_study_agent`, then `plat_supply_demand`. Override it with
`PLAT_MARKET_DATA_PROVIDERS=name,name,...` or `default_registry({"providers": [...]})`.
Parquet reading needs the `market` extra (`pip install ".[market]"`). Without
it the feeder providers report `available() == False`.

Feeder confidence is used when the feeder publishes one. The ensemble
diagnostics give `confidence_level` High/Medium/Low. For comps, the
`evidence_quality` median is used: `direct_playwright` is high,
`direct_cached` and `official_indexed` are medium, anything else is low.
Every other source is `medium`.

## Annualization

`annualize.py` turns quarterly series into per-analysis-year values. Analysis
year *k* is the 12-month window starting `analysis_start + 12(k-1)` months,
the same window `aggregate_analysis_years` uses.

- **YoY series** (`rent_growth_forecast`): the year's rate is the YoY value
  of the quarter that contains the window's last month.
- **QoQ series**: the year's rate compounds the four quarters whose last
  month falls in the window, `prod(1 + q) - 1`.
- **Level series** (`market_cap_rate`): `annual_value` is the mean of a
  calendar year's quarters.
- **Past the horizon**: `extend="hold_last"` (the default) repeats the last
  published value. `extend="none"` returns `None`. A year that ends before
  the first published quarter is always `None`.
