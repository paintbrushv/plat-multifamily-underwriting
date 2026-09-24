"""Regression tests for RedIQ OpEx growth-rate extraction.

These tests use a lightweight worksheet fake instead of real Excel fixtures so they
exercise only ``engine.rediq_bridge.extract_opex``'s Input-sheet calibration logic.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from engine.rediq_bridge import extract_opex


@dataclass(frozen=True)
class _FakeCell:
    value: Any


class _FakeWorksheet:
    def __init__(self, values: dict[tuple[int, int], Any]) -> None:
        self._values = values

    def cell(self, row: int, column: int) -> _FakeCell:
        return _FakeCell(self._values.get((row, column)))

    def __getitem__(self, ref: str) -> _FakeCell:
        column_letter = ref[0]
        row = int(ref[1:])
        column = ord(column_letter.upper()) - ord("A") + 1
        return self.cell(row=row, column=column)


class _FakeWorkbook:
    def __init__(self, input_sheet: _FakeWorksheet) -> None:
        self._input_sheet = input_sheet

    def __getitem__(self, sheet_name: str) -> _FakeWorksheet:
        if sheet_name != "Input":
            raise KeyError(sheet_name)
        return self._input_sheet


def _extract_single_opex_category_with_general_inflation(growth_rate: float) -> dict[str, Any]:
    """Return one legitimate fixed-annual OpEx row calibrated from the general inflation row."""
    input_sheet = _FakeWorksheet(
        {
            (10, 2): "Insurance",  # B10: category name
            (10, 5): 85_000.0,  # E10: base amount
            (10, 6): "Total",  # F10: type indicator
            (10, 7): 85_000.0,  # G10: Year 1 amount
            (100, 7): growth_rate,  # G100: Year 1 general inflation calibration
        }
    )
    categories = extract_opex(
        _FakeWorkbook(input_sheet),
        layout={
            "opex_start": 10,
            "opex_end": 11,
            "inflation_row": 100,
            "re_tax_inflation_row": 101,
        },
    )
    assert len(categories) == 1
    return categories[0]


@pytest.mark.parametrize("growth_rate", [0.0, -0.015])
def test_extract_opex_preserves_non_positive_general_inflation_metadata(growth_rate: float) -> None:
    """A real OpEx row must retain explicit zero/negative RedIQ growth calibration metadata."""
    assert _extract_single_opex_category_with_general_inflation(growth_rate) == {
        "category_name": "Insurance",
        "calculation_type": "fixed_annual",
        "base_value": 85_000.0,
        "recoverable_flag": False,
        "growth_rate": growth_rate,
    }
