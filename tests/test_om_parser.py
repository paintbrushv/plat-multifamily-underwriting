"""Tests for OmData model, to_partial_deal(), and parse_om()."""
import json
import pytest
from unittest.mock import MagicMock, patch
from engine.ingest.om_parser import OmData, OmUnitType


def _om(**kwargs) -> OmData:
    defaults = dict(
        property_name="Cedar Ridge Apartments",
        address="2847 Maple Creek Drive, Garland, TX 75041",
        market="dallas",
        year_built=1987,
        total_units=148,
        property_class="C",
        building_type="garden",
    )
    defaults.update(kwargs)
    return OmData(**defaults)


class TestOmData:
    def test_default_confidence_is_low(self):
        om = _om()
        assert om.extraction_confidence == "low"

    def test_unit_mix_defaults_empty(self):
        om = _om()
        assert om.unit_mix == []

    def test_raw_extracted_defaults_empty(self):
        om = _om()
        assert om.raw_extracted == {}


class TestToPartialDeal:
    def test_purchase_price_mapped(self):
        om = _om(asking_price=13_200_000)
        partial = om.to_partial_deal()
        assert partial["purchase_assumptions"]["purchase_price"] == 13_200_000

    def test_exit_cap_mapped(self):
        om = _om(exit_cap_rate=0.0575)
        partial = om.to_partial_deal()
        assert partial["exit_assumptions"]["exit_cap_rate"] == pytest.approx(0.0575)

    def test_property_name_in_metadata(self):
        om = _om()
        partial = om.to_partial_deal()
        assert partial["metadata"]["deal_id"] == "Cedar Ridge Apartments"

    def test_property_summary_fields(self):
        om = _om(year_built=1987, total_units=148, property_class="C")
        partial = om.to_partial_deal()
        ps = partial["property_summary"]
        assert ps["year_built"] == 1987
        assert ps["total_units"] == 148
        assert ps["property_class"] == "C"

    def test_none_fields_omitted(self):
        om = _om(asking_price=None, exit_cap_rate=None)
        partial = om.to_partial_deal()
        assert "purchase_assumptions" not in partial
        assert "exit_assumptions" not in partial

    def test_partial_deal_is_dict(self):
        partial = _om().to_partial_deal()
        assert isinstance(partial, dict)

    # ── Bug 1.5: unit_mix evidence emission ──────────────────────────────

    def test_to_partial_deal_emits_unit_mix_evidence(self):
        """OmData with 3 unit types → output has om_unit_mix_evidence with 3 entries."""
        om = _om(unit_mix=[
            OmUnitType("1BR/1BA", 72, 650, 825, 975),
            OmUnitType("2BR/1BA", 52, 875, 1025, 1175),
            OmUnitType("2BR/2BA", 24, 950, 1100, 1250),
        ])
        partial = om.to_partial_deal()
        assert "om_unit_mix_evidence" in partial
        assert len(partial["om_unit_mix_evidence"]) == 3
        # Verify evidence preserves count + rent fields the intake-orchestrator
        # needs to compare against rent roll.
        first = partial["om_unit_mix_evidence"][0]
        assert first["unit_type"] == "1BR/1BA"
        assert first["count"] == 72
        assert first["current_rent"] == 825
        assert first["market_rent"] == 975

    def test_to_partial_deal_unit_mix_uses_non_canonical_key(self):
        """Key must be exactly 'om_unit_mix_evidence' — engine must not see
        it as canonical 'unit_mix'."""
        om = _om(unit_mix=[OmUnitType("1BR/1BA", 72, 650, 825, 975)])
        partial = om.to_partial_deal()
        assert "om_unit_mix_evidence" in partial
        # Critical: NOT under the canonical 'unit_mix' or 'unit_cohorts' keys
        assert "unit_mix" not in partial
        assert "unit_cohorts" not in partial

    def test_to_partial_deal_omits_unit_mix_evidence_when_empty(self):
        """Empty unit_mix → key is omitted (don't pollute partial dict)."""
        om = _om(unit_mix=[])
        partial = om.to_partial_deal()
        assert "om_unit_mix_evidence" not in partial


CANNED_EXTRACTED = {
    "property_name": "Cedar Ridge Apartments",
    "address": "2847 Maple Creek Drive, Garland, TX 75041",
    "market": "dallas",
    "year_built": 1987,
    "total_units": 148,
    "property_class": "C",
    "building_type": "garden",
    "unit_mix": [
        {"unit_type": "1BR/1BA", "count": 72, "sqft": 650, "current_rent": 825, "market_rent": 975},
    ],
    "asking_price": 13200000,
    "price_per_unit": 89189,
    "hold_years": 5,
    "exit_cap_rate": 0.0575,
    "extraction_confidence": "high",
}


class TestParseOm:
    def _mock_client(self):
        client = MagicMock()
        message = MagicMock()
        message.content = [MagicMock(text=json.dumps(CANNED_EXTRACTED))]
        client.messages.create.return_value = message
        return client

    def test_returns_om_data(self, tmp_path):
        from engine.ingest.om_parser import parse_om
        pdf = tmp_path / "test.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        with patch("engine.ingest.om_parser.extract_om_data", return_value=CANNED_EXTRACTED):
            with patch("engine.ingest.om_parser._extract_pdf_text", return_value="OM text"):
                result = parse_om(pdf, client=self._mock_client())
        assert isinstance(result, OmData)

    def test_property_name_populated(self, tmp_path):
        from engine.ingest.om_parser import parse_om
        pdf = tmp_path / "test.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        with patch("engine.ingest.om_parser.extract_om_data", return_value=CANNED_EXTRACTED):
            with patch("engine.ingest.om_parser._extract_pdf_text", return_value="OM text"):
                result = parse_om(pdf, client=self._mock_client())
        assert result.property_name == "Cedar Ridge Apartments"

    def test_asking_price_populated(self, tmp_path):
        from engine.ingest.om_parser import parse_om
        pdf = tmp_path / "test.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        with patch("engine.ingest.om_parser.extract_om_data", return_value=CANNED_EXTRACTED):
            with patch("engine.ingest.om_parser._extract_pdf_text", return_value="OM text"):
                result = parse_om(pdf, client=self._mock_client())
        assert result.asking_price == 13_200_000

    def test_unit_mix_populated(self, tmp_path):
        from engine.ingest.om_parser import parse_om
        pdf = tmp_path / "test.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        with patch("engine.ingest.om_parser.extract_om_data", return_value=CANNED_EXTRACTED):
            with patch("engine.ingest.om_parser._extract_pdf_text", return_value="OM text"):
                result = parse_om(pdf, client=self._mock_client())
        assert len(result.unit_mix) == 1
        assert result.unit_mix[0].unit_type == "1BR/1BA"
        assert result.unit_mix[0].count == 72

    def test_exit_cap_rate_populated(self, tmp_path):
        from engine.ingest.om_parser import parse_om
        pdf = tmp_path / "test.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        with patch("engine.ingest.om_parser.extract_om_data", return_value=CANNED_EXTRACTED):
            with patch("engine.ingest.om_parser._extract_pdf_text", return_value="OM text"):
                result = parse_om(pdf, client=self._mock_client())
        assert result.exit_cap_rate == pytest.approx(0.0575)

    def test_extraction_error_returns_minimal_om_data(self, tmp_path):
        from engine.ingest.om_parser import parse_om
        pdf = tmp_path / "test.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        error_result = {"extraction_error": "API timeout"}
        with patch("engine.ingest.om_parser.extract_om_data", return_value=error_result):
            with patch("engine.ingest.om_parser._extract_pdf_text", return_value="text"):
                result = parse_om(pdf, client=self._mock_client())
        # Should return OmData with blanks, not raise
        assert isinstance(result, OmData)
        assert result.extraction_confidence == "low"

    def test_invalid_extracted_year_built_falls_back_to_text_summary(self, tmp_path):
        from engine.ingest.om_parser import parse_om
        pdf = tmp_path / "test.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        extracted = dict(CANNED_EXTRACTED)
        extracted["year_built"] = 1051
        om_text = "LOCATION\n10510 Example Way Rd, Sampletown, IN\n\nYEAR BUILT\n2025\n\nNUMBER OF UNITS\n261\n"
        with patch("engine.ingest.om_parser.extract_om_data", return_value=extracted):
            with patch("engine.ingest.om_parser._extract_pdf_text", return_value=om_text):
                result = parse_om(pdf, client=self._mock_client())
        assert result.year_built == 2025

    def test_txt_file_skips_pdfplumber(self, tmp_path):
        from engine.ingest.om_parser import parse_om
        txt = tmp_path / "test.txt"
        txt.write_text("plain text OM")
        with patch("engine.ingest.om_parser.extract_om_data", return_value=CANNED_EXTRACTED):
            result = parse_om(txt, client=self._mock_client())
        assert result.property_name == "Cedar Ridge Apartments"

    # ── use_langextract opt-in path ───────────────────────────────────────

    def test_use_langextract_calls_new_extractor(self, tmp_path):
        from engine.ingest.om_parser import parse_om
        from engine.extraction.schemas import ExtractedDeal, ExtractedField
        txt = tmp_path / "om.txt"
        txt.write_text("OM text body")
        synthetic = ExtractedDeal(
            purchase_price=ExtractedField("$62,400,000", 10, 21, "$62,400,000"),
            cap_rate=ExtractedField("5.85%", 30, 35, "5.85%"),
            year_1_noi=ExtractedField("$3,650,400", 40, 50, "$3,650,400"),
            unit_count=ExtractedField("240", 60, 63, "240"),
            year_built=ExtractedField("2003", 70, 74, "2003"),
        )
        with patch("engine.ingest.om_parser.extract_deal_from_text", return_value=synthetic) as new_call, \
             patch("engine.ingest.om_parser.extract_om_data") as old_call:
            result = parse_om(txt, use_langextract=True)
        new_call.assert_called_once()
        old_call.assert_not_called()
        assert isinstance(result, OmData)
        assert result.asking_price == 62_400_000
        assert result.year_built == 2003
        assert result.total_units == 240

    def test_use_langextract_default_false_calls_anthropic_path(self, tmp_path):
        from engine.ingest.om_parser import parse_om
        txt = tmp_path / "om.txt"
        txt.write_text("OM text body")
        with patch("engine.ingest.om_parser.extract_om_data", return_value=CANNED_EXTRACTED) as old_call, \
             patch("engine.ingest.om_parser.extract_deal_from_text") as new_call:
            parse_om(txt, client=self._mock_client())
        old_call.assert_called_once()
        new_call.assert_not_called()

    def test_use_langextract_partial_deal_carries_provenance(self, tmp_path):
        from engine.ingest.om_parser import parse_om
        from engine.extraction.schemas import ExtractedDeal, ExtractedField
        txt = tmp_path / "om.txt"
        txt.write_text("OM text body")
        synthetic = ExtractedDeal(
            purchase_price=ExtractedField("$62,400,000", 10, 21, "$62,400,000"),
            cap_rate=ExtractedField("5.85%", 30, 35, "5.85%"),
            year_1_noi=ExtractedField("$3,650,400", 40, 50, "$3,650,400"),
            unit_count=ExtractedField("240", 60, 63, "240"),
            year_built=ExtractedField("2003", 70, 74, "2003"),
        )
        with patch("engine.ingest.om_parser.extract_deal_from_text", return_value=synthetic):
            result = parse_om(txt, use_langextract=True)
        partial = result.to_partial_deal()
        assert "_provenance" in partial
        assert partial["_provenance"]["purchase_price"]["char_start"] == 10
        assert partial["_provenance"]["year_built"]["char_end"] == 74

    def test_pdf_no_30_page_cap(self, tmp_path):
        """Bug 1.10: confirm all pages are processed (no silent 30-page cap).

        Mocks pdfplumber to return a 50-page document and asserts every
        page's text appears in the extracted output. Previously pages[:30]
        truncated long Berkadia OMs (a prior production deal needs pages 14-40).
        """
        from engine.ingest.om_parser import _extract_pdf_text

        pdf_path = tmp_path / "long_om.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 fake")

        # Build 50 mock pages, each with a unique sentinel string.
        mock_pages = []
        for i in range(50):
            page = MagicMock()
            page.extract_text.return_value = f"PAGE_SENTINEL_{i:02d}"
            mock_pages.append(page)

        mock_pdf = MagicMock()
        mock_pdf.pages = mock_pages
        mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
        mock_pdf.__exit__ = MagicMock(return_value=False)

        with patch("pdfplumber.open", return_value=mock_pdf):
            text = _extract_pdf_text(pdf_path)

        # All 50 pages must appear — including pages 30-49 that the old cap
        # would have dropped.
        for i in range(50):
            assert f"PAGE_SENTINEL_{i:02d}" in text, (
                f"Page {i} missing from extracted text — page cap regression"
            )
        # Specifically verify the post-cap pages are present.
        assert "PAGE_SENTINEL_30" in text
        assert "PAGE_SENTINEL_49" in text
