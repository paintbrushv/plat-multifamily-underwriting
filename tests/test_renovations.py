"""
Tests for Renovations Module

Tests cover:
- Basic renovation scheduling
- On-turnover vs proactive strategies
- Downtime vacancy loss calculation
- Premium revenue generation
- ROI and payback calculations
- Multiple programs
- Edge cases (no units, program outside analysis period)
"""
import unittest

from engine.modules.renovations import compute_renovations
from engine.modules.time_grid import TimeGrid


class TestRenovationsBasic(unittest.TestCase):
    """Test basic renovation functionality."""

    def test_single_program_basic(self):
        """Basic renovation program should track units and costs."""
        time_grid = TimeGrid.build("2026-01", "2026-06")
        unit_cohorts = [
            {"cohort_id": "1B_classic", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}
        ]
        renovation_programs = [
            {
                "program_id": "upgrade_1b",
                "program_name": "1BR Upgrade",
                "target_cohort": "1B_classic",
                "output_cohort": "1B_renovated",
                "renovation_cost_per_unit": 15000,
                "rent_premium_monthly": 200,
                "downtime_days": 30,
                "strategy": "proactive",
                "start_month": "2026-01",
                "end_month": "2026-06",
                "monthly_pace": 5,
            }
        ]

        result = compute_renovations(time_grid, unit_cohorts, renovation_programs)

        # Should renovate 5 units per month for 6 months = 30 units
        self.assertEqual(result["summary"]["total_units_renovated"], 30)
        # Cost = 30 * 15000 = 450000
        self.assertAlmostEqual(result["summary"]["total_renovation_cost"], 450000.0, places=2)

    def test_renovation_cost_per_month(self):
        """Monthly renovation cost should equal units * cost_per_unit."""
        time_grid = TimeGrid.build("2026-01", "2026-01")
        unit_cohorts = [
            {"cohort_id": "1B_classic", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}
        ]
        renovation_programs = [
            {
                "program_id": "upgrade_1b",
                "program_name": "1BR Upgrade",
                "target_cohort": "1B_classic",
                "output_cohort": "1B_renovated",
                "renovation_cost_per_unit": 20000,
                "rent_premium_monthly": 250,
                "downtime_days": 30,
                "strategy": "proactive",
                "start_month": "2026-01",
                "monthly_pace": 10,
            }
        ]

        result = compute_renovations(time_grid, unit_cohorts, renovation_programs)

        # Month 1: 10 units * $20000 = $200000
        self.assertAlmostEqual(result["by_month"][0]["renovation_cost"], 200000.0, places=2)


class TestRenovationsStrategy(unittest.TestCase):
    """Test different renovation strategies."""

    def test_proactive_strategy(self):
        """Proactive strategy should renovate at full pace."""
        time_grid = TimeGrid.build("2026-01", "2026-03")
        unit_cohorts = [
            {"cohort_id": "1B_classic", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}
        ]
        renovation_programs = [
            {
                "program_id": "upgrade_1b",
                "target_cohort": "1B_classic",
                "output_cohort": "1B_renovated",
                "renovation_cost_per_unit": 15000,
                "rent_premium_monthly": 200,
                "downtime_days": 30,
                "strategy": "proactive",
                "start_month": "2026-01",
                "monthly_pace": 10,
            }
        ]

        result = compute_renovations(time_grid, unit_cohorts, renovation_programs)

        # Proactive: should hit full pace each month
        self.assertEqual(result["by_month"][0]["units_renovated"], 10)
        self.assertEqual(result["by_month"][1]["units_renovated"], 10)
        self.assertEqual(result["by_month"][2]["units_renovated"], 10)
        self.assertEqual(result["summary"]["total_units_renovated"], 30)

    def test_on_turnover_strategy(self):
        """On-turnover strategy should be limited by turnover rate."""
        time_grid = TimeGrid.build("2026-01", "2026-03")
        unit_cohorts = [
            {"cohort_id": "1B_classic", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}
        ]
        renovation_programs = [
            {
                "program_id": "upgrade_1b",
                "target_cohort": "1B_classic",
                "output_cohort": "1B_renovated",
                "renovation_cost_per_unit": 15000,
                "rent_premium_monthly": 200,
                "downtime_days": 30,
                "strategy": "on_turnover",
                "start_month": "2026-01",
                "monthly_pace": 20,  # High pace - should be limited by turnover
            }
        ]

        # 50% annual turnover = ~4.17 units/month for 100 units
        result = compute_renovations(time_grid, unit_cohorts, renovation_programs, turnover_rate=0.50)

        # Should be limited by turnover, not pace
        # With 100 units and 50% annual turnover, expect ~4 units/month
        self.assertLess(result["by_month"][0]["units_renovated"], 20)
        self.assertGreater(result["by_month"][0]["units_renovated"], 0)
    def test_on_turnover_tail_does_not_force_minimum_unit_without_explicit_floor(self):
        """Small tails should not renovate faster than natural turnover unless configured."""
        time_grid = TimeGrid.build("2026-01", "2026-01")
        unit_cohorts = [
            {"cohort_id": "1B_classic", "unit_type": "1BR", "unit_count": 10, "initial_inplace_rent": 1000}
        ]
        renovation_programs = [
            {
                "program_id": "upgrade_1b",
                "target_cohort": "1B_classic",
                "output_cohort": "1B_renovated",
                "renovation_cost_per_unit": 15000,
                "rent_premium_monthly": 200,
                "downtime_days": 30,
                "strategy": "on_turnover",
                "start_month": "2026-01",
                "monthly_pace": 10,
            }
        ]

        result = compute_renovations(time_grid, unit_cohorts, renovation_programs, turnover_rate=0.50)

        self.assertEqual(result["by_month"][0]["units_renovated"], 0)

    def test_on_turnover_explicit_min_monthly_pace_preserves_forced_tail_pace(self):
        """Analysts can opt into a guaranteed minimum monthly pace explicitly."""
        time_grid = TimeGrid.build("2026-01", "2026-01")
        unit_cohorts = [
            {"cohort_id": "1B_classic", "unit_type": "1BR", "unit_count": 10, "initial_inplace_rent": 1000}
        ]
        renovation_programs = [
            {
                "program_id": "upgrade_1b",
                "target_cohort": "1B_classic",
                "output_cohort": "1B_renovated",
                "renovation_cost_per_unit": 15000,
                "rent_premium_monthly": 200,
                "downtime_days": 30,
                "strategy": "on_turnover",
                "start_month": "2026-01",
                "monthly_pace": 10,
                "min_monthly_pace": 1,
            }
        ]

        result = compute_renovations(time_grid, unit_cohorts, renovation_programs, turnover_rate=0.50)

        self.assertEqual(result["by_month"][0]["units_renovated"], 1)


class TestRenovationsDowntime(unittest.TestCase):
    """Test downtime vacancy loss calculation."""

    def test_downtime_vacancy_loss(self):
        """Downtime should calculate vacancy loss correctly."""
        time_grid = TimeGrid.build("2026-01", "2026-01")
        unit_cohorts = [
            {"cohort_id": "1B_classic", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1500}
        ]
        renovation_programs = [
            {
                "program_id": "upgrade_1b",
                "target_cohort": "1B_classic",
                "output_cohort": "1B_renovated",
                "renovation_cost_per_unit": 15000,
                "rent_premium_monthly": 200,
                "downtime_days": 30,
                "strategy": "proactive",
                "start_month": "2026-01",
                "monthly_pace": 10,
            }
        ]

        result = compute_renovations(time_grid, unit_cohorts, renovation_programs)

        # 10 units * 30 days = 300 downtime days
        self.assertEqual(result["by_month"][0]["downtime_vacancy_days"], 300)

        # Vacancy loss = 10 units * (30/30) months * $1500/month = $15000
        self.assertAlmostEqual(result["by_month"][0]["downtime_vacancy_loss"], 15000.0, places=2)

    def test_longer_downtime(self):
        """Longer downtime should increase vacancy loss."""
        time_grid = TimeGrid.build("2026-01", "2026-01")
        unit_cohorts = [
            {"cohort_id": "1B_classic", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1500}
        ]
        renovation_programs = [
            {
                "program_id": "upgrade_1b",
                "target_cohort": "1B_classic",
                "output_cohort": "1B_renovated",
                "renovation_cost_per_unit": 15000,
                "rent_premium_monthly": 200,
                "downtime_days": 45,  # 45 days instead of 30
                "strategy": "proactive",
                "start_month": "2026-01",
                "monthly_pace": 10,
            }
        ]

        result = compute_renovations(time_grid, unit_cohorts, renovation_programs)

        # 10 units * 45 days = 450 downtime days
        self.assertEqual(result["by_month"][0]["downtime_vacancy_days"], 450)

        # Vacancy loss = 10 units * (45/30) months * $1500/month = $22500
        self.assertAlmostEqual(result["by_month"][0]["downtime_vacancy_loss"], 22500.0, places=2)


class TestRenovationsPremium(unittest.TestCase):
    """Test premium revenue generation."""

    def test_premium_starts_after_renovation(self):
        """Premium should start the month after renovation completes."""
        time_grid = TimeGrid.build("2026-01", "2026-03")
        unit_cohorts = [
            {"cohort_id": "1B_classic", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}
        ]
        renovation_programs = [
            {
                "program_id": "upgrade_1b",
                "target_cohort": "1B_classic",
                "output_cohort": "1B_renovated",
                "renovation_cost_per_unit": 15000,
                "rent_premium_monthly": 200,
                "downtime_days": 30,
                "strategy": "proactive",
                "start_month": "2026-01",
                "monthly_pace": 10,
            }
        ]

        result = compute_renovations(time_grid, unit_cohorts, renovation_programs)

        # Month 1: 10 units renovated, 0 premium (nothing completed yet)
        self.assertAlmostEqual(result["by_month"][0]["monthly_premium_revenue"], 0.0, places=2)

        # Month 2: 10 more units renovated, 10 units generating premium from month 1
        # 10 units * $200 = $2000
        self.assertAlmostEqual(result["by_month"][1]["monthly_premium_revenue"], 2000.0, places=2)

        # Month 3: 10 more units renovated, 20 units generating premium
        # 20 units * $200 = $4000
        self.assertAlmostEqual(result["by_month"][2]["monthly_premium_revenue"], 4000.0, places=2)

    def test_cumulative_premium(self):
        """Cumulative premium should sum all months."""
        time_grid = TimeGrid.build("2026-01", "2026-03")
        unit_cohorts = [
            {"cohort_id": "1B_classic", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}
        ]
        renovation_programs = [
            {
                "program_id": "upgrade_1b",
                "target_cohort": "1B_classic",
                "output_cohort": "1B_renovated",
                "renovation_cost_per_unit": 15000,
                "rent_premium_monthly": 200,
                "downtime_days": 30,
                "strategy": "proactive",
                "start_month": "2026-01",
                "monthly_pace": 10,
            }
        ]

        result = compute_renovations(time_grid, unit_cohorts, renovation_programs)

        # Cumulative: 0 + 2000 + 4000 = 6000
        self.assertAlmostEqual(result["by_month"][2]["cumulative_premium_revenue"], 6000.0, places=2)


class TestRenovationsROI(unittest.TestCase):
    """Test ROI and payback calculations."""

    def test_simple_roi(self):
        """ROI should be annual premium / renovation cost."""
        time_grid = TimeGrid.build("2026-01", "2026-12")
        unit_cohorts = [
            {"cohort_id": "1B_classic", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}
        ]
        renovation_programs = [
            {
                "program_id": "upgrade_1b",
                "program_name": "1BR Upgrade",
                "target_cohort": "1B_classic",
                "output_cohort": "1B_renovated",
                "renovation_cost_per_unit": 15000,
                "rent_premium_monthly": 200,
                "downtime_days": 30,
                "strategy": "proactive",
                "start_month": "2026-01",
                "end_month": "2026-12",
                "monthly_pace": 5,
            }
        ]

        result = compute_renovations(time_grid, unit_cohorts, renovation_programs)

        # 60 units renovated (5/month * 12 months)
        # Annual premium at completion = 60 * $200 * 12 = $144,000
        # Total cost = 60 * $15000 = $900,000
        # ROI = 144000 / 900000 = 0.16 = 16%
        program = result["by_program"][0]
        self.assertAlmostEqual(program["simple_roi"], 0.16, places=2)

    def test_payback_period(self):
        """Payback should be cost / monthly premium."""
        time_grid = TimeGrid.build("2026-01", "2026-01")
        unit_cohorts = [
            {"cohort_id": "1B_classic", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}
        ]
        renovation_programs = [
            {
                "program_id": "upgrade_1b",
                "program_name": "1BR Upgrade",
                "target_cohort": "1B_classic",
                "output_cohort": "1B_renovated",
                "renovation_cost_per_unit": 15000,
                "rent_premium_monthly": 200,
                "downtime_days": 30,
                "strategy": "proactive",
                "start_month": "2026-01",
                "monthly_pace": 10,
            }
        ]

        result = compute_renovations(time_grid, unit_cohorts, renovation_programs)

        # Payback = $15000 / $200 = 75 months
        program = result["by_program"][0]
        self.assertAlmostEqual(program["payback_months"], 75.0, places=1)


class TestRenovationsMultiplePrograms(unittest.TestCase):
    """Test multiple renovation programs."""

    def test_two_programs_aggregate(self):
        """Multiple programs should aggregate correctly."""
        time_grid = TimeGrid.build("2026-01", "2026-03")
        unit_cohorts = [
            {"cohort_id": "1B_classic", "unit_type": "1BR", "unit_count": 50, "initial_inplace_rent": 1000},
            {"cohort_id": "2B_classic", "unit_type": "2BR", "unit_count": 50, "initial_inplace_rent": 1500},
        ]
        renovation_programs = [
            {
                "program_id": "upgrade_1b",
                "program_name": "1BR Upgrade",
                "target_cohort": "1B_classic",
                "output_cohort": "1B_renovated",
                "renovation_cost_per_unit": 10000,
                "rent_premium_monthly": 150,
                "downtime_days": 30,
                "strategy": "proactive",
                "start_month": "2026-01",
                "monthly_pace": 5,
            },
            {
                "program_id": "upgrade_2b",
                "program_name": "2BR Upgrade",
                "target_cohort": "2B_classic",
                "output_cohort": "2B_renovated",
                "renovation_cost_per_unit": 15000,
                "rent_premium_monthly": 200,
                "downtime_days": 30,
                "strategy": "proactive",
                "start_month": "2026-01",
                "monthly_pace": 5,
            },
        ]

        result = compute_renovations(time_grid, unit_cohorts, renovation_programs)

        # Total units: 5+5 = 10/month * 3 months = 30 total
        self.assertEqual(result["summary"]["total_units_renovated"], 30)

        # Total cost: 15 * 10000 + 15 * 15000 = 150000 + 225000 = 375000
        self.assertAlmostEqual(result["summary"]["total_renovation_cost"], 375000.0, places=2)


class TestRenovationsEdgeCases(unittest.TestCase):
    """Test edge cases and error handling."""

    def test_empty_programs(self):
        """Empty programs list should return zeros."""
        time_grid = TimeGrid.build("2026-01", "2026-03")
        unit_cohorts = [
            {"cohort_id": "1B_classic", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}
        ]
        renovation_programs = []

        result = compute_renovations(time_grid, unit_cohorts, renovation_programs)

        self.assertEqual(result["summary"]["total_units_renovated"], 0)
        self.assertAlmostEqual(result["summary"]["total_renovation_cost"], 0.0, places=2)
        self.assertEqual(len(result["by_month"]), 3)

    def test_program_before_analysis_period(self):
        """Program ending before analysis should show zero activity."""
        time_grid = TimeGrid.build("2026-06", "2026-12")
        unit_cohorts = [
            {"cohort_id": "1B_classic", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}
        ]
        renovation_programs = [
            {
                "program_id": "upgrade_1b",
                "target_cohort": "1B_classic",
                "output_cohort": "1B_renovated",
                "renovation_cost_per_unit": 15000,
                "rent_premium_monthly": 200,
                "downtime_days": 30,
                "strategy": "proactive",
                "start_month": "2026-01",
                "end_month": "2026-03",  # Ends before analysis starts
                "monthly_pace": 10,
            }
        ]

        result = compute_renovations(time_grid, unit_cohorts, renovation_programs)

        # No renovations should occur
        self.assertEqual(result["summary"]["total_units_renovated"], 0)

    def test_program_after_analysis_period(self):
        """Program starting after analysis should show zero activity."""
        time_grid = TimeGrid.build("2026-01", "2026-03")
        unit_cohorts = [
            {"cohort_id": "1B_classic", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1000}
        ]
        renovation_programs = [
            {
                "program_id": "upgrade_1b",
                "target_cohort": "1B_classic",
                "output_cohort": "1B_renovated",
                "renovation_cost_per_unit": 15000,
                "rent_premium_monthly": 200,
                "downtime_days": 30,
                "strategy": "proactive",
                "start_month": "2026-06",  # Starts after analysis ends
                "monthly_pace": 10,
            }
        ]

        result = compute_renovations(time_grid, unit_cohorts, renovation_programs)

        # No renovations should occur
        self.assertEqual(result["summary"]["total_units_renovated"], 0)

    def test_exhaust_available_units(self):
        """Should stop renovating when all units exhausted."""
        time_grid = TimeGrid.build("2026-01", "2026-12")
        unit_cohorts = [
            {"cohort_id": "1B_classic", "unit_type": "1BR", "unit_count": 20, "initial_inplace_rent": 1000}
        ]
        renovation_programs = [
            {
                "program_id": "upgrade_1b",
                "target_cohort": "1B_classic",
                "output_cohort": "1B_renovated",
                "renovation_cost_per_unit": 15000,
                "rent_premium_monthly": 200,
                "downtime_days": 30,
                "strategy": "proactive",
                "start_month": "2026-01",
                "monthly_pace": 10,  # Would exhaust 20 units in 2 months
            }
        ]

        result = compute_renovations(time_grid, unit_cohorts, renovation_programs)

        # Should max out at 20 units
        self.assertEqual(result["summary"]["total_units_renovated"], 20)

        # Month 1: 10 units, Month 2: 10 units, Month 3+: 0 units
        self.assertEqual(result["by_month"][0]["units_renovated"], 10)
        self.assertEqual(result["by_month"][1]["units_renovated"], 10)
        self.assertEqual(result["by_month"][2]["units_renovated"], 0)


class TestRenovationsSummary(unittest.TestCase):
    """Test summary calculations."""

    def test_net_renovation_cost(self):
        """Net cost should include vacancy loss."""
        time_grid = TimeGrid.build("2026-01", "2026-01")
        unit_cohorts = [
            {"cohort_id": "1B_classic", "unit_type": "1BR", "unit_count": 100, "initial_inplace_rent": 1500}
        ]
        renovation_programs = [
            {
                "program_id": "upgrade_1b",
                "target_cohort": "1B_classic",
                "output_cohort": "1B_renovated",
                "renovation_cost_per_unit": 15000,
                "rent_premium_monthly": 200,
                "downtime_days": 30,
                "strategy": "proactive",
                "start_month": "2026-01",
                "monthly_pace": 10,
            }
        ]

        result = compute_renovations(time_grid, unit_cohorts, renovation_programs)

        # Renovation cost: 10 * 15000 = 150000
        # Vacancy loss: 10 * 1500 = 15000
        # Net cost: 165000
        self.assertAlmostEqual(result["summary"]["total_renovation_cost"], 150000.0, places=2)
        self.assertAlmostEqual(result["summary"]["total_vacancy_loss"], 15000.0, places=2)
        self.assertAlmostEqual(result["summary"]["net_renovation_cost"], 165000.0, places=2)


if __name__ == "__main__":
    unittest.main()
