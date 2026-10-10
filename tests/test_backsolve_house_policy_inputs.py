"""Regression: backsolve house policy must not misread or silently override analyst inputs.

Synthetic 447-unit, 1984-vintage deal (house R&M floor $900/unit, insurance
floor $600/unit). Each case used to produce a wrong price with no error:

1. a concession_schedule with only free_months rows crashed (max of empty);
2. per_unit opex lines were read as annual dollars;
3. a per-unit "Repairs & Maintenance + Turnover" line was bucketed as
   make-ready, so the R&M floor was stacked on top of it;
4. the vacancy anchor ignored "Vacant ..." status variants and a negative
   T12 vacancy loss;
5. a time-varying vacancy curve was flattened with no disclosure.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pytest

from engine.backsolve import _apply_house_revenue_policy, _prepare_house_assumptions

UNITS = 447


def _canonical(**overrides):
    canonical = {
        "metadata": {"year_built": 1984, "property_summary": {}},
        "time_grid": {"analysis_start_date": "2026-09-01", "analysis_end_date": "2031-08-01"},
        "unit_cohorts": [
            {"cohort_id": "a1", "unit_type": "A1", "unit_count": UNITS, "initial_inplace_rent": 1200.0},
        ],
        "market_rent_curve": [
            {"cohort_id": "a1", "start_period": "2026-09", "end_period": "2031-08", "market_rent": 1250.0},
        ],
        "loss_to_lease": [
            {"cohort_id": "a1", "start_period": "2026-09", "end_period": "2031-08", "ltl_percent": 0.04},
        ],
        "physical_vacancy_curve": [
            {"cohort_id": "a1", "start_period": "2026-09", "end_period": "2031-08", "vacancy_rate": 0.05},
        ],
        "collection_loss_curve": [],
        "opex_table": [
            {"category_name": "Insurance", "calculation_type": "per_unit",
             "base_value": 875.0, "recoverable_flag": False},
            {"category_name": "Repairs & Maintenance + Turnover", "calculation_type": "per_unit",
             "base_value": 900.0, "recoverable_flag": False},
        ],
        "exit_assumptions": {"exit_cap_rate": 0.06},
    }
    canonical.update(overrides)
    return canonical


def _prepare(canonical, **kwargs):
    return _prepare_house_assumptions(
        canonical,
        year_built=None,
        strategy="cashflow",
        target_coc=Decimal("0.07"),
        benchmark_treasury=Decimal("0.04"),
        agency_spread=Decimal("0.015"),
        exit_cap_rate=Decimal("0.06"),
        sale_cost_percent=Decimal("0.02"),
        purchase_closing_cost_pct=Decimal("0.015"),
        partnership_closing_costs=Decimal("50000"),
        acquisition_fee_pct=Decimal("0.01"),
        asset_management_fee_pct=Decimal("0.015"),
        annual_partnership_expenses=Decimal("25000"),
        disposition_fee_pct=Decimal("0.01"),
        loan_closing_costs=Decimal("0"),
        **kwargs,
    )


def _revenue_policy(canonical, **kwargs):
    return _apply_house_revenue_policy(canonical, year_built=1984, strategy="cashflow", **kwargs)


def test_free_months_only_concessions_do_not_crash():
    canonical = _canonical(concession_schedule=[
        {"concession_id": "lease_up", "start_month": "2026-09", "end_month": "2027-08",
         "concession_type": "free_months", "amount": 1.5, "applies_to_cohort": "ALL"},
    ])
    summary = _revenue_policy(canonical)

    # 1.5 free months on a 12-month lease = 12.5% of rent.
    assert summary["year1_concessions_rate_applied"] == pytest.approx(0.125)
    assert canonical["concession_schedule"][0]["concession_type"] == "pct_rent"
    assert any(o["input"] == "concession_schedule" for o in summary["policy_overrides"])


def test_unsupported_concession_type_fails_closed():
    canonical = _canonical(concession_schedule=[
        {"concession_id": "gift", "start_month": "2026-09", "end_month": "2027-08",
         "concession_type": "fixed_dollar", "amount": 250, "applies_to_cohort": "ALL"},
    ])
    with pytest.raises(ValueError, match="fixed_dollar"):
        _revenue_policy(canonical)


def test_per_unit_insurance_is_annualized_before_the_floor():
    prepared, summary = _prepare(_canonical())

    assert summary["observed_insurance_per_unit"] == pytest.approx(875.0)
    assert summary["insurance_per_unit_applied"] == pytest.approx(875.0)
    insurance = [r for r in prepared["opex_table"] if "insurance" in r["category_name"].lower()]
    assert len(insurance) == 1
    assert insurance[0]["calculation_type"] == "fixed_annual"
    assert insurance[0]["base_value"] == pytest.approx(875.0 * UNITS)


def test_insurance_floor_replaces_rather_than_stacks_on_differently_named_line():
    canonical = _canonical()
    canonical["opex_table"][0]["category_name"] = "Property Insurance"
    canonical["opex_table"][0]["base_value"] = 400.0
    prepared, summary = _prepare(canonical)

    insurance = [r for r in prepared["opex_table"] if "insurance" in r["category_name"].lower()]
    assert len(insurance) == 1
    assert insurance[0]["base_value"] == pytest.approx(600.0 * UNITS)
    override = next(o for o in summary["policy_overrides"] if o["input"] == "opex_table.insurance")
    assert override["original_annual"] == pytest.approx(400.0 * UNITS)


def test_insurance_floor_skips_employee_health_insurance():
    canonical = _canonical()
    canonical["opex_table"].insert(0, {
        "category_name": "Employee Health Insurance", "calculation_type": "fixed_annual",
        "base_value": 90_000.0, "recoverable_flag": False,
    })
    prepared, summary = _prepare(canonical)

    by_name = {r["category_name"]: r["base_value"] for r in prepared["opex_table"]}
    assert by_name["Employee Health Insurance"] == 90_000.0
    assert by_name["Insurance"] == pytest.approx(875.0 * UNITS)
    assert summary["observed_insurance_per_unit"] == pytest.approx(875.0)


def test_combined_r_and_m_turnover_line_meets_the_floor_without_top_up():
    prepared, summary = _prepare(_canonical())

    assert summary["observed_repairs_maintenance_annual"] == pytest.approx(402_300.0)
    assert summary["repairs_maintenance_floor_annual"] == pytest.approx(402_300.0)
    assert summary["repairs_maintenance_top_up_annual"] == 0.0
    assert not any("Top-Up" in r["category_name"] for r in prepared["opex_table"])


def test_vacancy_anchor_reads_vacant_status_variants_and_negative_t12_loss():
    canonical = _canonical()
    canonical["metadata"]["property_summary"]["house_box_score"] = {
        "total_units": UNITS,
        "status_counts": {"Occupied": 401, "Vacant Unrented": 30, "Vacant-Rented": 11, "Notice Unrented": 5},
    }
    summary = _revenue_policy(
        canonical,
        trailing_actuals={"gross_potential_rent": 6_000_000.0, "vacancy_loss": -375_000.0},
    )
    candidates = summary["physical_vacancy_anchor_summary"]["base_case_candidates"]

    assert candidates["rent_roll_vacant"] == pytest.approx(41 / UNITS)
    assert candidates["t12_vacancy_gpr"] == pytest.approx(0.0625)
    assert summary["physical_vacancy_rate_applied"] == pytest.approx(41 / UNITS)


def test_overridden_time_varying_vacancy_curve_is_disclosed():
    canonical = _canonical(physical_vacancy_curve=[
        {"cohort_id": "a1", "start_period": "2026-09", "end_period": "2027-08", "vacancy_rate": 0.09},
        {"cohort_id": "a1", "start_period": "2027-09", "end_period": "2028-08", "vacancy_rate": 0.06},
        {"cohort_id": "a1", "start_period": "2028-09", "end_period": "2031-08", "vacancy_rate": 0.05},
    ])
    original = deepcopy(canonical["physical_vacancy_curve"])
    summary = _revenue_policy(canonical)

    applied = summary["physical_vacancy_rate_applied"]
    overrides = [o for o in summary["policy_overrides"] if o["input"] == "physical_vacancy_curve"]
    assert [(o["original"], o["applied"]) for o in overrides] == [
        (seg["vacancy_rate"], applied) for seg in original if seg["vacancy_rate"] != applied
    ]
    assert overrides and all(o["start_period"] for o in overrides)
