from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
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


def month_id(value: str) -> str:
    d = parse_month(value)
    return f"{d.year:04d}-{d.month:02d}"


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
