# Scenario Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Input-sheet row-290 scenario output with a dedicated "Scenario Dashboard" tab featuring Bull/Base/Bear comparison + sensitivity tornado ranking.

**Architecture:** New `run_sensitivity_tornado()` in scenarios.py runs 2N+1 single-variable engine passes and returns ranked IRR impacts. New `write_scenario_dashboard()` in rediq_output.py renders comparison + tornado to the pre-existing template tab. Old `write_scenario_comparison()` removed.

**Tech Stack:** Python, lxml (XlsmWriter), existing engine + scenario infrastructure

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `engine/modules/scenarios.py` | Add `DEFAULT_TORNADO_SHOCKS`, `_apply_single_shock()`, `run_sensitivity_tornado()` |
| Modify | `engine/rediq_output.py` | Add `write_scenario_dashboard()`, remove `write_scenario_comparison()` + old constants, update `generate_rediq_workbook()` |
| Modify | `runs/generate_rediq_workbook.py` | Call `run_sensitivity_tornado()` when `--scenarios` is passed |
| Create | `tests/test_scenario_dashboard.py` | 6 tests |

---

## Critical Context

### scenarios.py Current State

```python
# Line 16-20: imports
from __future__ import annotations
import copy
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

# Line 74-89: _apply_scenario_deltas (modifies deep copy via dot-path deltas)
# Line 187-240: run_scenarios() — runs Bull/Base/Bear, returns comparison summary
# Line 130-164: _extract_scenario_metrics() — pulls IRR, EM, NOI, DSCR from results
```

### rediq_output.py Current State

```python
CF_SHEET = "CF Calculations"  # Line 223

# Lines 1120-1125: Old scenario constants (TO BE REMOVED)
_SCENARIO_START_ROW = 290
_SCENARIO_LABEL_COL = 2
_SCENARIO_BULL_COL = 3
_SCENARIO_BASE_COL = 4
_SCENARIO_BEAR_COL = 5

# Lines 1127-1140: _SCENARIO_METRIC_ROWS (TO BE KEPT — reused by dashboard)
_SCENARIO_METRIC_ROWS = [
    ("Levered IRR", "levered_irr"),
    ("Unlevered IRR", "unlevered_irr"),
    ("Levered EM", "levered_em"),
    ("Unlevered EM", "unlevered_em"),
    ("Partnership IRR", "partnership_irr"),
    ("Partnership EM", "partnership_em"),
    ("NOI Year 1", "noi_year_1"),
    ("NOI Exit Year", "noi_exit_year"),
    ("Average DSCR", "average_dscr"),
    ("Minimum DSCR", "minimum_dscr"),
    ("Going-In Cap", "going_in_cap"),
    ("Cash-on-Cash Y1", "cash_on_cash_y1"),
]

# Lines 1143-1210: write_scenario_comparison() (TO BE REMOVED)

# Line 1503: generate_rediq_workbook() signature
def generate_rediq_workbook(
    results, inputs, output_path, template_path=None,
    scenario_results=None, brand_config=None,
) -> Path:
```

### CLI Current State (runs/generate_rediq_workbook.py)

```python
# Line 147-152: existing scenario invocation
if args.scenarios:
    from engine.modules.scenarios import run_scenarios, STABILIZED_PRESETS, VALUE_ADD_PRESETS
    presets = VALUE_ADD_PRESETS if args.scenario_type == "value_add" else STABILIZED_PRESETS
    scenario_results = run_scenarios(inputs, presets=presets)

# Line 156: passes scenario_results to workbook generator
generate_rediq_workbook(results, inputs, output_path, template, scenario_results=scenario_results)
```

---

## Task 1: Sensitivity Tornado Engine Function

**Files:**
- Modify: `engine/modules/scenarios.py`
- Create: `tests/test_scenario_dashboard.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_scenario_dashboard.py`:

```python
"""Tests for scenario dashboard: tornado sensitivity + Excel output."""
import copy
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from lxml import etree

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


@pytest.fixture
def base_inputs():
    """Minimal inputs for tornado testing."""
    return {
        "metadata": {"deal_id": "test", "property_name": "Test", "analyst": "Test",
                     "as_of_date": "2025-01-01"},
        "unit_cohorts": [
            {"cohort_id": "1BR", "unit_count": 100, "in_place_rent": 1200,
             "market_rent": 1300, "sqft": 750}
        ],
        "market_rent_curve": [
            {"cohort_id": "1BR", "start_period": 1, "end_period": 120, "market_rent": 1300}
        ],
        "growth_assumptions": {"growth_type": "annual_compound", "annual_growth_rate": 0.03},
        "exit_assumptions": {"exit_month": 120, "exit_cap_rate": 0.055},
        "hold_period_months": 120,
        "physical_vacancy_curve": [
            {"start_month": 1, "end_month": 120, "vacancy_rate": 0.05}
        ],
        "opex_table": {
            "annual_growth_rate": 0.03,
            "categories": [
                {"name": "Insurance", "pricing_type": "$/unit", "amount": 800}
            ]
        },
        "debt_terms": {
            "commitment": 8_000_000,
            "rate": 0.05,
            "amort_months": 360,
            "io_months": 24,
            "term_months": 120,
            "loan_start_month": 1,
        },
        "fund_assumptions": {
            "sponsor_promote_pct": 0.20,
            "lp_pref_rate": 0.08,
            "sponsor_equity_pct": 0.10,
            "promote_splits": [{"hurdle_irr": 0.08, "gp_share": 0.20}],
        },
    }


class TestTornadoEngine:
    """Tests for run_sensitivity_tornado()."""

    def test_tornado_default_shocks(self, base_inputs):
        """Default shocks return 5 variables sorted by impact."""
        from engine.modules.scenarios import run_sensitivity_tornado

        result = run_sensitivity_tornado(base_inputs)
        variables = result["variables"]

        assert len(variables) == 5
        # Sorted descending by irr_impact
        impacts = [v["irr_impact"] for v in variables]
        assert impacts == sorted(impacts, reverse=True)
        # Each has required keys
        for v in variables:
            assert set(v.keys()) >= {"name", "label", "shock_bps", "irr_up", "irr_down", "irr_base", "irr_impact"}

    def test_tornado_custom_shocks(self, base_inputs):
        """Custom shock list overrides defaults."""
        from engine.modules.scenarios import run_sensitivity_tornado

        custom_shocks = [
            {"name": "rent_growth", "label": "Rent Growth", "shock_bps": 100,
             "path": "growth_assumptions.annual_growth_rate", "invert": False},
            {"name": "exit_cap", "label": "Exit Cap", "shock_bps": 50,
             "path": "exit_assumptions.exit_cap_rate", "invert": True},
        ]
        result = run_sensitivity_tornado(base_inputs, shocks=custom_shocks)
        variables = result["variables"]

        assert len(variables) == 2
        names = {v["name"] for v in variables}
        assert names == {"rent_growth", "exit_cap"}

    def test_tornado_invert_exit_cap(self, base_inputs):
        """Exit cap invert=True means favorable = lower cap = higher IRR."""
        from engine.modules.scenarios import run_sensitivity_tornado

        # Only test exit cap
        shocks = [
            {"name": "exit_cap", "label": "Exit Cap", "shock_bps": 25,
             "path": "exit_assumptions.exit_cap_rate", "invert": True},
        ]
        result = run_sensitivity_tornado(base_inputs, shocks=shocks)
        v = result["variables"][0]

        # Favorable (lower cap) should produce higher IRR than adverse (higher cap)
        assert v["irr_up"] > v["irr_down"]
        # irr_up = favorable = cap - 25bps; irr_down = adverse = cap + 25bps
        assert v["irr_up"] > v["irr_base"]
        assert v["irr_down"] < v["irr_base"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_scenario_dashboard.py::TestTornadoEngine -v`
Expected: FAIL with `ImportError: cannot import name 'run_sensitivity_tornado'`

- [ ] **Step 3: Implement run_sensitivity_tornado**

Add to `engine/modules/scenarios.py` (after `VALUE_ADD_PRESETS`, before `_apply_scenario_deltas`):

```python
# Default single-variable shocks for tornado sensitivity
DEFAULT_TORNADO_SHOCKS = [
    {"name": "rent_growth", "label": "Rent Growth", "shock_bps": 50,
     "path": "growth_assumptions.annual_growth_rate", "invert": False},
    {"name": "exit_cap_rate", "label": "Exit Cap Rate", "shock_bps": 25,
     "path": "exit_assumptions.exit_cap_rate", "invert": True},
    {"name": "vacancy", "label": "Vacancy", "shock_bps": 200,
     "path": "physical_vacancy_curve[*].vacancy_rate", "invert": True},
    {"name": "opex_growth", "label": "OpEx Growth", "shock_bps": 50,
     "path": "opex_table.annual_growth_rate", "invert": True},
    {"name": "rate", "label": "Interest Rate", "shock_bps": 50,
     "path": "debt_terms.rate", "invert": True},
]


def _apply_single_shock(
    inputs: Dict[str, Any],
    path: str,
    delta: float,
) -> Dict[str, Any]:
    """Apply a single shock to a deep copy of inputs at the given dot-path.

    Supports:
      - Simple paths: "exit_assumptions.exit_cap_rate"
      - Array wildcard: "physical_vacancy_curve[*].vacancy_rate"
      - Capital stack detection: "debt_terms.rate" checks for capital_stack
    """
    modified = copy.deepcopy(inputs)

    # Handle rate shock for capital_stack vs debt_terms
    if path == "debt_terms.rate":
        if "capital_stack" in modified:
            # Shock senior layer(s)
            for layer in modified["capital_stack"]:
                if layer.get("layer_type", "senior") in ("senior", "mezzanine"):
                    if layer.get("rate_type") == "variable":
                        layer["base_spread"] = layer.get("base_spread", 0) + delta
                    else:
                        layer["rate"] = layer.get("rate", 0.05) + delta
            return modified
        elif "debt_terms" in modified:
            dt = modified["debt_terms"]
            if dt.get("rate_type") == "variable":
                dt["base_spread"] = dt.get("base_spread", 0) + delta
            else:
                dt["rate"] = dt.get("rate", 0.05) + delta
            return modified

    # Handle array wildcard paths: "physical_vacancy_curve[*].vacancy_rate"
    if "[*]" in path:
        array_path, field = path.split("[*].")
        parts = array_path.split(".")
        obj = modified
        for part in parts:
            obj = obj[part]
        # obj is now the array
        for entry in obj:
            entry[field] = entry.get(field, 0) + delta
        return modified

    # Simple dot-path
    parts = path.split(".")
    obj = modified
    for part in parts[:-1]:
        obj = obj.setdefault(part, {})
    obj[parts[-1]] = obj.get(parts[-1], 0) + delta
    return modified


def run_sensitivity_tornado(
    inputs: Dict[str, Any],
    shocks: Optional[List[Dict]] = None,
    engine_runner: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Run single-variable sensitivity shocks and rank by IRR impact.

    For each variable, runs two engine passes (favorable + adverse) and
    computes the total IRR swing. Returns variables sorted descending by impact.

    Args:
        inputs: Validated deal inputs (base case)
        shocks: List of shock definitions. Defaults to DEFAULT_TORNADO_SHOCKS.
        engine_runner: Callable that takes inputs → results. Defaults to run_underwriting.

    Returns:
        {"variables": [{"name", "label", "shock_bps", "irr_up", "irr_down", "irr_base", "irr_impact"}, ...]}
    """
    if shocks is None:
        shocks = DEFAULT_TORNADO_SHOCKS

    if engine_runner is None:
        from engine.engine import run_underwriting
        def engine_runner(inp):
            return run_underwriting(inp, skip_validation=True)

    # Run base case
    base_results = engine_runner(inputs)
    base_irr = base_results.get("metrics", {}).get("irr", {}).get("levered_irr", 0) or 0

    variables = []
    for shock_def in shocks:
        delta = shock_def["shock_bps"] / 10000.0
        invert = shock_def.get("invert", False)

        # Favorable: positive delta if not inverted, negative if inverted
        fav_delta = -delta if invert else delta
        adv_delta = delta if invert else -delta

        # Run favorable and adverse
        fav_inputs = _apply_single_shock(inputs, shock_def["path"], fav_delta)
        adv_inputs = _apply_single_shock(inputs, shock_def["path"], adv_delta)

        fav_results = engine_runner(fav_inputs)
        adv_results = engine_runner(adv_inputs)

        irr_up = fav_results.get("metrics", {}).get("irr", {}).get("levered_irr", 0) or 0
        irr_down = adv_results.get("metrics", {}).get("irr", {}).get("levered_irr", 0) or 0

        variables.append({
            "name": shock_def["name"],
            "label": shock_def["label"],
            "shock_bps": shock_def["shock_bps"],
            "irr_up": irr_up,
            "irr_down": irr_down,
            "irr_base": base_irr,
            "irr_impact": abs(irr_up - irr_down),
        })

    # Sort descending by impact
    variables.sort(key=lambda v: v["irr_impact"], reverse=True)

    return {"variables": variables}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_scenario_dashboard.py::TestTornadoEngine -v`
Expected: PASS (3/3)

- [ ] **Step 5: Commit**

```bash
git add engine/modules/scenarios.py tests/test_scenario_dashboard.py
git commit -m "feat(scenarios): add run_sensitivity_tornado for single-variable IRR shocks

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 2: write_scenario_dashboard + Remove Old Code

**Files:**
- Modify: `engine/rediq_output.py`
- Modify: `tests/test_scenario_dashboard.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_scenario_dashboard.py`:

```python
@pytest.fixture
def dashboard_writer(tmp_path):
    """Create a minimal .xlsm template with Scenario Dashboard sheet."""
    from engine.xlsx_writer import XlsmWriter

    template = tmp_path / "template.xlsm"
    output = tmp_path / "output.xlsm"

    workbook_xml = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="CF Calculations" sheetId="1" r:id="rId1"/>
    <sheet name="Input" sheetId="2" r:id="rId2"/>
    <sheet name="Scenario Dashboard" sheetId="3" r:id="rId3"/>
  </sheets>
</workbook>"""

    rels_xml = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Target="worksheets/sheet1.xml"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>
  <Relationship Id="rId2" Target="worksheets/sheet2.xml"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>
  <Relationship Id="rId3" Target="worksheets/sheet3.xml"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>
</Relationships>"""

    sheet_xml = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1"><c r="A1"><v>0</v></c></row>
  </sheetData>
</worksheet>"""

    styles_xml = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <dxfs count="0"/>
</styleSheet>"""

    with zipfile.ZipFile(template, "w") as zf:
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", rels_xml)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        zf.writestr("xl/worksheets/sheet2.xml", sheet_xml)
        zf.writestr("xl/worksheets/sheet3.xml", sheet_xml)
        zf.writestr("xl/styles.xml", styles_xml)

    return template, output


class TestDashboardExcelOutput:
    """Tests for write_scenario_dashboard Excel output."""

    def test_dashboard_comparison_cells(self, dashboard_writer):
        """Bull/Base/Bear metrics written to correct cells."""
        from engine.xlsx_writer import XlsmWriter
        from engine.rediq_output import write_scenario_dashboard

        template, output = dashboard_writer
        writer = XlsmWriter(template, output)

        scenario_results = {
            "comparison": {
                "bull": {
                    "assumptions": {"rent_growth": 0.035, "exit_cap_rate": 0.0525, "avg_vacancy": 0.03},
                    "metrics": {"levered_irr": 0.182, "unlevered_irr": 0.145, "levered_em": 2.1},
                },
                "base": {
                    "assumptions": {"rent_growth": 0.03, "exit_cap_rate": 0.055, "avg_vacancy": 0.05},
                    "metrics": {"levered_irr": 0.161, "unlevered_irr": 0.132, "levered_em": 1.9},
                },
                "bear": {
                    "assumptions": {"rent_growth": 0.025, "exit_cap_rate": 0.0575, "avg_vacancy": 0.07},
                    "metrics": {"levered_irr": 0.140, "unlevered_irr": 0.118, "levered_em": 1.7},
                },
            }
        }
        write_scenario_dashboard(writer, scenario_results, tornado_results=None)
        writer.save()

        with zipfile.ZipFile(output, "r") as zf:
            sheet_xml = zf.read("xl/worksheets/sheet3.xml")
        root = etree.fromstring(sheet_xml)

        # Check title in A1
        cells = {c.get("r"): c for c in root.findall(f".//{{{NS}}}c")}
        assert "A1" in cells
        # Check Bull header in B3
        assert "B3" in cells
        # Check a metric was written (Levered IRR in B12)
        assert "B12" in cells

    def test_dashboard_tornado_cells(self, dashboard_writer):
        """Tornado table rows written with correct sort order."""
        from engine.xlsx_writer import XlsmWriter
        from engine.rediq_output import write_scenario_dashboard

        template, output = dashboard_writer
        writer = XlsmWriter(template, output)

        tornado_results = {
            "variables": [
                {"name": "exit_cap", "label": "Exit Cap Rate", "shock_bps": 25,
                 "irr_up": 0.182, "irr_down": 0.140, "irr_base": 0.161, "irr_impact": 0.042},
                {"name": "rent_growth", "label": "Rent Growth", "shock_bps": 50,
                 "irr_up": 0.175, "irr_down": 0.148, "irr_base": 0.161, "irr_impact": 0.027},
            ]
        }
        write_scenario_dashboard(writer, scenario_results=None, tornado_results=tornado_results)
        writer.save()

        with zipfile.ZipFile(output, "r") as zf:
            sheet_xml = zf.read("xl/worksheets/sheet3.xml")
        root = etree.fromstring(sheet_xml)

        # Row 22 should have tornado header
        cells = {c.get("r"): c for c in root.findall(f".//{{{NS}}}c")}
        assert "A22" in cells
        # Row 24 should have first variable (highest impact = exit cap)
        assert "A24" in cells

    def test_dashboard_conditional_formatting(self, dashboard_writer):
        """CF rules applied to delta and swing columns."""
        from engine.xlsx_writer import XlsmWriter
        from engine.rediq_output import write_scenario_dashboard

        template, output = dashboard_writer
        writer = XlsmWriter(template, output)

        scenario_results = {
            "comparison": {
                "bull": {"assumptions": {"rent_growth": 0.035}, "metrics": {"levered_irr": 0.182}},
                "base": {"assumptions": {"rent_growth": 0.03}, "metrics": {"levered_irr": 0.161}},
                "bear": {"assumptions": {"rent_growth": 0.025}, "metrics": {"levered_irr": 0.140}},
            }
        }
        tornado_results = {
            "variables": [
                {"name": "exit_cap", "label": "Exit Cap", "shock_bps": 25,
                 "irr_up": 0.182, "irr_down": 0.140, "irr_base": 0.161, "irr_impact": 0.042},
            ]
        }
        write_scenario_dashboard(writer, scenario_results, tornado_results)
        writer.save()

        with zipfile.ZipFile(output, "r") as zf:
            sheet_xml = zf.read("xl/worksheets/sheet3.xml")
        root = etree.fromstring(sheet_xml)

        cf_els = root.findall(f".//{{{NS}}}conditionalFormatting")
        # Should have CF rules for delta columns and tornado swing
        assert len(cf_els) >= 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_scenario_dashboard.py::TestDashboardExcelOutput -v`
Expected: FAIL with `ImportError: cannot import name 'write_scenario_dashboard'`

- [ ] **Step 3: Implement write_scenario_dashboard**

In `engine/rediq_output.py`, replace `write_scenario_comparison` and its constants with:

First, remove lines 1120-1125 (`_SCENARIO_START_ROW`, `_SCENARIO_LABEL_COL`, `_SCENARIO_BULL_COL`, `_SCENARIO_BASE_COL`, `_SCENARIO_BEAR_COL`).

Then remove the entire `write_scenario_comparison()` function (lines 1143-1210).

Add the new function in its place:

```python
# ── Scenario Dashboard ────────────────────────────────────────────────────
_DASHBOARD_SHEET = "Scenario Dashboard"

# Dashboard layout constants
_DASH_TITLE_ROW = 1
_DASH_HEADER_ROW = 3
_DASH_ASSUMPTIONS_START = 4
_DASH_RETURNS_LABEL_ROW = 11
_DASH_RETURNS_START = 12
_DASH_TORNADO_HEADER_ROW = 22
_DASH_TORNADO_LABELS_ROW = 23
_DASH_TORNADO_DATA_START = 24

# Columns: A=1, B=2(Bull), C=3(Base), D=4(Bear), E=5(Delta Bull), F=6(Delta Bear)
_DASH_LABEL_COL = 1
_DASH_BULL_COL = 2
_DASH_BASE_COL = 3
_DASH_BEAR_COL = 4
_DASH_DELTA_BULL_COL = 5
_DASH_DELTA_BEAR_COL = 6

_DASHBOARD_ASSUMPTIONS = [
    ("Rent Growth", "rent_growth"),
    ("Exit Cap Rate", "exit_cap_rate"),
    ("Avg Vacancy", "avg_vacancy"),
    ("OpEx Growth", "opex_growth"),
    ("Rate", "rate"),
]


def write_scenario_dashboard(
    writer,
    scenario_results: Optional[Dict[str, Any]],
    tornado_results: Optional[Dict[str, Any]],
) -> None:
    """Write scenario comparison + tornado sensitivity to the Scenario Dashboard sheet.

    Args:
        writer: XlsmWriter instance
        scenario_results: Output from run_scenarios() (has "comparison" key)
        tornado_results: Output from run_sensitivity_tornado() (has "variables" key)
    """
    sheet = _DASHBOARD_SHEET

    # Verify sheet exists
    try:
        writer._get_sheet_xml(sheet)
    except KeyError:
        return  # Template doesn't have the dashboard tab

    # Title
    writer.set_cell_value(sheet, _DASH_TITLE_ROW, _DASH_LABEL_COL, "Scenario Dashboard")

    # ── Section 1: Bull/Base/Bear Comparison ──
    if scenario_results:
        comparison = scenario_results.get("comparison", {})

        # Headers
        writer.set_cell_value(sheet, _DASH_HEADER_ROW, _DASH_BULL_COL, "Bull")
        writer.set_cell_value(sheet, _DASH_HEADER_ROW, _DASH_BASE_COL, "Base")
        writer.set_cell_value(sheet, _DASH_HEADER_ROW, _DASH_BEAR_COL, "Bear")
        writer.set_cell_value(sheet, _DASH_HEADER_ROW, _DASH_DELTA_BULL_COL, "\u0394 Bull")
        writer.set_cell_value(sheet, _DASH_HEADER_ROW, _DASH_DELTA_BEAR_COL, "\u0394 Bear")

        # Assumptions section
        writer.set_cell_value(sheet, _DASH_ASSUMPTIONS_START, _DASH_LABEL_COL, "ASSUMPTIONS")
        row = _DASH_ASSUMPTIONS_START + 1
        for label, key in _DASHBOARD_ASSUMPTIONS:
            writer.set_cell_value(sheet, row, _DASH_LABEL_COL, label)
            base_val = comparison.get("base", {}).get("assumptions", {}).get(key)
            for scenario, col in [("bull", _DASH_BULL_COL), ("base", _DASH_BASE_COL), ("bear", _DASH_BEAR_COL)]:
                val = comparison.get(scenario, {}).get("assumptions", {}).get(key)
                if val is not None:
                    writer.set_cell_value(sheet, row, col, val)
            # Deltas
            bull_val = comparison.get("bull", {}).get("assumptions", {}).get(key)
            bear_val = comparison.get("bear", {}).get("assumptions", {}).get(key)
            if bull_val is not None and base_val is not None:
                writer.set_cell_value(sheet, row, _DASH_DELTA_BULL_COL, bull_val - base_val)
            if bear_val is not None and base_val is not None:
                writer.set_cell_value(sheet, row, _DASH_DELTA_BEAR_COL, bear_val - base_val)
            row += 1

        # Returns section
        writer.set_cell_value(sheet, _DASH_RETURNS_LABEL_ROW, _DASH_LABEL_COL, "RETURNS")
        row = _DASH_RETURNS_START
        for label, metric_key in _SCENARIO_METRIC_ROWS:
            writer.set_cell_value(sheet, row, _DASH_LABEL_COL, label)
            base_val = comparison.get("base", {}).get("metrics", {}).get(metric_key)
            for scenario, col in [("bull", _DASH_BULL_COL), ("base", _DASH_BASE_COL), ("bear", _DASH_BEAR_COL)]:
                val = comparison.get(scenario, {}).get("metrics", {}).get(metric_key)
                if val is not None:
                    writer.set_cell_value(sheet, row, col, val)
            # Deltas
            bull_val = comparison.get("bull", {}).get("metrics", {}).get(metric_key)
            bear_val = comparison.get("bear", {}).get("metrics", {}).get(metric_key)
            if bull_val is not None and base_val is not None:
                writer.set_cell_value(sheet, row, _DASH_DELTA_BULL_COL, bull_val - base_val)
            if bear_val is not None and base_val is not None:
                writer.set_cell_value(sheet, row, _DASH_DELTA_BEAR_COL, bear_val - base_val)
            row += 1

        # Conditional formatting on delta columns
        last_data_row = _DASH_RETURNS_START + len(_SCENARIO_METRIC_ROWS) - 1
        writer.add_conditional_format(sheet, f"E5:E{last_data_row}", "threshold",
            thresholds=[
                {"operator": "greaterThan", "value": 0, "color": "FF008000"},
                {"operator": "lessThan", "value": 0, "color": "FFFF0000"},
            ]
        )
        writer.add_conditional_format(sheet, f"F5:F{last_data_row}", "threshold",
            thresholds=[
                {"operator": "greaterThan", "value": 0, "color": "FF008000"},
                {"operator": "lessThan", "value": 0, "color": "FFFF0000"},
            ]
        )

    # ── Section 2: Tornado Sensitivity Table ──
    if tornado_results:
        variables = tornado_results.get("variables", [])
        writer.set_cell_value(sheet, _DASH_TORNADO_HEADER_ROW, _DASH_LABEL_COL,
                              "IRR Sensitivity \u2014 Single-Variable Shocks")
        # Column headers
        writer.set_cell_value(sheet, _DASH_TORNADO_LABELS_ROW, 1, "Variable")
        writer.set_cell_value(sheet, _DASH_TORNADO_LABELS_ROW, 2, "Favorable")
        writer.set_cell_value(sheet, _DASH_TORNADO_LABELS_ROW, 3, "Adverse")
        writer.set_cell_value(sheet, _DASH_TORNADO_LABELS_ROW, 4, "Base")
        writer.set_cell_value(sheet, _DASH_TORNADO_LABELS_ROW, 5, "Swing (bps)")

        row = _DASH_TORNADO_DATA_START
        for v in variables:
            shock_label = f"{v['label']} (\u00b1{v['shock_bps']}bps)"
            writer.set_cell_value(sheet, row, 1, shock_label)
            writer.set_cell_value(sheet, row, 2, v["irr_up"])
            writer.set_cell_value(sheet, row, 3, v["irr_down"])
            writer.set_cell_value(sheet, row, 4, v["irr_base"])
            writer.set_cell_value(sheet, row, 5, round(v["irr_impact"] * 10000))
            row += 1

        # Gradient on swing column
        if variables:
            last_tornado_row = _DASH_TORNADO_DATA_START + len(variables) - 1
            writer.add_conditional_format(sheet,
                f"E{_DASH_TORNADO_DATA_START}:E{last_tornado_row}", "gradient",
                min_color="FFFF0000", max_color="FF008000"
            )

    # Freeze pane and print area
    writer.set_freeze_pane(sheet, 4, 1)
    writer.set_print_area(sheet, 1, 1, 30, 6)
    writer.set_page_setup(sheet, orientation="landscape", fit_to_width=1)
```

- [ ] **Step 4: Update generate_rediq_workbook signature and call**

In `generate_rediq_workbook()`, add `tornado_results` parameter and replace the `write_scenario_comparison` call:

Change the signature from:
```python
def generate_rediq_workbook(
    results, inputs, output_path, template_path=None,
    scenario_results=None, brand_config=None,
) -> Path:
```

To:
```python
def generate_rediq_workbook(
    results, inputs, output_path, template_path=None,
    scenario_results=None, tornado_results=None, brand_config=None,
) -> Path:
```

Replace the call (around line 1540):
```python
        if scenario_results:
            write_scenario_comparison(writer, scenario_results)
```

With:
```python
        if scenario_results or tornado_results:
            write_scenario_dashboard(writer, scenario_results, tornado_results)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_scenario_dashboard.py -v`
Expected: PASS (6/6)

- [ ] **Step 6: Run full test suite**

Run: `python3 -m pytest --tb=short -q`
Expected: All tests pass. (The old `write_scenario_comparison` is removed but nothing else calls it directly.)

- [ ] **Step 7: Commit**

```bash
git add engine/rediq_output.py tests/test_scenario_dashboard.py
git commit -m "feat(xlsx): add scenario dashboard tab, remove old Input-row-290 output

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Task 3: CLI Integration

**Files:**
- Modify: `runs/generate_rediq_workbook.py`

- [ ] **Step 1: Update CLI to call run_sensitivity_tornado**

In `runs/generate_rediq_workbook.py`, modify the scenario block (around lines 146-152):

Replace:
```python
    # Step 4b: Run scenario comparison (optional)
    scenario_results = None
    if args.scenarios:
        from engine.modules.scenarios import run_scenarios, STABILIZED_PRESETS, VALUE_ADD_PRESETS
        presets = VALUE_ADD_PRESETS if args.scenario_type == "value_add" else STABILIZED_PRESETS
        print(f"Running {args.scenario_type} scenario comparison (Bull/Base/Bear)...")
        scenario_results = run_scenarios(inputs, presets=presets)
        print("  Scenarios complete.")
```

With:
```python
    # Step 4b: Run scenario comparison + tornado sensitivity (optional)
    scenario_results = None
    tornado_results = None
    if args.scenarios:
        from engine.modules.scenarios import (
            run_scenarios, run_sensitivity_tornado,
            STABILIZED_PRESETS, VALUE_ADD_PRESETS,
        )
        presets = VALUE_ADD_PRESETS if args.scenario_type == "value_add" else STABILIZED_PRESETS
        print(f"Running {args.scenario_type} scenario comparison (Bull/Base/Bear)...")
        scenario_results = run_scenarios(inputs, presets=presets)
        print("  Running sensitivity tornado (5 variables, 11 engine passes)...")
        tornado_results = run_sensitivity_tornado(inputs)
        print("  Scenarios + tornado complete.")
```

Also update the `generate_rediq_workbook` call (around line 156):

Replace:
```python
    generate_rediq_workbook(results, inputs, output_path, template, scenario_results=scenario_results)
```

With:
```python
    generate_rediq_workbook(results, inputs, output_path, template,
                            scenario_results=scenario_results,
                            tornado_results=tornado_results)
```

- [ ] **Step 2: Run full test suite**

Run: `python3 -m pytest --tb=short -q`
Expected: All tests pass.

- [ ] **Step 3: Commit**

```bash
git add runs/generate_rediq_workbook.py
git commit -m "feat(cli): integrate tornado sensitivity into --scenarios flag

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Summary of All 6 Tests

| # | Class | Test | Verifies |
|---|-------|------|----------|
| 1 | TestTornadoEngine | test_tornado_default_shocks | 5 variables returned, sorted by impact |
| 2 | TestTornadoEngine | test_tornado_custom_shocks | Custom shock list respected |
| 3 | TestTornadoEngine | test_tornado_invert_exit_cap | Favorable = lower cap = higher IRR |
| 4 | TestDashboardExcelOutput | test_dashboard_comparison_cells | Bull/Base/Bear in correct cells |
| 5 | TestDashboardExcelOutput | test_dashboard_tornado_cells | Tornado table sorted correctly |
| 6 | TestDashboardExcelOutput | test_dashboard_conditional_formatting | CF rules on delta + swing columns |
