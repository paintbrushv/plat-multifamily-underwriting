"""Ingestion pipeline: raw documents → canonical deal JSON.

Combines T12 + rent roll into a valid canonical deal schema.
Optional CLI flags populate debt_terms, purchase_assumptions,
exit_assumptions, and fund_assumptions inline.

Output is compatible with canonical schema v0.1 and can be passed directly
to run_underwriting() or generate_rediq_workbook.py --from-json.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from engine.ingest.broker_om_snapshot import parse_broker_snapshot_from_om
from engine.ingest.om_parser import parse_om  # noqa: E402 — lazy-safe, no heavy imports
from engine.ingest.property_tax_migration import (
    migrate_legacy_property_tax_policy,
)
from engine.property_tax import build_property_tax_policy


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


def build_deal_from_documents(
    property_id: str,
    rent_roll_path: str | Path,
    t12_path: str | Path,
    analysis_start: str,
    analysis_end: str,
    rent_growth_rate: float = 0.03,
    vacancy_rate_override: float | None = None,
    collection_loss_rate: float = 0.005,
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
    sale_cost_pct: float = 0.02,
    # Fund structure (optional)
    sponsor_equity_pct: float | None = None,
    pref_return: float | None = None,
    acq_fee_pct: float = 0.01,
    am_fee_pct: float = 0.015,
    disposition_fee_pct: float = 0.01,
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
        rent_growth_rate: Annual rent growth assumption (default 3%)
        vacancy_rate_override: Override physical_vacancy_rate from rent roll
        collection_loss_rate: Collection loss rate (default 0.5%)
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
        exit_cap_rate: Exit cap rate (0-1)
        exit_month: Exit month "YYYY-MM" (default: analysis end)
        sale_cost_pct: Sale cost percent (default 2%)
        sponsor_equity_pct: GP equity share (0-1)
        pref_return: LP preferred return rate (0-1)
        acq_fee_pct: Acquisition fee percent (default 1%)
        am_fee_pct: Asset management fee percent (default 1.5%)
        disposition_fee_pct: Disposition fee percent (default 1%)

    Returns:
        Canonical deal inputs dict (schema_version 0.1)
    """
    from engine.modules.time_grid import TimeGrid
    from engine.ingest.t12_parser import parse_t12
    from engine.ingest.rent_roll_parser import parse_rent_roll

    tg = TimeGrid.build(analysis_start, analysis_end)
    roll = parse_rent_roll(rent_roll_path)
    opex_table = parse_t12(t12_path)

    unit_cohorts = roll["unit_cohorts"]
    vacancy_rate = vacancy_rate_override if vacancy_rate_override is not None else roll["physical_vacancy_rate"]
    market_rents = roll.get("market_rent_by_cohort", {})
    start = tg.month_ids[0]
    end = tg.month_ids[-1]

    # Ensure analyst is non-empty (schema requires minLength: 1)
    analyst_name = analyst if analyst else "Analyst"

    # V1.2 — propagate sanity flags emitted by sub-parsers (e.g.
    # rent_roll_parser's per-cohort target_monthly_rent_missing_*) up
    # to deal.metadata.intake_sanity_flags so downstream consumers
    # (deal-intake agent, intake punchlist) can see them without
    # re-parsing source documents.
    intake_sanity_flags: list[str] = list(roll.get("blockers", []))
    metadata: dict[str, Any] = {
        "deal_id": property_id,
        "run_id": "ingested",
        "as_of_date": f"{analysis_start}-01",
        "analyst": analyst_name,
        "purpose": "Document Ingestion",
    }
    if intake_sanity_flags:
        metadata["intake_sanity_flags"] = intake_sanity_flags

    deal: dict[str, Any] = {
        "schema_version": "0.1",
        "metadata": metadata,
        "time_grid": {
            "analysis_start_date": f"{analysis_start}-01",
            "analysis_end_date": f"{analysis_end}-01",
        },
        "unit_cohorts": unit_cohorts,
        "market_rent_curve": _build_market_rent_curve(unit_cohorts, market_rents, start, end),
        "loss_to_lease": _build_cohort_curve(unit_cohorts, "ltl_percent", 0.0, start, end),
        "physical_vacancy_curve": _build_cohort_curve(unit_cohorts, "vacancy_rate", vacancy_rate, start, end),
        "collection_loss_curve": [
            {"applies_to": "ALL", "start_period": start, "end_period": end, "loss_rate": collection_loss_rate}
        ],
        "revenue_programs": [],
        "program_adoption_curve": [],
        "opex_table": opex_table,
        "growth_assumptions": {
            "growth_type": "annual_compound",
            "annual_growth_rate": rent_growth_rate,
        },
    }

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

    # Build exit_assumptions from CLI flags (or OM)
    if exit_cap_rate is not None and "exit_assumptions" not in deal:
        deal["exit_assumptions"] = {
            "exit_cap_rate": exit_cap_rate,
            "exit_month": exit_month or end,
            "sale_cost_percent": sale_cost_pct,
        }

    # Build fund_assumptions from CLI flags
    if sponsor_equity_pct is not None:
        lp_pct = 1.0 - sponsor_equity_pct
        deal["fund_assumptions"] = {
            "sponsor_equity_pct": sponsor_equity_pct,
            "lp_equity_pct": lp_pct,
            "preferred_return": pref_return or 0.08,
            "acquisition_fee_pct": acq_fee_pct,
            "asset_management_fee_pct": am_fee_pct,
            "disposition_fee_pct": disposition_fee_pct,
            "promote_splits": [
                {"tier": "Tier 1", "hurdle_irr": pref_return or 0.08,
                 "lp_share": 0.7, "gp_share": 0.3},
            ],
        }

    return deal


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
    value: float,
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
