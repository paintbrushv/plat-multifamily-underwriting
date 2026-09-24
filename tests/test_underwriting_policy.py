from decimal import Decimal

from engine.underwriting_policy import (
    default_collection_loss_rate,
    default_loss_to_lease_rate,
    default_other_income_per_unit_month,
    default_property_tax_reassessment_ratio,
    default_physical_vacancy_rate,
    default_r_and_m_per_unit,
    default_replacement_reserve_per_unit,
    default_turn_capex_per_unit,
    default_vintage_operating_profile,
    default_year1_concessions_rate,
    default_year1_insurance_per_unit,
    estimated_year1_property_tax,
    insurance_requires_claim_review,
    revenue_guardrails,
    vintage_bucket,
)


def test_vintage_bucket_and_cashflow_defaults_for_1980s():
    assert vintage_bucket(1984) == "1980s"
    assert default_r_and_m_per_unit(1984) == Decimal("900")
    assert default_replacement_reserve_per_unit(1984) == Decimal("1000")
    assert default_turn_capex_per_unit(1984) == Decimal("1800")


def test_value_add_defaults_use_more_conservative_midpoints():
    assert default_r_and_m_per_unit(1984, strategy="value_add") == Decimal("1300")
    assert default_replacement_reserve_per_unit(1984, strategy="value_add") == Decimal("1750")
    assert default_turn_capex_per_unit(1984, strategy="value_add") == Decimal("5750")
    assert default_physical_vacancy_rate(1984, strategy="value_add") == Decimal("0.060")
    assert default_collection_loss_rate(1984, strategy="value_add") == Decimal("0.0100")
    assert default_year1_concessions_rate(1984, strategy="value_add") == Decimal("0.0100")


def test_revenue_defaults_for_1980s_cashflow():
    assert default_loss_to_lease_rate(1984) == Decimal("0.010")
    assert default_physical_vacancy_rate(1984) == Decimal("0.050")
    assert default_collection_loss_rate(1984) == Decimal("0.0075")
    assert default_year1_concessions_rate(1984) == Decimal("0.0075")
    assert default_other_income_per_unit_month(1984) == Decimal("100")


def test_revenue_guardrails_expose_typical_and_stress_bands():
    guardrails = revenue_guardrails(1984)
    assert guardrails["vintage_bucket"] == "1980s"
    assert guardrails["physical_vacancy_typical_low"] == Decimal("0.045")
    assert guardrails["physical_vacancy_typical_high"] == Decimal("0.070")
    assert guardrails["economic_vacancy_typical_low"] == Decimal("0.080")
    assert guardrails["economic_vacancy_stress_high"] == Decimal("0.200")
    assert guardrails["bad_debt_typical_high"] == Decimal("0.035")
    assert guardrails["concessions_stress_high"] == Decimal("0.070")
    assert guardrails["other_income_high_per_unit_month"] == Decimal("225")
    assert guardrails["physical_vacancy_house_floor"] == Decimal("0.050")


def test_insurance_floor_and_claim_review_trigger():
    assert default_year1_insurance_per_unit() == Decimal("600")
    assert default_year1_insurance_per_unit(500) == Decimal("600")
    assert default_year1_insurance_per_unit(950) == Decimal("950")
    assert insurance_requires_claim_review(950) is True
    assert insurance_requires_claim_review(600) is False


def test_property_tax_reassessment_defaults_to_full_value():
    assert default_property_tax_reassessment_ratio() == Decimal("1.00")
    assert estimated_year1_property_tax(
        purchase_price=10_000_000,
        tax_rate_pct=0.025,
    ) == Decimal("250000.000")


def test_property_tax_reassessment_supports_approved_ratio_override():
    assert default_property_tax_reassessment_ratio(
        analyst_ratio_override=0.70
    ) == Decimal("0.70")
    assert estimated_year1_property_tax(
        purchase_price=10_000_000,
        tax_rate_pct=0.025,
        analyst_ratio_override=0.70,
    ) == Decimal("175000.0000")


def test_operating_profile_summary_reflects_house_policy():
    profile = default_vintage_operating_profile(
        1984,
        strategy="cashflow",
        observed_insurance_per_unit=950,
    )
    assert profile["vintage_bucket"] == "1980s"
    assert profile["r_and_m_per_unit"] == 900.0
    assert profile["economic_capex_reserve_per_unit"] == 1000.0
    assert profile["year1_insurance_per_unit"] == 950.0
    assert profile["insurance_claim_review_required"] is True
    assert profile["property_tax_reassessment_ratio"] == 1.0
    assert profile["loss_to_lease_rate"] == 0.01
    assert profile["physical_vacancy_rate"] == 0.05
    assert profile["collection_loss_rate"] == 0.0075
    assert profile["year1_concessions_rate"] == 0.0075
