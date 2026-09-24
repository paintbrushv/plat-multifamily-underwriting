"""
Portfolio Excel Workbook Generator
===================================
Creates a standalone Excel workbook with portfolio-level analytics:
- Summary tab with aggregate metrics
- Deal Comparison matrix
- Metro breakdown
- Charts (scatter plot: IRR vs EM, sized by equity, colored by metro)

Uses openpyxl (safe here — we're creating fresh workbooks, not modifying templates).
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Dict, List

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XlImage
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side, numbers
from openpyxl.utils import get_column_letter

from engine.portfolio import Portfolio

# ── Brand Colors ──────────────────────────────────────────────────────────
from engine.brand import (
    HEX_GOLD as GOLD, HEX_GOLD_LIGHT as GOLD_LIGHT,
    HEX_DARK_900 as DARK_900, HEX_DARK_700 as DARK_700,
    HEX_DARK_500 as DARK_500, HEX_WHITE as WHITE, HEX_LIGHT_GRAY as LIGHT_GRAY,
)
from engine.xlsx_helpers import (
    set_col_widths, write_header_row, write_data_row,
)

# ── Style Presets ─────────────────────────────────────────────────────────

HEADER_FONT = Font(name="Georgia", size=14, bold=True, color=WHITE)
SUBHEADER_FONT = Font(name="Georgia", size=11, bold=True, color=DARK_900)
LABEL_FONT = Font(name="Calibri", size=10, color=DARK_700)
VALUE_FONT = Font(name="Calibri", size=10, color=DARK_900)
VALUE_BOLD_FONT = Font(name="Calibri", size=10, bold=True, color=DARK_900)
METRIC_FONT = Font(name="Calibri", size=12, bold=True, color=DARK_900)
SMALL_FONT = Font(name="Calibri", size=9, color=DARK_500)

HEADER_FILL = PatternFill(start_color=DARK_900, end_color=DARK_900, fill_type="solid")
GOLD_FILL = PatternFill(start_color=GOLD, end_color=GOLD, fill_type="solid")
GOLD_LIGHT_FILL = PatternFill(start_color=GOLD_LIGHT, end_color=GOLD_LIGHT, fill_type="solid")
LIGHT_FILL = PatternFill(start_color=LIGHT_GRAY, end_color=LIGHT_GRAY, fill_type="solid")
WHITE_FILL = PatternFill(start_color=WHITE, end_color=WHITE, fill_type="solid")

THIN_BORDER = Border(
    bottom=Side(style="thin", color=DARK_500),
)
BOTTOM_BORDER = Border(
    bottom=Side(style="medium", color=DARK_900),
)

CENTER = Alignment(horizontal="center", vertical="center")
RIGHT = Alignment(horizontal="right", vertical="center")
LEFT = Alignment(horizontal="left", vertical="center")


def _set_col_widths(ws, widths):
    set_col_widths(ws, widths)


def _write_header_row(ws, row, values, start_col=1):
    write_header_row(ws, row, values, start_col=start_col)


def _write_data_row(ws, row, values, start_col=1, zebra=False):
    write_data_row(ws, row, values, start_col=start_col, zebra=zebra)


# ── Summary Sheet ─────────────────────────────────────────────────────────

def _write_summary_sheet(ws, portfolio: Portfolio):
    """Write portfolio summary metrics."""
    ws.title = "Portfolio Summary"
    s = portfolio.summary()

    if s["deal_count"] == 0:
        ws.cell(row=2, column=2, value="No deals loaded.").font = SUBHEADER_FONT
        return

    # Title
    ws.merge_cells("B2:F2")
    title_cell = ws.cell(row=2, column=2, value="EXAMPLESPONSOR CAPITAL — PORTFOLIO SUMMARY")
    title_cell.font = HEADER_FONT
    title_cell.fill = HEADER_FILL
    title_cell.alignment = CENTER

    # Key metrics cards (row 4-9)
    metrics = [
        ("Deals in Portfolio", s["deal_count"], None),
        ("Total Units", f"{s['total_units']:,}", None),
        ("Total Purchase Price", s["total_purchase_price"], "$#,##0"),
        ("Total Equity", s["total_equity"], "$#,##0"),
        ("Avg Price per Unit", s["avg_price_per_unit"], "$#,##0"),
        ("Year 1 NOI", s["total_noi_year_1"], "$#,##0"),
        ("Wtd Avg Levered IRR", s.get("weighted_avg_levered_irr"), "0.00%"),
        ("Wtd Avg Levered EM", s.get("weighted_avg_levered_em"), "0.00x"),
        ("Metros", ", ".join(sorted(s.get("metros", []))), None),
        ("Vintage Years", ", ".join(str(y) for y in s.get("vintage_years", [])), None),
    ]

    row = 4
    for label, value, fmt in metrics:
        # Label
        label_cell = ws.cell(row=row, column=2, value=label)
        label_cell.font = LABEL_FONT
        label_cell.fill = GOLD_LIGHT_FILL
        label_cell.alignment = LEFT

        # Value
        val_cell = ws.cell(row=row, column=4, value=value)
        val_cell.font = METRIC_FONT
        val_cell.alignment = RIGHT
        if fmt and value is not None:
            val_cell.number_format = fmt

        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=3)
        ws.merge_cells(start_row=row, start_column=4, end_row=row, end_column=6)
        row += 1

    _set_col_widths(ws, {1: 3, 2: 22, 3: 10, 4: 18, 5: 10, 6: 10})


# ── Deal Comparison Sheet ─────────────────────────────────────────────────

def _write_comparison_sheet(ws, portfolio: Portfolio):
    """Write deal comparison matrix."""
    ws.title = "Deal Comparison"
    matrix = portfolio.deal_comparison_matrix()
    if not matrix:
        ws.cell(row=2, column=2, value="No deals loaded.").font = SUBHEADER_FONT
        return

    # Title
    ws.merge_cells("B2:L2")
    title_cell = ws.cell(row=2, column=2, value="DEAL COMPARISON MATRIX")
    title_cell.font = HEADER_FONT
    title_cell.fill = HEADER_FILL
    title_cell.alignment = CENTER

    # Column headers
    headers = [
        "Deal ID", "Metro", "Units", "Purchase Price", "Price/Unit",
        "Going-In Cap", "Exit Cap", "NOI Yr 1", "Levered IRR",
        "Levered EM", "Avg DSCR", "Total Equity",
    ]
    _write_header_row(ws, 4, headers, start_col=2)

    # Data rows
    for i, row_data in enumerate(matrix):
        r = 5 + i
        values = [
            row_data["deal_id"],
            row_data["metro"],
            row_data["units"],
            row_data["purchase_price"],
            row_data["price_per_unit"],
            row_data["going_in_cap"],
            row_data["exit_cap"],
            row_data["noi_year_1"],
            row_data["levered_irr"],
            row_data["levered_em"],
            row_data["average_dscr"],
            row_data["total_equity"],
        ]
        _write_data_row(ws, r, values, start_col=2, zebra=(i % 2 == 1))

        # Apply number formats
        ws.cell(row=r, column=5).number_format = "$#,##0"   # purchase
        ws.cell(row=r, column=6).number_format = "$#,##0"   # ppu
        ws.cell(row=r, column=7).number_format = "0.00%"    # going in cap
        ws.cell(row=r, column=8).number_format = "0.00%"    # exit cap
        ws.cell(row=r, column=9).number_format = "$#,##0"   # noi
        ws.cell(row=r, column=10).number_format = "0.00%"   # irr
        ws.cell(row=r, column=11).number_format = "0.00x"   # em
        ws.cell(row=r, column=12).number_format = "0.00"    # dscr
        ws.cell(row=r, column=13).number_format = "$#,##0"  # equity

    # Column widths
    _set_col_widths(ws, {
        1: 3, 2: 28, 3: 12, 4: 8, 5: 16, 6: 12,
        7: 12, 8: 10, 9: 14, 10: 12, 11: 12, 12: 10, 13: 14,
    })


# ── Metro Sheet ───────────────────────────────────────────────────────────

def _write_metro_sheet(ws, portfolio: Portfolio):
    """Write metro breakdown."""
    ws.title = "By Metro"
    by_metro = portfolio.group_by_metro()
    if not by_metro:
        ws.cell(row=2, column=2, value="No deals loaded.").font = SUBHEADER_FONT
        return

    # Title
    ws.merge_cells("B2:G2")
    title_cell = ws.cell(row=2, column=2, value="PORTFOLIO BY METRO")
    title_cell.font = HEADER_FONT
    title_cell.fill = HEADER_FILL
    title_cell.alignment = CENTER

    headers = ["Metro", "Deals", "Units", "Purchase Price", "Total Equity", "Wtd Avg IRR", "Deal IDs"]
    _write_header_row(ws, 4, headers, start_col=2)

    for i, (metro, data) in enumerate(sorted(by_metro.items())):
        r = 5 + i
        values = [
            metro,
            data["deal_count"],
            data["total_units"],
            data["total_purchase_price"],
            data["total_equity"],
            data.get("weighted_avg_levered_irr"),
            ", ".join(data.get("deal_ids", [])),
        ]
        _write_data_row(ws, r, values, start_col=2, zebra=(i % 2 == 1))
        ws.cell(row=r, column=5).number_format = "$#,##0"
        ws.cell(row=r, column=6).number_format = "$#,##0"
        ws.cell(row=r, column=7).number_format = "0.00%"

    _set_col_widths(ws, {1: 3, 2: 16, 3: 8, 4: 8, 5: 16, 6: 14, 7: 12, 8: 40})


# ── Charts Sheet ─────────────────────────────────────────────────────────

METRO_COLORS = {
    "DFW": "#0a0a0a",
    "Austin": "#c9a66c",
    "Birmingham": "#64748b",
    "San Antonio": "#334155",
    "Houston": "#a68b52",
    "Other": "#999999",
}


def _generate_scatter_chart(portfolio: Portfolio) -> bytes:
    """Generate IRR vs EM scatter plot as PNG bytes.

    Each deal is a bubble sized by equity and colored by metro.
    """
    deals = [d for d in portfolio.deals
             if d.levered_irr is not None and d.levered_em is not None]
    if len(deals) < 2:
        return b""

    fig, ax = plt.subplots(figsize=(8, 5))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    # Normalize bubble sizes (equity → 50-500 point range)
    equities = [d.total_equity for d in deals]
    min_eq, max_eq = min(equities), max(equities)
    eq_range = max_eq - min_eq if max_eq != min_eq else 1

    metros_seen = set()
    for d in deals:
        irr = d.levered_irr * 100  # percent
        em = d.levered_em
        size = 50 + 450 * (d.total_equity - min_eq) / eq_range
        color = METRO_COLORS.get(d.metro, "#999999")
        label = d.metro if d.metro not in metros_seen else None
        metros_seen.add(d.metro)

        ax.scatter(irr, em, s=size, c=color, alpha=0.7, edgecolors="white",
                   linewidths=0.5, label=label, zorder=3)
        ax.annotate(d.deal_id, (irr, em), fontsize=5, ha="center", va="bottom",
                    xytext=(0, 6), textcoords="offset points", color="#404040")

    ax.set_xlabel("Levered IRR (%)", fontsize=9, color="#333333")
    ax.set_ylabel("Levered Equity Multiple (x)", fontsize=9, color="#333333")
    ax.set_title("Deal Returns: IRR vs Equity Multiple", fontsize=11,
                 fontweight="bold", color="#0a0a0a", pad=10)
    ax.legend(fontsize=8, loc="upper left", frameon=True, framealpha=0.8,
              edgecolor="#e2e8f0")
    ax.grid(True, alpha=0.3, color="#e2e8f0")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#e2e8f0")
    ax.spines["bottom"].set_color("#e2e8f0")
    ax.tick_params(labelsize=8, colors="#666666")

    plt.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def _write_charts_sheet(ws, portfolio: Portfolio):
    """Write a Charts sheet with embedded scatter plot."""
    ws.title = "Charts"

    # Title
    ws.merge_cells("B2:L2")
    title_cell = ws.cell(row=2, column=2, value="PORTFOLIO CHARTS")
    title_cell.font = HEADER_FONT
    title_cell.fill = HEADER_FILL
    title_cell.alignment = CENTER

    chart_png = _generate_scatter_chart(portfolio)
    if not chart_png:
        ws.cell(row=4, column=2,
                value="Need at least 2 deals with IRR/EM data for scatter chart."
                ).font = LABEL_FONT
        return

    img = XlImage(io.BytesIO(chart_png))
    img.width = 720
    img.height = 450
    ws.add_image(img, "B4")


# ── Public API ────────────────────────────────────────────────────────────

def generate_portfolio_workbook(portfolio: Portfolio, output_path: str | Path) -> Path:
    """Generate a portfolio Excel workbook.

    Args:
        portfolio: Portfolio with deals loaded
        output_path: Where to save the .xlsx file

    Returns:
        Path to the saved workbook
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()

    # Summary sheet (uses the default sheet)
    _write_summary_sheet(wb.active, portfolio)

    # Deal comparison
    ws_comp = wb.create_sheet()
    _write_comparison_sheet(ws_comp, portfolio)

    # Metro breakdown
    ws_metro = wb.create_sheet()
    _write_metro_sheet(ws_metro, portfolio)

    # Charts (scatter plot)
    ws_charts = wb.create_sheet()
    _write_charts_sheet(ws_charts, portfolio)

    # Freeze panes on comparison sheet
    wb["Deal Comparison"].freeze_panes = "C5"

    # Print setup for all sheets
    from openpyxl.worksheet.page import PageMargins
    for ws in wb.worksheets:
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.page_setup.orientation = "landscape"
        ws.oddHeader.left.text = "ExampleSponsor Capital"
        ws.oddHeader.right.text = "Portfolio Report"
        ws.oddFooter.left.text = "Confidential"
        ws.oddFooter.center.text = "Page &P of &N"

    wb.save(str(output_path))
    return output_path
