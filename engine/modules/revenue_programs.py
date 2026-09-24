from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional

from engine.modules.time_grid import TimeGrid
from engine.modules.util import dec, month_id, round2


def _find_curve_value(curve_rows: List[Dict[str, Any]], month: str, value_field: str) -> Decimal:
    month = month_id(month)
    matches = [
        row
        for row in curve_rows
        if month_id(row["start_period"]) <= month <= month_id(row["end_period"])
    ]
    if not matches:
        raise KeyError(f"Missing curve value for month={month} field={value_field}")
    if len(matches) > 1:
        raise ValueError(
            f"Ambiguous curve lookup for month={month}: {len(matches)} overlapping segments. "
            f"Validator should have caught this; check augmented_curves invariants."
        )
    return dec(matches[0][value_field])


def _program_is_active(program: Dict[str, Any], month: str) -> bool:
    month = month_id(month)
    start = month_id(program["start_period"])
    end = program.get("end_period")
    if end is None:
        return month >= start
    return start <= month <= month_id(end)


def compute_program_revenue(
    time_grid: TimeGrid,
    unit_cohorts: List[Dict[str, Any]],
    revenue_programs: List[Dict[str, Any]],
    program_adoption_curve: List[Dict[str, Any]],
    program_capacity: Optional[List[Dict[str, Any]]],
    collection_loss_curve: List[Dict[str, Any]],
    physical_vacancy_curve: Optional[List[Dict[str, Any]]] = None,
    market_rent_curve: Optional[List[Dict[str, Any]]] = None,
    loss_to_lease: Optional[List[Dict[str, Any]]] = None,
    dynamic_unit_counts: Optional[Dict[str, Dict[str, int]]] = None,
) -> Dict[str, Any]:
    units_by_cohort = {c["cohort_id"]: dec(c["unit_count"]) for c in unit_cohorts}
    if dynamic_unit_counts:
        for cohort_id in dynamic_unit_counts:
            units_by_cohort.setdefault(cohort_id, Decimal("0"))
    rent_by_cohort = {c["cohort_id"]: dec(c.get("initial_inplace_rent", 0)) for c in unit_cohorts}
    total_units = sum(units_by_cohort.values(), Decimal("0"))
    # Weighted average rent for "ALL" eligible_units with % rent pricing
    if total_units > 0:
        avg_rent = sum(rent_by_cohort[cid] * units_by_cohort[cid] for cid in units_by_cohort) / total_units
    else:
        avg_rent = Decimal("0")

    mr_by_cohort = {
        cid: [r for r in market_rent_curve or [] if r["cohort_id"] == cid]
        for cid in units_by_cohort
    }
    ltl_by_cohort = {
        cid: [r for r in loss_to_lease or [] if r["cohort_id"] == cid]
        for cid in units_by_cohort
    }

    def current_unit_count(cohort_id: str, month: str) -> Decimal:
        if dynamic_unit_counts and cohort_id in dynamic_unit_counts:
            return dec(dynamic_unit_counts[cohort_id].get(month, units_by_cohort.get(cohort_id, 0)))
        return units_by_cohort.get(cohort_id, Decimal("0"))

    def current_total_units(month: str) -> Decimal:
        return sum((current_unit_count(cohort_id, month) for cohort_id in units_by_cohort), Decimal("0"))

    def current_rent_for_cohort(cohort_id: str, month: str) -> Decimal:
        if mr_by_cohort.get(cohort_id) and ltl_by_cohort.get(cohort_id):
            market_rent = _find_curve_value(mr_by_cohort[cohort_id], month, "market_rent")
            ltl = _find_curve_value(ltl_by_cohort[cohort_id], month, "ltl_percent")
            return market_rent * (Decimal("1") - ltl)
        return rent_by_cohort.get(cohort_id, Decimal("0"))

    def current_average_rent(month: str) -> Decimal:
        month_units = Decimal("0")
        rent_total = Decimal("0")
        for cohort_id in units_by_cohort:
            units = current_unit_count(cohort_id, month)
            month_units += units
            rent_total += current_rent_for_cohort(cohort_id, month) * units
        if month_units > 0:
            return rent_total / month_units
        return avg_rent

    adoption_by_program = {}
    for p in revenue_programs:
        adoption_by_program[p["program_id"]] = [r for r in program_adoption_curve if r["program_id"] == p["program_id"]]

    capacity_by_program: Dict[str, Decimal] = {}
    for row in program_capacity or []:
        capacity_by_program[row["program_id"]] = dec(row["total_capacity"])

    cl_programs = [r for r in collection_loss_curve if r["applies_to"] == "Programs"]
    cl_all = [r for r in collection_loss_curve if r["applies_to"] == "ALL"]

    def loss_rate_for_programs(month: str) -> Decimal:
        try:
            return _find_curve_value(cl_programs, month, "loss_rate")
        except KeyError:
            return _find_curve_value(cl_all, month, "loss_rate")

    cl_rate_by_month = {m: loss_rate_for_programs(m) for m in time_grid.month_ids}

    by_month = []
    by_program_by_month = []

    for month in time_grid.month_ids:
        billed_total = Decimal("0")
        net_total = Decimal("0")
        for program in revenue_programs:
            if not _program_is_active(program, month):
                continue

            pricing_type = program["pricing_type"]

            eligible_units_raw = program["eligible_units"]
            if eligible_units_raw == "ALL":
                eligible_units = total_units
            else:
                if eligible_units_raw not in units_by_cohort:
                    raise KeyError(f"eligible_units must be 'ALL' or a valid cohort_id; got: {eligible_units_raw}")
                eligible_units = units_by_cohort[eligible_units_raw]

            adoption_rate = _find_curve_value(adoption_by_program[program["program_id"]], month, "adoption_rate")
            billable_units = eligible_units * adoption_rate
            if program["program_id"] in capacity_by_program:
                billable_units = min(billable_units, capacity_by_program[program["program_id"]])

            price_value = dec(program["price_value"])

            if pricing_type == "$/unit":
                billed = billable_units * price_value
            elif pricing_type == "% rent":
                # Revenue = price_value (as %) * current modeled rent * adopted_units
                if eligible_units_raw == "ALL":
                    current_eligible_units = current_total_units(month)
                    base_rent = current_average_rent(month)
                else:
                    current_eligible_units = current_unit_count(eligible_units_raw, month)
                    base_rent = current_rent_for_cohort(eligible_units_raw, month)
                billable_units = current_eligible_units * adoption_rate
                if program["program_id"] in capacity_by_program:
                    billable_units = min(billable_units, capacity_by_program[program["program_id"]])
                billed = price_value * base_rent * billable_units
            elif pricing_type == "$/asset":
                # Flat monthly amount for the entire asset, not per-unit
                billed = price_value
            elif pricing_type == "$/occupied_unit":
                # Revenue = price × occupied_units (units × (1 - vacancy_rate))
                # Use cohort-specific vacancy or weighted average for "ALL"
                vacancy_rate = Decimal("0")
                if physical_vacancy_curve:
                    if eligible_units_raw != "ALL" and eligible_units_raw in units_by_cohort:
                        cohort_vac = [r for r in physical_vacancy_curve if r.get("cohort_id") == eligible_units_raw]
                        try:
                            vacancy_rate = _find_curve_value(cohort_vac, month, "vacancy_rate")
                        except KeyError:
                            pass
                    elif total_units > 0:
                        # Weighted average vacancy across all cohorts
                        weighted_vac = Decimal("0")
                        for cid, cnt in units_by_cohort.items():
                            cohort_vac = [r for r in physical_vacancy_curve if r.get("cohort_id") == cid]
                            try:
                                v = _find_curve_value(cohort_vac, month, "vacancy_rate")
                            except KeyError:
                                v = Decimal("0")
                            weighted_vac += v * cnt
                        vacancy_rate = weighted_vac / total_units
                occupied_units = eligible_units * (Decimal("1") - vacancy_rate)
                billed = occupied_units * adoption_rate * price_value
            else:
                raise NotImplementedError(f"pricing_type not implemented: {pricing_type}")
            cl_rate = cl_rate_by_month[month]
            net = billed * (Decimal("1") - cl_rate)

            billed_total += billed
            net_total += net

            by_program_by_month.append(
                {
                    "month": month,
                    "program_id": program["program_id"],
                    "program_name": program["program_name"],
                    "billed_revenue": round2(billed),
                    "net_revenue": round2(net),
                    "billable_units": float(billable_units),
                    "adoption_rate": float(adoption_rate),
                }
            )

        by_month.append({"month": month, "billed_programs": round2(billed_total), "net_programs": round2(net_total)})

    return {"by_month": by_month, "by_program_by_month": by_program_by_month}
