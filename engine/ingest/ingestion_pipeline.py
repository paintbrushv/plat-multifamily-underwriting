"""Ingestion pipeline: raw documents → canonical deal JSON.

Combines T12 + rent roll into a valid canonical deal schema.
Optional CLI flags populate debt_terms, purchase_assumptions,
exit_assumptions, and fund_assumptions inline.

Output is compatible with canonical schema v0.1 and can be passed directly
to run_underwriting() or generate_rediq_workbook.py --from-json.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

from engine.ingest.broker_om_snapshot import parse_broker_snapshot_from_om
from engine.ingest.om_parser import _normalize_year_built, parse_om  # noqa: E402 — lazy-safe, no heavy imports
from engine.ingest.property_tax_migration import (
    migrate_legacy_property_tax_policy,
)
from engine.property_tax import build_property_tax_policy
from engine.assumptions import DealProfile, resolve_assumptions
from engine.assumptions.canonical_ops import (
    annual_opex_amount,
    matching_opex_rows,
    opex_bucket_for_policy,
    sum_opex_bucket,
)
from engine.assumptions.opex import t12_opex_line_evidence
from engine.assumptions.profile import check_deal_type


_KNOWN_PROPERTY_TAX_PRODUCERS = frozenset(
    {
        "plat_property_tax_context",
        "engine_backsolve_policy",
        "broker_snapshot_mills",
        "broker_snapshot_percentage_points",
    }
)


def _preserved_unresolved_candidate(
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    for field in (
        "tax_rate",
        "tax_rate_pct",
        "property_tax_rate_pct",
        "property_tax_millage_rate",
    ):
        if field in candidate:
            return {
                "producer": candidate.get("producer", "unknown"),
                "field": field,
                "value": candidate[field],
                "source": candidate.get("source"),
                "source_locator": candidate.get("source_locator"),
            }
    return deepcopy(dict(candidate))


def _effective_provenance_parts(
    evidence_records: Sequence[Mapping[str, Any]],
) -> list[str]:
    parts: set[str] = set()
    for evidence in evidence_records:
        source = str(evidence["source"])
        source_locator = str(evidence["source_locator"])
        if source == "composite_evidence":
            parts.update(
                part.strip()
                for part in source_locator.split(";")
                if part.strip()
            )
        else:
            parts.add(f"{source}:{source_locator}")
    return sorted(parts)


def promote_property_tax_evidence(
    property_summary: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], str | None]:
    """Promote one unambiguous labelled millage value; preserve conflicts."""
    promoted_summary = deepcopy(dict(property_summary))
    normalized: list[tuple[dict[str, Any], dict[str, Any]]] = []
    unresolved: list[dict[str, Any]] = []

    for candidate in candidates:
        producer = candidate.get("producer")
        source = candidate.get("source")
        source_locator = candidate.get("source_locator")
        if (
            producer not in _KNOWN_PROPERTY_TAX_PRODUCERS
            or not isinstance(source, str)
            or not source.strip()
            or not isinstance(source_locator, str)
            or not source_locator.strip()
        ):
            unresolved.append(_preserved_unresolved_candidate(candidate))
            continue
        result = migrate_legacy_property_tax_policy(
            candidate,
            producer=producer,
            source=source,
            source_locator=source_locator,
        )
        if result.policy is None:
            unresolved.extend(deepcopy(list(result.evidence_candidates)))
            continue
        normalized.append((result.policy, result.evidence_candidates[0]))

    if unresolved:
        promoted_summary.pop("property_tax_policy", None)
        promoted_summary["property_tax_evidence_candidates"] = unresolved + [
            evidence for _policy, evidence in normalized
        ]
        return promoted_summary, "missing_property_tax_millage"
    if not normalized:
        return promoted_summary, "missing_property_tax_millage"

    by_mills: dict[Decimal, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for policy, evidence in normalized:
        key = Decimal(str(policy["millage_rate_mills"]))
        by_mills.setdefault(key, []).append((policy, evidence))

    if len(by_mills) != 1:
        promoted_summary.pop("property_tax_policy", None)
        promoted_summary["property_tax_evidence_candidates"] = sorted(
            (evidence for _policy, evidence in normalized),
            key=lambda evidence: (
                Decimal(str(evidence["millage_rate_mills"])),
                evidence["source"],
                evidence["source_locator"],
            ),
        )
        return promoted_summary, "conflicting_property_tax_millage"

    matches = next(iter(by_mills.values()))
    policy_shapes = {
        (
            Decimal(str(policy["assessment_ratio"])),
            policy["analyst_override"],
        )
        for policy, _evidence in matches
    }
    if len(policy_shapes) != 1:
        promoted_summary.pop("property_tax_policy", None)
        promoted_summary["property_tax_evidence_candidates"] = [
            evidence for _policy, evidence in matches
        ]
        return promoted_summary, "conflicting_property_tax_assessment_ratio"

    first_policy = matches[0][0]
    evidence_records = [evidence for _policy, evidence in matches]
    if len(evidence_records) == 1:
        policy = first_policy
    else:
        locators = _effective_provenance_parts(evidence_records)
        policy = build_property_tax_policy(
            millage_rate=first_policy["millage_rate_mills"],
            unit="mills",
            source="composite_evidence",
            source_locator="; ".join(locators),
            assessment_ratio=first_policy["assessment_ratio"],
            analyst_override=first_policy["analyst_override"],
        )
    promoted_summary["property_tax_policy"] = policy
    promoted_summary.pop("property_tax_evidence_candidates", None)
    return promoted_summary, None


# Sentinel: ``build_deal_from_documents(market=...)`` uses the default registry.
DEFAULT_MARKET: Any = object()

ANALYST_PROPERTY_TAX_SOURCE = "analyst"
DEFAULT_PROPERTY_TAX_SOURCE_LOCATOR = "analyst_input:property_tax_millage"
# OM intake flags an analyst millage settles.
_PROPERTY_TAX_INTAKE_FLAGS = frozenset(
    {
        "missing_property_tax_millage",
        "conflicting_property_tax_millage",
        "conflicting_property_tax_assessment_ratio",
    }
)


def analyst_property_tax_policy(
    millage_mills: object,
    assessment_ratio: object = None,
    *,
    source_locator: str = DEFAULT_PROPERTY_TAX_SOURCE_LOCATOR,
) -> dict[str, Any]:
    """``metadata.property_summary.property_tax_policy`` from an analyst millage.

    ``millage_mills`` is the combined millage in mills per $1,000 of assessed
    value. ``assessment_ratio`` defaults to 1.00; any other ratio is recorded
    as an analyst override, as ``engine.property_tax`` requires. Raises
    ``PropertyTaxPolicyError`` (a ``ValueError``) for an unusable value.
    """
    ratio = Decimal("1.00") if assessment_ratio is None else assessment_ratio
    try:
        is_override = Decimal(str(ratio).strip()) != Decimal("1.00")
    except (InvalidOperation, ValueError):
        is_override = False  # build_property_tax_policy reports the bad ratio
    return build_property_tax_policy(
        millage_rate=millage_mills,
        unit="mills",
        source=ANALYST_PROPERTY_TAX_SOURCE,
        source_locator=source_locator,
        assessment_ratio=ratio,
        analyst_override=is_override,
    )


def apply_analyst_property_tax_policy(deal: dict[str, Any], policy: Mapping[str, Any]) -> dict[str, Any]:
    """Set an analyst property-tax policy on ``deal`` (mutates and returns it).

    The analyst value settles any OM tax evidence: the OM candidates stay in
    ``property_summary`` as evidence and the OM millage intake flags are
    dropped.
    """
    metadata = deal.setdefault("metadata", {})
    summary = metadata.get("property_summary")
    if not isinstance(summary, dict):
        summary = {}
        metadata["property_summary"] = summary
    summary["property_tax_policy"] = dict(policy)
    flags = [
        flag for flag in metadata.get("intake_sanity_flags") or []
        if flag not in _PROPERTY_TAX_INTAKE_FLAGS
    ]
    if flags:
        metadata["intake_sanity_flags"] = flags
    else:
        metadata.pop("intake_sanity_flags", None)
    return deal


def build_deal_from_documents(
    property_id: str,
    rent_roll_path: str | Path,
    t12_path: str | Path,
    analysis_start: str,
    analysis_end: str,
    rent_growth_rate: float | None = None,
    vacancy_rate_override: float | None = None,
    collection_loss_rate: float | None = None,
    analyst: str = "",
    om_path: str | Path | None = None,
    om_source_locator: str | None = None,
    anthropic_client: Any | None = None,
    # Deal economics (optional — populate for engine-runnable JSON)
    purchase_price: float | None = None,
    ltv: float | None = None,
    loan_rate: float | None = None,
    amort_years: int | None = None,
    io_months: int = 0,
    term_months: int | None = None,
    exit_cap_rate: float | None = None,
    exit_month: str | None = None,
    sale_cost_pct: float | None = None,
    # Fund structure (optional)
    sponsor_equity_pct: float | None = None,
    pref_return: float | None = None,
    acq_fee_pct: float | None = None,
    am_fee_pct: float | None = None,
    disposition_fee_pct: float | None = None,
    scan_sidecar_workbooks: bool = False,
    year_built: int | None = None,
    # Assumption layer (ADR 0003)
    deal_type: str = "stabilized",
    msa: str | Mapping[str, Any] | None = None,
    submarket: str | Mapping[str, Any] | None = None,
    metro_slug: str | None = None,
    asset_class: str | None = None,
    star_tier: str | None = None,
    assumption_overrides: Mapping[str, Any] | None = None,
    market: Any = DEFAULT_MARKET,
    # Property tax (engine/property_tax.py)
    property_tax_millage: float | None = None,
    property_tax_assessment_ratio: float | None = None,
    property_tax_source_locator: str = DEFAULT_PROPERTY_TAX_SOURCE_LOCATOR,
) -> dict[str, Any]:
    """Build a canonical deal JSON from a rent roll, T12, and optional OM.

    Produces a valid deal schema. When deal economics flags are provided
    (purchase_price, ltv, loan_rate, etc.), the output is fully runnable
    by the engine. Without them, only revenue/opex sections are populated.

    Args:
        property_id: Deal identifier / deal_id for metadata
        rent_roll_path: Path to rent roll CSV/Excel
        t12_path: Path to T12 CSV/Excel
        analysis_start: Analysis start month "YYYY-MM"
        analysis_end: Analysis end month "YYYY-MM"
        rent_growth_rate: Annual rent growth assumption (flat). When omitted,
            the resolver writes a per-year vector: market forecast years, then
            a glide to the long-run rate (house 2.5% without market data); a
            house-sourced vector is recorded in
            ``metadata.ingest_gate.assumed_defaults`` for analyst review.
        vacancy_rate_override: Flat physical vacancy. When omitted, vacancy
            moves from the rent-roll level to stabilized vacancy over the
            deal type's stabilization window.
        collection_loss_rate: Collection loss rate. When omitted, T12 bad debt
            / GPR is used, else the house default (vintage
            ``bad_debt_typical_low``; 2.0% when the year built is unknown).
        analyst: Analyst name for metadata
        om_path: Optional path to OM PDF or text file
        om_source_locator: Stable original-document locator for OM tax evidence.
            Use this when ``om_path`` is a temporary extraction/download path.
        anthropic_client: Optional pre-built Anthropic client
        purchase_price: Total purchase price
        ltv: Loan-to-value ratio (0-1), used to compute commitment and equity
        loan_rate: Annual interest rate (0-1)
        amort_years: Amortization period in years
        io_months: Interest-only months (default 0)
        term_months: Loan term in months (default: analysis duration)
        exit_cap_rate: Exit cap rate (0-1), recorded as an analyst override.
            When omitted but ``exit_month`` or ``sale_cost_pct`` is given, the
            exit is still modeled and the resolver fills the cap rate (the
            market exit-year cap rate, else the disclosed engine default).
            When a purchase price is known (argument or OM) and no exit input
            is given, the exit is modeled at ``analysis_end`` with an
            ``exit_cap_rate`` placeholder (null) the resolver's fallback chain
            fills; ingest never invents a cap rate.
        exit_month: Exit month "YYYY-MM" (default: analysis end)
        sale_cost_pct: Sale cost percent. When omitted, the house default
            (``backsolve_policy.DEFAULTS``, 2%) is used and flagged for review.
        sponsor_equity_pct: GP equity share (0-1)
        pref_return: LP preferred return rate (0-1)
        acq_fee_pct: Acquisition fee percent (house default 1% when omitted)
        am_fee_pct: Asset management fee percent (house default 1.5% when omitted)
        disposition_fee_pct: Disposition fee percent (house default 1% when omitted)
        scan_sidecar_workbooks: Opt in to reading sibling workbooks in the rent
            roll's folder for bed/bath labels. Off by default: the scan opens
            every workbook in the folder.
        year_built: Subject year built. Drives the house vintage default for
            an omitted collection loss rate. When omitted, an OM-extracted
            ``year_built`` is used if available.
        deal_type: ``stabilized`` or ``value_add`` (ADR 0003 §1). ``lease_up``
            and ``distressed`` are reserved and raise ``UnsupportedDealTypeError``.
        msa: MSA name, five-digit CBSA code, or a ``deal_profile.msa`` mapping.
        submarket: Submarket name or a ``deal_profile.submarket`` mapping.
        metro_slug: Metro slug used by market-data providers.
        asset_class: ``deal_profile.asset_class`` (``A``, ``B`` or ``C``).
        star_tier: ``deal_profile.star_tier`` (``1_2``, ``3`` or ``4_5``); it
            is part of the geography market-data providers are asked for.
        assumption_overrides: ``{key: value | {"value": ..., "note": ...}}``
            analyst overrides applied by the assumption resolver. They win over
            the explicit kwargs above for the same key.
        market: Market-data registry for the resolver (anything with
            ``resolve(metric, Geo, as_of=None)``). Defaults to
            ``engine.market_data.default_registry()``; ``None`` uses no market
            data. Missing feeder data falls through to deal evidence and house
            policy.
        property_tax_millage: Combined property-tax millage in mills per
            $1,000 of assessed value. Builds
            ``metadata.property_summary.property_tax_policy`` with source
            ``analyst`` and ``property_tax_source_locator``; it wins over OM
            tax evidence. A priced deal (``purchase_price``) needs it: the
            validator reports ``PROPERTY_TAX_MILLAGE_MISSING`` without it.
        property_tax_assessment_ratio: Assessed value / purchase price
            (default 1.00). A ratio other than 1.00 is recorded as an analyst
            override. Requires ``property_tax_millage``.
        property_tax_source_locator: Stable provenance of the analyst millage
            (e.g. ``cli:--millage``).

    Returns:
        Canonical deal inputs dict (schema_version 0.1) with ``deal_profile``
        and ``assumptions`` sections (ADR 0003). Every explicit rate kwarg is
        recorded as an analyst override; omitted ones resolve to disclosed
        defaults. ``metadata.ingest_gate`` carries the T12 reconciliation, any
        blockers, the deal evidence the resolver reads, and (for backward
        compatibility) ``assumed_defaults`` derived from the resolver entries;
        a BLOCKED gate fails validation, assumed defaults only warn.
    """
    check_deal_type(deal_type)
    analyst_tax_policy = None
    if property_tax_millage is not None:
        analyst_tax_policy = analyst_property_tax_policy(
            property_tax_millage,
            property_tax_assessment_ratio,
            source_locator=property_tax_source_locator,
        )
    elif property_tax_assessment_ratio is not None:
        raise ValueError("property_tax_assessment_ratio requires property_tax_millage")
    from engine.modules.time_grid import TimeGrid
    from engine.ingest.t12_parser import parse_t12_statement
    from engine.ingest.rent_roll_parser import parse_rent_roll

    tg = TimeGrid.build(analysis_start, analysis_end)
    roll = parse_rent_roll(rent_roll_path, scan_sidecar_workbooks=scan_sidecar_workbooks)
    statement = parse_t12_statement(t12_path)
    opex_table = statement["opex_table"]

    gate_blockers: list[dict[str, str]] = []
    reconciliation = statement["reconciliation"]
    for side in ("opex", "income"):
        entry = reconciliation[side]
        if entry["status"] == "BLOCKED":
            gate_blockers.append({
                "code": f"t12_{side}_does_not_reconcile",
                "message": (
                    f"Parsed T12 {side} {entry['parsed_total']:,.2f} vs statement total "
                    f"{entry['statement_total']:,.2f} ({entry['variance_pct']:.2%} variance, "
                    f"tolerance {reconciliation['tolerance']:.2%})."
                ),
            })

    rent_roll_reconciliation = roll.get("reconciliation") or {"status": "UNVERIFIED"}
    gate_blockers.extend(_rent_roll_gate_blockers(rent_roll_reconciliation))

    unit_cohorts = roll["unit_cohorts"]
    # The rent-roll vacancy rate is deal evidence (ingest_gate.evidence); the
    # resolver builds the vacancy curve from it, and an explicit
    # vacancy_rate_override is applied as an analyst override.
    market_rents = roll.get("market_rent_by_cohort", {})
    start = tg.month_ids[0]
    end = tg.month_ids[-1]
    revenue_programs, program_adoption_curve, other_income = _build_t12_income_programs(
        statement["income_lines"], start, end
    )

    # Ensure analyst is non-empty (schema requires minLength: 1)
    analyst_name = analyst if analyst else "Analyst"

    # V1.2 — propagate sanity flags emitted by sub-parsers (e.g.
    # rent_roll_parser's per-cohort target_monthly_rent_missing_*) up
    # to deal.metadata.intake_sanity_flags so downstream consumers
    # (deal-intake agent, intake punchlist) can see them without
    # re-parsing source documents.
    intake_sanity_flags: list[str] = list(roll.get("blockers", [])) + list(roll.get("flags", []))
    metadata: dict[str, Any] = {
        "deal_id": property_id,
        "run_id": "ingested",
        "as_of_date": f"{analysis_start}-01",
        "analyst": analyst_name,
        "purpose": "Document Ingestion",
    }
    if reconciliation["status"] == "UNVERIFIED":
        intake_sanity_flags.append("t12_totals_not_found_reconciliation_unverified")
    if rent_roll_reconciliation.get("status") == "UNVERIFIED":
        intake_sanity_flags.append("rent_roll_summary_not_found_reconciliation_unverified")
    if intake_sanity_flags:
        metadata["intake_sanity_flags"] = intake_sanity_flags
    metadata["ingest_gate"] = {
        "status": "BLOCKED" if gate_blockers else "PASS",
        "blockers": gate_blockers,
        "assumed_defaults": [],
        "t12_reconciliation": reconciliation,
        "rent_roll_reconciliation": rent_roll_reconciliation,
        "t12_other_income": other_income,
        "evidence": {
            "rent_roll": _rent_roll_evidence(roll, unit_cohorts, market_rents),
            "t12": _t12_rent_evidence(statement["income_lines"]),
        },
    }

    deal: dict[str, Any] = {
        "schema_version": "0.1",
        "metadata": metadata,
        "time_grid": {
            "analysis_start_date": f"{analysis_start}-01",
            "analysis_end_date": f"{analysis_end}-01",
        },
        "unit_cohorts": unit_cohorts,
        "market_rent_curve": _build_market_rent_curve(unit_cohorts, market_rents, start, end),
        # Loss to lease, physical vacancy and concessions are placeholders the
        # assumption resolver fills with curves (ADR 0003 slice 4) from the
        # rent roll / T12 evidence recorded above, market data and house policy.
        "loss_to_lease": _build_cohort_curve(unit_cohorts, "ltl_percent", None, start, end),
        "physical_vacancy_curve": _build_cohort_curve(unit_cohorts, "vacancy_rate", None, start, end),
        "concession_schedule": None,
        "collection_loss_curve": [
            # loss_rate is filled by the assumption resolver (year_built may come from the OM).
            {"applies_to": "ALL", "start_period": start, "end_period": end, "loss_rate": None}
        ],
        "revenue_programs": revenue_programs,
        "program_adoption_curve": program_adoption_curve,
        "opex_table": opex_table,
        "growth_assumptions": {
            "growth_type": "annual_compound",
            "annual_growth_rate": None,  # filled by the assumption resolver
        },
    }
    _record_t12_opex_evidence(deal, statement["provenance"])

    # Merge OM data if provided (before CLI overrides, so CLI wins).
    # V1.2 — when om_parser auto-populates purchase_assumptions or
    # exit_assumptions, tag metadata.purchase_assumptions_source +
    # analyst_review_required so the analyst is on the hook for sanity-
    # checking the Haiku extraction. If extraction yields nothing usable
    # (or fails outright), emit an `om_extraction_unavailable` sanity
    # flag rather than blocking intake.
    if om_path is not None:
        om_extracted_anything = False
        try:
            broker_snapshot = parse_broker_snapshot_from_om(om_path)
            broker_tax_candidates = broker_snapshot.get(
                "property_tax_evidence_candidates", []
            )
            if not isinstance(broker_tax_candidates, list):
                broker_tax_candidates = []
            if om_source_locator:
                broker_tax_candidates = [
                    {
                        **dict(candidate),
                        "source_locator": om_source_locator,
                    }
                    if isinstance(candidate, Mapping)
                    else candidate
                    for candidate in broker_tax_candidates
                ]
        except Exception:  # noqa: BLE001 — optional local evidence parser
            broker_tax_candidates = []
        try:
            om = parse_om(om_path, client=anthropic_client)
            partial = om.to_partial_deal()
        except Exception:  # noqa: BLE001 — keep intake alive
            partial = {}
            om = None
            deal["metadata"].setdefault("intake_sanity_flags", []).append(
                "om_extraction_unavailable"
            )
        if broker_tax_candidates:
            partial_candidates = partial.get(
                "property_tax_evidence_candidates", []
            )
            if not isinstance(partial_candidates, list):
                partial_candidates = []
            partial["property_tax_evidence_candidates"] = [
                *partial_candidates,
                *broker_tax_candidates,
            ]

        if "purchase_assumptions" in partial and purchase_price is None:
            deal["purchase_assumptions"] = partial["purchase_assumptions"]
            # Extract OM purchase price for debt calculations below
            purchase_price = partial["purchase_assumptions"].get("purchase_price")
            if purchase_price is not None:
                om_extracted_anything = True
                deal["metadata"]["purchase_assumptions_source"] = (
                    "om_haiku_extraction"
                )
                deal["metadata"]["analyst_review_required"] = True
        if "exit_assumptions" in partial and exit_cap_rate is None:
            deal["exit_assumptions"] = partial["exit_assumptions"]
            if partial["exit_assumptions"].get("exit_cap_rate") is not None:
                deal["metadata"]["ingest_gate"]["evidence"]["om"] = {
                    "exit_cap_rate": partial["exit_assumptions"]["exit_cap_rate"],
                }
                # The OM rate is deal evidence; the resolver fills the field
                # (a market exit-year cap rate wins over it, ADR 0003 §3).
                deal["exit_assumptions"] = {**deal["exit_assumptions"], "exit_cap_rate": None}
                om_extracted_anything = True
                deal["metadata"]["analyst_review_required"] = True
        if (
            "property_summary" in partial
            or "property_tax_evidence_candidates" in partial
        ):
            property_summary = deepcopy(partial.get("property_summary", {}))
            labelled_candidates = property_summary.pop(
                "property_tax_evidence_candidates", []
            )
            if not isinstance(labelled_candidates, list):
                labelled_candidates = []
            top_level_candidates = partial.get(
                "property_tax_evidence_candidates", []
            )
            if isinstance(top_level_candidates, list):
                labelled_candidates.extend(top_level_candidates)
            if labelled_candidates:
                property_summary, tax_issue_code = (
                    promote_property_tax_evidence(
                        property_summary,
                        labelled_candidates,
                    )
                )
                if tax_issue_code is not None:
                    deal["metadata"].setdefault(
                        "intake_sanity_flags", []
                    ).append(tax_issue_code)
                    deal["metadata"]["analyst_review_required"] = True
            deal["metadata"]["property_summary"] = property_summary
            om_extracted_anything = True
        if om is not None:
            deal["metadata"]["om_extraction_confidence"] = om.extraction_confidence

        # If the OM parser ran without exception but produced nothing
        # the engine can use, surface the sanity flag so the deal-intake
        # punchlist can call it out for the analyst.
        if om is not None and not om_extracted_anything:
            deal["metadata"].setdefault("intake_sanity_flags", []).append(
                "om_extraction_unavailable"
            )

    if analyst_tax_policy is not None:
        apply_analyst_property_tax_policy(deal, analyst_tax_policy)

    # Explicit rate inputs are analyst overrides; omitted ones stay as
    # placeholders the assumption resolver fills with disclosed defaults.
    overrides: dict[str, Any] = {}
    for key, value, flag in (
        ("rent_growth", rent_growth_rate, "rent_growth_rate"),
        ("collection_loss", collection_loss_rate, "collection_loss_rate"),
        ("physical_vacancy", vacancy_rate_override, "vacancy_rate_override"),
    ):
        if value is not None:
            overrides[key] = {"value": value, "note": f"ingest input {flag}"}

    # Build purchase_assumptions from CLI flags
    if purchase_price is not None and "purchase_assumptions" not in deal:
        commitment = purchase_price * ltv if ltv else 0.0
        equity = purchase_price - commitment
        deal["purchase_assumptions"] = {
            "purchase_price": purchase_price,
            "equity_contribution": equity,
            "closing_costs": 0.0,
        }

    # Build debt_terms from CLI flags
    if purchase_price is not None and ltv is not None and loan_rate is not None and amort_years is not None:
        commitment = purchase_price * ltv
        deal["debt_terms"] = {
            "commitment": commitment,
            "rate": loan_rate,
            "amort_years": amort_years,
            "io_months": io_months,
            "term_months": term_months or len(tg.month_ids),
            "loan_start_month": start,
        }

    # Build exit_assumptions from CLI flags (or OM). An exit month or sale cost
    # without an exit cap rate still models the exit: the resolver fills the
    # cap rate (market exit-year cap rate, else the disclosed engine default).
    # A priced deal with no exit input is modeled as a sale at the analysis
    # end, with an exit_cap_rate placeholder (null) left for the resolver's
    # fallback chain; ingest never invents a cap rate.
    exit_given = any(value is not None for value in (exit_cap_rate, exit_month, sale_cost_pct))
    if (exit_given or purchase_price is not None) and "exit_assumptions" not in deal:
        deal["exit_assumptions"] = {
            "exit_month": exit_month or end,
            "sale_cost_percent": None,
        }
        if exit_cap_rate is not None or not exit_given:
            deal["exit_assumptions"]["exit_cap_rate"] = None
        if exit_cap_rate is not None:
            overrides["exit_cap_rate"] = {"value": exit_cap_rate, "note": "ingest input exit_cap_rate"}
        if sale_cost_pct is not None:
            overrides["sale_cost_percent"] = {"value": sale_cost_pct, "note": "ingest input sale_cost_pct"}

    # Build fund_assumptions from CLI flags
    if sponsor_equity_pct is not None:
        lp_pct = 1.0 - sponsor_equity_pct
        deal["fund_assumptions"] = {
            "sponsor_equity_pct": sponsor_equity_pct,
            "lp_equity_pct": lp_pct,
            "preferred_return": pref_return or 0.08,
            "acquisition_fee_pct": None,
            "asset_management_fee_pct": None,
            "disposition_fee_pct": None,
            "promote_splits": [
                {"tier": "Tier 1", "hurdle_irr": pref_return or 0.08,
                 "lp_share": 0.7, "gp_share": 0.3},
            ],
        }
        for key, value, flag in (
            ("acquisition_fee_pct", acq_fee_pct, "acq_fee_pct"),
            ("asset_management_fee_pct", am_fee_pct, "am_fee_pct"),
            ("disposition_fee_pct", disposition_fee_pct, "disposition_fee_pct"),
        ):
            if value is not None:
                overrides[key] = {"value": value, "note": f"ingest input {flag}"}

    resolved_year_built, year_built_origin = _resolve_year_built(year_built, deal["metadata"])
    if resolved_year_built is not None:
        deal["metadata"]["ingest_gate"]["evidence"]["year_built"] = {
            "value": resolved_year_built,
            "origin": year_built_origin,
        }
    profile = DealProfile.from_inputs(
        deal_type=deal_type,
        msa=msa,
        submarket=submarket,
        metro_slug=metro_slug,
        year_built=resolved_year_built,
    )
    if asset_class is not None or star_tier is not None:
        profile = replace(profile, asset_class=asset_class, star_tier=star_tier)
    if market is DEFAULT_MARKET:
        from engine.market_data import default_registry

        market = default_registry()
    deal = resolve_assumptions(
        deal,
        deal_profile=profile,
        overrides={**overrides, **dict(assumption_overrides or {})},
        market=market,
    )
    return sync_ingest_gate_assumptions(deal)


# Legacy ``metadata.ingest_gate.assumed_defaults`` view of the resolver
# entries: the two policy inputs ingest has always disclosed this way.
_LEGACY_ASSUMED_DEFAULT_FIELDS = (
    ("rent_growth", "growth_assumptions.annual_growth_rate"),
    ("collection_loss", "collection_loss_curve.loss_rate"),
)
_ASSUMED_DEFAULT_FLAG_PREFIX = "assumed_default_"


def _legacy_source(source: str) -> str:
    return "house_vintage_framework" if source.startswith("house:vintage_") else source


def sync_ingest_gate_assumptions(deal: dict[str, Any]) -> dict[str, Any]:
    """Refresh the backward-compatible ingest disclosures from ``deal["assumptions"]``.

    Rebuilds ``metadata.ingest_gate.assumed_defaults`` and the matching
    ``assumed_default_*`` intake sanity flags from the resolver entries so
    they never go stale after a re-resolve. A canonical without an ingest
    gate is returned unchanged. Mutates and returns ``deal``.
    """
    metadata = deal.get("metadata")
    gate = metadata.get("ingest_gate") if isinstance(metadata, dict) else None
    if not isinstance(gate, dict):
        return deal
    entries = (deal.get("assumptions") or {}).get("entries") or {}
    assumed: list[dict[str, Any]] = []
    for key, field in _LEGACY_ASSUMED_DEFAULT_FIELDS:
        entry = entries.get(key)
        if not entry or not entry.get("review_required") or entry.get("value") is None:
            continue
        value = entry["value"]
        if isinstance(value, list):
            # Per-year rent growth: the legacy field is the scalar (year-2) rate.
            value = value[0]
        elif isinstance(value, dict):
            continue
        assumed.append({
            "field": field,
            "value": value,
            "source": _legacy_source(entry["source"]),
            "basis": entry.get("basis", ""),
            "review_required": True,
        })
    gate["assumed_defaults"] = assumed
    flags = [
        flag
        for flag in metadata.get("intake_sanity_flags") or []
        if not flag.startswith(_ASSUMED_DEFAULT_FLAG_PREFIX)
    ]
    flags.extend(
        f"{_ASSUMED_DEFAULT_FLAG_PREFIX}{item['field'].rsplit('.', 1)[-1]}" for item in assumed
    )
    if flags:
        metadata["intake_sanity_flags"] = flags
    else:
        metadata.pop("intake_sanity_flags", None)
    collection = entries.get("collection_loss") or {}
    year_built = (deal.get("deal_profile") or {}).get("year_built")
    if str(collection.get("source", "")).startswith("house:vintage_") and year_built is not None:
        metadata.setdefault("year_built", year_built)
    return deal


_T12_GPR_PATTERNS = ("GROSS POTENTIAL", "POTENTIAL RENT", "GPR")
_T12_BAD_DEBT_PATTERNS = ("BAD DEBT", "WRITE OFF", "WRITE-OFF", "WRITEOFF", "COLLECTION LOSS")
_T12_CONCESSION_PATTERNS = ("CONCESSION",)


_RENT_ROLL_CHECK_LABELS = {
    "units": ("rent_roll_units_do_not_reconcile", "units"),
    "vacant_units": ("rent_roll_vacancy_does_not_reconcile", "vacant units"),
    "market_rent_total": ("rent_roll_market_rent_does_not_reconcile", "monthly market rent"),
    "rent_charges_total": ("rent_roll_rent_charges_do_not_reconcile", "monthly rent charges"),
}


def _rent_roll_gate_blockers(reconciliation: Mapping[str, Any]) -> list[dict[str, str]]:
    """One blocker per rent-roll check that does not tie to the export's own summary."""
    blockers: list[dict[str, str]] = []
    for check, (code, label) in _RENT_ROLL_CHECK_LABELS.items():
        entry = reconciliation.get(check)
        if not isinstance(entry, Mapping) or entry.get("status") != "BLOCKED":
            continue
        if check in {"units", "vacant_units"}:
            message = f"Parsed {entry['parsed']} {label} vs the export summary's {entry['export']}"
            if entry.get("non_revenue_units"):
                message += f" (+ up to {entry['non_revenue_units']} non-revenue units)"
            message += " (must match exactly)."
        else:
            message = (
                f"Parsed {label} {entry['parsed']:,.2f} vs the export summary's {entry['export']:,.2f} "
                f"({entry['variance_pct']:.2%} variance, tolerance {reconciliation['tolerance']:.2%})."
            )
        blockers.append({"code": code, "message": message})
    return blockers


def _t12_rent_evidence(income_lines: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """GPR, bad debt and concessions from the T12 income section, in absolute dollars."""
    totals: dict[str, float] = {}
    lines: dict[str, list[str]] = {}
    for line in income_lines:
        label = str(line.get("label", "")).upper()
        for field, patterns in (
            ("gross_potential_rent", _T12_GPR_PATTERNS),
            ("bad_debt", _T12_BAD_DEBT_PATTERNS),
            ("concessions", _T12_CONCESSION_PATTERNS),
        ):
            if any(pattern in label for pattern in patterns):
                totals[field] = round(totals.get(field, 0.0) + abs(float(line["annual_amount"])), 2)
                lines.setdefault(field, []).append(str(line.get("label", "")))
                break
    if not totals:
        return {}
    return {**totals, "source_lines": lines}


def _rent_roll_evidence(
    roll: Mapping[str, Any],
    unit_cohorts: Sequence[Mapping[str, Any]],
    market_rents: Mapping[str, float],
) -> dict[str, Any]:
    """Current physical vacancy and unit-weighted loss to lease from the rent roll."""
    evidence: dict[str, Any] = {"physical_vacancy_rate": roll["physical_vacancy_rate"]}
    for key in ("layout", "pre_leased_vacant_units", "bed_bath_inference"):
        if roll.get(key):
            evidence[key] = roll[key]
    market_total = Decimal("0")
    gap_total = Decimal("0")
    for cohort in unit_cohorts:
        market = market_rents.get(cohort["cohort_id"])
        if not market:
            continue
        units = Decimal(str(cohort.get("unit_count") or 0))
        market_dec = Decimal(str(market))
        inplace = Decimal(str(cohort.get("initial_inplace_rent") or 0))
        market_total += market_dec * units
        gap_total += max(Decimal("0"), market_dec - inplace) * units
    if market_total > 0:
        evidence["loss_to_lease_rate"] = float(round(gap_total / market_total, 6))
    return evidence


def _record_t12_opex_evidence(deal: dict[str, Any], provenance: Mapping[str, Any]) -> None:
    """T12 insurance and R&M annual dollars, annualized by calculation_type, and
    every T12 opex line with the raw source rows that fed it (ADR 0003 §6)."""
    evidence = deal["metadata"]["ingest_gate"]["evidence"]["t12"]
    evidence["opex_lines"] = t12_opex_line_evidence(deal, provenance)
    insurance_rows = matching_opex_rows(deal, "insurance")
    if insurance_rows:
        evidence["insurance_annual"] = float(annual_opex_amount(deal, insurance_rows[0]))
    if any(
        opex_bucket_for_policy(str(row.get("category_name", ""))) == "repairs_maintenance"
        for row in deal.get("opex_table") or []
    ):
        evidence["repairs_maintenance_annual"] = float(sum_opex_bucket(deal, "repairs_maintenance"))


def _resolve_year_built(
    explicit: int | None,
    metadata: Mapping[str, Any],
) -> tuple[int | None, str | None]:
    """Return (year_built, origin): the explicit input first, then the OM extraction."""
    property_summary = metadata.get("property_summary") or {}
    for candidate, origin in (
        (explicit, "analyst input"),
        (metadata.get("year_built"), "OM metadata.year_built"),
        (property_summary.get("year_built"), "OM property_summary.year_built"),
    ):
        if candidate in (None, ""):
            continue
        try:
            normalized = _normalize_year_built(int(candidate))
        except (TypeError, ValueError):
            continue
        if normalized is not None:
            return normalized, origin
    return None, None

T12_PROGRAM_PREFIX = "t12_"
_T12_INCOME_PROGRAMS = {
    "recovery": ("t12_utility_reimbursements", "T12 utility reimbursements", "recovery"),
    "other": ("t12_other_income", "T12 other income", "asset-based"),
}


def _build_t12_income_programs(
    income_lines: list[dict[str, Any]],
    start: str,
    end: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Carry T12 non-rent income into revenue programs.

    Rent lines are modeled from the rent roll and skipped. Reimbursement and
    other-income lines are summed into one flat monthly ($/asset) program per
    kind at their trailing-12 run rate. Returns (programs, adoption curve,
    per-line provenance).
    """
    programs: list[dict[str, Any]] = []
    adoption: list[dict[str, Any]] = []
    provenance: list[dict[str, Any]] = []
    for kind, (program_id, program_name, program_type) in _T12_INCOME_PROGRAMS.items():
        lines = [line for line in income_lines if line["kind"] == kind]
        annual = round(sum(line["annual_amount"] for line in lines), 2)
        included = annual > 0
        provenance.extend(
            {**line, "program_id": program_id if included else None} for line in lines
        )
        if not included:
            continue
        programs.append({
            "program_id": program_id,
            "program_name": program_name,
            "program_type": program_type,
            "pricing_type": "$/asset",
            "price_value": round(annual / 12, 2),
            "eligible_units": "ALL",
            "start_period": start,
            "end_period": end,
        })
        adoption.append({
            "program_id": program_id,
            "start_period": start,
            "end_period": end,
            "adoption_rate": 1.0,
        })
    return programs, adoption, provenance


def _build_market_rent_curve(
    cohorts: list[dict],
    market_rents: dict[str, float],
    start: str,
    end: str,
) -> list[dict]:
    """Build flat market_rent_curve matching schema v0.1.

    Bug 1.12 fix — tag each segment with `target_monthly_rent_source` so the
    Wave 1 validator can flag deals whose market rents are pure broker-rosy
    fallbacks (every cohort defaulting to in-place rent). Two values:
      - "rent_roll_market": explicit market rent provided by the rent roll
      - "in_place_fallback": no market rent on file; using initial_inplace_rent
    """
    curve: list[dict] = []
    for c in cohorts:
        cohort_id = c["cohort_id"]
        if cohort_id in market_rents:
            market_rent = market_rents[cohort_id]
            source = "rent_roll_market"
        else:
            market_rent = c["initial_inplace_rent"]
            source = "in_place_fallback"
        curve.append({
            "cohort_id": cohort_id,
            "start_period": start,
            "end_period": end,
            "market_rent": market_rent,
            "target_monthly_rent_source": source,
        })
    return curve


def _build_cohort_curve(
    cohorts: list[dict],
    value_key: str,
    value: float | None,
    start: str,
    end: str,
) -> list[dict]:
    """Build flat cohort curve (LTL, vacancy) matching schema v0.1."""
    return [
        {
            "cohort_id": c["cohort_id"],
            "start_period": start,
            "end_period": end,
            value_key: value,
        }
        for c in cohorts
    ]
