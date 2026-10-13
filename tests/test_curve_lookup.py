"""UPL-96: ``util.CurveLookup`` answers exactly as the per-call segment scan it replaced.

The reference below is the scan ``revenue._find_curve_value`` and
``revenue_programs._find_curve_value`` ran on every lookup before the
lookup parsed each segment's period once. Every case compares the value,
or the exception type and message, month by month.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from engine.modules.revenue import _find_curve_value as revenue_find
from engine.modules.revenue_programs import _find_curve_value as programs_find
from engine.modules.util import CurveLookup, dec, find_curve_value, month_id, parse_month


def _reference(curve_rows, month, value_field):
    month = month_id(month)
    matches = [
        row
        for row in curve_rows
        if month_id(row["start_period"]) <= month <= month_id(row["end_period"])
    ]
    if not matches:
        raise KeyError(f"Missing curve value for month={month} field={value_field}")
    if len(matches) > 1:
        raise ValueError(
            f"Ambiguous curve lookup for month={month}: {len(matches)} overlapping segments. "
            f"Validator should have caught this; check augmented_curves invariants."
        )
    return dec(matches[0][value_field])


def _outcome(fn, *args):
    try:
        value = fn(*args)
    except Exception as exc:  # noqa: BLE001 - the exception is the outcome compared
        return ("raised", type(exc), str(exc))
    return ("value", type(value), value)


MONTHS = ["2025-12", "2026-01", "2026-01-01", " 2026-03 ", "2026-06", "2026-07", "2026-09", "2026-12", "2027-01"]

CASES = {
    "contiguous": [
        {"start_period": "2026-01", "end_period": "2026-06", "market_rent": 1000},
        {"start_period": "2026-07-01", "end_period": "2026-12", "market_rent": 1100.5},
    ],
    "gap_and_overlap": [
        {"start_period": "2026-01", "end_period": "2026-06", "market_rent": 1000},
        {"start_period": "2026-04", "end_period": "2026-07", "market_rent": 1050},
        {"start_period": "2026-12", "end_period": "2026-12", "market_rent": 1200},
    ],
    "empty": [],
    "missing_value_field": [
        {"start_period": "2026-01", "end_period": "2026-12"},
    ],
    # A later row whose end is missing is only read for months on or after its start.
    "open_end_after_start": [
        {"start_period": "2026-01", "end_period": "2026-06", "market_rent": 1000},
        {"start_period": "2026-07", "end_period": None, "market_rent": 1100},
    ],
    "missing_end_key": [
        {"start_period": "2026-01", "end_period": "2026-06", "market_rent": 1000},
        {"start_period": "2026-07", "market_rent": 1100},
    ],
    "bad_start": [
        {"start_period": "2026-01", "end_period": "2026-06", "market_rent": 1000},
        {"start_period": "2026-07-15", "end_period": "2026-12", "market_rent": 1100},
    ],
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_curve_lookup_matches_the_per_call_scan(case):
    rows = CASES[case]
    lookup = CurveLookup(rows, "market_rent")
    for _ in range(2):  # a second pass answers from the memo, or raises again
        for month in MONTHS:
            expected = _outcome(_reference, rows, month, "market_rent")
            assert _outcome(lookup, month) == expected, (case, month)
            for fn in (find_curve_value, revenue_find, programs_find):
                assert _outcome(fn, rows, month, "market_rent") == expected, (case, month, fn)


def test_a_bad_month_raises_every_time():
    lookup = CurveLookup(CASES["contiguous"], "market_rent")
    for _ in range(2):
        with pytest.raises(ValueError):
            lookup("2026-13")
        with pytest.raises(ValueError):
            month_id("2026-01-15")
        with pytest.raises(AttributeError):
            parse_month(None)


def test_lookup_values_are_decimals_from_the_row():
    lookup = CurveLookup(CASES["contiguous"], "market_rent")
    assert lookup("2026-08") == Decimal("1100.5")
    assert month_id("2026-08-01") == "2026-08" and month_id(" 2026-08 ") == "2026-08"
