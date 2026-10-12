"""The loan is sized, and DSCR reported, on underwritten NCF (ADR 0003 §3).

Agency lenders size on NOI less replacement reserves. The house sizes the DSCR
constraint on NOI less the same below-NOI capex charge the cash flow deducts,
max(recurring capex, lender reserve), and reports DSCR on that basis, with the
NOI-based DSCR beside it. The basis is the overridable assumption
``loan_sizing_noi_basis``: ``ncf_after_capex_charge`` (house default),
``ncf_after_lender_reserve`` or ``noi_before_capex``. Synthetic deals only.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from functools import lru_cache

import pytest

from engine import backsolve
from engine import underwriting_policy as house
from engine.assumptions import resolve_assumptions
from engine.assumptions.rationale import rationale_for
from engine.assumptions.resolver import AssumptionOverrideError
from engine.backsolve_policy import BacksolveInputError, resolve_policy
from engine.engine import run_underwriting
from engine.feasibility import classify
from engine.modules.debt import AGENCY_MIN_DSCR, _annual_debt_constant, compute_agency_loan_terms, size_refi_loan
from engine.modules.time_grid import TimeGrid
from engine.validator import validate_deal
from tests import deal_library as dl

UNITS = 100  # the minimal deal fixture
STABILIZED = "a_stabilized_1990s"  # 200 units, 1990s band; recurring capex $750/unit binds over the reserve
BENCHMARK = {"rate": "0.04", "as_of": "2026-10-01", "source": "synthetic:loan-sizing"}
POLICY = {"version": "plat.backsolve-policy/1", "strategy": "cashflow"}
DSCR_BOUND_PRICE = Decimal("45000000")  # a going-in cap well below the debt constant: 1.25x DSCR binds
LTV_BOUND_PRICE = Decimal("15000000")  # a high going-in cap: 65% LTV binds


def test_house_default_and_bases_are_named_policy():
    assert house.HOUSE_LOAN_SIZING_NOI_BASIS == "ncf_after_capex_charge"
    assert house.LOAN_SIZING_NOI_BASES == ("ncf_after_capex_charge", "ncf_after_lender_reserve", "noi_before_capex")


# ── the agency sizing function ───────────────────────────────────────────


def _terms(deduction, *, price=60_000_000, basis="ncf_after_capex_charge"):
    return compute_agency_loan_terms(
        2000, t12_noi=3_000_000, projected_noi=3_000_000, purchase_price=price,
        below_noi_deduction=deduction, noi_basis=basis,
    )


def test_dscr_constrained_loan_falls_as_the_below_noi_charge_rises():
    light, heavy = _terms(50_000), _terms(200_000)
    assert light["sizing_method"] == heavy["sizing_method"] == "dscr_constrained"
    assert heavy["loan_amount"] < light["loan_amount"]
    constant = _annual_debt_constant(Decimal("0.055"), 30)
    expected = (Decimal("2800000") / (AGENCY_MIN_DSCR * constant) / 1000).quantize(Decimal("1")) * 1000
    assert heavy["loan_amount"] == float(expected)
    assert heavy["noi_used"] == 3_000_000.0  # NOI before capex, as before
    assert heavy["below_noi_deduction"] == 200_000.0
    assert heavy["sizing_cash_flow"] == 2_800_000.0
    assert heavy["sizing_noi_basis"] == "ncf_after_capex_charge"


def test_ltv_constrained_loan_is_unaffected_by_the_charge():
    light, heavy = _terms(50_000, price=30_000_000), _terms(200_000, price=30_000_000)
    assert light["sizing_method"] == heavy["sizing_method"] == "ltv_constrained"
    assert light["loan_amount"] == heavy["loan_amount"] == 19_500_000.0


def test_a_charge_that_consumes_the_noi_sizes_no_loan_rather_than_falling_back_to_ltv():
    terms = _terms(3_000_000, price=30_000_000)
    assert terms["loan_amount"] == 0.0 and terms["sizing_method"] == "dscr_constrained"


def test_without_a_deduction_sizing_is_on_noi_as_before():
    before = compute_agency_loan_terms(2000, t12_noi=3_000_000, projected_noi=3_000_000, purchase_price=60_000_000)
    assert before["sizing_noi_basis"] == "noi_before_capex"
    assert before["sizing_cash_flow"] == before["noi_used"] == 3_000_000.0
    with pytest.raises(ValueError):
        compute_agency_loan_terms(2000, t12_noi=1, projected_noi=1, below_noi_deduction=10, noi_basis="noi_before_capex")


# ── the engine: DSCR on the sizing basis ─────────────────────────────────


def _with_recurring(deal, per_unit, basis=None):
    out = copy.deepcopy(deal)
    out["capital_plan"] = {"recurring_capex": {"per_unit_annual": per_unit, "growth_rate": 0.0}}
    if basis is not None:
        out["loan_sizing_noi_basis"] = basis
    return out


def _dscr_by_year(results, deduct):
    out = []
    for year in results["cashflow"]["by_year"]:
        if year["debt_service"] > 0:
            out.append(round((year["net_operating_income"] - deduct(year)) / year["debt_service"], 2))
    return out


def test_engine_reports_dscr_on_ncf_after_the_capex_charge_by_default(minimal_deal_inputs):
    results = run_underwriting(_with_recurring(minimal_deal_inputs, 500.0))
    dscr = results["metrics"]["dscr"]
    assert dscr["basis"] == "ncf_after_capex_charge"
    assert "max(recurring capex, lender reserve)" in dscr["basis_label"]
    expected = _dscr_by_year(results, lambda y: y["replacement_reserves"])
    assert [row["dscr"] for row in dscr["by_year"] if row["dscr"] is not None] == expected
    assert dscr["minimum_dscr"] == min(expected)
    # The NOI-based DSCR stays visible, labeled, and is higher.
    noi = dscr["noi_before_capex"]
    assert noi["basis_label"] == "NOI (before capex)"
    assert noi["minimum_dscr"] == min(_dscr_by_year(results, lambda y: 0))
    assert noi["minimum_dscr"] > dscr["minimum_dscr"]


def test_without_a_capital_plan_the_reserve_is_the_charge(minimal_deal_inputs):
    results = run_underwriting(copy.deepcopy(minimal_deal_inputs))
    dscr = results["metrics"]["dscr"]
    assert dscr["minimum_dscr"] == min(_dscr_by_year(results, lambda y: y["replacement_reserves"]))
    assert dscr["minimum_dscr"] < dscr["noi_before_capex"]["minimum_dscr"]


@pytest.mark.parametrize("basis, deduct", [
    ("ncf_after_lender_reserve", lambda y: y["replacement_reserve_floor"]),
    ("noi_before_capex", lambda y: 0),
])
def test_the_basis_override_changes_the_reported_dscr(minimal_deal_inputs, basis, deduct):
    results = run_underwriting(_with_recurring(minimal_deal_inputs, 500.0, basis))
    dscr = results["metrics"]["dscr"]
    assert dscr["basis"] == basis
    assert dscr["minimum_dscr"] == min(_dscr_by_year(results, deduct))
    assert results["capital_plan"]["metric_basis"]["dscr"].startswith(
        "NOI / debt service" if basis == "noi_before_capex" else "underwritten NCF / debt service")


def test_an_unknown_basis_is_refused(minimal_deal_inputs):
    deal = copy.deepcopy(minimal_deal_inputs)
    deal["loan_sizing_noi_basis"] = "gross_revenue"
    assert validate_deal(deal).status != "PASS"
    with pytest.raises(ValueError):
        run_underwriting(deal, skip_validation=True)


def test_the_capital_plan_metric_basis_says_dscr_and_sizing_are_after_the_charge(minimal_deal_inputs):
    basis = run_underwriting(_with_recurring(minimal_deal_inputs, 500.0))["capital_plan"]["metric_basis"]
    assert basis["dscr"].startswith("underwritten NCF / debt service")
    assert "max(recurring capex, lender reserve)" in basis["dscr"]
    assert "noi_before_capex" in basis["dscr"]
    assert basis["loan_sizing"].startswith("agency DSCR sizing on underwritten NCF")
    assert basis["net_operating_income"].startswith("before capex")


# ── the assumption ───────────────────────────────────────────────────────


def test_the_basis_is_an_assumption_with_a_house_source_and_a_rationale(minimal_deal_inputs):
    resolved = resolve_assumptions(copy.deepcopy(minimal_deal_inputs))
    entry = resolved["assumptions"]["entries"]["loan_sizing_noi_basis"]
    assert entry["value"] == "ncf_after_capex_charge"
    assert entry["source"] == "house:loan_sizing_noi_basis"
    assert "HOUSE_LOAN_SIZING_NOI_BASIS" in entry["basis"]
    assert rationale_for("house:loan_sizing_noi_basis")["known"]
    # The house default is not written: the engine applies it when the field is absent.
    assert "loan_sizing_noi_basis" not in resolved


def test_an_override_writes_the_field_and_an_unknown_value_is_refused(minimal_deal_inputs):
    resolved = resolve_assumptions(copy.deepcopy(minimal_deal_inputs),
                                   overrides={"loan_sizing_noi_basis": "noi_before_capex"})
    entry = resolved["assumptions"]["entries"]["loan_sizing_noi_basis"]
    assert entry["source"] == "analyst" and entry["value"] == "noi_before_capex"
    assert resolved["loan_sizing_noi_basis"] == "noi_before_capex"
    assert run_underwriting(resolved)["metrics"]["dscr"]["basis"] == "noi_before_capex"
    with pytest.raises(AssumptionOverrideError):
        resolve_assumptions(copy.deepcopy(minimal_deal_inputs), overrides={"loan_sizing_noi_basis": "egi"})


# ── backsolve: every candidate price is sized on the basis ───────────────


@lru_cache(maxsize=None)
def _library(slug: str) -> str:
    return json.dumps(dl.ingest(dl.load_deal(slug)))


def _case(canonical, price, basis="ncf_after_capex_charge"):
    return backsolve._build_price_case(
        canonical, price=price, target_coc=Decimal("0.06"), year_built=1995,
        benchmark_treasury=Decimal("0.04"), agency_spread=Decimal("0.015"),
        purchase_closing_cost_pct=Decimal("0.01"), partnership_closing_costs=Decimal("0"),
        acquisition_fee_pct=Decimal("0"), loan_closing_costs=Decimal("0"), projected_noi=Decimal("0"),
        loan_sizing_noi_basis=basis,
    )


def _sizing(case):
    return case["metadata"]["property_summary"]["loan_sizing"]


def test_backsolve_sizes_each_candidate_on_ncf_after_the_capex_charge():
    canonical = json.loads(_library(STABILIZED))
    case = _case(canonical, DSCR_BOUND_PRICE)
    sizing = _sizing(case)
    assert sizing["sizing_method"] == "dscr_constrained"
    assert sizing["sizing_noi_basis"] == "ncf_after_capex_charge"
    # The deduction is the projected year's charge: recurring capex $750/unit on 200 units, grown one year.
    assert 150_000 < sizing["below_noi_deduction"] < 160_000
    assert sizing["sizing_cash_flow"] == pytest.approx(sizing["noi_used"] - sizing["below_noi_deduction"], abs=0.01)
    noi_case = _case(canonical, DSCR_BOUND_PRICE, basis="noi_before_capex")
    assert _sizing(noi_case)["below_noi_deduction"] == 0.0
    assert case["debt_terms"]["commitment"] < noi_case["debt_terms"]["commitment"]
    assert "underwritten NCF" in case["pricing_provenance"]["strike_price_derivation"]


def test_backsolve_sizing_loan_falls_when_recurring_capex_rises():
    base = json.loads(_library(STABILIZED))
    heavier = resolve_assumptions(copy.deepcopy(base), overrides={"recurring_capex_per_unit": 1_500})
    light, heavy = _case(base, DSCR_BOUND_PRICE), _case(heavier, DSCR_BOUND_PRICE)
    assert _sizing(light)["sizing_method"] == _sizing(heavy)["sizing_method"] == "dscr_constrained"
    assert heavy["debt_terms"]["commitment"] < light["debt_terms"]["commitment"]


def test_backsolve_ltv_constrained_candidate_is_unaffected():
    base = json.loads(_library(STABILIZED))
    heavier = resolve_assumptions(copy.deepcopy(base), overrides={"recurring_capex_per_unit": 1_500})
    light, heavy = _case(base, LTV_BOUND_PRICE), _case(heavier, LTV_BOUND_PRICE)
    assert _sizing(light)["sizing_method"] == _sizing(heavy)["sizing_method"] == "ltv_constrained"
    assert heavy["debt_terms"]["commitment"] == light["debt_terms"]["commitment"] == float(
        Decimal("0.65") * LTV_BOUND_PRICE)


def test_policy_and_canonical_basis_precedence():
    canonical = json.loads(_library(STABILIZED))
    assert resolve_policy(POLICY, canonical)["loan_sizing_noi_basis"] == "ncf_after_capex_charge"
    canonical["loan_sizing_noi_basis"] = "ncf_after_lender_reserve"
    assert resolve_policy(POLICY, canonical)["loan_sizing_noi_basis"] == "ncf_after_lender_reserve"
    explicit = {**POLICY, "loan_sizing_noi_basis": "noi_before_capex"}
    assert resolve_policy(explicit, canonical)["loan_sizing_noi_basis"] == "noi_before_capex"
    with pytest.raises(BacksolveInputError):
        resolve_policy({**POLICY, "loan_sizing_noi_basis": "egi"}, canonical)


def test_backsolve_discloses_the_basis_and_prices_dscr_on_it():
    # A low target pushes the solve up to prices where 1.25x DSCR binds.
    canonical = json.loads(_library(STABILIZED))
    result = backsolve.backsolve_price(
        copy.deepcopy(canonical), target_coc="0.03", policy=POLICY, benchmark=BENCHMARK,
        min_price="20000000.00", max_price="60000000.00",
    )
    assert result["status"] == "converged"
    summary = result["summary"]
    sizing = summary["loan_sizing"]
    assert sizing["basis"] == "ncf_after_capex_charge"
    assert sizing["source"] == "house default (HOUSE_LOAN_SIZING_NOI_BASIS)"
    assert sizing["sizing_method"] == "dscr_constrained"
    assert sizing["below_noi_deduction"] > 0
    assert summary["loan_amount"] == result["case"]["debt_terms"]["commitment"]
    assert summary["minimum_dscr_basis"] == "ncf_after_capex_charge"
    assert summary["minimum_dscr"] == result["results"]["metrics"]["dscr"]["minimum_dscr"]
    assert summary["minimum_dscr_noi_before_capex"] > summary["minimum_dscr"]
    assert "Loan sizing" in backsolve.format_cli_summary(summary, "summary.json")

    overridden = backsolve.backsolve_price(
        copy.deepcopy(canonical), target_coc="0.03", policy={**POLICY, "loan_sizing_noi_basis": "noi_before_capex"},
        benchmark=BENCHMARK, min_price="20000000.00", max_price="60000000.00",
    )
    assert overridden["summary"]["loan_sizing"]["basis"] == "noi_before_capex"
    assert overridden["summary"]["loan_sizing"]["source"] == "explicit policy value"
    assert overridden["case"]["loan_sizing_noi_basis"] == "noi_before_capex"
    assert overridden["results"]["metrics"]["dscr"]["basis"] == "noi_before_capex"
    entry = overridden["case"]["assumptions"]["entries"]["loan_sizing_noi_basis"]
    assert entry["source"] == "house:backsolve_policy" and entry["value"] == "noi_before_capex"


# ── the stabilized refinance: sized on the same basis ────────────────────

REFI = {
    "trigger_month": "2027-01",
    "sizing_method": "dscr",
    "target_dscr": 1.25,
    "perm_loan_terms": {"rate": 0.055, "amort_years": 30},
}


def _refi_rows(charge_per_month=None, floor_per_month=None):
    rows = []
    for month in TimeGrid.build("2025-01", "2030-12").month_ids:
        row = {"month": month, "net_operating_income": 50_000.0}
        if charge_per_month is not None:
            row["replacement_reserves"] = charge_per_month
        if floor_per_month is not None:
            row["replacement_reserve_floor"] = floor_per_month
        rows.append(row)
    return rows


def _debt_rows():
    return [{"month": r["month"], "ending_balance": 5_000_000.0} for r in _refi_rows()]


def _expected_refi_loan(cash_flow):
    constant = _annual_debt_constant(Decimal("0.055"), 30)
    return float((Decimal(str(cash_flow)) / (Decimal("1.25") * constant) / 1000).quantize(Decimal("1")) * 1000)


def test_refi_without_a_charge_sizes_on_noi_as_before():
    result = size_refi_loan(REFI, _refi_rows(), _debt_rows())
    assert result["sized_commitment"] == _expected_refi_loan(600_000)
    assert result["noi_used"] == result["sizing_cash_flow"] == 600_000.0
    assert result["below_noi_deduction"] == 0.0
    assert result["sizing_noi_basis"] == "ncf_after_capex_charge"


def test_refi_loan_falls_when_the_capex_charge_rises():
    light = size_refi_loan(REFI, _refi_rows(2_500.0), _debt_rows())
    heavy = size_refi_loan(REFI, _refi_rows(7_500.0), _debt_rows())
    assert heavy["sized_commitment"] < light["sized_commitment"]
    # The forward year's charge (12 months from the refi date) comes off the same year's NOI.
    assert heavy["below_noi_deduction"] == 90_000.0
    assert heavy["sizing_cash_flow"] == 510_000.0
    assert heavy["sized_commitment"] == _expected_refi_loan(510_000)
    assert heavy["deduction_window"] == ["2027-01", "2027-12"]
    assert heavy["noi_window"] == {"start": "2027-01", "end": "2027-12", "months": 12, "forward": True}
    assert heavy["stabilized_noi"] == 600_000.0  # NOI itself is unchanged


@pytest.mark.parametrize("basis, deduction", [
    ("ncf_after_capex_charge", 90_000.0),
    ("ncf_after_lender_reserve", 36_000.0),
    ("noi_before_capex", 0.0),
])
def test_refi_respects_the_basis_override(basis, deduction):
    result = size_refi_loan(REFI, _refi_rows(7_500.0, 3_000.0), _debt_rows(), noi_basis=basis)
    assert result["sizing_noi_basis"] == basis
    assert result["below_noi_deduction"] == deduction
    assert result["sized_commitment"] == _expected_refi_loan(600_000 - deduction)


def test_engine_refi_is_sized_on_the_canonical_basis(minimal_deal_inputs):
    def run(per_unit, basis=None):
        deal = _with_recurring(minimal_deal_inputs, per_unit, basis)
        deal["refi_event"] = dict(REFI, trigger_month="2028-07")
        return run_underwriting(deal, skip_validation=True)["refi"]

    light, heavy = run(300.0), run(1_200.0)
    assert heavy["sized_commitment"] < light["sized_commitment"]
    assert heavy["below_noi_deduction"] == pytest.approx(1_200.0 * UNITS, abs=1.0)
    assert heavy["sizing_noi_basis"] == "ncf_after_capex_charge"
    on_noi = run(1_200.0, "noi_before_capex")
    assert on_noi["below_noi_deduction"] == 0.0
    assert on_noi["sized_commitment"] > heavy["sized_commitment"]


# ── feasibility: the DSCR floors apply on the reported basis ─────────────


def _feasibility_metrics(min_dscr, **dscr):
    return {
        "irr": {"levered_irr": 0.15},
        "equity_multiple": {"levered_em": 1.8},
        "dscr": {"minimum_dscr": min_dscr, "average_dscr": min_dscr, **dscr},
        "yields": {"going_in_cap_rate": 0.065, "exit_cap_rate": 0.07},
    }


def test_feasibility_floors_apply_to_the_ncf_dscr_and_name_the_basis():
    metrics = _feasibility_metrics(
        1.08, basis="ncf_after_capex_charge", noi_before_capex={"minimum_dscr": 1.30},
    )
    result = classify(metrics)
    assert result.verdict == "fail"
    assert "dscr_below_1_10_likely_covenant_breach" in result.sanity_flags
    reason = result.reasons[result.sanity_flags.index("dscr_below_1_10_likely_covenant_breach")]
    assert "NCF after capex charge" in reason and "1.30 on NOI" in reason
    warn = classify(_feasibility_metrics(1.15, basis="ncf_after_lender_reserve"))
    assert warn.sanity_flags == ["dscr_below_1_20"]
    assert "NCF after lender reserve" in warn.reasons[0]


def test_feasibility_without_a_basis_reads_as_before():
    result = classify(_feasibility_metrics(1.08))
    assert result.reasons == ["Min DSCR 1.08 is below the 1.10 covenant-breach threshold."]


def test_the_engine_feasibility_reads_the_ncf_dscr(minimal_deal_inputs):
    deal = _with_recurring(minimal_deal_inputs, 1_500.0)
    # Lever the deal up until the NOI-based minimum DSCR is about 1.16x.
    noi_dscr = run_underwriting(copy.deepcopy(deal))["metrics"]["dscr"]["noi_before_capex"]["minimum_dscr"]
    deal["debt_terms"]["commitment"] = round(deal["debt_terms"]["commitment"] * noi_dscr / 1.16, -3)
    results = run_underwriting(deal)
    dscr = results["metrics"]["dscr"]
    # The NOI-based DSCR alone would clear 1.10; the NCF one does not.
    assert dscr["noi_before_capex"]["minimum_dscr"] >= 1.10 > dscr["minimum_dscr"]
    flags = results["feasibility"]["sanity_flags"]
    assert "dscr_below_1_10_likely_covenant_breach" in flags
    reason = results["feasibility"]["reasons"][flags.index("dscr_below_1_10_likely_covenant_breach")]
    assert "NCF after capex charge" in reason
