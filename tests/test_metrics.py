"""
Tests for Investment Metrics Module

Tests cover:
- IRR calculation
- Equity multiple calculation
- DSCR calculation
- Cash-on-cash yield
- Exit value calculation
- Yield calculations
"""
import unittest

from engine.modules.metrics import compute_metrics, _calculate_irr
from engine.modules.time_grid import TimeGrid


class TestIRRCalculation(unittest.TestCase):
    """Test IRR calculation function."""

    def test_simple_irr(self):
        """Test IRR for simple known cash flows."""
        # Invest -100, receive 110 after 1 year = 10% IRR
        # But this is annual, we're testing monthly
        # Invest -100, receive 110 after 1 period = 10% return
        cash_flows = [-100, 110]
        irr = _calculate_irr(cash_flows)
        self.assertIsNotNone(irr)
        self.assertAlmostEqual(irr, 0.10, places=2)

    def test_irr_no_solution(self):
        """IRR should return None when no solution exists."""
        # All positive cash flows - no IRR
        cash_flows = [100, 100, 100]
        irr = _calculate_irr(cash_flows)
        self.assertIsNone(irr)

    def test_irr_multiple_periods(self):
        """Test IRR with multiple cash flow periods."""
        # -100 initial, +30 per period for 5 periods
        # This should give roughly 15% monthly IRR
        cash_flows = [-100, 30, 30, 30, 30, 30]
        irr = _calculate_irr(cash_flows)
        self.assertIsNotNone(irr)
        self.assertGreater(irr, 0.1)


class TestMetricsDSCR(unittest.TestCase):
    """Test DSCR calculation."""

    def test_dscr_calculation(self):
        """DSCR should be NOI / Debt Service."""
        time_grid = TimeGrid.build("2026-01", "2026-12")
        cashflow_by_month = [
            {"month": f"2026-{m:02d}", "net_operating_income": 10000, "debt_service": 8000,
             "unleveraged_cash_flow": 8000, "leveraged_cash_flow": 2000}
            for m in range(1, 13)
        ]
        cashflow_by_year = [{
            "year": "2026",
            "net_operating_income": 120000,
            "debt_service": 96000,
            "unleveraged_cash_flow": 96000,
            "leveraged_cash_flow": 24000,
        }]
        debt_by_month = [{"month": f"2026-{m:02d}", "ending_balance": 1000000} for m in range(1, 13)]

        result = compute_metrics(
            time_grid, cashflow_by_month, cashflow_by_year, debt_by_month,
            purchase_assumptions={"purchase_price": 1500000, "equity_contribution": 500000},
            exit_assumptions=None,
        )

        # DSCR = 120000 / 96000 = 1.25
        self.assertAlmostEqual(result["dscr"]["average_dscr"], 1.25, places=2)
        self.assertAlmostEqual(result["dscr"]["minimum_dscr"], 1.25, places=2)

    def test_dscr_no_debt(self):
        """DSCR should be None when no debt service."""
        time_grid = TimeGrid.build("2026-01", "2026-01")
        cashflow_by_month = [{"month": "2026-01", "net_operating_income": 10000, "debt_service": 0,
                              "unleveraged_cash_flow": 8000, "leveraged_cash_flow": 8000}]
        cashflow_by_year = [{"year": "2026", "net_operating_income": 120000, "debt_service": 0,
                             "unleveraged_cash_flow": 96000, "leveraged_cash_flow": 96000}]
        debt_by_month = [{"month": "2026-01", "ending_balance": 0}]

        result = compute_metrics(time_grid, cashflow_by_month, cashflow_by_year, debt_by_month)

        self.assertIsNone(result["dscr"]["average_dscr"])


class TestMetricsCashOnCash(unittest.TestCase):
    """Test Cash-on-Cash yield calculation."""

    def test_cash_on_cash_yield(self):
        """Cash-on-cash should be leveraged CF / equity."""
        time_grid = TimeGrid.build("2026-01", "2026-12")
        cashflow_by_month = [
            {"month": f"2026-{m:02d}", "unleveraged_cash_flow": 8000, "leveraged_cash_flow": 3000}
            for m in range(1, 13)
        ]
        cashflow_by_year = [{
            "year": "2026",
            "net_operating_income": 120000,
            "debt_service": 60000,
            "unleveraged_cash_flow": 96000,
            "leveraged_cash_flow": 36000,
        }]
        debt_by_month = [{"month": f"2026-{m:02d}", "ending_balance": 1000000} for m in range(1, 13)]

        result = compute_metrics(
            time_grid, cashflow_by_month, cashflow_by_year, debt_by_month,
            purchase_assumptions={
                "purchase_price": 1500000,
                "equity_contribution": 500000,
                "total_equity_basis": 600000,
            },
        )

        # CoC uses total_equity_basis, matching V1.5 Year-1/stabilized CoC.
        # 36000 / 600000 = 0.06.
        self.assertAlmostEqual(result["cash_on_cash"]["by_year"][0]["yield"], 0.06, places=2)
        self.assertAlmostEqual(result["cash_on_cash"]["average"], 0.06, places=2)


class TestMetricsExitCalculation(unittest.TestCase):
    """Test exit value calculation."""

    def test_exit_value_with_cap_rate(self):
        """Exit value should be NOI / cap rate."""
        time_grid = TimeGrid.build("2026-01", "2026-12")
        cashflow_by_month = [
            {"month": f"2026-{m:02d}", "net_operating_income": 10000,
             "unleveraged_cash_flow": 8000, "leveraged_cash_flow": 3000}
            for m in range(1, 13)
        ]
        cashflow_by_year = [{
            "year": "2026",
            "net_operating_income": 120000,
            "debt_service": 60000,
            "unleveraged_cash_flow": 96000,
            "leveraged_cash_flow": 36000,
        }]
        debt_by_month = [{"month": f"2026-{m:02d}", "ending_balance": 900000} for m in range(1, 13)]

        result = compute_metrics(
            time_grid, cashflow_by_month, cashflow_by_year, debt_by_month,
            purchase_assumptions={"purchase_price": 1500000, "equity_contribution": 500000},
            exit_assumptions={"exit_cap_rate": 0.05, "sale_cost_percent": 0.02, "exit_month": "2026-12"},
        )

        # Gross sale = 120000 / 0.05 = 2,400,000
        self.assertAlmostEqual(result["exit"]["gross_sale_price"], 2400000.00, places=0)
        # Sale costs = 2,400,000 * 0.02 = 48,000
        self.assertAlmostEqual(result["exit"]["sale_costs"], 48000.00, places=0)
        # Net proceeds = 2,400,000 - 48,000 - 900,000 = 1,452,000
        self.assertAlmostEqual(result["exit"]["net_sale_proceeds"], 1452000.00, places=0)


class TestMetricsEquityMultiple(unittest.TestCase):
    """Test equity multiple calculation."""

    def test_equity_multiple(self):
        """EM should be total distributions / initial equity.

        Exit proceeds are now pre-injected into cashflow by the engine,
        so the test data must include them in the exit month.
        """
        time_grid = TimeGrid.build("2026-01", "2026-12")
        # Net sale = (200000/0.05) - (4M * 0.02) - 800000 = 4M - 80K - 800K = 3,120,000
        # Gross sale = 4,000,000; sale costs = 80,000
        gross_sale = 4000000
        sale_costs = 80000
        loan_payoff = 800000
        net_sale = gross_sale - sale_costs - loan_payoff  # 3,120,000

        # 12 months of $5000 leveraged CF = $60000, with exit proceeds in month 12
        cashflow_by_month = [
            {"month": f"2026-{m:02d}", "unleveraged_cash_flow": 10000, "leveraged_cash_flow": 5000}
            for m in range(1, 13)
        ]
        # Inject exit proceeds into exit month
        cashflow_by_month[-1]["unleveraged_cash_flow"] = 10000 + gross_sale - sale_costs
        cashflow_by_month[-1]["leveraged_cash_flow"] = 5000 + net_sale

        cashflow_by_year = [{
            "year": "2026",
            "net_operating_income": 200000,
            "debt_service": 60000,
            "unleveraged_cash_flow": 120000 + gross_sale - sale_costs,
            "leveraged_cash_flow": 60000 + net_sale,
        }]
        debt_by_month = [{"month": f"2026-{m:02d}", "ending_balance": 800000} for m in range(1, 13)]

        result = compute_metrics(
            time_grid, cashflow_by_month, cashflow_by_year, debt_by_month,
            purchase_assumptions={"purchase_price": 1500000, "closing_costs": 50000, "equity_contribution": 500000},
            exit_assumptions={"exit_cap_rate": 0.05, "sale_cost_percent": 0.02, "exit_month": "2026-12"},
        )

        # Total lev distributions = 60000 + 3,120,000 = 3,180,000
        # EM = 3,180,000 / 500,000 = 6.36x
        self.assertIsNotNone(result["equity_multiple"]["levered_em"])
        self.assertGreater(result["equity_multiple"]["levered_em"], 1.0)


class TestMetricsYields(unittest.TestCase):
    """Test yield calculations."""

    def test_going_in_cap_rate(self):
        """Going-in cap rate should be Year 1 NOI / purchase price."""
        time_grid = TimeGrid.build("2026-01", "2026-12")
        cashflow_by_month = [
            {"month": f"2026-{m:02d}", "unleveraged_cash_flow": 8000, "leveraged_cash_flow": 3000}
            for m in range(1, 13)
        ]
        cashflow_by_year = [{
            "year": "2026",
            "net_operating_income": 100000,
            "debt_service": 60000,
            "unleveraged_cash_flow": 96000,
            "leveraged_cash_flow": 36000,
        }]
        debt_by_month = [{"month": f"2026-{m:02d}", "ending_balance": 1000000} for m in range(1, 13)]

        result = compute_metrics(
            time_grid, cashflow_by_month, cashflow_by_year, debt_by_month,
            purchase_assumptions={"purchase_price": 2000000, "closing_costs": 0, "equity_contribution": 500000},
        )

        # Cap rate = 100000 / 2000000 = 0.05 = 5%
        self.assertAlmostEqual(result["yields"]["going_in_cap_rate"], 0.05, places=2)


class TestMetricsMissingInputs(unittest.TestCase):
    """Test handling of missing inputs."""

    def test_no_purchase_assumptions(self):
        """Should handle missing purchase assumptions gracefully."""
        time_grid = TimeGrid.build("2026-01", "2026-01")
        cashflow_by_month = [{"month": "2026-01", "unleveraged_cash_flow": 8000, "leveraged_cash_flow": 3000}]
        cashflow_by_year = [{"year": "2026", "net_operating_income": 100000, "debt_service": 60000,
                             "unleveraged_cash_flow": 96000, "leveraged_cash_flow": 36000}]
        debt_by_month = [{"month": "2026-01", "ending_balance": 0}]

        result = compute_metrics(time_grid, cashflow_by_month, cashflow_by_year, debt_by_month)

        # Should return structure without errors
        self.assertIn("irr", result)
        self.assertIn("equity_multiple", result)


if __name__ == "__main__":
    unittest.main()
