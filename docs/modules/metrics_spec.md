# Investment Metrics Module Specification

**Module:** `engine/modules/metrics.py`
**Version:** v0.2
**Status:** IMPLEMENTING

---

## Overview

The Investment Metrics module calculates key investment performance metrics:
- Internal Rate of Return (IRR) - levered and unlevered
- Equity Multiple (EM)
- Debt Service Coverage Ratio (DSCR)
- Cash-on-Cash yield by year
- Going-in and exit yields

---

## Input Schema

### exit_assumptions (Object)

```json
{
  "exit_assumptions": {
    "exit_cap_rate": 0.055,
    "sale_cost_percent": 0.02,
    "exit_month": "2030-12"
  }
}
```

### purchase_assumptions (Object)

```json
{
  "purchase_assumptions": {
    "purchase_price": 15000000,
    "closing_costs": 150000,
    "equity_contribution": 5000000
  }
}
```

---

## Metrics Definitions

### IRR (Internal Rate of Return)
Rate that makes NPV of all cash flows equal zero.

**Unlevered IRR:** Based on unleveraged cash flows + exit value
**Levered IRR:** Based on leveraged cash flows + equity at exit

```python
# IRR solves for r in:
# 0 = -Initial_Investment + Σ(CF_t / (1+r)^t) + Exit_Value / (1+r)^n
```

### Equity Multiple
Total distributions divided by initial equity.

```python
equity_multiple = (sum(leveraged_cf) + equity_at_exit) / initial_equity
```

### DSCR (Debt Service Coverage Ratio)
NOI divided by debt service, typically expressed as a minimum and average.

```python
dscr_month = noi_month / debt_service_month
average_dscr = total_noi / total_debt_service
```

### Cash-on-Cash Yield
Annual leveraged cash flow divided by initial equity.

```python
cash_on_cash_year = annual_leveraged_cf / initial_equity
```

### Exit Calculations

```python
# Projected sale price using forward NOI
forward_noi = trailing_12_month_noi  # or stabilized NOI
gross_sale_price = forward_noi / exit_cap_rate

# Net proceeds
sale_costs = gross_sale_price * sale_cost_percent
loan_payoff = outstanding_debt_balance
net_sale_proceeds = gross_sale_price - sale_costs - loan_payoff
```

---

## Output Structure

```json
{
  "metrics": {
    "irr": {
      "unlevered_irr": 0.125,
      "levered_irr": 0.182
    },
    "equity_multiple": {
      "unlevered_em": 1.65,
      "levered_em": 2.10
    },
    "dscr": {
      "minimum_dscr": 1.25,
      "average_dscr": 1.45,
      "by_year": [
        {"year": "2026", "dscr": 1.25},
        {"year": "2027", "dscr": 1.35}
      ]
    },
    "cash_on_cash": {
      "by_year": [
        {"year": "2026", "yield": 0.072},
        {"year": "2027", "yield": 0.085}
      ],
      "average": 0.098
    },
    "exit": {
      "exit_month": "2030-12",
      "forward_noi": 850000,
      "gross_sale_price": 15454545.45,
      "sale_costs": 309090.91,
      "loan_payoff": 9500000.00,
      "net_sale_proceeds": 5645454.55,
      "equity_at_exit": 5645454.55
    },
    "yields": {
      "going_in_cap_rate": 0.056,
      "exit_cap_rate": 0.055,
      "going_in_yield_on_cost": 0.052
    }
  }
}
```

---

## Function Signature

```python
def compute_metrics(
    time_grid: TimeGrid,
    cashflow_by_month: List[Dict[str, Any]],
    cashflow_by_year: List[Dict[str, Any]],
    debt_by_month: List[Dict[str, Any]],
    purchase_assumptions: Dict[str, Any],
    exit_assumptions: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Compute investment performance metrics.

    Args:
        time_grid: Authoritative time grid
        cashflow_by_month: Monthly cashflow waterfall
        cashflow_by_year: Annual cashflow totals
        debt_by_month: Debt service details (for payoff balance)
        purchase_assumptions: Initial investment structure
        exit_assumptions: Exit parameters (cap rate, costs, timing)

    Returns:
        Dict with irr, equity_multiple, dscr, cash_on_cash, exit, yields
    """
```

---

## IRR Calculation Method

Uses Newton-Raphson iteration to solve for IRR:

```python
def calculate_irr(cash_flows: List[float], initial_guess: float = 0.1) -> float:
    """
    Calculate IRR using Newton-Raphson method.

    cash_flows: List where [0] is negative (investment), rest are returns
    Returns: IRR as decimal (0.15 = 15%)
    """
    rate = initial_guess
    for _ in range(100):  # Max iterations
        npv = sum(cf / (1 + rate) ** i for i, cf in enumerate(cash_flows))
        npv_derivative = sum(-i * cf / (1 + rate) ** (i + 1) for i, cf in enumerate(cash_flows))
        if abs(npv_derivative) < 1e-10:
            break
        rate = rate - npv / npv_derivative
        if abs(npv) < 1e-6:
            break
    return rate
```

---

## Edge Cases

1. **No exit assumptions**: Cannot calculate IRR/EM, return None
2. **Zero debt service**: DSCR undefined, return None/Infinity
3. **Negative cash flows throughout**: IRR may not exist or be negative
4. **Exit before analysis end**: Truncate cash flows at exit month
5. **Very high/low IRR**: Clamp to reasonable range (-100% to 500%)

---

## Test Cases

1. IRR calculation matches expected for simple cash flows
2. Equity multiple calculates correctly
3. DSCR tracks NOI / Debt Service ratio
4. Cash-on-cash yield is annual CF / equity
5. Exit value uses cap rate correctly
6. Handles edge cases (no debt, negative CF)

---

## Integration Points

- **Inputs from:** `cashflow.py`, `debt.py`, input assumptions
- **Outputs to:** Final engine output, sensitivity analysis
- **Used by:** IC package generation, deal comparison

---

## Change Log

| Date | Change | Author |
|------|--------|--------|
| 2026-01-16 | Initial specification | Claude |
