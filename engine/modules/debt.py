"""
Debt Service Module

Calculates monthly debt service including:
- Loan draw schedule (progressive funding)
- Interest-only (I/O) period
- Amortizing principal + interest (P+I)
- Outstanding balance tracking

See: docs/modules/debt_spec.md for full specification
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional

from engine.modules.time_grid import TimeGrid
from engine.modules.util import dec, month_id, round2


@dataclass(frozen=True)
class DebtMonthResult:
    """Intermediate result for debt service in a single month."""

    month: str
    beginning_balance: Decimal
    draw_amount: Decimal
    interest_expense: Decimal
    principal_payment: Decimal
    debt_service: Decimal
    ending_balance: Decimal
    is_io_period: bool
    loan_payoff: Decimal = Decimal("0")


def _get_effective_rate(
    month: str,
    rate_type: str,
    base_annual_rate: Decimal,
    rate_curve_lookup: Dict[str, Decimal],
    rate_curve_sorted: List[Dict[str, Any]],
    base_spread: Decimal,
    rate_cap: Optional[Decimal],
    rate_floor: Optional[Decimal],
    rate_cap_expiry_month: Optional[str] = None,
    rate_cap_renewal_rate: Optional[Decimal] = None,
) -> Decimal:
    """Get the effective annual rate for a given month.

    For fixed-rate loans, returns the base rate.
    For variable-rate loans, finds the applicable rate from the curve.
    Rate cap expiry: after expiry_month, cap is removed (or replaced by renewal cap).
    """
    if rate_type != "variable" or not rate_curve_sorted:
        return base_annual_rate

    # Find the most recent curve entry at or before this month
    applicable_rate = dec(rate_curve_sorted[0]["rate"])
    for entry in rate_curve_sorted:
        entry_month = month_id(entry["start_month"])
        if entry_month <= month:
            applicable_rate = dec(entry["rate"])
        else:
            break

    effective = applicable_rate + base_spread

    # Determine active cap for this month
    active_cap = rate_cap
    if rate_cap_expiry_month and month > rate_cap_expiry_month:
        # Cap has expired — use renewal cap or no cap
        active_cap = rate_cap_renewal_rate  # None means no cap

    if active_cap is not None:
        effective = min(effective, active_cap)
    if rate_floor is not None:
        effective = max(effective, rate_floor)
    return effective


def compute_prepayment_penalty(
    outstanding_balance: Decimal,
    prepayment_type: str,
    remaining_debt_service: List[Decimal],
    treasury_rate: Decimal,
    penalty_pct: Decimal = Decimal("0"),
) -> Decimal:
    """Compute prepayment penalty for early loan payoff.

    Args:
        outstanding_balance: Loan balance at time of prepayment
        prepayment_type: "none", "defeasance", "yield_maintenance", "percentage"
        remaining_debt_service: List of remaining monthly DS payments
        treasury_rate: Monthly treasury rate for PV calculations
        penalty_pct: For percentage type, penalty as fraction of balance

    Returns:
        Dollar penalty amount
    """
    if prepayment_type == "none" or not prepayment_type:
        return Decimal("0")

    if prepayment_type == "percentage":
        return outstanding_balance * penalty_pct

    if prepayment_type == "defeasance":
        # Defeasance cost = PV(remaining debt service at Treasury) - outstanding balance
        # The borrower must purchase a portfolio of Treasury securities that replicate
        # the remaining payment stream.
        if not remaining_debt_service or treasury_rate <= 0:
            return Decimal("0")
        monthly_treasury = treasury_rate / Decimal("12")
        pv = Decimal("0")
        for i, payment in enumerate(remaining_debt_service):
            pv += payment / (Decimal("1") + monthly_treasury) ** (i + 1)
        penalty = max(pv - outstanding_balance, Decimal("0"))
        return penalty

    if prepayment_type == "yield_maintenance":
        # Yield maintenance = PV(remaining payments at Treasury) - balance
        # Similar to defeasance but the premium goes to the lender
        if not remaining_debt_service or treasury_rate <= 0:
            return Decimal("0")
        monthly_treasury = treasury_rate / Decimal("12")
        pv = Decimal("0")
        for i, payment in enumerate(remaining_debt_service):
            pv += payment / (Decimal("1") + monthly_treasury) ** (i + 1)
        penalty = max(pv - outstanding_balance, Decimal("0"))
        return penalty

    return Decimal("0")


def _calculate_amortizing_payment(
    principal: Decimal,
    monthly_rate: Decimal,
    amort_months: int,
) -> Decimal:
    """
    Calculate monthly amortizing payment using standard mortgage formula.

    P * r(1+r)^n / ((1+r)^n - 1)

    Args:
        principal: Outstanding balance at start of amortization
        monthly_rate: Monthly interest rate (annual / 12)
        amort_months: Number of amortization periods (months)

    Returns:
        Monthly payment amount (principal + interest)
    """
    if monthly_rate == Decimal("0"):
        # Zero interest: simple principal division
        return principal / Decimal(amort_months)

    r = monthly_rate
    n = amort_months
    # (1 + r)^n
    one_plus_r_n = (Decimal("1") + r) ** n
    # P * r * (1+r)^n / ((1+r)^n - 1)
    payment = principal * r * one_plus_r_n / (one_plus_r_n - Decimal("1"))
    return payment


def _annual_debt_constant(annual_rate: Decimal, amort_years: int) -> Decimal:
    """Compute annual debt constant: 12 × monthly payment per $1 of principal.

    The debt constant converts NOI → max loan amount via: loan = NOI / (DSCR × constant).
    """
    monthly_rate = annual_rate / Decimal("12")
    amort_months = amort_years * 12
    if monthly_rate == Decimal("0"):
        return Decimal("12") / Decimal(str(amort_months))
    r = monthly_rate
    n = amort_months
    one_plus_r_n = (Decimal("1") + r) ** n
    monthly_constant = r * one_plus_r_n / (one_plus_r_n - Decimal("1"))
    return monthly_constant * Decimal("12")


# =====================================================================
# Agency (Fannie/Freddie) leverage with DSCR-constrained sizing  (V1.5)
# =====================================================================
#
# Macro context (per analyst, as of 2026-05-07):
#   5-year Treasury benchmark .................. 4.00%   (override-able)
#   Agency spread (Fannie permanent, stabilized) 150 bps
#   All-in rate ................................ 5.50%
#   Amortization ............................... 30 yr (Fannie standard)
#   Maturity ................................... 7  yr (Fannie standard)
#   Max LTV (stabilized, agency) ............... 65%
#   Min DSCR (analyst floor at lifetime max P+I) 1.25x
#
# Vintage adjustment:
#   The user described a "decade-of-year-built" rule of thumb where older
#   vintages get a stricter required DSCR (capex risk). A grep of
#   `engine/`, `runs/`, `docs/` (2026-05-07) found NO existing implementation
#   of such a rule in the mfu codebase. The closest references are in
#   `engine/portfolio.py` (vintage_year aggregation only — no DSCR coupling)
#   and `engine/feasibility.py` (flat MIN_DSCR_HARD_FLOOR=1.10, _WARN_120,
#   _WARN_130). No decade → DSCR mapping exists.
#
#   Per V1.5 instructions: do NOT fabricate. We use the analyst's flat
#   1.25x DSCR floor and document a vintage I/O bump (older = more I/O
#   for capex window) as the only vintage-driven behavior here. The
#   `vintage_dscr_constrained_amount` sizing path is computed but tied
#   to the same 1.25x floor; if/when a real decade rule lands in the
#   codebase, swap `_vintage_required_dscr()` and the third sizing path
#   will activate without changes elsewhere.

BENCHMARK_5YR_TREASURY: Decimal = Decimal("0.04")   # 4.00% (analyst override-able)
AGENCY_SPREAD: Decimal = Decimal("0.0150")          # 150 bps
AGENCY_ALL_IN_RATE: Decimal = BENCHMARK_5YR_TREASURY + AGENCY_SPREAD  # 5.50%
AGENCY_AMORT_YEARS: int = 30
AGENCY_TERM_YEARS: int = 7
AGENCY_MAX_LTV: Decimal = Decimal("0.65")
AGENCY_MIN_DSCR: Decimal = Decimal("1.25")
AGENCY_DEFAULT_IO_MONTHS: int = 24    # 2-year I/O default
AGENCY_OLDER_IO_MONTHS: int = 36      # 3-year I/O for vintage <= 1995


def _vintage_required_dscr(vintage: Optional[int]) -> Decimal:
    """Return the DSCR floor adjusted for property vintage.

    The codebase has no documented decade-based DSCR rule (see comment
    block above). To avoid fabricating a policy, this returns the flat
    analyst-specified 1.25x floor for all vintages. The seam is preserved
    so a real decade rule can be plugged in here later (e.g., 1.30 for
    pre-1980, 1.25 for 1980-2000, 1.20 for post-2000).
    """
    return AGENCY_MIN_DSCR


def compute_agency_loan_terms(
    vintage: Optional[int],
    t12_noi: float | Decimal,
    projected_noi: float | Decimal,
    *,
    purchase_price: Optional[float | Decimal] = None,
    benchmark_5yr_treasury: Optional[Decimal] = None,
    agency_spread: Optional[Decimal] = None,
) -> Dict[str, Any]:
    """Size an agency (Fannie permanent) loan under DSCR + LTV constraints.

    Computes the binding constraint between three sizing rules:

    1. **LTV-constrained:** ``loan = AGENCY_MAX_LTV (65%) × purchase_price``
       (skipped when ``purchase_price`` is None).
    2. **DSCR-constrained:** ``loan = NOI / (1.25 × annual debt constant)``
       where the debt constant is the lifetime amortizing payment factor at
       the all-in rate over a 30-year amort. NOI used = the more
       conservative of T12 vs projected.
    3. **Vintage-DSCR-constrained:** identical to (2) but using the vintage-
       adjusted required DSCR from :func:`_vintage_required_dscr`. With the
       current flat-1.25 implementation this collapses onto rule (2);
       wired in for forward compatibility (see module docstring).

    Older vintages (<=1995) get 3-year I/O to widen the capex window;
    others get 2-year I/O. This is the only vintage-coupled behavior
    until a real decade-DSCR rule lands.

    Args:
        vintage: ``year_built``. ``None`` keeps default I/O and skips the
            vintage rule (effectively rule 1 vs rule 2 only).
        t12_noi: Trailing-12 NOI (annualized).
        projected_noi: Year-1 underwritten NOI.
        purchase_price: Optional purchase price; required to evaluate the
            LTV constraint.
        benchmark_5yr_treasury: Override the hardcoded 4.0% benchmark.
        agency_spread: Override the hardcoded 150bps spread.

    Returns:
        Dict matching the ``debt_terms`` shape consumed by
        :func:`compute_debt`, with extra keys:

        - ``ltv``: applied LTV (loan_amount / purchase_price, or 0.0 when
          purchase_price was not provided).
        - ``rate``: all-in fixed rate (float).
        - ``amort_years``: 30.
        - ``io_months``: 24 (default) or 36 (vintage <= 1995).
        - ``term_years``: 7.
        - ``loan_amount``: dollar amount, rounded to nearest $1,000.
        - ``sizing_method``: one of ``"ltv_constrained"``,
          ``"dscr_constrained"``, ``"vintage_dscr_constrained"``.
        - ``rate_breakdown``: dict with treasury/spread components for audit.
        - ``noi_used``: which NOI fed the DSCR sizing (min of t12/projected).
    """
    treasury = (
        dec(benchmark_5yr_treasury) if benchmark_5yr_treasury is not None
        else BENCHMARK_5YR_TREASURY
    )
    spread = (
        dec(agency_spread) if agency_spread is not None else AGENCY_SPREAD
    )
    all_in_rate = treasury + spread

    # I/O selection by vintage (older = wider capex window)
    if vintage is not None and vintage <= 1995:
        io_months = AGENCY_OLDER_IO_MONTHS
    else:
        io_months = AGENCY_DEFAULT_IO_MONTHS

    # NOI for DSCR sizing: take the more conservative of T12 / projected
    t12 = dec(t12_noi)
    proj = dec(projected_noi)
    if t12 > 0 and proj > 0:
        noi_for_sizing = min(t12, proj)
    elif t12 > 0:
        noi_for_sizing = t12
    elif proj > 0:
        noi_for_sizing = proj
    else:
        noi_for_sizing = Decimal("0")

    debt_constant = _annual_debt_constant(all_in_rate, AGENCY_AMORT_YEARS)

    # Rule 1: LTV
    ltv_loan: Optional[Decimal] = None
    if purchase_price is not None:
        pp = dec(purchase_price)
        if pp > 0:
            ltv_loan = AGENCY_MAX_LTV * pp

    # Rule 2: flat 1.25 DSCR
    if debt_constant > 0 and noi_for_sizing > 0:
        dscr_loan = noi_for_sizing / (AGENCY_MIN_DSCR * debt_constant)
    else:
        dscr_loan = Decimal("0")

    # Rule 3: vintage-adjusted DSCR
    vintage_required = _vintage_required_dscr(vintage)
    if debt_constant > 0 and noi_for_sizing > 0:
        vintage_loan = noi_for_sizing / (vintage_required * debt_constant)
    else:
        vintage_loan = Decimal("0")

    # Pick the binding (smallest) constraint
    candidates = [
        ("dscr_constrained", dscr_loan),
        ("vintage_dscr_constrained", vintage_loan),
    ]
    if ltv_loan is not None:
        candidates.append(("ltv_constrained", ltv_loan))

    # Filter out zero/None
    valid = [(name, amt) for name, amt in candidates if amt > 0]
    if not valid:
        loan_amount = Decimal("0")
        sizing_method = "dscr_constrained"
    else:
        sizing_method, loan_amount = min(valid, key=lambda x: x[1])

    # Disambiguate: if vintage and flat DSCR produce the same loan (current
    # flat-1.25 case), prefer the simpler "dscr_constrained" label.
    if (
        sizing_method == "vintage_dscr_constrained"
        and vintage_required == AGENCY_MIN_DSCR
        and loan_amount == dscr_loan
    ):
        sizing_method = "dscr_constrained"

    # Round to nearest $1,000 (institutional convention)
    if loan_amount > 0:
        loan_amount = (loan_amount / Decimal("1000")).quantize(Decimal("1")) * Decimal("1000")

    # Effective LTV (only meaningful when purchase_price provided)
    if purchase_price is not None and dec(purchase_price) > 0:
        applied_ltv = float(loan_amount / dec(purchase_price))
    else:
        applied_ltv = 0.0

    return {
        "ltv": round(applied_ltv, 4),
        "rate": float(all_in_rate),
        "amort_years": AGENCY_AMORT_YEARS,
        "io_months": io_months,
        "term_years": AGENCY_TERM_YEARS,
        "loan_amount": float(loan_amount),
        "sizing_method": sizing_method,
        "rate_breakdown": {
            "benchmark_5yr_treasury": float(treasury),
            "agency_spread": float(spread),
            "all_in_rate": float(all_in_rate),
        },
        "noi_used": float(noi_for_sizing),
        "required_dscr": float(vintage_required),
    }


def size_refi_loan(
    refi_event: Dict[str, Any],
    cashflow_by_month: List[Dict[str, Any]],
    debt_by_month: List[Dict[str, Any]],
    exit_assumptions: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Size a permanent loan from stabilized NOI and compute refi proceeds.

    Args:
        refi_event: The refi_event object from deal inputs
        cashflow_by_month: Monthly cashflow (needs NOI for sizing)
        debt_by_month: Monthly debt (needs bridge balance for payoff)
        exit_assumptions: Exit assumptions (for LTV cap rate fallback)

    Returns:
        Dict with:
          - perm_loan_terms: Complete debt_terms for the new loan
          - refi_proceeds: Net cash from refi (positive = cash out, negative = shortfall)
          - bridge_payoff: Outstanding bridge balance at trigger month
          - refi_costs: Dollar closing costs
          - sized_commitment: The computed loan amount
          - stabilized_noi: The NOI used for sizing
    """
    trigger_month = refi_event["trigger_month"][:7]  # Normalize to YYYY-MM
    sizing_method = refi_event["sizing_method"]
    noi_months = int(refi_event.get("noi_months", 12))
    refi_costs_pct = dec(refi_event.get("refi_costs_pct", Decimal("0.01")))
    perm_terms = refi_event.get("perm_loan_terms", {})

    # Find trigger month index in cashflow
    trigger_idx = None
    for i, m in enumerate(cashflow_by_month):
        if m["month"][:7] == trigger_month:
            trigger_idx = i
            break
    if trigger_idx is None:
        raise ValueError(f"refi_event trigger_month {trigger_month} not found in cashflow")

    # Compute trailing stabilized NOI
    start_idx = max(0, trigger_idx - noi_months)
    trailing_noi = sum(
        dec(cashflow_by_month[i].get("net_operating_income", 0))
        for i in range(start_idx, trigger_idx)
    )
    # Annualize if less than 12 months of history
    actual_months = trigger_idx - start_idx
    if actual_months > 0 and actual_months < 12:
        stabilized_noi = trailing_noi * Decimal("12") / Decimal(str(actual_months))
    elif actual_months == 0:
        # Use the trigger month's NOI × 12 as fallback
        stabilized_noi = dec(cashflow_by_month[trigger_idx].get("net_operating_income", 0)) * Decimal("12")
    else:
        stabilized_noi = trailing_noi

    # Size the loan
    perm_rate = dec(perm_terms.get("rate", Decimal("0.055")))
    perm_amort = int(perm_terms.get("amort_years", 30))

    if sizing_method == "dscr":
        target_dscr = dec(refi_event["target_dscr"])
        debt_constant = _annual_debt_constant(perm_rate, perm_amort)
        # Max loan = NOI / (DSCR × annual debt constant)
        if debt_constant > 0 and target_dscr > 0:
            sized_commitment = stabilized_noi / (target_dscr * debt_constant)
        else:
            sized_commitment = Decimal("0")
    elif sizing_method == "ltv":
        target_ltv = dec(refi_event["target_ltv"])
        # Property value = NOI / cap rate
        cap_rate = dec(refi_event.get("exit_cap_for_ltv", 0))
        if cap_rate == 0 and exit_assumptions:
            cap_rate = dec(exit_assumptions.get("exit_cap_rate", Decimal("0.05")))
        if cap_rate > 0:
            property_value = stabilized_noi / cap_rate
        else:
            property_value = Decimal("0")
        sized_commitment = property_value * target_ltv
    else:
        raise ValueError(f"Unknown refi sizing_method: {sizing_method}")

    # Round to nearest $1,000 (institutional convention)
    sized_commitment = (sized_commitment / Decimal("1000")).quantize(Decimal("1")) * Decimal("1000")

    # Bridge payoff: outstanding balance at trigger month
    bridge_payoff = Decimal("0")
    if trigger_idx < len(debt_by_month):
        # Use ending balance of the month BEFORE trigger (bridge is paid off at trigger)
        payoff_idx = trigger_idx - 1 if trigger_idx > 0 else 0
        bridge_payoff = dec(debt_by_month[payoff_idx].get("ending_balance", 0))

    # Refi costs
    refi_costs = sized_commitment * refi_costs_pct

    # Net refi proceeds
    refi_proceeds = sized_commitment - bridge_payoff - refi_costs

    # Build complete perm loan terms
    complete_perm_terms = {
        "commitment": round2(sized_commitment),
        "rate": float(perm_rate),
        "amort_years": perm_amort,
        "io_months": int(perm_terms.get("io_months", 0)),
        "loan_start_month": trigger_month,
        "loan_closing_costs": round2(refi_costs),
    }
    if perm_terms.get("term_months"):
        complete_perm_terms["term_months"] = int(perm_terms["term_months"])
    if perm_terms.get("rate_type"):
        complete_perm_terms["rate_type"] = perm_terms["rate_type"]
    if perm_terms.get("base_spread") is not None:
        complete_perm_terms["base_spread"] = float(perm_terms["base_spread"])
    if perm_terms.get("rate_cap") is not None:
        complete_perm_terms["rate_cap"] = float(perm_terms["rate_cap"])
    if perm_terms.get("rate_floor") is not None:
        complete_perm_terms["rate_floor"] = float(perm_terms["rate_floor"])
    if perm_terms.get("rate_curve"):
        complete_perm_terms["rate_curve"] = perm_terms["rate_curve"]

    return {
        "perm_loan_terms": complete_perm_terms,
        "refi_proceeds": round2(refi_proceeds),
        "bridge_payoff": round2(bridge_payoff),
        "refi_costs": round2(refi_costs),
        "sized_commitment": round2(sized_commitment),
        "stabilized_noi": round2(stabilized_noi),
        "sizing_method": sizing_method,
        "trigger_month": trigger_month,
    }


def compute_preferred_equity(
    time_grid: TimeGrid,
    layer: Dict[str, Any],
) -> Dict[str, Any]:
    """Compute monthly preferred equity returns.

    Preferred equity accrues a fixed annual return on outstanding balance.
    In 'current_pay' mode (default), the return is paid monthly.
    In 'accruing' mode, unpaid returns compound onto the balance.

    The balance is repaid at exit (handled via loan_payoff at term_months).

    Args:
        time_grid: Authoritative time grid
        layer: Capital stack layer with pref_return_rate, commitment, etc.

    Returns:
        Dict with by_month, by_year, summary (same structure as compute_debt)
    """
    commitment = dec(layer["commitment"])
    annual_rate = dec(layer.get("pref_return_rate", layer.get("rate", 0)))
    monthly_rate = annual_rate / Decimal("12")
    term_months = int(layer.get("term_months", 0))
    loan_start = month_id(layer.get("loan_start_month", time_grid.month_ids[0]))

    try:
        start_idx = time_grid.month_ids.index(loan_start)
    except ValueError:
        start_idx = 0

    balance = Decimal("0")
    by_month = []

    for idx, month in enumerate(time_grid.month_ids):
        beginning_balance = balance
        draw_amount = Decimal("0")

        # Draw at start
        if month == loan_start:
            draw_amount = commitment
            balance += draw_amount

        # Preferred return accrual
        pref_return = balance * monthly_rate if balance > 0 else Decimal("0")

        # Loan payoff at maturity (pref equity redeemed)
        loan_payoff = Decimal("0")
        if term_months > 0:
            maturity_idx = start_idx + term_months - 1
            if idx == maturity_idx and balance > 0:
                loan_payoff = balance
                balance = Decimal("0")
            elif idx > maturity_idx:
                pref_return = Decimal("0")

        by_month.append({
            "month": month,
            "beginning_balance": round2(beginning_balance),
            "draw_amount": round2(draw_amount),
            "interest_expense": round2(pref_return),
            "principal_payment": round2(loan_payoff),
            "debt_service": round2(pref_return),
            "ending_balance": round2(balance),
            "is_io_period": True,  # Pref equity is always IO-like
            "loan_payoff": round2(loan_payoff),
        })

    # By year
    by_year = []
    for year in time_grid.year_ids:
        year_months = [m for m in by_month if m["month"].startswith(year)]
        by_year.append({
            "year": year,
            "total_interest": round2(sum(dec(m["interest_expense"]) for m in year_months)),
            "total_principal": round2(sum(dec(m["principal_payment"]) for m in year_months)),
            "total_debt_service": round2(sum(dec(m["debt_service"]) for m in year_months)),
            "ending_balance": round2(dec(year_months[-1]["ending_balance"])) if year_months else 0.0,
        })

    total_return = sum(dec(m["interest_expense"]) for m in by_month)
    final_bal = dec(by_month[-1]["ending_balance"]) if by_month else Decimal("0")
    return {
        "by_month": by_month,
        "by_year": by_year,
        "summary": {
            "total_commitment": round2(commitment),
            "total_drawn": round2(commitment),
            "total_interest_paid": round2(total_return),
            "total_principal_paid": round2(commitment) if term_months > 0 else 0.0,
            "final_balance": round2(final_bal),
            "weighted_avg_rate": round2(annual_rate),
            "amortizing_payment": 0.0,
        },
    }


def compute_debt(
    time_grid: TimeGrid,
    debt_terms: Optional[Dict[str, Any]] = None,
    debt_draw_schedule: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Compute monthly debt service with I/O and amortization.

    Args:
        time_grid: Authoritative time grid
        debt_terms: Loan terms (commitment, rate, amort_years, io_months)
        debt_draw_schedule: Optional draw schedule (defaults to full draw at start)

    Returns:
        Dict with by_month, by_year, summary
    """
    if not debt_terms:
        # No debt - return empty structure
        return {
            "by_month": [
                {
                    "month": m,
                    "beginning_balance": 0.0,
                    "draw_amount": 0.0,
                    "interest_expense": 0.0,
                    "principal_payment": 0.0,
                    "debt_service": 0.0,
                    "ending_balance": 0.0,
                    "is_io_period": False,
                }
                for m in time_grid.month_ids
            ],
            "by_year": [
                {
                    "year": y,
                    "total_interest": 0.0,
                    "total_principal": 0.0,
                    "total_debt_service": 0.0,
                    "ending_balance": 0.0,
                }
                for y in time_grid.year_ids
            ],
            "summary": {
                "total_commitment": 0.0,
                "total_drawn": 0.0,
                "total_interest_paid": 0.0,
                "total_principal_paid": 0.0,
                "final_balance": 0.0,
                "weighted_avg_rate": 0.0,
                "amortizing_payment": 0.0,
            },
        }

    # Extract debt terms
    commitment = dec(debt_terms["commitment"])
    annual_rate = dec(debt_terms["rate"])
    amort_years = int(debt_terms["amort_years"])
    io_months = int(debt_terms.get("io_months", 0))
    term_months = int(debt_terms.get("term_months", 0))  # 0 = no term limit
    loan_start_month = month_id(debt_terms.get("loan_start_month", time_grid.month_ids[0]))

    # Variable rate / rate cap support
    rate_type = debt_terms.get("rate_type", "fixed")
    base_spread = dec(debt_terms.get("base_spread", 0))
    rate_cap: Optional[Decimal] = dec(debt_terms["rate_cap"]) if debt_terms.get("rate_cap") is not None else None
    rate_floor: Optional[Decimal] = dec(debt_terms["rate_floor"]) if debt_terms.get("rate_floor") is not None else None
    rate_curve_raw: List[Dict[str, Any]] = debt_terms.get("rate_curve", [])
    rate_curve_sorted = sorted(rate_curve_raw, key=lambda e: e["start_month"]) if rate_curve_raw else []

    # Rate cap expiry and prepayment
    cap_expiry_month: Optional[str] = None
    if debt_terms.get("rate_cap_expiry_month"):
        cap_expiry_month = month_id(debt_terms["rate_cap_expiry_month"])
    cap_renewal_rate: Optional[Decimal] = None
    if debt_terms.get("rate_cap_renewal_rate") is not None:
        cap_renewal_rate = dec(debt_terms["rate_cap_renewal_rate"])
    cap_renewal_cost = dec(debt_terms.get("rate_cap_renewal_cost", 0))

    prepayment_type = debt_terms.get("prepayment_type", "none")
    prepayment_lockout = int(debt_terms.get("prepayment_lockout_months", 0))
    prepayment_penalty_pct = dec(debt_terms.get("prepayment_penalty_pct", 0))
    treasury_rate = dec(debt_terms.get("treasury_rate", 0))

    # For assumable loans, the fixed monthly payment was computed from the
    # original loan balance (not the assumed/current balance).  When provided,
    # this overrides the payment calculated from the outstanding balance.
    fixed_payment: Optional[Decimal] = None
    if debt_terms.get("fixed_monthly_payment"):
        fixed_payment = dec(debt_terms["fixed_monthly_payment"])

    # When a bridge loan is extended by +1 month for refi alignment, the
    # payoff month should only retire the balance (no regular DS payment).
    # RedIQ's bridge has its last DS one month before the payoff.
    skip_maturity_ds = bool(debt_terms.get("skip_maturity_ds", False))

    # Calculate base monthly rate and amort months
    monthly_rate = annual_rate / Decimal("12")
    amort_months = amort_years * 12

    # Build draw schedule lookup.
    # For staged (construction) draws, separate the initial draw from
    # subsequent construction draws.  RedIQ computes interest on the balance
    # BEFORE applying construction draws — only the initial Day-0 draw
    # accrues interest in its first month.
    draw_lookup: Dict[str, Decimal] = {}
    construction_draw_lookup: Dict[str, Decimal] = {}
    is_staged = bool(debt_terms.get("staged_draws", False))

    if is_staged and debt_draw_schedule and len(debt_draw_schedule) > 1:
        # Staged drawdown: first entry = initial draw, rest = construction draws
        first = debt_draw_schedule[0]
        draw_lookup[month_id(first["month"])] = dec(first["draw_amount"])
        for draw in debt_draw_schedule[1:]:
            m = month_id(draw["month"])
            construction_draw_lookup[m] = construction_draw_lookup.get(m, Decimal("0")) + dec(draw["draw_amount"])
    elif debt_draw_schedule:
        for draw in debt_draw_schedule:
            m = month_id(draw["month"])
            draw_lookup[m] = draw_lookup.get(m, Decimal("0")) + dec(draw["draw_amount"])
    else:
        # Default: full draw at loan start
        draw_lookup[loan_start_month] = commitment

    # Determine which months are in I/O period
    # I/O starts at loan_start_month and lasts for io_months
    try:
        loan_start_idx = time_grid.month_ids.index(loan_start_month)
    except ValueError:
        loan_start_idx = 0  # Default to start if loan_start not in grid

    io_end_idx = loan_start_idx + io_months  # First amortizing month index

    # Track balance and calculate debt service
    results: List[DebtMonthResult] = []
    outstanding_balance = Decimal("0")
    amortizing_payment: Optional[Decimal] = None

    for idx, month in enumerate(time_grid.month_ids):
        beginning_balance = outstanding_balance

        if is_staged:
            # Staged draws: apply initial draw before interest,
            # construction draws after interest
            initial_this_month = draw_lookup.get(month, Decimal("0"))
            outstanding_balance += initial_this_month
            draw_amount = initial_this_month
        else:
            # Standard loan: apply full draw before interest
            draw_amount = draw_lookup.get(month, Decimal("0"))
            outstanding_balance += draw_amount

        # Determine if in I/O period
        is_io_period = idx < io_end_idx

        # Determine effective rate for this month
        if rate_type == "variable" and rate_curve_sorted:
            effective_annual = _get_effective_rate(
                month, rate_type, annual_rate,
                {}, rate_curve_sorted,
                base_spread, rate_cap, rate_floor,
                cap_expiry_month, cap_renewal_rate,
            )
            effective_monthly = effective_annual / Decimal("12")
        else:
            effective_monthly = monthly_rate

        # Calculate interest
        interest_expense = outstanding_balance * effective_monthly

        # For staged draws: apply construction draws AFTER interest calculation.
        # RedIQ computes interest on the pre-draw balance, then adds the draw.
        if is_staged:
            construction_draw = construction_draw_lookup.get(month, Decimal("0"))
            outstanding_balance += construction_draw
            draw_amount += construction_draw

        # Calculate principal payment
        if outstanding_balance == Decimal("0"):
            # No balance, no payment
            principal_payment = Decimal("0")
            debt_service = Decimal("0")
        elif is_io_period:
            # Interest-only: no principal payment
            principal_payment = Decimal("0")
            debt_service = interest_expense
        else:
            # Amortizing period
            if rate_type == "variable" and rate_curve_sorted and fixed_payment is None:
                # Variable rate: recalculate payment each month based on current rate
                remaining_amort_months = amort_months - (idx - io_end_idx)
                if remaining_amort_months <= 0:
                    remaining_amort_months = 1
                amortizing_payment = _calculate_amortizing_payment(
                    outstanding_balance, effective_monthly, remaining_amort_months
                )
            elif amortizing_payment is None:
                if fixed_payment is not None:
                    # Assumable loan: use the payment from original loan terms
                    amortizing_payment = fixed_payment
                else:
                    # First amortizing month: calculate payment based on current balance
                    remaining_amort_months = amort_months - (idx - io_end_idx)
                    if remaining_amort_months <= 0:
                        remaining_amort_months = 1  # Prevent division by zero
                    amortizing_payment = _calculate_amortizing_payment(
                        outstanding_balance, effective_monthly, remaining_amort_months
                    )

            debt_service = amortizing_payment
            principal_payment = debt_service - interest_expense

            # Ensure principal doesn't exceed balance
            if principal_payment > outstanding_balance:
                principal_payment = outstanding_balance
                debt_service = principal_payment + interest_expense

        # Loan maturity: payoff the remaining balance (separate from debt_service).
        # In RedIQ, the regular DS payment (row 92) continues in the maturity month;
        # the balloon payoff (row 94) covers only the remaining balance beyond that
        # month's normal principal payment.
        # Exception: when skip_maturity_ds is set (bridge refi alignment), the
        # payoff month has NO regular DS — only the balance is retired.
        loan_payoff_amount = Decimal("0")
        if term_months > 0:
            maturity_idx = loan_start_idx + term_months - 1
            if idx == maturity_idx and outstanding_balance > Decimal("0"):
                if skip_maturity_ds:
                    # Refi alignment: no DS at payoff month, full balance as payoff
                    loan_payoff_amount = outstanding_balance
                    interest_expense = Decimal("0")
                    debt_service = Decimal("0")
                    principal_payment = outstanding_balance
                else:
                    # Normal maturity: DS continues, payoff covers the remainder
                    loan_payoff_amount = outstanding_balance - principal_payment
                    principal_payment = outstanding_balance  # zeros the balance
            elif idx > maturity_idx:
                # Post-maturity: no payments, balance already 0
                interest_expense = Decimal("0")
                principal_payment = Decimal("0")
                debt_service = Decimal("0")

        # Update balance
        outstanding_balance -= principal_payment
        ending_balance = outstanding_balance

        results.append(
            DebtMonthResult(
                month=month,
                beginning_balance=beginning_balance,
                draw_amount=draw_amount,
                interest_expense=interest_expense,
                principal_payment=principal_payment,
                debt_service=debt_service,
                ending_balance=ending_balance,
                is_io_period=is_io_period,
                loan_payoff=loan_payoff_amount,
            )
        )

    # Aggregate by month
    by_month = [
        {
            "month": r.month,
            "beginning_balance": round2(r.beginning_balance),
            "draw_amount": round2(r.draw_amount),
            "interest_expense": round2(r.interest_expense),
            "principal_payment": round2(r.principal_payment),
            "debt_service": round2(r.debt_service),
            "ending_balance": round2(r.ending_balance),
            "is_io_period": r.is_io_period,
            "loan_payoff": round2(r.loan_payoff),
        }
        for r in results
    ]

    # Inject rate cap renewal cost at expiry month.
    # Cashflow deducts financing costs via loan_closing_costs; keep the
    # explicit audit/display key while also flowing the cost through the
    # standard cashflow-consumed field.
    if cap_renewal_cost > 0 and cap_expiry_month:
        for row in by_month:
            if row["month"] == cap_expiry_month:
                renewal_cost = round2(cap_renewal_cost)
                row["rate_cap_renewal_cost"] = renewal_cost
                row["loan_closing_costs"] = round2(
                    dec(row.get("loan_closing_costs", 0)) + cap_renewal_cost
                )
                break

    # Aggregate by year
    by_year: List[Dict[str, Any]] = []
    for year in time_grid.year_ids:
        year_results = [r for r in results if r.month.startswith(year)]
        total_interest = sum((r.interest_expense for r in year_results), Decimal("0"))
        total_principal = sum((r.principal_payment for r in year_results), Decimal("0"))
        total_debt_service = sum((r.debt_service for r in year_results), Decimal("0"))
        ending_balance = year_results[-1].ending_balance if year_results else Decimal("0")

        by_year.append(
            {
                "year": year,
                "total_interest": round2(total_interest),
                "total_principal": round2(total_principal),
                "total_debt_service": round2(total_debt_service),
                "ending_balance": round2(ending_balance),
            }
        )

    # Summary
    total_drawn = sum((r.draw_amount for r in results), Decimal("0"))
    total_interest_paid = sum((r.interest_expense for r in results), Decimal("0"))
    total_principal_paid = sum((r.principal_payment for r in results), Decimal("0"))
    final_balance = results[-1].ending_balance if results else Decimal("0")

    result = {
        "by_month": by_month,
        "by_year": by_year,
        "summary": {
            "total_commitment": round2(commitment),
            "total_drawn": round2(total_drawn),
            "total_interest_paid": round2(total_interest_paid),
            "total_principal_paid": round2(total_principal_paid),
            "final_balance": round2(final_balance),
            "weighted_avg_rate": round2(annual_rate),
            "amortizing_payment": round2(amortizing_payment) if amortizing_payment else 0.0,
        },
    }

    # Attach prepayment metadata for exit module consumption
    if prepayment_type and prepayment_type != "none":
        result["prepayment"] = {
            "type": prepayment_type,
            "lockout_months": prepayment_lockout,
            "penalty_pct": round2(prepayment_penalty_pct),
            "treasury_rate": round2(treasury_rate),
        }

    return result
