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
                "monthly_pace": 10,
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

        # Pool = 50 - 3 = 47 for program. Program does 5/month for 6 months = 30.
        # Total = 30 (program) + 3 (overrides) = 33
        self.assertEqual(result["summary"]["total_units_renovated"], 33)
        # Total cost = 30 * 15000 + 5000 + 14500 + 14500 = 484000
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
        result = compute_renovations(time_grid, unit_cohorts, [self._make_program_with_seasonal()])
        # 5 units * ceil(21 * 1.3) = 5 * 28 = 140 downtime days
        self.assertEqual(result["by_month"][0]["downtime_vacancy_days"], 140)

    def test_summer_downtime(self):
        """June renovation should get ceil(21 * 0.8) = 17 days downtime."""
        time_grid = TimeGrid.build("2026-06", "2026-06")
        unit_cohorts = [
            {"cohort_id": "1B", "unit_type": "1BR", "unit_count": 50, "initial_inplace_rent": 1000}
        ]
        result = compute_renovations(time_grid, unit_cohorts, [self._make_program_with_seasonal()])
        # 5 units * ceil(21 * 0.8) = 5 * 17 = 85 downtime days
        self.assertEqual(result["by_month"][0]["downtime_vacancy_days"], 85)

    def test_default_month_no_multiplier(self):
        """March (not in seasonal map) should use 1.0x = 21 days unchanged."""
        time_grid = TimeGrid.build("2026-03", "2026-03")
        unit_cohorts = [
            {"cohort_id": "1B", "unit_type": "1BR", "unit_count": 50, "initial_inplace_rent": 1000}
        ]
        result = compute_renovations(time_grid, unit_cohorts, [self._make_program_with_seasonal()])
        # 5 units * 21 days = 105 downtime days
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
            "monthly_pace": 0,
            "seasonal_downtime_multipliers": {"01": 1.3},
        }
        unit_renovations = [
            {
                "unit_id": "301",
                "cohort_id": "1B",
                "renovation_month": "2026-01",
                "scope": "standard_value_add",
                "cost": 14500,
                "expected_premium": 200,
            }
        ]

        result = compute_renovations(
            time_grid, unit_cohorts, [program], unit_renovations=unit_renovations
        )
        # 1 unit * ceil(21 * 1.3) = 28 downtime days
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
            "seasonal_downtime_multipliers": {"01": 1.3},
        }
        unit_renovations = [
            {
                "unit_id": "301",
                "cohort_id": "1B",
                "renovation_month": "2026-01",
                "scope": "light",
                "cost": 5000,
                "expected_premium": 100,
                "downtime_days": 14,
            }
        ]

        result = compute_renovations(
            time_grid, unit_cohorts, [program], unit_renovations=unit_renovations
        )
        # 1 unit * ceil(14 * 1.3) = ceil(18.2) = 19 downtime days
        self.assertEqual(result["by_month"][0]["downtime_vacancy_days"], 19)


class TestOutputCohortAssignment(unittest.TestCase):
    """Test output cohort for unit overrides."""

    def test_override_appears_in_by_program_by_month(self):
        """Unit override should appear in by_program_by_month with __unit_override__ program_id."""
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

        override_rows = [
            r for r in result["by_program_by_month"] if r["program_id"] == "__unit_override__"
        ]
        self.assertTrue(len(override_rows) > 0)
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
        self.assertEqual(result["by_month"][0]["units_renovated"], 1)
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

        result_without = compute_renovations(time_grid, unit_cohorts, renovation_programs)
        result_with = compute_renovations(
            time_grid, unit_cohorts, renovation_programs, unit_renovations=[]
        )

        self.assertEqual(result_without["summary"], result_with["summary"])
        for a, b in zip(result_without["by_month"], result_with["by_month"]):
            self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
