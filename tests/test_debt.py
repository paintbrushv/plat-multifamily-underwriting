"""
Tests for Debt Service Module

Tests cover:
- Interest-only period calculations
- Amortizing payment formula
- Draw schedule accumulation
- I/O to P+I transition
- Balance tracking
- Empty input handling
- Annual aggregation
"""
import unittest

from engine.modules.debt import compute_debt, _calculate_amortizing_payment
from engine.modules.cashflow import compute_cashflow
from engine.modules.time_grid import TimeGrid
from decimal import Decimal


class TestAmortizingPaymentFormula(unittest.TestCase):
    """Test the amortizing payment formula."""

    def test_standard_mortgage_payment(self):
        """Standard 30-year mortgage at 6% should calculate correctly."""
        # $100,000 loan at 6% for 30 years
        # Expected payment: ~$599.55
        principal = Decimal("100000")
        monthly_rate = Decimal("0.06") / Decimal("12")  # 0.5% monthly
        amort_months = 360

        payment = _calculate_amortizing_payment(principal, monthly_rate, amort_months)

        self.assertAlmostEqual(float(payment), 599.55, places=0)

    def test_zero_interest_rate(self):
        """Zero interest should result in simple division."""
        principal = Decimal("12000")
        monthly_rate = Decimal("0")
        amort_months = 12

        payment = _calculate_amortizing_payment(principal, monthly_rate, amort_months)

        self.assertAlmostEqual(float(payment), 1000.00, places=2)


class TestDebtInterestOnly(unittest.TestCase):
    """Test interest-only period calculations."""

    def test_io_period_interest_only(self):
        """During I/O period, debt service should equal interest only."""
        time_grid = TimeGrid.build("2026-01", "2026-12")
        debt_terms = {
            "commitment": 1000000,
            "rate": 0.06,  # 6% annual
            "amort_years": 30,
            "io_months": 24,  # 2 years I/O (longer than analysis)
        }

        result = compute_debt(time_grid, debt_terms)

        # All months should be I/O
        for row in result["by_month"]:
            self.assertTrue(row["is_io_period"])
            # Monthly interest: 1,000,000 * 0.06 / 12 = 5,000
            self.assertAlmostEqual(row["interest_expense"], 5000.00, places=2)
            self.assertAlmostEqual(row["principal_payment"], 0.00, places=2)
            self.assertAlmostEqual(row["debt_service"], 5000.00, places=2)

    def test_no_io_period(self):
        """With io_months=0, amortization should start immediately."""
        time_grid = TimeGrid.build("2026-01", "2026-03")
        debt_terms = {
            "commitment": 1000000,
            "rate": 0.06,
            "amort_years": 30,
            "io_months": 0,
        }

        result = compute_debt(time_grid, debt_terms)

        # All months should be amortizing
        for row in result["by_month"]:
            self.assertFalse(row["is_io_period"])
            self.assertGreater(row["principal_payment"], 0)


class TestDebtDrawSchedule(unittest.TestCase):
    """Test draw schedule functionality."""

    def test_progressive_draws(self):
        """Multiple draws should accumulate balance correctly."""
        time_grid = TimeGrid.build("2026-01", "2026-03")
        debt_terms = {
            "commitment": 1000000,
            "rate": 0.06,
            "amort_years": 30,
            "io_months": 12,
        }
        draw_schedule = [
            {"month": "2026-01", "draw_amount": 500000},
            {"month": "2026-02", "draw_amount": 300000},
            {"month": "2026-03", "draw_amount": 200000},
        ]

        result = compute_debt(time_grid, debt_terms, draw_schedule)

        # Check balances
        jan = result["by_month"][0]
        self.assertAlmostEqual(jan["draw_amount"], 500000.00, places=2)
        self.assertAlmostEqual(jan["ending_balance"], 500000.00, places=2)
        # Interest on 500000: 500000 * 0.06 / 12 = 2500
        self.assertAlmostEqual(jan["interest_expense"], 2500.00, places=2)

        feb = result["by_month"][1]
        self.assertAlmostEqual(feb["draw_amount"], 300000.00, places=2)
        self.assertAlmostEqual(feb["ending_balance"], 800000.00, places=2)
        # Interest on 800000: 800000 * 0.06 / 12 = 4000
        self.assertAlmostEqual(feb["interest_expense"], 4000.00, places=2)

        mar = result["by_month"][2]
        self.assertAlmostEqual(mar["draw_amount"], 200000.00, places=2)
        self.assertAlmostEqual(mar["ending_balance"], 1000000.00, places=2)
        # Interest on 1000000: 1000000 * 0.06 / 12 = 5000
        self.assertAlmostEqual(mar["interest_expense"], 5000.00, places=2)

    def test_default_full_draw_at_start(self):
        """Without draw schedule, full commitment should be drawn at start."""
        time_grid = TimeGrid.build("2026-01", "2026-03")
        debt_terms = {
            "commitment": 1000000,
            "rate": 0.06,
            "amort_years": 30,
            "io_months": 12,
        }

        result = compute_debt(time_grid, debt_terms)

        jan = result["by_month"][0]
        self.assertAlmostEqual(jan["draw_amount"], 1000000.00, places=2)
        self.assertAlmostEqual(jan["ending_balance"], 1000000.00, places=2)


class TestDebtTransition(unittest.TestCase):
    """Test I/O to amortizing transition."""

    def test_transition_io_to_amortizing(self):
        """Payment should increase when transitioning from I/O to P+I."""
        time_grid = TimeGrid.build("2026-01", "2026-06")
        debt_terms = {
            "commitment": 1000000,
            "rate": 0.06,
            "amort_years": 30,
            "io_months": 3,  # I/O for first 3 months
        }

        result = compute_debt(time_grid, debt_terms)

        # First 3 months: I/O
        for i in range(3):
            row = result["by_month"][i]
            self.assertTrue(row["is_io_period"])
            self.assertAlmostEqual(row["principal_payment"], 0.00, places=2)

        # Month 4+: Amortizing
        for i in range(3, 6):
            row = result["by_month"][i]
            self.assertFalse(row["is_io_period"])
            self.assertGreater(row["principal_payment"], 0)
            # Debt service should be higher than I/O interest
            self.assertGreater(row["debt_service"], 5000)


class TestDebtBalanceTracking(unittest.TestCase):
    """Test balance tracking over time."""

    def test_balance_reduces_during_amortization(self):
        """Outstanding balance should decrease during amortization."""
        time_grid = TimeGrid.build("2026-01", "2026-12")
        debt_terms = {
            "commitment": 1000000,
            "rate": 0.06,
            "amort_years": 30,
            "io_months": 0,  # No I/O
        }

        result = compute_debt(time_grid, debt_terms)

        # Each month's ending balance should be less than previous
        prev_balance = float("inf")
        for row in result["by_month"]:
            self.assertLess(row["ending_balance"], prev_balance)
            prev_balance = row["ending_balance"]

        # Final balance should be less than initial
        self.assertLess(result["by_month"][-1]["ending_balance"], 1000000)


class TestDebtEmptyInput(unittest.TestCase):
    """Test empty input handling."""

    def test_no_debt_terms(self):
        """No debt_terms should return all zeros."""
        time_grid = TimeGrid.build("2026-01", "2026-03")

        result = compute_debt(time_grid, debt_terms=None)

        self.assertEqual(len(result["by_month"]), 3)
        for row in result["by_month"]:
            self.assertAlmostEqual(row["debt_service"], 0.00, places=2)
            self.assertAlmostEqual(row["ending_balance"], 0.00, places=2)


class TestDebtAnnualTotals(unittest.TestCase):
    """Test annual aggregation."""

    def test_annual_totals_sum_monthly(self):
        """Annual totals should sum monthly values correctly."""
        time_grid = TimeGrid.build("2026-01", "2026-12")
        debt_terms = {
            "commitment": 1000000,
            "rate": 0.06,
            "amort_years": 30,
            "io_months": 12,  # All I/O
        }

        result = compute_debt(time_grid, debt_terms)

        # Annual interest should be sum of monthly
        monthly_interest_sum = sum(r["interest_expense"] for r in result["by_month"])
        self.assertAlmostEqual(
            result["by_year"][0]["total_interest"], monthly_interest_sum, places=2
        )
        # Full year I/O: 1,000,000 * 0.06 = 60,000
        self.assertAlmostEqual(result["by_year"][0]["total_interest"], 60000.00, places=2)


class TestDebtSummary(unittest.TestCase):
    """Test summary calculations."""

    def test_summary_totals(self):
        """Summary should show correct totals."""
        time_grid = TimeGrid.build("2026-01", "2026-12")
        debt_terms = {
            "commitment": 1000000,
            "rate": 0.06,
            "amort_years": 30,
            "io_months": 12,
        }

        result = compute_debt(time_grid, debt_terms)

        summary = result["summary"]
        self.assertAlmostEqual(summary["total_commitment"], 1000000.00, places=2)
        self.assertAlmostEqual(summary["total_drawn"], 1000000.00, places=2)
        self.assertAlmostEqual(summary["total_interest_paid"], 60000.00, places=2)
        self.assertAlmostEqual(summary["total_principal_paid"], 0.00, places=2)
        self.assertAlmostEqual(summary["final_balance"], 1000000.00, places=2)


class TestVariableRate(unittest.TestCase):
    """Test variable rate loans with rate curves."""

    def test_variable_rate_io(self):
        """Variable rate I/O should use the curve rate + spread, not the base rate."""
        time_grid = TimeGrid.build("2026-01", "2026-06")
        debt_terms = {
            "commitment": 1000000,
            "rate": 0.065,  # Base rate (used if no curve entry found)
            "amort_years": 30,
            "io_months": 12,
            "rate_type": "variable",
            "base_spread": 0.025,  # 250bps spread over SOFR
            "rate_curve": [
                {"start_month": "2026-01", "rate": 0.04},  # SOFR = 4.0%
                {"start_month": "2026-04", "rate": 0.045},  # SOFR rises to 4.5%
            ],
        }

        result = compute_debt(time_grid, debt_terms)

        # Jan-Mar: SOFR 4.0% + 2.5% spread = 6.5% → $1M * 6.5% / 12 = $5,416.67/mo
        for i in range(3):
            self.assertAlmostEqual(
                result["by_month"][i]["interest_expense"],
                1000000 * 0.065 / 12, places=0,
            )

        # Apr-Jun: SOFR 4.5% + 2.5% spread = 7.0% → $1M * 7.0% / 12 = $5,833.33/mo
        for i in range(3, 6):
            self.assertAlmostEqual(
                result["by_month"][i]["interest_expense"],
                1000000 * 0.07 / 12, places=0,
            )

    def test_rate_cap_limits_effective_rate(self):
        """Rate cap should prevent effective rate from exceeding the cap."""
        time_grid = TimeGrid.build("2026-01", "2026-06")
        debt_terms = {
            "commitment": 1000000,
            "rate": 0.065,
            "amort_years": 30,
            "io_months": 12,
            "rate_type": "variable",
            "base_spread": 0.025,  # 250bps
            "rate_cap": 0.07,      # Cap at 7.0%
            "rate_curve": [
                {"start_month": "2026-01", "rate": 0.04},   # 4.0% + 2.5% = 6.5% (under cap)
                {"start_month": "2026-04", "rate": 0.06},   # 6.0% + 2.5% = 8.5% → CAPPED to 7.0%
            ],
        }

        result = compute_debt(time_grid, debt_terms)

        # Jan-Mar: 6.5% effective (under cap)
        for i in range(3):
            self.assertAlmostEqual(
                result["by_month"][i]["interest_expense"],
                1000000 * 0.065 / 12, places=0,
            )

        # Apr-Jun: would be 8.5% but CAPPED to 7.0%
        for i in range(3, 6):
            self.assertAlmostEqual(
                result["by_month"][i]["interest_expense"],
                1000000 * 0.07 / 12, places=0,
            )

    def test_rate_floor(self):
        """Rate floor should prevent effective rate from going below the floor."""
        time_grid = TimeGrid.build("2026-01", "2026-03")
        debt_terms = {
            "commitment": 1000000,
            "rate": 0.065,
            "amort_years": 30,
            "io_months": 12,
            "rate_type": "variable",
            "base_spread": 0.01,    # 100bps spread
            "rate_floor": 0.04,     # Floor at 4.0%
            "rate_curve": [
                {"start_month": "2026-01", "rate": 0.02},  # 2.0% + 1.0% = 3.0% → FLOORED to 4.0%
            ],
        }

        result = compute_debt(time_grid, debt_terms)

        # All months: would be 3.0% but FLOORED to 4.0%
        for i in range(3):
            self.assertAlmostEqual(
                result["by_month"][i]["interest_expense"],
                1000000 * 0.04 / 12, places=0,
            )

    def test_fixed_rate_ignores_curve(self):
        """Fixed rate loans should ignore rate_curve even if provided."""
        time_grid = TimeGrid.build("2026-01", "2026-03")
        debt_terms = {
            "commitment": 1000000,
            "rate": 0.06,
            "amort_years": 30,
            "io_months": 12,
            "rate_type": "fixed",  # Explicit fixed
            "rate_curve": [
                {"start_month": "2026-01", "rate": 0.10},  # Should be ignored
            ],
        }

        result = compute_debt(time_grid, debt_terms)

        # Should use the fixed rate of 6%, not the curve
        for i in range(3):
            self.assertAlmostEqual(
                result["by_month"][i]["interest_expense"],
                1000000 * 0.06 / 12, places=0,
            )

    def test_variable_rate_amortizing_recalculates(self):
        """Variable rate amortizing loans should recalculate payment when rate changes."""
        time_grid = TimeGrid.build("2026-01", "2026-06")
        debt_terms = {
            "commitment": 500000,
            "rate": 0.06,
            "amort_years": 30,
            "io_months": 0,  # Immediately amortizing
            "rate_type": "variable",
            "base_spread": 0.02,
            "rate_curve": [
                {"start_month": "2026-01", "rate": 0.04},  # 6% effective
                {"start_month": "2026-04", "rate": 0.05},  # 7% effective
            ],
        }

        result = compute_debt(time_grid, debt_terms)

        # Debt service should change between month 3 and 4
        ds_month3 = result["by_month"][2]["debt_service"]
        ds_month4 = result["by_month"][3]["debt_service"]
        self.assertNotAlmostEqual(ds_month3, ds_month4, places=0)
        # Higher rate → higher payment
        self.assertGreater(ds_month4, ds_month3)

    def test_cap_cost_passthrough(self):
        """cap_cost field should be accepted by schema (used as closing cost upstream)."""
        time_grid = TimeGrid.build("2026-01", "2026-03")
        debt_terms = {
            "commitment": 1000000,
            "rate": 0.065,
            "amort_years": 30,
            "io_months": 12,
            "rate_type": "variable",
            "base_spread": 0.025,
            "rate_cap": 0.07,
            "cap_cost": 25000,  # $25K cap cost (handled upstream as closing cost)
            "rate_curve": [
                {"start_month": "2026-01", "rate": 0.04},
            ],
        }

        # Should not error
        result = compute_debt(time_grid, debt_terms)
        self.assertEqual(len(result["by_month"]), 3)

    def test_cap_renewal_cost_flows_to_leveraged_cashflow(self):
        """Rate-cap renewal premium should be deducted via loan_closing_costs."""
        time_grid = TimeGrid.build("2026-01", "2026-03")
        debt_terms = {
            "commitment": 1000000,
            "rate": 0.065,
            "amort_years": 30,
            "io_months": 12,
            "rate_type": "variable",
            "base_spread": 0.025,
            "rate_cap": 0.07,
            "rate_cap_expiry_month": "2026-02",
            "rate_cap_renewal_rate": 0.08,
            "rate_cap_renewal_cost": 10000,
            "rate_curve": [
                {"start_month": "2026-01", "rate": 0.04},
            ],
        }

        debt = compute_debt(time_grid, debt_terms)
        renewal_month = debt["by_month"][1]

        self.assertAlmostEqual(renewal_month["rate_cap_renewal_cost"], 10000.00, places=2)
        self.assertAlmostEqual(renewal_month["loan_closing_costs"], 10000.00, places=2)

        revenue = [
            {"month": month, "net_rent": 100000, "net_programs": 0}
            for month in time_grid.month_ids
        ]
        opex = [
            {"month": month, "total_opex": 40000, "recoverable_opex": 0}
            for month in time_grid.month_ids
        ]
        capex = [{"month": month, "total_capex": 0} for month in time_grid.month_ids]

        cashflow = compute_cashflow(time_grid, revenue, opex, capex, debt["by_month"])
        renewal_cashflow = cashflow["by_month"][1]
        expected_leveraged_cf = (
            renewal_cashflow["unleveraged_cash_flow"]
            - renewal_cashflow["debt_service"]
            - 10000
        )

        self.assertAlmostEqual(renewal_cashflow["loan_closing_costs"], 10000.00, places=2)
        self.assertAlmostEqual(
            renewal_cashflow["leveraged_cash_flow"], expected_leveraged_cf, places=2
        )


if __name__ == "__main__":
    unittest.main()
