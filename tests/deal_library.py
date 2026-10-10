"""Synthetic deal library helpers (ADR 0003 §9, slice 8).

Each deal in ``tests/fixtures/deals/<slug>/`` is a rent roll, a T12 and a
``deal.json`` (built by ``tests/fixtures/deals/build_fixtures.py``). These
helpers turn a deal into an engine-runnable canonical the way an analyst
would, with no hand edits:

1. ``build_deal_from_documents`` ingests the rent roll and T12 with the deal
   profile, the deal economics and the deal's synthetic market data;
2. the deal's ``analyst_inputs`` (the property-tax millage and, for a
   value-add deal, its renovation program) are merged and re-resolved exactly
   as ``runs/ingest_deal.py --partial-json`` does.

``synthetic_registry`` builds the ``SyntheticProvider`` registry a deal's
market data describes. All data is invented.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Optional

from engine.assumptions.sheet import sheet_rows
from engine.ingest.ingestion_pipeline import build_deal_from_documents
from engine.market_data import Geo, MarketDataRegistry, SyntheticProvider
from runs.ingest_deal import apply_partial_and_reresolve

DEALS_DIR = Path(__file__).resolve().parent / "fixtures" / "deals"
DEAL_SLUGS = tuple(sorted(p.name for p in DEALS_DIR.iterdir() if (p / "deal.json").is_file()))
GOLDEN_NAME = "expected_assumptions.json"
REGENERATE_HINT = "python tests/fixtures/deals/build_fixtures.py --update-goldens"


def load_deal(slug: str) -> dict[str, Any]:
    with open(DEALS_DIR / slug / "deal.json", encoding="utf-8") as handle:
        deal = json.load(handle)
    deal["_dir"] = DEALS_DIR / slug
    return deal


def deal_geo(deal: dict[str, Any], level: str) -> Geo:
    """The ``Geo`` a market observation is stored under: ``msa`` or ``submarket``."""
    profile = deal["deal_profile"]
    if level == "msa":
        return Geo(metro_slug=profile["metro_slug"])
    if level == "submarket":
        return Geo(metro_slug=profile["metro_slug"], submarket=profile["submarket"])
    raise ValueError(f"unknown market-data level {level!r}")


def synthetic_registry(deal: dict[str, Any]) -> Optional[MarketDataRegistry]:
    """A ``SyntheticProvider`` registry with the deal's market data, or None when it has none."""
    market = deal.get("market_data")
    if not market:
        return None
    data = {
        (obs["metric"], deal_geo(deal, obs["level"])): obs["value"]
        for obs in market["observations"]
    }
    provider = SyntheticProvider(
        data,
        name=market["provider"],
        as_of=date.fromisoformat(market["as_of"]),
        confidence=market["confidence"],
    )
    return MarketDataRegistry([provider])


def ingest_documents(deal: dict[str, Any], *, market: Any = "deal") -> dict[str, Any]:
    """Step 1: ingest the rent roll and T12 with the profile, economics and market data."""
    registry = synthetic_registry(deal) if market == "deal" else market
    profile = deal["deal_profile"]
    economics = deal["economics"]
    folder: Path = deal["_dir"]
    return build_deal_from_documents(
        property_id=deal["property_id"],
        rent_roll_path=folder / deal["documents"]["rent_roll"],
        t12_path=folder / deal["documents"]["t12"],
        analysis_start=deal["analysis"]["start"],
        analysis_end=deal["analysis"]["end"],
        analyst="Synthetic Library",
        purchase_price=economics["purchase_price"],
        ltv=economics["ltv"],
        loan_rate=economics["loan_rate"],
        amort_years=economics["amort_years"],
        io_months=economics["io_months"],
        exit_cap_rate=economics["exit_cap_rate"],
        exit_month=economics["exit_month"],
        sale_cost_pct=economics["sale_cost_pct"],
        year_built=profile["year_built"],
        deal_type=profile["deal_type"],
        msa=profile["msa"],
        submarket=profile["submarket"],
        metro_slug=profile["metro_slug"],
        market=registry,
    )


def ingest(deal: dict[str, Any], *, market: Any = "deal") -> dict[str, Any]:
    """Steps 1 and 2: the engine-runnable canonical for a library deal."""
    registry = synthetic_registry(deal) if market == "deal" else market
    canonical = ingest_documents(deal, market=registry)
    return apply_partial_and_reresolve(canonical, deal["analyst_inputs"], market=registry)


def golden_path(slug: str) -> Path:
    return DEALS_DIR / slug / GOLDEN_NAME


def golden_text(canonical: dict[str, Any]) -> str:
    """The committed snapshot: the assumptions sheet rows, as stable JSON."""
    return json.dumps(sheet_rows(canonical), indent=2, ensure_ascii=False) + "\n"
