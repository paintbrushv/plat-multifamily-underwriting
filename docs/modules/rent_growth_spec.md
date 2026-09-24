# Rent Growth Module Specification

## Overview

The Rent Growth module automates market rent curve generation from simple growth assumptions. Instead of manually specifying rent for each period, users provide a starting rent and growth parameters, and the module generates the full time series.

## Core Concepts

### Market Rent Growth

Market rent growth models how asking rents evolve over time:

- **Annual compounding**: X% increase applied at year boundary
- **Monthly compounding**: X% annual rate spread evenly across months
- **Step function**: Discrete jumps at specific dates
- **Hybrid**: Combine compound growth with periodic step increases

### Loss-to-Lease Dynamics

Loss-to-lease (LTL) represents the gap between in-place and market rents:

- **Initial LTL**: Starting discount from market (e.g., 5%)
- **LTL decay**: How quickly in-place rents catch up to market
- **Decay triggers**: Lease expiration, renewal, or time-based

### Renovation Premiums

When units are renovated, their rent increases:

- **Premium amount**: Additional rent from renovations
- **Premium timing**: Applied after renovation completion
- **Premium persistence**: Assumed permanent (part of new base)

## Input Schema

### Rent Growth Assumptions

```json
{
  "rent_growth_assumptions": {
    "growth_type": "annual_compound",
    "annual_growth_rate": 0.03,
    "growth_start_month": "2027-01",
    "step_increases": [
      {
        "effective_month": "2026-06",
        "increase_percent": 0.02,
        "reason": "Lease-up completion"
      }
    ]
  }
}
```

### Field Definitions

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| growth_type | enum | Yes | "annual_compound", "monthly_compound", "step_only" |
| annual_growth_rate | number | Yes | Annual growth rate (0.03 = 3%) |
| growth_start_month | string | No | When growth begins (defaults to analysis start) |
| step_increases | array | No | One-time step increases |

### LTL Decay Assumptions

```json
{
  "ltl_decay_assumptions": {
    "decay_type": "annual_step",
    "initial_ltl_percent": 0.05,
    "annual_decay_rate": 0.02,
    "minimum_ltl_percent": 0.00,
    "decay_start_month": "2026-01"
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| decay_type | enum | Yes | "annual_step", "monthly_linear", "none" |
| initial_ltl_percent | number | Yes | Starting LTL (0.05 = 5%) |
| annual_decay_rate | number | Yes | Annual LTL reduction |
| minimum_ltl_percent | number | No | Floor for LTL (default 0) |
| decay_start_month | string | No | When decay begins |

## Output Schema

### Generated Market Rent Curve

```json
{
  "generated_market_rent_curve": [
    {
      "cohort_id": "1B",
      "start_period": "2026-01",
      "end_period": "2026-12",
      "market_rent": 1500.0,
      "growth_applied": 0.0,
      "step_applied": 0.0
    },
    {
      "cohort_id": "1B",
      "start_period": "2027-01",
      "end_period": "2027-12",
      "market_rent": 1545.0,
      "growth_applied": 0.03,
      "step_applied": 0.0
    }
  ]
}
```

### Generated LTL Curve

```json
{
  "generated_ltl_curve": [
    {
      "cohort_id": "1B",
      "start_period": "2026-01",
      "end_period": "2026-12",
      "ltl_percent": 0.05,
      "decay_applied": 0.0
    },
    {
      "cohort_id": "1B",
      "start_period": "2027-01",
      "end_period": "2027-12",
      "ltl_percent": 0.03,
      "decay_applied": 0.02
    }
  ]
}
```

### Summary

```json
{
  "summary": {
    "starting_market_rent_psf": 1500.0,
    "ending_market_rent_psf": 1639.09,
    "total_growth_percent": 0.0927,
    "cagr": 0.03,
    "starting_ltl": 0.05,
    "ending_ltl": 0.01,
    "ltl_reduction": 0.04
  }
}
```

## Calculation Logic

### Annual Compound Growth

```python
def calculate_annual_compound(base_rent, annual_rate, years_elapsed):
    """
    Apply compound growth at year boundaries.

    Year 0 (analysis year): base_rent
    Year 1: base_rent * (1 + rate)
    Year N: base_rent * (1 + rate)^N
    """
    return base_rent * (1 + annual_rate) ** years_elapsed
```

### Monthly Compound Growth

```python
def calculate_monthly_compound(base_rent, annual_rate, months_elapsed):
    """
    Apply compound growth monthly.

    Monthly rate = (1 + annual_rate)^(1/12) - 1
    """
    monthly_rate = (1 + annual_rate) ** (1/12) - 1
    return base_rent * (1 + monthly_rate) ** months_elapsed
```

### Step Increases

```python
def apply_step_increases(base_rent, step_increases, current_month):
    """
    Apply all step increases effective on or before current month.
    Steps are multiplicative.
    """
    adjusted = base_rent
    for step in step_increases:
        if step["effective_month"] <= current_month:
            adjusted *= (1 + step["increase_percent"])
    return adjusted
```

### LTL Decay - Annual Step

```python
def calculate_ltl_annual_step(initial_ltl, decay_rate, years_elapsed, min_ltl):
    """
    Reduce LTL by fixed amount each year.
    """
    decayed = initial_ltl - (decay_rate * years_elapsed)
    return max(decayed, min_ltl)
```

### LTL Decay - Monthly Linear

```python
def calculate_ltl_monthly_linear(initial_ltl, annual_decay, months_elapsed, min_ltl):
    """
    Smooth linear reduction in LTL.
    """
    monthly_decay = annual_decay / 12
    decayed = initial_ltl - (monthly_decay * months_elapsed)
    return max(decayed, min_ltl)
```

## Integration with Renovations

When renovation data is available, rent premiums are applied:

```python
def apply_renovation_premium(base_market_rent, renovations_by_cohort, month):
    """
    Add renovation premium for completed units.

    Premium is applied to the cohort's rent after completion.
    """
    if cohort_id in renovations_by_cohort:
        reno_data = renovations_by_cohort[cohort_id]
        if month >= reno_data["completion_month"]:
            return base_market_rent + reno_data["rent_premium"]
    return base_market_rent
```

## Usage Example

### Simple Annual Growth

```python
# Input
unit_cohorts = [
    {"cohort_id": "1B", "unit_count": 50, "initial_inplace_rent": 1425}
]
growth_assumptions = {
    "growth_type": "annual_compound",
    "annual_growth_rate": 0.03
}
ltl_assumptions = {
    "decay_type": "annual_step",
    "initial_ltl_percent": 0.05,
    "annual_decay_rate": 0.02
}

# Generate curves
result = generate_rent_curves(time_grid, unit_cohorts, growth_assumptions, ltl_assumptions)

# Result feeds directly into revenue module
market_rent_curve = result["generated_market_rent_curve"]
ltl_curve = result["generated_ltl_curve"]
```

### With Step Increases

```python
growth_assumptions = {
    "growth_type": "annual_compound",
    "annual_growth_rate": 0.025,
    "step_increases": [
        {"effective_month": "2026-06", "increase_percent": 0.02},  # Lease-up bump
        {"effective_month": "2027-07", "increase_percent": 0.015}  # Market reset
    ]
}
```

## Edge Cases

### No Growth

If `annual_growth_rate` is 0 or `growth_type` is "step_only":
- Market rent remains flat except for step increases
- LTL decay still applies

### Growth Start Delayed

If `growth_start_month` is after analysis start:
- Base rent holds flat until growth start
- Years elapsed counted from growth start, not analysis start

### LTL Already at Minimum

If `initial_ltl_percent` <= `minimum_ltl_percent`:
- No decay applied
- LTL holds at initial value

### Multiple Cohorts

Each cohort can have different:
- Starting market rent (from `initial_inplace_rent` adjusted for LTL)
- Growth rates (if specified per-cohort)
- LTL percentages

## Backward Compatibility

This module generates curves compatible with the existing `compute_base_rent()` function:

- `generated_market_rent_curve` → `market_rent_curve` parameter
- `generated_ltl_curve` → `loss_to_lease` parameter

Existing manual curves continue to work; this module is additive.

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 0.1 | 2026-01 | Initial specification |
