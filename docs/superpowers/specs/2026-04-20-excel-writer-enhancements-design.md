# Task 2.2 — Excel Writer Enhancements

**Date:** 2026-04-20
**Status:** Approved
**Complexity:** L

---

## Summary

Add conditional formatting, named ranges, and dynamic print areas to `XlsmWriter`. Freeze panes and page setup already exist — this task completes the remaining Excel UX features needed for LP-quality workbooks.

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| CF rule storage | Inline in worksheet XML (no styles.xml modification) | Avoids touching the complex template styles.xml; colorScale/font elements work inline |
| Named ranges location | `xl/workbook.xml` `<definedNames>` | Standard Excel location; also handles print areas |
| Workbook XML modification | Lazy-parse + surgical modify (same pattern as sheets) | Consistent with existing architecture |
| Print area mechanism | `<definedName name="_xlnm.Print_Area">` in workbook.xml | Standard Excel mechanism; dynamic range passed by caller |
| CF API style | Hybrid helper with 3 rule types | Clean calling code in rediq_output.py while maintaining XML robustness |

## Existing (Already Implemented)

These methods already exist on `XlsmWriter` and are already called in `rediq_output.py`:
- `set_freeze_pane(sheet, row, col)` — freezes at specified cell
- `set_page_setup(sheet, orientation, fit_to_width, fit_to_height)` — page layout
- `set_header_footer(sheet, header, footer)` — branded headers/footers

## New Methods on `XlsmWriter`

### `add_conditional_format(sheet_name, cell_range, rule_type, **kwargs)`

Inserts a `<conditionalFormatting>` block into the worksheet XML. Three rule types:

**`rule_type="threshold"`** — Multi-threshold color rules (e.g., DSCR bands)
```python
writer.add_conditional_format("CF Calculations", "S86:EI86", "threshold",
    thresholds=[
        {"operator": "lessThan", "value": 1.0, "color": "FF0000"},      # red
        {"operator": "between", "value": [1.0, 1.25], "color": "FFFF00"}, # yellow
        {"operator": "greaterThan", "value": 1.25, "color": "00FF00"},   # green
    ]
)
```

Generates per-threshold `<cfRule type="cellIs">` with `<font><color rgb="..."/></font>` inside a `<dxf>`-less inline format. Uses `stopIfTrue="1"` for priority ordering.

**`rule_type="gradient"`** — Two or three-color scale
```python
writer.add_conditional_format("CF Calculations", "E155:N155", "gradient",
    min_color="FF0000", mid_color="FFFF00", max_color="00FF00"
)
```

Generates `<cfRule type="colorScale">` with `<colorScale><cfvo/><color/>` children.

**`rule_type="negative_red"`** — Negative values highlighted red
```python
writer.add_conditional_format("CF Calculations", "S100:EI100", "negative_red")
```

Generates `<cfRule type="cellIs" operator="lessThan">` with value 0 and red font color.

**XML structure generated:**
```xml
<conditionalFormatting sqref="S86:EI86">
  <cfRule type="cellIs" dxfId="0" priority="1" operator="lessThan" stopIfTrue="1">
    <formula>1.0</formula>
  </cfRule>
</conditionalFormatting>
```

Note: Since we avoid modifying `styles.xml`, CF rules use inline color specification via the `<colorScale>` element for gradients. For threshold rules, we embed formatting directly using `type="cellIs"` with formula-based conditions and reference a `dxfId` — but since we can't add to styles.xml, we'll use the alternative approach: `<cfRule>` elements with `type="cellIs"` and a `<formula>` child, paired with a `<dxf>` collection appended to the `<styleSheet>` in styles.xml.

**Revised approach for threshold/negative_red:** We WILL append `<dxf>` entries to `xl/styles.xml` — but only to the `<dxfs>` collection (a simple append, not modifying existing entries). This is safe because `<dxfs>` is specifically designed for conditional format styles and is append-only. The `count` attribute on `<dxfs>` must be updated to match.

### `define_named_range(name, sheet_name, cell_ref)`

Adds a `<definedName>` entry to `xl/workbook.xml`.

```python
writer.define_named_range("LeveredIRR", "CF Calculations", "E155")
```

Generates in workbook.xml:
```xml
<definedNames>
  <definedName name="LeveredIRR">'CF Calculations'!$E$155</definedName>
</definedNames>
```

For range references (multiple cells):
```python
writer.define_named_range("AnnualDebtService", "CF Calculations", "E92:N92")
```

Generates:
```xml
<definedName name="AnnualDebtService">'CF Calculations'!$E$92:$N$92</definedName>
```

**Implementation:** Lazy-parse `xl/workbook.xml` on first call. Find or create `<definedNames>` element. Append new entries. Serialize modified workbook.xml at save time (same pattern as sheets).

### `set_print_area(sheet_name, start_row, start_col, end_row, end_col)` (fix existing stub)

Uses the named range mechanism with reserved name `_xlnm.Print_Area`:

```python
writer.set_print_area("CF Calculations", 1, 1, 158, 139)
```

Generates:
```xml
<definedName name="_xlnm.Print_Area" localSheetId="21">'CF Calculations'!$A$1:$EI$158</definedName>
```

The `localSheetId` is the 0-based sheet index from workbook.xml's `<sheets>` collection.

## Named Ranges — Full LP Reporting Set (18)

| Named Range | Sheet | Cell/Range | Source |
|-------------|-------|-----------|--------|
| `LeveredIRR` | CF Calculations | Row 155, col E | IRR output |
| `UnleveredIRR` | CF Calculations | Row 153, col E | IRR output |
| `LeveredEM` | CF Calculations | Row 156, col E | EM output |
| `UnleveredEM` | CF Calculations | Row 154, col E | EM output |
| `PartnershipIRR` | CF Calculations | Row 157, col E | Partnership IRR |
| `PartnershipEM` | CF Calculations | Row 158, col E | Partnership EM |
| `NOI_Year1` | CF Calculations | Row 55, col E | NOI Year 1 |
| `GoingInCap` | Input | Dynamic (layout detection) | Going-in cap |
| `ExitCap` | Input | Dynamic (layout detection) | Exit cap rate |
| `AvgDSCR` | CF Calculations | Row 148, col E | Average DSCR |
| `MinDSCR` | CF Calculations | Row 149, col E | Min DSCR |
| `PurchasePrice` | CF Calculations | Row 86, col D | Purchase price |
| `TotalEquity` | CF Calculations | Row 108, col D | Equity basis |
| `AnnualDebtService` | CF Calculations | Row 92, cols E:N | Annual DS |
| `RefiProceeds` | CF Calculations | Row 110, col D | Refi net proceeds |
| `DispositionFee` | CF Calculations | Row 128, exit year col | Disposition fee |
| `PromoteGP` | Waterfall | Summary GP total cell | GP promote |
| `PromoteLP` | Waterfall | Summary LP total cell | LP promote |

Note: Exact row/col references will be confirmed during implementation against the actual `write_*` functions in `rediq_output.py`. The rows listed above match the current CF Calculations fixed-row layout.

## Conditional Formatting Rules Applied

### CF Calculations Sheet

| Row(s) | Range | Rule | Colors |
|--------|-------|------|--------|
| 86 (DSCR annual) | E:N | threshold | <1.0 red, 1.0-1.25 yellow, >1.25 green |
| 87 (DSCR monthly) | S:EI | threshold | <1.0 red, 1.0-1.25 yellow, >1.25 green |
| 100 (Levered CF annual) | E:N | negative_red | Negative = red |
| 155 (Levered IRR) | E | gradient | red → yellow → green (0% → 10% → 25%) |
| 156 (Levered EM) | E | gradient | red → yellow → green (1.0 → 1.5 → 2.5) |

### Input Sheet (Sensitivity Tables)

| Section | Range | Rule | Colors |
|---------|-------|------|--------|
| Rent sensitivity | Dynamic | gradient | red → green |
| Cap rate sensitivity | Dynamic | gradient | green → red (inverse) |

### Waterfall Sheet

| Row(s) | Range | Rule | Colors |
|--------|-------|------|--------|
| Promote distribution rows | C:P | negative_red | Negative = red |

## Dynamic Print Areas Applied

| Sheet | Range | Notes |
|-------|-------|-------|
| CF Calculations | Row 1 to 158, Col A to EI | Full cashflow model |
| Input | Row 1 to last data row, Col A to M | Varies by floor plan count |
| Waterfall | Row 1 to 69, Col A to EI | Full waterfall |
| Summary | Row 1 to last row, Col A to B | If summary tab present |

The Input sheet range is determined after `write_input_sheet()` completes — the function already tracks the last row written.

## Internal Architecture

### Workbook XML Modification

New internal state on `XlsmWriter`:
- `self._workbook_xml: Optional[etree._Element]` — parsed workbook.xml (lazy)
- `self._styles_xml: Optional[etree._Element]` — parsed styles.xml (lazy, for dxf append)
- `self._workbook_modified: bool` — flag for save
- `self._styles_modified: bool` — flag for save
- `self._named_ranges: List[tuple]` — accumulated (name, sheet, ref) entries
- `self._cf_dxf_count: int` — tracks appended dxf entries for cfRule dxfId assignment

### Save Modifications

`save()` updated to also:
1. Serialize `xl/workbook.xml` if `_workbook_modified`
2. Serialize `xl/styles.xml` if `_styles_modified`
3. Same atomic-write pattern as sheets

### Sheet Index Lookup

For `localSheetId` in print area definedNames, use the `<sheets>` collection order in workbook.xml (already parsed for `_build_sheet_map`).

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `engine/xlsx_writer.py` | `add_conditional_format()`, `define_named_range()`, fix `set_print_area()`, workbook/styles XML parsing + save |
| Modify | `engine/rediq_output.py` | Apply CF rules, named ranges, and print areas in `generate_rediq_workbook()` |
| Create | `tests/test_xlsx_enhancements.py` | 12 tests |

## Test Plan (12 tests)

| # | Test | What it verifies |
|---|------|-----------------|
| 1 | Threshold CF rule XML | Correct `<conditionalFormatting>` + `<cfRule type="cellIs">` structure |
| 2 | Gradient CF rule XML | Correct `<colorScale>` with min/mid/max color elements |
| 3 | Negative_red CF rule XML | `operator="lessThan"` with formula 0 |
| 4 | Multiple CF rules on same sheet | Multiple `<conditionalFormatting>` blocks, correct priorities |
| 5 | Named range in workbook.xml | `<definedName>` with correct sheet reference and absolute cell ref |
| 6 | Named range with range ref | Multi-cell range (e.g., `$E$92:$N$92`) |
| 7 | Print area definedName | `_xlnm.Print_Area` with correct `localSheetId` |
| 8 | Dynamic print area | Different ranges for different calls produce correct XML |
| 9 | DXF append to styles.xml | `<dxfs count="N">` updated, new `<dxf>` entries appended |
| 10 | Save round-trip | Write CF + named ranges → save → re-read ZIP → verify both workbook.xml and sheet XML modified |
| 11 | No clobber existing definedNames | Template's existing named ranges preserved after adding new ones |
| 12 | Integration: generate_rediq_workbook | Full pipeline produces workbook with CF rules and named ranges present in XML |

## Out of Scope

- No new CLI flags (enhancements applied automatically during generation)
- No conditional formatting for portfolio workbooks (only RedIQ clone)
- No data validation dropdowns (separate concern)
- No chart modifications (preserved from template)
- No sparklines
