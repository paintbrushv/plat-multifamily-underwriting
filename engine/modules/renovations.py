"""
Renovations Module

Models value-add capital improvements that generate rent premiums.
Tracks renovation schedules, costs, downtime, and resulting rent increases.

See: docs/modules/renovations_spec.md for full specification
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional

from engine.modules.time_grid import TimeGrid
from engine.modules.util import dec, month_id, round2


@dataclass(frozen=True)
class RenovationMonthResult:
    """Intermediate result for renovations in a single month."""

    month: str
    program_id: str
    units_renovated: int
    renovation_cost: Decimal
    downtime_vacancy_days: int
    downtime_vacancy_loss: Decimal
    cumulative_units_renovated: int
    monthly_premium_revenue: Decimal


def _get_cohort_units(unit_cohorts: List[Dict[str, Any]], cohort_id: str) -> int:
    """Get unit count for a specific cohort."""
    for cohort in unit_cohorts:
        if cohort.get("cohort_id") == cohort_id:
            return int(cohort.get("unit_count", 0))
    return 0


def _get_cohort_rent(unit_cohorts: List[Dict[str, Any]], cohort_id: str) -> Decimal:
    """Get monthly rent for a specific cohort."""
    for cohort in unit_cohorts:
        if cohort.get("cohort_id") == cohort_id:
            return dec(cohort.get("initial_inplace_rent", 0))
    return Decimal("0")


def _month_in_range(month: str, start_month: str, end_month: Optional[str]) -> bool:
    """Check if month is within program range (inclusive)."""
    if month < start_month:
        return False
    if end_month and month > end_month:
        return False
    return True


def _get_seasonal_multiplier(
    month: str,
    seasonal_multipliers: Optional[Dict[str, float]],
) -> float:
    """Get seasonal downtime multiplier for a given month.

    Args:
        month: Month ID like "2026-01"
        seasonal_multipliers: Map of "01"-"12" to multiplier, or None

    Returns:
        Multiplier (default 1.0 if no map or month not in map)
    """
    if not seasonal_multipliers:
        return 1.0
    cal_month = month[5:7]  # "2026-01" -> "01"
    return seasonal_multipliers.get(cal_month, 1.0)


def _apply_seasonal_downtime(
    base_downtime_days: int,
    month: str,
    seasonal_multipliers: Optional[Dict[str, float]],
) -> int:
    """Apply seasonal multiplier to base downtime days.

    Returns ceil(base * multiplier) so partial days round up.
    """
    multiplier = _get_seasonal_multiplier(month, seasonal_multipliers)
    return math.ceil(base_downtime_days * multiplier)


def compute_renovations(
    time_grid: TimeGrid,
    unit_cohorts: List[Dict[str, Any]],
    renovation_programs: List[Dict[str, Any]],
    turnover_rate: float = 0.50,
    unit_renovations: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Compute monthly renovation activity and premium generation.

    Args:
        time_grid: Authoritative time grid
        unit_cohorts: Unit mix with cohort definitions
        renovation_programs: List of renovation program definitions
        turnover_rate: Annual turnover rate for on_turnover strategy (default 50%)

    Returns:
        Dict with by_month, by_program, summary
    """
    if not renovation_programs and not unit_renovations:
        # No renovations - return empty structure
        return {
            "by_month": [
                {
                    "month": m,
                    "units_renovated": 0,
                    "renovation_cost": 0.0,
                    "downtime_vacancy_days": 0,
                    "downtime_vacancy_loss": 0.0,
                    "cumulative_units_renovated": 0,
                    "monthly_premium_revenue": 0.0,
                    "cumulative_premium_revenue": 0.0,
                }
                for m in time_grid.month_ids
            ],
            "by_program": [],
            "by_program_by_month": [],
            "summary": {
                "total_units_renovated": 0,
                "total_renovation_cost": 0.0,
                "total_vacancy_loss": 0.0,
                "net_renovation_cost": 0.0,
                "total_annual_premium": 0.0,
                "blended_roi": None,
                "avg_payback_months": None,
            },
        }

    if unit_renovations is None:
        unit_renovations = []

    # Build lookup: cohort_id -> program (for seasonal multiplier inheritance)
    cohort_to_program: Dict[str, Dict[str, Any]] = {}
    for program in renovation_programs:
        tc = program["target_cohort"]
        if tc not in cohort_to_program:
            cohort_to_program[tc] = program

    # Index unit overrides by month
    overrides_by_month: Dict[str, List[Dict[str, Any]]] = {}
    for ur in unit_renovations:
        m = month_id(ur["renovation_month"])
        if m not in overrides_by_month:
            overrides_by_month[m] = []
        overrides_by_month[m].append(ur)

    monthly_turnover_rate = Decimal(str(turnover_rate)) / Decimal("12")

    # Track shared remaining-units pool per target cohort
    # Multiple programs sharing the same target_cohort draw from one pool
    cohort_pool: Dict[str, int] = {}
    for program in renovation_programs:
        target_cohort = program["target_cohort"]
        if target_cohort not in cohort_pool:
            cohort_pool[target_cohort] = _get_cohort_units(unit_cohorts, target_cohort)

    # Subtract unit overrides from cohort pool upfront (Option A)
    for ur in unit_renovations:
        cid = ur["cohort_id"]
        if cid not in cohort_pool:
            cohort_pool[cid] = _get_cohort_units(unit_cohorts, cid)
        cohort_pool[cid] = max(0, cohort_pool[cid] - 1)

    # Track per-program cumulative renovated count and optional max_units cap
    program_state: Dict[str, Dict[str, Any]] = {}
    for program in renovation_programs:
        pid = program["program_id"]
        program_state[pid] = {
            "cumulative_renovated": 0,
            "max_units": int(program.get("max_units", 0)) or 0,
        }

    # Calculate renovations for each month
    results: List[RenovationMonthResult] = []

    for month in time_grid.month_ids:
        for program in renovation_programs:
            pid = program["program_id"]
            start_month = month_id(program["start_month"])
            end_month = month_id(program["end_month"]) if program.get("end_month") else None

            # Check if month is in program range
            if not _month_in_range(month, start_month, end_month):
                # Still record zero activity for tracking
                results.append(
                    RenovationMonthResult(
                        month=month,
                        program_id=pid,
                        units_renovated=0,
                        renovation_cost=Decimal("0"),
                        downtime_vacancy_days=0,
                        downtime_vacancy_loss=Decimal("0"),
                        cumulative_units_renovated=program_state[pid]["cumulative_renovated"],
                        monthly_premium_revenue=dec(program["rent_premium_monthly"])
                        * program_state[pid]["cumulative_renovated"],
                    )
                )
                continue

            # Calculate units available from shared pool
            target_cohort = program["target_cohort"]
            remaining = cohort_pool[target_cohort]
            monthly_pace = int(program["monthly_pace"])
            strategy = program["strategy"]

            # Determine max units this month
            if strategy == "on_turnover":
                # Limited by natural turnover.  Analysts can explicitly opt into
                # a guaranteed floor with min_monthly_pace, but the default must
                # not manufacture one renovation per month when turnover support
                # is below one unit.
                max_from_turnover = int(Decimal(str(remaining)) * monthly_turnover_rate)
                min_monthly_pace = int(program.get("min_monthly_pace", 0))
                if min_monthly_pace > 0 and remaining > 0:
                    max_from_turnover = max(min_monthly_pace, max_from_turnover)
                units_this_month = min(monthly_pace, max_from_turnover, remaining)
            else:
                # Proactive - limited only by pace and remaining units
                units_this_month = min(monthly_pace, remaining)

            # Enforce per-program max_units cap (annual target from bridge)
            max_units = program_state[pid]["max_units"]
            if max_units > 0:
                remaining_budget = max_units - program_state[pid]["cumulative_renovated"]
                units_this_month = min(units_this_month, remaining_budget)

            # Calculate costs
            cost_per_unit = dec(program["renovation_cost_per_unit"])
            renovation_cost = cost_per_unit * units_this_month

            # Calculate downtime vacancy loss
            base_downtime = int(program["downtime_days"])
            seasonal_multipliers = program.get("seasonal_downtime_multipliers")
            effective_downtime = _apply_seasonal_downtime(base_downtime, month, seasonal_multipliers)
            total_downtime_days = effective_downtime * units_this_month

            # Get original rent for vacancy loss calculation
            daily_rent = _get_cohort_rent(unit_cohorts, target_cohort) / Decimal("30")
            vacancy_loss = daily_rent * total_downtime_days

            # Update shared pool and per-program state
            cohort_pool[target_cohort] -= units_this_month
            program_state[pid]["cumulative_renovated"] += units_this_month

            # Calculate premium (from previously completed renovations)
            # Premium starts month AFTER renovation completes
            rent_premium = dec(program["rent_premium_monthly"])
            # Units completed before this month generate premium
            prior_cumulative = program_state[pid]["cumulative_renovated"] - units_this_month
            premium_revenue = rent_premium * prior_cumulative

            results.append(
                RenovationMonthResult(
                    month=month,
                    program_id=pid,
                    units_renovated=units_this_month,
                    renovation_cost=renovation_cost,
                    downtime_vacancy_days=total_downtime_days,
                    downtime_vacancy_loss=vacancy_loss,
                    cumulative_units_renovated=program_state[pid]["cumulative_renovated"],
                    monthly_premium_revenue=premium_revenue,
                )
            )

        # Process unit overrides scheduled for this month
        for ur in overrides_by_month.get(month, []):
            # Determine base downtime
            if "downtime_days" in ur and ur["downtime_days"] is not None:
                base_dt = int(ur["downtime_days"])
            else:
                matching_prog = cohort_to_program.get(ur["cohort_id"])
                base_dt = int(matching_prog["downtime_days"]) if matching_prog else 21

            # Apply seasonal multiplier from matching program
            matching_prog = cohort_to_program.get(ur["cohort_id"])
            seasonal_mults = matching_prog.get("seasonal_downtime_multipliers") if matching_prog else None
            effective_dt = _apply_seasonal_downtime(base_dt, month, seasonal_mults)

            # Vacancy loss based on cohort rent
            daily_rent = _get_cohort_rent(unit_cohorts, ur["cohort_id"]) / Decimal("30")
            vacancy_loss = daily_rent * effective_dt

            results.append(
                RenovationMonthResult(
                    month=month,
                    program_id="__unit_override__",
                    units_renovated=1,
                    renovation_cost=dec(ur["cost"]),
                    downtime_vacancy_days=effective_dt,
                    downtime_vacancy_loss=vacancy_loss,
                    cumulative_units_renovated=0,  # Not tracked per-program for overrides
                    monthly_premium_revenue=Decimal("0"),  # Recalculated in aggregation
                )
            )

    # Aggregate by month (across all programs)
    by_month: List[Dict[str, Any]] = []
    cumulative_premium = Decimal("0")

    for month in time_grid.month_ids:
        month_results = [r for r in results if r.month == month]
        total_units = sum(r.units_renovated for r in month_results)
        total_cost = sum((r.renovation_cost for r in month_results), Decimal("0"))
        total_downtime_days = sum(r.downtime_vacancy_days for r in month_results)
        total_vacancy_loss = sum((r.downtime_vacancy_loss for r in month_results), Decimal("0"))
        # Cumulative from programs
        total_cumulative = sum(
            r.cumulative_units_renovated for r in month_results if r.program_id != "__unit_override__"
        )
        # Add cumulative unit overrides (all scheduled up to and including this month)
        total_cumulative += sum(
            1 for ur in unit_renovations if month_id(ur["renovation_month"]) <= month
        )

        # Premium calculation: sum premiums from all programs
        # Need to recalculate based on cumulative units at start of month
        month_premium = Decimal("0")
        for program in renovation_programs:
            pid = program["program_id"]
            rent_premium = dec(program["rent_premium_monthly"])
            # Find cumulative for this program at end of prior month
            prior_months = [r for r in results if r.program_id == pid and r.month < month]
            if prior_months:
                prior_cumulative = prior_months[-1].cumulative_units_renovated
            else:
                prior_cumulative = 0
            month_premium += rent_premium * prior_cumulative

        # Add premium from unit overrides completed in prior months
        for ur in unit_renovations:
            ur_month = month_id(ur["renovation_month"])
            if ur_month < month:
                month_premium += dec(ur["expected_premium"])

        cumulative_premium += month_premium

        by_month.append(
            {
                "month": month,
                "units_renovated": total_units,
                "renovation_cost": round2(total_cost),
                "downtime_vacancy_days": total_downtime_days,
                "downtime_vacancy_loss": round2(total_vacancy_loss),
                "cumulative_units_renovated": total_cumulative,
                "monthly_premium_revenue": round2(month_premium),
                "cumulative_premium_revenue": round2(cumulative_premium),
            }
        )

    # Aggregate by program
    by_program: List[Dict[str, Any]] = []
    for program in renovation_programs:
        pid = program["program_id"]
        program_results = [r for r in results if r.program_id == pid]

        total_units_renovated = sum(r.units_renovated for r in program_results)
        total_renovation_cost = sum((r.renovation_cost for r in program_results), Decimal("0"))
        total_vacancy_loss = sum((r.downtime_vacancy_loss for r in program_results), Decimal("0"))

        rent_premium = dec(program["rent_premium_monthly"])
        monthly_premium_at_completion = rent_premium * total_units_renovated
        annual_premium = monthly_premium_at_completion * 12

        # Calculate ROI and payback
        if total_renovation_cost > 0:
            simple_roi = annual_premium / total_renovation_cost
            # Payback is per-unit: cost_per_unit / rent_premium
            cost_per_unit = dec(program["renovation_cost_per_unit"])
            payback_months = cost_per_unit / rent_premium if rent_premium > 0 else None
        else:
            simple_roi = None
            payback_months = None

        by_program.append(
            {
                "program_id": pid,
                "program_name": program.get("program_name", pid),
                "total_units_renovated": total_units_renovated,
                "total_renovation_cost": round2(total_renovation_cost),
                "total_downtime_vacancy_loss": round2(total_vacancy_loss),
                "monthly_premium_at_completion": round2(monthly_premium_at_completion),
                "annual_premium_at_completion": round2(annual_premium),
                "simple_roi": round2(simple_roi) if simple_roi else None,
                "payback_months": round2(payback_months) if payback_months else None,
            }
        )

    # Unit override aggregate data for summary
    override_results = [r for r in results if r.program_id == "__unit_override__"]
    override_units = sum(r.units_renovated for r in override_results)
    override_cost = sum((r.renovation_cost for r in override_results), Decimal("0"))
    override_vacancy = sum((r.downtime_vacancy_loss for r in override_results), Decimal("0"))
    override_annual_premium = sum(
        (dec(ur["expected_premium"]) * 12 for ur in unit_renovations), Decimal("0")
    )

    # Summary
    total_units_renovated = sum(p["total_units_renovated"] for p in by_program) + override_units
    total_renovation_cost = sum((dec(p["total_renovation_cost"]) for p in by_program), Decimal("0")) + override_cost
    total_vacancy_loss = sum(
        (dec(p["total_downtime_vacancy_loss"]) for p in by_program), Decimal("0")
    ) + override_vacancy
    net_renovation_cost = total_renovation_cost + total_vacancy_loss
    total_annual_premium = sum(
        (dec(p["annual_premium_at_completion"]) for p in by_program), Decimal("0")
    ) + override_annual_premium

    # Blended metrics
    if total_renovation_cost > 0:
        blended_roi = total_annual_premium / total_renovation_cost
    else:
        blended_roi = None

    if total_units_renovated > 0 and by_program:
        valid_paybacks = [p["payback_months"] for p in by_program if p["payback_months"]]
        if valid_paybacks:
            avg_payback = sum(valid_paybacks) / len(valid_paybacks)
        else:
            avg_payback = None
    else:
        avg_payback = None

    by_program_by_month = [
        {
            "month": r.month,
            "program_id": r.program_id,
            "units_renovated": r.units_renovated,
            "cumulative_units_renovated": r.cumulative_units_renovated,
            "renovation_cost": round2(r.renovation_cost),
            "monthly_premium_revenue": round2(r.monthly_premium_revenue),
        }
        for r in results
    ]

    return {
        "by_month": by_month,
        "by_program": by_program,
        "by_program_by_month": by_program_by_month,
        "summary": {
            "total_units_renovated": total_units_renovated,
            "total_renovation_cost": round2(total_renovation_cost),
            "total_vacancy_loss": round2(total_vacancy_loss),
            "net_renovation_cost": round2(net_renovation_cost),
            "total_annual_premium": round2(total_annual_premium),
            "blended_roi": round2(blended_roi) if blended_roi else None,
            "avg_payback_months": round(avg_payback, 1) if avg_payback else None,
        },
    }
