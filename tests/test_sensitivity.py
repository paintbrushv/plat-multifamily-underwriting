"""
Tests for Sensitivity Engine Module

Tests cover:
- 2-way matrix generation
- Parameter application
- Metric extraction
- Standard sensitivity types
- Custom configurations
- Edge cases
"""
import unittest

from engine.modules.sensitivity import (
    run_sensitivity_analysis,
    create_standard_sensitivity,
    _apply_parameter_override,
    _extract_metric,
)


class TestParameterOverride(unittest.TestCase):
    """Test parameter override application."""

    def test_rent_growth_override(self):
        """Should apply rent growth rate to growth_assumptions."""
        deal = {"growth_assumptions": {"annual_growth_rate": 0.03}}
        modified = _apply_parameter_override(deal, "rent_growth_rate", 0.05)

        self.assertAlmostEqual(
            modified["growth_assumptions"]["annual_growth_rate"], 0.05, places=4
        )

    def test_exit_cap_override(self):
        """Should apply exit cap rate to exit_assumptions."""
        deal = {"exit_assumptions": {"exit_cap_rate": 0.055}}
        modified = _apply_parameter_override(deal, "exit_cap_rate", 0.06)

        self.assertAlmostEqual(
            modified["exit_assumptions"]["exit_cap_rate"], 0.06, places=4
        )

    def test_creates_missing_structure(self):
        """Should create missing nested structures."""
        deal = {}
        modified = _apply_parameter_override(deal, "rent_growth_rate", 0.04)

        self.assertIn("growth_assumptions", modified)
        self.assertAlmostEqual(
            modified["growth_assumptions"]["annual_growth_rate"], 0.04, places=4
        )

    def test_purchase_price_variance(self):
        """Should apply variance to existing purchase price."""
        deal = {"purchase_assumptions": {"purchase_price": 10000000}}
        modified = _apply_parameter_override(deal, "purchase_price_variance", 0.05)

        # 10M * 1.05 = 10.5M
        self.assertAlmostEqual(
            modified["purchase_assumptions"]["purchase_price"], 10500000, places=0
        )

    def test_ltv_ratio_updates_debt_and_equity_basis(self):
        """LTV overrides must update both loan amount and equity denominator."""
        deal = {
            "purchase_assumptions": {
                "purchase_price": 10000000,
                "equity_contribution": 3500000,
                "total_equity_basis": 3700000,
            },
            "debt_terms": {"commitment": 6500000},
        }

        modified = _apply_parameter_override(deal, "ltv_ratio", 0.75)

        self.assertAlmostEqual(modified["debt_terms"]["commitment"], 7500000, places=0)
        self.assertAlmostEqual(
            modified["purchase_assumptions"]["equity_contribution"], 2500000, places=0
        )
        # Preserve the original $200k non-equity basis add-on.
        self.assertAlmostEqual(
            modified["purchase_assumptions"]["total_equity_basis"], 2700000, places=0
        )

    def test_preserves_other_fields(self):
        """Override should not affect unrelated fields."""
        deal = {
            "growth_assumptions": {"annual_growth_rate": 0.03},
            "exit_assumptions": {"exit_cap_rate": 0.055},
            "other_field": "preserved",
        }
        modified = _apply_parameter_override(deal, "rent_growth_rate", 0.05)

        self.assertEqual(modified["other_field"], "preserved")
        self.assertAlmostEqual(
            modified["exit_assumptions"]["exit_cap_rate"], 0.055, places=4
        )


class TestMetricExtraction(unittest.TestCase):
    """Test metric extraction from results."""

    def test_extract_levered_irr(self):
        """Should extract levered IRR from nested structure."""
        result = {"irr": {"levered_irr": 0.18, "unlevered_irr": 0.12}}
        value = _extract_metric(result, "levered_irr")
        self.assertAlmostEqual(value, 0.18, places=4)

    def test_extract_dscr(self):
        """Should extract DSCR metrics."""
        result = {"dscr": {"average_dscr": 1.35, "minimum_dscr": 1.20}}
        avg = _extract_metric(result, "average_dscr")
        minimum = _extract_metric(result, "minimum_dscr")

        self.assertAlmostEqual(avg, 1.35, places=2)
        self.assertAlmostEqual(minimum, 1.20, places=2)

    def test_missing_metric_returns_none(self):
        """Should return None for missing metrics."""
        result = {"irr": {"levered_irr": 0.18}}
        value = _extract_metric(result, "nonexistent_metric")
        self.assertIsNone(value)


class TestSensitivityAnalysis(unittest.TestCase):
    """Test full sensitivity analysis."""

    def test_basic_2way_matrix(self):
        """Should generate 2-way matrix with correct dimensions."""
        deal = {
            "purchase_assumptions": {"purchase_price": 10000000, "equity_contribution": 3000000},
            "exit_assumptions": {"exit_cap_rate": 0.055},
            "growth_assumptions": {"annual_growth_rate": 0.03},
            "debt_terms": {"commitment": 7000000, "rate": 0.06},
        }

        config = {
            "row_input": {
                "parameter": "rent_growth_rate",
                "values": [0.02, 0.03, 0.04],
            },
            "column_input": {
                "parameter": "exit_cap_rate",
                "values": [0.05, 0.055, 0.06],
            },
            "metrics": ["levered_irr"],
        }

        result = run_sensitivity_analysis(deal, config)

        # Should have 3x3 matrix
        matrix = result["sensitivity_results"]["levered_irr"]["matrix"]
        self.assertEqual(len(matrix), 3)  # 3 rows
        self.assertEqual(len(matrix[0]), 3)  # 3 columns

    def test_all_scenarios_calculated(self):
        """Should calculate all row × column scenarios."""
        deal = {
            "purchase_assumptions": {"purchase_price": 10000000, "equity_contribution": 3000000},
        }

        config = {
            "row_input": {
                "parameter": "rent_growth_rate",
                "values": [0.02, 0.03, 0.04, 0.05],
            },
            "column_input": {
                "parameter": "exit_cap_rate",
                "values": [0.05, 0.055],
            },
            "metrics": ["levered_irr"],
        }

        result = run_sensitivity_analysis(deal, config)

        # Should have 4 × 2 = 8 scenarios
        self.assertEqual(result["summary"]["total_scenarios"], 8)
        self.assertEqual(len(result["scenarios"]), 8)

    def test_base_case_identification(self):
        """Should correctly identify base case."""
        deal = {
            "purchase_assumptions": {"purchase_price": 10000000, "equity_contribution": 3000000},
        }

        config = {
            "row_input": {
                "parameter": "rent_growth_rate",
                "values": [0.02, 0.03, 0.04],
            },
            "column_input": {
                "parameter": "exit_cap_rate",
                "values": [0.05, 0.055, 0.06],
            },
            "metrics": ["levered_irr"],
            "base_case": {"row_index": 1, "column_index": 1},
        }

        result = run_sensitivity_analysis(deal, config)

        # Base case should be at [1, 1]
        irr_result = result["sensitivity_results"]["levered_irr"]
        self.assertEqual(irr_result["base_case_position"], [1, 1])
        self.assertIsNotNone(irr_result["base_case_value"])

    def test_min_max_tracking(self):
        """Should track min and max values."""
        deal = {
            "purchase_assumptions": {"purchase_price": 10000000, "equity_contribution": 3000000},
        }

        config = {
            "row_input": {
                "parameter": "rent_growth_rate",
                "values": [0.02, 0.05],
            },
            "column_input": {
                "parameter": "exit_cap_rate",
                "values": [0.045, 0.065],
            },
            "metrics": ["levered_irr"],
        }

        result = run_sensitivity_analysis(deal, config)

        irr_result = result["sensitivity_results"]["levered_irr"]
        self.assertIsNotNone(irr_result["min_value"])
        self.assertIsNotNone(irr_result["max_value"])
        self.assertLess(irr_result["min_value"], irr_result["max_value"])

    def test_multiple_metrics(self):
        """Should calculate multiple metrics in single pass."""
        deal = {
            "purchase_assumptions": {"purchase_price": 10000000, "equity_contribution": 3000000},
        }

        config = {
            "row_input": {
                "parameter": "rent_growth_rate",
                "values": [0.03, 0.04],
            },
            "column_input": {
                "parameter": "exit_cap_rate",
                "values": [0.05, 0.06],
            },
            "metrics": ["levered_irr", "levered_em", "average_dscr"],
        }

        result = run_sensitivity_analysis(deal, config)

        # Should have results for all three metrics
        self.assertIn("levered_irr", result["sensitivity_results"])
        self.assertIn("levered_em", result["sensitivity_results"])
        self.assertIn("average_dscr", result["sensitivity_results"])


class TestStandardSensitivity(unittest.TestCase):
    """Test standard sensitivity configurations."""

    def test_exit_cap_rent_growth(self):
        """Should create exit cap × rent growth sensitivity."""
        deal = {
            "purchase_assumptions": {"purchase_price": 10000000, "equity_contribution": 3000000},
        }

        result = create_standard_sensitivity(deal, "exit_cap_rent_growth")

        # Should have 5x5 matrix (standard config)
        irr_result = result["sensitivity_results"]["levered_irr"]
        self.assertEqual(len(irr_result["matrix"]), 5)
        self.assertEqual(len(irr_result["matrix"][0]), 5)

    def test_price_exit_cap(self):
        """Should create price × exit cap sensitivity."""
        deal = {
            "purchase_assumptions": {"purchase_price": 10000000, "equity_contribution": 3000000},
        }

        result = create_standard_sensitivity(deal, "price_exit_cap")

        # Should have 5x4 matrix
        irr_result = result["sensitivity_results"]["levered_irr"]
        self.assertEqual(len(irr_result["matrix"]), 5)
        self.assertEqual(len(irr_result["matrix"][0]), 4)

    def test_interest_ltv(self):
        """Should create interest × LTV sensitivity."""
        deal = {
            "purchase_assumptions": {"purchase_price": 10000000, "equity_contribution": 3000000},
            "debt_terms": {"commitment": 7000000, "rate": 0.06},
        }

        result = create_standard_sensitivity(deal, "interest_ltv")

        # Should include DSCR metric
        self.assertIn("average_dscr", result["sensitivity_results"])

    def test_unknown_type_raises(self):
        """Should raise error for unknown sensitivity type."""
        deal = {}
        with self.assertRaises(ValueError):
            create_standard_sensitivity(deal, "unknown_type")


class TestCustomModelRunner(unittest.TestCase):
    """Test custom model runner integration."""

    def test_custom_runner_called(self):
        """Should call custom model runner for each scenario."""
        call_count = [0]

        def custom_runner(deal):
            call_count[0] += 1
            return {
                "irr": {"levered_irr": 0.15},
                "equity_multiple": {"levered_em": 2.0},
            }

        deal = {}
        config = {
            "row_input": {"parameter": "rent_growth_rate", "values": [0.03, 0.04]},
            "column_input": {"parameter": "exit_cap_rate", "values": [0.05, 0.06]},
            "metrics": ["levered_irr"],
        }

        result = run_sensitivity_analysis(deal, config, model_runner=custom_runner)

        # Should call runner 4 times (2 × 2)
        self.assertEqual(call_count[0], 4)

    def test_custom_runner_values_used(self):
        """Should use values from custom runner."""

        def custom_runner(deal):
            # Return different IRR based on rent growth
            growth = deal.get("growth_assumptions", {}).get("annual_growth_rate", 0.03)
            irr = 0.10 + growth  # 10% + growth rate
            return {"irr": {"levered_irr": irr}}

        deal = {"growth_assumptions": {"annual_growth_rate": 0.03}}
        config = {
            "row_input": {"parameter": "rent_growth_rate", "values": [0.02, 0.04]},
            "column_input": {"parameter": "exit_cap_rate", "values": [0.05]},
            "metrics": ["levered_irr"],
        }

        result = run_sensitivity_analysis(deal, config, model_runner=custom_runner)

        matrix = result["sensitivity_results"]["levered_irr"]["matrix"]
        # Row 0: 10% + 2% = 12%
        # Row 1: 10% + 4% = 14%
        self.assertAlmostEqual(matrix[0][0], 0.12, places=2)
        self.assertAlmostEqual(matrix[1][0], 0.14, places=2)

    def test_runner_failures_warn_and_count_failed_cells(self):
        """Sensitivity failures should be visible instead of silent None cells."""

        def custom_runner(deal):
            growth = deal.get("growth_assumptions", {}).get("annual_growth_rate")
            if growth == 0.04:
                raise RuntimeError("engine exploded")
            return {"irr": {"levered_irr": 0.12}}

        deal = {"growth_assumptions": {"annual_growth_rate": 0.03}}
        config = {
            "row_input": {"parameter": "rent_growth_rate", "values": [0.02, 0.04]},
            "column_input": {"parameter": "exit_cap_rate", "values": [0.05]},
            "metrics": ["levered_irr"],
        }

        with self.assertWarnsRegex(RuntimeWarning, "Sensitivity cell.*engine exploded"):
            result = run_sensitivity_analysis(deal, config, model_runner=custom_runner)

        matrix = result["sensitivity_results"]["levered_irr"]["matrix"]
        self.assertEqual(matrix, [[0.12], [None]])
        self.assertEqual(result["summary"]["failed_cell_count"], 1)


class TestLabels(unittest.TestCase):
    """Test label handling."""

    def test_custom_labels(self):
        """Should use custom labels when provided."""
        deal = {}
        config = {
            "row_input": {
                "parameter": "rent_growth_rate",
                "values": [0.02, 0.03],
                "labels": ["Low", "Base"],
            },
            "column_input": {
                "parameter": "exit_cap_rate",
                "values": [0.05, 0.06],
                "labels": ["Aggressive", "Conservative"],
            },
            "metrics": ["levered_irr"],
        }

        result = run_sensitivity_analysis(deal, config)

        irr_result = result["sensitivity_results"]["levered_irr"]
        self.assertEqual(irr_result["row_labels"], ["Low", "Base"])
        self.assertEqual(irr_result["column_labels"], ["Aggressive", "Conservative"])

    def test_default_labels_from_values(self):
        """Should generate labels from values when not provided."""
        deal = {}
        config = {
            "row_input": {
                "parameter": "rent_growth_rate",
                "values": [0.02, 0.03],
            },
            "column_input": {
                "parameter": "exit_cap_rate",
                "values": [0.05, 0.06],
            },
            "metrics": ["levered_irr"],
        }

        result = run_sensitivity_analysis(deal, config)

        irr_result = result["sensitivity_results"]["levered_irr"]
        self.assertEqual(irr_result["row_labels"], ["0.02", "0.03"])
        self.assertEqual(irr_result["column_labels"], ["0.05", "0.06"])


class TestEdgeCases(unittest.TestCase):
    """Test edge cases."""

    def test_single_row_column(self):
        """Should handle 1x1 matrix."""
        deal = {}
        config = {
            "row_input": {"parameter": "rent_growth_rate", "values": [0.03]},
            "column_input": {"parameter": "exit_cap_rate", "values": [0.055]},
            "metrics": ["levered_irr"],
        }

        result = run_sensitivity_analysis(deal, config)

        matrix = result["sensitivity_results"]["levered_irr"]["matrix"]
        self.assertEqual(len(matrix), 1)
        self.assertEqual(len(matrix[0]), 1)
        self.assertEqual(result["summary"]["total_scenarios"], 1)


if __name__ == "__main__":
    unittest.main()
