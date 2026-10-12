from __future__ import annotations

from decimal import Decimal
from typing import Optional


# Cap-rate sanity band (inclusive): going-in AND exit cap must both fall
# within this range or the feasibility classifier emits a HARD flag.
# Both engine/feasibility.py and engine/validator.py import this constant
# so the two check sites cannot drift apart.
CAP_RATE_SANITY_BAND: tuple = (0.06, 0.12)

PRIMARY_COC_TARGET_PCT = Decimal("0.08")
PRIMARY_IRR_TARGET_LOW_PCT = Decimal("0.10")
PRIMARY_IRR_TARGET_HIGH_PCT = Decimal("0.13")

DEFAULT_AGENCY_SPREAD_PCT = Decimal("0.0150")

# Saved-down analyst defaults used when a live deal lacks an explicit fee budget.
DEFAULT_PURCHASE_CLOSING_COST_PCT = Decimal("0.015")
DEFAULT_PARTNERSHIP_CLOSING_COSTS = Decimal("50000")
DEFAULT_ACQUISITION_FEE_PCT = Decimal("0.01")
DEFAULT_ASSET_MANAGEMENT_FEE_PCT = Decimal("0.015")
DEFAULT_DISPOSITION_FEE_PCT = Decimal("0.01")
DEFAULT_ANNUAL_PARTNERSHIP_EXPENSES = Decimal("25000")
DEFAULT_YEAR1_INSURANCE_PER_UNIT = Decimal("600")
HIGH_INSURANCE_REVIEW_THRESHOLD_PER_UNIT = Decimal("900")
DEFAULT_PROPERTY_TAX_REASSESSMENT_RATIO = Decimal("1.00")

# ── Opex growth by category (ADR 0003 §6) ────────────────────────────────
# HOUSE ASSUMPTIONS TO BE REVIEWED, NOT MARKET DATA. Long-run nominal annual
# growth per canonical T12 opex category (the names engine/ingest/t12_parser.py
# emits). They are set at or above long-run CPI on purpose: overstating
# expense growth is the conservative error. The assumption resolver
# (engine/assumptions/opex.py) writes a rate into an ingested opex_table row
# only when the row carries no growth_rate; an analyst rate always wins.
# Replace a constant with market data once a provider supplies an opex trend.
#
# Wage growth: on-site payroll, benefits and payroll taxes.
OPEX_GROWTH_PAYROLL = Decimal("0.035")
# Labor and materials for repairs, turns and contracted site services.
OPEX_GROWTH_REPAIRS_MAINTENANCE = Decimal("0.030")
OPEX_GROWTH_TURNOVER_MAKE_READY = Decimal("0.030")
OPEX_GROWTH_CONTRACT_SERVICES = Decimal("0.030")
OPEX_GROWTH_LANDSCAPING_GROUNDS = Decimal("0.030")
OPEX_GROWTH_SECURITY = Decimal("0.030")
# Discretionary, largely fixed-price lines.
OPEX_GROWTH_MARKETING_ADVERTISING = Decimal("0.025")
OPEX_GROWTH_ADMINISTRATIVE = Decimal("0.025")
# Utilities: a blended rate for a combined line, and per sub-category. Municipal
# water/sewer and hauling contracts have run above CPI.
OPEX_GROWTH_UTILITIES = Decimal("0.035")
OPEX_GROWTH_WATER_SEWER = Decimal("0.040")
OPEX_GROWTH_ELECTRICITY = Decimal("0.030")
OPEX_GROWTH_FUEL_GAS_OIL = Decimal("0.030")
OPEX_GROWTH_TRASH = Decimal("0.035")
# Property insurance: a house trend above CPI for a hardening market.
OPEX_GROWTH_INSURANCE = Decimal("0.050")
OPEX_GROWTH_OTHER_OPERATING = Decimal("0.030")
OPEX_GROWTH_FRANCHISE_FEE = Decimal("0.030")
# Reimbursement credits booked inside opex are held flat: growing an offset
# would understate net expense.
OPEX_GROWTH_REIMBURSEMENTS = Decimal("0")
# A management fee entered in dollars tracks revenue; a fee entered as a
# percent of revenue (percent_egr / percent_rent) already grows with revenue,
# so any percent-of-revenue row gets no growth (the engine would otherwise
# compound the percentage itself).
OPEX_GROWTH_PROPERTY_MANAGEMENT_FEE_FIXED = Decimal("0.030")
OPEX_GROWTH_PERCENT_OF_REVENUE = Decimal("0")
# Any category the table does not name.
OPEX_GROWTH_DEFAULT = Decimal("0.030")
# Real estate taxes: engine/property_tax.py resets the year-1 tax to purchase
# price x assessment ratio x millage and keeps the row's own growth_rate, which
# the engine applies from analysis year 2 (the rent-growth convention). After
# the purchase reassessment the house grows taxes at a flat 3% a year (millage
# creep and annual reassessment of a rising value). The resolver writes it
# once into the tax row; property_tax.py never compounds it, so it is applied
# exactly once, on top of the reassessed year-1 amount.
HOUSE_RE_TAX_GROWTH_AFTER_REASSESSMENT = Decimal("0.03")
OPEX_GROWTH_REAL_ESTATE_TAXES: Optional[Decimal] = HOUSE_RE_TAX_GROWTH_AFTER_REASSESSMENT

# Management fee for an ingested deal whose T12 carries none (percent of EGR).
DEFAULT_MANAGEMENT_FEE_PCT_EGR = Decimal("0.030")

# Canonical T12 category -> (constant name, rate).
OPEX_GROWTH_TABLE: dict[str, tuple[str, Optional[Decimal]]] = {
    "Payroll": ("OPEX_GROWTH_PAYROLL", OPEX_GROWTH_PAYROLL),
    "Repairs & Maintenance": ("OPEX_GROWTH_REPAIRS_MAINTENANCE", OPEX_GROWTH_REPAIRS_MAINTENANCE),
    "Turnover / Make-Ready": ("OPEX_GROWTH_TURNOVER_MAKE_READY", OPEX_GROWTH_TURNOVER_MAKE_READY),
    "Contract Services": ("OPEX_GROWTH_CONTRACT_SERVICES", OPEX_GROWTH_CONTRACT_SERVICES),
    "Landscaping / Grounds": ("OPEX_GROWTH_LANDSCAPING_GROUNDS", OPEX_GROWTH_LANDSCAPING_GROUNDS),
    "Security": ("OPEX_GROWTH_SECURITY", OPEX_GROWTH_SECURITY),
    "Marketing / Advertising": ("OPEX_GROWTH_MARKETING_ADVERTISING", OPEX_GROWTH_MARKETING_ADVERTISING),
    "Administrative": ("OPEX_GROWTH_ADMINISTRATIVE", OPEX_GROWTH_ADMINISTRATIVE),
    "Utilities": ("OPEX_GROWTH_UTILITIES", OPEX_GROWTH_UTILITIES),
    "Water & Sewer": ("OPEX_GROWTH_WATER_SEWER", OPEX_GROWTH_WATER_SEWER),
    "Electricity": ("OPEX_GROWTH_ELECTRICITY", OPEX_GROWTH_ELECTRICITY),
    "Fuel (Gas & Oil)": ("OPEX_GROWTH_FUEL_GAS_OIL", OPEX_GROWTH_FUEL_GAS_OIL),
    "Trash": ("OPEX_GROWTH_TRASH", OPEX_GROWTH_TRASH),
    "Insurance": ("OPEX_GROWTH_INSURANCE", OPEX_GROWTH_INSURANCE),
    "Other Operating Expenses": ("OPEX_GROWTH_OTHER_OPERATING", OPEX_GROWTH_OTHER_OPERATING),
    "Franchise Fee": ("OPEX_GROWTH_FRANCHISE_FEE", OPEX_GROWTH_FRANCHISE_FEE),
    "Reimbursements": ("OPEX_GROWTH_REIMBURSEMENTS", OPEX_GROWTH_REIMBURSEMENTS),
    "Property Management Fee": (
        "OPEX_GROWTH_PROPERTY_MANAGEMENT_FEE_FIXED",
        OPEX_GROWTH_PROPERTY_MANAGEMENT_FEE_FIXED,
    ),
    "Real Estate Taxes": ("HOUSE_RE_TAX_GROWTH_AFTER_REASSESSMENT", HOUSE_RE_TAX_GROWTH_AFTER_REASSESSMENT),
}
PERCENT_OF_REVENUE_CALCULATION_TYPES = frozenset({"percent_egr", "percent_rent"})


def default_opex_growth_rate(
    category: Optional[str],
    *,
    calculation_type: str = "fixed_annual",
) -> tuple[str, Optional[Decimal]]:
    """House growth for a canonical opex category: ``(constant name, rate)``.

    Percent-of-revenue rows never grow. An unknown category gets
    ``OPEX_GROWTH_DEFAULT``. Real estate taxes get
    ``HOUSE_RE_TAX_GROWTH_AFTER_REASSESSMENT``, applied from year 2 on top of
    the reassessed year-1 tax (engine/property_tax.py).
    """
    if calculation_type in PERCENT_OF_REVENUE_CALCULATION_TYPES:
        return "OPEX_GROWTH_PERCENT_OF_REVENUE", OPEX_GROWTH_PERCENT_OF_REVENUE
    if category in OPEX_GROWTH_TABLE:
        return OPEX_GROWTH_TABLE[category]
    return "OPEX_GROWTH_DEFAULT", OPEX_GROWTH_DEFAULT

# Institutional vintage framework supplied by the user on 2026-05-08.
# Ranges are in 2026 dollars: annual/unit R&M (on R_AND_M_FLOOR_BASIS),
# economic CapEx (the recurring capex band below NOI; the replacement reserve
# is its floor) and a rough turn-cost band, for use when a deal lacks better property-specific data.
VINTAGE_FRAMEWORK_2026 = {
    "1960s": {
        "year_min": 1960,
        "year_max": 1969,
        "r_and_m_low": Decimal("1400"),
        "r_and_m_high": Decimal("2400"),
        "economic_capex_low": Decimal("1500"),
        "economic_capex_high": Decimal("3500"),
        "turn_low": Decimal("2500"),
        "turn_high": Decimal("6500"),
        "ltl_floor": Decimal("0.020"),
        "vacancy_floor": Decimal("0.080"),
        "collection_loss_floor": Decimal("0.015"),
        "year1_concessions_floor": Decimal("0.010"),
        "physical_vacancy_typical_low": Decimal("0.050"),
        "physical_vacancy_typical_high": Decimal("0.080"),
        "physical_vacancy_stress_low": Decimal("0.080"),
        "physical_vacancy_stress_high": Decimal("0.120"),
        "economic_vacancy_typical_low": Decimal("0.100"),
        "economic_vacancy_typical_high": Decimal("0.180"),
        "economic_vacancy_stress_low": Decimal("0.180"),
        "economic_vacancy_stress_high": Decimal("0.250"),
        "bad_debt_typical_low": Decimal("0.020"),
        "bad_debt_typical_high": Decimal("0.050"),
        "bad_debt_stress_low": Decimal("0.050"),
        "bad_debt_stress_high": Decimal("0.080"),
        "concessions_typical_low": Decimal("0.000"),
        "concessions_typical_high": Decimal("0.030"),
        "concessions_stress_low": Decimal("0.030"),
        "concessions_stress_high": Decimal("0.080"),
        "other_income_low_per_unit_month": Decimal("75"),
        "other_income_high_per_unit_month": Decimal("175"),
        "other_income_strong_low_per_unit_month": Decimal("150"),
        "other_income_strong_high_per_unit_month": Decimal("250"),
    },
    "1970s": {
        "year_min": 1970,
        "year_max": 1979,
        "r_and_m_low": Decimal("1200"),
        "r_and_m_high": Decimal("2000"),
        "economic_capex_low": Decimal("1250"),
        "economic_capex_high": Decimal("3000"),
        "turn_low": Decimal("2000"),
        "turn_high": Decimal("5500"),
        "ltl_floor": Decimal("0.0175"),
        "vacancy_floor": Decimal("0.070"),
        "collection_loss_floor": Decimal("0.0125"),
        "year1_concessions_floor": Decimal("0.010"),
        "physical_vacancy_typical_low": Decimal("0.050"),
        "physical_vacancy_typical_high": Decimal("0.070"),
        "physical_vacancy_stress_low": Decimal("0.080"),
        "physical_vacancy_stress_high": Decimal("0.110"),
        "economic_vacancy_typical_low": Decimal("0.090"),
        "economic_vacancy_typical_high": Decimal("0.160"),
        "economic_vacancy_stress_low": Decimal("0.160"),
        "economic_vacancy_stress_high": Decimal("0.230"),
        "bad_debt_typical_low": Decimal("0.015"),
        "bad_debt_typical_high": Decimal("0.045"),
        "bad_debt_stress_low": Decimal("0.050"),
        "bad_debt_stress_high": Decimal("0.070"),
        "concessions_typical_low": Decimal("0.000"),
        "concessions_typical_high": Decimal("0.040"),
        "concessions_stress_low": Decimal("0.040"),
        "concessions_stress_high": Decimal("0.080"),
        "other_income_low_per_unit_month": Decimal("85"),
        "other_income_high_per_unit_month": Decimal("200"),
        "other_income_strong_low_per_unit_month": Decimal("175"),
        "other_income_strong_high_per_unit_month": Decimal("275"),
    },
    "1980s": {
        "year_min": 1980,
        "year_max": 1989,
        "r_and_m_low": Decimal("900"),
        "r_and_m_high": Decimal("1700"),
        "economic_capex_low": Decimal("1000"),
        "economic_capex_high": Decimal("2500"),
        "turn_low": Decimal("1800"),
        "turn_high": Decimal("4500"),
        "value_add_turn_low": Decimal("4000"),
        "value_add_turn_high": Decimal("7500"),
        "ltl_floor": Decimal("0.010"),
        "vacancy_floor": Decimal("0.050"),
        "collection_loss_floor": Decimal("0.0075"),
        "year1_concessions_floor": Decimal("0.0075"),
        "physical_vacancy_typical_low": Decimal("0.045"),
        "physical_vacancy_typical_high": Decimal("0.070"),
        "physical_vacancy_stress_low": Decimal("0.070"),
        "physical_vacancy_stress_high": Decimal("0.100"),
        "economic_vacancy_typical_low": Decimal("0.080"),
        "economic_vacancy_typical_high": Decimal("0.140"),
        "economic_vacancy_stress_low": Decimal("0.140"),
        "economic_vacancy_stress_high": Decimal("0.200"),
        "bad_debt_typical_low": Decimal("0.010"),
        "bad_debt_typical_high": Decimal("0.035"),
        "bad_debt_stress_low": Decimal("0.040"),
        "bad_debt_stress_high": Decimal("0.060"),
        "concessions_typical_low": Decimal("0.000"),
        "concessions_typical_high": Decimal("0.030"),
        "concessions_stress_low": Decimal("0.030"),
        "concessions_stress_high": Decimal("0.070"),
        "other_income_low_per_unit_month": Decimal("100"),
        "other_income_high_per_unit_month": Decimal("225"),
        "other_income_strong_low_per_unit_month": Decimal("200"),
        "other_income_strong_high_per_unit_month": Decimal("325"),
    },
    "1990s": {
        "year_min": 1990,
        "year_max": 1999,
        "r_and_m_low": Decimal("700"),
        "r_and_m_high": Decimal("1400"),
        "economic_capex_low": Decimal("750"),
        "economic_capex_high": Decimal("2000"),
        "turn_low": Decimal("1500"),
        "turn_high": Decimal("3000"),
        "value_add_turn_low": Decimal("3500"),
        "value_add_turn_high": Decimal("6500"),
        "ltl_floor": Decimal("0.010"),
        "vacancy_floor": Decimal("0.050"),
        "collection_loss_floor": Decimal("0.0050"),
        "year1_concessions_floor": Decimal("0.0050"),
        "physical_vacancy_typical_low": Decimal("0.040"),
        "physical_vacancy_typical_high": Decimal("0.065"),
        "physical_vacancy_stress_low": Decimal("0.070"),
        "physical_vacancy_stress_high": Decimal("0.090"),
        "economic_vacancy_typical_low": Decimal("0.070"),
        "economic_vacancy_typical_high": Decimal("0.120"),
        "economic_vacancy_stress_low": Decimal("0.120"),
        "economic_vacancy_stress_high": Decimal("0.170"),
        "bad_debt_typical_low": Decimal("0.0075"),
        "bad_debt_typical_high": Decimal("0.025"),
        "bad_debt_stress_low": Decimal("0.030"),
        "bad_debt_stress_high": Decimal("0.050"),
        "concessions_typical_low": Decimal("0.000"),
        "concessions_typical_high": Decimal("0.030"),
        "concessions_stress_low": Decimal("0.030"),
        "concessions_stress_high": Decimal("0.060"),
        "other_income_low_per_unit_month": Decimal("125"),
        "other_income_high_per_unit_month": Decimal("250"),
        "other_income_strong_low_per_unit_month": Decimal("225"),
        "other_income_strong_high_per_unit_month": Decimal("350"),
    },
    "2000s": {
        "year_min": 2000,
        "year_max": 2009,
        "r_and_m_low": Decimal("600"),
        "r_and_m_high": Decimal("1200"),
        "economic_capex_low": Decimal("500"),
        "economic_capex_high": Decimal("1500"),
        "turn_low": Decimal("1200"),
        "turn_high": Decimal("2500"),
        "value_add_turn_low": Decimal("3000"),
        "value_add_turn_high": Decimal("5500"),
        "ltl_floor": Decimal("0.0075"),
        "vacancy_floor": Decimal("0.045"),
        "collection_loss_floor": Decimal("0.0050"),
        "year1_concessions_floor": Decimal("0.0035"),
        "physical_vacancy_typical_low": Decimal("0.040"),
        "physical_vacancy_typical_high": Decimal("0.060"),
        "physical_vacancy_stress_low": Decimal("0.065"),
        "physical_vacancy_stress_high": Decimal("0.085"),
        "economic_vacancy_typical_low": Decimal("0.065"),
        "economic_vacancy_typical_high": Decimal("0.110"),
        "economic_vacancy_stress_low": Decimal("0.110"),
        "economic_vacancy_stress_high": Decimal("0.150"),
        "bad_debt_typical_low": Decimal("0.0050"),
        "bad_debt_typical_high": Decimal("0.020"),
        "bad_debt_stress_low": Decimal("0.025"),
        "bad_debt_stress_high": Decimal("0.040"),
        "concessions_typical_low": Decimal("0.000"),
        "concessions_typical_high": Decimal("0.030"),
        "concessions_stress_low": Decimal("0.030"),
        "concessions_stress_high": Decimal("0.070"),
        "other_income_low_per_unit_month": Decimal("150"),
        "other_income_high_per_unit_month": Decimal("300"),
        "other_income_strong_low_per_unit_month": Decimal("250"),
        "other_income_strong_high_per_unit_month": Decimal("400"),
    },
    "2010s": {
        "year_min": 2010,
        "year_max": 2019,
        "r_and_m_low": Decimal("500"),
        "r_and_m_high": Decimal("1000"),
        "economic_capex_low": Decimal("300"),
        "economic_capex_high": Decimal("1000"),
        "turn_low": Decimal("1000"),
        "turn_high": Decimal("2200"),
        "ltl_floor": Decimal("0.0050"),
        "vacancy_floor": Decimal("0.045"),
        "collection_loss_floor": Decimal("0.0040"),
        "year1_concessions_floor": Decimal("0.0025"),
        "physical_vacancy_typical_low": Decimal("0.040"),
        "physical_vacancy_typical_high": Decimal("0.065"),
        "physical_vacancy_stress_low": Decimal("0.070"),
        "physical_vacancy_stress_high": Decimal("0.100"),
        "economic_vacancy_typical_low": Decimal("0.070"),
        "economic_vacancy_typical_high": Decimal("0.130"),
        "economic_vacancy_stress_low": Decimal("0.130"),
        "economic_vacancy_stress_high": Decimal("0.200"),
        "bad_debt_typical_low": Decimal("0.0025"),
        "bad_debt_typical_high": Decimal("0.015"),
        "bad_debt_stress_low": Decimal("0.020"),
        "bad_debt_stress_high": Decimal("0.035"),
        "concessions_typical_low": Decimal("0.010"),
        "concessions_typical_high": Decimal("0.050"),
        "concessions_stress_low": Decimal("0.050"),
        "concessions_stress_high": Decimal("0.120"),
        "other_income_low_per_unit_month": Decimal("200"),
        "other_income_high_per_unit_month": Decimal("400"),
        "other_income_strong_low_per_unit_month": Decimal("350"),
        "other_income_strong_high_per_unit_month": Decimal("550"),
    },
    "2020s": {
        "year_min": 2020,
        "year_max": 2029,
        "r_and_m_low": Decimal("400"),
        "r_and_m_high": Decimal("900"),
        "economic_capex_low": Decimal("200"),
        "economic_capex_high": Decimal("750"),
        "turn_low": Decimal("800"),
        "turn_high": Decimal("2000"),
        "ltl_floor": Decimal("0.0050"),
        "vacancy_floor": Decimal("0.040"),
        "collection_loss_floor": Decimal("0.0035"),
        "year1_concessions_floor": Decimal("0.0025"),
        "physical_vacancy_typical_low": Decimal("0.040"),
        "physical_vacancy_typical_high": Decimal("0.070"),
        "physical_vacancy_stress_low": Decimal("0.080"),
        "physical_vacancy_stress_high": Decimal("0.150"),
        "economic_vacancy_typical_low": Decimal("0.080"),
        "economic_vacancy_typical_high": Decimal("0.150"),
        "economic_vacancy_stress_low": Decimal("0.150"),
        "economic_vacancy_stress_high": Decimal("0.250"),
        "bad_debt_typical_low": Decimal("0.0025"),
        "bad_debt_typical_high": Decimal("0.0125"),
        "bad_debt_stress_low": Decimal("0.015"),
        "bad_debt_stress_high": Decimal("0.030"),
        "concessions_typical_low": Decimal("0.020"),
        "concessions_typical_high": Decimal("0.070"),
        "concessions_stress_low": Decimal("0.080"),
        "concessions_stress_high": Decimal("0.150"),
        "other_income_low_per_unit_month": Decimal("250"),
        "other_income_high_per_unit_month": Decimal("500"),
        "other_income_strong_low_per_unit_month": Decimal("400"),
        "other_income_strong_high_per_unit_month": Decimal("650"),
    },
}


# What the vintage R&M band (``r_and_m_low`` / ``r_and_m_high``, e.g.
# $900-$1,700/unit for the 1980s) is compared with: the R&M floor checks the
# deal's repairs & maintenance lines plus its turnover / make-ready lines,
# annualized. The band excludes payroll, so site payroll (maintenance staff
# wages, benefits, payroll taxes) is never counted toward the floor, even on a
# line labeled as repairs. A T12 that books turns on their own lines is not
# topped up for R&M it already carries. engine/assumptions/canonical_ops
# (``in_r_and_m_floor_basis``) applies this; the guardrail and the resolver's
# ``r_and_m_per_unit`` use the same basis.
R_AND_M_FLOOR_BASIS = (
    "repairs & maintenance plus turnover / make-ready lines, annualized; payroll excluded"
)

# ── R&M floor: the payroll review (ADR 0003 §5) ──────────────────────────
# HOUSE POLICY, NOT MARKET DATA. Payroll is payroll: site payroll earns no
# credit toward the R&M floor by default, even when the whole team's time is
# booked there, because the vintage R&M band is repair and maintenance spend
# (contracts, materials, turns) on top of normal site staffing. A credit needs
# evidence that maintenance labor is booked in payroll beyond normal staffing:
# payroll lines labeled as maintenance labor (canonical_ops
# ``is_maintenance_labor_label``) AND total payroll per unit above this
# benchmark. The credit is the lesser of the labeled maintenance payroll and
# the payroll above the benchmark; the floor test is R&M + turnover + credit.
#
# The benchmark is total site payroll (wages, payroll taxes and benefits) per
# unit a year for a garden community, set at the top of normal staffing:
# - NAA Income/Expense IQ 2024 data (NAA, 2025-12-22): administrative plus
#   payroll costs $2,323/unit/year nationally (all property types).
# - NAA 2021 Survey of Operating Income & Expenses (2020 data): salaries and
#   personnel are 25% of total opex for garden communities; 25% of the 2024
#   national total opex of $8,657/unit is about $2,160/unit.
# - NAA Best Practices, Staffing (2019 survey, 2018 data): the general rule is
#   one office and one maintenance employee per 100 units, and the surveyed
#   average was one employee per 45.3 units, so about half of site payroll is
#   maintenance staff in a normal operation.
# $2,000/unit sits just below the derived garden figure, so payroll above it
# is above normal staffing for a garden asset. No public NAA table gives
# garden payroll per unit in dollars; replace it with market data when a
# provider supplies one.
HOUSE_PAYROLL_BENCHMARK_PER_UNIT = Decimal("2000")


def vintage_bucket(year_built: Optional[int]) -> Optional[str]:
    if year_built is None:
        return None
    for label, row in VINTAGE_FRAMEWORK_2026.items():
        if row["year_min"] <= year_built <= row["year_max"]:
            return label
    if year_built < 1960:
        return "1960s"
    if year_built >= 2030:
        return "2020s"
    return None


# ── Capital: the three capex buckets (ADR 0003 §3, §5) ───────────────────
# HOUSE POLICY, NOT MARKET DATA. Every deal (stabilized and value-add) budgets
# capital in three buckets, and older vintages need more of it:
#
# 1. Immediate / deferred capex at close: one-time capital for deferred
#    maintenance and PCA immediate needs, funded with equity at close (or a
#    lender holdback). It is separate from the value-add renovation budget.
#    Without PCA evidence or an analyst figure nothing is budgeted
#    (``HOUSE_IMMEDIATE_CAPEX_DEFAULT``, flagged for review), unless the deal
#    is flagged with deferred maintenance: then a placeholder of
#    ``HOUSE_DEFERRED_CAPEX_PLACEHOLDER_YEARS`` year(s) of the vintage band high
#    end per unit is budgeted (``default_deferred_capex_placeholder_per_unit``),
#    flagged for review with a recommendation to commission a PCA.
# 2. Recurring capex by vintage: annual $/unit below NOI (not opex), grown
#    every analysis year at ``HOUSE_RECURRING_CAPEX_GROWTH``. The house
#    default is the vintage framework's economic CapEx band
#    (``economic_capex_low`` / ``economic_capex_high``): the low end for a
#    stabilized deal, the midpoint for value-add, the high end for a deal
#    flagged with deferred maintenance (``default_recurring_capex_per_unit``).
#    A PCA (deal evidence) or an analyst figure replaces it; in pricing the
#    band is a floor.
# 3. Lender replacement reserve: $250/unit/year below NOI, the same for every
#    vintage and strategy (desk practice). It is a FLOOR, not an addition:
#    the below-NOI capital charge each month is max(recurring capex, reserve),
#    never both. It is also backsolve's pricing floor for reserves.
HOUSE_REPLACEMENT_RESERVE_PER_UNIT = Decimal("250")
HOUSE_IMMEDIATE_CAPEX_DEFAULT = Decimal("0")
# Deferred maintenance flagged with no PCA and no analyst figure: budget this
# many years of the vintage economic CapEx band high end per unit at close.
# One year of the top of the band is a placeholder, not an estimate: it keeps
# a known capital need from being priced at $0 until a PCA sizes it.
HOUSE_DEFERRED_CAPEX_PLACEHOLDER_YEARS = Decimal("1")
# Agency lenders escrow PCA immediate repairs at 125% of the engineer's
# estimate (Fannie Mae Multifamily Guide 405.02 completion/repair escrow
# guidance; Freddie Mac SBL priority repair escrow guidance, 2024). Disclosed
# with the placeholder so the analyst knows what a lender will hold back.
LENDER_PCA_REPAIR_ESCROW_FACTOR = Decimal("1.25")
# Recurring capex is labor and materials for building systems (roofs, HVAC,
# paving, unit interiors), so it grows with repairs & maintenance.
HOUSE_RECURRING_CAPEX_GROWTH_CONSTANT = "OPEX_GROWTH_REPAIRS_MAINTENANCE"
HOUSE_RECURRING_CAPEX_GROWTH = OPEX_GROWTH_REPAIRS_MAINTENANCE
# A vintage built before this year without PCA evidence is flagged for a
# capex review (validator CAPEX_PCA_MISSING, review_required on the entry).
CAPEX_PCA_REVIEW_BEFORE_YEAR = 2000
# Where an immediate-capex budget is funded.
IMMEDIATE_CAPEX_EQUITY_AT_CLOSE = "equity_at_close"
IMMEDIATE_CAPEX_LENDER_HOLDBACK = "lender_holdback"
IMMEDIATE_CAPEX_FUNDING_MODES = (IMMEDIATE_CAPEX_EQUITY_AT_CLOSE, IMMEDIATE_CAPEX_LENDER_HOLDBACK)


def default_replacement_reserve_per_unit(
    year_built: Optional[int] = None,
    *,
    strategy: str = "cashflow",
    deferred: bool = False,
) -> Decimal:
    """House lender-style annual replacement reserve per unit (below NOI).

    Always ``HOUSE_REPLACEMENT_RESERVE_PER_UNIT``: the reserve does not vary by
    vintage, strategy or deferred maintenance. The arguments are accepted so
    callers that pass the deal profile keep working. Recurring capex by
    vintage is ``default_recurring_capex_per_unit``; the charge below NOI is
    the larger of the two.
    """
    return HOUSE_REPLACEMENT_RESERVE_PER_UNIT


def recurring_capex_band_pick(*, strategy: str = "cashflow", deferred: bool = False) -> str:
    """Which point of the vintage band the house uses: ``low``, ``midpoint`` or ``high``."""
    if deferred:
        return "high"
    if strategy == "value_add":
        return "midpoint"
    return "low"


def default_recurring_capex_per_unit(
    year_built: Optional[int],
    *,
    strategy: str = "cashflow",
    deferred: bool = False,
) -> Optional[Decimal]:
    """Annual/unit recurring capex (year 1) from the vintage economic CapEx band.

    - stabilized (cashflow) deals: the low end of the band;
    - value-add deals: the midpoint;
    - a deal flagged with deferred maintenance: the high end.

    ``None`` when the year built is unknown (the resolver then falls back to
    the $250 replacement reserve and flags the entry for review).
    """
    bucket = vintage_bucket(year_built)
    if bucket is None:
        return None
    row = VINTAGE_FRAMEWORK_2026[bucket]
    low = row["economic_capex_low"]
    high = row["economic_capex_high"]
    pick = recurring_capex_band_pick(strategy=strategy, deferred=deferred)
    if pick == "high":
        return high
    if pick == "midpoint":
        return (low + high) / Decimal("2")
    return low


def default_economic_capex_per_unit(
    year_built: Optional[int],
    *,
    strategy: str = "cashflow",
    deferred: bool = False,
) -> Optional[Decimal]:
    """Deprecated name of ``default_recurring_capex_per_unit`` (same values)."""
    return default_recurring_capex_per_unit(year_built, strategy=strategy, deferred=deferred)


def recurring_capex_band(year_built: Optional[int]) -> Optional[tuple[Decimal, Decimal]]:
    """The vintage economic CapEx band ``(low, high)`` per unit, or None when unknown."""
    bucket = vintage_bucket(year_built)
    if bucket is None:
        return None
    row = VINTAGE_FRAMEWORK_2026[bucket]
    return row["economic_capex_low"], row["economic_capex_high"]


def default_deferred_capex_placeholder_per_unit(year_built: Optional[int]) -> Optional[Decimal]:
    """Immediate capex per unit when deferred maintenance is flagged without a PCA or analyst figure.

    ``HOUSE_DEFERRED_CAPEX_PLACEHOLDER_YEARS`` x the vintage economic CapEx band
    high end (e.g. $2,500/unit for the 1980s). ``None`` when the year built is
    unknown (nothing is budgeted; the entry stays flagged).
    """
    band = recurring_capex_band(year_built)
    if band is None:
        return None
    return band[1] * HOUSE_DEFERRED_CAPEX_PLACEHOLDER_YEARS


def default_r_and_m_per_unit(
    year_built: Optional[int],
    *,
    strategy: str = "cashflow",
    deferred: bool = False,
) -> Optional[Decimal]:
    """Return a conservative annual/unit R&M assumption by vintage.

    The value is on ``R_AND_M_FLOOR_BASIS``: R&M including turnover /
    make-ready, excluding payroll. The low end of the band for a stabilized
    deal, the midpoint for value-add, the high end with deferred maintenance
    (``deferred``: ``deal_profile.deferred_maintenance``).
    """
    bucket = vintage_bucket(year_built)
    if bucket is None:
        return None
    row = VINTAGE_FRAMEWORK_2026[bucket]
    low = row["r_and_m_low"]
    high = row["r_and_m_high"]
    if deferred:
        return high
    if strategy == "value_add":
        return (low + high) / Decimal("2")
    return low


def default_turn_capex_per_unit(
    year_built: Optional[int],
    *,
    strategy: str = "cashflow",
) -> Optional[Decimal]:
    bucket = vintage_bucket(year_built)
    if bucket is None:
        return None
    row = VINTAGE_FRAMEWORK_2026[bucket]
    if strategy == "value_add" and "value_add_turn_low" in row:
        return (row["value_add_turn_low"] + row["value_add_turn_high"]) / Decimal("2")
    return row["turn_low"]


def default_loss_to_lease_rate(
    year_built: Optional[int],
    *,
    strategy: str = "cashflow",
) -> Optional[Decimal]:
    bucket = vintage_bucket(year_built)
    if bucket is None:
        return None
    value = VINTAGE_FRAMEWORK_2026[bucket]["ltl_floor"]
    if strategy == "value_add":
        return value + Decimal("0.0025")
    return value


# ── Deferred maintenance minimums (ADR 0003 §3, §5) ──────────────────────
# HOUSE POLICY, NOT MARKET DATA. A deal flagged with deferred maintenance
# (``deal_profile.deferred_maintenance``) carries more vacancy (down units,
# slower turns), weaker collections and more year-1 concessions (units come
# back on line into a discounted lease-up), and spends more on repairs. The
# flag moves each house minimum to the upper part of its vintage band, so the
# step scales with vintage, and never by less than a named step over the
# minimum the same deal would carry without the flag:
#
#   physical vacancy   max(vintage physical_vacancy_typical_high, floor + 100 bp)
#   collection loss    max(midpoint of the vintage bad_debt typical band, floor + 50 bp)
#   year-1 concessions max(midpoint of the vintage concessions typical band, floor + 50 bp)
#   R&M (ex payroll)   the vintage r_and_m_high (R&M including turnover)
#   recurring capex    the vintage economic_capex_high (``recurring_capex_band_pick``)
#
# "floor" is the minimum for the deal's strategy without the flag (the
# value-add floors already sit above the stabilized ones). For a 1980s
# stabilized deal: vacancy 5.0% -> 7.0%, collection loss 0.75% -> 2.25%,
# year-1 concessions 0.75% -> 1.50%, R&M $900 -> $1,700/unit. Physical vacancy
# + collection loss + year-1 concessions + loss to lease under these minimums
# lands inside every vintage's economic-vacancy typical band, and above the
# agency 5% minimum economic vacancy (``deferred_maintenance_economic_vacancy``).
#
# Sources (see ADR 0003, "Deferred maintenance minimums"). The band points are
# house judgment; the sources set the direction and the agency minimums:
# - Fannie Mae Multifamily Guide Part II 203.01 (conventional): underwritten
#   vacancy + concessions + bad debt is at least the greater of the trailing
#   3-month collections gap and 5% of GPR. Freddie Mac Guide ch. 17, 17.2(k):
#   vacancy and collection loss may not be less than 5%, adjusted upward when
#   conditions require; the Freddie SBL update of 2024-06-28 adds an economic
#   vacancy allowance on top of physical vacancy where there are concessions
#   or a history of delinquency or eviction.
# - Freddie Mac Guide ch. 62 (PCA): the engineer inspects at least half of the
#   down units and costs making them rent-ready, i.e. down units are a real
#   vacancy and capital item on older stock.
# - RealPage concessions report (December 2025 data, published 2026-01-23):
#   Class C has the most units offering concessions (19.1%, average discount
#   10.5%), above Class A/B. No public RealPage or Yardi Matrix series gives
#   Class C bad debt as a percent of rent; RealPage commentary describes Class C
#   bad debt as deeper than A/B, so the house uses its own vintage band.
# - NAA Income/Expense IQ 2024 data (NAA, 2025-12-22): national repairs &
#   maintenance $1,098/unit/year, turnover cost up 17.5% year on year, bad
#   debt $75/unit (an institutional, stabilized sample: not a Class C floor).
#   Older garden stock with deferred maintenance sits at the top of the
#   vintage R&M band (R&M including turnover).
DEFERRED_MAINTENANCE_VACANCY_STEP = Decimal("0.010")
DEFERRED_MAINTENANCE_COLLECTION_LOSS_STEP = Decimal("0.005")
DEFERRED_MAINTENANCE_CONCESSIONS_STEP = Decimal("0.005")
# Agency minimum underwritten vacancy and collection loss (Fannie Mae Guide
# Part II 203.01, Freddie Mac Guide 17.2(k)): a check on the deferred
# minimums, never applied as its own floor.
AGENCY_MIN_ECONOMIC_VACANCY = Decimal("0.05")


def _typical_midpoint(row: dict, name: str) -> Decimal:
    return (row[f"{name}_typical_low"] + row[f"{name}_typical_high"]) / Decimal("2")


def default_physical_vacancy_rate(
    year_built: Optional[int],
    *,
    strategy: str = "cashflow",
    deferred: bool = False,
) -> Optional[Decimal]:
    """House physical-vacancy floor: the vintage floor (+100 bp value-add).

    With deferred maintenance (``deferred``): max(vintage typical-band high,
    that floor + ``DEFERRED_MAINTENANCE_VACANCY_STEP``).
    """
    bucket = vintage_bucket(year_built)
    if bucket is None:
        return None
    row = VINTAGE_FRAMEWORK_2026[bucket]
    value = row["vacancy_floor"]
    if strategy == "value_add":
        value += Decimal("0.010")
    if deferred:
        return max(row["physical_vacancy_typical_high"], value + DEFERRED_MAINTENANCE_VACANCY_STEP)
    return value


def default_collection_loss_rate(
    year_built: Optional[int],
    *,
    strategy: str = "cashflow",
    deferred: bool = False,
) -> Optional[Decimal]:
    """House collection-loss floor: the vintage floor (+25 bp value-add).

    With deferred maintenance: max(midpoint of the vintage bad-debt typical
    band, that floor + ``DEFERRED_MAINTENANCE_COLLECTION_LOSS_STEP``).
    """
    bucket = vintage_bucket(year_built)
    if bucket is None:
        return None
    row = VINTAGE_FRAMEWORK_2026[bucket]
    value = row["collection_loss_floor"]
    if strategy == "value_add":
        value += Decimal("0.0025")
    if deferred:
        return max(_typical_midpoint(row, "bad_debt"), value + DEFERRED_MAINTENANCE_COLLECTION_LOSS_STEP)
    return value


def default_year1_concessions_rate(
    year_built: Optional[int],
    *,
    strategy: str = "cashflow",
    deferred: bool = False,
) -> Optional[Decimal]:
    """House year-1 concessions floor: the vintage floor (+25 bp value-add).

    With deferred maintenance: max(midpoint of the vintage concessions typical
    band, that floor + ``DEFERRED_MAINTENANCE_CONCESSIONS_STEP``).
    """
    bucket = vintage_bucket(year_built)
    if bucket is None:
        return None
    row = VINTAGE_FRAMEWORK_2026[bucket]
    value = row["year1_concessions_floor"]
    if strategy == "value_add":
        value += Decimal("0.0025")
    if deferred:
        return max(_typical_midpoint(row, "concessions"), value + DEFERRED_MAINTENANCE_CONCESSIONS_STEP)
    return value


def deferred_maintenance_economic_vacancy(
    year_built: Optional[int],
    *,
    strategy: str = "cashflow",
) -> Optional[Decimal]:
    """Economic vacancy implied by the deferred-maintenance minimums.

    Physical vacancy + collection loss + year-1 concessions (all with
    deferred maintenance) + the loss-to-lease floor. A disclosure and a test
    of the minimums against the vintage economic-vacancy band and
    ``AGENCY_MIN_ECONOMIC_VACANCY``; the engine floors each component, never
    the sum.
    """
    parts = (
        default_physical_vacancy_rate(year_built, strategy=strategy, deferred=True),
        default_collection_loss_rate(year_built, strategy=strategy, deferred=True),
        default_year1_concessions_rate(year_built, strategy=strategy, deferred=True),
        default_loss_to_lease_rate(year_built, strategy=strategy),
    )
    if any(part is None for part in parts):
        return None
    return sum(parts, Decimal("0"))


# ── ADR 0003 slice 4: house curve policy ─────────────────────────────────
# House policy used by the assumption resolver (engine/assumptions/curves.py)
# when the deal and the market data do not supply a value.

# Long-run annual market rent growth. Rent growth glides to this rate after
# the market forecast horizon, unless a provider supplies an MSA long-run rate.
HOUSE_LONG_RUN_RENT_GROWTH = Decimal("0.025")
# Years over which rent growth glides linearly from the last forecast year to
# the long-run rate.
HOUSE_RENT_GROWTH_GLIDE_YEARS = 2

# Default collection loss (bad debt) when the T12 shows none: the vintage
# ``bad_debt_typical_low``. When the year built is unknown the house uses 2.0%,
# the most conservative vintage typical-low (1960s). The vintage
# ``collection_loss_floor`` stays a guardrail floor, not the default.
HOUSE_DEFAULT_COLLECTION_LOSS = Decimal("0.020")

# Months over which physical vacancy moves linearly from the rent-roll level
# to stabilized vacancy, by deal type (ADR 0003 §1 deal_type).
HOUSE_STABILIZATION_WINDOW_MONTHS = {"stabilized": 12, "value_add": 24}
# Months over which the rent-roll loss to lease burns off to the vintage
# floor (one lease term).
HOUSE_LTL_BURN_OFF_MONTHS = 12
# Months the year-1 concession rate applies before concessions step to the
# stabilized level.
HOUSE_YEAR1_CONCESSION_MONTHS = 12

# ── Exit cap rate (ADR 0003 §3) ──────────────────────────────────────────
# HOUSE POLICY. Exit caps track entry caps: unless the analyst sets one, the
# exit cap is the going-in cap rate AT THE PRICE BEING MODELED (year-1 NOI /
# purchase price) plus HOUSE_EXIT_CAP_SPREAD. It is re-measured whenever the
# price changes (backsolve recomputes it at every candidate price), so a solved
# price never pairs a high entry cap with an exit cap anchored at the ask.
#
# 50 bps of cap-rate expansion over a typical 5-year hold is the conservative
# house assumption: it prices the exit on an older asset in a market that may
# not stay as tight as today's.
HOUSE_EXIT_CAP_SPREAD = Decimal("0.0050")
# A going-in cap above this is not usable (the price or the NOI is not
# comparable), so the exit is not anchored to it.
HOUSE_EXIT_CAP_GOING_IN_MAX = Decimal("0.25")
# The "screaming steal" exception, which is rare: only when a market or
# submarket cap rate is available AND the going-in cap at the modeled price is
# more than HOUSE_STEAL_THRESHOLD above it may the exit compress toward market
# + spread. Without market data there is never compression; a going-in cap at or
# below market (paying up) still exits at entry + spread.
#
# The compression scales with how big the discount is. With excess = going-in
# cap - market cap rate:
#   compression = min(1, (excess - HOUSE_STEAL_THRESHOLD)
#                        / (HOUSE_STEAL_FULL_COMPRESSION_EXCESS - HOUSE_STEAL_THRESHOLD))
#                 x HOUSE_MAX_EXIT_COMPRESSION
# 0 at or below the threshold (continuous there), growing linearly to the full
# HOUSE_MAX_EXIT_COMPRESSION at HOUSE_STEAL_FULL_COMPRESSION_EXCESS (threshold +
# 200 bps) and capped beyond it. A going-in cap just over the threshold no
# longer jumps to the full 100 bps. The exit is never below market + spread (so
# never below the market cap rate).
HOUSE_STEAL_THRESHOLD = Decimal("0.0150")
HOUSE_MAX_EXIT_COMPRESSION = Decimal("0.0100")
HOUSE_STEAL_FULL_COMPRESSION_EXCESS = HOUSE_STEAL_THRESHOLD + Decimal("0.0200")
# Exit-cap sensitivity grid (engine/modules/exit_cap_grid.py): every run and
# the backsolve summary report the base case at these offsets (bps) around the
# exit cap as run, each point pinned (never re-anchored to a price). Weighted
# to the downside: a tighter exit is the less likely error for an older asset.
HOUSE_EXIT_CAP_SENSITIVITY_OFFSETS_BPS = (-50, -25, 0, 25, 50, 75, 100, 150)


def house_exit_cap_rate(
    going_in_cap_rate: Decimal | float,
    *,
    market_cap_rate: Optional[Decimal | float] = None,
) -> dict[str, Decimal | bool | None]:
    """The house exit cap for a going-in cap rate measured at the modeled price.

    Returns ``exit_cap_rate`` and how it was reached: ``anchored`` (going-in +
    spread), ``market_cap_rate``, ``excess_over_market`` (going-in - market,
    None without market data), ``steal``, ``steal_scale`` (the fraction of
    ``HOUSE_MAX_EXIT_COMPRESSION`` applied, 0 to 1) and ``compression``
    (anchored minus the exit cap; 0 unless it is a steal). The caller guards
    the going-in cap with ``HOUSE_EXIT_CAP_GOING_IN_MAX``.
    """
    going_in = Decimal(str(going_in_cap_rate))
    market = Decimal(str(market_cap_rate)) if market_cap_rate is not None else None
    anchored = going_in + HOUSE_EXIT_CAP_SPREAD
    value = anchored
    excess = going_in - market if market is not None else None
    steal = excess is not None and excess > HOUSE_STEAL_THRESHOLD
    scale = Decimal("0")
    if steal:
        scale = min(
            Decimal("1"),
            (excess - HOUSE_STEAL_THRESHOLD) / (HOUSE_STEAL_FULL_COMPRESSION_EXCESS - HOUSE_STEAL_THRESHOLD),
        )
        value = max(market + HOUSE_EXIT_CAP_SPREAD, anchored - scale * HOUSE_MAX_EXIT_COMPRESSION)
    return {
        "going_in_cap_rate": going_in,
        "spread": HOUSE_EXIT_CAP_SPREAD,
        "anchored": anchored,
        "market_cap_rate": market,
        "excess_over_market": excess,
        "steal": steal,
        "steal_scale": scale,
        "compression": anchored - value,
        "exit_cap_rate": value,
    }


# ── Loan sizing NOI basis (ADR 0003 §3) ──────────────────────────────────
# HOUSE POLICY. Agency lenders size the loan on underwritten net cash flow
# (NOI less replacement reserves), not on NOI. The house sizes the DSCR
# constraint, and reports DSCR, on NOI less the same below-NOI capex charge
# the cash flow deducts: max(recurring capex by vintage, lender replacement
# reserve). An older asset that needs more capital every year therefore
# carries a smaller loan. The basis is an overridable assumption
# (``loan_sizing_noi_basis``); NOI-based DSCR stays visible beside it.
#
# - ncf_after_capex_charge: NOI - max(recurring capex, lender reserve) (default)
# - ncf_after_lender_reserve: NOI - the lender replacement reserve only
# - noi_before_capex: NOI (the engine's convention before this decision)
LOAN_SIZING_NCF_AFTER_CAPEX_CHARGE = "ncf_after_capex_charge"
LOAN_SIZING_NCF_AFTER_LENDER_RESERVE = "ncf_after_lender_reserve"
LOAN_SIZING_NOI_BEFORE_CAPEX = "noi_before_capex"
LOAN_SIZING_NOI_BASES = (
    LOAN_SIZING_NCF_AFTER_CAPEX_CHARGE,
    LOAN_SIZING_NCF_AFTER_LENDER_RESERVE,
    LOAN_SIZING_NOI_BEFORE_CAPEX,
)
HOUSE_LOAN_SIZING_NOI_BASIS = LOAN_SIZING_NCF_AFTER_CAPEX_CHARGE


def default_bad_debt_rate(
    year_built: Optional[int],
    *,
    strategy: str = "cashflow",
    deferred: bool = False,
) -> Decimal:
    """House default collection loss: vintage ``bad_debt_typical_low``, else 2.0%.

    With deferred maintenance the default is never below the deferred
    collection-loss floor (``default_collection_loss_rate``), so a house-only
    deal does not sit below its own floor.
    """
    bucket = vintage_bucket(year_built)
    if bucket is None:
        return HOUSE_DEFAULT_COLLECTION_LOSS
    value = VINTAGE_FRAMEWORK_2026[bucket]["bad_debt_typical_low"]
    if deferred:
        floor = default_collection_loss_rate(year_built, strategy=strategy, deferred=True)
        return max(value, floor) if floor is not None else value
    return value


def typical_physical_vacancy_rate(year_built: Optional[int]) -> Optional[Decimal]:
    """Midpoint of the vintage typical physical-vacancy band.

    The resolver's house stabilized vacancy is
    ``max(this midpoint, default_physical_vacancy_rate)`` for the deal's
    strategy (engine/assumptions/curves.py), so a house-only deal never sits
    below its own vacancy floor.
    """
    bucket = vintage_bucket(year_built)
    if bucket is None:
        return None
    row = VINTAGE_FRAMEWORK_2026[bucket]
    return (row["physical_vacancy_typical_low"] + row["physical_vacancy_typical_high"]) / Decimal("2")


def stabilized_concessions_rate(year_built: Optional[int]) -> Optional[Decimal]:
    """House stabilized concessions: the vintage ``concessions_typical_low``."""
    bucket = vintage_bucket(year_built)
    if bucket is None:
        return None
    return VINTAGE_FRAMEWORK_2026[bucket]["concessions_typical_low"]


def revenue_guardrails(
    year_built: Optional[int],
    *,
    strategy: str = "cashflow",
    deferred: bool = False,
) -> dict[str, Decimal | str | None]:
    bucket = vintage_bucket(year_built)
    if bucket is None:
        return {
            "vintage_bucket": None,
            "strategy": strategy,
            "physical_vacancy_typical_low": None,
            "physical_vacancy_typical_high": None,
            "physical_vacancy_stress_low": None,
            "physical_vacancy_stress_high": None,
            "economic_vacancy_typical_low": None,
            "economic_vacancy_typical_high": None,
            "economic_vacancy_stress_low": None,
            "economic_vacancy_stress_high": None,
            "bad_debt_typical_low": None,
            "bad_debt_typical_high": None,
            "bad_debt_stress_low": None,
            "bad_debt_stress_high": None,
            "concessions_typical_low": None,
            "concessions_typical_high": None,
            "concessions_stress_low": None,
            "concessions_stress_high": None,
            "other_income_low_per_unit_month": None,
            "other_income_high_per_unit_month": None,
            "other_income_strong_low_per_unit_month": None,
            "other_income_strong_high_per_unit_month": None,
        }

    row = VINTAGE_FRAMEWORK_2026[bucket]
    return {
        "vintage_bucket": bucket,
        "strategy": strategy,
        "physical_vacancy_typical_low": row["physical_vacancy_typical_low"],
        "physical_vacancy_typical_high": row["physical_vacancy_typical_high"],
        "physical_vacancy_stress_low": row["physical_vacancy_stress_low"],
        "physical_vacancy_stress_high": row["physical_vacancy_stress_high"],
        "economic_vacancy_typical_low": row["economic_vacancy_typical_low"],
        "economic_vacancy_typical_high": row["economic_vacancy_typical_high"],
        "economic_vacancy_stress_low": row["economic_vacancy_stress_low"],
        "economic_vacancy_stress_high": row["economic_vacancy_stress_high"],
        "bad_debt_typical_low": row["bad_debt_typical_low"],
        "bad_debt_typical_high": row["bad_debt_typical_high"],
        "bad_debt_stress_low": row["bad_debt_stress_low"],
        "bad_debt_stress_high": row["bad_debt_stress_high"],
        "concessions_typical_low": row["concessions_typical_low"],
        "concessions_typical_high": row["concessions_typical_high"],
        "concessions_stress_low": row["concessions_stress_low"],
        "concessions_stress_high": row["concessions_stress_high"],
        "other_income_low_per_unit_month": row["other_income_low_per_unit_month"],
        "other_income_high_per_unit_month": row["other_income_high_per_unit_month"],
        "other_income_strong_low_per_unit_month": row["other_income_strong_low_per_unit_month"],
        "other_income_strong_high_per_unit_month": row["other_income_strong_high_per_unit_month"],
        "physical_vacancy_house_floor": default_physical_vacancy_rate(
            year_built,
            strategy=strategy,
            deferred=deferred,
        ),
        "collection_loss_house_floor": default_collection_loss_rate(
            year_built,
            strategy=strategy,
            deferred=deferred,
        ),
        "concessions_house_floor": default_year1_concessions_rate(
            year_built,
            strategy=strategy,
            deferred=deferred,
        ),
        "loss_to_lease_house_floor": default_loss_to_lease_rate(
            year_built,
            strategy=strategy,
        ),
    }


def default_other_income_per_unit_month(
    year_built: Optional[int],
    *,
    strong_operator: bool = False,
) -> Optional[Decimal]:
    bucket = vintage_bucket(year_built)
    if bucket is None:
        return None
    row = VINTAGE_FRAMEWORK_2026[bucket]
    if strong_operator:
        return row["other_income_strong_low_per_unit_month"]
    return row["other_income_low_per_unit_month"]


def default_year1_insurance_per_unit(
    observed_per_unit: Optional[float | Decimal] = None,
) -> Decimal:
    """Apply the conservative Year-1 insurance floor.

    Use the higher of observed run-rate insurance and the house minimum
    of $600/unit. When the observed run rate is $900+/unit, flag it for
    claims-history review rather than silently normalizing it away.
    """
    if observed_per_unit is None:
        return DEFAULT_YEAR1_INSURANCE_PER_UNIT
    observed = Decimal(str(observed_per_unit))
    return max(observed, DEFAULT_YEAR1_INSURANCE_PER_UNIT)


def insurance_requires_claim_review(
    observed_per_unit: Optional[float | Decimal] = None,
) -> bool:
    if observed_per_unit is None:
        return False
    return Decimal(str(observed_per_unit)) >= HIGH_INSURANCE_REVIEW_THRESHOLD_PER_UNIT


def default_property_tax_reassessment_ratio(
    *,
    analyst_ratio_override: Optional[float | Decimal] = None,
) -> Decimal:
    """Return the Year-1 property-tax assessment multiplier.

    The house default is full purchase value. ``analyst_ratio_override`` is
    reserved for an explicitly approved, evidence-backed non-default
    multiplier for the local assessment convention.
    """
    if analyst_ratio_override is not None:
        return Decimal(str(analyst_ratio_override))
    return DEFAULT_PROPERTY_TAX_REASSESSMENT_RATIO


def estimated_year1_property_tax(
    *,
    purchase_price: float | Decimal,
    tax_rate_pct: float | Decimal,
    analyst_ratio_override: Optional[float | Decimal] = None,
) -> Decimal:
    """Estimate Year-1 taxes from purchase price and local tax rate."""
    assessment_ratio = default_property_tax_reassessment_ratio(
        analyst_ratio_override=analyst_ratio_override,
    )
    return (
        Decimal(str(purchase_price))
        * assessment_ratio
        * Decimal(str(tax_rate_pct))
    )


def default_vintage_operating_profile(
    year_built: Optional[int],
    *,
    strategy: str = "cashflow",
    deferred: bool = False,
    observed_insurance_per_unit: Optional[float | Decimal] = None,
    analyst_tax_ratio_override: Optional[float | Decimal] = None,
) -> dict[str, float | str | bool | None]:
    """Return the house-default operating profile for orchestration.

    This is the condensed policy surface sibling orchestrators should use
    when they need a conservative first-pass assumption set.
    """
    bucket = vintage_bucket(year_built)
    recurring_capex = default_recurring_capex_per_unit(year_built, strategy=strategy, deferred=deferred)
    return {
        "vintage_bucket": bucket,
        "strategy": strategy,
        "deferred": deferred,
        "r_and_m_per_unit": (
            float(default_r_and_m_per_unit(year_built, strategy=strategy, deferred=deferred))
            if default_r_and_m_per_unit(year_built, strategy=strategy, deferred=deferred) is not None
            else None
        ),
        "turn_cost_per_unit": (
            float(default_turn_capex_per_unit(year_built, strategy=strategy))
            if default_turn_capex_per_unit(year_built, strategy=strategy) is not None
            else None
        ),
        "replacement_reserve_per_unit": float(default_replacement_reserve_per_unit()),
        # Recurring capex by vintage (year 1, grown at HOUSE_RECURRING_CAPEX_GROWTH);
        # the charge below NOI is max(recurring capex, replacement reserve). The
        # older key names are kept for callers that read them.
        "recurring_capex_per_unit": float(recurring_capex) if recurring_capex is not None else None,
        "recurring_capex_growth": float(HOUSE_RECURRING_CAPEX_GROWTH),
        "economic_capex_per_unit": float(recurring_capex) if recurring_capex is not None else None,
        "economic_capex_reserve_per_unit": float(recurring_capex) if recurring_capex is not None else None,
        "year1_insurance_per_unit": float(
            default_year1_insurance_per_unit(observed_insurance_per_unit)
        ),
        "insurance_claim_review_required": insurance_requires_claim_review(
            observed_insurance_per_unit
        ),
        "property_tax_reassessment_ratio": float(
            default_property_tax_reassessment_ratio(
                analyst_ratio_override=analyst_tax_ratio_override,
            )
        ),
        "loss_to_lease_rate": (
            float(default_loss_to_lease_rate(year_built, strategy=strategy))
            if default_loss_to_lease_rate(year_built, strategy=strategy) is not None
            else None
        ),
        "physical_vacancy_rate": (
            float(default_physical_vacancy_rate(year_built, strategy=strategy, deferred=deferred))
            if default_physical_vacancy_rate(year_built, strategy=strategy, deferred=deferred) is not None
            else None
        ),
        "collection_loss_rate": (
            float(default_collection_loss_rate(year_built, strategy=strategy, deferred=deferred))
            if default_collection_loss_rate(year_built, strategy=strategy, deferred=deferred) is not None
            else None
        ),
        "year1_concessions_rate": (
            float(default_year1_concessions_rate(year_built, strategy=strategy, deferred=deferred))
            if default_year1_concessions_rate(year_built, strategy=strategy, deferred=deferred) is not None
            else None
        ),
        "other_income_per_unit_month": (
            float(default_other_income_per_unit_month(year_built))
            if default_other_income_per_unit_month(year_built) is not None
            else None
        ),
        "revenue_guardrails": {
            key: (float(value) if isinstance(value, Decimal) else value)
            for key, value in revenue_guardrails(
                year_built,
                strategy=strategy,
                deferred=deferred,
            ).items()
        },
    }
