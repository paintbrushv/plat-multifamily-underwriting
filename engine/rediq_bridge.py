"""
RedIQ Bridge Adapter
====================
Phase 2: Maps RedIQ named ranges → canonical tbl_* engine schema.

Reads the RedIQ clone workbook (or source), extracts values via named ranges,
and outputs a valid inputs.json for the underwriting engine.

This bridges RedIQ's named-range world ↔ engine's table-based world.

Usage:
    from engine.rediq_bridge import extract_inputs_from_rediq
    inputs = extract_inputs_from_rediq("excel/rediq_clone.xlsm")

    # Or from CLI:
    python -m engine.rediq_bridge excel/rediq_clone.xlsm --output inputs.json
"""

from __future__ import annotations

import json
import re
import sys
import warnings
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import openpyxl
from openpyxl.utils import get_column_letter


# ── Named Range → Engine Schema Mapping ────────────────────────────────────
# These map RedIQ named ranges to our canonical deal schema fields.
# Format: (rediq_named_range, engine_field, transform_func_name)

# --- Deal Metadata ---
METADATA_MAP = {
    "DealName": "deal_id",
    "CounterId": "run_id",
    "AnalysisStartDate": "analysis_start_date",
    "YrOfExit": "exit_year",
    "DateOfExit": "exit_date",
    "CreatedBy": "analyst",
    "PropertyNotes": "notes",
    "Address1": "address_1",
    "Address2": "address_2",
    "City": "city",
}

# --- Acquisition/Disposition ---
PURCHASE_MAP = {
    "PurchasePrice": "purchase_price",
    "GoingInCapRate": "going_in_cap_rate",
    "ExitCapRate": "exit_cap_rate",
    "SalesPrice": "sales_price",
    "TotalOtherCCAcq": "closing_costs",
    "SalesCommission": "sale_cost_percent",
    "TransferTaxValue": "transfer_tax",
    "BrokerCommissionValue": "broker_commission",
}

# --- Loan fields (up to 3 loans in RedIQ) ---
LOAN_FIELDS = {
    "Amount": "commitment",
    "IntRate": "rate",
    "AmortPer": "amort_years",
    "IOPer": "io_months",
    "Term": "term_months",
    "OrigMnth": "loan_start_month",
    "LTV": "ltv",
    "DSCR": "dscr_constraint",
    "IntType": "interest_type",
    "PmtType": "payment_type",
    "BseRate": "base_rate",
    "IntSprd": "interest_spread",
    "PointsPct": "points_percent",
}

# --- OpEx category mapping ---
# RedIQ uses fixed expense line items at Input!$B$147:$B$164
REDIQ_EXPENSE_LABELS = [
    "Real Estate Taxes",
    "Insurance",
    "Utilities",
    "Repairs & Maintenance",
    "Contract Services",
    "Turnover",
    "Administrative",
    "Payroll",
    "Management Fee",
    "Marketing",
    "Professional Fees",
    "Ground Lease",
    "Capital Reserves",
    "Other",
    "RUBS",
    "Amenity Expenses",
    "Custom Exp 1",
    "Custom Exp 2",
]

# --- Revenue/Loss assumptions ---
LOSS_FACTOR_MAP = {
    "StructuralVacancy": "structural_vacancy",
    "NonRevenueUnits": "non_revenue_units",
    "LossToLease": "loss_to_lease",
    "ConcessionsGPR": "concessions",
    "CollectionLoss": "collection_loss",
}

# ── RedIQ Layout Configuration ──────────────────────────────────────────────
# Centralizes all hardcoded row/column references from the Input sheet.
# Changing RedIQ's layout only requires updating this dict.
REDIQ_LAYOUT = {
    # Floor plans: Input rows 53-63
    "floor_plans": {"start_row": 53, "end_row": 64, "cols": {
        "name": "B", "unit_type": "D", "sqft": "F", "beds": "G",
        "unit_count": "H", "occupied": "I", "non_revenue": "K",
        "market_rent": "L", "inplace_rent": "M", "is_renovated": "O",
        "starting_market": "P",
    }},
    # Market rent growth: Input row 78, cols G-Q (Yr1-Yr11)
    "rent_growth": {"row": 78, "start_col": 7, "end_col": 18},
    # Renovation schedule: Input rows 87-97
    "renovations": {"start_row": 87, "end_row": 98, "cols": {
        "name": "B", "downtime": "C", "cost": "D", "premium": "E",
        "post_market": "F",
    }, "year_start_col": 7, "year_end_col": 14},
    # Other income: Input rows 124-138
    "other_income": {"start_row": 124, "end_row": 139, "cols": {
        "name": "B", "unit_basis": "E", "frequency": "F", "amount": "G",
    }, "inflation_row": 136, "inflation_start_col": 8},
    # Operating expenses: Input rows 147-166
    "opex": {"start_row": 147, "end_row": 167, "cols": {
        "name": "B", "base_value": "E", "type_indicator": "F",
    }},
    # Inflation rates: Input rows 169-170, cols G-Q
    "inflation": {
        "general_row": 169, "re_tax_row": 170,
        "start_col": 7, "end_col": 18,
    },
    # CapEx schedule: Input rows 198-204, cols G-M (Yr1-Yr7)
    "capex": {"start_row": 198, "end_row": 205, "cols": {
        "name": "B", "pct": "F",
    }, "year_start_col": 7, "year_end_col": 14},
    # Fund assumptions: Input rows 242-258
    "fund": {
        "sponsor_pct": (247, 6),    # F247
        "hurdle_irr": (247, 10),    # J247
        "tier4_lp": (248, 12),      # L248
        "tier4_gp": (248, 13),      # M248
        "acq_fee": (254, 11),       # K254
        "am_fee": (256, 11),        # K256
        "disp_fee": (257, 11),      # K257
        "partnership_exp": (258, 12),  # L258
    },
    # CF Calculations sheet row references
    "cf_calculations": {
        "market_rent": 18,
        "gpr": 20,
        "vacancy": 22,
        "reno_downtime": 23,
        "concessions": 24,
        "non_revenue": 25,
        "collection_loss": 26,
        "egr": 34,
        "total_opex": 54,
        "noi": 59,
        "unlev_cf": 86,
        "debt_service": 92,
        "lev_cf": 108,
        # Fund waterfall rows
        "partnership_closing_costs": 113,
        "partnership_expenses": 114,
        "acquisition_fee": 118,
        "am_fee": 120,
        "cf_before_promote": 124,
        "promote": 126,
        "sponsor_share": 134,
        "lp_share": 141,
    },
    # Year columns in CF Calculations: E=5 for Yr1
    "cf_year_start_col": 5,
}


def _warn_default(message: str, default):
    """Issue a warning and return the default value."""
    warnings.warn(f"RedIQ Bridge: {message}", stacklevel=3)
    return default


# ── Section Scanner Utilities ─────────────────────────────────────────────
# These scan the Input sheet dynamically to find section boundaries,
# replacing hardcoded row numbers that break across different RedIQ models.


def _scan_for_row(ws, col, marker, start=1, end=300):
    """Scan column `col` from `start` to `end` for a cell containing `marker`.

    Case-insensitive substring match. Returns the row number or None.
    """
    marker_lower = marker.lower()
    for row in range(start, end + 1):
        val = ws[f"{col}{row}"].value
        if val is not None and marker_lower in str(val).lower():
            return row
    return None


def _scan_for_rows(ws, col, marker, start=1, end=300):
    """Scan column `col` for ALL cells containing `marker` (case-insensitive).

    Returns list of matching row numbers.
    """
    marker_lower = marker.lower()
    rows = []
    for row in range(start, end + 1):
        val = ws[f"{col}{row}"].value
        if val is not None and marker_lower in str(val).lower():
            rows.append(row)
    return rows


def _find_section_end(ws, col, start_row, max_scan=50):
    """Find the end of a data section by looking for blank or 'Total' row."""
    for row in range(start_row + 1, start_row + max_scan):
        val = ws[f"{col}{row}"].value
        if val is None or str(val).strip() == "":
            return row
        if str(val).strip().lower().startswith("total"):
            return row
    return start_row + max_scan


def _detect_layout(ws) -> dict:
    """Detect the layout of the Input sheet by scanning for section markers.

    Builds a layout dict with row numbers for each section. Falls back to
    REDIQ_LAYOUT defaults when a marker isn't found.
    """
    layout = {}

    # ── Floor Plans ──
    fp_row = (_scan_for_row(ws, "A", "Floor Plan Mix") or
              _scan_for_row(ws, "B", "Floor Plan") or
              _scan_for_row(ws, "A", "Unit Mix"))
    if fp_row:
        # Data rows start after the header row (skip header + column labels)
        # Scan for the first row with data in column B below the header
        data_start = fp_row + 1
        for r in range(fp_row + 1, fp_row + 5):
            val = ws[f"B{r}"].value
            if val is not None and str(val).strip():
                data_start = r
                break
        layout["floor_plans_start"] = data_start
    else:
        layout["floor_plans_start"] = REDIQ_LAYOUT["floor_plans"]["start_row"]

    # ── Other Income ──
    oi_row = (_scan_for_row(ws, "A", "Other Income") or
              _scan_for_row(ws, "B", "Other Income"))
    if oi_row:
        # Data starts after the header
        data_start = oi_row + 1
        for r in range(oi_row + 1, oi_row + 5):
            val = ws[f"B{r}"].value
            if val is not None and str(val).strip():
                data_start = r
                break
        layout["other_income_start"] = data_start
        # Find end: scan for section terminators (not just first blank row,
        # as OI sections can have blank rows between items).
        oi_end = None
        for marker in ["Total Other Income", "Other Income Inflation",
                        "Expense Reimbursements", "EXPENSE ASSUMPTIONS",
                        "ANNUAL OPERATING"]:
            found = _scan_for_row(ws, "B", marker, start=data_start, end=data_start + 30)
            if found:
                oi_end = found if oi_end is None else min(oi_end, found)
        layout["other_income_end"] = oi_end or (data_start + 20)
    else:
        layout["other_income_start"] = REDIQ_LAYOUT["other_income"]["start_row"]
        layout["other_income_end"] = REDIQ_LAYOUT["other_income"]["end_row"]
        _warn_default("'Other Income' header not found; using default rows", None)

    # ── Other Income Inflation ──
    oi_start = layout["other_income_start"]
    oi_end = layout["other_income_end"]
    oi_infl = (_scan_for_row(ws, "B", "Other Income Inflation", start=oi_start, end=oi_end + 10) or
               _scan_for_row(ws, "A", "Other Income Inflation", start=oi_start, end=oi_end + 10))
    layout["other_income_inflation_row"] = oi_infl or REDIQ_LAYOUT["other_income"].get("inflation_row", 136)

    # ── Operating Expenses ──
    opex_row = (_scan_for_row(ws, "A", "Operating Expenses") or
                _scan_for_row(ws, "A", "Expense Category") or
                _scan_for_row(ws, "B", "Operating Expenses"))
    if opex_row:
        # Data starts after header (first row with a category name in col B)
        data_start = opex_row + 1
        for r in range(opex_row + 1, opex_row + 5):
            val = ws[f"B{r}"].value
            if val is not None and str(val).strip():
                data_start = r
                break
        layout["opex_start"] = data_start
        # Find end: "Total Operating Expenses" or next section
        total_row = _scan_for_row(ws, "B", "Total Operating", start=data_start, end=data_start + 30)
        if not total_row:
            total_row = _scan_for_row(ws, "A", "Total Operating", start=data_start, end=data_start + 30)
        layout["opex_end"] = total_row or _find_section_end(ws, "B", data_start)
    else:
        layout["opex_start"] = REDIQ_LAYOUT["opex"]["start_row"]
        layout["opex_end"] = REDIQ_LAYOUT["opex"]["end_row"]
        _warn_default("'Operating Expenses' header not found; using default rows", None)

    # ── Inflation ──
    infl_row = (_scan_for_row(ws, "B", "General Inflation") or
                _scan_for_row(ws, "A", "General Inflation"))
    layout["inflation_row"] = infl_row or REDIQ_LAYOUT["inflation"]["general_row"]

    re_tax_row = (_scan_for_row(ws, "B", "RE Tax Inflation") or
                  _scan_for_row(ws, "A", "RE Tax Inflation") or
                  _scan_for_row(ws, "B", "Real Estate Tax Inflation") or
                  _scan_for_row(ws, "A", "Real Estate Tax Inflation"))
    layout["re_tax_inflation_row"] = re_tax_row or REDIQ_LAYOUT["inflation"]["re_tax_row"]

    # ── Capital Expenditures ──
    # "PROPERTY-WIDE CAPITAL EXPENSES" is the actual data section header (rows 190+).
    # There are earlier mentions ("Projected Capital Expenditures" at row 42) that
    # are just toggles/labels — skip those by scanning from row 150+.
    capex_row = (_scan_for_row(ws, "B", "PROPERTY-WIDE CAPITAL", start=150) or
                 _scan_for_row(ws, "A", "PROPERTY-WIDE CAPITAL", start=150) or
                 _scan_for_row(ws, "B", "Capital Expenditures", start=150) or
                 _scan_for_row(ws, "A", "Capital Improvements", start=150))
    if capex_row:
        # Data rows start after "Description" header (typically capex_row + 3 or +4)
        desc_row = _scan_for_row(ws, "B", "Description", start=capex_row, end=capex_row + 10)
        if desc_row:
            data_start = desc_row + 1
        else:
            data_start = capex_row + 1
            for r in range(capex_row + 1, capex_row + 5):
                val = ws[f"B{r}"].value
                if val is not None and str(val).strip():
                    data_start = r
                    break
        layout["capex_start"] = data_start
    else:
        layout["capex_start"] = REDIQ_LAYOUT["capex"]["start_row"]

    # ── Fund Assumptions ──
    # Scan for multiple markers — fund section varies widely across deals.
    # Scan up to row 400 because deals with 40+ floor plans push the
    # partnership section past row 300 (e.g., Principal Residences at row 306).
    fund_row = None
    for marker in ["Sponsor", "GP/LP", "Equity Partner", "Fund Structure"]:
        for col in ["A", "B"]:
            found = _scan_for_row(ws, col, marker, start=200, end=400)
            if found:
                fund_row = found
                break
        if fund_row:
            break
    layout["fund_start"] = fund_row  # May be None if no fund section

    # ── Rent Growth ──
    rg_row = (_scan_for_row(ws, "A", "Market Rent Growth") or
              _scan_for_row(ws, "B", "Market Rent Growth") or
              _scan_for_row(ws, "A", "Rent Growth") or
              _scan_for_row(ws, "B", "Rent Growth"))
    layout["rent_growth_row"] = rg_row or REDIQ_LAYOUT["rent_growth"]["row"]

    # ── Renovations ──
    reno_row = (_scan_for_row(ws, "A", "Renovation") or
                _scan_for_row(ws, "B", "Renovation") or
                _scan_for_row(ws, "A", "Interior Upgrade") or
                _scan_for_row(ws, "B", "Interior Upgrade"))
    if reno_row:
        data_start = reno_row + 1
        for r in range(reno_row + 1, reno_row + 5):
            val = ws[f"B{r}"].value
            if val is not None and str(val).strip():
                data_start = r
                break
        layout["renovations_start"] = data_start
    else:
        layout["renovations_start"] = REDIQ_LAYOUT["renovations"]["start_row"]

    return layout


def _safe_value(cell_or_value):
    """Normalize a cell value."""
    if cell_or_value is None:
        return None
    if hasattr(cell_or_value, "value"):
        val = cell_or_value.value
    else:
        val = cell_or_value
    if isinstance(val, datetime):
        return val.date().isoformat()
    if isinstance(val, date):
        return val.isoformat()
    return val


def _to_float(val, default=None):
    """Safely convert to float."""
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _to_int(val, default=None):
    """Safely convert to int."""
    if val is None:
        return default
    try:
        f = float(val)
        if f == int(f):
            return int(f)
        return int(f)
    except (ValueError, TypeError):
        return default


def _to_str(val, default=None):
    """Safely convert to string."""
    if val is None:
        return default
    s = str(val).strip()
    return s if s else default


def _to_month_str(val):
    """Convert a date/string value to YYYY-MM format for the schema month type."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        if val == 0:
            return None  # 0 means not set
        return None  # Can't convert a bare number to month
    if isinstance(val, datetime):
        return val.strftime("%Y-%m")
    if isinstance(val, date):
        return val.strftime("%Y-%m")
    s = str(val).strip()
    if not s or s == "0":
        return None
    # Already YYYY-MM
    if re.match(r"^\d{4}-\d{2}$", s):
        return s
    # YYYY-MM-DD → YYYY-MM
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:7]
    return None


def _resolve_named_range(wb, name):
    """Resolve a named range to its cell value(s). Returns single value or list of values."""
    try:
        dn = wb.defined_names[name]
    except KeyError:
        return None

    dest = dn.attr_text if hasattr(dn, "attr_text") else str(dn.value)

    # Skip formula-based defined names
    if not dest or "!" not in dest:
        return None

    # Handle multi-area ranges (comma-separated)
    # Just use the first area for scalar lookups
    if "," in dest:
        dest = dest.split(",")[0]

    # Parse sheet!cell reference
    parts = dest.split("!")
    if len(parts) != 2:
        return None

    sheet_name = parts[0].replace("'", "")
    cell_ref = parts[1]

    try:
        ws = wb[sheet_name]
    except KeyError:
        return None

    # Check if it's a range (e.g., $A$1:$B$10) or single cell
    if ":" in cell_ref:
        try:
            cells = ws[cell_ref]
            if isinstance(cells, tuple):
                # Multi-row range
                values = []
                for row in cells:
                    if isinstance(row, tuple):
                        values.append([_safe_value(c) for c in row])
                    else:
                        values.append(_safe_value(row))
                return values
            return _safe_value(cells)
        except Exception:
            return None
    else:
        try:
            cell = ws[cell_ref]
            return _safe_value(cell)
        except Exception:
            return None


def _read_cell(wb, sheet_name, cell_ref):
    """Read a specific cell by sheet name and cell reference."""
    try:
        ws = wb[sheet_name]
        cell = ws[cell_ref]
        return _safe_value(cell)
    except (KeyError, Exception):
        return None


def _read_range_as_list(wb, sheet_name, range_ref):
    """Read a range as a flat list of values."""
    try:
        ws = wb[sheet_name]
        cells = ws[range_ref]
        values = []
        for row in cells:
            if isinstance(row, tuple):
                for c in row:
                    values.append(_safe_value(c))
            else:
                values.append(_safe_value(row))
        return values
    except (KeyError, Exception):
        return []


def extract_cf_calc_loss_curves(
    wb,
    start_date: str,
    end_date: str,
    cohort_ids: List[str],
    yr_of_exit: int,
) -> Dict[str, Any]:
    """Extract year-by-year loss factors from CF Calculations sheet.

    When named-range loss factors return 0 (formula cells without cached values),
    we fall back to deriving rates from the CF Calculations sheet which has
    Excel-cached annual aggregates.

    CF Calculations layout:
      Row 18: Potential Market Rent
      Row 20: Gross Potential Revenue (after LTL, non-revenue units)
      Row 22: Vacancy (negative)
      Row 24: Concessions (negative)
      Row 26: Collection Loss / Bad Debt (negative)
      Columns: E=Yr1, F=Yr2, ... (up to yr_of_exit)

    Returns dict with piecewise curves: loss_to_lease, physical_vacancy, collection_loss
    """
    from dateutil.relativedelta import relativedelta

    try:
        cf_ws = wb["CF Calculations"]
    except KeyError:
        return {}

    start_dt = date.fromisoformat(start_date)
    end_dt = date.fromisoformat(end_date)

    loss_to_lease = []
    physical_vacancy = []
    collection_loss = []

    for yr_idx in range(yr_of_exit):
        col = 5 + yr_idx  # E=5 for year 1

        mkt_rent = _to_float(cf_ws.cell(row=18, column=col).value, 0)
        gpr = _to_float(cf_ws.cell(row=20, column=col).value, 0)
        net_rev = _to_float(cf_ws.cell(row=27, column=col).value, 0)

        if mkt_rent <= 0 or gpr <= 0:
            continue

        # LTL rate = (Market Rent - GPR) / Market Rent
        # Negative LTL = gain-to-lease (in-place rents above market)
        ltl_rate = max(-1, min(1, (mkt_rent - gpr) / mkt_rent))

        # Combined vacancy rate captures ALL deductions between GPR and Net Revenue
        # (vacancy, reno downtime, concessions, collection loss, non-revenue units).
        # This avoids row-attribution issues where the meaning of rows 22-26 varies
        # across deals.
        vac_rate = max(0, min(1, (gpr - net_rev) / gpr)) if gpr > 0 else 0

        # Collection loss set to 0 — already captured in the combined vacancy rate
        coll_rate = 0

        # Year boundaries
        yr_start = start_dt + relativedelta(years=yr_idx)
        next_yr = start_dt + relativedelta(years=yr_idx + 1)
        yr_end = end_dt if next_yr >= end_dt else next_yr - relativedelta(months=1)

        sp = yr_start.strftime("%Y-%m")
        ep = yr_end.strftime("%Y-%m")

        for cid in cohort_ids:
            loss_to_lease.append({
                "cohort_id": cid,
                "start_period": sp,
                "end_period": ep,
                "ltl_percent": round(ltl_rate, 4),
            })
            physical_vacancy.append({
                "cohort_id": cid,
                "start_period": sp,
                "end_period": ep,
                "vacancy_rate": round(vac_rate, 4),
            })

        collection_loss.append({
            "applies_to": "ALL",
            "start_period": sp,
            "end_period": ep,
            "loss_rate": round(coll_rate, 4),
        })

    return {
        "loss_to_lease": loss_to_lease,
        "physical_vacancy": physical_vacancy,
        "collection_loss": collection_loss,
    }


def extract_floor_plans(wb, layout: Optional[dict] = None) -> List[Dict[str, Any]]:
    """Extract floor plan / unit cohort data from the Input sheet.

    Uses layout detection to find the floor plan section dynamically.
    Scans until hitting a blank row, "Total", or "Average" row.
    """
    cohorts = []
    ws = wb["Input"]

    start_row = layout["floor_plans_start"] if layout else REDIQ_LAYOUT["floor_plans"]["start_row"]
    max_end = start_row + 60  # Scan up to 60 rows (some deals have 40+ floor plans)

    for row_idx in range(start_row, max_end):
        fp_name = _safe_value(ws[f"B{row_idx}"])
        if not fp_name or str(fp_name).strip() == "":
            break  # End of floor plan section

        name_str = str(fp_name).strip()
        name_lower = name_str.lower()

        # Skip total/average/summary rows
        if name_lower.startswith("total") or name_lower.startswith("average"):
            break  # End of floor plan section

        unit_count = _to_int(_safe_value(ws[f"H{row_idx}"]))
        if not unit_count or unit_count <= 0:
            continue

        cohort = {
            "cohort_id": _to_str(fp_name),
            "unit_type": _to_str(_safe_value(ws[f"D{row_idx}"]), fp_name),
            "unit_count": unit_count,
            "initial_inplace_rent": _to_float(_safe_value(ws[f"M{row_idx}"]), 0),
        }

        sqft = _to_float(_safe_value(ws[f"F{row_idx}"]))
        if sqft:
            cohort["sqft"] = sqft

        beds = _to_int(_safe_value(ws[f"G{row_idx}"]))
        if beds is not None:
            cohort["beds"] = beds

        market_rent = _to_float(_safe_value(ws[f"L{row_idx}"]))
        if market_rent:
            cohort["market_rent"] = market_rent

        occupied = _to_int(_safe_value(ws[f"I{row_idx}"]))
        if occupied is not None:
            cohort["occupied_units"] = occupied

        non_rev = _to_int(_safe_value(ws[f"K{row_idx}"]))
        if non_rev is not None:
            cohort["non_revenue_units"] = non_rev

        starting_market = _to_float(_safe_value(ws[f"P{row_idx}"]))
        if starting_market:
            cohort["starting_market_rent"] = starting_market

        is_renovated = _safe_value(ws[f"O{row_idx}"])
        if is_renovated:
            cohort["is_renovated"] = bool(is_renovated)

        cohorts.append(cohort)

    return cohorts


def extract_loss_factors(wb) -> Dict[str, float]:
    """Extract loss factor assumptions from the Input sheet."""
    factors = {}
    for rediq_name, engine_name in LOSS_FACTOR_MAP.items():
        val = _resolve_named_range(wb, rediq_name)
        if val is not None:
            factors[engine_name] = _to_float(val, 0)
    return factors


# ---------------------------------------------------------------------------
# OpEx CF-Calcs row map — module-level so both the fallback function and
# extract_opex's claimed-row tracker can reference it.
# ---------------------------------------------------------------------------

# Category name (lowercase) → CF Calculations row (Year 1 value in col E).
# Mirrors OPEX_CATEGORY_ROW in rediq_output.py.
_CF_OPEX_ROWS: dict = {
    "repair & maintenance": 39, "contract services": 40,
    "security": 41, "turnover / make-ready": 42,
    "landscaping / grounds": 43, "personnel": 44,
    "marketing / advertising": 45, "administrative": 46,
    "administrative expenses": 46, "utilities": 47,
    "electricity": 47, "fuel (gas & oil)": 47,
    "water & sewer": 47, "other utilities": 47,
    "insurance": 48, "real estate taxes": 49,
    "other property taxes": 49, "property management fee": 50,
    "other operating expenses": 51, "franchise fee": 51,
    "reimbursements": 52,
}

# Rows mapped by 2+ category names.  The fallback must never assign these
# because the CF Calcs row holds an aggregate and we cannot know which portion
# belongs to the current category — doing so would create duplicates.
_CF_SHARED_ROWS: frozenset = frozenset(
    row
    for row in set(_CF_OPEX_ROWS.values())
    if sum(1 for v in _CF_OPEX_ROWS.values() if v == row) > 1
)


def _try_read_cf_calcs_opex(
    wb,
    category_name_lower: str,
    claimed_rows: Optional[set] = None,
) -> Optional[float]:
    """Try to read an OpEx category's Year 1 value from CF Calculations.

    This is a fallback for "% over Historicals" items where the Input sheet
    has no cached values. CF Calculations rows 39-52 contain the actual
    computed OpEx by category with Year 1 in column E.

    Guards (return None, behaving like pre-fallback ``continue``):
      - Shared rows: the row is mapped by 2+ category names → we cannot know
        which category owns the aggregate value; assigning it would create
        duplicates (e.g. "other property taxes" cloning the RE-taxes total).
      - Claimed rows: a previous category already used this row via fallback;
        a second assignment would be a duplicate.

    Preserves the original purpose: institutional asset-management sheets
    where the Input sheet genuinely lacks cached values AND the target row
    is unambiguously mapped to this one category.
    """
    row = _CF_OPEX_ROWS.get(category_name_lower)
    if not row:
        return None
    # Guard 1: shared row — multiple categories map here; can't safely assign.
    if row in _CF_SHARED_ROWS:
        return None
    # Guard 2: already claimed by a previous extraction in this workbook pass.
    if claimed_rows is not None and row in claimed_rows:
        return None
    try:
        ws = wb["CF Calculations"]
        val = _to_float(_safe_value(ws.cell(row=row, column=5)))  # col E = Year 1
        return abs(val) if val else None
    except Exception:
        return None


def extract_opex(wb, utility_categories: Optional[set] = None, layout: Optional[dict] = None) -> List[Dict[str, Any]]:
    """Extract operating expense categories from the Input sheet.

    Uses layout detection to find the OpEx section dynamically.
    Handles "% over Hist." type by reading the Year 1 value from column G.

    Column layout per row:
      B = category name
      E = base value (annual total or percentage rate)
      F = type indicator: "Total", "of EGR", "per Unit", "% over Hist."
      G-M = Year 1-7 projected amounts
    """
    ws = wb["Input"]
    categories = []

    # Determine row ranges from layout
    if layout:
        opex_start = layout["opex_start"]
        opex_end = layout["opex_end"]
        infl_row = layout["inflation_row"]
        re_tax_infl_row = layout["re_tax_inflation_row"]
    else:
        opex_start = REDIQ_LAYOUT["opex"]["start_row"]
        opex_end = REDIQ_LAYOUT["opex"]["end_row"]
        infl_row = REDIQ_LAYOUT["inflation"]["general_row"]
        re_tax_infl_row = REDIQ_LAYOUT["inflation"]["re_tax_row"]

    # Extract general and RE tax inflation rates with timing
    general_infl = 0.0
    general_infl_found = False
    general_start_yr = 0
    re_tax_infl = 0.0
    re_tax_infl_found = False
    re_tax_start_yr = 0
    for col_idx in range(7, 18):  # G=7 through Q (Yr1 through Yr11)
        yr_idx = col_idx - 7
        gi = _to_float(_safe_value(ws.cell(row=infl_row, column=col_idx)))
        if gi is not None and not general_infl_found:
            general_infl = gi
            general_infl_found = True
            general_start_yr = yr_idx
        rti = _to_float(_safe_value(ws.cell(row=re_tax_infl_row, column=col_idx)))
        if rti is not None and not re_tax_infl_found:
            re_tax_infl = rti
            re_tax_infl_found = True
            re_tax_start_yr = yr_idx

    # Utility category names for recoverable marking
    _utility_names = utility_categories or {
        "electricity", "fuel (gas & oil)", "fuel", "gas", "water & sewer",
        "water", "sewer", "other utilities", "utilities",
    }

    # Header/column-label rows to skip
    _opex_skip_names = {"expense category", "expense item", "category name"}

    # Track which CF Calcs rows have already been assigned during this pass.
    # Prevents the fallback from pulling the same aggregate row value twice
    # (e.g. if two unique-row categories both lack Input-sheet cached values).
    claimed_rows: set = set()

    for row_idx in range(opex_start, opex_end):
        name = _safe_value(ws[f"B{row_idx}"])
        if not name or str(name).strip() == "":
            continue

        name_str = str(name).strip()
        name_lower = name_str.lower()

        # Skip "Total" summary rows
        if name_lower.startswith("total"):
            continue

        # Skip column header/label rows
        if name_lower in _opex_skip_names:
            continue

        base_amount = _to_float(_safe_value(ws[f"E{row_idx}"]))
        type_indicator = _to_str(_safe_value(ws[f"F{row_idx}"]))
        type_str = str(type_indicator).lower() if type_indicator else ""
        yr1_amount = _to_float(_safe_value(ws.cell(row=row_idx, column=7)))  # col G

        # Handle "% over Hist." — use Year 1 projected value from column G
        # instead of skipping entirely. The G column has the resulting dollar
        # amount after applying the inflation % to the historical value.
        if "hist" in type_str:
            if yr1_amount and yr1_amount != 0:
                calc_type = "fixed_annual"
                base_value = yr1_amount
            else:
                # Column G may also be None (uncached formula). Try to read
                # the base_amount (col E) as the inflation %, and look for
                # a historical value in column D or use base_amount as-is
                hist_val = _to_float(_safe_value(ws[f"D{row_idx}"]))
                if hist_val and hist_val > 0 and base_amount:
                    base_value = hist_val * (1 + base_amount)
                    calc_type = "fixed_annual"
                elif base_amount and abs(base_amount) > 1:
                    base_value = base_amount
                    calc_type = "fixed_annual"
                else:
                    # Last resort: try reading from CF Calculations for this
                    # category (asset management models with historical sheets).
                    # Guards inside prevent shared rows and already-claimed rows
                    # from creating duplicate OpEx entries.
                    cf_val = _try_read_cf_calcs_opex(wb, name_lower, claimed_rows)
                    if cf_val and cf_val > 0:
                        base_value = cf_val
                        calc_type = "fixed_annual"
                    else:
                        continue
        elif base_amount is None or base_amount == 0:
            # No base amount — try Year 1 column
            if yr1_amount and yr1_amount != 0:
                base_value = yr1_amount
                calc_type = "fixed_annual"
            else:
                continue
        elif "egr" in type_str:
            calc_type = "percent_egr"
            base_value = base_amount
        elif "unit" in type_str:
            calc_type = "per_unit"
            base_value = base_amount
        elif "total" in type_str:
            # For "Total" type: column E is the pro forma assumption and
            # column G is the actual Year 1 computed amount.  These can
            # differ when RedIQ applies detailed calculations (e.g., RE Tax
            # reassessment computes tax from assessed value * millage rate,
            # which may differ from the pro forma input).
            # Prefer G when it exists and is a valid dollar amount:
            #  - E < 1 (intermediate value like millage or inflation rate)
            #  - G greatly exceeds E (> 10x)
            #  - E and G are both dollar amounts but differ (reassessment)
            calc_type = "fixed_annual"
            if yr1_amount and yr1_amount > 1:
                if base_amount < 1 or yr1_amount > base_amount * 10:
                    # E is a rate/millage, G is the dollar amount
                    base_value = yr1_amount
                elif abs(yr1_amount - base_amount) > 1 and base_amount > 1:
                    # Both are dollar amounts but differ (e.g., reassessed RE Tax).
                    # Column G is the actual calculated value; prefer it.
                    base_value = yr1_amount
                else:
                    base_value = base_amount
            else:
                base_value = base_amount
        else:
            if abs(base_amount) < 1:
                calc_type = "percent_egr"
            else:
                calc_type = "fixed_annual"
            base_value = base_amount

        # Mark utility categories as recoverable
        is_utility = any(u in name_lower for u in _utility_names)
        recoverable = is_utility

        # Apply appropriate inflation rate.
        # For RE Tax, derive growth from the Input sheet's Yr1 (col G) vs Yr2
        # (col H) values when available.  Reassessed taxes often have 0% growth
        # for the hold period, which differs from the stated RE Tax inflation rate.
        # Also scan Yr3+ (cols I, J, K) for mid-hold reassessment step changes.
        is_re_tax = "tax" in name_lower and ("real estate" in name_lower or "re tax" in name_lower)
        growth_phases = None
        growth_derived_from_amounts = False
        if is_re_tax and yr1_amount and yr1_amount > 1:
            yr2_amount = _to_float(_safe_value(ws.cell(row=row_idx, column=8)))  # col H
            if yr2_amount and yr2_amount > 0:
                growth = round(yr2_amount / yr1_amount - 1, 6)
                growth_derived_from_amounts = True
                # Check Yr3+ for mid-hold reassessment (>5% step change)
                yr3_amount = _to_float(_safe_value(ws.cell(row=row_idx, column=9)))  # col I
                if (yr3_amount and yr3_amount > 0 and yr2_amount > 0
                        and abs(yr3_amount / yr2_amount - 1) > 0.05):
                    # Mid-hold step change detected — use two-phase growth
                    phase1_rate = round(yr2_amount / yr1_amount - 1, 6)
                    phase2_rate = round(yr3_amount / yr2_amount - 1, 6)
                    # Check if Yr4 (col J) confirms the new rate stabilized
                    yr4_amount = _to_float(_safe_value(ws.cell(row=row_idx, column=10)))  # col J
                    if yr4_amount and yr4_amount > 0 and yr3_amount > 0:
                        phase2_rate = round(yr4_amount / yr3_amount - 1, 6)
                    growth_phases = [
                        {"through_year": 1, "rate": phase1_rate},
                        {"from_year": 2, "rate": phase2_rate},
                    ]
                    growth = phase1_rate  # default growth for backward compat
            else:
                growth = re_tax_infl
        elif is_re_tax:
            growth = re_tax_infl
        else:
            growth = general_infl
        if calc_type == "percent_egr":
            growth = 0.0

        start_yr = re_tax_start_yr if is_re_tax else general_start_yr
        growth_found = growth_derived_from_amounts or (re_tax_infl_found if is_re_tax else general_infl_found)

        cat = {
            "category_name": name_str,
            "calculation_type": calc_type,
            "base_value": base_value,
            "recoverable_flag": recoverable,
        }
        if growth_phases:
            cat["growth_phases"] = growth_phases
            # Also include first phase rate as growth_rate for backward compat
            cat["growth_rate"] = growth_phases[0]["rate"]
        elif growth_found:
            cat["growth_rate"] = growth
        if ((growth_found and growth != 0) or growth_phases) and start_yr > 0:
            cat["growth_start_year"] = start_yr
        categories.append(cat)
        # Mark this category's CF Calcs row as claimed so the fallback cannot
        # assign the same row value to a later category in this extraction pass.
        _cf_row = _CF_OPEX_ROWS.get(name_lower)
        if _cf_row is not None:
            claimed_rows.add(_cf_row)

    return categories


def extract_debt(wb) -> Tuple[Optional[Dict], Optional[Dict], Optional[Dict]]:
    """Extract debt terms for up to 3 loans."""
    loans = []

    # Loan 1 (primary)
    loan1_amount = _to_float(_resolve_named_range(wb, "Loan1Amount"))
    if loan1_amount and loan1_amount > 0:
        amort = _to_int(_resolve_named_range(wb, "Loan1AmortPer"), 0)
        # Interest rate: try IntRate first, fall back to BseRate + IntSprd
        rate = _to_float(_resolve_named_range(wb, "Loan1IntRate"))
        if rate is None or rate == 0:
            bse = _to_float(_resolve_named_range(wb, "Loan1BseRate"), 0)
            sprd = _to_float(_resolve_named_range(wb, "Loan1IntSprd"), 0)
            rate = bse + sprd
        term = _to_int(_resolve_named_range(wb, "Loan1Term"), 120)
        # If amort=0, this is IO-only. Set amort_years = term/12 (schema min 1)
        amort_years = amort if amort and amort > 0 else max(1, (term or 120) // 12)
        loan1 = {
            "commitment": loan1_amount,
            "rate": rate if rate else _warn_default("Loan rate not found; defaulting to 5.0%", 0.05),
            "amort_years": amort_years,
        }
        # If amort=0, the full term is IO
        io_months = _to_int(_resolve_named_range(wb, "Loan1IOPer"))
        if amort == 0 and term:
            io_months = term
        if io_months:
            loan1["io_months"] = io_months
        if term and term > 0:
            loan1["term_months"] = term
        start = _resolve_named_range(wb, "Loan1OrigMnth")
        if start:
            month_str = _to_month_str(start)
            if not month_str and isinstance(start, (int, float)) and start > 0:
                # OrigMnth is a non-zero relative offset; fall back to OrigDate
                orig_date = _resolve_named_range(wb, "Loan1OrigDate")
                if orig_date:
                    month_str = _to_month_str(orig_date)
            if month_str:
                loan1["loan_start_month"] = month_str
        loans.append(loan1)

    # Loan 2
    loan2_amount = _to_float(_resolve_named_range(wb, "Loan2Amount"))
    if loan2_amount and loan2_amount > 0:
        amort = _to_int(_resolve_named_range(wb, "Loan2AmortPer"), 0)
        rate = _to_float(_resolve_named_range(wb, "Loan2IntRate"))
        if rate is None or rate == 0:
            bse = _to_float(_resolve_named_range(wb, "Loan2BseRate"), 0)
            sprd = _to_float(_resolve_named_range(wb, "Loan2IntSprd"), 0)
            rate = bse + sprd
        term = _to_int(_resolve_named_range(wb, "Loan2Term"), 120)
        amort_years = amort if amort and amort > 0 else max(1, (term or 120) // 12)
        loan2 = {
            "commitment": loan2_amount,
            "rate": rate if rate else _warn_default("Loan rate not found; defaulting to 5.0%", 0.05),
            "amort_years": amort_years,
        }
        io_months = _to_int(_resolve_named_range(wb, "Loan2IOPer"))
        if amort == 0 and term:
            io_months = term
        if io_months:
            loan2["io_months"] = io_months
        if term and term > 0:
            loan2["term_months"] = term
        start = _resolve_named_range(wb, "Loan2OrigMnth")
        if start:
            month_str = _to_month_str(start)
            if not month_str and isinstance(start, (int, float)) and start > 0:
                orig_date = _resolve_named_range(wb, "Loan2OrigDate")
                if orig_date:
                    month_str = _to_month_str(orig_date)
            if month_str:
                loan2["loan_start_month"] = month_str
        loans.append(loan2)

    # Loan 3
    loan3_amount = _to_float(_resolve_named_range(wb, "Loan3Amount"))
    if loan3_amount and loan3_amount > 0:
        amort = _to_int(_resolve_named_range(wb, "Loan3AmortPer"), 0)
        rate = _to_float(_resolve_named_range(wb, "Loan3IntRate"))
        if rate is None or rate == 0:
            bse = _to_float(_resolve_named_range(wb, "Loan3BseRate"), 0)
            sprd = _to_float(_resolve_named_range(wb, "Loan3IntSprd"), 0)
            rate = bse + sprd
        term = _to_int(_resolve_named_range(wb, "Loan3Term"), 120)
        amort_years = amort if amort and amort > 0 else max(1, (term or 120) // 12)
        loan3 = {
            "commitment": loan3_amount,
            "rate": rate if rate else _warn_default("Loan rate not found; defaulting to 5.0%", 0.05),
            "amort_years": amort_years,
        }
        io_months = _to_int(_resolve_named_range(wb, "Loan3IOPer"))
        if amort == 0 and term:
            io_months = term
        if io_months:
            loan3["io_months"] = io_months
        if term and term > 0:
            loan3["term_months"] = term
        start = _resolve_named_range(wb, "Loan3OrigMnth")
        if start:
            month_str = _to_month_str(start)
            if not month_str and isinstance(start, (int, float)) and start > 0:
                orig_date = _resolve_named_range(wb, "Loan3OrigDate")
                if orig_date:
                    month_str = _to_month_str(orig_date)
            if month_str:
                loan3["loan_start_month"] = month_str
        loans.append(loan3)

    # Assumable loan — extract only when the deal flags it as active.
    # HasAssumableDebt (GenValidation BF10) is the authoritative flag.
    # If it's explicitly False, the Input sheet may still contain loan terms
    # (e.g., for reference) but the analyst chose not to model them.
    assum_bal = _to_float(_resolve_named_range(wb, "AssumCurrBal"))
    has_assumable = _resolve_named_range(wb, "HasAssumableDebt")
    # Treat None as "unknown" — extract if balance exists; treat False/0 as explicit no
    assumable_active = has_assumable is None or bool(has_assumable)
    if assum_bal and assum_bal > 0 and assumable_active:
        assum_rate = _to_float(_resolve_named_range(wb, "AssumIntRate"), 0)
        assum_amort = _to_int(_resolve_named_range(wb, "AssumAmortPer"), 30)
        assum = {
            "commitment": assum_bal,
            "rate": assum_rate,
            "amort_years": assum_amort,
            "is_assumable": True,
        }
        # Remaining term — determines when this loan matures/pays off
        remain_term = _to_int(_resolve_named_range(wb, "AssumRemainTerm"))
        # IO period — for assumable loans the IO period from the original loan
        # has typically already expired by the time of assumption.
        # Only set io_months if the IO period hasn't expired yet relative to
        # the remaining term.  Most assumed loans are already amortizing.
        io = _to_int(_resolve_named_range(wb, "AssumeIOPer"), 0)
        orig_term = _to_int(_resolve_named_range(wb, "AssumLoanTerm"), 0)
        if io and orig_term and remain_term:
            elapsed = orig_term - remain_term
            remaining_io = max(0, io - elapsed)
            if remaining_io > 0:
                assum["io_months"] = remaining_io
        paydown_month = _to_int(_resolve_named_range(wb, "AssumPydwnMnth"))
        term = remain_term or paydown_month
        if term and term > 0:
            assum["term_months"] = term

        # Fixed monthly payment — for assumable loans the amortization payment
        # was computed from the ORIGINAL loan balance, not the current balance.
        # If we can read the original balance, compute the fixed payment so the
        # engine uses it instead of deriving from the (lower) current balance.
        assum_orig_bal = _to_float(_resolve_named_range(wb, "AssumOrigBal"))
        if assum_orig_bal and assum_orig_bal > 0 and assum_rate and assum_amort:
            mr = assum_rate / 12
            n = assum_amort * 12
            if mr > 0 and n > 0:
                opr_n = (1 + mr) ** n
                fixed_pmt = assum_orig_bal * mr * opr_n / (opr_n - 1)
                assum["fixed_monthly_payment"] = round(fixed_pmt, 2)

        # Assumable loan starts at Day 0 (analysis start)
        # Insert at front so it's the first loan if no originated loans exist
        if not loans:
            loans.append(assum)
        else:
            loans.insert(0, assum)  # Assumable first, then originated

    return loans


def extract_renovation_programs(wb, start_date: str = None, layout: Optional[dict] = None) -> List[Dict[str, Any]]:
    """Extract renovation / value-add program data.

    Reads columns G-M (Years 1-7 unit counts) and creates one program per
    active year-segment per floor plan. This allows the engine to model the
    year-by-year renovation pace from RedIQ.

    Also reads per-row downtime_days from column C if available, otherwise
    defaults to 30 days.

    Federation-emitted output_cohorts use ``_postreno`` suffix to avoid
    collision with rent-roll-derived ``*_renovated`` subtotals (Wave 2
    schema_mapper convention; see the schema audit Bug 1.1 / Family A). The
    suffix matches plat-agent's `schema_mapper.build_renovation_programs`.

    Args:
        wb: openpyxl workbook
        start_date: Analysis start date (YYYY-MM-DD or YYYY-MM). Used to
            compute per-year start/end months. If None, programs get no
            timing info and must be set by caller.
    """
    ws = wb["Input"]
    programs = []

    # Parse start_date for year-segment timing
    start_dt = None
    if start_date:
        from dateutil.relativedelta import relativedelta
        sd = start_date[:10]
        start_dt = date.fromisoformat(sd) if len(sd) == 10 else date.fromisoformat(sd + "-01")

    # Year columns: G=7 (Yr1), H=8 (Yr2), ... M=13 (Yr7)
    year_columns = list(range(7, 14))  # columns G through M

    # Renovation cost inflation: RedIQ grows renovation costs at the general
    # inflation rate for Year 2+.  Read from the inflation row in Input sheet.
    infl_row = layout["inflation_row"] if layout else REDIQ_LAYOUT["inflation"]["general_row"]
    reno_inflation = 0.0
    for col_idx in range(7, 18):
        gi = _to_float(_safe_value(ws.cell(row=infl_row, column=col_idx)))
        if gi and gi > 0:
            reno_inflation = gi
            break

    reno_start = layout["renovations_start"] if layout else REDIQ_LAYOUT["renovations"]["start_row"]
    reno_end = reno_start + 60  # Scan up to 60 rows (some deals have 30+ floor plans)

    consecutive_blanks = 0
    for row_idx in range(reno_start, reno_end):
        fp_name = _to_str(_safe_value(ws[f"B{row_idx}"]))
        if not fp_name:
            consecutive_blanks += 1
            if consecutive_blanks >= 3:
                break  # 3+ consecutive blanks = end of section
            continue  # Skip blank rows (headers, spacers)
        consecutive_blanks = 0

        # Skip total/average rows
        if fp_name.lower().startswith("total") or fp_name.lower().startswith("average"):
            break

        cost = _to_float(_safe_value(ws[f"D{row_idx}"]))
        premium = _to_float(_safe_value(ws[f"E{row_idx}"]))
        if not cost or cost <= 0:
            continue

        # Read downtime days from column C if available, default 30
        downtime = _to_int(_safe_value(ws[f"C{row_idx}"]))
        if not downtime or downtime < 0:
            downtime = 30

        post_market = _to_float(_safe_value(ws[f"F{row_idx}"]))

        # Read units per year from columns G-M
        yearly_units = []
        for col_idx in year_columns:
            val = _to_int(_safe_value(ws.cell(row=row_idx, column=col_idx)))
            yearly_units.append(val if val and val > 0 else 0)

        # Create one program per active year-segment
        has_any_year = False
        for yr_idx, units in enumerate(yearly_units):
            if units <= 0:
                continue
            has_any_year = True
            yr_num = yr_idx + 1

            program = {
                "program_id": f"reno_{fp_name}_yr{yr_num}",
                "program_name": f"Renovation - {fp_name} (Yr{yr_num})",
                "target_cohort": fp_name,
                "output_cohort": f"{fp_name}_postreno",
                "renovation_cost_per_unit": round(cost * (1 + reno_inflation) ** yr_idx, 2),
                "rent_premium_monthly": premium if premium else 0,
                "downtime_days": downtime,
            }

            if post_market:
                program["post_renovation_market_rent"] = post_market

            # Annual unit count → monthly pace + total cap.
            # The Input sheet stores ANNUAL unit counts (e.g., 2 = renovate 2 units
            # that year).  monthly_pace = ceil(units/12) spreads them across 12
            # months, and max_units caps total renovated to the annual target.
            import math
            program["monthly_pace"] = max(1, math.ceil(units / 12))
            program["max_units"] = units

            # Set start/end months for this year segment
            if start_dt:
                from dateutil.relativedelta import relativedelta
                seg_start = start_dt + relativedelta(years=yr_idx)
                seg_end = start_dt + relativedelta(years=yr_idx + 1) - relativedelta(months=1)
                program["start_month"] = seg_start.strftime("%Y-%m")
                program["end_month"] = seg_end.strftime("%Y-%m")

            programs.append(program)

        # Fallback: if no year columns had data but cost/premium exist, skip
        # (no units to renovate means no program)

    return programs


def extract_fund_assumptions(wb, layout: Optional[dict] = None) -> Optional[Dict[str, Any]]:
    """Extract fund-level waterfall assumptions from the Input sheet.

    Uses marker-based scanning within the fund section to find each field
    dynamically. The fund section location varies across deals.
    """
    ws = wb["Input"]

    # Determine fund section start from layout
    if layout and layout.get("fund_start"):
        fund_start = layout["fund_start"]
    else:
        # Try scanning if no layout provided (up to 400 for deals with many FPs)
        fund_start = None
        for marker in ["Sponsor", "GP/LP", "Equity Partner", "Fund Structure"]:
            for col in ["A", "B"]:
                found = _scan_for_row(ws, col, marker, start=200, end=400)
                if found:
                    fund_start = found
                    break
            if fund_start:
                break
        if not fund_start:
            # Final fallback to REDIQ_LAYOUT default
            fund_start = REDIQ_LAYOUT["fund"]["sponsor_pct"][0]

    fund_end = fund_start + 25  # Scan window

    # ── Sponsor equity % ──
    # Look for "Sponsor" row, read from column F (percentage)
    sponsor_row = _scan_for_row(ws, "B", "Sponsor", start=fund_start, end=fund_end)
    if not sponsor_row:
        sponsor_row = _scan_for_row(ws, "A", "Sponsor", start=fund_start, end=fund_end)
    if not sponsor_row:
        sponsor_row = fund_start

    # Try columns F, E, G for the sponsor equity percentage
    sponsor_pct = None
    for col_idx in [6, 5, 7]:  # F, E, G
        val = _to_float(_safe_value(ws.cell(row=sponsor_row, column=col_idx)))
        if val is not None and 0 < val <= 1:
            sponsor_pct = val
            break

    if sponsor_pct is None:
        # Derive from Equity Partner row (e.g., CHDO deals with 0% sponsor equity)
        # Search a few rows above fund_start since EP row is often just above Sponsor
        ep_row = _scan_for_row(ws, "B", "Equity Partner", start=max(1, fund_start - 5), end=fund_end)
        if ep_row:
            for col_idx in [6, 5, 7]:
                val = _to_float(_safe_value(ws.cell(row=ep_row, column=col_idx)))
                if val is not None and 0 < val <= 1:
                    sponsor_pct = round(1.0 - val, 4)
                    break
        if sponsor_pct is None:
            return None  # No fund structure defined

    lp_pct = 1.0 - sponsor_pct

    # ── Hurdle IRR ──
    # Tier III row has the hurdle IRR in column J. The row has "Tier III" in col H.
    hurdle_irr = 0.10  # default
    hurdle_row = (_scan_for_row(ws, "H", "Tier III", start=fund_start - 2, end=fund_end) or
                  _scan_for_row(ws, "B", "IRR", start=fund_start, end=fund_end) or
                  _scan_for_row(ws, "B", "Hurdle", start=fund_start, end=fund_end))
    if hurdle_row:
        for col_idx in [10, 11, 9]:  # J, K, I
            val = _to_float(_safe_value(ws.cell(row=hurdle_row, column=col_idx)))
            if val is not None and 0 < val <= 1:
                hurdle_irr = val
                break
    # Also check the sponsor row (institutional exports place IRR on the same row)
    if hurdle_irr == 0.10:
        for col_idx in [10, 11]:  # J, K
            val = _to_float(_safe_value(ws.cell(row=sponsor_row, column=col_idx)))
            if val is not None and 0 < val <= 1:
                hurdle_irr = val
                break

    # ── Tier IV LP/GP splits ──
    tier4_lp = 0.80
    tier4_gp = 0.20
    tier_row = (_scan_for_row(ws, "H", "Tier IV", start=fund_start, end=fund_end) or
                _scan_for_row(ws, "B", "Tier IV", start=fund_start, end=fund_end) or
                _scan_for_row(ws, "H", "Tier 4", start=fund_start, end=fund_end))
    if not tier_row:
        # Try the row after the sponsor/hurdle row
        tier_row = sponsor_row + 1
    for col_idx in [12, 11]:  # L, K
        val = _to_float(_safe_value(ws.cell(row=tier_row, column=col_idx)))
        if val is not None and 0 < val <= 1:
            tier4_lp = val
            # GP is in the next column
            gp_val = _to_float(_safe_value(ws.cell(row=tier_row, column=col_idx + 1)))
            if gp_val is not None and 0 < gp_val <= 1:
                tier4_gp = gp_val
            else:
                tier4_gp = 1.0 - tier4_lp
            break

    promote_splits = [
        {"tier": "I", "hurdle_irr": 0.0, "lp_share": 1.0, "gp_share": 0.0},
        {"tier": "II", "hurdle_irr": 0.0, "lp_share": 1.0, "gp_share": 0.0},
        {"tier": "III", "hurdle_irr": hurdle_irr, "lp_share": 1.0, "gp_share": 0.0},
        {"tier": "IV", "hurdle_irr": hurdle_irr, "lp_share": tier4_lp, "gp_share": tier4_gp},
    ]

    # ── Fees ──
    # RedIQ places fee labels in column H (right side of the fund section)
    # with percentages in column K and dollar amounts in column L.
    acq_fee = 0.0
    acq_row = (_scan_for_row(ws, "H", "Acq", start=fund_start, end=fund_end) or
               _scan_for_row(ws, "B", "Acquisition Fee", start=fund_start, end=fund_end))
    if acq_row:
        for col_idx in [11, 12, 10, 6]:  # K, L, J, F
            val = _to_float(_safe_value(ws.cell(row=acq_row, column=col_idx)))
            if val is not None and val != 0:
                acq_fee = val
                break

    am_fee = 0.0
    am_row = (_scan_for_row(ws, "H", "Asset M", start=fund_start, end=fund_end) or
              _scan_for_row(ws, "H", "AM Fee", start=fund_start, end=fund_end) or
              _scan_for_row(ws, "B", "Asset Management", start=fund_start, end=fund_end))
    if am_row:
        for col_idx in [11, 12, 10, 6]:  # K, L, J, F
            val = _to_float(_safe_value(ws.cell(row=am_row, column=col_idx)))
            if val is not None and val != 0:
                am_fee = val
                break

    disp_fee = 0.0
    disp_row = (_scan_for_row(ws, "H", "Disposition", start=fund_start, end=fund_end) or
                _scan_for_row(ws, "B", "Disposition Fee", start=fund_start, end=fund_end))
    if disp_row:
        for col_idx in [11, 12, 10, 6]:  # K, L, J, F
            val = _to_float(_safe_value(ws.cell(row=disp_row, column=col_idx)))
            if val is not None and val != 0:
                disp_fee = val
                break

    partnership_exp = 0.0
    ptnr_row = (_scan_for_row(ws, "H", "Partnership Expense", start=fund_start, end=fund_end) or
                _scan_for_row(ws, "H", "Annual Partnership", start=fund_start, end=fund_end) or
                _scan_for_row(ws, "B", "Partnership Expense", start=fund_start, end=fund_end))
    if ptnr_row:
        for col_idx in [12, 11, 10, 7]:  # L, K, J, G
            val = _to_float(_safe_value(ws.cell(row=ptnr_row, column=col_idx)))
            if val is not None and val != 0:
                partnership_exp = val
                break

    return {
        "sponsor_equity_pct": sponsor_pct,
        "lp_equity_pct": lp_pct,
        "preferred_return": hurdle_irr,
        "promote_splits": promote_splits,
        "acquisition_fee_pct": acq_fee,
        "asset_management_fee_pct": am_fee,
        "disposition_fee_pct": disp_fee,
        "annual_partnership_expenses": partnership_exp,
    }


def extract_sensitivity_config(wb) -> Optional[Dict[str, Any]]:
    """Extract sensitivity analysis configuration."""
    cap_rate_input = _to_float(_resolve_named_range(wb, "ExitCapRateSensitivityInput"))
    cap_rate_incr = _to_float(_resolve_named_range(wb, "ExitCapRateSensitivityIncrement"))
    rent_input = _resolve_named_range(wb, "MarketRentSensitivityInput")
    rent_incr = _to_float(_resolve_named_range(wb, "MarketRentSensitivityDollarIncrement"))
    rent_pct = _to_float(_resolve_named_range(wb, "MarketRentSensitivityPcntIncrement"))
    price_incr = _to_float(_resolve_named_range(wb, "PriceSensitivityDollarIncrement"))

    if not cap_rate_input and not rent_input:
        return None

    config = {}
    if cap_rate_input:
        config["exit_cap_rate_base"] = cap_rate_input
        if cap_rate_incr:
            config["exit_cap_rate_step"] = cap_rate_incr
    if rent_incr:
        config["rent_growth_dollar_step"] = rent_incr
    if rent_pct:
        config["rent_growth_pct_step"] = rent_pct
    if price_incr:
        config["purchase_price_step"] = price_incr

    return config


def extract_other_income(wb, total_units: int = 1, layout: Optional[dict] = None) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Extract other income / revenue programs from the Input sheet.

    Returns:
        Tuple of (revenue_programs, reimbursement_items)
        Revenue programs are true other income; reimbursement items
        will be modeled as utility recovery instead.

    Uses layout detection to find the Other Income section dynamically.
    Guards against renovation floor plan rows that can appear in the same area.
    """
    ws = wb["Input"]
    programs = []
    reimbursements = []

    # Determine row range from layout or defaults
    if layout:
        start_row = layout["other_income_start"]
        end_row = layout["other_income_end"]
    else:
        start_row = REDIQ_LAYOUT["other_income"]["start_row"]
        end_row = REDIQ_LAYOUT["other_income"]["end_row"]

    # Reimbursement keywords — these items are still included as revenue programs
    # (they contribute to EGR); we don't separate them out.
    _reimb_keywords = set()  # Disabled: all OI items treated as revenue programs

    # Keywords that indicate renovation rows (NOT other income)
    _reno_skip_keywords = {"selectrenovated", "floorplan", "renovatedfloorplan",
                           "selectreno", "interior upgrade"}

    # Header/label rows to skip (these contain year numbers in G, not dollar amounts)
    _header_skip = {"line items", "income item", "income category", "unit type"}

    # Year columns: G=7 (Yr1), H=8 (Yr2), ... M=13 (Yr7)
    _year_cols = list(range(7, 14))

    for row_idx in range(start_row, end_row):
        # Read from both col A and col B — different deals use different columns
        name_a = _safe_value(ws[f"A{row_idx}"])
        name_b = _safe_value(ws[f"B{row_idx}"])
        name = name_b if (name_b and str(name_b).strip()) else name_a
        if not name or str(name).strip() == "":
            continue

        name_str = str(name).strip()
        name_lower = name_str.lower()

        # Guard: skip renovation floor plan rows that may appear in this range
        col_a_str = str(name_a).lower() if name_a else ""
        if any(kw in name_lower.replace(" ", "") for kw in _reno_skip_keywords):
            continue
        if any(kw in col_a_str.replace(" ", "") for kw in _reno_skip_keywords):
            continue

        # Skip "Total" summary rows
        if name_lower.startswith("total"):
            continue

        # Skip header/label rows (contain year numbers, not data)
        if any(h in name_lower for h in _header_skip):
            continue

        # Read Year 1 value from column G
        amount = _to_float(_safe_value(ws[f"G{row_idx}"]))
        if not amount or amount == 0:
            continue

        unit_basis = _to_str(_safe_value(ws[f"E{row_idx}"]))
        frequency = _to_str(_safe_value(ws[f"F{row_idx}"]))
        # RedIQ G column values are always annualized — treat as annual unless
        # explicitly marked "per month" or "monthly".
        is_monthly = frequency and "month" in str(frequency).lower()
        is_annual = not is_monthly  # Default to annual

        # Determine pricing conversion
        program = {
            "program_id": re.sub(r"[^a-z0-9_]", "_", name_lower),
            "program_name": name_str,
            "eligible_units": "ALL",
        }

        def _convert_amount(val):
            """Convert an annual amount to monthly per-unit for the engine."""
            if unit_basis and "unit" in str(unit_basis).lower():
                return round(val / 12, 2) if is_annual else val
            elif unit_basis and "total" in str(unit_basis).lower():
                monthly = val / 12 if is_annual else val
                return round(monthly / total_units, 2) if total_units > 0 else round(monthly, 2)
            else:
                # No unit basis — treat as total property annual amount
                monthly = val / 12 if is_annual else val
                return round(monthly / total_units, 2) if total_units > 0 else round(monthly, 2)

        if unit_basis and "%" in str(unit_basis):
            program["pricing_type"] = "% rent"
            program["program_type"] = "tenant-based"
            program["price_value"] = amount
        elif unit_basis and "occ" in str(unit_basis).lower():
            # "$ / occ. unit" — revenue scales with occupied units, not total
            program["pricing_type"] = "$/occupied_unit"
            program["program_type"] = "tenant-based"
            program["price_value"] = _convert_amount(amount)
        else:
            program["pricing_type"] = "$/unit"
            program["program_type"] = "tenant-based"
            program["price_value"] = _convert_amount(amount)

        # Read multi-year values from columns H-M (Years 2-7)
        multi_year = [program["price_value"]]  # Year 1
        for col_idx in _year_cols[1:]:  # H=8 through M=13
            yr_val = _to_float(_safe_value(ws.cell(row=row_idx, column=col_idx)))
            if yr_val and yr_val > 0:
                multi_year.append(_convert_amount(yr_val))
            else:
                multi_year.append(None)  # Will use previous year's value
        program["multi_year_values"] = multi_year

        programs.append(program)

    return programs, reimbursements


def extract_capex(wb, start_date: str = "2023-05", yr_of_exit: int = 7, layout: Optional[dict] = None) -> List[Dict[str, Any]]:
    """Extract capital expenditure schedule with multi-year support.

    Uses layout detection to find the CapEx section dynamically.
    For categories with values in multiple years, generates recurring capex
    entries with proper year-aligned start/end months.
    """
    from dateutil.relativedelta import relativedelta

    ws = wb["Input"]
    schedule = []

    start_dt = date.fromisoformat(start_date[:10] if len(start_date) >= 10 else start_date + "-01")

    capex_start = layout["capex_start"] if layout else REDIQ_LAYOUT["capex"]["start_row"]
    capex_end = capex_start + 15  # Scan up to 15 rows

    # Year columns: G=7 (Yr1), H=8 (Yr2), ... M=13 (Yr7)
    year_columns = list(range(7, 14))  # columns G through M
    num_years = min(yr_of_exit, len(year_columns))

    for row_idx in range(capex_start, capex_end):
        name = _safe_value(ws[f"B{row_idx}"])
        if not name or str(name).strip() == "":
            continue

        category = str(name).strip()
        is_reserve = "reserve" in category.lower()

        # Read all year values
        yearly_amounts = []
        for col_idx in year_columns[:num_years]:
            val = _to_float(_safe_value(ws.cell(row=row_idx, column=col_idx)))
            yearly_amounts.append(val if val else 0.0)

        # Check if any year has a non-zero value
        if not any(a != 0 for a in yearly_amounts):
            continue

        if is_reserve:
            # Reserve items: use Year 1 value as annual per-unit amount
            # (same logic as before — reserves are spread monthly by the engine)
            pct = _to_float(_safe_value(ws[f"F{row_idx}"]))
            item = {
                "category": category,
                "capex_type": "reserve",
                "amount": yearly_amounts[0],
            }
            if pct:
                item["amount_per_unit"] = pct
            schedule.append(item)
        else:
            # Non-reserve items: spread each year's amount across 12 months
            # to match RedIQ's monthly CapEx distribution.
            end_dt = start_dt + relativedelta(years=yr_of_exit) - relativedelta(months=1)
            end_date_str = end_dt.strftime("%Y-%m")
            for yr_idx, amount in enumerate(yearly_amounts):
                if amount == 0:
                    continue
                seg_start = start_dt + relativedelta(years=yr_idx)
                monthly_amount = round(amount / 12, 2)
                for mo_offset in range(12):
                    m_date = seg_start + relativedelta(months=mo_offset)
                    m_str = m_date.strftime("%Y-%m")
                    if m_str <= end_date_str:
                        schedule.append({
                            "category": category,
                            "capex_type": "one_time",
                            "amount": monthly_amount,
                            "month": m_str,
                        })

    return schedule


def _extract_rent_growth_from_cf(wb, yr_of_exit: int) -> List[float]:
    """Derive implied rent growth rates from the CF Calculations sheet.

    Reads the "Potential Market Rent" row (row 18) across years and computes
    year-over-year growth rates. Used as a fallback when the Input sheet
    rent growth row returns None or year-number artifacts.
    """
    try:
        cf_ws = wb["CF Calculations"]
    except KeyError:
        return []

    market_rents = []
    for yr_idx in range(yr_of_exit):
        col = 5 + yr_idx  # E=5 for year 1
        val = _to_float(_safe_value(cf_ws.cell(row=18, column=col)))
        market_rents.append(val)

    if not market_rents or market_rents[0] is None or market_rents[0] <= 0:
        return []

    growth_rates = [0.0]  # Year 1 has no prior year to compare
    for i in range(1, len(market_rents)):
        if market_rents[i] and market_rents[i - 1] and market_rents[i - 1] > 0:
            rate = (market_rents[i] - market_rents[i - 1]) / market_rents[i - 1]
            growth_rates.append(round(rate, 6))
        else:
            growth_rates.append(0.0)

    return growth_rates


def _validate_rent_growth(values: List[float], yr_of_exit: int) -> bool:
    """Check if extracted rent growth values look valid.

    Returns False if values appear to be year numbers (1,2,3,...) or are all None/zero.
    """
    if not values:
        return False

    # Check for year-number artifact: values are integers 1-11
    non_zero = [v for v in values if v and v != 0]
    if non_zero and all(v == int(v) and 1 <= v <= 11 for v in non_zero):
        return False

    # Check for all None/zero
    if all(v is None or v == 0 for v in values):
        return False

    # Check for unreasonable values (> 50% growth or negative)
    if any(v is not None and abs(v) > 0.5 for v in values):
        return False

    return True


def extract_inputs_from_rediq(
    workbook_path: str | Path,
    data_only: bool = True,
) -> Dict[str, Any]:
    """
    Extract all inputs from a RedIQ workbook and map to canonical engine schema.

    Uses marker-based layout detection to handle different Input sheet layouts
    across RedIQ models. Falls back to REDIQ_LAYOUT defaults when markers
    aren't found.

    Args:
        workbook_path: Path to RedIQ .xlsm file
        data_only: If True, read calculated values (not formulas)

    Returns:
        Dict matching the canonical deal schema for the underwriting engine
    """
    wb_path = Path(workbook_path)
    # NOTE: read_only=True streams cells from the ZIP rather than loading the
    # entire workbook into memory. This dramatically reduces memory pressure
    # for large RedIQ workbooks (>5MB). Because cells stream lazily, wb.close()
    # MUST run only after all reads complete — see the try/finally below.
    # keep_vba=True is incompatible with read_only=True (per openpyxl docs);
    # we don't write VBA back, so dropping it is safe.
    wb = openpyxl.load_workbook(wb_path, data_only=data_only, read_only=True, keep_links=True)

    try:
        # ── Detect Input sheet layout ──
        if "Input" not in wb.sheetnames:
            raise ValueError(f"Workbook {wb_path.name} missing required 'Input' sheet")
        ws_input = wb["Input"]
        layout = _detect_layout(ws_input)

        from dateutil.relativedelta import relativedelta

        # --- Metadata ---
        deal_name = _to_str(_resolve_named_range(wb, "DealName"), "Unknown Deal")
        counter_id = _to_str(_resolve_named_range(wb, "CounterId"), "001")
        analysis_start = _resolve_named_range(wb, "AnalysisStartDate")
        exit_date = _resolve_named_range(wb, "DateOfExit")
        yr_of_exit = _to_int(_resolve_named_range(wb, "YrOfExit"), 10)
        analyst = _to_str(_resolve_named_range(wb, "CreatedBy"), "Unknown")

        # Build time grid from analysis start date and exit year
        if isinstance(analysis_start, (date, datetime)):
            d = analysis_start if isinstance(analysis_start, date) else analysis_start.date()
            start_date = d.replace(day=1).isoformat()
        elif isinstance(analysis_start, str):
            start_date = analysis_start[:7] + "-01" if len(analysis_start) >= 7 else analysis_start
        else:
            start_date = _warn_default("Analysis start date not found; defaulting to 2025-01-01", "2025-01-01")

        # Compute analysis_end_date = start + yr_of_exit years - 1 month.
        # RedIQ's analysis period for a 5-year hold starting May 2026 is May 2026 - Apr 2031
        # (60 months).  The exit DATE (May 2031) is when the sale closes, but the last
        # analysis month is April 2031.  Exit proceeds are included in that last month.
        _exit_date_valid = False
        if isinstance(exit_date, (date, datetime)):
            d = exit_date if isinstance(exit_date, date) else exit_date.date()
            if d.year >= 1950:  # Guard against Excel epoch dates from broken formulas
                # Use one month before exit date as analysis end
                exit_dt = d.replace(day=1)
                end_dt_calc = exit_dt - relativedelta(months=1)
                end_date = end_dt_calc.isoformat()
                _exit_date_valid = True
        elif isinstance(exit_date, str) and len(exit_date) >= 7:
            try:
                _ed = date.fromisoformat(exit_date[:10] if len(exit_date) >= 10 else exit_date)
                if _ed.year >= 1950:
                    exit_dt = _ed.replace(day=1)
                    end_dt_calc = exit_dt - relativedelta(months=1)
                    end_date = end_dt_calc.isoformat()
                    _exit_date_valid = True
            except ValueError:
                pass

        if not _exit_date_valid:
            try:
                sd = date.fromisoformat(start_date)
                # start + yr_of_exit years - 1 month
                end_dt_calc = sd + relativedelta(years=yr_of_exit) - relativedelta(months=1)
                end_date = end_dt_calc.isoformat()
                warnings.warn(
                    f"RedIQ Bridge: DateOfExit invalid ({exit_date}); "
                    f"computed from start + {yr_of_exit} years - 1mo → {end_date}",
                    stacklevel=2,
                )
            except Exception:
                end_date = _warn_default("Analysis end date not found; defaulting to 2035-01-01", "2035-01-01")

        # --- Unit cohorts ---
        cohorts = extract_floor_plans(wb, layout=layout)

        # --- Market rent growth rates (year-by-year) ---
        # Use layout-detected rent growth row
        rent_growth_row = layout["rent_growth_row"]
        yearly_rent_growth = []
        for col_idx in range(7, 18):  # G=7 through Q=17 (up to 11 years)
            val = _to_float(_safe_value(ws_input.cell(row=rent_growth_row, column=col_idx)))
            yearly_rent_growth.append(val if val else 0.0)

        # Validate rent growth values — detect year-number confusion or all-None
        if not _validate_rent_growth(yearly_rent_growth, yr_of_exit):
            warnings.warn(
                f"RedIQ Bridge: Rent growth row {rent_growth_row} returned invalid values "
                f"{yearly_rent_growth[:7]}; falling back to CF Calculations sheet",
                stacklevel=2,
            )
            cf_growth = _extract_rent_growth_from_cf(wb, yr_of_exit)
            if cf_growth:
                yearly_rent_growth = cf_growth
            else:
                # Ultimate fallback: 3% flat growth
                default_growth = _warn_default(
                    "Rent growth not available from Input or CF Calculations; using 3%", 0.03
                )
                yearly_rent_growth = [default_growth] * yr_of_exit

        # --- Market rent curves (piecewise, reflecting year-by-year growth) ---
        from dateutil.relativedelta import relativedelta
        start_dt = date.fromisoformat(start_date)
        end_dt = date.fromisoformat(end_date)
        market_rent_curve = []
        for cohort in cohorts:
            base_mr = cohort.get("market_rent") or cohort.get("starting_market_rent") or cohort.get("initial_inplace_rent", 0)
            cumulative_mr = base_mr
            for yr_idx in range(yr_of_exit):
                yr_start = start_dt + relativedelta(years=yr_idx)
                next_yr_start = start_dt + relativedelta(years=yr_idx + 1)
                if next_yr_start >= end_dt:
                    yr_end = end_dt
                else:
                    yr_end = next_yr_start - relativedelta(months=1)
                growth = yearly_rent_growth[yr_idx] if yr_idx < len(yearly_rent_growth) else 0.0
                cumulative_mr = cumulative_mr * (1 + growth)
                market_rent_curve.append({
                    "cohort_id": cohort["cohort_id"],
                    "start_period": yr_start.strftime("%Y-%m"),
                    "end_period": yr_end.strftime("%Y-%m"),
                    "market_rent": round(cumulative_mr, 2),
                })

        # --- Calibrate market rent to CF Calculations ---
        # The Input sheet column L gives base market rents, but RedIQ applies monthly
        # rent growth and may use different reno premiums. Calibrate per-year totals
        # to match CF Calculations row 18 (Potential Market Rent).
        try:
            cf_ws_mr = wb["CF Calculations"]
            for yr_idx in range(yr_of_exit):
                col = 5 + yr_idx  # E=5 for Year 1
                cf_mkt = _to_float(cf_ws_mr.cell(row=18, column=col).value, 0)
                if cf_mkt <= 0:
                    continue
                yr_start = start_dt + relativedelta(years=yr_idx)
                next_yr = start_dt + relativedelta(years=yr_idx + 1)
                yr_end = end_dt if next_yr >= end_dt else next_yr - relativedelta(months=1)
                yr_start_str = yr_start.strftime("%Y-%m")
                yr_end_str = yr_end.strftime("%Y-%m")
                # Sum engine market rent for this year across cohorts
                engine_mkt = sum(
                    seg["market_rent"] * next(
                        (c["unit_count"] for c in cohorts if c["cohort_id"] == seg["cohort_id"]), 0
                    ) * 12
                    for seg in market_rent_curve
                    if seg["start_period"] == yr_start_str and seg["end_period"] == yr_end_str
                )
                if engine_mkt > 0:
                    factor = cf_mkt / engine_mkt
                    if abs(factor - 1.0) > 0.001:
                        for seg in market_rent_curve:
                            if seg["start_period"] == yr_start_str and seg["end_period"] == yr_end_str:
                                seg["market_rent"] = round(seg["market_rent"] * factor, 2)
        except (KeyError, Exception):
            pass  # CF Calculations not available; use uncalibrated values

        # --- Loss factors ---
        loss_factors = extract_loss_factors(wb)
        ltl_pct = loss_factors.get("loss_to_lease", 0)
        vacancy_pct = loss_factors.get("structural_vacancy", 0)
        collection_pct = loss_factors.get("collection_loss", 0)

        cohort_ids = [c["cohort_id"] for c in cohorts]

        # Always prefer CF Calculations derived loss curves — they reflect the actual
        # RedIQ model with year-varying rates. Named range percentages can be misleading
        # (e.g., "collection loss" named range may report a gross input rate that RedIQ
        # doesn't actually apply directly).
        cf_curves = extract_cf_calc_loss_curves(
            wb, start_date, end_date, cohort_ids, yr_of_exit
        )
        if cf_curves and cf_curves.get("loss_to_lease"):
            loss_to_lease = cf_curves["loss_to_lease"]
            physical_vacancy = cf_curves["physical_vacancy"]
            collection_loss = cf_curves["collection_loss"]
        elif ltl_pct > 0 or vacancy_pct > 0 or collection_pct > 0:
            # Fallback to named ranges if CF Calculations unavailable
            loss_to_lease = []
            physical_vacancy = []
            collection_loss = []
            for cohort in cohorts:
                loss_to_lease.append({
                    "cohort_id": cohort["cohort_id"],
                    "start_period": start_date[:7],
                    "end_period": end_date[:7],
                    "ltl_percent": ltl_pct,
                })
                physical_vacancy.append({
                    "cohort_id": cohort["cohort_id"],
                    "start_period": start_date[:7],
                    "end_period": end_date[:7],
                    "vacancy_rate": vacancy_pct,
                })
            collection_loss.append({
                "applies_to": "ALL",
                "start_period": start_date[:7],
                "end_period": end_date[:7],
                "loss_rate": collection_pct,
            })
        else:
            loss_to_lease = [{"cohort_id": c, "start_period": start_date[:7],
                              "end_period": end_date[:7], "ltl_percent": 0} for c in cohort_ids]
            physical_vacancy = [{"cohort_id": c, "start_period": start_date[:7],
                                 "end_period": end_date[:7], "vacancy_rate": 0} for c in cohort_ids]
            collection_loss = [{"applies_to": "ALL", "start_period": start_date[:7],
                                "end_period": end_date[:7], "loss_rate": 0}]

        # --- Other income / revenue programs ---
        # Use CF Calculations total OI (row 32) per year as the authoritative source.
        # OI in RedIQ is added AFTER vacancy/loss deductions, but the engine applies
        # vacancy to revenue programs. To compensate, gross up the OI amount by the
        # combined loss rate so the engine's post-loss result matches RedIQ's total.
        if not cohorts:
            raise ValueError(f"No unit cohorts extracted from {wb_path.name}; check Floor Plans section")
        total_units = sum(c["unit_count"] for c in cohorts)
        if total_units <= 0:
            raise ValueError(f"Total units is 0 in {wb_path.name}; cannot proceed")
        revenue_programs_final = []
        program_adoption = []

        cf_ws_oi = None
        try:
            cf_ws_oi = wb["CF Calculations"]
            cf_oi_available = True
        except KeyError:
            cf_oi_available = False

        if cf_oi_available:
            for yr_idx in range(yr_of_exit):
                col = 5 + yr_idx  # E=5 for Year 1
                total_oi = _to_float(cf_ws_oi.cell(row=32, column=col).value, 0)
                if total_oi <= 0:
                    continue

                yr_start = start_dt + relativedelta(years=yr_idx)
                next_yr = start_dt + relativedelta(years=yr_idx + 1)
                yr_end = end_dt if next_yr >= end_dt else next_yr - relativedelta(months=1)

                # Use $/asset pricing — flat monthly amount not multiplied by units.
                # The engine only applies collection loss to programs (not vacancy/LTL),
                # and our CF-derived loss curves set collection loss to 0, so the
                # OI will flow through at the exact RedIQ amount.
                monthly_amount = total_oi / 12

                seg_id = f"other_income_yr{yr_idx + 1}"
                revenue_programs_final.append({
                    "program_id": seg_id,
                    "program_name": f"Other Income (Yr{yr_idx + 1})",
                    "program_type": "tenant-based",
                    "pricing_type": "$/asset",
                    "price_value": round(monthly_amount, 2),
                    "eligible_units": "ALL",
                    "start_period": yr_start.strftime("%Y-%m"),
                    "end_period": yr_end.strftime("%Y-%m"),
                })
                program_adoption.append({
                    "program_id": seg_id,
                    "start_period": yr_start.strftime("%Y-%m"),
                    "end_period": yr_end.strftime("%Y-%m"),
                    "adoption_rate": 1.0,
                })
        else:
            # Fallback: extract individual items from Input sheet
            other_income, _ = extract_other_income(wb, total_units=total_units, layout=layout)
            for prog in other_income:
                for yr_idx in range(yr_of_exit):
                    yr_start = start_dt + relativedelta(years=yr_idx)
                    next_yr = start_dt + relativedelta(years=yr_idx + 1)
                    yr_end = end_dt if next_yr >= end_dt else next_yr - relativedelta(months=1)
                    multi_yr = prog.get("multi_year_values")
                    if multi_yr and yr_idx < len(multi_yr) and multi_yr[yr_idx] is not None:
                        price = multi_yr[yr_idx]
                    else:
                        price = prog["price_value"]
                    seg_id = f"{prog['program_id']}_yr{yr_idx + 1}"
                    revenue_programs_final.append({
                        "program_id": seg_id,
                        "program_name": f"{prog['program_name']} (Yr{yr_idx + 1})",
                        "program_type": prog["program_type"],
                        "pricing_type": prog["pricing_type"],
                        "price_value": round(price, 2),
                        "eligible_units": prog["eligible_units"],
                        "start_period": yr_start.strftime("%Y-%m"),
                        "end_period": yr_end.strftime("%Y-%m"),
                    })
                    program_adoption.append({
                        "program_id": seg_id,
                        "start_period": yr_start.strftime("%Y-%m"),
                        "end_period": yr_end.strftime("%Y-%m"),
                        "adoption_rate": 1.0,
                    })

        # --- Purchase assumptions ---
        purchase_price = _to_float(_resolve_named_range(wb, "PurchasePrice"))
        closing_costs = _to_float(_resolve_named_range(wb, "TotalOtherCCAcq"), 0)

        # --- Debt ---
        loans = extract_debt(wb)

        # Validate debt against CF Calculations: if debt service is $0 for all years,
        # the deal has no active debt (named ranges may contain residual/formula values).
        if loans and cf_oi_available:
            has_debt_service = False
            for yr_idx in range(yr_of_exit):
                col = 5 + yr_idx  # E=5 for Year 1
                ds_val = _to_float(cf_ws_oi.cell(row=92, column=col).value, 0)
                if ds_val and abs(ds_val) > 0:
                    has_debt_service = True
                    break
            if not has_debt_service:
                _warn_default(
                    f"Debt extracted (${loans[0].get('commitment', 0):,.0f}) but CF Calculations "
                    f"shows zero debt service. Suppressing phantom debt.", None
                )
                loans = []

        # --- OpEx (uses layout) ---
        opex = extract_opex(wb, layout=layout)

        # --- CapEx (uses layout) ---
        capex = extract_capex(wb, start_date=start_date, yr_of_exit=yr_of_exit, layout=layout)

        # --- Renovations ---
        renovations = extract_renovation_programs(wb, start_date=start_date, layout=layout)

        # --- Fund assumptions (uses layout) ---
        fund_assumptions = extract_fund_assumptions(wb, layout=layout)

        # --- Sensitivity ---
        sensitivity = extract_sensitivity_config(wb)

        # --- Exit cap rate, sale costs, and reversion NOI ---
        exit_cap = _to_float(_resolve_named_range(wb, "ExitCapRate"))
        sale_commission = _to_float(_resolve_named_range(wb, "SalesCommission"))
        pct_other_cc_disp = _to_float(_resolve_named_range(wb, "PctOtherCCDisp"), 0)
        total_sale_cost_pct = (sale_commission or 0) + pct_other_cc_disp
        reversion_noi = _to_float(_resolve_named_range(wb, "ReversionNOI"))

        # --- Partnership Closing Costs (CF Calcs row 113, Day 0 column) ---
        # These are fund-level costs (legal, organization) that add to the
        # waterfall's initial equity balance.  Separate from property closing costs.
        partnership_closing_costs_day0 = 0.0
        if cf_oi_available:
            pcc_val = _to_float(cf_ws_oi.cell(row=113, column=4).value, 0)
            if pcc_val:
                partnership_closing_costs_day0 = abs(pcc_val)

        # --- Replacement Reserves (CF Calcs row 60) ---
        replacement_reserves = []
        if cf_oi_available:
            for yr_idx in range(yr_of_exit):
                col = 5 + yr_idx
                val = abs(_to_float(cf_ws_oi.cell(row=60, column=col).value, 0))
                if val > 0:
                    yr_start = start_dt + relativedelta(years=yr_idx)
                    next_yr = start_dt + relativedelta(years=yr_idx + 1)
                    yr_end = end_dt if next_yr >= end_dt else next_yr - relativedelta(months=1)
                    replacement_reserves.append({
                        "start_period": yr_start.strftime("%Y-%m"),
                        "end_period": yr_end.strftime("%Y-%m"),
                        "annual_amount": val,
                    })

        # --- Loan Draw Schedule (CF Calcs row 90) ---
        # For construction/rehab loans, lender draws are staged — they don't draw
        # the full commitment on Day 0. Row 90 shows annual draw amounts.
        # We emit monthly draws (Day 0 draw + annual draws spread monthly).
        #
        # IMPORTANT: Row 90 is aggregated across ALL loans. If a mid-hold refi
        # exists, post-maturity draws belong to the refi loan (additional_debt),
        # not the primary.  The additional loan auto-draws its commitment at
        # loan_start_month, so we exclude those from the primary's draw schedule.
        debt_draw_schedule = []

        # Detect refi event: primary loan matures mid-hold and additional loan starts
        primary_maturity_month = None
        if loans and len(loans) > 1 and loans[0].get("is_assumable"):
            assum_term = loans[0].get("term_months", 0)
            if assum_term and assum_term > 0:
                maturity_dt = start_dt + relativedelta(months=assum_term - 1)
                primary_maturity_month = maturity_dt.strftime("%Y-%m")

        if cf_oi_available:
            # Day 0 draw (column D = 4)
            day0_draw = _to_float(cf_ws_oi.cell(row=90, column=4).value, 0)
            if day0_draw and day0_draw > 0:
                # Check if primary is fully drawn at Day 0 (no staged drawdowns needed).
                # Row 90 aggregates ALL loans — if the primary's Day 0 draw matches its
                # commitment, any subsequent Row 90 draws are for other loans (e.g., refi).
                primary_commitment = loans[0].get("commitment", 0) if loans else 0
                primary_fully_drawn = (
                    primary_commitment > 0
                    and abs(day0_draw - primary_commitment) / primary_commitment < 0.01
                )

                # Check if there are subsequent draws (indicating staged drawdowns)
                subsequent_draws = []
                if not primary_fully_drawn or not primary_maturity_month:
                    # Only look for staged draws if primary is NOT fully drawn,
                    # OR if there's no refi (maturity_month is None).
                    for yr_idx in range(yr_of_exit):
                        col = 5 + yr_idx
                        val = _to_float(cf_ws_oi.cell(row=90, column=col).value, 0)
                        if val and val > 0:
                            subsequent_draws.append((yr_idx, val))

                if subsequent_draws:
                    # Staged drawdown — Day 0 draw + annual draws spread monthly
                    debt_draw_schedule.append({
                        "month": start_date[:7],
                        "draw_amount": day0_draw,
                    })
                    for yr_idx, annual_draw in subsequent_draws:
                        yr_start = start_dt + relativedelta(years=yr_idx)
                        next_yr = start_dt + relativedelta(years=yr_idx + 1)
                        yr_end = end_dt if next_yr >= end_dt else next_yr - relativedelta(months=1)

                        # Spread annual draw evenly across months in the year
                        yr_months = []
                        current = yr_start
                        while current <= yr_end:
                            yr_months.append(current.strftime("%Y-%m"))
                            current += relativedelta(months=1)
                        if yr_months:
                            monthly_draw = annual_draw / len(yr_months)
                            for m in yr_months:
                                debt_draw_schedule.append({
                                    "month": m,
                                    "draw_amount": round(monthly_draw, 2),
                                })

        # --- Upfront-funded CapEx (CF Calcs row 72) ---
        capex_upfront = []
        if cf_oi_available:
            for yr_idx in range(yr_of_exit):
                col = 5 + yr_idx
                val = abs(_to_float(cf_ws_oi.cell(row=72, column=col).value, 0))
                if val > 0:
                    yr_start = start_dt + relativedelta(years=yr_idx)
                    next_yr = start_dt + relativedelta(years=yr_idx + 1)
                    yr_end = end_dt if next_yr >= end_dt else next_yr - relativedelta(months=1)
                    capex_upfront.append({
                        "start_period": yr_start.strftime("%Y-%m"),
                        "end_period": yr_end.strftime("%Y-%m"),
                        "amount": val,
                    })


        # ═══ Build canonical engine inputs ═══
        inputs = {
            "schema_version": "0.1",
            "metadata": {
                "deal_id": deal_name,
                "run_id": f"rediq_{counter_id}",
                "as_of_date": date.today().isoformat(),
                "analyst": analyst,
                "purpose": "RedIQ Import",
            },
            "time_grid": {
                "analysis_start_date": start_date,
                "analysis_end_date": end_date,
            },
            "unit_cohorts": [
                {
                    "cohort_id": c["cohort_id"],
                    "unit_type": c.get("unit_type", c["cohort_id"]),
                    "unit_count": c["unit_count"],
                    "initial_inplace_rent": c["initial_inplace_rent"],
                    **({"sqft": c["sqft"]} if "sqft" in c else {}),
                }
                for c in cohorts
            ],
            "market_rent_curve": market_rent_curve,
            "loss_to_lease": loss_to_lease,
            "physical_vacancy_curve": physical_vacancy,
            "collection_loss_curve": collection_loss,
            "revenue_programs": revenue_programs_final,
            "program_adoption_curve": program_adoption,
        }

        # Optional sections
        if purchase_price:
            equity = purchase_price - sum(l.get("commitment", 0) for l in loans)
            inputs["purchase_assumptions"] = {
                "purchase_price": purchase_price,
                "equity_contribution": max(0, equity),
            }
            if closing_costs and closing_costs > 0:
                inputs["purchase_assumptions"]["closing_costs"] = closing_costs

            # Total equity at risk = |Day 0 LCF| from CF Calcs row 108, col D.
            # This includes upfront CapEx, loan fees, and other closing costs that
            # the simple equity formula (purchase - loans) misses.  Used as the
            # basis for AM-fee calculations in the fund waterfall.
            if cf_oi_available:
                day0_lcf = _to_float(cf_ws_oi.cell(row=108, column=4).value, 0)
                if day0_lcf and day0_lcf < 0:
                    inputs["purchase_assumptions"]["total_equity_basis"] = abs(day0_lcf)

                # Total unlevered basis = |Day 0 UCF| from CF Calcs row 86, col D.
                # Includes upfront CapEx that the simple purchase_price + closing_costs misses.
                day0_ucf = _to_float(cf_ws_oi.cell(row=86, column=4).value, 0)
                if day0_ucf and day0_ucf < 0:
                    inputs["purchase_assumptions"]["total_unlevered_basis"] = abs(day0_ucf)

        if exit_date or yr_of_exit:
            # Verify that RedIQ actually models an exit sale by checking
            # CF Calcs Row 80 (Gross Sale Price) in the exit year column.
            # Some deals (e.g., all-cash holds) have ExitCapRate set but
            # no sale modeled — Row 80 is $0 for all years.
            has_exit_sale = True
            if cf_oi_available:
                exit_col = 4 + yr_of_exit  # Exit year column
                gross_sale_check = _to_float(cf_ws_oi.cell(row=80, column=exit_col).value, 0)
                if not gross_sale_check or gross_sale_check == 0:
                    has_exit_sale = False

            if has_exit_sale:
                exit_month_str = end_date[:7] if end_date else None
                exit_assumptions = {
                    "exit_cap_rate": exit_cap if exit_cap else _warn_default("Exit cap rate not found; defaulting to 5.0%", 0.05),
                    "exit_month": exit_month_str,
                    "sale_cost_percent": total_sale_cost_pct if total_sale_cost_pct else 0.02,
                }
                if reversion_noi:
                    exit_assumptions["forward_noi_override"] = reversion_noi
                inputs["exit_assumptions"] = exit_assumptions

        if opex:
            # --- Calibrate OpEx growth rate from CF Calculations ---
            # The Input sheet inflation rate applies uniformly (e.g. 3%), but RedIQ's
            # per-category growth averages to a lower effective rate when categories
            # with non-standard growth (RE Tax, mgmt fee) are included.
            # Derive the effective rate from CF Calcs year-over-year OpEx totals,
            # EXCLUDING:
            #   - management fee (row 50): percent_egr, swings with lease-up revenue
            #   - RE Tax (row 49): may have 0% growth from reassessment, its own
            #     growth rate is already set from Input sheet Yr1/Yr2 columns
            if cf_oi_available:
                try:
                    cf_yr1_opex = abs(_to_float(cf_ws_oi.cell(row=54, column=5).value, 0))
                    cf_yr2_opex = abs(_to_float(cf_ws_oi.cell(row=54, column=6).value, 0))
                    # Subtract management fee (row 50) and RE Tax (row 49)
                    cf_yr1_mgmt = abs(_to_float(cf_ws_oi.cell(row=50, column=5).value, 0))
                    cf_yr2_mgmt = abs(_to_float(cf_ws_oi.cell(row=50, column=6).value, 0))
                    cf_yr1_retax = abs(_to_float(cf_ws_oi.cell(row=49, column=5).value, 0))
                    cf_yr2_retax = abs(_to_float(cf_ws_oi.cell(row=49, column=6).value, 0))
                    cf_yr1_base = cf_yr1_opex - cf_yr1_mgmt - cf_yr1_retax
                    cf_yr2_base = cf_yr2_opex - cf_yr2_mgmt - cf_yr2_retax
                    if cf_yr1_base > 0 and cf_yr2_base > 0:
                        eff_growth = round((cf_yr2_base / cf_yr1_base) - 1, 4)
                        if -0.10 < eff_growth < 0.10:  # Sanity check: +/-10%
                            for c in opex:
                                if c.get("growth_rate") is not None and c["calculation_type"] != "percent_egr":
                                    # Skip RE Tax — its growth is already set from
                                    # Input sheet Yr1/Yr2 column comparison
                                    cat_lower = c.get("category_name", "").lower()
                                    is_re_tax = "tax" in cat_lower and (
                                        "real estate" in cat_lower or "re tax" in cat_lower
                                    )
                                    if not is_re_tax:
                                        c["growth_rate"] = eff_growth
                except Exception:
                    pass
            inputs["opex_table"] = opex

        # Utility recovery: RedIQ supports two methods:
        #   1) "As Revenue" — reimbursements appear as OI (CF Calcs row 29 > 0)
        #   2) "As Expense Offset" — reimbursements reduce OpEx (CF Calcs row 52 > 0)
        # When using CF Calcs Total OI (row 32), method 1 is already captured.
        # For method 2 we'd need the engine's utility_recovery mechanism.
        reimb_as_expense = 0
        if cf_oi_available:
            reimb_as_expense = abs(_to_float(cf_ws_oi.cell(row=52, column=5).value, 0))
        if reimb_as_expense > 0:
            # "As Expense Offset" method — let the engine handle recovery
            # recovery_rate is inferred from reimbursement / total utility opex
            total_utility_opex = sum(
                c.get("base_value", 0) for c in (opex or [])
                if c.get("recoverable_flag")
            )
            rate = min(1.0, reimb_as_expense / total_utility_opex) if total_utility_opex > 0 else 0
            inputs["utility_recovery_rules"] = [{
                "utility_category": "ALL",
                "recovery_basis": "percent_of_expense",
                "recovery_rate": round(rate, 4),
            }]
        else:
            # "As Revenue" method (or no reimbursements) — recovery already in OI
            inputs["utility_recovery_rules"] = [{
                "utility_category": "ALL",
                "recovery_basis": "percent_of_expense",
                "recovery_rate": 0,
            }]

        if capex:
            inputs["capex_schedule"] = capex

        if loans:
            primary_loan = loans[0]
            if primary_loan.get("amort_years", 0) < 1:
                primary_loan["amort_years"] = _warn_default("Loan amortization not found; defaulting to 30 years", 30)

            # Refi alignment: when an assumable bridge matures exactly 1 month
            # before a permanent loan starts, extend the bridge term by 1 so
            # payoff and draw land in the same month (matching RedIQ behavior).
            if (primary_loan.get("is_assumable") and len(loans) > 1
                    and primary_loan.get("term_months")):
                p_term = primary_loan["term_months"]
                p_maturity = start_dt + relativedelta(months=p_term - 1)
                for extra in loans[1:]:
                    extra_start = extra.get("loan_start_month")
                    if extra_start:
                        try:
                            extra_start_dt = datetime.strptime(extra_start[:7], "%Y-%m").date()
                        except (ValueError, TypeError):
                            continue
                        # If bridge matures exactly 1 month before perm loan starts
                        if (extra_start_dt.year == p_maturity.year
                                and extra_start_dt.month == p_maturity.month + 1) or \
                           (p_maturity.month == 12
                                and extra_start_dt.year == p_maturity.year + 1
                                and extra_start_dt.month == 1):
                            primary_loan["term_months"] = p_term + 1
                            # RedIQ has the bridge's last DS one month before payoff;
                            # at the payoff month only the balance is retired (no DS).
                            primary_loan["skip_maturity_ds"] = True
                            break

            inputs["debt_terms"] = primary_loan
            if len(loans) > 1:
                # Extract mid-hold loan closing costs from CF Calcs Row 91.
                # Day 0 costs (column D) are part of purchase; Year 1+ costs
                # are refi origination fees that belong in operating LCF.
                refi_closing_costs_by_year: Dict[int, float] = {}
                if cf_oi_available:
                    for yr_idx in range(yr_of_exit):
                        col = 5 + yr_idx
                        cc_val = abs(_to_float(cf_ws_oi.cell(row=91, column=col).value, 0))
                        if cc_val > 0:
                            refi_closing_costs_by_year[yr_idx] = cc_val

                additional = []
                for extra_loan in loans[1:]:
                    if extra_loan.get("amort_years", 0) < 1:
                        extra_loan["amort_years"] = 30
                    # Assign refi closing costs: match the year containing the loan start month
                    extra_start = extra_loan.get("loan_start_month")
                    if extra_start and refi_closing_costs_by_year:
                        try:
                            extra_start_dt = datetime.strptime(extra_start[:7], "%Y-%m").date()
                        except (ValueError, TypeError):
                            extra_start_dt = None
                        if extra_start_dt:
                            for yr_idx, cc_val in refi_closing_costs_by_year.items():
                                yr_start_d = (start_dt + relativedelta(years=yr_idx))
                                yr_end_d = (start_dt + relativedelta(years=yr_idx + 1) - relativedelta(days=1))
                                if yr_start_d <= extra_start_dt <= yr_end_d:
                                    extra_loan["loan_closing_costs"] = cc_val
                                    break
                    additional.append(extra_loan)
                inputs["additional_debt_terms"] = additional

        if renovations:
            inputs["renovation_programs"] = [
                {
                    "program_id": r["program_id"],
                    "program_name": r["program_name"],
                    "target_cohort": r["target_cohort"],
                    "output_cohort": r["output_cohort"],
                    "renovation_cost_per_unit": r["renovation_cost_per_unit"],
                    "rent_premium_monthly": r["rent_premium_monthly"],
                    "downtime_days": r.get("downtime_days", 30),
                    "strategy": r.get("strategy", "on_turnover"),
                    "start_month": r.get("start_month", start_date[:7]),
                    **({"end_month": r["end_month"]} if "end_month" in r else {}),
                    "monthly_pace": r.get("monthly_pace", 2),
                    **({"max_units": r["max_units"]} if "max_units" in r else {}),
                    **({"post_renovation_market_rent": r["post_renovation_market_rent"]}
                       if "post_renovation_market_rent" in r else {}),
                }
                for r in renovations
            ]

        if fund_assumptions:
            if partnership_closing_costs_day0 > 0:
                fund_assumptions["partnership_closing_costs"] = partnership_closing_costs_day0

            # --- AM Fee Schedule (CF Calcs Row 120) ---
            # RedIQ's AM fee basis can change mid-hold (refi, construction draws,
            # exit-year revaluation).  Extract actual AM fee per year so the fund
            # waterfall matches exactly.
            if cf_oi_available and yr_of_exit:
                am_fee_schedule = []
                for yr_idx in range(yr_of_exit):
                    col = 5 + yr_idx
                    val = abs(_to_float(cf_ws_oi.cell(row=120, column=col).value, 0))
                    am_fee_schedule.append(round(val, 2))
                if any(v > 0 for v in am_fee_schedule):
                    fund_assumptions["am_fee_schedule"] = am_fee_schedule

            inputs["fund_assumptions"] = fund_assumptions

        if capex_upfront:
            inputs["capex_upfront_funded"] = capex_upfront

        if replacement_reserves:
            inputs["replacement_reserves"] = replacement_reserves

        if debt_draw_schedule:
            inputs["debt_draw_schedule"] = debt_draw_schedule
            # Flag staged construction draws so debt module computes interest
            # on the opening balance BEFORE applying subsequent draws.
            if len(debt_draw_schedule) > 1 and "debt_terms" in inputs:
                inputs["debt_terms"]["staged_draws"] = True

        return inputs
    finally:
        wb.close()


def get_all_named_range_values(
    workbook_path: str | Path,
    data_only: bool = True,
) -> Dict[str, Any]:
    """
    Extract ALL named range values from a RedIQ workbook.
    Useful for debugging and documentation.

    Returns:
        Dict of {name: value} for all resolvable named ranges
    """
    wb_path = Path(workbook_path)
    wb = openpyxl.load_workbook(wb_path, data_only=data_only, keep_vba=True, keep_links=True)

    values = {}
    for dn in wb.defined_names.values():
        name = dn.name
        val = _resolve_named_range(wb, name)
        if val is not None:
            values[name] = val

    wb.close()
    return values


# ── CLI ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Extract engine inputs from RedIQ workbook")
    parser.add_argument("workbook", help="Path to RedIQ .xlsm file")
    parser.add_argument("--output", "-o", help="Output JSON file (default: stdout)")
    parser.add_argument("--all-names", action="store_true", help="Dump all named range values")
    args = parser.parse_args()

    if args.all_names:
        values = get_all_named_range_values(args.workbook)
        output = json.dumps(values, indent=2, default=str)
    else:
        inputs = extract_inputs_from_rediq(args.workbook)
        output = json.dumps(inputs, indent=2, default=str)

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"Written to {args.output}")
    else:
        print(output)
