from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine.ingest.ingestion_pipeline import promote_property_tax_evidence
from engine.ingest.property_tax_migration import (
    audit_reusable_property_tax_inputs,
    migrate_legacy_property_tax_policy,
)


@pytest.mark.parametrize(
    ("producer", "legacy", "expected_mills"),
    [
        (
            "plat_property_tax_context",
            {"property_tax_context": {"local_tax_rate": 0.02531}},
            25.31,
        ),
        (
            "engine_backsolve_policy",
            {"property_tax_policy": {"tax_rate_pct": 0.02531}},
            25.31,
        ),
        ("broker_snapshot_mills", {"property_tax_millage_rate": 25.31}, 25.31),
        (
            "broker_snapshot_percentage_points",
            {"property_tax_rate_pct": 2.531},
            25.31,
        ),
    ],
)
def test_known_producer_units_migrate_without_magnitude_guessing(
    producer: str,
    legacy: dict,
    expected_mills: float,
) -> None:
    result = migrate_legacy_property_tax_policy(
        legacy,
        producer=producer,
        source="offering_memorandum",
        source_locator="raw_inputs/OM.pdf, property tax table",
    )

    assert result.policy is not None
    assert result.policy["millage_rate_mills"] == expected_mills
    assert result.policy["assessment_ratio"] == 1.0
    assert result.policy["analyst_override"] is False
    assert result.evidence_candidates == (
        {
            "millage_rate_mills": expected_mills,
            "assessment_ratio": 1.0,
            "source": "offering_memorandum",
            "source_locator": "raw_inputs/OM.pdf, property tax table",
            "analyst_override": False,
        },
    )
    assert result.removed_fields
    for removed_field in result.removed_fields:
        cursor: object = result.property_summary
        for segment in removed_field.split("."):
            if not isinstance(cursor, dict) or segment not in cursor:
                break
            cursor = cursor[segment]
        else:  # pragma: no cover - assertion failure branch
            pytest.fail(f"legacy field survived migration: {removed_field}")


def test_known_producer_conversion_does_not_depend_on_value_magnitude() -> None:
    mills = migrate_legacy_property_tax_policy(
        {"property_tax_millage_rate": 0.02531},
        producer="broker_snapshot_mills",
        source="offering_memorandum",
        source_locator="raw_inputs/OM.pdf, property tax table",
    )
    percentage_points = migrate_legacy_property_tax_policy(
        {"property_tax_rate_pct": 0.02531},
        producer="broker_snapshot_percentage_points",
        source="offering_memorandum",
        source_locator="raw_inputs/OM.pdf, property tax table",
    )

    assert mills.policy is not None
    assert percentage_points.policy is not None
    assert mills.policy["millage_rate_mills"] == 0.02531
    assert percentage_points.policy["millage_rate_mills"] == 0.2531


def test_ambiguous_unknown_field_is_evidence_only_and_requires_analyst() -> None:
    result = migrate_legacy_property_tax_policy(
        {"tax_rate": 2.531},
        producer="unknown",
        source="offering_memorandum",
        source_locator="raw_inputs/OM.pdf, page 4",
    )

    assert result.policy is None
    assert result.issue_code == "missing_property_tax_millage"
    assert result.property_summary["tax_rate"] == 2.531
    assert result.evidence_candidates == (
        {
            "producer": "unknown",
            "field": "tax_rate",
            "value": 2.531,
            "source": "offering_memorandum",
            "source_locator": "raw_inputs/OM.pdf, page 4",
        },
    )
    assert result.removed_fields == ()


def test_unapproved_legacy_ninety_percent_becomes_full_value() -> None:
    result = migrate_legacy_property_tax_policy(
        {
            "property_tax_policy": {
                "tax_rate_pct": 0.02531,
                "assessment_ratio": 0.90,
            },
        },
        producer="engine_backsolve_policy",
        source="offering_memorandum",
        source_locator="raw_inputs/OM.pdf, page 4",
    )

    assert result.policy is not None
    assert result.policy["assessment_ratio"] == 1.0
    assert result.policy["analyst_override"] is False


def test_explicit_evidence_backed_ratio_survives_migration() -> None:
    result = migrate_legacy_property_tax_policy(
        {
            "property_tax_policy": {
                "tax_rate_pct": 0.02531,
                "assessment_ratio": 0.70,
                "analyst_override": True,
                "assessment_ratio_source": "cad_rate_table:raw_inputs/CAD.pdf, page 3",
            },
        },
        producer="engine_backsolve_policy",
        source="county_tax_notice",
        source_locator="raw_inputs/notice.pdf, page 2",
    )

    assert result.policy is not None
    assert result.policy["assessment_ratio"] == 0.7
    assert result.policy["source"] == "composite_evidence"
    assert result.policy["source_locator"] == (
        "cad_rate_table:raw_inputs/CAD.pdf, page 3; "
        "county_tax_notice:raw_inputs/notice.pdf, page 2"
    )
    assert result.policy["analyst_override"] is True
    assert result.evidence_candidates == (
        {
            "millage_rate_mills": 25.31,
            "assessment_ratio": 0.7,
            "source": "composite_evidence",
            "source_locator": (
                "cad_rate_table:raw_inputs/CAD.pdf, page 3; "
                "county_tax_notice:raw_inputs/notice.pdf, page 2"
            ),
            "analyst_override": True,
        },
    )


def test_promotion_deduplicates_equal_values_and_combines_provenance() -> None:
    property_summary, issue_code = promote_property_tax_evidence(
        {},
        [
            {
                "producer": "broker_snapshot_mills",
                "property_tax_millage_rate": 25.31,
                "source": "offering_memorandum",
                "source_locator": "raw_inputs/B.pdf, tax table",
            },
            {
                "producer": "plat_property_tax_context",
                "property_tax_context": {"local_tax_rate": 0.02531},
                "source": "county_tax_notice",
                "source_locator": "raw_inputs/A.pdf, rate table",
            },
        ],
    )

    assert issue_code is None
    assert property_summary["property_tax_policy"] == {
        "millage_rate_mills": 25.31,
        "assessment_ratio": 1.0,
        "source": "composite_evidence",
        "source_locator": (
            "county_tax_notice:raw_inputs/A.pdf, rate table; "
            "offering_memorandum:raw_inputs/B.pdf, tax table"
        ),
        "analyst_override": False,
    }
    assert "property_tax_evidence_candidates" not in property_summary


def test_promotion_preserves_conflicting_values_for_analyst_resolution() -> None:
    property_summary, issue_code = promote_property_tax_evidence(
        {},
        [
            {
                "producer": "broker_snapshot_mills",
                "property_tax_millage_rate": 25.31,
                "source": "offering_memorandum",
                "source_locator": "raw_inputs/B.pdf, tax table",
            },
            {
                "producer": "broker_snapshot_percentage_points",
                "property_tax_rate_pct": 2.6,
                "source": "offering_memorandum",
                "source_locator": "raw_inputs/C.pdf, tax table",
            },
        ],
    )

    assert issue_code == "conflicting_property_tax_millage"
    assert "property_tax_policy" not in property_summary
    assert property_summary["property_tax_evidence_candidates"] == [
        {
            "millage_rate_mills": 25.31,
            "assessment_ratio": 1.0,
            "source": "offering_memorandum",
            "source_locator": "raw_inputs/B.pdf, tax table",
            "analyst_override": False,
        },
        {
            "millage_rate_mills": 26.0,
            "assessment_ratio": 1.0,
            "source": "offering_memorandum",
            "source_locator": "raw_inputs/C.pdf, tax table",
            "analyst_override": False,
        },
    ]


def test_unknown_candidate_is_preserved_without_numeric_promotion() -> None:
    property_summary, issue_code = promote_property_tax_evidence(
        {},
        [
            {
                "producer": "unknown",
                "tax_rate": 2.531,
                "source": "offering_memorandum",
                "source_locator": "raw_inputs/OM.pdf, page 4",
            }
        ],
    )

    assert issue_code == "missing_property_tax_millage"
    assert "property_tax_policy" not in property_summary
    assert property_summary["property_tax_evidence_candidates"][0]["field"] == "tax_rate"


def test_equal_approved_ratios_keep_all_effective_composite_provenance() -> None:
    property_summary, issue_code = promote_property_tax_evidence(
        {},
        [
            {
                "producer": "engine_backsolve_policy",
                "property_tax_policy": {
                    "tax_rate_pct": 0.02531,
                    "assessment_ratio": 0.7,
                    "analyst_override": True,
                    "assessment_ratio_source": (
                        "cad_rate_table:raw_inputs/A.pdf, page 3"
                    ),
                },
                "source": "county_tax_notice",
                "source_locator": "raw_inputs/A-notice.pdf, page 2",
            },
            {
                "producer": "engine_backsolve_policy",
                "property_tax_policy": {
                    "tax_rate_pct": 0.02531,
                    "assessment_ratio": 0.7,
                    "analyst_override": True,
                    "assessment_ratio_source": (
                        "cad_rate_table:raw_inputs/B.pdf, page 3"
                    ),
                },
                "source": "county_tax_notice",
                "source_locator": "raw_inputs/B-notice.pdf, page 2",
            },
        ],
    )

    assert issue_code is None
    assert property_summary["property_tax_policy"] == {
        "millage_rate_mills": 25.31,
        "assessment_ratio": 0.7,
        "source": "composite_evidence",
        "source_locator": (
            "cad_rate_table:raw_inputs/A.pdf, page 3; "
            "cad_rate_table:raw_inputs/B.pdf, page 3; "
            "county_tax_notice:raw_inputs/A-notice.pdf, page 2; "
            "county_tax_notice:raw_inputs/B-notice.pdf, page 2"
        ),
        "analyst_override": True,
    }


def test_conflicting_assessment_ratios_preserve_normalized_evidence() -> None:
    property_summary, issue_code = promote_property_tax_evidence(
        {},
        [
            {
                "producer": "engine_backsolve_policy",
                "property_tax_policy": {
                    "tax_rate_pct": 0.02531,
                    "assessment_ratio": 0.7,
                    "analyst_override": True,
                    "assessment_ratio_source": (
                        "cad_rate_table:raw_inputs/CAD.pdf, page 3"
                    ),
                },
                "source": "county_tax_notice",
                "source_locator": "raw_inputs/notice.pdf, page 2",
            },
            {
                "producer": "broker_snapshot_mills",
                "property_tax_millage_rate": 25.31,
                "source": "offering_memorandum",
                "source_locator": "raw_inputs/OM.pdf, tax table",
            },
        ],
    )

    assert issue_code == "conflicting_property_tax_assessment_ratio"
    assert "property_tax_policy" not in property_summary
    assert property_summary["property_tax_evidence_candidates"] == [
        {
            "millage_rate_mills": 25.31,
            "assessment_ratio": 0.7,
            "source": "composite_evidence",
            "source_locator": (
                "cad_rate_table:raw_inputs/CAD.pdf, page 3; "
                "county_tax_notice:raw_inputs/notice.pdf, page 2"
            ),
            "analyst_override": True,
        },
        {
            "millage_rate_mills": 25.31,
            "assessment_ratio": 1.0,
            "source": "offering_memorandum",
            "source_locator": "raw_inputs/OM.pdf, tax table",
            "analyst_override": False,
        },
    ]


def test_existing_canonical_policy_wins_and_all_finite_aliases_are_removed() -> None:
    original = {
        "property_tax_policy": {
            "millage_rate_mills": 25.31,
            "assessment_ratio": 1.0,
            "source": "analyst",
            "source_locator": "raw_inputs/workpaper.pdf, combined millage",
            "analyst_override": False,
        },
        "property_tax_context": {"local_tax_rate": 0.024},
        "property_tax_millage_rate": 24.0,
        "property_tax_rate_pct": 2.4,
        "assessment_ratio": 0.9,
        "analyst_override": True,
        "assessment_ratio_source": "legacy analyst note",
        "reassessment_ratio": 0.9,
        "analyst_ratio_override": 0.9,
        "full_value_assessment": False,
    }

    result = migrate_legacy_property_tax_policy(
        original,
        producer="broker_snapshot_mills",
        source="offering_memorandum",
        source_locator="raw_inputs/OM.pdf, property tax table",
    )

    assert result.policy == original["property_tax_policy"]
    assert result.property_summary == {
        "property_tax_policy": original["property_tax_policy"]
    }
    assert set(result.removed_fields) == {
        "property_tax_context.local_tax_rate",
        "property_tax_millage_rate",
        "property_tax_rate_pct",
        "assessment_ratio",
        "analyst_override",
        "assessment_ratio_source",
        "reassessment_ratio",
        "analyst_ratio_override",
        "full_value_assessment",
    }
    assert original["property_tax_context"]["local_tax_rate"] == 0.024
    assert original["property_tax_millage_rate"] == 24.0
    assert original["assessment_ratio"] == 0.9
    assert original["analyst_override"] is True
    assert original["assessment_ratio_source"] == "legacy analyst note"


def test_audit_reports_legacy_and_ambiguous_reusable_inputs(tmp_path: Path) -> None:
    known = tmp_path / "known.json"
    known.write_text(
        json.dumps(
            {
                "metadata": {
                    "property_summary": {
                        "property_tax_context": {"local_tax_rate": 0.02531}
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    ambiguous = tmp_path / "ambiguous.json"
    ambiguous.write_text(json.dumps({"tax_rate": 2.531}), encoding="utf-8")

    issues = audit_reusable_property_tax_inputs([known, ambiguous])

    assert [(issue.path, issue.code, issue.field) for issue in issues] == [
        (ambiguous, "missing_property_tax_millage", "tax_rate"),
        (known, "legacy_property_tax_field", "property_tax_context.local_tax_rate"),
    ]


def test_audit_flags_unapproved_ratio_but_excludes_historical_outputs(
    tmp_path: Path,
) -> None:
    reusable = tmp_path / "standardized" / "canonical.json"
    reusable.parent.mkdir()
    reusable.write_text(
        json.dumps(
            {
                "metadata": {
                    "property_summary": {
                        "property_tax_policy": {
                            "tax_rate_pct": 0.02531,
                            "assessment_ratio": 0.90,
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    historical = tmp_path / "outputs" / "run_001" / "canonical.json"
    historical.parent.mkdir(parents=True)
    historical.write_bytes(reusable.read_bytes())
    reusable_before = reusable.read_bytes()
    historical_before = historical.read_bytes()

    issues = audit_reusable_property_tax_inputs([tmp_path])

    assert [issue.path for issue in issues] == [reusable]
    assert issues[0].code == "unsupported_property_tax_assessment_override"
    assert reusable.read_bytes() == reusable_before
    assert historical.read_bytes() == historical_before


def test_audit_accepts_canonical_default_and_approved_ratio(tmp_path: Path) -> None:
    default = tmp_path / "canonical_inputs.json"
    default.write_text(
        json.dumps(
            {
                "metadata": {
                    "property_summary": {
                        "property_tax_policy": {
                            "millage_rate_mills": 25.31,
                            "assessment_ratio": 1.0,
                            "source": "analyst",
                            "source_locator": "underwrite-deal:property_tax_millage",
                            "analyst_override": False,
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    approved = tmp_path / "standardized" / "canonical.json"
    approved.parent.mkdir()
    approved.write_text(
        json.dumps(
            {
                "metadata": {
                    "property_summary": {
                        "property_tax_policy": {
                            "millage_rate_mills": 25.31,
                            "assessment_ratio": 0.70,
                            "source": "composite_evidence",
                            "source_locator": (
                                "millage=notice.pdf;"
                                "assessment_ratio=cad_rate_table:cad.pdf"
                            ),
                            "analyst_override": True,
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    assert audit_reusable_property_tax_inputs([default, approved]) == []


def test_audit_excludes_symlinked_outputs_and_symlink_escapes(
    tmp_path: Path,
) -> None:
    reusable_root = tmp_path / "reusable"
    reusable_root.mkdir()
    historical = reusable_root / "archive" / "outputs" / "run_001" / "canonical.json"
    historical.parent.mkdir(parents=True)
    historical.write_text(json.dumps({"tax_rate": 2.531}), encoding="utf-8")
    output_link = reusable_root / "looks_reusable.json"
    output_link.symlink_to(historical)

    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"tax_rate": 2.531}), encoding="utf-8")
    escape_link = reusable_root / "escape.json"
    escape_link.symlink_to(outside)
    cycle = reusable_root / "cycle.json"
    cycle.symlink_to(cycle)
    historical_before = historical.read_bytes()
    outside_before = outside.read_bytes()

    assert audit_reusable_property_tax_inputs([reusable_root]) == []
    assert historical.read_bytes() == historical_before
    assert outside.read_bytes() == outside_before


def test_audit_prioritizes_unapproved_standalone_ratio_aliases(
    tmp_path: Path,
) -> None:
    assessment = tmp_path / "assessment.json"
    assessment.write_text(
        json.dumps(
            {
                "metadata": {
                    "property_summary": {
                        "assessment_ratio": 0.90,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    reassessment = tmp_path / "reassessment.json"
    reassessment.write_text(
        json.dumps(
            {
                "metadata": {
                    "property_summary": {
                        "reassessment_ratio": 0.90,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    issues = audit_reusable_property_tax_inputs([assessment, reassessment])

    assert [(issue.path, issue.code, issue.field) for issue in issues] == [
        (
            assessment,
            "unsupported_property_tax_assessment_override",
            "assessment_ratio",
        ),
        (
            reassessment,
            "unsupported_property_tax_assessment_override",
            "reassessment_ratio",
        ),
    ]


def test_audit_flags_full_value_standalone_ratio_as_legacy_alias(
    tmp_path: Path,
) -> None:
    reusable = tmp_path / "canonical_inputs.json"
    reusable.write_text(
        json.dumps(
            {
                "metadata": {
                    "property_summary": {
                        "assessment_ratio": 1.0,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    issues = audit_reusable_property_tax_inputs([reusable])

    assert [(issue.code, issue.field) for issue in issues] == [
        ("legacy_property_tax_field", "assessment_ratio")
    ]


def test_audit_covers_analyst_ratio_override_alias(tmp_path: Path) -> None:
    reusable = tmp_path / "canonical_inputs.json"
    reusable.write_text(
        json.dumps(
            {
                "metadata": {
                    "property_summary": {
                        "analyst_ratio_override": 0.90,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    issues = audit_reusable_property_tax_inputs([reusable])

    assert [(issue.code, issue.field) for issue in issues] == [
        (
            "unsupported_property_tax_assessment_override",
            "analyst_ratio_override",
        )
    ]


@pytest.mark.parametrize("ratio", [0, -0.1, "not-a-number", "NaN", "Infinity"])
def test_audit_reports_approved_invalid_ratio_values(
    tmp_path: Path,
    ratio: object,
) -> None:
    reusable = tmp_path / "canonical_inputs.json"
    reusable.write_text(
        json.dumps(
            {
                "metadata": {
                    "property_summary": {
                        "assessment_ratio": ratio,
                        "analyst_override": True,
                        "assessment_ratio_source": "county assessment table",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    issues = audit_reusable_property_tax_inputs([reusable])

    assert [(issue.code, issue.field) for issue in issues] == [
        (
            "unsupported_property_tax_assessment_override",
            "assessment_ratio",
        )
    ]
    assert "expected a positive finite decimal" in issues[0].message
    assert issues[0].value == ratio


def test_audit_prioritizes_ratio_diagnostic_before_rate_alias(
    tmp_path: Path,
) -> None:
    reusable = tmp_path / "canonical_inputs.json"
    reusable.write_text(
        json.dumps(
            {
                "metadata": {
                    "property_summary": {
                        "property_tax_context": {
                            "local_tax_rate": 0.02531,
                        },
                        "reassessment_ratio": 0.90,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    issues = audit_reusable_property_tax_inputs([reusable])

    assert [(issue.code, issue.field) for issue in issues] == [
        (
            "unsupported_property_tax_assessment_override",
            "reassessment_ratio",
        ),
        ("legacy_property_tax_field", "property_tax_context.local_tax_rate"),
    ]
