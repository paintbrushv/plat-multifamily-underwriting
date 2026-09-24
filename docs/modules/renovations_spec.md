# Renovations Module Specification

## Overview

The Renovations module models value-add capital improvements that generate rent premiums. It tracks renovation schedules, associated costs, downtime periods, and resulting rent increases for cohort-level analysis.

## Core Concepts

### Renovation Program

A renovation program defines the scope of unit improvements across the property:

- **Target units**: Number of units to renovate per period
- **Renovation cost**: Cost per unit for improvements
- **Rent premium**: Monthly rent increase after renovation
- **Downtime**: Vacancy period during renovation (typically 30-45 days)
- **Timing**: When renovations occur (on turnover vs. proactive)

### Renovation Strategies

1. **On-Turnover**: Renovate units only when they naturally become vacant
   - Lower execution risk
   - Dependent on natural turnover rate
   - No additional vacancy loss

2. **Proactive**: Actively vacate and renovate occupied units
   - Faster execution
   - Causes vacancy loss during renovation
   - Higher execution risk

### Value-Add Economics

The economic benefit of renovation:
```
Annual Premium Value = Rent Premium × 12 months
Renovation ROI = Annual Premium Value / Renovation Cost
Payback Period = Renovation Cost / (Rent Premium × 12)
```

## Input Schema

### Renovation Schedule Table

```json
{
  "renovation_programs": [
    {
      "program_id": "classic_to_premium",
      "program_name": "Classic to Premium Upgrade",
      "target_cohort": "1B_classic",
      "output_cohort": "1B_renovated",
      "renovation_cost_per_unit": 15000,
      "rent_premium_monthly": 200,
      "downtime_days": 30,
      "strategy": "on_turnover",
      "start_month": "2026-03",
      "end_month": "2027-12",
      "monthly_pace": 5
    }
  ]
}
```

### Field Definitions

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| program_id | string | Yes | Unique identifier for the program |
| program_name | string | Yes | Human-readable name |
| target_cohort | string | Yes | Cohort ID of units to renovate |
| output_cohort | string | Yes | Cohort ID after renovation |
| renovation_cost_per_unit | number | Yes | Cost per unit ($) |
| rent_premium_monthly | number | Yes | Additional monthly rent after renovation ($) |
| downtime_days | number | Yes | Days unit is vacant during renovation |
| strategy | enum | Yes | "on_turnover" or "proactive" |
| start_month | string | Yes | First month renovations can occur |
| end_month | string | No | Last month renovations can occur |
| monthly_pace | number | Yes | Target units per month |

## Output Schema

### by_month

Monthly renovation activity:

```json
{
  "month": "2026-03",
  "units_renovated": 5,
  "renovation_cost": 75000.0,
  "downtime_vacancy_days": 150,
  "downtime_vacancy_loss": 3333.33,
  "cumulative_units_renovated": 5,
  "monthly_premium_revenue": 1000.0,
  "cumulative_premium_revenue": 1000.0
}
```

### by_program

Program-level summary:

```json
{
  "program_id": "classic_to_premium",
  "program_name": "Classic to Premium Upgrade",
  "total_units_renovated": 50,
  "total_renovation_cost": 750000.0,
  "total_downtime_vacancy_loss": 33333.33,
  "monthly_premium_at_completion": 10000.0,
  "annual_premium_at_completion": 120000.0,
  "simple_roi": 0.16,
  "payback_months": 75.0
}
```

### summary

Overall renovation metrics:

```json
{
  "total_units_renovated": 50,
  "total_renovation_cost": 750000.0,
  "total_vacancy_loss": 33333.33,
  "net_renovation_cost": 783333.33,
  "total_annual_premium": 120000.0,
  "blended_roi": 0.153,
  "avg_payback_months": 78.3
}
```

## Calculation Logic

### Monthly Renovation Count

For each month within the program window:

```python
def calculate_monthly_renovations(program, month, cohort_units_available):
    if month < program.start_month or month > program.end_month:
        return 0

    if program.strategy == "on_turnover":
        # Limited by natural turnover
        expected_turnover = cohort_units_available * monthly_turnover_rate
        return min(program.monthly_pace, expected_turnover)
    else:
        # Proactive - limited only by pace
        return min(program.monthly_pace, cohort_units_available)
```

### Downtime Vacancy Loss

```python
def calculate_downtime_loss(units_renovated, downtime_days, daily_rent):
    """Calculate vacancy loss during renovation period."""
    return units_renovated * (downtime_days / 30) * daily_rent
```

### Premium Revenue

```python
def calculate_premium_revenue(cumulative_renovated, rent_premium):
    """Monthly premium from all completed renovations."""
    return cumulative_renovated * rent_premium
```

### ROI Calculation

```python
def calculate_renovation_roi(renovation_cost, annual_premium):
    """Simple unlevered ROI on renovation spend."""
    if renovation_cost == 0:
        return None
    return annual_premium / renovation_cost
```

### Payback Period

```python
def calculate_payback_months(renovation_cost, rent_premium):
    """Months to recover renovation cost from premium."""
    if rent_premium == 0:
        return None
    return renovation_cost / rent_premium
```

## Integration Points

### Revenue Module Integration

The Renovations module feeds into Revenue calculations:

1. **Pre-renovation cohort**: Original rent, reduced unit count over time
2. **Post-renovation cohort**: Premium rent, increasing unit count
3. **Downtime impact**: Vacancy loss during renovation period

### CapEx Module Integration

Renovation costs flow to CapEx:
- Category: "Unit Renovations" or program-specific
- CapEx type: "renovation"
- Timing: Month of renovation completion

### Metrics Module Integration

Renovation metrics contribute to:
- **IRR**: Higher cash flows from premium rents
- **Exit value**: Higher NOI → higher valuation
- **Value creation**: Premium attribution

## Edge Cases

### Insufficient Units

If monthly_pace exceeds available units:
```python
actual_renovations = min(monthly_pace, available_units)
```

### Turnover Constraint

For on_turnover strategy with low turnover:
```python
max_renovations = cohort_units * turnover_rate / 12
actual_renovations = min(monthly_pace, max_renovations)
```

### Mid-Month Timing

Renovations are assumed to complete at month-end:
- No premium revenue in renovation month
- Premium begins following month

### Program Overlap

Multiple programs can run simultaneously:
- Each tracked independently
- Costs and premiums aggregate

## Example Scenarios

### Scenario 1: Basic Value-Add

```
Units: 100
Renovation cost: $15,000/unit
Rent premium: $200/month
Pace: 5 units/month

Year 1 Analysis:
- Units renovated: 60 (5/month × 12)
- Total cost: $900,000
- Year-end premium run rate: $12,000/month = $144,000/year
- Simple ROI: 16.0%
- Payback: 75 months
```

### Scenario 2: Aggressive Value-Add

```
Units: 200
Renovation cost: $20,000/unit
Rent premium: $300/month
Pace: 15 units/month (proactive)
Downtime: 45 days

Year 1 Analysis:
- Units renovated: 180 (15/month × 12)
- Total cost: $3,600,000
- Vacancy loss: ~$540,000 (45 days per unit)
- Net cost: $4,140,000
- Year-end premium: $54,000/month = $648,000/year
- Simple ROI: 15.7%
```

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 0.1 | 2026-01 | Initial specification |
