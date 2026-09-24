"""Tests for portfolio stress testing methods."""
import copy
import time
import pytest

from engine.portfolio import (
    Portfolio,
    _rerun_deal_with_overrides,
    _extract_stress_metrics,
    _compute_irr_from_cashflows,
    _equity_weighted_metrics,
)
from engine.engine import run_underwriting


@pytest.fixture
def stress_portfolio(minimal_deal_inputs):
    """Portfolio with 3 deals for stress testing."""
    p = Portfolio()

    # Deal 1: base
    deal1 = copy.deepcopy(minimal_deal_inputs)
    deal1["metadata"]["deal_id"] = "deal_A"
    deal1["metadata"]["city"] = "Dallas"
    results1 = run_underwriting(deal1, skip_validation=True)
    p.add_deal(deal1, results1)

    # Deal 2: different metro, higher cap
    deal2 = copy.deepcopy(minimal_deal_inputs)
    deal2["metadata"]["deal_id"] = "deal_B"
    deal2["metadata"]["city"] = "Austin"
    deal2["exit_assumptions"]["exit_cap_rate"] = 0.06
    results2 = run_underwriting(deal2, skip_validation=True)
    p.add_deal(deal2, results2)

    # Deal 3: different vintage
    deal3 = copy.deepcopy(minimal_deal_inputs)
    deal3["metadata"]["deal_id"] = "deal_C"
    deal3["metadata"]["city"] = "Birmingham"
    deal3["time_grid"]["analysis_start_date"] = "2025-01"
    deal3["time_grid"]["analysis_end_date"] = "2030-12"
    for curve_key in ["market_rent_curve", "loss_to_lease", "physical_vacancy_curve"]:
        for entry in deal3[curve_key]:
            entry["start_period"] = "2025-01"
            entry["end_period"] = "2030-12"
    deal3["collection_loss_curve"][0]["start_period"] = "2025-01"
    deal3["collection_loss_curve"][0]["end_period"] = "2030-12"
    deal3["program_adoption_curve"][0]["start_period"] = "2025-01"
    deal3["program_adoption_curve"][0]["end_period"] = "2030-12"
    deal3["revenue_programs"][0]["start_period"] = "2025-01"
    deal3["exit_assumptions"]["exit_month"] = "2030-12"
    deal3["replacement_reserves"][0]["start_period"] = "2025-01"
    deal3["replacement_reserves"][0]["end_period"] = "2030-12"
    results3 = run_underwriting(deal3, skip_validation=True)
    p.add_deal(deal3, results3)

    return p


class TestPortfolioInputStorage:
    """Verify Portfolio stores raw inputs for re-running."""

    def test_inputs_stored_on_add_deal(self, minimal_deal_inputs):
        p = Portfolio()
        results = run_underwriting(minimal_deal_inputs)
        p.add_deal(minimal_deal_inputs, results)

        assert len(p._deal_inputs) == 1
        assert p._deal_inputs[0]["metadata"]["deal_id"] == "TEST-001"

    def test_inputs_stored_on_add_deal_from_files(self, tmp_path, minimal_deal_inputs):
        import json
        from decimal import Decimal

        class _DecimalEncoder(json.JSONEncoder):
            def default(self, o):
                if isinstance(o, Decimal):
                    return float(o)
                return super().default(o)

        results = run_underwriting(minimal_deal_inputs)
        inp_path = tmp_path / "inputs.json"
        out_path = tmp_path / "outputs.json"
        inp_path.write_text(json.dumps(minimal_deal_inputs))
        out_path.write_text(json.dumps(results, cls=_DecimalEncoder))

        p = Portfolio()
        p.add_deal_from_files(inp_path, out_path)

        assert len(p._deal_inputs) == 1
        assert p._deal_inputs[0]["metadata"]["deal_id"] == "TEST-001"


class TestInternalHelpers:
    """Verify internal helper functions."""

    def test_rerun_deal_with_overrides_changes_cap_rate(self, minimal_deal_inputs):
        """Override exit cap rate and verify different results."""
        base_results = run_underwriting(minimal_deal_inputs, skip_validation=True)
        base_irr = base_results["metrics"]["irr"]["levered_irr"]

        shocked_results = _rerun_deal_with_overrides(
            minimal_deal_inputs,
            {"exit_assumptions.exit_cap_rate": 0.065},
        )
        shocked_irr = shocked_results["metrics"]["irr"]["levered_irr"]

        assert shocked_irr < base_irr, "Higher exit cap should reduce IRR"

    def test_rerun_preserves_original_inputs(self, minimal_deal_inputs):
        """Verify deep-copy: original inputs are not mutated."""
        original_cap = minimal_deal_inputs["exit_assumptions"]["exit_cap_rate"]
        _rerun_deal_with_overrides(
            minimal_deal_inputs,
            {"exit_assumptions.exit_cap_rate": 0.10},
        )
        assert minimal_deal_inputs["exit_assumptions"]["exit_cap_rate"] == original_cap

    def test_extract_stress_metrics_keys(self, minimal_deal_inputs):
        """Verify extracted metrics contain expected keys."""
        results = run_underwriting(minimal_deal_inputs, skip_validation=True)
        metrics = _extract_stress_metrics(results)

        expected_keys = {
            "levered_irr", "levered_em", "unlevered_irr", "unlevered_em",
            "min_dscr", "avg_dscr", "noi_exit", "exit_proceeds", "year_1_lcf",
        }
        assert set(metrics.keys()) == expected_keys

    def test_compute_irr_from_cashflows_simple(self):
        """Known IRR: invest 1000, receive 100/yr for 15 yrs + 1000 at end."""
        cfs = [-1000] + [100] * 14 + [1100]
        monthly_irr = _compute_irr_from_cashflows(cfs)
        assert monthly_irr is not None
        assert 0.09 < monthly_irr < 0.11  # ~10% annual

    def test_compute_irr_from_cashflows_no_convergence(self):
        """All positive cashflows should return None."""
        cfs = [100, 200, 300]
        assert _compute_irr_from_cashflows(cfs) is None

    def test_equity_weighted_metrics(self):
        """Weighted average across 2 deals."""
        deal_metrics = [
            {"metrics": {"levered_irr": 0.15, "levered_em": 2.0}, "equity": 1_000_000},
            {"metrics": {"levered_irr": 0.20, "levered_em": 2.5}, "equity": 3_000_000},
        ]
        result = _equity_weighted_metrics(deal_metrics)
        # Weighted: (0.15*1M + 0.20*3M) / 4M = 0.1875
        assert abs(result["weighted_levered_irr"] - 0.1875) < 0.001
        # Weighted: (2.0*1M + 2.5*3M) / 4M = 2.375
        assert abs(result["weighted_levered_em"] - 2.375) < 0.01


class TestCapRateStress:
    """Tests 1-4 from spec: cap rate shock scenarios."""

    def test_single_deal_irr_decreases(self, minimal_deal_inputs):
        """Test 1: IRR decreases as exit cap increases."""
        p = Portfolio()
        results = run_underwriting(minimal_deal_inputs)
        p.add_deal(minimal_deal_inputs, results)

        stress = p.stress_test_cap_rate(shocks=[50])
        deal = stress["deals"][0]
        base_irr = deal["base"]["levered_irr"]
        shocked_irr = deal["shocked"]["50"]["levered_irr"]

        assert shocked_irr < base_irr

    def test_monotonic_irr_decrease(self, minimal_deal_inputs):
        """Test 2: Higher shock → strictly lower IRR."""
        p = Portfolio()
        results = run_underwriting(minimal_deal_inputs)
        p.add_deal(minimal_deal_inputs, results)

        stress = p.stress_test_cap_rate(shocks=[25, 50, 75, 100])
        deal = stress["deals"][0]

        irrs = [deal["base"]["levered_irr"]]
        for shock in [25, 50, 75, 100]:
            irrs.append(deal["shocked"][str(shock)]["levered_irr"])

        for i in range(1, len(irrs)):
            assert irrs[i] < irrs[i - 1], f"IRR should decrease at shock index {i}"

    def test_portfolio_aggregation(self, stress_portfolio):
        """Test 3: Equity-weighted IRR across 3 deals."""
        stress = stress_portfolio.stress_test_cap_rate(shocks=[50])

        assert "portfolio" in stress
        assert stress["portfolio"]["base"]["weighted_levered_irr"] is not None
        assert stress["portfolio"]["shocked"]["50"]["weighted_levered_irr"] is not None

        # Portfolio shocked IRR should be lower than base
        base = stress["portfolio"]["base"]["weighted_levered_irr"]
        shocked = stress["portfolio"]["shocked"]["50"]["weighted_levered_irr"]
        assert shocked < base

    def test_zero_shock_equals_base(self, minimal_deal_inputs):
        """Test 4: Shock of 0 bps = base metrics."""
        p = Portfolio()
        results = run_underwriting(minimal_deal_inputs)
        p.add_deal(minimal_deal_inputs, results)

        stress = p.stress_test_cap_rate(shocks=[0])
        deal = stress["deals"][0]

        base_irr = deal["base"]["levered_irr"]
        zero_shock_irr = deal["shocked"]["0"]["levered_irr"]

        assert abs(base_irr - zero_shock_irr) < 0.001


class TestRateStress:
    """Tests 5-7 from spec: rate shock scenarios."""

    def test_single_deal_dscr_decreases(self, minimal_deal_inputs):
        """Test 5: DSCR decreases as rate increases."""
        p = Portfolio()
        results = run_underwriting(minimal_deal_inputs)
        p.add_deal(minimal_deal_inputs, results)

        stress = p.stress_test_rates(shocks=[200])
        deal = stress["deals"][0]
        base_dscr = deal["base"]["avg_dscr"]
        shocked_dscr = deal["shocked"]["200"]["avg_dscr"]

        assert shocked_dscr < base_dscr

    def test_dscr_floor_computed(self, minimal_deal_inputs):
        """Test 6: Min DSCR under stress is computed correctly."""
        p = Portfolio()
        results = run_underwriting(minimal_deal_inputs)
        p.add_deal(minimal_deal_inputs, results)

        stress = p.stress_test_rates(shocks=[300])
        deal = stress["deals"][0]
        shocked = deal["shocked"]["300"]

        assert shocked["min_dscr"] is not None
        assert shocked["min_dscr"] <= shocked["avg_dscr"]

    def test_capital_stack_layers_shocked(self, minimal_deal_inputs):
        """Test 7: Rate shock applies to all capital stack layers."""
        deal = copy.deepcopy(minimal_deal_inputs)
        # Replace debt_terms with capital_stack (senior + mezzanine)
        del deal["debt_terms"]
        deal["capital_stack"] = [
            {
                "layer_type": "senior",
                "priority": 1,
                "commitment": 8_775_000,
                "rate": 0.055,
                "amort_years": 30,
                "io_months": 12,
            },
            {
                "layer_type": "mezzanine",
                "priority": 2,
                "commitment": 1_000_000,
                "rate": 0.10,
                "amort_years": 0,
                "io_months": 60,
            },
        ]
        deal["purchase_assumptions"]["equity_contribution"] = 3_725_000
        deal["purchase_assumptions"]["total_equity_basis"] = 3_875_000

        p = Portfolio()
        results = run_underwriting(deal, skip_validation=True)
        p.add_deal(deal, results)

        stress = p.stress_test_rates(shocks=[200])
        deal_result = stress["deals"][0]

        # Both layers should be shocked — combined effect on IRR
        base_irr = deal_result["base"]["levered_irr"]
        shocked_irr = deal_result["shocked"]["200"]["levered_irr"]
        assert shocked_irr < base_irr


class TestConcentrationRisk:
    """Tests 8-11 from spec: concentration risk analysis."""

    def test_metro_breach(self, minimal_deal_inputs):
        """Test 8: Metro at 60% equity flags when threshold is 50%."""
        p = Portfolio()
        # 3 deals: 2 in DFW (66%), 1 in Austin (33%)
        for i, city in enumerate(["Dallas", "Dallas", "Austin"]):
            deal = copy.deepcopy(minimal_deal_inputs)
            deal["metadata"]["deal_id"] = f"deal_{i}"
            deal["metadata"]["city"] = city
            results = run_underwriting(deal, skip_validation=True)
            p.add_deal(deal, results)

        result = p.concentration_risk(thresholds={"metro": 0.5})
        metro = result["dimensions"]["metro"]
        assert len(metro["breaches"]) > 0
        breach = metro["breaches"][0]
        assert breach["group"] == "DFW"
        assert breach["share"] > 0.5

    def test_no_breach(self, stress_portfolio):
        """Test 9: All metros under threshold → empty breaches."""
        # stress_portfolio has 3 deals in 3 different metros (DFW, Austin, Birmingham)
        result = stress_portfolio.concentration_risk(thresholds={"metro": 0.5})
        metro = result["dimensions"]["metro"]
        assert metro["breaches"] == []

    def test_weight_by_units(self, minimal_deal_inputs):
        """Test 10: Switching to units changes concentration values."""
        p = Portfolio()
        for i, city in enumerate(["Dallas", "Austin"]):
            deal = copy.deepcopy(minimal_deal_inputs)
            deal["metadata"]["deal_id"] = f"deal_{i}"
            deal["metadata"]["city"] = city
            results = run_underwriting(deal, skip_validation=True)
            p.add_deal(deal, results)

        eq_result = p.concentration_risk(weight_by="equity")
        unit_result = p.concentration_risk(weight_by="units")

        assert eq_result["weight_by"] == "equity"
        assert unit_result["weight_by"] == "units"
        # With identical deals, both weightings should give 50/50
        eq_dfw = eq_result["dimensions"]["metro"]["concentrations"]["DFW"]
        unit_dfw = unit_result["dimensions"]["metro"]["concentrations"]["DFW"]
        assert abs(eq_dfw - 0.5) < 0.01
        assert abs(unit_dfw - 0.5) < 0.01

    def test_vintage_dimension(self, stress_portfolio):
        """Test 11: Groups by year, computes shares correctly."""
        result = stress_portfolio.concentration_risk(thresholds={"vintage": 0.4})
        vintage = result["dimensions"]["vintage"]
        # stress_portfolio: 2 deals vintage 2026, 1 deal vintage 2025
        assert "2026" in vintage["concentrations"]
        assert "2025" in vintage["concentrations"]
        # 2026 has 2/3 of deals — share depends on equity weighting
        assert vintage["concentrations"]["2026"] > vintage["concentrations"]["2025"]


class TestMergedIRR:
    """Tests 12-14 from spec: merged portfolio IRR."""

    def test_two_deals_same_period(self, minimal_deal_inputs):
        """Test 12: Merged CF = sum, IRR between the two individual IRRs."""
        p = Portfolio()
        deal1 = copy.deepcopy(minimal_deal_inputs)
        deal1["metadata"]["deal_id"] = "deal_1"
        results1 = run_underwriting(deal1)
        p.add_deal(deal1, results1)

        deal2 = copy.deepcopy(minimal_deal_inputs)
        deal2["metadata"]["deal_id"] = "deal_2"
        deal2["exit_assumptions"]["exit_cap_rate"] = 0.06  # Lower returns
        results2 = run_underwriting(deal2)
        p.add_deal(deal2, results2)

        result = p.merged_irr()

        assert result["annual_irr"] is not None
        assert result["deal_count"] == 2
        assert result["start_month"] == "2026-07"
        assert result["end_month"] == "2031-06"

        # Merged IRR should be between individual IRRs
        irr1 = results1["metrics"]["irr"]["levered_irr"]
        irr2 = results2["metrics"]["irr"]["levered_irr"]
        low = min(irr1, irr2)
        high = max(irr1, irr2)
        assert low - 0.01 <= result["annual_irr"] <= high + 0.01

    def test_staggered_start_dates(self, minimal_deal_inputs):
        """Test 13: Union timeline handles deals starting 12 months apart."""
        p = Portfolio()
        deal1 = copy.deepcopy(minimal_deal_inputs)
        deal1["metadata"]["deal_id"] = "deal_early"
        results1 = run_underwriting(deal1)
        p.add_deal(deal1, results1)

        # Deal 2 starts 12 months later
        deal2 = copy.deepcopy(minimal_deal_inputs)
        deal2["metadata"]["deal_id"] = "deal_late"
        deal2["time_grid"]["analysis_start_date"] = "2027-07"
        deal2["time_grid"]["analysis_end_date"] = "2032-06"
        for curve_key in ["market_rent_curve", "loss_to_lease", "physical_vacancy_curve"]:
            for entry in deal2[curve_key]:
                entry["start_period"] = "2027-07"
                entry["end_period"] = "2032-06"
        deal2["collection_loss_curve"][0]["start_period"] = "2027-07"
        deal2["collection_loss_curve"][0]["end_period"] = "2032-06"
        deal2["program_adoption_curve"][0]["start_period"] = "2027-07"
        deal2["program_adoption_curve"][0]["end_period"] = "2032-06"
        deal2["revenue_programs"][0]["start_period"] = "2027-07"
        deal2["exit_assumptions"]["exit_month"] = "2032-06"
        deal2["replacement_reserves"][0]["start_period"] = "2027-07"
        deal2["replacement_reserves"][0]["end_period"] = "2032-06"
        results2 = run_underwriting(deal2)
        p.add_deal(deal2, results2)

        result = p.merged_irr()

        assert result["start_month"] == "2026-07"
        assert result["end_month"] == "2032-06"
        assert result["total_months"] == 72  # 6 years
        assert result["annual_irr"] is not None

    def test_single_deal_equals_own_irr(self, minimal_deal_inputs):
        """Test 14: Merged IRR equals the deal's own levered IRR."""
        p = Portfolio()
        results = run_underwriting(minimal_deal_inputs)
        p.add_deal(minimal_deal_inputs, results)

        result = p.merged_irr()
        deal_irr = results["metrics"]["irr"]["levered_irr"]

        assert result["deal_count"] == 1
        # Should match within tolerance (monthly vs annual compounding differences)
        assert abs(result["annual_irr"] - deal_irr) < 0.02


class TestPerformance:
    """Test 15 from spec: stress test performance."""

    def test_three_deals_four_shocks_under_5_seconds(self, stress_portfolio):
        """Test 15: 3 deals × 4 shocks completes in <5 seconds."""
        start = time.time()
        stress_portfolio.stress_test_cap_rate(shocks=[25, 50, 75, 100])
        elapsed = time.time() - start

        assert elapsed < 5.0, f"Cap rate stress took {elapsed:.1f}s (limit: 5s)"
