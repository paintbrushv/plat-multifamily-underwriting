"""No silent market-data fallback: the status block, fallback notes, CLI output and the doctor.

The feeder roots are the empty directories conftest points the environment
at, or synthetic roots written here. Synthetic slugs only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("pandas")
pytest.importorskip("pyarrow")

import tests.deal_library as dl  # noqa: E402
from engine.assumptions import resolve_assumptions  # noqa: E402
from engine.assumptions.profile import DealProfile  # noqa: E402
from engine.assumptions.register import build_register  # noqa: E402
from engine.assumptions.register_html import render_register_html  # noqa: E402
from engine.market_data import Geo, default_registry  # noqa: E402
from engine.market_data.diagnostics import (  # noqa: E402
    FALLBACK_PREFIX,
    SLOT_METRICS,
    market_data_status,
)
from engine.validator import validate_deal  # noqa: E402
from tests.market_data_feeders import costar_row, write_costar_submarket  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
_venv_python = REPO_ROOT / ".venv" / "bin" / "python"
PYTHON = _venv_python if _venv_python.exists() else Path(sys.executable)
ROLL = REPO_ROOT / "tests" / "fixtures" / "sample_rent_roll.csv"
T12 = REPO_ROOT / "tests" / "fixtures" / "sample_t12.csv"
NO_MARKET = "b_stabilized_2010s_no_market"
WITH_MARKET = "a_stabilized_1990s"
GEO = Geo(cbsa_code="99977", metro_slug="testville_zz", submarket="North Lake/Flats")
METRO = "testville_zz_usa"


def _entries(canonical):
    return canonical["assumptions"]["entries"]


@pytest.fixture(scope="module")
def empty_feeders() -> dict:
    """Library deal b ingested with the default registry over the empty feeder roots."""
    return dl.ingest(dl.load_deal(NO_MARKET), market=default_registry())


def test_slot_metrics_match_the_resolver_specs():
    from engine.assumptions.resolver import _specs_for

    from engine.market_data.diagnostics import COMPONENT_SLOTS

    specs = _specs_for(DealProfile(deal_type="value_add", year_built=1985))
    assert {s.key: s.market_metrics for s in specs if s.market_metrics} | COMPONENT_SLOTS == SLOT_METRICS


def test_status_names_each_provider_root_and_why_it_supplied_nothing():
    registry = default_registry()
    status = market_data_status(registry, GEO)
    assert [p["name"] for p in status["providers"]] == ["plat_market_study_agent", "plat_supply_demand"]
    for report, env_name in zip(status["providers"], ("PLAT_MARKET_STUDY_ROOT", "PLAT_SUPPLY_DEMAND_ROOT")):
        assert report["available"] is False
        assert report["root"] == os.environ[env_name]
        assert report["root_origin"] == f"env {env_name}"
        assert report["root_found"] is False
        assert report["metrics_supplied"] == []
    assert all(not slot["supplied"] for slot in status["slots"].values())
    note = status["slots"]["rent_growth"]["note"]
    assert note.startswith(FALLBACK_PREFIX)
    assert "rent_growth_forecast or rent_growth_long_run" in note
    assert "metro testville_zz, CBSA 99977, submarket 'North Lake/Flats'" in note
    assert f"plat_market_study_agent: plat-market-study-agent root not found: {os.environ['PLAT_MARKET_STUDY_ROOT']}" in note
    assert "plat_supply_demand: plat-supply-demand root not found" in note
    assert status["summary"].startswith("0 of 6 market slots supplied")
    assert status["config"]["provider_order"] == "default order"
    json.dumps(status)  # JSON-serializable


def test_fallback_entries_say_which_provider_and_root_fell_back(empty_feeders):
    entries = _entries(empty_feeders)
    rent = entries["rent_growth"]
    assert rent["source"] == "house:long_run_rent_growth"
    assert "Market data fell back: no provider supplied rent_growth_forecast or rent_growth_long_run" in rent["basis"]
    assert os.environ["PLAT_MARKET_STUDY_ROOT"] in rent["basis"]
    assert rent["detail"]["market_data"]["supplied"] is False
    assert rent["detail"]["market_data"]["metrics"] == ["rent_growth_forecast", "rent_growth_long_run"]
    # Only a house or engine fallback stands where market data would have.
    for key, entry in entries.items():
        if FALLBACK_PREFIX in entry["basis"]:
            assert entry["source"].startswith("house:") or entry["source"] == "engine_fallback", key
    status = empty_feeders["assumptions"]["market_data_status"]
    assert status["slots"]["rent_growth"]["entry_source"] == "house:long_run_rent_growth"
    assert status["geography"]["metro_slug"]
    assert validate_deal(empty_feeders).status == "PASS"


def test_supplied_slots_carry_no_fallback_note():
    canonical = dl.ingest(dl.load_deal(WITH_MARKET))
    rent = _entries(canonical)["rent_growth"]
    assert rent["source"].startswith("market:")
    assert FALLBACK_PREFIX not in rent["basis"]
    slot = canonical["assumptions"]["market_data_status"]["slots"]["rent_growth"]
    assert slot["supplied"] is True and slot["note"] is None
    assert {r["provider"] for r in slot["supplied_by"]} == {rent["source"].split(":", 1)[1]}


def test_reresolving_without_market_data_keeps_the_status_and_notes_once(empty_feeders):
    again = resolve_assumptions(empty_feeders)
    assert again["assumptions"]["market_data_status"] == empty_feeders["assumptions"]["market_data_status"]
    assert _entries(again)["rent_growth"]["basis"].count(FALLBACK_PREFIX) == 1
    assert json.dumps(resolve_assumptions(again), sort_keys=True) == json.dumps(again, sort_keys=True)


def test_a_deal_without_geography_says_no_provider_was_asked(empty_feeders):
    out = resolve_assumptions(empty_feeders, deal_profile={"deal_type": "stabilized"}, market=default_registry())
    status = out["assumptions"]["market_data_status"]
    assert status["geography"] is None and "no geography" in status["summary"]
    assert "no geography" in _entries(out)["rent_growth"]["basis"]


def test_register_shows_the_market_data_status(empty_feeders):
    register = build_register(empty_feeders)
    assert register["market_data"]["summary"].startswith("0 of ")
    html = render_register_html(empty_feeders)
    assert "Market data" in html and "plat_market_study_agent" in html and "fell back" in html.lower()
    assert "market_data" not in build_register(dl.ingest(dl.load_deal(NO_MARKET), market=None))


# -- CLIs ---------------------------------------------------------------------------


def _ingest_cli(monkeypatch, capsys, tmp_path: Path, *flags: str) -> tuple[int, dict, str]:
    from runs import ingest_deal

    output = tmp_path / "deal.json"
    monkeypatch.setattr(sys, "argv", [
        "ingest_deal.py", "--property-id", "Synthetic Status Deal",
        "--rent-roll", str(ROLL), "--t12", str(T12), "--start", "2026-05", "--end", "2031-04",
        "--msa", "99977", "--metro-slug", "testville_zz", "--submarket", "North Lake/Flats",
        "--output", str(output), *flags,
    ])
    code = ingest_deal.main()
    out = capsys.readouterr().out
    return code, json.loads(output.read_text(encoding="utf-8")), out


def test_ingest_prints_the_market_data_status(monkeypatch, capsys, tmp_path):
    code, deal, out = _ingest_cli(monkeypatch, capsys, tmp_path)
    assert code == 0
    assert "Market data status: 0 of 5 market slots supplied" in out
    assert "plat_market_study_agent: NOT AVAILABLE" in out
    assert f"root: {os.environ['PLAT_MARKET_STUDY_ROOT']} (env PLAT_MARKET_STUDY_ROOT; not found)" in out
    assert "rent_growth: FELL BACK -> house:long_run_rent_growth" in out
    assert deal["assumptions"]["market_data_status"]["enabled"] is True


def test_ingest_without_market_data_says_so(monkeypatch, capsys, tmp_path):
    code, deal, out = _ingest_cli(monkeypatch, capsys, tmp_path, "--no-market-data")
    assert code == 0
    assert "Market data status: Market data not used: --no-market-data." in out
    assert "market_data_status" not in deal["assumptions"]


def _run_deal(canonical: dict, tmp_path: Path) -> tuple[subprocess.CompletedProcess, dict]:
    path = tmp_path / "deal.json"
    path.write_text(json.dumps(canonical), encoding="utf-8")
    out = tmp_path / "run"
    proc = subprocess.run(
        [str(PYTHON), "runs/run_deal.py", str(path), "--output-dir", str(out)],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc, json.loads((out / "results.json").read_text(encoding="utf-8"))


def test_run_deal_writes_the_status_into_results_metadata(empty_feeders, tmp_path):
    proc, results = _run_deal(empty_feeders, tmp_path)
    assert results["metadata"]["market_data_status"] == empty_feeders["assumptions"]["market_data_status"]
    assert "Market data status: 0 of 5 market slots supplied" in proc.stdout
    assert "rent_growth: FELL BACK" in proc.stdout


def test_run_deal_says_when_no_status_was_recorded(empty_feeders, tmp_path):
    canonical = json.loads(json.dumps(empty_feeders))
    del canonical["assumptions"]["market_data_status"]
    proc, results = _run_deal(canonical, tmp_path)
    assert results["metadata"]["market_data_status"]["enabled"] is False
    assert "Market data status: Market data not used" in proc.stdout


def _doctor(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(PYTHON), "runs/market_data_doctor.py", *args],
        capture_output=True, text=True, cwd=str(REPO_ROOT), env={**os.environ, **(env or {})},
    )


def test_doctor_reports_missing_feeders_with_next_steps():
    proc = _doctor("--msa", "99977", "--metro-slug", "testville_zz", "--submarket", "North Lake/Flats")
    assert proc.returncode == 1, proc.stdout + proc.stderr  # nothing supplied
    assert "plat_market_study_agent: NOT AVAILABLE" in proc.stdout
    assert "plat_supply_demand: NOT AVAILABLE" in proc.stdout
    assert "transform_costar_excel.py" in proc.stdout and "econ forecast --metro" in proc.stdout


def test_doctor_shows_what_a_synthetic_feeder_supplies(tmp_path):
    stem = f"{METRO}_north_lake_flats"
    rows = [
        costar_row(q, "All", is_forecast=False, stem=stem, vacancy_rate=0.06, stabilized_vacancy=0.055,
                   asking_rent_unit=1500.0, effective_rent_unit=1450.0, market_cap_rate=0.055,
                   inventory_units=1000, under_construction=50)
        for q in ("2025 Q4", "2026 Q1")
    ]
    write_costar_submarket(tmp_path, stem, rows)
    env = {"PLAT_MARKET_STUDY_ROOT": str(tmp_path)}
    proc = _doctor("--metro-slug", "testville_zz", "--submarket", "North Lake/Flats", "--verbose", env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "plat_market_study_agent: available, geography matched" in proc.stdout
    assert "physical_vacancy: market data from plat_market_study_agent (submarket)" in proc.stdout
    assert f"geography.submarket_file: {stem}" in proc.stdout
    data = json.loads(_doctor("--metro-slug", "testville_zz", "--submarket", "North Lake/Flats", "--json",
                              env=env).stdout)
    assert data["slots"]["physical_vacancy"]["supplied"] is True
    assert data["slots"]["rent_growth"]["supplied"] is False


def test_doctor_needs_a_geography():
    proc = _doctor()
    assert proc.returncode == 2
    assert "geography" in proc.stderr
