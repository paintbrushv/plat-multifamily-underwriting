"""Synthetic Yardi-format fixtures: a ``ysi_is`` T12 and a "Rent Roll with
Lease Charges" export.

Everything here is invented: the property ("Example Commons", code ``x701``),
residents, unit numbers and every amount. The workbooks only reproduce the
STRUCTURE of the two Yardi reports, which is what the parsers have to handle:

T12 (tree ``ysi_is``)
  - four title rows, a month header row, labels in column A indented with
    leading spaces (sections one space, lines four), 12 months plus Total;
  - income sections with "TOTAL ..." rows, two exceptions: the OTHER INCOME
    total row is labeled just " OTHER INCOME", and NET RENTAL INCOME is a
    subtotal; the OTHER INCOME and BAD DEBT headers carry no leading space;
  - income ends at " EFFECTIVE GROSS INCOME";
  - " OPERATING EXPENSES" (no values), then sections each closed by a
    "TOTAL ..." row, with REPAIRS & MAINTENANCE nesting two sections that
    have their own totals; " TAXES & INSURANCE" uses "&"; many all-zero lines;
    " TOTAL OPERATING EXPENSES" then " NET OPERATING INCOME".

Rent roll (Rent Roll with Lease Charges)
  - four title rows, a two-row header, a "Current/Notice/Vacant Residents"
    section row;
  - per unit a row with the unit fields AND the first charge line (often a
    small fee, not rent), one row per further charge (charge code and amount
    only), a "Total" charge row and a blank row;
  - vacant units read VACANT / VACANT with no charge code and a 0 amount;
    there is no status column;
  - a "Future Residents/Applicants" section that repeats a vacant unit, a
    grand total row, a "Summary Groups" block and a "Summary of Charges by
    Charge Code" block.

Run ``python tests/yardi_fixtures.py <dir>`` to write both workbooks.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Sequence

PROPERTY = "Example Commons"
PROPERTY_CODE = "x701"
MONTHS = [f"{name} 2026" for name in (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)]

Amount = float | Sequence[float]
Line = tuple[str, Amount]

# ── T12 ────────────────────────────────────────────────────────────────────

# (header or None, header indent, lines, total label, children)
T12_INCOME: list[dict[str, Any]] = [
    {"header": None, "lines": [
        ("Gross Potential Rent", 0),
        ("Market Rent", 1_152_000.00),
        ("Tenant-Based Subsidy", 0),
        ("Prepaid Rent - Current Month", 0),
        ("Leases Over/Under Schedule", -17_412.60),
    ], "total": " TOTAL GROSS POTENTIAL RENT"},
    {"header": " RENTAL LOSS", "lines": [
        ("Concessions", -1_200.00),
        ("Employee Rent Discount", -2_160.00),
        ("Model/Storage/Down Unit", 0),
        ("Vacancy Loss", -61_488.30),
        ("Short Term Lease", 0),
    ], "total": " TOTAL RENTAL LOSS"},
    {"subtotal": " NET RENTAL INCOME"},
    {"header": " UTILITY INCOME", "lines": [
        ("Pest Control - RUBS", 2_880.00),
        ("RUBS Admin Fee Income", 4_104.00),
        ("Trash - RUBS", 7_560.00),
        ("Water/ Sewer - RUBS", 33_912.48),
        ("Valet Trash - RUBS", 18_000.00),
        ("EV Charger Income", 0),
    ], "total": " TOTAL RUBS INCOME"},
    {"header": "OTHER INCOME", "lines": [
        ("Application Fees", 2_450.00),
        ("Administrative Fees", 3_600.00),
        ("Damages", 940.00),
        ("Electric Reimbursement", 1_734.12),
        ("Late Charges", 11_206.55),
        ("Pet Rent", 2_040.00),
        ("Parking", 1_800.00),
        ("Storage", 4_215.30),
        ("Cable/Internet Income", 64_800.00),
        ("Reletting Fee", 3_900.00),
        ("Waiver Fees", 7_920.40),
        ("Other Income", 9_310.75),
        ("Interest Income", 1_102.88),
        ("Bad Debt Expense", 0),
    ], "total": " OTHER INCOME"},
    {"header": " MISC INCOME", "lines": [
        ("Miscellaneous Income", 0),
        ("Commissions", 0),
    ], "total": " TOTAL MISC INCOME"},
    {"header": "BAD DEBT", "lines": [
        ("Bad Debt", -6_870.20),
        ("Bad Debt Collections", 2_215.65),
    ], "total": " TOTAL BAD DEBT EXPENSE"},
]

T12_OPEX: list[dict[str, Any]] = [
    {"header": " COMMISSIONS", "lines": [("Commissions", 0)], "total": " TOTAL COMMISSIONS"},
    {"header": " MARKETING EXPENSES", "lines": [
        ("ILS Advertising", 5_400.00),
        ("PPC Advertising", 1_320.50),
        ("Referral Fee", 2_500.00),
        ("Website", 0),
        ("Reputation Management", 1_140.00),
    ], "total": " TOTAL MARKETING EXPENSES"},
    {"header": " PAYROLL EXPENSE", "lines": [
        ("Manager Salaries", 52_000.00),
        ("Leasing Salaries", 0),
        ("Repairs Salaries", 41_600.00),
        ("Grounds Salaries", 0),
        ("Bonuses", 9_750.00),
        ("Payroll /Unemployment Tax", 8_140.20),
        ("Workers Compensation Insurance", 905.40),
        ("Health/Life Insurance", 11_280.00),
        ("Vision Insurance", 14.40),
        ("Dental Insurance", 61.20),
        ("Temporary Help-Maint", 2_310.00),
        ("Payroll Fees", 548.60),
    ], "total": " TOTAL PAYROLL EXPENSE"},
    {"header": " LICENSES/FEES/PERMITS", "lines": [
        ("Licenses", 1_850.00),
        ("Permits", 75.00),
        ("Fees", 310.40),
    ], "total": " TOTAL LICENSES/FEES/PERMITS"},
    {"header": " GENERAL & ADMINISTRATIVE EXPENSES", "lines": [
        ("Computer & Software Expense", 4_980.00),
        ("Mileage Reimbursement", 120.00),
        ("Legal Expenses", 640.00),
        ("Office Supplies/Expenses", 395.25),
        ("Telephone Expenses", 612.00),
        ("Revenue Management", 0),
        ("Misc. Administrative Expenses", 0),
    ], "total": " TOTAL GENERAL & ADMINISTRATIVE EXPENSE"},
    {"header": " UTILITY EXPENSES", "lines": [
        ("Electric - House Meters", 4_620.80),
        ("Electric - Vacant Units", 2_975.10),
        ("Gas", 1_240.00),
        ("Water & Sewer", 41_350.65),
        ("Sewer", 0),
        ("Common Area Water and Sewage", 2_860.15),
    ], "total": " TOTAL UTILITY EXPENSES"},
    {"header": " SERVICE CONTRACTS", "lines": [
        ("Bulk Cable Service/ Internet Service", 38_400.00),
        ("Courtesy Patrol/Alarms", 180.00),
        ("Exterminating", 2_280.00),
        ("Garbage & Trash Removal", 12_960.00),
        ("Valet Trash Service", 6_840.00),
        ("Lawn Maintenance", 9_600.00),
        ("Elevator Repairs", 0),
        ("3rd Party Billing Service", 8_160.00),
    ], "total": " TOTAL SERVICE CONTRACTS"},
    {"header": " REPAIRS & MAINTENANCE EXPENSES", "lines": [], "children": [
        {"header": " EXTERIOR & GROUNDS", "lines": [
            ("Exterior Repairs/Supplies", 410.00),
            ("Misc Landscaping", 160.00),
            ("Irrigation Repairs", 225.00),
            ("Roof Repairs", 0),
        ], "total": " TOTAL EXTERIOR & GROUNDS"},
        {"header": " INTERIOR REPAIRS/MAINTENANCE", "lines": [
            ("Appliance Repairs/Supplies", 1_480.00),
            ("Cleaning Contract", 3_150.00),
            ("Electrical Repairs/Supplies", 905.30),
            ("HVAC Repairs/Supplies", 2_240.00),
            ("Plumbing Repairs/Supplies", 1_115.75),
            ("Water Heater Repairs/Supplies", 0),
            ("Paint/Supplies", 690.00),
        ], "total": " TOTAL INTERIOR REPAIRS/MAINTENANCE"},
    ], "total": " TOTAL REPAIRS & MAINTENANCE EXPENSES"},
    {"header": " MANAGEMENT FEES", "lines": [
        ("Management Fees", 41_850.00),
        ("Construction Management", 0),
    ], "total": " TOTAL MANAGEMENT FEES"},
    {"header": " TAXES & INSURANCE", "lines": [
        ("Real Estate Taxes", 148_600.00),
        ("Real Estate Taxes - Prior Year", [0, 0, 0, -6_240.50, 0, 0, 0, 0, 0, 0, 0, 0]),
        ("Personal Property Taxes", 0),
        ("Property & Liability Insurance", 52_380.00),
        ("Other Insurance", 0),
        ("Franchise Tax", 0),
    ], "total": " TOTAL TAXES & INSURANCE"},
]


def _months(amount: Amount) -> list[float]:
    """Spread an annual amount over 12 months (cents exact), or take 12 given months."""
    if not isinstance(amount, (int, float)):
        return [round(float(v), 2) for v in amount]
    cents = round(amount * 100)
    base, rest = divmod(abs(cents), 12)
    sign = -1 if cents < 0 else 1
    return [sign * (base + (1 if m < rest else 0)) / 100 for m in range(12)]


def _row(label: str, months: list[float]) -> list[Any]:
    return [label, *months, round(sum(months), 2)]


def _add(a: list[float], b: list[float]) -> list[float]:
    return [round(x + y, 2) for x, y in zip(a, b)]


def _section_rows(section: dict[str, Any], out: list[list[Any]]) -> list[float]:
    """Append a section's rows; return its monthly total."""
    total = [0.0] * 12
    if section.get("header"):
        out.append([section["header"]])
    for child in section.get("children", []):
        out.append([None])
        total = _add(total, _section_rows(child, out))
    for label, amount in section.get("lines", []):
        months = _months(amount)
        out.append(_row(f"    {label}", months))
        total = _add(total, months)
    out.append([None])
    out.append(_row(section["total"], total))
    out.append([None])
    return total


def yardi_t12_rows() -> list[list[Any]]:
    rows: list[list[Any]] = [
        [f"{PROPERTY} ({PROPERTY_CODE})"],
        ["Statement (12 months)"],
        ["Period = Jan 2026-Dec 2026"],
        ["Book = Accrual ; Tree = ysi_is"],
        [None, *MONTHS, "Total"],
    ]
    income = [0.0] * 12
    running = [0.0] * 12
    for section in T12_INCOME:
        if "subtotal" in section:
            rows.append(_row(section["subtotal"], running))
            rows.append([None])
            continue
        section_total = _section_rows(section, rows)
        income = _add(income, section_total)
        running = _add(running, section_total)
    rows.append(_row(" EFFECTIVE GROSS INCOME", income))
    rows.append([None])
    rows.append([" OPERATING EXPENSES"])
    rows.append([None])
    opex = [0.0] * 12
    for section in T12_OPEX:
        opex = _add(opex, _section_rows(section, rows))
    rows.append(_row(" TOTAL OPERATING EXPENSES", opex))
    rows.append([None])
    rows.append(_row(" NET OPERATING INCOME", [round(i - o, 2) for i, o in zip(income, opex)]))
    return rows


def _lines(sections: list[dict[str, Any]]) -> list[Line]:
    found: list[Line] = []
    for section in sections:
        found.extend(section.get("lines", []))
        found.extend(_lines(section.get("children", [])))
    return found


def _annual(amount: Amount) -> float:
    return round(sum(_months(amount)), 2)


def t12_line(label: str) -> float:
    """Annual amount of one synthetic T12 line."""
    for line_label, amount in _lines(T12_INCOME + T12_OPEX):
        if line_label == label:
            return _annual(amount)
    raise KeyError(label)


def t12_section(total_label: str) -> float:
    """Annual total of one synthetic T12 section, by its total row label."""
    def _find(sections: list[dict[str, Any]]) -> float | None:
        for section in sections:
            if section.get("total", "").strip() == total_label:
                return round(sum(_annual(a) for _, a in _lines([section])), 2)
            found = _find(section.get("children", []))
            if found is not None:
                return found
        return None

    found = _find(T12_INCOME + T12_OPEX)
    if found is None:
        raise KeyError(total_label)
    return found


T12_EGI = round(sum(_annual(a) for _, a in _lines(T12_INCOME)), 2)
T12_OPEX_TOTAL = round(sum(_annual(a) for _, a in _lines(T12_OPEX)), 2)


def write_yardi_t12(path: Path) -> Path:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Report1"
    for row in yardi_t12_rows():
        ws.append(row)
    wb.save(path)
    wb.close()
    return path


# ── Rent roll with lease charges ───────────────────────────────────────────

# (unit, type, sqft, resident id or "VACANT", market rent, charges, move in, lease end)
# The unit row carries the first charge line; vacant units have none.
RR_UNITS: list[tuple[str, str, int, str, float, list[tuple[str, float]], str | None, str | None]] = [
    ("101", "x701_A1", 640, "r9000101", 1_095, [("pest", 4), ("rent", 1_070), ("trash", 10), ("cable", 75)],
     "3/1/2026", "2/28/2027"),
    ("102", "x701_A1", 640, "r9000102", 1_095, [("rent", 1_085), ("cable", 75), ("pest", 4)],
     "6/15/2025", "6/14/2026"),
    ("103", "x701_A1G", 660, "VACANT", 1_140, [], None, None),
    ("104", "x701_A1G", 660, "r9000104", 1_140, [("pest", 4), ("rent", 1_120), ("empl", -100)],
     "9/1/2025", "8/31/2026"),
    ("105", "x701_B1", 880, "r9000105", 1_380, [("rent", 1_345), ("valtrash", 25)],
     "1/10/2026", "1/9/2027"),
    ("106", "x701_B1", 880, "r9000106", 1_380, [("pest", 4), ("adminrub", 3), ("rent", 1_360), ("cable", 75)],
     "4/1/2025", "3/31/2026"),
    ("107", "x701_B1", 880, "VACANT", 1_380, [], None, None),
    ("108", "x701_B2P", 960, "r9000108", 1_560, [("rent", 1_525), ("cable", 75)],
     "11/1/2025", "10/31/2026"),
    ("109", "x701_B2P", 960, "r9000109", 1_560, [("cable", 75), ("rent", 800), ("rent", 735)],
     "2/1/2026", "1/31/2027"),
    ("110", "X701_B3P", 1_180, "r9000110", 1_795, [("pest", 4), ("rent", 1_760)],
     "7/1/2025", "6/30/2026"),
    ("111", "X701_B3P", 1_180, "r9000111", 1_795, [("carport", 35), ("rent", 1_780)],
     "8/15/2025", "8/14/2026"),
    ("112", "x701_A1", 640, "r9000112", 1_095, [("rent", 1_060)],
     "5/1/2026", "4/30/2027"),
]
# Pre-leased vacant unit repeated in the Future Residents/Applicants section.
RR_FUTURE = [("107", "x701_B1", 880, "r9000201", 1_380, "2/1/2027", "1/31/2028")]

RR_HEADER_TOP = ["Unit", "Unit Type", "Unit", "Resident", "Name", "Market", "Charge", "Amount",
                 "Resident", "Other", "Move In", "Lease", "Move Out", "Balance"]
RR_HEADER_BOTTOM = [None, None, "Sq Ft", None, None, "Rent", "Code", None,
                    "Deposit", "Deposit", None, "Expiration", None, None]


def rr_expected() -> dict[str, Any]:
    """What a correct parse of the synthetic rent roll yields."""
    occupied = [u for u in RR_UNITS if u[3] != "VACANT"]
    return {
        "units": len(RR_UNITS),
        "vacant_units": len(RR_UNITS) - len(occupied),
        "market_rent_total": float(sum(u[4] for u in RR_UNITS)),
        "rent_by_unit": {u[0]: float(sum(a for code, a in u[5] if code == "rent")) for u in RR_UNITS},
        "rent_charges_total": float(sum(a for u in RR_UNITS for code, a in u[5] if code == "rent")),
    }


def yardi_rent_roll_rows(
    *,
    summary: bool = True,
    summary_overrides: dict[str, float] | None = None,
) -> list[list[Any]]:
    """Rows of the synthetic export. ``summary_overrides`` corrupts the Summary
    Groups figures (``units``, ``vacant_units``, ``market_rent_total``) to
    exercise the reconciliation gate."""
    rows: list[list[Any]] = [
        ["Rent Roll with Lease Charges"],
        [f"{PROPERTY} ({PROPERTY_CODE})"],
        ["As Of = 01/15/2027"],
        ["Month Year = 01/2027"],
        RR_HEADER_TOP,
        RR_HEADER_BOTTOM,
        ["Current/Notice/Vacant Residents"],
    ]
    charge_totals: dict[str, float] = {}
    lease_charges = 0.0
    for unit, unit_type, sqft, resident, market, charges, move_in, lease_end in RR_UNITS:
        vacant = resident == "VACANT"
        name = "VACANT" if vacant else "Resident"
        deposit = 0 if vacant else 200
        first_code, first_amount = charges[0] if charges else (None, 0)
        rows.append([int(unit), unit_type, sqft, resident, name, market, first_code, first_amount,
                     deposit, 0, move_in, lease_end, None, 0])
        for code, amount in charges[1:]:
            rows.append([None, None, None, None, None, None, code, amount, None, None, None, None, None, None])
        unit_total = sum(amount for _, amount in charges)
        rows.append([None, None, None, None, None, None, "Total", unit_total, None, None, None, None, None, None])
        rows.append([None] * 14)
        lease_charges += unit_total
        for code, amount in charges:
            charge_totals[code] = charge_totals.get(code, 0.0) + amount

    rows.append(["Future Residents/Applicants"])
    for unit, unit_type, sqft, resident, market, move_in, lease_end in RR_FUTURE:
        rows.append([int(unit), unit_type, sqft, resident, "Resident", market, None, 0, 0, 0,
                     move_in, lease_end, None, 0])
        rows.append([None, None, None, None, None, None, "Total", 0, None, None, None, None, None, None])
        rows.append([None] * 14)

    market_total = float(sum(u[4] for u in RR_UNITS))
    sqft_total = sum(u[2] for u in RR_UNITS)
    occupied = [u for u in RR_UNITS if u[3] != "VACANT"]
    vacant = [u for u in RR_UNITS if u[3] == "VACANT"]
    rows.append([None, None, None, "Total", f"{PROPERTY}({PROPERTY_CODE})", market_total, None,
                 lease_charges, 200 * len(occupied), 0, None, None, None, 0])
    if not summary:
        return rows

    figures = {"units": len(RR_UNITS), "vacant_units": len(vacant), "market_rent_total": market_total}
    figures.update(summary_overrides or {})
    occ_pct = round(100 * len(occupied) / len(RR_UNITS), 2)
    rows += [
        [None] * 14,
        ["Summary Groups", None, None, None, None, "Square", "Market", "Lease", "Security", "Other",
         "# Of", "% Unit", "% Sqft", "Balance"],
        [None, None, None, None, None, "Footage", "Rent", "Charges", "Deposit", "Deposits",
         "Units", "Occupancy", "Occupied", None],
        ["Current/Notice/Vacant Residents", None, None, None, None, sqft_total, figures["market_rent_total"],
         lease_charges, 200 * len(occupied), 0, figures["units"], occ_pct, occ_pct, 0],
        ["Future Residents/Applicants", None, None, None, None, sum(u[2] for u in RR_FUTURE),
         float(sum(u[4] for u in RR_FUTURE)), 0, 0, 0, len(RR_FUTURE), None, None, 0],
        ["Occupied Units", None, None, None, None, sum(u[2] for u in occupied),
         float(sum(u[4] for u in occupied)), None, None, None, len(occupied), occ_pct, occ_pct, None],
        ["Total Non Rev Units", None, None, None, None, 0, 0, None, None, None, 0, 0, 0, None],
        ["Total Vacant Units", None, None, None, None, sum(u[2] for u in vacant),
         float(sum(u[4] for u in vacant)), None, None, None, figures["vacant_units"],
         round(100 - occ_pct, 2), round(100 - occ_pct, 2), None],
        ["Totals:", None, None, None, None, sqft_total, figures["market_rent_total"], lease_charges,
         200 * len(occupied), 0, figures["units"], 100, 100, 0],
        [None] * 14,
        ["Summary of Charges by Charge Code "],
        ["(Current/Notice Residents Only)"],
        ["Charge Code", None, None, "Amount"],
    ]
    for code, amount in charge_totals.items():
        rows.append([code, None, None, amount])
    rows.append(["Total", None, None, f"{lease_charges:,.2f}"])
    return rows


def write_yardi_rent_roll(path: Path, **kwargs: Any) -> Path:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Report1"
    for row in yardi_rent_roll_rows(**kwargs):
        ws.append(row)
    wb.save(path)
    wb.close()
    return path


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    print(write_yardi_t12(out / "yardi_ysi_is_t12.xlsx"))
    print(write_yardi_rent_roll(out / "yardi_lease_charges_rent_roll.xlsx"))
