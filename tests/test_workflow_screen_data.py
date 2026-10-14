"""The data the screens need from the workflow tools (docs/MCP_TOOLS.md).

``run_deal`` returns ``annual_cash_flow`` (the monthly cash flow summed by analysis year, the
sale left out) and the exit NOI window; ``get_deal`` returns the deal's recorded
``pricing_defaults`` (the sample's illustrative benchmark, so the pricing screen can say so).
Synthetic sample deal, hermetic workspace, no market data.
"""

from __future__ import annotations

import json

import pytest

from engine.workflow import config as wf_config
from engine.workflow import fields as profile_fields
from engine.workflow import service


@pytest.fixture
def sample(monkeypatch, tmp_path):
    for name in ("PLAT_MARKET_STUDY_ROOT", "PLAT_SUPPLY_DEMAND_ROOT"):
        monkeypatch.setenv(name, str(tmp_path / "no-market-data" / name.lower()))
    for name in ("PLAT_MARKET_DATA_PROVIDERS", "PLAT_MARKET_DATA_USER_FILE", "PLAT_MARKET_STUDY_COMPS",
                 "PLAT_UW_MAX_UPLOAD_MB", "PLAT_UW_ALLOWED_EXTENSIONS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PLAT_UNDERWRITING_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setenv(wf_config.HOME_ENV, str(tmp_path / "workspace"))
    loaded = service.load_sample_deal()
    assert loaded["ok"], loaded
    return tmp_path / "workspace", loaded


def test_get_deal_returns_the_recorded_pricing_defaults(sample):
    _, loaded = sample
    deal = service.get_deal(loaded["deal_id"])
    assert deal["pricing_defaults"] == loaded["pricing_defaults"]
    assert deal["pricing_defaults"]["benchmark_5yr_treasury"]
    own = service.create_deal("Synthetic Own Deal")
    assert service.get_deal(own["deal_id"])["pricing_defaults"] is None


def test_run_deal_returns_the_annual_cash_flow_and_the_exit_window(sample):
    workspace, loaded = sample
    deal_id = loaded["deal_id"]
    assert service.ingest_deal(deal_id)["ok"]
    run = service.run_deal(deal_id)
    assert run["ok"], run
    results = json.loads((workspace / "deals" / deal_id / "run" / "results.json").read_text())
    annual = run["annual_cash_flow"]
    months = results["cashflow"]["by_month"]
    assert [row["analysis_year"] for row in annual] == [1, 2, 3, 4, 5]
    assert sum(row["months"] for row in annual) == len(months) == 60
    assert (annual[0]["start"], annual[0]["end"], annual[-1]["end"]) == ("2026-11", "2027-10", "2031-10")
    coc = results["metrics"]["coc"]
    year1 = annual[0]
    assert year1["net_operating_income"] == pytest.approx(run["metrics"]["noi_year_1"], abs=0.05)
    assert year1["capex_charge"] == pytest.approx(coc["components"]["replacement_reserves_year_1"], abs=0.05)
    assert year1["debt_service"] == pytest.approx(coc["components"]["debt_service_year_1"], abs=0.05)
    assert year1["cash_flow_after_debt"] == pytest.approx(coc["free_cf_year_1"], abs=0.05)
    for row in annual:  # operating only: no sale proceeds or loan payoff in either cash flow
        assert row["cash_flow_before_debt"] == pytest.approx(
            row["net_operating_income"] - row["capex_charge"] - row["other_capex"], abs=1.0)
        assert row["cash_flow_after_debt"] == pytest.approx(row["cash_flow_before_debt"] - row["debt_service"], abs=1.0)
    exit_ = run["metrics"]["exit"]
    assert exit_["noi_window"]["basis"] == "forward" and exit_["noi_window"]["start"] == "2031-11"
    assert exit_["sale_costs"] > 0 and exit_["loan_payoff"] > 0
    assert exit_["value"] - exit_["sale_costs"] == pytest.approx(exit_["net_sale_proceeds"], abs=1.0)


def test_required_to_run_is_what_run_deal_reports_missing():
    assert sorted(service.missing_to_run({})) == sorted(profile_fields.REQUIRED_TO_RUN)
