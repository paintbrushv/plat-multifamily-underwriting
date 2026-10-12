"""A priced deal with no exit input models an exit at the analysis end (ADR 0003 §3).

Ingest writes an ``exit_cap_rate`` placeholder (null) and never invents a cap
rate: the resolver's fallback chain fills it (analyst > market exit-year cap
rate > house fallback). The validator accepts the placeholder; the engine
entry refuses to run on an unresolved one.
"""

from __future__ import annotations

import copy
from datetime import date
from pathlib import Path

import pytest

from engine.assumptions import resolve_assumptions
from engine.engine import run_underwriting
from engine.ingest.ingestion_pipeline import build_deal_from_documents
from engine.market_data import Geo, MarketDataRegistry, SyntheticProvider
from engine.validator import (
    EXIT_CAP_RATE_PLACEHOLDER,
    EXIT_CAP_RATE_UNRESOLVED,
    validate_deal,
    validate_or_raise,
)

FIXTURES = Path(__file__).parent / "fixtures"
ROLL = FIXTURES / "sample_rent_roll.csv"
T12 = FIXTURES / "sample_t12.csv"
SUB = Geo(metro_slug="synthetic_metro", submarket="Synthetic North")
CAP_RATES = {"2030Q4": 0.058, "2031Q1": 0.06, "2031Q2": 0.062, "2031Q3": 0.064, "2031Q4": 0.066}
# Ingest itself may never be the source of an exit cap rate it was not given.
NOT_INVENTED_BY_INGEST = ("analyst", "deal:om")


def _ingest(**kwargs):
    params = dict(
        property_id="Synthetic Exit Deal",
        rent_roll_path=ROLL,
        t12_path=T12,
        analysis_start="2026-05",
        analysis_end="2031-04",
        market=None,
        property_tax_millage=20.0,
    )
    params.update(kwargs)
    return build_deal_from_documents(**params)


def test_priced_deal_without_exit_inputs_models_an_exit_at_analysis_end():
    deal = _ingest(purchase_price=10_000_000, ltv=0.6, loan_rate=0.06, amort_years=30)
    exit_assumptions = deal["exit_assumptions"]
    assert exit_assumptions["exit_month"] == "2031-04"
    assert "exit_cap_rate" in exit_assumptions
    entry = deal["assumptions"]["entries"]["exit_cap_rate"]
    assert entry["override"] is None
    assert entry["source"] not in NOT_INVENTED_BY_INGEST
    # The cap rate is the resolver's: either still the placeholder or a disclosed fallback.
    if exit_assumptions["exit_cap_rate"] is not None:
        assert entry["review_required"] is True
    assert exit_assumptions["sale_cost_percent"] == deal["assumptions"]["entries"]["sale_cost_percent"]["value"]


def test_ingest_cli_reports_the_modeled_exit(monkeypatch, capsys, tmp_path):
    import sys

    from runs import ingest_deal

    output = tmp_path / "deal.json"
    monkeypatch.setattr(sys, "argv", [
        "ingest_deal.py", "--property-id", "Synthetic Exit Deal",
        "--rent-roll", str(ROLL), "--t12", str(T12), "--start", "2026-05", "--end", "2031-04",
        "--no-market-data", "--output", str(output), "--purchase-price", "10000000",
        "--millage", "20", "--validate",
    ])
    assert ingest_deal.main() == 0
    out = capsys.readouterr().out
    assert "exit 2031-04" in out
    assert "Validation: PASS" in out


def test_market_exit_cap_rate_fills_the_placeholder():
    registry = MarketDataRegistry(
        [SyntheticProvider({("market_cap_rate", SUB): CAP_RATES}, as_of=date(2026, 3, 31), confidence="high")]
    )
    deal = _ingest(
        purchase_price=10_000_000, market=registry,
        msa="Synthetic Metro", metro_slug="synthetic_metro", submarket="Synthetic North",
    )
    assert deal["exit_assumptions"]["exit_month"] == "2031-04"
    assert deal["exit_assumptions"]["exit_cap_rate"] == pytest.approx(0.063)
    assert deal["assumptions"]["entries"]["exit_cap_rate"]["source"] == "market:synthetic"


def test_unpriced_deal_without_exit_inputs_models_no_exit():
    deal = _ingest()
    assert "exit_assumptions" not in deal


def test_explicit_exit_cap_rate_stays_an_analyst_override():
    deal = _ingest(purchase_price=10_000_000, exit_cap_rate=0.061)
    assert deal["exit_assumptions"]["exit_cap_rate"] == 0.061
    assert deal["assumptions"]["entries"]["exit_cap_rate"]["source"] == "analyst"


def _with_placeholder(inputs: dict) -> dict:
    out = copy.deepcopy(inputs)
    out["exit_assumptions"]["exit_cap_rate"] = None
    return out


def test_validator_accepts_the_placeholder_with_a_warning(minimal_deal_inputs):
    report = validate_deal(_with_placeholder(minimal_deal_inputs))
    assert report.status == "PASS", [i for i in report.issues if i.severity == "ERROR"]
    placeholder = [i for i in report.issues if i.code == EXIT_CAP_RATE_PLACEHOLDER]
    assert [i.severity for i in placeholder] == ["WARNING"]
    assert not [i for i in report.issues if i.path == "/exit_assumptions/exit_cap_rate" and i.code == "SCHEMA"]


def test_engine_refuses_an_unresolved_placeholder(minimal_deal_inputs):
    inputs = _with_placeholder(minimal_deal_inputs)
    with pytest.raises(ValueError, match=EXIT_CAP_RATE_UNRESOLVED):
        validate_or_raise(inputs)
    with pytest.raises(ValueError, match=EXIT_CAP_RATE_UNRESOLVED):
        run_underwriting(inputs)


def test_an_override_resolves_the_placeholder(minimal_deal_inputs):
    resolved = resolve_assumptions(_with_placeholder(minimal_deal_inputs), overrides={"exit_cap_rate": 0.0575})
    assert resolved["exit_assumptions"]["exit_cap_rate"] == 0.0575
    validate_or_raise(resolved)
