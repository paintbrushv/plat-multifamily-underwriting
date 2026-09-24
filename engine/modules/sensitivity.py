"""
Sensitivity Engine Module

Generates 2-way sensitivity matrices for key metrics.
Varies two inputs simultaneously to show metric outcomes.

See: docs/modules/sensitivity_spec.md for full specification
"""
from __future__ import annotations

import copy
import warnings
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

from engine.modules.util import dec, round2


@dataclass(frozen=True)
class SensitivityScenario:
    """A single sensitivity scenario with row/column values."""

    row_index: int
    col_index: int
    row_value: float
    col_value: float
    metrics: Dict[str, float]


def _apply_parameter_override(
    deal: Dict[str, Any],
    parameter: str,
    value: float,
) -> Dict[str, Any]:
    """
    Apply a parameter override to a deal structure.

    Returns a modified copy of the deal.
    """
    modified = copy.deepcopy(deal)

    # Map parameter names to deal structure locations
    if parameter == "rent_growth_rate":
        if "growth_assumptions" not in modified:
            modified["growth_assumptions"] = {}
        modified["growth_assumptions"]["annual_growth_rate"] = value

    elif parameter == "exit_cap_rate":
        if "exit_assumptions" not in modified:
            modified["exit_assumptions"] = {}
        modified["exit_assumptions"]["exit_cap_rate"] = value

    elif parameter == "purchase_price":
        if "purchase_assumptions" not in modified:
            modified["purchase_assumptions"] = {}
        modified["purchase_assumptions"]["purchase_price"] = value

    elif parameter == "purchase_price_variance":
        # Apply as percentage variance to existing price
        if "purchase_assumptions" in modified:
            base_price = modified["purchase_assumptions"].get("purchase_price", 0)
            modified["purchase_assumptions"]["purchase_price"] = base_price * (1 + value)

    elif parameter == "interest_rate":
        if "debt_terms" not in modified:
            modified["debt_terms"] = {}
        modified["debt_terms"]["rate"] = value

    elif parameter == "vacancy_rate":
        # Apply to all vacancy curve entries
        if "physical_vacancy_curve" in modified:
            for entry in modified["physical_vacancy_curve"]:
                entry["vacancy_rate"] = value

    elif parameter == "opex_growth_rate":
        # Apply to all opex entries
        if "opex_table" in modified:
            for entry in modified["opex_table"]:
                entry["growth_rate"] = value

    elif parameter == "ltv_ratio":
        # Derive loan amount from LTV and purchase price; keep the levered
        # denominator in sync so sensitivity columns actually reflect leverage.
        if "purchase_assumptions" in modified and "debt_terms" in modified:
            purchase = modified["purchase_assumptions"]
            price = dec(purchase.get("purchase_price", 0))
            old_equity = dec(purchase.get("equity_contribution", 0))
            old_basis = dec(purchase.get("total_equity_basis", old_equity))
            non_equity_basis_addon = old_basis - old_equity
            new_debt = price * dec(value)
            new_equity = price - new_debt
            modified["debt_terms"]["commitment"] = float(new_debt)
            purchase["equity_contribution"] = float(new_equity)
            purchase["total_equity_basis"] = float(new_equity + non_equity_basis_addon)

    else:
        # Generic: try to set in top-level or nested dict
        if "." in parameter:
            parts = parameter.split(".")
            target = modified
            for part in parts[:-1]:
                if part not in target:
                    target[part] = {}
                target = target[part]
            target[parts[-1]] = value
        else:
            modified[parameter] = value

    return modified


def _extract_metric(metrics_result: Dict[str, Any], metric_name: str) -> Optional[float]:
    """
    Extract a specific metric from metrics result.

    Returns None if metric not available.
    """
    if not metrics_result:
        return None

    # Map metric names to result paths
    if metric_name == "levered_irr":
        return metrics_result.get("irr", {}).get("levered_irr")
    elif metric_name == "unlevered_irr":
        return metrics_result.get("irr", {}).get("unlevered_irr")
    elif metric_name == "levered_em":
        return metrics_result.get("equity_multiple", {}).get("levered_em")
    elif metric_name == "unlevered_em":
        return metrics_result.get("equity_multiple", {}).get("unlevered_em")
    elif metric_name == "average_dscr":
        return metrics_result.get("dscr", {}).get("average_dscr")
    elif metric_name == "minimum_dscr":
        return metrics_result.get("dscr", {}).get("minimum_dscr")
    elif metric_name == "cash_on_cash_y1":
        coc = metrics_result.get("cash_on_cash", {}).get("by_year", [])
        return coc[0].get("yield") if coc else None
    elif metric_name == "going_in_cap":
        return metrics_result.get("yields", {}).get("going_in_cap_rate")
    elif metric_name == "exit_noi_yield":
        return metrics_result.get("yields", {}).get("exit_noi_yield")
    else:
        # Try direct access
        return metrics_result.get(metric_name)


def run_sensitivity_analysis(
    base_deal: Dict[str, Any],
    sensitivity_config: Dict[str, Any],
    model_runner: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Run 2-way sensitivity analysis.

    Args:
        base_deal: Base deal input structure
        sensitivity_config: Configuration with row/column inputs and metrics
        model_runner: Optional callable that runs full model and returns metrics.
                     If not provided, uses simplified metric calculation.

    Returns:
        Dict with sensitivity_results, summary
    """
    row_config = sensitivity_config["row_input"]
    col_config = sensitivity_config["column_input"]
    requested_metrics = sensitivity_config.get("metrics", ["levered_irr"])
    base_case_config = sensitivity_config.get("base_case", {})

    row_param = row_config["parameter"]
    row_values = row_config["values"]
    row_labels = row_config.get("labels", [str(v) for v in row_values])

    col_param = col_config["parameter"]
    col_values = col_config["values"]
    col_labels = col_config.get("labels", [str(v) for v in col_values])

    base_row_idx = base_case_config.get("row_index", 0)
    base_col_idx = base_case_config.get("column_index", 0)

    # Use provided model runner or default simplified calculator
    if model_runner is None:
        model_runner = _simplified_model_runner

    # Run all scenarios
    scenarios: List[SensitivityScenario] = []
    failed_cell_count = 0
    for r_idx, r_val in enumerate(row_values):
        for c_idx, c_val in enumerate(col_values):
            # Apply both overrides
            modified = _apply_parameter_override(base_deal, row_param, r_val)
            modified = _apply_parameter_override(modified, col_param, c_val)

            # Run model
            try:
                result = model_runner(modified)
                metrics = {}
                for metric in requested_metrics:
                    metrics[metric] = _extract_metric(result, metric)
            except Exception as exc:
                failed_cell_count += 1
                warnings.warn(
                    f"Sensitivity cell ({r_idx}, {c_idx}) failed: {exc}",
                    RuntimeWarning,
                    stacklevel=2,
                )
                # If model fails, record None for all metrics
                metrics = {m: None for m in requested_metrics}

            scenarios.append(
                SensitivityScenario(
                    row_index=r_idx,
                    col_index=c_idx,
                    row_value=r_val,
                    col_value=c_val,
                    metrics=metrics,
                )
            )

    # Build result matrices for each metric
    sensitivity_results: Dict[str, Any] = {}

    for metric in requested_metrics:
        matrix: List[List[Optional[float]]] = []
        for r_idx in range(len(row_values)):
            row: List[Optional[float]] = []
            for c_idx in range(len(col_values)):
                scenario = next(
                    s for s in scenarios if s.row_index == r_idx and s.col_index == c_idx
                )
                value = scenario.metrics.get(metric)
                row.append(round2(dec(value)) if value is not None else None)
            matrix.append(row)

        # Find base case value
        base_scenario = next(
            (s for s in scenarios if s.row_index == base_row_idx and s.col_index == base_col_idx),
            None,
        )
        base_value = base_scenario.metrics.get(metric) if base_scenario else None

        # Calculate min/max
        all_values = [
            v for row in matrix for v in row if v is not None
        ]
        min_val = min(all_values) if all_values else None
        max_val = max(all_values) if all_values else None

        # Calculate variance from base
        if base_value is not None and min_val is not None and max_val is not None:
            min_delta = min_val - base_value
            max_delta = max_val - base_value
        else:
            min_delta = None
            max_delta = None

        sensitivity_results[metric] = {
            "metric_name": _get_metric_display_name(metric),
            "row_parameter": row_param,
            "column_parameter": col_param,
            "row_labels": row_labels,
            "column_labels": col_labels,
            "matrix": matrix,
            "base_case_value": round2(dec(base_value)) if base_value is not None else None,
            "base_case_position": [base_row_idx, base_col_idx],
            "min_value": min_val,
            "max_value": max_val,
            "variance_from_base": {
                "min_delta": round2(dec(min_delta)) if min_delta is not None else None,
                "max_delta": round2(dec(max_delta)) if max_delta is not None else None,
            },
        }

    # Find worst and best case scenarios
    irr_metric = "levered_irr" if "levered_irr" in requested_metrics else requested_metrics[0]
    irr_results = sensitivity_results.get(irr_metric, {})
    irr_matrix = irr_results.get("matrix", [])

    worst_val = None
    worst_pos = None
    best_val = None
    best_pos = None

    for r_idx, row in enumerate(irr_matrix):
        for c_idx, val in enumerate(row):
            if val is not None:
                if worst_val is None or val < worst_val:
                    worst_val = val
                    worst_pos = (r_idx, c_idx)
                if best_val is None or val > best_val:
                    best_val = val
                    best_pos = (r_idx, c_idx)

    # Build summary
    base_metrics = {}
    if base_scenario:
        for metric in requested_metrics:
            val = base_scenario.metrics.get(metric)
            base_metrics[metric] = round2(dec(val)) if val is not None else None

    summary = {
        "total_scenarios": len(scenarios),
        "row_count": len(row_values),
        "column_count": len(col_values),
        "failed_cell_count": failed_cell_count,
        "base_case_metrics": base_metrics,
        "worst_case": {
            "value": worst_val,
            "scenario": f"{row_labels[worst_pos[0]]} {row_param}, {col_labels[worst_pos[1]]} {col_param}"
            if worst_pos
            else None,
        },
        "best_case": {
            "value": best_val,
            "scenario": f"{row_labels[best_pos[0]]} {row_param}, {col_labels[best_pos[1]]} {col_param}"
            if best_pos
            else None,
        },
    }

    return {
        "sensitivity_results": sensitivity_results,
        "scenarios": [
            {
                "row_index": s.row_index,
                "col_index": s.col_index,
                "row_value": s.row_value,
                "col_value": s.col_value,
                "metrics": s.metrics,
            }
            for s in scenarios
        ],
        "summary": summary,
    }


def _get_metric_display_name(metric: str) -> str:
    """Get human-readable name for metric."""
    names = {
        "levered_irr": "Levered IRR",
        "unlevered_irr": "Unlevered IRR",
        "levered_em": "Levered Equity Multiple",
        "unlevered_em": "Unlevered Equity Multiple",
        "average_dscr": "Average DSCR",
        "minimum_dscr": "Minimum DSCR",
        "cash_on_cash_y1": "Cash-on-Cash (Year 1)",
        "going_in_cap": "Going-In Cap Rate",
        "exit_noi_yield": "Exit NOI Yield",
    }
    return names.get(metric, metric)


def _simplified_model_runner(deal: Dict[str, Any]) -> Dict[str, Any]:
    """
    Simplified model runner for testing.

    This provides approximate metric calculations based on key inputs.
    In production, this would be replaced with full model integration.
    """
    # Extract key inputs with defaults
    purchase = deal.get("purchase_assumptions", {})
    exit_assumptions = deal.get("exit_assumptions", {})
    growth = deal.get("growth_assumptions", {})
    debt = deal.get("debt_terms", {})

    purchase_price = float(purchase.get("purchase_price", 10000000))
    equity = float(purchase.get("equity_contribution", purchase_price * 0.30))
    exit_cap = float(exit_assumptions.get("exit_cap_rate", 0.055))
    rent_growth = float(growth.get("annual_growth_rate", 0.03))
    interest_rate = float(debt.get("rate", 0.06))
    loan_amount = float(debt.get("commitment", purchase_price * 0.70))

    # Simplified NOI assumption (5% cap rate on purchase)
    year1_noi = purchase_price * 0.05

    # Project NOI with growth (5 year hold)
    hold_years = 5
    exit_noi = year1_noi * (1 + rent_growth) ** hold_years

    # Exit value
    exit_value = exit_noi / exit_cap
    sale_costs = exit_value * 0.02

    # Simplified debt paydown (interest only for simplicity)
    remaining_debt = loan_amount * 0.95  # Assume 5% paydown

    # Net proceeds
    net_proceeds = exit_value - sale_costs - remaining_debt

    # Annual cash flows (simplified)
    annual_cf = year1_noi - (loan_amount * interest_rate)
    total_distributions = annual_cf * hold_years + net_proceeds

    # Calculate metrics
    levered_em = total_distributions / equity if equity > 0 else None

    # IRR approximation (simplified)
    if equity > 0:
        # Use basic IRR approximation: (EM^(1/n) - 1)
        if levered_em and levered_em > 0:
            levered_irr = (levered_em ** (1 / hold_years)) - 1
        else:
            levered_irr = None
    else:
        levered_irr = None

    # DSCR
    debt_service = loan_amount * interest_rate
    dscr = year1_noi / debt_service if debt_service > 0 else None

    # Cash on cash
    coc = annual_cf / equity if equity > 0 else None

    # Unlevered metrics
    unlevered_cf = year1_noi
    total_unlev_dist = unlevered_cf * hold_years + (exit_value - sale_costs)
    unlevered_em = total_unlev_dist / purchase_price if purchase_price > 0 else None
    unlevered_irr = (unlevered_em ** (1 / hold_years)) - 1 if unlevered_em and unlevered_em > 0 else None

    return {
        "irr": {
            "levered_irr": levered_irr,
            "unlevered_irr": unlevered_irr,
        },
        "equity_multiple": {
            "levered_em": levered_em,
            "unlevered_em": unlevered_em,
        },
        "dscr": {
            "average_dscr": dscr,
            "minimum_dscr": dscr,
        },
        "cash_on_cash": {
            "by_year": [{"year": "Y1", "yield": coc}],
        },
        "yields": {
            "going_in_cap_rate": year1_noi / purchase_price if purchase_price > 0 else None,
            "exit_noi_yield": exit_noi / purchase_price if purchase_price > 0 else None,
        },
    }


def create_standard_sensitivity(
    base_deal: Dict[str, Any],
    sensitivity_type: str = "exit_cap_rent_growth",
    model_runner: Optional[Callable] = None,
) -> Dict[str, Any]:
    """
    Create a standard sensitivity analysis with preset configurations.

    Args:
        base_deal: Base deal inputs
        sensitivity_type: Type of analysis (exit_cap_rent_growth, price_exit_cap, etc.)
        model_runner: Optional custom model runner

    Returns:
        Sensitivity results
    """
    configs = {
        "exit_cap_rent_growth": {
            "row_input": {
                "parameter": "rent_growth_rate",
                "values": [0.02, 0.025, 0.03, 0.035, 0.04],
                "labels": ["2.0%", "2.5%", "3.0%", "3.5%", "4.0%"],
            },
            "column_input": {
                "parameter": "exit_cap_rate",
                "values": [0.045, 0.05, 0.055, 0.06, 0.065],
                "labels": ["4.5%", "5.0%", "5.5%", "6.0%", "6.5%"],
            },
            "metrics": ["levered_irr", "levered_em"],
            "base_case": {"row_index": 2, "column_index": 2},
        },
        "price_exit_cap": {
            "row_input": {
                "parameter": "purchase_price_variance",
                "values": [-0.10, -0.05, 0.0, 0.05, 0.10],
                "labels": ["-10%", "-5%", "Base", "+5%", "+10%"],
            },
            "column_input": {
                "parameter": "exit_cap_rate",
                "values": [0.05, 0.055, 0.06, 0.065],
                "labels": ["5.0%", "5.5%", "6.0%", "6.5%"],
            },
            "metrics": ["levered_irr", "levered_em"],
            "base_case": {"row_index": 2, "column_index": 1},
        },
        "interest_ltv": {
            "row_input": {
                "parameter": "interest_rate",
                "values": [0.05, 0.055, 0.06, 0.065, 0.07],
                "labels": ["5.0%", "5.5%", "6.0%", "6.5%", "7.0%"],
            },
            "column_input": {
                "parameter": "ltv_ratio",
                "values": [0.60, 0.65, 0.70, 0.75],
                "labels": ["60%", "65%", "70%", "75%"],
            },
            "metrics": ["levered_irr", "average_dscr"],
            "base_case": {"row_index": 2, "column_index": 2},
        },
    }

    if sensitivity_type not in configs:
        raise ValueError(f"Unknown sensitivity type: {sensitivity_type}")

    return run_sensitivity_analysis(base_deal, configs[sensitivity_type], model_runner)
