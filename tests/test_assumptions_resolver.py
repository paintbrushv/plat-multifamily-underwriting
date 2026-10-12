"""ADR 0003 slice 2: deal_profile + assumptions schema and resolver v1.

The resolver records provenance and candidates for every v1 assumption key
and writes resolved values into the existing canonical fields. Slice 2 changed
no numbers. Slice 4 (tests/test_assumptions_curves.py) intentionally changes
the ingest values of rent_growth, collection_loss, physical_vacancy,
loss_to_lease and concessions; the expectations below that pin those keys
were updated with it.
"""
from __future__ import annotations

import copy
import json
import sys
from decimal import Decimal
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
    "r_and_m_payroll_credit",
    "replacement_reserve_per_unit",
    "immediate_capex",
    "recurring_capex_per_unit",
    "recurring_capex_growth",
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


def _v1_keys(entries):
    """Entry keys other than the per-line opex keys (ADR 0003 slice 5)."""
    return {key for key in entries if not key.startswith("opex.")}


# ── provenance and candidates ────────────────────────────────────────────


@pytest.fixture(scope="module")
def full_deal():
    return _ingest(year_built=1984, **FULL_ECONOMICS)


def test_every_stabilized_key_is_resolved_with_the_registry_shape(full_deal):
    entries = _entries(full_deal)
    assert _v1_keys(entries) == STABILIZED_KEYS
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
        # Slice 4: ADR 0003 §3 precedence (house long-run growth, T12 bad debt
        # and concessions, vacancy and LTL curves to the vintage levels).
        ("rent_growth", "house:long_run_rent_growth", [0.025, 0.025, 0.025, 0.025],
         ["house:long_run_rent_growth"]),
        ("collection_loss", "deal:t12", 0.015,
         ["deal:t12", "house:vintage_1980s", "house:vintage_1980s"]),
        ("physical_vacancy", "house:vintage_1980s",
         {"start": 0.2, "stabilized": 0.0575, "window_months": 12},
         ["deal:rent_roll", "house:vintage_1980s", "house:vintage_1980s"]),
        ("loss_to_lease", "house:vintage_1980s",
         {"start": 0.08737, "floor": 0.01, "burn_off_months": 12,
          "start_by_cohort": {"1br1ba": 0.117547, "2br1ba": 0.076809, "2br2ba": 0.067308}},
         ["deal:rent_roll", "house:vintage_1980s"]),
        ("concessions", "deal:t12", 0.01, ["deal:t12", "house:vintage_1980s", "house:vintage_1980s"]),
        ("insurance_per_unit", "deal:t12", 25_200.0,
         ["deal:t12", "house:year1_insurance_floor", "engine_fallback"]),
        # R&M on the floor basis: $195,000 of R&M plus $81,000 of turnover lines, over 10 units.
        ("r_and_m_per_unit", "deal:t12", 27_600.0, ["deal:t12", "house:vintage_1980s", "engine_fallback"]),
        # The house lender-style reserve is written when the canonical carries none.
        ("replacement_reserve_per_unit", "house:replacement_reserve", 250.0,
         ["house:replacement_reserve", "engine_fallback"]),
        # The capex buckets: nothing at close without evidence; recurring capex at the
        # low end of the 1980s band (stabilized), growing like R&M.
        ("immediate_capex", "house:deferred_capex", 0.0, ["house:deferred_capex"]),
        ("recurring_capex_per_unit", "house:recurring_capex_vintage", 1_000.0, ["house:recurring_capex_vintage"]),
        ("recurring_capex_growth", "house:opex_growth_table", 0.03, ["house:opex_growth_table"]),
        ("exit_cap_rate", "analyst", 0.055, ["analyst", "house:exit_cap_spread", "engine_fallback"]),
        ("sale_cost_percent", "house:backsolve_policy", 0.02, ["house:backsolve_policy", "engine_fallback"]),
        ("acquisition_fee_pct", "house:backsolve_policy", 0.01, ["house:backsolve_policy", "engine_fallback"]),
        ("asset_management_fee_pct", "house:backsolve_policy", 0.015,
         ["house:backsolve_policy", "engine_fallback"]),
        ("disposition_fee_pct", "house:backsolve_policy", 0.01, ["house:backsolve_policy", "engine_fallback"]),
        ("annual_partnership_expenses", "engine_fallback", 0.0, ["house:backsolve_policy", "engine_fallback"]),
        ("partnership_closing_costs", "engine_fallback", 0.0, ["house:backsolve_policy", "engine_fallback"]),
        ("purchase_closing_cost_pct", "house:backsolve_policy", 0.015,
         ["house:backsolve_policy", "engine_fallback"]),
        ("loan_closing_costs", "engine_fallback", 0.0, ["house:backsolve_policy", "engine_fallback"]),
    ],
)
def test_provenance_and_candidates_per_key(full_deal, key, source, value, candidate_sources):
    entry = _entries(full_deal)[key]
    assert entry["source"] == source
    if isinstance(value, dict):
        assert entry["value"] == value
    else:
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
    vacancy = [(c["source"], c["value"]) for c in entries["physical_vacancy"]["candidates"]]
    assert ("house:vintage_1980s", float(house.default_physical_vacancy_rate(1984, strategy="cashflow"))) in vacancy


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
    for key in ("annual_partnership_expenses", "partnership_closing_costs"):
        assert "ADR 0003 precedence would select" in entries[key]["basis"], key
        assert "does not fill this field outside pricing" in entries[key]["basis"], key
    # These keys resolve by the ADR precedence itself (reserves and closing costs
    # now take the house value), so no note is left.
    for key in ("physical_vacancy", "insurance_per_unit", "r_and_m_per_unit", "rent_growth",
                "collection_loss", "loss_to_lease", "concessions", "replacement_reserve_per_unit",
                "purchase_closing_cost_pct"):
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
    assert _v1_keys(entries) == STABILIZED_KEYS | {"turn_cost_per_unit", "renovation_premium"}
    # Slice 4: T12 bad debt wins; value_add stabilizes over 24 months to a higher LTL floor.
    assert entries["collection_loss"]["value"] == pytest.approx(0.015)
    assert entries["collection_loss"]["source"] == "deal:t12"
    assert entries["physical_vacancy"]["value"]["window_months"] == 24
    assert entries["loss_to_lease"]["value"]["floor"] == pytest.approx(0.0125)
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
    assert growth["default_value"] == [0.025, 0.025, 0.025, 0.025]
    assert growth["override"] == {"value": 0.025, "note": "analyst override"}
    assert growth["source"] == "analyst"
    assert growth["review_required"] is False
    assert _sources(growth)[0] == "analyst"
    assert resolved["growth_assumptions"]["annual_growth_rate"] == 0.025

    vacancy = entries["physical_vacancy"]
    assert vacancy["override"] == {"value": 0.07, "note": "leasing report"}
    assert vacancy["default_value"] == pytest.approx(0.2)  # no year built: rent-roll level held flat
    assert {row["vacancy_rate"] for row in resolved["physical_vacancy_curve"]} == {0.07}

    reserves = entries["replacement_reserve_per_unit"]
    assert reserves["default_value"] == 250.0  # the house reserve needs no vintage
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
    # The target is on the floor basis: R&M ($195,000) plus turnover ($81,000).
    assert [row["base_value"] for row in adjustment] == [20_000 * 10 - 276_000.0]
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
    assert growth["default_value"] == [0.025, 0.025, 0.025, 0.025]
    assert growth["override"]["note"] == "changed in the canonical after the last resolution"
    assert resolved["growth_assumptions"] == {"growth_type": "annual_compound", "annual_growth_rate": 0.02}


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
    assert set(ASSUMPTION_KEYS) == STABILIZED_KEYS | {"turn_cost_per_unit", "renovation_premium"}


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
    resolved = resolve_assumptions(minimal_deal_inputs)
    stripped = {k: v for k, v in resolved.items() if k not in ("assumptions", "deal_profile")}

    # The resolver fills only the capital plan (the three capex buckets, ADR 0003 §3);
    # every other field is the hand-authored one. The engine ignores the new sections.
    assert {k: v for k, v in stripped.items() if k != "capital_plan"} == minimal_deal_inputs
    baseline = run_underwriting({**copy.deepcopy(minimal_deal_inputs), "capital_plan": resolved["capital_plan"]})
    assert run_underwriting(resolved) == baseline
    assert run_underwriting(stripped) == baseline
    value_add = resolve_assumptions(minimal_deal_inputs, deal_profile=DealProfile("value_add", year_built=1984))
    assert run_underwriting(value_add) == run_underwriting(
        {**copy.deepcopy(minimal_deal_inputs), "capital_plan": value_add["capital_plan"]}
    )


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
    # The T12 fee ($180,000) is entered as percent_egr = fee / T12 EGI ($6,381,000).
    ("Property Management Fee", round(180_000 / 6_381_000, 6)),
]


def _curve_values(deal, table, key):
    return {row[key] for row in deal[table]}


_HOUSE_VECTOR = {"growth_type": "annual_compound", "annual_growth_rate": 0.025,
                 "annual_growth_rates": [0.025, 0.025, 0.025, 0.025]}
_LTL_STARTS = {0.117547, 0.076809, 0.067308}


# Slice 4 intentionally changed rent growth (house long-run vector instead of the
# 3% engine fallback), collection loss (T12 bad debt 1.5% instead of the vintage
# floor / 0.5% fallback), vacancy and LTL (curves instead of flat 20% / 0%) and
# concessions (T12 1% year-1 schedule instead of none). Opex and economics are
# unchanged.
@pytest.mark.parametrize(
    "kwargs, growth, loss, vacancy, ltl, concessions",
    [
        ({}, _HOUSE_VECTOR, 0.015, {0.2}, _LTL_STARTS, 0.01),
        ({"year_built": 1984}, _HOUSE_VECTOR, 0.015, None, None, 0.01),
        ({"rent_growth_rate": 0.025, "collection_loss_rate": 0.02, "vacancy_rate_override": 0.07},
         {"growth_type": "annual_compound", "annual_growth_rate": 0.025}, 0.02, {0.07}, _LTL_STARTS, 0.01),
    ],
)
def test_ingest_numbers(kwargs, growth, loss, vacancy, ltl, concessions):
    deal = _ingest(**kwargs)

    assert deal["growth_assumptions"] == growth
    assert deal["collection_loss_curve"] == [
        {"applies_to": "ALL", "start_period": "2026-08", "end_period": "2031-07", "loss_rate": loss}
    ]
    if vacancy is None:  # year built known: ramp from 20% to the 1980s typical 5.75%
        assert deal["physical_vacancy_curve"][0]["vacancy_rate"] == 0.2
        assert deal["physical_vacancy_curve"][-1]["vacancy_rate"] == 0.0575
    else:
        assert _curve_values(deal, "physical_vacancy_curve", "vacancy_rate") == vacancy
    if ltl is None:  # burns off to the 1980s LTL floor
        assert deal["loss_to_lease"][-1]["ltl_percent"] == 0.01
    else:  # no year built: each cohort's rent-roll LTL held flat
        assert _curve_values(deal, "loss_to_lease", "ltl_percent") == ltl
    assert [row["amount"] for row in deal["concession_schedule"]] == [concessions]
    assert [(row["category_name"], row["base_value"]) for row in deal["opex_table"]] == _EXPECTED_OPEX
    assert [row["calculation_type"] for row in deal["opex_table"]] == ["fixed_annual"] * 9 + ["percent_egr"]
    for absent in ("exit_assumptions", "fund_assumptions", "purchase_assumptions", "debt_terms"):
        assert absent not in deal, absent
    # The house lender-style reserve ($250/unit, any vintage) over the hold.
    assert deal["replacement_reserves"] == [
        {"start_period": "2026-08", "end_period": "2031-07", "annual_amount": 2_500.0}
    ]
    assert "capex_schedule" not in deal  # economic CapEx is informational


def test_ingest_economics_are_unchanged(full_deal):
    assert full_deal["exit_assumptions"] == {
        "exit_cap_rate": 0.055, "exit_month": "2031-07", "sale_cost_percent": 0.02,
    }
    # Closing costs: the house 1.5% of price (ingest's 0.0 is a placeholder).
    assert full_deal["purchase_assumptions"] == {
        "purchase_price": 25_000_000, "equity_contribution": 6_250_000.0, "closing_costs": 375_000.0,
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
    # Slice 4: collection loss now comes from T12 bad debt, so nothing is assumed.
    assert deal["metadata"]["ingest_gate"]["assumed_defaults"] == []


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
    # Slice 4: house-sourced curves are flagged; T12-sourced collection loss is not.
    assert "rent_growth" in flagged and "physical_vacancy" in flagged and "collection_loss" not in flagged
    assert not [i for i in report.issues if i.severity == "ERROR"]


def test_overridden_entries_do_not_warn():
    deal = _ingest(year_built=1984)
    resolved = resolve_assumptions(deal, overrides={"rent_growth": 0.025})
    paths = {i.path for i in validate_deal(resolved).issues if i.code == "ASSUMPTION_REVIEW_REQUIRED"}

    assert "/assumptions/entries/rent_growth" not in paths
    assert "/assumptions/entries/physical_vacancy" in paths


# ── market data is read through a registry with a Geo (slice 4) ─────────


class _RecordingMarket:
    """A registry stand-in that records calls and has no data."""

    def __init__(self):
        self.calls = []

    def resolve(self, metric, geo, as_of=None):
        self.calls.append((metric, geo, as_of))
        return None, []


def test_market_is_queried_with_a_geo_and_missing_data_falls_through():
    from engine.market_data import Geo

    deal = _ingest(metro_slug="synthetic_metro")
    market = _RecordingMarket()
    resolved = resolve_assumptions(deal, market=market)

    assert {call[0] for call in market.calls} == {
        "rent_growth_forecast", "rent_growth_long_run", "vacancy_current", "vacancy_stabilized",
        "concession_rate", "market_cap_rate",
    }
    assert all(call[1] == Geo(metro_slug="synthetic_metro") and call[2] is None for call in market.calls)
    without = resolve_assumptions(deal)
    strip = lambda d: {k: v for k, v in d.items() if k != "assumptions"}  # noqa: E731
    assert strip(without) == strip(resolved)
    assert {k: e["value"] for k, e in _entries(without).items()} == {
        k: e["value"] for k, e in _entries(resolved).items()
    }
    assert _entries(resolved)["rent_growth"]["source"] == "house:long_run_rent_growth"


# ── --partial-json staleness fix ─────────────────────────────────────────


def test_partial_json_reresolves_and_records_analyst_overrides():
    from runs.ingest_deal import apply_partial_and_reresolve

    base = _ingest()
    assumed = {e["field"] for e in base["metadata"]["ingest_gate"]["assumed_defaults"]}
    assert "growth_assumptions.annual_growth_rate" in assumed

    merged = apply_partial_and_reresolve(base, {"growth_assumptions": {"annual_growth_rate": 0.025}})
    gate = merged["metadata"]["ingest_gate"]

    # Slice 4: collection loss comes from T12 bad debt, so it is not an assumed default.
    assert gate["assumed_defaults"] == []
    growth = _entries(merged)["rent_growth"]
    assert growth["source"] == "analyst"
    assert growth["override"] == {"value": 0.025, "note": "supplied by --partial-json"}
    assert growth["default_value"] == [0.025, 0.025, 0.025, 0.025]
    assert "annual_growth_rates" not in merged["growth_assumptions"]
    flags = merged["metadata"].get("intake_sanity_flags") or []
    assert "assumed_default_annual_growth_rate" not in flags
    assert "assumed_default_loss_rate" not in flags
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


# ── gap fill: reserves, closing costs, exit cap, renovation premium ──────


def test_house_reserve_is_written_when_the_canonical_has_none():
    deal = _ingest(year_built=1984, deal_type="value_add")
    entry = _entries(deal)["replacement_reserve_per_unit"]
    per_unit = float(house.HOUSE_REPLACEMENT_RESERVE_PER_UNIT)

    assert per_unit == 250.0  # lender-style, not the 1980s economic CapEx band
    assert (entry["source"], entry["value"], entry["review_required"]) == ("house:replacement_reserve", per_unit, True)
    assert deal["replacement_reserves"] == [
        {"start_period": "2026-08", "end_period": "2031-07", "annual_amount": per_unit * 10}
    ]
    assert "ADR 0003 precedence" not in entry["basis"]
    assert resolve_assumptions(deal) == deal
    # No guardrail warning: the house reserve is the floor.
    assert "guardrail" not in entry


def test_recurring_capex_by_vintage_is_written_below_noi_for_every_deal():
    deal = _ingest(year_built=1984, deal_type="value_add")
    entries = _entries(deal)
    entry = entries["recurring_capex_per_unit"]
    midpoint = float(house.default_recurring_capex_per_unit(1984, strategy="value_add"))

    assert midpoint == 1_750.0  # value-add: midpoint of the 1980s band
    assert (entry["source"], entry["value"], entry["review_required"]) == (
        "house:recurring_capex_vintage", midpoint, True)
    assert entry["override"] is None
    assert entry["detail"]["band_pick"] == "midpoint" and entry["detail"]["pca_review_required"] is True
    assert "max(recurring capex, replacement reserve)" in entry["basis"]
    assert deal["capital_plan"]["recurring_capex"] == {"per_unit_annual": midpoint, "growth_rate": 0.03}
    assert entries["recurring_capex_growth"]["source"] == "house:opex_growth_table"
    assert deal["capital_plan"]["immediate_capex"] == {"amount": 0.0, "funding": "equity_at_close"}
    assert "capex_schedule" not in deal  # below NOI, never an operating capex line
    stabilized = _entries(_ingest(year_built=1984))["recurring_capex_per_unit"]
    assert stabilized["value"] == 1_000.0  # stabilized: the low end of the band
    deferred = _entries(_ingest(year_built=1984, deferred_maintenance=True))["recurring_capex_per_unit"]
    assert deferred["value"] == 2_500.0  # deferred maintenance: the high end
    assert resolve_assumptions(deal) == deal


def test_recurring_capex_precedence_pca_then_analyst_then_house():
    pca = {"recurring_capex_per_unit": 1_400, "immediate_capex": 250_000, "as_of": "2026-09-01",
           "reference": "Synthetic PCA"}
    with_pca = _ingest(year_built=1984, pca=pca, recurring_capex_per_unit=900, immediate_capex=100_000)
    entries = _entries(with_pca)
    recurring = entries["recurring_capex_per_unit"]
    # PCA > analyst: the analyst figure is kept as a superseded candidate.
    assert (recurring["source"], recurring["value"], recurring["override"]) == ("deal:pca", 1_400.0, None)
    assert [c["source"] for c in recurring["candidates"]] == ["analyst", "deal:pca", "house:recurring_capex_vintage"]
    assert recurring["candidates"][0]["value"] == 900.0
    assert recurring["review_required"] is False and recurring["detail"]["pca_review_required"] is False
    immediate = entries["immediate_capex"]
    assert (immediate["source"], immediate["value"]) == ("deal:pca", 250_000.0)
    assert with_pca["capital_plan"]["recurring_capex"]["per_unit_annual"] == 1_400.0
    assert with_pca["capital_plan"]["immediate_capex"]["amount"] == 250_000.0
    assert resolve_assumptions(with_pca) == with_pca

    # Analyst > house: no PCA, so the analyst figures are overrides.
    analyst = _entries(_ingest(year_built=1984, recurring_capex_per_unit=900, immediate_capex=100_000))
    assert (analyst["recurring_capex_per_unit"]["source"], analyst["recurring_capex_per_unit"]["value"]) == (
        "analyst", 900.0)
    assert analyst["recurring_capex_per_unit"]["default_value"] == 1_000.0
    # Built before 2000 with no PCA: still a review prompt, even with the analyst's figure.
    assert analyst["recurring_capex_per_unit"]["review_required"] is True
    assert (analyst["immediate_capex"]["source"], analyst["immediate_capex"]["value"]) == ("analyst", 100_000.0)
    # An explicit later edit still wins over the PCA (shown as a user edit, PCA as default).
    edited = resolve_assumptions(with_pca, overrides={"recurring_capex_per_unit": {"value": 1_600, "note": "IC"}})
    entry = _entries(edited)["recurring_capex_per_unit"]
    assert (entry["source"], entry["value"], entry["default_value"]) == ("analyst", 1_600.0, 1_400.0)


def test_unknown_vintage_falls_back_to_the_reserve_and_is_flagged():
    deal = _ingest()
    entry = _entries(deal)["recurring_capex_per_unit"]
    assert (entry["source"], entry["value"], entry["review_required"]) == ("house:replacement_reserve", 250.0, True)
    assert "Year built unknown" in entry["basis"]
    assert deal["capital_plan"]["recurring_capex"]["per_unit_annual"] == 250.0


def test_the_retired_economic_capex_opt_in_becomes_the_analysts_recurring_capex():
    deal = _ingest(year_built=1984)
    legacy = copy.deepcopy(deal)
    legacy.pop("capital_plan")
    legacy["assumptions"]["entries"].pop("recurring_capex_per_unit")
    legacy["assumptions"]["entries"].pop("recurring_capex_growth")
    legacy["capex_schedule"] = [{"category": "Economic CapEx (analyst opt-in)", "capex_type": "reserve",
                                 "amount_per_unit": 1_200.0, "timing": "annual"}]
    migrated = resolve_assumptions(legacy)
    entry = _entries(migrated)["recurring_capex_per_unit"]
    assert (entry["source"], entry["value"]) == ("analyst", 1_200.0)
    assert "capex_schedule" not in migrated  # charged once, below NOI
    assert migrated["capital_plan"]["recurring_capex"]["per_unit_annual"] == 1_200.0
    # The retired key is still accepted in overrides.
    aliased = resolve_assumptions(deal, overrides={"economic_capex_per_unit": 1_100})
    assert _entries(aliased)["recurring_capex_per_unit"]["value"] == 1_100.0


def _capex_warnings(deal, code="CAPEX_PCA_MISSING"):
    return [i for i in validate_deal(deal).issues if i.code == code]


def test_pre_2000_vintage_without_a_pca_is_flagged_for_capex_review(minimal_deal_inputs):
    deal = copy.deepcopy(minimal_deal_inputs)
    assert _capex_warnings(deal) == []  # no year built: nothing to say
    deal["metadata"]["year_built"] = 1984
    warnings = _capex_warnings(deal)
    assert [(w.severity, w.path) for w in warnings] == [("WARNING", "/capital_plan/pca")]
    assert "PCA" in warnings[0].message and "recurring_capex_per_unit" in warnings[0].message
    assert validate_deal(deal).status == "PASS"

    # A capex budget no longer silences it: only PCA evidence does.
    budgeted = copy.deepcopy(deal)
    budgeted["capex_schedule"] = [{"category": "Roofs", "capex_type": "one_time", "month": "2027-03",
                                   "amount": 250_000}]
    assert len(_capex_warnings(budgeted)) == 1
    with_pca = copy.deepcopy(deal)
    with_pca["capital_plan"] = {"pca": {"recurring_capex_per_unit": 1_100.0}}
    assert _capex_warnings(with_pca) == []
    newer = copy.deepcopy(deal)
    newer["metadata"]["year_built"] = 2004
    assert _capex_warnings(newer) == []

    # The ingest gate carries the same review flag; a PCA clears it.
    ingested = _ingest(year_built=1984)
    assert "capex_pca_missing_pre_2000_review" in ingested["metadata"]["intake_sanity_flags"]
    assert _entries(ingested)["recurring_capex_per_unit"]["review_required"] is True
    cleared = _ingest(year_built=1984, pca={"recurring_capex_per_unit": 1_100})
    assert "capex_pca_missing_pre_2000_review" not in cleared["metadata"].get("intake_sanity_flags", [])


def test_deferred_maintenance_without_an_immediate_budget_is_flagged():
    # Unknown vintage: no band to size the house placeholder, so nothing is budgeted.
    deal = _ingest(deferred_maintenance=True)
    assert deal["deal_profile"]["deferred_maintenance"] is True
    assert [w.path for w in _capex_warnings(deal, "DEFERRED_CAPEX_UNBUDGETED")] == ["/capital_plan/immediate_capex"]
    assert _entries(deal)["immediate_capex"]["review_required"] is True
    # With a vintage, the house placeholder budgets one year of the band high end
    # and is flagged for a PCA (tests/test_deferred_maintenance_policy.py).
    placeholder = _ingest(year_built=2005, deferred_maintenance=True)
    assert _capex_warnings(placeholder, "DEFERRED_CAPEX_UNBUDGETED") == []
    assert [w.path for w in _capex_warnings(placeholder, "DEFERRED_CAPEX_PLACEHOLDER")] == [
        "/capital_plan/immediate_capex"]
    budgeted = _ingest(year_built=2005, deferred_maintenance=True, immediate_capex=300_000)
    assert _capex_warnings(budgeted, "DEFERRED_CAPEX_UNBUDGETED") == []
    assert _capex_warnings(budgeted, "DEFERRED_CAPEX_PLACEHOLDER") == []
    assert validate_deal(budgeted).status == "PASS"


def test_analyst_reserve_and_closing_costs_win(minimal_deal_inputs):
    deal = copy.deepcopy(minimal_deal_inputs)
    deal["metadata"]["year_built"] = 1984
    resolved = resolve_assumptions(deal)
    entries = _entries(resolved)

    assert resolved["replacement_reserves"] == minimal_deal_inputs["replacement_reserves"]
    assert entries["replacement_reserve_per_unit"]["source"] == "analyst"
    assert resolved["purchase_assumptions"]["closing_costs"] == 150_000
    assert entries["purchase_closing_cost_pct"]["source"] == "analyst"
    assert entries["purchase_closing_cost_pct"]["default_value"] == 0.015

    explicit = _ingest(year_built=1984, purchase_price=10_000_000,
                       assumption_overrides={"purchase_closing_cost_pct": 0.01})
    assert explicit["purchase_assumptions"]["closing_costs"] == 100_000.0
    assert _entries(explicit)["purchase_closing_cost_pct"]["source"] == "analyst"


def test_house_closing_costs_fill_a_purchase_section_without_them(minimal_deal_inputs):
    deal = _ingest(purchase_price=10_000_000)
    entry = _entries(deal)["purchase_closing_cost_pct"]
    assert deal["purchase_assumptions"]["closing_costs"] == 150_000.0
    assert (entry["source"], entry["value"], entry["review_required"]) == ("house:backsolve_policy", 0.015, True)
    assert resolve_assumptions(deal) == deal

    hand = copy.deepcopy(minimal_deal_inputs)
    hand["purchase_assumptions"].pop("closing_costs")
    resolved = resolve_assumptions(hand)
    assert resolved["purchase_assumptions"]["closing_costs"] == 13_500_000 * 0.015
    assert _entries(resolved)["purchase_closing_cost_pct"]["source"] == "house:backsolve_policy"


def test_exit_cap_falls_back_to_going_in_plus_the_house_spread():
    deal = _ingest(year_built=1984, purchase_price=25_000_000, exit_month="2031-07", market=None)
    entry = _entries(deal)["exit_cap_rate"]
    going_in = (6_381_000 - 3_087_000) / 25_000_000  # T12 NOI / price

    assert house.HOUSE_EXIT_CAP_SPREAD == Decimal("0.0050")
    assert entry["source"] == "house:exit_cap_spread"
    assert entry["value"] == pytest.approx(going_in + 0.005)
    assert entry["review_required"] is True
    assert deal["exit_assumptions"]["exit_cap_rate"] == entry["value"]
    assert "going-in cap" in entry["basis"] and "50 bps" in entry["basis"]
    assert validate_schema(deal) == []
    assert resolve_assumptions(deal) == deal

    # The analyst still wins.
    analyst = _ingest(purchase_price=25_000_000, exit_cap_rate=0.06, market=None)
    assert _entries(analyst)["exit_cap_rate"]["source"] == "analyst"
    # A priced deal with no exit inputs gets an exit at analysis end (ingest),
    # and its cap resolves through the same going-in + spread fallback.
    no_exit_inputs = _ingest(purchase_price=25_000_000, market=None)
    assert no_exit_inputs["exit_assumptions"]["exit_month"] == "2031-07"
    assert _entries(no_exit_inputs)["exit_cap_rate"]["source"] == "house:exit_cap_spread"
    assert no_exit_inputs["exit_assumptions"]["exit_cap_rate"] == pytest.approx(going_in + 0.005)


_PROGRAM = {
    "program_id": "interior_1br", "program_name": "1BR interior", "target_cohort": "1br1ba",
    "output_cohort": "1br1ba_reno", "renovation_cost_per_unit": 9_000, "rent_premium_monthly": 150.0,
    "downtime_days": 30, "strategy": "proactive", "start_month": "2027-01", "monthly_pace": 1, "max_units": 2,
}


class _PremiumMarket:
    """A registry stand-in with only a renovation premium."""

    def __init__(self, value):
        self.value = value

    def resolve(self, metric, geo, as_of=None):
        from datetime import date

        from engine.market_data import SyntheticProvider

        if metric != "renovation_premium":
            return None, []
        provider = SyntheticProvider({(metric, geo): self.value}, as_of=date(2026, 6, 30), confidence="medium")
        return provider.get(metric, geo), []


def test_renovation_premium_without_programs_is_flagged():
    deal = _ingest(year_built=1984, deal_type="value_add", market=None)
    entry = _entries(deal)["renovation_premium"]
    assert (entry["source"], entry["value"], entry["review_required"]) == ("engine_fallback", None, True)
    assert "no renovation_programs" in entry["basis"] and "no house premium" in entry["basis"]
    assert "renovation_premium" not in _entries(_ingest(year_built=1984))  # value-add only

    with_market = _ingest(year_built=1984, deal_type="value_add", metro_slug="synthetic_metro",
                          market=_PremiumMarket(180.0))
    entry = _entries(with_market)["renovation_premium"]
    assert entry["value"] == 180.0 and entry["source"].startswith("market:")
    assert entry["review_required"] is True  # still no program to apply it to
    assert "no renovation_programs" in entry["basis"]


def test_renovation_premium_precedence_program_then_comps_then_market():
    base = _ingest(year_built=1984, deal_type="value_add", metro_slug="synthetic_metro", market=None)
    market = _PremiumMarket(180.0)

    # A program with its own premium is the analyst's value.
    deal = copy.deepcopy(base)
    deal["renovation_programs"] = [dict(_PROGRAM)]
    entry = _entries(resolve_assumptions(deal, market=market))["renovation_premium"]
    assert (entry["source"], entry["value"], entry["default_value"]) == ("analyst", 150.0, 180.0)

    # A program without a premium is filled: comps evidence, else the market metric.
    deal["renovation_programs"][0]["rent_premium_monthly"] = None
    resolved = resolve_assumptions(deal, market=market)
    assert resolved["renovation_programs"][0]["rent_premium_monthly"] == 180.0
    entry = _entries(resolved)["renovation_premium"]
    assert entry["source"].startswith("market:") and entry["review_required"] is False

    deal["metadata"]["ingest_gate"]["evidence"]["comps"] = {
        "renovated_effective_rent": 1_400.0, "classic_effective_rent": 1_235.0,
    }
    resolved = resolve_assumptions(deal, market=market)
    assert resolved["renovation_programs"][0]["rent_premium_monthly"] == 165.0
    assert _entries(resolved)["renovation_premium"]["source"] == "deal:comps"
    assert resolve_assumptions(resolved, market=market) == resolved

    # An override sets every program's premium.
    overridden = resolve_assumptions(resolved, overrides={"renovation_premium": 200}, market=market)
    assert overridden["renovation_programs"][0]["rent_premium_monthly"] == 200.0
    assert _entries(overridden)["renovation_premium"]["source"] == "analyst"


# ── null clears an override; cohort overrides only where supported ──────


@pytest.mark.parametrize("cleared", [None, {"value": None}, {"value": None, "note": "back to default"}])
def test_null_override_clears_the_override(cleared):
    deal = _ingest(year_built=1984)
    overridden = resolve_assumptions(deal, overrides={"rent_growth": 0.04, "collection_loss": 0.03})
    assert _entries(overridden)["rent_growth"]["source"] == "analyst"

    cleared_deal = resolve_assumptions(overridden, overrides={"rent_growth": cleared})
    growth = _entries(cleared_deal)["rent_growth"]
    assert growth["override"] is None
    assert growth["source"] == "house:long_run_rent_growth"
    assert growth["value"] == growth["default_value"] == [0.025, 0.025, 0.025, 0.025]
    assert cleared_deal["growth_assumptions"]["annual_growth_rates"] == [0.025] * 4
    # Only the cleared key moves; a later plain re-resolve keeps it cleared.
    assert _entries(cleared_deal)["collection_loss"]["override"]["value"] == 0.03
    assert resolve_assumptions(cleared_deal) == cleared_deal
    assert {k: v for k, v in cleared_deal.items() if k != "assumptions"} == {
        k: v for k, v in resolve_assumptions(deal, overrides={"collection_loss": 0.03}).items()
        if k != "assumptions"
    }


def test_null_override_reverts_a_hand_authored_value(minimal_deal_inputs):
    resolved = resolve_assumptions(minimal_deal_inputs, overrides={"collection_loss": None})
    entry = _entries(resolved)["collection_loss"]
    assert entry["override"] is None
    assert entry["source"] == "house:default_collection_loss"
    assert {row["loss_rate"] for row in resolved["collection_loss_curve"]} == {entry["default_value"]}


def test_normalize_overrides_marks_a_clear_and_rejects_unsupported_cohorts():
    from engine.assumptions import normalize_overrides
    from engine.assumptions.resolver import COHORT_OVERRIDE_KEYS

    assert normalize_overrides({"rent_growth": None})["rent_growth"]["clear"] is True
    assert COHORT_OVERRIDE_KEYS == {"physical_vacancy", "loss_to_lease"}
    with pytest.raises(AssumptionOverrideError, match="does not support cohort_overrides"):
        normalize_overrides({"concessions": {"value": 0.01, "cohort_overrides": {"1br1ba": 0.02}}})
    accepted = normalize_overrides({"physical_vacancy": {"value": 0.05, "cohort_overrides": {"1br1ba": 0.08}}})
    assert accepted["physical_vacancy"]["cohort_overrides"] == {"1br1ba": 0.08}


# ── asset class -> star tier ─────────────────────────────────────────────


@pytest.mark.parametrize("asset_class, star_tier", [("A", "4_5"), ("B", "3"), ("C", "1_2")])
def test_asset_class_defaults_the_star_tier(asset_class, star_tier):
    from engine.assumptions.curves import profile_geo

    profile = DealProfile(asset_class=asset_class, msa={"metro_slug": "synthetic_metro"})
    assert profile.star_tier == star_tier
    assert profile.to_dict()["star_tier"] == star_tier
    assert DealProfile.from_dict(profile.to_dict()) == profile
    assert profile_geo(profile).star_tier is not None
    # An explicit star tier wins.
    assert DealProfile(asset_class=asset_class, star_tier="3").star_tier == "3"
