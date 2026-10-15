"""Stabilized vacancy built from evidence (ADR 0003 decision o, UPL-108).

The vacancy floor is case by case: it depends on submarket occupancy, future
deliveries and stabilization at the asset. ``build_stabilized_vacancy`` turns
that evidence into the stabilized vacancy the ``physical_vacancy`` curve
glides to (``curves._vacancy_default``):

``stabilized = market base + asset-vs-market carry``, and for the lease-up
years (``HOUSE_SUPPLY_ADJUSTMENT_YEARS``) ``stabilized + supply adjustment``.

- **Market base**: the submarket ``vacancy_stabilized`` for the asset's star
  tier, else the metro aggregate, else the house vintage default
  max(typical-band midpoint, vintage floor). The level and the star tier
  matched are disclosed.
- **Supply adjustment**: ``supply_pipeline`` (net deliveries over the next
  ``HOUSE_SUPPLY_HORIZON_MONTHS`` / inventory) above ``supply_pipeline_norm``
  (the trailing long-run pace over the same span) adds
  ``HOUSE_SUPPLY_VACANCY_BPS_PER_PCT`` per point of inventory, capped at
  ``HOUSE_SUPPLY_VACANCY_CAP``. No supply data: no adjustment, and the note
  says what the feeder must emit.
- **Asset vs market**: ``HOUSE_ASSET_GAP_CARRY_SHARE`` of the gap between the
  asset's T12 vacancy loss / GPR and the market's stabilized vacancy over the
  trailing year, capped at +/- ``HOUSE_ASSET_GAP_CAP``, after stabilization
  effects are separated: units offline (down, model, admin, renovation) from
  the house box score when the T12 does not book them apart, and no carry at
  all for an asset still in lease-up during its T12. Notice and pre-leased
  units are disclosed; they describe today, not the trailing year.

The rent roll's current vacancy sets year 1 only. The pricing floor is the
house vintage floor alone; it is recorded on the build so the register shows
it, but this module never floors the build.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from engine import underwriting_policy as house
from engine.assumptions.curves import (
    MarketPick,
    _deferred,
    _observation_candidate,
    _r,
    house_stabilized_vacancy,
    market_observations,
    market_pick,
    profile_geo,
)
from engine.assumptions.registry import SOURCE_DEAL_RENT_ROLL, SOURCE_DEAL_T12, house_vintage_source
from engine.assumptions.resolver import _Context, _num
from engine.market_data.types import Observation

METHOD = "house:stabilized_vacancy_model (ADR 0003 decision o)"
# Rent-roll statuses of units off the market: not vacancy the market would see.
OFFLINE_STATUSES = ("Down", "Model", "Admin", "Renovation", "Offline", "Non-Revenue", "Non Revenue", "Employee")
SUPPLY_METRICS = ("supply_pipeline", "supply_pipeline_norm")
NO_PIPELINE_NOTE = (
    "No provider supplied supply_pipeline (net deliveries over the next 24 months / inventory) for this "
    "geography, so there is no supply adjustment. The market-study feeder's CoStar submarket parquet serves it "
    "(forecast-quarter deliveries over inventory_units, else under_construction); plat-supply-demand would need "
    "to persist a deliveries forecast."
)
NO_NORM_NOTE = (
    "No provider supplied supply_pipeline_norm (the trailing long-run delivery pace over 24 months) for this "
    "geography, so the deliveries cannot be compared with a normal pace: no supply adjustment."
)


@dataclass(frozen=True)
class VacancyBuild:
    stabilized: float
    supply_adjustment: float
    supply_months: int
    basis: str
    detail: dict[str, Any]
    as_of: Optional[str]
    confidence: Optional[str]


def _pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.2%}"


def _signed(value: float) -> str:
    return f"{value:+.2%}"


# ── market base ──────────────────────────────────────────────────────────


def _star_tier_requested(ctx: _Context) -> Optional[str]:
    geo = profile_geo(ctx.profile)
    return geo.star_tier if geo else None


def market_base(ctx: _Context) -> Optional[dict[str, Any]]:
    """The stabilized vacancy of the asset's market: submarket, else metro, else house."""
    pick = market_pick(ctx, "vacancy_stabilized")
    if pick is not None:
        observation = pick.observation
        value = _r(observation.scalar)
        level = "submarket" if pick.level == "submarket" else "metro"
        trailing = _num((observation.attributes or {}).get("trailing_4q_mean"))
        requested = (observation.attributes or {}).get("submarket_requested")
        where = (
            f"submarket {observation.geo.submarket}" if level == "submarket"
            else "metro aggregate" + (f" (submarket {requested!r} not found)" if requested else "")
        )
        tier = observation.geo.star_tier
        return {
            "value": value,
            "level": level,
            "source": pick.source,
            "provider": str(observation.provenance.provider),
            "as_of": pick.as_of,
            "confidence": observation.confidence,
            "star_tier_requested": _star_tier_requested(ctx),
            "star_tier_matched": tier,
            "comparator": _r(trailing) if trailing is not None else value,
            "comparator_basis": (
                "the market's stabilized vacancy averaged over its last four actual quarters (trailing year)"
                if trailing is not None else "the market's latest stabilized vacancy"
            ),
            "basis": (
                f"{where} stabilized vacancy {value:.2%}" + (f" ({tier})" if tier else "")
                + f" from {observation.provenance.provider}" + (f" as of {pick.as_of}" if pick.as_of else "")
            ),
            "_pick": pick,
        }
    house_value = house_stabilized_vacancy(ctx)
    if house_value is None:
        return None
    return {
        "value": house_value[0],
        "level": "house",
        "source": house_vintage_source(ctx.bucket),
        "provider": None,
        "as_of": None,
        "confidence": "medium",
        "star_tier_requested": _star_tier_requested(ctx),
        "star_tier_matched": None,
        "comparator": None,
        "basis": f"no submarket or metro stabilized vacancy; {house_value[1]}",
    }


# ── supply ───────────────────────────────────────────────────────────────


def _norm_for(ctx: _Context, pipeline: MarketPick) -> Optional[Observation]:
    """The norm at the pipeline's level (same provider first), else any norm."""
    observations = market_observations(ctx, "supply_pipeline_norm")
    at_level = [o for o in observations if bool(o.geo.submarket) == bool(pipeline.observation.geo.submarket)]
    same_provider = [o for o in at_level if o.provenance.provider == pipeline.observation.provenance.provider]
    for group in (same_provider, at_level, observations):
        if group:
            return group[0]
    return None


def supply_adjustment(ctx: _Context) -> dict[str, Any]:
    """Extra vacancy for the lease-up years from deliveries above the normal pace."""
    years = int(house.HOUSE_SUPPLY_ADJUSTMENT_YEARS)
    out: dict[str, Any] = {
        "available": False,
        "value": 0.0,
        "bps": 0.0,
        "years": years,
        "months": 0,
        "horizon_months": int(house.HOUSE_SUPPLY_HORIZON_MONTHS),
        "bps_per_pct": float(house.HOUSE_SUPPLY_VACANCY_BPS_PER_PCT),
        "cap": float(house.HOUSE_SUPPLY_VACANCY_CAP),
        "capped": False,
    }
    pipeline = market_pick(ctx, "supply_pipeline")
    if pipeline is None:
        out["note"] = NO_PIPELINE_NOTE
        return out
    deliveries = _r(pipeline.observation.scalar)
    out.update({
        "deliveries_pct_inventory": deliveries,
        "deliveries_source": pipeline.source,
        "level": "submarket" if pipeline.level == "submarket" else "metro",
        "as_of": pipeline.as_of,
        "deliveries_basis": (pipeline.observation.attributes or {}).get("basis"),
    })
    norm = _norm_for(ctx, pipeline)
    if norm is None:
        out["note"] = NO_NORM_NOTE
        return out
    normal = _r(norm.scalar)
    excess = _r(max(0.0, deliveries - normal))
    bps = round(excess * 100 * float(house.HOUSE_SUPPLY_VACANCY_BPS_PER_PCT), 4)
    raw = _r(bps / 10_000)
    cap = float(house.HOUSE_SUPPLY_VACANCY_CAP)
    value = min(raw, cap)
    out.update({
        "available": True,
        "normal_pct_inventory": normal,
        "normal_level": "submarket" if norm.geo.submarket else "metro",
        "excess_pct_inventory": excess,
        "bps": bps,
        "value": _r(value),
        "capped": raw > cap,
        "months": years * 12 if value > 0 else 0,
    })
    if value > 0:
        out["note"] = (
            f"Deliveries of {deliveries:.2%} of inventory over the next {out['horizon_months']} months vs a normal "
            f"{normal:.2%}: {excess * 100:.2f} points of excess x {out['bps_per_pct']:g} bp = {bps:g} bp"
            + (f", capped at {cap:.2%}" if raw > cap else "") + f", for years 1-{years}."
        )
    else:
        out["note"] = (
            f"Deliveries of {deliveries:.2%} of inventory over the next {out['horizon_months']} months are at or "
            f"below the normal {normal:.2%}: no supply adjustment (below-normal supply earns no credit)."
        )
    return out


# ── stabilization effects and the asset-vs-market gap ────────────────────


def stabilization_exclusions(ctx: _Context) -> dict[str, Any]:
    """Units off the market, lease-up, and notice / pre-leased units at the asset."""
    from engine.assumptions.guardrails import status_count

    metadata = ctx.canonical.get("metadata") or {}
    box = ((metadata.get("property_summary") or {}).get("house_box_score")) or {}
    counts = box.get("status_counts") or {}
    t12 = ctx.evidence.get("t12") or {}
    gpr = _num(t12.get("gross_potential_rent"))
    non_revenue = _num(t12.get("non_revenue_loss"))
    t12_non_revenue_rate = _r(non_revenue / gpr) if non_revenue is not None and gpr else None

    out: dict[str, Any] = {
        "offline_units": None,
        "offline_share": None,
        "offline_source": None,
        "offline_applied": False,
        "t12_non_revenue_rate": t12_non_revenue_rate,
        "lease_up": False,
        "notice_units": None,
        "pre_leased_vacant_units": len((ctx.evidence.get("rent_roll") or {}).get("pre_leased_vacant_units") or []),
    }
    total = int(box.get("total_units") or ctx.units or 0)
    if counts and total:
        offline = status_count(counts, *OFFLINE_STATUSES)
        out.update({
            "offline_units": offline,
            "offline_share": _r(offline / total),
            "offline_source": "house_box_score.status_counts",
            "notice_units": status_count(counts, "Notice"),
        })
        # A T12 that books down and model units on their own lines already keeps them out of vacancy.
        out["offline_applied"] = bool(offline) and t12_non_revenue_rate is None
    start_year = int(ctx.start[:4]) if len(ctx.start) == 7 else None
    if ctx.year_built is not None and start_year is not None:
        out["lease_up"] = start_year - ctx.year_built < int(house.HOUSE_LEASE_UP_YEARS)
    gaps = []
    if not counts:
        gaps.append("the rent roll carries no per-status unit counts (house box score), so offline units are "
                    "not separated unless the T12 books them on their own lines")
    out["note"] = (
        "Offline units are taken out of the T12 vacancy before it is compared with the market; an asset built "
        f"within {house.HOUSE_LEASE_UP_YEARS} years of the analysis start was in lease-up and carries no gap. "
        "Units on notice and pre-leased units are disclosed only: they describe today's roll (year 1), not the trailing "
        "year." + (f" Gap: {'; '.join(gaps)}." if gaps else "")
    )
    return out


def asset_vs_market(ctx: _Context, base: Mapping[str, Any], exclusions: Mapping[str, Any]) -> dict[str, Any]:
    """A capped share of the asset's T12 gap to the market's stabilized vacancy."""
    share = float(house.HOUSE_ASSET_GAP_CARRY_SHARE)
    cap = float(house.HOUSE_ASSET_GAP_CAP)
    out: dict[str, Any] = {"available": False, "value": 0.0, "carried_share": share, "cap": cap, "capped": False}
    if base.get("level") == "house":
        out["note"] = ("No submarket or metro stabilized vacancy to compare the asset with: no asset-vs-market "
                       "adjustment.")
        return out
    t12 = ctx.evidence.get("t12") or {}
    gpr, vacancy = _num(t12.get("gross_potential_rent")), _num(t12.get("vacancy_loss"))
    if not gpr or gpr <= 0 or vacancy is None:
        out["note"] = "The T12 shows no vacancy loss line (or no gross potential rent): no asset-vs-market adjustment."
        return out
    asset = _r(vacancy / gpr)
    stabilized_asset = asset
    if exclusions.get("offline_applied"):
        stabilized_asset = _r(max(0.0, asset - float(exclusions["offline_share"])))
    comparator = float(base["comparator"])
    out.update({
        "asset_vacancy": asset,
        "asset_vacancy_basis": "T12 vacancy loss / gross potential rent",
        "asset_vacancy_stabilized": stabilized_asset,
        "asset_source": SOURCE_DEAL_T12,
        "market_comparator": comparator,
        "comparator_basis": base.get("comparator_basis"),
    })
    if exclusions.get("lease_up"):
        out["note"] = ("The asset was in lease-up during its T12 (built within "
                       f"{house.HOUSE_LEASE_UP_YEARS} years of the analysis start): no gap is carried.")
        return out
    gap = _r(stabilized_asset - comparator)
    raw = _r(gap * share)
    value = _r(min(cap, max(-cap, raw)))
    out.update({
        "available": True,
        "gap": gap,
        "value": value,
        "capped": abs(raw) > cap,
        "note": (
            f"T12 vacancy {asset:.2%}"
            + (f" less {float(exclusions['offline_share']):.2%} offline = {stabilized_asset:.2%}"
               if exclusions.get("offline_applied") else "")
            + f" vs market {comparator:.2%}: gap {_signed(gap)}, {share:.0%} carried = {_signed(raw)}"
            + (f", capped at {_signed(value)}" if abs(raw) > cap else "") + "."
        ),
    })
    return out


# ── the build ────────────────────────────────────────────────────────────


def _floor(ctx: _Context) -> dict[str, Any]:
    floor = house.default_physical_vacancy_rate(ctx.year_built, strategy=ctx.strategy, deferred=_deferred(ctx))
    return {
        "value": _r(float(floor)) if floor is not None else None,
        "source": house_vintage_source(ctx.bucket) if floor is not None else None,
        "basis": (
            "The pricing floor is the house vintage floor alone (raised when deferred maintenance is flagged): "
            "pricing raises any vacancy segment below it and underwrite warns. The rent roll, T12, broker and "
            "benchmark vacancies are evidence for this build, not floors."
        ),
    }


def _year1(ctx: _Context) -> dict[str, Any]:
    from engine.assumptions.guardrails import rent_roll_unit_counts

    start = _num((ctx.evidence.get("rent_roll") or {}).get("physical_vacancy_rate"))
    return {
        "value": _r(start) if start is not None else None,
        "source": SOURCE_DEAL_RENT_ROLL if start is not None else None,
        "counts": rent_roll_unit_counts(ctx.canonical),
        "basis": (
            "The rent roll's current physical vacancy sets year 1 only; it is evidence, not a floor."
            if start is not None else "No rent-roll vacancy: the curve starts at the stabilized level."
        ),
    }


def supply_candidates(ctx: _Context) -> list[dict[str, Any]]:
    """Every provider's supply observation, recorded as candidates of the vacancy entry."""
    out = []
    for metric in SUPPLY_METRICS:
        pick = market_pick(ctx, metric)
        for observation in market_observations(ctx, metric):
            out.append(_observation_candidate(metric, observation, pick is not None and observation is pick.observation))
    return out


def build_stabilized_vacancy(ctx: _Context) -> Optional[VacancyBuild]:
    """The evidence build, or None when there is neither market data nor a vintage."""
    base = market_base(ctx)
    if base is None:
        return None
    supply = supply_adjustment(ctx)
    exclusions = stabilization_exclusions(ctx)
    gap = asset_vs_market(ctx, base, exclusions)
    stabilized = _r(max(0.0, float(base["value"]) + float(gap["value"])))
    supply_value = float(supply["value"])
    years = int(house.HOUSE_SUPPLY_ADJUSTMENT_YEARS)
    formula = (
        f"stabilized = market base {float(base['value']):.2%} + asset-vs-market carry {_signed(gap['value'])} "
        f"= {stabilized:.2%}; years 1-{years}: + supply {_signed(supply_value)} = {stabilized + supply_value:.2%}"
    )
    pick = base.pop("_pick", None)
    detail = {
        "method": METHOD,
        "formula": formula,
        "year1_start": _year1(ctx),
        "market_base": base,
        "supply": supply,
        "asset_vs_market": gap,
        "stabilization_exclusions": exclusions,
        "stabilized": stabilized,
        "supply_adjustment": supply_value,
        "supply_months": int(supply["months"]),
        "floor": _floor(ctx),
    }
    basis = (
        f"Stabilized {stabilized:.2%} = {base['basis']}"
        + (f", {_signed(gap['value'])} asset-vs-market ({gap['note'].rstrip('.')})" if gap["available"]
           else f"; {gap['note'].rstrip('.')}")
        + "; supply: " + supply["note"]
    )
    return VacancyBuild(
        stabilized=stabilized,
        supply_adjustment=supply_value,
        supply_months=int(supply["months"]),
        basis=basis,
        detail=detail,
        as_of=pick.as_of if pick is not None else None,
        confidence=pick.observation.confidence if pick is not None else "medium",
    )
