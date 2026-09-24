# Capital Expenditures Module Specification

**Module:** `engine/modules/capex.py`
**Version:** v0.2
**Status:** IMPLEMENTING

---

## Overview

The Capital Expenditures (CapEx) module calculates property capital expenditures on a monthly basis. It supports:
- Scheduled CapEx by category and month
- Renovation-linked CapEx (tied to renovation schedule)
- Reserve fund contributions
- Categorization (lease-up, stabilization, recurring)

---

## Input Schema

### capex_schedule (Array)

```json
{
  "capex_schedule": [
    {
      "category": "Roof Replacement",
      "capex_type": "one_time",
      "month": "2026-06",
      "amount": 150000,
      "notes": "Full roof replacement"
    },
    {
      "category": "Unit Renovations",
      "capex_type": "renovation",
      "month": "2026-03",
      "amount": 75000,
      "units_affected": 5,
      "notes": "March renovation batch"
    },
    {
      "category": "Recurring Reserves",
      "capex_type": "reserve",
      "amount_per_unit": 250,
      "timing": "annual",
      "notes": "Annual capital reserve"
    },
    {
      "category": "Appliance Upgrades",
      "capex_type": "recurring",
      "amount_per_unit_monthly": 15,
      "notes": "Ongoing appliance fund"
    }
  ]
}
```

### Field Definitions

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `category` | string | Yes | CapEx category name |
| `capex_type` | enum | Yes | Type of capital expenditure |
| `month` | string | Conditional | Specific month (for one_time, renovation) |
| `amount` | number | Conditional | Fixed amount (for one_time, renovation) |
| `amount_per_unit` | number | Conditional | Annual $/unit (for reserve) |
| `amount_per_unit_monthly` | number | Conditional | Monthly $/unit (for recurring) |
| `timing` | enum | Conditional | "annual" or "monthly" (for reserve) |
| `units_affected` | integer | No | Units impacted (informational) |
| `notes` | string | No | Description/notes |

### CapEx Types

| Type | Description | Required Fields |
|------|-------------|-----------------|
| `one_time` | Single expenditure in specific month | month, amount |
| `renovation` | Renovation-linked expenditure | month, amount |
| `reserve` | Reserve fund contribution | amount_per_unit, timing |
| `recurring` | Ongoing monthly expenditure | amount_per_unit_monthly |

---

## Calculation Logic

### One-Time CapEx
```python
# Applied in specified month only
if current_month == scheduled_month:
    capex = amount
```

### Renovation CapEx
```python
# Applied in specified month, linked to renovation schedule
if current_month == scheduled_month:
    capex = amount
    # Optionally link to units_affected from renovation_schedule
```

### Reserve CapEx
```python
# Spread across periods based on timing
if timing == "annual":
    monthly_amount = amount_per_unit * total_units / 12
else:  # monthly
    monthly_amount = amount_per_unit * total_units
```

### Recurring CapEx
```python
# Applied every month
monthly_amount = amount_per_unit_monthly * total_units
```

---

## Output Structure

```json
{
  "capex": {
    "by_month": [
      {
        "month": "2026-01",
        "total_capex": 25000.00,
        "one_time_capex": 0.00,
        "renovation_capex": 0.00,
        "reserve_capex": 20833.33,
        "recurring_capex": 1500.00
      },
      {
        "month": "2026-06",
        "total_capex": 172333.33,
        "one_time_capex": 150000.00,
        "renovation_capex": 0.00,
        "reserve_capex": 20833.33,
        "recurring_capex": 1500.00
      }
    ],
    "by_category": [
      {
        "category": "Roof Replacement",
        "capex_type": "one_time",
        "total_amount": 150000.00
      },
      {
        "category": "Recurring Reserves",
        "capex_type": "reserve",
        "total_amount": 250000.00
      }
    ],
    "totals_by_year": [
      {
        "year": "2026",
        "total_capex": 550000.00,
        "one_time_capex": 150000.00,
        "renovation_capex": 75000.00,
        "reserve_capex": 250000.00,
        "recurring_capex": 18000.00
      }
    ],
    "summary": {
      "total_capex": 550000.00,
      "capex_per_unit": 5500.00
    }
  }
}
```

---

## Function Signature

```python
def compute_capex(
    time_grid: TimeGrid,
    unit_cohorts: List[Dict[str, Any]],
    capex_schedule: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Compute monthly capital expenditures by category and type.

    Args:
        time_grid: Authoritative time grid
        unit_cohorts: Unit mix for per_unit calculations
        capex_schedule: CapEx schedule definitions

    Returns:
        Dict with by_month, by_category, totals_by_year, summary
    """
```

---

## Validation Rules

1. **One-time and renovation** types require `month` and `amount`
2. **Reserve** type requires `amount_per_unit` and `timing`
3. **Recurring** type requires `amount_per_unit_monthly`
4. **month** must be within analysis period
5. **amount** values must be >= 0

---

## Edge Cases

1. **Empty capex_schedule**: Return zero CapEx for all months
2. **Month outside analysis period**: Ignore (with warning)
3. **Multiple items same category same month**: Sum amounts

---

## Test Cases

1. One-time CapEx applies in correct month only
2. Reserve spreads evenly across 12 months (annual timing)
3. Recurring applies every month
4. Multiple categories aggregate correctly
5. Per-unit calculations use total units
6. Empty schedule returns zeros
7. Annual totals sum monthly correctly

---

## Integration Points

- **Inputs from:** `unit_cohorts`
- **Outputs to:** `cashflow.py` (Unleveraged CF = NOI - CapEx)
- **Referenced by:** `metrics.py` (total investment calculations)
- **Future:** Link to `renovations.py` for renovation-driven CapEx

---

## Change Log

| Date | Change | Author |
|------|--------|--------|
| 2026-01-16 | Initial specification | Claude |
