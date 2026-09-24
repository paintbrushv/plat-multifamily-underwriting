"""
Tests for Cashflow Aggregation Module

Tests cover:
- EGI calculation (rent + programs + recovery)
- NOI calculation (EGI - OpEx)
- Unleveraged CF (NOI - CapEx)
- Leveraged CF (Unleveraged - Debt Service)
- Annual aggregation
- Summary ratios
- Negative cash flow scenarios
"""
import unittest

from engine.modules.cashflow import compute_cashflow
from engine.modules.time_grid import TimeGrid


class TestCashflowEGI(unittest.TestCase):
    """Test Effective Gross Income calculation."""

    def test_egi_components(self):
        """EGI should be sum of rent, programs, and utility recovery."""
        time_grid = TimeGrid.build("2026-01", "2026-01")
        revenue = [{"month": "2026-01", "net_rent": 100000, "net_programs": 5000}]
        opex = [{"month": "2026-01", "total_opex": 40000, "recoverable_opex": 8000}]
        capex = [{"month": "2026-01", "total_capex": 2000}]
        debt = [{"month": "2026-01", "debt_service": 30000}]

        result = compute_cashflow(time_grid, revenue, opex, capex, debt)

        row = result["by_month"][0]
        # EGI = 100000 + 5000 + 8000 = 113000
        self.assertAlmostEqual(row["effective_gross_income"], 113000.00, places=2)


class TestCashflowNOI(unittest.TestCase):
    """Test Net Operating Income calculation."""

    def test_noi_equals_egi_minus_opex(self):
        """NOI should be EGI minus total operating expenses."""
        time_grid = TimeGrid.build("2026-01", "2026-01")
        revenue = [{"month": "2026-01", "net_rent": 100000, "net_programs": 5000}]
        opex = [{"month": "2026-01", "total_opex": 40000, "recoverable_opex": 8000}]
        capex = [{"month": "2026-01", "total_capex": 0}]
        debt = [{"month": "2026-01", "debt_service": 0}]

        result = compute_cashflow(time_grid, revenue, opex, capex, debt)

        row = result["by_month"][0]
        # EGI = 100000 + 5000 + 8000 = 113000
        # NOI = 113000 - 40000 = 73000
        self.assertAlmostEqual(row["net_operating_income"], 73000.00, places=2)


class TestCashflowUnleveraged(unittest.TestCase):
    """Test Unleveraged Cash Flow calculation."""

    def test_unleveraged_cf_equals_noi_minus_capex(self):
        """Unleveraged CF should be NOI minus CapEx."""
        time_grid = TimeGrid.build("2026-01", "2026-01")
        revenue = [{"month": "2026-01", "net_rent": 100000, "net_programs": 0}]
        opex = [{"month": "2026-01", "total_opex": 30000, "recoverable_opex": 0}]
        capex = [{"month": "2026-01", "total_capex": 10000}]
        debt = [{"month": "2026-01", "debt_service": 0}]

        result = compute_cashflow(time_grid, revenue, opex, capex, debt)

        row = result["by_month"][0]
        # NOI = 100000 - 30000 = 70000
        # Unleveraged CF = 70000 - 10000 = 60000
        self.assertAlmostEqual(row["unleveraged_cash_flow"], 60000.00, places=2)


class TestCashflowLeveraged(unittest.TestCase):
    """Test Leveraged Cash Flow calculation."""

    def test_leveraged_cf_equals_unleveraged_minus_debt(self):
        """Leveraged CF should be Unleveraged CF minus debt service."""
        time_grid = TimeGrid.build("2026-01", "2026-01")
        revenue = [{"month": "2026-01", "net_rent": 100000, "net_programs": 0}]
        opex = [{"month": "2026-01", "total_opex": 30000, "recoverable_opex": 0}]
        capex = [{"month": "2026-01", "total_capex": 10000}]
        debt = [{"month": "2026-01", "debt_service": 25000}]

        result = compute_cashflow(time_grid, revenue, opex, capex, debt)

        row = result["by_month"][0]
        # Unleveraged CF = 70000 - 10000 = 60000
        # Leveraged CF = 60000 - 25000 = 35000
        self.assertAlmostEqual(row["leveraged_cash_flow"], 35000.00, places=2)

    def test_no_debt_unleveraged_equals_leveraged(self):
        """With no debt, unleveraged and leveraged CF should be equal."""
        time_grid = TimeGrid.build("2026-01", "2026-01")
        revenue = [{"month": "2026-01", "net_rent": 100000, "net_programs": 0}]
        opex = [{"month": "2026-01", "total_opex": 30000, "recoverable_opex": 0}]
        capex = [{"month": "2026-01", "total_capex": 10000}]
        debt = [{"month": "2026-01", "debt_service": 0}]

        result = compute_cashflow(time_grid, revenue, opex, capex, debt)

        row = result["by_month"][0]
        self.assertAlmostEqual(row["unleveraged_cash_flow"], row["leveraged_cash_flow"], places=2)


class TestCashflowNegative(unittest.TestCase):
    """Test negative cash flow scenarios."""

    def test_negative_leveraged_cf(self):
        """Leveraged CF can be negative when debt service exceeds unleveraged CF."""
        time_grid = TimeGrid.build("2026-01", "2026-01")
        revenue = [{"month": "2026-01", "net_rent": 50000, "net_programs": 0}]
        opex = [{"month": "2026-01", "total_opex": 30000, "recoverable_opex": 0}]
        capex = [{"month": "2026-01", "total_capex": 10000}]
        debt = [{"month": "2026-01", "debt_service": 25000}]

        result = compute_cashflow(time_grid, revenue, opex, capex, debt)

        row = result["by_month"][0]
        # NOI = 50000 - 30000 = 20000
        # Unleveraged CF = 20000 - 10000 = 10000
        # Leveraged CF = 10000 - 25000 = -15000
        self.assertAlmostEqual(row["leveraged_cash_flow"], -15000.00, places=2)


class TestCashflowAnnualTotals(unittest.TestCase):
    """Test annual aggregation."""

    def test_annual_totals_sum_monthly(self):
        """Annual totals should sum monthly values."""
        time_grid = TimeGrid.build("2026-01", "2026-03")
        revenue = [
            {"month": "2026-01", "net_rent": 100000, "net_programs": 5000},
            {"month": "2026-02", "net_rent": 100000, "net_programs": 5000},
            {"month": "2026-03", "net_rent": 100000, "net_programs": 5000},
        ]
        opex = [
            {"month": "2026-01", "total_opex": 40000, "recoverable_opex": 8000},
            {"month": "2026-02", "total_opex": 40000, "recoverable_opex": 8000},
            {"month": "2026-03", "total_opex": 40000, "recoverable_opex": 8000},
        ]
        capex = [
            {"month": "2026-01", "total_capex": 2000},
            {"month": "2026-02", "total_capex": 2000},
            {"month": "2026-03", "total_capex": 2000},
        ]
        debt = [
            {"month": "2026-01", "debt_service": 30000},
            {"month": "2026-02", "debt_service": 30000},
            {"month": "2026-03", "debt_service": 30000},
        ]

        result = compute_cashflow(time_grid, revenue, opex, capex, debt)

        year = result["by_year"][0]
        # 3 months * 100000 = 300000 net rent
        self.assertAlmostEqual(year["net_rent"], 300000.00, places=2)
        # 3 months * (113000 - 40000 - 2000 - 30000) = 3 * 41000 = 123000
        self.assertAlmostEqual(year["leveraged_cash_flow"], 123000.00, places=2)


class TestCashflowSummary(unittest.TestCase):
    """Test summary calculations."""

    def test_summary_totals(self):
        """Summary should calculate totals correctly."""
        time_grid = TimeGrid.build("2026-01", "2026-01")
        revenue = [{"month": "2026-01", "net_rent": 100000, "net_programs": 5000}]
        opex = [{"month": "2026-01", "total_opex": 40000, "recoverable_opex": 8000}]
        capex = [{"month": "2026-01", "total_capex": 2000}]
        debt = [{"month": "2026-01", "debt_service": 30000}]

        result = compute_cashflow(time_grid, revenue, opex, capex, debt)

        summary = result["summary"]
        # EGI = 113000
        self.assertAlmostEqual(summary["total_egi"], 113000.00, places=2)
        # NOI = 73000
        self.assertAlmostEqual(summary["total_noi"], 73000.00, places=2)
        self.assertAlmostEqual(summary["total_capex"], 2000.00, places=2)
        self.assertAlmostEqual(summary["total_debt_service"], 30000.00, places=2)

    def test_summary_ratios(self):
        """Summary should calculate NOI margin and OpEx ratio."""
        time_grid = TimeGrid.build("2026-01", "2026-01")
        revenue = [{"month": "2026-01", "net_rent": 100000, "net_programs": 0}]
        opex = [{"month": "2026-01", "total_opex": 40000, "recoverable_opex": 0}]
        capex = [{"month": "2026-01", "total_capex": 0}]
        debt = [{"month": "2026-01", "debt_service": 0}]

        result = compute_cashflow(time_grid, revenue, opex, capex, debt)

        summary = result["summary"]
        # EGI = 100000, OpEx = 40000, NOI = 60000
        # NOI Margin = 60000 / 100000 = 0.6
        # OpEx Ratio = 40000 / 100000 = 0.4
        self.assertAlmostEqual(summary["average_noi_margin"], 0.60, places=2)
        self.assertAlmostEqual(summary["average_opex_ratio"], 0.40, places=2)


class TestCashflowMissingInputs(unittest.TestCase):
    """Test handling of missing inputs."""

    def test_empty_revenue(self):
        """Empty revenue should result in zero EGI."""
        time_grid = TimeGrid.build("2026-01", "2026-01")
        revenue = []
        opex = [{"month": "2026-01", "total_opex": 40000, "recoverable_opex": 0}]
        capex = [{"month": "2026-01", "total_capex": 2000}]
        debt = [{"month": "2026-01", "debt_service": 30000}]

        result = compute_cashflow(time_grid, revenue, opex, capex, debt)

        row = result["by_month"][0]
        self.assertAlmostEqual(row["effective_gross_income"], 0.00, places=2)
        # NOI = 0 - 40000 = -40000
        self.assertAlmostEqual(row["net_operating_income"], -40000.00, places=2)


class TestCashflowFullWaterfall(unittest.TestCase):
    """Test complete cashflow waterfall calculation."""

    def test_full_waterfall_consistency(self):
        """All waterfall steps should be mathematically consistent."""
        time_grid = TimeGrid.build("2026-01", "2026-01")
        revenue = [{"month": "2026-01", "net_rent": 95000, "net_programs": 5000}]
        opex = [{"month": "2026-01", "total_opex": 42500, "recoverable_opex": 8500}]
        capex = [{"month": "2026-01", "total_capex": 2000}]
        debt = [{"month": "2026-01", "debt_service": 35000}]

        result = compute_cashflow(time_grid, revenue, opex, capex, debt)

        row = result["by_month"][0]

        # Verify each step of the waterfall
        expected_egi = 95000 + 5000 + 8500  # 108500
        self.assertAlmostEqual(row["effective_gross_income"], expected_egi, places=2)

        expected_noi = expected_egi - 42500  # 66000
        self.assertAlmostEqual(row["net_operating_income"], expected_noi, places=2)

        expected_unlev = expected_noi - 2000  # 64000
        self.assertAlmostEqual(row["unleveraged_cash_flow"], expected_unlev, places=2)

        expected_lev = expected_unlev - 35000  # 29000
        self.assertAlmostEqual(row["leveraged_cash_flow"], expected_lev, places=2)


if __name__ == "__main__":
    unittest.main()
