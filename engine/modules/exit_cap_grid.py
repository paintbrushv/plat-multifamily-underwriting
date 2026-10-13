"""Exit-cap sensitivity grid (ADR 0003 §3).

Every run reports its base case at the named exit-cap offsets
``HOUSE_EXIT_CAP_SENSITIVITY_OFFSETS_BPS`` around the exit cap it ran with,
whoever set it (the analyst, a backsolve policy, or the entry-anchored house
exit cap at the modeled price). Each point PINS its exit cap at base + offset:
it is never re-anchored to a price, so a stress point does not drift with a
re-measured going-in cap.

The exit cap only changes the reversion (gross sale price = exit NOI / exit
cap, and the sale costs on it; the exit NOI is the run's, forward by default,
ADR 0003 decision n); the operating cash flows, the debt and the
loan payoff do not move. Each point therefore reuses the base case's cash flow
series and shifts only the exit month (and exit year) by the change in net
reversion, then recomputes IRR and the equity multiple with the same functions
``compute_metrics`` uses, so the 0 bps point is the base case exactly.
Year-1 cash-on-cash does not depend on the exit cap and is shown once.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence

from engine import underwriting_policy as house
from engine.modules.metrics import return_series, returns_from_series
from engine.modules.util import dec, round2

RULE = (
    "Each point is pinned: exit cap = the base exit cap as run + offset, never re-anchored to a price. The base "
    "case's cash flows are reused; only the reversion moves: exit value = exit NOI (the run's window, forward "
    "by default) / exit cap, less sale costs; the loan payoff is unchanged."
)


def day0_bases(purchase_assumptions: Optional[Mapping[str, Any]]) -> tuple[Decimal, Decimal]:
    """``(total_unlevered_basis, total_equity_basis)`` exactly as ``compute_metrics`` reads them."""
    pa = purchase_assumptions or {"purchase_price": 0, "closing_costs": 0, "equity_contribution": 0}
    raw_price = pa.get("purchase_price")
    price = dec(raw_price) if raw_price is not None else Decimal("0")
    closing = dec(pa.get("closing_costs", 0))
    equity_contribution = dec(pa.get("equity_contribution", 0))
    total_equity_basis = dec(pa.get("total_equity_basis", float(equity_contribution)))
    total_unlevered_basis = dec(pa.get("total_unlevered_basis", float(price + closing)))
    return total_unlevered_basis, total_equity_basis


def _shifted(series: Dict[str, List[float]], month_index: int, year_index: Optional[int], delta: float):
    out = {key: list(values) for key, values in series.items()}
    if delta:
        for key in ("unlevered_monthly", "levered_monthly"):
            if month_index < len(out[key]):
                out[key][month_index] += delta
        if year_index is not None:
            for key in ("unlevered_annual", "levered_annual"):
                out[key][year_index] += delta
    return out


def compute_exit_cap_grid(
    *,
    cashflow_by_month: List[Dict[str, Any]],
    cashflow_by_year: List[Dict[str, Any]],
    exit_result: Mapping[str, Any],
    purchase_assumptions: Optional[Mapping[str, Any]],
    exit_assumptions: Optional[Mapping[str, Any]],
    cash_on_cash_year_1: Any = None,
    offsets_bps: Sequence[int] = house.HOUSE_EXIT_CAP_SENSITIVITY_OFFSETS_BPS,
) -> Optional[Dict[str, Any]]:
    """The grid for a run whose exit proceeds are already in ``cashflow_by_*``; None without an exit.

    ``exit_result`` is the run's ``compute_exit_proceeds`` record. Offsets that
    would take the exit cap to zero or below are left out.
    """
    if not exit_result or not exit_assumptions:
        return None
    base_cap = dec(exit_assumptions.get("exit_cap_rate", 0.05))
    if base_cap <= 0:
        return None
    sale_cost_pct = dec(exit_assumptions.get("sale_cost_percent", 0.02))
    exit_noi = dec(exit_result.get("exit_noi", exit_result.get("forward_noi")))
    exit_month_idx = int(exit_result["exit_month_idx"])
    exit_year = str(exit_result["exit_month"])[:4]
    unlevered_basis, equity_basis = day0_bases(purchase_assumptions)
    series = return_series(cashflow_by_month, cashflow_by_year, exit_month_idx, unlevered_basis, equity_basis)
    year_index = next(
        (i + 1 for i, row in enumerate(cashflow_by_year) if str(row.get("year")) == exit_year), None
    )

    def reversion(cap: Decimal) -> tuple[Decimal, Decimal]:
        gross = exit_noi / cap
        return gross, gross * sale_cost_pct

    base_gross, base_costs = reversion(base_cap)
    reported_gross = dec(exit_result["gross_sale_price"])
    reported_costs = dec(exit_result["sale_costs"])
    reported_net = dec(exit_result["net_sale_proceeds"])
    points: List[Dict[str, Any]] = []
    for offset in offsets_bps:
        cap = base_cap + Decimal(offset) / Decimal(10_000)
        if cap <= 0:
            continue
        d_gross = d_costs = Decimal("0")
        if offset != 0:
            gross, costs = reversion(cap)
            d_gross, d_costs = gross - base_gross, costs - base_costs
        delta = float(round2(d_gross - d_costs))
        returns = returns_from_series(_shifted(series, exit_month_idx + 1, year_index, delta))
        points.append({
            "offset_bps": int(offset),
            "exit_cap_rate": round(float(cap), 6),
            "exit_value": float(round2(reported_gross + d_gross)),
            "sale_costs": float(round2(reported_costs + d_costs)),
            "net_sale_proceeds": float(round2(reported_net + d_gross - d_costs)),
            "unlevered_irr": round(returns["unlevered_irr"], 4) if returns["unlevered_irr"] is not None else None,
            "levered_irr": round(returns["levered_irr"], 4) if returns["levered_irr"] is not None else None,
            "unlevered_em": round(returns["unlevered_em"], 2) if returns["unlevered_em"] is not None else None,
            "levered_em": round(returns["levered_em"], 2) if returns["levered_em"] is not None else None,
            "is_base": offset == 0,
        })
    return {
        "base_exit_cap_rate": float(exit_assumptions.get("exit_cap_rate", 0.05)),
        "offsets_bps": [int(o) for o in offsets_bps],
        "rule": RULE,
        "purchase_price": (purchase_assumptions or {}).get("purchase_price"),
        "exit_month": exit_result["exit_month"],
        # The NOI every point is valued on, and its window (the run's exit NOI).
        "forward_noi": float(round2(exit_noi)),
        "exit_noi_window": exit_result.get("exit_noi_window"),
        # Does not move with the exit cap: shown once, not per point.
        "cash_on_cash_year_1": cash_on_cash_year_1,
        "points": points,
    }


__all__ = ["RULE", "compute_exit_cap_grid", "day0_bases"]
