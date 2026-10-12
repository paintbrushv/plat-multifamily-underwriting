"""Rent-roll reconciliation gate (ADR 0003 §5), mirroring the T12 gate.

When the export carries its own summary, the parsed units and vacant units
must match it exactly and the monthly market-rent total within 0.5%, or
``metadata.ingest_gate`` is BLOCKED. Without a summary the check is
UNVERIFIED: flagged for review, never blocked.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from engine.ingest.ingestion_pipeline import build_deal_from_documents
from engine.ingest.rent_roll_parser import reconcile_rent_roll
from engine.validator import validate_deal
from tests.yardi_fixtures import rr_expected, write_yardi_rent_roll, write_yardi_t12

FIXTURES = Path(__file__).parent / "fixtures"


def _ingest(tmp_path, rent_roll):
    return build_deal_from_documents(
        property_id="Synthetic Yardi Deal",
        rent_roll_path=rent_roll,
        t12_path=write_yardi_t12(tmp_path / "ysi_is.xlsx"),
        analysis_start="2027-02",
        analysis_end="2032-01",
        rent_growth_rate=0.025,
        collection_loss_rate=0.01,
    )


def test_a_tied_export_summary_passes(tmp_path):
    deal = _ingest(tmp_path, write_yardi_rent_roll(tmp_path / "rr.xlsx"))
    gate = deal["metadata"]["ingest_gate"]

    assert gate["status"] == "PASS"
    assert gate["rent_roll_reconciliation"]["status"] == "PASS"
    assert gate["evidence"]["rent_roll"]["pre_leased_vacant_units"] == ["107"]
    assert "rent_roll_summary_not_found_reconciliation_unverified" not in deal["metadata"].get(
        "intake_sanity_flags", []
    )
    assert validate_deal(deal).status == "PASS"


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"vacant_units": 3}, "rent_roll_vacancy_does_not_reconcile"),
        ({"units": 13}, "rent_roll_units_do_not_reconcile"),
        ({"market_rent_total": rr_expected()["market_rent_total"] * 1.01}, "rent_roll_market_rent_does_not_reconcile"),
    ],
)
def test_a_summary_that_does_not_tie_blocks_the_gate(tmp_path, override, code):
    rent_roll = write_yardi_rent_roll(tmp_path / "rr.xlsx", summary_overrides=override)
    deal = _ingest(tmp_path, rent_roll)
    gate = deal["metadata"]["ingest_gate"]

    assert gate["status"] == "BLOCKED"
    assert gate["rent_roll_reconciliation"]["status"] == "BLOCKED"
    assert [b["code"] for b in gate["blockers"]] == [code]
    report = validate_deal(deal)
    assert report.status == "FAIL"
    assert any(i.code == "INGEST_BLOCKED" and code in i.message for i in report.issues)


def test_market_rent_within_tolerance_passes(tmp_path):
    within = rr_expected()["market_rent_total"] * 1.004
    rent_roll = write_yardi_rent_roll(tmp_path / "rr.xlsx", summary_overrides={"market_rent_total": within})
    gate = _ingest(tmp_path, rent_roll)["metadata"]["ingest_gate"]

    assert gate["status"] == "PASS"
    assert gate["rent_roll_reconciliation"]["market_rent_total"]["status"] == "PASS"


def test_no_export_summary_is_unverified_not_blocked(tmp_path):
    rent_roll = write_yardi_rent_roll(tmp_path / "rr.xlsx", summary=False)
    deal = _ingest(tmp_path, rent_roll)
    gate = deal["metadata"]["ingest_gate"]

    assert gate["status"] == "PASS"
    assert gate["rent_roll_reconciliation"]["status"] == "UNVERIFIED"
    assert "rent_roll_summary_not_found_reconciliation_unverified" in deal["metadata"]["intake_sanity_flags"]

    plain = build_deal_from_documents(
        property_id="Synthetic Plain Deal",
        rent_roll_path=FIXTURES / "sample_rent_roll.csv",
        t12_path=FIXTURES / "entrata_style_t12.csv",
        analysis_start="2026-08",
        analysis_end="2031-07",
        rent_growth_rate=0.025,
        collection_loss_rate=0.02,
    )
    assert plain["metadata"]["ingest_gate"]["status"] == "PASS"
    assert plain["metadata"]["ingest_gate"]["rent_roll_reconciliation"]["status"] == "UNVERIFIED"


def test_non_revenue_units_may_count_either_way():
    rows = [{"status": "Occupied", "market_rent": 1000.0}] * 8 + [{"status": "Vacant", "market_rent": 1000.0}] * 2
    summary = {"source": "test", "units": 10, "vacant_units": 1, "non_revenue_units": 1, "market_rent_total": 10_000.0}
    assert reconcile_rent_roll(rows, summary)["status"] == "PASS"
    assert reconcile_rent_roll(rows, {**summary, "non_revenue_units": 0})["status"] == "BLOCKED"


def test_cli_prints_the_rent_roll_gate_next_to_the_t12(monkeypatch, capsys, tmp_path):
    from runs import ingest_deal

    output = tmp_path / "deal.json"
    monkeypatch.setattr(sys, "argv", [
        "ingest_deal.py",
        "--property-id", "Synthetic Yardi Deal",
        "--rent-roll", str(write_yardi_rent_roll(tmp_path / "rr.xlsx", summary_overrides={"vacant_units": 3})),
        "--t12", str(write_yardi_t12(tmp_path / "ysi_is.xlsx")),
        "--start", "2027-02",
        "--end", "2032-01",
        "--no-market-data",
        "--output", str(output),
    ])
    ingest_deal.main()
    out = capsys.readouterr().out

    assert "T12 opex: PASS" in out
    assert "Rent roll units: PASS parsed 12 vs export 12" in out
    assert "Rent roll vacant units: BLOCKED parsed 2 vs export 3" in out
    assert "Rent roll market rent: PASS" in out
    assert "BLOCKER [rent_roll_vacancy_does_not_reconcile]" in out
    gate = json.loads(output.read_text(encoding="utf-8"))["metadata"]["ingest_gate"]
    assert gate["status"] == "BLOCKED"
