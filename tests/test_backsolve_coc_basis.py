"""The backsolve cash-on-cash basis (UPL-62 decision b). Synthetic inputs only.

Value-add pricing targets the post-debt cash-on-cash of the first stabilized
analysis year; stabilized (cashflow) pricing keeps the Year-1 basis. The basis
is a policy field (``coc_basis``) that defaults by strategy.
"""
from __future__ import annotations

import json
from decimal import Decimal
from functools import lru_cache

import pytest

from engine.backsolve import backsolve_price
from engine.backsolve_policy import BacksolveInputError, resolve_policy
from engine.modules.metrics import (
    compute_cash_on_cash_for_year,
    first_full_analysis_year_after,
    stabilized_analysis_year,
)
from tests import deal_library as dl

BENCHMARK = {"rate": "0.04", "as_of": "2026-10-01", "source": "synthetic:test"}


@pytest.mark.parametrize("strategy, expected", [("cashflow", "year_1"), ("value_add", "stabilized_year")])
def test_coc_basis_defaults_by_strategy(strategy, expected):
    values = resolve_policy({"version": "plat.backsolve-policy/1", "strategy": strategy, "exit_cap_rate": "0.06"}, {})
    assert values["coc_basis"] == expected


def test_coc_basis_is_overridable_and_validated():
    policy = {"version": "plat.backsolve-policy/1", "strategy": "value_add", "exit_cap_rate": "0.06"}
    assert resolve_policy({**policy, "coc_basis": "year_1"}, {})["coc_basis"] == "year_1"
    with pytest.raises(BacksolveInputError, match="coc_basis"):
        resolve_policy({**policy, "coc_basis": "year_3"}, {})


@pytest.mark.parametrize("month, expected", [
    ("2026-11", 2), ("2027-10", 2), ("2027-11", 3), ("2028-09", 3), ("2028-10", 3), ("2028-11", 4),
])
def test_first_full_analysis_year_after_a_month(month, expected):
    # Analysis years start in November: year 3 is 2028-11 to 2029-10.
    assert first_full_analysis_year_after(month, "2026-11") == expected


def test_stabilized_year_without_renovations_uses_find_stabilized_year():
    years = [{"months_in_year": 12, "total_capex": 0} for _ in range(5)]
    # Vacancy settles to the 5% steady state in year 3 (within the 0.5pt tolerance).
    vacancy = [Decimal("0.09"), Decimal("0.07"), Decimal("0.052"), Decimal("0.05"), Decimal("0.05")]
    result = stabilized_analysis_year(years, analysis_start="2026-01", total_basis=Decimal("10000000"),
                                      vacancy_by_year=vacancy, steady_state_vacancy=Decimal("0.05"))
    assert (result["year"], result["rule"]) == (3, "find_stabilized_year")
    # Operating CapEx of 5% of the basis or more also fails a year; with no
    # qualifying year in the first three, find_stabilized_year falls back to year 2.
    years[2]["total_capex"] = 600_000
    assert stabilized_analysis_year(years, analysis_start="2026-01", total_basis=Decimal("10000000"),
                                    vacancy_by_year=vacancy, steady_state_vacancy=Decimal("0.05"))["year"] == 2
    years[2]["total_capex"] = 0
    vacancy[0] = Decimal("0.05")
    assert stabilized_analysis_year(years, analysis_start="2026-01", total_basis=Decimal("10000000"),
                                    vacancy_by_year=vacancy, steady_state_vacancy=Decimal("0.05"))["year"] == 1


def test_cash_on_cash_for_a_year_nets_fund_expenses():
    years = [{"leveraged_cash_flow": 100_000}, {"leveraged_cash_flow": 250_000, "net_operating_income": 600_000}]
    fund = [{}, {"asset_management_fee": 30_000, "partnership_expenses": 20_000}]
    out = compute_cash_on_cash_for_year(years, 2, Decimal("2000000"), fund)
    assert out["free_cf"] == 200_000.0
    assert out["cash_on_cash"] == Decimal("0.1")


@lru_cache(maxsize=None)
def _deal_c() -> str:
    return json.dumps(dl.ingest(dl.load_deal("c_value_add_1970s_reno")))


def test_a_stabilized_year_at_or_after_the_exit_refuses():
    """Renovations that finish in the exit year leave no stabilized operating year."""
    deal = json.loads(_deal_c())
    for program in deal["renovation_programs"]:
        program["monthly_pace"] = 1  # 50 units at one a month from 2027-01: done 2031-02
    with pytest.raises(BacksolveInputError, match="not a full year before the exit"):
        backsolve_price(deal, target_coc="0.06",
                        policy={"version": "plat.backsolve-policy/1", "strategy": "value_add"},
                        benchmark=BENCHMARK, min_price="1000000.00", max_price="100000000.00")
