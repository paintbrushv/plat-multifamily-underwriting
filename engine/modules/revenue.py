from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional

from engine.modules.time_grid import TimeGrid
from engine.modules.util import dec, month_id, round2


@dataclass(frozen=True)
class BaseRentMonthResult:
    month: str
    net_rent: Decimal
    billed_rent_after_vacancy: Decimal
    billed_after_concessions: Decimal
    concession_amount: Decimal
    market_rent: Decimal
    inplace_rent: Decimal
    physical_vacancy_loss: Decimal
    collection_loss: Decimal


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


def _compute_concession_for_cohort(
    concession_schedule: List[Dict[str, Any]],
    cohort_id: str,
    month: str,
    billed_after_vacancy: Decimal,
    occupied_units: Decimal,
) -> Decimal:
    """Compute total concession deduction for one cohort in one month.

    Concessions stack additively when multiple apply to the same cohort/month.
    """
    total = Decimal("0")
    mid = month_id(month)
    for entry in concession_schedule:
        start = month_id(entry["start_month"])
        end = month_id(entry["end_month"])
        if not (start <= mid <= end):
            continue
        target = entry["applies_to_cohort"]
        if target != "ALL" and target != cohort_id:
            continue

        ctype = entry["concession_type"]
        amount = dec(entry["amount"])
        if ctype == "free_months":
            # N free months on 12-month lease = N/12 reduction of billed rent
            total += billed_after_vacancy * (amount / Decimal("12"))
        elif ctype == "fixed_dollar":
            total += amount * occupied_units
        elif ctype == "pct_rent":
            total += billed_after_vacancy * amount
    return total


def compute_base_rent(
    time_grid: TimeGrid,
    unit_cohorts: List[Dict[str, Any]],
    market_rent_curve: List[Dict[str, Any]],
    loss_to_lease: List[Dict[str, Any]],
    physical_vacancy_curve: List[Dict[str, Any]],
    collection_loss_curve: List[Dict[str, Any]],
    dynamic_unit_counts: Optional[Dict[str, Dict[str, int]]] = None,
    concession_schedule: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    # Order-preserving dedup: defense-in-depth against malformed inputs that
    # bypass validation (skip_validation=True path). Iterating duplicate
    # cohort_ids would double-count units; dict.fromkeys preserves first-occurrence
    # order while collapsing duplicates.
    cohort_ids = list(dict.fromkeys(c["cohort_id"] for c in unit_cohorts))
    # Include output cohorts from dynamic_unit_counts that aren't in unit_cohorts
    if dynamic_unit_counts:
        for cid in dynamic_unit_counts:
            if cid not in cohort_ids:
                cohort_ids.append(cid)
    mr_by_cohort = {cid: [r for r in market_rent_curve if r["cohort_id"] == cid] for cid in cohort_ids}
    ltl_by_cohort = {cid: [r for r in loss_to_lease if r["cohort_id"] == cid] for cid in cohort_ids}
    vac_by_cohort = {cid: [r for r in physical_vacancy_curve if r["cohort_id"] == cid] for cid in cohort_ids}

    cl_rent = [r for r in collection_loss_curve if r["applies_to"] == "Rent"]
    cl_all = [r for r in collection_loss_curve if r["applies_to"] == "ALL"]

    def loss_rate_for_rent(month: str) -> Decimal:
        try:
            return _find_curve_value(cl_rent, month, "loss_rate")
        except KeyError:
            return _find_curve_value(cl_all, month, "loss_rate")

    cl_rate_by_month = {m: loss_rate_for_rent(m) for m in time_grid.month_ids}

    # Build cohort lookup for static unit counts
    cohort_lookup = {c["cohort_id"]: c for c in unit_cohorts}

    concessions = concession_schedule or []

    by_month: List[BaseRentMonthResult] = []
    for month in time_grid.month_ids:
        market_total = Decimal("0")
        inplace_total = Decimal("0")
        billed_after_vacancy_total = Decimal("0")
        concession_total = Decimal("0")

        for cohort_id in cohort_ids:
            # Determine unit count: dynamic overrides static
            if dynamic_unit_counts and cohort_id in dynamic_unit_counts:
                static_count = cohort_lookup[cohort_id]["unit_count"] if cohort_id in cohort_lookup else 0
                unit_count = dec(dynamic_unit_counts[cohort_id].get(month, static_count))
            elif cohort_id in cohort_lookup:
                unit_count = dec(cohort_lookup[cohort_id]["unit_count"])
            else:
                unit_count = Decimal("0")

            market_rent = _find_curve_value(mr_by_cohort[cohort_id], month, "market_rent")
            ltl = _find_curve_value(ltl_by_cohort[cohort_id], month, "ltl_percent")
            vacancy = _find_curve_value(vac_by_cohort[cohort_id], month, "vacancy_rate")

            inplace_rent = market_rent * (Decimal("1") - ltl)
            occupied_units = unit_count * (Decimal("1") - vacancy)
            billed_after_vacancy = inplace_rent * occupied_units

            # Concession deduction (per-cohort, after vacancy)
            if concessions:
                cohort_concession = _compute_concession_for_cohort(
                    concessions, cohort_id, month, billed_after_vacancy, occupied_units,
                )
                concession_total += cohort_concession

            market_total += market_rent * unit_count
            inplace_total += inplace_rent * unit_count
            billed_after_vacancy_total += billed_after_vacancy

        billed_after_concessions = billed_after_vacancy_total - concession_total
        cl_rate = cl_rate_by_month[month]
        net_rent = billed_after_concessions * (Decimal("1") - cl_rate)

        by_month.append(
            BaseRentMonthResult(
                month=month,
                net_rent=net_rent,
                billed_rent_after_vacancy=billed_after_vacancy_total,
                billed_after_concessions=billed_after_concessions,
                concession_amount=concession_total,
                market_rent=market_total,
                inplace_rent=inplace_total,
                physical_vacancy_loss=inplace_total - billed_after_vacancy_total,
                collection_loss=billed_after_concessions - net_rent,
            )
        )

    return {
        "by_month": [
            {
                "month": r.month,
                "net_rent": round2(r.net_rent),
                "billed_rent_after_vacancy": round2(r.billed_rent_after_vacancy),
                "billed_after_concessions": round2(r.billed_after_concessions),
                "concession_amount": round2(r.concession_amount),
                "market_rent": round2(r.market_rent),
                "inplace_rent": round2(r.inplace_rent),
                "physical_vacancy_loss": round2(r.physical_vacancy_loss),
                "collection_loss": round2(r.collection_loss),
            }
            for r in by_month
        ]
    }
