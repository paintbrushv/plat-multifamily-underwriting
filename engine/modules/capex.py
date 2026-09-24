"""
Capital Expenditures Module

Calculates monthly capital expenditures with support for:
- One-time scheduled CapEx
- Renovation-linked CapEx
- Reserve fund contributions (annual or monthly)
- Recurring monthly CapEx

See: docs/modules/capex_spec.md for full specification
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List

from engine.modules.time_grid import TimeGrid
from engine.modules.util import dec, get_total_units, month_id, round2


@dataclass(frozen=True)
class CapexMonthResult:
    """Intermediate result for CapEx in a single month."""

    month: str
    category: str
    capex_type: str
    amount: Decimal


def compute_capex(
    time_grid: TimeGrid,
    unit_cohorts: List[Dict[str, Any]],
    capex_schedule: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Compute monthly capital expenditures by category and type.

    Args:
        time_grid: Authoritative time grid
        unit_cohorts: Unit mix for per_unit calculations
        capex_schedule: CapEx schedule definitions

    Returns:
        Dict with by_month, by_category, totals_by_year, summary
    """
    if not capex_schedule:
        # Return empty structure if no CapEx defined
        return {
            "by_month": [
                {
                    "month": m,
                    "total_capex": 0.0,
                    "one_time_capex": 0.0,
                    "renovation_capex": 0.0,
                    "reserve_capex": 0.0,
                    "recurring_capex": 0.0,
                }
                for m in time_grid.month_ids
            ],
            "by_category": [],
            "totals_by_year": [
                {
                    "year": y,
                    "total_capex": 0.0,
                    "one_time_capex": 0.0,
                    "renovation_capex": 0.0,
                    "reserve_capex": 0.0,
                    "recurring_capex": 0.0,
                }
                for y in time_grid.year_ids
            ],
            "summary": {"total_capex": 0.0, "capex_per_unit": 0.0},
        }

    total_units = get_total_units(unit_cohorts)
    results: List[CapexMonthResult] = []

    for item in capex_schedule:
        category = item["category"]
        capex_type = item["capex_type"]

        if capex_type == "one_time":
            # One-time expenditure in specific month
            scheduled_month = month_id(item["month"])
            if scheduled_month in time_grid.month_ids:
                amount = dec(item["amount"])
                results.append(
                    CapexMonthResult(
                        month=scheduled_month,
                        category=category,
                        capex_type=capex_type,
                        amount=amount,
                    )
                )

        elif capex_type == "renovation":
            # Renovation-linked expenditure in specific month
            scheduled_month = month_id(item["month"])
            if scheduled_month in time_grid.month_ids:
                amount = dec(item["amount"])
                results.append(
                    CapexMonthResult(
                        month=scheduled_month,
                        category=category,
                        capex_type=capex_type,
                        amount=amount,
                    )
                )

        elif capex_type == "reserve":
            # Reserve fund contribution spread across periods. Bridge extracts may
            # provide only a total annual/monthly amount when the per-unit column
            # is blank; convert that total to the per-unit basis expected here.
            raw_amount_per_unit = item.get("amount_per_unit")
            if raw_amount_per_unit is None:
                total_amount = dec(item.get("amount", 0))
                amount_per_unit = total_amount / total_units if total_units > 0 else Decimal("0")
            else:
                amount_per_unit = dec(raw_amount_per_unit)
            timing = item.get("timing", "annual")

            if timing == "annual":
                # Annual amount per unit, spread monthly
                monthly_amount = amount_per_unit * total_units / Decimal("12")
            else:
                # Monthly amount per unit
                monthly_amount = amount_per_unit * total_units

            for month in time_grid.month_ids:
                results.append(
                    CapexMonthResult(
                        month=month,
                        category=category,
                        capex_type=capex_type,
                        amount=monthly_amount,
                    )
                )

        elif capex_type == "recurring":
            # Recurring monthly expenditure
            amount_per_unit_monthly = dec(item["amount_per_unit_monthly"])
            monthly_amount = amount_per_unit_monthly * total_units

            for month in time_grid.month_ids:
                results.append(
                    CapexMonthResult(
                        month=month,
                        category=category,
                        capex_type=capex_type,
                        amount=monthly_amount,
                    )
                )

        else:
            raise ValueError(f"Unknown capex_type: {capex_type}")

    # Aggregate by month
    by_month: List[Dict[str, Any]] = []
    for month in time_grid.month_ids:
        month_results = [r for r in results if r.month == month]
        total = sum((r.amount for r in month_results), Decimal("0"))
        one_time = sum((r.amount for r in month_results if r.capex_type == "one_time"), Decimal("0"))
        renovation = sum((r.amount for r in month_results if r.capex_type == "renovation"), Decimal("0"))
        reserve = sum((r.amount for r in month_results if r.capex_type == "reserve"), Decimal("0"))
        recurring = sum((r.amount for r in month_results if r.capex_type == "recurring"), Decimal("0"))

        by_month.append(
            {
                "month": month,
                "total_capex": round2(total),
                "one_time_capex": round2(one_time),
                "renovation_capex": round2(renovation),
                "reserve_capex": round2(reserve),
                "recurring_capex": round2(recurring),
            }
        )

    # Aggregate by category
    categories = set(r.category for r in results)
    by_category: List[Dict[str, Any]] = []
    for category in sorted(categories):
        cat_results = [r for r in results if r.category == category]
        total_amount = sum((r.amount for r in cat_results), Decimal("0"))
        capex_type = cat_results[0].capex_type if cat_results else "unknown"
        by_category.append(
            {
                "category": category,
                "capex_type": capex_type,
                "total_amount": round2(total_amount),
            }
        )

    # Aggregate by year
    totals_by_year: List[Dict[str, Any]] = []
    for year in time_grid.year_ids:
        year_months = [m for m in time_grid.month_ids if m.startswith(year)]
        year_results = [r for r in results if r.month in year_months]
        total = sum((r.amount for r in year_results), Decimal("0"))
        one_time = sum((r.amount for r in year_results if r.capex_type == "one_time"), Decimal("0"))
        renovation = sum((r.amount for r in year_results if r.capex_type == "renovation"), Decimal("0"))
        reserve = sum((r.amount for r in year_results if r.capex_type == "reserve"), Decimal("0"))
        recurring = sum((r.amount for r in year_results if r.capex_type == "recurring"), Decimal("0"))

        totals_by_year.append(
            {
                "year": year,
                "total_capex": round2(total),
                "one_time_capex": round2(one_time),
                "renovation_capex": round2(renovation),
                "reserve_capex": round2(reserve),
                "recurring_capex": round2(recurring),
            }
        )

    # Summary
    grand_total = sum((r.amount for r in results), Decimal("0"))
    capex_per_unit = grand_total / total_units if total_units > 0 else Decimal("0")

    return {
        "by_month": by_month,
        "by_category": by_category,
        "totals_by_year": totals_by_year,
        "summary": {
            "total_capex": round2(grand_total),
            "capex_per_unit": round2(capex_per_unit),
        },
    }
