# Operating Expenses Module Specification

**Module:** `engine/modules/opex.py`
**Version:** v0.2
**Status:** IMPLEMENTING

---

## Overview

The Operating Expenses (OpEx) module calculates property operating expenses on a monthly basis. It supports multiple calculation types, growth rate application, and tracks recoverable vs non-recoverable expenses.

---

## Input Schema

### opex_table (Array)

```json
{
  "opex_table": [
    {
      "category_name": "Property Taxes",
      "calculation_type": "fixed_annual",
      "base_value": 120000,
      "growth_rate": 0.02,
      "timing": "annual",
      "recoverable_flag": false
    },
    {
      "category_name": "Insurance",
      "calculation_type": "per_unit",
      "base_value": 450,
      "growth_rate": 0.03,
      "timing": "annual",
      "recoverable_flag": false
    },
    {
      "category_name": "Utilities",
      "calculation_type": "per_unit_monthly",
      "base_value": 85,
      "growth_rate": 0.025,
      "timing": "monthly",
      "recoverable_flag": true
    },
    {
      "category_name": "Management Fee",
      "calculation_type": "percent_egr",
      "base_value": 0.04,
      "growth_rate": 0,
      "timing": "monthly",
      "recoverable_flag": false
    },
    {
      "category_name": "R&M",
      "calculation_type": "per_unit",
      "base_value": 600,
      "growth_rate": 0.025,
      "timing": "annual",
      "recoverable_flag": false
    }
  ]
}
```

### Field Definitions

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `category_name` | string | Yes | Unique identifier for expense line |
| `calculation_type` | enum | Yes | How to calculate (see below) |
| `base_value` | number | Yes | Base amount for calculation |
| `growth_rate` | number | No | Annual growth rate (0.02 = 2%), default 0 |
| `timing` | enum | Yes | "annual" or "monthly" |
| `recoverable_flag` | boolean | Yes | Whether expense can be recovered from tenants |

### Calculation Types

| Type | Formula | base_value meaning |
|------|---------|-------------------|
| `fixed_annual` | base_value / 12 per month | Total annual $ |
| `fixed_monthly` | base_value per month | Monthly $ |
| `per_unit` | base_value × total_units / 12 | Annual $/unit |
| `per_unit_monthly` | base_value × total_units | Monthly $/unit |
| `per_sqft` | base_value × total_sqft / 12 | Annual $/sqft |
| `percent_egr` | base_value × EGR | % of Effective Gross Revenue |
| `percent_rent` | base_value × Gross Rent | % of gross rent revenue |

---

## Calculation Logic

### Growth Rate Application

Growth rates are applied **annually** from the analysis start date:

```python
years_elapsed = (current_month - analysis_start).years  # Integer years
growth_factor = (1 + growth_rate) ** years_elapsed
adjusted_value = base_value * growth_factor
```

Growth is applied at the **start of each year**, not monthly compounding.

### Monthly Calculation Flow

```
For each month:
    For each expense category:
        1. Calculate years elapsed since analysis start
        2. Apply growth factor to base_value
        3. Apply calculation_type formula
        4. Store monthly expense amount

    Aggregate:
        - total_opex (sum all categories)
        - recoverable_opex (where recoverable_flag = true)
        - non_recoverable_opex (where recoverable_flag = false)
```

---

## Output Structure

```json
{
  "opex": {
    "by_category_by_month": [
      {
        "month": "2026-01",
        "category": "Property Taxes",
        "expense": 10000.00,
        "growth_factor": 1.0,
        "recoverable": false
      },
      {
        "month": "2026-01",
        "category": "Utilities",
        "expense": 17000.00,
        "growth_factor": 1.0,
        "recoverable": true
      }
    ],
    "totals_by_month": [
      {
        "month": "2026-01",
        "total_opex": 85000.00,
        "recoverable_opex": 17000.00,
        "non_recoverable_opex": 68000.00
      }
    ],
    "totals_by_year": [
      {
        "year": "2026",
        "total_opex": 1020000.00,
        "recoverable_opex": 204000.00,
        "non_recoverable_opex": 816000.00
      }
    ]
  }
}
```

---

## Function Signature

```python
def compute_opex(
    time_grid: TimeGrid,
    unit_cohorts: List[Dict[str, Any]],
    opex_table: List[Dict[str, Any]],
    revenue_by_month: Optional[List[Dict[str, Any]]] = None,  # For percent_egr/percent_rent
) -> Dict[str, Any]:
    """
    Compute monthly operating expenses with growth and categorization.

    Args:
        time_grid: Authoritative time grid
        unit_cohorts: Unit mix for per_unit/per_sqft calculations
        opex_table: Expense category definitions
        revenue_by_month: Revenue totals for percent-based calculations (optional)

    Returns:
        Dict with by_category_by_month, totals_by_month, totals_by_year
    """
```

---

## Validation Rules

1. **Category names must be unique** within opex_table
2. **growth_rate** must be >= 0 and typically < 0.10 (warning if > 10%)
3. **percent_egr/percent_rent** requires revenue_by_month to be provided
4. **base_value** for percent types must be in range [0, 1]
5. All categories must have valid calculation_type

---

## Edge Cases

1. **Empty opex_table**: Return zero expenses for all months
2. **Zero growth_rate**: Apply base calculation without growth
3. **Missing revenue**: percent_egr/percent_rent uses 0 if no revenue data
4. **Partial year**: Growth applied based on integer years elapsed only

---

## Test Cases

1. Fixed annual expense spreads evenly across 12 months
2. Per-unit calculation uses total units from cohorts
3. Growth rate applies at year boundaries
4. Percent of EGR calculates correctly
5. Recoverable vs non-recoverable segregation
6. Multi-year growth accumulation

---

## Integration Points

- **Inputs from:** `unit_cohorts`, `revenue.totals_by_month`
- **Outputs to:** `cashflow.py` (NOI = EGR - OpEx)
- **Referenced by:** `metrics.py` (OpEx ratio calculations)

---

## Change Log

| Date | Change | Author |
|------|--------|--------|
| 2026-01-16 | Initial specification | Claude |
