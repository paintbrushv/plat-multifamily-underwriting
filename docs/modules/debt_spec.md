# Debt Service Module Specification

**Module:** `engine/modules/debt.py`
**Version:** v0.2
**Status:** IMPLEMENTING

---

## Overview

The Debt Service module calculates monthly debt service including:
- Loan draw schedule (progressive funding)
- Interest-only (I/O) period
- Amortizing principal + interest (P+I)
- Outstanding balance tracking
- DSCR calculation support

---

## Input Schema

### debt_terms (Object)

```json
{
  "debt_terms": {
    "commitment": 10000000,
    "rate": 0.065,
    "amort_years": 30,
    "io_months": 24,
    "loan_start_month": "2026-01"
  }
}
```

### debt_draw_schedule (Array)

```json
{
  "debt_draw_schedule": [
    {"month": "2026-01", "draw_amount": 8000000},
    {"month": "2026-06", "draw_amount": 1000000},
    {"month": "2026-12", "draw_amount": 1000000}
  ]
}
```

### Field Definitions

#### debt_terms

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `commitment` | number | Yes | Total loan commitment ($) |
| `rate` | number | Yes | Annual interest rate (0.065 = 6.5%) |
| `amort_years` | integer | Yes | Amortization period in years |
| `io_months` | integer | No | Interest-only period in months (default: 0) |
| `loan_start_month` | string | No | First month of loan (default: analysis start) |

#### debt_draw_schedule

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `month` | string | Yes | Month of draw (YYYY-MM) |
| `draw_amount` | number | Yes | Amount drawn ($) |

---

## Calculation Logic

### Interest Rate Conversion
```python
# Monthly rate from annual
monthly_rate = annual_rate / 12
```

### Monthly Payment (Amortizing)
```python
# Standard mortgage formula: P * r(1+r)^n / ((1+r)^n - 1)
# Where:
#   P = principal balance at start of amortization
#   r = monthly interest rate
#   n = remaining amortization periods (months)

def calculate_monthly_payment(principal, monthly_rate, amort_months):
    if monthly_rate == 0:
        return principal / amort_months
    r = monthly_rate
    n = amort_months
    return principal * (r * (1 + r)**n) / ((1 + r)**n - 1)
```

### Monthly Calculation Flow

```
For each month:
    1. Apply any scheduled draws
       outstanding_balance += draw_amount

    2. Calculate interest expense
       interest = outstanding_balance * monthly_rate

    3. Calculate principal payment
       if in_io_period:
           principal_payment = 0
           debt_service = interest
       else:
           total_payment = amortizing_payment (calculated at IO end)
           principal_payment = total_payment - interest
           debt_service = total_payment

    4. Update balance
       outstanding_balance -= principal_payment

    5. Store results
```

### I/O to Amortization Transition

When I/O period ends:
1. Use current outstanding balance as amortizing principal
2. Calculate monthly payment for remaining amort term
3. Begin P+I payments

---

## Output Structure

```json
{
  "debt": {
    "by_month": [
      {
        "month": "2026-01",
        "beginning_balance": 0.00,
        "draw_amount": 8000000.00,
        "interest_expense": 43333.33,
        "principal_payment": 0.00,
        "debt_service": 43333.33,
        "ending_balance": 8000000.00,
        "is_io_period": true
      },
      {
        "month": "2028-01",
        "beginning_balance": 10000000.00,
        "draw_amount": 0.00,
        "interest_expense": 54166.67,
        "principal_payment": 9092.45,
        "debt_service": 63259.12,
        "ending_balance": 9990907.55,
        "is_io_period": false
      }
    ],
    "by_year": [
      {
        "year": "2026",
        "total_interest": 520000.00,
        "total_principal": 0.00,
        "total_debt_service": 520000.00,
        "ending_balance": 10000000.00
      }
    ],
    "summary": {
      "total_commitment": 10000000.00,
      "total_drawn": 10000000.00,
      "total_interest_paid": 2850000.00,
      "total_principal_paid": 250000.00,
      "final_balance": 9750000.00,
      "weighted_avg_rate": 0.065,
      "amortizing_payment": 63259.12
    }
  }
}
```

---

## Function Signature

```python
def compute_debt(
    time_grid: TimeGrid,
    debt_terms: Dict[str, Any],
    debt_draw_schedule: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Compute monthly debt service with I/O and amortization.

    Args:
        time_grid: Authoritative time grid
        debt_terms: Loan terms (commitment, rate, amort_years, io_months)
        debt_draw_schedule: Optional draw schedule (defaults to full draw at start)

    Returns:
        Dict with by_month, by_year, summary
    """
```

---

## Validation Rules

1. **commitment** must be > 0
2. **rate** must be in range [0, 1] (0% to 100%)
3. **amort_years** must be >= 1
4. **io_months** must be >= 0 and <= total analysis months
5. **draw_amount** sum must be <= commitment
6. **draw months** must be within analysis period

---

## Edge Cases

1. **No debt_terms**: Return empty debt structure (all zeros)
2. **No draw_schedule**: Default to full draw at loan_start_month
3. **Zero interest rate**: Pure principal amortization
4. **IO period longer than analysis**: All months are I/O
5. **Draw outside analysis period**: Ignore with warning

---

## Test Cases

1. Interest-only period calculates correctly
2. Amortizing payment formula is accurate
3. Draw schedule accumulates balance properly
4. Transition from I/O to P+I works correctly
5. Principal reduces balance each month
6. Empty inputs return zeros
7. Annual totals aggregate monthly values
8. Final balance matches expected

---

## Integration Points

- **Inputs from:** None (standalone)
- **Outputs to:** `cashflow.py` (Leveraged CF = Unleveraged CF - Debt Service)
- **Referenced by:** `metrics.py` (DSCR = NOI / Debt Service)

---

## Future Enhancements (v0.3+)

- Multiple debt tranches
- Variable rate loans (SOFR + spread)
- Interest rate caps/floors
- Prepayment penalties
- Debt payoff at exit

---

## Change Log

| Date | Change | Author |
|------|--------|--------|
| 2026-01-16 | Initial specification | Claude |
