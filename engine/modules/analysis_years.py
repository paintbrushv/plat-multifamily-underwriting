from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List

from engine.modules.util import dec, round2


def aggregate_analysis_years(
    by_month: List[Dict[str, Any]],
    analysis_start_date: str,
) -> List[Dict[str, Any]]:
    """Re-aggregate monthly cashflow into 12-month analysis years."""
    start_token = analysis_start_date[:7]
    start_year = int(start_token[:4])
    start_month = int(start_token[5:7])

    numeric_fields = [
        "net_rent",
        "net_programs",
        "utility_recovery",
        "effective_gross_income",
        "total_opex",
        "net_operating_income",
        "replacement_reserves",
        "total_capex",
        "unleveraged_cash_flow",
        "debt_service",
        "leveraged_cash_flow",
        "gross_sale_price",
        "sale_costs",
        "loan_payoff",
        "net_sale_proceeds",
    ]

    years: Dict[int, Dict[str, Any]] = {}
    for month_data in by_month:
        month = str(month_data["month"])[:7]
        month_year = int(month[:4])
        month_num = int(month[5:7])
        months_from_start = (month_year - start_year) * 12 + (month_num - start_month)
        analysis_year_idx = months_from_start // 12 + 1
        if analysis_year_idx < 1:
            continue

        if analysis_year_idx not in years:
            years[analysis_year_idx] = {"year": str(analysis_year_idx), "months_in_year": 0}
            for field in numeric_fields:
                years[analysis_year_idx][field] = Decimal("0")

        bucket = years[analysis_year_idx]
        bucket["months_in_year"] += 1
        for field in numeric_fields:
            bucket[field] += dec(month_data.get(field, 0))

    result: List[Dict[str, Any]] = []
    for analysis_year_idx in sorted(years.keys()):
        bucket = years[analysis_year_idx]
        for field in numeric_fields:
            bucket[field] = round2(bucket[field])
        result.append(bucket)
    return result
