from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from engine.property_tax import (
    PropertyTaxPolicyError,
    TaxRateUnit,
    build_property_tax_policy,
    validate_property_tax_policy,
)

LegacyProducer = Literal[
    "plat_property_tax_context",
    "engine_backsolve_policy",
    "broker_snapshot_mills",
    "broker_snapshot_percentage_points",
    "unknown",
]


@dataclass(frozen=True)
class MigrationIssue:
    path: Path
    code: str
    field: str
    value: object
    message: str


@dataclass(frozen=True)
class MigrationResult:
    property_summary: dict[str, Any]
    policy: dict[str, Any] | None
    evidence_candidates: tuple[dict[str, Any], ...]
    removed_fields: tuple[str, ...]
    issue_code: str | None = None


_PRODUCER_FIELD_UNIT: dict[LegacyProducer, tuple[str, TaxRateUnit]] = {
    "plat_property_tax_context": (
        "property_tax_context.local_tax_rate",
        "decimal_rate",
    ),
    "engine_backsolve_policy": (
        "property_tax_policy.tax_rate_pct",
        "decimal_rate",
    ),
    "broker_snapshot_mills": ("property_tax_millage_rate", "mills"),
    "broker_snapshot_percentage_points": (
        "property_tax_rate_pct",
        "percentage_points",
    ),
}

_LEGACY_RATE_PATHS = tuple(
    field_path for field_path, _unit in _PRODUCER_FIELD_UNIT.values()
)
_CANONICAL_CONFLICTING_ALIAS_PATHS = (
    *_LEGACY_RATE_PATHS,
    "assessment_ratio",
    "analyst_override",
    "assessment_ratio_source",
    "reassessment_ratio",
    "analyst_ratio_override",
    "full_value_assessment",
    "property_tax_context.assessment_ratio",
    "property_tax_context.reassessment_ratio",
    "property_tax_context.analyst_ratio_override",
    "property_tax_context.full_value_assessment",
    "property_tax_context.analyst_override",
    "property_tax_context.assessment_ratio_source",
)

_AMBIGUOUS_RATE_FIELDS = frozenset(
    {
        "tax_rate",
        "tax_rate_pct",
        "property_tax_rate_pct",
        "property_tax_millage_rate",
        "local_tax_rate",
    }
)
_RATIO_FIELD_NAMES = (
    "assessment_ratio",
    "reassessment_ratio",
    "analyst_ratio_override",
)
_RATIO_METADATA_NAMES = ("analyst_override", "assessment_ratio_source")


def _nested_value(mapping: Mapping[str, Any], dotted_path: str) -> object:
    value: object = mapping
    for segment in dotted_path.split("."):
        if not isinstance(value, Mapping) or segment not in value:
            return None
        value = value[segment]
    return value


def _remove_nested_field(mapping: dict[str, Any], dotted_path: str) -> bool:
    segments = dotted_path.split(".")
    cursor: dict[str, Any] = mapping
    parents: list[tuple[dict[str, Any], str]] = []
    for segment in segments[:-1]:
        child = cursor.get(segment)
        if not isinstance(child, dict):
            return False
        parents.append((cursor, segment))
        cursor = child
    if segments[-1] not in cursor:
        return False
    cursor.pop(segments[-1])
    for parent, segment in reversed(parents):
        child = parent.get(segment)
        if isinstance(child, dict) and not child:
            parent.pop(segment)
        else:
            break
    return True


def _legacy_container(
    property_summary: Mapping[str, Any], producer: LegacyProducer
) -> Mapping[str, Any]:
    if producer == "plat_property_tax_context":
        value = property_summary.get("property_tax_context")
        return value if isinstance(value, Mapping) else property_summary
    if producer == "engine_backsolve_policy":
        value = property_summary.get("property_tax_policy")
        return value if isinstance(value, Mapping) else property_summary
    return property_summary


def _ratio_policy(
    property_summary: Mapping[str, Any], producer: LegacyProducer
) -> tuple[object, bool, str | None]:
    container = _legacy_container(property_summary, producer)
    ratio: object = 1.0
    for field in _RATIO_FIELD_NAMES:
        if field in container:
            ratio = container[field]
            break
        if field in property_summary:
            ratio = property_summary[field]
            break

    approved = container.get("analyst_override") is True
    if not approved:
        approved = property_summary.get("analyst_override") is True
    ratio_source = container.get("assessment_ratio_source")
    if not isinstance(ratio_source, str) or not ratio_source.strip():
        ratio_source = property_summary.get("assessment_ratio_source")
    if not isinstance(ratio_source, str) or not ratio_source.strip():
        ratio_source = None

    if not approved or ratio_source is None:
        return 1.0, False, None
    return ratio, True, ratio_source.strip()


def _raw_evidence_candidate(
    property_summary: Mapping[str, Any],
    *,
    producer: LegacyProducer,
    source: str,
    source_locator: str,
) -> dict[str, Any] | None:
    for dotted_path in (
        "tax_rate",
        "tax_rate_pct",
        "property_tax_rate_pct",
        "property_tax_millage_rate",
        "property_tax_context.local_tax_rate",
        "property_tax_policy.tax_rate_pct",
    ):
        value = _nested_value(property_summary, dotted_path)
        if value is not None:
            return {
                "producer": producer,
                "field": dotted_path,
                "value": value,
                "source": source,
                "source_locator": source_locator,
            }
    return None


def _existing_canonical_policy(
    property_summary: Mapping[str, Any],
) -> dict[str, Any] | None:
    policy = property_summary.get("property_tax_policy")
    if not isinstance(policy, Mapping) or "millage_rate_mills" not in policy:
        return None
    try:
        return validate_property_tax_policy(policy)
    except PropertyTaxPolicyError:
        return None


def _policy_evidence(policy: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "millage_rate_mills": policy["millage_rate_mills"],
        "assessment_ratio": policy["assessment_ratio"],
        "source": policy["source"],
        "source_locator": policy["source_locator"],
        "analyst_override": policy["analyst_override"],
    }


def migrate_legacy_property_tax_policy(
    property_summary: Mapping[str, Any],
    *,
    producer: LegacyProducer,
    source: str,
    source_locator: str,
) -> MigrationResult:
    """Migrate one explicitly labelled legacy producer without guessing units."""
    migrated = deepcopy(dict(property_summary))
    existing_policy = _existing_canonical_policy(property_summary)
    if existing_policy is not None:
        removed_fields = tuple(
            dotted_path
            for dotted_path in _CANONICAL_CONFLICTING_ALIAS_PATHS
            if _remove_nested_field(migrated, dotted_path)
        )
        migrated["property_tax_policy"] = existing_policy
        return MigrationResult(
            property_summary=migrated,
            policy=existing_policy,
            evidence_candidates=(_policy_evidence(existing_policy),),
            removed_fields=removed_fields,
        )

    producer_contract = _PRODUCER_FIELD_UNIT.get(producer)
    if producer_contract is None:
        raw_candidate = _raw_evidence_candidate(
            property_summary,
            producer=producer,
            source=source,
            source_locator=source_locator,
        )
        return MigrationResult(
            property_summary=migrated,
            policy=None,
            evidence_candidates=(raw_candidate,) if raw_candidate else (),
            removed_fields=(),
            issue_code="missing_property_tax_millage",
        )

    legacy_field, unit = producer_contract
    legacy_rate = _nested_value(property_summary, legacy_field)
    if legacy_rate is None:
        raw_candidate = _raw_evidence_candidate(
            property_summary,
            producer=producer,
            source=source,
            source_locator=source_locator,
        )
        return MigrationResult(
            property_summary=migrated,
            policy=None,
            evidence_candidates=(raw_candidate,) if raw_candidate else (),
            removed_fields=(),
            issue_code="missing_property_tax_millage",
        )

    ratio, analyst_override, ratio_source = _ratio_policy(
        property_summary, producer
    )
    policy_source = source
    policy_locator = source_locator
    if analyst_override and ratio_source is not None:
        policy_source = "composite_evidence"
        policy_locator = "; ".join(
            sorted({ratio_source, f"{source}:{source_locator}"})
        )

    try:
        policy = build_property_tax_policy(
            millage_rate=legacy_rate,
            unit=unit,
            source=policy_source,
            source_locator=policy_locator,
            assessment_ratio=ratio,
            analyst_override=analyst_override,
        )
    except PropertyTaxPolicyError as exc:
        raw_candidate = _raw_evidence_candidate(
            property_summary,
            producer=producer,
            source=source,
            source_locator=source_locator,
        )
        return MigrationResult(
            property_summary=migrated,
            policy=None,
            evidence_candidates=(raw_candidate,) if raw_candidate else (),
            removed_fields=(),
            issue_code=exc.code,
        )

    removed_fields: list[str] = []
    removal_paths = list(_LEGACY_RATE_PATHS)
    container_prefix = legacy_field.rsplit(".", 1)[0] if "." in legacy_field else ""
    for field in (*_RATIO_FIELD_NAMES, *_RATIO_METADATA_NAMES):
        if container_prefix:
            removal_paths.append(f"{container_prefix}.{field}")
        removal_paths.append(field)
    for dotted_path in dict.fromkeys(removal_paths):
        if _remove_nested_field(migrated, dotted_path):
            removed_fields.append(dotted_path)

    migrated["property_tax_policy"] = policy
    evidence = _policy_evidence(policy)
    return MigrationResult(
        property_summary=migrated,
        policy=policy,
        evidence_candidates=(evidence,),
        removed_fields=tuple(removed_fields),
    )


def _safe_json_path(candidate: Path, *, root: Path) -> bool:
    if candidate.suffix.lower() != ".json" or "outputs" in candidate.parts:
        return False
    try:
        root_resolved = root.resolve(strict=True)
        candidate_resolved = candidate.resolve(strict=True)
        candidate_resolved.relative_to(root_resolved)
    except (OSError, RuntimeError, ValueError):
        return False
    return (
        "outputs" not in candidate_resolved.parts
        and candidate_resolved.is_file()
    )


def _iter_json_paths(paths: Sequence[Path]) -> list[Path]:
    discovered: set[Path] = set()
    for path_like in paths:
        path = Path(path_like)
        if "outputs" in path.parts or path.is_symlink():
            continue
        if path.is_dir():
            discovered.update(
                candidate
                for candidate in path.rglob("*.json")
                if _safe_json_path(candidate, root=path)
            )
        elif _safe_json_path(path, root=path.parent):
            discovered.add(path)
    return sorted(discovered, key=lambda candidate: str(candidate))


def _walk_fields(value: object, prefix: str = ""):
    if isinstance(value, Mapping):
        for key, child in value.items():
            field = f"{prefix}.{key}" if prefix else str(key)
            yield field, child
            yield from _walk_fields(child, field)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_fields(child, f"{prefix}[{index}]")


def _walk_field_records(
    value: object,
    prefix: str = "",
    ancestors: tuple[Mapping[str, Any], ...] = (),
):
    if isinstance(value, Mapping):
        nested_ancestors = (*ancestors, value)
        for key, child in value.items():
            field = f"{prefix}.{key}" if prefix else str(key)
            yield field, child, nested_ancestors
            yield from _walk_field_records(child, field, nested_ancestors)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_field_records(
                child,
                f"{prefix}[{index}]",
                ancestors,
            )


def _audit_field_name(full_field: str) -> str | None:
    for legacy_field, _unit in _PRODUCER_FIELD_UNIT.values():
        if full_field.endswith(legacy_field):
            return legacy_field
    leaf = full_field.rsplit(".", 1)[-1]
    if leaf in _AMBIGUOUS_RATE_FIELDS:
        return leaf
    return None


def _audit_ratio_alias(full_field: str) -> str | None:
    leaf = full_field.rsplit(".", 1)[-1]
    if leaf not in _RATIO_FIELD_NAMES:
        return None
    segments = full_field.split(".")
    if len(segments) == 1:
        return leaf
    if "property_summary" in segments or "property_tax_context" in segments:
        return leaf
    return None


def _ratio_alias_diagnostic(
    value: object,
    ancestors: tuple[Mapping[str, Any], ...],
) -> tuple[str, str]:
    invalid_message = "invalid assessment ratio: expected a positive finite decimal"
    if value is None or isinstance(value, bool):
        return "unsupported_property_tax_assessment_override", invalid_message
    try:
        ratio = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return "unsupported_property_tax_assessment_override", invalid_message
    if not ratio.is_finite() or ratio <= 0:
        return "unsupported_property_tax_assessment_override", invalid_message
    if ratio == Decimal("1.00"):
        return (
            "legacy_property_tax_field",
            "legacy assessment ratio requires canonical migration",
        )
    approved = any(
        context.get("analyst_override") is True for context in ancestors
    )
    source = next(
        (
            context.get("assessment_ratio_source")
            for context in reversed(ancestors)
            if isinstance(context.get("assessment_ratio_source"), str)
            and context["assessment_ratio_source"].strip()
        ),
        None,
    )
    if not approved or source is None:
        return (
            "unsupported_property_tax_assessment_override",
            "non-default legacy assessment ratio requires evidence and "
            "explicit analyst approval",
        )
    return (
        "legacy_property_tax_field",
        "approved legacy assessment ratio requires canonical migration",
    )


def _is_unapproved_non_default_ratio(policy: Mapping[str, Any]) -> bool:
    ratio = policy.get("assessment_ratio")
    if ratio is None or isinstance(ratio, bool):
        return False
    try:
        parsed = Decimal(str(ratio).strip())
    except (InvalidOperation, ValueError):
        return False
    return parsed != Decimal("1.00") and policy.get("analyst_override") is not True


def _canonical_policy_issues(
    path: Path,
    payload: object,
) -> tuple[list[MigrationIssue], set[str]]:
    issues: list[MigrationIssue] = []
    policy_fields: set[str] = set()
    for full_field, value in _walk_fields(payload):
        if full_field.rsplit(".", 1)[-1] != "property_tax_policy":
            continue
        policy_fields.add(full_field)
        if not isinstance(value, Mapping):
            issues.append(
                MigrationIssue(
                    path=path,
                    code="invalid_property_tax_millage",
                    field=full_field,
                    value=value,
                    message="canonical property_tax_policy must be an object",
                )
            )
            continue
        if _is_unapproved_non_default_ratio(value):
            issues.append(
                MigrationIssue(
                    path=path,
                    code="unsupported_property_tax_assessment_override",
                    field=f"{full_field}.assessment_ratio",
                    value=value.get("assessment_ratio"),
                    message=(
                        "non-default assessment_ratio requires evidence and "
                        "explicit analyst approval"
                    ),
                )
            )
            continue
        try:
            validate_property_tax_policy(value)
        except PropertyTaxPolicyError as exc:
            field = (
                full_field
                if exc.field == "property_tax_policy"
                else f"{full_field}.{exc.field}"
            )
            issues.append(
                MigrationIssue(
                    path=path,
                    code=exc.code,
                    field=field,
                    value=exc.value,
                    message=str(exc),
                )
            )
    return issues, policy_fields


def audit_reusable_property_tax_inputs(
    paths: Sequence[Path],
) -> list[MigrationIssue]:
    """Report finite legacy/ambiguous tax inputs without rewriting source files."""
    issues: list[MigrationIssue] = []
    for path in _iter_json_paths(paths):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(
                MigrationIssue(
                    path=path,
                    code="invalid_reusable_input",
                    field="/",
                    value=None,
                    message=str(exc),
                )
            )
            continue
        canonical_issues, policy_fields = _canonical_policy_issues(path, payload)
        issues.extend(canonical_issues)
        for full_field, value, ancestors in _walk_field_records(payload):
            if any(
                full_field == policy_field
                or full_field.startswith(f"{policy_field}.")
                for policy_field in policy_fields
            ):
                continue
            ratio_alias = _audit_ratio_alias(full_field)
            if ratio_alias is not None:
                code, message = _ratio_alias_diagnostic(value, ancestors)
                issues.append(
                    MigrationIssue(
                        path=path,
                        code=code,
                        field=ratio_alias,
                        value=value,
                        message=message,
                    )
                )
                continue
            field = _audit_field_name(full_field)
            if field is None:
                continue
            is_known = any(
                field == known_field
                for known_field, _unit in _PRODUCER_FIELD_UNIT.values()
            )
            issues.append(
                MigrationIssue(
                    path=path,
                    code=(
                        "legacy_property_tax_field"
                        if is_known
                        else "missing_property_tax_millage"
                    ),
                    field=field,
                    value=value,
                    message=(
                        "known producer field requires explicit unit migration"
                        if is_known
                        else "ambiguous tax rate requires analyst-labelled mills"
                    ),
                )
            )
    return sorted(
        issues,
        key=lambda issue: (
            str(issue.path),
            issue.code != "unsupported_property_tax_assessment_override",
            issue.field,
        ),
    )
