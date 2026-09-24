# Cashflow Aggregation Module Specification

**Module:** `engine/modules/cashflow.py`
**Version:** v0.2
**Status:** IMPLEMENTING

---

## Overview

The Cashflow Aggregation module combines outputs from Revenue, OpEx, CapEx, and Debt modules to produce a complete cashflow waterfall. It calculates:
- Effective Gross Income (EGI)
- Net Operating Income (NOI)
- Unleveraged Cash Flow
- Leveraged Cash Flow (after debt service)

---

## Input Sources

| Source | Module | Key Fields |
|--------|--------|------------|
| Revenue | `revenue.py`, `revenue_programs.py` | net_rent, net_programs |
| Operating Expenses | `opex.py` | total_opex, recoverable_opex |
| Capital Expenditures | `capex.py` | total_capex |
| Debt Service | `debt.py` | debt_service |

---

## Calculation Flow

```
┌─────────────────────────────────────────────────────────────┐
│                    CASHFLOW WATERFALL                        │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  Net Rent Revenue                                            │
│  + Net Program Revenue                                       │
│  + Utility Recovery (recoverable_opex)                       │
│  ─────────────────────────────────────────                   │
│  = Effective Gross Income (EGI)                              │
│                                                              │
│  - Operating Expenses (total_opex)                           │
│  ─────────────────────────────────────────                   │
│  = Net Operating Income (NOI)                                │
│                                                              │
│  - Capital Expenditures (total_capex)                        │
│  ─────────────────────────────────────────                   │
│  = Unleveraged Cash Flow (before debt)                       │
│                                                              │
│  - Debt Service                                              │
│  ─────────────────────────────────────────                   │
│  = Leveraged Cash Flow (to equity)                           │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

---

## Output Structure

```json
{
  "cashflow": {
    "by_month": [
      {
        "month": "2026-01",
        "net_rent": 95000.00,
        "net_programs": 5000.00,
        "utility_recovery": 8500.00,
        "effective_gross_income": 108500.00,
        "total_opex": 42500.00,
        "net_operating_income": 66000.00,
        "total_capex": 2000.00,
        "unleveraged_cash_flow": 64000.00,
        "debt_service": 35000.00,
        "leveraged_cash_flow": 29000.00
      }
    ],
    "by_year": [
      {
        "year": "2026",
        "net_rent": 1140000.00,
        "net_programs": 60000.00,
        "utility_recovery": 102000.00,
        "effective_gross_income": 1302000.00,
        "total_opex": 510000.00,
        "net_operating_income": 792000.00,
        "total_capex": 24000.00,
        "unleveraged_cash_flow": 768000.00,
        "debt_service": 420000.00,
        "leveraged_cash_flow": 348000.00
      }
    ],
    "summary": {
      "total_egi": 6510000.00,
      "total_noi": 3960000.00,
      "total_capex": 120000.00,
      "total_debt_service": 2100000.00,
      "total_leveraged_cf": 1740000.00,
      "average_noi_margin": 0.608,
      "average_opex_ratio": 0.392
    }
  }
}
```

---

## Function Signature

```python
def compute_cashflow(
    time_grid: TimeGrid,
    revenue_by_month: List[Dict[str, Any]],
    opex_by_month: List[Dict[str, Any]],
    capex_by_month: List[Dict[str, Any]],
    debt_by_month: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Aggregate all components into complete cashflow waterfall.

    Args:
        time_grid: Authoritative time grid
        revenue_by_month: Revenue totals (net_rent, net_programs)
        opex_by_month: OpEx totals (total_opex, recoverable_opex)
        capex_by_month: CapEx totals (total_capex)
        debt_by_month: Debt service totals (debt_service)

    Returns:
        Dict with by_month, by_year, summary
    """
```

---

## Key Formulas

```python
# Per month calculations:
egi = net_rent + net_programs + recoverable_opex
noi = egi - total_opex
unleveraged_cf = noi - total_capex
leveraged_cf = unleveraged_cf - debt_service

# Summary metrics:
noi_margin = total_noi / total_egi
opex_ratio = total_opex / total_egi
```

---

## Edge Cases

1. **Missing inputs**: Use zeros for any missing monthly data
2. **Negative cash flow**: Valid and should be represented accurately
3. **No debt**: debt_service = 0, unleveraged_cf = leveraged_cf
4. **No revenue**: All downstream values are <= 0

---

## Test Cases

1. EGI calculation is correct (rent + programs + recovery)
2. NOI = EGI - OpEx
3. Unleveraged CF = NOI - CapEx
4. Leveraged CF = Unleveraged CF - Debt Service
5. Annual totals aggregate monthly correctly
6. Summary ratios calculated properly
7. Handles negative cash flow scenarios

---

## Integration Points

- **Inputs from:** `revenue.py`, `opex.py`, `capex.py`, `debt.py`
- **Outputs to:** `metrics.py` (IRR, EM, DSCR calculations)
- **Used by:** `engine.py` (main orchestrator)

---

## Change Log

| Date | Change | Author |
|------|--------|--------|
| 2026-01-16 | Initial specification | Claude |
