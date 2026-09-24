"""
Fund-Level Waterfall Module

Computes fund-level cashflows matching RedIQ's CF Calculations rows 113-141:
- Partnership closing costs (acquisition fee + closing costs)
- Annual partnership expenses
- Annual asset management fee (% of equity)
- Cash flow before promote
- Monthly-compounded promote calculation (multi-tier waterfall)
- Sponsor / LP equity splits

The promote uses a monthly-compounded equity balance waterfall matching
RedIQ's Waterfall sheet: a single partnership balance is tracked, compounding
at the preferred return rate monthly.  Cash each month first returns capital
and accrued pref (reducing the balance toward zero), then any residual is
split by the promote tier's LP/GP shares.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional

from engine.modules.time_grid import TimeGrid
from engine.modules.util import calculate_irr, dec, round2


def _compute_growing_equity_am_fees(
    initial_basis: Decimal,
    am_fee_pct: Decimal,
    cashflow_by_year: List[Dict[str, Any]],
) -> List[float]:
    """Compute AM fees with a growing equity basis.

    Each year's cumulative equity = initial basis + sum of negative LCF from
    prior years.  The AM fee is the cumulative equity × fee pct.

    This matches RedIQ's CF Calculations Row 155 (Cumulative Equity Requirement)
    for deals where ongoing equity calls increase the basis.
    """
    cumulative_equity = initial_basis
    schedule = []
    for year_data in cashflow_by_year:
        lcf = dec(year_data.get("leveraged_cash_flow", 0))
        # AM fee based on current cumulative equity
        months = int(year_data.get("months_in_year", 12))
        yr_fee = cumulative_equity * am_fee_pct * Decimal(str(months)) / Decimal("12")
        schedule.append(float(round2(yr_fee)))
        # If LCF is negative, it represents additional equity call
        if lcf < 0:
            cumulative_equity += abs(lcf)
    return schedule


def compute_fund_waterfall(
    time_grid: TimeGrid,
    cashflow_by_year: List[Dict[str, Any]],
    purchase_assumptions: Dict[str, Any],
    exit_assumptions: Dict[str, Any],
    debt_by_month: List[Dict[str, Any]],
    fund_assumptions: Dict[str, Any],
    cashflow_by_month: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Compute fund-level waterfall matching RedIQ CF Calculations rows 113-141.

    Args:
        time_grid: Authoritative time grid
        cashflow_by_year: Annual cashflow totals (for year-level reporting)
        purchase_assumptions: Purchase price, equity, closing costs
        exit_assumptions: Exit cap rate, exit month, sale proceeds
        debt_by_month: Monthly debt data (for loan payoff balance)
        fund_assumptions: Fund structure (equity splits, fees, promote tiers)
        cashflow_by_month: Monthly cashflow data (for monthly promote calc)

    Returns:
        Dict with by_year waterfall, promote details, and summary
    """
    sponsor_pct = dec(fund_assumptions.get("sponsor_equity_pct", 0.05))
    lp_pct = dec(fund_assumptions.get("lp_equity_pct", 0.95))
    gp_coinvest_pct = dec(fund_assumptions.get("gp_coinvest_pct", 0))
    acq_fee_pct = dec(fund_assumptions.get("acquisition_fee_pct", 0))
    am_fee_pct = dec(fund_assumptions.get("asset_management_fee_pct", 0))
    disp_fee_pct = dec(fund_assumptions.get("disposition_fee_pct", 0))
    annual_partnership_exp = dec(fund_assumptions.get("annual_partnership_expenses", 0))
    # Multi-tier waterfall: promote_tiers overrides promote_splits when present
    promote_tiers = fund_assumptions.get("promote_tiers")
    if promote_tiers is None:
        # Backward compatibility: convert legacy promote_splits to promote_tiers
        promote_tiers = fund_assumptions.get("promote_splits", [])
    clawback_enabled = fund_assumptions.get("clawback_enabled", False)

    purchase_price = dec(purchase_assumptions.get("purchase_price", 0))
    equity = dec(purchase_assumptions.get("equity_contribution", 0))
    closing_costs = dec(purchase_assumptions.get("closing_costs", 0))

    # Partnership closing costs (Month 0): acquisition fee + closing costs
    acquisition_fee = purchase_price * acq_fee_pct
    partnership_closing_costs = acquisition_fee + closing_costs

    # Asset management fee: annual % of total equity at risk.
    # RedIQ uses the Day-0 cash outlay (includes upfront CapEx, loan fees, etc.)
    # as the basis, not just purchase equity.
    am_fee_basis = dec(purchase_assumptions.get("total_equity_basis", float(equity)))
    annual_am_fee = am_fee_basis * am_fee_pct
    monthly_am_fee = annual_am_fee / Decimal("12")

    # Per-year AM fee schedule: RedIQ's AM fee basis can change mid-hold
    # (refi, construction draws, exit-year revaluation).  When extracted from
    # CF Calcs Row 120, use those values instead of the flat computation.
    am_fee_schedule = fund_assumptions.get("am_fee_schedule")

    # Growing equity basis: when enabled, negative leveraged cash flow periods
    # increase the equity at risk, causing the AM fee to grow over time.
    # This handles deals like a construction-draw deal where construction draws or negative
    # cash flows increase cumulative equity requirements mid-hold.
    growing_equity_basis = fund_assumptions.get("growing_equity_basis", False)
    if growing_equity_basis and not am_fee_schedule and am_fee_pct > 0:
        am_fee_schedule = _compute_growing_equity_am_fees(
            am_fee_basis, am_fee_pct, cashflow_by_year
        )

    # Monthly partnership expenses
    monthly_partnership_exp = annual_partnership_exp / Decimal("12")

    # Build year-by-year waterfall, and pre-compute monthly AM fees for promote
    by_year: List[Dict[str, Any]] = []
    num_years = len(cashflow_by_year)

    # Map each month to its per-month AM fee (for monthly promote calc)
    month_am_fee_lookup: Dict[str, Decimal] = {}

    for idx, year_data in enumerate(cashflow_by_year):
        year = year_data["year"]
        leveraged_cf = dec(year_data.get("leveraged_cash_flow", 0))

        # Count months in this analysis year — prefer explicit field
        # (set by analysis-year aggregation), fall back to calendar-year count
        if "months_in_year" in year_data:
            months_in_year = int(year_data["months_in_year"])
        else:
            year_months = [m for m in time_grid.month_ids if m.startswith(year)]
            months_in_year = len(year_months)

        # Partnership expenses for this year
        yr_partnership_exp = monthly_partnership_exp * months_in_year

        # AM fee for this year — use schedule if available
        if am_fee_schedule and idx < len(am_fee_schedule):
            yr_am_fee = dec(am_fee_schedule[idx])
        else:
            yr_am_fee = monthly_am_fee * months_in_year

        # Compute per-month AM fee for the monthly promote calculation
        m_am_fee = yr_am_fee / Decimal(str(months_in_year)) if months_in_year > 0 else Decimal("0")
        if "months_in_year" in year_data:
            # Analysis-year based: look up months from time_grid by index
            start_month_idx = sum(
                int(cashflow_by_year[j].get("months_in_year", 12))
                for j in range(idx)
            )
            for offset in range(months_in_year):
                m_idx = start_month_idx + offset
                if m_idx < len(time_grid.month_ids):
                    month_am_fee_lookup[time_grid.month_ids[m_idx]] = m_am_fee
        else:
            for m in time_grid.month_ids:
                if m.startswith(year):
                    month_am_fee_lookup[m] = m_am_fee

        # Disposition fee in exit year (last year)
        yr_disp_fee = Decimal("0")
        if idx == num_years - 1 and disp_fee_pct > 0:
            gross_sale = dec(year_data.get("gross_sale_price", 0))
            yr_disp_fee = gross_sale * disp_fee_pct

        # Cash flow before promote = LCF - partnership expenses - AM fee - disposition fee
        cf_before_promote = leveraged_cf - yr_partnership_exp - yr_am_fee - yr_disp_fee

        by_year.append({
            "year": year,
            "leveraged_cash_flow": round2(leveraged_cf),
            "partnership_expenses": round2(yr_partnership_exp),
            "asset_management_fee": round2(yr_am_fee),
            "disposition_fee": round2(yr_disp_fee),
            "cash_flow_before_promote": round2(cf_before_promote),
            # promote and splits filled in below
            "promote_payment": 0.0,
            "cash_flow_to_partnership": round2(cf_before_promote),
            "sponsor_share": 0.0,
            "lp_share": 0.0,
        })

    # Build per-month smoothed CapEx lookup.
    # RedIQ's waterfall spreads CapEx evenly across months within each
    # analysis year rather than using the actual lumpy schedule.  Without
    # this, a $300K one-time CapEx in Month 1 compounds pref for 11 extra
    # months, producing a materially lower promote than RedIQ.
    month_smoothed_capex: Dict[str, Decimal] = {}
    if cashflow_by_month and cashflow_by_year:
        month_idx_cursor = 0
        for idx, year_data in enumerate(cashflow_by_year):
            n_months = int(year_data.get("months_in_year", 12))
            # Sum actual CapEx in this analysis year's months
            yr_capex = Decimal("0")
            yr_month_ids = []
            for offset in range(n_months):
                m_i = month_idx_cursor + offset
                if m_i < len(cashflow_by_month):
                    yr_capex += dec(cashflow_by_month[m_i].get("total_capex", 0))
                    yr_month_ids.append(cashflow_by_month[m_i]["month"])
            smoothed = yr_capex / Decimal(str(n_months)) if n_months > 0 else Decimal("0")
            for mid in yr_month_ids:
                month_smoothed_capex[mid] = smoothed
            month_idx_cursor += n_months

    # Build monthly CF Before Promote series for the promote engine
    monthly_cf_before_promote: List[Dict[str, Any]] = []
    if cashflow_by_month:
        for i, m_data in enumerate(cashflow_by_month):
            lcf = dec(m_data.get("leveraged_cash_flow", 0))
            month = m_data["month"]
            # Smooth CapEx: add back actual capex, subtract evenly-spread capex
            actual_capex = dec(m_data.get("total_capex", 0))
            smoothed_capex = month_smoothed_capex.get(month, actual_capex)
            lcf_smoothed = lcf + actual_capex - smoothed_capex
            # Use per-month AM fee (schedule-aware) or fallback to flat
            m_am = month_am_fee_lookup.get(month, monthly_am_fee)
            # Deduct monthly partnership expenses and AM fee
            m_cf_before = lcf_smoothed - monthly_partnership_exp - m_am
            # Disposition fee in the last month
            if i == len(cashflow_by_month) - 1 and disp_fee_pct > 0:
                gross_sale = dec(m_data.get("gross_sale_price", 0))
                m_cf_before -= gross_sale * disp_fee_pct
            monthly_cf_before_promote.append({
                "month": month,
                "cf_before_promote": m_cf_before,
            })

    # Initial balance for the waterfall = total Day-0 cash outlay
    # total_equity_basis = |Day 0 LCF| (includes equity, closing costs, upfront CapEx, loan fees)
    # acquisition_fee is an additional fee on top
    # partnership_closing_costs_day0 = fund-level costs (legal, org) from CF Calcs row 113
    pcc = dec(fund_assumptions.get("partnership_closing_costs", 0))
    initial_balance = am_fee_basis + acquisition_fee + pcc

    # Promote calculation using monthly-compounded equity balance waterfall
    promote_result = _compute_promote(
        monthly_cf=monthly_cf_before_promote,
        by_year=by_year,
        initial_balance=initial_balance,
        promote_splits=promote_tiers,
        sponsor_pct=sponsor_pct,
        lp_pct=lp_pct,
        clawback_enabled=clawback_enabled,
    )

    # Apply per-year promote and splits from tier calculation
    promote_by_year = promote_result.get("by_year", {})
    total_promote = Decimal("0")

    # GP co-invest distributions: co-invest capital earns pari passu with LP.
    # After promote deduction, partnership CF splits:
    #   - LP share = (lp_pct - gp_coinvest_pct) of CF to partnership
    #   - GP co-invest share = gp_coinvest_pct of CF to partnership
    #   - GP promote share = sponsor_pct of CF to partnership
    #   - Total sponsor = GP promote share + GP co-invest share + promote payment
    effective_lp_pct = lp_pct - gp_coinvest_pct  # LP's true equity share
    effective_gp_equity_pct = sponsor_pct + gp_coinvest_pct  # GP's total equity share

    for yr_data in by_year:
        year = yr_data["year"]
        yr_promote_info = promote_by_year.get(year, {})
        yr_promote = dec(yr_promote_info.get("promote", 0))
        total_promote += yr_promote

        cf_before = dec(yr_data["cash_flow_before_promote"])
        cf_to_partnership = cf_before - yr_promote
        sponsor_share = cf_to_partnership * effective_gp_equity_pct
        lp_share_val = cf_to_partnership * effective_lp_pct

        yr_data["promote_payment"] = round2(yr_promote)
        yr_data["cash_flow_to_partnership"] = round2(cf_to_partnership)
        yr_data["sponsor_share"] = round2(sponsor_share)
        yr_data["lp_share"] = round2(lp_share_val)
        if gp_coinvest_pct > 0:
            yr_data["gp_coinvest_share"] = round2(cf_to_partnership * gp_coinvest_pct)

    # Summary
    total_partnership_exp = sum(dec(y["partnership_expenses"]) for y in by_year)
    total_am_fee = sum(dec(y["asset_management_fee"]) for y in by_year)
    total_disp_fee = sum(dec(y["disposition_fee"]) for y in by_year)
    total_sponsor = sum(dec(y["sponsor_share"]) for y in by_year)
    total_lp = sum(dec(y["lp_share"]) for y in by_year)

    # Net IRR and Return Multiple (Partnership-level, after fees/promote)
    # RedIQ computes these from monthly "Cash Flow to Equity Partner" series.
    # Partnership IRR (C130) and Equity Partner IRR (C141) use monthly CFs.
    monthly_promotes = promote_result.get("monthly_promotes", {})
    partnership_irr = None
    partnership_em = None
    total_cf_to_partnership = sum(dec(y["cash_flow_to_partnership"]) for y in by_year)

    # Build monthly CF to Partnership = CF before promote - promote
    if monthly_cf_before_promote:
        monthly_partnership_cfs = [-float(initial_balance)]
        for m_data in monthly_cf_before_promote:
            m = m_data["month"]
            cf_before = float(m_data["cf_before_promote"])
            m_promote = float(monthly_promotes.get(m, Decimal("0")))
            monthly_partnership_cfs.append(cf_before - m_promote)

        monthly_irr = calculate_irr(monthly_partnership_cfs)
        # Annualize: (1 + monthly)^12 - 1
        if monthly_irr is not None:
            partnership_irr = (1 + monthly_irr) ** 12 - 1

    # Partnership equity multiple — RedIQ formula:
    # EM = sum(positive CFs) / sum(|negative CFs|) across the full series
    # including Day 0.  This matches RedIQ's Input sheet "Part'ship Eq. Mult."
    if monthly_cf_before_promote and initial_balance > 0:
        all_cfs = monthly_partnership_cfs  # Includes Day 0 as first element
        pos = sum(cf for cf in all_cfs if cf > 0)
        neg = sum(abs(cf) for cf in all_cfs if cf < 0)
        partnership_em = pos / neg if neg > 0 else None
    elif initial_balance > 0:
        # Fallback: use annual series
        annual_cfs = [-float(initial_balance)] + [float(y["cash_flow_to_partnership"]) for y in by_year]
        pos = sum(cf for cf in annual_cfs if cf > 0)
        neg = sum(abs(cf) for cf in annual_cfs if cf < 0)
        partnership_em = pos / neg if neg > 0 else None

    result = {
        "by_year": by_year,
        "closing_costs": {
            "acquisition_fee": round2(acquisition_fee),
            "partnership_closing_costs": round2(partnership_closing_costs),
        },
        "promote": promote_result,
        "summary": {
            "total_partnership_expenses": round2(total_partnership_exp),
            "total_asset_management_fee": round2(total_am_fee),
            "total_disposition_fee": round2(total_disp_fee),
            "total_promote": round2(total_promote),
            "total_sponsor_distributions": round2(total_sponsor),
            "total_lp_distributions": round2(total_lp),
            "annual_am_fee": round2(annual_am_fee),
            "annual_partnership_expenses": round2(annual_partnership_exp),
            "partnership_irr": round(partnership_irr, 4) if partnership_irr is not None else None,
            "partnership_equity_multiple": round(partnership_em, 4) if partnership_em is not None else None,
            **({"gp_coinvest_pct": round2(gp_coinvest_pct),
                "total_gp_coinvest_distributions": round2(
                    sum(dec(y.get("gp_coinvest_share", 0)) for y in by_year)
                )} if gp_coinvest_pct > 0 else {}),
        },
    }

    # JV mode: compute per-partner distributions and returns
    partners = fund_assumptions.get("partners")
    if partners:
        result["partners"] = _compute_jv_partners(
            partners=partners,
            by_year=by_year,
            initial_balance=initial_balance,
            monthly_cf_before_promote=monthly_cf_before_promote,
            monthly_promotes=monthly_promotes,
        )

    return result


def _compute_jv_partners(
    partners: List[Dict[str, Any]],
    by_year: List[Dict[str, Any]],
    initial_balance: Decimal,
    monthly_cf_before_promote: List[Dict[str, Any]],
    monthly_promotes: Dict[str, Decimal],
) -> List[Dict[str, Any]]:
    """Compute per-partner distributions and returns for JV structures.

    Each partner's equity share determines their pro-rata portion of
    the partnership cashflows. Their IRR and EM are computed from
    their individual equity investment and distribution stream.

    Args:
        partners: List of partner dicts from fund_assumptions
        by_year: Annual waterfall data (already computed)
        initial_balance: Total partnership initial balance
        monthly_cf_before_promote: Monthly CF series

    Returns:
        List of partner result dicts with distributions, IRR, EM
    """
    partner_results = []
    total_gp_equity_pct = sum(
        dec(partner.get("equity_pct", 0))
        for partner in partners
        if partner.get("role", "lp") == "gp"
    )

    for partner in partners:
        name = partner["name"]
        equity_pct = dec(partner["equity_pct"])
        role = partner.get("role", "lp")

        # Partner's share of initial equity
        partner_equity = initial_balance * equity_pct

        promote_allocation_pct = (
            equity_pct / total_gp_equity_pct
            if role == "gp" and total_gp_equity_pct > 0
            else Decimal("0")
        )

        # Partner's share of annual distributions.  GP partners receive their
        # equity share of post-promote partnership cash flow plus their promote
        # allocation; LP partners receive only their equity share after promote.
        partner_by_year = []
        total_distributions = Decimal("0")
        for yr in by_year:
            cf_to_partnership = dec(yr.get("cash_flow_to_partnership", 0))
            promote = dec(yr.get("promote_payment", 0))
            partner_cf = cf_to_partnership * equity_pct + promote * promote_allocation_pct
            total_distributions += partner_cf
            partner_by_year.append({
                "year": yr["year"],
                "distribution": round2(partner_cf),
            })

        # Partner IRR/EM from the same post-promote distribution economics as
        # the annual partner rows.  The old pro-rata cf_before_promote series
        # overstated LP returns and stripped promote from GP returns.
        partner_irr = None
        partner_em = None
        if monthly_cf_before_promote and partner_equity > 0:
            partner_monthly_cfs = [-float(partner_equity)]
            for m_data in monthly_cf_before_promote:
                month = m_data["month"]
                cf_before = dec(m_data["cf_before_promote"])
                promote = dec(monthly_promotes.get(month, 0))
                cf_to_partnership = cf_before - promote
                partner_cf = cf_to_partnership * equity_pct + promote * promote_allocation_pct
                partner_monthly_cfs.append(float(partner_cf))

            monthly_irr = calculate_irr(partner_monthly_cfs)
            if monthly_irr is not None:
                partner_irr = (1 + monthly_irr) ** 12 - 1

            # Partner EM
            pos = sum(cf for cf in partner_monthly_cfs if cf > 0)
            neg = sum(abs(cf) for cf in partner_monthly_cfs if cf < 0)
            partner_em = pos / neg if neg > 0 else None

        partner_results.append({
            "name": name,
            "role": role,
            "equity_pct": round2(equity_pct),
            "equity_invested": round2(partner_equity),
            "total_distributions": round2(total_distributions),
            "irr": round(partner_irr, 4) if partner_irr is not None else None,
            "equity_multiple": round(partner_em, 4) if partner_em is not None else None,
            "by_year": partner_by_year,
        })

    return partner_results


def _compute_promote(
    monthly_cf: List[Dict[str, Any]],
    by_year: List[Dict[str, Any]],
    initial_balance: Decimal,
    promote_splits: List[Dict[str, Any]],
    sponsor_pct: Decimal,
    lp_pct: Decimal,
    clawback_enabled: bool = False,
) -> Dict[str, Any]:
    """Compute promote via monthly-compounded multi-tier equity balance waterfall.

    Supports N tiers with sequential cash flow distribution:
    1. Pref tiers (gp_share=0): accrue preferred return on balance, all CF
       returns capital + pref to LP until balance satisfied.
    2. Catch-up tiers (catch_up=True): GP receives 100% (or tier gp_share)
       until cumulative GP distributions reach catch_up_target_pct of total
       profit (distributions minus returned capital).
    3. Promote tiers (gp_share>0): residual CF splits by LP/GP shares.
       Multiple promote tiers flow sequentially — first promote tier handles
       operating CF; higher tiers (by hurdle_irr) activate at exit when
       cumulative deal IRR exceeds the hurdle.

    Backward compatibility: legacy 2-tier promote_splits (pref + single
    promote) produce identical results — the new code handles them as a
    special case of the general N-tier algorithm.

    Clawback: when enabled, checks at exit whether GP cumulative distributions
    exceed what the final tier structure entitles. Reports clawback_amount.
    """
    if not promote_splits:
        return {"total_promote": 0.0, "by_year": {}, "tiers": [], "monthly_promotes": {}}

    if not monthly_cf:
        return {"total_promote": 0.0, "by_year": {}, "tiers": [], "monthly_promotes": {}}

    # Classify tiers
    pref_tiers = []       # gp_share == 0, provides pref accrual rate
    catch_up_tiers = []   # catch_up == True
    promote_tier_list = []  # gp_share > 0, no catch_up

    for tier in promote_splits:
        gp = dec(tier.get("gp_share", 0))
        is_catch_up = tier.get("catch_up", False)
        if is_catch_up:
            catch_up_tiers.append(tier)
        elif gp == 0:
            pref_tiers.append(tier)
        else:
            promote_tier_list.append(tier)

    # Sort promote tiers by hurdle_irr ascending (lowest hurdle first)
    promote_tier_list.sort(key=lambda t: dec(t.get("hurdle_irr", 0)))

    # Find the preferred return rate (highest hurdle from pref tiers)
    pref_rate = Decimal("0")
    for tier in pref_tiers:
        h = dec(tier.get("hurdle_irr", 0))
        if h > pref_rate:
            pref_rate = h
    # Also check catch-up tiers for pref rate (catch-up hurdle often equals pref)
    for tier in catch_up_tiers:
        h = dec(tier.get("hurdle_irr", 0))
        if h > pref_rate:
            pref_rate = h

    monthly_pref_rate = pref_rate / Decimal("12")

    # Single unified partnership balance (negative = unreturned capital)
    balance = -initial_balance

    # Track cumulative distributions for catch-up computation
    cumulative_gp = Decimal("0")
    cumulative_capital_returned = Decimal("0")
    cumulative_profit_distributed = Decimal("0")
    total_investor_distributions = Decimal("0")  # all CF distributed (capital + pref + profit)

    # Tier-by-tier tracking
    tier_totals: Dict[str, Dict[str, Decimal]] = {}
    for tier in promote_splits:
        name = tier.get("tier", "unnamed")
        tier_totals[name] = {"to_lp": Decimal("0"), "to_gp": Decimal("0")}

    # Track monthly promotes
    monthly_promotes: Dict[str, Decimal] = {}
    total_promote = Decimal("0")

    # Build IRR series for lookback (used for multi-tier promote at exit)
    irr_series = [-float(initial_balance)]

    for m_data in monthly_cf:
        month = m_data["month"]
        cf = m_data["cf_before_promote"]

        # Accrue preferred return (makes balance more negative)
        if balance < 0 and monthly_pref_rate > 0:
            balance += balance * monthly_pref_rate  # balance * rate is negative

        month_promote = Decimal("0")
        month_cf_distributed = Decimal("0")

        if cf < 0:
            # Negative CF increases unreturned capital (balance more negative)
            balance += cf
            irr_series.append(float(cf))
        elif cf > 0:
            remaining = cf

            # Phase 1: Return capital + accrued pref (reduce balance toward 0)
            if balance < 0 and remaining > 0:
                capital_return = min(remaining, -balance)
                balance += capital_return
                remaining -= capital_return
                cumulative_capital_returned += capital_return
                total_investor_distributions += capital_return
                month_cf_distributed += capital_return
                # Attribute to first pref tier
                if pref_tiers:
                    name = pref_tiers[0].get("tier", "pref")
                    tier_totals.setdefault(name, {"to_lp": Decimal("0"), "to_gp": Decimal("0")})
                    tier_totals[name]["to_lp"] += capital_return

            # Phase 2: Catch-up tiers (GP gets disproportionate share)
            for cu_tier in catch_up_tiers:
                if remaining <= 0:
                    break
                cu_target_pct = dec(cu_tier.get("catch_up_target_pct", Decimal("0.20")))
                cu_gp_share = dec(cu_tier.get("gp_share", Decimal("1.0")))
                cu_name = cu_tier.get("tier", "catch_up")

                # Profit = total distributions - initial capital invested
                # This includes pref return above capital (the key fix: pref
                # return IS profit from the GP catch-up perspective)
                total_profit_so_far = total_investor_distributions + cumulative_gp - initial_balance
                # Target: GP should have cu_target_pct of total profit
                # Catch-up continues until: cumulative_gp >= cu_target_pct * (total_profit_so_far + remaining allocated to catch-up)
                # Solve: gp_needed = cu_target_pct * (total_profit + gp_needed / cu_gp_share) - cumulative_gp
                # For cu_gp_share = 1.0: gp_needed = cu_target_pct * total_profit / (1 - cu_target_pct) - cumulative_gp
                if cu_gp_share > 0:
                    if cu_gp_share >= Decimal("1"):
                        # GP gets 100% until caught up
                        # Total profit including catch-up: P + catchup
                        # GP target: cu_target_pct * (P + catchup)
                        # At catch-up completion: cumulative_gp + catchup = cu_target_pct * (P + catchup)
                        # catchup * (1 - cu_target_pct) = cu_target_pct * P - cumulative_gp
                        denominator = Decimal("1") - cu_target_pct
                        if denominator > 0:
                            catch_up_needed = (cu_target_pct * total_profit_so_far - cumulative_gp) / denominator
                        else:
                            catch_up_needed = remaining
                    else:
                        # Partial catch-up: GP gets cu_gp_share until target
                        catch_up_needed = (cu_target_pct * total_profit_so_far - cumulative_gp)
                        if cu_gp_share > 0:
                            catch_up_needed = catch_up_needed / cu_gp_share

                    catch_up_needed = max(Decimal("0"), catch_up_needed)
                    catch_up_amount = min(remaining, catch_up_needed)

                    if catch_up_amount > 0:
                        gp_gets = catch_up_amount * cu_gp_share
                        lp_gets = catch_up_amount * (Decimal("1") - cu_gp_share)
                        month_promote += gp_gets
                        cumulative_gp += gp_gets
                        cumulative_profit_distributed += catch_up_amount
                        total_investor_distributions += lp_gets
                        month_cf_distributed += catch_up_amount
                        remaining -= catch_up_amount
                        tier_totals.setdefault(cu_name, {"to_lp": Decimal("0"), "to_gp": Decimal("0")})
                        tier_totals[cu_name]["to_gp"] += gp_gets
                        tier_totals[cu_name]["to_lp"] += lp_gets

            # Phase 3: Promote tiers (split residual by LP/GP shares)
            # During hold: use first promote tier (lowest hurdle)
            # At exit (last month): compute IRR lookback for higher tiers
            is_exit_month = (m_data is monthly_cf[-1])

            if remaining > 0 and promote_tier_list:
                if is_exit_month and len(promote_tier_list) > 1:
                    # IRR lookback: test each tier using the LP's actual net
                    # exit cash flow after that tier's promote split.  Using
                    # the gross residual would give the LP credit for GP
                    # promote and can activate a tier the LP hurdle did not hit.
                    active_tier = promote_tier_list[0]  # default to lowest
                    for pt in promote_tier_list:
                        pt_gp_share = dec(pt.get("gp_share", 0))
                        pt_lp_share = dec(pt.get("lp_share", Decimal("1") - pt_gp_share))
                        lp_exit_cf = month_cf_distributed + remaining * pt_lp_share
                        test_series = irr_series + [float(lp_exit_cf)]
                        deal_irr = calculate_irr(test_series)
                        annual_irr = ((1 + deal_irr) ** 12 - 1) if deal_irr is not None else 0.0
                        hurdle = float(pt.get("hurdle_irr", 0))
                        if annual_irr >= hurdle:
                            active_tier = pt
                        else:
                            break
                else:
                    # During hold: use first (lowest hurdle) promote tier
                    active_tier = promote_tier_list[0]

                gp_share = dec(active_tier.get("gp_share", 0))
                lp_share_tier = dec(active_tier.get("lp_share", Decimal("1") - gp_share))
                pt_name = active_tier.get("tier", "promote")

                if gp_share > 0:
                    gp_gets = remaining * gp_share
                    lp_gets = remaining * lp_share_tier
                    month_promote += gp_gets
                    cumulative_gp += gp_gets
                    cumulative_profit_distributed += remaining
                    total_investor_distributions += lp_gets
                    month_cf_distributed += remaining
                    tier_totals.setdefault(pt_name, {"to_lp": Decimal("0"), "to_gp": Decimal("0")})
                    tier_totals[pt_name]["to_gp"] += gp_gets
                    tier_totals[pt_name]["to_lp"] += lp_gets

            irr_series.append(float(month_cf_distributed - month_promote))
        else:
            irr_series.append(0.0)

        total_promote += month_promote
        monthly_promotes[month] = month_promote

    # Clawback computation: at exit, check if GP received more than entitled
    clawback_amount = Decimal("0")
    if clawback_enabled and promote_tier_list and cumulative_gp > 0:
        # Compute final deal IRR
        final_irr_raw = calculate_irr(irr_series)
        final_annual_irr = ((1 + final_irr_raw) ** 12 - 1) if final_irr_raw is not None else 0.0
        # Find the tier that should apply based on final IRR
        entitled_tier = promote_tier_list[0]
        for pt in promote_tier_list:
            hurdle = float(pt.get("hurdle_irr", 0))
            if final_annual_irr >= hurdle:
                entitled_tier = pt
            else:
                break
        # GP should have received entitled_gp_share of total profit
        entitled_gp_share = dec(entitled_tier.get("gp_share", 0))
        entitled_gp_amount = cumulative_profit_distributed * entitled_gp_share
        # Add catch-up entitlement
        for cu_tier in catch_up_tiers:
            cu_target = dec(cu_tier.get("catch_up_target_pct", 0))
            entitled_gp_amount = max(entitled_gp_amount, cumulative_profit_distributed * cu_target)
        if cumulative_gp > entitled_gp_amount:
            clawback_amount = cumulative_gp - entitled_gp_amount

    # Aggregate monthly promotes into analysis years
    promote_by_year: Dict[str, Dict[str, Any]] = {}
    month_idx = 0
    for yr_data in by_year:
        year = yr_data["year"]
        n_months = int(yr_data.get("months_in_year", 12))
        yr_promote = Decimal("0")
        for _ in range(n_months):
            if month_idx < len(monthly_cf):
                m = monthly_cf[month_idx]["month"]
                yr_promote += monthly_promotes.get(m, Decimal("0"))
                month_idx += 1
        promote_by_year[year] = {
            "promote": round2(yr_promote),
        }

    # Build portfolio IRR for reference
    annual_cfs = [-float(initial_balance)]
    for yr in by_year:
        annual_cfs.append(float(yr["cash_flow_before_promote"]))
    portfolio_irr = calculate_irr(annual_cfs)

    # Build tier breakdown for reporting
    by_tier = []
    for tier in promote_splits:
        name = tier.get("tier", "unnamed")
        totals = tier_totals.get(name, {"to_lp": Decimal("0"), "to_gp": Decimal("0")})
        by_tier.append({
            "tier": name,
            "hurdle_irr": tier.get("hurdle_irr", 0),
            "lp_share_pct": tier.get("lp_share", 0),
            "gp_share_pct": tier.get("gp_share", 0),
            "catch_up": tier.get("catch_up", False),
            "total_to_lp": round2(totals["to_lp"]),
            "total_to_gp": round2(totals["to_gp"]),
            "total_distributed": round2(totals["to_lp"] + totals["to_gp"]),
        })

    # Serialize monthly_promotes as Dict[str, float] for JSON-safety.
    # Internally these are Decimal for precision; the JSON encoder elsewhere
    # falls back to `default=str` which would emit "0" instead of 0.0 and
    # break downstream numeric guards (e.g. rediq_output.py:498
    # `if promote and promote != 0` admits the truthy string "0", then
    # `abs(promote)` raises TypeError). Converting here keeps the public
    # contract numeric.
    monthly_promotes_serialized = {
        m: round2(v) for m, v in monthly_promotes.items()
    }

    result = {
        "total_promote": round2(total_promote),
        "portfolio_irr": round(portfolio_irr, 4) if portfolio_irr is not None else None,
        "by_year": promote_by_year,
        "by_tier": by_tier,
        "tier_history": [],
        "monthly_promotes": monthly_promotes_serialized,
    }

    if clawback_enabled:
        result["clawback_amount"] = round2(clawback_amount)

    return result
