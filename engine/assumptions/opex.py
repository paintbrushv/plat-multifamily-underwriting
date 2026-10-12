"""Opex treatment for the assumption layer (ADR 0003 §6, slice 5).

Each opex_table row gets two entries:

- ``opex.<slug>.base``: the line in annual dollars (``detail`` carries the
  calculation_type, base_value and per-unit amount). Percent-of-revenue rows
  show the percent; they are never converted to dollars. Ingested lines cite
  ``deal:t12`` with the raw T12 source rows. An override takes annual dollars
  (the percent for a percent-of-revenue line) and rewrites the row's
  ``base_value`` in its own calculation_type. An override of ``0`` (or
  ``{"value": 0, "note": ...}``) suppresses the line: it stays in the table at
  $0, so house defaults never re-add it. ``null`` clears the override and
  restores the T12 / house amount.
- ``opex.<slug>.growth``: the annual growth the engine applies. An analyst
  rate on the row (or an explicit override) wins; otherwise the house
  per-category table in ``engine/underwriting_policy.py``. Real estate taxes
  defer to ``engine/property_tax.py``.

The resolver writes the house growth into every row (ingested or
hand-authored) that has no ``growth_rate``. For an ingested deal it also adds
Insurance, Repairs & Maintenance and a Property Management Fee from house
defaults when the T12 carries none of them, unless the analyst deleted that
line after the last resolution (it is then kept at $0 as an analyst
suppression), and it enters the T12 management fee as ``percent_egr`` = T12
fee / T12 EGI (house percent when the EGI is unknown) so the fee moves with
modeled revenue.

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
    in_r_and_m_floor_basis,
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
    """The per-line opex keys (``opex.<slug>.base`` / ``.growth``) among ``keys``."""
    return {key for key in keys if is_opex_key(key)}


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
    if category == REAL_ESTATE_TAXES:
        return (
            f"{name}: engine/property_tax.py resets the year-1 tax to purchase price x assessment ratio x "
            f"millage and keeps this growth_rate, which the engine applies from analysis year 2 on the "
            f"reassessed amount ({constant} = {float(rate):.2%} a year, engine/underwriting_policy.py). "
            "Applied once, never stacked on the reassessment. A house assumption to be reviewed."
        )
    mapped = f"maps to the house category {category!r}" if category else "is not in the house table"
    return (
        f"{name} {mapped}: {constant} = {float(rate):.2%} a year (engine/underwriting_policy.py). "
        "A house assumption to be reviewed, not market data."
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
        lines = ((provenance or {}).get("_line_amounts") or {}).get(name)
        if isinstance(lines, list) and lines:
            # Each statement line with its annual amount (the R&M payroll review
            # reads the labeled maintenance payroll from these).
            facts["source_lines"] = [dict(line) for line in lines]
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
        # Turnover / make-ready lines are R&M on the floor basis: a T12 that books
        # turns separately already carries R&M, so no house R&M line is added.
        return any(in_r_and_m_floor_basis(name) for name in names)
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
        deferred = bool(ctx.profile.deferred_maintenance)
        per_unit = house.default_r_and_m_per_unit(ctx.year_built, strategy=ctx.strategy, deferred=deferred)
        if per_unit is None:
            return None
        source = house_vintage_source(ctx.bucket)
        basis = (
            f"The T12 has no repairs & maintenance line; year built {ctx.year_built} maps to the "
            f"{ctx.bucket} vintage bucket: house R&M ${float(per_unit):,.2f}/unit (VINTAGE_FRAMEWORK_2026, "
            f"{ctx.strategy} strategy"
            + ("; deferred maintenance flagged: the high end of the band" if deferred else "")
            + ")."
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


def _rows(ctx: Any) -> list[dict[str, Any]]:
    return [row for row in ctx.canonical.get("opex_table") or [] if isinstance(row, dict)]


def _key_of(ctx: Any, row: Mapping[str, Any], kind: str = "base") -> Optional[str]:
    rows = _rows(ctx)
    for slug, candidate in zip(opex_line_slugs(rows), rows):
        if candidate is row:
            return f"{OPEX_KEY_PREFIX}{slug}.{kind}"
    return None


def _prior_for(ctx: Any, row: Mapping[str, Any], kind: str = "base") -> Optional[Mapping[str, Any]]:
    key = _key_of(ctx, row, kind)
    prior = (getattr(ctx, "prior_entries", None) or {}).get(key) if key else None
    return prior if isinstance(prior, Mapping) else None


# ── management fee as a percent of EGR ───────────────────────────────────


def _is_management_fee(name: Any) -> bool:
    return growth_category(name) == "Property Management Fee"


def management_fee_percent(ctx: Any, line: Mapping[str, Any]) -> tuple[float, str, str]:
    """``(percent of EGR, source, basis)`` for a T12 management fee in dollars."""
    annual = float(line["annual_amount"])
    egi = _r.t12_egi(ctx.canonical)
    if egi is not None and egi > 0:
        pct = round(annual / egi, 6)
        return pct, SOURCE_DEAL_T12, (
            f"T12 management fee ${annual:,.2f} / T12 EGI (total income) ${egi:,.2f} = {pct:.2%} of EGR, "
            "entered as percent_egr so the fee moves with modeled revenue."
        )
    pct = float(house.DEFAULT_MANAGEMENT_FEE_PCT_EGR)
    return pct, SOURCE_HOUSE_MANAGEMENT_FEE, (
        f"T12 management fee ${annual:,.2f}, but the T12 EGI is unknown: house fee {pct:.2%} of EGR "
        "(DEFAULT_MANAGEMENT_FEE_PCT_EGR), entered as percent_egr."
    )


def _convert_management_fee(ctx: Any) -> None:
    """Enter the ingested T12 management fee as percent_egr (once, and never over an analyst edit)."""
    t12 = _t12_lines(ctx)
    for row in _rows(ctx):
        name = row.get("category_name")
        line = t12.get(str(name))
        if not _is_management_fee(name) or _calc(row) in PERCENT_OF_REVENUE or not line:
            continue
        if line.get("calculation_type") in PERCENT_OF_REVENUE or line.get("annual_amount") is None:
            continue
        if not _r._same(_line_facts(ctx.canonical, row)["annual_amount"], line["annual_amount"]):
            continue  # the analyst changed the dollar amount
        prior = _prior_for(ctx, row)
        if prior is not None and (prior.get("override") or not _r._same(prior.get("value"), line["annual_amount"])):
            continue  # the analyst entered (or kept) this line in dollars
        growth_prior = _prior_for(ctx, row, "growth")
        row["calculation_type"] = "percent_egr"
        row["base_value"] = management_fee_percent(ctx, line)[0]
        if growth_prior is None or not growth_prior.get("override"):
            row.pop("growth_rate", None)  # a percent of revenue gets the house 0% growth


# ── writing a base amount back to the row ───────────────────────────────


def set_line_amount(canonical: Mapping[str, Any], row: dict[str, Any], value: Any) -> bool:
    """Write annual dollars (a percent for percent-of-revenue rows) into ``row["base_value"]``,
    converted to the row's calculation_type."""
    number = _r._num(value)
    if number is None:
        return False
    if _calc(row) in PERCENT_OF_REVENUE:
        row["base_value"] = number
        return True
    try:
        per_base = annual_opex_amount(dict(canonical), {**row, "base_value": 1})
    except ValueError:
        return False
    if per_base == 0:
        return False
    row["base_value"] = float(Decimal(str(number)) / per_base)
    return True


# ── missing core lines, suppressions and opex overrides ─────────────────

DELETED_NOTE = (
    "deleted from the opex_table after the last resolution; kept at $0 so the house default is not re-added"
)
SUPPRESSED_BASIS = (
    "Suppressed by the analyst (override 0): the line stays in the opex_table at $0 so house defaults "
    "never re-add it; clear the override (null) to restore it."
)
_DERIVED_NOTE_PREFIX = "set through opex."


def _deleted_lines(ctx: Any) -> list[dict[str, Any]]:
    """Lines the last resolution had (``opex.<slug>.base`` detail) that no row carries now."""
    names = {str(row.get("category_name", "")) for row in _rows(ctx)}
    out = []
    for key, entry in (getattr(ctx, "prior_entries", None) or {}).items():
        match = _OPEX_KEY.match(key) if isinstance(key, str) else None
        detail = entry.get("detail") if isinstance(entry, Mapping) else None
        if not match or match.group(2) != "base" or not isinstance(detail, Mapping):
            continue
        name = str(detail.get("category_name") or "")
        if name and name not in names:
            out.append({"category_name": name, "calculation_type": detail.get("calculation_type")})
    return out


def _apply_base_overrides(ctx: Any, explicit: Mapping[str, Any]) -> list[tuple[dict[str, Any], str]]:
    """Write explicit ``opex.<slug>.base`` overrides (and clears) into their rows."""
    rows = _rows(ctx)
    keyed = dict(zip(opex_line_slugs(rows), rows))
    t12 = _t12_lines(ctx)
    touched: list[tuple[dict[str, Any], str]] = []
    for key, override in explicit.items():
        match = _OPEX_KEY.match(key) if isinstance(key, str) else None
        row = keyed.get(match.group(1)) if match and match.group(2) == "base" else None
        if row is None:
            continue  # growth keys; unknown lines are rejected by resolve_opex_entries
        before = _line_value(_line_facts(ctx.canonical, row))
        if _r.is_clear(override):
            spec = _base_spec(key, row, t12.get(str(row.get("category_name", ""))), _prior_for(ctx, row))
            value, kind = spec.default(ctx, _r.PRESENT).value, "clear"
        else:
            value, kind = override["value"], "override"
        if value is not None and set_line_amount(ctx.canonical, row, value):
            if not _r._same(before, _line_value(_line_facts(ctx.canonical, row))):
                touched.append((row, kind))
    return touched


def _derived_v1_overrides(
    ctx: Any, touched: list[tuple[dict[str, Any], str]], explicit: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """insurance_per_unit / r_and_m_per_unit follow an opex line the resolver rewrote."""
    out: dict[str, dict[str, Any]] = {}
    insurance = matching_opex_rows(ctx.canonical, "insurance")
    keys = (
        ("insurance_per_unit", _r._read_insurance,
         lambda row: bool(insurance) and row is insurance[0]),
        ("r_and_m_per_unit", _r._read_r_and_m,
         lambda row: in_r_and_m_floor_basis(str(row.get("category_name", "")))),
    )
    for key, read, covers in keys:
        kinds = [(row, kind) for row, kind in touched if covers(row)]
        if not kinds or key in explicit:
            continue
        if any(kind != "clear" for _row, kind in kinds):
            row, kind = next((r, k) for r, k in kinds if k != "clear")
            note = DELETED_NOTE if kind == "deleted" else f"{_DERIVED_NOTE_PREFIX}{_key_of(ctx, row)[5:]}"
            value = read(ctx).value
            if value is not None:
                out[key] = {"value": value, "note": note}
            continue
        prior_note = ((ctx.prior_entries.get(key) or {}).get("override") or {}).get("note")
        if prior_note is None or prior_note in (_r._NOTE_EDITED, _r._NOTE_PRESENT, DELETED_NOTE) or str(
            prior_note
        ).startswith(_DERIVED_NOTE_PREFIX):
            out[key] = {_r.CLEAR: True, "note": "opex line restored"}
    return out


def prepare_opex_table(ctx: Any, explicit: Optional[Mapping[str, Any]] = None) -> dict[str, dict[str, Any]]:
    """Hook 1 (before the v1 keys): opex overrides, the management fee and missing core lines.

    - Explicit ``opex.<slug>.base`` overrides (and ``null`` clears) are written
      into their rows first, so the v1 insurance / R&M keys read them.
    - Ingested deals: the T12 lines are snapshotted, the T12 management fee is
      entered as percent_egr, and Insurance, Repairs & Maintenance and a
      Property Management Fee are added from house defaults when no line
      covers the category under any matching name. A core line the analyst
      deleted after the last resolution is not replaced by the house default:
      it is kept at $0 as an analyst suppression. Real estate taxes are never
      added (the property-tax module owns them).

    Returns the overrides these row changes imply (the suppression of a
    deleted line, and the insurance / R&M per-unit keys an opex base override
    moved); the caller's overrides win over them.
    """
    explicit = explicit or {}
    extra: dict[str, dict[str, Any]] = {}
    if ctx.ingested and ctx.units:
        _t12_lines(ctx)
        _convert_management_fee(ctx)
    touched = _apply_base_overrides(ctx, explicit)
    if ctx.ingested and ctx.units:
        table = ctx.canonical.setdefault("opex_table", [])
        deleted = _deleted_lines(ctx)
        for core in (CORE_INSURANCE, CORE_REPAIRS_MAINTENANCE, CORE_MANAGEMENT_FEE):
            if _has_core(ctx.canonical, core):
                continue
            gone = next(
                (d for d in deleted if _has_core({"opex_table": [{"category_name": d["category_name"]}]}, core)),
                None,
            )
            if gone is not None:
                row = {"category_name": gone["category_name"],
                       "calculation_type": gone["calculation_type"] or "fixed_annual",
                       "base_value": 0.0, "recoverable_flag": False}
                table.append(row)
                touched.append((row, "deleted"))
                extra[_key_of(ctx, row)] = {"value": 0.0, "note": DELETED_NOTE}
                continue
            fill = _house_core(ctx, core)
            if fill is not None:
                row = dict(fill["row"])
                table.append(row)
                ctx.opex_house_rows.add(id(row))
    extra.update(_derived_v1_overrides(ctx, touched, explicit))
    return extra


def _house_fill_for(ctx: Any, row: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """The house fill a row came from, if it is a line prepare_opex_table added.

    A row is a house fill when this resolution added it, when the last
    resolution recorded it from house defaults and it is unchanged, or when it
    carries a core category the T12 never had.
    """
    if not ctx.ingested:
        return None
    for core, category in _CORE_CATEGORY.items():
        if row.get("category_name") != category:
            continue
        fill = _house_core(ctx, core)
        if fill is None:
            return None
        if id(row) in ctx.opex_house_rows or _missing_from_t12(ctx, core):
            return fill
        prior = _prior_for(ctx, row)
        if (
            prior is not None
            and not prior.get("override")
            and str(prior.get("source", "")) == fill["source"]
            and _calc(row) == fill["row"]["calculation_type"]
            and _r._same(row.get("base_value"), fill["row"]["base_value"])
        ):
            return fill
    return None


def filled_core_candidate(ctx: Any, evidence_field: str) -> Optional[dict[str, Any]]:
    """Hook 3: the house per-unit candidate for insurance_per_unit / r_and_m_per_unit
    when that core line was added from house defaults."""
    core = _CORE_BY_EVIDENCE_FIELD.get(evidence_field)
    if core is None or not ctx.ingested:
        return None
    rows = [row for row in _rows(ctx) if row.get("category_name") == _CORE_CATEGORY[core]]
    fill = next((f for f in (_house_fill_for(ctx, row) for row in rows) if f is not None), None)
    if fill is None:
        return None
    return _r._candidate(fill["source"], fill["per_unit"], fill["basis"])


# ── per-line entries ─────────────────────────────────────────────────────


def _no_adr_note(_source: str) -> Optional[int]:
    return None


def _base_spec(
    key: str, row: dict[str, Any], t12: Optional[Mapping[str, Any]], prior: Optional[Mapping[str, Any]] = None,
) -> "_r._Spec":
    name = row.get("category_name")

    def read(ctx: Any) -> "_r._Current":
        return _r._Current(_r.PRESENT, _line_value(_line_facts(ctx.canonical, row)))

    def write(ctx: Any, value: Any, _cohorts: Optional[dict[str, float]]) -> bool:
        if value is None:
            return True  # no default: the canonical's own amount stands
        return set_line_amount(ctx.canonical, row, value)

    def t12_candidate(ctx: Any) -> Optional[dict[str, Any]]:
        if t12 is None:
            return None
        rows = t12.get("source_rows") or []
        cited = f"; source rows: {'; '.join(rows)}." if rows else "."
        if (_calc(row) in PERCENT_OF_REVENUE and t12.get("calculation_type") not in PERCENT_OF_REVENUE
                and _is_management_fee(name) and t12.get("annual_amount") is not None):
            pct, source, basis = management_fee_percent(ctx, t12)
            return _r._candidate(
                source, pct, f"T12 line {name!r}: {basis}" + (f" Source rows: {'; '.join(rows)}." if rows else ""),
                calculation_type="percent_egr", t12_annual_amount=t12.get("annual_amount"),
                source_rows=list(rows) or None,
            )
        value = t12.get("percent_of_revenue") if t12.get("calculation_type") in PERCENT_OF_REVENUE \
            else t12.get("annual_amount")
        return _r._candidate(
            SOURCE_DEAL_T12,
            value,
            f"T12 line {name!r} ({t12.get('calculation_type')}): {_describe(t12)}{cited}",
            calculation_type=t12.get("calculation_type"),
            per_unit_annual=t12.get("per_unit_annual"),
            source_rows=list(rows) or None,
        )

    def default(ctx: Any, _state: str) -> "_r._Choice":
        fill = _house_fill_for(ctx, row)
        if fill is not None:
            return _r._Choice(fill["source"], fill["value"], fill["basis"], confidence="medium")
        evidence = t12_candidate(ctx)
        if evidence is not None:
            return _r._choice_from(evidence, ctx)
        return _r._fallback(None, f"No T12 line or house default for {name!r}; the value is the canonical's own.")

    def gather(ctx: Any) -> list[dict[str, Any]]:
        out = [c for c in (t12_candidate(ctx),) if c is not None]
        fill = _house_fill_for(ctx, row)
        if fill is not None:
            out.append(_r._candidate(fill["source"], fill["value"], fill["basis"],
                                     calculation_type=fill["row"]["calculation_type"],
                                     per_unit_annual=fill.get("per_unit")))
        if not out:
            out.append(_r._candidate(SOURCE_ENGINE_FALLBACK, None, "No T12 or house evidence for this line."))
        return out

    return _r._Spec(key=key, unit=_line_unit(row), read=read, write=write,
                    default=default, gather=gather, adr_rank=_no_adr_note)


def _growth_spec(key: str, row: dict[str, Any]) -> "_r._Spec":
    category, constant, rate = _house_growth(row)

    def read(ctx: Any) -> "_r._Current":
        if row.get("growth_phases"):
            return _r._Current(_r.PRESENT, deepcopy(row["growth_phases"]))
        if row.get("growth_rate") is not None:
            return _r._Current(_r.PRESENT, _r._num(row["growth_rate"]))
        if rate is None:
            return _r._Current(_r.ABSENT)
        # No growth_rate (ingested or hand-authored): the house rate is written.
        return _r._Current(_r.PLACEHOLDER)

    def write(_ctx: Any, value: Any, _cohorts: Optional[dict[str, float]]) -> bool:
        number = _r._num(value)
        if number is None:
            return False
        row["growth_rate"] = number
        row.pop("growth_phases", None)
        return True

    def house_candidate() -> dict[str, Any]:
        return _r._candidate(SOURCE_HOUSE_OPEX_GROWTH, float(rate), _growth_basis(row, category, constant, rate),
                             constant=constant)

    def default(ctx: Any, state: str) -> "_r._Choice":
        if rate is None:
            return _r._fallback(0.0, f"{row.get('category_name')} has no house growth rate: the engine applies none.")
        candidate = house_candidate()
        return _r._Choice(candidate["source"], candidate["value"], candidate["basis"], confidence="medium")

    def gather(_ctx: Any) -> list[dict[str, Any]]:
        out = [house_candidate()] if rate is not None else []
        out.append(_r._candidate(SOURCE_ENGINE_FALLBACK, 0.0, "No growth_rate on the row: the engine applies none."))
        return out

    return _r._Spec(key=key, unit=_GROWTH_UNIT, read=read, write=write, default=default, gather=gather,
                    adr_rank=_no_adr_note)


def resolve_opex_entries(
    ctx: Any,
    prior_entries: Mapping[str, Any],
    explicit: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    """Hook 2 (after the v1 keys): ``opex.<slug>.base`` and ``opex.<slug>.growth`` per row."""
    rows = _rows(ctx)
    slugs = opex_line_slugs(rows)
    known = {f"{OPEX_KEY_PREFIX}{slug}.{kind}" for slug in slugs for kind in ("base", "growth")}
    unknown = sorted(key for key in explicit if is_opex_key(key) and key not in known)
    if unknown:
        raise _r.AssumptionOverrideError(
            f"unknown opex lines {unknown}; this deal has {sorted(known)}"
        )
    t12 = _t12_lines(ctx)
    egi = _r.t12_egi(ctx.canonical)
    entries: dict[str, Any] = {}
    for slug, row in zip(slugs, rows):
        name = str(row.get("category_name", ""))
        line_t12 = t12.get(name)
        base_key = f"{OPEX_KEY_PREFIX}{slug}.base"
        growth_key = f"{OPEX_KEY_PREFIX}{slug}.growth"

        prior_base = prior_entries.get(base_key)
        base = _r._resolve_key(_base_spec(base_key, row, line_t12, prior_base), ctx, prior_base,
                               explicit.get(base_key))
        if base.override is not None and _r._num(base.override.get("value")) == 0:
            base.basis = f"{base.basis} {SUPPRESSED_BASIS}"
        facts = _line_facts(ctx.canonical, row)
        source_rows = row.get("source_rows") or (line_t12 or {}).get("source_rows") or []
        if source_rows:
            facts["source_rows"] = list(source_rows)
        if (line_t12 and _calc(row) in PERCENT_OF_REVENUE
                and line_t12.get("calculation_type") not in PERCENT_OF_REVENUE
                and line_t12.get("annual_amount") is not None):
            # A T12 dollar line entered as a percent of revenue (the management fee).
            facts["t12_annual_amount"] = line_t12["annual_amount"]
            facts["t12_egi"] = egi
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
