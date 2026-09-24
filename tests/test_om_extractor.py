"""Tests for om_extractor — LLM extraction with mocked Anthropic client."""
import json
from pathlib import Path
import pytest
from unittest.mock import MagicMock, patch

from engine.ingest.om_extractor import extract_om_data, _build_prompt

SAMPLE_TEXT = (Path(__file__).parent / "fixtures" / "sample_om_text.txt").read_text()

CANNED_RESPONSE = {
    "property_name": "Cedar Ridge Apartments",
    "address": "2847 Maple Creek Drive, Garland, TX 75041",
    "market": "dallas",
    "year_built": 1987,
    "total_units": 148,
    "property_class": "C",
    "building_type": "garden",
    "unit_mix": [
        {"unit_type": "1BR/1BA", "count": 72, "sqft": 650, "current_rent": 825, "market_rent": 975},
        {"unit_type": "2BR/1BA", "count": 52, "sqft": 875, "current_rent": 1025, "market_rent": 1175},
        {"unit_type": "2BR/2BA", "count": 24, "sqft": 1000, "current_rent": 1150, "market_rent": 1325},
    ],
    "asking_price": 13200000,
    "price_per_unit": 89189,
    "hold_years": 5,
    "exit_cap_rate": 0.0575,
    "extraction_confidence": "high",
}


def _mock_client(response_dict: dict) -> MagicMock:
    """Build an Anthropic client mock that returns canned JSON."""
    client = MagicMock()
    message = MagicMock()
    message.content = [MagicMock(text=json.dumps(response_dict))]
    client.messages.create.return_value = message
    return client


class TestExtractOmData:
    def test_returns_dict(self):
        client = _mock_client(CANNED_RESPONSE)
        result = extract_om_data(SAMPLE_TEXT, client)
        assert isinstance(result, dict)

    def test_property_name_extracted(self):
        client = _mock_client(CANNED_RESPONSE)
        result = extract_om_data(SAMPLE_TEXT, client)
        assert result["property_name"] == "Cedar Ridge Apartments"

    def test_total_units_extracted(self):
        client = _mock_client(CANNED_RESPONSE)
        result = extract_om_data(SAMPLE_TEXT, client)
        assert result["total_units"] == 148

    def test_asking_price_extracted(self):
        client = _mock_client(CANNED_RESPONSE)
        result = extract_om_data(SAMPLE_TEXT, client)
        assert result["asking_price"] == 13_200_000

    def test_exit_cap_extracted(self):
        client = _mock_client(CANNED_RESPONSE)
        result = extract_om_data(SAMPLE_TEXT, client)
        assert result["exit_cap_rate"] == pytest.approx(0.0575)

    def test_unit_mix_extracted(self):
        client = _mock_client(CANNED_RESPONSE)
        result = extract_om_data(SAMPLE_TEXT, client)
        assert len(result["unit_mix"]) == 3
        assert result["unit_mix"][0]["unit_type"] == "1BR/1BA"

    def test_client_called_with_messages(self):
        client = _mock_client(CANNED_RESPONSE)
        extract_om_data(SAMPLE_TEXT, client)
        assert client.messages.create.called
        call_kwargs = client.messages.create.call_args.kwargs
        assert "messages" in call_kwargs
        assert "model" in call_kwargs

    def test_invalid_json_returns_empty_with_error(self):
        client = MagicMock()
        message = MagicMock()
        message.content = [MagicMock(text="not valid json at all")]
        client.messages.create.return_value = message
        result = extract_om_data(SAMPLE_TEXT, client)
        # Should not raise — returns empty dict with error key
        assert isinstance(result, dict)
        assert "extraction_error" in result

    def test_api_exception_returns_empty_with_error(self):
        client = MagicMock()
        client.messages.create.side_effect = Exception("API timeout")
        result = extract_om_data(SAMPLE_TEXT, client)
        assert isinstance(result, dict)
        assert "extraction_error" in result

    def test_missing_fields_return_none(self):
        sparse = {"property_name": "Test", "total_units": None, "asking_price": None}
        client = _mock_client(sparse)
        result = extract_om_data(SAMPLE_TEXT, client)
        assert result.get("asking_price") is None


class TestBuildPrompt:
    def test_prompt_contains_text(self):
        prompt = _build_prompt("some OM text here")
        assert "some OM text here" in prompt

    def test_prompt_requests_json(self):
        prompt = _build_prompt("text")
        assert "JSON" in prompt or "json" in prompt

    def test_prompt_mentions_key_fields(self):
        prompt = _build_prompt("text")
        assert "asking_price" in prompt
        assert "unit_mix" in prompt
        assert "exit_cap_rate" in prompt

    def test_prompt_handles_large_pdf_without_truncation(self):
        """V1.3 — om_extractor must NOT truncate at 12k chars.

        Long broker OMs (a 12MB production OM, a long production OM, a prior production deal) previously
        had their financial summary, debt assumability, and rent comps
        silently dropped at the prior `om_text[:12000]` cap. The full
        OM text must reach the LLM since claude-haiku-4-5-20251001
        supports a 1M-token context window.
        """
        # Build a 50k-character OM body with a sentinel in the tail so we
        # can detect any silent truncation at the old 12k cap.
        long_text = "A " * 25_000 + "\nFINANCIAL_SUMMARY_SENTINEL_TAIL"
        assert len(long_text) > 12_000, "test premise: text exceeds prior cap"
        prompt = _build_prompt(long_text)
        # Sentinel from the tail must be present — proves no 12k slice.
        assert "FINANCIAL_SUMMARY_SENTINEL_TAIL" in prompt
        # And the prompt body itself must be at least as long as the input.
        assert len(prompt) >= len(long_text)
