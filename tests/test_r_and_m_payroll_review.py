"""The R&M floor's payroll review (ADR 0003 §5).

The R&M floor compares R&M plus turnover / make-ready with the vintage floor
and tops up the shortfall. Payroll is payroll: it earns no credit toward the
floor by default. Only evidence that maintenance labor is booked in payroll
beyond normal earns a credit: payroll lines labeled as maintenance labor AND
total payroll per unit above the house benchmark. The credit is the lesser of
the labeled maintenance payroll and the payroll above the benchmark. The
review is always disclosed (``r_and_m_payroll_credit``), flagged for review
when a shortfall exists and payroll is above the benchmark, and the analyst
can override the credit.

All deals and figures are synthetic.
"""
from __future__ import annotations

import copy
import json
from decimal import Decimal
from functools import lru_cache

import pytest

from engine import underwriting_policy as house
from engine.assumptions import resolve_assumptions
from engine.assumptions import guardrails
from engine.assumptions.guardrails import GuardrailProfile
from engine.assumptions.rationale import rationale_for
from engine.assumptions.register import build_register
from engine.ingest.t12_parser import parse_t12_with_provenance
from engine.validator import validate_deal
from tests import deal_library as dl

YEAR = 1984  # 1980s: R&M floor $900/unit (stabilized)
UNITS = 100  # the conftest deal
BENCHMARK = house.HOUSE_PAYROLL_BENCHMARK_PER_UNIT
SOURCE = "house:r_and_m_payroll_review"


def _deal(minimal_deal_inputs, payroll_lines, r_and_m_per_unit=300.0):
    deal = copy.deepcopy(minimal_deal_inputs)
    deal["metadata"]["year_built"] = YEAR
    rows = [r for r in deal["opex_table"] if r["category_name"] != "R&M"]
    rows.append({"category_name": "Repairs & Maintenance", "calculation_type": "per_unit",
                 "base_value": r_and_m_per_unit, "recoverable_flag": False})
    for label, annual in payroll_lines:
        rows.append({"category_name": label, "calculation_type": "fixed_annual",
                     "base_value": annual, "recoverable_flag": False})
    deal["opex_table"] = rows
    return deal


def _entries(canonical):
    return canonical["assumptions"]["entries"]


def _price(deal):
    """Pricing-mode R&M floor on the canonical: (findings, top-up line annual)."""
    priced = copy.deepcopy(deal)
    overrides: list = []
    findings = guardrails.enforce_floors(
        priced, mode="pricing", profile=GuardrailProfile("cashflow", YEAR),
        context={"policy_overrides": overrides}, keys=("r_and_m_per_unit",))
    top_up = sum(r["base_value"] for r in priced["opex_table"]
                 if r["category_name"] == guardrails.R_AND_M_TOP_UP_CATEGORY)
    return findings, top_up, overrides


def test_the_benchmark_is_a_named_house_constant_with_a_rationale():
    assert BENCHMARK == house.HOUSE_PAYROLL_BENCHMARK_PER_UNIT > 0
    assert rationale_for(SOURCE)["known"]


def test_payroll_at_the_benchmark_earns_no_credit_and_the_full_shortfall_is_topped_up(minimal_deal_inputs):
    payroll = float(BENCHMARK) * UNITS
    deal = resolve_assumptions(_deal(minimal_deal_inputs, [
        ("Payroll - Office", payroll / 2), ("Payroll - Maintenance", payroll / 2)]))
    entry = _entries(deal)["r_and_m_payroll_credit"]
    review = entry["detail"]
    assert (entry["source"], entry["value"], entry["review_required"]) == (SOURCE, 0.0, False)
    assert review["payroll_per_unit"] == float(BENCHMARK)
    assert review["benchmark_per_unit"] == float(BENCHMARK)
    assert review["payroll_above_benchmark_annual"] == 0.0
    assert review["maintenance_payroll_annual"] == payroll / 2
    assert review["maintenance_payroll_lines"] == [{"label": "Payroll - Maintenance", "annual": payroll / 2}]
    assert review["credit_annual"] == 0.0
    assert review["r_and_m_basis_annual"] == 30_000.0 and review["floor_annual"] == 90_000.0
    assert review["top_up_annual"] == 60_000.0
    findings, top_up, _ = _price(deal)
    assert top_up == 60_000.0
    assert findings[0]["payroll_review"]["credit_annual"] == 0.0


def test_labeled_maintenance_payroll_well_above_the_benchmark_earns_a_partial_credit(minimal_deal_inputs):
    # $2,500/unit of payroll: $500/unit above a $2,000 benchmark; $150,000 is labeled maintenance.
    office, maintenance = 100_000.0, 150_000.0
    deal = resolve_assumptions(_deal(minimal_deal_inputs, [
        ("Payroll - Office", office), ("Maintenance Wages", maintenance)]))
    entry = _entries(deal)["r_and_m_payroll_credit"]
    review = entry["detail"]
    above = office + maintenance - float(BENCHMARK) * UNITS
    credit = min(maintenance, above)
    assert 0 < credit < maintenance
    assert (entry["source"], entry["value"]) == (SOURCE, credit / UNITS)
    assert review["credit_annual"] == credit
    assert review["top_up_annual"] == 60_000.0 - credit
    # A shortfall with payroll above the benchmark is ambiguous: the analyst confirms it.
    assert entry["review_required"] is True and review["review_required"] is True
    assert any(i.path == "/assumptions/entries/r_and_m_payroll_credit"
               for i in validate_deal(deal).issues if i.code == "ASSUMPTION_REVIEW_REQUIRED")
    # The underwrite warning still fires (R&M + credit is below the floor) and carries the review.
    record = _entries(deal)["r_and_m_per_unit"]["guardrail"]
    assert record["action"] == "warned" and "payroll" in record["basis"]
    findings, top_up, overrides = _price(deal)
    assert top_up == 60_000.0 - credit
    assert findings[0]["payroll_review"]["credit_annual"] == credit
    assert overrides[0]["payroll_review"]["credit_annual"] == credit


def test_a_credit_that_covers_the_shortfall_clears_the_floor(minimal_deal_inputs):
    deal = resolve_assumptions(_deal(minimal_deal_inputs, [
        ("Payroll - Office", 100_000.0), ("Maintenance Technician Payroll", 200_000.0)]))
    review = _entries(deal)["r_and_m_payroll_credit"]["detail"]
    assert review["credit_annual"] == 100_000.0 and review["top_up_annual"] == 0.0
    assert "guardrail" not in _entries(deal)["r_and_m_per_unit"]
    findings, top_up, _ = _price(deal)
    assert findings == [] and top_up == 0


def test_high_payroll_without_maintenance_labels_earns_no_credit_and_is_flagged(minimal_deal_inputs):
    deal = resolve_assumptions(_deal(minimal_deal_inputs, [("Payroll", 300_000.0)]))
    entry = _entries(deal)["r_and_m_payroll_credit"]
    review = entry["detail"]
    assert review["payroll_above_benchmark_annual"] == 300_000.0 - float(BENCHMARK) * UNITS
    assert review["maintenance_payroll_annual"] == 0.0 and review["maintenance_payroll_lines"] == []
    assert (entry["value"], review["top_up_annual"]) == (0.0, 60_000.0)
    assert entry["review_required"] is True
    findings, top_up, _ = _price(deal)
    assert top_up == 60_000.0


def test_payroll_lines_never_count_toward_the_r_and_m_basis(minimal_deal_inputs):
    deal = resolve_assumptions(_deal(minimal_deal_inputs, [("Repairs Salaries", 50_000.0)]))
    review = _entries(deal)["r_and_m_payroll_credit"]["detail"]
    assert review["r_and_m_basis_annual"] == 30_000.0
    assert review["maintenance_payroll_annual"] == 50_000.0  # labeled, but payroll is below the benchmark
    assert review["credit_annual"] == 0.0 and _entries(deal)["r_and_m_payroll_credit"]["review_required"] is False


def test_the_analyst_can_override_the_credit(minimal_deal_inputs):
    base = _deal(minimal_deal_inputs, [("Payroll", 300_000.0)])
    deal = resolve_assumptions(base, overrides={"r_and_m_payroll_credit": 400})
    entry = _entries(deal)["r_and_m_payroll_credit"]
    assert (entry["source"], entry["value"], entry["default_value"]) == ("analyst", 400.0, 0.0)
    assert entry["review_required"] is False
    assert entry["detail"]["credit_annual"] == 40_000.0 and entry["detail"]["credit_source"] == "analyst"
    assert entry["detail"]["top_up_annual"] == 20_000.0
    findings, top_up, _ = _price(deal)
    assert top_up == 20_000.0
    assert findings[0]["payroll_review"]["credit_source"] == "analyst"
    # Clearing the override restores the house review.
    cleared = resolve_assumptions(deal, overrides={"r_and_m_payroll_credit": None})
    assert _entries(cleared)["r_and_m_payroll_credit"]["source"] == SOURCE
    again = resolve_assumptions(deal)
    assert _entries(again)["r_and_m_payroll_credit"] == entry
    assert resolve_assumptions(again) == again


def test_the_register_shows_the_review(minimal_deal_inputs):
    deal = resolve_assumptions(_deal(minimal_deal_inputs, [
        ("Payroll - Office", 100_000.0), ("Maintenance Wages", 150_000.0)]))
    rows = {row["key"]: row for row in build_register(deal)["rows"]}
    row = rows["r_and_m_payroll_credit"]
    assert row["group"] == rows["r_and_m_per_unit"]["group"]
    assert row["why"]["rationale_known"]
    assert row["extra"]["detail"]["maintenance_payroll_annual"] == 150_000.0


# ── T12 evidence: per-line payroll amounts ───────────────────────────────


def test_the_t12_parser_keeps_each_payroll_line_amount():
    path = dl.DEALS_DIR / "a_stabilized_1990s" / "t12.csv"
    _, provenance = parse_t12_with_provenance(path)
    lines = provenance["_line_amounts"]["Payroll"]
    assert [(line["label"], line["annual"]) for line in lines] == [
        ("Payroll - Office", 104_000.0), ("Payroll - Leasing", 76_000.0), ("Payroll - Maintenance", 120_000.0)]


@lru_cache(maxsize=None)
def _library(slug: str) -> str:
    return json.dumps(dl.ingest(dl.load_deal(slug)))


@pytest.mark.parametrize("slug", dl.DEAL_SLUGS)
def test_library_deals_disclose_the_review_from_the_t12_lines(slug):
    canonical = json.loads(_library(slug))
    entry = _entries(canonical)["r_and_m_payroll_credit"]
    review = entry["detail"]
    units = sum(c["unit_count"] for c in canonical["unit_cohorts"])
    evidence = canonical["metadata"]["ingest_gate"]["evidence"]["t12"]["opex_lines"]["Payroll"]
    assert review["payroll_annual"] == pytest.approx(evidence["annual_amount"])
    assert review["payroll_per_unit"] == pytest.approx(evidence["annual_amount"] / units)
    labeled = [line for line in evidence["source_lines"] if "maint" in line["label"].lower()]
    assert review["maintenance_payroll_annual"] == pytest.approx(sum(line["annual"] for line in labeled))
    # Library payroll is at or below the benchmark: payroll is payroll, no credit.
    assert review["payroll_per_unit"] <= float(BENCHMARK)
    assert entry["value"] == 0.0 and entry["review_required"] is False


# ── Yardi ysi_is: a payroll section read line by line ────────────────────
#
# A Yardi statement itemizes its PAYROLL EXPENSE section (maintenance wages,
# office wages, bonuses, taxes, benefits, fees) and closes it with a total.
# The section is taken whole as Payroll, but each line keeps its own annual
# amount, so the review reads the labeled maintenance payroll from it.


def _yardi_payroll_deal(tmp_path, payroll_lines=None):
    from engine.ingest.ingestion_pipeline import build_deal_from_documents
    from tests import yardi_fixtures as yf

    sections = copy.deepcopy(yf.T12_OPEX)
    if payroll_lines is not None:
        [payroll] = [s for s in sections if s["header"].strip() == "PAYROLL EXPENSE"]
        payroll["lines"] = list(payroll_lines)
    deal = build_deal_from_documents(
        property_id="Synthetic Yardi Deal",
        rent_roll_path=yf.write_yardi_rent_roll(tmp_path / "rent_roll.xlsx"),
        t12_path=yf.write_yardi_t12(tmp_path / "ysi_is.xlsx", sections),
        analysis_start="2027-02",
        analysis_end="2032-01",
        rent_growth_rate=0.025,
        collection_loss_rate=0.01,
        year_built=YEAR,
    )
    return deal, len(yf.RR_UNITS)


def _payroll_evidence(deal):
    return deal["metadata"]["ingest_gate"]["evidence"]["t12"]["opex_lines"]["Payroll"]


def test_a_yardi_payroll_section_above_the_benchmark_credits_its_labeled_maintenance_lines(tmp_path):
    from tests.yardi_fixtures import t12_line, t12_section

    deal, units = _yardi_payroll_deal(tmp_path)
    payroll = t12_section("TOTAL PAYROLL EXPENSE")
    maintenance = t12_line("Repairs Salaries") + t12_line("Temporary Help-Maint")
    assert payroll / units > float(BENCHMARK)

    # Opex and the reconciliation gate are untouched: Payroll is the section total, once.
    assert deal["metadata"]["ingest_gate"]["t12_reconciliation"]["status"] == "PASS"
    [row] = [r for r in deal["opex_table"] if r["category_name"] == "Payroll"]
    assert row["base_value"] == pytest.approx(payroll)
    assert not any(r["category_name"] == "Repairs & Maintenance" and r["base_value"] >= t12_line("Repairs Salaries")
                   for r in deal["opex_table"])
    evidence = _payroll_evidence(deal)
    assert evidence["annual_amount"] == pytest.approx(payroll)
    lines = {line["label"]: line["annual"] for line in evidence["source_lines"]}
    assert lines["Manager Salaries"] == pytest.approx(t12_line("Manager Salaries"))
    assert lines["Repairs Salaries"] == pytest.approx(t12_line("Repairs Salaries"))
    assert sum(lines.values()) == pytest.approx(payroll)

    review = guardrails.r_and_m_payroll_review(deal, Decimal("900"))
    assert review["payroll_annual"] == pytest.approx(payroll)
    assert review["maintenance_payroll_lines"] == [
        {"label": "Repairs Salaries", "annual": pytest.approx(t12_line("Repairs Salaries"))},
        {"label": "Temporary Help-Maint", "annual": pytest.approx(t12_line("Temporary Help-Maint"))},
    ]
    assert review["maintenance_payroll_annual"] == pytest.approx(maintenance)
    assert review["unseparated_payroll_sections"] == []
    above = payroll - float(BENCHMARK) * units
    assert maintenance < above
    assert review["house_credit_annual"] == pytest.approx(min(maintenance, above))


def test_a_yardi_payroll_section_below_the_benchmark_lists_its_maintenance_lines_and_earns_no_credit(tmp_path):
    payroll_lines = [
        ("Manager Salaries", 9_000.00),
        ("Repairs Salaries", 8_400.00),
        ("Bonuses", 300.00),
        ("Payroll /Unemployment Tax", 1_150.00),
        ("Workers Compensation Insurance", 240.00),
        ("Health/Life Insurance", 2_200.00),
        ("Temporary Help-Office", 450.00),
        ("Temporary Help-Maint", 600.00),
        ("Payroll Fees", 180.00),
    ]
    deal, units = _yardi_payroll_deal(tmp_path, payroll_lines)
    payroll = sum(amount for _, amount in payroll_lines)
    assert payroll / units < float(BENCHMARK)
    assert deal["metadata"]["ingest_gate"]["t12_reconciliation"]["status"] == "PASS"

    review = guardrails.r_and_m_payroll_review(deal, Decimal("900"))
    assert review["payroll_annual"] == pytest.approx(payroll)
    assert review["maintenance_payroll_lines"] == [
        {"label": "Repairs Salaries", "annual": 8_400.0},
        {"label": "Temporary Help-Maint", "annual": 600.0},
    ]
    assert review["maintenance_payroll_annual"] == pytest.approx(9_000.0)
    assert review["unseparated_payroll_sections"] == []
    assert review["payroll_above_benchmark_annual"] == 0.0
    assert review["house_credit_annual"] == 0.0
