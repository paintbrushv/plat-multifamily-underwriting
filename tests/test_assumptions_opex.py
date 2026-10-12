"""ADR 0003 slice 5: opex treatment in the assumption layer.

Every opex line gets ``opex.<slug>.base`` (annual dollars, T12 provenance)
and ``opex.<slug>.growth`` (house per-category table unless the analyst set a
rate). Ingested deals now grow opex by category; canonicals that already carry
growth rates keep their numbers.
"""
from __future__ import annotations

import copy
import csv
import json
from decimal import Decimal
from pathlib import Path

import pytest

from engine import underwriting_policy as house
from engine.assumptions import AssumptionEntry, AssumptionOverrideError, resolve_assumptions
from engine.assumptions.opex import growth_category, opex_line_slugs
from engine.engine import run_underwriting
from engine.ingest import t12_parser
from engine.ingest.ingestion_pipeline import build_deal_from_documents
from engine.validator import validate_deal, validate_schema

FIXTURES = Path(__file__).parent / "fixtures"
T12 = FIXTURES / "entrata_style_t12.csv"
ROLL = FIXTURES / "sample_rent_roll.csv"
UNITS = 10
PURCHASE_PRICE = 25_000_000
TAX_MILLS = 36.0  # 25,000,000 x 36 / 1,000 = 900,000, the T12 tax line

ECONOMICS = dict(
    purchase_price=PURCHASE_PRICE,
    ltv=0.75,
    loan_rate=0.065,
    amort_years=30,
    exit_cap_rate=0.055,
    sponsor_equity_pct=0.10,
    pref_return=0.08,
)


def _ingest(t12=T12, **kwargs):
    params = dict(
        property_id="Synthetic Opex",
        rent_roll_path=ROLL,
        t12_path=t12,
        analysis_start="2026-08",
        analysis_end="2031-07",
    )
    params.update(kwargs)
    return build_deal_from_documents(**params)


def _entries(deal):
    return deal["assumptions"]["entries"]


def _row(deal, name):
    return next(row for row in deal["opex_table"] if row["category_name"] == name)


def _with_tax_policy(deal):
    deal = copy.deepcopy(deal)
    deal["metadata"].setdefault("property_summary", {})["property_tax_policy"] = {
        "millage_rate_mills": TAX_MILLS,
        "assessment_ratio": 1.0,
        "source": "synthetic_test",
        "source_locator": "tests/test_assumptions_opex.py",
        "analyst_override": False,
    }
    return deal


def _t12_without(tmp_path: Path, labels: set[str]) -> Path:
    """The synthetic T12 minus ``labels``, with Total Expenses and NOI re-tied."""
    with open(T12, newline="") as f:
        rows = list(csv.reader(f))
    removed = [0.0] * 13
    kept = []
    for row in rows:
        if row[0] in labels:
            removed = [a + float(b or 0) for a, b in zip(removed, row[1:])]
            continue
        kept.append(row)
    for row in kept:
        if row[0] in ("Total Expenses", "Net Operating Income"):
            sign = -1 if row[0] == "Total Expenses" else 1
            row[1:] = [f"{float(v) + sign * r:.2f}" for v, r in zip(row[1:], removed)]
    out = tmp_path / "t12_missing_lines.csv"
    with open(out, "w", newline="") as f:
        csv.writer(f).writerows(kept)
    return out


@pytest.fixture(scope="module")
def ingested():
    return _ingest(year_built=1984, **ECONOMICS)


# ── 1. house growth table ────────────────────────────────────────────────


def _parser_categories() -> set[str]:
    names = {canonical for _pattern, canonical, _rec in t12_parser._CATEGORY_MAP}
    names |= {canonical for _pattern, canonical in t12_parser._SUBTOTAL_CATEGORY_MAP}
    return names


def test_growth_table_covers_every_parser_category():
    table = house.OPEX_GROWTH_TABLE
    assert _parser_categories() <= set(table)
    for category, (constant, rate) in table.items():
        assert getattr(house, constant) == rate, category
        assert isinstance(rate, Decimal), category
        assert Decimal("0") <= rate <= Decimal("0.06"), category
    assert table["Real Estate Taxes"] == ("HOUSE_RE_TAX_GROWTH_AFTER_REASSESSMENT", Decimal("0.03"))
    assert house.default_opex_growth_rate("Unmapped Line") == ("OPEX_GROWTH_DEFAULT", house.OPEX_GROWTH_DEFAULT)
    assert house.default_opex_growth_rate("Payroll", calculation_type="percent_egr") == (
        "OPEX_GROWTH_PERCENT_OF_REVENUE", Decimal("0"),
    )


@pytest.mark.parametrize(
    "label, category",
    [
        ("Payroll", "Payroll"),
        ("Water & Sewer", "Water & Sewer"),
        ("Fuel (Gas & Oil)", "Fuel (Gas & Oil)"),
        ("R&M", "Repairs & Maintenance"),
        ("Management Fee", "Property Management Fee"),
        ("Office Supplies", "Administrative"),
        ("Telephone", "Contract Services"),
        ("Real Estate Taxes", "Real Estate Taxes"),
        ("Taxes - Real Estate", "Real Estate Taxes"),
        ("Bespoke Amenity Line", None),
    ],
)
def test_line_names_map_to_house_categories(label, category):
    assert growth_category(label) == category


def test_ingested_rows_get_the_house_rate_by_category(ingested):
    entries = _entries(ingested)
    expected = {
        "Payroll": house.OPEX_GROWTH_PAYROLL,
        "Repairs & Maintenance": house.OPEX_GROWTH_REPAIRS_MAINTENANCE,
        "Turnover / Make-Ready": house.OPEX_GROWTH_TURNOVER_MAKE_READY,
        "Utilities": house.OPEX_GROWTH_UTILITIES,
        "Office Supplies": house.OPEX_GROWTH_ADMINISTRATIVE,
        "Telephone": house.OPEX_GROWTH_CONTRACT_SERVICES,
        "Marketing / Advertising": house.OPEX_GROWTH_MARKETING_ADVERTISING,
        "Insurance": house.OPEX_GROWTH_INSURANCE,
        "Real Estate Taxes": house.HOUSE_RE_TAX_GROWTH_AFTER_REASSESSMENT,
    }
    for name, rate in expected.items():
        assert _row(ingested, name)["growth_rate"] == float(rate), name
        slug = opex_line_slugs([{"category_name": name}])[0]
        entry = entries[f"opex.{slug}.growth"]
        assert entry["source"] == "house:opex_growth_table", name
        assert entry["value"] == float(rate), name
        assert entry["review_required"] is True
        assert "house assumption to be reviewed" in entry["basis"]
    # The T12 fee is entered as a percent of EGR, so it gets the house 0% growth.
    fee = entries["opex.property_management_fee.growth"]
    assert (fee["value"], fee["source"]) == (0.0, "house:opex_growth_table")
    assert _row(ingested, "Property Management Fee")["growth_rate"] == 0.0


# ── 2. analyst growth is preserved ───────────────────────────────────────


def test_existing_growth_rate_is_an_analyst_value_and_is_preserved(ingested):
    deal = copy.deepcopy(ingested)
    _row(deal, "Payroll")["growth_rate"] = 0.02
    resolved = resolve_assumptions(deal)
    entry = _entries(resolved)["opex.payroll.growth"]

    assert _row(resolved, "Payroll")["growth_rate"] == 0.02
    assert entry["source"] == "analyst"
    assert entry["value"] == 0.02
    assert entry["default_value"] == float(house.OPEX_GROWTH_PAYROLL)
    assert entry["override"]["note"] == "changed in the canonical after the last resolution"


def test_growth_on_a_fresh_ingest_row_is_never_overwritten():
    deal = _ingest(year_built=1984)
    stripped = copy.deepcopy(deal)
    stripped.pop("assumptions")
    for row in stripped["opex_table"]:
        row.pop("growth_rate", None)
    _row(stripped, "Insurance")["growth_rate"] = 0.08
    resolved = resolve_assumptions(stripped)

    assert _row(resolved, "Insurance")["growth_rate"] == 0.08
    assert _entries(resolved)["opex.insurance.growth"]["source"] == "analyst"
    assert _row(resolved, "Payroll")["growth_rate"] == float(house.OPEX_GROWTH_PAYROLL)


def test_explicit_growth_override_wins_and_keeps_the_default(ingested):
    resolved = resolve_assumptions(ingested, overrides={"opex.insurance.growth": {"value": 0.07, "note": "IC"}})
    entry = _entries(resolved)["opex.insurance.growth"]

    assert _row(resolved, "Insurance")["growth_rate"] == 0.07
    assert entry["override"] == {"value": 0.07, "note": "IC"}
    assert entry["default_value"] == float(house.OPEX_GROWTH_INSURANCE)
    assert resolve_assumptions(resolved) == resolved


def test_opex_override_validation(ingested):
    with pytest.raises(AssumptionOverrideError, match="unknown opex lines"):
        resolve_assumptions(ingested, overrides={"opex.pool_heating.base": 1_000})
    with pytest.raises(AssumptionOverrideError, match="finite number"):
        resolve_assumptions(ingested, overrides={"opex.payroll.base": "lots"})
    with pytest.raises(AssumptionOverrideError, match="unknown opex lines"):
        resolve_assumptions(ingested, overrides={"opex.pool_heating.growth": 0.03})
    with pytest.raises(AssumptionOverrideError, match="finite number"):
        resolve_assumptions(ingested, overrides={"opex.payroll.growth": "3%"})


def test_partial_json_opex_growth_becomes_an_analyst_override(ingested):
    from runs.ingest_deal import apply_partial_and_reresolve

    table = copy.deepcopy(ingested["opex_table"])
    for row in table:
        row.pop("growth_rate", None)
    _row({"opex_table": table}, "Utilities")["growth_rate"] = float(house.OPEX_GROWTH_UTILITIES)
    merged = apply_partial_and_reresolve(ingested, {"opex_table": table})
    entries = _entries(merged)

    assert entries["opex.utilities.growth"]["source"] == "analyst"
    assert entries["opex.utilities.growth"]["override"]["note"] == "supplied by --partial-json"
    assert entries["opex.payroll.growth"]["source"] == "house:opex_growth_table"
    assert _row(merged, "Payroll")["growth_rate"] == float(house.OPEX_GROWTH_PAYROLL)


def test_hand_authored_canonical_numbers_are_unchanged(minimal_deal_inputs):
    resolved = resolve_assumptions(minimal_deal_inputs)

    assert resolved["opex_table"] == minimal_deal_inputs["opex_table"]
    for key, entry in _entries(resolved).items():
        if key.endswith(".growth"):
            assert entry["source"] == "analyst", key
    assert run_underwriting(resolved) == run_underwriting(copy.deepcopy(minimal_deal_inputs))


def test_hand_authored_row_without_growth_gets_the_house_rate(minimal_deal_inputs):
    deal = copy.deepcopy(minimal_deal_inputs)
    _row(deal, "Utilities").pop("growth_rate")
    resolved = resolve_assumptions(deal)
    entry = _entries(resolved)["opex.utilities.growth"]

    # Written and flagged exactly as for an ingested deal.
    assert _row(resolved, "Utilities")["growth_rate"] == float(house.OPEX_GROWTH_UTILITIES)
    assert entry["source"] == "house:opex_growth_table"
    assert entry["value"] == float(house.OPEX_GROWTH_UTILITIES)
    assert entry["review_required"] is True
    assert entry["override"] is None
    assert resolve_assumptions(resolved) == resolved
    assert entry["candidates"][0] == {
        "source": "house:opex_growth_table",
        "value": float(house.OPEX_GROWTH_UTILITIES),
        "basis": entry["candidates"][0]["basis"],
        "constant": "OPEX_GROWTH_UTILITIES",
    }


# ── 3. real estate taxes defer to the property-tax module ────────────────


def _taxes(results):
    return [r["expense"] for r in results["opex"]["by_category_by_month"] if r["category"] == "Real Estate Taxes"]


def test_real_estate_taxes_grow_at_the_house_rate_after_the_reassessment(ingested):
    entry = _entries(ingested)["opex.real_estate_taxes.growth"]

    assert _row(ingested, "Real Estate Taxes")["growth_rate"] == 0.03
    assert entry["source"] == "house:opex_growth_table"
    assert entry["value"] == float(house.HOUSE_RE_TAX_GROWTH_AFTER_REASSESSMENT) == 0.03
    assert entry["review_required"] is True
    assert entry["detail"]["house_constant"] == "HOUSE_RE_TAX_GROWTH_AFTER_REASSESSMENT"
    assert "engine/property_tax.py" in entry["basis"]
    assert [c["source"] for c in entry["candidates"]] == ["house:opex_growth_table", "engine_fallback"]

    taxes = _taxes(run_underwriting(_with_tax_policy(ingested)))
    assert len(taxes) == 60
    year1 = PURCHASE_PRICE * TAX_MILLS / 1000 / 12  # the reassessed tax, unchanged in year 1
    for year in range(5):
        months = taxes[12 * year:12 * year + 12]
        assert months == pytest.approx([year1 * 1.03**year] * 12, abs=0.01), year
    # Year over year 3%, exactly once: never stacked on the reassessment.
    assert taxes[12] / taxes[0] == pytest.approx(1.03, abs=1e-6)
    assert taxes[48] / taxes[36] == pytest.approx(1.03, abs=1e-6)


def test_real_estate_tax_growth_override_to_zero_holds_taxes_flat(ingested):
    resolved = resolve_assumptions(ingested, overrides={"opex.real_estate_taxes.growth": 0})
    entry = _entries(resolved)["opex.real_estate_taxes.growth"]
    assert (entry["source"], entry["value"], entry["default_value"]) == ("analyst", 0.0, 0.03)
    assert set(_taxes(run_underwriting(_with_tax_policy(resolved)))) == {PURCHASE_PRICE * TAX_MILLS / 1000 / 12}


def test_analyst_tax_growth_is_kept_on_the_reassessed_row(ingested):
    resolved = resolve_assumptions(ingested, overrides={"opex.real_estate_taxes.growth": 0.02})
    assert _row(resolved, "Real Estate Taxes")["growth_rate"] == 0.02

    results = run_underwriting(_with_tax_policy(resolved))
    taxes = [r["expense"] for r in results["opex"]["by_category_by_month"] if r["category"] == "Real Estate Taxes"]
    assert taxes[48] == pytest.approx(75_000 * 1.02**4, abs=0.01)


# ── 4. percent-of-EGR lines ──────────────────────────────────────────────


def test_percent_of_egr_line_shows_the_percent_and_gets_no_growth(minimal_deal_inputs):
    deal = copy.deepcopy(minimal_deal_inputs)
    fee = _row(deal, "Management Fee")
    fee.pop("growth_rate")
    deal["metadata"]["ingest_gate"] = {"status": "PASS", "blockers": []}  # treat as ingested
    resolved = resolve_assumptions(deal)
    entries = _entries(resolved)

    base = entries["opex.management_fee.base"]
    assert base["value"] == 0.04
    assert base["unit"] == "decimal_of_egr"
    assert base["detail"]["percent_of_revenue"] == 0.04
    assert base["detail"]["annual_amount"] is None
    assert base["detail"]["per_unit_annual"] is None

    growth = entries["opex.management_fee.growth"]
    assert growth["value"] == 0.0
    assert growth["source"] == "house:opex_growth_table"
    assert growth["detail"]["house_constant"] == "OPEX_GROWTH_PERCENT_OF_REVENUE"
    assert _row(resolved, "Management Fee")["growth_rate"] == 0.0


# ── 5. missing core categories ───────────────────────────────────────────

_CORE_LABELS = {
    "Maintenance Supplies", "Electrical Repairs/Supplies", "Plumbing Repairs", "HVAC Repairs",
    "Property Insurance", "Management Fee",
}


def test_missing_core_lines_are_added_from_house_defaults(tmp_path):
    deal = _ingest(t12=_t12_without(tmp_path, _CORE_LABELS), year_built=1984)
    names = [row["category_name"] for row in deal["opex_table"]]
    entries = _entries(deal)

    assert deal["metadata"]["ingest_gate"]["status"] == "PASS"
    assert names.count("Insurance") == 1
    assert names.count("Property Management Fee") == 1
    assert "Real Estate Taxes" in names and names.count("Real Estate Taxes") == 1
    # "Water Extraction" stays a repairs line, so R&M is present and not added.
    assert "Repairs & Maintenance" in names
    assert entries["opex.repairs_maintenance.base"]["source"] == "deal:t12"

    insurance = _row(deal, "Insurance")
    assert insurance == {"category_name": "Insurance", "calculation_type": "per_unit", "base_value": 600.0,
                         "recoverable_flag": False, "growth_rate": float(house.OPEX_GROWTH_INSURANCE)}
    base = entries["opex.insurance.base"]
    assert base["source"] == "house:year1_insurance_floor"
    assert base["value"] == 600.0 * UNITS
    assert base["detail"]["per_unit_annual"] == 600.0
    assert base["review_required"] is True
    assert entries["insurance_per_unit"]["source"] == "house:year1_insurance_floor"
    assert entries["insurance_per_unit"]["value"] == 600.0

    fee = _row(deal, "Property Management Fee")
    assert (fee["calculation_type"], fee["base_value"], fee["growth_rate"]) == (
        "percent_egr", float(house.DEFAULT_MANAGEMENT_FEE_PCT_EGR), 0.0,
    )
    assert entries["opex.property_management_fee.base"]["source"] == "house:management_fee_pct_egr"
    assert entries["opex.property_management_fee.base"]["review_required"] is True
    assert validate_schema(deal) == []
    assert validate_deal(deal).status == "PASS"
    assert resolve_assumptions(deal) == deal


_TURNOVER_LABELS = {"Turnover - Paint", "Turnover - Carpet Cleaning", "Turnover - Cleaning"}


def test_missing_r_and_m_uses_the_vintage_framework(tmp_path):
    # No R&M and no turnover line: the T12 carries nothing on the R&M floor basis.
    t12 = _t12_without(tmp_path, _CORE_LABELS | {"Water Extraction"} | _TURNOVER_LABELS)
    deal = _ingest(t12=t12, year_built=1984)
    row = _row(deal, "Repairs & Maintenance")
    per_unit = float(house.default_r_and_m_per_unit(1984, strategy="cashflow"))

    assert (row["calculation_type"], row["base_value"]) == ("per_unit", per_unit)
    entries = _entries(deal)
    assert entries["opex.repairs_maintenance.base"]["source"] == "house:vintage_1980s"
    assert entries["r_and_m_per_unit"]["source"] == "house:vintage_1980s"
    assert entries["r_and_m_per_unit"]["value"] == per_unit
    assert resolve_assumptions(deal) == deal

    unknown_vintage = _ingest(t12=t12)
    assert not [r for r in unknown_vintage["opex_table"] if r["category_name"] == "Repairs & Maintenance"]


def test_turnover_lines_count_as_r_and_m_so_no_house_line_is_added(tmp_path):
    """A T12 that books turns on their own lines carries R&M on the floor basis."""
    t12 = _t12_without(tmp_path, _CORE_LABELS | {"Water Extraction"})
    deal = _ingest(t12=t12, year_built=1984)
    assert not [r for r in deal["opex_table"] if r["category_name"] == "Repairs & Maintenance"]
    turnover = _row(deal, "Turnover / Make-Ready")
    entry = _entries(deal)["r_and_m_per_unit"]
    assert entry["source"] == "deal:t12"
    assert entry["value"] == pytest.approx(turnover["base_value"] / UNITS)
    assert "guardrail" not in entry  # $8,100/unit of turns is above the $900 floor


def test_fill_never_double_counts_a_differently_named_line(ingested):
    deal = copy.deepcopy(ingested)
    deal.pop("assumptions")
    renames = {"Insurance": "Hazard Insurance Premium", "Property Management Fee": "Mgmt Services",
               "Repairs & Maintenance": "Building Repairs"}
    for row in deal["opex_table"]:
        row["category_name"] = renames.get(row["category_name"], row["category_name"])
        row.pop("growth_rate", None)
    resolved = resolve_assumptions(deal)

    assert len(resolved["opex_table"]) == len(deal["opex_table"])
    assert [r["category_name"] for r in resolved["opex_table"]] == [r["category_name"] for r in deal["opex_table"]]
    assert len(resolve_assumptions(ingested)["opex_table"]) == 10


# ── 6. provenance ────────────────────────────────────────────────────────


def test_base_entries_carry_t12_source_rows_and_per_unit_amounts(ingested):
    entry = _entries(ingested)["opex.payroll.base"]
    rows = ["Payroll - Office", "Payroll - Leasing", "Payroll - Maintenance"]

    assert entry["source"] == "deal:t12"
    assert entry["value"] == 810_000.0
    assert entry["unit"] == "usd_per_year"
    assert entry["review_required"] is False
    assert entry["detail"] == {
        "category_name": "Payroll", "calculation_type": "fixed_annual", "base_value": 810_000.0,
        "annual_amount": 810_000.0, "per_unit_annual": 81_000.0, "source_rows": rows,
    }
    assert entry["candidates"][0]["source_rows"] == rows
    assert all(row in entry["basis"] for row in rows)
    assert "$81,000.00 per unit" in entry["basis"]
    lines = ingested["metadata"]["ingest_gate"]["evidence"]["t12"]["opex_lines"]
    assert lines["Payroll"]["source_rows"] == rows
    assert set(lines) == {row["category_name"] for row in ingested["opex_table"]}


def test_base_is_annualized_from_any_calculation_type(minimal_deal_inputs):
    entries = _entries(resolve_assumptions(minimal_deal_inputs))

    utilities = entries["opex.utilities.base"]  # per_unit_monthly 85 x 100 units x 12
    assert utilities["value"] == pytest.approx(102_000.0)
    assert utilities["detail"]["per_unit_annual"] == pytest.approx(1_020.0)
    assert entries["opex.r_m.base"]["value"] == pytest.approx(75_000.0)
    assert entries["opex.r_m.base"]["source"] == "analyst"


def test_row_source_rows_are_shown_when_present(minimal_deal_inputs):
    deal = copy.deepcopy(minimal_deal_inputs)
    _row(deal, "Insurance")["source_rows"] = ["Property Insurance", "Umbrella Policy"]
    entry = _entries(resolve_assumptions(deal))["opex.insurance.base"]
    assert entry["detail"]["source_rows"] == ["Property Insurance", "Umbrella Policy"]


def test_edited_t12_line_becomes_an_analyst_base(ingested):
    deal = copy.deepcopy(ingested)
    _row(deal, "Payroll")["base_value"] = 900_000.0
    entry = _entries(resolve_assumptions(deal))["opex.payroll.base"]
    assert entry["source"] == "analyst"
    assert entry["default_value"] == 810_000.0


# ── 7. determinism ───────────────────────────────────────────────────────


def test_resolution_is_deterministic_and_idempotent(ingested):
    again = _ingest(year_built=1984, **ECONOMICS)
    assert json.dumps(again, sort_keys=True) == json.dumps(ingested, sort_keys=True)
    assert resolve_assumptions(ingested) == ingested
    assert json.loads(json.dumps(ingested)) == ingested
    assert validate_schema(ingested) == []
    for key, entry in _entries(ingested).items():
        assert AssumptionEntry.from_dict(entry).to_dict() == entry, key
    keys = list(_entries(ingested))
    assert keys[-2:] == ["opex.property_management_fee.base", "opex.property_management_fee.growth"]


def test_duplicate_line_names_get_distinct_slugs():
    assert opex_line_slugs(
        [{"category_name": "Utilities"}, {"category_name": "utilities"}, {"category_name": "Fuel (Gas & Oil)"}]
    ) == ["utilities", "utilities_2", "fuel_gas_oil"]


# ── 8. end to end ────────────────────────────────────────────────────────


def test_ingest_and_run_underwriting_grows_opex_by_category(ingested):
    deal = _with_tax_policy(ingested)
    assert validate_deal(deal).status == "PASS"
    after = run_underwriting(deal)

    flat = copy.deepcopy(deal)
    for row in flat["opex_table"]:
        row.pop("growth_rate", None)
    before = run_underwriting(flat)

    by_category = {}
    for row in after["opex"]["by_category_by_month"]:
        by_category.setdefault(row["category"], []).append(row["expense"])
    assert by_category["Payroll"][48] == pytest.approx(810_000 / 12 * 1.035**4, abs=0.01)
    assert by_category["Insurance"][48] == pytest.approx(252_000 / 12 * 1.05**4, abs=0.01)
    # Taxes: the reassessed year-1 amount, then the house 3% from year 2.
    assert by_category["Real Estate Taxes"][48] == pytest.approx(by_category["Real Estate Taxes"][0] * 1.03**4)
    # Year 5 (months 49-60) of every dollar line is four growth steps from its year-1 amount.
    fixed = [row for row in deal["opex_table"] if row["calculation_type"] == "fixed_annual"]
    year1 = {name: sum(values[:12]) for name, values in by_category.items()}
    expected = sum(year1[row["category_name"]] * (1 + row.get("growth_rate", 0.0)) ** 4 for row in fixed)
    actual = sum(sum(by_category[row["category_name"]][48:60]) for row in fixed)
    assert actual == pytest.approx(expected, abs=1)
    assert sum(sum(r["expense"] for r in before["opex"]["by_category_by_month"]
                   if r["category"] == row["category_name"] and r["month"] >= "2030-08")
               for row in fixed) == pytest.approx(sum(year1[row["category_name"]] for row in fixed), abs=1)
    assert after["metrics"]["noi"]["year_1_noi"] == before["metrics"]["noi"]["year_1_noi"]


# ── 9. opex base overrides, suppressions and the management fee ─────────


def test_base_override_is_annual_dollars_written_in_the_row_calculation_type(minimal_deal_inputs):
    resolved = resolve_assumptions(minimal_deal_inputs, overrides={
        "opex.r_m.base": {"value": 90_000, "note": "bid"},  # per_unit row, 100 units
        "opex.utilities.base": 120_000,  # per_unit_monthly row
        "opex.management_fee.base": 0.035,  # percent_egr: the percent
    })
    entries = _entries(resolved)

    assert _row(resolved, "R&M")["base_value"] == 900.0
    assert _row(resolved, "Utilities")["base_value"] == pytest.approx(100.0)
    assert _row(resolved, "Management Fee")["base_value"] == 0.035
    base = entries["opex.r_m.base"]
    assert (base["source"], base["value"], base["override"]) == ("analyst", 90_000, {"value": 90_000, "note": "bid"})
    assert base["detail"]["per_unit_annual"] == 900.0
    # The v1 R&M key follows the line it reads.
    assert entries["r_and_m_per_unit"]["value"] == 900.0
    assert resolve_assumptions(resolved) == resolved


def test_ingested_management_fee_is_a_percent_of_t12_egi(ingested):
    row = _row(ingested, "Property Management Fee")
    entry = _entries(ingested)["opex.property_management_fee.base"]

    assert (row["calculation_type"], row["base_value"]) == ("percent_egr", round(180_000 / 6_381_000, 6))
    assert entry["source"] == "deal:t12" and entry["review_required"] is False
    assert entry["unit"] == "decimal_of_egr"
    assert entry["detail"]["t12_annual_amount"] == 180_000.0
    assert entry["detail"]["t12_egi"] == 6_381_000.0
    assert "$180,000.00" in entry["basis"] and "$6,381,000.00" in entry["basis"]
    # The T12 snapshot keeps the dollar line.
    assert ingested["metadata"]["ingest_gate"]["evidence"]["t12"]["opex_lines"]["Property Management Fee"][
        "annual_amount"] == 180_000.0


def test_management_fee_falls_back_to_the_house_percent_without_egi(ingested):
    deal = copy.deepcopy(ingested)
    deal.pop("assumptions")
    deal["metadata"]["ingest_gate"].pop("t12_reconciliation")
    _row(deal, "Property Management Fee").update(calculation_type="fixed_annual", base_value=180_000.0)
    resolved = resolve_assumptions(deal)
    entry = _entries(resolved)["opex.property_management_fee.base"]

    assert _row(resolved, "Property Management Fee")["base_value"] == float(house.DEFAULT_MANAGEMENT_FEE_PCT_EGR)
    assert (entry["source"], entry["review_required"]) == ("house:management_fee_pct_egr", True)
    assert entry["detail"]["t12_annual_amount"] == 180_000.0


def test_analyst_dollar_management_fee_is_not_converted(ingested):
    deal = copy.deepcopy(ingested)
    _row(deal, "Property Management Fee").update(calculation_type="fixed_annual", base_value=200_000.0)
    resolved = resolve_assumptions(deal)
    assert _row(resolved, "Property Management Fee")["calculation_type"] == "fixed_annual"
    assert _entries(resolved)["opex.property_management_fee.base"]["source"] == "analyst"


def _no_insurance(tmp_path):
    return _ingest(t12=_t12_without(tmp_path, {"Property Insurance"}), year_built=1984)


def test_zero_override_suppresses_a_house_line_and_null_restores_it(tmp_path):
    deal = _no_insurance(tmp_path)
    assert _entries(deal)["opex.insurance.base"]["source"] == "house:year1_insurance_floor"

    suppressed = resolve_assumptions(deal, overrides={"opex.insurance.base": {"value": 0, "note": "self-insured"}})
    rows = [r for r in suppressed["opex_table"] if r["category_name"] == "Insurance"]
    assert len(rows) == 1 and rows[0]["base_value"] == 0.0  # zeroed, never re-added
    entry = _entries(suppressed)["opex.insurance.base"]
    assert entry["source"] == "analyst" and entry["value"] == 0
    assert "Suppressed by the analyst" in entry["basis"]
    assert _entries(suppressed)["insurance_per_unit"]["value"] == 0.0
    assert resolve_assumptions(suppressed) == suppressed

    restored = resolve_assumptions(suppressed, overrides={"opex.insurance.base": None})
    assert _row(restored, "Insurance")["base_value"] == 600.0
    entries = _entries(restored)
    assert entries["opex.insurance.base"]["source"] == "house:year1_insurance_floor"
    assert entries["opex.insurance.base"]["override"] is None
    assert entries["insurance_per_unit"]["source"] == "house:year1_insurance_floor"
    assert entries["insurance_per_unit"]["override"] is None
    assert {k: v for k, v in restored.items() if k != "assumptions"} == {
        k: v for k, v in deal.items() if k != "assumptions"
    }


def test_explicitly_deleted_t12_line_stays_deleted(ingested):
    deal = copy.deepcopy(ingested)
    deal["opex_table"] = [r for r in deal["opex_table"] if r["category_name"] != "Insurance"]
    resolved = resolve_assumptions(deal)

    rows = [r for r in resolved["opex_table"] if "insurance" in r["category_name"].lower()]
    assert len(rows) == 1 and rows[0]["base_value"] == 0.0  # no house Insurance line re-added
    entries = _entries(resolved)
    assert entries["opex.insurance.base"]["source"] == "analyst"
    assert "deleted from the opex_table" in entries["opex.insurance.base"]["override"]["note"]
    assert entries["insurance_per_unit"]["source"] == "analyst"
    assert entries["insurance_per_unit"]["value"] == 0.0
    assert resolve_assumptions(resolved) == resolved


def test_re_added_house_line_is_never_labelled_analyst(ingested):
    deal = copy.deepcopy(ingested)
    deal.pop("assumptions")  # no record of the line: the house default fills the gap
    deal["opex_table"] = [r for r in deal["opex_table"] if r["category_name"] != "Property Management Fee"]
    resolved = resolve_assumptions(deal)
    entry = _entries(resolved)["opex.property_management_fee.base"]

    assert entry["source"] == "house:management_fee_pct_egr"
    assert entry["override"] is None and entry["review_required"] is True
    assert resolve_assumptions(resolved) == resolved
