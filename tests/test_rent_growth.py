"""
Tests for Rent Growth Module

Tests cover:
- Annual compound growth
- Monthly compound growth
- Step increases
- LTL decay (annual step, monthly linear)
- Renovation premium integration
- Multi-cohort scenarios
- Edge cases
"""
import unittest

from engine.modules.rent_growth import generate_rent_curves
from engine.modules.time_grid import TimeGrid


class TestRentGrowthAnnualCompound(unittest.TestCase):
    """Test annual compound growth calculation."""

    def test_no_growth_first_year(self):
        """First year should have no growth applied."""
        time_grid = TimeGrid.build("2026-01", "2026-12")
        unit_cohorts = [
            {"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}
        ]
        growth_assumptions = {
            "growth_type": "annual_compound",
            "annual_growth_rate": 0.03,
        }

        result = generate_rent_curves(time_grid, unit_cohorts, growth_assumptions)

        # Year 1 should have base rent (no growth in first year)
        jan = next(r for r in result["by_month"] if r["month"] == "2026-01")
        dec = next(r for r in result["by_month"] if r["month"] == "2026-12")
        self.assertAlmostEqual(jan["market_rent"], 1000.0, places=2)
        self.assertAlmostEqual(dec["market_rent"], 1000.0, places=2)

    def test_growth_applies_year_two(self):
        """Growth should apply starting in year two."""
        time_grid = TimeGrid.build("2026-01", "2027-12")
        unit_cohorts = [
            {"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}
        ]
        growth_assumptions = {
            "growth_type": "annual_compound",
            "annual_growth_rate": 0.03,
        }

        result = generate_rent_curves(time_grid, unit_cohorts, growth_assumptions)

        # Year 2 should have 3% growth
        jan_y2 = next(r for r in result["by_month"] if r["month"] == "2027-01")
        self.assertAlmostEqual(jan_y2["market_rent"], 1030.0, places=2)

    def test_multi_year_compound_growth(self):
        """Growth should compound across years."""
        time_grid = TimeGrid.build("2026-01", "2028-12")
        unit_cohorts = [
            {"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}
        ]
        growth_assumptions = {
            "growth_type": "annual_compound",
            "annual_growth_rate": 0.05,
        }

        result = generate_rent_curves(time_grid, unit_cohorts, growth_assumptions)

        # Year 3: 1000 * 1.05^2 = 1102.50
        jan_y3 = next(r for r in result["by_month"] if r["month"] == "2028-01")
        self.assertAlmostEqual(jan_y3["market_rent"], 1102.50, places=2)


class TestRentGrowthMonthlyCompound(unittest.TestCase):
    """Test monthly compound growth calculation."""

    def test_monthly_compound_applies_each_month(self):
        """Monthly compound should apply each month after start."""
        time_grid = TimeGrid.build("2026-01", "2026-12")
        unit_cohorts = [
            {"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}
        ]
        growth_assumptions = {
            "growth_type": "monthly_compound",
            "annual_growth_rate": 0.12,  # ~1% per month
            "growth_start_month": "2026-01",
        }

        result = generate_rent_curves(time_grid, unit_cohorts, growth_assumptions)

        # After 12 months with 12% annual compounded monthly, should be ~$1120
        dec = next(r for r in result["by_month"] if r["month"] == "2026-12")
        # (1 + 0.12)^(11/12) ≈ 1.11
        self.assertGreater(dec["market_rent"], 1100.0)
        self.assertLess(dec["market_rent"], 1130.0)


class TestRentGrowthStepIncreases(unittest.TestCase):
    """Test step increase functionality."""

    def test_step_increase_applies(self):
        """Step increase should apply in effective month."""
        time_grid = TimeGrid.build("2026-01", "2026-12")
        unit_cohorts = [
            {"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}
        ]
        growth_assumptions = {
            "growth_type": "step_only",
            "annual_growth_rate": 0.0,
            "step_increases": [
                {"effective_month": "2026-06", "increase_percent": 0.05}
            ]
        }

        result = generate_rent_curves(time_grid, unit_cohorts, growth_assumptions)

        # Before step: $1000
        may = next(r for r in result["by_month"] if r["month"] == "2026-05")
        self.assertAlmostEqual(may["market_rent"], 1000.0, places=2)

        # After step: $1050
        jun = next(r for r in result["by_month"] if r["month"] == "2026-06")
        self.assertAlmostEqual(jun["market_rent"], 1050.0, places=2)

    def test_multiple_step_increases(self):
        """Multiple step increases should stack."""
        time_grid = TimeGrid.build("2026-01", "2026-12")
        unit_cohorts = [
            {"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}
        ]
        growth_assumptions = {
            "growth_type": "step_only",
            "annual_growth_rate": 0.0,
            "step_increases": [
                {"effective_month": "2026-03", "increase_percent": 0.02},
                {"effective_month": "2026-06", "increase_percent": 0.03},
            ]
        }

        result = generate_rent_curves(time_grid, unit_cohorts, growth_assumptions)

        # After both steps: 1000 * 1.02 * 1.03 = 1050.60
        dec = next(r for r in result["by_month"] if r["month"] == "2026-12")
        self.assertAlmostEqual(dec["market_rent"], 1050.60, places=2)

    def test_step_with_compound_growth(self):
        """Step increases should combine with compound growth."""
        time_grid = TimeGrid.build("2026-01", "2027-12")
        unit_cohorts = [
            {"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}
        ]
        growth_assumptions = {
            "growth_type": "annual_compound",
            "annual_growth_rate": 0.03,
            "step_increases": [
                {"effective_month": "2026-06", "increase_percent": 0.02}
            ]
        }

        result = generate_rent_curves(time_grid, unit_cohorts, growth_assumptions)

        # Year 1 June+: 1000 * 1.02 = 1020
        jun_y1 = next(r for r in result["by_month"] if r["month"] == "2026-06")
        self.assertAlmostEqual(jun_y1["market_rent"], 1020.0, places=2)

        # Year 2: 1000 * 1.03 * 1.02 = 1050.60
        jan_y2 = next(r for r in result["by_month"] if r["month"] == "2027-01")
        self.assertAlmostEqual(jan_y2["market_rent"], 1050.60, places=2)


class TestLTLDecayAnnualStep(unittest.TestCase):
    """Test annual step LTL decay."""

    def test_ltl_no_decay_first_year(self):
        """LTL should hold at initial value first year."""
        time_grid = TimeGrid.build("2026-01", "2026-12")
        unit_cohorts = [
            {"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 950}
        ]
        ltl_assumptions = {
            "decay_type": "annual_step",
            "initial_ltl_percent": 0.05,
            "annual_decay_rate": 0.02,
        }

        result = generate_rent_curves(time_grid, unit_cohorts, ltl_assumptions=ltl_assumptions)

        # LTL should be 5% all year 1
        for row in result["ltl_by_month"]:
            self.assertAlmostEqual(row["ltl_percent"], 0.05, places=2)

    def test_ltl_decays_year_two(self):
        """LTL should decay in year two."""
        time_grid = TimeGrid.build("2026-01", "2027-12")
        unit_cohorts = [
            {"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 950}
        ]
        ltl_assumptions = {
            "decay_type": "annual_step",
            "initial_ltl_percent": 0.05,
            "annual_decay_rate": 0.02,
        }

        result = generate_rent_curves(time_grid, unit_cohorts, ltl_assumptions=ltl_assumptions)

        # Year 2: 5% - 2% = 3%
        jan_y2 = next(r for r in result["ltl_by_month"] if r["month"] == "2027-01")
        self.assertAlmostEqual(jan_y2["ltl_percent"], 0.03, places=2)

    def test_ltl_respects_minimum(self):
        """LTL should not decay below minimum."""
        time_grid = TimeGrid.build("2026-01", "2030-12")
        unit_cohorts = [
            {"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 950}
        ]
        ltl_assumptions = {
            "decay_type": "annual_step",
            "initial_ltl_percent": 0.05,
            "annual_decay_rate": 0.02,
            "minimum_ltl_percent": 0.01,
        }

        result = generate_rent_curves(time_grid, unit_cohorts, ltl_assumptions=ltl_assumptions)

        # By year 5, should be at minimum 1% (not negative)
        dec_y5 = next(r for r in result["ltl_by_month"] if r["month"] == "2030-12")
        self.assertAlmostEqual(dec_y5["ltl_percent"], 0.01, places=2)


class TestLTLDecayMonthlyLinear(unittest.TestCase):
    """Test monthly linear LTL decay."""

    def test_monthly_decay_gradual(self):
        """Monthly decay should be gradual each month."""
        time_grid = TimeGrid.build("2026-01", "2026-12")
        unit_cohorts = [
            {"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 950}
        ]
        ltl_assumptions = {
            "decay_type": "monthly_linear",
            "initial_ltl_percent": 0.06,
            "annual_decay_rate": 0.06,  # 0.5% per month
        }

        result = generate_rent_curves(time_grid, unit_cohorts, ltl_assumptions=ltl_assumptions)

        # After 6 months: 6% - 3% = 3%
        jul = next(r for r in result["ltl_by_month"] if r["month"] == "2026-07")
        self.assertAlmostEqual(jul["ltl_percent"], 0.03, places=2)


class TestRenovationPremiums(unittest.TestCase):
    """Test renovation premium integration."""

    def test_premium_applied_after_completion(self):
        """Premium should apply after completion month."""
        time_grid = TimeGrid.build("2026-01", "2026-12")
        unit_cohorts = [
            {"cohort_id": "1B_renovated", "unit_type": "1BR", "unit_count": 50, "initial_inplace_rent": 1000}
        ]
        renovation_premiums = {
            "1B_renovated": {
                "completion_month": "2026-06",
                "rent_premium": 200,
            }
        }

        result = generate_rent_curves(
            time_grid, unit_cohorts,
            renovation_premiums=renovation_premiums
        )

        # Before completion: $1000
        may = next(r for r in result["by_month"] if r["month"] == "2026-05")
        self.assertAlmostEqual(may["market_rent"], 1000.0, places=2)

        # After completion: $1200
        jun = next(r for r in result["by_month"] if r["month"] == "2026-06")
        self.assertAlmostEqual(jun["market_rent"], 1200.0, places=2)


class TestMultipleCohorts(unittest.TestCase):
    """Test handling of multiple cohorts."""

    def test_multiple_cohorts_independent(self):
        """Each cohort should get its own curve."""
        time_grid = TimeGrid.build("2026-01", "2026-12")
        unit_cohorts = [
            {"cohort_id": "1B", "unit_type": "1BR", "unit_count": 50, "initial_inplace_rent": 1000},
            {"cohort_id": "2B", "unit_type": "2BR", "unit_count": 50, "initial_inplace_rent": 1500},
        ]

        result = generate_rent_curves(time_grid, unit_cohorts)

        # Should have entries for both cohorts
        cohort_ids = set(r["cohort_id"] for r in result["by_month"])
        self.assertIn("1B", cohort_ids)
        self.assertIn("2B", cohort_ids)

        # Rents should differ
        jan_1b = next(r for r in result["by_month"] if r["month"] == "2026-01" and r["cohort_id"] == "1B")
        jan_2b = next(r for r in result["by_month"] if r["month"] == "2026-01" and r["cohort_id"] == "2B")
        self.assertAlmostEqual(jan_1b["market_rent"], 1000.0, places=2)
        self.assertAlmostEqual(jan_2b["market_rent"], 1500.0, places=2)


class TestGeneratedCurves(unittest.TestCase):
    """Test generated curve output format."""

    def test_generated_curve_has_periods(self):
        """Generated curve should have period-based format."""
        time_grid = TimeGrid.build("2026-01", "2027-12")
        unit_cohorts = [
            {"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}
        ]
        growth_assumptions = {
            "growth_type": "annual_compound",
            "annual_growth_rate": 0.03,
        }

        result = generate_rent_curves(time_grid, unit_cohorts, growth_assumptions)

        # Should consolidate into periods
        curve = result["generated_market_rent_curve"]
        self.assertGreater(len(curve), 0)

        # Each entry should have required fields
        for entry in curve:
            self.assertIn("cohort_id", entry)
            self.assertIn("start_period", entry)
            self.assertIn("end_period", entry)
            self.assertIn("market_rent", entry)


class TestSummary(unittest.TestCase):
    """Test summary calculations."""

    def test_summary_total_growth(self):
        """Summary should calculate total growth percent."""
        time_grid = TimeGrid.build("2026-01", "2028-12")
        unit_cohorts = [
            {"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}
        ]
        growth_assumptions = {
            "growth_type": "annual_compound",
            "annual_growth_rate": 0.05,
        }

        result = generate_rent_curves(time_grid, unit_cohorts, growth_assumptions)

        # Total growth over 2 full years: 1.05^2 - 1 = 0.1025 = 10.25%
        self.assertAlmostEqual(result["summary"]["total_growth_percent"], 0.10, places=2)

    def test_summary_ltl_reduction(self):
        """Summary should show LTL reduction."""
        time_grid = TimeGrid.build("2026-01", "2028-12")
        unit_cohorts = [
            {"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 950}
        ]
        ltl_assumptions = {
            "decay_type": "annual_step",
            "initial_ltl_percent": 0.05,
            "annual_decay_rate": 0.02,
        }

        result = generate_rent_curves(time_grid, unit_cohorts, ltl_assumptions=ltl_assumptions)

        # LTL reduction: 5% - 1% = 4%
        self.assertAlmostEqual(result["summary"]["starting_ltl"], 0.05, places=2)
        self.assertAlmostEqual(result["summary"]["ending_ltl"], 0.01, places=2)
        self.assertAlmostEqual(result["summary"]["ltl_reduction"], 0.04, places=2)


class TestEdgeCases(unittest.TestCase):
    """Test edge cases and error handling."""

    def test_no_assumptions_returns_flat(self):
        """No growth assumptions should return flat rent."""
        time_grid = TimeGrid.build("2026-01", "2026-12")
        unit_cohorts = [
            {"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}
        ]

        result = generate_rent_curves(time_grid, unit_cohorts)

        # All months should have same rent
        rents = [r["market_rent"] for r in result["by_month"]]
        self.assertEqual(len(set(rents)), 1)
        self.assertAlmostEqual(rents[0], 1000.0, places=2)

    def test_zero_growth_rate(self):
        """Zero growth rate should return flat rent."""
        time_grid = TimeGrid.build("2026-01", "2027-12")
        unit_cohorts = [
            {"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}
        ]
        growth_assumptions = {
            "growth_type": "annual_compound",
            "annual_growth_rate": 0.0,
        }

        result = generate_rent_curves(time_grid, unit_cohorts, growth_assumptions)

        # All months should have same rent
        rents = [r["market_rent"] for r in result["by_month"]]
        self.assertTrue(all(r == rents[0] for r in rents))

    def test_delayed_growth_start(self):
        """Growth should not apply before growth_start_month."""
        time_grid = TimeGrid.build("2026-01", "2027-12")
        unit_cohorts = [
            {"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}
        ]
        growth_assumptions = {
            "growth_type": "annual_compound",
            "annual_growth_rate": 0.03,
            "growth_start_month": "2027-01",  # Delayed start
        }

        result = generate_rent_curves(time_grid, unit_cohorts, growth_assumptions)

        # 2026 should have no growth
        dec_y1 = next(r for r in result["by_month"] if r["month"] == "2026-12")
        self.assertAlmostEqual(dec_y1["market_rent"], 1000.0, places=2)

        # 2027 should still have no growth (first year from growth start)
        jan_y2 = next(r for r in result["by_month"] if r["month"] == "2027-01")
        self.assertAlmostEqual(jan_y2["market_rent"], 1000.0, places=2)


if __name__ == "__main__":
    unittest.main()
