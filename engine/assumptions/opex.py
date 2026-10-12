"""Opex treatment for the assumption layer (ADR 0003 §6, slice 5).

Each opex_table row gets two entries:

- ``opex.<slug>.base``: the line in annual dollars (``detail`` carries the
  calculation_type, base_value and per-unit amount). Percent-of-revenue rows
  show the percent; they are never converted to dollars. Ingested lines cite
  ``deal:t12`` with the raw T12 source rows.
- ``opex.<slug>.growth``: the annual growth the engine applies. An analyst
  rate on the row (or an explicit override) wins; otherwise the house
  per-category table in ``engine/underwriting_policy.py``. Real estate taxes
  defer to ``engine/property_tax.py``.

For an ingested deal the resolver also writes the house growth into every row
that has no ``growth_rate`` and adds Insurance, Repairs & Maintenance and a
Property Management Fee from house defaults when the T12 carries none of them.
Hand-authored canonicals are never filled: a missing growth_rate stays at the
engine's 0% and the house rate is recorded as a candidate.

The resolver calls this module through three small hooks (see
``engine/assumptions/resolver.py``); the engine never reads it.
"""

from __future__ import annotations

import re
from copy import deepcopy
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional

from engine import underwriting_policy as house
from engine.assumptions import resolver as _r
from engine.assumptions.canonical_ops import (
    annual_opex_amount,
    matching_opex_rows,
    opex_bucket_for_policy,
    sum_units,
)
from engine.assumptions.registry import (
    HOUSE_YEAR1_INSURANCE_FLOOR,
    SOURCE_DEAL_T12,
    SOURCE_ENGINE_FALLBACK,
    house_vintage_source,
)
from engine.ingest.t12_parser import _matches_category_map
from engine.property_tax import is_property_tax_label

OPEX_KEY_PREFIX = "opex."
_OPEX_KEY = re.compile(r"^opex\.([a-z0-9_]+)\.(base|growth)$")

SOURCE_HOUSE_OPEX_GROWTH = "house:opex_growth_table"
SOURCE_HOUSE_PROPERTY_TAX = "house:property_tax_module"
SOURCE_HOUSE_MANAGEMENT_FEE = "house:management_fee_pct_egr"

REAL_ESTATE_TAXES = "Real Estate Taxes"
PERCENT_OF_REVENUE = house.PERCENT_OF_REVENUE_CALCULATION_TYPES
_PERCENT_UNITS = {"percent_egr": "decimal_of_egr", "percent_rent": "decimal_of_rent"}
_DOLLAR_UNIT = "usd_per_year"
_GROWTH_UNIT = "decimal_yoy"

# canonical_ops policy bucket -> house growth-table category.
_BUCKET_CATEGORY = {
    "insurance": "Insurance",
    "management_fees": "Property Management Fee",
    "utilities": "Utilities",
    "repairs_maintenance": "Repairs & Maintenance",
    "make_ready": "Turnover / Make-Ready",
    "payroll": "Payroll",
    "marketing": "Marketing / Advertising",
    "contract_services": "Contract Services",
    "general_administrative": "Administrative",
}

# Core lines an ingested deal must carry; field names match the T12 evidence
# the v1 insurance_per_unit / r_and_m_per_unit keys read.
CORE_INSURANCE = "insurance"
CORE_REPAIRS_MAINTENANCE = "repairs_maintenance"
CORE_MANAGEMENT_FEE = "management_fee"
_CORE_CATEGORY = {
    CORE_INSURANCE: "Insurance",
    CORE_REPAIRS_MAINTENANCE: "Repairs & Maintenance",
    CORE_MANAGEMENT_FEE: "Property Management Fee",
}
_CORE_BY_EVIDENCE_FIELD = {
    "insurance_annual": CORE_INSURANCE,
    "repairs_maintenance_annual": CORE_REPAIRS_MAINTENANCE,
}


# ── keys ─────────────────────────────────────────────────────────────────


def is_opex_key(key: str) -> bool:
    return bool(isinstance(key, str) and _OPEX_KEY.match(key))


def check_override_keys(keys: Iterable[str]) -> set[str]:
    """The per-line opex keys among ``keys``. Only growth can be overridden."""
    accepted: set[str] = set()
    for key in keys:
        match = _OPEX_KEY.match(key) if isinstance(key, str) else None
        if not match:
            continue
        if match.group(2) == "base":
            raise _r.AssumptionOverrideError(
                f"{key} is display-only; change the opex_table row (or insurance_per_unit / "
                "r_and_m_per_unit) instead"
            )
        accepted.add(key)
    return accepted


def _slug(name: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name or "").lower()).strip("_") or "line"


def opex_line_slugs(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    """One slug per row, in row order; duplicates get ``_2``, ``_3``..."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for row in rows:
        slug = _slug(row.get("category_name"))
        seen[slug] = seen.get(slug, 0) + 1
        out.append(slug if seen[slug] == 1 else f"{slug}_{seen[slug]}")
    return out


# ── categories and growth ────────────────────────────────────────────────


def growth_category(name: Any) -> Optional[str]:
    """The house growth-table category for an opex line name, else None (default rate).

    Parser canonical names map to themselves; other labels go through the T12
    parser's matcher, then the canonical_ops policy buckets. Only the labels
    the property-tax module replaces count as real estate taxes.
    """
    label = str(name or "")
    if is_property_tax_label(label):
        return REAL_ESTATE_TAXES
    if label in house.OPEX_GROWTH_TABLE and label != REAL_ESTATE_TAXES:
        return label
    parsed = _matches_category_map(label)
    if parsed in house.OPEX_GROWTH_TABLE and parsed != REAL_ESTATE_TAXES:
        return parsed
    return _BUCKET_CATEGORY.get(opex_bucket_for_policy(label) or "")


def _calc(row: Mapping[str, Any]) -> str:
    return str(row.get("calculation_type") or "fixed_annual")


def _house_growth(row: Mapping[str, Any]) -> tuple[Optional[str], str, Optional[Decimal]]:
    category = growth_category(row.get("category_name"))
    constant, rate = house.default_opex_growth_rate(category, calculation_type=_calc(row))
    return category, constant, rate


def _growth_basis(row: Mapping[str, Any], category: Optional[str], constant: str, rate: Decimal) -> str:
    name = row.get("category_name")
    if _calc(row) in PERCENT_OF_REVENUE:
        return (
            f"{name} is entered as {_calc(row)}: it already moves with modeled revenue, so the house "
            f"table applies no growth ({constant} = {float(rate):.2%}; growth would compound the "
            "percentage itself)."
        )
    mapped = f"maps to the house category {category!r}" if category else "is not in the house table"
    return (
        f"{name} {mapped}: {constant} = {float(rate):.2%} a year (engine/underwriting_policy.py). "
        "A house assumption to be reviewed, not market data."
    )


def _property_tax_basis(row: Mapping[str, Any]) -> str:
    return (
        f"{row.get('category_name')} is owned by engine/property_tax.py: apply_property_tax_policy "
        "resets the year-1 tax to purchase price x assessment ratio x millage and keeps the row's "
        "own growth_rate. The house table has no rate for it (OPEX_GROWTH_REAL_ESTATE_TAXES) and "
        "the resolver writes none, so growth is never stacked on the reassessment; without a "
        "growth_rate the engine holds the reassessed tax flat."
    )


# ── amounts and T12 evidence ─────────────────────────────────────────────


def _line_facts(canonical: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    calc = _calc(row)
    base = _r._num(row.get("base_value"))
    facts: dict[str, Any] = {"category_name": row.get("category_name"), "calculation_type": calc,
                             "base_value": base}
    if calc in PERCENT_OF_REVENUE:
        facts.update(percent_of_revenue=base, annual_amount=None, per_unit_annual=None)
        return facts
    try:
        annual = annual_opex_amount(canonical, row)
    except ValueError:
        facts.update(annual_amount=None, per_unit_annual=None)
        return facts
    units = sum_units(canonical)
    facts["annual_amount"] = float(annual)
    facts["per_unit_annual"] = float(annual / Decimal(units)) if units else None
    return facts


def _line_value(facts: Mapping[str, Any]) -> Optional[float]:
    if facts["calculation_type"] in PERCENT_OF_REVENUE:
        return facts["percent_of_revenue"]
    return facts["annual_amount"]


def _describe(facts: Mapping[str, Any]) -> str:
    if facts.get("calculation_type") in PERCENT_OF_REVENUE:
        return f"{float(facts.get('percent_of_revenue') or 0):.2%} of revenue (not converted to dollars)"
    annual, per_unit = facts.get("annual_amount"), facts.get("per_unit_annual")
    if annual is None:
        return "amount unavailable"
    text = f"${annual:,.2f} a year"
    return f"{text}, ${per_unit:,.2f} per unit a year" if per_unit is not None else text


def _line_unit(row: Mapping[str, Any]) -> str:
    return _PERCENT_UNITS.get(_calc(row), _DOLLAR_UNIT)


def t12_opex_line_evidence(
    canonical: Mapping[str, Any],
    provenance: Optional[Mapping[str, Any]] = None,
) -> dict[str, dict[str, Any]]:
    """Every opex line as ingested, keyed by category_name, with its T12 source rows."""
    out: dict[str, dict[str, Any]] = {}
    for row in canonical.get("opex_table") or []:
        name = str(row.get("category_name", ""))
        facts = _line_facts(canonical, row)
        facts.pop("category_name")
        rows = (provenance or {}).get(name)
        rows = rows if isinstance(rows, list) else row.get("source_rows")
        facts["source_rows"] = [str(r) for r in rows] if isinstance(rows, list) else []
        out[name] = facts
    return out


def _t12_lines(ctx: Any) -> dict[str, dict[str, Any]]:
    """The T12 opex snapshot for an ingested deal, recorded once if ingest predates it."""
    if not ctx.ingested:
        return {}
    t12 = ctx.evidence.get("t12")
    if isinstance(t12, Mapping) and isinstance(t12.get("opex_lines"), Mapping):
        return dict(t12["opex_lines"])
    snapshot = t12_opex_line_evidence(ctx.canonical)
    gate = ctx.canonical["metadata"]["ingest_gate"]
    evidence = gate.setdefault("evidence", {})
    if not isinstance(evidence.get("t12"), dict):
        evidence["t12"] = {}
    evidence["t12"]["opex_lines"] = snapshot
    ctx.evidence["t12"] = evidence["t12"]
    return snapshot


# ── missing core lines ───────────────────────────────────────────────────


def _has_core(canonical: Mapping[str, Any], core: str) -> bool:
    """Whether any line already covers ``core`` under a matching name (no double count)."""
    rows = canonical.get("opex_table") or []
    names = [str(row.get("category_name", "")) for row in rows]
    if core == CORE_INSURANCE:
        return bool(matching_opex_rows(canonical, "insurance"))
    if core == CORE_REPAIRS_MAINTENANCE:
        return any(opex_bucket_for_policy(name) == "repairs_maintenance" for name in names)
    return any(
        opex_bucket_for_policy(name) == "management_fees"
        or _matches_category_map(name) == "Property Management Fee"
        or "management" in name.lower()
        or "mgmt" in name.lower()
        for name in names
    )


def _house_core(ctx: Any, core: str) -> Optional[dict[str, Any]]:
    """The house line for a missing core category: row, per-unit candidate and source."""
    category = _CORE_CATEGORY[core]
    if core == CORE_INSURANCE:
        per_unit = house.default_year1_insurance_per_unit(None)
        source = HOUSE_YEAR1_INSURANCE_FLOOR
        basis = (
            f"The T12 has no insurance line; house Year-1 insurance default ${float(per_unit):,.2f}/unit "
            "(DEFAULT_YEAR1_INSURANCE_PER_UNIT; the vintage framework has no insurance band)."
        )
    elif core == CORE_REPAIRS_MAINTENANCE:
        if ctx.bucket is None:
            return None
        per_unit = house.default_r_and_m_per_unit(ctx.year_built, strategy=ctx.strategy)
        if per_unit is None:
            return None
        source = house_vintage_source(ctx.bucket)
        basis = (
            f"The T12 has no repairs & maintenance line; year built {ctx.year_built} maps to the "
            f"{ctx.bucket} vintage bucket: house R&M ${float(per_unit):,.2f}/unit (VINTAGE_FRAMEWORK_2026, "
            f"{ctx.strategy} strategy)."
        )
    else:
        pct = house.DEFAULT_MANAGEMENT_FEE_PCT_EGR
        return {
            "row": {"category_name": category, "calculation_type": "percent_egr",
                    "base_value": float(pct), "recoverable_flag": False},
            "source": SOURCE_HOUSE_MANAGEMENT_FEE,
            "value": float(pct),  # a percent of EGR, never converted to dollars
            "basis": (
                f"The T12 has no management fee line; house fee {float(pct):.2%} of EGR "
                "(DEFAULT_MANAGEMENT_FEE_PCT_EGR)."
            ),
        }
    return {
        "row": {"category_name": category, "calculation_type": "per_unit",
                "base_value": float(per_unit), "recoverable_flag": False},
        "source": source,
        "value": float(per_unit * Decimal(ctx.units)),  # annual dollars
        "per_unit": float(per_unit),
        "basis": basis,
    }


def _missing_from_t12(ctx: Any, core: str) -> bool:
    snapshot = {"opex_table": [{"category_name": name} for name in _t12_lines(ctx)]}
    return not _has_core(snapshot, core)


def prepare_opex_table(ctx: Any) -> None:
    """Hook 1 (before the v1 keys): snapshot the T12 lines and add missing core lines.

    Only an ingested deal is filled, only when no line covers the category
    under any matching name, and never with real estate taxes (the
    property-tax module owns them).
    """
    if not ctx.ingested or not ctx.units:
        return
    _t12_lines(ctx)
    table = ctx.canonical.setdefault("opex_table", [])
    for core in (CORE_INSURANCE, CORE_REPAIRS_MAINTENANCE, CORE_MANAGEMENT_FEE):
        if _has_core(ctx.canonical, core):
            continue
        fill = _house_core(ctx, core)
        if fill is not None:
            table.append(dict(fill["row"]))


def _house_fill_for(ctx: Any, row: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """The house fill a row came from, if it is the line prepare_opex_table adds."""
    if not ctx.ingested:
        return None
    for core, category in _CORE_CATEGORY.items():
        if row.get("category_name") == category and _missing_from_t12(ctx, core):
            return _house_core(ctx, core)
    return None


def filled_core_candidate(ctx: Any, evidence_field: str) -> Optional[dict[str, Any]]:
    """Hook 3: the house per-unit candidate for insurance_per_unit / r_and_m_per_unit
    when that core line was added from house defaults."""
    core = _CORE_BY_EVIDENCE_FIELD.get(evidence_field)
    if core is None or not ctx.ingested or not _missing_from_t12(ctx, core):
        return None
    if not any(row.get("category_name") == _CORE_CATEGORY[core] for row in ctx.canonical.get("opex_table") or []):
        return None
    fill = _house_core(ctx, core)
    if fill is None:
        return None
    return _r._candidate(fill["source"], fill["per_unit"], fill["basis"])


# ── per-line entries ─────────────────────────────────────────────────────


def _no_adr_note(_source: str) -> Optional[int]:
    return None


def _base_spec(key: str, row: dict[str, Any], t12: Optional[Mapping[str, Any]]) -> "_r._Spec":
    name = row.get("category_name")

    def read(ctx: Any) -> "_r._Current":
        return _r._Current(_r.PRESENT, _line_value(_line_facts(ctx.canonical, row)))

    def t12_candidate() -> Optional[dict[str, Any]]:
        if t12 is None:
            return None
        rows = t12.get("source_rows") or []
        value = t12.get("percent_of_revenue") if t12.get("calculation_type") in PERCENT_OF_REVENUE \
            else t12.get("annual_amount")
        return _r._candidate(
            SOURCE_DEAL_T12,
            value,
            f"T12 line {name!r} ({t12.get('calculation_type')}): {_describe(t12)}"
            + (f"; source rows: {'; '.join(rows)}." if rows else "."),
            calculation_type=t12.get("calculation_type"),
            per_unit_annual=t12.get("per_unit_annual"),
            source_rows=list(rows) or None,
        )

    def default(ctx: Any, _state: str) -> "_r._Choice":
        evidence = t12_candidate()
        if evidence is not None:
            return _r._choice_from(evidence, ctx)
        fill = _house_fill_for(ctx, row)
        if fill is not None:
            return _r._Choice(fill["source"], fill["value"], fill["basis"], confidence="medium")
        return _r._fallback(None, f"No T12 line or house default for {name!r}; the value is the canonical's own.")

    def gather(ctx: Any) -> list[dict[str, Any]]:
        out = [c for c in (t12_candidate(),) if c is not None]
        fill = _house_fill_for(ctx, row)
        if fill is not None:
            out.append(_r._candidate(fill["source"], fill["value"], fill["basis"],
                                     calculation_type=fill["row"]["calculation_type"],
                                     per_unit_annual=fill.get("per_unit")))
        if not out:
            out.append(_r._candidate(SOURCE_ENGINE_FALLBACK, None, "No T12 or house evidence for this line."))
        return out

    return _r._Spec(key=key, unit=_line_unit(row), read=read, write=_r._write_nothing,
                    default=default, gather=gather, adr_rank=_no_adr_note)


def _growth_spec(key: str, row: dict[str, Any]) -> "_r._Spec":
    category, constant, rate = _house_growth(row)
    deferred = rate is None  # real estate taxes: the property-tax module owns them

    def read(ctx: Any) -> "_r._Current":
        if row.get("growth_phases"):
            return _r._Current(_r.PRESENT, deepcopy(row["growth_phases"]))
        if row.get("growth_rate") is not None:
            return _r._Current(_r.PRESENT, _r._num(row["growth_rate"]))
        if deferred or not ctx.ingested:
            return _r._Current(_r.ABSENT)
        return _r._Current(_r.PLACEHOLDER)

    def write(_ctx: Any, value: Any, _cohorts: Optional[dict[str, float]]) -> bool:
        number = _r._num(value)
        if number is None:
            return False
        row["growth_rate"] = number
        row.pop("growth_phases", None)
        return True

    def house_candidate() -> dict[str, Any]:
        if deferred:
            return _r._candidate(SOURCE_HOUSE_PROPERTY_TAX, 0.0, _property_tax_basis(row))
        return _r._candidate(SOURCE_HOUSE_OPEX_GROWTH, float(rate), _growth_basis(row, category, constant, rate),
                             constant=constant)

    def default(ctx: Any, state: str) -> "_r._Choice":
        candidate = house_candidate()
        if state == _r.ABSENT and not deferred:
            return _r._fallback(
                0.0,
                f"{row.get('category_name')} carries no growth_rate and the canonical was not ingested, so "
                f"the engine applies none; the house rate {float(rate):.2%} ({constant}) is a candidate and "
                "is not written to a hand-authored canonical.",
            )
        return _r._Choice(candidate["source"], candidate["value"], candidate["basis"], confidence="medium")

    def gather(_ctx: Any) -> list[dict[str, Any]]:
        return [
            house_candidate(),
            _r._candidate(SOURCE_ENGINE_FALLBACK, 0.0, "No growth_rate on the row: the engine applies none."),
        ]

    return _r._Spec(key=key, unit=_GROWTH_UNIT, read=read, write=write, default=default, gather=gather,
                    adr_rank=_no_adr_note)


def resolve_opex_entries(
    ctx: Any,
    prior_entries: Mapping[str, Any],
    explicit: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    """Hook 2 (after the v1 keys): ``opex.<slug>.base`` and ``opex.<slug>.growth`` per row."""
    rows = [row for row in ctx.canonical.get("opex_table") or [] if isinstance(row, dict)]
    slugs = opex_line_slugs(rows)
    growth_keys = {f"{OPEX_KEY_PREFIX}{slug}.growth" for slug in slugs}
    unknown = sorted(key for key in explicit if is_opex_key(key) and key not in growth_keys)
    if unknown:
        raise _r.AssumptionOverrideError(
            f"unknown opex lines {unknown}; this deal has {sorted(growth_keys)}"
        )
    t12 = _t12_lines(ctx)
    entries: dict[str, Any] = {}
    for slug, row in zip(slugs, rows):
        name = str(row.get("category_name", ""))
        line_t12 = t12.get(name)
        base_key = f"{OPEX_KEY_PREFIX}{slug}.base"
        growth_key = f"{OPEX_KEY_PREFIX}{slug}.growth"

        base = _r._resolve_key(_base_spec(base_key, row, line_t12), ctx, prior_entries.get(base_key), None)
        facts = _line_facts(ctx.canonical, row)
        source_rows = row.get("source_rows") or (line_t12 or {}).get("source_rows") or []
        if source_rows:
            facts["source_rows"] = list(source_rows)
        base.detail = _r._jsonable(facts)
        entries[base_key] = base.to_dict()

        growth = _r._resolve_key(_growth_spec(growth_key, row), ctx, prior_entries.get(growth_key),
                                 explicit.get(growth_key))
        category, constant, _rate = _house_growth(row)
        growth.detail = {
            "category_name": name,
            "growth_category": category,
            "house_constant": constant,
            "calculation_type": _calc(row),
        }
        entries[growth_key] = growth.to_dict()
    return entries


def overrides_from_patch(patch: Mapping[str, Any], ctx: Any, note: str) -> dict[str, dict[str, Any]]:
    """Analyst growth overrides for every row of a patched opex_table that carries a growth_rate."""
    if "opex_table" not in patch:
        return {}
    rows = [row for row in ctx.canonical.get("opex_table") or [] if isinstance(row, dict)]
    out: dict[str, dict[str, Any]] = {}
    for slug, row in zip(opex_line_slugs(rows), rows):
        rate = _r._num(row.get("growth_rate"))
        if rate is not None and not row.get("growth_phases"):
            out[f"{OPEX_KEY_PREFIX}{slug}.growth"] = {"value": rate, "note": note}
    return out
