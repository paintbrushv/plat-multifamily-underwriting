"""Regression: backsolve house policy must not misread or silently override analyst inputs.

Synthetic 447-unit, 1984-vintage deal (house R&M floor $900/unit, insurance
floor $600/unit). Each case used to produce a wrong price with no error:

1. a concession_schedule with only free_months rows crashed (max of empty);
2. per_unit opex lines were read as annual dollars;
3. a per-unit "Repairs & Maintenance + Turnover" line was bucketed as
   make-ready, so the R&M floor was stacked on top of it;
4. the vacancy anchor ignored "Vacant ..." status variants and a negative
   T12 vacancy loss;
5. a time-varying vacancy curve was flattened to one property-wide rate with
   no disclosure; the vacancy anchor now floors each segment and keeps the
   analyst's curve shape;
6. loss to lease, collection loss, concessions and reserves were rewritten
   before the floors (re-derived, flattened, collapsed, forced to the vintage
   reserve); the floors now only raise, per segment (UPL-62).
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

    # 1.5 free months on a 12-month lease = 12.5% of rent, above the floor: the
    # analyst's row is kept with its type (UPL-62) and nothing is overridden.
    assert summary["year1_concessions_rate_applied"] == pytest.approx(0.125)
    assert canonical["concession_schedule"] == [
        {"concession_id": "lease_up", "start_month": "2026-09", "end_month": "2027-08",
         "concession_type": "free_months", "amount": 1.5, "applies_to_cohort": "ALL"},
    ]
    assert not any(o["input"] == "concession_schedule" for o in summary["policy_overrides"])


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


def _time_varying_curve(*rates):
    periods = [("2026-09", "2027-08"), ("2027-09", "2028-08"), ("2028-09", "2031-08")]
    return [
        {"cohort_id": "a1", "start_period": start, "end_period": end, "vacancy_rate": rate}
        for (start, end), rate in zip(periods, rates)
    ]


def _vacancy_overrides(summary):
    return [o for o in summary["policy_overrides"] if o["input"] == "physical_vacancy_curve"]


def test_overridden_time_varying_vacancy_curve_is_disclosed():
    # T12 vacancy of 6.25% outranks the 1984 vintage floor, so the anchor
    # sits between the analyst's 9% / 6% / 5% segments.
    canonical = _canonical(physical_vacancy_curve=_time_varying_curve(0.09, 0.06, 0.05))
    summary = _revenue_policy(
        canonical,
        trailing_actuals={"gross_potential_rent": 6_000_000.0, "vacancy_loss": -375_000.0},
    )

    anchor = summary["physical_vacancy_rate_applied"]
    assert anchor == pytest.approx(0.0625)
    expected = _time_varying_curve(0.09, anchor, anchor)
    assert canonical["physical_vacancy_curve"] == expected
    assert summary["physical_vacancy_curve_applied"] == expected
    # Only the raised segments are listed, with their own periods.
    assert [
        (o["start_period"], o["end_period"], o["original"], o["applied"])
        for o in _vacancy_overrides(summary)
    ] == [
        ("2027-09", "2028-08", 0.06, anchor),
        ("2028-09", "2031-08", 0.05, anchor),
    ]


def test_vacancy_segment_above_the_anchor_is_never_lowered():
    canonical = _canonical(physical_vacancy_curve=_time_varying_curve(0.15, 0.12, 0.10))
    original = deepcopy(canonical["physical_vacancy_curve"])
    summary = _revenue_policy(
        canonical,
        trailing_actuals={"gross_potential_rent": 6_000_000.0, "vacancy_loss": -375_000.0},
    )

    assert summary["physical_vacancy_rate_applied"] == pytest.approx(0.0625)
    assert canonical["physical_vacancy_curve"] == original
    assert summary["physical_vacancy_curve_applied"] == original
    assert _vacancy_overrides(summary) == []


def test_house_box_rebase_keeps_vacancy_segments_and_discloses_analyst_original():
    canonical = _canonical(physical_vacancy_curve=_time_varying_curve(0.09, 0.06, 0.05))
    canonical["metadata"]["property_summary"]["house_box_score"] = {
        "total_units": UNITS,
        "status_counts": {"Occupied": 417, "Vacant": 30},
        "floorplans": [
            {"code": "a1", "avg_sqft": 750.0, "avg_in_place_rent": 1200.0,
             "avg_market_rent": 1200.0, "units": UNITS},
        ],
    }
    prepared, summary = _prepare(canonical)

    anchor = summary["physical_vacancy_rate_applied"]
    assert anchor == pytest.approx(30 / UNITS)
    assert summary["house_box_score_rebase"]["observed_unavailable_rate"] == pytest.approx(anchor)
    expected = _time_varying_curve(0.09, anchor, anchor)
    assert prepared["physical_vacancy_curve"] == expected
    assert summary["physical_vacancy_curve_applied"] == expected
    # The rebase must not pre-floor the curve, or the override would record
    # the rent-roll rate as the "original" and hide the analyst's input.
    assert [(o["original"], o["applied"]) for o in _vacancy_overrides(summary)] == [
        (0.06, anchor),
        (0.05, anchor),
    ]


def _segments(field, *rates, key="cohort_id", value="a1"):
    periods = [("2026-09", "2027-08"), ("2027-09", "2031-08")]
    return [{key: value, "start_period": start, "end_period": end, field: rate}
            for (start, end), rate in zip(periods, rates)]


def test_pricing_floors_only_raise_and_keep_the_analyst_curves():
    """UPL-62 decision (a): house floors only raise, per segment, as
    max(analyst value, floor); they never lower or reshape the analyst's input.

    The legacy pricing re-derived loss to lease from market vs in-place (4% here),
    flattened collection loss to its worst rate, collapsed concessions into one
    year-1 row and forced reserves down to the vintage reserve."""
    canonical = _canonical(
        loss_to_lease=_segments("ltl_percent", 0.08, 0.005),
        collection_loss_curve=_segments("loss_rate", 0.03, 0.001, key="applies_to", value="ALL"),
        concession_schedule=[
            {"concession_id": "y1", "start_month": "2026-09", "end_month": "2027-08",
             "concession_type": "pct_rent", "amount": 0.004, "applies_to_cohort": "ALL"},
            {"concession_id": "later", "start_month": "2027-09", "end_month": "2031-08",
             "concession_type": "pct_rent", "amount": 0.002, "applies_to_cohort": "ALL"},
        ],
        replacement_reserves=[
            {"start_period": "2026-09", "end_period": "2031-08", "annual_amount": 1500.0 * UNITS},
        ],
    )
    prepared, summary = _prepare(canonical)

    # Higher analyst values are kept; only the segments below a floor are raised.
    assert prepared["loss_to_lease"] == _segments("ltl_percent", 0.08, 0.01)
    assert prepared["collection_loss_curve"] == _segments("loss_rate", 0.03, 0.0075, key="applies_to", value="ALL")
    assert prepared["replacement_reserves"] == canonical["replacement_reserves"]
    assert prepared["concession_schedule"] == [
        dict(canonical["concession_schedule"][0], amount=0.0075),
        canonical["concession_schedule"][1],  # later rows are kept
    ]
    # Every raise is disclosed with its original and applied value, and recorded as enforced.
    raised = [(o["assumption"], o["original"], o["applied"]) for o in summary["policy_overrides"]
              if o.get("guardrail") == "house_floor"]
    assert sorted(raised) == [("collection_loss", 0.001, 0.0075), ("concessions", 0.004, 0.0075),
                              ("loss_to_lease", 0.005, 0.01)]
    findings = summary["guardrails"]["findings"]
    assert sorted((f["key"], f["action"], f["floor_source"]) for f in findings) == [
        ("collection_loss", "enforced", "house:vintage_1980s"),
        ("concessions", "enforced", "house:vintage_1980s"),
        ("loss_to_lease", "enforced", "house:vintage_1980s"),
    ]
    assert not [o for o in summary["policy_overrides"] if "normalization" in o]

    # Pricing the priced canonical again raises nothing and changes nothing.
    again, again_summary = _prepare(deepcopy(prepared))
    for field in ("loss_to_lease", "collection_loss_curve", "concession_schedule", "replacement_reserves"):
        assert again[field] == prepared[field], field
    assert again_summary["guardrails"]["findings"] == []
