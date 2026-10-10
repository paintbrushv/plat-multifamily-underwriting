"""Assumptions sheet: export and import (ADR 0003 §8, slice 7).

Every run writes an assumptions sheet so an analyst can inspect each resolved
assumption and change it. The sheet is a flat view of the canonical
``assumptions.entries``: one row per entry with its value, provenance, the
resolver default, the analyst override and the delta from the default.

Two formats carry the same rows:

- ``assumptions.json``: ``write_assumptions_json`` / ``read_assumption_overrides``.
- An "Assumptions" Excel tab (an Excel table named ``tbl_assumptions``):
  ``add_assumptions_sheet`` / ``write_assumptions_xlsx`` /
  ``read_assumption_overrides_xlsx``.

The read side turns an edited sheet into the ``{key: {"value": ..., "note": ...}}``
overrides ``resolve_assumptions`` takes. A row becomes an override when its
override column is set (that wins), or else when its value differs from its
``default_value``.

Rows are built generically from whatever an entry carries: scalar, vector and
dict values all work, and fields the sheet does not know about are kept in the
row's ``extra`` object. Nothing here special-cases an assumption key.
"""

from __future__ import annotations

import json
import math
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

SHEET_VERSION = "plat.assumptions.sheet/1"
SHEET_TITLE = "Assumptions"
TABLE_NAME = "tbl_assumptions"
DEFAULT_EDIT_NOTE = "edited in the assumptions sheet"

# Entry fields with a dedicated sheet column. Every other entry field is kept
# verbatim in the row's ``extra`` object. ``candidates`` stays in the
# canonical (it is the audit trail, not an editable value) and an entry's
# top-level ``cohort_overrides`` mirrors ``override.cohort_overrides``.
_ENTRY_COLUMNS = (
    "value",
    "unit",
    "source",
    "basis",
    "as_of",
    "confidence",
    "review_required",
    "default_value",
    "override",
)
_NOT_EXTRA = frozenset(_ENTRY_COLUMNS) | {"candidates", "cohort_overrides"}

# (row field, Excel header). The order is the column order of both formats.
COLUMNS: tuple[tuple[str, str], ...] = (
    ("key", "Key"),
    ("value", "Value"),
    ("unit", "Unit"),
    ("source", "Source"),
    ("basis", "Basis"),
    ("as_of", "As Of"),
    ("confidence", "Confidence"),
    ("review_required", "Review Required"),
    ("default_value", "Default Value"),
    ("delta", "Delta"),
    ("override_value", "Override"),
    ("override_note", "Note"),
    ("override_cohorts", "Override Cohorts"),
    ("guardrail_status", "Guardrail"),
    ("extra", "Extra"),
)
ROW_FIELDS = tuple(field for field, _ in COLUMNS)
_HEADER_TO_FIELD = {header.lower(): field for field, header in COLUMNS}
_EDITABLE_FIELDS = ("override_value", "override_note")
# Columns whose cells may hold a number, a vector or an object.
_VALUE_FIELDS = frozenset(
    {"value", "default_value", "delta", "override_value", "override_cohorts", "extra"}
)
_NUMBER_FORMAT_FIELDS = ("value", "default_value", "delta", "override_value")

_REL_TOL = 1e-9
_ABS_TOL = 1e-12


# ── value helpers ────────────────────────────────────────────────────────


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _clean(number: float) -> float:
    """Round away float noise and normalize ``-0.0`` so deltas are stable."""
    rounded = round(number, 12)
    return 0.0 if rounded == 0 else rounded


def _same(a: Any, b: Any) -> bool:
    """Structural equality with a float tolerance (Excel round-trips floats)."""
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, (list, tuple)) or isinstance(b, (list, tuple)):
        if not isinstance(a, (list, tuple)) or not isinstance(b, (list, tuple)) or len(a) != len(b):
            return False
        return all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, Mapping) or isinstance(b, Mapping):
        if not isinstance(a, Mapping) or not isinstance(b, Mapping) or set(a) != set(b):
            return False
        return all(_same(a[k], b[k]) for k in a)
    na, nb = _number(a), _number(b)
    if na is not None and nb is not None:
        return math.isclose(na, nb, rel_tol=_REL_TOL, abs_tol=_ABS_TOL)
    return a == b


def delta(value: Any, default: Any) -> Any:
    """``value - default`` for numeric scalars, element-wise for numeric vectors.

    Anything else (dicts, text, mismatched shapes, missing values) has no
    delta and returns ``None``.
    """
    nv, nd = _number(value), _number(default)
    if nv is not None and nd is not None:
        return _clean(nv - nd)
    if isinstance(value, (list, tuple)) and isinstance(default, (list, tuple)) and len(value) == len(default):
        pairs = [(_number(v), _number(d)) for v, d in zip(value, default)]
        if pairs and all(v is not None and d is not None for v, d in pairs):
            return [_clean(v - d) for v, d in pairs]
    return None


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _guardrail_status(guardrail: Any) -> Optional[str]:
    if guardrail is None:
        return None
    if isinstance(guardrail, Mapping):
        status = guardrail.get("status")
        return None if status is None else str(status)
    return str(guardrail)


# ── rows ─────────────────────────────────────────────────────────────────


def _entries(canonical: Mapping[str, Any]) -> Mapping[str, Any]:
    section = canonical.get("assumptions") if isinstance(canonical, Mapping) else None
    entries = section.get("entries") if isinstance(section, Mapping) else None
    return entries if isinstance(entries, Mapping) else {}


def has_assumptions(canonical: Any) -> bool:
    """True when the canonical carries an ``assumptions`` section."""
    return isinstance(canonical, Mapping) and isinstance(canonical.get("assumptions"), Mapping)


def _key_order() -> dict[str, int]:
    from engine.assumptions.resolver import ASSUMPTION_KEYS

    return {key: index for index, key in enumerate(ASSUMPTION_KEYS)}


def sorted_keys(keys: Iterable[str]) -> list[str]:
    """Registry order first (``ASSUMPTION_KEYS``), then any other key A→Z."""
    order = _key_order()
    return sorted(keys, key=lambda key: (order.get(key, len(order)), key))


def entry_row(key: str, entry: Mapping[str, Any]) -> dict[str, Any]:
    """One sheet row for one registry entry. Works for any entry shape."""
    entry = entry if isinstance(entry, Mapping) else {"value": entry}
    value = _jsonable(entry.get("value"))
    default_value = _jsonable(entry.get("default_value"))
    override = entry.get("override")
    override = override if isinstance(override, Mapping) else None
    extra = {
        str(name): _jsonable(entry[name])
        for name in sorted(entry, key=str)
        if name not in _NOT_EXTRA
    }
    row = {
        "key": key,
        "value": value,
        "unit": entry.get("unit"),
        "source": entry.get("source"),
        "basis": entry.get("basis"),
        "as_of": entry.get("as_of"),
        "confidence": entry.get("confidence"),
        "review_required": bool(entry.get("review_required", False)),
        "default_value": default_value,
        "delta": delta(value, default_value),
        "override_value": _jsonable(override.get("value")) if override else None,
        "override_note": override.get("note") if override else None,
        "override_cohorts": _jsonable(override.get("cohort_overrides")) if override else None,
        "guardrail_status": _guardrail_status(entry.get("guardrail")),
        "extra": extra or None,
    }
    return row


def sheet_rows(canonical: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One row per assumption entry, in a stable key order."""
    entries = _entries(canonical)
    return [entry_row(key, entries[key]) for key in sorted_keys(entries)]


def assumptions_document(canonical: Mapping[str, Any]) -> dict[str, Any]:
    """The ``assumptions.json`` payload: sheet rows plus section metadata."""
    section = canonical.get("assumptions") if has_assumptions(canonical) else {}
    metadata = canonical.get("metadata") if isinstance(canonical.get("metadata"), Mapping) else {}
    return {
        "sheet_version": SHEET_VERSION,
        "resolver_version": section.get("resolver_version"),
        "mode": section.get("mode"),
        "deal_id": metadata.get("deal_id"),
        "instructions": (
            "Set override_value (and override_note) on a row, or edit its value, then pass this "
            "file to --assumption-overrides or `runs/assumptions_sheet.py apply`. A set "
            "override_value wins over an edited value."
        ),
        "columns": list(ROW_FIELDS),
        "rows": sheet_rows(canonical),
    }


def write_assumptions_json(canonical: Mapping[str, Any], path: Path | str) -> Path:
    """Write ``assumptions.json`` for the canonical and return its path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(assumptions_document(canonical), indent=2, ensure_ascii=False)
    path.write_text(payload + "\n", encoding="utf-8")
    return path


def sheet_path_for(output_path: Path | str) -> Path:
    """``deal.json`` → ``deal.assumptions.json`` in the same folder."""
    output_path = Path(output_path)
    return output_path.with_name(f"{output_path.stem}.assumptions.json")


# ── overrides from an edited sheet ───────────────────────────────────────


def _row_override(row: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    value = row.get("value")
    override_value = row.get("override_value")
    note = row.get("override_note")
    note = str(note).strip() if note is not None and str(note).strip() else None
    if override_value is not None:
        chosen = override_value
    elif value is not None and not _same(value, row.get("default_value")):
        chosen = value
    else:
        return None
    out: dict[str, Any] = {"value": chosen, "note": note or DEFAULT_EDIT_NOTE}
    cohorts = row.get("override_cohorts")
    if override_value is not None and cohorts:
        out["cohort_overrides"] = cohorts
    return out


def overrides_from_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """``{key: {"value", "note"[, "cohort_overrides"]}}`` for each edited row."""
    out: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"assumptions sheet row {index} must be an object")
        key = row.get("key")
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"assumptions sheet row {index} has no key")
        key = key.strip()
        if key in seen:
            raise ValueError(f"assumptions sheet lists {key!r} twice")
        seen.add(key)
        override = _row_override(row)
        if override is not None:
            out[key] = override
    return {key: out[key] for key in sorted_keys(out)}


def overrides_from_document(data: Any) -> dict[str, Any]:
    """Overrides from an exported sheet document, a bare row list, or a plain
    ``{key: value | {"value": ..., "note": ...}}`` overrides object (returned as-is)."""
    if isinstance(data, list):
        return overrides_from_rows(data)
    if not isinstance(data, Mapping):
        raise ValueError("assumption overrides must be a JSON object or a list of sheet rows")
    if "rows" in data:
        rows = data["rows"]
        if not isinstance(rows, list):
            raise ValueError("assumptions sheet 'rows' must be a list")
        return overrides_from_rows(rows)
    return dict(data)


def read_assumption_overrides(path: Path | str) -> dict[str, Any]:
    """Overrides from an edited ``assumptions.json`` (or a plain overrides JSON)."""
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    return overrides_from_document(data)


def load_assumption_overrides(path: Path | str) -> dict[str, Any]:
    """Overrides from a ``.json`` sheet/overrides file or an ``.xlsx``/``.xlsm`` workbook."""
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        return read_assumption_overrides_xlsx(path)
    return read_assumption_overrides(path)


# ── Excel ────────────────────────────────────────────────────────────────


def _to_cell(value: Any) -> Any:
    """Scalars are written as themselves; vectors and objects as JSON text."""
    if isinstance(value, (list, tuple, Mapping)):
        return json.dumps(_jsonable(value), ensure_ascii=False)
    if isinstance(value, Decimal):
        return float(value)
    return value


def _from_cell(value: Any, *, field: str, key: Any) -> Any:
    """Decode a cell of a value column back to a number, vector or object."""
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return None
    if text[0] in "[{":
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"assumptions sheet {key!r} {field}: invalid JSON {text!r} ({exc})") from exc
    percent = text.endswith("%")
    try:
        number = float(text[:-1] if percent else text.replace(",", ""))
    except ValueError:
        return text
    return number / 100.0 if percent else number


def _number_format(unit: Any) -> Optional[str]:
    unit = str(unit or "")
    if unit.startswith("decimal"):
        return "0.00%"
    if unit.startswith("usd"):
        return '"$"#,##0.00'
    return None


def add_assumptions_sheet(wb: Any, canonical: Mapping[str, Any], *, title: str = SHEET_TITLE) -> Any:
    """Add an "Assumptions" tab (Excel table ``tbl_assumptions``) to an openpyxl workbook."""
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.table import Table, TableStyleInfo

    from engine.brand import HEX_DARK_900, HEX_GOLD_LIGHT
    from engine.xlsx_helpers import set_col_widths, write_data_row, write_header_row

    if title in wb.sheetnames:
        del wb[title]
    ws = wb.create_sheet(title)
    rows = sheet_rows(canonical)

    ws["A1"] = "ASSUMPTIONS"
    ws["A1"].font = Font(name="Calibri", size=14, bold=True, color=HEX_DARK_900)
    ws["A2"] = (
        "Edit the Override and Note columns (or Value), then pass this workbook to "
        "--assumption-overrides or `runs/assumptions_sheet.py apply`. Vectors and objects are JSON text."
    )
    ws["A2"].font = Font(name="Calibri", size=10, italic=True, color=HEX_DARK_900)

    header_row = 4
    write_header_row(ws, header_row, [header for _, header in COLUMNS])
    editable_fill = PatternFill(start_color=HEX_GOLD_LIGHT, end_color=HEX_GOLD_LIGHT, fill_type="solid")
    editable_cols = [ROW_FIELDS.index(field) + 1 for field in _EDITABLE_FIELDS]
    wrap = Alignment(horizontal="left", vertical="center", wrap_text=True)

    for offset, row in enumerate(rows, start=1):
        excel_row = header_row + offset
        write_data_row(ws, excel_row, [_to_cell(row[field]) for field in ROW_FIELDS], zebra=offset % 2 == 0)
        fmt = _number_format(row.get("unit"))
        for field in _NUMBER_FORMAT_FIELDS:
            cell = ws.cell(row=excel_row, column=ROW_FIELDS.index(field) + 1)
            if fmt and _number(cell.value) is not None:
                cell.number_format = fmt
        for col in editable_cols:
            ws.cell(row=excel_row, column=col).fill = editable_fill
        ws.cell(row=excel_row, column=ROW_FIELDS.index("basis") + 1).alignment = wrap
    for col in editable_cols:
        ws.cell(row=header_row, column=col).font = Font(
            name="Calibri", size=10, bold=True, color=HEX_DARK_900
        )
        ws.cell(row=header_row, column=col).fill = editable_fill

    # An Excel table needs at least one data row; an empty sheet keeps a blank one.
    last_row = header_row + max(len(rows), 1)
    ref = f"A{header_row}:{get_column_letter(len(COLUMNS))}{last_row}"
    table = Table(displayName=TABLE_NAME, ref=ref)
    table.tableStyleInfo = TableStyleInfo(name="TableStyleLight1", showRowStripes=False)
    ws.add_table(table)

    widths = {
        "key": 30, "value": 16, "unit": 26, "source": 24, "basis": 70, "as_of": 12,
        "confidence": 12, "review_required": 10, "default_value": 16, "delta": 14,
        "override_value": 16, "override_note": 32, "override_cohorts": 22,
        "guardrail_status": 14, "extra": 30,
    }
    set_col_widths(ws, {ROW_FIELDS.index(field) + 1: width for field, width in widths.items()})
    ws.freeze_panes = ws.cell(row=header_row + 1, column=2)
    return ws


def write_assumptions_xlsx(canonical: Mapping[str, Any], path: Path | str) -> Path:
    """Write a workbook holding only the "Assumptions" tab."""
    from openpyxl import Workbook

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    default = wb.active
    add_assumptions_sheet(wb, canonical)
    wb.remove(default)
    wb.save(path)
    return path


def _rows_from_table(headers: list[Any], cells: Iterable[list[Any]]) -> list[dict[str, Any]]:
    fields: list[Optional[str]] = [
        _HEADER_TO_FIELD.get(str(header).strip().lower()) if header is not None else None
        for header in headers
    ]
    if "key" not in fields:
        raise ValueError("assumptions sheet has no Key column")
    rows: list[dict[str, Any]] = []
    for raw in cells:
        row: dict[str, Any] = {}
        for field, cell in zip(fields, raw):
            if field is not None:
                row[field] = cell
        key = row.get("key")
        if key is None or (isinstance(key, str) and not key.strip()):
            continue
        for field in _VALUE_FIELDS:
            if field in row:
                row[field] = _from_cell(row[field], field=field, key=key)
        rows.append(row)
    return rows


def read_assumption_rows_xlsx(path: Path | str) -> list[dict[str, Any]]:
    """Sheet rows from a workbook's ``tbl_assumptions`` table.

    Reads through ``engine.excel_io`` like every other workbook table. When the
    table is gone (some editors drop Excel tables), the "Assumptions" tab is
    scanned for its header row instead.
    """
    from engine.excel_io import _is_blank_row, _normalize_cell, _read_excel_table

    path = Path(path)
    try:
        table = _read_excel_table(path, TABLE_NAME)
    except KeyError:
        table = None
    if table is not None:
        return _rows_from_table(list(table.headers), table.rows)

    from openpyxl import load_workbook

    wb = load_workbook(path, data_only=True)
    try:
        if SHEET_TITLE not in wb.sheetnames:
            raise KeyError(f"workbook has no {TABLE_NAME} table or {SHEET_TITLE!r} tab: {path}")
        values = [
            [_normalize_cell(v) for v in row]
            for row in wb[SHEET_TITLE].iter_rows(values_only=True)
        ]
    finally:
        wb.close()
    for index, row in enumerate(values):
        if row and isinstance(row[0], str) and row[0].strip().lower() == "key":
            body = [r for r in values[index + 1:] if not _is_blank_row(r)]
            return _rows_from_table(row, body)
    raise KeyError(f"{SHEET_TITLE!r} tab has no Key header row: {path}")


def read_assumption_overrides_xlsx(path: Path | str) -> dict[str, dict[str, Any]]:
    """Overrides from an edited "Assumptions" tab."""
    return overrides_from_rows(read_assumption_rows_xlsx(path))
