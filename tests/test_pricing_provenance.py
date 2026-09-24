"""Tests for pricing_provenance section (Schema Gap 5).

Covers:
- Back-compat: canonical without pricing_provenance still validates
- Happy path: canonical with valid pricing_provenance validates
- Cross-section consistency: strike_price != purchase_price → SCHEMA_VIOLATION
- Schema strictness: missing required field in pricing_provenance fails
"""
from __future__ import annotations

import copy

from engine.validator import validate_deal


def _provenance(strike: float = 13_500_000) -> dict:
    # Note: basis is "other" because the strike (13.5M) is 14M × 0.964 (not 0.95);
    # using basis "broker_whisper_minus_5pct" would trip the derivation-mismatch
    # check added in Wave 1a Task 1.1 (must be within 1% of whisper × 0.95).
    return {
        "published_om_price": None,
        "broker_whisper_price": 14_000_000,
        "strike_price": strike,
        "strike_price_basis": "other",
        "strike_price_derivation": "14M whisper × 0.964 = 13.5M; ~5.5% cap on T12",
        "om_pricing_process": "best_offers_loi_unpriced",
        "as_of_date": "2026-04-23",
    }


def test_canonical_without_pricing_provenance_validates(minimal_deal_inputs):
    """Legacy canonical files (no pricing_provenance) remain valid."""
    assert "pricing_provenance" not in minimal_deal_inputs
    report = validate_deal(minimal_deal_inputs)
    assert report.status == "PASS", [i.__dict__ for i in report.issues]


def test_canonical_with_valid_pricing_provenance_validates(minimal_deal_inputs):
    """strike_price == purchase_price is the happy path."""
    inputs = copy.deepcopy(minimal_deal_inputs)
    purchase_price = inputs["purchase_assumptions"]["purchase_price"]
    inputs["pricing_provenance"] = _provenance(strike=purchase_price)
    report = validate_deal(inputs)
    assert report.status == "PASS", [i.__dict__ for i in report.issues]


def test_pricing_provenance_strike_mismatch_fails(minimal_deal_inputs):
    """strike_price != purchase_price → SCHEMA_VIOLATION naming both values."""
    inputs = copy.deepcopy(minimal_deal_inputs)
    purchase_price = inputs["purchase_assumptions"]["purchase_price"]
    bad_strike = purchase_price + 100_000
    inputs["pricing_provenance"] = _provenance(strike=bad_strike)
    report = validate_deal(inputs)
    assert report.status == "FAIL"
    violations = [
        i for i in report.issues
        if i.code == "SCHEMA_VIOLATION" and i.path == "/pricing_provenance/strike_price"
    ]
    assert len(violations) == 1, [i.__dict__ for i in report.issues]
    msg = violations[0].message
    assert str(bad_strike) in msg
    assert str(purchase_price) in msg


def test_pricing_provenance_partial_missing_required_field_fails(minimal_deal_inputs):
    """Missing required field (e.g., strike_price_derivation) → SCHEMA error."""
    inputs = copy.deepcopy(minimal_deal_inputs)
    purchase_price = inputs["purchase_assumptions"]["purchase_price"]
    prov = _provenance(strike=purchase_price)
    del prov["strike_price_derivation"]
    inputs["pricing_provenance"] = prov
    report = validate_deal(inputs)
    assert report.status == "FAIL"
    schema_errors = [i for i in report.issues if i.code == "SCHEMA"]
    assert any("strike_price_derivation" in i.message for i in schema_errors), [
        i.__dict__ for i in report.issues
    ]
