"""
Tests for Portfolio Analytics Module
"""
import unittest

from engine.portfolio import (
    Portfolio,
    DealSnapshot,
    _infer_metro,
    _extract_deal_snapshot,
    _equity_weighted_metrics,
)


def _make_deal(deal_id, units=100, pp=10000000, irr=0.15, em=1.9, metro_city="dallas"):
    """Create mock inputs + results for testing."""
    inputs = {
        "metadata": {"deal_id": deal_id, "city": metro_city},
        "time_grid": {"analysis_start_date": "2026-01-01", "analysis_end_date": "2030-12-31"},
        "unit_cohorts": [{"cohort_id": "1BR", "unit_count": units, "sqft": 800, "initial_inplace_rent": 1200}],
        "purchase_assumptions": {"purchase_price": pp},
        "exit_assumptions": {"exit_cap_rate": 0.055},
        "debt_terms": {"commitment": pp * 0.7, "rate": 0.065, "amort_years": 30},
    }
    results = {
        "metrics": {
            "irr": {"levered_irr": irr, "unlevered_irr": irr * 0.7},
            "equity_multiple": {"levered_em": em, "unlevered_em": em * 0.8},
            "dscr": {"average_dscr": 1.35, "minimum_dscr": 1.20},
            "yields": {"going_in_cap_rate": 0.05},
        },
        "cashflow": {
            "by_year": [
                {"year": "2026", "net_operating_income": 500000},
                {"year": "2030", "net_operating_income": 580000},
            ],
        },
        "fund_waterfall": {"summary": {"partnership_equity_multiple": em * 0.9}},
    }
    return inputs, results


class TestInferMetro(unittest.TestCase):
    def test_dfw_from_city(self):
        self.assertEqual(_infer_metro({"metadata": {"city": "Dallas"}}), "DFW")

    def test_austin_from_city(self):
        self.assertEqual(_infer_metro({"metadata": {"city": "Austin"}}), "Austin")

    def test_birmingham_from_city(self):
        self.assertEqual(_infer_metro({"metadata": {"city": "Birmingham"}}), "Birmingham")

    def test_unknown_city(self):
        self.assertEqual(_infer_metro({"metadata": {"city": "Portland"}}), "Other")

    def test_explicit_metro(self):
        self.assertEqual(_infer_metro({"metadata": {"metro": "DFW", "city": "Portland"}}), "DFW")

    def test_explicit_metro_non_canonical_falls_through_to_address(self):
        # Non-canonical metadata.metro values like "Dallas, TX" must NOT short-circuit;
        # the address keyword match should still classify the deal correctly.
        # Regression guard for the M2 refactor (caught by Codex review).
        self.assertEqual(
            _infer_metro({"metadata": {"metro": "Dallas, TX", "city": "Dallas"}}),
            "DFW",
        )

    def test_address_fallback(self):
        self.assertEqual(_infer_metro({"metadata": {"address_1": "123 Main St, Fort Worth TX"}}), "DFW")


class TestEquityWeightedMetrics(unittest.TestCase):
    """Regression guards for _equity_weighted_metrics edge cases (caught by Codex)."""

    def test_skips_zero_equity_deals_in_denominator(self):
        # If the only deals contributing IRR have equity=0, the helper must
        # return None rather than ZeroDivisionError. Old inline implementations
        # filtered total_equity > 0; the helper now does the same.
        items = [
            {"metrics": {"levered_irr": 0.15, "levered_em": 1.8}, "equity": 0},
            {"metrics": {"levered_irr": 0.12, "levered_em": 1.6}, "equity": 0},
            {"metrics": {"levered_irr": 0.10, "levered_em": 1.5}, "equity": 1_000_000},
        ]
        result = _equity_weighted_metrics(items)
        # Only the third deal contributes; weighted_irr should equal its irr.
        self.assertAlmostEqual(result["weighted_levered_irr"], 0.10, places=6)
        self.assertAlmostEqual(result["weighted_levered_em"], 1.5, places=4)

    def test_all_zero_equity_returns_none_not_crash(self):
        items = [
            {"metrics": {"levered_irr": 0.15, "levered_em": 1.8}, "equity": 0},
            {"metrics": {"levered_irr": 0.12, "levered_em": 1.6}, "equity": 0},
        ]
        result = _equity_weighted_metrics(items)
        self.assertIsNone(result["weighted_levered_irr"])
        self.assertIsNone(result["weighted_levered_em"])


class TestDealSnapshot(unittest.TestCase):
    def test_extract_basic_fields(self):
        inputs, results = _make_deal("Test Deal", units=200, pp=20000000)
        snap = _extract_deal_snapshot(inputs, results)
        self.assertEqual(snap.deal_id, "Test Deal")
        self.assertEqual(snap.units, 200)
        self.assertEqual(snap.purchase_price, 20000000)
        self.assertEqual(snap.price_per_unit, 100000)

    def test_vintage_year_from_start_date(self):
        inputs, results = _make_deal("Test Deal")
        snap = _extract_deal_snapshot(inputs, results)
        self.assertEqual(snap.vintage_year, 2026)


class TestPortfolio(unittest.TestCase):
    def _build_portfolio(self):
        p = Portfolio()
        p.add_deal(*_make_deal("Deal A", units=100, pp=10000000, irr=0.15, em=1.9, metro_city="Dallas"))
        p.add_deal(*_make_deal("Deal B", units=200, pp=25000000, irr=0.18, em=2.1, metro_city="Austin"))
        p.add_deal(*_make_deal("Deal C", units=150, pp=15000000, irr=0.12, em=1.7, metro_city="Dallas"))
        return p

    def test_deal_count(self):
        p = self._build_portfolio()
        self.assertEqual(len(p.deals), 3)

    def test_summary_totals(self):
        p = self._build_portfolio()
        s = p.summary()
        self.assertEqual(s["deal_count"], 3)
        self.assertEqual(s["total_units"], 450)
        self.assertEqual(s["total_purchase_price"], 50000000)

    def test_weighted_irr(self):
        p = self._build_portfolio()
        s = p.summary()
        # Equity = 30% of PP: A=3M, B=7.5M, C=4.5M
        # Weighted IRR = (0.15*3M + 0.18*7.5M + 0.12*4.5M) / 15M
        expected = (0.15 * 3000000 + 0.18 * 7500000 + 0.12 * 4500000) / 15000000
        self.assertAlmostEqual(s["weighted_avg_levered_irr"], round(expected, 4), places=3)

    def test_group_by_metro(self):
        p = self._build_portfolio()
        by_metro = p.group_by_metro()
        self.assertIn("DFW", by_metro)
        self.assertIn("Austin", by_metro)
        self.assertEqual(by_metro["DFW"]["deal_count"], 2)
        self.assertEqual(by_metro["Austin"]["deal_count"], 1)
        self.assertEqual(by_metro["DFW"]["total_units"], 250)

    def test_deal_comparison_matrix(self):
        p = self._build_portfolio()
        matrix = p.deal_comparison_matrix()
        self.assertEqual(len(matrix), 3)
        self.assertEqual(matrix[0]["deal_id"], "Deal A")
        self.assertIn("levered_irr", matrix[0])
        self.assertIn("metro", matrix[0])

    def test_top_deals(self):
        p = self._build_portfolio()
        top = p.top_deals("levered_irr", n=2)
        self.assertEqual(len(top), 2)
        self.assertEqual(top[0].deal_id, "Deal B")  # 18% IRR
        self.assertEqual(top[1].deal_id, "Deal A")  # 15% IRR

    def test_group_by_vintage(self):
        p = self._build_portfolio()
        by_vintage = p.group_by_vintage()
        self.assertIn(2026, by_vintage)
        self.assertEqual(by_vintage[2026]["deal_count"], 3)

    def test_empty_portfolio(self):
        p = Portfolio()
        s = p.summary()
        self.assertEqual(s["deal_count"], 0)


if __name__ == "__main__":
    unittest.main()
