from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


def stable_property_tax_source_locator(path: str | Path) -> str:
    """Return an invocation-independent logical locator for OM tax evidence."""
    om_path = Path(path)
    raw_input_indexes = [
        index
        for index, segment in enumerate(om_path.parts)
        if segment == "raw_inputs"
    ]
    if raw_input_indexes:
        logical_path = Path(*om_path.parts[raw_input_indexes[-1]:])
    else:
        logical_path = Path(om_path.name)
    return f"{logical_path.as_posix()}, property tax section"


def parse_broker_snapshot_from_om(path: str | Path) -> dict[str, Any]:
    om_path = Path(path)
    text = _extract_layout_text(om_path)
    property_facts = _extract_property_facts(text, om_path)
    trailing_actuals, forecast, notes = _extract_income_statement(text)
    tax_notes = _extract_tax_analysis(text)
    notes.update(tax_notes)
    property_tax_evidence = _pop_property_tax_evidence(notes, om_path)

    missing_sections: list[str] = []
    matched_sections: list[str] = []

    if property_facts:
        matched_sections.append("property_facts")
    else:
        missing_sections.append("property_facts")

    if trailing_actuals.get("noi") is not None:
        matched_sections.append("broker_noi")
        matched_sections.append("noi_table")
    else:
        missing_sections.extend(["broker_noi", "noi_table"])

    if any(key in forecast for key in _INCOME_BUCKETS):
        matched_sections.append("income_table")
    else:
        missing_sections.append("income_table")

    if any(key in forecast for key in _EXPENSE_BUCKETS):
        matched_sections.append("expense_table")
    else:
        missing_sections.append("expense_table")

    if property_tax_evidence:
        matched_sections.append("property_tax_section")
    else:
        missing_sections.append("property_tax_section")

    revenue_assumptions = {k: forecast[k] for k in _INCOME_BUCKETS if k in forecast}
    expense_assumptions = {k: forecast[k] for k in _EXPENSE_BUCKETS if k in forecast}

    return {
        "deal_name": property_facts.get("deal_name") or om_path.stem,
        "source": om_path.name,
        "address": property_facts.get("address"),
        "year_built": property_facts.get("year_built"),
        "unit_count": property_facts.get("unit_count"),
        "trailing_noi": trailing_actuals.get("noi"),
        "noi": forecast.get("noi"),
        "revenue_assumptions": revenue_assumptions,
        "expense_assumptions": expense_assumptions,
        "underwriting_notes": notes,
        "property_tax_evidence_candidates": property_tax_evidence,
        "parser_metadata": {
            "parser_family": "layout_text_om_v1",
            "matched_sections": matched_sections,
            "missing_sections": missing_sections,
        },
        "trailing_actuals": trailing_actuals,
    }


def _pop_property_tax_evidence(
    notes: dict[str, Any],
    om_path: Path,
) -> list[dict[str, Any]]:
    source_locator = stable_property_tax_source_locator(om_path)
    evidence: list[dict[str, Any]] = []
    for producer, field in (
        ("broker_snapshot_mills", "property_tax_millage_rate"),
        ("broker_snapshot_percentage_points", "property_tax_rate_pct"),
    ):
        if field not in notes:
            continue
        evidence.append(
            {
                "producer": producer,
                field: notes.pop(field),
                "source": "offering_memorandum",
                "source_locator": source_locator,
            }
        )
    return evidence


def enrich_broker_snapshot_from_om(
    existing_snapshot: dict[str, Any],
    *,
    broker_json_path: str | Path | None = None,
    om_path: str | Path | None = None,
) -> tuple[dict[str, Any], dict[str, float] | None]:
    needs_enrichment = not (
        existing_snapshot.get("noi") is not None
        and existing_snapshot.get("revenue_assumptions")
        and existing_snapshot.get("expense_assumptions")
    )
    if not needs_enrichment:
        trailing = existing_snapshot.get("trailing_actuals")
        return existing_snapshot, trailing if isinstance(trailing, dict) else None

    resolved_om = Path(om_path) if om_path else _find_om_near_broker_json(broker_json_path)
    if resolved_om is None or not resolved_om.exists():
        return existing_snapshot, None

    parsed = parse_broker_snapshot_from_om(resolved_om)
    merged = dict(existing_snapshot)
    for key in ("deal_name", "source", "address", "year_built", "unit_count", "trailing_noi", "noi"):
        if parsed.get(key) not in (None, "", {}, []):
            merged[key] = parsed[key]
    if not merged.get("revenue_assumptions"):
        merged["revenue_assumptions"] = parsed["revenue_assumptions"]
    if not merged.get("expense_assumptions"):
        merged["expense_assumptions"] = parsed["expense_assumptions"]

    merged_notes = dict(parsed.get("underwriting_notes", {}))
    merged_notes.update(existing_snapshot.get("underwriting_notes", {}))
    merged["underwriting_notes"] = merged_notes
    merged_candidates = list(
        existing_snapshot.get("property_tax_evidence_candidates", [])
    )
    for candidate in parsed.get("property_tax_evidence_candidates", []):
        if candidate not in merged_candidates:
            merged_candidates.append(candidate)
    if merged_candidates:
        merged["property_tax_evidence_candidates"] = merged_candidates

    merged["parser_metadata"] = parsed["parser_metadata"]
    merged["trailing_actuals"] = parsed.get("trailing_actuals", {})
    return merged, parsed.get("trailing_actuals")


_ROW_MAP: dict[str, tuple[str, str]] = {
    "grossscheduledincome": ("gross_scheduled_income", "income"),
    "gainlosstolease": ("loss_to_lease", "income"),
    "grosspotentialincome": ("gross_potential_rent", "income"),
    "vacancyloss": ("vacancy_loss", "income"),
    "concessionsnote1": ("concessions", "income"),
    "employeemodel": ("employee_model", "income"),
    "netbaddebt": ("bad_debt", "income"),
    "totalvacancyloss": ("total_vacancy_loss", "income"),
    "netrentalincome": ("net_rental_income", "income"),
    "totalcommercialincome": ("commercial_income", "income"),
    "totalotherincome": ("other_income", "income"),
    "totalrevenue": ("total_revenue", "income"),
    "realestatetaxes": ("real_estate_taxes", "expense"),
    "propertyinsurance": ("insurance", "expense"),
    "electriccommon": ("electric_common", "expense"),
    "electricvacant": ("electric_vacant", "expense"),
    "gas": ("gas", "expense"),
    "water": ("water", "expense"),
    "sewer": ("sewer", "expense"),
    "totalfixedexpenses": ("total_fixed_expenses", "expense"),
    "payroll": ("payroll", "expense"),
    "managementfee": ("management_fees", "expense"),
    "maintenancerepairs": ("repairs_maintenance", "expense"),
    "marketingnote1": ("marketing", "expense"),
    "administrative": ("general_administrative", "expense"),
    "totalvariableexpenses": ("total_variable_expenses", "expense"),
    "totalexpenses": ("total_opex", "expense"),
    "netoperatingincome": ("noi", "expense"),
}

_INCOME_BUCKETS = {
    "gross_potential_rent",
    "loss_to_lease",
    "vacancy_loss",
    "concessions",
    "employee_model",
    "bad_debt",
    "total_vacancy_loss",
    "net_rental_income",
    "commercial_income",
    "utility_reimbursements",
    "parking_storage_income",
    "internet_income",
    "fee_income",
    "pet_income",
    "collection_previous_tenants",
    "rubs_admin_fees_taxes",
    "pest_control_reimbursement",
    "trash_reimbursement",
    "mineral_rights_income",
    "other_income",
    "total_revenue",
    "noi",
}
_EXPENSE_BUCKETS = {
    "real_estate_taxes",
    "insurance",
    "utilities",
    "payroll",
    "make_ready",
    "contract_services",
    "management_fees",
    "repairs_maintenance",
    "marketing",
    "general_administrative",
    "trash_refuse",
    "pest_control",
    "total_opex",
}


def _extract_layout_text(path: Path) -> str:
    pdftotext = shutil.which("pdftotext")
    if pdftotext:
        completed = subprocess.run(
            [pdftotext, "-layout", str(path), "-"],
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout
    from engine.ingest.om_parser import _extract_pdf_text

    return _extract_pdf_text(path)


def _extract_property_facts(text: str, path: Path) -> dict[str, Any]:
    facts: dict[str, Any] = {"deal_name": path.stem}
    cover_summary = re.search(
        r"(?P<year>(?:19|20)\d{2})\s+[\d,]+\s+SF\s+(?P<units>\d{2,4})\s+(?P<address>\d{3,6}\s+[^\n]+)\n"
        r"\s*BUILT\s+AVG\.\s+UNIT\s+UNITS\s+(?P<city>[A-Z][A-Z\s.]+,\s*[A-Z]{2}\s+\d{5})",
        text,
        re.IGNORECASE,
    )
    if cover_summary:
        street = " ".join(cover_summary.group("address").strip().title().split())
        city = " ".join(cover_summary.group("city").strip().upper().split())
        facts["address"] = f"{street}, {city}"
        facts["year_built"] = int(cover_summary.group("year"))
        facts["unit_count"] = int(cover_summary.group("units"))

    berkadia = re.search(
        r"Property Description\s+(?P<name>[^\n]+)\s+(?P<address>\d{2,6}\s+[^\n|]+)\|\s*(?P<city>[^\n]+?)\s+ASSET SUMMARY(?P<section>.*?)(?:\n\s*Unit Mix|\n\s*PROPERTY OVERVIEW|\f)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if berkadia:
        facts["deal_name"] = " ".join(berkadia.group("name").split())
        facts["address"] = " ".join(f"{berkadia.group('address').strip()}, {berkadia.group('city').strip()}".split())
        section = berkadia.group("section")
        built = re.search(r"\bBuilt\s+((?:19|20)\d{2})\b", section, re.IGNORECASE)
        if built:
            facts["year_built"] = int(built.group(1))
        units = re.search(r"\bUnits\s+(\d{2,4})\b", section, re.IGNORECASE)
        if units:
            facts["unit_count"] = int(units.group(1))
    year_inline = re.search(
        r"YEAR BUILT\s*\n\s*([^\n]+?)\s+((?:19|20)\d{2})\s*\n\s*NUMBER OF UNITS",
        text,
        re.IGNORECASE,
    )
    if year_inline:
        address_candidate = " ".join(year_inline.group(1).split())
        if re.search(r"\d", address_candidate) and re.search(r"[A-Za-z]", address_candidate) and len(address_candidate) >= 8:
            facts["address"] = address_candidate
        facts["year_built"] = int(year_inline.group(2))
    unit_inline = re.search(
        r"NUMBER OF UNITS\s+OCCUPANCY\s*\n\s*(\d{2,4})\s+\d{1,3}%",
        text,
        re.IGNORECASE,
    )
    if unit_inline:
        facts["unit_count"] = int(unit_inline.group(1))
    address_match = re.search(r"LOCATION\s*\n\s*([^\n]+)", text, re.IGNORECASE)
    if address_match and "address" not in facts:
        facts["address"] = " ".join(address_match.group(1).split())
    year_match = re.search(r"YEAR BUILT\s*\n\s*((?:19|20)\d{2})", text, re.IGNORECASE)
    if year_match:
        facts["year_built"] = int(year_match.group(1))
    unit_match = re.search(r"NUMBER OF UNITS\s*\n\s*(\d{2,4})", text, re.IGNORECASE)
    if unit_match and "unit_count" not in facts:
        facts["unit_count"] = int(unit_match.group(1))
    deal_match = re.search(r"^\s*([A-Z][A-Za-z0-9 '&.-]+)\s*\n+\s*LOCATION", text, re.MULTILINE)
    if deal_match and "address" not in facts:
        facts["deal_name"] = " ".join(deal_match.group(1).split())
    return facts


def _extract_income_statement(text: str) -> tuple[dict[str, float], dict[str, float], dict[str, Any]]:
    trailing_actuals: dict[str, float] = {}
    forecast: dict[str, float] = {}
    notes: dict[str, Any] = {}

    berkadia_trailing, berkadia_forecast, berkadia_notes = _extract_berkadia_proforma(text)
    if berkadia_forecast:
        return berkadia_trailing, berkadia_forecast, berkadia_notes

    section_match = re.search(
        r"INCOME STATEMENT(?P<section>.*?)(?:TRAILING 12 MONTH INCOME STATEMENT|FLOOR PLAN MIX)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not section_match:
        return trailing_actuals, forecast, notes
    section = section_match.group("section")
    for line in section.splitlines():
        normalized = _normalize_label(line)
        if not normalized:
            continue
        for key, (bucket, kind) in _ROW_MAP.items():
            if normalized.startswith(key):
                values = _extract_currency_amounts(line)
                if len(values) < 6:
                    continue
                annual_values = _extract_annual_values(values)
                if len(annual_values) < 6:
                    continue
                trailing_actuals[bucket] = annual_values[1]
                forecast[bucket] = annual_values[5]
                if kind == "income" and "assumption" in line.lower():
                    notes[f"{bucket}_assumption"] = " ".join(line.split())
                break

    utilities_actual = sum(
        trailing_actuals.get(k, 0.0)
        for k in ("electric_common", "electric_vacant", "gas", "water", "sewer")
    )
    utilities_forecast = sum(
        forecast.get(k, 0.0)
        for k in ("electric_common", "electric_vacant", "gas", "water", "sewer")
    )
    if utilities_actual:
        trailing_actuals["utilities"] = round(utilities_actual, 2)
    if utilities_forecast:
        forecast["utilities"] = round(utilities_forecast, 2)

    return trailing_actuals, forecast, notes


def _extract_berkadia_proforma(text: str) -> tuple[dict[str, float], dict[str, float], dict[str, Any]]:
    compact = _extract_berkadia_compact_proforma(text)
    if compact[1]:
        return compact

    section_match = re.search(
        r"^\s*Pro Forma\s*$(?P<section>.*?)(?:^\s*Income Notes\b)",
        text,
        re.IGNORECASE | re.DOTALL | re.MULTILINE,
    )
    if not section_match:
        return {}, {}, {}
    section = section_match.group("section")
    if "TOTAL OPERATING INCOME" not in section or "NET OPERATING INCOME" not in section:
        return {}, {}, {}

    revenue_rows = {
        "SCHEDULED RENT": "gross_potential_rent",
        "Less: Loss-to-Lease": "loss_to_lease",
        "Less: Vacancy": "vacancy_loss",
        "Less: Concessions": "concessions",
        "Less: Bad Debt": "bad_debt",
        "Less: Model Units": "employee_model",
        "NET RENTAL INCOME": "net_rental_income",
        "Plus: Fee Income": "fee_income",
        "Plus: Water/Sewer/Trash/Pest/Bulk Internet Income": "utility_reimbursements",
        "Plus: Valet Trash Income": "valet_trash_income",
        "Plus: Resident Insurance": "resident_insurance_income",
        "Plus: Garage Income": "parking_storage_income",
        "Plus: Other Income": "other_income",
        "TOTAL OPERATING INCOME": "total_revenue",
    }
    expense_rows = {
        "Administrative": "general_administrative",
        "Bulk Internet Expense": "bulk_internet_expense",
        "Advertising & Promotion": "marketing",
        "Payroll": "payroll",
        "Repairs & Maintenance/Turnover": "repairs_maintenance",
        "Grounds & Landscaping": "grounds_landscaping",
        "Management Fee": "management_fees",
        "Utilities (Electric/Gas)": "utilities_electric_gas",
        "Utilities (Water/Sewer)": "utilities_water_sewer",
        "Pest Control/Trash": "pest_control_trash",
        "Real Estate Taxes": "real_estate_taxes",
        "Forced Place Insurance": "forced_place_insurance",
        "Insurance": "insurance",
        "Replacement Reserve": "replacement_reserves",
        "TOTAL EXPENSES": "total_opex",
        "NET OPERATING INCOME": "noi",
    }

    trailing_actuals: dict[str, float] = {}
    forecast: dict[str, float] = {}
    notes: dict[str, Any] = {"broker_table_layout": "berkadia_proforma_v1"}
    for raw_line in section.splitlines():
        line = " ".join(raw_line.split())
        if not line:
            continue
        values = _extract_currency_amounts(line)
        if not values:
            continue
        for label, bucket in revenue_rows.items():
            if line.startswith(label) and len(values) >= 4:
                forecast[bucket] = abs(values[0]) if bucket in {"loss_to_lease", "vacancy_loss", "concessions", "bad_debt", "employee_model"} else values[0]
                trailing_actuals[bucket] = abs(values[3]) if bucket in {"loss_to_lease", "vacancy_loss", "concessions", "bad_debt", "employee_model"} else values[3]
                break
        else:
            for label, bucket in expense_rows.items():
                if not line.startswith(label):
                    continue
                annual_values = values[1:5] if len(values) >= 5 else values[:4]
                if len(annual_values) >= 4:
                    forecast[bucket] = annual_values[0]
                    trailing_actuals[bucket] = annual_values[3]
                break

    utilities_actual = trailing_actuals.get("utilities_electric_gas", 0.0) + trailing_actuals.get("utilities_water_sewer", 0.0)
    utilities_forecast = forecast.get("utilities_electric_gas", 0.0) + forecast.get("utilities_water_sewer", 0.0)
    if utilities_actual:
        trailing_actuals["utilities"] = round(utilities_actual, 2)
    if utilities_forecast:
        forecast["utilities"] = round(utilities_forecast, 2)

    millage = re.search(r"\bTax Millage Rate\s*\([^)]*\)\s+([0-9.]+)", text, re.IGNORECASE)
    if millage:
        notes["property_tax_rate_pct"] = round(float(millage.group(1)) / 10.0, 4)
        notes["property_tax_millage_rate"] = float(millage.group(1))

    return trailing_actuals, forecast, notes


def _extract_berkadia_compact_proforma(text: str) -> tuple[dict[str, float], dict[str, float], dict[str, Any]]:
    section_match = re.search(
        r"FINANCIALS\s*-\s*PROFORMA(?P<section>.*?)(?:IMPORTANT INFORMATION|FINANCIALS\s*-\s*RENT COMPS|\f)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not section_match:
        return {}, {}, {}
    section = section_match.group("section")
    if not re.search(r"Income\s+T3\s+Income\s*/\s*T12\s+Expense\s+Proforma", section, re.IGNORECASE):
        return {}, {}, {}
    if "NOI" not in section:
        return {}, {}, {}

    row_map = {
        "market rent": ("gross_scheduled_income", "income"),
        "loss to lease": ("loss_to_lease", "income"),
        "gross potential rent (gpr)": ("gross_potential_rent", "income"),
        "vacancy": ("vacancy_loss", "income"),
        "concessions": ("concessions", "income"),
        "non revenue": ("employee_model", "income"),
        "bad debt": ("bad_debt", "income"),
        "net rental income": ("net_rental_income", "income"),
        "utility reimbursements (t12)": ("utility_reimbursements", "income"),
        "collection - previous tenants": ("collection_previous_tenants", "income"),
        "parking income": ("parking_storage_income", "income"),
        "fee income": ("fee_income", "income"),
        "pet fees": ("pet_income", "income"),
        "rubs - admin fees & taxes (t12)": ("rubs_admin_fees_taxes", "income"),
        "pest control reimbursement (t12)": ("pest_control_reimbursement", "income"),
        "trash reimbursement": ("trash_reimbursement", "income"),
        "cable income": ("internet_income", "income"),
        "mineral rights income*": ("mineral_rights_income", "income"),
        "total other income": ("other_income", "income"),
        "effective gross income (egi)": ("total_revenue", "income"),
        "apartment prep/turnover": ("make_ready", "expense"),
        "administrative": ("general_administrative", "expense"),
        "marketing & promotion": ("marketing", "expense"),
        "contracted services": ("contract_services", "expense"),
        "repairs & maintenance": ("repairs_maintenance", "expense"),
        "payroll": ("payroll", "expense"),
        "utilities": ("utilities", "expense"),
        "trash/refuse": ("trash_refuse", "expense"),
        "pest control": ("pest_control", "expense"),
        "total controllable expenses": ("total_controllable_expenses", "expense"),
        "management fee (3%)": ("management_fees", "expense"),
        "insurance": ("insurance", "expense"),
        "real estate taxes": ("real_estate_taxes", "expense"),
        "total non-controllable expenses": ("total_non_controllable_expenses", "expense"),
        "total expenses": ("total_opex", "expense"),
        "noi": ("noi", "expense"),
    }

    trailing_actuals: dict[str, float] = {}
    forecast: dict[str, float] = {}
    notes: dict[str, Any] = {"broker_table_layout": "berkadia_compact_proforma_v1"}
    loss_buckets = {"loss_to_lease", "vacancy_loss", "concessions", "employee_model", "bad_debt"}

    for raw_line in section.splitlines():
        line = " ".join(raw_line.split())
        if not line:
            continue
        values = _extract_signed_currency_amounts(line)
        if len(values) < 2:
            continue
        normalized = _berkadia_compact_label(line)
        label = next(
            (candidate for candidate in row_map if normalized.startswith(_berkadia_compact_label(candidate))),
            None,
        )
        if not label:
            continue
        bucket, _kind = row_map[label]
        actual_value, forecast_value = values[0], values[1]
        if bucket in loss_buckets:
            actual_value = abs(actual_value)
            forecast_value = abs(forecast_value)
        trailing_actuals[bucket] = actual_value
        forecast[bucket] = forecast_value

    split_admin_marketing = re.search(
        r"Administrative\s+Marketing\s*&\s*Promotion\s+"
        r"(?P<admin_actual>\$[\d,]+)\s+(?P<marketing_actual>\$[\d,]+)\s+"
        r"(?P<admin_forecast>\$[\d,]+)\s+(?P<marketing_forecast>\$[\d,]+)",
        " ".join(section.split()),
        re.IGNORECASE,
    )
    if split_admin_marketing:
        trailing_actuals.setdefault("general_administrative", _parse_money(split_admin_marketing.group("admin_actual")) or 0.0)
        trailing_actuals.setdefault("marketing", _parse_money(split_admin_marketing.group("marketing_actual")) or 0.0)
        forecast.setdefault("general_administrative", _parse_money(split_admin_marketing.group("admin_forecast")) or 0.0)
        forecast.setdefault("marketing", _parse_money(split_admin_marketing.group("marketing_forecast")) or 0.0)

    utility_income_actual = sum(
        trailing_actuals.get(key, 0.0)
        for key in (
            "utility_reimbursements",
            "rubs_admin_fees_taxes",
            "pest_control_reimbursement",
            "trash_reimbursement",
        )
    )
    utility_income_forecast = sum(
        forecast.get(key, 0.0)
        for key in (
            "utility_reimbursements",
            "rubs_admin_fees_taxes",
            "pest_control_reimbursement",
            "trash_reimbursement",
        )
    )
    if utility_income_actual:
        trailing_actuals["utility_reimbursements"] = round(utility_income_actual, 2)
    if utility_income_forecast:
        forecast["utility_reimbursements"] = round(utility_income_forecast, 2)

    if not trailing_actuals.get("utilities"):
        utilities_actual = trailing_actuals.get("trash_refuse", 0.0) + trailing_actuals.get("pest_control", 0.0)
        if utilities_actual:
            trailing_actuals["utilities"] = round(utilities_actual, 2)
    if not forecast.get("utilities"):
        utilities_forecast = forecast.get("trash_refuse", 0.0) + forecast.get("pest_control", 0.0)
        if utilities_forecast:
            forecast["utilities"] = round(utilities_forecast, 2)

    mineral = re.search(
        r"mineral rights agreement.*?expires\s+([0-9]{2}/[0-9]{4})",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if mineral:
        notes["mineral_rights_expiration"] = mineral.group(1)

    return trailing_actuals, forecast, notes


def _berkadia_compact_label(line: str) -> str:
    label = line.split("$", 1)[0]
    return re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()


def _extract_tax_analysis(text: str) -> dict[str, Any]:
    notes: dict[str, Any] = {}
    pay_2026 = re.search(
        r"2025\s+pay\s+2026.*?Property Totals.*?\$([0-9,\s]+)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if pay_2026:
        notes["property_tax_estimate_pay_2026"] = _parse_money(pay_2026.group(1))
    sea1 = re.search(
        r"Tot al Tax Liab ilit y w it h S EA- 1 D ed u ct ion\s+\$([0-9,\s]+)",
        text,
        re.IGNORECASE,
    )
    if sea1:
        notes["property_tax_sea1_forecast"] = _parse_money(sea1.group(1))
    rate = re.search(r"2 0 2 6 Total Tax Rate\s+([0-9.]+)%", text, re.IGNORECASE)
    if rate:
        notes["property_tax_rate_pct"] = float(rate.group(1))
    return notes


def _normalize_label(line: str) -> str:
    label = line.split("$", 1)[0]
    return re.sub(r"[^a-z0-9]+", "", label.lower())


def _extract_currency_amounts(line: str) -> list[float]:
    cells = re.split(r"\s{2,}", line.strip())
    matches: list[str] = []
    for cell in cells[1:]:
        normalized = cell
        while True:
            compacted = re.sub(r"(?<=[\d,$]) (?=[\d,.])", "", normalized)
            if compacted == normalized:
                break
            normalized = compacted
        matches.extend(re.findall(r"\(?\s*\$\d[\d,]*(?:\.\d+)?\)?", normalized))
    if not matches:
        normalized = line
        while True:
            compacted = re.sub(r"(?<=[\d,$]) (?=[\d,.])", "", normalized)
            if compacted == normalized:
                break
            normalized = compacted
        matches = re.findall(r"\(?\s*\$\d[\d,]*(?:\.\d+)?\)?", normalized)
    values = [_parse_money(match) for match in matches]
    cleaned = [value for value in values if value is not None]
    return cleaned


def _extract_signed_currency_amounts(line: str) -> list[float]:
    normalized = line
    while True:
        compacted = re.sub(r"(?<=[\d,$]) (?=[\d,.])", "", normalized)
        if compacted == normalized:
            break
        normalized = compacted
    matches = re.findall(r"-?\(?\s*\$\d[\d,]*(?:\.\d+)?\)?", normalized)
    values = [_parse_money(match) for match in matches]
    return [value for value in values if value is not None]


def _extract_annual_values(values: list[float]) -> list[float]:
    if len(values) >= 12:
        pair_candidates = values[:12]
        annual = pair_candidates[::2]
        per_unit = pair_candidates[1::2]
        if annual and all(
            abs(unit) < 100000 and abs(amount) > abs(unit) * 20
            for amount, unit in zip(annual, per_unit, strict=False)
        ):
            return annual[:6]
    return values[:6]


def _parse_money(value: str) -> float | None:
    negative = value.strip().startswith("-")
    cleaned = (
        value.replace("$", "")
        .replace(",", "")
        .replace(" ", "")
        .replace("-", "")
        .replace("(", "-")
        .replace(")", "")
        .strip()
    )
    if not cleaned:
        return None
    try:
        parsed = float(cleaned)
        return round(-abs(parsed) if negative else parsed, 2)
    except ValueError:
        return None


def _find_om_near_broker_json(path: str | Path | None) -> Path | None:
    if path is None:
        return None
    broker_json = Path(path)
    for parent in broker_json.parents:
        raw_inputs = parent / "raw_inputs"
        if not raw_inputs.exists():
            continue
        pdfs = sorted(raw_inputs.glob("*.pdf"))
        om_candidates = [
            pdf
            for pdf in pdfs
            if re.search(r"(^|[\s_\-])(om|offering memorandum)([\s_\-]|$)", pdf.stem, re.IGNORECASE)
            or re.search(r"\boffering memorandum\b", pdf.stem, re.IGNORECASE)
        ]
        if om_candidates:
            return om_candidates[0]
        if pdfs:
            return pdfs[0]
    return None


if __name__ == "__main__":  # pragma: no cover
    import argparse

    parser = argparse.ArgumentParser(description="Parse broker underwriting snapshot from OM.")
    parser.add_argument("om")
    args = parser.parse_args()
    print(json.dumps(parse_broker_snapshot_from_om(args.om), indent=2))
