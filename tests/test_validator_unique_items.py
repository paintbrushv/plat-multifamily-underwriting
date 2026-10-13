"""UPL-96: the validator's ``uniqueItems`` fast path decides exactly as jsonschema's.

``engine.validator`` proves an array unique from distinct item fingerprints
and otherwise defers to jsonschema. Every case here must yield the same
errors (message and path) as plain ``Draft202012Validator``.
"""
from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator

from engine.validator import StrictDraft202012Validator

NAN = float("nan")
CASES = [
    [],
    [1, 2, 3],
    [1, 1.0],
    [1, True],
    [0, False],
    [True, True],
    [[1], [True]],
    [[1], [True], [1]],
    ["1", 1],
    [None, None],
    [None, 0],
    [{"a": 1, "b": 2}, {"b": 2, "a": 1}],
    [{"a": 1}, {"a": 1.0}],
    [{"a": True}, {"a": 1}],
    [{"a": [1, 2]}, {"a": [2, 1]}],
    [{"a": {"b": [1, {"c": None}]}}, {"a": {"b": [1, {"c": None}]}}],
    [{"start_period": "2026-01", "end_period": "2026-06", "market_rent": 1000},
     {"start_period": "2026-07", "end_period": "2026-12", "market_rent": 1000}],
    [{"start_period": "2026-01", "end_period": "2026-06", "market_rent": 1000},
     {"start_period": "2026-07", "end_period": "2026-12", "market_rent": 1000},
     {"start_period": "2026-01", "end_period": "2026-06", "market_rent": 1000.0}],
    [NAN, NAN],
    [float("nan"), float("nan")],
    [[NAN], [NAN]],
    [1, "a", None, [1], {"a": 1}],
    "not an array",
    {"not": "an array"},
]


def _errors(validator_class, schema, instance):
    return sorted(
        (error.message, tuple(error.absolute_path))
        for error in validator_class(schema).iter_errors(instance)
    )


@pytest.mark.parametrize("unique", [True, False])
@pytest.mark.parametrize("instance", CASES, ids=[repr(c)[:40] for c in CASES])
def test_unique_items_matches_jsonschema(instance, unique):
    for schema in ({"uniqueItems": unique}, {"properties": {"rows": {"uniqueItems": unique}}}):
        target = {"rows": instance} if "properties" in schema else instance
        expected = _errors(Draft202012Validator, schema, target)
        assert _errors(StrictDraft202012Validator, schema, target) == expected


def test_shared_nan_object_is_a_duplicate_as_in_jsonschema():
    same = [NAN, NAN]  # one object twice: jsonschema's identity check calls it a duplicate
    expected = _errors(Draft202012Validator, {"uniqueItems": True}, same)
    assert expected
    assert _errors(StrictDraft202012Validator, {"uniqueItems": True}, same) == expected
