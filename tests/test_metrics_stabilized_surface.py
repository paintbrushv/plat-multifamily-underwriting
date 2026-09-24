"""Runtime tests for stabilized CoC and stabilized YoC surfaces in compute_metrics."""

from engine.modules.metrics import compute_metrics
from engine.modules.time_grid import TimeGrid


def _metrics_with_distinct_equity_basis():
    time_grid = TimeGrid.build("2026-01", "2026-12")
    cashflow_by_month = [
        {
            "month": f"2026-{month:02d}",
            "net_operating_income": 10_000,
            "unleveraged_cash_flow": 8_000,
            "leveraged_cash_flow": 3_000,
            "total_capex": 0,
        }
        for month in range(1, 13)
    ]
    cashflow_by_year = [
        {
            "year": "2026",
            "net_operating_income": 120_000,
            "debt_service": 60_000,
            "unleveraged_cash_flow": 96_000,
            "leveraged_cash_flow": 36_000,
            "total_capex": 0,
        }
    ]
    debt_by_month = [
        {"month": f"2026-{month:02d}", "ending_balance": 1_000_000}
        for month in range(1, 13)
    ]

    return compute_metrics(
        time_grid,
        cashflow_by_month,
        cashflow_by_year,
        debt_by_month,
        purchase_assumptions={
            "purchase_price": 1_500_000,
            "closing_costs": 50_000,
            "equity_contribution": 500_000,
            "total_equity_basis": 600_000,
        },
    )


def test_stabilized_keys_appear_in_compute_metrics_return_when_invoked():
    result = _metrics_with_distinct_equity_basis()

    assert result["cash_on_cash"]["stabilized"] is not None
    assert result["cash_on_cash"]["stabilized_year"] == 1
    assert result["noi"]["stabilized_year_noi"] == 120_000.0
    assert result["noi"]["stabilized_year"] == 1


def test_coc_surfaces_use_total_equity_basis_not_equity_contribution():
    result = _metrics_with_distinct_equity_basis()

    # 36,000 / 600,000 total equity basis = 6%; using only the 500,000
    # equity contribution would produce 7.2% and overstate cash-on-cash.
    assert result["cash_on_cash"]["by_year"][0]["yield"] == 0.06
    assert result["cash_on_cash"]["average"] == 0.06
    assert result["cash_on_cash"]["stabilized"] == 0.06
