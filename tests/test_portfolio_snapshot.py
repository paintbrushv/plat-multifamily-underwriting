"""
Tests for Portfolio Snapshot and Monthly LP Report
"""
import json
import tempfile
import unittest
from pathlib import Path

from engine.portfolio import Portfolio
from engine.portfolio_snapshot import (
    save_snapshot,
    load_snapshot,
    load_latest_snapshot,
    load_previous_snapshot,
    compute_period_deltas,
)


def _make_deal(deal_id, units=100, pp=10_000_000, irr=0.15, em=1.9, metro_city="dallas"):
    """Create mock inputs + results for testing."""
    inputs = {
        "metadata": {"deal_id": deal_id, "city": metro_city},
        "time_grid": {"analysis_start_date": "2026-01-01", "analysis_end_date": "2030-12-31"},
        "unit_cohorts": [{"cohort_id": "1BR", "unit_count": units, "sqft": 800,
                          "initial_inplace_rent": 1200}],
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
                {"year": "2026", "net_operating_income": 500_000},
                {"year": "2030", "net_operating_income": 580_000},
            ],
        },
        "fund_waterfall": {"summary": {"partnership_equity_multiple": em * 0.9}},
    }
    return inputs, results


class TestSaveLoadSnapshot(unittest.TestCase):
    def test_save_and_load(self):
        portfolio = Portfolio()
        portfolio.add_deal(*_make_deal("Deal A"))
        portfolio.add_deal(*_make_deal("Deal B", units=200, pp=20_000_000, metro_city="austin"))

        with tempfile.TemporaryDirectory() as td:
            path = save_snapshot(portfolio, td, 2026, 3)
            self.assertTrue(path.exists())
            self.assertEqual(path.name, "portfolio_2026-03.json")

            snap = load_snapshot(td, 2026, 3)
            self.assertIsNotNone(snap)
            self.assertEqual(snap["period"], "2026-03")
            self.assertEqual(snap["summary"]["deal_count"], 2)
            self.assertEqual(len(snap["deals"]), 2)

    def test_load_nonexistent(self):
        with tempfile.TemporaryDirectory() as td:
            snap = load_snapshot(td, 2026, 1)
            self.assertIsNone(snap)

    def test_load_latest(self):
        portfolio = Portfolio()
        portfolio.add_deal(*_make_deal("Deal A"))

        with tempfile.TemporaryDirectory() as td:
            save_snapshot(portfolio, td, 2026, 1)
            save_snapshot(portfolio, td, 2026, 2)
            save_snapshot(portfolio, td, 2026, 3)

            latest = load_latest_snapshot(td)
            self.assertEqual(latest["period"], "2026-03")

    def test_load_previous(self):
        portfolio = Portfolio()
        portfolio.add_deal(*_make_deal("Deal A"))

        with tempfile.TemporaryDirectory() as td:
            save_snapshot(portfolio, td, 2026, 1)
            save_snapshot(portfolio, td, 2026, 2)
            save_snapshot(portfolio, td, 2026, 3)

            prev = load_previous_snapshot(td, 2026, 3)
            self.assertEqual(prev["period"], "2026-02")

    def test_load_previous_no_prior(self):
        portfolio = Portfolio()
        portfolio.add_deal(*_make_deal("Deal A"))

        with tempfile.TemporaryDirectory() as td:
            save_snapshot(portfolio, td, 2026, 1)
            prev = load_previous_snapshot(td, 2026, 1)
            self.assertIsNone(prev)


class TestComputeDeltas(unittest.TestCase):
    def test_new_deal_detected(self):
        p1 = Portfolio()
        p1.add_deal(*_make_deal("Deal A"))

        p2 = Portfolio()
        p2.add_deal(*_make_deal("Deal A"))
        p2.add_deal(*_make_deal("Deal B", metro_city="austin"))

        with tempfile.TemporaryDirectory() as td:
            save_snapshot(p1, td, 2026, 1)
            save_snapshot(p2, td, 2026, 2)

            s1 = load_snapshot(td, 2026, 1)
            s2 = load_snapshot(td, 2026, 2)
            deltas = compute_period_deltas(s2, s1)

        self.assertEqual(deltas["new_deals"], ["Deal B"])
        self.assertEqual(deltas["removed_deals"], [])
        self.assertEqual(deltas["summary_deltas"]["deal_count"]["change"], 1)

    def test_removed_deal_detected(self):
        p1 = Portfolio()
        p1.add_deal(*_make_deal("Deal A"))
        p1.add_deal(*_make_deal("Deal B"))

        p2 = Portfolio()
        p2.add_deal(*_make_deal("Deal A"))

        with tempfile.TemporaryDirectory() as td:
            save_snapshot(p1, td, 2026, 1)
            save_snapshot(p2, td, 2026, 2)

            s1 = load_snapshot(td, 2026, 1)
            s2 = load_snapshot(td, 2026, 2)
            deltas = compute_period_deltas(s2, s1)

        self.assertEqual(deltas["new_deals"], [])
        self.assertEqual(deltas["removed_deals"], ["Deal B"])
        self.assertEqual(deltas["summary_deltas"]["deal_count"]["change"], -1)

    def test_unit_count_delta(self):
        p1 = Portfolio()
        p1.add_deal(*_make_deal("Deal A", units=100))

        p2 = Portfolio()
        p2.add_deal(*_make_deal("Deal A", units=100))
        p2.add_deal(*_make_deal("Deal B", units=250, metro_city="austin"))

        with tempfile.TemporaryDirectory() as td:
            save_snapshot(p1, td, 2026, 1)
            save_snapshot(p2, td, 2026, 2)

            s1 = load_snapshot(td, 2026, 1)
            s2 = load_snapshot(td, 2026, 2)
            deltas = compute_period_deltas(s2, s1)

        self.assertEqual(deltas["summary_deltas"]["total_units"]["change"], 250)

    def test_metro_changes(self):
        p1 = Portfolio()
        p1.add_deal(*_make_deal("Deal A", metro_city="dallas"))

        p2 = Portfolio()
        p2.add_deal(*_make_deal("Deal A", metro_city="dallas"))
        p2.add_deal(*_make_deal("Deal B", units=200, metro_city="austin"))

        with tempfile.TemporaryDirectory() as td:
            save_snapshot(p1, td, 2026, 1)
            save_snapshot(p2, td, 2026, 2)

            s1 = load_snapshot(td, 2026, 1)
            s2 = load_snapshot(td, 2026, 2)
            deltas = compute_period_deltas(s2, s1)

        # Austin should show +1 deal, +200 units
        austin_changes = deltas["metro_changes"].get("Austin", {})
        self.assertEqual(austin_changes.get("deals"), 1)
        self.assertEqual(austin_changes.get("units"), 200)

    def test_irr_delta(self):
        p1 = Portfolio()
        p1.add_deal(*_make_deal("Deal A", irr=0.15))

        p2 = Portfolio()
        p2.add_deal(*_make_deal("Deal A", irr=0.17))

        with tempfile.TemporaryDirectory() as td:
            save_snapshot(p1, td, 2026, 1)
            save_snapshot(p2, td, 2026, 2)

            s1 = load_snapshot(td, 2026, 1)
            s2 = load_snapshot(td, 2026, 2)
            deltas = compute_period_deltas(s2, s1)

        irr_delta = deltas["summary_deltas"]["weighted_avg_levered_irr"]
        self.assertIsNotNone(irr_delta["change"])
        self.assertAlmostEqual(irr_delta["change"], 0.02, places=4)


class TestMonthlyLPReport(unittest.TestCase):
    def test_generate_without_deltas(self):
        from engine.portfolio_pdf import generate_monthly_lp_report

        portfolio = Portfolio()
        portfolio.add_deal(*_make_deal("Deal A"))
        portfolio.add_deal(*_make_deal("Deal B", metro_city="austin"))

        with tempfile.TemporaryDirectory() as td:
            pdf_path = Path(td) / "monthly.pdf"
            result = generate_monthly_lp_report(portfolio, pdf_path, report_month="March 2026")
            self.assertTrue(result.exists())
            self.assertGreater(result.stat().st_size, 1000)

    def test_generate_with_deltas(self):
        from engine.portfolio_pdf import generate_monthly_lp_report

        p1 = Portfolio()
        p1.add_deal(*_make_deal("Deal A"))

        p2 = Portfolio()
        p2.add_deal(*_make_deal("Deal A"))
        p2.add_deal(*_make_deal("Deal B", metro_city="austin"))

        with tempfile.TemporaryDirectory() as td:
            snap_dir = Path(td) / "snapshots"
            save_snapshot(p1, snap_dir, 2026, 2)
            save_snapshot(p2, snap_dir, 2026, 3)

            s1 = load_snapshot(snap_dir, 2026, 2)
            s2 = load_snapshot(snap_dir, 2026, 3)
            deltas = compute_period_deltas(s2, s1)

            pdf_path = Path(td) / "monthly_with_deltas.pdf"
            result = generate_monthly_lp_report(
                p2, pdf_path, deltas=deltas, report_month="March 2026"
            )
            self.assertTrue(result.exists())
            self.assertGreater(result.stat().st_size, 1000)

    def test_generate_no_deals(self):
        from engine.portfolio_pdf import generate_monthly_lp_report

        portfolio = Portfolio()
        with tempfile.TemporaryDirectory() as td:
            pdf_path = Path(td) / "empty.pdf"
            result = generate_monthly_lp_report(portfolio, pdf_path)
            self.assertTrue(result.exists())


if __name__ == "__main__":
    unittest.main()
