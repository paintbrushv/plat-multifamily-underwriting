"""ADR 0003 slice 6: guardrail modes.

One module (engine/assumptions/guardrails.py) defines the house floors.
Underwrite mode warns and never changes a value; pricing mode raises each
value below its floor (per segment for curves) and discloses the raise.
Backsolve uses the same floors, and its solved canonical carries a pricing-mode
``assumptions`` section that matches the priced values.

Synthetic deals only.
"""
from __future__ import annotations

import copy
import json
from decimal import Decimal

import pytest

from engine import backsolve
from engine import underwriting_policy as house
from engine.assumptions import (
    AssumptionEntry,
    DealProfile,
    apply_guardrails,
    resolve_assumptions,
)
from engine.assumptions import guardrails
from engine.assumptions.guardrails import GuardrailProfile, enforce_floors, house_floors
from engine.modules.util import dec
from engine.validator import validate_deal, validate_schema

YEAR = 1984  # 1980s bucket: vacancy 5%, LTL 1%, collection 0.75%, concessions 0.75%, R&M $900, reserve $1,000
PROFILE = DealProfile("stabilized", year_built=YEAR)


def _strip_guardrail(canonical):
    out = copy.deepcopy(canonical)
    for entry in ((out.get("assumptions") or {}).get("entries") or {}).values():
        entry.pop("guardrail", None)
    return out


def _below_floor_deal(minimal_deal_inputs):
    """The conftest deal with a 1984 vintage and values below several floors."""
    deal = copy.deepcopy(minimal_deal_inputs)
    deal["metadata"]["year_built"] = YEAR
    deal["physical_vacancy_curve"] = [
        {"cohort_id": "1BR", "start_period": "2026-07", "end_period": "2027-06", "vacancy_rate": 0.09},
        {"cohort_id": "1BR", "start_period": "2027-07", "end_period": "2031-06", "vacancy_rate": 0.03},
        {"cohort_id": "2BR", "start_period": "2026-07", "end_period": "2031-06", "vacancy_rate": 0.04},
    ]
    deal["collection_loss_curve"] = [
        {"applies_to": "ALL", "start_period": "2026-07", "end_period": "2027-06", "loss_rate": 0.002},
        {"applies_to": "ALL", "start_period": "2027-07", "end_period": "2031-06", "loss_rate": 0.02},
    ]
    return deal


# ── floors ───────────────────────────────────────────────────────────────


def test_floors_reuse_the_underwriting_policy_functions():
    gp = GuardrailProfile("value_add", YEAR)
    assert gp.physical_vacancy() == house.default_physical_vacancy_rate(YEAR, strategy="value_add")
    assert gp.loss_to_lease() == house.default_loss_to_lease_rate(YEAR, strategy="value_add")
    assert gp.collection_loss() == house.default_collection_loss_rate(YEAR, strategy="value_add")
    assert gp.year1_concessions() == house.default_year1_concessions_rate(YEAR, strategy="value_add")
    assert gp.r_and_m_per_unit() == house.default_r_and_m_per_unit(YEAR, strategy="value_add")
    assert gp.replacement_reserve_per_unit() == house.default_replacement_reserve_per_unit(
        YEAR, strategy="value_add")
    assert gp.insurance_per_unit() == house.DEFAULT_YEAR1_INSURANCE_PER_UNIT
    assert GuardrailProfile.coerce(DealProfile("value_add", year_built=YEAR)) == gp
    assert GuardrailProfile.coerce({"deal_type": "value_add", "year_built": YEAR}) == gp


def test_unknown_vintage_leaves_only_the_insurance_floor(minimal_deal_inputs):
    floors, _ = house_floors(minimal_deal_inputs, {"deal_type": "stabilized"})
    assert floors["insurance_per_unit"].value == Decimal("600")
    for key in ("loss_to_lease", "collection_loss", "concessions", "r_and_m_per_unit",
                "replacement_reserve_per_unit"):
        assert floors[key].value is None, key
    assert floors["physical_vacancy"].value == Decimal("0")


def test_context_anchors_raise_but_never_lower_a_floor(minimal_deal_inputs):
    deal = _below_floor_deal(minimal_deal_inputs)
    context = {
        "broker_snapshot": {"underwriting_notes": {"loss_to_lease": "0.5%", "bad_debt": "3%"}},
        "trailing_actuals": {"gross_potential_rent": 1_000_000.0, "vacancy_loss": -80_000.0},
    }
    floors, summary = house_floors(deal, PROFILE, context)
    assert floors["loss_to_lease"].value == Decimal("0.010")  # broker 0.5% is below the vintage floor
    assert floors["collection_loss"].value == Decimal("0.03")
    assert floors["collection_loss"].source == "deal:om"
    assert floors["physical_vacancy"].value == Decimal("0.08")
    assert floors["physical_vacancy"].source == "deal:t12"
    assert summary["base_case_selected_anchor"] == ["t12_vacancy_gpr"]


# ── underwrite mode ──────────────────────────────────────────────────────


def test_underwrite_warns_without_changing_anything(minimal_deal_inputs):
    resolved = resolve_assumptions(_below_floor_deal(minimal_deal_inputs), deal_profile=PROFILE)
    before = copy.deepcopy(resolved)
    out, findings = apply_guardrails(resolved, mode="underwrite", profile=PROFILE)

    assert resolved == before  # input untouched
    assert _strip_guardrail(out) == _strip_guardrail(resolved)  # nothing but the findings differs
    assert {f["key"] for f in findings} == {
        "physical_vacancy", "collection_loss", "concessions", "r_and_m_per_unit", "replacement_reserve_per_unit",
    }
    assert {f["action"] for f in findings} == {"warned"}
    assert all("applied" not in f for f in findings)
    vacancy = out["assumptions"]["entries"]["physical_vacancy"]["guardrail"]
    assert vacancy["floor"] == 0.05
    assert vacancy["status"] == "below_floor" and vacancy["action"] == "warned"
    assert vacancy["original"] == 0.03
    assert [(s["cohort_id"], s["original"]) for s in vacancy["segments"]] == [("1BR", 0.03), ("2BR", 0.04)]
    assert "guardrail" not in out["assumptions"]["entries"]["loss_to_lease"]
    assert validate_schema(out) == []


def test_resolver_underwrite_mode_records_findings_and_keeps_values(minimal_deal_inputs):
    deal = _below_floor_deal(minimal_deal_inputs)
    resolved = resolve_assumptions(deal, deal_profile=PROFILE)

    stripped = {k: v for k, v in resolved.items() if k not in ("assumptions", "deal_profile")}
    assert stripped == deal
    entries = resolved["assumptions"]["entries"]
    assert entries["r_and_m_per_unit"]["guardrail"]["original"] == 750.0
    assert entries["r_and_m_per_unit"]["guardrail"]["floor"] == 900.0
    assert entries["replacement_reserve_per_unit"]["guardrail"]["original"] == 250.0
    for entry in entries.values():
        assert AssumptionEntry.from_dict(entry).to_dict() == entry
    assert resolve_assumptions(resolved) == resolved  # idempotent


def test_validator_warns_guardrail_below_floor_and_adds_no_errors(minimal_deal_inputs):
    deal = _below_floor_deal(minimal_deal_inputs)
    baseline = validate_deal(deal)
    resolved = resolve_assumptions(deal, deal_profile=PROFILE)
    report = validate_deal(resolved)

    warnings = [i for i in report.issues if i.code == "GUARDRAIL_BELOW_FLOOR"]
    assert {w.path for w in warnings} == {
        f"/assumptions/entries/{key}/guardrail"
        for key in ("physical_vacancy", "collection_loss", "concessions", "r_and_m_per_unit",
                    "replacement_reserve_per_unit")
    }
    assert {w.severity for w in warnings} == {"WARNING"}
    assert report.status == baseline.status == "PASS"
    assert [i for i in report.issues if i.severity == "ERROR"] == []


# ── pricing mode ─────────────────────────────────────────────────────────


def test_pricing_enforces_per_segment_keeps_shape_and_records_overrides(minimal_deal_inputs):
    deal = _below_floor_deal(minimal_deal_inputs)
    overrides: list = []
    out, findings = apply_guardrails(deal, mode="pricing", profile=PROFILE,
                                     context={"policy_overrides": overrides})

    assert deal == _below_floor_deal(minimal_deal_inputs)
    assert [row["vacancy_rate"] for row in out["physical_vacancy_curve"]] == [0.09, 0.05, 0.05]
    assert [(r["start_period"], r["end_period"]) for r in out["physical_vacancy_curve"]] == [
        (r["start_period"], r["end_period"]) for r in deal["physical_vacancy_curve"]
    ]
    assert [row["loss_rate"] for row in out["collection_loss_curve"]] == [0.0075, 0.02]
    vacancy = [o for o in overrides if o["input"] == "physical_vacancy_curve"]
    assert [(o["cohort_id"], o["start_period"], o["original"], o["applied"]) for o in vacancy] == [
        ("1BR", "2027-07", 0.03, 0.05),
        ("2BR", "2026-07", 0.04, 0.05),
    ]
    assert all(o["assumption"] and o["guardrail"] == "house_floor" for o in overrides)
    assert len(overrides) == len(findings)
    assert {f["action"] for f in findings} == {"enforced"}


def test_pricing_leaves_values_at_or_above_floor_untouched(minimal_deal_inputs):
    deal = _below_floor_deal(minimal_deal_inputs)
    deal["physical_vacancy_curve"] = [dict(r, vacancy_rate=0.15) for r in deal["physical_vacancy_curve"]]
    out, findings = apply_guardrails(deal, mode="pricing", profile=PROFILE)
    assert out["physical_vacancy_curve"] == deal["physical_vacancy_curve"]
    assert out["loss_to_lease"] == deal["loss_to_lease"]
    assert "physical_vacancy" not in {f["key"] for f in findings}


@pytest.mark.parametrize(
    "key, mutate, check",
    [
        ("physical_vacancy",
         lambda d: d.update(physical_vacancy_curve=[dict(r, vacancy_rate=0.01) for r in d["physical_vacancy_curve"]]),
         lambda d: {r["vacancy_rate"] for r in d["physical_vacancy_curve"]} == {0.05}),
        ("loss_to_lease",
         lambda d: d.update(loss_to_lease=[dict(r, ltl_percent=0.002) for r in d["loss_to_lease"]]),
         lambda d: {r["ltl_percent"] for r in d["loss_to_lease"]} == {0.01}),
        ("collection_loss",
         lambda d: d.pop("collection_loss_curve"),
         lambda d: d["collection_loss_curve"] == [
             {"applies_to": "ALL", "start_period": "2026-07", "end_period": "2031-06", "loss_rate": 0.0075}]),
        ("concessions",
         lambda d: d.update(concession_schedule=[{
             "concession_id": "c", "start_month": "2026-07", "end_month": "2027-06",
             "concession_type": "free_months", "amount": 0.06, "applies_to_cohort": "ALL"}]),
         lambda d: [(r["concession_type"], r["amount"]) for r in d["concession_schedule"]] == [("pct_rent", 0.0075)]),
        ("insurance_per_unit",
         lambda d: next(r for r in d["opex_table"] if r["category_name"] == "Insurance").update(base_value=40_000),
         lambda d: next(r for r in d["opex_table"] if r["category_name"] == "Insurance")["base_value"] == 60_000.0),
        ("r_and_m_per_unit",
         lambda d: None,
         lambda d: next(r for r in d["opex_table"] if r["category_name"] == guardrails.R_AND_M_TOP_UP_CATEGORY)[
             "base_value"] == 15_000.0),
        ("replacement_reserve_per_unit",
         lambda d: None,
         lambda d: [r["annual_amount"] for r in d["replacement_reserves"]] == [100_000.0]),
    ],
)
def test_each_floor_type(minimal_deal_inputs, key, mutate, check):
    deal = _below_floor_deal(minimal_deal_inputs)
    mutate(deal)
    warned = enforce_floors(copy.deepcopy(deal), mode="underwrite", profile=PROFILE, keys=(key,))
    assert warned and {f["key"] for f in warned} == {key}
    assert {f["action"] for f in warned} == {"warned"}

    priced = copy.deepcopy(deal)
    enforced = enforce_floors(priced, mode="pricing", profile=PROFILE, keys=(key,))
    assert [f["original"] for f in enforced] == [f["original"] for f in warned]
    assert all(f["applied"] == f["floor"] for f in enforced)
    assert check(priced)
    # Enforcing again finds nothing: the floor is met.
    assert enforce_floors(priced, mode="pricing", profile=PROFILE, keys=(key,)) == []


def test_ltl_floor_skips_cohorts_without_a_market_gap(minimal_deal_inputs):
    deal = _below_floor_deal(minimal_deal_inputs)
    deal["loss_to_lease"] = [dict(r, ltl_percent=0.0) for r in deal["loss_to_lease"]]
    deal["market_rent_curve"][1]["market_rent"] = 1400  # 2BR in-place 1500: capped market rent
    findings = enforce_floors(deal, mode="pricing", profile=PROFILE, keys=("loss_to_lease",))
    assert [f["cohort_id"] for f in findings] == ["1BR"]
    assert [r["ltl_percent"] for r in deal["loss_to_lease"]] == [0.01, 0.0]


def test_analyst_insurance_override_waives_the_insurance_floor(minimal_deal_inputs):
    deal = _below_floor_deal(minimal_deal_inputs)
    next(r for r in deal["opex_table"] if r["category_name"] == "Insurance").update(base_value=40_000)
    before = copy.deepcopy(deal)
    overrides: list = []
    findings = enforce_floors(deal, mode="pricing", profile=PROFILE, keys=("insurance_per_unit",),
                              context={"insurance_per_unit_override": Decimal("400"),
                                       "policy_overrides": overrides})
    assert deal == before  # the analyst's figure is kept
    # ... and the waiver is disclosed.
    assert [(f["action"], f["floor"], f["applied"]) for f in findings] == [
        ("floor_waived_by_analyst_override", 600.0, 400.0)]
    assert overrides == [{"input": "insurance_per_unit", "action": "floor_waived_by_analyst_override",
                          "floor": 600.0, "floor_source": "house:year1_insurance_floor",
                          "original": 400.0, "applied": 400.0}]


def test_resolver_pricing_mode_updates_entries_to_the_priced_values(minimal_deal_inputs):
    deal = resolve_assumptions(_below_floor_deal(minimal_deal_inputs), deal_profile=PROFILE)
    priced = resolve_assumptions(deal, mode="pricing")
    entries = priced["assumptions"]["entries"]

    assert priced["assumptions"]["mode"] == "pricing"
    assert [r["vacancy_rate"] for r in priced["physical_vacancy_curve"]] == [0.09, 0.05, 0.05]
    r_and_m = entries["r_and_m_per_unit"]
    assert r_and_m["value"] == 900.0
    assert r_and_m["source"] == "house:vintage_1980s"
    assert r_and_m["guardrail"]["action"] == "enforced"
    assert r_and_m["guardrail"]["original"] == 750.0 and r_and_m["guardrail"]["applied"] == 900.0
    assert r_and_m["override"] == {"value": 900.0, "note": guardrails.GUARDRAIL_OVERRIDE_NOTE}
    reserves = entries["replacement_reserve_per_unit"]
    assert reserves["value"] == 1000.0 and priced["replacement_reserves"][0]["annual_amount"] == 100_000.0
    assert validate_schema(priced) == []
    assert not [i for i in validate_deal(priced).issues if i.code == "GUARDRAIL_BELOW_FLOOR"]
    # The priced values stick: re-resolving in either mode keeps them.
    assert resolve_assumptions(priced, mode="pricing") == priced
    rewarned = resolve_assumptions(priced)
    assert {k: v for k, v in rewarned.items() if k != "assumptions"} == {
        k: v for k, v in priced.items() if k != "assumptions"
    }


def _assert_no_silent_override(canonical):
    for key, entry in canonical["assumptions"]["entries"].items():
        if entry["override"] is None:
            assert entry["value"] == entry["default_value"], key


def test_pricing_entries_without_an_override_keep_value_equal_to_default(minimal_deal_inputs):
    deal = resolve_assumptions(_below_floor_deal(minimal_deal_inputs), deal_profile=PROFILE)
    for priced in (
        resolve_assumptions(deal, mode="pricing"),
        apply_guardrails(deal, mode="pricing", profile=PROFILE)[0],
        resolve_assumptions(_below_floor_deal(minimal_deal_inputs), deal_profile=PROFILE, mode="pricing"),
    ):
        assert priced["assumptions"]["mode"] == "pricing"
        _assert_no_silent_override(priced)
        enforced = [e for e in priced["assumptions"]["entries"].values()
                    if (e.get("guardrail") or {}).get("action") == "enforced"]
        assert enforced
        assert all(e["override"] is not None for e in enforced)


# ── backsolve ────────────────────────────────────────────────────────────

BENCHMARK = {"rate": "0.04", "as_of": "2026-10-03", "source": "synthetic:test"}
POLICY = {"version": "plat.backsolve-policy/1", "strategy": "cashflow", "exit_cap_rate": "0.06"}


@pytest.fixture
def pricing_deal(minimal_deal_inputs):
    """A resolved (underwrite-mode) synthetic deal with values below several floors."""
    deal = _below_floor_deal(minimal_deal_inputs)
    deal["opex_table"] = [r for r in deal["opex_table"] if r["calculation_type"] != "percent_egr"]
    return resolve_assumptions(deal, deal_profile=PROFILE)


def _solve(deal):
    return backsolve.backsolve_price(
        copy.deepcopy(deal), target_coc="0.06", policy=POLICY, benchmark=BENCHMARK,
        min_price="1000000.00", max_price="40000000.00",
    )


def test_backsolve_solved_canonical_carries_pricing_assumptions(pricing_deal):
    assert pricing_deal["assumptions"]["mode"] == "underwrite"
    result = _solve(pricing_deal)
    assert result["status"] == "converged"
    case = result["case"]
    section = case["assumptions"]
    entries = section["entries"]

    assert section["mode"] == "pricing"
    assert case["deal_profile"]["deal_type"] == "stabilized"
    assert case["deal_profile"]["year_built"] == YEAR
    # Entries match the values actually priced.
    assert {r["vacancy_rate"] for r in case["physical_vacancy_curve"]} >= {0.05}
    assert entries["replacement_reserve_per_unit"]["value"] == pytest.approx(
        case["replacement_reserves"][0]["annual_amount"] / 100)
    assert entries["collection_loss"]["value"] == case["collection_loss_curve"][0]["loss_rate"] == 0.02
    assert entries["concessions"]["value"] == case["concession_schedule"][0]["amount"] == 0.0075
    # The enforced floors are shown.
    enforced = {k for k, e in entries.items() if (e.get("guardrail") or {}).get("action") == "enforced"}
    assert enforced == {"physical_vacancy", "concessions", "r_and_m_per_unit", "replacement_reserve_per_unit"}
    assert entries["r_and_m_per_unit"]["guardrail"]["original"] == 750.0
    assert entries["physical_vacancy"]["guardrail"]["segments"] == [
        {"cohort_id": "1BR", "start_period": "2027-07", "end_period": "2031-06", "original": 0.03, "applied": 0.05},
        {"cohort_id": "2BR", "start_period": "2026-07", "end_period": "2031-06", "original": 0.04, "applied": 0.05},
    ]
    findings = result["summary"]["policy_summary"]["guardrails"]["findings"]
    assert {f["key"] for f in findings} == enforced
    overrides = result["summary"]["policy_summary"]["policy_overrides"]
    assert {o["assumption"] for o in overrides if "assumption" in o} == enforced
    _assert_no_silent_override(case)
    assert validate_schema(case) == []
    assert not [i for i in validate_deal(case).issues if i.code == "GUARDRAIL_BELOW_FLOOR"]
    json.dumps(case)


def test_backsolve_reads_the_vintage_from_the_deal_profile(pricing_deal):
    """A year built carried only by deal_profile (an analyst ingest input) sets the
    pricing vintage, as it does for the resolver and the shared guardrails."""
    deal = copy.deepcopy(pricing_deal)
    deal["metadata"].pop("year_built")
    assert deal["deal_profile"]["year_built"] == YEAR
    result = _solve(deal)
    summary = result["summary"]["policy_summary"]
    assert summary["year_built"] == YEAR
    assert summary["guardrails"]["profile"]["year_built"] == YEAR
    assert result["case"]["replacement_reserves"][0]["annual_amount"] == 100_000.0  # 1980s reserve x 100 units
    assert result["case"] == _solve(pricing_deal)["case"]


def test_backsolve_keeps_the_ingest_evidence_its_case_is_resolved_from():
    """The solved case keeps metadata.ingest_gate, so its pricing re-resolution reads
    the same T12 / rent-roll evidence as ingest: every entry matches the priced
    canonical and re-resolving the case changes nothing. A blocked gate still blocks."""
    from tests import deal_library

    deal = deal_library.ingest(deal_library.load_deal("a_stabilized_1990s"))
    policy = {"version": "plat.backsolve-policy/1", "strategy": "cashflow"}
    result = backsolve.backsolve_price(
        copy.deepcopy(deal), target_coc="0.06", policy=policy, benchmark=BENCHMARK,
        min_price="1000000.00", max_price="60000000.00",
    )
    assert result["status"] == "converged"
    case = result["case"]
    entries = case["assumptions"]["entries"]
    assert case["metadata"]["ingest_gate"] == deal["metadata"]["ingest_gate"]
    assert entries["collection_loss"]["source"] == "deal:t12"
    assert entries["collection_loss"]["value"] == case["collection_loss_curve"][0]["loss_rate"] == 0.008
    assert entries["insurance_per_unit"]["source"] == "deal:t12"
    assert entries["insurance_per_unit"]["value"] == 650.0
    assert entries["opex.payroll.base"]["source"] == "deal:t12"
    _assert_no_silent_override(case)
    assert resolve_assumptions(case, mode="pricing") == case

    blocked = copy.deepcopy(deal)
    blocked["metadata"]["ingest_gate"]["status"] = "BLOCKED"
    with pytest.raises(ValueError, match="INGEST_BLOCKED"):
        backsolve.backsolve_price(blocked, target_coc="0.06", policy=policy, benchmark=BENCHMARK,
                                  min_price="1000000.00", max_price="60000000.00")


def test_backsolve_assumptions_refresh_changes_no_priced_number(pricing_deal):
    result = _solve(pricing_deal)
    case = result["case"]
    without = {k: v for k, v in case.items() if k not in ("assumptions", "deal_profile")}
    original = {k: v for k, v in pricing_deal.items() if k not in ("assumptions", "deal_profile")}
    assert set(without) >= set(original) - {"metadata"}
    # The shared guardrails find nothing left to enforce in the priced case.
    profile = GuardrailProfile("cashflow", YEAR)
    assert enforce_floors(copy.deepcopy(case), mode="pricing", profile=profile) == []
    # Engine output is the same with or without the refreshed sections.
    from engine.engine import run_underwriting
    assert run_underwriting(copy.deepcopy(without)) == result["results"]


def test_backsolve_house_policy_matches_generic_pricing_floors(pricing_deal):
    """apply_guardrails(pricing) on backsolve's prepared canonical raises nothing:
    backsolve already enforced every shared floor."""
    values = {k: Decimal(v) if isinstance(v, str) else v for k, v in {
        "agency_spread": "0.015", "sale_cost_percent": "0.02", "purchase_closing_cost_pct": "0.015",
        "partnership_closing_costs": "50000", "acquisition_fee_pct": "0.01",
        "asset_management_fee_pct": "0.015", "annual_partnership_expenses": "25000",
        "disposition_fee_pct": "0.01", "loan_closing_costs": "0",
    }.items()}
    prepared, summary = backsolve._prepare_house_assumptions(
        copy.deepcopy(pricing_deal), year_built=None, strategy="cashflow", target_coc=Decimal("0.06"),
        benchmark_treasury=Decimal("0.04"), exit_cap_rate=Decimal("0.06"), **values,
    )
    assert summary["guardrails"]["profile"]["year_built"] == YEAR
    _, findings = apply_guardrails(prepared, mode="pricing", profile=GuardrailProfile("cashflow", YEAR))
    assert findings == []


# ── pricing labels ───────────────────────────────────────────────────────


def _shown_as_analyst_edits(case, original):
    """Entries the solved case shows as analyst values the input canonical did not carry."""
    from engine.assumptions.resolver import _NOTE_EDITED, _NOTE_PRESENT

    before = original["assumptions"]["entries"]
    out = set()
    for key, entry in case["assumptions"]["entries"].items():
        note = (entry.get("override") or {}).get("note")
        if note == _NOTE_EDITED:
            out.add(key)
        elif note == _NOTE_PRESENT and not (
            (before.get(key, {}).get("override") or {}).get("note") == _NOTE_PRESENT
            and before[key]["value"] == entry["value"]
        ):
            out.add(key)
    return out


def _assert_pricing_labels(result, original):
    """Every value backsolve's pricing set is labeled by who set it and disclosed."""
    case = result["case"]
    entries = case["assumptions"]["entries"]
    overrides = result["summary"]["policy_summary"]["policy_overrides"]
    assert _shown_as_analyst_edits(case, original) == set()
    for key in backsolve.PRICING_POLICY_KEYS:
        assert entries[key]["source"] == "house:backsolve_policy", key
        assert entries[key]["override"] == {"value": entries[key]["value"], "note": guardrails.PRICING_POLICY_NOTE}
    # Each normalization that changed an input is in policy_overrides with original and applied,
    # and its entry carries house:pricing_normalization unless a floor was enforced on it.
    normalized = {}
    for override in overrides:
        if override.get("normalization"):
            assert override["source"] == "house:pricing_normalization"
            assert override["original"] != override["applied"]
            normalized[backsolve.PRICING_NORMALIZATIONS[override["input"]][1]] = override
    labeled = {k for k, e in entries.items() if e["source"] == "house:pricing_normalization"}
    enforced = {k for k, e in entries.items() if (e.get("guardrail") or {}).get("action") == "enforced"}
    assert set(normalized) - enforced <= labeled
    assert {k for k in labeled if not k.startswith("opex.")} <= set(normalized)
    for key in labeled:
        assert entries[key]["override"]["note"] == guardrails.PRICING_NORMALIZATION_NOTE
        assert entries[key]["review_required"] is True
    # The labels are stable: re-resolving the solved canonical changes nothing.
    assert resolve_assumptions(case, mode="pricing") == case
    return entries, normalized


def test_backsolve_labels_what_its_pricing_policy_and_normalizations_set(pricing_deal):
    result = _solve(pricing_deal)
    entries, normalized = _assert_pricing_labels(result, pricing_deal)
    case = result["case"]
    # The policy's explicit exit cap, fees and closing costs.
    assert entries["exit_cap_rate"]["source"] == "house:backsolve_policy"
    assert "explicit policy value" in entries["exit_cap_rate"]["basis"]
    assert entries["annual_partnership_expenses"]["value"] == case["fund_assumptions"]["annual_partnership_expenses"]
    assert "DEFAULTS['acquisition_fee_pct']" in entries["acquisition_fee_pct"]["basis"]
    # Collection loss (two rows -> one ALL row at the worst 2%) and loss to lease are rewritten.
    assert set(normalized) == {"collection_loss", "loss_to_lease"}
    assert entries["collection_loss"]["source"] == "house:pricing_normalization"
    assert normalized["collection_loss"]["applied"] == case["collection_loss_curve"]
    assert [r["loss_rate"] for r in normalized["collection_loss"]["original"]] == [0.002, 0.02]
    # Concessions were raised by the floor: the guardrail label stands.
    assert entries["concessions"]["source"] == "house:vintage_1980s"
    # The real estate tax line is reassessed by the property-tax module at the priced price.
    tax = entries["opex.real_estate_taxes.base"]
    assert tax["source"] == "house:property_tax_module"
    assert tax["override"]["note"] == guardrails.PRICING_PROPERTY_TAX_NOTE
    assert tax["value"] == next(r for r in case["opex_table"] if r["category_name"] == "Real Estate Taxes")[
        "base_value"]
    # The R&M top-up line takes the R&M floor's source.
    top_up = next(e for k, e in entries.items()
                  if (e.get("detail") or {}).get("category_name") == guardrails.R_AND_M_TOP_UP_CATEGORY)
    assert top_up["source"] == "house:vintage_1980s"
    assert top_up["override"]["note"] == guardrails.PRICING_FLOOR_LINE_NOTE


def test_reserves_above_the_vintage_reserve_are_a_disclosed_normalization(minimal_deal_inputs):
    """Backsolve sets reserves to the vintage reserve even when the analyst's are higher
    (a pricing normalization, unchanged here): it is labeled and listed, not shown as an edit."""
    deal = _below_floor_deal(minimal_deal_inputs)
    deal["opex_table"] = [r for r in deal["opex_table"] if r["calculation_type"] != "percent_egr"]
    deal["replacement_reserves"] = [dict(r, annual_amount=150_000) for r in deal["replacement_reserves"]]
    deal = resolve_assumptions(deal, deal_profile=PROFILE)
    result = _solve(deal)
    entries, normalized = _assert_pricing_labels(result, deal)
    reserves = normalized["replacement_reserve_per_unit"]
    assert reserves["normalization"] == "reserves_set_to_vintage_reserve"
    assert [r["annual_amount"] for r in reserves["original"]] == [150_000.0]
    assert reserves["applied"] == result["case"]["replacement_reserves"]
    assert entries["replacement_reserve_per_unit"]["source"] == "house:pricing_normalization"
    assert entries["replacement_reserve_per_unit"]["value"] == 1000.0


@pytest.mark.parametrize("slug, strategy, target, max_price", [
    ("a_stabilized_1990s", "cashflow", "0.06", "40000000.00"),
    ("d_value_add_1980s_msa_only", "value_add", "0.03", "20000000.00"),
])
def test_library_backsolve_cases_carry_pricing_labels(slug, strategy, target, max_price):
    from tests import deal_library

    deal = deal_library.ingest(deal_library.load_deal(slug))
    result = backsolve.backsolve_price(
        copy.deepcopy(deal), target_coc=target, policy={"version": "plat.backsolve-policy/1", "strategy": strategy},
        benchmark=BENCHMARK, min_price="1000000.00", max_price=max_price,
    )
    assert result["status"] == "converged"
    entries, normalized = _assert_pricing_labels(result, deal)
    assert entries["loss_to_lease"]["source"] == "house:pricing_normalization"
    assert normalized["loss_to_lease"]["normalization"] == "ltl_rederived_from_market_vs_in_place"
    assert entries["concessions"]["source"] == "house:pricing_normalization"
    assert entries["opex.real_estate_taxes.base"]["source"] == "house:property_tax_module"
    # The exit cap came from the canonical, not the policy: its label is unchanged.
    assert entries["exit_cap_rate"]["source"] != "house:backsolve_policy"
    # sale_cost_percent was an ingest input the policy matched: the basis says so.
    assert "matches the canonical's 2.00% (analyst)" in entries["sale_cost_percent"]["basis"]


def test_insurance_override_below_the_floor_is_kept_and_disclosed(pricing_deal):
    policy = {**POLICY, "insurance_per_unit_override": "400"}
    result = backsolve.backsolve_price(
        copy.deepcopy(pricing_deal), target_coc="0.06", policy=policy, benchmark=BENCHMARK,
        min_price="1000000.00", max_price="40000000.00",
    )
    assert result["status"] == "converged"
    case = result["case"]
    summary = result["summary"]["policy_summary"]
    # The waiver keeps today's price: insurance is priced at the analyst's $400/unit.
    insurance = next(r for r in case["opex_table"] if r["category_name"] == "Insurance")
    assert insurance["base_value"] == 40_000.0
    assert summary["insurance_per_unit_applied"] == 400.0
    waiver = [o for o in summary["policy_overrides"] if o["input"] == "insurance_per_unit"]
    assert waiver == [{"input": "insurance_per_unit", "action": "floor_waived_by_analyst_override",
                       "floor": 600.0, "floor_source": "house:year1_insurance_floor",
                       "category_name": "Insurance", "original": 600.0, "applied": 400.0}]
    assert [f["action"] for f in summary["guardrails"]["findings"] if f["key"] == "insurance_per_unit"] == [
        "floor_waived_by_analyst_override"]
    entry = case["assumptions"]["entries"]["insurance_per_unit"]
    assert entry["value"] == 400.0
    assert entry["source"] == "analyst"
    assert entry["override"] == {"value": 400.0, "note": guardrails.PRICING_INSURANCE_OVERRIDE_NOTE}
    assert entry["guardrail"]["action"] == "floor_waived_by_analyst_override"
    assert entry["guardrail"]["floor"] == 600.0 and entry["guardrail"]["applied"] == 400.0
    assert not [i for i in validate_deal(case).issues if i.code == "GUARDRAIL_BELOW_FLOOR"]
    # Re-resolving honors the recorded waiver in either mode: insurance is not raised.
    assert resolve_assumptions(case, mode="pricing") == case
    rewarned = resolve_assumptions(case)
    assert rewarned["opex_table"] == case["opex_table"]
    assert rewarned["assumptions"]["entries"]["insurance_per_unit"]["guardrail"] == entry["guardrail"]
    # Without the policy override the floor is enforced and the price is lower.
    floored = _solve(pricing_deal)
    assert dec(floored["summary"]["solved_purchase_price"]) < dec(result["summary"]["solved_purchase_price"])


def _r_and_m_rows(canonical):
    return [r for r in canonical["opex_table"]
            if r["category_name"] in ("R&M", guardrails.R_AND_M_TOP_UP_CATEGORY)]


def test_r_and_m_floor_ignores_its_own_top_up_line(minimal_deal_inputs):
    """Re-enforcing after the analyst lowers R&M sizes the top-up from the analyst
    lines alone: the existing top-up line never counts toward the floor."""
    priced = _below_floor_deal(minimal_deal_inputs)
    enforce_floors(priced, mode="pricing", profile=PROFILE, keys=("r_and_m_per_unit",))
    next(r for r in priced["opex_table"] if r["category_name"] == "R&M").update(base_value=600)  # $/unit

    findings = enforce_floors(priced, mode="pricing", profile=PROFILE, keys=("r_and_m_per_unit",))
    top_ups = [r for r in priced["opex_table"] if r["category_name"] == guardrails.R_AND_M_TOP_UP_CATEGORY]
    assert len(top_ups) == 1 and top_ups[0]["base_value"] == 30_000.0  # ($900 - $600) x 100 units
    assert backsolve._sum_opex_bucket(priced, "repairs_maintenance") == Decimal("90000")
    [finding] = findings
    assert finding["original"] == 750.0  # the bucket as found: $600 + the old $150 top-up
    assert finding["top_up_annual"] == 30_000.0 and finding["applied_annual"] == 90_000.0
    assert finding["replaced_top_up_annual"] == 15_000.0
    # The floor is met: enforcing again finds nothing and changes nothing.
    again = copy.deepcopy(priced)
    assert enforce_floors(again, mode="pricing", profile=PROFILE, keys=("r_and_m_per_unit",)) == []
    assert again == priced


def test_repricing_after_lowering_r_and_m_tops_up_to_the_floor_exactly(pricing_deal):
    first = _solve(pricing_deal)["case"]
    assert [r["category_name"] for r in _r_and_m_rows(first)] == ["R&M", guardrails.R_AND_M_TOP_UP_CATEGORY]

    # The analyst lowers R&M on the priced canonical, then prices it again.
    lowered = copy.deepcopy(first)
    next(r for r in lowered["opex_table"] if r["category_name"] == "R&M").update(base_value=600)
    result = _solve(lowered)
    assert result["status"] == "converged"
    case = result["case"]

    rows = _r_and_m_rows(case)
    assert [r["category_name"] for r in rows] == ["R&M", guardrails.R_AND_M_TOP_UP_CATEGORY]
    units = backsolve._sum_units(case)
    assert backsolve._sum_opex_bucket(case, "repairs_maintenance") / units == Decimal("900")
    summary = result["summary"]["policy_summary"]
    assert summary["repairs_maintenance_top_up_annual"] == 30_000.0
    entry = case["assumptions"]["entries"]["r_and_m_per_unit"]
    assert entry["value"] == 900.0 and entry["guardrail"]["action"] == "enforced"
    assert resolve_assumptions(case, mode="pricing") == case


# ── determinism ──────────────────────────────────────────────────────────


def test_guardrails_are_deterministic(minimal_deal_inputs, pricing_deal):
    deal = _below_floor_deal(minimal_deal_inputs)
    for mode in ("underwrite", "pricing"):
        first = apply_guardrails(deal, mode=mode, profile=PROFILE)
        second = apply_guardrails(copy.deepcopy(deal), mode=mode, profile=PROFILE)
        assert first == second
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert resolve_assumptions(deal, deal_profile=PROFILE, mode="pricing") == resolve_assumptions(
        copy.deepcopy(deal), deal_profile=PROFILE, mode="pricing")
    first, second = _solve(pricing_deal), _solve(pricing_deal)
    assert first["case"] == second["case"]
    assert first["summary"] == second["summary"]
