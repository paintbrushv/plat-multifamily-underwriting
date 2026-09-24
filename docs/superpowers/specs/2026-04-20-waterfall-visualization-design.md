# Task 2.5 — Waterfall Visualization (Excel + PDF)

**Date:** 2026-04-20
**Status:** Design approved
**Complexity:** M
**Dependencies:** 1.1 (multi-tier waterfall engine), 2.1 (template), 2.2 (CF/named range support)

---

## Overview

Enhance the Excel Waterfall tab with a summary metrics section and formatting, plus add a conditional stacked bar chart to the PDF one-pager showing tier-by-tier distributions by year. No image embedding in Excel — data tables only.

---

## Excel — Waterfall Tab Enhancement

### Summary Section (rows 2-7)

Written above the existing 4-tier balance layout (rows 9-69), which remains untouched.

| Row | Col C | Col D | Col E |
|-----|-------|-------|-------|
| 2 | **WATERFALL SUMMARY** (header, merged C-E) | | |
| 3 | Total to LP | Total to GP | Total Distributed |
| 4 | Partnership IRR | Partnership EM | |
| 5 | GP Coinvest % | Clawback (if any) | |
| 7 | **TIER BREAKDOWN** (header) | | |

### Data Sources

- `Total to LP` / `Total to GP`: `summary["total_lp_distributions"]` / `summary["total_sponsor_distributions"]`
- `Partnership IRR`: `summary["partnership_irr"]`
- `Partnership EM`: `summary["partnership_equity_multiple"]`
- `GP Coinvest %`: `summary["gp_coinvest_pct"]` (0 if none)
- `Clawback`: `promote["clawback_amount"]` (blank if not enabled)

### Formatting Enhancements

- **Conditional formatting** on tier distribution rows: green for positive CF, red (negative_red) for negative
- **Named ranges**: `WF_Total_LP` (row 3, col C), `WF_Total_GP` (row 3, col D), `WF_Partnership_IRR` (row 4, col C), `WF_Partnership_EM` (row 4, col D)
- **Bold** summary values in rows 3-5
- **Header styling**: dark background text via existing CF threshold pattern

### Backward Compatibility

- Single-tier deals: summary still written (total to LP/GP, IRR, EM all valid)
- No `promote["by_tier"]` (legacy path): summary section populated from `summary` dict directly
- Existing tier-detail rows (9-69) remain unchanged in structure

---

## PDF — Conditional Stacked Bar Chart

### Trigger Condition

Chart section rendered ONLY when `promote["by_tier"]` exists and has >1 entry. Single-tier deals skip this section entirely.

### Chart Specification

- **Type**: Vertical stacked bar chart
- **X-axis**: Analysis years (Year 1, Year 2, ... Year N)
- **Y-axis**: Dollar amount (formatted as `$X.XM` or `$X.XK`)
- **Dimensions**: 6.5" × 2.0" (matches existing NOI/LCF chart)
- **DPI**: 150

### Stack Segments (bottom to top)

| Segment | Color | Source |
|---------|-------|--------|
| Return of Capital | #0a0a0a (dark) | LP capital returned (negative balance reduction) |
| LP Pref | #334155 (slate) | LP pref distributions |
| LP Promote Share | #c9a66c (gold) | LP share of promote above pref |
| GP Promote | #a68b52 (dark gold) | GP promote/carry |

### Data Derivation

From `fund_waterfall["by_year"]` for each year:
- **GP Promote** = `promote_payment` (direct field)
- **LP Total** = `lp_share` (includes capital return + pref + promote share)
- **Segment split** (LP decomposition): Use cumulative balance tracking from `by_tier` to determine what fraction of each year's LP distribution is capital return vs pref vs excess. If tier-level annual breakdown unavailable, approximate:
  - Capital return portion = min(lp_share, remaining_unreturned_capital)
  - Pref portion = min(remaining, pref_accrual_for_year)
  - Excess = remainder

### Chart Styling

- Matches existing `_generate_cf_chart()` patterns:
  - Spines: top/right hidden
  - Legend: upper left, no frame, 4 entries
  - Background: white
  - Font: Helvetica labels, consistent with brand
- Title: "Waterfall Distribution by Year"

### PDF Placement

- New section after the NOI/Levered CF chart
- Section header: "WATERFALL DISTRIBUTION" (same style as other section headers)
- Natural page break if content exceeds page height (reportlab handles automatically)

---

## Implementation Plan

### Files to Modify

| File | Change |
|------|--------|
| `engine/rediq_output.py` | Add `write_waterfall_summary()` function; add CF rules + named ranges for Waterfall sheet |
| `engine/pdf_onepager.py` | Add `_generate_waterfall_chart()` function; conditional section in `generate_onepager()` |
| `tests/test_waterfall_viz.py` | New test file: 6+ tests |

### No Files Created (besides test file)

All logic lives in existing modules — no new engine files needed.

---

## Test Requirements

| Test | Description |
|------|-------------|
| `test_waterfall_summary_values` | Summary metrics match fund_waterfall output |
| `test_waterfall_summary_single_tier` | Single-tier deal still gets summary section |
| `test_waterfall_named_ranges` | Named ranges defined correctly |
| `test_waterfall_conditional_formatting` | CF rules applied to tier rows |
| `test_waterfall_chart_generated` | Multi-tier deal produces PNG bytes |
| `test_waterfall_chart_skipped_single_tier` | Single-tier deal skips chart |
| `test_waterfall_chart_segments` | Stacked segments sum to total distributions per year |

---

## Non-Goals

- No image embedding in Excel (too complex for ROI)
- No interactive chart in Excel (would require chart XML, out of scope)
- No separate waterfall PDF document (integrated into one-pager)
- No GP IRR as separate metric (would require separate cashflow series construction — defer)
