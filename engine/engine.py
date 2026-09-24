from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from engine.modules.analysis_years import aggregate_analysis_years
from engine.modules.capex import compute_capex
from engine.modules.cashflow import compute_cashflow
from engine.modules.debt import compute_debt, compute_preferred_equity, size_refi_loan
from engine.modules.fund_waterfall import compute_fund_waterfall
from engine.modules.metrics import compute_cash_on_cash, compute_exit_proceeds, compute_metrics
from engine.modules.opex import compute_opex
from engine.modules.renovations import compute_renovations
from engine.modules.revenue import compute_base_rent
from engine.modules.revenue_programs import compute_program_revenue
from engine.modules.time_grid import TimeGrid
from engine.modules.util import dec, month_id, round2
from engine.property_tax import apply_property_tax_policy
from engine.validator import validate_augmented_curves, validate_or_raise
from engine.version import ENGINE_VERSION, SCHEMA_VERSION


def _normalize_capital_stack(inputs: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Normalize debt inputs into a capital_stack[] array.

    If capital_stack is present, returns it sorted by priority.
    Otherwise wraps debt_terms + additional_debt_terms as senior layers.
    Returns empty list if no debt is configured.
    """
    if inputs.get("capital_stack"):
        return sorted(inputs["capital_stack"], key=lambda l: l.get("priority", 1))

    layers = []
    if inputs.get("debt_terms"):
        layer = dict(inputs["debt_terms"])
        layer.setdefault("layer_type", "senior")
        layer.setdefault("priority", 1)
        layer.setdefault("label", "Senior Debt")
        layers.append(layer)

    for i, extra in enumerate(inputs.get("additional_debt_terms", [])):
        layer = dict(extra)
        layer.setdefault("layer_type", "senior")
        layer.setdefault("priority", 1)
        layer.setdefault("label", f"Additional Debt {i + 1}")
        layers.append(layer)

    return sorted(layers, key=lambda l: l.get("priority", 1))


def _compute_layer_debt(
    time_grid: TimeGrid,
    layer: Dict[str, Any],
    draw_schedule: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Compute debt service for a single capital stack layer.

    Routes to compute_preferred_equity() for preferred_equity layers,
    compute_debt() for senior/mezzanine.
    """
    if layer.get("layer_type") == "preferred_equity":
        return compute_preferred_equity(time_grid, layer)
    return compute_debt(time_grid, layer, draw_schedule)


def _build_capital_stack_summary(
    stack_results: List[Dict[str, Any]],
    cashflow_result: Dict[str, Any],
) -> Dict[str, Any]:
    """Build capital stack summary with per-layer metrics and layered DSCR.

    Returns:
        Dict with layers[] (per-layer summary), senior_dscr, combined_dscr
    """
    layers_summary = []
    senior_ds_total = Decimal("0")
    all_ds_total = Decimal("0")

    for lr in stack_results:
        result = lr["result"]
        summary = result.get("summary", {})
        layer_ds = sum(dec(m.get("debt_service", 0)) for m in result["by_month"])
        layer_balance = dec(summary.get("total_commitment", 0))

        layers_summary.append({
            "layer_type": lr["layer_type"],
            "priority": lr["priority"],
            "label": lr["label"],
            "commitment": summary.get("total_commitment", 0),
            "rate": summary.get("weighted_avg_rate", 0),
            "total_debt_service": round2(layer_ds),
            "ending_balance": summary.get("final_balance", 0),
        })

        if lr["layer_type"] == "senior":
            senior_ds_total += layer_ds
        if lr["layer_type"] in ("senior", "mezzanine"):
            all_ds_total += layer_ds
        # Preferred equity DS is not included in DSCR (it's equity return, not debt)

    # Compute DSCRs from cashflow NOI
    total_noi = sum(
        dec(yr.get("net_operating_income", 0))
        for yr in cashflow_result.get("by_year", [])
    )

    senior_dscr = round2(total_noi / senior_ds_total) if senior_ds_total > 0 else None
    combined_dscr = round2(total_noi / all_ds_total) if all_ds_total > 0 else None

    return {
        "layers": layers_summary,
        "senior_dscr": senior_dscr,
        "combined_dscr": combined_dscr,
    }


def _utility_rule_matches_category(rule_category: str, opex_category: str) -> bool:
    """Return whether a recovery rule applies to an opex category label."""
    rule = rule_category.strip().lower().replace("_", " ").replace("-", " ")
    category = opex_category.strip().lower().replace("_", " ").replace("-", " ")
    if rule in {"all", "all recoverable opex", "all utilities", "utilities"}:
        return True
    return rule in category


def _compute_utility_recovery_by_month(
    time_grid: TimeGrid,
    utility_recovery_rules: Optional[List[Dict[str, Any]]],
    opex_by_category_by_month: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Calculate explicit monthly utility recovery from configured rules.

    Conservative default: absent rules recover $0, even if opex rows are marked
    recoverable. Multiple rules are applied by matching utility_category against
    the recoverable opex category label; unmatched recoverable rows recover $0.
    """
    if not utility_recovery_rules:
        return [{"month": m, "utility_recovery": 0.0} for m in time_grid.month_ids]

    by_month = []
    for month in time_grid.month_ids:
        recovery = Decimal("0")
        for row in opex_by_category_by_month:
            if row.get("month") != month or not row.get("recoverable"):
                continue
            category = str(row.get("category", ""))
            expense = dec(row.get("expense", 0))
            category_rate = Decimal("0")
            for rule in utility_recovery_rules:
                if _utility_rule_matches_category(str(rule.get("utility_category", "")), category):
                    category_rate = max(category_rate, dec(rule.get("recovery_rate", 0)))
            recovery += expense * category_rate
        by_month.append({"month": month, "utility_recovery": round2(recovery)})
    return by_month


def _build_renovation_context(
    time_grid: TimeGrid,
    inputs: Dict[str, Any],
    unit_renovations: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Dict[str, int]], List[Dict[str, Any]], List[Dict], List[Dict], List[Dict]]:
    """
    Compute renovations and build dynamic unit counts + output cohort data.

    Returns:
        (renovation_result, dynamic_unit_counts,
         output_cohort_entries, output_market_rent_curve,
         output_loss_to_lease, output_vacancy_curve)
    """
    renovation_result = compute_renovations(
        time_grid=time_grid,
        unit_cohorts=inputs["unit_cohorts"],
        renovation_programs=inputs.get("renovation_programs", []),
        unit_renovations=unit_renovations or [],
    )

    months = time_grid.month_ids

    # Initialize dynamic unit counts with static values for all original cohorts
    dynamic_unit_counts: Dict[str, Dict[str, int]] = {}
    for cohort in inputs["unit_cohorts"]:
        cid = cohort["cohort_id"]
        dynamic_unit_counts[cid] = {m: cohort["unit_count"] for m in months}

    # Map output_cohort -> list of programs that feed into it
    output_cohort_programs: Dict[str, List[Dict[str, Any]]] = {}
    for program in inputs["renovation_programs"]:
        oc = program["output_cohort"]
        if oc not in output_cohort_programs:
            output_cohort_programs[oc] = []
            dynamic_unit_counts[oc] = {m: 0 for m in months}
        output_cohort_programs[oc].append(program)

    # Track cumulative reductions per target cohort (sum across programs sharing target)
    # and cumulative additions per output cohort
    target_reductions: Dict[str, Dict[str, int]] = {}
    output_additions: Dict[str, Dict[str, int]] = {}

    for program in inputs["renovation_programs"]:
        tc = program["target_cohort"]
        oc = program["output_cohort"]
        if tc not in target_reductions:
            target_reductions[tc] = {m: 0 for m in months}
        if oc not in output_additions:
            output_additions[oc] = {m: 0 for m in months}

    # Process per-program-per-month data
    # Group by program_id for easy lookup
    program_lookup = {p["program_id"]: p for p in inputs["renovation_programs"]}

    for row in renovation_result["by_program_by_month"]:
        month = row["month"]
        pid = row["program_id"]
        cumulative = row["cumulative_units_renovated"]
        program = program_lookup[pid]
        tc = program["target_cohort"]
        oc = program["output_cohort"]

        # Accumulate reductions for target cohort (sum across all programs with same target)
        target_reductions[tc][month] += cumulative
        # Accumulate additions for output cohort (sum across all programs with same output)
        output_additions[oc][month] += cumulative

    # Apply reductions and additions to dynamic unit counts
    for tc, reductions in target_reductions.items():
        original_count = next(
            c["unit_count"] for c in inputs["unit_cohorts"] if c["cohort_id"] == tc
        )
        for month in months:
            dynamic_unit_counts[tc][month] = max(0, original_count - reductions[month])

    for oc, additions in output_additions.items():
        for month in months:
            dynamic_unit_counts[oc][month] = additions[month]

    # Build output cohort entries and curves
    output_cohort_entries: List[Dict[str, Any]] = []
    output_market_rent_curve: List[Dict[str, Any]] = []
    output_loss_to_lease: List[Dict[str, Any]] = []
    output_vacancy_curve: List[Dict[str, Any]] = []

    # Track which output cohorts we've already created entries for
    created_output_cohorts: set = set()

    for oc, programs in output_cohort_programs.items():
        if oc in created_output_cohorts:
            continue
        created_output_cohorts.add(oc)

        # Defense-in-depth: all programs sharing an output_cohort must share a
        # single target_cohort. Otherwise curve inheritance from
        # `representative_program = programs[0]` is ill-defined. Validator
        # should catch this in Wave 1a; this assert protects skip_validation paths.
        target_cohorts = {p["target_cohort"] for p in programs}
        if len(target_cohorts) > 1:
            raise ValueError(
                f"renovation_programs sharing output_cohort '{oc}' must share "
                f"target_cohort, got {target_cohorts}"
            )

        # Use the first program to determine target cohort for curve inheritance
        representative_program = programs[0]
        tc = representative_program["target_cohort"]
        premium = dec(representative_program["rent_premium_monthly"])

        # Find target cohort data
        target_cohort = next(
            c for c in inputs["unit_cohorts"] if c["cohort_id"] == tc
        )

        # Create unit_cohorts entry for output cohort. Preserve ALL fields from
        # the target cohort except those we intentionally re-derive
        # (cohort_id, unit_count, unit_type, initial_inplace_rent). This carries
        # bedrooms/bathrooms/sqft/etc. through to the renovated cohort so
        # downstream consumers (validator cross-checks, comp matching, memo) see
        # a fully-described cohort rather than a stripped one. unit_count=0 is
        # intentional — dynamic counts handle the actual units. Keep
        # initial_inplace_rent as Decimal; round2() at the JSON serialization
        # boundary handles float coercion.
        output_cohort_entries.append({
            **{
                k: v
                for k, v in target_cohort.items()
                if k not in ("cohort_id", "unit_count", "unit_type", "initial_inplace_rent")
            },
            "cohort_id": oc,
            "unit_type": target_cohort.get("unit_type", tc) + "_reno",
            "unit_count": 0,
            "initial_inplace_rent": dec(target_cohort["initial_inplace_rent"]) + premium,
        })

        # Generate market_rent_curve for output cohort.
        # Use the target cohort's calibrated market rent directly — the bridge
        # calibrates per-year market rents to CF Calculations Row 18, which
        # already embeds the renovation mix effect.  Applying post_renovation_
        # market_rent on top would double-inflate GPR.
        target_mr_segments = [
            r for r in inputs["market_rent_curve"] if r["cohort_id"] == tc
        ]

        for seg in target_mr_segments:
            output_market_rent_curve.append({
                "cohort_id": oc,
                "start_period": seg["start_period"],
                "end_period": seg["end_period"],
                "market_rent": seg["market_rent"],
            })

        # Copy loss_to_lease from target
        target_ltl_segments = [
            r for r in inputs["loss_to_lease"] if r["cohort_id"] == tc
        ]
        for seg in target_ltl_segments:
            output_loss_to_lease.append({
                "cohort_id": oc,
                "start_period": seg["start_period"],
                "end_period": seg["end_period"],
                "ltl_percent": seg["ltl_percent"],
            })

        # Copy physical_vacancy_curve from target
        target_vac_segments = [
            r for r in inputs["physical_vacancy_curve"] if r["cohort_id"] == tc
        ]
        for seg in target_vac_segments:
            output_vacancy_curve.append({
                "cohort_id": oc,
                "start_period": seg["start_period"],
                "end_period": seg["end_period"],
                "vacancy_rate": seg["vacancy_rate"],
            })

    # Handle unit overrides: adjust dynamic unit counts
    if unit_renovations:
        for ur in unit_renovations:
            tc = ur["cohort_id"]
            ur_month = month_id(ur["renovation_month"])

            # Find or synthesize output cohort
            matching_prog = next(
                (p for p in inputs.get("renovation_programs", []) if p["target_cohort"] == tc),
                None,
            )
            oc = matching_prog["output_cohort"] if matching_prog else f"{tc}_reno"

            # Ensure output cohort exists in dynamic counts
            if oc not in dynamic_unit_counts:
                dynamic_unit_counts[oc] = {m: 0 for m in months}

                # Create synthetic output cohort entry if not already created
                if oc not in created_output_cohorts:
                    created_output_cohorts.add(oc)
                    target_cohort_data = next(
                        (c for c in inputs["unit_cohorts"] if c["cohort_id"] == tc), None
                    )
                    if target_cohort_data:
                        # Preserve all metadata from target cohort (bedrooms,
                        # bathrooms, sqft, etc.); keep initial_inplace_rent as
                        # Decimal — round2() handles float coercion at the JSON
                        # serialization boundary.
                        output_cohort_entries.append({
                            **{
                                k: v
                                for k, v in target_cohort_data.items()
                                if k not in ("cohort_id", "unit_count", "unit_type", "initial_inplace_rent")
                            },
                            "cohort_id": oc,
                            "unit_type": target_cohort_data.get("unit_type", tc) + "_reno",
                            "unit_count": 0,
                            "initial_inplace_rent": dec(target_cohort_data["initial_inplace_rent"]) + dec(ur["expected_premium"]),
                        })

                        # Copy curves from target cohort
                        for seg in [r for r in inputs["market_rent_curve"] if r["cohort_id"] == tc]:
                            output_market_rent_curve.append({**seg, "cohort_id": oc})
                        for seg in [r for r in inputs["loss_to_lease"] if r["cohort_id"] == tc]:
                            output_loss_to_lease.append({**seg, "cohort_id": oc})
                        for seg in [r for r in inputs["physical_vacancy_curve"] if r["cohort_id"] == tc]:
                            output_vacancy_curve.append({**seg, "cohort_id": oc})

            # Adjust dynamic counts: -1 from target, +1 to output, starting at renovation month
            for m in months:
                if m >= ur_month:
                    dynamic_unit_counts[tc][m] = max(0, dynamic_unit_counts[tc][m] - 1)
                    dynamic_unit_counts[oc][m] = dynamic_unit_counts[oc].get(m, 0) + 1

    return (
        renovation_result,
        dynamic_unit_counts,
        output_cohort_entries,
        output_market_rent_curve,
        output_loss_to_lease,
        output_vacancy_curve,
    )


def run_underwriting(
    inputs: Dict[str, Any],
    *,
    skip_validation: bool = False,
    federation_mode: bool = False,
    deal_root: Optional[Path] = None,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the underwriting engine.

    Parameters
    ----------
    inputs:
        Canonical deal inputs dict (validated against deal_schema_v0_1.json).
    skip_validation:
        If True, bypasses pre-engine schema/cross-section validation. Allowed
        only for sweep/scenarios.py and portfolio.py callers that mutate a
        validated base. MUST NOT be combined with ``federation_mode=True``.
    federation_mode:
        If True (Wave 1b Task 1.5), enforces that validation runs and captures
        the validator report into ``result['_validator_report']`` for downstream
        provenance. Combining ``federation_mode=True`` with
        ``skip_validation=True`` is a contract violation and raises ValueError.
    deal_root:
        Optional federation deal root used by validator cross-checks that need
        on-disk sibling artifacts (for example market-study comp evidence).
        Only used when ``federation_mode=True``.
    run_id:
        Optional federation run id paired with ``deal_root`` for validator
        cross-checks against ``<deal_root>/outputs/<run_id>/...``. Only used
        when ``federation_mode=True``.

    Returns
    -------
    Dict[str, Any]
        Engine results. When ``federation_mode=True``, additionally contains a
        ``_validator_report`` key holding the dict-shape ValidationReport.
    """
    if federation_mode and skip_validation:
        # Federation runs MUST validate. Only sweep/scenarios.py is allowed to
        # bypass — and federation never calls scenarios.py directly.
        raise ValueError(
            "federation_mode=True is incompatible with skip_validation=True. "
            "Federation runs must validate canonical inputs before engine "
            "execution. If you need to bypass validation (sweep/scenarios), "
            "call run_underwriting without federation_mode."
        )

    inputs, property_tax_calculation = apply_property_tax_policy(inputs)

    validator_report_dict: Optional[Dict[str, Any]] = None
    if not skip_validation:
        if federation_mode:
            # Capture report on PASS so downstream provenance sees what was
            # checked. validate_or_raise still raises on FAIL.
            from engine.validator import validate_deal
            report = validate_deal(
                inputs,
                deal_root=deal_root,
                run_id=run_id,
                federation_mode=True,
            )
            if report.status != "PASS":
                messages = "\n".join(
                    f"{i.code} {i.path}: {i.message}" for i in report.issues
                )
                raise ValueError(f"Validation failed:\n{messages}")
            validator_report_dict = report.to_dict()
        else:
            validate_or_raise(inputs)
    time_grid = TimeGrid.build(inputs["time_grid"]["analysis_start_date"], inputs["time_grid"]["analysis_end_date"])

    # Renovation module - compute before revenue so dynamic unit counts are available
    renovation_result = None
    dynamic_unit_counts = None
    augmented_cohorts = list(inputs["unit_cohorts"])
    augmented_mr_curve = list(inputs["market_rent_curve"])
    augmented_ltl = list(inputs["loss_to_lease"])
    augmented_vac = list(inputs["physical_vacancy_curve"])

    unit_renovations = inputs.get("unit_renovations", [])
    if inputs.get("renovation_programs") or unit_renovations:
        (
            renovation_result,
            dynamic_unit_counts,
            output_cohort_entries,
            output_mr_curve,
            output_ltl,
            output_vac,
        ) = _build_renovation_context(time_grid, inputs, unit_renovations=unit_renovations)

        augmented_cohorts.extend(output_cohort_entries)
        augmented_mr_curve.extend(output_mr_curve)
        augmented_ltl.extend(output_ltl)
        augmented_vac.extend(output_vac)

        # Post-augmentation validation: catch cohort_id collisions or overlapping
        # segments introduced by _build_renovation_context (e.g., the cohort-collision bug
        # where output_cohort collides with an existing canonical cohort_id).
        if not skip_validation:
            augmented_report = validate_augmented_curves(
                augmented_mr_curve, augmented_ltl, augmented_vac
            )
            if augmented_report.status != "PASS":
                messages = "\n".join(
                    f"{i.code} {i.path}: {i.message}" for i in augmented_report.issues
                )
                raise ValueError(
                    f"Augmented-curve validation failed after renovation context build:\n{messages}"
                )

    # Revenue module - base rent
    base_rent = compute_base_rent(
        time_grid=time_grid,
        unit_cohorts=augmented_cohorts,
        market_rent_curve=augmented_mr_curve,
        loss_to_lease=augmented_ltl,
        physical_vacancy_curve=augmented_vac,
        collection_loss_curve=inputs["collection_loss_curve"],
        dynamic_unit_counts=dynamic_unit_counts,
        concession_schedule=inputs.get("concession_schedule"),
    )

    # Revenue module - programs
    programs = compute_program_revenue(
        time_grid=time_grid,
        unit_cohorts=augmented_cohorts,
        revenue_programs=inputs["revenue_programs"],
        program_adoption_curve=inputs["program_adoption_curve"],
        program_capacity=inputs.get("program_capacity"),
        collection_loss_curve=inputs["collection_loss_curve"],
        physical_vacancy_curve=inputs["physical_vacancy_curve"],
        market_rent_curve=augmented_mr_curve,
        loss_to_lease=augmented_ltl,
        dynamic_unit_counts=dynamic_unit_counts,
    )

    # Combine revenue totals
    revenue_totals = []
    by_month_rent = {row["month"]: row for row in base_rent["by_month"]}
    by_month_programs = {row["month"]: row for row in programs["by_month"]}
    for month in time_grid.month_ids:
        rent = by_month_rent[month]["net_rent"]
        prog = by_month_programs[month]["net_programs"]
        revenue_totals.append({
            "month": month,
            "net_rent": rent,
            "net_programs": prog,
            "net_total_revenue": round(rent + prog, 2),
        })

    # OpEx module
    opex_by_month = [{"month": m, "total_opex": 0, "recoverable_opex": 0} for m in time_grid.month_ids]
    opex_result = {"totals_by_month": opex_by_month, "totals_by_year": [], "by_category_by_month": []}
    if inputs.get("opex_table"):
        opex_result = compute_opex(
            time_grid=time_grid,
            unit_cohorts=inputs["unit_cohorts"],
            opex_table=inputs["opex_table"],
            revenue_by_month=revenue_totals,
        )
        opex_by_month = opex_result["totals_by_month"]

    # CapEx module
    capex_by_month = [{"month": m, "total_capex": 0} for m in time_grid.month_ids]
    capex_result = {"by_month": capex_by_month}
    if inputs.get("capex_schedule"):
        capex_result = compute_capex(
            time_grid=time_grid,
            unit_cohorts=inputs["unit_cohorts"],
            capex_schedule=inputs["capex_schedule"],
        )
        capex_by_month = capex_result["by_month"]

    # Add renovation costs to capex
    if renovation_result:
        capex_lookup = {row["month"]: row for row in capex_by_month}
        for reno_month in renovation_result["by_month"]:
            month = reno_month["month"]
            reno_cost = reno_month["renovation_cost"]
            if reno_cost and reno_cost > 0:
                capex_row = capex_lookup[month]
                capex_row["total_capex"] = round(float(dec(capex_row["total_capex"]) + dec(reno_cost)), 2)
                if "renovation_capex" in capex_row:
                    capex_row["renovation_capex"] = round(
                        float(dec(capex_row.get("renovation_capex", 0)) + dec(reno_cost)), 2
                    )

    # Offset capex by upfront-funded amounts (pre-funded at closing, doesn't reduce CF).
    # RedIQ treats upfront-funded CapEx (row 72) as equity funded at close; it does
    # NOT reduce operating cash flow.  We subtract the funded amount from the total
    # CapEx for the period — if funding >= total, zero out the period.
    capex_upfront = inputs.get("capex_upfront_funded", [])
    if capex_upfront:
        for entry in capex_upfront:
            start = month_id(entry["start_period"])
            end = month_id(entry["end_period"])
            funded = dec(entry["amount"])
            period_months = [m for m in capex_by_month if start <= m["month"] <= end]
            period_total = sum(dec(m["total_capex"]) for m in period_months)
            if period_total <= Decimal("0"):
                continue
            if funded >= period_total:
                # Upfront fully covers all CapEx — zero out
                for m in period_months:
                    m["total_capex"] = 0.0
            else:
                # Partial offset — reduce proportionally
                keep_ratio = (period_total - funded) / period_total
                for m in period_months:
                    m["total_capex"] = round2(dec(m["total_capex"]) * keep_ratio)

    # Replacement Reserves — spread annual amounts evenly across months
    reserves_by_month: List[Dict[str, Any]] = []
    reserve_segments = inputs.get("replacement_reserves", [])
    if reserve_segments:
        reserve_lookup: Dict[str, Decimal] = {}
        for seg in reserve_segments:
            seg_start = month_id(seg["start_period"])
            seg_end = month_id(seg["end_period"])
            annual = dec(seg["annual_amount"])
            monthly = annual / Decimal("12")
            for m in time_grid.month_ids:
                if seg_start <= m <= seg_end:
                    reserve_lookup[m] = reserve_lookup.get(m, Decimal("0")) + monthly
        for m in time_grid.month_ids:
            reserves_by_month.append({
                "month": m,
                "replacement_reserves": round2(reserve_lookup.get(m, Decimal("0"))),
            })

    # Snapshot raw CapEx before debt-draw offset for unlevered CF correction.
    raw_capex_lookup = {m["month"]: float(m["total_capex"]) for m in capex_by_month}

    # Offset CapEx funded by loan drawdowns (construction/rehab loans).
    # When subsequent draws exist (beyond Day 0), they fund CapEx — the CapEx
    # should not reduce operating cash flow since it's financed by the loan.
    # We offset at the period level: total draws in a period offset total CapEx.
    debt_draw_schedule = inputs.get("debt_draw_schedule")
    if debt_draw_schedule and len(debt_draw_schedule) > 1:
        # Sum subsequent draws (excluding Day 0) by month
        draw_by_month: Dict[str, Decimal] = {}
        for draw in debt_draw_schedule[1:]:
            m = month_id(draw["month"])
            draw_by_month[m] = draw_by_month.get(m, Decimal("0")) + dec(draw["draw_amount"])

        # For each month with draws, offset CapEx proportionally
        # If draws exceed CapEx in a month, carry over to adjacent months
        # Simple approach: reduce total CapEx in proportion across each year
        from itertools import groupby
        from operator import itemgetter

        # Group months by analysis year
        start_year = int(inputs["time_grid"]["analysis_start_date"][:4])
        start_mo = int(inputs["time_grid"]["analysis_start_date"][5:7])

        # Build year boundaries
        year_months_map: Dict[int, List[Dict]] = {}
        for cm in capex_by_month:
            m_year = int(cm["month"][:4])
            m_month = int(cm["month"][5:7])
            months_from_start = (m_year - start_year) * 12 + (m_month - start_mo)
            ay = months_from_start // 12 + 1
            if ay not in year_months_map:
                year_months_map[ay] = []
            year_months_map[ay].append(cm)

        for ay, yr_capex_months in year_months_map.items():
            yr_draws = sum(draw_by_month.get(m["month"], Decimal("0")) for m in yr_capex_months)
            if yr_draws <= 0:
                continue
            yr_capex = sum(dec(m["total_capex"]) for m in yr_capex_months)
            if yr_capex <= 0:
                continue
            if yr_draws >= yr_capex:
                for m in yr_capex_months:
                    m["total_capex"] = 0.0
            else:
                keep_ratio = (yr_capex - yr_draws) / yr_capex
                for m in yr_capex_months:
                    m["total_capex"] = round2(dec(m["total_capex"]) * keep_ratio)

    # Compute debt-funded CapEx per month (raw minus post-offset).
    # This amount was zeroed from LCF (correct — financed by draw), but must be
    # restored for UCF since the unlevered view has no loan draws.
    debt_funded_capex: Dict[str, float] = {}
    for m in capex_by_month:
        raw = raw_capex_lookup.get(m["month"], 0.0)
        final = float(m["total_capex"])
        if raw > final + 0.01:
            debt_funded_capex[m["month"]] = round(raw - final, 2)

    # Debt module — support multiple loans (primary + additional) or capital_stack[]
    debt_by_month = [{"month": m, "debt_service": 0, "loan_payoff": 0, "ending_balance": 0} for m in time_grid.month_ids]
    debt_result = {"by_month": debt_by_month}
    capital_stack_results = []  # Per-layer results for reporting

    if inputs.get("capital_stack"):
        # Capital stack mode: process each layer independently, aggregate
        stack = _normalize_capital_stack(inputs)
        is_first = True
        for layer in stack:
            # Only the primary senior layer gets the draw schedule
            draw_sched = inputs.get("debt_draw_schedule") if is_first and layer.get("layer_type") == "senior" else None
            layer_result = _compute_layer_debt(time_grid, layer, draw_sched)
            capital_stack_results.append({
                "layer_type": layer.get("layer_type", "senior"),
                "priority": layer.get("priority", 1),
                "label": layer.get("label", layer.get("layer_type", "senior").replace("_", " ").title()),
                "result": layer_result,
            })

            if is_first:
                debt_result = layer_result
                debt_by_month = [dict(m) for m in layer_result["by_month"]]
                is_first = False
            else:
                # Aggregate into combined debt_by_month
                for i, extra_m in enumerate(layer_result["by_month"]):
                    debt_by_month[i]["debt_service"] = round2(
                        dec(debt_by_month[i]["debt_service"]) + dec(extra_m["debt_service"])
                    )
                    debt_by_month[i]["loan_payoff"] = round2(
                        dec(debt_by_month[i].get("loan_payoff", 0)) + dec(extra_m.get("loan_payoff", 0))
                    )
                    debt_by_month[i]["draw_amount"] = round2(
                        dec(debt_by_month[i].get("draw_amount", 0)) + dec(extra_m.get("draw_amount", 0))
                    )
                    debt_by_month[i]["ending_balance"] = round2(
                        dec(debt_by_month[i].get("ending_balance", 0)) + dec(extra_m.get("ending_balance", 0))
                    )
                    extra_draw = dec(extra_m.get("draw_amount", 0))
                    if extra_draw > 0 and debt_by_month[i]["month"] != time_grid.month_ids[0]:
                        debt_by_month[i]["financing_draw"] = round2(
                            dec(debt_by_month[i].get("financing_draw", 0)) + extra_draw
                        )

                # Inject closing costs
                extra_cc = dec(layer.get("loan_closing_costs", 0))
                extra_start = layer.get("loan_start_month")
                if extra_cc > 0 and extra_start:
                    extra_start_mid = month_id(extra_start)
                    for row in debt_by_month:
                        if row["month"] == extra_start_mid:
                            row["loan_closing_costs"] = round2(
                                dec(row.get("loan_closing_costs", 0)) + extra_cc
                            )

    elif inputs.get("debt_terms"):
        debt_result = compute_debt(
            time_grid=time_grid,
            debt_terms=inputs["debt_terms"],
            debt_draw_schedule=inputs.get("debt_draw_schedule"),
        )
        debt_by_month = debt_result["by_month"]

        # Additional loans (e.g., assumable + originated running concurrently)
        for extra_terms in inputs.get("additional_debt_terms", []):
            extra_result = compute_debt(
                time_grid=time_grid,
                debt_terms=extra_terms,
            )
            # Sum debt service, loan_payoff, draw_amount, and ending_balance
            for i, extra_m in enumerate(extra_result["by_month"]):
                debt_by_month[i]["debt_service"] = round2(
                    dec(debt_by_month[i]["debt_service"]) + dec(extra_m["debt_service"])
                )
                debt_by_month[i]["loan_payoff"] = round2(
                    dec(debt_by_month[i].get("loan_payoff", 0)) + dec(extra_m.get("loan_payoff", 0))
                )
                debt_by_month[i]["draw_amount"] = round2(
                    dec(debt_by_month[i].get("draw_amount", 0)) + dec(extra_m.get("draw_amount", 0))
                )
                debt_by_month[i]["ending_balance"] = round2(
                    dec(debt_by_month[i].get("ending_balance", 0)) + dec(extra_m.get("ending_balance", 0))
                )
                # Additional loan draws that occur AFTER Day 0 are mid-hold
                # financing cash flows (e.g., refi draws).  Day 0 draws from
                # additional loans are part of the initial equity calculation
                # and must NOT inflate operating LCF.
                extra_draw = dec(extra_m.get("draw_amount", 0))
                if extra_draw > 0 and debt_by_month[i]["month"] != time_grid.month_ids[0]:
                    debt_by_month[i]["financing_draw"] = round2(
                        dec(debt_by_month[i].get("financing_draw", 0)) + extra_draw
                    )

            # Inject refi loan closing costs at the additional loan's start month
            extra_cc = dec(extra_terms.get("loan_closing_costs", 0))
            extra_start = extra_terms.get("loan_start_month")
            if extra_cc > 0 and extra_start:
                extra_start_mid = month_id(extra_start)
                for row in debt_by_month:
                    if row["month"] == extra_start_mid:
                        row["loan_closing_costs"] = round2(
                            dec(row.get("loan_closing_costs", 0)) + extra_cc
                        )

    # Utility recovery from utility_recovery_rules. Absent rules recover $0;
    # multiple configured rules are applied by category instead of silently
    # using only the first rule for every recoverable expense.
    utility_recovery_by_month = _compute_utility_recovery_by_month(
        time_grid=time_grid,
        utility_recovery_rules=inputs.get("utility_recovery_rules"),
        opex_by_category_by_month=opex_result.get("by_category_by_month", []),
    )

    # Cashflow aggregation
    cashflow_result = compute_cashflow(
        time_grid=time_grid,
        revenue_by_month=revenue_totals,
        opex_by_month=opex_by_month,
        capex_by_month=capex_by_month,
        debt_by_month=debt_by_month,
        reserves_by_month=reserves_by_month if reserves_by_month else None,
        utility_recovery_by_month=utility_recovery_by_month,
    )

    # Dynamic refi event: size perm loan from stabilized NOI, then re-run
    # debt + cashflow with the perm loan added as additional debt.
    refi_result = None
    if inputs.get("refi_event") and inputs.get("debt_terms"):
        refi_result = size_refi_loan(
            refi_event=inputs["refi_event"],
            cashflow_by_month=cashflow_result["by_month"],
            debt_by_month=debt_by_month,
            exit_assumptions=inputs.get("exit_assumptions"),
        )

        # Set bridge loan to terminate at refi month
        trigger_month = refi_result["trigger_month"]
        bridge_terms = inputs["debt_terms"]
        bridge_start = month_id(bridge_terms.get("loan_start_month", time_grid.month_ids[0]))
        try:
            bridge_start_idx = time_grid.month_ids.index(bridge_start)
        except ValueError:
            bridge_start_idx = 0
        try:
            trigger_idx = time_grid.month_ids.index(trigger_month)
        except ValueError:
            trigger_idx = len(time_grid.month_ids) - 1
        bridge_term = trigger_idx - bridge_start_idx + 1
        if bridge_term > 0 and not bridge_terms.get("term_months"):
            bridge_terms = dict(bridge_terms)
            bridge_terms["term_months"] = bridge_term
            bridge_terms["skip_maturity_ds"] = True

        # Re-compute primary debt with bridge termination
        debt_result = compute_debt(
            time_grid=time_grid,
            debt_terms=bridge_terms,
            debt_draw_schedule=inputs.get("debt_draw_schedule"),
        )
        debt_by_month = debt_result["by_month"]

        # Add perm loan as additional debt
        perm_terms = refi_result["perm_loan_terms"]
        extra_result = compute_debt(
            time_grid=time_grid,
            debt_terms=perm_terms,
        )
        for i, extra_m in enumerate(extra_result["by_month"]):
            debt_by_month[i]["debt_service"] = round2(
                dec(debt_by_month[i]["debt_service"]) + dec(extra_m["debt_service"])
            )
            debt_by_month[i]["loan_payoff"] = round2(
                dec(debt_by_month[i].get("loan_payoff", 0)) + dec(extra_m.get("loan_payoff", 0))
            )
            debt_by_month[i]["draw_amount"] = round2(
                dec(debt_by_month[i].get("draw_amount", 0)) + dec(extra_m.get("draw_amount", 0))
            )
            debt_by_month[i]["ending_balance"] = round2(
                dec(debt_by_month[i].get("ending_balance", 0)) + dec(extra_m.get("ending_balance", 0))
            )
            # Perm loan draw is a financing event (mid-hold)
            extra_draw = dec(extra_m.get("draw_amount", 0))
            if extra_draw > 0 and debt_by_month[i]["month"] != time_grid.month_ids[0]:
                debt_by_month[i]["financing_draw"] = round2(
                    dec(debt_by_month[i].get("financing_draw", 0)) + extra_draw
                )
        # Inject refi closing costs
        refi_costs = dec(perm_terms.get("loan_closing_costs", 0))
        if refi_costs > 0:
            for row in debt_by_month:
                if row["month"] == trigger_month:
                    row["loan_closing_costs"] = round2(
                        dec(row.get("loan_closing_costs", 0)) + refi_costs
                    )

        # Re-compute cashflow with updated debt
        cashflow_result = compute_cashflow(
            time_grid=time_grid,
            revenue_by_month=revenue_totals,
            opex_by_month=opex_by_month,
            capex_by_month=capex_by_month,
            debt_by_month=debt_by_month,
            reserves_by_month=reserves_by_month if reserves_by_month else None,
            utility_recovery_by_month=utility_recovery_by_month,
        )

    # Attach debt-funded CapEx to cashflow months so metrics can correct UCF
    if debt_funded_capex:
        for cf_m in cashflow_result["by_month"]:
            dfc = debt_funded_capex.get(cf_m["month"], 0.0)
            if dfc > 0:
                cf_m["debt_funded_capex"] = dfc

    # Inject exit proceeds into cashflow (before metrics and fund waterfall)
    exit_result = compute_exit_proceeds(
        time_grid=time_grid,
        cashflow_by_year=cashflow_result["by_year"],
        debt_by_month=debt_by_month,
        purchase_assumptions=inputs.get("purchase_assumptions"),
        exit_assumptions=inputs.get("exit_assumptions"),
        cashflow_by_month=cashflow_result["by_month"],
    )
    if exit_result:
        gross_sale = dec(exit_result["gross_sale_price"])
        sale_costs_val = dec(exit_result["sale_costs"])
        loan_payoff_val = dec(exit_result["loan_payoff"])
        net_sale = dec(exit_result["net_sale_proceeds"])
        exit_month = exit_result["exit_month"]
        exit_year = exit_month[:4]

        # Update exit year in by_year
        for yr_data in cashflow_result["by_year"]:
            if yr_data["year"] == exit_year:
                yr_data["gross_sale_price"] = round2(gross_sale)
                yr_data["sale_costs"] = round2(sale_costs_val)
                yr_data["loan_payoff"] = round2(loan_payoff_val)
                yr_data["net_sale_proceeds"] = round2(gross_sale - sale_costs_val)
                yr_data["unleveraged_cash_flow"] = round2(
                    dec(yr_data["unleveraged_cash_flow"]) + gross_sale - sale_costs_val
                )
                yr_data["leveraged_cash_flow"] = round2(
                    dec(yr_data["leveraged_cash_flow"]) + net_sale
                )
                break

        # Update exit month in by_month
        for m_data in cashflow_result["by_month"]:
            if m_data["month"] == exit_month:
                m_data["gross_sale_price"] = round2(gross_sale)
                m_data["sale_costs"] = round2(sale_costs_val)
                m_data["loan_payoff"] = round2(loan_payoff_val)
                m_data["net_sale_proceeds"] = round2(gross_sale - sale_costs_val)
                m_data["unleveraged_cash_flow"] = round2(
                    dec(m_data["unleveraged_cash_flow"]) + gross_sale - sale_costs_val
                )
                m_data["leveraged_cash_flow"] = round2(
                    dec(m_data["leveraged_cash_flow"]) + net_sale
                )
                break

    # Metrics calculation
    metrics_result = compute_metrics(
        time_grid=time_grid,
        cashflow_by_month=cashflow_result["by_month"],
        cashflow_by_year=cashflow_result["by_year"],
        debt_by_month=debt_by_month,
        purchase_assumptions=inputs.get("purchase_assumptions"),
        exit_assumptions=inputs.get("exit_assumptions"),
    )

    result = {
        "schema_version": SCHEMA_VERSION,
        "engine_version": ENGINE_VERSION,
        "time_grid": {
            "analysis_start": inputs["time_grid"]["analysis_start_date"],
            "analysis_end": inputs["time_grid"]["analysis_end_date"],
            "months": time_grid.month_ids,
            "years": time_grid.year_ids,
        },
        "revenue": {
            "base_rent": base_rent,
            "programs": programs,
            "totals_by_month": revenue_totals,
        },
        "opex": opex_result,
        "capex": capex_result,
        "debt": debt_result,
        "cashflow": cashflow_result,
        "metrics": metrics_result,
    }
    result["property_tax_calculation"] = property_tax_calculation.to_json()

    metadata = inputs.get("metadata", {}) or {}
    address = metadata.get("address")
    if isinstance(address, dict):
        city = str(address.get("city") or "").strip()
        state = str(address.get("state") or "").strip()
        location = f"{city}, {state}" if city and state else str(address.get("street") or "").strip() or None
    elif isinstance(address, str) and address.strip():
        parts = [part.strip() for part in address.split(",") if part.strip()]
        location = ", ".join(parts[-2:]) if len(parts) >= 2 else address.strip()
    else:
        market = metadata.get("market")
        location = str(market).strip() if market not in (None, "") else None

    purchase_assumptions = inputs.get("purchase_assumptions") or {}
    total_units = sum(int(c.get("unit_count", 0) or 0) for c in inputs.get("unit_cohorts", []))
    purchase_price = purchase_assumptions.get("purchase_price")
    price_per_unit = (
        float(purchase_price) / float(total_units)
        if purchase_price not in (None, "") and total_units
        else None
    )
    result["property_snapshot"] = {
        "year_built": metadata.get("year_built"),
        "location": location,
        "price_per_unit": price_per_unit,
        "units": total_units,
    }

    # Trade-out tracking (informational metric for AM comparison)
    if inputs.get("trade_out_assumptions"):
        ta = inputs["trade_out_assumptions"]
        annual_turnover = dec(ta["annual_turnover_pct"])
        trade_out_pct = dec(ta["expected_trade_out_pct"])
        monthly_turnover = annual_turnover / Decimal("12")
        trade_out_by_month = []
        rent_lookup = {r["month"]: r for r in base_rent["by_month"]}
        for month in time_grid.month_ids:
            r = rent_lookup[month]
            # Use total occupied units derived from billed/inplace
            inplace = dec(r["inplace_rent"])
            market = dec(r["market_rent"])
            billed = dec(r["billed_rent_after_vacancy"])
            # occupied_units approximation: billed / inplace_per_unit (portfolio level)
            # Simpler: use market_rent total and turnover pct
            if inplace > 0:
                # weighted avg inplace per unit = inplace_total / total_units (approx)
                # occupied units ≈ billed / (inplace / total_units) but we don't have total_units here
                # Use the ratio: turning_rent = billed * monthly_turnover
                expiring_rent = billed * monthly_turnover
                new_lease_rent = expiring_rent * (Decimal("1") + trade_out_pct)
                spread = new_lease_rent - expiring_rent
            else:
                expiring_rent = Decimal("0")
                new_lease_rent = Decimal("0")
                spread = Decimal("0")
            trade_out_by_month.append({
                "month": month,
                "expiring_rent": round2(expiring_rent),
                "new_lease_rent": round2(new_lease_rent),
                "trade_out_spread": round2(spread),
            })
        result["trade_out"] = {"by_month": trade_out_by_month}

    if renovation_result:
        result["renovations"] = renovation_result

    # Pass renovation_detail sidecar through untouched
    if inputs.get("renovation_detail"):
        result["renovation_detail"] = inputs["renovation_detail"]

    if refi_result:
        result["refi"] = refi_result

    if capital_stack_results:
        result["capital_stack"] = _build_capital_stack_summary(capital_stack_results, cashflow_result)

    # Fund-level waterfall (optional)
    if inputs.get("fund_assumptions") and inputs.get("purchase_assumptions") and inputs.get("exit_assumptions"):
        # Build analysis-year cashflow for the fund waterfall.
        # This ensures correct year boundaries for non-January starts.
        analysis_cf_by_year = aggregate_analysis_years(
            cashflow_result["by_month"],
            inputs["time_grid"]["analysis_start_date"],
        )
        fund_result = compute_fund_waterfall(
            time_grid=time_grid,
            cashflow_by_year=analysis_cf_by_year,
            purchase_assumptions=inputs["purchase_assumptions"],
            exit_assumptions=inputs["exit_assumptions"],
            debt_by_month=debt_by_month,
            fund_assumptions=inputs["fund_assumptions"],
            cashflow_by_month=cashflow_result["by_month"],
        )
        result["fund_waterfall"] = fund_result

        # V1.5 patch: re-compute Year-1 CoC now that AM fees + partnership
        # expenses are known. compute_metrics ran with fund_by_year=None
        # (chicken/egg), producing a CoC missing those two deductions; we
        # overwrite with the fund-aware version here so deal_summary and
        # judgment see the authoritative value.
        coc_v15 = compute_cash_on_cash(
            cashflow_by_year=cashflow_result["by_year"],
            equity_basis=(inputs.get("purchase_assumptions") or {}).get(
                "total_equity_basis",
                (inputs.get("purchase_assumptions") or {}).get("equity_contribution", 0),
            ),
            fund_by_year=fund_result.get("by_year") if fund_result else None,
            cashflow_by_month=cashflow_result["by_month"],
            analysis_start_date=inputs["time_grid"]["analysis_start_date"],
        )
        if "metrics" in result and isinstance(result["metrics"], dict):
            cash_block = result["metrics"].setdefault("cash_on_cash", {})
            cash_block["cash_on_cash_year_1"] = coc_v15["cash_on_cash_year_1"]
            cash_block["cash_on_cash_year_1_exact"] = coc_v15["cash_on_cash_year_1_exact"]
            cash_block["free_cf_year_1"] = coc_v15["free_cf_year_1"]
            cash_block["components"] = coc_v15["components"]
            cash_block["target_cash_on_cash_pct"] = coc_v15["target_cash_on_cash_pct"]
            cash_block["coc_below_target"] = coc_v15["coc_below_target"]
            cash_block["coc_below_target_7pct"] = coc_v15["coc_below_target_7pct"]
            cash_block["coc_below_target_6pct"] = coc_v15["coc_below_target_6pct"]
            result["metrics"]["coc"] = {
                "cash_on_cash_year_1": coc_v15["cash_on_cash_year_1"],
                "cash_on_cash_year_1_exact": coc_v15["cash_on_cash_year_1_exact"],
                "free_cf_year_1": coc_v15["free_cf_year_1"],
                "components": coc_v15["components"],
                "target_cash_on_cash_pct": coc_v15["target_cash_on_cash_pct"],
                "coc_below_target": coc_v15["coc_below_target"],
                "coc_below_target_7pct": coc_v15["coc_below_target_7pct"],
                "coc_below_target_6pct": coc_v15["coc_below_target_6pct"],
            }

    # Wave 1b Task 1.5: stash validator report so federation callers can embed
    # it in provenance. Only attached when federation_mode=True ran a validator;
    # legacy non-federation callers see no shape change (key absent).
    if validator_report_dict is not None:
        result["_validator_report"] = validator_report_dict

    # Wave 4 Task 4.1: embed deterministic feasibility classification directly
    # alongside metrics so downstream consumers (deal_summary.json, runner) can
    # read result['feasibility'] without re-running the classifier or going
    # through the api.py wrapper. Mirrors the verdict/flags/reasons that
    # engine.api.build_provenance emits, sourced from the same classify() call.
    try:
        from engine.feasibility import classify as _classify_feasibility
        _fres = _classify_feasibility(result.get("metrics", {}) or {})
        result["feasibility"] = {
            "verdict": _fres.verdict,
            "sanity_flags": list(_fres.sanity_flags),
            "reasons": list(_fres.reasons),
        }
    except (KeyError, TypeError, ValueError) as exc:
        # Narrow except: only the expected error classes from a malformed
        # metrics block are swallowed. A genuine classifier bug (AttributeError,
        # ImportError, etc.) propagates so it surfaces in CI rather than being
        # silently masked. Downstream provenance still has the canonical copy
        # via engine.api.build_provenance.
        import logging
        logging.getLogger(__name__).warning(
            "feasibility.classify failed on engine result; result['feasibility'] omitted: %s",
            exc,
        )

    return result


def run_from_files(inputs_path: Path, outputs_path: Path, validation_path: Path) -> None:
    inputs = json.loads(inputs_path.read_text(encoding="utf-8"))
    from engine.validator import validate_deal

    report = validate_deal(inputs)
    validation_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    if report.status != "PASS":
        raise SystemExit("Validation failed. See validation report.")

    outputs = run_underwriting(inputs)
    outputs_path.write_text(json.dumps(outputs, indent=2), encoding="utf-8")
