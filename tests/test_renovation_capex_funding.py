"""renovation_capex_funding: a renovation budget funded with equity at close (UPL-62).

With ``renovation_capex_funding: "equity_at_close"`` the renovation budget
(renovation program and unit renovation costs) stays out of operating cash
flow and belongs in the Day-0 equity outlay. The default funds it out of
operating cash flow, as before. Synthetic library deal c only.
"""
from __future__ import annotations

import copy
import json
from functools import lru_cache

import pytest

from engine.engine import run_underwriting
from engine.validator import validate_schema
from tests import deal_library as dl

SLUG = "c_value_add_1970s_reno"


@lru_cache(maxsize=None)
def _deal_json() -> str:
    return json.dumps(dl.ingest(dl.load_deal(SLUG)))


def _deal(**patch):
    deal = json.loads(_deal_json())
    deal.update(patch)
    return deal


def _sum(rows, field):
    return round(sum(row.get(field, 0) or 0 for row in rows), 2)


def test_default_spends_the_renovation_budget_out_of_operating_cash_flow():
    results = run_underwriting(_deal())
    budget = results["renovations"]["summary"]["total_renovation_cost"]
    assert budget == 50 * 11_500 + 40 * 13_500
    assert _sum(results["cashflow"]["by_month"], "total_capex") == pytest.approx(budget)
    assert "capex_funding" not in results
    assert not any("equity_funded_capex" in row for row in results["cashflow"]["by_month"])


def test_equity_at_close_keeps_the_budget_out_of_cash_flow():
    base = _deal()
    funded = _deal(renovation_capex_funding="equity_at_close")
    assert validate_schema(funded) == []
    spent, kept = run_underwriting(copy.deepcopy(base)), run_underwriting(copy.deepcopy(funded))
    budget = spent["renovations"]["summary"]["total_renovation_cost"]

    assert kept["capex_funding"]["renovation_capex_funding"] == "equity_at_close"
    assert kept["capex_funding"]["equity_funded_renovation_budget"] == pytest.approx(budget)
    assert _sum(kept["cashflow"]["by_month"], "total_capex") == 0
    assert _sum(kept["cashflow"]["by_month"], "equity_funded_capex") == pytest.approx(budget)
    # Operations are unchanged; only the funded CapEx leaves the cash flow.
    for a, b in zip(spent["cashflow"]["by_month"], kept["cashflow"]["by_month"]):
        assert a["net_operating_income"] == b["net_operating_income"]
        assert b["leveraged_cash_flow"] == pytest.approx(a["leveraged_cash_flow"] + a["total_capex"], abs=0.01)
    assert run_underwriting(copy.deepcopy(funded)) == kept  # deterministic


def test_day0_bases_include_the_funded_budget():
    """IRR coherence: the Day-0 equity and unlevered outflows include the budget the
    cash flows no longer spend. A stated basis is taken as given (it must include
    the budget); a basis left out defaults to the usual basis plus the budget."""
    deal = _deal(renovation_capex_funding="equity_at_close")
    purchase = deal["purchase_assumptions"]
    purchase.pop("total_equity_basis", None)
    defaulted = run_underwriting(copy.deepcopy(deal))
    budget = defaulted["capex_funding"]["equity_funded_renovation_budget"]

    stated = copy.deepcopy(deal)
    stated["purchase_assumptions"].update(
        total_equity_basis=purchase["equity_contribution"] + budget,
        total_unlevered_basis=purchase["purchase_price"] + purchase["closing_costs"] + budget,
    )
    explicit = run_underwriting(stated)
    assert explicit["metrics"]["irr"] == defaulted["metrics"]["irr"]
    assert explicit["metrics"]["equity_multiple"] == defaulted["metrics"]["equity_multiple"]
    assert explicit["metrics"]["coc"]["components"]["equity_basis"] == pytest.approx(
        purchase["equity_contribution"] + budget)

    # Leaving the budget out of a stated basis overstates the levered IRR.
    understated = copy.deepcopy(deal)
    understated["purchase_assumptions"]["total_equity_basis"] = purchase["equity_contribution"]
    assert run_underwriting(understated)["metrics"]["irr"]["levered_irr"] > explicit["metrics"]["irr"]["levered_irr"]


def test_unknown_funding_mode_is_rejected():
    deal = _deal(renovation_capex_funding="seller_credit")
    assert validate_schema(deal)
    with pytest.raises(Exception, match="renovation_capex_funding|seller_credit"):
        run_underwriting(deal)
