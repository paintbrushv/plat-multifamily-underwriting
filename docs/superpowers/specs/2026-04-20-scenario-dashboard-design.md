# Task 2.4 — Scenario Dashboard Excel Tab

**Date:** 2026-04-20
**Status:** Approved
**Complexity:** M

---

## Summary

Replace the row-290 scenario comparison on the Input sheet with a dedicated "Scenario Dashboard" tab. Add a sensitivity tornado engine function that runs single-variable shocks and ranks assumptions by IRR impact. Apply conditional formatting to highlight favorable/adverse deltas.

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Tornado data source | New `run_sensitivity_tornado()` in scenarios.py | Clean separation — engine produces data, Excel renders it |
| Sheet creation | Pre-existing blank tab in template | Avoids dynamic sheet creation complexity (Content_Types, rels) |
| Shock configuration | Configurable with defaults | Stabilized vs value-add deals use different magnitudes |
| Old scenario output | Removed (Input row 290) | Dashboard replaces it completely |
| Engine runs for tornado | 2N+1 (base + 2 per variable = 11) | Single-variable isolation requires independent runs |

## New Engine Function

### `run_sensitivity_tornado(inputs, shocks=None, engine_runner=None)`

Located in `engine/modules/scenarios.py`.

**Parameters:**
- `inputs`: Validated deal inputs (base case)
- `shocks`: Optional list of shock definitions. Defaults to `DEFAULT_TORNADO_SHOCKS`.
- `engine_runner`: Callable that takes inputs → results. Defaults to `run_underwriting(skip_validation=True)`.

**Shock definition:**
```python
{"name": "exit_cap_rate", "label": "Exit Cap Rate", "shock_bps": 25, "path": "exit_assumptions.exit_cap_rate", "invert": True}
```

- `path`: Dot-path into inputs dict (same pattern as portfolio stress `_rerun_deal_with_overrides`)
- `invert`: When True, favorable = negative delta (lower cap = higher valuation)
- `shock_bps`: Magnitude of shock in basis points (applied as ±shock_bps/10000)

**Default shocks (`DEFAULT_TORNADO_SHOCKS`):**

| Variable | Label | shock_bps | path | invert |
|----------|-------|-----------|------|--------|
| rent_growth | Rent Growth | 50 | `growth_assumptions.annual_growth_rate` | False |
| exit_cap_rate | Exit Cap Rate | 25 | `exit_assumptions.exit_cap_rate` | True |
| vacancy | Vacancy | 200 | `physical_vacancy_curve[*].vacancy_rate` | True |
| opex_growth | OpEx Growth | 50 | `opex_table.annual_growth_rate` | True |
| rate | Interest Rate | 50 | `debt_terms.rate` | True |

Note: Vacancy uses `[*]` wildcard — applies shock uniformly to all entries in the array.
Rate shock detects `capital_stack` vs `debt_terms` (same logic as portfolio stress).

**Returns:**
```python
{
    "variables": [
        {
            "name": "exit_cap_rate",
            "label": "Exit Cap Rate",
            "shock_bps": 25,
            "irr_up": 0.182,       # IRR when shocked favorably
            "irr_down": 0.143,     # IRR when shocked adversely
            "irr_base": 0.161,     # Base case IRR
            "irr_impact": 0.039,   # abs(irr_up - irr_down)
        },
        ...  # sorted descending by irr_impact
    ]
}
```

**Internal helper:** `_apply_single_shock(inputs, path, delta)` — deep copies inputs, applies a single dot-path delta. For `[*]` paths, iterates all array entries. For variable-rate loans, shocks `base_spread` instead of `rate`.

## Excel Dashboard Layout

### Template Requirement

A blank "Scenario Dashboard" sheet must exist in `excel/rediq_clone.xlsm`. The writer addresses it by name.

### Column Layout

| Col | A | B | C | D | E | F |
|-----|---|---|---|---|---|---|
| Use | Labels | Bull | Base | Bear | Delta (Bull-Base) | Delta (Bear-Base) |

### Row Layout

**Rows 1-20: Scenario Comparison**

```
Row 1:  "Scenario Dashboard" (title)
Row 3:  ""  "Bull"  "Base"  "Bear"  "Δ Bull"  "Δ Bear"
Row 4:  "ASSUMPTIONS"
Row 5:  "Rent Growth"        3.5%   3.0%   2.5%   +0.005  -0.005
Row 6:  "Exit Cap Rate"      5.25%  5.50%  5.75%  -0.0025 +0.0025
Row 7:  "Avg Vacancy"        3.0%   5.0%   7.0%   -0.02   +0.02
Row 8:  "OpEx Growth"        2.5%   3.0%   3.5%   -0.005  +0.005
Row 9:  "Rate"               4.5%   5.0%   5.5%   -0.005  +0.005
Row 10: (blank)
Row 11: "RETURNS"
Row 12: "Levered IRR"
Row 13: "Unlevered IRR"
Row 14: "Levered EM"
Row 15: "Partnership IRR"
Row 16: "NOI Year 1"
Row 17: "Average DSCR"
Row 18: "Min DSCR"
Row 19: "Going-In Cap"
Row 20: "Cash-on-Cash Y1"
```

Delta columns (E, F) contain raw numeric deltas (Bull metric - Base metric, Bear metric - Base metric).

**Rows 22-30: Tornado Sensitivity Table**

```
Row 22: "IRR Sensitivity — Single-Variable Shocks"
Row 23: "Variable"  "Favorable"  "Adverse"  "Base"  "Swing (bps)"
Row 24+: One row per variable, sorted descending by swing
```

"Swing (bps)" = `irr_impact * 10000` (displayed as integer bps for readability).

### Conditional Formatting Applied

| Range | Rule | Colors |
|-------|------|--------|
| E5:E20 (Bull delta) | threshold | >0 green, <0 red |
| F5:F20 (Bear delta) | threshold | >0 green, <0 red |
| E24:E29 (Tornado swing) | gradient | green (high) → red (low) |

### Other Formatting

- Freeze pane: Row 3, Col A
- Print area: A1:F30
- Page setup: landscape, fit-to-width=1

## Integration Points

### `generate_rediq_workbook()` Signature Change

```python
def generate_rediq_workbook(
    results, inputs, output_path, template_path=None,
    scenario_results=None,
    tornado_results=None,   # NEW
    brand_config=None,
) -> Path:
```

### `write_scenario_dashboard(writer, scenario_results, tornado_results)`

New function in `rediq_output.py`. Called from `generate_rediq_workbook()` when `scenario_results` is not None. Replaces `write_scenario_comparison()`.

### CLI (`runs/generate_rediq_workbook.py`)

When `--scenarios` flag is passed:
1. Runs `run_scenarios(inputs)` (existing)
2. Runs `run_sensitivity_tornado(inputs)` (new)
3. Passes both to `generate_rediq_workbook()`

### Removal

- Delete `write_scenario_comparison()` function
- Delete `_SCENARIO_START_ROW`, `_SCENARIO_LABEL_COL`, `_SCENARIO_BULL_COL`, `_SCENARIO_BASE_COL`, `_SCENARIO_BEAR_COL` constants
- Keep `_SCENARIO_METRIC_ROWS` (reused by dashboard as the metrics list)

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `engine/modules/scenarios.py` | Add `run_sensitivity_tornado()`, `_apply_single_shock()`, `DEFAULT_TORNADO_SHOCKS` |
| Modify | `engine/rediq_output.py` | Add `write_scenario_dashboard()`, remove `write_scenario_comparison()` + old constants, update `generate_rediq_workbook()` |
| Modify | `runs/generate_rediq_workbook.py` | Call `run_sensitivity_tornado()` alongside `run_scenarios()` |
| Modify | `excel/rediq_clone.xlsm` | Add blank "Scenario Dashboard" tab (manual template edit) |
| Create | `tests/test_scenario_dashboard.py` | 6 tests |

## Test Plan (6 tests)

| # | Test | What it verifies |
|---|------|-----------------|
| 1 | `test_tornado_default_shocks` | 5 variables returned, sorted descending by irr_impact |
| 2 | `test_tornado_custom_shocks` | Custom shock list overrides defaults, correct count |
| 3 | `test_tornado_invert_exit_cap` | Exit cap favorable = lower value produces higher IRR |
| 4 | `test_dashboard_comparison_cells` | Bull/Base/Bear metrics + deltas written to correct sheet/row/col |
| 5 | `test_dashboard_tornado_cells` | Tornado table rows match sorted engine output |
| 6 | `test_dashboard_conditional_formatting` | CF rules on delta columns (threshold) and swing column (gradient) |

## Out of Scope

- No actual Excel chart object (tornado bar chart) — just the data table. Charts require complex drawing XML.
- No portfolio-level scenario dashboard (single-deal only)
- No sensitivity surface (2D grid) — that's a separate concern
- Template tab addition is a manual step (documented in plan)
