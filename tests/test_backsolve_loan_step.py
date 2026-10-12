"""The price search tolerates the CoC blip from the $1,000 loan rounding step.

The agency loan is rounded to the nearest $1,000, so as price rises the loan
occasionally steps up $1,000 and equity falls $1,000: cash-on-cash rises by
about |CoC| x 1,000 / equity. That bounded blip is not a model reversal and
must not refuse a solvable deal. Synthetic deals only (tests/fixtures/deals).
"""

from __future__ import annotations

import copy
from decimal import Decimal

from engine import backsolve
from tests import deal_library as dl

BENCHMARK = {"rate": "0.04", "as_of": "2026-10-01", "source": "synthetic:deal-library"}


def test_tolerance_admits_one_loan_step_blip_and_rejects_a_real_reversal():
    from engine.modules.debt import LOAN_ROUNDING_STEP

    coc, equity = Decimal("0.10"), Decimal("8850000")
    tolerance = backsolve._monotone_tolerance(coc, equity)
    one_step_blip = coc * LOAN_ROUNDING_STEP / equity
    assert one_step_blip > Decimal("0.000001")  # above the old fixed tolerance
    assert one_step_blip <= tolerance
    assert Decimal("0.001") > tolerance
    assert backsolve._monotone_tolerance(Decimal("0"), equity) == Decimal("0.000001")
    assert backsolve._monotone_tolerance(coc, Decimal("0")) == Decimal("0.000001")


def test_bisection_across_a_loan_rounding_step_still_converges():
    """Deal a at a 10% target over $15M-$40M bisects onto a loan step: the
    upper bound's loan steps up $1,000 and its CoC sits ~1e-6 above the next
    midpoint's. The solve must converge on the highest feasible cent."""
    canonical = dl.ingest(dl.load_deal("a_stabilized_1990s"))
    result = backsolve.backsolve_price(
        copy.deepcopy(canonical), target_coc="0.10",
        policy={"version": "plat.backsolve-policy/1", "strategy": "cashflow"},
        benchmark=BENCHMARK, min_price="15000000.00", max_price="40000000.00",
    )
    assert result["status"] == "converged"
    summary = result["summary"]
    boundary = summary["maximum_feasible_boundary"]
    assert summary["cash_on_cash_year_1_persisted"] >= 0.10
    assert boundary["next_infeasible_coc"] < 0.10
    assert round(boundary["next_infeasible_price"] - boundary["highest_feasible_price"], 2) == 0.01
    assert 15_000_000 < summary["solved_purchase_price"] < 40_000_000
