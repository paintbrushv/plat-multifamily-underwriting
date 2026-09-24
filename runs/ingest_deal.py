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
      --exit-cap 0.055 --sponsor-pct 0.10 --pref-return 0.08

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
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.ingest.broker_om_snapshot import stable_property_tax_source_locator
from engine.ingest.ingestion_pipeline import build_deal_from_documents


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

    metadata = merged.setdefault("metadata", {})
    partial_metadata = partial.get("metadata", {}) if isinstance(partial.get("metadata"), dict) else {}
    if "purchase_assumptions" in partial and "purchase_assumptions_source" not in partial_metadata:
        metadata["purchase_assumptions_source"] = "analyst_override"
    if any(k in partial for k in ("purchase_assumptions", "debt_terms", "exit_assumptions", "fund_assumptions")):
        metadata["analyst_review_required"] = False
    return merged


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
    p.add_argument("--rent-growth", type=float, default=0.03, help="Annual rent growth rate (default 3%%)")
    p.add_argument("--vacancy", type=float, default=None, help="Vacancy rate override (0-1)")
    p.add_argument("--collection-loss", type=float, default=0.005, help="Collection loss rate (default 0.5%%)")

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

    # Exit assumptions
    p.add_argument("--exit-cap", type=float, default=None,
                   help="Exit cap rate (0-1). Overrides OM if both provided.")
    p.add_argument("--exit-month", type=str, default=None,
                   help="Exit month YYYY-MM (default: analysis end)")
    p.add_argument("--sale-cost", type=float, default=0.02,
                   help="Sale cost percent (default 2%%)")

    # Fund structure
    p.add_argument("--sponsor-pct", type=float, default=None,
                   help="Sponsor (GP) equity percent (e.g. 0.10 for 10%%)")
    p.add_argument("--pref-return", type=float, default=None,
                   help="LP preferred return rate (e.g. 0.08 for 8%%)")
    p.add_argument("--acq-fee", type=float, default=0.01,
                   help="Acquisition fee percent (default 1%%)")
    p.add_argument("--am-fee", type=float, default=0.015,
                   help="Asset management fee percent (default 1.5%%)")
    p.add_argument("--disp-fee", type=float, default=0.01,
                   help="Disposition fee percent (default 1%%)")

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
        print(f"  Period:    {args.start} → {args.end}")

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
        )
        if partial_payload is not None:
            result = _apply_partial_json(result, partial_payload)
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
        ec = result["exit_assumptions"]["exit_cap_rate"]
        sections.append(f"exit cap {ec:.2%}")
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

    if not args.canonical_json and om_path_resolved:
        confidence = result["metadata"].get("om_extraction_confidence", "unknown")
        print(f"  OM parsed (confidence: {confidence})")

    print(f"\nOutput: {out}")

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

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
