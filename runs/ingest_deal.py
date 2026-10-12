#!/usr/bin/env python3
"""CLI: ingest deal documents or manual agent-authored JSON → canonical deal JSON.

Usage:
    # Minimal (revenue/opex only — analyst adds debt/exit later):
    python runs/ingest_deal.py \\
      --property-id "Forest Hills Apartments" \\
      --rent-roll data/rent_roll.xlsx \\
      --t12 data/t12.xlsx \\
      --start 2026-05 --end 2031-04

    # Fully runnable (all sections populated):
    python runs/ingest_deal.py \\
      --property-id "Forest Hills Apartments" \\
      --rent-roll data/rent_roll.xlsx \\
      --t12 data/t12.xlsx \\
      --start 2026-05 --end 2031-04 \\
      --purchase-price 25000000 --ltv 0.75 --rate 0.065 --amort 30 \\
      --exit-cap 0.055 --sponsor-pct 0.10 --pref-return 0.08 \\
      --millage 22.5 --assessment-ratio 0.8

    A purchase price makes the property-tax millage required: the engine taxes
    price x assessment ratio x mills / 1000, so validation reports
    PROPERTY_TAX_MILLAGE_MISSING until --millage (or OM tax evidence, or a
    --partial-json property_tax_policy) supplies it.

    # With OM parsing (extracts purchase price, exit cap from PDF):
    python runs/ingest_deal.py \\
      --property-id "Forest Hills" \\
      --rent-roll data/rent_roll.xlsx \\
      --t12 data/t12.xlsx \\
      --start 2026-05 --end 2031-04 \\
      --om data/offering_memo.pdf \\
      --ltv 0.75 --rate 0.065 --amort 30

The output JSON can be passed directly to generate_rediq_workbook.py via
--from-json. When deal economics flags are provided, the JSON is fully
engine-runnable. Without them, the analyst must add debt_terms,
purchase_assumptions, exit_assumptions, and fund_assumptions manually.

Manual / terminal-assisted workflows:
    # Use an already-complete canonical JSON produced in-session:
    python runs/ingest_deal.py \
      --canonical-json data/deal_inputs.json \
      --output output/deal_inputs.validated.json --validate

    # Merge an agent-authored partial JSON onto doc-derived rent roll + T12:
    python runs/ingest_deal.py \
      --rent-roll data/rent_roll.xlsx \
      --t12 data/t12.xlsx \
      --start 2026-05 --end 2031-04 \
      --partial-json data/analyst_patch.json \
      --output output/deal_inputs.json --validate

Assumption layer (ADR 0003):
    # Value-add profile with geography and analyst assumption overrides:
    python runs/ingest_deal.py ... --deal-type value_add --year-built 1984 \
      --msa "Synthetic Metro" --metro-slug synthetic_metro --submarket "Synthetic North" \
      --star-tier 3 --asset-class B \
      --assumption-overrides data/assumption_overrides.json

    The overrides file is a JSON object {key: value | {"value": v, "note": "..."}}
    keyed by assumption key (rent_growth, collection_loss, physical_vacancy, ...),
    or an edited assumptions sheet: the <output>.assumptions.json this CLI
    writes, or an .xlsx carrying the "Assumptions" tab
    (runs/assumptions_sheet.py export). Values a --partial-json patch supplies
    for an assumption are recorded as analyst overrides, and the assumptions
    are re-resolved after the merge.

    Every run that resolves assumptions also writes the assumptions sheet
    <output stem>.assumptions.json next to the output JSON.

    Market data (rent-growth forecast, stabilized vacancy, concession rate,
    market cap rate) comes from engine.market_data.default_registry(); pass
    --no-market-data to resolve from deal evidence and house policy only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.assumptions import (
    AssumptionOverrideError,
    DealProfile,
    UnsupportedDealTypeError,
    normalize_overrides,
    overrides_from_patch,
    resolve_assumptions,
)
from engine.assumptions.profile import ASSET_CLASSES, STAR_TIERS
from engine.assumptions.sheet import (
    has_assumptions,
    load_assumption_overrides,
    sheet_path_for,
    write_assumptions_json,
)
from engine.ingest.broker_om_snapshot import stable_property_tax_source_locator
from engine.ingest.ingestion_pipeline import (
    analyst_property_tax_policy,
    apply_analyst_property_tax_policy,
    build_deal_from_documents,
    sync_ingest_gate_assumptions,
)
from engine.validator import PROPERTY_TAX_MILLAGE_MISSING

PARTIAL_JSON_OVERRIDE_NOTE = "supplied by --partial-json"


def _load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return data


def _deep_merge(base: object, patch: object) -> object:
    if isinstance(base, dict) and isinstance(patch, dict):
        merged = dict(base)
        for key, value in patch.items():
            if key in merged:
                merged[key] = _deep_merge(merged[key], value)
            else:
                merged[key] = value
        return merged
    return patch


def _apply_partial_json(base_deal: dict, partial: dict) -> dict:
    merged = _deep_merge(base_deal, partial)
    if not isinstance(merged, dict):  # defensive; _deep_merge returns dict here
        raise ValueError("Merged deal payload must be a dict")
    growth_patch = partial.get("growth_assumptions")
    if (
        isinstance(growth_patch, dict)
        and "annual_growth_rate" in growth_patch
        and "annual_growth_rates" not in growth_patch
        and isinstance(merged.get("growth_assumptions"), dict)
    ):
        # A scalar rent-growth patch is a flat rate: drop the resolved per-year vector.
        merged["growth_assumptions"] = {
            k: v for k, v in merged["growth_assumptions"].items() if k != "annual_growth_rates"
        }

    metadata = merged.setdefault("metadata", {})
    partial_metadata = partial.get("metadata", {}) if isinstance(partial.get("metadata"), dict) else {}
    if "purchase_assumptions" in partial and "purchase_assumptions_source" not in partial_metadata:
        metadata["purchase_assumptions_source"] = "analyst_override"
    if any(k in partial for k in ("purchase_assumptions", "debt_terms", "exit_assumptions", "fund_assumptions")):
        metadata["analyst_review_required"] = False
    return merged


def apply_partial_and_reresolve(base_deal: dict, partial: dict, *, market: object = None) -> dict:
    """Merge a partial patch, then re-run the assumption resolver.

    Assumption values the patch supplies become analyst overrides, so the
    assumptions section and the legacy ``ingest_gate.assumed_defaults`` reflect
    the merged deal instead of the pre-merge ingest. ``market`` is the
    market-data registry used at ingest (market-sourced values are kept when
    it is None).
    """
    merged = _apply_partial_json(base_deal, partial)
    if "assumptions" not in base_deal:
        return merged
    overrides = overrides_from_patch(partial, merged, note=PARTIAL_JSON_OVERRIDE_NOTE)
    resolved = resolve_assumptions(merged, overrides=overrides, market=market)
    return sync_ingest_gate_assumptions(resolved)


_PROFILE_FLAGS = ("deal_type", "msa", "submarket", "metro_slug", "year_built", "asset_class", "star_tier")
MILLAGE_SOURCE_LOCATOR = "cli:--millage"
MILLAGE_AND_RATIO_SOURCE_LOCATOR = "cli:--millage,--assessment-ratio"


def _profile_from_args(
    args: argparse.Namespace, *, base: object = None
) -> DealProfile:
    """The deal profile the flags describe, merged over ``base`` (an existing
    ``deal_profile``) field by field: a flag wins, an omitted flag keeps the base."""
    base = base if isinstance(base, dict) else {}
    flags = DealProfile.from_inputs(
        deal_type=args.deal_type or base.get("deal_type") or "stabilized",
        msa=args.msa,
        submarket=args.submarket,
        metro_slug=args.metro_slug,
    )
    msa = {**(base.get("msa") or {}), **(flags.msa or {})}
    submarket = {**(base.get("submarket") or {}), **(flags.submarket or {})}
    return DealProfile(
        deal_type=flags.deal_type,
        msa=msa or None,
        submarket=submarket or None,
        year_built=args.year_built if args.year_built is not None else base.get("year_built"),
        asset_class=args.asset_class or base.get("asset_class"),
        star_tier=args.star_tier or base.get("star_tier"),
    )


def _profile_flags_given(args: argparse.Namespace) -> bool:
    return any(getattr(args, name) is not None for name in _PROFILE_FLAGS)


def _property_tax_policy_from_args(args: argparse.Namespace) -> dict | None:
    """``property_tax_policy`` from --millage/--assessment-ratio, or None.

    Raises ValueError for --assessment-ratio without --millage or an unusable value.
    """
    if args.millage is None:
        if args.assessment_ratio is not None:
            raise ValueError("--assessment-ratio requires --millage")
        return None
    locator = MILLAGE_SOURCE_LOCATOR if args.assessment_ratio is None else MILLAGE_AND_RATIO_SOURCE_LOCATOR
    return analyst_property_tax_policy(args.millage, args.assessment_ratio, source_locator=locator)


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for this CLI.

    Extracted from main() so tests can introspect flags / mutex behavior
    without spawning a subprocess (which re-imports the entire engine).
    """
    p = argparse.ArgumentParser(
        description="Ingest deal documents (rent roll + T12) → canonical deal JSON"
    )
    # Required document inputs
    p.add_argument("--property-id", required=False, help="Deal identifier")
    p.add_argument("--canonical-json", type=Path, default=None,
                   help="Path to a full canonical deal JSON produced manually or by an agent. "
                        "Bypasses document parsing and writes/validates that payload directly.")
    p.add_argument("--partial-json", type=Path, default=None,
                   help="Path to an agent-authored partial canonical JSON patch. Merged onto the "
                        "document-derived deal after rent roll/T12 ingestion.")
    p.add_argument("--rent-roll", type=Path, default=None,
                   help="Rent roll CSV or Excel (required in document mode)")
    p.add_argument("--t12", type=Path, default=None,
                   help="T12 CSV or Excel (required in document mode)")
    p.add_argument("--start", required=False, help="Analysis start YYYY-MM (e.g. 2026-05)")
    p.add_argument("--end", required=False, help="Analysis end YYYY-MM (e.g. 2031-04)")

    # Output
    p.add_argument("--output", "-o", type=Path, default=None, help="Output JSON path")
    p.add_argument("--analyst", default="", help="Analyst name for metadata")

    # Revenue/OpEx assumptions
    p.add_argument("--rent-growth", type=float, default=None,
                   help="Annual rent growth rate. If omitted, a house default is used and "
                        "flagged for review (metadata.ingest_gate.assumed_defaults).")
    p.add_argument("--vacancy", type=float, default=None, help="Vacancy rate override (0-1)")
    p.add_argument("--collection-loss", type=float, default=None,
                   help="Collection loss rate. If omitted, a house default is used (the vintage "
                        "framework floor when the year built is known) and flagged for review.")
    p.add_argument("--year-built", type=int, default=None,
                   help="Subject year built (deal_profile.year_built). Drives the house vintage "
                        "defaults; otherwise an OM-extracted year built is used.")
    p.add_argument("--scan-sidecar-workbooks", action="store_true",
                   help="Read sibling workbooks in the rent roll's folder (e.g. a Box Score) for "
                        "bed/bath labels. Off by default.")

    # OM parsing
    p.add_argument("--om", type=Path, default=None,
                   help="Path to Offering Memorandum PDF (optional). Extracts purchase price, "
                        "exit cap, unit mix via Claude Haiku. Requires ANTHROPIC_API_KEY.")

    # Deal economics
    p.add_argument("--purchase-price", type=float, default=None,
                   help="Purchase price (overrides OM extraction if both provided)")
    p.add_argument("--ltv", type=float, default=None,
                   help="Loan-to-value ratio (0-1). Requires --purchase-price or OM.")
    p.add_argument("--rate", type=float, default=None,
                   help="Annual interest rate (e.g. 0.065 for 6.5%%)")
    p.add_argument("--amort", type=int, default=None,
                   help="Amortization period in years (e.g. 30)")
    p.add_argument("--io-months", type=int, default=0,
                   help="Interest-only months (default 0)")
    p.add_argument("--term-months", type=int, default=None,
                   help="Loan term in months (default: analysis duration)")

    # Property tax (engine/property_tax.py)
    p.add_argument("--millage", type=float, default=None, metavar="MILLS",
                   help="Combined property-tax millage in mills per $1,000 of assessed value "
                        "(e.g. 22.5). Writes metadata.property_summary.property_tax_policy "
                        "(source analyst). A priced deal fails validation without it "
                        "(PROPERTY_TAX_MILLAGE_MISSING).")
    p.add_argument("--assessment-ratio", type=float, default=None, metavar="RATIO",
                   help="Assessed value / purchase price for the property tax (default 1.00; "
                        "any other value is recorded as an analyst override). Requires --millage.")

    # Exit assumptions
    p.add_argument("--exit-cap", type=float, default=None,
                   help="Exit cap rate (0-1). Overrides OM if both provided.")
    p.add_argument("--exit-month", type=str, default=None,
                   help="Exit month YYYY-MM (default: analysis end)")
    p.add_argument("--sale-cost", type=float, default=None,
                   help="Sale cost percent. If omitted, the house default (2%%) is used and "
                        "flagged for review.")

    # Fund structure
    p.add_argument("--sponsor-pct", type=float, default=None,
                   help="Sponsor (GP) equity percent (e.g. 0.10 for 10%%)")
    p.add_argument("--pref-return", type=float, default=None,
                   help="LP preferred return rate (e.g. 0.08 for 8%%)")
    p.add_argument("--acq-fee", type=float, default=None,
                   help="Acquisition fee percent (house default 1%% when omitted)")
    p.add_argument("--am-fee", type=float, default=None,
                   help="Asset management fee percent (house default 1.5%% when omitted)")
    p.add_argument("--disp-fee", type=float, default=None,
                   help="Disposition fee percent (house default 1%% when omitted)")

    # Assumption layer (ADR 0003)
    p.add_argument("--deal-type", default=None,
                   help="Deal profile: stabilized (default) or value_add. lease_up and "
                        "distressed are reserved and rejected.")
    p.add_argument("--msa", default=None,
                   help="MSA name or five-digit CBSA code (deal_profile.msa)")
    p.add_argument("--submarket", default=None,
                   help="Submarket name (deal_profile.submarket)")
    p.add_argument("--metro-slug", default=None,
                   help="Metro slug used by market-data providers (deal_profile.msa.metro_slug)")
    p.add_argument("--star-tier", default=None, choices=list(STAR_TIERS),
                   help="CoStar-style star tier (deal_profile.star_tier); part of the market-data geography")
    p.add_argument("--asset-class", default=None, choices=list(ASSET_CLASSES),
                   help="Asset class (deal_profile.asset_class)")
    p.add_argument("--no-market-data", action="store_true",
                   help="Resolve assumptions without market data. By default the market-data "
                        "registry (engine.market_data.default_registry) is used; missing feeder "
                        "data falls through to deal evidence and house policy.")
    p.add_argument("--assumption-overrides", type=Path, default=None, metavar="PATH",
                   help="Analyst assumption overrides: a JSON object "
                        '{key: value | {"value": v, "note": "..."}}, an edited '
                        "<output>.assumptions.json sheet, or an .xlsx with an edited "
                        '"Assumptions" tab.')

    # Validation
    p.add_argument("--validate", action="store_true",
                   help="Run schema validation on output and report issues")

    return p


def main() -> int:
    p = build_parser()
    args = p.parse_args()

    if args.canonical_json and args.partial_json:
        print("Error: --canonical-json cannot be combined with --partial-json.", file=sys.stderr)
        return 2

    try:
        analyst_tax_policy = _property_tax_policy_from_args(args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if args.no_market_data:
        market = None
    else:
        from engine.market_data import default_registry

        market = default_registry()

    assumption_overrides = None
    if args.assumption_overrides is not None:
        if not args.assumption_overrides.exists():
            print(f"Error: Assumption overrides not found: {args.assumption_overrides}", file=sys.stderr)
            return 1
        try:
            assumption_overrides = load_assumption_overrides(args.assumption_overrides)
        except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
            print(f"Error: failed to load assumption overrides: {exc}", file=sys.stderr)
            return 1

    if args.canonical_json:
        forbidden = [
            args.rent_roll,
            args.t12,
            args.start,
            args.end,
            args.om,
            args.purchase_price,
            args.ltv,
            args.rate,
            args.amort,
            args.exit_cap,
            args.sponsor_pct,
            args.pref_return,
        ]
        if any(value is not None and value is not False for value in forbidden):
            print(
                "Error: --canonical-json is a standalone input mode and cannot be combined "
                "with document-source, economics, or OM flags.",
                file=sys.stderr,
            )
            return 2
        if not args.canonical_json.exists():
            print(f"Error: Canonical JSON not found: {args.canonical_json}", file=sys.stderr)
            return 1
        try:
            result = _load_json(args.canonical_json)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"Error: failed to load canonical JSON: {exc}", file=sys.stderr)
            return 1
        property_id = (
            args.property_id
            or result.get("metadata", {}).get("deal_id")
            or args.canonical_json.stem
        )
        rent_roll_path = None
        t12_path = None
        om_path_resolved = None
        om_source_locator = None
        if analyst_tax_policy is not None:
            apply_analyst_property_tax_policy(result, analyst_tax_policy)
        if assumption_overrides is not None or _profile_flags_given(args):
            try:
                result = resolve_assumptions(
                    result,
                    deal_profile=(
                        _profile_from_args(args, base=result.get("deal_profile"))
                        if _profile_flags_given(args)
                        else None
                    ),
                    overrides=assumption_overrides,
                    market=market,
                )
            except (UnsupportedDealTypeError, AssumptionOverrideError, ValueError) as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 2
            sync_ingest_gate_assumptions(result)
    else:
        if not args.start or not args.end:
            print(
                "Error: --start and --end are required unless --canonical-json is used.",
                file=sys.stderr,
            )
            return 2
        if not args.rent_roll or not args.t12:
            print(
                "Error: both --rent-roll and --t12 are required in document mode.",
                file=sys.stderr,
            )
            return 2

        partial_payload = None
        if args.partial_json is not None:
            if not args.partial_json.exists():
                print(f"Error: Partial JSON not found: {args.partial_json}", file=sys.stderr)
                return 1
            try:
                partial_payload = _load_json(args.partial_json)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                print(f"Error: failed to load partial JSON: {exc}", file=sys.stderr)
                return 1
        property_id = (
            args.property_id
            or (partial_payload or {}).get("metadata", {}).get("deal_id")
        )
        if not property_id:
            print(
                "Error: --property-id is required unless --partial-json provides metadata.deal_id.",
                file=sys.stderr,
            )
            return 2

        om_source_locator: str | None = None

        if not args.rent_roll.exists():
            print(f"Error: Rent roll not found: {args.rent_roll}", file=sys.stderr)
            return 1
        if not args.t12.exists():
            print(f"Error: T12 not found: {args.t12}", file=sys.stderr)
            return 1
        rent_roll_path = args.rent_roll
        t12_path = args.t12
        om_path_resolved = args.om
        if om_path_resolved is not None:
            om_source_locator = stable_property_tax_source_locator(
                om_path_resolved
            )

        print(f"Ingesting documents for: {property_id}")
        print(f"  Rent roll: {rent_roll_path}")
        print(f"  T12:       {t12_path}")
        print(f"  Period:    {args.start} to {args.end}")

        try:
            _profile_from_args(args)
            normalize_overrides(assumption_overrides)
        except (UnsupportedDealTypeError, AssumptionOverrideError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2

        result = build_deal_from_documents(
            property_id=property_id,
            rent_roll_path=rent_roll_path,
            t12_path=t12_path,
            analysis_start=args.start,
            analysis_end=args.end,
            rent_growth_rate=args.rent_growth,
            vacancy_rate_override=args.vacancy,
            collection_loss_rate=args.collection_loss,
            analyst=args.analyst,
            om_path=om_path_resolved,
            om_source_locator=om_source_locator,
            purchase_price=args.purchase_price,
            ltv=args.ltv,
            loan_rate=args.rate,
            amort_years=args.amort,
            io_months=args.io_months,
            term_months=args.term_months,
            exit_cap_rate=args.exit_cap,
            exit_month=args.exit_month,
            sale_cost_pct=args.sale_cost,
            sponsor_equity_pct=args.sponsor_pct,
            pref_return=args.pref_return,
            acq_fee_pct=args.acq_fee,
            am_fee_pct=args.am_fee,
            disposition_fee_pct=args.disp_fee,
            scan_sidecar_workbooks=args.scan_sidecar_workbooks,
            year_built=args.year_built,
            deal_type=args.deal_type or "stabilized",
            msa=args.msa,
            submarket=args.submarket,
            metro_slug=args.metro_slug,
            asset_class=args.asset_class,
            star_tier=args.star_tier,
            assumption_overrides=assumption_overrides,
            market=market,
            property_tax_millage=args.millage,
            property_tax_assessment_ratio=args.assessment_ratio,
            property_tax_source_locator=(
                analyst_tax_policy["source_locator"] if analyst_tax_policy else MILLAGE_SOURCE_LOCATOR
            ),
        )
        if partial_payload is not None:
            try:
                result = apply_partial_and_reresolve(result, partial_payload, market=market)
            except (UnsupportedDealTypeError, AssumptionOverrideError, ValueError) as exc:
                print(f"Error: failed to re-resolve assumptions after --partial-json: {exc}", file=sys.stderr)
                return 2
            print(f"  Applied partial overrides from: {args.partial_json}")

    if args.canonical_json:
        print(f"Using canonical JSON for: {property_id}")
        print(f"  Source: {args.canonical_json}")

    out = args.output
    if out is None:
        safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in property_id).strip()
        out = REPO_ROOT / "output" / f"{safe}_ingested_inputs.json"

    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        json.dump(result, f, indent=2)
    sheet_path = None
    if has_assumptions(result):
        sheet_path = write_assumptions_json(result, sheet_path_for(out))

    # Summary
    print(f"\nIngestion complete:")
    cohorts = result.get("unit_cohorts", [])
    opex = result.get("opex_table", [])
    if cohorts:
        vacancy_curve = result.get("physical_vacancy_curve", [])
        vacancy = vacancy_curve[0].get("vacancy_rate", 0) if vacancy_curve else 0
        total_units = sum(c["unit_count"] for c in cohorts)
        print(f"  {len(cohorts)} unit cohorts, {total_units} total units")
        print(f"  {len(opex)} opex categories")
        print(f"  Physical vacancy: {vacancy:.1%}")
    else:
        print("  Canonical payload loaded without document-ingestion summary fields.")

    # Report populated sections
    sections = []
    if "purchase_assumptions" in result:
        pp = result["purchase_assumptions"]["purchase_price"]
        sections.append(f"purchase ${pp:,.0f}")
    if "debt_terms" in result:
        c = result["debt_terms"]["commitment"]
        r = result["debt_terms"]["rate"]
        sections.append(f"debt ${c:,.0f} @ {r:.1%}")
    if "exit_assumptions" in result:
        exit_assumptions = result["exit_assumptions"]
        ec = exit_assumptions.get("exit_cap_rate")
        if isinstance(ec, (int, float)) and not isinstance(ec, bool):
            sections.append(f"exit {exit_assumptions.get('exit_month')} at {ec:.2%} cap")
        else:
            sections.append(f"exit {exit_assumptions.get('exit_month')} (cap rate unresolved)")
    if "fund_assumptions" in result:
        sp = result["fund_assumptions"]["sponsor_equity_pct"]
        sections.append(f"fund {sp:.0%} GP")
    property_tax_policy = (
        result.get("metadata", {})
        .get("property_summary", {})
        .get("property_tax_policy")
    )
    if isinstance(property_tax_policy, dict):
        mills = property_tax_policy.get("millage_rate_mills")
        if isinstance(mills, (int, float)) and not isinstance(mills, bool):
            sections.append(f"property tax {mills:g} mills per $1,000")

    if sections:
        print(f"  Deal economics: {', '.join(sections)}")
    else:
        print("\n  Note: No deal economics provided. Add --purchase-price, --ltv, --rate,")
        print("        --amort, --exit-cap, --sponsor-pct for engine-runnable JSON.")

    gate = result.get("metadata", {}).get("ingest_gate")
    if isinstance(gate, dict):
        recon = gate.get("t12_reconciliation") or {}
        for side in ("opex", "income"):
            entry = recon.get(side) or {}
            if entry.get("statement_total") is None:
                print(f"  T12 {side}: {entry.get('status', 'UNVERIFIED')} (no statement total found)")
            else:
                print(
                    f"  T12 {side}: {entry['status']} parsed ${entry['parsed_total']:,.0f} vs "
                    f"statement ${entry['statement_total']:,.0f}"
                )
        print(f"  Ingest gate: {gate.get('status')}")
        for blocker in gate.get("blockers") or []:
            print(f"    BLOCKER [{blocker.get('code')}] {blocker.get('message')}")
        for assumed in gate.get("assumed_defaults") or []:
            print(f"    ASSUMED [{assumed.get('field')}] {assumed.get('value')} ({assumed.get('source')})")
    entries = (result.get("assumptions") or {}).get("entries") or {}
    review = [key for key, entry in entries.items() if entry.get("review_required")]
    if entries:
        print(f"  Assumptions: {len(entries)} resolved, {len(review)} flagged for review")

    if not args.canonical_json and om_path_resolved:
        confidence = result["metadata"].get("om_extraction_confidence", "unknown")
        print(f"  OM parsed (confidence: {confidence})")

    print(f"\nOutput: {out}")
    if sheet_path is not None:
        print(f"Assumptions sheet: {sheet_path}")

    # Optional validation
    if args.validate:
        from engine.validator import validate_deal
        report = validate_deal(result)
        if report.status == "PASS":
            warnings = [i for i in report.issues if i.severity == "WARNING"]
            print(f"\n  Validation: PASS ({len(warnings)} warnings)")
            for w in warnings[:5]:
                print(f"    WARN [{w.code}] {w.path}: {w.message}")
        else:
            errors = [i for i in report.issues if i.severity == "ERROR"]
            warnings = [i for i in report.issues if i.severity == "WARNING"]
            print(f"\n  Validation: FAIL ({len(errors)} errors, {len(warnings)} warnings)")
            for e in errors[:10]:
                print(f"    ERROR [{e.code}] {e.path}: {e.message}")
            if any(e.code == PROPERTY_TAX_MILLAGE_MISSING for e in errors):
                print("    Fix: pass --millage <combined mills per $1,000> [--assessment-ratio R]")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
