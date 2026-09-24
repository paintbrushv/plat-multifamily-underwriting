import unittest

from engine.engine import run_underwriting


class TestRevenuePrograms(unittest.TestCase):
    def test_percent_rent_pricing_uses_current_modeled_rent(self):
        inputs = {
            "schema_version": "0.1",
            "metadata": {
                "deal_id": "d",
                "run_id": "r",
                "as_of_date": "2026-01-01",
                "analyst": "a",
                "purpose": "p",
            },
            "time_grid": {"analysis_start_date": "2026-01", "analysis_end_date": "2026-02"},
            "unit_cohorts": [{"cohort_id": "c1", "unit_type": "1B", "unit_count": 10, "initial_inplace_rent": 1000}],
            "market_rent_curve": [
                {"cohort_id": "c1", "start_period": "2026-01", "end_period": "2026-01", "market_rent": 1000},
                {"cohort_id": "c1", "start_period": "2026-02", "end_period": "2026-02", "market_rent": 1100},
            ],
            "loss_to_lease": [
                {"cohort_id": "c1", "start_period": "2026-01", "end_period": "2026-02", "ltl_percent": 0.0}
            ],
            "physical_vacancy_curve": [{"cohort_id": "c1", "start_period": "2026-01", "end_period": "2026-02", "vacancy_rate": 0.0}],
            "collection_loss_curve": [
                {"applies_to": "ALL", "start_period": "2026-01", "end_period": "2026-02", "loss_rate": 0.0},
                {"applies_to": "Programs", "start_period": "2026-01", "end_period": "2026-02", "loss_rate": 0.0},
            ],
            "revenue_programs": [
                {
                    "program_id": "p1",
                    "program_name": "RUBS",
                    "program_type": "recovery",
                    "pricing_type": "% rent",
                    "price_value": 0.10,
                    "eligible_units": "c1",
                    "start_period": "2026-01",
                    "end_period": "2026-02",
                }
            ],
            "program_adoption_curve": [{"program_id": "p1", "start_period": "2026-01", "end_period": "2026-02", "adoption_rate": 1.0}],
        }

        outputs = run_underwriting(inputs)
        by_month = outputs["revenue"]["programs"]["by_month"]

        self.assertAlmostEqual(by_month[0]["billed_programs"], 1000.00, places=2)
        self.assertAlmostEqual(by_month[1]["billed_programs"], 1100.00, places=2)

    def test_percent_rent_uses_dynamic_units_after_renovation(self):
        inputs = {
            "schema_version": "0.1",
            "metadata": {
                "deal_id": "d",
                "run_id": "r",
                "as_of_date": "2026-01-01",
                "analyst": "a",
                "purpose": "p",
            },
            "time_grid": {"analysis_start_date": "2026-01", "analysis_end_date": "2026-01"},
            "unit_cohorts": [{"cohort_id": "c1", "unit_type": "1B", "unit_count": 10, "initial_inplace_rent": 1000}],
            "market_rent_curve": [{"cohort_id": "c1", "start_period": "2026-01", "end_period": "2026-01", "market_rent": 1000}],
            "loss_to_lease": [{"cohort_id": "c1", "start_period": "2026-01", "end_period": "2026-01", "ltl_percent": 0.0}],
            "physical_vacancy_curve": [{"cohort_id": "c1", "start_period": "2026-01", "end_period": "2026-01", "vacancy_rate": 0.0}],
            "collection_loss_curve": [
                {"applies_to": "ALL", "start_period": "2026-01", "end_period": "2026-01", "loss_rate": 0.0},
                {"applies_to": "Programs", "start_period": "2026-01", "end_period": "2026-01", "loss_rate": 0.0},
            ],
            "renovation_programs": [
                {
                    "program_id": "reno_c1",
                    "program_name": "C1 renovation",
                    "target_cohort": "c1",
                    "output_cohort": "c1_reno",
                    "renovation_cost_per_unit": 10000,
                    "rent_premium_monthly": 200,
                    "downtime_days": 0,
                    "strategy": "proactive",
                    "start_month": "2026-01",
                    "monthly_pace": 5,
                }
            ],
            "revenue_programs": [
                {
                    "program_id": "p1",
                    "program_name": "RUBS",
                    "program_type": "recovery",
                    "pricing_type": "% rent",
                    "price_value": 0.10,
                    "eligible_units": "c1",
                    "start_period": "2026-01",
                    "end_period": "2026-01",
                }
            ],
            "program_adoption_curve": [{"program_id": "p1", "start_period": "2026-01", "end_period": "2026-01", "adoption_rate": 1.0}],
        }

        outputs = run_underwriting(inputs)
        by_program = outputs["revenue"]["programs"]["by_program_by_month"]

        self.assertEqual(by_program[0]["billable_units"], 5.0)
        self.assertAlmostEqual(by_program[0]["billed_revenue"], 500.00, places=2)

    def test_adoption_and_capacity(self):
        inputs = {
            "schema_version": "0.1",
            "metadata": {
                "deal_id": "d",
                "run_id": "r",
                "as_of_date": "2026-01-01",
                "analyst": "a",
                "purpose": "p",
            },
            "time_grid": {"analysis_start_date": "2026-01", "analysis_end_date": "2026-02"},
            "unit_cohorts": [{"cohort_id": "c1", "unit_type": "1B", "unit_count": 10, "initial_inplace_rent": 1000}],
            "market_rent_curve": [{"cohort_id": "c1", "start_period": "2026-01", "end_period": "2026-02", "market_rent": 1200}],
            "loss_to_lease": [{"cohort_id": "c1", "start_period": "2026-01", "end_period": "2026-02", "ltl_percent": 0.1}],
            "physical_vacancy_curve": [{"cohort_id": "c1", "start_period": "2026-01", "end_period": "2026-02", "vacancy_rate": 0.0}],
            "collection_loss_curve": [
                {"applies_to": "ALL", "start_period": "2026-01", "end_period": "2026-02", "loss_rate": 0.0},
                {"applies_to": "Programs", "start_period": "2026-01", "end_period": "2026-02", "loss_rate": 0.1}
            ],
            "revenue_programs": [
                {
                    "program_id": "p1",
                    "program_name": "Parking",
                    "program_type": "tenant-based",
                    "pricing_type": "$/unit",
                    "price_value": 50,
                    "eligible_units": "ALL",
                    "start_period": "2026-01",
                    "end_period": "2026-02"
                }
            ],
            "program_adoption_curve": [{"program_id": "p1", "start_period": "2026-01", "end_period": "2026-02", "adoption_rate": 0.9}],
            "program_capacity": [{"program_id": "p1", "total_capacity": 6}]
        }

        outputs = run_underwriting(inputs)
        by_month = outputs["revenue"]["programs"]["by_month"]
        self.assertEqual(len(by_month), 2)
        self.assertAlmostEqual(by_month[0]["billed_programs"], 300.00, places=2)  # min(10*0.9,6)*50
        self.assertAlmostEqual(by_month[0]["net_programs"], 270.00, places=2)  # 300*(1-0.1)


if __name__ == "__main__":
    unittest.main()
