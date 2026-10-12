"""The installed stdio adapter must keep responses bounded and gates honest."""

import unittest
from unittest.mock import patch

from engine import mcp_server


class _Report:
    status = "PASS"

    def to_dict(self):
        return {"status": self.status, "issues": []}


class TestMcpAdapter(unittest.TestCase):
    def setUp(self):
        self.inputs = {
            "purchase_assumptions": {"purchase_price": 100},
            "debt_terms": {"commitment": 80},
        }
        self.results = {
            "metrics": {
                "irr": {"levered_irr": 0.14, "unlevered_irr": 0.10},
                "dscr": {"minimum_dscr": 1.3, "average_dscr": 1.4},
                "equity_multiple": {"levered_em": 1.8},
                "yields": {"going_in_cap_rate": 0.07},
            },
            "cashflow": {"summary": {"total_noi": 1000}, "by_year": [1, 2]},
        }

    def test_small_summary_excludes_cashflow_and_identifies_adapter(self):
        with patch.object(mcp_server, "run_underwriting", return_value=self.results):
            result = mcp_server.run_deal_summary(self.inputs)
        self.assertEqual(result["adapter_contract"], "plat.underwriting.mcp/1")
        self.assertEqual(result["noi_summary"]["total_noi"], 1000)
        self.assertNotIn("cashflow", result)

    def test_feasibility_enforces_ltv_and_refuses_unknown_ratio(self):
        with patch.object(mcp_server, "validate_deal", return_value=_Report()), patch.object(
            mcp_server, "run_underwriting", return_value=self.results
        ):
            result = mcp_server.check_deal_feasibility(self.inputs)
            missing = mcp_server.check_deal_feasibility({"debt_terms": {"commitment": 80}})
        self.assertFalse(result["feasible"])
        self.assertEqual(result["blocked_by"], ["ltv"])
        self.assertEqual(result["gates"]["ltv"]["actual"], 0.8)
        self.assertFalse(missing["feasible"])
        self.assertIsNone(missing["gates"]["ltv"]["actual"])

    def test_validation_failure_never_runs_engine(self):
        report = _Report()
        report.status = "FAIL"
        with patch.object(mcp_server, "validate_deal", return_value=report), patch.object(
            mcp_server, "run_underwriting"
        ) as run:
            result = mcp_server.check_deal_feasibility(self.inputs)
        run.assert_not_called()
        self.assertEqual(result["blocked_by"], "validation")

    def test_assumption_register_is_bounded_and_refuses_without_assumptions(self):
        entry = {
            "value": 0.0625, "unit": "decimal", "source": "analyst", "basis": "synthetic",
            "as_of": None, "confidence": "high", "default_value": 0.06, "review_required": False,
            "override": {"value": 0.0625, "note": "IC view", "edited_by": "Analyst A"},
            "candidates": [{"source": "analyst", "value": 0.0625,
                            "default": {"source": "house:exit_cap_spread", "basis": "spread"}}],
        }
        inputs = {"metadata": {"deal_id": "Synthetic MCP"},
                  "assumptions": {"entries": {"exit_cap_rate": entry}}}
        with patch.object(mcp_server, "run_underwriting") as run:
            result = mcp_server.assumption_register(inputs, filter="edited")
        run.assert_not_called()
        self.assertEqual(result["adapter_contract"], "plat.underwriting.mcp/1")
        self.assertEqual(result["status"], "success")
        row = result["rows"][0]
        self.assertEqual(row["user_edit"]["edited_by"], "Analyst A")
        self.assertEqual(row["default"]["source"], "house:exit_cap_spread")
        self.assertNotIn("candidates", row)
        self.assertEqual(row["candidate_count"], 1)
        self.assertEqual(mcp_server.assumption_register({})["status"], "refused")
        self.assertEqual(mcp_server.assumption_register(inputs, filter="nope")["error"]["code"], "BAD_FILTER")


if __name__ == "__main__":
    unittest.main()
