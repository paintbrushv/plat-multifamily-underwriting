"""
Cashflow Aggregation Module

Combines outputs from Revenue, OpEx, CapEx, and Debt modules to produce:
- Effective Gross Income (EGI)
- Net Operating Income (NOI)
- Unleveraged Cash Flow
- Leveraged Cash Flow

See: docs/modules/cashflow_spec.md for full specification
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional

from engine.modules.time_grid import TimeGrid
from engine.modules.util import dec, round2


def compute_cashflow(
    time_grid: TimeGrid,
    revenue_by_month: List[Dict[str, Any]],
    opex_by_month: List[Dict[str, Any]],
    capex_by_month: List[Dict[str, Any]],
    debt_by_month: List[Dict[str, Any]],
    utility_recovery_rate: float = 1.0,
    reserves_by_month: Optional[List[Dict[str, Any]]] = None,
    utility_recovery_by_month: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Aggregate all components into complete cashflow waterfall.

    Args:
        time_grid: Authoritative time grid
        revenue_by_month: Revenue totals (net_rent, net_programs, net_total_revenue)
        opex_by_month: OpEx totals (total_opex, recoverable_opex)
        capex_by_month: CapEx totals (total_capex)
        debt_by_month: Debt service totals (debt_service)
        utility_recovery_rate: Fraction of recoverable opex actually recovered (0-1)
        reserves_by_month: Replacement reserves (replacement_reserves)
        utility_recovery_by_month: Optional explicit utility recovery dollars by month.
            When supplied, overrides utility_recovery_rate for that month.

    Returns:
        Dict with by_month, by_year, summary
    """
    # Build lookups for each input source
    revenue_lookup: Dict[str, Dict[str, Decimal]] = {}
    for row in revenue_by_month:
        m = row["month"]
        revenue_lookup[m] = {
            "net_rent": dec(row.get("net_rent", 0)),
            "net_programs": dec(row.get("net_programs", 0)),
        }

    opex_lookup: Dict[str, Dict[str, Decimal]] = {}
    for row in opex_by_month:
        m = row["month"]
        opex_lookup[m] = {
            "total_opex": dec(row.get("total_opex", 0)),
            "recoverable_opex": dec(row.get("recoverable_opex", 0)),
        }

    capex_lookup: Dict[str, Decimal] = {}
    for row in capex_by_month:
        m = row["month"]
        capex_lookup[m] = dec(row.get("total_capex", 0))

    debt_lookup: Dict[str, Decimal] = {}
    loan_payoff_lookup: Dict[str, Decimal] = {}
    financing_draw_lookup: Dict[str, Decimal] = {}
    closing_costs_lookup: Dict[str, Decimal] = {}
    for row in debt_by_month:
        m = row["month"]
        debt_lookup[m] = dec(row.get("debt_service", 0))
        loan_payoff_lookup[m] = dec(row.get("loan_payoff", 0))
        # financing_draw: only mid-hold draws from additional loans (refi),
        # NOT primary loan draws (those are part of the initial equity calc)
        financing_draw_lookup[m] = dec(row.get("financing_draw", 0))
        closing_costs_lookup[m] = dec(row.get("loan_closing_costs", 0))

    reserves_lookup: Dict[str, Decimal] = {}
    if reserves_by_month:
        for row in reserves_by_month:
            m = row["month"]
            reserves_lookup[m] = dec(row.get("replacement_reserves", 0))

    utility_recovery_lookup: Dict[str, Decimal] = {}
    if utility_recovery_by_month:
        for row in utility_recovery_by_month:
            m = row["month"]
            utility_recovery_lookup[m] = dec(row.get("utility_recovery", 0))

    # Calculate cashflow for each month
    by_month: List[Dict[str, Any]] = []
    for month in time_grid.month_ids:
        # Get values from lookups (default to zero)
        rev = revenue_lookup.get(month, {"net_rent": Decimal("0"), "net_programs": Decimal("0")})
        opex = opex_lookup.get(month, {"total_opex": Decimal("0"), "recoverable_opex": Decimal("0")})
        capex = capex_lookup.get(month, Decimal("0"))
        debt_service = debt_lookup.get(month, Decimal("0"))
        loan_payoff = loan_payoff_lookup.get(month, Decimal("0"))
        replacement_reserves = reserves_lookup.get(month, Decimal("0"))

        net_rent = rev["net_rent"]
        net_programs = rev["net_programs"]
        total_opex = opex["total_opex"]
        recoverable_opex = opex["recoverable_opex"]
        financing_draw = financing_draw_lookup.get(month, Decimal("0"))
        loan_closing_costs = closing_costs_lookup.get(month, Decimal("0"))

        # Calculate cashflow waterfall
        # EGI = Net Rent + Net Programs + Utility Recovery (recoverable expenses * recovery rate)
        if month in utility_recovery_lookup:
            utility_recovery = utility_recovery_lookup[month]
        else:
            utility_recovery = recoverable_opex * dec(utility_recovery_rate)
        effective_gross_income = net_rent + net_programs + utility_recovery

        # NOI = EGI - Total OpEx (matches RedIQ CF Calcs row 59: NOI before Reserves)
        net_operating_income = effective_gross_income - total_opex

        # Unleveraged CF = NOI - Reserves - CapEx
        unleveraged_cash_flow = net_operating_income - replacement_reserves - capex

        # Leveraged CF = Unleveraged CF - Debt Service - Loan Payoff
        #              + Financing Draws - Loan Closing Costs
        # financing_draw = mid-hold draws from additional loans only (e.g., refi)
        # Primary loan draws are excluded — they're part of initial equity calc
        leveraged_cash_flow = (
            unleveraged_cash_flow - debt_service - loan_payoff
            + financing_draw - loan_closing_costs
        )

        by_month.append(
            {
                "month": month,
                "net_rent": round2(net_rent),
                "net_programs": round2(net_programs),
                "utility_recovery": round2(utility_recovery),
                "effective_gross_income": round2(effective_gross_income),
                "total_opex": round2(total_opex),
                "net_operating_income": round2(net_operating_income),
                "replacement_reserves": round2(replacement_reserves),
                "total_capex": round2(capex),
                "unleveraged_cash_flow": round2(unleveraged_cash_flow),
                "debt_service": round2(debt_service),
                "loan_payoff": round2(loan_payoff),
                "financing_draw": round2(financing_draw),
                "loan_closing_costs": round2(loan_closing_costs),
                "leveraged_cash_flow": round2(leveraged_cash_flow),
            }
        )

    # Aggregate by year
    by_year: List[Dict[str, Any]] = []
    for year in time_grid.year_ids:
        year_months = [m for m in by_month if m["month"].startswith(year)]

        net_rent = sum((dec(m["net_rent"]) for m in year_months), Decimal("0"))
        net_programs = sum((dec(m["net_programs"]) for m in year_months), Decimal("0"))
        utility_recovery = sum((dec(m["utility_recovery"]) for m in year_months), Decimal("0"))
        egi = sum((dec(m["effective_gross_income"]) for m in year_months), Decimal("0"))
        total_opex = sum((dec(m["total_opex"]) for m in year_months), Decimal("0"))
        noi = sum((dec(m["net_operating_income"]) for m in year_months), Decimal("0"))
        reserves = sum((dec(m.get("replacement_reserves", 0)) for m in year_months), Decimal("0"))
        total_capex = sum((dec(m["total_capex"]) for m in year_months), Decimal("0"))
        unlev_cf = sum((dec(m["unleveraged_cash_flow"]) for m in year_months), Decimal("0"))
        debt_service = sum((dec(m["debt_service"]) for m in year_months), Decimal("0"))
        loan_payoff = sum((dec(m.get("loan_payoff", 0)) for m in year_months), Decimal("0"))
        financing_draw = sum((dec(m.get("financing_draw", 0)) for m in year_months), Decimal("0"))
        loan_closing_costs = sum((dec(m.get("loan_closing_costs", 0)) for m in year_months), Decimal("0"))
        lev_cf = sum((dec(m["leveraged_cash_flow"]) for m in year_months), Decimal("0"))

        by_year.append(
            {
                "year": year,
                "net_rent": round2(net_rent),
                "net_programs": round2(net_programs),
                "utility_recovery": round2(utility_recovery),
                "effective_gross_income": round2(egi),
                "total_opex": round2(total_opex),
                "net_operating_income": round2(noi),
                "replacement_reserves": round2(reserves),
                "total_capex": round2(total_capex),
                "unleveraged_cash_flow": round2(unlev_cf),
                "debt_service": round2(debt_service),
                "loan_payoff": round2(loan_payoff),
                "financing_draw": round2(financing_draw),
                "loan_closing_costs": round2(loan_closing_costs),
                "leveraged_cash_flow": round2(lev_cf),
            }
        )

    # Summary calculations
    total_egi = sum((dec(m["effective_gross_income"]) for m in by_month), Decimal("0"))
    total_opex = sum((dec(m["total_opex"]) for m in by_month), Decimal("0"))
    total_noi = sum((dec(m["net_operating_income"]) for m in by_month), Decimal("0"))
    total_capex = sum((dec(m["total_capex"]) for m in by_month), Decimal("0"))
    total_debt_service = sum((dec(m["debt_service"]) for m in by_month), Decimal("0"))
    total_lev_cf = sum((dec(m["leveraged_cash_flow"]) for m in by_month), Decimal("0"))

    # Calculate ratios (avoid division by zero)
    noi_margin = total_noi / total_egi if total_egi > 0 else Decimal("0")
    opex_ratio = total_opex / total_egi if total_egi > 0 else Decimal("0")

    return {
        "by_month": by_month,
        "by_year": by_year,
        "summary": {
            "total_egi": round2(total_egi),
            "total_noi": round2(total_noi),
            "total_capex": round2(total_capex),
            "total_debt_service": round2(total_debt_service),
            "total_leveraged_cf": round2(total_lev_cf),
            "average_noi_margin": round2(noi_margin),
            "average_opex_ratio": round2(opex_ratio),
        },
    }
