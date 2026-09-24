"""
Investment Metrics Module

Calculates key investment performance metrics:
- Internal Rate of Return (IRR) - levered and unlevered
- Equity Multiple (EM)
- Debt Service Coverage Ratio (DSCR)
- Cash-on-Cash yield by year
- Going-in and exit yields

See: docs/modules/metrics_spec.md for full specification
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional

from engine.underwriting_policy import PRIMARY_COC_TARGET_PCT
from engine.modules.analysis_years import aggregate_analysis_years
from engine.modules.time_grid import TimeGrid
from engine.modules.util import calculate_irr, dec, month_id, round2, round4


# Keep local aliases for backward compatibility within this module
_calculate_irr = calculate_irr


def compute_exit_proceeds(
    time_grid: TimeGrid,
    cashflow_by_year: List[Dict[str, Any]],
    debt_by_month: List[Dict[str, Any]],
    purchase_assumptions: Optional[Dict[str, Any]] = None,
    exit_assumptions: Optional[Dict[str, Any]] = None,
    cashflow_by_month: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Compute exit / sale proceeds as a standalone helper.

    Returns dict with: exit_month, exit_month_idx, forward_noi,
    gross_sale_price, sale_costs, loan_payoff, net_sale_proceeds.
    """
    if not exit_assumptions:
        return {}

    exit_cap_rate = dec(exit_assumptions.get("exit_cap_rate", 0.05))
    sale_cost_percent = dec(exit_assumptions.get("sale_cost_percent", 0.02))
    exit_month = exit_assumptions.get("exit_month")

    if exit_month:
        exit_month = month_id(exit_month)
        if exit_month in time_grid.month_ids:
            exit_month_idx = time_grid.month_ids.index(exit_month)
        else:
            exit_month_idx = len(time_grid.month_ids) - 1
            exit_month = time_grid.month_ids[-1]
    else:
        exit_month = time_grid.month_ids[-1]
        exit_month_idx = len(time_grid.month_ids) - 1

    # Forward NOI for exit valuation.  When an explicit override is provided
    # (e.g., RedIQ's ReversionNOI), use it directly.  Otherwise, fall back to
    # trailing 12-month NOI from the cashflow.
    forward_noi_override = exit_assumptions.get("forward_noi_override")
    if forward_noi_override is not None:
        forward_noi = dec(forward_noi_override)
    elif cashflow_by_month:
        # Sum trailing 12 months of monthly NOI
        start_idx = max(0, exit_month_idx - 11)
        trailing = cashflow_by_month[start_idx:exit_month_idx + 1]
        trailing_noi = sum(dec(m.get("net_operating_income", 0)) for m in trailing)
        n_months = len(trailing)
        if n_months >= 12:
            forward_noi = trailing_noi
        elif n_months > 0:
            # Annualize partial-year data
            forward_noi = trailing_noi * Decimal("12") / Decimal(str(n_months))
        else:
            forward_noi = Decimal("0")
    else:
        # Fallback: year-based calculation
        forward_noi = Decimal("0")
        exit_year = exit_month[:4]
        exit_year_months = [m for m in time_grid.month_ids if m.startswith(exit_year)]

        if len(exit_year_months) >= 12:
            exit_year_data = next((y for y in cashflow_by_year if y["year"] == exit_year), None)
            forward_noi = dec(exit_year_data.get("net_operating_income", 0)) if exit_year_data else Decimal("0")
        else:
            prior_year = str(int(exit_year) - 1)
            prior_year_data = next((y for y in cashflow_by_year if y["year"] == prior_year), None)
            if prior_year_data:
                forward_noi = dec(prior_year_data.get("net_operating_income", 0))
            elif cashflow_by_year:
                forward_noi = dec(cashflow_by_year[-2].get("net_operating_income", 0)) if len(cashflow_by_year) > 1 else Decimal("0")

    # Gross sale price
    if exit_cap_rate > 0:
        gross_sale_price = forward_noi / exit_cap_rate
    else:
        gross_sale_price = Decimal("0")

    # Net proceeds
    sale_costs = gross_sale_price * sale_cost_percent

    # Loan payoff at exit: two sources
    # 1. If the debt module records a loan_payoff at the exit month (term = hold),
    #    the balance was already retired in the cashflow module.  We report it
    #    for display/comparison but do NOT subtract it again from proceeds.
    # 2. If the loan is still active at exit (term > hold), we use ending_balance.
    debt_balance_lookup: Dict[str, Decimal] = {}
    debt_payoff_lookup: Dict[str, Decimal] = {}
    for row in debt_by_month:
        debt_balance_lookup[row["month"]] = dec(row.get("ending_balance", 0))
        lp_val = dec(row.get("loan_payoff", 0))
        if lp_val > 0:
            debt_payoff_lookup[row["month"]] = lp_val

    # Remaining balance after any maturity payoff (used for net proceeds)
    remaining_balance = debt_balance_lookup.get(exit_month, Decimal("0"))

    # Display loan_payoff: use the debt module's payoff at or before exit.
    # When loan matures before exit (term < hold), the balance is already 0
    # but we still need to report the historical payoff for display/comparison.
    if exit_month in debt_payoff_lookup:
        loan_payoff = debt_payoff_lookup[exit_month]
    elif debt_payoff_lookup:
        # Loan matured before exit — find the latest payoff at or before exit
        prior_payoffs = {m: p for m, p in debt_payoff_lookup.items() if m <= exit_month}
        if prior_payoffs:
            loan_payoff = prior_payoffs[max(prior_payoffs)]
        else:
            loan_payoff = remaining_balance
    else:
        loan_payoff = remaining_balance

    net_sale_proceeds = gross_sale_price - sale_costs - remaining_balance

    return {
        "exit_month": exit_month,
        "exit_month_idx": exit_month_idx,
        "forward_noi": round2(forward_noi),
        "gross_sale_price": round2(gross_sale_price),
        "sale_costs": round2(sale_costs),
        "loan_payoff": round2(loan_payoff),
        "net_sale_proceeds": round2(net_sale_proceeds),
    }


def compute_cash_on_cash(
    cashflow_by_year: List[Dict[str, Any]],
    equity_basis: float | Decimal,
    fund_by_year: Optional[List[Dict[str, Any]]] = None,
    cashflow_by_month: Optional[List[Dict[str, Any]]] = None,
    analysis_start_date: Optional[str] = None,
    target_pct: float | Decimal = PRIMARY_COC_TARGET_PCT,
) -> Dict[str, Any]:
    """Compute Year-1 Cash-on-Cash yield using post-debt cash to equity.

    Free Cash Flow Year 1 =
        leveraged_cash_flow
      − asset_management_fee (Year 1)
      − partnership_expenses (Year 1)

    CoC = Free CF Y1 / total_equity_basis

    Args:
        cashflow_by_year: per-year cashflow including ``leveraged_cash_flow``.
        equity_basis: total Day-0 equity outlay used as the CoC denominator.
        fund_by_year: optional fund-waterfall by_year (provides asset_management_fee
            and partnership_expenses). When absent (no fund_assumptions), AM fees
            and partnership expenses are treated as 0.
        cashflow_by_month: optional monthly cashflow used to rebuild analysis-year
            buckets for non-January starts.
        analysis_start_date: analysis start date in ``YYYY-MM`` or ``YYYY-MM-DD``.
        target_pct: CoC hurdle used for the boolean sanity flags.

    Returns:
        Dict with cash_on_cash_year_1, free_cf_year_1, components, and
        target-comparison flags.
    """
    target = dec(target_pct)
    if not cashflow_by_year:
        return {
            "cash_on_cash_year_1": None,
            "cash_on_cash_year_1_exact": None,
            "free_cf_year_1": None,
            "components": {
                "noi_year_1": 0.0,
                "replacement_reserves_year_1": 0.0,
                "capex_year_1": 0.0,
                "debt_service_year_1": 0.0,
                "leveraged_cash_flow_year_1": 0.0,
                "asset_management_fee_year_1": 0.0,
                "partnership_expenses_year_1": 0.0,
                "equity_basis": float(dec(equity_basis)),
            },
            "target_cash_on_cash_pct": float(target),
            "coc_below_target": False,
            "coc_below_target_7pct": False,
            "coc_below_target_6pct": False,
        }

    if (
        cashflow_by_month
        and analysis_start_date
        and int(analysis_start_date[5:7]) != 1
    ):
        analysis_years = aggregate_analysis_years(cashflow_by_month, analysis_start_date)
        year_1 = analysis_years[0] if analysis_years else cashflow_by_year[0]
    else:
        year_1 = cashflow_by_year[0]
    noi_y1 = dec(year_1.get("net_operating_income", 0))
    reserves_y1 = dec(year_1.get("replacement_reserves", 0))
    capex_y1 = dec(year_1.get("total_capex", 0))
    debt_service_y1 = dec(year_1.get("debt_service", 0))
    leveraged_cf = year_1.get("leveraged_cash_flow")
    if leveraged_cf is not None:
        base_cf_y1 = dec(leveraged_cf)
    else:
        # Backward-compatible fallback for callers that still supply only
        # NOI/reserves/CapEx without a levered cashflow field.
        base_cf_y1 = noi_y1 - reserves_y1 - capex_y1 - debt_service_y1

    # AM fees + partnership expenses live on fund_waterfall.by_year (when
    # fund_assumptions present). Both are positive expense values there.
    am_fee_y1 = Decimal("0")
    partnership_y1 = Decimal("0")
    if fund_by_year:
        fy1 = fund_by_year[0] if fund_by_year else {}
        am_fee_y1 = dec(fy1.get("asset_management_fee", 0))
        partnership_y1 = dec(fy1.get("partnership_expenses", 0))

    free_cf_y1 = base_cf_y1 - am_fee_y1 - partnership_y1

    eq = dec(equity_basis)
    if eq > 0:
        coc_y1 = free_cf_y1 / eq
    else:
        coc_y1 = None

    display_coc = round(float(coc_y1), 4) if coc_y1 is not None else None
    exact_coc = float(coc_y1) if coc_y1 is not None else None
    coc_below_target = coc_y1 is not None and coc_y1 < target

    return {
        "cash_on_cash_year_1": display_coc,
        "cash_on_cash_year_1_exact": exact_coc,
        "free_cf_year_1": float(round2(free_cf_y1)),
        "components": {
            "noi_year_1": float(round2(noi_y1)),
            "replacement_reserves_year_1": float(round2(reserves_y1)),
            "capex_year_1": float(round2(capex_y1)),
            "debt_service_year_1": float(round2(debt_service_y1)),
            "leveraged_cash_flow_year_1": float(round2(base_cf_y1)),
            "asset_management_fee_year_1": float(round2(am_fee_y1)),
            "partnership_expenses_year_1": float(round2(partnership_y1)),
            "equity_basis": float(round2(eq)),
        },
        "target_cash_on_cash_pct": float(target),
        "coc_below_target": bool(coc_below_target),
        "coc_below_target_7pct": bool(coc_below_target and target == PRIMARY_COC_TARGET_PCT),
        # Backward-compatible alias preserved for older consumers.
        "coc_below_target_6pct": bool(coc_below_target),
    }


def find_stabilized_year(
    cashflow_by_year: List[Dict[str, Any]],
    capex_by_year: List[Decimal],
    total_basis: Decimal,
    steady_state_vacancy: Decimal,
    *,
    capex_threshold: Decimal = Decimal("0.05"),
    vacancy_tolerance: Decimal = Decimal("0.005"),
    min_year: int = 1,
) -> int:
    """First 1-indexed year where vacancy <= steady_state + tolerance AND
    year-CapEx < capex_threshold * basis. Falls back to 2 if no year
    qualifies by year 3.

    Args:
        min_year: Earliest allowable stabilized year (1-indexed). Pass 2 when
            the analysis starts mid-year so that the partial stub year is
            never selected as the stabilized year.
    """
    start_i = max(0, min_year - 1)
    for i in range(start_i, min(start_i + 3, len(cashflow_by_year))):
        yr = cashflow_by_year[i]
        vacancy = yr.get("vacancy_rate", Decimal("0"))
        if not isinstance(vacancy, Decimal):
            vacancy = Decimal(str(vacancy))
        capex = capex_by_year[i] if i < len(capex_by_year) else Decimal("0")
        if not isinstance(capex, Decimal):
            capex = Decimal(str(capex))
        vacancy_ok = vacancy <= steady_state_vacancy + vacancy_tolerance
        capex_ok = capex < total_basis * capex_threshold
        if vacancy_ok and capex_ok:
            return i + 1
    return max(2, min_year)


def compute_metrics(
    time_grid: TimeGrid,
    cashflow_by_month: List[Dict[str, Any]],
    cashflow_by_year: List[Dict[str, Any]],
    debt_by_month: List[Dict[str, Any]],
    purchase_assumptions: Optional[Dict[str, Any]] = None,
    exit_assumptions: Optional[Dict[str, Any]] = None,
    fund_by_year: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Compute investment performance metrics.

    Args:
        time_grid: Authoritative time grid
        cashflow_by_month: Monthly cashflow waterfall
        cashflow_by_year: Annual cashflow totals
        debt_by_month: Debt service details (for payoff balance)
        purchase_assumptions: Initial investment structure
        exit_assumptions: Exit parameters (cap rate, costs, timing)

    Returns:
        Dict with irr, equity_multiple, dscr, cash_on_cash, exit, yields
    """
    # Handle missing inputs
    if not purchase_assumptions:
        purchase_assumptions = {
            "purchase_price": 0,
            "closing_costs": 0,
            "equity_contribution": 0,
        }

    purchase_price = dec(purchase_assumptions.get("purchase_price", 0))
    closing_costs = dec(purchase_assumptions.get("closing_costs", 0))
    equity_contribution = dec(purchase_assumptions.get("equity_contribution", 0))
    # total_equity_basis = actual Day 0 cash outlay (equity + closing costs +
    # loan fees + upfront CapEx).  Matches RedIQ's CF Calcs Row 108 Day 0.
    total_equity_basis = dec(purchase_assumptions.get(
        "total_equity_basis", float(equity_contribution)
    ))
    total_investment = purchase_price + closing_costs
    # total_unlevered_basis = Day 0 UCF from CF Calcs Row 86, includes upfront CapEx.
    # Falls back to purchase_price + closing_costs when not available.
    total_unlevered_basis = dec(purchase_assumptions.get(
        "total_unlevered_basis", float(total_investment)
    ))

    # Calculate exit values using shared helper
    exit_result = compute_exit_proceeds(
        time_grid=time_grid,
        cashflow_by_year=cashflow_by_year,
        debt_by_month=debt_by_month,
        purchase_assumptions=purchase_assumptions,
        exit_assumptions=exit_assumptions,
        cashflow_by_month=cashflow_by_month,
    )
    net_sale_proceeds = dec(exit_result.get("net_sale_proceeds", 0))
    exit_month_idx = exit_result.get("exit_month_idx", len(time_grid.month_ids) - 1)

    # Calculate DSCR
    dscr_by_year: List[Dict[str, Any]] = []
    total_noi = Decimal("0")
    total_debt_service = Decimal("0")
    min_dscr: Optional[Decimal] = None

    for year_data in cashflow_by_year:
        noi = dec(year_data.get("net_operating_income", 0))
        ds = dec(year_data.get("debt_service", 0))
        total_noi += noi
        total_debt_service += ds

        if ds > 0:
            dscr = noi / ds
            if min_dscr is None or dscr < min_dscr:
                min_dscr = dscr
            dscr_by_year.append({
                "year": year_data["year"],
                "dscr": round2(dscr),
            })
        else:
            dscr_by_year.append({
                "year": year_data["year"],
                "dscr": None,  # No debt service
            })

    avg_dscr = total_noi / total_debt_service if total_debt_service > 0 else None

    # Calculate Cash-on-Cash yield by year.
    # House policy uses actual Day-0 cash outlay as the denominator everywhere:
    # total_equity_basis = equity + closing costs + loan fees + upfront CapEx.
    coc_by_year: List[Dict[str, Any]] = []
    total_lev_cf = Decimal("0")

    for year_data in cashflow_by_year:
        lev_cf = dec(year_data.get("leveraged_cash_flow", 0))
        total_lev_cf += lev_cf

        if total_equity_basis > 0:
            coc_yield = lev_cf / total_equity_basis
            coc_by_year.append({
                "year": year_data["year"],
                "yield": round2(coc_yield),
            })
        else:
            coc_by_year.append({
                "year": year_data["year"],
                "yield": None,
            })

    avg_coc = total_lev_cf / total_equity_basis / Decimal(len(cashflow_by_year)) if total_equity_basis > 0 and cashflow_by_year else None

    # Build cash flow series for IRR calculation
    # Monthly cash flows up to exit + exit proceeds
    unlevered_cf_series: List[float] = []
    levered_cf_series: List[float] = []

    # Initial investment (negative)
    # Unleveraged: total purchase price + closing costs
    # Leveraged: total_equity_basis (actual cash outlay at Day 0)
    unlevered_cf_series.append(-float(total_unlevered_basis))
    levered_cf_series.append(-float(total_equity_basis))

    # Monthly cash flows (limited to exit month)
    # Note: exit proceeds are already injected into cashflow_by_month by the engine
    for idx, cf_month in enumerate(cashflow_by_month):
        if idx > exit_month_idx:
            break

        unlev_cf = float(cf_month.get("unleveraged_cash_flow", 0))
        # Restore debt-funded CapEx for unlevered view (zeroed in engine for LCF)
        unlev_cf -= float(cf_month.get("debt_funded_capex", 0))
        lev_cf = float(cf_month.get("leveraged_cash_flow", 0))

        unlevered_cf_series.append(unlev_cf)
        levered_cf_series.append(lev_cf)

    # Calculate IRR (monthly rate, then annualize)
    monthly_irr_unlev = _calculate_irr(unlevered_cf_series)
    monthly_irr_lev = _calculate_irr(levered_cf_series)

    # Annualize monthly IRR: (1 + monthly)^12 - 1
    annual_irr_unlev = ((1 + monthly_irr_unlev) ** 12 - 1) if monthly_irr_unlev is not None else None
    annual_irr_lev = ((1 + monthly_irr_lev) ** 12 - 1) if monthly_irr_lev is not None else None

    # Calculate Equity Multiple — RedIQ formula:
    # EM = sum(positive annual CFs) / sum(|negative annual CFs|)
    # Uses annual CFs (which include exit proceeds) plus Day 0 outflow.
    # For unlevered EM, subtract debt-funded CapEx (same correction as IRR series).
    dfc_by_year: Dict[str, float] = {}
    for cf_m in cashflow_by_month[:exit_month_idx + 1]:
        dfc = float(cf_m.get("debt_funded_capex", 0))
        if dfc > 0:
            yr = cf_m["month"][:4]
            dfc_by_year[yr] = dfc_by_year.get(yr, 0.0) + dfc
    unlev_annual = [
        float(y.get("unleveraged_cash_flow", 0)) - dfc_by_year.get(y["year"], 0.0)
        for y in cashflow_by_year
    ]
    lev_annual = [float(y.get("leveraged_cash_flow", 0)) for y in cashflow_by_year]

    def _em_rediq(day0: float, annual_cfs: list) -> float | None:
        all_cfs = [day0] + annual_cfs
        pos = sum(cf for cf in all_cfs if cf > 0)
        neg = sum(abs(cf) for cf in all_cfs if cf < 0)
        return round(pos / neg, 2) if neg > 0 else None

    unlev_em = _em_rediq(-float(total_unlevered_basis), unlev_annual)
    lev_em = _em_rediq(-float(total_equity_basis), lev_annual)

    # Calculate yields
    if (
        cashflow_by_month
        and time_grid.month_ids
        and int(time_grid.month_ids[0][5:7]) != 1
    ):
        analysis_years = aggregate_analysis_years(cashflow_by_month, time_grid.month_ids[0])
        year_1_surface = analysis_years[0] if analysis_years else (cashflow_by_year[0] if cashflow_by_year else {})
    else:
        year_1_surface = cashflow_by_year[0] if cashflow_by_year else {}
    year_1_noi = dec(year_1_surface.get("net_operating_income", 0))
    going_in_cap = year_1_noi / purchase_price if purchase_price > 0 else Decimal("0")
    yield_on_cost = year_1_noi / total_investment if total_investment > 0 else Decimal("0")

    # V1.5 policy: Year-1 Cash-on-Cash net of capex, AM fees, partnership
    # expenses. Surfaces alongside the legacy coc_by_year (which is just
    # leveraged_cash_flow / equity_contribution).
    coc_v15 = compute_cash_on_cash(
        cashflow_by_year=cashflow_by_year,
        equity_basis=total_equity_basis,
        fund_by_year=fund_by_year,
        cashflow_by_month=cashflow_by_month,
        analysis_start_date=time_grid.month_ids[0] if time_grid.month_ids else None,
    )

    # Stabilized metrics — pick the first year that meets vacancy + capex thresholds.
    # capex_by_year is derived from cashflow_by_year["total_capex"] (already aggregated).
    # steady_state_vacancy is not available in compute_metrics; default to 5% (industry
    # standard).  The vacancy check in find_stabilized_year defaults missing vacancy_rate
    # to 0, so vacancy_ok is always True here — capex gate drives stabilization detection.
    #
    # Partial-year guard: when the analysis starts mid-year (e.g. Aug), calendar
    # year 1 contains only a stub period (< 12 months) whose annualized NOI would
    # massively understate yield-on-cost.  Force min_year=2 in that case so the
    # stabilized metrics always use the first full calendar year.
    analysis_start_month = int(time_grid.month_ids[0][5:7]) if time_grid.month_ids else 1
    stab_min_year = 2 if analysis_start_month != 1 else 1
    capex_by_year = [
        dec(yr.get("total_capex", Decimal("0"))) for yr in cashflow_by_year
    ]
    stabilized_year = find_stabilized_year(
        cashflow_by_year=cashflow_by_year,
        capex_by_year=capex_by_year,
        total_basis=total_investment if total_investment > 0 else Decimal("0"),
        steady_state_vacancy=Decimal("0.05"),
        min_year=stab_min_year,
    )
    stab_idx = stabilized_year - 1
    if 0 <= stab_idx < len(cashflow_by_year):
        stab_yr = cashflow_by_year[stab_idx]
        stab_noi = dec(stab_yr.get("net_operating_income", Decimal("0")))
        stab_lev_cf = dec(stab_yr.get("leveraged_cash_flow", Decimal("0")))
    else:
        stab_noi = Decimal("0")
        stab_lev_cf = Decimal("0")

    stab_yoc = (
        stab_noi / total_investment
        if total_investment > 0 else None
    )
    stab_coc = (
        round2(stab_lev_cf / total_equity_basis)
        if total_equity_basis > 0 else None
    )

    return {
        "irr": {
            "unlevered_irr": round(annual_irr_unlev, 4) if annual_irr_unlev is not None else None,
            "levered_irr": round(annual_irr_lev, 4) if annual_irr_lev is not None else None,
        },
        "equity_multiple": {
            "unlevered_em": round(unlev_em, 2) if unlev_em is not None else None,
            "levered_em": round(lev_em, 2) if lev_em is not None else None,
        },
        "dscr": {
            "minimum_dscr": round2(min_dscr) if min_dscr is not None else None,
            "average_dscr": round2(avg_dscr) if avg_dscr is not None else None,
            "by_year": dscr_by_year,
        },
        "cash_on_cash": {
            "by_year": coc_by_year,
            "average": round2(avg_coc) if avg_coc is not None else None,
            # V1.5 fields (recommendation rule consumes these):
            "cash_on_cash_year_1": coc_v15["cash_on_cash_year_1"],
            "cash_on_cash_year_1_exact": coc_v15["cash_on_cash_year_1_exact"],
            "year_1": coc_v15["cash_on_cash_year_1"],  # alias for CRM publisher (canonical key)
            "free_cf_year_1": coc_v15["free_cf_year_1"],
            "components": coc_v15["components"],
            "target_cash_on_cash_pct": coc_v15["target_cash_on_cash_pct"],
            "coc_below_target": coc_v15["coc_below_target"],
            "coc_below_target_7pct": coc_v15["coc_below_target_7pct"],
            "coc_below_target_6pct": coc_v15["coc_below_target_6pct"],
            # Stabilized fields (Task 1.2):
            "stabilized": stab_coc,
            "stabilized_year": stabilized_year,
        },
        "coc": {
            # Federation-friendly nested shape per V1.5 plat-agent CRM/judgment
            # extraction map: deal_summary.metrics.coc.cash_on_cash_year_1
            "cash_on_cash_year_1": coc_v15["cash_on_cash_year_1"],
            "cash_on_cash_year_1_exact": coc_v15["cash_on_cash_year_1_exact"],
            "free_cf_year_1": coc_v15["free_cf_year_1"],
            "components": coc_v15["components"],
            "target_cash_on_cash_pct": coc_v15["target_cash_on_cash_pct"],
            "coc_below_target": coc_v15["coc_below_target"],
            "coc_below_target_7pct": coc_v15["coc_below_target_7pct"],
            "coc_below_target_6pct": coc_v15["coc_below_target_6pct"],
        },
        "exit": exit_result,
        "noi": {
            "year_1_noi": float(round2(year_1_noi)),
            "stabilized_year_noi": float(round2(stab_noi)),
            "stabilized_year": stabilized_year,
        },
        "yields": {
            "going_in_cap_rate": round4(going_in_cap),
            "exit_cap_rate": round4(dec(exit_assumptions.get("exit_cap_rate", 0))) if exit_assumptions else 0.0,
            "going_in_yield_on_cost": round4(yield_on_cost),
            "stabilized_yield_on_cost": round4(dec(stab_yoc)) if stab_yoc else stab_yoc,
            "stabilized_year": stabilized_year,
        },
    }
