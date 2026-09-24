"""
Tests for Bull/Base/Bear Scenario Comparison Module

Tests cover:
- Delta application to deal inputs
- Scenario runner with mock engine
- Comparison metric extraction
- Preset configurations
- Edge cases (missing data, zero deltas)
"""
import copy
import unittest

from engine.modules.scenarios import (
    STABILIZED_PRESETS,
    VALUE_ADD_PRESETS,
    ScenarioPresets,
    _apply_scenario_deltas,
    _build_assumptions_summary,
    _extract_comparison_metrics,
    run_scenarios,
    run_refi_vs_sell,
)


def _make_base_inputs():
    """Create minimal base deal inputs for testing."""
    return {
        "growth_assumptions": {"annual_growth_rate": 0.03},
        "exit_assumptions": {"exit_cap_rate": 0.055},
        "physical_vacancy_curve": [
            {"cohort_id": "1BR", "start_period": "2026-01", "end_period": "2030-12", "vacancy_rate": 0.05},
        ],
        "opex_table": [
            {"category": "insurance", "pricing_type": "$/unit", "amount": 100, "growth_rate": 0.03},
            {"category": "repairs", "pricing_type": "$/unit", "amount": 200, "growth_rate": 0.025},
        ],
        "market_rent_curve": [
            {"cohort_id": "1BR", "start_period": "2026-01", "end_period": "2026-12", "market_rent": 1200},
            {"cohort_id": "1BR", "start_period": "2027-01", "end_period": "2027-12", "market_rent": 1236},
        ],
    }


def _mock_engine_runner(inputs):
    """Mock engine that returns metrics derived from inputs."""
    rg = inputs.get("growth_assumptions", {}).get("annual_growth_rate", 0.03)
    ec = inputs.get("exit_assumptions", {}).get("exit_cap_rate", 0.055)
    vac = inputs.get("physical_vacancy_curve", [{}])[0].get("vacancy_rate", 0.05)

    # Simplified metrics that vary with inputs
    irr = 0.15 + (rg - 0.03) * 2 - (ec - 0.055) * 3 - (vac - 0.05) * 0.5
    em = 1.8 + (rg - 0.03) * 5 - (ec - 0.055) * 8 - (vac - 0.05) * 1.0

    return {
        "metrics": {
            "irr": {"levered_irr": round(irr, 4), "unlevered_irr": round(irr * 0.7, 4)},
            "equity_multiple": {"levered_em": round(em, 2), "unlevered_em": round(em * 0.8, 2)},
            "dscr": {"average_dscr": 1.35, "minimum_dscr": 1.20},
            "cash_on_cash": {"by_year": [{"year": "Y1", "yield": 0.06}]},
            "yields": {"going_in_cap_rate": 0.05, "exit_noi_yield": 0.065},
        },
        "cashflow": {
            "by_year": [
                {"year": "2026", "net_operating_income": 500000},
                {"year": "2030", "net_operating_income": 580000},
            ],
        },
        "fund_waterfall": {
            "summary": {"partnership_equity_multiple": round(em * 0.9, 2)},
        },
    }


class TestScenarioDeltas(unittest.TestCase):
    """Test _apply_scenario_deltas."""

    def test_bull_rent_growth_increases(self):
        inputs = _make_base_inputs()
        bull = STABILIZED_PRESETS["bull"]
        modified = _apply_scenario_deltas(inputs, bull)
        # +50bps
        self.assertAlmostEqual(
            modified["growth_assumptions"]["annual_growth_rate"],
            0.035,
            places=4,
        )

    def test_bear_exit_cap_increases(self):
        inputs = _make_base_inputs()
        bear = STABILIZED_PRESETS["bear"]
        modified = _apply_scenario_deltas(inputs, bear)
        # +25bps
        self.assertAlmostEqual(
            modified["exit_assumptions"]["exit_cap_rate"],
            0.0575,
            places=4,
        )

    def test_bull_vacancy_decreases(self):
        inputs = _make_base_inputs()
        bull = STABILIZED_PRESETS["bull"]
        modified = _apply_scenario_deltas(inputs, bull)
        # -100bps
        self.assertAlmostEqual(
            modified["physical_vacancy_curve"][0]["vacancy_rate"],
            0.04,
            places=4,
        )

    def test_bear_vacancy_floored_at_zero(self):
        inputs = _make_base_inputs()
        inputs["physical_vacancy_curve"][0]["vacancy_rate"] = 0.01
        bear = ScenarioPresets("Bear", -50, 25, 200, 25)
        modified = _apply_scenario_deltas(inputs, bear)
        # 1% + 2% = 3%, not negative
        self.assertAlmostEqual(
            modified["physical_vacancy_curve"][0]["vacancy_rate"],
            0.03,
            places=4,
        )

    def test_opex_growth_adjusted(self):
        inputs = _make_base_inputs()
        bear = STABILIZED_PRESETS["bear"]
        modified = _apply_scenario_deltas(inputs, bear)
        # +25bps on 3.0% = 3.25%
        self.assertAlmostEqual(
            modified["opex_table"][0]["growth_rate"],
            0.0325,
            places=4,
        )

    def test_original_inputs_not_mutated(self):
        inputs = _make_base_inputs()
        original_rg = inputs["growth_assumptions"]["annual_growth_rate"]
        _apply_scenario_deltas(inputs, STABILIZED_PRESETS["bull"])
        self.assertEqual(
            inputs["growth_assumptions"]["annual_growth_rate"],
            original_rg,
        )

    def test_market_rent_curve_scaled(self):
        inputs = _make_base_inputs()
        bull = STABILIZED_PRESETS["bull"]
        modified = _apply_scenario_deltas(inputs, bull)
        # Year 1 rent unchanged, Year 2 scaled by (1 + 0.005)^1
        self.assertAlmostEqual(modified["market_rent_curve"][0]["market_rent"], 1200, places=0)
        self.assertAlmostEqual(modified["market_rent_curve"][1]["market_rent"], 1236 * 1.005, places=1)


class TestExtractMetrics(unittest.TestCase):
    """Test _extract_comparison_metrics."""

    def test_extracts_all_metrics(self):
        results = _mock_engine_runner(_make_base_inputs())
        metrics = _extract_comparison_metrics(results)
        self.assertIn("levered_irr", metrics)
        self.assertIn("levered_em", metrics)
        self.assertIn("noi_year_1", metrics)
        self.assertIn("noi_exit_year", metrics)
        self.assertIn("average_dscr", metrics)

    def test_handles_empty_results(self):
        metrics = _extract_comparison_metrics({})
        self.assertIsNone(metrics.get("levered_irr"))
        self.assertEqual(metrics["noi_year_1"], 0)


class TestRunScenarios(unittest.TestCase):
    """Test the full run_scenarios flow."""

    def test_returns_three_scenarios(self):
        inputs = _make_base_inputs()
        result = run_scenarios(inputs, engine_runner=_mock_engine_runner)
        self.assertIn("bull", result)
        self.assertIn("base", result)
        self.assertIn("bear", result)
        self.assertIn("comparison", result)

    def test_bull_irr_exceeds_base(self):
        inputs = _make_base_inputs()
        result = run_scenarios(inputs, engine_runner=_mock_engine_runner)
        comp = result["comparison"]
        bull_irr = comp["bull"]["metrics"]["levered_irr"]
        base_irr = comp["base"]["metrics"]["levered_irr"]
        bear_irr = comp["bear"]["metrics"]["levered_irr"]
        self.assertGreater(bull_irr, base_irr)
        self.assertGreater(base_irr, bear_irr)

    def test_comparison_has_assumptions(self):
        inputs = _make_base_inputs()
        result = run_scenarios(inputs, engine_runner=_mock_engine_runner)
        comp = result["comparison"]
        for name in ["bull", "base", "bear"]:
            self.assertIn("assumptions", comp[name])
            self.assertIn("rent_growth", comp[name]["assumptions"])
            self.assertIn("exit_cap_rate", comp[name]["assumptions"])

    def test_value_add_presets(self):
        inputs = _make_base_inputs()
        result = run_scenarios(
            inputs,
            presets=VALUE_ADD_PRESETS,
            engine_runner=_mock_engine_runner,
        )
        comp = result["comparison"]
        # Value-add bull has +75bps rent growth vs stabilized +50bps
        bull_rg = comp["bull"]["assumptions"]["rent_growth"]
        self.assertAlmostEqual(bull_rg, 0.0375, places=4)

    def test_base_results_match_direct_run(self):
        inputs = _make_base_inputs()
        direct = _mock_engine_runner(inputs)
        scenario = run_scenarios(inputs, engine_runner=_mock_engine_runner)
        # Base scenario should match direct run
        self.assertEqual(
            scenario["base"]["metrics"]["irr"]["levered_irr"],
            direct["metrics"]["irr"]["levered_irr"],
        )


class TestBuildAssumptionsSummary(unittest.TestCase):
    def test_base_case_label(self):
        inputs = _make_base_inputs()
        summary = _build_assumptions_summary(inputs, None)
        self.assertEqual(summary["scenario"], "Base")

    def test_bull_case_label(self):
        inputs = _make_base_inputs()
        summary = _build_assumptions_summary(inputs, STABILIZED_PRESETS["bull"])
        self.assertEqual(summary["scenario"], "Bull")


class TestRefiVsSell(unittest.TestCase):
    """Test refi vs sell comparison."""

    def test_returns_both_scenarios(self):
        inputs = _make_base_inputs()
        refi_terms = {
            "commitment": 800000,
            "rate": 0.055,
            "amort_years": 30,
            "io_months": 12,
            "loan_start_month": "2028-01",
            "loan_closing_costs": 15000,
        }
        result = run_refi_vs_sell(
            inputs,
            refi_year=3,
            sell_year=5,
            refi_loan_terms=refi_terms,
            engine_runner=_mock_engine_runner,
        )
        self.assertIn("sell", result)
        self.assertIn("refi_hold", result)
        self.assertIn("comparison", result)

    def test_comparison_has_delta(self):
        inputs = _make_base_inputs()
        refi_terms = {
            "commitment": 800000,
            "rate": 0.055,
            "amort_years": 30,
            "io_months": 12,
            "loan_start_month": "2028-01",
        }
        result = run_refi_vs_sell(
            inputs,
            refi_year=3,
            sell_year=5,
            refi_loan_terms=refi_terms,
            engine_runner=_mock_engine_runner,
        )
        comp = result["comparison"]
        self.assertIn("delta", comp)
        self.assertIn("irr_spread", comp["delta"])
        self.assertIn("em_spread", comp["delta"])

    def test_refi_hold_has_additional_debt(self):
        inputs = _make_base_inputs()
        refi_terms = {
            "commitment": 800000,
            "rate": 0.055,
            "amort_years": 30,
            "io_months": 12,
            "loan_start_month": "2028-01",
        }
        # Mock that captures the inputs to verify additional debt was added
        captured_inputs = []
        def capturing_runner(inp):
            captured_inputs.append(inp)
            return _mock_engine_runner(inp)

        run_refi_vs_sell(
            inputs,
            refi_year=3,
            sell_year=5,
            refi_loan_terms=refi_terms,
            engine_runner=capturing_runner,
        )
        # Second call (refi scenario) should have additional_debt_terms
        self.assertEqual(len(captured_inputs), 2)
        refi_inp = captured_inputs[1]
        self.assertIn("additional_debt_terms", refi_inp)
        self.assertEqual(len(refi_inp["additional_debt_terms"]), 1)
        self.assertEqual(refi_inp["additional_debt_terms"][0]["commitment"], 800000)

    def test_refi_hold_retires_original_debt_at_refi_year(self):
        inputs = _make_base_inputs()
        inputs["debt_terms"] = {
            "commitment": 1000000,
            "rate": 0.06,
            "amort_years": 30,
            "io_months": 12,
        }
        refi_terms = {
            "commitment": 800000,
            "rate": 0.055,
            "amort_years": 30,
            "io_months": 12,
            "loan_start_month": "2028-01",
        }
        captured_inputs = []
        def capturing_runner(inp):
            captured_inputs.append(inp)
            return _mock_engine_runner(inp)

        run_refi_vs_sell(
            inputs,
            refi_year=3,
            sell_year=5,
            refi_loan_terms=refi_terms,
            engine_runner=capturing_runner,
        )

        refi_inp = captured_inputs[1]
        self.assertEqual(refi_inp["debt_terms"]["term_months"], 24)
        self.assertTrue(refi_inp["debt_terms"]["skip_maturity_ds"])

    def test_refi_vs_sell_uses_refi_and_sell_years_for_timing(self):
        inputs = _make_base_inputs()
        inputs["time_grid"] = {
            "analysis_start_date": "2026-01-01",
            "analysis_end_date": "2035-12-01",
        }
        refi_terms = {"commitment": 800000, "rate": 0.055, "amort_years": 30, "io_months": 12}
        captured_inputs = []
        def capturing_runner(inp):
            captured_inputs.append(inp)
            return _mock_engine_runner(inp)

        run_refi_vs_sell(
            inputs,
            refi_year=3,
            sell_year=5,
            refi_loan_terms=refi_terms,
            engine_runner=capturing_runner,
        )

        self.assertEqual(captured_inputs[0]["time_grid"]["analysis_end_date"], "2030-12-01")
        self.assertEqual(captured_inputs[1]["time_grid"]["analysis_end_date"], "2030-12-01")
        self.assertEqual(captured_inputs[1]["additional_debt_terms"][0]["loan_start_month"], "2028-01")

    def test_original_inputs_not_mutated(self):
        inputs = _make_base_inputs()
        refi_terms = {"commitment": 800000, "rate": 0.055, "amort_years": 30, "io_months": 12, "loan_start_month": "2028-01"}
        run_refi_vs_sell(inputs, 3, 5, refi_terms, engine_runner=_mock_engine_runner)
        self.assertNotIn("additional_debt_terms", inputs)


if __name__ == "__main__":
    unittest.main()
