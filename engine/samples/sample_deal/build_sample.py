#!/usr/bin/env python3
"""Build the sample deal: Sample Garden Apartments (synthetic).

Everything here is invented: the property, the city, the residents, the unit
numbers and every amount. The sample exists so a new user can run the whole
flow (ingest, review, run, price) before touching a real deal.

The deal is a 128-unit, 1985 garden community underwritten as stabilized. It
is built to teach:

- the ingest gates PASS: the T12 ties to its own totals and the rent roll
  ties to its own summary block;
- the R&M floor triggers: R&M plus turnover is $750/unit against the 1980s
  floor of $900/unit, and the payroll review gives no credit (payroll is
  $1,680/unit, under the $2,000/unit benchmark), so underwriting warns and
  pricing raises R&M to the floor;
- the three capex buckets: an analyst immediate-capex figure (roofs), the
  vintage recurring capex (no PCA, so it is flagged), and the replacement
  reserve floor;
- house fallbacks: the geography is synthetic, so no market data matches and
  rent growth, stabilized vacancy and the exit cap fall back to house values
  flagged for review.

The documents follow the patterns of the synthetic test generators
(``tests/fixtures/deals/build_fixtures.py`` for the T12,
``tests/yardi_fixtures.py`` for the "Rent Roll with Lease Charges" layout).
The script is deterministic: no RNG, no clock. Amounts are built in integer
cents and spread over the twelve months with fixed weights.

Usage:
    python engine/samples/sample_deal/build_sample.py           # rewrite t12.xlsx, rent_roll.xlsx, profile.json
    python engine/samples/sample_deal/build_sample.py --check   # exit 1 if a committed file differs
    python engine/samples/sample_deal/build_sample.py --out DIR # write somewhere else
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent

PROPERTY_NAME = "Sample Garden Apartments (synthetic)"
PROPERTY_CODE = "sga01"
PROPERTY_ID = "SAMPLE-GARDEN"
ANALYSIS_START = "2026-11"
ANALYSIS_END = "2031-10"
T12_MONTHS = (
    "Nov 2025", "Dec 2025", "Jan 2026", "Feb 2026", "Mar 2026", "Apr 2026",
    "May 2026", "Jun 2026", "Jul 2026", "Aug 2026", "Sep 2026", "Oct 2026",
)
# Monthly weights (each set sums to 12).
FLAT = (1.00, 0.99, 1.01, 1.00, 0.98, 1.02, 1.00, 1.01, 0.99, 1.00, 1.01, 0.99)
UTILITY = (1.08, 1.12, 1.15, 1.10, 0.98, 0.90, 0.88, 0.92, 1.00, 1.02, 0.95, 0.90)
# The trailing year's GPR ramps up to today's rent roll.
GPR_RAMP_START = Decimal("0.985")


@dataclass(frozen=True)
class UnitType:
    label: str
    sqft: int
    count: int
    market_rent: int
    vacant: int


@dataclass(frozen=True)
class Line:
    """One T12 line: label and $/unit a year (income lines: $/unit a month)."""

    label: str
    per_unit: float
    weights: tuple[float, ...] = FLAT


UNIT_TYPES = (
    UnitType("1BR/1BA", 640, 56, 1095, 3),
    UnitType("2BR/1BA", 860, 40, 1295, 2),
    UnitType("2BR/2BA", 940, 24, 1395, 1),
    UnitType("3BR/2BA", 1150, 8, 1625, 1),
)
# In-place rent discount to market, cycled over the occupied units.
LTL_PATTERN = (0.02, 0.035, 0.05, 0.03, 0.045, 0.025, 0.04, 0.06)
# Non-rent charge lines on some leases (code, amount): every 3rd unit pest, every 4th trash.
VACANCY_LOSS = 0.055
CONCESSIONS = 0.010
BAD_DEBT = 0.010
OTHER_INCOME = (
    Line("Application Fees", 4),
    Line("Late Fees", 7),
    Line("Pet Rent", 10),
    Line("Admin Fees", 4),
)
REIMBURSEMENTS = (
    Line("RUBS - Water/Sewer", 38),
    Line("Trash Billback", 8),
)
EXPENSES: tuple[tuple[str, tuple[Line, ...]], ...] = (
    ("Payroll", (
        Line("Payroll - Office", 650),
        Line("Payroll - Maintenance", 700),
        Line("Payroll Taxes & Benefits", 330),
    )),
    ("Repairs & Maintenance", (
        Line("Maintenance Supplies", 260),
        Line("Plumbing Repairs", 120),
        Line("HVAC Repairs", 110),
        Line("Electrical Repairs/Supplies", 60),
        Line("Appliance Repairs", 50),
    )),
    ("Turnover", (
        Line("Turnover - Paint", 90),
        Line("Turnover - Cleaning", 60),
    )),
    ("Contract Services", (
        Line("Landscaping", 170),
        Line("Pest Control", 40),
    )),
    ("Utilities", (
        Line("Water & Sewer", 520, UTILITY),
        Line("Electric - Common Area", 180, UTILITY),
        Line("Gas - Common Area", 70, UTILITY),
        Line("Trash Removal", 110),
    )),
    ("Administrative", (
        Line("Office Supplies", 80),
        Line("Telephone", 50),
        Line("Legal & Professional", 40),
        Line("Computer & Software", 60),
    )),
    ("Marketing", (Line("Advertising", 110),)),
    ("Insurance", (Line("Property Insurance", 620),)),
)
MANAGEMENT_FEE_PCT = 0.03
# The asking price is set at this cap rate on the reassessed tax.
GOING_IN_CAP = 0.065
MILLAGE_MILLS = 21.0
ASSESSMENT_RATIO = 0.80
# The seller's taxes run below the reassessed bill.
PRIOR_TAX_FACTOR = 0.90


# ── money helpers (integer cents) ────────────────────────────────────────


def _cents(value: float | Decimal) -> int:
    return int(Decimal(str(value)).scaleb(2).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _spread(annual_cents: int, weights: tuple[float, ...]) -> list[int]:
    """Twelve monthly amounts that sum exactly to ``annual_cents``."""
    months = [
        int((Decimal(annual_cents) * Decimal(str(w)) / Decimal(12)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        for w in weights[:11]
    ]
    months.append(annual_cents - sum(months))
    return months


def _dollars(cents: int) -> float:
    return float(Decimal(cents) / 100)


def units_total() -> int:
    return sum(t.count for t in UNIT_TYPES)


# ── rent roll ("Rent Roll with Lease Charges" layout) ────────────────────


@dataclass(frozen=True)
class Unit:
    unit: str
    unit_type: str
    sqft: int
    market_rent: int
    resident: Optional[str]
    charges: tuple[tuple[str, int], ...]
    move_in: Optional[str]
    lease_end: Optional[str]


def _vacant_positions(count: int, vacant: int) -> set[int]:
    return {int((k + 0.5) * count / vacant) for k in range(vacant)} if vacant else set()


def build_units() -> list[Unit]:
    units: list[Unit] = []
    unit_no = occupied = 0
    for unit_type in UNIT_TYPES:
        vacant = _vacant_positions(unit_type.count, unit_type.vacant)
        for j in range(unit_type.count):
            unit_no += 1
            building, number = divmod(unit_no - 1, 16)
            unit = f"{building + 1}{number + 1:02d}"
            if j in vacant:
                units.append(Unit(unit, unit_type.label, unit_type.sqft, unit_type.market_rent,
                                  None, (), None, None))
                continue
            discount = Decimal(str(LTL_PATTERN[occupied % len(LTL_PATTERN)]))
            rent = int((Decimal(unit_type.market_rent) * (1 - discount)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            charges: list[tuple[str, int]] = [("rent", rent)]
            if occupied % 3 == 0:
                charges.append(("pest", 4))
            if occupied % 4 == 1:
                charges.append(("trash", 12))
            if occupied % 7 == 2:
                charges.append(("petrent", 25))
            # Leases roll over the next twelve months; move-ins a year earlier.
            month = (occupied * 5) % 12
            year, month_no = (2026, 11 + month) if month < 2 else (2027, month - 1)
            lease_end = f"{month_no}/28/{year}"
            move_in = f"{month_no}/1/{year - 1}"
            units.append(Unit(unit, unit_type.label, unit_type.sqft, unit_type.market_rent,
                              f"t{1000 + unit_no:05d}", tuple(charges), move_in, lease_end))
            occupied += 1
    return units


RR_HEADER_TOP = ["Unit", "Unit Type", "Unit", "Resident", "Name", "Market", "Charge", "Amount",
                 "Resident", "Other", "Move In", "Lease", "Move Out", "Balance"]
RR_HEADER_BOTTOM = [None, None, "Sq Ft", None, None, "Rent", "Code", None,
                    "Deposit", "Deposit", None, "Expiration", None, None]


def rent_roll_rows(units: list[Unit]) -> list[list[Any]]:
    rows: list[list[Any]] = [
        ["Rent Roll with Lease Charges"],
        [f"{PROPERTY_NAME} ({PROPERTY_CODE})"],
        ["As Of = 10/31/2026"],
        ["Month Year = 10/2026"],
        RR_HEADER_TOP,
        RR_HEADER_BOTTOM,
        ["Current/Notice/Vacant Residents"],
    ]
    charge_totals: dict[str, int] = {}
    lease_charges = 0
    for u in units:
        vacant = u.resident is None
        first_code, first_amount = u.charges[0] if u.charges else (None, 0)
        rows.append([u.unit, u.unit_type, u.sqft, "VACANT" if vacant else u.resident,
                     "VACANT" if vacant else "Resident", u.market_rent, first_code, first_amount,
                     0 if vacant else 300, 0, u.move_in, u.lease_end, None, 0])
        for code, amount in u.charges[1:]:
            rows.append([None, None, None, None, None, None, code, amount, None, None, None, None, None, None])
        unit_total = sum(amount for _, amount in u.charges)
        rows.append([None, None, None, None, None, None, "Total", unit_total, None, None, None, None, None, None])
        rows.append([None] * 14)
        lease_charges += unit_total
        for code, amount in u.charges:
            charge_totals[code] = charge_totals.get(code, 0) + amount

    occupied = [u for u in units if u.resident is not None]
    vacant_units = [u for u in units if u.resident is None]
    market_total = sum(u.market_rent for u in units)
    sqft_total = sum(u.sqft for u in units)
    deposits = 300 * len(occupied)
    rows.append([None, None, None, "Total", PROPERTY_NAME, market_total, None,
                 lease_charges, deposits, 0, None, None, None, 0])
    occ_pct = round(100 * len(occupied) / len(units), 2)
    rows += [
        [None] * 14,
        ["Summary Groups", None, None, None, None, "Square", "Market", "Lease", "Security", "Other",
         "# Of", "% Unit", "% Sqft", "Balance"],
        [None, None, None, None, None, "Footage", "Rent", "Charges", "Deposit", "Deposits",
         "Units", "Occupancy", "Occupied", None],
        ["Current/Notice/Vacant Residents", None, None, None, None, sqft_total, market_total,
         lease_charges, deposits, 0, len(units), occ_pct, occ_pct, 0],
        ["Occupied Units", None, None, None, None, sum(u.sqft for u in occupied),
         sum(u.market_rent for u in occupied), None, None, None, len(occupied), occ_pct, occ_pct, None],
        ["Total Non Rev Units", None, None, None, None, 0, 0, None, None, None, 0, 0, 0, None],
        ["Total Vacant Units", None, None, None, None, sum(u.sqft for u in vacant_units),
         sum(u.market_rent for u in vacant_units), None, None, None, len(vacant_units),
         round(100 - occ_pct, 2), round(100 - occ_pct, 2), None],
        ["Totals:", None, None, None, None, sqft_total, market_total, lease_charges,
         deposits, 0, len(units), 100, 100, 0],
        [None] * 14,
        ["Summary of Charges by Charge Code "],
        ["(Current/Notice Residents Only)"],
        ["Charge Code", None, None, "Amount"],
    ]
    for code in sorted(charge_totals):
        rows.append([code, None, None, charge_totals[code]])
    rows.append(["Total", None, None, lease_charges])
    return rows


def rent_roll_ltl(units: list[Unit]) -> Decimal:
    """Unit-weighted loss to lease as ingest measures it: cohort average in-place vs market."""
    gap = market = Decimal(0)
    for t in UNIT_TYPES:
        rents = [dict(u.charges)["rent"] for u in units if u.unit_type == t.label and u.resident]
        avg = Decimal(sum(rents)) / Decimal(len(rents))
        market += Decimal(t.count * t.market_rent)
        gap += Decimal(t.count) * max(Decimal(0), Decimal(t.market_rent) - avg)
    return gap / market


# ── T12 ──────────────────────────────────────────────────────────────────


@dataclass
class T12:
    rows: list[list[Any]]
    income_cents: int
    opex_cents: int
    purchase_price: int


def _row(label: str, months: Optional[list[int]] = None) -> list[Any]:
    if months is None:
        return [label]
    return [label, *(_dollars(m) for m in months), _dollars(sum(months))]


def build_t12(units: list[Unit]) -> T12:
    n = units_total()
    current = sum(t.count * t.market_rent for t in UNIT_TYPES) * 100
    gpr = [
        int((Decimal(current) * (GPR_RAMP_START + (1 - GPR_RAMP_START) * Decimal(i) / Decimal(11)))
            .quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        for i in range(12)
    ]

    def pct_of_gpr(rate: float | Decimal) -> list[int]:
        return [-int((Decimal(g) * Decimal(str(rate))).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) for g in gpr]

    rows: list[list[Any]] = [
        [PROPERTY_NAME],
        ["12 Month Statement"],
        ["Period = Nov 2025-Oct 2026"],
        [None],
        ["Account Name", *T12_MONTHS, "Total"],
        _row("INCOME"),
        _row("Rental Income"),
    ]
    income: list[list[int]] = []

    def add(label: str, months: list[int], bucket: list[list[int]]) -> None:
        rows.append(_row(label, months))
        bucket.append(months)

    add("Gross Potential Rent", gpr, income)
    add("Loss to Lease", pct_of_gpr(rent_roll_ltl(units).quantize(Decimal("0.000001"))), income)
    add("Vacancy Loss", pct_of_gpr(VACANCY_LOSS), income)
    add("Concessions", pct_of_gpr(CONCESSIONS), income)
    add("Bad Debt", pct_of_gpr(BAD_DEBT), income)
    for header, lines in (("Other Income", OTHER_INCOME), ("Reimbursable Income", REIMBURSEMENTS)):
        rows.append(_row(header))
        for line in lines:
            add(line.label, _spread(_cents(line.per_unit * n * 12), line.weights), income)
    income_months = [sum(m[i] for m in income) for i in range(12)]
    rows.append(_row("Total Income", income_months))
    egi = sum(income_months)

    rows.append(_row("EXPENSES"))
    expense: list[list[int]] = []
    for header, lines in EXPENSES:
        rows.append(_row(header))
        for line in lines:
            add(line.label, _spread(_cents(line.per_unit * n), line.weights), expense)
    opex_ex_tax_mgmt = sum(sum(m) for m in expense)
    management = _cents(Decimal(egi) / 100 * Decimal(str(MANAGEMENT_FEE_PCT)))
    tax_rate = Decimal(str(MILLAGE_MILLS)) / 1000 * Decimal(str(ASSESSMENT_RATIO))
    noi_before_tax = Decimal(egi - opex_ex_tax_mgmt - management) / 100
    # Price at the going-in cap on the reassessed tax: NOI_before_tax - price * t = cap * price.
    price = int((noi_before_tax / (Decimal(str(GOING_IN_CAP)) + tax_rate) / 10000)
                .quantize(Decimal("1"), rounding=ROUND_HALF_UP)) * 10000
    tax = _cents(Decimal(price) * tax_rate * Decimal(str(PRIOR_TAX_FACTOR)))
    for header, label, annual in (
        ("Taxes", "Real Estate Taxes", tax),
        ("Management Fees", "Management Fee", management),
    ):
        rows.append(_row(header))
        add(label, _spread(annual, FLAT), expense)
    expense_months = [sum(m[i] for m in expense) for i in range(12)]
    rows.append(_row("Total Operating Expenses", expense_months))
    rows.append(_row("Net Operating Income", [income_months[i] - expense_months[i] for i in range(12)]))
    return T12(rows, egi, sum(expense_months), price)


# ── profile.json ─────────────────────────────────────────────────────────


def profile_document(t12: T12) -> dict[str, Any]:
    n = units_total()
    return {
        "name": PROPERTY_NAME,
        "synthetic": True,
        "description": (
            f"A fictional {n}-unit, 1985 garden community underwritten as a stabilized deal. "
            "Every name, unit and amount is invented. Use it to learn the flow before a real deal."
        ),
        "documents": {"rent_roll": "rent_roll.xlsx", "t12": "t12.xlsx"},
        # Keys are runs/ingest_deal.py flags without the leading dashes
        # (underscores for hyphens); true means a bare switch.
        "ingest": {
            "property_id": PROPERTY_ID,
            "start": ANALYSIS_START,
            "end": ANALYSIS_END,
            "deal_type": "stabilized",
            "year_built": 1985,
            "asset_class": "B",
            "star_tier": "3",
            "msa": "Quillmere Synthetic MSA",
            "metro_slug": "quillmere_sx",
            "submarket": "Quillmere East",
            "purchase_price": t12.purchase_price,
            "ltv": 0.65,
            "rate": 0.0575,
            "amort": 30,
            "io_months": 24,
            "sale_cost": 0.02,
            "millage": MILLAGE_MILLS,
            "assessment_ratio": ASSESSMENT_RATIO,
            "immediate_capex": 192000,
            "validate": True,
        },
        # Inputs for pricing (python -m engine.backsolve). The benchmark
        # treasury is illustrative, not a market quote.
        "pricing": {
            "strategy": "cashflow",
            "target_coc": 0.08,
            "benchmark_5yr_treasury": 0.04,
            "benchmark_as_of": "2026-09-30",
            "benchmark_source": "illustrative sample value",
        },
        "notes": [
            "Immediate capex of $192,000 ($1,500/unit) is an analyst figure for two roof replacements, "
            "funded with equity at close.",
            "No PCA: the 1985 vintage is flagged for one (CAPEX_PCA_MISSING).",
            "R&M plus turnover is $750/unit, under the 1980s floor of $900/unit: underwriting warns, "
            "pricing raises it to the floor.",
            "The geography is synthetic, so market data falls back to flagged house values.",
        ],
    }


def ingest_argv(profile: dict[str, Any], folder: Path, output: Path) -> list[str]:
    """runs/ingest_deal.py arguments for ``profile`` with documents in ``folder``."""
    argv = [
        "--rent-roll", str(folder / profile["documents"]["rent_roll"]),
        "--t12", str(folder / profile["documents"]["t12"]),
        "--output", str(output),
    ]
    for key, value in profile["ingest"].items():
        flag = "--" + key.replace("_", "-")
        if value is True:
            argv.append(flag)
        elif value is not None and value is not False:
            argv += [flag, str(value)]
    return argv


# ── rendering ────────────────────────────────────────────────────────────


def _write_xlsx(path: Path, rows: list[list[Any]], sheet: str) -> None:
    from datetime import datetime

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    for row in rows:
        ws.append(row)
    fixed = datetime(2026, 10, 31)
    wb.properties.creator = "build_sample.py"
    wb.properties.created = fixed
    wb.properties.modified = fixed
    wb.save(path)
    wb.close()


def render() -> tuple[list[list[Any]], list[list[Any]], str]:
    """Rent-roll rows, T12 rows and profile.json text. Pure: same output every call."""
    units = build_units()
    t12 = build_t12(units)
    profile = json.dumps(profile_document(t12), indent=2) + "\n"
    return rent_roll_rows(units), t12.rows, profile


def sheet_values(path: Path) -> list[list[Any]]:
    """Cell values of a workbook's first sheet, trailing empty cells dropped."""
    from openpyxl import load_workbook

    wb = load_workbook(path, data_only=True)
    try:
        out = []
        for row in wb.worksheets[0].iter_rows(values_only=True):
            values = list(row)
            while values and values[-1] is None:
                values.pop()
            out.append(values)
        while out and not out[-1]:
            out.pop()
        return out
    finally:
        wb.close()


def _normalized(rows: list[list[Any]]) -> list[list[Any]]:
    out = []
    for row in rows:
        values = list(row)
        while values and values[-1] is None:
            values.pop()
        out.append(values)
    return out


def stale_files(folder: Path = HERE) -> list[str]:
    """Names of the committed sample files that differ from a fresh build."""
    roll, t12, profile = render()
    stale = []
    for name, rows in (("rent_roll.xlsx", roll), ("t12.xlsx", t12)):
        path = folder / name
        if not path.exists() or sheet_values(path) != _normalized(rows):
            stale.append(name)
    path = folder / "profile.json"
    if not path.exists() or path.read_text(encoding="utf-8") != profile:
        stale.append("profile.json")
    return stale


def write(folder: Path = HERE) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    roll, t12, profile = render()
    _write_xlsx(folder / "rent_roll.xlsx", roll, "Report1")
    _write_xlsx(folder / "t12.xlsx", t12, "T12")
    (folder / "profile.json").write_text(profile, encoding="utf-8")
    return [folder / "rent_roll.xlsx", folder / "t12.xlsx", folder / "profile.json"]


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="Fail if a committed file differs; write nothing.")
    parser.add_argument("--out", type=Path, default=HERE, help="Folder to write (default: this folder).")
    args = parser.parse_args(argv)
    if args.check:
        stale = stale_files(args.out)
        for name in stale:
            print(f"stale: {name}")
        return 1 if stale else 0
    for path in write(args.out):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
