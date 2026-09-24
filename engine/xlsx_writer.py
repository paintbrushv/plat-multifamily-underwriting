"""
ZIP-Level XLSM Writer
======================
Edits .xlsm files by surgically modifying worksheet XML inside the ZIP archive.

Unlike openpyxl (which deserializes the entire workbook and re-serializes it,
corrupting VBA, conditional formatting, styles, and VML objects), this writer:

  1. Copies the template ZIP byte-for-byte
  2. Parses only the worksheet XMLs that need cell edits
  3. Writes values into <c> elements, preserving styles and merge ranges
  4. Replaces only the modified sheet XMLs in the output ZIP

Everything else — VBA macros, styles.xml, drawings, charts — is untouched.

Usage:
    from engine.xlsx_writer import XlsmWriter

    with XlsmWriter(template_path, output_path) as writer:
        writer.set_cell_value("CF Calculations", 18, 19, 98765.43)
        writer.set_cell_value("Input", 10, 5, "Deal Name")
"""

from __future__ import annotations

import os
import re
import tempfile
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Optional, Set, Union

from lxml import etree

# SpreadsheetML namespace
NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
NSMAP = {"s": NS}


# ── Helpers ──────────────────────────────────────────────────────────────

def col_to_letter(col: int) -> str:
    """1-based column number → Excel column letter(s).  1→A, 19→S, 139→EI."""
    result = []
    while col > 0:
        col, rem = divmod(col - 1, 26)
        result.append(chr(65 + rem))
    return "".join(reversed(result))


def cell_ref(row: int, col: int) -> str:
    """(row, col) → Excel cell reference.  (18, 19) → 'S18'."""
    return f"{col_to_letter(col)}{row}"


def _col_from_ref(ref: str) -> int:
    """Extract 1-based column number from cell reference like 'S18' → 19."""
    letters = re.match(r"([A-Z]+)", ref).group(1)
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n


def _row_from_ref(ref: str) -> int:
    """Extract row number from cell reference like 'S18' → 18."""
    return int(re.search(r"(\d+)$", ref).group(1))


# Excel serial date epoch: Jan 0, 1900 (with Lotus 1-2-3 leap year bug)
_EXCEL_EPOCH = date(1899, 12, 30)


def _to_excel_serial(dt: Union[date, datetime]) -> int:
    """Convert Python date/datetime → Excel serial number (with Lotus bug)."""
    if isinstance(dt, datetime):
        dt = dt.date()
    delta = dt - _EXCEL_EPOCH
    serial = delta.days
    # Lotus 1-2-3 bug: Excel thinks 1900 was a leap year, so dates after
    # Feb 28, 1900 are off by +1
    if serial >= 60:
        serial += 1
    return serial


# ── XlsmWriter ───────────────────────────────────────────────────────────

class XlsmWriter:
    """ZIP-level XML writer for .xlsm files.

    Opens the template, maps sheet names → XML paths, lazily parses
    worksheet XML on first write, and saves by copying the ZIP with
    only modified sheet XMLs replaced.
    """

    def __init__(self, template_path: Union[str, Path], output_path: Union[str, Path]):
        self.template_path = Path(template_path)
        self.output_path = Path(output_path)

        if not self.template_path.exists():
            raise FileNotFoundError(f"Template not found: {self.template_path}")

        # Map sheet name → ZIP entry path
        self._sheet_map: Dict[str, str] = {}
        self._build_sheet_map()

        # Lazy-loaded parsed XML trees: zip_entry_path → (etree root, sheetData element)
        self._parsed: Dict[str, etree._Element] = {}
        # Track which ZIP entries were modified
        self._modified: set[str] = set()

        # Workbook-level XML (lazy-loaded for named ranges / print areas)
        self._workbook_xml: Optional[etree._Element] = None
        self._workbook_modified: bool = False

        # Styles XML (lazy-loaded for conditional format dxf entries)
        self._styles_xml: Optional[etree._Element] = None
        self._styles_modified: bool = False
        self._next_dxf_id: int = 0
        self._cf_priority: int = 1

    def _build_sheet_map(self):
        """Parse workbook.xml + workbook.xml.rels to map sheet names → XML paths."""
        with zipfile.ZipFile(self.template_path, "r") as zf:
            # 1. Read workbook.xml for sheet name → rId mapping
            wb_xml = zf.read("xl/workbook.xml")
            wb_root = etree.fromstring(wb_xml)
            name_to_rid: Dict[str, str] = {}
            for sheet_el in wb_root.findall(f".//{{{NS}}}sheet"):
                name = sheet_el.get("name")
                rid = sheet_el.get(f"{{{NS_R}}}id")
                if name and rid:
                    name_to_rid[name] = rid

            # 2. Read workbook.xml.rels for rId → Target path mapping
            rels_xml = zf.read("xl/_rels/workbook.xml.rels")
            rels_root = etree.fromstring(rels_xml)
            rid_to_target: Dict[str, str] = {}
            for rel in rels_root.findall(f"{{{NS_PKG}}}Relationship"):
                rid = rel.get("Id")
                target = rel.get("Target")
                if rid and target:
                    rid_to_target[rid] = target

            # 3. Combine: sheet name → ZIP entry path
            for name, rid in name_to_rid.items():
                target = rid_to_target.get(rid, "")
                if target:
                    # Target may be absolute (/xl/worksheets/...) or relative (worksheets/...)
                    if target.startswith("/"):
                        zip_path = target.lstrip("/")
                    else:
                        zip_path = f"xl/{target}"
                    self._sheet_map[name] = zip_path

    def _get_sheet_xml(self, sheet_name: str) -> etree._Element:
        """Lazy-load and parse worksheet XML, returning the root element."""
        zip_path = self._sheet_map.get(sheet_name)
        if zip_path is None:
            raise KeyError(
                f"Sheet '{sheet_name}' not found. "
                f"Available: {list(self._sheet_map.keys())}"
            )

        if zip_path not in self._parsed:
            with zipfile.ZipFile(self.template_path, "r") as zf:
                raw = zf.read(zip_path)
            self._parsed[zip_path] = etree.fromstring(raw)

        return self._parsed[zip_path]

    def _get_sheet_data(self, root: etree._Element) -> etree._Element:
        """Find the <sheetData> element within a worksheet root."""
        sd = root.find(f"{{{NS}}}sheetData")
        if sd is None:
            raise ValueError("Worksheet XML has no <sheetData> element")
        return sd

    def _find_or_create_row(
        self, sheet_data: etree._Element, row_num: int
    ) -> etree._Element:
        """Find existing <row r="N"> or create one at the correct sorted position."""
        for row_el in sheet_data.findall(f"{{{NS}}}row"):
            r = int(row_el.get("r", "0"))
            if r == row_num:
                return row_el
            if r > row_num:
                # Insert before this row
                new_row = etree.Element(f"{{{NS}}}row", r=str(row_num))
                row_el.addprevious(new_row)
                return new_row

        # All existing rows have lower numbers — append
        new_row = etree.SubElement(sheet_data, f"{{{NS}}}row", r=str(row_num))
        return new_row

    def _find_or_create_cell(
        self, row_el: etree._Element, row_num: int, col_num: int
    ) -> etree._Element:
        """Find existing <c r="XX"> or create one at the correct sorted position."""
        ref = cell_ref(row_num, col_num)

        for cell_el in row_el.findall(f"{{{NS}}}c"):
            cell_r = cell_el.get("r", "")
            if cell_r == ref:
                return cell_el
            # Compare by column position for insertion ordering
            existing_col = _col_from_ref(cell_r)
            if existing_col > col_num:
                new_cell = etree.Element(f"{{{NS}}}c", r=ref)
                cell_el.addprevious(new_cell)
                return new_cell

        # All existing cells in this row are to the left — append
        new_cell = etree.SubElement(row_el, f"{{{NS}}}c", r=ref)
        return new_cell

    def set_cell_value(
        self,
        sheet_name: str,
        row: int,
        col: int,
        value: Any,
    ) -> None:
        """Set a cell's value, preserving its style and formula.

        Handles:
          - Numbers (int, float) → <v>value</v>
          - Strings → inline string <is><t>value</t></is>
          - Dates → Excel serial number
          - None → clears value
          - If the cell has a formula, keeps it and sets <v> as cached value
            (Excel will recalculate on open). Call remove_formula() first
            if you need to replace a formula with a static value.
        """
        if value is None:
            return

        root = self._get_sheet_xml(sheet_name)
        sheet_data = self._get_sheet_data(root)
        row_el = self._find_or_create_row(sheet_data, row)
        cell_el = self._find_or_create_cell(row_el, row, col)

        # Mark this sheet as modified
        zip_path = self._sheet_map[sheet_name]
        self._modified.add(zip_path)

        # Check if cell has a formula — if so, preserve it and just set <v>
        has_formula = cell_el.find(f"{{{NS}}}f") is not None

        # Remove existing <v> and <is> (but NOT <f> — formulas are preserved)
        for child_tag in ("v", "is"):
            existing = cell_el.find(f"{{{NS}}}{child_tag}")
            if existing is not None:
                cell_el.remove(existing)

        # Convert dates to serial numbers
        if isinstance(value, (date, datetime)):
            value = _to_excel_serial(value)

        # Write value
        if has_formula:
            # Cell has a formula — just set <v> as cached value, keep t attribute
            v_el = etree.SubElement(cell_el, f"{{{NS}}}v")
            if isinstance(value, str):
                v_el.text = value
            elif isinstance(value, (int, float)):
                if isinstance(value, float):
                    v_el.text = f"{value:.10g}"
                else:
                    v_el.text = str(value)
            else:
                v_el.text = str(value)
        elif isinstance(value, str):
            # Inline string
            cell_el.set("t", "inlineStr")
            is_el = etree.SubElement(cell_el, f"{{{NS}}}is")
            t_el = etree.SubElement(is_el, f"{{{NS}}}t")
            t_el.text = value
        elif isinstance(value, (int, float)):
            # Numeric
            cell_el.set("t", "n")
            v_el = etree.SubElement(cell_el, f"{{{NS}}}v")
            # Use repr-level precision for floats, avoid scientific notation
            if isinstance(value, float):
                v_el.text = f"{value:.10g}"
            else:
                v_el.text = str(value)
        else:
            # Fallback: convert to string
            cell_el.set("t", "inlineStr")
            is_el = etree.SubElement(cell_el, f"{{{NS}}}is")
            t_el = etree.SubElement(is_el, f"{{{NS}}}t")
            t_el.text = str(value)

    def remove_formula(self, sheet_name: str, row: int, col: int) -> None:
        """Remove the formula from a cell, keeping its style.

        Call this before set_cell_value() when you need to replace a formula
        with a static value (e.g., CF Calculations monthly data).
        """
        root = self._get_sheet_xml(sheet_name)
        sheet_data = self._get_sheet_data(root)

        # Find the row — don't create if missing (no formula to remove)
        for row_el in sheet_data.findall(f"{{{NS}}}row"):
            if int(row_el.get("r", "0")) == row:
                ref = cell_ref(row, col)
                for cell_el in row_el.findall(f"{{{NS}}}c"):
                    if cell_el.get("r") == ref:
                        f_el = cell_el.find(f"{{{NS}}}f")
                        if f_el is not None:
                            cell_el.remove(f_el)
                            zip_path = self._sheet_map[sheet_name]
                            self._modified.add(zip_path)
                        return
                return
        return

    @staticmethod
    def _expand_self_closing_tags(xml_bytes: bytes) -> bytes:
        """Expand self-closing XML tags to explicit open/close form.

        Excel requires <v></v> not <v/>, <c ...></c> not <c .../>, etc.
        While semantically identical in XML, Excel's parser treats them
        differently and may corrupt formatting or report errors.
        """
        # Match self-closing tags: <tagname attrs/>
        # Captures: (1) tag name, (2) attributes including whitespace
        return re.sub(
            rb"<([\w:]+)((?:\s[^>]*)?)/>",
            rb"<\1\2></\1>",
            xml_bytes,
        )

    def set_freeze_pane(
        self,
        sheet_name: str,
        row: int,
        col: int,
    ) -> None:
        """Freeze panes at the specified cell (row/col 1-based).

        Freezes rows above and columns to the left of the specified cell.
        E.g., set_freeze_pane("Sheet1", 3, 2) freezes rows 1-2 and column A.

        Args:
            sheet_name: Target sheet name
            row: First unfrozen row (1-based)
            col: First unfrozen column (1-based)
        """
        root = self._get_sheet_xml(sheet_name)
        zip_path = self._sheet_map[sheet_name]

        # Find or create <sheetViews> → <sheetView>
        views = root.find(f"{{{NS}}}sheetViews")
        if views is None:
            # Insert before sheetData
            sheet_data = root.find(f"{{{NS}}}sheetData")
            idx = list(root).index(sheet_data) if sheet_data is not None else 0
            views = etree.Element(f"{{{NS}}}sheetViews")
            root.insert(idx, views)

        view = views.find(f"{{{NS}}}sheetView")
        if view is None:
            view = etree.SubElement(views, f"{{{NS}}}sheetView")
            view.set("workbookViewId", "0")

        # Remove existing <pane> and <selection>
        for old in view.findall(f"{{{NS}}}pane"):
            view.remove(old)
        for old in view.findall(f"{{{NS}}}selection"):
            view.remove(old)

        # Create <pane>
        ref = cell_ref(row, col)
        pane = etree.SubElement(view, f"{{{NS}}}pane")

        if row > 1 and col > 1:
            pane.set("xSplit", str(col - 1))
            pane.set("ySplit", str(row - 1))
            pane.set("topLeftCell", ref)
            pane.set("activePane", "bottomRight")
            pane.set("state", "frozen")
        elif row > 1:
            pane.set("ySplit", str(row - 1))
            pane.set("topLeftCell", ref)
            pane.set("activePane", "bottomLeft")
            pane.set("state", "frozen")
        elif col > 1:
            pane.set("xSplit", str(col - 1))
            pane.set("topLeftCell", ref)
            pane.set("activePane", "topRight")
            pane.set("state", "frozen")

        self._modified.add(zip_path)

    def set_print_area(
        self,
        sheet_name: str,
        start_row: int,
        start_col: int,
        end_row: int,
        end_col: int,
    ) -> None:
        """Define the print area for a sheet via workbook.xml definedNames.

        Uses the reserved name '_xlnm.Print_Area' with localSheetId.

        Args:
            sheet_name: Target sheet name
            start_row, start_col: Top-left cell (1-based)
            end_row, end_col: Bottom-right cell (1-based)
        """
        root = self._get_workbook_xml()

        # Determine localSheetId from <sheets> order
        sheets_el = root.find(f"{{{NS}}}sheets")
        local_id = None
        if sheets_el is not None:
            for idx, sheet in enumerate(sheets_el.findall(f"{{{NS}}}sheet")):
                if sheet.get("name") == sheet_name:
                    local_id = str(idx)
                    break

        if local_id is None:
            return  # Sheet not found in workbook

        # Find or create <definedNames>
        defined_names = root.find(f"{{{NS}}}definedNames")
        if defined_names is None:
            sheets_el = root.find(f"{{{NS}}}sheets")
            idx = list(root).index(sheets_el) + 1 if sheets_el is not None else len(root)
            defined_names = etree.Element(f"{{{NS}}}definedNames")
            root.insert(idx, defined_names)

        # Remove existing print area for this sheet (if any)
        for existing in defined_names.findall(f"{{{NS}}}definedName"):
            if (existing.get("name") == "_xlnm.Print_Area"
                    and existing.get("localSheetId") == local_id):
                defined_names.remove(existing)
                break

        # Build reference
        start_ref = f"${col_to_letter(start_col)}${start_row}"
        end_ref = f"${col_to_letter(end_col)}${end_row}"
        ref_str = f"'{sheet_name}'!{start_ref}:{end_ref}"

        # Create definedName entry
        dn = etree.SubElement(defined_names, f"{{{NS}}}definedName")
        dn.set("name", "_xlnm.Print_Area")
        dn.set("localSheetId", local_id)
        dn.text = ref_str

        self._workbook_modified = True

    def set_page_setup(
        self,
        sheet_name: str,
        orientation: str = "landscape",
        fit_to_width: int = 1,
        fit_to_height: int = 0,
    ):
        """Set page setup for printing (orientation, fit-to-page).

        Args:
            sheet_name: Target sheet name
            orientation: "landscape" or "portrait"
            fit_to_width: Number of pages wide (0 = no constraint)
            fit_to_height: Number of pages tall (0 = no constraint)
        """
        root = self._get_sheet_xml(sheet_name)
        zip_path = self._sheet_map[sheet_name]

        # Find or create <pageSetup>
        page_setup = root.find(f"{{{NS}}}pageSetup")
        if page_setup is None:
            page_setup = etree.SubElement(root, f"{{{NS}}}pageSetup")
        page_setup.set("orientation", orientation)
        if fit_to_width:
            page_setup.set("fitToWidth", str(fit_to_width))
        if fit_to_height:
            page_setup.set("fitToHeight", str(fit_to_height))
        else:
            page_setup.set("fitToHeight", "0")

        # Set <sheetFormatPr> to use fitToPage
        fmt = root.find(f"{{{NS}}}sheetFormatPr")
        # Don't create if not present — some sheets may not have it

        # Enable fitToPage on <pageSetup> parent <sheetPr>
        sheet_pr = root.find(f"{{{NS}}}sheetPr")
        if sheet_pr is None:
            # Insert before sheetData
            sheet_data = root.find(f"{{{NS}}}sheetData")
            idx = list(root).index(sheet_data) if sheet_data is not None else 0
            sheet_pr = etree.Element(f"{{{NS}}}sheetPr")
            root.insert(idx, sheet_pr)

        page_setup_pr = sheet_pr.find(f"{{{NS}}}pageSetUpPr")
        if page_setup_pr is None:
            page_setup_pr = etree.SubElement(sheet_pr, f"{{{NS}}}pageSetUpPr")
        page_setup_pr.set("fitToPage", "1")

        self._modified.add(zip_path)

    def set_header_footer(
        self,
        sheet_name: str,
        header: str = "",
        footer: str = "",
    ):
        """Set header and footer for printing.

        Uses Excel header/footer codes:
          &L = left, &C = center, &R = right
          &D = date, &P = page, &N = total pages
          &A = sheet name, &F = filename

        Example: "&LExampleSponsor Capital&R&D" → left: "ExampleSponsor Capital", right: date
        """
        root = self._get_sheet_xml(sheet_name)
        zip_path = self._sheet_map[sheet_name]

        hf = root.find(f"{{{NS}}}headerFooter")
        if hf is None:
            hf = etree.SubElement(root, f"{{{NS}}}headerFooter")

        if header:
            odd_header = hf.find(f"{{{NS}}}oddHeader")
            if odd_header is None:
                odd_header = etree.SubElement(hf, f"{{{NS}}}oddHeader")
            odd_header.text = header

        if footer:
            odd_footer = hf.find(f"{{{NS}}}oddFooter")
            if odd_footer is None:
                odd_footer = etree.SubElement(hf, f"{{{NS}}}oddFooter")
            odd_footer.text = footer

        self._modified.add(zip_path)

    def _get_workbook_xml(self) -> etree._Element:
        """Lazy-load and parse xl/workbook.xml."""
        if self._workbook_xml is None:
            with zipfile.ZipFile(self.template_path, "r") as zf:
                raw = zf.read("xl/workbook.xml")
            self._workbook_xml = etree.fromstring(raw)
        return self._workbook_xml

    def define_named_range(
        self,
        name: str,
        sheet_name: str,
        cell_ref_str: str,
    ) -> None:
        """Define a named range in xl/workbook.xml.

        Args:
            name: Range name (e.g., "LeveredIRR")
            sheet_name: Sheet containing the range
            cell_ref_str: Cell or range reference (e.g., "E155" or "E92:N92")
        """
        root = self._get_workbook_xml()

        # Find or create <definedNames>
        defined_names = root.find(f"{{{NS}}}definedNames")
        if defined_names is None:
            sheets_el = root.find(f"{{{NS}}}sheets")
            if sheets_el is not None:
                idx = list(root).index(sheets_el) + 1
            else:
                idx = len(root)
            defined_names = etree.Element(f"{{{NS}}}definedNames")
            root.insert(idx, defined_names)

        # Build absolute reference
        if ":" in cell_ref_str:
            start, end = cell_ref_str.split(":")
            start_letters = "".join(c for c in start if c.isalpha())
            start_digits = "".join(c for c in start if c.isdigit())
            end_letters = "".join(c for c in end if c.isalpha())
            end_digits = "".join(c for c in end if c.isdigit())
            ref_str = f"'{sheet_name}'!${start_letters}${start_digits}:${end_letters}${end_digits}"
        else:
            letters = "".join(c for c in cell_ref_str if c.isalpha())
            digits = "".join(c for c in cell_ref_str if c.isdigit())
            ref_str = f"'{sheet_name}'!${letters}${digits}"

        # Remove existing entry with same name (prevent duplicates)
        for existing in defined_names.findall(f"{{{NS}}}definedName"):
            if existing.get("name") == name:
                defined_names.remove(existing)
                break

        # Create <definedName> element
        dn = etree.SubElement(defined_names, f"{{{NS}}}definedName")
        dn.set("name", name)
        dn.text = ref_str

        self._workbook_modified = True

    def _get_styles_xml(self) -> etree._Element:
        """Lazy-load and parse xl/styles.xml."""
        if self._styles_xml is None:
            with zipfile.ZipFile(self.template_path, "r") as zf:
                raw = zf.read("xl/styles.xml")
            self._styles_xml = etree.fromstring(raw)
            # Set next dxf ID based on existing count
            dxfs = self._styles_xml.find(f"{{{NS}}}dxfs")
            if dxfs is not None:
                self._next_dxf_id = int(dxfs.get("count", "0"))
        return self._styles_xml

    def _append_dxf(self, font_color: str) -> int:
        """Append a dxf entry with font color to styles.xml. Returns dxfId."""
        root = self._get_styles_xml()
        dxfs = root.find(f"{{{NS}}}dxfs")
        if dxfs is None:
            dxfs = etree.SubElement(root, f"{{{NS}}}dxfs", count="0")

        dxf = etree.SubElement(dxfs, f"{{{NS}}}dxf")
        font = etree.SubElement(dxf, f"{{{NS}}}font")
        color = etree.SubElement(font, f"{{{NS}}}color")
        color.set("rgb", font_color)

        dxf_id = self._next_dxf_id
        self._next_dxf_id += 1
        dxfs.set("count", str(self._next_dxf_id))
        self._styles_modified = True
        return dxf_id

    def add_conditional_format(
        self,
        sheet_name: str,
        cell_range: str,
        rule_type: str,
        **kwargs,
    ) -> None:
        """Add conditional formatting rules to a worksheet.

        Args:
            sheet_name: Target sheet name
            cell_range: Range to apply formatting (e.g., "E5:N5")
            rule_type: One of "threshold", "gradient", "negative_red"
            **kwargs: Rule-specific parameters:
                threshold: thresholds=[{"operator", "value", "color"}]
                gradient: min_color, max_color, mid_color (optional)
                negative_red: (no extra params)
        """
        root = self._get_sheet_xml(sheet_name)
        zip_path = self._sheet_map[sheet_name]

        # Create <conditionalFormatting sqref="...">
        cf = etree.Element(f"{{{NS}}}conditionalFormatting")
        cf.set("sqref", cell_range)

        if rule_type == "threshold":
            thresholds = kwargs.get("thresholds", [])
            for t in thresholds:
                dxf_id = self._append_dxf(t["color"])
                rule = etree.SubElement(cf, f"{{{NS}}}cfRule")
                rule.set("type", "cellIs")
                rule.set("dxfId", str(dxf_id))
                rule.set("priority", str(self._cf_priority))
                rule.set("operator", t["operator"])
                rule.set("stopIfTrue", "1")
                self._cf_priority += 1

                value = t["value"]
                if t["operator"] == "between" and isinstance(value, list):
                    f1 = etree.SubElement(rule, f"{{{NS}}}formula")
                    f1.text = str(value[0])
                    f2 = etree.SubElement(rule, f"{{{NS}}}formula")
                    f2.text = str(value[1])
                else:
                    f_el = etree.SubElement(rule, f"{{{NS}}}formula")
                    f_el.text = str(value)

        elif rule_type == "gradient":
            rule = etree.SubElement(cf, f"{{{NS}}}cfRule")
            rule.set("type", "colorScale")
            rule.set("priority", str(self._cf_priority))
            self._cf_priority += 1

            cs = etree.SubElement(rule, f"{{{NS}}}colorScale")

            min_color = kwargs.get("min_color", "FFFF0000")
            mid_color = kwargs.get("mid_color")
            max_color = kwargs.get("max_color", "FF00FF00")

            if mid_color:
                for vtype in ("min", "percentile", "max"):
                    cfvo = etree.SubElement(cs, f"{{{NS}}}cfvo")
                    cfvo.set("type", vtype)
                    if vtype == "percentile":
                        cfvo.set("val", "50")
                for c in (min_color, mid_color, max_color):
                    color_el = etree.SubElement(cs, f"{{{NS}}}color")
                    color_el.set("rgb", c)
            else:
                for vtype in ("min", "max"):
                    cfvo = etree.SubElement(cs, f"{{{NS}}}cfvo")
                    cfvo.set("type", vtype)
                for c in (min_color, max_color):
                    color_el = etree.SubElement(cs, f"{{{NS}}}color")
                    color_el.set("rgb", c)

        elif rule_type == "negative_red":
            dxf_id = self._append_dxf("FFFF0000")
            rule = etree.SubElement(cf, f"{{{NS}}}cfRule")
            rule.set("type", "cellIs")
            rule.set("dxfId", str(dxf_id))
            rule.set("priority", str(self._cf_priority))
            rule.set("operator", "lessThan")
            self._cf_priority += 1

            f_el = etree.SubElement(rule, f"{{{NS}}}formula")
            f_el.text = "0"

        # Insert <conditionalFormatting> after <sheetData>
        sheet_data = root.find(f"{{{NS}}}sheetData")
        if sheet_data is not None:
            sheet_data.addnext(cf)
        else:
            root.append(cf)

        self._modified.add(zip_path)

    def save(self) -> Path:
        """Save the workbook by copying the template ZIP, replacing modified sheets.

        Uses atomic replace: writes to a temp file, then os.replace to output_path.
        """
        self.output_path.parent.mkdir(parents=True, exist_ok=True)

        # Write to a temp file in the same directory for atomic replace
        fd, tmp_path = tempfile.mkstemp(
            suffix=".xlsm",
            dir=self.output_path.parent,
        )
        os.close(fd)

        try:
            with zipfile.ZipFile(self.template_path, "r") as zf_in:
                with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf_out:
                    for entry in zf_in.infolist():
                        if entry.filename in self._modified:
                            # Serialize modified XML
                            root = self._parsed[entry.filename]
                            xml_bytes = etree.tostring(
                                root,
                                xml_declaration=False,
                                encoding="UTF-8",
                            )
                            # Excel requires explicit open/close tags, not
                            # self-closing — expand <v/> → <v></v> etc.
                            xml_bytes = self._expand_self_closing_tags(xml_bytes)
                            zf_out.writestr(entry, xml_bytes)
                        elif entry.filename == "xl/workbook.xml" and self._workbook_modified:
                            xml_bytes = etree.tostring(
                                self._workbook_xml,
                                xml_declaration=False,
                                encoding="UTF-8",
                            )
                            xml_bytes = self._expand_self_closing_tags(xml_bytes)
                            zf_out.writestr(entry, xml_bytes)
                        elif entry.filename == "xl/styles.xml" and self._styles_modified:
                            xml_bytes = etree.tostring(
                                self._styles_xml,
                                xml_declaration=False,
                                encoding="UTF-8",
                            )
                            xml_bytes = self._expand_self_closing_tags(xml_bytes)
                            zf_out.writestr(entry, xml_bytes)
                        else:
                            # Copy original bytes untouched
                            zf_out.writestr(entry, zf_in.read(entry.filename))

            # Atomic replace
            os.replace(tmp_path, self.output_path)
        except Exception:
            # Clean up temp file on failure
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

        return self.output_path

    # Context manager support
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self.save()
        return False
