import json
from decimal import Decimal

import pytest

from engine.property_tax import (
    PropertyTaxPolicyError,
    apply_property_tax_policy,
    build_property_tax_policy,
    calculate_property_tax,
    normalize_millage_rate,
    validate_property_tax_policy,
)


@pytest.mark.parametrize(
    ("value", "unit"),
    [
        ("25.31", "mills"),
        ("0.02531", "decimal_rate"),
        ("2.531", "percentage_points"),
        ("2.531", "per_100"),
    ],
)
def test_labelled_units_normalize_to_identical_mills(value, unit):
    assert normalize_millage_rate(value, unit=unit) == Decimal("25.31000")


def test_policy_materializes_full_value_default_and_stable_provenance():
    policy = build_property_tax_policy(
        millage_rate="25.31",
        unit="mills",
        source="analyst",
        source_locator="underwrite-deal:property_tax_millage",
    )
    assert policy == {
        "millage_rate_mills": 25.31,
        "assessment_ratio": 1.0,
        "source": "analyst",
        "source_locator": "underwrite-deal:property_tax_millage",
        "analyst_override": False,
    }
    assert validate_property_tax_policy(policy) == policy


def test_non_default_ratio_requires_approval_and_provenance():
    with pytest.raises(PropertyTaxPolicyError) as exc:
        build_property_tax_policy(
            millage_rate="25.31",
            unit="mills",
            assessment_ratio="0.70",
            source="county_tax_notice",
            source_locator="raw_inputs/notice.pdf, page 2",
            analyst_override=False,
        )
    assert exc.value.code == "unsupported_property_tax_assessment_override"

    approved = build_property_tax_policy(
        millage_rate="25.31",
        unit="mills",
        assessment_ratio="0.70",
        source="composite_evidence",
        source_locator=(
            "millage=raw_inputs/rate.pdf, page 1;"
            "assessment_ratio=cad_rate_table:raw_inputs/ratio.pdf, page 3"
        ),
        analyst_override=True,
    )
    assert approved["assessment_ratio"] == 0.7
    assert approved["analyst_override"] is True


@pytest.mark.parametrize(
    "value", [None, "", True, "NaN", "Infinity", "0", "-1", "25.31 mills", "2.531%"]
)
def test_invalid_or_unit_decorated_millage_is_named(value):
    with pytest.raises(PropertyTaxPolicyError) as exc:
        normalize_millage_rate(value, unit="mills")
    assert exc.value.code == "invalid_property_tax_millage"


def test_validator_rejects_legacy_aliases_and_extra_fields():
    policy = {
        "millage_rate_mills": 25.31,
        "assessment_ratio": 1.0,
        "source": "analyst",
        "source_locator": "underwrite-deal:property_tax_millage",
        "analyst_override": False,
        "tax_rate_pct": 0.02531,
    }

    with pytest.raises(PropertyTaxPolicyError) as exc:
        validate_property_tax_policy(policy)

    assert exc.value.code == "invalid_property_tax_millage"
    assert exc.value.field == "property_tax_policy"


def test_formula_and_half_up_cent_rounding():
    policy = {
        "millage_rate_mills": 25.0,
        "assessment_ratio": 1.0,
        "source": "county_tax_notice",
        "source_locator": "raw_inputs/notice.pdf, page 2",
        "analyst_override": False,
    }
    result = calculate_property_tax(
        purchase_price=Decimal("10000000"),
        policy=policy,
    )
    assert result.annual_ad_valorem_tax == Decimal("250000.00")

    rounding = calculate_property_tax(
        purchase_price=Decimal("10000000.50"),
        policy={**policy, "millage_rate_mills": 25.31},
    )
    assert rounding.annual_ad_valorem_tax == Decimal("253100.01")


def test_apply_collapses_all_replaceable_aliases_and_preserves_special_assessment(
    minimal_deal_inputs,
):
    canonical = minimal_deal_inputs
    canonical["metadata"]["property_summary"] = {
        "property_tax_policy": {
            "millage_rate_mills": 25.0,
            "assessment_ratio": 1.0,
            "source": "analyst",
            "source_locator": "underwrite-deal:property_tax_millage",
            "analyst_override": False,
        }
    }
    canonical["opex_table"].extend(
        [
            {
                "category_name": " property   taxes ",
                "calculation_type": "fixed_annual",
                "base_value": 1,
            },
            {
                "category_name": "RE Tax",
                "calculation_type": "fixed_annual",
                "base_value": 2,
            },
            {
                "category_name": "BID Special Assessment",
                "calculation_type": "fixed_annual",
                "base_value": 10000,
            },
        ]
    )

    priced, calculation = apply_property_tax_policy(
        canonical,
        purchase_price=Decimal("10000000"),
    )

    assert (
        [row["category_name"] for row in priced["opex_table"]].count(
            "Real Estate Taxes"
        )
        == 1
    )
    assert next(
        row
        for row in priced["opex_table"]
        if row["category_name"] == "Real Estate Taxes"
    )["base_value"] == 250000.0
    assert any(
        row["category_name"] == "BID Special Assessment"
        for row in priced["opex_table"]
    )
    assert calculation.purchase_price_basis == Decimal("10000000")


def test_missing_policy_never_retains_trailing_tax(minimal_deal_inputs):
    minimal_deal_inputs["metadata"]["property_summary"].pop("property_tax_policy")
    minimal_deal_inputs["opex_table"].append(
        {
            "category_name": "Real Estate Taxes",
            "calculation_type": "fixed_annual",
            "base_value": 123456,
        }
    )

    with pytest.raises(PropertyTaxPolicyError) as exc:
        apply_property_tax_policy(
            minimal_deal_inputs,
            purchase_price=Decimal("10000000"),
        )

    assert exc.value.code == "missing_property_tax_millage"


@pytest.mark.parametrize(
    "value",
    [
        Decimal("1e-400"),
        Decimal("1e400"),
        Decimal("25.3100000000000001"),
    ],
)
def test_policy_builder_rejects_values_without_exact_finite_json_number(value):
    with pytest.raises(PropertyTaxPolicyError) as exc:
        build_property_tax_policy(
            millage_rate=value,
            unit="mills",
            source="analyst",
            source_locator="tests:json-number-boundary",
        )

    assert exc.value.code == "invalid_property_tax_millage"
    assert "finite JSON number" in str(exc.value)


@pytest.mark.parametrize(
    "purchase_price",
    [
        Decimal("1e-400"),
        Decimal("1e400"),
        Decimal("10000000.1999999999"),
    ],
)
def test_calculation_rejects_purchase_price_without_exact_finite_json_number(
    purchase_price,
):
    policy = {
        "millage_rate_mills": 25.0,
        "assessment_ratio": 1.0,
        "source": "analyst",
        "source_locator": "tests:json-number-boundary",
        "analyst_override": False,
    }

    with pytest.raises(PropertyTaxPolicyError) as exc:
        calculate_property_tax(
            purchase_price=purchase_price,
            policy=policy,
        ).to_json()

    assert exc.value.code == "invalid_property_tax_millage"
    assert "finite JSON number" in str(exc.value)


def test_policy_and_calculation_json_are_standard_and_self_validating():
    policy = build_property_tax_policy(
        millage_rate=Decimal("25.31"),
        unit="mills",
        source="analyst",
        source_locator="tests:json-number-boundary",
    )
    calculation = calculate_property_tax(
        purchase_price=Decimal("10000000.50"),
        policy=policy,
    ).to_json()

    assert validate_property_tax_policy(policy) == policy
    json.dumps(policy, allow_nan=False)
    json.dumps(calculation, allow_nan=False)
