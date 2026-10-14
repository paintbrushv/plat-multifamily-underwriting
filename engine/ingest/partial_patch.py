"""Merge an analyst or agent-authored partial canonical patch onto an ingested deal.

``runs/ingest_deal.py --partial-json`` and the MCP workflow (``engine.workflow``)
both use this: deep-merge the patch, then re-run the assumption resolver so
the values the patch supplies for an assumption become analyst overrides and
the assumptions section reflects the merged deal.
"""

from __future__ import annotations

from engine.assumptions import overrides_from_patch, resolve_assumptions
from engine.ingest.ingestion_pipeline import sync_ingest_gate_assumptions

PARTIAL_JSON_OVERRIDE_NOTE = "supplied by --partial-json"


def deep_merge(base: object, patch: object) -> object:
    """``patch`` merged into ``base``: dicts merge key by key, anything else is replaced."""
    if isinstance(base, dict) and isinstance(patch, dict):
        merged = dict(base)
        for key, value in patch.items():
            if key in merged:
                merged[key] = deep_merge(merged[key], value)
            else:
                merged[key] = value
        return merged
    return patch


def apply_partial_json(base_deal: dict, partial: dict) -> dict:
    """Deep-merge ``partial`` onto ``base_deal`` and record that the analyst supplied it."""
    merged = deep_merge(base_deal, partial)
    if not isinstance(merged, dict):  # defensive; deep_merge returns dict here
        raise ValueError("Merged deal payload must be a dict")
    growth_patch = partial.get("growth_assumptions")
    if (
        isinstance(growth_patch, dict)
        and "annual_growth_rate" in growth_patch
        and "annual_growth_rates" not in growth_patch
        and isinstance(merged.get("growth_assumptions"), dict)
    ):
        # A scalar rent-growth patch is a flat rate: drop the resolved per-year vector.
        merged["growth_assumptions"] = {
            k: v for k, v in merged["growth_assumptions"].items() if k != "annual_growth_rates"
        }

    metadata = merged.setdefault("metadata", {})
    partial_metadata = partial.get("metadata", {}) if isinstance(partial.get("metadata"), dict) else {}
    if "purchase_assumptions" in partial and "purchase_assumptions_source" not in partial_metadata:
        metadata["purchase_assumptions_source"] = "analyst_override"
    if any(k in partial for k in ("purchase_assumptions", "debt_terms", "exit_assumptions", "fund_assumptions")):
        metadata["analyst_review_required"] = False
    return merged


def apply_partial_and_reresolve(
    base_deal: dict, partial: dict, *, market: object = None, note: str = PARTIAL_JSON_OVERRIDE_NOTE,
) -> dict:
    """Merge a partial patch, then re-run the assumption resolver.

    Assumption values the patch supplies become analyst overrides (with
    ``note``), so the assumptions section and the legacy
    ``ingest_gate.assumed_defaults`` reflect the merged deal instead of the
    pre-merge ingest. ``market`` is the market-data registry used at ingest
    (market-sourced values are kept when it is None).
    """
    merged = apply_partial_json(base_deal, partial)
    if "assumptions" not in base_deal:
        return merged
    overrides = overrides_from_patch(partial, merged, note=note)
    resolved = resolve_assumptions(merged, overrides=overrides, market=market)
    return sync_ingest_gate_assumptions(resolved)
