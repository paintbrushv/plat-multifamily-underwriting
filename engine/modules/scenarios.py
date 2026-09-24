"""
Scenario Comparison Engine
===========================
Bull/Base/Bear assumption deltas and refi/exit timing comparisons.

Usage:
    from engine.modules.scenarios import run_scenarios, STABILIZED_PRESETS

    # Bull/Base/Bear
    scenario_results = run_scenarios(inputs, presets=STABILIZED_PRESETS)

    # Refi vs Sell
    from engine.modules.scenarios import run_refi_vs_sell
    comparison = run_refi_vs_sell(inputs, refi_year=3, sell_year=5, refi_loan_terms={...})
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional


def _analysis_month_for_year(inputs: Dict[str, Any], analysis_year: int) -> Optional[str]:
    """Return YYYY-MM for the first month of a 1-indexed analysis year."""
    if analysis_year < 1:
        raise ValueError("analysis_year must be >= 1")
    time_grid = inputs.get("time_grid") or {}
    start = time_grid.get("analysis_start_date")
    if not start:
        return None
    year = int(start[:4])
    month = int(start[5:7])
    offset = (analysis_year - 1) * 12
    month_index = (month - 1) + offset
    return f"{year + month_index // 12:04d}-{month_index % 12 + 1:02d}"


def _analysis_end_date_for_year(inputs: Dict[str, Any], analysis_year: int) -> Optional[str]:
    """Return YYYY-MM-01 for the final month of a 1-indexed analysis year."""
    if analysis_year < 1:
        raise ValueError("analysis_year must be >= 1")
    time_grid = inputs.get("time_grid") or {}
    start = time_grid.get("analysis_start_date")
    if not start:
        return None
    year = int(start[:4])
    month = int(start[5:7])
    offset = analysis_year * 12 - 1
    month_index = (month - 1) + offset
    return f"{year + month_index // 12:04d}-{month_index % 12 + 1:02d}-01"


def _truncate_period_rows_to_end(inputs: Dict[str, Any], end_date: str) -> None:
    """Trim top-level start_period/end_period rows to the scenario end month."""
    end_month = end_date[:7]
    for key, rows in list(inputs.items()):
        if not isinstance(rows, list):
            continue
        truncated_rows = []
        changed = False
        for row in rows:
            if not isinstance(row, dict) or "start_period" not in row or "end_period" not in row:
                truncated_rows.append(row)
                continue
            start_period = str(row["start_period"])[:7]
            end_period = str(row["end_period"])[:7]
            if start_period > end_month:
                changed = True
                continue
            if end_period > end_month:
                row = dict(row)
                row["end_period"] = end_month
                changed = True
            truncated_rows.append(row)
        if changed:
            inputs[key] = truncated_rows


@dataclass(frozen=True)
class ScenarioPresets:
    """Preset deltas applied to base case assumptions.

    All values are deltas (additive) unless noted. Rent growth and vacancy
    are in basis points (bps); exit cap is in bps; opex growth is in bps.
    """
    name: str
    rent_growth_delta_bps: int    # +50 = add 50bps to rent growth
    exit_cap_delta_bps: int       # -25 = subtract 25bps from exit cap (higher val)
    vacancy_delta_bps: int        # -100 = subtract 100bps from vacancy (lower)
    opex_growth_delta_bps: int    # -25 = subtract 25bps from opex growth


# Stabilized acquisition defaults (from interview)
STABILIZED_PRESETS = {
    "bull": ScenarioPresets(
        name="Bull",
        rent_growth_delta_bps=50,
        exit_cap_delta_bps=-25,
        vacancy_delta_bps=-100,
        opex_growth_delta_bps=-25,
    ),
    "bear": ScenarioPresets(
        name="Bear",
        rent_growth_delta_bps=-50,
        exit_cap_delta_bps=25,
        vacancy_delta_bps=200,
        opex_growth_delta_bps=25,
    ),
}

# Value-add presets (wider spread for renovation deals)

VALUE_ADD_PRESETS = {
    "bull": ScenarioPresets(
        name="Bull",
        rent_growth_delta_bps=75,
        exit_cap_delta_bps=-25,
        vacancy_delta_bps=-150,
        opex_growth_delta_bps=-25,
    ),
    "bear": ScenarioPresets(
        name="Bear",
        rent_growth_delta_bps=-75,
        exit_cap_delta_bps=50,
        vacancy_delta_bps=300,
        opex_growth_delta_bps=50,
    ),
}


# Default single-variable shocks for tornado sensitivity
DEFAULT_TORNADO_SHOCKS = [
    {"name": "rent_growth", "label": "Rent Growth", "shock_bps": 50,
     "path": "growth_assumptions.annual_growth_rate", "invert": False},
    {"name": "exit_cap_rate", "label": "Exit Cap Rate", "shock_bps": 25,
     "path": "exit_assumptions.exit_cap_rate", "invert": True},
    {"name": "vacancy", "label": "Vacancy", "shock_bps": 200,
     "path": "physical_vacancy_curve[*].vacancy_rate", "invert": True},
    {"name": "opex_growth", "label": "OpEx Growth", "shock_bps": 50,
     "path": "opex_table[*].growth_rate", "invert": True},
    {"name": "rate", "label": "Interest Rate", "shock_bps": 50,
     "path": "debt_terms.rate", "invert": True},
]


def _apply_single_shock(
    inputs: Dict[str, Any],
    path: str,
    delta: float,
) -> Dict[str, Any]:
    """Apply a single shock to a deep copy of inputs at the given dot-path.

    Supports:
      - Simple paths: "exit_assumptions.exit_cap_rate"
      - Array wildcard: "physical_vacancy_curve[*].vacancy_rate"
      - Capital stack detection: "debt_terms.rate" checks for capital_stack
    """
    modified = copy.deepcopy(inputs)

    # Handle rate shock for capital_stack vs debt_terms
    if path == "debt_terms.rate":
        if "capital_stack" in modified:
            for layer in modified["capital_stack"]:
                if layer.get("layer_type", "senior") in ("senior", "mezzanine"):
                    if layer.get("rate_type") == "variable":
                        layer["base_spread"] = layer.get("base_spread", 0) + delta
                    else:
                        layer["rate"] = layer.get("rate", 0.05) + delta
            return modified
        elif "debt_terms" in modified:
            dt = modified["debt_terms"]
            if dt.get("rate_type") == "variable":
                dt["base_spread"] = dt.get("base_spread", 0) + delta
            else:
                dt["rate"] = dt.get("rate", 0.05) + delta
            return modified

    # Handle array wildcard paths: "physical_vacancy_curve[*].vacancy_rate"
    if "[*]" in path:
        array_path, field = path.split("[*].")
        parts = array_path.split(".")
        obj = modified
        for part in parts:
            obj = obj[part]
        for entry in obj:
            entry[field] = entry.get(field, 0) + delta
        return modified

    # Simple dot-path
    parts = path.split(".")
    obj = modified
    for part in parts[:-1]:
        obj = obj.setdefault(part, {})
    obj[parts[-1]] = obj.get(parts[-1], 0) + delta
    return modified


def run_sensitivity_tornado(
    inputs: Dict[str, Any],
    shocks: Optional[List[Dict]] = None,
    engine_runner: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Run single-variable sensitivity shocks and rank by IRR impact.

    For each variable, runs two engine passes (favorable + adverse) and
    computes the total IRR swing. Returns variables sorted descending by impact.

    Args:
        inputs: Validated deal inputs (base case)
        shocks: List of shock definitions. Defaults to DEFAULT_TORNADO_SHOCKS.
        engine_runner: Callable that takes inputs -> results.

    Returns:
        {"variables": [{"name", "label", "shock_bps", "irr_up", "irr_down", "irr_base", "irr_impact"}, ...]}
    """
    if shocks is None:
        shocks = DEFAULT_TORNADO_SHOCKS

    if engine_runner is None:
        from engine.engine import run_underwriting
        def engine_runner(inp):
            return run_underwriting(inp, skip_validation=True)

    # Run base case
    base_results = engine_runner(inputs)
    base_irr = base_results.get("metrics", {}).get("irr", {}).get("levered_irr", 0) or 0

    variables = []
    for shock_def in shocks:
        delta = shock_def["shock_bps"] / 10000.0
        invert = shock_def.get("invert", False)

        # Favorable: positive delta if not inverted, negative if inverted
        fav_delta = -delta if invert else delta
        adv_delta = delta if invert else -delta

        fav_inputs = _apply_single_shock(inputs, shock_def["path"], fav_delta)
        adv_inputs = _apply_single_shock(inputs, shock_def["path"], adv_delta)

        fav_results = engine_runner(fav_inputs)
        adv_results = engine_runner(adv_inputs)

        irr_up = fav_results.get("metrics", {}).get("irr", {}).get("levered_irr", 0) or 0
        irr_down = adv_results.get("metrics", {}).get("irr", {}).get("levered_irr", 0) or 0

        variables.append({
            "name": shock_def["name"],
            "label": shock_def["label"],
            "shock_bps": shock_def["shock_bps"],
            "irr_up": irr_up,
            "irr_down": irr_down,
            "irr_base": base_irr,
            "irr_impact": abs(irr_up - irr_down),
        })

    variables.sort(key=lambda v: v["irr_impact"], reverse=True)
    return {"variables": variables}


def _apply_scenario_deltas(
    inputs: Dict[str, Any],
    presets: ScenarioPresets,
) -> Dict[str, Any]:
    """Apply scenario deltas to a deep copy of inputs."""
    modified = copy.deepcopy(inputs)

    # Rent growth: adjust market_rent_curve growth rates via growth_assumptions
    rg_delta = presets.rent_growth_delta_bps / 10000.0  # bps → decimal
    ga = modified.get("growth_assumptions", {})
    base_rg = ga.get("annual_growth_rate", 0.03)
    ga["annual_growth_rate"] = base_rg + rg_delta
    # Preserve required growth_type field (default to annual_compound)
    if "growth_type" not in ga:
        ga["growth_type"] = "annual_compound"
    modified["growth_assumptions"] = ga

    # Adjust per-year market rent curve if present
    # Market rent entries have absolute values, not growth rates, so we scale
    # future years' rents by the compounded growth delta
    mr_curve = modified.get("market_rent_curve", [])
    if mr_curve and rg_delta != 0:
        # Group by cohort, find each cohort's year-1 rent, scale subsequent
        from collections import defaultdict
        cohort_entries = defaultdict(list)
        for entry in mr_curve:
            cohort_entries[entry["cohort_id"]].append(entry)

        for cohort_id, entries in cohort_entries.items():
            sorted_entries = sorted(entries, key=lambda e: e["start_period"])
            if len(sorted_entries) <= 1:
                continue
            base_rent = sorted_entries[0]["market_rent"]
            for i, entry in enumerate(sorted_entries[1:], 1):
                # Scale this year's rent by compounded delta
                scale = (1 + rg_delta) ** i
                entry["market_rent"] = round(entry["market_rent"] * scale, 2)

    # Exit cap rate
    ec_delta = presets.exit_cap_delta_bps / 10000.0
    ea = modified.get("exit_assumptions", {})
    base_ec = ea.get("exit_cap_rate", 0.055)
    ea["exit_cap_rate"] = base_ec + ec_delta
    modified["exit_assumptions"] = ea

    # Vacancy: adjust physical_vacancy_curve entries
    vac_delta = presets.vacancy_delta_bps / 10000.0
    for entry in modified.get("physical_vacancy_curve", []):
        base_vac = entry.get("vacancy_rate", 0.05)
        entry["vacancy_rate"] = max(0.0, base_vac + vac_delta)

    # OpEx growth: adjust opex_table growth_rate entries
    opex_delta = presets.opex_growth_delta_bps / 10000.0
    for entry in modified.get("opex_table", []):
        base_og = entry.get("growth_rate", 0.03)
        entry["growth_rate"] = max(0.0, base_og + opex_delta)

    return modified


def _extract_comparison_metrics(results: Dict[str, Any]) -> Dict[str, Any]:
    """Extract key metrics from engine results for comparison."""
    metrics = results.get("metrics", {})
    cashflow = results.get("cashflow", {})
    by_year = cashflow.get("by_year", [])

    # NOI Year 1 and exit year
    noi_y1 = by_year[0].get("net_operating_income", 0) if by_year else 0
    noi_exit = by_year[-1].get("net_operating_income", 0) if by_year else 0

    # DSCR
    dscr = metrics.get("dscr", {})

    # Cash-on-cash
    coc_by_year = metrics.get("cash_on_cash", {}).get("by_year", [])
    coc_y1 = coc_by_year[0].get("yield", 0) if coc_by_year else 0

    return {
        "levered_irr": metrics.get("irr", {}).get("levered_irr"),
        "unlevered_irr": metrics.get("irr", {}).get("unlevered_irr"),
        "levered_em": metrics.get("equity_multiple", {}).get("levered_em"),
        "unlevered_em": metrics.get("equity_multiple", {}).get("unlevered_em"),
        "partnership_irr": metrics.get("irr", {}).get("partnership_irr"),
        "partnership_em": metrics.get("equity_multiple", {}).get("partnership_em"),
        "noi_year_1": noi_y1,
        "noi_exit_year": noi_exit,
        "average_dscr": dscr.get("average_dscr"),
        "minimum_dscr": dscr.get("minimum_dscr"),
        "going_in_cap": metrics.get("yields", {}).get("going_in_cap_rate"),
        "cash_on_cash_y1": coc_y1,
    }


def _build_assumptions_summary(
    inputs: Dict[str, Any],
    presets: Optional[ScenarioPresets],
) -> Dict[str, Any]:
    """Summarize key assumptions for a scenario."""
    ga = inputs.get("growth_assumptions", {})
    ea = inputs.get("exit_assumptions", {})
    vac = inputs.get("physical_vacancy_curve", [])
    avg_vac = (
        sum(e.get("vacancy_rate", 0) for e in vac) / len(vac) if vac else 0
    )

    return {
        "scenario": presets.name if presets else "Base",
        "rent_growth": ga.get("annual_growth_rate", 0.03),
        "exit_cap_rate": ea.get("exit_cap_rate", 0.055),
        "avg_vacancy": round(avg_vac, 4),
    }


def run_scenarios(
    inputs: Dict[str, Any],
    presets: Optional[Dict[str, ScenarioPresets]] = None,
    engine_runner: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Run Bull/Base/Bear scenario comparison.

    Args:
        inputs: Validated deal inputs (base case)
        presets: Dict of scenario name → ScenarioPresets. Defaults to STABILIZED_PRESETS.
        engine_runner: Callable that takes inputs → results. Defaults to run_underwriting.

    Returns:
        Dict with keys: bull, base, bear (full results), plus comparison summary.
    """
    if presets is None:
        presets = STABILIZED_PRESETS

    if engine_runner is None:
        from engine.engine import run_underwriting
        # Base case validates; delta-modified scenarios skip validation
        # (deltas are additive bps adjustments to already-validated inputs)
        def _base_runner(inp):
            return run_underwriting(inp)
        def _scenario_runner(inp):
            return run_underwriting(inp, skip_validation=True)
        engine_runner = _base_runner
        scenario_engine_runner = _scenario_runner
    else:
        scenario_engine_runner = engine_runner

    # Run base case
    base_results = engine_runner(inputs)

    # Run scenarios
    scenario_results = {"base": base_results}
    scenario_inputs = {"base": inputs}

    for scenario_name, scenario_presets in presets.items():
        modified_inputs = _apply_scenario_deltas(inputs, scenario_presets)
        scenario_inputs[scenario_name] = modified_inputs
        scenario_results[scenario_name] = scenario_engine_runner(modified_inputs)

    # Build comparison summary
    comparison = {}
    for name, results in scenario_results.items():
        preset = presets.get(name)
        comparison[name] = {
            "metrics": _extract_comparison_metrics(results),
            "assumptions": _build_assumptions_summary(
                scenario_inputs[name], preset
            ),
        }

    return {
        **scenario_results,
        "comparison": comparison,
    }


# ── Refi vs Sell Comparison ──────────────────────────────────────────────


def run_refi_vs_sell(
    inputs: Dict[str, Any],
    refi_year: int,
    sell_year: int,
    refi_loan_terms: Dict[str, Any],
    engine_runner: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Compare hold-and-refi vs sell-at-exit scenarios.

    Creates two variants of the deal:
    - **Sell**: Exit at sell_year (standard disposition)
    - **Refi + Hold**: Refinance at refi_year, continue holding to sell_year

    The refi scenario adds the new loan as an additional_debt_term, extends
    the hold period, and models cash-out refi proceeds.

    Args:
        inputs: Validated deal inputs (base case)
        refi_year: Analysis year to refinance (1-indexed, e.g., 3 = Year 3)
        sell_year: Analysis year to sell (must be >= refi_year)
        refi_loan_terms: Debt terms for the refi loan (commitment, rate, etc.)
            Should include `loan_start_month` matching the refi timing.
        engine_runner: Callable that takes inputs → results.

    Returns:
        Dict with sell, refi_hold scenarios and comparison.
    """
    if engine_runner is None:
        from engine.engine import run_underwriting
        engine_runner = run_underwriting

    if sell_year < refi_year:
        raise ValueError("sell_year must be >= refi_year")

    # Scenario 1: Sell at sell_year.
    sell_inputs = copy.deepcopy(inputs)
    sell_end_date = _analysis_end_date_for_year(inputs, sell_year)
    if sell_end_date:
        sell_inputs.setdefault("time_grid", {})["analysis_end_date"] = sell_end_date
        _truncate_period_rows_to_end(sell_inputs, sell_end_date)
    sell_results = engine_runner(sell_inputs)

    # Scenario 2: Refi at refi_year, hold to sell_year.
    refi_inputs = copy.deepcopy(inputs)
    if sell_end_date:
        refi_inputs.setdefault("time_grid", {})["analysis_end_date"] = sell_end_date
        _truncate_period_rows_to_end(refi_inputs, sell_end_date)

    # Retire the original loan immediately before the refi loan starts so the
    # refi branch does not carry duplicate debt service after refinance.
    if refi_inputs.get("debt_terms"):
        refi_inputs["debt_terms"] = dict(refi_inputs["debt_terms"])
        refi_inputs["debt_terms"]["term_months"] = (refi_year - 1) * 12
        refi_inputs["debt_terms"]["skip_maturity_ds"] = True

    # Add refi loan as additional debt, with refi_year driving timing when the
    # caller does not provide an explicit loan_start_month.
    refi_terms = copy.deepcopy(refi_loan_terms)
    refi_start_month = _analysis_month_for_year(inputs, refi_year)
    if refi_start_month:
        refi_terms.setdefault("loan_start_month", refi_start_month)
    if "additional_debt_terms" not in refi_inputs:
        refi_inputs["additional_debt_terms"] = []
    refi_inputs["additional_debt_terms"].append(refi_terms)

    refi_results = engine_runner(refi_inputs)

    # Extract comparison
    sell_metrics = _extract_comparison_metrics(sell_results)
    refi_metrics = _extract_comparison_metrics(refi_results)

    # Calculate refi cash-out proceeds (new loan amount - payoff of original - costs)
    refi_commitment = refi_loan_terms.get("commitment", 0)
    refi_closing = refi_loan_terms.get("loan_closing_costs", 0)

    return {
        "sell": sell_results,
        "refi_hold": refi_results,
        "comparison": {
            "sell": {
                "label": f"Sell Year {sell_year}",
                "metrics": sell_metrics,
            },
            "refi_hold": {
                "label": f"Refi Year {refi_year}, Sell Year {sell_year}",
                "metrics": refi_metrics,
                "refi_loan_amount": refi_commitment,
                "refi_closing_costs": refi_closing,
            },
            "delta": {
                "irr_spread": (
                    (refi_metrics.get("levered_irr") or 0)
                    - (sell_metrics.get("levered_irr") or 0)
                ),
                "em_spread": (
                    (refi_metrics.get("levered_em") or 0)
                    - (sell_metrics.get("levered_em") or 0)
                ),
            },
        },
    }
