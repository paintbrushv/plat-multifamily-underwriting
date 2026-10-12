Excellent. Below is the **Canonical Deal Schema v0.1**, written as a **formal, approval-ready specification**.

This is the contract.
Everything downstream (Excel, engine, Power Query, web UI later) must conform to this.

I’ve kept it:

* conservative
* explicit
* table-driven
* auditable
* RedIQ-parity or better

---

# `canonical_deal_schema_v0_1.md`

```markdown
# Canonical Deal Schema v0.1
## Multifamily Underwriting (Value-Add, Lease-Up, Repositioning)

---

## 0. Purpose & Design Principles

This schema defines the **single source of truth** for underwriting a multifamily deal.

Design goals:
- Table-driven (no hidden formulas)
- Annual-first, monthly where material
- Cohort-based revenue modeling
- Explicit separation of physical vacancy and collection loss
- First-class modeling of revenue programs (“other income”)
- Deterministic, auditable, reproducible

Out of scope (by policy):
- Fee-like income (late fees, NSF, fines)
- Behavioral penalties
- Black-box blended vacancy assumptions

---

## 1. Metadata (Traceability Only)

### DealMetadata (scalar)

| Field | Required | Notes |
|-----|---------|------|
| deal_id | Yes | Stable identifier |
| run_id | Yes | Unique per model run |
| as_of_date | Yes | Model valuation date |
| analyst | Yes | Human owner |
| purpose | Yes | Screening / IC / Update / Sensitivity |
| notes | Optional | Narrative context |

---

## 2. Time Grid (Authoritative)

### TimeGrid (scalar + derived)

| Field | Required | Notes |
|-----|---------|------|
| analysis_start_date | Yes | Month-level |
| analysis_end_date | Yes | Month-level |
| annual_periods | Derived | Year buckets |
| monthly_periods | Derived | Explicit months |

All time-based tables reference this grid.

---

## 3. Unit Cohorts (Base Rent Foundation)

### UnitCohorts (table)

Each row represents a **rent-bearing cohort** (up to ~20).

| Field | Required | Notes |
|-----|---------|------|
| cohort_id | Yes | Primary key |
| unit_type | Yes | 1B, 2B, etc. |
| unit_count | Yes | Initial count |
| sqft | Optional | Informational |
| initial_inplace_rent | Yes | Contract rent at start |
| bedrooms | Optional | Number of bedrooms (0 for studio) |
| bathrooms | Optional | Number of bathrooms |

Unit count may be time-varying in future versions; v0.1 assumes static.

---

## 4. Market Rent & In-Place Rent Logic

### MarketRentCurve (table)

| Field | Required | Notes |
|-----|---------|------|
| cohort_id | Yes | FK → UnitCohorts |
| start_period | Yes | Month |
| end_period | Yes | Month |
| market_rent | Yes | $ / unit / month |

### GrowthAssumptions (scalar)

| Field | Required | Notes |
|-----|---------|------|
| growth_type | Yes | `annual_compound`, `monthly_compound` or `step_only` |
| annual_growth_rate | Yes | Scalar annual market rent growth |
| annual_growth_rates | Optional | Per-year growth vector (ADR 0003 §4) |
| growth_start_month | Optional | Month growth years count from (default: analysis start) |

The engine grows a cohort whose `market_rent_curve` is flat. When
`annual_growth_rates` is present it wins over `annual_growth_rate`:

- Year 1 has no growth. Element `k` is the growth applied at the start of
  growth year `k + 2`, so the first increase (entering year 2) uses element 0.
- Elements past the hold are ignored. If the vector is shorter than the hold,
  its last value repeats.
- Growth years count from `growth_start_month`, so it shifts every increase.
- `monthly_compound` compounds each year's rate monthly within that year.
- A cohort `growth_rate` and explicit time-varying `market_rent_curve` rows
  stay authoritative.

The assumption resolver writes the vector and its element 0 to
`annual_growth_rate`, which stays the scalar shorthand.

---

### LossToLease (table)

Explicit bridge from market rent to in-place rent.

| Field | Required | Notes |
|-----|---------|------|
| cohort_id | Yes | FK |
| start_period | Yes | Month |
| end_period | Yes | Month |
| ltl_percent | Yes | % discount to market |

**Derived:**
```

In-Place Rent = Market Rent × (1 − LTL)

```

---

## 5. Physical Vacancy (Units)

### PhysicalVacancyCurve (table)

| Field | Required | Notes |
|-----|---------|------|
| cohort_id | Yes | FK |
| start_period | Yes | Month |
| end_period | Yes | Month |
| vacancy_rate | Yes | % of units vacant |

**Derived:**
```

Occupied Units = Units × (1 − Vacancy)

```

---

## 6. Collection Loss (Dollar-Level)

> Collection loss is the **only** dollar adjustment for non-payment.
> This captures “economic vacancy” without using that term.

### CollectionLossCurve (table)

| Field | Required | Notes |
|-----|---------|------|
| applies_to | Yes | Rent / Programs / ALL |
| start_period | Yes | Month |
| end_period | Yes | Month |
| loss_rate | Yes | % of billed dollars |

Applies after physical vacancy.

---

## 7. Revenue Programs (Other Income)

Revenue programs are **first-class revenue engines**, not residual buckets.

### 7.1 RevenuePrograms (table)

| Field | Required | Notes |
|-----|---------|------|
| program_id | Yes | Primary key |
| program_name | Yes | Human-readable |
| program_type | Yes | tenant-based / asset-based / recovery |
| pricing_type | Yes | $/unit, % rent, $/asset |
| price_value | Yes | Numeric |
| eligible_units | Yes | ALL / cohort_id / custom |
| start_period | Yes | Month |
| end_period | Optional | Month |

---

### 7.2 ProgramAdoptionCurve (table)

Monthly control of lease-up / opt-in.

| Field | Required | Notes |
|-----|---------|------|
| program_id | Yes | FK |
| start_period | Yes | Month |
| end_period | Yes | Month |
| adoption_rate | Yes | % of eligible base |

Adoption **may exceed unit turnover speed**.

---

### 7.3 ProgramCapacity (optional table)

For asset-limited programs (carports, storage).

| Field | Required | Notes |
|-----|---------|------|
| program_id | Yes | FK |
| total_capacity | Yes | Physical cap |

**Derived:**
```

Billable Units = min(adoption × eligible_units, capacity)

```

---

### 7.4 ProgramCosts (optional table)

Allows net revenue modeling.

| Field | Required | Notes |
|-----|---------|------|
| program_id | Yes | FK |
| cost_type | Yes | fixed / % revenue / $/unit |
| cost_value | Yes | Numeric |

---

## 8. Utility Recoveries (Linked Revenue)

Utilities are modeled as **recoveries**, not discretionary income.

### UtilityRecoveryRules (table)

| Field | Required | Notes |
|-----|---------|------|
| utility_category | Yes | water, sewer, electric, etc. |
| recovery_basis | Yes | occupied units / RUBS |
| recovery_rate | Yes | % |
| lag_months | Optional | Timing delay |

Recoveries link directly to opex and occupancy.

---

## 9. Operating Expenses (Opex)

### 9.1 OpexBaselineTemplate (reference)

A suggested starting list:
- Property Taxes
- Insurance
- R&M
- Utilities (by type)
- Payroll
- Contract Services
- Turnover
- G&A
- Grounds / Cleaning
- Other

---

### 9.2 OpexTable (user-editable)

| Field | Required | Notes |
|-----|---------|------|
| category_name | Yes | Free text |
| calculation_type | Yes | fixed, $/unit, % EGR, turnover-linked |
| base_value | Yes | Numeric |
| growth_rate | Optional | Annual |
| timing | Yes | annual / monthly |
| recoverable_flag | Yes | true / false |

---

## 10. Turnover & Renewal Assumptions

### TurnoverAssumptions (table)

| Field | Required | Notes |
|-----|---------|------|
| cohort_id | Yes | FK or ALL |
| renewal_rate | Yes | % |
| turnover_cost | Yes | $ per event |
| downtime_months | Yes | Vacancy duration |

---

## 11. Capex & Lease-Up (Monthly)

### CapexSchedule (table)

| Field | Required | Notes |
|-----|---------|------|
| category | Yes | Interior, Exterior, etc. |
| month | Yes | Month |
| amount | Yes | $ |
| units_affected | Optional | Count |

---

## 12. Debt Model (Monthly Where Needed)

### DebtTerms (scalar)

| Field | Required | Notes |
|-----|---------|------|
| commitment | Yes | $ |
| rate | Yes | % |
| amort_years | Yes | Years |
| io_months | Optional | Months |

---

### DebtDrawSchedule (table)

| Field | Required | Notes |
|-----|---------|------|
| month | Yes | Month |
| draw_amount | Yes | $ |

---

## 13. Exit Assumptions

### ExitAssumptions (scalar)

| Field | Required | Notes |
|-----|---------|------|
| exit_cap_rate | Yes | % |
| sale_cost_percent | Yes | % |
| exit_month | Yes | Month |

---

## 13.5 Pricing Provenance (Optional)

Pricing has provenance: a published OM price, a broker whisper, and the strike the analyst underwrites are three distinct things. This section makes that distinction first-class so the memo writer can cite from a stable structured location instead of grepping prose in `deal_manifest.md`.

### PricingProvenance (scalar, optional)

| Field | Type | Required | Notes |
|-----|------|----------|------|
| published_om_price | number \| null | Optional | Listed asking price from OM; null if unpriced |
| broker_whisper_price | number \| null | Optional | Verbal whisper / guidance; null if none |
| strike_price | number | Required when section present | The underwritten price; **MUST equal `purchase_assumptions.purchase_price`** |
| strike_price_basis | enum | Required when section present | `om_published` \| `broker_whisper` \| `broker_whisper_minus_5pct` \| `broker_whisper_minus_10pct` \| `cap_rate_derived` \| `analyst_target` \| `other` |
| strike_price_derivation | string | Required when section present | Free-text recipe (e.g., "76M whisper × 0.95 ≈ 72.2M") |
| om_pricing_process | enum | Optional | `published_asking_price` \| `best_offers_loi_unpriced` \| `call_for_offers` \| `unknown` |
| as_of_date | string (YYYY-MM-DD) | Required when section present | When provenance was recorded |

**Cross-section consistency rule (validator-enforced):**

If `pricing_provenance` is present, then
`pricing_provenance.strike_price == purchase_assumptions.purchase_price`.
Failure → `SCHEMA_VIOLATION` naming both values.

The whole section is optional; legacy canonical_inputs.json files without it remain valid.

---

## 13.6 Deal Profile and Assumptions (Optional, ADR 0003)

`deal_profile` (`deal_type`, `msa`, `submarket`, `year_built`, `asset_class`,
`star_tier`) and `assumptions` (`resolver_version`, `mode`, `entries`) are
written by `engine.assumptions.resolve_assumptions`. The engine ignores both;
the resolver writes resolved values into the canonical fields above. Each entry
records `value`, `unit`, `source`, `basis`, `as_of`, `confidence`,
`candidates`, `default_value`, `override`, `review_required`, and optional
`cohort_overrides` and `guardrail`. `guardrail` (`floor`, `floor_source`,
`status: "below_floor"`, `action`, `original`, optional `applied`,
`segments`, `basis`) is present when a value is below its house floor:
`action: "warned"` in underwrite mode (validator `GUARDRAIL_BELOW_FLOOR`
WARNING) and `"enforced"` in pricing mode. See
`docs/adr/0003-assumption-layer.md`.

`deal_type` supports `stabilized` and `value_add`; `lease_up` and `distressed`
are reserved and rejected (validator `DEAL_TYPE_UNSUPPORTED`). Entries sourced
from `house:*` or `engine_fallback` warn with `ASSUMPTION_REVIEW_REQUIRED`.
`deal_profile.deferred_maintenance` (boolean) puts recurring capex at the high
end of the vintage band. A waived floor (an analyst or PCA figure kept below
it) records `action: "floor_waived_by_analyst_override"` or
`"floor_waived_by_deal_evidence"` with `waived_by`.

### `capital_plan` (ADR 0003 §3): the three capex buckets

```json
"capital_plan": {
  "immediate_capex": {"amount": 180000, "funding": "equity_at_close"},
  "recurring_capex": {"per_unit_annual": 1500, "growth_rate": 0.03},
  "pca": {"immediate_capex": 180000, "recurring_capex_per_unit": 1500,
          "as_of": "2026-08-15", "reference": "Synthetic PCA"}
}
```

- `immediate_capex` (bucket 1): one-time capital at close. `funding` is
  `equity_at_close` (default: added to `total_equity_basis` and
  `total_unlevered_basis`, stated or defaulted) or `lender_holdback` (added to
  `total_unlevered_basis` only; the debt terms carry the holdback).
  `amount_in_stated_basis` is how much the stated bases already include
  (backsolve's priced case sets it). Never in NOI or the monthly cash flows.
- `recurring_capex` (bucket 2): year-1 $/unit a year below NOI, grown by
  `growth_rate` from analysis year 2 (optional `start_period` / `end_period`).
- `replacement_reserves` (bucket 3) is the floor: the engine charges
  `max(recurring capex, reserve)` each month in the cash flow's
  `replacement_reserves` line, never both, and reports
  `recurring_capex_by_vintage`, `replacement_reserve_floor` and
  `results.capital_plan` (the charge by analysis year and which bucket binds).
- `pca` is deal evidence the resolver reads (`deal:pca`); the engine ignores it.

---

## 14. Derived Outputs (Not Inputs)

- Net Rental Income
- Net Revenue Program Income
- Utility Recoveries
- Effective Gross Income (EGI)
- NOI
- Cash Flow (monthly & annual)
- IRR / EM / DSCR
- Sensitivities

---

## 15. Validation Rules (Non-Exhaustive)

- All periods must exist in TimeGrid
- Vacancy, LTL, collection loss ∈ [0,1]
- Adoption rates ∈ [0,1]
- No negative revenues unless explicitly allowed
- Monthly tables must reconcile to annual totals
- Capacity constraints enforced

---

## 16. Versioning

- Schema version: v0.1
- Backward-incompatible changes require new version
- Engine must declare supported schema versions

---

## End of Schema v0.1
```
