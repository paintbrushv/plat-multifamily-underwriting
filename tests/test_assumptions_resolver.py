"""ADR 0003 slice 2: deal_profile + assumptions schema and resolver v1.

The resolver records provenance and candidates for every v1 assumption key
and writes resolved values into the existing canonical fields. Slice 2 changes
no numbers: ingest and the engine must produce exactly what they produced
before the assumption layer existed.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

from engine.assumptions import (
    ASSUMPTION_KEYS,
    AssumptionEntry,
    AssumptionOverrideError,
    DealProfile,
    UnsupportedDealTypeError,
    resolve_assumptions,
)
from engine import underwriting_policy as house
from engine.engine import run_underwriting
from engine.ingest.ingestion_pipeline import build_deal_from_documents
from engine.validator import validate_deal, validate_schema

FIXTURES = Path(__file__).parent / "fixtures"
T12 = FIXTURES / "entrata_style_t12.csv"
ROLL = FIXTURES / "sample_rent_roll.csv"
REPO_ROOT = Path(__file__).resolve().parent.parent

STABILIZED_KEYS = {
    "rent_growth",
    "collection_loss",
    "physical_vacancy",
    "loss_to_lease",
    "concessions",
    "insurance_per_unit",
    "r_and_m_per_unit",
    "replacement_reserve_per_unit",
    "exit_cap_rate",
    "sale_cost_percent",
    "acquisition_fee_pct",
    "asset_management_fee_pct",
    "disposition_fee_pct",
    "annual_partnership_expenses",
    "partnership_closing_costs",
    "purchase_closing_cost_pct",
    "loan_closing_costs",
}

FULL_ECONOMICS = dict(
    purchase_price=25_000_000,
    ltv=0.75,
    loan_rate=0.065,
    amort_years=30,
    exit_cap_rate=0.055,
    sponsor_equity_pct=0.10,
    pref_return=0.08,
)


def _ingest(**kwargs):
    params = dict(
        property_id="Synthetic Statement",
        rent_roll_path=ROLL,
        t12_path=T12,
        analysis_start="2026-08",
        analysis_end="2031-07",
    )
    params.update(kwargs)
    return build_deal_from_documents(**params)


def _entries(deal):
    return deal["assumptions"]["entries"]


def _sources(entry):
    return [candidate["source"] for candidate in entry["candidates"]]


# ── provenance and candidates ────────────────────────────────────────────


@pytest.fixture(scope="module")
def full_deal():
    return _ingest(year_built=1984, **FULL_ECONOMICS)


def test_every_stabilized_key_is_resolved_with_the_registry_shape(full_deal):
    entries = _entries(full_deal)
    assert set(entries) == STABILIZED_KEYS
    assert full_deal["assumptions"]["resolver_version"] == "plat.assumptions/1"
    assert full_deal["assumptions"]["mode"] == "underwrite"
    for key, entry in entries.items():
        assert set(entry) >= {
            "value", "unit", "source", "basis", "as_of", "confidence",
            "candidates", "default_value", "override", "review_required",
        }, key
        assert entry["basis"], key
        assert entry["candidates"], key
        assert AssumptionEntry.from_dict(entry).to_dict() == entry
    assert validate_schema(full_deal) == []


@pytest.mark.parametrize(
    "key, source, value, candidate_sources",
    [
        ("rent_growth", "engine_fallback", 0.03, ["engine_fallback"]),
        ("collection_loss", "house:vintage_1980s", 0.0075,
         ["deal:t12", "house:vintage_1980s", "engine_fallback"]),
        ("physical_vacancy", "deal:rent_roll", 0.2, ["deal:rent_roll", "house:vintage_1980s"]),
        ("loss_to_lease", "engine_fallback", 0.0,
         ["deal:rent_roll", "house:vintage_1980s", "engine_fallback"]),
        ("concessions", "engine_fallback", 0.0, ["deal:t12", "house:vintage_1980s", "engine_fallback"]),
        ("insurance_per_unit", "deal:t12", 25_200.0,
         ["deal:t12", "house:year1_insurance_floor", "engine_fallback"]),
        ("r_and_m_per_unit", "deal:t12", 19_500.0, ["deal:t12", "house:vintage_1980s", "engine_fallback"]),
        ("replacement_reserve_per_unit", "engine_fallback", 0.0, ["house:vintage_1980s", "engine_fallback"]),
        ("exit_cap_rate", "analyst", 0.055, ["analyst", "engine_fallback"]),
        ("sale_cost_percent", "house:backsolve_policy", 0.02, ["house:backsolve_policy", "engine_fallback"]),
        ("acquisition_fee_pct", "house:backsolve_policy", 0.01, ["house:backsolve_policy", "engine_fallback"]),
        ("asset_management_fee_pct", "house:backsolve_policy", 0.015,
         ["house:backsolve_policy", "engine_fallback"]),
        ("disposition_fee_pct", "house:backsolve_policy", 0.01, ["house:backsolve_policy", "engine_fallback"]),
        ("annual_partnership_expenses", "engine_fallback", 0.0, ["house:backsolve_policy", "engine_fallback"]),
        ("partnership_closing_costs", "engine_fallback", 0.0, ["house:backsolve_policy", "engine_fallback"]),
        ("purchase_closing_cost_pct", "engine_fallback", 0.0, ["house:backsolve_policy", "engine_fallback"]),
        ("loan_closing_costs", "engine_fallback", 0.0, ["house:backsolve_policy", "engine_fallback"]),
    ],
)
def test_provenance_and_candidates_per_key(full_deal, key, source, value, candidate_sources):
    entry = _entries(full_deal)[key]
    assert entry["source"] == source
    assert entry["value"] == pytest.approx(value)
    assert _sources(entry) == candidate_sources
    assert entry["review_required"] is (source.startswith("house:") or source == "engine_fallback")


def test_house_candidates_are_recorded_even_when_deal_evidence_wins(full_deal):
    entries = _entries(full_deal)
    insurance = {c["source"]: c["value"] for c in entries["insurance_per_unit"]["candidates"]}
    assert insurance["deal:t12"] == pytest.approx(25_200.0)
    assert insurance["house:year1_insurance_floor"] == pytest.approx(600.0)
    r_and_m = {c["source"]: c["value"] for c in entries["r_and_m_per_unit"]["candidates"]}
    assert r_and_m["house:vintage_1980s"] == float(house.default_r_and_m_per_unit(1984, strategy="cashflow"))
    vacancy = {c["source"]: c["value"] for c in entries["physical_vacancy"]["candidates"]}
    assert vacancy["house:vintage_1980s"] == float(house.default_physical_vacancy_rate(1984, strategy="cashflow"))


def test_deal_evidence_candidates_come_from_the_documents(full_deal):
    entries = _entries(full_deal)
    collection = {c["source"]: c["value"] for c in entries["collection_loss"]["candidates"]}
    assert collection["deal:t12"] == pytest.approx(90_000 / 6_000_000)
    concessions = {c["source"]: c["value"] for c in entries["concessions"]["candidates"]}
    assert concessions["deal:t12"] == pytest.approx(60_000 / 6_000_000)
    ltl = {c["source"]: c["value"] for c in entries["loss_to_lease"]["candidates"]}
    assert 0 < ltl["deal:rent_roll"] < 0.2


def test_where_adr_precedence_would_change_a_number_the_basis_says_so(full_deal):
    entries = _entries(full_deal)
    for key in (
        "collection_loss",
        "loss_to_lease",
        "concessions",
        "replacement_reserve_per_unit",
        "annual_partnership_expenses",
        "partnership_closing_costs",
        "purchase_closing_cost_pct",
    ):
        assert "ADR 0003 precedence would select" in entries[key]["basis"], key
        assert "today's" in entries[key]["basis"], key
    for key in ("physical_vacancy", "insurance_per_unit", "r_and_m_per_unit", "rent_growth"):
        assert "ADR 0003 precedence" not in entries[key]["basis"], key


def test_opex_evidence_is_annualized_by_calculation_type(minimal_deal_inputs):
    deal = copy.deepcopy(minimal_deal_inputs)
    insurance = next(row for row in deal["opex_table"] if row["category_name"] == "Insurance")
    insurance.update(calculation_type="per_unit_monthly", base_value=50)
    resolved = resolve_assumptions(deal)

    entry = _entries(resolved)["insurance_per_unit"]
    assert entry["value"] == pytest.approx(600.0)
    assert entry["source"] == "analyst"
    assert resolved["opex_table"] == deal["opex_table"]


# ── deal profile ─────────────────────────────────────────────────────────


def test_deal_type_maps_to_house_strategy():
    assert DealProfile("stabilized").strategy == "cashflow"
    assert DealProfile("value_add").strategy == "value_add"
    assert DealProfile().deal_type == "stabilized"


def test_value_add_profile_uses_value_add_house_values_and_adds_turn_cost():
    deal = _ingest(year_built=1984, deal_type="value_add", msa="Synthetic Metro",
                   metro_slug="synthetic_metro", submarket="Synthetic North")
    entries = _entries(deal)

    assert deal["deal_profile"] == {
        "deal_type": "value_add",
        "msa": {"name": "Synthetic Metro", "metro_slug": "synthetic_metro"},
        "submarket": {"name": "Synthetic North"},
        "year_built": 1984,
    }
    assert set(entries) == STABILIZED_KEYS | {"turn_cost_per_unit"}
    assert entries["collection_loss"]["value"] == pytest.approx(0.01)
    assert entries["collection_loss"]["source"] == "house:vintage_1980s"
    turn = entries["turn_cost_per_unit"]
    assert turn["source"] == "house:vintage_1980s"
    assert turn["value"] > 0
    assert "does not consume" in turn["basis"]
    assert validate_deal(deal).status == "PASS"


@pytest.mark.parametrize("deal_type", ["lease_up", "distressed"])
def test_reserved_deal_types_are_rejected_by_the_resolver(minimal_deal_inputs, deal_type):
    with pytest.raises(UnsupportedDealTypeError, match="reserved"):
        DealProfile(deal_type)
    with pytest.raises(UnsupportedDealTypeError, match="reserved"):
        resolve_assumptions(minimal_deal_inputs, deal_profile={"deal_type": deal_type})
    stored = copy.deepcopy(minimal_deal_inputs)
    stored["deal_profile"] = {"deal_type": deal_type}
    with pytest.raises(UnsupportedDealTypeError, match="reserved"):
        resolve_assumptions(stored)
    with pytest.raises(UnsupportedDealTypeError, match="reserved"):
        _ingest(deal_type=deal_type)


@pytest.mark.parametrize("deal_type", ["lease_up", "distressed"])
def test_validator_errors_on_an_unsupported_deal_type(minimal_deal_inputs, deal_type):
    deal = copy.deepcopy(minimal_deal_inputs)
    deal["deal_profile"] = {"deal_type": deal_type}
    report = validate_deal(deal)

    assert report.status == "FAIL"
    errors = [i for i in report.issues if i.code == "DEAL_TYPE_UNSUPPORTED"]
    assert len(errors) == 1
    assert errors[0].severity == "ERROR"
    assert errors[0].path == "/deal_profile/deal_type"
    assert "reserved" in errors[0].message


def test_unknown_deal_type_fails_schema_validation(minimal_deal_inputs):
    deal = copy.deepcopy(minimal_deal_inputs)
    deal["deal_profile"] = {"deal_type": "core_plus"}
    assert validate_deal(deal).status == "FAIL"
    with pytest.raises(UnsupportedDealTypeError, match="not a known deal type"):
        resolve_assumptions(deal)


# ── overrides and idempotence ────────────────────────────────────────────


def test_override_round_trip_keeps_default_value():
    deal = _ingest()
    resolved = resolve_assumptions(
        deal,
        overrides={
            "rent_growth": 0.025,
            "physical_vacancy": {"value": 0.07, "note": "leasing report"},
            "replacement_reserve_per_unit": 300,
        },
    )
    entries = _entries(resolved)

    growth = entries["rent_growth"]
    assert growth["value"] == 0.025
    assert growth["default_value"] == 0.03
    assert growth["override"] == {"value": 0.025, "note": "analyst override"}
    assert growth["source"] == "analyst"
    assert growth["review_required"] is False
    assert _sources(growth)[0] == "analyst"
    assert resolved["growth_assumptions"]["annual_growth_rate"] == 0.025

    vacancy = entries["physical_vacancy"]
    assert vacancy["override"] == {"value": 0.07, "note": "leasing report"}
    assert vacancy["default_value"] == pytest.approx(0.2)
    assert {row["vacancy_rate"] for row in resolved["physical_vacancy_curve"]} == {0.07}

    reserves = entries["replacement_reserve_per_unit"]
    assert reserves["default_value"] == 0.0
    assert resolved["replacement_reserves"] == [
        {"start_period": "2026-08", "end_period": "2031-07", "annual_amount": 3_000.0}
    ]
    assert validate_schema(resolved) == []

    for entry in entries.values():
        assert AssumptionEntry.from_dict(entry).to_dict() == entry
    assert json.loads(json.dumps(resolved)) == resolved


def test_resolving_twice_is_idempotent_and_preserves_overrides():
    deal = _ingest(year_built=1984, rent_growth_rate=0.025, **FULL_ECONOMICS)
    once = resolve_assumptions(deal, overrides={"insurance_per_unit": 900, "collection_loss": 0.02})
    twice = resolve_assumptions(once)
    thrice = resolve_assumptions(twice)

    assert twice == once
    assert thrice == once
    entries = _entries(twice)
    assert entries["insurance_per_unit"]["override"]["value"] == 900
    assert entries["collection_loss"]["override"]["value"] == 0.02
    assert entries["rent_growth"]["override"] == {"value": 0.025, "note": "ingest input rent_growth_rate"}
    assert resolve_assumptions(deal) == deal


def test_insurance_override_replaces_the_t12_line_without_stacking():
    deal = _ingest(year_built=1984)
    resolved = resolve_assumptions(deal, overrides={"insurance_per_unit": 900})
    insurance = [row for row in resolved["opex_table"] if "insurance" in row["category_name"].lower()]

    assert len(insurance) == 1
    assert insurance[0]["category_name"] == "Insurance"
    assert insurance[0]["calculation_type"] == "fixed_annual"
    assert insurance[0]["base_value"] == 9_000.0
    assert _entries(resolved)["insurance_per_unit"]["default_value"] == pytest.approx(25_200.0)


def test_concession_and_r_and_m_overrides_write_the_fields_the_engine_reads():
    deal = _ingest(year_built=1984)
    resolved = resolve_assumptions(deal, overrides={"concessions": 0.02, "r_and_m_per_unit": 20_000})

    assert resolved["concession_schedule"] == [{
        "concession_id": "assumption_year1_concessions",
        "start_month": "2026-08",
        "end_month": "2027-07",
        "concession_type": "pct_rent",
        "amount": 0.02,
        "applies_to_cohort": "ALL",
    }]
    adjustment = [
        row for row in resolved["opex_table"]
        if row["category_name"] == "Repairs & Maintenance Assumption Adjustment"
    ]
    assert [row["base_value"] for row in adjustment] == [20_000 * 10 - 195_000.0]
    assert _entries(resolved)["r_and_m_per_unit"]["value"] == 20_000
    assert validate_schema(resolved) == []
    assert resolve_assumptions(resolved) == resolved


def test_canonical_edit_after_resolution_becomes_an_override():
    deal = _ingest()
    deal["growth_assumptions"]["annual_growth_rate"] = 0.02
    resolved = resolve_assumptions(deal)
    growth = _entries(resolved)["rent_growth"]

    assert growth["source"] == "analyst"
    assert growth["value"] == 0.02
    assert growth["default_value"] == 0.03
    assert growth["override"]["note"] == "changed in the canonical after the last resolution"


def test_override_validation():
    deal = _ingest()
    with pytest.raises(AssumptionOverrideError, match="unknown assumption keys"):
        resolve_assumptions(deal, overrides={"cap_rate": 0.05})
    with pytest.raises(AssumptionOverrideError, match="finite number"):
        resolve_assumptions(deal, overrides={"rent_growth": "3%"})
    with pytest.raises(AssumptionOverrideError, match="do not apply"):
        resolve_assumptions(deal, overrides={"turn_cost_per_unit": 4_000})
    with pytest.raises(ValueError, match="mode"):
        resolve_assumptions(deal, mode="stress")
    assert set(ASSUMPTION_KEYS) == STABILIZED_KEYS | {"turn_cost_per_unit"}


def test_input_is_not_mutated(minimal_deal_inputs):
    deal = _ingest(year_built=1984, **FULL_ECONOMICS)
    before = copy.deepcopy(deal)
    resolve_assumptions(deal, overrides={"rent_growth": 0.01, "insurance_per_unit": 1_000,
                                         "r_and_m_per_unit": 500, "concessions": 0.02})
    assert deal == before

    minimal_before = copy.deepcopy(minimal_deal_inputs)
    resolve_assumptions(minimal_deal_inputs, deal_profile={"deal_type": "value_add"})
    assert minimal_deal_inputs == minimal_before


# ── engine ignores the new sections ──────────────────────────────────────


def test_engine_output_is_unchanged_with_and_without_the_new_sections(minimal_deal_inputs):
    baseline = run_underwriting(copy.deepcopy(minimal_deal_inputs))
    resolved = resolve_assumptions(minimal_deal_inputs)
    stripped = {k: v for k, v in resolved.items() if k not in ("assumptions", "deal_profile")}

    assert stripped == minimal_deal_inputs
    assert run_underwriting(resolved) == baseline
    assert run_underwriting(stripped) == baseline
    value_add = resolve_assumptions(minimal_deal_inputs, deal_profile=DealProfile("value_add", year_built=1984))
    assert run_underwriting(value_add) == baseline


def test_hand_authored_values_are_recorded_as_analyst_overrides(minimal_deal_inputs):
    entries = _entries(resolve_assumptions(minimal_deal_inputs))

    for key in ("rent_growth", "collection_loss", "physical_vacancy", "insurance_per_unit", "exit_cap_rate"):
        assert entries[key]["source"] == "analyst", key
        assert entries[key]["override"]["note"] == "present in the canonical before resolution", key
    assert entries["concessions"]["source"] == "engine_fallback"


# ── ingest numbers unchanged ─────────────────────────────────────────────

# Values the pre-assumption-layer ingest produced for the synthetic fixtures.
_EXPECTED_OPEX = [
    ("Payroll", 810_000.0),
    ("Repairs & Maintenance", 195_000.0),
    ("Turnover / Make-Ready", 81_000.0),
    ("Utilities", 600_000.0),
    ("Office Supplies", 18_000.0),
    ("Telephone", 15_000.0),
    ("Marketing / Advertising", 36_000.0),
    ("Insurance", 252_000.0),
    ("Real Estate Taxes", 900_000.0),
    ("Property Management Fee", 180_000.0),
]


def _curve_values(deal, table, key):
    return {row[key] for row in deal[table]}


@pytest.mark.parametrize(
    "kwargs, growth, loss, vacancy",
    [
        ({}, 0.03, 0.005, 0.2),
        ({"year_built": 1984}, 0.03, 0.0075, 0.2),
        ({"rent_growth_rate": 0.025, "collection_loss_rate": 0.02, "vacancy_rate_override": 0.07},
         0.025, 0.02, 0.07),
    ],
)
def test_ingest_numbers_are_unchanged(kwargs, growth, loss, vacancy):
    deal = _ingest(**kwargs)

    assert deal["growth_assumptions"] == {"growth_type": "annual_compound", "annual_growth_rate": growth}
    assert deal["collection_loss_curve"] == [
        {"applies_to": "ALL", "start_period": "2026-08", "end_period": "2031-07", "loss_rate": loss}
    ]
    assert _curve_values(deal, "physical_vacancy_curve", "vacancy_rate") == {vacancy}
    assert _curve_values(deal, "loss_to_lease", "ltl_percent") == {0.0}
    assert [(row["category_name"], row["base_value"]) for row in deal["opex_table"]] == _EXPECTED_OPEX
    assert {row["calculation_type"] for row in deal["opex_table"]} == {"fixed_annual"}
    for absent in ("concession_schedule", "replacement_reserves", "exit_assumptions",
                   "fund_assumptions", "purchase_assumptions", "debt_terms"):
        assert absent not in deal, absent


def test_ingest_economics_are_unchanged(full_deal):
    assert full_deal["exit_assumptions"] == {
        "exit_cap_rate": 0.055, "exit_month": "2031-07", "sale_cost_percent": 0.02,
    }
    assert full_deal["purchase_assumptions"] == {
        "purchase_price": 25_000_000, "equity_contribution": 6_250_000.0, "closing_costs": 0.0,
    }
    assert full_deal["debt_terms"]["commitment"] == pytest.approx(18_750_000)
    fund = full_deal["fund_assumptions"]
    assert (fund["acquisition_fee_pct"], fund["asset_management_fee_pct"], fund["disposition_fee_pct"]) == (
        0.01, 0.015, 0.01,
    )
    assert "annual_partnership_expenses" not in fund
    assert "partnership_closing_costs" not in fund


def test_explicit_fee_inputs_are_analyst_overrides():
    deal = _ingest(sale_cost_pct=0.03, acq_fee_pct=0.02, **FULL_ECONOMICS)
    entries = _entries(deal)

    assert deal["exit_assumptions"]["sale_cost_percent"] == 0.03
    assert deal["fund_assumptions"]["acquisition_fee_pct"] == 0.02
    assert entries["sale_cost_percent"]["source"] == "analyst"
    assert entries["sale_cost_percent"]["default_value"] == 0.02
    assert entries["acquisition_fee_pct"]["source"] == "analyst"


def test_assumption_overrides_kwarg_wins_over_explicit_inputs():
    deal = _ingest(rent_growth_rate=0.025, assumption_overrides={"rent_growth": {"value": 0.04, "note": "IC"}})
    growth = _entries(deal)["rent_growth"]

    assert deal["growth_assumptions"]["annual_growth_rate"] == 0.04
    assert growth["override"] == {"value": 0.04, "note": "IC"}
    assert deal["metadata"]["ingest_gate"]["assumed_defaults"][0]["field"] == "collection_loss_curve.loss_rate"


# ── validator gates ──────────────────────────────────────────────────────


def test_validator_warns_once_per_review_required_entry():
    deal = _ingest(year_built=1984)
    report = validate_deal(deal)

    assert report.status == "PASS"
    warnings = [i for i in report.issues if i.code == "ASSUMPTION_REVIEW_REQUIRED"]
    flagged = {key for key, entry in _entries(deal).items() if entry["review_required"]}
    assert {w.path for w in warnings} == {f"/assumptions/entries/{key}" for key in flagged}
    assert len(warnings) == len(flagged)
    assert {w.severity for w in warnings} == {"WARNING"}
    assert "rent_growth" in flagged and "physical_vacancy" not in flagged
    assert not [i for i in report.issues if i.severity == "ERROR"]


def test_overridden_entries_do_not_warn():
    deal = _ingest(year_built=1984)
    resolved = resolve_assumptions(deal, overrides={"rent_growth": 0.025})
    paths = {i.path for i in validate_deal(resolved).issues if i.code == "ASSUMPTION_REVIEW_REQUIRED"}

    assert "/assumptions/entries/rent_growth" not in paths
    assert "/assumptions/entries/collection_loss" in paths


# ── market candidates are recorded, never applied ────────────────────────


class _SyntheticMarket:
    def __init__(self):
        self.calls = []

    def resolve(self, metric, geo, as_of=None):
        self.calls.append((metric, geo, as_of))
        if metric == "rent_growth_forecast":
            observation = {"provider": "synthetic", "value": 0.021, "unit": "decimal_yoy",
                           "as_of": "2026-09-30", "confidence": "medium"}
            return observation, [observation, {"provider": "synthetic-msa", "value": 0.024}]
        return None, []


def test_market_results_are_recorded_as_candidates_without_changing_values():
    deal = _ingest(metro_slug="synthetic_metro")
    market = _SyntheticMarket()
    resolved = resolve_assumptions(deal, market=market)

    assert [call[0] for call in market.calls] == [
        "rent_growth_forecast", "vacancy_current", "vacancy_stabilized", "concession_rate", "market_cap_rate",
    ]
    assert all(call[1] == {"msa": {"metro_slug": "synthetic_metro"}} and call[2] is None for call in market.calls)
    growth = _entries(resolved)["rent_growth"]
    assert growth["value"] == 0.03
    assert resolved["growth_assumptions"]["annual_growth_rate"] == 0.03
    market_candidates = [c for c in growth["candidates"] if c["source"].startswith("market:")]
    assert [(c["source"], c["value"]) for c in market_candidates] == [
        ("market:synthetic", 0.021), ("market:synthetic-msa", 0.024),
    ]
    assert market_candidates[0]["as_of"] == "2026-09-30"
    assert "ADR 0003 precedence would select market:synthetic" in growth["basis"]
    stripped = {k: v for k, v in resolved.items() if k != "assumptions"}
    assert stripped == {k: v for k, v in deal.items() if k != "assumptions"}


# ── --partial-json staleness fix ─────────────────────────────────────────


def test_partial_json_reresolves_and_records_analyst_overrides():
    from runs.ingest_deal import apply_partial_and_reresolve

    base = _ingest()
    assumed = {e["field"] for e in base["metadata"]["ingest_gate"]["assumed_defaults"]}
    assert "growth_assumptions.annual_growth_rate" in assumed

    merged = apply_partial_and_reresolve(base, {"growth_assumptions": {"annual_growth_rate": 0.025}})
    gate = merged["metadata"]["ingest_gate"]

    assert [e["field"] for e in gate["assumed_defaults"]] == ["collection_loss_curve.loss_rate"]
    growth = _entries(merged)["rent_growth"]
    assert growth["source"] == "analyst"
    assert growth["override"] == {"value": 0.025, "note": "supplied by --partial-json"}
    assert growth["default_value"] == 0.03
    flags = merged["metadata"]["intake_sanity_flags"]
    assert "assumed_default_annual_growth_rate" not in flags
    assert "assumed_default_loss_rate" in flags
    assert not [
        i for i in validate_deal(merged).issues
        if i.code == "ASSUMED_DEFAULT" and "annual_growth_rate" in i.message
    ]


def test_partial_json_value_equal_to_the_default_is_still_an_analyst_value():
    from runs.ingest_deal import apply_partial_and_reresolve

    merged = apply_partial_and_reresolve(
        _ingest(), {"collection_loss_curve": [
            {"applies_to": "ALL", "start_period": "2026-08", "end_period": "2031-07", "loss_rate": 0.005}
        ]},
    )
    loss = _entries(merged)["collection_loss"]
    assert loss["source"] == "analyst"
    assert loss["override"]["note"] == "supplied by --partial-json"
    assert [e["field"] for e in merged["metadata"]["ingest_gate"]["assumed_defaults"]] == [
        "growth_assumptions.annual_growth_rate"
    ]


def test_cli_partial_json_and_assumption_overrides(tmp_path, monkeypatch, capsys):
    from runs import ingest_deal

    partial = tmp_path / "partial.json"
    partial.write_text(json.dumps({"growth_assumptions": {"annual_growth_rate": 0.025}}))
    overrides = tmp_path / "overrides.json"
    overrides.write_text(json.dumps({"collection_loss": {"value": 0.015, "note": "T12 bad debt"}}))
    output = tmp_path / "deal.json"
    monkeypatch.setattr(sys, "argv", [
        "ingest_deal.py", "--property-id", "Synthetic CLI", "--rent-roll", str(ROLL), "--t12", str(T12),
        "--start", "2026-08", "--end", "2031-07", "--deal-type", "value_add", "--msa", "12345",
        "--submarket", "Synthetic North", "--metro-slug", "synthetic_metro",
        "--partial-json", str(partial), "--assumption-overrides", str(overrides), "--output", str(output),
    ])
    assert ingest_deal.main() == 0
    deal = json.loads(output.read_text())

    assert deal["deal_profile"] == {
        "deal_type": "value_add",
        "msa": {"cbsa_code": "12345", "metro_slug": "synthetic_metro"},
        "submarket": {"name": "Synthetic North"},
    }
    entries = _entries(deal)
    assert entries["collection_loss"]["override"] == {"value": 0.015, "note": "T12 bad debt"}
    assert entries["rent_growth"]["override"]["note"] == "supplied by --partial-json"
    assert deal["metadata"]["ingest_gate"]["assumed_defaults"] == []
    assert deal["growth_assumptions"]["annual_growth_rate"] == 0.025
    assert deal["collection_loss_curve"][0]["loss_rate"] == 0.015


def test_cli_rejects_reserved_deal_type(tmp_path, monkeypatch, capsys):
    from runs import ingest_deal

    monkeypatch.setattr(sys, "argv", [
        "ingest_deal.py", "--property-id", "Synthetic CLI", "--rent-roll", str(ROLL), "--t12", str(T12),
        "--start", "2026-08", "--end", "2031-07", "--deal-type", "lease_up",
        "--output", str(tmp_path / "deal.json"),
    ])
    assert ingest_deal.main() == 2
    assert "reserved" in capsys.readouterr().err
