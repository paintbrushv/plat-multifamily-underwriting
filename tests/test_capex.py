"""
Tests for Capital Expenditures Module

Tests cover:
- One-time CapEx scheduling
- Renovation-linked CapEx
- Reserve fund contributions (annual and monthly)
- Recurring CapEx
- Aggregation by month, category, and year
- Empty input handling
"""
import unittest

from engine.modules.capex import compute_capex
from engine.modules.time_grid import TimeGrid


class TestCapexOneTime(unittest.TestCase):
    """Test one_time CapEx type."""

    def test_one_time_applies_in_correct_month(self):
        """One-time CapEx should only appear in scheduled month."""
        time_grid = TimeGrid.build("2026-01", "2026-06")
        unit_cohorts = [{"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}]
        capex_schedule = [
            {
                "category": "Roof Replacement",
                "capex_type": "one_time",
                "month": "2026-03",
                "amount": 150000,
            }
        ]

        result = compute_capex(time_grid, unit_cohorts, capex_schedule)

        # Check month-by-month
        for row in result["by_month"]:
            if row["month"] == "2026-03":
                self.assertAlmostEqual(row["one_time_capex"], 150000.00, places=2)
                self.assertAlmostEqual(row["total_capex"], 150000.00, places=2)
            else:
                self.assertAlmostEqual(row["one_time_capex"], 0.00, places=2)

    def test_one_time_outside_analysis_period_ignored(self):
        """One-time CapEx outside analysis period should be ignored."""
        time_grid = TimeGrid.build("2026-01", "2026-03")
        unit_cohorts = [{"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}]
        capex_schedule = [
            {
                "category": "Future Expense",
                "capex_type": "one_time",
                "month": "2027-01",  # Outside period
                "amount": 100000,
            }
        ]

        result = compute_capex(time_grid, unit_cohorts, capex_schedule)

        # All months should have zero CapEx
        for row in result["by_month"]:
            self.assertAlmostEqual(row["total_capex"], 0.00, places=2)


class TestCapexRenovation(unittest.TestCase):
    """Test renovation CapEx type."""

    def test_renovation_applies_in_correct_month(self):
        """Renovation CapEx should appear in scheduled month."""
        time_grid = TimeGrid.build("2026-01", "2026-06")
        unit_cohorts = [{"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}]
        capex_schedule = [
            {
                "category": "Unit Renovations",
                "capex_type": "renovation",
                "month": "2026-04",
                "amount": 75000,
                "units_affected": 5,
            }
        ]

        result = compute_capex(time_grid, unit_cohorts, capex_schedule)

        april = next(r for r in result["by_month"] if r["month"] == "2026-04")
        self.assertAlmostEqual(april["renovation_capex"], 75000.00, places=2)
        self.assertAlmostEqual(april["total_capex"], 75000.00, places=2)


class TestCapexReserve(unittest.TestCase):
    """Test reserve CapEx type."""

    def test_reserve_annual_spreads_evenly(self):
        """Annual reserve should spread evenly across 12 months."""
        time_grid = TimeGrid.build("2026-01", "2026-12")
        unit_cohorts = [{"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}]
        capex_schedule = [
            {
                "category": "Capital Reserves",
                "capex_type": "reserve",
                "amount_per_unit": 240,  # $240/unit/year
                "timing": "annual",
            }
        ]

        result = compute_capex(time_grid, unit_cohorts, capex_schedule)

        # 100 units * $240/year / 12 = $2000/month
        for row in result["by_month"]:
            self.assertAlmostEqual(row["reserve_capex"], 2000.00, places=2)

        # Annual total should be $24000
        self.assertAlmostEqual(result["totals_by_year"][0]["reserve_capex"], 24000.00, places=2)

    def test_reserve_monthly_per_unit(self):
        """Monthly reserve should apply per-unit-per-month directly."""
        time_grid = TimeGrid.build("2026-01", "2026-03")
        unit_cohorts = [{"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}]
        capex_schedule = [
            {
                "category": "Capital Reserves",
                "capex_type": "reserve",
                "amount_per_unit": 20,  # $20/unit/month
                "timing": "monthly",
            }
        ]

        result = compute_capex(time_grid, unit_cohorts, capex_schedule)

        # 100 units * $20/month = $2000/month
        for row in result["by_month"]:
            self.assertAlmostEqual(row["reserve_capex"], 2000.00, places=2)

    def test_reserve_annual_falls_back_to_total_amount_when_per_unit_missing(self):
        """Bridge reserve rows can provide annual total amount without amount_per_unit."""
        time_grid = TimeGrid.build("2026-01", "2026-12")
        unit_cohorts = [{"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}]
        capex_schedule = [
            {
                "category": "Capital Reserves",
                "capex_type": "reserve",
                "amount": 24000,  # $24,000/year total = $240/unit/year
                "timing": "annual",
            }
        ]

        result = compute_capex(time_grid, unit_cohorts, capex_schedule)

        for row in result["by_month"]:
            self.assertAlmostEqual(row["reserve_capex"], 2000.00, places=2)
        self.assertAlmostEqual(result["totals_by_year"][0]["reserve_capex"], 24000.00, places=2)


class TestCapexRecurring(unittest.TestCase):
    """Test recurring CapEx type."""

    def test_recurring_applies_every_month(self):
        """Recurring CapEx should apply every month."""
        time_grid = TimeGrid.build("2026-01", "2026-06")
        unit_cohorts = [{"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}]
        capex_schedule = [
            {
                "category": "Appliance Fund",
                "capex_type": "recurring",
                "amount_per_unit_monthly": 15,  # $15/unit/month
            }
        ]

        result = compute_capex(time_grid, unit_cohorts, capex_schedule)

        # 100 units * $15/month = $1500/month
        for row in result["by_month"]:
            self.assertAlmostEqual(row["recurring_capex"], 1500.00, places=2)


class TestCapexMultipleCategories(unittest.TestCase):
    """Test multiple CapEx categories."""

    def test_multiple_categories_aggregate(self):
        """Multiple CapEx items should aggregate correctly."""
        time_grid = TimeGrid.build("2026-01", "2026-01")
        unit_cohorts = [{"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}]
        capex_schedule = [
            {
                "category": "Roof",
                "capex_type": "one_time",
                "month": "2026-01",
                "amount": 50000,
            },
            {
                "category": "Reserves",
                "capex_type": "reserve",
                "amount_per_unit": 120,  # $120/unit/year = $10/unit/month
                "timing": "annual",
            },
            {
                "category": "Recurring",
                "capex_type": "recurring",
                "amount_per_unit_monthly": 5,
            },
        ]

        result = compute_capex(time_grid, unit_cohorts, capex_schedule)

        row = result["by_month"][0]
        # One-time: 50000
        # Reserve: 100 * 120 / 12 = 1000
        # Recurring: 100 * 5 = 500
        # Total: 51500
        self.assertAlmostEqual(row["one_time_capex"], 50000.00, places=2)
        self.assertAlmostEqual(row["reserve_capex"], 1000.00, places=2)
        self.assertAlmostEqual(row["recurring_capex"], 500.00, places=2)
        self.assertAlmostEqual(row["total_capex"], 51500.00, places=2)


class TestCapexByCategory(unittest.TestCase):
    """Test by_category aggregation."""

    def test_by_category_totals(self):
        """Categories should have correct total amounts."""
        time_grid = TimeGrid.build("2026-01", "2026-12")
        unit_cohorts = [{"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}]
        capex_schedule = [
            {
                "category": "Reserves",
                "capex_type": "reserve",
                "amount_per_unit": 240,
                "timing": "annual",
            }
        ]

        result = compute_capex(time_grid, unit_cohorts, capex_schedule)

        # Find reserves category
        reserves = next(c for c in result["by_category"] if c["category"] == "Reserves")
        # 100 units * $240/year = $24000 total
        self.assertAlmostEqual(reserves["total_amount"], 24000.00, places=2)
        self.assertEqual(reserves["capex_type"], "reserve")


class TestCapexSummary(unittest.TestCase):
    """Test summary calculations."""

    def test_summary_total_and_per_unit(self):
        """Summary should show total and per-unit CapEx."""
        time_grid = TimeGrid.build("2026-01", "2026-12")
        unit_cohorts = [{"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}]
        capex_schedule = [
            {
                "category": "Reserves",
                "capex_type": "reserve",
                "amount_per_unit": 240,
                "timing": "annual",
            }
        ]

        result = compute_capex(time_grid, unit_cohorts, capex_schedule)

        # Total: 100 * 240 = 24000
        # Per unit: 24000 / 100 = 240
        self.assertAlmostEqual(result["summary"]["total_capex"], 24000.00, places=2)
        self.assertAlmostEqual(result["summary"]["capex_per_unit"], 240.00, places=2)


class TestCapexEmptyInput(unittest.TestCase):
    """Test empty input handling."""

    def test_empty_schedule_returns_zeros(self):
        """Empty capex_schedule should return zero CapEx."""
        time_grid = TimeGrid.build("2026-01", "2026-03")
        unit_cohorts = [{"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}]
        capex_schedule = []

        result = compute_capex(time_grid, unit_cohorts, capex_schedule)

        self.assertEqual(len(result["by_month"]), 3)
        for row in result["by_month"]:
            self.assertAlmostEqual(row["total_capex"], 0.00, places=2)
        self.assertAlmostEqual(result["summary"]["total_capex"], 0.00, places=2)


class TestCapexAnnualTotals(unittest.TestCase):
    """Test annual aggregation."""

    def test_annual_totals_sum_monthly(self):
        """Annual totals should sum monthly values."""
        time_grid = TimeGrid.build("2026-01", "2026-12")
        unit_cohorts = [{"cohort_id": "1B", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}]
        capex_schedule = [
            {
                "category": "Reserves",
                "capex_type": "reserve",
                "amount_per_unit": 240,
                "timing": "annual",
            }
        ]

        result = compute_capex(time_grid, unit_cohorts, capex_schedule)

        # Full year: 100 * 240 = 24000
        self.assertEqual(len(result["totals_by_year"]), 1)
        self.assertAlmostEqual(result["totals_by_year"][0]["reserve_capex"], 24000.00, places=2)


if __name__ == "__main__":
    unittest.main()
