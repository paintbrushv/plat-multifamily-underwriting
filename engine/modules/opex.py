"""
Operating Expenses Module

Calculates monthly operating expenses with support for:
- Multiple calculation types (fixed, per-unit, per-sqft, percent of revenue)
- Annual growth rate application
- Recoverable vs non-recoverable expense tracking

See: docs/modules/opex_spec.md for full specification
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional

from engine.modules.time_grid import TimeGrid
from engine.modules.util import dec, get_total_units, month_id, round2, parse_month


@dataclass(frozen=True)
class OpexCategoryMonthResult:
    """Intermediate result for a single category in a single month."""

    month: str
    category: str
    expense: Decimal
    growth_factor: Decimal
    recoverable: bool


def _calculate_years_elapsed(month: str, analysis_start: str) -> int:
    """Calculate integer years elapsed since analysis start."""
    current = parse_month(month)
    start = parse_month(analysis_start)
    years = current.year - start.year
    # If we haven't reached the anniversary month yet, subtract 1
    if (current.month, current.day) < (start.month, start.day):
        years -= 1
    return max(0, years)


def _compute_phased_growth(
    phases: List[Dict[str, Any]],
    effective_years: int,
) -> Decimal:
    """Compute growth factor with multi-phase rates.

    Phases define rate changes at specific year boundaries. E.g.:
      [{"through_year": 1, "rate": 0.0},
       {"from_year": 2, "rate": 0.02}]
    means 0% growth for year 1, then 2% growth from year 2 onward.
    """
    if effective_years <= 0:
        return Decimal("1")

    factor = Decimal("1")
    for yr in range(1, effective_years + 1):
        # Find the applicable phase for this year
        rate = Decimal("0")
        for phase in phases:
            if "through_year" in phase and yr <= phase["through_year"]:
                rate = dec(phase["rate"])
                break
            elif "from_year" in phase and yr >= phase["from_year"]:
                rate = dec(phase["rate"])
                # Don't break — later phases might override
        factor *= (Decimal("1") + rate)
    return factor


def _get_total_sqft(unit_cohorts: List[Dict[str, Any]]) -> Decimal:
    """Sum total square footage across all cohorts."""
    total = Decimal("0")
    for c in unit_cohorts:
        unit_count = dec(c["unit_count"])
        sqft = dec(c.get("sqft", 0))
        total += unit_count * sqft
    return total


def _calculate_expense_for_month(
    category: Dict[str, Any],
    month: str,
    analysis_start: str,
    total_units: Decimal,
    total_sqft: Decimal,
    revenue_for_month: Optional[Decimal],
) -> OpexCategoryMonthResult:
    """Calculate expense for a single category in a single month."""
    category_name = category["category_name"]
    calc_type = category["calculation_type"]
    base_value = dec(category["base_value"])
    growth_rate = dec(category.get("growth_rate", 0))
    recoverable = category["recoverable_flag"]

    # Calculate growth factor (with optional delayed start)
    years_elapsed = _calculate_years_elapsed(month, analysis_start)
    growth_start_year = int(category.get("growth_start_year", 0))
    effective_growth_years = max(0, years_elapsed - growth_start_year)

    # Support multi-phase growth (e.g., RE Tax reassessment mid-hold)
    growth_phases = category.get("growth_phases")
    if growth_phases:
        growth_factor = _compute_phased_growth(growth_phases, effective_growth_years)
    else:
        growth_factor = (Decimal("1") + growth_rate) ** effective_growth_years
    adjusted_base = base_value * growth_factor

    # Calculate expense based on calculation type
    if calc_type == "fixed_annual":
        # base_value is total annual, spread evenly
        expense = adjusted_base / Decimal("12")
    elif calc_type == "fixed_monthly":
        # base_value is monthly amount
        expense = adjusted_base
    elif calc_type == "per_unit":
        # base_value is annual $/unit
        expense = adjusted_base * total_units / Decimal("12")
    elif calc_type == "per_unit_monthly":
        # base_value is monthly $/unit
        expense = adjusted_base * total_units
    elif calc_type == "per_sqft":
        # base_value is annual $/sqft
        expense = adjusted_base * total_sqft / Decimal("12")
    elif calc_type == "percent_egr":
        # base_value is percentage (0.04 = 4%)
        if revenue_for_month is None:
            expense = Decimal("0")
        else:
            expense = adjusted_base * revenue_for_month
    elif calc_type == "percent_rent":
        # Same as percent_egr for now (rent is primary revenue)
        if revenue_for_month is None:
            expense = Decimal("0")
        else:
            expense = adjusted_base * revenue_for_month
    else:
        raise ValueError(f"Unknown calculation_type: {calc_type}")

    return OpexCategoryMonthResult(
        month=month,
        category=category_name,
        expense=expense,
        growth_factor=growth_factor,
        recoverable=recoverable,
    )


def compute_opex(
    time_grid: TimeGrid,
    unit_cohorts: List[Dict[str, Any]],
    opex_table: List[Dict[str, Any]],
    revenue_by_month: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Compute monthly operating expenses with growth and categorization.

    Args:
        time_grid: Authoritative time grid
        unit_cohorts: Unit mix for per_unit/per_sqft calculations
        opex_table: Expense category definitions
        revenue_by_month: Revenue totals for percent-based calculations (optional)

    Returns:
        Dict with by_category_by_month, totals_by_month, totals_by_year
    """
    if not opex_table:
        # Return empty structure if no expenses defined
        return {
            "by_category_by_month": [],
            "totals_by_month": [
                {"month": m, "total_opex": 0.0, "recoverable_opex": 0.0, "non_recoverable_opex": 0.0}
                for m in time_grid.month_ids
            ],
            "totals_by_year": [
                {"year": y, "total_opex": 0.0, "recoverable_opex": 0.0, "non_recoverable_opex": 0.0}
                for y in time_grid.year_ids
            ],
        }

    total_units = get_total_units(unit_cohorts)
    total_sqft = _get_total_sqft(unit_cohorts)
    analysis_start = time_grid.month_ids[0]

    # Build revenue lookup if provided
    revenue_lookup: Dict[str, Decimal] = {}
    if revenue_by_month:
        for row in revenue_by_month:
            m = month_id(row["month"])
            # Use net_total_revenue if available, otherwise sum net_rent + net_programs
            if "net_total_revenue" in row:
                revenue_lookup[m] = dec(row["net_total_revenue"])
            else:
                net_rent = dec(row.get("net_rent", 0))
                net_programs = dec(row.get("net_programs", 0))
                revenue_lookup[m] = net_rent + net_programs

    # Calculate expenses for each category for each month
    by_category_by_month: List[OpexCategoryMonthResult] = []
    for month in time_grid.month_ids:
        revenue_for_month = revenue_lookup.get(month)
        for category in opex_table:
            result = _calculate_expense_for_month(
                category=category,
                month=month,
                analysis_start=analysis_start,
                total_units=total_units,
                total_sqft=total_sqft,
                revenue_for_month=revenue_for_month,
            )
            by_category_by_month.append(result)

    # Aggregate totals by month
    totals_by_month: List[Dict[str, Any]] = []
    for month in time_grid.month_ids:
        month_results = [r for r in by_category_by_month if r.month == month]
        total = sum((r.expense for r in month_results), Decimal("0"))
        recoverable = sum((r.expense for r in month_results if r.recoverable), Decimal("0"))
        non_recoverable = sum((r.expense for r in month_results if not r.recoverable), Decimal("0"))
        totals_by_month.append(
            {
                "month": month,
                "total_opex": round2(total),
                "recoverable_opex": round2(recoverable),
                "non_recoverable_opex": round2(non_recoverable),
            }
        )

    # Aggregate totals by year
    totals_by_year: List[Dict[str, Any]] = []
    for year in time_grid.year_ids:
        year_months = [m for m in time_grid.month_ids if m.startswith(year)]
        year_results = [r for r in by_category_by_month if r.month in year_months]
        total = sum((r.expense for r in year_results), Decimal("0"))
        recoverable = sum((r.expense for r in year_results if r.recoverable), Decimal("0"))
        non_recoverable = sum((r.expense for r in year_results if not r.recoverable), Decimal("0"))
        totals_by_year.append(
            {
                "year": year,
                "total_opex": round2(total),
                "recoverable_opex": round2(recoverable),
                "non_recoverable_opex": round2(non_recoverable),
            }
        )

    return {
        "by_category_by_month": [
            {
                "month": r.month,
                "category": r.category,
                "expense": round2(r.expense),
                "growth_factor": round2(r.growth_factor),
                "recoverable": r.recoverable,
            }
            for r in by_category_by_month
        ],
        "totals_by_month": totals_by_month,
        "totals_by_year": totals_by_year,
    }
