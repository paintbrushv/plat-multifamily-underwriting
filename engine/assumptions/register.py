"""Assumption register: the analyst-facing view of every assumption (ADR 0003 §8).

``build_register(canonical)`` turns the canonical ``assumptions`` section into
one JSON-serializable document that every surface renders: the HTML page
(``engine/assumptions/register_html.py``), the Excel "Assumptions" tab
(``engine/assumptions/sheet.py``), the API (``engine.api.handle_assumption_register``)
and the MCP tool. For each assumption it shows:

- what it is (key, human label, group) and its current value and unit;
- the resolver default and where it came from;
- why: the entry's own basis plus the policy rationale for the default's
  source (``engine/assumptions/rationale.py``);
- the candidates the resolver considered;
- the user's edit (override value, note, optional ``edited_by`` /
  ``edited_at``) and whether and by how much it differs from the default;
- guardrail status, review flag, confidence and as-of date.

The view is generic over the entry shape: scalars, vectors, curve objects
and fields it has never seen all render, and a key it does not know falls
into a group by prefix (else "Profile/Other") with a label made from the key.
It is deterministic: the same canonical always gives the same document, and
nothing here reads the clock.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Optional

from engine.assumptions.rationale import (
    FAMILY_ANALYST,
    FAMILY_DEAL,
    FAMILY_ENGINE_FALLBACK,
    FAMILY_HOUSE,
    FAMILY_MARKET,
    rationale_for,
    source_family,
)
from engine.assumptions.sheet import _jsonable, _number, _same, delta as _sheet_delta, sorted_keys

REGISTER_VERSION = "plat.assumptions.register/1"

# ── groups ───────────────────────────────────────────────────────────────

GROUP_REVENUE = "Revenue"
GROUP_VACANCY_CREDIT = "Vacancy & Credit"
GROUP_OPEX = "Opex"
GROUP_CAPITAL = "Capital/Capex"
GROUP_EXIT = "Exit"
GROUP_DEBT_FEES = "Debt/Fees"
GROUP_OTHER = "Profile/Other"
GROUPS = (
    GROUP_REVENUE,
    GROUP_VACANCY_CREDIT,
    GROUP_OPEX,
    GROUP_CAPITAL,
    GROUP_EXIT,
    GROUP_DEBT_FEES,
    GROUP_OTHER,
)

# Exact keys first, then the longest matching prefix, then a token anywhere
# in the key. New keys (e.g. ``capex.<item>.*`` or ``economic_capex_per_unit``)
# land in a group without a code change; anything else is "Profile/Other".
_GROUP_BY_KEY: dict[str, str] = {
    "rent_growth": GROUP_REVENUE,
    "renovation_premium": GROUP_REVENUE,
    "physical_vacancy": GROUP_VACANCY_CREDIT,
    "loss_to_lease": GROUP_VACANCY_CREDIT,
    "concessions": GROUP_VACANCY_CREDIT,
    "collection_loss": GROUP_VACANCY_CREDIT,
    "insurance_per_unit": GROUP_OPEX,
    "r_and_m_per_unit": GROUP_OPEX,
    "r_and_m_payroll_credit": GROUP_OPEX,
    "immediate_capex": GROUP_CAPITAL,
    "recurring_capex_per_unit": GROUP_CAPITAL,
    "recurring_capex_growth": GROUP_CAPITAL,
    "replacement_reserve_per_unit": GROUP_CAPITAL,
    "turn_cost_per_unit": GROUP_CAPITAL,
    "exit_cap_rate": GROUP_EXIT,
    "sale_cost_percent": GROUP_EXIT,
    "purchase_closing_cost_pct": GROUP_DEBT_FEES,
    "loan_closing_costs": GROUP_DEBT_FEES,
    "acquisition_fee_pct": GROUP_DEBT_FEES,
    "asset_management_fee_pct": GROUP_DEBT_FEES,
    "disposition_fee_pct": GROUP_DEBT_FEES,
    "annual_partnership_expenses": GROUP_DEBT_FEES,
    "partnership_closing_costs": GROUP_DEBT_FEES,
}
_GROUP_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("opex.", GROUP_OPEX),
    ("capex.", GROUP_CAPITAL),
    ("reserve", GROUP_CAPITAL),
    ("replacement_reserve", GROUP_CAPITAL),
    ("renovation", GROUP_CAPITAL),
    ("economic_capex", GROUP_CAPITAL),
    ("rent", GROUP_REVENUE),
    ("other_income", GROUP_REVENUE),
    ("vacancy", GROUP_VACANCY_CREDIT),
    ("exit", GROUP_EXIT),
    ("sale_", GROUP_EXIT),
    ("debt", GROUP_DEBT_FEES),
    ("loan", GROUP_DEBT_FEES),
    ("profile", GROUP_OTHER),
)
_GROUP_BY_TOKEN: tuple[tuple[str, str], ...] = (
    ("capex", GROUP_CAPITAL),
    ("reserve", GROUP_CAPITAL),
    ("r_and_m", GROUP_OPEX),
    ("opex", GROUP_OPEX),
    ("insurance", GROUP_OPEX),
    ("vacancy", GROUP_VACANCY_CREDIT),
    ("concession", GROUP_VACANCY_CREDIT),
    ("bad_debt", GROUP_VACANCY_CREDIT),
    ("collection", GROUP_VACANCY_CREDIT),
    ("exit", GROUP_EXIT),
    ("fee", GROUP_DEBT_FEES),
    ("closing", GROUP_DEBT_FEES),
    ("loan", GROUP_DEBT_FEES),
    ("rent", GROUP_REVENUE),
    ("income", GROUP_REVENUE),
)


def group_for(key: str) -> str:
    """The register group of an assumption key (``Profile/Other`` when nothing matches)."""
    if key in _GROUP_BY_KEY:
        return _GROUP_BY_KEY[key]
    for prefix, group in sorted(_GROUP_BY_PREFIX, key=lambda item: -len(item[0])):
        if key.startswith(prefix):
            return group
    for token, group in _GROUP_BY_TOKEN:
        if token in key:
            return group
    return GROUP_OTHER


# ── labels ───────────────────────────────────────────────────────────────

_LABELS: dict[str, str] = {
    "rent_growth": "Market rent growth",
    "collection_loss": "Collection loss (bad debt)",
    "physical_vacancy": "Physical vacancy",
    "loss_to_lease": "Loss to lease",
    "concessions": "Concessions",
    "insurance_per_unit": "Insurance per unit",
    "r_and_m_per_unit": "Repairs & maintenance per unit",
    "r_and_m_payroll_credit": "R&M floor: payroll credit per unit (payroll review)",
    "immediate_capex": "Capex bucket 1: immediate / deferred capex at close",
    "recurring_capex_per_unit": "Capex bucket 2: recurring capex per unit (year 1)",
    "recurring_capex_growth": "Capex bucket 2: recurring capex growth",
    "replacement_reserve_per_unit": "Capex bucket 3: replacement reserve per unit (floor)",
    "turn_cost_per_unit": "Turn cost per unit",
    "renovation_premium": "Renovation rent premium",
    "exit_cap_rate": "Exit cap rate",
    "sale_cost_percent": "Sale cost",
    "acquisition_fee_pct": "Acquisition fee",
    "asset_management_fee_pct": "Asset management fee",
    "disposition_fee_pct": "Disposition fee",
    "annual_partnership_expenses": "Annual partnership expenses",
    "partnership_closing_costs": "Partnership closing costs",
    "purchase_closing_cost_pct": "Purchase closing costs",
    "loan_closing_costs": "Loan closing costs",
}
_OPEX_PART_LABELS = {"base": "base (year 1)", "growth": "growth"}
_WORD_LABELS = {"pct": "%", "r": "R", "and": "&", "m": "M", "yoy": "YoY", "egr": "EGR", "ltl": "LTL",
                "capex": "capex", "per": "per", "usd": "$"}


def _humanize(text: str) -> str:
    words = [w for w in text.replace(".", " ").replace("_", " ").split() if w]
    if not words:
        return text
    out = [_WORD_LABELS.get(w, w) for w in words]
    first = out[0]
    out[0] = first[:1].upper() + first[1:]
    return " ".join(out)


def label_for(key: str, entry: Optional[Mapping[str, Any]] = None) -> str:
    """A human label: a curated one, an opex line's own category name, or one made from the key."""
    if key in _LABELS:
        return _LABELS[key]
    detail = entry.get("detail") if isinstance(entry, Mapping) else None
    parts = key.split(".")
    if len(parts) == 3 and parts[0] in ("opex", "capex"):
        name = detail.get("category_name") if isinstance(detail, Mapping) else None
        name = str(name) if name else _humanize(parts[1])
        return f"{name}: {_OPEX_PART_LABELS.get(parts[2], _humanize(parts[2]))}"
    return _humanize(key)


# ── value display ────────────────────────────────────────────────────────

_UNIT_LABELS: dict[str, str] = {
    "decimal": "%",
    "decimal_yoy": "% a year",
    "decimal_yoy_by_analysis_year": "% a year, by analysis year",
    "decimal_of_rent": "% of rent",
    "decimal_of_units": "% of units",
    "decimal_of_market_rent": "% of market rent",
    "decimal_of_sale_price": "% of sale price",
    "decimal_of_purchase_price": "% of purchase price",
    "decimal_per_year": "% a year",
    "decimal_of_egr": "% of EGR",
    "usd": "$",
    "usd_per_year": "$ a year",
    "usd_per_unit_year": "$ per unit a year",
    "usd_per_unit_month": "$ per unit a month",
    "usd_per_turn": "$ per turn",
}


def unit_label(unit: Any) -> str:
    """A short human label for a unit string (the raw unit when unknown)."""
    if not isinstance(unit, str) or not unit:
        return ""
    if unit in _UNIT_LABELS:
        return _UNIT_LABELS[unit]
    if unit.startswith("decimal"):
        return "%" + (" " + _humanize(unit[len("decimal"):]).lower() if unit != "decimal" else "")
    if unit.startswith("usd"):
        return "$" + (" " + _humanize(unit[len("usd"):]).lower() if unit != "usd" else "")
    return _humanize(unit).lower()


def _unit_kind(unit: Any) -> str:
    unit = str(unit or "")
    if unit.startswith("decimal"):
        return "pct"
    if unit.startswith("usd"):
        return "usd"
    return "num"


def format_number(value: Any, unit: Any) -> str:
    """One number in its unit: ``2.50%``, ``$1,350``, ``$12.50`` or ``24``."""
    number = _number(value)
    if number is None:
        return "—" if value is None else str(value)
    if not math.isfinite(number):
        return str(number)
    kind = _unit_kind(unit)
    if kind == "pct":
        text = f"{number * 100:.2f}"
        if text.endswith("0"):
            text = text[:-1]  # two decimals, trailing zero dropped: 2.5%, 5.75%, 0.0%
        return f"{'0.0' if text in ('-0.0', '0.0') else text}%"
    if kind == "usd":
        sign = "-" if number < 0 else ""
        magnitude = abs(number)
        if magnitude >= 100 or float(magnitude).is_integer():
            return f"{sign}${magnitude:,.0f}"
        return f"{sign}${magnitude:,.2f}"
    if float(number).is_integer():
        return f"{int(number):,}"
    return f"{number:,.4f}".rstrip("0").rstrip(".")


def _period_word(unit: Any, n: int) -> str:
    unit = str(unit or "")
    if "by_analysis_year" in unit or "by_year" in unit:
        return "yr" if n == 1 else "yrs"
    return "value" if n == 1 else "values"


def _series(values: list[Any], unit: Any, *, word: Optional[str] = None, labels: Optional[list[str]] = None) -> str:
    n = len(values)
    if n == 0:
        return "(empty)"
    shown = [format_number(v, unit) for v in values]
    if labels is not None:
        shown = [f"{label} {text}" for label, text in zip(labels, shown)]
    word = word or _period_word(unit, n)
    if n <= 3:
        body = " → ".join(shown)
    else:
        body = f"{shown[0]} → {shown[1]} … {shown[-1]}"
    return f"{body} ({n} {word})" if n > 1 else body


_START_KEYS = ("start", "year1", "initial", "current", "from")
_END_KEYS = ("stabilized", "floor", "target", "end", "long_run", "to")
_MONTH_SUFFIXES = ("_months", "months", "_mo")


def _months_key(value: Mapping[str, Any]) -> Optional[str]:
    for name in value:
        if isinstance(name, str) and name.endswith(_MONTH_SUFFIXES) and _number(value[name]) is not None:
            return name
    return None


def _curve_text(value: Mapping[str, Any], unit: Any) -> Optional[str]:
    """``start 8% → 6.5% over 24 mo`` for a start/end/months curve object."""
    start = next((k for k in _START_KEYS if _number(value.get(k)) is not None), None)
    end = next((k for k in _END_KEYS if _number(value.get(k)) is not None), None)
    if start is None or end is None:
        return None
    months_key = _months_key(value)
    text = f"{'year 1' if start == 'year1' else start} {format_number(value[start], unit)} → " \
           f"{format_number(value[end], unit)}"
    if months_key is not None:
        months = format_number(value[months_key], None)
        text += f" after {months} mo" if start == "year1" else f" over {months} mo"
    for name in value:
        if name in (start, end, months_key):
            continue
        item = value[name]
        if isinstance(item, Mapping) and str(name).startswith("start_by_"):
            text += f" ({len(item)} {str(name)[len('start_by_'):].replace('_', ' ')}s)"
        elif isinstance(item, (Mapping, list, tuple)):
            text += f" (+{_humanize(str(name)).lower()})"
        elif item is not None:
            text += f"; {_humanize(str(name)).lower()} {format_value(item, unit)}"
    return text


def format_value(value: Any, unit: Any = None) -> str:
    """A compact one-line rendering of any assumption value.

    Scalars in their unit; vectors as ``2.5% → 2.6% … 3% (5 yrs)``; curve
    objects as ``start 8% → 6.5% over 24 mo``; a period-keyed series (for
    example quarterly forecasts) as ``2026Q4 2% → 2027Q1 2.2% … (9 values)``;
    any other object as ``name: value; ...``.
    """
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, str):
        return value
    if _number(value) is not None:
        return format_number(value, unit)
    if isinstance(value, (list, tuple)):
        items = list(value)
        if all(_number(v) is not None for v in items):
            return _series(items, unit)
        return "[" + ", ".join(format_value(v, unit) for v in items[:4]) + (" …" if len(items) > 4 else "") + "]"
    if isinstance(value, Mapping):
        if not value:
            return "(empty)"
        curve = _curve_text(value, unit)
        if curve is not None:
            return curve
        keys = list(value)
        if len(keys) > 3 and all(_number(value[k]) is not None for k in keys):
            ordered = sorted(keys, key=str)
            return _series([value[k] for k in ordered], unit, word="values", labels=[str(k) for k in ordered])
        parts = []
        for name in keys[:6]:
            item = value[name]
            if isinstance(item, Mapping):
                parts.append(f"{name}: {{{len(item)} items}}")
            elif isinstance(item, (list, tuple)):
                parts.append(f"{name}: [{len(item)} items]")
            else:
                parts.append(f"{name}: {format_value(item, unit)}")
        return "; ".join(parts) + (" …" if len(keys) > 6 else "")
    return str(value)


# ── deltas ───────────────────────────────────────────────────────────────


def value_delta(value: Any, default: Any) -> Any:
    """``value - default``: a number for scalars, a list for same-length vectors,
    and an object of per-field deltas for curve objects (numeric fields both
    carry). ``None`` when the shapes cannot be compared."""
    out = _sheet_delta(value, default)
    if out is not None:
        return out
    if isinstance(value, Mapping) and isinstance(default, Mapping):
        fields: dict[str, Any] = {}
        for name in sorted(set(value) & set(default), key=str):
            sub = value_delta(value[name], default[name])
            if sub is not None:
                fields[str(name)] = sub
        return fields or None
    return None


def _format_delta(delta: Any, unit: Any) -> Optional[str]:
    if delta is None:
        return None
    if _number(delta) is not None:
        number = float(delta)
        if _unit_kind(unit) == "pct":
            bps = number * 10000
            return f"{bps:+,.0f} bps" if abs(bps) >= 1 or bps == 0 else f"{bps:+,.2f} bps"
        text = format_number(abs(number), unit)
        return ("+" if number >= 0 else "-") + text
    if isinstance(delta, list):
        return "[" + ", ".join(_format_delta(d, unit) or "?" for d in delta) + "]"
    if isinstance(delta, Mapping):
        # Per-field deltas of a curve object: only the fields that changed.
        changed = [
            f"{name} {_format_delta(d, None if 'month' in name else unit)}"
            for name, d in delta.items()
            if not _is_zero(d)
        ]
        return "; ".join(changed) or None
    return None


def _is_zero(delta: Any) -> bool:
    if isinstance(delta, Mapping):
        return all(_is_zero(d) for d in delta.values())
    if isinstance(delta, list):
        return all(_is_zero(d) for d in delta)
    number = _number(delta)
    return number is not None and number == 0


# ── guardrails ───────────────────────────────────────────────────────────

GUARDRAIL_WARNED = "warned"
GUARDRAIL_ENFORCED = "enforced"
GUARDRAIL_WAIVED = "waived"
GUARDRAIL_EXEMPT = "exempt"
GUARDRAIL_OTHER = "flagged"


def _guardrail(record: Any, unit: Any) -> Optional[dict[str, Any]]:
    if record is None:
        return None
    if not isinstance(record, Mapping):
        return {"state": GUARDRAIL_OTHER, "status": str(record), "action": None, "floor": None,
                "floor_display": None, "floor_source": None, "original": None, "original_display": None,
                "basis": None}
    status = record.get("status")
    action = record.get("action")
    action_text = str(action or "")
    status_text = str(status or "")
    if action_text in ("enforced", "floor_applied_over_deal_evidence"):
        state = GUARDRAIL_ENFORCED
    elif action_text == "warned":
        state = GUARDRAIL_WARNED
    elif "waive" in action_text:
        state = GUARDRAIL_WAIVED
    elif status_text.startswith("exempt") or action_text.startswith("exempt"):
        state = GUARDRAIL_EXEMPT
    else:
        state = GUARDRAIL_OTHER
    floor = record.get("floor")
    original = record.get("original")
    return {
        "state": state,
        "status": None if status is None else str(status),
        "action": None if action is None else str(action),
        "floor": _jsonable(floor),
        "floor_display": None if floor is None else format_value(floor, unit),
        "floor_source": record.get("floor_source"),
        "original": _jsonable(original),
        "original_display": None if original is None else format_value(original, unit),
        "basis": record.get("basis"),
    }


# Human labels for the flag codes a register row carries (``row["flags"]``).
FLAG_LABELS: dict[str, str] = {
    "edited": "Edited by user",
    "pinned": "Pinned by pricing",
    "review": "Needs review",
    f"guardrail_{GUARDRAIL_WARNED}": "Below house floor (warned)",
    f"guardrail_{GUARDRAIL_ENFORCED}": "Raised to house floor",
    f"guardrail_{GUARDRAIL_WAIVED}": "House floor waived",
    f"guardrail_{GUARDRAIL_EXEMPT}": "Exempt from house floor",
    f"guardrail_{GUARDRAIL_OTHER}": "Guardrail",
    "low_confidence": "Low confidence",
    "no_rationale": "No rationale",
}


def flag_labels(row: Mapping[str, Any]) -> list[str]:
    """The human labels of a register row's flags, in flag order."""
    return [FLAG_LABELS.get(flag, flag) for flag in row.get("flags") or ()]


def guardrail_detail(row: Mapping[str, Any]) -> Optional[str]:
    """``floor 8.0% (house:vintage_1970s) original 6.5%`` for a row with a guardrail record."""
    guardrail = row.get("guardrail")
    if not guardrail:
        return None
    detail = []
    if guardrail.get("floor_display") is not None:
        detail.append(f"floor {guardrail['floor_display']}")
    if guardrail.get("floor_source"):
        detail.append(f"({guardrail['floor_source']})")
    if guardrail.get("original_display") is not None:
        detail.append(f"original {guardrail['original_display']}")
    if not detail and guardrail.get("status"):
        detail.append(str(guardrail["status"]))
    return " ".join(detail) or None


# ── one row ──────────────────────────────────────────────────────────────

# Entry fields the register shows in a dedicated place; every other field is
# kept in ``extra`` so nothing an entry carries is dropped from the view.
_SHOWN_FIELDS = frozenset({
    "value", "unit", "source", "basis", "as_of", "confidence", "candidates", "default_value",
    "override", "review_required", "guardrail", "cohort_overrides",
})


def _default_provenance(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Where the default came from: the entry itself, or (for an overridden
    entry) the default provenance the resolver keeps on its analyst candidate."""
    for candidate in entry.get("candidates") or []:
        if isinstance(candidate, Mapping) and isinstance(candidate.get("default"), Mapping):
            default = candidate["default"]
            return {
                "source": default.get("source"),
                "basis": default.get("basis"),
                "as_of": default.get("as_of"),
                "confidence": default.get("confidence"),
            }
    return {
        "source": entry.get("source"),
        "basis": entry.get("basis"),
        "as_of": entry.get("as_of"),
        "confidence": entry.get("confidence"),
    }


def _candidates(entry: Mapping[str, Any], unit: Any, default_source: Any) -> list[dict[str, Any]]:
    out = []
    for candidate in entry.get("candidates") or []:
        if not isinstance(candidate, Mapping):
            continue
        source = candidate.get("source")
        value = _jsonable(candidate.get("value"))
        cand_unit = candidate.get("unit") if candidate.get("unit") else unit
        out.append({
            "source": source,
            "source_family": source_family(source),
            "value": value,
            "display": format_value(value, cand_unit),
            "basis": candidate.get("basis"),
            "as_of": candidate.get("as_of"),
            "confidence": candidate.get("confidence"),
            "is_default_source": source == default_source,
        })
    return out


def register_row(key: str, entry: Any, *, include_candidates: bool = True) -> dict[str, Any]:
    """The register row of one assumption entry. Works for any entry shape."""
    entry = entry if isinstance(entry, Mapping) else {"value": entry}
    unit = entry.get("unit")
    value = _jsonable(entry.get("value"))
    default_value = _jsonable(entry.get("default_value"))
    source = entry.get("source")
    family = source_family(source)
    default = _default_provenance(entry)
    default_source = default["source"]
    override = entry.get("override") if isinstance(entry.get("override"), Mapping) else None

    user_edit = None
    pinned = None
    if override is not None:
        override_value = _jsonable(override.get("value"))
        differs = not _same(override_value, default_value)
        delta = value_delta(override_value, default_value) if differs else None
        if family == FAMILY_ANALYST:
            user_edit = {
                "value": override_value,
                "display": format_value(override_value, unit),
                "note": override.get("note"),
                "edited_by": override.get("edited_by"),
                "edited_at": override.get("edited_at"),
                "cohort_overrides": _jsonable(override.get("cohort_overrides")),
                "differs_from_default": differs,
                "delta": delta,
                "delta_display": _format_delta(delta, unit) if differs else None,
            }
        else:
            # A value a pricing rule pinned (guardrail floor, backsolve policy):
            # recorded as an override but not an analyst edit.
            pinned = {
                "value": override_value,
                "display": format_value(override_value, unit),
                "note": override.get("note"),
                "source": source,
                "differs_from_default": differs,
                "delta": delta,
                "delta_display": _format_delta(delta, unit) if differs else None,
            }

    guardrail = _guardrail(entry.get("guardrail"), unit)
    review = bool(entry.get("review_required", False))
    confidence = entry.get("confidence")
    why = rationale_for(default_source)
    flags: list[str] = []
    if user_edit is not None:
        flags.append("edited")
    if pinned is not None:
        flags.append("pinned")
    if review:
        flags.append("review")
    if guardrail is not None:
        flags.append(f"guardrail_{guardrail['state']}")
    if confidence == "low":
        flags.append("low_confidence")
    if not why["known"]:
        flags.append("no_rationale")

    extra = {
        str(name): _jsonable(entry[name])
        for name in sorted(entry, key=str)
        if name not in _SHOWN_FIELDS
    }
    row = {
        "key": key,
        "label": label_for(key, entry),
        "group": group_for(key),
        "value": value,
        "value_display": format_value(value, unit),
        "unit": unit,
        "unit_label": unit_label(unit),
        "source": source,
        "source_family": family,
        "default": {
            "value": default_value,
            "display": format_value(default_value, unit),
            "source": default_source,
            "source_family": source_family(default_source),
            "basis": default["basis"],
            "as_of": default["as_of"],
            "confidence": default["confidence"],
        },
        "why": {
            "basis": entry.get("basis"),
            "default_basis": default["basis"],
            "rationale": why["text"],
            "rationale_source": default_source,
            "rationale_known": why["known"],
        },
        "user_edit": user_edit,
        "pinned": pinned,
        "guardrail": guardrail,
        "review_required": review,
        "confidence": confidence,
        "as_of": entry.get("as_of"),
        "flags": flags,
        "extra": extra or None,
    }
    if include_candidates:
        row["candidates"] = _candidates(entry, unit, default_source)
    else:
        row["candidate_count"] = len([c for c in entry.get("candidates") or [] if isinstance(c, Mapping)])
    return row


# ── capital plan: the three capex buckets (ADR 0003 §3) ──────────────────

CAPITAL_PLAN_KEYS = (
    ("immediate_capex", 1, "Immediate / deferred capex at close",
     "One-time, funded with equity at close (or a lender holdback); in the equity basis, never in NOI."),
    ("recurring_capex_per_unit", 2, "Recurring capex by vintage",
     "Annual $/unit below NOI, grown every year."),
    ("replacement_reserve_per_unit", 3, "Lender replacement reserve",
     "Annual $/unit below NOI; a floor, not an addition."),
)
CAPITAL_PLAN_RULE = "The charge below NOI each year is max(recurring capex, replacement reserve), never both."


def capital_plan_summary(entries: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """The three capex buckets side by side, with the one that binds in year 1.

    ``None`` when the canonical resolved none of the bucket keys.
    """
    if not any(key in entries for key, *_ in CAPITAL_PLAN_KEYS):
        return None
    buckets = []
    for key, number, label, note in CAPITAL_PLAN_KEYS:
        entry = entries.get(key) if isinstance(entries.get(key), Mapping) else None
        value = _jsonable(entry.get("value")) if entry else None
        unit = entry.get("unit") if entry else None
        buckets.append({
            "bucket": number,
            "key": key,
            "label": label,
            "note": note,
            "value": value,
            "value_display": format_value(value, unit) if entry else "not modeled",
            "source": entry.get("source") if entry else None,
            "review_required": bool(entry.get("review_required")) if entry else False,
        })
    growth = entries.get("recurring_capex_growth")
    recurring = _number(buckets[1]["value"])
    reserve = _number(buckets[2]["value"])
    binding = None
    if recurring is not None or reserve is not None:
        recurring, reserve = recurring or 0.0, reserve or 0.0
        binding = "recurring_capex_per_unit" if recurring > reserve else "replacement_reserve_per_unit"
    return {
        "rule": CAPITAL_PLAN_RULE,
        "buckets": buckets,
        "recurring_capex_growth": _jsonable(growth.get("value")) if isinstance(growth, Mapping) else None,
        "binding_year_1": binding,
        "charge_year_1_per_unit": max(recurring, reserve) if binding else None,
    }


# ── the register ─────────────────────────────────────────────────────────


def _entries(canonical: Any) -> Mapping[str, Any]:
    section = canonical.get("assumptions") if isinstance(canonical, Mapping) else None
    entries = section.get("entries") if isinstance(section, Mapping) else None
    return entries if isinstance(entries, Mapping) else {}


def ordered_keys(keys: Iterable[str]) -> list[str]:
    """Group order (``GROUPS``), then the sheet's key order (registry order, then A→Z)."""
    keys = sorted_keys(keys)
    rank = {group: index for index, group in enumerate(GROUPS)}
    position = {key: index for index, key in enumerate(keys)}
    return sorted(keys, key=lambda key: (rank.get(group_for(key), len(rank)), position[key]))


# Filter vocabulary shared by the HTML chips, the API and the MCP tool.
FILTERS = ("all", "edited", "review", "house", "deal", "market", "guardrail")


def row_filters(row: Mapping[str, Any]) -> list[str]:
    """The filters a register row matches (``all`` always)."""
    out = ["all"]
    if row.get("user_edit") is not None:
        out.append("edited")
    if row.get("review_required"):
        out.append("review")
    family = row.get("source_family")
    if family in (FAMILY_HOUSE, FAMILY_ENGINE_FALLBACK):
        out.append("house")
    elif family == FAMILY_DEAL:
        out.append("deal")
    elif family == FAMILY_MARKET:
        out.append("market")
    if row.get("guardrail") is not None:
        out.append("guardrail")
    return out


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def count(predicate) -> int:
        return sum(1 for row in rows if predicate(row))

    def guardrail_state(state: str):
        return lambda row: (row.get("guardrail") or {}).get("state") == state

    by_group = {group: count(lambda row, g=group: row["group"] == g) for group in GROUPS}
    return {
        "total": len(rows),
        "from_deal_evidence": count(lambda row: row["source_family"] == FAMILY_DEAL),
        "from_market": count(lambda row: row["source_family"] == FAMILY_MARKET),
        "house_defaults": count(lambda row: row["source_family"] == FAMILY_HOUSE),
        "engine_fallbacks": count(lambda row: row["source_family"] == FAMILY_ENGINE_FALLBACK),
        "analyst": count(lambda row: row["source_family"] == FAMILY_ANALYST),
        "edited_by_user": count(lambda row: row.get("user_edit") is not None),
        "edits_differing_from_default": count(
            lambda row: bool((row.get("user_edit") or {}).get("differs_from_default"))
        ),
        "pinned_by_pricing": count(lambda row: row.get("pinned") is not None),
        "needing_review": count(lambda row: row["review_required"]),
        "guardrail_warnings": count(guardrail_state(GUARDRAIL_WARNED)),
        "guardrail_enforced": count(guardrail_state(GUARDRAIL_ENFORCED)),
        "guardrail_waived": count(guardrail_state(GUARDRAIL_WAIVED)),
        "guardrail_exempt": count(guardrail_state(GUARDRAIL_EXEMPT)),
        "missing_rationale": count(lambda row: not row["why"]["rationale_known"]),
        "by_group": {group: n for group, n in by_group.items() if n},
    }


def build_register(
    canonical: Mapping[str, Any],
    *,
    include_candidates: bool = True,
    filter: str = "all",
) -> dict[str, Any]:
    """The assumption register of a canonical: a deterministic, JSON-serializable dict.

    ``{register_version, resolver_version, mode, deal_id, deal_profile,
    summary, capital_plan, groups, filter, rows}``. ``capital_plan`` shows the
    three capex buckets side by side (``capital_plan_summary``). ``rows`` follow ``ordered_keys``;
    ``groups`` lists each non-empty group with its keys in that order.
    ``summary`` always counts every entry; ``filter`` (one of ``FILTERS``)
    only narrows ``rows`` and ``groups``. ``include_candidates=False`` drops
    each row's candidate list (keeping ``candidate_count``) for a bounded
    response.
    """
    if filter not in FILTERS:
        raise ValueError(f"filter must be one of {FILTERS}")
    canonical = canonical if isinstance(canonical, Mapping) else {}
    section = canonical.get("assumptions") if isinstance(canonical.get("assumptions"), Mapping) else {}
    metadata = canonical.get("metadata") if isinstance(canonical.get("metadata"), Mapping) else {}
    entries = _entries(canonical)
    all_rows = [register_row(key, entries[key], include_candidates=include_candidates)
                for key in ordered_keys(entries)]
    for row in all_rows:
        row["filters"] = row_filters(row)
    rows = [row for row in all_rows if filter in row["filters"]]
    groups = []
    for group in GROUPS:
        keys = [row["key"] for row in rows if row["group"] == group]
        if keys:
            groups.append({"group": group, "keys": keys})
    profile = canonical.get("deal_profile")
    return {
        "register_version": REGISTER_VERSION,
        "resolver_version": section.get("resolver_version"),
        "mode": section.get("mode"),
        "deal_id": metadata.get("deal_id"),
        "deal_profile": _jsonable(profile) if isinstance(profile, Mapping) else None,
        "summary": _summary(all_rows),
        "capital_plan": capital_plan_summary(entries),
        "filter": filter,
        "groups": groups,
        "rows": rows,
    }
