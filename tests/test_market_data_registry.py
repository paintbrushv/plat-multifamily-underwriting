"""Registry precedence, candidates, defaults, placeholders, user and synthetic providers."""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from engine.market_data import (
    METRICS,
    CoStarProvider,
    Geo,
    MarketDataProvider,
    MarketDataRegistry,
    PlatMarketStudyAgentProvider,
    PlatSupplyDemandProvider,
    RealPageProvider,
    SyntheticProvider,
    UserFileProvider,
    YardiMatrixProvider,
    default_registry,
)
from engine.market_data.paths import market_study_root, supply_demand_root

REPO = Path(__file__).resolve().parents[1]
GEO = Geo(metro_slug="testville_zz", submarket="alpha")


class _Raising:
    name = "raising"
    unavailable_reason = None

    def available(self) -> bool:
        return True

    def get(self, metric, geo, *, as_of=None):
        raise RuntimeError("boom")


class _Unavailable(SyntheticProvider):
    def available(self) -> bool:
        return False


def test_first_provider_with_data_wins_and_all_are_candidates():
    first = SyntheticProvider({("vacancy_current", Geo(metro_slug="testville_zz")): 0.07}, name="first")
    second = SyntheticProvider({("vacancy_current", GEO): 0.05, ("concession_rate", GEO): 0.04}, name="second")
    registry = MarketDataRegistry([first, _Raising(), second])

    winner, candidates = registry.resolve("vacancy_current", GEO)
    assert winner.value == 0.07 and winner.provenance.provider == "first"
    assert [c.provenance.provider for c in candidates] == ["first", "second"]

    winner, candidates = registry.resolve("concession_rate", GEO)
    assert winner.provenance.provider == "second" and len(candidates) == 1

    assert registry.resolve("supply_risk", GEO) == (None, [])


def test_order_is_configurable():
    a = SyntheticProvider({("vacancy_current", GEO): 0.07}, name="a")
    b = SyntheticProvider({("vacancy_current", GEO): 0.05}, name="b")
    assert MarketDataRegistry([b, a]).resolve("vacancy_current", GEO)[0].value == 0.05


def test_unavailable_providers_are_skipped_and_reported():
    hidden = _Unavailable({("vacancy_current", GEO): 0.99}, name="hidden")
    shown = SyntheticProvider({("vacancy_current", GEO): 0.05}, name="shown")
    registry = MarketDataRegistry([hidden, CoStarProvider(env={}), shown])
    winner, candidates = registry.resolve("vacancy_current", GEO)
    assert winner.value == 0.05 and len(candidates) == 1
    status = registry.status()
    assert [s["name"] for s in status] == ["hidden", "costar", "shown"]
    assert status[1]["available"] is False and "PLAT_COSTAR_CREDENTIALS" in status[1]["unavailable_reason"]


def test_unknown_metric_and_duplicate_names_are_rejected():
    registry = MarketDataRegistry([SyntheticProvider()])
    with pytest.raises(ValueError, match="unknown market-data metric"):
        registry.resolve("cap_rate", GEO)
    with pytest.raises(ValueError, match="duplicate"):
        MarketDataRegistry([SyntheticProvider(), SyntheticProvider()])


def test_as_of_filters_synthetic_entries():
    provider = SyntheticProvider({("vacancy_current", GEO): 0.05}, as_of=date(2026, 6, 30))
    registry = MarketDataRegistry([provider])
    assert registry.resolve("vacancy_current", GEO, as_of=date(2026, 1, 1))[0] is None
    assert registry.resolve("vacancy_current", GEO, as_of=date(2026, 7, 1))[0].value == 0.05


def test_synthetic_provider_prefers_the_most_specific_geo():
    provider = SyntheticProvider({
        ("market_cap_rate", Geo(metro_slug="testville_zz")): {"2026Q1": 0.05, "2026Q2": 0.051},
        ("market_cap_rate", GEO): {"2026Q1": 0.055},
    })
    assert provider.get("market_cap_rate", GEO).series == {"2026Q1": 0.055}
    assert provider.get("market_cap_rate", Geo(metro_slug="testville_zz", submarket="beta")).series == {
        "2026Q1": 0.05, "2026Q2": 0.051}
    assert provider.get("market_cap_rate", Geo(metro_slug="elsewhere_zz")) is None
    assert provider.get("market_cap_rate", GEO).unit == "decimal"


@pytest.mark.parametrize("cls,env_name", [
    (CoStarProvider, "PLAT_COSTAR_CREDENTIALS"),
    (YardiMatrixProvider, "PLAT_YARDI_MATRIX_CREDENTIALS"),
    (RealPageProvider, "PLAT_REALPAGE_CREDENTIALS"),
])
def test_paid_placeholders_are_unavailable(cls, env_name):
    unconfigured = cls(env={})
    assert isinstance(unconfigured, MarketDataProvider)
    assert unconfigured.available() is False
    assert env_name in unconfigured.unavailable_reason
    assert unconfigured.get("vacancy_current", GEO) is None
    configured = cls(env={env_name: "synthetic-token"})
    assert configured.configured and configured.available() is False
    assert "no adapter" in configured.unavailable_reason


def test_user_file_unavailable_until_configured(tmp_path):
    assert UserFileProvider(env={}).available() is False
    assert "PLAT_MARKET_DATA_USER_FILE" in UserFileProvider(env={}).unavailable_reason
    missing = UserFileProvider(tmp_path / "missing.json", env={})
    assert not missing.available() and "not found" in missing.unavailable_reason
    assert missing.get("vacancy_current", GEO) is None


def test_user_file_json(tmp_path):
    path = tmp_path / "analyst.json"
    path.write_text(json.dumps({"observations": [
        {"metric": "renovation_premium", "value": 175, "geo": {"metro_slug": "testville_zz"},
         "as_of": "2026-05-01", "confidence": "High"},
        {"metric": "renovation_premium", "value": 210, "geo": {"metro_slug": "testville_zz", "submarket": "alpha"},
         "as_of": "2026-04-01"},
        {"metric": "rent_growth_forecast", "value": {"2026Q3": 0.03, "2026Q4": 0.031},
         "geo": {"metro_slug": "testville_zz"}},
        {"metric": "made_up_metric", "value": 1},
        {"metric": "vacancy_current", "value": "n/a"},
    ]}), encoding="utf-8")
    provider = UserFileProvider(path, env={})
    assert provider.available()
    premium = provider.get("renovation_premium", GEO)
    assert premium.value == 210.0 and premium.unit == "usd_per_unit_month"
    assert premium.provenance.provider == "user_file" and len(premium.provenance.content_hash) == 64
    broad = provider.get("renovation_premium", Geo(metro_slug="testville_zz"))
    assert broad.value == 175.0 and broad.confidence == "high"
    assert provider.get("renovation_premium", GEO, as_of=date(2026, 3, 1)) is None
    assert provider.get("rent_growth_forecast", GEO).series == {"2026Q3": 0.03, "2026Q4": 0.031}
    assert provider.get("vacancy_current", GEO) is None


def test_user_file_csv_with_keyed_rows(tmp_path):
    path = tmp_path / "analyst.csv"
    path.write_text(
        "metric,key,value,metro_slug,as_of\n"
        "effective_rent_by_bed,1br,1325,testville_zz,2026-05-01\n"
        "effective_rent_by_bed,2br,1690,testville_zz,2026-05-01\n"
        "vacancy_stabilized,,0.055,testville_zz,2026-05-01\n",
        encoding="utf-8",
    )
    provider = UserFileProvider(path, env={})
    assert provider.get("effective_rent_by_bed", GEO).series == {"1br": 1325.0, "2br": 1690.0}
    assert provider.get("vacancy_stabilized", GEO).value == 0.055


def test_user_file_bad_content_returns_none(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    assert UserFileProvider(path, env={}).get("vacancy_current", GEO) is None


def test_default_registry_order(tmp_path):
    env = {"PLAT_MARKET_STUDY_ROOT": str(tmp_path / "ms"), "PLAT_SUPPLY_DEMAND_ROOT": str(tmp_path / "sd")}
    assert default_registry(env=env).names == ["plat_market_study_agent", "plat_supply_demand"]

    user = tmp_path / "analyst.json"
    user.write_text(json.dumps([{"metric": "vacancy_current", "value": 0.04}]), encoding="utf-8")
    configured = dict(env, PLAT_MARKET_DATA_USER_FILE=str(user), PLAT_YARDI_MATRIX_CREDENTIALS="x")
    registry = default_registry(env=configured)
    assert registry.names == ["user_file", "yardi_matrix", "plat_market_study_agent", "plat_supply_demand"]
    winner, candidates = registry.resolve("vacancy_current", GEO)
    assert winner.value == 0.04 and len(candidates) == 1

    ms = registry.providers[2]
    assert isinstance(ms, PlatMarketStudyAgentProvider) and ms.root == tmp_path / "ms"
    assert isinstance(registry.providers[3], PlatSupplyDemandProvider)

    override = dict(env, PLAT_MARKET_DATA_PROVIDERS="plat_supply_demand, plat_market_study_agent")
    assert default_registry(env=override).names == ["plat_supply_demand", "plat_market_study_agent"]
    assert default_registry({"providers": ["costar"]}, env=env).names == ["costar"]
    with pytest.raises(ValueError, match="unknown market-data providers"):
        default_registry({"providers": ["nope"]}, env=env)


def test_feeder_roots_honor_env_and_sibling_defaults(tmp_path):
    assert market_study_root({"PLAT_MARKET_STUDY_ROOT": str(tmp_path / "x")}) == tmp_path / "x"
    assert supply_demand_root({"PLAT_SUPPLY_DEMAND_ROOT": str(tmp_path / "y")}) == tmp_path / "y"
    assert market_study_root({}, siblings=tmp_path) == tmp_path / "plat-market-study-agent"
    assert supply_demand_root({}, siblings=tmp_path) == tmp_path / "plat-supply-demand"
    (tmp_path / "market-study-agent").mkdir()
    assert market_study_root({}, siblings=tmp_path) == tmp_path / "market-study-agent"
    (tmp_path / "plat-market-study-agent").mkdir()
    assert market_study_root({}, siblings=tmp_path) == tmp_path / "plat-market-study-agent"


def test_market_integration_root_honors_env(tmp_path):
    env = dict(os.environ, PLAT_MARKET_STUDY_ROOT=str(tmp_path))
    out = subprocess.run(
        [sys.executable, "-c", "import engine.market_integration as m; print(m.MARKET_AGENT_ROOT)"],
        cwd=REPO, env=env, capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() == str(tmp_path)


def test_feeder_providers_unavailable_without_parquet_support(tmp_path, monkeypatch):
    import engine.market_data.providers.market_study as ms
    import engine.market_data.providers.supply_demand as sd

    no_extra = lambda: (False, "missing optional dependency pyarrow; install plat-multifamily-underwriting[market]")  # noqa: E731
    monkeypatch.setattr(ms, "parquet_support", no_extra)
    monkeypatch.setattr(sd, "parquet_support", no_extra)
    for provider in (PlatMarketStudyAgentProvider(tmp_path, env={}), PlatSupplyDemandProvider(tmp_path, env={})):
        assert provider.available() is False
        assert "[market]" in provider.unavailable_reason
        assert provider.get("rent_growth_forecast", GEO) is None


def test_readme_metric_table_matches_code():
    text = (REPO / "engine" / "market_data" / "README.md").read_text(encoding="utf-8")
    section = text.split("## Metrics (v1)")[1].split("##")[0]
    rows = re.findall(r"^\| `([a-z_]+)` \| `([a-z_]+)` \|", section, re.MULTILINE)
    assert dict(rows) == {name: spec.unit for name, spec in METRICS.items()}


def test_run_underwriting_never_calls_providers(minimal_deal_inputs, monkeypatch):
    import engine.market_data.registry as registry_module
    from engine.engine import run_underwriting

    def forbidden(*args, **kwargs):
        raise AssertionError("run_underwriting must not touch market-data providers")

    monkeypatch.setattr(registry_module.MarketDataRegistry, "resolve", forbidden)
    monkeypatch.setattr(registry_module, "default_registry", forbidden)
    for cls in (PlatMarketStudyAgentProvider, PlatSupplyDemandProvider, UserFileProvider, SyntheticProvider):
        monkeypatch.setattr(cls, "get", forbidden)
    first = run_underwriting(copy.deepcopy(minimal_deal_inputs))
    second = run_underwriting(copy.deepcopy(minimal_deal_inputs))
    assert json.dumps(first, sort_keys=True, default=str) == json.dumps(second, sort_keys=True, default=str)
