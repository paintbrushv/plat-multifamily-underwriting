"""Tests for T12 → opex_table canonical format parser."""
import importlib
import re
import shutil
from datetime import datetime
from pathlib import Path

import pytest
from openpyxl import Workbook

from engine.ingest import t12_parser as t12_parser_mod
from engine.ingest.t12_parser import (
    _CATEGORY_MAP,
    OpexGroup,
    ParseWarning,
    _detect_hierarchy,
    _is_subcategory_subtotal,
    _matches_category_map,
    _matches_section_total,
    _normalize_subtotal_category,
    _normalize_category,
    _resolve_groups,
    _strip_trailing_total_column,
    _text_indent_level,
    _to_float,
    parse_t12,
    parse_t12_with_provenance,
)

FIXTURE = Path("tests/fixtures/sample_t12.csv")
LENNAR_FIXTURE = Path("tests/fixtures/lennar_style_t12.csv")


def test_parse_returns_list():
    result = parse_t12(FIXTURE)
    assert isinstance(result, list)
    assert len(result) > 0


def test_each_category_has_required_fields():
    result = parse_t12(FIXTURE)
    for cat in result:
        assert "category_name" in cat, f"Missing category_name: {cat}"
        assert "calculation_type" in cat
        assert "base_value" in cat
        assert "recoverable_flag" in cat


def test_base_value_is_annualized():
    result = parse_t12(FIXTURE)
    payroll = next(c for c in result if "payroll" in c["category_name"].lower())
    # T12 total: 8*12000 + 4*12200 = 144800
    assert payroll["base_value"] > 100_000


def test_calculation_type_is_fixed_annual():
    result = parse_t12(FIXTURE)
    for cat in result:
        assert cat["calculation_type"] == "fixed_annual"


def test_re_tax_not_recoverable():
    result = parse_t12(FIXTURE)
    re_tax = next((c for c in result if "tax" in c["category_name"].lower()), None)
    assert re_tax is not None
    assert re_tax["recoverable_flag"] is False


def test_utilities_are_recoverable():
    """Water and electricity should be recoverable."""
    result = parse_t12(FIXTURE)
    water = next((c for c in result if "water" in c["category_name"].lower()), None)
    assert water is not None
    assert water["recoverable_flag"] is True

    elec = next((c for c in result if "electric" in c["category_name"].lower()), None)
    assert elec is not None
    assert elec["recoverable_flag"] is True


def test_csv_and_copy_both_work(tmp_path):
    """Parser works from any path — test a copy."""
    csv_copy = tmp_path / "test.csv"
    shutil.copy(FIXTURE, csv_copy)
    result = parse_t12(csv_copy)
    assert len(result) > 0


def test_all_categories_parsed():
    result = parse_t12(FIXTURE)
    # Sample has 11 rows after header
    assert len(result) == 11


def test_category_names_are_canonical():
    """No raw T12 labels should leak through — all should be normalized."""
    result = parse_t12(FIXTURE)
    names = [c["category_name"] for c in result]
    # "Payroll & Benefits" in CSV → "Payroll" canonical
    assert "Payroll" in names
    # "Water & Sewer" stays "Water & Sewer"
    assert "Water & Sewer" in names
    # "Real Estate Taxes" stays
    assert "Real Estate Taxes" in names
    # "Property Management Fee" stays
    assert "Property Management Fee" in names


def test_unsupported_extension_raises():
    with pytest.raises(ValueError, match="Unsupported"):
        parse_t12(Path("file.txt"))


def test_parse_text_based_pdf_t12(monkeypatch, tmp_path):
    pdf_path = tmp_path / "Financials-Dylan-T12-4.2026.pdf"
    pdf_path.write_bytes(b"%PDF")
    layout_text = """
Account                                           May 2025      Jun 2025       Jul 2025     Aug 2025      Sep 2025      Oct 2025      Nov 2025      Dec 2025      Jan 2026      Feb 2026      Mar 2026     Apr 2026       Adjusted      Variance
  INCOME
    Rental Income
      Gross Potential Rent                      172,120.00    172,120.00    172,120.00    172,120.00    172,120.00    172,120.00    172,120.00    172,120.00    172,120.00    172,120.00    172,120.00   172,120.00   2,065,440.00    688,480.00
  TOTAL INCOME                                  173,478.43    163,657.46    185,722.94    173,751.46    179,675.39    164,720.01    178,599.30    188,278.29    168,549.20    166,597.54    191,865.58   184,804.24   2,119,699.84    592,029.72
  EXPENSE
    Administration Expense
      Tenant Credit Check                           154.34        440.98        264.58        308.68        110.23        396.88        122.27        220.04        331.20        264.83        176.57       308.97       3,099.57     (1,320.85)
      Office Supplies                                0.00          0.00        108.23        282.42        286.50          0.00        124.63          0.00        367.04          0.00        244.09       141.88       1,554.79       (710.15)
     Total Administration Expense                   154.34        440.98        372.81        591.10        396.73        396.88        246.90        220.04        698.24        264.83        420.66       450.85       4,654.36     (2,031.00)
"""
    monkeypatch.setattr(t12_parser_mod, "_extract_pdf_layout_text", lambda path: layout_text)

    result = parse_t12(pdf_path)

    by_name = {row["category_name"]: row["base_value"] for row in result}
    assert by_name["Tenant Credit Check"] == 3099.57
    assert by_name["Office Supplies"] == 1554.79
    assert "Gross Potential Rent" not in by_name


def test_payroll_correct_value():
    result = parse_t12(FIXTURE)
    payroll = next(c for c in result if c["category_name"] == "Payroll")
    # 8 months × $12,000 + 4 months × $12,200 = $96,000 + $48,800 = $144,800
    assert payroll["base_value"] == 144_800.0


# --- Wave 3 / Task 3.2 fixes (Bugs 1.6, 1.7, 1.11) ---------------------


def test_longest_pattern_wins():
    """Bug 1.11: 'Security Contract Services' must map to 'Security'.

    Under first-match-wins iteration of _CATEGORY_MAP, the
    'contract serv' pattern (declared earlier in the list) would
    win and the row would be mis-classified as 'Contract Services'.
    The fix uses earliest-position-wins (with longest-pattern as
    tiebreaker), so 'security' matching at position 0 beats
    'contract serv' matching at position 9.
    """
    canonical, _ = _normalize_category("Security Contract Services")
    assert canonical == "Security", (
        f"Expected 'Security' (earliest-position match) but got {canonical!r}"
    )


def test_longest_pattern_tiebreaker():
    """When two patterns match at the same position, longer wins.

    'contract serv' (13 chars) beats 'admin' if both started at
    the same position. Use a synthesized example: 'maintenance'
    (11 chars) and 'admin' (5 chars) — but they don't co-occur.
    Instead test 'real estate tax' (15) vs 're tax' (6) which
    both match starting at position 0 in 'real estate tax bill';
    longest wins → 'Real Estate Taxes'. (Both happen to map to
    the same canonical, but the test confirms ordering didn't
    accidentally pick a shorter pattern.)
    """
    canonical, _ = _normalize_category("Real Estate Tax Bill")
    assert canonical == "Real Estate Taxes"


def test_negative_credit_preserved():
    """Bug 1.7: negative inputs (credits) must NOT be abs()'d."""
    assert _to_float(-500) == -500.0
    assert _to_float("-500") == -500.0
    assert _to_float("-1,234.56") == -1234.56


def test_parens_negative_still_handled():
    """Bug 1.7: '(500)' accounting-style negatives still parse to -500."""
    assert _to_float("(500)") == -500.0
    assert _to_float("(1,234.56)") == -1234.56
    assert _to_float("$(500)") == -500.0


def test_positive_value_stays_positive():
    """Sanity: positive values are unchanged."""
    assert _to_float(500) == 500.0
    assert _to_float("500") == 500.0
    assert _to_float("$1,234.56") == 1234.56


def test_strip_trailing_total_column_when_present():
    assert _strip_trailing_total_column([100.0, 200.0, 300.0]) == [100.0, 200.0]
    assert _strip_trailing_total_column([100.0, 200.0, 305.0]) == [100.0, 200.0, 305.0]


def test_employee_health_insurance_maps_to_payroll():
    canonical, recoverable = _normalize_category("Employee Health Insurance")
    assert canonical == "Payroll"
    assert recoverable is False


def test_account_code_prefixed_salary_maps_to_payroll():
    canonical, recoverable = _normalize_category("6110 Manager Salary")
    assert canonical == "Payroll"
    assert recoverable is False


def test_recoverable_or_semantics(tmp_path):
    """Bug 1.6: when two rows merge, recoverable=True from any source wins.

    Row 1: 'Electricity - Vacant Units' (recoverable via 'electric')
    Row 2: also 'Electricity - Common Areas' (recoverable via 'electric')
    Both flagged recoverable here — but we need to test the
    OR-merge logic across a False/True case. Use 'Admin' (False)
    + 'Other Operating' (False) won't help. Construct a synthetic
    case with manual recoverable mapping by feeding two rows that
    map to the same canonical but where one source pattern is
    flagged recoverable and the other is not.

    Looking at _CATEGORY_MAP: 'water' (recoverable=True) and
    'sewer' (recoverable=True) both → 'Water & Sewer'. No
    False/True pairing maps to the same canonical.

    So this test fabricates the scenario by patching _CATEGORY_MAP
    with a False/True pair that share a canonical, runs parse_t12,
    and confirms the merged flag is True.
    """
    # Patch _CATEGORY_MAP for this test only
    original = list(t12_parser_mod._CATEGORY_MAP)
    original_sorted = list(t12_parser_mod._SORTED_CATEGORY_MAP)
    try:
        t12_parser_mod._CATEGORY_MAP = [
            ("alpha-cat", "MergedCat", False),
            ("beta-cat", "MergedCat", True),
        ]
        t12_parser_mod._SORTED_CATEGORY_MAP = sorted(
            t12_parser_mod._CATEGORY_MAP, key=lambda t: len(t[0]), reverse=True
        )

        csv_path = tmp_path / "merge.csv"
        csv_path.write_text(
            "Category,M1,M2\n"
            "Alpha-Cat Row,100,100\n"
            "Beta-Cat Row,50,50\n"
        )
        result = parse_t12(csv_path)
        merged = next(c for c in result if c["category_name"] == "MergedCat")
        assert merged["recoverable_flag"] is True, (
            "OR-merge: any True input should yield True merged flag"
        )
        assert merged["base_value"] == 300.0
    finally:
        t12_parser_mod._CATEGORY_MAP = original
        t12_parser_mod._SORTED_CATEGORY_MAP = original_sorted


def test_recoverable_or_first_true(tmp_path):
    """Bug 1.6: True-then-False ordering still yields True (commutative OR)."""
    original = list(t12_parser_mod._CATEGORY_MAP)
    original_sorted = list(t12_parser_mod._SORTED_CATEGORY_MAP)
    try:
        t12_parser_mod._CATEGORY_MAP = [
            ("alpha-cat", "MergedCat", True),
            ("beta-cat", "MergedCat", False),
        ]
        t12_parser_mod._SORTED_CATEGORY_MAP = sorted(
            t12_parser_mod._CATEGORY_MAP, key=lambda t: len(t[0]), reverse=True
        )

        csv_path = tmp_path / "merge_ft.csv"
        csv_path.write_text(
            "Category,M1,M2\n"
            "Alpha-Cat Row,100,100\n"
            "Beta-Cat Row,50,50\n"
        )
        result = parse_t12(csv_path)
        merged = next(c for c in result if c["category_name"] == "MergedCat")
        assert merged["recoverable_flag"] is True
    finally:
        t12_parser_mod._CATEGORY_MAP = original
        t12_parser_mod._SORTED_CATEGORY_MAP = original_sorted


def test_hierarchy_skips_unmapped_subtotal_only_rollups() -> None:
    groups = [
        OpexGroup(
            subsection_header=None,
            detail_rows=[],
            subtotal_row=("Total Salary & Wages", 358166.71),
            indent_level=2,
        ),
        OpexGroup(
            subsection_header=None,
            detail_rows=[],
            subtotal_row=("Total Payroll", 560787.94),
            indent_level=2,
        ),
    ]
    warnings: list[ParseWarning] = []

    rows = _resolve_groups(groups, warnings)

    assert rows == [("Payroll", [560787.94])]
    assert warnings == []


def test_total_payroll_suppresses_nested_payroll_subtotals() -> None:
    groups = [
        OpexGroup(
            subsection_header=None,
            detail_rows=[],
            subtotal_row=("Total Admin Salaries", 139533.77),
            indent_level=2,
        ),
        OpexGroup(
            subsection_header=None,
            detail_rows=[],
            subtotal_row=("Total Payroll Taxes", 122396.09),
            indent_level=2,
        ),
        OpexGroup(
            subsection_header=None,
            detail_rows=[],
            subtotal_row=("Total Payroll", 560787.94),
            indent_level=2,
        ),
    ]
    warnings: list[ParseWarning] = []

    rows = _resolve_groups(groups, warnings)

    assert rows == [("Payroll", [560787.94])]
    assert warnings == []


def test_resolve_groups_skips_parent_rollups_that_repeat_prior_subtotals() -> None:
    groups = [
        OpexGroup(
            subsection_header="MANAGEMENT FEE EXPENSE",
            detail_rows=[("Management Fee Expense", 100.0)],
            subtotal_row=("TOTAL MANAGEMENT FEE EXPENSE", 100.0),
            indent_level=1,
        ),
        OpexGroup(
            subsection_header="MARKETING",
            detail_rows=[("Marketing", 50.0)],
            subtotal_row=("TOTAL MARKETING", 50.0),
            indent_level=1,
        ),
        OpexGroup(
            subsection_header=None,
            detail_rows=[],
            subtotal_row=("TOTAL ADMINISTRATIVE EXPENSE", 150.0),
            indent_level=0,
        ),
        OpexGroup(
            subsection_header="SALARIES & WAGES",
            detail_rows=[("Office Salaries", 120.0)],
            subtotal_row=("TOTAL SALARIES & WAGES", 120.0),
            indent_level=1,
        ),
        OpexGroup(
            subsection_header="PAYROLL TAXES & OVERHEAD",
            detail_rows=[("Payroll Taxes", 80.0)],
            subtotal_row=("TOTAL PAYROLL TAXES & OVERHEAD", 80.0),
            indent_level=1,
        ),
        OpexGroup(
            subsection_header=None,
            detail_rows=[],
            subtotal_row=("TOTAL PAYROLL & BENEFITS", 200.0),
            indent_level=0,
        ),
        OpexGroup(
            subsection_header="CONTRACT SERVICES",
            detail_rows=[("Contract Services", 60.0)],
            subtotal_row=("TOTAL CONTRACT SERVICES", 60.0),
            indent_level=1,
        ),
        OpexGroup(
            subsection_header="REPAIRS & MAINTENANCE",
            detail_rows=[("Repairs & Maintenance", 40.0)],
            subtotal_row=("TOTAL REPAIRS & MAINTENANCE", 40.0),
            indent_level=1,
        ),
        OpexGroup(
            subsection_header=None,
            detail_rows=[],
            subtotal_row=("TOTAL MAINTENANCE EXPENSE", 100.0),
            indent_level=0,
        ),
    ]
    warnings: list[ParseWarning] = []

    rows = _resolve_groups(groups, warnings)

    assert sum(values[0] for _, values in rows) == 450.0
    assert ("Administrative", [150.0]) not in rows
    assert ("Payroll", [200.0]) not in rows
    assert ("Repairs & Maintenance", [100.0]) not in rows
    assert warnings == []


def test_resolve_groups_keeps_unrelated_subtotal_collision() -> None:
    groups = [
        OpexGroup(
            subsection_header=None,
            detail_rows=[],
            subtotal_row=("TOTAL UTILITIES", 100.0),
            indent_level=0,
        ),
        OpexGroup(
            subsection_header=None,
            detail_rows=[],
            subtotal_row=("TOTAL INSURANCE", 200.0),
            indent_level=0,
        ),
        OpexGroup(
            subsection_header=None,
            detail_rows=[],
            subtotal_row=("TOTAL REAL ESTATE TAXES", 300.0),
            indent_level=0,
        ),
    ]
    warnings: list[ParseWarning] = []

    rows = _resolve_groups(groups, warnings)

    assert rows == [
        ("Utilities", [100.0]),
        ("Insurance", [200.0]),
        ("Real Estate Taxes", [300.0]),
    ]
    assert warnings == []


def test_source_rows_provenance(tmp_path):
    """Bonus: source_rows accumulates raw category names per canonical.

    Provenance is exposed via parse_t12_with_provenance() rather
    than embedded in opex_table dicts because the canonical schema
    forbids extra fields. The opex_table return value remains
    schema-clean.
    """
    csv_path = tmp_path / "prov.csv"
    csv_path.write_text(
        "Category,M1,M2\n"
        "Water Utility Charges,100,100\n"
        "Sewer Service Fees,50,50\n"
    )
    opex_table, provenance = parse_t12_with_provenance(csv_path)

    # opex_table dicts are schema-clean (no source_rows leaking in).
    water_entry = next(c for c in opex_table if c["category_name"] == "Water & Sewer")
    assert set(water_entry.keys()) == {
        "category_name",
        "calculation_type",
        "base_value",
        "recoverable_flag",
    }

    # Provenance dict carries both raw labels.
    assert "Water & Sewer" in provenance
    raws = provenance["Water & Sewer"]
    assert "Water Utility Charges" in raws
    assert "Sewer Service Fees" in raws
    assert len(raws) == 2

    # And the plain parse_t12() return matches opex_table — no
    # extra fields slip through.
    plain = parse_t12(csv_path)
    assert plain == opex_table


def test_excel_total_column_not_double_counted(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.append(["Category", "M1", "M2", "Total"])
    ws.append(["Prop & Gen Liab Insurance - Impounded", 100.0, 200.0, 300.0])
    path = tmp_path / "t12_totals.xlsx"
    wb.save(path)

    result = parse_t12(path)
    ins = next(c for c in result if c["category_name"] == "Insurance")
    assert ins["base_value"] == 300.0


def test_excel_account_code_first_layout_uses_next_text_label(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.append(["Statement"] + [None] * 14)
    ws.append(["OPERATING EXPENSE"] + [None] * 14)
    ws.append([60015, "Salaries - Manager", 100.0, 110.0, 120.0])
    ws.append([60035, "Salaries - Asst. Manager", 50.0, 60.0, 70.0])
    ws.append([60349, "TOTAL SALARIES AND BENEFITS", 150.0, 170.0, 190.0])
    path = tmp_path / "account_code_first.xlsx"
    wb.save(path)

    result = parse_t12(path)

    names = {c["category_name"] for c in result}
    assert "Salaries - Manager" in names
    assert "Salaries - Asst. Manager" in names
    manager = next(c for c in result if c["category_name"] == "Salaries - Manager")
    asst = next(c for c in result if c["category_name"] == "Salaries - Asst. Manager")
    assert manager["base_value"] == 330.0
    assert asst["base_value"] == 180.0


def test_excel_annualized_summary_columns_not_double_counted(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.append(["Statement"] + [None] * 31)
    header = [None] * 32
    header[3] = "Category"
    for month_idx, col_idx in enumerate(range(5, 28, 2), start=1):
        header[col_idx] = f"M{month_idx}"
    header[28] = "Total"
    header[29] = "T-9 Annualized"
    header[30] = "T-6 Annualized"
    header[31] = "T-3 Annualized"
    ws.append(header)
    ws.append(["EXPENSE"] + [None] * 31)
    row = [None] * 32
    row[3] = "6110 Manager Salary"
    month_values = [float(n * 100) for n in range(1, 13)]
    for value, col_idx in zip(month_values, range(5, 28, 2)):
        row[col_idx] = value
    row[28] = sum(month_values)
    row[29] = 8200.0
    row[30] = 8000.0
    row[31] = 7800.0
    ws.append(row)
    path = tmp_path / "t12_annualized.xlsx"
    wb.save(path)

    result = parse_t12(path)
    payroll = next(c for c in result if c["category_name"] == "Payroll")
    assert payroll["base_value"] == sum(month_values)


def test_module_level_sort_idempotent():
    """Re-importing the module must not reorder _CATEGORY_MAP itself.

    _CATEGORY_MAP stays in human-readable declaration order;
    _SORTED_CATEGORY_MAP is the longest-first view used at runtime.
    """
    before = list(_CATEGORY_MAP)
    importlib.reload(t12_parser_mod)
    after = list(t12_parser_mod._CATEGORY_MAP)
    assert before == after, "_CATEGORY_MAP must be stable across imports"
    # And the sorted view should still be longest-first.
    sorted_view = t12_parser_mod._SORTED_CATEGORY_MAP
    lengths = [len(p) for p, _, _ in sorted_view]
    assert lengths == sorted(lengths, reverse=True), (
        "_SORTED_CATEGORY_MAP must be longest-pattern-first"
    )


def test_full_statement_csv_uses_operating_expense_section_only(tmp_path):
    """Full P&L exports should ignore income rows and subtotal rollups."""
    csv_path = tmp_path / "full_statement.csv"
    csv_path.write_text(
        "Category,M1,M2\n"
        "REVENUE,,\n"
        "Market Rent - All Units,1000,1000\n"
        "TOTAL INCOME,1000,1000\n"
        "OPERATING EXPENSES,,\n"
        "Payroll Taxes,100,100\n"
        "Water,50,50\n"
        "TOTAL PERSONNEL EXPENSE,100,100\n"
        "NON-OPERATING EXPENSES,,\n"
        "Interest Expense,25,25\n"
    )

    result = parse_t12(csv_path)
    names = {row["category_name"] for row in result}

    assert "Payroll" in names
    assert "Water & Sewer" in names
    assert "Market Rent - All Units" not in names
    assert "Interest Expense" not in names
    assert "Total Personnel Expense" not in names


def test_full_statement_excel_uses_operating_expense_section_only(tmp_path):
    """Excel statements should use macro-section context like live PMS exports."""
    openpyxl = pytest.importorskip("openpyxl")
    xlsx_path = tmp_path / "full_statement.xlsx"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Property Statement", None, None])
    ws.append(["Category", "Apr", "May"])
    ws.append(["REVENUE", None, None])
    ws.append(["Late Fees", 100, 100])
    ws.append(["OPERATING EXPENSES", None, None])
    ws.append(["Management Fees", 50, 50])
    ws.append(["TOTAL MANAGEMENT SERVICES", 50, 50])
    ws.append(["NON-OPERATING EXPENSES", None, None])
    ws.append(["Interest Expense", 25, 25])
    wb.save(xlsx_path)
    wb.close()

    result = parse_t12(xlsx_path)
    names = {row["category_name"] for row in result}

    assert "Property Management Fee" in names
    assert "Late Fees" not in names
    assert "Interest Expense" not in names
    assert "Total Management Services" not in names


def test_interest_expense_header_stops_operating_expense_window(tmp_path):
    """Owner exports may use `Interest Expense` as the non-op section header."""
    openpyxl = pytest.importorskip("openpyxl")
    xlsx_path = tmp_path / "interest_header_statement.xlsx"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Property Statement", None, None])
    ws.append(["Category", "Apr", "May"])
    ws.append(["REVENUE", None, None])
    ws.append(["Market Rent", 1000, 1000])
    ws.append(["TOTAL INCOME", 1000, 1000])
    ws.append(["EXPENSE", None, None])
    ws.append(["Management Fees", 50, 50])
    ws.append(["Repairs", 75, 75])
    ws.append(["Interest Expense", None, None])
    ws.append(["Mortgage int exp - first", 500, 500])
    ws.append(["Total Interest Expense", 500, 500])
    ws.append(["Net Income After Int Exp & Debt Fin", 375, 375])
    ws.append(["Building Improvements", 25, 25])
    wb.save(xlsx_path)
    wb.close()

    result = parse_t12(xlsx_path)
    names = {row["category_name"] for row in result}

    assert "Property Management Fee" in names
    assert "Repairs & Maintenance" in names
    assert "Mortgage Int Exp - First" not in names
    assert "Building Improvements" not in names


def test_account_code_prefixed_total_rows_are_not_additive_expenses(tmp_path):
    """Rows like `5000 Total Administrative Expenses` are section rollups."""
    openpyxl = pytest.importorskip("openpyxl")
    xlsx_path = tmp_path / "account_prefixed_totals.xlsx"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Statement", None, None, None])
    ws.append(["Account", "Account Name", "Jan", "Feb"])
    ws.append(["INCOME", None, None, None])
    ws.append(["TOTAL INCOME", None, 1000, 1000])
    ws.append(["EXPENSE", None, None, None])
    ws.append([None, "    5000 Administrative Expenses", None, None])
    ws.append([None, "        5010 Answering Service", 100, 100])
    ws.append([None, "        5020 Office Supplies", 50, 50])
    ws.append([None, "        5000 Total Administrative Expenses", 150, 150])
    ws.append(["TOTAL", None, "EXPENSE", None])
    wb.save(xlsx_path)
    wb.close()

    result = parse_t12(xlsx_path)
    by_name = {row["category_name"]: row["base_value"] for row in result}

    assert by_name["5010 Answering Service"] == 200
    assert by_name["5020 Office Supplies"] == 100
    assert "5000 Total Administrative Expenses" not in by_name


def test_column_split_total_expense_and_noi_rows_are_not_expenses(tmp_path):
    """Rows whose visible label resolves to EXPENSE or NOI are statement totals."""
    openpyxl = pytest.importorskip("openpyxl")
    xlsx_path = tmp_path / "split_total_expense.xlsx"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Statement", None, None, None])
    ws.append(["Account", "Account Name", "Jan", "Feb", "Total"])
    ws.append(["INCOME", None, None, None, None])
    ws.append(["TOTAL", None, "INCOME", 1000, 1000])
    ws.append(["EXPENSE", None, None, None, None])
    ws.append([None, "    5000 Administrative Expenses", None, None, None])
    ws.append([None, "        5010 Answering Service", 100, 100, 200])
    ws.append([None, "        5000 Total Administrative Expenses", 100, 100, 200])
    ws.append(["TOTAL", None, "EXPENSE", 100, 100, 200])
    ws.append(["NOI", None, None, 900, 900, 1800])
    wb.save(xlsx_path)
    wb.close()

    result = parse_t12(xlsx_path)
    by_name = {row["category_name"]: row["base_value"] for row in result}

    assert by_name == {"5010 Answering Service": 200}


def test_excel_statement_skips_section_subtotals_without_total_prefix(tmp_path):
    """Some owner exports subtotal sections with the bare section label."""
    openpyxl = pytest.importorskip("openpyxl")
    xlsx_path = tmp_path / "section_subtotals.xlsx"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Statement", None, None, None])
    ws.append(["Account", "Account Name", "Jan", "Feb", "Total"])
    ws.append(["TOTAL INCOME", None, None, None])
    ws.append(["Expenses", None, None, None])
    ws.append(["Administrative", None, None, None])
    ws.append(["6110-01", "Office Expense", 100, 100, 200])
    ws.append([None, "Administrative", 100, 100, 200])
    ws.append(["Utilities", None, None, None])
    ws.append(["6180-05", "Water", 50, 50, 100])
    ws.append([None, "Utilities", 50, 50, 100])
    ws.append([None, "Manager Controlled Expenses", 150, 150, 300])
    ws.append([None, "Expenses", 150, 150, 300])
    wb.save(xlsx_path)
    wb.close()

    result = parse_t12(xlsx_path)
    by_name = {row["category_name"]: row["base_value"] for row in result}

    assert by_name["Office Expense"] == 200
    assert by_name["Water & Sewer"] == 100
    assert "Administrative" not in by_name
    assert "Utilities" not in by_name
    assert "Manager Controlled Expenses" not in by_name
    assert "Expenses" not in by_name


def test_excel_statement_skips_coded_section_subtotals_without_total_prefix(tmp_path):
    """S2 exports use account-code section headers and bare subtotal labels."""
    openpyxl = pytest.importorskip("openpyxl")
    xlsx_path = tmp_path / "coded_section_subtotals.xlsx"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["S2 Trailing Profit and Loss", None, None, None])
    ws.append(["Ledger Account", "Jan", "Feb", "Total"])
    ws.append(["TOTAL INCOME", 1000, 1000, 2000])
    ws.append(["EXPENSE", None, None, None])
    ws.append(["5100:Utility Expense", 0, 0, 0])
    ws.append(["5110:Utility - Electric - House", 100, 100, 200])
    ws.append(["5130:Utility - Water/Sewer", 50, 50, 100])
    ws.append(["Utilities", 150, 150, 300])
    ws.append(["5200:Contract Services", 0, 0, 0])
    ws.append(["5210:Contract - Security", 25, 25, 50])
    ws.append(["Contract Services", 25, 25, 50])
    ws.append(["6200:Managment Fees", 50, 50, 100])
    ws.append(["Management Fees", 50, 50, 100])
    ws.append(["Total Controllable Expenses", 175, 175, 350])
    ws.append(["TOTAL EXPENSE", 175, 175, 350])
    ws.append(["NET OPERATING INCOME", 825, 825, 1650])
    wb.save(xlsx_path)
    wb.close()

    result = parse_t12(xlsx_path)
    by_name = {row["category_name"]: row["base_value"] for row in result}

    assert by_name["Utilities"] == 300
    assert by_name["Security"] == 50
    assert by_name["Property Management Fee"] == 100
    assert by_name.get("Contract Services") is None


def test_column_shifted_statement_finds_expense_labels_after_total_income(tmp_path):
    """Layouts like JC Hart can put labels several columns to the right."""
    openpyxl = pytest.importorskip("openpyxl")
    xlsx_path = tmp_path / "shifted_statement.xlsx"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["The Property", None, None, None, None, None, None, None])
    ws.append([None, None, None, None, None, datetime(2025, 3, 31), None, datetime(2025, 4, 30)])
    ws.append(["INCOME", None, None, None, None, None, None, None])
    ws.append([None, None, None, "5110 Gross Potential Income", None, 1000, None, 1000])
    ws.append(["TOTAL INCOME", None, None, None, None, 1000, None, 1000])
    ws.append(["EXPENSE", None, None, None, None, None, None, None])
    ws.append([None, None, "Direct Labor", None, None, None, None, None])
    ws.append([None, None, None, "6110 Manager Salary", None, 100, None, 1200])
    ws.append([None, None, None, "6210 Water", None, 50, None, 600])
    ws.append([None, None, None, "TOTAL DIRECT LABOR", None, 100, None, 1200])
    ws.append(["NON-OPERATING EXPENSES", None, None, None, None, None, None, None])
    ws.append([None, None, None, "Interest Expense", None, 25, None, 300])
    wb.save(xlsx_path)
    wb.close()

    result = parse_t12(xlsx_path)
    names = {row["category_name"] for row in result}

    assert "Payroll" in names
    assert "Water & Sewer" in names
    assert "Gross Potential Income" not in names
    assert "Interest Expense" not in names
    assert "Total Direct Labor" not in names


def test_total_revenue_boundary_starts_expenses_without_explicit_header(tmp_path):
    """If no expense header exists, start after the final income subtotal."""
    csv_path = tmp_path / "income_boundary_only.csv"
    csv_path.write_text(
        "Category,M1,M2\n"
        "REVENUE,,\n"
        "Gross Potential Rent,1000,1000\n"
        "Other Income,100,100\n"
        "TOTAL OPERATING REVENUE,1100,1100\n"
        "6110 Manager Salary,100,100\n"
        "6210 Water,50,50\n"
        "NON-OPERATING EXPENSES,,\n"
        "Interest Expense,25,25\n"
    )

    result = parse_t12(csv_path)
    names = {row["category_name"] for row in result}

    assert "Payroll" in names
    assert "Water & Sewer" in names
    assert "Gross Potential Rent" not in names
    assert "Other Income" not in names
    assert "Interest Expense" not in names


def test_controllable_rollups_are_not_expense_categories(tmp_path):
    """Owner exports may include non-total rollups inside the expense window."""
    csv_path = tmp_path / "controllable_rollups.csv"
    csv_path.write_text(
        "Category,M1,M2\n"
        "TOTAL INCOME,1000,1000\n"
        "Payroll,100,100\n"
        "Controllable Expenses,100,100\n"
        "Controllable Operating Income,900,900\n"
        "Real Estate Taxes,50,50\n"
    )

    result = parse_t12(csv_path)
    names = {row["category_name"] for row in result}

    assert "Payroll" in names
    assert "Real Estate Taxes" in names
    assert "Controllable Expenses" not in names
    assert "Controllable Operating Income" not in names


def test_other_income_subtotal_is_not_expense_boundary(tmp_path):
    """A full P&L can have Total Other Income before final Total Income."""
    csv_path = tmp_path / "other_income_before_total_income.csv"
    csv_path.write_text(
        "Category,M1,M2,Total\n"
        "Other Income,,,\n"
        "Approval Fee,10,10,20\n"
        "Water & Sewer Reimbursement,20,20,40\n"
        "Total Other Income,30,30,60\n"
        "Total Income,1000,1000,2000\n"
        "Payroll,,,\n"
        "Payroll - Office,100,100,200\n"
        "Total Payroll,100,100,200\n"
        "Utilities,,,\n"
        "Utilities - Water Sewer,50,50,100\n"
        "Total Utilities,50,50,100\n"
        "Controllable Expenses,150,150,300\n"
        "Controllable Operating Income,850,850,1700\n"
        "Fixed Expenses,,,\n"
        "Management Fees,25,25,50\n"
        "Real Estate Taxes,75,75,150\n"
        "Building Insurance,15,15,30\n"
        "Total Fixed Expenses,115,115,230\n"
        "Total Operating Expenses,265,265,530\n"
        "Net Operating Income,735,735,1470\n"
    )

    result = parse_t12(csv_path)
    by_name = {row["category_name"]: row["base_value"] for row in result}

    assert "Approval Fee" not in by_name
    assert "Other_Opex" not in by_name
    assert "Total Fixed Expenses" not in by_name
    assert by_name["Payroll"] == 200.0
    assert by_name["Utilities"] == 100.0
    assert by_name["Property Management Fee"] == 50.0


# ── Hierarchy helpers ───────────────────────────────────────────────────────


def test_is_subcategory_subtotal_title_case():
    assert _is_subcategory_subtotal("Total Personnel Expense") is True
    assert _is_subcategory_subtotal("Total Utilities") is True
    assert _is_subcategory_subtotal("Total Contract Services") is True


def test_is_subcategory_subtotal_rejects_section_totals():
    assert _is_subcategory_subtotal("TOTAL OPERATING EXPENSES") is False
    assert _is_subcategory_subtotal("Total Operating Expenses") is False
    assert _is_subcategory_subtotal("Net Operating Income") is False
    assert _is_subcategory_subtotal("Total Revenue") is False
    assert _is_subcategory_subtotal("Total Income") is False
    assert _is_subcategory_subtotal("Effective Gross Income") is False


def test_is_subcategory_subtotal_rejects_non_total_rows():
    assert _is_subcategory_subtotal("513110 - Salary - Manager") is False
    assert _is_subcategory_subtotal("Personnel Expense") is False
    assert _is_subcategory_subtotal("") is False


def test_matches_section_total():
    assert _matches_section_total("TOTAL OPERATING EXPENSES") is True
    assert _matches_section_total("Total Operating Expenses") is True
    assert _matches_section_total("Net Operating Income") is True
    assert _matches_section_total("Effective Gross Revenue") is True
    assert _matches_section_total("Total Revenue") is True
    assert _matches_section_total("Total Personnel Expense") is False
    assert _matches_section_total("Total Utilities") is False


def test_normalize_subtotal_category_known_patterns():
    assert _normalize_subtotal_category("Total Personnel Expense") == "Payroll"
    assert _normalize_subtotal_category("TOTAL PAYROLL") == "Payroll"
    assert _normalize_subtotal_category("Total Utilities") == "Utilities"
    assert _normalize_subtotal_category("Total Repairs And Maintenance") == "Repairs & Maintenance"
    assert _normalize_subtotal_category("Total Administrative") == "Administrative"
    assert _normalize_subtotal_category("Total Marketing") == "Marketing / Advertising"
    assert _normalize_subtotal_category("Total Management Fee") == "Property Management Fee"
    assert _normalize_subtotal_category("Total Contract Services") == "Contract Services"
    assert _normalize_subtotal_category("Total Insurance") == "Insurance"
    assert _normalize_subtotal_category("Total Security") == "Security"
    assert _normalize_subtotal_category("Total Landscaping") == "Landscaping / Grounds"
    assert _normalize_subtotal_category("OFFICE ADMINISTRATION") == "Administrative"
    assert _normalize_subtotal_category("TOTAL RESIDENT RELATED EXPENSES") == "Other Operating Expenses"
    assert _normalize_subtotal_category("MAKE READY MAINTENANCE") == "Turnover / Make-Ready"


def test_normalize_subtotal_category_no_match_returns_none():
    assert _normalize_subtotal_category("Total Debt Service") is None
    assert _normalize_subtotal_category("Total Capex Reserve") is None
    assert _normalize_subtotal_category("Random Label") is None


def test_text_indent_level_spaces():
    assert _text_indent_level("    text") == 2
    assert _text_indent_level("  text") == 1
    assert _text_indent_level("text") == 0
    assert _text_indent_level("") == 0
    assert _text_indent_level("        text") == 4


def test_matches_category_map_known():
    assert _matches_category_map("513110 - Salary - Manager") == "Payroll"
    assert _matches_category_map("516110 - Electricity - Common") == "Electricity"
    assert _matches_category_map("Real Estate Taxes") == "Real Estate Taxes"


def test_matches_category_map_unknown_returns_none():
    assert _matches_category_map("513120 - Bonuses And Incentives") is None
    assert _matches_category_map("Completely Unknown Line Item") is None


def test_opex_group_and_parse_warning_are_dataclasses():
    group = OpexGroup(
        subsection_header="Personnel",
        detail_rows=[],
        subtotal_row=None,
        indent_level=1,
    )
    assert group.subsection_header == "Personnel"
    warning = ParseWarning(
        tier_reached=4,
        raw_label="Unknown",
        amount=1000.0,
        category_assigned="other_opex",
    )
    assert warning.tier_reached == 4


# ── Hierarchy grouping ──────────────────────────────────────────────────────


def _lennar_opex_rows() -> list[tuple[str, str, list[float | None]]]:
    return [
        ("Personnel Expense", "  Personnel Expense", [None] * 12),
        ("513110 - Salary - Manager", "    513110 - Salary - Manager", [7000.0] * 12),
        ("513120 - Bonuses", "    513120 - Bonuses", [500.0] * 12),
        ("Total Personnel Expense", "  Total Personnel Expense", [7500.0] * 12),
        ("Utilities", "  Utilities", [None] * 12),
        ("516110 - Electricity", "    516110 - Electricity", [1500.0] * 12),
        ("516210 - Water And Sewer", "    516210 - Water And Sewer", [800.0] * 12),
        ("Total Utilities", "  Total Utilities", [2300.0] * 12),
        ("Administrative", "  Administrative", [None] * 12),
        ("517110 - Office Supplies", "    517110 - Office Supplies", [100.0] * 12),
        ("Total Administrative", "  Total Administrative", [100.0] * 12),
    ]


def test_detect_hierarchy_returns_three_groups():
    assert len(_detect_hierarchy(_lennar_opex_rows())) == 3


def test_detect_hierarchy_group_subtotals():
    groups = _detect_hierarchy(_lennar_opex_rows())
    subtotal_labels = [group.subtotal_row[0] for group in groups]
    assert "Total Personnel Expense" in subtotal_labels
    assert "Total Utilities" in subtotal_labels
    assert "Total Administrative" in subtotal_labels


def test_detect_hierarchy_subtotal_amounts():
    groups = _detect_hierarchy(_lennar_opex_rows())
    by_subtotal = {group.subtotal_row[0]: group.subtotal_row[1] for group in groups}
    assert by_subtotal["Total Personnel Expense"] == 7500.0 * 12
    assert by_subtotal["Total Utilities"] == 2300.0 * 12
    assert by_subtotal["Total Administrative"] == 100.0 * 12


def test_detect_hierarchy_strips_trailing_total_column():
    rows = [
        ("Personnel Expense", "  Personnel Expense", [None] * 13),
        ("513110 - Salary - Manager", "    513110 - Salary - Manager", [7000.0] * 12 + [84000.0]),
        ("Total Personnel Expense", "  Total Personnel Expense", [7000.0] * 12 + [84000.0]),
        ("Utilities", "  Utilities", [None] * 13),
        ("516110 - Electricity", "    516110 - Electricity", [1000.0] * 12 + [12000.0]),
        ("Total Utilities", "  Total Utilities", [1000.0] * 12 + [12000.0]),
    ]

    groups = _detect_hierarchy(rows)
    by_subtotal = {group.subtotal_row[0]: group.subtotal_row[1] for group in groups}
    assert by_subtotal["Total Personnel Expense"] == 84000.0
    assert by_subtotal["Total Utilities"] == 12000.0


def test_detect_hierarchy_caps_to_first_12_months_when_extra_numeric_column_is_not_total():
    rows = [
        ("Personnel Expense", "  Personnel Expense", [None] * 13),
        ("513110 - Salary - Manager", "    513110 - Salary - Manager", [100.0] * 13),
        ("Total Personnel Expense", "  Total Personnel Expense", [100.0] * 13),
        ("Utilities", "  Utilities", [None] * 13),
        ("516110 - Electricity", "    516110 - Electricity", [50.0] * 13),
        ("Total Utilities", "  Total Utilities", [50.0] * 13),
    ]

    groups = _detect_hierarchy(rows)
    by_subtotal = {}
    for group in groups:
        assert group.subtotal_row is not None
        by_subtotal[group.subtotal_row[0]] = group.subtotal_row[1]
    assert by_subtotal["Total Personnel Expense"] == 1200.0
    assert by_subtotal["Total Utilities"] == 600.0


def test_detect_hierarchy_detail_rows_stored():
    groups = _detect_hierarchy(_lennar_opex_rows())
    payroll_group = next(
        group for group in groups if group.subtotal_row and "Personnel" in group.subtotal_row[0]
    )
    detail_labels = [label for label, _ in payroll_group.detail_rows]
    assert "513110 - Salary - Manager" in detail_labels
    assert "513120 - Bonuses" in detail_labels


def test_detect_hierarchy_flat_returns_empty():
    flat_rows = [
        ("Payroll Taxes", "Payroll Taxes", [100.0] * 12),
        ("Water", "Water", [50.0] * 12),
        ("Total Personnel", "Total Personnel", [100.0] * 12),
    ]
    assert _detect_hierarchy(flat_rows) == []


def test_detect_hierarchy_stops_at_section_total():
    rows = [
        ("Personnel Expense", "  Personnel Expense", [None] * 12),
        ("513110 - Salary", "    513110 - Salary", [1000.0] * 12),
        ("Total Personnel Expense", "  Total Personnel Expense", [1000.0] * 12),
        ("Utilities", "  Utilities", [None] * 12),
        ("516110 - Electricity", "    516110 - Electricity", [500.0] * 12),
        ("Total Utilities", "  Total Utilities", [500.0] * 12),
        ("TOTAL OPERATING EXPENSES", "TOTAL OPERATING EXPENSES", [1500.0] * 12),
        ("Net Operating Income", "Net Operating Income", [8500.0] * 12),
    ]
    groups = _detect_hierarchy(rows)
    assert len(groups) == 2
    for group in groups:
        assert group.subtotal_row is not None
        assert "Net Operating Income" not in [label for label, _ in group.detail_rows]


def test_detect_hierarchy_group_without_subtotal_included():
    rows = [
        ("Personnel Expense", "  Personnel Expense", [None] * 12),
        ("513110 - Salary", "    513110 - Salary", [1000.0] * 12),
        ("Total Personnel Expense", "  Total Personnel Expense", [1000.0] * 12),
        ("Utilities", "  Utilities", [None] * 12),
        ("516110 - Electricity", "  516110 - Electricity", [500.0] * 12),
    ]
    assert _detect_hierarchy(rows) == []


# ── Hierarchy resolution ────────────────────────────────────────────────────


def test_resolve_groups_uses_subtotal_value_not_detail_sum():
    groups = [
        OpexGroup(
            subsection_header="Personnel Expense",
            detail_rows=[
                ("513110 - Salary - Manager", 84_000.0),
                ("513120 - Bonuses", 6_000.0),
                ("513130 - Overtime", 2_400.0),
            ],
            subtotal_row=("Total Personnel Expense", 92_400.0),
            indent_level=1,
        ),
    ]
    warnings: list[ParseWarning] = []
    rows = _resolve_groups(groups, warnings)
    assert len(rows) == 1
    label, values = rows[0]
    assert label == "Payroll"
    assert sum(v for v in values if v is not None) == 92_400.0
    assert warnings == []


def test_resolve_groups_tier2_header_fallback():
    groups = [
        OpexGroup(
            subsection_header="Total Personnel",
            detail_rows=[("Salary", 50_000.0)],
            subtotal_row=("Unrecognized Subtotal Label", 50_000.0),
            indent_level=1,
        ),
    ]
    warnings: list[ParseWarning] = []
    rows = _resolve_groups(groups, warnings)
    assert rows[0][0] == "Payroll"
    assert warnings == []


def test_resolve_groups_tier3_detail_fallback():
    groups = [
        OpexGroup(
            subsection_header="Misc Expense",
            detail_rows=[("Real Estate Taxes Paid", 120_000.0)],
            subtotal_row=("Total Misc Expense", 120_000.0),
            indent_level=1,
        ),
    ]
    warnings: list[ParseWarning] = []
    rows = _resolve_groups(groups, warnings)
    assert rows[0][0] == "Real Estate Taxes"
    assert warnings == []


def test_resolve_groups_tier4_other_opex_with_warning():
    groups = [
        OpexGroup(
            subsection_header="Special Assessment",
            detail_rows=[("Unrecognized Line 1", 5_000.0)],
            subtotal_row=("Total Special Assessment", 5_000.0),
            indent_level=1,
        ),
    ]
    warnings: list[ParseWarning] = []
    rows = _resolve_groups(groups, warnings)
    assert rows[0][0] == "other_opex"
    assert len(warnings) == 1
    assert warnings[0].tier_reached == 4
    assert warnings[0].amount == 5_000.0
    assert warnings[0].category_assigned == "other_opex"


def test_resolve_groups_multiple_unmatched_aggregate_to_one_other_opex():
    groups = [
        OpexGroup(
            subsection_header="Unknown A",
            detail_rows=[("Line A", 1_000.0)],
            subtotal_row=("Total Unknown A", 1_000.0),
            indent_level=1,
        ),
        OpexGroup(
            subsection_header="Unknown B",
            detail_rows=[("Line B", 2_000.0)],
            subtotal_row=("Total Unknown B", 2_000.0),
            indent_level=1,
        ),
    ]
    warnings: list[ParseWarning] = []
    rows = _resolve_groups(groups, warnings)
    assert all(label == "other_opex" for label, _ in rows)
    assert len(warnings) == 2


def test_resolve_groups_no_subtotal_sums_detail_rows():
    groups = [
        OpexGroup(
            subsection_header="Payroll",
            detail_rows=[("Salary Row", 6_000.0), ("Benefits Row", 1_200.0)],
            subtotal_row=None,
            indent_level=1,
        ),
    ]
    warnings: list[ParseWarning] = []
    rows = _resolve_groups(groups, warnings)
    label, values = rows[0]
    assert label == "Payroll"
    assert sum(v for v in values if v is not None) == 7_200.0


# ── Integration: Lennar-style hierarchical T12 ──────────────────────────────


def test_lennar_fixture_no_account_code_leakthrough():
    result = parse_t12(LENNAR_FIXTURE)
    names = {row["category_name"] for row in result}
    for name in names:
        assert not re.match(r"^\d{5,6}", name), (
            f"Account-code leakthrough: {name!r} should not appear as a canonical category"
        )


def test_lennar_fixture_uses_subtotal_values_not_detail_sums():
    result = parse_t12(LENNAR_FIXTURE)
    by_name = {row["category_name"]: row["base_value"] for row in result}
    assert by_name["Payroll"] == 92_400.0
    assert by_name["Utilities"] == 27_600.0
    assert by_name["Administrative"] == 1_800.0


def test_lennar_fixture_exactly_three_categories():
    result = parse_t12(LENNAR_FIXTURE)
    assert len(result) == 3, f"Expected 3 categories, got: {[r['category_name'] for r in result]}"


def test_lennar_fixture_no_other_opex():
    result = parse_t12(LENNAR_FIXTURE)
    names = {row["category_name"] for row in result}
    assert "other_opex" not in names


def test_lennar_fixture_no_parse_warnings():
    _, provenance = parse_t12_with_provenance(LENNAR_FIXTURE)
    assert provenance.get("_warnings", []) == [], (
        f"Unexpected warnings: {provenance['_warnings']}"
    )


def test_lennar_fixture_provenance_source_rows():
    _, provenance = parse_t12_with_provenance(LENNAR_FIXTURE)
    assert "Payroll" in provenance
    payroll_sources = provenance["Payroll"]
    assert payroll_sources


def test_existing_flat_fixture_still_works_alongside_lennar():
    flat_result = parse_t12(FIXTURE)
    lennar_result = parse_t12(LENNAR_FIXTURE)
    assert len(flat_result) == 11
    assert len(lennar_result) == 3


def test_operating_income_boundary_and_controllable_expense_section(tmp_path):
    path = tmp_path / "legacy_style_t12.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["Label", "M1", "M2", "Total"])
    ws.append(["REVENUE", None, None, None])
    ws.append(["Market Rent", 1000, 1000, 2000])
    ws.append(["TOTAL OPERATING INCOME", 1000, 1000, 2000])
    ws.append(["CONTROLLABLE EXPENSES", None, None, None])
    ws.append(["PAYROLL AND BENEFITS", None, None, None])
    ws.append(["6100-0005 - Payroll - Office", 100, 100, 200])
    ws.append(["TOTAL PAYROLL AND BENEFITS", 100, 100, 200])
    ws.append(["ADMINISTRATIVE EXPENSES", None, None, None])
    ws.append(["6600-0002 - Admin - Office Expense", 10, 10, 20])
    ws.append(["TOTAL ADMINISTRATIVE EXPENSES", 10, 10, 20])
    ws.append(["TOTAL CONTROLLABLE EXPENSES", 110, 110, 220])
    ws.append(["Funds from Operations", 890, 890, 1780])
    ws.append(["NON-CONTROLLABLE EXPENSE", None, None, None])
    ws.append(["TAXES AND INSURANCE", None, None, None])
    ws.append(["7100-0001 - Taxes - Real Estate", 70, 70, 140])
    ws.append(["7100-0025 - Insurance - Property/Liability", 30, 30, 60])
    ws.append(["TOTAL TAXES AND INSURANCE", 100, 100, 200])
    ws.append(["TOTAL NON-CONTROLLABLE EXPENSE", 100, 100, 200])
    ws.append(["OPERATING EXPENSES", 210, 210, 420])
    ws.append(["NET OPERATING INCOME (LOSS)", 790, 790, 1580])
    wb.save(path)

    result = parse_t12(path)
    by_name = {row["category_name"]: row["base_value"] for row in result}

    assert by_name["Payroll"] == 200.0
    assert by_name["Administrative"] == 20.0
    assert by_name["Real Estate Taxes"] == 140.0
    assert by_name["Insurance"] == 60.0
    assert "Utilities" not in by_name
    assert "Other_Opex" not in by_name


def test_mixed_detail_rows_before_subtotal_are_not_collapsed_to_first_category(tmp_path):
    """Some owner P&Ls list detail rows first, then only later subtotal nested sections."""
    path = tmp_path / "mixed_details_then_subtotals.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append([None, None, None, None, None, "Jan", "Feb"])
    ws.append([None, None, "Income", None, None, None, None])
    ws.append([None, None, None, "RENT", None, 1000, 1000])
    ws.append([None, None, "Total Income", None, None, 1000, 1000])
    ws.append([None, None, "Expense", None, None, None, None])
    ws.append([None, None, None, "Wages", None, 100, 100])
    ws.append([None, None, None, "ADVERTISING", None, 10, 10])
    ws.append([None, None, None, "OFFICE", None, 7, 7])
    ws.append([None, None, None, "MANAGEMENT", None, 30, 30])
    ws.append([None, None, None, "PROPERTY INSURANCE", None, 40, 40])
    ws.append([None, None, None, "REPAIR AND MAINTENANCE", None, None, None])
    ws.append([None, None, None, None, "PAINT", 5, 5])
    ws.append([None, None, None, "Total REPAIR AND MAINTENANCE", None, 5, 5])
    ws.append([None, None, None, "PROPERTY TAX", None, 70, 70])
    ws.append([None, None, None, "TRASH", None, 20, 20])
    ws.append([None, None, None, "UTILITIES", None, None, None])
    ws.append([None, None, None, None, "ELECTRIC", 15, 15])
    ws.append([None, None, None, "Total UTILITIES", None, 15, 15])
    ws.append([None, None, "Total Expense", None, None, 290, 290])
    wb.save(path)

    result = parse_t12(path)
    by_name = {row["category_name"]: row["base_value"] for row in result}

    assert by_name["Payroll"] == 200.0
    assert by_name["Marketing / Advertising"] == 20.0
    assert by_name["Office"] == 14.0
    assert by_name["Property Management Fee"] == 60.0
    assert by_name["Insurance"] == 80.0
    assert by_name["Repairs & Maintenance"] == 10.0
    assert by_name["Real Estate Taxes"] == 140.0
    assert by_name["Utilities"] == 30.0
    assert by_name["Trash"] == 40.0


def test_numeric_operating_expenses_rollup_is_not_additive(tmp_path):
    """A final Operating Expenses rollup is a summary row, not an opex category."""
    path = tmp_path / "operating_expenses_rollup.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["Account Name", "Jan", "Feb", "Total"])
    ws.append(["Income", None, None, None])
    ws.append(["Rental Income", 1000, 1000, 2000])
    ws.append(["Total Income", 1000, 1000, 2000])
    ws.append(["Expenses", None, None, None])
    ws.append(["Utilities - Electric", 10, 10, 20])
    ws.append(["Insurance", 20, 20, 40])
    ws.append(["Property Taxes", 30, 30, 60])
    ws.append(["Operating Expenses", 60, 60, 120])
    ws.append(["Net Operating Income", 940, 940, 1880])
    wb.save(path)

    result = parse_t12(path)
    by_name = {row["category_name"]: row["base_value"] for row in result}

    assert by_name["Utilities"] == 20.0
    assert by_name["Insurance"] == 40.0
    assert by_name["Real Estate Taxes"] == 60.0
    assert "Operating Expenses" not in by_name


def test_excel_loader_selects_statement_sheet_instead_of_active_summary(tmp_path):
    """Multi-sheet workbooks should parse the real statement, not wb.active roulette."""
    path = tmp_path / "multi_sheet_t12.xlsx"
    wb = Workbook()
    summary = wb.active
    assert summary is not None
    summary.title = "Summary"
    summary.append(["Dashboard only"])
    statement = wb.create_sheet("T12 Statement")
    statement.append(["Account Name", "Jan", "Feb", "Total"])
    statement.append(["Income", None, None, None])
    statement.append(["Rent Received", 1000, 1000, 2000])
    statement.append(["Total Income", 1000, 1000, 2000])
    statement.append(["Expenses", None, None, None])
    statement.append(["Utilities", 10, 10, 20])
    statement.append(["Insurance", 20, 20, 40])
    statement.append(["Net Income", 970, 970, 1940])
    wb.save(path)

    result = parse_t12(path)
    by_name = {row["category_name"]: row["base_value"] for row in result}

    assert by_name["Utilities"] == 20.0
    assert by_name["Insurance"] == 40.0


def test_bare_income_summary_row_is_not_additive(tmp_path):
    """A bare Income summary row should not leak into opex_table."""
    path = tmp_path / "income_summary.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["Account Name", "Apr", "May", "Jun", "Total"])
    ws.append(["Income", None, None, None, None])
    ws.append(["Rent Received", 100, 100, 100, 300])
    ws.append(["Total Income", 100, 100, 100, 300])
    ws.append(["Expenses", None, None, None, None])
    ws.append(["Utilities", 10, 10, 10, 30])
    ws.append(["Net Income", 90, 90, 90, 270])
    wb.save(path)

    result = parse_t12(path)
    by_name = {row["category_name"]: row["base_value"] for row in result}

    assert by_name["Utilities"] == 30.0
    assert "Income" not in by_name
