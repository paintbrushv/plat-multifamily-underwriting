"""Tests for Task 1.6: Concession Burn-Off and Lease Trade-Out.

Covers: free_months, fixed_dollar, pct_rent concession types,
burn-off timing, cohort targeting, stacking, trade-out spread,
engine integration, backward compatibility, and schema validation.
"""
import json
import unittest
from decimal import Decimal
from pathlib import Path

from engine.modules.revenue import compute_base_rent
from engine.modules.time_grid import TimeGrid
from engine.modules.util import dec


def _tg(start="2026-01", end="2026-12"):
    return TimeGrid.build(start, end)


def _basic_inputs(start="2026-01", end="2026-12"):
    """Minimal revenue inputs: 10 units at $1200 market, 10% LTL, 20% vacancy."""
    return {
        "unit_cohorts": [
            {"cohort_id": "1BR", "unit_type": "1BR", "unit_count": 10, "initial_inplace_rent": 1080},
        ],
        "market_rent_curve": [
            {"cohort_id": "1BR", "start_period": start, "end_period": end, "market_rent": 1200},
        ],
        "loss_to_lease": [
            {"cohort_id": "1BR", "start_period": start, "end_period": end, "ltl_percent": 0.10},
        ],
        "physical_vacancy_curve": [
            {"cohort_id": "1BR", "start_period": start, "end_period": end, "vacancy_rate": 0.20},
        ],
        "collection_loss_curve": [
            {"applies_to": "ALL", "start_period": start, "end_period": end, "loss_rate": 0.05},
        ],
    }


def _two_cohort_inputs(start="2026-01", end="2026-12"):
    """Two cohorts: 1BR (10 units, $1200 market) and 2BR (10 units, $1500 market)."""
    return {
        "unit_cohorts": [
            {"cohort_id": "1BR", "unit_type": "1BR", "unit_count": 10, "initial_inplace_rent": 1080},
            {"cohort_id": "2BR", "unit_type": "2BR", "unit_count": 10, "initial_inplace_rent": 1350},
        ],
        "market_rent_curve": [
            {"cohort_id": "1BR", "start_period": start, "end_period": end, "market_rent": 1200},
            {"cohort_id": "2BR", "start_period": start, "end_period": end, "market_rent": 1500},
        ],
        "loss_to_lease": [
            {"cohort_id": "1BR", "start_period": start, "end_period": end, "ltl_percent": 0.10},
            {"cohort_id": "2BR", "start_period": start, "end_period": end, "ltl_percent": 0.10},
        ],
        "physical_vacancy_curve": [
            {"cohort_id": "1BR", "start_period": start, "end_period": end, "vacancy_rate": 0.20},
            {"cohort_id": "2BR", "start_period": start, "end_period": end, "vacancy_rate": 0.20},
        ],
        "collection_loss_curve": [
            {"applies_to": "ALL", "start_period": start, "end_period": end, "loss_rate": 0.05},
        ],
    }


class TestFreeMonthConcession(unittest.TestCase):
    """Free month concession reduces billed rent by amount/12."""

    def test_one_free_month(self):
        """1 free month on 12-month lease = 8.33% reduction of billed rent."""
        tg = _tg()
        inp = _basic_inputs()
        concessions = [{
            "concession_id": "c1",
            "start_month": "2026-01",
            "end_month": "2026-12",
            "concession_type": "free_months",
            "amount": 1.0,
            "applies_to_cohort": "ALL",
        }]

        result = compute_base_rent(
            tg, inp["unit_cohorts"], inp["market_rent_curve"],
            inp["loss_to_lease"], inp["physical_vacancy_curve"],
            inp["collection_loss_curve"], concession_schedule=concessions,
        )

        # Without concessions: billed = 1080 * 8 = 8640, net = 8640 * 0.95 = 8208
        # With 1 free month: concession = 8640 * (1/12) = 720
        # billed_after_concessions = 8640 - 720 = 7920
        # net = 7920 * 0.95 = 7524
        m1 = result["by_month"][0]
        self.assertAlmostEqual(float(m1["concession_amount"]), 720.0, delta=1)
        self.assertAlmostEqual(float(m1["billed_after_concessions"]), 7920.0, delta=1)
        self.assertAlmostEqual(float(m1["net_rent"]), 7524.0, delta=1)

    def test_two_free_months(self):
        """2 free months = 16.67% reduction."""
        tg = _tg()
        inp = _basic_inputs()
        concessions = [{
            "concession_id": "c1",
            "start_month": "2026-01",
            "end_month": "2026-12",
            "concession_type": "free_months",
            "amount": 2.0,
            "applies_to_cohort": "ALL",
        }]

        result = compute_base_rent(
            tg, inp["unit_cohorts"], inp["market_rent_curve"],
            inp["loss_to_lease"], inp["physical_vacancy_curve"],
            inp["collection_loss_curve"], concession_schedule=concessions,
        )

        # concession = 8640 * (2/12) = 1440
        m1 = result["by_month"][0]
        self.assertAlmostEqual(float(m1["concession_amount"]), 1440.0, delta=1)


class TestFixedDollarConcession(unittest.TestCase):
    """Fixed dollar concession reduces rent by $/occupied_unit/month."""

    def test_fixed_200_per_unit(self):
        """$200/unit/month concession on 8 occupied units = $1600 reduction."""
        tg = _tg()
        inp = _basic_inputs()
        concessions = [{
            "concession_id": "c1",
            "start_month": "2026-01",
            "end_month": "2026-12",
            "concession_type": "fixed_dollar",
            "amount": 200,
            "applies_to_cohort": "ALL",
        }]

        result = compute_base_rent(
            tg, inp["unit_cohorts"], inp["market_rent_curve"],
            inp["loss_to_lease"], inp["physical_vacancy_curve"],
            inp["collection_loss_curve"], concession_schedule=concessions,
        )

        # 10 units * 0.80 occupancy = 8 occupied units
        # concession = 200 * 8 = 1600
        m1 = result["by_month"][0]
        self.assertAlmostEqual(float(m1["concession_amount"]), 1600.0, delta=1)
        # billed_after_concessions = 8640 - 1600 = 7040
        self.assertAlmostEqual(float(m1["billed_after_concessions"]), 7040.0, delta=1)


class TestPctRentConcession(unittest.TestCase):
    """Percentage concession reduces billed rent by a fraction."""

    def test_five_percent(self):
        """5% rent concession on $8640 billed = $432 reduction."""
        tg = _tg()
        inp = _basic_inputs()
        concessions = [{
            "concession_id": "c1",
            "start_month": "2026-01",
            "end_month": "2026-12",
            "concession_type": "pct_rent",
            "amount": 0.05,
            "applies_to_cohort": "ALL",
        }]

        result = compute_base_rent(
            tg, inp["unit_cohorts"], inp["market_rent_curve"],
            inp["loss_to_lease"], inp["physical_vacancy_curve"],
            inp["collection_loss_curve"], concession_schedule=concessions,
        )

        m1 = result["by_month"][0]
        self.assertAlmostEqual(float(m1["concession_amount"]), 432.0, delta=1)


class TestBurnOffTiming(unittest.TestCase):
    """Concessions are only active during their schedule period."""

    def test_concession_active_then_expires(self):
        """Concession active Jan-Jun, no concession Jul-Dec."""
        tg = _tg()
        inp = _basic_inputs()
        concessions = [{
            "concession_id": "c1",
            "start_month": "2026-01",
            "end_month": "2026-06",
            "concession_type": "pct_rent",
            "amount": 0.10,
            "applies_to_cohort": "ALL",
        }]

        result = compute_base_rent(
            tg, inp["unit_cohorts"], inp["market_rent_curve"],
            inp["loss_to_lease"], inp["physical_vacancy_curve"],
            inp["collection_loss_curve"], concession_schedule=concessions,
        )

        # Jan (active): concession > 0
        jan = result["by_month"][0]
        self.assertGreater(float(jan["concession_amount"]), 0)

        # Jul (expired): concession = 0
        jul = result["by_month"][6]
        self.assertAlmostEqual(float(jul["concession_amount"]), 0.0, places=2)

        # Jul net_rent should be higher than Jan (no concession)
        self.assertGreater(float(jul["net_rent"]), float(jan["net_rent"]))


class TestCohortTargeting(unittest.TestCase):
    """Concessions can target specific cohorts or ALL."""

    def test_single_cohort_only(self):
        """Concession on 1BR only should not affect 2BR revenue."""
        tg = _tg()
        inp = _two_cohort_inputs()
        concessions = [{
            "concession_id": "c1",
            "start_month": "2026-01",
            "end_month": "2026-12",
            "concession_type": "fixed_dollar",
            "amount": 100,
            "applies_to_cohort": "1BR",
        }]

        with_conc = compute_base_rent(
            tg, inp["unit_cohorts"], inp["market_rent_curve"],
            inp["loss_to_lease"], inp["physical_vacancy_curve"],
            inp["collection_loss_curve"], concession_schedule=concessions,
        )
        without_conc = compute_base_rent(
            tg, inp["unit_cohorts"], inp["market_rent_curve"],
            inp["loss_to_lease"], inp["physical_vacancy_curve"],
            inp["collection_loss_curve"],
        )

        # Concession should be $100 * 8 occupied = $800 (1BR only)
        m1_with = with_conc["by_month"][0]
        self.assertAlmostEqual(float(m1_with["concession_amount"]), 800.0, delta=1)

        # Total billed_after_vacancy is the same (concessions don't affect vacancy calc)
        m1_without = without_conc["by_month"][0]
        self.assertAlmostEqual(
            float(m1_with["billed_rent_after_vacancy"]),
            float(m1_without["billed_rent_after_vacancy"]),
            delta=1,
        )

    def test_all_cohort_applies_to_both(self):
        """ALL cohort concession affects both 1BR and 2BR."""
        tg = _tg()
        inp = _two_cohort_inputs()
        concessions = [{
            "concession_id": "c1",
            "start_month": "2026-01",
            "end_month": "2026-12",
            "concession_type": "fixed_dollar",
            "amount": 100,
            "applies_to_cohort": "ALL",
        }]

        result = compute_base_rent(
            tg, inp["unit_cohorts"], inp["market_rent_curve"],
            inp["loss_to_lease"], inp["physical_vacancy_curve"],
            inp["collection_loss_curve"], concession_schedule=concessions,
        )

        # 1BR: 8 occupied * $100 = $800; 2BR: 8 occupied * $100 = $800; total = $1600
        m1 = result["by_month"][0]
        self.assertAlmostEqual(float(m1["concession_amount"]), 1600.0, delta=1)


class TestStackingConcessions(unittest.TestCase):
    """Multiple overlapping concessions stack additively."""

    def test_two_concessions_stack(self):
        """Two 5% concessions = 10% total reduction."""
        tg = _tg()
        inp = _basic_inputs()
        concessions = [
            {
                "concession_id": "c1",
                "start_month": "2026-01",
                "end_month": "2026-12",
                "concession_type": "pct_rent",
                "amount": 0.05,
                "applies_to_cohort": "ALL",
            },
            {
                "concession_id": "c2",
                "start_month": "2026-01",
                "end_month": "2026-12",
                "concession_type": "pct_rent",
                "amount": 0.05,
                "applies_to_cohort": "ALL",
            },
        ]

        result = compute_base_rent(
            tg, inp["unit_cohorts"], inp["market_rent_curve"],
            inp["loss_to_lease"], inp["physical_vacancy_curve"],
            inp["collection_loss_curve"], concession_schedule=concessions,
        )

        # billed = 8640, two 5% concessions = 432 + 432 = 864
        m1 = result["by_month"][0]
        self.assertAlmostEqual(float(m1["concession_amount"]), 864.0, delta=1)


class TestTradeOutSpread(unittest.TestCase):
    """Trade-out spread is a reporting metric computed in the engine."""

    def test_trade_out_calculation(self):
        """50% annual turnover, 3% trade-out = positive spread."""
        from engine.engine import run_underwriting

        inputs = self._base_inputs()
        inputs["trade_out_assumptions"] = {
            "annual_turnover_pct": 0.50,
            "expected_trade_out_pct": 0.03,
        }

        result = run_underwriting(inputs)
        self.assertIn("trade_out", result)
        to = result["trade_out"]
        self.assertIn("by_month", to)
        self.assertEqual(len(to["by_month"]), len(result["time_grid"]["months"]))

        m1 = to["by_month"][0]
        self.assertIn("trade_out_spread", m1)
        self.assertGreater(float(m1["trade_out_spread"]), 0)
        self.assertGreater(float(m1["new_lease_rent"]), float(m1["expiring_rent"]))

    def test_zero_turnover(self):
        """0% turnover = zero trade-out spread."""
        from engine.engine import run_underwriting

        inputs = self._base_inputs()
        inputs["trade_out_assumptions"] = {
            "annual_turnover_pct": 0.0,
            "expected_trade_out_pct": 0.03,
        }

        result = run_underwriting(inputs)
        m1 = result["trade_out"]["by_month"][0]
        self.assertAlmostEqual(float(m1["trade_out_spread"]), 0.0, places=2)

    def _base_inputs(self):
        """Schema-compliant inputs for engine integration."""
        start = "2026-07"
        end = "2031-06"
        return {
            "schema_version": "0.1",
            "metadata": {
                "deal_id": "CONC-001",
                "run_id": "test-001",
                "as_of_date": "2026-04-17",
                "analyst": "Test",
                "purpose": "Concession test",
            },
            "time_grid": {"analysis_start_date": start, "analysis_end_date": end},
            "unit_cohorts": [
                {"cohort_id": "1BR", "unit_type": "1BR", "unit_count": 50, "initial_inplace_rent": 1200},
                {"cohort_id": "2BR", "unit_type": "2BR", "unit_count": 50, "initial_inplace_rent": 1500},
            ],
            "market_rent_curve": [
                {"cohort_id": "1BR", "start_period": start, "end_period": end, "market_rent": 1250},
                {"cohort_id": "2BR", "start_period": start, "end_period": end, "market_rent": 1550},
            ],
            "loss_to_lease": [
                {"cohort_id": "1BR", "start_period": start, "end_period": end, "ltl_percent": 0.03},
                {"cohort_id": "2BR", "start_period": start, "end_period": end, "ltl_percent": 0.03},
            ],
            "physical_vacancy_curve": [
                {"cohort_id": "1BR", "start_period": start, "end_period": end, "vacancy_rate": 0.05},
                {"cohort_id": "2BR", "start_period": start, "end_period": end, "vacancy_rate": 0.05},
            ],
            "collection_loss_curve": [
                {"applies_to": "ALL", "start_period": start, "end_period": end, "loss_rate": 0.01},
            ],
            "revenue_programs": [
                {
                    "program_id": "RUBS",
                    "program_name": "RUBS",
                    "program_type": "recovery",
                    "pricing_type": "$/unit",
                    "price_value": 50,
                    "eligible_units": "ALL",
                    "start_period": start,
                },
            ],
            "program_adoption_curve": [
                {"program_id": "RUBS", "start_period": start, "end_period": end, "adoption_rate": 0.90},
            ],
            "opex_table": [
                {"category_name": "R&M", "calculation_type": "per_unit", "base_value": 750, "growth_rate": 0.03, "recoverable_flag": False},
                {"category_name": "Management Fee", "calculation_type": "percent_egr", "base_value": 0.04, "growth_rate": 0.0, "recoverable_flag": False},
            ],
            "purchase_assumptions": {
                "purchase_price": 13_500_000,
                "closing_costs": 150_000,
                "equity_contribution": 4_725_000,
                "total_equity_basis": 4_875_000,
            },
            "debt_terms": {
                "commitment": 8_775_000,
                "rate": 0.0575,
                "amort_years": 30,
                "io_months": 12,
            },
            "exit_assumptions": {
                "exit_cap_rate": 0.055,
                "sale_cost_percent": 0.02,
                "exit_month": end,
            },
            "fund_assumptions": {
                "sponsor_equity_pct": 0.05,
                "lp_equity_pct": 0.95,
                "preferred_return": 0.08,
                "acquisition_fee_pct": 0.01,
                "asset_management_fee_pct": 0.01,
                "disposition_fee_pct": 0.01,
                "annual_partnership_expenses": 25000,
                "partnership_closing_costs": 50000,
                "promote_splits": [
                    {"tier": "Pref", "hurdle_irr": 0.08, "lp_share": 1.0, "gp_share": 0.0},
                    {"tier": "Promote", "hurdle_irr": 0.0, "lp_share": 0.70, "gp_share": 0.30},
                ],
            },
            "growth_assumptions": {"growth_type": "annual_compound", "annual_growth_rate": 0.03},
            "replacement_reserves": [
                {"start_period": start, "end_period": end, "annual_amount": 25000},
            ],
        }


class TestEngineIntegration(unittest.TestCase):
    """Full engine run with concessions verifies cashflow impact."""

    def test_concession_reduces_cashflow(self):
        """Concessions reduce net_rent and flow through to NOI."""
        from engine.engine import run_underwriting

        inputs = TestTradeOutSpread._base_inputs(TestTradeOutSpread())
        inputs_no_conc = json.loads(json.dumps(inputs))

        inputs["concession_schedule"] = [{
            "concession_id": "lease_up",
            "start_month": "2026-07",
            "end_month": "2027-06",
            "concession_type": "free_months",
            "amount": 1.0,
            "applies_to_cohort": "ALL",
        }]

        result_with = run_underwriting(inputs)
        result_without = run_underwriting(inputs_no_conc)

        # Year 1 NOI should be lower with concessions
        yr1_with = result_with["cashflow"]["by_year"][0]
        yr1_without = result_without["cashflow"]["by_year"][0]
        self.assertLess(
            float(yr1_with["net_operating_income"]),
            float(yr1_without["net_operating_income"]),
        )

        # Year 3 (2028) should be identical — concession ended 2027-06,
        # and by_year uses calendar years, so 2028 is fully clean.
        yr3_with = result_with["cashflow"]["by_year"][2]
        yr3_without = result_without["cashflow"]["by_year"][2]
        self.assertAlmostEqual(
            float(yr3_with["net_operating_income"]),
            float(yr3_without["net_operating_income"]),
            delta=100,  # small tolerance for mgmt fee % EGR feedback
        )

    def test_concession_fields_in_revenue_output(self):
        """Revenue output includes concession_amount and billed_after_concessions."""
        from engine.engine import run_underwriting

        inputs = TestTradeOutSpread._base_inputs(TestTradeOutSpread())
        inputs["concession_schedule"] = [{
            "concession_id": "c1",
            "start_month": "2026-07",
            "end_month": "2026-12",
            "concession_type": "pct_rent",
            "amount": 0.05,
            "applies_to_cohort": "ALL",
        }]

        result = run_underwriting(inputs)
        rent_m1 = result["revenue"]["base_rent"]["by_month"][0]
        self.assertIn("concession_amount", rent_m1)
        self.assertIn("billed_after_concessions", rent_m1)
        self.assertGreater(float(rent_m1["concession_amount"]), 0)


class TestBackwardCompat(unittest.TestCase):
    """No concession_schedule = identical output to pre-1.6."""

    def test_no_concessions_zero_deduction(self):
        """Without concessions, concession_amount is 0 and net_rent unchanged."""
        tg = _tg()
        inp = _basic_inputs()

        result = compute_base_rent(
            tg, inp["unit_cohorts"], inp["market_rent_curve"],
            inp["loss_to_lease"], inp["physical_vacancy_curve"],
            inp["collection_loss_curve"],
        )

        m1 = result["by_month"][0]
        self.assertAlmostEqual(float(m1["concession_amount"]), 0.0, places=2)
        self.assertAlmostEqual(float(m1["billed_after_concessions"]), float(m1["billed_rent_after_vacancy"]), places=2)
        # Original net_rent: 8640 * 0.95 = 8208
        self.assertAlmostEqual(float(m1["net_rent"]), 8208.0, delta=1)

    def test_no_trade_out_in_result(self):
        """Without trade_out_assumptions, no trade_out key in results."""
        from engine.engine import run_underwriting

        inputs = TestTradeOutSpread._base_inputs(TestTradeOutSpread())
        result = run_underwriting(inputs)
        self.assertNotIn("trade_out", result)


class TestSchemaValidation(unittest.TestCase):
    """Schema has concession and trade-out fields."""

    def test_concession_entry_schema(self):
        """concession_entry definition exists with correct fields."""
        schema = json.loads(Path("engine/schemas/deal_schema_v0_1.json").read_text())
        defs = schema["$defs"]
        self.assertIn("concession_entry", defs)
        props = defs["concession_entry"]["properties"]
        self.assertIn("concession_id", props)
        self.assertIn("start_month", props)
        self.assertIn("end_month", props)
        self.assertIn("concession_type", props)
        self.assertIn("amount", props)
        self.assertIn("applies_to_cohort", props)
        # Check enum values
        ct = props["concession_type"]
        self.assertEqual(set(ct["enum"]), {"free_months", "fixed_dollar", "pct_rent"})

    def test_trade_out_assumptions_schema(self):
        """trade_out_assumptions definition exists with correct fields."""
        schema = json.loads(Path("engine/schemas/deal_schema_v0_1.json").read_text())
        defs = schema["$defs"]
        self.assertIn("trade_out_assumptions", defs)
        props = defs["trade_out_assumptions"]["properties"]
        self.assertIn("annual_turnover_pct", props)
        self.assertIn("expected_trade_out_pct", props)

    def test_top_level_properties(self):
        """concession_schedule and trade_out_assumptions are top-level properties."""
        schema = json.loads(Path("engine/schemas/deal_schema_v0_1.json").read_text())
        props = schema["properties"]
        self.assertIn("concession_schedule", props)
        self.assertIn("trade_out_assumptions", props)


if __name__ == "__main__":
    unittest.main()
