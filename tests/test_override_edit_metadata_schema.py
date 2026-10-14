"""An analyst edit stamped with who and when still validates (ADR 0003 §8).

``runs/assumptions_sheet.py apply --editor/--edit-date`` and the MCP
``edit_assumptions`` tool record ``edited_by`` / ``edited_at`` on the
override; the canonical schema must accept them, or the edited deal can no
longer run.
"""

from __future__ import annotations

from engine.assumptions import resolve_assumptions
from engine.assumptions.sheet import stamp_overrides
from engine.validator import validate_deal


def _schema_errors(canonical):
    return [i for i in validate_deal(canonical).issues if i.severity == "ERROR" and i.code == "SCHEMA"]


def test_a_stamped_override_passes_schema_validation(minimal_deal_inputs):
    resolved = resolve_assumptions(minimal_deal_inputs, market=None)
    overrides = stamp_overrides({"collection_loss": {"value": 0.012, "note": "IC view"}}, resolved,
                                edited_by="Analyst A", edited_at="2026-10-14")
    edited = resolve_assumptions(resolved, overrides=overrides, market=None)
    override = edited["assumptions"]["entries"]["collection_loss"]["override"]
    assert override["edited_by"] == "Analyst A" and override["edited_at"] == "2026-10-14"
    assert _schema_errors(edited) == []


def test_override_metadata_is_still_typed(minimal_deal_inputs):
    resolved = resolve_assumptions(minimal_deal_inputs, overrides={"collection_loss": 0.012}, market=None)
    resolved["assumptions"]["entries"]["collection_loss"]["override"]["edited_at"] = 20261014
    assert _schema_errors(resolved)
