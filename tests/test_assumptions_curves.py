"""ADR 0003 slice 4: per-year rent growth, curves, and market wiring.

Covers the engine's ``annual_growth_rates`` semantics, the resolver's
forecast -> vector glide, the §3 precedence for rent_growth, physical_vacancy,
loss_to_lease, concessions, collection_loss and exit_cap_rate (with a
SyntheticProvider registry and with no market data), the vacancy and LTL
curve shapes per deal type, overrides (vectors and curve objects),
determinism, and an end-to-end ingest -> run_underwriting.

All market data here is synthetic.
"""
from __future__ import annotations

import copy
import json
import math
import sys
from datetime import date
from pathlib import Path

import pytest

from engine import underwriting_policy as house
from engine.assumptions import AssumptionOverrideError, resolve_assumptions
from engine.engine import _apply_growth_assumptions, run_underwriting
from engine.ingest.ingestion_pipeline import build_deal_from_documents
from engine.market_data import Geo, MarketDataRegistry, SyntheticProvider
from engine.modules.rent_growth import generate_rent_curves
from engine.modules.time_grid import TimeGrid
from engine.validator import validate_deal, validate_schema
from tests.synthetic_tax_policy import with_synthetic_tax_policy

FIXTURES = Path(__file__).parent / "fixtures"
T12 = FIXTURES / "entrata_style_t12.csv"
ROLL = FIXTURES / "sample_rent_roll.csv"

MSA = Geo(metro_slug="synthetic_metro")
SUB = Geo(metro_slug="synthetic_metro", submarket="Synthetic North")

# Analysis 2026-08..2031-07: year 1 ends 2027-07 (2027Q3), year 2 ends 2028-07 (2028Q3).
FORECAST = {"2026Q3": 0.01, "2026Q4": 0.012, "2027Q1": 0.015, "2027Q2": 0.02,
            "2027Q3": 0.03, "2027Q4": 0.031, "2028Q1": 0.032, "2028Q2": 0.034, "2028Q3": 0.035}
MSA_FORECAST = {quarter: rate - 0.01 for quarter, rate in FORECAST.items()}
CAP_RATES = {"2030Q4": 0.058, "2031Q1": 0.06, "2031Q2": 0.062, "2031Q3": 0.064, "2031Q4": 0.066}


def _registry(**overrides):
    data = {
        ("rent_growth_forecast", SUB): FORECAST,
        ("rent_growth_forecast", MSA): MSA_FORECAST,
        ("rent_growth_long_run", MSA): 0.028,
        ("vacancy_stabilized", SUB): 0.06,
        ("vacancy_stabilized", MSA): 0.065,
        ("concession_rate", SUB): 0.02,
        ("market_cap_rate", SUB): CAP_RATES,
    }
    for key, value in overrides.items():
        metric, level = key.rsplit("__", 1)
        geo = SUB if level == "sub" else MSA
        if value is None:
            data.pop((metric, geo), None)
        else:
            data[(metric, geo)] = value
    return MarketDataRegistry([SyntheticProvider(data, as_of=date(2026, 6, 30), confidence="high")])


def _ingest(**kwargs):
    params = dict(
        property_id="Synthetic Curves",
        rent_roll_path=ROLL,
        t12_path=T12,
        analysis_start="2026-08",
        analysis_end="2031-07",
    )
    params.update(kwargs)
    return build_deal_from_documents(**params)


def _geo_kwargs():
    return dict(msa="Synthetic Metro", metro_slug="synthetic_metro", submarket="Synthetic North")


def _entries(deal):
    return deal["assumptions"]["entries"]


def _fresh(deal, **evidence_drop):
    """A copy of an ingested deal without its assumptions, ready to re-resolve from scratch."""
    out = copy.deepcopy(deal)
    out.pop("assumptions", None)
    t12 = out["metadata"]["ingest_gate"]["evidence"]["t12"]
    for field in evidence_drop.get("t12", ()):
        t12.pop(field, None)
    return out


def _cohort_values(deal, table, field, cohort_id=None):
    rows = deal[table]
    cohort_id = cohort_id or rows[0]["cohort_id"]
    return [row[field] for row in rows if row["cohort_id"] == cohort_id], [
        row["start_period"] for row in rows if row["cohort_id"] == cohort_id
    ]


# ── engine: annual_growth_rates semantics ────────────────────────────────


def _flat_inputs(start="2026-01", end="2029-12", **growth):
    return {
        "market_rent_curve": [{"cohort_id": "A", "start_period": start, "end_period": end, "market_rent": 1000}],
        "growth_assumptions": {"growth_type": "annual_compound", **growth},
    }


def _rent_at(curve, month):
    [row] = [r for r in curve if r["start_period"] <= month <= r["end_period"]]
    return row["market_rent"]


def test_vector_first_increase_enters_year_two_and_last_value_repeats():
    grid = TimeGrid.build("2026-01", "2029-12")
    inputs = _flat_inputs(annual_growth_rate=0.5, annual_growth_rates=[0.10, 0.20])
    curve, disclosure = _apply_growth_assumptions(grid, inputs)

    assert _rent_at(curve, "2026-12") == 1000  # year 1: no growth
    assert _rent_at(curve, "2027-01") == pytest.approx(1100)  # element 0 enters year 2
    assert _rent_at(curve, "2028-01") == pytest.approx(1320)  # element 1 enters year 3
    assert _rent_at(curve, "2029-01") == pytest.approx(1584)  # past the vector: last value repeats
    assert disclosure["annual_growth_rates"] == [0.10, 0.20]
    assert disclosure["applied_to_cohorts"] == ["A"]


def test_vector_elements_past_the_hold_are_ignored():
    grid = TimeGrid.build("2026-01", "2027-12")
    curve, _ = _apply_growth_assumptions(grid, _flat_inputs(annual_growth_rate=0.0,
                                                            annual_growth_rates=[0.05, 0.5, 0.9]))
    assert _rent_at(curve, "2027-12") == pytest.approx(1050)


def test_growth_start_month_shifts_the_vector():
    grid = TimeGrid.build("2026-01", "2029-12")
    curve, _ = _apply_growth_assumptions(
        grid, _flat_inputs(annual_growth_rate=0.0, annual_growth_rates=[0.10, 0.20], growth_start_month="2026-07")
    )
    assert _rent_at(curve, "2027-06") == 1000
    assert _rent_at(curve, "2027-07") == pytest.approx(1100)
    assert _rent_at(curve, "2028-07") == pytest.approx(1320)
    assert _rent_at(curve, "2029-07") == pytest.approx(1584)


def test_single_element_vector_matches_the_scalar_exactly():
    grid = TimeGrid.build("2026-08", "2031-07")
    scalar, _ = _apply_growth_assumptions(grid, _flat_inputs("2026-08", "2031-07", annual_growth_rate=0.03))
    vector, _ = _apply_growth_assumptions(
        grid, _flat_inputs("2026-08", "2031-07", annual_growth_rate=0.07, annual_growth_rates=[0.03])
    )
    assert sorted(vector, key=lambda r: r["start_period"]) == sorted(scalar, key=lambda r: r["start_period"])


def test_all_zero_vector_means_no_growth_even_with_a_scalar():
    grid = TimeGrid.build("2026-01", "2028-12")
    inputs = _flat_inputs(annual_growth_rate=0.05, annual_growth_rates=[0.0])
    curve, disclosure = _apply_growth_assumptions(grid, inputs)
    assert curve == inputs["market_rent_curve"]
    assert disclosure["applied_to_cohorts"] == []


def test_monthly_compound_vector():
    grid = TimeGrid.build("2026-01", "2028-12")
    result = generate_rent_curves(
        grid, [{"cohort_id": "A", "initial_inplace_rent": 1000}],
        growth_assumptions={"growth_type": "monthly_compound", "annual_growth_rates": [0.10, 0.20]},
    )
    rents = {row["month"]: row["market_rent"] for row in result["by_month"]}
    assert rents["2027-01"] == pytest.approx(1100, abs=0.01)
    assert rents["2027-02"] == pytest.approx(1100 * 1.2 ** (1 / 12), abs=0.01)
    assert rents["2028-01"] == pytest.approx(1320, abs=0.01)


def test_explicit_time_varying_curves_stay_authoritative():
    grid = TimeGrid.build("2026-01", "2027-12")
    inputs = {
        "market_rent_curve": [
            {"cohort_id": "A", "start_period": "2026-01", "end_period": "2026-12", "market_rent": 1000},
            {"cohort_id": "A", "start_period": "2027-01", "end_period": "2027-12", "market_rent": 1010},
        ],
        "growth_assumptions": {"growth_type": "annual_compound", "annual_growth_rate": 0.1,
                               "annual_growth_rates": [0.1]},
    }
    curve, disclosure = _apply_growth_assumptions(grid, inputs)
    assert curve == inputs["market_rent_curve"]
    assert disclosure["explicit_curve_cohorts"] == ["A"]


def test_schema_and_validator_accept_the_vector(minimal_deal_inputs):
    deal = copy.deepcopy(minimal_deal_inputs)
    deal["growth_assumptions"]["annual_growth_rates"] = [0.02, 0.03, 0.2]
    assert validate_schema(deal) == []
    issues = [i for i in validate_deal(deal).issues if i.code == "GROWTH_RATE_OUT_OF_BAND"]
    assert [i.path for i in issues] == ["/growth_assumptions/annual_growth_rates/2"]


# ── rent growth: forecast -> vector with glide ───────────────────────────


def test_forecast_years_then_two_year_glide_to_the_msa_long_run():
    deal = _ingest(year_built=1984, market=_registry(), **_geo_kwargs())
    growth = _entries(deal)["rent_growth"]

    # Years 1-2 covered by the submarket forecast, then glide to the MSA long-run 2.8%.
    assert growth["value"] == [0.03, 0.035, 0.0315, 0.028]
    assert growth["source"] == "market:synthetic"
    assert growth["as_of"] == "2026-06-30"
    assert growth["confidence"] == "high"
    assert growth["review_required"] is False
    assert deal["growth_assumptions"]["annual_growth_rates"] == [0.03, 0.035, 0.0315, 0.028]
    assert deal["growth_assumptions"]["annual_growth_rate"] == 0.03
    sources = [(c["source"], c.get("metric")) for c in growth["candidates"]]
    assert ("market:synthetic", "rent_growth_forecast") in sources
    assert ("market:synthetic", "rent_growth_long_run") in sources
    assert ("house:long_run_rent_growth", None) in sources


def test_msa_forecast_when_no_submarket_and_house_long_run_without_one():
    registry = _registry(rent_growth_forecast__sub=None, rent_growth_long_run__msa=None)
    deal = _ingest(year_built=1984, market=registry, **_geo_kwargs())
    # MSA forecast (1pt lower) for years 1-2, then glide to the house 2.5%.
    assert _entries(deal)["rent_growth"]["value"] == [0.02, 0.025, 0.025, 0.025]

    one_year = {"2027Q3": 0.04}
    registry = _registry(rent_growth_forecast__sub=one_year, rent_growth_long_run__msa=None)
    deal = _ingest(year_built=1984, market=registry, **_geo_kwargs())
    assert _entries(deal)["rent_growth"]["value"] == [0.04, 0.0325, 0.025, 0.025]


def test_no_market_data_uses_the_house_long_run_everywhere():
    deal = _ingest(year_built=1984, market=None, **_geo_kwargs())
    growth = _entries(deal)["rent_growth"]
    assert growth["value"] == [0.025] * 4
    assert growth["source"] == "house:long_run_rent_growth"
    assert growth["review_required"] is True
    assert float(house.HOUSE_LONG_RUN_RENT_GROWTH) == 0.025


def test_rent_growth_analyst_override_wins_over_market():
    deal = _ingest(rent_growth_rate=0.02, market=_registry(), **_geo_kwargs())
    growth = _entries(deal)["rent_growth"]
    assert growth["source"] == "analyst"
    assert growth["value"] == 0.02
    assert growth["default_value"][:2] == [0.03, 0.035]
    assert deal["growth_assumptions"] == {"growth_type": "annual_compound", "annual_growth_rate": 0.02}


# ── precedence per key ───────────────────────────────────────────────────


def test_market_precedence_for_vacancy_and_concessions():
    deal = _ingest(year_built=1984, market=_registry(), **_geo_kwargs())
    entries = _entries(deal)

    vacancy = entries["physical_vacancy"]
    assert vacancy["value"] == {"start": 0.2, "stabilized": 0.06, "window_months": 12}
    assert vacancy["source"] == "market:synthetic"

    concessions = entries["concessions"]
    assert concessions["value"] == {"year1": 0.01, "stabilized": 0.02, "year1_months": 12}
    assert concessions["source"] == "deal:t12"  # year 1 from the T12 beats the market
    assert deal["concession_schedule"] == [
        {"concession_id": "assumption_year1_concessions", "start_month": "2026-08", "end_month": "2027-07",
         "concession_type": "pct_rent", "amount": 0.01, "applies_to_cohort": "ALL"},
        {"concession_id": "assumption_stabilized_concessions", "start_month": "2027-08",
         "end_month": "2031-07", "concession_type": "pct_rent", "amount": 0.02, "applies_to_cohort": "ALL"},
    ]
    # Collection loss is deal evidence first, whatever the market says.
    assert entries["collection_loss"]["source"] == "deal:t12"
    assert entries["collection_loss"]["value"] == 0.015
    assert validate_schema(deal) == []


def test_msa_vacancy_when_the_submarket_has_none():
    deal = _ingest(year_built=1984, market=_registry(vacancy_stabilized__sub=None), **_geo_kwargs())
    assert _entries(deal)["physical_vacancy"]["value"]["stabilized"] == 0.065


def test_house_precedence_without_market_data():
    deal = _ingest(year_built=1984, market=None)
    entries = _entries(deal)
    assert entries["physical_vacancy"]["value"] == {"start": 0.2, "stabilized": 0.0575, "window_months": 12}
    assert entries["physical_vacancy"]["source"] == "house:vintage_1980s"
    assert float(house.typical_physical_vacancy_rate(1984)) == 0.0575
    assert entries["concessions"]["value"] == 0.01  # T12 year 1; 1980s typical-low stabilized is 0%
    assert [row["concession_id"] for row in deal["concession_schedule"]] == ["assumption_year1_concessions"]
    assert entries["loss_to_lease"]["value"]["floor"] == 0.01


def test_concessions_without_t12_use_market_then_house():
    base = _ingest(year_built=1984, market=None, **_geo_kwargs())
    deal = _fresh(base, t12=("concessions",))
    deal["concession_schedule"] = None

    with_market = resolve_assumptions(deal, market=_registry())
    entry = _entries(with_market)["concessions"]
    assert entry["value"] == {"year1": 0.02, "stabilized": 0.02, "year1_months": 12}
    assert entry["source"] == "market:synthetic"

    house_only = resolve_assumptions(deal)
    entry = _entries(house_only)["concessions"]
    assert entry["value"] == float(house.default_year1_concessions_rate(1984))
    assert entry["source"] == "house:vintage_1980s"


def test_collection_loss_without_t12_uses_the_house_default():
    base = _ingest(year_built=1984, market=None)
    deal = _fresh(base, t12=("bad_debt",))
    deal["collection_loss_curve"][0]["loss_rate"] = None
    entry = _entries(resolve_assumptions(deal))["collection_loss"]
    assert entry["value"] == 0.01  # 1980s bad_debt_typical_low, not the 0.75% floor
    assert entry["source"] == "house:vintage_1980s"

    deal["metadata"]["ingest_gate"]["evidence"].pop("year_built")
    deal["deal_profile"] = {"deal_type": "stabilized"}
    entry = _entries(resolve_assumptions(deal))["collection_loss"]
    assert entry["value"] == 0.02
    assert entry["source"] == "house:default_collection_loss"


def test_exit_cap_rate_market_beats_existing_behavior_but_not_the_analyst():
    base = _ingest(year_built=1984, market=None, **_geo_kwargs())

    deal = _fresh(base)
    deal["exit_assumptions"] = {"exit_month": "2031-07"}
    resolved = resolve_assumptions(deal, market=_registry())
    entry = _entries(resolved)["exit_cap_rate"]
    assert entry["value"] == pytest.approx(0.063)  # 2031 calendar-year mean of the submarket series
    assert entry["source"] == "market:synthetic"
    assert resolved["exit_assumptions"]["exit_cap_rate"] == pytest.approx(0.063)

    analyst = _ingest(year_built=1984, exit_cap_rate=0.055, purchase_price=1_000_000,
                      market=_registry(), **_geo_kwargs())
    assert _entries(analyst)["exit_cap_rate"]["source"] == "analyst"
    assert analyst["exit_assumptions"]["exit_cap_rate"] == 0.055

    no_exit = resolve_assumptions(_fresh(base), market=_registry())
    assert "exit_assumptions" not in no_exit  # no exit is modeled: none is created
    assert _entries(no_exit)["exit_cap_rate"]["value"] is None


def test_ingest_exit_month_without_a_cap_rate_resolves_the_exit_cap():
    """An exit month (or sale cost) given at ingest without an exit cap rate models
    the exit; the resolver fills the cap rate instead of the exit being dropped."""
    deal = _ingest(year_built=1984, purchase_price=1_000_000, exit_month="2031-07", sale_cost_pct=0.025,
                   market=_registry(), **_geo_kwargs())
    entries = _entries(deal)
    assert deal["exit_assumptions"]["exit_month"] == "2031-07"
    assert deal["exit_assumptions"]["exit_cap_rate"] == pytest.approx(0.063)
    assert entries["exit_cap_rate"]["source"] == "market:synthetic"
    assert entries["exit_cap_rate"]["override"] is None
    assert deal["exit_assumptions"]["sale_cost_percent"] == 0.025
    assert entries["sale_cost_percent"]["source"] == "analyst"

    no_market = _ingest(year_built=1984, purchase_price=1_000_000, exit_month="2031-07", market=None)
    entries = _entries(no_market)
    assert no_market["exit_assumptions"]["exit_month"] == "2031-07"
    assert "exit_cap_rate" not in no_market["exit_assumptions"]  # the engine default applies, disclosed
    assert entries["exit_cap_rate"]["source"] == "engine_fallback"
    assert entries["exit_cap_rate"]["review_required"] is True
    assert no_market["exit_assumptions"]["sale_cost_percent"] == 0.02
    assert entries["sale_cost_percent"]["source"] == "house:backsolve_policy"


def test_star_tier_and_submarket_become_the_geo():
    from engine.assumptions.curves import profile_geo
    from engine.assumptions import DealProfile

    profile = DealProfile(msa={"cbsa_code": "99999", "metro_slug": "synthetic_metro"},
                          submarket={"name": "Synthetic North"}, star_tier="4_5")
    assert profile_geo(profile) == Geo(cbsa_code="99999", metro_slug="synthetic_metro",
                                       submarket="Synthetic North", star_tier="4 & 5 Star")
    assert profile_geo(DealProfile()) is None


# ── curve shapes ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("deal_type, window, ltl_floor", [("stabilized", 12, 0.01), ("value_add", 24, 0.0125)])
def test_vacancy_and_ltl_burn_off_shapes(deal_type, window, ltl_floor):
    deal = _ingest(year_built=1984, deal_type=deal_type, market=None)

    vacancy, months = _cohort_values(deal, "physical_vacancy_curve", "vacancy_rate")
    assert len(vacancy) == window + 1  # one segment per ramp month, then one flat segment
    assert months[:2] == ["2026-08", "2026-09"]
    assert vacancy[0] == 0.2
    assert vacancy[window // 2] == pytest.approx(0.2 + (0.0575 - 0.2) / 2, abs=1e-6)
    assert vacancy[-1] == 0.0575
    assert all(a > b for a, b in zip(vacancy, vacancy[1:]))
    stabilized_from = "2027-08" if window == 12 else "2028-08"
    assert months[-1] == stabilized_from
    assert deal["physical_vacancy_curve"][-1]["end_period"] == "2031-07"

    ltl, ltl_months = _cohort_values(deal, "loss_to_lease", "ltl_percent", "1br1ba")
    assert len(ltl) == 13 and ltl_months[-1] == "2027-08"  # one lease term whatever the deal type
    assert ltl[0] == pytest.approx(0.117547)
    assert ltl[-1] == ltl_floor
    for table in ("physical_vacancy_curve", "loss_to_lease"):
        assert {row["cohort_id"] for row in deal[table]} == {"1br1ba", "2br1ba", "2br2ba"}
    assert validate_schema(deal) == []


def test_no_year_built_and_no_market_holds_the_rent_roll_levels_flat():
    deal = _ingest(market=None)
    assert {row["vacancy_rate"] for row in deal["physical_vacancy_curve"]} == {0.2}
    assert len(deal["physical_vacancy_curve"]) == 3
    assert _entries(deal)["physical_vacancy"]["source"] == "deal:rent_roll"
    assert len(deal["loss_to_lease"]) == 3


def test_hand_authored_curves_are_left_as_they_are(minimal_deal_inputs):
    resolved = resolve_assumptions(minimal_deal_inputs, market=_registry())
    for table in ("physical_vacancy_curve", "loss_to_lease", "market_rent_curve", "collection_loss_curve"):
        assert resolved[table] == minimal_deal_inputs[table]
    assert resolved["growth_assumptions"] == minimal_deal_inputs["growth_assumptions"]
    assert _entries(resolved)["physical_vacancy"]["source"] == "analyst"


# ── overrides: scalars, vectors and curve objects ────────────────────────


def test_vector_override_round_trips_and_is_idempotent():
    deal = _ingest(year_built=1984, market=None)
    resolved = resolve_assumptions(deal, overrides={"rent_growth": [0.02, 0.03, 0.035]})
    entry = _entries(resolved)["rent_growth"]

    assert entry["value"] == [0.02, 0.03, 0.035]
    assert entry["default_value"] == [0.025] * 4
    assert entry["override"] == {"value": [0.02, 0.03, 0.035], "note": "analyst override"}
    assert resolved["growth_assumptions"]["annual_growth_rates"] == [0.02, 0.03, 0.035]
    assert resolved["growth_assumptions"]["annual_growth_rate"] == 0.02
    assert validate_schema(resolved) == []
    assert resolve_assumptions(resolved) == resolved
    assert resolve_assumptions(resolve_assumptions(resolved)) == resolved


def test_curve_object_overrides_round_trip_and_are_idempotent():
    deal = _ingest(year_built=1984, market=None)
    resolved = resolve_assumptions(deal, overrides={
        "physical_vacancy": {"value": {"start": 0.15, "stabilized": 0.05, "window_months": 6}, "note": "plan"},
        "loss_to_lease": {"value": {"start": 0.05, "floor": 0.02, "burn_off_months": 3}},
        "concessions": {"value": {"year1": 0.03, "stabilized": 0.01}},
    })
    entries = _entries(resolved)
    vacancy, _ = _cohort_values(resolved, "physical_vacancy_curve", "vacancy_rate")
    assert vacancy[0] == 0.15 and vacancy[-1] == 0.05 and len(vacancy) == 7
    ltl, _ = _cohort_values(resolved, "loss_to_lease", "ltl_percent")
    assert ltl == [0.05, 0.04, 0.03, 0.02]
    assert [row["amount"] for row in resolved["concession_schedule"]] == [0.03, 0.01]
    assert entries["physical_vacancy"]["override"]["note"] == "plan"
    assert entries["physical_vacancy"]["default_value"]["stabilized"] == 0.0575
    assert entries["concessions"]["value"] == {"year1": 0.03, "stabilized": 0.01, "year1_months": 12}
    assert resolve_assumptions(resolved) == resolved


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"collection_loss": [0.01, 0.02]}, "not a vector"),
        ({"rent_growth": []}, "must not be empty"),
        ({"rent_growth": [0.02, float("nan")]}, "finite number"),
        ({"rent_growth": {"value": {"start": 0.1}}}, "not an object"),
        ({"physical_vacancy": {"value": {"start": 0.1, "stabilized": 0.05}}}, "missing"),
        ({"physical_vacancy": {"value": {"start": 0.1, "stabilized": 0.05, "window_months": 1.5}}}, "whole number"),
        ({"loss_to_lease": {"value": {"start": 0.1, "floor": 0.01, "burn_off_months": 12, "x": 1}}}, "unknown"),
        ({"concessions": {"value": {"year1": math.inf, "stabilized": 0.0}}}, "finite number"),
        ({"physical_vacancy": {"value": {"start": 0.1, "stabilized": 0.05, "window_months": 12},
                               "cohort_overrides": {"A": 0.03}}}, "scalar value"),
        # Only physical_vacancy and loss_to_lease take cohort overrides.
        ({"rent_growth": {"value": [0.02], "cohort_overrides": {"A": 0.03}}}, "does not support cohort_overrides"),
        ({"collection_loss": {"value": 0.02, "cohort_overrides": {"A": 0.03}}}, "does not support cohort_overrides"),
    ],
)
def test_override_shapes_are_validated(overrides, message):
    deal = _ingest(market=None)
    with pytest.raises(AssumptionOverrideError, match=message):
        resolve_assumptions(deal, overrides=overrides)


# ── market wiring: ingest, re-resolve, CLI ───────────────────────────────


def test_missing_market_data_falls_through():
    empty = MarketDataRegistry([SyntheticProvider({})])
    with_empty = _ingest(year_built=1984, market=empty, **_geo_kwargs())
    without = _ingest(year_built=1984, market=None, **_geo_kwargs())
    strip = lambda d: {k: v for k, v in d.items() if k not in ("assumptions", "metadata")}  # noqa: E731
    assert strip(with_empty) == strip(without)
    values = lambda d: {k: e["value"] for k, e in _entries(d).items()}  # noqa: E731
    assert values(with_empty) == values(without)


def test_reresolving_without_market_data_keeps_market_values():
    deal = _ingest(year_built=1984, market=_registry(), **_geo_kwargs())
    again = resolve_assumptions(deal, overrides={"insurance_per_unit": 900})

    for key in ("rent_growth", "physical_vacancy", "concessions"):
        assert _entries(again)[key]["value"] == _entries(deal)[key]["value"], key
    assert _entries(again)["rent_growth"]["source"] == "market:synthetic"
    assert "Kept from the previous resolution" in _entries(again)["rent_growth"]["basis"]
    for field in ("growth_assumptions", "physical_vacancy_curve", "concession_schedule"):
        assert again[field] == deal[field], field
    assert resolve_assumptions(again) == again


def test_partial_json_reresolve_keeps_market_values():
    from runs.ingest_deal import apply_partial_and_reresolve

    registry = _registry()
    deal = _ingest(year_built=1984, market=registry, **_geo_kwargs())
    merged = apply_partial_and_reresolve(deal, {"metadata": {"analyst": "Synthetic"}}, market=registry)
    assert merged["growth_assumptions"] == deal["growth_assumptions"]
    assert merged["physical_vacancy_curve"] == deal["physical_vacancy_curve"]


def test_ingest_uses_the_default_registry_unless_told_not_to(monkeypatch):
    import engine.market_data as market_data

    calls = []

    def fake_default_registry(*args, **kwargs):
        calls.append(True)
        return _registry()

    monkeypatch.setattr(market_data, "default_registry", fake_default_registry)
    deal = _ingest(year_built=1984, **_geo_kwargs())
    assert calls == [True]
    assert _entries(deal)["rent_growth"]["source"] == "market:synthetic"
    _ingest(year_built=1984, market=None, **_geo_kwargs())
    assert calls == [True]


def test_cli_no_market_data_flag(tmp_path, monkeypatch):
    from runs import ingest_deal
    import engine.market_data as market_data

    assert ingest_deal.build_parser().parse_args(["--no-market-data"]).no_market_data is True
    monkeypatch.setattr(market_data, "default_registry", lambda *a, **k: _registry())
    outputs = {}
    for flag in ([], ["--no-market-data"]):
        output = tmp_path / f"deal{len(flag)}.json"
        monkeypatch.setattr(sys, "argv", [
            "ingest_deal.py", "--property-id", "Synthetic CLI", "--rent-roll", str(ROLL), "--t12", str(T12),
            "--start", "2026-08", "--end", "2031-07", "--year-built", "1984",
            "--metro-slug", "synthetic_metro", "--submarket", "Synthetic North",
            "--output", str(output), *flag,
        ])
        assert ingest_deal.main() == 0
        outputs[bool(flag)] = json.loads(output.read_text())
    assert outputs[False]["growth_assumptions"]["annual_growth_rates"] == [0.03, 0.035, 0.0315, 0.028]
    assert outputs[True]["growth_assumptions"]["annual_growth_rates"] == [0.025] * 4


# ── determinism and end to end ───────────────────────────────────────────


def test_resolution_is_deterministic():
    first = _ingest(year_built=1984, deal_type="value_add", market=_registry(), **_geo_kwargs())
    second = _ingest(year_built=1984, deal_type="value_add", market=_registry(), **_geo_kwargs())
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert resolve_assumptions(first, market=_registry()) == first


def test_end_to_end_ingest_runs_underwriting():
    deal = _ingest(
        year_built=1984, market=_registry(), purchase_price=1_000_000, ltv=0.6, loan_rate=0.06,
        amort_years=30, exit_cap_rate=0.06, **_geo_kwargs(),
    )
    report = validate_deal(deal)
    assert report.status == "PASS", [i for i in report.issues if i.severity == "ERROR"]

    inputs = with_synthetic_tax_policy(deal)
    result = run_underwriting(copy.deepcopy(inputs))
    assert run_underwriting(copy.deepcopy(inputs)) == result

    assert result["rent_growth"]["annual_growth_rates"] == [0.03, 0.035, 0.0315, 0.028]
    assert result["rent_growth"]["applied_to_cohorts"] == ["1br1ba", "2br1ba", "2br2ba"]
    rents = [row["net_rent"] for row in result["revenue"]["base_rent"]["by_month"]]
    # Vacancy and LTL burn off through year 1, so year-2 rent beats month 1 by far more than growth.
    assert rents[12] > rents[0] * 1.2
    years = result["cashflow"]["by_year"]
    assert len(years) == 6
    assert all(math.isfinite(row["net_operating_income"]) for row in years)
