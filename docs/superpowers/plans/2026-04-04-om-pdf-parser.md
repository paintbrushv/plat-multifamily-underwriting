# OM PDF Parser Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Parse multifamily Offering Memorandum PDFs into partial canonical deal schema fields (purchase price, unit mix, hold period, exit cap) using pdfplumber for text extraction and Claude Haiku for structured data extraction.

**Architecture:** Three-layer design: `om_extractor.py` isolates the Anthropic API call (mockable), `om_parser.py` orchestrates pdfplumber + extractor → `OmData` dataclass, `ingestion_pipeline.py` accepts an optional `om_path` and merges OM data into the canonical output. The LLM only sees extracted text, never raw bytes. All fields are nullable — OM parsing is best-effort.

**Tech Stack:** Python 3.10+, pdfplumber 0.10+ (already installed), anthropic 0.89.0 (already installed), pytest with unittest.mock

---

## Repo Path

- `ENGINE`: `/Users/matthewdickson/projects/multifamily-underwriting`
- Python: `ENGINE/.venv/bin/python`

---

## File Map

| File | Role |
|------|------|
| `engine/ingest/om_extractor.py` (new) | LLM extraction: raw OM text → structured dict |
| `engine/ingest/om_parser.py` (new) | Orchestrates pdfplumber + extractor → `OmData` |
| `tests/test_om_extractor.py` (new) | Tests for LLM extraction with mocked Anthropic |
| `tests/test_om_parser.py` (new) | Tests for parser with mocked extractor + pdfplumber |
| `tests/fixtures/sample_om_text.txt` (new) | Realistic OM text fixture for tests |
| `engine/ingest/ingestion_pipeline.py` (modify) | Add `om_path` param, merge OmData |
| `tests/test_ingestion_pipeline.py` (modify) | Add OM integration tests |
| `runs/ingest_deal.py` (modify) | Add `--om` flag |

---

## Task 1: OmData model and sample fixture

**Files:**
- Create: `ENGINE/tests/fixtures/sample_om_text.txt`
- Create: `ENGINE/engine/ingest/om_parser.py` (OmData only, no parse logic yet)
- Create: `ENGINE/tests/test_om_parser.py` (model tests)

- [ ] **Step 1: Create sample OM text fixture**

Create `ENGINE/tests/fixtures/sample_om_text.txt`:

```
OFFERING MEMORANDUM

CEDAR RIDGE APARTMENTS
2847 Maple Creek Drive, Garland, TX 75041

EXECUTIVE SUMMARY

Cedar Ridge Apartments is a 148-unit garden-style apartment community
located in Garland, Texas (Dallas MSA). Built in 1987, the property
is classified as a Class C asset with significant value-add opportunity.

PROPERTY OVERVIEW

Total Units:          148
Year Built:           1987
Property Class:       C
Building Type:        Garden
Occupancy:            91%
Address:              2847 Maple Creek Drive, Garland, TX 75041
Market:               Dallas

UNIT MIX

Type        Units   Avg SF    Current Rent    Market Rent
--------------------------------------------------------------
1BR/1BA       72     650       $825/mo         $975/mo
2BR/1BA       52     875       $1,025/mo       $1,175/mo
2BR/2BA       24    1,000      $1,150/mo       $1,325/mo
--------------------------------------------------------------
Total        148

INVESTMENT HIGHLIGHTS

- Asking Price: $13,200,000 ($89,189 per unit)
- Current In-Place NOI: $672,000
- Pro Forma NOI (Year 2): $1,124,000
- Value-Add Opportunity: $200/unit rent premium post-renovation

DEAL ECONOMICS

Asking Price:          $13,200,000
Price Per Unit:        $89,189
Projected Hold:        5 years
Exit Cap Rate:         5.75%
Sale Cost:             1.5%

MARKET OVERVIEW

Cedar Ridge is located in Garland, a suburb of Dallas with strong
multifamily fundamentals. The Dallas-Fort Worth MSA recorded 3.2%
rent growth over the trailing twelve months.

RENOVATION PROGRAM

The sponsor intends to implement a standard value-add renovation
program targeting $200/month rent premium per unit at an estimated
cost of $15,000-$17,000 per unit. Renovations include new flooring
(LVP), kitchen cabinet refinishing, granite countertops, updated
fixtures, and in-unit washer/dryer connections.
```

- [ ] **Step 2: Write the OmData model**

Create `ENGINE/engine/ingest/om_parser.py`:

```python
"""OM (Offering Memorandum) PDF parser → canonical deal fields.

Two-step process:
  1. pdfplumber extracts raw text from the PDF
  2. om_extractor.extract_om_data() sends text to Claude Haiku for structured extraction

Output is OmData — a nullable dataclass of deal fields extracted from the OM.
All fields are Optional; callers must treat None as "analyst must provide."

The to_partial_deal() method converts OmData to a partial canonical schema
dict that can be merged into a full deal inputs dict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class OmUnitType:
    unit_type: str          # e.g. "1BR/1BA"
    count: int
    sqft: float
    current_rent: float | None
    market_rent: float | None


@dataclass
class OmData:
    # Property identity
    property_name: str
    address: str | None
    market: str | None
    year_built: int | None
    total_units: int | None
    property_class: str | None      # "A", "B", "C"
    building_type: str | None       # "garden", "midrise", "highrise"

    # Unit mix (supplements rent roll — count/sqft/rents)
    unit_mix: list[OmUnitType] = field(default_factory=list)

    # Deal economics
    asking_price: float | None = None
    price_per_unit: float | None = None

    # Hold / exit assumptions
    hold_years: int | None = None
    exit_cap_rate: float | None = None    # decimal, e.g. 0.0575

    # Extraction metadata
    extraction_confidence: str = "low"   # "high", "medium", "low"
    raw_extracted: dict = field(default_factory=dict)

    def to_partial_deal(self) -> dict[str, Any]:
        """Convert OmData to a partial canonical schema dict.

        Returns only the fields that are populated. The caller is
        responsible for merging this with the full deal inputs.
        """
        out: dict[str, Any] = {}

        if self.property_name:
            out.setdefault("metadata", {})["deal_id"] = self.property_name

        if self.asking_price is not None:
            out["purchase_assumptions"] = {"purchase_price": self.asking_price}

        if self.exit_cap_rate is not None:
            out.setdefault("exit_assumptions", {})["exit_cap_rate"] = self.exit_cap_rate

        if self.year_built is not None or self.total_units is not None:
            props: dict[str, Any] = {}
            if self.year_built is not None:
                props["year_built"] = self.year_built
            if self.total_units is not None:
                props["total_units"] = self.total_units
            if self.property_class is not None:
                props["property_class"] = self.property_class
            if self.building_type is not None:
                props["building_type"] = self.building_type
            if self.market is not None:
                props["market"] = self.market
            if props:
                out["property_summary"] = props

        return out
```

- [ ] **Step 3: Write failing model tests**

Create `ENGINE/tests/test_om_parser.py`:

```python
"""Tests for OmData model and to_partial_deal()."""
import pytest
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/matthewdickson/projects/multifamily-underwriting
.venv/bin/pytest tests/test_om_parser.py -v --tb=short 2>&1 | tail -20
```

Expected: 9 tests passing.

- [ ] **Step 5: Commit**

```bash
cd /Users/matthewdickson/projects/multifamily-underwriting
git add engine/ingest/om_parser.py tests/test_om_parser.py tests/fixtures/sample_om_text.txt
git commit -m "Add OmData model and sample OM text fixture"
```

---

## Task 2: LLM extractor (om_extractor.py)

**Files:**
- Create: `ENGINE/engine/ingest/om_extractor.py`
- Create: `ENGINE/tests/test_om_extractor.py`

- [ ] **Step 1: Write failing extractor tests**

Create `ENGINE/tests/test_om_extractor.py`:

```python
"""Tests for om_extractor — LLM extraction with mocked Anthropic client."""
import json
import pytest
from unittest.mock import MagicMock, patch

from engine.ingest.om_extractor import extract_om_data, _build_prompt

SAMPLE_TEXT = open("tests/fixtures/sample_om_text.txt").read()

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
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd /Users/matthewdickson/projects/multifamily-underwriting
.venv/bin/pytest tests/test_om_extractor.py -v 2>&1 | head -15
```

Expected: `ImportError: cannot import name 'extract_om_data'`

- [ ] **Step 3: Implement om_extractor.py**

Create `ENGINE/engine/ingest/om_extractor.py`:

```python
"""LLM-based extraction of structured data from OM text.

Sends raw OM text to Claude Haiku and returns a dict of extracted fields.
All fields are nullable — missing data comes back as null/None.

Isolated from om_parser.py so it can be mocked in tests without
touching pdfplumber or file I/O.
"""

from __future__ import annotations

import json
import re
from typing import Any


_EXTRACTION_SCHEMA = """\
{
  "property_name": "string or null",
  "address": "string or null",
  "market": "city/metro name or null — e.g. 'dallas', 'birmingham', 'phoenix'",
  "year_built": "integer or null",
  "total_units": "integer or null",
  "property_class": "one of 'A', 'B', 'C', 'D' or null",
  "building_type": "one of 'garden', 'midrise', 'highrise', 'townhome', 'mixed' or null",
  "unit_mix": [
    {
      "unit_type": "e.g. '1BR/1BA'",
      "count": "integer",
      "sqft": "number or null",
      "current_rent": "monthly rent as number or null",
      "market_rent": "monthly market rent as number or null"
    }
  ],
  "asking_price": "total purchase price as number or null",
  "price_per_unit": "per-unit price as number or null",
  "hold_years": "projected hold period in years as integer or null",
  "exit_cap_rate": "exit cap rate as decimal (e.g. 0.0575 for 5.75%) or null",
  "extraction_confidence": "one of 'high', 'medium', 'low'"
}"""


def _build_prompt(om_text: str) -> str:
    """Build the extraction prompt for Claude."""
    return f"""\
You are a commercial real estate analyst. Extract structured data from this \
Offering Memorandum text and return ONLY a valid JSON object.

Extract every field you can find. Use null for any field not present in the text.
For exit_cap_rate, convert percentages to decimals (5.75% → 0.0575).
For asking_price, return the full dollar amount as a number (e.g. 13200000).
For unit_mix, create one entry per unit type found in the document.

Return ONLY the JSON object — no explanation, no markdown, no code fences.

Required JSON schema:
{_EXTRACTION_SCHEMA}

Offering Memorandum text:
---
{om_text[:12000]}
---

JSON:"""


def extract_om_data(om_text: str, client: Any) -> dict[str, Any]:
    """Send OM text to Claude Haiku and return extracted fields as dict.

    Args:
        om_text: Raw text extracted from the OM PDF
        client: anthropic.Anthropic client instance

    Returns:
        Dict of extracted fields. Contains "extraction_error" key on failure.
        All deal fields are nullable — missing data comes back as None.
    """
    prompt = _build_prompt(om_text)

    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_text = message.content[0].text.strip()

        # Strip markdown code fences if present
        raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
        raw_text = re.sub(r"\s*```$", "", raw_text)

        return json.loads(raw_text)

    except json.JSONDecodeError as e:
        return {"extraction_error": f"JSON parse failed: {e}", "raw_text": raw_text if "raw_text" in dir() else ""}
    except Exception as e:
        return {"extraction_error": str(e)}
```

- [ ] **Step 4: Run tests**

```bash
cd /Users/matthewdickson/projects/multifamily-underwriting
.venv/bin/pytest tests/test_om_extractor.py -v --tb=short 2>&1 | tail -20
```

Expected: 11 tests passing.

- [ ] **Step 5: Commit**

```bash
cd /Users/matthewdickson/projects/multifamily-underwriting
git add engine/ingest/om_extractor.py tests/test_om_extractor.py
git commit -m "Add LLM-based OM data extractor with mocked tests"
```

---

## Task 3: parse_om() — PDF → OmData

**Files:**
- Modify: `ENGINE/engine/ingest/om_parser.py` (add `parse_om()`)
- Modify: `ENGINE/tests/test_om_parser.py` (add parse_om tests)

- [ ] **Step 1: Write failing parse_om tests**

Add to `ENGINE/tests/test_om_parser.py`:

```python
# Add these imports at the top:
import json
from unittest.mock import MagicMock, patch

# Add these test classes after the existing ones:

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

    def test_txt_file_skips_pdfplumber(self, tmp_path):
        from engine.ingest.om_parser import parse_om
        txt = tmp_path / "test.txt"
        txt.write_text("plain text OM")
        with patch("engine.ingest.om_parser.extract_om_data", return_value=CANNED_EXTRACTED):
            result = parse_om(txt, client=self._mock_client())
        assert result.property_name == "Cedar Ridge Apartments"
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd /Users/matthewdickson/projects/multifamily-underwriting
.venv/bin/pytest tests/test_om_parser.py::TestParseOm -v 2>&1 | head -15
```

Expected: `ImportError: cannot import name 'parse_om'`

- [ ] **Step 3: Implement parse_om() in om_parser.py**

Add to `ENGINE/engine/ingest/om_parser.py` (append after the `OmData` class):

```python
from engine.ingest.om_extractor import extract_om_data


def parse_om(
    path: str | Path,
    client: Any = None,
    api_key: str | None = None,
) -> OmData:
    """Parse an Offering Memorandum PDF (or text file) into OmData.

    Args:
        path: Path to .pdf or .txt OM file
        client: Optional anthropic.Anthropic client. If None, creates one
                from ANTHROPIC_API_KEY environment variable.
        api_key: Override for ANTHROPIC_API_KEY (optional)

    Returns:
        OmData with all extractable fields populated. Missing fields are None.
        On extraction failure, returns minimal OmData with extraction_confidence="low".
    """
    import os

    path = Path(path)

    if client is None:
        import anthropic
        key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        client = anthropic.Anthropic(api_key=key)

    text = _extract_pdf_text(path)
    raw = extract_om_data(text, client)

    if "extraction_error" in raw:
        return OmData(
            property_name=path.stem,
            address=None,
            market=None,
            year_built=None,
            total_units=None,
            property_class=None,
            building_type=None,
            extraction_confidence="low",
            raw_extracted=raw,
        )

    unit_mix = [
        OmUnitType(
            unit_type=u.get("unit_type", "Unknown"),
            count=int(u.get("count") or 0),
            sqft=float(u.get("sqft") or 0),
            current_rent=_to_float(u.get("current_rent")),
            market_rent=_to_float(u.get("market_rent")),
        )
        for u in raw.get("unit_mix") or []
        if u.get("count")
    ]

    return OmData(
        property_name=raw.get("property_name") or path.stem,
        address=raw.get("address"),
        market=raw.get("market"),
        year_built=_to_int(raw.get("year_built")),
        total_units=_to_int(raw.get("total_units")),
        property_class=raw.get("property_class"),
        building_type=raw.get("building_type"),
        unit_mix=unit_mix,
        asking_price=_to_float(raw.get("asking_price")),
        price_per_unit=_to_float(raw.get("price_per_unit")),
        hold_years=_to_int(raw.get("hold_years")),
        exit_cap_rate=_to_float(raw.get("exit_cap_rate")),
        extraction_confidence=raw.get("extraction_confidence", "medium"),
        raw_extracted=raw,
    )


def _extract_pdf_text(path: Path) -> str:
    """Extract text from PDF using pdfplumber, or read directly for .txt files."""
    if path.suffix.lower() in (".txt", ".md"):
        return path.read_text(encoding="utf-8")

    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            pages = []
            for page in pdf.pages[:30]:  # cap at 30 pages
                text = page.extract_text()
                if text:
                    pages.append(text)
            return "\n\n".join(pages)
    except Exception as e:
        return f"[PDF extraction failed: {e}]"


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return None


def _to_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(float(str(v).strip()))
    except (ValueError, TypeError):
        return None
```

Also add `from typing import Any` and `from pathlib import Path` to the top of the file (they're already imported via the dataclass section — confirm they're present after adding).

- [ ] **Step 4: Run all om_parser tests**

```bash
cd /Users/matthewdickson/projects/multifamily-underwriting
.venv/bin/pytest tests/test_om_parser.py -v --tb=short 2>&1 | tail -25
```

Expected: 16 tests passing.

- [ ] **Step 5: Commit**

```bash
cd /Users/matthewdickson/projects/multifamily-underwriting
git add engine/ingest/om_parser.py tests/test_om_parser.py
git commit -m "Add parse_om(): PDF/text → OmData with pdfplumber + Claude Haiku"
```

---

## Task 4: Integrate OM into ingestion pipeline

**Files:**
- Modify: `ENGINE/engine/ingest/ingestion_pipeline.py`
- Modify: `ENGINE/tests/test_ingestion_pipeline.py`

- [ ] **Step 1: Write failing integration tests**

Add to `ENGINE/tests/test_ingestion_pipeline.py`:

```python
# Add these imports at the top of the file:
from unittest.mock import patch, MagicMock
from engine.ingest.om_parser import OmData, OmUnitType

# Add this helper:
def _sample_om(**kwargs) -> OmData:
    defaults = dict(
        property_name="Cedar Ridge Apartments",
        address="2847 Maple, Garland TX",
        market="dallas",
        year_built=1987,
        total_units=148,
        property_class="C",
        building_type="garden",
        asking_price=13_200_000,
        exit_cap_rate=0.0575,
        hold_years=5,
        unit_mix=[
            OmUnitType("1BR/1BA", 72, 650, 825, 975),
            OmUnitType("2BR/1BA", 52, 875, 1025, 1175),
        ],
        extraction_confidence="high",
    )
    defaults.update(kwargs)
    return OmData(**defaults)


# Add this test class:
class TestBuildDealWithOm:
    def test_purchase_price_from_om(self):
        with patch("engine.ingest.ingestion_pipeline.parse_om", return_value=_sample_om()):
            result = build_deal_from_documents(
                property_id="Cedar Ridge",
                rent_roll_path=ROLL,
                t12_path=T12,
                analysis_start="2026-05",
                analysis_end="2031-04",
                om_path="fake_om.pdf",
            )
        assert result["purchase_assumptions"]["purchase_price"] == 13_200_000

    def test_exit_cap_from_om(self):
        with patch("engine.ingest.ingestion_pipeline.parse_om", return_value=_sample_om()):
            result = build_deal_from_documents(
                property_id="Cedar Ridge",
                rent_roll_path=ROLL,
                t12_path=T12,
                analysis_start="2026-05",
                analysis_end="2031-04",
                om_path="fake_om.pdf",
            )
        assert result["exit_assumptions"]["exit_cap_rate"] == pytest.approx(0.0575)

    def test_no_om_leaves_sections_absent(self):
        result = build_deal_from_documents(
            property_id="Test",
            rent_roll_path=ROLL,
            t12_path=T12,
            analysis_start="2026-05",
            analysis_end="2031-04",
        )
        # Without OM, purchase_assumptions should be absent
        assert "purchase_assumptions" not in result

    def test_om_data_logged_in_metadata(self):
        with patch("engine.ingest.ingestion_pipeline.parse_om", return_value=_sample_om()):
            result = build_deal_from_documents(
                property_id="Cedar Ridge",
                rent_roll_path=ROLL,
                t12_path=T12,
                analysis_start="2026-05",
                analysis_end="2031-04",
                om_path="fake_om.pdf",
            )
        assert result["metadata"].get("om_extraction_confidence") == "high"
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd /Users/matthewdickson/projects/multifamily-underwriting
.venv/bin/pytest tests/test_ingestion_pipeline.py::TestBuildDealWithOm -v 2>&1 | head -15
```

Expected: `TypeError: build_deal_from_documents() got an unexpected keyword argument 'om_path'`

- [ ] **Step 3: Modify ingestion_pipeline.py to accept om_path**

In `ENGINE/engine/ingest/ingestion_pipeline.py`, update `build_deal_from_documents`:

```python
def build_deal_from_documents(
    property_id: str,
    rent_roll_path: str | Path,
    t12_path: str | Path,
    analysis_start: str,
    analysis_end: str,
    rent_growth_rate: float = 0.03,
    vacancy_rate_override: float | None = None,
    collection_loss_rate: float = 0.005,
    analyst: str = "",
    om_path: str | Path | None = None,            # NEW
    anthropic_client: Any | None = None,           # NEW
) -> dict[str, Any]:
    """Build a canonical deal JSON from a rent roll, T12, and optional OM.

    Args:
        property_id: Deal identifier / deal_id for metadata
        rent_roll_path: Path to rent roll CSV/Excel
        t12_path: Path to T12 CSV/Excel
        analysis_start: Analysis start month "YYYY-MM"
        analysis_end: Analysis end month "YYYY-MM"
        rent_growth_rate: Annual rent growth assumption (default 3%)
        vacancy_rate_override: Override physical_vacancy_rate from rent roll
        collection_loss_rate: Collection loss rate (default 0.5%)
        analyst: Analyst name for metadata
        om_path: Optional path to OM PDF or text file. When provided,
                 extracts purchase_price, exit_cap_rate, and property
                 summary via Claude Haiku.
        anthropic_client: Optional pre-built Anthropic client. If None
                 and om_path is provided, creates one from ANTHROPIC_API_KEY.

    Returns:
        Canonical deal inputs dict (schema_version 0.1)
    """
    from engine.modules.time_grid import TimeGrid
    from engine.ingest.t12_parser import parse_t12
    from engine.ingest.rent_roll_parser import parse_rent_roll

    tg = TimeGrid.build(analysis_start, analysis_end)
    roll = parse_rent_roll(rent_roll_path)
    opex_table = parse_t12(t12_path)

    unit_cohorts = roll["unit_cohorts"]
    vacancy_rate = vacancy_rate_override if vacancy_rate_override is not None else roll["physical_vacancy_rate"]
    market_rents = roll.get("market_rent_by_cohort", {})
    start = tg.month_ids[0]
    end = tg.month_ids[-1]

    deal = {
        "schema_version": "0.1",
        "metadata": {
            "deal_id": property_id,
            "run_id": "ingested",
            "as_of_date": start,
            "analyst": analyst,
            "purpose": "Document Ingestion",
        },
        "time_grid": {
            "analysis_start_date": f"{analysis_start}-01",
            "analysis_end_date": f"{analysis_end}-01",
        },
        "unit_cohorts": unit_cohorts,
        "market_rent_curve": _build_market_rent_curve(unit_cohorts, market_rents, start, end),
        "loss_to_lease": _build_simple_curve(unit_cohorts, "ltl_pct", 0.0, start, end),
        "physical_vacancy_curve": _build_simple_curve(unit_cohorts, "vacancy_pct", vacancy_rate, start, end),
        "collection_loss_curve": [
            {"applies_to": "ALL", "segments": [{"start_month": start, "end_month": end, "loss_pct": collection_loss_rate}]}
        ],
        "revenue_programs": [],
        "program_adoption_curve": [],
        "opex_table": opex_table,
        "growth_assumptions": {
            "rent_growth_type": "flat",
            "annual_growth_rate": rent_growth_rate,
        },
    }

    # Merge OM data if provided
    if om_path is not None:
        from engine.ingest.om_parser import parse_om
        om = parse_om(om_path, client=anthropic_client)
        partial = om.to_partial_deal()

        if "purchase_assumptions" in partial:
            deal["purchase_assumptions"] = partial["purchase_assumptions"]
        if "exit_assumptions" in partial:
            deal["exit_assumptions"] = partial["exit_assumptions"]
        if "property_summary" in partial:
            deal["metadata"]["property_summary"] = partial["property_summary"]
        deal["metadata"]["om_extraction_confidence"] = om.extraction_confidence

    return deal
```

Also add `from typing import Any` if not already present at the top of `ingestion_pipeline.py`.

- [ ] **Step 4: Run OM integration tests**

```bash
cd /Users/matthewdickson/projects/multifamily-underwriting
.venv/bin/pytest tests/test_ingestion_pipeline.py -v --tb=short 2>&1 | tail -25
```

Expected: 16 tests passing (12 existing + 4 new).

- [ ] **Step 5: Commit**

```bash
cd /Users/matthewdickson/projects/multifamily-underwriting
git add engine/ingest/ingestion_pipeline.py tests/test_ingestion_pipeline.py
git commit -m "Integrate OM parsing into ingestion pipeline (purchase_price, exit_cap)"
```

---

## Task 5: Add --om flag to ingest_deal.py CLI

**Files:**
- Modify: `ENGINE/runs/ingest_deal.py`

- [ ] **Step 1: Add --om argument**

In `ENGINE/runs/ingest_deal.py`, add after the `--collection-loss` argument:

```python
    p.add_argument("--om", type=Path, default=None,
                   help="Path to Offering Memorandum PDF (optional). Extracts purchase price, "
                        "exit cap, unit mix via Claude Haiku. Requires ANTHROPIC_API_KEY.")
```

- [ ] **Step 2: Pass om_path to build_deal_from_documents**

In the `result = build_deal_from_documents(...)` call in `main()`, add the new keyword argument:

```python
    result = build_deal_from_documents(
        property_id=args.property_id,
        rent_roll_path=args.rent_roll,
        t12_path=args.t12,
        analysis_start=args.start,
        analysis_end=args.end,
        rent_growth_rate=args.rent_growth,
        vacancy_rate_override=args.vacancy,
        collection_loss_rate=args.collection_loss,
        analyst=args.analyst,
        om_path=args.om,
    )
```

- [ ] **Step 3: Print OM summary in output**

After the existing print statements at the end of `main()`, add:

```python
    if args.om:
        confidence = result["metadata"].get("om_extraction_confidence", "unknown")
        purchase = result.get("purchase_assumptions", {}).get("purchase_price")
        exit_cap = result.get("exit_assumptions", {}).get("exit_cap_rate")
        print(f"  OM parsed (confidence: {confidence})")
        if purchase:
            print(f"    Purchase price: ${purchase:,.0f}")
        if exit_cap:
            print(f"    Exit cap: {exit_cap:.2%}")
```

- [ ] **Step 4: Test the CLI manually with the text fixture**

```bash
cd /Users/matthewdickson/projects/multifamily-underwriting
ANTHROPIC_API_KEY="test" .venv/bin/python runs/ingest_deal.py \
  --property-id "Cedar Ridge Apartments" \
  --rent-roll tests/fixtures/sample_rent_roll.csv \
  --t12 tests/fixtures/sample_t12.csv \
  --start 2026-05 --end 2031-04 \
  --om tests/fixtures/sample_om_text.txt \
  --output /tmp/cedar_ridge_test.json 2>&1
```

Expected output (values from real LLM extraction — requires ANTHROPIC_API_KEY):
```
Ingesting documents for: Cedar Ridge Apartments
  Rent roll: tests/fixtures/sample_rent_roll.csv
  T12:       tests/fixtures/sample_t12.csv
  Period:    2026-05 → 2031-04
  OM parsed (confidence: high)
    Purchase price: $13,200,000
    Exit cap: 5.75%
...
```

Note: if `ANTHROPIC_API_KEY` is not set or invalid, the OM section will show `confidence: low` but the rest of ingestion will complete normally.

- [ ] **Step 5: Commit**

```bash
cd /Users/matthewdickson/projects/multifamily-underwriting
git add runs/ingest_deal.py
git commit -m "Add --om flag to ingest_deal CLI for OM PDF parsing"
```

---

## Task 6: Full test suite verification

**Files:** None (verification only)

- [ ] **Step 1: Run full test suite**

```bash
cd /Users/matthewdickson/projects/multifamily-underwriting
.venv/bin/pytest tests/ -q --tb=short 2>&1 | tail -10
```

Expected: 287 + ~27 new = ~314 tests passing.

- [ ] **Step 2: Test with real OM if ANTHROPIC_API_KEY is available**

If `ANTHROPIC_API_KEY` is set:

```bash
cd /Users/matthewdickson/projects/multifamily-underwriting
.venv/bin/python -c "
from engine.ingest.om_parser import parse_om
from pathlib import Path
result = parse_om(Path('tests/fixtures/sample_om_text.txt'))
print('property_name:', result.property_name)
print('total_units:', result.total_units)
print('asking_price:', result.asking_price)
print('exit_cap_rate:', result.exit_cap_rate)
print('unit_mix:', [(u.unit_type, u.count, u.sqft) for u in result.unit_mix])
print('confidence:', result.extraction_confidence)
"
```

Expected:
```
property_name: Cedar Ridge Apartments
total_units: 148
asking_price: 13200000.0
exit_cap_rate: 0.0575
unit_mix: [('1BR/1BA', 72, 650.0), ('2BR/1BA', 52, 875.0), ('2BR/2BA', 24, 1000.0)]
confidence: high
```

- [ ] **Step 3: Final commit if any cleanup needed**

```bash
cd /Users/matthewdickson/projects/multifamily-underwriting
git status
# If any files untracked/modified:
git add -p  # review each change
git commit -m "OM PDF parser: full test suite passing"
```

---

## Self-Review

**Spec coverage:**
- ✅ pdfplumber for PDF text extraction (`_extract_pdf_text`)
- ✅ Claude Haiku for structured data extraction (`om_extractor.py`)
- ✅ OmData dataclass with all deal fields (`om_parser.py`)
- ✅ Nullable fields — best-effort, no hard failures
- ✅ `to_partial_deal()` for canonical schema merge
- ✅ Integrated into `build_deal_from_documents()` via `om_path`
- ✅ `--om` CLI flag in `ingest_deal.py`
- ✅ All tests mocked — no API key required for test suite
- ✅ Graceful fallback when extraction fails (returns low-confidence OmData)

**Gaps intentionally deferred:**
- OM-derived unit mix is not currently merged into `unit_cohorts` — rent roll takes precedence. A future task could compare OM unit mix vs rent roll and warn on discrepancies.
- No table extraction — pdfplumber's table extraction could improve unit mix parsing for structured PDFs. Left for iteration after basic text extraction is validated.

**Type consistency check:** All methods match across tasks:
- `parse_om(path, client, api_key) -> OmData` ✅
- `extract_om_data(om_text, client) -> dict` ✅
- `OmData.to_partial_deal() -> dict` ✅
- `build_deal_from_documents(..., om_path, anthropic_client) -> dict` ✅
