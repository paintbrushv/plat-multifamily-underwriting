"""ADR 0003 §3: the three capex buckets on every deal.

1. Immediate / deferred capex at close (``capital_plan.immediate_capex``):
   funded with equity at close (or a lender holdback), in the Day-0 basis,
   never in NOI or the monthly cash flows, separate from the renovation budget.
2. Recurring capex by vintage (``capital_plan.recurring_capex``): $/unit below
   NOI, grown every analysis year. PCA > analyst > house vintage band (low
   end stabilized, midpoint value-add, high end with deferred maintenance).
3. The lender replacement reserve ($250/unit) is a floor, not an addition:
   the charge below NOI each month is max(recurring capex, reserve).

All deals and figures are synthetic.
"""
from __future__ import annotations

import copy
import json
import sys
from functools import lru_cache

import pytest

from engine import underwriting_policy as house
from engine.assumptions import resolve_assumptions
from engine.assumptions import guardrails
from engine.assumptions.guardrails import GuardrailProfile
from engine.assumptions.rationale import rationale_for
from engine.assumptions.register import build_register
from engine.assumptions.register_html import render_register_html
from engine.assumptions.sheet import (
    read_assumption_overrides,
    read_assumption_overrides_xlsx,
    sheet_rows,
    write_assumptions_json,
    write_assumptions_xlsx,
)
from engine.backsolve import backsolve_price
from engine.engine import run_underwriting
from engine.validator import validate_deal
from tests import deal_library as dl

UNITS = 100  # the minimal deal fixture
BENCHMARK = {"rate": "0.04", "as_of": "2026-10-01", "source": "synthetic:capex-buckets"}
STABILIZED = "a_stabilized_1990s"  # 200 units, 1990s band $750-$2,000


def _with_plan(deal, **plan):
    out = copy.deepcopy(deal)
    out["capital_plan"] = plan
    return out


def _by_year(results, field):
    return [row[field] for row in results["capital_plan"]["below_noi_capex_charge"]["by_analysis_year"]]


# ── engine: where each bucket enters ─────────────────────────────────────


def test_without_a_capital_plan_the_reserve_is_charged_exactly_as_before(minimal_deal_inputs):
    results = run_underwriting(copy.deepcopy(minimal_deal_inputs))
    assert "capital_plan" not in results
    months = results["cashflow"]["by_month"]
    assert {m["replacement_reserves"] for m in months} == {round(25_000 / 12, 2)}
    assert "recurring_capex_by_vintage" not in months[0]


@pytest.mark.parametrize("recurring, binding", [(200.0, "replacement_reserve"), (500.0, "recurring_capex")])
def test_the_reserve_is_a_floor_not_stacked(minimal_deal_inputs, recurring, binding):
    base = run_underwriting(copy.deepcopy(minimal_deal_inputs))
    results = run_underwriting(_with_plan(
        minimal_deal_inputs, recurring_capex={"per_unit_annual": recurring, "growth_rate": 0.0}))
    charge = max(recurring, 250.0) * UNITS
    years = results["capital_plan"]["below_noi_capex_charge"]["by_analysis_year"]
    assert [y["below_noi_capex_charge"] for y in years] == pytest.approx([charge] * 5, abs=0.1)
    assert [y["recurring_capex_by_vintage"] for y in years] == pytest.approx([recurring * UNITS] * 5, abs=0.1)
    assert [y["replacement_reserve_floor"] for y in years] == pytest.approx([25_000.0] * 5, abs=0.1)
    assert {y["binding"] for y in years} == {binding}
    # Charged once: never recurring + reserve.
    month = results["cashflow"]["by_month"][0]
    assert month["replacement_reserves"] == pytest.approx(charge / 12, abs=0.01)
    assert month["replacement_reserves"] < month["recurring_capex_by_vintage"] + month["replacement_reserve_floor"]
    # NOI is before capex; DSCR is on underwritten NCF (NOI less the charge), with the
    # NOI-based DSCR beside it; unlevered cash flow carries the charge.
    for field in ("net_operating_income", "debt_service"):
        assert [y[field] for y in results["cashflow"]["by_year"]] == [y[field] for y in base["cashflow"]["by_year"]]
    assert results["metrics"]["dscr"]["noi_before_capex"] == base["metrics"]["dscr"]["noi_before_capex"]
    if charge > 25_000.0:
        assert results["metrics"]["dscr"]["minimum_dscr"] < base["metrics"]["dscr"]["minimum_dscr"]
    else:
        assert results["metrics"]["dscr"]["minimum_dscr"] == base["metrics"]["dscr"]["minimum_dscr"]
    assert results["metrics"]["yields"]["going_in_cap_rate"] == base["metrics"]["yields"]["going_in_cap_rate"]
    extra = charge - 25_000.0
    assert results["metrics"]["coc"]["components"]["replacement_reserves_year_1"] == pytest.approx(
        base["metrics"]["coc"]["components"]["replacement_reserves_year_1"] + extra, abs=1.0)
    if extra:
        assert results["metrics"]["coc"]["cash_on_cash_year_1"] < base["metrics"]["coc"]["cash_on_cash_year_1"]
        assert results["metrics"]["irr"]["levered_irr"] < base["metrics"]["irr"]["levered_irr"]
    assert results["capital_plan"]["metric_basis"]["dscr"].startswith("underwritten NCF / debt service")


def test_recurring_capex_grows_every_year_and_can_overtake_the_reserve(minimal_deal_inputs):
    results = run_underwriting(_with_plan(
        minimal_deal_inputs, recurring_capex={"per_unit_annual": 240.0, "growth_rate": 0.10}))
    assert _by_year(results, "recurring_capex_by_vintage") == pytest.approx(
        [240 * UNITS * 1.1 ** k for k in range(5)], abs=0.5)
    assert _by_year(results, "binding") == ["replacement_reserve"] + ["recurring_capex"] * 4
    assert _by_year(results, "below_noi_capex_charge")[0] == pytest.approx(25_000.0, abs=0.1)


def test_immediate_capex_is_equity_at_close_never_noi(minimal_deal_inputs):
    base = run_underwriting(copy.deepcopy(minimal_deal_inputs))
    results = run_underwriting(_with_plan(minimal_deal_inputs, immediate_capex={"amount": 400_000.0}))
    # The fixture states its total equity basis; the bucket is added to it, never assumed inside it.
    stated = minimal_deal_inputs["purchase_assumptions"]["total_equity_basis"]
    assert results["metrics"]["coc"]["components"]["equity_basis"] == pytest.approx(stated + 400_000.0)
    assert results["capital_plan"]["immediate_capex"] == {
        "amount": 400_000.0, "funding": "equity_at_close", "in_total_equity_basis": True,
        "in_total_unlevered_basis": True, "amount_in_stated_basis": 0.0, "added_by_engine": 400_000.0,
    }
    # Not in NOI, the below-NOI charge or operating capex: only the Day-0 outlay moves.
    for field in ("net_operating_income", "replacement_reserves", "total_capex", "leveraged_cash_flow"):
        assert [y[field] for y in results["cashflow"]["by_year"]] == [y[field] for y in base["cashflow"]["by_year"]]
    assert results["metrics"]["coc"]["cash_on_cash_year_1"] < base["metrics"]["coc"]["cash_on_cash_year_1"]
    assert results["metrics"]["irr"]["levered_irr"] < base["metrics"]["irr"]["levered_irr"]
    assert results["metrics"]["irr"]["unlevered_irr"] < base["metrics"]["irr"]["unlevered_irr"]

    # Without a stated basis the default (equity contribution) carries it.
    unstated = _with_plan(minimal_deal_inputs, immediate_capex={"amount": 400_000.0})
    unstated["purchase_assumptions"].pop("total_equity_basis")
    equity = minimal_deal_inputs["purchase_assumptions"]["equity_contribution"]
    assert run_underwriting(unstated)["metrics"]["coc"]["components"]["equity_basis"] == pytest.approx(
        equity + 400_000.0)
    # A basis that already carries part of it (backsolve's priced case) gets only the rest.
    partial = _with_plan(minimal_deal_inputs,
                         immediate_capex={"amount": 400_000.0, "amount_in_stated_basis": 150_000.0})
    partial_results = run_underwriting(partial)
    assert partial_results["metrics"]["coc"]["components"]["equity_basis"] == pytest.approx(stated + 250_000.0)
    assert partial_results["capital_plan"]["immediate_capex"]["added_by_engine"] == 250_000.0


def test_a_lender_holdback_moves_the_unlevered_basis_only(minimal_deal_inputs):
    base = run_underwriting(copy.deepcopy(minimal_deal_inputs))
    results = run_underwriting(_with_plan(
        minimal_deal_inputs, immediate_capex={"amount": 400_000.0, "funding": "lender_holdback"}))
    assert results["metrics"]["coc"]["components"]["equity_basis"] == base["metrics"]["coc"]["components"]["equity_basis"]
    assert results["metrics"]["irr"]["levered_irr"] == base["metrics"]["irr"]["levered_irr"]
    assert results["metrics"]["irr"]["unlevered_irr"] < base["metrics"]["irr"]["unlevered_irr"]
    assert results["capital_plan"]["immediate_capex"]["in_total_equity_basis"] is False


def test_immediate_capex_and_the_renovation_budget_are_separate_budgets(minimal_deal_inputs):
    deal = _with_plan(minimal_deal_inputs, immediate_capex={"amount": 100_000.0})
    deal["renovation_capex_funding"] = "equity_at_close"
    deal["renovation_programs"] = [{
        "program_id": "synthetic_interiors", "program_name": "Synthetic interiors",
        "target_cohort": "1BR", "output_cohort": "1BR_reno", "renovation_cost_per_unit": 8_000,
        "rent_premium_monthly": 100, "downtime_days": 30, "strategy": "proactive",
        "start_month": "2026-09", "monthly_pace": 2, "max_units": 10,
    }]
    results = run_underwriting(deal)
    stated = minimal_deal_inputs["purchase_assumptions"]["total_equity_basis"]
    plan = results["capital_plan"]
    assert plan["renovation_budget"] == {"amount": 80_000.0, "funding": "equity_at_close"}
    assert plan["immediate_capex"]["amount"] == 100_000.0
    # The stated basis is taken as carrying the renovation budget (its convention);
    # the immediate-capex bucket is added on top, so the two never double count.
    assert results["metrics"]["coc"]["components"]["equity_basis"] == pytest.approx(stated + 100_000)
    unstated = copy.deepcopy(deal)
    unstated["purchase_assumptions"].pop("total_equity_basis")
    equity = minimal_deal_inputs["purchase_assumptions"]["equity_contribution"]
    assert run_underwriting(unstated)["metrics"]["coc"]["components"]["equity_basis"] == pytest.approx(
        equity + 80_000 + 100_000)


# ── resolver precedence and guardrails ───────────────────────────────────


@pytest.mark.parametrize("deal_type, deferred, expected, pick", [
    ("stabilized", False, 1_000.0, "low"),
    ("value_add", False, 1_750.0, "midpoint"),
    ("stabilized", True, 2_500.0, "high"),
])
def test_house_band_pick_by_deal_type_and_deferred_flag(minimal_deal_inputs, deal_type, deferred, expected, pick):
    profile = {"deal_type": deal_type, "year_built": 1984, "deferred_maintenance": deferred}
    resolved = resolve_assumptions(minimal_deal_inputs, deal_profile=profile)
    entry = resolved["assumptions"]["entries"]["recurring_capex_per_unit"]
    assert (entry["source"], entry["value"], entry["detail"]["band_pick"]) == (
        "house:recurring_capex_vintage", expected, pick)
    assert resolved["capital_plan"]["recurring_capex"] == {"per_unit_annual": expected, "growth_rate": 0.03}
    assert house.HOUSE_RECURRING_CAPEX_GROWTH == house.OPEX_GROWTH_REPAIRS_MAINTENANCE
    assert GuardrailProfile.coerce(resolved["deal_profile"]).recurring_capex_per_unit() == expected


def _pricing(canonical, profile):
    findings = guardrails.enforce_floors(
        canonical, mode="pricing", profile=profile, keys=("recurring_capex_per_unit",))
    return canonical, findings


def test_pricing_floor_raises_unsourced_and_pca_values_and_waives_analyst(minimal_deal_inputs):
    profile = GuardrailProfile(strategy="cashflow", year_built=1984)  # floor $1,000
    # No provenance: raised to the floor (max(value, floor)).
    bare = _with_plan(minimal_deal_inputs, recurring_capex={"per_unit_annual": 400.0})
    raised, findings = _pricing(bare, profile)
    assert [(f["action"], f["original"], f["applied"]) for f in findings] == [("enforced", 400.0, 1_000.0)]
    assert raised["capital_plan"]["recurring_capex"] == {"per_unit_annual": 1_000.0, "growth_rate": 0.03}
    # A hold with no recurring capex gets the floor.
    _, none_findings = _pricing(copy.deepcopy(minimal_deal_inputs), profile)
    assert none_findings[0]["original"] == 0.0 and none_findings[0]["action"] == "enforced"

    # An analyst figure below the floor is kept and disclosed (insurance waiver convention).
    deal = copy.deepcopy(minimal_deal_inputs)
    deal["metadata"]["year_built"] = 1984
    analyst = resolve_assumptions(deal, overrides={"recurring_capex_per_unit": 400})
    kept, findings = _pricing(copy.deepcopy(analyst), profile)
    assert [(f["action"], f["applied"]) for f in findings] == [("floor_waived_by_analyst_override", 400.0)]
    assert kept["capital_plan"]["recurring_capex"]["per_unit_annual"] == 400.0
    record = analyst["assumptions"]["entries"]["recurring_capex_per_unit"]["guardrail"]
    assert record["action"] == "floor_waived_by_analyst_override" and record["floor"] == 1_000.0
    assert not [i for i in validate_deal(analyst).issues
                if i.code == "GUARDRAIL_BELOW_FLOOR" and "recurring_capex" in i.path]

    # PCA evidence below the floor: warned in underwrite, raised to the floor in
    # pricing and disclosed (a lender PCA understates economic capex).
    pca = copy.deepcopy(deal)
    pca["capital_plan"] = {"pca": {"recurring_capex_per_unit": 600.0}}
    pca = resolve_assumptions(pca)
    raised, findings = _pricing(copy.deepcopy(pca), profile)
    assert [(f["action"], f["applied"]) for f in findings] == [("floor_applied_over_deal_evidence", 1_000.0)]
    assert raised["capital_plan"]["recurring_capex"]["per_unit_annual"] == 1_000.0
    record = pca["assumptions"]["entries"]["recurring_capex_per_unit"]["guardrail"]
    assert record["action"] == "warned" and record["evidence"].startswith("the PCA")


# ── backsolve: buckets 1 and 2 move the price ────────────────────────────


@lru_cache(maxsize=None)
def _library(slug: str) -> str:
    return json.dumps(dl.ingest(dl.load_deal(slug)))


def _solve(canonical, **policy_values):
    result = backsolve_price(
        canonical, target_coc="0.06",
        policy={"version": "plat.backsolve-policy/1", "strategy": "cashflow", **policy_values},
        benchmark=BENCHMARK, min_price="15000000.00", max_price="40000000.00",
    )
    assert result["status"] == "converged"
    return result


@lru_cache(maxsize=None)
def _baseline() -> str:
    return json.dumps(_solve(json.loads(_library(STABILIZED))))


def _price(result):
    return result["summary"]["solved_purchase_price"]


def test_backsolve_price_falls_when_recurring_capex_rises():
    base = json.loads(_baseline())
    plan = base["summary"]["capital_plan"]
    assert plan["recurring_capex_per_unit"] == 750.0 and plan["binding_year_1"] == "recurring_capex"
    assert plan["recurring_capex_floor_per_unit"] == 750.0 and plan["replacement_reserve_per_unit"] == 250.0
    assert plan["below_noi_capex_charge"]["by_analysis_year"][0]["binding"] == "recurring_capex"
    heavier = resolve_assumptions(json.loads(_library(STABILIZED)), overrides={"recurring_capex_per_unit": 1_500})
    result = _solve(heavier)
    assert _price(result) < _price(base)
    assert result["case"]["capital_plan"]["recurring_capex"]["per_unit_annual"] == 1_500.0


def test_backsolve_price_falls_when_immediate_capex_is_added_and_equity_carries_it():
    base = json.loads(_baseline())
    canonical = resolve_assumptions(json.loads(_library(STABILIZED)), overrides={"immediate_capex": 600_000})
    result = _solve(canonical)
    assert _price(result) < _price(base)
    purchase, fund = result["case"]["purchase_assumptions"], result["case"]["fund_assumptions"]
    price = purchase["purchase_price"]
    expected = (purchase["equity_contribution"] + purchase["closing_costs"] + price * fund["acquisition_fee_pct"]
                + fund["partnership_closing_costs"] + 600_000)
    assert purchase["total_equity_basis"] == pytest.approx(expected, abs=0.01)
    assert purchase["total_unlevered_basis"] == pytest.approx(price + purchase["closing_costs"] + 600_000, abs=0.01)
    immediate = result["summary"]["capital_plan"]["immediate_capex"]
    assert immediate == {"amount": 600_000.0, "funding": "equity_at_close",
                         "included_in_total_equity_basis": True, "loan_upsized_by_holdback": False}
    # The priced case states the amount it already carries, so the engine adds nothing more.
    assert result["case"]["capital_plan"]["immediate_capex"]["amount_in_stated_basis"] == 600_000.0
    assert result["results"]["metrics"]["coc"]["components"]["equity_basis"] == pytest.approx(
        purchase["total_equity_basis"], abs=0.01)
    # Never in the cash flows.
    assert sum(m["total_capex"] for m in result["results"]["cashflow"]["by_month"]) == 0


def test_a_lender_holdback_upsizes_the_loan_instead_of_the_equity():
    canonical = resolve_assumptions(json.loads(_library(STABILIZED)), overrides={"immediate_capex": 600_000})
    canonical["capital_plan"]["immediate_capex"]["funding"] = "lender_holdback"
    result = _solve(canonical)
    case = result["case"]
    purchase, fund = case["purchase_assumptions"], case["fund_assumptions"]
    price = purchase["purchase_price"]
    sized = price - purchase["equity_contribution"]
    assert case["debt_terms"]["commitment"] == pytest.approx(sized + 600_000, abs=0.01)
    assert purchase["total_equity_basis"] == pytest.approx(
        purchase["equity_contribution"] + purchase["closing_costs"] + price * fund["acquisition_fee_pct"]
        + fund["partnership_closing_costs"], abs=0.01)
    assert result["summary"]["capital_plan"]["immediate_capex"]["loan_upsized_by_holdback"] is True


def test_backsolve_discloses_an_analyst_recurring_capex_below_the_floor():
    base = json.loads(_baseline())
    lighter = resolve_assumptions(json.loads(_library(STABILIZED)), overrides={"recurring_capex_per_unit": 400})
    result = _solve(lighter)
    waivers = [o for o in result["summary"]["policy_summary"]["policy_overrides"]
               if o.get("input") == "recurring_capex_per_unit"]
    assert waivers == [{
        "input": "recurring_capex_per_unit", "action": "floor_waived_by_analyst_override", "floor": 750.0,
        "floor_source": "house:recurring_capex_vintage", "waived_by": "the analyst's recurring capex figure",
        "original": 400.0, "applied": 400.0,
    }]
    entry = result["case"]["assumptions"]["entries"]["recurring_capex_per_unit"]
    assert (entry["value"], entry["source"]) == (400.0, "analyst")
    assert entry["guardrail"]["action"] == "floor_waived_by_analyst_override"
    assert _price(result) > _price(base)  # priced as given: the waiver is disclosed, not silent


# ── assumptions sheet and register ───────────────────────────────────────


def test_the_buckets_round_trip_through_the_sheet(tmp_path):
    canonical = json.loads(_library(STABILIZED))
    rows = {row["key"]: row for row in sheet_rows(canonical)}
    assert [rows[k]["value"] for k in ("immediate_capex", "recurring_capex_per_unit", "replacement_reserve_per_unit")] \
        == [0.0, 750.0, 250.0]

    sheet = tmp_path / "deal.assumptions.json"
    write_assumptions_json(canonical, sheet)
    document = json.loads(sheet.read_text(encoding="utf-8"))
    for row in document["rows"]:
        if row["key"] == "recurring_capex_per_unit":
            row["override_value"], row["override_note"] = 1_200.0, "PCA pending"
        elif row["key"] == "immediate_capex":
            row["override_value"] = 350_000.0
    sheet.write_text(json.dumps(document), encoding="utf-8")
    edited = resolve_assumptions(canonical, overrides=read_assumption_overrides(sheet))
    assert edited["capital_plan"]["recurring_capex"]["per_unit_annual"] == 1_200.0
    assert edited["capital_plan"]["immediate_capex"]["amount"] == 350_000.0
    entry = edited["assumptions"]["entries"]["recurring_capex_per_unit"]
    assert (entry["source"], entry["default_value"], entry["override"]["note"]) == ("analyst", 750.0, "PCA pending")

    # The Excel tab round-trips the same edit, and blanking the override clears it.
    workbook = tmp_path / "deal.xlsx"
    write_assumptions_xlsx(edited, workbook)
    assert resolve_assumptions(edited, overrides=read_assumption_overrides_xlsx(workbook)) == edited
    cleared = resolve_assumptions(edited, overrides={"recurring_capex_per_unit": None, "immediate_capex": None})
    assert cleared["capital_plan"]["recurring_capex"]["per_unit_annual"] == 750.0
    assert cleared["capital_plan"]["immediate_capex"]["amount"] == 0.0


def test_the_register_shows_the_three_buckets_with_their_rationale():
    canonical = json.loads(_library("d_value_add_1980s_msa_only"))  # synthetic PCA
    register = build_register(canonical)
    plan = register["capital_plan"]
    assert [(b["bucket"], b["key"], b["source"]) for b in plan["buckets"]] == [
        (1, "immediate_capex", "deal:pca"),
        (2, "recurring_capex_per_unit", "deal:pca"),
        (3, "replacement_reserve_per_unit", "house:replacement_reserve"),
    ]
    assert plan["binding_year_1"] == "recurring_capex_per_unit" and plan["charge_year_1_per_unit"] == 1_500.0
    rows = {row["key"]: row for row in register["rows"]}
    for key in ("immediate_capex", "recurring_capex_per_unit", "recurring_capex_growth",
                "replacement_reserve_per_unit"):
        assert rows[key]["group"] == "Capital/Capex"
        assert rows[key]["why"]["rationale_known"], key
    assert rows["recurring_capex_per_unit"]["guardrail"]["state"] == "warned"  # PCA below the band
    for source in ("deal:pca", "house:recurring_capex_vintage", "house:deferred_capex", "house:replacement_reserve"):
        assert rationale_for(source)["known"], source
    html = render_register_html(canonical)
    assert "Capital plan: three capex buckets" in html
    assert "Capex bucket 2: recurring capex per unit (year 1)" in html
    assert "binds in year 1" in html and "max(recurring capex, replacement reserve)" in html


# ── ingest CLI ───────────────────────────────────────────────────────────


def test_ingest_cli_capital_plan_flags(tmp_path, monkeypatch, capsys):
    from runs import ingest_deal

    deal = dl.load_deal("c_value_add_1970s_reno")
    folder = deal["_dir"]
    pca = tmp_path / "pca.json"
    pca.write_text(json.dumps({"immediate_capex": 250_000, "recurring_capex_per_unit": 2_400,
                               "as_of": "2026-08-01", "reference": "Synthetic PCA"}), encoding="utf-8")
    output = tmp_path / "deal.json"
    monkeypatch.setattr(sys, "argv", [
        "ingest_deal.py", "--property-id", "Synthetic CLI", "--rent-roll", str(folder / "rent_roll.csv"),
        "--t12", str(folder / "t12.csv"), "--start", deal["analysis"]["start"], "--end", deal["analysis"]["end"],
        "--deal-type", "value_add", "--year-built", "1974", "--deferred-maintenance", "--no-market-data",
        "--pca", str(pca), "--recurring-capex-per-unit", "1800", "--immediate-capex", "90000",
        "--immediate-capex-funding", "lender_holdback", "--output", str(output),
    ])
    assert ingest_deal.main() == 0
    out = capsys.readouterr().out
    canonical = json.loads(output.read_text(encoding="utf-8"))
    assert canonical["deal_profile"]["deferred_maintenance"] is True
    plan = canonical["capital_plan"]
    assert plan["pca"]["recurring_capex_per_unit"] == 2_400.0
    assert plan["recurring_capex"]["per_unit_annual"] == 2_400.0  # PCA > analyst
    assert plan["immediate_capex"] == {"amount": 250_000.0, "funding": "lender_holdback"}
    entries = canonical["assumptions"]["entries"]
    assert [c["source"] for c in entries["recurring_capex_per_unit"]["candidates"]][:2] == ["analyst", "deal:pca"]
    assert "1. Immediate capex at close: $250,000 (deal:pca)" in out
    assert "recurring capex binds" in out
    assert "superseded by the PCA" in out

    # Without a PCA, the analyst figures are the values; a bad funding mode is refused.
    monkeypatch.setattr(sys, "argv", [
        "ingest_deal.py", "--canonical-json", str(output), "--immediate-capex-funding", "seller_credit",
        "--output", str(tmp_path / "bad.json"),
    ])
    with pytest.raises(SystemExit):
        ingest_deal.main()


def test_the_workbook_shows_the_buckets_and_the_charge(tmp_path, minimal_deal_inputs):
    from openpyxl import load_workbook

    from engine.excel_output import write_results_to_workbook

    deal = _with_plan(minimal_deal_inputs, immediate_capex={"amount": 400_000.0},
                      recurring_capex={"per_unit_annual": 500.0, "growth_rate": 0.03})
    path = tmp_path / "results.xlsx"
    write_results_to_workbook(run_underwriting(copy.deepcopy(deal)), path, inputs=deal)
    workbook = load_workbook(path)
    labels = {row[0]: row for row in workbook["Summary"].iter_rows(values_only=True) if row[0]}
    assert "CAPITAL PLAN (3 CAPEX BUCKETS)" in labels
    assert labels["1. Immediate capex at close:"][1] == 400_000.0
    assert labels["2. Recurring capex, $/unit yr 1:"][1] == 500.0
    assert labels["3. Replacement reserve, year 1:"][1] == pytest.approx(25_000.0, abs=0.1)
    assert labels["Below-NOI charge, year 1:"][1] == pytest.approx(50_000.0, abs=0.1)
    annual = {row[0] for row in workbook["Annual_ProForma"].iter_rows(values_only=True) if row[0]}
    assert {"Below-NOI CapEx Charge", "  Recurring CapEx (vintage)", "  Replacement Reserve (floor)"} <= annual


def test_backsolve_cli_summary_prints_the_buckets(tmp_path):
    from engine.backsolve import format_cli_summary

    text = format_cli_summary(json.loads(_baseline())["summary"], tmp_path / "backsolve_summary.json")
    assert "  Immediate capex:  $0 at close (equity_at_close)" in text
    assert "  Recurring capex:  $750/unit/yr vs reserve $250/unit/yr; binds: recurring_capex" in text
