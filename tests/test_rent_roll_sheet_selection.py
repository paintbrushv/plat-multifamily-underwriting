"""Regression: rent-roll sheet selection on Entrata-style two-tab exports.

Entrata "Rent Roll 4.0" exports carry the unit detail on a tab named after
the property and a short "Report Parameters" tab. `_select_sheet` used to treat
any name containing "report" as a detail candidate, so it picked the
parameters tab and ingested zero units.

Fixtures are synthetic: the property name and every row are invented.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from engine.ingest.rent_roll_parser import parse_rent_roll

openpyxl = pytest.importorskip("openpyxl")

UNIT_HEADER = [
    "Bldg-Unit", "Unit", "Floorplan", "Sq Ft", "Status", "Resident",
    "Market Rent", "Rent", "Lease Start", "Lease End",
]


def _unit_rows() -> list[list]:
    rows = []
    plans = [("A1", 1, 1, 700, 1250), ("B1", 2, 1, 950, 1525), ("B2", 2, 2, 1050, 1650)]
    for n in range(30):
        plan, _beds, _baths, sqft, market = plans[n % 3]
        occupied = n % 10 != 0
        rows.append([
            f"1-{100 + n}", str(100 + n), plan, sqft,
            "Occupied" if occupied else "Vacant",
            f"Resident {n}" if occupied else "",
            market, market - 25 if occupied else 0, "2026-01-01", "2026-12-31",
        ])
    return rows


def _entrata_workbook(path: Path, *, parameters_first: bool = False) -> Path:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    def _detail(ws):
        ws.append(["Maplecrest Commons"])
        ws.append(["Rent Roll"])
        ws.append(["As of 08/05/2026"])
        ws.append([])
        ws.append(UNIT_HEADER)
        for row in _unit_rows():
            ws.append(row)
        ws.append(["Total", None, None, None, None, None, None, None])

    def _parameters(ws):
        ws.append(["Report Parameters"])
        for key, value in [
            ("Report", "Rent Roll 4.0"), ("Property", "Maplecrest Commons"),
            ("As Of Date", "08/05/2026"), ("Unit Status", "All"),
            ("Include Future Residents", "No"), ("Group By", "None"),
        ] * 3:
            ws.append([key, value])

    if parameters_first:
        _parameters(wb.create_sheet("Report Parameters"))
        _detail(wb.create_sheet("Maplecrest Commons"))
    else:
        _detail(wb.create_sheet("Maplecrest Commons"))
        _parameters(wb.create_sheet("Report Parameters"))
    wb.save(path)
    wb.close()
    return path


@pytest.mark.parametrize("parameters_first", [False, True])
def test_entrata_export_ingests_unit_detail_not_report_parameters(tmp_path, parameters_first):
    path = _entrata_workbook(tmp_path / "rent_roll.xlsx", parameters_first=parameters_first)

    result = parse_rent_roll(path)

    assert result["total_units"] == 30
    assert {c["unit_type"] for c in result["unit_cohorts"]} == {"A1", "B1", "B2"}
    assert result["physical_vacancy_rate"] == pytest.approx(0.10)


def test_ambiguous_name_hints_prefer_the_best_header(tmp_path):
    path = tmp_path / "two_detail_named_tabs.xlsx"
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    notes = wb.create_sheet("Unit Details Notes")
    for n in range(60):
        notes.append([f"note {n}", "see leasing office"])
    detail = wb.create_sheet("Rent Roll Detail")
    detail.append(UNIT_HEADER)
    for row in _unit_rows():
        detail.append(row)
    wb.save(path)
    wb.close()

    assert parse_rent_roll(path)["total_units"] == 30


def test_parameters_only_workbook_is_rejected(tmp_path):
    path = tmp_path / "params_only.xlsx"
    wb = openpyxl.Workbook()
    wb.active.title = "Report Parameters"
    wb.active.append(["Report", "Rent Roll 4.0"])
    wb.create_sheet("Filter Criteria").append(["Status", "All"])
    wb.save(path)
    wb.close()

    with pytest.raises(ValueError, match="no unit-detail sheet"):
        parse_rent_roll(path)


def test_legacy_xls_path_skips_report_parameters(monkeypatch, tmp_path):
    pd = pytest.importorskip("pandas")
    path = tmp_path / "legacy.xls"
    path.write_bytes(b"not-a-zip")

    def fake_read_excel(*_args, **_kwargs):
        return {
            "Maplecrest Commons": pd.DataFrame([UNIT_HEADER, *_unit_rows()]),
            "Report Parameters": pd.DataFrame([["Report", "Rent Roll 4.0"]] * 24),
        }

    monkeypatch.setattr(pd, "read_excel", fake_read_excel)

    assert parse_rent_roll(path)["total_units"] == 30
