"""
RedIQ Output Writer
====================
Maps engine results + inputs → RedIQ clone workbook cells.

Uses ZIP-level XML manipulation (via XlsmWriter) to write cell values
without corrupting VBA, styles, conditional formatting, or other features
that openpyxl destroys on save.

CF Calculations monthly source data fills cols S-EI; annual SUMIF formulas
auto-calculate.  Input sheet assumptions, Waterfall tier data, and
Summary/metric cells are also populated.

Usage:
    from engine.rediq_output import generate_rediq_workbook
    generate_rediq_workbook(results, inputs, output_path)

    # Or from CLI:
    python runs/generate_rediq_workbook.py excel/deal.xlsm --output output/deal_model.xlsm
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import openpyxl  # read_only mode for layout detection (safe, no corruption)

from engine.xlsx_writer import XlsmWriter


# ── Column Helpers ────────────────────────────────────────────────────────

def _month_to_cf_col(month_index: int) -> int:
    """0-based month index → CF Calculations column.  Month 0 = col S (19)."""
    return 19 + month_index


def _day0_col() -> int:
    """Day 0 column in CF Calculations = col D (4)."""
    return 4


def _year_to_cf_col(year_index: int) -> int:
    """0-based year index → CF Calculations annual column.  Year 0 = col E (5)."""
    return 5 + year_index


def _year_to_op_col(year_index: int) -> int:
    """0-based year index → Operating Calculations annual column.  Year 0 = col C (3)."""
    return 3 + year_index


# ── Cell Writer ───────────────────────────────────────────────────────────

def _write_cell(writer: XlsmWriter, sheet: str, row: int, col: int, value):
    """Write a value to a cell via the XlsmWriter.

    Preserves any existing formula and sets the value as cached result.
    Use _write_cf_cell() for CF Calculations where formulas must be replaced.
    """
    writer.set_cell_value(sheet, row, col, value)


def _write_cell_if(writer: XlsmWriter, sheet: str, row: int, col: int, value):
    """Write a value only if it is not None and non-zero."""
    if value is not None and value != 0:
        _write_cell(writer, sheet, row, col, value)


def _write_cf_cell(writer: XlsmWriter, row: int, col: int, value):
    """Write a value to CF Calculations, replacing any existing formula.

    CF Calcs cells have formulas referencing Operating Calculations that
    we need to overwrite with engine-computed values.
    """
    writer.remove_formula(CF_SHEET, row, col)
    writer.set_cell_value(CF_SHEET, row, col, value)


def _write_cf_cell_if(writer: XlsmWriter, row: int, col: int, value):
    """Write explicit CF Calculations values, including zero.

    CF Calculations cells can carry template formulas or stale static values;
    an engine-computed zero is therefore a real value that must remove the
    template formula and write a static 0.  Only None means "no engine value".
    """
    if value is not None:
        _write_cf_cell(writer, row, col, value)


# ── CF Calculations Row Map ──────────────────────────────────────────────
# All row numbers reference the CF Calculations sheet in the clone template.

CF_ROW = {
    # Acquisition Cost (Day 0 only)
    "purchase_price": 10,
    "closing_costs": 11,
    "upfront_capex": 12,
    "acquisition_cost": 13,

    # Operating Revenue
    "market_rent": 18,
    "loss_to_lease": 19,
    "gross_potential_revenue": 20,    # SUBTOTAL formula
    "vacancy": 22,
    "renovation_downtime": 23,
    "concessions": 24,
    "non_revenue_units": 25,
    "collection_loss": 26,
    "base_rental_revenue": 27,       # SUBTOTAL formula
    "expense_reimbursements": 29,
    "other_residential_income": 30,
    "commercial_income": 31,
    "other_income": 32,
    "egr": 34,                       # SUBTOTAL formula

    # Operating Expenses (rows 39-52 mapped by category)
    "total_opex": 54,

    # NOI
    "noi_before_reserves": 59,
    "replacement_reserves": 60,
    "noi_after_reserves": 62,        # SUBTOTAL formula

    # Capital Expenditures
    "unit_renovations": 67,
    "property_wide_capex": 68,
    "commercial_ti_lc": 69,
    "total_capex": 71,
    "paid_from_upfront": 72,

    # Operating CF
    "operating_cf": 76,              # SUBTOTAL formula

    # Disposition
    "gross_sales_proceeds": 80,
    "selling_costs": 81,
    "net_sales_proceeds": 82,        # SUBTOTAL formula

    # Unleveraged CF
    "unleveraged_cf": 86,

    # Financing
    "loan_drawdowns": 90,
    "loan_closing_costs": 91,
    "debt_service": 92,
    "prepayment_costs": 93,
    "loan_repayments": 94,
    "financing_cf": 95,              # SUBTOTAL formula

    # Reserves
    "reserves_funded": 100,
    "reserves_replenished": 101,
    "reserves_used": 102,
    "release_reserves": 103,
    "reserve_cf": 104,               # SUBTOTAL formula

    # Leveraged CF
    "leveraged_cf": 108,

    # Partnership Expenses
    "partnership_closing_costs": 113,
    "annual_partnership_expenses": 114,
    "total_partnership_expenses": 115,

    # Partnership Fees
    "acquisition_fee": 118,
    "asset_management_fee": 120,
    "disposition_fee": 121,
    "total_fees": 122,

    # CF Before/After Promote
    "cf_before_promote": 124,
    "promote_payment": 126,
    "cf_to_partnership_monthly": 129,
    "cf_to_partnership": 130,

    # Sponsor / LP splits
    "sponsor_share": 134,
    "lp_share": 141,

    # Metrics rows
    "going_in_cap_rate": 145,
    "cash_on_cash_yield": 146,
    "blended_dscr": 158,
}

# OpEx category → CF Calculations row mapping.
# Category names come from the bridge's category_name field; we normalize
# to lower-case for matching.
OPEX_CATEGORY_ROW = {
    "repair & maintenance": 39,
    "contract services": 40,
    "security": 41,
    "turnover / make-ready": 42,
    "landscaping / grounds": 43,
    "personnel": 44,
    "marketing / advertising": 45,
    "administrative": 46,
    "administrative expenses": 46,
    "utilities": 47,
    "electricity": 47,
    "fuel (gas & oil)": 47,
    "water & sewer": 47,
    "other utilities": 47,
    "insurance": 48,
    "real estate taxes": 49,
    "other property taxes": 49,
    "property management fee": 50,
    "other operating expenses": 51,
    "franchise fee": 51,
    "reimbursements": 52,
}

# IRR value cells (column C)
IRR_CELLS = {
    "unlevered_irr": (86, 3),    # C86
    "levered_irr": (108, 3),     # C108
    "partnership_irr": (130, 3), # C130
}


# ── CF Calculations Writer ───────────────────────────────────────────────

CF_SHEET = "CF Calculations"


def write_monthly_cashflows(writer: XlsmWriter, results: Dict[str, Any], inputs: Dict[str, Any]):
    """Write all monthly data to CF Calculations sheet (cols S-EI)."""
    cashflow = results.get("cashflow", {})
    by_month = cashflow.get("by_month", [])
    revenue = results.get("revenue", {})
    rent_by_month = revenue.get("base_rent", {}).get("by_month", [])
    programs_by_month = revenue.get("programs", {}).get("by_month", [])

    # Build lookups by month
    rent_lookup = {r["month"]: r for r in rent_by_month}
    prog_lookup = {p["month"]: p for p in programs_by_month}

    # OpEx by category by month
    opex_result = results.get("opex", {})
    opex_cat_by_month = opex_result.get("by_category_by_month", [])
    # Build {month: {normalized_category: expense}}
    opex_cat_lookup: Dict[str, Dict[str, float]] = {}
    for row in opex_cat_by_month:
        m = row["month"]
        cat = row.get("category", "").lower().strip()
        if m not in opex_cat_lookup:
            opex_cat_lookup[m] = {}
        # Accumulate (some categories like utilities may have sub-categories)
        target_row = OPEX_CATEGORY_ROW.get(cat)
        if target_row:
            key = str(target_row)
            opex_cat_lookup[m][key] = opex_cat_lookup[m].get(key, 0) + float(row.get("expense", 0))

    # Debt by month
    debt_result = results.get("debt", {})
    debt_by_month = debt_result.get("by_month", [])
    debt_lookup = {d["month"]: d for d in debt_by_month}

    # CapEx by month
    capex_result = results.get("capex", {})
    capex_by_month = capex_result.get("by_month", [])
    capex_lookup = {c["month"]: c for c in capex_by_month}

    # Renovation by month (for unit renovation cost breakdown)
    reno_result = results.get("renovations", {})
    reno_by_month = reno_result.get("by_month", [])
    reno_lookup = {r["month"]: r for r in reno_by_month}

    # Fund waterfall
    fund_wf = results.get("fund_waterfall", {})
    wf_by_year = fund_wf.get("by_year", [])
    wf_by_year_lookup = {str(y["year"]): y for y in wf_by_year}
    wf_closing = fund_wf.get("closing_costs", {})
    wf_promote = fund_wf.get("promote", {})
    monthly_promotes = wf_promote.get("monthly_promotes", {})
    wf_summary = fund_wf.get("summary", {})

    # Metrics
    metrics = results.get("metrics", {})
    exit_data = metrics.get("exit", {})

    # Determine analysis start for year mapping
    time_grid = results.get("time_grid", {})
    months = time_grid.get("months", [])
    analysis_start = time_grid.get("analysis_start", "")
    if analysis_start:
        start_year = int(analysis_start[:4])
        start_mo = int(analysis_start[5:7])
    elif months:
        start_year = int(months[0][:4])
        start_mo = int(months[0][5:7])
    else:
        start_year, start_mo = 2026, 1

    # Sponsor/LP pct from fund_assumptions
    fund_assumptions = inputs.get("fund_assumptions", {})
    sponsor_pct = fund_assumptions.get("sponsor_equity_pct", 0)
    lp_pct = fund_assumptions.get("lp_equity_pct", 0)
    # CHDO deals: if sponsor_pct is 0, derive from equity partner
    if sponsor_pct == 0 and lp_pct > 0:
        sponsor_pct = round(1.0 - lp_pct, 6)

    for i, month_data in enumerate(by_month):
        col = _month_to_cf_col(i)
        if col > 139:  # Column EI = 139 for 120 months
            break

        month_id = month_data.get("month", "")

        # --- Revenue detail ---
        rent = rent_lookup.get(month_id, {})
        prog = prog_lookup.get(month_id, {})

        market_rent = rent.get("market_rent")
        if market_rent is not None:
            _write_cf_cell(writer, CF_ROW["market_rent"], col, market_rent)

        # Loss to lease (negative in RedIQ)
        inplace = rent.get("inplace_rent")
        if market_rent is not None and inplace is not None:
            ltl = inplace - market_rent  # negative when in-place < market
            _write_cf_cell_if(writer, CF_ROW["loss_to_lease"], col, ltl)

        # Vacancy (negative)
        vacancy = rent.get("physical_vacancy_loss")
        if vacancy is not None:
            _write_cf_cell(writer, CF_ROW["vacancy"], col, -abs(vacancy))

        # Collection loss (negative)
        collection = rent.get("collection_loss")
        if collection is not None:
            _write_cf_cell(writer, CF_ROW["collection_loss"], col, -abs(collection))

        # Expense reimbursements (utility recovery)
        util_recov = month_data.get("utility_recovery")
        _write_cf_cell_if(writer, CF_ROW["expense_reimbursements"], col, util_recov)

        # Other income (programs)
        net_programs = prog.get("net_programs", month_data.get("net_programs"))
        _write_cf_cell_if(writer, CF_ROW["other_residential_income"], col, net_programs)

        # EGR
        egi = month_data.get("effective_gross_income")
        if egi is not None:
            _write_cf_cell(writer, CF_ROW["egr"], col, egi)

        # --- OpEx by category ---
        opex_cats = opex_cat_lookup.get(month_id, {})
        for row_key, amount in opex_cats.items():
            row_num = int(row_key)
            # OpEx is negative in RedIQ
            _write_cf_cell(writer, row_num, col, -abs(amount))

        # Total OpEx (negative)
        total_opex = month_data.get("total_opex")
        if total_opex is not None:
            _write_cf_cell(writer, CF_ROW["total_opex"], col, -abs(total_opex))

        # --- NOI ---
        noi = month_data.get("net_operating_income")
        if noi is not None:
            _write_cf_cell(writer, CF_ROW["noi_before_reserves"], col, noi)

        # Replacement reserves (negative)
        reserves = month_data.get("replacement_reserves")
        if reserves is not None:
            _write_cf_cell(writer, CF_ROW["replacement_reserves"], col, -abs(reserves))

        # --- CapEx ---
        capex = capex_lookup.get(month_id, {})
        reno = reno_lookup.get(month_id, {})

        # Unit renovations (negative)
        reno_cost = reno.get("renovation_cost", 0)
        if reno_cost and reno_cost > 0:
            _write_cf_cell(writer, CF_ROW["unit_renovations"], col, -abs(reno_cost))

        # Property-wide CapEx: one_time + recurring (negative)
        one_time = capex.get("one_time_capex", 0) or 0
        recurring = capex.get("recurring_capex", 0) or 0
        prop_capex = one_time + recurring
        if prop_capex > 0:
            _write_cf_cell(writer, CF_ROW["property_wide_capex"], col, -abs(prop_capex))

        # Total CapEx (negative)
        total_capex = month_data.get("total_capex")
        if total_capex is not None:
            _write_cf_cell(writer, CF_ROW["total_capex"], col, -abs(total_capex))

        # UCF
        ucf = month_data.get("unleveraged_cash_flow")
        if ucf is not None:
            _write_cf_cell(writer, CF_ROW["unleveraged_cf"], col, ucf)

        # --- Disposition (exit month only) ---
        gross_sale = month_data.get("gross_sale_price")
        if gross_sale is not None and gross_sale > 0:
            _write_cf_cell(writer, CF_ROW["gross_sales_proceeds"], col, gross_sale)

        sale_costs = month_data.get("sale_costs")
        if sale_costs is not None and sale_costs > 0:
            _write_cf_cell(writer, CF_ROW["selling_costs"], col, -abs(sale_costs))

        # --- Debt / Financing ---
        debt = debt_lookup.get(month_id, {})

        # Loan drawdowns
        draw_amount = debt.get("draw_amount", 0)
        financing_draw = debt.get("financing_draw", 0)
        total_draw = (draw_amount or 0) + (financing_draw or 0)
        _write_cf_cell_if(writer, CF_ROW["loan_drawdowns"], col, total_draw)

        # Loan closing costs (negative)
        loan_cc = debt.get("loan_closing_costs", 0)
        if loan_cc and loan_cc > 0:
            _write_cf_cell(writer, CF_ROW["loan_closing_costs"], col, -abs(loan_cc))

        # Debt service (negative)
        ds = debt.get("debt_service", 0)
        if ds and ds != 0:
            _write_cf_cell(writer, CF_ROW["debt_service"], col, -abs(ds))

        # Loan repayments/payoff (negative)
        payoff = month_data.get("loan_payoff", 0) or debt.get("loan_payoff", 0)
        if payoff and payoff != 0:
            _write_cf_cell(writer, CF_ROW["loan_repayments"], col, -abs(payoff))

        # LCF
        lcf = month_data.get("leveraged_cash_flow")
        if lcf is not None:
            _write_cf_cell(writer, CF_ROW["leveraged_cf"], col, lcf)

        # --- Partnership / Fund Waterfall (monthly) ---
        if fund_wf:
            # Determine which analysis year this month falls in
            m_year = int(month_id[:4])
            m_month = int(month_id[5:7])
            months_from_start = (m_year - start_year) * 12 + (m_month - start_mo)
            ay_idx = months_from_start // 12 + 1

            # Get year data for this analysis year
            yr_data = wf_by_year_lookup.get(str(ay_idx), {})

            if yr_data:
                # Spread annual values to months (divide by 12, or by months_in_year)
                # Determine how many months are in this analysis year
                months_in_ay = sum(
                    1 for j, md in enumerate(by_month)
                    if _month_to_analysis_year(md["month"], start_year, start_mo) == ay_idx
                )
                months_in_ay = max(months_in_ay, 1)

                # Annual partnership expenses (negative, spread monthly)
                ann_pe = yr_data.get("partnership_expenses", 0)
                if ann_pe and ann_pe != 0:
                    monthly_pe = ann_pe / months_in_ay
                    _write_cf_cell(writer, CF_ROW["annual_partnership_expenses"], col, -abs(monthly_pe))

                # Asset management fee (negative, spread monthly)
                am_fee = yr_data.get("asset_management_fee", 0)
                if am_fee and am_fee != 0:
                    monthly_am = am_fee / months_in_ay
                    _write_cf_cell(writer, CF_ROW["asset_management_fee"], col, -abs(monthly_am))

                # Disposition fee (negative, exit year only — put in last month of year)
                disp_fee = yr_data.get("disposition_fee", 0)
                if disp_fee and disp_fee != 0:
                    # Only write in the last month of the exit year
                    is_last_month = (i == len(by_month) - 1) or (
                        i + 1 < len(by_month) and
                        _month_to_analysis_year(by_month[i + 1]["month"], start_year, start_mo) != ay_idx
                    )
                    if is_last_month:
                        _write_cf_cell(writer, CF_ROW["disposition_fee"], col, -abs(disp_fee))

                # CF before promote
                cf_bp = yr_data.get("cash_flow_before_promote", 0)
                if cf_bp is not None:
                    monthly_cfbp = cf_bp / months_in_ay
                    _write_cf_cell(writer, CF_ROW["cf_before_promote"], col, monthly_cfbp)

                # CF to partnership
                cf_tp = yr_data.get("cash_flow_to_partnership", 0)
                if cf_tp is not None:
                    monthly_cftp = cf_tp / months_in_ay
                    _write_cf_cell(writer, CF_ROW["cf_to_partnership"], col, monthly_cftp)

                    # Sponsor share
                    if sponsor_pct > 0:
                        _write_cf_cell(writer, CF_ROW["sponsor_share"], col, monthly_cftp * sponsor_pct)

                    # LP share
                    if lp_pct > 0:
                        _write_cf_cell(writer, CF_ROW["lp_share"], col, monthly_cftp * lp_pct)

            # Promote payment (monthly granularity available)
            promote = monthly_promotes.get(month_id, 0)
            if promote and promote != 0:
                _write_cf_cell(writer, CF_ROW["promote_payment"], col, -abs(promote))

    # --- Day 0 values (column D = 4) ---
    d0_col = _day0_col()
    purchase = inputs.get("purchase_assumptions", {})

    pp = purchase.get("purchase_price")
    if pp:
        _write_cf_cell(writer, CF_ROW["purchase_price"], d0_col, -abs(pp))

    cc = purchase.get("closing_costs", 0)
    if cc:
        _write_cf_cell(writer, CF_ROW["closing_costs"], d0_col, -abs(cc))

    # Upfront CapEx
    capex_upfront = inputs.get("capex_upfront_funded", [])
    if capex_upfront:
        total_upfront = sum(e.get("amount", 0) for e in capex_upfront)
        if total_upfront > 0:
            _write_cf_cell(writer, CF_ROW["upfront_capex"], d0_col, -abs(total_upfront))

    # Day 0 debt draw (loan commitment)
    debt_terms = inputs.get("debt_terms", {})
    commitment = debt_terms.get("commitment", 0)
    debt_draw_schedule = inputs.get("debt_draw_schedule")
    if debt_draw_schedule:
        day0_draw = debt_draw_schedule[0].get("draw_amount", 0) if debt_draw_schedule else 0
    elif commitment:
        day0_draw = commitment
    else:
        day0_draw = 0
    # Additional loan Day 0 draws
    for extra in inputs.get("additional_debt_terms", []):
        if not extra.get("loan_start_month"):
            day0_draw += extra.get("commitment", 0)
    if day0_draw > 0:
        _write_cf_cell(writer, CF_ROW["loan_drawdowns"], d0_col, day0_draw)

    # Day 0 loan closing costs
    loan_cc_day0 = debt_terms.get("loan_closing_costs", 0)
    loan_points = debt_terms.get("loan_points_pct", 0)
    if loan_points and commitment:
        loan_cc_day0 = commitment * loan_points
    if loan_cc_day0 and loan_cc_day0 > 0:
        _write_cf_cell(writer, CF_ROW["loan_closing_costs"], d0_col, -abs(loan_cc_day0))

    # Partnership closing costs (Day 0)
    if wf_closing:
        pcc = wf_closing.get("partnership_closing_costs", 0)
        if pcc and pcc > 0:
            _write_cf_cell(writer, CF_ROW["partnership_closing_costs"], d0_col, -abs(pcc))

        acq_fee = wf_closing.get("acquisition_fee", 0)
        if acq_fee and acq_fee > 0:
            _write_cf_cell(writer, CF_ROW["acquisition_fee"], d0_col, -abs(acq_fee))


def _month_to_analysis_year(month_id: str, start_year: int, start_mo: int) -> int:
    """Convert month ID to 1-based analysis year index."""
    m_year = int(month_id[:4])
    m_month = int(month_id[5:7])
    months_from_start = (m_year - start_year) * 12 + (m_month - start_mo)
    return months_from_start // 12 + 1


def write_irr_values(writer: XlsmWriter, results: Dict[str, Any]):
    """Write IRR values to column C of CF Calculations."""
    metrics = results.get("metrics", {})
    irr = metrics.get("irr", {})

    unlevered_irr = irr.get("unlevered_irr")
    if unlevered_irr is not None:
        _write_cf_cell(writer, *IRR_CELLS["unlevered_irr"], unlevered_irr)

    levered_irr = irr.get("levered_irr")
    if levered_irr is not None:
        _write_cf_cell(writer, *IRR_CELLS["levered_irr"], levered_irr)

    fund_wf = results.get("fund_waterfall", {})
    wf_summary = fund_wf.get("summary", {})
    partnership_irr = wf_summary.get("partnership_irr")
    if partnership_irr is not None:
        _write_cf_cell(writer, *IRR_CELLS["partnership_irr"], partnership_irr)


def write_metrics_rows(writer: XlsmWriter, results: Dict[str, Any]):
    """Write yield and DSCR metrics to CF Calculations annual columns."""
    metrics = results.get("metrics", {})

    # Going-in cap rate (all annual columns get same value)
    yields = metrics.get("yields", {})
    going_in = yields.get("going_in_cap_rate")
    if going_in is not None:
        for yr in range(10):
            _write_cf_cell(writer, CF_ROW["going_in_cap_rate"], _year_to_cf_col(yr), going_in)

    # Cash-on-cash yield by year
    coc = metrics.get("cash_on_cash", {})
    coc_by_year = coc.get("by_year", [])
    for i, yr_data in enumerate(coc_by_year):
        if i >= 10:
            break
        y = yr_data.get("yield")
        if y is not None:
            _write_cf_cell(writer, CF_ROW["cash_on_cash_yield"], _year_to_cf_col(i), y)

    # Blended DSCR by year
    dscr = metrics.get("dscr", {})
    dscr_by_year = dscr.get("by_year", [])
    for i, yr_data in enumerate(dscr_by_year):
        if i >= 10:
            break
        d = yr_data.get("dscr")
        if d is not None:
            _write_cf_cell(writer, CF_ROW["blended_dscr"], _year_to_cf_col(i), d)


# ── Input Sheet Layout Detection ─────────────────────────────────────────
# Mirrors the bridge's _detect_layout() pattern so the output writer finds
# section rows dynamically regardless of how many floor plan slots exist.
# Uses openpyxl read_only=True which is safe (no writes, no corruption).

def _scan_input_row(ws, col_letter: str, marker: str, start: int = 1, end: int = 300):
    """Scan a column for a cell containing marker (case-insensitive substring)."""
    marker_lower = marker.lower()
    for row in range(start, end + 1):
        val = ws[f"{col_letter}{row}"].value
        if val is not None and marker_lower in str(val).lower():
            return row
    return None


def _detect_input_layout(ws) -> Dict[str, int]:
    """Detect the Input sheet's section boundaries by scanning for headers.

    Returns a dict of section_name → row_number so the writer can target
    the correct rows regardless of how many floor plan slots exist.
    """
    layout: Dict[str, int] = {}

    # Floor plans — data starts below the "Floor Plans" header
    fp_header = (
        _scan_input_row(ws, "A", "Floor Plan Mix") or
        _scan_input_row(ws, "B", "Floor Plan") or
        _scan_input_row(ws, "A", "Unit Mix") or
        _scan_input_row(ws, "B", "RENT ASSUMPTIONS", start=40, end=60)
    )
    if fp_header:
        # First data row: scan for a non-empty B cell below header
        fp_start = fp_header + 1
        for r in range(fp_header + 1, fp_header + 5):
            val = ws.cell(row=r, column=2).value
            if val is not None and str(val).strip():
                fp_start = r
                break
        layout["fp_start"] = fp_start
    else:
        layout["fp_start"] = 53  # fallback

    # Total/Average row (marks end of floor plan area)
    total_row = _scan_input_row(ws, "B", "Total/Average", start=layout["fp_start"], end=layout["fp_start"] + 70)
    if total_row:
        layout["fp_end"] = total_row  # exclusive — don't write here
        layout["fp_capacity"] = total_row - layout["fp_start"]  # max floor plans
    else:
        layout["fp_end"] = layout["fp_start"] + 11
        layout["fp_capacity"] = 11

    # Market Rent Growth
    rg = (_scan_input_row(ws, "B", "MARKET RENT GROWTH") or
          _scan_input_row(ws, "A", "MARKET RENT GROWTH") or
          _scan_input_row(ws, "B", "Rent Growth"))
    layout["rent_growth_header"] = rg or (layout["fp_end"] + 10)
    # Data row is typically 3 rows below header
    layout["rent_growth_data"] = layout["rent_growth_header"] + 3

    # Rental Loss Factors
    rlf = (_scan_input_row(ws, "B", "RENTAL LOSS FACTORS") or
           _scan_input_row(ws, "A", "RENTAL LOSS FACTORS") or
           _scan_input_row(ws, "B", "Rental Loss"))
    layout["loss_factors_header"] = rlf or (layout["rent_growth_header"] + 31)
    # Loss to Lease is typically +4, Vacancy is typically +9 from header
    layout["ltl_row"] = layout["loss_factors_header"] + 4
    layout["vacancy_row"] = layout["loss_factors_header"] + 9

    # Operating Expenses
    opex_hdr = (_scan_input_row(ws, "B", "ANNUAL OPERATING EXPENSES") or
                _scan_input_row(ws, "A", "Operating Expenses", start=100) or
                _scan_input_row(ws, "B", "Operating Expenses", start=100))
    if opex_hdr:
        # Data starts after header rows (typically +3)
        opex_start = opex_hdr + 3
        for r in range(opex_hdr + 1, opex_hdr + 6):
            val = ws.cell(row=r, column=2).value
            if val is not None and str(val).strip() and "category" not in str(val).lower():
                opex_start = r
                break
        layout["opex_start"] = opex_start
    else:
        layout["opex_start"] = 147

    # Inflation
    infl = (_scan_input_row(ws, "B", "General Inflation") or
            _scan_input_row(ws, "A", "General Inflation"))
    layout["inflation_row"] = infl or (layout["opex_start"] + 22)

    # Replacement Reserves
    res = (_scan_input_row(ws, "B", "REPLACEMENT RESERVES") or
           _scan_input_row(ws, "A", "Replacement Reserves"))
    layout["reserves_header"] = res or (layout["inflation_row"] + 18)
    layout["reserves_data"] = layout["reserves_header"] + 2

    # Property-Wide CapEx
    capex_hdr = (_scan_input_row(ws, "B", "PROPERTY-WIDE CAPITAL", start=150) or
                 _scan_input_row(ws, "A", "PROPERTY-WIDE CAPITAL", start=150) or
                 _scan_input_row(ws, "B", "Capital Expenditures", start=150))
    if capex_hdr:
        # Data typically starts 4 rows below header
        capex_start = capex_hdr + 4
        for r in range(capex_hdr + 1, capex_hdr + 6):
            val = ws.cell(row=r, column=2).value
            if val is not None and str(val).strip() and "description" not in str(val).lower():
                capex_start = r
                break
        layout["capex_start"] = capex_start
    else:
        layout["capex_start"] = 198

    # Debt Assumptions
    debt_hdr = (_scan_input_row(ws, "B", "DEBT ASSUMPTIONS") or
                _scan_input_row(ws, "A", "DEBT ASSUMPTIONS"))
    layout["debt_header"] = debt_hdr or (layout["capex_start"] + 11)
    # Row offsets within debt section (relative to header)
    layout["debt_orig_month"] = layout["debt_header"] + 4   # Origination Month
    layout["debt_term"] = layout["debt_header"] + 5          # Term
    layout["debt_amount"] = layout["debt_header"] + 10       # Loan Amount
    layout["debt_amort"] = layout["debt_header"] + 14        # Amort Period
    layout["debt_io"] = layout["debt_header"] + 15           # IO Period
    layout["debt_rate"] = layout["debt_header"] + 23         # Interest Rate

    # Partnership Assumptions
    part_hdr = (_scan_input_row(ws, "B", "PARTNERSHIP ASSUMPTIONS") or
                _scan_input_row(ws, "A", "PARTNERSHIP ASSUMPTIONS"))
    layout["partnership_header"] = part_hdr or (layout["debt_header"] + 31)
    # Equity Partner / Sponsor rows (typically +6/+7 from header)
    ep = _scan_input_row(ws, "B", "Equity Partner", start=layout["partnership_header"],
                         end=layout["partnership_header"] + 15)
    sp = _scan_input_row(ws, "B", "Sponsor", start=layout["partnership_header"],
                         end=layout["partnership_header"] + 15)
    layout["equity_partner_row"] = ep or (layout["partnership_header"] + 6)
    layout["sponsor_row"] = sp or (layout["partnership_header"] + 7)
    # Promote tiers start at equity_partner_row - 1
    layout["promote_tier_start"] = layout["equity_partner_row"] - 1

    # Fee rows (typically 8-12 rows below sponsor)
    acq_fee = _scan_input_row(ws, "B", "Acquisition Fee", start=layout["sponsor_row"],
                              end=layout["sponsor_row"] + 15)
    layout["acq_fee_row"] = acq_fee or (layout["sponsor_row"] + 7)
    layout["am_fee_row"] = layout["acq_fee_row"] + 2
    layout["disp_fee_row"] = layout["acq_fee_row"] + 3
    layout["partnership_exp_row"] = layout["acq_fee_row"] + 4

    return layout


def write_input_sheet(writer: XlsmWriter, results: Dict[str, Any], inputs: Dict[str, Any],
                      template_path: Path = None):
    """Write deal assumptions to the Input sheet.

    Uses dynamic layout detection (via openpyxl read_only) to find section rows,
    then writes via XlsmWriter so the writer works correctly regardless of how
    many floor plan slots the template has.
    """
    sheet = "Input"

    # Detect layout from the template using openpyxl read_only (safe, no saves)
    if template_path is None:
        template_path = Path("excel/rediq_clone.xlsm")
    wb_ro = openpyxl.load_workbook(template_path, read_only=True, data_only=True)
    try:
        ws_ro = wb_ro["Input"]
    except KeyError:
        wb_ro.close()
        return
    layout = _detect_input_layout(ws_ro)
    wb_ro.close()

    meta = inputs.get("metadata", {})
    purchase = inputs.get("purchase_assumptions", {})
    exit_a = inputs.get("exit_assumptions", {})
    tg = inputs.get("time_grid", {})
    cohorts = inputs.get("unit_cohorts", [])
    fund = inputs.get("fund_assumptions", {})

    # --- General Info (rows 10-12 are fixed across all templates) ---
    deal_id = meta.get("deal_id", "")
    _write_cell(writer, sheet, 10, 5, deal_id)  # E10: Deal Name

    total_units = sum(c.get("unit_count", 0) for c in cohorts)
    _write_cell(writer, sheet, 12, 5, total_units)  # E12: # Units

    start_date = tg.get("analysis_start_date", "")
    end_date = tg.get("analysis_end_date", "")
    if start_date:
        try:
            start_dt = datetime.strptime(str(start_date)[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            start_dt = start_date
        _write_cell(writer, sheet, 10, 13, start_dt)  # M10: AnalysisStartDate
    if end_date:
        try:
            end_dt = datetime.strptime(str(end_date)[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            end_dt = end_date
        _write_cell(writer, sheet, 12, 13, end_dt)  # M12: DateOfExit

    # --- Acquisition (rows 32-37 are fixed) ---
    pp = purchase.get("purchase_price")
    if pp:
        _write_cell(writer, sheet, 32, 6, pp)  # F32: PurchasePrice

    cc = purchase.get("closing_costs", 0)
    if cc:
        _write_cell(writer, sheet, 36, 7, cc)  # G36: Total Closing Costs

    # --- Disposition (same rows as acquisition, right side) ---
    if exit_a:
        exit_cap = exit_a.get("exit_cap_rate")
        if exit_cap is not None:
            _write_cell(writer, sheet, 33, 13, exit_cap)  # M33: ExitCapRate

        sale_cost_pct = exit_a.get("sale_cost_percent")
        if sale_cost_pct is not None:
            _write_cell(writer, sheet, 37, 11, sale_cost_pct)  # K37: SalesCommission

    # --- Floor Plans (dynamic capacity) ---
    fp_start = layout["fp_start"]
    fp_capacity = layout["fp_capacity"]

    if len(cohorts) > fp_capacity:
        import warnings
        warnings.warn(
            f"Deal has {len(cohorts)} floor plans but template has {fp_capacity} slots; "
            f"truncating to {fp_capacity}. Regenerate template with more slots for full output."
        )

    for idx, cohort in enumerate(cohorts):
        if idx >= fp_capacity:
            break
        row = fp_start + idx
        _write_cell(writer, sheet, row, 2, cohort.get("unit_type", cohort.get("cohort_id", "")))  # B: Name
        if "sqft" in cohort:
            _write_cell(writer, sheet, row, 6, cohort["sqft"])  # F: Size
        _write_cell(writer, sheet, row, 8, cohort.get("unit_count", 0))  # H: Units
        _write_cell(writer, sheet, row, 12, cohort.get("initial_inplace_rent", 0))  # L: Market Rent

    # --- Market Rent Growth (dynamic row) ---
    rent_growth_row = layout["rent_growth_data"]
    mr_curve = inputs.get("market_rent_curve", [])
    if mr_curve:
        first_cohort_id = cohorts[0]["cohort_id"] if cohorts else ""
        first_curve = [r for r in mr_curve if r.get("cohort_id") == first_cohort_id]
        if len(first_curve) >= 2:
            for j in range(1, min(len(first_curve), 11)):
                prev_rent = first_curve[j - 1].get("market_rent", 0)
                curr_rent = first_curve[j].get("market_rent", 0)
                if prev_rent > 0:
                    growth = (curr_rent / prev_rent) - 1
                    _write_cell(writer, sheet, rent_growth_row, 7 + (j - 1), round(growth, 4))

    # --- Loss Factors (dynamic rows) ---
    ltl_row = layout["ltl_row"]
    vacancy_row = layout["vacancy_row"]
    vac_curve = inputs.get("physical_vacancy_curve", [])
    ltl_curve = inputs.get("loss_to_lease", [])

    if vac_curve:
        first_vac = [r for r in vac_curve if r.get("cohort_id") == (cohorts[0]["cohort_id"] if cohorts else "")]
        if not first_vac:
            first_vac = vac_curve
        for j, seg in enumerate(first_vac[:10]):
            _write_cell(writer, sheet, vacancy_row, 7 + j * 2, seg.get("vacancy_rate", 0))

    if ltl_curve:
        first_ltl = [r for r in ltl_curve if r.get("cohort_id") == (cohorts[0]["cohort_id"] if cohorts else "")]
        if not first_ltl:
            first_ltl = ltl_curve
        for j, seg in enumerate(first_ltl[:10]):
            _write_cell(writer, sheet, ltl_row, 7 + j * 2, seg.get("ltl_percent", 0))

    # --- OpEx Categories (dynamic start) ---
    opex_start = layout["opex_start"]
    opex_table = inputs.get("opex_table", [])
    for idx, cat in enumerate(opex_table):
        if idx >= 20:
            break
        row = opex_start + idx
        _write_cell(writer, sheet, row, 2, cat.get("category_name", ""))  # B: Label
        _write_cell(writer, sheet, row, 5, cat.get("base_value", 0))  # E: Pro Forma
        calc_type = cat.get("calculation_type", "fixed_annual")
        if calc_type == "percent_egr":
            _write_cell(writer, sheet, row, 6, "% of EGR")
        else:
            _write_cell(writer, sheet, row, 6, cat.get("unit_type", "$/unit"))

    # General inflation rate (dynamic row)
    inflation_row = layout["inflation_row"]
    if opex_table:
        growth_rates = [c.get("growth_rate", 0) for c in opex_table if c.get("growth_rate")]
        if growth_rates:
            avg_infl = sum(growth_rates) / len(growth_rates)
            _write_cell(writer, sheet, inflation_row, 7, round(avg_infl, 4))

    # --- Replacement Reserves (dynamic row) ---
    reserves_row = layout["reserves_data"]
    reserves = inputs.get("replacement_reserves", [])
    if reserves:
        annual = reserves[0].get("annual_amount", 0)
        if annual and total_units > 0:
            _write_cell(writer, sheet, reserves_row, 7, annual / total_units)

    # --- CapEx Schedule (dynamic start) ---
    capex_start = layout["capex_start"]
    capex_schedule = inputs.get("capex_schedule", [])
    for idx, item in enumerate(capex_schedule):
        if idx >= 7:
            break
        row = capex_start + idx
        _write_cell(writer, sheet, row, 2, item.get("category", ""))
        _write_cell(writer, sheet, row, 7, item.get("total_amount", 0))

    # --- Debt Terms (dynamic rows) ---
    debt = inputs.get("debt_terms", {})
    if debt:
        _write_cell(writer, sheet, layout["debt_amount"], 8, debt.get("commitment", 0))
        _write_cell(writer, sheet, layout["debt_rate"], 9, debt.get("rate", 0))
        _write_cell(writer, sheet, layout["debt_amort"], 9, debt.get("amort_years", 30))
        _write_cell(writer, sheet, layout["debt_io"], 9, debt.get("io_months", 0))
        _write_cell(writer, sheet, layout["debt_term"], 9, debt.get("term_months", 120))

    additional = inputs.get("additional_debt_terms", [])
    for loan_idx, extra in enumerate(additional[:2]):
        base_col = 10 + loan_idx * 2
        amt_col = base_col
        detail_col = base_col + 1
        _write_cell(writer, sheet, layout["debt_amount"], amt_col, extra.get("commitment", 0))
        _write_cell(writer, sheet, layout["debt_rate"], detail_col, extra.get("rate", 0))
        _write_cell(writer, sheet, layout["debt_amort"], detail_col, extra.get("amort_years", 30))
        _write_cell(writer, sheet, layout["debt_io"], detail_col, extra.get("io_months", 0))
        _write_cell(writer, sheet, layout["debt_term"], detail_col, extra.get("term_months", 120))
        start_month = extra.get("loan_start_month")
        if start_month:
            _write_cell(writer, sheet, layout["debt_orig_month"], detail_col, start_month)

    # --- Partnership / Fund Assumptions (dynamic rows) ---
    if fund:
        sponsor_pct = fund.get("sponsor_equity_pct", 0)
        lp_pct = fund.get("lp_equity_pct", 0)
        if sponsor_pct == 0 and lp_pct > 0:
            sponsor_pct = round(1.0 - lp_pct, 6)

        _write_cell(writer, sheet, layout["equity_partner_row"], 6, lp_pct)
        _write_cell(writer, sheet, layout["sponsor_row"], 6, sponsor_pct)

        promote_splits = fund.get("promote_splits", [])
        for t_idx, tier in enumerate(promote_splits[:4]):
            row = layout["promote_tier_start"] + t_idx
            _write_cell(writer, sheet, row, 10, tier.get("hurdle_irr", 0))
            _write_cell(writer, sheet, row, 12, tier.get("lp_share", 0))
            _write_cell(writer, sheet, row, 13, tier.get("gp_share", 0))

        _write_cell(writer, sheet, layout["acq_fee_row"], 11, fund.get("acquisition_fee_pct", 0))
        _write_cell(writer, sheet, layout["am_fee_row"], 11, fund.get("asset_management_fee_pct", 0))
        _write_cell(writer, sheet, layout["disp_fee_row"], 11, fund.get("disposition_fee_pct", 0))
        _write_cell(writer, sheet, layout["partnership_exp_row"], 12, fund.get("annual_partnership_expenses", 0))
        _write_cell(writer, sheet, layout["partnership_exp_row"], 6, fund.get("partnership_closing_costs", 0))


# ── Waterfall Sheet Writer ───────────────────────────────────────────────

def _month_to_wf_col(month_index: int) -> int:
    """0-based month index → Waterfall sheet column.  Month 0 = col R (18)."""
    return 18 + month_index


def write_waterfall_sheet(writer: XlsmWriter, results: Dict[str, Any], inputs: Dict[str, Any]):
    """Write fund waterfall tier data to the Waterfall sheet."""
    sheet = "Waterfall"

    fund_wf = results.get("fund_waterfall", {})
    if not fund_wf:
        return

    wf_promote = fund_wf.get("promote", {})
    tier_history = wf_promote.get("tier_history", [])
    wf_by_year = fund_wf.get("by_year", [])
    wf_closing = fund_wf.get("closing_costs", {})
    wf_summary = fund_wf.get("summary", {})

    time_grid = results.get("time_grid", {})
    months = time_grid.get("months", [])
    analysis_start = time_grid.get("analysis_start", "")
    if analysis_start:
        start_year = int(analysis_start[:4])
        start_mo = int(analysis_start[5:7])
    elif months:
        start_year = int(months[0][:4])
        start_mo = int(months[0][5:7])
    else:
        return

    # Waterfall structure: 4 tiers
    # Tier I: rows 9-22, Tier II: rows 26-40, Tier III: rows 44-59, Tier IV: rows 63-69
    tier_base_rows = [9, 26, 44, 63]

    fund = inputs.get("fund_assumptions", {})
    promote_splits = fund.get("promote_splits", [])

    # Write hurdle rates and splits to header area
    for t_idx, tier in enumerate(promote_splits[:4]):
        if t_idx < len(tier_base_rows):
            base = tier_base_rows[t_idx]
            hurdle = tier.get("hurdle_irr", 0)
            _write_cell(writer, sheet, base, 5, hurdle)  # E: Hurdle rate

    # If tier_history is available, write detailed monthly data
    if tier_history:
        for entry in tier_history:
            tier_idx = entry.get("tier_index", 0)
            month_idx = entry.get("month_index", 0)
            if tier_idx >= len(tier_base_rows) or month_idx >= 120:
                continue

            base_row = tier_base_rows[tier_idx]
            col = _month_to_wf_col(month_idx)

            _write_cell_if(writer, sheet, base_row + 1, col, entry.get("beginning_balance"))
            _write_cell_if(writer, sheet, base_row + 2, col, entry.get("equity_funding"))
            _write_cell_if(writer, sheet, base_row + 3, col, entry.get("preferred_return"))
            _write_cell_if(writer, sheet, base_row + 4, col, entry.get("distribution"))
            _write_cell_if(writer, sheet, base_row + 5, col, entry.get("ending_balance"))
    else:
        # Write annual summary data spread to the right annual columns
        for i, yr_data in enumerate(wf_by_year):
            if i >= 10:
                break
            # Annual columns in Waterfall: C-P (3-16)
            col = 3 + i

            # Write CF before promote as Tier I distribution proxy
            cf_bp = yr_data.get("cash_flow_before_promote", 0)
            promote = yr_data.get("promote_payment", 0)
            cf_tp = yr_data.get("cash_flow_to_partnership", 0)

            # Tier I: write distribution
            _write_cell_if(writer, sheet, tier_base_rows[0] + 4, col, cf_tp)
            # Promote to appropriate tier
            if promote and promote != 0:
                _write_cell_if(writer, sheet, tier_base_rows[0] + 7, col, promote)


def write_waterfall_summary(writer: XlsmWriter, results: Dict[str, Any]):
    """Write waterfall summary metrics to rows 2-7 of the Waterfall sheet."""
    sheet = "Waterfall"

    # Guard: no fund_waterfall data → nothing to write
    fund_wf = results.get("fund_waterfall")
    if not fund_wf:
        return

    summary = fund_wf.get("summary", {})
    if not summary:
        return

    # Guard: check sheet exists
    try:
        writer._get_sheet_xml(sheet)
    except KeyError:
        return

    # Row 2: Header
    writer.set_cell_value(sheet, 2, 3, "WATERFALL SUMMARY")

    # Row 3: Total LP / GP / All
    total_lp = summary.get("total_lp_distributions", 0)
    total_gp = summary.get("total_sponsor_distributions", 0)
    total_distributed = total_lp + total_gp

    writer.set_cell_value(sheet, 3, 2, "Total LP / GP / All")
    writer.set_cell_value(sheet, 3, 3, total_lp)
    writer.set_cell_value(sheet, 3, 4, total_gp)
    writer.set_cell_value(sheet, 3, 5, total_distributed)

    # Row 4: IRR / EM
    partnership_irr = summary.get("partnership_irr")
    partnership_em = summary.get("partnership_equity_multiple")

    writer.set_cell_value(sheet, 4, 2, "IRR / EM")
    if partnership_irr is not None:
        writer.set_cell_value(sheet, 4, 3, partnership_irr)
    if partnership_em is not None:
        writer.set_cell_value(sheet, 4, 4, partnership_em)

    # Row 5: Coinvest / Clawback
    gp_coinvest = summary.get("gp_coinvest_pct")
    promote = fund_wf.get("promote", {})
    clawback = promote.get("clawback_amount")

    writer.set_cell_value(sheet, 5, 2, "Coinvest / Clawback")
    if gp_coinvest is not None:
        writer.set_cell_value(sheet, 5, 3, gp_coinvest)
    if clawback is not None:
        writer.set_cell_value(sheet, 5, 4, clawback)

    # Row 7: Tier Breakdown header
    writer.set_cell_value(sheet, 7, 3, "TIER BREAKDOWN")

    # Named ranges
    writer.define_named_range("WF_Total_LP", sheet, "$C$3")
    writer.define_named_range("WF_Total_GP", sheet, "$D$3")
    writer.define_named_range("WF_Partnership_IRR", sheet, "$C$4")
    writer.define_named_range("WF_Partnership_EM", sheet, "$D$4")

    # Conditional formatting: negative_red on tier distribution rows
    tier_dist_rows = [13, 30, 48, 67]
    for row in tier_dist_rows:
        range_str = f"C{row}:P{row}"
        writer.add_conditional_format(sheet, range_str, "negative_red")


# ── Summary & GenValidation Writer ───────────────────────────────────────

def write_summary_metrics(writer: XlsmWriter, results: Dict[str, Any], inputs: Optional[Dict[str, Any]] = None):
    """Write key metrics to GenValidation sheet."""
    sheet = "GenValidation"
    metrics = results.get("metrics", {})

    irr = metrics.get("irr", {})
    em = metrics.get("equity_multiple", {})

    unlevered_em = em.get("unlevered_em")
    levered_em = em.get("levered_em")

    fund_wf = results.get("fund_waterfall", {})
    wf_summary = fund_wf.get("summary", {})
    partnership_em = wf_summary.get("partnership_equity_multiple")

    # TotalDealUnits (GenValidation A2)
    if inputs:
        cohorts = inputs.get("unit_cohorts", [])
        total_units = sum(c.get("unit_count", 0) for c in cohorts)
        if total_units > 0:
            _write_cell(writer, sheet, 2, 1, total_units)


def write_sensitivity_tables(writer: XlsmWriter, results: Dict[str, Any]):
    """Write sensitivity analysis results to the Input sheet sensitivity area."""
    sensitivity = results.get("sensitivity")
    if not sensitivity:
        return

    sheet = "Input"
    sensitivity_results = sensitivity.get("sensitivity_results", {})

    for metric_name, metric_data in sensitivity_results.items():
        matrix = metric_data.get("matrix", [])
        if not matrix:
            continue

        if "levered_irr" in metric_name.lower():
            start_row, start_col = 267, 27
        elif "unlevered_irr" in metric_name.lower():
            start_row, start_col = 267, 18
        elif "levered" in metric_name.lower() and "mult" in metric_name.lower():
            start_row, start_col = 278, 27
        elif "unlevered" in metric_name.lower() and "mult" in metric_name.lower():
            start_row, start_col = 278, 18
        else:
            continue

        for i, row_data in enumerate(matrix):
            for j, value in enumerate(row_data):
                if value is not None:
                    _write_cell(writer, sheet, start_row + i, start_col + j, value)


# ── Scenario Dashboard ────────────────────────────────────────────────────

_SCENARIO_METRIC_ROWS = [
    ("Levered IRR", "levered_irr"),
    ("Unlevered IRR", "unlevered_irr"),
    ("Levered EM", "levered_em"),
    ("Unlevered EM", "unlevered_em"),
    ("Partnership IRR", "partnership_irr"),
    ("Partnership EM", "partnership_em"),
    ("NOI Year 1", "noi_year_1"),
    ("NOI Exit Year", "noi_exit_year"),
    ("Average DSCR", "average_dscr"),
    ("Minimum DSCR", "minimum_dscr"),
    ("Going-In Cap", "going_in_cap"),
    ("Cash-on-Cash Y1", "cash_on_cash_y1"),
]

_DASHBOARD_SHEET = "Scenario Dashboard"

_DASH_TITLE_ROW = 1
_DASH_HEADER_ROW = 3
_DASH_ASSUMPTIONS_START = 4
_DASH_RETURNS_LABEL_ROW = 11
_DASH_RETURNS_START = 12
_DASH_TORNADO_HEADER_ROW = 22
_DASH_TORNADO_LABELS_ROW = 23
_DASH_TORNADO_DATA_START = 24

_DASH_LABEL_COL = 1
_DASH_BULL_COL = 2
_DASH_BASE_COL = 3
_DASH_BEAR_COL = 4
_DASH_DELTA_BULL_COL = 5
_DASH_DELTA_BEAR_COL = 6

_DASHBOARD_ASSUMPTIONS = [
    ("Rent Growth", "rent_growth"),
    ("Exit Cap Rate", "exit_cap_rate"),
    ("Avg Vacancy", "avg_vacancy"),
    ("OpEx Growth", "opex_growth"),
    ("Rate", "rate"),
]


def write_scenario_dashboard(
    writer,
    scenario_results: Optional[Dict[str, Any]],
    tornado_results: Optional[Dict[str, Any]],
) -> None:
    """Write scenario comparison + tornado sensitivity to the Scenario Dashboard sheet."""
    sheet = _DASHBOARD_SHEET

    # Verify sheet exists
    try:
        writer._get_sheet_xml(sheet)
    except KeyError:
        return

    # Title
    writer.set_cell_value(sheet, _DASH_TITLE_ROW, _DASH_LABEL_COL, "Scenario Dashboard")

    # ── Section 1: Bull/Base/Bear Comparison ──
    if scenario_results:
        comparison = scenario_results.get("comparison", {})

        # Headers
        writer.set_cell_value(sheet, _DASH_HEADER_ROW, _DASH_BULL_COL, "Bull")
        writer.set_cell_value(sheet, _DASH_HEADER_ROW, _DASH_BASE_COL, "Base")
        writer.set_cell_value(sheet, _DASH_HEADER_ROW, _DASH_BEAR_COL, "Bear")
        writer.set_cell_value(sheet, _DASH_HEADER_ROW, _DASH_DELTA_BULL_COL, "\u0394 Bull")
        writer.set_cell_value(sheet, _DASH_HEADER_ROW, _DASH_DELTA_BEAR_COL, "\u0394 Bear")

        # Assumptions section
        writer.set_cell_value(sheet, _DASH_ASSUMPTIONS_START, _DASH_LABEL_COL, "ASSUMPTIONS")
        row = _DASH_ASSUMPTIONS_START + 1
        for label, key in _DASHBOARD_ASSUMPTIONS:
            writer.set_cell_value(sheet, row, _DASH_LABEL_COL, label)
            base_val = comparison.get("base", {}).get("assumptions", {}).get(key)
            for scenario, col in [("bull", _DASH_BULL_COL), ("base", _DASH_BASE_COL), ("bear", _DASH_BEAR_COL)]:
                val = comparison.get(scenario, {}).get("assumptions", {}).get(key)
                if val is not None:
                    writer.set_cell_value(sheet, row, col, val)
            # Deltas
            bull_val = comparison.get("bull", {}).get("assumptions", {}).get(key)
            bear_val = comparison.get("bear", {}).get("assumptions", {}).get(key)
            if bull_val is not None and base_val is not None:
                writer.set_cell_value(sheet, row, _DASH_DELTA_BULL_COL, bull_val - base_val)
            if bear_val is not None and base_val is not None:
                writer.set_cell_value(sheet, row, _DASH_DELTA_BEAR_COL, bear_val - base_val)
            row += 1

        # Returns section
        writer.set_cell_value(sheet, _DASH_RETURNS_LABEL_ROW, _DASH_LABEL_COL, "RETURNS")
        row = _DASH_RETURNS_START
        for label, metric_key in _SCENARIO_METRIC_ROWS:
            writer.set_cell_value(sheet, row, _DASH_LABEL_COL, label)
            base_val = comparison.get("base", {}).get("metrics", {}).get(metric_key)
            for scenario, col in [("bull", _DASH_BULL_COL), ("base", _DASH_BASE_COL), ("bear", _DASH_BEAR_COL)]:
                val = comparison.get(scenario, {}).get("metrics", {}).get(metric_key)
                if val is not None:
                    writer.set_cell_value(sheet, row, col, val)
            # Deltas
            bull_val = comparison.get("bull", {}).get("metrics", {}).get(metric_key)
            bear_val = comparison.get("bear", {}).get("metrics", {}).get(metric_key)
            if bull_val is not None and base_val is not None:
                writer.set_cell_value(sheet, row, _DASH_DELTA_BULL_COL, bull_val - base_val)
            if bear_val is not None and base_val is not None:
                writer.set_cell_value(sheet, row, _DASH_DELTA_BEAR_COL, bear_val - base_val)
            row += 1

        # Conditional formatting on delta columns
        last_data_row = _DASH_RETURNS_START + len(_SCENARIO_METRIC_ROWS) - 1
        writer.add_conditional_format(sheet, f"E5:E{last_data_row}", "threshold",
            thresholds=[
                {"operator": "greaterThan", "value": 0, "color": "FF008000"},
                {"operator": "lessThan", "value": 0, "color": "FFFF0000"},
            ]
        )
        writer.add_conditional_format(sheet, f"F5:F{last_data_row}", "threshold",
            thresholds=[
                {"operator": "greaterThan", "value": 0, "color": "FF008000"},
                {"operator": "lessThan", "value": 0, "color": "FFFF0000"},
            ]
        )

    # ── Section 2: Tornado Sensitivity Table ──
    if tornado_results:
        variables = tornado_results.get("variables", [])
        writer.set_cell_value(sheet, _DASH_TORNADO_HEADER_ROW, _DASH_LABEL_COL,
                              "IRR Sensitivity \u2014 Single-Variable Shocks")
        writer.set_cell_value(sheet, _DASH_TORNADO_LABELS_ROW, 1, "Variable")
        writer.set_cell_value(sheet, _DASH_TORNADO_LABELS_ROW, 2, "Favorable")
        writer.set_cell_value(sheet, _DASH_TORNADO_LABELS_ROW, 3, "Adverse")
        writer.set_cell_value(sheet, _DASH_TORNADO_LABELS_ROW, 4, "Base")
        writer.set_cell_value(sheet, _DASH_TORNADO_LABELS_ROW, 5, "Swing (bps)")

        row = _DASH_TORNADO_DATA_START
        for v in variables:
            shock_label = f"{v['label']} (\u00b1{v['shock_bps']}bps)"
            writer.set_cell_value(sheet, row, 1, shock_label)
            writer.set_cell_value(sheet, row, 2, v["irr_up"])
            writer.set_cell_value(sheet, row, 3, v["irr_down"])
            writer.set_cell_value(sheet, row, 4, v["irr_base"])
            writer.set_cell_value(sheet, row, 5, round(v["irr_impact"] * 10000))
            row += 1

        # Gradient on swing column
        if variables:
            last_tornado_row = _DASH_TORNADO_DATA_START + len(variables) - 1
            writer.add_conditional_format(sheet,
                f"E{_DASH_TORNADO_DATA_START}:E{last_tornado_row}", "gradient",
                min_color="FFFF0000", max_color="FF008000"
            )

    # Freeze pane and print area
    writer.set_freeze_pane(sheet, 4, 1)
    writer.set_print_area(sheet, 1, 1, 30, 6)
    writer.set_page_setup(sheet, orientation="landscape", fit_to_width=1)


# ── Main API ─────────────────────────────────────────────────────────────

def write_freeze_panes(writer: XlsmWriter):
    """Apply freeze panes to key tabs for easy scrolling.

    - CF Calculations: freeze row headers (col A-D) and top rows
    - Input: freeze section headers
    - Waterfall: freeze year labels
    """
    freeze_config = {
        CF_SHEET: (5, 5),     # Freeze rows 1-4 (headers) + cols A-D (labels)
        "Input": (3, 1),      # Freeze rows 1-2 (header)
        "Waterfall": (3, 4),  # Freeze rows 1-2 + cols A-C (tier labels)
    }
    for sheet, (row, col) in freeze_config.items():
        try:
            writer.set_freeze_pane(sheet, row, col)
        except (KeyError, ValueError):
            pass  # Sheet may not exist


def write_deal_summary_tab(
    writer: XlsmWriter,
    results: Dict[str, Any],
    inputs: Dict[str, Any],
):
    """Write a one-page Deal Summary tab — the LP-friendly snapshot.

    Populates the "Summary" sheet (if it exists in the template) with:
    - Deal Overview: name, location, units, purchase price, $/unit
    - Investment Returns: IRR (unlevered/levered/partnership), EM, DSCR
    - Capital Structure: debt/equity breakdown, LTV, rate
    - Annual NOI Summary: Year 1-5 NOI
    - LP/GP Distribution Summary
    """
    SHEET = "Summary"
    try:
        # Test if sheet exists
        writer._get_sheet_xml(SHEET)
    except KeyError:
        return  # Summary sheet not in template — skip silently

    def w(row, col, val):
        _write_cell(writer, SHEET, row, col, val)

    # ── Section 1: Deal Overview ──
    w(1, 1, "DEAL SUMMARY")
    from engine.brand import BrandConfig
    _brand = BrandConfig.from_inputs(inputs)
    w(1, 5, _brand.company_name)

    metadata = inputs.get("metadata", {})
    purchase = inputs.get("purchase_assumptions", {})
    exit_a = inputs.get("exit_assumptions", {})
    fund = inputs.get("fund_assumptions", {})
    debt = inputs.get("debt_terms", {})

    total_units = sum(c.get("unit_count", 0) for c in inputs.get("unit_cohorts", []))
    pp = purchase.get("purchase_price", 0)
    per_unit = pp / total_units if total_units > 0 else 0

    row = 3
    w(row, 1, "Property")
    w(row, 2, metadata.get("deal_id", ""))
    row += 1
    w(row, 1, "Date")
    w(row, 2, metadata.get("as_of_date", ""))
    row += 1
    w(row, 1, "Total Units")
    w(row, 2, total_units)
    row += 1
    w(row, 1, "Purchase Price")
    w(row, 2, pp)
    row += 1
    w(row, 1, "Price Per Unit")
    w(row, 2, round(per_unit, 0))
    row += 1
    w(row, 1, "Entry Cap Rate")
    metrics = results.get("metrics", {})
    entry_cap = metrics.get("entry_cap_rate")
    if entry_cap is not None:
        w(row, 2, entry_cap)
    row += 1
    w(row, 1, "Exit Cap Rate")
    w(row, 2, exit_a.get("exit_cap_rate", ""))
    row += 1
    w(row, 1, "Hold Period")
    time_info = results.get("time_grid", {})
    years = time_info.get("years", [])
    w(row, 2, f"{len(years)} years")

    # ── Section 2: Investment Returns ──
    row += 2
    w(row, 1, "INVESTMENT RETURNS")
    row += 1

    returns = [
        ("Unlevered IRR", metrics.get("unlevered_irr")),
        ("Unlevered EM", metrics.get("unlevered_equity_multiple")),
        ("Levered IRR", metrics.get("levered_irr")),
        ("Levered EM", metrics.get("levered_equity_multiple")),
    ]

    # Partnership metrics from fund waterfall
    waterfall = results.get("fund_waterfall", {})
    wf_summary = waterfall.get("summary", {})
    returns.extend([
        ("Partnership IRR", wf_summary.get("partnership_irr")),
        ("Partnership EM", wf_summary.get("partnership_equity_multiple")),
    ])

    for label, val in returns:
        w(row, 1, label)
        if val is not None:
            w(row, 2, val)
        row += 1

    # ── Section 3: Capital Structure ──
    row += 1
    w(row, 1, "CAPITAL STRUCTURE")
    row += 1

    commitment = debt.get("commitment", 0)
    equity = purchase.get("equity_contribution", 0)
    ltv = commitment / pp if pp > 0 else 0

    cap_structure = [
        ("Senior Debt", commitment),
        ("Equity", equity),
        ("LTV", ltv),
        ("Interest Rate", debt.get("rate", "")),
        ("Amortization", f"{debt.get('amort_years', '')}yr"),
        ("I/O Period", f"{debt.get('io_months', 0)} months"),
        ("LP / GP Split", f"{fund.get('lp_equity_pct', 0.95):.0%} / {fund.get('sponsor_equity_pct', 0.05):.0%}"),
        ("Preferred Return", fund.get("preferred_return", fund.get("promote_splits", [{}])[0].get("hurdle_irr", ""))),
    ]

    for label, val in cap_structure:
        w(row, 1, label)
        w(row, 2, val)
        row += 1

    # ── Section 4: Annual NOI Summary ──
    row += 1
    w(row, 1, "ANNUAL NOI")
    row += 1

    cf_by_year = results.get("cashflow", {}).get("by_year", [])
    for i, yr in enumerate(cf_by_year[:7]):  # Max 7 years
        label = f"Year {i + 1}"
        noi = yr.get("net_operating_income", 0)
        w(row, 1, label)
        w(row, 2, noi)
        row += 1

    # ── Section 5: Refi Details (if applicable) ──
    refi = results.get("refi")
    if refi:
        row += 1
        w(row, 1, "REFINANCE EVENT")
        row += 1
        refi_fields = [
            ("Trigger Month", refi.get("trigger_month")),
            ("Sizing Method", refi.get("sizing_method", "").upper()),
            ("Stabilized NOI", refi.get("stabilized_noi")),
            ("Perm Loan Amount", refi.get("sized_commitment")),
            ("Bridge Payoff", refi.get("bridge_payoff")),
            ("Refi Costs", refi.get("refi_costs")),
            ("Net Refi Proceeds", refi.get("refi_proceeds")),
        ]
        for label, val in refi_fields:
            w(row, 1, label)
            if val is not None:
                w(row, 2, val)
            row += 1

    # ── Section 6: Feasibility (Wave 5 Task 5.2) ──
    # Embed feasibility verdict + sanity flags + provenance fingerprints so the
    # workbook itself surfaces the same federation-policy classification that
    # `_provenance.json` records (per CONSOLIDATED_FIX_PLAN.md Wave 5).
    provenance = results.get("provenance") or {}
    if provenance:
        row += 1
        w(row, 1, "FEASIBILITY")
        feasibility_section_start = row
        row += 1

        verdict = provenance.get("feasibility_verdict")
        verdict_str = str(verdict).lower() if verdict is not None else ""
        w(row, 1, "Verdict")
        # Cell value is e.g. "Verdict: pass" so it's human-scannable in print.
        w(row, 2, f"Verdict: {verdict_str}")
        verdict_cell_row = row
        row += 1

        # Sanity flags (one per row). If empty, write a placeholder so the
        # absence is explicit (vs. unclear "did the writer skip this section?").
        flags = provenance.get("feasibility_sanity_flags") or []
        w(row, 1, "Sanity Flags")
        if flags:
            w(row, 2, f"{len(flags)} flag(s)")
            row += 1
            for flag in flags:
                w(row, 2, str(flag))
                row += 1
        else:
            w(row, 2, "(none)")
            row += 1

        # Provenance fingerprints — engine version, generation timestamp,
        # short inputs hash for analyst reproducibility.
        engine_version = provenance.get("engine_version")
        if engine_version is not None:
            w(row, 1, "Engine Version")
            w(row, 2, str(engine_version))
            row += 1

        generated_at = provenance.get("generated_at_utc")
        if generated_at is not None:
            w(row, 1, "Generated At (UTC)")
            w(row, 2, str(generated_at))
            row += 1

        inputs_hash = provenance.get("inputs_hash_sha256")
        if inputs_hash:
            w(row, 1, "Inputs Hash (SHA256, first 16)")
            w(row, 2, str(inputs_hash)[:16])
            row += 1

        # Color-code the verdict cell via conditional formatting. The cell text
        # is "Verdict: <value>" so we match against quoted strings. Font color
        # only — XlsmWriter._append_dxf supports font color, not fill.
        try:
            cell_ref = f"{_col_letter(2)}{verdict_cell_row}:{_col_letter(2)}{verdict_cell_row}"
            writer.add_conditional_format(
                SHEET, cell_ref, "threshold",
                thresholds=[
                    {"operator": "equal", "value": '"Verdict: pass"', "color": "FF008000"},      # green
                    {"operator": "equal", "value": '"Verdict: marginal"', "color": "FFFF8C00"},  # orange
                    {"operator": "equal", "value": '"Verdict: fail"', "color": "FFFF0000"},      # red
                ],
            )
        except Exception:
            # Conditional formatting is a presentation enhancement; never fail
            # workbook generation if the dxf/CF write fails on a quirky template.
            pass


def _col_letter(col: int) -> str:
    """1-based column index → Excel column letters (1='A', 27='AA')."""
    s = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        s = chr(65 + rem) + s
    return s


def _validate_provenance_shape(result: Dict[str, Any]) -> None:
    """Defensive check: ensure result has the Wave 1b standardized provenance
    shape before writing the workbook.

    Per CONSOLIDATED_FIX_PLAN.md Wave 5 Task 5.2, rediq_output.py must reject
    results that lack required provenance + headline metric keys. This catches
    schema regressions early (workbook generation is a downstream consumer of
    federation provenance and must fail loud, not silently produce a workbook
    missing verdict / sanity-flag annotations).

    Raises
    ------
    ValueError
        If any required key is missing or the headline metrics are non-numeric.
    """
    if not isinstance(result, dict):
        raise ValueError(
            f"rediq_output requires Wave 1b standardized provenance shape; "
            f"got: {type(result).__name__} (expected dict)"
        )

    missing: List[str] = []

    prov = result.get("provenance")
    if not isinstance(prov, dict):
        missing.append("provenance")
    else:
        if "engine_version" not in prov:
            missing.append("provenance.engine_version")
        if "feasibility_verdict" not in prov:
            missing.append("provenance.feasibility_verdict")
        if "feasibility_sanity_flags" not in prov:
            missing.append("provenance.feasibility_sanity_flags")
        elif not isinstance(prov["feasibility_sanity_flags"], list):
            missing.append("provenance.feasibility_sanity_flags(not_list)")

    metrics = result.get("metrics")
    if not isinstance(metrics, dict):
        missing.append("metrics")
    else:
        irr_block = metrics.get("irr") if isinstance(metrics.get("irr"), dict) else {}
        em_block = metrics.get("equity_multiple") if isinstance(metrics.get("equity_multiple"), dict) else {}
        dscr_block = metrics.get("dscr") if isinstance(metrics.get("dscr"), dict) else {}

        levered_irr = irr_block.get("levered_irr") if isinstance(irr_block, dict) else None
        levered_em = em_block.get("levered_em") if isinstance(em_block, dict) else None
        # Engine emits ``minimum_dscr``; CONSOLIDATED_FIX_PLAN.md spells it
        # ``minimum``. Accept either to honor both contracts.
        if isinstance(dscr_block, dict):
            min_dscr = dscr_block.get("minimum_dscr", dscr_block.get("minimum"))
        else:
            min_dscr = None

        for label, val in [
            ("metrics.irr.levered_irr", levered_irr),
            ("metrics.equity_multiple.levered_em", levered_em),
            ("metrics.dscr.minimum", min_dscr),
        ]:
            if val is None:
                missing.append(label)
            elif isinstance(val, bool) or not isinstance(val, (int, float)):
                missing.append(f"{label}(not_numeric)")

    if missing:
        keys_present = sorted(result.keys()) if isinstance(result, dict) else []
        raise ValueError(
            f"rediq_output requires Wave 1b standardized provenance shape; "
            f"got: {keys_present}; missing or invalid: {missing}"
        )


def write_print_setup(writer: XlsmWriter, inputs: Dict[str, Any], brand_config=None):
    """Set print areas, page orientation, and headers/footers on key tabs.

    Makes the workbook screenshot-ready and print-friendly:
    - CF Calculations: landscape, fit to 1 page wide
    - Input: portrait, fit to 1 page wide
    - Waterfall: landscape
    - Summary: portrait, fit to 1 page

    When brand_config has a partner, header reads:
    "Prepared by [Company] for [Partner Name]"
    """
    from engine.brand import BrandConfig

    if brand_config is None:
        brand_config = BrandConfig.from_inputs(inputs)

    deal_name = inputs.get("metadata", {}).get("deal_id", "")
    header = f"&L&B{deal_name}&R{brand_config.header_text}"
    footer_left = "Confidential"
    if brand_config.has_partner:
        footer_left += f" — {brand_config.company_name} & {brand_config.partner_name}"
    footer = f"&L{footer_left}&C&P of &N&R&D"

    for sheet in [CF_SHEET, "Input", "Waterfall", "Summary"]:
        try:
            orientation = "portrait" if sheet in ("Input", "Summary") else "landscape"
            writer.set_page_setup(sheet, orientation=orientation, fit_to_width=1)
            writer.set_header_footer(sheet, header=header, footer=footer)
        except (KeyError, ValueError):
            pass  # Sheet may not exist in all templates


def write_conditional_formats(writer):
    """Apply conditional formatting rules to CF Calculations and Waterfall."""
    # DSCR annual (row 158, cols E-N)
    writer.add_conditional_format(CF_SHEET, "E158:N158", "threshold",
        thresholds=[
            {"operator": "lessThan", "value": 1.0, "color": "FFFF0000"},
            {"operator": "between", "value": [1.0, 1.25], "color": "FFFF8C00"},
            {"operator": "greaterThan", "value": 1.25, "color": "FF008000"},
        ]
    )

    # DSCR monthly (row 158, cols S-EI)
    writer.add_conditional_format(CF_SHEET, "S158:EI158", "threshold",
        thresholds=[
            {"operator": "lessThan", "value": 1.0, "color": "FFFF0000"},
            {"operator": "between", "value": [1.0, 1.25], "color": "FFFF8C00"},
            {"operator": "greaterThan", "value": 1.25, "color": "FF008000"},
        ]
    )

    # Leveraged CF annual (row 108, cols E-N) — negative red
    writer.add_conditional_format(CF_SHEET, "E108:N108", "negative_red")

    # Leveraged CF monthly (row 108, cols S-EI) — negative red
    writer.add_conditional_format(CF_SHEET, "S108:EI108", "negative_red")

    # Levered IRR gradient (C108)
    writer.add_conditional_format(CF_SHEET, "C108", "gradient",
        min_color="FFFF0000", mid_color="FFFFFF00", max_color="FF00FF00"
    )

    # Waterfall promote rows — negative red
    try:
        writer.add_conditional_format("Waterfall", "C60:P60", "negative_red")
    except KeyError:
        pass  # Waterfall sheet may not exist in template


def write_named_ranges(writer):
    """Define named ranges for key metrics in workbook.xml."""
    named_ranges = [
        ("LeveredIRR", CF_SHEET, "C108"),
        ("UnleveredIRR", CF_SHEET, "C86"),
        ("PartnershipIRR", CF_SHEET, "C130"),
        ("NOI_Year1", CF_SHEET, "E59"),
        ("GoingInCap", CF_SHEET, "E145"),
        ("AvgDSCR", CF_SHEET, "E158"),
        ("PurchasePrice", CF_SHEET, "D10"),
        ("TotalEquity", CF_SHEET, "D108"),
        ("AnnualDebtService", CF_SHEET, "E92:N92"),
    ]
    for name, sheet, ref in named_ranges:
        try:
            writer.define_named_range(name, sheet, ref)
        except KeyError:
            pass


def write_print_areas(writer, last_input_row=200):
    """Set dynamic print areas for key sheets."""
    # CF Calculations: rows 1-158, cols A-EI (139)
    try:
        writer.set_print_area(CF_SHEET, 1, 1, 158, 139)
    except KeyError:
        pass

    # Input: rows 1 to last_input_row, cols A-M (13)
    try:
        writer.set_print_area("Input", 1, 1, last_input_row, 13)
    except KeyError:
        pass

    # Waterfall: rows 1-69, cols A-EI (139)
    try:
        writer.set_print_area("Waterfall", 1, 1, 69, 139)
    except KeyError:
        pass

    # Summary: rows 1-50, cols A-B (if present)
    writer.set_print_area("Summary", 1, 1, 50, 2)


def generate_rediq_workbook(
    results: Dict[str, Any],
    inputs: Dict[str, Any],
    output_path: Path,
    template_path: Path = Path("excel/rediq_clone.xlsm"),
    scenario_results: Optional[Dict[str, Any]] = None,
    tornado_results: Optional[Dict[str, Any]] = None,
    brand_config=None,
) -> Path:
    """Generate a RedIQ-format workbook from engine results.

    Uses ZIP-level XML manipulation to write cell values without corrupting
    VBA, styles, conditional formatting, or other features.

    Args:
        results: Engine output dict (from run_underwriting)
        inputs: Engine input dict (validated deal inputs)
        output_path: Where to save the generated workbook
        template_path: Path to the RedIQ clone template
        scenario_results: Optional Bull/Base/Bear scenario comparison
            (from engine.modules.scenarios.run_scenarios)
        brand_config: Optional BrandConfig for JV co-branding.
            If None, built from inputs.

    Returns:
        Path to the saved workbook
    """
    from engine.brand import BrandConfig

    # Wave 5 Task 5.2: defensive provenance shape check. Fail loud if the
    # engine result is missing standardized federation keys rather than
    # silently producing a workbook without verdict / sanity-flag annotations.
    _validate_provenance_shape(results)

    template_path = Path(template_path)
    output_path = Path(output_path)

    if brand_config is None:
        brand_config = BrandConfig.from_inputs(inputs)

    with XlsmWriter(template_path, output_path) as writer:
        write_monthly_cashflows(writer, results, inputs)
        write_irr_values(writer, results)
        write_metrics_rows(writer, results)
        write_input_sheet(writer, results, inputs, template_path=template_path)
        write_waterfall_sheet(writer, results, inputs)
        write_waterfall_summary(writer, results)
        write_summary_metrics(writer, results, inputs)
        write_sensitivity_tables(writer, results)
        if scenario_results or tornado_results:
            write_scenario_dashboard(writer, scenario_results, tornado_results)
        write_deal_summary_tab(writer, results, inputs)
        write_freeze_panes(writer)
        write_print_setup(writer, inputs, brand_config)
        write_conditional_formats(writer)
        write_named_ranges(writer)
        write_print_areas(writer)

    return output_path


def write_outputs_to_rediq(
    outputs_path: str | Path,
    workbook_path: str | Path,
    output_workbook_path: Optional[str | Path] = None,
    inputs: Optional[Dict[str, Any]] = None,
) -> Path:
    """
    Write engine outputs into a RedIQ clone workbook (legacy API).

    Args:
        outputs_path: Path to engine outputs.json
        workbook_path: Path to RedIQ clone .xlsm
        output_workbook_path: Where to save (default: overwrite workbook_path)
        inputs: Optional inputs dict for context

    Returns:
        Path to the output workbook
    """
    outputs_path = Path(outputs_path)
    workbook_path = Path(workbook_path)
    output_path = Path(output_workbook_path) if output_workbook_path else workbook_path

    results = json.loads(outputs_path.read_text(encoding="utf-8"))
    if inputs is None:
        inputs = {}

    template_path = workbook_path

    with XlsmWriter(template_path, output_path) as writer:
        write_monthly_cashflows(writer, results, inputs)
        write_irr_values(writer, results)
        write_metrics_rows(writer, results)
        write_input_sheet(writer, results, inputs, template_path=template_path)
        write_waterfall_sheet(writer, results, inputs)
        write_waterfall_summary(writer, results)
        write_summary_metrics(writer, results, inputs)
        write_sensitivity_tables(writer, results)

    return output_path


# ── CLI ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Write engine outputs to RedIQ workbook")
    parser.add_argument("outputs", help="Path to outputs.json")
    parser.add_argument("workbook", help="Path to RedIQ clone .xlsm")
    parser.add_argument("--save-as", help="Save to different path (default: overwrite)")
    parser.add_argument("--inputs", help="Path to inputs.json for full population")
    args = parser.parse_args()

    inputs_dict = None
    if args.inputs:
        inputs_dict = json.loads(Path(args.inputs).read_text(encoding="utf-8"))

    out_path = write_outputs_to_rediq(
        args.outputs,
        args.workbook,
        args.save_as,
        inputs_dict,
    )
    print(f"Results written to: {out_path}")
