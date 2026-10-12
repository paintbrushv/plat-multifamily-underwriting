"""Regression: a Yardi ``ysi_is`` income statement must tie to its own totals.

The synthetic workbook (tests/yardi_fixtures.py) reproduces the structure of
the report: indented labels, sections closed by TOTAL rows, an OTHER INCOME
total named like its header, NET RENTAL INCOME as a subtotal, the income side
ending at EFFECTIVE GROSS INCOME, REPAIRS & MAINTENANCE nesting two sections
with their own totals, and a TAXES & INSURANCE section.

Before the fix, the income boundary was unreachable (no statement income
total), subtotals were added on top of their own lines (Utilities, Management
Fees, the TAXES & INSURANCE total, the nested R&M totals), payroll benefit
insurance landed in Insurance, all-zero lines split sections apart, and
loss/gain to lease and employee discounts counted as other income.
"""

from __future__ import annotations

import pytest

from engine.ingest.ingestion_pipeline import build_deal_from_documents
from engine.ingest.t12_parser import (
    OpexGroup,
    ParseWarning,
    _is_combined_tax_insurance,
    _is_income_boundary_row,
    _normalize_category,
    _parse_nested_sections,
    _resolve_groups,
    parse_t12_statement,
    parse_t12_with_provenance,
)
from tests.yardi_fixtures import (
    T12_EGI,
    T12_OPEX_TOTAL,
    t12_line,
    t12_section,
    write_yardi_rent_roll,
    write_yardi_t12,
)


@pytest.fixture
def yardi_t12(tmp_path):
    return write_yardi_t12(tmp_path / "ysi_is.xlsx")


def _by_name(statement):
    return {row["category_name"]: row["base_value"] for row in statement["opex_table"]}


def test_opex_and_income_tie_to_the_statement_totals(yardi_t12):
    statement = parse_t12_statement(yardi_t12)
    recon = statement["reconciliation"]

    assert statement["statement_totals"] == {"income": T12_EGI, "opex": T12_OPEX_TOTAL}
    assert recon["status"] == "PASS"
    assert recon["opex"]["parsed_total"] == pytest.approx(T12_OPEX_TOTAL, abs=0.01)
    assert recon["income"]["parsed_total"] == pytest.approx(T12_EGI, abs=0.01)
    assert statement["provenance"]["_warnings"] == []


def test_effective_gross_income_is_an_income_boundary():
    assert _is_income_boundary_row(" EFFECTIVE GROSS INCOME", True)
    assert _is_income_boundary_row("Effective Gross Revenue", True)
    assert not _is_income_boundary_row(" EFFECTIVE GROSS INCOME", False)
    assert not _is_income_boundary_row(" NET RENTAL INCOME", True)


def test_subtotals_are_never_added_on_top_of_their_lines(yardi_t12):
    by_name = _by_name(parse_t12_statement(yardi_t12))

    assert by_name["Utilities"] == pytest.approx(t12_section("TOTAL UTILITY EXPENSES"))
    assert "Electricity" not in by_name and "Water & Sewer" not in by_name
    assert by_name["Property Management Fee"] == pytest.approx(t12_line("Management Fees"))
    # Nested: the parent total equals its two child totals and is counted once.
    assert by_name["Repairs & Maintenance"] == pytest.approx(
        t12_section("TOTAL EXTERIOR & GROUNDS") + t12_section("TOTAL INTERIOR REPAIRS/MAINTENANCE")
    )
    assert by_name["Repairs & Maintenance"] == pytest.approx(t12_section("TOTAL REPAIRS & MAINTENANCE EXPENSES"))
    assert by_name["Marketing / Advertising"] == pytest.approx(t12_section("TOTAL MARKETING EXPENSES"))
    assert by_name["Administrative"] == pytest.approx(
        t12_section("TOTAL LICENSES/FEES/PERMITS") + t12_section("TOTAL GENERAL & ADMINISTRATIVE EXPENSE")
    )
    assert "Other Operating Expenses" not in by_name and "Other_Opex" not in by_name


def test_taxes_and_insurance_with_an_ampersand_split_into_taxes_and_insurance(yardi_t12):
    by_name = _by_name(parse_t12_statement(yardi_t12))

    assert by_name["Real Estate Taxes"] == pytest.approx(
        t12_line("Real Estate Taxes") + t12_line("Real Estate Taxes - Prior Year")
    )
    assert by_name["Insurance"] == pytest.approx(t12_line("Property & Liability Insurance"))
    assert by_name["Real Estate Taxes"] + by_name["Insurance"] == pytest.approx(
        t12_section("TOTAL TAXES & INSURANCE")
    )


def test_grouped_layout_splits_an_ampersand_taxes_and_insurance_subtotal():
    for label in ("TOTAL TAXES & INSURANCE", "TOTAL TAXES AND INSURANCE", "Total Tax & Insurance"):
        assert _is_combined_tax_insurance(label)
        groups = [
            OpexGroup("PAYROLL", [("Payroll - Office", 100.0)], ("TOTAL PAYROLL", 100.0), 1),
            OpexGroup(
                "TAXES & INSURANCE",
                [("Real Estate Taxes", 70.0), ("Property Insurance", 30.0)],
                (label, 100.0),
                1,
            ),
        ]
        warnings: list[ParseWarning] = []
        rows = dict((name, values[0]) for name, values in _resolve_groups(groups, warnings))
        assert rows == {"Payroll": 100.0, "Real Estate Taxes": 70.0, "Insurance": 30.0}
    assert not _is_combined_tax_insurance("TOTAL INSURANCE")


def test_payroll_benefit_insurance_stays_in_payroll(yardi_t12):
    statement = parse_t12_statement(yardi_t12)
    by_name = _by_name(statement)

    assert by_name["Payroll"] == pytest.approx(t12_section("TOTAL PAYROLL EXPENSE"))
    for benefit in ("Health/Life Insurance", "Dental Insurance", "Vision Insurance", "Workers Compensation Insurance"):
        assert benefit in statement["provenance"]["Payroll"]
        assert _normalize_category(benefit) == ("Payroll", False)
    assert statement["provenance"]["Insurance"] == ["Property & Liability Insurance"]
    assert _normalize_category("Property & Liability Insurance") == ("Insurance", False)


def test_service_contracts_keep_trash_and_landscaping_separate(yardi_t12):
    by_name = _by_name(parse_t12_statement(yardi_t12))

    trash = t12_line("Garbage & Trash Removal") + t12_line("Valet Trash Service")
    assert by_name["Trash"] == pytest.approx(trash)
    assert by_name["Landscaping / Grounds"] == pytest.approx(t12_line("Lawn Maintenance"))
    assert by_name["Contract Services"] == pytest.approx(
        t12_section("TOTAL SERVICE CONTRACTS") - trash - t12_line("Lawn Maintenance")
    )


def test_income_lines_are_classified_by_the_statement_sections(yardi_t12):
    statement = parse_t12_statement(yardi_t12)
    kinds: dict[str, float] = {}
    by_label = {}
    for line in statement["income_lines"]:
        kinds[line["kind"]] = round(kinds.get(line["kind"], 0.0) + line["annual_amount"], 2)
        by_label[line["label"]] = line["kind"]

    assert kinds["recovery"] == pytest.approx(t12_section("TOTAL RUBS INCOME"))
    assert kinds["other"] == pytest.approx(t12_section("OTHER INCOME") + t12_section("TOTAL MISC INCOME"))
    assert by_label["Cable/Internet Income"] == "other"
    assert by_label["Electric Reimbursement"] == "other"  # filed under OTHER INCOME by the statement
    assert by_label["Water/ Sewer - RUBS"] == "recovery"
    for rental in ("Market Rent", "Leases Over/Under Schedule", "Employee Rent Discount",
                   "Concessions", "Vacancy Loss", "Bad Debt", "Bad Debt Collections"):
        assert by_label[rental] == "rental", rental
    # Summary rows (TOTAL ..., NET RENTAL INCOME, the OTHER INCOME total) are not lines.
    assert not any(label.upper().startswith(("TOTAL", "NET ")) for label in by_label)
    assert list(by_label).count("Other Income") == 1


def test_management_fee_percent_uses_the_statement_egi(tmp_path, yardi_t12):
    roll = write_yardi_rent_roll(tmp_path / "rent_roll.xlsx")
    deal = build_deal_from_documents(
        property_id="Synthetic Yardi Deal",
        rent_roll_path=roll,
        t12_path=yardi_t12,
        analysis_start="2027-02",
        analysis_end="2032-01",
        rent_growth_rate=0.025,
        collection_loss_rate=0.01,
    )
    gate = deal["metadata"]["ingest_gate"]
    assert gate["status"] == "PASS"
    assert gate["t12_reconciliation"]["status"] == "PASS"

    [fee] = [row for row in deal["opex_table"] if row["category_name"] == "Property Management Fee"]
    assert fee["calculation_type"] == "percent_egr"
    assert fee["base_value"] == pytest.approx(round(t12_line("Management Fees") / T12_EGI, 6))

    programs = {p["program_id"]: p["price_value"] * 12 for p in deal["revenue_programs"]}
    assert programs["t12_utility_reimbursements"] == pytest.approx(t12_section("TOTAL RUBS INCOME"), abs=0.1)
    assert programs["t12_other_income"] == pytest.approx(t12_section("OTHER INCOME"), abs=0.1)


def test_nested_section_parse_declines_layouts_it_cannot_tie():
    def rows(*items):
        return [(label, label, values) for label, values in items]

    flat = rows(("Payroll", []), ("Payroll - Office", [10.0]), ("Utilities", []), ("Electric", [5.0]))
    assert _parse_nested_sections(flat) is None  # sections never close

    untied = rows(
        ("PAYROLL", []), ("Salaries", [100.0]), ("TOTAL PAYROLL", [110.0]),
        ("UTILITIES", []), ("Electric", [5.0]), ("TOTAL UTILITIES", [5.0]),
    )
    assert _parse_nested_sections(untied) is None  # a total that doesn't tie

    stray = rows(
        ("Insurance", [3.0]),
        ("PAYROLL", []), ("Salaries", [10.0]), ("TOTAL PAYROLL", [10.0]),
        ("UTILITIES", []), ("Electric", [5.0]), ("TOTAL UTILITIES", [5.0]),
    )
    assert _parse_nested_sections(stray) is None  # a line outside any section


def test_existing_layouts_resolve_as_before():
    from pathlib import Path

    fixtures = Path(__file__).parent / "fixtures"
    lennar, _ = parse_t12_with_provenance(fixtures / "lennar_style_t12.csv")
    assert {row["category_name"]: row["base_value"] for row in lennar} == {
        "Payroll": 92_400.0,
        "Utilities": 27_600.0,
        "Administrative": 1_800.0,
    }
