# T12 Parser Indent-Hierarchy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `engine/ingest/t12_parser.py` with a two-pass hierarchy-aware path that correctly classifies OpEx for Lennar/OneSite T12s (and any PMS using `Total X` subcategory subtotals), eliminating the account-code leakthrough bug without changing the flat-path or output schema.

**Architecture:** A pre-scan groups OpEx rows into `OpexGroup` objects using `Total X` markers and leading-whitespace indent. A 4-tier category resolver maps each group to a canonical name via `_SUBTOTAL_CATEGORY_MAP` (tiers 1–2) then `_CATEGORY_MAP` (tier 3), emitting `ParseWarning` on tier-4 misses. The flat path (existing logic) is untouched — it activates when fewer than 2 subtotal groups are detected.

**Tech Stack:** Python 3.12+, dataclasses, openpyxl (already a dependency), pytest, existing `_CATEGORY_MAP`/`_normalize_category()`.

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `engine/ingest/t12_parser.py` | Modify | All new types, constants, helpers, and wiring |
| `tests/test_t12_parser.py` | Modify | New unit tests for each new function + integration tests |
| `tests/fixtures/lennar_style_t12.csv` | Create | Hierarchical T12 fixture with account codes + subtotals |
| `.claude/error-memory.md` | Modify | Correct the misdiagnosed Lennar T12 entry |

---

## Task 1: Types, constants, and pure helpers

**Files:**
- Modify: `engine/ingest/t12_parser.py`
- Test: `tests/test_t12_parser.py`

- [ ] **Step 1.1: Write the failing tests**

Add at the bottom of `tests/test_t12_parser.py`:

```python
# ── Task 1: Hierarchy helpers ────────────────────────────────────────────────

from engine.ingest.t12_parser import (
    _is_subcategory_subtotal,
    _matches_section_total,
    _normalize_subtotal_category,
    _text_indent_level,
    _matches_category_map,
    OpexGroup,
    ParseWarning,
)


def test_is_subcategory_subtotal_title_case():
    assert _is_subcategory_subtotal("Total Personnel Expense") is True
    assert _is_subcategory_subtotal("Total Utilities") is True
    assert _is_subcategory_subtotal("Total Contract Services") is True


def test_is_subcategory_subtotal_rejects_section_totals():
    assert _is_subcategory_subtotal("TOTAL OPERATING EXPENSES") is False
    assert _is_subcategory_subtotal("Total Operating Expenses") is False
    assert _is_subcategory_subtotal("Net Operating Income") is False
    assert _is_subcategory_subtotal("Total Revenue") is False
    assert _is_subcategory_subtotal("Total Income") is False
    assert _is_subcategory_subtotal("Effective Gross Income") is False


def test_is_subcategory_subtotal_rejects_non_total_rows():
    assert _is_subcategory_subtotal("513110 - Salary - Manager") is False
    assert _is_subcategory_subtotal("Personnel Expense") is False
    assert _is_subcategory_subtotal("") is False


def test_matches_section_total():
    assert _matches_section_total("TOTAL OPERATING EXPENSES") is True
    assert _matches_section_total("Total Operating Expenses") is True
    assert _matches_section_total("Net Operating Income") is True
    assert _matches_section_total("Effective Gross Revenue") is True
    assert _matches_section_total("Total Revenue") is True
    assert _matches_section_total("Total Personnel Expense") is False
    assert _matches_section_total("Total Utilities") is False


def test_normalize_subtotal_category_known_patterns():
    assert _normalize_subtotal_category("Total Personnel Expense") == "Payroll"
    assert _normalize_subtotal_category("TOTAL PAYROLL") == "Payroll"
    assert _normalize_subtotal_category("Total Utilities") == "Utilities"
    assert _normalize_subtotal_category("Total Repairs And Maintenance") == "Repairs & Maintenance"
    assert _normalize_subtotal_category("Total Administrative") == "Administrative"
    assert _normalize_subtotal_category("Total Marketing") == "Marketing / Advertising"
    assert _normalize_subtotal_category("Total Management Fee") == "Property Management Fee"
    assert _normalize_subtotal_category("Total Contract Services") == "Contract Services"
    assert _normalize_subtotal_category("Total Insurance") == "Insurance"
    assert _normalize_subtotal_category("Total Security") == "Security"
    assert _normalize_subtotal_category("Total Landscaping") == "Landscaping / Grounds"


def test_normalize_subtotal_category_no_match_returns_none():
    assert _normalize_subtotal_category("Total Debt Service") is None
    assert _normalize_subtotal_category("Total Capex Reserve") is None
    assert _normalize_subtotal_category("Random Label") is None


def test_text_indent_level_spaces():
    assert _text_indent_level("    text") == 2   # 4 spaces = level 2
    assert _text_indent_level("  text") == 1     # 2 spaces = level 1
    assert _text_indent_level("text") == 0
    assert _text_indent_level("") == 0
    assert _text_indent_level("        text") == 4  # 8 spaces = level 4


def test_matches_category_map_known():
    assert _matches_category_map("513110 - Salary - Manager") == "Payroll"
    assert _matches_category_map("516110 - Electricity - Common") == "Electricity"
    assert _matches_category_map("Real Estate Taxes") == "Real Estate Taxes"


def test_matches_category_map_unknown_returns_none():
    assert _matches_category_map("513120 - Bonuses And Incentives") is None
    assert _matches_category_map("Completely Unknown Line Item") is None


def test_opex_group_and_parse_warning_are_dataclasses():
    g = OpexGroup(subsection_header="Personnel", detail_rows=[], subtotal_row=None, indent_level=1)
    assert g.subsection_header == "Personnel"
    w = ParseWarning(tier_reached=4, raw_label="Unknown", amount=1000.0, category_assigned="other_opex")
    assert w.tier_reached == 4
```

- [ ] **Step 1.2: Run tests to verify they fail**

```bash
cd /Users/matthewdickson/projects/multifamily-underwriting
.venv/bin/python -m pytest tests/test_t12_parser.py::test_is_subcategory_subtotal_title_case -v
```

Expected: `ImportError` or `AttributeError` — none of the new names exist yet.

- [ ] **Step 1.3: Add imports and types to `engine/ingest/t12_parser.py`**

After line 36 (`from __future__ import annotations`), add:

```python
from dataclasses import dataclass
```

After line 107 (`_SORTED_CATEGORY_MAP` block ends), before the blank line before `_LEADING_ACCOUNT_CODE_RE`, insert:

```python
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
    tier_reached: int       # 1–4; 4 = no match at any tier
    raw_label: str          # subtotal or header text that failed to match
    amount: float
    category_assigned: str  # always "other_opex" for tier-4 failures


# Used ONLY by _normalize_subtotal_category() (tiers 1 and 2).
# Must NOT be merged into _CATEGORY_MAP — these "total X" patterns would
# incorrectly fire against detail rows during tier-3 fallback.
_SUBTOTAL_CATEGORY_MAP: list[tuple[str, str]] = [
    ("total personnel",      "Payroll"),
    ("total payroll",        "Payroll"),
    ("total utilities",      "Utilities"),
    ("total maintenance",    "Repairs & Maintenance"),
    ("total repairs",        "Repairs & Maintenance"),
    ("total administrative", "Administrative"),
    ("total marketing",      "Marketing / Advertising"),
    ("total management",     "Property Management Fee"),
    ("total contract",       "Contract Services"),
    ("total insurance",      "Insurance"),
    ("total professional",   "Administrative"),
    ("total amenities",      "Other Operating Expenses"),
    ("total landscaping",    "Landscaping / Grounds"),
    ("total cleaning",       "Other Operating Expenses"),
    ("total security",       "Security"),
]

# Patterns that identify section-level totals (stop markers for the hierarchy
# detector). These are NOT subcategory subtotals.
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
```

- [ ] **Step 1.4: Add pure helper functions after `_normalize_category()` (after line 137)**

```python
def _normalize_subtotal_category(text: str) -> str | None:
    """Tier-1/2 lookup against _SUBTOTAL_CATEGORY_MAP only.

    Lowercases input and checks as a substring. No account-code stripping
    needed — subtotal rows never carry account-code prefixes.
    Returns None if no pattern matches.
    """
    lowered = text.strip().lower()
    for pattern, canonical in _SUBTOTAL_CATEGORY_MAP:
        if pattern in lowered:
            return canonical
    return None


def _is_subcategory_subtotal(text: str) -> bool:
    """True if text is a subcategory subtotal (e.g. 'Total Personnel Expense').

    Must start with 'Total ' (case-insensitive) AND not be a section-level
    total (which would match _SECTION_TOTAL_RE).
    """
    stripped = text.strip()
    if not stripped.lower().startswith("total "):
        return False
    return not bool(_SECTION_TOTAL_RE.search(stripped))


def _matches_section_total(text: str) -> bool:
    """True if text is a section-level total (stop marker for hierarchy scan)."""
    return bool(_SECTION_TOTAL_RE.search(text.strip()))


def _text_indent_level(raw_text: str) -> int:
    """Indent level derived from leading spaces in cell text.

    Lennar/OneSite uses 2-space increments; 4 leading spaces = level 2.
    Returns 0 for None or empty input.
    """
    if not raw_text:
        return 0
    spaces = len(raw_text) - len(raw_text.lstrip(" "))
    return spaces // 2


def _matches_category_map(text: str) -> str | None:
    """Like _normalize_category() but returns None instead of a title-cased
    fallback when no pattern in _CATEGORY_MAP matches.

    Used by tier-3 detail-row resolution to distinguish 'matched' from
    'fell through as a raw account-code string'.
    """
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
```

- [ ] **Step 1.5: Run tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_t12_parser.py -k "subtotal or section_total or normalize_subtotal or indent_level or category_map or opex_group or parse_warning" -v
```

Expected: all new tests PASS, all existing tests unchanged.

- [ ] **Step 1.6: Commit**

```bash
git add engine/ingest/t12_parser.py tests/test_t12_parser.py
git commit -m "feat(t12-parser): add hierarchy types, constants, and pure helpers"
```

---

## Task 2: `_detect_hierarchy()`

**Files:**
- Modify: `engine/ingest/t12_parser.py`
- Test: `tests/test_t12_parser.py`

- [ ] **Step 2.1: Write the failing tests**

Add at the bottom of `tests/test_t12_parser.py`:

```python
# ── Task 2: _detect_hierarchy ────────────────────────────────────────────────

from engine.ingest.t12_parser import _detect_hierarchy


def _lennar_opex_rows() -> list[tuple[str, str, list[float | None]]]:
    """Synthetic Lennar-style OpEx rows (stripped_label, raw_label, values)."""
    return [
        # (stripped, raw with indent, values)
        ("Personnel Expense",       "  Personnel Expense",        [None]*12),
        ("513110 - Salary - Manager","    513110 - Salary - Manager", [7000.0]*12),
        ("513120 - Bonuses",         "    513120 - Bonuses",          [500.0]*12),
        ("Total Personnel Expense",  "  Total Personnel Expense",  [7500.0]*12),
        ("Utilities",                "  Utilities",                [None]*12),
        ("516110 - Electricity",     "    516110 - Electricity",   [1500.0]*12),
        ("516210 - Water And Sewer", "    516210 - Water And Sewer",[800.0]*12),
        ("Total Utilities",          "  Total Utilities",          [2300.0]*12),
        ("Administrative",           "  Administrative",           [None]*12),
        ("517110 - Office Supplies", "    517110 - Office Supplies",[100.0]*12),
        ("Total Administrative",     "  Total Administrative",     [100.0]*12),
    ]


def test_detect_hierarchy_returns_three_groups():
    groups = _detect_hierarchy(_lennar_opex_rows())
    assert len(groups) == 3


def test_detect_hierarchy_group_subtotals():
    groups = _detect_hierarchy(_lennar_opex_rows())
    subtotal_labels = [g.subtotal_row[0] for g in groups]
    assert "Total Personnel Expense" in subtotal_labels
    assert "Total Utilities" in subtotal_labels
    assert "Total Administrative" in subtotal_labels


def test_detect_hierarchy_subtotal_amounts():
    groups = _detect_hierarchy(_lennar_opex_rows())
    by_subtotal = {g.subtotal_row[0]: g.subtotal_row[1] for g in groups}
    assert by_subtotal["Total Personnel Expense"] == 7500.0 * 12
    assert by_subtotal["Total Utilities"] == 2300.0 * 12
    assert by_subtotal["Total Administrative"] == 100.0 * 12


def test_detect_hierarchy_detail_rows_stored():
    groups = _detect_hierarchy(_lennar_opex_rows())
    payroll_group = next(g for g in groups if g.subtotal_row and "Personnel" in g.subtotal_row[0])
    detail_labels = [label for label, _ in payroll_group.detail_rows]
    assert "513110 - Salary - Manager" in detail_labels
    assert "513120 - Bonuses" in detail_labels


def test_detect_hierarchy_flat_returns_empty():
    """Flat T12 with < 2 subtotals → empty list (use flat path)."""
    flat_rows = [
        ("Payroll Taxes",   "Payroll Taxes",   [100.0]*12),
        ("Water",           "Water",           [50.0]*12),
        ("Total Personnel", "Total Personnel", [100.0]*12),  # only 1 subtotal
    ]
    groups = _detect_hierarchy(flat_rows)
    assert groups == []


def test_detect_hierarchy_stops_at_section_total():
    """TOTAL OPERATING EXPENSES stops the scan; rows after it are not in any group."""
    rows = [
        ("Personnel Expense",      "  Personnel Expense",     [None]*12),
        ("513110 - Salary",        "    513110 - Salary",     [1000.0]*12),
        ("Total Personnel Expense","  Total Personnel Expense",[1000.0]*12),
        ("Utilities",              "  Utilities",             [None]*12),
        ("516110 - Electricity",   "    516110 - Electricity", [500.0]*12),
        ("Total Utilities",        "  Total Utilities",        [500.0]*12),
        ("TOTAL OPERATING EXPENSES","TOTAL OPERATING EXPENSES",[1500.0]*12),
        ("Net Operating Income",   "Net Operating Income",    [8500.0]*12),
    ]
    groups = _detect_hierarchy(rows)
    # Only 2 groups; NOI row not included
    assert len(groups) == 2
    for g in groups:
        assert g.subtotal_row is not None
        assert "Net Operating Income" not in [label for label, _ in g.detail_rows]


def test_detect_hierarchy_group_without_subtotal_included():
    """A group with detail rows but no closing Total row is still emitted."""
    rows = [
        ("Personnel Expense", "  Personnel Expense", [None]*12),
        ("513110 - Salary",   "    513110 - Salary", [1000.0]*12),
        ("Total Personnel Expense","  Total Personnel Expense",[1000.0]*12),
        # Second group starts but has no subtotal (e.g. trailing rows at EOF)
        ("Utilities",         "  Utilities",         [None]*12),
        ("516110 - Electricity","  516110 - Electricity",[500.0]*12),
    ]
    groups = _detect_hierarchy(rows)
    # First group has subtotal, second does not — both returned (>=2 groups with subtotals is not met here)
    # Actually this has only 1 subtotal so flat detection kicks in → empty list
    assert groups == []
```

- [ ] **Step 2.2: Run tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_t12_parser.py -k "detect_hierarchy" -v
```

Expected: `ImportError` — `_detect_hierarchy` does not exist yet.

- [ ] **Step 2.3: Implement `_detect_hierarchy()` in `engine/ingest/t12_parser.py`**

Insert this function before the existing `_extract_statement_rows()` function (before line 245):

```python
def _detect_hierarchy(
    opex_rows: list[tuple[str, str, list[float | None]]],
) -> list[OpexGroup]:
    """Pass 1: group OpEx rows into OpexGroup objects.

    Args:
        opex_rows: (stripped_label, raw_label, values) for rows within the
                   OpEx section. raw_label preserves leading whitespace so
                   _text_indent_level() can compute indent depth.

    Returns:
        List of OpexGroup objects. Returns an empty list when the document
        appears flat (fewer than 2 groups have a subtotal_row), signalling
        that the caller should use the existing flat path instead.
    """
    groups: list[OpexGroup] = []
    current = OpexGroup(
        subsection_header=None, detail_rows=[], subtotal_row=None, indent_level=0
    )

    for stripped, raw, values in opex_rows:
        has_values = any(v is not None for v in values)

        if _matches_section_total(stripped):
            if current.detail_rows or current.subtotal_row:
                groups.append(current)
            break

        if _is_subcategory_subtotal(stripped):
            amount = sum(v for v in values if v is not None) if values else 0.0
            current.subtotal_row = (stripped, amount)
            groups.append(current)
            current = OpexGroup(
                subsection_header=None, detail_rows=[], subtotal_row=None, indent_level=0
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
            amount = sum(v for v in values if v is not None)
            current.detail_rows.append((stripped, amount))

    if current.detail_rows or current.subtotal_row:
        groups.append(current)

    subtotal_count = sum(1 for g in groups if g.subtotal_row is not None)
    if subtotal_count < 2:
        return []
    return groups
```

- [ ] **Step 2.4: Run tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_t12_parser.py -k "detect_hierarchy" -v
```

Expected: all `test_detect_hierarchy_*` tests PASS.

- [ ] **Step 2.5: Run full suite to confirm no regressions**

```bash
.venv/bin/python -m pytest tests/test_t12_parser.py -v
```

Expected: all previously passing tests still PASS.

- [ ] **Step 2.6: Commit**

```bash
git add engine/ingest/t12_parser.py tests/test_t12_parser.py
git commit -m "feat(t12-parser): add _detect_hierarchy() pass-1 grouping"
```

---

## Task 3: `_resolve_groups()`

**Files:**
- Modify: `engine/ingest/t12_parser.py`
- Test: `tests/test_t12_parser.py`

- [ ] **Step 3.1: Write the failing tests**

Add at the bottom of `tests/test_t12_parser.py`:

```python
# ── Task 3: _resolve_groups ──────────────────────────────────────────────────

from engine.ingest.t12_parser import _resolve_groups


def test_resolve_groups_uses_subtotal_value_not_detail_sum():
    """Subtotal value must be used; detail rows must not be double-counted."""
    groups = [
        OpexGroup(
            subsection_header="Personnel Expense",
            detail_rows=[
                ("513110 - Salary - Manager", 84_000.0),
                ("513120 - Bonuses", 6_000.0),
                ("513130 - Overtime", 2_400.0),
            ],
            subtotal_row=("Total Personnel Expense", 92_400.0),
            indent_level=1,
        ),
    ]
    warnings: list[ParseWarning] = []
    rows = _resolve_groups(groups, warnings)
    assert len(rows) == 1
    label, values = rows[0]
    assert label == "Payroll"
    assert sum(v for v in values if v is not None) == 92_400.0
    assert warnings == []


def test_resolve_groups_tier2_header_fallback():
    """When subtotal text doesn't match, try the subsection header."""
    groups = [
        OpexGroup(
            subsection_header="Total Personnel",   # unusual subtotal text
            detail_rows=[("Salary", 50_000.0)],
            subtotal_row=("Unrecognized Subtotal Label", 50_000.0),
            indent_level=1,
        ),
    ]
    # "Unrecognized Subtotal Label" won't hit tier-1
    # subsection_header "Total Personnel" contains "total personnel" → tier-2 hit
    warnings: list[ParseWarning] = []
    rows = _resolve_groups(groups, warnings)
    assert rows[0][0] == "Payroll"
    assert warnings == []


def test_resolve_groups_tier3_detail_fallback():
    """When subtotal + header don't match, fall back to a detail row keyword."""
    groups = [
        OpexGroup(
            subsection_header="Misc Expense",
            detail_rows=[("Real Estate Taxes Paid", 120_000.0)],
            subtotal_row=("Total Misc Expense", 120_000.0),
            indent_level=1,
        ),
    ]
    # "Total Misc Expense" and "Misc Expense" don't match _SUBTOTAL_CATEGORY_MAP
    # But "Real Estate Taxes Paid" hits _CATEGORY_MAP via tier-3
    warnings: list[ParseWarning] = []
    rows = _resolve_groups(groups, warnings)
    assert rows[0][0] == "Real Estate Taxes"
    assert warnings == []


def test_resolve_groups_tier4_other_opex_with_warning():
    """Completely unmapped group → other_opex + ParseWarning."""
    groups = [
        OpexGroup(
            subsection_header="Special Assessment",
            detail_rows=[("Unrecognized Line 1", 5_000.0)],
            subtotal_row=("Total Special Assessment", 5_000.0),
            indent_level=1,
        ),
    ]
    warnings: list[ParseWarning] = []
    rows = _resolve_groups(groups, warnings)
    assert rows[0][0] == "other_opex"
    assert len(warnings) == 1
    assert warnings[0].tier_reached == 4
    assert warnings[0].amount == 5_000.0
    assert warnings[0].category_assigned == "other_opex"


def test_resolve_groups_multiple_unmatched_aggregate_to_one_other_opex():
    """Two unmatched groups produce two warnings but must aggregate in the caller."""
    groups = [
        OpexGroup(
            subsection_header="Unknown A",
            detail_rows=[("Line A", 1_000.0)],
            subtotal_row=("Total Unknown A", 1_000.0),
            indent_level=1,
        ),
        OpexGroup(
            subsection_header="Unknown B",
            detail_rows=[("Line B", 2_000.0)],
            subtotal_row=("Total Unknown B", 2_000.0),
            indent_level=1,
        ),
    ]
    warnings: list[ParseWarning] = []
    rows = _resolve_groups(groups, warnings)
    # Both emit "other_opex" label — caller's += aggregation handles dedup
    assert all(label == "other_opex" for label, _ in rows)
    assert len(warnings) == 2


def test_resolve_groups_no_subtotal_sums_detail_rows():
    """Group without a subtotal row sums its detail rows."""
    groups = [
        OpexGroup(
            subsection_header="Payroll",
            detail_rows=[("Salary Row", 6_000.0), ("Benefits Row", 1_200.0)],
            subtotal_row=None,
            indent_level=1,
        ),
    ]
    warnings: list[ParseWarning] = []
    rows = _resolve_groups(groups, warnings)
    label, values = rows[0]
    assert label == "Payroll"
    assert sum(v for v in values if v is not None) == 7_200.0
```

- [ ] **Step 3.2: Run tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_t12_parser.py -k "resolve_groups" -v
```

Expected: `ImportError` — `_resolve_groups` does not exist yet.

- [ ] **Step 3.3: Implement `_resolve_groups()` in `engine/ingest/t12_parser.py`**

Insert this function immediately after `_detect_hierarchy()` (before `_extract_statement_rows()`):

```python
def _resolve_groups(
    groups: list[OpexGroup],
    warnings: list[ParseWarning],
) -> list[tuple[str, list[float | None]]]:
    """Pass 2: resolve each OpexGroup to a (canonical_label, [amount]) pair.

    Resolution tiers (stops at first match):
      1. subtotal_row text → _normalize_subtotal_category()
      2. subsection_header → _normalize_subtotal_category()
      3. first detail row that hits _matches_category_map()
      4. no match → ParseWarning appended to `warnings`; label = "other_opex"

    Value: subtotal_row amount when present; sum of detail_rows otherwise.
    Detail row amounts are never combined with the subtotal (prevents double-counting).
    """
    rows: list[tuple[str, list[float | None]]] = []

    for group in groups:
        if group.subtotal_row is not None:
            amount = group.subtotal_row[1]
        elif group.detail_rows:
            amount = sum(a for _, a in group.detail_rows)
        else:
            continue

        # Tier 1: subtotal text
        canonical: str | None = None
        if group.subtotal_row:
            canonical = _normalize_subtotal_category(group.subtotal_row[0])

        # Tier 2: subsection header
        if canonical is None and group.subsection_header:
            canonical = _normalize_subtotal_category(group.subsection_header)

        # Tier 3: detail rows via _CATEGORY_MAP
        if canonical is None:
            for detail_label, _ in group.detail_rows:
                match = _matches_category_map(detail_label)
                if match is not None:
                    canonical = match
                    break

        # Tier 4: no match
        if canonical is None:
            raw_label = (
                group.subtotal_row[0]
                if group.subtotal_row
                else group.subsection_header or "unknown"
            )
            warnings.append(
                ParseWarning(
                    tier_reached=4,
                    raw_label=raw_label,
                    amount=amount,
                    category_assigned="other_opex",
                )
            )
            canonical = "other_opex"

        rows.append((canonical, [amount]))

    return rows
```

- [ ] **Step 3.4: Run tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_t12_parser.py -k "resolve_groups" -v
```

Expected: all `test_resolve_groups_*` PASS.

- [ ] **Step 3.5: Run full suite**

```bash
.venv/bin/python -m pytest tests/test_t12_parser.py -v
```

Expected: all existing tests PASS.

- [ ] **Step 3.6: Commit**

```bash
git add engine/ingest/t12_parser.py tests/test_t12_parser.py
git commit -m "feat(t12-parser): add _resolve_groups() pass-2 category resolution"
```

---

## Task 4: Wire into `_extract_statement_rows()` + provenance threading

**Files:**
- Modify: `engine/ingest/t12_parser.py`
- Test: `tests/test_t12_parser.py`

The wiring requires two changes:
1. `_extract_statement_rows()` collects raw labels (pre-strip) during the existing first pass, detects hierarchy, and branches.
2. `_load_rows()` / `_load_csv()` / `_load_excel()` / `parse_t12_with_provenance()` thread a `warnings` list through so `ParseWarning` objects surface in the provenance dict.

- [ ] **Step 4.1: Add `_get_raw_label()` helper**

Add this function immediately before `_extract_statement_rows()`:

```python
def _get_raw_label(raw_row: Any) -> str:
    """Extract the raw (unstripped) label text from a row for indent detection.

    Mirrors the label-finding logic of _extract_label_and_values() but
    preserves leading whitespace so _text_indent_level() can compute depth.
    """
    row = list(raw_row)
    numeric_indices = [i for i, v in enumerate(row) if _to_float(v) is not None]
    if numeric_indices:
        first_num = min(numeric_indices)
        for i in range(first_num - 1, -1, -1):
            v = row[i]
            if v is not None and not _is_date_like(v):
                text = str(v)
                if text.strip():
                    return text
    for v in reversed(row):
        if v is not None and not _is_date_like(v):
            text = str(v)
            if text.strip():
                return text
    return ""
```

- [ ] **Step 4.2: Modify `_load_rows()`, `_load_csv()`, `_load_excel()` to accept and thread a `warnings` list**

Replace the current `_load_rows`, `_load_csv`, `_load_excel` (lines 212–242) with:

```python
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
    else:
        raise ValueError(f"Unsupported file type: {suffix}. Use .csv or .xlsx")


def _load_csv(
    path: Path,
    warnings: list[ParseWarning] | None = None,
) -> list[tuple[str, list[float | None]]]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        next(reader, None)
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
        ws = wb.active
        iterator = ws.iter_rows(values_only=True)
        next(iterator, None)
        return _extract_statement_rows(iterator, warnings)
    finally:
        wb.close()
```

- [ ] **Step 4.3: Modify `_extract_statement_rows()` to add hierarchy branch**

Replace the current `_extract_statement_rows()` function signature and the block between the existing first-pass loop and the second-pass loop. The full replacement:

```python
def _extract_statement_rows(
    raw_rows: Any,
    warnings: list[ParseWarning] | None = None,
) -> list[tuple[str, list[float | None]]]:
    """Extract expense rows from a T12/P&L export.

    When the OpEx section contains ≥2 subcategory subtotals (e.g. 'Total
    Personnel Expense'), activates the hierarchy-aware two-pass path which
    uses subtotal values as canonical amounts. Otherwise falls through to
    the original flat row-by-row path.
    """
    if warnings is None:
        warnings = []

    # Materialize so we can iterate twice and capture raw labels.
    all_raw = list(raw_rows)

    # ── First pass: boundary detection + prepare rows ──────────────────────
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

    # ── Collect OpEx-window rows for hierarchy detection ───────────────────
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

    # ── Hierarchy detection: branch on result ─────────────────────────────
    groups = _detect_hierarchy(opex_raw_rows)
    if groups:
        resolved = _resolve_groups(groups, warnings)
        # Aggregate other_opex across multiple unmatched groups (same key).
        totals: dict[str, float] = {}
        for canon_label, vals in resolved:
            amount = sum(v for v in vals if v is not None)
            totals[canon_label] = totals.get(canon_label, 0.0) + amount
        return [(label, [amount]) for label, amount in totals.items()]

    # ── Flat path: original logic (unchanged) ──────────────────────────────
    rows: list[tuple[str, list[float | None]]] = []
    current_macro_section = None
    after_income_boundary = False
    current_detail_section: str | None = None
    detail_seen_in_section = False

    for label, raw_label, values, has_numeric_values, macro_section, income_boundary in prepared_rows:
        if macro_section is not None and not has_numeric_values:
            current_macro_section = macro_section
            current_detail_section = None
            detail_seen_in_section = False
            continue
        if income_boundary:
            after_income_boundary = True
            if not contains_operating_section and current_macro_section == "income":
                current_macro_section = None
            current_detail_section = None
            detail_seen_in_section = False
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
            continue
        if contains_operating_section and current_macro_section != "operating_expenses":
            continue
        if contains_income_boundary and not contains_operating_section and not after_income_boundary:
            continue
        if current_macro_section == "non_operating_expenses":
            continue
        if current_macro_section == "income" and contains_income_boundary:
            continue
        if _is_summary_row(label):
            continue
        if _is_section_subtotal_row(label, current_detail_section, detail_seen_in_section):
            continue
        numeric_values = [value for value in values if value is not None]
        if len(numeric_values) > 12:
            numeric_values = numeric_values[:12]
        numeric_values = _strip_trailing_total_column(numeric_values)
        if not numeric_values:
            continue
        rows.append((label, numeric_values))
        detail_seen_in_section = True
    return rows
```

- [ ] **Step 4.4: Thread warnings through `parse_t12_with_provenance()`**

Replace `parse_t12_with_provenance()` body (lines 177–209) with:

```python
def parse_t12_with_provenance(
    path: str | Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Parse a T12 file and return (opex_table, provenance).

    provenance keys:
      "source_rows": dict[str, list[str]] — raw labels per canonical category
      "_warnings": list[dict] — ParseWarning dicts for unresolved groups
    """
    path = Path(path)
    _warnings: list[ParseWarning] = []
    rows = _load_rows(path, _warnings)

    totals: dict[str, float] = {}
    recoverables: dict[str, bool] = {}
    source_rows: dict[str, list[str]] = {}

    for category_raw, monthly_values in rows:
        if not category_raw or not monthly_values:
            continue
        canonical, recoverable = _normalize_category(category_raw)
        annual_total = sum(v for v in monthly_values if v is not None)
        if annual_total == 0:
            continue
        totals[canonical] = totals.get(canonical, 0.0) + annual_total
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
                "tier_reached": w.tier_reached,
                "raw_label": w.raw_label,
                "amount": w.amount,
                "category_assigned": w.category_assigned,
            }
            for w in _warnings
        ],
    }
    return opex_table, provenance
```

- [ ] **Step 4.5: Run the full test suite**

```bash
.venv/bin/python -m pytest tests/test_t12_parser.py -v
```

Expected: all existing tests PASS, all new tests PASS. Watch especially:
- `test_full_statement_csv_uses_operating_expense_section_only`
- `test_column_shifted_statement_finds_expense_labels_after_total_income`
- `test_excel_annualized_summary_columns_not_double_counted`
- `test_source_rows_provenance` — provenance now has an extra `"_warnings"` key; confirm the test still passes (it checks for specific keys in `water_entry`, not in `provenance`)

If `test_source_rows_provenance` fails because it asserts `provenance == {expected_dict}`, update it to access `provenance["Water & Sewer"]` instead of comparing the whole dict.

- [ ] **Step 4.6: Commit**

```bash
git add engine/ingest/t12_parser.py tests/test_t12_parser.py
git commit -m "feat(t12-parser): wire hierarchy path into _extract_statement_rows, thread ParseWarnings"
```

---

## Task 5: Integration test + error-memory update

**Files:**
- Create: `tests/fixtures/lennar_style_t12.csv`
- Modify: `tests/test_t12_parser.py`
- Modify: `.claude/error-memory.md`

- [ ] **Step 5.1: Create the Lennar-style fixture**

Create `tests/fixtures/lennar_style_t12.csv`:

```
Category,Jan,Feb,Mar,Apr,May,Jun,Jul,Aug,Sep,Oct,Nov,Dec
REVENUE,,,,,,,,,,,,
Gross Potential Rent,50000,50000,50000,50000,50000,50000,50000,50000,50000,50000,50000,50000
TOTAL REVENUE,50000,50000,50000,50000,50000,50000,50000,50000,50000,50000,50000,50000
OPERATING EXPENSES,,,,,,,,,,,,
  Personnel Expense,,,,,,,,,,,,
    513110 - Salary - Manager,7000,7000,7000,7000,7000,7000,7000,7000,7000,7000,7000,7000
    513120 - Bonuses And Incentives,500,500,500,500,500,500,500,500,500,500,500,500
    513130 - Overtime,200,200,200,200,200,200,200,200,200,200,200,200
  Total Personnel Expense,7700,7700,7700,7700,7700,7700,7700,7700,7700,7700,7700,7700
  Utilities,,,,,,,,,,,,
    516110 - Electricity - Common,1500,1500,1500,1500,1500,1500,1500,1500,1500,1500,1500,1500
    516210 - Water And Sewer,800,800,800,800,800,800,800,800,800,800,800,800
  Total Utilities,2300,2300,2300,2300,2300,2300,2300,2300,2300,2300,2300,2300
  Administrative,,,,,,,,,,,,
    517110 - Office Supplies,100,100,100,100,100,100,100,100,100,100,100,100
    517120 - Postage And Delivery,50,50,50,50,50,50,50,50,50,50,50,50
  Total Administrative,150,150,150,150,150,150,150,150,150,150,150,150
TOTAL OPERATING EXPENSES,10150,10150,10150,10150,10150,10150,10150,10150,10150,10150,10150,10150
```

- [ ] **Step 5.2: Write the integration tests**

Add at the bottom of `tests/test_t12_parser.py`:

```python
# ── Task 5: Integration — Lennar-style hierarchical T12 ──────────────────────

LENNAR_FIXTURE = Path("tests/fixtures/lennar_style_t12.csv")


def test_lennar_fixture_no_account_code_leakthrough():
    """No raw account-code strings (e.g. '513110 - Salary') in canonical names."""
    result = parse_t12(LENNAR_FIXTURE)
    names = {r["category_name"] for r in result}
    for name in names:
        assert not re.match(r"^\d{5,6}", name), (
            f"Account-code leakthrough: {name!r} should not appear as a canonical category"
        )


def test_lennar_fixture_uses_subtotal_values_not_detail_sums():
    """Payroll canonical value must equal the Total Personnel Expense subtotal (7700 × 12),
    not the sum of individual account rows which would give the same number if summed
    individually but would double-count if both subtotals AND detail rows were included."""
    result = parse_t12(LENNAR_FIXTURE)
    by_name = {r["category_name"]: r["base_value"] for r in result}
    # 7700/month × 12 = 92400 (from subtotal, not from summing 7000+500+200 and the subtotal)
    assert by_name["Payroll"] == 92_400.0
    assert by_name["Utilities"] == 27_600.0   # 2300 × 12
    assert by_name["Administrative"] == 1_800.0  # 150 × 12


def test_lennar_fixture_exactly_three_categories():
    """Only Payroll, Utilities, Administrative — no extra leakthrough rows."""
    result = parse_t12(LENNAR_FIXTURE)
    assert len(result) == 3, f"Expected 3 categories, got: {[r['category_name'] for r in result]}"


def test_lennar_fixture_no_other_opex():
    """All three OpEx groups resolve cleanly — no other_opex bucket."""
    result = parse_t12(LENNAR_FIXTURE)
    names = {r["category_name"] for r in result}
    assert "other_opex" not in names


def test_lennar_fixture_no_parse_warnings():
    """All groups map to known canonical names — zero ParseWarnings in provenance."""
    _, provenance = parse_t12_with_provenance(LENNAR_FIXTURE)
    assert provenance.get("_warnings", []) == [], (
        f"Unexpected warnings: {provenance['_warnings']}"
    )


def test_lennar_fixture_provenance_source_rows():
    """Source rows for Payroll show the subtotal label, not the account codes."""
    _, provenance = parse_t12_with_provenance(LENNAR_FIXTURE)
    assert "Payroll" in provenance
    payroll_sources = provenance["Payroll"]
    # The hierarchical path uses the subtotal as the source row label
    assert any("Total Personnel" in s or "Personnel" in s for s in payroll_sources)


def test_existing_flat_fixture_still_works_alongside_lennar():
    """Flat fixture (sample_t12.csv) unaffected by hierarchy code path."""
    flat_result = parse_t12(FIXTURE)          # FIXTURE = tests/fixtures/sample_t12.csv
    lennar_result = parse_t12(LENNAR_FIXTURE)
    # Both parse without error; flat result has 11 categories as before
    assert len(flat_result) == 11
    assert len(lennar_result) == 3
```

- [ ] **Step 5.3: Run the integration tests**

```bash
.venv/bin/python -m pytest tests/test_t12_parser.py -k "lennar_fixture" -v
```

Expected: all `test_lennar_fixture_*` PASS.

- [ ] **Step 5.4: Run the full suite one final time**

```bash
.venv/bin/python -m pytest tests/test_t12_parser.py -v
```

Expected: all tests PASS.

- [ ] **Step 5.5: Update `.claude/error-memory.md`**

Find the entry containing "Lennar/OneSite indented P&L T12" and replace it with:

```markdown
### Lennar/OneSite T12 — account-code leakthrough (FIXED 2026-05-14)

**Misdiagnosis in prior entry:** The bug was described as "all-null opex_table".
That was wrong. The parser *did* return entries — 62 of them — but 49 were raw
account-code strings like `513120 - Bonuses And Incentives` that didn't match
any `_CATEGORY_MAP` pattern. Subcategory subtotals like "Total Personnel Expense"
were silently removed by `_is_summary_row()`.

**Root cause:** `_is_summary_row()` bans ALL rows starting with "Total ", including
valid subcategory subtotals. `_CATEGORY_MAP` had ~44 keyword patterns covering common
human-readable labels but none for account-code descriptions (e.g., "Bonuses And Incentives").

**Fix applied (2026-05-14):** Hierarchy-aware two-pass parser.
- Pass 1 (`_detect_hierarchy()`): groups OpEx rows into `OpexGroup` objects using
  `Total X` markers. Returns empty list for flat T12s (existing path unchanged).
- Pass 2 (`_resolve_groups()`): maps each group to a canonical name via
  `_SUBTOTAL_CATEGORY_MAP` (tier-1: subtotal text, tier-2: subsection header),
  then `_CATEGORY_MAP` (tier-3: detail row fallback), then `other_opex` + `ParseWarning`.
- `_is_summary_row()` untouched — still used by the flat path.
- New `_SUBTOTAL_CATEGORY_MAP` is separate from `_CATEGORY_MAP` to prevent
  "total X" patterns from firing against detail rows during tier-3.

**Test fixture:** `tests/fixtures/lennar_style_t12.csv`
**Spec:** `docs/superpowers/specs/2026-05-14-t12-parser-redesign.md`

**Formats covered by the hierarchy path:** RealPage (Archetype B), 360 Market Square (C),
AppFolio (D), JC Hart (F), CRP (G), MRI (H) — all use `Total X` Title Case subtotals.
**Not covered (out of scope):** Yardi ALLCAPS style (A), Entrata bare-description subtotals (J).
```

- [ ] **Step 5.6: Final commit**

```bash
git add tests/fixtures/lennar_style_t12.csv tests/test_t12_parser.py .claude/error-memory.md
git commit -m "feat(t12-parser): integration test + correct error-memory diagnosis for Lennar T12"
```

---

## Self-Review

**Spec coverage check:**

| Spec section | Task covering it |
|---|---|
| `OpexGroup`, `ParseWarning` dataclasses | Task 1 |
| `_SUBTOTAL_CATEGORY_MAP` (separate from `_CATEGORY_MAP`) | Task 1 |
| `_normalize_subtotal_category()` with `.lower()`, no account-code strip | Task 1 |
| `_is_subcategory_subtotal()`, `_matches_section_total()` | Task 1 |
| Pass 1 grouping algorithm with flush guard | Task 2 |
| Flat detection (`< 2 subtotals → []`) | Task 2 |
| Tier 1–4 resolution, `other_opex` with `+=` aggregation | Task 3 |
| `_is_summary_row()` retained for flat path | Task 4 (flat path block unchanged) |
| ParseWarnings in provenance `"_warnings"` key | Task 4 |
| Test: Lennar hierarchical fixture | Task 5 |
| Test: flat regression | Task 5 (`test_existing_flat_fixture_still_works_alongside_lennar`) |
| Test: unmatched group → `other_opex` + warning | Task 3 |
| Error-memory update | Task 5 |

All spec requirements covered. No gaps.

**Placeholder scan:** No TBDs, no "implement later" comments. All code blocks are complete.

**Type consistency check:**
- `OpexGroup.detail_rows: list[tuple[str, float]]` — consistent across Tasks 1, 2, 3
- `_detect_hierarchy()` input: `list[tuple[str, str, list[float | None]]]` — consistent with Task 4 collection logic
- `_resolve_groups()` returns `list[tuple[str, list[float | None]]]` — compatible with `rows.append((label, numeric_values))` in flat path
- `ParseWarning` fields match between Task 1 definition and Task 3 construction
- `provenance["_warnings"]` key name consistent across Tasks 4 and 5
