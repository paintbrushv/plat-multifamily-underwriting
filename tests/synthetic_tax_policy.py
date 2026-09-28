"""Explicit test-only tax evidence for legacy synthetic engine fixtures.

Production underwriting still refuses a missing property-tax policy.  These
older tests exercise unrelated engine mechanics and need a declared synthetic
millage and purchase price to reach those mechanics.
"""

from copy import deepcopy


def with_synthetic_tax_policy(inputs: dict) -> dict:
    prepared = deepcopy(inputs)
    prepared.setdefault("purchase_assumptions", {
        "purchase_price": 12_000,
        "closing_costs": 0,
        "equity_contribution": 12_000,
        "total_equity_basis": 12_000,
    })
    metadata = prepared.setdefault("metadata", {})
    metadata.setdefault("property_summary", {})["property_tax_policy"] = {
        "millage_rate_mills": 1.0,
        "assessment_ratio": 1.0,
        "source": "synthetic_test",
        "source_locator": "tests/synthetic_tax_policy.py",
        "analyst_override": False,
    }
    return prepared
