#!/usr/bin/env python3
"""CLI: validate and underwrite a canonical deal JSON, and write the run artifacts.

Usage:
    python runs/run_deal.py output/deal.json --output-dir output/deal_run --xlsx

    # Re-resolve with analyst overrides first (ADR 0003 §8):
    python runs/run_deal.py output/deal.json --output-dir output/deal_run \\
      --assumption-overrides output/deal.assumptions.json --no-market-data

Steps:
    1. With --assumption-overrides, re-resolve the assumptions with the
       overrides (a plain {key: value | {"value", "note"}} JSON, an edited
       <stem>.assumptions.json sheet, or an .xlsx with an edited
       "Assumptions" tab). Market data comes from
       engine.market_data.default_registry() unless --no-market-data;
       market-sourced values already in the canonical are kept either way.
    2. Validate. Any ERROR exits 1 with the report (validation.json is
       written either way).
    3. Run the engine (engine.engine.run_underwriting).
    4. Write into --output-dir:
         canonical.json   the exact inputs that were run
         validation.json  the validation report
         results.json     the engine results, with metadata.market_data_status
                          (the canonical's market-data status block: each
                          provider, its root, and every slot that fell back)
         assumptions.json the assumptions sheet (when the canonical has an
                          assumptions section)
         assumptions.html the assumption register: every assumption, its
                          default and why, and the user's edits (same
                          condition)
         underwriting.xlsx with --xlsx: the underwriting workbook, including
                          the "Assumptions" tab
    5. Print the key metrics and the market-data status.

Exit codes: 0 ok, 1 validation failure (including an engine refusal), 2 bad
input (missing or unreadable canonical or overrides, unusable override).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.assumptions import (  # noqa: E402
    AssumptionOverrideError,
    UnsupportedDealTypeError,
    resolve_assumptions,
)
from engine.assumptions.register_html import write_register_html  # noqa: E402
from engine.assumptions.sheet import (  # noqa: E402
    has_assumptions,
    load_assumption_overrides,
    stamp_overrides,
    write_assumptions_json,
)
from engine.ingest.ingestion_pipeline import sync_ingest_gate_assumptions  # noqa: E402
from engine.market_data.diagnostics import disabled_status, status_lines  # noqa: E402

EXIT_OK = 0
EXIT_VALIDATION = 1
EXIT_BAD_INPUT = 2

CANONICAL_NAME = "canonical.json"
VALIDATION_NAME = "validation.json"
RESULTS_NAME = "results.json"
ASSUMPTIONS_NAME = "assumptions.json"
REGISTER_NAME = "assumptions.html"
WORKBOOK_NAME = "underwriting.xlsx"


class BadInput(Exception):
    """An input the run cannot use (exit 2)."""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Validate and underwrite a canonical deal JSON; write results, the "
        "assumptions sheet and (optionally) the underwriting workbook."
    )
    p.add_argument("canonical", type=Path, help="Canonical deal JSON (e.g. from runs/ingest_deal.py)")
    p.add_argument("--output-dir", type=Path, required=True, help="Folder for the run artifacts")
    p.add_argument("--xlsx", action="store_true",
                   help=f"Also write {WORKBOOK_NAME} (summary, pro forma, monthly detail, "
                        "model health and the Assumptions tab)")
    p.add_argument("--assumption-overrides", type=Path, default=None, metavar="PATH",
                   help='Analyst overrides applied by the resolver before the run: {key: value | '
                        '{"value": v, "note": "..."}}, an edited assumptions.json sheet, or an '
                        '.xlsx with an edited "Assumptions" tab.')
    p.add_argument("--no-market-data", action="store_true",
                   help="With --assumption-overrides, re-resolve without the market-data registry.")
    return p


def _load_canonical(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise BadInput(f"canonical JSON not found: {path}")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BadInput(f"failed to read canonical JSON {path}: {exc}") from None
    if not isinstance(data, dict):
        raise BadInput(f"canonical JSON root must be an object: {path}")
    return data


def _apply_overrides(canonical: dict[str, Any], path: Path, *, no_market_data: bool) -> dict[str, Any]:
    if not path.is_file():
        raise BadInput(f"assumption overrides not found: {path}")
    try:
        overrides = load_assumption_overrides(path)
    except (OSError, KeyError, ValueError) as exc:
        raise BadInput(f"failed to load assumption overrides: {exc}") from None
    if no_market_data:
        market = None
    else:
        from engine.market_data import default_registry

        market = default_registry()
    try:
        overrides = stamp_overrides(overrides, canonical)  # keeps recorded edit metadata
        resolved = resolve_assumptions(canonical, overrides=overrides, market=market)
    except (UnsupportedDealTypeError, AssumptionOverrideError, ValueError) as exc:
        raise BadInput(f"failed to apply assumption overrides: {exc}") from None
    print(f"Applied {len(overrides)} assumption override(s): {', '.join(overrides) or 'none'}")
    return sync_ingest_gate_assumptions(resolved)


def _write_json(path: Path, payload: Any) -> Path:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _get(data: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _fmt(value: Any, kind: str) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "n/a"
    if kind == "usd":
        return f"${value:,.0f}"
    if kind == "pct":
        return f"{value:.2%}"
    return f"{value:.2f}x"


def _dscr_basis_label(metrics: dict[str, Any]) -> str:
    from engine.modules.loan_sizing import SHORT_LABELS

    basis = _get(metrics, "dscr", "basis")
    return SHORT_LABELS.get(basis, "NOI before capex")


def key_metrics(results: dict[str, Any]) -> list[tuple[str, Any, str]]:
    """(label, value, kind) for the headline metrics of an engine result."""
    metrics = results.get("metrics") or {}
    return [
        ("NOI (year 1)", _get(metrics, "noi", "year_1_noi"), "usd"),
        ("Unlevered IRR", _get(metrics, "irr", "unlevered_irr"), "pct"),
        ("Levered IRR", _get(metrics, "irr", "levered_irr"), "pct"),
        ("Levered equity multiple", _get(metrics, "equity_multiple", "levered_em"), "x"),
        ("Unlevered equity multiple", _get(metrics, "equity_multiple", "unlevered_em"), "x"),
        (f"Minimum DSCR ({_dscr_basis_label(metrics)})", _get(metrics, "dscr", "minimum_dscr"), "x"),
        ("Minimum DSCR (NOI before capex)", _get(metrics, "dscr", "noi_before_capex", "minimum_dscr"), "x"),
        ("Exit value (gross sale price)", _get(metrics, "exit", "gross_sale_price"), "usd"),
        ("Exit cap rate", _get(metrics, "yields", "exit_cap_rate"), "pct"),
    ]


def _print_issues(report: Any, *, limit: int = 20) -> None:
    errors = [i for i in report.issues if i.severity == "ERROR"]
    warnings = [i for i in report.issues if i.severity == "WARNING"]
    print(f"Validation: {report.status} ({len(errors)} errors, {len(warnings)} warnings)")
    for issue in errors[:limit]:
        print(f"  ERROR [{issue.code}] {issue.path}: {issue.message}")
    for issue in warnings[:5]:
        print(f"  WARN [{issue.code}] {issue.path}: {issue.message}")
    if len(warnings) > 5:
        print(f"  ... {len(warnings) - 5} more warnings in {VALIDATION_NAME}")


def run(args: argparse.Namespace) -> int:
    canonical = _load_canonical(args.canonical)
    if args.assumption_overrides is not None:
        canonical = _apply_overrides(canonical, args.assumption_overrides, no_market_data=args.no_market_data)

    out_dir: Path = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_json(out_dir / CANONICAL_NAME, canonical)

    from engine.validator import validate_deal

    report = validate_deal(canonical)
    _write_json(out_dir / VALIDATION_NAME, report.to_dict())
    _print_issues(report)
    if report.status != "PASS":
        print(f"Refusing to run: validation failed. Report: {out_dir / VALIDATION_NAME}", file=sys.stderr)
        return EXIT_VALIDATION

    from engine.engine import run_underwriting

    try:
        results = run_underwriting(canonical)
    except ValueError as exc:
        if not str(exc).startswith("Validation failed"):
            raise
        print(f"Refusing to run: {exc}", file=sys.stderr)
        return EXIT_VALIDATION

    status = market_data_status_of(canonical)
    results.setdefault("metadata", {})["market_data_status"] = status
    print(f"Results: {_write_json(out_dir / RESULTS_NAME, results)}")
    if has_assumptions(canonical):
        print(f"Assumptions sheet: {write_assumptions_json(canonical, out_dir / ASSUMPTIONS_NAME)}")
        print(f"Assumption register: {write_register_html(canonical, out_dir / REGISTER_NAME)}")
    if args.xlsx:
        from engine.excel_output import write_results_to_workbook

        workbook = out_dir / WORKBOOK_NAME
        write_results_to_workbook(results, workbook, inputs=canonical)
        print(f"Workbook: {workbook}")

    deal_id = _get(canonical, "metadata", "deal_id") or args.canonical.stem
    print(f"\nKey metrics: {deal_id}")
    for label, value, kind in key_metrics(results):
        print(f"  {label:<30} {_fmt(value, kind)}")
    print()
    for line in status_lines(status, indent=""):
        print(line)
    return EXIT_OK


def market_data_status_of(canonical: dict[str, Any]) -> dict[str, Any]:
    """The canonical's recorded market-data status, or a block saying none was recorded."""
    status = _get(canonical, "assumptions", "market_data_status")
    if isinstance(status, dict):
        return status
    return disabled_status(
        "the canonical records no market-data status (resolved with --no-market-data "
        "or before the status block existed)"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except BadInput as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
