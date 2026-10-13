from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
from typing import List, Optional


def dec(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def round2(value: Decimal) -> float:
    q = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(q)


def round4(value: Decimal) -> float:
    """4 decimal places for rates/percentages (e.g. 0.0525 = 5.25%)."""
    q = value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return float(q)


def parse_month(value: str) -> date:
    # Months are parsed millions of times per run (every curve lookup), from a
    # handful of distinct strings: memoize str input. ``date`` is immutable, and
    # a bad value still raises on every call (lru_cache does not cache errors).
    if type(value) is str:
        return _parse_month_cached(value)
    return _parse_month(value)


def _parse_month(value: str) -> date:
    value = value.strip()
    if len(value) == 7 and value[4] == "-":
        year = int(value[0:4])
        month = int(value[5:7])
        return date(year, month, 1)

    parts = value.split("-")
    if len(parts) != 3:
        raise ValueError(f"Invalid month/date value: {value}")
    year, month, day = (int(parts[0]), int(parts[1]), int(parts[2]))
    if day != 1:
        raise ValueError(f"Date must be first of month: {value}")
    return date(year, month, 1)


_parse_month_cached = lru_cache(maxsize=4096)(_parse_month)


def month_id(value: str) -> str:
    """``YYYY-MM`` for a month or first-of-month date string (memoized for str input)."""
    if type(value) is str:
        return _month_id_cached(value)
    return _month_id(value)


def _month_id(value: str) -> str:
    d = _parse_month(value)
    return f"{d.year:04d}-{d.month:02d}"


_month_id_cached = lru_cache(maxsize=4096)(_month_id)


class CurveLookup:
    """One curve's ``start_period``/``end_period`` segments, looked up by month.

    Equivalent to scanning ``rows`` for the one segment covering a month on
    every call (a missing month raises ``KeyError``, overlapping segments raise
    ``ValueError``), but each row's period is parsed once, on the first lookup,
    and each month's value is computed once. Errors are never cached: a lookup
    that raised raises again. A curve with a row whose period does not parse
    is scanned row by row on every lookup, exactly as before, so it raises the
    same error at the same lookup. ``rows`` must not change while in use.
    """

    __slots__ = ("_rows", "_value_field", "_segments", "_values")

    def __init__(self, rows: List[dict], value_field: str):
        self._rows = rows
        self._value_field = value_field
        self._segments: Optional[List[tuple]] = None  # None: not parsed yet; False: unparseable
        self._values: dict = {}

    def __call__(self, month: str) -> Decimal:
        month = month_id(month)
        values = self._values
        if month in values:
            return values[month]
        segments = self._segments
        if segments is None:
            try:
                segments = [(month_id(row["start_period"]), month_id(row["end_period"]), row) for row in self._rows]
            except Exception:
                segments = False
            self._segments = segments
        if segments is False:
            matches = [
                row
                for row in self._rows
                if month_id(row["start_period"]) <= month <= month_id(row["end_period"])
            ]
        else:
            matches = [row for start, end, row in segments if start <= month <= end]
        value_field = self._value_field
        if not matches:
            raise KeyError(f"Missing curve value for month={month} field={value_field}")
        if len(matches) > 1:
            raise ValueError(
                f"Ambiguous curve lookup for month={month}: {len(matches)} overlapping segments. "
                f"Validator should have caught this; check augmented_curves invariants."
            )
        value = values[month] = dec(matches[0][value_field])
        return value


def find_curve_value(curve_rows: List[dict], month: str, value_field: str) -> Decimal:
    """The value of the one segment of ``curve_rows`` covering ``month`` (see ``CurveLookup``)."""
    return CurveLookup(curve_rows, value_field)(month)


def get_total_units(unit_cohorts: List[dict]) -> Decimal:
    """Sum total units across all cohorts."""
    return sum(dec(c["unit_count"]) for c in unit_cohorts)


def npv(cash_flows: List[float], rate: float) -> float:
    """Calculate Net Present Value at given periodic rate."""
    return sum(cf / (1 + rate) ** i for i, cf in enumerate(cash_flows))


def calculate_irr(cash_flows: List[float], max_iterations: int = 100) -> Optional[float]:
    """Calculate IRR using bisection method for reliability.

    Args:
        cash_flows: List where [0] is negative (investment), rest are returns
        max_iterations: Maximum number of iterations

    Returns:
        IRR as decimal (0.15 = 15%) or None if cannot converge
    """
    if not cash_flows or len(cash_flows) < 2:
        return None

    has_negative = any(cf < 0 for cf in cash_flows)
    has_positive = any(cf > 0 for cf in cash_flows)
    if not (has_negative and has_positive):
        return None

    low = -0.99
    high = 1.0

    npv_low = npv(cash_flows, low)
    npv_high = npv(cash_flows, high)

    if npv_low * npv_high > 0:
        for test_high in [2.0, 5.0, 10.0]:
            npv_test = npv(cash_flows, test_high)
            if npv_low * npv_test < 0:
                high = test_high
                npv_high = npv_test
                break
        else:
            return None

    for _ in range(max_iterations):
        mid = (low + high) / 2
        npv_mid = npv(cash_flows, mid)

        if abs(npv_mid) < 1e-6:
            return mid

        if npv_low * npv_mid < 0:
            high = mid
            npv_high = npv_mid
        else:
            low = mid
            npv_low = npv_mid

        if abs(high - low) < 1e-8:
            return mid

    return (low + high) / 2
