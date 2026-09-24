from __future__ import annotations

from decimal import Decimal
from typing import Optional


# Cap-rate sanity band (inclusive): going-in AND exit cap must both fall
# within this range or the feasibility classifier emits a HARD flag.
# Both engine/feasibility.py and engine/validator.py import this constant
# so the two check sites cannot drift apart.
CAP_RATE_SANITY_BAND: tuple = (0.06, 0.12)

PRIMARY_COC_TARGET_PCT = Decimal("0.07")
PRIMARY_IRR_TARGET_LOW_PCT = Decimal("0.10")
PRIMARY_IRR_TARGET_HIGH_PCT = Decimal("0.13")

DEFAULT_AGENCY_SPREAD_PCT = Decimal("0.0150")

# Saved-down analyst defaults used when a live deal lacks an explicit fee budget.
DEFAULT_PURCHASE_CLOSING_COST_PCT = Decimal("0.015")
DEFAULT_PARTNERSHIP_CLOSING_COSTS = Decimal("50000")
DEFAULT_ACQUISITION_FEE_PCT = Decimal("0.01")
DEFAULT_ASSET_MANAGEMENT_FEE_PCT = Decimal("0.015")
DEFAULT_DISPOSITION_FEE_PCT = Decimal("0.01")
DEFAULT_ANNUAL_PARTNERSHIP_EXPENSES = Decimal("25000")
DEFAULT_YEAR1_INSURANCE_PER_UNIT = Decimal("600")
HIGH_INSURANCE_REVIEW_THRESHOLD_PER_UNIT = Decimal("900")
DEFAULT_PROPERTY_TAX_REASSESSMENT_RATIO = Decimal("1.00")

# Institutional vintage framework supplied by the user on 2026-05-08.
# Ranges are in 2026 dollars and represent annual/unit reserve floors plus a
# rough turn-cost band for use when a deal lacks better property-specific data.
VINTAGE_FRAMEWORK_2026 = {
    "1960s": {
        "year_min": 1960,
        "year_max": 1969,
        "r_and_m_low": Decimal("1400"),
        "r_and_m_high": Decimal("2400"),
        "economic_capex_low": Decimal("1500"),
        "economic_capex_high": Decimal("3500"),
        "turn_low": Decimal("2500"),
        "turn_high": Decimal("6500"),
        "ltl_floor": Decimal("0.020"),
        "vacancy_floor": Decimal("0.080"),
        "collection_loss_floor": Decimal("0.015"),
        "year1_concessions_floor": Decimal("0.010"),
        "physical_vacancy_typical_low": Decimal("0.050"),
        "physical_vacancy_typical_high": Decimal("0.080"),
        "physical_vacancy_stress_low": Decimal("0.080"),
        "physical_vacancy_stress_high": Decimal("0.120"),
        "economic_vacancy_typical_low": Decimal("0.100"),
        "economic_vacancy_typical_high": Decimal("0.180"),
        "economic_vacancy_stress_low": Decimal("0.180"),
        "economic_vacancy_stress_high": Decimal("0.250"),
        "bad_debt_typical_low": Decimal("0.020"),
        "bad_debt_typical_high": Decimal("0.050"),
        "bad_debt_stress_low": Decimal("0.050"),
        "bad_debt_stress_high": Decimal("0.080"),
        "concessions_typical_low": Decimal("0.000"),
        "concessions_typical_high": Decimal("0.030"),
        "concessions_stress_low": Decimal("0.030"),
        "concessions_stress_high": Decimal("0.080"),
        "other_income_low_per_unit_month": Decimal("75"),
        "other_income_high_per_unit_month": Decimal("175"),
        "other_income_strong_low_per_unit_month": Decimal("150"),
        "other_income_strong_high_per_unit_month": Decimal("250"),
    },
    "1970s": {
        "year_min": 1970,
        "year_max": 1979,
        "r_and_m_low": Decimal("1200"),
        "r_and_m_high": Decimal("2000"),
        "economic_capex_low": Decimal("1250"),
        "economic_capex_high": Decimal("3000"),
        "turn_low": Decimal("2000"),
        "turn_high": Decimal("5500"),
        "ltl_floor": Decimal("0.0175"),
        "vacancy_floor": Decimal("0.070"),
        "collection_loss_floor": Decimal("0.0125"),
        "year1_concessions_floor": Decimal("0.010"),
        "physical_vacancy_typical_low": Decimal("0.050"),
        "physical_vacancy_typical_high": Decimal("0.070"),
        "physical_vacancy_stress_low": Decimal("0.080"),
        "physical_vacancy_stress_high": Decimal("0.110"),
        "economic_vacancy_typical_low": Decimal("0.090"),
        "economic_vacancy_typical_high": Decimal("0.160"),
        "economic_vacancy_stress_low": Decimal("0.160"),
        "economic_vacancy_stress_high": Decimal("0.230"),
        "bad_debt_typical_low": Decimal("0.015"),
        "bad_debt_typical_high": Decimal("0.045"),
        "bad_debt_stress_low": Decimal("0.050"),
        "bad_debt_stress_high": Decimal("0.070"),
        "concessions_typical_low": Decimal("0.000"),
        "concessions_typical_high": Decimal("0.040"),
        "concessions_stress_low": Decimal("0.040"),
        "concessions_stress_high": Decimal("0.080"),
        "other_income_low_per_unit_month": Decimal("85"),
        "other_income_high_per_unit_month": Decimal("200"),
        "other_income_strong_low_per_unit_month": Decimal("175"),
        "other_income_strong_high_per_unit_month": Decimal("275"),
    },
    "1980s": {
        "year_min": 1980,
        "year_max": 1989,
        "r_and_m_low": Decimal("900"),
        "r_and_m_high": Decimal("1700"),
        "economic_capex_low": Decimal("1000"),
        "economic_capex_high": Decimal("2500"),
        "turn_low": Decimal("1800"),
        "turn_high": Decimal("4500"),
        "value_add_turn_low": Decimal("4000"),
        "value_add_turn_high": Decimal("7500"),
        "ltl_floor": Decimal("0.010"),
        "vacancy_floor": Decimal("0.050"),
        "collection_loss_floor": Decimal("0.0075"),
        "year1_concessions_floor": Decimal("0.0075"),
        "physical_vacancy_typical_low": Decimal("0.045"),
        "physical_vacancy_typical_high": Decimal("0.070"),
        "physical_vacancy_stress_low": Decimal("0.070"),
        "physical_vacancy_stress_high": Decimal("0.100"),
        "economic_vacancy_typical_low": Decimal("0.080"),
        "economic_vacancy_typical_high": Decimal("0.140"),
        "economic_vacancy_stress_low": Decimal("0.140"),
        "economic_vacancy_stress_high": Decimal("0.200"),
        "bad_debt_typical_low": Decimal("0.010"),
        "bad_debt_typical_high": Decimal("0.035"),
        "bad_debt_stress_low": Decimal("0.040"),
        "bad_debt_stress_high": Decimal("0.060"),
        "concessions_typical_low": Decimal("0.000"),
        "concessions_typical_high": Decimal("0.030"),
        "concessions_stress_low": Decimal("0.030"),
        "concessions_stress_high": Decimal("0.070"),
        "other_income_low_per_unit_month": Decimal("100"),
        "other_income_high_per_unit_month": Decimal("225"),
        "other_income_strong_low_per_unit_month": Decimal("200"),
        "other_income_strong_high_per_unit_month": Decimal("325"),
    },
    "1990s": {
        "year_min": 1990,
        "year_max": 1999,
        "r_and_m_low": Decimal("700"),
        "r_and_m_high": Decimal("1400"),
        "economic_capex_low": Decimal("750"),
        "economic_capex_high": Decimal("2000"),
        "turn_low": Decimal("1500"),
        "turn_high": Decimal("3000"),
        "value_add_turn_low": Decimal("3500"),
        "value_add_turn_high": Decimal("6500"),
        "ltl_floor": Decimal("0.010"),
        "vacancy_floor": Decimal("0.050"),
        "collection_loss_floor": Decimal("0.0050"),
        "year1_concessions_floor": Decimal("0.0050"),
        "physical_vacancy_typical_low": Decimal("0.040"),
        "physical_vacancy_typical_high": Decimal("0.065"),
        "physical_vacancy_stress_low": Decimal("0.070"),
        "physical_vacancy_stress_high": Decimal("0.090"),
        "economic_vacancy_typical_low": Decimal("0.070"),
        "economic_vacancy_typical_high": Decimal("0.120"),
        "economic_vacancy_stress_low": Decimal("0.120"),
        "economic_vacancy_stress_high": Decimal("0.170"),
        "bad_debt_typical_low": Decimal("0.0075"),
        "bad_debt_typical_high": Decimal("0.025"),
        "bad_debt_stress_low": Decimal("0.030"),
        "bad_debt_stress_high": Decimal("0.050"),
        "concessions_typical_low": Decimal("0.000"),
        "concessions_typical_high": Decimal("0.030"),
        "concessions_stress_low": Decimal("0.030"),
        "concessions_stress_high": Decimal("0.060"),
        "other_income_low_per_unit_month": Decimal("125"),
        "other_income_high_per_unit_month": Decimal("250"),
        "other_income_strong_low_per_unit_month": Decimal("225"),
        "other_income_strong_high_per_unit_month": Decimal("350"),
    },
    "2000s": {
        "year_min": 2000,
        "year_max": 2009,
        "r_and_m_low": Decimal("600"),
        "r_and_m_high": Decimal("1200"),
        "economic_capex_low": Decimal("500"),
        "economic_capex_high": Decimal("1500"),
        "turn_low": Decimal("1200"),
        "turn_high": Decimal("2500"),
        "value_add_turn_low": Decimal("3000"),
        "value_add_turn_high": Decimal("5500"),
        "ltl_floor": Decimal("0.0075"),
        "vacancy_floor": Decimal("0.045"),
        "collection_loss_floor": Decimal("0.0050"),
        "year1_concessions_floor": Decimal("0.0035"),
        "physical_vacancy_typical_low": Decimal("0.040"),
        "physical_vacancy_typical_high": Decimal("0.060"),
        "physical_vacancy_stress_low": Decimal("0.065"),
        "physical_vacancy_stress_high": Decimal("0.085"),
        "economic_vacancy_typical_low": Decimal("0.065"),
        "economic_vacancy_typical_high": Decimal("0.110"),
        "economic_vacancy_stress_low": Decimal("0.110"),
        "economic_vacancy_stress_high": Decimal("0.150"),
        "bad_debt_typical_low": Decimal("0.0050"),
        "bad_debt_typical_high": Decimal("0.020"),
        "bad_debt_stress_low": Decimal("0.025"),
        "bad_debt_stress_high": Decimal("0.040"),
        "concessions_typical_low": Decimal("0.000"),
        "concessions_typical_high": Decimal("0.030"),
        "concessions_stress_low": Decimal("0.030"),
        "concessions_stress_high": Decimal("0.070"),
        "other_income_low_per_unit_month": Decimal("150"),
        "other_income_high_per_unit_month": Decimal("300"),
        "other_income_strong_low_per_unit_month": Decimal("250"),
        "other_income_strong_high_per_unit_month": Decimal("400"),
    },
    "2010s": {
        "year_min": 2010,
        "year_max": 2019,
        "r_and_m_low": Decimal("500"),
        "r_and_m_high": Decimal("1000"),
        "economic_capex_low": Decimal("300"),
        "economic_capex_high": Decimal("1000"),
        "turn_low": Decimal("1000"),
        "turn_high": Decimal("2200"),
        "ltl_floor": Decimal("0.0050"),
        "vacancy_floor": Decimal("0.045"),
        "collection_loss_floor": Decimal("0.0040"),
        "year1_concessions_floor": Decimal("0.0025"),
        "physical_vacancy_typical_low": Decimal("0.040"),
        "physical_vacancy_typical_high": Decimal("0.065"),
        "physical_vacancy_stress_low": Decimal("0.070"),
        "physical_vacancy_stress_high": Decimal("0.100"),
        "economic_vacancy_typical_low": Decimal("0.070"),
        "economic_vacancy_typical_high": Decimal("0.130"),
        "economic_vacancy_stress_low": Decimal("0.130"),
        "economic_vacancy_stress_high": Decimal("0.200"),
        "bad_debt_typical_low": Decimal("0.0025"),
        "bad_debt_typical_high": Decimal("0.015"),
        "bad_debt_stress_low": Decimal("0.020"),
        "bad_debt_stress_high": Decimal("0.035"),
        "concessions_typical_low": Decimal("0.010"),
        "concessions_typical_high": Decimal("0.050"),
        "concessions_stress_low": Decimal("0.050"),
        "concessions_stress_high": Decimal("0.120"),
        "other_income_low_per_unit_month": Decimal("200"),
        "other_income_high_per_unit_month": Decimal("400"),
        "other_income_strong_low_per_unit_month": Decimal("350"),
        "other_income_strong_high_per_unit_month": Decimal("550"),
    },
    "2020s": {
        "year_min": 2020,
        "year_max": 2029,
        "r_and_m_low": Decimal("400"),
        "r_and_m_high": Decimal("900"),
        "economic_capex_low": Decimal("200"),
        "economic_capex_high": Decimal("750"),
        "turn_low": Decimal("800"),
        "turn_high": Decimal("2000"),
        "ltl_floor": Decimal("0.0050"),
        "vacancy_floor": Decimal("0.040"),
        "collection_loss_floor": Decimal("0.0035"),
        "year1_concessions_floor": Decimal("0.0025"),
        "physical_vacancy_typical_low": Decimal("0.040"),
        "physical_vacancy_typical_high": Decimal("0.070"),
        "physical_vacancy_stress_low": Decimal("0.080"),
        "physical_vacancy_stress_high": Decimal("0.150"),
        "economic_vacancy_typical_low": Decimal("0.080"),
        "economic_vacancy_typical_high": Decimal("0.150"),
        "economic_vacancy_stress_low": Decimal("0.150"),
        "economic_vacancy_stress_high": Decimal("0.250"),
        "bad_debt_typical_low": Decimal("0.0025"),
        "bad_debt_typical_high": Decimal("0.0125"),
        "bad_debt_stress_low": Decimal("0.015"),
        "bad_debt_stress_high": Decimal("0.030"),
        "concessions_typical_low": Decimal("0.020"),
        "concessions_typical_high": Decimal("0.070"),
        "concessions_stress_low": Decimal("0.080"),
        "concessions_stress_high": Decimal("0.150"),
        "other_income_low_per_unit_month": Decimal("250"),
        "other_income_high_per_unit_month": Decimal("500"),
        "other_income_strong_low_per_unit_month": Decimal("400"),
        "other_income_strong_high_per_unit_month": Decimal("650"),
    },
}


def vintage_bucket(year_built: Optional[int]) -> Optional[str]:
    if year_built is None:
        return None
    for label, row in VINTAGE_FRAMEWORK_2026.items():
        if row["year_min"] <= year_built <= row["year_max"]:
            return label
    if year_built < 1960:
        return "1960s"
    if year_built >= 2030:
        return "2020s"
    return None


def default_replacement_reserve_per_unit(
    year_built: Optional[int],
    *,
    strategy: str = "cashflow",
    deferred: bool = False,
) -> Optional[Decimal]:
    """Return a conservative annual/unit economic CapEx reserve.

    Default policy:
    - cashflow / stabilized deals start at the low end of the sustainable band
    - value-add / deferred deals use the midpoint of the sustainable band
    - explicitly deferred assets can be pushed to the high end
    """
    bucket = vintage_bucket(year_built)
    if bucket is None:
        return None
    row = VINTAGE_FRAMEWORK_2026[bucket]
    low = row["economic_capex_low"]
    high = row["economic_capex_high"]
    if deferred:
        return high
    if strategy == "value_add":
        return (low + high) / Decimal("2")
    return low


def default_r_and_m_per_unit(
    year_built: Optional[int],
    *,
    strategy: str = "cashflow",
    deferred: bool = False,
) -> Optional[Decimal]:
    """Return a conservative annual/unit R&M assumption by vintage."""
    bucket = vintage_bucket(year_built)
    if bucket is None:
        return None
    row = VINTAGE_FRAMEWORK_2026[bucket]
    low = row["r_and_m_low"]
    high = row["r_and_m_high"]
    if deferred:
        return high
    if strategy == "value_add":
        return (low + high) / Decimal("2")
    return low


def default_turn_capex_per_unit(
    year_built: Optional[int],
    *,
    strategy: str = "cashflow",
) -> Optional[Decimal]:
    bucket = vintage_bucket(year_built)
    if bucket is None:
        return None
    row = VINTAGE_FRAMEWORK_2026[bucket]
    if strategy == "value_add" and "value_add_turn_low" in row:
        return (row["value_add_turn_low"] + row["value_add_turn_high"]) / Decimal("2")
    return row["turn_low"]


def default_loss_to_lease_rate(
    year_built: Optional[int],
    *,
    strategy: str = "cashflow",
) -> Optional[Decimal]:
    bucket = vintage_bucket(year_built)
    if bucket is None:
        return None
    value = VINTAGE_FRAMEWORK_2026[bucket]["ltl_floor"]
    if strategy == "value_add":
        return value + Decimal("0.0025")
    return value


def default_physical_vacancy_rate(
    year_built: Optional[int],
    *,
    strategy: str = "cashflow",
    deferred: bool = False,
) -> Optional[Decimal]:
    bucket = vintage_bucket(year_built)
    if bucket is None:
        return None
    value = VINTAGE_FRAMEWORK_2026[bucket]["vacancy_floor"]
    if deferred or strategy == "value_add":
        return value + Decimal("0.010")
    return value


def default_collection_loss_rate(
    year_built: Optional[int],
    *,
    strategy: str = "cashflow",
    deferred: bool = False,
) -> Optional[Decimal]:
    bucket = vintage_bucket(year_built)
    if bucket is None:
        return None
    value = VINTAGE_FRAMEWORK_2026[bucket]["collection_loss_floor"]
    if deferred or strategy == "value_add":
        return value + Decimal("0.0025")
    return value


def default_year1_concessions_rate(
    year_built: Optional[int],
    *,
    strategy: str = "cashflow",
    deferred: bool = False,
) -> Optional[Decimal]:
    bucket = vintage_bucket(year_built)
    if bucket is None:
        return None
    value = VINTAGE_FRAMEWORK_2026[bucket]["year1_concessions_floor"]
    if deferred or strategy == "value_add":
        return value + Decimal("0.0025")
    return value


def revenue_guardrails(
    year_built: Optional[int],
    *,
    strategy: str = "cashflow",
    deferred: bool = False,
) -> dict[str, Decimal | str | None]:
    bucket = vintage_bucket(year_built)
    if bucket is None:
        return {
            "vintage_bucket": None,
            "strategy": strategy,
            "physical_vacancy_typical_low": None,
            "physical_vacancy_typical_high": None,
            "physical_vacancy_stress_low": None,
            "physical_vacancy_stress_high": None,
            "economic_vacancy_typical_low": None,
            "economic_vacancy_typical_high": None,
            "economic_vacancy_stress_low": None,
            "economic_vacancy_stress_high": None,
            "bad_debt_typical_low": None,
            "bad_debt_typical_high": None,
            "bad_debt_stress_low": None,
            "bad_debt_stress_high": None,
            "concessions_typical_low": None,
            "concessions_typical_high": None,
            "concessions_stress_low": None,
            "concessions_stress_high": None,
            "other_income_low_per_unit_month": None,
            "other_income_high_per_unit_month": None,
            "other_income_strong_low_per_unit_month": None,
            "other_income_strong_high_per_unit_month": None,
        }

    row = VINTAGE_FRAMEWORK_2026[bucket]
    return {
        "vintage_bucket": bucket,
        "strategy": strategy,
        "physical_vacancy_typical_low": row["physical_vacancy_typical_low"],
        "physical_vacancy_typical_high": row["physical_vacancy_typical_high"],
        "physical_vacancy_stress_low": row["physical_vacancy_stress_low"],
        "physical_vacancy_stress_high": row["physical_vacancy_stress_high"],
        "economic_vacancy_typical_low": row["economic_vacancy_typical_low"],
        "economic_vacancy_typical_high": row["economic_vacancy_typical_high"],
        "economic_vacancy_stress_low": row["economic_vacancy_stress_low"],
        "economic_vacancy_stress_high": row["economic_vacancy_stress_high"],
        "bad_debt_typical_low": row["bad_debt_typical_low"],
        "bad_debt_typical_high": row["bad_debt_typical_high"],
        "bad_debt_stress_low": row["bad_debt_stress_low"],
        "bad_debt_stress_high": row["bad_debt_stress_high"],
        "concessions_typical_low": row["concessions_typical_low"],
        "concessions_typical_high": row["concessions_typical_high"],
        "concessions_stress_low": row["concessions_stress_low"],
        "concessions_stress_high": row["concessions_stress_high"],
        "other_income_low_per_unit_month": row["other_income_low_per_unit_month"],
        "other_income_high_per_unit_month": row["other_income_high_per_unit_month"],
        "other_income_strong_low_per_unit_month": row["other_income_strong_low_per_unit_month"],
        "other_income_strong_high_per_unit_month": row["other_income_strong_high_per_unit_month"],
        "physical_vacancy_house_floor": default_physical_vacancy_rate(
            year_built,
            strategy=strategy,
            deferred=deferred,
        ),
        "collection_loss_house_floor": default_collection_loss_rate(
            year_built,
            strategy=strategy,
            deferred=deferred,
        ),
        "concessions_house_floor": default_year1_concessions_rate(
            year_built,
            strategy=strategy,
            deferred=deferred,
        ),
        "loss_to_lease_house_floor": default_loss_to_lease_rate(
            year_built,
            strategy=strategy,
        ),
    }


def default_other_income_per_unit_month(
    year_built: Optional[int],
    *,
    strong_operator: bool = False,
) -> Optional[Decimal]:
    bucket = vintage_bucket(year_built)
    if bucket is None:
        return None
    row = VINTAGE_FRAMEWORK_2026[bucket]
    if strong_operator:
        return row["other_income_strong_low_per_unit_month"]
    return row["other_income_low_per_unit_month"]


def default_year1_insurance_per_unit(
    observed_per_unit: Optional[float | Decimal] = None,
) -> Decimal:
    """Apply the conservative Year-1 insurance floor.

    Use the higher of observed run-rate insurance and the house minimum
    of $600/unit. When the observed run rate is $900+/unit, flag it for
    claims-history review rather than silently normalizing it away.
    """
    if observed_per_unit is None:
        return DEFAULT_YEAR1_INSURANCE_PER_UNIT
    observed = Decimal(str(observed_per_unit))
    return max(observed, DEFAULT_YEAR1_INSURANCE_PER_UNIT)


def insurance_requires_claim_review(
    observed_per_unit: Optional[float | Decimal] = None,
) -> bool:
    if observed_per_unit is None:
        return False
    return Decimal(str(observed_per_unit)) >= HIGH_INSURANCE_REVIEW_THRESHOLD_PER_UNIT


def default_property_tax_reassessment_ratio(
    *,
    analyst_ratio_override: Optional[float | Decimal] = None,
) -> Decimal:
    """Return the Year-1 property-tax assessment multiplier.

    The house default is full purchase value. ``analyst_ratio_override`` is
    reserved for an explicitly approved, evidence-backed non-default
    multiplier for the local assessment convention.
    """
    if analyst_ratio_override is not None:
        return Decimal(str(analyst_ratio_override))
    return DEFAULT_PROPERTY_TAX_REASSESSMENT_RATIO


def estimated_year1_property_tax(
    *,
    purchase_price: float | Decimal,
    tax_rate_pct: float | Decimal,
    analyst_ratio_override: Optional[float | Decimal] = None,
) -> Decimal:
    """Estimate Year-1 taxes from purchase price and local tax rate."""
    assessment_ratio = default_property_tax_reassessment_ratio(
        analyst_ratio_override=analyst_ratio_override,
    )
    return (
        Decimal(str(purchase_price))
        * assessment_ratio
        * Decimal(str(tax_rate_pct))
    )


def default_vintage_operating_profile(
    year_built: Optional[int],
    *,
    strategy: str = "cashflow",
    deferred: bool = False,
    observed_insurance_per_unit: Optional[float | Decimal] = None,
    analyst_tax_ratio_override: Optional[float | Decimal] = None,
) -> dict[str, float | str | bool | None]:
    """Return the house-default operating profile for orchestration.

    This is the condensed policy surface sibling orchestrators should use
    when they need a conservative first-pass assumption set.
    """
    bucket = vintage_bucket(year_built)
    return {
        "vintage_bucket": bucket,
        "strategy": strategy,
        "deferred": deferred,
        "r_and_m_per_unit": (
            float(default_r_and_m_per_unit(year_built, strategy=strategy, deferred=deferred))
            if default_r_and_m_per_unit(year_built, strategy=strategy, deferred=deferred) is not None
            else None
        ),
        "turn_cost_per_unit": (
            float(default_turn_capex_per_unit(year_built, strategy=strategy))
            if default_turn_capex_per_unit(year_built, strategy=strategy) is not None
            else None
        ),
        "economic_capex_reserve_per_unit": (
            float(default_replacement_reserve_per_unit(year_built, strategy=strategy, deferred=deferred))
            if default_replacement_reserve_per_unit(year_built, strategy=strategy, deferred=deferred) is not None
            else None
        ),
        "year1_insurance_per_unit": float(
            default_year1_insurance_per_unit(observed_insurance_per_unit)
        ),
        "insurance_claim_review_required": insurance_requires_claim_review(
            observed_insurance_per_unit
        ),
        "property_tax_reassessment_ratio": float(
            default_property_tax_reassessment_ratio(
                analyst_ratio_override=analyst_tax_ratio_override,
            )
        ),
        "loss_to_lease_rate": (
            float(default_loss_to_lease_rate(year_built, strategy=strategy))
            if default_loss_to_lease_rate(year_built, strategy=strategy) is not None
            else None
        ),
        "physical_vacancy_rate": (
            float(default_physical_vacancy_rate(year_built, strategy=strategy, deferred=deferred))
            if default_physical_vacancy_rate(year_built, strategy=strategy, deferred=deferred) is not None
            else None
        ),
        "collection_loss_rate": (
            float(default_collection_loss_rate(year_built, strategy=strategy, deferred=deferred))
            if default_collection_loss_rate(year_built, strategy=strategy, deferred=deferred) is not None
            else None
        ),
        "year1_concessions_rate": (
            float(default_year1_concessions_rate(year_built, strategy=strategy, deferred=deferred))
            if default_year1_concessions_rate(year_built, strategy=strategy, deferred=deferred) is not None
            else None
        ),
        "other_income_per_unit_month": (
            float(default_other_income_per_unit_month(year_built))
            if default_other_income_per_unit_month(year_built) is not None
            else None
        ),
        "revenue_guardrails": {
            key: (float(value) if isinstance(value, Decimal) else value)
            for key, value in revenue_guardrails(
                year_built,
                strategy=strategy,
                deferred=deferred,
            ).items()
        },
    }
