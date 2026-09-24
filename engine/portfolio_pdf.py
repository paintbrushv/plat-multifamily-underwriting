"""
Portfolio PDF Report Generator
================================
Creates branded portfolio summary PDFs from portfolio analytics.

Includes:
- generate_portfolio_pdf(): Single-page portfolio snapshot
- generate_monthly_lp_report(): Multi-page monthly LP report with period-over-period deltas

Default brand: Dark (#0a0a0a) + Accent Gold (#c9a66c)

Usage:
    from engine.portfolio import Portfolio
    from engine.portfolio_pdf import generate_portfolio_pdf, generate_monthly_lp_report

    portfolio = Portfolio()
    portfolio.add_deals_from_directory("output/")
    generate_portfolio_pdf(portfolio, "output/portfolio_summary.pdf")
    generate_monthly_lp_report(portfolio, "output/monthly_lp_report.pdf", deltas=deltas)
"""
from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate,
    Table,
    TableStyle,
    Paragraph,
    Spacer,
    Image,
    HRFlowable,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT

from engine.portfolio import Portfolio


# ── Brand Colors ──────────────────────────────────────────────────────────
from engine.brand import MPL_DARK_900 as MPL_DARK, MPL_GOLD, MPL_DARK_800 as MPL_BG
from engine.formatters import fmt_pct, fmt_currency, fmt_multiple

GOLD = colors.HexColor("#c9a66c")
GOLD_LIGHT = colors.HexColor("#d4b88a")
DARK_900 = colors.HexColor("#0a0a0a")
DARK_800 = colors.HexColor("#111111")
DARK_700 = colors.HexColor("#1a1a1a")
WHITE = colors.white
LIGHT_GRAY = colors.HexColor("#aaaaaa")


def _build_styles():
    """Build branded paragraph styles."""
    ss = getSampleStyleSheet()
    styles = {}
    styles["Title"] = ParagraphStyle(
        "PortTitle", parent=ss["Title"],
        fontName="Times-Bold", fontSize=16, textColor=WHITE,
        alignment=TA_CENTER, spaceAfter=2,
    )
    styles["Subtitle"] = ParagraphStyle(
        "PortSubtitle", parent=ss["Normal"],
        fontName="Helvetica", fontSize=8, textColor=GOLD,
        alignment=TA_CENTER, spaceAfter=4,
    )
    styles["Section"] = ParagraphStyle(
        "PortSection", parent=ss["Heading2"],
        fontName="Times-Bold", fontSize=10, textColor=GOLD,
        spaceAfter=4, spaceBefore=6,
    )
    styles["Body"] = ParagraphStyle(
        "PortBody", parent=ss["Normal"],
        fontName="Helvetica", fontSize=8, textColor=WHITE,
    )
    styles["Footer"] = ParagraphStyle(
        "PortFooter", parent=ss["Normal"],
        fontName="Helvetica", fontSize=6, textColor=LIGHT_GRAY,
        alignment=TA_CENTER,
    )
    return styles


def _fmt_pct(val, places=2):
    return fmt_pct(val, decimals=places)

def _fmt_currency(val):
    return fmt_currency(val)

def _fmt_multiple(val):
    return fmt_multiple(val)


def _make_summary_table(summary):
    """Build portfolio summary metrics table."""
    data = [
        ["Metric", "Value"],
        ["Deals in Portfolio", str(summary.get("deal_count", 0))],
        ["Total Units", f"{summary.get('total_units', 0):,}"],
        ["Total Purchase Price", _fmt_currency(summary.get("total_purchase_price"))],
        ["Total Equity", _fmt_currency(summary.get("total_equity"))],
        ["Avg Price / Unit", _fmt_currency(summary.get("avg_price_per_unit"))],
        ["Year 1 NOI", _fmt_currency(summary.get("total_noi_year_1"))],
        ["Wtd Avg Levered IRR", _fmt_pct(summary.get("weighted_avg_levered_irr"))],
        ["Wtd Avg Levered EM", _fmt_multiple(summary.get("weighted_avg_levered_em"))],
        ["Metros", ", ".join(sorted(summary.get("metros", [])))],
    ]

    col_widths = [2.2 * inch, 2.2 * inch]
    table = Table(data, colWidths=col_widths)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), DARK_700),
        ("TEXTCOLOR", (0, 0), (-1, 0), GOLD),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("TEXTCOLOR", (0, 1), (0, -1), GOLD),
        ("TEXTCOLOR", (1, 1), (1, -1), WHITE),
        ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (1, 1), (1, -1), "Helvetica"),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("BACKGROUND", (0, 1), (-1, -1), DARK_900),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [DARK_900, DARK_800]),
        ("GRID", (0, 0), (-1, -1), 0.5, DARK_700),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return table


def _make_deal_table(matrix):
    """Build deal comparison table."""
    header = ["Deal", "Metro", "Units", "Purchase", "IRR", "EM"]
    data = [header]
    for row in matrix:
        data.append([
            row["deal_id"][:22],
            row["metro"],
            str(row["units"]),
            _fmt_currency(row["purchase_price"]),
            _fmt_pct(row["levered_irr"]),
            _fmt_multiple(row["levered_em"]),
        ])

    col_widths = [1.8 * inch, 0.8 * inch, 0.5 * inch, 1.2 * inch, 0.7 * inch, 0.7 * inch]
    table = Table(data, colWidths=col_widths)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), DARK_700),
        ("TEXTCOLOR", (0, 0), (-1, 0), GOLD),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("TEXTCOLOR", (0, 1), (-1, -1), WHITE),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("BACKGROUND", (0, 1), (-1, -1), DARK_900),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [DARK_900, DARK_800]),
        ("GRID", (0, 0), (-1, -1), 0.5, DARK_700),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    return table


def _make_metro_chart(by_metro):
    """Create a horizontal bar chart of total equity by metro."""
    if not by_metro:
        return None

    metros = list(by_metro.keys())
    equity = [by_metro[m].get("total_equity", 0) for m in metros]

    fig, ax = plt.subplots(figsize=(5.5, max(1.2, len(metros) * 0.4)))
    fig.patch.set_facecolor(MPL_BG)
    ax.set_facecolor(MPL_BG)

    bars = ax.barh(metros, equity, color=MPL_GOLD, height=0.5)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"${x / 1e6:.1f}M"))
    ax.tick_params(colors="white", labelsize=8)
    ax.spines["bottom"].set_color("#333333")
    ax.spines["left"].set_color("#333333")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.set_xlabel("Total Equity", color="white", fontsize=8)

    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=200, facecolor=MPL_BG,
                bbox_inches="tight", pad_inches=0.1)
    plt.close(fig)
    buf.seek(0)
    return buf


def generate_portfolio_pdf(
    portfolio: Portfolio,
    output_path: str | Path,
) -> Path:
    """Generate a branded portfolio summary PDF.

    Args:
        portfolio: Portfolio with deals loaded
        output_path: Where to save the PDF

    Returns:
        Path to the saved PDF
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    styles = _build_styles()
    summary = portfolio.summary()
    matrix = portfolio.deal_comparison_matrix()
    by_metro = portfolio.group_by_metro()

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=letter,
        topMargin=0.4 * inch,
        bottomMargin=0.4 * inch,
        leftMargin=0.5 * inch,
        rightMargin=0.5 * inch,
    )

    # Page background
    def draw_bg(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(DARK_900)
        canvas.rect(0, 0, letter[0], letter[1], fill=1, stroke=0)
        # Gold accent line at top
        canvas.setFillColor(GOLD)
        canvas.rect(0, letter[1] - 4, letter[0], 4, fill=1, stroke=0)
        canvas.restoreState()

    elements = []

    # Title
    elements.append(Paragraph("EXAMPLESPONSOR CAPITAL — PORTFOLIO SUMMARY", styles["Title"]))
    elements.append(Paragraph(
        f"{summary.get('deal_count', 0)} Deals | {datetime.now().strftime('%B %Y')}",
        styles["Subtitle"]
    ))
    elements.append(HRFlowable(width="100%", thickness=1, color=GOLD, spaceAfter=6))

    # Summary metrics
    elements.append(Paragraph("PORTFOLIO METRICS", styles["Section"]))
    elements.append(_make_summary_table(summary))
    elements.append(Spacer(1, 6))

    # Deal comparison
    if matrix:
        elements.append(Paragraph("DEAL COMPARISON", styles["Section"]))
        elements.append(_make_deal_table(matrix))
        elements.append(Spacer(1, 6))

    # Metro chart
    if by_metro:
        elements.append(Paragraph("EQUITY BY METRO", styles["Section"]))
        chart_buf = _make_metro_chart(by_metro)
        if chart_buf:
            chart_height = max(0.8, len(by_metro) * 0.35)
            elements.append(Image(chart_buf, width=5.5 * inch, height=chart_height * inch))

    # Footer
    elements.append(Spacer(1, 8))
    elements.append(HRFlowable(width="100%", thickness=0.5, color=DARK_700))
    elements.append(Paragraph(
        "CONFIDENTIAL — For authorized investors only. "
        "This document does not constitute an offer to sell securities.",
        styles["Footer"]
    ))

    doc.build(elements, onFirstPage=draw_bg, onLaterPages=draw_bg)
    return output_path


# ── Monthly LP Report ────────────────────────────────────────────────────

GREEN = colors.HexColor("#22c55e")
RED = colors.HexColor("#ef4444")


def _delta_str(val, fmt="currency"):
    """Format a delta value with +/- prefix."""
    if val is None or val == 0:
        return "—"
    sign = "+" if val > 0 else ""
    if fmt == "currency":
        return f"{sign}${val:,.0f}"
    elif fmt == "pct":
        return f"{sign}{val * 100:.2f}%"
    elif fmt == "multiple":
        return f"{sign}{val:.2f}x"
    elif fmt == "int":
        return f"{sign}{val:,}"
    return f"{sign}{val}"


def _delta_color(val):
    """Green for positive, red for negative, white for zero/none."""
    if val is None or val == 0:
        return WHITE
    return GREEN if val > 0 else RED


def _make_delta_table(deltas):
    """Build period-over-period comparison table."""
    sd = deltas.get("summary_deltas", {})

    rows_config = [
        ("Deals", "deal_count", "int"),
        ("Total Units", "total_units", "int"),
        ("Total Purchase Price", "total_purchase_price", "currency"),
        ("Total Equity", "total_equity", "currency"),
        ("Avg Price / Unit", "avg_price_per_unit", "currency"),
        ("Year 1 NOI", "total_noi_year_1", "currency"),
        ("Wtd Avg Levered IRR", "weighted_avg_levered_irr", "pct"),
        ("Wtd Avg Levered EM", "weighted_avg_levered_em", "multiple"),
    ]

    header = ["Metric", "Prior Period", "Current Period", "Change"]
    data = [header]

    for label, key, fmt in rows_config:
        d = sd.get(key, {})
        prev = d.get("previous")
        curr = d.get("current")
        change = d.get("change")

        if fmt == "currency":
            prev_s = _fmt_currency(prev)
            curr_s = _fmt_currency(curr)
            chg_s = _delta_str(change, "currency")
        elif fmt == "pct":
            prev_s = _fmt_pct(prev)
            curr_s = _fmt_pct(curr)
            chg_s = _delta_str(change, "pct")
        elif fmt == "multiple":
            prev_s = _fmt_multiple(prev)
            curr_s = _fmt_multiple(curr)
            chg_s = _delta_str(change, "multiple")
        else:
            prev_s = f"{prev:,}" if prev is not None else "—"
            curr_s = f"{curr:,}" if curr is not None else "—"
            chg_s = _delta_str(change, "int")

        data.append([label, prev_s, curr_s, chg_s])

    col_widths = [2.0 * inch, 1.3 * inch, 1.3 * inch, 1.1 * inch]
    table = Table(data, colWidths=col_widths)

    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), DARK_700),
        ("TEXTCOLOR", (0, 0), (-1, 0), GOLD),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("TEXTCOLOR", (0, 1), (0, -1), GOLD),
        ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold"),
        ("TEXTCOLOR", (1, 1), (2, -1), WHITE),
        ("FONTNAME", (1, 1), (2, -1), "Helvetica"),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("BACKGROUND", (0, 1), (-1, -1), DARK_900),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [DARK_900, DARK_800]),
        ("GRID", (0, 0), (-1, -1), 0.5, DARK_700),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]

    # Color the change column per-row
    for i, (_, key, _) in enumerate(rows_config):
        d = sd.get(key, {})
        change = d.get("change")
        color = _delta_color(change)
        style_cmds.append(("TEXTCOLOR", (3, i + 1), (3, i + 1), color))
        style_cmds.append(("FONTNAME", (3, i + 1), (3, i + 1), "Helvetica-Bold"))

    table.setStyle(TableStyle(style_cmds))
    return table


def _make_activity_section(deltas, styles):
    """Build new/removed deals section as paragraphs."""
    elements = []
    new_deals = deltas.get("new_deals", [])
    removed = deltas.get("removed_deals", [])

    if new_deals:
        elements.append(Paragraph("NEW ACQUISITIONS", styles["Section"]))
        for deal_id in new_deals:
            elements.append(Paragraph(
                f"<font color='#22c55e'>+</font> {deal_id}",
                styles["Body"]
            ))
        elements.append(Spacer(1, 4))

    if removed:
        elements.append(Paragraph("DISPOSITIONS", styles["Section"]))
        for deal_id in removed:
            elements.append(Paragraph(
                f"<font color='#ef4444'>-</font> {deal_id}",
                styles["Body"]
            ))
        elements.append(Spacer(1, 4))

    return elements


def _make_metro_delta_table(deltas):
    """Build metro-level change table."""
    metro_changes = deltas.get("metro_changes", {})
    if not metro_changes:
        return None

    # Filter to metros with actual changes
    active = {m: c for m, c in metro_changes.items()
              if any(v != 0 for v in c.values())}
    if not active:
        return None

    header = ["Metro", "Deals", "Units", "Equity"]
    data = [header]

    for metro in sorted(active.keys()):
        c = active[metro]
        data.append([
            metro,
            _delta_str(c["deals"], "int"),
            _delta_str(c["units"], "int"),
            _delta_str(c["equity"], "currency"),
        ])

    col_widths = [1.5 * inch, 0.8 * inch, 0.8 * inch, 1.5 * inch]
    table = Table(data, colWidths=col_widths)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), DARK_700),
        ("TEXTCOLOR", (0, 0), (-1, 0), GOLD),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("TEXTCOLOR", (0, 1), (-1, -1), WHITE),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("BACKGROUND", (0, 1), (-1, -1), DARK_900),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [DARK_900, DARK_800]),
        ("GRID", (0, 0), (-1, -1), 0.5, DARK_700),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return table


def _make_narrative_block(narrative_summary, styles):
    """Render executive summary as text block paragraphs."""
    elements = []
    elements.append(Paragraph("EXECUTIVE SUMMARY", styles["Section"]))
    for line in narrative_summary.get("summary_lines", []):
        elements.append(Paragraph(line, styles["Body"]))
    stale = narrative_summary.get("stale_deals", [])
    if stale:
        stale_str = ", ".join(stale)
        elements.append(Paragraph(
            f"<font color='#ef4444'>Warning: Stale data detected for: {stale_str}</font>",
            styles["Body"],
        ))
    return elements


def _make_exception_table(exceptions, styles):
    """Render exception callouts as a colored table."""
    if not exceptions:
        return []

    RED_EXCEPTION = colors.HexColor("#ff6b6b")
    GREEN_EXCEPTION = colors.HexColor("#51cf66")

    elements = []
    elements.append(Paragraph("EXCEPTION CALLOUTS", styles["Section"]))

    header = ["Deal", "NOI Variance", "Source", "Root Cause"]
    data = [header]
    for exc in exceptions:
        variance = exc.get("noi_variance_pct", 0)
        variance_str = f"{variance:+.1f}%"
        data.append([
            exc.get("deal_id", ""),
            variance_str,
            exc.get("source", ""),
            exc.get("root_cause", ""),
        ])

    col_widths = [140, 90, 100, 100]
    table = Table(data, colWidths=col_widths)

    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), DARK_700),
        ("TEXTCOLOR", (0, 0), (-1, 0), GOLD),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("TEXTCOLOR", (0, 1), (-1, -1), WHITE),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("BACKGROUND", (0, 1), (-1, -1), DARK_900),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [DARK_900, DARK_800]),
        ("GRID", (0, 0), (-1, -1), 0.5, DARK_700),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]

    # Color variance column per-row
    for i, exc in enumerate(exceptions):
        variance = exc.get("noi_variance_pct", 0)
        color = GREEN_EXCEPTION if variance >= 0 else RED_EXCEPTION
        style_cmds.append(("TEXTCOLOR", (1, i + 1), (1, i + 1), color))
        style_cmds.append(("FONTNAME", (1, i + 1), (1, i + 1), "Helvetica-Bold"))

    table.setStyle(TableStyle(style_cmds))
    elements.append(table)
    return elements


def _make_outlook_section(outlook, styles):
    """Render outlook as a bullet list of sub-sections."""
    elements = []
    elements.append(Paragraph("UPCOMING OUTLOOK", styles["Section"]))

    # Lease Turnover
    am_exp = outlook.get("am_lease_expirations")
    turnover = outlook.get("turnover_estimate", {})
    if am_exp is not None:
        elements.append(Paragraph(
            f"<b>Lease Turnover:</b> {am_exp} upcoming expirations (from AM data)",
            styles["Body"],
        ))
    elif turnover:
        units = turnover.get("units", 0)
        pct = turnover.get("pct", 0)
        elements.append(Paragraph(
            f"<b>Lease Turnover:</b> ~{units} units estimated ({pct:.0f}% annual turnover rate)",
            styles["Body"],
        ))

    # Rate Cap Expirations
    rate_caps = outlook.get("rate_cap_expirations", [])
    if rate_caps:
        cap_strs = [f"{c['deal_id']} (month {c['expiry_month']})" for c in rate_caps]
        elements.append(Paragraph(
            f"<b>Rate Cap Expirations:</b> {', '.join(cap_strs)}",
            styles["Body"],
        ))
    else:
        elements.append(Paragraph(
            "<b>Rate Cap Expirations:</b> None in lookahead window",
            styles["Body"],
        ))

    # Upcoming Refi
    refi_events = outlook.get("upcoming_refi_events", [])
    if refi_events:
        refi_strs = [f"{r['deal_id']} (month {r['refi_month']})" for r in refi_events]
        elements.append(Paragraph(
            f"<b>Upcoming Refi:</b> {', '.join(refi_strs)}",
            styles["Body"],
        ))
    else:
        elements.append(Paragraph(
            "<b>Upcoming Refi:</b> None in lookahead window",
            styles["Body"],
        ))

    # Renovation Pipeline
    reno = outlook.get("renovation_pipeline", {})
    if reno:
        reno_units = reno.get("units_scheduled", 0)
        reno_cost = reno.get("cost_estimate", 0)
        elements.append(Paragraph(
            f"<b>Renovation Pipeline:</b> {reno_units} units scheduled, "
            f"estimated cost {fmt_currency(reno_cost)}",
            styles["Body"],
        ))
    else:
        elements.append(Paragraph(
            "<b>Renovation Pipeline:</b> No units scheduled in lookahead window",
            styles["Body"],
        ))

    return elements


def generate_monthly_lp_report(
    portfolio: Portfolio,
    output_path: str | Path,
    deltas: Optional[Dict[str, Any]] = None,
    report_month: Optional[str] = None,
    narrative: Optional[Dict[str, Any]] = None,
) -> Path:
    """Generate a branded monthly LP report PDF.

    Includes portfolio summary, period-over-period changes (if deltas provided),
    deal comparison, metro breakdown, and equity chart.

    Args:
        portfolio: Portfolio with deals loaded
        output_path: Where to save the PDF
        deltas: Period-over-period delta dict from compute_period_deltas()
        report_month: Display string like "March 2026" (default: current month)

    Returns:
        Path to the saved PDF
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    styles = _build_styles()
    summary = portfolio.summary()
    matrix = portfolio.deal_comparison_matrix()
    by_metro = portfolio.group_by_metro()

    if not report_month:
        report_month = datetime.now().strftime("%B %Y")

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=letter,
        topMargin=0.4 * inch,
        bottomMargin=0.4 * inch,
        leftMargin=0.5 * inch,
        rightMargin=0.5 * inch,
    )

    def draw_bg(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(DARK_900)
        canvas.rect(0, 0, letter[0], letter[1], fill=1, stroke=0)
        canvas.setFillColor(GOLD)
        canvas.rect(0, letter[1] - 4, letter[0], 4, fill=1, stroke=0)
        canvas.restoreState()

    elements = []

    # Title
    elements.append(Paragraph(
        f"EXAMPLESPONSOR CAPITAL — MONTHLY PORTFOLIO REPORT",
        styles["Title"]
    ))
    elements.append(Paragraph(
        f"{report_month} | {summary.get('deal_count', 0)} Deals | "
        f"{summary.get('total_units', 0):,} Units",
        styles["Subtitle"]
    ))
    elements.append(HRFlowable(width="100%", thickness=1, color=GOLD, spaceAfter=6))

    # Period-over-period changes (if available)
    if deltas:
        prev_period = deltas.get("previous_period", "")
        curr_period = deltas.get("current_period", "")
        elements.append(Paragraph(
            f"PERIOD-OVER-PERIOD CHANGES ({prev_period} vs {curr_period})",
            styles["Section"]
        ))
        elements.append(_make_delta_table(deltas))
        elements.append(Spacer(1, 4))

        # New/removed deals
        activity = _make_activity_section(deltas, styles)
        elements.extend(activity)

        # Metro-level changes
        metro_table = _make_metro_delta_table(deltas)
        if metro_table:
            elements.append(Paragraph("CHANGES BY METRO", styles["Section"]))
            elements.append(metro_table)
            elements.append(Spacer(1, 4))
    else:
        # No prior period — show summary table
        elements.append(Paragraph("PORTFOLIO METRICS", styles["Section"]))
        elements.append(_make_summary_table(summary))
        elements.append(Spacer(1, 6))

    # Deal comparison table
    if matrix:
        elements.append(Paragraph("DEAL COMPARISON", styles["Section"]))
        elements.append(_make_deal_table(matrix))
        elements.append(Spacer(1, 6))

    # Narrative sections (only when narrative dict provided)
    if narrative:
        elements.append(Spacer(1, 12))
        elements.extend(_make_narrative_block(narrative.get("summary", {}), styles))
        elements.append(Spacer(1, 8))
        elements.extend(_make_exception_table(narrative.get("exceptions", []), styles))
        elements.append(Spacer(1, 8))
        elements.extend(_make_outlook_section(narrative.get("outlook", {}), styles))

    # Metro equity chart
    if by_metro:
        elements.append(Paragraph("EQUITY BY METRO", styles["Section"]))
        chart_buf = _make_metro_chart(by_metro)
        if chart_buf:
            chart_height = max(0.8, len(by_metro) * 0.35)
            elements.append(Image(chart_buf, width=5.5 * inch, height=chart_height * inch))

    # Top 5 deals callout
    top = portfolio.top_deals("levered_irr", 5)
    if top:
        elements.append(Spacer(1, 4))
        elements.append(Paragraph("TOP 5 DEALS BY LEVERED IRR", styles["Section"]))
        top_data = [["Rank", "Deal", "Metro", "IRR", "EM"]]
        for i, d in enumerate(top, 1):
            top_data.append([
                str(i),
                d.deal_id[:25],
                d.metro,
                _fmt_pct(d.levered_irr),
                _fmt_multiple(d.levered_em),
            ])
        top_widths = [0.4 * inch, 2.2 * inch, 0.9 * inch, 0.8 * inch, 0.8 * inch]
        top_table = Table(top_data, colWidths=top_widths)
        top_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), DARK_700),
            ("TEXTCOLOR", (0, 0), (-1, 0), GOLD),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 7.5),
            ("TEXTCOLOR", (0, 1), (-1, -1), WHITE),
            ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
            ("ALIGN", (0, 0), (0, -1), "CENTER"),
            ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
            ("BACKGROUND", (0, 1), (-1, -1), DARK_900),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [DARK_900, DARK_800]),
            ("GRID", (0, 0), (-1, -1), 0.5, DARK_700),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        elements.append(top_table)

    # Footer
    elements.append(Spacer(1, 8))
    elements.append(HRFlowable(width="100%", thickness=0.5, color=DARK_700))
    elements.append(Paragraph(
        f"CONFIDENTIAL — For authorized investors only. "
        f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}. "
        f"This document does not constitute an offer to sell securities.",
        styles["Footer"]
    ))

    doc.build(elements, onFirstPage=draw_bg, onLaterPages=draw_bg)
    return output_path
