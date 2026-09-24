"""
PDF One-Pager Generator
========================
Auto-generates a branded single-page deal summary PDF from engine results.

Default brand: Dark (#0a0a0a) + Accent Gold (#c9a66c)
Font: DM Serif Display for headings (Times-Bold fallback), DM Sans for body (Helvetica fallback)

Usage:
    from engine.pdf_onepager import generate_onepager

    generate_onepager(results, inputs, "output/deal_onepager.pdf")
    generate_onepager(results, inputs, "output/deal.pdf", scenario_results=scenarios)
"""
from __future__ import annotations

import io
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import matplotlib
matplotlib.use("Agg")  # Non-interactive backend
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


# ── Brand Colors (default palette — override via engine.brand) ────────────
from engine.brand import (
    MPL_DARK_900, MPL_GOLD, MPL_GOLD_DARK, MPL_SLATE_500, MPL_SLATE_200,
    BrandConfig,
)
from engine.formatters import fmt_pct, fmt_currency, fmt_multiple, fmt_number

# Dark neutrals (backgrounds, text)
DARK_900 = colors.HexColor("#0a0a0a")    # Primary BG
DARK_800 = colors.HexColor("#111111")    # Secondary BG
DARK_700 = colors.HexColor("#1a1a1a")    # Tertiary BG
DARK_600 = colors.HexColor("#262626")    # Subtle border
DARK_500 = colors.HexColor("#404040")    # Emphasis border

# Light neutrals (text on dark, light-mode backgrounds)
NEUTRAL_50 = colors.HexColor("#fafafa")  # Primary text (light)
NEUTRAL_300 = colors.HexColor("#a3a3a3") # Secondary text
NEUTRAL_500 = colors.HexColor("#737373") # Muted text

# Accent gold
GOLD = colors.HexColor("#c9a66c")        # Primary accent
GOLD_LIGHT = colors.HexColor("#d4b88a")  # Lighter variant
GOLD_DARK = colors.HexColor("#a68b52")   # Darker variant

# For print: light background with dark text + gold accents
# (Dark backgrounds waste ink and look poor in B&W printing)
SLATE_900 = colors.HexColor("#0f172a")   # Dark text (print-friendly)
SLATE_700 = colors.HexColor("#334155")
SLATE_500 = colors.HexColor("#64748b")
SLATE_200 = colors.HexColor("#e2e8f0")
SLATE_100 = colors.HexColor("#f1f5f9")
SLATE_50 = colors.HexColor("#f8fafc")

WHITE = colors.white


def _location_text(meta: Dict[str, Any]) -> str:
    address = meta.get("address")
    if isinstance(address, dict):
        city = str(address.get("city") or "").strip()
        state = str(address.get("state") or "").strip()
        if city and state:
            return f"{city}, {state}"
        street = str(address.get("street") or "").strip()
        if street:
            return street
    elif isinstance(address, str) and address.strip():
        parts = [part.strip() for part in address.split(",") if part.strip()]
        if len(parts) >= 2:
            return ", ".join(parts[-2:])
        return address.strip()

    market = meta.get("market")
    if market not in (None, ""):
        return str(market)
    city = meta.get("city")
    state = meta.get("state")
    if city and state:
        return f"{city}, {state}"
    return "—"


# ── Styles ───────────────────────────────────────────────────────────────

def _build_styles():
    """Build custom paragraph styles for the one-pager."""
    styles = getSampleStyleSheet()

    styles.add(ParagraphStyle(
        "BrandTitle",
        parent=styles["Title"],
        fontName="Times-Bold",
        fontSize=20,
        textColor=SLATE_900,
        spaceAfter=1,
        spaceBefore=0,
        alignment=TA_LEFT,
    ))
    styles.add(ParagraphStyle(
        "BrandSubtitle",
        parent=styles["Normal"],
        fontName="Times-Roman",
        fontSize=10,
        textColor=SLATE_500,
        spaceAfter=3,
    ))
    styles.add(ParagraphStyle(
        "SectionHeader",
        parent=styles["Heading2"],
        fontName="Times-Bold",
        fontSize=11,
        textColor=GOLD_DARK,
        spaceBefore=6,
        spaceAfter=2,
        borderWidth=0,
    ))
    styles.add(ParagraphStyle(
        "BrandBody",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        textColor=SLATE_700,
        leading=12,
    ))
    styles.add(ParagraphStyle(
        "TableHeader",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8,
        textColor=WHITE,
    ))
    styles.add(ParagraphStyle(
        "TableCell",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8,
        textColor=SLATE_700,
    ))
    styles.add(ParagraphStyle(
        "FooterText",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=7,
        textColor=SLATE_500,
        alignment=TA_CENTER,
    ))
    return styles


# ── Formatting Helpers (thin wrappers for pdf_onepager defaults) ─────────

def _fmt_pct(val, decimals=1):
    return fmt_pct(val, decimals=decimals)

def _fmt_mult(val):
    return fmt_multiple(val)

def _fmt_currency(val, abbrev=True):
    return fmt_currency(val, abbrev=abbrev)

def _fmt_number(val):
    return fmt_number(val)


# ── Chart Generation ─────────────────────────────────────────────────────

def _generate_cf_chart(
    results: Dict[str, Any],
    width_inches: float = 6.5,
    height_inches: float = 2.0,
) -> bytes:
    """Generate a monthly cash flow bar chart as PNG bytes."""
    cashflow = results.get("cashflow", {})
    by_year = cashflow.get("by_year", [])

    if not by_year:
        return b""

    years = [yr.get("year", "") for yr in by_year]
    noi = [float(yr.get("net_operating_income", 0)) for yr in by_year]
    lcf = [float(yr.get("leveraged_cash_flow", 0)) for yr in by_year]

    fig, ax = plt.subplots(figsize=(width_inches, height_inches))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    x = range(len(years))
    bar_width = 0.35

    bars1 = ax.bar(
        [i - bar_width / 2 for i in x], noi, bar_width,
        label="NOI", color=MPL_DARK_900, alpha=0.85,
    )
    bars2 = ax.bar(
        [i + bar_width / 2 for i in x], lcf, bar_width,
        label="Levered CF", color=MPL_GOLD, alpha=0.85,
    )

    ax.set_xticks(list(x))
    ax.set_xticklabels(years, fontsize=7, color=MPL_SLATE_500)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(
        lambda val, pos: f"${val/1e6:.1f}M" if abs(val) >= 1e6 else f"${val/1e3:.0f}K"
    ))
    ax.tick_params(axis="y", labelsize=7, colors=MPL_SLATE_500)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color(MPL_SLATE_200)
    ax.spines["bottom"].set_color(MPL_SLATE_200)
    ax.legend(fontsize=7, loc="upper left", frameon=False)

    plt.tight_layout(pad=0.3)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


# ── Waterfall Chart ──────────────────────────────────────────────────────

def _compute_waterfall_segments(results: Dict[str, Any]) -> Dict[str, List[float]]:
    """Decompose annual waterfall distributions into stacked chart segments.

    Returns dict with keys: years, capital_return, lp_pref, lp_excess, gp_promote.
    """
    fund_wf = results.get("fund_waterfall", {})
    promote = fund_wf.get("promote", {})
    by_year = promote.get("by_year", [])
    by_tier = promote.get("by_tier", [])

    if not by_year:
        return {"years": [], "capital_return": [], "lp_pref": [], "lp_excess": [], "gp_promote": []}

    # Classify tiers
    total_pref_distributed = 0.0
    total_lp_from_promote = 0.0
    for tier in by_tier:
        gp_pct = tier.get("gp_share_pct", 0)
        lp_amount = tier.get("total_to_lp", 0)
        if gp_pct == 0:
            total_pref_distributed += lp_amount
        else:
            total_lp_from_promote += lp_amount

    # Total LP across all years
    total_lp_all_years = sum(yr.get("lp_share", 0) for yr in by_year)
    capital_return_total = max(0.0, total_lp_all_years - total_pref_distributed - total_lp_from_promote)

    years = []
    capital_return = []
    lp_pref = []
    lp_excess = []
    gp_promote = []

    for yr in by_year:
        years.append(yr.get("year", 0))
        gp_promo = yr.get("promote_payment", 0)
        gp_promote.append(gp_promo)

        lp_share = yr.get("lp_share", 0)
        if total_lp_all_years > 0:
            year_frac = lp_share / total_lp_all_years
        else:
            year_frac = 0.0

        capital_return.append(capital_return_total * year_frac)
        lp_pref.append(total_pref_distributed * year_frac)
        lp_excess.append(total_lp_from_promote * year_frac)

    return {
        "years": years,
        "capital_return": capital_return,
        "lp_pref": lp_pref,
        "lp_excess": lp_excess,
        "gp_promote": gp_promote,
    }


def _generate_waterfall_chart(
    results: Dict[str, Any],
    width_inches: float = 6.5,
    height_inches: float = 2.0,
) -> bytes:
    """Generate a stacked bar waterfall distribution chart as PNG bytes.

    Returns b"" if single-tier or no data.
    """
    fund_wf = results.get("fund_waterfall", {})
    promote = fund_wf.get("promote", {})
    by_tier = promote.get("by_tier", [])

    if len(by_tier) <= 1:
        return b""

    segments = _compute_waterfall_segments(results)
    years = segments["years"]
    if not years:
        return b""

    fig, ax = plt.subplots(figsize=(width_inches, height_inches))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    x = range(len(years))
    bar_width = 0.6
    labels = [f"Yr {i+1}" for i in range(len(years))]

    # Stacked bars bottom-to-top
    bottom = [0.0] * len(years)

    ax.bar(x, segments["capital_return"], bar_width, bottom=bottom,
           label="Capital Return", color=MPL_DARK_900, alpha=0.85)
    bottom = [b + v for b, v in zip(bottom, segments["capital_return"])]

    ax.bar(x, segments["lp_pref"], bar_width, bottom=bottom,
           label="LP Pref", color=MPL_SLATE_500, alpha=0.85)
    bottom = [b + v for b, v in zip(bottom, segments["lp_pref"])]

    ax.bar(x, segments["lp_excess"], bar_width, bottom=bottom,
           label="LP Excess", color=MPL_GOLD, alpha=0.85)
    bottom = [b + v for b, v in zip(bottom, segments["lp_excess"])]

    ax.bar(x, segments["gp_promote"], bar_width, bottom=bottom,
           label="GP Promote", color=MPL_GOLD_DARK, alpha=0.85)

    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, fontsize=7, color=MPL_SLATE_500)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(
        lambda val, pos: f"${val/1e6:.1f}M" if abs(val) >= 1e6 else f"${val/1e3:.1f}K"
    ))
    ax.tick_params(axis="y", labelsize=7, colors=MPL_SLATE_500)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color(MPL_SLATE_200)
    ax.spines["bottom"].set_color(MPL_SLATE_200)
    ax.legend(fontsize=6, loc="upper left", frameon=False, ncol=4)

    plt.tight_layout(pad=0.3)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


# ── Table Builders ───────────────────────────────────────────────────────

def _returns_table(results: Dict[str, Any], styles) -> Table:
    """Build the returns summary table."""
    metrics = results.get("metrics", {})
    irr = metrics.get("irr", {})
    em = metrics.get("equity_multiple", {})
    dscr = metrics.get("dscr", {})
    yields = metrics.get("yields", {})
    coc = metrics.get("cash_on_cash", {}).get("by_year", [])
    coc_y1 = coc[0].get("yield") if coc else None

    fund = results.get("fund_waterfall", {}).get("summary", {})

    data = [
        ["Metric", "Levered", "Unlevered", "Partnership"],
        ["IRR", _fmt_pct(irr.get("levered_irr")), _fmt_pct(irr.get("unlevered_irr")),
         _fmt_pct(irr.get("partnership_irr"))],
        ["Equity Multiple", _fmt_mult(em.get("levered_em")), _fmt_mult(em.get("unlevered_em")),
         _fmt_mult(fund.get("partnership_equity_multiple"))],
        ["DSCR (Avg / Min)", f'{_fmt_mult(dscr.get("average_dscr"))} / {_fmt_mult(dscr.get("minimum_dscr"))}',
         "", ""],
        ["Going-In Cap", _fmt_pct(yields.get("going_in_cap_rate")), "", ""],
        ["Cash-on-Cash Y1", _fmt_pct(coc_y1), "", ""],
    ]

    col_widths = [1.5 * inch, 1.2 * inch, 1.2 * inch, 1.2 * inch]
    t = Table(data, colWidths=col_widths)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), SLATE_900),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("TEXTCOLOR", (0, 1), (-1, -1), SLATE_700),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, SLATE_50]),
        ("GRID", (0, 0), (-1, -1), 0.5, SLATE_200),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def _deal_summary_table(inputs: Dict[str, Any], results: Dict[str, Any], styles) -> Table:
    """Build the deal overview table."""
    meta = inputs.get("metadata", {})
    purchase = inputs.get("purchase_assumptions", {})
    exit_a = inputs.get("exit_assumptions", {})
    tg = inputs.get("time_grid", {})
    cohorts = inputs.get("unit_cohorts", [])

    total_units = sum(c.get("unit_count", 0) for c in cohorts)
    total_sf = sum(c.get("unit_count", 0) * c.get("sqft", 0) for c in cohorts)
    pp = purchase.get("purchase_price", 0)
    pp_per_unit = pp / total_units if total_units else 0
    pp_per_sf = pp / total_sf if total_sf else 0

    data = [
        ["Purchase Price", _fmt_currency(pp), "Units", _fmt_number(total_units)],
        ["Price / Unit", _fmt_currency(pp_per_unit), "Year Built", _fmt_number(meta.get("year_built"))],
        ["Price / SF", _fmt_currency(pp_per_sf, abbrev=False), "Location", _location_text(meta)],
        ["Exit Cap Rate", _fmt_pct(exit_a.get("exit_cap_rate")),
         "Hold Period", f'{tg.get("analysis_start_date", "")[:4]}–{tg.get("analysis_end_date", "")[:4]}'],
    ]

    col_widths = [1.2 * inch, 1.3 * inch, 1.0 * inch, 1.2 * inch]
    t = Table(data, colWidths=col_widths)
    t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica"),
        ("FONTNAME", (3, 0), (3, -1), "Helvetica"),
        ("TEXTCOLOR", (0, 0), (0, -1), SLATE_700),
        ("TEXTCOLOR", (2, 0), (2, -1), SLATE_700),
        ("TEXTCOLOR", (1, 0), (1, -1), SLATE_900),
        ("TEXTCOLOR", (3, 0), (3, -1), SLATE_900),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [WHITE, SLATE_50]),
        ("GRID", (0, 0), (-1, -1), 0.5, SLATE_200),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return t


def _comp_table(comp_summary: Dict[str, Any], styles) -> Optional[Table]:
    """Build a comp comparison mini-table for the one-pager."""
    by_bed = comp_summary.get("by_bed_type", [])
    if not by_bed:
        return None

    header = ["Bed Type", "Subject", "Comp Avg", "$/SF Subj", "$/SF Comp", "Premium"]
    rows = [header]

    for row in by_bed:
        premium_val = row.get("premium_pct", 0)
        premium_str = f"{premium_val * 100:+.1f}%"
        rows.append([
            row["bed_type"],
            f"${row['subject_rent']:,.0f}",
            f"${row['comp_rent']:,.0f}",
            f"${row['subject_rent_psf']:.2f}",
            f"${row['comp_rent_psf']:.2f}",
            premium_str,
        ])

    col_widths = [0.7 * inch, 0.8 * inch, 0.8 * inch, 0.8 * inch, 0.8 * inch, 0.8 * inch]
    t = Table(rows, colWidths=col_widths)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), SLATE_900),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("TEXTCOLOR", (0, 1), (-1, -1), SLATE_700),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, SLATE_50]),
        ("GRID", (0, 0), (-1, -1), 0.5, SLATE_200),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    return t


def _scenario_table(scenario_results: Dict[str, Any], styles) -> Optional[Table]:
    """Build the Bull/Base/Bear comparison mini-table."""
    comparison = scenario_results.get("comparison")
    if not comparison:
        return None

    header = ["", "Bull", "Base", "Bear"]
    rows = [header]

    metrics_map = [
        ("Levered IRR", "levered_irr", _fmt_pct),
        ("Levered EM", "levered_em", _fmt_mult),
        ("Unlevered IRR", "unlevered_irr", _fmt_pct),
    ]

    for label, key, fmt in metrics_map:
        row = [label]
        for scenario in ["bull", "base", "bear"]:
            val = comparison.get(scenario, {}).get("metrics", {}).get(key)
            row.append(fmt(val))
        rows.append(row)

    # Assumptions rows (no blank separator)
    rg_row = ["Rent Growth"]
    for scenario in ["bull", "base", "bear"]:
        val = comparison.get(scenario, {}).get("assumptions", {}).get("rent_growth")
        rg_row.append(_fmt_pct(val))
    rows.append(rg_row)

    ec_row = ["Exit Cap"]
    for scenario in ["bull", "base", "bear"]:
        val = comparison.get(scenario, {}).get("assumptions", {}).get("exit_cap_rate")
        ec_row.append(_fmt_pct(val))
    rows.append(ec_row)

    col_widths = [1.2 * inch, 0.9 * inch, 0.9 * inch, 0.9 * inch]
    t = Table(rows, colWidths=col_widths)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), GOLD_DARK),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("TEXTCOLOR", (0, 1), (-1, -1), SLATE_700),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, SLATE_50]),
        ("GRID", (0, 0), (-1, -1), 0.5, SLATE_200),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return t


# ── Main Generator ───────────────────────────────────────────────────────

def generate_onepager(
    results: Dict[str, Any],
    inputs: Dict[str, Any],
    output_path: str | Path,
    scenario_results: Optional[Dict[str, Any]] = None,
    comp_summary: Optional[Dict[str, Any]] = None,
    brand_config: Optional[BrandConfig] = None,
) -> Path:
    """Generate a branded one-page PDF deal summary.

    Args:
        results: Engine output dict (from run_underwriting)
        inputs: Engine input dict (validated deal inputs)
        output_path: Where to save the PDF
        scenario_results: Optional Bull/Base/Bear scenario results
        comp_summary: Optional comp comparison from market_integration
            (output of MarketContext.comp_comparison())
        brand_config: Optional brand config for JV co-branding.
            If None, built from inputs (fund_assumptions.jv_partner_name).

    Returns:
        Path to the generated PDF
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if brand_config is None:
        brand_config = BrandConfig.from_inputs(inputs)

    styles = _build_styles()
    elements = []

    # ── Header ──
    meta = inputs.get("metadata", {})
    deal_name = meta.get("deal_id", "Deal Summary")
    address = meta.get("address", "")

    # Partner logo in top-right (if configured and file exists)
    if brand_config.has_partner and brand_config.partner_logo_path:
        logo_path = brand_config.partner_logo_path
        if logo_path.exists():
            logo_img = Image(str(logo_path), width=1.2 * inch, height=0.5 * inch)
            # Two-column header: title left, logo right
            header_data = [[
                Paragraph(deal_name, styles["BrandTitle"]),
                logo_img,
            ]]
            header_table = Table(header_data, colWidths=[5.0 * inch, 1.5 * inch])
            header_table.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
            ]))
            elements.append(header_table)
        else:
            elements.append(Paragraph(deal_name, styles["BrandTitle"]))
    else:
        elements.append(Paragraph(deal_name, styles["BrandTitle"]))

    if address:
        elements.append(Paragraph(address, styles["BrandSubtitle"]))

    # "Prepared for [Partner]" subtitle when co-branded
    if brand_config.has_partner:
        elements.append(Paragraph(
            brand_config.subtitle_text, styles["BrandSubtitle"],
        ))

    elements.append(Paragraph(
        f"Generated {datetime.now().strftime('%B %d, %Y')} | {brand_config.header_text}",
        styles["BrandSubtitle"],
    ))
    elements.append(HRFlowable(
        width="100%", thickness=1, color=GOLD, spaceAfter=4,
    ))

    # Determine if we need compact layout (scenarios/comps take extra space)
    has_scenarios = scenario_results and scenario_results.get("comparison")
    has_comps = comp_summary and comp_summary.get("by_bed_type")

    # ── Deal Overview ──
    elements.append(Paragraph("Deal Overview", styles["SectionHeader"]))
    elements.append(_deal_summary_table(inputs, results, styles))
    elements.append(Spacer(1, 4))

    # ── Returns ──
    elements.append(Paragraph("Investment Returns", styles["SectionHeader"]))
    elements.append(_returns_table(results, styles))
    elements.append(Spacer(1, 4))

    # ── Cash Flow Chart (smaller when extras present) ──
    compact = has_scenarios or has_comps
    chart_h = 1.4 if compact else 1.8
    chart_w = 5.5 if compact else 6.0
    chart_png = _generate_cf_chart(results, height_inches=chart_h, width_inches=chart_w)
    if chart_png:
        elements.append(Paragraph("Annual Cash Flow", styles["SectionHeader"]))
        chart_img = Image(io.BytesIO(chart_png), width=chart_w * inch, height=chart_h * inch)
        elements.append(chart_img)
        elements.append(Spacer(1, 2))

    # ── Waterfall Distribution Chart (multi-tier only) ──
    fund_wf = results.get("fund_waterfall", {})
    wf_promote = fund_wf.get("promote", {})
    wf_by_tier = wf_promote.get("by_tier", [])
    if len(wf_by_tier) > 1:
        wf_chart_h = 1.4 if compact else 1.8
        wf_chart_w = 5.5 if compact else 6.0
        wf_chart_png = _generate_waterfall_chart(results, height_inches=wf_chart_h, width_inches=wf_chart_w)
        if wf_chart_png:
            elements.append(Paragraph("Waterfall Distribution", styles["SectionHeader"]))
            wf_img = Image(io.BytesIO(wf_chart_png), width=wf_chart_w * inch, height=wf_chart_h * inch)
            elements.append(wf_img)
            elements.append(Spacer(1, 2))

    # ── Scenario Comparison (optional) ──
    if has_scenarios:
        sc_table = _scenario_table(scenario_results, styles)
        if sc_table:
            elements.append(Paragraph("Scenario Comparison", styles["SectionHeader"]))
            elements.append(sc_table)
            elements.append(Spacer(1, 2))

    # ── Market Comps (optional) ──
    if has_comps:
        ct = _comp_table(comp_summary, styles)
        if ct:
            elements.append(Paragraph("Market Comps", styles["SectionHeader"]))
            elements.append(ct)
            elements.append(Spacer(1, 2))

    # ── Key Assumptions ──
    elements.append(Paragraph("Key Assumptions", styles["SectionHeader"]))
    ga = inputs.get("growth_assumptions", {})
    ea = inputs.get("exit_assumptions", {})
    debt = inputs.get("debt_terms", {})
    assumptions_text = (
        f"Rent Growth: {_fmt_pct(ga.get('annual_growth_rate'))} | "
        f"Exit Cap: {_fmt_pct(ea.get('exit_cap_rate'))} | "
        f"Loan Rate: {_fmt_pct(debt.get('rate'))} | "
        f"LTV: {_fmt_pct(debt.get('ltv'))}"
    )
    elements.append(Paragraph(assumptions_text, styles["BrandBody"]))

    # ── Footer ──
    elements.append(Spacer(1, 6))
    elements.append(HRFlowable(
        width="100%", thickness=0.5, color=SLATE_200, spaceAfter=4,
    ))
    footer_text = "Confidential"
    if brand_config.has_partner:
        footer_text += f" — {brand_config.company_name} & {brand_config.partner_name}"
    footer_text += " — For authorized recipients only. Projections are estimates and not guarantees of future performance."
    elements.append(Paragraph(footer_text, styles["FooterText"]))

    # Build PDF
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=letter,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.5 * inch,
        bottomMargin=0.5 * inch,
    )
    doc.build(elements)
    return output_path
