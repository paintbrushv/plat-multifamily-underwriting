"""
Tests for Rate Cap Expiry and Prepayment Penalties (Task 1.5).

Tests cover:
- Rate cap expiry timing (cap active before, removed after)
- Renewal cap (replaced with higher cap after expiry)
- Cap renewal cost injection
- Defeasance penalty calculation
- Yield maintenance penalty calculation
- Percentage prepayment penalty
- No-penalty backward compat
- Schema validation
"""
import json
import unittest
from decimal import Decimal
from pathlib import Path

from engine.modules.debt import (
    compute_debt,
    compute_prepayment_penalty,
    _get_effective_rate,
)
from engine.modules.time_grid import TimeGrid
from engine.modules.util import dec, month_id


def _tg(start="2026-01", end="2028-12"):
    return TimeGrid.build(start, end)


class TestRateCapExpiry(unittest.TestCase):
    """Test rate cap expiry in variable-rate loans."""

    def _variable_terms(self, cap_expiry=None, renewal_rate=None, renewal_cost=0):
        """Build variable-rate loan terms with optional cap expiry."""
        terms = {
            "commitment": 10_000_000,
            "rate": 0.05,
            "amort_years": 30,
            "io_months": 36,
            "rate_type": "variable",
            "base_spread": 0.02,
            "rate_cap": 0.06,
            "rate_curve": [
                {"start_month": "2026-01", "rate": 0.04},
                {"start_month": "2027-07", "rate": 0.07},  # Rate shock above cap
            ],
        }
        if cap_expiry:
            terms["rate_cap_expiry_month"] = cap_expiry
        if renewal_rate is not None:
            terms["rate_cap_renewal_rate"] = renewal_rate
        if renewal_cost:
            terms["rate_cap_renewal_cost"] = renewal_cost
        return terms

    def test_cap_active_before_expiry(self):
        """Before cap expiry, rate is capped normally."""
        tg = _tg()
        terms = self._variable_terms(cap_expiry="2027-06")
        result = compute_debt(tg, terms)

        # 2026-01: SOFR 4% + 2% spread = 6%, capped at 6% → $50K/mo IO
        jan_26 = result["by_month"][0]
        expected_io = 10_000_000 * 0.06 / 12  # $50K
        self.assertAlmostEqual(jan_26["interest_expense"], expected_io, delta=10)

    def test_cap_removed_after_expiry(self):
        """After cap expiry with no renewal, rate floats uncapped."""
        tg = _tg()
        terms = self._variable_terms(cap_expiry="2027-06")
        result = compute_debt(tg, terms)

        # 2027-07: SOFR jumps to 7% + 2% spread = 9%, no cap → $75K/mo
        # Month index for 2027-07 is month 18
        jul_27 = [m for m in result["by_month"] if m["month"] == "2027-07"][0]
        expected_io = 10_000_000 * 0.09 / 12  # $75K
        self.assertAlmostEqual(jul_27["interest_expense"], expected_io, delta=10)

    def test_cap_replaced_with_renewal(self):
        """After cap expiry, renewal cap limits the rate."""
        tg = _tg()
        terms = self._variable_terms(cap_expiry="2027-06", renewal_rate=0.08)
        result = compute_debt(tg, terms)

        # 2027-07: SOFR 7% + 2% = 9%, renewal cap 8% → capped at 8%
        jul_27 = [m for m in result["by_month"] if m["month"] == "2027-07"][0]
        expected_io = 10_000_000 * 0.08 / 12  # $66,667
        self.assertAlmostEqual(jul_27["interest_expense"], expected_io, delta=10)

    def test_renewal_cost_injected(self):
        """Cap renewal cost appears at expiry month."""
        tg = _tg()
        terms = self._variable_terms(cap_expiry="2027-06", renewal_cost=50_000)
        result = compute_debt(tg, terms)

        jun_27 = [m for m in result["by_month"] if m["month"] == "2027-06"][0]
        self.assertAlmostEqual(jun_27.get("rate_cap_renewal_cost", 0), 50_000, places=0)

    def test_no_expiry_backward_compat(self):
        """Without rate_cap_expiry_month, cap applies for full term."""
        tg = _tg()
        terms = self._variable_terms()  # No expiry
        result = compute_debt(tg, terms)

        # 2027-07: SOFR 7% + 2% = 9%, but cap at 6% → $50K/mo
        jul_27 = [m for m in result["by_month"] if m["month"] == "2027-07"][0]
        expected_io = 10_000_000 * 0.06 / 12
        self.assertAlmostEqual(jul_27["interest_expense"], expected_io, delta=10)


class TestPrepaymentPenalty(unittest.TestCase):
    """Test prepayment penalty calculations."""

    def test_no_penalty(self):
        """No penalty when type is 'none'."""
        penalty = compute_prepayment_penalty(
            Decimal("5000000"), "none", [], Decimal("0.04"))
        self.assertEqual(penalty, Decimal("0"))

    def test_percentage_penalty(self):
        """Percentage penalty = balance × penalty_pct."""
        penalty = compute_prepayment_penalty(
            Decimal("5000000"), "percentage", [],
            Decimal("0.04"), Decimal("0.02"))
        self.assertEqual(penalty, Decimal("100000"))

    def test_defeasance_penalty(self):
        """Defeasance: PV(remaining DS at Treasury) - balance > 0."""
        # Remaining 12 monthly payments of $50K each
        remaining_ds = [Decimal("50000")] * 12
        penalty = compute_prepayment_penalty(
            Decimal("500000"), "defeasance", remaining_ds,
            Decimal("0.03"))  # 3% Treasury

        # PV of $50K × 12 at 0.25%/mo should be > $500K (since loan rate > treasury)
        # So defeasance cost > 0
        self.assertGreater(penalty, Decimal("0"))

    def test_yield_maintenance_penalty(self):
        """Yield maintenance: same formula as defeasance."""
        remaining_ds = [Decimal("50000")] * 24
        penalty = compute_prepayment_penalty(
            Decimal("800000"), "yield_maintenance", remaining_ds,
            Decimal("0.03"))
        self.assertGreater(penalty, Decimal("0"))

    def test_defeasance_zero_when_no_remaining(self):
        """Defeasance with no remaining payments = $0."""
        penalty = compute_prepayment_penalty(
            Decimal("500000"), "defeasance", [], Decimal("0.03"))
        self.assertEqual(penalty, Decimal("0"))

    def test_prepayment_metadata_in_result(self):
        """compute_debt includes prepayment metadata when configured."""
        tg = _tg()
        terms = {
            "commitment": 5_000_000,
            "rate": 0.055,
            "amort_years": 30,
            "io_months": 12,
            "prepayment_type": "defeasance",
            "treasury_rate": 0.035,
        }
        result = compute_debt(tg, terms)
        self.assertIn("prepayment", result)
        self.assertEqual(result["prepayment"]["type"], "defeasance")

    def test_no_prepayment_metadata_when_none(self):
        """compute_debt omits prepayment when type is 'none'."""
        tg = _tg()
        terms = {
            "commitment": 5_000_000,
            "rate": 0.055,
            "amort_years": 30,
        }
        result = compute_debt(tg, terms)
        self.assertNotIn("prepayment", result)


class TestGetEffectiveRate(unittest.TestCase):
    """Test _get_effective_rate with cap expiry."""

    def test_cap_active(self):
        """Before expiry: cap applies."""
        rate = _get_effective_rate(
            "2026-06", "variable", dec("0.05"),
            {}, [{"start_month": "2026-01", "rate": 0.08}],
            dec("0.02"), dec("0.06"), None,
            "2027-01", None,
        )
        # 8% + 2% = 10%, capped at 6%
        self.assertAlmostEqual(float(rate), 0.06, places=4)

    def test_cap_expired_no_renewal(self):
        """After expiry with no renewal: uncapped."""
        rate = _get_effective_rate(
            "2027-02", "variable", dec("0.05"),
            {}, [{"start_month": "2026-01", "rate": 0.08}],
            dec("0.02"), dec("0.06"), None,
            "2027-01", None,
        )
        # 8% + 2% = 10%, no cap
        self.assertAlmostEqual(float(rate), 0.10, places=4)

    def test_cap_expired_with_renewal(self):
        """After expiry with renewal cap: new cap applies."""
        rate = _get_effective_rate(
            "2027-02", "variable", dec("0.05"),
            {}, [{"start_month": "2026-01", "rate": 0.08}],
            dec("0.02"), dec("0.06"), None,
            "2027-01", dec("0.09"),
        )
        # 8% + 2% = 10%, renewal cap 9%
        self.assertAlmostEqual(float(rate), 0.09, places=4)


class TestSchemaFields(unittest.TestCase):
    """Verify schema includes rate cap expiry and prepayment fields."""

    def test_debt_terms_has_new_fields(self):
        schema = json.loads(Path("engine/schemas/deal_schema_v0_1.json").read_text())
        props = schema["$defs"]["debt_terms"]["properties"]
        self.assertIn("rate_cap_expiry_month", props)
        self.assertIn("rate_cap_renewal_cost", props)
        self.assertIn("rate_cap_renewal_rate", props)
        self.assertIn("prepayment_type", props)
        self.assertIn("prepayment_lockout_months", props)
        self.assertIn("prepayment_penalty_pct", props)
        self.assertIn("treasury_rate", props)

    def test_prepayment_type_enum(self):
        schema = json.loads(Path("engine/schemas/deal_schema_v0_1.json").read_text())
        pt = schema["$defs"]["debt_terms"]["properties"]["prepayment_type"]
        self.assertEqual(set(pt["enum"]), {"none", "defeasance", "yield_maintenance", "percentage"})


if __name__ == "__main__":
    unittest.main()
