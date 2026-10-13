"""The exit is valued on forward NOI (UPL-95, ADR 0003 decision n).

Market convention values a sale on the buyer's year: the projected NOI for the
12 months after the exit month. The analysis grid ends at the exit, so the
engine projects one more year for NOI only, with the conventions it applies
in the hold (the rent growth vector, the curves at their terminal values, opex
growth including real estate tax growth after the reassessment, the
management fee on EGI). Nothing past the exit enters the hold's cash flows,
DSCR, cash-on-cash or debt; only the reversion moves.
``exit_assumptions.noi_window: trailing`` opts into the old basis, the 12
months ending at the exit month. Synthetic deals only.
"""

from __future__ import annotations

import copy
import json
from functools import lru_cache

import pytest

from engine.assumptions import AssumptionOverrideError, resolve_assumptions
from engine.assumptions.rationale import rationale_for
from engine.backsolve import backsolve_price
from engine.engine import run_underwriting
from engine.modules.debt import size_refi_loan
from engine.modules.metrics import compute_exit_proceeds
from engine.modules.time_grid import TimeGrid
from engine.modules.util import dec
from tests import deal_library as dl

# Reversion fields the exit writes into the exit month (and year) of the cash flow.
REVERSION_FIELDS = {"gross_sale_price", "sale_costs", "loan_payoff", "net_sale_proceeds"}
BENCHMARK = {"rate": "0.04", "as_of": "2026-10-01", "source": "synthetic:exit-forward-noi"}


def _add_months(month: str, n: int) -> str:
    year, mon = int(month[:4]), int(month[5:7])
    index = year * 12 + (mon - 1) + n
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _window(results):
    return results["metrics"]["exit"]["exit_noi_window"]


def _with_window(inputs, window):
    out = copy.deepcopy(inputs)
    out["exit_assumptions"]["noi_window"] = window
    return out


def _monthly_noi(results):
    return {row["month"]: row["net_operating_income"] for row in results["cashflow"]["by_month"]}


def _sum(values):
    return float(sum((dec(v) for v in values), dec(0)))


def _longer_analysis(inputs, months=12):
    """The same deal with the analysis run ``months`` longer and the exit where it was.

    Every curve segment, program and reserve that reaches the analysis end is
    carried to the new end at its terminal value, as the projection does.
    """
    out = copy.deepcopy(inputs)
    end = out["time_grid"]["analysis_end_date"][:7]
    new_end = _add_months(end, months)
    out["time_grid"]["analysis_end_date"] = new_end
    for value in out.values():
        if not isinstance(value, list):
            continue
        for row in value:
            if not isinstance(row, dict):
                continue
            for key in ("end_period", "end_month"):
                if str(row.get(key) or "")[:7] == end:
                    row[key] = new_end
    return out


@lru_cache(maxsize=None)
def _library(slug: str) -> str:
    return json.dumps(dl.ingest(dl.load_deal(slug)))


# ── the window ───────────────────────────────────────────────────────────


def test_the_exit_is_valued_on_the_twelve_months_after_the_exit_month(minimal_deal_inputs):
    results = run_underwriting(copy.deepcopy(minimal_deal_inputs))
    exit_ = results["metrics"]["exit"]
    assert exit_["exit_month"] == "2031-06"
    assert _window(results) == {
        "start": "2031-07", "end": "2032-06", "months": 12, "basis": "forward", "requested": "forward",
    }
    # The NOI valued is disclosed under its own name; forward_noi stays as an alias.
    assert exit_["exit_noi"] == exit_["forward_noi"]
    cap = minimal_deal_inputs["exit_assumptions"]["exit_cap_rate"]
    assert exit_["gross_sale_price"] == pytest.approx(exit_["exit_noi"] / cap, abs=0.01)


def test_forward_noi_matches_a_run_one_year_longer(minimal_deal_inputs):
    """The projection is the engine's own NOI for the next year, not an estimate of it."""
    results = run_underwriting(copy.deepcopy(minimal_deal_inputs))
    longer = run_underwriting(_longer_analysis(minimal_deal_inputs))
    noi = _monthly_noi(longer)
    expected = _sum(noi[_add_months("2031-06", k)] for k in range(1, 13))
    assert results["metrics"]["exit"]["exit_noi"] == pytest.approx(expected, abs=0.01)
    # The longer run's hold months are the hold's months: projecting never moves them.
    for month, value in _monthly_noi(results).items():
        assert noi[month] == value, month


@pytest.mark.parametrize("slug", ["a_stabilized_1990s", "c_value_add_1970s_reno"])
def test_library_forward_noi_matches_a_run_one_year_longer(slug):
    """Curves at their terminal values, the rent growth vector, opex growth and renovations."""
    canonical = json.loads(_library(slug))
    results = run_underwriting(copy.deepcopy(canonical))
    longer = run_underwriting(_longer_analysis(canonical), skip_validation=True)
    exit_month = results["metrics"]["exit"]["exit_month"]
    noi = _monthly_noi(longer)
    expected = _sum(noi[_add_months(exit_month, k)] for k in range(1, 13))
    assert results["metrics"]["exit"]["exit_noi"] == pytest.approx(expected, abs=0.01)
    window = _window(results)
    assert (window["start"], window["end"], window["months"], window["basis"]) == (
        _add_months(exit_month, 1), _add_months(exit_month, 12), 12, "forward",
    )


def test_rising_noi_gives_a_higher_forward_exit_value_than_trailing(minimal_deal_inputs):
    forward = run_underwriting(copy.deepcopy(minimal_deal_inputs))
    trailing = run_underwriting(_with_window(minimal_deal_inputs, "trailing"))
    f, t = forward["metrics"]["exit"], trailing["metrics"]["exit"]
    assert f["exit_noi"] > t["exit_noi"]
    assert f["gross_sale_price"] > t["gross_sale_price"]
    assert f["net_sale_proceeds"] > t["net_sale_proceeds"]
    assert forward["metrics"]["irr"]["levered_irr"] > trailing["metrics"]["irr"]["levered_irr"]
    assert forward["metrics"]["irr"]["unlevered_irr"] > trailing["metrics"]["irr"]["unlevered_irr"]


def test_flat_noi_values_the_exit_the_same_on_either_window(minimal_deal_inputs):
    deal = copy.deepcopy(minimal_deal_inputs)
    deal["growth_assumptions"] = {"growth_type": "annual_compound", "annual_growth_rate": 0.0}
    for row in deal["opex_table"]:
        row["growth_rate"] = 0.0
    forward = run_underwriting(copy.deepcopy(deal))
    trailing = run_underwriting(_with_window(deal, "trailing"))
    assert len(set(_monthly_noi(forward).values())) == 1  # NOI is flat
    assert forward["metrics"]["exit"]["exit_noi"] == trailing["metrics"]["exit"]["exit_noi"]
    assert forward["metrics"]["exit"]["gross_sale_price"] == trailing["metrics"]["exit"]["gross_sale_price"]
    assert forward["metrics"]["irr"] == trailing["metrics"]["irr"]


def test_an_exit_before_the_analysis_end_uses_the_hold_months_after_it(minimal_deal_inputs):
    deal = copy.deepcopy(minimal_deal_inputs)
    deal["exit_assumptions"]["exit_month"] = "2030-06"
    results = run_underwriting(deal)
    noi = _monthly_noi(results)
    expected = _sum(noi[_add_months("2030-06", k)] for k in range(1, 13))
    assert results["metrics"]["exit"]["exit_noi"] == pytest.approx(expected, abs=0.01)
    assert _window(results)["start"] == "2030-07" and _window(results)["end"] == "2031-06"


# ── nothing past the exit leaks into the hold ────────────────────────────


def test_hold_cash_flows_are_unchanged_except_the_reversion(minimal_deal_inputs):
    forward = run_underwriting(copy.deepcopy(minimal_deal_inputs))
    trailing = run_underwriting(_with_window(minimal_deal_inputs, "trailing"))
    hold = [m for m in forward["time_grid"]["months"]]
    assert hold == trailing["time_grid"]["months"] and hold[-1] == "2031-06"
    exit_month = forward["metrics"]["exit"]["exit_month"]
    f_exit, t_exit = forward["metrics"]["exit"], trailing["metrics"]["exit"]
    reversion_delta = (f_exit["gross_sale_price"] - f_exit["sale_costs"]) - (
        t_exit["gross_sale_price"] - t_exit["sale_costs"]
    )
    for f_row, t_row in zip(forward["cashflow"]["by_month"], trailing["cashflow"]["by_month"]):
        assert f_row["month"] == t_row["month"]
        if f_row["month"] != exit_month:
            assert f_row == t_row, f_row["month"]
            continue
        moved = {k for k in f_row if f_row[k] != t_row[k]}
        assert moved <= REVERSION_FIELDS | {"unleveraged_cash_flow", "leveraged_cash_flow"}
        assert f_row["unleveraged_cash_flow"] - t_row["unleveraged_cash_flow"] == pytest.approx(
            reversion_delta, abs=0.05)
        assert f_row["leveraged_cash_flow"] - t_row["leveraged_cash_flow"] == pytest.approx(
            reversion_delta, abs=0.05)
    # The months, revenue, opex, debt, DSCR, cash-on-cash and the going-in cap do not move.
    for section in ("revenue", "opex", "capex", "debt", "rent_growth", "property_tax_calculation"):
        assert forward[section] == trailing[section], section
    f_m, t_m = forward["metrics"], trailing["metrics"]
    for key in ("dscr", "coc", "noi", "yields"):
        assert f_m[key] == t_m[key], key
    assert f_m["cash_on_cash"]["cash_on_cash_year_1"] == t_m["cash_on_cash"]["cash_on_cash_year_1"]
    for row in forward["rent_growth"]["market_rent_curve"]:
        assert row["end_period"] <= "2031-06"


def test_the_trailing_opt_in_reproduces_the_previous_valuation_exactly(minimal_deal_inputs):
    """Values computed by this engine before the change, on the same synthetic deal."""
    trailing = run_underwriting(_with_window(minimal_deal_inputs, "trailing"))
    exit_ = trailing["metrics"]["exit"]
    assert exit_["exit_noi"] == 1190058.36
    assert exit_["forward_noi"] == 1190058.36
    assert exit_["gross_sale_price"] == 21637424.73
    assert exit_["sale_costs"] == 432748.49
    assert exit_["net_sale_proceeds"] == 12922796.72
    assert trailing["metrics"]["irr"] == {"unlevered_irr": 0.1649, "levered_irr": 0.2983}
    assert trailing["metrics"]["equity_multiple"] == {"unlevered_em": 1.95, "levered_em": 3.17}
    assert _window(trailing) == {
        "start": "2030-07", "end": "2031-06", "months": 12, "basis": "trailing", "requested": "trailing",
    }
    noi = _monthly_noi(trailing)
    assert exit_["exit_noi"] == pytest.approx(_sum(noi[_add_months("2030-06", k)] for k in range(1, 13)), abs=0.01)


def test_an_override_noi_is_disclosed_as_the_basis(minimal_deal_inputs):
    deal = copy.deepcopy(minimal_deal_inputs)
    deal["exit_assumptions"]["forward_noi_override"] = 1_000_000
    results = run_underwriting(deal)
    assert results["metrics"]["exit"]["exit_noi"] == 1_000_000
    assert _window(results)["basis"] == "override"


def test_an_unknown_window_is_refused(minimal_deal_inputs):
    with pytest.raises(ValueError, match="noi_window"):
        run_underwriting(_with_window(minimal_deal_inputs, "sideways"), skip_validation=True)


def test_the_schema_carries_the_window_with_forward_as_the_default():
    from engine.validator import _load_schema  # noqa: PLC0415 - the schema the validator uses

    prop = _load_schema()["$defs"]["exit_assumptions"]["properties"]["noi_window"]
    assert prop["enum"] == ["forward", "trailing"]
    assert prop["default"] == "forward"


# ── the helper without a projection ──────────────────────────────────────


def test_compute_exit_proceeds_uses_the_projected_months():
    grid = TimeGrid.build("2026-01", "2027-12")
    cashflow = [{"month": m, "net_operating_income": 1000.0 + i} for i, m in enumerate(grid.month_ids)]
    projected = [{"month": _add_months("2027-12", k), "net_operating_income": 5000.0} for k in range(1, 13)]
    exit_assumptions = {"exit_cap_rate": 0.05, "sale_cost_percent": 0.0, "exit_month": "2027-12"}
    result = compute_exit_proceeds(grid, [], [], exit_assumptions=exit_assumptions,
                                   cashflow_by_month=cashflow, projected_noi_by_month=projected)
    assert result["exit_noi"] == 60000.0
    assert result["gross_sale_price"] == 1200000.0
    assert result["exit_noi_window"]["start"] == "2028-01"
    # Without a projection past the analysis end there is no forward month: the
    # helper falls back to the trailing year and says so.
    fallback = compute_exit_proceeds(grid, [], [], exit_assumptions=exit_assumptions, cashflow_by_month=cashflow)
    assert fallback["exit_noi"] == _sum(1000.0 + i for i in range(12, 24))
    assert fallback["exit_noi_window"] == {
        "start": "2027-01", "end": "2027-12", "months": 12, "basis": "trailing", "requested": "forward",
    }


# ── the exit-cap grid ────────────────────────────────────────────────────


@pytest.mark.parametrize("window", ["forward", "trailing"])
def test_the_grid_values_on_the_same_noi_and_its_base_point_is_the_base_case(minimal_deal_inputs, window):
    results = run_underwriting(_with_window(minimal_deal_inputs, window))
    grid, metrics = results["exit_cap_sensitivity"], results["metrics"]
    assert grid["forward_noi"] == metrics["exit"]["exit_noi"]
    assert grid["exit_noi_window"] == metrics["exit"]["exit_noi_window"]
    base = next(p for p in grid["points"] if p["offset_bps"] == 0)
    assert base["exit_value"] == metrics["exit"]["gross_sale_price"]
    assert base["net_sale_proceeds"] == metrics["exit"]["net_sale_proceeds"]
    assert base["levered_irr"] == metrics["irr"]["levered_irr"]
    assert base["unlevered_irr"] == metrics["irr"]["unlevered_irr"]
    up = next(p for p in grid["points"] if p["offset_bps"] == 100)
    assert up["exit_value"] == pytest.approx(metrics["exit"]["exit_noi"] / up["exit_cap_rate"], abs=0.02)


# ── the assumption ───────────────────────────────────────────────────────


def test_the_window_is_an_assumption_with_a_house_source_and_a_rationale(minimal_deal_inputs):
    resolved = resolve_assumptions(copy.deepcopy(minimal_deal_inputs))
    entry = resolved["assumptions"]["entries"]["exit_noi_window"]
    assert entry["value"] == "forward"
    assert entry["source"] == "house:exit_noi_window"
    assert "HOUSE_EXIT_NOI_WINDOW" in entry["basis"]
    assert rationale_for("house:exit_noi_window")["known"]
    # The house default is not written: the engine applies it when the field is absent.
    assert "noi_window" not in resolved["exit_assumptions"]


def test_an_override_writes_the_field_and_an_unknown_value_is_refused(minimal_deal_inputs):
    resolved = resolve_assumptions(copy.deepcopy(minimal_deal_inputs), overrides={"exit_noi_window": "trailing"})
    entry = resolved["assumptions"]["entries"]["exit_noi_window"]
    assert entry["source"] == "analyst" and entry["value"] == "trailing"
    assert resolved["exit_assumptions"]["noi_window"] == "trailing"
    assert _window(run_underwriting(resolved))["basis"] == "trailing"
    with pytest.raises(AssumptionOverrideError):
        resolve_assumptions(copy.deepcopy(minimal_deal_inputs), overrides={"exit_noi_window": "stabilized"})


def test_the_register_shows_the_window_in_the_exit_group():
    from engine.assumptions.register import build_register  # noqa: PLC0415

    canonical = json.loads(_library("a_stabilized_1990s"))
    rows = {row["key"]: row for row in build_register(canonical)["rows"]}
    row = rows["exit_noi_window"]
    assert row["group"] == "Exit"
    assert row["why"]["rationale_known"]


# ── the refi: the forward year draws on the projection ───────────────────


def test_a_refi_near_the_analysis_end_sizes_on_projected_months(minimal_deal_inputs):
    deal = copy.deepcopy(minimal_deal_inputs)
    deal["refi_event"] = {
        "trigger_month": "2031-01",
        "sizing_method": "dscr",
        "target_dscr": 1.25,
        "perm_loan_terms": {"rate": 0.055, "amort_years": 30},
    }
    results = run_underwriting(deal)
    refi = results["refi"]
    assert refi["noi_window"] == {"start": "2031-01", "end": "2031-12", "months": 12, "forward": True}
    assert refi["deduction_window"] == ["2031-01", "2031-12"]
    assert refi["noi_projected_months"] == 6
    longer = _longer_analysis(minimal_deal_inputs)
    noi = _monthly_noi(run_underwriting(longer))
    expected = _sum(noi[_add_months("2030-12", k)] for k in range(1, 13))
    assert refi["noi_used"] == pytest.approx(expected, abs=0.01)
    # The hold still ends at the analysis end.
    assert results["cashflow"]["by_month"][-1]["month"] == "2031-06"


def test_size_refi_loan_uses_projected_rows_past_the_cash_flow():
    grid = TimeGrid.build("2025-01", "2028-12")
    cashflow = [{"month": m, "net_operating_income": 50000.0, "replacement_reserves": 1000.0}
                for m in grid.month_ids]
    projected = [{"month": _add_months("2028-12", k), "net_operating_income": 60000.0,
                  "replacement_reserves": 1000.0} for k in range(1, 13)]
    debt = [{"month": m, "ending_balance": 5_000_000} for m in grid.month_ids]
    event = {"trigger_month": "2028-07", "sizing_method": "dscr", "target_dscr": 1.25,
             "perm_loan_terms": {"rate": 0.055, "amort_years": 30}}
    result = size_refi_loan(event, cashflow, debt, projected_by_month=projected)
    assert result["noi_used"] == 6 * 50000.0 + 6 * 60000.0
    assert result["below_noi_deduction"] == 12000.0
    assert result["noi_window"] == {"start": "2028-07", "end": "2029-06", "months": 12, "forward": True}
    assert result["deduction_window"] == ["2028-07", "2029-06"]
    assert result["noi_projected_months"] == 6
    # Without a projection the six months left are annualized, as before.
    annualized = size_refi_loan(event, cashflow, debt)
    assert annualized["noi_used"] == 600000.0 and annualized["noi_projected_months"] == 0


# ── backsolve: cash-on-cash prices do not move; IRR at the price does ────


def test_a_cash_on_cash_solve_prices_the_same_on_either_window():
    canonical = json.loads(_library("a_stabilized_1990s"))
    policy = {"version": "plat.backsolve-policy/1", "strategy": "cashflow"}
    solved = {}
    for window in ("forward", "trailing"):
        deal = _with_window(canonical, window)
        solved[window] = backsolve_price(
            deal, target_coc="0.06", policy=policy, benchmark=BENCHMARK,
            min_price="1000000.00", max_price="40000000.00",
        )
        assert solved[window]["status"] == "converged"
    f, t = solved["forward"]["summary"], solved["trailing"]["summary"]
    assert f["solved_purchase_price"] == t["solved_purchase_price"]
    assert f["cash_on_cash_year_1"] == t["cash_on_cash_year_1"]
    # The going-in cap (year-1 NOI / price) and so the entry-anchored exit cap are unchanged.
    assert (solved["forward"]["case"]["exit_assumptions"]["exit_cap_rate"]
            == solved["trailing"]["case"]["exit_assumptions"]["exit_cap_rate"])
    assert f["levered_irr"] != t["levered_irr"]
    assert _window(solved["forward"]["results"])["basis"] == "forward"
