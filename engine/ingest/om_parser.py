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
import re


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

        # Bug 1.5 fix — emit OM-extracted unit mix under a non-canonical key so
        # the engine ignores it but downstream tools (intake-orchestrator,
        # deal-intake) can compare against the rent roll. The unit_mix is the
        # most expensive thing the LLM extraction produced; previously dropped.
        if self.unit_mix:
            out["om_unit_mix_evidence"] = [
                {
                    "unit_type": u.unit_type,
                    "count": u.count,
                    "sqft": u.sqft,
                    "current_rent": u.current_rent,
                    "market_rent": u.market_rent,
                }
                for u in self.unit_mix
            ]

        if isinstance(self.raw_extracted, dict) and "_provenance" in self.raw_extracted:
            out["_provenance"] = self.raw_extracted["_provenance"]

        return out


from engine.ingest.om_extractor import extract_om_data  # noqa: E402

# Langextract path is imported lazily inside parse_om() to avoid circular
# imports — extractor_translation imports OmData from this module.
def extract_deal_from_text(*args, **kwargs):  # pragma: no cover — patched in tests
    from engine.extraction.extractor import extract_deal_from_text as _impl
    return _impl(*args, **kwargs)


def parse_om(
    path: str | Path,
    client: Any = None,
    api_key: str | None = None,
    use_langextract: bool = False,
    langextract_model_id: str | None = None,
) -> OmData:
    """Parse an Offering Memorandum PDF (or text file) into OmData.

    Args:
        path: Path to .pdf or .txt OM file
        client: Optional anthropic.Anthropic client. If None, creates one
                from ANTHROPIC_API_KEY environment variable.
        api_key: Override for ANTHROPIC_API_KEY (optional)
        use_langextract: When True, route extraction through the source-grounded
                         langextract pipeline (engine.extraction). Default False
                         preserves the existing Anthropic-Haiku path.
        langextract_model_id: Explicit model id for the langextract path.
                             Required unless LANGEXTRACT_MODEL_ID is set.

    Returns:
        OmData with all extractable fields populated. Missing fields are None.
        On extraction failure, returns minimal OmData with extraction_confidence="low".
    """
    import os

    path = Path(path)

    text = _extract_pdf_text(path)

    if use_langextract:
        from engine.extraction.extractor_translation import extracted_deal_to_om_data
        try:
            extracted = extract_deal_from_text(text, model_id=langextract_model_id)
        except Exception as e:
            return OmData(
                property_name=path.stem,
                address=None,
                market=None,
                year_built=None,
                total_units=None,
                property_class=None,
                building_type=None,
                extraction_confidence="low",
                raw_extracted={"extraction_error": str(e)},
            )
        return extracted_deal_to_om_data(extracted, property_name=path.stem)

    if client is None:
        import anthropic
        key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        client = anthropic.Anthropic(api_key=key)

    # V1.3 — om_extractor no longer truncates at 12k chars; the model
    # (claude-haiku-4-5-20251001) supports a 1M token context so we send
    # the full OM text. Large broker OMs (large production OMs)
    # used to lose their financial summary + debt assumability tail.
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
        year_built=_normalize_year_built(_to_int(raw.get("year_built")), text),
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
            # Bug 1.10 fix — read ALL pages (no silent 30-page cap). Long
            # Berkadia OMs (e.g. a production deal needs pages 14-40) had
            # their financial summary, debt assumability, and rent comp
            # tables silently truncated under the previous cap.
            for page in pdf.pages:
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


def _normalize_year_built(year: int | None, text: str | None = None) -> int | None:
    if year is not None and 1800 <= year <= 2100:
        return year
    if not text:
        return None
    direct_match = re.search(r"YEAR BUILT\s*\n\s*((?:19|20)\d{2})", text, re.IGNORECASE)
    if direct_match:
        return int(direct_match.group(1))
    inline_match = re.search(r"Year Built[:\s]+((?:19|20)\d{2})", text, re.IGNORECASE)
    if inline_match:
        return int(inline_match.group(1))
    return None
