"""Assumption layer (ADR 0003): deal profile, assumption registry, resolver."""

from engine.assumptions.profile import (
    DEAL_TYPE_STRATEGY,
    RESERVED_DEAL_TYPES,
    SUPPORTED_DEAL_TYPES,
    DealProfile,
    UnsupportedDealTypeError,
)
from engine.assumptions.guardrails import apply_guardrails
from engine.assumptions.registry import RESOLVER_VERSION, AssumptionEntry
from engine.assumptions.resolver import (
    ASSUMPTION_KEYS,
    AssumptionOverrideError,
    normalize_overrides,
    overrides_from_patch,
    resolve_assumptions,
)

__all__ = [
    "ASSUMPTION_KEYS",
    "AssumptionEntry",
    "AssumptionOverrideError",
    "DEAL_TYPE_STRATEGY",
    "DealProfile",
    "RESERVED_DEAL_TYPES",
    "RESOLVER_VERSION",
    "SUPPORTED_DEAL_TYPES",
    "UnsupportedDealTypeError",
    "apply_guardrails",
    "normalize_overrides",
    "overrides_from_patch",
    "resolve_assumptions",
]
