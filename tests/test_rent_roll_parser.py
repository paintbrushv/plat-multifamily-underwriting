"""Tests for rent roll → unit_cohorts + vacancy canonical format."""
from pathlib import Path

import pytest

from engine.ingest.rent_roll_parser import parse_rent_roll

FIXTURE = Path("tests/fixtures/sample_rent_roll.csv")


def test_returns_dict_with_expected_keys():
    result = parse_rent_roll(FIXTURE)
    assert "unit_cohorts" in result
    assert "physical_vacancy_rate" in result
    assert "total_units" in result
    assert "market_rent_by_cohort" in result


def test_cohorts_grouped_by_type():
    result = parse_rent_roll(FIXTURE)
    cohort_ids = [c["cohort_id"] for c in result["unit_cohorts"]]
    assert len(cohort_ids) == len(set(cohort_ids)), "Duplicate cohort IDs"
    # Sample has 3 types: 1BR/1BA, 2BR/1BA, 2BR/2BA
    assert len(result["unit_cohorts"]) == 3


def test_cohort_has_required_fields():
    result = parse_rent_roll(FIXTURE)
    for cohort in result["unit_cohorts"]:
        assert "cohort_id" in cohort
        assert "unit_type" in cohort
        assert "unit_count" in cohort
        assert "initial_inplace_rent" in cohort
        assert "sqft" in cohort


def test_initial_rent_is_average_of_occupied_units():
    result = parse_rent_roll(FIXTURE)
    oneb = next(c for c in result["unit_cohorts"] if "1BR" in c["unit_type"])
    # Occupied 1BR rents: 850, 825, 840 → avg = 838.33
    assert 830 < oneb["initial_inplace_rent"] < 850


def test_vacancy_rate_calculated_correctly():
    result = parse_rent_roll(FIXTURE)
    # 2 vacant out of 10 = 20% vacancy
    assert abs(result["physical_vacancy_rate"] - 0.20) < 0.01


def test_total_units_count():
    result = parse_rent_roll(FIXTURE)
    assert result["total_units"] == 10


def test_unit_counts_per_cohort():
    result = parse_rent_roll(FIXTURE)
    by_type = {c["unit_type"]: c["unit_count"] for c in result["unit_cohorts"]}
    assert by_type["1BR/1BA"] == 4
    assert by_type["2BR/1BA"] == 3
    assert by_type["2BR/2BA"] == 3


def test_market_rent_by_cohort_populated():
    result = parse_rent_roll(FIXTURE)
    mrents = result["market_rent_by_cohort"]
    assert len(mrents) == 3
    # All 1BR/1BA market rents = $950. Cohort_id is now lowercased
    # (Bug 1.3 fix — "Beal" and "BEAL" must collapse).
    assert mrents["1br1ba"] == pytest.approx(950.0)


def test_market_rent_by_cohort_ignores_zero_placeholders(tmp_path):
    """Blank/$0 market-rent cells must not dilute the exported market curve."""
    import csv

    path = tmp_path / "zero_dilution_rent_roll.csv"
    rows = [
        ["Unit", "Type", "Sqft", "Beds", "Baths", "Status", "Monthly Rent", "Market Rent"],
        ["101", "1BR/1BA", "650", "1", "1", "Occupied", "1100", "1200"],
        ["102", "1BR/1BA", "650", "1", "1", "Occupied", "1120", "1200"],
        ["103", "1BR/1BA", "650", "1", "1", "Occupied", "1110", "1200"],
        ["104", "1BR/1BA", "650", "1", "1", "Occupied", "1130", "1200"],
        ["105", "1BR/1BA", "650", "1", "1", "Vacant", "0", ""],
        ["106", "1BR/1BA", "650", "1", "1", "Vacant", "0", "0"],
    ]
    with path.open("w", newline="") as f:
        csv.writer(f).writerows(rows)

    result = parse_rent_roll(path)

    assert result["unit_cohorts"][0]["target_monthly_rent"] == pytest.approx(1200.0)
    assert result["market_rent_by_cohort"]["1br1ba"] == pytest.approx(1200.0)


def test_sqft_from_sample_unit():
    result = parse_rent_roll(FIXTURE)
    oneb = next(c for c in result["unit_cohorts"] if "1BR" in c["unit_type"])
    assert oneb["sqft"] == 650.0


def test_unsupported_extension_raises():
    with pytest.raises(ValueError, match="Unsupported"):
        parse_rent_roll(Path("file.txt"))


def test_cohorts_carry_bedrooms_and_bathrooms():
    """Schema gap fix: each cohort should expose bedrooms (int) and bathrooms (float).

    The fixture has 1BR/1BA, 2BR/1BA, 2BR/2BA — verify integer beds and float
    baths land on the canonical cohort dict in the expected types.
    """
    result = parse_rent_roll(FIXTURE)
    by_type = {c["unit_type"]: c for c in result["unit_cohorts"]}

    oneb = by_type["1BR/1BA"]
    assert "bedrooms" in oneb and "bathrooms" in oneb
    assert oneb["bedrooms"] == 1
    assert isinstance(oneb["bedrooms"], int)
    assert oneb["bathrooms"] == 1.0
    assert isinstance(oneb["bathrooms"], float)

    twob_oneba = by_type["2BR/1BA"]
    assert twob_oneba["bedrooms"] == 2
    assert twob_oneba["bathrooms"] == 1.0

    twob_twoba = by_type["2BR/2BA"]
    assert twob_twoba["bedrooms"] == 2
    assert twob_twoba["bathrooms"] == 2.0


def test_bedrooms_bathrooms_handle_studio_and_half_bath():
    """Studios → bedrooms=0; half baths → bathrooms=1.5 (float preserved)."""
    import csv
    import os
    import tempfile

    rows = [
        ["Unit", "Type", "Sqft", "Beds", "Baths", "Status", "Monthly Rent", "Market Rent"],
        ["S01", "Studio", "450", "0", "1", "Occupied", "800", "850"],
        ["S02", "Studio", "450", "0", "1", "Occupied", "810", "850"],
        ["301", "2BR/1.5BA", "950", "2", "1.5", "Occupied", "1100", "1200"],
        ["302", "2BR/1.5BA", "950", "2", "1.5", "Occupied", "1110", "1200"],
    ]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="") as f:
        csv.writer(f).writerows(rows)
        tmp = f.name
    try:
        result = parse_rent_roll(Path(tmp))
        by_type = {c["unit_type"]: c for c in result["unit_cohorts"]}

        studio = by_type["Studio"]
        assert studio["bedrooms"] == 0
        assert studio["bathrooms"] == 1.0

        half = by_type["2BR/1.5BA"]
        assert half["bedrooms"] == 2
        assert half["bathrooms"] == 1.5
    finally:
        os.unlink(tmp)


def test_all_vacant_cohort_uses_market_rent():
    """If a cohort has only vacant units, use market rent as initial_inplace_rent."""
    import csv, tempfile, os
    # Create a CSV with 2 vacant units of a type
    rows = [
        ["Unit", "Type", "Sqft", "Beds", "Baths", "Status", "Monthly Rent", "Market Rent"],
        ["201", "3BR/2BA", "1200", "3", "2", "Vacant", "0", "1500"],
        ["202", "3BR/2BA", "1200", "3", "2", "Vacant", "0", "1500"],
    ]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="") as f:
        csv.writer(f).writerows(rows)
        tmp = f.name
    try:
        result = parse_rent_roll(Path(tmp))
        cohort = result["unit_cohorts"][0]
        assert cohort["initial_inplace_rent"] == pytest.approx(1500.0)
    finally:
        os.unlink(tmp)


def test_explicit_blank_status_is_not_treated_as_occupied(tmp_path):
    """A blank status cell is unknown/vacant risk, not occupied evidence."""
    import csv

    path = tmp_path / "blank_status_rent_roll.csv"
    rows = [
        ["Unit", "Type", "Sqft", "Beds", "Baths", "Status", "Monthly Rent", "Market Rent"],
        ["101", "1BR/1BA", "650", "1", "1", "Occupied", "1100", "1200"],
        ["102", "1BR/1BA", "650", "1", "1", "", "0", "1200"],
    ]
    with path.open("w", newline="") as f:
        csv.writer(f).writerows(rows)

    result = parse_rent_roll(path)

    assert result["total_units"] == 2
    assert result["physical_vacancy_rate"] == pytest.approx(0.5)
    assert result["unit_cohorts"][0]["initial_inplace_rent"] == pytest.approx(1100.0)


def test_excel_rent_roll_stops_before_status_and_future_sections(tmp_path):
    """PMS rent rolls can append summaries and future residents below detail rows."""
    openpyxl = pytest.importorskip("openpyxl")

    path = tmp_path / "rent_roll.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Rent Roll"
    ws.append(["Rent Roll"])
    ws.append(["Property"])
    ws.append([None])
    ws.append(["Bldg-Unit", "Unit Type", "SQFT", "Unit Status", "Market Rent", "Actual Charges"])
    ws.append(["101", "0 Bed 1 Bath-A", 450, "Occupied No Notice", 1000, 950])
    ws.append(["102", "0 Bed 1 Bath-A", 450, "Vacant Unrented Ready", 1050, 0])
    ws.append([None, "The Frank Total:", 900, None, 2050, 950])
    ws.append(["Status Summary", None, None, "Charge Code Summary"])
    ws.append(["Description", "Unit Count", "Percent"])
    ws.append(["Total Rentable Units", 2, 1.0])
    ws.append(["Future Resident Details"])
    ws.append(["Bldg-Unit", "Unit Type", "SQFT", "Unit Status", "Market Rent", "Actual Charges"])
    ws.append(["102", "0 Bed 1 Bath-A", 450, "Vacant Rented Ready", 1050, 1040])
    wb.save(path)

    result = parse_rent_roll(path)

    assert result["total_units"] == 2
    assert [c["unit_type"] for c in result["unit_cohorts"]] == ["0 Bed 1 Bath-A"]
    assert result["unit_cohorts"][0]["unit_count"] == 2
    assert result["physical_vacancy_rate"] == pytest.approx(0.5)


def test_excel_rent_roll_recovers_shifted_unit_ids(tmp_path):
    """Some S2-style rent rolls place the unit id one column left of the Unit header."""
    openpyxl = pytest.importorskip("openpyxl")

    path = tmp_path / "shifted_unit_ids.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet"
    ws.append(["The Holden"])
    ws.append(["S2 Residential"])
    ws.append(["Rent Roll"])
    ws.append(["5/11/2026"])
    ws.append([None])
    ws.append([None])
    ws.append(["Current"])
    ws.append([None, "Unit", "Type", None, "Sq. Feet", "Residents", None, None, None, None, "Status"])
    ws.append(["111", None, "A1", None, 700, "Trina Turner", None, None, None, None, "C"])
    ws.append(["112", None, "A1", None, 700, "Kyle Baker", None, None, None, None, "C"])
    wb.save(path)

    result = parse_rent_roll(path)

    assert result["total_units"] == 2
    assert len(result["unit_cohorts"]) == 1
    cohort = result["unit_cohorts"][0]
    assert cohort["unit_type"] == "A1"
    assert cohort["unit_count"] == 2
    assert cohort["sqft"] == 700.0


def test_excel_rent_roll_does_not_promote_floorplan_summary_rows(tmp_path):
    """Floorplan summary rows can look unit-like but must stay excluded."""
    openpyxl = pytest.importorskip("openpyxl")

    path = tmp_path / "floorplan_summary.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet"
    ws.append(["The Holden"])
    ws.append(["S2 Residential"])
    ws.append(["Rent Roll"])
    ws.append(["5/11/2026"])
    ws.append([None])
    ws.append([None])
    ws.append(["Current"])
    ws.append([None, "Unit", "Type", None, "Sq. Feet", "Residents", None, None, None, None, "Status"])
    ws.append(["A1", None, None, None, None, None, None, 34625, None, None, None, None])
    ws.append(["A1R", None, None, None, None, None, None, 69300, None, None, None, None])
    wb.save(path)

    result = parse_rent_roll(path)

    assert result["total_units"] == 0
    assert result["unit_cohorts"] == []


# ---------------------------------------------------------------------------
# Wave 3 Task 3.1 — bug 1.2 / 1.3 / 1.13 + beds/baths population + provenance.
# ---------------------------------------------------------------------------


def _write_csv(rows: list[list[str]]) -> Path:
    import csv, tempfile
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, newline=""
    )
    csv.writer(f).writerows(rows)
    f.close()
    return Path(f.name)


def test_exact_column_match_wins():
    """Bug 1.2 fix — when both 'Rent' and 'Rent Type' are present,
    `rent` resolution must pick `Rent`, not `Rent Type` (which is
    a string like 'MTM' that would silently coerce to 0.0).
    """
    import os
    rows = [
        # `Rent Type` is a non-numeric column ("Lease"/"MTM"). The
        # legacy substring-first resolver returned that column for
        # `_find("rent")` because it's listed first.
        ["Unit", "Rent Type", "Type", "Sqft", "Beds", "Baths",
         "Status", "Rent", "Market Rent"],
        ["101", "Lease", "1BR/1BA", "650", "1", "1", "Occupied",
         "850", "950"],
        ["102", "MTM", "1BR/1BA", "650", "1", "1", "Occupied",
         "830", "950"],
    ]
    tmp = _write_csv(rows)
    try:
        result = parse_rent_roll(tmp)
        cohort = result["unit_cohorts"][0]
        # Average of 850 and 830 = 840 — only possible if `Rent` won
        # over `Rent Type`.
        assert cohort["initial_inplace_rent"] == pytest.approx(840.0), (
            "Exact-match column resolution failed: 'Rent' should win "
            "over 'Rent Type'."
        )
    finally:
        os.unlink(tmp)


def test_substring_fallback_only_when_no_exact():
    """Bug 1.2 fix — if no exact `Rent` header but a `Monthly Rent`
    column is present, the substring fallback path must still resolve.
    """
    import os
    rows = [
        ["Unit", "Type", "Sqft", "Beds", "Baths", "Status",
         "Monthly Rent", "Market Rent"],
        ["101", "1BR/1BA", "650", "1", "1", "Occupied", "900", "950"],
        ["102", "1BR/1BA", "650", "1", "1", "Occupied", "920", "950"],
    ]
    tmp = _write_csv(rows)
    try:
        result = parse_rent_roll(tmp)
        cohort = result["unit_cohorts"][0]
        assert cohort["initial_inplace_rent"] == pytest.approx(910.0)
    finally:
        os.unlink(tmp)


def test_cohort_id_lowercase_normalization():
    """Bug 1.3 fix — `Type` strings differing only in case must collapse
    to the same cohort_id namespace, exposed by an explicit duplicate
    raising the uniqueness assertion.
    """
    import os
    rows = [
        ["Unit", "Type", "Sqft", "Beds", "Baths", "Status",
         "Monthly Rent", "Market Rent"],
        # Two cohort_keys differing only in case — these MUST collide,
        # which proves lowercase normalization is doing its job. The
        # uniqueness assertion will catch them and raise.
        ["101", "Beal", "650", "1", "1", "Occupied", "850", "950"],
        ["102", "BEAL", "650", "1", "1", "Occupied", "860", "950"],
    ]
    tmp = _write_csv(rows)
    try:
        with pytest.raises(AssertionError, match="Duplicate cohort_ids"):
            parse_rent_roll(tmp)
    finally:
        os.unlink(tmp)


def test_cohort_id_uniqueness_assert():
    """Bug 1.3 fix — collisions surface with a clear error naming both
    raw cohort keys.
    """
    import os
    rows = [
        ["Unit", "Type", "Sqft", "Beds", "Baths", "Status",
         "Monthly Rent", "Market Rent"],
        ["101", "Beal!", "650", "1", "1", "Occupied", "850", "950"],
        ["102", "BEAL", "650", "1", "1", "Occupied", "860", "950"],
    ]
    tmp = _write_csv(rows)
    try:
        with pytest.raises(AssertionError) as excinfo:
            parse_rent_roll(tmp)
        msg = str(excinfo.value)
        # Both raw keys must appear in the error.
        assert "Beal!" in msg or "BEAL" in msg
        assert "beal" in msg.lower()  # the resolved colliding cohort_id
    finally:
        os.unlink(tmp)


def test_to_int_raises_on_no_digit():
    """Bug 1.13 fix — `_to_int("studio")` must raise loudly with a
    diagnostic, instead of silently returning 0.
    """
    from engine.ingest.rent_roll_parser import _to_int
    with pytest.raises(ValueError, match="no digit"):
        _to_int("studio")


def test_to_int_handles_bedroom_formats():
    """Bug 1.13 fix — regex extracts the leading int from many formats."""
    from engine.ingest.rent_roll_parser import _to_int
    assert _to_int("1 Bedroom") == 1
    assert _to_int("1bed") == 1
    assert _to_int("1BR") == 1
    assert _to_int("2 BR/1.5 BA") == 2
    assert _to_int("3 Bedrooms") == 3


def test_unit_cohorts_have_beds_baths():
    """Wave 2 follow-up — every cohort dict must carry bedrooms (int) and
    bathrooms (float). Covers (a) explicit Beds/Baths columns and
    (b) regex inference from cohort_key when columns are absent.
    """
    import os
    # Path A — explicit Beds/Baths columns.
    result_a = parse_rent_roll(FIXTURE)
    for cohort in result_a["unit_cohorts"]:
        assert "bedrooms" in cohort
        assert "bathrooms" in cohort
        assert isinstance(cohort["bedrooms"], int)
        assert isinstance(cohort["bathrooms"], float)

    # Path B — only Type column, infer beds/baths via cohort_key regex.
    rows = [
        ["Unit", "Type", "Sqft", "Status", "Monthly Rent", "Market Rent"],
        ["101", "1BR/1BA", "650", "Occupied", "900", "950"],
        ["102", "2BR/1.5BA", "950", "Occupied", "1100", "1200"],
    ]
    tmp = _write_csv(rows)
    try:
        result_b = parse_rent_roll(tmp)
        by_type = {c["unit_type"]: c for c in result_b["unit_cohorts"]}
        assert by_type["1BR/1BA"]["bedrooms"] == 1
        assert by_type["1BR/1BA"]["bathrooms"] == 1.0
        assert by_type["2BR/1.5BA"]["bedrooms"] == 2
        assert by_type["2BR/1.5BA"]["bathrooms"] == 1.5
        for c in result_b["unit_cohorts"]:
            assert isinstance(c["bedrooms"], int)
            assert isinstance(c["bathrooms"], float)
    finally:
        os.unlink(tmp)


# ---------------------------------------------------------------------------
# legacy PMS regression — multi-sheet RedIQ rent rolls + unit-type-code beds.
# ---------------------------------------------------------------------------


def _make_xlsx(sheets: dict[str, list[list]]) -> Path:
    """Helper: build a temp xlsx from {sheet_name: [[row], ...]}."""
    import openpyxl
    import tempfile

    wb = openpyxl.Workbook()
    # Drop the default sheet.
    default = wb.active
    wb.remove(default)
    for name, rows in sheets.items():
        ws = wb.create_sheet(title=name)
        for row in rows:
            ws.append(row)
    f = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    wb.save(f.name)
    f.close()
    return Path(f.name)


def test_multi_sheet_picks_unit_detail_over_floor_plan_summary():
    """Sheet-selection heuristic — when a workbook has both a 'Floor Plan'
    summary sheet and a 'Rent Roll' detail sheet, pick the detail sheet.

    Mirrors RedIQ's real-world export structure (a production deal):
      Sheet 1: 'Floor Plan'  (15 rows of summary aggregates)
      Sheet 2: 'Rent Roll'   (272+ unit-level rows)
    """
    import os
    floor_plan_summary = [
        ["Floor Plan", "Units", "Avg Rent"],  # summary header
        ["1BR/1BA", 50, 1000],
        ["2BR/2BA", 30, 1300],
    ]
    rent_roll_detail = [
        ["Unit", "Type", "Sqft", "Beds", "Baths", "Status",
         "Monthly Rent", "Market Rent"],
        ["101", "1BR/1BA", 650, 1, 1, "Occupied", 900, 950],
        ["102", "1BR/1BA", 650, 1, 1, "Occupied", 920, 950],
        ["201", "2BR/2BA", 1100, 2, 2, "Occupied", 1300, 1400],
    ]
    fp = _make_xlsx({
        "Floor Plan": floor_plan_summary,
        "Rent Roll": rent_roll_detail,
    })
    try:
        result = parse_rent_roll(fp)
        # Detail sheet → 3 units (2x 1BR + 1x 2BR), 2 cohorts.
        assert result["total_units"] == 3
        cohort_units = {c["unit_type"]: c["unit_count"] for c in result["unit_cohorts"]}
        assert cohort_units["1BR/1BA"] == 2
        assert cohort_units["2BR/2BA"] == 1
    finally:
        os.unlink(fp)


def test_unit_type_codes_extract_beds():
    """legacy-PMS export fix — when no Beds column is present, extract bed
    count from unit-type codes like 'blm-1dlx', 'blm-2dlx', 'blm-3bdr'.
    """
    import os
    rows = [
        ["Unit", "Unit Type", "Sqft", "Status", "Monthly Rent", "Market Rent"],
        ["0111", "blm-3dlx", 1817, "Occupied", 1525, 1599],
        ["0112", "blm-2dlx", 1622, "Occupied", 1352, 1499],
        ["0113", "blm-1dlx", 1185, "Occupied", 1199, 1199],
        ["0114", "blm-3bdr", 1622, "Occupied", 1525, 1525],
        ["0115", "blm-2std", 1622, "Occupied", 1499, 1499],
    ]
    fp = _make_xlsx({"Report1": rows})
    try:
        result = parse_rent_roll(fp)
        assert result["total_units"] == 5
        beds_by_type = {c["unit_type"]: c["bedrooms"] for c in result["unit_cohorts"]}
        assert beds_by_type["blm-3dlx"] == 3
        assert beds_by_type["blm-2dlx"] == 2
        assert beds_by_type["blm-1dlx"] == 1
        assert beds_by_type["blm-3bdr"] == 3
        assert beds_by_type["blm-2std"] == 2
        # No blocker emitted when extraction succeeds for all rows.
        assert "blockers" not in result or "unit_type_codes_unrecognized" not in result.get("blockers", [])
    finally:
        os.unlink(fp)


def test_unit_type_codes_unrecognized_returns_blocker():
    """legacy-PMS export fix — when >20% of rows have unrecognized unit-type
    codes (no digit, no studio/efficiency match), emit a blocker.
    """
    import os
    rows = [
        ["Unit", "Unit Type", "Sqft", "Status", "Monthly Rent", "Market Rent"],
        # All-letters codes — no bed count extractable.
        ["101", "alpha", 800, "Occupied", 1000, 1100],
        ["102", "bravo", 850, "Occupied", 1050, 1150],
        ["103", "charlie", 900, "Occupied", 1100, 1200],
        ["104", "delta", 950, "Occupied", 1150, 1250],
        ["105", "echo", 1000, "Occupied", 1200, 1300],
    ]
    fp = _make_xlsx({"Report1": rows})
    try:
        result = parse_rent_roll(fp)
        assert "blockers" in result
        assert "unit_type_codes_unrecognized" in result["blockers"]
    finally:
        os.unlink(fp)


def test_letter_prefixed_bed_bath_codes_infer_beds_and_baths():
    """Custom PMS codes like c-b2-2x2 should not collapse to fallback 0BR."""
    import os

    rows = [
        ["Unit", "Unit Type", "Sqft", "Status", "Monthly Rent", "Market Rent"],
        ["0112", "c-a1-1x1", 1017, "Occupied", 1190, 1425],
        ["0113", "c-b2-2x2", 1332, "Vacant", 0, 1550],
    ]
    fp = _make_xlsx({"Report1": rows})
    try:
        result = parse_rent_roll(fp)
        by_type = {c["unit_type"]: c for c in result["unit_cohorts"]}
        assert by_type["c-a1-1x1"]["bedrooms"] == 1
        assert by_type["c-a1-1x1"]["bathrooms"] == 1.0
        assert by_type["c-b2-2x2"]["bedrooms"] == 2
        assert by_type["c-b2-2x2"]["bathrooms"] == 2.0
        assert "blockers" not in result or "unit_type_codes_unrecognized" not in result.get("blockers", [])
    finally:
        os.unlink(fp)


def test_space_separated_plan_codes_infer_beds_and_baths():
    """Plan labels like `B2 2x2` should not collapse to fallback 0BR."""
    import os

    rows = [
        ["Unit", "Unit Type", "Sqft", "Status", "Monthly Rent", "Market Rent"],
        ["1004", "A1 1x1", 700, "Occupied", 907, 1100],
        ["1002", "B2 2x2", 1125, "Occupied", 1257, 1450],
        ["1001", "C1 3x2.5", 1500, "Vacant", 0, 1850],
    ]
    fp = _make_xlsx({"Report1": rows})
    try:
        result = parse_rent_roll(fp)
        by_type = {c["unit_type"]: c for c in result["unit_cohorts"]}
        assert by_type["A1 1x1"]["bedrooms"] == 1
        assert by_type["A1 1x1"]["bathrooms"] == 1.0
        assert by_type["B2 2x2"]["bedrooms"] == 2
        assert by_type["B2 2x2"]["bathrooms"] == 2.0
        assert by_type["C1 3x2.5"]["bedrooms"] == 3
        assert by_type["C1 3x2.5"]["bathrooms"] == 2.5
        assert "blockers" not in result or "unit_type_codes_unrecognized" not in result.get("blockers", [])
    finally:
        os.unlink(fp)


def test_excel_summary_row_without_unit_is_skipped() -> None:
    """Aggregate rows with large rent totals but no unit identity are not units."""
    import os

    rows = [
        ["Unit", "Unit Type", "Sqft", "Status", "Monthly Rent", "Market Rent"],
        ["0101", "bc_Q1", 860, "Occupied", 1483, 1727],
        [None, None, None, None, 444588, 444588],
    ]
    fp = _make_xlsx({"Report1": rows})
    try:
        result = parse_rent_roll(fp)
        assert result["total_units"] == 1
        assert [c["unit_type"] for c in result["unit_cohorts"]] == ["bc_Q1"]
    finally:
        os.unlink(fp)


def test_box_score_sidecar_maps_beds_and_baths_for_opaque_codes(tmp_path: Path) -> None:
    """Opaque PMS codes can borrow bed/bath data from a sibling box score workbook."""
    import openpyxl

    rent_roll = tmp_path / "Briarcrest Rent Roll 4.14.26.xlsx"
    box_score = tmp_path / "Briarcrest Box Score 4.14.26.xlsx"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Report1"
    ws.append(["Unit", "Unit Type", "Sqft", "Status", "Monthly Rent", "Market Rent"])
    ws.append(["0101", "bc_Q1", 860, "Occupied", 1483, 1727])
    ws.append(["0201", "bc_B2", 1029, "Occupied", 1765, 1899])
    wb.save(rent_roll)
    wb.close()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Box Score"
    ws.append(["Plan Code", "Description"])
    ws.append(["bc_Q1", "2X1 860 sqft"])
    ws.append(["bc_B2", "2x2 1029 sqft"])
    wb.save(box_score)
    wb.close()

    result = parse_rent_roll(rent_roll)
    by_type = {c["unit_type"]: c for c in result["unit_cohorts"]}
    assert by_type["bc_Q1"]["bedrooms"] == 2
    assert by_type["bc_Q1"]["bathrooms"] == 1.0
    assert by_type["bc_B2"]["bedrooms"] == 2
    assert by_type["bc_B2"]["bathrooms"] == 2.0
    assert "blockers" not in result or "unit_type_codes_unrecognized" not in result.get("blockers", [])


def test_cohort_id_provenance_emitted():
    """Bonus — returned dict carries `cohort_id_provenance` with one entry
    per cohort, each documenting raw_cohort_key, resolved_cohort_id, and
    the ordered normalization steps.
    """
    result = parse_rent_roll(FIXTURE)
    assert "cohort_id_provenance" in result
    prov = result["cohort_id_provenance"]
    assert isinstance(prov, list)
    assert len(prov) == len(result["unit_cohorts"])
    for entry in prov:
        assert "raw_cohort_key" in entry
        assert "resolved_cohort_id" in entry
        assert "normalization_steps" in entry
        assert isinstance(entry["normalization_steps"], list)
        assert len(entry["normalization_steps"]) >= 1
    # Spot-check a known cohort.
    by_raw = {e["raw_cohort_key"]: e for e in prov}
    assert "1BR/1BA" in by_raw
    assert by_raw["1BR/1BA"]["resolved_cohort_id"] == "1br1ba"


# ---------------------------------------------------------------------------
# V1.2 — target_monthly_rent default from rent roll average market rent.
# ---------------------------------------------------------------------------


def test_target_monthly_rent_defaults_to_cohort_avg_market_rent():
    """V1.2 — every cohort gets a target_monthly_rent equal to its in-place
    average market rent, with provenance source 'rent_roll_avg'.

    Fixture has 3 cohorts. We compute the expected average per cohort by
    re-reading the CSV directly and verify each cohort's
    target_monthly_rent matches.
    """
    import csv as _csv
    rows = [
        ["Unit", "Type", "Sqft", "Beds", "Baths", "Status",
         "Monthly Rent", "Market Rent"],
        # 1BR/1BA — 3 units, market rents 950, 960, 970 → avg 960
        ["101", "1BR/1BA", "650", "1", "1", "Occupied", "850", "950"],
        ["102", "1BR/1BA", "650", "1", "1", "Occupied", "830", "960"],
        ["103", "1BR/1BA", "650", "1", "1", "Vacant",   "0",   "970"],
        # 2BR/2BA — 2 units, market rents 1300, 1320 → avg 1310
        ["201", "2BR/2BA", "950", "2", "2", "Occupied", "1100", "1300"],
        ["202", "2BR/2BA", "950", "2", "2", "Occupied", "1110", "1320"],
        # 3BR/2BA — 2 units, market rents 1500, 1500 → avg 1500
        ["301", "3BR/2BA", "1200", "3", "2", "Occupied", "1400", "1500"],
        ["302", "3BR/2BA", "1200", "3", "2", "Occupied", "1410", "1500"],
    ]
    tmp = _write_csv(rows)
    try:
        result = parse_rent_roll(tmp)
        by_id = {c["cohort_id"]: c for c in result["unit_cohorts"]}

        assert by_id["1br1ba"]["target_monthly_rent"] == pytest.approx(960.0)
        assert by_id["1br1ba"]["target_monthly_rent_source"] == "rent_roll_avg"

        assert by_id["2br2ba"]["target_monthly_rent"] == pytest.approx(1310.0)
        assert by_id["2br2ba"]["target_monthly_rent_source"] == "rent_roll_avg"

        assert by_id["3br2ba"]["target_monthly_rent"] == pytest.approx(1500.0)
        assert by_id["3br2ba"]["target_monthly_rent_source"] == "rent_roll_avg"

        # No cohort should have triggered a missing flag.
        for flag in result.get("blockers", []):
            assert not flag.startswith("target_monthly_rent_missing_for_cohort_")
    finally:
        import os
        os.unlink(tmp)


def test_target_monthly_rent_missing_emits_sanity_flag():
    """V1.2 — when a cohort has no usable rent data at all, target_monthly_rent
    is set to None and a per-cohort sanity flag is emitted in `blockers`.

    We construct a CSV where one cohort has zero market_rent AND zero
    in-place rent (vacant + no asking). The flag must reference the
    resolved cohort_id.
    """
    import os
    rows = [
        ["Unit", "Type", "Sqft", "Beds", "Baths", "Status",
         "Monthly Rent", "Market Rent"],
        # 4BR/2BA cohort — no rents on file
        ["401", "4BR/2BA", "1400", "4", "2", "Vacant", "0", "0"],
        # 1BR/1BA cohort — has data
        ["101", "1BR/1BA", "650", "1", "1", "Occupied", "850", "950"],
    ]
    tmp = _write_csv(rows)
    try:
        result = parse_rent_roll(tmp)
        by_id = {c["cohort_id"]: c for c in result["unit_cohorts"]}

        assert by_id["4br2ba"]["target_monthly_rent"] is None
        assert by_id["4br2ba"]["target_monthly_rent_source"] == "missing"
        assert "target_monthly_rent_missing_for_cohort_4br2ba" in (
            result.get("blockers", [])
        )

        # Cohort with data should still be tagged rent_roll_avg.
        assert by_id["1br1ba"]["target_monthly_rent"] == pytest.approx(950.0)
        assert by_id["1br1ba"]["target_monthly_rent_source"] == "rent_roll_avg"
    finally:
        os.unlink(tmp)


def test_legacy_xls_rent_roll_uses_pandas_reader(monkeypatch, tmp_path):
    """Legacy BIFF .xls rent rolls should not be routed through openpyxl."""
    import pandas as pd

    path = tmp_path / "legacy_rent_roll.xls"
    path.write_bytes(b"not-a-zip")

    def fake_read_excel(received_path, *, sheet_name, header, dtype, engine):
        assert received_path == path
        assert sheet_name is None
        assert header is None
        assert dtype is object
        assert engine == "xlrd"
        return {
            "Summary": pd.DataFrame([["Floor Plan"], ["Total"]]),
            "Rent Roll": pd.DataFrame(
                [
                    ["Unit", "Type", "Sqft", "Beds", "Baths", "Status", "Monthly Rent", "Market Rent"],
                    ["101", "1BR/1BA", "650", "1", "1", "Occupied", "850", "950"],
                    ["102", "1BR/1BA", "650", "1", "1", "Vacant", "0", "960"],
                ]
            ),
        }

    monkeypatch.setattr(pd, "read_excel", fake_read_excel)

    result = parse_rent_roll(path)

    assert result["total_units"] == 2
    assert result["physical_vacancy_rate"] == pytest.approx(0.5)
    assert result["unit_cohorts"][0]["initial_inplace_rent"] == pytest.approx(850.0)


def test_future_applicant_rows_do_not_count_as_units():
    """RealPage detail+summary rent rolls include duplicate future lease rows."""
    import os

    rows = [
        ["Unit", "Type", "Sqft", "Beds", "Baths", "Status", "Monthly Rent", "Market Rent"],
        ["101", "A1", "550", "1", "1", "Occupied", "1100", "1150"],
        ["101", "A1", "550", "1", "1", "Pending renewal", "1125", "1150"],
        ["102", "A1", "550", "1", "1", "Applicant", "1125", "1150"],
        ["103", "A1", "550", "1", "1", "Pending resident", "1125", "1150"],
        ["104", "A1", "550", "1", "1", "Vacant-Leased", "0", "1150"],
        ["105", "A1", "550", "1", "1", "Admin/Down", "0", "1150"],
    ]
    tmp = _write_csv(rows)
    try:
        result = parse_rent_roll(tmp)
        assert result["total_units"] == 3
        assert result["unit_cohorts"][0]["unit_count"] == 3
    finally:
        os.unlink(tmp)
