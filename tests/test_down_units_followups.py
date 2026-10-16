"""Down units follow-ups (ADR 0003 decision q, UPL-110).

1. Taking offline units (box-score offline statuses, or the rent roll's down
   units) out of the asset's trailing vacancy never creates a below-market
   credit: the exclusion only removes vacancy above the market comparator,
   and the uncapped value is disclosed.
2. A T12 that carries a $0 model / storage / down-unit line does not book
   the down units apart: a zero non-revenue rate does not block the
   exclusion.
3. Model and admin units stay out of the rent roll's offline counts: they are
   non-revenue by choice, not a capital problem.
4. runs/ingest_deal.py takes the down-unit inputs as flags.

Every rent roll, unit and figure here is synthetic.
"""

from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path

import pytest

from engine.assumptions import resolve_assumptions
from engine.ingest.ingestion_pipeline import build_deal_from_documents
from tests import deal_library as dl
from tests.test_down_units import (
    MARKET,
    ROLL_ROWS,
    _ingested,
    _library_with_down_units,
    _registry,
    _reresolved,
    _write_roll,
)
from tests.yardi_fixtures import write_yardi_rent_roll, write_yardi_t12


@pytest.fixture(scope="module")
def roll_dir(tmp_path_factory):
    return str(tmp_path_factory.mktemp("down_units_followups"))


def _build(deal):
    return deal["assumptions"]["entries"]["physical_vacancy"]["detail"]["vacancy_build"]


# ── 1. no below-market credit from the offline exclusion ─────────────────


def test_the_down_unit_exclusion_stops_at_the_market(roll_dir):
    # 30% T12 vacancy less 25% of units down would be 5%, under the 6% market.
    gap = _build(_reresolved(roll_dir, t12_vacancy=0.30))["asset_vs_market"]
    assert gap["offline_exclusion_capped"] is True
    assert gap["asset_vacancy_stabilized_uncapped"] == pytest.approx(0.05)
    assert gap["asset_vacancy_stabilized"] == pytest.approx(0.06)
    assert gap["gap"] == pytest.approx(0.0)
    assert "market" in gap["note"]


def test_a_vacancy_still_above_the_market_keeps_the_full_exclusion(roll_dir):
    gap = _build(_reresolved(roll_dir, t12_vacancy=0.40))["asset_vs_market"]
    assert gap["offline_exclusion_capped"] is False
    assert gap["asset_vacancy_stabilized_uncapped"] == pytest.approx(0.15)
    assert gap["asset_vacancy_stabilized"] == pytest.approx(0.15)
    assert gap["gap"] == pytest.approx(0.09)


def test_a_vacancy_already_below_the_market_is_not_reduced_further(roll_dir):
    gap = _build(_reresolved(roll_dir, t12_vacancy=0.04))["asset_vs_market"]
    assert gap["asset_vacancy_stabilized_uncapped"] == pytest.approx(0.0)
    assert gap["asset_vacancy_stabilized"] == pytest.approx(0.04)  # the T12's own vacancy, untouched
    assert gap["gap"] == pytest.approx(-0.02)


def test_the_box_score_offline_path_is_capped_too(roll_dir):
    deal = _ingested(roll_dir)
    deal.pop("assumptions", None)
    for row in deal["physical_vacancy_curve"]:
        row["vacancy_rate"] = None
    deal["metadata"].setdefault("property_summary", {})["house_box_score"] = {
        "total_units": 12, "status_counts": {"Occupied": 8, "Vacant": 1, "Down": 3},
    }
    t12 = deal["metadata"]["ingest_gate"]["evidence"]["t12"]
    t12["vacancy_loss"] = round(0.30 * t12["gross_potential_rent"], 2)
    t12.pop("non_revenue_loss", None)
    build = _build(resolve_assumptions(deal, market=_registry(MARKET)))
    assert build["stabilization_exclusions"]["offline_source"] == "house_box_score.status_counts"
    gap = build["asset_vs_market"]
    assert gap["offline_exclusion_capped"] is True
    assert gap["asset_vacancy_stabilized"] == pytest.approx(0.06)


@lru_cache(maxsize=None)
def _variant_builds(slug: str, tmp: str) -> str:
    base = _build(dl.ingest(dl.load_deal(slug)))
    down = _build(_library_with_down_units(Path(tmp), 6, slug=slug))
    return json.dumps([base, down])


@pytest.mark.parametrize("slug", ["a_stabilized_1990s", "c_value_add_1970s_reno"])
def test_library_variants_never_fall_below_the_market_comparison(slug, tmp_path_factory):
    base, down = json.loads(_variant_builds(slug, str(tmp_path_factory.mktemp(slug))))
    gap = down["asset_vs_market"]
    comparator = gap["market_comparator"]
    # The down units can explain away vacancy above the market, never earn a credit below it.
    assert gap["asset_vacancy_stabilized"] >= min(gap["asset_vacancy"], comparator) - 1e-12
    assert down["stabilized"] >= min(base["stabilized"], down["market_base"]["value"]) - 1e-12
    if slug == "a_stabilized_1990s":
        # The T12 vacancy sits at the market: nothing to explain away, stabilized unchanged.
        assert down["stabilized"] == pytest.approx(base["stabilized"])
    else:
        # Deal c's T12 runs above the market: the down units explain the excess.
        assert gap["offline_exclusion_capped"] is True
        assert gap["gap"] == pytest.approx(0.0)
        assert down["stabilized"] == pytest.approx(down["market_base"]["value"])


# ── 2. a $0 non-revenue line does not block the exclusion ────────────────


def _yardi_deal(tmp_path, *, non_revenue=None):
    deal = build_deal_from_documents(
        property_id="Synthetic Yardi Down Units",
        rent_roll_path=write_yardi_rent_roll(tmp_path / "rr.xlsx", off_market={"105": "DOWN"}),
        t12_path=write_yardi_t12(tmp_path / "ysi_is.xlsx"),
        analysis_start="2027-02",
        analysis_end="2032-01",
        year_built=1988,
        msa="Synthetic Metro",
        metro_slug="synthetic_metro",
        submarket="Synthetic North",
        market=None,
    )
    deal.pop("assumptions", None)
    for row in deal["physical_vacancy_curve"]:
        row["vacancy_rate"] = None
    t12 = deal["metadata"]["ingest_gate"]["evidence"]["t12"]
    t12["vacancy_loss"] = round(0.20 * t12["gross_potential_rent"], 2)
    if non_revenue is not None:
        t12["non_revenue_loss"] = round(non_revenue * t12["gross_potential_rent"], 2)
    return resolve_assumptions(deal, market=_registry(MARKET))


def test_a_zero_model_storage_down_line_does_not_block_the_exclusion(tmp_path):
    deal = _yardi_deal(tmp_path)
    t12 = deal["metadata"]["ingest_gate"]["evidence"]["t12"]
    # The synthetic Yardi T12 prints "Model/Storage/Down Unit" at $0.
    assert t12["non_revenue_loss"] == 0.0
    exclusions = _build(deal)["stabilization_exclusions"]
    assert exclusions["t12_non_revenue_rate"] == 0.0
    assert exclusions["offline_source"] == "rent_roll.down_units"
    assert exclusions["offline_applied"] is True
    gap = _build(deal)["asset_vs_market"]
    assert gap["asset_vacancy_stabilized"] == pytest.approx(0.20 - 1 / 12, abs=1e-6)


def test_a_t12_that_books_down_units_on_a_nonzero_line_still_keeps_them_apart(tmp_path):
    exclusions = _build(_yardi_deal(tmp_path, non_revenue=0.03))["stabilization_exclusions"]
    assert exclusions["t12_non_revenue_rate"] == pytest.approx(0.03)
    assert exclusions["offline_applied"] is False


# ── 3. model and admin units are not offline ─────────────────────────────


def test_model_and_admin_units_stay_out_of_the_rent_roll_offline_counts(roll_dir):
    exclusions = _build(_reresolved(roll_dir, t12_vacancy=0.40))["stabilization_exclusions"]
    # The roll shows 3 down units plus a model and an employee unit: only the 3 down units are offline.
    assert [row[5] for row in ROLL_ROWS].count("Model") == 1
    assert exclusions["offline_units"] == 3


# ── 4. CLI flags ─────────────────────────────────────────────────────────

T12 = Path(__file__).parent / "fixtures" / "entrata_style_t12.csv"


def _run_ingest(monkeypatch, capsys, tmp_path, roll, *flags):
    from runs import ingest_deal

    output = tmp_path / "deal.json"
    argv = ["ingest_deal.py", "--property-id", "Synthetic CLI Down Units", "--rent-roll", str(roll),
            "--t12", str(T12), "--start", "2026-08", "--end", "2031-07", "--year-built", "1984",
            "--no-market-data", "--output", str(output), *flags]
    monkeypatch.setattr(sys, "argv", argv)
    code = ingest_deal.main()
    captured = capsys.readouterr()
    deal = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
    return code, deal, captured.out, captured.err


def test_the_cli_takes_the_down_unit_inputs(monkeypatch, capsys, tmp_path):
    roll = _write_roll(tmp_path / "roll.csv")
    code, deal, out, err = _run_ingest(
        monkeypatch, capsys, tmp_path, roll,
        "--down-unit-scope", "make_ready", "--down-unit-cost-per-unit", "7000", "--down-unit-pace", "2",
        "--down-unit-funding", "lender_holdback", "--down-unit-scopes", '{"203": "full_gut"}',
    )
    assert code == 0, err
    entries = deal["assumptions"]["entries"]
    assert (entries["down_unit_scope"]["value"], entries["down_unit_scope"]["source"]) == ("make_ready", "analyst")
    assert entries["down_unit_cost_per_unit"]["value"] == 7000.0
    assert entries["down_unit_pace_per_month"]["value"] == 2
    assert entries["down_unit_funding"]["value"] == "lender_holdback"
    units = {u["unit_id"]: u for u in deal["capital_plan"]["down_unit_restoration"]["units"]}
    assert units["203"]["scope"] == "full_gut"
    assert "Down-unit restoration" in out


def test_the_cli_reads_per_unit_scopes_from_a_file_and_flags_a_roll_without_down_units(
    monkeypatch, capsys, tmp_path,
):
    scopes = tmp_path / "scopes.json"
    scopes.write_text('{"102": "make_ready"}', encoding="utf-8")
    code, deal, _out, err = _run_ingest(
        monkeypatch, capsys, tmp_path, _write_roll(tmp_path / "roll.csv"), "--down-unit-scopes", str(scopes))
    assert code == 0, err
    units = {u["unit_id"]: u for u in deal["capital_plan"]["down_unit_restoration"]["units"]}
    assert units["102"]["scope"] == "make_ready"

    clean = [row for row in ROLL_ROWS if row[5] not in ("Down", "Vacant-Down", "Offline - Fire")]
    code, deal, _out, err = _run_ingest(
        monkeypatch, capsys, tmp_path, _write_roll(tmp_path / "clean.csv", clean), "--down-unit-scope", "full_gut")
    assert code == 0, err
    assert "down_unit_inputs_ignored_no_down_units" in deal["metadata"]["intake_sanity_flags"]


def test_the_cli_refuses_a_bad_down_unit_scope(monkeypatch, capsys, tmp_path):
    code, _deal, _out, err = _run_ingest(
        monkeypatch, capsys, tmp_path, _write_roll(tmp_path / "roll.csv"), "--down-unit-scopes", '{"203": "paint"}')
    assert code == 2
    assert "scope" in err


def test_the_pca_help_mentions_down_unit_costs():
    from runs import ingest_deal

    assert "down_unit_costs" in ingest_deal.build_parser().format_help()
