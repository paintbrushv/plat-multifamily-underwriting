"""House guardrail floors and the two guardrail modes (ADR 0003 §5, slice 6).

One place defines the house floors for a (deal_type -> strategy, year_built)
profile: physical vacancy (the property-level vacancy anchor), loss to lease,
collection loss, year-1 concessions, insurance per unit, R&M per unit (floor
and top-up) and replacement reserves per unit. The vintage values come from
``engine/underwriting_policy.py``; deal evidence passed in ``context`` (broker
notes, trailing actuals) can raise a floor, exactly as backsolve has always
done.

Two modes:

- ``underwrite``: values are never changed. Each value below its floor yields
  a finding with ``action: "warned"``, recorded on the matching
  ``assumptions.entries[key].guardrail``. The validator turns those into
  ``GUARDRAIL_BELOW_FLOOR`` warnings.
- ``pricing``: each value below its floor is raised to it. Curves are floored
  per segment, so the analyst's curve shape is kept; scalars use
  ``max(value, floor)``. Every raise is a finding with ``action: "enforced"``
  and an original/applied pair, appended to ``context["policy_overrides"]``
  when the caller supplies that list.

``enforce_floors`` works in place on the canonical fields only; backsolve uses
it. ``apply_guardrails`` is the public, copy-returning entry point that also
updates the ``assumptions`` section. ``resolve_assumptions(mode=...)`` calls
into this module through one delimited hook.
"""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional

from engine import underwriting_policy as house
from engine.assumptions.canonical_ops import (
    annual_opex_amount,
    concession_pct_rent,
    matching_opex_rows,
    sum_opex_bucket,
    sum_units,
    upsert_annual_opex,
    upsert_annual_opex_by_match,
)
from engine.assumptions.profile import DEAL_TYPE_STRATEGY, DealProfile, check_deal_type
from engine.assumptions.registry import (
    HOUSE_BACKSOLVE_POLICY,
    HOUSE_PREFIX,
    HOUSE_PRICING_NORMALIZATION,
    HOUSE_YEAR1_INSURANCE_FLOOR,
    MODES,
    SOURCE_ANALYST,
    SOURCE_DEAL_OM,
    SOURCE_DEAL_RENT_ROLL,
    SOURCE_DEAL_T12,
    house_vintage_source,
    market_source,
    requires_review,
)
from engine.backsolve_policy import validate_benchmark
from engine.modules.util import dec

MODE_UNDERWRITE = "underwrite"
MODE_PRICING = "pricing"
STATUS_BELOW_FLOOR = "below_floor"
ACTION_WARNED = "warned"
ACTION_ENFORCED = "enforced"
# An explicit analyst insurance figure below the insurance floor: the value is
# kept (no price change) and the waiver is disclosed.
ACTION_WAIVED = "floor_waived_by_analyst_override"

STRATEGIES = ("cashflow", "value_add")
STRATEGY_DEAL_TYPE = {strategy: deal_type for deal_type, strategy in DEAL_TYPE_STRATEGY.items()}

# Keys in the order they are checked (and their findings listed).
GUARDRAIL_KEYS = (
    "physical_vacancy",
    "loss_to_lease",
    "collection_loss",
    "concessions",
    "insurance_per_unit",
    "r_and_m_per_unit",
    "replacement_reserve_per_unit",
)
CURVE_KEYS = frozenset({
    "physical_vacancy", "loss_to_lease", "collection_loss", "concessions", "replacement_reserve_per_unit",
})

# canonical input each key's findings refer to (also the policy_overrides "input").
GUARDRAIL_INPUTS = {
    "physical_vacancy": "physical_vacancy_curve",
    "loss_to_lease": "loss_to_lease",
    "collection_loss": "collection_loss_curve",
    "concessions": "concession_schedule",
    "insurance_per_unit": "opex_table.insurance",
    "r_and_m_per_unit": "opex_table.repairs_maintenance",
    "replacement_reserve_per_unit": "replacement_reserves",
}
_UNITS = {
    "physical_vacancy": "decimal",
    "loss_to_lease": "decimal",
    "collection_loss": "decimal",
    "concessions": "decimal",
    "insurance_per_unit": "usd",
    "r_and_m_per_unit": "usd",
    "replacement_reserve_per_unit": "usd",
}

R_AND_M_TOP_UP_CATEGORY = "Repairs & Maintenance Vintage Floor Top-Up"
# The year-1 pct_rent row the concessions floor adds when no row covers year 1.
YEAR1_CONCESSION_FLOOR_ID = "house_year1_concessions"
SOURCE_HOUSE_VACANCY_ANCHOR = "house:vacancy_anchor"
SOURCE_SUPPLIED_MARKET_VACANCY = market_source("supplied_benchmark")

# Note on the override that pins a value the pricing guardrail raised, so a
# later re-resolution keeps the priced value instead of the resolver default.
GUARDRAIL_OVERRIDE_NOTE = "pinned at the house floor enforced by the pricing guardrail"

# Notes on the overrides that pin values backsolve's pricing set outside the
# shared floors (see ``label_pricing_entry``). Each names who set the value, so
# the entry is never shown as an analyst edit of the canonical.
PRICING_POLICY_NOTE = "set by the backsolve pricing policy"
PRICING_NORMALIZATION_NOTE = "rewritten by a backsolve pricing normalization"
PRICING_FLOOR_LINE_NOTE = "opex line set by a house floor the pricing guardrail enforced"
PRICING_PROPERTY_TAX_NOTE = "reassessed by the property tax module at the priced purchase price"
PRICING_INSURANCE_OVERRIDE_NOTE = "analyst insurance_per_unit_override in the backsolve policy"
PRICING_LABEL_NOTES = frozenset({
    PRICING_POLICY_NOTE,
    PRICING_NORMALIZATION_NOTE,
    PRICING_FLOOR_LINE_NOTE,
    PRICING_PROPERTY_TAX_NOTE,
    PRICING_INSURANCE_OVERRIDE_NOTE,
})
SOURCE_HOUSE_BACKSOLVE_POLICY = HOUSE_BACKSOLVE_POLICY
SOURCE_HOUSE_PRICING_NORMALIZATION = HOUSE_PRICING_NORMALIZATION

_VACANCY_ANCHOR_SOURCES = {
    "broker_om": SOURCE_DEAL_OM,
    "rent_roll_vacant": SOURCE_DEAL_RENT_ROLL,
    "t12_vacancy_gpr": SOURCE_DEAL_T12,
    "supplied_market_benchmark": SOURCE_SUPPLIED_MARKET_VACANCY,
}


# ── profile ──────────────────────────────────────────────────────────────


def _normalize_year(value: Any) -> Optional[int]:
    from engine.ingest.om_parser import _normalize_year_built

    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return _normalize_year_built(int(value))
    except (TypeError, ValueError):
        return None


def canonical_year_built(canonical: Mapping[str, Any]) -> Optional[int]:
    """Year built recorded in the canonical (deal_profile, then metadata)."""
    metadata = canonical.get("metadata") or {}
    for value in (
        (canonical.get("deal_profile") or {}).get("year_built"),
        metadata.get("year_built"),
        (metadata.get("property_summary") or {}).get("year_built"),
    ):
        year = _normalize_year(value)
        if year is not None:
            return year
    return None


@dataclass(frozen=True)
class GuardrailProfile:
    """The (strategy, vintage) the house floors are looked up for."""

    strategy: str = "cashflow"
    year_built: Optional[int] = None
    deferred: bool = False

    def __post_init__(self) -> None:
        if self.strategy not in STRATEGIES:
            raise ValueError(f"guardrail strategy must be one of {STRATEGIES}")

    @classmethod
    def coerce(cls, profile: Any, canonical: Optional[Mapping[str, Any]] = None) -> "GuardrailProfile":
        """Build from a GuardrailProfile, a DealProfile, or a mapping.

        A mapping carries ``strategy`` or ``deal_type`` plus ``year_built``
        (and optionally ``deferred``). A missing year built falls back to the
        canonical's ``deal_profile`` / ``metadata``.
        """
        if isinstance(profile, cls):
            out = profile
        elif isinstance(profile, DealProfile):
            out = cls(strategy=profile.strategy, year_built=profile.year_built)
        elif isinstance(profile, Mapping):
            if profile.get("strategy") is not None:
                strategy = profile["strategy"]
            else:
                strategy = DEAL_TYPE_STRATEGY[check_deal_type(profile.get("deal_type", "stabilized"))]
            out = cls(
                strategy=strategy,
                year_built=_normalize_year(profile.get("year_built")),
                deferred=bool(profile.get("deferred", False)),
            )
        elif profile is None:
            stored = (canonical or {}).get("deal_profile") or {}
            out = cls(strategy=DEAL_TYPE_STRATEGY[check_deal_type(stored.get("deal_type", "stabilized"))])
        else:
            raise TypeError("profile must be a GuardrailProfile, a DealProfile, a mapping, or None")
        if out.year_built is None and canonical is not None:
            year = canonical_year_built(canonical)
            if year is not None:
                out = cls(strategy=out.strategy, year_built=year, deferred=out.deferred)
        return out

    @property
    def bucket(self) -> Optional[str]:
        return house.vintage_bucket(self.year_built)

    @property
    def vintage_source(self) -> Optional[str]:
        return house_vintage_source(self.bucket) if self.bucket else None

    # Vintage floors, straight from engine/underwriting_policy.py.
    def physical_vacancy(self) -> Optional[Decimal]:
        return house.default_physical_vacancy_rate(self.year_built, strategy=self.strategy, deferred=self.deferred)

    def loss_to_lease(self) -> Optional[Decimal]:
        return house.default_loss_to_lease_rate(self.year_built, strategy=self.strategy)

    def collection_loss(self) -> Optional[Decimal]:
        return house.default_collection_loss_rate(self.year_built, strategy=self.strategy, deferred=self.deferred)

    def year1_concessions(self) -> Optional[Decimal]:
        return house.default_year1_concessions_rate(self.year_built, strategy=self.strategy, deferred=self.deferred)

    def insurance_per_unit(self) -> Decimal:
        return house.DEFAULT_YEAR1_INSURANCE_PER_UNIT

    def r_and_m_per_unit(self) -> Optional[Decimal]:
        return house.default_r_and_m_per_unit(self.year_built, strategy=self.strategy, deferred=self.deferred)

    def replacement_reserve_per_unit(self) -> Optional[Decimal]:
        return house.default_replacement_reserve_per_unit(
            self.year_built, strategy=self.strategy, deferred=self.deferred
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "deal_type": STRATEGY_DEAL_TYPE[self.strategy],
            "year_built": self.year_built,
            "vintage_bucket": self.bucket,
            "deferred": self.deferred,
        }


def year1_insurance_per_unit(observed_per_unit: Optional[Decimal]) -> Decimal:
    """Year-1 insurance per unit: max(observed run rate, house floor)."""
    return house.default_year1_insurance_per_unit(observed_per_unit)


# ── deal-evidence anchors (moved verbatim from engine/backsolve.py) ──────


def status_count(status_counts: dict[str, Any], *names: str) -> int:
    wanted = {name.strip().lower().replace("_", " ").replace("-", " ") for name in names}
    total = 0
    for key, value in (status_counts or {}).items():
        normalized = " ".join(str(key).strip().lower().replace("_", " ").replace("-", " ").split())
        # Status variants such as "Vacant Unrented" / "Notice Rented" count
        # toward their leading status.
        if any(normalized == w or normalized.startswith(w + " ") for w in wanted):
            total += int(value or 0)
    return total


def parse_percent_note(text: str | None) -> Decimal | None:
    if not text:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", str(text))
    if not match:
        return None
    return Decimal(match.group(1)) / Decimal("100")


def t12_vacancy_rate(trailing_actuals: dict[str, Any] | None) -> Decimal | None:
    if not trailing_actuals:
        return None
    gpr = dec(trailing_actuals.get("gross_potential_rent", 0))
    # Statements carry vacancy loss as a negative revenue line.
    vacancy = abs(dec(trailing_actuals.get("vacancy_loss", 0)))
    if gpr <= 0 or vacancy <= 0:
        return None
    return vacancy / gpr


def broker_vacancy_rate(broker_snapshot: dict[str, Any] | None) -> Decimal | None:
    if not broker_snapshot:
        return None
    revenue = broker_snapshot.get("revenue_assumptions") or {}
    gpr = dec(revenue.get("gross_potential_rent", 0))
    vacancy = abs(dec(revenue.get("vacancy_loss", 0)))
    if (gpr <= 0 or vacancy <= 0) and broker_snapshot.get("income_table"):
        for row in broker_snapshot.get("income_table") or []:
            line = str(row.get("line", "")).lower()
            amount = dec(row.get("annual_amount", 0))
            if "current market rent" in line or "gross potential" in line:
                gpr += amount
            elif "value-add rent growth" in line:
                gpr += amount
            elif "vacancy" in line:
                vacancy += abs(amount)
    if gpr <= 0 or vacancy <= 0:
        return None
    return vacancy / gpr


def market_vacancy_benchmark(
    canonical: dict[str, Any],
    broker_snapshot: dict[str, Any] | None = None,
    *,
    strict: bool = True,
) -> dict[str, Any] | None:
    """The analyst-supplied ``property_summary.market_vacancy_benchmark``.

    ``strict`` (pricing) refuses a malformed benchmark; underwrite ignores it.
    """
    benchmark = ((canonical.get("metadata") or {}).get("property_summary") or {}).get("market_vacancy_benchmark")
    if benchmark is None:
        return None
    try:
        checked = validate_benchmark(benchmark)
    except ValueError:
        if strict:
            raise
        return None
    return {**checked, "vacancy_rate": checked["rate"]}


def vacancy_anchor_summary(
    canonical: dict[str, Any],
    *,
    default_vacancy: Decimal,
    broker_vacancy: Decimal | None,
    trailing_actuals: dict[str, Any] | None,
    broker_snapshot: dict[str, Any] | None,
    strict: bool = True,
) -> tuple[Decimal, dict[str, Any]]:
    """Property-level vacancy anchor: the max of the deal and house anchors."""
    property_summary = (canonical.get("metadata") or {}).get("property_summary") or {}
    house_box = property_summary.get("house_box_score") or {}
    status_counts = house_box.get("status_counts") or {}
    total_units = Decimal(str(house_box.get("total_units") or sum_units(canonical) or 0))
    vacant_units = Decimal(str(status_count(status_counts, "Vacant", "Non-Revenue", "Non Revenue")))
    notice_units = Decimal(str(status_count(status_counts, "Notice")))
    rent_roll_vacant = vacant_units / total_units if total_units else None
    vacant_plus_notice = (vacant_units + notice_units) / total_units if total_units else None
    t12_vacancy = t12_vacancy_rate(trailing_actuals)
    broker_table_vacancy = broker_vacancy_rate(broker_snapshot)
    broker_anchor = broker_vacancy if broker_vacancy is not None else broker_table_vacancy
    market = market_vacancy_benchmark(canonical, broker_snapshot, strict=strict)
    market_vacancy = (
        Decimal(str(market["vacancy_rate"]))
        if market and market.get("vacancy_rate") is not None
        else None
    )
    cohort_rates = [
        Decimal(str(row.get("vacancy_rate", 0)))
        for row in canonical.get("physical_vacancy_curve") or []
    ]
    worst_cohort = max(cohort_rates) if cohort_rates else None

    base_candidates = {
        "broker_om": broker_anchor,
        "rent_roll_vacant": rent_roll_vacant,
        "t12_vacancy_gpr": t12_vacancy,
        "vintage_house_floor": default_vacancy,
        "supplied_market_benchmark": market_vacancy,
    }
    conservative_candidates = {
        "broker_om": broker_anchor,
        "rent_roll_vacant_plus_notice": vacant_plus_notice,
        "t12_vacancy_gpr": t12_vacancy,
        "vintage_house_floor": default_vacancy,
        "supplied_market_benchmark": market_vacancy,
    }
    usable_base = {k: v for k, v in base_candidates.items() if v is not None}
    usable_conservative = {k: v for k, v in conservative_candidates.items() if v is not None}
    base_rate = max(usable_base.values()) if usable_base else Decimal("0")
    conservative_rate = max(usable_conservative.values()) if usable_conservative else base_rate

    def fmt_candidates(values: dict[str, Decimal | None]) -> dict[str, float | None]:
        return {k: (float(v) if v is not None else None) for k, v in values.items()}

    return base_rate, {
        "method": "base_case_max_property_level_anchors",
        "base_case_candidates": fmt_candidates(base_candidates),
        "base_case_selected_rate": float(base_rate),
        "base_case_selected_anchor": [
            k for k, v in usable_base.items() if v == base_rate
        ],
        "conservative_downside_candidates": fmt_candidates(conservative_candidates),
        "conservative_downside_selected_rate": float(conservative_rate),
        "conservative_downside_selected_anchor": [
            k for k, v in usable_conservative.items() if v == conservative_rate
        ],
        "stress_flag_worst_cohort_vacancy": float(worst_cohort) if worst_cohort is not None else None,
        "stress_flag_note": (
            "Worst cohort vacancy is logged for review/stress only and is not "
            "applied property-wide unless explicitly selected."
        ),
        "observed_status_counts": status_counts,
        "market_vacancy_benchmark_source": market,
    }


# ── floors ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Floor:
    """A house floor for one assumption key (``value`` None: no floor applies)."""

    key: str
    value: Optional[Decimal]
    source: Optional[str]
    basis: str


def _max_floor(
    key: str,
    vintage: Optional[Decimal],
    vintage_source: Optional[str],
    broker: Optional[Decimal],
    label: str,
) -> Floor:
    if vintage is None and broker is None:
        return Floor(key, None, None, f"No vintage {label} floor (year built unknown) and no broker anchor.")
    if broker is not None and (vintage is None or broker > vintage):
        return Floor(key, broker, SOURCE_DEAL_OM, f"Broker {label} note outranks the vintage floor.")
    return Floor(key, vintage, vintage_source, f"House vintage {label} floor (VINTAGE_FRAMEWORK_2026).")


def _broker_notes(context: Mapping[str, Any]) -> Mapping[str, Any]:
    return ((context.get("broker_snapshot") or {}).get("underwriting_notes") or {})


def house_floors(
    canonical: dict[str, Any],
    profile: Any = None,
    context: Optional[Mapping[str, Any]] = None,
    *,
    strict: bool = True,
) -> tuple[dict[str, Floor], dict[str, Any]]:
    """Every house floor for ``profile``, plus the vacancy anchor summary.

    ``context`` may carry ``broker_snapshot`` and ``trailing_actuals``: broker
    notes and T12 vacancy are anchors that raise a floor, never lower it.
    """
    gp = GuardrailProfile.coerce(profile, canonical)
    context = context or {}
    notes = _broker_notes(context)
    vintage_vacancy = gp.physical_vacancy()
    anchor, vacancy_summary = vacancy_anchor_summary(
        canonical,
        default_vacancy=vintage_vacancy or Decimal("0"),
        broker_vacancy=parse_percent_note(notes.get("vacancy")),
        trailing_actuals=context.get("trailing_actuals"),
        broker_snapshot=context.get("broker_snapshot"),
        strict=strict,
    )
    selected = vacancy_summary["base_case_selected_anchor"]
    if "vintage_house_floor" in selected:
        vacancy_source = gp.vintage_source or SOURCE_HOUSE_VACANCY_ANCHOR
    else:
        vacancy_source = _VACANCY_ANCHOR_SOURCES.get(selected[0], SOURCE_HOUSE_VACANCY_ANCHOR)
    floors = {
        "physical_vacancy": Floor(
            "physical_vacancy",
            anchor,
            vacancy_source,
            f"Property-level vacancy anchor (max of {', '.join(sorted(vacancy_summary['base_case_candidates']))}); "
            f"selected {', '.join(selected) or 'none'}.",
        ),
        "loss_to_lease": _max_floor(
            "loss_to_lease", gp.loss_to_lease(), gp.vintage_source,
            parse_percent_note(notes.get("loss_to_lease")), "loss-to-lease",
        ),
        "collection_loss": _max_floor(
            "collection_loss", gp.collection_loss(), gp.vintage_source,
            parse_percent_note(notes.get("bad_debt")), "collection-loss",
        ),
        "concessions": _max_floor(
            "concessions", gp.year1_concessions(), gp.vintage_source,
            parse_percent_note(notes.get("concessions")), "year-1 concessions",
        ),
        "insurance_per_unit": Floor(
            "insurance_per_unit", gp.insurance_per_unit(), HOUSE_YEAR1_INSURANCE_FLOOR,
            "House Year-1 insurance minimum per unit.",
        ),
        "r_and_m_per_unit": Floor(
            "r_and_m_per_unit", gp.r_and_m_per_unit(), gp.vintage_source,
            "House vintage R&M per unit; a shortfall is topped up with one line.",
        ),
        "replacement_reserve_per_unit": Floor(
            "replacement_reserve_per_unit", gp.replacement_reserve_per_unit(), gp.vintage_source,
            "House vintage economic CapEx reserve per unit.",
        ),
    }
    return floors, vacancy_summary


# ── per-key checks (in place) ────────────────────────────────────────────


def _finding(key: str, floor: Floor, original: Any, mode: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "key": key,
        "input": GUARDRAIL_INPUTS[key],
        "status": STATUS_BELOW_FLOOR,
        "action": ACTION_ENFORCED if mode == MODE_PRICING else ACTION_WARNED,
        "floor": float(floor.value),
        "floor_source": floor.source,
    }
    out.update(extra)
    out["original"] = original
    if mode == MODE_PRICING:
        out["applied"] = float(floor.value)
    return out


def _grid(canonical: Mapping[str, Any]) -> Optional[tuple[str, str]]:
    grid = canonical.get("time_grid") or {}
    start, end = grid.get("analysis_start_date"), grid.get("analysis_end_date")
    if not start or not end:
        return None
    return str(start)[:7], str(end)[:7]


def _guard_vacancy(canonical: dict[str, Any], floor: Floor, mode: str) -> list[dict[str, Any]]:
    findings = []
    for row in canonical.get("physical_vacancy_curve") or []:
        rate = row.get("vacancy_rate", 0)
        if rate is None or Decimal(str(rate)) >= floor.value:
            continue
        findings.append(_finding(
            "physical_vacancy", floor, row.get("vacancy_rate"), mode,
            cohort_id=row.get("cohort_id"),
            start_period=row.get("start_period"),
            end_period=row.get("end_period"),
        ))
        if mode == MODE_PRICING:
            row["vacancy_rate"] = float(floor.value)
    return findings


def _guard_ltl(canonical: dict[str, Any], floor: Floor, mode: str) -> list[dict[str, Any]]:
    """Each loss-to-lease segment against the floor: ``max(segment, floor)``.

    Every segment is floored, whatever the cohort's market vs in-place rent: the
    floor only raises, so the analyst's higher segments and curve shape stay.
    A cohort with no loss-to-lease row is read as 0% and gets one row at the
    floor for the analysis period.
    """
    findings = []
    rows = canonical.get("loss_to_lease") or []
    for row in rows:
        rate = row.get("ltl_percent")
        if rate is None or Decimal(str(rate)) >= floor.value:
            continue
        findings.append(_finding(
            "loss_to_lease", floor, rate, mode,
            cohort_id=row.get("cohort_id"),
            start_period=row.get("start_period"),
            end_period=row.get("end_period"),
        ))
        if mode == MODE_PRICING:
            row["ltl_percent"] = float(floor.value)
    grid = _grid(canonical)
    if grid is None or floor.value <= 0:
        return findings
    covered = {str(row.get("cohort_id")) for row in rows}
    for cohort in canonical.get("unit_cohorts") or []:
        cohort_id = cohort.get("cohort_id")
        if cohort_id is None or str(cohort_id) in covered:
            continue
        findings.append(_finding(
            "loss_to_lease", floor, 0.0, mode,
            cohort_id=cohort_id, start_period=grid[0], end_period=grid[1],
        ))
        if mode == MODE_PRICING:
            if not isinstance(canonical.get("loss_to_lease"), list):
                canonical["loss_to_lease"] = []
            canonical["loss_to_lease"].append({
                "cohort_id": cohort_id, "start_period": grid[0], "end_period": grid[1],
                "ltl_percent": float(floor.value),
            })
    return findings


def _guard_collection(canonical: dict[str, Any], floor: Floor, mode: str) -> list[dict[str, Any]]:
    curve = canonical.get("collection_loss_curve") or []
    if not curve:
        grid = _grid(canonical)
        if grid is None or floor.value <= 0:
            return []
        finding = _finding("collection_loss", floor, 0.0, mode,
                           applies_to="ALL", start_period=grid[0], end_period=grid[1])
        if mode == MODE_PRICING:
            canonical["collection_loss_curve"] = [{
                "applies_to": "ALL", "start_period": grid[0], "end_period": grid[1],
                "loss_rate": float(floor.value),
            }]
        return [finding]
    findings = []
    for row in curve:
        rate = row.get("loss_rate", 0)
        if rate is None or Decimal(str(rate)) >= floor.value:
            continue
        findings.append(_finding(
            "collection_loss", floor, rate, mode,
            applies_to=row.get("applies_to"),
            start_period=row.get("start_period"),
            end_period=row.get("end_period"),
        ))
        if mode == MODE_PRICING:
            row["loss_rate"] = float(floor.value)
    return findings


def _shift_month(month: str, months: int) -> str:
    index = int(month[:4]) * 12 + int(month[5:7]) - 1 + months
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def year1_window(canonical: Mapping[str, Any]) -> Optional[tuple[str, str]]:
    """(first, last) month of analysis year 1, clipped to the analysis period."""
    grid = _grid(canonical)
    if grid is None:
        return None
    return grid[0], min(_shift_month(grid[0], 11), grid[1])


def year1_concession_rows(canonical: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The ``concession_schedule`` rows that apply in analysis year 1."""
    window = year1_window(canonical)
    if window is None:
        return []
    return [
        row for row in canonical.get("concession_schedule") or []
        if str(row.get("start_month", ""))[:7] <= window[1] and str(row.get("end_month", ""))[:7] >= window[0]
    ]


def year1_concession_rate(canonical: Mapping[str, Any]) -> Decimal:
    """The highest year-1 concession rate (free months as their pct-of-rent equivalent)."""
    return max((concession_pct_rent(row) for row in year1_concession_rows(canonical)), default=Decimal("0"))


def _guard_concessions(canonical: dict[str, Any], floor: Floor, mode: str) -> list[dict[str, Any]]:
    """Year-1 concession rows against the floor, row by row.

    The analyst's schedule is kept: every row outside year 1 is untouched, and
    a year-1 row at or above the floor keeps its rate. A year-1 row below the
    floor is raised to it. ``free_months`` rows are compared as their percent
    of rent (months / 12) and keep their type (a raised row carries the floor's
    months equivalent). A row that runs past year 1 is split at the year-1
    boundary so its later months keep the analyst's rate. With no year-1 row at
    all, one year-1 ``pct_rent`` row at the floor is added.
    """
    window = year1_window(canonical)
    if window is None or floor.value <= 0:
        return []
    year1 = year1_concession_rows(canonical)
    try:
        rates = [concession_pct_rent(row) for row in year1]
    except ValueError:
        if mode == MODE_PRICING:
            raise
        return []
    if not year1:
        finding = _finding("concessions", floor, 0.0, mode,
                           concession_id=YEAR1_CONCESSION_FLOOR_ID, applies_to_cohort="ALL",
                           start_month=window[0], end_month=window[1], concession_type="pct_rent")
        if mode == MODE_PRICING:
            if not isinstance(canonical.get("concession_schedule"), list):
                canonical["concession_schedule"] = []
            canonical["concession_schedule"].append({
                "concession_id": YEAR1_CONCESSION_FLOOR_ID, "start_month": window[0], "end_month": window[1],
                "concession_type": "pct_rent", "amount": float(floor.value), "applies_to_cohort": "ALL",
            })
        return [finding]
    below = {id(row): rate for row, rate in zip(year1, rates) if rate < floor.value}
    findings: list[dict[str, Any]] = []
    schedule: list[dict[str, Any]] = []
    for row in canonical.get("concession_schedule") or []:
        if id(row) not in below:
            schedule.append(row)
            continue
        free_months = row.get("concession_type") == "free_months"
        extra: dict[str, Any] = {
            "concession_id": row.get("concession_id"),
            "applies_to_cohort": row.get("applies_to_cohort"),
            "start_month": row.get("start_month"),
            "end_month": row.get("end_month"),
            "concession_type": row.get("concession_type"),
        }
        if free_months:
            extra["original_amount"] = row.get("amount")
        finding = _finding("concessions", floor, float(below[id(row)]), mode, **extra)
        findings.append(finding)
        if mode != MODE_PRICING:
            schedule.append(row)
            continue
        raised = dict(row)
        later = None
        if str(row.get("end_month", ""))[:7] > window[1]:
            # The months after year 1 keep the analyst's rate.
            later = dict(row, concession_id=f"{row.get('concession_id')}_after_year1",
                         start_month=_shift_month(window[1], 1))
            raised["end_month"] = window[1]
            finding["split_at"] = window[1]
        raised["amount"] = float(floor.value * Decimal("12")) if free_months else float(floor.value)
        if free_months:
            finding["applied_amount"] = raised["amount"]
        schedule.extend([raised] + ([later] if later else []))
    if mode == MODE_PRICING:
        canonical["concession_schedule"] = schedule
    return findings


def recorded_insurance_waiver(canonical: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """The insurance floor waiver a priced canonical records, if any.

    Backsolve keeps an explicit analyst insurance figure below the floor and
    records ``action: "floor_waived_by_analyst_override"`` on the
    ``insurance_per_unit`` entry, whose override pins that figure. A later
    resolution honors the waiver while the canonical still carries that figure.
    """
    section = canonical.get("assumptions") if isinstance(canonical, Mapping) else None
    entry = ((section or {}).get("entries") or {}).get("insurance_per_unit") if isinstance(section, Mapping) else None
    if not isinstance(entry, Mapping):
        return None
    record, override = entry.get("guardrail"), entry.get("override")
    if not isinstance(record, Mapping) or record.get("action") != ACTION_WAIVED or not isinstance(override, Mapping):
        return None
    return {"value": override.get("value"), "original": record.get("original")}


def waiver_finding(floor: Floor, *, original: Any, applied: Any, **extra: Any) -> dict[str, Any]:
    """A disclosed insurance floor waiver (the value is kept as the analyst set it)."""
    return {
        "key": "insurance_per_unit",
        "input": "insurance_per_unit",
        "status": STATUS_BELOW_FLOOR,
        "action": ACTION_WAIVED,
        "floor": float(floor.value),
        "floor_source": floor.source,
        **extra,
        "original": original,
        "applied": applied,
    }


def _guard_insurance(
    canonical: dict[str, Any],
    floor: Floor,
    mode: str,
    units: int,
    *,
    analyst_override: Any = None,
) -> list[dict[str, Any]]:
    if not units:
        return []
    rows = matching_opex_rows(canonical, "insurance")
    try:
        annual = annual_opex_amount(canonical, rows[0]) if rows else Decimal("0")
    except ValueError:
        if mode == MODE_PRICING:
            raise
        return []
    per_unit = annual / Decimal(str(units))
    if per_unit >= floor.value:
        return []
    # An explicit analyst figure waives the floor: nothing changes, the waiver is disclosed.
    waiver = None
    if analyst_override is not None:
        waiver = {"original": float(per_unit)}
    else:
        recorded = recorded_insurance_waiver(canonical)
        if recorded is not None and _same(recorded["value"], float(per_unit)):
            waiver = recorded
    if waiver is not None:
        return [waiver_finding(floor, original=waiver["original"], applied=float(per_unit))]
    applied_annual = floor.value * Decimal(str(units))
    category = rows[0].get("category_name") if rows else "Insurance"
    if mode == MODE_PRICING:
        category = upsert_annual_opex_by_match(
            canonical, name_fragment="insurance", fallback_category_name="Insurance",
            annual_amount=applied_annual,
        )
    finding = _finding("insurance_per_unit", floor, float(per_unit), mode,
                       category_name=category, original_annual=float(annual))
    if mode == MODE_PRICING:
        finding["applied_annual"] = float(applied_annual)
    return [finding]


def _is_r_and_m_top_up(row: Mapping[str, Any]) -> bool:
    return str(row.get("category_name", "")).strip().lower() == R_AND_M_TOP_UP_CATEGORY.lower()


def _guard_r_and_m(canonical: dict[str, Any], floor: Floor, mode: str, units: int) -> list[dict[str, Any]]:
    """The R&M bucket against the vintage floor; a shortfall is one top-up line.

    The bucket as found (``original``) includes a top-up line an earlier pricing
    run added. The new top-up is sized from the other R&M lines alone, so
    re-pricing after the analyst lowers R&M lands exactly on the floor instead
    of keeping the stale top-up's size.
    """
    if not units:
        return []
    try:
        observed = sum_opex_bucket(canonical, "repairs_maintenance")
        prior_top_up = sum(
            (annual_opex_amount(canonical, row) for row in canonical.get("opex_table") or []
             if _is_r_and_m_top_up(row)),
            Decimal("0"),
        )
    except ValueError:
        if mode == MODE_PRICING:
            raise
        return []
    floor_annual = floor.value * Decimal(str(units))
    if observed >= floor_annual:
        return []
    finding = _finding("r_and_m_per_unit", floor, float(observed / Decimal(str(units))), mode,
                       original_annual=float(observed))
    if mode == MODE_PRICING:
        top_up = floor_annual - (observed - prior_top_up)
        upsert_annual_opex(canonical, category_name=R_AND_M_TOP_UP_CATEGORY, annual_amount=top_up)
        _grow_like_r_and_m(canonical)
        finding.update(applied_annual=float(floor_annual), top_up_annual=float(top_up),
                       category_name=R_AND_M_TOP_UP_CATEGORY)
        if prior_top_up:
            finding["replaced_top_up_annual"] = float(prior_top_up)
    return [finding]


def _grow_like_r_and_m(canonical: dict[str, Any]) -> None:
    """Give a new R&M top-up line the house R&M growth rate.

    The top-up is R&M expense, so it grows with R&M. Without a rate it would
    stay flat for the hold, and the next resolution would fill the house rate
    in, so the priced canonical would not be idempotent.
    """
    from engine.assumptions.opex import _house_growth

    for row in canonical.get("opex_table") or []:
        if _is_r_and_m_top_up(row) and row.get("growth_rate") is None and not row.get("growth_phases"):
            _category, _constant, rate = _house_growth(row)
            if rate is not None:
                row["growth_rate"] = float(rate)


def _guard_reserves(canonical: dict[str, Any], floor: Floor, mode: str, units: int) -> list[dict[str, Any]]:
    if not units:
        return []
    units_dec = Decimal(str(units))
    floor_annual = floor.value * units_dec
    rows = canonical.get("replacement_reserves") or []
    if not rows:
        grid = _grid(canonical)
        if grid is None or floor_annual <= 0:
            return []
        finding = _finding("replacement_reserve_per_unit", floor, 0.0, mode,
                           start_period=grid[0], end_period=grid[1], original_annual=0.0)
        if mode == MODE_PRICING:
            canonical["replacement_reserves"] = [{
                "start_period": grid[0], "end_period": grid[1], "annual_amount": float(floor_annual),
            }]
            finding["applied_annual"] = float(floor_annual)
        return [finding]
    findings = []
    for row in rows:
        amount = row.get("annual_amount")
        if amount is None or Decimal(str(amount)) >= floor_annual:
            continue
        finding = _finding("replacement_reserve_per_unit", floor, float(Decimal(str(amount)) / units_dec), mode,
                           start_period=row.get("start_period"), end_period=row.get("end_period"),
                           original_annual=float(amount))
        if mode == MODE_PRICING:
            row["annual_amount"] = float(floor_annual)
            finding["applied_annual"] = float(floor_annual)
        findings.append(finding)
    return findings


def _check_mode(mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")


def enforce_floors(
    canonical: dict[str, Any],
    *,
    mode: str,
    profile: Any = None,
    context: Optional[Mapping[str, Any]] = None,
    keys: Optional[Iterable[str]] = None,
) -> list[dict[str, Any]]:
    """Check (underwrite) or enforce (pricing) the house floors IN PLACE.

    Touches only the canonical fields the engine reads; the ``assumptions``
    section is left alone. Returns the findings in ``GUARDRAIL_KEYS`` order.

    ``context`` keys:
        ``broker_snapshot``, ``trailing_actuals``: deal anchors that can raise
            a floor (as backsolve's house policy always has).
        ``insurance_per_unit_override``: an explicit analyst insurance figure;
            it waives the insurance floor. A value below the floor is kept and
            disclosed as a finding with ``action:
            "floor_waived_by_analyst_override"``. A waiver recorded on the
            canonical's ``insurance_per_unit`` entry (a priced canonical) is
            honored the same way while the insurance line still carries the
            pinned figure.
        ``policy_overrides``: a list; every enforced or waived finding is
            appended to it in ``policy_overrides`` form.
    """
    _check_mode(mode)
    context = context or {}
    selected = tuple(GUARDRAIL_KEYS) if keys is None else tuple(keys)
    unknown = set(selected) - set(GUARDRAIL_KEYS)
    if unknown:
        raise ValueError(f"unknown guardrail keys {sorted(unknown)}")
    floors, _ = house_floors(canonical, profile, context, strict=mode == MODE_PRICING)
    units = sum_units(canonical)
    findings: list[dict[str, Any]] = []
    for key in GUARDRAIL_KEYS:
        if key not in selected:
            continue
        floor = floors[key]
        if floor.value is None:
            continue
        if key == "physical_vacancy":
            findings += _guard_vacancy(canonical, floor, mode)
        elif key == "loss_to_lease":
            findings += _guard_ltl(canonical, floor, mode)
        elif key == "collection_loss":
            findings += _guard_collection(canonical, floor, mode)
        elif key == "concessions":
            findings += _guard_concessions(canonical, floor, mode)
        elif key == "insurance_per_unit":
            findings += _guard_insurance(canonical, floor, mode, units,
                                         analyst_override=context.get("insurance_per_unit_override"))
        elif key == "r_and_m_per_unit":
            findings += _guard_r_and_m(canonical, floor, mode, units)
        elif key == "replacement_reserve_per_unit":
            findings += _guard_reserves(canonical, floor, mode, units)
    sink = context.get("policy_overrides")
    if isinstance(sink, list):
        sink.extend(policy_override(f) for f in findings if f["action"] in (ACTION_ENFORCED, ACTION_WAIVED))
    return findings


def policy_override(finding: Mapping[str, Any]) -> dict[str, Any]:
    """A finding in backsolve's ``policy_overrides`` form (original/applied).

    An enforced floor carries ``assumption`` and ``guardrail: "house_floor"``. A
    waived floor is ``{input: "insurance_per_unit", action:
    "floor_waived_by_analyst_override", floor, floor_source, original, applied}``.
    """
    if finding["action"] == ACTION_WAIVED:
        out = {k: v for k, v in finding.items() if k not in ("key", "status")}
        return out
    out = {
        k: v for k, v in finding.items()
        if k not in ("key", "status", "action")
    }
    out["assumption"] = finding["key"]
    out["guardrail"] = "house_floor"
    return out


# ── assumptions entries ──────────────────────────────────────────────────


def _same(a: Any, b: Any) -> bool:
    from engine.assumptions.resolver import _same as resolver_same

    return resolver_same(a, b)


def _fmt(value: Any, key: str) -> str:
    if value is None:
        return "none"
    if _UNITS[key] == "usd":
        return f"${float(value):,.2f}"
    return f"{float(value):.2%}"


_SEGMENT_FIELDS = (
    "cohort_id", "applies_to", "start_period", "end_period",
    "concession_id", "applies_to_cohort", "start_month", "end_month", "concession_type",
)


def _waiver_record(key: str, first: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "floor": first["floor"],
        "floor_source": first["floor_source"],
        "status": STATUS_BELOW_FLOOR,
        "action": ACTION_WAIVED,
        "original": first.get("original"),
        "applied": first.get("applied"),
        "basis": (
            f"{key} {_fmt(first.get('applied'), key)} is below the house floor {_fmt(first['floor'], key)} "
            f"({first['floor_source']}); the analyst's explicit insurance override waives the floor, so "
            "pricing keeps the analyst's figure. Review the override."
        ),
    }


def _entry_record(key: str, items: list[Mapping[str, Any]]) -> dict[str, Any]:
    first = items[0]
    if first["action"] == ACTION_WAIVED:
        return _waiver_record(key, first)
    originals = [float(i["original"]) for i in items if i.get("original") is not None]
    lowest = min(originals) if originals else None
    enforced = first["action"] == ACTION_ENFORCED
    verb = "raised to" if enforced else "is below"
    count = f" ({len(items)} segment{'s' if len(items) != 1 else ''})" if key in CURVE_KEYS else ""
    record: dict[str, Any] = {
        "floor": first["floor"],
        "floor_source": first["floor_source"],
        "status": STATUS_BELOW_FLOOR,
        "action": first["action"],
        "original": lowest,
    }
    if enforced:
        record["applied"] = first["applied"]
    if key in CURVE_KEYS:
        record["segments"] = [
            {
                **{f: i[f] for f in _SEGMENT_FIELDS if i.get(f) is not None},
                "original": i.get("original"),
                **({"applied": i["applied"]} if "applied" in i else {}),
            }
            for i in items
        ]
    record["basis"] = (
        f"{key}{count} {verb} the house floor {_fmt(first['floor'], key)} ({first['floor_source']}); "
        f"lowest value before the guardrail {_fmt(lowest, key)}. "
        + (
            "Pricing mode enforces the floor."
            if enforced
            else "Underwrite mode keeps the value; pricing mode would enforce the floor."
        )
    )
    return record


def _confidence(source: str) -> Optional[str]:
    if source.startswith(HOUSE_PREFIX) or source == SOURCE_DEAL_OM:
        return "medium"
    if source.startswith("deal:"):
        return "high"
    return "medium"


def record_on_entries(
    entries: dict[str, dict[str, Any]],
    findings: Iterable[Mapping[str, Any]],
    *,
    mode: str,
    prior_entries: Optional[Mapping[str, Any]] = None,
) -> dict[str, dict[str, Any]]:
    """Record guardrail findings on ``entries`` (mutated and returned).

    An enforced key takes the floor's source, and its override pins the priced
    value (note ``GUARDRAIL_OVERRIDE_NOTE``) so a re-resolution keeps it; the
    pre-floor value stays in ``guardrail.original``. Every entry without an
    override still has ``value == default_value``. In pricing mode a pinned
    entry from ``prior_entries`` keeps its record when nothing new is found.
    """
    from engine.assumptions.resolver import _NOTE_EDITED, _NOTE_PRESENT

    auto_notes = {_NOTE_EDITED, _NOTE_PRESENT, GUARDRAIL_OVERRIDE_NOTE}
    by_key: dict[str, list[Mapping[str, Any]]] = {}
    for finding in findings:
        by_key.setdefault(finding["key"], []).append(finding)
    for key, entry in entries.items():
        entry.pop("guardrail", None)
        items = by_key.get(key)
        if not items:
            prior = (prior_entries or {}).get(key)
            prior_record = prior.get("guardrail") if isinstance(prior, Mapping) else None
            pinned = (entry.get("override") or {}).get("note") == GUARDRAIL_OVERRIDE_NOTE
            if (
                mode == MODE_PRICING
                and isinstance(prior_record, Mapping)
                and prior_record.get("action") == ACTION_ENFORCED
                and pinned
                and _same(entry.get("value"), prior.get("value"))
            ):
                for field in ("guardrail", "source", "basis", "confidence", "review_required", "candidates"):
                    entry[field] = deepcopy(prior[field])
            continue
        record = _entry_record(key, items)
        entry["guardrail"] = record
        if record["action"] != ACTION_ENFORCED:
            continue
        source = record["floor_source"]
        entry["source"] = source
        entry["confidence"] = _confidence(source)
        entry["review_required"] = requires_review(source)
        override = entry.get("override") or {}
        pin: dict[str, Any] = {"value": entry["value"], "note": GUARDRAIL_OVERRIDE_NOTE}
        cohorts = override.get("cohort_overrides") or entry.get("cohort_overrides")
        if cohorts:
            pin["cohort_overrides"] = deepcopy(cohorts)
        entry["override"] = pin
        candidates = [
            c for c in entry.get("candidates") or []
            if not (
                c.get("source") == SOURCE_ANALYST
                and c.get("basis") in auto_notes
                and _same(c.get("value"), entry["value"])
            )
        ]
        if not any(c.get("source") == source and _same(c.get("value"), record["floor"]) for c in candidates):
            candidates.append({"source": source, "value": record["floor"],
                               "basis": "House floor enforced by the pricing guardrail."})
        entry["candidates"] = candidates
        entry["basis"] = f"Pricing guardrail: {record['basis']} Resolver default: {_fmt_default(entry, key)}."
    return entries


def _fmt_default(entry: Mapping[str, Any], key: str) -> str:
    value = entry.get("default_value")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _fmt(value, key)
    return "none" if value is None else str(value)


# ── pricing labels (values backsolve sets outside the shared floors) ─────


def fmt_unit_value(value: Any, unit: Any) -> str:
    """A value for a basis text, formatted by its entry unit."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "none" if value is None else str(value)
    unit = str(unit or "")
    if unit.startswith("decimal"):
        return f"{float(value):.2%}"
    if unit.startswith("usd"):
        return f"${float(value):,.2f}"
    return f"{float(value):g}"


def _entry_confidence(source: str) -> Optional[str]:
    return "high" if source == SOURCE_ANALYST else _confidence(source)


def label_pricing_entry(entry: dict[str, Any], *, source: str, note: str, basis: str) -> dict[str, Any]:
    """Label an entry whose value backsolve's pricing set (mutated and returned).

    Backsolve sets values outside the shared floors: its pricing policy (fees,
    closing costs, sale cost, an explicit exit cap; ``house:backsolve_policy``),
    its pricing normalizations (``house:pricing_normalization``), the
    property-tax reassessment at the priced price, and opex lines a floor
    raised. Re-resolving the solved canonical would otherwise show each of them
    as an ``analyst`` edit ("changed in the canonical after the last
    resolution"). The entry takes ``source`` and ``basis``, and an override
    with ``note`` (one of ``PRICING_LABEL_NOTES``) pins the priced value so a
    re-resolution keeps it; ``restore_pricing_labels`` then keeps the label.
    An entry the pricing guardrail enforced keeps its guardrail label.
    """
    from engine.assumptions.resolver import _NOTE_EDITED, _NOTE_PRESENT

    if (entry.get("guardrail") or {}).get("action") == ACTION_ENFORCED:
        return entry
    auto_notes = {_NOTE_EDITED, _NOTE_PRESENT, GUARDRAIL_OVERRIDE_NOTE} | PRICING_LABEL_NOTES
    value = entry.get("value")
    override = entry.get("override") or {}
    pin: dict[str, Any] = {"value": deepcopy(value), "note": note}
    cohorts = override.get("cohort_overrides") or entry.get("cohort_overrides")
    if cohorts:
        pin["cohort_overrides"] = deepcopy(cohorts)
    candidates = [
        c for c in entry.get("candidates") or []
        if not (c.get("source") == SOURCE_ANALYST and c.get("basis") in auto_notes)
        and not (c.get("source") == source and _same(c.get("value"), value))
    ]
    entry.update(
        source=source,
        confidence=_entry_confidence(source),
        review_required=requires_review(source),
        override=pin,
        candidates=[{"source": source, "value": deepcopy(value), "basis": basis}] + candidates,
        basis=f"{basis} Resolver default: {fmt_unit_value(entry.get('default_value'), entry.get('unit'))}.",
    )
    return entry


def restore_pricing_labels(
    entries: dict[str, dict[str, Any]],
    prior_entries: Optional[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Keep each pricing label on re-resolution (mutated and returned).

    An entry still pinned by a ``PRICING_LABEL_NOTES`` override at the same
    value as in ``prior_entries`` takes back the prior entry's source, basis,
    confidence, review flag and candidates. A changed value (the analyst edited
    the canonical) or a new override drops the label.
    """
    for key, entry in entries.items():
        prior = (prior_entries or {}).get(key)
        override = entry.get("override") or {}
        if (
            not isinstance(prior, Mapping)
            or override.get("note") not in PRICING_LABEL_NOTES
            or (prior.get("override") or {}).get("note") != override.get("note")
            or not _same(entry.get("value"), prior.get("value"))
        ):
            continue
        for field in ("source", "basis", "confidence", "review_required", "candidates"):
            entry[field] = deepcopy(prior.get(field))
    return entries


# ── public entry point ───────────────────────────────────────────────────


def apply_guardrails(
    canonical: Mapping[str, Any],
    *,
    mode: str,
    profile: Any = None,
    context: Optional[Mapping[str, Any]] = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Apply the house guardrails in ``mode`` and return ``(canonical_copy, findings)``.

    The input is never mutated.

    - ``underwrite``: no value changes. Findings are recorded on
      ``assumptions.entries[key].guardrail`` with ``action: "warned"``.
    - ``pricing``: values below their floor are raised (per segment for
      curves). When the canonical carries an ``assumptions`` section it is
      re-resolved in pricing mode so its values match what is priced, with
      ``action: "enforced"`` records.

    Args:
        canonical: Canonical deal inputs.
        mode: ``"underwrite"`` or ``"pricing"``.
        profile: ``GuardrailProfile``, ``DealProfile`` or a mapping with
            ``deal_type``/``strategy`` and ``year_built``. ``None`` reads the
            canonical's ``deal_profile``.
        context: See ``enforce_floors``.
    """
    _check_mode(mode)
    out = deepcopy(dict(canonical))
    findings = enforce_floors(out, mode=mode, profile=profile, context=context)
    section = out.get("assumptions")
    entries = section.get("entries") if isinstance(section, Mapping) else None
    if not isinstance(entries, dict):
        return out, findings
    if mode == MODE_PRICING:
        from engine.assumptions.resolver import resolve_assumptions

        gp = GuardrailProfile.coerce(profile, out)
        resolved = resolve_assumptions(
            out,
            deal_profile=pricing_profile(out, strategy=gp.strategy, year_built=gp.year_built),
            mode=MODE_PRICING,
            guardrail_context={"enforced": findings},
        )
        out["assumptions"] = resolved["assumptions"]
        out["deal_profile"] = resolved["deal_profile"]
    else:
        record_on_entries(entries, findings, mode=mode)
        section["mode"] = mode
    return out, findings


def pricing_profile(canonical: Mapping[str, Any], *, strategy: str, year_built: Optional[int]) -> DealProfile:
    """The deal profile a pricing run resolves with: the canonical's geography,
    the pricing strategy's deal type, and the year built pricing used."""
    stored = canonical.get("deal_profile") if isinstance(canonical.get("deal_profile"), Mapping) else {}
    return DealProfile(
        deal_type=STRATEGY_DEAL_TYPE[strategy],
        msa=stored.get("msa"),
        submarket=stored.get("submarket"),
        year_built=year_built if year_built is not None else stored.get("year_built"),
        asset_class=stored.get("asset_class"),
        star_tier=stored.get("star_tier"),
    )
