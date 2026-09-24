"""
Tests for Operating Expenses Module

Tests cover:
- All calculation types (fixed, per-unit, per-sqft, percent of revenue)
- Growth rate application
- Recoverable vs non-recoverable tracking
- Empty input handling
- Multi-year growth accumulation
"""
import unittest

from engine.modules.opex import compute_opex
from engine.modules.time_grid import TimeGrid


class TestOpexFixedAnnual(unittest.TestCase):
    """Test fixed_annual calculation type."""

    def test_fixed_annual_spreads_evenly(self):
        """Fixed annual expense should spread evenly across 12 months."""
        time_grid = TimeGrid.build("2026-01", "2026-12")
        unit_cohorts = [{"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}]
        opex_table = [
            {
                "category_name": "Property Taxes",
                "calculation_type": "fixed_annual",
                "base_value": 120000,
                "growth_rate": 0,
                "timing": "annual",
                "recoverable_flag": False,
            }
        ]

        result = compute_opex(time_grid, unit_cohorts, opex_table)

        # 120000 / 12 = 10000 per month
        for row in result["totals_by_month"]:
            self.assertAlmostEqual(row["total_opex"], 10000.00, places=2)

        # Total for year should be 120000
        self.assertAlmostEqual(result["totals_by_year"][0]["total_opex"], 120000.00, places=2)


class TestOpexPerUnit(unittest.TestCase):
    """Test per_unit calculation type."""

    def test_per_unit_calculation(self):
        """Per-unit expense should multiply by total units."""
        time_grid = TimeGrid.build("2026-01", "2026-01")
        unit_cohorts = [
            {"cohort_id": "1B", "unit_type": "1BR", "unit_count": 50, "initial_inplace_rent": 1000},
            {"cohort_id": "2B", "unit_type": "2BR", "unit_count": 50, "initial_inplace_rent": 1200},
        ]
        opex_table = [
            {
                "category_name": "R&M",
                "calculation_type": "per_unit",
                "base_value": 600,  # $600/unit/year
                "growth_rate": 0,
                "timing": "annual",
                "recoverable_flag": False,
            }
        ]

        result = compute_opex(time_grid, unit_cohorts, opex_table)

        # 100 units * $600/year / 12 months = $5000/month
        self.assertAlmostEqual(result["totals_by_month"][0]["total_opex"], 5000.00, places=2)


class TestOpexPerUnitMonthly(unittest.TestCase):
    """Test per_unit_monthly calculation type."""

    def test_per_unit_monthly_calculation(self):
        """Per-unit-monthly expense should be base * units directly."""
        time_grid = TimeGrid.build("2026-01", "2026-01")
        unit_cohorts = [{"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}]
        opex_table = [
            {
                "category_name": "Utilities",
                "calculation_type": "per_unit_monthly",
                "base_value": 85,  # $85/unit/month
                "growth_rate": 0,
                "timing": "monthly",
                "recoverable_flag": True,
            }
        ]

        result = compute_opex(time_grid, unit_cohorts, opex_table)

        # 100 units * $85/month = $8500/month
        self.assertAlmostEqual(result["totals_by_month"][0]["total_opex"], 8500.00, places=2)
        # Should be recoverable
        self.assertAlmostEqual(result["totals_by_month"][0]["recoverable_opex"], 8500.00, places=2)
        self.assertAlmostEqual(result["totals_by_month"][0]["non_recoverable_opex"], 0.00, places=2)


class TestOpexPerSqft(unittest.TestCase):
    """Test per_sqft calculation type."""

    def test_per_sqft_calculation(self):
        """Per-sqft expense should multiply by total square footage."""
        time_grid = TimeGrid.build("2026-01", "2026-01")
        unit_cohorts = [
            {"cohort_id": "1B", "unit_type": "1BR", "unit_count": 50, "sqft": 700, "initial_inplace_rent": 1000},
            {"cohort_id": "2B", "unit_type": "2BR", "unit_count": 50, "sqft": 1000, "initial_inplace_rent": 1200},
        ]
        opex_table = [
            {
                "category_name": "CAM",
                "calculation_type": "per_sqft",
                "base_value": 6,  # $6/sqft/year
                "growth_rate": 0,
                "timing": "annual",
                "recoverable_flag": False,
            }
        ]

        result = compute_opex(time_grid, unit_cohorts, opex_table)

        # Total sqft = 50*700 + 50*1000 = 35000 + 50000 = 85000
        # 85000 * $6/year / 12 months = $42500/month
        self.assertAlmostEqual(result["totals_by_month"][0]["total_opex"], 42500.00, places=2)


class TestOpexPercentEGR(unittest.TestCase):
    """Test percent_egr calculation type."""

    def test_percent_egr_calculation(self):
        """Percent of EGR expense should calculate from revenue."""
        time_grid = TimeGrid.build("2026-01", "2026-01")
        unit_cohorts = [{"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}]
        opex_table = [
            {
                "category_name": "Management Fee",
                "calculation_type": "percent_egr",
                "base_value": 0.04,  # 4% of revenue
                "growth_rate": 0,
                "timing": "monthly",
                "recoverable_flag": False,
            }
        ]
        revenue_by_month = [{"month": "2026-01", "net_total_revenue": 100000}]

        result = compute_opex(time_grid, unit_cohorts, opex_table, revenue_by_month)

        # 4% of $100000 = $4000
        self.assertAlmostEqual(result["totals_by_month"][0]["total_opex"], 4000.00, places=2)

    def test_percent_egr_without_revenue(self):
        """Percent of EGR should return 0 if no revenue provided."""
        time_grid = TimeGrid.build("2026-01", "2026-01")
        unit_cohorts = [{"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}]
        opex_table = [
            {
                "category_name": "Management Fee",
                "calculation_type": "percent_egr",
                "base_value": 0.04,
                "growth_rate": 0,
                "timing": "monthly",
                "recoverable_flag": False,
            }
        ]

        result = compute_opex(time_grid, unit_cohorts, opex_table, revenue_by_month=None)

        self.assertAlmostEqual(result["totals_by_month"][0]["total_opex"], 0.00, places=2)


class TestOpexGrowthRate(unittest.TestCase):
    """Test growth rate application."""

    def test_growth_applies_at_year_boundary(self):
        """Growth rate should apply at integer year boundaries."""
        time_grid = TimeGrid.build("2026-01", "2027-01")
        unit_cohorts = [{"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}]
        opex_table = [
            {
                "category_name": "Property Taxes",
                "calculation_type": "fixed_annual",
                "base_value": 120000,
                "growth_rate": 0.03,  # 3% growth
                "timing": "annual",
                "recoverable_flag": False,
            }
        ]

        result = compute_opex(time_grid, unit_cohorts, opex_table)

        # Year 1 (2026): $120000 / 12 = $10000/month
        jan_2026 = next(r for r in result["totals_by_month"] if r["month"] == "2026-01")
        self.assertAlmostEqual(jan_2026["total_opex"], 10000.00, places=2)

        # Year 2 (2027): $120000 * 1.03 / 12 = $10300/month
        jan_2027 = next(r for r in result["totals_by_month"] if r["month"] == "2027-01")
        self.assertAlmostEqual(jan_2027["total_opex"], 10300.00, places=2)

    def test_multi_year_compound_growth(self):
        """Growth should compound over multiple years."""
        time_grid = TimeGrid.build("2026-01", "2028-01")
        unit_cohorts = [{"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}]
        opex_table = [
            {
                "category_name": "Property Taxes",
                "calculation_type": "fixed_annual",
                "base_value": 120000,
                "growth_rate": 0.03,
                "timing": "annual",
                "recoverable_flag": False,
            }
        ]

        result = compute_opex(time_grid, unit_cohorts, opex_table)

        # Year 3 (2028): $120000 * 1.03^2 / 12 = $10609/month
        jan_2028 = next(r for r in result["totals_by_month"] if r["month"] == "2028-01")
        self.assertAlmostEqual(jan_2028["total_opex"], 10609.00, places=2)


class TestOpexRecoverable(unittest.TestCase):
    """Test recoverable vs non-recoverable categorization."""

    def test_recoverable_segregation(self):
        """Recoverable and non-recoverable should be tracked separately."""
        time_grid = TimeGrid.build("2026-01", "2026-01")
        unit_cohorts = [{"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}]
        opex_table = [
            {
                "category_name": "Utilities",
                "calculation_type": "per_unit_monthly",
                "base_value": 100,
                "growth_rate": 0,
                "timing": "monthly",
                "recoverable_flag": True,
            },
            {
                "category_name": "Property Taxes",
                "calculation_type": "fixed_annual",
                "base_value": 120000,
                "growth_rate": 0,
                "timing": "annual",
                "recoverable_flag": False,
            },
        ]

        result = compute_opex(time_grid, unit_cohorts, opex_table)

        row = result["totals_by_month"][0]
        # Utilities: 100 * 100 = 10000 (recoverable)
        # Taxes: 120000 / 12 = 10000 (non-recoverable)
        self.assertAlmostEqual(row["total_opex"], 20000.00, places=2)
        self.assertAlmostEqual(row["recoverable_opex"], 10000.00, places=2)
        self.assertAlmostEqual(row["non_recoverable_opex"], 10000.00, places=2)


class TestOpexEmptyInput(unittest.TestCase):
    """Test empty input handling."""

    def test_empty_opex_table(self):
        """Empty opex_table should return zero expenses."""
        time_grid = TimeGrid.build("2026-01", "2026-03")
        unit_cohorts = [{"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}]
        opex_table = []

        result = compute_opex(time_grid, unit_cohorts, opex_table)

        self.assertEqual(len(result["totals_by_month"]), 3)
        for row in result["totals_by_month"]:
            self.assertAlmostEqual(row["total_opex"], 0.00, places=2)


class TestOpexAnnualTotals(unittest.TestCase):
    """Test annual aggregation."""

    def test_annual_totals_aggregate_monthly(self):
        """Annual totals should sum monthly values."""
        time_grid = TimeGrid.build("2026-01", "2026-12")
        unit_cohorts = [{"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}]
        opex_table = [
            {
                "category_name": "Property Taxes",
                "calculation_type": "fixed_annual",
                "base_value": 120000,
                "growth_rate": 0,
                "timing": "annual",
                "recoverable_flag": False,
            }
        ]

        result = compute_opex(time_grid, unit_cohorts, opex_table)

        # Full year should total to base_value
        self.assertEqual(len(result["totals_by_year"]), 1)
        self.assertAlmostEqual(result["totals_by_year"][0]["total_opex"], 120000.00, places=2)


class TestPhasedGrowth(unittest.TestCase):
    """Test multi-phase growth (e.g., RE Tax reassessment mid-hold)."""

    def test_two_phase_growth(self):
        """Year 1-2: 0% growth, Year 3+: 2% growth (reassessment kicks in)."""
        time_grid = TimeGrid.build("2026-01", "2029-12")
        unit_cohorts = [{"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}]
        opex_table = [
            {
                "category_name": "RE Tax",
                "calculation_type": "fixed_annual",
                "base_value": 100000,
                "recoverable_flag": False,
                "growth_phases": [
                    {"through_year": 1, "rate": 0.0},
                    {"from_year": 2, "rate": 0.02},
                ],
            }
        ]

        result = compute_opex(time_grid, unit_cohorts, opex_table)
        by_year = result["totals_by_year"]

        # effective_growth_years: Y1=0, Y2=1, Y3=2, Y4=3
        # Phase 1 (through growth year 1): 0% → Y1=1.0, Y2=1.0
        # Phase 2 (from growth year 2): 2% → Y3=1.0*1.02, Y4=1.0*1.02*1.02
        self.assertAlmostEqual(by_year[0]["total_opex"], 100000, places=0)
        self.assertAlmostEqual(by_year[1]["total_opex"], 100000, places=0)
        self.assertAlmostEqual(by_year[2]["total_opex"], 102000, places=0)
        self.assertAlmostEqual(by_year[3]["total_opex"], 104040, places=0)

    def test_step_change_growth(self):
        """20% jump in growth year 1, then 3% inflation from year 2+."""
        time_grid = TimeGrid.build("2026-01", "2029-12")
        unit_cohorts = [{"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}]
        opex_table = [
            {
                "category_name": "RE Tax",
                "calculation_type": "fixed_annual",
                "base_value": 100000,
                "recoverable_flag": False,
                "growth_phases": [
                    {"through_year": 1, "rate": 0.20},
                    {"from_year": 2, "rate": 0.03},
                ],
            }
        ]

        result = compute_opex(time_grid, unit_cohorts, opex_table)
        by_year = result["totals_by_year"]

        # effective_growth_years: Y1=0, Y2=1, Y3=2, Y4=3
        # Phase 1 (through yr 1): 20% → Y2 = 100K * 1.20 = 120K
        # Phase 2 (from yr 2): 3%  → Y3 = 100K * 1.20 * 1.03 = 123,600
        self.assertAlmostEqual(by_year[0]["total_opex"], 100000, places=0)
        self.assertAlmostEqual(by_year[1]["total_opex"], 120000, places=0)
        self.assertAlmostEqual(by_year[2]["total_opex"], 123600, places=0)
        self.assertAlmostEqual(by_year[3]["total_opex"], 127308, places=0)

    def test_no_phases_backward_compat(self):
        """Without growth_phases, use standard growth_rate."""
        time_grid = TimeGrid.build("2026-01", "2027-12")
        unit_cohorts = [{"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}]
        opex_table = [
            {
                "category_name": "Insurance",
                "calculation_type": "fixed_annual",
                "base_value": 50000,
                "growth_rate": 0.03,
                "recoverable_flag": False,
            }
        ]

        result = compute_opex(time_grid, unit_cohorts, opex_table)
        by_year = result["totals_by_year"]

        self.assertAlmostEqual(by_year[0]["total_opex"], 50000, places=0)
        self.assertAlmostEqual(by_year[1]["total_opex"], 51500, places=0)


if __name__ == "__main__":
    unittest.main()
