"""
Brand Constants
===============
Centralized brand color definitions used across all output generators
(PDF, PPTX, XLSX, AM overlay). Colors are defaults; override `BrandConfig`
for your own branding.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional


# ── Hex strings (for openpyxl and other non-lib contexts) ────────────────

HEX_GOLD = "C9A66C"
HEX_GOLD_LIGHT = "F5EDE0"
HEX_DARK_900 = "0A0A0A"
HEX_DARK_800 = "111111"
HEX_DARK_700 = "333333"
HEX_DARK_500 = "666666"
HEX_WHITE = "FFFFFF"
HEX_LIGHT_GRAY = "F5F5F5"
HEX_GREEN_BG = "E8F5E9"
HEX_RED_BG = "FFEBEE"


# ── Matplotlib color palette ─────────────────────────────────────────────

MPL_DARK_900 = "#0a0a0a"
MPL_DARK_800 = "#111111"
MPL_GOLD = "#c9a66c"
MPL_GOLD_DARK = "#a68b52"
MPL_SLATE_500 = "#64748b"
MPL_SLATE_200 = "#e2e8f0"
MPL_GRAY = "#333333"


# ── JV Co-Brand Support ─────────────────────────────────────────────────

@dataclass
class BrandConfig:
    """Brand configuration for output generators.

    When partner_name is set, outputs include co-branding:
    - PDF: partner logo top-right, "Prepared for [Partner Name]" subtitle
    - PPTX: partner logo on cover, partner name in all slide footers
    - Excel: header includes "Prepared by [Company] for [Partner Name]"

    When partner_name is None, outputs are company-only (backward compatible).
    """
    company_name: str = "ExampleSponsor Capital"
    primary_color: str = HEX_GOLD
    partner_name: Optional[str] = None
    partner_logo_path: Optional[Path] = None

    @property
    def has_partner(self) -> bool:
        return self.partner_name is not None

    @property
    def header_text(self) -> str:
        """Text for Excel/PDF headers."""
        if self.has_partner:
            return f"Prepared by {self.company_name} for {self.partner_name}"
        return self.company_name

    @property
    def subtitle_text(self) -> str:
        """Subtitle text for PDF/PPTX."""
        if self.has_partner:
            return f"Prepared for {self.partner_name}"
        return ""

    @classmethod
    def from_inputs(cls, inputs: Dict[str, Any]) -> "BrandConfig":
        """Build BrandConfig from deal inputs (fund_assumptions)."""
        fund = inputs.get("fund_assumptions", {})
        partner_name = fund.get("jv_partner_name")
        logo_path_str = fund.get("jv_partner_logo_path")
        logo_path = Path(logo_path_str) if logo_path_str else None
        return cls(
            partner_name=partner_name,
            partner_logo_path=logo_path,
        )
