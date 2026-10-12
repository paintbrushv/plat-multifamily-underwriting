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
SOURCE_DEAL_COMPS = "deal:comps"
# A property condition assessment (capital_plan.pca): immediate needs and recurring capex.
SOURCE_DEAL_PCA = "deal:pca"
SOURCE_ENGINE_FALLBACK = "engine_fallback"
DEAL_PREFIX = "deal:"
MARKET_PREFIX = "market:"
HOUSE_PREFIX = "house:"

# House rules referenced by entries (`house:<rule>`).
HOUSE_BACKSOLVE_POLICY = "house:backsolve_policy"
HOUSE_PRICING_NORMALIZATION = "house:pricing_normalization"
HOUSE_YEAR1_INSURANCE_FLOOR = "house:year1_insurance_floor"
HOUSE_EXIT_CAP_SPREAD = "house:exit_cap_spread"
# HOUSE_REPLACEMENT_RESERVE_PER_UNIT: the lender-style reserve below NOI.
HOUSE_REPLACEMENT_RESERVE = "house:replacement_reserve"
# default_recurring_capex_per_unit: the vintage economic CapEx band (low /
# midpoint / high by deal type and deferred maintenance), recurring capex below NOI.
HOUSE_RECURRING_CAPEX = "house:recurring_capex_vintage"
# HOUSE_IMMEDIATE_CAPEX_DEFAULT: nothing budgeted at close without PCA or analyst evidence.
HOUSE_DEFERRED_CAPEX = "house:deferred_capex"
# default_deferred_capex_placeholder_per_unit: deferred maintenance flagged with no
# PCA and no analyst figure, one year of the vintage band high end at close.
HOUSE_DEFERRED_CAPEX_PLACEHOLDER = "house:deferred_capex_placeholder"
# The R&M floor's payroll review (r_and_m_payroll_credit): payroll earns a credit
# toward the R&M floor only with labeled maintenance payroll above the benchmark.
HOUSE_R_AND_M_PAYROLL_REVIEW = "house:r_and_m_payroll_review"
# House sources that disclose a review rather than set a default to confirm:
# they are flagged only when the resolver finds the case ambiguous.
REVIEW_ON_FINDING_SOURCES = frozenset({HOUSE_R_AND_M_PAYROLL_REVIEW})
# HOUSE_LOAN_SIZING_NOI_BASIS: loan sizing and DSCR on NOI less the below-NOI capex charge.
HOUSE_LOAN_SIZING_NOI_BASIS = "house:loan_sizing_noi_basis"


def house_vintage_source(bucket: str) -> str:
    """``house:vintage_1980s`` for the VINTAGE_FRAMEWORK_2026 bucket ``1980s``."""
    return f"{HOUSE_PREFIX}vintage_{bucket}"


def market_source(provider: str) -> str:
    return f"{MARKET_PREFIX}{provider}"


SOURCE_PATTERN = re.compile(
    r"^(analyst|engine_fallback|deal:[a-z0-9_]+|market:[A-Za-z0-9_.\-]+|house:[a-z0-9_]+)$"
)
CONFIDENCE_LEVELS = ("high", "medium", "low")


# Optional edit metadata an override may carry (ADR 0003 §8): who made the
# edit and on what date. Only a caller supplies them; resolution never reads
# the clock, and an override without them stays exactly as before.
OVERRIDE_METADATA_FIELDS = ("edited_by", "edited_at")
_EDITED_BY_MAX_LENGTH = 120


def override_metadata(raw: Mapping[str, Any]) -> dict[str, str]:
    """Validated ``edited_by`` / ``edited_at`` from an override object.

    ``edited_by`` is a non-blank string (trimmed). ``edited_at`` is an ISO
    date (``YYYY-MM-DD``; a ``date`` or an ISO datetime string is cut to its
    date). Absent or null fields are omitted. Raises ``ValueError`` on
    anything else.
    """
    from datetime import date, datetime

    out: dict[str, str] = {}
    editor = raw.get("edited_by")
    if editor is not None:
        if not isinstance(editor, str) or not editor.strip():
            raise ValueError("edited_by must be a non-blank string")
        editor = " ".join(editor.split())
        if len(editor) > _EDITED_BY_MAX_LENGTH:
            raise ValueError(f"edited_by must be at most {_EDITED_BY_MAX_LENGTH} characters")
        out["edited_by"] = editor
    when = raw.get("edited_at")
    if when is not None:
        if isinstance(when, datetime):
            when = when.date()
        if isinstance(when, date):
            out["edited_at"] = when.isoformat()
        elif isinstance(when, str):
            text = when.strip()
            try:
                parsed = date.fromisoformat(text[:10])
            except ValueError:
                raise ValueError(f"edited_at must be an ISO date (YYYY-MM-DD), got {when!r}") from None
            if len(text) > 10 and text[10] not in "T ":
                raise ValueError(f"edited_at must be an ISO date (YYYY-MM-DD), got {when!r}")
            out["edited_at"] = parsed.isoformat()
        else:
            raise ValueError(f"edited_at must be an ISO date (YYYY-MM-DD), got {when!r}")
    return out


def is_valid_source(source: str) -> bool:
    return bool(isinstance(source, str) and SOURCE_PATTERN.match(source))


def requires_review(source: str) -> bool:
    """House defaults and engine fallbacks are review prompts (ADR 0003 §5).

    A ``REVIEW_ON_FINDING_SOURCES`` source is a disclosure: its entry is
    flagged only when the resolver finds the case ambiguous.
    """
    if source in REVIEW_ON_FINDING_SOURCES:
        return False
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
    # Display-only facts about the canonical field (e.g. an opex line's
    # calculation_type and per-unit amount). The resolver never reads it.
    detail: Optional[dict[str, Any]] = None
    # ADR 0003 §5 guardrail record (engine/assumptions/guardrails.py).
    guardrail: Optional[dict[str, Any]] = None

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
        if self.detail:
            out["detail"] = deepcopy(self.detail)
        if self.guardrail:
            out["guardrail"] = deepcopy(self.guardrail)
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
            detail=deepcopy(data.get("detail")) or None,
            guardrail=deepcopy(data.get("guardrail")) or None,
        )
