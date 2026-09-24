"""Metro / region inference for deal inputs.

Single source of truth for mapping a deal's city/address fields to
canonical metro names. Replaces three duplicate implementations
formerly in portfolio.py, persistence.py, and market_automation.py.

The keyword lists below are the UNION of the three prior implementations
(see git history of those files). Conflicts noted:

- Houston: market_automation.py previously included extra keywords
  ("spring", "the woodlands", "baytown") not present in portfolio.py /
  persistence.py. These are preserved in the unified list.
- Sherman-Denison: market_automation.py defined a slug mapping for
  "Sherman-Denison" but had no keyword list. We add it here as a
  display-name -> slug entry only (no keywords yet); callers can populate
  via metadata.metro explicitly.
- portfolio.py / persistence.py returned "Other" for unknown cases;
  market_automation.resolve_metro_slug() returned None. The new helper
  always returns a display name (with "Other" as the fallback). Callers
  that need slug-or-None can map "Other" -> None themselves.
"""

from __future__ import annotations

from typing import Mapping, Optional

# Display name -> slug (for callers that need the slug form).
METRO_DISPLAY_TO_SLUG: dict[str, str] = {
    "DFW": "dallas_tx",
    "Austin": "austin_tx",
    "Birmingham": "birmingham_al",
    "San Antonio": "san_antonio_tx",
    "Houston": "houston_tx",
    "Sherman-Denison": "sherman_denison_tx",
    "Other": "other",
}

# Keyword lists per metro (lowercase). UNION of the three prior implementations.
_METRO_KEYWORDS: dict[str, tuple[str, ...]] = {
    "DFW": (
        "dallas", "fort worth", "arlington", "plano", "irving",
        "garland", "mesquite", "denton", "frisco", "mckinney",
    ),
    "Austin": (
        "austin", "round rock", "cedar park", "pflugerville",
        "georgetown", "san marcos", "kyle", "buda",
    ),
    "Birmingham": (
        "birmingham", "hoover", "vestavia", "homewood",
        "mountain brook", "trussville", "bessemer",
    ),
    "San Antonio": (
        "san antonio", "new braunfels", "schertz", "converse",
    ),
    "Houston": (
        # union: portfolio.py + persistence.py had {katy, sugar land, pearland, pasadena};
        # market_automation.py added {spring, the woodlands, baytown}
        "houston", "katy", "sugar land", "spring", "the woodlands",
        "pearland", "pasadena", "baytown",
    ),
}


def infer_metro(inputs: Mapping) -> str:
    """Infer a canonical metro display name from deal inputs.

    Resolution order:
    1. metadata.metro IF it's a known canonical display name (one of
       METRO_DISPLAY_TO_SLUG keys). Non-canonical values like
       "Dallas, TX" or "Custom Metro" are ignored so that the
       address-based fallback can still classify the deal correctly.
    2. Keyword match on metadata.city + metadata.address_1 + metadata.address_2
    3. "Other" fallback

    Always returns a display name (never None).
    """
    meta = inputs.get("metadata", {}) if inputs else {}

    # Explicit metro field wins ONLY if it's already a canonical name.
    # Non-canonical values fall through so address keywords still work.
    explicit = meta.get("metro")
    if explicit and explicit in METRO_DISPLAY_TO_SLUG:
        return explicit

    city = (meta.get("city") or "").lower()
    address_1 = (meta.get("address_1") or "").lower()
    address_2 = (meta.get("address_2") or "").lower()
    combined = f"{city} {address_1} {address_2}"

    for display_name, keywords in _METRO_KEYWORDS.items():
        if any(kw in combined for kw in keywords):
            return display_name

    return "Other"


def infer_metro_slug(inputs: Mapping) -> Optional[str]:
    """Convenience: infer metro and return its slug, or None if Other/unmapped.

    Mirrors the legacy market_automation.resolve_metro_slug() return shape
    (returns None when the metro can't be resolved to a known slug).
    """
    display = infer_metro(inputs)
    if display == "Other":
        return None
    return METRO_DISPLAY_TO_SLUG.get(display)
