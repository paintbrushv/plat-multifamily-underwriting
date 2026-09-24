from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from jsonschema.validators import extend

from engine.modules.time_grid import TimeGrid
from engine.modules.util import dec, month_id, parse_month
from engine.property_tax import (
    PropertyTaxPolicyError,
    require_property_tax_policy,
    validate_property_tax_policy,
)
from engine.underwriting_policy import CAP_RATE_SANITY_BAND
from engine.version import SCHEMA_VERSION


@dataclass(frozen=True)
class ValidationIssue:
    severity: str  # "ERROR" or "WARNING"
    code: str
    message: str
    path: str


@dataclass(frozen=True)
class ValidationReport:
    status: str  # "PASS" or "FAIL"
    issues: List[ValidationIssue]

    def to_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "issues": [issue.__dict__ for issue in self.issues]}


def _schema_path() -> Path:
    return Path(__file__).resolve().parent / "schemas" / "deal_schema_v0_1.json"


def _load_schema() -> Dict[str, Any]:
    return json.loads(_schema_path().read_text(encoding="utf-8"))


def _as_json_pointer_path(error: ValidationError) -> str:
    if not error.absolute_path:
        return "/"
    return "/" + "/".join(str(p) for p in error.absolute_path)


def _no_additional_properties(validator, aP, instance, schema):
    for error in Draft202012Validator.VALIDATORS["additionalProperties"](validator, aP, instance, schema):
        yield error


StrictDraft202012Validator = extend(Draft202012Validator, {"additionalProperties": _no_additional_properties})


def validate_schema(inputs: Dict[str, Any]) -> List[ValidationIssue]:
    schema = _load_schema()
    validator = StrictDraft202012Validator(schema)
    issues: List[ValidationIssue] = []
    for error in sorted(validator.iter_errors(inputs), key=lambda e: list(e.path)):
        issues.append(
            ValidationIssue(
                severity="ERROR",
                code="SCHEMA",
                message=error.message,
                path=_as_json_pointer_path(error),
            )
        )
    return issues


def _check_property_tax_policy(
    inputs: Dict[str, Any],
    *,
    required: bool,
) -> List[ValidationIssue]:
    metadata = inputs.get("metadata")
    if not isinstance(metadata, Mapping):
        return []
    property_summary = metadata.get("property_summary") or {}
    if not isinstance(property_summary, Mapping):
        return []
    policy = property_summary.get("property_tax_policy")
    if policy is None and not required:
        return []
    try:
        if required:
            require_property_tax_policy(inputs)
        elif isinstance(policy, Mapping):
            validate_property_tax_policy(policy)
        else:
            require_property_tax_policy(inputs)
    except PropertyTaxPolicyError as exc:
        field = exc.field.replace(".", "/")
        if field.startswith("metadata/"):
            path = f"/{field}"
        elif field == "property_tax_policy":
            path = "/metadata/property_summary/property_tax_policy"
        else:
            path = f"/metadata/property_summary/property_tax_policy/{field}"
        return [
            ValidationIssue(
                severity="ERROR",
                code=exc.code,
                message=str(exc),
                path=path,
            )
        ]
    return []


def _check_non_overlapping_segments(
    rows: List[Dict[str, Any]],
    key_fields: List[str],
    start_field: str,
    end_field: str,
    code: str,
) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        key = "|".join(str(row.get(k)) for k in key_fields)
        grouped.setdefault(key, []).append(row)

    for key, group in grouped.items():
        segments = []
        for row in group:
            start = month_id(row[start_field])
            end = month_id(row[end_field])
            segments.append((start, end))
            if end < start:
                issues.append(
                    ValidationIssue(
                        severity="ERROR",
                        code=code,
                        message=f"Segment end before start for key={key}: {start_field}={row[start_field]} {end_field}={row[end_field]}",
                        path="/",
                    )
                )

        segments.sort()
        for i in range(1, len(segments)):
            prev_start, prev_end = segments[i - 1]
            start, end = segments[i]
            if start <= prev_end:
                issues.append(
                    ValidationIssue(
                        severity="ERROR",
                        code=code,
                        message=f"Overlapping segments for key={key}: [{prev_start},{prev_end}] overlaps [{start},{end}]",
                        path="/",
                    )
                )
    return issues


def _check_coverage(
    month_ids: List[str],
    rows: List[Dict[str, Any]],
    key_field: str,
    code: str,
    label: str,
) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    by_key: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        by_key.setdefault(row[key_field], []).append(row)

    for key, group in by_key.items():
        covered = set()
        for row in group:
            start = month_id(row["start_period"])
            end = month_id(row["end_period"])
            for m in month_ids:
                if start <= m <= end:
                    covered.add(m)
        missing = [m for m in month_ids if m not in covered]
        if missing:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    code=code,
                    message=f"{label} missing coverage for key={key}: {missing}",
                    path="/",
                )
            )
    return issues


def _check_periods_within_grid(
    month_ids: List[str],
    rows: List[Dict[str, Any]],
    fields: List[str],
    code: str,
    label: str,
) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    allowed = set(month_ids)
    for row in rows:
        for f in fields:
            if f not in row or row[f] is None:
                continue
            m = month_id(row[f])
            if m not in allowed:
                issues.append(
                    ValidationIssue(
                        severity="ERROR",
                        code=code,
                        message=f"{label}.{f} not in time grid: {row[f]}",
                        path="/",
                    )
                )
    return issues


def _check_collection_loss_coverage(month_ids: List[str], curve: List[Dict[str, Any]], applies_to: str, code: str) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    missing = []
    for m in month_ids:
        has_specific = any(
            r["applies_to"] == applies_to and month_id(r["start_period"]) <= m <= month_id(r["end_period"]) for r in curve
        )
        has_all = any(r["applies_to"] == "ALL" and month_id(r["start_period"]) <= m <= month_id(r["end_period"]) for r in curve)
        if not (has_specific or has_all):
            missing.append(m)
    if missing:
        issues.append(
            ValidationIssue(
                severity="ERROR",
                code=code,
                message=f"collection_loss_curve missing coverage for applies_to={applies_to} (or ALL): {missing}",
                path="/collection_loss_curve",
            )
        )
    return issues


def _program_active_months(month_ids: List[str], program: Dict[str, Any]) -> List[str]:
    start = month_id(program["start_period"])
    end = month_id(program.get("end_period") or month_ids[-1])
    return [m for m in month_ids if start <= m <= end]


def _check_program_adoption_coverage(month_ids: List[str], programs: List[Dict[str, Any]], adoption: List[Dict[str, Any]]) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    by_program: Dict[str, List[Dict[str, Any]]] = {}
    for row in adoption:
        by_program.setdefault(row["program_id"], []).append(row)

    for program in programs:
        pid = program["program_id"]
        group = by_program.get(pid, [])
        if not group:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    code="COVERAGE_ADOPTION",
                    message=f"program_adoption_curve missing rows for program_id={pid}",
                    path="/program_adoption_curve",
                )
            )
            continue

        covered = set()
        for row in group:
            start = month_id(row["start_period"])
            end = month_id(row["end_period"])
            for m in month_ids:
                if start <= m <= end:
                    covered.add(m)

        active_months = _program_active_months(month_ids, program)
        missing = [m for m in active_months if m not in covered]
        if missing:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    code="COVERAGE_ADOPTION",
                    message=f"program_adoption_curve missing coverage for program_id={pid}: {missing}",
                    path="/program_adoption_curve",
                )
            )

    return issues


def _check_unit_cohorts_uniqueness(inputs: Dict[str, Any]) -> List[ValidationIssue]:
    """Every cohort_id in unit_cohorts must be unique. HARD on duplicate."""
    issues: List[ValidationIssue] = []
    seen: Dict[str, int] = {}
    for c in inputs.get("unit_cohorts", []):
        cid = c.get("cohort_id")
        if cid is None:
            continue
        seen[cid] = seen.get(cid, 0) + 1
    for cid, count in seen.items():
        if count > 1:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    code="UNIT_COHORTS_DUPLICATE_ID",
                    message=f"unit_cohorts.cohort_id duplicated {count}x: {cid}",
                    path="/unit_cohorts",
                )
            )
    return issues


def _check_renovation_programs(inputs: Dict[str, Any], month_ids: List[str]) -> List[ValidationIssue]:
    """Cross-section checks for renovation_programs section.

    Covers:
      (a) target_cohort ∈ unit_cohorts[].cohort_id (HARD on missing)
      (b) output_cohort NOT IN unit_cohorts[].cohort_id (HARD — the cohort-collision bug)
      (c) program_id unique within renovation_programs (HARD)
      (d) output_cohort uniqueness across programs UNLESS sharing target_cohort
      (e) start_month / end_month within time_grid (HARD)
      (f) monthly_pace > 0, rent_premium_monthly >= 0,
          renovation_cost_per_unit > 0, downtime_days >= 0 (HARD)
      (g) strategy ∈ {"on_turnover", "proactive"} (HARD on unknown)
    """
    issues: List[ValidationIssue] = []
    programs = inputs.get("renovation_programs") or []
    if not programs:
        return issues

    cohort_ids = {c["cohort_id"] for c in inputs.get("unit_cohorts", [])}
    allowed_months = set(month_ids)
    allowed_strategies = {"on_turnover", "proactive"}

    program_id_counts: Dict[str, int] = {}
    output_cohort_to_targets: Dict[str, set] = {}

    for idx, program in enumerate(programs):
        path = f"/renovation_programs[{idx}]"
        pid = program.get("program_id")
        target = program.get("target_cohort")
        output = program.get("output_cohort")

        if pid is not None:
            program_id_counts[pid] = program_id_counts.get(pid, 0) + 1

        # (a) target_cohort REF check
        if target is not None and target not in cohort_ids:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    code="RENOVATION_TARGET_COHORT_NOT_FOUND",
                    message=f"renovation_programs.target_cohort not found in unit_cohorts: {target}",
                    path=path,
                )
            )

        # (b) output_cohort collision with existing unit_cohorts (the cohort-collision bug)
        if output is not None and output in cohort_ids:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    code="RENOVATION_OUTPUT_COHORT_COLLISION",
                    message=(
                        f"renovation_programs.output_cohort '{output}' collides with an existing "
                        f"unit_cohorts.cohort_id; output_cohort must be a NEW cohort id"
                    ),
                    path=path,
                )
            )

        # (d) record output -> targets mapping
        if output is not None:
            output_cohort_to_targets.setdefault(output, set())
            if target is not None:
                output_cohort_to_targets[output].add(target)

        # (e) start_month / end_month within time grid
        for f in ("start_month", "end_month"):
            v = program.get(f)
            if v is None:
                continue
            try:
                m = month_id(v)
            except Exception as e:
                issues.append(
                    ValidationIssue(
                        severity="ERROR",
                        code="RENOVATION_PERIOD_INVALID",
                        message=f"renovation_programs.{f} unparseable: {v} ({e})",
                        path=path,
                    )
                )
                continue
            if m not in allowed_months:
                issues.append(
                    ValidationIssue(
                        severity="ERROR",
                        code="RENOVATION_PERIOD_OUT_OF_GRID",
                        message=f"renovation_programs.{f} not in time grid: {v}",
                        path=path,
                    )
                )

        # (f) numeric sanity
        monthly_pace = program.get("monthly_pace")
        if monthly_pace is not None and monthly_pace <= 0:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    code="RENOVATION_PACE_NONPOSITIVE",
                    message=f"renovation_programs.monthly_pace must be > 0: {monthly_pace}",
                    path=path,
                )
            )
        rent_prem = program.get("rent_premium_monthly")
        if rent_prem is not None and rent_prem < 0:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    code="RENOVATION_PREMIUM_NEGATIVE",
                    message=f"renovation_programs.rent_premium_monthly must be >= 0: {rent_prem}",
                    path=path,
                )
            )
        cost = program.get("renovation_cost_per_unit")
        if cost is not None and cost <= 0:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    code="RENOVATION_COST_NONPOSITIVE",
                    message=f"renovation_programs.renovation_cost_per_unit must be > 0: {cost}",
                    path=path,
                )
            )
        downtime = program.get("downtime_days")
        if downtime is not None and downtime < 0:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    code="RENOVATION_DOWNTIME_NEGATIVE",
                    message=f"renovation_programs.downtime_days must be >= 0: {downtime}",
                    path=path,
                )
            )

        # (g) strategy enum
        strategy = program.get("strategy")
        if strategy is not None and strategy not in allowed_strategies:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    code="RENOVATION_STRATEGY_UNKNOWN",
                    message=(
                        f"renovation_programs.strategy unknown: {strategy} "
                        f"(allowed: {sorted(allowed_strategies)})"
                    ),
                    path=path,
                )
            )

    # (c) program_id uniqueness within renovation_programs
    for pid, count in program_id_counts.items():
        if count > 1:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    code="RENOVATION_PROGRAM_ID_DUPLICATE",
                    message=f"renovation_programs.program_id duplicated {count}x: {pid}",
                    path="/renovation_programs",
                )
            )

    # (d) output_cohort uniqueness across programs unless sharing target_cohort
    for output, targets in output_cohort_to_targets.items():
        if len(targets) > 1:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    code="RENOVATION_OUTPUT_COHORT_TARGET_CONFLICT",
                    message=(
                        f"renovation_programs.output_cohort '{output}' is shared by programs "
                        f"with conflicting target_cohorts: {sorted(targets)}; "
                        f"programs sharing an output_cohort must share a target_cohort"
                    ),
                    path="/renovation_programs",
                )
            )

    return issues


def _check_program_id_namespace_uniqueness(inputs: Dict[str, Any]) -> List[ValidationIssue]:
    """program_id must be unique across revenue_programs ∪ renovation_programs.

    Engine treats them in shared lookup namespace (program_adoption_curve etc.).
    """
    issues: List[ValidationIssue] = []
    counts: Dict[str, List[str]] = {}
    for p in inputs.get("revenue_programs", []) or []:
        pid = p.get("program_id")
        if pid is None:
            continue
        counts.setdefault(pid, []).append("revenue_programs")
    for p in inputs.get("renovation_programs", []) or []:
        pid = p.get("program_id")
        if pid is None:
            continue
        counts.setdefault(pid, []).append("renovation_programs")
    for pid, sources in counts.items():
        if len(sources) > 1:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    code="PROGRAM_ID_NAMESPACE_COLLISION",
                    message=(
                        f"program_id '{pid}' appears in multiple program sections: {sources}; "
                        f"program_id must be unique across revenue_programs ∪ renovation_programs"
                    ),
                    path="/",
                )
            )
    return issues


def _check_required_revenue_quality_bridge(inputs: Dict[str, Any]) -> List[ValidationIssue]:
    property_summary = ((inputs.get("metadata") or {}).get("property_summary") or {})
    material = property_summary.get("material_ancillary_income") or {}
    if not material.get("requires_revenue_quality_bridge"):
        return []

    bridge_summary = property_summary.get("revenue_quality_bridge") or {}
    if bridge_summary.get("program_count", 0) > 0:
        return []

    program_ids = {
        str(row.get("program_id") or "")
        for row in inputs.get("revenue_programs", [])
    }
    if any(program_id.startswith("rq_bridge_") for program_id in program_ids):
        return []

    reason = material.get("reason") or "material ancillary/commercial income requires a revenue-quality bridge"
    return [
        ValidationIssue(
            severity="ERROR",
            code="REVENUE_QUALITY_BRIDGE_REQUIRED",
            message=(
                f"{reason}; add a revenue_quality_bridge.json and carry supported "
                "lines into canonical revenue_programs as rq_bridge_* programs before underwriting."
            ),
            path="/metadata/property_summary/material_ancillary_income",
        )
    ]


def _check_growth_bounds(inputs: Dict[str, Any]) -> List[ValidationIssue]:
    """Bound growth_assumptions.annual_growth_rate and per-category opex growth_rate."""
    issues: List[ValidationIssue] = []
    g = inputs.get("growth_assumptions")
    if g is not None:
        rate = g.get("annual_growth_rate")
        if rate is not None:
            if rate < -1.0 or rate > 1.0:
                issues.append(
                    ValidationIssue(
                        severity="ERROR",
                        code="GROWTH_RATE_ABSURD",
                        message=(
                            f"growth_assumptions.annual_growth_rate is mathematically absurd: "
                            f"{rate} (must be in (-1.0, 1.0))"
                        ),
                        path="/growth_assumptions/annual_growth_rate",
                    )
                )
            elif rate < -0.05 or rate > 0.15:
                issues.append(
                    ValidationIssue(
                        severity="WARNING",
                        code="GROWTH_RATE_OUT_OF_BAND",
                        message=(
                            f"growth_assumptions.annual_growth_rate {rate} outside expected "
                            f"band [-0.05, 0.15]"
                        ),
                        path="/growth_assumptions/annual_growth_rate",
                    )
                )

    for idx, row in enumerate(inputs.get("opex_table", []) or []):
        cat_rate = row.get("growth_rate")
        if cat_rate is None:
            continue
        if cat_rate < -0.05 or cat_rate > 0.20:
            issues.append(
                ValidationIssue(
                    severity="WARNING",
                    code="OPEX_GROWTH_RATE_OUT_OF_BAND",
                    message=(
                        f"opex_table[{idx}].growth_rate {cat_rate} outside expected "
                        f"band [-0.05, 0.20] (category={row.get('category_name')})"
                    ),
                    path=f"/opex_table[{idx}]/growth_rate",
                )
            )
    return issues


def _check_vacancy_bounds(inputs: Dict[str, Any]) -> List[ValidationIssue]:
    """physical_vacancy_curve[].vacancy_rate ∈ [0, 0.50] WARN; <0 or >1 HARD."""
    issues: List[ValidationIssue] = []
    for idx, row in enumerate(inputs.get("physical_vacancy_curve", []) or []):
        v = row.get("vacancy_rate")
        if v is None:
            continue
        if v < 0 or v > 1:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    code="VACANCY_RATE_ABSURD",
                    message=(
                        f"physical_vacancy_curve[{idx}].vacancy_rate {v} outside "
                        f"mathematical bounds [0, 1]"
                    ),
                    path=f"/physical_vacancy_curve[{idx}]/vacancy_rate",
                )
            )
        elif v > 0.50:
            issues.append(
                ValidationIssue(
                    severity="WARNING",
                    code="VACANCY_RATE_OUT_OF_BAND",
                    message=(
                        f"physical_vacancy_curve[{idx}].vacancy_rate {v} above "
                        f"expected band 0.50 (cohort={row.get('cohort_id')})"
                    ),
                    path=f"/physical_vacancy_curve[{idx}]/vacancy_rate",
                )
            )
    return issues


def _check_collection_loss_bounds(inputs: Dict[str, Any]) -> List[ValidationIssue]:
    """collection_loss_curve[].loss_rate ∈ [0, 0.20] WARN."""
    issues: List[ValidationIssue] = []
    for idx, row in enumerate(inputs.get("collection_loss_curve", []) or []):
        v = row.get("loss_rate")
        if v is None:
            continue
        if v < 0 or v > 0.20:
            issues.append(
                ValidationIssue(
                    severity="WARNING",
                    code="COLLECTION_LOSS_OUT_OF_BAND",
                    message=(
                        f"collection_loss_curve[{idx}].loss_rate {v} outside expected "
                        f"band [0, 0.20] (applies_to={row.get('applies_to')})"
                    ),
                    path=f"/collection_loss_curve[{idx}]/loss_rate",
                )
            )
    return issues


def _compute_year_one_noi(inputs: Dict[str, Any]) -> Optional[Decimal]:
    """Approximate Year-1 NOI from cohorts/curves/opex for going-in cap derivation.

    This is a coarse approximation (does not run the full engine):
      GPR_y1 ≈ Σ_cohort (initial_inplace_rent × unit_count × 12)
      EGR_y1 ≈ GPR_y1 × (1 - vacancy_y1) × (1 - collection_loss_y1)
      OpEx_y1 ≈ Σ opex_table base_value (fixed_annual + per-unit-extrapolated)
      NOI_y1 ≈ EGR_y1 - OpEx_y1
    Returns None if any required structure is missing.
    """
    cohorts = inputs.get("unit_cohorts") or []
    if not cohorts:
        return None

    total_units = sum(dec(c.get("unit_count", 0)) for c in cohorts)
    if total_units == 0:
        return None

    gpr = Decimal("0")
    for c in cohorts:
        rent = c.get("initial_inplace_rent")
        units = c.get("unit_count", 0)
        if rent is None:
            continue
        gpr += dec(rent) * dec(units) * Decimal("12")

    # Year-1 vacancy: pick first segment's vacancy_rate (use weighted unit-mix avg)
    vac_rows = inputs.get("physical_vacancy_curve") or []
    if vac_rows:
        # take the average across cohorts at start of analysis
        vac_sum = Decimal("0")
        vac_n = 0
        for c in cohorts:
            cid = c.get("cohort_id")
            seg = next((r for r in vac_rows if r.get("cohort_id") == cid), None)
            if seg and seg.get("vacancy_rate") is not None:
                vac_sum += dec(seg["vacancy_rate"]) * dec(c.get("unit_count", 0))
                vac_n += int(c.get("unit_count", 0))
        vacancy = (vac_sum / dec(vac_n)) if vac_n else Decimal("0.05")
    else:
        vacancy = Decimal("0.05")

    cl_rows = inputs.get("collection_loss_curve") or []
    if cl_rows:
        cl = next(
            (r for r in cl_rows if r.get("applies_to") in ("Rent", "ALL")),
            None,
        )
        collection_loss = dec(cl["loss_rate"]) if cl and cl.get("loss_rate") is not None else Decimal("0.01")
    else:
        collection_loss = Decimal("0.01")

    egr = gpr * (Decimal("1") - vacancy) * (Decimal("1") - collection_loss)

    # Approximate Year-1 OpEx
    opex = Decimal("0")
    for row in inputs.get("opex_table", []) or []:
        ctype = row.get("calculation_type")
        base = row.get("base_value")
        if base is None:
            continue
        bv = dec(base)
        if ctype == "fixed_annual":
            opex += bv
        elif ctype == "per_unit":
            opex += bv * total_units
        elif ctype == "per_unit_monthly":
            opex += bv * total_units * Decimal("12")
        elif ctype == "percent_egr":
            opex += bv * egr

    return egr - opex


def _check_exit_cap_vs_going_in(inputs: Dict[str, Any]) -> List[ValidationIssue]:
    """Compute Year-1 going-in cap from cohorts/curves; WARN if exit < going-in.

    Also WARN if exit_cap_rate or going-in is outside [0.03, 0.12] sanity bound.
    """
    issues: List[ValidationIssue] = []
    exit_assumptions = inputs.get("exit_assumptions")
    purchase = inputs.get("purchase_assumptions")
    if exit_assumptions is None or purchase is None:
        return issues

    exit_cap = exit_assumptions.get("exit_cap_rate")
    purchase_price = purchase.get("purchase_price")
    if exit_cap is None or purchase_price in (None, 0):
        return issues

    _cap_lo, _cap_hi = CAP_RATE_SANITY_BAND
    if exit_cap < _cap_lo or exit_cap > _cap_hi:
        issues.append(
            ValidationIssue(
                severity="WARNING",
                code="EXIT_CAP_RATE_OUT_OF_BAND",
                message=(
                    f"exit_assumptions.exit_cap_rate {exit_cap} outside sanity band "
                    f"[{_cap_lo:.2f}, {_cap_hi:.2f}]"
                ),
                path="/exit_assumptions/exit_cap_rate",
            )
        )

    noi_y1 = _compute_year_one_noi(inputs)
    if noi_y1 is None or noi_y1 <= 0:
        return issues

    going_in = noi_y1 / dec(purchase_price)

    if going_in < dec(str(_cap_lo)) or going_in > dec(str(_cap_hi)):
        issues.append(
            ValidationIssue(
                severity="WARNING",
                code="GOING_IN_CAP_OUT_OF_BAND",
                message=(
                    f"derived going-in cap (Year-1 NOI / purchase_price) is "
                    f"{float(going_in):.4f}, outside sanity band [{_cap_lo:.2f}, {_cap_hi:.2f}]"
                ),
                path="/exit_assumptions/exit_cap_rate",
            )
        )

    if dec(exit_cap) < going_in:
        issues.append(
            ValidationIssue(
                severity="WARNING",
                code="EXIT_CAP_LOWER_THAN_GOING_IN",
                message=(
                    f"exit_cap_rate ({exit_cap}) is lower than derived going-in cap "
                    f"({float(going_in):.4f}); cap-rate compression silently inflates exit value"
                ),
                path="/exit_assumptions/exit_cap_rate",
            )
        )

    return issues


def _check_pricing_provenance_consistency(inputs: Dict[str, Any]) -> List[ValidationIssue]:
    """Pricing provenance invariants 1-5 from stage_5 audit.

    1. strike_price_basis == "broker_whisper" → broker_whisper_price not null
    2. strike_price_basis == "broker_whisper_minus_5pct" →
         abs(strike_price - broker_whisper_price * 0.95) / strike_price < 0.01
    3. strike_price_basis == "om_published" →
         published_om_price not null AND == strike_price
    4. om_pricing_process == "best_offers_loi_unpriced" → published_om_price IS null
    5. as_of_date parseable AND ≤ today (HARD on future; WARN if > 90 days old)
    """
    issues: List[ValidationIssue] = []
    pricing = inputs.get("pricing_provenance")
    if pricing is None:
        return issues

    basis = pricing.get("strike_price_basis")
    strike = pricing.get("strike_price")
    whisper = pricing.get("broker_whisper_price")
    om_published = pricing.get("published_om_price")
    om_process = pricing.get("om_pricing_process")
    as_of = pricing.get("as_of_date")

    # 1
    if basis == "broker_whisper" and whisper is None:
        issues.append(
            ValidationIssue(
                severity="ERROR",
                code="PRICING_PROVENANCE_WHISPER_MISSING",
                message=(
                    "pricing_provenance.strike_price_basis='broker_whisper' but "
                    "broker_whisper_price is null"
                ),
                path="/pricing_provenance/broker_whisper_price",
            )
        )

    # 2
    if basis == "broker_whisper_minus_5pct":
        if whisper is None:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    code="PRICING_PROVENANCE_WHISPER_MISSING",
                    message=(
                        "pricing_provenance.strike_price_basis='broker_whisper_minus_5pct' but "
                        "broker_whisper_price is null"
                    ),
                    path="/pricing_provenance/broker_whisper_price",
                )
            )
        elif strike is not None and strike != 0:
            expected = dec(whisper) * dec("0.95")
            ratio = abs(dec(strike) - expected) / dec(strike)
            if ratio >= dec("0.01"):
                issues.append(
                    ValidationIssue(
                        severity="ERROR",
                        code="PRICING_PROVENANCE_WHISPER_DERIVATION_MISMATCH",
                        message=(
                            f"strike_price_basis='broker_whisper_minus_5pct' but "
                            f"abs(strike_price - broker_whisper_price*0.95)/strike_price "
                            f"= {float(ratio):.4f} (must be < 0.01); "
                            f"strike={strike}, whisper={whisper}, expected≈{float(expected)}"
                        ),
                        path="/pricing_provenance/strike_price",
                    )
                )

    # 3
    if basis == "om_published":
        if om_published is None:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    code="PRICING_PROVENANCE_OM_PRICE_MISSING",
                    message=(
                        "pricing_provenance.strike_price_basis='om_published' but "
                        "published_om_price is null"
                    ),
                    path="/pricing_provenance/published_om_price",
                )
            )
        elif strike is not None and om_published != strike:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    code="PRICING_PROVENANCE_OM_PRICE_MISMATCH",
                    message=(
                        f"strike_price_basis='om_published' but published_om_price "
                        f"({om_published}) != strike_price ({strike})"
                    ),
                    path="/pricing_provenance/published_om_price",
                )
            )

    # 4
    if om_process == "best_offers_loi_unpriced" and om_published is not None:
        issues.append(
            ValidationIssue(
                severity="ERROR",
                code="PRICING_PROVENANCE_UNPRICED_OM_HAS_PRICE",
                message=(
                    f"om_pricing_process='best_offers_loi_unpriced' but "
                    f"published_om_price is not null ({om_published})"
                ),
                path="/pricing_provenance/published_om_price",
            )
        )

    # 5
    if as_of is not None:
        try:
            as_of_dt = datetime.strptime(as_of, "%Y-%m-%d").date()
        except (ValueError, TypeError) as e:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    code="PRICING_PROVENANCE_AS_OF_INVALID",
                    message=f"pricing_provenance.as_of_date unparseable: {as_of} ({e})",
                    path="/pricing_provenance/as_of_date",
                )
            )
        else:
            today = date.today()
            if as_of_dt > today:
                issues.append(
                    ValidationIssue(
                        severity="ERROR",
                        code="PRICING_PROVENANCE_AS_OF_FUTURE",
                        message=(
                            f"pricing_provenance.as_of_date is in the future: "
                            f"{as_of} > today {today.isoformat()}"
                        ),
                        path="/pricing_provenance/as_of_date",
                    )
                )
            elif (today - as_of_dt) > timedelta(days=90):
                issues.append(
                    ValidationIssue(
                        severity="WARNING",
                        code="PRICING_PROVENANCE_AS_OF_STALE",
                        message=(
                            f"pricing_provenance.as_of_date is more than 90 days old: "
                            f"{as_of} (age {(today - as_of_dt).days} days)"
                        ),
                        path="/pricing_provenance/as_of_date",
                    )
                )

    return issues


def _check_dual_encoding_overlap(inputs: Dict[str, Any]) -> List[ValidationIssue]:
    """WARN if revenue_programs.program_id matches reno_premium_* AND its eligible_units
    cohort_id ALSO appears as a renovation_programs[].target_cohort.

    This is the dual-encoding fragility from code_review_findings Fix 4.
    """
    issues: List[ValidationIssue] = []
    reno_targets = {
        p.get("target_cohort")
        for p in (inputs.get("renovation_programs") or [])
        if p.get("target_cohort") is not None
    }
    if not reno_targets:
        return issues
    for program in inputs.get("revenue_programs", []) or []:
        pid = program.get("program_id")
        eligible = program.get("eligible_units")
        if not pid or not isinstance(pid, str):
            continue
        if not pid.startswith("reno_premium_"):
            continue
        if eligible in reno_targets:
            issues.append(
                ValidationIssue(
                    severity="WARNING",
                    code="RENOVATION_DUAL_ENCODING_WARN",
                    message=(
                        f"revenue_programs.program_id='{pid}' (reno_premium_*) targets "
                        f"cohort '{eligible}' which is ALSO a renovation_programs.target_cohort; "
                        f"dual encoding is fragile and may double-count the renovation premium"
                    ),
                    path="/revenue_programs",
                )
            )
    return issues


def _check_comp_evidence_for_premium(
    inputs: Dict[str, Any],
    *,
    deal_root: Optional[Path] = None,
    run_id: Optional[str] = None,
    federation_mode: bool = False,
) -> List[ValidationIssue]:
    """Wave 6 Task 6.3 — Comp-evidence gating for any cohort claiming a rent premium.

    For each renovation_program with rent_premium_monthly > 0, AND for each
    revenue_program whose program_id starts with ``reno_premium_``, this check
    requires a comp-evidence entry in ``<deal_root>/outputs/<run_id>/market_study/comps.json``
    for the relevant cohort.

    Severity tiering (per Q5 user answer):
      - ``federation_mode=True``: missing evidence emits a HARD
        ``COMP_EVIDENCE_MISSING`` issue (blocks federation runs).
      - ``federation_mode=False``: missing evidence emits a WARN
        ``COMP_EVIDENCE_RECOMMENDED`` (advisory only).

    When ``deal_root`` and ``run_id`` are not both provided we cannot inspect
    the on-disk evidence; we fall back to emitting only the WARN tier so that
    tests and ad-hoc engine calls aren't blocked by missing federation context.
    """
    issues: List[ValidationIssue] = []

    # Cohorts that claim a renovation premium and therefore demand comp evidence.
    cohorts_needing_evidence: List[Dict[str, str]] = []

    for idx, program in enumerate(inputs.get("renovation_programs") or []):
        premium = program.get("rent_premium_monthly")
        if premium is None or premium <= 0:
            continue
        target = program.get("target_cohort")
        if target is None:
            continue
        cohorts_needing_evidence.append({
            "cohort_id": target,
            "program_id": program.get("program_id") or f"renovation_programs[{idx}]",
            "source": "renovation_programs",
            "path": f"/renovation_programs[{idx}]",
        })

    for idx, program in enumerate(inputs.get("revenue_programs") or []):
        pid = program.get("program_id")
        if not isinstance(pid, str) or not pid.startswith("reno_premium_"):
            continue
        eligible = program.get("eligible_units")
        if not eligible or eligible == "ALL":
            continue
        cohorts_needing_evidence.append({
            "cohort_id": eligible,
            "program_id": pid,
            "source": "revenue_programs",
            "path": f"/revenue_programs[{idx}]",
        })

    if not cohorts_needing_evidence:
        return issues

    # Load on-disk comp evidence if federation context provided.
    #
    # File precedence (per market-study-agent's actual federation outputs;
    # see runs/deals/a prior production deal/outputs/run_002/market_study/
    # for the canonical layout):
    #   1. comps_cohort_grouped.json — THE structured federation output:
    #      {"comps_by_cohort": {"<bedrooms>BR_<bathrooms>BA_<sqft>sf": [...]}}
    #      Populated by collect_comps_snapshot.py + Playwright fallback.
    #      Cohort keys are STRUCTURAL (1BR_1BA_660sf), not the canonical
    #      named cohort_ids (beal/bradford/etc.) — so cross-checking per
    #      named cohort_id requires a structural-key reverse lookup that
    #      this gate can't do without unit_cohorts.bedrooms/bathrooms/sqft.
    #      Treat as "evidence-present" when the file exists AND has at
    #      least one populated cohort.
    #   2. comps.json — legacy raw-snapshot dump (per-comp keyed, mostly
    #      empty when scrapers fail). Accept as evidence ONLY if it has
    #      a non-empty comps_by_cohort field (ie was used as the
    #      structured contract output by an older convention).
    comp_cohorts_present: Optional[set] = None
    evidence_path: Optional[Path] = None
    if deal_root is not None and run_id is not None:
        market_study_dir = Path(deal_root) / "outputs" / run_id / "market_study"
        cohort_grouped = market_study_dir / "comps_cohort_grouped.json"
        legacy_comps = market_study_dir / "comps.json"

        candidates = [cohort_grouped, legacy_comps]
        for candidate in candidates:
            if not candidate.exists():
                continue
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(payload, dict):
                continue
            cbc = payload.get("comps_by_cohort")
            if isinstance(cbc, dict) and any(cbc.values()):
                # File exists AND has populated cohort data — accept as evidence.
                evidence_path = candidate
                comp_cohorts_present = set(cbc.keys())
                # Also accept the permissive {cohort_id: ...} mapping shape.
                for k, v in payload.items():
                    if k in {"comps_by_cohort", "cohorts", "metadata", "as_of_utc",
                             "run_date", "metro", "metro_slug", "comps"}:
                        continue
                    if isinstance(v, (list, dict)):
                        comp_cohorts_present.add(k)
                cohorts_field = payload.get("cohorts")
                if isinstance(cohorts_field, list):
                    for entry in cohorts_field:
                        if isinstance(entry, dict) and entry.get("cohort_id"):
                            comp_cohorts_present.add(entry["cohort_id"])
                break

        # If no candidate had populated cohort data, evidence_path stays
        # None. Falls through to per-cohort missing-evidence flagging.
        if evidence_path is None:
            comp_cohorts_present = set()
            evidence_path = cohort_grouped  # for diagnostic message

    have_federation_context = deal_root is not None and run_id is not None

    # Build a NAMED -> structural-cohort lookup from canonical's unit_cohorts
    # so we can bridge canonical's named cohort_id ('beal', 'bradford', ...)
    # to market-study-agent's structural cohort_key ('1BR_1BA_530sf',
    # '1BR_1.0BA_530sf', ...). See plat_agent.contracts.domain.market_study
    # for the canonical cohort_key() format. We construct BOTH the integer-
    # bath form (e.g. '1BA') and the {:.1f}-bath form ('1.0BA') because
    # the COHORT_KEY_PATTERN regex permits both and live comp files
    # (e.g. comps_cohort_grouped.json) use the integer form.
    unit_cohorts_by_id: Dict[str, Dict[str, Any]] = {}
    for uc in inputs.get("unit_cohorts") or []:
        if isinstance(uc, dict) and uc.get("cohort_id"):
            unit_cohorts_by_id[uc["cohort_id"]] = uc

    # Pre-extract sqft values from comp-file structural keys so we can
    # do sqft-only tolerance matching when canonical's unit_cohort is
    # missing bedrooms/bathrooms (which is the common case today —
    # canonical's unit_cohorts only carries cohort_id/unit_type/sqft).
    comp_keys_with_sqft: List[tuple] = []  # [(comp_key, sqft_int), ...]
    if comp_cohorts_present:
        import re as _re
        _STRUCT_RE = _re.compile(r"^(\d+)BR_(\d+(?:\.\d)?)BA_(\d+)sf$")
        for ck in comp_cohorts_present:
            m = _STRUCT_RE.match(ck) if isinstance(ck, str) else None
            if m:
                comp_keys_with_sqft.append((ck, int(m.group(3))))

    def _structural_candidates(uc: Dict[str, Any]) -> List[str]:
        """Return possible structural cohort_keys for this unit_cohort.

        Includes ±10% sqft variants and BOTH bath formats ('1BA', '1.0BA')
        because comp files in the wild use either.

        Returns [] if bedrooms/bathrooms/sqft are not all populated — the
        caller falls back to NAMED-only matching (and, if available, to
        sqft-tolerance matching against parsed comp-file keys).
        """
        b = uc.get("bedrooms")
        ba = uc.get("bathrooms")
        sf = uc.get("sqft")
        if b is None or ba is None or sf is None:
            return []
        try:
            b_int = int(b)
            ba_f = float(ba)
            sf_int = int(sf)
        except (TypeError, ValueError):
            return []
        # ±10% sqft tolerance: comp files key by the COMP's sqft, not the
        # subject's, so a Bradford 660sf subject still matches a comp
        # cohort '1BR_1BA_630sf' (~5% delta). 10% covers typical
        # market-study cohort grouping latitude without broadening the
        # gate (a 530sf studio still won't match a 1052sf 2BR comp).
        sqft_lo = int(round(sf_int * 0.90))
        sqft_hi = int(round(sf_int * 1.10))
        candidates: List[str] = []
        # Match plat_agent.contracts.domain.market_study.cohort_key()
        # exactly: f"{int(b)}BR_{float(ba):.1f}BA_{int(sf)}sf"
        # AND emit the integer-bath variant since live comp files use it.
        ba_int_str = f"{int(ba_f)}" if float(ba_f).is_integer() else f"{ba_f:.1f}"
        ba_dec_str = f"{ba_f:.1f}"
        for s in range(sqft_lo, sqft_hi + 1):
            candidates.append(f"{b_int}BR_{ba_dec_str}BA_{s}sf")
            if ba_dec_str != ba_int_str:
                candidates.append(f"{b_int}BR_{ba_int_str}BA_{s}sf")
        return candidates

    def _evidence_present_for(cid: str) -> bool:
        if comp_cohorts_present is None:
            return False
        # 1) Direct named match (cid as-is is one of the comp file keys).
        if cid in comp_cohorts_present:
            return True
        uc = unit_cohorts_by_id.get(cid)
        if uc is None:
            return False
        # 2) Structural-key bridge: construct candidate cohort_keys from
        #    bedrooms/bathrooms/sqft (with ±10% sqft tolerance) and
        #    check whether any matches a comp file key.
        for cand in _structural_candidates(uc):
            if cand in comp_cohorts_present:
                return True
        # 3) sqft-only fallback when bedrooms/bathrooms are missing on
        #    the canonical unit_cohort but sqft is populated — match
        #    against the comp-file's parsed structural keys within
        #    ±10% sqft. This is the realistic case for canonicals
        #    produced by the property-extractor today (cohort_id +
        #    unit_type + sqft only). Bedroom/bath check is skipped
        #    here because the data simply isn't there; the gate is
        #    still a HARD error when no sqft is in tolerance.
        sf = uc.get("sqft")
        if sf is not None and (uc.get("bedrooms") is None or uc.get("bathrooms") is None):
            try:
                sf_int = int(sf)
            except (TypeError, ValueError):
                return False
            sqft_lo = sf_int * 0.90
            sqft_hi = sf_int * 1.10
            for _ck, ck_sf in comp_keys_with_sqft:
                if sqft_lo <= ck_sf <= sqft_hi:
                    return True
        return False

    for entry in cohorts_needing_evidence:
        cid = entry["cohort_id"]
        pid = entry["program_id"]
        path = entry["path"]

        evidence_present = _evidence_present_for(cid)

        if evidence_present:
            continue

        # No evidence on disk — pick severity based on federation_mode.
        if federation_mode and have_federation_context:
            missing_loc = str(evidence_path) if evidence_path is not None else "<unknown>"
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    code="COMP_EVIDENCE_MISSING",
                    message=(
                        f"comp evidence missing for cohort '{cid}' (program '{pid}') "
                        f"that claims a renovation rent premium; expected entry at "
                        f"{missing_loc} (federation_mode requires comp evidence "
                        f"for any non-zero rent premium)"
                    ),
                    path=path,
                )
            )
        else:
            issues.append(
                ValidationIssue(
                    severity="WARNING",
                    code="COMP_EVIDENCE_RECOMMENDED",
                    message=(
                        f"comp evidence not verified for cohort '{cid}' (program '{pid}') "
                        f"that claims a renovation rent premium; recommend running "
                        f"market-study-agent comp-finder and emitting comps.json before "
                        f"relying on this premium"
                    ),
                    path=path,
                )
            )

    return issues


def _check_concession_overlap(inputs: Dict[str, Any]) -> List[ValidationIssue]:
    """HARD if concession_schedule has overlapping (applies_to_cohort, concession_type) periods."""
    schedule = inputs.get("concession_schedule") or []
    if not schedule:
        return []
    return _check_non_overlapping_segments(
        schedule,
        ["applies_to_cohort", "concession_type"],
        "start_month",
        "end_month",
        "SEGMENT_CONCESSION",
    )


def validate_augmented_curves(
    augmented_mr_curve: List[Dict[str, Any]],
    augmented_ltl: List[Dict[str, Any]],
    augmented_vac: List[Dict[str, Any]],
) -> ValidationReport:
    """Post-renovation-context check: assert augmented curves have no overlap.

    Engine.py calls this AFTER _build_renovation_context (line ~401) so we
    catch any collisions introduced by the renovation context-builder.

    Returns a ValidationReport (status='PASS' or 'FAIL').
    """
    issues: List[ValidationIssue] = []
    issues.extend(
        _check_non_overlapping_segments(
            augmented_mr_curve, ["cohort_id"], "start_period", "end_period",
            "SEGMENT_MARKET_RENT_AUGMENTED",
        )
    )
    issues.extend(
        _check_non_overlapping_segments(
            augmented_ltl, ["cohort_id"], "start_period", "end_period",
            "SEGMENT_LTL_AUGMENTED",
        )
    )
    issues.extend(
        _check_non_overlapping_segments(
            augmented_vac, ["cohort_id"], "start_period", "end_period",
            "SEGMENT_VACANCY_AUGMENTED",
        )
    )
    status = "FAIL" if any(i.severity == "ERROR" for i in issues) else "PASS"
    return ValidationReport(status=status, issues=issues)


def validate_deal(
    inputs: Dict[str, Any],
    strict: bool = True,
    *,
    deal_root: Optional[Path] = None,
    run_id: Optional[str] = None,
    federation_mode: bool = False,
) -> ValidationReport:
    """Validate deal inputs against schema and business rules.

    Args:
        inputs: Canonical deal inputs dict
        strict: If True (default), schema violations are ERRORs.
                If False, missing optional sections (debt_terms,
                purchase_assumptions, exit_assumptions, fund_assumptions)
                are reported as WARNINGs instead of ERRORs, allowing
                partial validation of ingested deals.
        deal_root: Optional path to the deal repo root. When provided alongside
                ``run_id``, enables on-disk comp evidence checking against
                ``<deal_root>/outputs/<run_id>/market_study/comps.json``.
        run_id: Optional federation run id. See ``deal_root``.
        federation_mode: When True, missing comp evidence for cohorts claiming
                a renovation rent premium emits a HARD ``COMP_EVIDENCE_MISSING``
                issue. When False (default), the same condition emits an
                advisory ``COMP_EVIDENCE_RECOMMENDED`` warning only.
    """

    def _is_missing_optional_section(issue: ValidationIssue) -> bool:
        if issue.severity != "ERROR" or issue.code != "SCHEMA":
            return False
        return any(
            (
                issue.path == "/" and f"'{section}' is a required property" in issue.message
            )
            or (
                issue.path == f"/{section}" and "is a required property" in issue.message
            )
            for section in _optional_sections
        )

    issues = validate_schema(inputs)
    issues.extend(_check_property_tax_policy(inputs, required=False))

    if not strict:
        # Downgrade missing-optional-section errors to warnings
        _optional_sections = {
            "purchase_assumptions", "debt_terms", "exit_assumptions",
            "fund_assumptions", "opex_table", "growth_assumptions",
            "capex_schedule", "renovation_programs", "turnover_assumptions",
            "replacement_reserves",
        }
        downgraded = []
        for issue in issues:
            if _is_missing_optional_section(issue):
                downgraded.append(ValidationIssue(
                    severity="WARNING",
                    code=issue.code,
                    message=issue.message,
                    path=issue.path,
                ))
            else:
                downgraded.append(issue)
        issues = downgraded

    if any(i.severity == "ERROR" for i in issues):
        return ValidationReport(status="FAIL", issues=issues)

    if inputs.get("schema_version") != SCHEMA_VERSION:
        return ValidationReport(
            status="FAIL",
            issues=[
                ValidationIssue(
                    severity="ERROR",
                    code="VERSION",
                    message=f"Unsupported schema_version: {inputs.get('schema_version')}",
                    path="/schema_version",
                )
            ],
        )

    time_grid = TimeGrid.build(inputs["time_grid"]["analysis_start_date"], inputs["time_grid"]["analysis_end_date"])
    month_ids = time_grid.month_ids

    cohort_ids = {c["cohort_id"] for c in inputs["unit_cohorts"]}
    for table_name in ("market_rent_curve", "loss_to_lease", "physical_vacancy_curve"):
        for row in inputs[table_name]:
            if row["cohort_id"] not in cohort_ids:
                issues.append(
                    ValidationIssue(
                        severity="ERROR",
                        code="REF",
                        message=f"{table_name}.cohort_id not found: {row['cohort_id']}",
                        path=f"/{table_name}",
                    )
                )

    program_ids = {p["program_id"] for p in inputs["revenue_programs"]}
    for row in inputs["program_adoption_curve"]:
        if row["program_id"] not in program_ids:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    code="REF",
                    message=f"program_adoption_curve.program_id not found: {row['program_id']}",
                    path="/program_adoption_curve",
                )
            )

    for program in inputs["revenue_programs"]:
        eligible = program["eligible_units"]
        if eligible == "custom":
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    code="PROGRAM_ELIGIBILITY",
                    message="eligible_units='custom' is declared by the schema spec but the v0.1 input contract does not define how custom eligibility is expressed in inputs.json yet.",
                    path="/revenue_programs",
                )
            )
        elif eligible not in ("ALL",) and eligible not in cohort_ids:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    code="PROGRAM_ELIGIBILITY",
                    message=f"revenue_programs.eligible_units must be 'ALL' or a valid cohort_id (custom eligibility is not specified in v0.1 inputs): {eligible}",
                    path="/revenue_programs",
                )
            )
        if program["pricing_type"] not in ("$/unit", "% rent", "$/asset", "$/occupied_unit"):
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    code="PROGRAM_PRICING",
                    message=f"pricing_type not implemented: {program['pricing_type']}",
                    path="/revenue_programs",
                )
            )

    issues.extend(_check_periods_within_grid(month_ids, inputs["market_rent_curve"], ["start_period", "end_period"], "TIME", "market_rent_curve"))
    issues.extend(_check_periods_within_grid(month_ids, inputs["loss_to_lease"], ["start_period", "end_period"], "TIME", "loss_to_lease"))
    issues.extend(
        _check_periods_within_grid(month_ids, inputs["physical_vacancy_curve"], ["start_period", "end_period"], "TIME", "physical_vacancy_curve")
    )
    issues.extend(
        _check_periods_within_grid(month_ids, inputs["collection_loss_curve"], ["start_period", "end_period"], "TIME", "collection_loss_curve")
    )
    issues.extend(
        _check_periods_within_grid(month_ids, inputs["program_adoption_curve"], ["start_period", "end_period"], "TIME", "program_adoption_curve")
    )
    issues.extend(_check_periods_within_grid(month_ids, inputs["revenue_programs"], ["start_period", "end_period"], "TIME", "revenue_programs"))

    issues.extend(
        _check_non_overlapping_segments(inputs["market_rent_curve"], ["cohort_id"], "start_period", "end_period", "SEGMENT_MARKET_RENT")
    )
    issues.extend(_check_non_overlapping_segments(inputs["loss_to_lease"], ["cohort_id"], "start_period", "end_period", "SEGMENT_LTL"))
    issues.extend(
        _check_non_overlapping_segments(inputs["physical_vacancy_curve"], ["cohort_id"], "start_period", "end_period", "SEGMENT_VACANCY")
    )
    issues.extend(
        _check_non_overlapping_segments(inputs["collection_loss_curve"], ["applies_to"], "start_period", "end_period", "SEGMENT_COLLECTION_LOSS")
    )
    issues.extend(
        _check_non_overlapping_segments(inputs["program_adoption_curve"], ["program_id"], "start_period", "end_period", "SEGMENT_ADOPTION")
    )

    issues.extend(_check_coverage(month_ids, inputs["market_rent_curve"], "cohort_id", "COVERAGE_MARKET_RENT", "market_rent_curve"))
    issues.extend(_check_coverage(month_ids, inputs["loss_to_lease"], "cohort_id", "COVERAGE_LTL", "loss_to_lease"))
    issues.extend(_check_coverage(month_ids, inputs["physical_vacancy_curve"], "cohort_id", "COVERAGE_VACANCY", "physical_vacancy_curve"))
    issues.extend(_check_collection_loss_coverage(month_ids, inputs["collection_loss_curve"], "Rent", "COVERAGE_COLLECTION_LOSS_RENT"))
    if inputs["revenue_programs"]:
        issues.extend(_check_collection_loss_coverage(month_ids, inputs["collection_loss_curve"], "Programs", "COVERAGE_COLLECTION_LOSS_PROGRAMS"))
        issues.extend(_check_program_adoption_coverage(month_ids, inputs["revenue_programs"], inputs["program_adoption_curve"]))

    # Cross-section consistency: pricing_provenance.strike_price must equal purchase_assumptions.purchase_price
    pricing = inputs.get("pricing_provenance")
    purchase = inputs.get("purchase_assumptions")
    if pricing is not None and purchase is not None:
        strike = pricing.get("strike_price")
        purchase_price = purchase.get("purchase_price")
        if strike is not None and purchase_price is not None and strike != purchase_price:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    code="SCHEMA_VIOLATION",
                    message=(
                        f"pricing_provenance.strike_price ({strike}) must equal "
                        f"purchase_assumptions.purchase_price ({purchase_price})"
                    ),
                    path="/pricing_provenance/strike_price",
                )
            )

    # ------------------------------------------------------------------
    # New cross-section invariants (Wave 1a Task 1.1):
    # Order: uniqueness checks first (cohort, program_id), then renovation_programs
    # (which depends on those), then bounds checks, then provenance, then dual-encoding warn.
    # ------------------------------------------------------------------
    issues.extend(_check_unit_cohorts_uniqueness(inputs))
    issues.extend(_check_program_id_namespace_uniqueness(inputs))
    issues.extend(_check_required_revenue_quality_bridge(inputs))
    issues.extend(_check_renovation_programs(inputs, month_ids))
    issues.extend(_check_growth_bounds(inputs))
    issues.extend(_check_vacancy_bounds(inputs))
    issues.extend(_check_collection_loss_bounds(inputs))
    issues.extend(_check_exit_cap_vs_going_in(inputs))
    issues.extend(_check_pricing_provenance_consistency(inputs))
    issues.extend(_check_concession_overlap(inputs))
    issues.extend(_check_dual_encoding_overlap(inputs))
    issues.extend(
        _check_comp_evidence_for_premium(
            inputs,
            deal_root=deal_root,
            run_id=run_id,
            federation_mode=federation_mode,
        )
    )

    return ValidationReport(status="FAIL" if any(i.severity == "ERROR" for i in issues) else "PASS", issues=issues)


def validate_or_raise(inputs: Dict[str, Any]) -> ValidationReport:
    report = validate_deal(inputs)
    issues = list(report.issues)
    existing = {(issue.code, issue.path) for issue in issues}
    issues.extend(
        issue
        for issue in _check_property_tax_policy(inputs, required=True)
        if (issue.code, issue.path) not in existing
    )
    report = ValidationReport(
        status=(
            "FAIL"
            if any(issue.severity == "ERROR" for issue in issues)
            else "PASS"
        ),
        issues=issues,
    )
    if report.status != "PASS":
        messages = "\n".join(
            f"{i.code} {i.path}: {i.message}" for i in report.issues
        )
        raise ValueError(f"Validation failed:\n{messages}")
    return report
