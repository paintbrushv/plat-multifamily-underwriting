"""T12 rent evidence: the collection-loss and concession rates the resolver
reads must use the real rent basis and net losses.

The synthetic Yardi statement (tests/yardi_fixtures.py) has a $0 "Gross
Potential Rent" detail line, a Market Rent line, a TOTAL GROSS POTENTIAL RENT
row, bad-debt write-offs and bad-debt collections. Before the fix the GPR
basis was the $0 line (so no T12 rate at all) and collections were added to
write-offs instead of offsetting them.
"""

from __future__ import annotations

import pytest

from engine.ingest.ingestion_pipeline import _t12_rent_evidence, build_deal_from_documents
from tests.yardi_fixtures import t12_line, t12_section, write_yardi_rent_roll, write_yardi_t12


def _line(label, amount):
    return {"label": label, "annual_amount": amount, "kind": "rental", "section": None}


def test_yardi_collection_loss_is_net_bad_debt_over_the_statement_gpr(tmp_path):
    deal = build_deal_from_documents(
        property_id="Synthetic Yardi Deal",
        rent_roll_path=write_yardi_rent_roll(tmp_path / "rr.xlsx"),
        t12_path=write_yardi_t12(tmp_path / "ysi_is.xlsx"),
        analysis_start="2027-02",
        analysis_end="2032-01",
        rent_growth_rate=0.025,
        collection_loss_rate=None,
        year_built=1988,
    )
    t12 = deal["metadata"]["ingest_gate"]["evidence"]["t12"]
    gpr = t12_section("TOTAL GROSS POTENTIAL RENT")
    assert t12_line("Gross Potential Rent") == 0
    net_bad_debt = abs(t12_line("Bad Debt")) - t12_line("Bad Debt Collections")

    assert t12["gross_potential_rent"] == pytest.approx(gpr)
    assert t12["gross_potential_rent_basis"] == "statement gross potential rent total"
    assert t12["bad_debt"] == pytest.approx(net_bad_debt)
    assert t12["bad_debt_recoveries"] == pytest.approx(t12_line("Bad Debt Collections"))
    assert t12["concessions"] == pytest.approx(abs(t12_line("Concessions")))

    loss = deal["collection_loss_curve"][0]["loss_rate"]
    assert loss == pytest.approx(net_bad_debt / gpr, abs=1e-6)
    entry = deal["assumptions"]["entries"]["collection_loss"]
    assert entry["source"] == "deal:t12"
    assert not any(a["field"] == "collection_loss_curve.loss_rate"
                   for a in deal["metadata"]["ingest_gate"]["assumed_defaults"])


def test_market_rent_is_the_basis_when_the_gpr_line_is_zero_and_there_is_no_total():
    evidence = _t12_rent_evidence([
        _line("Gross Potential Rent", 0.0),
        _line("Market Rent", 500_000.0),
        _line("Loss to Lease", -5_000.0),
        _line("Bad Debt", -4_000.0),
        _line("Recovery of Bad Debt", 1_000.0),
        _line("Concessions", -3_000.0),
        _line("Concessions Reversal", 500.0),
    ])
    assert evidence["gross_potential_rent"] == 500_000.0
    assert evidence["gross_potential_rent_basis"].startswith("market rent lines")
    assert evidence["source_lines"]["gross_potential_rent"] == ["Market Rent"]
    assert evidence["bad_debt"] == 3_000.0
    assert evidence["concessions"] == 2_500.0


def test_bad_debt_nets_whatever_the_sign_convention():
    positive = _t12_rent_evidence([
        _line("Gross Potential Rent", 100_000.0),
        _line("Bad Debt Write-Off", 2_000.0),
        _line("Bad Debt Collections", -500.0),
    ])
    assert positive["gross_potential_rent"] == 100_000.0
    assert positive["gross_potential_rent_basis"] == "gross potential rent lines"
    assert positive["bad_debt"] == 1_500.0

    recovered_more = _t12_rent_evidence([_line("Bad Debt", -100.0), _line("Bad Debt Recovery", 300.0)])
    assert recovered_more["bad_debt"] == 0.0
