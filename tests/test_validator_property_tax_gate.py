"""ADR 0003 §5: a missing or invalid property-tax millage is an integrity blocker.

The engine prices real estate tax from the purchase price on every run
(``engine.property_tax.apply_property_tax_policy``). A priced canonical
without a usable millage must fail validation, and ``run_underwriting`` must
refuse it with that validation error instead of failing inside the tax math.
"""

from __future__ import annotations

import copy
import json

import pytest

from engine.engine import run_from_files, run_underwriting
from engine.property_tax import PropertyTaxPolicyError
from engine.validator import (
    PROPERTY_TAX_BASIS_INVALID,
    PROPERTY_TAX_MILLAGE_INVALID,
    PROPERTY_TAX_MILLAGE_MISSING,
    property_tax_preflight_issues,
    property_tax_required,
    validate_deal,
)

MILLS_PATH = "/metadata/property_summary/property_tax_policy/millage_rate_mills"


def _without_policy(inputs: dict) -> dict:
    out = copy.deepcopy(inputs)
    out["metadata"]["property_summary"].pop("property_tax_policy")
    return out


def _errors(report) -> list:
    return [issue for issue in report.issues if issue.severity == "ERROR"]


def test_priced_deal_with_millage_passes(minimal_deal_inputs):
    assert property_tax_required(minimal_deal_inputs)
    report = validate_deal(minimal_deal_inputs)
    assert report.status == "PASS", _errors(report)


def test_priced_deal_without_millage_is_an_error(minimal_deal_inputs):
    report = validate_deal(_without_policy(minimal_deal_inputs))
    assert report.status == "FAIL"
    errors = _errors(report)
    assert [issue.code for issue in errors] == [PROPERTY_TAX_MILLAGE_MISSING]
    assert errors[0].path == MILLS_PATH
    assert "--millage" in errors[0].message


@pytest.mark.parametrize("mills", [0, -5, "abc"])
def test_priced_deal_with_invalid_millage_is_an_error(minimal_deal_inputs, mills):
    inputs = copy.deepcopy(minimal_deal_inputs)
    inputs["metadata"]["property_summary"]["property_tax_policy"]["millage_rate_mills"] = mills
    errors = _errors(validate_deal(inputs))
    assert PROPERTY_TAX_MILLAGE_INVALID in [issue.code for issue in errors]


def test_unpriced_draft_without_millage_still_validates_as_partial(minimal_deal_inputs):
    """An ingest draft with no purchase price cannot run yet; it is not blocked on millage."""
    draft = _without_policy(minimal_deal_inputs)
    for section in ("purchase_assumptions", "debt_terms", "exit_assumptions", "fund_assumptions"):
        draft.pop(section)
    assert not property_tax_required(draft)
    codes = [issue.code for issue in _errors(validate_deal(draft))]
    assert PROPERTY_TAX_MILLAGE_MISSING not in codes


def test_run_underwriting_refuses_with_the_validation_error(minimal_deal_inputs):
    with pytest.raises(ValueError) as exc:
        run_underwriting(_without_policy(minimal_deal_inputs))
    assert not isinstance(exc.value, PropertyTaxPolicyError)
    assert str(exc.value).startswith("Validation failed:")
    assert PROPERTY_TAX_MILLAGE_MISSING in str(exc.value)


def test_run_underwriting_refuses_an_unpriced_deal_on_the_tax_basis(minimal_deal_inputs):
    inputs = copy.deepcopy(minimal_deal_inputs)
    inputs.pop("purchase_assumptions")
    issues = property_tax_preflight_issues(inputs)
    assert [issue.code for issue in issues] == [PROPERTY_TAX_BASIS_INVALID]
    with pytest.raises(ValueError, match="Validation failed:\n" + PROPERTY_TAX_BASIS_INVALID):
        run_underwriting(inputs)


def test_preflight_is_clean_for_a_runnable_deal(minimal_deal_inputs):
    assert property_tax_preflight_issues(minimal_deal_inputs) == []


def test_run_from_files_writes_the_error_instead_of_crashing(minimal_deal_inputs, tmp_path):
    inputs_path = tmp_path / "inputs.json"
    inputs_path.write_text(json.dumps(_without_policy(minimal_deal_inputs)), encoding="utf-8")
    validation_path = tmp_path / "validation.json"
    with pytest.raises(SystemExit):
        run_from_files(inputs_path, tmp_path / "outputs.json", validation_path)
    report = json.loads(validation_path.read_text(encoding="utf-8"))
    assert report["status"] == "FAIL"
    assert [i["code"] for i in report["issues"] if i["severity"] == "ERROR"] == [
        PROPERTY_TAX_MILLAGE_MISSING
    ]
    assert not (tmp_path / "outputs.json").exists()
