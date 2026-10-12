"""Regression: a Yardi "Rent Roll with Lease Charges" export.

The synthetic workbook (tests/yardi_fixtures.py) reproduces the report's
structure: one block per unit whose first row also carries the first charge
line (often a small fee, not rent), further charge rows, a Total row; vacant
units marked VACANT with no status column; a Future Residents/Applicants
section repeating a pre-leased vacant unit; Summary Groups and Summary of
Charges blocks; unit types with a property-code prefix.

Before the fix the future section was counted as units, no unit was vacant,
in-place rent was the market rent, and every cohort had 0 bedrooms.
"""

from __future__ import annotations

import pytest
from openpyxl import Workbook

from engine.ingest.rent_roll_parser import _letter_floorplan_bed_bath, parse_rent_roll
from tests.yardi_fixtures import RR_UNITS, rr_expected, write_yardi_rent_roll, yardi_rent_roll_rows


@pytest.fixture
def roll(tmp_path):
    return parse_rent_roll(write_yardi_rent_roll(tmp_path / "rent_roll.xlsx"))


def _cohorts(result):
    return {c["cohort_id"]: c for c in result["unit_cohorts"]}


def test_future_residents_and_summary_blocks_are_not_units(roll):
    expected = rr_expected()

    assert roll["layout"] == "yardi_lease_charges"
    assert roll["total_units"] == expected["units"]
    assert sum(c["unit_count"] for c in roll["unit_cohorts"]) == expected["units"]
    assert roll["pre_leased_vacant_units"] == ["107"]


def test_vacancy_comes_from_vacant_resident_rows(roll):
    expected = rr_expected()

    assert roll["vacant_units"] == expected["vacant_units"] == 2
    assert roll["physical_vacancy_rate"] == pytest.approx(round(2 / 12, 4))


def test_in_place_rent_is_the_sum_of_rent_charge_code_lines(roll):
    rent = rr_expected()["rent_by_unit"]
    cohorts = _cohorts(roll)

    # 101's first charge line is pest 4.00; rent is the "rent" line.
    assert cohorts["x701_a1"]["initial_inplace_rent"] == pytest.approx(
        round((rent["101"] + rent["102"] + rent["112"]) / 3, 2)
    )
    # 104 also carries an employee credit (not rent); 103 is vacant.
    assert cohorts["x701_a1g"]["initial_inplace_rent"] == pytest.approx(rent["104"])
    # 109 has two rent lines.
    assert cohorts["x701_b2p"]["initial_inplace_rent"] == pytest.approx((rent["108"] + rent["109"]) / 2)
    assert cohorts["x701_b2p"]["initial_inplace_rent"] != cohorts["x701_b2p"]["target_monthly_rent"]


def test_first_row_amount_is_rent_only_without_charge_codes(tmp_path):
    rows = yardi_rent_roll_rows(summary=False)
    by_unit = {u[0]: u for u in RR_UNITS}
    kept = []
    for i, row in enumerate(rows):
        if row and isinstance(row[0], int):
            unit = by_unit[str(row[0])]
            row = list(row)
            row[6] = None
            row[7] = sum(amount for code, amount in unit[5] if code == "rent")
            kept.append(row)
        elif row and isinstance(row[0], str) and row[0].startswith("Future"):
            break
        elif i < 7 or not (row[0] is None and len(row) > 6 and row[6] is not None):  # drop charge rows
            kept.append(row)
    path = tmp_path / "no_codes.xlsx"
    wb = Workbook()
    for row in kept:
        wb.active.append(row)
    wb.save(path)

    result = parse_rent_roll(path)
    rent = rr_expected()["rent_by_unit"]
    assert result["total_units"] == 12
    assert _cohorts(result)["x701_b2p"]["initial_inplace_rent"] == pytest.approx((rent["108"] + rent["109"]) / 2)


def test_property_code_prefix_is_stripped_for_bed_bath_inference(roll):
    cohorts = _cohorts(roll)

    assert (cohorts["x701_a1"]["bedrooms"], cohorts["x701_a1"]["bathrooms"]) == (1, 1.0)
    assert (cohorts["x701_a1g"]["bedrooms"], cohorts["x701_a1g"]["bathrooms"]) == (1, 1.0)
    assert (cohorts["x701_b1"]["bedrooms"], cohorts["x701_b1"]["bathrooms"]) == (2, 1.0)
    assert (cohorts["x701_b2p"]["bedrooms"], cohorts["x701_b2p"]["bathrooms"]) == (2, 2.0)
    # B3: 3 baths in a 2BR is not a plausible reading; house default, flagged.
    assert (cohorts["x701_b3p"]["bedrooms"], cohorts["x701_b3p"]["bathrooms"]) == (2, 2.0)
    assert "bathrooms_assumed_for_cohort_x701_b3p" in roll["flags"]
    assert "bed_bath_inferred_from_floorplan_letter_code" in roll["flags"]
    assert "unit_type_codes_unrecognized" not in roll.get("blockers", [])


@pytest.mark.parametrize(
    ("code", "beds", "baths", "from_code"),
    [
        ("A1", 1, 1.0, True),
        ("B1", 2, 1.0, True),
        ("B2", 2, 2.0, True),
        ("B2P", 2, 2.0, True),
        ("x701_B1G", 2, 1.0, True),
        ("Q9X_C2", 3, 2.0, True),
        ("A2", 1, 1.0, False),
        ("B3", 2, 2.0, False),
        ("B", 2, 2.0, False),
    ],
)
def test_letter_floorplan_baths_are_read_only_when_plausible(code, beds, baths, from_code):
    inferred = _letter_floorplan_bed_bath(code)
    assert (inferred["bedrooms"], inferred["bathrooms"], inferred["baths_from_code"]) == (beds, baths, from_code)


@pytest.mark.parametrize("code", ["bc_Q1", "dlx", "blm-2dlx", "1BR/1BA", "E1"])
def test_non_letter_floorplan_codes_are_not_letter_floorplans(code):
    assert _letter_floorplan_bed_bath(code) is None


def test_export_summary_is_read_and_ties(roll):
    expected = rr_expected()

    assert roll["export_summary"] == {
        "source": "yardi_summary_groups",
        "units": expected["units"],
        "occupied_units": expected["units"] - expected["vacant_units"],
        "vacant_units": expected["vacant_units"],
        "non_revenue_units": 0,
        "market_rent_total": expected["market_rent_total"],
        "rent_charges_total": expected["rent_charges_total"],
    }
    recon = roll["reconciliation"]
    assert recon["status"] == "PASS"
    assert {check: recon[check]["status"] for check in
            ("units", "vacant_units", "market_rent_total", "rent_charges_total")} == dict.fromkeys(
        ("units", "vacant_units", "market_rent_total", "rent_charges_total"), "PASS"
    )
