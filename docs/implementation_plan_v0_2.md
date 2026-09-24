# Stack Implementation Plan: Underwriting Engine v0.2

**Created:** 2026-01-16
**Status:** IN PROGRESS
**Priority Order:** A (Cashflow Layer) -> B (Renovations) -> C (Excel UX)

---

## Decision Log

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Module priority | Cashflow layer first | Foundation for all metrics |
| Sensitivity approach | 2-way matrix | Practical for IC packages |
| Monte Carlo | Future version | Note in roadmap |
| Deployment | Excel primary, FastAPI later | Prove out locally first |
| Documentation | Obsessive .md files | Audit trail + onboarding |

---

## Phase 1: Complete Cashflow Layer

### 1A: Operating Expenses Module
- **File:** `engine/modules/opex.py`
- **Test:** `tests/test_opex.py`
- **Doc:** `docs/modules/opex_spec.md`
- **Schema additions:** `opex_table`, `expense_growth_assumptions`

### 1B: Capital Expenditures Module
- **File:** `engine/modules/capex.py`
- **Test:** `tests/test_capex.py`
- **Doc:** `docs/modules/capex_spec.md`
- **Schema additions:** `capex_schedule`

### 1C: Debt Service Module
- **File:** `engine/modules/debt.py`
- **Test:** `tests/test_debt.py`
- **Doc:** `docs/modules/debt_spec.md`
- **Schema additions:** `debt_terms`, `debt_draw_schedule`

### 1D: Cashflow Aggregation Module
- **File:** `engine/modules/cashflow.py`
- **Test:** `tests/test_cashflow.py`
- **Doc:** `docs/modules/cashflow_spec.md`
- **Output:** Monthly and annual cashflow waterfall

### 1E: Investment Metrics Module
- **File:** `engine/modules/metrics.py`
- **Test:** `tests/test_metrics.py`
- **Doc:** `docs/modules/metrics_spec.md`
- **Metrics:** IRR, Equity Multiple, DSCR, Cash-on-Cash, Yields

---

## Phase 2: Enhanced Modeling

### 2A: Renovation Module
- **File:** `engine/modules/renovations.py`
- **Test:** `tests/test_renovations.py`
- **Doc:** `docs/modules/renovations_spec.md`
- **Features:** Renovation schedule, premium tracking, downtime, ROI

### 2B: Rent Growth Automation
- **File:** Update `engine/modules/revenue.py`
- **Doc:** `docs/modules/rent_growth_spec.md`
- **Features:** Annual compound, step functions, auto-expand curves

### 2C: Sensitivity Engine
- **File:** `engine/modules/sensitivity.py`
- **Test:** `tests/test_sensitivity.py`
- **Doc:** `docs/modules/sensitivity_spec.md`
- **Features:** 2-way matrix (exit cap × rent growth)
- **Future:** Monte Carlo simulation (noted for v0.3+)

---

## Phase 3: Excel UX Overhaul

### 3A: RedIQ-Style Input Template
### 3B: Model Health Dashboard
### 3C: Output Summary Sheets
### 3D: VBA Macros + Hotkeys

---

## Implementation Progress Tracker

| Module | Spec | Code | Tests | Integration | Doc |
|--------|------|------|-------|-------------|-----|
| OpEx | ✅ | ✅ | ✅ | ✅ | ✅ |
| CapEx | ✅ | ✅ | ✅ | ✅ | ✅ |
| Debt | ✅ | ✅ | ✅ | ✅ | ✅ |
| Cashflow | ✅ | ✅ | ✅ | ✅ | ✅ |
| Metrics | ✅ | ✅ | ✅ | ✅ | ✅ |
| Renovations | ✅ | ✅ | ✅ | ✅ | ✅ |
| Rent Growth | ✅ | ✅ | ✅ | ✅ | ✅ |
| Sensitivity | ✅ | ✅ | ✅ | ✅ | ✅ |
| Excel UX | ✅ | ✅ | ✅ | ✅ | ✅ |

Legend: ⬜ Not started | 🔨 In progress | ✅ Complete

---

## Schema Version Strategy

- **v0.1:** Current (revenue only)
- **v0.2:** Add opex, capex, debt, cashflow, metrics, renovations, sensitivity
- **v0.3:** Monte Carlo, turnover modeling, benchmarks (future)

All v0.2 additions will be **optional** to maintain backward compatibility with v0.1 inputs.

---

## File Change Log

| Date | File | Change | Author |
|------|------|--------|--------|
| 2026-01-16 | docs/implementation_plan_v0_2.md | Created | Claude |

---
