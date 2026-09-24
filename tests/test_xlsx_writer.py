"""
Tests for XlsmWriter enhancements (freeze panes, print areas, header/footer).

These tests work at the XML level using the actual rediq_clone.xlsm template.
If the template is missing, tests are skipped gracefully.
"""
import os
import tempfile
import unittest
from pathlib import Path

from lxml import etree

TEMPLATE_PATH = Path("excel/rediq_clone.xlsm")
NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _template_available():
    """Check if the RedIQ clone template exists for integration tests."""
    return TEMPLATE_PATH.exists()


@unittest.skipUnless(_template_available(), "rediq_clone.xlsm template not found")
class TestFreezePanes(unittest.TestCase):
    """Test freeze pane XML generation."""

    def setUp(self):
        from engine.xlsx_writer import XlsmWriter

        self.fd, self.tmp_path = tempfile.mkstemp(suffix=".xlsm")
        os.close(self.fd)
        self.writer = XlsmWriter(TEMPLATE_PATH, self.tmp_path)

    def tearDown(self):
        if os.path.exists(self.tmp_path):
            os.unlink(self.tmp_path)

    def test_freeze_rows_only(self):
        """Freeze top 2 rows: ySplit=2, no xSplit."""
        self.writer.set_freeze_pane("Input", 3, 1)  # Freeze rows 1-2
        root = self.writer._get_sheet_xml("Input")
        pane = root.find(f".//{{{NS}}}pane")
        self.assertIsNotNone(pane)
        self.assertEqual(pane.get("ySplit"), "2")
        self.assertIsNone(pane.get("xSplit"))
        self.assertEqual(pane.get("state"), "frozen")

    def test_freeze_rows_and_cols(self):
        """Freeze top 4 rows and left 4 cols: ySplit=4, xSplit=4."""
        self.writer.set_freeze_pane("CF Calculations", 5, 5)
        root = self.writer._get_sheet_xml("CF Calculations")
        pane = root.find(f".//{{{NS}}}pane")
        self.assertIsNotNone(pane)
        self.assertEqual(pane.get("ySplit"), "4")
        self.assertEqual(pane.get("xSplit"), "4")
        self.assertEqual(pane.get("state"), "frozen")

    def test_freeze_pane_survives_save(self):
        """Freeze pane should persist after save() and be readable in output."""
        self.writer.set_freeze_pane("Input", 3, 1)
        self.writer.save()

        # Re-open the output and verify
        from engine.xlsx_writer import XlsmWriter as XW2
        import zipfile

        with zipfile.ZipFile(self.tmp_path, "r") as zf:
            # Find Input sheet XML path
            writer2 = XW2.__new__(XW2)
            writer2.template_path = Path(self.tmp_path)
            writer2._sheet_map = {}
            writer2._parsed = {}
            writer2._modified = set()
            writer2._build_sheet_map()

            xml_path = writer2._sheet_map.get("Input")
            self.assertIsNotNone(xml_path)
            raw = zf.read(xml_path)
            root = etree.fromstring(raw)
            pane = root.find(f".//{{{NS}}}pane")
            self.assertIsNotNone(pane)
            self.assertEqual(pane.get("ySplit"), "2")


@unittest.skipUnless(_template_available(), "rediq_clone.xlsm template not found")
class TestPageSetup(unittest.TestCase):
    """Test page setup and header/footer."""

    def setUp(self):
        from engine.xlsx_writer import XlsmWriter

        self.fd, self.tmp_path = tempfile.mkstemp(suffix=".xlsm")
        os.close(self.fd)
        self.writer = XlsmWriter(TEMPLATE_PATH, self.tmp_path)

    def tearDown(self):
        if os.path.exists(self.tmp_path):
            os.unlink(self.tmp_path)

    def test_landscape_orientation(self):
        """Page setup should set orientation to landscape."""
        self.writer.set_page_setup("CF Calculations", orientation="landscape")
        root = self.writer._get_sheet_xml("CF Calculations")
        ps = root.find(f".//{{{NS}}}pageSetup")
        self.assertIsNotNone(ps)
        self.assertEqual(ps.get("orientation"), "landscape")

    def test_header_footer(self):
        """Header and footer should be set in XML."""
        self.writer.set_header_footer(
            "Input",
            header="&LDeal Name&RExampleSponsor Capital",
            footer="&LConfidential&C&P of &N",
        )
        root = self.writer._get_sheet_xml("Input")
        hf = root.find(f".//{{{NS}}}headerFooter")
        self.assertIsNotNone(hf)
        oh = hf.find(f"{{{NS}}}oddHeader")
        self.assertIsNotNone(oh)
        self.assertIn("ExampleSponsor Capital", oh.text)


@unittest.skipUnless(_template_available(), "rediq_clone.xlsm template not found")
class TestWriteFreezePanesIntegration(unittest.TestCase):
    """Test write_freeze_panes() from rediq_output.py."""

    def test_freeze_panes_applies_to_key_tabs(self):
        """write_freeze_panes should apply freeze to CF Calcs, Input, Waterfall."""
        from engine.xlsx_writer import XlsmWriter
        from engine.rediq_output import write_freeze_panes

        fd, tmp_path = tempfile.mkstemp(suffix=".xlsm")
        os.close(fd)
        try:
            writer = XlsmWriter(TEMPLATE_PATH, tmp_path)
            write_freeze_panes(writer)

            # Check CF Calculations has freeze pane
            root = writer._get_sheet_xml("CF Calculations")
            pane = root.find(f".//{{{NS}}}pane")
            self.assertIsNotNone(pane, "CF Calculations should have freeze pane")
            self.assertEqual(pane.get("state"), "frozen")

            # Check Input has freeze pane
            root = writer._get_sheet_xml("Input")
            pane = root.find(f".//{{{NS}}}pane")
            self.assertIsNotNone(pane, "Input should have freeze pane")
        finally:
            os.unlink(tmp_path)


if __name__ == "__main__":
    unittest.main()
