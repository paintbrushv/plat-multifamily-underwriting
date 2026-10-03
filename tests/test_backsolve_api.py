"""Public solver boundary, policy validation, and adapter parity."""

from copy import deepcopy
from decimal import Decimal
import json

import pytest

from engine import backsolve


POLICY = {"version": "plat.backsolve-policy/1", "strategy": "cashflow", "exit_cap_rate": "0.06"}
BENCHMARK = {"rate": "0.04", "as_of": "2026-10-03", "source": "synthetic:test"}
INPUTS = {"metadata": {"property_summary": {"property_tax_policy": {
    "millage_rate_mills": 25.0, "assessment_ratio": 1.0,
    "source": "analyst", "source_locator": "synthetic:test", "analyst_override": False,
}}}}


@pytest.fixture
def economy(monkeypatch):
    """Known decreasing function: target 7% crosses exactly at $50.00."""
    monkeypatch.setattr(backsolve, "_prepare_house_assumptions", lambda c, **kw: (deepcopy(c), {}))
    monkeypatch.setattr(backsolve, "_preview_projected_noi", lambda c: Decimal("100"))

    def build(c, *, price, **kw):
        return {"purchase_assumptions": {"purchase_price": float(price), "total_equity_basis": 100},
                "debt_terms": {"commitment": 0, "rate": 0, "io_months": 0, "term_months": 60}}

    def evaluate(c):
        coc = Decimal("0.08") - Decimal(str(c["purchase_assumptions"]["purchase_price"])) * Decimal("0.0002")
        return {"cashflow": {"by_year": [{"net_operating_income": 100}]}, "metrics": {
            "coc": {"cash_on_cash_year_1": float(coc), "cash_on_cash_year_1_exact": float(coc), "free_cf_year_1": float(coc * 100)},
            "irr": {"levered_irr": 0}, "dscr": {"minimum_dscr": 0},
            "yields": {"going_in_cap_rate": 0, "exit_cap_rate": 0.06},
        }}, coc

    monkeypatch.setattr(backsolve, "_build_price_case", build)
    monkeypatch.setattr(backsolve, "_evaluate_case", evaluate)


def solve(**kwargs):
    request = dict(target_coc="0.07", policy=POLICY, benchmark=BENCHMARK,
                   min_price="20.00", max_price="100.00")
    request.update(kwargs)
    return backsolve.backsolve_price(INPUTS, **request)


def test_maximum_cent_boundary_and_no_input_mutation(economy):
    before = deepcopy((INPUTS, POLICY, BENCHMARK))
    result = solve()
    assert result["status"] == "converged"
    assert result["summary"]["solved_purchase_price"] == 50.0
    boundary = result["summary"]["maximum_feasible_boundary"]
    assert boundary["next_infeasible_price"] == 50.01
    assert boundary["price_gap"] == 0.01
    assert result["effective_assumptions"]["benchmark"] == BENCHMARK
    assert (INPUTS, POLICY, BENCHMARK) == before


@pytest.mark.parametrize("ceiling", ["50.00", "40.00"])
def test_feasible_ceiling_is_reported_without_extrapolating(economy, ceiling):
    result = solve(max_price=ceiling)
    assert result["status"] == "ceiling_feasible"
    assert result["summary"]["solved_purchase_price"] == float(ceiling)
    assert result["summary"]["maximum_feasible_boundary"]["next_infeasible_price"] is None


def test_infeasible_bracket_does_not_return_solved_case(economy):
    result = solve(min_price="60.00")
    assert result["status"] == "infeasible"
    assert result["case"] is None
    assert result["summary"]["solved_purchase_price"] is None


def test_iteration_limit_keeps_feasible_candidate_but_does_not_claim_convergence(economy):
    result = solve(max_iterations=1)
    assert result["status"] == "iteration_limit"
    assert result["summary"]["solved_purchase_price"] == 20.0
    assert result["summary"]["maximum_feasible_boundary"]["price_gap"] > 0.01


@pytest.mark.parametrize("overrides", [
    {"policy": None}, {"policy": {**POLICY, "version": "future"}},
    {"policy": {**POLICY, "unrecognized": 1}},
    {"policy": {**POLICY, "agency_spread": "NaN"}},
    {"benchmark": {"rate": "0.04"}},
    {"benchmark": {**BENCHMARK, "as_of": "2026-02-30"}},
    {"benchmark": {**BENCHMARK, "source": " "}},
    {"benchmark": {**BENCHMARK, "rate": "Infinity"}},
    {"target_coc": True}, {"target_coc": "NaN"},
    {"min_price": "1.001"}, {"min_price": "101"},
    {"max_iterations": 0}, {"max_iterations": True},
])
def test_invalid_inputs_fail_before_calculation(monkeypatch, overrides):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid request reached underwriting")
    monkeypatch.setattr(backsolve, "_prepare_house_assumptions", forbidden)
    with pytest.raises(ValueError):
        solve(**overrides)


def test_cli_and_api_share_solver_output(economy, tmp_path, monkeypatch):
    source = tmp_path / "synthetic.json"
    source.write_text(json.dumps(INPUTS))
    monkeypatch.setattr(backsolve.sys, "argv", [
        "backsolve", "--canonical-json", str(source), "--output-dir", str(tmp_path / "out"),
        "--policy-version", POLICY["version"], "--benchmark-5yr-treasury", BENCHMARK["rate"],
        "--benchmark-as-of", BENCHMARK["as_of"], "--benchmark-source", BENCHMARK["source"],
        "--exit-cap-rate", "0.06", "--min-price", "20", "--max-price", "100",
    ])
    backsolve.main()
    summary = json.loads((tmp_path / "out/backsolve_summary.json").read_text())
    assert summary == {**solve()["summary"], "artifacts": {
        "canonical": str(tmp_path / "out/canonical_backsolved_target_coc.json"),
        "underwriting": str(tmp_path / "out/underwriting_backsolved_target_coc.json"),
    }}


def test_engine_mcp_uses_public_api(economy):
    from engine.mcp_server import backsolve_deal_price

    result = backsolve_deal_price(INPUTS, "0.07", POLICY, BENCHMARK, "20.00", "100.00")
    assert result["summary"] == solve()["summary"]
    assert "results" not in result
    refused = backsolve_deal_price(INPUTS, "0.07", POLICY, {})
    assert refused["status"] == "refused"
    assert refused["error"]["code"] == "INVALID_BACKSOLVE_INPUT"
