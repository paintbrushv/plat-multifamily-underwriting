"""Assumption resolver v1 (ADR 0003).

``resolve_assumptions`` gathers every candidate for each assumption key
(analyst, deal evidence, market, house policy, engine fallback), records them
with provenance in the canonical ``assumptions`` section, and writes the
resolved value into the existing canonical field the engine reads.

Semantics (v1):

- Precedence is ADR 0003 §3: analyst override > deal evidence > submarket >
  MSA > house default for (deal type, vintage) > engine fallback. The curve
  keys (rent_growth, physical_vacancy, loss_to_lease, concessions,
  collection_loss, exit_cap_rate) live in ``engine/assumptions/curves.py``;
  per-line opex in ``engine/assumptions/opex.py``.
- A value already in a canonical the resolver did not write is the analyst's
  (``source: analyst``) and is kept. Ingested and hand-authored canonicals are
  treated alike: where a field the engine reads is missing, the resolver
  fills it with the resolved default and flags house or fallback values
  ``review_required``. That covers rent growth (a ``growth_assumptions``
  section is created when there is none), opex growth rates, the three
  capex buckets (``capital_plan``: immediate capex at close, recurring capex
  by vintage and its growth; the replacement reserve, the house lender-style
  reserve x units over the analysis window, is the floor of the charge below
  NOI), purchase closing costs (house percent of price), the exit cap rate of
  a modeled exit (the going-in cap at the modeled price + the house spread;
  ``engine/assumptions/exit_cap.py``) and a renovation program's missing rent
  premium.
- Overrides: ``{key: value | {"value", "note"[, "cohort_overrides"]}}``. A
  ``null`` value (``{key: None}`` or ``{key: {"value": None}}``) clears the
  key's override: the entry reverts to its resolved default and source.
  ``cohort_overrides`` are accepted only by ``physical_vacancy`` and
  ``loss_to_lease`` (``COHORT_OVERRIDE_KEYS``); any other key raises
  ``AssumptionOverrideError``. ``opex.<slug>.base`` takes the line in annual
  dollars (a percent for percent-of-revenue lines); ``0`` suppresses the line
  (kept at $0 so house defaults never re-add it).
- Only engine fallbacks remain where neither deal, market nor house supplies
  a value; where ADR 0003 precedence would pick a house value the resolver
  does not write (fund partnership expenses and closing costs outside
  pricing), the entry's ``basis`` says so.
"""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass, field, is_dataclass, asdict
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Mapping, Optional

from engine import backsolve_policy
from engine import underwriting_policy as house
from engine.assumptions.canonical_ops import (
    concession_pct_rent,
    ensure_year1_concession_schedule,
    matching_opex_rows,
    annual_opex_amount,
    r_and_m_floor_rows,
    sum_r_and_m_floor_basis,
    sum_units,
    upsert_annual_opex,
    upsert_annual_opex_by_match,
)
from engine.assumptions.profile import DealProfile, check_deal_type
from engine.assumptions.registry import (
    DEAL_PREFIX,
    HOUSE_BACKSOLVE_POLICY,
    HOUSE_DEFERRED_CAPEX,
    HOUSE_DEFERRED_CAPEX_PLACEHOLDER,
    HOUSE_EXIT_NOI_WINDOW,
    HOUSE_LOAN_SIZING_NOI_BASIS,
    HOUSE_PREFIX,
    HOUSE_R_AND_M_PAYROLL_REVIEW,
    HOUSE_RECURRING_CAPEX,
    HOUSE_REPLACEMENT_RESERVE,
    HOUSE_YEAR1_INSURANCE_FLOOR,
    MARKET_PREFIX,
    MODES,
    OVERRIDE_METADATA_FIELDS,
    RESOLVER_VERSION,
    HOUSE_EXIT_CAP_SPREAD,
    SOURCE_ANALYST,
    SOURCE_DEAL_COMPS,
    SOURCE_DEAL_OM,
    SOURCE_DEAL_PCA,
    SOURCE_DEAL_RENT_ROLL,
    SOURCE_DEAL_T12,
    SOURCE_ENGINE_FALLBACK,
    AssumptionEntry,
    house_vintage_source,
    override_metadata,
    requires_review,
)

# Engine fallbacks that ingest applied before the assumption layer existed.
ENGINE_FALLBACK_RENT_GROWTH = 0.03
ENGINE_FALLBACK_COLLECTION_LOSS = 0.005
# backsolve's exit cap when neither the analyst nor the canonical supplies one.
ENGINE_FALLBACK_EXIT_CAP = 0.0625
# engine/modules/metrics.py defaults when exit_assumptions omits a field.
ENGINE_DEFAULT_SALE_COST = 0.02
ENGINE_DEFAULT_EXIT_CAP = 0.05

R_AND_M_ADJUSTMENT_CATEGORY = "Repairs & Maintenance Assumption Adjustment"

# States of a canonical field before resolution.
PRESENT = "present"          # field carries a value
PLACEHOLDER = "placeholder"  # field exists with null: the resolver fills it
ABSENT = "absent"            # section exists, field missing: engine default applies
NO_SECTION = "no_section"    # containing section missing: not modeled
NO_FIELD = "no_field"        # no canonical field; the engine does not consume it

_NOTE_PRESENT = "present in the canonical before resolution"
_NOTE_EDITED = "changed in the canonical after the last resolution"
# Basis of an analyst candidate that deal evidence outranked (``evidence_first``).
_SUPERSEDED = "Analyst value in the canonical, superseded by"


class AssumptionOverrideError(ValueError):
    """An override names an unknown key or carries an unusable value."""


@dataclass
class _Current:
    state: str
    value: Any = None
    cohort_overrides: Optional[dict[str, float]] = None


@dataclass
class _Choice:
    source: str
    value: Any
    basis: str
    as_of: Optional[str] = None
    confidence: Optional[str] = None
    # Forces review_required on (True) beyond the source rule; None follows the source.
    review: Optional[bool] = None
    # Display-only facts recorded on the entry (e.g. how the exit cap was anchored).
    detail: Optional[dict[str, Any]] = None
    # Keeps review_required on even when the analyst overrides the value (a
    # pre-2000 vintage without PCA evidence stays a review prompt).
    review_always: bool = False


@dataclass
class _Context:
    canonical: dict[str, Any]
    profile: DealProfile
    mode: str
    market: Any
    year_built: Optional[int]
    year_built_origin: Optional[str]
    bucket: Optional[str]
    ingested: bool
    evidence: dict[str, Any]
    units: int
    start: str
    end: str
    om_confidence: Optional[str] = None
    notes: dict[str, list[str]] = field(default_factory=dict)
    market_cache: dict[Any, Any] = field(default_factory=dict)  # slice 4: per-run market lookups
    # Entries of the last resolution (empty on a first resolution).
    prior_entries: Mapping[str, Any] = field(default_factory=dict)
    # ids of the opex rows this resolution added from house defaults (opex.py).
    opex_house_rows: set[int] = field(default_factory=set)

    @property
    def strategy(self) -> str:
        return self.profile.strategy


@dataclass(frozen=True)
class _Spec:
    key: str
    unit: str
    read: Callable[[_Context], _Current]
    write: Callable[[_Context, Any, Optional[dict[str, float]]], bool]
    default: Callable[[_Context, str], _Choice]
    gather: Callable[[_Context], list[dict[str, Any]]]
    market_metrics: tuple[str, ...] = ()
    patch_paths: tuple[tuple[str, ...], ...] = ()
    adr_rank: Optional[Callable[[str], Optional[int]]] = None
    cohort: bool = False
    value_add_only: bool = False
    # Disclosed, never written: only an analyst value reaches the canonical.
    informational: bool = False
    # The default moves with the purchase price (the entry-anchored exit cap):
    # resolved after every other key, and a value that moved with the price is
    # not an analyst edit.
    price_linked: bool = False
    # Deal evidence outranks an analyst value already in the canonical on a
    # first resolution (the capex buckets: PCA > analyst > house). The analyst
    # value is kept as a candidate. An explicit or later edit still wins.
    evidence_first: bool = False


# ── small helpers ────────────────────────────────────────────────────────


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _same(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, (list, tuple)) or isinstance(b, (list, tuple)):
        if not isinstance(a, (list, tuple)) or not isinstance(b, (list, tuple)) or len(a) != len(b):
            return False
        return all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, Mapping) or isinstance(b, Mapping):
        if not isinstance(a, Mapping) or not isinstance(b, Mapping) or set(a) != set(b):
            return False
        return all(_same(a[k], b[k]) for k in a)
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None:
        return math.isclose(na, nb, rel_tol=1e-12, abs_tol=1e-12)
    return a == b


def _fmt(value: Any, unit: str) -> str:
    number = _num(value)
    if number is None:
        return "none" if value is None else str(value)
    if unit.startswith("decimal"):
        return f"{number:.2%}"
    if unit.startswith("usd"):
        return f"${number:,.2f}"
    return f"{number:g}"


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _candidate(source: str, value: Any, basis: str, **extra: Any) -> dict[str, Any]:
    out = {"source": source, "value": _jsonable(value), "basis": basis}
    out.update({k: _jsonable(v) for k, v in extra.items() if v is not None})
    return out


def _confidence_for(source: str, ctx: _Context) -> Optional[str]:
    if source == SOURCE_DEAL_OM:
        return ctx.om_confidence or "medium"
    if source == SOURCE_ANALYST or source.startswith(DEAL_PREFIX):
        return "high"
    if source.startswith(HOUSE_PREFIX):
        return "medium"
    if source == SOURCE_ENGINE_FALLBACK:
        return "low"
    return None


def _house_vintage(
    ctx: _Context,
    fn: Callable[..., Optional[Decimal]],
    label: str,
    unit: str,
    *,
    strategy: Optional[str] = None,
    deferred: bool = False,
) -> Optional[dict[str, Any]]:
    """The house vintage candidate; ``deferred`` applies the deferred-maintenance minimums."""
    if ctx.bucket is None:
        return None
    strategy = strategy or ctx.strategy
    value = fn(ctx.year_built, strategy=strategy, deferred=True) if deferred else fn(ctx.year_built, strategy=strategy)
    if value is None:
        return None
    return _candidate(
        house_vintage_source(ctx.bucket),
        float(value),
        f"Year built {ctx.year_built} ({ctx.year_built_origin}) maps to the {ctx.bucket} vintage "
        f"bucket; house {label} {_fmt(value, unit)} (VINTAGE_FRAMEWORK_2026, {strategy} strategy"
        + ("; deferred maintenance flagged: the upper part of the vintage band" if deferred else "")
        + ").",
    )


def _choice_from(candidate: dict[str, Any], ctx: _Context, basis: Optional[str] = None) -> _Choice:
    return _Choice(
        source=candidate["source"],
        value=candidate["value"],
        basis=basis or candidate.get("basis", ""),
        as_of=candidate.get("as_of"),
        confidence=candidate.get("confidence") or _confidence_for(candidate["source"], ctx),
    )


def _fallback(value: Any, basis: str) -> _Choice:
    return _Choice(SOURCE_ENGINE_FALLBACK, value, basis, confidence="low")


def _get_path(data: Any, path: tuple[str, ...]) -> tuple[bool, Any]:
    node = data
    for part in path:
        if not isinstance(node, Mapping) or part not in node:
            return False, None
        node = node[part]
    return True, node


# ── canonical readers and writers ────────────────────────────────────────


def _scalar_reader(section: str, field_name: str) -> Callable[[_Context], _Current]:
    def read(ctx: _Context) -> _Current:
        container = ctx.canonical.get(section)
        if not isinstance(container, Mapping):
            return _Current(NO_SECTION)
        if field_name not in container:
            return _Current(ABSENT)
        value = container[field_name]
        if value is None:
            return _Current(PLACEHOLDER)
        return _Current(PRESENT, _num(value) if _num(value) is not None else value)

    return read


def _scalar_writer(section: str, field_name: str, *, create: Optional[Callable[[_Context], dict]] = None):
    def write(ctx: _Context, value: Any, _cohorts: Optional[dict[str, float]]) -> bool:
        container = ctx.canonical.get(section)
        if not isinstance(container, dict):
            if create is None:
                return False
            container = create(ctx)
            ctx.canonical[section] = container
        container[field_name] = value
        return True

    return write


def _read_collection_loss(ctx: _Context) -> _Current:
    curve = [row for row in ctx.canonical.get("collection_loss_curve") or [] if isinstance(row, Mapping)]
    if not curve:
        return _Current(NO_SECTION)
    rent_rows = [row for row in curve if row.get("applies_to") in ("ALL", "Rent")] or curve
    first = sorted(rent_rows, key=lambda row: str(row.get("start_period", "")))[0]
    if first.get("loss_rate") is None:
        return _Current(PLACEHOLDER)
    return _Current(PRESENT, _num(first["loss_rate"]))


def _write_collection_loss(ctx: _Context, value: Any, _cohorts: Optional[dict[str, float]]) -> bool:
    curve = ctx.canonical.get("collection_loss_curve")
    if not curve:
        ctx.canonical["collection_loss_curve"] = [
            {"applies_to": "ALL", "start_period": ctx.start, "end_period": ctx.end, "loss_rate": value}
        ]
        return True
    for row in curve:
        row["loss_rate"] = value
    return True


def _cohort_units(ctx: _Context) -> dict[str, float]:
    return {
        str(c.get("cohort_id")): float(c.get("unit_count") or 0)
        for c in ctx.canonical.get("unit_cohorts") or []
        if isinstance(c, Mapping)
    }


def _cohort_reader(table: str, value_key: str) -> Callable[[_Context], _Current]:
    def read(ctx: _Context) -> _Current:
        rows = [row for row in ctx.canonical.get(table) or [] if isinstance(row, Mapping)]
        if not rows:
            return _Current(NO_SECTION)
        first_by_cohort: dict[str, Any] = {}
        for row in sorted(rows, key=lambda r: (str(r.get("cohort_id")), str(r.get("start_period", "")))):
            first_by_cohort.setdefault(str(row.get("cohort_id")), row.get(value_key))
        if any(v is None for v in first_by_cohort.values()):
            return _Current(PLACEHOLDER)
        units = _cohort_units(ctx)
        weight: dict[float, float] = {}
        for cohort_id, rate in first_by_cohort.items():
            weight[float(rate)] = weight.get(float(rate), 0.0) + units.get(cohort_id, 0.0)
        # Property-level value: the rate covering the most units (lowest on ties).
        value = sorted(weight.items(), key=lambda item: (-item[1], item[0]))[0][0]
        cohorts = {cid: float(rate) for cid, rate in first_by_cohort.items() if not _same(rate, value)}
        return _Current(PRESENT, value, cohorts or None)

    return read


def _cohort_writer(table: str, value_key: str):
    def write(ctx: _Context, value: Any, cohorts: Optional[dict[str, float]]) -> bool:
        rows = ctx.canonical.get(table) or []
        if not rows:
            return False
        for row in rows:
            row[value_key] = (cohorts or {}).get(str(row.get("cohort_id")), value)
        return True

    return write


def _read_concessions(ctx: _Context) -> _Current:
    schedule = ctx.canonical.get("concession_schedule") or []
    if not schedule:
        return _Current(ABSENT)
    try:
        rate = max(concession_pct_rent(row) for row in schedule)
    except ValueError as exc:
        ctx.notes.setdefault("concessions", []).append(str(exc))
        return _Current(PRESENT, None)
    return _Current(PRESENT, float(rate))


def _write_concessions(ctx: _Context, value: Any, _cohorts: Optional[dict[str, float]]) -> bool:
    if _num(value) is None:
        return False
    ensure_year1_concession_schedule(ctx.canonical, rate=Decimal(str(value)))
    for row in ctx.canonical.get("concession_schedule") or []:
        row["concession_id"] = "assumption_year1_concessions"
    return True


def _per_unit(ctx: _Context, annual: Decimal) -> Optional[float]:
    if not ctx.units:
        return None
    return float(annual / Decimal(str(ctx.units)))


def _read_insurance(ctx: _Context) -> _Current:
    rows = matching_opex_rows(ctx.canonical, "insurance")
    if not rows:
        return _Current(ABSENT)
    try:
        return _Current(PRESENT, _per_unit(ctx, annual_opex_amount(ctx.canonical, rows[0])))
    except ValueError as exc:
        ctx.notes.setdefault("insurance_per_unit", []).append(str(exc))
        return _Current(PRESENT, None)


def _write_insurance(ctx: _Context, value: Any, _cohorts: Optional[dict[str, float]]) -> bool:
    if _num(value) is None or not ctx.units:
        return False
    upsert_annual_opex_by_match(
        ctx.canonical,
        name_fragment="insurance",
        fallback_category_name="Insurance",
        annual_amount=Decimal(str(value)) * Decimal(str(ctx.units)),
    )
    return True


def _r_and_m_rows(canonical: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The lines on the R&M floor basis: R&M and turnover / make-ready, never payroll."""
    return r_and_m_floor_rows(canonical)


def _read_r_and_m(ctx: _Context) -> _Current:
    if not _r_and_m_rows(ctx.canonical):
        return _Current(ABSENT)
    try:
        return _Current(PRESENT, _per_unit(ctx, sum_r_and_m_floor_basis(ctx.canonical)))
    except ValueError as exc:
        ctx.notes.setdefault("r_and_m_per_unit", []).append(str(exc))
        return _Current(PRESENT, None)


def _write_r_and_m(ctx: _Context, value: Any, _cohorts: Optional[dict[str, float]]) -> bool:
    """Top the R&M bucket up (or down) to the target with one adjustment line."""
    if _num(value) is None or not ctx.units:
        return False
    adjustment = R_AND_M_ADJUSTMENT_CATEGORY.lower()
    others = Decimal("0")
    for row in _r_and_m_rows(ctx.canonical):
        if str(row.get("category_name", "")).lower() != adjustment:
            others += annual_opex_amount(ctx.canonical, row)
    delta = Decimal(str(value)) * Decimal(str(ctx.units)) - others
    has_adjustment = any(
        str(row.get("category_name", "")).lower() == adjustment for row in ctx.canonical.get("opex_table") or []
    )
    if delta == 0 and not has_adjustment:
        return True
    upsert_annual_opex(ctx.canonical, category_name=R_AND_M_ADJUSTMENT_CATEGORY, annual_amount=delta)
    return True


def _prior_entry(ctx: _Context, key: str) -> Optional[Mapping[str, Any]]:
    """The entry ``key`` had at the last resolution, if any."""
    section = ctx.canonical.get("assumptions")
    entries = section.get("entries") if isinstance(section, Mapping) else None
    entry = entries.get(key) if isinstance(entries, Mapping) else None
    return entry if isinstance(entry, Mapping) else None


def _read_reserves(ctx: _Context) -> _Current:
    rows = [row for row in ctx.canonical.get("replacement_reserves") or [] if isinstance(row, Mapping)]
    if not rows:
        # No reserves: the resolver fills the house lender-style reserve.
        fillable = ctx.units and len(ctx.start) == 7 and len(ctx.end) == 7
        return _Current(PLACEHOLDER if fillable else ABSENT)
    first = sorted(rows, key=lambda r: str(r.get("start_period", "")))[0]
    if first.get("annual_amount") is None:
        return _Current(PLACEHOLDER)
    return _Current(PRESENT, _per_unit(ctx, Decimal(str(first["annual_amount"]))))


def _write_reserves(ctx: _Context, value: Any, _cohorts: Optional[dict[str, float]]) -> bool:
    if _num(value) is None or not ctx.units:
        return False
    ctx.canonical["replacement_reserves"] = [
        {
            "start_period": ctx.start,
            "end_period": ctx.end,
            "annual_amount": float(Decimal(str(value)) * Decimal(str(ctx.units))),
        }
    ]
    return True


# ── capital plan: the three capex buckets (ADR 0003 §3) ──────────────────
#
# Bucket 1 (immediate_capex) and bucket 2 (recurring_capex_per_unit and its
# growth) are written into ``capital_plan``; bucket 3 is the replacement
# reserve above. The engine charges max(recurring capex, reserve) below NOI.

# The capex_schedule row the retired economic_capex_per_unit opt-in wrote. A
# canonical that still carries one is read as the analyst's recurring capex,
# and the row is removed when recurring capex is written (never charged twice).
ECONOMIC_CAPEX_CATEGORY = "Economic CapEx (analyst opt-in)"
# Retired keys still accepted in overrides and sheets.
KEY_ALIASES = {"economic_capex_per_unit": "recurring_capex_per_unit"}


def _capital_plan(ctx: _Context) -> Mapping[str, Any]:
    plan = ctx.canonical.get("capital_plan")
    return plan if isinstance(plan, Mapping) else {}


def _pca(ctx: _Context) -> Mapping[str, Any]:
    pca = _capital_plan(ctx).get("pca")
    return pca if isinstance(pca, Mapping) else {}


def _pca_label(pca: Mapping[str, Any]) -> str:
    parts = [str(pca[name]) for name in ("reference", "as_of") if pca.get(name)]
    return f" ({', '.join(parts)})" if parts else ""


def _plan_block(ctx: _Context, name: str) -> dict[str, Any]:
    plan = ctx.canonical.get("capital_plan")
    if not isinstance(plan, dict):
        plan = {}
        ctx.canonical["capital_plan"] = plan
    block = plan.get(name)
    if not isinstance(block, dict):
        block = {}
        plan[name] = block
    return block


def _fillable(ctx: _Context) -> bool:
    return bool(ctx.units and len(ctx.start) == 7 and len(ctx.end) == 7)


def _economic_capex_rows(ctx: _Context) -> list[dict[str, Any]]:
    return [
        row for row in ctx.canonical.get("capex_schedule") or []
        if isinstance(row, dict) and row.get("category") == ECONOMIC_CAPEX_CATEGORY
    ]


def _read_immediate_capex(ctx: _Context) -> _Current:
    block = _capital_plan(ctx).get("immediate_capex")
    if not isinstance(block, Mapping) or block.get("amount") is None:
        return _Current(PLACEHOLDER if ctx.units else ABSENT)
    return _Current(PRESENT, _num(block.get("amount")))


def _write_immediate_capex(ctx: _Context, value: Any, _cohorts: Optional[dict[str, float]]) -> bool:
    if _num(value) is None:
        return False
    block = _plan_block(ctx, "immediate_capex")
    block["amount"] = float(value)
    block.setdefault("funding", house.IMMEDIATE_CAPEX_EQUITY_AT_CLOSE)
    return True


def _read_recurring_capex(ctx: _Context) -> _Current:
    block = _capital_plan(ctx).get("recurring_capex")
    if isinstance(block, Mapping) and block.get("per_unit_annual") is not None:
        return _Current(PRESENT, _num(block.get("per_unit_annual")))
    legacy = _economic_capex_rows(ctx)
    if legacy and _num(legacy[0].get("amount_per_unit")) is not None:
        # The retired economic CapEx opt-in is the analyst's recurring capex: it
        # moves to capital_plan (ctx.canonical is the resolver's own copy) and
        # leaves the operating capex schedule, so it is charged once, below NOI.
        value = _num(legacy[0].get("amount_per_unit"))
        _write_recurring_capex(ctx, value, None)
        return _Current(PRESENT, value)
    return _Current(PLACEHOLDER if _fillable(ctx) else ABSENT)


def _write_recurring_capex(ctx: _Context, value: Any, _cohorts: Optional[dict[str, float]]) -> bool:
    """Write the year-1 recurring capex per unit; its growth is its own key."""
    if _num(value) is None:
        return False
    _plan_block(ctx, "recurring_capex")["per_unit_annual"] = float(value)
    schedule = [
        row for row in ctx.canonical.get("capex_schedule") or []
        if not (isinstance(row, dict) and row.get("category") == ECONOMIC_CAPEX_CATEGORY)
    ]
    if schedule:
        ctx.canonical["capex_schedule"] = schedule
    else:
        ctx.canonical.pop("capex_schedule", None)
    return True


def _read_recurring_growth(ctx: _Context) -> _Current:
    block = _capital_plan(ctx).get("recurring_capex")
    if not isinstance(block, Mapping):
        return _Current(NO_SECTION)
    if block.get("growth_rate") is None:
        return _Current(PLACEHOLDER)
    return _Current(PRESENT, _num(block.get("growth_rate")))


def _write_recurring_growth(ctx: _Context, value: Any, _cohorts: Optional[dict[str, float]]) -> bool:
    block = _capital_plan(ctx).get("recurring_capex")
    if not isinstance(block, dict) or _num(value) is None:
        return False
    block["growth_rate"] = float(value)
    return True


def _read_no_field(_ctx: _Context) -> _Current:
    return _Current(NO_FIELD)


def _write_nothing(_ctx: _Context, _value: Any, _cohorts: Optional[dict[str, float]]) -> bool:
    return False


def _read_purchase_closing(ctx: _Context) -> _Current:
    purchase = ctx.canonical.get("purchase_assumptions")
    if not isinstance(purchase, Mapping):
        return _Current(NO_SECTION)
    closing, price = purchase.get("closing_costs"), _num(purchase.get("purchase_price"))
    if closing is None:
        # No closing costs: the house percent of price is filled when there is a price.
        return _Current(PLACEHOLDER if price else ABSENT)
    if not price:
        return _Current(PRESENT, None)
    if ctx.ingested and _num(closing) == 0 and _prior_entry(ctx, "purchase_closing_cost_pct") is None:
        # Ingest writes closing_costs = 0.0 as a placeholder; the analyst never set it.
        return _Current(PLACEHOLDER)
    return _Current(PRESENT, float(Decimal(str(closing)) / Decimal(str(price))))


def _write_purchase_closing(ctx: _Context, value: Any, _cohorts: Optional[dict[str, float]]) -> bool:
    purchase = ctx.canonical.get("purchase_assumptions")
    price = _num((purchase or {}).get("purchase_price")) if isinstance(purchase, Mapping) else None
    if not isinstance(purchase, dict) or not price or _num(value) is None:
        return False
    purchase["closing_costs"] = round(float(Decimal(str(value)) * Decimal(str(price))), 2)
    return True


def _new_exit_section(ctx: _Context) -> dict[str, Any]:
    return {"exit_month": ctx.end}


# ── defaults (today's behavior) and candidates per key ───────────────────


def _not_modeled(section: str) -> _Choice:
    return _fallback(None, f"The canonical has no {section}, so the engine does not model this assumption.")


def _rent_growth_default(ctx: _Context, state: str) -> _Choice:
    if state in (ABSENT, NO_SECTION):
        return _fallback(
            0.0,
            "The canonical carries no growth_assumptions.annual_growth_rate; the engine applies no "
            "market rent growth.",
        )
    return _fallback(
        ENGINE_FALLBACK_RENT_GROWTH,
        "No market or house rent-growth assumption was available; engine fallback of "
        f"{ENGINE_FALLBACK_RENT_GROWTH:.2%} annual compound applied.",
    )


def _rent_growth_candidates(ctx: _Context) -> list[dict[str, Any]]:
    return [
        _candidate(
            SOURCE_ENGINE_FALLBACK,
            ENGINE_FALLBACK_RENT_GROWTH,
            "Ingest engine fallback (annual compound).",
        )
    ]


def _t12_rate(ctx: _Context, field_name: str) -> Optional[float]:
    t12 = ctx.evidence.get("t12") or {}
    gpr, amount = _num(t12.get("gross_potential_rent")), _num(t12.get(field_name))
    if not gpr or amount is None:
        return None
    return float(Decimal(str(abs(amount))) / Decimal(str(gpr)))


def _collection_loss_candidates(ctx: _Context) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    t12_rate = _t12_rate(ctx, "bad_debt")
    if t12_rate is not None:
        out.append(_candidate(SOURCE_DEAL_T12, t12_rate, "T12 bad debt / gross potential rent."))
    house_candidate = _house_vintage(
        ctx, house.default_collection_loss_rate, "collection-loss floor", "decimal_of_rent",
        deferred=ctx.profile.deferred_maintenance,
    )
    if house_candidate:
        out.append(house_candidate)
    out.append(
        _candidate(
            SOURCE_ENGINE_FALLBACK,
            ENGINE_FALLBACK_COLLECTION_LOSS,
            "Ingest engine fallback when the year built is unknown.",
        )
    )
    return out


def _collection_loss_default(ctx: _Context, state: str) -> _Choice:
    house_candidate = next(
        (c for c in _collection_loss_candidates(ctx) if c["source"].startswith(HOUSE_PREFIX)), None
    )
    if house_candidate:
        return _choice_from(house_candidate, ctx)
    return _fallback(
        ENGINE_FALLBACK_COLLECTION_LOSS,
        "Year built unknown, so the house vintage framework could not be applied; engine "
        f"fallback of {ENGINE_FALLBACK_COLLECTION_LOSS:.2%} applied.",
    )


def _vacancy_candidates(ctx: _Context) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    roll_rate = _num((ctx.evidence.get("rent_roll") or {}).get("physical_vacancy_rate"))
    if roll_rate is not None:
        out.append(_candidate(SOURCE_DEAL_RENT_ROLL, roll_rate, "Current physical vacancy from the rent roll."))
    house_candidate = _house_vintage(
        ctx, house.default_physical_vacancy_rate, "physical-vacancy floor", "decimal_of_units",
        deferred=ctx.profile.deferred_maintenance,
    )
    if house_candidate:
        guardrails = house.revenue_guardrails(ctx.year_built, strategy=ctx.strategy)
        low, high = guardrails["physical_vacancy_typical_low"], guardrails["physical_vacancy_typical_high"]
        house_candidate["basis"] += f" Typical stabilized band {float(low):.2%}-{float(high):.2%}."
        out.append(house_candidate)
    return out


def _vacancy_default(ctx: _Context, state: str) -> _Choice:
    candidates = _vacancy_candidates(ctx)
    for prefix in (SOURCE_DEAL_RENT_ROLL, HOUSE_PREFIX):
        match = next((c for c in candidates if c["source"].startswith(prefix)), None)
        if match:
            return _choice_from(match, ctx)
    return _fallback(None, "No rent-roll evidence and no year built; no vacancy default is available.")


def _ltl_candidates(ctx: _Context) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    roll_ltl = _num((ctx.evidence.get("rent_roll") or {}).get("loss_to_lease_rate"))
    if roll_ltl is not None:
        out.append(
            _candidate(SOURCE_DEAL_RENT_ROLL, roll_ltl, "Rent roll market vs in-place rent, unit-weighted.")
        )
    house_candidate = _house_vintage(
        ctx, house.default_loss_to_lease_rate, "loss-to-lease floor", "decimal_of_market_rent"
    )
    if house_candidate:
        out.append(house_candidate)
    out.append(_candidate(SOURCE_ENGINE_FALLBACK, 0.0, "Ingest writes a flat 0% loss to lease."))
    return out


def _ltl_default(ctx: _Context, state: str) -> _Choice:
    return _fallback(0.0, "Ingest writes a flat 0% loss to lease; market rent and in-place rent carry the gap.")


def _concession_candidates(ctx: _Context) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    t12_rate = _t12_rate(ctx, "concessions")
    if t12_rate is not None:
        out.append(_candidate(SOURCE_DEAL_T12, t12_rate, "T12 concessions / gross potential rent."))
    house_candidate = _house_vintage(
        ctx, house.default_year1_concessions_rate, "year-1 concessions floor", "decimal_of_rent",
        deferred=ctx.profile.deferred_maintenance,
    )
    if house_candidate:
        out.append(house_candidate)
    out.append(_candidate(SOURCE_ENGINE_FALLBACK, 0.0, "No concession_schedule: the engine applies none."))
    return out


def _concession_default(ctx: _Context, state: str) -> _Choice:
    return _fallback(0.0, "No concession_schedule: the engine applies no concessions.")


def _opex_evidence(ctx: _Context, field_name: str, label: str) -> Optional[dict[str, Any]]:
    annual = _num((ctx.evidence.get("t12") or {}).get(field_name))
    if annual is None or not ctx.units:
        return None
    return _candidate(
        SOURCE_DEAL_T12,
        _per_unit(ctx, Decimal(str(annual))),
        f"T12 {label} annualized by calculation_type: ${annual:,.2f} / {ctx.units} units.",
    )


def _insurance_candidates(ctx: _Context) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    evidence = _opex_evidence(ctx, "insurance_annual", "insurance")
    if evidence:
        out.append(evidence)
    out.append(
        _candidate(
            HOUSE_YEAR1_INSURANCE_FLOOR,
            float(house.default_year1_insurance_per_unit(None)),
            "House Year-1 insurance minimum per unit (a guardrail floor; backsolve applies "
            "max(observed, floor) in pricing).",
        )
    )
    out.append(_candidate(SOURCE_ENGINE_FALLBACK, 0.0, "No insurance opex line: the engine carries none."))
    return out


def _opex_default(field_name: str, label: str, absent_basis: str):
    def default(ctx: _Context, state: str) -> _Choice:
        if state != ABSENT:
            # ── ADR 0003 slice 5 hook: core line added from house defaults ──
            filled = _opex_slice().filled_core_candidate(ctx, field_name)
            if filled:
                return _choice_from(filled, ctx)
            evidence = _opex_evidence(ctx, field_name, label)
            if evidence:
                return _choice_from(evidence, ctx)
        return _fallback(0.0, absent_basis)

    return default


def _r_and_m_candidates(ctx: _Context) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    evidence = _opex_evidence(
        ctx, "repairs_maintenance_annual", "R&M including turnover / make-ready (payroll excluded)"
    )
    if evidence:
        out.append(evidence)
    house_candidate = _house_vintage(ctx, house.default_r_and_m_per_unit, "R&M per unit", "usd_per_unit_year",
                                     deferred=ctx.profile.deferred_maintenance)
    if house_candidate:
        out.append(house_candidate)
    out.append(_candidate(SOURCE_ENGINE_FALLBACK, 0.0, "No R&M opex lines: the engine carries none."))
    return out


def _r_and_m_floor_per_unit(ctx: _Context) -> Optional[Decimal]:
    from engine.assumptions.guardrails import GuardrailProfile

    return GuardrailProfile(
        strategy=ctx.strategy, year_built=ctx.year_built,
        deferred_maintenance=ctx.profile.deferred_maintenance,
    ).r_and_m_per_unit()


def _payroll_review(ctx: _Context, credit_override_per_unit: Any = None) -> dict[str, Any]:
    from engine.assumptions import guardrails

    override = Decimal(str(credit_override_per_unit)) if credit_override_per_unit is not None else None
    return guardrails.r_and_m_payroll_review(
        ctx.canonical, _r_and_m_floor_per_unit(ctx), credit_override_per_unit=override)


def _payroll_credit_candidates(ctx: _Context) -> list[dict[str, Any]]:
    from engine.assumptions import guardrails

    review = _payroll_review(ctx)
    return [_candidate(HOUSE_R_AND_M_PAYROLL_REVIEW, review["credit_per_unit"] or 0.0,
                       guardrails.payroll_review_text(review))]


def _payroll_credit_default(ctx: _Context, state: str) -> _Choice:
    """The R&M floor's payroll credit per unit a year (ADR 0003 §5): $0 unless the review finds one."""
    from engine.assumptions import guardrails

    review = _payroll_review(ctx)
    return _Choice(
        source=HOUSE_R_AND_M_PAYROLL_REVIEW,
        value=review["credit_per_unit"] or 0.0,
        basis=(
            "Payroll is payroll: it earns no credit toward the R&M floor unless the T12 labels maintenance labor "
            "in payroll and payroll per unit is above the house benchmark (HOUSE_PAYROLL_BENCHMARK_PER_UNIT); the "
            "credit is the lesser of the two. " + guardrails.payroll_review_text(review)
        ),
        confidence="medium",
        review=True if review["review_required"] else None,
        detail=review,
    )


def _reserve_candidates(ctx: _Context) -> list[dict[str, Any]]:
    reserve = house.default_replacement_reserve_per_unit()
    return [
        _candidate(
            HOUSE_REPLACEMENT_RESERVE,
            float(reserve),
            f"House lender-style replacement reserve {_fmt(reserve, 'usd')}/unit/year below NOI "
            "(HOUSE_REPLACEMENT_RESERVE_PER_UNIT, the same for every vintage). It is a floor, not an "
            "addition: the charge below NOI is max(recurring capex by vintage, this reserve).",
        ),
        _candidate(SOURCE_ENGINE_FALLBACK, 0.0, "No replacement_reserves: the engine reserves nothing."),
    ]


def _reserve_default(ctx: _Context, state: str) -> _Choice:
    candidate = _reserve_candidates(ctx)[0]
    return _choice_from(
        candidate,
        ctx,
        candidate["basis"] + " Written as replacement_reserves = reserve per unit x units over the "
        "analysis window when the canonical carries none.",
    )


_CHARGE_NOTE = (
    " Recurring capex sits below NOI (not opex) and grows every analysis year; the charge below NOI each "
    "month is max(recurring capex, replacement reserve), never both."
)


def _pre_2000_without_pca(ctx: _Context) -> bool:
    return (
        ctx.year_built is not None
        and ctx.year_built < house.CAPEX_PCA_REVIEW_BEFORE_YEAR
        and _num(_pca(ctx).get("recurring_capex_per_unit")) is None
    )


def _recurring_house_candidate(ctx: _Context) -> Optional[dict[str, Any]]:
    deferred = ctx.profile.deferred_maintenance
    value = house.default_recurring_capex_per_unit(ctx.year_built, strategy=ctx.strategy, deferred=deferred)
    band = house.recurring_capex_band(ctx.year_built)
    if value is None or band is None:
        return None
    pick = house.recurring_capex_band_pick(strategy=ctx.strategy, deferred=deferred)
    reason = (
        "deferred maintenance flagged" if deferred
        else ("value-add deal" if ctx.strategy == "value_add" else "stabilized deal")
    )
    return _candidate(
        HOUSE_RECURRING_CAPEX,
        float(value),
        f"Year built {ctx.year_built} ({ctx.year_built_origin}) maps to the {ctx.bucket} vintage; house "
        f"recurring capex is the {pick} of its economic CapEx band {_fmt(band[0], 'usd')}-"
        f"{_fmt(band[1], 'usd')}/unit/year ({reason}; VINTAGE_FRAMEWORK_2026).",
    )


def _recurring_capex_candidates(ctx: _Context) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    pca = _pca(ctx)
    pca_value = _num(pca.get("recurring_capex_per_unit"))
    if pca_value is not None:
        out.append(_candidate(
            SOURCE_DEAL_PCA, pca_value,
            f"Property condition assessment{_pca_label(pca)}: annual capital needs "
            f"{_fmt(pca_value, 'usd')}/unit/year.",
            as_of=pca.get("as_of"),
        ))
    house_candidate = _recurring_house_candidate(ctx)
    if house_candidate:
        out.append(house_candidate)
    else:
        reserve = house.default_replacement_reserve_per_unit()
        out.append(_candidate(
            HOUSE_REPLACEMENT_RESERVE, float(reserve),
            f"Year built unknown: no vintage band, so recurring capex falls back to the "
            f"{_fmt(reserve, 'usd')}/unit lender replacement reserve.",
        ))
    return out


def _recurring_capex_detail(ctx: _Context) -> dict[str, Any]:
    deferred = ctx.profile.deferred_maintenance
    band = house.recurring_capex_band(ctx.year_built)
    return {
        "bucket": "2: recurring capex by vintage (below NOI)",
        "vintage_bucket": ctx.bucket,
        "band_low": float(band[0]) if band else None,
        "band_high": float(band[1]) if band else None,
        "band_pick": house.recurring_capex_band_pick(strategy=ctx.strategy, deferred=deferred) if band else None,
        "deal_type": ctx.profile.deal_type,
        "deferred_maintenance": deferred,
        "pca_evidence": _num(_pca(ctx).get("recurring_capex_per_unit")) is not None,
        "pca_review_required": _pre_2000_without_pca(ctx),
        "replacement_reserve_floor_per_unit": float(house.default_replacement_reserve_per_unit()),
        "charge_rule": "max(recurring capex, replacement reserve) per month below NOI",
    }


def _recurring_capex_default(ctx: _Context, state: str) -> _Choice:
    candidates = _recurring_capex_candidates(ctx)
    choice = _choice_from(candidates[0], ctx, candidates[0]["basis"] + _CHARGE_NOTE)
    if _pre_2000_without_pca(ctx):
        choice.basis += (
            f" Built {ctx.year_built}, before {house.CAPEX_PCA_REVIEW_BEFORE_YEAR}, with no PCA: review the "
            "recurring capex against a property condition assessment."
        )
        choice.review_always = True
    choice.detail = _recurring_capex_detail(ctx)
    return choice


def _recurring_growth_candidates(ctx: _Context) -> list[dict[str, Any]]:
    return [_candidate(
        "house:opex_growth_table", float(house.HOUSE_RECURRING_CAPEX_GROWTH),
        f"Recurring capex grows like repairs & maintenance ({house.HOUSE_RECURRING_CAPEX_GROWTH_CONSTANT} "
        f"{_fmt(house.HOUSE_RECURRING_CAPEX_GROWTH, 'decimal')} a year), applied from analysis year 2.",
    )]


def _recurring_growth_default(ctx: _Context, state: str) -> _Choice:
    if state == NO_SECTION:
        return _not_modeled("capital_plan.recurring_capex")
    return _choice_from(_recurring_growth_candidates(ctx)[0], ctx)


def _immediate_capex_candidates(ctx: _Context) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    pca = _pca(ctx)
    pca_value = _num(pca.get("immediate_capex"))
    if pca_value is not None:
        out.append(_candidate(
            SOURCE_DEAL_PCA, pca_value,
            f"Property condition assessment{_pca_label(pca)}: immediate needs {_fmt(pca_value, 'usd')}.",
            as_of=pca.get("as_of"),
        ))
    placeholder = _deferred_capex_placeholder(ctx)
    if placeholder:
        out.append(placeholder)
    out.append(_candidate(
        HOUSE_DEFERRED_CAPEX, float(house.HOUSE_IMMEDIATE_CAPEX_DEFAULT),
        "No PCA immediate needs and no analyst figure: nothing is budgeted at close.",
    ))
    return out


def _deferred_capex_placeholder(ctx: _Context) -> Optional[dict[str, Any]]:
    """Deferred maintenance flagged: one year of the vintage band high end per unit at close."""
    if not ctx.profile.deferred_maintenance or not ctx.units:
        return None
    per_unit = house.default_deferred_capex_placeholder_per_unit(ctx.year_built)
    if per_unit is None:
        return None
    escrow = house.LENDER_PCA_REPAIR_ESCROW_FACTOR
    return _candidate(
        HOUSE_DEFERRED_CAPEX_PLACEHOLDER,
        float(per_unit * Decimal(ctx.units)),
        f"Deferred maintenance is flagged with no PCA and no analyst figure: placeholder of "
        f"{house.HOUSE_DEFERRED_CAPEX_PLACEHOLDER_YEARS:g} year of the {ctx.bucket} economic CapEx band high end "
        f"({_fmt(per_unit, 'usd')}/unit x {ctx.units} units). Commission a PCA and replace it: lenders escrow "
        f"PCA immediate repairs at {float(escrow):.0%} of the estimate.",
    )


def _immediate_capex_default(ctx: _Context, state: str) -> _Choice:
    candidates = _immediate_capex_candidates(ctx)
    note = (
        " One-time capital at close for deferred maintenance / PCA immediate needs, funded with equity at "
        "close (or a lender holdback): it adds to the total equity basis and never enters NOI. It is "
        "separate from the renovation budget."
    )
    choice = _choice_from(candidates[0], ctx, candidates[0]["basis"] + note)
    if ctx.profile.deferred_maintenance and candidates[0]["source"] == HOUSE_DEFERRED_CAPEX:
        choice.basis += " Deferred maintenance is flagged: budget the immediate needs (PCA or analyst)."
        choice.review_always = True
    elif candidates[0]["source"] == HOUSE_DEFERRED_CAPEX_PLACEHOLDER:
        choice.review_always = True
    choice.detail = {"bucket": "1: immediate / deferred capex at close"}
    if candidates[0]["source"] == HOUSE_DEFERRED_CAPEX_PLACEHOLDER:
        choice.detail.update(
            placeholder_per_unit=float(house.default_deferred_capex_placeholder_per_unit(ctx.year_built)),
            placeholder_years=float(house.HOUSE_DEFERRED_CAPEX_PLACEHOLDER_YEARS),
            lender_pca_repair_escrow_factor=float(house.LENDER_PCA_REPAIR_ESCROW_FACTOR),
        )
    return choice


def _turn_candidates(ctx: _Context) -> list[dict[str, Any]]:
    house_candidate = _house_vintage(
        ctx, house.default_turn_capex_per_unit, "turn cost per turned unit", "usd_per_turn",
        strategy="value_add",
    )
    return [house_candidate] if house_candidate else []


def _turn_default(ctx: _Context, state: str) -> _Choice:
    candidates = _turn_candidates(ctx)
    note = " Informational: the engine does not consume a turn cost in this slice."
    if candidates:
        return _choice_from(candidates[0], ctx, candidates[0]["basis"] + note)
    return _fallback(None, "Year built unknown, so no house turn cost is available." + note)


# ── renovation premium (value-add only, ADR 0003 §3) ─────────────────────


def _renovation_programs(ctx: _Context) -> list[dict[str, Any]]:
    return [p for p in ctx.canonical.get("renovation_programs") or [] if isinstance(p, dict)]


def _program_premium(programs: list[dict[str, Any]]) -> float:
    """One property-level premium: the programs' common premium, else the max_units-weighted mean."""
    premiums = [float(_num(p["rent_premium_monthly"])) for p in programs]
    if len(set(premiums)) == 1:
        return premiums[0]
    weights = [float(_num(p.get("max_units")) or 0) for p in programs]
    if sum(weights) > 0:
        return round(sum(w * v for w, v in zip(weights, premiums)) / sum(weights), 2)
    return round(sum(premiums) / len(premiums), 2)


def _read_renovation_premium(ctx: _Context) -> _Current:
    programs = _renovation_programs(ctx)
    if not programs:
        return _Current(NO_FIELD)
    if any(_num(p.get("rent_premium_monthly")) is None for p in programs):
        return _Current(PLACEHOLDER)
    return _Current(PRESENT, _program_premium(programs))


def _write_renovation_premium(ctx: _Context, value: Any, _cohorts: Optional[dict[str, float]]) -> bool:
    """Fill programs without a premium; an override sets every program's premium."""
    programs = _renovation_programs(ctx)
    if not programs or _num(value) is None:
        return False
    missing = [p for p in programs if _num(p.get("rent_premium_monthly")) is None]
    for program in missing or programs:
        program["rent_premium_monthly"] = float(value)
    return True


def _comps_premium(ctx: _Context) -> Optional[tuple[float, str]]:
    comps = ctx.evidence.get("comps")
    if not isinstance(comps, Mapping):
        return None
    premium = _num(comps.get("renovation_premium"))
    if premium is not None:
        return premium, f"Comp evidence: renovated-unit premium ${premium:,.2f}/unit/month."
    renovated, classic = _num(comps.get("renovated_effective_rent")), _num(comps.get("classic_effective_rent"))
    if renovated is None or classic is None:
        return None
    premium = round(renovated - classic, 2)
    return premium, (
        f"Comp evidence: renovated effective rent ${renovated:,.2f} - classic ${classic:,.2f} "
        f"= ${premium:,.2f}/unit/month."
    )


def _renovation_premium_candidates(ctx: _Context) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    comps = _comps_premium(ctx)
    if comps is not None:
        out.append(_candidate(SOURCE_DEAL_COMPS, comps[0], comps[1]))
    out.append(_candidate(
        SOURCE_ENGINE_FALLBACK, None,
        "No house renovation premium exists: without a program, comp or market premium none is modeled.",
    ))
    return out


_NO_PROGRAMS = (
    " This value-add deal has no renovation_programs, so no premium is modeled: add a renovation "
    "program (or confirm the business plan has none)."
)


def _renovation_premium_default(ctx: _Context, state: str) -> _Choice:
    no_programs = state == NO_FIELD
    choice: Optional[_Choice] = None
    comps = _comps_premium(ctx)
    if comps is not None:
        choice = _Choice(SOURCE_DEAL_COMPS, comps[0], comps[1], confidence="high")
    else:
        pick = _curves().market_pick(ctx, "renovation_premium")
        if pick is not None:
            value = round(float(pick.observation.scalar), 2)
            choice = _Choice(
                pick.source, value,
                f"{pick.level} market renovation_premium ${value:,.2f}/unit/month from "
                f"{pick.observation.provenance.provider} (renovated vs classic rent).",
                as_of=pick.as_of, confidence=pick.observation.confidence,
            )
    if choice is None:
        basis = (
            "No renovation program premium, comp evidence or market renovation_premium is available, and "
            "there is no house premium: the analyst must supply the premium."
        )
        return _Choice(SOURCE_ENGINE_FALLBACK, None, basis + (_NO_PROGRAMS if no_programs else ""),
                       confidence="low", review=True)
    if no_programs:
        choice.basis += _NO_PROGRAMS
        choice.review = True
    return choice


def _no_adr(_source: str) -> Optional[int]:
    return None


def _t12_total(gate: Mapping[str, Any], section: str) -> Optional[float]:
    recon = gate.get("t12_reconciliation")
    totals = recon.get(section) if isinstance(recon, Mapping) else None
    if not isinstance(totals, Mapping):
        return None
    for name in ("statement_total", "parsed_total"):
        value = _num(totals.get(name))
        if value is not None:
            return value
    return None


def t12_egi(canonical: Mapping[str, Any]) -> Optional[float]:
    """Trailing EGI (T12 total income) from the ingest gate's reconciliation, else None."""
    gate = (canonical.get("metadata") or {}).get("ingest_gate")
    return _t12_total(gate, "income") if isinstance(gate, Mapping) else None


# ── exit cap rate (ADR 0003 §3): entry-anchored at the modeled price ────
#
# Precedence: analyst > entry-anchored (going-in cap at the modeled price +
# HOUSE_EXIT_CAP_SPREAD, with the rare steal exception) > the exit-year market
# cap rate (only when there is no price to anchor to) > the offering
# memorandum's exit cap (likewise only without an anchor or a market rate) >
# engine default. Every source is recorded as a candidate. The market
# candidates and the steal test live in curves.py; the anchor here.


def _t12_noi(ctx: _Context) -> Optional[Decimal]:
    """Trailing NOI (T12 total income - T12 total opex) from the ingest gate, else None."""
    gate = (ctx.canonical.get("metadata") or {}).get("ingest_gate")
    if not isinstance(gate, Mapping):
        return None
    income, opex = _t12_total(gate, "income"), _t12_total(gate, "opex")
    if income is None or opex is None:
        return None
    return Decimal(str(income)) - Decimal(str(opex))


def _purchase_price(ctx: _Context) -> Optional[float]:
    purchase = ctx.canonical.get("purchase_assumptions")
    return _num(purchase.get("purchase_price")) if isinstance(purchase, Mapping) else None


def entry_anchor(ctx: _Context, market: Optional[Mapping[str, Any]] = None) -> Optional[dict[str, Any]]:
    """The entry-anchored exit cap record at the canonical's price, or None.

    Year-1 NOI comes from the engine at that price; when the engine cannot run
    the canonical (an incomplete or hand-authored one), trailing T12 NOI is
    the anchor instead and the record says so. ``market`` is the exit-year
    market cap rate reference used only for the steal test.
    """
    price = _purchase_price(ctx)
    if not price:
        return None
    noi, noi_basis = _exit_cap_module().year1_noi(ctx.canonical, price), "year-1 NOI"
    if noi is None:
        noi, noi_basis = _t12_noi(ctx), "trailing T12 NOI (the engine could not compute year-1 NOI)"
    record = _exit_cap_module().anchored_exit_cap(
        noi, price, market_cap_rate=(market or {}).get("rate"),
    )
    if record is None:
        return None
    record["noi_basis"] = noi_basis
    record["market"] = deepcopy(dict(market)) if market else None
    return record


def _exit_cap_module():
    from engine.assumptions import exit_cap

    return exit_cap


def _house_exit_cap(ctx: _Context, market: Optional[Mapping[str, Any]] = None) -> Optional[dict[str, Any]]:
    record = entry_anchor(ctx, market)
    if record is None:
        return None
    return _candidate(
        HOUSE_EXIT_CAP_SPREAD,
        record["exit_cap_rate"],
        _exit_cap_module().describe(record, noi_basis=record["noi_basis"]),
    )


def _exit_cap_candidates(ctx: _Context, market: Optional[Mapping[str, Any]] = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    om_cap = _num((ctx.evidence.get("om") or {}).get("exit_cap_rate"))
    if om_cap is not None:
        out.append(_candidate(
            SOURCE_DEAL_OM, om_cap,
            "Exit cap rate extracted from the offering memorandum (a candidate only: the house exit "
            "cap tracks the entry cap).",
        ))
    spread = _house_exit_cap(ctx, market)
    if spread is not None:
        out.append(spread)
    out.append(
        _candidate(
            SOURCE_ENGINE_FALLBACK,
            ENGINE_FALLBACK_EXIT_CAP,
            "backsolve fallback when no exit cap rate is supplied.",
        )
    )
    return out


def _anchored_exit_cap_choice(ctx: _Context, market: Optional[Mapping[str, Any]] = None) -> Optional[_Choice]:
    record = entry_anchor(ctx, market)
    if record is None:
        return None
    return _Choice(
        HOUSE_EXIT_CAP_SPREAD,
        record["exit_cap_rate"],
        _exit_cap_module().describe(record, noi_basis=record["noi_basis"]),
        confidence=_confidence_for(HOUSE_EXIT_CAP_SPREAD, ctx),
        review=True,
        detail=record,
    )


def _exit_cap_default(ctx: _Context, state: str) -> _Choice:
    if state == NO_SECTION:
        return _not_modeled("exit_assumptions")
    anchored = _anchored_exit_cap_choice(ctx)
    if anchored is not None:
        return anchored
    om_cap = _num((ctx.evidence.get("om") or {}).get("exit_cap_rate"))
    if om_cap is not None:
        # No price to anchor to and no market rate (curves.py checks the market first).
        return _Choice(
            SOURCE_DEAL_OM, om_cap,
            "No usable going-in cap rate to anchor to (no purchase price, or year-1 NOI / price outside the "
            "HOUSE_EXIT_CAP_GOING_IN_MAX guard) and no market cap rate: the offering memorandum's exit cap.",
            confidence=_confidence_for(SOURCE_DEAL_OM, ctx),
        )
    if state == ABSENT:
        return _fallback(
            ENGINE_DEFAULT_EXIT_CAP,
            "exit_assumptions has no exit_cap_rate and no usable going-in cap rate (no purchase price, "
            "or year-1 NOI / price outside the HOUSE_EXIT_CAP_GOING_IN_MAX guard): engine default applies.",
        )
    return _not_modeled("exit cap rate")


def _policy_default(name: str) -> float:
    return float(backsolve_policy.DEFAULTS[name])


def _policy_candidates(name: str, engine_default: Optional[float], engine_basis: str):
    def gather(ctx: _Context) -> list[dict[str, Any]]:
        out = [
            _candidate(
                HOUSE_BACKSOLVE_POLICY,
                _policy_default(name),
                f"engine/backsolve_policy.DEFAULTS[{name!r}].",
            )
        ]
        if engine_default is not None:
            out.append(_candidate(SOURCE_ENGINE_FALLBACK, engine_default, engine_basis))
        return out

    return gather


def _policy_default_choice(name: str, section: str, engine_default: float, engine_basis: str, *, ingest_fills: bool):
    """Ingest fills ``ingest_fills`` fields from the policy defaults; others stay at the engine default."""

    def default(ctx: _Context, state: str) -> _Choice:
        if state == NO_SECTION:
            return _not_modeled(section)
        if state == PLACEHOLDER or (ingest_fills and state == PRESENT):
            return _Choice(
                HOUSE_BACKSOLVE_POLICY,
                _policy_default(name),
                f"House default from engine/backsolve_policy.DEFAULTS[{name!r}] "
                f"({_policy_default(name):g}), the value ingest applies when the analyst omits it.",
                confidence="medium",
            )
        return _fallback(engine_default, engine_basis)

    return default


def _closing_default(ctx: _Context, state: str) -> _Choice:
    if state == NO_SECTION:
        return _not_modeled("purchase_assumptions")
    if state == ABSENT:
        return _fallback(0.0, "purchase_assumptions has no purchase price: the engine applies no closing costs.")
    pct = _policy_default("purchase_closing_cost_pct")
    return _Choice(
        HOUSE_BACKSOLVE_POLICY,
        pct,
        f"House purchase closing costs {pct:.2%} of the purchase price (DEFAULT_PURCHASE_CLOSING_COST_PCT, "
        "engine/backsolve_policy.DEFAULTS['purchase_closing_cost_pct']), written to "
        "purchase_assumptions.closing_costs when the canonical carries none.",
        confidence="medium",
    )


def _fee_spec(name: str, section: str, field_name: str, unit: str, *, ingest_fills: bool) -> _Spec:
    engine_basis = f"{section}.{field_name} not set: the engine applies 0."
    return _Spec(
        key=name,
        unit=unit,
        read=_scalar_reader(section, field_name),
        write=_scalar_writer(section, field_name),
        default=_policy_default_choice(name, section, 0.0, engine_basis, ingest_fills=ingest_fills),
        gather=_policy_candidates(name, 0.0, engine_basis),
        patch_paths=((section, field_name),),
    )


# ── loan sizing NOI basis (ADR 0003 §3) ──────────────────────────────────
# A categorical assumption: the cash flow agency loan sizing and DSCR use. The
# canonical's top-level ``loan_sizing_noi_basis``; absent, the engine applies
# the house default, so the default is never written.
LOAN_SIZING_KEY = "loan_sizing_noi_basis"
# The exit NOI window (ADR 0003 decision n): exit_assumptions.noi_window.
EXIT_NOI_WINDOW_KEY = "exit_noi_window"
CATEGORICAL_KEYS = {
    LOAN_SIZING_KEY: house.LOAN_SIZING_NOI_BASES,
    EXIT_NOI_WINDOW_KEY: house.EXIT_NOI_WINDOWS,
}


def _read_loan_sizing_basis(ctx: _Context) -> _Current:
    if LOAN_SIZING_KEY not in ctx.canonical:
        return _Current(ABSENT)
    value = ctx.canonical[LOAN_SIZING_KEY]
    return _Current(PLACEHOLDER) if value is None else _Current(PRESENT, value)


def _write_loan_sizing_basis(ctx: _Context, value: Any, _cohorts: Optional[dict[str, float]]) -> bool:
    ctx.canonical[LOAN_SIZING_KEY] = value
    return True


def _loan_sizing_default(ctx: _Context, state: str) -> _Choice:
    basis = house.HOUSE_LOAN_SIZING_NOI_BASIS
    return _Choice(
        HOUSE_LOAN_SIZING_NOI_BASIS,
        basis,
        f"House loan sizing basis {basis!r} (HOUSE_LOAN_SIZING_NOI_BASIS): agency DSCR sizing and the reported "
        "DSCR use underwritten NCF, NOI less the below-NOI capex charge max(recurring capex, lender reserve), "
        "as agency lenders size on NOI less replacement reserves. The NOI-based DSCR is reported beside it. "
        "Alternatives: 'ncf_after_lender_reserve' (NOI less the lender reserve only) or 'noi_before_capex'.",
        confidence="medium",
    )


def _loan_sizing_candidates(ctx: _Context) -> list[dict[str, Any]]:
    return [_candidate(
        HOUSE_LOAN_SIZING_NOI_BASIS, house.HOUSE_LOAN_SIZING_NOI_BASIS,
        "engine/underwriting_policy.HOUSE_LOAN_SIZING_NOI_BASIS.",
    )]


# ── exit NOI window (ADR 0003 decision n) ────────────────────────────────
# A categorical assumption: the NOI the exit is valued on. The canonical's
# ``exit_assumptions.noi_window``; absent, the engine applies the house default
# (forward), so the default is never written.


def _exit_noi_window_default(ctx: _Context, state: str) -> _Choice:
    window = house.HOUSE_EXIT_NOI_WINDOW
    return _Choice(
        HOUSE_EXIT_NOI_WINDOW,
        window,
        f"House exit NOI window {window!r} (HOUSE_EXIT_NOI_WINDOW): the exit value is the projected NOI for the "
        "12 months after the exit month over the exit cap, as a buyer prices the asset on forward income. The "
        "engine projects that year for NOI only; the hold's cash flows end at the exit. Alternative: 'trailing' "
        "(the 12 months ending at the exit month).",
        confidence="medium",
    )


def _exit_noi_window_candidates(ctx: _Context) -> list[dict[str, Any]]:
    return [_candidate(
        HOUSE_EXIT_NOI_WINDOW, house.HOUSE_EXIT_NOI_WINDOW,
        "engine/underwriting_policy.HOUSE_EXIT_NOI_WINDOW.",
    )]


def _normalize_categorical(key: str, value: Any) -> str:
    allowed = CATEGORICAL_KEYS[key]
    if value not in allowed:
        raise AssumptionOverrideError(f"override for {key!r} must be one of {list(allowed)}, got {value!r}")
    return str(value)


def _opex_slice():
    """ADR 0003 slice 5: per-line opex entries live in engine/assumptions/opex.py."""
    from engine.assumptions import opex

    return opex


# ── ADR 0003 §3 precedence (used only to disclose where it would differ) ─


def _generic_rank(source: str) -> Optional[int]:
    if source.startswith(DEAL_PREFIX):
        return 0
    if source.startswith(MARKET_PREFIX):
        return 1
    if source.startswith(HOUSE_PREFIX):
        return 2
    if source == SOURCE_ENGINE_FALLBACK:
        return 3
    return None


def _market_first_rank(source: str) -> Optional[int]:
    if source.startswith(MARKET_PREFIX):
        return 0
    if source.startswith(DEAL_PREFIX):
        return 1
    if source.startswith(HOUSE_PREFIX):
        return 2
    if source == SOURCE_ENGINE_FALLBACK:
        return 3
    return None


def _growth_rank(source: str) -> Optional[int]:
    # Deal evidence is not used for growth (ADR 0003 §3).
    if source.startswith(DEAL_PREFIX):
        return None
    return _market_first_rank(source)


def _adr_pick(spec: _Spec, candidates: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    rank = spec.adr_rank or _generic_rank
    ranked = [
        (rank(c["source"]), i, c)
        for i, c in enumerate(candidates)
        if c.get("value") is not None and rank(c["source"]) is not None
    ]
    if not ranked:
        return None
    return sorted(ranked, key=lambda item: (item[0], item[1]))[0][2]


# ── key registry ─────────────────────────────────────────────────────────


def _base_specs() -> tuple[_Spec, ...]:
    return (
        _Spec(
            key="rent_growth",
            unit="decimal_yoy",
            read=_scalar_reader("growth_assumptions", "annual_growth_rate"),
            write=_scalar_writer(
                "growth_assumptions",
                "annual_growth_rate",
                create=lambda ctx: {"growth_type": "annual_compound"},
            ),
            default=_rent_growth_default,
            gather=_rent_growth_candidates,
            market_metrics=("rent_growth_forecast",),
            patch_paths=(("growth_assumptions", "annual_growth_rate"),),
            adr_rank=_growth_rank,
        ),
        _Spec(
            key="collection_loss",
            unit="decimal_of_rent",
            read=_read_collection_loss,
            write=_write_collection_loss,
            default=_collection_loss_default,
            gather=_collection_loss_candidates,
            patch_paths=(("collection_loss_curve",),),
        ),
        _Spec(
            key="physical_vacancy",
            unit="decimal_of_units",
            read=_cohort_reader("physical_vacancy_curve", "vacancy_rate"),
            write=_cohort_writer("physical_vacancy_curve", "vacancy_rate"),
            default=_vacancy_default,
            gather=_vacancy_candidates,
            market_metrics=("vacancy_current", "vacancy_stabilized"),
            patch_paths=(("physical_vacancy_curve",),),
            cohort=True,
        ),
        _Spec(
            key="loss_to_lease",
            unit="decimal_of_market_rent",
            read=_cohort_reader("loss_to_lease", "ltl_percent"),
            write=_cohort_writer("loss_to_lease", "ltl_percent"),
            default=_ltl_default,
            gather=_ltl_candidates,
            patch_paths=(("loss_to_lease",),),
            cohort=True,
        ),
        _Spec(
            key="concessions",
            unit="decimal_of_rent",
            read=_read_concessions,
            write=_write_concessions,
            default=_concession_default,
            gather=_concession_candidates,
            market_metrics=("concession_rate",),
            patch_paths=(("concession_schedule",),),
        ),
        _Spec(
            key="insurance_per_unit",
            unit="usd_per_unit_year",
            read=_read_insurance,
            write=_write_insurance,
            default=_opex_default(
                "insurance_annual", "insurance", "No insurance opex line in the canonical: the engine carries none."
            ),
            gather=_insurance_candidates,
            patch_paths=(("opex_table",),),
        ),
        _Spec(
            key="r_and_m_per_unit",
            unit="usd_per_unit_year",
            read=_read_r_and_m,
            write=_write_r_and_m,
            default=_opex_default(
                "repairs_maintenance_annual",
                "R&M including turnover / make-ready (payroll excluded)",
                "No R&M opex lines in the canonical: the engine carries none.",
            ),
            gather=_r_and_m_candidates,
            patch_paths=(("opex_table",),),
        ),
        _Spec(
            # The R&M floor's payroll review: disclosed, never written to an opex
            # line; the guardrail reads it (an analyst value replaces the credit).
            key="r_and_m_payroll_credit",
            unit="usd_per_unit_year",
            read=_read_no_field,
            write=_write_nothing,
            default=_payroll_credit_default,
            gather=_payroll_credit_candidates,
        ),
        _Spec(
            key="replacement_reserve_per_unit",
            unit="usd_per_unit_year",
            read=_read_reserves,
            write=_write_reserves,
            default=_reserve_default,
            gather=_reserve_candidates,
            patch_paths=(("replacement_reserves",),),
        ),
        _Spec(
            key="immediate_capex",
            unit="usd",
            read=_read_immediate_capex,
            write=_write_immediate_capex,
            default=_immediate_capex_default,
            gather=_immediate_capex_candidates,
            patch_paths=(("capital_plan", "immediate_capex"),),
            evidence_first=True,
        ),
        _Spec(
            key="recurring_capex_per_unit",
            unit="usd_per_unit_year",
            read=_read_recurring_capex,
            write=_write_recurring_capex,
            default=_recurring_capex_default,
            gather=_recurring_capex_candidates,
            patch_paths=(("capital_plan", "recurring_capex", "per_unit_annual"),),
            evidence_first=True,
        ),
        _Spec(
            key="recurring_capex_growth",
            unit="decimal_yoy",
            read=_read_recurring_growth,
            write=_write_recurring_growth,
            default=_recurring_growth_default,
            gather=_recurring_growth_candidates,
            patch_paths=(("capital_plan", "recurring_capex", "growth_rate"),),
        ),
        _Spec(
            key="turn_cost_per_unit",
            unit="usd_per_turn",
            read=_read_no_field,
            write=_write_nothing,
            default=_turn_default,
            gather=_turn_candidates,
            value_add_only=True,
        ),
        _Spec(
            key="renovation_premium",
            unit="usd_per_unit_month",
            read=_read_renovation_premium,
            write=_write_renovation_premium,
            default=_renovation_premium_default,
            gather=_renovation_premium_candidates,
            market_metrics=("renovation_premium",),
            patch_paths=(("renovation_programs",),),
            adr_rank=_no_adr,
            value_add_only=True,
        ),
        _Spec(
            key="exit_cap_rate",
            unit="decimal",
            read=_scalar_reader("exit_assumptions", "exit_cap_rate"),
            write=_scalar_writer("exit_assumptions", "exit_cap_rate", create=_new_exit_section),
            default=_exit_cap_default,
            gather=_exit_cap_candidates,
            market_metrics=("market_cap_rate",),
            patch_paths=(("exit_assumptions", "exit_cap_rate"),),
            adr_rank=_market_first_rank,
            price_linked=True,
        ),
        _Spec(
            key="sale_cost_percent",
            unit="decimal_of_sale_price",
            read=_scalar_reader("exit_assumptions", "sale_cost_percent"),
            write=_scalar_writer("exit_assumptions", "sale_cost_percent"),
            default=_policy_default_choice(
                "sale_cost_percent",
                "exit_assumptions",
                ENGINE_DEFAULT_SALE_COST,
                "exit_assumptions.sale_cost_percent not set: engine default applies.",
                ingest_fills=True,
            ),
            gather=_policy_candidates(
                "sale_cost_percent",
                ENGINE_DEFAULT_SALE_COST,
                "Engine default when exit_assumptions omits sale_cost_percent.",
            ),
            patch_paths=(("exit_assumptions", "sale_cost_percent"),),
        ),
        _fee_spec("acquisition_fee_pct", "fund_assumptions", "acquisition_fee_pct",
                  "decimal_of_purchase_price", ingest_fills=True),
        _fee_spec("asset_management_fee_pct", "fund_assumptions", "asset_management_fee_pct",
                  "decimal_per_year", ingest_fills=True),
        _fee_spec("disposition_fee_pct", "fund_assumptions", "disposition_fee_pct",
                  "decimal_of_sale_price", ingest_fills=True),
        _fee_spec("annual_partnership_expenses", "fund_assumptions", "annual_partnership_expenses",
                  "usd_per_year", ingest_fills=False),
        _fee_spec("partnership_closing_costs", "fund_assumptions", "partnership_closing_costs",
                  "usd", ingest_fills=False),
        _Spec(
            key="purchase_closing_cost_pct",
            unit="decimal_of_purchase_price",
            read=_read_purchase_closing,
            write=_write_purchase_closing,
            default=_closing_default,
            gather=_policy_candidates(
                "purchase_closing_cost_pct",
                0.0,
                "No closing costs: the engine applies none.",
            ),
            patch_paths=(("purchase_assumptions", "closing_costs"),),
        ),
        _fee_spec("loan_closing_costs", "debt_terms", "loan_closing_costs", "usd", ingest_fills=False),
        _Spec(
            key=LOAN_SIZING_KEY,
            unit="enum",
            read=_read_loan_sizing_basis,
            write=_write_loan_sizing_basis,
            default=_loan_sizing_default,
            gather=_loan_sizing_candidates,
            patch_paths=((LOAN_SIZING_KEY,),),
        ),
        _Spec(
            key=EXIT_NOI_WINDOW_KEY,
            unit="enum",
            read=_scalar_reader("exit_assumptions", "noi_window"),
            write=_scalar_writer("exit_assumptions", "noi_window"),
            default=_exit_noi_window_default,
            gather=_exit_noi_window_candidates,
            patch_paths=(("exit_assumptions", "noi_window"),),
        ),
    )


# ── ADR 0003 slice 4 hook: curve keys (engine/assumptions/curves.py) ──────


def _curves():
    from engine.assumptions import curves  # lazy: curves imports this module

    return curves


def _specs() -> tuple[_Spec, ...]:
    return _curves().apply(_base_specs())


# ── end slice 4 hook ─────────────────────────────────────────────────────


ASSUMPTION_KEYS = tuple(spec.key for spec in _specs())
# Keys whose override may carry ``cohort_overrides: {cohort_id: value}`` (a
# per-cohort flat curve); every other key rejects them.
COHORT_OVERRIDE_KEYS = frozenset(spec.key for spec in _specs() if spec.cohort)
# Marker a normalized override carries when the caller passed ``null``.
CLEAR = "clear"


def _specs_for(profile: DealProfile) -> tuple[_Spec, ...]:
    return tuple(
        spec for spec in _specs() if not spec.value_add_only or profile.deal_type == "value_add"
    )


# ── market candidates ────────────────────────────────────────────────────


def _market_candidates(ctx: _Context, spec: _Spec) -> list[dict[str, Any]]:
    # Slice 4: market lookups go through the registry with a Geo (curves.py).
    return _curves().market_candidates(ctx, spec)


# ── overrides ────────────────────────────────────────────────────────────


def is_clear(override: Optional[Mapping[str, Any]]) -> bool:
    """Whether a normalized override is the "clear this key's override" marker."""
    return isinstance(override, Mapping) and override.get(CLEAR) is True


def _normalize_override(key: str, raw: Any, *, note: str = "analyst override") -> dict[str, Any]:
    if isinstance(raw, Mapping):
        if "value" not in raw:
            raise AssumptionOverrideError(f"override for {key!r} must carry a value")
        unknown = set(raw) - {"value", "note", "cohort_overrides", *OVERRIDE_METADATA_FIELDS}
        if unknown:
            raise AssumptionOverrideError(f"override for {key!r} has unknown fields: {sorted(unknown)}")
        value, note = raw["value"], raw.get("note") or note
        cohorts = raw.get("cohort_overrides")
        try:  # optional edit metadata (edited_by / edited_at), kept on the override as given
            metadata = override_metadata(raw)
        except ValueError as exc:
            raise AssumptionOverrideError(f"override for {key!r}: {exc}") from None
    else:
        value, cohorts, metadata = raw, None, {}
    if value is None:
        # null clears the key's override: the entry reverts to its resolved default.
        return {CLEAR: True, "note": note}
    if cohorts and key not in COHORT_OVERRIDE_KEYS:
        raise AssumptionOverrideError(
            f"{key!r} does not support cohort_overrides; only {sorted(COHORT_OVERRIDE_KEYS)} do"
        )
    # Slice 4: a scalar, a per-year vector, or the curve shape the key writes;
    # a categorical key takes one of its named values.
    if key in CATEGORICAL_KEYS:
        value = _normalize_categorical(key, value)
    else:
        value = _curves().normalize_override_value(key, value)
    out: dict[str, Any] = {"value": value, "note": note}
    if cohorts:
        if not isinstance(value, float):
            raise AssumptionOverrideError(f"cohort_overrides for {key!r} need a scalar value")
        if not isinstance(cohorts, Mapping):
            raise AssumptionOverrideError(f"cohort_overrides for {key!r} must be an object")
        cleaned = {}
        for cohort_id, cohort_value in cohorts.items():
            cohort_number = _num(cohort_value)
            if cohort_number is None or not math.isfinite(cohort_number):
                raise AssumptionOverrideError(f"cohort override {key}.{cohort_id} must be a finite number")
            cleaned[str(cohort_id)] = cohort_number
        out["cohort_overrides"] = cleaned
    out.update(metadata)
    return out


def normalize_overrides(overrides: Optional[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Validate ``{key: value | {"value": ..., "note": ...}}`` and return the dict form.

    A ``null`` value (``{key: None}`` or ``{key: {"value": None}}``) becomes
    ``{"clear": True, "note": ...}``: resolving with it drops the key's
    recorded override, so the entry reverts to its resolved default and the
    default's source. ``cohort_overrides`` are accepted only for
    ``COHORT_OVERRIDE_KEYS`` (physical_vacancy, loss_to_lease); other keys
    raise ``AssumptionOverrideError``.
    """
    if not overrides:
        return {}
    if not isinstance(overrides, Mapping):
        raise AssumptionOverrideError("assumption overrides must be an object keyed by assumption key")
    # Retired keys (economic_capex_per_unit) map to the key that replaced them.
    overrides = {KEY_ALIASES.get(key, key): raw for key, raw in overrides.items()}
    # ── ADR 0003 slice 5 hook: per-line opex growth keys (opex.<slug>.growth) ──
    opex_keys = _opex_slice().check_override_keys(overrides)
    unknown = sorted(set(overrides) - set(ASSUMPTION_KEYS) - opex_keys)
    if unknown:
        raise AssumptionOverrideError(
            f"unknown assumption keys {unknown}; known keys: {list(ASSUMPTION_KEYS)}"
        )
    return {key: _normalize_override(key, raw) for key, raw in overrides.items()}


def overrides_from_patch(
    patch: Mapping[str, Any],
    canonical: Mapping[str, Any],
    *,
    note: str = "supplied by a partial canonical patch",
) -> dict[str, dict[str, Any]]:
    """Analyst overrides for every assumption a partial canonical patch touches.

    ``canonical`` is the canonical after the patch was merged; the override
    value is what the merged canonical now carries for the key.
    """
    profile = _profile_for(canonical, None)
    ctx = _context(deepcopy(dict(canonical)), profile, "underwrite", None)
    out: dict[str, dict[str, Any]] = {}
    for spec in _specs_for(profile):
        if not any(_get_path(patch, path)[0] for path in spec.patch_paths):
            continue
        current = spec.read(ctx)
        if current.state != PRESENT or current.value is None:
            continue
        try:  # slice 4: vectors and curve objects are valid override values too
            _curves().normalize_override_value(spec.key, current.value)
        except AssumptionOverrideError:
            continue
        override: dict[str, Any] = {"value": current.value, "note": note}
        if current.cohort_overrides:
            override["cohort_overrides"] = dict(current.cohort_overrides)
        out[spec.key] = override
    # ── ADR 0003 slice 5 hook: growth rates a patched opex_table carries ──
    out.update(_opex_slice().overrides_from_patch(patch, ctx, note))
    return out


# ── resolution ───────────────────────────────────────────────────────────


def _normalize_year(value: Any) -> Optional[int]:
    from engine.ingest.om_parser import _normalize_year_built

    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return _normalize_year_built(int(value))
    except (TypeError, ValueError):
        return None


def _profile_for(canonical: Mapping[str, Any], deal_profile: Any) -> DealProfile:
    if isinstance(deal_profile, DealProfile):
        profile = deal_profile
    elif isinstance(deal_profile, Mapping):
        check_deal_type(deal_profile.get("deal_type", "stabilized"))
        profile = DealProfile.from_dict(deal_profile)
    elif deal_profile is None and isinstance(canonical.get("deal_profile"), Mapping):
        stored = canonical["deal_profile"]
        check_deal_type(stored.get("deal_type"))
        profile = DealProfile.from_dict(stored)
    elif deal_profile is None:
        profile = DealProfile()
    else:
        raise TypeError("deal_profile must be a DealProfile, a mapping, or None")
    return profile


def _year_built(canonical: Mapping[str, Any], profile: DealProfile) -> tuple[Optional[int], Optional[str]]:
    metadata = canonical.get("metadata") or {}
    property_summary = metadata.get("property_summary") or {}
    for value, origin in (
        (profile.year_built, "deal_profile.year_built"),
        (metadata.get("year_built"), "metadata.year_built"),
        (property_summary.get("year_built"), "metadata.property_summary.year_built"),
    ):
        year = _normalize_year(value)
        if year is not None:
            return year, origin
    return None, None


def _context(canonical: dict[str, Any], profile: DealProfile, mode: str, market: Any) -> _Context:
    year, origin = _year_built(canonical, profile)
    metadata = canonical.get("metadata") or {}
    gate = metadata.get("ingest_gate")
    evidence = dict(gate.get("evidence") or {}) if isinstance(gate, Mapping) else {}
    year_evidence = evidence.get("year_built") or {}
    if year is not None and year_evidence.get("value") == year and year_evidence.get("origin"):
        origin = str(year_evidence["origin"])
    time_grid = canonical.get("time_grid") or {}
    return _Context(
        canonical=canonical,
        profile=profile,
        mode=mode,
        market=market,
        year_built=year,
        year_built_origin=origin,
        bucket=house.vintage_bucket(year),
        ingested=isinstance(gate, Mapping),
        evidence=evidence,
        units=sum_units(canonical),
        start=str(time_grid.get("analysis_start_date", ""))[:7],
        end=str(time_grid.get("analysis_end_date", ""))[:7],
        om_confidence=metadata.get("om_extraction_confidence"),
    )


def default_provenance(default: _Choice) -> dict[str, Any]:
    """Where an overridden entry's default came from (kept on its analyst candidate)."""
    return {
        "source": default.source,
        "basis": default.basis,
        "as_of": default.as_of,
        "confidence": default.confidence,
    }


def _same_current(current: _Current, prior: Mapping[str, Any]) -> bool:
    return _same(current.value, prior.get("value")) and _same(
        current.cohort_overrides or None, prior.get("cohort_overrides") or None
    )


def _resolve_key(
    spec: _Spec,
    ctx: _Context,
    prior: Optional[Mapping[str, Any]],
    explicit: Optional[dict[str, Any]],
) -> AssumptionEntry:
    current = spec.read(ctx)
    default = spec.default(ctx, current.state)
    candidates = spec.gather(ctx) + _market_candidates(ctx, spec)
    if not spec.price_linked:
        # Slice 4 hook: re-resolving without market data keeps a market-sourced default.
        # A price-linked default is always re-measured (its market reference is kept
        # on the entry's detail instead).
        default, candidates = _curves().carry_market_default(ctx, current, prior, default, candidates)

    override: Optional[dict[str, Any]] = None
    cleared = is_clear(explicit)
    if cleared:
        pass  # null override: drop any recorded or edited override, use the default
    elif explicit is not None:
        override = deepcopy(explicit)
    elif prior is not None:
        edited = current.state == PRESENT and not _same_current(current, prior)
        if (
            edited and spec.price_linked and not prior.get("override")
            and _same(current.value, default.value)
        ):
            # The value moved with the price (e.g. backsolve re-anchored the exit
            # cap at the solved price): it is still the resolver default.
            edited = False
        if edited:
            override = {"value": current.value, "note": _NOTE_EDITED}
            if current.cohort_overrides:
                override["cohort_overrides"] = dict(current.cohort_overrides)
        elif prior.get("override"):
            override = deepcopy(dict(prior["override"]))
    elif current.state == PRESENT:
        # An informational key's default is never written, so a value present is the analyst's.
        is_default = (
            ctx.ingested and not spec.informational
            and _same(current.value, default.value) and not current.cohort_overrides
        )
        if not is_default and spec.evidence_first and default.source.startswith(DEAL_PREFIX):
            # Deal evidence outranks the analyst value already in the canonical
            # (PCA > analyst > house); the analyst value stays a candidate.
            candidates = [_candidate(
                SOURCE_ANALYST, current.value,
                f"{_SUPERSEDED} {default.source} evidence (precedence: PCA > analyst > house).",
            )] + candidates
        elif not is_default:
            override = {"value": current.value, "note": _NOTE_PRESENT}
            if current.cohort_overrides:
                override["cohort_overrides"] = dict(current.cohort_overrides)

    if spec.evidence_first and override is None and prior is not None and default.source.startswith(DEAL_PREFIX):
        # Keep the record of an analyst value the deal evidence superseded.
        kept = [
            c for c in prior.get("candidates") or []
            if c.get("source") == SOURCE_ANALYST and str(c.get("basis", "")).startswith(_SUPERSEDED)
        ]
        if kept and not any(c.get("source") == SOURCE_ANALYST for c in candidates):
            candidates = deepcopy(kept) + candidates

    notes: list[str] = list(ctx.notes.get(spec.key, []))
    if override is not None:
        value = override["value"]
        cohorts = override.get("cohort_overrides") or None
        source, confidence, as_of = SOURCE_ANALYST, _confidence_for(SOURCE_ANALYST, ctx), None
        basis = (
            f"Analyst override ({override.get('note') or 'analyst override'}); the resolver default is "
            f"{_fmt(default.value, spec.unit)} from {default.source}."
        )
        # The analyst candidate records the default's provenance, so a later
        # re-resolution without market data can clear the override back to it.
        candidates = [
            _candidate(
                SOURCE_ANALYST, value, override.get("note") or "analyst override",
                default=default_provenance(default),
            )
        ] + candidates
    else:
        value, cohorts = default.value, None
        source, confidence, as_of = default.source, default.confidence, default.as_of
        basis = default.basis
        pick = _adr_pick(spec, candidates) if value is not None else None
        if pick is not None and not _same(pick["value"], value):
            notes.append(
                f"ADR 0003 precedence would select {pick['source']} "
                f"({_fmt(pick['value'], spec.unit)}); today's {_fmt(value, spec.unit)} is kept "
                "because the resolver does not fill this field outside pricing."
            )

    write_needed = False
    if spec.informational:
        # Only an analyst value is written. Without one, an opt-in left in the
        # canonical (a cleared override) is removed.
        if override is not None:
            write_needed = current.state != PRESENT or not _same(current.value, value)
        elif current.state == PRESENT and not spec.write(ctx, None, None):
            notes.append("Not written: the canonical has no field the engine reads for this value.")
    elif current.state == PLACEHOLDER:
        write_needed = True
    elif current.state == NO_FIELD:
        write_needed = False
    elif cleared and value is None:
        notes.append("No resolved default exists, so the canonical value is kept.")
    elif override is not None:
        write_needed = current.state != PRESENT or not _same_current(
            current, {"value": value, "cohort_overrides": cohorts}
        )
    elif current.state == PRESENT and not _same(current.value, value):
        write_needed = True
    if write_needed and not spec.write(ctx, value, cohorts):
        notes.append("Not written: the canonical has no field the engine reads for this value.")

    if notes:
        basis = f"{basis} {' '.join(notes)}"

    return AssumptionEntry(
        value=_jsonable(value),
        unit=spec.unit,
        source=source,
        basis=basis,
        as_of=as_of,
        confidence=confidence,
        candidates=candidates,
        default_value=_jsonable(default.value),
        override=_jsonable(override),
        review_required=(
            requires_review(source) or (override is None and default.review is True) or default.review_always
        ),
        cohort_overrides=_jsonable(cohorts),
        detail=_jsonable(default.detail) if default.detail else None,
    )


# ── ADR 0003 slice 6: guardrail modes ────────────────────────────────────


def _apply_guardrail_mode(
    ctx: _Context,
    specs: tuple[_Spec, ...],
    entries: dict[str, Any],
    prior_entries: Mapping[str, Any],
    context: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """Warn (underwrite) or enforce (pricing) the house floors on the resolved canonical."""
    from engine.assumptions import guardrails

    context = dict(context or {})
    enforced = context.pop("enforced", None)
    # The guardrails read who set a value (an analyst recurring capex below the
    # vintage floor is waived and disclosed; a PCA one is warned, or raised in
    # pricing) from these entries.
    context["assumption_entries"] = entries
    credit = entries.get("r_and_m_payroll_credit")
    if isinstance(credit, dict) and credit.get("source") == SOURCE_ANALYST:
        # The review the analyst's credit replaces: disclosed with that credit.
        credit["detail"] = _jsonable(_payroll_review(ctx, credit.get("value")))
    if ctx.mode == guardrails.MODE_PRICING and enforced is not None:
        findings = list(enforced)
    else:
        profile = guardrails.GuardrailProfile(
            strategy=ctx.strategy, year_built=ctx.year_built,
            deferred_maintenance=ctx.profile.deferred_maintenance,
        )
        findings = guardrails.enforce_floors(ctx.canonical, mode=ctx.mode, profile=profile, context=context)
        if any(f["action"] in guardrails.ENFORCING_ACTIONS for f in findings):
            # Re-read the raised values, with the first pass as the prior resolution.
            # Keys outside the specs (opex.*) are kept; the caller re-reads them.
            entries = {
                **entries,
                **{spec.key: _resolve_key(spec, ctx, entries[spec.key], None).to_dict() for spec in specs},
            }
    exemptions = guardrails.ltl_exemptions(
        ctx.canonical, guardrails.GuardrailProfile(strategy=ctx.strategy, year_built=ctx.year_built), context,
        strict=False,
    )
    return guardrails.record_on_entries(entries, findings, mode=ctx.mode, prior_entries=prior_entries,
                                        exemptions=exemptions)


def resolve_assumptions(
    canonical: Mapping[str, Any],
    *,
    deal_profile: DealProfile | Mapping[str, Any] | None = None,
    overrides: Optional[Mapping[str, Any]] = None,
    mode: str = "underwrite",
    market: Any = None,
    guardrail_context: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Resolve every v1 assumption key and return a NEW canonical.

    The input is never mutated. The result carries ``deal_profile`` and an
    ``assumptions`` section (provenance, candidates, overrides) and has the
    resolved values written into the canonical fields the engine reads.

    Args:
        canonical: Canonical deal inputs (schema v0.1).
        deal_profile: Profile to resolve with; defaults to the canonical's
            ``deal_profile``, else a stabilized profile. ``lease_up`` and
            ``distressed`` raise ``UnsupportedDealTypeError``.
        overrides: ``{key: value | {"value": ..., "note": ...}}``. An overridden
            entry keeps ``default_value``, records ``override``, and its value
            wins. Overrides already recorded in the canonical are preserved.
            A ``null`` value (``{key: None}`` / ``{key: {"value": None}}``)
            clears the key's override: the entry reverts to its resolved
            default and that default's source. ``cohort_overrides`` are
            supported by ``physical_vacancy`` and ``loss_to_lease`` only
            (``COHORT_OVERRIDE_KEYS``); on any other key they raise
            ``AssumptionOverrideError``. ``opex.<slug>.base`` takes annual
            dollars (the percent for a percent-of-revenue line); ``0``
            suppresses the line.
        mode: ``"underwrite"`` or ``"pricing"`` (ADR 0003 §5). Underwrite
            never changes a value for a house floor; each value below its
            floor gets a ``guardrail`` record (``action: "warned"``). Pricing
            raises it to the floor (per segment for curves) and records
            ``action: "enforced"``. See ``engine/assumptions/guardrails.py``.
        market: Optional ``engine.market_data`` registry (anything exposing
            ``resolve(metric, Geo, as_of=None) -> (observation | None, candidates)``).
            The deal profile is converted to a ``Geo``. Market observations feed
            rent_growth, physical_vacancy, concessions and exit_cap_rate under the
            ADR 0003 §3 precedence (``engine/assumptions/curves.py``) and every
            observation is recorded as a candidate. Missing data falls through.
        guardrail_context: Passed to the guardrails (``broker_snapshot``,
            ``trailing_actuals``, ``insurance_per_unit_override``). In pricing
            mode, ``enforced`` lists findings a caller (backsolve) already
            enforced: they are recorded and no floor is enforced again.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    out = deepcopy(dict(canonical))
    explicit = normalize_overrides(overrides)
    profile = _profile_for(out, deal_profile)
    ctx = _context(out, profile, mode, market)
    profile = profile.with_year_built(ctx.year_built)
    ctx.profile = profile

    prior_section = out.get("assumptions") if isinstance(out.get("assumptions"), Mapping) else {}
    prior_entries = prior_section.get("entries") if isinstance(prior_section.get("entries"), Mapping) else {}
    ctx.prior_entries = prior_entries

    specs = _specs_for(profile)
    opex_slice = _opex_slice()
    inapplicable = sorted(
        key for key in set(explicit) - {spec.key for spec in specs} if not opex_slice.is_opex_key(key)
    )
    if inapplicable:
        raise AssumptionOverrideError(
            f"assumption keys {inapplicable} do not apply to deal_type {profile.deal_type!r}"
        )

    # ── ADR 0003 slice 5 hook: apply opex line overrides and add missing core
    # opex lines before the v1 keys read them. It returns the overrides its
    # row changes imply (an explicitly deleted line kept at $0, the
    # insurance / R&M per-unit keys an opex base override moved); the
    # caller's own overrides win.
    explicit = {**opex_slice.prepare_opex_table(ctx, explicit), **explicit}
    entries: dict[str, Any] = {}
    for spec in specs:
        if spec.price_linked:
            continue
        entry = _resolve_key(spec, ctx, prior_entries.get(spec.key), explicit.get(spec.key))
        entries[spec.key] = entry.to_dict()
    # ── ADR 0003 slice 5 hook: opex.<slug>.base / opex.<slug>.growth per opex line ──
    opex_entries = opex_slice.resolve_opex_entries(ctx, prior_entries, explicit)
    # Price-linked keys (the entry-anchored exit cap) read year-1 NOI, so they
    # resolve after every value that feeds it has been written.
    for spec in specs:
        if spec.price_linked:
            entries[spec.key] = _resolve_key(spec, ctx, prior_entries.get(spec.key), explicit.get(spec.key)).to_dict()
    entries = {spec.key: entries[spec.key] for spec in specs}
    entries.update(opex_entries)

    # ── ADR 0003 slice 6 hook: guardrail modes (engine/assumptions/guardrails.py) ──
    entries = _apply_guardrail_mode(ctx, specs, entries, prior_entries, guardrail_context)
    # Pricing floors can add opex lines (e.g. the R&M vintage floor top-up);
    # re-read the opex entries so a second resolution is identical. The first
    # pass is the prior: a value it wrote is not mistaken for an analyst value.
    entries.update(opex_slice.resolve_opex_entries(ctx, {**prior_entries, **entries}, explicit))
    # Keep the labels backsolve gave the values its pricing set (house:backsolve_policy, ...).
    from engine.assumptions.guardrails import restore_pricing_labels
    restore_pricing_labels(entries, prior_entries)
    # ── end slice 6 hook ──

    # No silent market fallback: say which provider had what, and append the
    # reason to every market-reading entry no provider supplied.
    status = _market_data_status(ctx, specs, entries, prior_section.get("market_data_status"))

    out["deal_profile"] = profile.to_dict()
    out["assumptions"] = {
        "resolver_version": RESOLVER_VERSION,
        "mode": mode,
        "entries": entries,
    }
    if status is not None:
        out["assumptions"]["market_data_status"] = status
    return out


def _carries_market_provenance(entry: Mapping[str, Any]) -> bool:
    """Whether an entry still records market data (a kept candidate or the exit cap's reference)."""
    if any(str(c.get("source", "")).startswith(MARKET_PREFIX)
           for c in entry.get("candidates") or [] if isinstance(c, Mapping)):
        return True
    detail = entry.get("detail")
    market = detail.get("market") if isinstance(detail, Mapping) else None
    return isinstance(market, Mapping) and str(market.get("source", "")).startswith(MARKET_PREFIX)


def _market_data_status(
    ctx: _Context,
    specs: tuple[_Spec, ...],
    entries: dict[str, Any],
    prior: Any,
) -> Optional[dict[str, Any]]:
    """The market-data status block (``engine.market_data.diagnostics``), or None.

    With a registry the block is measured now (from the per-run lookups the
    keys already made). Without one (backsolve's re-resolution, overrides
    applied with ``--no-market-data``) the last recorded block is kept. Either
    way, each market-reading entry no provider supplied, and whose value is
    not market-sourced, gets the block's fallback note on its basis and a
    ``detail.market_data`` record.
    """
    from engine.market_data.diagnostics import MSA_ONLY, market_data_status

    curves = _curves()
    if ctx.market is not None:
        slots = {spec.key: spec.market_metrics for spec in specs if spec.market_metrics}
        status = market_data_status(
            ctx.market,
            curves.profile_geo(ctx.profile),
            slots=slots,
            lookup=lambda metric: curves.market_observations(ctx, metric, msa_only=metric in MSA_ONLY),
        )
    elif isinstance(prior, Mapping):
        status = deepcopy(dict(prior))
    else:
        return None
    for key, record in (status.get("slots") or {}).items():
        entry = entries.get(key)
        if not isinstance(entry, dict) or not isinstance(record, dict):
            continue
        record["entry_source"] = entry.get("source")
        note = record.get("note")
        source = str(entry.get("source", ""))
        # Deal evidence and analyst values outrank market data: only a house or
        # engine fallback stands where market data would have.
        if not note or not (source.startswith(HOUSE_PREFIX) or source == SOURCE_ENGINE_FALLBACK):
            continue
        if _carries_market_provenance(entry):
            # A market value or reference kept from an earlier resolution.
            record["kept_from_prior_resolution"] = True
            continue
        if note not in str(entry.get("basis", "")):
            entry["basis"] = f"{entry.get('basis', '')} {note}".strip()
        detail = dict(entry.get("detail") or {})
        detail["market_data"] = {"supplied": False, "metrics": list(record.get("metrics") or []), "note": note}
        entry["detail"] = detail
    return status
