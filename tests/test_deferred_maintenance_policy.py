"""Deferred maintenance policy (ADR 0003 §3, §5).

When ``deal_profile.deferred_maintenance`` is flagged:

(a) the house floors and defaults for physical vacancy, collection loss,
    year-1 concessions and R&M move to the upper part of the vintage bands
    (never less than a named step over the floor the deal would otherwise
    carry), so they scale with vintage;
(b) with no PCA and no analyst figure, immediate capex at close defaults to a
    placeholder of one year of the vintage economic CapEx band high end,
    funded with equity at close, flagged for review with a PCA warning;
(c) a PCA recurring capex below the vintage band is warned in underwrite and
    raised to the band floor in pricing (disclosed as
    ``floor_applied_over_deal_evidence``); an analyst figure below it keeps
    the waiver convention.

All deals and figures are synthetic.
"""
from __future__ import annotations

import copy
import json
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

import pytest

from engine import underwriting_policy as house
from engine.assumptions import resolve_assumptions
from engine.assumptions import guardrails
from engine.assumptions.guardrails import GuardrailProfile, house_floors
from engine.assumptions.rationale import rationale_for
from engine.assumptions.register import build_register
from engine.backsolve import backsolve_price
from engine.ingest.ingestion_pipeline import build_deal_from_documents
from engine.validator import validate_deal
from tests import deal_library as dl

FIXTURES = Path(__file__).resolve().parent / "fixtures"
T12 = FIXTURES / "entrata_style_t12.csv"
ROLL = FIXTURES / "sample_rent_roll.csv"
YEAR = 1984  # 1980s bucket
BUCKETS = tuple(house.VINTAGE_FRAMEWORK_2026)
BENCHMARK = {"rate": "0.04", "as_of": "2026-10-01", "source": "synthetic:deferred-maintenance"}


def _ingest(**kwargs):
    params = dict(property_id="Synthetic Deferred", rent_roll_path=ROLL, t12_path=T12,
                  analysis_start="2026-08", analysis_end="2031-07")
    params.update(kwargs)
    return build_deal_from_documents(**params)


def _entries(canonical):
    return canonical["assumptions"]["entries"]


def _year(bucket):
    return house.VINTAGE_FRAMEWORK_2026[bucket]["year_min"] + 4


def _mid(row, name):
    return (row[f"{name}_typical_low"] + row[f"{name}_typical_high"]) / Decimal("2")


def _issues(canonical, code):
    return [i for i in validate_deal(canonical).issues if i.code == code]


# ── (a) the house minimums move with the flag ────────────────────────────


def test_1980s_deferred_minimums_are_the_upper_part_of_the_vintage_bands():
    # Stabilized 1980s without the flag: vacancy 5.0%, collection 0.75%,
    # year-1 concessions 0.75%, R&M $900 (the low end of $900-$1,700).
    assert house.default_physical_vacancy_rate(YEAR) == Decimal("0.050")
    assert house.default_physical_vacancy_rate(YEAR, deferred=True) == Decimal("0.070")  # typical high
    assert house.default_collection_loss_rate(YEAR, deferred=True) == Decimal("0.0225")  # typical midpoint
    assert house.default_year1_concessions_rate(YEAR, deferred=True) == Decimal("0.015")  # typical midpoint
    assert house.default_r_and_m_per_unit(YEAR, deferred=True) == Decimal("1700")  # band high end
    # The house default collection loss (no T12 bad debt) is never below the deferred floor.
    assert house.default_bad_debt_rate(YEAR) == Decimal("0.010")
    assert house.default_bad_debt_rate(YEAR, deferred=True) == Decimal("0.0225")
    # Unknown vintage: no band, so nothing moves.
    assert house.default_physical_vacancy_rate(None, deferred=True) is None
    assert house.default_bad_debt_rate(None, deferred=True) == house.HOUSE_DEFAULT_COLLECTION_LOSS


@pytest.mark.parametrize("bucket", BUCKETS)
@pytest.mark.parametrize("strategy", ["cashflow", "value_add"])
def test_deferred_minimums_scale_with_vintage_and_always_raise(bucket, strategy):
    year, row = _year(bucket), house.VINTAGE_FRAMEWORK_2026[bucket]
    cases = (
        (house.default_physical_vacancy_rate, row["physical_vacancy_typical_high"],
         house.DEFERRED_MAINTENANCE_VACANCY_STEP),
        (house.default_collection_loss_rate, _mid(row, "bad_debt"),
         house.DEFERRED_MAINTENANCE_COLLECTION_LOSS_STEP),
        (house.default_year1_concessions_rate, _mid(row, "concessions"),
         house.DEFERRED_MAINTENANCE_CONCESSIONS_STEP),
    )
    for fn, band_point, step in cases:
        base = fn(year, strategy=strategy)
        deferred = fn(year, strategy=strategy, deferred=True)
        assert deferred == max(band_point, base + step), fn.__name__
        assert deferred >= base + step > base
    assert house.default_r_and_m_per_unit(year, strategy=strategy, deferred=True) == row["r_and_m_high"]


@pytest.mark.parametrize("bucket", BUCKETS)
def test_deferred_economic_vacancy_lands_in_the_vintage_typical_band(bucket):
    """Physical vacancy + collection loss + year-1 concessions + loss to lease
    under the deferred minimums sits inside the vintage economic-vacancy
    typical band, and above the 5% agency minimum economic vacancy."""
    year, row = _year(bucket), house.VINTAGE_FRAMEWORK_2026[bucket]
    economic = house.deferred_maintenance_economic_vacancy(year)
    assert economic == (
        house.default_physical_vacancy_rate(year, deferred=True)
        + house.default_collection_loss_rate(year, deferred=True)
        + house.default_year1_concessions_rate(year, deferred=True)
        + house.default_loss_to_lease_rate(year)
    )
    assert row["economic_vacancy_typical_low"] <= economic <= row["economic_vacancy_typical_high"]
    assert economic >= house.AGENCY_MIN_ECONOMIC_VACANCY


def test_the_guardrail_profile_reads_the_deferred_maintenance_flag(minimal_deal_inputs):
    plain = GuardrailProfile("cashflow", YEAR)
    flagged = GuardrailProfile("cashflow", YEAR, deferred_maintenance=True)
    assert flagged.physical_vacancy() == Decimal("0.070") > plain.physical_vacancy()
    assert flagged.collection_loss() == Decimal("0.0225") > plain.collection_loss()
    assert flagged.year1_concessions() == Decimal("0.015") > plain.year1_concessions()
    assert flagged.r_and_m_per_unit() == Decimal("1700") > plain.r_and_m_per_unit()
    assert GuardrailProfile.coerce({"deal_type": "stabilized", "year_built": YEAR,
                                    "deferred_maintenance": True}) == flagged
    floors, _ = house_floors(copy.deepcopy(minimal_deal_inputs), flagged)
    assert floors["collection_loss"].value == Decimal("0.0225")
    assert "deferred maintenance" in floors["collection_loss"].basis
    assert floors["r_and_m_per_unit"].value == Decimal("1700")
    assert "deferred maintenance" in floors["r_and_m_per_unit"].basis


def test_the_resolver_uses_the_deferred_defaults_and_warns_below_the_deferred_floors():
    plain = _entries(_ingest(year_built=YEAR))
    deal = _ingest(year_built=YEAR, deferred_maintenance=True)
    entries = _entries(deal)
    # Stabilized vacancy: max(typical midpoint 5.75%, deferred floor 7.0%).
    assert plain["physical_vacancy"]["value"]["stabilized"] == 0.0575
    assert entries["physical_vacancy"]["value"]["stabilized"] == 0.07
    assert "deferred maintenance" in entries["physical_vacancy"]["basis"]
    # The T12 bad debt (1.5%) and concessions (1.0%) stay the defaults, but sit
    # below the deferred floors (2.25%, 1.5%): warned, never changed, in underwrite.
    for key, floor in (("collection_loss", 0.0225), ("concessions", 0.015)):
        assert entries[key]["source"] == "deal:t12"
        assert (entries[key]["guardrail"]["action"], entries[key]["guardrail"]["floor"]) == ("warned", floor)
        assert "guardrail" not in plain[key]
    assert {i.path for i in _issues(deal, "GUARDRAIL_BELOW_FLOOR")} >= {
        "/assumptions/entries/collection_loss/guardrail", "/assumptions/entries/concessions/guardrail"}
    assert resolve_assumptions(deal) == deal


def test_the_house_r_and_m_line_uses_the_deferred_band_point(minimal_deal_inputs):
    profile = {"deal_type": "stabilized", "year_built": YEAR, "deferred_maintenance": True}
    resolved = resolve_assumptions(copy.deepcopy(minimal_deal_inputs), deal_profile=profile)
    house_candidates = [c for c in _entries(resolved)["r_and_m_per_unit"]["candidates"]
                        if c["source"] == "house:vintage_1980s"]
    assert [c["value"] for c in house_candidates] == [1_700.0]
    assert "deferred maintenance" in house_candidates[0]["basis"]


# ── (b) immediate capex placeholder ──────────────────────────────────────


def test_deferred_maintenance_without_pca_or_analyst_budgets_a_placeholder_at_close():
    deal = _ingest(year_built=YEAR, deferred_maintenance=True)
    units = sum(c["unit_count"] for c in deal["unit_cohorts"])
    entry = _entries(deal)["immediate_capex"]
    expected = float(house.VINTAGE_FRAMEWORK_2026["1980s"]["economic_capex_high"]) * units  # one year, $2,500/unit
    assert (entry["source"], entry["value"], entry["review_required"]) == (
        "house:deferred_capex_placeholder", expected, True)
    assert deal["capital_plan"]["immediate_capex"] == {"amount": expected, "funding": "equity_at_close"}
    assert "PCA" in entry["basis"] and "125%" in entry["basis"]
    assert [c["source"] for c in entry["candidates"]] == ["house:deferred_capex_placeholder", "house:deferred_capex"]
    warnings = _issues(deal, "DEFERRED_CAPEX_PLACEHOLDER")
    assert [w.path for w in warnings] == ["/capital_plan/immediate_capex"]
    assert "125%" in warnings[0].message and "PCA" in warnings[0].message
    assert _issues(deal, "DEFERRED_CAPEX_UNBUDGETED") == []
    assert rationale_for("house:deferred_capex_placeholder")["known"]
    register = build_register(deal)
    assert register["capital_plan"]["buckets"][0]["source"] == "house:deferred_capex_placeholder"
    assert resolve_assumptions(deal) == deal


def test_without_the_flag_or_with_evidence_the_placeholder_is_not_used():
    plain = _entries(_ingest(year_built=YEAR))["immediate_capex"]
    assert (plain["source"], plain["value"]) == ("house:deferred_capex", 0.0)
    analyst = _entries(_ingest(year_built=YEAR, deferred_maintenance=True, immediate_capex=90_000))
    assert (analyst["immediate_capex"]["source"], analyst["immediate_capex"]["value"]) == ("analyst", 90_000.0)
    pca = _entries(_ingest(year_built=YEAR, deferred_maintenance=True, pca={"immediate_capex": 40_000}))
    assert (pca["immediate_capex"]["source"], pca["immediate_capex"]["value"]) == ("deal:pca", 40_000.0)
    # Unknown vintage: no band to size a placeholder, so $0 stays and is flagged.
    unknown = _ingest(deferred_maintenance=True)
    entry = _entries(unknown)["immediate_capex"]
    assert (entry["source"], entry["value"], entry["review_required"]) == ("house:deferred_capex", 0.0, True)
    assert _issues(unknown, "DEFERRED_CAPEX_UNBUDGETED")


def test_flagging_deferred_maintenance_later_replaces_the_house_zero(minimal_deal_inputs):
    deal = copy.deepcopy(minimal_deal_inputs)
    deal["metadata"]["year_built"] = YEAR
    first = resolve_assumptions(deal)
    assert _entries(first)["immediate_capex"]["value"] == 0.0
    flagged = resolve_assumptions(first, deal_profile={**first["deal_profile"], "deferred_maintenance": True})
    assert _entries(flagged)["immediate_capex"]["source"] == "house:deferred_capex_placeholder"
    assert flagged["capital_plan"]["immediate_capex"]["amount"] == 2_500.0 * 100


# ── (c) PCA recurring capex below the vintage band ───────────────────────


def _pca_deal(minimal_deal_inputs, per_unit=600.0):
    deal = copy.deepcopy(minimal_deal_inputs)
    deal["metadata"]["year_built"] = YEAR
    deal["capital_plan"] = {"pca": {"recurring_capex_per_unit": per_unit, "reference": "Synthetic PCA"}}
    return resolve_assumptions(deal)


def test_underwrite_keeps_a_pca_figure_below_the_band_and_warns(minimal_deal_inputs):
    deal = _pca_deal(minimal_deal_inputs)
    entry = _entries(deal)["recurring_capex_per_unit"]
    assert (entry["source"], entry["value"]) == ("deal:pca", 600.0)
    assert deal["capital_plan"]["recurring_capex"]["per_unit_annual"] == 600.0
    record = entry["guardrail"]
    assert (record["action"], record["floor"], record["original"]) == ("warned", 1_000.0, 600.0)
    assert record["evidence"].startswith("the PCA")
    assert "building systems" in record["basis"]
    assert [i.path for i in _issues(deal, "GUARDRAIL_BELOW_FLOOR")
            if "recurring_capex" in i.path] == ["/assumptions/entries/recurring_capex_per_unit/guardrail"]


def test_pricing_raises_a_pca_figure_to_the_band_floor_and_discloses_it(minimal_deal_inputs):
    deal = _pca_deal(minimal_deal_inputs)
    overrides: list = []
    findings = guardrails.enforce_floors(
        deal, mode="pricing", profile=GuardrailProfile("cashflow", YEAR),
        context={"policy_overrides": overrides}, keys=("recurring_capex_per_unit",))
    assert [(f["action"], f["original"], f["applied"]) for f in findings] == [
        ("floor_applied_over_deal_evidence", 600.0, 1_000.0)]
    assert deal["capital_plan"]["recurring_capex"]["per_unit_annual"] == 1_000.0
    assert overrides == [{
        "input": "recurring_capex_per_unit", "action": "floor_applied_over_deal_evidence", "floor": 1_000.0,
        "floor_source": "house:recurring_capex_vintage", "evidence": "the PCA (deal:pca) recurring capex",
        "original": 600.0, "applied": 1_000.0,
    }]


def test_apply_guardrails_in_pricing_pins_the_floor_over_the_pca(minimal_deal_inputs):
    deal = _pca_deal(minimal_deal_inputs)
    priced, findings = guardrails.apply_guardrails(deal, mode="pricing")
    assert [f["action"] for f in findings if f["key"] == "recurring_capex_per_unit"] == [
        "floor_applied_over_deal_evidence"]
    entry = _entries(priced)["recurring_capex_per_unit"]
    assert (entry["value"], entry["source"]) == (1_000.0, "house:recurring_capex_vintage")
    assert entry["override"]["note"] == guardrails.GUARDRAIL_OVERRIDE_NOTE
    assert entry["guardrail"]["action"] == "floor_applied_over_deal_evidence"
    assert entry["guardrail"]["original"] == 600.0 and entry["guardrail"]["applied"] == 1_000.0
    assert "deal:pca" in [c["source"] for c in entry["candidates"]]
    assert resolve_assumptions(priced, mode="pricing") == priced
    assert not [i for i in validate_deal(priced).issues if i.severity == "ERROR"]


def test_an_analyst_figure_below_the_band_keeps_the_waiver(minimal_deal_inputs):
    deal = copy.deepcopy(minimal_deal_inputs)
    deal["metadata"]["year_built"] = YEAR
    analyst = resolve_assumptions(deal, overrides={"recurring_capex_per_unit": 400})
    findings = guardrails.enforce_floors(copy.deepcopy(analyst), mode="pricing",
                                         profile=GuardrailProfile("cashflow", YEAR),
                                         keys=("recurring_capex_per_unit",))
    assert [(f["action"], f["applied"]) for f in findings] == [("floor_waived_by_analyst_override", 400.0)]


# ── backsolve ────────────────────────────────────────────────────────────


@lru_cache(maxsize=None)
def _library(slug: str) -> str:
    return json.dumps(dl.ingest(dl.load_deal(slug)))


def _solve(canonical, strategy, target):
    result = backsolve_price(
        copy.deepcopy(canonical), target_coc=target,
        policy={"version": "plat.backsolve-policy/1", "strategy": strategy},
        benchmark=BENCHMARK, min_price="1000000.00", max_price="40000000.00",
    )
    assert result["status"] == "converged"
    return result


def test_backsolve_enforces_the_deferred_revenue_floors():
    canonical = json.loads(_library("a_stabilized_1990s"))
    flagged = resolve_assumptions(canonical, deal_profile={**canonical["deal_profile"],
                                                           "deferred_maintenance": True})
    base, deferred = _solve(canonical, "cashflow", "0.06"), _solve(flagged, "cashflow", "0.06")
    findings = deferred["summary"]["policy_summary"]["guardrails"]["findings"]
    floors = {f["key"]: f["floor"] for f in findings}
    year = flagged["deal_profile"]["year_built"]
    assert floors["collection_loss"] == float(house.default_collection_loss_rate(year, deferred=True))
    assert deferred["summary"]["policy_summary"]["guardrails"]["profile"]["deferred_maintenance"] is True
    assert deferred["summary"]["solved_purchase_price"] < base["summary"]["solved_purchase_price"]


def test_backsolve_raises_the_library_pca_recurring_capex_to_the_band():
    canonical = json.loads(_library("d_value_add_1980s_msa_only"))  # synthetic PCA $1,500 < $1,750 midpoint
    result = backsolve_price(
        copy.deepcopy(canonical), target_coc="0.03",
        policy={"version": "plat.backsolve-policy/1", "strategy": "value_add"},
        benchmark=BENCHMARK, min_price="1000000.00", max_price="20000000.00",
    )
    assert result["status"] == "converged"
    case = result["case"]
    assert case["capital_plan"]["recurring_capex"]["per_unit_annual"] == 1_750.0
    applied = [o for o in result["summary"]["policy_summary"]["policy_overrides"]
               if o.get("action") == "floor_applied_over_deal_evidence"]
    assert [(o["input"], o["original"], o["applied"]) for o in applied] == [
        ("recurring_capex_per_unit", 1_500.0, 1_750.0)]
    assert _entries(case)["recurring_capex_per_unit"]["guardrail"]["action"] == "floor_applied_over_deal_evidence"
    assert resolve_assumptions(case, mode="pricing") == case
