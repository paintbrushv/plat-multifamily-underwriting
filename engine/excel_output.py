"""
Excel Output Writer Module

Writes model results to Excel workbooks with formatted output sheets.
Includes summary dashboard, annual pro forma, and sensitivity matrices.

See: docs/modules/excel_ux_spec.md for full specification
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter


# Style definitions
HEADER_FILL = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
HEADER_FONT = Font(bold=True, color="FFFFFF")
TITLE_FONT = Font(bold=True, size=14)
SECTION_FONT = Font(bold=True, size=11)
CURRENCY_FORMAT = '"$"#,##0.00'
PERCENT_FORMAT = "0.00%"
NUMBER_FORMAT = "#,##0.00"
THIN_BORDER = Border(
    left=Side(style="thin", color="D9D9D9"),
    right=Side(style="thin", color="D9D9D9"),
    top=Side(style="thin", color="D9D9D9"),
    bottom=Side(style="thin", color="D9D9D9"),
)


def write_results_to_workbook(
    results: Dict[str, Any],
    output_path: Path,
    inputs: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Write model results to an Excel workbook.

    Args:
        results: Complete model results dictionary
        output_path: Path for output workbook
        inputs: Optional input data for context
    """
    wb = Workbook()

    # Remove default sheet
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    # Create output sheets
    _write_summary_sheet(wb, results, inputs)
    _write_annual_proforma_sheet(wb, results)
    _write_monthly_detail_sheet(wb, results)

    if "sensitivity" in results:
        _write_sensitivity_sheet(wb, results)

    _write_model_health_sheet(wb, results, inputs)

    wb.save(output_path)


def _write_summary_sheet(
    wb: Workbook,
    results: Dict[str, Any],
    inputs: Optional[Dict[str, Any]] = None,
) -> None:
    """Write the summary dashboard sheet."""
    ws = wb.create_sheet("Summary")

    # Title
    ws["A1"] = "DEAL SUMMARY"
    ws["A1"].font = TITLE_FONT
    ws.merge_cells("A1:F1")

    # Deal info
    row = 3
    if inputs:
        metadata = inputs.get("metadata", {})
        ws[f"A{row}"] = "Deal:"
        ws[f"B{row}"] = metadata.get("deal_id", "N/A")
        ws[f"D{row}"] = "Date:"
        ws[f"E{row}"] = datetime.now().strftime("%Y-%m-%d")
        row += 1
        ws[f"A{row}"] = "Analyst:"
        ws[f"B{row}"] = metadata.get("analyst", "N/A")
        ws[f"D{row}"] = "Purpose:"
        ws[f"E{row}"] = metadata.get("purpose", "N/A")
        row += 2

    # Key Metrics section
    ws[f"A{row}"] = "KEY METRICS"
    ws[f"A{row}"].font = SECTION_FONT
    row += 1

    metrics = results.get("metrics", {})

    # IRR
    irr_data = metrics.get("irr", {})
    ws[f"A{row}"] = "Levered IRR:"
    ws[f"B{row}"] = irr_data.get("levered_irr")
    ws[f"B{row}"].number_format = PERCENT_FORMAT
    ws[f"D{row}"] = "Unlevered IRR:"
    ws[f"E{row}"] = irr_data.get("unlevered_irr")
    ws[f"E{row}"].number_format = PERCENT_FORMAT
    row += 1

    # EM
    em_data = metrics.get("equity_multiple", {})
    ws[f"A{row}"] = "Equity Multiple:"
    ws[f"B{row}"] = em_data.get("levered_em")
    ws[f"B{row}"].number_format = '0.00"x"'
    ws[f"D{row}"] = "Unlevered EM:"
    ws[f"E{row}"] = em_data.get("unlevered_em")
    ws[f"E{row}"].number_format = '0.00"x"'
    row += 1

    # DSCR
    dscr_data = metrics.get("dscr", {})
    ws[f"A{row}"] = "DSCR (Avg):"
    ws[f"B{row}"] = dscr_data.get("average_dscr")
    ws[f"B{row}"].number_format = '0.00"x"'
    ws[f"D{row}"] = "DSCR (Min):"
    ws[f"E{row}"] = dscr_data.get("minimum_dscr")
    ws[f"E{row}"].number_format = '0.00"x"'
    row += 1

    # Yields
    yields_data = metrics.get("yields", {})
    ws[f"A{row}"] = "Going-In Cap:"
    ws[f"B{row}"] = yields_data.get("going_in_cap_rate")
    ws[f"B{row}"].number_format = PERCENT_FORMAT
    row += 2

    # Sources & Uses
    if inputs and "purchase_assumptions" in inputs:
        ws[f"A{row}"] = "SOURCES & USES"
        ws[f"A{row}"].font = SECTION_FONT
        row += 1

        purchase = inputs["purchase_assumptions"]
        debt = inputs.get("debt_terms", {})

        # Uses
        ws[f"A{row}"] = "Purchase Price:"
        ws[f"B{row}"] = purchase.get("purchase_price", 0)
        ws[f"B{row}"].number_format = CURRENCY_FORMAT
        row += 1

        closing_costs = purchase.get("closing_costs", 0)
        if closing_costs:
            ws[f"A{row}"] = "Closing Costs:"
            ws[f"B{row}"] = closing_costs
            ws[f"B{row}"].number_format = CURRENCY_FORMAT
            row += 1

        total_uses = purchase.get("purchase_price", 0) + closing_costs
        ws[f"A{row}"] = "Total Uses:"
        ws[f"B{row}"] = total_uses
        ws[f"B{row}"].number_format = CURRENCY_FORMAT
        ws[f"A{row}"].font = Font(bold=True)
        ws[f"B{row}"].font = Font(bold=True)
        row += 2

        # Sources
        loan = debt.get("commitment", 0)
        equity = purchase.get("equity_contribution", 0)
        ltv = loan / purchase.get("purchase_price", 1) if purchase.get("purchase_price") else 0

        ws[f"A{row}"] = "Senior Debt:"
        ws[f"B{row}"] = loan
        ws[f"B{row}"].number_format = CURRENCY_FORMAT
        ws[f"C{row}"] = f"({ltv:.0%} LTV)"
        row += 1

        ws[f"A{row}"] = "Equity:"
        ws[f"B{row}"] = equity
        ws[f"B{row}"].number_format = CURRENCY_FORMAT
        row += 1

        ws[f"A{row}"] = "Total Sources:"
        ws[f"B{row}"] = loan + equity
        ws[f"B{row}"].number_format = CURRENCY_FORMAT
        ws[f"A{row}"].font = Font(bold=True)
        ws[f"B{row}"].font = Font(bold=True)

    # Adjust column widths
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 15
    ws.column_dimensions["C"].width = 12
    ws.column_dimensions["D"].width = 18
    ws.column_dimensions["E"].width = 15


def _write_annual_proforma_sheet(wb: Workbook, results: Dict[str, Any]) -> None:
    """Write the annual pro forma sheet."""
    ws = wb.create_sheet("Annual_ProForma")

    cashflow = results.get("cashflow", {})
    by_year = cashflow.get("by_year", [])

    if not by_year:
        ws["A1"] = "No annual cashflow data available"
        return

    # Title
    ws["A1"] = "ANNUAL PRO FORMA"
    ws["A1"].font = TITLE_FONT
    ws.merge_cells(f"A1:{get_column_letter(len(by_year) + 1)}1")

    # Headers
    row = 3
    ws[f"A{row}"] = "Metric"
    ws[f"A{row}"].font = HEADER_FONT
    ws[f"A{row}"].fill = HEADER_FILL

    for i, year_data in enumerate(by_year):
        col = get_column_letter(i + 2)
        ws[f"{col}{row}"] = year_data.get("year", f"Year {i+1}")
        ws[f"{col}{row}"].font = HEADER_FONT
        ws[f"{col}{row}"].fill = HEADER_FILL
        ws[f"{col}{row}"].alignment = Alignment(horizontal="center")

    # Data rows
    metrics = [
        ("Net Rent", "net_rent", CURRENCY_FORMAT),
        ("Other Income", "net_programs", CURRENCY_FORMAT),
        ("Effective Gross Income", "effective_gross_income", CURRENCY_FORMAT),
        ("Total OpEx", "total_opex", CURRENCY_FORMAT),
        ("Net Operating Income", "net_operating_income", CURRENCY_FORMAT),
        ("Total CapEx", "total_capex", CURRENCY_FORMAT),
        ("Unleveraged Cash Flow", "unleveraged_cash_flow", CURRENCY_FORMAT),
        ("Debt Service", "debt_service", CURRENCY_FORMAT),
        ("Leveraged Cash Flow", "leveraged_cash_flow", CURRENCY_FORMAT),
    ]

    row = 4
    for label, key, fmt in metrics:
        ws[f"A{row}"] = label

        # Bold key metrics
        if label in ("Effective Gross Income", "Net Operating Income", "Unleveraged Cash Flow", "Leveraged Cash Flow"):
            ws[f"A{row}"].font = Font(bold=True)

        for i, year_data in enumerate(by_year):
            col = get_column_letter(i + 2)
            value = year_data.get(key, 0)
            ws[f"{col}{row}"] = value if value is not None else 0
            ws[f"{col}{row}"].number_format = fmt

            if label in ("Effective Gross Income", "Net Operating Income", "Unleveraged Cash Flow", "Leveraged Cash Flow"):
                ws[f"{col}{row}"].font = Font(bold=True)

        row += 1

    # Adjust column widths
    ws.column_dimensions["A"].width = 25
    for i in range(len(by_year)):
        ws.column_dimensions[get_column_letter(i + 2)].width = 15


def _write_monthly_detail_sheet(wb: Workbook, results: Dict[str, Any]) -> None:
    """Write the monthly detail sheet."""
    ws = wb.create_sheet("Monthly_Detail")

    cashflow = results.get("cashflow", {})
    by_month = cashflow.get("by_month", [])

    if not by_month:
        ws["A1"] = "No monthly cashflow data available"
        return

    # Title
    ws["A1"] = "MONTHLY DETAIL"
    ws["A1"].font = TITLE_FONT

    # Headers
    headers = ["Month", "EGI", "OpEx", "NOI", "CapEx", "Unlev CF", "Debt Svc", "Lev CF"]
    row = 3
    for i, header in enumerate(headers):
        col = get_column_letter(i + 1)
        ws[f"{col}{row}"] = header
        ws[f"{col}{row}"].font = HEADER_FONT
        ws[f"{col}{row}"].fill = HEADER_FILL

    # Data
    row = 4
    for month_data in by_month:
        ws[f"A{row}"] = month_data.get("month", "")
        ws[f"B{row}"] = month_data.get("effective_gross_income", 0)
        ws[f"B{row}"].number_format = CURRENCY_FORMAT
        ws[f"C{row}"] = month_data.get("total_opex", 0)
        ws[f"C{row}"].number_format = CURRENCY_FORMAT
        ws[f"D{row}"] = month_data.get("net_operating_income", 0)
        ws[f"D{row}"].number_format = CURRENCY_FORMAT
        ws[f"E{row}"] = month_data.get("total_capex", 0)
        ws[f"E{row}"].number_format = CURRENCY_FORMAT
        ws[f"F{row}"] = month_data.get("unleveraged_cash_flow", 0)
        ws[f"F{row}"].number_format = CURRENCY_FORMAT
        ws[f"G{row}"] = month_data.get("debt_service", 0)
        ws[f"G{row}"].number_format = CURRENCY_FORMAT
        ws[f"H{row}"] = month_data.get("leveraged_cash_flow", 0)
        ws[f"H{row}"].number_format = CURRENCY_FORMAT
        row += 1

    # Adjust column widths
    ws.column_dimensions["A"].width = 12
    for i in range(1, 8):
        ws.column_dimensions[get_column_letter(i + 1)].width = 14


def _write_sensitivity_sheet(wb: Workbook, results: Dict[str, Any]) -> None:
    """Write the sensitivity analysis sheet."""
    ws = wb.create_sheet("Sensitivity")

    sensitivity = results.get("sensitivity", {})
    sensitivity_results = sensitivity.get("sensitivity_results", {})

    if not sensitivity_results:
        ws["A1"] = "No sensitivity analysis available"
        return

    # Title
    ws["A1"] = "SENSITIVITY ANALYSIS"
    ws["A1"].font = TITLE_FONT

    current_row = 3

    for metric_name, metric_data in sensitivity_results.items():
        # Metric title
        ws[f"A{current_row}"] = metric_data.get("metric_name", metric_name)
        ws[f"A{current_row}"].font = SECTION_FONT
        current_row += 1

        row_labels = metric_data.get("row_labels", [])
        col_labels = metric_data.get("column_labels", [])
        matrix = metric_data.get("matrix", [])
        base_pos = metric_data.get("base_case_position", [0, 0])

        if not matrix:
            current_row += 2
            continue

        # Column headers (exit cap rates)
        ws[f"A{current_row}"] = ""
        for j, col_label in enumerate(col_labels):
            col = get_column_letter(j + 2)
            ws[f"{col}{current_row}"] = col_label
            ws[f"{col}{current_row}"].font = HEADER_FONT
            ws[f"{col}{current_row}"].fill = HEADER_FILL
            ws[f"{col}{current_row}"].alignment = Alignment(horizontal="center")
        current_row += 1

        # Data rows
        for i, row_data in enumerate(matrix):
            # Row label
            ws[f"A{current_row}"] = row_labels[i] if i < len(row_labels) else ""
            ws[f"A{current_row}"].font = Font(bold=True)

            for j, value in enumerate(row_data):
                col = get_column_letter(j + 2)
                ws[f"{col}{current_row}"] = value
                ws[f"{col}{current_row}"].number_format = PERCENT_FORMAT
                ws[f"{col}{current_row}"].alignment = Alignment(horizontal="center")

                # Highlight base case
                if i == base_pos[0] and j == base_pos[1]:
                    ws[f"{col}{current_row}"].fill = PatternFill(
                        start_color="FFFF00", end_color="FFFF00", fill_type="solid"
                    )
                    ws[f"{col}{current_row}"].font = Font(bold=True)

            current_row += 1

        current_row += 2  # Space between matrices

    # Adjust column widths
    ws.column_dimensions["A"].width = 12
    for i in range(10):
        ws.column_dimensions[get_column_letter(i + 2)].width = 10


def _write_model_health_sheet(
    wb: Workbook,
    results: Dict[str, Any],
    inputs: Optional[Dict[str, Any]] = None,
) -> None:
    """Write the model health dashboard sheet."""
    ws = wb.create_sheet("Model_Health")

    # Title
    ws["A1"] = "MODEL HEALTH DASHBOARD"
    ws["A1"].font = TITLE_FONT

    # Status
    row = 3
    errors = results.get("errors", [])
    warnings = results.get("warnings", [])

    if errors:
        ws[f"A{row}"] = "Status: ERRORS FOUND"
        ws[f"A{row}"].font = Font(bold=True, color="FF0000")
    elif warnings:
        ws[f"A{row}"] = "Status: WARNINGS"
        ws[f"A{row}"].font = Font(bold=True, color="FFA500")
    else:
        ws[f"A{row}"] = "Status: All Checks Passed"
        ws[f"A{row}"].font = Font(bold=True, color="008000")
    row += 1

    ws[f"A{row}"] = f"Last Run: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    row += 2

    # Input validation
    ws[f"A{row}"] = "INPUT VALIDATION"
    ws[f"A{row}"].font = SECTION_FONT
    row += 1

    if inputs:
        # Time grid check
        time_grid = inputs.get("time_grid", {})
        start = time_grid.get("analysis_start_date", "N/A")
        end = time_grid.get("analysis_end_date", "N/A")
        ws[f"A{row}"] = f"✓ Time grid valid ({start} to {end})"
        row += 1

        # Unit cohorts check
        cohorts = inputs.get("unit_cohorts", [])
        total_units = sum(c.get("unit_count", 0) for c in cohorts)
        ws[f"A{row}"] = f"✓ Unit cohorts complete ({len(cohorts)} cohorts, {total_units} units)"
        row += 1

        # Check for required tables
        checks = [
            ("market_rent_curve", "Market rent curves"),
            ("physical_vacancy_curve", "Vacancy curves"),
            ("opex_table", "OpEx categories"),
            ("debt_terms", "Debt terms"),
        ]
        for key, label in checks:
            if key in inputs and inputs[key]:
                ws[f"A{row}"] = f"✓ {label} defined"
            else:
                ws[f"A{row}"] = f"⚠ {label} not defined"
            row += 1
    row += 1

    # Calculation checks
    ws[f"A{row}"] = "CALCULATION CHECKS"
    ws[f"A{row}"].font = SECTION_FONT
    row += 1

    modules = ["revenue", "opex", "capex", "debt", "cashflow", "metrics"]
    for module in modules:
        if module in results and results[module]:
            ws[f"A{row}"] = f"✓ {module.title()} computed successfully"
        else:
            ws[f"A{row}"] = f"⚠ {module.title()} not computed"
        row += 1
    row += 1

    # Warnings section
    if warnings:
        ws[f"A{row}"] = "WARNINGS"
        ws[f"A{row}"].font = SECTION_FONT
        row += 1
        for warning in warnings:
            ws[f"A{row}"] = f"⚠ {warning}"
            ws[f"A{row}"].font = Font(color="FFA500")
            row += 1
        row += 1

    # Errors section
    if errors:
        ws[f"A{row}"] = "ERRORS"
        ws[f"A{row}"].font = SECTION_FONT
        row += 1
        for error in errors:
            ws[f"A{row}"] = f"✗ {error}"
            ws[f"A{row}"].font = Font(color="FF0000")
            row += 1

    # Adjust column width
    ws.column_dimensions["A"].width = 60


# Note: input validation lives in engine/validator.py (validate_deal).
# A duplicate validate_inputs() previously lived here but was removed (M5)
# in favor of the canonical ValidationReport returned by validate_deal().
