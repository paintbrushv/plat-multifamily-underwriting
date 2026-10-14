#!/usr/bin/env python3
"""Build the synthetic deal library (ADR 0003 §9, slice 8).

Every deal here is invented. Each deal folder holds a rent roll and a T12 that
agree with each other (the T12 gross potential rent is the rent roll's market
rent x 12, ramped over the trailing year; opex is 35-50% of EGI), plus a
``deal.json`` with the deal profile, the deal economics, the analyst inputs
the documents cannot carry (property-tax millage, a renovation program),
synthetic market data, and the expected bands the tests assert.

The script is deterministic: no RNG, no clock. Amounts are built in integer
cents and spread over the twelve months with fixed weights.

Usage:
    python tests/fixtures/deals/build_fixtures.py               # write rent_roll.csv, t12.csv, deal.json
    python tests/fixtures/deals/build_fixtures.py --check       # exit 1 if a committed file differs
    python tests/fixtures/deals/build_fixtures.py --update-goldens
        # also re-run each deal through ingest and rewrite expected_assumptions.json

The golden snapshots (``expected_assumptions.json``) are never rewritten by
the tests. Regenerate them only with ``--update-goldens`` after a reviewed
change to the assumption layer, and commit the diff with that change.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]

ANALYSIS_START = "2026-11"
ANALYSIS_END = "2031-10"
# Trailing twelve months ending the month before the analysis starts.
T12_MONTHS = (
    "Nov 2025", "Dec 2025", "Jan 2026", "Feb 2026", "Mar 2026", "Apr 2026",
    "May 2026", "Jun 2026", "Jul 2026", "Aug 2026", "Sep 2026", "Oct 2026",
)
# Monthly weights (each set sums to 12): a mild spread for most lines and a
# winter/summer swing for utilities.
FLAT = (1.00, 0.99, 1.01, 1.00, 0.98, 1.02, 1.00, 1.01, 0.99, 1.00, 1.01, 0.99)
UTILITY = (1.08, 1.12, 1.15, 1.10, 0.98, 0.90, 0.88, 0.92, 1.00, 1.02, 0.95, 0.90)
# The trailing year's GPR ramps up to today's rent roll (rents rose over the year).
GPR_RAMP_START = Decimal("0.985")
MARKET_AS_OF = "2026-09-30"
PROVIDER = "synthetic"


# ── specs ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class UnitType:
    label: str
    beds: int
    baths: int
    sqft: int
    count: int
    market_rent: int
    vacant: int


@dataclass(frozen=True)
class Line:
    """One T12 line: label, $/unit a year (or $/unit a month for income), expected parser category."""

    label: str
    per_unit: float
    category: Optional[str] = None
    weights: tuple[float, ...] = FLAT


@dataclass(frozen=True)
class Section:
    header: str
    lines: tuple[Line, ...]
    # A bare rollup row repeating the section name, equal to the section sum.
    rollup: Optional[str] = None


@dataclass
class DealSpec:
    slug: str
    property_id: str
    property_name: str
    summary: str
    year_built: int
    deal_type: str
    msa: dict[str, str]
    submarket: str
    unit_types: tuple[UnitType, ...]
    ltl_pattern: tuple[float, ...]
    vacancy_loss: float
    concessions: Optional[float]
    bad_debt: float
    bad_debt_label: str
    other_income: tuple[Line, ...]
    reimbursements: tuple[Line, ...]
    expenses: tuple[Section, ...]
    management_fee_pct: float
    going_in_cap: float
    millage_mills: float
    assessment_ratio: float
    prior_tax_factor: float
    loan: dict[str, Any]
    exit: dict[str, Any]
    market: Optional[dict[str, Any]]
    expected: dict[str, Any]
    renovation_programs: list[dict[str, Any]] = field(default_factory=list)
    # Property condition assessment evidence (capital_plan.pca, source deal:pca).
    pca: Optional[dict[str, Any]] = None


# ── shared opex sections ─────────────────────────────────────────────────


def _payroll(*lines: Line) -> Section:
    return Section("Payroll", lines)


DEALS: tuple[DealSpec, ...] = (
    DealSpec(
        slug="a_stabilized_1990s",
        property_id="SYN-A-LARKSPUR",
        property_name="Larkspur Bend Apartments (synthetic)",
        summary="Stabilized 1994 garden deal, 200 units, submarket and MSA market data.",
        year_built=1994,
        deal_type="stabilized",
        msa={"name": "Larkspur Synthetic MSA", "metro_slug": "larkspur_sx", "cbsa_code": "99901"},
        submarket="Larkspur North",
        unit_types=(
            UnitType("1BR/1BA", 1, 1, 680, 80, 1250, 4),
            UnitType("2BR/2BA", 2, 2, 980, 96, 1550, 5),
            UnitType("3BR/2BA", 3, 2, 1220, 24, 1850, 1),
        ),
        ltl_pattern=(0.02, 0.03, 0.05, 0.04, 0.03, 0.035, 0.025, 0.04),
        vacancy_loss=0.055,
        concessions=0.008,
        bad_debt=0.008,
        bad_debt_label="Bad Debt",
        other_income=(
            Line("Application Fees", 4),
            Line("Late Fees", 6),
            Line("Pet Rent", 9),
            Line("Admin Fees", 4),
            Line("Damages", 3),
        ),
        reimbursements=(
            Line("RUBS - Water", 32),
            Line("Trash Billback", 8),
            Line("Valet Trash", 5),
        ),
        expenses=(
            _payroll(
                Line("Payroll - Office", 520, "Payroll"),
                Line("Payroll - Leasing", 380, "Payroll"),
                Line("Payroll - Maintenance", 600, "Payroll"),
            ),
            Section("Repairs & Maintenance", (
                Line("Maintenance Supplies", 300, "Repairs & Maintenance"),
                Line("Plumbing Repairs", 160, "Repairs & Maintenance"),
                Line("HVAC Repairs", 140, "Repairs & Maintenance"),
                Line("Electrical Repairs/Supplies", 90, "Repairs & Maintenance"),
                Line("Water Extraction", 40, "Repairs & Maintenance"),
            )),
            Section("Turnover", (
                Line("Turnover - Paint", 120, "Turnover / Make-Ready"),
                Line("Turnover - Cleaning", 100, "Turnover / Make-Ready"),
                Line("Turnover - Carpet Cleaning", 70, "Turnover / Make-Ready"),
            )),
            Section("Contract Services", (
                Line("Landscaping", 180, "Landscaping / Grounds"),
                Line("Pest Control", 45, "Pest Control"),
            )),
            Section("Utilities", (
                Line("Utilities - Electric", 260, "Utilities", UTILITY),
                Line("Utilities - Water", 420, "Utilities", UTILITY),
                Line("Utilities - Gas", 90, "Utilities", UTILITY),
                Line("Utilities - Trash", 110, "Utilities"),
            )),
            Section("Administrative", (
                Line("Office Supplies", 90, "Office Supplies"),
                Line("Telephone", 60, "Telephone"),
            )),
            Section("Marketing", (Line("Marketing - Internet Listing", 140, "Marketing / Advertising"),)),
            Section("Insurance", (Line("Property Insurance", 650, "Insurance"),)),
        ),
        management_fee_pct=0.03,
        going_in_cap=0.055,
        millage_mills=20.0,
        assessment_ratio=0.70,
        prior_tax_factor=0.93,
        loan={"ltv": 0.60, "loan_rate": 0.0575, "amort_years": 30, "io_months": 24},
        exit={"exit_month": "2031-10", "sale_cost_pct": 0.02, "exit_cap_rate": None},
        market={
            "confidence": "high",
            "observations": [
                {"metric": "rent_growth_forecast", "level": "submarket", "value": {
                    "2026Q4": 0.018, "2027Q1": 0.020, "2027Q2": 0.022, "2027Q3": 0.025, "2027Q4": 0.028,
                    "2028Q1": 0.030, "2028Q2": 0.031, "2028Q3": 0.032, "2028Q4": 0.033}},
                {"metric": "rent_growth_forecast", "level": "msa", "value": {
                    "2026Q4": 0.015, "2027Q1": 0.017, "2027Q2": 0.019, "2027Q3": 0.022, "2027Q4": 0.025,
                    "2028Q1": 0.027, "2028Q2": 0.028, "2028Q3": 0.029, "2028Q4": 0.030}},
                {"metric": "rent_growth_long_run", "level": "msa", "value": 0.028},
                {"metric": "vacancy_stabilized", "level": "submarket", "value": 0.055},
                {"metric": "vacancy_stabilized", "level": "msa", "value": 0.058},
                {"metric": "concession_rate", "level": "submarket", "value": 0.010},
                {"metric": "market_cap_rate", "level": "submarket", "value": {
                    "2026Q4": 0.0540, "2027Q4": 0.0550, "2028Q4": 0.0560, "2029Q4": 0.0565,
                    "2030Q4": 0.0570, "2031Q1": 0.0572, "2031Q2": 0.0574, "2031Q3": 0.0576, "2031Q4": 0.0578}},
            ],
        },
        expected={
            "sources": {
                "rent_growth": "market:synthetic",
                "physical_vacancy": "market:synthetic",
                "loss_to_lease": "house:vintage_1990s",
                "concessions": "deal:t12",
                "collection_loss": "deal:t12",
                "exit_cap_rate": "house:exit_cap_spread",
                "insurance_per_unit": "deal:t12",
                "r_and_m_per_unit": "deal:t12",
                "replacement_reserve_per_unit": "house:replacement_reserve",
                "immediate_capex": "house:deferred_capex",
                "recurring_capex_per_unit": "house:recurring_capex_vintage",
                "recurring_capex_growth": "house:opex_growth_table",
                "purchase_closing_cost_pct": "house:backsolve_policy",
                "sale_cost_percent": "analyst",
            },
            "bands": {
                "rent_growth": [[0.026, 0.030], [0.031, 0.035], [0.029, 0.032], [0.027, 0.029]],
                "vacancy_start": [0.045, 0.055],
                "vacancy_stabilized": [0.050, 0.060],
                "loss_to_lease_start": [0.030, 0.040],
                "loss_to_lease_floor": [0.010, 0.010],
                "concessions_year1": [0.006, 0.010],
                "concessions_stabilized": [0.008, 0.012],
                "collection_loss": [0.006, 0.010],
                "exit_cap_rate": [0.0620, 0.0626],
                "insurance_per_unit": [600, 700],
                "r_and_m_per_unit": [990, 1050],
                "replacement_reserve_per_unit": [250, 250],
                "recurring_capex_per_unit": [750, 750],
                "immediate_capex": [0, 0],
            },
            "opex_growth": {
                "Payroll": [0.035, 0.035],
                "Repairs & Maintenance": [0.03, 0.03],
                "Turnover / Make-Ready": [0.03, 0.03],
                "Landscaping / Grounds": [0.03, 0.03],
                "Pest Control": [0.03, 0.03],
                "Utilities": [0.035, 0.035],
                "Office Supplies": [0.025, 0.03],
                "Telephone": [0.025, 0.03],
                "Marketing / Advertising": [0.025, 0.025],
                "Insurance": [0.05, 0.05],
                "Real Estate Taxes": [0.03, 0.03],
                "Property Management Fee": [0.0, 0.0],
            },
            "results": {
                "unlevered_irr": [0.05, 0.10],
                "levered_irr": [0.05, 0.16],
                "minimum_dscr": [1.15, 2.0],
                "going_in_cap_rate": [0.048, 0.060],
            },
            "guardrail_warnings": [],
            "guardrail_waivers": [],
        },
    ),
    DealSpec(
        slug="b_stabilized_2010s_no_market",
        property_id="SYN-B-TESSALINE",
        property_name="Tessaline Court (synthetic)",
        summary="Stabilized 2014 mid-rise deal, 120 units, no market data (house fallback path).",
        year_built=2014,
        deal_type="stabilized",
        msa={"name": "Tessaline Synthetic MSA", "metro_slug": "tessaline_sx"},
        submarket="Tessaline Uptown",
        unit_types=(
            UnitType("1BR/1BA", 1, 1, 720, 54, 1650, 3),
            UnitType("2BR/2BA", 2, 2, 1080, 54, 2050, 2),
            UnitType("3BR/2BA", 3, 2, 1300, 12, 2450, 1),
        ),
        ltl_pattern=(0.01, 0.02, 0.03, 0.025, 0.035, 0.015),
        vacancy_loss=0.05,
        concessions=0.015,
        bad_debt=0.005,
        bad_debt_label="Bad Debt",
        other_income=(
            Line("Application Fees", 5),
            Line("Late Fees", 5),
            Line("Pet Rent", 14),
            Line("Parking Income", 20),
            Line("Storage Income", 6),
        ),
        reimbursements=(
            Line("Utility Reimbursement - Water", 45),
            Line("Trash Billback", 9),
            Line("Pest Control Billback", 3),
        ),
        expenses=(
            _payroll(
                Line("Payroll - Office", 620, "Payroll"),
                Line("Payroll - Maintenance", 780, "Payroll"),
            ),
            Section("Repairs & Maintenance", (
                Line("Maintenance Supplies", 280, "Repairs & Maintenance"),
                Line("Plumbing Repairs", 120, "Repairs & Maintenance"),
                Line("HVAC Repairs", 150, "Repairs & Maintenance"),
            )),
            Section("Turnover", (
                Line("Turnover - Paint", 140, "Turnover / Make-Ready"),
                Line("Turnover - Cleaning", 110, "Turnover / Make-Ready"),
            )),
            Section("Contract Services", (
                Line("Landscaping", 200, "Landscaping / Grounds"),
                Line("Pest Control", 40, "Pest Control"),
            )),
            Section("Utilities", (
                Line("Utilities - Electric", 180, "Utilities", UTILITY),
                Line("Utilities - Water", 450, "Utilities", UTILITY),
                Line("Utilities - Trash", 120, "Utilities"),
            )),
            Section("Administrative", (
                Line("Office Supplies", 80, "Office Supplies"),
                Line("Telephone", 70, "Telephone"),
            )),
            Section("Marketing", (Line("Marketing - Internet Listing", 180, "Marketing / Advertising"),)),
            Section("Insurance", (Line("Property Insurance", 560, "Insurance"),)),
        ),
        management_fee_pct=0.03,
        going_in_cap=0.0525,
        millage_mills=22.0,
        assessment_ratio=0.65,
        prior_tax_factor=0.95,
        loan={"ltv": 0.60, "loan_rate": 0.0575, "amort_years": 30, "io_months": 36},
        exit={"exit_month": "2031-10", "sale_cost_pct": 0.02, "exit_cap_rate": 0.0575},
        market=None,
        expected={
            "sources": {
                "rent_growth": "house:long_run_rent_growth",
                "physical_vacancy": "house:vintage_2010s",
                "loss_to_lease": "house:vintage_2010s",
                "concessions": "deal:t12",
                "collection_loss": "deal:t12",
                "exit_cap_rate": "analyst",
                "insurance_per_unit": "deal:t12",
                "r_and_m_per_unit": "deal:t12",
                "replacement_reserve_per_unit": "house:replacement_reserve",
                "immediate_capex": "house:deferred_capex",
                "recurring_capex_per_unit": "house:recurring_capex_vintage",
                "recurring_capex_growth": "house:opex_growth_table",
                "purchase_closing_cost_pct": "house:backsolve_policy",
                "sale_cost_percent": "analyst",
            },
            "bands": {
                "rent_growth": [[0.025, 0.025], [0.025, 0.025], [0.025, 0.025], [0.025, 0.025]],
                "vacancy_start": [0.045, 0.055],
                "vacancy_stabilized": [0.050, 0.055],
                "loss_to_lease_start": [0.015, 0.030],
                "loss_to_lease_floor": [0.005, 0.005],
                "concessions_year1": [0.012, 0.018],
                "concessions_stabilized": [0.010, 0.010],
                "collection_loss": [0.004, 0.006],
                "exit_cap_rate": [0.0575, 0.0575],
                "insurance_per_unit": [540, 580],
                "r_and_m_per_unit": [750, 850],
                "replacement_reserve_per_unit": [250, 250],
                "recurring_capex_per_unit": [300, 300],
                "immediate_capex": [0, 0],
            },
            "opex_growth": {
                "Payroll": [0.035, 0.035],
                "Repairs & Maintenance": [0.03, 0.03],
                "Turnover / Make-Ready": [0.03, 0.03],
                "Landscaping / Grounds": [0.03, 0.03],
                "Pest Control": [0.03, 0.03],
                "Utilities": [0.035, 0.035],
                "Office Supplies": [0.025, 0.03],
                "Telephone": [0.025, 0.03],
                "Marketing / Advertising": [0.025, 0.025],
                "Insurance": [0.05, 0.05],
                "Real Estate Taxes": [0.03, 0.03],
                "Property Management Fee": [0.0, 0.0],
            },
            "results": {
                "unlevered_irr": [0.04, 0.09],
                "levered_irr": [0.03, 0.14],
                "minimum_dscr": [1.10, 2.0],
                "going_in_cap_rate": [0.045, 0.058],
            },
            "guardrail_warnings": ["insurance_per_unit"],
            "guardrail_waivers": [],
        },
    ),
    DealSpec(
        slug="c_value_add_1970s_reno",
        property_id="SYN-C-CORVANE",
        property_name="Corvane Gardens (synthetic)",
        summary="Value-add 1974 garden deal, 150 units, market data and an interior renovation program.",
        year_built=1974,
        deal_type="value_add",
        msa={"name": "Corvane Synthetic MSA", "metro_slug": "corvane_sx", "cbsa_code": "99903"},
        submarket="Corvane East",
        unit_types=(
            UnitType("1BR/1BA", 1, 1, 640, 70, 1175, 6),
            UnitType("2BR/1BA", 2, 1, 860, 56, 1375, 4),
            UnitType("2BR/2BA", 2, 2, 940, 24, 1525, 2),
        ),
        ltl_pattern=(0.04, 0.06, 0.08, 0.05, 0.07, 0.06, 0.03, 0.09),
        vacancy_loss=0.075,
        concessions=0.010,
        bad_debt=0.016,
        bad_debt_label="Bad Debt",
        other_income=(
            Line("Application Fees", 3),
            Line("Late Fees", 8),
            Line("Pet Rent", 8),
            Line("Laundry Income", 8),
        ),
        reimbursements=(
            Line("RUBS - Water", 45),
            Line("RUBS - Sewer", 15),
            Line("Trash Billback", 8),
        ),
        expenses=(
            _payroll(
                Line("Payroll - Office", 380, "Payroll"),
                Line("Payroll - Leasing", 220, "Payroll"),
                Line("Payroll - Maintenance", 550, "Payroll"),
            ),
            Section("Repairs & Maintenance", (
                Line("Maintenance Supplies", 520, "Repairs & Maintenance"),
                Line("Plumbing Repairs", 300, "Repairs & Maintenance"),
                Line("HVAC Repairs", 220, "Repairs & Maintenance"),
                Line("Electrical Repairs/Supplies", 160, "Repairs & Maintenance"),
                Line("Water Extraction", 90, "Repairs & Maintenance"),
                Line("Appliance Repairs", 60, "Repairs & Maintenance"),
            )),
            Section("Turnover", (
                Line("Turnover - Paint", 150, "Turnover / Make-Ready"),
                Line("Turnover - Cleaning", 100, "Turnover / Make-Ready"),
                Line("Turnover - Carpet Replacement", 80, "Turnover / Make-Ready"),
            )),
            Section("Contract Services", (
                Line("Landscaping", 140, "Landscaping / Grounds"),
                Line("Pest Control", 50, "Pest Control"),
            )),
            Section("Utilities", (
                Line("Utilities - Electric", 220, "Utilities", UTILITY),
                Line("Utilities - Water", 380, "Utilities", UTILITY),
                Line("Utilities - Gas", 120, "Utilities", UTILITY),
                Line("Utilities - Trash", 130, "Utilities"),
            )),
            Section("Administrative", (
                Line("Office Supplies", 90, "Office Supplies"),
                Line("Telephone", 70, "Telephone"),
            )),
            Section("Marketing", (Line("Marketing - Internet Listing", 100, "Marketing / Advertising"),)),
            Section("Insurance", (Line("Property Insurance", 680, "Insurance"),)),
        ),
        management_fee_pct=0.035,
        going_in_cap=0.060,
        millage_mills=22.0,
        assessment_ratio=0.60,
        prior_tax_factor=0.92,
        loan={"ltv": 0.65, "loan_rate": 0.0625, "amort_years": 30, "io_months": 36},
        exit={"exit_month": "2031-10", "sale_cost_pct": 0.02, "exit_cap_rate": None},
        market={
            "confidence": "high",
            "observations": [
                {"metric": "rent_growth_forecast", "level": "submarket", "value": {
                    "2026Q4": 0.020, "2027Q1": 0.022, "2027Q2": 0.025, "2027Q3": 0.028, "2027Q4": 0.030,
                    "2028Q1": 0.031, "2028Q2": 0.032, "2028Q3": 0.033, "2028Q4": 0.034}},
                {"metric": "rent_growth_forecast", "level": "msa", "value": {
                    "2026Q4": 0.017, "2027Q1": 0.019, "2027Q2": 0.022, "2027Q3": 0.025, "2027Q4": 0.027,
                    "2028Q1": 0.028, "2028Q2": 0.029, "2028Q3": 0.030, "2028Q4": 0.031}},
                {"metric": "rent_growth_long_run", "level": "msa", "value": 0.027},
                {"metric": "vacancy_stabilized", "level": "submarket", "value": 0.065},
                {"metric": "vacancy_stabilized", "level": "msa", "value": 0.068},
                {"metric": "concession_rate", "level": "submarket", "value": 0.012},
                {"metric": "market_cap_rate", "level": "submarket", "value": {
                    "2026Q4": 0.0590, "2027Q4": 0.0595, "2028Q4": 0.0598, "2029Q4": 0.0600,
                    "2030Q4": 0.0600, "2031Q1": 0.0598, "2031Q2": 0.0600, "2031Q3": 0.0602, "2031Q4": 0.0604}},
                {"metric": "renovation_premium", "level": "submarket", "value": 175.0},
            ],
        },
        renovation_programs=[
            {
                "program_id": "interior_1br", "program_name": "1BR interior renovation",
                "target_cohort": "1br1ba", "output_cohort": "1br1ba_reno",
                "renovation_cost_per_unit": 11500, "rent_premium_monthly": "market:renovation_premium",
                "downtime_days": 30, "strategy": "proactive", "start_month": "2027-01",
                "monthly_pace": 3, "max_units": 50,
            },
            {
                "program_id": "interior_2br", "program_name": "2BR/1BA interior renovation",
                "target_cohort": "2br1ba", "output_cohort": "2br1ba_reno",
                "renovation_cost_per_unit": 13500, "rent_premium_monthly": "market:renovation_premium",
                "downtime_days": 30, "strategy": "proactive", "start_month": "2027-02",
                "monthly_pace": 2, "max_units": 40,
            },
        ],
        expected={
            "sources": {
                "rent_growth": "market:synthetic",
                "physical_vacancy": "market:synthetic",
                "loss_to_lease": "house:vintage_1970s",
                "concessions": "deal:t12",
                "collection_loss": "deal:t12",
                "exit_cap_rate": "house:exit_cap_spread",
                "insurance_per_unit": "deal:t12",
                "r_and_m_per_unit": "deal:t12",
                "replacement_reserve_per_unit": "house:replacement_reserve",
                "immediate_capex": "house:deferred_capex",
                "recurring_capex_per_unit": "house:recurring_capex_vintage",
                "recurring_capex_growth": "house:opex_growth_table",
                "purchase_closing_cost_pct": "house:backsolve_policy",
                "renovation_premium": "analyst",
                "turn_cost_per_unit": "house:vintage_1970s",
                "sale_cost_percent": "analyst",
            },
            "bands": {
                "rent_growth": [[0.028, 0.032], [0.032, 0.036], [0.029, 0.032], [0.026, 0.028]],
                "vacancy_start": [0.070, 0.090],
                "vacancy_stabilized": [0.060, 0.070],
                "loss_to_lease_start": [0.050, 0.070],
                "loss_to_lease_floor": [0.020, 0.020],
                "concessions_year1": [0.008, 0.012],
                "concessions_stabilized": [0.010, 0.014],
                "collection_loss": [0.015, 0.021],
                "exit_cap_rate": [0.0682, 0.0688],
                "insurance_per_unit": [680, 760],
                "r_and_m_per_unit": [1630, 1730],
                "replacement_reserve_per_unit": [250, 250],
                "recurring_capex_per_unit": [2125, 2125],
                "immediate_capex": [0, 0],
            },
            "opex_growth": {
                "Payroll": [0.035, 0.035],
                "Repairs & Maintenance": [0.03, 0.03],
                "Turnover / Make-Ready": [0.03, 0.03],
                "Landscaping / Grounds": [0.03, 0.03],
                "Pest Control": [0.03, 0.03],
                "Utilities": [0.035, 0.035],
                "Office Supplies": [0.025, 0.03],
                "Telephone": [0.025, 0.03],
                "Marketing / Advertising": [0.025, 0.025],
                "Insurance": [0.05, 0.05],
                "Real Estate Taxes": [0.03, 0.03],
                "Property Management Fee": [0.0, 0.0],
            },
            "results": {
                # At the asking price, after $2,125/unit of recurring capex (1970s value-add midpoint).
                "unlevered_irr": [0.03, 0.12],
                "levered_irr": [0.01, 0.20],
                "minimum_dscr": [1.0, 2.0],
                "going_in_cap_rate": [0.050, 0.066],
            },
            "guardrail_warnings": ["physical_vacancy", "concessions"],
            "guardrail_waivers": [],
        },
    ),
    DealSpec(
        slug="d_value_add_1980s_msa_only",
        property_id="SYN-D-BRINDLEWOOD",
        property_name="Brindlewood Row (synthetic)",
        summary=(
            "Value-add 1986 deal, 90 units, MSA-only market data (the submarket has none); the T12 has "
            "GL collisions, other income and a repair line with a utility word."
        ),
        year_built=1986,
        deal_type="value_add",
        msa={"name": "Brindlewood Synthetic MSA", "metro_slug": "brindlewood_sx"},
        submarket="Brindlewood South",
        unit_types=(
            UnitType("1BR/1BA", 1, 1, 700, 40, 1195, 3),
            UnitType("2BR/2BA", 2, 2, 1010, 42, 1475, 2),
            UnitType("3BR/2BA", 3, 2, 1180, 8, 1700, 1),
        ),
        ltl_pattern=(0.03, 0.05, 0.04, 0.06, 0.045, 0.035),
        vacancy_loss=0.07,
        concessions=None,
        bad_debt=0.009,
        bad_debt_label="Bad Debt Write-Offs",
        other_income=(
            Line("Pet Fees", 7),
            Line("Parking Income", 10),
            Line("Late Fees", 6),
            Line("Application Fees", 3),
        ),
        reimbursements=(
            Line("RUBS - Water/Sewer", 40),
            Line("Trash Billback", 8),
        ),
        expenses=(
            _payroll(
                Line("Payroll - Office", 480, "Payroll"),
                Line("Payroll - Maintenance", 620, "Payroll"),
                Line("Payroll Taxes & Benefits", 200, "Payroll"),
            ),
            Section(
                "Repairs & Maintenance",
                (
                    # Two GL accounts with the same name collide on one category.
                    Line("6110 - Repairs & Maintenance", 600, "Repairs & Maintenance"),
                    Line("6120 - Repairs & Maintenance", 260, "Repairs & Maintenance"),
                    Line("Plumbing Repairs", 240, "Repairs & Maintenance"),
                    # Repair lines with a utility word stay in R&M.
                    Line("Water Extraction", 120, "Repairs & Maintenance"),
                    Line("Electrical Repairs/Supplies", 160, "Repairs & Maintenance"),
                    Line("Gas Line Repair", 70, "Repairs & Maintenance"),
                ),
                rollup="Repairs & Maintenance",
            ),
            Section("Turnover", (
                Line("Turnover - Paint", 160, "Turnover / Make-Ready"),
                Line("Turnover - Cleaning", 150, "Turnover / Make-Ready"),
            )),
            Section("Contract Services", (
                Line("Landscaping", 190, "Landscaping / Grounds"),
                Line("Pest Control", 50, "Pest Control"),
            )),
            Section("Utilities", (
                Line("Water & Sewer", 480, "Water & Sewer", UTILITY),
                Line("Electric - Common Area", 210, "Electricity", UTILITY),
                Line("Gas - Common Area", 90, "Fuel (Gas & Oil)", UTILITY),
                Line("Trash Removal", 120, "Trash"),
            )),
            Section("Administrative", (
                Line("Office Supplies", 90, "Office Supplies"),
                Line("Telephone", 60, "Telephone"),
                Line("Legal & Professional", 50, "Legal & Professional"),
            )),
            Section("Marketing", (Line("Advertising", 130, "Marketing / Advertising"),)),
            Section("Insurance", (
                Line("Property Insurance", 520, "Insurance"),
                Line("General Liability Insurance", 120, "Insurance"),
            )),
        ),
        management_fee_pct=0.035,
        going_in_cap=0.0625,
        millage_mills=17.0,
        assessment_ratio=0.75,
        prior_tax_factor=0.94,
        loan={"ltv": 0.62, "loan_rate": 0.0600, "amort_years": 30, "io_months": 24},
        exit={"exit_month": "2031-10", "sale_cost_pct": 0.02, "exit_cap_rate": None},
        market={
            "confidence": "medium",
            "observations": [
                {"metric": "rent_growth_forecast", "level": "msa", "value": {
                    "2026Q4": 0.016, "2027Q1": 0.018, "2027Q2": 0.020, "2027Q3": 0.022, "2027Q4": 0.024,
                    "2028Q1": 0.026, "2028Q2": 0.027, "2028Q3": 0.028, "2028Q4": 0.029}},
                {"metric": "vacancy_stabilized", "level": "msa", "value": 0.060},
                {"metric": "concession_rate", "level": "msa", "value": 0.015},
                {"metric": "market_cap_rate", "level": "msa", "value": {
                    "2026Q4": 0.0600, "2027Q4": 0.0605, "2028Q4": 0.0610, "2029Q4": 0.0612,
                    "2030Q4": 0.0614, "2031Q1": 0.0613, "2031Q2": 0.0615, "2031Q3": 0.0616, "2031Q4": 0.0616}},
            ],
        },
        # A synthetic PCA: recurring capex below the value-add vintage midpoint (the
        # floor is waived and disclosed) and immediate needs funded at close.
        pca={"immediate_capex": 180000.0, "recurring_capex_per_unit": 1500.0, "as_of": "2026-08-15",
             "reference": "Synthetic PCA (deal d)"},
        expected={
            "sources": {
                "rent_growth": "market:synthetic",
                "physical_vacancy": "market:synthetic",
                "loss_to_lease": "house:vintage_1980s",
                "concessions": "market:synthetic",
                "collection_loss": "deal:t12",
                "exit_cap_rate": "house:exit_cap_spread",
                "insurance_per_unit": "deal:t12",
                "r_and_m_per_unit": "deal:t12",
                "replacement_reserve_per_unit": "house:replacement_reserve",
                "immediate_capex": "deal:pca",
                "recurring_capex_per_unit": "deal:pca",
                "recurring_capex_growth": "house:opex_growth_table",
                "purchase_closing_cost_pct": "house:backsolve_policy",
                "renovation_premium": "engine_fallback",
                "turn_cost_per_unit": "house:vintage_1980s",
                "sale_cost_percent": "analyst",
            },
            "bands": {
                "rent_growth": [[0.022, 0.026], [0.027, 0.031], [0.026, 0.028], [0.025, 0.025]],
                "vacancy_start": [0.060, 0.075],
                "vacancy_stabilized": [0.060, 0.060],
                "loss_to_lease_start": [0.035, 0.055],
                "loss_to_lease_floor": [0.0125, 0.0125],
                "concessions_year1": [0.015, 0.015],
                "concessions_stabilized": [0.015, 0.015],
                "collection_loss": [0.007, 0.011],
                "exit_cap_rate": [0.0690, 0.0696],
                "insurance_per_unit": [600, 680],
                "r_and_m_per_unit": [1710, 1810],
                "replacement_reserve_per_unit": [250, 250],
                "recurring_capex_per_unit": [1500, 1500],
                "immediate_capex": [180000, 180000],
            },
            "opex_growth": {
                "Payroll": [0.035, 0.035],
                "Repairs & Maintenance": [0.03, 0.03],
                "Turnover / Make-Ready": [0.03, 0.03],
                "Landscaping / Grounds": [0.03, 0.03],
                "Pest Control": [0.03, 0.03],
                "Water & Sewer": [0.04, 0.04],
                "Electricity": [0.03, 0.03],
                "Fuel (Gas & Oil)": [0.03, 0.03],
                "Trash": [0.035, 0.035],
                "Office Supplies": [0.025, 0.03],
                "Telephone": [0.025, 0.03],
                "Legal & Professional": [0.025, 0.03],
                "Marketing / Advertising": [0.025, 0.025],
                "Insurance": [0.05, 0.05],
                "Real Estate Taxes": [0.03, 0.03],
                "Property Management Fee": [0.0, 0.0],
            },
            "results": {
                "unlevered_irr": [0.05, 0.12],
                "levered_irr": [0.04, 0.20],
                "minimum_dscr": [1.05, 2.0],
                "going_in_cap_rate": [0.052, 0.068],
            },
            # The PCA recurring capex ($1,500) is below the value-add band midpoint
            # ($1,750): warned in underwrite, raised to the band in pricing. The
            # 6.0% stabilized vacancy is below the rent roll's 6 of 90 vacant
            # (6.67%), the property-level vacancy anchor (UPL-107).
            "guardrail_warnings": ["collection_loss", "recurring_capex_per_unit", "physical_vacancy"],
            "guardrail_waivers": [],
        },
    ),
)


# ── money helpers (integer cents) ────────────────────────────────────────


def _cents(value: float | Decimal) -> int:
    return int(Decimal(str(value)).scaleb(2).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _spread(annual_cents: int, weights: tuple[float, ...]) -> list[int]:
    """Twelve monthly amounts that sum exactly to ``annual_cents``."""
    months = [
        int((Decimal(annual_cents) * Decimal(str(w)) / Decimal(12)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        for w in weights[:11]
    ]
    months.append(annual_cents - sum(months))
    return months


def _money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


# ── rent roll ────────────────────────────────────────────────────────────


def _vacant_positions(count: int, vacant: int) -> set[int]:
    return {int((k + 0.5) * count / vacant) for k in range(vacant)} if vacant else set()


def rent_roll_rows(spec: DealSpec) -> list[list[str]]:
    rows = [["Unit", "Type", "Sqft", "Beds", "Baths", "Status", "Monthly Rent", "Market Rent", "Lease End"]]
    unit_no, occupied_index = 0, 0
    for unit_type in spec.unit_types:
        vacant = _vacant_positions(unit_type.count, unit_type.vacant)
        for j in range(unit_type.count):
            unit_no += 1
            building, number = divmod(unit_no - 1, 24)
            unit = f"{chr(ord('A') + building)}{100 + number + 1}"
            if j in vacant:
                rows.append([unit, unit_type.label, str(unit_type.sqft), str(unit_type.beds),
                             str(unit_type.baths), "Vacant", "0", str(unit_type.market_rent), ""])
                continue
            discount = Decimal(str(spec.ltl_pattern[occupied_index % len(spec.ltl_pattern)]))
            rent = (Decimal(unit_type.market_rent) * (1 - discount)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            # Leases roll over the next twelve months.
            month = (occupied_index * 5) % 12
            year, month_no = (2026, 11 + month) if month < 2 else (2027, month - 1)
            rows.append([unit, unit_type.label, str(unit_type.sqft), str(unit_type.beds), str(unit_type.baths),
                         "Occupied", str(rent), str(unit_type.market_rent), f"{year}-{month_no:02d}-28"])
            occupied_index += 1
    return rows


def _units(spec: DealSpec) -> int:
    return sum(t.count for t in spec.unit_types)


def _monthly_market_gpr_cents(spec: DealSpec) -> int:
    return sum(t.count * t.market_rent for t in spec.unit_types) * 100


def rent_roll_ltl(spec: DealSpec, rows: list[list[str]]) -> Decimal:
    """Unit-weighted loss to lease as ingest measures it: cohort average in-place vs market."""
    by_type: dict[str, list[int]] = {}
    for row in rows[1:]:
        if row[5] == "Occupied":
            by_type.setdefault(row[1], []).append(int(row[6]))
    gap = market = Decimal(0)
    for t in spec.unit_types:
        rents = by_type[t.label]
        avg = Decimal(sum(rents)) / Decimal(len(rents))
        market += Decimal(t.count * t.market_rent)
        gap += Decimal(t.count) * max(Decimal(0), Decimal(t.market_rent) - avg)
    return gap / market


# ── T12 ──────────────────────────────────────────────────────────────────


@dataclass
class T12:
    rows: list[list[str]]
    income_cents: int
    opex_cents: int
    gpr_cents: int
    categories: dict[str, int]
    purchase_price: int
    tax_cents: int


def _row(label: str, months: Optional[list[int]] = None) -> list[str]:
    if months is None:
        return [label] + [""] * 13
    return [label] + [_money(m) for m in months] + [_money(sum(months))]


def build_t12(spec: DealSpec, roll: list[list[str]]) -> T12:
    units = _units(spec)
    current = _monthly_market_gpr_cents(spec)
    gpr = [
        int((Decimal(current) * (GPR_RAMP_START + (1 - GPR_RAMP_START) * Decimal(i) / Decimal(11)))
            .quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        for i in range(12)
    ]

    def pct_of_gpr(rate: float) -> list[int]:
        return [-int((Decimal(g) * Decimal(str(rate))).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) for g in gpr]

    rows = [["Account Name", *T12_MONTHS, "Total"], _row("INCOME"), _row("Rental Income")]
    income: list[list[int]] = []

    def add(label: str, months: list[int], bucket: Optional[list[list[int]]] = None) -> None:
        rows.append(_row(label, months))
        if bucket is not None:
            bucket.append(months)

    add("Gross Potential Rent", gpr, income)
    add("Loss to Lease", pct_of_gpr(float(rent_roll_ltl(spec, roll))), income)
    add("Vacancy Loss", pct_of_gpr(spec.vacancy_loss), income)
    if spec.concessions is not None:
        add("Concessions", pct_of_gpr(spec.concessions), income)
    add(spec.bad_debt_label, pct_of_gpr(spec.bad_debt), income)
    for header, lines in (("Other Income", spec.other_income), ("Reimbursable Income", spec.reimbursements)):
        rows.append(_row(header))
        for line in lines:
            add(line.label, _spread(_cents(line.per_unit * units * 12), line.weights), income)
    income_months = [sum(m[i] for m in income) for i in range(12)]
    rows.append(_row("Total Income", income_months))
    egi = sum(income_months)

    rows.append(_row("EXPENSES"))
    expense: list[list[int]] = []
    categories: dict[str, int] = {}
    for section in spec.expenses:
        rows.append(_row(section.header))
        section_months: list[list[int]] = []
        for line in section.lines:
            months = _spread(_cents(line.per_unit * units), line.weights)
            add(line.label, months, expense)
            section_months.append(months)
            categories[line.category] = categories.get(line.category, 0) + sum(months)
        if section.rollup:
            rows.append(_row(section.rollup, [sum(m[i] for m in section_months) for i in range(12)]))
    opex_ex_tax_mgmt = sum(sum(m) for m in expense)
    management = _cents(Decimal(egi) / 100 * Decimal(str(spec.management_fee_pct)))
    tax_rate = Decimal(str(spec.millage_mills)) / 1000 * Decimal(str(spec.assessment_ratio))
    noi_before_tax = Decimal(egi - opex_ex_tax_mgmt - management) / 100
    # Price at the going-in cap on the reassessed tax: NOI_before_tax - price * t = cap * price.
    price = int((noi_before_tax / (Decimal(str(spec.going_in_cap)) + tax_rate) / 10000)
                .quantize(Decimal("1"), rounding=ROUND_HALF_UP)) * 10000
    tax = _cents(Decimal(price) * tax_rate * Decimal(str(spec.prior_tax_factor)))
    for header, label, annual, category in (
        ("Taxes", "Real Estate Taxes", tax, "Real Estate Taxes"),
        ("Management Fees", "Management Fee", management, "Property Management Fee"),
    ):
        rows.append(_row(header))
        months = _spread(annual, FLAT)
        add(label, months, expense)
        categories[category] = categories.get(category, 0) + sum(months)
    expense_months = [sum(m[i] for m in expense) for i in range(12)]
    rows.append(_row("Total Expenses", expense_months))
    rows.append(_row("Net Operating Income", [income_months[i] - expense_months[i] for i in range(12)]))
    return T12(rows, egi, sum(expense_months), sum(gpr), categories, price, tax)


# ── deal.json ────────────────────────────────────────────────────────────


def _market_premium(spec: DealSpec) -> Optional[float]:
    for obs in (spec.market or {}).get("observations", []):
        if obs["metric"] == "renovation_premium":
            return float(obs["value"])
    return None


def deal_document(spec: DealSpec, roll: list[list[str]], t12: T12) -> dict[str, Any]:
    units = _units(spec)
    premium = _market_premium(spec)
    programs = []
    for program in spec.renovation_programs:
        program = dict(program)
        if program["rent_premium_monthly"] == "market:renovation_premium":
            program["rent_premium_monthly"] = premium
        programs.append(program)
    analyst_inputs: dict[str, Any] = {
        "metadata": {"property_summary": {"property_tax_policy": {
            "millage_rate_mills": spec.millage_mills,
            "assessment_ratio": spec.assessment_ratio,
            "source": "synthetic_tax_bill",
            "source_locator": f"tests/fixtures/deals/{spec.slug}/deal.json",
            # A ratio other than 1.00 is an analyst assessment override (engine/property_tax.py).
            "analyst_override": spec.assessment_ratio != 1.0,
        }}},
    }
    if programs:
        analyst_inputs["renovation_programs"] = programs
    if spec.pca:
        analyst_inputs["capital_plan"] = {"pca": dict(spec.pca)}
    market = None
    if spec.market is not None:
        market = {"provider": PROVIDER, "as_of": MARKET_AS_OF, **spec.market}
    gpr_rent_roll = _monthly_market_gpr_cents(spec) * 12
    return {
        "slug": spec.slug,
        "property_id": spec.property_id,
        "property_name": spec.property_name,
        "summary": spec.summary,
        "synthetic": True,
        "documents": {"rent_roll": "rent_roll.csv", "t12": "t12.csv"},
        "analysis": {"start": ANALYSIS_START, "end": ANALYSIS_END},
        "deal_profile": {
            "deal_type": spec.deal_type,
            "year_built": spec.year_built,
            "msa": spec.msa,
            "metro_slug": spec.msa["metro_slug"],
            "submarket": spec.submarket,
        },
        "economics": {
            "purchase_price": t12.purchase_price,
            **spec.loan,
            **spec.exit,
        },
        "analyst_inputs": analyst_inputs,
        "market_data": market,
        "expected": {
            "documents": {
                "units": units,
                "rent_roll_physical_vacancy": round(sum(t.vacant for t in spec.unit_types) / units, 6),
                "rent_roll_loss_to_lease": float(rent_roll_ltl(spec, roll).quantize(Decimal("0.000001"))),
                "rent_roll_gpr_annual": gpr_rent_roll / 100,
                "t12_gpr_annual": t12.gpr_cents / 100,
                "t12_total_income": t12.income_cents / 100,
                "t12_total_opex": t12.opex_cents / 100,
                "t12_opex_to_egi": round(t12.opex_cents / t12.income_cents, 6),
                "t12_opex_by_category": {k: v / 100 for k, v in t12.categories.items()},
            },
            "ingest_gate": "PASS",
            **spec.expected,
        },
    }


# ── rendering ────────────────────────────────────────────────────────────


def _csv_text(rows: list[list[str]]) -> str:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerows(rows)
    return buffer.getvalue()


def render_all() -> dict[Path, str]:
    """Every generated fixture file (path -> text). Pure: same output every call."""
    out: dict[Path, str] = {}
    for spec in DEALS:
        roll = rent_roll_rows(spec)
        t12 = build_t12(spec, roll)
        folder = HERE / spec.slug
        out[folder / "rent_roll.csv"] = _csv_text(roll)
        out[folder / "t12.csv"] = _csv_text(t12.rows)
        out[folder / "deal.json"] = json.dumps(deal_document(spec, roll, t12), indent=2) + "\n"
    return out


def write_goldens() -> list[Path]:
    """Re-run every deal through ingest and rewrite its expected_assumptions.json."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from tests import deal_library

    written = []
    for spec in DEALS:
        deal = deal_library.load_deal(spec.slug)
        canonical = deal_library.ingest(deal)
        path = deal_library.golden_path(spec.slug)
        path.write_text(deal_library.golden_text(canonical), encoding="utf-8")
        written.append(path)
    return written


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="Fail if a committed fixture differs; write nothing.")
    parser.add_argument("--update-goldens", action="store_true",
                        help="Also rewrite each deal's expected_assumptions.json from a fresh ingest.")
    args = parser.parse_args(argv)
    rendered = render_all()
    if args.check:
        stale = [p for p, text in rendered.items() if not p.exists() or p.read_text(encoding="utf-8") != text]
        for path in stale:
            print(f"stale: {path.relative_to(REPO_ROOT)}")
        return 1 if stale else 0
    for path, text in rendered.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path.relative_to(REPO_ROOT)}")
    if args.update_goldens:
        for path in write_goldens():
            print(f"wrote {path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
