#!/usr/bin/env python3
"""CLI: check what market data is available for a geography, in one command.

Usage:
    python runs/market_data_doctor.py --msa 19100 --metro-slug dallas_tx \\
      --submarket "Some Submarket" [--star-tier 3] [--verbose] [--json]

It builds the same registry ingest uses (engine.market_data.default_registry:
environment, then the user config file, then sibling checkouts) and the same
geography the deal profile flags give (--msa is a five-digit CBSA code or an
MSA name, as on runs/ingest_deal.py), then prints:

- the user config file and where the provider order came from;
- each provider: available or not and why, its root and where that root
  came from, whether the geography matched, and the metrics it supplies
  (--verbose adds the inventory and the geography match details);
- each assumption slot that reads market data: supplied (by which provider,
  at submarket or MSA level) or FELL BACK;
- for each feeder that supplied nothing, the feeder-repository steps that
  produce the files it reads.

--json prints the status block instead (the same block ingest records as
assumptions.market_data_status).

Exit codes: 0 when at least one slot is supplied by market data, 1 when none
is, 2 for bad arguments (no geography, or an unknown provider name in
PLAT_MARKET_DATA_PROVIDERS / the config file).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.assumptions.profile import STAR_TIERS, DealProfile  # noqa: E402
from engine.market_data.diagnostics import market_data_status, status_lines  # noqa: E402

EXIT_OK = 0
EXIT_NOTHING_SUPPLIED = 1
EXIT_BAD_INPUT = 2

# What each feeder needs before this repository can read it (see the README's
# Market data section). The feeders' output directories are git-ignored, so a
# fresh clone has none of these files.
NEXT_STEPS: dict[str, list[str]] = {
    "plat_market_study_agent": [
        "CoStar submarket parquet (vacancy, stabilized vacancy, concessions, cap rate, rents, supply): "
        "export CoStar Market Analytics submarket data (licensed) and, in plat-market-study-agent, run "
        "`uv run python etl/transform_costar_excel.py --input <export.xlsx|csv> --out-dir "
        "data/paid/costar-submarket` (omit --metro-slug for a multi-metro export).",
        "Ensemble rent-growth forecast: in plat-market-study-agent, run `uv run python -m "
        "etl.submarket_forecast --config <property config.yaml>` (the config needs notes.metro_slug, "
        "costar_submarket.parquet and outputs.report_path; it writes <report dir>/forecasts/"
        "v2_ensemble_<tier>.csv and model_diagnostics_<tier>.json under reports/).",
        "Comps (effective rent by bed): agents/runners/emit_comp_finder_envelope.py writes "
        "<deal_root>/outputs/<run_id>/comps/comps.json; point PLAT_MARKET_STUDY_COMPS (or "
        "comps_paths in the user config) at it.",
    ],
    "plat_supply_demand": [
        "Metro rent-growth forecast: in plat-supply-demand, run `econ forecast --metro <metro_id>` "
        "(metro_id from econometrics/config/metros/*.yaml, whose msa_code is the CBSA). It reads "
        "data/costar/processed/<costar_market_key>.csv (a licensed CoStar metro export) and fetches "
        "FRED/BLS/migration data (FRED_API_KEY, BLS_API_KEY in .env; --no-fetch skips it), and writes "
        "data/econometrics/forecasts/<metro_id>/<metro_id>_forecast_<vintage>.parquet.",
    ],
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Show which market data is available for a geography.")
    p.add_argument("--msa", default=None, help="MSA name or five-digit CBSA code (as on ingest)")
    p.add_argument("--metro-slug", default=None, help="Metro slug used by the providers, e.g. a metro_id")
    p.add_argument("--submarket", default=None, help="Submarket name")
    p.add_argument("--star-tier", default=None, choices=list(STAR_TIERS), help="CoStar-style star tier")
    p.add_argument("--verbose", action="store_true", help="Also print each provider's inventory and match")
    p.add_argument("--json", action="store_true", help="Print the status block as JSON")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    from engine.assumptions.curves import profile_geo
    from engine.market_data import default_registry

    profile = DealProfile.from_inputs(
        deal_type="stabilized", msa=args.msa, submarket=args.submarket, metro_slug=args.metro_slug,
    )
    if args.star_tier:
        from dataclasses import replace

        profile = replace(profile, star_tier=args.star_tier)
    geo = profile_geo(profile)
    if geo is None:
        print("Error: give a geography: --msa, --metro-slug and/or --submarket.", file=sys.stderr)
        return EXIT_BAD_INPUT
    try:
        registry = default_registry()
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    status = market_data_status(registry, geo)
    supplied = any(slot["supplied"] for slot in status["slots"].values())
    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True))
        return EXIT_OK if supplied else EXIT_NOTHING_SUPPLIED

    print(f"Market data doctor: {', '.join(f'{k}={v}' for k, v in status['geography'].items())}")
    for line in status_lines(status, indent="", verbose=args.verbose):
        print(line)
    print("\nMetrics:")
    for metric, records in sorted(status["metrics"].items()):
        if records:
            first = records[0]
            more = f" (+{len(records) - 1} more)" if len(records) > 1 else ""
            print(f"  {metric}: {first['provider']} at {first['level']} level, as of {first['as_of']}{more}")
        else:
            print(f"  {metric}: none")
    missing = [r for r in status["providers"] if not r.get("metrics_supplied") and r["name"] in NEXT_STEPS]
    if missing:
        print("\nNext steps (in the feeder repositories; this repository never writes their files):")
        for report in missing:
            print(f"  {report['name']} ({report.get('root')}):")
            for step in NEXT_STEPS[report["name"]]:
                print(f"    - {step}")
        print("  Then point this repository at the feeders: PLAT_MARKET_STUDY_ROOT / PLAT_SUPPLY_DEMAND_ROOT, "
              "or market_study_root / supply_demand_root under [market_data] in the user config file.")
    return EXIT_OK if supplied else EXIT_NOTHING_SUPPLIED


if __name__ == "__main__":
    raise SystemExit(main())
