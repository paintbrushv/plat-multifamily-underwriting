"""Down-unit restoration entries (ADR 0003 decision q, UPL-110).

The rent roll's down units (``ingest_gate.evidence.rent_roll.down_units``,
classified by ``engine.ingest.rent_roll_parser.classify_unit_status``) are a
capital project with a schedule, not vacancy. This slice of the resolver:

- ``prepare``: before the keys resolve, writes the units into
  ``capital_plan.down_unit_restoration`` (once), applies a PCA's per-unit
  costs (``capital_plan.pca.down_unit_costs``, source ``deal:pca``) and an
  explicit ``down_units`` count, so the vacancy keys see the modeled units;
- ``specs``: the register entries (count by status, scope, cost per unit,
  start lag, duration, crew pace, months to lease, funding), present only
  when the deal has down units;
- ``finalize``: after the keys resolve, writes the house cost and schedule
  for a unit the analyst gave its own scope, and records the per-unit cost
  table and the total budget on the cost entry;
- ``offline_status_counts`` / ``year1_vacancy_start``: what the vacancy
  build reads so down units are neither part of the asset-vs-market gap
  (they enter ``vacancy.stabilization_exclusions`` as offline units) nor
  counted vacant in year 1 (they are offline in the schedule instead).

Cost precedence, per unit: PCA > analyst > house (scope x vintage,
``engine.underwriting_policy.default_down_unit_cost_per_unit``). The engine
side is ``engine/modules/down_units.py``.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from engine import underwriting_policy as house
from engine.assumptions.registry import (
    HOUSE_DOWN_UNIT_COST,
    HOUSE_DOWN_UNIT_FUNDING,
    HOUSE_DOWN_UNIT_SCHEDULE,
    HOUSE_DOWN_UNIT_SCOPE,
    SOURCE_ANALYST,
    SOURCE_DEAL_PCA,
)
from engine.modules.down_units import BLOCK_KEY, resolved_units

KEYS = (
    "down_units",
    "down_unit_scope",
    "down_unit_cost_per_unit",
    "down_unit_start_lag_months",
    "down_unit_duration_months",
    "down_unit_pace_per_month",
    "down_unit_months_to_lease",
    "down_unit_funding",
)
CATEGORICAL = {"down_unit_scope": house.DOWN_UNIT_SCOPES, "down_unit_funding": house.DOWN_UNIT_FUNDING_MODES}
# Whole-number keys and their minimum.
WHOLE = {
    "down_units": 0,
    "down_unit_start_lag_months": 0,
    "down_unit_duration_months": 1,
    "down_unit_pace_per_month": 1,
    "down_unit_months_to_lease": 0,
}

HOUSE_SCOPE = HOUSE_DOWN_UNIT_SCOPE
HOUSE_COST = HOUSE_DOWN_UNIT_COST
HOUSE_SCHEDULE = HOUSE_DOWN_UNIT_SCHEDULE
HOUSE_FUNDING = HOUSE_DOWN_UNIT_FUNDING
SOURCE_PCA = SOURCE_DEAL_PCA

IGNORED_FLAG = "down_unit_inputs_ignored_no_down_units"


# ── where the units live ─────────────────────────────────────────────────


def evidence_units(canonical: Mapping[str, Any]) -> list[dict[str, Any]]:
    gate = (canonical.get("metadata") or {}).get("ingest_gate") or {}
    rent_roll = (gate.get("evidence") or {}).get("rent_roll") or {}
    return [dict(u) for u in rent_roll.get("down_units") or [] if isinstance(u, Mapping)]


def block(canonical: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    plan = canonical.get("capital_plan")
    found = plan.get(BLOCK_KEY) if isinstance(plan, Mapping) else None
    return found if isinstance(found, dict) else None


def has_down_units(canonical: Mapping[str, Any]) -> bool:
    """Whether the down-unit entries apply: rent-roll down units, or a restoration block."""
    return bool(evidence_units(canonical)) or block(canonical) is not None


def _unit_record(unit: Mapping[str, Any]) -> dict[str, Any]:
    record = {"unit_id": str(unit.get("unit")), "cohort_id": unit.get("cohort_id")}
    for field in ("status", "market_rent", "vacant"):
        if unit.get(field) is not None:
            record[field] = unit[field]
    return record


def _ensure_block(canonical: dict[str, Any]) -> Optional[dict[str, Any]]:
    found = block(canonical)
    if found is not None:
        return found
    units = evidence_units(canonical)
    if not units:
        return None
    plan = canonical.get("capital_plan")
    if not isinstance(plan, dict):
        plan = {}
        canonical["capital_plan"] = plan
    plan[BLOCK_KEY] = {"units": [_unit_record(u) for u in units]}
    return plan[BLOCK_KEY]


def _set_count(canonical: dict[str, Any], count: int) -> None:
    """Model the first ``count`` down units (rent-roll order), keeping edits on the units kept.

    Without rent-roll evidence (a hand-authored block) the listed units are
    the pool, so a lower count drops units for good.
    """
    found = _ensure_block(canonical)
    if found is None:
        return
    current = {str(u.get("unit_id")): u for u in found.get("units") or []}
    pool = [_unit_record(u) for u in evidence_units(canonical)] or list(found.get("units") or [])
    found["units"] = [current.get(str(u["unit_id"]), u) for u in pool[: max(0, count)]]


def _pca_costs(canonical: Mapping[str, Any]) -> dict[str, float]:
    pca = (canonical.get("capital_plan") or {}).get("pca") or {}
    costs = pca.get("down_unit_costs") if isinstance(pca, Mapping) else None
    return {str(k): float(v) for k, v in (costs or {}).items() if v is not None}


def _apply_pca(found: dict[str, Any], costs: Mapping[str, float]) -> None:
    for unit in found.get("units") or []:
        unit_id = str(unit.get("unit_id"))
        if unit_id in costs:
            if unit.get("cost") is not None and unit.get("cost_source") in (None, SOURCE_ANALYST):
                unit["analyst_cost"] = float(unit["cost"])  # superseded by the PCA, kept for the record
            unit["cost"] = float(costs[unit_id])
            unit["cost_source"] = SOURCE_PCA
        elif unit.get("cost_source") == SOURCE_PCA:
            # The PCA no longer prices this unit: back to the analyst's cost, else the block's.
            if unit.get("analyst_cost") is not None:
                unit["cost"], unit["cost_source"] = unit.pop("analyst_cost"), SOURCE_ANALYST
            else:
                unit.pop("cost", None)
                unit.pop("cost_source", None)
        elif unit.get("cost") is not None and unit.get("cost_source") is None:
            unit["cost_source"] = SOURCE_ANALYST


def prepare(ctx: Any, explicit: dict[str, Any]) -> dict[str, Any]:
    """Write the modeled units before the keys resolve; returns ``explicit`` unchanged."""
    from engine.assumptions.resolver import is_clear

    found = _ensure_block(ctx.canonical)
    if found is None:
        return explicit
    override = explicit.get("down_units")
    if override is not None:
        if is_clear(override):
            _set_count(ctx.canonical, len(evidence_units(ctx.canonical)) or len(found.get("units") or []))
        else:
            _set_count(ctx.canonical, int(override["value"]))
    found = block(ctx.canonical)
    for unit in found.get("units") or []:
        if unit.get("scope") is not None and unit.get("scope_source") is None:
            unit["scope_source"] = SOURCE_ANALYST
    _apply_pca(found, _pca_costs(ctx.canonical))
    return explicit


# ── the per-unit plan ────────────────────────────────────────────────────


def _block_scope(found: Mapping[str, Any]) -> str:
    return found.get("scope") or house.HOUSE_DOWN_UNIT_DEFAULT_SCOPE


def unit_plan(ctx: Any) -> list[dict[str, Any]]:
    """Each modeled unit's scope, cost and schedule, with where the cost came from."""
    found = block(ctx.canonical)
    if not found or not found.get("units"):
        return []
    scope = _block_scope(found)
    rows = []
    for unit, resolved in zip(found["units"], resolved_units(ctx.canonical, found)):
        own_scope = resolved["scope"] != scope
        source = unit.get("cost_source")
        if unit.get("cost") is None:
            source = HOUSE_COST if own_scope or found.get("cost_per_unit") is None else "block"
        rows.append({
            "unit_id": resolved["unit_id"],
            "cohort_id": resolved["cohort_id"],
            "status": unit.get("status"),
            "scope": resolved["scope"],
            "scope_source": unit.get("scope_source") or ("block" if not own_scope else SOURCE_ANALYST),
            "cost": float(resolved["cost"]),
            "cost_source": source,
            "start_lag_months": resolved["start_lag_months"],
            "duration_months": resolved["duration_months"],
        })
    return rows


def _house_cost(ctx: Any, scope: str) -> float:
    return float(house.default_down_unit_cost_per_unit(scope, ctx.year_built))


def _cost_basis(ctx: Any, scope: str) -> str:
    factor = house.down_unit_vintage_factor(ctx.year_built)
    if scope == house.DOWN_UNIT_SCOPE_MAKE_READY:
        how = (f"the top of the {ctx.bucket} vintage turn band" if ctx.bucket
               else f"the top of the 1980s turn band x {float(factor):g} (unknown vintage)")
    else:
        how = (f"house {scope} ${float(house.HOUSE_DOWN_UNIT_COST_PER_UNIT[scope]):,.0f} x vintage factor "
               f"{float(factor):g}" + (f" ({ctx.bucket})" if ctx.bucket else " (unknown vintage: the highest factor)"))
    return f"${_house_cost(ctx, scope):,.0f} per unit for {scope}: {how}"


def budget_detail(ctx: Any, cost_per_unit: Optional[float] = None) -> dict[str, Any]:
    """The per-unit cost table and the total budget (``cost_per_unit`` for units at the block cost)."""
    rows = unit_plan(ctx)
    for row in rows:
        if row["cost_source"] == "block" and cost_per_unit is not None:
            row["cost"] = float(cost_per_unit)
    return {
        "units": rows,
        "total_budget": round(sum(row["cost"] for row in rows), 2),
        "precedence": "per unit: PCA (capital_plan.pca.down_unit_costs) > analyst > house scope x vintage",
    }


# ── readers, writers, defaults ───────────────────────────────────────────


def _reader(field: Optional[str]):
    from engine.assumptions.resolver import ABSENT, NO_SECTION, PLACEHOLDER, PRESENT, _Current

    def read(ctx: Any) -> Any:
        found = block(ctx.canonical)
        if found is None:
            return _Current(NO_SECTION)
        if field is None:
            return _Current(PRESENT, len(found.get("units") or []))
        value = found.get(field)
        if value is None:
            return _Current(PLACEHOLDER if found.get("units") else ABSENT)
        return _Current(PRESENT, value)

    return read


def _writer(field: str, cast):
    def write(ctx: Any, value: Any, _cohorts: Any) -> bool:
        found = block(ctx.canonical)
        if found is None or value is None:
            return False
        found[field] = cast(value)
        return True

    return write


def _write_count(ctx: Any, value: Any, _cohorts: Any) -> bool:
    if value is None or block(ctx.canonical) is None:
        return False
    _set_count(ctx.canonical, int(value))
    return True


def _choice(source: str, value: Any, basis: str, *, review: Optional[bool] = None,
            detail: Optional[dict[str, Any]] = None):
    from engine.assumptions.resolver import _Choice

    confidence = "high" if source.startswith("deal:") else "medium"
    return _Choice(source=source, value=value, basis=basis, confidence=confidence, review=review, detail=detail)


def _count_default(ctx: Any, state: str):
    from engine.assumptions.resolver import _fallback
    from engine.assumptions.registry import SOURCE_DEAL_RENT_ROLL

    units = evidence_units(ctx.canonical)
    rent_roll = ((ctx.canonical.get("metadata") or {}).get("ingest_gate") or {}).get("evidence", {}).get(
        "rent_roll") or {}
    if not units:
        found = block(ctx.canonical) or {}
        return _fallback(
            len(found.get("units") or []),
            "No rent-roll down units in the evidence: the units listed in capital_plan.down_unit_restoration are "
            "modeled.",
        )
    ids = [str(u.get("unit")) for u in units]
    counts = rent_roll.get("status_counts")
    start = rent_roll.get("physical_vacancy_rate")
    start_ex = rent_roll.get("physical_vacancy_rate_excluding_down")
    detail = {
        "status_counts": counts,
        "down_unit_ids": ids,
        "down_unit_statuses": {str(u.get("unit")): u.get("status") for u in units},
        "non_revenue_unit_ids": rent_roll.get("non_revenue_units") or [],
        "rent_roll_vacancy": start,
        "rent_roll_vacancy_excluding_down": start_ex,
    }
    by_status = ", ".join(f"{n} {name}" for name, n in (counts or {}).items() if n)
    return _choice(
        SOURCE_DEAL_RENT_ROLL, len(units),
        f"The rent roll shows {len(units)} unit(s) down ({', '.join(ids)}; units by status: {by_status}). A down "
        "unit is a capital project, not weak demand: it is restored on a construction schedule in the capital "
        "plan, earns no rent until restored and leased, and is left out of the asset-vs-market vacancy gap and "
        "out of the year-1 vacancy"
        + (f" ({start:.2%} on the roll, {start_ex:.2%} without the down units)" if start is not None
           and start_ex is not None else "")
        + ". Override the count to model fewer (0 models none).",
        detail=detail,
    )


def _scope_default(ctx: Any, state: str):
    scope = house.HOUSE_DOWN_UNIT_DEFAULT_SCOPE
    return _choice(
        HOUSE_SCOPE, scope,
        f"House default scope {scope}: the rent roll says a unit is down, not how much work it needs. A "
        "make-ready understates a unit held off the market and a full gut overstates most, so the house assumes "
        "the middle. Walk the units and classify each one (make_ready, heavy_repair or full_gut): set this for "
        "every down unit, or a unit's own scope.",
        review=True,
        detail={"units_by_scope": {row["unit_id"]: row["scope"] for row in unit_plan(ctx)}},
    )


def _scope_candidates(ctx: Any) -> list[dict[str, Any]]:
    from engine.assumptions.resolver import _candidate

    return [
        _candidate(
            HOUSE_SCOPE, scope,
            f"{scope}: {_cost_basis(ctx, scope)}; start lag {house.HOUSE_DOWN_UNIT_START_LAG_MONTHS[scope]} "
            f"month(s), {house.HOUSE_DOWN_UNIT_DURATION_MONTHS[scope]} month(s) of work.",
        )
        for scope in house.DOWN_UNIT_SCOPES
    ]


def _scope_of(ctx: Any) -> str:
    found = block(ctx.canonical) or {}
    return _block_scope(found)


def _cost_default(ctx: Any, state: str):
    scope = _scope_of(ctx)
    pca = _pca_costs(ctx.canonical)
    units = (block(ctx.canonical) or {}).get("units") or []
    ids = [str(u.get("unit_id")) for u in units]
    if ids and all(i in pca for i in ids):
        mean = round(sum(pca[i] for i in ids) / len(ids), 2)
        return _choice(
            SOURCE_PCA, mean,
            f"The PCA prices every down unit (average ${mean:,.0f}); each unit carries its own PCA cost.",
            detail=budget_detail(ctx, mean),
        )
    value = _house_cost(ctx, scope)
    return _choice(
        HOUSE_COST, value,
        f"House restoration cost {_cost_basis(ctx, scope)}, for every down unit at the {scope} scope without a "
        "PCA or analyst cost of its own (precedence per unit: PCA > analyst > house). No primary survey prices "
        "down-unit restoration; replace it with a PCA, an adjuster's estimate or bids.",
        review=True,
        detail={**budget_detail(ctx, value), "scope": scope,
                "vintage_factor": float(house.down_unit_vintage_factor(ctx.year_built))},
    )


def _cost_candidates(ctx: Any) -> list[dict[str, Any]]:
    from engine.assumptions.resolver import _candidate

    out = [_candidate(HOUSE_COST, _house_cost(ctx, scope), _cost_basis(ctx, scope)) for scope in house.DOWN_UNIT_SCOPES]
    for unit_id, cost in sorted(_pca_costs(ctx.canonical).items()):
        out.append(_candidate(SOURCE_PCA, cost, f"PCA restoration cost for unit {unit_id}."))
    return out


def _schedule_default(field: str, table: Mapping[str, int], label: str):
    def default(ctx: Any, state: str):
        scope = _scope_of(ctx)
        value = int(table[scope])
        return _choice(
            HOUSE_SCHEDULE, value,
            f"House {label} for a {scope} down unit: {value} month(s) "
            f"({', '.join(f'{s} {table[s]}' for s in house.DOWN_UNIT_SCOPES)}). A unit with its own scope takes "
            "the house value for that scope.",
            review=True,
        )

    return default


def _pace_default(ctx: Any, state: str):
    value = int(house.HOUSE_DOWN_UNIT_PACE_UNITS_PER_MONTH)
    return _choice(
        HOUSE_SCHEDULE, value,
        f"House crew pace: {value} down units started per month (one dedicated crew or contractor), the same "
        "convention as a renovation program's monthly pace. A unit waits for the crew once its start lag has "
        "passed.",
        review=True,
    )


def _lease_default(ctx: Any, state: str):
    value = int(house.HOUSE_DOWN_UNIT_MONTHS_TO_LEASE)
    return _choice(
        HOUSE_SCHEDULE, value,
        f"A restored unit is marketed for {value} month(s) after completion and earns rent from the month after, "
        "at market rent (no premium unless the analyst sets rent_premium_monthly); once leased it follows the "
        "property's vacancy curve.",
        review=True,
    )


def _funding_default(ctx: Any, state: str):
    value = house.HOUSE_DOWN_UNIT_FUNDING
    return _choice(
        HOUSE_FUNDING, value,
        "The restoration budget is reserved at close with equity, like the value-add renovation budget: it is in "
        "the total equity basis at every price backsolve tries and never reduces operating cash flow. "
        "lender_holdback funds it from the loan instead (the loan is upsized; equity unchanged). A stabilized "
        "deal's year-1 cash-on-cash carries both the reserve and the rent the units lose until restored.",
        review=True,
    )


def _no_candidates(_ctx: Any) -> list[dict[str, Any]]:
    return []


def specs() -> tuple[Any, ...]:
    """The resolver specs of the down-unit keys (``down_units_only``)."""
    from engine.assumptions.resolver import _Spec, _no_adr

    def spec(key, unit, read, write, default, gather=_no_candidates, field=None):
        path = (("capital_plan", BLOCK_KEY, field),) if field else (("capital_plan", BLOCK_KEY, "units"),)
        return _Spec(key=key, unit=unit, read=read, write=write, default=default, gather=gather,
                     patch_paths=path, adr_rank=_no_adr, down_units_only=True)

    return (
        spec("down_units", "units", _reader(None), _write_count, _count_default),
        spec("down_unit_scope", "enum", _reader("scope"), _writer("scope", str), _scope_default,
             _scope_candidates, "scope"),
        spec("down_unit_cost_per_unit", "usd_per_unit", _reader("cost_per_unit"), _writer("cost_per_unit", float),
             _cost_default, _cost_candidates, "cost_per_unit"),
        spec("down_unit_start_lag_months", "months", _reader("start_lag_months"), _writer("start_lag_months", int),
             _schedule_default("start_lag_months", house.HOUSE_DOWN_UNIT_START_LAG_MONTHS, "start lag"),
             field="start_lag_months"),
        spec("down_unit_duration_months", "months", _reader("duration_months"), _writer("duration_months", int),
             _schedule_default("duration_months", house.HOUSE_DOWN_UNIT_DURATION_MONTHS, "construction duration"),
             field="duration_months"),
        spec("down_unit_pace_per_month", "units_per_month", _reader("pace_units_per_month"),
             _writer("pace_units_per_month", int), _pace_default, field="pace_units_per_month"),
        spec("down_unit_months_to_lease", "months", _reader("months_to_lease"), _writer("months_to_lease", int),
             _lease_default, field="months_to_lease"),
        spec("down_unit_funding", "enum", _reader("funding"), _writer("funding", str), _funding_default,
             field="funding"),
    )


def normalize_override(key: str, value: Any) -> Any:
    """A whole number (counts, months, pace) or a non-negative cost; raises AssumptionOverrideError."""
    from engine.assumptions.resolver import AssumptionOverrideError

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AssumptionOverrideError(f"override for {key!r} must be a number")
    if key in WHOLE:
        if float(value) != int(value) or int(value) < WHOLE[key]:
            raise AssumptionOverrideError(f"override for {key!r} must be a whole number >= {WHOLE[key]}")
        return int(value)
    if value < 0:
        raise AssumptionOverrideError(f"override for {key!r} must be >= 0")
    return float(value)


# ── after the keys resolve ───────────────────────────────────────────────


def finalize(ctx: Any, entries: dict[str, Any]) -> None:
    """Per-unit house values for units with their own scope; the budget on the cost entry."""
    found = block(ctx.canonical)
    if found is None:
        return
    scope = _block_scope(found)
    for unit in found.get("units") or []:
        own = unit.get("scope")
        if own is not None and own == scope and unit.get("scope_source") != SOURCE_ANALYST:
            unit.pop("scope", None)
        if unit.get("scope") is None or unit["scope"] == scope:
            # At the block scope: the block's values apply; drop stale house values.
            if unit.get("cost_source") == HOUSE_COST:
                unit.pop("cost", None)
                unit.pop("cost_source", None)
            if unit.get("schedule_source") == HOUSE_SCHEDULE:
                for field in ("start_lag_months", "duration_months", "schedule_source"):
                    unit.pop(field, None)
            continue
        own = unit["scope"]
        if unit.get("cost") is None or unit.get("cost_source") == HOUSE_COST:
            unit["cost"] = _house_cost(ctx, own)
            unit["cost_source"] = HOUSE_COST
        if unit.get("schedule_source") in (None, HOUSE_SCHEDULE) and (
            unit.get("start_lag_months") is None or unit.get("schedule_source") == HOUSE_SCHEDULE
        ):
            unit["start_lag_months"] = int(house.HOUSE_DOWN_UNIT_START_LAG_MONTHS[own])
            unit["duration_months"] = int(house.HOUSE_DOWN_UNIT_DURATION_MONTHS[own])
            unit["schedule_source"] = HOUSE_SCHEDULE
    entry = entries.get("down_unit_cost_per_unit")
    if isinstance(entry, dict):
        detail = dict(entry.get("detail") or {})
        detail.update(budget_detail(ctx, found.get("cost_per_unit")))
        entry["detail"] = detail


# ── vacancy hooks ────────────────────────────────────────────────────────


def modeled_units(canonical: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    found = block(canonical)
    return list(found.get("units") or []) if found else []


def offline_status_counts(ctx: Any) -> dict[str, int]:
    """Per-status unit counts for ``vacancy.stabilization_exclusions`` when there is no box score.

    ``{"Down": modeled down units, "Notice": notice units}`` from the rent
    roll (``{}`` without modeled down units): the vacancy build's offline
    statuses (``vacancy.OFFLINE_STATUSES``) then take the down units out of
    the trailing vacancy before it is compared with the market, unless the
    T12 books them on a non-zero line of its own. Today's down share stands in
    for the trailing year's, as for a box score, and the exclusion never takes
    the asset below the market comparator (``vacancy.asset_vs_market``).

    Model, admin, employee and office units are left out on purpose: they are
    non-revenue by the operator's choice, not a capital problem, so the
    rent-roll path never counts them offline (a house box score that lists
    them keeps its own treatment).
    """
    units = modeled_units(ctx.canonical)
    if not units:
        return {}
    counts = ((ctx.evidence.get("rent_roll") or {}).get("status_counts")) or {}
    return {"Down": len(units), "Notice": int(counts.get("notice") or 0)}


def year1_vacancy_start(ctx: Any, start: Optional[float]) -> Optional[float]:
    """The rent-roll vacancy without the modeled down units (they are offline in the schedule)."""
    units = modeled_units(ctx.canonical)
    rent_roll = (ctx.evidence.get("rent_roll") or {})
    total = rent_roll.get("total_units") or ctx.units
    vacant = rent_roll.get("vacant_units")
    if start is None or not units or not total or vacant is None or len(units) >= total:
        return start
    down_vacant = sum(1 for u in units if u.get("vacant", True))
    return round(max(0, int(vacant) - down_vacant) / (int(total) - len(units)), 4)
