"""Tests for LP narrative generation, exception detection, and outlook."""

import pytest
from unittest.mock import MagicMock
from datetime import datetime, timezone


def _make_portfolio_and_deltas(noi_current=1_000_000, noi_previous=950_000,
                                new_deals=None, deal_count=18, total_units=3200,
                                total_equity=45_000_000, weighted_irr=0.152):
    """Build mock portfolio + snapshot deltas for narrative tests."""
    portfolio = MagicMock()
    portfolio.deals = [MagicMock() for _ in range(deal_count)]
    portfolio.summary.return_value = {
        "deal_count": deal_count,
        "total_units": total_units,
        "total_equity": total_equity,
        "total_noi_year_1": noi_current,
        "weighted_avg_levered_irr": weighted_irr,
        "weighted_avg_levered_em": 1.85,
    }
    noi_change = noi_current - noi_previous
    deltas = {
        "current_period": "2026-04",
        "previous_period": "2026-03",
        "summary_deltas": {
            "total_noi_year_1": {
                "current": noi_current,
                "previous": noi_previous,
                "change": noi_change,
            },
            "deal_count": {"current": deal_count, "previous": deal_count, "change": 0},
        },
        "new_deals": new_deals or [],
        "removed_deals": [],
    }
    return portfolio, deltas


class TestGenerateNarrative:
    def test_narrative_above_underwriting(self):
        from engine.lp_narrative import generate_narrative
        portfolio, deltas = _make_portfolio_and_deltas(noi_current=1_000_000, noi_previous=950_000)
        result = generate_narrative(portfolio, deltas)
        assert result["noi_source"] == "snapshot_delta"
        assert result["deal_count"] == 18
        assert result["portfolio_noi_delta_pct"] == pytest.approx(5.26, abs=0.1)
        assert any("above" in line.lower() for line in result["summary_lines"])

    def test_narrative_below_underwriting(self):
        from engine.lp_narrative import generate_narrative
        portfolio, deltas = _make_portfolio_and_deltas(noi_current=900_000, noi_previous=1_000_000)
        result = generate_narrative(portfolio, deltas)
        assert result["portfolio_noi_delta_pct"] == pytest.approx(-10.0, abs=0.1)
        assert any("below" in line.lower() for line in result["summary_lines"])

    def test_narrative_with_new_deals(self):
        from engine.lp_narrative import generate_narrative
        portfolio, deltas = _make_portfolio_and_deltas(new_deals=["sample_deal_gamma", "sample_deal_delta"])
        result = generate_narrative(portfolio, deltas)
        assert any("2 new" in line.lower() or "2 acquisition" in line.lower()
                    for line in result["summary_lines"])


def _make_portfolio_with_deals(deal_overrides):
    portfolio = MagicMock()
    deals = []
    inputs_list = []
    for d in deal_overrides:
        snap = MagicMock()
        snap.deal_id = d["deal_id"]
        snap.noi_year_1 = d["engine_noi"]
        deals.append(snap)
        inputs_list.append({"metadata": {"deal_id": d["deal_id"]}})
    portfolio.deals = deals
    portfolio._deal_inputs = inputs_list
    return portfolio


class TestDetectExceptions:
    def test_exception_detection_am(self):
        from engine.lp_narrative import detect_exceptions
        portfolio = _make_portfolio_with_deals([
            {"deal_id": "good_deal", "engine_noi": 100_000},
            {"deal_id": "bad_deal", "engine_noi": 100_000},
        ])
        am_data = {
            "good_deal": {"actual_noi": 97_000, "engine_noi": 100_000,
                          "revenue_variance": -0.01, "opex_variance": 0.02,
                          "occupancy_variance": -0.01, "capex_variance": 0.0},
            "bad_deal": {"actual_noi": 85_000, "engine_noi": 100_000,
                         "revenue_variance": -0.05, "opex_variance": 0.03,
                         "occupancy_variance": -0.12, "capex_variance": 0.01},
        }
        exceptions = detect_exceptions(portfolio, am_data=am_data)
        assert len(exceptions) == 1
        assert exceptions[0]["deal_id"] == "bad_deal"
        assert exceptions[0]["noi_variance_pct"] == pytest.approx(-15.0, abs=0.1)
        assert exceptions[0]["source"] == "am_actuals"

    def test_exception_detection_snapshot(self):
        from engine.lp_narrative import detect_exceptions
        portfolio = _make_portfolio_with_deals([
            {"deal_id": "stable", "engine_noi": 100_000},
            {"deal_id": "drifted", "engine_noi": 120_000},
        ])
        prev_deal_noi = {"stable": 98_000, "drifted": 100_000}
        exceptions = detect_exceptions(portfolio, prev_deal_noi=prev_deal_noi)
        assert len(exceptions) == 1
        assert exceptions[0]["deal_id"] == "drifted"
        assert exceptions[0]["source"] == "snapshot_delta"

    def test_exception_root_cause(self):
        from engine.lp_narrative import detect_exceptions
        portfolio = _make_portfolio_with_deals([
            {"deal_id": "opex_problem", "engine_noi": 100_000},
        ])
        am_data = {
            "opex_problem": {"actual_noi": 88_000, "engine_noi": 100_000,
                             "revenue_variance": -0.02, "opex_variance": 0.15,
                             "occupancy_variance": -0.03, "capex_variance": 0.01},
        }
        exceptions = detect_exceptions(portfolio, am_data=am_data)
        assert len(exceptions) == 1
        assert exceptions[0]["root_cause"] == "opex"


def _make_portfolio_with_inputs(inputs_list):
    portfolio = MagicMock()
    portfolio._deal_inputs = inputs_list
    deals = []
    for inp in inputs_list:
        snap = MagicMock()
        snap.deal_id = inp.get("metadata", {}).get("deal_id", "unknown")
        deals.append(snap)
    portfolio.deals = deals
    return portfolio


class TestComputeOutlook:
    def test_outlook_turnover_estimate(self):
        from engine.lp_narrative import compute_outlook
        portfolio = _make_portfolio_with_inputs([{
            "metadata": {"deal_id": "deal_a", "close_date": "2024-01-01"},
            "unit_cohorts": [{"cohort_id": "1br", "count": 100}],
            "trade_out_assumptions": {"annual_turnover_pct": 0.50},
            "debt_terms": {"rate": 0.065},
        }])
        result = compute_outlook(portfolio, lookahead_months=3, current_month=12)
        assert result["turnover_estimate"]["units"] == 13
        assert result["turnover_estimate"]["pct"] == pytest.approx(50.0, abs=0.1)

    def test_outlook_rate_cap_expiry(self):
        from engine.lp_narrative import compute_outlook
        portfolio = _make_portfolio_with_inputs([{
            "metadata": {"deal_id": "rio", "close_date": "2024-01-01"},
            "unit_cohorts": [{"cohort_id": "1br", "count": 50}],
            "debt_terms": {"rate_type": "variable", "rate_cap_expiry_month": 38, "rate": 0.065},
        }])
        result = compute_outlook(portfolio, lookahead_months=6, current_month=35)
        assert len(result["rate_cap_expirations"]) == 1
        assert result["rate_cap_expirations"][0]["deal_id"] == "rio"
        assert result["rate_cap_expirations"][0]["expiry_month"] == 38

    def test_outlook_renovation_pipeline(self):
        from engine.lp_narrative import compute_outlook
        portfolio = _make_portfolio_with_inputs([{
            "metadata": {"deal_id": "sample_deal_gamma", "close_date": "2024-01-01"},
            "unit_cohorts": [{"cohort_id": "1br", "count": 200}],
            "debt_terms": {"rate": 0.065},
            "unit_renovations": [
                {"unit_id": "101", "cohort_id": "1br", "renovation_month": 14, "scope": "full", "cost": 22000, "expected_premium": 200},
                {"unit_id": "102", "cohort_id": "1br", "renovation_month": 15, "scope": "full", "cost": 22000, "expected_premium": 200},
                {"unit_id": "103", "cohort_id": "1br", "renovation_month": 50, "scope": "full", "cost": 22000, "expected_premium": 200},
            ],
        }])
        result = compute_outlook(portfolio, lookahead_months=3, current_month=13)
        assert result["renovation_pipeline"]["units_scheduled"] == 2
        assert result["renovation_pipeline"]["cost_estimate"] == 44_000

    def test_outlook_am_lease_override(self):
        from engine.lp_narrative import compute_outlook
        portfolio = _make_portfolio_with_inputs([{
            "metadata": {"deal_id": "deal_a", "close_date": "2024-01-01"},
            "unit_cohorts": [{"cohort_id": "1br", "count": 100}],
            "trade_out_assumptions": {"annual_turnover_pct": 0.50},
            "debt_terms": {"rate": 0.065},
        }])
        am_data = {"deal_a": {"upcoming_lease_expirations": 8}}
        result = compute_outlook(portfolio, lookahead_months=3, current_month=12, am_data=am_data)
        assert result["am_lease_expirations"] == 8


from datetime import timedelta


class TestStaleness:
    def test_staleness_warning(self):
        from engine.lp_narrative import detect_stale_deals
        now = datetime(2026, 4, 20, tzinfo=timezone.utc)
        deal_timestamps = {
            "fresh_deal": (now - timedelta(days=5)).isoformat(),
            "stale_deal": (now - timedelta(days=45)).isoformat(),
            "borderline": (now - timedelta(days=30)).isoformat(),
        }
        stale = detect_stale_deals(deal_timestamps, max_age_days=30, now=now)
        assert stale == ["stale_deal"]


from pathlib import Path


class TestPDFRendering:
    def test_pdf_narrative_rendering(self, tmp_path):
        """PDF includes executive summary, exceptions, outlook when narrative provided."""
        from engine.portfolio_pdf import generate_monthly_lp_report
        from engine.portfolio import Portfolio

        portfolio = Portfolio()
        inputs = {
            "metadata": {"deal_id": "test_deal", "property_name": "Test Property",
                         "city": "Dallas", "close_date": "2024-01-01"},
            "unit_cohorts": [{"cohort_id": "1br", "count": 100, "in_place_rent": 1200,
                              "sqft": 750}],
            "purchase_price": 10_000_000,
            "growth_assumptions": {"rent_growth": 0.03, "growth_type": "annual_compound"},
            "hold_period_months": 60,
            "exit_assumptions": {"exit_cap_rate": 0.055},
            "debt_terms": {"commitment": 7_000_000, "rate": 0.065,
                           "amort_months": 360, "io_months": 24,
                           "term_months": 60, "loan_start_month": 1},
            "fund_assumptions": {"sponsor_promote_pct": 0.20, "pref_rate": 0.08,
                                 "equity_contribution": 3_000_000,
                                 "promote_splits": [{"gp_share": 0.20, "lp_share": 0.80}]},
            "opex_table": [{"category": "insurance", "year_1_amount": 50000,
                            "pricing_type": "$/asset"}],
        }
        results = {
            "noi_by_year": {str(y): 500_000 + y * 10_000 for y in range(1, 6)},
            "cashflow_by_year": {str(y): {"levered_cf": 200_000 + y * 5000} for y in range(1, 6)},
            "metrics": {"levered_irr": 0.152, "unlevered_irr": 0.095,
                        "levered_em": 1.85, "unlevered_em": 1.45},
        }
        portfolio.add_deal(inputs, results)

        narrative = {
            "summary": {
                "summary_lines": [
                    "Portfolio NOI is tracking 3.2% above underwriting across 1 deal.",
                    "Total equity deployed: $3,000,000. Weighted average levered IRR: 15.2%.",
                ],
                "period": "2026-04",
                "deal_count": 1,
                "portfolio_noi_delta_pct": 3.2,
                "noi_source": "snapshot_delta",
                "stale_deals": [],
            },
            "exceptions": [
                {"deal_id": "test_deal", "noi_variance_pct": -12.5,
                 "source": "am_actuals", "root_cause": "occupancy"},
            ],
            "outlook": {
                "turnover_estimate": {"units": 13, "pct": 50.0},
                "am_lease_expirations": None,
                "rate_cap_expirations": [],
                "renovation_pipeline": {"units_scheduled": 5, "cost_estimate": 110_000},
                "upcoming_refi_events": [],
            },
        }

        pdf_path = tmp_path / "test_report.pdf"
        result = generate_monthly_lp_report(portfolio, pdf_path, narrative=narrative)
        assert result.exists()
        assert result.stat().st_size > 0

    def test_pdf_backward_compat(self, tmp_path):
        """PDF unchanged when narrative=None."""
        from engine.portfolio_pdf import generate_monthly_lp_report
        from engine.portfolio import Portfolio

        portfolio = Portfolio()
        inputs = {
            "metadata": {"deal_id": "compat_deal", "property_name": "Compat",
                         "city": "Austin", "close_date": "2024-01-01"},
            "unit_cohorts": [{"cohort_id": "1br", "count": 50, "in_place_rent": 1100,
                              "sqft": 700}],
            "purchase_price": 5_000_000,
            "growth_assumptions": {"rent_growth": 0.03, "growth_type": "annual_compound"},
            "hold_period_months": 60,
            "exit_assumptions": {"exit_cap_rate": 0.06},
            "debt_terms": {"commitment": 3_500_000, "rate": 0.07,
                           "amort_months": 360, "io_months": 12,
                           "term_months": 60, "loan_start_month": 1},
            "fund_assumptions": {"sponsor_promote_pct": 0.20, "pref_rate": 0.08,
                                 "equity_contribution": 1_500_000,
                                 "promote_splits": [{"gp_share": 0.20, "lp_share": 0.80}]},
            "opex_table": [{"category": "insurance", "year_1_amount": 25000,
                            "pricing_type": "$/asset"}],
        }
        results = {
            "noi_by_year": {str(y): 250_000 for y in range(1, 6)},
            "cashflow_by_year": {str(y): {"levered_cf": 100_000} for y in range(1, 6)},
            "metrics": {"levered_irr": 0.12, "unlevered_irr": 0.08,
                        "levered_em": 1.6, "unlevered_em": 1.3},
        }
        portfolio.add_deal(inputs, results)

        pdf_path = tmp_path / "compat_report.pdf"
        result = generate_monthly_lp_report(portfolio, pdf_path, narrative=None)
        assert result.exists()
        assert result.stat().st_size > 0
