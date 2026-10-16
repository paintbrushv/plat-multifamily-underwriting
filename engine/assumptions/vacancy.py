"""Stabilized vacancy built from evidence (ADR 0003 decisions o and p, UPL-108, UPL-109).

The vacancy floor is case by case: it depends on submarket occupancy, future
deliveries and stabilization at the asset. ``build_stabilized_vacancy`` turns
that evidence into the stabilized vacancy the ``physical_vacancy`` curve
glides to (``curves._vacancy_default``):

``stabilized = market base + asset-vs-market carry``, and for the lease-up
years (``HOUSE_SUPPLY_ADJUSTMENT_YEARS``) ``stabilized + supply adjustment``.

- **Market base**: the analyst's submarket benchmark
  (``property_summary.market_vacancy_benchmark``, level ``analyst``), else the
  submarket ``vacancy_stabilized`` for the asset's star tier, else the metro
  aggregate, else the house vintage default max(typical-band midpoint,
  vintage floor). The level and the star tier matched are disclosed.
- **Supply adjustment**: ``supply_pipeline`` (net deliveries over the next
  ``HOUSE_SUPPLY_HORIZON_MONTHS`` / inventory) above ``supply_pipeline_norm``
  (the trailing long-run pace over the same span) adds
  ``HOUSE_SUPPLY_VACANCY_BPS_PER_PCT`` per point of inventory, capped at
  ``HOUSE_SUPPLY_VACANCY_CAP``. No supply data: no adjustment, and the note
  says what the feeder must emit.
- **Asset vs market**: ``HOUSE_ASSET_GAP_CARRY_SHARE`` of the gap between the
  asset's trailing vacancy loss / GPR (the ingested T12, else typed trailing
  actuals, else broker-OM actuals labelled actual: ``asset_statement``) and the
  market's stabilized vacancy over the trailing year, capped at +/- ``HOUSE_ASSET_GAP_CAP``, after stabilization
  effects are separated: units offline (down, model, admin, renovation) from
  the house box score when the T12 does not book them apart, and no carry at
  all for an asset still in lease-up during its T12. Notice and pre-leased
  units are disclosed; they describe today, not the trailing year.

The rent roll's current vacancy sets year 1 only. The pricing floor is the
house vintage floor alone (on a value-add deal, from the month after the last
renovation); it is recorded on the build so the register shows it, but this
module never floors the build. ``reprice_vacancy_build`` folds evidence that
reaches backsolve after the last resolution into the recorded build.
"""

from __future__ import annotations

import re
from copy import deepcopy
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
from engine.assumptions.registry import (
    SOURCE_ANALYST,
    SOURCE_DEAL_OM,
    SOURCE_DEAL_RENT_ROLL,
    SOURCE_DEAL_T12,
    house_vintage_source,
)
from engine.assumptions.resolver import _Context, _num
from engine.market_data.types import Observation

METHOD = "house:stabilized_vacancy_model (ADR 0003 decision o)"
# Rent-roll statuses of units off the market: not vacancy the market would see.
OFFLINE_STATUSES = ("Down", "Model", "Admin", "Renovation", "Offline", "Non-Revenue", "Non Revenue", "Employee",
                    "Uninhabitable", "Vacant Down")
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


def analyst_benchmark(ctx: _Context) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """The analyst-supplied ``property_summary.market_vacancy_benchmark``, and a note when it is unusable."""
    from engine.assumptions.guardrails import market_vacancy_benchmark

    summary = (ctx.canonical.get("metadata") or {}).get("property_summary") or {}
    if summary.get("market_vacancy_benchmark") is None:
        return None, None
    benchmark = market_vacancy_benchmark(ctx.canonical, strict=False)
    if benchmark is None:
        return None, ("The analyst market_vacancy_benchmark is ignored: it needs exactly rate, as_of (YYYY-MM-DD) "
                      "and source.")
    return benchmark, None


def _market_pick_base(ctx: _Context, pick: MarketPick) -> dict[str, Any]:
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


def market_base(ctx: _Context, prior: Optional[Mapping[str, Any]] = None) -> Optional[dict[str, Any]]:
    """The stabilized vacancy of the asset's market.

    The analyst's submarket benchmark (``market_vacancy_benchmark``) outranks
    the feeders (analyst > market, UPL-109); then the submarket, else metro,
    market data; else, when ``prior`` (a recorded build, backsolve's pricing
    rebuild) had a market base, that base; else the house vintage default.
    """
    benchmark, benchmark_note = analyst_benchmark(ctx)
    pick = market_pick(ctx, "vacancy_stabilized")
    if benchmark is not None:
        rate = _r(float(benchmark["rate"]))
        outranked = None
        if pick is not None:
            feeder = _market_pick_base(ctx, pick)
            outranked = {k: feeder[k] for k in ("value", "level", "source", "as_of", "basis")}
        elif isinstance(prior, Mapping) and prior.get("level") in ("submarket", "metro"):
            outranked = {k: prior.get(k) for k in ("value", "level", "source", "as_of", "basis")}
        return {
            "value": rate,
            "level": "analyst",
            "source": SOURCE_ANALYST,
            "provider": None,
            "as_of": benchmark["as_of"],
            "source_note": benchmark["source"],
            "confidence": "high",
            "star_tier_requested": _star_tier_requested(ctx),
            "star_tier_matched": None,
            "comparator": rate,
            "comparator_basis": "the analyst-supplied submarket stabilized vacancy benchmark",
            "outranked": outranked,
            "basis": (
                f"analyst submarket vacancy benchmark {rate:.2%} ({benchmark['source']}, as of {benchmark['as_of']})"
                + (f", ahead of the {outranked['level']} {float(outranked['value']):.2%} from {outranked['source']}"
                   if outranked and outranked.get("value") is not None else "")
            ),
        }
    if pick is not None:
        base = _market_pick_base(ctx, pick)
        if benchmark_note:
            base["analyst_benchmark_note"] = benchmark_note
        return base
    if isinstance(prior, Mapping) and prior.get("level") in ("submarket", "metro"):
        kept = {k: deepcopy(v) for k, v in prior.items() if k != "kept_from_prior_resolution"}
        kept["kept_from_prior_resolution"] = True
        return kept
    house_value = house_stabilized_vacancy(ctx)
    if house_value is None:
        return None
    out = {
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
    if benchmark_note:
        out["analyst_benchmark_note"] = benchmark_note
    return out


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


def supply_adjustment(ctx: _Context, prior: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Extra vacancy for the lease-up years from deliveries above the normal pace.

    Without supply data, ``prior`` (a recorded build's supply record, in
    backsolve's pricing rebuild) is kept.
    """
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
    if pipeline is None and isinstance(prior, Mapping) and prior.get("deliveries_pct_inventory") is not None:
        return {**deepcopy(dict(prior)), "kept_from_prior_resolution": True}
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


_PRO_FORMA = re.compile(r"pro\s*-?\s*forma|projected|projection|forecast|budget|underwrit|stabilized year", re.I)


def _statement_rate(statement: Any) -> Optional[tuple[float, float]]:
    """(GPR, vacancy loss magnitude) of a trailing statement, or None without both."""
    if not isinstance(statement, Mapping):
        return None
    gpr, vacancy = _num(statement.get("gross_potential_rent")), _num(statement.get("vacancy_loss"))
    if not gpr or gpr <= 0 or vacancy is None:
        return None
    return float(gpr), abs(float(vacancy))


def _actual_label_problem(label: Any) -> Optional[str]:
    """Why an OM column label does not establish trailing actuals (None: it does)."""
    if not isinstance(label, Mapping) or label.get("kind") != "actual":
        return ("the OM table carries no actual-period label (trailing_actuals_basis kind 'actual', e.g. a "
                "verified T12 or T3 column header): its trailing column was read by position and may be a broker "
                "projection")
    period = str(label.get("period") or "")
    if _PRO_FORMA.search(period):
        return f"the OM column is labelled {period!r}, a pro forma or projection, not trailing actuals"
    if not period.strip():
        return "the OM actual label names no period"
    return None


def _om_statements(ctx: _Context) -> list[tuple[str, Mapping[str, Any]]]:
    """Broker-OM trailing actuals: the ingested OM, then the broker snapshot backsolve recorded."""
    summary = (ctx.canonical.get("metadata") or {}).get("property_summary") or {}
    out = []
    for where, record in (("ingest_gate.evidence.om_trailing_actuals", ctx.evidence.get("om_trailing_actuals")),
                          ("property_summary.broker_underwriting_snapshot",
                           summary.get("broker_underwriting_snapshot"))):
        if isinstance(record, Mapping) and isinstance(record.get("trailing_actuals"), Mapping):
            out.append((where, record))
    return out


def asset_statement(ctx: _Context) -> dict[str, Any]:
    """The trailing operating statement the asset's vacancy is read from (UPL-109).

    In order: the ingested T12 (``deal:t12``, confidence high); trailing
    actuals typed into backsolve (``property_summary.trailing_actuals``,
    ``deal:t12``, medium); broker-OM trailing actuals the OM labels as actuals
    (``deal:om``, low). A broker pro forma, or an OM column the parser could
    not confirm as actuals, is never used: ``considered`` says why.
    """
    summary = (ctx.canonical.get("metadata") or {}).get("property_summary") or {}
    candidates: list[dict[str, Any]] = [
        {"kind": "ingested_t12", "source": SOURCE_DEAL_T12, "confidence": "high", "where": "ingest_gate.evidence.t12",
         "statement": ctx.evidence.get("t12"), "label": "T12 vacancy loss / gross potential rent",
         "non_revenue_field": "non_revenue_loss"},
        {"kind": "typed_trailing_actuals", "source": SOURCE_DEAL_T12, "confidence": "medium",
         "where": "property_summary.trailing_actuals", "statement": summary.get("trailing_actuals"),
         "label": "typed trailing actuals (backsolve trailing_actuals) vacancy loss / gross potential rent",
         "non_revenue_field": "non_revenue_loss"},
    ]
    for where, record in _om_statements(ctx):
        label = record.get("trailing_actuals_basis")
        period = label.get("period") if isinstance(label, Mapping) else None
        candidates.append({
            "kind": "broker_om_actuals", "source": SOURCE_DEAL_OM, "confidence": "low", "where": where,
            "statement": record.get("trailing_actuals"), "om_label": label, "problem": _actual_label_problem(label),
            "label": f"broker OM trailing actuals ({period or 'unlabelled'}) vacancy loss / gross potential rent",
            "non_revenue_field": "employee_model",
        })
    considered: list[dict[str, Any]] = []
    chosen: Optional[dict[str, Any]] = None
    for candidate in candidates:
        rates = _statement_rate(candidate["statement"])
        if rates is None:
            if candidate["kind"] != "ingested_t12" and isinstance(candidate["statement"], Mapping):
                considered.append({"kind": candidate["kind"], "where": candidate["where"], "used": False,
                                   "reason": "no vacancy loss and gross potential rent"})
            continue
        record = {"kind": candidate["kind"], "source": candidate["source"], "where": candidate["where"],
                  "vacancy": _r(rates[1] / rates[0])}
        if candidate.get("problem"):
            considered.append({**record, "used": False, "reason": candidate["problem"]})
            continue
        if chosen is not None:
            considered.append({**record, "used": False,
                               "reason": f"outranked by the {chosen['kind'].replace('_', ' ')}"})
            continue
        chosen = {**candidate, "gpr": rates[0], "vacancy_loss": rates[1], "vacancy": record["vacancy"]}
        considered.append({**record, "used": True})
    out: dict[str, Any] = {"considered": considered}
    if chosen is not None:
        statement = chosen["statement"]
        non_revenue = _num(statement.get(chosen["non_revenue_field"]))
        out.update({k: chosen[k] for k in ("kind", "source", "confidence", "where", "label", "gpr", "vacancy_loss",
                                            "vacancy")})
        out["non_revenue_loss"] = abs(non_revenue) if non_revenue is not None else None
        if chosen.get("om_label") is not None:
            out["om_label"] = deepcopy(dict(chosen["om_label"]))
    return out


def stabilization_exclusions(ctx: _Context, statement: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Units off the market, lease-up, and notice / pre-leased units at the asset."""
    from engine.assumptions.guardrails import status_count

    metadata = ctx.canonical.get("metadata") or {}
    box = ((metadata.get("property_summary") or {}).get("house_box_score")) or {}
    counts = box.get("status_counts") or {}
    counts_source = "house_box_score.status_counts"
    if not counts:
        # UPL-110 (ADR 0003 decision q): without a box score, the rent roll's modeled
        # down units are the offline units (a capital project, not weak demand).
        from engine.assumptions.down_units import offline_status_counts

        counts, counts_source = offline_status_counts(ctx), "rent_roll.down_units"
    statement = statement if statement is not None else asset_statement(ctx)
    gpr = statement.get("gpr")
    non_revenue = statement.get("non_revenue_loss")
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
            "offline_source": counts_source,
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


def asset_vs_market(
    ctx: _Context,
    base: Mapping[str, Any],
    exclusions: Mapping[str, Any],
    statement: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """A capped share of the asset's trailing-vacancy gap to the market's stabilized vacancy.

    ``statement`` is ``asset_statement``: the ingested T12, else typed trailing
    actuals, else broker-OM actuals labelled as actuals. Its source, kind and
    confidence are disclosed, with every statement considered.
    """
    share = float(house.HOUSE_ASSET_GAP_CARRY_SHARE)
    cap = float(house.HOUSE_ASSET_GAP_CAP)
    statement = statement if statement is not None else asset_statement(ctx)
    out: dict[str, Any] = {"available": False, "value": 0.0, "carried_share": share, "cap": cap, "capped": False}
    if statement.get("considered"):
        out["asset_sources_considered"] = deepcopy(statement["considered"])
    if base.get("level") == "house":
        out["note"] = ("No submarket or metro stabilized vacancy to compare the asset with: no asset-vs-market "
                       "adjustment.")
        return out
    if statement.get("kind") is None:
        skipped = [c for c in statement.get("considered") or [] if not c.get("used")]
        out["note"] = (
            "The T12 shows no vacancy loss line (or no gross potential rent): no asset-vs-market adjustment."
            + "".join(f" The {c['kind'].replace('_', ' ')} ({c['where']}) is not used: {c['reason']}." for c in skipped)
        )
        return out
    asset = _r(float(statement["vacancy"]))
    stabilized_asset = asset
    if exclusions.get("offline_applied"):
        stabilized_asset = _r(max(0.0, asset - float(exclusions["offline_share"])))
    comparator = float(base["comparator"])
    out.update({
        "asset_vacancy": asset,
        "asset_vacancy_basis": statement["label"],
        "asset_vacancy_stabilized": stabilized_asset,
        "asset_source": statement["source"],
        "asset_source_kind": statement["kind"],
        "asset_source_where": statement["where"],
        "asset_confidence": statement["confidence"],
        "market_comparator": comparator,
        "comparator_basis": base.get("comparator_basis"),
    })
    if statement.get("om_label") is not None:
        out["asset_om_label"] = deepcopy(dict(statement["om_label"]))
    asset_name = {"ingested_t12": "T12", "typed_trailing_actuals": "Typed trailing-actuals",
                  "broker_om_actuals": "Broker OM trailing-actuals"}[statement["kind"]]
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
            f"{asset_name} vacancy {asset:.2%}"
            + (f" less {float(exclusions['offline_share']):.2%} offline = {stabilized_asset:.2%}"
               if exclusions.get("offline_applied") else "")
            + f" vs market {comparator:.2%}: gap {_signed(gap)}, {share:.0%} carried = {_signed(raw)}"
            + (f", capped at {_signed(value)}" if abs(raw) > cap else "") + "."
        ),
    })
    return out


# ── the build ────────────────────────────────────────────────────────────


def _floor(ctx: _Context) -> dict[str, Any]:
    from engine.assumptions.guardrails import vacancy_floor_window

    floor = house.default_physical_vacancy_rate(ctx.year_built, strategy=ctx.strategy, deferred=_deferred(ctx))
    out = {
        "value": _r(float(floor)) if floor is not None else None,
        "source": house_vintage_source(ctx.bucket) if floor is not None else None,
        "basis": (
            "The pricing floor is the house vintage floor alone (raised when deferred maintenance is flagged): "
            "pricing raises any vacancy segment below it and underwrite warns. The rent roll, T12, broker and "
            "benchmark vacancies are evidence for this build, not floors."
        ),
    }
    window = vacancy_floor_window(ctx.canonical, strategy=ctx.strategy) if floor is not None else None
    if window:
        out["applies_from"] = window["floor_applies_from"]
        out["renovation_window"] = window
        out["basis"] += " " + window["basis"]
    return out


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


def _asset_candidate(ctx: _Context) -> Optional[dict[str, Any]]:
    """A typed or OM trailing-actuals vacancy candidate (the ingested T12's is recorded by curves)."""
    from engine.assumptions.resolver import _candidate

    statement = asset_statement(ctx)
    if statement.get("kind") in (None, "ingested_t12"):
        return None
    return _candidate(statement["source"], statement["vacancy"],
                      f"{statement['label'][:1].upper()}{statement['label'][1:]}: the asset's trailing vacancy "
                      f"({statement['confidence']} confidence), compared with the market's in the stabilized "
                      "vacancy build.")


def supply_candidates(ctx: _Context) -> list[dict[str, Any]]:
    """Every provider's supply observation, recorded as candidates of the vacancy entry.

    A typed or OM trailing-actuals vacancy the build compares is recorded first.
    """
    out = [c for c in (_asset_candidate(ctx),) if c is not None]
    for metric in SUPPLY_METRICS:
        pick = market_pick(ctx, metric)
        for observation in market_observations(ctx, metric):
            out.append(_observation_candidate(metric, observation, pick is not None and observation is pick.observation))
    return out


_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def _weakest(*levels: Optional[str]) -> Optional[str]:
    known = [level for level in levels if level in _CONFIDENCE_RANK]
    return min(known, key=_CONFIDENCE_RANK.__getitem__) if known else None


def build_stabilized_vacancy(ctx: _Context, prior: Optional[Mapping[str, Any]] = None) -> Optional[VacancyBuild]:
    """The evidence build, or None when there is neither market data nor a vintage.

    ``prior`` is a recorded build (``detail.vacancy_build``): backsolve's
    pricing rebuild reuses its market base and supply record when no market
    data is supplied, so only the evidence pricing adds changes the build.
    """
    prior = prior if isinstance(prior, Mapping) else {}
    base = market_base(ctx, prior.get("market_base"))
    if base is None:
        return None
    supply = supply_adjustment(ctx, prior.get("supply"))
    statement = asset_statement(ctx)
    exclusions = stabilization_exclusions(ctx, statement)
    gap = asset_vs_market(ctx, base, exclusions, statement)
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
    if pick is not None:
        as_of, confidence = pick.as_of, pick.observation.confidence
    elif base["level"] == "house":
        as_of, confidence = None, "medium"
    else:  # the analyst benchmark, or a base kept from the recorded build
        as_of, confidence = base.get("as_of"), base.get("confidence") or "medium"
    if gap["available"]:
        # The build is only as confident as the trailing statement it carries.
        confidence = _weakest(confidence, gap.get("asset_confidence")) or confidence
    return VacancyBuild(
        stabilized=stabilized,
        supply_adjustment=supply_value,
        supply_months=int(supply["months"]),
        basis=basis,
        detail=detail,
        as_of=as_of,
        confidence=confidence,
    )


# ── backsolve: evidence pricing adds (UPL-109) ───────────────────────────


def reprice_vacancy_build(canonical: dict[str, Any], *, strategy: str) -> Optional[dict[str, Any]]:
    """Rebuild the stabilized vacancy IN PLACE when backsolve's evidence changes it.

    Backsolve prices the canonical's vacancy curve, which the last resolution
    wrote. Evidence that reaches pricing afterwards (trailing actuals typed into
    backsolve, a broker snapshot's labelled actuals, an analyst benchmark added
    to ``property_summary``) is folded into the recorded build: its market base
    and supply are kept when no market data is supplied, the asset-vs-market
    gap and the analyst base are re-read, and the curve and the entry are
    rewritten. The returned record says what changed (``rebuilt``) or why
    nothing was rebuilt; None when there is no such evidence.

    An analyst vacancy (an override), a curve that is not the recorded build
    (the house box-score rebase, an edit) or a canonical without a recorded
    build is never rebuilt.
    """
    from engine.assumptions.curves import _VACANCY, _ramp_rows, _ramp_writer, _same_curve, vacancy_choice
    from engine.assumptions.guardrails import STRATEGY_DEAL_TYPE, pricing_profile
    from engine.assumptions.registry import HOUSE_STABILIZED_VACANCY_MODEL
    from engine.assumptions.resolver import _context

    summary = (canonical.get("metadata") or {}).get("property_summary") or {}
    evidence = [name for name, present in (
        ("trailing_actuals", isinstance(summary.get("trailing_actuals"), Mapping)),
        ("broker_underwriting_snapshot",
         isinstance((summary.get("broker_underwriting_snapshot") or {}).get("trailing_actuals"), Mapping)),
        ("market_vacancy_benchmark", summary.get("market_vacancy_benchmark") is not None),
    ) if present]
    if not evidence:
        return None
    record: dict[str, Any] = {"rebuilt": False, "evidence": evidence}
    section = canonical.get("assumptions")
    entry = ((section or {}).get("entries") or {}).get("physical_vacancy") if isinstance(section, Mapping) else None
    if not isinstance(entry, dict):
        return {**record, "reason": "the canonical records no physical_vacancy entry (no stabilized vacancy build)"}
    if entry.get("override") is not None:
        return {**record, "reason": "physical_vacancy is an analyst override: the analyst value is priced"}
    prior = (entry.get("detail") or {}).get("vacancy_build")
    if entry.get("source") != HOUSE_STABILIZED_VACANCY_MODEL or not isinstance(prior, Mapping):
        return {**record, "reason": f"physical_vacancy is sourced {entry.get('source')}, not the stabilized build"}
    profile = pricing_profile(canonical, strategy=strategy, year_built=None)
    resolved_type = (canonical.get("deal_profile") or {}).get("deal_type")
    if resolved_type and resolved_type != STRATEGY_DEAL_TYPE.get(strategy):
        return {**record, "reason": "the pricing strategy differs from the deal type the build was resolved for"}
    ctx = _context(canonical, profile, "pricing", None)
    ctx.profile = profile.with_year_built(ctx.year_built)
    ctx.prior_entries = section.get("entries") or {}
    rows = canonical.get(_VACANCY.table) or []
    if not _same_curve(ctx, _VACANCY, rows, _ramp_rows(ctx, _VACANCY, entry.get("value"))):
        return {**record, "reason": "the priced vacancy curve is not the recorded build (rebased or edited)"}
    build = build_stabilized_vacancy(ctx, prior=prior)
    if build is None:
        return {**record, "reason": "no stabilized vacancy build is available"}
    choice = vacancy_choice(ctx, build)
    previous = {"stabilized": prior.get("stabilized"), "supply_adjustment": prior.get("supply_adjustment")}
    record.update(previous_stabilized=prior.get("stabilized"), stabilized=build.stabilized,
                  previous_value=deepcopy(entry.get("value")), value=deepcopy(choice.value),
                  market_base_level=build.detail["market_base"].get("level"),
                  asset_source_kind=build.detail["asset_vs_market"].get("asset_source_kind"))
    if previous == {"stabilized": build.stabilized, "supply_adjustment": build.supply_adjustment} and \
            build.detail["market_base"].get("level") == (prior.get("market_base") or {}).get("level"):
        return {**record, "reason": "the evidence pricing adds leaves the stabilized vacancy unchanged"}
    if not _ramp_writer(_VACANCY)(ctx, choice.value, None):
        return {**record, "reason": "the canonical has no analysis grid to write the curve on"}
    entry.update(
        value=deepcopy(choice.value), default_value=deepcopy(choice.value), as_of=choice.as_of,
        confidence=choice.confidence, detail={**(entry.get("detail") or {}), "vacancy_build": build.detail},
        basis=f"{choice.basis} Rebuilt in pricing with the evidence backsolve adds ({', '.join(evidence)}).",
    )
    candidates = [c for c in entry.get("candidates") or [] if isinstance(c, Mapping)]
    for candidate in supply_candidates(ctx):
        if not any(c.get("source") == candidate.get("source") and c.get("value") == candidate.get("value")
                   for c in candidates):
            candidates.append(candidate)
    entry["candidates"] = candidates
    return {**record, "rebuilt": True,
            "reason": f"stabilized vacancy {float(prior.get('stabilized') or 0):.2%} -> {build.stabilized:.2%}"}
