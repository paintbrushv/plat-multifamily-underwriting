"""Regression tests for RedIQ Input-sheet writer cell placement."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

import engine.rediq_output as rediq_output


@dataclass
class _Cell:
    value: Any = None


class _FakeInputSheet:
    """Tiny worksheet surface used by rediq_output._detect_input_layout."""

    def __init__(self, values: dict[tuple[int, int], Any]):
        self._values = values

    def __getitem__(self, ref: str) -> _Cell:
        letters = "".join(ch for ch in ref if ch.isalpha()).upper()
        digits = "".join(ch for ch in ref if ch.isdigit())
        col = 0
        for ch in letters:
            col = col * 26 + ord(ch) - ord("A") + 1
        return self.cell(row=int(digits), column=col)

    def cell(self, row: int, column: int) -> _Cell:
        return _Cell(self._values.get((row, column)))


class _FakeWorkbook:
    def __init__(self, sheet: _FakeInputSheet):
        self._sheet = sheet
        self.closed = False

    def __getitem__(self, name: str) -> _FakeInputSheet:
        if name != "Input":
            raise KeyError(name)
        return self._sheet

    def close(self) -> None:
        self.closed = True


class _RecordingWriter:
    def __init__(self):
        self.values: dict[tuple[str, int, int], Any] = {}

    def set_cell_value(self, sheet: str, row: int, col: int, value: Any) -> None:
        self.values[(sheet, row, col)] = value


@pytest.fixture
def fake_input_workbook(monkeypatch):
    rent_growth_header_row = 70
    values = {
        (50, 1): "Floor Plan Mix",
        (51, 2): "Existing 1BR row",
        (55, 2): "Total/Average",
        (rent_growth_header_row, 2): "MARKET RENT GROWTH",
        (110, 2): "RENTAL LOSS FACTORS",
        (140, 2): "ANNUAL OPERATING EXPENSES",
        (165, 2): "General Inflation",
        (185, 2): "REPLACEMENT RESERVES",
        (205, 2): "PROPERTY-WIDE CAPITAL",
        (225, 2): "DEBT ASSUMPTIONS",
        (260, 2): "PARTNERSHIP ASSUMPTIONS",
    }
    workbook = _FakeWorkbook(_FakeInputSheet(values))

    monkeypatch.setattr(
        rediq_output.openpyxl,
        "load_workbook",
        lambda *args, **kwargs: workbook,
    )

    return workbook, rent_growth_header_row + 3


def test_input_sheet_writes_first_cohort_annual_rent_growth_to_consecutive_year_cells(
    fake_input_workbook,
):
    """Year-over-year rent growth belongs in adjacent annual cells, not alternate columns."""
    _workbook, rent_growth_row = fake_input_workbook
    writer = _RecordingWriter()
    inputs = {
        "metadata": {"deal_id": "Stride regression"},
        "unit_cohorts": [
            {
                "cohort_id": "one_bed",
                "unit_type": "1BR",
                "unit_count": 12,
                "sqft": 700,
                "initial_inplace_rent": 950,
            }
        ],
        "market_rent_curve": [
            {"cohort_id": "one_bed", "year": 1, "market_rent": 1000},
            {"cohort_id": "one_bed", "year": 2, "market_rent": 1050},
            {"cohort_id": "one_bed", "year": 3, "market_rent": 1102.5},
        ],
    }

    rediq_output.write_input_sheet(
        writer,
        results={},
        inputs=inputs,
        template_path="fake-rediq-template.xlsm",
    )

    assert writer.values[("Input", rent_growth_row, 7)] == pytest.approx(0.05)
    assert writer.values[("Input", rent_growth_row, 8)] == pytest.approx(0.05)
    assert ("Input", rent_growth_row, 9) not in writer.values
