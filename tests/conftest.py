"""
Shared pytest fixtures for the multifamily underwriting engine.

Provides minimal but schema-compliant deal inputs that can be composed
into full engine runs or used individually by module-level tests.
"""
import pytest

from engine.modules.time_grid import TimeGrid


@pytest.fixture
def minimal_time_grid():
    """5-year monthly time grid starting 2026-07."""
    return TimeGrid.build("2026-07", "2031-06")


@pytest.fixture
def minimal_unit_cohorts():
    """2 cohorts: 1BR (50 units, $1200) and 2BR (50 units, $1500)."""
    return [
        {
            "cohort_id": "1BR",
            "unit_type": "1BR",
            "unit_count": 50,
            "initial_inplace_rent": 1200,
        },
        {
            "cohort_id": "2BR",
            "unit_type": "2BR",
            "unit_count": 50,
            "initial_inplace_rent": 1500,
        },
    ]


@pytest.fixture
def minimal_deal_inputs(minimal_time_grid, minimal_unit_cohorts):
    """Complete deal inputs dict matching schema v0.1 — enough to run full engine.

    100 units, blended $1350/unit rent, 5-year hold from 2026-07 to 2031-06.
    Purchase price $13.5M at 5.5% cap, 65% LTV senior debt at 5.75%.
    Fund: 95/5 LP/GP split, 8% pref, 70/30 promote, 1% AM fee.
    """
    start = "2026-07"
    end = "2031-06"

    # Market rent curve — flat across both cohorts for the full hold
    market_rent_curve = [
        {"cohort_id": "1BR", "start_period": start, "end_period": end, "market_rent": 1250},
        {"cohort_id": "2BR", "start_period": start, "end_period": end, "market_rent": 1550},
    ]

    # Loss-to-lease — 3% for both cohorts (in-place below market)
    loss_to_lease = [
        {"cohort_id": "1BR", "start_period": start, "end_period": end, "ltl_percent": 0.03},
        {"cohort_id": "2BR", "start_period": start, "end_period": end, "ltl_percent": 0.03},
    ]

    # Physical vacancy — 5% stabilized
    physical_vacancy_curve = [
        {"cohort_id": "1BR", "start_period": start, "end_period": end, "vacancy_rate": 0.05},
        {"cohort_id": "2BR", "start_period": start, "end_period": end, "vacancy_rate": 0.05},
    ]

    # Collection loss — 1% flat
    collection_loss_curve = [
        {"applies_to": "ALL", "start_period": start, "end_period": end, "loss_rate": 0.01},
    ]

    # Revenue programs — RUBS utility recovery at $50/unit/month
    revenue_programs = [
        {
            "program_id": "RUBS",
            "program_name": "RUBS Utility Recovery",
            "program_type": "recovery",
            "pricing_type": "$/unit",
            "price_value": 50,
            "eligible_units": "ALL",
            "start_period": start,
        },
    ]

    program_adoption_curve = [
        {"program_id": "RUBS", "start_period": start, "end_period": end, "adoption_rate": 0.90},
    ]

    # OpEx — simplified table covering major categories
    opex_table = [
        {
            "category_name": "Real Estate Taxes",
            "calculation_type": "fixed_annual",
            "base_value": 337500,
            "growth_rate": 0.02,
            "recoverable_flag": False,
        },
        {
            "category_name": "Insurance",
            "calculation_type": "fixed_annual",
            "base_value": 60000,
            "growth_rate": 0.03,
            "recoverable_flag": False,
        },
        {
            "category_name": "R&M",
            "calculation_type": "per_unit",
            "base_value": 750,
            "growth_rate": 0.03,
            "recoverable_flag": False,
        },
        {
            "category_name": "Management Fee",
            "calculation_type": "percent_egr",
            "base_value": 0.04,
            "growth_rate": 0.0,
            "recoverable_flag": False,
        },
        {
            "category_name": "Utilities",
            "calculation_type": "per_unit_monthly",
            "base_value": 85,
            "growth_rate": 0.03,
            "recoverable_flag": True,
        },
    ]

    # Purchase assumptions — $13.5M, 65% LTV
    purchase_price = 13_500_000
    ltv = 0.65
    commitment = purchase_price * ltv  # $8,775,000
    equity = purchase_price - commitment  # $4,725,000
    closing_costs = 150_000

    purchase_assumptions = {
        "purchase_price": purchase_price,
        "closing_costs": closing_costs,
        "equity_contribution": equity,
        "total_equity_basis": equity + closing_costs,
    }

    # Debt terms — 5.75% fixed, 30-yr amort, 12 months I/O
    debt_terms = {
        "commitment": commitment,
        "rate": 0.0575,
        "amort_years": 30,
        "io_months": 12,
    }

    # Exit assumptions — sell month 60 at 5.5% cap
    exit_assumptions = {
        "exit_cap_rate": 0.055,
        "sale_cost_percent": 0.02,
        "exit_month": end,
    }

    # Fund assumptions — 95/5, 8% pref, 70/30 promote
    fund_assumptions = {
        "sponsor_equity_pct": 0.05,
        "lp_equity_pct": 0.95,
        "preferred_return": 0.08,
        "acquisition_fee_pct": 0.01,
        "asset_management_fee_pct": 0.01,
        "disposition_fee_pct": 0.01,
        "annual_partnership_expenses": 25000,
        "partnership_closing_costs": 50000,
        "promote_splits": [
            {
                "tier": "Pref",
                "hurdle_irr": 0.08,
                "lp_share": 1.0,
                "gp_share": 0.0,
            },
            {
                "tier": "Promote",
                "hurdle_irr": 0.0,
                "lp_share": 0.70,
                "gp_share": 0.30,
            },
        ],
    }

    # Growth assumptions — 3% annual compound
    growth_assumptions = {
        "growth_type": "annual_compound",
        "annual_growth_rate": 0.03,
    }

    # Replacement reserves — $250/unit/year
    replacement_reserves = [
        {
            "start_period": start,
            "end_period": end,
            "annual_amount": 25000,
        },
    ]

    return {
        "schema_version": "0.1",
        "metadata": {
            "deal_id": "TEST-001",
            "run_id": "test-run-001",
            "as_of_date": "2026-04-16",
            "analyst": "Test Analyst",
            "purpose": "Unit test fixture",
            "property_summary": {
                "property_tax_policy": {
                    "millage_rate_mills": 25.0,
                    "assessment_ratio": 1.0,
                    "source": "analyst",
                    "source_locator": "tests:minimum_deal_inputs",
                    "analyst_override": False,
                }
            },
        },
        "time_grid": {
            "analysis_start_date": start,
            "analysis_end_date": end,
        },
        "unit_cohorts": minimal_unit_cohorts,
        "market_rent_curve": market_rent_curve,
        "loss_to_lease": loss_to_lease,
        "physical_vacancy_curve": physical_vacancy_curve,
        "collection_loss_curve": collection_loss_curve,
        "revenue_programs": revenue_programs,
        "program_adoption_curve": program_adoption_curve,
        "utility_recovery_rules": [
            {
                "utility_category": "ALL",
                "recovery_basis": "percent_of_expense",
                "recovery_rate": 1.0,
                "lag_months": 0,
            }
        ],
        "opex_table": opex_table,
        "purchase_assumptions": purchase_assumptions,
        "debt_terms": debt_terms,
        "exit_assumptions": exit_assumptions,
        "fund_assumptions": fund_assumptions,
        "growth_assumptions": growth_assumptions,
        "replacement_reserves": replacement_reserves,
    }
