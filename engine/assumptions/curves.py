"""Assumption curves and market wiring (ADR 0003, slice 4).

Resolver v1 (``resolver.py``) records candidates and keeps today's values.
This module applies the ADR 0003 §3 precedence for six keys and turns their
results into curves the engine reads:

``rent_growth``
    A per-year vector written to ``growth_assumptions.annual_growth_rates``
    (element ``k`` is the increase entering analysis year ``k + 2``) and its
    first element to the scalar ``annual_growth_rate``. Years with forecast
    coverage come from ``rent_growth_forecast`` (submarket, else MSA; a
    submarket series with no annual coverage falls back to the MSA one),
    annualized with ``engine.market_data.annualize``. After the horizon the
    rate glides linearly over ``HOUSE_RENT_GROWTH_GLIDE_YEARS`` to the long-run
    rate: the MSA ``rent_growth_long_run`` if a provider has one, else
    ``HOUSE_LONG_RUN_RENT_GROWTH``. A canonical without ``growth_assumptions``
    gets the section created from this vector.
``physical_vacancy``
    Current physical vacancy (rent roll) moving linearly to stabilized vacancy
    over the deal type's stabilization window, then flat. Stabilized vacancy is
    ``vacancy_stabilized`` (submarket, else MSA), else the house value
    max(vintage typical-band midpoint, house vacancy floor for the strategy). Written as monthly segments per cohort.
``loss_to_lease``
    Each cohort's rent-roll loss to lease (market vs in-place) burning off
    linearly over ``HOUSE_LTL_BURN_OFF_MONTHS`` to the house vintage LTL floor.
    Written as monthly segments per cohort.
``concessions``
    Year-1 rate from T12 concessions / GPR, else ``concession_rate``
    (submarket, else MSA), else the house vintage year-1 floor; after
    ``HOUSE_YEAR1_CONCESSION_MONTHS`` it steps to ``concession_rate``, else the
    house vintage ``concessions_typical_low``. Written as pct_rent rows.
``collection_loss``
    T12 bad debt / GPR, else the house default (vintage
    ``bad_debt_typical_low``, 2.0% when the year built is unknown).
``exit_cap_rate``
    The exit-year ``market_cap_rate`` (submarket, else MSA) when an exit is
    modeled, else the OM rate, else the going-in cap rate (T12 NOI / price)
    plus ``HOUSE_EXIT_CAP_SPREAD`` (``house:exit_cap_spread``), else the
    engine default.

Analyst overrides keep resolver v1 semantics and always win. A curve already
in a canonical that the resolver did not write is recorded as an analyst
value and left as it is.

Entry values for curve keys are compact parameter objects, e.g.
``{"start": 0.2, "stabilized": 0.0575, "window_months": 12}`` for vacancy;
a scalar means a flat curve (the v1 shape). The canonical readers recognize
the curves this module writes and return the same parameters, so a
re-resolve is idempotent.

Market data is read once per (metric, geography) through
``ctx.market.resolve(metric, Geo, as_of=None)``; a missing or failing
provider simply falls through to the next source.
"""

from __future__ import annotations

import math
import re
from copy import deepcopy
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Callable, Mapping, Optional

from engine import underwriting_policy as house
from engine.assumptions.canonical_ops import ensure_year1_concession_schedule
from engine.assumptions.registry import (
    MARKET_PREFIX,
    SOURCE_DEAL_RENT_ROLL,
    SOURCE_DEAL_T12,
    house_vintage_source,
    market_source,
)
from engine.assumptions.resolver import (
    ABSENT,
    NO_SECTION,
    PLACEHOLDER,
    PRESENT,
    _candidate,
    _Choice,
    _cohort_reader,
    _Context,
    _Current,
    _exit_cap_default,
    _fallback,
    _house_exit_cap,
    _ltl_candidates,
    _num,
    _read_concessions,
    _rent_growth_default,
    _same,
    _scalar_reader,
    _Spec,
    _t12_rate,
    _vacancy_candidates,
    _write_concessions,
)
from engine.market_data.annualize import annual_value, yoy_series_to_analysis_years
from engine.market_data.types import Geo, Observation

# house:<rule> sources introduced by this slice.
HOUSE_LONG_RUN_RENT_GROWTH_SOURCE = "house:long_run_rent_growth"
HOUSE_DEFAULT_COLLECTION_LOSS_SOURCE = "house:default_collection_loss"

# deal_profile.star_tier -> CoStar star-slice label used by the providers.
STAR_TIER_LABELS = {"1_2": "1 & 2 Star", "3": "3 Star", "4_5": "4 & 5 Star"}

CONCESSION_YEAR1_ID = "assumption_year1_concessions"
CONCESSION_STABILIZED_ID = "assumption_stabilized_concessions"

_PLACES = 6
_KEPT_NOTE = "Kept from the previous resolution: no market data was supplied when re-resolving."


def _r(value: float) -> float:
    return round(float(value), _PLACES)


# ── geography and market lookups ─────────────────────────────────────────


def profile_geo(profile: Any) -> Optional[Geo]:
    """The ``Geo`` a deal profile asks market data about (None without geography)."""
    msa = getattr(profile, "msa", None) or {}
    submarket = getattr(profile, "submarket", None) or {}
    star_tier = getattr(profile, "star_tier", None)
    geo = Geo(
        cbsa_code=msa.get("cbsa_code"),
        metro_slug=msa.get("metro_slug"),
        submarket=submarket.get("name") or submarket.get("source_id"),
        star_tier=STAR_TIER_LABELS.get(star_tier, star_tier) if star_tier else None,
    )
    if not (geo.cbsa_code or geo.metro_slug or geo.submarket):
        return None
    return geo


def msa_geo(geo: Optional[Geo]) -> Optional[Geo]:
    if geo is None or not (geo.cbsa_code or geo.metro_slug):
        return None
    return replace(geo, submarket=None)


@dataclass(frozen=True)
class MarketPick:
    metric: str
    observation: Observation
    level: str  # "submarket" | "msa"

    @property
    def source(self) -> str:
        return _observation_source(self.observation)

    @property
    def as_of(self) -> Optional[str]:
        return self.observation.as_of.isoformat() if self.observation.as_of else None


def _observation_source(observation: Observation) -> str:
    provider = re.sub(r"[^A-Za-z0-9_.\-]", "_", str(observation.provenance.provider)) or "unknown"
    return market_source(provider)


def _usable(metric: str, observation: Any) -> bool:
    if not isinstance(observation, Observation):
        return False
    if metric in ("rent_growth_forecast", "market_cap_rate"):
        return observation.is_series or observation.scalar is not None
    value = observation.scalar
    return value is not None and math.isfinite(value)


def _cache(ctx: _Context) -> dict:
    return ctx.market_cache


def market_observations(ctx: _Context, metric: str, *, msa_only: bool = False) -> list[Observation]:
    """Every provider's observation for ``metric`` at the deal's geography (cached)."""
    if ctx.market is None:
        return []
    geo = profile_geo(ctx.profile)
    if msa_only:
        geo = msa_geo(geo)
    if geo is None:
        return []
    key = (metric, geo)
    cache = _cache(ctx)
    if key not in cache:
        try:
            observation, candidates = ctx.market.resolve(metric, geo, as_of=None)
        except Exception:  # noqa: BLE001 - missing market data must fall through
            observation, candidates = None, []
        items = list(candidates or [])
        if observation is not None and not any(item is observation for item in items):
            items.insert(0, observation)
        cache[key] = [item for item in items if _usable(metric, item)]
    return cache[key]


def market_pick(ctx: _Context, metric: str, *, msa_only: bool = False) -> Optional[MarketPick]:
    """Submarket observation if any provider has one, else the first (MSA) one."""
    observations = market_observations(ctx, metric, msa_only=msa_only)
    if not observations:
        return None
    if not msa_only:
        for observation in observations:
            if observation.geo.submarket:
                return MarketPick(metric, observation, "submarket")
    first = observations[0]
    return MarketPick(metric, first, "submarket" if first.geo.submarket else "msa")


def _observation_candidate(metric: str, observation: Observation, selected: bool) -> dict[str, Any]:
    level = "submarket" if observation.geo.submarket else "MSA"
    return _candidate(
        _observation_source(observation),
        observation.series if observation.is_series else observation.scalar,
        f"Market metric {metric!r} at {level} level from {observation.provenance.provider}"
        + (" (selected)." if selected else "."),
        metric=metric,
        unit=observation.unit,
        as_of=observation.as_of,
        confidence=observation.confidence,
        horizon=observation.horizon,
        geo={k: v for k, v in observation.geo.as_dict().items() if v is not None},
        provenance=observation.provenance.as_dict(),
    )


def market_candidates(ctx: _Context, spec: _Spec) -> list[dict[str, Any]]:
    """Candidates for every market metric a key reads (replaces resolver v1's)."""
    out: list[dict[str, Any]] = []
    for metric in spec.market_metrics:
        msa_only = metric == "rent_growth_long_run"
        pick = market_pick(ctx, metric, msa_only=msa_only)
        for observation in market_observations(ctx, metric, msa_only=msa_only):
            selected = pick is not None and observation is pick.observation
            out.append(_observation_candidate(metric, observation, selected))
    return out


def carry_market_default(
    ctx: _Context,
    current: _Current,
    prior: Optional[Mapping[str, Any]],
    default: _Choice,
    candidates: list[dict[str, Any]],
) -> tuple[_Choice, list[dict[str, Any]]]:
    """Keep a market-informed default when re-resolving without market data.

    Re-resolving (``--partial-json``, overrides) without a registry must not
    silently revert market-derived values to house defaults.
    """
    if ctx.market is not None or not isinstance(prior, Mapping) or prior.get("override"):
        return default, candidates
    source = str(prior.get("source", ""))
    used_market = source.startswith(MARKET_PREFIX) or any(
        str(c.get("source", "")).startswith(MARKET_PREFIX)
        for c in prior.get("candidates") or []
        if isinstance(c, Mapping)
    )
    if not used_market or current.state != PRESENT:
        return default, candidates
    if not _same(current.value, prior.get("value")):
        return default, candidates
    basis = str(prior.get("basis", ""))
    if _KEPT_NOTE not in basis:
        basis = f"{basis} {_KEPT_NOTE}".strip()
    kept = _Choice(
        source=source,
        value=deepcopy(prior.get("default_value")),
        basis=basis,
        as_of=prior.get("as_of"),
        confidence=prior.get("confidence"),
    )
    return kept, deepcopy(list(prior.get("candidates") or []))


# ── months ───────────────────────────────────────────────────────────────


def _month_index(month: str) -> int:
    return int(month[:4]) * 12 + int(month[5:7]) - 1


def _month(index: int) -> str:
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _grid(ctx: _Context) -> list[str]:
    if len(ctx.start) != 7 or len(ctx.end) != 7:
        return []
    first, last = _month_index(ctx.start), _month_index(ctx.end)
    return [_month(i) for i in range(first, last + 1)]


def analysis_years(ctx: _Context) -> int:
    return max(1, math.ceil(len(_grid(ctx)) / 12))


# ── rent growth ──────────────────────────────────────────────────────────


def _long_run(ctx: _Context) -> tuple[float, str, str, Optional[MarketPick]]:
    pick = market_pick(ctx, "rent_growth_long_run", msa_only=True)
    if pick is not None:
        value = float(pick.observation.scalar)
        return value, pick.source, f"MSA long-run {value:.2%} from {pick.observation.provenance.provider}", pick
    value = float(house.HOUSE_LONG_RUN_RENT_GROWTH)
    return value, HOUSE_LONG_RUN_RENT_GROWTH_SOURCE, f"house long-run {value:.2%} (HOUSE_LONG_RUN_RENT_GROWTH)", None


def _forecast_years(ctx: _Context, pick: MarketPick, years: int) -> list[float]:
    """Forecast growth per analysis year, covered prefix only (leading gaps back-filled)."""
    observation = pick.observation
    if not observation.is_series:
        return [float(observation.scalar)]
    yearly = yoy_series_to_analysis_years(observation, ctx.start, years, extend="none")
    first = next((i for i, value in enumerate(yearly) if value is not None), None)
    if first is None:
        return []
    out: list[float] = []
    for i, value in enumerate(yearly):
        if i < first:
            out.append(float(yearly[first]))
            continue
        if value is None:
            break
        out.append(float(value))
    return out


def _covered_forecast(ctx: _Context, length: int) -> tuple[Optional[MarketPick], list[float], str]:
    """The first forecast with annual coverage: the submarket pick, else an MSA forecast.

    A submarket series whose quarters do not cover an analysis year falls back
    to the MSA forecast instead of skipping straight to the long-run rate.
    """
    pick = market_pick(ctx, "rent_growth_forecast")
    if pick is None:
        return None, [], ""
    covered = _forecast_years(ctx, pick, length)
    if covered or pick.level != "submarket":
        return pick, covered, ""
    msa = [o for o in market_observations(ctx, "rent_growth_forecast") if not o.geo.submarket]
    msa += market_observations(ctx, "rent_growth_forecast", msa_only=True)
    for observation in msa:
        candidate = MarketPick("rent_growth_forecast", observation, "msa")
        years = _forecast_years(ctx, candidate, length)
        if years:
            note = (
                f"The submarket rent_growth_forecast ({pick.observation.provenance.provider}) has no "
                "annual coverage of the analysis years, so the MSA forecast is used. "
            )
            return candidate, years, note
    return pick, [], ""


def rent_growth_vector(ctx: _Context) -> _Choice:
    """The per-year rent-growth vector for this deal (ADR 0003 §3)."""
    length = max(1, analysis_years(ctx) - 1)
    long_run, long_run_source, long_run_basis, long_run_pick = _long_run(ctx)
    forecast, covered, fallback_note = _covered_forecast(ctx, length) if ctx.start else (None, [], "")

    vector = covered[:length]
    glide_years = int(house.HOUSE_RENT_GROWTH_GLIDE_YEARS)
    last = vector[-1] if vector else None
    step = 0
    while len(vector) < length:
        step += 1
        if last is None or glide_years <= 0:
            vector.append(long_run)
        else:
            vector.append(last + (long_run - last) * min(step, glide_years) / glide_years)
    vector = [_r(v) for v in vector]

    convention = "Element k is the increase entering analysis year k+2."
    if covered:
        observation = forecast.observation
        n = min(len(covered), length)
        basis = (
            f"Increases entering analysis years 2-{n + 1} from the {forecast.level} rent_growth_forecast "
            f"({observation.provenance.provider}, as of {forecast.as_of or 'unknown'}), annualized to "
            f"analysis years; then a linear glide over {glide_years} years to the {long_run_basis}. "
            f"{fallback_note}{convention}"
        )
        return _Choice(forecast.source, vector, basis, as_of=forecast.as_of, confidence=observation.confidence)
    reason = (
        "No rent_growth_forecast for the deal's geography"
        if ctx.market is not None and profile_geo(ctx.profile) is not None
        else "No market data was used (no registry or no deal geography)"
    )
    basis = f"{reason}; every year uses the {long_run_basis}. {convention}"
    if long_run_pick is not None:
        return _Choice(
            long_run_source, vector, basis,
            as_of=long_run_pick.as_of, confidence=long_run_pick.observation.confidence,
        )
    return _Choice(long_run_source, vector, basis, confidence="medium")


def _read_rent_growth(ctx: _Context) -> _Current:
    growth = ctx.canonical.get("growth_assumptions")
    if not isinstance(growth, Mapping):
        # No growth section (e.g. a hand-authored canonical): the resolver creates
        # one from the resolved rent growth rather than leaving growth unmodeled.
        return _Current(PLACEHOLDER)
    vector = growth.get("annual_growth_rates")
    if isinstance(vector, list) and vector:
        values = [_num(v) if _num(v) is not None else v for v in vector]
        scalar = _num(growth.get("annual_growth_rate"))
        if scalar is not None and not _same(scalar, values[0]):
            # The scalar was edited after the vector was written: the edit is
            # the analyst's flat rate, so the stale vector is dropped (the
            # engine would otherwise keep growing by the vector).
            if isinstance(growth, dict):
                growth.pop("annual_growth_rates", None)
            return _Current(PRESENT, scalar)
        return _Current(PRESENT, values)
    if "annual_growth_rate" not in growth:
        return _Current(PLACEHOLDER)
    value = growth["annual_growth_rate"]
    if value is None:
        return _Current(PLACEHOLDER)
    return _Current(PRESENT, _num(value) if _num(value) is not None else value)


def _write_rent_growth(ctx: _Context, value: Any, _cohorts: Optional[dict[str, float]]) -> bool:
    growth = ctx.canonical.get("growth_assumptions")
    if not isinstance(growth, dict):
        growth = {"growth_type": "annual_compound"}
        ctx.canonical["growth_assumptions"] = growth
    if isinstance(value, (list, tuple)):
        if not value or any(_num(v) is None for v in value):
            return False
        growth["annual_growth_rate"] = float(value[0])
        growth["annual_growth_rates"] = [float(v) for v in value]
        return True
    if _num(value) is None:
        return False
    growth["annual_growth_rate"] = value
    growth.pop("annual_growth_rates", None)
    return True


def _rent_growth_candidates(ctx: _Context) -> list[dict[str, Any]]:
    return [
        _candidate(
            HOUSE_LONG_RUN_RENT_GROWTH_SOURCE,
            float(house.HOUSE_LONG_RUN_RENT_GROWTH),
            "House long-run annual rent growth (HOUSE_LONG_RUN_RENT_GROWTH).",
        )
    ]


# ── collection loss ──────────────────────────────────────────────────────


def _house_collection_loss(ctx: _Context) -> dict[str, Any]:
    value = float(house.default_bad_debt_rate(ctx.year_built))
    if ctx.bucket is None:
        return _candidate(
            HOUSE_DEFAULT_COLLECTION_LOSS_SOURCE,
            value,
            f"Year built unknown; house default collection loss {value:.2%} "
            "(HOUSE_DEFAULT_COLLECTION_LOSS).",
        )
    return _candidate(
        house_vintage_source(ctx.bucket),
        value,
        f"Year built {ctx.year_built} ({ctx.year_built_origin}) maps to the {ctx.bucket} vintage bucket; "
        f"house default collection loss = vintage bad_debt_typical_low {value:.2%}.",
    )


def _collection_loss_candidates(ctx: _Context) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    t12_rate = _t12_rate(ctx, "bad_debt")
    if t12_rate is not None:
        out.append(_candidate(SOURCE_DEAL_T12, _r(t12_rate), "T12 bad debt / gross potential rent."))
    out.append(_house_collection_loss(ctx))
    if ctx.bucket is not None:
        floor = house.default_collection_loss_rate(ctx.year_built, strategy=ctx.strategy)
        if floor is not None:
            out.append(
                _candidate(
                    house_vintage_source(ctx.bucket),
                    float(floor),
                    f"House vintage collection-loss floor {float(floor):.2%}: a guardrail floor "
                    "(ADR 0003 §5), not the default.",
                )
            )
    return out


def _collection_loss_default(ctx: _Context, state: str) -> _Choice:
    t12_rate = _t12_rate(ctx, "bad_debt")
    if t12_rate is not None:
        return _Choice(SOURCE_DEAL_T12, _r(t12_rate), "T12 bad debt / gross potential rent.", confidence="high")
    candidate = _house_collection_loss(ctx)
    return _Choice(
        candidate["source"],
        candidate["value"],
        candidate["basis"] + " The T12 shows no bad debt line.",
        confidence="medium",
    )


# ── ramp curves (vacancy, loss to lease) ─────────────────────────────────


@dataclass(frozen=True)
class _Ramp:
    table: str
    field: str
    target_key: str
    months_key: str


_VACANCY = _Ramp("physical_vacancy_curve", "vacancy_rate", "stabilized", "window_months")
_LTL = _Ramp("loss_to_lease", "ltl_percent", "floor", "burn_off_months")


def _cohort_ids(ctx: _Context, ramp: _Ramp) -> list[str]:
    ids = [str(row.get("cohort_id")) for row in ctx.canonical.get(ramp.table) or [] if isinstance(row, Mapping)]
    if not ids:
        ids = [str(c.get("cohort_id")) for c in ctx.canonical.get("unit_cohorts") or [] if isinstance(c, Mapping)]
    return list(dict.fromkeys(ids))


def _ramp_values(start: float, target: Optional[float], months: int, count: int) -> list[float]:
    if target is None or months <= 0:
        value = target if target is not None else start
        return [_r(value)] * count
    return [
        _r(start + (target - start) * i / months) if i < months else _r(target)
        for i in range(count)
    ]


def _segments(cohort_id: str, values: list[float], grid: list[str], field: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for month, value in zip(grid, values):
        if rows and rows[-1][field] == value:
            rows[-1]["end_period"] = month
        else:
            rows.append({"cohort_id": cohort_id, "start_period": month, "end_period": month, field: value})
    return rows


def _ramp_rows(ctx: _Context, ramp: _Ramp, value: Any, cohorts: Optional[Mapping[str, float]] = None):
    """Rows for a scalar (flat), a parameter dict (ramp) or explicit rows (list)."""
    grid = _grid(ctx)
    if not grid:
        return None
    if isinstance(value, list):
        return deepcopy(value)
    rows: list[dict[str, Any]] = []
    for cohort_id in _cohort_ids(ctx, ramp):
        if isinstance(value, Mapping):
            start = (value.get("start_by_cohort") or {}).get(cohort_id, value.get("start"))
            if _num(start) is None:
                return None
            target = _num(value.get(ramp.target_key))
            months = int(value.get(ramp.months_key) or 0)
            values = _ramp_values(float(start), target, months, len(grid))
        else:
            number = _num((cohorts or {}).get(cohort_id, value))
            if number is None:
                return None
            values = [number] * len(grid)
        rows.extend(_segments(cohort_id, values, grid, ramp.field))
    return rows


def _monthly(ctx: _Context, ramp: _Ramp, rows: list[Mapping[str, Any]]) -> Optional[dict[str, list[float]]]:
    grid = _grid(ctx)
    if not grid:
        return None
    out: dict[str, list[float]] = {}
    for cohort_id in _cohort_ids(ctx, ramp):
        spans = [
            (_month_index(str(r["start_period"])[:7]), _month_index(str(r["end_period"])[:7]), _num(r.get(ramp.field)))
            for r in rows
            if str(r.get("cohort_id")) == cohort_id
        ]
        values: list[float] = []
        for month in grid:
            index = _month_index(month)
            hits = [v for lo, hi, v in spans if lo <= index <= hi]
            if len(hits) != 1 or hits[0] is None:
                return None
            values.append(hits[0])
        out[cohort_id] = values
    return out


def _same_curve(ctx: _Context, ramp: _Ramp, rows: list, other: Optional[list]) -> bool:
    if other is None:
        return False
    a, b = _monthly(ctx, ramp, rows), _monthly(ctx, ramp, other)
    return a is not None and b is not None and _same(a, b)


def _infer_ramp(ctx: _Context, ramp: _Ramp, monthly: Mapping[str, list[float]]) -> Optional[dict[str, Any]]:
    finals = {values[-1] for values in monthly.values()}
    if len(finals) != 1:
        return None
    target = finals.pop()
    months = 0
    for values in monthly.values():
        settled = len(values)
        while settled > 0 and _same(values[settled - 1], target):
            settled -= 1
        months = max(months, settled)
    starts = {cid: values[0] for cid, values in monthly.items()}
    value: dict[str, Any] = {"start": _weighted_start(ctx, starts), ramp.target_key: target, ramp.months_key: months}
    if len(set(starts.values())) > 1:
        value["start_by_cohort"] = starts
    return value


def _weighted_start(ctx: _Context, starts: Mapping[str, float]) -> float:
    units = {
        str(c.get("cohort_id")): float(c.get("unit_count") or 0)
        for c in ctx.canonical.get("unit_cohorts") or []
        if isinstance(c, Mapping)
    }
    total = sum(units.get(cid, 0.0) for cid in starts)
    if not total:
        return _r(sum(starts.values()) / len(starts)) if starts else 0.0
    return _r(sum(value * units.get(cid, 0.0) for cid, value in starts.items()) / total)


def _ramp_reader(ramp: _Ramp, default: Callable[[_Context, str], _Choice]):
    flat_reader = _cohort_reader(ramp.table, ramp.field)

    def read(ctx: _Context) -> _Current:
        rows = [row for row in ctx.canonical.get(ramp.table) or [] if isinstance(row, Mapping)]
        if not rows:
            return _Current(NO_SECTION)
        if any(row.get(ramp.field) is None for row in rows):
            return _Current(PLACEHOLDER)
        choice = default(ctx, PRESENT)
        if choice.value is not None and _same_curve(ctx, ramp, rows, _ramp_rows(ctx, ramp, choice.value)):
            return _Current(PRESENT, choice.value)
        monthly = _monthly(ctx, ramp, rows)
        if monthly is None or all(len(set(values)) == 1 for values in monthly.values()):
            return flat_reader(ctx)  # flat (or unreadable) curve: resolver v1 shape
        inferred = _infer_ramp(ctx, ramp, monthly)
        if inferred is not None and _same_curve(ctx, ramp, rows, _ramp_rows(ctx, ramp, inferred)):
            return _Current(PRESENT, inferred)
        return _Current(PRESENT, deepcopy(rows))

    return read


def _ramp_writer(ramp: _Ramp):
    def write(ctx: _Context, value: Any, cohorts: Optional[dict[str, float]]) -> bool:
        rows = _ramp_rows(ctx, ramp, value, cohorts)
        if rows is None:
            return False
        ctx.canonical[ramp.table] = rows
        return True

    return write


# ── physical vacancy ─────────────────────────────────────────────────────


def _stabilized_vacancy(ctx: _Context) -> Optional[tuple[float, str, str, Optional[MarketPick]]]:
    pick = market_pick(ctx, "vacancy_stabilized")
    if pick is not None:
        value = float(pick.observation.scalar)
        return value, pick.source, (
            f"{pick.level} stabilized vacancy {value:.2%} from {pick.observation.provenance.provider}"
        ), pick
    house_value = house_stabilized_vacancy(ctx)
    if house_value is None:
        return None
    return house_value[0], house_vintage_source(ctx.bucket), house_value[1], None


def house_stabilized_vacancy(ctx: _Context) -> Optional[tuple[float, str]]:
    """House stabilized vacancy: max(vintage typical-band midpoint, house vacancy floor).

    The floor is ``default_physical_vacancy_rate`` for the deal's vintage and
    strategy (the guardrail floor), so a house-only deal is never below its own
    floor. The floors themselves are unchanged.
    """
    typical = house.typical_physical_vacancy_rate(ctx.year_built)
    floor = house.default_physical_vacancy_rate(ctx.year_built, strategy=ctx.strategy)
    if typical is None:
        return None
    guardrails = house.revenue_guardrails(ctx.year_built, strategy=ctx.strategy)
    low, high = guardrails["physical_vacancy_typical_low"], guardrails["physical_vacancy_typical_high"]
    value = max(typical, floor) if floor is not None else typical
    basis = (
        f"house {ctx.bucket} vintage stabilized vacancy {float(value):.2%} = max(typical-band midpoint "
        f"{float(typical):.2%} of {float(low):.2%}-{float(high):.2%}, house vacancy floor "
        f"{float(floor if floor is not None else typical):.2%} for the {ctx.strategy} strategy; "
        f"year built {ctx.year_built})"
    )
    return _r(float(value)), basis


def _vacancy_default(ctx: _Context, state: str) -> _Choice:
    start = _num((ctx.evidence.get("rent_roll") or {}).get("physical_vacancy_rate"))
    stabilized = _stabilized_vacancy(ctx)
    window = int(house.HOUSE_STABILIZATION_WINDOW_MONTHS[ctx.profile.deal_type])
    if start is None and stabilized is None:
        return _fallback(None, "No rent-roll evidence and no stabilized vacancy; no vacancy default is available.")
    if stabilized is None:
        return _Choice(
            SOURCE_DEAL_RENT_ROLL, _r(start),
            "Current physical vacancy from the rent roll, held flat: no stabilized vacancy is available "
            "(no market data and the year built is unknown).",
            confidence="high",
        )
    target, source, target_basis, pick = stabilized
    as_of = pick.as_of if pick else None
    confidence = pick.observation.confidence if pick else "medium"
    if start is None:
        return _Choice(source, _r(target), f"No rent-roll evidence; flat at the {target_basis}.",
                       as_of=as_of, confidence=confidence)
    if _same(_r(start), _r(target)):
        value: Any = _r(target)
    else:
        value = {"start": _r(start), "stabilized": _r(target), "window_months": window}
    basis = (
        f"Physical vacancy moves linearly from the rent-roll {start:.2%} to the {target_basis} over the "
        f"{ctx.profile.deal_type} stabilization window of {window} months, then stays flat "
        "(monthly segments per cohort)."
    )
    return _Choice(source, value, basis, as_of=as_of, confidence=confidence)


def _vacancy_candidates_v4(ctx: _Context) -> list[dict[str, Any]]:
    out = _vacancy_candidates(ctx)
    house_value = house_stabilized_vacancy(ctx)
    if house_value is not None:
        out.append(
            _candidate(
                house_vintage_source(ctx.bucket),
                house_value[0],
                f"House stabilized vacancy: {house_value[1]}.",
            )
        )
    return out


# ── loss to lease ────────────────────────────────────────────────────────


def _cohort_ltl(ctx: _Context) -> dict[str, float]:
    """Each cohort's loss to lease: (market - in-place) / market from the canonical rent roll data."""
    first_market: dict[str, float] = {}
    for row in sorted(
        (r for r in ctx.canonical.get("market_rent_curve") or [] if isinstance(r, Mapping)),
        key=lambda r: str(r.get("start_period", "")),
    ):
        first_market.setdefault(str(row.get("cohort_id")), _num(row.get("market_rent")) or 0.0)
    out: dict[str, float] = {}
    inplace = {
        str(c.get("cohort_id")): _num(c.get("initial_inplace_rent")) or 0.0
        for c in ctx.canonical.get("unit_cohorts") or []
        if isinstance(c, Mapping)
    }
    for cohort_id in _cohort_ids(ctx, _LTL):
        market = first_market.get(cohort_id, 0.0)
        rent = inplace.get(cohort_id, 0.0)
        out[cohort_id] = _r(max(0.0, (market - rent) / market)) if market > 0 else 0.0
    return out


def _ltl_default(ctx: _Context, state: str) -> _Choice:
    if _num((ctx.evidence.get("rent_roll") or {}).get("loss_to_lease_rate")) is None:
        return _fallback(0.0, "No rent-roll market rents: loss to lease defaults to 0%.")
    starts = _cohort_ltl(ctx)
    if not starts:
        return _fallback(0.0, "No unit cohorts: loss to lease defaults to 0%.")
    start = _weighted_market_ltl(ctx, starts)
    floor = house.default_loss_to_lease_rate(ctx.year_built, strategy=ctx.strategy)
    months = int(house.HOUSE_LTL_BURN_OFF_MONTHS)
    uniform = len(set(starts.values())) == 1
    if floor is None:
        if uniform:
            return _Choice(SOURCE_DEAL_RENT_ROLL, next(iter(starts.values())),
                           "Rent-roll loss to lease held flat: the year built is unknown, so there is no "
                           "house vintage floor to burn off to.", confidence="high")
        value: Any = {"start": start, "floor": None, "burn_off_months": 0, "start_by_cohort": starts}
        return _Choice(SOURCE_DEAL_RENT_ROLL, value,
                       "Rent-roll loss to lease per cohort held flat: the year built is unknown, so there "
                       "is no house vintage floor to burn off to.", confidence="high")
    target = _r(float(floor))
    if uniform and _same(next(iter(starts.values())), target):
        return _Choice(house_vintage_source(ctx.bucket), target,
                       f"Rent-roll loss to lease equals the house {ctx.bucket} vintage floor {target:.2%}.",
                       confidence="medium")
    value = {"start": start, "floor": target, "burn_off_months": months}
    if not uniform:
        value["start_by_cohort"] = starts
    basis = (
        f"Each cohort's rent-roll loss to lease (market vs in-place; {start:.2%} market-weighted) burns off "
        f"linearly over {months} months (one lease term) to the house {ctx.bucket} vintage LTL floor "
        f"{target:.2%} ({ctx.strategy} strategy), then stays flat (monthly segments per cohort)."
    )
    return _Choice(house_vintage_source(ctx.bucket), value, basis, confidence="medium")


def _weighted_market_ltl(ctx: _Context, starts: Mapping[str, float]) -> float:
    rent_roll = _num((ctx.evidence.get("rent_roll") or {}).get("loss_to_lease_rate"))
    return _r(rent_roll) if rent_roll is not None else _weighted_start(ctx, starts)


def _ltl_candidates_v4(ctx: _Context) -> list[dict[str, Any]]:
    return [c for c in _ltl_candidates(ctx) if c["source"] != "engine_fallback"]


# ── concessions ──────────────────────────────────────────────────────────


def _concession_parts(ctx: _Context):
    t12 = _t12_rate(ctx, "concessions")
    pick = market_pick(ctx, "concession_rate")
    market = float(pick.observation.scalar) if pick else None
    house_year1 = house.default_year1_concessions_rate(ctx.year_built, strategy=ctx.strategy)
    house_stabilized = house.stabilized_concessions_rate(ctx.year_built)

    if t12 is not None:
        year1 = (_r(t12), SOURCE_DEAL_T12, f"T12 concessions / GPR {t12:.2%}")
    elif market is not None:
        year1 = (_r(market), pick.source, f"{pick.level} concession rate {market:.2%} from "
                 f"{pick.observation.provenance.provider}")
    elif house_year1 is not None:
        year1 = (_r(float(house_year1)), house_vintage_source(ctx.bucket),
                 f"house {ctx.bucket} vintage year-1 concessions floor {float(house_year1):.2%}")
    else:
        year1 = None
    if market is not None:
        stabilized = (_r(market), pick.source, f"{pick.level} concession rate {market:.2%}")
    elif house_stabilized is not None:
        stabilized = (_r(float(house_stabilized)), house_vintage_source(ctx.bucket),
                      f"house {ctx.bucket} vintage concessions_typical_low {float(house_stabilized):.2%}")
    else:
        stabilized = None
    return year1, stabilized, pick


def _concession_default(ctx: _Context, state: str) -> _Choice:
    if state == ABSENT:
        return _fallback(0.0, "No concession_schedule: the engine applies no concessions.")
    year1, stabilized, pick = _concession_parts(ctx)
    months = int(house.HOUSE_YEAR1_CONCESSION_MONTHS)
    if year1 is None:
        return _fallback(0.0, "No T12 concessions, no market concession rate and no year built: no concessions.")
    rate, source, label = year1
    confidence = "high" if source == SOURCE_DEAL_T12 else (pick.observation.confidence if pick and
                                                           source == pick.source else "medium")
    as_of = pick.as_of if pick and source == pick.source else None
    if stabilized is None or stabilized[0] == 0:
        after = (f"then none ({stabilized[2]})" if stabilized else
                 "then none (no stabilized concession level is available)")
        return _Choice(source, rate, f"Year-1 concessions from the {label} for {months} months, {after}.",
                       as_of=as_of, confidence=confidence)
    value = {"year1": rate, "stabilized": stabilized[0], "year1_months": months}
    basis = (
        f"Year-1 concessions from the {label} for {months} months, then the stabilized level from the "
        f"{stabilized[2]} (pct_rent concession_schedule rows)."
    )
    return _Choice(source, value, basis, as_of=as_of, confidence=confidence)


def _concession_rows(ctx: _Context, value: Any) -> Optional[list[dict[str, Any]]]:
    if not isinstance(value, Mapping) or len(ctx.start) != 7:
        return None
    months = int(value.get("year1_months") or house.HOUSE_YEAR1_CONCESSION_MONTHS)
    first = _month_index(ctx.start)
    rows = []
    year1, stabilized = _num(value.get("year1")) or 0.0, _num(value.get("stabilized")) or 0.0
    if year1 > 0:
        rows.append({
            "concession_id": CONCESSION_YEAR1_ID, "start_month": ctx.start,
            "end_month": _month(first + months - 1), "concession_type": "pct_rent",
            "amount": year1, "applies_to_cohort": "ALL",
        })
    if stabilized > 0 and len(ctx.end) == 7 and first + months <= _month_index(ctx.end):
        rows.append({
            "concession_id": CONCESSION_STABILIZED_ID, "start_month": _month(first + months),
            "end_month": ctx.end, "concession_type": "pct_rent",
            "amount": stabilized, "applies_to_cohort": "ALL",
        })
    return rows


def _scalar_concession_rows(ctx: _Context, value: Any) -> Optional[list[dict[str, Any]]]:
    if _num(value) is None or len(ctx.start) != 7:
        return None
    probe = {"time_grid": {"analysis_start_date": ctx.start + "-01"}}
    ensure_year1_concession_schedule(probe, rate=Decimal(str(value)))
    rows = probe.get("concession_schedule") or []
    for row in rows:
        row["concession_id"] = CONCESSION_YEAR1_ID
    return rows


def _read_concessions_v4(ctx: _Context) -> _Current:
    if "concession_schedule" in ctx.canonical and ctx.canonical["concession_schedule"] is None:
        return _Current(PLACEHOLDER)
    schedule = ctx.canonical.get("concession_schedule") or []
    if not schedule:
        return _Current(ABSENT)
    choice = _concession_default(ctx, PRESENT)
    expected = (
        _concession_rows(ctx, choice.value) if isinstance(choice.value, Mapping)
        else _scalar_concession_rows(ctx, choice.value)
    )
    if expected and _same(schedule, expected):
        return _Current(PRESENT, choice.value)
    if (
        len(schedule) == 2
        and all(isinstance(row, Mapping) and row.get("concession_type") == "pct_rent"
                and row.get("applies_to_cohort") == "ALL" for row in schedule)
        and str(schedule[0].get("start_month")) == ctx.start
    ):
        months = _month_index(str(schedule[1].get("start_month"))) - _month_index(ctx.start)
        inferred = {"year1": _num(schedule[0].get("amount")), "stabilized": _num(schedule[1].get("amount")),
                    "year1_months": months}
        if _same(schedule, _concession_rows(ctx, inferred)):
            return _Current(PRESENT, inferred)
    return _read_concessions(ctx)


def _write_concessions_v4(ctx: _Context, value: Any, cohorts: Optional[dict[str, float]]) -> bool:
    if isinstance(value, Mapping):
        rows = _concession_rows(ctx, value)
        if rows is None:
            return False
        if rows:
            ctx.canonical["concession_schedule"] = rows
        else:
            ctx.canonical.pop("concession_schedule", None)
        return True
    return _write_concessions(ctx, value, cohorts)


def _concession_candidates(ctx: _Context) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    t12_rate = _t12_rate(ctx, "concessions")
    if t12_rate is not None:
        out.append(_candidate(SOURCE_DEAL_T12, _r(t12_rate), "T12 concessions / gross potential rent (year 1)."))
    house_year1 = house.default_year1_concessions_rate(ctx.year_built, strategy=ctx.strategy)
    if house_year1 is not None:
        out.append(_candidate(house_vintage_source(ctx.bucket), float(house_year1),
                              f"House {ctx.bucket} vintage year-1 concessions floor."))
    house_stabilized = house.stabilized_concessions_rate(ctx.year_built)
    if house_stabilized is not None:
        out.append(_candidate(house_vintage_source(ctx.bucket), float(house_stabilized),
                              f"House {ctx.bucket} vintage concessions_typical_low (stabilized level)."))
    return out


# ── exit cap rate ────────────────────────────────────────────────────────


def _exit_year(ctx: _Context) -> Optional[int]:
    exit_section = ctx.canonical.get("exit_assumptions")
    month = (exit_section or {}).get("exit_month") if isinstance(exit_section, Mapping) else None
    month = str(month or ctx.end)
    try:
        return int(month[:4])
    except ValueError:
        return None


def _market_exit_cap(ctx: _Context) -> Optional[tuple[float, MarketPick, int]]:
    year = _exit_year(ctx)
    pick = market_pick(ctx, "market_cap_rate")
    if pick is None or year is None:
        return None
    rate = annual_value(pick.observation, year)
    if rate is None or not math.isfinite(rate):
        return None
    return _r(rate), pick, year


_read_exit_scalar = _scalar_reader("exit_assumptions", "exit_cap_rate")


def _read_exit_cap(ctx: _Context) -> _Current:
    current = _read_exit_scalar(ctx)
    # A modeled exit without a cap rate takes the market rate when one exists,
    # else the going-in cap rate plus the house spread (ADR 0003 §3).
    if current.state == ABSENT and (_market_exit_cap(ctx) is not None or _house_exit_cap(ctx) is not None):
        return _Current(PLACEHOLDER)
    return current


def _exit_cap_default_v4(ctx: _Context, state: str) -> _Choice:
    if state != NO_SECTION:
        market = _market_exit_cap(ctx)
        if market is not None:
            rate, pick, year = market
            return _Choice(
                pick.source,
                rate,
                f"{pick.level} market cap rate for the exit year {year} ({pick.observation.provenance.provider}, "
                f"calendar-year mean of the quarterly series).",
                as_of=pick.as_of,
                confidence=pick.observation.confidence,
            )
    return _exit_cap_default(ctx, state)


# ── override shapes (called from resolver._normalize_override) ───────────

# Curve shapes an analyst override may carry, per key: required fields,
# optional fields, and which fields are whole months.
_CURVE_SHAPES: dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {
    "physical_vacancy": (("start", "stabilized", "window_months"), ("start_by_cohort",), ("window_months",)),
    "loss_to_lease": (("start", "floor", "burn_off_months"), ("start_by_cohort",), ("burn_off_months",)),
    "concessions": (("year1", "stabilized"), ("year1_months",), ("year1_months",)),
}
_VECTOR_KEYS = ("rent_growth",)
_NULLABLE = {("loss_to_lease", "floor")}


def _finite(key: str, label: str, value: Any) -> float:
    number = _num(value)
    if number is None or not math.isfinite(number):
        raise _override_error(f"override {key}.{label} must be a finite number")
    return number


def _override_error(message: str) -> Exception:
    from engine.assumptions.resolver import AssumptionOverrideError

    return AssumptionOverrideError(message)


def normalize_override_value(key: str, value: Any) -> Any:
    """Validate an override value: a finite number, a per-year vector
    (``rent_growth``), or the curve object the key writes (vacancy, LTL,
    concessions). Returns the normalized value."""
    if isinstance(value, (list, tuple)):
        if key not in _VECTOR_KEYS:
            raise _override_error(f"override for {key!r} must be a finite number, not a vector")
        if not value:
            raise _override_error(f"override vector for {key!r} must not be empty")
        return [_finite(key, f"[{i}]", v) for i, v in enumerate(value)]
    if isinstance(value, Mapping):
        if key not in _CURVE_SHAPES:
            raise _override_error(f"override for {key!r} must be a finite number, not an object")
        required, optional, whole = _CURVE_SHAPES[key]
        missing = [f for f in required if f not in value]
        unknown = sorted(set(value) - set(required) - set(optional))
        if missing or unknown:
            raise _override_error(
                f"override curve for {key!r} needs fields {list(required)} (optional {list(optional)}); "
                f"missing {missing}, unknown {unknown}"
            )
        out: dict[str, Any] = {}
        for name in required + optional:
            if name not in value:
                continue
            raw = value[name]
            if name == "start_by_cohort":
                if not isinstance(raw, Mapping) or not raw:
                    raise _override_error(f"override {key}.start_by_cohort must be a non-empty object")
                out[name] = {str(c): _r(_finite(key, f"start_by_cohort.{c}", v)) for c, v in raw.items()}
            elif raw is None and (key, name) in _NULLABLE:
                out[name] = None
            elif name in whole:
                number = _finite(key, name, raw)  # whole months
                if number < 0 or number != int(number):
                    raise _override_error(f"override {key}.{name} must be a whole number of months >= 0")
                out[name] = int(number)
            else:
                out[name] = _r(_finite(key, name, raw))
        if key == "concessions":
            out.setdefault("year1_months", int(house.HOUSE_YEAR1_CONCESSION_MONTHS))
        return out
    number = _num(value)
    if number is None or not math.isfinite(number):
        raise _override_error(f"override for {key!r} must be a finite number")
    return number


# ── spec wiring (called from resolver._specs) ────────────────────────────


def _no_adr_note(_source: str) -> Optional[int]:
    # These keys already resolve by ADR 0003 §3; no "would select" disclosure.
    return None


def apply(specs: tuple[_Spec, ...]) -> tuple[_Spec, ...]:
    """Swap in the slice-4 readers, writers, defaults and candidates."""
    vacancy_read = _ramp_reader(_VACANCY, _vacancy_default)
    ltl_read = _ramp_reader(_LTL, _ltl_default)
    patches: dict[str, dict[str, Any]] = {
        "rent_growth": dict(
            unit="decimal_yoy_by_analysis_year",
            read=_read_rent_growth,
            write=_write_rent_growth,
            default=_rent_growth_default_v4,
            gather=_rent_growth_candidates,
            market_metrics=("rent_growth_forecast", "rent_growth_long_run"),
            patch_paths=(("growth_assumptions", "annual_growth_rate"), ("growth_assumptions", "annual_growth_rates")),
        ),
        "collection_loss": dict(default=_collection_loss_default, gather=_collection_loss_candidates),
        "physical_vacancy": dict(
            read=vacancy_read, write=_ramp_writer(_VACANCY), default=_vacancy_default,
            gather=_vacancy_candidates_v4,
        ),
        "loss_to_lease": dict(
            read=ltl_read, write=_ramp_writer(_LTL), default=_ltl_default, gather=_ltl_candidates_v4,
        ),
        "concessions": dict(
            read=_read_concessions_v4, write=_write_concessions_v4, default=_concession_default,
            gather=_concession_candidates,
        ),
        "exit_cap_rate": dict(read=_read_exit_cap, default=_exit_cap_default_v4),
    }
    return tuple(
        replace(spec, adr_rank=_no_adr_note, **patches[spec.key]) if spec.key in patches else spec
        for spec in specs
    )


def _rent_growth_default_v4(ctx: _Context, state: str) -> _Choice:
    if state in (ABSENT, NO_SECTION):
        return _rent_growth_default(ctx, state)
    return rent_growth_vector(ctx)


__all__ = [
    "HOUSE_DEFAULT_COLLECTION_LOSS_SOURCE",
    "HOUSE_LONG_RUN_RENT_GROWTH_SOURCE",
    "MarketPick",
    "analysis_years",
    "apply",
    "carry_market_default",
    "market_candidates",
    "market_observations",
    "market_pick",
    "msa_geo",
    "normalize_override_value",
    "profile_geo",
    "rent_growth_vector",
]
