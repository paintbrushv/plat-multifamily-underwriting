from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from openpyxl import load_workbook


@dataclass(frozen=True)
class ExcelTable:
    name: str
    headers: List[str]
    rows: List[List[Any]]


def _normalize_cell(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, datetime):
        d = value.date()
        return d.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _is_blank_row(values: Iterable[Any]) -> bool:
    for v in values:
        if v is None:
            continue
        if isinstance(v, str) and v.strip() == "":
            continue
        return False
    return True


_INT_COERCION_EPSILON = 0.01


def _coerce_int(value: Any, field: str) -> int:
    """Coerce a workbook cell to int.

    Standardized policy (Wave 3 / Bug 1.8): allow floats whose fractional part
    is within ±_INT_COERCION_EPSILON of an integer (round-to-int). Anything
    with a larger fractional part is a HARD error rather than silent truncation
    (the prior `rediq_bridge._to_int` truncate-anything behavior is not safe
    for required integer fields like unit_count / monthly_pace).
    """
    if value is None:
        raise ValueError(f"Missing required integer field: {field}")
    if isinstance(value, bool):
        raise ValueError(f"Invalid integer for {field}: {value}")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        nearest = round(value)
        if abs(value - nearest) <= _INT_COERCION_EPSILON:
            return int(nearest)
        raise ValueError(
            f"Invalid integer for {field}: {value!r} "
            f"(non-integer fraction exceeds tolerance {_INT_COERCION_EPSILON})"
        )
    if isinstance(value, str):
        s = value.strip()
        if s.isdigit():
            return int(s)
        # Allow signed integer strings ("-7", "+12") and float strings within epsilon
        try:
            f = float(s)
        except ValueError as e:
            raise ValueError(f"Invalid integer for {field}: {value!r}") from e
        nearest = round(f)
        if abs(f - nearest) <= _INT_COERCION_EPSILON:
            return int(nearest)
        raise ValueError(
            f"Invalid integer for {field}: {value!r} "
            f"(non-integer fraction exceeds tolerance {_INT_COERCION_EPSILON})"
        )
    raise ValueError(f"Invalid integer for {field}: {value!r}")


def _coerce_float(value: Any, field: str) -> float:
    if value is None:
        raise ValueError(f"Missing required numeric field: {field}")
    if isinstance(value, bool):
        raise ValueError(f"Invalid numeric for {field}: {value}")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return float(value.strip())
    raise ValueError(f"Invalid numeric for {field}: {value!r}")


def _coerce_str(value: Any, field: str) -> str:
    if value is None:
        raise ValueError(f"Missing required text field: {field}")
    s = str(value).strip()
    if s == "":
        raise ValueError(f"Missing required text field: {field}")
    return s


# ---------------------------------------------------------------------------
# Enum-migration maps — normalise old workbook enum values to current schema.
# Workbooks produced before the schema rename still encode the old strings;
# reading them through these maps avoids hard failures on legacy templates.
# ---------------------------------------------------------------------------
_GROWTH_TYPE_ALIASES: dict[str, str] = {
    "compound": "annual_compound",
    "monthly_compound": "monthly_compound",  # already canonical
    "step_only": "step_only",                # already canonical
    "annual_compound": "annual_compound",    # already canonical
}

_DECAY_TYPE_ALIASES: dict[str, str] = {
    "linear": "monthly_linear",
    "monthly_linear": "monthly_linear",      # already canonical
    "annual_step": "annual_step",            # already canonical
    "none": "none",                          # already canonical
}


def _read_excel_table(wb_path: Path, table_name: str) -> ExcelTable:
    wb = load_workbook(wb_path, data_only=True)
    try:
        for ws in wb.worksheets:
            if table_name not in ws.tables:
                continue

            table = ws.tables[table_name]
            cells = ws[table.ref]
            headers = [_normalize_cell(c.value) for c in cells[0]]
            if any(h is None or str(h).strip() == "" for h in headers):
                raise ValueError(f"Table {table_name} has blank header(s)")

            rows: List[List[Any]] = []
            for row_cells in cells[1:]:
                row = [_normalize_cell(c.value) for c in row_cells]
                if _is_blank_row(row):
                    continue
                rows.append(row)

            return ExcelTable(name=table_name, headers=[str(h) for h in headers], rows=rows)

        raise KeyError(f"Missing required Excel table: {table_name}")
    finally:
        wb.close()


def _table_to_dicts(table: ExcelTable) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in table.rows:
        if len(row) != len(table.headers):
            raise ValueError(f"Table {table.name} has malformed row length: {len(row)} expected {len(table.headers)}")
        item = {table.headers[i]: row[i] for i in range(len(table.headers))}
        out.append(item)
    return out


def _single_row_table_to_dict(table: ExcelTable) -> Dict[str, Any]:
    if len(table.rows) != 1:
        raise ValueError(f"Table {table.name} must have exactly 1 row; got {len(table.rows)}")
    return _table_to_dicts(table)[0]


def load_inputs_from_workbook(workbook_path: Path) -> Dict[str, Any]:
    metadata = _single_row_table_to_dict(_read_excel_table(workbook_path, "tbl_metadata"))
    time_grid = _single_row_table_to_dict(_read_excel_table(workbook_path, "tbl_time_grid"))

    unit_cohorts = _table_to_dicts(_read_excel_table(workbook_path, "tbl_unit_cohorts"))
    market_rent_curve = _table_to_dicts(_read_excel_table(workbook_path, "tbl_market_rent_curve"))
    loss_to_lease = _table_to_dicts(_read_excel_table(workbook_path, "tbl_loss_to_lease"))
    physical_vacancy_curve = _table_to_dicts(_read_excel_table(workbook_path, "tbl_physical_vacancy_curve"))
    collection_loss_curve = _table_to_dicts(_read_excel_table(workbook_path, "tbl_collection_loss_curve"))

    revenue_programs = _table_to_dicts(_read_excel_table(workbook_path, "tbl_revenue_programs"))
    program_adoption_curve = _table_to_dicts(_read_excel_table(workbook_path, "tbl_program_adoption_curve"))

    program_capacity: Optional[List[Dict[str, Any]]] = None
    try:
        program_capacity = _table_to_dicts(_read_excel_table(workbook_path, "tbl_program_capacity"))
    except KeyError:
        program_capacity = None

    program_costs: Optional[List[Dict[str, Any]]] = None
    try:
        program_costs = _table_to_dicts(_read_excel_table(workbook_path, "tbl_program_costs"))
    except KeyError:
        program_costs = None

    for c in unit_cohorts:
        c["cohort_id"] = _coerce_str(c.get("cohort_id"), "unit_cohorts.cohort_id")
        c["unit_type"] = _coerce_str(c.get("unit_type"), "unit_cohorts.unit_type")
        c["unit_count"] = _coerce_int(c.get("unit_count"), "unit_cohorts.unit_count")
        if c.get("sqft") is not None and str(c.get("sqft")).strip() != "":
            c["sqft"] = _coerce_float(c.get("sqft"), "unit_cohorts.sqft")
        else:
            c.pop("sqft", None)
        c["initial_inplace_rent"] = _coerce_float(c.get("initial_inplace_rent"), "unit_cohorts.initial_inplace_rent")

    def coerce_curve(rows: List[Dict[str, Any]], value_field: str, label: str) -> None:
        for r in rows:
            if "cohort_id" in r:
                r["cohort_id"] = _coerce_str(r.get("cohort_id"), f"{label}.cohort_id")
            if "program_id" in r:
                r["program_id"] = _coerce_str(r.get("program_id"), f"{label}.program_id")
            if "applies_to" in r:
                r["applies_to"] = _coerce_str(r.get("applies_to"), f"{label}.applies_to")
            r["start_period"] = _coerce_str(r.get("start_period"), f"{label}.start_period")
            r["end_period"] = _coerce_str(r.get("end_period"), f"{label}.end_period")
            r[value_field] = _coerce_float(r.get(value_field), f"{label}.{value_field}")

    coerce_curve(market_rent_curve, "market_rent", "market_rent_curve")
    coerce_curve(loss_to_lease, "ltl_percent", "loss_to_lease")
    coerce_curve(physical_vacancy_curve, "vacancy_rate", "physical_vacancy_curve")
    coerce_curve(collection_loss_curve, "loss_rate", "collection_loss_curve")

    for p in revenue_programs:
        p["program_id"] = _coerce_str(p.get("program_id"), "revenue_programs.program_id")
        p["program_name"] = _coerce_str(p.get("program_name"), "revenue_programs.program_name")
        p["program_type"] = _coerce_str(p.get("program_type"), "revenue_programs.program_type")
        p["pricing_type"] = _coerce_str(p.get("pricing_type"), "revenue_programs.pricing_type")
        p["price_value"] = _coerce_float(p.get("price_value"), "revenue_programs.price_value")
        p["eligible_units"] = _coerce_str(p.get("eligible_units"), "revenue_programs.eligible_units")
        p["start_period"] = _coerce_str(p.get("start_period"), "revenue_programs.start_period")
        if p.get("end_period") is None or str(p.get("end_period")).strip() == "":
            p.pop("end_period", None)
        else:
            p["end_period"] = _coerce_str(p.get("end_period"), "revenue_programs.end_period")

    coerce_curve(program_adoption_curve, "adoption_rate", "program_adoption_curve")

    if program_capacity is not None:
        cleaned = []
        for row in program_capacity:
            if _is_blank_row(row.values()):
                continue
            cleaned.append(
                {
                    "program_id": _coerce_str(row.get("program_id"), "program_capacity.program_id"),
                    "total_capacity": _coerce_float(row.get("total_capacity"), "program_capacity.total_capacity"),
                }
            )
        program_capacity = cleaned if cleaned else None

    if program_costs is not None:
        cleaned = []
        for row in program_costs:
            if _is_blank_row(row.values()):
                continue
            cleaned.append(
                {
                    "program_id": _coerce_str(row.get("program_id"), "program_costs.program_id"),
                    "cost_type": _coerce_str(row.get("cost_type"), "program_costs.cost_type"),
                    "cost_value": _coerce_float(row.get("cost_value"), "program_costs.cost_value"),
                }
            )
        program_costs = cleaned if cleaned else None

    inputs: Dict[str, Any] = {
        "schema_version": "0.1",
        "metadata": {
            "deal_id": _coerce_str(metadata.get("deal_id"), "metadata.deal_id"),
            "run_id": _coerce_str(metadata.get("run_id"), "metadata.run_id"),
            "as_of_date": _coerce_str(metadata.get("as_of_date"), "metadata.as_of_date"),
            "analyst": _coerce_str(metadata.get("analyst"), "metadata.analyst"),
            "purpose": _coerce_str(metadata.get("purpose"), "metadata.purpose"),
        },
        "time_grid": {
            "analysis_start_date": _coerce_str(time_grid.get("analysis_start_date"), "time_grid.analysis_start_date"),
            "analysis_end_date": _coerce_str(time_grid.get("analysis_end_date"), "time_grid.analysis_end_date"),
        },
        "unit_cohorts": unit_cohorts,
        "market_rent_curve": market_rent_curve,
        "loss_to_lease": loss_to_lease,
        "physical_vacancy_curve": physical_vacancy_curve,
        "collection_loss_curve": collection_loss_curve,
        "revenue_programs": revenue_programs,
        "program_adoption_curve": program_adoption_curve,
    }

    if metadata.get("notes") is not None and str(metadata.get("notes")).strip() != "":
        inputs["metadata"]["notes"] = str(metadata.get("notes")).strip()

    if program_capacity is not None:
        inputs["program_capacity"] = program_capacity
    if program_costs is not None:
        inputs["program_costs"] = program_costs

    # V0.2 tables - load if present
    # Purchase assumptions
    try:
        purchase_table = _single_row_table_to_dict(_read_excel_table(workbook_path, "tbl_purchase_assumptions"))
        inputs["purchase_assumptions"] = {
            "purchase_price": _coerce_float(purchase_table.get("purchase_price"), "purchase_assumptions.purchase_price"),
            "equity_contribution": _coerce_float(purchase_table.get("equity_contribution"), "purchase_assumptions.equity_contribution"),
        }
        if purchase_table.get("closing_costs") is not None:
            inputs["purchase_assumptions"]["closing_costs"] = _coerce_float(purchase_table.get("closing_costs"), "purchase_assumptions.closing_costs")
        if purchase_table.get("closing_date") is not None:
            inputs["purchase_assumptions"]["closing_date"] = _coerce_str(purchase_table.get("closing_date"), "purchase_assumptions.closing_date")
    except KeyError:
        pass

    # Exit assumptions
    try:
        exit_table = _single_row_table_to_dict(_read_excel_table(workbook_path, "tbl_exit_assumptions"))
        inputs["exit_assumptions"] = {
            "exit_cap_rate": _coerce_float(exit_table.get("exit_cap_rate"), "exit_assumptions.exit_cap_rate"),
            "exit_month": _coerce_str(exit_table.get("exit_month"), "exit_assumptions.exit_month"),
        }
        if exit_table.get("sale_cost_percent") is not None:
            inputs["exit_assumptions"]["sale_cost_percent"] = _coerce_float(exit_table.get("sale_cost_percent"), "exit_assumptions.sale_cost_percent")
    except KeyError:
        pass

    # OpEx categories
    try:
        opex_rows = _table_to_dicts(_read_excel_table(workbook_path, "tbl_opex_categories"))
        opex_table = []
        for row in opex_rows:
            if _is_blank_row(row.values()):
                continue
            item = {
                "category_name": _coerce_str(row.get("category_name"), "opex_categories.category_name"),
                "calculation_type": _coerce_str(row.get("calculation_type"), "opex_categories.calculation_type"),
                "base_value": _coerce_float(row.get("base_value"), "opex_categories.base_value"),
                "recoverable_flag": _coerce_bool(row.get("recoverable_flag", False)),
            }
            if row.get("growth_rate") is not None and str(row.get("growth_rate")).strip() != "":
                item["growth_rate"] = _coerce_float(row.get("growth_rate"), "opex_categories.growth_rate")
            opex_table.append(item)
        if opex_table:
            inputs["opex_table"] = opex_table
    except KeyError:
        pass

    # CapEx schedule
    try:
        capex_rows = _table_to_dicts(_read_excel_table(workbook_path, "tbl_capex_schedule"))
        capex_schedule = []
        for row in capex_rows:
            if _is_blank_row(row.values()):
                continue
            item = {
                "category": _coerce_str(row.get("category"), "capex_schedule.category"),
                "capex_type": _coerce_str(row.get("capex_type"), "capex_schedule.capex_type"),
            }
            # Conditional fields based on capex_type
            if row.get("month") is not None and str(row.get("month")).strip() != "":
                item["month"] = _coerce_str(row.get("month"), "capex_schedule.month")
            if row.get("amount") is not None and str(row.get("amount")).strip() != "":
                item["amount"] = _coerce_float(row.get("amount"), "capex_schedule.amount")
            if row.get("amount_per_unit") is not None and str(row.get("amount_per_unit")).strip() != "":
                item["amount_per_unit"] = _coerce_float(row.get("amount_per_unit"), "capex_schedule.amount_per_unit")
            if row.get("amount_per_unit_monthly") is not None and str(row.get("amount_per_unit_monthly")).strip() != "":
                item["amount_per_unit_monthly"] = _coerce_float(row.get("amount_per_unit_monthly"), "capex_schedule.amount_per_unit_monthly")
            if row.get("timing") is not None and str(row.get("timing")).strip() != "":
                item["timing"] = _coerce_str(row.get("timing"), "capex_schedule.timing")
            if row.get("units_affected") is not None and str(row.get("units_affected")).strip() != "":
                item["units_affected"] = _coerce_int(row.get("units_affected"), "capex_schedule.units_affected")
            capex_schedule.append(item)
        if capex_schedule:
            inputs["capex_schedule"] = capex_schedule
    except KeyError:
        pass

    # Debt terms
    try:
        debt_table = _single_row_table_to_dict(_read_excel_table(workbook_path, "tbl_debt_terms"))
        inputs["debt_terms"] = {
            "commitment": _coerce_float(debt_table.get("commitment"), "debt_terms.commitment"),
            "rate": _coerce_float(debt_table.get("rate"), "debt_terms.rate"),
            "amort_years": _coerce_int(debt_table.get("amort_years"), "debt_terms.amort_years"),
        }
        if debt_table.get("io_months") is not None and str(debt_table.get("io_months")).strip() != "":
            inputs["debt_terms"]["io_months"] = _coerce_int(debt_table.get("io_months"), "debt_terms.io_months")
        if debt_table.get("loan_start_month") is not None and str(debt_table.get("loan_start_month")).strip() != "":
            inputs["debt_terms"]["loan_start_month"] = _coerce_str(debt_table.get("loan_start_month"), "debt_terms.loan_start_month")
    except KeyError:
        pass

    # Debt draw schedule (optional)
    try:
        draw_rows = _table_to_dicts(_read_excel_table(workbook_path, "tbl_debt_draw_schedule"))
        debt_draw_schedule = []
        for row in draw_rows:
            if _is_blank_row(row.values()):
                continue
            debt_draw_schedule.append({
                "month": _coerce_str(row.get("month"), "debt_draw_schedule.month"),
                "draw_amount": _coerce_float(row.get("draw_amount"), "debt_draw_schedule.draw_amount"),
            })
        if debt_draw_schedule:
            inputs["debt_draw_schedule"] = debt_draw_schedule
    except KeyError:
        pass

    # Renovation programs (optional)
    try:
        reno_rows = _table_to_dicts(_read_excel_table(workbook_path, "tbl_renovation_programs"))
        renovation_programs = []
        for row in reno_rows:
            if _is_blank_row(row.values()):
                continue
            item = {
                "program_id": _coerce_str(row.get("program_id"), "renovation_programs.program_id"),
                "program_name": _coerce_str(row.get("program_name"), "renovation_programs.program_name"),
                "target_cohort": _coerce_str(row.get("target_cohort"), "renovation_programs.target_cohort"),
                "output_cohort": _coerce_str(row.get("output_cohort"), "renovation_programs.output_cohort"),
                "renovation_cost_per_unit": _coerce_float(row.get("renovation_cost_per_unit"), "renovation_programs.renovation_cost_per_unit"),
                "rent_premium_monthly": _coerce_float(row.get("rent_premium_monthly"), "renovation_programs.rent_premium_monthly"),
                "downtime_days": _coerce_int(row.get("downtime_days"), "renovation_programs.downtime_days"),
                "strategy": _coerce_str(row.get("strategy"), "renovation_programs.strategy"),
                "start_month": _coerce_str(row.get("start_month"), "renovation_programs.start_month"),
                "monthly_pace": _coerce_int(row.get("monthly_pace"), "renovation_programs.monthly_pace"),
            }
            if row.get("end_month") is not None and str(row.get("end_month")).strip() != "":
                item["end_month"] = _coerce_str(row.get("end_month"), "renovation_programs.end_month")
            renovation_programs.append(item)
        if renovation_programs:
            # Bug 1.9 fix: enforce the same uniqueness invariant as
            # engine.validator._check_renovation_programs at the loader
            # boundary so workbooks with colliding output_cohorts fail loudly
            # before the engine ever sees them. Mirror of the cohort-collision bug
            # (output_cohort = "Beal_renovated" colliding with the rent-roll
            # subtotal cohort_id "Beal Renovated" → first-match-wins on
            # market_rent_curve → wrong IRR).
            _check_renovation_output_cohort_collision(
                unit_cohorts=unit_cohorts,
                renovation_programs=renovation_programs,
            )
            inputs["renovation_programs"] = renovation_programs
    except KeyError:
        pass

    # Growth assumptions (optional)
    try:
        growth_table = _single_row_table_to_dict(_read_excel_table(workbook_path, "tbl_growth_assumptions"))
        _raw_gt = _coerce_str(growth_table.get("growth_type"), "growth_assumptions.growth_type")
        inputs["growth_assumptions"] = {
            "growth_type": _GROWTH_TYPE_ALIASES.get(_raw_gt, _raw_gt),
            "annual_growth_rate": _coerce_float(growth_table.get("annual_growth_rate"), "growth_assumptions.annual_growth_rate"),
        }
        if growth_table.get("growth_start_month") is not None and str(growth_table.get("growth_start_month")).strip() != "":
            inputs["growth_assumptions"]["growth_start_month"] = _coerce_str(growth_table.get("growth_start_month"), "growth_assumptions.growth_start_month")
    except KeyError:
        pass

    # LTL decay assumptions (optional)
    try:
        ltl_table = _single_row_table_to_dict(_read_excel_table(workbook_path, "tbl_ltl_decay"))
        _raw_dt = _coerce_str(ltl_table.get("decay_type"), "ltl_decay.decay_type")
        inputs["ltl_decay_assumptions"] = {
            "decay_type": _DECAY_TYPE_ALIASES.get(_raw_dt, _raw_dt),
            "initial_ltl_percent": _coerce_float(ltl_table.get("initial_ltl_percent"), "ltl_decay.initial_ltl_percent"),
        }
        if ltl_table.get("annual_decay_rate") is not None and str(ltl_table.get("annual_decay_rate")).strip() != "":
            inputs["ltl_decay_assumptions"]["annual_decay_rate"] = _coerce_float(ltl_table.get("annual_decay_rate"), "ltl_decay.annual_decay_rate")
        if ltl_table.get("minimum_ltl_percent") is not None and str(ltl_table.get("minimum_ltl_percent")).strip() != "":
            inputs["ltl_decay_assumptions"]["minimum_ltl_percent"] = _coerce_float(ltl_table.get("minimum_ltl_percent"), "ltl_decay.minimum_ltl_percent")
        if ltl_table.get("decay_start_month") is not None and str(ltl_table.get("decay_start_month")).strip() != "":
            inputs["ltl_decay_assumptions"]["decay_start_month"] = _coerce_str(ltl_table.get("decay_start_month"), "ltl_decay.decay_start_month")
    except KeyError:
        pass

    return inputs


def _check_renovation_output_cohort_collision(
    unit_cohorts: List[Dict[str, Any]],
    renovation_programs: List[Dict[str, Any]],
) -> None:
    """Reject workbooks where renovation_programs[].output_cohort collides
    with an existing unit_cohorts[].cohort_id.

    This mirrors `engine.validator._check_renovation_programs` rule (b) but
    fires at the workbook-load boundary so the error surfaces immediately on
    ingest rather than after the validator has run.

    Raises ValueError on the first collision found.
    """
    cohort_ids = {c.get("cohort_id") for c in unit_cohorts if c.get("cohort_id") is not None}
    for idx, program in enumerate(renovation_programs):
        output = program.get("output_cohort")
        if output is None:
            continue
        if output in cohort_ids:
            raise ValueError(
                f"renovation_programs[{idx}].output_cohort '{output}' collides with an "
                f"existing unit_cohorts.cohort_id; output_cohort must be a NEW cohort id "
                f"(see engine.validator._check_renovation_programs / the schema audit Bug 1.9)"
            )


def _coerce_bool(value: Any) -> bool:
    """Coerce a value to boolean."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("true", "yes", "y", "1"):
        return True
    if s in ("false", "no", "n", "0", ""):
        return False
    raise ValueError(f"Invalid boolean value: {value!r}")

