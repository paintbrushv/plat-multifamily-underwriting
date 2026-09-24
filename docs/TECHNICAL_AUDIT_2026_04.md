# Technical Audit: Multifamily Underwriting Engine

**Date:** 2026-04-17
**Scope:** Full codebase — engine core, I/O layer, integrations, ingestion pipeline, test suite
**Method:** 4 parallel audit agents covering non-overlapping areas

---

## Executive Summary

The multifamily underwriting engine is a **well-architected, modular system** with ~14,000 lines of production code and 337 passing tests. Core computational accuracy is excellent (17/18 deals within 0.1% NOI). The architecture has zero circular dependencies, clean module boundaries, and lazy imports for optional dependencies.

**However**, the codebase has accumulated significant **copy-paste duplication** (formatting helpers across 5 files, brand colors across 5 files) and has **critical test gaps** (the two most complex files — `rediq_bridge.py` at 2,469 LOC and `fund_waterfall.py` at 442 LOC — have zero unit tests). There is no CI/CD to enforce test health.

**Bottom line:** Solid architecture undermined by duplication and missing test coverage. Refactoring shared utilities would reduce maintenance burden by ~30-40% before building new features.

---

## Codebase Inventory

### By Layer

| Layer | Files | LOC | Tests | Test Files |
|-------|-------|-----|-------|------------|
| Engine Core (modules/) | 11 | 3,600 | 148 | 11 |
| I/O & Bridge | 10 | 7,300 | 30 | 3 |
| Integrations & Ingest | 8 | 2,500 | 136 | 8 |
| CLI Scripts | 7 | 2,700 | 0 | 0 |
| Schema | 1 | 3,700 | - | - |
| **Total** | **37** | **~14,000** | **337** | **28** |

### Largest Files (complexity risk)

| File | LOC | Tests | Risk |
|------|-----|-------|------|
| `rediq_bridge.py` | 2,469 | 0 | **CRITICAL** |
| `rediq_validate_cashflows.py` | 1,252 | 0 | Medium (script) |
| `rediq_output.py` | 1,333 | 9 (ZIP only) | **HIGH** |
| `engine/modules/broker_etl.py` | 828 | 0 | **HIGH** |
| `pptx_generator.py` | 652 | 7 | Medium |
| `engine.py` | 626 | 1 | Medium |
| `portfolio_pdf.py` | 619 | 0 | **HIGH** |
| `am_integration.py` | 574 | 24 | Low |
| `pdf_onepager.py` | 535 | 0 | **HIGH** |
| `xlsx_writer.py` | 467 | 0 (indirect) | **HIGH** |
| `market_integration.py` | 461 | 15 | Low |
| `fund_waterfall.py` | 442 | 0 | **HIGH** |
| `debt.py` | 440 | 17 | Low |

---

## Duplication Analysis

### CRITICAL: Formatting Helpers (5x duplication)

Identical `_fmt_pct()`, `_fmt_currency()`, `_fmt_multiple()` functions in:
1. `engine/pdf_onepager.py`
2. `engine/portfolio_pdf.py`
3. `engine/pptx_generator.py`
4. `engine/portfolio_xlsx.py` (partial)
5. `runs/portfolio_report.py`

**Impact:** ~50+ lines duplicated. Bug fix requires 5 edits. Inconsistency risk.
**Fix:** Extract to `engine/formatters.py`

### CRITICAL: Brand Colors (5x duplication)

`GOLD`, `DARK_900`, `WHITE`, `LIGHT_GRAY` defined in:
1. `engine/pdf_onepager.py`
2. `engine/pptx_generator.py`
3. `engine/portfolio_pdf.py`
4. `engine/portfolio_xlsx.py`
5. `engine/am_overlay_xlsx.py`

**Impact:** ~100 lines of color definitions. Brand change requires 5 edits.
**Fix:** Extract to `engine/brand.py`

### MODERATE: Excel Helpers (2x duplication)

`_set_col_widths()`, `_write_header_row()`, `_write_data_row()` duplicated in:
1. `engine/portfolio_xlsx.py`
2. `engine/am_overlay_xlsx.py`

**Fix:** Extract to `engine/xlsx_helpers.py`

### MODERATE: Layout Detection (2x duplication)

`_detect_layout()` logic duplicated in:
1. `engine/rediq_bridge.py` (extraction)
2. `engine/rediq_output.py` (writing)

**Fix:** Single shared function in a common module

### LOW: Utility Functions

`_get_total_units()` defined identically in:
1. `engine/modules/capex.py`
2. `engine/modules/opex.py`

**Fix:** Move to `engine/modules/util.py`

---

## Dead Code

| Location | Code | Status |
|----------|------|--------|
| `debt.py:37-73` | `_build_rate_curve_lookup()` | Never called — logic inlined in `_get_effective_rate()` |

**Total dead code:** 36 lines (minimal)

---

## Test Coverage Analysis

### Well-Tested Modules

| Module | Test File | Tests | Notes |
|--------|-----------|-------|-------|
| ingestion_pipeline.py | test_ingestion_pipeline.py | 33 | Very high — schema compliance, all CLI flags |
| am_integration.py | test_am_integration.py | 24 | Strong — all variance calcs |
| scenarios.py | test_scenarios.py | 20 | Excellent — bull/base/bear, refi vs sell |
| debt.py | test_debt.py | 17 | Excellent — variable rate, assumable, staged draws |
| renovations.py | test_renovations.py | 16 | Excellent — turnover, multiple programs |
| portfolio.py | test_portfolio.py | 16 | Good — aggregation, grouping |
| om_parser.py | test_om_parser.py | 16 | Good — PDF extraction, confidence levels |
| market_integration.py | test_market_integration.py | 15 | Good — comp analysis, growth adjustment |
| opex.py | test_opex.py | 14 | Good — phased growth, calc types |
| cloud_storage.py | test_cloud_storage.py | 13 | Good — mocked Azure |
| om_extractor.py | test_om_extractor.py | 13 | Good — mocked Claude API |
| cashflow.py | test_cashflow.py | 11 | Good — utility recovery, financing |
| capex.py | test_capex.py | 11 | Good — reserve/recurring/one-time |
| rent_roll_parser.py | test_rent_roll_parser.py | 11 | Good — CSV/Excel, occupancy |
| t12_parser.py | test_t12_parser.py | 11 | Good — category mapping |
| metrics.py | test_metrics.py | 10 | Good — IRR/EM/DSCR |
| api.py | test_api.py | 10 | Medium — happy paths |

### Critically Under-Tested

| Module | LOC | Tests | Gap |
|--------|-----|-------|-----|
| **rediq_bridge.py** | 2,469 | 0 | Largest file, core data extraction, zero tests |
| **fund_waterfall.py** | 442 | 0* | Complex promote/waterfall math, no dedicated tests |
| **broker_etl.py** | 828 | 0 | Second-largest module, zero tests |
| **validator.py** | 403 | 0* | Tested indirectly but no dedicated file |
| **pdf_onepager.py** | 535 | 0 | No tests |
| **portfolio_pdf.py** | 619 | 0 | No tests |
| **portfolio_xlsx.py** | 384 | 0 | No tests |
| **xlsx_writer.py** | 467 | 0* | Only indirect ZIP validation |
| **am_overlay_xlsx.py** | 374 | 0 | No tests |
| **rediq_output.py** | 1,333 | 9 | Only ZIP structure validation, no cell values |
| revenue.py | 121 | 1 | Single test, missing edge cases |
| time_grid.py | — | 1 | Single test |
| revenue_programs.py | — | 1 | Single test |
| end_to_end | — | 1 | Single integration test |

\* Has some indirect coverage through other tests

### Test Infrastructure Issues

1. **No CI/CD** — no GitHub Actions, no Azure Pipelines, nothing
2. **No `conftest.py`** — zero shared fixtures; every test builds inputs from scratch
3. **No `@pytest.fixture`** — all setup is inline
4. **No `@pytest.mark.parametrize`** — manual repetition instead
5. **Mixed styles** — older tests use `unittest.TestCase`, newer use plain pytest
6. **Conditional tests** — 2 tests in `test_excel_output.py` skip unless output files pre-exist
7. **No integration test pipeline** — no test runs bridge → engine → output → validation end-to-end

---

## Architectural Debt

### Engine Core

1. **Engine mutates cashflow results in-place** — exit proceeds injected post-computation (engine.py:513-558), tightly coupled to internal data structure
2. **Debt-funded CapEx correction** — engine zeroes CapEx, metrics.py restores it. Implicit contract.
3. **Division by zero risk** — capex.py:217 (`capex_per_unit = total / total_units`) doesn't guard zero units
4. **Silent forward NOI fallback** — metrics.py returns 0 if no cashflow data, causing $0 exit sale
5. **Scenario rent scaling** — doesn't account for partial-year starts; Year 1 may over/under-scale

### I/O Layer

1. **118+ hardcoded row/column references in rediq_bridge.py** — any RedIQ template change requires code modification
2. **Two entry points in rediq_output.py** — `generate_rediq_workbook()` and `write_outputs_to_rediq()` do nearly identical work
3. **Sign convention translation scattered** — each write function independently negates values; not centralized
4. **Float precision in xlsx_writer.py** — `.10g` format loses precision for values >1e10
5. **rediq_validate_cashflows.py is a 1,252-line script** — should be a library module

### Integration Layer

1. **Metro inference duplicated** — `portfolio._infer_metro()` vs similar logic in other modules
2. **Equity basis hardcoded fallback** — portfolio.py:127 uses `pp * 0.3` if equity not extractable
3. **API has no request validation** — handlers assume well-formed JSON
4. **Hard-coded category keywords** in t12_parser and am_integration
5. **OM extraction has no sanity bounds** — Claude Haiku may hallucinate prices/years

---

## Dependency Analysis

### External Dependencies (pip)

| Package | Used By | Required? |
|---------|---------|-----------|
| openpyxl | bridge, output (read_only), portfolio_xlsx, am_overlay_xlsx | Yes |
| reportlab | pdf_onepager, portfolio_pdf | Yes (for PDF) |
| matplotlib | pdf_onepager, portfolio_pdf, pptx_generator | Yes (for charts) |
| python-pptx | pptx_generator | Yes (for PPTX) |
| jsonschema | validator | Yes |
| pdfplumber | om_parser | Optional (lazy) |
| anthropic | om_extractor | Optional (lazy) |
| pyyaml | market_integration | Optional (lazy) |
| azure-storage-blob | cloud_storage | Optional (lazy) |

### Internal Dependency Graph

```
engine.py (orchestrator)
  ├─ revenue, opex, capex, debt, cashflow, metrics, renovations, fund_waterfall
  ├─ revenue_programs, time_grid, util, validator
  └─ scenarios (calls engine recursively)

api.py (handlers)
  ├─ rediq_bridge, validator, engine, scenarios
  ├─ market_integration, portfolio
  └─ (all lazy imports)

ingest/ingestion_pipeline.py
  ├─ rent_roll_parser, t12_parser, om_parser
  └─ time_grid

rediq_output.py → xlsx_writer.py
portfolio_xlsx.py (standalone openpyxl)
am_overlay_xlsx.py (standalone openpyxl)
pdf_onepager.py (standalone reportlab)
portfolio_pdf.py (standalone reportlab)
pptx_generator.py (standalone python-pptx)
```

**Circular dependencies:** None

---

## Recommendations (Prioritized)

### Before Building New Features (Foundation)

| Priority | Action | Impact | Effort |
|----------|--------|--------|--------|
| P0 | Extract `engine/formatters.py` | Eliminate 50+ lines of duplication | 1 hour |
| P0 | Extract `engine/brand.py` | Centralize colors, enable JV co-brand | 1 hour |
| P0 | Delete `_build_rate_curve_lookup()` dead code | Clean | 5 min |
| P0 | Move `_get_total_units()` to util.py | Dedup | 15 min |
| P1 | Extract `engine/xlsx_helpers.py` | Dedup Excel helpers | 1 hour |
| P1 | Share layout detection between bridge/output | Dedup, consistency | 2 hours |
| P1 | Create `tests/conftest.py` with shared fixtures | Reduce test boilerplate | 3 hours |
| P1 | Set up GitHub Actions CI | Enforce test health | 2 hours |
| P2 | Add tests for `fund_waterfall.py` | Close critical gap | 4 hours |
| P2 | Add tests for `rediq_bridge.py` (top 10 functions) | Close critical gap | 8 hours |
| P2 | Consolidate `rediq_output.py` to single entry point | Simplify API | 2 hours |
| P3 | Extract `rediq_validate_cashflows.py` → module | Reusable library | 4 hours |
| P3 | Add request validation to `api.py` | Production safety | 3 hours |

### Summary Stats

- **Production LOC:** ~14,000
- **Test LOC:** ~5,800
- **Tests:** 337 (all passing)
- **Dead code:** 36 lines (1 function)
- **Duplicated code:** ~250+ lines across 5+ files
- **Modules with zero tests:** 10
- **Architecture:** Clean (no circular deps, good module boundaries)
- **Accuracy:** 17/18 deals within 0.1% NOI
