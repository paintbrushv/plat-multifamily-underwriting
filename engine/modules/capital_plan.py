"""Capital plan: the three capex buckets (ADR 0003 §3).

1. Immediate / deferred capex at close (``capital_plan.immediate_capex``): a
   one-time amount funded with equity at close or a lender holdback. It never
   enters the monthly cash flows; it is part of the Day-0 basis (see
   ``engine.engine``), next to (never inside) the renovation budget.
2. Recurring capex by vintage (``capital_plan.recurring_capex``): annual $/unit
   below NOI, grown every analysis year (the opex convention: year 1 flat,
   growth from analysis year 2).
3. The lender replacement reserve (``replacement_reserves``): a floor, not an
   addition. The charge below NOI each month is
   ``max(recurring capex, replacement reserve)``; it is carried in the cash
   flow's ``replacement_reserves`` line, the one place reserves have always
   entered, so nothing is charged twice.

NOI stays before capex: going-in cap, yield on cost, exit value and DSCR use
NOI; unlevered and levered cash flow, cash-on-cash, IRR and the equity
multiple are after the below-NOI charge.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional

from engine.modules.opex import _calculate_years_elapsed
from engine.modules.time_grid import TimeGrid
from engine.modules.util import dec, get_total_units, month_id, round2

FUNDING_EQUITY_AT_CLOSE = "equity_at_close"
FUNDING_LENDER_HOLDBACK = "lender_holdback"
IMMEDIATE_CAPEX_FUNDING_MODES = (FUNDING_EQUITY_AT_CLOSE, FUNDING_LENDER_HOLDBACK)

BINDING_RECURRING = "recurring_capex"
BINDING_RESERVE = "replacement_reserve"
BINDING_EQUAL = "equal"
BINDING_NONE = "none"
BINDING_MIXED = "mixed"

CHARGE_RULE = "max(recurring capex, lender replacement reserve) per month; never both"
METRIC_BASIS = {
    "net_operating_income": "before capex (the below-NOI charge, renovation and immediate capex are excluded)",
    "going_in_cap_rate": "year-1 NOI / purchase price (before capex)",
    "exit_value": "forward NOI / exit cap rate (before capex)",
    "dscr": "NOI / debt service (before capex)",
    "unleveraged_cash_flow": "after the below-NOI charge and operating capex",
    "cash_on_cash": "levered cash flow after the below-NOI charge / total equity basis (immediate capex funded "
                    "at close is in the basis)",
    "irr_and_equity_multiple": "after the below-NOI charge, with immediate capex in the Day-0 outlay",
}


def capital_plan_section(inputs: Mapping[str, Any]) -> Mapping[str, Any]:
    plan = inputs.get("capital_plan")
    return plan if isinstance(plan, Mapping) else {}


def immediate_capex(inputs: Mapping[str, Any]) -> tuple[Decimal, str, Decimal]:
    """``(amount, funding, amount_in_stated_basis)`` of the immediate-capex bucket.

    ``amount_in_stated_basis`` is how much of it the caller's stated
    ``total_equity_basis`` / ``total_unlevered_basis`` already carry (backsolve's
    priced case states it); the engine adds the rest. ``0`` when absent.
    """
    block = capital_plan_section(inputs).get("immediate_capex")
    if not isinstance(block, Mapping):
        return Decimal("0"), FUNDING_EQUITY_AT_CLOSE, Decimal("0")
    funding = block.get("funding") or FUNDING_EQUITY_AT_CLOSE
    if funding not in IMMEDIATE_CAPEX_FUNDING_MODES:
        raise ValueError(f"capital_plan.immediate_capex.funding must be one of {IMMEDIATE_CAPEX_FUNDING_MODES}")
    return dec(block.get("amount") or 0), funding, dec(block.get("amount_in_stated_basis") or 0)


def _recurring_by_month(
    time_grid: TimeGrid,
    units: Decimal,
    block: Mapping[str, Any],
    analysis_start: str,
) -> Dict[str, Decimal]:
    per_unit = dec(block.get("per_unit_annual") or 0)
    growth = dec(block.get("growth_rate") or 0)
    start = month_id(block["start_period"]) if block.get("start_period") else time_grid.month_ids[0]
    end = month_id(block["end_period"]) if block.get("end_period") else time_grid.month_ids[-1]
    out: Dict[str, Decimal] = {}
    for month in time_grid.month_ids:
        if not start <= month <= end:
            continue
        years = _calculate_years_elapsed(month, analysis_start)
        out[month] = per_unit * units * (Decimal("1") + growth) ** years / Decimal("12")
    return out


def _reserve_by_month(time_grid: TimeGrid, segments: List[Mapping[str, Any]]) -> Dict[str, Decimal]:
    out: Dict[str, Decimal] = {}
    for seg in segments:
        seg_start = month_id(seg["start_period"])
        seg_end = month_id(seg["end_period"])
        monthly = dec(seg["annual_amount"]) / Decimal("12")
        for month in time_grid.month_ids:
            if seg_start <= month <= seg_end:
                out[month] = out.get(month, Decimal("0")) + monthly
    return out


def _binding(recurring: Decimal, reserve: Decimal) -> str:
    if recurring == 0 and reserve == 0:
        return BINDING_NONE
    if recurring > reserve:
        return BINDING_RECURRING
    if reserve > recurring:
        return BINDING_RESERVE
    return BINDING_EQUAL


def compute_below_noi_charge(
    time_grid: TimeGrid,
    inputs: Mapping[str, Any],
) -> tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """The monthly below-NOI capital charge and its disclosure.

    Returns ``(reserves_by_month, disclosure)``. ``reserves_by_month`` rows
    carry ``replacement_reserves`` (the charge the cash flow deducts),
    ``replacement_reserve_floor`` (bucket 3), ``recurring_capex_by_vintage``
    (bucket 2) and ``binding``. Without recurring capex the charge is the
    replacement reserve exactly as before and ``disclosure`` is None; with
    neither, both are empty.
    """
    segments = list(inputs.get("replacement_reserves") or [])
    block = capital_plan_section(inputs).get("recurring_capex")
    has_recurring = isinstance(block, Mapping) and block.get("per_unit_annual") is not None
    if not segments and not has_recurring:
        return [], None
    analysis_start = str(inputs["time_grid"]["analysis_start_date"])
    reserve = _reserve_by_month(time_grid, segments)
    recurring: Dict[str, Decimal] = {}
    if has_recurring:
        recurring = _recurring_by_month(
            time_grid, get_total_units(inputs.get("unit_cohorts") or []), block, analysis_start
        )
    rows: List[Dict[str, Any]] = []
    for month in time_grid.month_ids:
        res = reserve.get(month, Decimal("0"))
        rec = recurring.get(month, Decimal("0"))
        rows.append({
            "month": month,
            "replacement_reserves": round2(max(res, rec)),
            "replacement_reserve_floor": round2(res),
            "recurring_capex_by_vintage": round2(rec),
            "binding": _binding(round2(rec), round2(res)),
        })
    if not has_recurring:
        return [{"month": r["month"], "replacement_reserves": r["replacement_reserves"]} for r in rows], None

    by_year: List[Dict[str, Any]] = []
    for index in range(0, len(rows), 12):
        chunk = rows[index:index + 12]
        bindings = {r["binding"] for r in chunk} - {BINDING_NONE}
        by_year.append({
            "analysis_year": index // 12 + 1,
            "months": [chunk[0]["month"], chunk[-1]["month"]],
            "recurring_capex_by_vintage": float(round2(sum((dec(r["recurring_capex_by_vintage"]) for r in chunk),
                                                           Decimal("0")))),
            "replacement_reserve_floor": float(round2(sum((dec(r["replacement_reserve_floor"]) for r in chunk),
                                                          Decimal("0")))),
            "below_noi_capex_charge": float(round2(sum((dec(r["replacement_reserves"]) for r in chunk),
                                                       Decimal("0")))),
            "binding": bindings.pop() if len(bindings) == 1 else (BINDING_MIXED if bindings else BINDING_NONE),
        })
    disclosure = {
        "rule": CHARGE_RULE,
        "recurring_capex": {
            "per_unit_annual_year_1": float(dec(block.get("per_unit_annual") or 0)),
            "growth_rate": float(dec(block.get("growth_rate") or 0)),
        },
        "by_analysis_year": by_year,
    }
    return rows, disclosure
