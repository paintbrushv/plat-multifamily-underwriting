"""Backsolve's exit cap rate comes from the registry's market_cap_rate."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from engine import backsolve
from engine.market_data import Geo, MarketDataRegistry, SyntheticProvider

CAPS = {"2030Q1": 0.0550, "2030Q2": 0.0560, "2031Q1": 0.0570, "2031Q2": 0.0590}


def _canonical(market="Dallas-Fort Worth", exit_month="2031-06"):
    return {
        "metadata": {"market": market},
        "exit_assumptions": {"exit_month": exit_month},
    }


def _registry(data):
    return MarketDataRegistry([SyntheticProvider(data, name="synthetic")])


def _isolate_env(monkeypatch, tmp_path: Path, market_study_root: Path | None = None) -> None:
    monkeypatch.setenv("PLAT_MARKET_STUDY_ROOT", str(market_study_root or tmp_path / "missing-ms"))
    monkeypatch.setenv("PLAT_SUPPLY_DEMAND_ROOT", str(tmp_path / "missing-sd"))
    for name in ("PLAT_MARKET_DATA_PROVIDERS", "PLAT_MARKET_DATA_USER_FILE", "PLAT_MARKET_STUDY_COMPS"):
        monkeypatch.delenv(name, raising=False)


def test_exit_cap_rate_comes_from_registry_market_cap_rate():
    registry = _registry({("market_cap_rate", Geo(metro_slug="dallas_fort_worth")): CAPS})
    rate, source = backsolve._costar_exit_cap_rate_for_case(_canonical(), None, registry=registry)
    assert rate == Decimal(str((0.0570 + 0.0590) / 2))
    assert source == {
        "source": "synthetic fixture market_cap_rate",
        "metro": "Dallas-Fort Worth",
        "exit_year": 2031,
        "rate": float(rate),
    }


def test_exit_year_past_the_series_holds_the_last_quarter():
    registry = _registry({("market_cap_rate", Geo(metro_slug="dallas_fort_worth")): CAPS})
    rate, source = backsolve._costar_exit_cap_rate_for_case(_canonical(exit_month="2034-06"), None, registry=registry)
    assert rate == Decimal("0.059") and source["exit_year"] == 2034


def test_scalar_cap_rate_and_market_slug_metadata():
    registry = _registry({("market_cap_rate", Geo(metro_slug="testville_zz")): 0.061})
    rate, source = backsolve._costar_exit_cap_rate_for_case(_canonical(market="testville_zz"), None, registry=registry)
    assert rate == Decimal("0.061") and source["metro"] == "testville zz"


def test_no_data_returns_none_none():
    empty = _registry({})
    assert backsolve._costar_exit_cap_rate_for_case(_canonical(), None, registry=empty) == (None, None)
    assert backsolve._costar_exit_cap_rate_for_case({"metadata": {}}, None, registry=empty) == (None, None)
    before = _registry({("market_cap_rate", Geo(metro_slug="dallas_fort_worth")): CAPS})
    assert backsolve._costar_exit_cap_rate_for_case(
        _canonical(exit_month="2025-06"), None, registry=before) == (None, None)


def test_registry_failure_returns_none_none():
    class Broken:
        def resolve(self, *args, **kwargs):
            raise RuntimeError("boom")

    assert backsolve._costar_exit_cap_rate_for_case(_canonical(), None, registry=Broken()) == (None, None)


def test_default_registry_without_feeder_data_returns_none_none(tmp_path, monkeypatch):
    _isolate_env(monkeypatch, tmp_path)
    assert backsolve._costar_exit_cap_rate_for_case(_canonical(), None) == (None, None)


def test_default_registry_reads_market_study_parquet(tmp_path: Path, monkeypatch):
    pytest.importorskip("pandas")
    pytest.importorskip("pyarrow")
    from tests.market_data_feeders import costar_row, write_costar_submarket

    stem = "dallas_fort_worth_tx_usa_alpha"
    rows = [costar_row("2026 Q2", "All", is_forecast=False, stem=stem, market_cap_rate=0.052)]
    rows += [costar_row(f"2031 Q{q}", "All", is_forecast=True, stem=stem, market_cap_rate=0.054 + 0.002 * q)
             for q in (1, 2, 3, 4)]
    write_costar_submarket(tmp_path, stem, rows)
    _isolate_env(monkeypatch, tmp_path, market_study_root=tmp_path)

    first = backsolve._costar_exit_cap_rate_for_case(_canonical(), None)
    second = backsolve._costar_exit_cap_rate_for_case(_canonical(), None)
    assert first == second  # deterministic
    rate, source = first
    assert float(rate) == pytest.approx(0.059)
    assert source["source"].startswith("CoStar submarket export via plat-market-study-agent")
