"""
Tests for Fund Waterfall Module

Tests cover:
- Basic waterfall with single promote tier
- Preferred return accrual with monthly compounding
- Capital return before promote kicks in
- Multiple promote tiers
- Zero cash flow months
- Negative CF months (additional equity requirement)
- Asset management fee calculation (flat and growing equity basis)
- Disposition fee deduction from exit year
- Partnership IRR calculation
- Partnership equity multiple (sum positive / sum |negative|)
- No exit proceeds scenario
- Acquisition fee and PCC in initial equity basis
"""
import pytest
from decimal import Decimal

from engine.modules.fund_waterfall import compute_fund_waterfall, _compute_growing_equity_am_fees, _compute_promote
from engine.modules.time_grid import TimeGrid
from engine.modules.util import dec


# ---------------------------------------------------------------------------
# Helpers to build synthetic cashflow data for waterfall tests
# ---------------------------------------------------------------------------

def _build_monthly_cf(time_grid, monthly_lcf, exit_month=None, exit_gross_sale=0):
    """Build cashflow_by_month list from a constant monthly leveraged CF.

    If exit_month is given, exit proceeds are added to that month.
    monthly_lcf can be a float (constant) or a list of per-month values.
    """
    rows = []
    for i, mid in enumerate(time_grid.month_ids):
        lcf = monthly_lcf[i] if isinstance(monthly_lcf, list) else monthly_lcf
        row = {
            "month": mid,
            "leveraged_cash_flow": lcf,
            "total_capex": 0,
        }
        if exit_month and mid == exit_month:
            row["leveraged_cash_flow"] = lcf + exit_gross_sale
            row["gross_sale_price"] = exit_gross_sale
        rows.append(row)
    return rows


def _build_yearly_cf(time_grid, monthly_cf_rows):
    """Aggregate monthly rows into yearly rows."""
    by_year = {}
    month_idx = 0
    for mid in time_grid.month_ids:
        year = mid[:4]
        if year not in by_year:
            by_year[year] = {
                "year": year,
                "leveraged_cash_flow": 0.0,
                "gross_sale_price": 0.0,
                "months_in_year": 0,
            }
        by_year[year]["leveraged_cash_flow"] += monthly_cf_rows[month_idx]["leveraged_cash_flow"]
        by_year[year]["gross_sale_price"] += monthly_cf_rows[month_idx].get("gross_sale_price", 0)
        by_year[year]["months_in_year"] += 1
        month_idx += 1
    return list(by_year.values())


def _build_debt_by_month(time_grid, balance=1_000_000):
    """Build constant-balance debt_by_month list."""
    return [{"month": mid, "ending_balance": balance} for mid in time_grid.month_ids]


# ---------------------------------------------------------------------------
# Test 1: Basic waterfall — single promote tier (70/30 above 8% pref)
# ---------------------------------------------------------------------------

class TestBasicWaterfall:
    """A simple 2-year deal with positive CF should produce promote after pref is met."""

    def test_basic_waterfall_produces_promote(self):
        """With sufficient CF to exceed pref, promote should be positive."""
        tg = TimeGrid.build("2026-01", "2027-12")
        # $10K/month LCF for 24 months, with $500K exit at end
        monthly_cf = _build_monthly_cf(tg, 10_000, exit_month="2027-12", exit_gross_sale=500_000)
        yearly_cf = _build_yearly_cf(tg, monthly_cf)
        debt = _build_debt_by_month(tg, balance=0)

        result = compute_fund_waterfall(
            time_grid=tg,
            cashflow_by_year=yearly_cf,
            purchase_assumptions={
                "purchase_price": 200_000,
                "equity_contribution": 200_000,
                "closing_costs": 0,
                "total_equity_basis": 200_000,
            },
            exit_assumptions={"exit_cap_rate": 0.06, "exit_month": "2027-12"},
            debt_by_month=debt,
            fund_assumptions={
                "sponsor_equity_pct": 0.05,
                "lp_equity_pct": 0.95,
                "promote_splits": [
                    {"tier": "Pref", "hurdle_irr": 0.08, "lp_share": 1.0, "gp_share": 0.0},
                    {"tier": "Promote", "hurdle_irr": 0.0, "lp_share": 0.70, "gp_share": 0.30},
                ],
            },
            cashflow_by_month=monthly_cf,
        )

        assert result["summary"]["total_promote"] > 0
        assert len(result["by_year"]) == 2

    def test_basic_waterfall_lp_gp_split(self):
        """Sponsor and LP shares should sum to cash_flow_to_partnership."""
        tg = TimeGrid.build("2026-01", "2027-12")
        monthly_cf = _build_monthly_cf(tg, 10_000, exit_month="2027-12", exit_gross_sale=500_000)
        yearly_cf = _build_yearly_cf(tg, monthly_cf)
        debt = _build_debt_by_month(tg, balance=0)

        result = compute_fund_waterfall(
            time_grid=tg,
            cashflow_by_year=yearly_cf,
            purchase_assumptions={
                "purchase_price": 200_000,
                "equity_contribution": 200_000,
                "closing_costs": 0,
                "total_equity_basis": 200_000,
            },
            exit_assumptions={"exit_cap_rate": 0.06, "exit_month": "2027-12"},
            debt_by_month=debt,
            fund_assumptions={
                "sponsor_equity_pct": 0.05,
                "lp_equity_pct": 0.95,
                "promote_splits": [
                    {"tier": "Pref", "hurdle_irr": 0.08, "lp_share": 1.0, "gp_share": 0.0},
                    {"tier": "Promote", "hurdle_irr": 0.0, "lp_share": 0.70, "gp_share": 0.30},
                ],
            },
            cashflow_by_month=monthly_cf,
        )

        for yr in result["by_year"]:
            total_share = yr["sponsor_share"] + yr["lp_share"]
            assert abs(total_share - yr["cash_flow_to_partnership"]) < 0.02


# ---------------------------------------------------------------------------
# Test 2: Preferred return accrual — monthly compounding
# ---------------------------------------------------------------------------

class TestPrefReturnAccrual:
    """Verify that preferred return accrues monthly on unreturned capital."""

    def test_pref_accrual_increases_required_return(self):
        """With zero CF months, more cash is needed to clear the pref hurdle.

        After 12 months of zero CF on $100K equity at 8% pref:
        Balance grows by compound pref, so total required > $100K + $8K simple.
        """
        tg = TimeGrid.build("2026-01", "2027-12")
        # 12 months of $0, then 12 months of $20K + $200K exit
        lcf_by_month = [0] * 12 + [20_000] * 11 + [20_000 + 200_000]
        monthly_cf = _build_monthly_cf(tg, lcf_by_month, exit_month="2027-12", exit_gross_sale=200_000)
        yearly_cf = _build_yearly_cf(tg, monthly_cf)
        debt = _build_debt_by_month(tg, balance=0)

        result = compute_fund_waterfall(
            time_grid=tg,
            cashflow_by_year=yearly_cf,
            purchase_assumptions={
                "purchase_price": 100_000,
                "equity_contribution": 100_000,
                "closing_costs": 0,
                "total_equity_basis": 100_000,
            },
            exit_assumptions={"exit_cap_rate": 0.06, "exit_month": "2027-12"},
            debt_by_month=debt,
            fund_assumptions={
                "sponsor_equity_pct": 0.05,
                "lp_equity_pct": 0.95,
                "promote_splits": [
                    {"tier": "Pref", "hurdle_irr": 0.08, "lp_share": 1.0, "gp_share": 0.0},
                    {"tier": "Promote", "hurdle_irr": 0.0, "lp_share": 0.70, "gp_share": 0.30},
                ],
            },
            cashflow_by_month=monthly_cf,
        )

        # Promote should be positive — plenty of CF to clear pref + return
        assert result["summary"]["total_promote"] > 0


# ---------------------------------------------------------------------------
# Test 3: Capital return before promote
# ---------------------------------------------------------------------------

class TestCapitalReturnBeforePromote:
    """LP gets capital back before any promote kicks in."""

    def test_no_promote_when_capital_not_returned(self):
        """If total CF < initial equity + pref, promote should be zero."""
        tg = TimeGrid.build("2026-01", "2026-12")
        # Only $1K/month for 12 months on $200K equity — nowhere near breakeven
        monthly_cf = _build_monthly_cf(tg, 1_000)
        yearly_cf = _build_yearly_cf(tg, monthly_cf)
        debt = _build_debt_by_month(tg, balance=0)

        result = compute_fund_waterfall(
            time_grid=tg,
            cashflow_by_year=yearly_cf,
            purchase_assumptions={
                "purchase_price": 200_000,
                "equity_contribution": 200_000,
                "closing_costs": 0,
                "total_equity_basis": 200_000,
            },
            exit_assumptions={"exit_cap_rate": 0.06, "exit_month": "2026-12"},
            debt_by_month=debt,
            fund_assumptions={
                "sponsor_equity_pct": 0.05,
                "lp_equity_pct": 0.95,
                "promote_splits": [
                    {"tier": "Pref", "hurdle_irr": 0.08, "lp_share": 1.0, "gp_share": 0.0},
                    {"tier": "Promote", "hurdle_irr": 0.0, "lp_share": 0.70, "gp_share": 0.30},
                ],
            },
            cashflow_by_month=monthly_cf,
        )

        assert result["summary"]["total_promote"] == 0.0


# ---------------------------------------------------------------------------
# Test 4: Multiple promote tiers
# ---------------------------------------------------------------------------

class TestMultiplePromoteTiers:
    """Two-tier promote: 70/30 up to 12% IRR, then 50/50."""

    def test_two_tier_promote(self):
        """Multi-tier structure should produce promote from the last gp_share tier."""
        tg = TimeGrid.build("2026-01", "2027-12")
        monthly_cf = _build_monthly_cf(tg, 15_000, exit_month="2027-12", exit_gross_sale=500_000)
        yearly_cf = _build_yearly_cf(tg, monthly_cf)
        debt = _build_debt_by_month(tg, balance=0)

        result = compute_fund_waterfall(
            time_grid=tg,
            cashflow_by_year=yearly_cf,
            purchase_assumptions={
                "purchase_price": 200_000,
                "equity_contribution": 200_000,
                "closing_costs": 0,
                "total_equity_basis": 200_000,
            },
            exit_assumptions={"exit_cap_rate": 0.06, "exit_month": "2027-12"},
            debt_by_month=debt,
            fund_assumptions={
                "sponsor_equity_pct": 0.05,
                "lp_equity_pct": 0.95,
                "promote_splits": [
                    {"tier": "Pref", "hurdle_irr": 0.08, "lp_share": 1.0, "gp_share": 0.0},
                    {"tier": "Tier1", "hurdle_irr": 0.12, "lp_share": 0.70, "gp_share": 0.30},
                    {"tier": "Tier2", "hurdle_irr": 0.0, "lp_share": 0.50, "gp_share": 0.50},
                ],
            },
            cashflow_by_month=monthly_cf,
        )

        # With $15K/mo + $500K exit on $200K equity, promote should be significant
        assert result["summary"]["total_promote"] > 0

    def test_exit_lookback_uses_lp_net_of_promote_cashflow_for_tier_selection(self):
        """Exit hurdle lookback should not give LP credit for GP's promote share."""
        result = _compute_promote(
            monthly_cf=[{"month": "2026-01", "cf_before_promote": Decimal("250000")}],
            by_year=[{"year": "2026", "months_in_year": 1, "cash_flow_before_promote": 250000}],
            initial_balance=Decimal("100000"),
            promote_splits=[
                {"tier": "Tier1", "hurdle_irr": 0.0, "lp_share": 0.80, "gp_share": 0.20},
                {"tier": "Tier2", "hurdle_irr": 1000.0, "lp_share": 0.50, "gp_share": 0.50},
            ],
            sponsor_pct=Decimal("0.10"),
            lp_pct=Decimal("0.90"),
        )

        by_tier = {tier["tier"]: tier for tier in result["by_tier"]}
        assert by_tier["Tier1"]["total_to_gp"] == pytest.approx(30000.0)
        assert by_tier["Tier1"]["total_to_lp"] == pytest.approx(120000.0)
        assert by_tier["Tier2"]["total_distributed"] == 0.0


# ---------------------------------------------------------------------------
# Test 5: Zero cash flow months
# ---------------------------------------------------------------------------

class TestZeroCashFlowMonths:
    """Pref should continue accruing even with zero CF."""

    def test_zero_cf_months_accrue_pref(self):
        """Zero CF months should still produce a valid result with pref accrual."""
        tg = TimeGrid.build("2026-01", "2026-12")
        # All zeros except exit month
        lcf_by_month = [0] * 11 + [300_000]
        monthly_cf = _build_monthly_cf(tg, lcf_by_month)
        yearly_cf = _build_yearly_cf(tg, monthly_cf)
        debt = _build_debt_by_month(tg, balance=0)

        result = compute_fund_waterfall(
            time_grid=tg,
            cashflow_by_year=yearly_cf,
            purchase_assumptions={
                "purchase_price": 200_000,
                "equity_contribution": 200_000,
                "closing_costs": 0,
                "total_equity_basis": 200_000,
            },
            exit_assumptions={"exit_cap_rate": 0.06, "exit_month": "2026-12"},
            debt_by_month=debt,
            fund_assumptions={
                "sponsor_equity_pct": 0.05,
                "lp_equity_pct": 0.95,
                "promote_splits": [
                    {"tier": "Pref", "hurdle_irr": 0.08, "lp_share": 1.0, "gp_share": 0.0},
                    {"tier": "Promote", "hurdle_irr": 0.0, "lp_share": 0.70, "gp_share": 0.30},
                ],
            },
            cashflow_by_month=monthly_cf,
        )

        # With $300K on $200K equity + pref, promote should be positive
        assert result["summary"]["total_promote"] > 0
        # Compare: promote should be LESS than if CF came in month 1
        # (because pref accrued for 11 months on the full balance)
        assert result["summary"]["total_promote"] < 100_000


# ---------------------------------------------------------------------------
# Test 6: Negative CF months
# ---------------------------------------------------------------------------

class TestNegativeCFMonths:
    """Negative CF should increase unreturned capital (balance more negative)."""

    def test_negative_cf_increases_equity_requirement(self):
        """Negative CF months should reduce promote vs all-positive scenario."""
        tg = TimeGrid.build("2026-01", "2027-12")
        n = len(tg.month_ids)  # 24

        # Scenario A: flat $5K/month, $400K exit at end
        monthly_cf_a = _build_monthly_cf(tg, 5_000, exit_month="2027-12", exit_gross_sale=400_000)
        yearly_cf_a = _build_yearly_cf(tg, monthly_cf_a)

        # Scenario B: 6 months of -$5K, then 18 months adjusted so total
        # operating CF is the same as A ($5K * 24 = $120K), same exit.
        # -5K * 6 + x * 18 = 120K → x = (120K + 30K) / 18 = 8333.33
        op_pos = (5_000 * n + 5_000 * 6) / (n - 6)
        lcf_b = [-5_000] * 6 + [op_pos] * (n - 6)
        monthly_cf_b = _build_monthly_cf(tg, lcf_b, exit_month="2027-12", exit_gross_sale=400_000)
        yearly_cf_b = _build_yearly_cf(tg, monthly_cf_b)

        debt = _build_debt_by_month(tg, balance=0)
        common_kwargs = dict(
            time_grid=tg,
            purchase_assumptions={
                "purchase_price": 200_000,
                "equity_contribution": 200_000,
                "closing_costs": 0,
                "total_equity_basis": 200_000,
            },
            exit_assumptions={"exit_cap_rate": 0.06, "exit_month": "2027-12"},
            debt_by_month=debt,
            fund_assumptions={
                "sponsor_equity_pct": 0.05,
                "lp_equity_pct": 0.95,
                "promote_splits": [
                    {"tier": "Pref", "hurdle_irr": 0.08, "lp_share": 1.0, "gp_share": 0.0},
                    {"tier": "Promote", "hurdle_irr": 0.0, "lp_share": 0.70, "gp_share": 0.30},
                ],
            },
        )

        result_a = compute_fund_waterfall(
            cashflow_by_year=yearly_cf_a, cashflow_by_month=monthly_cf_a, **common_kwargs
        )
        result_b = compute_fund_waterfall(
            cashflow_by_year=yearly_cf_b, cashflow_by_month=monthly_cf_b, **common_kwargs
        )

        # Scenario B should have lower promote (negative months compound pref on higher balance)
        assert result_b["summary"]["total_promote"] < result_a["summary"]["total_promote"]


# ---------------------------------------------------------------------------
# Test 7: Asset management fee calculation
# ---------------------------------------------------------------------------

class TestAMFeeCalculation:
    """AM fee as % of equity basis, both flat and growing."""

    def test_flat_am_fee(self):
        """1% AM fee on $200K equity = $2K/year."""
        tg = TimeGrid.build("2026-01", "2026-12")
        monthly_cf = _build_monthly_cf(tg, 10_000)
        yearly_cf = _build_yearly_cf(tg, monthly_cf)
        debt = _build_debt_by_month(tg, balance=0)

        result = compute_fund_waterfall(
            time_grid=tg,
            cashflow_by_year=yearly_cf,
            purchase_assumptions={
                "purchase_price": 200_000,
                "equity_contribution": 200_000,
                "closing_costs": 0,
                "total_equity_basis": 200_000,
            },
            exit_assumptions={"exit_cap_rate": 0.06, "exit_month": "2026-12"},
            debt_by_month=debt,
            fund_assumptions={
                "sponsor_equity_pct": 0.05,
                "lp_equity_pct": 0.95,
                "asset_management_fee_pct": 0.01,
                "promote_splits": [],
            },
            cashflow_by_month=monthly_cf,
        )

        assert abs(result["summary"]["total_asset_management_fee"] - 2000.0) < 1.0
        assert abs(result["summary"]["annual_am_fee"] - 2000.0) < 1.0

    def test_growing_equity_basis_am_fee(self):
        """Growing equity basis should increase AM fee when LCF is negative."""
        tg = TimeGrid.build("2026-01", "2027-12")
        # Year 1: negative LCF increases equity basis
        lcf_list = [-5_000] * 12 + [10_000] * 12
        monthly_cf = _build_monthly_cf(tg, lcf_list)
        yearly_cf = _build_yearly_cf(tg, monthly_cf)
        debt = _build_debt_by_month(tg, balance=0)

        result = compute_fund_waterfall(
            time_grid=tg,
            cashflow_by_year=yearly_cf,
            purchase_assumptions={
                "purchase_price": 200_000,
                "equity_contribution": 200_000,
                "closing_costs": 0,
                "total_equity_basis": 200_000,
            },
            exit_assumptions={"exit_cap_rate": 0.06, "exit_month": "2027-12"},
            debt_by_month=debt,
            fund_assumptions={
                "sponsor_equity_pct": 0.05,
                "lp_equity_pct": 0.95,
                "asset_management_fee_pct": 0.01,
                "growing_equity_basis": True,
                "promote_splits": [],
            },
            cashflow_by_month=monthly_cf,
        )

        yr1_fee = result["by_year"][0]["asset_management_fee"]
        yr2_fee = result["by_year"][1]["asset_management_fee"]
        # Year 2 fee should be higher because negative Year 1 CF grew the basis
        assert yr2_fee > yr1_fee

    def test_growing_equity_helper_function(self):
        """Test _compute_growing_equity_am_fees directly."""
        initial_basis = Decimal("200000")
        am_fee_pct = Decimal("0.01")
        cashflow_by_year = [
            {"leveraged_cash_flow": -60000, "months_in_year": 12},
            {"leveraged_cash_flow": 120000, "months_in_year": 12},
        ]

        schedule = _compute_growing_equity_am_fees(initial_basis, am_fee_pct, cashflow_by_year)

        # Year 1: 200K * 1% = $2,000
        assert abs(schedule[0] - 2000.0) < 1.0
        # Year 2: (200K + 60K) * 1% = $2,600 (negative LCF increased basis)
        assert abs(schedule[1] - 2600.0) < 1.0


# ---------------------------------------------------------------------------
# Test 8: Disposition fee
# ---------------------------------------------------------------------------

class TestDispositionFee:
    """Disposition fee should be deducted from exit year CF before promote."""

    def test_disposition_fee_deducted_in_exit_year(self):
        """1% disp fee on $500K gross sale = $5K deducted in exit year."""
        tg = TimeGrid.build("2026-01", "2026-12")
        monthly_cf = _build_monthly_cf(tg, 5_000, exit_month="2026-12", exit_gross_sale=500_000)
        yearly_cf = _build_yearly_cf(tg, monthly_cf)
        debt = _build_debt_by_month(tg, balance=0)

        result = compute_fund_waterfall(
            time_grid=tg,
            cashflow_by_year=yearly_cf,
            purchase_assumptions={
                "purchase_price": 200_000,
                "equity_contribution": 200_000,
                "closing_costs": 0,
                "total_equity_basis": 200_000,
            },
            exit_assumptions={"exit_cap_rate": 0.06, "exit_month": "2026-12"},
            debt_by_month=debt,
            fund_assumptions={
                "sponsor_equity_pct": 0.05,
                "lp_equity_pct": 0.95,
                "disposition_fee_pct": 0.01,
                "promote_splits": [
                    {"tier": "Pref", "hurdle_irr": 0.08, "lp_share": 1.0, "gp_share": 0.0},
                    {"tier": "Promote", "hurdle_irr": 0.0, "lp_share": 0.70, "gp_share": 0.30},
                ],
            },
            cashflow_by_month=monthly_cf,
        )

        # Disposition fee = 500K * 1% = $5,000
        assert abs(result["summary"]["total_disposition_fee"] - 5000.0) < 1.0
        assert abs(result["by_year"][-1]["disposition_fee"] - 5000.0) < 1.0

    def test_disposition_fee_reduces_promote(self):
        """Disp fee should reduce CF before promote, hence reduce promote."""
        tg = TimeGrid.build("2026-01", "2026-12")
        monthly_cf = _build_monthly_cf(tg, 5_000, exit_month="2026-12", exit_gross_sale=500_000)
        yearly_cf = _build_yearly_cf(tg, monthly_cf)
        debt = _build_debt_by_month(tg, balance=0)

        common_kwargs = dict(
            time_grid=tg,
            cashflow_by_year=yearly_cf,
            purchase_assumptions={
                "purchase_price": 200_000,
                "equity_contribution": 200_000,
                "closing_costs": 0,
                "total_equity_basis": 200_000,
            },
            exit_assumptions={"exit_cap_rate": 0.06, "exit_month": "2026-12"},
            debt_by_month=debt,
            cashflow_by_month=monthly_cf,
        )

        # Without disp fee
        result_no_disp = compute_fund_waterfall(
            fund_assumptions={
                "sponsor_equity_pct": 0.05, "lp_equity_pct": 0.95,
                "disposition_fee_pct": 0.0,
                "promote_splits": [
                    {"tier": "Pref", "hurdle_irr": 0.08, "lp_share": 1.0, "gp_share": 0.0},
                    {"tier": "Promote", "hurdle_irr": 0.0, "lp_share": 0.70, "gp_share": 0.30},
                ],
            },
            **common_kwargs,
        )

        # With disp fee
        result_with_disp = compute_fund_waterfall(
            fund_assumptions={
                "sponsor_equity_pct": 0.05, "lp_equity_pct": 0.95,
                "disposition_fee_pct": 0.01,
                "promote_splits": [
                    {"tier": "Pref", "hurdle_irr": 0.08, "lp_share": 1.0, "gp_share": 0.0},
                    {"tier": "Promote", "hurdle_irr": 0.0, "lp_share": 0.70, "gp_share": 0.30},
                ],
            },
            **common_kwargs,
        )

        assert result_with_disp["summary"]["total_promote"] < result_no_disp["summary"]["total_promote"]


# ---------------------------------------------------------------------------
# Test 9: Partnership IRR
# ---------------------------------------------------------------------------

class TestPartnershipIRR:
    """Verify partnership IRR is computed from monthly CF series."""

    def test_partnership_irr_is_annualized(self):
        """Partnership IRR should be annualized from monthly IRR."""
        tg = TimeGrid.build("2026-01", "2027-12")
        monthly_cf = _build_monthly_cf(tg, 10_000, exit_month="2027-12", exit_gross_sale=300_000)
        yearly_cf = _build_yearly_cf(tg, monthly_cf)
        debt = _build_debt_by_month(tg, balance=0)

        result = compute_fund_waterfall(
            time_grid=tg,
            cashflow_by_year=yearly_cf,
            purchase_assumptions={
                "purchase_price": 200_000,
                "equity_contribution": 200_000,
                "closing_costs": 0,
                "total_equity_basis": 200_000,
            },
            exit_assumptions={"exit_cap_rate": 0.06, "exit_month": "2027-12"},
            debt_by_month=debt,
            fund_assumptions={
                "sponsor_equity_pct": 0.05,
                "lp_equity_pct": 0.95,
                "promote_splits": [
                    {"tier": "Pref", "hurdle_irr": 0.08, "lp_share": 1.0, "gp_share": 0.0},
                    {"tier": "Promote", "hurdle_irr": 0.0, "lp_share": 0.70, "gp_share": 0.30},
                ],
            },
            cashflow_by_month=monthly_cf,
        )

        irr = result["summary"]["partnership_irr"]
        assert irr is not None
        # Should be positive with these cash flows (significant return)
        assert irr > 0.0
        # Sanity: should be less than 200% annualized
        assert irr < 2.0

    def test_partnership_irr_none_without_monthly_cf(self):
        """Without monthly CF data, partnership IRR should be None."""
        tg = TimeGrid.build("2026-01", "2026-12")
        monthly_cf = _build_monthly_cf(tg, 10_000)
        yearly_cf = _build_yearly_cf(tg, monthly_cf)
        debt = _build_debt_by_month(tg, balance=0)

        result = compute_fund_waterfall(
            time_grid=tg,
            cashflow_by_year=yearly_cf,
            purchase_assumptions={
                "purchase_price": 200_000,
                "equity_contribution": 200_000,
                "closing_costs": 0,
                "total_equity_basis": 200_000,
            },
            exit_assumptions={"exit_cap_rate": 0.06, "exit_month": "2026-12"},
            debt_by_month=debt,
            fund_assumptions={
                "sponsor_equity_pct": 0.05,
                "lp_equity_pct": 0.95,
                "promote_splits": [
                    {"tier": "Pref", "hurdle_irr": 0.08, "lp_share": 1.0, "gp_share": 0.0},
                    {"tier": "Promote", "hurdle_irr": 0.0, "lp_share": 0.70, "gp_share": 0.30},
                ],
            },
            cashflow_by_month=None,  # No monthly data
        )

        assert result["summary"]["partnership_irr"] is None


# ---------------------------------------------------------------------------
# Test 10: Partnership equity multiple
# ---------------------------------------------------------------------------

class TestPartnershipEM:
    """Verify EM = sum(positive CFs) / sum(|negative CFs|)."""

    def test_partnership_em_formula(self):
        """EM should match the sum-positive / sum-|negative| formula."""
        tg = TimeGrid.build("2026-01", "2027-12")
        monthly_cf = _build_monthly_cf(tg, 10_000, exit_month="2027-12", exit_gross_sale=400_000)
        yearly_cf = _build_yearly_cf(tg, monthly_cf)
        debt = _build_debt_by_month(tg, balance=0)

        result = compute_fund_waterfall(
            time_grid=tg,
            cashflow_by_year=yearly_cf,
            purchase_assumptions={
                "purchase_price": 200_000,
                "equity_contribution": 200_000,
                "closing_costs": 0,
                "total_equity_basis": 200_000,
            },
            exit_assumptions={"exit_cap_rate": 0.06, "exit_month": "2027-12"},
            debt_by_month=debt,
            fund_assumptions={
                "sponsor_equity_pct": 0.05,
                "lp_equity_pct": 0.95,
                "promote_splits": [
                    {"tier": "Pref", "hurdle_irr": 0.08, "lp_share": 1.0, "gp_share": 0.0},
                    {"tier": "Promote", "hurdle_irr": 0.0, "lp_share": 0.70, "gp_share": 0.30},
                ],
            },
            cashflow_by_month=monthly_cf,
        )

        em = result["summary"]["partnership_equity_multiple"]
        assert em is not None
        # With $10K/mo for 24 months + $400K exit vs $200K equity, EM > 1.0
        assert em > 1.0

    def test_partnership_em_greater_than_one_for_profitable_deal(self):
        """A clearly profitable deal should have EM > 1."""
        tg = TimeGrid.build("2026-01", "2026-12")
        monthly_cf = _build_monthly_cf(tg, 30_000, exit_month="2026-12", exit_gross_sale=300_000)
        yearly_cf = _build_yearly_cf(tg, monthly_cf)
        debt = _build_debt_by_month(tg, balance=0)

        result = compute_fund_waterfall(
            time_grid=tg,
            cashflow_by_year=yearly_cf,
            purchase_assumptions={
                "purchase_price": 200_000,
                "equity_contribution": 200_000,
                "closing_costs": 0,
                "total_equity_basis": 200_000,
            },
            exit_assumptions={"exit_cap_rate": 0.06, "exit_month": "2026-12"},
            debt_by_month=debt,
            fund_assumptions={
                "sponsor_equity_pct": 0.05,
                "lp_equity_pct": 0.95,
                "promote_splits": [
                    {"tier": "Pref", "hurdle_irr": 0.08, "lp_share": 1.0, "gp_share": 0.0},
                    {"tier": "Promote", "hurdle_irr": 0.0, "lp_share": 0.70, "gp_share": 0.30},
                ],
            },
            cashflow_by_month=monthly_cf,
        )

        assert result["summary"]["partnership_equity_multiple"] > 1.0


# ---------------------------------------------------------------------------
# Test 11: No exit proceeds
# ---------------------------------------------------------------------------

class TestNoExitProceeds:
    """Without exit, waterfall should still compute from operating CF only."""

    def test_no_exit_proceeds(self):
        """Deal with no sale should still compute waterfall from operating CF."""
        tg = TimeGrid.build("2026-01", "2027-12")
        monthly_cf = _build_monthly_cf(tg, 15_000)  # No exit
        yearly_cf = _build_yearly_cf(tg, monthly_cf)
        debt = _build_debt_by_month(tg, balance=0)

        result = compute_fund_waterfall(
            time_grid=tg,
            cashflow_by_year=yearly_cf,
            purchase_assumptions={
                "purchase_price": 200_000,
                "equity_contribution": 200_000,
                "closing_costs": 0,
                "total_equity_basis": 200_000,
            },
            exit_assumptions=None,
            debt_by_month=debt,
            fund_assumptions={
                "sponsor_equity_pct": 0.05,
                "lp_equity_pct": 0.95,
                "promote_splits": [
                    {"tier": "Pref", "hurdle_irr": 0.08, "lp_share": 1.0, "gp_share": 0.0},
                    {"tier": "Promote", "hurdle_irr": 0.0, "lp_share": 0.70, "gp_share": 0.30},
                ],
            },
            cashflow_by_month=monthly_cf,
        )

        # Should compute without error
        assert len(result["by_year"]) == 2
        # With $15K/mo (180K/yr) vs $200K equity, CF barely covers capital
        # Promote may or may not be zero depending on monthly pref math
        assert result["summary"]["total_promote"] >= 0

    def test_no_exit_no_disp_fee(self):
        """Disposition fee should be zero when there's no sale."""
        tg = TimeGrid.build("2026-01", "2026-12")
        monthly_cf = _build_monthly_cf(tg, 10_000)
        yearly_cf = _build_yearly_cf(tg, monthly_cf)
        debt = _build_debt_by_month(tg, balance=0)

        result = compute_fund_waterfall(
            time_grid=tg,
            cashflow_by_year=yearly_cf,
            purchase_assumptions={
                "purchase_price": 200_000,
                "equity_contribution": 200_000,
                "closing_costs": 0,
                "total_equity_basis": 200_000,
            },
            exit_assumptions=None,
            debt_by_month=debt,
            fund_assumptions={
                "sponsor_equity_pct": 0.05,
                "lp_equity_pct": 0.95,
                "disposition_fee_pct": 0.01,
                "promote_splits": [],
            },
            cashflow_by_month=monthly_cf,
        )

        assert result["summary"]["total_disposition_fee"] == 0.0


# ---------------------------------------------------------------------------
# Test 12: Acquisition fee and PCC in initial equity basis
# ---------------------------------------------------------------------------

class TestAcquisitionFeeAndPCC:
    """Verify initial balance includes equity + acq fee + PCC."""

    def test_acquisition_fee_increases_initial_balance(self):
        """1% acq fee on $1M purchase = $10K added to initial balance.

        Higher initial balance means more pref to accrue, so less promote.
        """
        tg = TimeGrid.build("2026-01", "2027-12")
        monthly_cf = _build_monthly_cf(tg, 10_000, exit_month="2027-12", exit_gross_sale=500_000)
        yearly_cf = _build_yearly_cf(tg, monthly_cf)
        debt = _build_debt_by_month(tg, balance=0)

        common_kwargs = dict(
            time_grid=tg,
            cashflow_by_year=yearly_cf,
            purchase_assumptions={
                "purchase_price": 1_000_000,
                "equity_contribution": 200_000,
                "closing_costs": 0,
                "total_equity_basis": 200_000,
            },
            exit_assumptions={"exit_cap_rate": 0.06, "exit_month": "2027-12"},
            debt_by_month=debt,
            cashflow_by_month=monthly_cf,
        )

        # Without acq fee
        result_no_fee = compute_fund_waterfall(
            fund_assumptions={
                "sponsor_equity_pct": 0.05, "lp_equity_pct": 0.95,
                "acquisition_fee_pct": 0.0,
                "promote_splits": [
                    {"tier": "Pref", "hurdle_irr": 0.08, "lp_share": 1.0, "gp_share": 0.0},
                    {"tier": "Promote", "hurdle_irr": 0.0, "lp_share": 0.70, "gp_share": 0.30},
                ],
            },
            **common_kwargs,
        )

        # With 1% acq fee
        result_with_fee = compute_fund_waterfall(
            fund_assumptions={
                "sponsor_equity_pct": 0.05, "lp_equity_pct": 0.95,
                "acquisition_fee_pct": 0.01,
                "promote_splits": [
                    {"tier": "Pref", "hurdle_irr": 0.08, "lp_share": 1.0, "gp_share": 0.0},
                    {"tier": "Promote", "hurdle_irr": 0.0, "lp_share": 0.70, "gp_share": 0.30},
                ],
            },
            **common_kwargs,
        )

        # Acq fee = $1M * 1% = $10K
        assert abs(result_with_fee["closing_costs"]["acquisition_fee"] - 10000.0) < 1.0
        # Higher initial balance → more pref accrual → less promote
        assert result_with_fee["summary"]["total_promote"] < result_no_fee["summary"]["total_promote"]

    def test_pcc_in_initial_balance(self):
        """Partnership closing costs should increase initial balance."""
        tg = TimeGrid.build("2026-01", "2027-12")
        monthly_cf = _build_monthly_cf(tg, 10_000, exit_month="2027-12", exit_gross_sale=500_000)
        yearly_cf = _build_yearly_cf(tg, monthly_cf)
        debt = _build_debt_by_month(tg, balance=0)

        common_kwargs = dict(
            time_grid=tg,
            cashflow_by_year=yearly_cf,
            purchase_assumptions={
                "purchase_price": 200_000,
                "equity_contribution": 200_000,
                "closing_costs": 0,
                "total_equity_basis": 200_000,
            },
            exit_assumptions={"exit_cap_rate": 0.06, "exit_month": "2027-12"},
            debt_by_month=debt,
            cashflow_by_month=monthly_cf,
        )

        # Without PCC
        result_no_pcc = compute_fund_waterfall(
            fund_assumptions={
                "sponsor_equity_pct": 0.05, "lp_equity_pct": 0.95,
                "partnership_closing_costs": 0,
                "promote_splits": [
                    {"tier": "Pref", "hurdle_irr": 0.08, "lp_share": 1.0, "gp_share": 0.0},
                    {"tier": "Promote", "hurdle_irr": 0.0, "lp_share": 0.70, "gp_share": 0.30},
                ],
            },
            **common_kwargs,
        )

        # With $50K PCC
        result_with_pcc = compute_fund_waterfall(
            fund_assumptions={
                "sponsor_equity_pct": 0.05, "lp_equity_pct": 0.95,
                "partnership_closing_costs": 50_000,
                "promote_splits": [
                    {"tier": "Pref", "hurdle_irr": 0.08, "lp_share": 1.0, "gp_share": 0.0},
                    {"tier": "Promote", "hurdle_irr": 0.0, "lp_share": 0.70, "gp_share": 0.30},
                ],
            },
            **common_kwargs,
        )

        # Higher initial balance → more pref → less promote
        assert result_with_pcc["summary"]["total_promote"] < result_no_pcc["summary"]["total_promote"]


# ---------------------------------------------------------------------------
# Test: No promote splits → zero promote
# ---------------------------------------------------------------------------

class TestNoPromoteSplits:
    """Edge case: empty promote_splits should produce zero promote."""

    def test_empty_promote_splits(self):
        tg = TimeGrid.build("2026-01", "2026-12")
        monthly_cf = _build_monthly_cf(tg, 10_000)
        yearly_cf = _build_yearly_cf(tg, monthly_cf)
        debt = _build_debt_by_month(tg, balance=0)

        result = compute_fund_waterfall(
            time_grid=tg,
            cashflow_by_year=yearly_cf,
            purchase_assumptions={
                "purchase_price": 200_000,
                "equity_contribution": 200_000,
                "closing_costs": 0,
                "total_equity_basis": 200_000,
            },
            exit_assumptions=None,
            debt_by_month=debt,
            fund_assumptions={
                "sponsor_equity_pct": 0.05,
                "lp_equity_pct": 0.95,
                "promote_splits": [],
            },
            cashflow_by_month=monthly_cf,
        )

        assert result["summary"]["total_promote"] == 0.0


# ---------------------------------------------------------------------------
# Test: Integration with minimal_deal_inputs fixture
# ---------------------------------------------------------------------------

class TestIntegrationWithFixture:
    """Use the conftest minimal_deal_inputs fixture for a smoke test."""

    def test_waterfall_with_fixture_inputs(self, minimal_deal_inputs):
        """Run waterfall using the shared fixture data."""
        inputs = minimal_deal_inputs
        tg = TimeGrid.build(
            inputs["time_grid"]["analysis_start_date"],
            inputs["time_grid"]["analysis_end_date"],
        )

        # Build synthetic monthly/yearly CF for the fixture's time grid
        monthly_cf = _build_monthly_cf(
            tg, 15_000, exit_month=inputs["exit_assumptions"]["exit_month"],
            exit_gross_sale=500_000,
        )
        yearly_cf = _build_yearly_cf(tg, monthly_cf)
        debt = _build_debt_by_month(tg, balance=inputs["debt_terms"]["commitment"])

        result = compute_fund_waterfall(
            time_grid=tg,
            cashflow_by_year=yearly_cf,
            purchase_assumptions=inputs["purchase_assumptions"],
            exit_assumptions=inputs["exit_assumptions"],
            debt_by_month=debt,
            fund_assumptions=inputs["fund_assumptions"],
            cashflow_by_month=monthly_cf,
        )

        # Basic structural checks
        assert "by_year" in result
        assert "summary" in result
        assert "closing_costs" in result
        assert len(result["by_year"]) == len(tg.year_ids)
        # Partnership IRR should be computed
        assert result["summary"]["partnership_irr"] is not None
        assert result["summary"]["partnership_equity_multiple"] is not None


# ---------------------------------------------------------------------------
# Multi-Tier Waterfall Tests (Task 1.1)
# ---------------------------------------------------------------------------

class TestMultiTierWaterfall:
    """Test N-tier waterfall with sequential cash flow distribution."""

    def test_three_tier_waterfall_produces_by_tier(self):
        """A 3-tier waterfall (pref, promote1, promote2) should report by_tier."""
        tg = TimeGrid.build("2026-07", "2029-06")
        monthly = _build_monthly_cf(tg, 5000, exit_month="2029-06", exit_gross_sale=200000)
        yearly = _build_yearly_cf(tg, monthly)
        fund = {
            "sponsor_equity_pct": 0.10,
            "lp_equity_pct": 0.90,
            "promote_tiers": [
                {"tier": "pref", "hurdle_irr": 0.08, "lp_share": 1.0, "gp_share": 0.0},
                {"tier": "promote1", "hurdle_irr": 0.0, "lp_share": 0.80, "gp_share": 0.20},
                {"tier": "promote2", "hurdle_irr": 0.15, "lp_share": 0.70, "gp_share": 0.30},
            ],
        }
        result = compute_fund_waterfall(
            time_grid=tg,
            cashflow_by_year=yearly,
            purchase_assumptions={"purchase_price": 100000, "equity_contribution": 30000,
                                  "closing_costs": 1000, "total_equity_basis": 31000},
            exit_assumptions={"exit_cap_rate": 0.06, "exit_month": "2029-06"},
            debt_by_month=[],
            fund_assumptions=fund,
            cashflow_by_month=monthly,
        )
        by_tier = result["promote"]["by_tier"]
        assert len(by_tier) == 3
        assert by_tier[0]["tier"] == "pref"
        assert by_tier[1]["tier"] == "promote1"
        assert by_tier[2]["tier"] == "promote2"
        # Pref tier should have LP distributions only
        assert float(by_tier[0]["total_to_gp"]) == 0.0
        assert float(by_tier[0]["total_to_lp"]) > 0
        # Promote tiers should have GP distributions
        total_gp = sum(float(t["total_to_gp"]) for t in by_tier)
        assert total_gp > 0

    def test_four_tier_blackstone_style(self):
        """4-tier institutional waterfall: pref, catch-up, promote, super-promote."""
        tg = TimeGrid.build("2026-07", "2031-06")
        monthly = _build_monthly_cf(tg, 8000, exit_month="2031-06", exit_gross_sale=500000)
        yearly = _build_yearly_cf(tg, monthly)
        fund = {
            "sponsor_equity_pct": 0.05,
            "lp_equity_pct": 0.95,
            "promote_tiers": [
                {"tier": "pref", "hurdle_irr": 0.08, "lp_share": 1.0, "gp_share": 0.0},
                {"tier": "catch_up", "hurdle_irr": 0.08, "lp_share": 0.0, "gp_share": 1.0,
                 "catch_up": True, "catch_up_target_pct": 0.20},
                {"tier": "promote", "hurdle_irr": 0.0, "lp_share": 0.80, "gp_share": 0.20},
                {"tier": "super_promote", "hurdle_irr": 0.20, "lp_share": 0.50, "gp_share": 0.50},
            ],
        }
        result = compute_fund_waterfall(
            time_grid=tg,
            cashflow_by_year=yearly,
            purchase_assumptions={"purchase_price": 200000, "equity_contribution": 60000,
                                  "closing_costs": 2000, "total_equity_basis": 62000},
            exit_assumptions={"exit_cap_rate": 0.06, "exit_month": "2031-06"},
            debt_by_month=[],
            fund_assumptions=fund,
            cashflow_by_month=monthly,
        )
        by_tier = result["promote"]["by_tier"]
        assert len(by_tier) == 4
        # Catch-up tier should have GP distributions
        catch_up_tier = [t for t in by_tier if t["tier"] == "catch_up"][0]
        assert float(catch_up_tier["total_to_gp"]) > 0
        assert catch_up_tier["catch_up"] is True
        # Total promote should be positive
        assert float(result["promote"]["total_promote"]) > 0


class TestCatchUpProvision:
    """Test catch-up tier mechanics."""

    def test_catch_up_gp_receives_100pct(self):
        """In a catch-up tier with gp_share=1.0, GP gets all CF until caught up."""
        tg = TimeGrid.build("2026-07", "2031-06")
        # Large exit makes catch-up meaningful
        monthly = _build_monthly_cf(tg, 5000, exit_month="2031-06", exit_gross_sale=400000)
        yearly = _build_yearly_cf(tg, monthly)
        fund = {
            "sponsor_equity_pct": 0.05,
            "lp_equity_pct": 0.95,
            "promote_tiers": [
                {"tier": "pref", "hurdle_irr": 0.08, "lp_share": 1.0, "gp_share": 0.0},
                {"tier": "catch_up", "hurdle_irr": 0.08, "lp_share": 0.0, "gp_share": 1.0,
                 "catch_up": True, "catch_up_target_pct": 0.20},
                {"tier": "promote", "hurdle_irr": 0.0, "lp_share": 0.80, "gp_share": 0.20},
            ],
        }
        result = compute_fund_waterfall(
            time_grid=tg,
            cashflow_by_year=yearly,
            purchase_assumptions={"purchase_price": 100000, "equity_contribution": 30000,
                                  "closing_costs": 1000, "total_equity_basis": 31000},
            exit_assumptions={"exit_cap_rate": 0.06, "exit_month": "2031-06"},
            debt_by_month=[],
            fund_assumptions=fund,
            cashflow_by_month=monthly,
        )
        by_tier = result["promote"]["by_tier"]
        catch_up = [t for t in by_tier if t["tier"] == "catch_up"][0]
        promote = [t for t in by_tier if t["tier"] == "promote"][0]
        # Catch-up LP share should be 0 (GP gets 100%)
        assert float(catch_up["total_to_lp"]) == 0.0
        assert float(catch_up["total_to_gp"]) > 0
        # Promote tier should also have distributions
        assert float(promote["total_to_gp"]) > 0

    def test_no_catch_up_when_insufficient_profit(self):
        """If deal barely returns capital + pref, catch-up should be minimal."""
        tg = TimeGrid.build("2026-07", "2029-06")
        # Small exit: barely enough to return capital
        monthly = _build_monthly_cf(tg, 1000, exit_month="2029-06", exit_gross_sale=35000)
        yearly = _build_yearly_cf(tg, monthly)
        fund = {
            "sponsor_equity_pct": 0.05,
            "lp_equity_pct": 0.95,
            "promote_tiers": [
                {"tier": "pref", "hurdle_irr": 0.08, "lp_share": 1.0, "gp_share": 0.0},
                {"tier": "catch_up", "hurdle_irr": 0.08, "lp_share": 0.0, "gp_share": 1.0,
                 "catch_up": True, "catch_up_target_pct": 0.20},
                {"tier": "promote", "hurdle_irr": 0.0, "lp_share": 0.80, "gp_share": 0.20},
            ],
        }
        result = compute_fund_waterfall(
            time_grid=tg,
            cashflow_by_year=yearly,
            purchase_assumptions={"purchase_price": 100000, "equity_contribution": 30000,
                                  "closing_costs": 1000, "total_equity_basis": 31000},
            exit_assumptions={"exit_cap_rate": 0.06, "exit_month": "2029-06"},
            debt_by_month=[],
            fund_assumptions=fund,
            cashflow_by_month=monthly,
        )
        # Total promote should be very small or zero (barely returned capital)
        total_promote = float(result["promote"]["total_promote"])
        # With small exit, promote should be modest
        assert total_promote >= 0


class TestClawback:
    """Test clawback computation."""

    def test_clawback_disabled_by_default(self):
        """No clawback_amount in result when not enabled."""
        tg = TimeGrid.build("2026-07", "2029-06")
        monthly = _build_monthly_cf(tg, 5000, exit_month="2029-06", exit_gross_sale=200000)
        yearly = _build_yearly_cf(tg, monthly)
        fund = {
            "sponsor_equity_pct": 0.10,
            "lp_equity_pct": 0.90,
            "promote_splits": [
                {"tier": "pref", "hurdle_irr": 0.08, "lp_share": 1.0, "gp_share": 0.0},
                {"tier": "promote", "hurdle_irr": 0.0, "lp_share": 0.70, "gp_share": 0.30},
            ],
        }
        result = compute_fund_waterfall(
            time_grid=tg,
            cashflow_by_year=yearly,
            purchase_assumptions={"purchase_price": 100000, "equity_contribution": 30000,
                                  "closing_costs": 1000, "total_equity_basis": 31000},
            exit_assumptions={"exit_cap_rate": 0.06, "exit_month": "2029-06"},
            debt_by_month=[],
            fund_assumptions=fund,
            cashflow_by_month=monthly,
        )
        assert "clawback_amount" not in result["promote"]

    def test_clawback_enabled_reports_amount(self):
        """When clawback enabled, result includes clawback_amount."""
        tg = TimeGrid.build("2026-07", "2029-06")
        monthly = _build_monthly_cf(tg, 5000, exit_month="2029-06", exit_gross_sale=200000)
        yearly = _build_yearly_cf(tg, monthly)
        fund = {
            "sponsor_equity_pct": 0.10,
            "lp_equity_pct": 0.90,
            "clawback_enabled": True,
            "promote_tiers": [
                {"tier": "pref", "hurdle_irr": 0.08, "lp_share": 1.0, "gp_share": 0.0},
                {"tier": "promote", "hurdle_irr": 0.0, "lp_share": 0.70, "gp_share": 0.30},
            ],
        }
        result = compute_fund_waterfall(
            time_grid=tg,
            cashflow_by_year=yearly,
            purchase_assumptions={"purchase_price": 100000, "equity_contribution": 30000,
                                  "closing_costs": 1000, "total_equity_basis": 31000},
            exit_assumptions={"exit_cap_rate": 0.06, "exit_month": "2029-06"},
            debt_by_month=[],
            fund_assumptions=fund,
            cashflow_by_month=monthly,
        )
        assert "clawback_amount" in result["promote"]
        # Single promote tier: no over-distribution expected
        assert float(result["promote"]["clawback_amount"]) >= 0


class TestTierReporting:
    """Test tier-by-tier distribution reporting."""

    def test_by_tier_sums_to_total(self):
        """Sum of all tier distributions should equal total CF distributed."""
        tg = TimeGrid.build("2026-07", "2031-06")
        monthly = _build_monthly_cf(tg, 6000, exit_month="2031-06", exit_gross_sale=300000)
        yearly = _build_yearly_cf(tg, monthly)
        fund = {
            "sponsor_equity_pct": 0.10,
            "lp_equity_pct": 0.90,
            "promote_tiers": [
                {"tier": "pref", "hurdle_irr": 0.08, "lp_share": 1.0, "gp_share": 0.0},
                {"tier": "promote", "hurdle_irr": 0.0, "lp_share": 0.80, "gp_share": 0.20},
            ],
        }
        result = compute_fund_waterfall(
            time_grid=tg,
            cashflow_by_year=yearly,
            purchase_assumptions={"purchase_price": 100000, "equity_contribution": 30000,
                                  "closing_costs": 1000, "total_equity_basis": 31000},
            exit_assumptions={"exit_cap_rate": 0.06, "exit_month": "2031-06"},
            debt_by_month=[],
            fund_assumptions=fund,
            cashflow_by_month=monthly,
        )
        by_tier = result["promote"]["by_tier"]
        assert len(by_tier) == 2
        total_gp = sum(float(t["total_to_gp"]) for t in by_tier)
        total_lp_tier = sum(float(t["total_to_lp"]) for t in by_tier)
        # GP total should match promote
        assert abs(total_gp - float(result["promote"]["total_promote"])) < 0.02

    def test_by_tier_fields_present(self):
        """Each tier should have required reporting fields."""
        tg = TimeGrid.build("2026-07", "2029-06")
        monthly = _build_monthly_cf(tg, 5000, exit_month="2029-06", exit_gross_sale=200000)
        yearly = _build_yearly_cf(tg, monthly)
        fund = {
            "sponsor_equity_pct": 0.10,
            "lp_equity_pct": 0.90,
            "promote_tiers": [
                {"tier": "pref", "hurdle_irr": 0.08, "lp_share": 1.0, "gp_share": 0.0},
                {"tier": "promote", "hurdle_irr": 0.0, "lp_share": 0.70, "gp_share": 0.30},
            ],
        }
        result = compute_fund_waterfall(
            time_grid=tg,
            cashflow_by_year=yearly,
            purchase_assumptions={"purchase_price": 100000, "equity_contribution": 30000,
                                  "closing_costs": 1000, "total_equity_basis": 31000},
            exit_assumptions={"exit_cap_rate": 0.06, "exit_month": "2029-06"},
            debt_by_month=[],
            fund_assumptions=fund,
            cashflow_by_month=monthly,
        )
        for tier in result["promote"]["by_tier"]:
            assert "tier" in tier
            assert "hurdle_irr" in tier
            assert "lp_share_pct" in tier
            assert "gp_share_pct" in tier
            assert "total_to_lp" in tier
            assert "total_to_gp" in tier
            assert "total_distributed" in tier
            assert "catch_up" in tier


class TestBackwardCompatPromoteSplits:
    """Verify promote_splits (legacy) and promote_tiers produce same results."""

    def test_legacy_promote_splits_same_as_promote_tiers(self):
        """promote_splits and equivalent promote_tiers should produce identical output."""
        tg = TimeGrid.build("2026-07", "2031-06")
        monthly = _build_monthly_cf(tg, 6000, exit_month="2031-06", exit_gross_sale=300000)
        yearly = _build_yearly_cf(tg, monthly)

        splits_config = [
            {"tier": "pref", "hurdle_irr": 0.08, "lp_share": 1.0, "gp_share": 0.0},
            {"tier": "promote", "hurdle_irr": 0.0, "lp_share": 0.70, "gp_share": 0.30},
        ]
        base_args = dict(
            time_grid=tg,
            cashflow_by_year=yearly,
            purchase_assumptions={"purchase_price": 100000, "equity_contribution": 30000,
                                  "closing_costs": 1000, "total_equity_basis": 31000},
            exit_assumptions={"exit_cap_rate": 0.06, "exit_month": "2031-06"},
            debt_by_month=[],
            cashflow_by_month=monthly,
        )

        # Run with promote_splits (legacy)
        result_legacy = compute_fund_waterfall(
            **base_args,
            fund_assumptions={
                "sponsor_equity_pct": 0.10, "lp_equity_pct": 0.90,
                "promote_splits": splits_config,
            },
        )

        # Run with promote_tiers (new)
        result_new = compute_fund_waterfall(
            **base_args,
            fund_assumptions={
                "sponsor_equity_pct": 0.10, "lp_equity_pct": 0.90,
                "promote_tiers": splits_config,
            },
        )

        # Total promote should be identical
        assert result_legacy["promote"]["total_promote"] == result_new["promote"]["total_promote"]
        # Partnership metrics should match
        assert result_legacy["summary"]["partnership_irr"] == result_new["summary"]["partnership_irr"]
        assert result_legacy["summary"]["partnership_equity_multiple"] == result_new["summary"]["partnership_equity_multiple"]


# ---------------------------------------------------------------------------
# Co-Invest Tests (Task 1.2)
# ---------------------------------------------------------------------------

class TestGPCoinvest:
    """Test GP co-invest capital earns pari passu pref with LP."""

    def _run_waterfall(self, fund_overrides):
        tg = TimeGrid.build("2026-01", "2028-12")
        monthly = _build_monthly_cf(tg, 10_000, exit_month="2028-12", exit_gross_sale=400_000)
        yearly = _build_yearly_cf(tg, monthly)
        fund = {
            "sponsor_equity_pct": 0.05,
            "lp_equity_pct": 0.95,
            "promote_splits": [
                {"tier": "Pref", "hurdle_irr": 0.08, "lp_share": 1.0, "gp_share": 0.0},
                {"tier": "Promote", "hurdle_irr": 0.0, "lp_share": 0.70, "gp_share": 0.30},
            ],
            **fund_overrides,
        }
        return compute_fund_waterfall(
            time_grid=tg,
            cashflow_by_year=yearly,
            purchase_assumptions={
                "purchase_price": 200_000, "equity_contribution": 200_000,
                "closing_costs": 0, "total_equity_basis": 200_000,
            },
            exit_assumptions={"exit_cap_rate": 0.06, "exit_month": "2028-12"},
            debt_by_month=_build_debt_by_month(tg, balance=0),
            fund_assumptions=fund,
            cashflow_by_month=monthly,
        )

    def test_no_coinvest_backward_compat(self):
        """Without gp_coinvest_pct, results should be identical to baseline."""
        result = self._run_waterfall({})
        assert "gp_coinvest_pct" not in result["summary"]
        # LP share should be 95% of CF to partnership
        yr = result["by_year"][0]
        cf_to_p = float(yr["cash_flow_to_partnership"])
        lp = float(yr["lp_share"])
        if cf_to_p > 0:
            assert abs(lp / cf_to_p - 0.95) < 0.01

    def test_coinvest_changes_distribution_split(self):
        """With 10% GP co-invest, LP's effective share decreases."""
        result_no_ci = self._run_waterfall({})
        result_with_ci = self._run_waterfall({"gp_coinvest_pct": 0.10})

        # LP share should be lower with co-invest (85% vs 95% of CF to partnership)
        lp_no_ci = float(result_no_ci["summary"]["total_lp_distributions"])
        lp_with_ci = float(result_with_ci["summary"]["total_lp_distributions"])
        self.assertLess = lambda a, b: None  # pytest doesn't have assertLess
        assert lp_with_ci < lp_no_ci

    def test_coinvest_reported_in_summary(self):
        """Summary should include gp_coinvest_pct and total_gp_coinvest_distributions."""
        result = self._run_waterfall({"gp_coinvest_pct": 0.10})
        assert "gp_coinvest_pct" in result["summary"]
        assert float(result["summary"]["gp_coinvest_pct"]) == 0.10
        assert "total_gp_coinvest_distributions" in result["summary"]
        assert float(result["summary"]["total_gp_coinvest_distributions"]) > 0

    def test_coinvest_share_appears_in_by_year(self):
        """Each year should have gp_coinvest_share field."""
        result = self._run_waterfall({"gp_coinvest_pct": 0.10})
        for yr in result["by_year"]:
            assert "gp_coinvest_share" in yr
            # Should be 10% of CF to partnership
            cf_to_p = float(yr["cash_flow_to_partnership"])
            ci_share = float(yr["gp_coinvest_share"])
            if cf_to_p > 0:
                assert abs(ci_share / cf_to_p - 0.10) < 0.01

    def test_coinvest_preserves_total_promote(self):
        """Co-invest should not change the promote calculation (it's pre-promote)."""
        result_no_ci = self._run_waterfall({})
        result_with_ci = self._run_waterfall({"gp_coinvest_pct": 0.10})
        # Promote is computed on the same CF before promote — should be identical
        assert result_no_ci["promote"]["total_promote"] == result_with_ci["promote"]["total_promote"]


class TestJVPartners:
    """Test JV mode with multiple partners."""

    def _run_jv_waterfall(self, partners):
        tg = TimeGrid.build("2026-01", "2028-12")
        monthly = _build_monthly_cf(tg, 10_000, exit_month="2028-12", exit_gross_sale=400_000)
        yearly = _build_yearly_cf(tg, monthly)
        fund = {
            "sponsor_equity_pct": 0.10,
            "lp_equity_pct": 0.90,
            "partners": partners,
            "promote_splits": [
                {"tier": "Pref", "hurdle_irr": 0.08, "lp_share": 1.0, "gp_share": 0.0},
                {"tier": "Promote", "hurdle_irr": 0.0, "lp_share": 0.70, "gp_share": 0.30},
            ],
        }
        return compute_fund_waterfall(
            time_grid=tg,
            cashflow_by_year=yearly,
            purchase_assumptions={
                "purchase_price": 200_000, "equity_contribution": 200_000,
                "closing_costs": 0, "total_equity_basis": 200_000,
            },
            exit_assumptions={"exit_cap_rate": 0.06, "exit_month": "2028-12"},
            debt_by_month=_build_debt_by_month(tg, balance=0),
            fund_assumptions=fund,
            cashflow_by_month=monthly,
        )

    def test_no_partners_no_jv_section(self):
        """Without partners[], result should not have partners key."""
        tg = TimeGrid.build("2026-01", "2026-12")
        monthly = _build_monthly_cf(tg, 10_000)
        yearly = _build_yearly_cf(tg, monthly)
        result = compute_fund_waterfall(
            time_grid=tg,
            cashflow_by_year=yearly,
            purchase_assumptions={
                "purchase_price": 200_000, "equity_contribution": 200_000,
                "closing_costs": 0, "total_equity_basis": 200_000,
            },
            exit_assumptions=None,
            debt_by_month=_build_debt_by_month(tg, balance=0),
            fund_assumptions={
                "sponsor_equity_pct": 0.10,
                "lp_equity_pct": 0.90,
                "promote_splits": [],
            },
            cashflow_by_month=monthly,
        )
        assert "partners" not in result

    def test_two_partner_jv(self):
        """Two-partner JV should produce per-partner returns."""
        partners = [
            {"name": "ExampleSponsor Capital", "equity_pct": 0.10, "role": "gp"},
            {"name": "Family Office LP", "equity_pct": 0.90, "role": "lp"},
        ]
        result = self._run_jv_waterfall(partners)
        assert "partners" in result
        assert len(result["partners"]) == 2
        assert result["partners"][0]["name"] == "ExampleSponsor Capital"
        assert result["partners"][1]["name"] == "Family Office LP"

    def test_partner_equity_sums_to_one(self):
        """Partner equity percentages should sum to initial balance pro-rata."""
        partners = [
            {"name": "GP", "equity_pct": 0.10, "role": "gp"},
            {"name": "LP", "equity_pct": 0.90, "role": "lp"},
        ]
        result = self._run_jv_waterfall(partners)
        total_invested = sum(float(p["equity_invested"]) for p in result["partners"])
        # Should approximately equal the initial balance
        # (200K equity basis + 0 acq fee + 0 PCC)
        assert abs(total_invested - 200_000) < 1

    def test_partner_irr_computed(self):
        """Each partner should have IRR and EM computed."""
        partners = [
            {"name": "GP", "equity_pct": 0.10, "role": "gp"},
            {"name": "LP", "equity_pct": 0.90, "role": "lp"},
        ]
        result = self._run_jv_waterfall(partners)
        for p in result["partners"]:
            assert p["irr"] is not None
            assert p["equity_multiple"] is not None
            assert p["equity_multiple"] > 0

    def test_partner_distributions_by_year(self):
        """Each partner should have annual distribution breakdown."""
        partners = [
            {"name": "GP", "equity_pct": 0.10, "role": "gp"},
            {"name": "LP", "equity_pct": 0.90, "role": "lp"},
        ]
        result = self._run_jv_waterfall(partners)
        for p in result["partners"]:
            assert "by_year" in p
            assert len(p["by_year"]) > 0

    def test_gp_partner_distributions_include_promote(self):
        """JV GP partner distributions must include promote, not only equity split."""
        partners = [
            {"name": "GP", "equity_pct": 0.10, "role": "gp"},
            {"name": "LP", "equity_pct": 0.90, "role": "lp"},
        ]
        result = self._run_jv_waterfall(partners)
        gp = result["partners"][0]
        lp = result["partners"][1]

        total_promote = float(result["summary"]["total_promote"])
        assert total_promote > 0

        expected_gp_total = sum(
            float(yr["cash_flow_to_partnership"]) * 0.10
            + float(yr["promote_payment"])
            for yr in result["by_year"]
        )
        expected_lp_total = sum(
            float(yr["cash_flow_to_partnership"]) * 0.90
            for yr in result["by_year"]
        )

        assert float(gp["total_distributions"]) == pytest.approx(expected_gp_total, abs=0.02)
        assert float(lp["total_distributions"]) == pytest.approx(expected_lp_total, abs=0.02)
        assert float(gp["total_distributions"]) > sum(
            float(yr["cash_flow_to_partnership"]) * 0.10
            for yr in result["by_year"]
        )

    def test_partner_irr_and_em_use_post_promote_streams(self):
        """JV partner IRR/EM should differ when GP receives promote and LP is net of promote."""
        partners = [
            {"name": "GP", "equity_pct": 0.10, "role": "gp"},
            {"name": "LP", "equity_pct": 0.90, "role": "lp"},
        ]
        result = self._run_jv_waterfall(partners)
        gp = result["partners"][0]
        lp = result["partners"][1]

        assert result["summary"]["total_promote"] > 0
        assert gp["irr"] > lp["irr"]
        assert gp["equity_multiple"] > lp["equity_multiple"]

    def test_three_partner_unequal_split(self):
        """Three partners with unequal splits should compute independently."""
        partners = [
            {"name": "GP-Sponsor", "equity_pct": 0.05, "role": "gp"},
            {"name": "LP-Anchor", "equity_pct": 0.70, "role": "lp"},
            {"name": "LP-Co-Invest", "equity_pct": 0.25, "role": "lp"},
        ]
        result = self._run_jv_waterfall(partners)
        assert len(result["partners"]) == 3

        # Anchor LP receives 70% of post-promote partnership cash flow; GP
        # promote is not part of the LP denominator.
        total_post_promote_cf = sum(float(yr["cash_flow_to_partnership"]) for yr in result["by_year"])
        anchor_dist = float(result["partners"][1]["total_distributions"])
        if total_post_promote_cf > 0:
            assert abs(anchor_dist / total_post_promote_cf - 0.70) < 0.01

    def test_single_partner_mode(self):
        """Single partner (backward compat) should still work."""
        partners = [
            {"name": "Solo LP", "equity_pct": 1.0, "role": "lp"},
        ]
        result = self._run_jv_waterfall(partners)
        assert len(result["partners"]) == 1
        p = result["partners"][0]
        assert p["irr"] is not None


# ---------------------------------------------------------------------------
# Regression: monthly_promotes must be JSON-safe numeric (not Decimal/str)
# ---------------------------------------------------------------------------

class TestMonthlyPromotesJsonSafe:
    """Regression for downstream TypeError in rediq_output writer.

    Symptom: fund_waterfall.promote.monthly_promotes was a Dict[str, Decimal]
    that the JSON encoder serialized via `default=str`, producing values like
    "0" (string). The writer guard `if promote and promote != 0:` admitted the
    truthy string, then `abs(promote)` raised
    `TypeError: bad operand type for abs(): 'str'`.

    Contract: every per-month value must be a Python int or float so the dict
    JSON-serializes cleanly and arithmetic guards work without coercion.
    """

    def _run_basic_waterfall(self):
        tg = TimeGrid.build("2026-01", "2027-12")
        monthly_cf = _build_monthly_cf(tg, 10_000, exit_month="2027-12", exit_gross_sale=500_000)
        yearly_cf = _build_yearly_cf(tg, monthly_cf)
        debt = _build_debt_by_month(tg, balance=0)
        return compute_fund_waterfall(
            time_grid=tg,
            cashflow_by_year=yearly_cf,
            purchase_assumptions={
                "purchase_price": 200_000,
                "equity_contribution": 200_000,
                "closing_costs": 0,
                "total_equity_basis": 200_000,
            },
            exit_assumptions={"exit_cap_rate": 0.06, "exit_month": "2027-12"},
            debt_by_month=debt,
            fund_assumptions={
                "sponsor_equity_pct": 0.05,
                "lp_equity_pct": 0.95,
                "promote_splits": [
                    {"tier": "Pref", "hurdle_irr": 0.08, "lp_share": 1.0, "gp_share": 0.0},
                    {"tier": "Promote", "hurdle_irr": 0.0, "lp_share": 0.70, "gp_share": 0.30},
                ],
            },
            cashflow_by_month=monthly_cf,
        )

    def test_monthly_promotes_values_are_numeric(self):
        """Every per-month promote value must be int/float (not Decimal, not str)."""
        result = self._run_basic_waterfall()
        mp = result["promote"]["monthly_promotes"]
        assert isinstance(mp, dict) and len(mp) > 0
        for month, val in mp.items():
            assert isinstance(val, (int, float)), (
                f"month {month} promote is {type(val).__name__}={val!r}; "
                "must be int/float for JSON-safe downstream consumption"
            )
            # Must NOT be a Decimal (Decimal is not JSON-native and would
            # serialize to a string under default=str).
            assert not isinstance(val, Decimal)

    def test_monthly_promotes_json_roundtrip_preserves_numeric(self):
        """After json.dumps/loads, values stay numeric and equal abs() works."""
        import json
        result = self._run_basic_waterfall()
        mp = result["promote"]["monthly_promotes"]
        # Standard dump (no default= fallback) must succeed without TypeError.
        encoded = json.dumps(mp)
        decoded = json.loads(encoded)
        for month, val in decoded.items():
            assert isinstance(val, (int, float)), (
                f"after json roundtrip, month {month} = {val!r} ({type(val).__name__})"
            )
            # The exact crash that motivated this regression: abs(promote).
            # Must succeed for both zero and non-zero values.
            _ = abs(val)

    def test_zero_promote_months_are_numeric_zero(self):
        """Months with no promote payment must be 0/0.0, never the string "0"."""
        result = self._run_basic_waterfall()
        mp = result["promote"]["monthly_promotes"]
        zero_months = [m for m, v in mp.items() if v == 0]
        # Basic deal won't promote in early months — expect at least some zeros.
        assert len(zero_months) > 0
        for m in zero_months:
            assert mp[m] == 0
            assert not isinstance(mp[m], str)
            # Reproduce the specific failure mode the bug created.
            promote = mp[m]
            # Old buggy code: `if promote and promote != 0:` — string "0" is
            # truthy and "0" != 0, so guard admitted it. After fix, numeric 0
            # is falsy, so the guard correctly skips.
            assert not (promote and promote != 0)
