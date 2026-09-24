from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from openpyxl import load_workbook

from engine.modules.util import month_id, parse_month


_MONTH_LABEL_RE = re.compile(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})$")
_ACCT_CODE_RE = re.compile(r"^\d{4}-\d{4}$")


def _norm(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    return value


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _is_blank_row(values: Iterable[Any]) -> bool:
    for v in values:
        if v is None:
            continue
        if isinstance(v, str) and v.strip() == "":
            continue
        return False
    return True


def _parse_month_label(label: str) -> Optional[str]:
    m = _MONTH_LABEL_RE.match(label.strip())
    if not m:
        return None
    month_name, year_s = m.group(1), m.group(2)
    month_map = {
        "Jan": 1,
        "Feb": 2,
        "Mar": 3,
        "Apr": 4,
        "May": 5,
        "Jun": 6,
        "Jul": 7,
        "Aug": 8,
        "Sep": 9,
        "Oct": 10,
        "Nov": 11,
        "Dec": 12,
    }
    d = date(int(year_s), month_map[month_name], 1)
    return f"{d.year:04d}-{d.month:02d}"


def _month_range(months: List[str]) -> Tuple[str, str]:
    if not months:
        raise ValueError("No months parsed from report")
    return months[0], months[-1]


@dataclass(frozen=True)
class RentRollUnitRow:
    unit: str
    unit_type: str
    sqft: Optional[float]
    resident_id: Optional[str]
    resident_name: Optional[str]
    market_rent: Optional[float]
    move_in: Optional[str]
    lease_expiration: Optional[str]


@dataclass(frozen=True)
class RentRollChargeRow:
    unit: str
    charge_code: str
    amount: float


@dataclass(frozen=True)
class RentRollExtract:
    property_name: Optional[str]
    as_of_date: Optional[str]
    month_year: Optional[str]
    units: List[RentRollUnitRow]
    charges: List[RentRollChargeRow]


@dataclass(frozen=True)
class T12MonthlyRow:
    account_code: str
    account_name: str
    month: str  # YYYY-MM
    amount: Optional[float]


@dataclass(frozen=True)
class T12Extract:
    property_name: Optional[str]
    period_start: str  # YYYY-MM
    period_end: str  # YYYY-MM
    rows: List[T12MonthlyRow]


def _sheet_values(ws, max_cols: int) -> List[List[Any]]:
    out = []
    for r in range(1, ws.max_row + 1):
        row = []
        for c in range(1, max_cols + 1):
            row.append(_norm(ws.cell(r, c).value))
        out.append(row)
    return out


def detect_yardi_report_type(xlsx_path: Path) -> str:
    wb = load_workbook(xlsx_path, data_only=True)
    try:
        ws = wb.worksheets[0]
        values = _sheet_values(ws, max_cols=min(20, ws.max_column))
        text = " ".join(_as_str(v) for row in values[:20] for v in row if _as_str(v))
        if "Rent Roll with Lease Charges" in text:
            return "rent_roll_with_lease_charges"
        if "Statement (12 months)" in text or "TOTAL GROSS POTENTIAL RENT" in text:
            return "statement_12_months"
        return "unknown"
    finally:
        wb.close()


def parse_yardi_rent_roll_with_lease_charges(xlsx_path: Path, redact_pii: bool = True) -> RentRollExtract:
    wb = load_workbook(xlsx_path, data_only=True)
    try:
        ws = wb.worksheets[0]

        values = _sheet_values(ws, max_cols=min(30, ws.max_column))
    finally:
        wb.close()

    property_name: Optional[str] = None
    as_of_date: Optional[str] = None
    month_year: Optional[str] = None

    for row in values[:20]:
        s = _as_str(row[0])
        if s.endswith(" "):  # property name row is typically padded/merged
            s = s.strip()
        if property_name is None and s and s.lower() not in ("unit", "rent roll with lease charges"):
            property_name = s
        if "As Of" in s and "=" in s:
            as_of_date = s.split("=", 1)[1].strip()
        if "Month Year" in s and "=" in s:
            month_year = s.split("=", 1)[1].strip()

    header_row_idx = None
    headers: List[str] = []
    for i, row in enumerate(values[:100], start=1):
        row_text = [_as_str(v) for v in row]
        if "Unit" in row_text and "Unit Type" in row_text and "Charge" in row_text and "Amount" in row_text:
            header_row_idx = i
            main = row_text
            sub = [_as_str(v) for v in values[i]] if i < len(values) else [""] * len(main)
            combined: List[str] = []
            for j in range(len(main)):
                a = main[j]
                b = sub[j] if j < len(sub) else ""
                if a and b:
                    combined.append(f"{a} {b}".strip())
                elif b:
                    combined.append(b)
                else:
                    combined.append(a)
            headers = combined
            break
    if header_row_idx is None:
        raise ValueError("Could not find header row for rent roll report")

    def idx(name: str) -> int:
        try:
            return headers.index(name)
        except ValueError as e:
            raise ValueError(f"Missing expected column in rent roll: {name}") from e

    unit_idx = idx("Unit")
    unit_type_idx = idx("Unit Type")
    sqft_idx = idx("Unit Sq Ft")
    resident_idx = idx("Resident")
    name_idx = idx("Name")
    market_rent_idx = idx("Market Rent")
    charge_code_idx = idx("Charge Code")
    amount_idx = idx("Amount")
    move_in_idx = idx("Move In")
    lease_exp_idx = idx("Lease Expiration")

    units: List[RentRollUnitRow] = []
    charges: List[RentRollChargeRow] = []

    current_unit: Optional[RentRollUnitRow] = None
    for row in values[header_row_idx:]:
        unit = _as_str(row[unit_idx])
        charge_code = _as_str(row[charge_code_idx])
        amount_raw = row[amount_idx]

        if unit and not unit.lower().startswith("current/notice/vacant"):
            if not any(ch.isdigit() for ch in unit):
                # Skip section headers like "Square Footage" or similar.
                continue
            unit_type = _as_str(row[unit_type_idx])
            if not unit_type:
                # Totals/headers sometimes appear in the Unit column; ignore unless Unit Type is populated.
                continue
            sqft_raw = row[sqft_idx]
            resident_id = _as_str(row[resident_idx]) or None
            resident_name = _as_str(row[name_idx]) or None
            market_rent_raw = row[market_rent_idx]
            move_in_raw = row[move_in_idx]
            lease_exp_raw = row[lease_exp_idx]

            sqft = None
            if sqft_raw not in (None, ""):
                try:
                    sqft = float(sqft_raw)
                except Exception:
                    sqft = None

            market_rent = None
            if market_rent_raw not in (None, ""):
                try:
                    market_rent = float(market_rent_raw)
                except Exception:
                    market_rent = None

            move_in = None
            if isinstance(move_in_raw, date):
                move_in = move_in_raw.isoformat()
            elif _as_str(move_in_raw):
                move_in = _as_str(move_in_raw)

            lease_exp = None
            if isinstance(lease_exp_raw, date):
                lease_exp = lease_exp_raw.isoformat()
            elif _as_str(lease_exp_raw):
                lease_exp = _as_str(lease_exp_raw)

            if redact_pii:
                resident_id = None
                resident_name = None

            current_unit = RentRollUnitRow(
                unit=unit,
                unit_type=unit_type,
                sqft=sqft,
                resident_id=resident_id,
                resident_name=resident_name,
                market_rent=market_rent,
                move_in=move_in,
                lease_expiration=lease_exp,
            )
            units.append(current_unit)
            continue

        if current_unit is None:
            continue

        if not charge_code:
            continue

        if charge_code.lower() == "total":
            current_unit = None
            continue

        if amount_raw is None or amount_raw == "":
            continue

        try:
            amount = float(amount_raw)
        except Exception:
            continue

        charges.append(RentRollChargeRow(unit=current_unit.unit, charge_code=charge_code, amount=amount))

    return RentRollExtract(
        property_name=property_name,
        as_of_date=as_of_date,
        month_year=month_year,
        units=units,
        charges=charges,
    )


def parse_yardi_statement_12_months(xlsx_path: Path) -> T12Extract:
    wb = load_workbook(xlsx_path, data_only=True)
    try:
        ws = wb.worksheets[0]
        values = _sheet_values(ws, max_cols=min(40, ws.max_column))
    finally:
        wb.close()

    property_name: Optional[str] = None
    for row in values[:20]:
        s = _as_str(row[0])
        if s and s.lower() not in ("statement (12 months)",) and "Period =" not in s and "Book =" not in s:
            property_name = s.strip()
            break

    header_row_idx = None
    months: List[str] = []
    for i, row in enumerate(values[:200], start=1):
        candidate_months = []
        for v in row:
            label = _as_str(v)
            m = _parse_month_label(label)
            if m:
                candidate_months.append(m)
        if len(candidate_months) >= 6:
            header_row_idx = i
            months = candidate_months
            break
    if header_row_idx is None:
        raise ValueError("Could not find month header row for statement report")

    period_start, period_end = _month_range(months)

    rows: List[T12MonthlyRow] = []
    for row in values[header_row_idx:]:
        acct = _as_str(row[0])
        name = _as_str(row[1])
        if not acct or not _ACCT_CODE_RE.match(acct):
            continue
        if not name:
            continue

        for j, month in enumerate(months):
            col_idx = 2 + j
            if col_idx >= len(row):
                continue
            raw = row[col_idx]
            amount = None
            if raw not in (None, ""):
                try:
                    amount = float(raw)
                except Exception:
                    amount = None
            rows.append(T12MonthlyRow(account_code=acct, account_name=name.strip(), month=month, amount=amount))

    return T12Extract(property_name=property_name, period_start=period_start, period_end=period_end, rows=rows)


def parse_yardi_unknown(xlsx_path: Path, redact_pii: bool = True) -> Dict[str, Any]:
    kind = detect_yardi_report_type(xlsx_path)
    if kind == "rent_roll_with_lease_charges":
        rr = parse_yardi_rent_roll_with_lease_charges(xlsx_path, redact_pii=redact_pii)
        return {"kind": kind, "rent_roll": rr}
    if kind == "statement_12_months":
        t12 = parse_yardi_statement_12_months(xlsx_path)
        return {"kind": kind, "t12": t12}
    return {"kind": "unknown"}


def derive_canonical_inputs_from_rent_roll(
    rent_roll: RentRollExtract,
    analysis_start: str,
    analysis_end: str,
    deal_id: str,
    run_id: str,
    analyst: str = "yardi_ingest",
) -> Dict[str, Any]:
    start_month = month_id(analysis_start)
    end_month = month_id(analysis_end)

    unit_type_to_units: Dict[str, List[RentRollUnitRow]] = {}
    for u in rent_roll.units:
        unit_type_to_units.setdefault(u.unit_type, []).append(u)

    unit_cohorts = []
    market_rent_curve = []
    loss_to_lease = []
    physical_vacancy_curve = []

    total_units = len(rent_roll.units)

    # Approximate occupancy: if we redacted PII, resident fields are None; treat all as occupied if resident_id/name missing.
    occupied_units = total_units
    vacancy_rate = 0.0

    for unit_type, units in unit_type_to_units.items():
        cohort_id = unit_type
        unit_count = len(units)
        avg_sqft = None
        sqft_vals = [u.sqft for u in units if u.sqft is not None]
        if sqft_vals:
            avg_sqft = sum(sqft_vals) / len(sqft_vals)

        market_vals = [u.market_rent for u in units if u.market_rent is not None]
        avg_market = sum(market_vals) / len(market_vals) if market_vals else 0.0

        # Use rent charge code as in-place rent proxy when present; otherwise fall back to market.
        rent_by_unit: Dict[str, float] = {}
        for ch in rent_roll.charges:
            if ch.charge_code.lower() == "rent":
                rent_by_unit[ch.unit] = ch.amount

        inplace_vals = [rent_by_unit.get(u.unit) for u in units if rent_by_unit.get(u.unit) is not None]
        avg_inplace = sum(inplace_vals) / len(inplace_vals) if inplace_vals else avg_market

        ltl = 0.0
        if avg_market > 0:
            ltl = max(0.0, min(1.0, 1.0 - (avg_inplace / avg_market)))

        unit_cohorts.append(
            {
                "cohort_id": cohort_id,
                "unit_type": unit_type,
                "unit_count": unit_count,
                "sqft": avg_sqft,
                "initial_inplace_rent": avg_inplace,
            }
        )

        market_rent_curve.append(
            {
                "cohort_id": cohort_id,
                "start_period": start_month,
                "end_period": end_month,
                "market_rent": avg_market,
            }
        )
        loss_to_lease.append(
            {
                "cohort_id": cohort_id,
                "start_period": start_month,
                "end_period": end_month,
                "ltl_percent": ltl,
            }
        )
        physical_vacancy_curve.append(
            {
                "cohort_id": cohort_id,
                "start_period": start_month,
                "end_period": end_month,
                "vacancy_rate": vacancy_rate,
            }
        )

    # Programs derived from non-rent charge codes (excluding fee-like patterns).
    fee_like_re = re.compile(r"(late|nsf|fee|fines?|penalt)", re.IGNORECASE)
    charge_codes = sorted({c.charge_code for c in rent_roll.charges if c.charge_code and c.charge_code.lower() != "rent"})

    charges_by_code: Dict[str, List[RentRollChargeRow]] = {}
    for c in rent_roll.charges:
        if c.charge_code.lower() == "rent":
            continue
        charges_by_code.setdefault(c.charge_code, []).append(c)

    revenue_programs = []
    program_adoption_curve = []
    excluded_codes: List[str] = []

    for code in charge_codes:
        if fee_like_re.search(code):
            excluded_codes.append(code)
            continue

        rows = charges_by_code.get(code, [])
        if not rows:
            continue
        if any(r.amount < 0 for r in rows):
            # Negative charge codes are typically discounts/credits and are not modeled as revenue programs in v0.1.
            excluded_codes.append(code)
            continue
        units_with_charge = {r.unit for r in rows}
        adoption = (len(units_with_charge) / total_units) if total_units else 0.0
        avg_price = sum(r.amount for r in rows) / len(rows)
        if avg_price < 0:
            excluded_codes.append(code)
            continue

        revenue_programs.append(
            {
                "program_id": code,
                "program_name": code,
                "program_type": "tenant-based",
                "pricing_type": "$/unit",
                "price_value": avg_price,
                "eligible_units": "ALL",
                "start_period": start_month,
                "end_period": end_month,
            }
        )
        program_adoption_curve.append(
            {
                "program_id": code,
                "start_period": start_month,
                "end_period": end_month,
                "adoption_rate": adoption,
            }
        )

    inputs: Dict[str, Any] = {
        "schema_version": "0.1",
        "metadata": {
            "deal_id": deal_id,
            "run_id": run_id,
            "as_of_date": rent_roll.as_of_date or parse_month(analysis_end).isoformat(),
            "analyst": analyst,
            "purpose": "Ingested",
            "notes": "Autogenerated from Yardi rent roll charges report. Collection loss defaults to 0.0; vacancy defaults to 0.0 in v0.1 ingest.",
        },
        "time_grid": {"analysis_start_date": parse_month(analysis_start).isoformat(), "analysis_end_date": parse_month(analysis_end).isoformat()},
        "unit_cohorts": unit_cohorts,
        "market_rent_curve": market_rent_curve,
        "loss_to_lease": loss_to_lease,
        "physical_vacancy_curve": physical_vacancy_curve,
        "collection_loss_curve": [{"applies_to": "ALL", "start_period": start_month, "end_period": end_month, "loss_rate": 0.0}],
        "revenue_programs": revenue_programs,
        "program_adoption_curve": program_adoption_curve,
    }
    if excluded_codes:
        inputs["metadata"]["notes"] += f" Excluded charge codes from revenue_programs: {sorted(set(excluded_codes))}."
    return inputs
