"""
Unit tests for the OpEx CF-Calcs fallback guard in engine/rediq_bridge.py.

Exercises _try_read_cf_calcs_opex and the row-claiming logic introduced
in commit that fixed the "phantom OpEx" bug where blank Input-sheet lines
(% over Historicals type, no cached values) would fall through to the
CF-Calcs last-resort path and clone aggregate row values — most visibly
"Other Property Taxes" duplicating "Real Estate Taxes" (both map to row 49).

No real .xlsm file is required; tests construct the minimum data structures
needed to exercise the guard paths directly.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from engine.rediq_bridge import (
    _CF_OPEX_ROWS,
    _CF_SHARED_ROWS,
    _try_read_cf_calcs_opex,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_wb(row_values: dict) -> MagicMock:
    """Build a minimal workbook mock where CF Calculations col E (column=5)
    returns the value specified in row_values for the given row number.
    Any unspecified cell returns 0.
    """
    wb = MagicMock()
    ws = MagicMock()
    wb.__getitem__ = lambda self, key: ws if key == "CF Calculations" else MagicMock()

    def _cell(row, column):
        cell = MagicMock()
        cell.value = row_values.get(row, 0) if column == 5 else 0
        return cell

    ws.cell = _cell
    return wb


# ---------------------------------------------------------------------------
# Structural invariants about _CF_OPEX_ROWS / _CF_SHARED_ROWS
# ---------------------------------------------------------------------------


def test_shared_rows_computed_correctly() -> None:
    """Rows that appear 2+ times in _CF_OPEX_ROWS must be in _CF_SHARED_ROWS."""
    from collections import Counter
    counts = Counter(_CF_OPEX_ROWS.values())
    expected_shared = frozenset(r for r, c in counts.items() if c > 1)
    assert _CF_SHARED_ROWS == expected_shared, (
        f"Mismatch: expected {expected_shared}, got {_CF_SHARED_ROWS}"
    )


def test_known_shared_rows_present() -> None:
    """Row 47 (utilities aggregate) and 49 (RE taxes) must be shared."""
    assert 47 in _CF_SHARED_ROWS
    assert 49 in _CF_SHARED_ROWS


def test_insurance_row_not_shared() -> None:
    """Row 48 (insurance) maps to exactly one category — not shared."""
    assert 48 not in _CF_SHARED_ROWS
    assert _CF_OPEX_ROWS.get("insurance") == 48


# ---------------------------------------------------------------------------
# Guard (a): shared row → fallback must skip regardless of claimed_rows state
# ---------------------------------------------------------------------------


def test_shared_row_category_blocked_no_claimed_rows() -> None:
    """'other property taxes' → row 49, which is shared → None even with data."""
    wb = _make_wb({49: 50_000.0})
    result = _try_read_cf_calcs_opex(wb, "other property taxes")
    assert result is None, (
        "Fallback must return None for a shared row to prevent RE-Tax duplication"
    )


def test_shared_row_category_blocked_with_claimed_rows() -> None:
    """Same assertion holds when claimed_rows is explicitly provided."""
    wb = _make_wb({49: 50_000.0})
    result = _try_read_cf_calcs_opex(wb, "other property taxes", claimed_rows=set())
    assert result is None


def test_utilities_aggregate_row_blocked() -> None:
    """'electricity' → row 47, which is shared → None."""
    wb = _make_wb({47: 120_000.0})
    result = _try_read_cf_calcs_opex(wb, "electricity")
    assert result is None


def test_fuel_row_blocked() -> None:
    """'fuel (gas & oil)' → row 47, shared → None."""
    wb = _make_wb({47: 30_000.0})
    result = _try_read_cf_calcs_opex(wb, "fuel (gas & oil)")
    assert result is None


def test_real_estate_taxes_row_blocked() -> None:
    """'real estate taxes' → row 49, shared → None (it would return the full
    RE tax total, which 'other property taxes' would then try to clone)."""
    wb = _make_wb({49: 200_000.0})
    result = _try_read_cf_calcs_opex(wb, "real estate taxes")
    assert result is None


# ---------------------------------------------------------------------------
# Guard (b): unique-row category → fallback succeeds when unclaimed
# ---------------------------------------------------------------------------


def test_unique_row_fallback_succeeds() -> None:
    """'insurance' → row 48 (unique) → fallback returns the CF value."""
    wb = _make_wb({48: 85_000.0})
    result = _try_read_cf_calcs_opex(wb, "insurance", claimed_rows=set())
    assert result == 85_000.0


def test_unique_row_fallback_returns_abs_value() -> None:
    """CF Calcs stores expenses as negatives in some templates; abs() applied."""
    wb = _make_wb({48: -85_000.0})
    result = _try_read_cf_calcs_opex(wb, "insurance", claimed_rows=set())
    assert result == 85_000.0


def test_unique_row_fallback_security() -> None:
    """'security' → row 41 (unique) → returns value."""
    wb = _make_wb({41: 12_000.0})
    result = _try_read_cf_calcs_opex(wb, "security", claimed_rows=set())
    assert result == 12_000.0


# ---------------------------------------------------------------------------
# Guard (b continued): unique row already claimed → fallback blocked
# ---------------------------------------------------------------------------


def test_unique_row_already_claimed_blocked() -> None:
    """If row 48 was already claimed by a prior extraction, the fallback must
    return None to prevent assigning the same value to a second category."""
    wb = _make_wb({48: 85_000.0})
    claimed = {48}
    result = _try_read_cf_calcs_opex(wb, "insurance", claimed_rows=claimed)
    assert result is None


def test_unique_row_claimed_other_row_not_blocked() -> None:
    """Claiming row 48 does not block a different unique row (41)."""
    wb = _make_wb({41: 12_000.0})
    claimed = {48}  # only row 48 is claimed
    result = _try_read_cf_calcs_opex(wb, "security", claimed_rows=claimed)
    assert result == 12_000.0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_unknown_category_returns_none() -> None:
    """A category name not in _CF_OPEX_ROWS returns None cleanly."""
    wb = _make_wb({})
    result = _try_read_cf_calcs_opex(wb, "some_unknown_expense_category")
    assert result is None


def test_zero_cf_value_returns_none() -> None:
    """A zero value in the CF row is treated as absent → None."""
    wb = _make_wb({48: 0.0})
    result = _try_read_cf_calcs_opex(wb, "insurance", claimed_rows=set())
    assert result is None


def test_wb_missing_sheet_returns_none() -> None:
    """If CF Calculations sheet is missing, exception is swallowed → None."""
    wb = MagicMock()
    wb.__getitem__ = MagicMock(side_effect=KeyError("CF Calculations"))
    result = _try_read_cf_calcs_opex(wb, "insurance", claimed_rows=set())
    assert result is None
