"""Regression: T12 ingest must keep every GL line and refuse to PASS when it can't.

The synthetic fixture tests/fixtures/entrata_style_t12.csv mimics an accrual
income statement export: group headers with no subtotal rows, several bare GL
lines per canonical category, rent / other / reimbursable income sections,
then Total Income, Total Expenses and NOI.

Before the fix, lines that collided on a canonical category were dropped
(only one survived), repair lines with utility words were filed as
recoverable utilities, other income was never ingested, and the deal still
validated as PASS.
"""
from __future__ import annotations

import copy
import csv
from pathlib import Path

import pytest

from engine.backsolve import _apply_revenue_quality_bridge
from engine.engine import run_underwriting
from engine.ingest import rent_roll_parser
from engine.ingest.ingestion_pipeline import build_deal_from_documents
from engine.ingest.t12_parser import parse_t12_statement, parse_t12_with_provenance
from engine.validator import validate_deal

FIXTURES = Path(__file__).parent / "fixtures"
T12 = FIXTURES / "entrata_style_t12.csv"
ROLL = FIXTURES / "sample_rent_roll.csv"

STATEMENT_OPEX = 3_087_000.0
STATEMENT_INCOME = 6_381_000.0


def _ingest(t12=T12, **kwargs):
    params = dict(
        property_id="Synthetic Statement",
        rent_roll_path=ROLL,
        t12_path=t12,
        analysis_start="2026-08",
        analysis_end="2031-07",
        rent_growth_rate=0.025,
        collection_loss_rate=0.02,
    )
    params.update(kwargs)
    return build_deal_from_documents(**params)


def _rewrite_rows(src: Path, dst: Path, edit) -> Path:
    with open(src, newline="") as f:
        rows = list(csv.reader(f))
    with open(dst, "w", newline="") as f:
        csv.writer(f).writerows(edit(rows))
    return dst


def test_colliding_gl_lines_are_summed_not_dropped():
    opex_table, provenance = parse_t12_with_provenance(T12)
    by_name = {row["category_name"]: row["base_value"] for row in opex_table}

    assert by_name["Payroll"] == 810_000.0
    assert by_name["Utilities"] == 600_000.0
    assert by_name["Turnover / Make-Ready"] == 81_000.0
    assert sum(by_name.values()) == pytest.approx(STATEMENT_OPEX)
    assert provenance["Payroll"] == ["Payroll - Office", "Payroll - Leasing", "Payroll - Maintenance"]


def test_repair_lines_with_utility_words_stay_in_repairs():
    opex_table, provenance = parse_t12_with_provenance(T12)
    by_name = {row["category_name"]: row for row in opex_table}

    assert by_name["Repairs & Maintenance"]["base_value"] == 195_000.0
    assert by_name["Repairs & Maintenance"]["recoverable_flag"] is False
    assert "Electricity" not in by_name
    assert "Water & Sewer" not in by_name
    assert "Repairs & Maintenance: Water Extraction" in provenance["Repairs & Maintenance"]


def test_statement_reconciles_opex_and_income():
    statement = parse_t12_statement(T12)
    recon = statement["reconciliation"]

    assert recon["status"] == "PASS"
    assert recon["opex"]["statement_total"] == STATEMENT_OPEX
    assert recon["income"]["statement_total"] == STATEMENT_INCOME
    kinds = {}
    for line in statement["income_lines"]:
        kinds[line["kind"]] = kinds.get(line["kind"], 0) + line["annual_amount"]
    assert kinds == {"rental": 5_355_000.0, "other": 264_000.0, "recovery": 762_000.0}


def test_other_income_is_ingested_as_revenue_programs():
    deal = _ingest()
    programs = {p["program_id"]: p for p in deal["revenue_programs"]}

    assert programs["t12_utility_reimbursements"]["price_value"] == pytest.approx(762_000 / 12)
    assert programs["t12_utility_reimbursements"]["program_type"] == "recovery"
    assert programs["t12_other_income"]["price_value"] == pytest.approx(264_000 / 12)
    assert {a["program_id"] for a in deal["program_adoption_curve"]} == set(programs)

    gate = deal["metadata"]["ingest_gate"]
    assert gate["status"] == "PASS"
    sources = {line["label"]: line["program_id"] for line in gate["t12_other_income"]}
    assert sources["RUBS - Water"] == "t12_utility_reimbursements"
    assert sources["Pet Rent"] == "t12_other_income"
    assert validate_deal(deal).status == "PASS"


def test_unreconciled_t12_blocks_validation_and_the_engine_run(tmp_path):
    """Two equal bare lines look like line + subtotal, so one is dropped. The gate must catch it."""

    def make_leasing_equal_office(rows):
        for row in rows:
            if row[0] == "Payroll - Leasing":
                row[1:13] = ["25000.00"] * 12
                row[13] = "300000.00"
            if row[0] == "Total Expenses":
                row[1:13] = [f"{257_250 + 2_500:.2f}"] * 12
                row[13] = f"{(257_250 + 2_500) * 12:.2f}"
        return rows

    t12 = _rewrite_rows(T12, tmp_path / "ambiguous.csv", make_leasing_equal_office)
    deal = _ingest(t12=t12)
    gate = deal["metadata"]["ingest_gate"]

    assert gate["status"] == "BLOCKED"
    assert [b["code"] for b in gate["blockers"]] == ["t12_opex_does_not_reconcile"]
    report = validate_deal(deal)
    assert report.status == "FAIL"
    assert any(i.code == "INGEST_BLOCKED" for i in report.issues)


def test_blocked_gate_stops_run_underwriting(minimal_deal_inputs):
    deal = copy.deepcopy(minimal_deal_inputs)
    deal["metadata"]["ingest_gate"] = {
        "status": "BLOCKED",
        "blockers": [{"code": "t12_opex_does_not_reconcile", "message": "synthetic"}],
    }
    with pytest.raises(ValueError, match="INGEST_BLOCKED"):
        run_underwriting(deal)


def test_bare_other_income_line_counts_toward_income(tmp_path):
    csv_path = tmp_path / "bare_other_income.csv"
    csv_path.write_text(
        "Category,M1,M2\n"
        "REVENUE,,\n"
        "Gross Potential Rent,1000,1000\n"
        "Other Income,100,100\n"
        "TOTAL OPERATING REVENUE,1100,1100\n"
        "6110 Manager Salary,100,100\n"
        "TOTAL OPERATING EXPENSES,100,100\n"
    )
    statement = parse_t12_statement(csv_path)

    assert statement["reconciliation"]["status"] == "PASS"
    assert [line["kind"] for line in statement["income_lines"]] == ["rental", "other"]


def test_operating_expense_total_preferred_over_total_expenses(tmp_path):
    csv_path = tmp_path / "non_operating_below_noi.csv"
    csv_path.write_text(
        "Category,M1,M2\n"
        "Rent,1000,1000\n"
        "Total Income,1000,1000\n"
        "EXPENSES,,\n"
        "Insurance,100,100\n"
        "TOTAL OPERATING EXPENSES,100,100\n"
        "NET OPERATING INCOME,900,900\n"
        "NON-OPERATING EXPENSES,,\n"
        "Interest Expense,50,50\n"
        "TOTAL EXPENSES,150,150\n"
    )
    recon = parse_t12_statement(csv_path)["reconciliation"]

    assert recon["opex"]["statement_total"] == 200.0
    assert recon["status"] == "PASS"


def test_policy_defaults_are_blockers():
    deal = _ingest(rent_growth_rate=None, collection_loss_rate=None)
    gate = deal["metadata"]["ingest_gate"]

    assert gate["status"] == "BLOCKED"
    assert [b["code"] for b in gate["blockers"]] == [
        "policy_default_rent_growth_rate",
        "policy_default_collection_loss_rate",
    ]
    assert deal["growth_assumptions"]["annual_growth_rate"] == 0.03
    assert validate_deal(deal).status == "FAIL"


def test_statement_without_totals_is_unverified_not_blocked():
    deal = _ingest(t12=FIXTURES / "sample_t12.csv")
    gate = deal["metadata"]["ingest_gate"]

    assert gate["status"] == "PASS"
    assert gate["t12_reconciliation"]["status"] == "UNVERIFIED"
    assert "t12_totals_not_found_reconciliation_unverified" in deal["metadata"]["intake_sanity_flags"]


def test_sidecar_workbook_scan_is_opt_in(tmp_path, monkeypatch):
    openpyxl = pytest.importorskip("openpyxl")
    rent_roll = tmp_path / "rent roll.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Unit", "Unit Type", "Sqft", "Status", "Monthly Rent", "Market Rent"])
    ws.append(["0101", "bc_Q1", 860, "Occupied", 1483, 1727])
    wb.save(rent_roll)
    wb.close()

    def _fail(*_args, **_kwargs):
        raise AssertionError("sibling workbooks must not be opened by default")

    monkeypatch.setattr(rent_roll_parser, "_load_sidecar_bed_bath_map", _fail)
    rent_roll_parser.parse_rent_roll(rent_roll)


def test_revenue_quality_bridge_replaces_t12_income_programs():
    deal = _ingest()
    bridge = {"lines": [{"line_item": "other_income", "house_credit": 120_000, "decision": "credit"}]}
    _apply_revenue_quality_bridge(deal, bridge)

    program_ids = {p["program_id"] for p in deal["revenue_programs"]}
    assert program_ids == {"rq_bridge_other_income"}
