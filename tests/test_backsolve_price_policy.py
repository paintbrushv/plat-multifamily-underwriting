from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import sys

import runs.backsolve_price_for_target_coc as backsolve
import runs.backsolve_price_for_target_coc as backsolve_module

import pytest

from engine.property_tax import PropertyTaxPolicyError

from runs.backsolve_price_for_target_coc import (
    _apply_house_revenue_policy,
    _apply_revenue_quality_bridge,
    _build_price_case,
    _evaluate_case,
    _load_bridge_if_available,
    _prepare_house_assumptions,
    _preview_projected_noi,
    _vacancy_anchor_summary,
)


def test_prepare_house_assumptions_rebases_to_house_box_score_and_status_mix():
    canonical = {
        "metadata": {
            "year_built": 1984,
            "property_summary": {
                "house_box_score": {
                    "derivation_basis": "market-study-agent standardized rent roll",
                    "total_units": 238,
                    "status_counts": {"Occupied": 218, "Vacant": 19, "Non-Revenue": 1},
                    "floorplans": [
                        {"code": "a1", "avg_sqft": 700.0, "avg_in_place_rent": 1100.0, "avg_market_rent": 1200.0, "units": 100},
                        {"code": "b1", "avg_sqft": 900.0, "avg_in_place_rent": 1400.0, "avg_market_rent": 1500.0, "units": 138},
                    ],
                }
            },
        },
        "time_grid": {
            "analysis_start_date": "2026-06-01",
            "analysis_end_date": "2031-05-01",
        },
        "unit_cohorts": [
            {
                "cohort_id": "a1",
                "unit_type": "A1",
                "unit_count": 102,
                "initial_inplace_rent": 1190.0,
                "target_monthly_rent": 1210.0,
                "sqft": 690.0,
            },
            {
                "cohort_id": "b1",
                "unit_type": "B1",
                "unit_count": 140,
                "initial_inplace_rent": 1490.0,
                "target_monthly_rent": 1510.0,
                "sqft": 890.0,
            },
        ],
        "market_rent_curve": [
            {"cohort_id": "a1", "market_rent": 1210.0},
            {"cohort_id": "b1", "market_rent": 1510.0},
        ],
        "physical_vacancy_curve": [
            {"cohort_id": "a1", "start_period": "2026-06", "end_period": "2031-05", "vacancy_rate": 0.0},
            {"cohort_id": "b1", "start_period": "2026-06", "end_period": "2031-05", "vacancy_rate": 0.0},
        ],
        "collection_loss_curve": [],
        "loss_to_lease": [],
        "opex_table": [
            {"category_name": "Insurance", "base_value": 150000.0},
            {"category_name": "Repairs & Maintenance", "base_value": 100000.0},
        ],
        "exit_assumptions": {"exit_cap_rate": 0.06},
    }

    prepared, policy_summary = _prepare_house_assumptions(
        canonical,
        year_built=None,
        strategy="cashflow",
        target_coc=Decimal("0.07"),
        benchmark_treasury=Decimal("0.0391"),
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
        broker_snapshot=None,
    )

    assert sum(row["unit_count"] for row in prepared["unit_cohorts"]) == 238
    assert prepared["unit_cohorts"][0]["unit_count"] == 100
    assert prepared["unit_cohorts"][1]["unit_count"] == 138
    assert prepared["unit_cohorts"][0]["initial_inplace_rent"] == 1100.0
    assert prepared["unit_cohorts"][0]["target_monthly_rent"] == 1100.0
    assert prepared["physical_vacancy_curve"][0]["vacancy_rate"] == 20 / 238
    assert prepared["market_rent_curve"][0]["market_rent"] == 1100.0
    assert prepared["market_rent_curve"][0]["start_period"] == "2026-06"
    assert prepared["market_rent_curve"][0]["end_period"] == "2031-05"
    assert prepared["loss_to_lease"][0]["start_period"] == "2026-06"
    assert prepared["loss_to_lease"][0]["end_period"] == "2031-05"
    top_up = next(
        row
        for row in prepared["opex_table"]
        if row["category_name"] == "Repairs & Maintenance Vintage Floor Top-Up"
    )
    assert top_up["base_value"] == 114200.0
    assert policy_summary["house_box_score_rebase"]["applied"] is True
    assert policy_summary["house_box_score_rebase"]["rebased_unit_count"] == 238
    assert policy_summary["house_box_score_rebase"]["market_rent_capped_to_in_place_default"] is True
    assert policy_summary["year_built"] == 1984
    assert policy_summary["observed_repairs_maintenance_annual"] == 100000.0
    assert policy_summary["repairs_maintenance_floor_per_unit"] == 900.0
    assert policy_summary["repairs_maintenance_floor_annual"] == 214200.0
    assert policy_summary["repairs_maintenance_top_up_annual"] == 114200.0


def test_preview_projected_noi_supplies_required_pricing_provenance(monkeypatch):
    captured = {}

    def fake_run_underwriting(inputs):
        captured["pricing_provenance"] = inputs["pricing_provenance"]
        return {"cashflow": {"by_year": [{"net_operating_income": 1_000_000}]}}

    monkeypatch.setattr(
        "runs.backsolve_price_for_target_coc.run_underwriting",
        fake_run_underwriting,
    )
    canonical = {
        "time_grid": {
            "analysis_start_date": "2026-06-01",
            "analysis_end_date": "2031-05-01",
        },
        "purchase_assumptions": {"purchase_price": 12_000_000},
    }

    assert _preview_projected_noi(canonical) == Decimal("1000000")
    provenance = captured["pricing_provenance"]
    assert provenance["strike_price"] == 20_000_000.0
    assert provenance["strike_price_basis"] == "other"
    assert provenance["strike_price_derivation"]
    assert provenance["as_of_date"]


def test_build_price_case_applies_price_dependent_property_tax(
    minimal_deal_inputs,
):
    case = _build_price_case(
        minimal_deal_inputs,
        price=Decimal("10000000"),
        target_coc=Decimal("0.07"),
        year_built=1984,
        benchmark_treasury=Decimal("0.04"),
        agency_spread=Decimal("0.015"),
        purchase_closing_cost_pct=Decimal("0.015"),
        partnership_closing_costs=Decimal("50000"),
        acquisition_fee_pct=Decimal("0.01"),
        loan_closing_costs=Decimal("0"),
        projected_noi=Decimal("1000000"),
    )

    tax_row = next(
        row
        for row in case["opex_table"]
        if row["category_name"] == "Real Estate Taxes"
    )
    calculation = case["metadata"]["property_summary"][
        "property_tax_calculation"
    ]
    assert tax_row["calculation_type"] == "fixed_annual"
    assert tax_row["base_value"] == 250000.0
    assert calculation["annual_ad_valorem_tax"] == 250000.0
    assert calculation["assessment_ratio"] == 1.0
    assert calculation["millage_rate_mills"] == 25.0


def test_build_price_case_recalculates_tax_and_dependent_economics_for_each_price(
    minimal_deal_inputs,
):
    tax_row = next(
        row
        for row in minimal_deal_inputs["opex_table"]
        if row["category_name"] == "Real Estate Taxes"
    )
    tax_row["growth_rate"] = 0.0

    def build(price: str):
        return _build_price_case(
            minimal_deal_inputs,
            price=Decimal(price),
            target_coc=Decimal("0.07"),
            year_built=1984,
            benchmark_treasury=Decimal("0.04"),
            agency_spread=Decimal("0.015"),
            purchase_closing_cost_pct=Decimal("0.015"),
            partnership_closing_costs=Decimal("50000"),
            acquisition_fee_pct=Decimal("0.01"),
            loan_closing_costs=Decimal("0"),
            projected_noi=Decimal("1000000"),
        )

    low_case = build("10000000")
    high_case = build("12000000")
    low_results, low_coc = _evaluate_case(low_case)
    high_results, high_coc = _evaluate_case(high_case)

    low_tax = low_case["metadata"]["property_summary"]["property_tax_calculation"]
    high_tax = high_case["metadata"]["property_summary"]["property_tax_calculation"]
    assert low_tax["annual_ad_valorem_tax"] == 250000.0
    assert high_tax["annual_ad_valorem_tax"] == 300000.0

    low_noi = Decimal(str(low_results["cashflow"]["by_year"][1]["net_operating_income"]))
    high_noi = Decimal(
        str(high_results["cashflow"]["by_year"][1]["net_operating_income"])
    )
    assert low_noi - high_noi == pytest.approx(Decimal("50000.00"), abs=Decimal("0.05"))
    assert (
        low_case["pricing_provenance"]["strike_price_derivation"]
        != high_case["pricing_provenance"]["strike_price_derivation"]
    )
    assert f"projected NOI used {float(low_noi):,.0f}" in low_case[
        "pricing_provenance"
    ]["strike_price_derivation"]
    assert f"projected NOI used {float(high_noi):,.0f}" in high_case[
        "pricing_provenance"
    ]["strike_price_derivation"]
    assert low_case["debt_terms"]["commitment"] != high_case["debt_terms"]["commitment"]
    assert (
        low_case["purchase_assumptions"]["total_equity_basis"]
        != high_case["purchase_assumptions"]["total_equity_basis"]
    )
    assert low_coc != high_coc


def test_build_price_case_missing_millage_raises_named_error_even_with_t12_tax(
    minimal_deal_inputs,
):
    minimal_deal_inputs["metadata"]["property_summary"].pop("property_tax_policy")
    stale_tax_row = next(
        row
        for row in minimal_deal_inputs["opex_table"]
        if row["category_name"] == "Real Estate Taxes"
    )
    stale_tax_row["base_value"] = 123456

    with pytest.raises(PropertyTaxPolicyError) as exc:
        _build_price_case(
            minimal_deal_inputs,
            price=Decimal("10000000"),
            target_coc=Decimal("0.07"),
            year_built=1984,
            benchmark_treasury=Decimal("0.04"),
            agency_spread=Decimal("0.015"),
            purchase_closing_cost_pct=Decimal("0.015"),
            partnership_closing_costs=Decimal("50000"),
            acquisition_fee_pct=Decimal("0.01"),
            loan_closing_costs=Decimal("0"),
            projected_noi=Decimal("1000000"),
        )

    assert exc.value.code == "missing_property_tax_millage"


def test_main_missing_millage_precedes_ancillary_bridge_economics(
    monkeypatch,
    tmp_path,
):
    canonical = {
        "metadata": {"property_summary": {}},
        "opex_table": [
            {
                "category_name": "Real Estate Taxes",
                "calculation_type": "fixed_annual",
                "base_value": 123456,
            }
        ],
        "revenue_programs": [
            {
                "program_id": "other_income",
                "program_type": "other_income",
                "annual_amount": 1_000_000,
            }
        ],
    }
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "backsolve_price_for_target_coc.py",
            "--canonical-json",
            "canonical.json",
            "--output-dir",
            str(tmp_path),
            "--benchmark-5yr-treasury",
            "0.04",
            "--exit-cap-rate",
            "0.06",
        ],
    )
    monkeypatch.setattr(backsolve_module, "_load_json", lambda _: canonical)

    def reject_missing_ancillary_bridge(*args, **kwargs):
        raise RuntimeError("revenue_quality_bridge_required")

    monkeypatch.setattr(
        backsolve_module,
        "_prepare_house_assumptions",
        reject_missing_ancillary_bridge,
    )

    with pytest.raises(PropertyTaxPolicyError) as exc:
        backsolve_module.main()

    assert exc.value.code == "missing_property_tax_millage"


def test_candidate_price_representation_keeps_canonical_engine_and_summary_tax_identical(
    minimal_deal_inputs,
):
    price = Decimal("10000000.2")
    case = _build_price_case(
        minimal_deal_inputs,
        price=price,
        target_coc=Decimal("0.07"),
        year_built=1984,
        benchmark_treasury=Decimal("0.04"),
        agency_spread=Decimal("0.015"),
        purchase_closing_cost_pct=Decimal("0.015"),
        partnership_closing_costs=Decimal("50000"),
        acquisition_fee_pct=Decimal("0.01"),
        loan_closing_costs=Decimal("0"),
        projected_noi=Decimal("1000000"),
    )
    results, _ = _evaluate_case(case)

    persisted_price = Decimal(
        str(case["purchase_assumptions"]["purchase_price"])
    )
    canonical_tax = case["metadata"]["property_summary"][
        "property_tax_calculation"
    ]
    engine_tax = results["property_tax_calculation"]
    summary_tax = case["metadata"]["property_summary"][
        "property_tax_calculation"
    ]

    assert Decimal(str(canonical_tax["purchase_price_basis"])) == persisted_price
    assert Decimal(str(engine_tax["purchase_price_basis"])) == persisted_price
    assert canonical_tax == engine_tax == summary_tax
    assert canonical_tax["annual_ad_valorem_tax"] == 250000.01


def test_candidate_price_rejects_unrepresentable_high_precision_without_float_rounding(
    minimal_deal_inputs,
):
    with pytest.raises(PropertyTaxPolicyError) as exc:
        _build_price_case(
            minimal_deal_inputs,
            price=Decimal("10000000.1999999999"),
            target_coc=Decimal("0.07"),
            year_built=1984,
            benchmark_treasury=Decimal("0.04"),
            agency_spread=Decimal("0.015"),
            purchase_closing_cost_pct=Decimal("0.015"),
            partnership_closing_costs=Decimal("50000"),
            acquisition_fee_pct=Decimal("0.01"),
            loan_closing_costs=Decimal("0"),
            projected_noi=Decimal("1000000"),
        )

    assert exc.value.code == "invalid_property_tax_millage"
    assert "finite JSON number" in str(exc.value)


def test_house_revenue_policy_logs_worst_cohort_vacancy_without_applying_property_wide():
    canonical = {
        "metadata": {
            "property_summary": {
                "house_box_score": {
                    "total_units": 20,
                    "status_counts": {"Occupied": 18, "Vacant": 1, "Notice": 1},
                }
            }
        },
        "time_grid": {
            "analysis_start_date": "2026-06-01",
            "analysis_end_date": "2031-05-01",
        },
        "unit_cohorts": [
            {"cohort_id": "a1", "unit_count": 10, "initial_inplace_rent": 1000.0},
            {"cohort_id": "b1", "unit_count": 10, "initial_inplace_rent": 1000.0},
        ],
        "market_rent_curve": [
            {"cohort_id": "a1", "market_rent": 1000.0},
            {"cohort_id": "b1", "market_rent": 1000.0},
        ],
        "loss_to_lease": [
            {"cohort_id": "a1", "ltl_percent": 0.0},
            {"cohort_id": "b1", "ltl_percent": 0.0},
        ],
        "physical_vacancy_curve": [
            {"cohort_id": "a1", "start_period": "2026-06", "end_period": "2031-05", "vacancy_rate": 0.05},
            {"cohort_id": "b1", "start_period": "2026-06", "end_period": "2031-05", "vacancy_rate": 0.30},
        ],
        "collection_loss_curve": [],
    }

    summary = _apply_house_revenue_policy(
        canonical,
        year_built=1984,
        strategy="cashflow",
        trailing_actuals={"gross_potential_rent": 1000.0, "vacancy_loss": 90.0},
    )

    assert {row["vacancy_rate"] for row in canonical["physical_vacancy_curve"]} == {0.09}
    vacancy_summary = summary["physical_vacancy_anchor_summary"]
    assert vacancy_summary["base_case_selected_anchor"] == ["t12_vacancy_gpr"]
    assert vacancy_summary["conservative_downside_selected_rate"] == 0.1
    assert vacancy_summary["stress_flag_worst_cohort_vacancy"] == 0.3


def test_vacancy_anchor_counts_statuses_case_and_separator_insensitively():
    canonical = {
        "metadata": {
            "property_summary": {
                "house_box_score": {
                    "total_units": 20,
                    "status_counts": {
                        "vacant": 1,
                        "VACANT": 2,
                        "Non Revenue": 3,
                        "non-revenue": 4,
                        "notice": 5,
                        "NOTICE": 1,
                    },
                }
            }
        },
        "unit_cohorts": [{"cohort_id": "all", "unit_count": 20}],
        "physical_vacancy_curve": [],
    }

    base_rate, summary = _vacancy_anchor_summary(
        canonical,
        default_vacancy=Decimal("0.05"),
        broker_vacancy=None,
        trailing_actuals=None,
        broker_snapshot=None,
    )

    assert base_rate == Decimal("0.5")
    assert summary["base_case_candidates"]["rent_roll_vacant"] == 0.5
    assert summary["base_case_selected_anchor"] == ["rent_roll_vacant"]
    assert summary["conservative_downside_candidates"]["rent_roll_vacant_plus_notice"] == 0.8
    assert summary["conservative_downside_selected_anchor"] == ["rent_roll_vacant_plus_notice"]

def test_prepare_house_assumptions_preserves_multisegment_market_rent_curve():
    canonical = {
        "metadata": {
            "year_built": 1984,
            "property_summary": {
                "house_box_score": {
                    "derivation_basis": "market-study-agent standardized rent roll",
                    "total_units": 10,
                    "status_counts": {"Occupied": 10},
                    "floorplans": [
                        {"code": "a1", "avg_sqft": 700.0, "avg_in_place_rent": 1100.0, "avg_market_rent": 1100.0, "units": 10},
                    ],
                }
            },
        },
        "time_grid": {
            "analysis_start_date": "2026-06-01",
            "analysis_end_date": "2028-05-01",
        },
        "unit_cohorts": [
            {"cohort_id": "a1", "unit_type": "A1", "unit_count": 10, "initial_inplace_rent": 1100.0, "target_monthly_rent": 1100.0, "sqft": 700.0},
        ],
        "market_rent_curve": [
            {"cohort_id": "a1", "start_period": "2026-06", "end_period": "2027-05", "market_rent": 1100.0},
            {"cohort_id": "a1", "start_period": "2027-06", "end_period": "2028-05", "market_rent": 1133.0},
        ],
        "physical_vacancy_curve": [
            {"cohort_id": "a1", "start_period": "2026-06", "end_period": "2028-05", "vacancy_rate": 0.0},
        ],
        "collection_loss_curve": [],
        "loss_to_lease": [
            {"cohort_id": "a1", "start_period": "2026-06", "end_period": "2027-05", "ltl_percent": 0.0},
            {"cohort_id": "a1", "start_period": "2027-06", "end_period": "2028-05", "ltl_percent": 0.0},
        ],
        "opex_table": [
            {"category_name": "Insurance", "base_value": 9000.0},
            {"category_name": "Repairs & Maintenance", "base_value": 9000.0},
        ],
        "exit_assumptions": {"exit_cap_rate": 0.06},
    }

    prepared, _ = _prepare_house_assumptions(
        canonical,
        year_built=None,
        strategy="cashflow",
        target_coc=Decimal("0.07"),
        benchmark_treasury=Decimal("0.0391"),
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
        broker_snapshot=None,
    )

    assert prepared["market_rent_curve"] == [
        {"cohort_id": "a1", "start_period": "2026-06", "end_period": "2027-05", "market_rent": 1100.0},
        {"cohort_id": "a1", "start_period": "2027-06", "end_period": "2028-05", "market_rent": 1133.0},
    ]
    assert prepared["loss_to_lease"] == [
        {"cohort_id": "a1", "start_period": "2026-06", "end_period": "2027-05", "ltl_percent": 0.0},
        {"cohort_id": "a1", "start_period": "2027-06", "end_period": "2028-05", "ltl_percent": 0.0},
    ]


def test_load_bridge_if_available_falls_back_to_run_artifact(tmp_path):
    run_root = tmp_path / "runs" / "wythe_apartment_homes" / "outputs" / "run_001"
    output_dir = run_root / "judgment" / "pricing_policy"
    bridge_dir = run_root / "reconciliation_house_case"
    bridge_dir.mkdir(parents=True)
    bridge_path = bridge_dir / "revenue_quality_bridge.json"
    bridge_path.write_text(
        json.dumps({"status": "applied", "line_items": [{"line_item": "other_income", "house_credit": 200000.0}]}),
    )

    loaded = _load_bridge_if_available(None, output_dir)
    assert loaded is not None
    assert loaded["status"] == "applied"


def test_load_bridge_if_available_skips_non_applied(tmp_path):
    run_root = tmp_path / "runs" / "wythe_apartment_homes" / "outputs" / "run_001"
    output_dir = run_root / "judgment" / "pricing_policy"
    bridge_dir = run_root / "reconciliation_house_case"
    bridge_dir.mkdir(parents=True)
    bridge_path = bridge_dir / "revenue_quality_bridge.json"
    bridge_path.write_text(json.dumps({"status": "needs_review_not_applied", "line_items": []}))

    loaded = _load_bridge_if_available(None, output_dir)
    assert loaded is None


def test_prepare_house_assumptions_preserves_analyst_override_rent_caps():
    canonical = {
        "metadata": {
            "year_built": 1984,
            "property_summary": {
                "house_box_score": {
                    "derivation_basis": "market-study-agent standardized rent roll",
                    "total_units": 10,
                    "status_counts": {"Occupied": 10},
                    "floorplans": [
                        {
                            "code": "b3",
                            "avg_sqft": 1011.0,
                            "avg_in_place_rent": 1627.2,
                            "avg_market_rent": 1579.23,
                            "units": 10,
                        }
                    ],
                }
            },
        },
        "time_grid": {
            "analysis_start_date": "2026-06-01",
            "analysis_end_date": "2031-05-01",
        },
        "unit_cohorts": [
            {
                "cohort_id": "b3",
                "unit_type": "B3",
                "unit_count": 10,
                "initial_inplace_rent": 1627.2,
                "target_monthly_rent": 1465.0,
                "target_monthly_rent_source": "analyst_override",
                "sqft": 1011.0,
            }
        ],
        "market_rent_curve": [
            {
                "cohort_id": "b3",
                "start_period": "2026-06",
                "end_period": "2031-05",
                "market_rent": 1465.0,
            }
        ],
        "physical_vacancy_curve": [
            {
                "cohort_id": "b3",
                "start_period": "2026-06",
                "end_period": "2031-05",
                "vacancy_rate": 0.0,
            }
        ],
        "collection_loss_curve": [],
        "loss_to_lease": [],
        "opex_table": [
            {"category_name": "Insurance", "base_value": 10000.0},
            {"category_name": "Repairs & Maintenance", "base_value": 9000.0},
        ],
        "exit_assumptions": {"exit_cap_rate": 0.06},
    }

    prepared, policy_summary = _prepare_house_assumptions(
        canonical,
        year_built=None,
        strategy="cashflow",
        target_coc=Decimal("0.07"),
        benchmark_treasury=Decimal("0.0391"),
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
        broker_snapshot=None,
    )

    cohort = prepared["unit_cohorts"][0]
    market_curve = prepared["market_rent_curve"][0]
    assert cohort["initial_inplace_rent"] == 1627.2
    assert cohort["target_monthly_rent"] == 1465.0
    assert cohort["target_monthly_rent_source"] == "analyst_override"
    assert market_curve["market_rent"] == 1465.0
    assert policy_summary["house_box_score_rebase"]["analyst_override_rent_cap_units"] == 10


def test_prepare_house_assumptions_does_not_impose_ltl_on_capped_market_rents():
    canonical = {
        "metadata": {
            "year_built": 1984,
            "property_summary": {
                "house_box_score": {
                    "derivation_basis": "market-study-agent standardized rent roll",
                    "total_units": 10,
                    "status_counts": {"Occupied": 10},
                    "floorplans": [
                        {
                            "code": "b3",
                            "avg_sqft": 1011.0,
                            "avg_in_place_rent": 1627.2,
                            "avg_market_rent": 1579.23,
                            "units": 10,
                        }
                    ],
                }
            },
        },
        "time_grid": {
            "analysis_start_date": "2026-06-01",
            "analysis_end_date": "2031-05-01",
        },
        "unit_cohorts": [
            {
                "cohort_id": "b3",
                "unit_type": "B3",
                "unit_count": 10,
                "initial_inplace_rent": 1627.2,
                "target_monthly_rent": 1465.0,
                "target_monthly_rent_source": "analyst_override",
                "sqft": 1011.0,
            }
        ],
        "market_rent_curve": [
            {
                "cohort_id": "b3",
                "start_period": "2026-06",
                "end_period": "2031-05",
                "market_rent": 1465.0,
            }
        ],
        "loss_to_lease": [
            {
                "cohort_id": "b3",
                "start_period": "2026-06",
                "end_period": "2031-05",
                "ltl_percent": 0.0,
            }
        ],
        "physical_vacancy_curve": [
            {
                "cohort_id": "b3",
                "start_period": "2026-06",
                "end_period": "2031-05",
                "vacancy_rate": 0.0,
            }
        ],
        "collection_loss_curve": [
            {
                "applies_to": "ALL",
                "start_period": "2026-06",
                "end_period": "2031-05",
                "loss_rate": 0.011,
            }
        ],
        "concession_schedule": [
            {
                "concession_id": "house_year1_concessions",
                "start_month": "2026-06",
                "end_month": "2027-05",
                "concession_type": "pct_rent",
                "amount": 0.011,
                "applies_to_cohort": "ALL",
            }
        ],
        "opex_table": [
            {"category_name": "Insurance", "base_value": 10000.0},
            {"category_name": "Repairs & Maintenance", "base_value": 9000.0},
        ],
        "exit_assumptions": {"exit_cap_rate": 0.06},
    }

    prepared, policy_summary = _prepare_house_assumptions(
        canonical,
        year_built=None,
        strategy="cashflow",
        target_coc=Decimal("0.07"),
        benchmark_treasury=Decimal("0.0391"),
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
        broker_snapshot=None,
    )

    assert prepared["loss_to_lease"][0]["ltl_percent"] == 0.0
    assert prepared["collection_loss_curve"][0]["loss_rate"] == 0.011
    assert prepared["concession_schedule"][0]["amount"] == 0.011
    assert policy_summary["loss_to_lease_rate_applied"] == 0.0
    assert policy_summary["collection_loss_rate_applied"] == 0.011
    assert policy_summary["year1_concessions_rate_applied"] == 0.011


def test_prepare_house_assumptions_requires_bridge_for_material_ancillary_income():
    canonical = {
        "metadata": {
            "year_built": 2010,
            "property_summary": {
                "material_ancillary_income": {
                    "requires_revenue_quality_bridge": True,
                    "reason": "broker other income exceeds materiality threshold",
                }
            },
        },
        "time_grid": {
            "analysis_start_date": "2026-06-01",
            "analysis_end_date": "2031-05-01",
        },
        "unit_cohorts": [
            {
                "cohort_id": "a1",
                "unit_type": "A1",
                "unit_count": 10,
                "initial_inplace_rent": 1000.0,
                "target_monthly_rent": 1000.0,
            }
        ],
        "market_rent_curve": [{"cohort_id": "a1", "market_rent": 1000.0}],
        "physical_vacancy_curve": [
            {
                "cohort_id": "a1",
                "start_period": "2026-06",
                "end_period": "2031-05",
                "vacancy_rate": 0.0,
            }
        ],
        "collection_loss_curve": [],
        "loss_to_lease": [],
        "opex_table": [{"category_name": "Insurance", "base_value": 10000.0}],
        "exit_assumptions": {"exit_cap_rate": 0.06},
    }

    try:
        _prepare_house_assumptions(
            canonical,
            year_built=None,
            strategy="cashflow",
            target_coc=Decimal("0.07"),
            benchmark_treasury=Decimal("0.0391"),
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
            broker_snapshot=None,
        )
    except RuntimeError as exc:
        assert "Revenue-quality bridge required before pricing" in str(exc)
    else:
        raise AssertionError("Expected material ancillary income to require a bridge")


def test_prepare_house_assumptions_accepts_insurance_override() -> None:
    canonical = {
        "metadata": {"year_built": 1984, "property_summary": {}},
        "time_grid": {
            "analysis_start_date": "2026-06-01",
            "analysis_end_date": "2031-05-01",
        },
        "unit_cohorts": [
            {
                "cohort_id": "a1",
                "unit_type": "A1",
                "unit_count": 10,
                "initial_inplace_rent": 1000.0,
                "target_monthly_rent": 1000.0,
            }
        ],
        "market_rent_curve": [{"cohort_id": "a1", "market_rent": 1000.0}],
        "physical_vacancy_curve": [{"cohort_id": "a1", "start_period": "2026-06", "end_period": "2031-05", "vacancy_rate": 0.0}],
        "collection_loss_curve": [],
        "loss_to_lease": [],
        "opex_table": [{"category_name": "Insurance", "base_value": 25000.0}],
        "exit_assumptions": {"exit_cap_rate": 0.06},
    }

    prepared, policy_summary = _prepare_house_assumptions(
        canonical,
        year_built=1984,
        strategy="cashflow",
        target_coc=Decimal("0.07"),
        benchmark_treasury=Decimal("0.0391"),
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
        insurance_per_unit_override=Decimal("900"),
        broker_snapshot=None,
    )

    insurance_row = next(row for row in prepared["opex_table"] if row["category_name"] == "Insurance")
    assert insurance_row["base_value"] == 9000.0
    assert policy_summary["insurance_per_unit_applied"] == 900.0
    assert policy_summary["insurance_per_unit_override"] == 900.0


def test_prepare_house_assumptions_applies_revenue_quality_bridge() -> None:
    canonical = {
        "metadata": {"year_built": 2025, "property_summary": {}},
        "time_grid": {
            "analysis_start_date": "2026-06-01",
            "analysis_end_date": "2031-05-01",
        },
        "unit_cohorts": [
            {
                "cohort_id": "a1",
                "unit_type": "A1",
                "unit_count": 100,
                "initial_inplace_rent": 1500.0,
                "target_monthly_rent": 1500.0,
            }
        ],
        "market_rent_curve": [
            {"cohort_id": "a1", "start_period": "2026-06", "end_period": "2031-05", "market_rent": 1500.0}
        ],
        "physical_vacancy_curve": [
            {"cohort_id": "a1", "start_period": "2026-06", "end_period": "2031-05", "vacancy_rate": 0.0}
        ],
        "collection_loss_curve": [],
        "loss_to_lease": [
            {"cohort_id": "a1", "start_period": "2026-06", "end_period": "2031-05", "ltl_percent": 0.0}
        ],
        "revenue_programs": [
            {
                "program_id": "rq_bridge_stale",
                "program_name": "Stale Bridge",
                "program_type": "asset-based",
                "pricing_type": "$/asset",
                "price_value": 1.0,
                "eligible_units": "ALL",
                "start_period": "2026-06",
                "end_period": "2031-05",
            }
        ],
        "program_adoption_curve": [
            {
                "program_id": "rq_bridge_stale",
                "start_period": "2026-06",
                "end_period": "2031-05",
                "adoption_rate": 1.0,
            }
        ],
        "utility_recovery_rules": [
            {
                "utility_category": "water",
                "recovery_basis": "recoverable_opex",
                "recovery_rate": 1.0,
                "lag_months": 0,
            }
        ],
        "opex_table": [
            {"category_name": "Insurance", "calculation_type": "fixed_annual", "base_value": 60000.0, "recoverable_flag": False},
            {"category_name": "6210 Cable Internet", "calculation_type": "fixed_annual", "base_value": 100000.0, "recoverable_flag": False},
        ],
        "exit_assumptions": {"exit_cap_rate": 0.06},
    }
    bridge = {
        "deal_slug": "sample_deal_alpha",
        "run_id": "run_001",
        "prepared_on": "2026-05-09",
        "rent_roll_turnover_signal": {
            "as_of": "2026-04-30",
            "move_ins_last_12_pct_occupied": 0.49,
            "new_lease_share_last_12": 0.49,
            "renewal_share_last_12": 0.51,
        },
        "credit_policy_notes": [
            "Application/admin fee haircuts should consider new-lease share and turnover evidence.",
            "Revenue-quality haircuts are distinct from explicit collection loss.",
        ],
        "recommended_house_revenue_view": {
            "paired_expenses": {"internet_expense": 152737.0}
        },
        "lines": [
            {
                "line_item": "commercial_income",
                "bucket_type": "mixed_use_commercial",
                "decision": "haircut",
                "house_credit": 214600.0,
            },
            {
                "line_item": "internet_income",
                "bucket_type": "recurring_other_income_with_paired_expense",
                "decision": "keep",
                "house_credit": 223155.0,
                "paired_expense": 152737.0,
            },
            {
                "line_item": "late_charges",
                "bucket_type": "transactional_other_income",
                "decision": "exclude",
                "house_credit": 0.0,
            },
        ],
    }

    prepared, policy_summary = _prepare_house_assumptions(
        canonical,
        year_built=2025,
        strategy="cashflow",
        target_coc=Decimal("0.07"),
        benchmark_treasury=Decimal("0.0391"),
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
        revenue_quality_bridge=bridge,
    )

    program_ids = {row["program_id"] for row in prepared["revenue_programs"]}
    assert program_ids == {"rq_bridge_commercial_income", "rq_bridge_internet_income"}
    commercial = next(row for row in prepared["revenue_programs"] if row["program_id"] == "rq_bridge_commercial_income")
    assert commercial["price_value"] == 214600.0 / 12
    assert prepared["utility_recovery_rules"][0]["recovery_rate"] == 0.0
    internet_expense = next(row for row in prepared["opex_table"] if row["category_name"] == "6210 Cable Internet")
    assert internet_expense["base_value"] == 152737.0
    assert policy_summary["revenue_quality_bridge"]["applied"] is True
    assert policy_summary["revenue_quality_bridge"]["commercial_income_credit"] == 214600.0
    assert policy_summary["revenue_quality_bridge"]["other_income_credit"] == 223155.0
    assert prepared["metadata"]["property_summary"]["revenue_quality_bridge"]["program_count"] == 2
    bridge_metadata = prepared["metadata"]["property_summary"]["revenue_quality_bridge"]
    assert bridge_metadata["credit_by_bucket_type"]["mixed_use_commercial"] == 214600.0
    assert bridge_metadata["credit_by_decision"]["keep"] == 223155.0
    assert bridge_metadata["rent_roll_turnover_signal"]["new_lease_share_last_12"] == 0.49
    assert bridge_metadata["credit_policy_notes"][1] == "Revenue-quality haircuts are distinct from explicit collection loss."
    late_fee_summary = next(row for row in bridge_metadata["line_summaries"] if row["line_item"] == "late_charges")
    assert late_fee_summary["decision"] == "exclude"
    assert late_fee_summary["annual_house_credit"] == 0.0


def test_revenue_quality_bridge_accepts_canonical_line_items() -> None:
    canonical = {
        "metadata": {"property_summary": {}},
        "time_grid": {
            "analysis_start_date": "2026-06-01",
            "analysis_end_date": "2031-05-01",
        },
        "revenue_programs": [],
        "program_adoption_curve": [],
        "opex_table": [],
    }
    bridge = {
        "deal_slug": "sample_deal_beta",
        "run_id": "run_004",
        "line_items": [
            {
                "line_item": "Amenity / bundled internet fee",
                "bucket_type": "contracted resident program with paired expense",
                "decision": "haircut",
                "house_credit": 152595.3,
            }
        ],
    }

    summary = _apply_revenue_quality_bridge(canonical, bridge)

    assert summary["applied"] is True
    assert canonical["revenue_programs"][0]["program_id"] == "rq_bridge_amenity_bundled_internet_fee"
    assert canonical["program_adoption_curve"][0]["program_id"] == "rq_bridge_amenity_bundled_internet_fee"
    assert canonical["metadata"]["property_summary"]["revenue_quality_bridge"]["program_count"] == 1


def test_revenue_quality_bridge_does_not_overwrite_internet_ads() -> None:
    canonical = {
        "metadata": {"year_built": 2013, "property_summary": {}},
        "time_grid": {
            "analysis_start_date": "2026-06-01",
            "analysis_end_date": "2031-05-01",
        },
        "unit_cohorts": [
            {
                "cohort_id": "a1",
                "unit_type": "A1",
                "unit_count": 100,
                "initial_inplace_rent": 1200.0,
                "target_monthly_rent": 1200.0,
            }
        ],
        "market_rent_curve": [
            {"cohort_id": "a1", "start_period": "2026-06", "end_period": "2031-05", "market_rent": 1200.0}
        ],
        "physical_vacancy_curve": [
            {"cohort_id": "a1", "start_period": "2026-06", "end_period": "2031-05", "vacancy_rate": 0.0}
        ],
        "collection_loss_curve": [],
        "loss_to_lease": [],
        "revenue_programs": [],
        "program_adoption_curve": [],
        "opex_table": [
            {"category_name": "Internet Ads", "calculation_type": "fixed_annual", "base_value": 50000.0, "recoverable_flag": False},
            {"category_name": "Internet - Community", "calculation_type": "fixed_annual", "base_value": 30000.0, "recoverable_flag": False},
            {"category_name": "Insurance", "calculation_type": "fixed_annual", "base_value": 60000.0, "recoverable_flag": False},
        ],
        "exit_assumptions": {"exit_cap_rate": 0.06},
    }
    bridge = {
        "deal_slug": "sample",
        "run_id": "run_001",
        "prepared_on": "2026-05-14",
        "recommended_house_revenue_view": {
            "paired_expenses": {"internet_expense": 125000.0}
        },
        "lines": [
            {
                "line_item": "internet_income",
                "bucket_type": "contracted_paired_expense_program",
                "decision": "keep",
                "house_credit": 200000.0,
            }
        ],
    }

    prepared, policy_summary = _prepare_house_assumptions(
        canonical,
        year_built=2013,
        strategy="cashflow",
        target_coc=Decimal("0.07"),
        benchmark_treasury=Decimal("0.0391"),
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
        revenue_quality_bridge=bridge,
    )

    ads = next(row for row in prepared["opex_table"] if row["category_name"] == "Internet Ads")
    community = next(row for row in prepared["opex_table"] if row["category_name"] == "Internet - Community")
    assert ads["base_value"] == 50000.0
    assert community["base_value"] == 125000.0
    assert policy_summary["revenue_quality_bridge"]["paired_expenses"][0]["category_name"] == "Internet - Community"


def test_prepare_house_assumptions_uses_grouped_comps_to_cap_upside() -> None:
    canonical = {
        "metadata": {
            "year_built": 1984,
            "property_summary": {
                "house_box_score": {
                    "derivation_basis": "market-study-agent standardized rent roll",
                    "total_units": 10,
                    "status_counts": {"Occupied": 10, "Vacant": 0, "Non-Revenue": 0},
                    "floorplans": [
                        {"code": "a1", "avg_sqft": 700.0, "avg_in_place_rent": 1100.0, "avg_market_rent": 1400.0, "units": 10},
                    ],
                }
            },
        },
        "time_grid": {
            "analysis_start_date": "2026-06-01",
            "analysis_end_date": "2031-05-01",
        },
        "unit_cohorts": [
            {
                "cohort_id": "a1",
                "unit_type": "A1",
                "unit_count": 10,
                "initial_inplace_rent": 1200.0,
                "target_monthly_rent": 1450.0,
                "sqft": 700.0,
                "bedrooms": 1,
                "bathrooms": 1.0,
            },
        ],
        "market_rent_curve": [
            {"cohort_id": "a1", "market_rent": 1450.0},
        ],
        "physical_vacancy_curve": [
            {"cohort_id": "a1", "start_period": "2026-06", "end_period": "2031-05", "vacancy_rate": 0.0},
        ],
        "collection_loss_curve": [],
        "loss_to_lease": [],
        "opex_table": [
            {"category_name": "Insurance", "base_value": 8000.0},
            {"category_name": "Repairs & Maintenance", "base_value": 6000.0},
        ],
        "exit_assumptions": {"exit_cap_rate": 0.06},
    }
    grouped_comps = {
        "comps_by_cohort": {
            "1BR_1BA_700sf": [
                {"asking_rent": 1250.0},
                {"asking_rent": 1275.0},
                {"asking_rent": 1300.0},
            ]
        }
    }

    prepared, policy_summary = _prepare_house_assumptions(
        canonical,
        year_built=1984,
        strategy="cashflow",
        target_coc=Decimal("0.07"),
        benchmark_treasury=Decimal("0.0391"),
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
        broker_snapshot=None,
        grouped_comps=grouped_comps,
    )

    assert prepared["unit_cohorts"][0]["initial_inplace_rent"] == 1100.0
    assert prepared["unit_cohorts"][0]["target_monthly_rent"] == 1275.0
    assert prepared["market_rent_curve"][0]["market_rent"] == 1275.0
    assert policy_summary["house_box_score_rebase"]["comp_supported_upside_units"] == 10
    assert policy_summary["house_box_score_rebase"]["unsupported_upside_units"] == 0
    assert policy_summary["house_box_score_rebase"]["cohort_support"][0]["comp_supported_cap_rent"] == 1275.0


def test_prepare_house_assumptions_infers_bedrooms_for_grouped_comp_caps() -> None:
    canonical = {
        "metadata": {
            "year_built": 1984,
            "property_summary": {
                "house_box_score": {
                    "derivation_basis": "market-study-agent standardized rent roll",
                    "total_units": 10,
                    "status_counts": {"Occupied": 10, "Vacant": 0, "Non-Revenue": 0},
                    "floorplans": [
                        {"code": "B2", "avg_sqft": 970.0, "avg_in_place_rent": 1500.0, "avg_market_rent": 1700.0, "units": 10},
                    ],
                }
            },
        },
        "time_grid": {
            "analysis_start_date": "2026-06-01",
            "analysis_end_date": "2031-05-01",
        },
        "unit_cohorts": [
            {
                "cohort_id": "b2",
                "unit_type": "B2",
                "unit_count": 10,
                "initial_inplace_rent": 1500.0,
                "target_monthly_rent": 1700.0,
                "sqft": 970.0,
                "bedrooms": 0,
                "bathrooms": 1.0,
            },
        ],
        "market_rent_curve": [
            {"cohort_id": "b2", "market_rent": 1700.0},
        ],
        "physical_vacancy_curve": [
            {"cohort_id": "b2", "start_period": "2026-06", "end_period": "2031-05", "vacancy_rate": 0.0},
        ],
        "collection_loss_curve": [],
        "loss_to_lease": [],
        "opex_table": [
            {"category_name": "Insurance", "base_value": 8000.0},
            {"category_name": "Repairs & Maintenance", "base_value": 9000.0},
        ],
        "exit_assumptions": {"exit_cap_rate": 0.06},
    }
    grouped_comps = {
        "comps_by_cohort": {
            "2BR_2.0BA_970sf": [
                {"asking_rent": 1600.0},
                {"asking_rent": 1625.0},
                {"asking_rent": 1650.0},
            ]
        }
    }

    prepared, policy_summary = _prepare_house_assumptions(
        canonical,
        year_built=1984,
        strategy="cashflow",
        target_coc=Decimal("0.07"),
        benchmark_treasury=Decimal("0.0391"),
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
        broker_snapshot=None,
        grouped_comps=grouped_comps,
    )

    assert prepared["unit_cohorts"][0]["bedrooms"] == 2
    assert prepared["unit_cohorts"][0]["bathrooms"] == 2.0
    assert prepared["unit_cohorts"][0]["target_monthly_rent"] == 1625.0
    assert policy_summary["house_box_score_rebase"]["cohort_support"][0]["comp_count"] == 3


def test_evaluate_case_uses_exact_coc_for_price_search(monkeypatch) -> None:
    monkeypatch.setattr(
        backsolve,
        "run_underwriting",
        lambda _case: {
            "metrics": {
                "coc": {
                    "cash_on_cash_year_1": 0.07,
                    "cash_on_cash_year_1_exact": 0.06996,
                }
            }
        },
    )

    _, coc = backsolve._evaluate_case({})

    assert coc == Decimal("0.06996")


def test_evaluate_case_uses_persisted_cash_and_equity_for_hurdle(monkeypatch) -> None:
    monkeypatch.setattr(
        backsolve,
        "run_underwriting",
        lambda _case: {
            "metrics": {
                "coc": {
                    "cash_on_cash_year_1": 0.07,
                    "cash_on_cash_year_1_exact": 0.070000000001,
                    "free_cf_year_1": 699999.99,
                    "components": {"equity_basis": 10000000.0},
                }
            }
        },
    )

    _, coc = backsolve._evaluate_case({})

    assert coc == Decimal("699999.99") / Decimal("10000000.0")


def _run_stubbed_backsolve_main(
    monkeypatch,
    tmp_path: Path,
    *,
    max_price: Decimal,
) -> tuple[dict, dict]:
    canonical_path = tmp_path / "canonical.json"
    canonical_path.write_text(
        json.dumps(
            {
                "metadata": {
                    "property_summary": {
                        "property_tax_policy": {
                            "millage_rate_mills": 25.0,
                            "assessment_ratio": 1.0,
                            "source": "analyst",
                            "source_locator": "tests:stubbed_backsolve",
                            "analyst_override": False,
                        }
                    }
                }
            }
        )
    )
    output_dir = tmp_path / "pricing"

    monkeypatch.setattr(backsolve, "_prepare_house_assumptions", lambda canonical, **_kwargs: (canonical, {}))
    monkeypatch.setattr(backsolve, "_preview_projected_noi", lambda _canonical: Decimal("100"))

    def build_case(_canonical, *, price, **_kwargs):
        return {
            "purchase_assumptions": {
                "purchase_price": float(price),
                "total_equity_basis": 100.0,
            },
            "debt_terms": {
                "commitment": 0.0,
                "rate": 0.0,
                "io_months": 0,
                "term_months": 60,
            },
            "metadata": {"property_summary": {}},
        }

    def evaluate_case(case):
        price = Decimal(str(case["purchase_assumptions"]["purchase_price"]))
        coc = Decimal("0.08") - price * Decimal("0.0002")
        results = {
            "cashflow": {"by_year": [{"net_operating_income": 100.0}, {"net_operating_income": 100.0}]},
            "metrics": {
                "coc": {
                    "cash_on_cash_year_1": round(float(coc), 4),
                    "cash_on_cash_year_1_exact": float(coc),
                    "free_cf_year_1": float(coc * Decimal("100")),
                    "components": {"equity_basis": 100.0},
                },
                "irr": {"levered_irr": 0.0},
                "dscr": {"minimum_dscr": 0.0},
                "yields": {"going_in_cap_rate": 0.0, "exit_cap_rate": 0.06},
            },
        }
        return results, coc

    monkeypatch.setattr(backsolve, "_build_price_case", build_case)
    monkeypatch.setattr(backsolve, "_evaluate_case", evaluate_case)
    monkeypatch.setattr(
        backsolve.sys,
        "argv",
        [
            "backsolve_price_for_target_coc.py",
            "--canonical-json",
            str(canonical_path),
            "--output-dir",
            str(output_dir),
            "--benchmark-5yr-treasury",
            "0.04",
            "--exit-cap-rate",
            "0.06",
            "--min-price",
            "20",
            "--max-price",
            str(max_price),
            "--max-iterations",
            "8",
        ],
    )

    backsolve.main()
    return (
        json.loads((output_dir / "backsolve_summary.json").read_text()),
        json.loads((output_dir / "canonical_backsolved_target_coc.json").read_text()),
    )


def test_main_persists_exact_coc_and_highest_feasible_boundary(
    monkeypatch,
    tmp_path: Path,
) -> None:
    summary, canonical = _run_stubbed_backsolve_main(
        monkeypatch,
        tmp_path,
        max_price=Decimal("100"),
    )

    assert summary["cash_on_cash_year_1_exact"] == 0.07
    assert summary["cash_on_cash_year_1_persisted"] == 0.07
    assert summary["maximum_feasible_boundary"]["highest_feasible_price"] == 50.0
    assert summary["maximum_feasible_boundary"]["next_infeasible_price"] > 50.0
    assert summary["maximum_feasible_boundary"]["next_infeasible_coc"] < 0.07
    assert canonical["purchase_assumptions"]["purchase_price"] == 50.0


def test_main_selects_exact_target_at_maximum_price(
    monkeypatch,
    tmp_path: Path,
) -> None:
    summary, canonical = _run_stubbed_backsolve_main(
        monkeypatch,
        tmp_path,
        max_price=Decimal("50"),
    )

    boundary = summary["maximum_feasible_boundary"]
    assert summary["cash_on_cash_year_1_persisted"] == 0.07
    assert boundary["highest_feasible_price"] == 50.0
    assert boundary["search_ceiling_reached"] is True
    assert boundary["next_infeasible_price"] is None
    assert boundary["next_infeasible_coc"] is None
    assert canonical["purchase_assumptions"]["purchase_price"] == 50.0
