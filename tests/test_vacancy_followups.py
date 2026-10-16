"""Stabilized vacancy follow-ups (ADR 0003 decision p, UPL-109).

1. Trailing actuals typed into backsolve, and broker-OM trailing actuals the
   OM labels as actuals, feed the asset-vs-market gap when the deal has no
   ingested T12 vacancy, at a lower confidence. A broker pro forma, or an OM
   column the parser could not confirm as actuals, never does: the build says
   why.
2. An analyst-supplied submarket vacancy benchmark is the market base, ahead
   of the market-data feeder, and survives backsolve's re-resolution.
3. On a value-add deal the vintage vacancy floor applies only after the
   renovations finish; the renovation window is governed by the build.

All data here is synthetic.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

import pytest

from engine.assumptions import resolve_assumptions
from engine.assumptions.guardrails import (
    GuardrailProfile,
    apply_guardrails,
    enforce_floors,
    house_floors,
    vacancy_floor_window,
)
from engine.assumptions.register import build_register
from engine.backsolve import backsolve_price
from engine.ingest import broker_om_snapshot
from engine.ingest.ingestion_pipeline import build_deal_from_documents
from engine.market_data import Geo, MarketDataRegistry, SyntheticProvider
from engine.workflow.fields import BY_NAME
from tests import deal_library as dl

FIXTURES = Path(__file__).parent / "fixtures"
T12 = FIXTURES / "entrata_style_t12.csv"  # GPR $6,000,000, vacancy loss $375,000 (6.25%)
ROLL = FIXTURES / "sample_rent_roll.csv"  # 10 units, 2 vacant (20%)
REPO_ROOT = Path(__file__).resolve().parent.parent

MSA = Geo(metro_slug="synthetic_metro")
SUB = Geo(metro_slug="synthetic_metro", submarket="Synthetic North")
BASE = {("vacancy_stabilized", SUB): 0.06, ("vacancy_stabilized", MSA): 0.065}
BENCHMARK = {"rate": "0.04", "as_of": "2026-10-01", "source": "synthetic:deal-library"}
POLICY = {"version": "plat.backsolve-policy/1"}
ANALYST_BENCHMARK = {"rate": "0.07", "as_of": "2026-09-30", "source": "synthetic broker survey, 3 Star"}
ACTUAL_LABEL = {"kind": "actual", "period": "T3 income / T12 expenses",
                "evidence": "column header 'Income T3 Income / T12 Expense Proforma'"}


def _registry(data=None):
    provider = SyntheticProvider(data or {}, as_of=date(2026, 6, 30), confidence="high")
    return MarketDataRegistry([provider])


@lru_cache(maxsize=None)
def _ingested() -> str:
    deal = build_deal_from_documents(
        property_id="Synthetic Vacancy", rent_roll_path=ROLL, t12_path=T12,
        analysis_start="2026-08", analysis_end="2031-07", year_built=1984,
        msa="Synthetic Metro", metro_slug="synthetic_metro", submarket="Synthetic North", market=None,
    )
    return json.dumps(deal)


def _deal(*, t12_vacancy=True, summary=None, om_evidence=None):
    """The ingested synthetic deal ready to re-resolve from scratch."""
    deal = json.loads(_ingested())
    deal.pop("assumptions", None)
    for row in deal["physical_vacancy_curve"]:
        row["vacancy_rate"] = None
    evidence = deal["metadata"]["ingest_gate"]["evidence"]
    if not t12_vacancy:
        evidence["t12"].pop("vacancy_loss", None)
    if om_evidence is not None:
        evidence["om_trailing_actuals"] = om_evidence
    if summary:
        deal["metadata"].setdefault("property_summary", {}).update(copy.deepcopy(summary))
    return deal


def _resolve(data=BASE, **kwargs):
    return resolve_assumptions(_deal(**kwargs), market=_registry(data) if data is not None else None)


def _entry(deal):
    return deal["assumptions"]["entries"]["physical_vacancy"]


def _build(deal):
    return _entry(deal)["detail"]["vacancy_build"]


def _gap(deal):
    return _build(deal)["asset_vs_market"]


TYPED = {"gross_potential_rent": 1_000_000.0, "vacancy_loss": -80_000.0}  # 8% vacancy
OM_ACTUALS = {"gross_potential_rent": 2_000_000.0, "vacancy_loss": 140_000.0}  # 7% vacancy


# ── 1. typed-in and OM trailing actuals feed the gap ─────────────────────


def test_typed_trailing_actuals_feed_the_gap_at_medium_confidence():
    deal = _resolve(t12_vacancy=False, summary={"trailing_actuals": TYPED})
    gap = _gap(deal)
    assert gap["available"] is True
    assert gap["asset_vacancy"] == pytest.approx(0.08)
    assert gap["asset_source"] == "deal:t12"
    assert gap["asset_source_kind"] == "typed_trailing_actuals"
    assert gap["asset_confidence"] == "medium"
    assert "typed" in gap["asset_vacancy_basis"]
    assert gap["value"] == pytest.approx(0.01)  # half of 8% - 6%
    assert _entry(deal)["value"]["stabilized"] == pytest.approx(0.07)
    # The build is only as confident as its weakest input.
    assert _entry(deal)["confidence"] == "medium"


def test_an_ingested_t12_outranks_typed_actuals():
    deal = _resolve(summary={"trailing_actuals": TYPED})
    gap = _gap(deal)
    assert gap["asset_source_kind"] == "ingested_t12"
    assert gap["asset_confidence"] == "high"
    assert gap["asset_vacancy"] == pytest.approx(0.0625)
    assert _entry(deal)["confidence"] == "high"
    skipped = {c["kind"]: c for c in gap["asset_sources_considered"]}
    assert skipped["typed_trailing_actuals"]["used"] is False


def test_om_trailing_actuals_labelled_actual_feed_the_gap_at_low_confidence():
    om = {"trailing_actuals": OM_ACTUALS, "trailing_actuals_basis": ACTUAL_LABEL}
    deal = _resolve(t12_vacancy=False, om_evidence=om)
    gap = _gap(deal)
    assert gap["available"] is True
    assert gap["asset_source"] == "deal:om"
    assert gap["asset_source_kind"] == "broker_om_actuals"
    assert gap["asset_confidence"] == "low"
    assert gap["asset_vacancy"] == pytest.approx(0.07)
    assert "T3 income / T12 expenses" in gap["asset_vacancy_basis"]
    assert _entry(deal)["confidence"] == "low"


def test_om_actuals_from_the_broker_snapshot_feed_the_gap():
    snapshot = {"trailing_actuals": OM_ACTUALS, "trailing_actuals_basis": ACTUAL_LABEL}
    deal = _resolve(t12_vacancy=False, summary={"broker_underwriting_snapshot": snapshot})
    assert _gap(deal)["asset_source_kind"] == "broker_om_actuals"
    assert _gap(deal)["asset_vacancy"] == pytest.approx(0.07)


@pytest.mark.parametrize("label, reason", [
    (None, "no actual-period label"),
    ({"kind": "unverified", "period": "trailing column read by position"}, "no actual-period label"),
    ({"kind": "actual", "period": "Year 1 Pro Forma"}, "pro forma"),
])
def test_om_columns_not_labelled_actual_never_feed_the_gap(label, reason):
    om = {"trailing_actuals": OM_ACTUALS, **({"trailing_actuals_basis": label} if label is not None else {})}
    deal = _resolve(t12_vacancy=False, om_evidence=om)
    gap = _gap(deal)
    assert gap["available"] is False
    assert gap["value"] == 0.0
    considered = {c["kind"]: c for c in gap["asset_sources_considered"]}
    assert considered["broker_om_actuals"]["used"] is False
    assert reason in considered["broker_om_actuals"]["reason"]
    assert reason in gap["note"]


def test_the_om_parser_labels_only_a_verified_actual_column():
    text = (
        "FINANCIALS - PROFORMA\n"
        "Income T3 Income / T12 Expense Proforma\n"
        "Gross Potential Rent (GPR) $1,000,000 $1,100,000\n"
        "Vacancy ($70,000) ($55,000)\n"
        "NOI $500,000 $600,000\n"
        "IMPORTANT INFORMATION\n"
    )
    trailing, forecast, notes = broker_om_snapshot._extract_income_statement(text)
    assert trailing["vacancy_loss"] == 70_000 and forecast["vacancy_loss"] == 55_000
    assert notes["trailing_actuals_basis"]["kind"] == "actual"
    assert "T3" in notes["trailing_actuals_basis"]["period"]


def test_ingest_records_the_om_trailing_actuals_and_their_label(monkeypatch, tmp_path):
    snapshot = {"trailing_actuals": {**OM_ACTUALS, "noi": 900_000.0}, "trailing_actuals_basis": ACTUAL_LABEL,
                "property_tax_evidence_candidates": [], "parser_metadata": {"parser_family": "layout_text_om_v1"}}
    monkeypatch.setattr("engine.ingest.ingestion_pipeline.parse_broker_snapshot_from_om", lambda path: snapshot)
    monkeypatch.setattr("engine.ingest.ingestion_pipeline.parse_om", lambda path, client=None: None)
    om = tmp_path / "om.txt"
    om.write_text("synthetic offering memorandum", encoding="utf-8")
    deal = build_deal_from_documents(
        property_id="Synthetic Vacancy", rent_roll_path=ROLL, t12_path=T12, om_path=om,
        analysis_start="2026-08", analysis_end="2031-07", year_built=1984, market=None,
    )
    recorded = deal["metadata"]["ingest_gate"]["evidence"]["om_trailing_actuals"]
    assert recorded["trailing_actuals"] == {"gross_potential_rent": 2_000_000.0, "vacancy_loss": 140_000.0}
    assert recorded["trailing_actuals_basis"] == ACTUAL_LABEL


# ── 2. the analyst submarket benchmark is the market base ────────────────


def test_the_analyst_benchmark_is_the_market_base_ahead_of_the_feeder():
    deal = _resolve(summary={"market_vacancy_benchmark": ANALYST_BENCHMARK})
    base = _build(deal)["market_base"]
    assert (base["value"], base["level"], base["source"]) == (0.07, "analyst", "analyst")
    assert base["as_of"] == "2026-09-30"
    assert base["source_note"] == "synthetic broker survey, 3 Star"
    # The feeder value it outranks stays disclosed.
    assert base["outranked"]["value"] == 0.06 and base["outranked"]["source"] == "market:synthetic"
    gap = _gap(deal)
    assert gap["market_comparator"] == 0.07  # the T12's 6.25% is compared with the benchmark
    assert _entry(deal)["value"]["stabilized"] == pytest.approx(0.07 + (0.0625 - 0.07) / 2)
    assert _entry(deal)["as_of"] == "2026-09-30"


def test_the_analyst_benchmark_is_the_base_without_market_data():
    deal = _resolve(None, summary={"market_vacancy_benchmark": ANALYST_BENCHMARK})
    assert _build(deal)["market_base"]["level"] == "analyst"
    assert _gap(deal)["available"] is True  # an analyst base is a market to compare with


def test_an_invalid_analyst_benchmark_is_ignored_and_disclosed():
    deal = _resolve(summary={"market_vacancy_benchmark": {"rate": "0.07"}})
    base = _build(deal)["market_base"]
    assert base["level"] == "submarket"
    assert "market_vacancy_benchmark" in base["analyst_benchmark_note"]


def test_the_analyst_base_survives_re_resolution():
    deal = _resolve(summary={"market_vacancy_benchmark": ANALYST_BENCHMARK})
    again = resolve_assumptions(copy.deepcopy(deal))
    assert _build(again)["market_base"]["level"] == "analyst"
    assert again["physical_vacancy_curve"] == deal["physical_vacancy_curve"]
    assert _entry(again)["override"] is None


def test_the_register_names_the_analyst_base_and_the_typed_t12():
    deal = _resolve(t12_vacancy=False, summary={"market_vacancy_benchmark": ANALYST_BENCHMARK,
                                                "trailing_actuals": TYPED})
    row = next(r for r in build_register(deal)["rows"] if r["key"] == "physical_vacancy")
    text = {line["label"]: line["text"] for line in row["breakdown"]}
    assert "analyst submarket benchmark" in text["Market base"]
    assert "typed trailing actuals" in text["Asset vs market"]


def test_ingest_takes_the_submarket_vacancy_benchmark():
    deal = build_deal_from_documents(
        property_id="Synthetic Vacancy", rent_roll_path=ROLL, t12_path=T12,
        analysis_start="2026-08", analysis_end="2031-07", year_built=1984,
        msa="Synthetic Metro", metro_slug="synthetic_metro", submarket="Synthetic North",
        market=_registry(BASE), market_vacancy_benchmark=ANALYST_BENCHMARK,
    )
    assert deal["metadata"]["property_summary"]["market_vacancy_benchmark"] == ANALYST_BENCHMARK
    assert _build(deal)["market_base"]["level"] == "analyst"


def test_ingest_refuses_a_malformed_benchmark():
    with pytest.raises(ValueError):
        build_deal_from_documents(
            property_id="Synthetic Vacancy", rent_roll_path=ROLL, t12_path=T12,
            analysis_start="2026-08", analysis_end="2031-07", market=None,
            market_vacancy_benchmark={"rate": "0.07", "as_of": "not a date", "source": "x"},
        )


def test_the_cli_and_describe_deal_take_the_submarket_vacancy():
    sys.path.insert(0, str(REPO_ROOT))
    from runs.ingest_deal import build_parser

    args = build_parser().parse_args([
        "--rent-roll", "r.csv", "--t12", "t.csv", "--start", "2026-08", "--end", "2031-07", "--output", "o.json",
        "--submarket-vacancy", "0.07", "--submarket-vacancy-as-of", "2026-09-30",
        "--submarket-vacancy-source", "synthetic survey",
    ])
    assert (args.submarket_vacancy, args.submarket_vacancy_as_of, args.submarket_vacancy_source) == (
        0.07, "2026-09-30", "synthetic survey")
    for name in ("submarket_vacancy", "submarket_vacancy_as_of", "submarket_vacancy_source"):
        assert name in BY_NAME
    assert "market base" in BY_NAME["submarket_vacancy"].why
    assert BY_NAME["submarket_vacancy"].check(0.07) == 0.07
    with pytest.raises(ValueError):
        BY_NAME["submarket_vacancy"].check(7)


def test_the_workflow_passes_the_benchmark_to_ingest():
    from engine.workflow.service import _market_vacancy_benchmark

    profile = {"submarket_vacancy": 0.07, "submarket_vacancy_as_of": "2026-09-30",
               "submarket_vacancy_source": "synthetic survey"}
    assert _market_vacancy_benchmark(profile) == {"rate": "0.07", "as_of": "2026-09-30",
                                                  "source": "synthetic survey"}
    assert _market_vacancy_benchmark({}) is None
    assert _market_vacancy_benchmark({"submarket_vacancy": 0.07})["source"] == "describe_deal:submarket_vacancy"


# ── backsolve: typed actuals and the analyst base reach the priced curve ──


A = "a_stabilized_1990s"
C = "c_value_add_1970s_reno"


@lru_cache(maxsize=None)
def _library(slug: str) -> str:
    return json.dumps(dl.ingest(dl.load_deal(slug)))


def _no_t12_vacancy(slug=A):
    """Library deal a re-resolved with market data but without the T12 vacancy line."""
    canonical = json.loads(_library(slug))
    canonical["metadata"]["ingest_gate"]["evidence"]["t12"].pop("vacancy_loss", None)
    return resolve_assumptions(canonical, market=dl.synthetic_registry(dl.load_deal(slug)))


def _solve(canonical, **kwargs):
    return backsolve_price(copy.deepcopy(canonical), target_coc="0.06", policy={**POLICY, "strategy": "cashflow"},
                           benchmark=BENCHMARK, min_price="1000000.00", max_price="40000000.00", **kwargs)


def _vacancy_months(case):
    return {(r["cohort_id"], r["start_period"]): r["vacancy_rate"] for r in case["physical_vacancy_curve"]}


def test_backsolve_trailing_actuals_reprice_the_stabilized_vacancy():
    canonical = _no_t12_vacancy()
    assert _gap(canonical)["available"] is False
    plain = _solve(canonical)
    typed = {"gross_potential_rent": 1_000_000.0, "vacancy_loss": -90_000.0}
    result = _solve(canonical, trailing_actuals=typed)
    gap = _gap(result["case"])
    assert gap["available"] is True and gap["asset_source_kind"] == "typed_trailing_actuals"
    assert gap["asset_confidence"] == "medium"
    rebuilt = result["summary"]["policy_summary"]["physical_vacancy_build_repriced"]
    assert rebuilt["rebuilt"] is True
    assert rebuilt["stabilized"] > rebuilt["previous_stabilized"]
    assert result["case"]["metadata"]["property_summary"]["trailing_actuals"] == typed
    # Higher vacancy, lower price.
    assert result["summary"]["solved_purchase_price"] < plain["summary"]["solved_purchase_price"]
    # The priced curve is the rebuilt build, and the case is stable on re-resolve.
    case = result["case"]
    assert resolve_assumptions(case, mode="pricing") == case


def test_backsolve_without_new_evidence_keeps_the_ingested_build():
    canonical = json.loads(_library(A))
    result = _solve(canonical)
    assert result["summary"]["policy_summary"].get("physical_vacancy_build_repriced") is None


def test_backsolve_never_rebuilds_an_analyst_vacancy():
    canonical = resolve_assumptions(_no_t12_vacancy(), overrides={"physical_vacancy": {"value": 0.07, "note": "x"}})
    result = _solve(canonical, trailing_actuals=TYPED)
    record = result["summary"]["policy_summary"]["physical_vacancy_build_repriced"]
    assert record["rebuilt"] is False and "override" in record["reason"]
    assert {r["vacancy_rate"] for r in result["case"]["physical_vacancy_curve"]} == {0.07}


def test_backsolve_prices_the_analyst_benchmark_added_after_ingest():
    canonical = json.loads(_library(A))
    plain = _solve(canonical)
    canonical["metadata"]["property_summary"]["market_vacancy_benchmark"] = {
        "rate": "0.09", "as_of": "2026-09-30", "source": "synthetic submarket survey"}
    result = _solve(canonical)
    case = result["case"]
    assert _build(case)["market_base"]["level"] == "analyst"
    assert _build(case)["market_base"]["value"] == 0.09
    assert result["summary"]["solved_purchase_price"] < plain["summary"]["solved_purchase_price"]
    # The base persists through backsolve's re-resolution and a later one.
    assert resolve_assumptions(case, mode="pricing") == case
    again = resolve_assumptions(copy.deepcopy(case))
    assert _build(again)["market_base"]["level"] == "analyst"


# ── 3. the value-add vintage floor applies after the renovations ─────────


def _c():
    return json.loads(_library(C))


def test_the_floor_window_starts_after_the_last_renovation():
    window = vacancy_floor_window(_c(), strategy="value_add")
    assert window["first_renovation_month"] == "2027-01"
    assert window["last_renovation_month"] == "2028-09"
    assert window["floor_applies_from"] == "2028-10"
    assert vacancy_floor_window(_c(), strategy="cashflow") is None


def test_pricing_floors_only_the_months_after_the_renovations():
    deal = _c()
    profile = GuardrailProfile(strategy="value_add", year_built=1974)
    original = _vacancy_months(deal)
    out, findings = apply_guardrails(deal, mode="pricing", profile=profile)
    vacancy = [f for f in findings if f["key"] == "physical_vacancy"]
    assert vacancy and all(f["start_period"] >= "2028-10" for f in vacancy)
    assert {f["floor_applies_from"] for f in vacancy} == {"2028-10"}
    assert {f["renovation_window"]["last_renovation_month"] for f in vacancy} == {"2028-09"}
    for row in out["physical_vacancy_curve"]:
        if row["end_period"] <= "2028-09":
            assert row["vacancy_rate"] == original[(row["cohort_id"], row["start_period"])]
        else:
            assert row["vacancy_rate"] >= 0.08
    assert any(row["vacancy_rate"] < 0.08 for row in out["physical_vacancy_curve"])
    record = out["assumptions"]["entries"]["physical_vacancy"]["guardrail"]
    assert record["floor_applies_from"] == "2028-10"
    assert "renovation" in record["basis"]


def test_a_segment_across_the_renovation_end_is_split():
    deal = _c()
    deal["physical_vacancy_curve"] = [
        {"cohort_id": row["cohort_id"], "start_period": "2026-11", "end_period": "2031-10", "vacancy_rate": 0.05}
        for row in {r["cohort_id"]: r for r in deal["physical_vacancy_curve"]}.values()
    ]
    out, findings = apply_guardrails(deal, mode="pricing", profile=GuardrailProfile(strategy="value_add",
                                                                                     year_built=1974))
    rows = [r for r in out["physical_vacancy_curve"] if r["cohort_id"] == "1br1ba"]
    assert [(r["start_period"], r["end_period"], r["vacancy_rate"]) for r in rows] == [
        ("2026-11", "2028-09", 0.05), ("2028-10", "2031-10", 0.08)]
    split = [f for f in findings if f["key"] == "physical_vacancy" and f.get("cohort_id") == "1br1ba"]
    assert split[0]["split_at"] == "2028-09"


def test_a_stabilized_strategy_floors_every_month():
    deal = _c()
    for row in deal["physical_vacancy_curve"]:
        row["vacancy_rate"] = 0.03
    profile = GuardrailProfile(strategy="cashflow", year_built=1974)
    floors, summary = house_floors(deal, profile)
    assert floors["physical_vacancy"].applies_from is None and summary["floor_window"] is None
    findings = enforce_floors(deal, mode="pricing", profile=profile, keys=("physical_vacancy",))
    assert len(findings) == len(deal["physical_vacancy_curve"])
    assert not any("floor_applies_from" in f for f in findings)
    assert {r["vacancy_rate"] for r in deal["physical_vacancy_curve"]} == {float(floors["physical_vacancy"].value)}


def test_the_floor_discloses_the_window():
    deal = _c()
    floors, summary = house_floors(deal, GuardrailProfile(strategy="value_add", year_built=1974))
    assert floors["physical_vacancy"].applies_from == "2028-10"
    assert "2028-10" in floors["physical_vacancy"].basis
    assert summary["floor_window"]["floor_applies_from"] == "2028-10"
    build_floor = _build(deal)["floor"]
    assert build_floor["applies_from"] == "2028-10"
    assert build_floor["renovation_window"]["last_renovation_month"] == "2028-09"


def test_deal_c_prices_the_renovation_window_without_the_floor():
    result = backsolve_price(copy.deepcopy(_c()), target_coc="0.06", policy={**POLICY, "strategy": "value_add"},
                             benchmark=BENCHMARK, min_price="1000000.00", max_price="100000000.00")
    assert result["status"] == "converged"
    summary = result["summary"]["policy_summary"]
    assert summary["physical_vacancy_floor_applies_from"] == "2028-10"
    curve = result["case"]["physical_vacancy_curve"]
    assert any(r["vacancy_rate"] < 0.08 for r in curve if r["end_period"] <= "2028-09")
    assert all(r["vacancy_rate"] >= 0.08 for r in curve if r["start_period"] >= "2028-10")
    case = result["case"]
    assert resolve_assumptions(case, mode="pricing") == case


def test_the_cli_backsolve_reads_trailing_actuals_json(tmp_path):
    canonical = _no_t12_vacancy()
    path, actuals = tmp_path / "deal.json", tmp_path / "t12.json"
    path.write_text(json.dumps(canonical), encoding="utf-8")
    actuals.write_text(json.dumps({"trailing_actuals": {"gross_potential_rent": 1_000_000.0,
                                                        "vacancy_loss": -90_000.0}}), encoding="utf-8")
    python = REPO_ROOT / ".venv" / "bin" / "python"
    run = subprocess.run(
        [str(python if python.exists() else sys.executable), "-m", "engine.backsolve", "--canonical-json", str(path),
         "--output-dir", str(tmp_path / "out"), "--policy-version", "plat.backsolve-policy/1", "--strategy",
         "cashflow", "--target-coc", "0.06", "--benchmark-5yr-treasury", "0.04", "--benchmark-as-of", "2026-10-01",
         "--benchmark-source", "synthetic", "--max-price", "40000000.00", "--trailing-actuals-json", str(actuals)],
        capture_output=True, text=True, cwd=str(REPO_ROOT))
    assert run.returncode == 0, run.stderr
    summary = json.loads((tmp_path / "out" / "backsolve_summary.json").read_text())
    assert summary["policy_summary"]["physical_vacancy_build_repriced"]["rebuilt"] is True
