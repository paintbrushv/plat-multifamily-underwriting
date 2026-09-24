import unittest

from engine.engine import run_underwriting


class TestRevenueBaseRent(unittest.TestCase):
    def _base_inputs(self):
        return {
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
            "market_rent_curve": [
                {"cohort_id": "c1", "start_period": "2026-01", "end_period": "2026-01", "market_rent": 1200}
            ],
            "loss_to_lease": [{"cohort_id": "c1", "start_period": "2026-01", "end_period": "2026-01", "ltl_percent": 0.0}],
            "physical_vacancy_curve": [
                {"cohort_id": "c1", "start_period": "2026-01", "end_period": "2026-01", "vacancy_rate": 0.0}
            ],
            "collection_loss_curve": [
                {"applies_to": "ALL", "start_period": "2026-01", "end_period": "2026-01", "loss_rate": 0.0}
            ],
            "revenue_programs": [],
            "program_adoption_curve": [],
            "opex_table": [],
        }

    def test_vacancy_and_collection_loss_separated(self):
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
            "market_rent_curve": [
                {"cohort_id": "c1", "start_period": "2026-01", "end_period": "2026-01", "market_rent": 1200}
            ],
            "loss_to_lease": [{"cohort_id": "c1", "start_period": "2026-01", "end_period": "2026-01", "ltl_percent": 0.1}],
            "physical_vacancy_curve": [
                {"cohort_id": "c1", "start_period": "2026-01", "end_period": "2026-01", "vacancy_rate": 0.2}
            ],
            "collection_loss_curve": [
                {"applies_to": "ALL", "start_period": "2026-01", "end_period": "2026-01", "loss_rate": 0.05}
            ],
            "revenue_programs": [],
            "program_adoption_curve": []
        }

        outputs = run_underwriting(inputs)
        row = outputs["revenue"]["base_rent"]["by_month"][0]

        self.assertAlmostEqual(row["market_rent"], 12000.00, places=2)  # 1200 * 10
        self.assertAlmostEqual(row["inplace_rent"], 10800.00, places=2)  # 1200*(1-0.1)*10
        self.assertAlmostEqual(row["billed_rent_after_vacancy"], 8640.00, places=2)  # 1080 * 8 units
        self.assertAlmostEqual(row["physical_vacancy_loss"], 2160.00, places=2)  # 10800 - 8640
        self.assertAlmostEqual(row["collection_loss"], 432.00, places=2)  # 8640 * 0.05
        self.assertAlmostEqual(row["net_rent"], 8208.00, places=2)  # 8640 - 432

    def test_recoverable_opex_without_rules_gets_no_phantom_recovery(self):
        inputs = self._base_inputs()
        inputs["opex_table"] = [
            {
                "category_name": "Water and Sewer",
                "calculation_type": "fixed_monthly",
                "base_value": 1000,
                "growth_rate": 0,
                "recoverable_flag": True,
            }
        ]

        outputs = run_underwriting(inputs)
        row = outputs["cashflow"]["by_month"][0]

        self.assertAlmostEqual(row["utility_recovery"], 0.00, places=2)
        self.assertAlmostEqual(row["effective_gross_income"], 12000.00, places=2)
        self.assertAlmostEqual(row["net_operating_income"], 11000.00, places=2)

    def test_multiple_utility_recovery_rules_apply_by_category(self):
        inputs = self._base_inputs()
        inputs["opex_table"] = [
            {
                "category_name": "Water and Sewer",
                "calculation_type": "fixed_monthly",
                "base_value": 1000,
                "growth_rate": 0,
                "recoverable_flag": True,
            },
            {
                "category_name": "Electricity",
                "calculation_type": "fixed_monthly",
                "base_value": 2000,
                "growth_rate": 0,
                "recoverable_flag": True,
            },
        ]
        inputs["utility_recovery_rules"] = [
            {"utility_category": "water", "recovery_basis": "percent_of_expense", "recovery_rate": 0.50},
            {"utility_category": "electric", "recovery_basis": "percent_of_expense", "recovery_rate": 0.25},
        ]

        outputs = run_underwriting(inputs)
        row = outputs["cashflow"]["by_month"][0]

        self.assertAlmostEqual(row["utility_recovery"], 1000.00, places=2)
        self.assertAlmostEqual(row["effective_gross_income"], 13000.00, places=2)
        self.assertAlmostEqual(row["net_operating_income"], 10000.00, places=2)


if __name__ == "__main__":
    unittest.main()

