# Unit-Level Renovation Scheduling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add optional per-unit renovation overrides with seasonal downtime multipliers to the existing cohort-level renovation engine.

**Architecture:** Unit overrides are subtracted from the cohort pool upfront (Option A), then processed alongside programs in the monthly loop. Seasonal multipliers stretch `downtime_days` via `ceil(base * multiplier)`. A `renovation_detail` sidecar passes plat-costmodel metadata through the engine untouched for output formatters.

**Tech Stack:** Python 3, Decimal arithmetic, JSON Schema, pytest

**Spec:** `docs/superpowers/specs/2026-04-19-unit-level-renovation-scheduling-design.md`

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `engine/schemas/deal_schema_v0_1.json` | Add `unit_renovations[]`, `seasonal_downtime_multipliers`, `renovation_detail` |
| Modify | `engine/modules/renovations.py` | Pool subtraction, unit override processing, seasonal downtime |
| Modify | `engine/engine.py:117-275` | Pass `unit_renovations` through `_build_renovation_context()`, pass `renovation_detail` to results |
| Modify | `engine/engine.py:330-350` | Trigger renovation context when `unit_renovations` present (not just `renovation_programs`) |
| Create | `tests/test_unit_renovations.py` | All 12 tests from the spec |

---

### Task 1: Schema — Add `unit_renovation` definition and `seasonal_downtime_multipliers`

**Files:**
- Modify: `engine/schemas/deal_schema_v0_1.json:541-562` (renovation_programs item, add seasonal field)
- Modify: `engine/schemas/deal_schema_v0_1.json:96` (top-level properties, add unit_renovations + renovation_detail)

- [ ] **Step 1: Add `unit_renovation` definition to `$defs`**

In `engine/schemas/deal_schema_v0_1.json`, after the `concession_entry` definition block (around line 604), add:

```json
    "unit_renovation": {
      "type": "object",
      "additionalProperties": false,
      "required": ["unit_id", "cohort_id", "renovation_month", "scope", "cost", "expected_premium"],
      "properties": {
        "unit_id": { "type": "string", "minLength": 1 },
        "cohort_id": { "type": "string", "minLength": 1 },
        "renovation_month": { "$ref": "#/$defs/month" },
        "scope": { "type": "string", "minLength": 1 },
        "cost": { "type": "number", "minimum": 0 },
        "expected_premium": { "type": "number" },
        "downtime_days": { "type": "integer", "minimum": 0 }
      }
    },
```

- [ ] **Step 2: Add `seasonal_downtime_multipliers` to renovation_programs item**

In the `renovation_programs` definition (line ~560), add after `post_renovation_market_rent`:

```json
          "seasonal_downtime_multipliers": {
            "type": "object",
            "description": "Monthly multipliers for downtime_days. Keys are zero-padded month numbers 01-12. Missing months default to 1.0.",
            "additionalProperties": false,
            "patternProperties": {
              "^(0[1-9]|1[0-2])$": { "type": "number", "minimum": 0 }
            }
          }
```

- [ ] **Step 3: Add `unit_renovations` and `renovation_detail` to top-level properties**

After `renovation_programs` (line ~96), add:

```json
    "unit_renovations": {
      "type": "array",
      "description": "Per-unit renovation overrides. Units listed here are subtracted from cohort pools before program scheduling runs.",
      "items": { "$ref": "#/$defs/unit_renovation" }
    },
    "renovation_detail": {
      "type": "object",
      "description": "Sidecar for rich per-unit renovation metadata from plat-costmodel. Engine ignores this; output formatters may render it.",
      "additionalProperties": true
    },
```

- [ ] **Step 4: Validate schema is valid JSON**

Run:
```bash
python3 -c "import json; json.load(open('engine/schemas/deal_schema_v0_1.json')); print('Schema is valid JSON')"
```
Expected: `Schema is valid JSON`

- [ ] **Step 5: Commit**

```bash
git add engine/schemas/deal_schema_v0_1.json
git commit -m "schema: add unit_renovations[], seasonal_downtime_multipliers, renovation_detail"
```

---

### Task 2: Tests — Write failing tests for unit overrides and seasonal downtime

**Files:**
- Create: `tests/test_unit_renovations.py`

- [ ] **Step 1: Write all 12 tests**

Create `tests/test_unit_renovations.py`:

```python
"""
Tests for Unit-Level Renovation Scheduling (Task 1.7)

Tests cover:
- Unit override basic scheduling
- Pool subtraction (overrides reduce cohort pool for programs)
- Mixed mode (unit overrides + programs on same cohort)
- Seasonal downtime multipliers (winter, summer, default)
- Seasonal on unit overrides (inherited and explicit downtime)
- Output cohort assignment
- Rollup accuracy (by_month aggregation)
- Pure unit-override mode (no programs)
- Backward compatibility
"""
import math
import unittest

from engine.modules.renovations import compute_renovations
from engine.modules.time_grid import TimeGrid


class TestUnitOverrideBasic(unittest.TestCase):
    """Test basic unit override functionality."""

    def test_single_unit_override(self):
        """A single unit override should produce correct cost, downtime, and premium timing."""
        time_grid = TimeGrid.build("2026-01", "2026-04")
        unit_cohorts = [
            {"cohort_id": "1B", "unit_type": "1BR", "unit_count": 50, "initial_inplace_rent": 1000}
        ]
        unit_renovations = [
            {
                "unit_id": "301",
                "cohort_id": "1B",
                "renovation_month": "2026-02",
                "scope": "standard_value_add",
                "cost": 14500,
                "expected_premium": 200,
                "downtime_days": 21,
            }
        ]

        result = compute_renovations(
            time_grid, unit_cohorts, renovation_programs=[], unit_renovations=unit_renovations
        )

        # 1 unit renovated total
        self.assertEqual(result["summary"]["total_units_renovated"], 1)
        # Cost = 14500
        self.assertAlmostEqual(result["summary"]["total_renovation_cost"], 14500.0, places=2)
        # Month 1 (Jan): 0 units renovated
        self.assertEqual(result["by_month"][0]["units_renovated"], 0)
        # Month 2 (Feb): 1 unit renovated
        self.assertEqual(result["by_month"][1]["units_renovated"], 1)
        # Month 2: premium = 0 (starts month AFTER renovation)
        self.assertAlmostEqual(result["by_month"][1]["monthly_premium_revenue"], 0.0, places=2)
        # Month 3 (Mar): premium = 200 (1 unit * $200)
        self.assertAlmostEqual(result["by_month"][2]["monthly_premium_revenue"], 200.0, places=2)


class TestPoolSubtraction(unittest.TestCase):
    """Test that unit overrides reduce the cohort pool for programs."""

    def test_override_reduces_pool(self):
        """Program should see fewer available units when overrides claim some."""
        time_grid = TimeGrid.build("2026-01", "2026-03")
        unit_cohorts = [
            {"cohort_id": "1B", "unit_type": "1BR", "unit_count": 10, "initial_inplace_rent": 1000}
        ]
        renovation_programs = [
            {
                "program_id": "upgrade_1b",
                "program_name": "1BR Upgrade",
                "target_cohort": "1B",
                "output_cohort": "1B_reno",
                "renovation_cost_per_unit": 15000,
                "rent_premium_monthly": 200,
                "downtime_days": 30,
                "strategy": "proactive",
                "start_month": "2026-01",
                "monthly_pace": 10,  # Would take all 10 in month 1
            }
        ]
        unit_renovations = [
            {
                "unit_id": "101",
                "cohort_id": "1B",
                "renovation_month": "2026-03",
                "scope": "standard_value_add",
                "cost": 14500,
                "expected_premium": 200,
                "downtime_days": 21,
            },
            {
                "unit_id": "102",
                "cohort_id": "1B",
                "renovation_month": "2026-03",
                "scope": "standard_value_add",
                "cost": 14500,
                "expected_premium": 200,
                "downtime_days": 21,
            },
        ]

        result = compute_renovations(
            time_grid, unit_cohorts, renovation_programs, unit_renovations=unit_renovations
        )

        # Pool starts at 10 - 2 overrides = 8 for program
        # Program takes 8 in month 1, then 0 remaining
        # Overrides add 2 in month 3
        # Total = 8 (program) + 2 (overrides) = 10
        self.assertEqual(result["summary"]["total_units_renovated"], 10)
        # Month 1: program takes 8 (not 10, because 2 are reserved)
        self.assertEqual(result["by_month"][0]["units_renovated"], 8)
        # Month 3: overrides contribute 2
        self.assertEqual(result["by_month"][2]["units_renovated"], 2)


class TestMixedMode(unittest.TestCase):
    """Test mixed mode: unit overrides + programs on same cohort."""

    def test_mixed_mode_totals(self):
        """3 unit overrides + program should produce correct totals."""
        time_grid = TimeGrid.build("2026-01", "2026-06")
        unit_cohorts = [
            {"cohort_id": "1B", "unit_type": "1BR", "unit_count": 50, "initial_inplace_rent": 1000}
        ]
        renovation_programs = [
            {
                "program_id": "upgrade_1b",
                "program_name": "1BR Upgrade",
                "target_cohort": "1B",
                "output_cohort": "1B_reno",
                "renovation_cost_per_unit": 15000,
                "rent_premium_monthly": 200,
                "downtime_days": 30,
                "strategy": "proactive",
                "start_month": "2026-01",
                "monthly_pace": 5,
            }
        ]
        unit_renovations = [
            {
                "unit_id": "201",
                "cohort_id": "1B",
                "renovation_month": "2026-02",
                "scope": "light",
                "cost": 5000,
                "expected_premium": 100,
                "downtime_days": 14,
            },
            {
                "unit_id": "202",
                "cohort_id": "1B",
                "renovation_month": "2026-03",
                "scope": "standard_value_add",
                "cost": 14500,
                "expected_premium": 200,
                "downtime_days": 21,
            },
            {
                "unit_id": "203",
                "cohort_id": "1B",
                "renovation_month": "2026-04",
                "scope": "standard_value_add",
                "cost": 14500,
                "expected_premium": 200,
                "downtime_days": 21,
            },
        ]

        result = compute_renovations(
            time_grid, unit_cohorts, renovation_programs, unit_renovations=unit_renovations
        )

        # Pool = 50 - 3 = 47 for program. Program does 5/month for 6 months = 30 (capped at 47).
        # Actually 5/month * 6 = 30, and 30 < 47, so program does 30.
        # Total = 30 (program) + 3 (overrides) = 33
        self.assertEqual(result["summary"]["total_units_renovated"], 33)

        # Total cost = 30 * 15000 + 5000 + 14500 + 14500 = 450000 + 34000 = 484000
        self.assertAlmostEqual(result["summary"]["total_renovation_cost"], 484000.0, places=2)


class TestSeasonalDowntime(unittest.TestCase):
    """Test seasonal downtime multipliers."""

    def _make_program_with_seasonal(self):
        return {
            "program_id": "upgrade_1b",
            "program_name": "1BR Upgrade",
            "target_cohort": "1B",
            "output_cohort": "1B_reno",
            "renovation_cost_per_unit": 15000,
            "rent_premium_monthly": 200,
            "downtime_days": 21,
            "strategy": "proactive",
            "start_month": "2026-01",
            "monthly_pace": 5,
            "seasonal_downtime_multipliers": {
                "01": 1.3, "02": 1.3,
                "05": 0.8, "06": 0.8, "07": 0.8, "08": 0.8,
                "11": 1.3, "12": 1.3,
            },
        }

    def test_winter_downtime(self):
        """January renovation should get ceil(21 * 1.3) = 28 days downtime."""
        time_grid = TimeGrid.build("2026-01", "2026-01")
        unit_cohorts = [
            {"cohort_id": "1B", "unit_type": "1BR", "unit_count": 50, "initial_inplace_rent": 1000}
        ]
        program = self._make_program_with_seasonal()

        result = compute_renovations(time_grid, unit_cohorts, [program])

        # 5 units * ceil(21 * 1.3) = 5 * 28 = 140 downtime days
        self.assertEqual(result["by_month"][0]["downtime_vacancy_days"], 140)

    def test_summer_downtime(self):
        """June renovation should get ceil(21 * 0.8) = 17 days downtime."""
        time_grid = TimeGrid.build("2026-06", "2026-06")
        unit_cohorts = [
            {"cohort_id": "1B", "unit_type": "1BR", "unit_count": 50, "initial_inplace_rent": 1000}
        ]
        program = self._make_program_with_seasonal()

        result = compute_renovations(time_grid, unit_cohorts, [program])

        # 5 units * ceil(21 * 0.8) = 5 * 17 = 85 downtime days
        self.assertEqual(result["by_month"][0]["downtime_vacancy_days"], 85)

    def test_default_month_no_multiplier(self):
        """March (not in seasonal map) should use 1.0x = 21 days unchanged."""
        time_grid = TimeGrid.build("2026-03", "2026-03")
        unit_cohorts = [
            {"cohort_id": "1B", "unit_type": "1BR", "unit_count": 50, "initial_inplace_rent": 1000}
        ]
        program = self._make_program_with_seasonal()

        result = compute_renovations(time_grid, unit_cohorts, [program])

        # 5 units * 21 days = 105 downtime days (no seasonal adjustment)
        self.assertEqual(result["by_month"][0]["downtime_vacancy_days"], 105)


class TestSeasonalOnUnitOverrides(unittest.TestCase):
    """Test seasonal downtime applied to unit overrides."""

    def test_override_inherits_seasonal(self):
        """Unit override without explicit downtime inherits program base * seasonal."""
        time_grid = TimeGrid.build("2026-01", "2026-02")
        unit_cohorts = [
            {"cohort_id": "1B", "unit_type": "1BR", "unit_count": 50, "initial_inplace_rent": 1000}
        ]
        program = {
            "program_id": "upgrade_1b",
            "program_name": "1BR Upgrade",
            "target_cohort": "1B",
            "output_cohort": "1B_reno",
            "renovation_cost_per_unit": 15000,
            "rent_premium_monthly": 200,
            "downtime_days": 21,
            "strategy": "proactive",
            "start_month": "2026-01",
            "monthly_pace": 0,  # No program activity — only overrides
            "seasonal_downtime_multipliers": {
                "01": 1.3,
            },
        }
        unit_renovations = [
            {
                "unit_id": "301",
                "cohort_id": "1B",
                "renovation_month": "2026-01",
                "scope": "standard_value_add",
                "cost": 14500,
                "expected_premium": 200,
                # No downtime_days — inherits from program (21) * seasonal (1.3) = ceil(27.3) = 28
            }
        ]

        result = compute_renovations(
            time_grid, unit_cohorts, [program], unit_renovations=unit_renovations
        )

        # 1 unit * 28 days = 28 downtime days
        self.assertEqual(result["by_month"][0]["downtime_vacancy_days"], 28)

    def test_override_explicit_downtime_still_seasonal(self):
        """Override with explicit downtime_days uses that as base, still applies seasonal."""
        time_grid = TimeGrid.build("2026-01", "2026-02")
        unit_cohorts = [
            {"cohort_id": "1B", "unit_type": "1BR", "unit_count": 50, "initial_inplace_rent": 1000}
        ]
        program = {
            "program_id": "upgrade_1b",
            "program_name": "1BR Upgrade",
            "target_cohort": "1B",
            "output_cohort": "1B_reno",
            "renovation_cost_per_unit": 15000,
            "rent_premium_monthly": 200,
            "downtime_days": 21,
            "strategy": "proactive",
            "start_month": "2026-01",
            "monthly_pace": 0,
            "seasonal_downtime_multipliers": {
                "01": 1.3,
            },
        }
        unit_renovations = [
            {
                "unit_id": "301",
                "cohort_id": "1B",
                "renovation_month": "2026-01",
                "scope": "light",
                "cost": 5000,
                "expected_premium": 100,
                "downtime_days": 14,  # Explicit — base is 14, not program's 21
            }
        ]

        result = compute_renovations(
            time_grid, unit_cohorts, [program], unit_renovations=unit_renovations
        )

        # 1 unit * ceil(14 * 1.3) = ceil(18.2) = 19 downtime days
        self.assertEqual(result["by_month"][0]["downtime_vacancy_days"], 19)


class TestOutputCohortAssignment(unittest.TestCase):
    """Test output cohort for unit overrides."""

    def test_override_uses_program_output_cohort(self):
        """Unit override should use the matching program's output_cohort in by_program_by_month."""
        time_grid = TimeGrid.build("2026-01", "2026-02")
        unit_cohorts = [
            {"cohort_id": "1B", "unit_type": "1BR", "unit_count": 50, "initial_inplace_rent": 1000}
        ]
        renovation_programs = [
            {
                "program_id": "upgrade_1b",
                "program_name": "1BR Upgrade",
                "target_cohort": "1B",
                "output_cohort": "1B_reno",
                "renovation_cost_per_unit": 15000,
                "rent_premium_monthly": 200,
                "downtime_days": 30,
                "strategy": "proactive",
                "start_month": "2026-01",
                "monthly_pace": 0,
            }
        ]
        unit_renovations = [
            {
                "unit_id": "301",
                "cohort_id": "1B",
                "renovation_month": "2026-01",
                "scope": "standard_value_add",
                "cost": 14500,
                "expected_premium": 200,
                "downtime_days": 21,
            }
        ]

        result = compute_renovations(
            time_grid, unit_cohorts, renovation_programs, unit_renovations=unit_renovations
        )

        # The unit override should appear in by_program_by_month with program_id "__unit_override__"
        override_rows = [
            r for r in result["by_program_by_month"] if r["program_id"] == "__unit_override__"
        ]
        self.assertTrue(len(override_rows) > 0)
        # And the override should have renovated 1 unit in month 2026-01
        jan_row = [r for r in override_rows if r["month"] == "2026-01"][0]
        self.assertEqual(jan_row["units_renovated"], 1)


class TestRollupAccuracy(unittest.TestCase):
    """Test by_month aggregation includes both programs and overrides."""

    def test_by_month_includes_both(self):
        """by_month totals should sum program + override activity."""
        time_grid = TimeGrid.build("2026-01", "2026-03")
        unit_cohorts = [
            {"cohort_id": "1B", "unit_type": "1BR", "unit_count": 50, "initial_inplace_rent": 1000}
        ]
        renovation_programs = [
            {
                "program_id": "upgrade_1b",
                "program_name": "1BR Upgrade",
                "target_cohort": "1B",
                "output_cohort": "1B_reno",
                "renovation_cost_per_unit": 10000,
                "rent_premium_monthly": 150,
                "downtime_days": 30,
                "strategy": "proactive",
                "start_month": "2026-01",
                "monthly_pace": 3,
            }
        ]
        unit_renovations = [
            {
                "unit_id": "301",
                "cohort_id": "1B",
                "renovation_month": "2026-02",
                "scope": "standard_value_add",
                "cost": 14500,
                "expected_premium": 200,
                "downtime_days": 21,
            }
        ]

        result = compute_renovations(
            time_grid, unit_cohorts, renovation_programs, unit_renovations=unit_renovations
        )

        # Month 1: 3 from program
        self.assertEqual(result["by_month"][0]["units_renovated"], 3)
        # Month 2: 3 from program + 1 override = 4
        self.assertEqual(result["by_month"][1]["units_renovated"], 4)
        # Month 3: 3 from program
        self.assertEqual(result["by_month"][2]["units_renovated"], 3)
        # Total cost: 9 * 10000 + 14500 = 104500
        self.assertAlmostEqual(result["summary"]["total_renovation_cost"], 104500.0, places=2)


class TestPureOverrideMode(unittest.TestCase):
    """Test unit overrides with no programs."""

    def test_no_programs_only_overrides(self):
        """Should work with no renovation_programs, only unit_renovations."""
        time_grid = TimeGrid.build("2026-01", "2026-04")
        unit_cohorts = [
            {"cohort_id": "1B", "unit_type": "1BR", "unit_count": 50, "initial_inplace_rent": 1000}
        ]
        unit_renovations = [
            {
                "unit_id": "101",
                "cohort_id": "1B",
                "renovation_month": "2026-01",
                "scope": "light",
                "cost": 5000,
                "expected_premium": 100,
                "downtime_days": 14,
            },
            {
                "unit_id": "102",
                "cohort_id": "1B",
                "renovation_month": "2026-02",
                "scope": "standard_value_add",
                "cost": 14500,
                "expected_premium": 200,
                "downtime_days": 21,
            },
        ]

        result = compute_renovations(
            time_grid, unit_cohorts, renovation_programs=[], unit_renovations=unit_renovations
        )

        self.assertEqual(result["summary"]["total_units_renovated"], 2)
        self.assertAlmostEqual(result["summary"]["total_renovation_cost"], 19500.0, places=2)
        # Month 1: 1 unit
        self.assertEqual(result["by_month"][0]["units_renovated"], 1)
        # Month 2: 1 unit
        self.assertEqual(result["by_month"][1]["units_renovated"], 1)
        # Month 3: premium from unit 101 ($100) + unit 102 ($200) = $300
        self.assertAlmostEqual(result["by_month"][2]["monthly_premium_revenue"], 300.0, places=2)


class TestBackwardCompat(unittest.TestCase):
    """Test backward compatibility."""

    def test_no_unit_renovations_unchanged(self):
        """Omitting unit_renovations should produce identical output to current behavior."""
        time_grid = TimeGrid.build("2026-01", "2026-03")
        unit_cohorts = [
            {"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}
        ]
        renovation_programs = [
            {
                "program_id": "upgrade_1b",
                "program_name": "1BR Upgrade",
                "target_cohort": "1B",
                "output_cohort": "1B_reno",
                "renovation_cost_per_unit": 15000,
                "rent_premium_monthly": 200,
                "downtime_days": 30,
                "strategy": "proactive",
                "start_month": "2026-01",
                "monthly_pace": 5,
            }
        ]

        # Call without unit_renovations (default)
        result_without = compute_renovations(time_grid, unit_cohorts, renovation_programs)
        # Call with empty unit_renovations
        result_with = compute_renovations(
            time_grid, unit_cohorts, renovation_programs, unit_renovations=[]
        )

        self.assertEqual(result_without["summary"], result_with["summary"])
        self.assertEqual(len(result_without["by_month"]), len(result_with["by_month"]))
        for a, b in zip(result_without["by_month"], result_with["by_month"]):
            self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
python3 -m pytest tests/test_unit_renovations.py -v --tb=short 2>&1 | tail -20
```
Expected: All 12 tests FAIL with `TypeError: compute_renovations() got an unexpected keyword argument 'unit_renovations'`

- [ ] **Step 3: Commit**

```bash
git add tests/test_unit_renovations.py
git commit -m "test: add 12 failing tests for unit-level renovation scheduling"
```

---

### Task 3: Implement seasonal downtime multiplier helper

**Files:**
- Modify: `engine/modules/renovations.py:1-17` (imports)
- Modify: `engine/modules/renovations.py:49-55` (add helper after `_month_in_range`)

- [ ] **Step 1: Add `math` import**

At the top of `engine/modules/renovations.py`, add `import math` after the existing imports (line 9, after `from __future__ import annotations`):

```python
import math
```

- [ ] **Step 2: Add `_apply_seasonal_downtime` helper**

After `_month_in_range` (line 55), add:

```python
def _get_seasonal_multiplier(
    month: str,
    seasonal_multipliers: Optional[Dict[str, float]],
) -> float:
    """Get seasonal downtime multiplier for a given month.

    Args:
        month: Month ID like "2026-01"
        seasonal_multipliers: Map of "01"-"12" to multiplier, or None

    Returns:
        Multiplier (default 1.0 if no map or month not in map)
    """
    if not seasonal_multipliers:
        return 1.0
    cal_month = month[5:7]  # "2026-01" -> "01"
    return seasonal_multipliers.get(cal_month, 1.0)


def _apply_seasonal_downtime(
    base_downtime_days: int,
    month: str,
    seasonal_multipliers: Optional[Dict[str, float]],
) -> int:
    """Apply seasonal multiplier to base downtime days.

    Returns ceil(base * multiplier) so partial days round up.
    """
    multiplier = _get_seasonal_multiplier(month, seasonal_multipliers)
    return math.ceil(base_downtime_days * multiplier)
```

- [ ] **Step 3: Verify existing tests still pass**

Run:
```bash
python3 -m pytest tests/test_renovations.py -v --tb=short
```
Expected: All 16 tests PASS (new helpers are unused so far)

- [ ] **Step 4: Commit**

```bash
git add engine/modules/renovations.py
git commit -m "feat: add seasonal downtime multiplier helpers to renovations module"
```

---

### Task 4: Implement unit override processing + seasonal downtime in `compute_renovations()`

**Files:**
- Modify: `engine/modules/renovations.py:58-339` (the `compute_renovations` function)

- [ ] **Step 1: Add `unit_renovations` parameter to function signature**

Change the signature (line 58-63) from:

```python
def compute_renovations(
    time_grid: TimeGrid,
    unit_cohorts: List[Dict[str, Any]],
    renovation_programs: List[Dict[str, Any]],
    turnover_rate: float = 0.50,
) -> Dict[str, Any]:
```

To:

```python
def compute_renovations(
    time_grid: TimeGrid,
    unit_cohorts: List[Dict[str, Any]],
    renovation_programs: List[Dict[str, Any]],
    turnover_rate: float = 0.50,
    unit_renovations: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
```

- [ ] **Step 2: Update early return for empty state**

Change the early return guard (line 76) from:

```python
    if not renovation_programs:
```

To:

```python
    if not renovation_programs and not unit_renovations:
```

- [ ] **Step 3: Initialize unit_renovations default and build lookup structures**

After the early return block (after line 103), before `monthly_turnover_rate`, add:

```python
    if unit_renovations is None:
        unit_renovations = []

    # Build a lookup: cohort_id -> program with seasonal_downtime_multipliers
    # Used to find seasonal multipliers for unit overrides
    cohort_to_program: Dict[str, Dict[str, Any]] = {}
    for program in renovation_programs:
        tc = program["target_cohort"]
        if tc not in cohort_to_program:
            cohort_to_program[tc] = program

    # Index unit overrides by renovation_month for efficient lookup
    overrides_by_month: Dict[str, List[Dict[str, Any]]] = {}
    for ur in unit_renovations:
        m = month_id(ur["renovation_month"])
        if m not in overrides_by_month:
            overrides_by_month[m] = []
        overrides_by_month[m].append(ur)
```

- [ ] **Step 4: Subtract override units from cohort pool**

After the cohort_pool initialization loop (after line 113), add:

```python
    # Subtract unit overrides from cohort pool upfront (Option A)
    for ur in unit_renovations:
        cid = ur["cohort_id"]
        if cid not in cohort_pool:
            cohort_pool[cid] = _get_cohort_units(unit_cohorts, cid)
        cohort_pool[cid] = max(0, cohort_pool[cid] - 1)
```

- [ ] **Step 5: Apply seasonal downtime to program-scheduled units**

In the program processing loop, replace the downtime calculation (line 178-179):

```python
            downtime_days = int(program["downtime_days"])
            total_downtime_days = downtime_days * units_this_month
```

With:

```python
            base_downtime = int(program["downtime_days"])
            seasonal_multipliers = program.get("seasonal_downtime_multipliers")
            effective_downtime = _apply_seasonal_downtime(base_downtime, month, seasonal_multipliers)
            total_downtime_days = effective_downtime * units_this_month
```

- [ ] **Step 6: Process unit overrides in the monthly loop**

After the program loop within the month loop (after the `results.append(...)` for programs, around line 207), add:

```python
        # Process unit overrides scheduled for this month
        for ur in overrides_by_month.get(month, []):
            # Determine base downtime
            if "downtime_days" in ur and ur["downtime_days"] is not None:
                base_dt = int(ur["downtime_days"])
            else:
                # Inherit from matching program
                matching_prog = cohort_to_program.get(ur["cohort_id"])
                base_dt = int(matching_prog["downtime_days"]) if matching_prog else 21

            # Apply seasonal multiplier from matching program
            matching_prog = cohort_to_program.get(ur["cohort_id"])
            seasonal_mults = matching_prog.get("seasonal_downtime_multipliers") if matching_prog else None
            effective_dt = _apply_seasonal_downtime(base_dt, month, seasonal_mults)

            # Vacancy loss based on cohort rent
            daily_rent = _get_cohort_rent(unit_cohorts, ur["cohort_id"]) / Decimal("30")
            vacancy_loss = daily_rent * effective_dt

            results.append(
                RenovationMonthResult(
                    month=month,
                    program_id="__unit_override__",
                    units_renovated=1,
                    renovation_cost=dec(ur["cost"]),
                    downtime_vacancy_days=effective_dt,
                    downtime_vacancy_loss=vacancy_loss,
                    cumulative_units_renovated=0,  # Tracked separately below
                    monthly_premium_revenue=Decimal("0"),  # Recalculated in aggregation
                )
            )
```

- [ ] **Step 7: Update premium aggregation to include unit overrides**

In the by_month aggregation loop (around line 223), after the program premium calculation, add unit override premium calculation. Replace the entire premium block:

```python
        # Premium calculation: sum premiums from all programs
        # Need to recalculate based on cumulative units at start of month
        month_premium = Decimal("0")
        for program in renovation_programs:
            pid = program["program_id"]
            rent_premium = dec(program["rent_premium_monthly"])
            # Find cumulative for this program at end of prior month
            prior_months = [r for r in results if r.program_id == pid and r.month < month]
            if prior_months:
                prior_cumulative = prior_months[-1].cumulative_units_renovated
            else:
                prior_cumulative = 0
            month_premium += rent_premium * prior_cumulative
```

With:

```python
        # Premium calculation: sum premiums from all programs
        month_premium = Decimal("0")
        for program in renovation_programs:
            pid = program["program_id"]
            rent_premium = dec(program["rent_premium_monthly"])
            prior_months = [r for r in results if r.program_id == pid and r.month < month]
            if prior_months:
                prior_cumulative = prior_months[-1].cumulative_units_renovated
            else:
                prior_cumulative = 0
            month_premium += rent_premium * prior_cumulative

        # Add premium from unit overrides completed in prior months
        for ur in unit_renovations:
            ur_month = month_id(ur["renovation_month"])
            if ur_month < month:
                month_premium += dec(ur["expected_premium"])
```

- [ ] **Step 8: Update summary to include unit override totals**

In the summary section (around line 288), the `by_program` aggregation only covers programs. The summary needs to also count unit overrides. After the `by_program` loop, before the summary calculation, add:

```python
    # Add unit override summary data to totals
    override_results = [r for r in results if r.program_id == "__unit_override__"]
    override_units = sum(r.units_renovated for r in override_results)
    override_cost = sum((r.renovation_cost for r in override_results), Decimal("0"))
    override_vacancy = sum((r.downtime_vacancy_loss for r in override_results), Decimal("0"))
    override_annual_premium = sum(
        (dec(ur["expected_premium"]) * 12 for ur in unit_renovations), Decimal("0")
    )
```

Then update the summary calculations to add override values. Replace:

```python
    total_units_renovated = sum(p["total_units_renovated"] for p in by_program)
    total_renovation_cost = sum((dec(p["total_renovation_cost"]) for p in by_program), Decimal("0"))
    total_vacancy_loss = sum(
        (dec(p["total_downtime_vacancy_loss"]) for p in by_program), Decimal("0")
    )
    net_renovation_cost = total_renovation_cost + total_vacancy_loss
    total_annual_premium = sum(
        (dec(p["annual_premium_at_completion"]) for p in by_program), Decimal("0")
    )
```

With:

```python
    total_units_renovated = sum(p["total_units_renovated"] for p in by_program) + override_units
    total_renovation_cost = sum((dec(p["total_renovation_cost"]) for p in by_program), Decimal("0")) + override_cost
    total_vacancy_loss = sum(
        (dec(p["total_downtime_vacancy_loss"]) for p in by_program), Decimal("0")
    ) + override_vacancy
    net_renovation_cost = total_renovation_cost + total_vacancy_loss
    total_annual_premium = sum(
        (dec(p["annual_premium_at_completion"]) for p in by_program), Decimal("0")
    ) + override_annual_premium
```

- [ ] **Step 9: Include unit override rows in cumulative counts for by_month**

Update the `total_cumulative` calculation in the by_month aggregation. The current line:

```python
        total_cumulative = sum(r.cumulative_units_renovated for r in month_results)
```

Needs to also count cumulative unit overrides. Replace with:

```python
        # Cumulative from programs
        total_cumulative = sum(
            r.cumulative_units_renovated for r in month_results if r.program_id != "__unit_override__"
        )
        # Add cumulative unit overrides (all overrides scheduled up to and including this month)
        total_cumulative += sum(
            1 for ur in unit_renovations if month_id(ur["renovation_month"]) <= month
        )
```

- [ ] **Step 10: Run all tests**

Run:
```bash
python3 -m pytest tests/test_unit_renovations.py tests/test_renovations.py -v --tb=short
```
Expected: All 28 tests PASS (16 existing + 12 new)

- [ ] **Step 11: Commit**

```bash
git add engine/modules/renovations.py
git commit -m "feat: implement unit-level renovation overrides and seasonal downtime multipliers"
```

---

### Task 5: Wire unit_renovations through engine.py

**Files:**
- Modify: `engine/engine.py:117-133` (`_build_renovation_context` signature and call)
- Modify: `engine/engine.py:330-350` (trigger condition and passthrough)

- [ ] **Step 1: Update `_build_renovation_context` to accept and pass `unit_renovations`**

Change the function signature (line 117-120) from:

```python
def _build_renovation_context(
    time_grid: TimeGrid,
    inputs: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Dict[str, int]], List[Dict[str, Any]], List[Dict], List[Dict], List[Dict]]:
```

To:

```python
def _build_renovation_context(
    time_grid: TimeGrid,
    inputs: Dict[str, Any],
    unit_renovations: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Dict[str, int]], List[Dict[str, Any]], List[Dict], List[Dict], List[Dict]]:
```

Then update the `compute_renovations` call inside it (line 129-133) from:

```python
    renovation_result = compute_renovations(
        time_grid=time_grid,
        unit_cohorts=inputs["unit_cohorts"],
        renovation_programs=inputs["renovation_programs"],
    )
```

To:

```python
    renovation_result = compute_renovations(
        time_grid=time_grid,
        unit_cohorts=inputs["unit_cohorts"],
        renovation_programs=inputs.get("renovation_programs", []),
        unit_renovations=unit_renovations or [],
    )
```

- [ ] **Step 2: Add unit override dynamic unit count tracking**

In `_build_renovation_context`, after the output cohort processing loop (after line 266, before the return), add:

```python
    # Handle unit overrides: adjust dynamic unit counts
    if unit_renovations:
        for ur in unit_renovations:
            tc = ur["cohort_id"]
            ur_month = month_id(ur["renovation_month"])

            # Find or synthesize output cohort
            matching_prog = next(
                (p for p in inputs.get("renovation_programs", []) if p["target_cohort"] == tc),
                None,
            )
            oc = matching_prog["output_cohort"] if matching_prog else f"{tc}_reno"

            # Ensure output cohort exists in dynamic counts
            if oc not in dynamic_unit_counts:
                dynamic_unit_counts[oc] = {m: 0 for m in months}

                # Create synthetic output cohort entry if not already created
                if oc not in created_output_cohorts:
                    created_output_cohorts.add(oc)
                    target_cohort_data = next(
                        (c for c in inputs["unit_cohorts"] if c["cohort_id"] == tc), None
                    )
                    if target_cohort_data:
                        output_cohort_entries.append({
                            "cohort_id": oc,
                            "unit_type": target_cohort_data.get("unit_type", tc) + "_reno",
                            "unit_count": 0,
                            "initial_inplace_rent": round(
                                float(dec(target_cohort_data["initial_inplace_rent"]) + dec(ur["expected_premium"])),
                                2,
                            ),
                            **({"sqft": target_cohort_data["sqft"]} if "sqft" in target_cohort_data else {}),
                        })

                        # Copy curves from target cohort
                        for seg in [r for r in inputs["market_rent_curve"] if r["cohort_id"] == tc]:
                            output_market_rent_curve.append({**seg, "cohort_id": oc})
                        for seg in [r for r in inputs["loss_to_lease"] if r["cohort_id"] == tc]:
                            output_loss_to_lease.append({**seg, "cohort_id": oc})
                        for seg in [r for r in inputs["physical_vacancy_curve"] if r["cohort_id"] == tc]:
                            output_vacancy_curve.append({**seg, "cohort_id": oc})

            # Adjust dynamic counts: -1 from target, +1 to output, starting at renovation month
            for m in months:
                if m >= ur_month:
                    dynamic_unit_counts[tc][m] = max(0, dynamic_unit_counts[tc][m] - 1)
                    dynamic_unit_counts[oc][m] = dynamic_unit_counts[oc].get(m, 0) + 1
```

- [ ] **Step 3: Update the trigger condition in `run_deal()`**

Change line 338 from:

```python
    if inputs.get("renovation_programs"):
```

To:

```python
    unit_renovations = inputs.get("unit_renovations", [])
    if inputs.get("renovation_programs") or unit_renovations:
```

And update the call (line 346) from:

```python
        ) = _build_renovation_context(time_grid, inputs)
```

To:

```python
        ) = _build_renovation_context(time_grid, inputs, unit_renovations=unit_renovations)
```

- [ ] **Step 4: Pass `renovation_detail` sidecar through to results**

Find where the results dict is assembled (search for `"renovations":` in the return statement of `run_deal()`). After the renovations entry, add `renovation_detail`:

```python
        "renovation_detail": inputs.get("renovation_detail", {}),
```

- [ ] **Step 5: Run full test suite**

Run:
```bash
python3 -m pytest tests/test_unit_renovations.py tests/test_renovations.py -v --tb=short
```
Expected: All 28 tests PASS

- [ ] **Step 6: Commit**

```bash
git add engine/engine.py
git commit -m "feat: wire unit_renovations and renovation_detail through engine pipeline"
```

---

### Task 6: Final validation — run full test suite

**Files:** None (validation only)

- [ ] **Step 1: Run full test suite**

Run:
```bash
python3 -m pytest tests/ -v --tb=short 2>&1 | tail -30
```
Expected: All tests pass (existing 475+ plus 12 new = 487+)

- [ ] **Step 2: Verify schema validates a deal with unit_renovations**

Run:
```bash
python3 -c "
from engine.validator import validate_or_raise
import json

# Load an existing deal and add unit_renovations
with open('runs/examples/deal_001_run_001/inputs.json') as f:
    inputs = json.load(f)

inputs['unit_renovations'] = [
    {
        'unit_id': '101',
        'cohort_id': inputs['unit_cohorts'][0]['cohort_id'],
        'renovation_month': '2026-06',
        'scope': 'standard_value_add',
        'cost': 14500,
        'expected_premium': 200,
        'downtime_days': 21,
    }
]
inputs['renovation_detail'] = {
    '101': {'scope_level': 'standard_value_add', 'finish_tier': 'upgraded'}
}

try:
    validate_or_raise(inputs)
    print('Schema validation PASSED')
except Exception as e:
    print(f'Schema validation FAILED: {e}')
"
```
Expected: `Schema validation PASSED`

- [ ] **Step 3: Update session handoff**

Update `docs/session_handoff.md`: move Task 1.7 to Completed, update Next Up to Task 2.2.

- [ ] **Step 4: Commit**

```bash
git add docs/session_handoff.md
git commit -m "docs: mark Task 1.7 complete in session handoff"
```
