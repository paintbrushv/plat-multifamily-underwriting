"""
Tests for Capital Stack / Mezzanine / Preferred Equity (Task 1.4).

Tests cover:
- Preferred equity return computation
- Capital stack normalization (debt_terms → capital_stack)
- Multi-layer debt processing (senior + mezz + pref equity)
- Payment priority and DSCR computation
- Backward compatibility (debt_terms-only unchanged)
- Schema validation
"""
import json
import unittest
from decimal import Decimal
from pathlib import Path

from engine.modules.debt import compute_debt, compute_preferred_equity
from engine.modules.time_grid import TimeGrid
from engine.modules.util import dec, round2


def _tg(start="2026-01", end="2026-12"):
    """Build a time grid for testing."""
    return TimeGrid.build(start, end)


class TestPreferredEquity(unittest.TestCase):
    """Test preferred equity return computation."""

    def test_basic_pref_return(self):
        """Preferred equity accrues monthly return at annual rate."""
        tg = _tg("2026-01", "2026-12")
        result = compute_preferred_equity(tg, {
            "commitment": 2_000_000,
            "pref_return_rate": 0.12,
            "rate": 0.12,
        })
        by_month = result["by_month"]
        # First month: $2M × 1% = $20,000
        self.assertAlmostEqual(by_month[0]["debt_service"], 20_000, places=0)
        self.assertAlmostEqual(by_month[0]["ending_balance"], 2_000_000, places=0)

    def test_pref_return_with_term(self):
        """Preferred equity redeemed at term maturity."""
        tg = _tg("2026-01", "2028-12")
        result = compute_preferred_equity(tg, {
            "commitment": 1_000_000,
            "pref_return_rate": 0.10,
            "rate": 0.10,
            "term_months": 24,
        })
        by_month = result["by_month"]
        # Month 23 (0-indexed) = maturity: loan_payoff = balance
        self.assertAlmostEqual(by_month[23]["loan_payoff"], 1_000_000, places=0)
        # Post-maturity: zero balance and zero return
        self.assertAlmostEqual(by_month[24]["debt_service"], 0, places=0)
        self.assertAlmostEqual(by_month[24]["ending_balance"], 0, places=0)

    def test_pref_return_zero_before_start(self):
        """No pref return before loan start month."""
        tg = _tg("2026-01", "2027-12")
        result = compute_preferred_equity(tg, {
            "commitment": 1_000_000,
            "pref_return_rate": 0.12,
            "rate": 0.12,
            "loan_start_month": "2026-07",
        })
        by_month = result["by_month"]
        # Months 0-5 (Jan-Jun): no balance, no return
        for i in range(6):
            self.assertAlmostEqual(by_month[i]["debt_service"], 0, places=0)
        # Month 6 (Jul): balance drawn, return starts
        self.assertAlmostEqual(by_month[6]["debt_service"], 10_000, places=0)

    def test_summary_fields(self):
        """Summary reports total commitment and return."""
        tg = _tg("2026-01", "2026-12")
        result = compute_preferred_equity(tg, {
            "commitment": 5_000_000,
            "pref_return_rate": 0.08,
            "rate": 0.08,
        })
        summary = result["summary"]
        self.assertAlmostEqual(summary["total_commitment"], 5_000_000, places=0)
        self.assertAlmostEqual(summary["weighted_avg_rate"], 0.08, places=4)
        # 12 months × $5M × 8%/12 = $400,000
        self.assertAlmostEqual(summary["total_interest_paid"], 400_000, delta=100)


class TestCapitalStackNormalization(unittest.TestCase):
    """Test _normalize_capital_stack()."""

    def test_debt_terms_wraps_as_senior(self):
        from engine.engine import _normalize_capital_stack
        inputs = {"debt_terms": {"commitment": 10_000_000, "rate": 0.05, "amort_years": 30}}
        stack = _normalize_capital_stack(inputs)
        self.assertEqual(len(stack), 1)
        self.assertEqual(stack[0]["layer_type"], "senior")
        self.assertEqual(stack[0]["priority"], 1)

    def test_debt_terms_with_additional(self):
        from engine.engine import _normalize_capital_stack
        inputs = {
            "debt_terms": {"commitment": 10_000_000, "rate": 0.05, "amort_years": 30},
            "additional_debt_terms": [{"commitment": 2_000_000, "rate": 0.06, "amort_years": 25}],
        }
        stack = _normalize_capital_stack(inputs)
        self.assertEqual(len(stack), 2)

    def test_explicit_capital_stack_sorted(self):
        from engine.engine import _normalize_capital_stack
        inputs = {
            "capital_stack": [
                {"layer_type": "mezzanine", "priority": 2, "commitment": 2_000_000, "rate": 0.10, "amort_years": 30},
                {"layer_type": "senior", "priority": 1, "commitment": 10_000_000, "rate": 0.05, "amort_years": 30},
            ],
        }
        stack = _normalize_capital_stack(inputs)
        self.assertEqual(stack[0]["layer_type"], "senior")
        self.assertEqual(stack[1]["layer_type"], "mezzanine")

    def test_no_debt_returns_empty(self):
        from engine.engine import _normalize_capital_stack
        self.assertEqual(_normalize_capital_stack({}), [])


class TestCapitalStackEngine(unittest.TestCase):
    """Integration tests: capital stack through the engine.

    Uses the minimal_deal_inputs fixture pattern from conftest.py.
    """

    def _base_inputs(self):
        """Build schema-compliant inputs with capital_stack."""
        start = "2026-07"
        end = "2031-06"
        return {
            "schema_version": "0.1",
            "metadata": {
                "deal_id": "TEST-STACK",
                "run_id": "test-stack-001",
                "as_of_date": "2026-04-16",
                "analyst": "Test",
                "purpose": "Capital stack test",
            },
            "time_grid": {"analysis_start_date": start, "analysis_end_date": end},
            "unit_cohorts": [
                {"cohort_id": "1BR", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1200},
            ],
            "market_rent_curve": [
                {"cohort_id": "1BR", "start_period": start, "end_period": end, "market_rent": 1300},
            ],
            "loss_to_lease": [
                {"cohort_id": "1BR", "start_period": start, "end_period": end, "ltl_percent": 0.03},
            ],
            "physical_vacancy_curve": [
                {"cohort_id": "1BR", "start_period": start, "end_period": end, "vacancy_rate": 0.05},
            ],
            "collection_loss_curve": [
                {"applies_to": "ALL", "start_period": start, "end_period": end, "loss_rate": 0.01},
            ],
            "revenue_programs": [],
            "program_adoption_curve": [],
            "opex_table": [
                {"category_name": "Total", "calculation_type": "fixed_annual",
                 "base_value": 600_000, "growth_rate": 0.03, "recoverable_flag": False},
            ],
            "growth_assumptions": {"growth_type": "annual_compound", "annual_growth_rate": 0.03},
            "purchase_assumptions": {
                "purchase_price": 15_000_000,
                "closing_costs": 300_000,
                "equity_contribution": 3_500_000,
                "total_equity_basis": 3_800_000,
            },
            "exit_assumptions": {
                "exit_cap_rate": 0.055,
                "sale_cost_percent": 0.02,
                "exit_month": end,
            },
            "capital_stack": [
                {
                    "layer_type": "senior",
                    "priority": 1,
                    "label": "Senior Bridge",
                    "commitment": 10_000_000,
                    "rate": 0.055,
                    "amort_years": 30,
                    "io_months": 24,
                },
                {
                    "layer_type": "mezzanine",
                    "priority": 2,
                    "label": "Mezzanine Loan",
                    "commitment": 2_000_000,
                    "rate": 0.10,
                    "amort_years": 30,
                    "io_months": 60,
                },
                {
                    "layer_type": "preferred_equity",
                    "priority": 3,
                    "label": "LP Preferred",
                    "commitment": 1_500_000,
                    "rate": 0.12,
                    "pref_return_rate": 0.12,
                },
            ],
        }

    def test_senior_only_backward_compat(self):
        """Engine with debt_terms (no capital_stack) unchanged."""
        from engine.engine import run_underwriting

        inputs = self._base_inputs()
        del inputs["capital_stack"]
        inputs["debt_terms"] = {
            "commitment": 10_000_000,
            "rate": 0.055,
            "amort_years": 30,
            "io_months": 24,
        }
        result = run_underwriting(inputs)
        self.assertIn("debt", result)
        self.assertIn("cashflow", result)
        self.assertNotIn("capital_stack", result)

    def test_three_layer_stack(self):
        """3-layer capital stack produces correct output structure."""
        from engine.engine import run_underwriting

        result = run_underwriting(self._base_inputs())
        self.assertIn("capital_stack", result)
        cs = result["capital_stack"]
        self.assertEqual(len(cs["layers"]), 3)
        self.assertEqual(cs["layers"][0]["layer_type"], "senior")
        self.assertEqual(cs["layers"][1]["layer_type"], "mezzanine")
        self.assertEqual(cs["layers"][2]["layer_type"], "preferred_equity")

    def test_combined_dscr_lower_than_senior(self):
        """Combined DSCR (senior + mezz) < senior-only DSCR."""
        from engine.engine import run_underwriting

        result = run_underwriting(self._base_inputs())
        cs = result["capital_stack"]
        if cs["senior_dscr"] is not None and cs["combined_dscr"] is not None:
            self.assertGreater(cs["senior_dscr"], cs["combined_dscr"])

    def test_debt_service_includes_all_layers(self):
        """Total debt service in cashflow includes all layers."""
        from engine.engine import run_underwriting

        result = run_underwriting(self._base_inputs())
        # Sum all years for full-period comparison
        total_ds = sum(yr["debt_service"] for yr in result["cashflow"]["by_year"])
        # 5 years (60 months):
        # Senior IO (24mo) then amort: ~$550K/yr IO + ~$680K/yr P+I ≈ ~$3.1-3.2M
        # Mezz IO (60mo): $200K/yr × 5 = $1M
        # Pref equity: $180K/yr × 5 = $900K
        # Total ≈ $5-5.2M range
        self.assertGreater(total_ds, 4_500_000)
        self.assertLess(total_ds, 6_000_000)

    def test_senior_plus_mezz(self):
        """Two-layer stack (senior + mezz) without pref equity."""
        from engine.engine import run_underwriting

        inputs = self._base_inputs()
        inputs["capital_stack"] = [
            {"layer_type": "senior", "priority": 1, "commitment": 10_000_000,
             "rate": 0.05, "amort_years": 30, "io_months": 12},
            {"layer_type": "mezzanine", "priority": 2, "commitment": 3_000_000,
             "rate": 0.12, "amort_years": 25, "io_months": 60},
        ]
        result = run_underwriting(inputs)
        cs = result["capital_stack"]
        self.assertEqual(len(cs["layers"]), 2)
        self.assertIsNotNone(cs["combined_dscr"])


class TestPaymentPriority(unittest.TestCase):
    """Test that layers are processed in priority order."""

    def test_priority_ordering(self):
        from engine.engine import _normalize_capital_stack
        inputs = {
            "capital_stack": [
                {"layer_type": "preferred_equity", "priority": 3, "commitment": 1_000_000, "rate": 0.12},
                {"layer_type": "senior", "priority": 1, "commitment": 10_000_000, "rate": 0.05, "amort_years": 30},
                {"layer_type": "mezzanine", "priority": 2, "commitment": 2_000_000, "rate": 0.10, "amort_years": 30},
            ],
        }
        stack = _normalize_capital_stack(inputs)
        types = [l["layer_type"] for l in stack]
        self.assertEqual(types, ["senior", "mezzanine", "preferred_equity"])


class TestSchemaValidation(unittest.TestCase):
    """Verify schema accepts capital_stack."""

    def test_schema_has_capital_stack(self):
        schema = json.loads(Path("engine/schemas/deal_schema_v0_1.json").read_text())
        self.assertIn("capital_stack", schema["properties"])
        self.assertIn("capital_stack_layer", schema["$defs"])
        layer_props = schema["$defs"]["capital_stack_layer"]["properties"]
        self.assertIn("layer_type", layer_props)
        self.assertIn("priority", layer_props)
        self.assertIn("pref_return_rate", layer_props)

    def test_layer_types_enum(self):
        schema = json.loads(Path("engine/schemas/deal_schema_v0_1.json").read_text())
        layer_type = schema["$defs"]["capital_stack_layer"]["properties"]["layer_type"]
        self.assertEqual(set(layer_type["enum"]), {"senior", "mezzanine", "preferred_equity"})


if __name__ == "__main__":
    unittest.main()
