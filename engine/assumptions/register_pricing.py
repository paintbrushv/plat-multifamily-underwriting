"""The pricing summary header of the assumption register (UPL-97).

``pricing_header(summary, canonical=..., results=...)`` turns a backsolve
summary (``backsolve_summary.json``; the ``summary`` of
``engine.backsolve.backsolve_price``) into one JSON-serializable dict that
``engine/assumptions/register_html.py`` renders above the register table:

- ``status`` / ``status_label`` / ``solved``, and ``notes`` explaining a
  status that is not a converged boundary (infeasible, search ceiling,
  iteration limit);
- ``metrics``: target CoC and its basis, achieved CoC, solved price and
  $/unit, loan amount and sizing basis, equity, going-in and exit cap (with
  the steal flag), levered and unlevered IRR, minimum DSCR on its basis;
- ``exit_cap_grid``: the exit-cap sensitivity grid at the solved price;
- ``floors_enforced``: every house floor the pricing guardrail enforced
  (raised to the floor, or applied over deal evidence), one item per
  assumption and action with its original and applied values;
- ``r_and_m_top_up``: the R&M top-up line the R&M floor added;
- ``other_adjustments``: the other inputs the pricing replaced or disclosed
  (``policy_overrides`` that are not an enforced floor, such as a waiver);
- ``enforced_keys``: the register rows those floors set, highlighted on the
  page.

Every value is kept raw and as a display string; the page escapes the
display strings. Any field the summary lacks shows ``n/a``, so a bare or
infeasible summary renders too. Nothing here reads the clock.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Optional

from engine.assumptions import guardrails
from engine.assumptions.register import format_number, format_value, label_for
from engine.assumptions.sheet import _jsonable, _number
from engine.modules.loan_sizing import SHORT_LABELS as LOAN_SIZING_LABELS

NA = "n/a"

STATUS_LABELS = {
    "converged": "Converged",
    "ceiling_feasible": "Feasible at the search ceiling",
    "iteration_limit": "Iteration limit (not converged)",
    "infeasible": "Infeasible",
}

ACTION_LABELS = {
    guardrails.ACTION_ENFORCED: "Raised to house floor",
    guardrails.ACTION_APPLIED_OVER_EVIDENCE: "Floor applied over deal evidence",
    guardrails.ACTION_WAIVED: "House floor waived (analyst override)",
    guardrails.ACTION_WAIVED_BY_EVIDENCE: "House floor waived (deal evidence)",
    guardrails.ACTION_WARNED: "Below house floor (warned)",
}

def _get(data: Any, *keys: Any) -> Any:
    for key in keys:
        if not isinstance(data, Mapping):
            return None
        data = data.get(key)
    return data


def _finite(value: Any) -> Optional[float]:
    """A finite number; decimal strings count (``summary.bracket`` keeps prices as ``"15000000.00"``)."""
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    number = _number(value)
    if number is None or not math.isfinite(number):
        return None
    return float(number)


def _text(value: Any) -> str:
    """A value the summary carries that is not the number expected (shown as given)."""
    return NA if value is None else str(value)


def _pct(value: Any) -> str:
    number = _finite(value)
    return f"{number:.2%}" if number is not None else _text(value)


def _usd(value: Any, *, cents: bool = False) -> str:
    number = _finite(value)
    if number is None:
        return _text(value)
    return f"${number:,.2f}" if cents else f"${number:,.0f}"


def _times(value: Any) -> str:
    number = _finite(value)
    return f"{number:.2f}x" if number is not None else _text(value)


def _bps(value: Any) -> str:
    number = _finite(value)
    return f"{number * 10_000:,.0f} bps" if number is not None else _text(value)


def _metric(key: str, label: str, value: Any, display: str, detail: Optional[str] = None) -> dict[str, Any]:
    return {"key": key, "label": label, "value": _jsonable(value), "display": display, "detail": detail}


def _sizing_label(basis: Any) -> str:
    return LOAN_SIZING_LABELS.get(basis, _text(basis)) if isinstance(basis, str) else _text(basis)


def _entries(canonical: Any) -> Mapping[str, Any]:
    entries = _get(canonical, "assumptions", "entries")
    return entries if isinstance(entries, Mapping) else {}


def _unit_for(key: Any, entries: Mapping[str, Any]) -> Any:
    """The entry's unit; else a guess from the key (``*_per_unit`` / ``*annual*`` are dollars)."""
    entry = entries.get(key) if isinstance(key, str) else None
    if isinstance(entry, Mapping) and entry.get("unit"):
        return entry["unit"]
    name = str(key or "")
    if "per_unit" in name or "annual" in name or "amount" in name:
        return "usd"
    return None


def _range_display(values: list[Any], unit: Any) -> Optional[str]:
    if not values:
        return None
    numbers = [_finite(v) for v in values]
    if all(n is not None for n in numbers):
        low, high = min(numbers), max(numbers)
        if low == high:
            return format_number(low, unit)
        return f"{format_number(low, unit)} to {format_number(high, unit)}"
    return format_value(values[0], unit)


def _policy_summary(summary: Mapping[str, Any]) -> Mapping[str, Any]:
    """``summary.policy_summary``; an infeasible summary keeps it in effective_assumptions."""
    for found in (summary.get("policy_summary"), _get(summary, "effective_assumptions", "house_adjustments")):
        if isinstance(found, Mapping) and found:
            return found
    return {}


def _findings(policy_summary: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    findings = _get(policy_summary, "guardrails", "findings")
    return [f for f in findings if isinstance(f, Mapping)] if isinstance(findings, list) else []


# ── the floors the pricing enforced ──────────────────────────────────────


def floors_enforced(findings: Iterable[Mapping[str, Any]], entries: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One item per (assumption, action) the pricing guardrail enforced, in finding order.

    A floor applied per segment (vacancy by cohort and month) is one item with
    the segment ``count`` and the range of the originals it raised.
    """
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for finding in findings:
        action = finding.get("action")
        if action not in guardrails.ENFORCING_ACTIONS:
            continue
        grouped.setdefault((str(finding.get("key")), str(action)), []).append(finding)
    out = []
    for (key, action), items in grouped.items():
        unit = _unit_for(key, entries)
        first = items[0]
        floors = [f.get("floor") for f in items if f.get("floor") is not None]
        applied = [f.get("applied") for f in items if f.get("applied") is not None]
        sources = sorted({str(f.get("floor_source")) for f in items if f.get("floor_source")})
        entry = entries.get(key) if isinstance(entries.get(key), Mapping) else None
        out.append({
            "key": key,
            "label": label_for(key, entry),
            "in_register": entry is not None,
            "action": action,
            "action_label": ACTION_LABELS.get(action, action),
            "count": len(items),
            "input": _jsonable(first.get("input")),
            "floor_display": _range_display(floors, unit),
            "floor_source": ", ".join(sources) or None,
            "original_display": _range_display([f.get("original") for f in items if "original" in f], unit),
            "applied_display": _range_display(applied, unit),
            "evidence": _jsonable(first.get("evidence")),
            "top_up_annual": _jsonable(first.get("top_up_annual")),
        })
    return out


def _floor_line_keys(entries: Mapping[str, Any]) -> list[str]:
    """Register rows a floor set outside its own entry: the insurance line, the R&M top-up."""
    keys = []
    for key, entry in entries.items():
        if not isinstance(entry, Mapping):
            continue
        note = _get(entry, "override", "note")
        action = _get(entry, "guardrail", "action")
        if note == guardrails.PRICING_FLOOR_LINE_NOTE or action in guardrails.ENFORCING_ACTIONS:
            keys.append(str(key))
    return keys


def _top_up_key(entries: Mapping[str, Any]) -> Optional[str]:
    for key, entry in entries.items():
        if _get(entry, "detail", "category_name") == guardrails.R_AND_M_TOP_UP_CATEGORY and str(key).endswith(".base"):
            return str(key)
    return None


def _r_and_m_top_up(policy_summary: Mapping[str, Any], entries: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    annual = _finite(policy_summary.get("repairs_maintenance_top_up_annual"))
    if not annual:
        return None
    floor = policy_summary.get("repairs_maintenance_floor_per_unit")
    return {
        "annual": annual,
        "annual_display": _usd(annual),
        "category_name": guardrails.R_AND_M_TOP_UP_CATEGORY,
        "key": _top_up_key(entries),
        "floor_per_unit_display": None if floor is None else _usd(floor),
    }


def _other_adjustments(policy_summary: Mapping[str, Any], entries: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``policy_overrides`` that are not an enforced floor (those are in ``floors_enforced``)."""
    overrides = policy_summary.get("policy_overrides")
    out = []
    for override in overrides if isinstance(overrides, list) else []:
        if not isinstance(override, Mapping):
            continue
        action = override.get("action")
        if override.get("guardrail") == "house_floor" or action in guardrails.ENFORCING_ACTIONS:
            continue
        name = override.get("assumption") or override.get("input")
        if "original" in override or "applied" in override:
            original, applied, unit = override.get("original"), override.get("applied"), _unit_for(name, entries)
        else:
            original, applied, unit = override.get("original_annual"), override.get("applied_annual"), "usd"
        detail = [f"{k}: {format_value(_jsonable(override[k]))}" for k in ("floor_source", "category_name", "reason")
                  if override.get(k) is not None]
        out.append({
            "input": _jsonable(name),
            "action": _jsonable(action),
            "action_label": ACTION_LABELS.get(action, _text(action) if action else "Replaced by pricing"),
            "original_display": None if original is None else format_value(_jsonable(original), unit),
            "applied_display": None if applied is None else format_value(_jsonable(applied), unit),
            "detail": "; ".join(detail) or None,
        })
    return out


# ── the summary metrics ──────────────────────────────────────────────────


def _target(summary: Mapping[str, Any]) -> Any:
    if summary.get("target_cash_on_cash_pct") is not None:
        return summary["target_cash_on_cash_pct"]
    if _finite(summary.get("target_coc_pct")) is not None:
        return _finite(summary["target_coc_pct"]) / 100
    return _get(summary, "effective_assumptions", "target_coc")


def _coc_basis(summary: Mapping[str, Any], policy_summary: Mapping[str, Any]) -> tuple[Any, str, Optional[str]]:
    basis = summary.get("coc_basis")
    detail = basis if isinstance(basis, Mapping) else {}
    name = detail.get("basis") if detail else basis
    if name is None:
        name = policy_summary.get("coc_basis") or _get(summary, "effective_assumptions", "policy", "coc_basis")
    labels = {"year_1": "Year 1", "stabilized_year": "Stabilized year"}
    display = labels.get(name, _text(name)) if isinstance(name, str) else _text(name)
    year = detail.get("analysis_year")
    if year is not None and name != "year_1":
        display += f" (analysis year {_text(year)}"
        months = detail.get("months")
        if isinstance(months, list) and len(months) == 2:
            display += f": {_text(months[0])} to {_text(months[1])}"
        display += ")"
    return name, display, None if detail.get("source") is None else _text(detail.get("source"))


def _exit_cap_detail(calc: Mapping[str, Any]) -> tuple[Optional[str], bool]:
    mode = calc.get("mode")
    steal = bool(calc.get("steal"))
    if mode == "entry_anchored" and calc.get("going_in_cap_rate") is not None:
        text = f"entry-anchored: going-in {_pct(calc['going_in_cap_rate'])} + {_bps(calc.get('spread'))}"
    elif mode:
        text = _text(mode).replace("_", " ")
    else:
        text = None
    if steal:
        text = (text + "; " if text else "") + f"steal exception: exit compressed {_bps(calc.get('compression'))}"
        if calc.get("market_cap_rate") is not None:
            text += f" toward market {_pct(calc['market_cap_rate'])}"
    return text, steal


def _grid(grid: Any) -> Optional[dict[str, Any]]:
    if not isinstance(grid, Mapping) or not isinstance(grid.get("points"), list):
        return None
    points = []
    for point in grid["points"]:
        if not isinstance(point, Mapping):
            continue
        offset = point.get("offset_bps")
        offset_display = f"{int(offset):+d} bps" if isinstance(offset, int) and not isinstance(offset, bool) else _text(offset)
        points.append({
            "offset_bps": _jsonable(offset),
            "offset_display": offset_display,
            "is_base": bool(point.get("is_base")),
            "exit_cap_display": _pct(point.get("exit_cap_rate")),
            "exit_value_display": _usd(point.get("exit_value")),
            "net_sale_proceeds_display": _usd(point.get("net_sale_proceeds")),
            "levered_irr_display": _pct(point.get("levered_irr")),
            "unlevered_irr_display": _pct(point.get("unlevered_irr")),
            "levered_em_display": _times(point.get("levered_em")),
        })
    return {
        "rule": _jsonable(grid.get("rule")),
        "base_exit_cap_display": _pct(grid.get("base_exit_cap_rate")),
        "exit_month": _jsonable(grid.get("exit_month")),
        "points": points,
    }


def _unlevered_irr(summary: Mapping[str, Any], results: Any) -> Any:
    found = _get(results, "metrics", "irr", "unlevered_irr")
    if found is not None:
        return found
    points = _get(summary, "exit_cap_sensitivity", "points")
    for point in points if isinstance(points, list) else []:
        if isinstance(point, Mapping) and point.get("is_base"):
            return point.get("unlevered_irr")
    return None


def _metrics(summary: Mapping[str, Any], policy_summary: Mapping[str, Any], results: Any) -> list[dict[str, Any]]:
    target = _target(summary)
    basis_name, basis_display, basis_source = _coc_basis(summary, policy_summary)
    achieved = _finite(summary.get("achieved_coc_pct"))
    achieved = None if achieved is None else achieved / 100
    margin = summary.get("cash_on_cash_hurdle_margin")
    price = summary.get("solved_purchase_price")
    units = policy_summary.get("units")
    price_detail = None
    if _finite(price) is not None and _finite(units):
        price_detail = f"{_usd(_finite(price) / _finite(units))}/unit · {format_number(units, None)} units"
    elif units is not None:
        price_detail = f"{_text(units)} units"
    sizing = summary.get("loan_sizing") if isinstance(summary.get("loan_sizing"), Mapping) else {}
    loan_detail = []
    if sizing.get("basis") is not None:
        loan_detail.append(f"sized on {_sizing_label(sizing.get('basis'))}")
    if sizing.get("sizing_method") is not None:
        loan_detail.append(_text(sizing["sizing_method"]).replace("_", " "))
    if sizing.get("required_dscr") is not None:
        loan_detail.append(f"{_times(sizing['required_dscr'])} required DSCR")
    if summary.get("interest_rate") is not None:
        loan_detail.append(f"{_pct(summary['interest_rate'])} rate")
    if sizing.get("source") is not None:
        loan_detail.append(f"basis source: {_text(sizing['source'])}")
    calc = summary.get("exit_cap_calculation") if isinstance(summary.get("exit_cap_calculation"), Mapping) else {}
    exit_detail, _ = _exit_cap_detail(calc)
    dscr_basis = summary.get("minimum_dscr_basis")
    dscr_detail = f"on {_sizing_label(dscr_basis)}" if dscr_basis is not None else None
    noi_dscr = summary.get("minimum_dscr_noi_before_capex")
    if noi_dscr is not None and dscr_basis != "noi_before_capex":
        dscr_detail = (dscr_detail + "; " if dscr_detail else "") + f"{_times(noi_dscr)} on NOI before capex"
    return [
        _metric("target_coc", "Target CoC", target, _pct(target), f"on {basis_display}"),
        _metric("coc_basis", "CoC basis", basis_name, basis_display, basis_source),
        _metric("achieved_coc", "Achieved CoC", achieved, _pct(achieved),
                None if _finite(margin) is None else f"{_bps(margin)} over target"),
        _metric("solved_price", "Solved price", price,
                _usd(price, cents=True) if price is not None else f"{NA} (no solved price)", price_detail),
        _metric("loan_amount", "Loan amount", summary.get("loan_amount"), _usd(summary.get("loan_amount")),
                "; ".join(loan_detail) or None),
        _metric("equity", "Equity", summary.get("solved_total_equity_basis"),
                _usd(summary.get("solved_total_equity_basis")),
                None if summary.get("solved_total_equity_basis") is None else "total equity basis"),
        _metric("going_in_cap", "Going-in cap", summary.get("going_in_cap_rate"), _pct(summary.get("going_in_cap_rate"))),
        _metric("exit_cap", "Exit cap", summary.get("exit_cap_rate"), _pct(summary.get("exit_cap_rate")), exit_detail),
        _metric("levered_irr", "Levered IRR", summary.get("levered_irr"), _pct(summary.get("levered_irr"))),
        _metric("unlevered_irr", "Unlevered IRR", _unlevered_irr(summary, results), _pct(_unlevered_irr(summary, results))),
        _metric("minimum_dscr", "Minimum DSCR", summary.get("minimum_dscr"), _times(summary.get("minimum_dscr")),
                dscr_detail),
    ]


def _notes(status: Any, summary: Mapping[str, Any], target: Any, has_assumptions: bool) -> list[str]:
    bracket = summary.get("bracket") if isinstance(summary.get("bracket"), Mapping) else {}
    notes = []
    if status == "infeasible":
        text = "No solved price: the target CoC is not met anywhere in the search bracket"
        if bracket.get("min_price") is not None:
            text += (f"; at the minimum price {_usd(bracket['min_price'], cents=True)} the CoC is "
                     f"{_pct(bracket.get('min_price_coc'))} against a target of {_pct(target)}")
        notes.append(text + ".")
        if has_assumptions:
            notes.append("The assumptions below are those the search priced at the bracket floor "
                         f"({_usd(bracket.get('min_price'), cents=True)}).")
    elif status == "ceiling_feasible":
        notes.append(f"The target is met at the maximum price searched ({_usd(bracket.get('max_price'), cents=True)}); "
                     "the true boundary may be higher.")
    elif status == "iteration_limit":
        notes.append("The search stopped at its iteration limit: the price shown is feasible but is not the "
                     "converged boundary.")
    return notes


def pricing_header(
    summary: Optional[Mapping[str, Any]],
    *,
    canonical: Optional[Mapping[str, Any]] = None,
    results: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """The pricing header for the register page of a backsolve run.

    ``summary`` is the backsolve summary; ``canonical`` the case the page
    shows (the solved case, or for an infeasible solve the case priced at the
    bracket floor), used for units and the rows a floor set; ``results`` the
    solved results (for the unlevered IRR).
    """
    summary = summary if isinstance(summary, Mapping) else {}
    entries = _entries(canonical)
    policy_summary = _policy_summary(summary)
    status = summary.get("status")
    findings = _findings(policy_summary)
    floors = floors_enforced(findings, entries)
    enforced_keys = sorted({item["key"] for item in floors} | set(_floor_line_keys(entries)))
    top_up = _r_and_m_top_up(policy_summary, entries)
    if top_up and top_up["key"] and top_up["key"] not in enforced_keys:
        enforced_keys = sorted(set(enforced_keys) | {top_up["key"]})
    return {
        "status": _jsonable(status),
        "status_label": STATUS_LABELS.get(status, _text(status)) if isinstance(status, str) else _text(status),
        "solved": summary.get("solved_purchase_price") is not None,
        "notes": _notes(status, summary, _target(summary), bool(entries)),
        "metrics": _metrics(summary, policy_summary, results),
        "exit_cap_grid": _grid(summary.get("exit_cap_sensitivity")),
        "exit_cap_steal": bool(_get(summary, "exit_cap_calculation", "steal")),
        "floors_enforced": floors,
        # The CLI's "Floors enforced" count: one per finding (a segment, a cohort).
        "floors_enforced_findings": sum(1 for f in findings if f.get("action") in guardrails.ENFORCING_ACTIONS),
        "r_and_m_top_up": top_up,
        "other_adjustments": _other_adjustments(policy_summary, entries),
        "enforced_keys": enforced_keys,
    }
