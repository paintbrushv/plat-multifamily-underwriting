"""
Tests for Bridge-to-Perm Refinance Event (Task 1.3)

Tests cover:
- Dynamic DSCR-based loan sizing
- Dynamic LTV-based loan sizing
- Refi proceeds (cash out) and shortfall computation
- Debt service transition (bridge stops, perm starts)
- Forward NOI window (the 12 months from the refi month, annualized near the
  end of the analysis); the trailing window is an explicit opt-in
- Debt constant computation
- Full engine integration with refi_event
- Backward compatibility (no refi_event = unchanged behavior)
"""
import unittest
from decimal import Decimal

from tests.synthetic_tax_policy import with_synthetic_tax_policy

from engine.modules.debt import (
    _annual_debt_constant,
    _calculate_amortizing_payment,
    compute_debt,
    size_refi_loan,
)
from engine.modules.time_grid import TimeGrid
from engine.modules.util import dec, round2


def _make_cashflow_by_month(time_grid, monthly_noi=50000):
    """Helper: generate flat-NOI monthly cashflow for testing."""
    return [
        {
            "month": m,
            "net_operating_income": monthly_noi,
            "leveraged_cash_flow": monthly_noi * 0.6,
            "unleveraged_cash_flow": monthly_noi,
        }
        for m in time_grid.month_ids
    ]


def _make_debt_by_month(time_grid, balance=5000000):
    """Helper: generate flat-balance debt for testing (I/O bridge)."""
    return [
        {
            "month": m,
            "beginning_balance": balance,
            "ending_balance": balance,
            "debt_service": balance * 0.06 / 12,
            "draw_amount": balance if i == 0 else 0,
        }
        for i, m in enumerate(time_grid.month_ids)
    ]


class TestAnnualDebtConstant(unittest.TestCase):
    """Test the annual debt constant calculation."""

    def test_standard_30yr_amort(self):
        """30-year amort at 5.5% should produce ~6.8% annual constant."""
        constant = _annual_debt_constant(Decimal("0.055"), 30)
        # 30yr at 5.5%: monthly payment per $1 ≈ 0.00568, annual ≈ 0.0681
        self.assertAlmostEqual(float(constant), 0.0681, places=3)

    def test_25yr_amort(self):
        """25-year amort at 6% should produce higher constant than 30yr."""
        c_25 = _annual_debt_constant(Decimal("0.06"), 25)
        c_30 = _annual_debt_constant(Decimal("0.06"), 30)
        self.assertGreater(float(c_25), float(c_30))

    def test_zero_rate(self):
        """Zero rate should produce 1/amort_months * 12."""
        constant = _annual_debt_constant(Decimal("0"), 30)
        expected = Decimal("12") / Decimal("360")
        self.assertAlmostEqual(float(constant), float(expected), places=6)


class TestSizeRefiLoanDSCR(unittest.TestCase):
    """Test DSCR-based loan sizing."""

    def setUp(self):
        self.time_grid = TimeGrid.build("2025-01", "2030-12")
        self.cashflow = _make_cashflow_by_month(self.time_grid, monthly_noi=50000)
        self.debt = _make_debt_by_month(self.time_grid, balance=5000000)

    def test_dscr_sizes_loan_correctly(self):
        """DSCR sizing should produce loan where DS / NOI ≈ target DSCR."""
        refi_event = {
            "trigger_month": "2027-01",
            "sizing_method": "dscr",
            "target_dscr": 1.25,
            "perm_loan_terms": {"rate": 0.055, "amort_years": 30},
        }
        result = size_refi_loan(refi_event, self.cashflow, self.debt)

        commitment = dec(result["sized_commitment"])
        self.assertGreater(float(commitment), 0)

        # Verify: NOI / (commitment × debt_constant) ≈ target_dscr
        annual_noi = Decimal("600000")  # 50k × 12
        debt_constant = _annual_debt_constant(Decimal("0.055"), 30)
        actual_dscr = annual_noi / (commitment * debt_constant)
        self.assertAlmostEqual(float(actual_dscr), 1.25, places=1)

    def test_dscr_rounds_to_1000(self):
        """Commitment should be rounded to nearest $1,000."""
        refi_event = {
            "trigger_month": "2027-01",
            "sizing_method": "dscr",
            "target_dscr": 1.25,
            "perm_loan_terms": {"rate": 0.055, "amort_years": 30},
        }
        result = size_refi_loan(refi_event, self.cashflow, self.debt)
        commitment = float(result["sized_commitment"])
        self.assertEqual(commitment % 1000, 0)

    def test_higher_dscr_means_smaller_loan(self):
        """Higher DSCR target → smaller loan (more conservative)."""
        base = {
            "trigger_month": "2027-01",
            "sizing_method": "dscr",
            "perm_loan_terms": {"rate": 0.055, "amort_years": 30},
        }
        r125 = size_refi_loan({**base, "target_dscr": 1.25}, self.cashflow, self.debt)
        r150 = size_refi_loan({**base, "target_dscr": 1.50}, self.cashflow, self.debt)
        self.assertGreater(
            float(r125["sized_commitment"]),
            float(r150["sized_commitment"]),
        )


class TestSizeRefiLoanLTV(unittest.TestCase):
    """Test LTV-based loan sizing."""

    def setUp(self):
        self.time_grid = TimeGrid.build("2025-01", "2030-12")
        self.cashflow = _make_cashflow_by_month(self.time_grid, monthly_noi=50000)
        self.debt = _make_debt_by_month(self.time_grid, balance=5000000)

    def test_ltv_sizes_loan_correctly(self):
        """LTV sizing: loan = property_value × LTV."""
        refi_event = {
            "trigger_month": "2027-01",
            "sizing_method": "ltv",
            "target_ltv": 0.70,
            "exit_cap_for_ltv": 0.05,
            "perm_loan_terms": {"rate": 0.055, "amort_years": 30},
        }
        result = size_refi_loan(refi_event, self.cashflow, self.debt)

        # Property value = 600K NOI / 5% cap = $12M
        # Loan = $12M × 70% = $8.4M
        commitment = float(result["sized_commitment"])
        self.assertAlmostEqual(commitment, 8400000, delta=1000)

    def test_ltv_falls_back_to_exit_cap(self):
        """When exit_cap_for_ltv not specified, uses exit_assumptions cap rate."""
        refi_event = {
            "trigger_month": "2027-01",
            "sizing_method": "ltv",
            "target_ltv": 0.70,
            "perm_loan_terms": {"rate": 0.055, "amort_years": 30},
        }
        exit_assum = {"exit_cap_rate": 0.05, "exit_month": "2030-01"}
        result = size_refi_loan(
            refi_event, self.cashflow, self.debt, exit_assumptions=exit_assum
        )
        # Same as above — should be ~$8.4M
        commitment = float(result["sized_commitment"])
        self.assertAlmostEqual(commitment, 8400000, delta=1000)


class TestRefiProceeds(unittest.TestCase):
    """Test refi proceeds and shortfall computation."""

    def setUp(self):
        self.time_grid = TimeGrid.build("2025-01", "2030-12")
        self.cashflow = _make_cashflow_by_month(self.time_grid, monthly_noi=50000)

    def test_cash_out_refi(self):
        """When perm loan > bridge payoff + costs, proceeds are positive (cash out)."""
        # Small bridge balance → big perm loan → cash out
        debt = _make_debt_by_month(self.time_grid, balance=3000000)
        refi_event = {
            "trigger_month": "2027-01",
            "sizing_method": "ltv",
            "target_ltv": 0.70,
            "exit_cap_for_ltv": 0.05,
            "perm_loan_terms": {"rate": 0.055, "amort_years": 30},
            "refi_costs_pct": 0.01,
        }
        result = size_refi_loan(refi_event, self.cashflow, debt)

        # Perm ~$8.4M, bridge payoff $3M, costs ~$84K
        # Proceeds ≈ 8.4M - 3M - 84K ≈ $5.3M
        self.assertGreater(float(result["refi_proceeds"]), 0)
        self.assertAlmostEqual(float(result["bridge_payoff"]), 3000000, places=0)

    def test_shortfall_refi(self):
        """When perm loan < bridge payoff + costs, proceeds are negative (shortfall)."""
        # Large bridge balance → small perm loan → shortfall
        debt = _make_debt_by_month(self.time_grid, balance=10000000)
        refi_event = {
            "trigger_month": "2027-01",
            "sizing_method": "dscr",
            "target_dscr": 1.50,  # Conservative → smaller loan
            "perm_loan_terms": {"rate": 0.065, "amort_years": 25},
            "refi_costs_pct": 0.01,
        }
        result = size_refi_loan(refi_event, self.cashflow, debt)

        # Perm loan sized from $600K NOI at 1.5 DSCR will be much less than $10M
        self.assertLess(float(result["refi_proceeds"]), 0)

    def test_refi_costs_default_1pct(self):
        """Default refi costs = 1% of new loan amount."""
        debt = _make_debt_by_month(self.time_grid, balance=5000000)
        refi_event = {
            "trigger_month": "2027-01",
            "sizing_method": "ltv",
            "target_ltv": 0.70,
            "exit_cap_for_ltv": 0.05,
            "perm_loan_terms": {"rate": 0.055, "amort_years": 30},
            # No refi_costs_pct → defaults to 0.01
        }
        result = size_refi_loan(refi_event, self.cashflow, debt)
        commitment = float(result["sized_commitment"])
        costs = float(result["refi_costs"])
        self.assertAlmostEqual(costs, commitment * 0.01, places=0)


def _make_ramp_cashflow(time_grid, noi_for_index):
    """Helper: monthly cashflow whose NOI is ``noi_for_index(i)``."""
    return [
        {
            "month": m,
            "net_operating_income": noi_for_index(i),
            "leveraged_cash_flow": noi_for_index(i) * 0.6,
            "unleveraged_cash_flow": noi_for_index(i),
        }
        for i, m in enumerate(time_grid.month_ids)
    ]


DSCR_REFI = {
    "trigger_month": "2027-01",
    "sizing_method": "dscr",
    "target_dscr": 1.25,
    "perm_loan_terms": {"rate": 0.055, "amort_years": 30},
}


def _dscr_loan(cash_flow, dscr="1.25", rate="0.055", amort=30):
    constant = _annual_debt_constant(Decimal(rate), amort)
    raw = Decimal(str(cash_flow)) / (Decimal(dscr) * constant)
    return float((raw / 1000).quantize(Decimal("1")) * 1000)


class TestTrailingNOI(unittest.TestCase):
    """The NOI window: forward by default, trailing only on request."""

    def test_uses_forward_12_months(self):
        """Default: uses the 12 months starting at the refi month."""
        time_grid = TimeGrid.build("2025-01", "2028-12")
        cashflow = _make_cashflow_by_month(time_grid, monthly_noi=50000)
        debt = _make_debt_by_month(time_grid, balance=5000000)

        result = size_refi_loan(DSCR_REFI, cashflow, debt)
        # Stabilized NOI = 12 × $50K = $600K (2027-01 through 2027-12)
        self.assertAlmostEqual(float(result["stabilized_noi"]), 600000, places=0)
        self.assertEqual(
            result["noi_window"],
            {"start": "2027-01", "end": "2027-12", "months": 12, "forward": True},
        )

    def test_trailing_opt_in_annualizes_short_history(self):
        """noi_window=trailing: less than 12 months before trigger is annualized."""
        time_grid = TimeGrid.build("2026-06", "2028-12")
        cashflow = _make_cashflow_by_month(time_grid, monthly_noi=50000)
        debt = _make_debt_by_month(time_grid, balance=5000000)

        refi_event = {**DSCR_REFI, "noi_window": "trailing"}  # Only 7 months of history
        result = size_refi_loan(refi_event, cashflow, debt)
        # 7 months × $50K = $350K → annualized = $350K × 12/7 = $600K
        self.assertAlmostEqual(float(result["stabilized_noi"]), 600000, places=0)
        self.assertEqual(
            result["noi_window"],
            {"start": "2026-06", "end": "2026-12", "months": 7, "forward": False},
        )

    def test_custom_noi_months(self):
        """noi_months sets the trailing window when noi_window=trailing."""
        time_grid = TimeGrid.build("2025-01", "2028-12")
        # Ramp NOI: $30K in year 1, $60K in year 2, $90K after
        cashflow = _make_ramp_cashflow(
            time_grid, lambda i: 30000 if i < 12 else 60000 if i < 24 else 90000
        )
        debt = _make_debt_by_month(time_grid, balance=5000000)

        # 6-month trailing window at 2026-10 → 2026-04..2026-09, all $60K months
        refi_event = {
            **DSCR_REFI,
            "trigger_month": "2026-10",
            "noi_window": "trailing",
            "noi_months": 6,
        }
        result = size_refi_loan(refi_event, cashflow, debt)
        # 6 months × $60K = $360K → annualized = $720K
        self.assertAlmostEqual(float(result["stabilized_noi"]), 720000, places=0)

        # The house default ignores noi_months: forward 2026-10..2027-09
        # = 3 × $60K + 9 × $90K = $990K
        forward = size_refi_loan({**refi_event, "noi_window": "forward"}, cashflow, debt)
        self.assertAlmostEqual(float(forward["stabilized_noi"]), 990000, places=0)
        default = {k: v for k, v in refi_event.items() if k != "noi_window"}
        self.assertEqual(
            size_refi_loan(default, cashflow, debt)["stabilized_noi"],
            forward["stabilized_noi"],
        )


class TestForwardNOIWindow(unittest.TestCase):
    """A stabilized refi sizes on forward NOI (product-owner decision)."""

    def setUp(self):
        self.time_grid = TimeGrid.build("2025-01", "2028-12")
        self.debt = _make_debt_by_month(self.time_grid, balance=5000000)

    def test_forward_window_is_used(self):
        """NOI before the refi month does not enter the sizing NOI."""
        # $40K a month before the refi (2027-01), $50K from it
        cashflow = _make_ramp_cashflow(self.time_grid, lambda i: 40000 if i < 24 else 50000)
        result = size_refi_loan(DSCR_REFI, cashflow, self.debt)
        self.assertEqual(result["noi_used"], 600000.0)
        self.assertEqual(result["stabilized_noi"], 600000.0)
        self.assertEqual(result["sized_commitment"], _dscr_loan(600000))
        self.assertEqual(result["noi_window"]["start"], "2027-01")
        self.assertEqual(result["noi_window"]["end"], "2027-12")
        self.assertTrue(result["noi_window"]["forward"])

    def test_rising_noi_sizes_a_larger_forward_loan(self):
        """DSCR-bound with NOI growing: the forward loan exceeds the trailing one."""
        # NOI grows 1% a month
        cashflow = _make_ramp_cashflow(self.time_grid, lambda i: 40000 * 1.01 ** i)
        forward = size_refi_loan(DSCR_REFI, cashflow, self.debt)
        trailing = size_refi_loan({**DSCR_REFI, "noi_window": "trailing"}, cashflow, self.debt)

        forward_noi = sum(40000 * 1.01 ** i for i in range(24, 36))
        trailing_noi = sum(40000 * 1.01 ** i for i in range(12, 24))
        self.assertAlmostEqual(forward["noi_used"], forward_noi, delta=0.01)
        self.assertAlmostEqual(trailing["noi_used"], trailing_noi, delta=0.01)
        self.assertGreater(forward["sized_commitment"], trailing["sized_commitment"])
        self.assertEqual(forward["sized_commitment"], _dscr_loan(round(forward_noi, 2)))

    def test_window_is_annualized_near_the_analysis_end(self):
        """Fewer than 12 months left: the forward window is annualized."""
        # The analysis ends 2028-12: a 2028-07 refi has 6 forward months
        cashflow = _make_ramp_cashflow(self.time_grid, lambda i: 50000 if i < 42 else 55000)
        refi_event = {**DSCR_REFI, "trigger_month": "2028-07"}
        result = size_refi_loan(refi_event, cashflow, self.debt)
        # 6 × $55K = $330K → annualized $660K
        self.assertEqual(result["noi_used"], 660000.0)
        self.assertEqual(
            result["noi_window"],
            {"start": "2028-07", "end": "2028-12", "months": 6, "forward": True},
        )
        self.assertEqual(result["sized_commitment"], _dscr_loan(660000))

    def test_deduction_and_noi_windows_match(self):
        """NOI and the below-NOI deduction cover the same months."""
        for trigger in ("2027-01", "2028-07"):
            cashflow = _make_ramp_cashflow(self.time_grid, lambda i: 50000 + 500 * i)
            for row in cashflow:
                row["replacement_reserves"] = 7500.0
            result = size_refi_loan({**DSCR_REFI, "trigger_month": trigger}, cashflow, self.debt)
            window = result["noi_window"]
            self.assertEqual([window["start"], window["end"]], result["deduction_window"])
            self.assertEqual(result["below_noi_deduction"], 90000.0)
            self.assertAlmostEqual(
                result["sizing_cash_flow"], result["noi_used"] - 90000.0, places=2
            )

    def test_ltv_values_the_property_on_forward_noi(self):
        """An LTV-sized refi values the property on the same forward NOI."""
        cashflow = _make_ramp_cashflow(self.time_grid, lambda i: 40000 if i < 24 else 50000)
        for row in cashflow:
            row["replacement_reserves"] = 7500.0
        refi_event = {
            "trigger_month": "2027-01",
            "sizing_method": "ltv",
            "target_ltv": 0.70,
            "exit_cap_for_ltv": 0.05,
            "perm_loan_terms": {"rate": 0.055, "amort_years": 30},
        }
        result = size_refi_loan(refi_event, cashflow, self.debt)
        # Forward NOI $600K / 5% = $12M × 70% = $8.4M (the trailing $480K would give $6.72M)
        self.assertEqual(result["sized_commitment"], 8400000.0)
        self.assertEqual(result["noi_used"], 600000.0)
        self.assertEqual(result["below_noi_deduction"], 0.0)
        self.assertEqual(result["sizing_noi_basis"], "noi_before_capex")
        self.assertEqual(
            result["noi_window"],
            {"start": "2027-01", "end": "2027-12", "months": 12, "forward": True},
        )

    def test_unknown_noi_window_raises(self):
        cashflow = _make_cashflow_by_month(self.time_grid)
        with self.assertRaises(ValueError):
            size_refi_loan({**DSCR_REFI, "noi_window": "sideways"}, cashflow, self.debt)

    def test_schema_accepts_the_window_and_defaults_to_forward(self):
        import json
        from pathlib import Path

        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "engine/schemas/deal_schema_v0_1.json").read_text()
        )
        prop = schema["$defs"]["refi_event"]["properties"]["noi_window"]
        self.assertEqual(prop["enum"], ["forward", "trailing"])
        self.assertEqual(prop["default"], "forward")


class TestDebtServiceTransition(unittest.TestCase):
    """Test that bridge DS stops and perm DS starts cleanly."""

    def test_bridge_terminates_at_trigger(self):
        """Bridge loan should have zero balance after trigger month."""
        time_grid = TimeGrid.build("2025-01", "2030-12")
        bridge_terms = {
            "commitment": 5000000,
            "rate": 0.06,
            "amort_years": 30,
            "io_months": 36,  # 3 years I/O
            "term_months": 24,  # Bridge matures in 24 months
            "skip_maturity_ds": True,
        }
        result = compute_debt(time_grid, bridge_terms)

        # Month 24 (2026-12): bridge payoff happens
        # Months 25+: zero balance and zero DS
        for i in range(24, len(result["by_month"])):
            self.assertAlmostEqual(result["by_month"][i]["ending_balance"], 0, places=2)
            self.assertAlmostEqual(result["by_month"][i]["debt_service"], 0, places=2)

    def test_perm_starts_at_trigger(self):
        """Perm loan should start drawing at trigger month."""
        time_grid = TimeGrid.build("2025-01", "2030-12")
        perm_terms = {
            "commitment": 8000000,
            "rate": 0.055,
            "amort_years": 30,
            "io_months": 12,
            "loan_start_month": "2027-01",
        }
        result = compute_debt(time_grid, perm_terms)

        # Before 2027-01: no balance, no DS
        for row in result["by_month"]:
            if row["month"] < "2027-01":
                self.assertAlmostEqual(row["ending_balance"], 0, places=2)
                self.assertAlmostEqual(row["debt_service"], 0, places=2)

        # At and after 2027-01: balance = $8M, DS > 0
        for row in result["by_month"]:
            if row["month"] >= "2027-01":
                self.assertGreater(row["ending_balance"], 0)

    def test_no_gap_no_overlap(self):
        """Bridge payoff month and perm start month should be adjacent.
        There should be exactly one month where bridge ends and perm begins —
        no multi-month gap in debt service coverage."""
        time_grid = TimeGrid.build("2025-01", "2030-12")

        # Bridge: matures at month 24 (2026-12), payoff zeroes balance
        bridge_terms = {
            "commitment": 5000000,
            "rate": 0.06,
            "amort_years": 30,
            "io_months": 36,
            "term_months": 24,
            "skip_maturity_ds": True,
        }
        bridge_result = compute_debt(time_grid, bridge_terms)

        # Perm: starts at 2027-01 (month 25, immediately after bridge)
        perm_terms = {
            "commitment": 8000000,
            "rate": 0.055,
            "amort_years": 30,
            "io_months": 12,
            "loan_start_month": "2027-01",
        }
        perm_result = compute_debt(time_grid, perm_terms)

        # Bridge has positive balance through 2026-11, pays off at 2026-12
        # Perm has positive balance from 2027-01 onward
        # The only month with zero combined balance is 2026-12 (payoff month)
        zero_months = []
        for i in range(len(time_grid.month_ids)):
            combined = (
                bridge_result["by_month"][i]["ending_balance"]
                + perm_result["by_month"][i]["ending_balance"]
            )
            if combined == 0:
                zero_months.append(time_grid.month_ids[i])

        # At most the bridge payoff month should be the gap
        self.assertLessEqual(len(zero_months), 1, f"Too many gap months: {zero_months}")
        if zero_months:
            self.assertEqual(zero_months[0], "2026-12")


class TestEngineIntegration(unittest.TestCase):
    """Test refi_event through the full engine pipeline."""

    def _make_minimal_inputs(self, refi_event=None):
        """Build minimal valid engine inputs with optional refi_event."""
        inputs = {
            "schema_version": "0.1",
            "metadata": {
                "deal_id": "test-refi",
                "run_id": "r1",
                "as_of_date": "2025-01-01",
                "analyst": "Test",
                "purpose": "testing",
            },
            "time_grid": {
                "analysis_start_date": "2025-01",
                "analysis_end_date": "2030-12",
            },
            "unit_cohorts": [
                {
                    "cohort_id": "A",
                    "unit_type": "1BR",
                    "unit_count": 100,
                    "initial_inplace_rent": 1200,
                },
            ],
            "market_rent_curve": [
                {
                    "cohort_id": "A",
                    "start_period": "2025-01",
                    "end_period": "2030-12",
                    "market_rent": 1250,
                },
            ],
            "loss_to_lease": [
                {
                    "cohort_id": "A",
                    "start_period": "2025-01",
                    "end_period": "2030-12",
                    "ltl_percent": 0.02,
                },
            ],
            "physical_vacancy_curve": [
                {
                    "cohort_id": "A",
                    "start_period": "2025-01",
                    "end_period": "2030-12",
                    "vacancy_rate": 0.05,
                },
            ],
            "collection_loss_curve": [
                {
                    "applies_to": "ALL",
                    "start_period": "2025-01",
                    "end_period": "2030-12",
                    "loss_rate": 0.02,
                },
            ],
            "revenue_programs": [],
            "program_adoption_curve": [],
            "debt_terms": {
                "commitment": 8000000,
                "rate": 0.06,
                "amort_years": 30,
                "io_months": 36,
                "term_months": 24,
            },
            "purchase_assumptions": {
                "purchase_price": 12000000,
                "equity_contribution": 4000000,
                "closing_costs": 200000,
                "total_equity_basis": 4200000,
                "total_unlevered_basis": 12200000,
            },
            "exit_assumptions": {
                "exit_cap_rate": 0.05,
                "exit_month": "2030-06",
                "sale_cost_percent": 0.02,
            },
        }
        if refi_event:
            inputs["refi_event"] = refi_event
        return inputs

    def test_no_refi_event_unchanged(self):
        """Without refi_event, engine behaves exactly as before."""
        from engine.engine import run_underwriting

        inputs = self._make_minimal_inputs()
        result = run_underwriting(with_synthetic_tax_policy(inputs), skip_validation=True)
        self.assertNotIn("refi", result)

    def test_with_refi_event_produces_refi_section(self):
        """With refi_event, engine output includes refi details."""
        from engine.engine import run_underwriting

        refi_event = {
            "trigger_month": "2027-01",
            "sizing_method": "dscr",
            "target_dscr": 1.25,
            "perm_loan_terms": {"rate": 0.055, "amort_years": 30, "io_months": 12},
        }
        inputs = self._make_minimal_inputs(refi_event)
        result = run_underwriting(with_synthetic_tax_policy(inputs), skip_validation=True)

        self.assertIn("refi", result)
        refi = result["refi"]
        self.assertIn("sized_commitment", refi)
        self.assertIn("refi_proceeds", refi)
        self.assertIn("bridge_payoff", refi)
        self.assertIn("stabilized_noi", refi)
        self.assertGreater(float(refi["sized_commitment"]), 0)
        self.assertEqual(
            refi["noi_window"],
            {"start": "2027-01", "end": "2027-12", "months": 12, "forward": True},
        )
        self.assertEqual(
            [refi["noi_window"]["start"], refi["noi_window"]["end"]], refi["deduction_window"]
        )

    def test_refi_cashflow_has_no_gap(self):
        """After refi, leveraged CF should be non-zero in operating months."""
        from engine.engine import run_underwriting

        refi_event = {
            "trigger_month": "2027-01",
            "sizing_method": "dscr",
            "target_dscr": 1.25,
            "perm_loan_terms": {"rate": 0.055, "amort_years": 30, "io_months": 12},
        }
        inputs = self._make_minimal_inputs(refi_event)
        result = run_underwriting(with_synthetic_tax_policy(inputs), skip_validation=True)

        # Every month should have some cashflow activity
        for m in result["cashflow"]["by_month"]:
            # At minimum, there should be revenue (even if LCF goes negative due to DS)
            self.assertNotEqual(m.get("net_rent", 0), 0, f"No revenue at {m['month']}")


class TestRefiEventValidation(unittest.TestCase):
    """Test edge cases and validation."""

    def test_trigger_month_not_found_raises(self):
        """Invalid trigger month should raise ValueError."""
        time_grid = TimeGrid.build("2025-01", "2026-12")
        cashflow = _make_cashflow_by_month(time_grid)
        debt = _make_debt_by_month(time_grid)

        refi_event = {
            "trigger_month": "2030-01",  # Beyond analysis period
            "sizing_method": "dscr",
            "target_dscr": 1.25,
            "perm_loan_terms": {"rate": 0.055, "amort_years": 30},
        }
        with self.assertRaises(ValueError):
            size_refi_loan(refi_event, cashflow, debt)

    def test_unknown_sizing_method_raises(self):
        """Unknown sizing method should raise ValueError."""
        time_grid = TimeGrid.build("2025-01", "2028-12")
        cashflow = _make_cashflow_by_month(time_grid)
        debt = _make_debt_by_month(time_grid)

        refi_event = {
            "trigger_month": "2027-01",
            "sizing_method": "magic",
            "perm_loan_terms": {"rate": 0.055, "amort_years": 30},
        }
        with self.assertRaises(ValueError):
            size_refi_loan(refi_event, cashflow, debt)

    def test_perm_terms_passthrough(self):
        """Variable rate and other perm terms should pass through to output."""
        time_grid = TimeGrid.build("2025-01", "2028-12")
        cashflow = _make_cashflow_by_month(time_grid)
        debt = _make_debt_by_month(time_grid)

        refi_event = {
            "trigger_month": "2027-01",
            "sizing_method": "dscr",
            "target_dscr": 1.25,
            "perm_loan_terms": {
                "rate": 0.055,
                "amort_years": 30,
                "io_months": 24,
                "term_months": 120,
                "rate_type": "variable",
                "base_spread": 0.02,
                "rate_cap": 0.08,
            },
        }
        result = size_refi_loan(refi_event, cashflow, debt)
        perm = result["perm_loan_terms"]
        self.assertEqual(perm["io_months"], 24)
        self.assertEqual(perm["term_months"], 120)
        self.assertEqual(perm["rate_type"], "variable")
        self.assertAlmostEqual(perm["base_spread"], 0.02)
        self.assertAlmostEqual(perm["rate_cap"], 0.08)
        self.assertEqual(perm["loan_start_month"], "2027-01")


if __name__ == "__main__":
    unittest.main()
