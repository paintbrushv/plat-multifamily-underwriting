"""Wave 1b Task 1.4 — defense-in-depth tests for revenue.compute_base_rent and
_find_curve_value.

Covers:
- cohort_ids dedup (order-preserving) when unit_cohorts has duplicate cohort_id
- _find_curve_value raises on overlapping segments
- _find_curve_value raises on missing segment
"""
from __future__ import annotations

import pytest

from engine.engine import run_underwriting
from engine.modules.revenue import _find_curve_value


def _base_inputs():
    """Minimal single-cohort inputs that exercise compute_base_rent."""
    return {
        "schema_version": "0.1",
        "metadata": {
            "deal_id": "d",
            "run_id": "r",
            "as_of_date": "2026-01-01",
            "analyst": "a",
            "purpose": "p",
        },
        "time_grid": {"analysis_start_date": "2026-01", "analysis_end_date": "2026-01"},
        "unit_cohorts": [
            {"cohort_id": "c1", "unit_type": "1B", "unit_count": 10, "initial_inplace_rent": 1000},
        ],
        "market_rent_curve": [
            {"cohort_id": "c1", "start_period": "2026-01", "end_period": "2026-01", "market_rent": 1200},
        ],
        "loss_to_lease": [
            {"cohort_id": "c1", "start_period": "2026-01", "end_period": "2026-01", "ltl_percent": 0.1},
        ],
        "physical_vacancy_curve": [
            {"cohort_id": "c1", "start_period": "2026-01", "end_period": "2026-01", "vacancy_rate": 0.2},
        ],
        "collection_loss_curve": [
            {"applies_to": "ALL", "start_period": "2026-01", "end_period": "2026-01", "loss_rate": 0.05},
        ],
        "revenue_programs": [],
        "program_adoption_curve": [],
    }


def test_cohort_ids_dedupe_preserves_order():
    """If unit_cohorts contains a duplicate cohort_id (only possible via
    skip_validation=True since Wave 1a's HARD validator gate rejects this),
    compute_base_rent must NOT iterate the duplicate and double-count units."""
    inputs = _base_inputs()
    # Inject a duplicate-cohort_id second entry. This is malformed input; the
    # Wave 1a validator would reject it before run_underwriting reaches the
    # revenue module. We bypass with skip_validation=True to exercise the
    # defense-in-depth path.
    inputs["unit_cohorts"].append(
        {"cohort_id": "c1", "unit_type": "1B", "unit_count": 10, "initial_inplace_rent": 1000}
    )

    outputs = run_underwriting(inputs, skip_validation=True)
    row = outputs["revenue"]["base_rent"]["by_month"][0]

    # Without dedup, market_rent would be 12000 * 2 = 24000; with dedup it stays at 12000.
    assert row["market_rent"] == pytest.approx(12000.00, abs=0.01)
    assert row["inplace_rent"] == pytest.approx(10800.00, abs=0.01)
    assert row["net_rent"] == pytest.approx(8208.00, abs=0.01)


def test_cohort_ids_order_preserved_with_three_cohorts():
    """Multiple distinct cohorts with one duplicate — order of first occurrences preserved."""
    inputs = _base_inputs()
    inputs["unit_cohorts"] = [
        {"cohort_id": "a", "unit_type": "S", "unit_count": 1, "initial_inplace_rent": 100},
        {"cohort_id": "b", "unit_type": "1B", "unit_count": 1, "initial_inplace_rent": 200},
        {"cohort_id": "a", "unit_type": "S", "unit_count": 1, "initial_inplace_rent": 100},  # dup
        {"cohort_id": "c", "unit_type": "2B", "unit_count": 1, "initial_inplace_rent": 300},
    ]
    for cid in ("a", "b", "c"):
        inputs["market_rent_curve"].append(
            {"cohort_id": cid, "start_period": "2026-01", "end_period": "2026-01", "market_rent": 100}
        )
        inputs["loss_to_lease"].append(
            {"cohort_id": cid, "start_period": "2026-01", "end_period": "2026-01", "ltl_percent": 0.0}
        )
        inputs["physical_vacancy_curve"].append(
            {"cohort_id": cid, "start_period": "2026-01", "end_period": "2026-01", "vacancy_rate": 0.0}
        )
    # Drop the c1 baseline entries
    inputs["market_rent_curve"] = [r for r in inputs["market_rent_curve"] if r["cohort_id"] != "c1"]
    inputs["loss_to_lease"] = [r for r in inputs["loss_to_lease"] if r["cohort_id"] != "c1"]
    inputs["physical_vacancy_curve"] = [r for r in inputs["physical_vacancy_curve"] if r["cohort_id"] != "c1"]

    outputs = run_underwriting(inputs, skip_validation=True)
    row = outputs["revenue"]["base_rent"]["by_month"][0]
    # 3 unique cohorts × $100 each = $300. With double-count of `a`, would be $400.
    assert row["market_rent"] == pytest.approx(300.00, abs=0.01)


def test_find_curve_value_raises_on_overlap():
    """Two segments covering the same month → ValueError."""
    rows = [
        {"cohort_id": "x", "start_period": "2026-01", "end_period": "2026-06", "market_rent": 1000},
        {"cohort_id": "x", "start_period": "2026-04", "end_period": "2026-09", "market_rent": 1100},
    ]
    with pytest.raises(ValueError, match="Ambiguous curve lookup"):
        _find_curve_value(rows, "2026-05", "market_rent")


def test_find_curve_value_raises_on_missing():
    """No segment covers the month → KeyError."""
    rows = [
        {"cohort_id": "x", "start_period": "2026-01", "end_period": "2026-06", "market_rent": 1000},
    ]
    with pytest.raises(KeyError, match="Missing curve value"):
        _find_curve_value(rows, "2026-09", "market_rent")


def test_find_curve_value_single_match_returns():
    """Sanity: exactly-one-match path still works."""
    rows = [
        {"cohort_id": "x", "start_period": "2026-01", "end_period": "2026-06", "market_rent": 1000},
        {"cohort_id": "x", "start_period": "2026-07", "end_period": "2026-12", "market_rent": 1100},
    ]
    val = _find_curve_value(rows, "2026-05", "market_rent")
    assert val == 1000
