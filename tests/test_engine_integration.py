from __future__ import annotations

import copy
from decimal import Decimal

import pytest

from engine.engine import run_underwriting
from engine.modules.util import round2, round4


def test_fund_aware_coc_aligns_with_analysis_year_buckets(minimal_deal_inputs):
    inputs = copy.deepcopy(minimal_deal_inputs)
    result = run_underwriting(inputs)

    monthly = result["cashflow"]["by_month"][:12]
    fund_year_1 = result["fund_waterfall"]["by_year"][0]
    equity_basis = inputs["purchase_assumptions"]["total_equity_basis"]

    expected_free_cf = (
        sum(row.get("leveraged_cash_flow", 0) for row in monthly)
        - fund_year_1["asset_management_fee"]
        - fund_year_1["partnership_expenses"]
    )
    expected_coc_exact = Decimal(str(expected_free_cf)) / Decimal(str(equity_basis))
    expected_coc = round(float(expected_coc_exact), 4)

    stub_year = result["cashflow"]["by_year"][0]
    stub_free_cf = (
        stub_year["leveraged_cash_flow"]
        - fund_year_1["asset_management_fee"]
        - fund_year_1["partnership_expenses"]
    )
    stub_coc = round(float(Decimal(str(stub_free_cf)) / Decimal(str(equity_basis))), 4)

    actual_coc = result["metrics"]["coc"]["cash_on_cash_year_1"]
    assert actual_coc == pytest.approx(expected_coc, abs=1e-4)
    assert result["metrics"]["coc"]["cash_on_cash_year_1_exact"] == pytest.approx(
        float(expected_coc_exact),
        abs=1e-12,
    )
    assert actual_coc != pytest.approx(stub_coc, abs=1e-4)


def test_going_in_cap_aligns_with_analysis_year_buckets(minimal_deal_inputs):
    inputs = copy.deepcopy(minimal_deal_inputs)
    result = run_underwriting(inputs)

    monthly = result["cashflow"]["by_month"][:12]
    expected_noi = sum(row.get("net_operating_income", 0) for row in monthly)
    purchase_price = inputs["purchase_assumptions"]["purchase_price"]
    # Engine uses round4 for cap rates / yield percentages; match that precision.
    expected_cap = round4(Decimal(str(expected_noi)) / Decimal(str(purchase_price)))

    stub_year = result["cashflow"]["by_year"][0]
    stub_cap = round4(
        Decimal(str(stub_year["net_operating_income"])) / Decimal(str(purchase_price))
    )

    actual_cap = result["metrics"]["yields"]["going_in_cap_rate"]
    assert actual_cap == pytest.approx(expected_cap, abs=1e-4)
    assert actual_cap != pytest.approx(stub_cap, abs=1e-4)
