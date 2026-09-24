"""T12 (Trailing 12 Months) income statement parser.

Converts a T12 Excel or CSV into the canonical opex_table format.

Input: Excel/CSV with rows = expense categories, columns = months
Output: list of opex_table dicts matching canonical schema v0.1

Category name normalization maps common T12 label variants to
canonical category names and sets the recoverable flag. Pattern
matching prefers the earliest match position in the raw label,
with longest pattern as a tiebreaker. This ensures compound
labels like "Security Contract Services" resolve to the leading
specific category ("Security") rather than to a later generic
substring ("Contract Services"). See Bug 1.11 in stage_1_ingest.md.

Sign convention (Bug 1.7): T12 expense rows are positive; revenue
or credit rows (e.g. "Bad Debt Recovery", reimbursement offsets)
are negative. We preserve sign — we do NOT abs() values, because
unconditional abs() flips legitimate credit lines into inflated
expenses. Parens-as-negative formatting (e.g. "(500)") is still
parsed as -500.0.

Recoverable-flag merge (Bug 1.6): when multiple raw rows map to
the same canonical category, the recoverable flag uses OR
semantics — if any source row is flagged recoverable, the merged
category is recoverable. Each canonical category also accumulates
a `source_rows` list of the raw category names that fed into it,
so analysts can audit which T12 lines were merged. The provenance
mapping is exposed via `parse_t12_with_provenance()` because the
canonical `opex_table` schema (`additionalProperties: false`) does
not permit extra fields on opex entries — `parse_t12()` continues
to return schema-clean dicts.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any


# (pattern substring, canonical_name, recoverable)
# Order in this source list is for human readability; runtime
# matching uses _SORTED_CATEGORY_MAP (longest pattern first) so
# that compound labels resolve to the most specific category.
_CATEGORY_MAP: list[tuple[str, str, bool]] = [
    ("payroll", "Payroll", False),
    ("personnel", "Payroll", False),
    ("direct labor", "Payroll", False),
    ("indirect labor", "Payroll", False),
    ("manager salary", "Payroll", False),
    ("leasing salaries", "Payroll", False),
    ("leasing salary", "Payroll", False),
    ("maintenance wages", "Payroll", False),
    ("grounds wages", "Payroll", False),
    ("regional management wages", "Payroll", False),
    ("resident experience wages", "Payroll", False),
    ("incentive wages", "Payroll", False),
    ("salary", "Payroll", False),
    ("wages", "Payroll", False),
    ("employment tax", "Payroll", False),
    ("employee health", "Payroll", False),
    ("health insurance", "Payroll", False),
    ("group insurance", "Payroll", False),
    ("worker's compensation", "Payroll", False),
    ("workers compensation", "Payroll", False),
    ("401k", "Payroll", False),
    ("repair", "Repairs & Maintenance", False),
    ("maintenance", "Repairs & Maintenance", False),
    ("contract serv", "Contract Services", False),
    ("security", "Security", False),
    ("turnover", "Turnover / Make-Ready", False),
    ("make-ready", "Turnover / Make-Ready", False),
    ("landscap", "Landscaping / Grounds", False),
    ("grounds", "Landscaping / Grounds", False),
    ("marketing", "Marketing / Advertising", False),
    ("advertis", "Marketing / Advertising", False),
    ("leasing", "Marketing / Advertising", False),
    ("admin", "Administrative", False),
    ("water", "Water & Sewer", True),
    ("sewer", "Water & Sewer", True),
    ("electric", "Electricity", True),
    ("gas", "Fuel (Gas & Oil)", True),
    ("fuel", "Fuel (Gas & Oil)", True),
    ("insurance", "Insurance", False),
    ("taxes - real estate", "Real Estate Taxes", False),
    ("real estate tax", "Real Estate Taxes", False),
    ("re tax", "Real Estate Taxes", False),
    ("property tax", "Real Estate Taxes", False),
    ("management", "Property Management Fee", False),
    ("property management", "Property Management Fee", False),
    ("managment", "Property Management Fee", False),
    ("mgmt fee", "Property Management Fee", False),
    ("management fee", "Property Management Fee", False),
    ("trash", "Trash", True),
    ("waste", "Trash", True),
    ("other operat", "Other Operating Expenses", False),
    ("franchise", "Franchise Fee", False),
    ("reimburs", "Reimbursements", False),
    ("util", "Utilities", True),
]

# Pre-sorted view of _CATEGORY_MAP, longest pattern first. Used as
# a deterministic tiebreaker when two patterns match at the same
# position in the input. Computed once at module import; importing
# the module repeatedly does not re-sort _CATEGORY_MAP itself (it
# remains in human-readable order). See test_module_level_sort_idempotent.
_SORTED_CATEGORY_MAP: list[tuple[str, str, bool]] = sorted(
    _CATEGORY_MAP, key=lambda t: len(t[0]), reverse=True
)

_LEADING_ACCOUNT_CODE_RE = re.compile(r"^\d+(?:\.\d+)?\s+")


@dataclass
class OpexGroup:
    """One hierarchical subsection of an OpEx statement."""

    subsection_header: str | None
    detail_rows: list[tuple[str, float]]
    subtotal_row: tuple[str, float] | None
    indent_level: int


@dataclass
class ParseWarning:
    """Emitted when a group cannot be mapped to a canonical category."""

    tier_reached: int
    raw_label: str
    amount: float
    category_assigned: str


_SUBTOTAL_CATEGORY_MAP: list[tuple[str, str]] = [
    ("total admin salaries", "Payroll"),
    ("total leasing salaries", "Payroll"),
    ("total maint salaries", "Payroll"),
    ("total maintenance salaries", "Payroll"),
    ("total salary & wages", "Payroll"),
    ("total salaries & wages", "Payroll"),
    ("total bonus", "Payroll"),
    ("total payroll taxes", "Payroll"),
    ("total other payroll", "Payroll"),
    ("total personnel", "Payroll"),
    ("total payroll", "Payroll"),
    ("office administration", "Administrative"),
    ("resident related", "Other Operating Expenses"),
    ("make ready", "Turnover / Make-Ready"),
    ("total utilities", "Utilities"),
    ("total maintenance", "Repairs & Maintenance"),
    ("total repair", "Repairs & Maintenance"),
    ("total repairs", "Repairs & Maintenance"),
    ("total administrative", "Administrative"),
    ("total marketing", "Marketing / Advertising"),
    ("total management", "Property Management Fee"),
    ("total contract", "Contract Services"),
    ("total insurance", "Insurance"),
    ("total professional", "Administrative"),
    ("total amenities", "Other Operating Expenses"),
    ("total landscaping", "Landscaping / Grounds"),
    ("total cleaning", "Other Operating Expenses"),
    ("total security", "Security"),
]

_SECTION_TOTAL_RE = re.compile(
    r"total\s+operating|"
    r"net\s+operating\s+income|"
    r"effective\s+gross|"
    r"total\s+revenues?$|"
    r"total\s+income$|"
    r"total\s+expenses?$|"
    r"net\s+income",
    re.IGNORECASE,
)


def _normalize_category(raw_name: str) -> tuple[str, bool]:
    """Map a raw T12 category label to canonical name and recoverable flag.

    Matching rule: of all patterns that appear as substrings of
    the (lowercased) raw name, pick the one whose match starts
    earliest in the string. Ties on starting position are broken
    by longest pattern. This makes "Security Contract Services"
    resolve to "Security" (matches at position 0) rather than to
    "Contract Services" (matches at position 9), which is the
    behavior the Bug 1.11 fix requires. The longest-pattern
    tiebreaker ensures compound matches at the same position
    resolve to the most specific category.
    """
    lower = _LEADING_ACCOUNT_CODE_RE.sub("", raw_name.strip().lower())
    best: tuple[int, int, str, int] | None = None  # (pos, -len, canonical, recoverable_rank)
    for pattern, canonical, recoverable in _SORTED_CATEGORY_MAP:
        pos = lower.find(pattern)
        if pos < 0:
            continue
        # On ties, prefer recoverable=True so a shared canonical that has any
        # recoverable source pattern keeps the recoverable flag.
        candidate = (pos, -len(pattern), canonical, 0 if recoverable else 1)
        if best is None or candidate < best:
            best = candidate
    if best is None:
        return raw_name.strip().title(), False
    _, _, canonical, recoverable_rank = best
    recoverable = recoverable_rank == 0
    return canonical, recoverable


def _normalize_subtotal_category(text: str) -> str | None:
    """Tier-1/2 lookup against subtotal-only category patterns."""
    lowered = text.strip().lower()
    for pattern, canonical in _SUBTOTAL_CATEGORY_MAP:
        if pattern in lowered:
            return canonical
    return None


def _is_subcategory_subtotal(text: str) -> bool:
    """True for subcategory subtotal rows such as 'Total Personnel Expense'."""
    stripped = text.strip()
    if not stripped.lower().startswith("total "):
        return False
    return not bool(_SECTION_TOTAL_RE.search(stripped))


def _matches_section_total(text: str) -> bool:
    """True if text is a section-level total or stop marker."""
    normalized = _normalize_statement_label(text)
    if normalized in {"OPERATING EXPENSES"}:
        return True
    return bool(_SECTION_TOTAL_RE.search(text.strip()))


def _text_indent_level(raw_text: str) -> int:
    """Indent level derived from leading spaces in cell text."""
    if not raw_text:
        return 0
    spaces = len(raw_text) - len(raw_text.lstrip(" "))
    return spaces // 2


def _matches_category_map(text: str) -> str | None:
    """Return the canonical _CATEGORY_MAP hit, or None on no match."""
    lower = _LEADING_ACCOUNT_CODE_RE.sub("", text.strip().lower())
    best: tuple[int, int, str, bool] | None = None
    for pattern, canonical, recoverable in _SORTED_CATEGORY_MAP:
        pos = lower.find(pattern)
        if pos < 0:
            continue
        candidate = (pos, -len(pattern), canonical, recoverable)
        if best is None or candidate < best:
            best = candidate
    if best is None:
        return None
    _, _, canonical, _ = best
    return canonical


def parse_t12(path: str | Path) -> list[dict[str, Any]]:
    """Parse a T12 income statement file into canonical opex_table format.

    Args:
        path: Path to CSV or Excel (.xlsx/.xls/.xlsm) T12 file.
              Expected format: rows = categories, columns = months.
              First column = category name, subsequent columns = monthly amounts.

    Returns:
        List of opex_table dicts for the canonical deal schema. Each dict has:
          - category_name: str
          - calculation_type: "fixed_annual"
          - base_value: float (annualized sum of all month columns; sign preserved)
          - recoverable_flag: bool (OR-merged across duplicate source rows)

    The canonical schema does not permit additional properties on
    opex entries; for source-row provenance use
    :func:`parse_t12_with_provenance`.
    """
    opex_table, _provenance = parse_t12_with_provenance(path)
    return opex_table


def parse_t12_with_provenance(
    path: str | Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Parse a T12 file and return (opex_table, provenance).

    Same parsing logic as :func:`parse_t12` but additionally returns
    a mapping of canonical category name → list of raw source-row
    labels that fed into it. Useful for audit trails when multiple
    T12 lines are merged into one canonical category (e.g. duplicate
    "Electricity - Common" / "Electricity - Vacant" rows).

    The opex_table return value is schema-clean (no extra fields)
    so it can be inserted directly into a canonical deal JSON.
    """
    path = Path(path)
    _warnings: list[ParseWarning] = []
    rows = _load_rows(path, _warnings)

    # Accumulate by canonical name (handles duplicate rows)
    totals: dict[str, float] = {}
    recoverables: dict[str, bool] = {}
    source_rows: dict[str, list[str]] = {}

    for category_raw, monthly_values in rows:
        if not category_raw or not monthly_values:
            continue
        canonical, recoverable = _normalize_category(category_raw)
        # Sign-preserving annualized total. We sum all non-None
        # monthly values regardless of sign so that legitimate
        # credit/refund rows offset (rather than inflate) expenses.
        annual_total = sum(v for v in monthly_values if v is not None)
        if annual_total == 0:
            continue
        totals[canonical] = totals.get(canonical, 0.0) + annual_total
        # OR semantics: any source flagged recoverable → recoverable.
        recoverables[canonical] = recoverables.get(canonical, False) or recoverable
        source_rows.setdefault(canonical, []).append(category_raw.strip())

    opex_table = [
        {
            "category_name": name,
            "calculation_type": "fixed_annual",
            "base_value": round(total, 2),
            "recoverable_flag": recoverables[name],
        }
        for name, total in totals.items()
    ]
    provenance: dict[str, Any] = {
        **source_rows,
        "_warnings": [
            {
                "tier_reached": warning.tier_reached,
                "raw_label": warning.raw_label,
                "amount": warning.amount,
                "category_assigned": warning.category_assigned,
            }
            for warning in _warnings
        ],
    }
    return opex_table, provenance


def _load_rows(
    path: Path,
    warnings: list[ParseWarning] | None = None,
) -> list[tuple[str, list[float | None]]]:
    """Load rows from CSV or Excel. Returns (category_name, [month_values...])."""
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return _load_csv(path, warnings)
    elif suffix in (".xlsx", ".xls", ".xlsm"):
        return _load_excel(path, warnings)
    elif suffix == ".pdf":
        return _load_pdf(path, warnings)
    else:
        raise ValueError(f"Unsupported file type: {suffix}. Use .csv, .xlsx, or text-based .pdf")


def _load_csv(
    path: Path,
    warnings: list[ParseWarning] | None = None,
) -> list[tuple[str, list[float | None]]]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        next(reader, None)  # skip header
        return _extract_statement_rows(reader, warnings)


def _load_excel(
    path: Path,
    warnings: list[ParseWarning] | None = None,
) -> list[tuple[str, list[float | None]]]:
    try:
        import openpyxl
    except ImportError:
        raise ImportError("openpyxl required for Excel parsing: pip install openpyxl")
    wb = openpyxl.load_workbook(path, data_only=True)
    try:
        ws = _select_statement_sheet(wb)
        return _extract_statement_rows(ws.iter_rows(values_only=True), warnings)
    finally:
        wb.close()


def _select_statement_sheet(workbook: Any) -> Any:
    """Pick the worksheet that most likely contains the T12 statement.

    Workbooks often open on a summary/dashboard tab. Scoring parsed opex rows
    prevents `wb.active` from silently returning an empty statement.
    """
    sheets = list(workbook.worksheets)
    if not sheets:
        raise ValueError("Excel workbook contains no sheets")
    if len(sheets) == 1:
        return sheets[0]

    def _name_score(title: str) -> int:
        normalized = title.lower()
        positive_hints = (
            "t12",
            "trailing",
            "p&l",
            "profit",
            "loss",
            "income",
            "statement",
            "recap",
        )
        negative_hints = ("summary", "dashboard", "cover", "index")
        score = sum(1 for hint in positive_hints if hint in normalized)
        score -= sum(1 for hint in negative_hints if hint in normalized)
        return score

    scored: list[tuple[int, int, int, Any]] = []
    for idx, sheet in enumerate(sheets):
        _ = idx
        probe_warnings: list[ParseWarning] = []
        parsed_rows = _extract_statement_rows(sheet.iter_rows(values_only=True), probe_warnings)
        non_empty_rows = sum(
            1
            for row in sheet.iter_rows(values_only=True)
            if row and any(cell not in (None, "") for cell in row)
        )
        scored.append((len(parsed_rows), _name_score(sheet.title), non_empty_rows, sheet))

    scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return scored[0][3]


def _load_pdf(
    path: Path,
    warnings: list[ParseWarning] | None = None,
) -> list[tuple[str, list[float | None]]]:
    text = _extract_pdf_layout_text(path)
    rows = [_parse_pdf_layout_line(line) for line in text.splitlines()]
    return _extract_statement_rows((row for row in rows if row), warnings)


def _extract_pdf_layout_text(path: Path) -> str:
    pdftotext = shutil.which("pdftotext")
    if not pdftotext:
        raise ValueError("Text-based PDF T12 parsing requires `pdftotext` on PATH")
    completed = subprocess.run(
        [pdftotext, "-layout", str(path), "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError(f"pdftotext failed for T12 PDF: {completed.stderr.strip()}")
    return completed.stdout


def _parse_pdf_layout_line(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped:
        return None
    parts = re.split(r"\s{2,}", stripped)
    if len(parts) == 1:
        return parts
    numeric_start = next(
        (idx for idx, part in enumerate(parts) if _to_float(part) is not None),
        None,
    )
    if numeric_start is None:
        return parts
    label = " ".join(parts[:numeric_start]).strip()
    values = parts[numeric_start:numeric_start + 12]
    return [label, *values]


def _detect_hierarchy(
    opex_rows: list[tuple[str, str, list[float | None]]],
) -> list[OpexGroup]:
    """Pass 1: group OpEx rows into hierarchy-aware subsection buckets."""

    def _annual_amount(values: list[float | None]) -> float:
        numeric_values = [value for value in values if value is not None]
        numeric_values = _strip_trailing_total_column(numeric_values)
        if len(numeric_values) > 12:
            numeric_values = numeric_values[:12]
        return sum(numeric_values)

    groups: list[OpexGroup] = []
    current = OpexGroup(
        subsection_header=None,
        detail_rows=[],
        subtotal_row=None,
        indent_level=0,
    )

    for stripped, raw, values in opex_rows:
        has_values = any(v is not None for v in values)
        normalized = _normalize_statement_label(stripped)

        if _matches_section_total(stripped):
            if current.detail_rows or current.subtotal_row:
                groups.append(current)
            break
        if normalized in {
            "TOTAL CONTROLLABLE EXPENSES",
            "TOTAL CONTROLLABLE OPERATING EXPENSES",
            "TOTAL NON-CONTROLLABLE EXPENSE",
            "TOTAL NON-CONTROLLABLE EXP",
        }:
            if current.detail_rows or current.subtotal_row:
                groups.append(current)
                current = OpexGroup(
                    subsection_header=None,
                    detail_rows=[],
                    subtotal_row=None,
                    indent_level=0,
                )
            continue

        if _is_subcategory_subtotal(stripped):
            amount = _annual_amount(values) if values else 0.0
            current.subtotal_row = (stripped, amount)
            groups.append(current)
            current = OpexGroup(
                subsection_header=None,
                detail_rows=[],
                subtotal_row=None,
                indent_level=0,
            )
        elif not has_values:
            if current.detail_rows or current.subtotal_row:
                groups.append(current)
            current = OpexGroup(
                subsection_header=stripped,
                detail_rows=[],
                subtotal_row=None,
                indent_level=_text_indent_level(raw),
            )
        else:
            if has_values and _is_summary_row(stripped):
                continue
            amount = _annual_amount(values)
            if amount == 0 and _matches_category_map(stripped):
                if current.detail_rows or current.subtotal_row:
                    groups.append(current)
                current = OpexGroup(
                    subsection_header=stripped,
                    detail_rows=[],
                    subtotal_row=None,
                    indent_level=_text_indent_level(raw),
                )
                continue
            if _is_section_subtotal_row(
                stripped,
                current.subsection_header,
                bool(current.detail_rows),
            ):
                continue
            current.detail_rows.append((stripped, amount))

    if current.detail_rows or current.subtotal_row:
        groups.append(current)

    subtotal_count = sum(1 for group in groups if group.subtotal_row is not None)
    if subtotal_count < 2:
        return []
    return groups


def _resolve_groups(
    groups: list[OpexGroup],
    warnings: list[ParseWarning],
) -> list[tuple[str, list[float | None]]]:
    """Pass 2: resolve OpexGroups to canonical one-row category totals."""
    rows: list[tuple[str, list[float | None]]] = []
    row_indent_levels: list[int] = []
    has_total_payroll = any(
        group.subtotal_row
        and _normalize_statement_label(group.subtotal_row[0]) == "TOTAL PAYROLL"
        for group in groups
    )

    def _duplicates_prior_child_suffix(group: OpexGroup, amount: float) -> bool:
        if group.detail_rows or group.subtotal_row is None or len(rows) < 2:
            return False
        suffix_total = 0.0
        prior_rows = zip(reversed(rows), reversed(row_indent_levels))
        for count, ((_, values), indent_level) in enumerate(prior_rows, start=1):
            if indent_level <= group.indent_level:
                break
            suffix_total += sum(value for value in values if value is not None)
            if count >= 2 and abs(suffix_total - amount) <= max(
                0.01, abs(amount) * 1e-9
            ):
                return True
        return False

    for group in groups:
        if group.subtotal_row is not None:
            amount = group.subtotal_row[1]
        elif group.detail_rows:
            amount = sum(detail_amount for _, detail_amount in group.detail_rows)
        else:
            continue
        if _duplicates_prior_child_suffix(group, amount):
            continue

        canonical: str | None = None
        if group.subtotal_row:
            canonical = _normalize_subtotal_category(group.subtotal_row[0])
        if (
            canonical is None
            and group.subtotal_row
            and not _matches_section_total(group.subtotal_row[0])
        ):
            canonical = _matches_category_map(group.subtotal_row[0])

        if canonical is None and group.subsection_header:
            canonical = _normalize_subtotal_category(group.subsection_header)

        if canonical is None and group.detail_rows:
            detail_matches = [
                (match, amount)
                for detail_label, amount in group.detail_rows
                if (match := _matches_category_map(detail_label)) is not None
            ]
            if len({match for match, _ in detail_matches}) > 1:
                for detail_label, amount in group.detail_rows:
                    match = _matches_category_map(detail_label)
                    rows.append((match or detail_label.strip().title(), [amount]))
                    row_indent_levels.append(group.indent_level)
                continue

        if canonical is None and group.subtotal_row and group.detail_rows:
            detail_matches = [
                (match, amount)
                for detail_label, amount in group.detail_rows
                if (match := _matches_category_map(detail_label)) is not None
            ]
            if len({match for match, _ in detail_matches}) > 1:
                for match, amount in detail_matches:
                    rows.append((match, [amount]))
                    row_indent_levels.append(group.indent_level)
                continue

        if canonical is None:
            for detail_label, _ in group.detail_rows:
                match = _matches_category_map(detail_label)
                if match is not None:
                    canonical = match
                    break

        if canonical is None:
            raw_label = (
                group.subtotal_row[0]
                if group.subtotal_row
                else group.subsection_header or "unknown"
            )
            if group.subtotal_row and not group.detail_rows:
                continue
            warnings.append(
                ParseWarning(
                    tier_reached=4,
                    raw_label=raw_label,
                    amount=amount,
                    category_assigned="other_opex",
                )
            )
            canonical = "other_opex"

        if (
            has_total_payroll
            and canonical == "Payroll"
            and group.subtotal_row
            and _normalize_statement_label(group.subtotal_row[0]) != "TOTAL PAYROLL"
        ):
            continue

        rows.append((canonical, [amount]))
        row_indent_levels.append(group.indent_level)

    return rows


def _get_raw_label(raw_row: Any) -> str:
    """Extract row label text without stripping leading whitespace."""
    row = list(raw_row)
    numeric_indices = [i for i, value in enumerate(row) if _to_float(value) is not None]
    if numeric_indices:
        first_num = min(numeric_indices)
        for i in range(first_num - 1, -1, -1):
            value = row[i]
            if value is not None and not _is_date_like(value):
                text = str(value)
                if text.strip():
                    return text
    for value in reversed(row):
        if value is not None and not _is_date_like(value):
            text = str(value)
            if text.strip():
                return text
    return ""


def _extract_statement_rows(
    raw_rows: Any,
    warnings: list[ParseWarning] | None = None,
) -> list[tuple[str, list[float | None]]]:
    """Extract expense rows from a T12/P&L export.

    Many PMS exports are full statements, not bare expense schedules. When an
    `OPERATING EXPENSES` section is present, only rows inside that macro section
    should feed canonical `opex_table`. Revenue / other-income lines and
    subtotal rollups otherwise poison NOI.
    """
    if warnings is None:
        warnings = []

    all_raw = list(raw_rows)
    prepared_rows: list[tuple[str, str, list[float | None], bool, str | None, bool]] = []
    contains_operating_section = False
    contains_income_boundary = False

    for raw_row in all_raw:
        if not raw_row:
            continue
        label, values = _extract_label_and_values(raw_row)
        raw_label = _get_raw_label(raw_row)
        if not label:
            continue
        has_numeric_values = any(v is not None for v in values)
        macro_section = _detect_macro_section(label, has_numeric_values)
        if macro_section == "operating_expenses":
            contains_operating_section = True
        income_boundary = _is_income_boundary_row(label, has_numeric_values)
        if income_boundary:
            contains_income_boundary = True
        prepared_rows.append(
            (label, raw_label, values, has_numeric_values, macro_section, income_boundary)
        )

    opex_raw_rows: list[tuple[str, str, list[float | None]]] = []
    current_macro_section: str | None = None
    after_income_boundary = False

    for label, raw_label, values, has_numeric_values, macro_section, income_boundary in prepared_rows:
        if macro_section is not None and not has_numeric_values:
            current_macro_section = macro_section
            continue
        if income_boundary:
            after_income_boundary = True
            if not contains_operating_section and current_macro_section == "income":
                current_macro_section = None
            continue
        in_opex = _is_inside_expense_window(
            contains_operating_section=contains_operating_section,
            current_macro_section=current_macro_section,
            contains_income_boundary=contains_income_boundary,
            after_income_boundary=after_income_boundary,
        )
        if in_opex and current_macro_section != "non_operating_expenses":
            opex_raw_rows.append((label, raw_label, values))

    groups = _detect_hierarchy(opex_raw_rows)
    if groups:
        resolved = _resolve_groups(groups, warnings)
        totals: dict[str, float] = {}
        for canon_label, values in resolved:
            amount = sum(value for value in values if value is not None)
            totals[canon_label] = totals.get(canon_label, 0.0) + amount
        return [
            (label, [amount])
            for label, amount in totals.items()
        ]

    rows: list[tuple[str, list[float | None]]] = []
    current_macro_section: str | None = None
    after_income_boundary = False
    current_detail_section: str | None = None
    detail_seen_in_section = False
    detail_categories_in_section: set[str] = set()

    for label, raw_label, values, has_numeric_values, macro_section, income_boundary in prepared_rows:
        if macro_section is not None and not has_numeric_values:
            current_macro_section = macro_section
            current_detail_section = None
            detail_seen_in_section = False
            detail_categories_in_section = set()
            continue
        if income_boundary:
            after_income_boundary = True
            if not contains_operating_section and current_macro_section == "income":
                current_macro_section = None
            current_detail_section = None
            detail_seen_in_section = False
            detail_categories_in_section = set()
            continue
        if not has_numeric_values:
            if _is_inside_expense_window(
                contains_operating_section=contains_operating_section,
                current_macro_section=current_macro_section,
                contains_income_boundary=contains_income_boundary,
                after_income_boundary=after_income_boundary,
            ):
                current_detail_section = _normalize_statement_label(label)
                detail_seen_in_section = False
                detail_categories_in_section = set()
            continue
        if contains_operating_section and current_macro_section != "operating_expenses":
            continue
        if contains_income_boundary and not contains_operating_section and not after_income_boundary:
            continue
        if current_macro_section == "non_operating_expenses":
            continue
        if current_macro_section == "income" and contains_income_boundary:
            continue
        numeric_values = [value for value in values if value is not None]
        numeric_values = _strip_trailing_total_column(numeric_values)
        if (
            numeric_values
            and sum(numeric_values) == 0
            and _matches_category_map(label)
        ):
            current_detail_section = _normalize_statement_label(
                _matches_category_map(label) or label
            )
            detail_seen_in_section = False
            detail_categories_in_section = set()
            continue
        if _is_summary_row(label):
            continue
        label_is_account_detail = bool(re.match(r"^\s*\d+(?:\.\d+)?\s*[: -]", label))
        label_category = _matches_category_map(label)
        if (
            detail_seen_in_section
            and current_detail_section
            and not label_is_account_detail
            and label_category
            and label_category in detail_categories_in_section
        ):
            continue
        if _is_section_subtotal_row(label, current_detail_section, detail_seen_in_section):
            continue
        if len(numeric_values) > 12:
            numeric_values = numeric_values[:12]
        if not numeric_values:
            continue
        rows.append((label, numeric_values))
        detail_seen_in_section = True
        if label_category:
            detail_categories_in_section.add(label_category)
    return rows


def _is_inside_expense_window(
    *,
    contains_operating_section: bool,
    current_macro_section: str | None,
    contains_income_boundary: bool,
    after_income_boundary: bool,
) -> bool:
    if contains_operating_section:
        return current_macro_section == "operating_expenses"
    if contains_income_boundary:
        return after_income_boundary and current_macro_section != "non_operating_expenses"
    return current_macro_section not in {"income", "non_operating_expenses"}


def _is_section_subtotal_row(
    label: str,
    current_detail_section: str | None,
    detail_seen_in_section: bool,
) -> bool:
    normalized = _normalize_statement_label(label)
    if detail_seen_in_section and current_detail_section:
        current_normalized = _normalize_statement_label(current_detail_section)
        if current_normalized == normalized:
            return True
        current_canonical = _matches_category_map(current_detail_section)
        label_canonical = _matches_category_map(label)
        label_is_account_detail = bool(re.match(r"^\s*\d+(?:\.\d+)?\s*[: -]", label))
        if (
            not label_is_account_detail
            and current_canonical
            and label_canonical
            and current_canonical == label_canonical
        ):
            return True
    return normalized in {
        "MANAGER CONTROLLED EXPENSES",
        "ASSET MANAGER CONTROLLED EXPENSES",
        "EXPENSE",
        "EXPENSES",
    }


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _is_date_like(value: Any) -> bool:
    return isinstance(value, (date, datetime))


def _extract_label_and_values(raw_row: Any) -> tuple[str, list[float | None]]:
    """Return the most likely statement label plus numeric cells to its right.

    Many owner exports place the visible account name several columns to the
    right of the first stub column. We therefore look for the last non-empty
    text cell before the first numeric cell, rather than assuming column 0 is
    always the category label.
    """
    row = list(raw_row)
    numeric_indices = [idx for idx, value in enumerate(row) if _to_float(value) is not None]

    if numeric_indices:
        first_numeric_idx = min(numeric_indices)
        text_candidates = [
            (idx, _cell_text(value))
            for idx, value in enumerate(row[:first_numeric_idx])
            if _cell_text(value) and not _is_date_like(value)
        ]
        label_idx = text_candidates[-1][0] if text_candidates else None
        label = text_candidates[-1][1] if text_candidates else ""
        if label_idx is None and first_numeric_idx == 0:
            # Some owner exports place the account code in column 0 and the
            # visible label in column 1, with monthly values starting in column
            # 2. In that layout the "last text before first numeric" heuristic
            # sees nothing, so fall back to the first non-empty text cell after
            # the account code.
            for idx, value in enumerate(row[1:], start=1):
                text = _cell_text(value)
                if text and not _is_date_like(value):
                    label_idx = idx
                    label = text
                    break
        values = [_to_float(value) for value in row[(label_idx + 1 if label_idx is not None else 0):]]
        return label, values

    text_candidates = [
        _cell_text(value)
        for value in row
        if _cell_text(value) and not _is_date_like(value)
    ]
    return (text_candidates[-1] if text_candidates else ""), []


def _normalize_statement_label(label: str) -> str:
    stripped = re.sub(r"^\s*\d+(?:\.\d+)?\s*[:\-]?\s*", "", label)
    return " ".join(stripped.upper().split())


def _detect_macro_section(label: str, has_numeric_values: bool) -> str | None:
    """Classify statement macro headers.

    We only treat exact high-level statement sections as macro sections; nested
    groups like `ADMINISTRATIVE EXPENSES` stay inside the current macro section.
    """
    if has_numeric_values:
        return None
    normalized = _normalize_statement_label(label)
    if (
        "NON-OPERATING EXPENSE" in normalized
        or normalized in {
            "INTEREST EXPENSE",
            "INTEREST EXPENSES",
            "DEBT SERVICE",
            "DEBT FINANCING",
        }
    ):
        return "non_operating_expenses"
    if normalized in {
        "OPERATING EXPENSES",
        "OPERATING EXPENSE",
        "EXPENSE",
        "EXPENSES",
        "CONTROLLABLE EXPENSES",
        "CONTROLLABLE OPERATING EXPENSES",
    }:
        return "operating_expenses"
    if normalized in {"REVENUE", "OTHER INCOME", "RESIDENTIAL MANAGEMENT INCOME"}:
        return "income"
    return None


def _is_summary_row(label: str) -> bool:
    label_without_account = _LEADING_ACCOUNT_CODE_RE.sub("", label.strip())
    normalized = _normalize_statement_label(label_without_account)
    if normalized.startswith(("TOTAL ", "NET ", "GROSS ")):
        return True
    if normalized in {
        "INCOME",
        "REVENUE",
        "REVENUES",
        "CONTROLLABLE EXPENSES",
        "CONTROLLABLE OPERATING EXPENSES",
        "MANAGER CONTROLLED EXPENSES",
        "ASSET MANAGER CONTROLLED EXPENSES",
        "FUNDS FROM OPERATIONS",
        "EXPENSE",
        "EXPENSES",
        "OPERATING EXPENSE",
        "OPERATING EXPENSES",
        "NOI",
        "NET OPERATING INCOME",
    }:
        return True
    return "OPERATING INCOME" in normalized


def _is_income_boundary_row(label: str, has_numeric_values: bool) -> bool:
    """Identify summary rows that mark the handoff from income to expenses.

    Many owner statements use slightly different labels for the final income
    subtotal just before the operating-expense section. When no explicit
    `OPERATING EXPENSES` header exists, this boundary becomes the safest signal
    for where expense parsing should begin.
    """
    if not has_numeric_values:
        return False
    normalized = _normalize_statement_label(label)
    if not normalized.startswith(("TOTAL ", "NET ", "GROSS ")):
        return False
    if "NON-OPERATING" in normalized:
        return False
    return normalized in {
        "TOTAL INCOME",
        "TOTAL REVENUE",
        "TOTAL REVENUES",
        "TOTAL OPERATING REVENUE",
        "TOTAL OPERATING REVENUES",
        "TOTAL OPERATING INCOME",
        "EFFECTIVE GROSS INCOME",
        "EFFECTIVE GROSS REVENUE",
        "GROSS OPERATING INCOME",
        "GROSS OPERATING REVENUE",
    }


def _to_float(v: Any) -> float | None:
    """Coerce a T12 cell value to float, sign-preserving.

    Handles common formatting quirks:
      - currency symbols ("$") and thousands separators (",") are stripped
      - parens-as-negative ("(500)") is converted to -500.0
      - blank / non-numeric cells return None

    Sign convention: T12 expense rows are positive; revenue or
    credit rows (e.g. bad-debt recovery, reimbursement offsets)
    are negative. Sign is preserved — we do NOT abs() values.
    Unconditional abs() would silently flip legitimate credits
    into inflated expenses (Bug 1.7).
    """
    if v is None:
        return None
    try:
        cleaned = (
            str(v)
            .replace(",", "")
            .replace("$", "")
            .replace("(", "-")
            .replace(")", "")
            .strip()
        )
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def _strip_trailing_total_column(values: list[float]) -> list[float]:
    """Drop a trailing total column when the row is 12 months + annual total.

    Many Excel T12 exports include 12 monthly columns plus a final annual total.
    The canonical parser wants the 12 month values only. If the last numeric cell
    is approximately the sum of the prior numeric cells, treat it as a total and
    remove it.
    """
    if len(values) < 3:
        return values
    trailing = values[-1]
    prior_sum = sum(values[:-1])
    tolerance = max(abs(prior_sum) * 0.01, 1.0)
    if abs(trailing - prior_sum) <= tolerance:
        return values[:-1]
    return values
