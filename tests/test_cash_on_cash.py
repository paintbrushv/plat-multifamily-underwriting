"""
Tests for compute_cash_on_cash (V1.5).

Covers:
  - Year-1 CoC formula: (leveraged cash flow - AM fees - partnership exp) / equity
  - 7% sanity flag (below = True)
  - Fund_by_year integration (AM + partnership expenses)
  - Backward-compatible fallback when leveraged_cash_flow is absent
  - Edge cases: zero equity, empty cashflow
  - Provenance shape via build_provenance
"""
from __future__ import annotations

import unittest

from engine.modules.metrics import compute_cash_on_cash


class TestCashOnCashSimple(unittest.TestCase):
    def test_basic_coc_no_fund_block(self):
        # Leveraged CF 1.0M, equity 10M → CoC 10%
        cashflow_by_year = [
            {
                "year": "2026",
                "net_operating_income": 1_000_000.0,
                "replacement_reserves": 0.0,
                "total_capex": 0.0,
                "debt_service": 0.0,
                "leveraged_cash_flow": 1_000_000.0,
            }
        ]
        result = compute_cash_on_cash(cashflow_by_year, equity_basis=10_000_000)
        self.assertAlmostEqual(result["cash_on_cash_year_1"], 0.10, places=4)
        self.assertEqual(result["free_cf_year_1"], 1_000_000.0)
        self.assertFalse(result["coc_below_target"])
        self.assertFalse(result["coc_below_target_7pct"])

    def test_below_7pct_sets_flag(self):
        cashflow_by_year = [{
            "year": "2026", "net_operating_income": 500_000,
            "replacement_reserves": 0, "total_capex": 0, "debt_service": 0,
            "leveraged_cash_flow": 500_000,
        }]
        result = compute_cash_on_cash(cashflow_by_year, equity_basis=10_000_000)
        # 500k / 10M = 5% < 7%
        self.assertEqual(result["cash_on_cash_year_1"], 0.05)
        self.assertTrue(result["coc_below_target"])
        self.assertTrue(result["coc_below_target_7pct"])

    def test_at_7pct_threshold_not_flagged(self):
        # At exactly 7%, NOT below; flag = False
        cashflow_by_year = [{
            "year": "2026", "net_operating_income": 700_000,
            "replacement_reserves": 0, "total_capex": 0, "debt_service": 0,
            "leveraged_cash_flow": 700_000,
        }]
        result = compute_cash_on_cash(cashflow_by_year, equity_basis=10_000_000)
        self.assertEqual(result["cash_on_cash_year_1"], 0.07)
        self.assertFalse(result["coc_below_target"])
        self.assertFalse(result["coc_below_target_7pct"])

    def test_exact_hurdle_check_is_not_masked_by_display_rounding(self):
        cashflow_by_year = [{
            "year": "2026", "net_operating_income": 699_600,
            "replacement_reserves": 0, "total_capex": 0, "debt_service": 0,
            "leveraged_cash_flow": 699_600,
        }]
        result = compute_cash_on_cash(cashflow_by_year, equity_basis=10_000_000)
        self.assertEqual(result["cash_on_cash_year_1"], 0.07)
        self.assertAlmostEqual(result["cash_on_cash_year_1_exact"], 0.06996)
        self.assertTrue(result["coc_below_target"])

    def test_post_debt_coc_uses_leveraged_cash_flow(self):
        cashflow_by_year = [{
            "year": "2026", "net_operating_income": 1_000_000,
            "replacement_reserves": 100_000, "total_capex": 200_000,
            "debt_service": 300_000, "leveraged_cash_flow": 400_000,
        }]
        result = compute_cash_on_cash(cashflow_by_year, equity_basis=10_000_000)
        # Free CF = leveraged CF 400k → 4%
        self.assertEqual(result["free_cf_year_1"], 400_000.0)
        self.assertEqual(result["cash_on_cash_year_1"], 0.04)
        self.assertEqual(result["components"]["debt_service_year_1"], 300_000.0)

    def test_fallback_still_works_without_leveraged_cash_flow(self):
        cashflow_by_year = [{
            "year": "2026", "net_operating_income": 1_000_000,
            "replacement_reserves": 100_000, "total_capex": 200_000,
            "debt_service": 300_000,
        }]
        result = compute_cash_on_cash(cashflow_by_year, equity_basis=10_000_000)
        self.assertEqual(result["free_cf_year_1"], 400_000.0)
        self.assertEqual(result["cash_on_cash_year_1"], 0.04)


class TestCashOnCashWithFundBlock(unittest.TestCase):
    def test_fund_am_and_partnership_deduct(self):
        cashflow_by_year = [{
            "year": "2026", "net_operating_income": 1_000_000,
            "replacement_reserves": 50_000, "total_capex": 50_000,
            "debt_service": 200_000, "leveraged_cash_flow": 700_000,
        }]
        fund_by_year = [{
            "year": "2026",
            "asset_management_fee": 100_000,
            "partnership_expenses": 50_000,
        }]
        result = compute_cash_on_cash(
            cashflow_by_year, equity_basis=10_000_000, fund_by_year=fund_by_year,
        )
        # 700k levered CF - 100k AM - 50k partnership = 550k, or exactly 5.5%.
        self.assertEqual(result["free_cf_year_1"], 550_000.0)
        self.assertAlmostEqual(result["cash_on_cash_year_1"], 0.055, places=4)
        self.assertAlmostEqual(result["cash_on_cash_year_1_exact"], 0.055)
        self.assertTrue(result["coc_below_target"])
        comps = result["components"]
        self.assertEqual(comps["asset_management_fee_year_1"], 100_000.0)
        self.assertEqual(comps["partnership_expenses_year_1"], 50_000.0)

    def test_no_fund_block_treats_am_partnership_as_zero(self):
        cashflow_by_year = [{
            "year": "2026", "net_operating_income": 1_000_000,
            "replacement_reserves": 0, "total_capex": 0,
            "debt_service": 0, "leveraged_cash_flow": 1_000_000,
        }]
        result = compute_cash_on_cash(cashflow_by_year, equity_basis=10_000_000)
        comps = result["components"]
        self.assertEqual(comps["asset_management_fee_year_1"], 0.0)
        self.assertEqual(comps["partnership_expenses_year_1"], 0.0)


class TestCashOnCashEdgeCases(unittest.TestCase):
    def test_empty_cashflow(self):
        result = compute_cash_on_cash([], equity_basis=10_000_000)
        self.assertIsNone(result["cash_on_cash_year_1"])
        self.assertIsNone(result["free_cf_year_1"])
        self.assertFalse(result["coc_below_target"])

    def test_zero_equity_returns_none(self):
        cashflow_by_year = [{
            "year": "2026", "net_operating_income": 1_000_000,
            "replacement_reserves": 0, "total_capex": 0,
            "debt_service": 0, "leveraged_cash_flow": 1_000_000,
        }]
        result = compute_cash_on_cash(cashflow_by_year, equity_basis=0)
        self.assertIsNone(result["cash_on_cash_year_1"])
        # Free CF still computed
        self.assertEqual(result["free_cf_year_1"], 1_000_000.0)
        # No CoC → flag stays False
        self.assertFalse(result["coc_below_target"])

    def test_negative_free_cf_below_target(self):
        # Leveraged CF -100k → CoC -1% → below target
        cashflow_by_year = [{
            "year": "2026", "net_operating_income": 100_000,
            "replacement_reserves": 0, "total_capex": 200_000,
            "debt_service": 0, "leveraged_cash_flow": -100_000,
        }]
        result = compute_cash_on_cash(cashflow_by_year, equity_basis=10_000_000)
        self.assertEqual(result["free_cf_year_1"], -100_000.0)
        self.assertEqual(result["cash_on_cash_year_1"], -0.01)
        self.assertTrue(result["coc_below_target"])


class TestProvenanceShape(unittest.TestCase):
    """Verify build_provenance includes the coc block."""

    def test_provenance_includes_coc(self):
        from engine.api import build_provenance
        results = {
            "metrics": {
                "coc": {
                    "cash_on_cash_year_1": 0.07,
                    "cash_on_cash_year_1_exact": 0.0700000001,
                    "free_cf_year_1": 700_000.0,
                    "target_cash_on_cash_pct": 0.07,
                    "coc_below_target": False,
                    "coc_below_target_7pct": False,
                    "components": {},
                },
                "yields": {},
            }
        }
        # Minimal inputs (build_provenance hashes them)
        prov = build_provenance(
            inputs={"metadata": {"deal_id": "test"}},
            results=results,
            validator_status="PASS",
            validator_report_dict=None,
        )
        self.assertIn("coc", prov)
        self.assertIsNotNone(prov["coc"])
        self.assertEqual(prov["coc"]["cash_on_cash_year_1"], 0.07)
        self.assertEqual(prov["coc"]["cash_on_cash_year_1_exact"], 0.0700000001)
        self.assertFalse(prov["coc"]["coc_below_target"])

    def test_provenance_coc_none_when_missing(self):
        from engine.api import build_provenance
        results = {"metrics": {"yields": {}}}
        prov = build_provenance(
            inputs={"metadata": {"deal_id": "test"}},
            results=results,
            validator_status="PASS",
            validator_report_dict=None,
        )
        self.assertIn("coc", prov)
        self.assertIsNone(prov["coc"])


if __name__ == "__main__":
    unittest.main()
