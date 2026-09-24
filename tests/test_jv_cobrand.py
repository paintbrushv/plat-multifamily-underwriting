"""
Tests for JV Co-Brand support (Task 2.3).

Tests BrandConfig, PDF co-branding, PPTX co-branding, Excel header co-branding,
and backward compatibility when no partner is configured.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.brand import BrandConfig


class TestBrandConfig(unittest.TestCase):
    """Test BrandConfig dataclass and factory methods."""

    def test_default_no_partner(self):
        """Default BrandConfig has no partner (company-only)."""
        bc = BrandConfig()
        self.assertFalse(bc.has_partner)
        self.assertEqual(bc.company_name, "ExampleSponsor Capital")
        self.assertEqual(bc.header_text, "ExampleSponsor Capital")
        self.assertEqual(bc.subtitle_text, "")

    def test_with_partner(self):
        """BrandConfig with partner produces co-branded text."""
        bc = BrandConfig(partner_name="ExamplePartner Capital")
        self.assertTrue(bc.has_partner)
        self.assertEqual(bc.header_text, "Prepared by ExampleSponsor Capital for ExamplePartner Capital")
        self.assertEqual(bc.subtitle_text, "Prepared for ExamplePartner Capital")

    def test_with_partner_and_logo(self):
        """BrandConfig accepts partner logo path."""
        bc = BrandConfig(
            partner_name="Cortland",
            partner_logo_path=Path("/tmp/cortland_logo.png"),
        )
        self.assertTrue(bc.has_partner)
        self.assertEqual(bc.partner_logo_path, Path("/tmp/cortland_logo.png"))

    def test_from_inputs_no_fund_assumptions(self):
        """from_inputs with no fund_assumptions returns company-only."""
        bc = BrandConfig.from_inputs({})
        self.assertFalse(bc.has_partner)

    def test_from_inputs_with_partner(self):
        """from_inputs extracts jv_partner_name from fund_assumptions."""
        inputs = {
            "fund_assumptions": {
                "jv_partner_name": "Greystar",
                "jv_partner_logo_path": "/assets/greystar.png",
            }
        }
        bc = BrandConfig.from_inputs(inputs)
        self.assertTrue(bc.has_partner)
        self.assertEqual(bc.partner_name, "Greystar")
        self.assertEqual(bc.partner_logo_path, Path("/assets/greystar.png"))

    def test_from_inputs_no_partner_name(self):
        """from_inputs without jv_partner_name returns company-only."""
        inputs = {"fund_assumptions": {"sponsor_equity_pct": 0.1}}
        bc = BrandConfig.from_inputs(inputs)
        self.assertFalse(bc.has_partner)


class TestPDFCoBrand(unittest.TestCase):
    """Test PDF one-pager co-branding."""

    def _minimal_inputs(self, partner_name=None):
        inputs = {
            "metadata": {"deal_id": "Test Deal", "address": "123 Main St"},
            "unit_cohorts": [{"unit_count": 100, "sqft": 800}],
            "purchase_assumptions": {"purchase_price": 10_000_000},
            "exit_assumptions": {"exit_cap_rate": 0.055},
            "time_grid": {
                "analysis_start_date": "2026-01-01",
                "analysis_end_date": "2031-01-01",
            },
            "growth_assumptions": {"annual_growth_rate": 0.03},
            "debt_terms": {"rate": 0.05, "ltv": 0.7},
            "fund_assumptions": {},
        }
        if partner_name:
            inputs["fund_assumptions"]["jv_partner_name"] = partner_name
        return inputs

    def _minimal_results(self):
        return {
            "metrics": {
                "irr": {"levered_irr": 0.15, "unlevered_irr": 0.10, "partnership_irr": 0.18},
                "equity_multiple": {"levered_em": 2.0, "unlevered_em": 1.8},
                "dscr": {"average_dscr": 1.35, "minimum_dscr": 1.10},
                "yields": {"going_in_cap_rate": 0.055},
                "cash_on_cash": {"by_year": [{"yield": 0.08}]},
            },
            "cashflow": {"by_year": []},
            "fund_waterfall": {"summary": {"partnership_equity_multiple": 1.9}},
        }

    def test_pdf_no_partner(self):
        """PDF generates without partner (backward compatible)."""
        from engine.pdf_onepager import generate_onepager

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            tmp = f.name
        try:
            result = generate_onepager(
                self._minimal_results(), self._minimal_inputs(), tmp
            )
            self.assertTrue(Path(result).exists())
            self.assertGreater(Path(result).stat().st_size, 0)
        finally:
            os.unlink(tmp)

    def test_pdf_with_partner(self):
        """PDF generates with partner co-branding."""
        from engine.pdf_onepager import generate_onepager

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            tmp = f.name
        try:
            result = generate_onepager(
                self._minimal_results(),
                self._minimal_inputs(partner_name="ExamplePartner Capital"),
                tmp,
            )
            self.assertTrue(Path(result).exists())
            # File should be generated (content verified by visual inspection)
            self.assertGreater(Path(result).stat().st_size, 0)
        finally:
            os.unlink(tmp)

    def test_pdf_with_explicit_brand_config(self):
        """PDF accepts explicit BrandConfig parameter."""
        from engine.pdf_onepager import generate_onepager

        bc = BrandConfig(partner_name="Blackstone")
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            tmp = f.name
        try:
            result = generate_onepager(
                self._minimal_results(), self._minimal_inputs(), tmp,
                brand_config=bc,
            )
            self.assertTrue(Path(result).exists())
        finally:
            os.unlink(tmp)

    def test_pdf_summary_table_shows_year_built(self):
        """The deal summary table should surface Year Built explicitly."""
        from engine.pdf_onepager import _deal_summary_table

        inputs = self._minimal_inputs()
        inputs["metadata"]["address"] = {
            "street": "123 Main St",
            "city": "Dallas",
            "state": "TX",
            "zip": "75201",
        }
        inputs["metadata"]["year_built"] = 1987
        table = _deal_summary_table(inputs, self._minimal_results(), styles=None)
        cell_text = " ".join(str(cell) for row in table._cellvalues for cell in row)
        self.assertIn("Year Built", cell_text)
        self.assertIn("1,987", cell_text)
        self.assertIn("Location", cell_text)
        self.assertIn("Dallas, TX", cell_text)


class TestPPTXCoBrand(unittest.TestCase):
    """Test PPTX co-branding."""

    def _minimal_inputs(self, partner_name=None):
        inputs = {
            "metadata": {"deal_id": "Test Deal", "city": "Dallas", "state": "TX"},
            "unit_cohorts": [{"unit_count": 200, "sqft": 900}],
            "purchase_assumptions": {"purchase_price": 25_000_000},
            "exit_assumptions": {"exit_cap_rate": 0.055},
            "time_grid": {
                "analysis_start_date": "2026-01-01",
                "analysis_end_date": "2031-01-01",
            },
            "growth_assumptions": {"annual_growth_rate": 0.03},
            "debt_terms": {"rate": 0.05, "commitment": 17_500_000, "amort_years": 30},
            "physical_vacancy_curve": [{"vacancy_rate": 0.05}],
            "fund_assumptions": {},
        }
        if partner_name:
            inputs["fund_assumptions"]["jv_partner_name"] = partner_name
        return inputs

    def _minimal_results(self):
        return {
            "metrics": {
                "irr": {"levered_irr": 0.15, "unlevered_irr": 0.10, "partnership_irr": 0.18},
                "equity_multiple": {"levered_em": 2.0, "unlevered_em": 1.8, "partnership_em": 1.9},
                "dscr": {"average_dscr": 1.35, "minimum_dscr": 1.10},
                "yields": {"going_in_cap_rate": 0.055},
                "cash_on_cash": {"by_year": [{"yield": 0.08}]},
            },
            "cashflow": {"by_year": [
                {"year": 2026, "net_operating_income": 1_500_000, "levered_cashflow": 500_000},
                {"year": 2027, "net_operating_income": 1_550_000, "levered_cashflow": 550_000},
            ]},
            "fund_waterfall": {"summary": {"partnership_equity_multiple": 1.9}},
        }

    def test_pptx_no_partner(self):
        """PPTX generates without partner (backward compatible)."""
        from engine.pptx_generator import generate_presentation

        with tempfile.NamedTemporaryFile(suffix=".pptx", delete=False) as f:
            tmp = f.name
        try:
            result = generate_presentation(
                self._minimal_results(), self._minimal_inputs(), tmp,
            )
            self.assertTrue(Path(result).exists())
            self.assertGreater(Path(result).stat().st_size, 0)
        finally:
            os.unlink(tmp)

    def test_pptx_with_partner(self):
        """PPTX generates with partner co-branding."""
        from engine.pptx_generator import generate_presentation

        with tempfile.NamedTemporaryFile(suffix=".pptx", delete=False) as f:
            tmp = f.name
        try:
            result = generate_presentation(
                self._minimal_results(),
                self._minimal_inputs(partner_name="Cortland Partners"),
                tmp,
            )
            self.assertTrue(Path(result).exists())
            self.assertGreater(Path(result).stat().st_size, 0)
        finally:
            os.unlink(tmp)

    def test_pptx_footer_includes_partner(self):
        """PPTX slide footers include partner name when co-branded."""
        from engine.pptx_generator import _footer_text

        bc = BrandConfig(partner_name="Greystar")
        footer = _footer_text(bc)
        self.assertIn("GREYSTAR", footer)
        self.assertIn("EXAMPLESPONSOR CAPITAL", footer)

    def test_pptx_footer_company_only(self):
        """PPTX slide footers are company-only when no partner."""
        from engine.pptx_generator import _footer_text

        bc = BrandConfig()
        footer = _footer_text(bc)
        self.assertEqual(footer, "CONFIDENTIAL — EXAMPLESPONSOR CAPITAL")


class TestExcelCoBrand(unittest.TestCase):
    """Test Excel header co-branding."""

    def test_header_text_no_partner(self):
        """Excel header is the company name when no partner."""
        bc = BrandConfig()
        self.assertEqual(bc.header_text, "ExampleSponsor Capital")

    def test_header_text_with_partner(self):
        """Excel header includes 'Prepared by ... for ...' when partner set."""
        bc = BrandConfig(partner_name="ExamplePartner Capital")
        self.assertEqual(
            bc.header_text,
            "Prepared by ExampleSponsor Capital for ExamplePartner Capital",
        )


class TestSchemaAcceptsJVFields(unittest.TestCase):
    """Verify schema validates with jv_partner_name and jv_partner_logo_path."""

    def test_schema_with_jv_fields(self):
        """Schema validates fund_assumptions with JV co-brand fields."""
        import json
        from pathlib import Path

        schema_path = Path("engine/schemas/deal_schema_v0_1.json")
        schema = json.loads(schema_path.read_text())

        fund_props = schema["$defs"]["fund_assumptions"]["properties"]
        self.assertIn("jv_partner_name", fund_props)
        self.assertIn("jv_partner_logo_path", fund_props)
        self.assertEqual(fund_props["jv_partner_name"]["type"], "string")
        self.assertEqual(fund_props["jv_partner_logo_path"]["type"], "string")


if __name__ == "__main__":
    unittest.main()
