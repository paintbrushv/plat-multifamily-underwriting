"""Rent roll parser → unit_cohorts + physical_vacancy_rate.

Input: CSV or Excel with one row per unit.
Expected columns (case-insensitive):
  Unit, Type (or Beds/Baths), Sqft, Status, Monthly Rent, Market Rent, Lease End

Output dict:
  unit_cohorts: list of canonical unit_cohort dicts
  physical_vacancy_rate: float (0–1)
  total_units: int
  market_rent_by_cohort: dict mapping cohort_id → market_rent
  cohort_id_provenance: list of {raw_cohort_key, resolved_cohort_id,
                                 normalization_steps}
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Canonical column whitelist for _normalize_row.
#
# Each logical field maps to an ordered tuple of accepted header names.
# Resolution is exact-match (case-insensitive) FIRST across the entire list,
# then falls back to substring containment ONLY if no exact hit is found.
# Order within each tuple is the priority order for both exact and fallback.
# ---------------------------------------------------------------------------
_CANONICAL_COLUMNS: dict[str, tuple[str, ...]] = {
    "rent": (
        "InPlaceRent",
        "Lease Rent",
        "Charged Rent",
        "Monthly Rent",
        "Actual Rent",
        "Rent",
        "Current Rent",
    ),
    "market_rent": (
        "MktRent",
        "Market Rent",
        "Asking Rent",
        "Street Rent",
        "Market",
    ),
    "sqft": (
        "NetSF",
        "Net SF",
        "Square Feet",
        "SqFt",
        "Sq Ft",
        "Sq. Feet",
        "Size",
        "Square Footage",
        "Sq. Ft.",
    ),
    "status": (
        "OccStatus",
        "Occupancy Status",
        "Status",
        "Occupancy",
        "Occ",
    ),
    "unit_type": (
        # Floor-plan-style names first — they always carry the actual
        # unit-type label. "UnitType" / "Type" alone are intentionally
        # LAST because RedIQ-style exports use them for the
        # Residential/Commercial classification, not the floor plan.
        "PlanID",
        "Floor Plan",
        "Floorplan",
        "Plan",
        "Unit Type",
        "UnitType",
        "Type",
    ),
    "beds": (
        "Beds",
        "Bedrooms",
        "Bed",
        "BR",
    ),
    "baths": (
        "Baths",
        "Bathrooms",
        "Bath",
        "BA",
    ),
    "move_in": (
        "Move In",
        "Move-In",
        "Move In Date",
    ),
    "lease_start": (
        "Lease Start",
        "Lease Start Date",
    ),
    "lease_end": (
        "Lease End",
        "Lease End Date",
        "Lease Expiration",
    ),
    "resident": (
        "Resident",
        "Tenant",
        "Resident Name",
    ),
    "unit": (
        "UnitID",
        "Unit Number",
        "Unit #",
        "Unit No.",
        "Unit",
        "Apt",
    ),
}


def parse_rent_roll(path: str | Path) -> dict[str, Any]:
    """Parse a rent roll file into canonical unit_cohorts format.

    Args:
        path: CSV or Excel rent roll. Expected columns (case-insensitive):
              Unit, Type (or Bed/Bath), Sqft, Beds, Baths, Status,
              Monthly Rent (or Rent), Market Rent, Lease End

    Returns:
        Dict with:
          - unit_cohorts: list of canonical unit_cohort dicts
              (each carries cohort_id, unit_type, unit_count,
              initial_inplace_rent, sqft, bedrooms (int), bathrooms (float))
          - physical_vacancy_rate: float
          - total_units: int
          - market_rent_by_cohort: {cohort_id: market_rent}
          - cohort_id_provenance: list of {raw_cohort_key,
              resolved_cohort_id, normalization_steps} entries documenting
              how raw cohort labels were normalized into cohort_ids
    """
    path = Path(path)
    rows = _load_rows(path)

    if not rows:
        return {
            "unit_cohorts": [],
            "physical_vacancy_rate": 0.0,
            "total_units": 0,
            "market_rent_by_cohort": {},
            "cohort_id_provenance": [],
        }

    # legacy-PMS export fix — when no explicit Beds column was resolvable,
    # try extracting beds from the unit-type code on each row. If the
    # extraction fails on >20% of rows AND no rows have explicit beds,
    # emit a blocker so the caller can route to manual review instead
    # of cohort-collapsing every unit to "0BR".
    blockers: list[str] = []
    rows_with_beds_col = sum(1 for r in rows if r.get("beds") not in (None, 0))
    if rows_with_beds_col == 0:
        sidecar_bed_bath_map = _load_sidecar_bed_bath_map(path, rows)
        # Try extracting beds from each row's unit_type code. We only
        # populate `r["beds"]` if the cohort_key regex inside
        # `_resolve_beds_baths` would otherwise miss (i.e. the unit_type
        # is a code like 'blm-3dlx' rather than '1BR/1BA').
        unrecognized = 0
        for r in rows:
            unit_type = r.get("unit_type", "")
            # Skip if the existing bed+bath regex would handle it.
            if _has_bed_bath_pattern(unit_type):
                continue
            sidecar_match = sidecar_bed_bath_map.get(str(unit_type).strip().lower())
            if sidecar_match:
                r["beds"] = sidecar_match["beds"]
                r["baths"] = sidecar_match["baths"]
                continue
            inferred_bed_bath = _bed_bath_from_unit_type_code(unit_type)
            inferred = inferred_bed_bath[0] if inferred_bed_bath else None
            if inferred is None:
                unrecognized += 1
            else:
                r["beds"] = inferred
                if inferred_bed_bath:
                    r["baths"] = inferred_bed_bath[1]
        if rows and unrecognized / len(rows) > 0.20:
            blockers.append("unit_type_codes_unrecognized")

    # Group units by cohort key (e.g. "1BR/1BA")
    by_type: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        key = _cohort_key(row)
        by_type[key].append(row)

    unit_cohorts: list[dict] = []
    market_rent_by_cohort: dict[str, float] = {}
    cohort_id_provenance: list[dict] = []

    for cohort_key, units in by_type.items():
        occupied = [u for u in units if _is_occupied(u)]
        avg_inplace = (
            sum(u["rent"] for u in occupied) / len(occupied)
            if occupied else 0.0
        )
        # V1.2 — target_monthly_rent default. Average the cohort's
        # in-place market rent across ALL units (not just occupied) so
        # vacant units that carry an asking-market rent are included.
        # Units missing market_rent (zeroed) are also factored in: this
        # mirrors what the rent roll says, and the analyst can override.
        market_values = [
            float(u.get("market_rent") or 0.0)
            for u in units
        ]
        nonzero_market = [m for m in market_values if m > 0]
        avg_market = (
            sum(market_values) / len(market_values)
            if market_values else 0.0
        )
        # target_monthly_rent uses the non-zero average so vacant units
        # with $0 market_rent placeholders don't pull the default down.
        if nonzero_market:
            target_monthly_rent: float | None = round(
                sum(nonzero_market) / len(nonzero_market), 2
            )
        else:
            # Fallback — no market data at all for this cohort. Try
            # in-place rent instead so the analyst gets *some* default.
            target_monthly_rent = (
                round(avg_inplace, 2) if avg_inplace else None
            )
        sample = units[0]

        cohort_id, normalization_steps = _normalize_cohort_id(cohort_key)
        cohort_id_provenance.append({
            "raw_cohort_key": cohort_key,
            "resolved_cohort_id": cohort_id,
            "normalization_steps": normalization_steps,
        })

        bedrooms, bathrooms = _resolve_beds_baths(sample, cohort_key)

        cohort_dict: dict[str, Any] = {
            "cohort_id": cohort_id,
            "unit_type": cohort_key,
            "unit_count": len(units),
            "initial_inplace_rent": round(avg_inplace, 2) if avg_inplace else round(avg_market, 2),
            "sqft": sample.get("sqft", 0.0),
            "bedrooms": bedrooms,
            "bathrooms": bathrooms,
        }
        if target_monthly_rent is not None:
            cohort_dict["target_monthly_rent"] = target_monthly_rent
            cohort_dict["target_monthly_rent_source"] = "rent_roll_avg"
        else:
            cohort_dict["target_monthly_rent"] = None
            cohort_dict["target_monthly_rent_source"] = "missing"
            blockers.append(f"target_monthly_rent_missing_for_cohort_{cohort_id}")
        unit_cohorts.append(cohort_dict)
        # Export the same non-zero market-rent average used by
        # target_monthly_rent.  Vacant rows often carry blank/$0 market-rent
        # placeholders; including those zeros dilutes the engine's
        # market_rent_curve even though the cohort's supported asking rent is
        # visible on occupied / priced units.
        market_rent_by_cohort[cohort_id] = (
            target_monthly_rent if target_monthly_rent is not None else 0.0
        )

    # Uniqueness invariant — Bug 1.3 fix.
    seen: dict[str, list[str]] = defaultdict(list)
    for entry in cohort_id_provenance:
        seen[entry["resolved_cohort_id"]].append(entry["raw_cohort_key"])
    duplicates = {cid: keys for cid, keys in seen.items() if len(keys) > 1}
    assert not duplicates, (
        f"Duplicate cohort_ids produced by rent_roll_parser: {duplicates!r}. "
        "Two raw cohort keys collapsed to the same cohort_id after "
        "lowercasing + non-alphanumeric collapse. Distinct units must yield "
        "distinct cohort_ids — rename the source labels or extend the "
        "normalizer."
    )

    total = sum(len(v) for v in by_type.values())
    vacant = sum(1 for row in rows if not _is_occupied(row))
    vacancy_rate = vacant / total if total > 0 else 0.0

    result = {
        "unit_cohorts": unit_cohorts,
        "physical_vacancy_rate": round(vacancy_rate, 4),
        "total_units": total,
        "market_rent_by_cohort": market_rent_by_cohort,
        "cohort_id_provenance": cohort_id_provenance,
    }
    if blockers:
        result["blockers"] = blockers
    return result


# ---------------------------------------------------------------------------
# Unit-type-code → beds inference (legacy-PMS export fix).
#
# Some property-management exports lack an explicit Beds column and instead
# encode bed/bath info in the unit-type code, e.g. `blm-1dlx`, `blm-2dlx`,
# `blm-2std`, `blm-3bdr`, `blm-3dlx`. Pattern set covers:
#   - studio / efficiency  → 0
#   - <N>br / <N>bd / <N>bdr / <N>bed / <N>bedroom
#   - <N>dlx / <N>std / <N>bm  (dlx=deluxe, std=standard, bm=bedroom)
#   - <N>flat / <N>tnh  (flat / townhouse — sometimes seen on lofts)
# Returns None when no pattern matches.
# ---------------------------------------------------------------------------
_UNIT_TYPE_CODE_PATTERN = re.compile(
    r"(?:^|[^a-z0-9])"
    r"(\d+)\s*"
    r"(dlx|std|bm|br|bd|bdr|bed(?:room)?s?|flat|tnh)"
    r"(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)
_STUDIO_PATTERN = re.compile(r"\b(studio|efficiency|eff|stu)\b", re.IGNORECASE)
_LETTER_FLOORPLAN_BEDS = {"A": 1, "B": 2, "C": 3, "D": 4}
_CODE_BED_BATH_PATTERN = re.compile(
    r"(?:^|[^a-z0-9])(?:[a-z]+-)?[a-z](\d+)[\s_-]+(\d+)x(\d+(?:\.\d+)?)"
    r"(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)


_BED_BATH_PATTERN = re.compile(
    r"(\d+)\s*(?:BR|Bed(?:room)?)s?\b.*?(\d+(?:\.\d+)?)\s*(?:BA|Bath(?:room)?)s?\b",
    re.IGNORECASE,
)


def _has_bed_bath_pattern(unit_type: str) -> bool:
    """True if the unit_type already encodes both beds AND baths via
    the conventional `<N>BR/<N>BA` pattern."""
    return bool(unit_type and _BED_BATH_PATTERN.search(str(unit_type)))


def _beds_from_unit_type_code(unit_type: str) -> int | None:
    """Extract bed count from a unit-type code. Returns None if unparseable."""
    if not unit_type:
        return None
    s = str(unit_type).strip()
    if not s:
        return None
    if _STUDIO_PATTERN.search(s):
        return 0
    code_bed_bath = _CODE_BED_BATH_PATTERN.search(s)
    if code_bed_bath:
        return int(code_bed_bath.group(2))
    m = _UNIT_TYPE_CODE_PATTERN.search(s)
    if m:
        return int(m.group(1))
    simple_letter_code = re.match(r"^([A-D])(?:\d+)?[A-Z]*$", s)
    if simple_letter_code:
        return _LETTER_FLOORPLAN_BEDS[simple_letter_code.group(1).upper()]
    # Fall back to the existing cohort-key regex (handles "1BR/1BA",
    # "2 Bedroom" formats already covered by _resolve_beds_baths).
    bed_only = re.search(r"(\d+)\s*(?:BR|Bed)", s, re.IGNORECASE)
    if bed_only:
        return int(bed_only.group(1))
    return None


def _bed_bath_from_unit_type_code(unit_type: str) -> tuple[int, float] | None:
    if not unit_type:
        return None
    m = _CODE_BED_BATH_PATTERN.search(str(unit_type).strip())
    if m:
        return int(m.group(2)), float(m.group(3))
    beds = _beds_from_unit_type_code(unit_type)
    if beds is None:
        return None
    return beds, 1.0 if beds <= 1 else 2.0


def _normalize_cohort_id(cohort_key: str) -> tuple[str, list[str]]:
    """Normalize a raw cohort key into a stable cohort_id.

    Steps (in order, recorded for provenance):
      1. Strip slashes ("/"). "1BR/1BA" → "1BR1BA".
      2. Replace spaces with "_". "Beal Renovated" → "Beal_Renovated".
      3. Lowercase. "Beal_Renovated" → "beal_renovated".
      4. Collapse remaining non-alphanumeric (keeping "_") to "".
         "1br1ba!" → "1br1ba".

    "Beal" and "BEAL" both → "beal" (Bug 1.3 fix).
    """
    steps: list[str] = []
    s = cohort_key
    s = s.replace("/", "")
    steps.append(f"strip_slashes -> {s!r}")
    s = s.replace(" ", "_")
    steps.append(f"spaces_to_underscore -> {s!r}")
    s = s.lower()
    steps.append(f"lowercase -> {s!r}")
    s = re.sub(r"[^a-z0-9_]", "", s)
    steps.append(f"collapse_nonalnum -> {s!r}")
    return s, steps


def _resolve_beds_baths(sample: dict, cohort_key: str) -> tuple[int, float]:
    """Return (bedrooms: int, bathrooms: float) for a cohort.

    Strategy:
      1. Trust explicit Beds/Baths columns from the rent roll if both are
         non-zero / present.
      2. Otherwise, infer from the cohort_key string using a regex that
         tolerates "1BR/1BA", "1 BR / 1.5 BA", "2 Bedroom / 2 Bath", etc.
      3. If neither yields a value, fall back to (0, 1.0) — the historical
         default — but this branch is only reached for malformed inputs.
    """
    # Path 1 — explicit columns.
    beds = sample.get("beds")
    baths = sample.get("baths")
    if beds is not None and beds != 0:
        return int(beds), float(baths) if baths is not None else 1.0

    code_bed_bath = _CODE_BED_BATH_PATTERN.search(str(cohort_key))
    if code_bed_bath:
        return int(code_bed_bath.group(2)), float(code_bed_bath.group(3))

    # Path 2 — regex over cohort_key. Matches "1BR/1BA", "1 BR / 1.5 BA",
    # "2 Bedroom / 2 Bath", etc. The bedroom/bath token may use BR/Bedroom
    # and BA/Bath/Bathroom interchangeably.
    pattern = re.compile(
        r"(\d+)\s*(?:BR|Bed(?:room)?)s?\b"  # bedrooms
        r".*?"
        r"(\d+(?:\.\d+)?)\s*(?:BA|Bath(?:room)?)s?\b",  # baths
        re.IGNORECASE,
    )
    m = pattern.search(cohort_key)
    if m:
        return int(m.group(1)), float(m.group(2))

    # Path 3 — bedroom-only ("Studio" → 0/1.0; "1BR" alone → 1/1.0).
    if cohort_key.strip().lower() in ("studio", "efficiency"):
        return 0, 1.0
    bed_only = re.search(r"(\d+)\s*(?:BR|Bed)", cohort_key, re.IGNORECASE)
    if bed_only:
        return int(bed_only.group(1)), 1.0

    # Last-resort fallback — preserve historical default.
    return int(beds) if beds is not None else 0, float(baths) if baths is not None else 1.0


def _cohort_key(row: dict) -> str:
    """Generate a cohort key like '2BR/1BA' from a row dict."""
    unit_type = row.get("unit_type", "").strip()
    if unit_type:
        return unit_type
    beds_raw = row.get("beds")
    beds = int(beds_raw) if beds_raw not in (None, "") else 0
    baths = row.get("baths", 1.0)
    try:
        b = float(baths)
        baths_str = f"{int(b)}BA" if b == int(b) else f"{b}BA"
    except (ValueError, TypeError):
        baths_str = f"{baths}BA"
    return f"{beds}BR/{baths_str}"


def _is_occupied(row: dict) -> bool:
    status = str(row.get("status", "occupied")).strip().lower()
    if not status:
        return False
    vacant_markers = (
        "vacant",
        "vacancy",
        "unknown",
        "down",
        "offline",
        "model",
        "excluded",
    )
    return status != "0" and not any(marker in status for marker in vacant_markers)


def _load_rows(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return _load_csv(path)
    elif suffix in (".xlsx", ".xls", ".xlsm"):
        return _load_excel(path)
    raise ValueError(f"Unsupported file type: {suffix}")


def _load_csv(path: Path) -> list[dict[str, Any]]:
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            normalized = _normalize_row(dict(row))
            if _is_future_or_applicant_row(normalized):
                continue
            rows.append(normalized)
    return rows


def _load_excel(path: Path) -> list[dict[str, Any]]:
    """Load rent-roll rows from an Excel workbook.

    Sheet selection (a production deal / RedIQ multi-sheet fix):
      - If the workbook has multiple sheets, prefer one whose name looks
        like detail-level data ('Rent Roll', 'Unit Detail', 'Detail',
        'Units', 'Report1') over summary sheets ('Floor Plan', 'Summary',
        'About', 'Source Data', 'Sheet1', 'Sheet2', etc.).
      - If multiple candidates match, pick the one with the most data rows.
      - If no candidate matches by name, fall back to whichever sheet has
        the most rows (any sheet that looks tabular).

    Header detection (legacy-PMS multi-row header fix):
      - Scan the first 15 rows. A row counts as the header if it contains
        the most case-insensitive matches against canonical column names
        in `_CANONICAL_COLUMNS` (any tuple). Ties broken by earliest row.
      - This handles RedIQ's banner-then-section-then-header layout and
        PMS reports whose first few rows are property metadata.
    """
    if path.suffix.lower() == ".xls":
        all_rows = _load_legacy_xls_rows(path)
    else:
        try:
            import openpyxl
        except ImportError:
            raise ImportError("openpyxl required: pip install openpyxl")
        wb = openpyxl.load_workbook(path, data_only=True)
        try:
            ws = _select_sheet(wb)
            all_rows = list(ws.iter_rows(values_only=True))
        finally:
            wb.close()
    return _parse_excel_rows(all_rows)


def _load_legacy_xls_rows(path: Path) -> list[tuple[Any, ...]]:
    """Load rows from a legacy BIFF/OLE .xls workbook via pandas/xlrd."""
    try:
        import pandas as pd
    except ImportError as exc:
        raise ImportError("pandas required to read legacy .xls rent rolls") from exc
    try:
        sheets = pd.read_excel(
            path,
            sheet_name=None,
            header=None,
            dtype=object,
            engine="xlrd",
        )
    except ImportError as exc:
        raise ImportError("xlrd required to read legacy .xls rent rolls") from exc

    name, frame = _select_tabular_sheet(sheets)
    _ = name
    return [
        tuple(None if pd.isna(value) else value for value in row)
        for row in frame.itertuples(index=False, name=None)
    ]


def _select_tabular_sheet(sheets: dict[str, Any]) -> tuple[str, Any]:
    """Pick the most likely rent-roll sheet from a pandas sheet mapping."""
    if not sheets:
        raise ValueError("Excel workbook contains no sheets")
    if len(sheets) == 1:
        return next(iter(sheets.items()))

    def _name_matches(name: str, hints: tuple[str, ...]) -> bool:
        n = name.lower()
        return any(h in n for h in hints)

    detail_candidates = [
        (name, frame)
        for name, frame in sheets.items()
        if _name_matches(name, _DETAIL_SHEET_NAME_HINTS)
    ]
    if detail_candidates:
        detail_candidates.sort(key=lambda pair: -len(pair[1].index))
        return detail_candidates[0]

    non_summary = [
        (name, frame)
        for name, frame in sheets.items()
        if not _name_matches(name, _SUMMARY_SHEET_NAME_HINTS)
    ]
    if non_summary:
        non_summary.sort(key=lambda pair: -len(pair[1].index))
        return non_summary[0]

    return next(iter(sheets.items()))


def _parse_excel_rows(all_rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    header_idx, headers = _find_header_row(all_rows)
    has_unit_column = any(str(h).strip().lower() in {"unitid", "unit number", "unit #", "unit no.", "unit", "apt"} for h in headers)
    rows = []
    for i, row in enumerate(all_rows):
        if i <= header_idx:
            continue
        if not row:
            continue
        # First non-None cell test — many PMS reports have leading
        # blank columns or section banner rows below the header.
        if all(c is None or str(c).strip() == "" for c in row):
            continue
        if _is_detail_section_terminator(row):
            break
        row_dict = {
            headers[j]: v
            for j, v in enumerate(row)
            if j < len(headers) and headers[j]
        }
        # Some PMS exports place the actual unit id one column to the
        # left of the detected "Unit" header. When that happens the
        # header row is still useful for the rest of the columns, but
        # the unit id would otherwise be dropped because the blank first
        # header cell has no name. Recover the unit from column A when it
        # looks like a real unit identifier and the mapped Unit value is
        # empty.
        if (
            row
            and _looks_like_unit_id(row[0])
            and not _looks_like_unit_id(_get_unit_value(row_dict))
            and (row[2] not in (None, "") or row[4] not in (None, "") or row[5] not in (None, ""))
        ):
            row_dict["Unit"] = row[0]
        normalized = _normalize_row(row_dict)
        if _is_future_or_applicant_row(normalized):
            continue
        if _is_total_unit_row(row_dict, normalized):
            continue
        unit_val = _get_unit_value(row_dict)
        if has_unit_column and not _looks_like_unit_id(unit_val):
            continue
        # Summary lines sometimes carry big numeric totals but no unit
        # identifier and no unit-type code. Those are never unit rows.
        if not _row_looks_like_unit(row_dict) and not normalized.get("unit_type"):
            continue
        # Skip rows that have no usable data (e.g. lease-charge sub-rows
        # in Yardi/RealPage exports where unit + unit_type are blank).
        if not normalized.get("unit_type") and normalized.get("rent", 0) == 0:
            # Re-check using the raw dict — if the row has no Unit
            # value and no unit-type-ish value, drop it.
            if not _row_looks_like_unit(row_dict):
                continue
        # Skip section banners and summary rows — these typically
        # have a non-numeric Unit field ("Current/Notice/Vacant
        # Residents", "Total Vacant Units") and an empty Unit Type.
        if not normalized.get("unit_type"):
            if unit_val and not _looks_like_unit_id(unit_val):
                continue
        rows.append(normalized)
    return rows


def _is_future_or_applicant_row(normalized: dict[str, Any]) -> bool:
    """Drop future/applicant lease rows that duplicate current unit rows."""
    status = str(normalized.get("status") or "").strip().lower()
    return status in {"pending renewal", "pending resident", "applicant"}


def _is_detail_section_terminator(row: tuple[Any, ...]) -> bool:
    """True when a PMS rent-roll sheet has moved past unit detail rows."""
    labels = [
        str(c).strip().lower()
        for c in row[:4]
        if c is not None and str(c).strip()
    ]
    if not labels:
        return False
    terminators = {
        "status summary",
        "future resident details",
        "future residents",
        "charge code summary",
    }
    return any(label in terminators for label in labels)


def _is_total_unit_row(raw: dict, normalized: dict) -> bool:
    """Drop aggregate total rows that carry a unit-type-like label."""
    unit_type = str(normalized.get("unit_type") or "").strip().lower()
    if not unit_type:
        return False
    if not (unit_type.endswith("total:") or unit_type.startswith("total ")):
        return False
    unit_val = _get_unit_value(raw)
    return not unit_val or not _looks_like_unit_id(unit_val)


# Sheet-selection heuristic (multi-sheet RedIQ rent rolls).
_DETAIL_SHEET_NAME_HINTS = (
    "rent roll",
    "unit detail",
    "unit details",
    "details",
    "detail",
    "units",
    "report1",
    "report",
)
_SUMMARY_SHEET_NAME_HINTS = (
    "floor plan",
    "floorplan",
    "summary",
    "about",
    "source data",
    "cover",
    "instructions",
    "notes",
)


def _select_sheet(wb: Any) -> Any:
    """Pick the sheet most likely to contain unit-level rent roll rows.

    Rule:
      1. If a sheet name matches a `_DETAIL_SHEET_NAME_HINTS` token
         (case-insensitive substring), it's a candidate.
      2. Among candidates, pick the one with the most rows. Ties: first
         in workbook order.
      3. If no candidate matches by name, exclude sheets matching
         `_SUMMARY_SHEET_NAME_HINTS` and pick the largest of what remains.
      4. Last resort: workbook's active sheet.
    """
    sheets = list(wb.worksheets)
    if len(sheets) == 1:
        return sheets[0]

    def _name_matches(name: str, hints: tuple[str, ...]) -> bool:
        n = name.lower()
        return any(h in n for h in hints)

    detail_candidates = [s for s in sheets if _name_matches(s.title, _DETAIL_SHEET_NAME_HINTS)]
    if detail_candidates:
        detail_candidates.sort(key=lambda s: -(s.max_row or 0))
        return detail_candidates[0]

    non_summary = [s for s in sheets if not _name_matches(s.title, _SUMMARY_SHEET_NAME_HINTS)]
    if non_summary:
        non_summary.sort(key=lambda s: -(s.max_row or 0))
        return non_summary[0]

    return wb.active


def _find_header_row(all_rows: list[tuple]) -> tuple[int, list[str]]:
    """Locate the header row by scanning the first 15 rows.

    Scores each row by how many cells (case-insensitive, stripped) match
    a known canonical column name from `_CANONICAL_COLUMNS`. Returns the
    row index with the highest score (tie-break: earliest row); falls
    back to row 0 if no row scores > 0.
    """
    canonical_lookup: set[str] = set()
    for cands in _CANONICAL_COLUMNS.values():
        for c in cands:
            canonical_lookup.add(c.strip().lower())

    best_idx = 0
    best_score = 0
    scan_limit = min(len(all_rows), 15)
    for i in range(scan_limit):
        row = all_rows[i]
        if not row:
            continue
        # Score = number of DISTINCT canonical headers matched. Counting
        # distinct prevents rows with many duplicate cells (e.g. RedIQ's
        # row 7 "Status, Status, Rent, Rent, Rent...") from beating a
        # cleaner row whose every cell is a unique machine-readable header
        # (e.g. RedIQ's row 8 'UnitID, PlanID, NetSF, UnitType, MktRent').
        matched: set[str] = set()
        for cell in row:
            if cell is None:
                continue
            token = str(cell).strip().lower()
            if token and token in canonical_lookup:
                matched.add(token)
        score = len(matched)
        if score > best_score:
            best_score = score
            best_idx = i

    if best_score == 0:
        # Fall back to row 0 — preserves legacy single-row-header behavior.
        header_row = all_rows[0] if all_rows else ()
        effective_idx = 0
    else:
        header_row = all_rows[best_idx]
        effective_idx = best_idx

    # Multi-row header merge — many PMS exports (RealPage / Yardi) split
    # column labels across two rows: row N has 'Market', 'Move In',
    # 'Lease', and row N+1 has 'Rent', '', 'Expiration'. Concatenate
    # row N+1's non-null cells onto row N's labels so we recover headers
    # like 'Market Rent', 'Lease Expiration'. We only merge when the
    # next row looks like a continuation (no Unit-like first cell, mostly
    # short text labels rather than numeric data).
    if effective_idx > 0:
        prev_row = all_rows[effective_idx - 1]
        if prev_row and _is_continuation_header(prev_row):
            merged = []
            for j in range(max(len(prev_row), len(header_row))):
                top = prev_row[j] if j < len(prev_row) else None
                bot = header_row[j] if j < len(header_row) else None
                top_s = str(top).strip() if top else ""
                bot_s = str(bot).strip() if bot else ""
                if top_s and bot_s:
                    merged.append(f"{top_s} {bot_s}")
                elif bot_s:
                    merged.append(bot_s)
                elif top_s:
                    merged.append(top_s)
                else:
                    merged.append("")
            return effective_idx, merged

    if effective_idx + 1 < len(all_rows):
        next_row = all_rows[effective_idx + 1]
        if next_row and _is_continuation_header(next_row):
            merged: list[str] = []
            for j in range(max(len(header_row), len(next_row))):
                top = header_row[j] if j < len(header_row) else None
                bot = next_row[j] if j < len(next_row) else None
                top_s = str(top).strip() if top else ""
                bot_s = str(bot).strip() if bot else ""
                if top_s and bot_s:
                    merged.append(f"{top_s} {bot_s}")
                elif top_s:
                    merged.append(top_s)
                elif bot_s:
                    merged.append(bot_s)
                else:
                    merged.append("")
            # Skip past the continuation row when iterating data.
            return effective_idx + 1, merged

    headers = [str(h).strip() if h else "" for h in header_row]
    return effective_idx, headers


def _is_continuation_header(row: tuple) -> bool:
    """True if `row` looks like a wrapped second line of a header.

    Heuristic: all cells are either empty or short text strings (no
    numbers, no dates). We treat <= 30-char strings with no digits as
    label-like.
    """
    has_label = False
    for cell in row:
        if cell is None or str(cell).strip() == "":
            continue
        s = str(cell).strip()
        # Numbers, dates, longer free text → not a header continuation.
        if any(ch.isdigit() for ch in s):
            return False
        if len(s) > 30:
            return False
        has_label = True
    return has_label


def _row_looks_like_unit(row_dict: dict) -> bool:
    """True if any of the unit-identifying cells carry data."""
    for key in ("Unit", "Unit No.", "Unit #", "Apt", "UnitID"):
        for k in row_dict:
            if str(k).strip().lower() == key.lower() and row_dict[k] not in (None, ""):
                return True
    return False


def _get_unit_value(row_dict: dict) -> Any:
    """Return the value from the first Unit-like column found, or None."""
    for key in ("Unit", "Unit No.", "Unit #", "Apt", "UnitID"):
        for k in row_dict:
            if str(k).strip().lower() == key.lower():
                return row_dict[k]
    return None


def _looks_like_unit_id(value: Any) -> bool:
    """True if the value looks like a unit identifier (numeric or alphanumeric
    code), False if it's clearly a section banner / summary text.

    Real unit IDs: '101', '0111', 'A-12', '5B'. Banners: 'Current/Notice/Vacant
    Residents', 'Total Vacant Units', 'Summary Groups'. We accept anything
    with at least one digit and length <= 12.
    """
    if value is None:
        return False
    s = str(value).strip()
    if not s or len(s) > 12:
        return False
    return any(ch.isdigit() for ch in s)


def _resolve_column(raw: dict, candidates: tuple[str, ...]) -> Any:
    """Resolve one logical field against a row dict.

    Bug 1.2 fix: exact-match (case-insensitive) wins; fall back to substring
    containment only if zero exact hits exist. Iteration order of candidates
    is the priority order.
    """
    if not raw:
        return None
    # Build a case-folded lookup of the source columns once.
    folded = {str(col).strip().lower(): col for col in raw.keys() if col is not None}

    # Pass 1 — exact match (case-insensitive).
    for cand in candidates:
        target = cand.strip().lower()
        if target in folded:
            return raw[folded[target]]

    # Pass 2 — substring fallback. First candidate to match any source
    # column wins; among matches, prefer the source column whose lowercase
    # name is shortest (closer to the canonical header).
    #
    # Bug fix: short (<= 3 char) candidates like "BA", "BR", "Occ" are
    # NOT eligible for substring fallback — they produce false matches
    # against unrelated columns ("Balance" matches "ba", "Other" matches
    # "br"). Require at least 4 chars for substring fallback OR require
    # word-boundary match for short candidates.
    for cand in candidates:
        target = cand.strip().lower()
        if len(target) <= 3:
            # Word-boundary match only — avoids false-positives like
            # "BA" matching "Balance".
            matches = []
            for fold, col_name in folded.items():
                if re.search(rf"\b{re.escape(target)}\b", fold):
                    matches.append(col_name)
        else:
            matches = [col_name for fold, col_name in folded.items() if target in fold]
        if matches:
            matches.sort(key=lambda c: len(c))
            return raw[matches[0]]

    return None


def _normalize_row(raw: dict) -> dict:
    """Normalize column names to consistent internal keys.

    Uses _resolve_column (exact-match-first whitelist, substring fallback) —
    Bug 1.2 fix.
    """
    rent_val = _resolve_column(raw, _CANONICAL_COLUMNS["rent"])
    market_val = _resolve_column(raw, _CANONICAL_COLUMNS["market_rent"])
    sqft_val = _resolve_column(raw, _CANONICAL_COLUMNS["sqft"])
    status_val = _resolve_column(raw, _CANONICAL_COLUMNS["status"])
    unit_type_val = _resolve_column(raw, _CANONICAL_COLUMNS["unit_type"])
    beds_val = _resolve_column(raw, _CANONICAL_COLUMNS["beds"])
    baths_val = _resolve_column(raw, _CANONICAL_COLUMNS["baths"])

    # _to_int returns None when the input is missing OR has no digits — keep
    # that distinction so _resolve_beds_baths can fall back to the regex on
    # the cohort_key. _to_int still RAISES on "studio"/non-numeric strings
    # only when explicitly asked (see _to_int docstring).
    beds_int: int | None
    if beds_val is None or str(beds_val).strip() == "":
        beds_int = None
    else:
        beds_int = _to_int(beds_val, allow_no_digit=True)

    status = "occupied" if status_val is None else str(status_val).strip()
    if status == "":
        status = "unknown"

    return {
        "rent": _to_float(rent_val) or 0.0,
        "market_rent": _to_float(market_val) or _to_float(rent_val) or 0.0,
        "sqft": _to_float(sqft_val) or 0.0,
        "status": status,
        "unit_type": str(unit_type_val or "").strip(),
        "beds": beds_int,
        "baths": _to_float(baths_val) or 1.0,
    }


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return abs(float(str(v).replace(",", "").replace("$", "").strip()))
    except (ValueError, TypeError):
        return None


def _to_int(v: Any, allow_no_digit: bool = False) -> int:
    """Extract a leading integer from a string-ish value.

    Bug 1.13 fix: replaces brittle ``str(v).replace("BR", "")`` with a regex
    that finds the first run of digits, so "1 Bedroom", "1bed", "1BR",
    "1 BR/1BA" all return 1. Raises ValueError when no digit is found
    (e.g., "studio") UNLESS ``allow_no_digit=True``, in which case it
    returns 0 — provided for the column-resolution path where missing /
    non-numeric beds columns are common and recoverable via cohort_key
    regex inference downstream.
    """
    if v is None:
        if allow_no_digit:
            return 0
        raise ValueError("_to_int: received None with no fallback")
    s = str(v).strip()
    m = re.search(r"\d+", s)
    if not m:
        if allow_no_digit:
            return 0
        raise ValueError(
            f"_to_int: no digit found in {v!r} — cannot coerce to int. "
            "If this is a 'studio' label, the caller must handle it before "
            "passing to _to_int (e.g., via cohort_key regex inference)."
        )
    return int(m.group(0))


def _load_sidecar_bed_bath_map(
    rent_roll_path: Path,
    rows: list[dict[str, Any]],
) -> dict[str, dict[str, float | int]]:
    """Best-effort bed/bath lookup from sibling Excel exports like Box Score.

    Some PMS rent rolls only expose opaque unit-type codes (`bc_Q1`, `bc_B2`)
    while a sibling workbook carries the descriptive floor-plan label
    (`2X1 860 sqft`, `2x2.5 1098 sqft`). When present, use the sidecar so
    cohorts keep their original codes but gain correct bedrooms/bathrooms.
    """
    codes = {
        str(row.get("unit_type") or "").strip()
        for row in rows
        if str(row.get("unit_type") or "").strip()
    }
    if not codes:
        return {}

    rr_stem = rent_roll_path.stem.lower()
    raw_siblings = [
        p for p in rent_roll_path.parent.iterdir()
        if p != rent_roll_path
        and p.stem.lower() != rr_stem  # skip xls/xlsm twin of the rent roll itself
        and p.suffix.lower() in (".xlsx", ".xls", ".xlsm")
    ]
    # Deduplicate by stem: prefer .xlsx > .xlsm > .xls (old binary format)
    _suffix_priority = {".xlsx": 0, ".xlsm": 1, ".xls": 2}
    by_stem: dict[str, Path] = {}
    for p in raw_siblings:
        key = p.stem.lower()
        existing = by_stem.get(key)
        if existing is None or _suffix_priority.get(p.suffix.lower(), 9) < _suffix_priority.get(existing.suffix.lower(), 9):
            by_stem[key] = p
    sibling_excels = list(by_stem.values())
    preferred = [
        p for p in sibling_excels
        if "box score" in p.name.lower() or "boxscore" in p.name.lower()
    ]
    ordered_candidates = preferred + [p for p in sibling_excels if p not in preferred]

    merged: dict[str, dict[str, float | int]] = {}
    unresolved = {code.lower() for code in codes}
    for candidate in ordered_candidates:
        extracted = _extract_sidecar_bed_bath_map(candidate, unresolved)
        for code, payload in extracted.items():
            merged.setdefault(code, payload)
        unresolved -= set(extracted.keys())
        if not unresolved:
            break
    return merged


def _extract_sidecar_bed_bath_map(
    workbook_path: Path,
    target_codes: set[str],
) -> dict[str, dict[str, float | int]]:
    if not target_codes:
        return {}
    # openpyxl cannot read legacy .xls (OLE2) format — skip those silently.
    if workbook_path.suffix.lower() == '.xls':
        return {}
    try:
        import openpyxl
    except ImportError:
        return {}

    extracted: dict[str, dict[str, float | int]] = {}
    wb = openpyxl.load_workbook(workbook_path, data_only=True, read_only=True)
    try:
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                cells = [
                    str(cell).strip()
                    for cell in row
                    if cell is not None and str(cell).strip()
                ]
                if not cells:
                    continue
                lowered = [cell.lower() for cell in cells]
                row_text = " | ".join(cells)
                for code in list(target_codes):
                    if code not in lowered:
                        continue
                    parsed = _extract_beds_baths_from_text(row_text)
                    if parsed is None:
                        continue
                    beds, baths = parsed
                    extracted[code] = {"beds": beds, "baths": baths}
    finally:
        wb.close()
    return extracted


def _extract_beds_baths_from_text(text: str) -> tuple[int, float] | None:
    if not text:
        return None
    m = _BED_BATH_PATTERN.search(text)
    if m:
        return int(m.group(1)), float(m.group(2))
    plan_match = re.search(r"(\d+)\s*[xX]\s*(\d+(?:\.\d+)?)", text)
    if plan_match:
        return int(plan_match.group(1)), float(plan_match.group(2))
    return None
