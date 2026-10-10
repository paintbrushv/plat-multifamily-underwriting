"""Regression: run_underwriting must apply growth_assumptions to market rent.

Before the fix, run_underwriting never called generate_rent_curves, so a flat
market_rent_curve (the shape ingestion produces) modeled 0% rent growth no
matter what annual_growth_rate said, while opex kept growing.
"""
from __future__ import annotations

import copy

import pytest

from engine.engine import run_underwriting
from engine.modules.scenarios import ScenarioPresets, _apply_scenario_deltas


def _run(inputs, rate):
    deal = copy.deepcopy(inputs)
    deal["growth_assumptions"] = {"growth_type": "annual_compound", "annual_growth_rate": rate}
    return run_underwriting(deal)


def test_exit_noi_and_irr_respond_to_rent_growth(minimal_deal_inputs):
    low, mid, high = (_run(minimal_deal_inputs, r) for r in (0.005, 0.025, 0.045))

    exit_noi = [r["metrics"]["exit"]["forward_noi"] for r in (low, mid, high)]
    assert exit_noi[0] < exit_noi[1] < exit_noi[2]

    irr = [r["metrics"]["irr"]["unlevered_irr"] for r in (low, mid, high)]
    assert irr[0] < irr[1] < irr[2]

    # Annual compounding starts in year 2, so year-1 NOI is unchanged.
    year_1 = {r["metrics"]["noi"]["year_1_noi"] for r in (low, mid, high)}
    assert len(year_1) == 1


def test_flat_curve_is_grown_annually_from_year_two(minimal_deal_inputs):
    result = _run(minimal_deal_inputs, 0.03)

    applied = result["rent_growth"]
    assert applied["applied_to_cohorts"] == ["1BR", "2BR"]
    assert applied["explicit_curve_cohorts"] == []

    curve = sorted(
        (seg for seg in applied["market_rent_curve"] if seg["cohort_id"] == "1BR"),
        key=lambda seg: seg["start_period"],
    )
    assert [(s["start_period"], s["end_period"]) for s in curve[:2]] == [
        ("2026-07", "2027-06"),
        ("2027-07", "2028-06"),
    ]
    assert curve[0]["market_rent"] == pytest.approx(1250.00)
    assert curve[1]["market_rent"] == pytest.approx(1287.50)
    assert curve[-1]["market_rent"] == pytest.approx(round(1250 * 1.03**4, 2))


def test_zero_growth_leaves_results_unchanged(minimal_deal_inputs):
    deal = copy.deepcopy(minimal_deal_inputs)
    del deal["growth_assumptions"]
    without = run_underwriting(deal)
    zero = _run(minimal_deal_inputs, 0.0)

    assert zero["metrics"]["exit"] == without["metrics"]["exit"]
    assert zero["metrics"]["irr"] == without["metrics"]["irr"]


def test_explicit_time_varying_curve_is_not_grown_again(minimal_deal_inputs):
    """An analyst-built per-year curve is authoritative; growth is not stacked on it."""
    deal = copy.deepcopy(minimal_deal_inputs)
    deal["market_rent_curve"] = [
        {"cohort_id": cid, "start_period": f"{2026 + y}-07", "end_period": f"{2027 + y}-06",
         "market_rent": round(rent * 1.03**y, 2)}
        for cid, rent in (("1BR", 1250), ("2BR", 1550))
        for y in range(5)
    ]
    prebuilt = _run(deal, 0.0)
    with_growth = _run(deal, 0.03)

    assert with_growth["metrics"]["exit"] == prebuilt["metrics"]["exit"]
    assert with_growth["rent_growth"]["applied_to_cohorts"] == []
    assert with_growth["rent_growth"]["explicit_curve_cohorts"] == ["1BR", "2BR"]

    # The pre-built curve and the engine-expanded flat curve agree.
    expanded = _run(minimal_deal_inputs, 0.03)
    assert with_growth["metrics"]["exit"]["forward_noi"] == pytest.approx(
        expanded["metrics"]["exit"]["forward_noi"], abs=1.0
    )


def test_scenario_rent_growth_shock_is_not_double_counted(minimal_deal_inputs):
    """A multi-segment but flat curve is grown by the engine, not by the scenario."""
    deal = copy.deepcopy(minimal_deal_inputs)
    deal["market_rent_curve"] = [
        {"cohort_id": cid, "start_period": f"{2026 + y}-07", "end_period": f"{2027 + y}-06",
         "market_rent": rent}
        for cid, rent in (("1BR", 1250), ("2BR", 1550))
        for y in range(5)
    ]
    presets = ScenarioPresets(
        name="up", rent_growth_delta_bps=50, exit_cap_delta_bps=0,
        vacancy_delta_bps=0, opex_growth_delta_bps=0,
    )
    shocked = _apply_scenario_deltas(deal, presets)

    assert {s["market_rent"] for s in shocked["market_rent_curve"]} == {1250, 1550}
    assert shocked["growth_assumptions"]["annual_growth_rate"] == pytest.approx(0.035)
