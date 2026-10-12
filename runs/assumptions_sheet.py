#!/usr/bin/env python3
"""CLI: export and apply the assumptions sheet (ADR 0003 §8).

Usage:
    # Export the sheet of a resolved canonical (JSON and/or an Excel tab):
    python runs/assumptions_sheet.py export output/deal.json \\
      --json output/deal.assumptions.json --xlsx output/deal.assumptions.xlsx

    # Re-resolve the canonical with the edited sheet as analyst overrides:
    python runs/assumptions_sheet.py apply output/deal.json \\
      output/deal.assumptions.xlsx --output output/deal.reresolved.json [--no-market-data] \\
      [--editor "A. Analyst"] [--stamp-date | --edit-date 2026-10-12]

    # Write the assumption register page (assumptions.html):
    python runs/assumptions_sheet.py html output/deal.json [-o output/deal.assumptions.html]

``export`` writes ``<canonical stem>.assumptions.json`` next to the canonical
when neither --json nor --xlsx is given. ``apply`` accepts an edited
``assumptions.json``, a plain ``{key: value | {"value", "note"} | null}``
overrides file, or an ``.xlsx`` with an edited "Assumptions" tab. The sheet
carries each row's override as exported (Exported Override), and each row is
read against it:

- an Override changed from the export becomes the analyst override (it wins);
- with the Override left as exported, an edited Value becomes the override, so
  editing the Value column always works;
- a blanked Override on a row that had one clears that override (``null``):
  the entry goes back to its resolver default.

An overridden entry keeps its ``default_value``. ``apply`` also writes the
re-resolved sheet as ``<output stem>.assumptions.json`` and the assumption
register as ``<output stem>.assumptions.html``.

``--editor NAME`` records who made the edits applied in this run
(``edited_by`` on each new or changed override; an override left as it was
keeps its recorded editor). ``--edit-date YYYY-MM-DD`` records the date
(``edited_at``), and ``--stamp-date`` uses today's date: the only place the
clock is read, and only when asked. Resolution itself never reads it.

``html`` writes the assumption register (every assumption, its default and
why, and the user's edits) as a self-contained page, by default
``<canonical stem>.assumptions.html`` next to the canonical.

Like ``runs/ingest_deal.py``, ``apply`` re-resolves with the market-data
registry (``engine.market_data.default_registry()``) unless
``--no-market-data`` is given. Either way, a cleared override goes back to
its full default chain: without a registry, the market-informed default
recorded at the last resolution (``default_value`` and its market
candidates) is kept.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.assumptions import (  # noqa: E402
    AssumptionOverrideError,
    UnsupportedDealTypeError,
    resolve_assumptions,
)
from engine.assumptions.register_html import (  # noqa: E402
    register_html_path_for,
    write_register_html,
)
from engine.assumptions.sheet import (  # noqa: E402
    has_assumptions,
    load_assumption_overrides,
    sheet_path_for,
    stamp_overrides,
    write_assumptions_json,
    write_assumptions_xlsx,
)
from engine.ingest.ingestion_pipeline import sync_ingest_gate_assumptions  # noqa: E402


def _load_canonical(path: Path) -> dict:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return data


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Export or apply the assumptions sheet (ADR 0003 §8).")
    sub = p.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export", help="Write the assumptions sheet of a resolved canonical.")
    export.add_argument("canonical", type=Path, help="Canonical deal JSON with an assumptions section")
    export.add_argument("--json", type=Path, default=None, help="assumptions.json output path")
    export.add_argument("--xlsx", type=Path, default=None, help="Workbook output path (Assumptions tab)")

    apply = sub.add_parser("apply", help="Re-resolve a canonical with an edited sheet as overrides.")
    apply.add_argument("canonical", type=Path, help="Canonical deal JSON")
    apply.add_argument("sheet", type=Path, help="Edited assumptions .json or .xlsx")
    apply.add_argument("--output", "-o", type=Path, required=True, help="Re-resolved canonical JSON path")
    apply.add_argument("--no-market-data", action="store_true",
                       help="Re-resolve without market data. By default the market-data registry "
                            "(engine.market_data.default_registry) is used; market-sourced values "
                            "already in the canonical are kept either way.")
    apply.add_argument("--editor", default=None, metavar="NAME",
                       help="Record NAME as edited_by on each new or changed override.")
    when = apply.add_mutually_exclusive_group()
    when.add_argument("--edit-date", default=None, metavar="YYYY-MM-DD",
                      help="Record this date as edited_at on each new or changed override.")
    when.add_argument("--stamp-date", action="store_true",
                      help="Record today's date as edited_at on each new or changed override.")

    html = sub.add_parser("html", help="Write the assumption register page (assumptions.html).")
    html.add_argument("canonical", type=Path, help="Canonical deal JSON with an assumptions section")
    html.add_argument("--output", "-o", type=Path, default=None,
                      help="HTML output path (default: <canonical stem>.assumptions.html next to it)")
    return p


def _export(args: argparse.Namespace) -> int:
    canonical = _load_canonical(args.canonical)
    if not has_assumptions(canonical):
        print(f"Error: {args.canonical} has no assumptions section; run ingest first.", file=sys.stderr)
        return 2
    json_path = args.json
    if json_path is None and args.xlsx is None:
        json_path = sheet_path_for(args.canonical)
    if json_path is not None:
        print(f"Assumptions sheet: {write_assumptions_json(canonical, json_path)}")
    if args.xlsx is not None:
        print(f"Assumptions workbook: {write_assumptions_xlsx(canonical, args.xlsx)}")
    return 0


def _apply(args: argparse.Namespace) -> int:
    canonical = _load_canonical(args.canonical)
    if not args.sheet.exists():
        print(f"Error: assumptions sheet not found: {args.sheet}", file=sys.stderr)
        return 1
    try:
        overrides = load_assumption_overrides(args.sheet)
    except (KeyError, ValueError) as exc:
        print(f"Error: failed to read the assumptions sheet: {exc}", file=sys.stderr)
        return 1
    edited_at = date.today().isoformat() if args.stamp_date else args.edit_date
    if args.no_market_data:
        market = None
    else:
        from engine.market_data import default_registry

        market = default_registry()
    try:
        overrides = stamp_overrides(overrides, canonical, edited_by=args.editor, edited_at=edited_at)
        result = resolve_assumptions(canonical, overrides=overrides, market=market)
    except (UnsupportedDealTypeError, AssumptionOverrideError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    sync_ingest_gate_assumptions(result)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
    sheet = write_assumptions_json(result, sheet_path_for(args.output))
    page = write_register_html(result, register_html_path_for(args.output))
    cleared = [key for key, override in overrides.items() if override is None]
    applied = [key for key in overrides if key not in cleared]
    print(f"Applied {len(applied)} override(s): {', '.join(applied) or 'none'}")
    if cleared:
        print(f"Cleared {len(cleared)} override(s): {', '.join(cleared)}")
    print(f"Output: {args.output}")
    print(f"Assumptions sheet: {sheet}")
    print(f"Assumption register: {page}")
    return 0


def _html(args: argparse.Namespace) -> int:
    canonical = _load_canonical(args.canonical)
    if not has_assumptions(canonical):
        print(f"Error: {args.canonical} has no assumptions section; run ingest first.", file=sys.stderr)
        return 2
    path = args.output if args.output is not None else register_html_path_for(args.canonical)
    print(f"Assumption register: {write_register_html(canonical, path)}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "export":
            return _export(args)
        if args.command == "html":
            return _html(args)
        return _apply(args)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
