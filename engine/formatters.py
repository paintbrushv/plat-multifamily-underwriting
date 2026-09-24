"""
Shared Formatting Helpers
==========================
Canonical formatting functions used across PDF, PPTX, XLSX, and CLI outputs.

Extracted from duplicated definitions in pdf_onepager, portfolio_pdf,
pptx_generator, portfolio_xlsx, and portfolio_report.
"""
from __future__ import annotations

from typing import Optional


def fmt_pct(val: Optional[float], decimals: int = 2) -> str:
    """Format a decimal as a percentage string.

    Args:
        val: Decimal value (e.g. 0.085 → '8.50%')
        decimals: Number of decimal places (default 2)

    Returns:
        Formatted string or em-dash for None
    """
    if val is None:
        return "—"
    return f"{val * 100:.{decimals}f}%"


def fmt_currency(val: Optional[float], decimals: int = 0, abbrev: bool = False) -> str:
    """Format a number as currency.

    Args:
        val: Dollar amount
        decimals: Decimal places (default 0)
        abbrev: If True, abbreviate large values (e.g. $1.2M, $450K)

    Returns:
        Formatted string or em-dash for None
    """
    if val is None:
        return "—"
    if abbrev and abs(val) >= 1_000_000:
        return f"${val / 1_000_000:,.1f}M"
    if abbrev and abs(val) >= 1_000:
        return f"${val / 1_000:,.0f}K"
    return f"${val:,.{decimals}f}"


def fmt_multiple(val: Optional[float]) -> str:
    """Format a value as an equity multiple (e.g. 1.85x).

    Returns:
        Formatted string or em-dash for None
    """
    if val is None:
        return "—"
    return f"{val:.2f}x"


def fmt_number(val: Optional[float]) -> str:
    """Format a number with comma separators.

    Returns:
        Formatted string or em-dash for None
    """
    if val is None:
        return "—"
    return f"{val:,.0f}"
