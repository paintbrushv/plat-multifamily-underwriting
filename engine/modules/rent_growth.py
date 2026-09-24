"""
Rent Growth Module

Automates market rent curve generation from growth assumptions.
Supports annual/monthly compounding, step increases, and LTL decay.

See: docs/modules/rent_growth_spec.md for full specification
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional

from engine.modules.time_grid import TimeGrid
from engine.modules.util import dec, month_id, round2, parse_month


@dataclass(frozen=True)
class RentGrowthMonthResult:
    """Intermediate result for rent growth in a single month."""

    month: str
    cohort_id: str
    market_rent: Decimal
    growth_applied: Decimal
    step_applied: Decimal


@dataclass(frozen=True)
class LTLDecayMonthResult:
    """Intermediate result for LTL decay in a single month."""

    month: str
    cohort_id: str
    ltl_percent: Decimal
    decay_applied: Decimal


def _calculate_years_elapsed(current_month: str, start_month: str) -> int:
    """Calculate integer years elapsed since start month."""
    current = parse_month(current_month)
    start = parse_month(start_month)
    years = current.year - start.year
    if current.month < start.month:
        years -= 1
    return max(0, years)


def _calculate_months_elapsed(current_month: str, start_month: str) -> int:
    """Calculate months elapsed since start month."""
    current = parse_month(current_month)
    start = parse_month(start_month)
    months = (current.year - start.year) * 12 + (current.month - start.month)
    return max(0, months)


def _apply_annual_compound_growth(
    base_rent: Decimal,
    annual_rate: Decimal,
    current_month: str,
    growth_start: str,
) -> tuple[Decimal, Decimal]:
    """
    Apply annual compound growth.

    Returns: (new_rent, growth_factor_applied)
    """
    years = _calculate_years_elapsed(current_month, growth_start)
    if years <= 0:
        return base_rent, Decimal("0")

    growth_factor = (Decimal("1") + annual_rate) ** years
    new_rent = base_rent * growth_factor
    return new_rent, growth_factor - Decimal("1")


def _apply_monthly_compound_growth(
    base_rent: Decimal,
    annual_rate: Decimal,
    current_month: str,
    growth_start: str,
) -> tuple[Decimal, Decimal]:
    """
    Apply monthly compound growth.

    Monthly rate = (1 + annual)^(1/12) - 1

    Returns: (new_rent, growth_factor_applied)
    """
    months = _calculate_months_elapsed(current_month, growth_start)
    if months <= 0:
        return base_rent, Decimal("0")

    # Calculate monthly rate: (1 + annual)^(1/12) - 1
    # For precision, we use: monthly_factor = (1 + annual)^(1/12)
    one_plus_annual = Decimal("1") + annual_rate
    # Approximate monthly compounding
    monthly_factor = one_plus_annual ** (Decimal("1") / Decimal("12"))
    growth_factor = monthly_factor ** months
    new_rent = base_rent * growth_factor
    return new_rent, growth_factor - Decimal("1")


def _apply_step_increases(
    base_rent: Decimal,
    step_increases: List[Dict[str, Any]],
    current_month: str,
) -> tuple[Decimal, Decimal]:
    """
    Apply all step increases effective on or before current month.

    Returns: (new_rent, total_step_percent)
    """
    current = month_id(current_month)
    adjusted = base_rent
    total_step = Decimal("0")

    for step in step_increases:
        effective = month_id(step["effective_month"])
        if effective <= current:
            increase = dec(step["increase_percent"])
            adjusted *= (Decimal("1") + increase)
            total_step += increase

    return adjusted, total_step


def _calculate_ltl_annual_step(
    initial_ltl: Decimal,
    decay_rate: Decimal,
    current_month: str,
    decay_start: str,
    min_ltl: Decimal,
) -> tuple[Decimal, Decimal]:
    """
    Reduce LTL by fixed amount each year.

    Returns: (new_ltl, decay_applied)
    """
    years = _calculate_years_elapsed(current_month, decay_start)
    if years <= 0:
        return initial_ltl, Decimal("0")

    total_decay = decay_rate * years
    new_ltl = initial_ltl - total_decay
    if new_ltl < min_ltl:
        new_ltl = min_ltl
        total_decay = initial_ltl - min_ltl

    return new_ltl, total_decay


def _calculate_ltl_monthly_linear(
    initial_ltl: Decimal,
    annual_decay: Decimal,
    current_month: str,
    decay_start: str,
    min_ltl: Decimal,
) -> tuple[Decimal, Decimal]:
    """
    Smooth linear reduction in LTL.

    Returns: (new_ltl, decay_applied)
    """
    months = _calculate_months_elapsed(current_month, decay_start)
    if months <= 0:
        return initial_ltl, Decimal("0")

    monthly_decay = annual_decay / Decimal("12")
    total_decay = monthly_decay * months
    new_ltl = initial_ltl - total_decay
    if new_ltl < min_ltl:
        new_ltl = min_ltl
        total_decay = initial_ltl - min_ltl

    return new_ltl, total_decay


def generate_rent_curves(
    time_grid: TimeGrid,
    unit_cohorts: List[Dict[str, Any]],
    growth_assumptions: Optional[Dict[str, Any]] = None,
    ltl_assumptions: Optional[Dict[str, Any]] = None,
    renovation_premiums: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Generate market rent and LTL curves from growth assumptions.

    Args:
        time_grid: Authoritative time grid
        unit_cohorts: Unit mix with initial rents
        growth_assumptions: Rent growth parameters
        ltl_assumptions: LTL decay parameters
        renovation_premiums: Optional renovation premium data by cohort

    Returns:
        Dict with generated_market_rent_curve, generated_ltl_curve, summary
    """
    if not growth_assumptions:
        growth_assumptions = {
            "growth_type": "annual_compound",
            "annual_growth_rate": 0.0,
        }

    if not ltl_assumptions:
        ltl_assumptions = {
            "decay_type": "none",
            "initial_ltl_percent": 0.0,
        }

    # Extract growth parameters
    growth_type = growth_assumptions.get("growth_type", "annual_compound")
    annual_growth = dec(growth_assumptions.get("annual_growth_rate", 0))
    growth_start = month_id(growth_assumptions.get("growth_start_month", time_grid.month_ids[0]))
    step_increases = growth_assumptions.get("step_increases", [])

    # Extract LTL parameters
    decay_type = ltl_assumptions.get("decay_type", "none")
    initial_ltl = dec(ltl_assumptions.get("initial_ltl_percent", 0))
    annual_decay = dec(ltl_assumptions.get("annual_decay_rate", 0))
    min_ltl = dec(ltl_assumptions.get("minimum_ltl_percent", 0))
    decay_start = month_id(ltl_assumptions.get("decay_start_month", time_grid.month_ids[0]))

    # Generate curves for each cohort
    rent_results: List[RentGrowthMonthResult] = []
    ltl_results: List[LTLDecayMonthResult] = []

    for cohort in unit_cohorts:
        cohort_id = cohort["cohort_id"]
        # Base market rent = initial_inplace_rent / (1 - initial_ltl)
        # This derives market from inplace, assuming inplace = market * (1 - ltl)
        initial_inplace = dec(cohort.get("initial_inplace_rent", 0))
        if initial_ltl < Decimal("1"):
            base_market = initial_inplace / (Decimal("1") - initial_ltl)
        else:
            base_market = initial_inplace

        # Get cohort-specific growth rate if provided
        cohort_growth = dec(cohort.get("growth_rate", annual_growth))

        for month in time_grid.month_ids:
            # Calculate market rent
            if growth_type == "annual_compound":
                grown_rent, growth_pct = _apply_annual_compound_growth(
                    base_market, cohort_growth, month, growth_start
                )
            elif growth_type == "monthly_compound":
                grown_rent, growth_pct = _apply_monthly_compound_growth(
                    base_market, cohort_growth, month, growth_start
                )
            else:  # step_only
                grown_rent = base_market
                growth_pct = Decimal("0")

            # Apply step increases
            stepped_rent, step_pct = _apply_step_increases(grown_rent, step_increases, month)

            # Apply renovation premium if applicable
            if renovation_premiums and cohort_id in renovation_premiums:
                reno = renovation_premiums[cohort_id]
                completion_month = month_id(reno.get("completion_month", ""))
                if month >= completion_month:
                    premium = dec(reno.get("rent_premium", 0))
                    stepped_rent += premium

            rent_results.append(
                RentGrowthMonthResult(
                    month=month,
                    cohort_id=cohort_id,
                    market_rent=stepped_rent,
                    growth_applied=growth_pct,
                    step_applied=step_pct,
                )
            )

            # Calculate LTL
            if decay_type == "annual_step":
                new_ltl, decay_pct = _calculate_ltl_annual_step(
                    initial_ltl, annual_decay, month, decay_start, min_ltl
                )
            elif decay_type == "monthly_linear":
                new_ltl, decay_pct = _calculate_ltl_monthly_linear(
                    initial_ltl, annual_decay, month, decay_start, min_ltl
                )
            else:  # none
                new_ltl = initial_ltl
                decay_pct = Decimal("0")

            ltl_results.append(
                LTLDecayMonthResult(
                    month=month,
                    cohort_id=cohort_id,
                    ltl_percent=new_ltl,
                    decay_applied=decay_pct,
                )
            )

    # Convert to output format compatible with revenue module
    # Group consecutive months with same values into periods
    generated_market_rent_curve = _consolidate_rent_curve(rent_results, time_grid)
    generated_ltl_curve = _consolidate_ltl_curve(ltl_results, time_grid)

    # Calculate summary
    if rent_results:
        first_rents = [r for r in rent_results if r.month == time_grid.month_ids[0]]
        last_rents = [r for r in rent_results if r.month == time_grid.month_ids[-1]]
        starting_rent = sum((r.market_rent for r in first_rents), Decimal("0")) / len(first_rents) if first_rents else Decimal("0")
        ending_rent = sum((r.market_rent for r in last_rents), Decimal("0")) / len(last_rents) if last_rents else Decimal("0")
        total_growth = (ending_rent / starting_rent - Decimal("1")) if starting_rent > 0 else Decimal("0")

        # Calculate CAGR
        years = _calculate_years_elapsed(time_grid.month_ids[-1], time_grid.month_ids[0])
        if years > 0 and starting_rent > 0:
            cagr = (ending_rent / starting_rent) ** (Decimal("1") / Decimal(str(years))) - Decimal("1")
        else:
            cagr = Decimal("0")
    else:
        starting_rent = Decimal("0")
        ending_rent = Decimal("0")
        total_growth = Decimal("0")
        cagr = Decimal("0")

    first_ltl = initial_ltl
    if ltl_results:
        last_ltl_values = [r.ltl_percent for r in ltl_results if r.month == time_grid.month_ids[-1]]
        ending_ltl = last_ltl_values[0] if last_ltl_values else initial_ltl
    else:
        ending_ltl = initial_ltl

    return {
        "generated_market_rent_curve": generated_market_rent_curve,
        "generated_ltl_curve": generated_ltl_curve,
        "by_month": [
            {
                "month": r.month,
                "cohort_id": r.cohort_id,
                "market_rent": round2(r.market_rent),
                "growth_applied": round2(r.growth_applied),
                "step_applied": round2(r.step_applied),
            }
            for r in rent_results
        ],
        "ltl_by_month": [
            {
                "month": r.month,
                "cohort_id": r.cohort_id,
                "ltl_percent": round2(r.ltl_percent),
                "decay_applied": round2(r.decay_applied),
            }
            for r in ltl_results
        ],
        "summary": {
            "starting_market_rent_avg": round2(starting_rent),
            "ending_market_rent_avg": round2(ending_rent),
            "total_growth_percent": round2(total_growth),
            "cagr": round2(cagr),
            "starting_ltl": round2(first_ltl),
            "ending_ltl": round2(ending_ltl),
            "ltl_reduction": round2(first_ltl - ending_ltl),
        },
    }


def _consolidate_rent_curve(
    results: List[RentGrowthMonthResult],
    time_grid: TimeGrid,
) -> List[Dict[str, Any]]:
    """
    Consolidate monthly results into period-based curve for revenue module.

    Groups consecutive months with same rent into single periods.
    """
    if not results:
        return []

    curve: List[Dict[str, Any]] = []
    cohort_ids = list(set(r.cohort_id for r in results))

    for cohort_id in cohort_ids:
        cohort_results = [r for r in results if r.cohort_id == cohort_id]
        cohort_results.sort(key=lambda r: r.month)

        if not cohort_results:
            continue

        # Group consecutive months with same rent
        current_period_start = cohort_results[0].month
        current_rent = cohort_results[0].market_rent
        current_growth = cohort_results[0].growth_applied
        current_step = cohort_results[0].step_applied

        for i, r in enumerate(cohort_results[1:], 1):
            # Check if rent changed
            if r.market_rent != current_rent:
                # Close current period
                curve.append({
                    "cohort_id": cohort_id,
                    "start_period": current_period_start,
                    "end_period": cohort_results[i - 1].month,
                    "market_rent": round2(current_rent),
                    "growth_applied": round2(current_growth),
                    "step_applied": round2(current_step),
                })
                # Start new period
                current_period_start = r.month
                current_rent = r.market_rent
                current_growth = r.growth_applied
                current_step = r.step_applied

        # Close final period
        curve.append({
            "cohort_id": cohort_id,
            "start_period": current_period_start,
            "end_period": cohort_results[-1].month,
            "market_rent": round2(current_rent),
            "growth_applied": round2(current_growth),
            "step_applied": round2(current_step),
        })

    return curve


def _consolidate_ltl_curve(
    results: List[LTLDecayMonthResult],
    time_grid: TimeGrid,
) -> List[Dict[str, Any]]:
    """
    Consolidate monthly LTL results into period-based curve.
    """
    if not results:
        return []

    curve: List[Dict[str, Any]] = []
    cohort_ids = list(set(r.cohort_id for r in results))

    for cohort_id in cohort_ids:
        cohort_results = [r for r in results if r.cohort_id == cohort_id]
        cohort_results.sort(key=lambda r: r.month)

        if not cohort_results:
            continue

        # Group consecutive months with same LTL
        current_period_start = cohort_results[0].month
        current_ltl = cohort_results[0].ltl_percent
        current_decay = cohort_results[0].decay_applied

        for i, r in enumerate(cohort_results[1:], 1):
            # Check if LTL changed
            if r.ltl_percent != current_ltl:
                # Close current period
                curve.append({
                    "cohort_id": cohort_id,
                    "start_period": current_period_start,
                    "end_period": cohort_results[i - 1].month,
                    "ltl_percent": round2(current_ltl),
                    "decay_applied": round2(current_decay),
                })
                # Start new period
                current_period_start = r.month
                current_ltl = r.ltl_percent
                current_decay = r.decay_applied

        # Close final period
        curve.append({
            "cohort_id": cohort_id,
            "start_period": current_period_start,
            "end_period": cohort_results[-1].month,
            "ltl_percent": round2(current_ltl),
            "decay_applied": round2(current_decay),
        })

    return curve
