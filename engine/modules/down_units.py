"""Down-unit restoration: a capital project with a schedule (ADR 0003 decision q).

A unit the rent roll shows as down is restored before it can be leased. The
canonical's ``capital_plan.down_unit_restoration`` block lists the units
(``unit_id``, ``cohort_id``, optional per-unit ``scope``, ``cost``,
``start_lag_months``, ``duration_months``) and the block-level defaults the
assumption resolver fills (``scope``, ``cost_per_unit``,
``start_lag_months``, ``duration_months``, ``pace_units_per_month``,
``months_to_lease``, ``funding``, optional ``rent_premium_monthly``).

The schedule, month index 0 being the analysis start month:

- a unit can start once its start lag has passed, and the crew starts at most
  ``pace_units_per_month`` units a month (units in order of their earliest
  start, then as listed);
- it is worked for ``duration_months`` (start through completion), its cost
  spread evenly over those months (cents exact, so the months sum to the
  budget);
- it is marketed for ``months_to_lease`` after completion and earns rent from
  the month after; until then it is offline: no rent, out of gross potential
  rent and of every per-unit revenue program.

A restored unit leases at the market rent of its cohort (no loss to lease on
the new lease) plus ``rent_premium_monthly`` (0 unless the analyst sets one),
and from then on follows the cohort's vacancy curve.

The budget is funded at close (``equity_at_close``, the default, or
``lender_holdback``): it never enters operating cash flow. It is its own
capital-plan line, separate from immediate capex (bucket 1) and from the
value-add renovation budget; the engine keeps the down units out of any
renovation program's pool, so no unit is budgeted twice.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Tuple

from engine import underwriting_policy as house
from engine.modules.time_grid import TimeGrid
from engine.modules.util import CurveLookup, dec, round2

BLOCK_KEY = "down_unit_restoration"
LABEL = "Down-unit restoration"
RESTORED_SUFFIX = "__restored"


def restoration_block(inputs: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """The canonical's restoration block when it lists at least one unit, else None."""
    plan = inputs.get("capital_plan")
    block = plan.get(BLOCK_KEY) if isinstance(plan, Mapping) else None
    if not isinstance(block, Mapping) or not block.get("units"):
        return None
    return block


def restoration_funding(block: Mapping[str, Any]) -> str:
    funding = block.get("funding") or house.HOUSE_DOWN_UNIT_FUNDING
    if funding not in house.DOWN_UNIT_FUNDING_MODES:
        raise ValueError(
            f"capital_plan.{BLOCK_KEY}.funding must be one of {house.DOWN_UNIT_FUNDING_MODES}"
        )
    return funding


def _year_built(inputs: Mapping[str, Any]) -> Optional[int]:
    for value in (
        (inputs.get("deal_profile") or {}).get("year_built"),
        (inputs.get("metadata") or {}).get("year_built"),
        ((inputs.get("metadata") or {}).get("property_summary") or {}).get("year_built"),
    ):
        try:
            if value not in (None, "") and not isinstance(value, bool):
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


def resolved_units(inputs: Mapping[str, Any], block: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Each unit's scope, cost, start lag and duration as the engine runs them.

    A unit's own value wins; a unit at the block's scope takes the block's
    values; a unit with its own scope and no values takes the house values for
    that scope (the resolver normally writes them onto the unit).
    """
    block_scope = block.get("scope") or house.HOUSE_DOWN_UNIT_DEFAULT_SCOPE
    year_built = _year_built(inputs)
    out: List[Dict[str, Any]] = []
    for index, unit in enumerate(block.get("units") or []):
        scope = unit.get("scope") or block_scope
        if scope not in house.DOWN_UNIT_SCOPES:
            raise ValueError(f"down unit {unit.get('unit_id')!r}: scope must be one of {house.DOWN_UNIT_SCOPES}")
        at_block_scope = scope == block_scope

        def pick(field: str, block_field: str, fallback: Any) -> Any:
            if unit.get(field) is not None:
                return unit[field]
            if at_block_scope and block.get(block_field) is not None:
                return block[block_field]
            return fallback

        cost = dec(pick("cost", "cost_per_unit", house.default_down_unit_cost_per_unit(scope, year_built)))
        lag = int(pick("start_lag_months", "start_lag_months", house.HOUSE_DOWN_UNIT_START_LAG_MONTHS[scope]))
        duration = int(pick("duration_months", "duration_months", house.HOUSE_DOWN_UNIT_DURATION_MONTHS[scope]))
        if cost < 0 or lag < 0 or duration < 1:
            raise ValueError(
                f"down unit {unit.get('unit_id')!r}: cost and start lag must be >= 0 and duration >= 1 month"
            )
        out.append({
            "unit_id": str(unit.get("unit_id") or f"down_{index + 1}"),
            "cohort_id": unit.get("cohort_id"),
            "scope": scope,
            "cost": cost,
            "cost_source": unit.get("cost_source"),
            "start_lag_months": lag,
            "duration_months": duration,
        })
    return out


def _month(start: str, offset: int) -> str:
    year, mon = int(start[:4]), int(start[5:7])
    index = year * 12 + (mon - 1) + offset
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _spread(cost: Decimal, months: int) -> List[Decimal]:
    """``cost`` over ``months`` in cents, the remainder cents in the first months."""
    cents = int((cost * 100).to_integral_value())
    base, rest = divmod(cents, months)
    return [Decimal(base + (1 if k < rest else 0)) / Decimal(100) for k in range(months)]


def _cohort_for(cohort_id: Any, cohorts: List[Mapping[str, Any]]) -> Tuple[str, bool]:
    ids = [str(c["cohort_id"]) for c in cohorts]
    if cohort_id is not None and str(cohort_id) in ids:
        return str(cohort_id), False
    # A cohort the canonical no longer carries (a rebased unit mix): the largest cohort.
    largest = max(cohorts, key=lambda c: int(c.get("unit_count") or 0))
    return str(largest["cohort_id"]), True


def compute_down_unit_restoration(
    time_grid: TimeGrid,
    inputs: Mapping[str, Any],
    *,
    market_rent_curve: Optional[List[Mapping[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """The restoration schedule, its monthly capex and the units offline, or None.

    ``market_rent_curve`` (the grown curve the revenue module uses; default the
    canonical's) prices ``down_unit_loss``: the market rent of the units
    offline each month.
    """
    block = restoration_block(inputs)
    if block is None:
        return None
    cohorts = list(inputs.get("unit_cohorts") or [])
    if not cohorts:
        raise ValueError(f"capital_plan.{BLOCK_KEY} needs unit_cohorts")
    funding = restoration_funding(block)
    pace = int(block.get("pace_units_per_month") or house.HOUSE_DOWN_UNIT_PACE_UNITS_PER_MONTH)
    months_to_lease = block.get("months_to_lease")
    months_to_lease = int(house.HOUSE_DOWN_UNIT_MONTHS_TO_LEASE if months_to_lease is None else months_to_lease)
    if pace < 1 or months_to_lease < 0:
        raise ValueError(f"capital_plan.{BLOCK_KEY}: pace must be >= 1 unit a month and months_to_lease >= 0")
    premium = dec(block.get("rent_premium_monthly") or 0)
    units = resolved_units(inputs, block)
    start = time_grid.month_ids[0]
    curve = list(market_rent_curve if market_rent_curve is not None else inputs.get("market_rent_curve") or [])

    started: Counter = Counter()
    order = sorted(range(len(units)), key=lambda i: (units[i]["start_lag_months"], i))
    for i in order:
        unit = units[i]
        slot = unit["start_lag_months"]
        while started[slot] >= pace:
            slot += 1
        started[slot] += 1
        cohort_id, reassigned = _cohort_for(unit["cohort_id"], cohorts)
        unit.update({
            "cohort_id": cohort_id,
            "cohort_reassigned": reassigned,
            "start_index": slot,
            "completion_index": slot + unit["duration_months"] - 1,
            "rent_start_index": slot + unit["duration_months"] + months_to_lease,
            "monthly_spend": _spread(unit["cost"], unit["duration_months"]),
        })

    months = time_grid.month_ids
    lookups = {
        cid: CurveLookup([r for r in curve if r["cohort_id"] == cid], "market_rent")
        for cid in {u["cohort_id"] for u in units}
    } if curve else {}
    offline_by_cohort: Dict[str, Dict[str, int]] = {}
    restored_by_cohort: Dict[str, Dict[str, int]] = {}
    by_month: List[Dict[str, Any]] = []
    for index, month in enumerate(months):
        capex = Decimal("0")
        loss = Decimal("0")
        row = {"month": month, "units_started": 0, "units_under_construction": 0, "units_completed": 0,
               "units_rent_starting": 0, "offline_units": 0, "restored_units": 0}
        for unit in units:
            cid = unit["cohort_id"]
            offline_by_cohort.setdefault(cid, {}).setdefault(month, 0)
            restored_by_cohort.setdefault(cid, {}).setdefault(month, 0)
            if unit["start_index"] <= index <= unit["completion_index"]:
                capex += unit["monthly_spend"][index - unit["start_index"]]
                row["units_under_construction"] += 1
            row["units_started"] += int(index == unit["start_index"])
            row["units_completed"] += int(index == unit["completion_index"])
            row["units_rent_starting"] += int(index == unit["rent_start_index"])
            if index < unit["rent_start_index"]:
                row["offline_units"] += 1
                offline_by_cohort[cid][month] += 1
                if lookups:
                    loss += lookups[cid](month)
            else:
                row["restored_units"] += 1
                restored_by_cohort[cid][month] += 1
        row["capex"] = float(round2(capex))
        row["down_unit_loss"] = float(round2(loss))
        by_month.append(row)

    by_year: List[Dict[str, Any]] = []
    for offset in range(0, len(by_month), 12):
        chunk = by_month[offset:offset + 12]
        by_year.append({
            "analysis_year": offset // 12 + 1,
            "months": [chunk[0]["month"], chunk[-1]["month"]],
            "capex": float(round2(sum((dec(r["capex"]) for r in chunk), Decimal("0")))),
            "units_started": sum(r["units_started"] for r in chunk),
            "units_completed": sum(r["units_completed"] for r in chunk),
            "units_rent_starting": sum(r["units_rent_starting"] for r in chunk),
            "down_unit_loss": float(round2(sum((dec(r["down_unit_loss"]) for r in chunk), Decimal("0")))),
        })

    total = sum((u["cost"] for u in units), Decimal("0"))
    in_hold = sum((dec(r["capex"]) for r in by_month), Decimal("0"))
    last = len(months) - 1
    schedule = [
        {
            "unit_id": u["unit_id"],
            "cohort_id": u["cohort_id"],
            "cohort_reassigned": u["cohort_reassigned"],
            "scope": u["scope"],
            "cost": float(round2(u["cost"])),
            "cost_source": u["cost_source"],
            "start_lag_months": u["start_lag_months"],
            "duration_months": u["duration_months"],
            "start_month": _month(start, u["start_index"]),
            "completion_month": _month(start, u["completion_index"]),
            "rent_start_month": _month(start, u["rent_start_index"]),
        }
        for u in sorted(units, key=lambda u: (u["start_index"], u["unit_id"]))
    ]
    return {
        "label": LABEL,
        "units": schedule,
        "by_month": by_month,
        "by_analysis_year": by_year,
        "offline_by_cohort": offline_by_cohort,
        "restored_by_cohort": restored_by_cohort,
        "down_by_cohort": dict(Counter(u["cohort_id"] for u in units)),
        "rent_premium_monthly": float(premium),
        "summary": {
            "units": len(units),
            "units_by_scope": dict(Counter(u["scope"] for u in units)),
            "total_budget": float(round2(total)),
            "capex_in_hold": float(round2(in_hold)),
            "capex_after_analysis_end": float(round2(total - in_hold)),
            "funding": funding,
            "pace_units_per_month": pace,
            "months_to_lease": months_to_lease,
            "rent_premium_monthly": float(premium),
            "first_start_month": min(s["start_month"] for s in schedule),
            "last_completion_month": max(s["completion_month"] for s in schedule),
            "last_rent_start_month": max(s["rent_start_month"] for s in schedule),
            "units_not_leased_by_analysis_end": sum(1 for u in units if u["rent_start_index"] > last),
            "rule": (
                "Each down unit is offline (no rent) until restored and leased; its cost is spread evenly over its "
                "work months and funded at close, never in operating cash flow; restored units lease at market."
            ),
        },
    }


def restoration_budget(inputs: Mapping[str, Any]) -> Tuple[Decimal, str, Decimal]:
    """``(total budget, funding, amount already in the stated bases)``; zeros without a block."""
    block = restoration_block(inputs)
    if block is None:
        return Decimal("0"), house.HOUSE_DOWN_UNIT_FUNDING, Decimal("0")
    total = sum((u["cost"] for u in resolved_units(inputs, block)), Decimal("0"))
    return total, restoration_funding(block), dec(block.get("amount_in_stated_basis") or 0)


def restored_cohort_id(cohort_id: str) -> str:
    return f"{cohort_id}{RESTORED_SUFFIX}"


def rentable_cohorts(inputs: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """``unit_cohorts`` less each cohort's down units: the pool a renovation program can draw from.

    The engine and backsolve's renovation budget use the same pool, so a down
    unit is never budgeted twice (restoration and value-add renovation).
    """
    cohorts = [dict(c) for c in inputs.get("unit_cohorts") or []]
    block = restoration_block(inputs)
    if block is None or not cohorts:
        return cohorts
    down: Counter = Counter()
    for unit in block.get("units") or []:
        down[_cohort_for(unit.get("cohort_id"), cohorts)[0]] += 1
    for cohort in cohorts:
        cohort["unit_count"] = max(0, int(cohort.get("unit_count") or 0) - down.get(str(cohort["cohort_id"]), 0))
    return cohorts
