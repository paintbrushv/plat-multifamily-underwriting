"""ADR 0003 §9 (slice 8): the synthetic deal library and expected-band tests.

The "plug and play" promise: a stabilized or value-add deal with consistent
documents goes from rent roll + T12 (+ optional market data) to a fully
resolved, engine-runnable underwrite with no hand edits, and the resolved
assumptions land in each deal's expected bands.

Each deal lives in ``tests/fixtures/deals/<slug>/`` (built by
``build_fixtures.py``); ``tests/deal_library.py`` ingests it the way an analyst
would. The bands, expected sources and expected guardrail warnings are in each
``deal.json``; the golden ``expected_assumptions.json`` snapshots are
regenerated only with ``build_fixtures.py --update-goldens``.

All deals, names and market data are synthetic.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
from decimal import Decimal
from functools import lru_cache

import pytest

from engine import underwriting_policy as house
from engine.assumptions import DEAL_TYPE_STRATEGY, resolve_assumptions
from engine.assumptions.canonical_ops import (
    annual_opex_amount,
    concession_pct_rent,
    matching_opex_rows,
    sum_opex_bucket,
    sum_units,
)
from engine.assumptions.guardrails import GUARDRAIL_KEYS
from engine.assumptions.registry import is_valid_source, requires_review
from engine.assumptions.sheet import assumptions_document, write_assumptions_json
from engine.backsolve import backsolve_price
from engine.engine import run_underwriting
from engine.market_data import MarketDataRegistry, SyntheticProvider
from engine.validator import validate_deal
from runs import assumptions_sheet
from tests import deal_library as dl

SLUGS = dl.DEAL_SLUGS
BENCHMARK = {"rate": "0.04", "as_of": "2026-10-01", "source": "synthetic:deal-library"}
# Backsolve runs on one deal of each type plus the renovation deal; targets keep
# the solve inside a bracket where cash-on-cash falls with price (deal c's
# stabilized-year basis falls with price over the whole $1M-$100M bracket).
BACKSOLVE = {
    "a_stabilized_1990s": {"strategy": "cashflow", "target_coc": "0.06", "max_price": "40000000.00"},
    "c_value_add_1970s_reno": {"strategy": "value_add", "target_coc": "0.06", "max_price": "100000000.00"},
    "d_value_add_1980s_msa_only": {"strategy": "value_add", "target_coc": "0.03", "max_price": "20000000.00"},
}
TOL = 1e-9


@lru_cache(maxsize=None)
def _ingested(slug: str) -> str:
    return json.dumps(dl.ingest(dl.load_deal(slug)))


@lru_cache(maxsize=None)
def _results(slug: str) -> str:
    return json.dumps(run_underwriting(json.loads(_ingested(slug))))


def _deal(slug):
    return dl.load_deal(slug)


def _canonical(slug):
    return json.loads(_ingested(slug))


def _entries(canonical):
    return canonical["assumptions"]["entries"]


def _in(band, value, label):
    lo, hi = band
    assert lo - TOL <= value <= hi + TOL, f"{label} = {value} outside [{lo}, {hi}]"


def _parts(value, first, second):
    """(start, target) of a curve entry value: a parameter dict, or a flat scalar."""
    if isinstance(value, dict):
        return value[first], value[second]
    return value, value


# ── the library itself ───────────────────────────────────────────────────


def test_library_covers_the_adr_matrix():
    deals = [_deal(slug) for slug in SLUGS]
    types = [d["deal_profile"]["deal_type"] for d in deals]
    assert len(deals) >= 4
    assert types.count("stabilized") >= 2 and types.count("value_add") >= 2
    buckets = {house.vintage_bucket(d["deal_profile"]["year_built"]) for d in deals}
    assert len(buckets) == len(deals)  # one vintage per deal
    levels = [{o["level"] for o in (d["market_data"] or {}).get("observations", [])} for d in deals]
    assert set() in levels  # a deal with no market data (house fallback path)
    assert {"msa"} in levels  # a deal with MSA data only
    assert any(d["analyst_inputs"].get("renovation_programs") for d in deals)
    assert all(d["synthetic"] is True for d in deals)


def test_fixtures_match_the_committed_generator(monkeypatch):
    name = "deal_library_build_fixtures"
    spec = importlib.util.spec_from_file_location(name, dl.DEALS_DIR / "build_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)  # dataclasses resolve their module
    spec.loader.exec_module(module)
    rendered = module.render_all()
    assert {path.parent.name for path in rendered} == set(SLUGS)
    for path, text in rendered.items():
        assert path.read_text(encoding="utf-8") == text, (
            f"{path.name} of {path.parent.name} differs from build_fixtures.py output; edit the "
            "generator and rerun it instead of editing fixtures by hand"
        )
    assert module.render_all() == rendered  # deterministic


@pytest.mark.parametrize("slug", SLUGS)
def test_documents_are_mutually_consistent(slug):
    deal = _deal(slug)
    docs = deal["expected"]["documents"]
    gate = _canonical(slug)["metadata"]["ingest_gate"]
    evidence = gate["evidence"]

    # T12 GPR ~ rent-roll units x market rent x 12; opex a realistic share of EGI.
    assert docs["t12_gpr_annual"] == pytest.approx(docs["rent_roll_gpr_annual"], rel=0.02)
    assert 0.35 <= docs["t12_opex_to_egi"] <= 0.50
    assert evidence["t12"]["gross_potential_rent"] == pytest.approx(docs["t12_gpr_annual"])
    assert gate["t12_reconciliation"]["income"]["statement_total"] == pytest.approx(docs["t12_total_income"])
    assert gate["t12_reconciliation"]["opex"]["statement_total"] == pytest.approx(docs["t12_total_opex"])

    # The rent roll as ingest reads it.
    canonical = _canonical(slug)
    assert sum_units(canonical) == docs["units"]
    assert evidence["rent_roll"]["physical_vacancy_rate"] == pytest.approx(docs["rent_roll_physical_vacancy"], abs=1e-4)
    assert evidence["rent_roll"]["loss_to_lease_rate"] == pytest.approx(docs["rent_roll_loss_to_lease"], abs=1e-5)

    # Every T12 opex line lands in the category the generator meant (GL collisions
    # are summed, repair lines with utility words stay in R&M).
    parsed = {name: line["annual_amount"] for name, line in evidence["t12"]["opex_lines"].items()}
    assert parsed == pytest.approx(docs["t12_opex_by_category"])


# ── ingest ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("slug", SLUGS)
def test_ingest_gate_passes_and_the_deal_validates(slug):
    canonical = _canonical(slug)
    gate = canonical["metadata"]["ingest_gate"]
    assert gate["status"] == _deal(slug)["expected"]["ingest_gate"] == "PASS"
    assert gate["blockers"] == []
    assert gate["t12_reconciliation"]["status"] == "PASS"
    errors = [i for i in validate_deal(canonical).issues if i.severity == "ERROR"]
    assert errors == []


@pytest.mark.parametrize("slug", SLUGS)
def test_every_entry_has_a_source_and_the_matching_review_flag(slug):
    deal, canonical = _deal(slug), _canonical(slug)
    entries = _entries(canonical)
    assert canonical["deal_profile"]["deal_type"] == deal["deal_profile"]["deal_type"]
    assert canonical["deal_profile"]["year_built"] == deal["deal_profile"]["year_built"]
    for key, entry in entries.items():
        assert is_valid_source(entry["source"]), key
        assert entry["basis"], key
        assert entry["candidates"], key
        # House defaults and engine fallbacks are review prompts; deal, market and analyst values are not.
        assert entry["review_required"] is requires_review(entry["source"]), key
        if entry["override"] is None:
            assert entry["value"] == entry["default_value"], key
    for key, source in deal["expected"]["sources"].items():
        assert entries[key]["source"] == source, key
    market_sourced = {k for k, e in entries.items() if e["source"].startswith("market:")}
    if deal["market_data"] is None:
        assert market_sourced == set()
    else:
        assert {"rent_growth", "physical_vacancy", "exit_cap_rate"} <= market_sourced
        assert all(entries[k]["as_of"] == deal["market_data"]["as_of"] for k in market_sourced)


@pytest.mark.parametrize("slug", SLUGS)
def test_market_data_comes_from_the_right_geography(slug):
    deal, entries = _deal(slug), _entries(_canonical(slug))
    if deal["market_data"] is None:
        assert "No market data was used" in entries["rent_growth"]["basis"]
        return
    levels = {o["metric"]: o["level"] for o in deal["market_data"]["observations"] if o["level"] == "submarket"}
    expected_level = "submarket" if "rent_growth_forecast" in levels else "MSA"
    selected = [c for c in entries["rent_growth"]["candidates"]
                if c.get("metric") == "rent_growth_forecast" and c["basis"].endswith("(selected).")]
    assert len(selected) == 1 and f"at {expected_level} level" in selected[0]["basis"]
    if expected_level == "MSA":  # the profile names a submarket with no data: MSA fallback
        assert deal["deal_profile"]["submarket"]
        assert "submarket" not in json.dumps(selected[0]["geo"])


# ── expected bands ───────────────────────────────────────────────────────


@pytest.mark.parametrize("slug", SLUGS)
def test_resolved_assumptions_fall_in_the_expected_bands(slug):
    deal, canonical = _deal(slug), _canonical(slug)
    bands, entries = deal["expected"]["bands"], _entries(canonical)

    growth = entries["rent_growth"]["value"]
    assert len(growth) == len(bands["rent_growth"]) == 4  # increases entering years 2-5
    for year, (band, rate) in enumerate(zip(bands["rent_growth"], growth), start=2):
        _in(band, rate, f"rent growth entering year {year}")
    assert canonical["growth_assumptions"]["annual_growth_rates"] == growth

    start, stabilized = _parts(entries["physical_vacancy"]["value"], "start", "stabilized")
    _in(bands["vacancy_start"], start, "vacancy start")
    _in(bands["vacancy_stabilized"], stabilized, "vacancy stabilized")
    rates = [row["vacancy_rate"] for row in canonical["physical_vacancy_curve"]]
    assert min(rates) == pytest.approx(min(start, stabilized)) and max(rates) == pytest.approx(max(start, stabilized))

    ltl_start, ltl_floor = _parts(entries["loss_to_lease"]["value"], "start", "floor")
    _in(bands["loss_to_lease_start"], ltl_start, "loss to lease start")
    _in(bands["loss_to_lease_floor"], ltl_floor, "loss to lease floor")

    year1, stabilized_concessions = _parts(entries["concessions"]["value"], "year1", "stabilized")
    _in(bands["concessions_year1"], year1, "year-1 concessions")
    _in(bands["concessions_stabilized"], stabilized_concessions, "stabilized concessions")
    assert max(float(concession_pct_rent(r)) for r in canonical["concession_schedule"]) == pytest.approx(
        max(year1, stabilized_concessions))

    _in(bands["collection_loss"], entries["collection_loss"]["value"], "collection loss")
    assert {r["loss_rate"] for r in canonical["collection_loss_curve"]} == {entries["collection_loss"]["value"]}
    _in(bands["exit_cap_rate"], entries["exit_cap_rate"]["value"], "exit cap rate")
    assert canonical["exit_assumptions"]["exit_cap_rate"] == entries["exit_cap_rate"]["value"]
    _in(bands["insurance_per_unit"], entries["insurance_per_unit"]["value"], "insurance per unit")
    _in(bands["r_and_m_per_unit"], entries["r_and_m_per_unit"]["value"], "R&M per unit")
    # The house vintage reserve is written when the documents carry none.
    reserve = entries["replacement_reserve_per_unit"]["value"]
    _in(bands["replacement_reserve_per_unit"], reserve, "reserve per unit")
    assert canonical["replacement_reserves"][0]["annual_amount"] == pytest.approx(reserve * sum_units(canonical))
    closing = canonical["purchase_assumptions"]
    assert closing["closing_costs"] == pytest.approx(
        closing["purchase_price"] * float(house.DEFAULT_PURCHASE_CLOSING_COST_PCT), abs=0.01)


@pytest.mark.parametrize("slug", SLUGS)
def test_opex_growth_per_category_falls_in_the_expected_bands(slug):
    deal, canonical = _deal(slug), _canonical(slug)
    bands = deal["expected"]["opex_growth"]
    growth = {k: e for k, e in _entries(canonical).items() if k.startswith("opex.") and k.endswith(".growth")}
    categories = {e["detail"]["category_name"]: e for e in growth.values()}
    assert set(categories) == set(bands)  # every opex line has a band, and no line is missing
    rows = {row["category_name"]: row for row in canonical["opex_table"]}
    for name, entry in categories.items():
        _in(bands[name], entry["value"], f"{name} growth")
        assert rows[name]["growth_rate"] == entry["value"]
        if name == "Real Estate Taxes":  # 3% a year after the purchase reassessment
            assert entry["source"] == "house:opex_growth_table"
            assert entry["value"] == float(house.HOUSE_RE_TAX_GROWTH_AFTER_REASSESSMENT)
    # The T12 management fee is entered as a percent of EGR (T12 fee / T12 EGI).
    fee = rows["Property Management Fee"]
    assert fee["calculation_type"] == "percent_egr" and fee["growth_rate"] == 0.0


# ── engine run ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("slug", SLUGS)
def test_engine_run_succeeds_within_sanity_bands(slug):
    deal, results = _deal(slug), json.loads(_results(slug))
    bands = deal["expected"]["results"]
    noi = [year["net_operating_income"] for year in results["cashflow"]["by_year"]]
    assert noi and all(value > 0 for value in noi)
    metrics = results["metrics"]
    _in(bands["unlevered_irr"], metrics["irr"]["unlevered_irr"], "unlevered IRR")
    _in(bands["levered_irr"], metrics["irr"]["levered_irr"], "levered IRR")
    _in(bands["minimum_dscr"], metrics["dscr"]["minimum_dscr"], "minimum DSCR")
    _in(bands["going_in_cap_rate"], metrics["yields"]["going_in_cap_rate"], "going-in cap rate")
    exit_cap = _entries(_canonical(slug))["exit_cap_rate"]["value"]
    assert metrics["yields"]["exit_cap_rate"] == pytest.approx(exit_cap, abs=5e-5)  # the engine rounds to 4 dp
    programs = deal["analyst_inputs"].get("renovation_programs") or []
    renovated = sum(month["units_renovated"] for month in (results.get("renovations") or {}).get("by_month", []))
    assert renovated == sum(p["max_units"] for p in programs)


def test_the_renovation_premium_comes_from_market_data():
    deals = [_deal(slug) for slug in SLUGS if _deal(slug)["analyst_inputs"].get("renovation_programs")]
    assert deals
    for deal in deals:
        premium = next(o["value"] for o in deal["market_data"]["observations"] if o["metric"] == "renovation_premium")
        registry = dl.synthetic_registry(deal)
        observation, _ = registry.resolve("renovation_premium", dl.deal_geo(deal, "submarket"))
        assert observation.scalar == premium
        assert {p["rent_premium_monthly"] for p in deal["analyst_inputs"]["renovation_programs"]} == {premium}


# ── guardrails ───────────────────────────────────────────────────────────


def _below_floor_keys(canonical, deal):
    """Which keys sit below their house floor, computed straight from the vintage policy."""
    year = deal["deal_profile"]["year_built"]
    strategy = DEAL_TYPE_STRATEGY[deal["deal_profile"]["deal_type"]]
    units = Decimal(sum_units(canonical))
    d = lambda value: Decimal(str(value))  # noqa: E731
    below = set()
    if any(d(r["vacancy_rate"]) < house.default_physical_vacancy_rate(year, strategy=strategy)
           for r in canonical["physical_vacancy_curve"]):
        below.add("physical_vacancy")
    # Loss to lease: cohorts with a market vs in-place gap only; such a cohort
    # with no row reads as 0%.
    market = {}
    for r in canonical["market_rent_curve"]:
        market.setdefault(r["cohort_id"], d(r["market_rent"]))
    gap = {c["cohort_id"] for c in canonical["unit_cohorts"]
           if market.get(c["cohort_id"], d(0)) > d(c["initial_inplace_rent"])}
    ltl_cohorts = {r["cohort_id"] for r in canonical["loss_to_lease"]}
    if any(r["cohort_id"] in gap and d(r["ltl_percent"]) < house.default_loss_to_lease_rate(year, strategy=strategy)
           for r in canonical["loss_to_lease"]) or gap - ltl_cohorts:
        below.add("loss_to_lease")
    if any(d(r["loss_rate"]) < house.default_collection_loss_rate(year, strategy=strategy)
           for r in canonical.get("collection_loss_curve") or [{"loss_rate": 0}]):
        below.add("collection_loss")
    # Concessions: each row that applies in analysis year 1 (none reads as 0%).
    start = canonical["time_grid"]["analysis_start_date"][:7]
    index = int(start[:4]) * 12 + int(start[5:7]) + 10
    year1_end = f"{index // 12:04d}-{index % 12 + 1:02d}"
    year1 = [concession_pct_rent(r) for r in canonical.get("concession_schedule") or []
             if r["start_month"] <= year1_end and r["end_month"] >= start] or [Decimal(0)]
    if min(year1) < house.default_year1_concessions_rate(year, strategy=strategy):
        below.add("concessions")
    insurance = matching_opex_rows(canonical, "insurance")
    if annual_opex_amount(canonical, insurance[0]) / units < house.DEFAULT_YEAR1_INSURANCE_PER_UNIT:
        below.add("insurance_per_unit")
    if sum_opex_bucket(canonical, "repairs_maintenance") / units < house.default_r_and_m_per_unit(
            year, strategy=strategy):
        below.add("r_and_m_per_unit")
    reserves = [d(r["annual_amount"]) for r in canonical.get("replacement_reserves") or []] or [Decimal(0)]
    if min(reserves) / units < house.default_replacement_reserve_per_unit(year, strategy=strategy):
        below.add("replacement_reserve_per_unit")
    return below


@pytest.mark.parametrize("slug", SLUGS)
def test_underwrite_guardrails_warn_exactly_where_a_value_is_below_its_floor(slug):
    deal, canonical = _deal(slug), _canonical(slug)
    entries = _entries(canonical)
    assert canonical["assumptions"]["mode"] == "underwrite"
    warned = {k for k, e in entries.items() if e.get("guardrail")}
    assert all(entries[k]["guardrail"]["action"] == "warned" for k in warned)
    assert all(entries[k]["override"] is None or entries[k]["source"] == "analyst" for k in warned)
    assert warned <= set(GUARDRAIL_KEYS)
    assert warned == _below_floor_keys(canonical, deal) == set(deal["expected"]["guardrail_warnings"])
    report = validate_deal(canonical)
    flagged = {i.path for i in report.issues if i.code == "GUARDRAIL_BELOW_FLOOR"}
    assert all(i.severity == "WARNING" for i in report.issues if i.code == "GUARDRAIL_BELOW_FLOOR")
    assert flagged == {f"/assumptions/entries/{k}/guardrail" for k in warned}


# ── backsolve ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("slug", sorted(BACKSOLVE))
def test_backsolve_enforces_floors_and_its_case_is_stable_on_re_resolve(slug):
    canonical, config = _canonical(slug), BACKSOLVE[slug]
    deal_type = _deal(slug)["deal_profile"]["deal_type"]
    assert DEAL_TYPE_STRATEGY[deal_type] == config["strategy"]
    result = backsolve_price(
        copy.deepcopy(canonical), target_coc=config["target_coc"],
        policy={"version": "plat.backsolve-policy/1", "strategy": config["strategy"]},
        benchmark=BENCHMARK, min_price="1000000.00", max_price=config["max_price"],
    )
    assert result["status"] == "converged"
    case = result["case"]
    entries = _entries(case)
    assert case["assumptions"]["mode"] == "pricing"
    assert case["deal_profile"]["deal_type"] == deal_type

    # Every floor the solve enforced is recorded on its entry and in policy_overrides.
    enforced = {k for k, e in entries.items() if (e.get("guardrail") or {}).get("action") == "enforced"}
    summary = result["summary"]["policy_summary"]
    # The underwrite already carries the house vintage reserve, so pricing has none to enforce.
    assert "replacement_reserve_per_unit" not in enforced
    assert all(_entries(canonical)[k]["guardrail"]["action"] == "warned" for k in enforced)
    assert {f["key"] for f in summary["guardrails"]["findings"]} == enforced
    assert {o["assumption"] for o in summary["policy_overrides"] if "assumption" in o} == enforced
    for key in enforced:
        record = entries[key]["guardrail"]
        assert record["applied"] >= record["original"]
        assert entries[key]["override"]["value"] == entries[key]["value"]
    reserve = entries["replacement_reserve_per_unit"]["value"]
    assert case["replacement_reserves"][0]["annual_amount"] == pytest.approx(reserve * sum_units(case))

    # The solved canonical's assumptions are idempotent on re-resolve.
    assert resolve_assumptions(case, mode="pricing") == case
    for key, entry in entries.items():
        if entry["override"] is None:
            assert entry["value"] == entry["default_value"], key
    assert not [i for i in validate_deal(case).issues if i.severity == "ERROR"]


VALUE_ADD_RENO = "c_value_add_1970s_reno"
WIDE = {"min_price": "1000000.00", "max_price": "100000000.00"}


@lru_cache(maxsize=None)
def _deal_c_solve(coc_basis=None) -> str:
    policy = {"version": "plat.backsolve-policy/1", "strategy": "value_add"}
    if coc_basis:
        policy["coc_basis"] = coc_basis
    result = backsolve_price(_canonical(VALUE_ADD_RENO), target_coc="0.06", policy=policy,
                             benchmark=BENCHMARK, **WIDE)
    return json.dumps(result)


def test_value_add_renovation_deal_converges_over_a_wide_bracket_on_its_stabilized_year():
    """UPL-62 decision (b): deal c is priced on the post-debt CoC of its first
    stabilized year, with the renovation budget funded by equity at close, so
    CoC falls with price and the solve converges over $1M-$100M."""
    result = json.loads(_deal_c_solve())
    assert result["status"] == "converged"
    summary = result["summary"]
    assert summary["bracket"]["min_price"] == "1000000.00" and summary["bracket"]["max_price"] == "100000000.00"
    assert Decimal(summary["bracket"]["min_price_coc"]) > Decimal("0.06") > Decimal(summary["bracket"]["max_price_coc"])
    assert 1_000_000 < summary["solved_purchase_price"] < 100_000_000

    # The basis is reported: analysis year 3 (2028-11 to 2029-10), the first full
    # year after the last renovation (2028-09), by default for value_add.
    basis = summary["coc_basis"]
    assert basis["basis"] == "stabilized_year"
    assert basis["source"] == "default for the value_add strategy"
    assert (basis["analysis_year"], basis["months"], basis["rule"], basis["last_renovation_month"]) == (
        3, ["2028-11", "2029-10"], "first_full_year_after_renovations", "2028-09")
    assert summary["cash_on_cash_persisted"] == pytest.approx(basis["free_cf"] / basis["equity_basis"], abs=1e-6)
    assert summary["cash_on_cash_persisted"] >= 0.06
    assert summary["maximum_feasible_boundary"]["next_infeasible_coc"] < 0.06
    assert result["effective_assumptions"]["policy"]["coc_basis"] == "stabilized_year"


def test_value_add_renovation_budget_is_equity_at_close_and_out_of_cash_flow():
    result = json.loads(_deal_c_solve())
    case, results, summary = result["case"], result["results"], result["summary"]
    programs = _deal(VALUE_ADD_RENO)["analyst_inputs"]["renovation_programs"]
    budget = sum(p["max_units"] * p["renovation_cost_per_unit"] for p in programs)
    assert budget == 1_115_000

    funding = summary["renovation_funding"]
    assert funding["renovation_capex_funding"] == case["renovation_capex_funding"] == "equity_at_close"
    assert funding["equity_funded_renovation_budget"] == budget
    assert funding["included_in_total_equity_basis"] and funding["excluded_from_operating_cash_flow"]

    # The equity basis (sources) includes the budget.
    purchase, fund = case["purchase_assumptions"], case["fund_assumptions"]
    price = purchase["purchase_price"]
    expected_equity = (purchase["equity_contribution"] + purchase["closing_costs"]
                       + price * fund["acquisition_fee_pct"] + fund["partnership_closing_costs"] + budget)
    assert purchase["total_equity_basis"] == pytest.approx(expected_equity, abs=0.01)
    assert summary["solved_total_equity_basis"] == purchase["total_equity_basis"]
    assert summary["coc_basis"]["equity_basis"] == pytest.approx(purchase["total_equity_basis"], abs=0.01)
    assert purchase["total_unlevered_basis"] == pytest.approx(price + purchase["closing_costs"] + budget, abs=0.01)

    # Cash flow excludes the funded CapEx; NOI and the renovation schedule are unchanged.
    months = results["cashflow"]["by_month"]
    assert sum(m["total_capex"] for m in months) == 0
    assert sum(m.get("equity_funded_capex", 0) for m in months) == pytest.approx(budget)
    assert results["capex_funding"]["equity_funded_renovation_budget"] == pytest.approx(budget)
    assert results["renovations"]["summary"]["total_renovation_cost"] == budget
    assert results["metrics"]["coc"]["components"]["equity_basis"] == pytest.approx(
        purchase["total_equity_basis"], abs=0.01)


def test_value_add_coc_basis_is_overridable_and_disclosed():
    stabilized = json.loads(_deal_c_solve())["summary"]
    year1 = json.loads(_deal_c_solve("year_1"))
    assert year1["status"] == "converged"
    basis = year1["summary"]["coc_basis"]
    assert (basis["basis"], basis["source"], basis["analysis_year"]) == ("year_1", "explicit policy value", 1)
    # The budget is still funded at close; year 1 carries the renovation downtime,
    # so the same 6% target supports a lower price than the stabilized year.
    assert year1["summary"]["renovation_funding"]["equity_funded_renovation_budget"] == 1_115_000
    assert year1["summary"]["solved_purchase_price"] < stabilized["solved_purchase_price"]


def test_stabilized_deals_keep_the_year1_basis_and_their_price():
    slug, config = "a_stabilized_1990s", BACKSOLVE["a_stabilized_1990s"]
    results = {}
    for basis in (None, "year_1"):
        policy = {"version": "plat.backsolve-policy/1", "strategy": "cashflow"}
        if basis:
            policy["coc_basis"] = basis
        results[basis] = backsolve_price(_canonical(slug), target_coc=config["target_coc"], policy=policy,
                                         benchmark=BENCHMARK, min_price="1000000.00",
                                         max_price=config["max_price"])
    default, explicit = results[None], results["year_1"]
    assert default["summary"]["coc_basis"]["basis"] == "year_1"
    assert default["summary"]["solved_purchase_price"] == explicit["summary"]["solved_purchase_price"]
    assert default["summary"]["cash_on_cash_persisted"] == default["summary"]["cash_on_cash_year_1_persisted"]
    assert "renovation_capex_funding" not in default["case"]
    assert default["summary"]["renovation_funding"]["equity_funded_renovation_budget"] == 0.0
    assert "capex_funding" not in default["results"]


# ── assumptions sheet ────────────────────────────────────────────────────


@pytest.mark.parametrize("slug", SLUGS)
def test_sheet_override_round_trip_moves_the_result_the_expected_way(slug, tmp_path):
    canonical = _canonical(slug)
    baseline = json.loads(_results(slug))
    entries = _entries(canonical)
    canonical_path = tmp_path / "deal.json"
    canonical_path.write_text(json.dumps(canonical), encoding="utf-8")
    assert assumptions_sheet.main(["export", str(canonical_path)]) == 0
    sheet_path = tmp_path / "deal.assumptions.json"
    sheet = json.loads(sheet_path.read_text(encoding="utf-8"))

    # Edit one scalar (collection loss up 1pt) and one vector (rent growth down 1pt a year).
    rows = {row["key"]: row for row in sheet["rows"]}
    collection = round(entries["collection_loss"]["value"] + 0.01, 6)
    growth = [round(rate - 0.01, 6) for rate in entries["rent_growth"]["value"]]
    rows["collection_loss"]["override_value"] = collection
    rows["collection_loss"]["override_note"] = "stress collections"
    rows["rent_growth"]["override_value"] = growth
    sheet_path.write_text(json.dumps(sheet), encoding="utf-8")

    out_path = tmp_path / "edited.json"
    assert assumptions_sheet.main(["apply", str(canonical_path), str(sheet_path), "-o", str(out_path)]) == 0
    edited = json.loads(out_path.read_text(encoding="utf-8"))
    after = _entries(edited)
    for key, value in (("collection_loss", collection), ("rent_growth", growth)):
        assert after[key]["value"] == value
        assert after[key]["source"] == "analyst"
        assert after[key]["override"]["value"] == value
        assert after[key]["default_value"] == entries[key]["default_value"]  # the default is kept
    assert after["collection_loss"]["override"]["note"] == "stress collections"
    assert {r["loss_rate"] for r in edited["collection_loss_curve"]} == {collection}
    assert edited["growth_assumptions"]["annual_growth_rates"] == growth
    # Nothing else moved.
    untouched = set(entries) - {"collection_loss", "rent_growth"}
    assert {k: after[k]["value"] for k in untouched} == {k: entries[k]["value"] for k in untouched}

    results = run_underwriting(edited)
    year1 = lambda r: r["cashflow"]["by_year"][1]["effective_gross_income"]  # noqa: E731 - first full year
    assert year1(results) < year1(baseline)
    assert results["metrics"]["irr"]["unlevered_irr"] < baseline["metrics"]["irr"]["unlevered_irr"]
    assert results["metrics"]["irr"]["levered_irr"] < baseline["metrics"]["irr"]["levered_irr"]


CLEARED_KEYS = ("exit_cap_rate", "rent_growth")
RESTORED_FIELDS = ("value", "source", "as_of", "confidence", "default_value", "override", "review_required")


def _assert_restored(before, after):
    for key in CLEARED_KEYS:
        for field in RESTORED_FIELDS:
            assert _entries(after)[key][field] == _entries(before)[key][field], (key, field)
    assert after["exit_assumptions"]["exit_cap_rate"] == before["exit_assumptions"]["exit_cap_rate"]
    assert after["growth_assumptions"] == before["growth_assumptions"]


@pytest.mark.parametrize("registry", ["none", "empty", "deal"])
def test_clearing_an_override_restores_the_market_default(registry):
    slug = "a_stabilized_1990s"
    before = _canonical(slug)
    assert all(_entries(before)[k]["source"] == "market:synthetic" for k in CLEARED_KEYS)
    overridden = resolve_assumptions(before, overrides={"exit_cap_rate": 0.06, "rent_growth": 0.02})
    assert all(_entries(overridden)[k]["source"] == "analyst" for k in CLEARED_KEYS)
    # Re-resolving while the override stands keeps the market default as the default.
    kept = resolve_assumptions(overridden)
    for key in CLEARED_KEYS:
        assert _entries(kept)[key]["default_value"] == _entries(before)[key]["default_value"], key
        assert "from market:synthetic" in _entries(kept)[key]["basis"], key

    # No registry, one without data for the deal (e.g. no feeder files), or the deal's own.
    market = {
        "none": None,
        "empty": MarketDataRegistry([SyntheticProvider({})]),
        "deal": dl.synthetic_registry(_deal(slug)),
    }[registry]
    for start in (overridden, kept):
        cleared = resolve_assumptions(start, overrides={k: None for k in CLEARED_KEYS}, market=market)
        _assert_restored(before, cleared)
        # Stable: a later re-resolution without market data changes nothing.
        _assert_restored(before, resolve_assumptions(cleared))


@pytest.mark.parametrize("flags", [["--no-market-data"], []], ids=["no_market_data", "default_registry"])
def test_sheet_apply_clear_restores_the_market_default(flags, tmp_path, monkeypatch):
    import engine.market_data

    slug = "a_stabilized_1990s"
    before = _canonical(slug)
    calls = []

    def registry(*_args, **_kwargs):
        calls.append(1)
        return dl.synthetic_registry(_deal(slug))

    monkeypatch.setattr(engine.market_data, "default_registry", registry)
    paths = {name: tmp_path / f"{name}.json" for name in ("deal", "set", "overridden", "clear", "cleared")}
    paths["deal"].write_text(json.dumps(before), encoding="utf-8")
    paths["set"].write_text(json.dumps({"exit_cap_rate": 0.06, "rent_growth": 0.02}), encoding="utf-8")
    paths["clear"].write_text(json.dumps({k: None for k in CLEARED_KEYS}), encoding="utf-8")
    apply = lambda src, sheet, out: assumptions_sheet.main(  # noqa: E731
        ["apply", str(paths[src]), str(paths[sheet]), "-o", str(paths[out]), *flags]
    )
    assert apply("deal", "set", "overridden") == 0
    assert _entries(json.loads(paths["overridden"].read_text()))["exit_cap_rate"]["value"] == 0.06
    assert apply("overridden", "clear", "cleared") == 0
    _assert_restored(before, json.loads(paths["cleared"].read_text()))
    assert len(calls) == (0 if flags else 2)  # the registry is the default, as in ingest


# ── determinism and goldens ──────────────────────────────────────────────


@pytest.mark.parametrize("slug", SLUGS)
def test_two_runs_write_byte_identical_assumptions(slug, tmp_path):
    deal = _deal(slug)
    first, second = dl.ingest(deal), dl.ingest(_deal(slug))
    assert first == second
    paths = [write_assumptions_json(c, tmp_path / f"run{i}.assumptions.json") for i, c in enumerate((first, second))]
    assert paths[0].read_bytes() == paths[1].read_bytes()
    assert json.dumps(assumptions_document(first)) == json.dumps(assumptions_document(second))
    assert run_underwriting(first) == run_underwriting(second)


@pytest.mark.parametrize("slug", SLUGS)
def test_assumptions_match_the_golden_snapshot(slug):
    path = dl.golden_path(slug)
    assert path.is_file(), f"missing {path.name} for {slug}; run `{dl.REGENERATE_HINT}`"
    expected = path.read_text(encoding="utf-8")
    actual = dl.golden_text(_canonical(slug))
    assert actual == expected, (
        f"{slug}: the resolved assumptions differ from {path.name}. If the change is intended, "
        f"run `{dl.REGENERATE_HINT}` and review the diff."
    )
