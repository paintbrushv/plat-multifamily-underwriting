# Engine tax-regime schedules — `engine-tax-regimes/1.0.0`

**Task 4.2 (plan Item 4).** Deterministic, Decimal-based statutory tax
schedules for the four regimes researched in Task 4.1
(`tax-regime-research/1.0.0`). The engine owns every unit, rounding and
arithmetic decision; the harness selects and validates an approved regime
package and never recalculates tax. This module is a **standalone schedule
builder**: it does not alter the existing `engine.property_tax` millage
policy, `engine.py` run behavior, or any golden output.

```python
from engine.tax_regimes import build_tax_regime_schedule, supported_regimes

schedule = build_tax_regime_schedule({
    "state": "TX",
    "tax_year": 2026,
    "purchase_price": "12500000",
    "taxing_unit_rates_per_100": [{"unit": "county", "rate": "0.3545"}, ...],
    "special_assessments": [{"label": "BID", "amount": "12345.67",
                             "source_locator": "..."}],
})
schedule["annual_ad_valorem_tax"]      # Decimal-exact, ROUND_HALF_UP cents
schedule["statute_derived"]            # statute quantities, with citations
schedule["scenario_assumptions"]       # analyst-supplied inputs, echoed separately
schedule["requires_competent_human_review"]  # always True: research is not law
```

## Regimes and their researched mechanics

| State | Regime | Mechanics implemented |
|---|---|---|
| TX | `tx_ch23_market_value` | Jan-1 market-value appraisal at 100% (no assessment ratio, § 26.02); levy = sum of overlapping taxing units' per-$100 rates (§§ 26.04, 26.09); § 23.231 circuit breaker caps at prior + 20% + new improvements **and expires Dec 31, 2026** — any later tax year refuses `UNCERTAINTY_BLOCKS_STATUTORY_CLAIM` until re-researched; purchase price is evidence, never a percentage reset (§ 23.013). |
| CA | `ca_prop13_base_year` | Base year value at full cash value on change in ownership (art. XIII A § 2(a)); annual adjustment by the BOE-published CPI factor, capped at 2% (§ 2(b)) — the factor is per-year parcel input, a missing factor refuses; 1% constitutional rate cap plus voter-approved additions, never above the cap (§ 1(a)); § 75.11 supplemental counts (Jan–May → 2, Jun–Dec → 1); § 75.41 month-following date; new construction gets its own base year value. |
| FL | `fl_nonhomestead_10pct_cap` | 10% cap on assessment changes for all levies other than school district levies (art. VII § 4(h)); capped basis never exceeds just value; school millage applies to just value outside the cap; millage per $1,000 (§ 200.065); 10+-unit multifamily is § 193.1555 — 9-or-fewer refuses `UNSUPPORTED_TAX_RULE_FAMILY`; ownership/control change resets to just value (§ 193.1555(5)). |
| AL | `al_class_ii_millage` | Class II at 20% of fair and reasonable market value (Ala. Code § 40-8-1(a)); mills per $1,000 of **assessed** value; total millage below the 6.5 state mills refuses (unit confusion); a named jurisdiction is mandatory — no universal county fallback. |

## Refusals (typed, closed set)

- `UNSUPPORTED_TAX_REGIME` — unknown state; there is no "Standard" fallback.
- `UNSUPPORTED_TAX_RULE_FAMILY` — researched-out-of-scope statute (FL ≤9 units).
- `UNCERTAINTY_BLOCKS_STATUTORY_CLAIM` — TX circuit breaker after its Dec 31,
  2026 expiry; recorded uncertainty blocks instead of defaulting.
- `MISSING_EFFECTIVE_DATE` — missing `tax_year`.
- `INVALID_STATUTORY_VALUE` — null/negative/nonfinite/overflow values, unit-
  decorated numbers, missing CA CPI factor (never a hardcoded constant), a CA
  rate above the 1% cap, missing/null mandatory inputs (missing is never
  zero), FL null prior assessed value without a reset.
- `LOCAL_LEVY_INCOMPATIBLE` — a TX taxing unit named with a unit-of-measure
  word (e.g. `mills` — per-$100/per-$1,000 confusion), duplicate taxing units,
  missing levy list, or a missing/blank AL jurisdiction.

## Contract invariants

- **Money is Decimal, never float.** Every statutory input parses to a finite
  positive Decimal exactly representable as a JSON number; results quantize
  ROUND_HALF_UP to cents with overflow refusal.
- **Statute vs scenario separation.** `statute_derived` carries
  statute-derived quantities; `scenario_assumptions` echoes the
  analyst-supplied inputs; citations travel with the result.
- **Isolation.** Citation and config data are deep-copied into each result;
  mutating one result cannot leak into later builds.
- **Human review required.** Every result reports
  `requires_competent_human_review=True`; research alone is never production
  statutory approval.
- **Retrieval dates recorded.** All citations were retrieved `2026-09-21`;
  re-research at implementation time before relying on any figure — statutes
  drift (caps sunset, thresholds are CPI-adjusted annually).

## Parity with existing engine behavior

The legacy `engine.property_tax` millage policy is unchanged and tested at
parity: $10,000,000 at 25.31 mills via the legacy policy equals the TX
per-$100 schedule at 2.531 ($253,100.00). Binding tax regimes into
underwriting (Task 4.3, harness side) routes through this module; the engine
remains the only tax calculator.