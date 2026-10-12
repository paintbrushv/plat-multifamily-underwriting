"""Assumption registry entries with provenance (ADR 0003 §2)."""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

RESOLVER_VERSION = "plat.assumptions/1"
MODES = ("underwrite", "pricing")

# Source vocabulary (ADR 0003 §2).
SOURCE_ANALYST = "analyst"
SOURCE_DEAL_RENT_ROLL = "deal:rent_roll"
SOURCE_DEAL_T12 = "deal:t12"
SOURCE_DEAL_OM = "deal:om"
SOURCE_ENGINE_FALLBACK = "engine_fallback"
DEAL_PREFIX = "deal:"
MARKET_PREFIX = "market:"
HOUSE_PREFIX = "house:"

# House rules referenced by entries (`house:<rule>`).
HOUSE_BACKSOLVE_POLICY = "house:backsolve_policy"
HOUSE_YEAR1_INSURANCE_FLOOR = "house:year1_insurance_floor"


def house_vintage_source(bucket: str) -> str:
    """``house:vintage_1980s`` for the VINTAGE_FRAMEWORK_2026 bucket ``1980s``."""
    return f"{HOUSE_PREFIX}vintage_{bucket}"


def market_source(provider: str) -> str:
    return f"{MARKET_PREFIX}{provider}"


SOURCE_PATTERN = re.compile(
    r"^(analyst|engine_fallback|deal:[a-z0-9_]+|market:[A-Za-z0-9_.\-]+|house:[a-z0-9_]+)$"
)
CONFIDENCE_LEVELS = ("high", "medium", "low")


def is_valid_source(source: str) -> bool:
    return bool(isinstance(source, str) and SOURCE_PATTERN.match(source))


def requires_review(source: str) -> bool:
    """House defaults and engine fallbacks are review prompts (ADR 0003 §5)."""
    return source == SOURCE_ENGINE_FALLBACK or source.startswith(HOUSE_PREFIX)


@dataclass
class AssumptionEntry:
    value: Any
    unit: str
    source: str
    basis: str
    as_of: Optional[str] = None
    confidence: Optional[str] = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    default_value: Any = None
    override: Optional[dict[str, Any]] = None
    review_required: bool = False
    cohort_overrides: Optional[dict[str, Any]] = None

    def __post_init__(self) -> None:
        if not is_valid_source(self.source):
            raise ValueError(f"unknown assumption source {self.source!r}")
        if self.confidence is not None and self.confidence not in CONFIDENCE_LEVELS:
            raise ValueError(f"confidence must be one of {CONFIDENCE_LEVELS}")
        if self.override is not None and "value" not in self.override:
            raise ValueError("override must carry a value")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "value": deepcopy(self.value),
            "unit": self.unit,
            "source": self.source,
            "basis": self.basis,
            "as_of": self.as_of,
            "confidence": self.confidence,
            "candidates": deepcopy(self.candidates),
            "default_value": deepcopy(self.default_value),
            "override": deepcopy(self.override),
            "review_required": self.review_required,
        }
        if self.cohort_overrides:
            out["cohort_overrides"] = deepcopy(self.cohort_overrides)
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AssumptionEntry":
        return cls(
            value=deepcopy(data.get("value")),
            unit=data["unit"],
            source=data["source"],
            basis=data.get("basis", ""),
            as_of=data.get("as_of"),
            confidence=data.get("confidence"),
            candidates=deepcopy(list(data.get("candidates") or [])),
            default_value=deepcopy(data.get("default_value")),
            override=deepcopy(data.get("override")),
            review_required=bool(data.get("review_required", False)),
            cohort_overrides=deepcopy(data.get("cohort_overrides")) or None,
        )
