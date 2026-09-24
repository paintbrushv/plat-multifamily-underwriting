"""Task 4.2 — deterministic researched tax-regime schedules (engine side).

Boundary tests derived from the Task 4.1 research registry
(``tax-regime-research/1.0.0``, harness docs/TAX_REGIME_SPEC.md):

- TX: Jan-1 market-value appraisal at 100 percent (no assessment ratio);
  levy is the sum of overlapping taxing units' per-$100 rates; the
  § 23.231 20 percent circuit breaker expires Dec 31, 2026, so post-2026
  applicability is uncertainty-blocked; purchase price is evidence, never
  a statutory percentage reset.
- CA: Prop 13 acquisition-value system — base year value at full cash value,
  annual adjustment by the BOE-published CPI factor never exceeding 2
  percent (factor is per-year parcel input, never a constant); 1 percent
  constitutional rate cap plus voter-approved additions; ownership change
  resets the base year; new construction gets its own base year value;
  supplemental assessment count depends on the event month (R&TC § 75.11).
- FL: 10 percent nonhomestead cap applies to all levies other than school
  district levies; 10+-unit multifamily is § 193.1555 (9-or-fewer is
  § 193.1554 and refuses); ownership/control change resets to just value.
- AL: Class II at 20 percent of fair and reasonable market value; state
  6.5 mills plus jurisdiction-specific county millage (no universal
  fallback; unknown jurisdiction refuses).

Expected values in these tests were computed independently with Decimal
arithmetic (ROUND_HALF_UP to cents), not derived from the implementation.
Existing engine behavior (property_tax mills policy) is asserted unchanged
and at parity with the equivalent regime schedule.
"""
from decimal import Decimal

import pytest

from engine.property_tax import (
    PropertyTaxPolicyError,
    build_property_tax_policy,
    calculate_property_tax,
)
from engine.tax_regimes import (
    CONTRACT_VERSION,
    TaxRegimeError,
    build_tax_regime_schedule,
    supported_regimes,
)

# ---------------------------------------------------------------------------
# TX — market-value regime, per-$100 summed levies
# ---------------------------------------------------------------------------


def _tx_inputs(**overrides):
    inputs = {
        "state": "TX",
        "tax_year": 2026,
        "purchase_price": "12500000",
        "taxing_unit_rates_per_100": [
            {"unit": "county", "rate": "0.3545"},
            {"unit": "city", "rate": "0.6800"},
            {"unit": "school", "rate": "1.2345"},
            {"unit": "college", "rate": "0.1800"},
            {"unit": "special_district", "rate": "0.0820"},
        ],
        "special_assessments": [
            {
                "label": "BID special assessment",
                "amount": "12345.67",
                "source_locator": "raw_inputs/bid_notice.pdf, page 1",
            }
        ],
    }
    inputs.update(overrides)
    return inputs


def test_tx_market_value_levy_is_summed_per_100_rates_with_special_assessments():
    result = build_tax_regime_schedule(_tx_inputs())
    # 12,500,000 x 2.531 / 100 = 316,375.00
    assert result["taxable_value"] == 12500000.0
    assert result["annual_ad_valorem_tax"] == 316375.0
    assert result["special_assessment_total"] == 12345.67
    assert result["annual_total_tax"] == 328720.67
    assert result["assessment_ratio"] == 1.0  # § 26.02: ratios prohibited
    assert [c["unit"] for c in result["levy_components"]] == [
        "county",
        "city",
        "school",
        "college",
        "special_district",
    ]


def test_tx_circuit_breaker_caps_at_prior_appraised_plus_twenty_percent():
    result = build_tax_regime_schedule(
        _tx_inputs(
            circuit_breaker=True,
            prior_appraised_value="10000000",
            purchase_price="12500000",  # appraised (evidence) value
            new_improvement_value="250000",
            special_assessments=[],
        )
    )
    # cap = 10,000,000 + 20% + 250,000 = 12,250,000 < 12,500,000
    assert result["capped_appraised_value"] == 12250000.0
    assert result["annual_ad_valorem_tax"] == 310047.50


def test_tx_circuit_breaker_not_binding_at_exactly_twenty_percent():
    result = build_tax_regime_schedule(
        _tx_inputs(
            circuit_breaker=True,
            prior_appraised_value="10000000",
            purchase_price="12000000",
            special_assessments=[],
        )
    )
    # cap = 12,000,000 equals appraised value: boundary, not binding
    assert result["capped_appraised_value"] == 12000000.0
    assert result["annual_ad_valorem_tax"] == 303720.00


def test_tx_circuit_breaker_expires_after_2026():
    with pytest.raises(TaxRegimeError) as exc:
        build_tax_regime_schedule(
            _tx_inputs(
                tax_year=2027,
                circuit_breaker=True,
                prior_appraised_value="10000000",
            )
        )
    assert exc.value.code == "UNCERTAINTY_BLOCKS_STATUTORY_CLAIM"


def test_tx_missing_tax_year_and_null_purchase_price_refuse():
    with pytest.raises(TaxRegimeError) as exc:
        build_tax_regime_schedule(_tx_inputs(tax_year=None))
    assert exc.value.code == "MISSING_EFFECTIVE_DATE"

    with pytest.raises(TaxRegimeError) as exc:
        build_tax_regime_schedule(_tx_inputs(purchase_price=None))
    # a missing basis is never zero
    assert exc.value.code == "INVALID_STATUTORY_VALUE"


def test_tx_incompatible_local_levy_units_refuse():
    bad_unit = _tx_inputs()
    bad_unit["taxing_unit_rates_per_100"][0]["unit"] = "mills"
    with pytest.raises(TaxRegimeError) as exc:
        build_tax_regime_schedule(bad_unit)
    assert exc.value.code == "LOCAL_LEVY_INCOMPATIBLE"

    missing_unit = _tx_inputs()
    del missing_unit["taxing_unit_rates_per_100"][0]["unit"]
    with pytest.raises(TaxRegimeError) as exc:
        build_tax_regime_schedule(missing_unit)
    assert exc.value.code == "LOCAL_LEVY_INCOMPATIBLE"


def test_tx_special_assessment_requires_locator_and_nonnegative_amount():
    with pytest.raises(TaxRegimeError):
        build_tax_regime_schedule(
            _tx_inputs(special_assessments=[{"label": "BID", "amount": "-1"}])
        )
    with pytest.raises(TaxRegimeError):
        build_tax_regime_schedule(
            _tx_inputs(special_assessments=[{"label": "BID"}])
        )


# ---------------------------------------------------------------------------
# CA — Prop 13 acquisition-value regime
# ---------------------------------------------------------------------------


def _ca_inputs(**overrides):
    inputs = {
        "state": "CA",
        "tax_year": 2026,
        "base_year": 2024,
        "base_year_value": "8000000",
        "annual_cpi_factors": {2025: "0.0173", 2026: "0.0199"},
        "ad_valorem_rate": "0.01",
        "voter_approved_additions": "0.0032",
    }
    inputs.update(overrides)
    return inputs


def test_ca_base_year_value_factors_by_cpi_up_to_two_percent():
    result = build_tax_regime_schedule(_ca_inputs())
    # 8,000,000 x 1.0173 x 1.0199 = 8,300,354.16; tax at 1.32% = 109,564.67
    assert result["factored_base_year_value"] == 8300354.16
    assert result["annual_ad_valorem_tax"] == 109564.67
    assert result["annual_total_tax"] == 109564.67


def test_ca_cpi_factor_above_two_percent_is_capped_by_statute():
    result = build_tax_regime_schedule(
        _ca_inputs(annual_cpi_factors={2025: "0.031", 2026: "0.0199"})
    )
    # art. XIII A § 2(b): lesser of CPI or 2 percent -> 0.031 becomes 0.02
    assert result["statute_derived"]["applied_cpi_factors"] == {
        "2025": 0.02,
        "2026": 0.0199,
    }
    # 8,000,000 x 1.02 x 1.0199 = 8,322,384.00; tax at 1.32% = 109,855.47
    assert result["factored_base_year_value"] == 8322384.0
    assert result["annual_ad_valorem_tax"] == 109855.47


def test_ca_missing_cpi_factor_refuses_rather_than_defaulting():
    with pytest.raises(TaxRegimeError) as exc:
        build_tax_regime_schedule(_ca_inputs(annual_cpi_factors={2025: "0.0173"}))
    # the BOE factor is per-year parcel input, never a hardcoded constant
    assert exc.value.code == "INVALID_STATUTORY_VALUE"


def test_ca_rate_above_one_percent_constitutional_cap_refuses():
    with pytest.raises(TaxRegimeError) as exc:
        build_tax_regime_schedule(_ca_inputs(ad_valorem_rate="0.0101"))
    assert exc.value.code == "INVALID_STATUTORY_VALUE"


def test_ca_ownership_change_resets_base_year_and_counts_supplementals():
    result = build_tax_regime_schedule(
        _ca_inputs(
            ownership_change={
                "date": "2026-03-15",
                "full_cash_value": "9250000",
            }
        )
    )
    # reset: 9,250,000 x 1.32% = 122,100.00; Jan-May event -> two supplementals
    assert result["factored_base_year_value"] == 9250000.0
    assert result["annual_ad_valorem_tax"] == 122100.00
    assert result["statute_derived"]["supplemental_assessment_count"] == 2
    assert (
        result["statute_derived"]["month_following_effective_date"]
        == "2026-04-01"
    )

    june = build_tax_regime_schedule(
        _ca_inputs(
            ownership_change={"date": "2026-06-01", "full_cash_value": "9250000"}
        )
    )
    # R&TC § 75.11: Jun 1 - Dec 31 event -> one supplemental
    assert june["statute_derived"]["supplemental_assessment_count"] == 1
    assert (
        june["statute_derived"]["month_following_effective_date"] == "2026-07-01"
    )


def test_ca_new_construction_gets_separate_base_year_value():
    result = build_tax_regime_schedule(
        _ca_inputs(
            base_year_value="5000000",
            new_construction={"completion_year": 2025, "value": "500000"},
        )
    )
    # main: 5,000,000 factored 2025+2026 = 5,187,721.35;
    # construction (own base year 2025): 500,000 x 1.0199 = 509,950.00;
    # total 5,697,671.35 x 1.32% = 75,209.26
    assert result["factored_base_year_value"] == 5697671.35
    assert result["annual_ad_valorem_tax"] == 75209.26


def test_ca_invalid_base_year_tax_year_and_nonfinite_values_refuse():
    with pytest.raises(TaxRegimeError) as exc:
        build_tax_regime_schedule(_ca_inputs(tax_year=2023))
    assert exc.value.code == "INVALID_STATUTORY_VALUE"

    with pytest.raises(TaxRegimeError) as exc:
        build_tax_regime_schedule(_ca_inputs(base_year_value=Decimal("1e400")))
    assert exc.value.code == "INVALID_STATUTORY_VALUE"

    with pytest.raises(TaxRegimeError):
        build_tax_regime_schedule(_ca_inputs(voter_approved_additions="-0.001"))


# ---------------------------------------------------------------------------
# FL — 10 percent nonhomestead cap, non-school levies only
# ---------------------------------------------------------------------------


def _fl_inputs(**overrides):
    inputs = {
        "state": "FL",
        "tax_year": 2026,
        "unit_count": 220,
        "just_value": "15000000",
        "prior_assessed_value": "12000000",
        "ownership_change_or_qualifying_improvement": False,
        "non_school_millage": "18.5",
        "school_millage": "7.7430",
    }
    inputs.update(overrides)
    return inputs


def test_fl_cap_applies_to_non_school_levies_only():
    result = build_tax_regime_schedule(_fl_inputs())
    # capped = 12,000,000 x 1.10 = 13,200,000 (< just value);
    # non-school: 13,200,000 x 18.5 / 1000 = 244,200.00
    # school on JUST value: 15,000,000 x 7.7430 / 1000 = 116,145.00
    assert result["capped_assessed_value"] == 13200000.0
    assert result["annual_ad_valorem_tax"] == 360345.00
    components = {c["basis"]: c for c in result["levy_components"]}
    assert components["capped_non_school"]["tax"] == 244200.0
    assert components["just_value_school"]["tax"] == 116145.0


def test_fl_cap_never_exceeds_just_value():
    result = build_tax_regime_schedule(_fl_inputs(just_value="13000000"))
    # cap = 13,200,000 > just = 13,000,000 -> assessed = just value
    assert result["capped_assessed_value"] == 13000000.0


def test_fl_ownership_change_resets_assessment_to_just_value():
    result = build_tax_regime_schedule(
        _fl_inputs(ownership_change_or_qualifying_improvement=True)
    )
    # 15,000,000 x 18.5 / 1000 = 277,500.00 non-school
    assert result["capped_assessed_value"] == 15000000.0
    assert result["annual_ad_valorem_tax"] == 393645.00


def test_fl_unit_count_routes_the_statute():
    with pytest.raises(TaxRegimeError) as exc:
        build_tax_regime_schedule(_fl_inputs(unit_count=9))
    # § 193.1554 (9-or-fewer) is out of scope for this regime
    assert exc.value.code == "UNSUPPORTED_TAX_RULE_FAMILY"

    ten = build_tax_regime_schedule(_fl_inputs(unit_count=10))
    assert ten["annual_ad_valorem_tax"] == 360345.00

    with pytest.raises(TaxRegimeError) as exc:
        build_tax_regime_schedule(_fl_inputs(unit_count=None))
    # unit count is a mandatory applicability input
    assert exc.value.code == "INVALID_STATUTORY_VALUE"


def test_fl_null_prior_assessed_value_without_reset_refuses():
    with pytest.raises(TaxRegimeError) as exc:
        build_tax_regime_schedule(_fl_inputs(prior_assessed_value=None))
    # missing is never zero
    assert exc.value.code == "INVALID_STATUTORY_VALUE"


def test_fl_negative_nonfinite_millage_refuses():
    with pytest.raises(TaxRegimeError):
        build_tax_regime_schedule(_fl_inputs(non_school_millage="-1"))
    with pytest.raises(TaxRegimeError):
        build_tax_regime_schedule(_fl_inputs(school_millage="NaN"))
    with pytest.raises(TaxRegimeError):
        build_tax_regime_schedule(_fl_inputs(tax_year=None))


# ---------------------------------------------------------------------------
# AL — Class II 20 percent ratio, jurisdiction-specific millage
# ---------------------------------------------------------------------------


def _al_inputs(**overrides):
    inputs = {
        "state": "AL",
        "tax_year": 2026,
        "jurisdiction": "Madison County",
        "fair_market_value": "7500000",
        "total_millage_mills": "34.0",
    }
    inputs.update(overrides)
    return inputs


def test_al_class_ii_twenty_percent_ratio_and_millage():
    result = build_tax_regime_schedule(_al_inputs())
    # assessed = 7,500,000 x 20% = 1,500,000; tax = 1,500,000 x 34 / 1000
    assert result["assessment_ratio"] == 0.20
    assert result["assessed_value"] == 1500000.0
    assert result["annual_ad_valorem_tax"] == 51000.0
    assert result["annual_total_tax"] == 51000.0
    assert result["scenario_assumptions"]["jurisdiction"] == "Madison County"


def test_al_below_state_minimum_millage_refuses():
    with pytest.raises(TaxRegimeError) as exc:
        build_tax_regime_schedule(_al_inputs(total_millage_mills="6.0"))
    assert exc.value.code == "INVALID_STATUTORY_VALUE"


def test_al_requires_named_jurisdiction_no_universal_fallback():
    with pytest.raises(TaxRegimeError) as exc:
        build_tax_regime_schedule(_al_inputs(jurisdiction="  "))
    assert exc.value.code == "LOCAL_LEVY_INCOMPATIBLE"

    with pytest.raises(TaxRegimeError) as exc:
        build_tax_regime_schedule(_al_inputs(jurisdiction=None))
    assert exc.value.code == "LOCAL_LEVY_INCOMPATIBLE"


def test_al_nonfinite_market_value_refuses():
    with pytest.raises(TaxRegimeError):
        build_tax_regime_schedule(_al_inputs(fair_market_value="1e-400"))
    with pytest.raises(TaxRegimeError):
        build_tax_regime_schedule(_al_inputs(fair_market_value=Decimal("1e400")))
    with pytest.raises(TaxRegimeError):
        build_tax_regime_schedule(_al_inputs(fair_market_value=True))


# ---------------------------------------------------------------------------
# Cross-cutting contract behavior
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["TX", "CA", "FL", "AL"])
def test_every_result_requires_human_review_and_carries_citations(state):
    inputs = {
        "TX": _tx_inputs(special_assessments=[]),
        "CA": _ca_inputs(),
        "FL": _fl_inputs(),
        "AL": _al_inputs(),
    }[state]
    result = build_tax_regime_schedule(inputs)
    assert result["requires_competent_human_review"] is True
    assert result["contract_version"] == CONTRACT_VERSION
    assert result["citations"]
    assert all(c["url"].startswith("https://") for c in result["citations"])
    assert result["scenario_assumptions"]  # analyst-supplied inputs echo separately
    assert result["statute_derived"]  # statute-derived quantities stay separate


def test_unknown_regime_refuses_with_no_standard_fallback():
    with pytest.raises(TaxRegimeError) as exc:
        build_tax_regime_schedule({"state": "NY", "tax_year": 2026})
    assert exc.value.code == "UNSUPPORTED_TAX_REGIME"

    with pytest.raises(TaxRegimeError) as exc:
        build_tax_regime_schedule({"state": "Standard", "tax_year": 2026})
    assert exc.value.code == "UNSUPPORTED_TAX_REGIME"


def test_supported_regimes_are_exactly_the_researched_four():
    assert supported_regimes() == ("AL", "CA", "FL", "TX")


def test_results_are_isolated_from_module_state():
    first = build_tax_regime_schedule(_tx_inputs(special_assessments=[]))
    first["levy_components"][0]["rate"] = "999"
    first["scenario_assumptions"]["purchase_price"] = "0"
    second = build_tax_regime_schedule(_tx_inputs(special_assessments=[]))
    assert second["levy_components"][0]["rate"] == 0.3545
    assert second["scenario_assumptions"]["purchase_price"] == "12500000"


def test_mills_policy_parity_with_existing_engine_tax_calculation():
    # The existing property_tax mills policy on $10,000,000 at 25.31 mills
    # is exactly the TX per-$100 schedule at 2.531 — existing engine
    # behavior is preserved and the new schedule is at parity with it.
    policy = build_property_tax_policy(
        millage_rate="25.31",
        unit="mills",
        source="analyst",
        source_locator="tests:parity",
    )
    legacy = calculate_property_tax(purchase_price="10000000", policy=policy)
    regime = build_tax_regime_schedule(
        _tx_inputs(
            purchase_price="10000000",
            taxing_unit_rates_per_100=[{"unit": "combined", "rate": "2.531"}],
            special_assessments=[],
        )
    )
    assert regime["annual_ad_valorem_tax"] == 253100.0
    assert float(legacy.annual_ad_valorem_tax) == regime["annual_ad_valorem_tax"]


def test_existing_property_tax_error_surface_unchanged():
    # The legacy error path is untouched: wrong-unit millage still refuses
    # with the legacy code (regression guard for preserved behavior).
    with pytest.raises(PropertyTaxPolicyError) as exc:
        build_property_tax_policy(
            millage_rate="25.31 mills",
            unit="mills",
            source="analyst",
            source_locator="tests:legacy",
        )
    assert exc.value.code == "invalid_property_tax_millage"