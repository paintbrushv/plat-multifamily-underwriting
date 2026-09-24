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
    """Build the extraction prompt for Claude.

    V1.3: removed the 12k truncation slice. Claude Haiku 4.5 supports a
    1M-token context (`claude-haiku-4-5-20251001`), so the full OM text
    is now sent in one call. Long Berkadia OMs (a 12MB\+ production OM, a long production OM
    Building, a production deal) previously had their financial summary,
    debt assumability, and rent comp tables silently dropped at the 12k
    cap. Callers that need chunking can call `extract_om_data` directly
    on per-chunk slices.
    """
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
{om_text}
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
    raw_text = ""

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
        return {"extraction_error": f"JSON parse failed: {e}", "raw_text": raw_text}
    except Exception as e:
        return {"extraction_error": str(e)}
