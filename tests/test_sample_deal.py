"""The bundled sample deal (engine/samples/sample_deal, docs/USER_GUIDE.md).

The sample is rebuilt by its own script, ships as package data, and runs end
to end through the same CLIs the guide shows: ingest (gate PASS), run, and
the target cash-on-cash backsolve at the guide's illustrative 4.00% treasury.
The guide quotes the sample's numbers, so a change that moves them fails
here until "Your first 10 minutes" in docs/USER_GUIDE.md is updated.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from engine.samples import SAMPLE_DEAL_DIR, load_sample_profile, sample_document_paths, sample_ingest_argv
from engine.samples.sample_deal import build_sample

REPO_ROOT = Path(__file__).resolve().parent.parent
_venv_python = REPO_ROOT / ".venv" / "bin" / "python"
PYTHON = _venv_python if _venv_python.exists() else Path(sys.executable)
GUIDE = REPO_ROOT / "docs" / "USER_GUIDE.md"
SAMPLE_FILES = ("rent_roll.xlsx", "t12.xlsx", "profile.json")


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(PYTHON), *args], capture_output=True, text=True, cwd=str(REPO_ROOT)
    )


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# ── the files ────────────────────────────────────────────────────────────


def test_build_sample_reproduces_the_committed_files():
    assert build_sample.stale_files() == [], "run python engine/samples/sample_deal/build_sample.py"
    assert _run("engine/samples/sample_deal/build_sample.py", "--check").returncode == 0


def test_a_fresh_build_matches_cell_for_cell(tmp_path):
    build_sample.write(tmp_path)
    for name in ("rent_roll.xlsx", "t12.xlsx"):
        assert build_sample.sheet_values(tmp_path / name) == build_sample.sheet_values(SAMPLE_DEAL_DIR / name)
    assert (tmp_path / "profile.json").read_bytes() == (SAMPLE_DEAL_DIR / "profile.json").read_bytes()
    assert build_sample.stale_files(tmp_path) == []


def test_check_mode_catches_an_edited_file(tmp_path):
    build_sample.write(tmp_path)
    profile = _load(tmp_path / "profile.json")
    profile["ingest"]["purchase_price"] += 1
    (tmp_path / "profile.json").write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    assert build_sample.stale_files(tmp_path) == ["profile.json"]


def test_the_sample_is_synthetic_and_its_profile_keys_are_ingest_flags():
    profile = load_sample_profile()
    assert profile["synthetic"] is True
    assert "synthetic" in profile["name"]
    sys.path.insert(0, str(REPO_ROOT))
    from runs.ingest_deal import build_parser

    args = build_parser().parse_args(sample_ingest_argv("deal.json"))
    for key, value in profile["ingest"].items():
        assert hasattr(args, key), key
        assert getattr(args, key) is not None, key
    assert args.rent_roll == sample_document_paths()["rent_roll"]
    assert args.t12 == sample_document_paths()["t12"]


def test_the_sample_ships_as_package_data():
    tomllib = pytest.importorskip("tomllib")
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = config["tool"]["setuptools"]["package-data"]["engine.samples.sample_deal"]
    for name in SAMPLE_FILES:
        assert any(Path(name).match(pattern) for pattern in patterns), name
    assert (SAMPLE_DEAL_DIR / "__init__.py").exists()
    assert (SAMPLE_DEAL_DIR.parent / "__init__.py").exists()


# ── end to end ───────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def sample_run(tmp_path_factory) -> dict[str, Any]:
    out = tmp_path_factory.mktemp("sample")
    deal = out / "deal.json"
    pricing = load_sample_profile()["pricing"]
    ingest = _run("runs/ingest_deal.py", *sample_ingest_argv(deal))
    run = _run("runs/run_deal.py", str(deal), "--output-dir", str(out / "run"), "--xlsx")
    price = _run(
        "-m", "engine.backsolve", "--canonical-json", str(deal), "--output-dir", str(out / "price"),
        "--policy-version", "plat.backsolve-policy/1", "--strategy", pricing["strategy"],
        "--year-built", str(load_sample_profile()["ingest"]["year_built"]),
        "--target-coc", str(pricing["target_coc"]),
        "--benchmark-5yr-treasury", str(pricing["benchmark_5yr_treasury"]),
        "--benchmark-as-of", pricing["benchmark_as_of"], "--benchmark-source", pricing["benchmark_source"],
    )
    return {"out": out, "deal": deal, "ingest": ingest, "run": run, "price": price}


def test_ingest_passes_every_gate(sample_run):
    assert sample_run["ingest"].returncode == 0, sample_run["ingest"].stdout + sample_run["ingest"].stderr
    canonical = _load(sample_run["deal"])
    gate = canonical["metadata"]["ingest_gate"]
    assert gate["status"] == "PASS"
    assert gate["blockers"] == []
    assert gate["t12_reconciliation"]["status"] == "PASS"
    rent_roll = gate["rent_roll_reconciliation"]
    assert rent_roll["status"] == "PASS"
    assert {rent_roll[c]["status"] for c in ("units", "vacant_units", "market_rent_total", "rent_charges_total")} == {
        "PASS"
    }
    assert "Ingest gate: PASS" in sample_run["ingest"].stdout
    assert sum(c["unit_count"] for c in canonical["unit_cohorts"]) == 128
    assert canonical["deal_profile"]["year_built"] == 1985


def test_ingest_shows_the_teaching_points(sample_run):
    sheet = _load(sample_run["out"] / "deal.assumptions.json")
    rows = {row["key"]: row for row in sheet["rows"]}
    r_and_m = rows["r_and_m_per_unit"]
    assert r_and_m["value"] == 750.0
    assert r_and_m["guardrail_status"] == "below_floor"
    assert r_and_m["extra"]["guardrail"]["floor"] == 900.0
    assert r_and_m["extra"]["guardrail"]["action"] == "warned"
    assert rows["r_and_m_payroll_credit"]["value"] == 0.0
    assert rows["immediate_capex"]["source"] == "analyst"
    assert rows["recurring_capex_per_unit"]["source"] == "house:recurring_capex_vintage"
    assert rows["exit_cap_rate"]["source"] == "house:exit_cap_spread"
    validation = _load(sample_run["out"] / "run" / "validation.json")
    codes = {issue["code"] for issue in validation["issues"]}
    assert validation["status"] == "PASS"
    assert {"GUARDRAIL_BELOW_FLOOR", "CAPEX_PCA_MISSING"} <= codes
    assert not any(issue["severity"] == "ERROR" for issue in validation["issues"])


def test_run_writes_its_outputs(sample_run):
    assert sample_run["run"].returncode == 0, sample_run["run"].stdout + sample_run["run"].stderr
    run_dir = sample_run["out"] / "run"
    for name in ("canonical.json", "validation.json", "results.json", "assumptions.json", "assumptions.html",
                 "underwriting.xlsx"):
        assert (run_dir / name).is_file(), name


def test_backsolve_converges_and_enforces_the_r_and_m_floor(sample_run):
    assert sample_run["price"].returncode == 0, sample_run["price"].stdout + sample_run["price"].stderr
    summary = _load(sample_run["out"] / "price" / "backsolve_summary.json")
    assert summary["status"] == "converged"
    assert summary["cash_on_cash_year_1_exact"] == pytest.approx(0.08, abs=1e-6)
    enforced = [f for f in summary["policy_summary"]["guardrails"]["findings"] if f["action"] == "enforced"]
    assert [f["key"] for f in enforced] == ["r_and_m_per_unit"]
    assert (sample_run["out"] / "price" / "assumptions.html").is_file()


def test_backsolve_vacancy_evidence_reads_the_sample_rent_roll(sample_run):
    """UPL-107: the rent-roll vacancy evidence is the roll's 7 of 128 vacant, not 0.0.

    UPL-108 (ADR 0003 decision o): it is evidence, not a floor; the floor is the
    1980s vintage floor and the stabilized vacancy build is shown beside it.
    """
    summary = _load(sample_run["out"] / "price" / "backsolve_summary.json")
    policy = summary["policy_summary"]
    anchor = policy["physical_vacancy_anchor_summary"]
    assert anchor["evidence_candidates"]["rent_roll_vacant"] == pytest.approx(7 / 128)
    assert anchor["rent_roll_vacancy_evidence"]["source"] == "ingest_gate.rent_roll_reconciliation"
    assert policy["physical_vacancy_floor"] == 0.05
    assert anchor["vacancy_build"]["market_base"]["level"] == "house"


def _usd(value: float) -> str:
    return f"${value:,.0f}"


def _pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def test_the_user_guide_quotes_the_sample_numbers(sample_run):
    """docs/USER_GUIDE.md "Your first 10 minutes" must match what the sample produces."""
    guide = GUIDE.read_text(encoding="utf-8")
    results = _load(sample_run["out"] / "run" / "results.json")
    metrics = results["metrics"]
    coc = metrics["cash_on_cash"]["components"]
    summary = _load(sample_run["out"] / "price" / "backsolve_summary.json")
    grid = {p["offset_bps"]: p for p in results["exit_cap_sensitivity"]["points"]}
    expected = {
        "asking price": _usd(load_sample_profile()["ingest"]["purchase_price"]),
        "year-1 NOI": _usd(metrics["noi"]["year_1_noi"]),
        "year-1 debt service": _usd(coc["debt_service_year_1"]),
        "year-1 levered cash flow": _usd(coc["leveraged_cash_flow_year_1"]),
        "equity": _usd(coc["equity_basis"]),
        "year-1 CoC": _pct(metrics["cash_on_cash"]["cash_on_cash_year_1_exact"]),
        "unlevered IRR": _pct(metrics["irr"]["unlevered_irr"]),
        "levered IRR": _pct(metrics["irr"]["levered_irr"]),
        "levered EM": f"{metrics['equity_multiple']['levered_em']:.2f}x",
        "min DSCR (NCF)": f"{metrics['dscr']['minimum_dscr']:.2f}x",
        "min DSCR (NOI)": f"{metrics['dscr']['noi_before_capex']['minimum_dscr']:.2f}x",
        "going-in cap": _pct(metrics["yields"]["going_in_cap_rate"]),
        "exit cap": _pct(metrics["yields"]["exit_cap_rate"]),
        "forward NOI": _usd(metrics["exit"]["forward_noi"]),
        "exit value": _usd(metrics["exit"]["gross_sale_price"]),
        "grid +50 levered IRR": _pct(grid[50]["levered_irr"]),
        "grid +100 levered IRR": _pct(grid[100]["levered_irr"]),
        "year-1 tax": _usd(results["property_tax_calculation"]["annual_ad_valorem_tax"]),
        "solved price": _usd(summary["solved_purchase_price"]),
        "solved price per unit": _usd(summary["solved_purchase_price"] / 128),
        "pricing loan": _usd(summary["loan_amount"]),
        "pricing loan rate": _pct(summary["interest_rate"]),
        "pricing equity": _usd(summary["solved_total_equity_basis"]),
        "pricing levered IRR": _pct(summary["levered_irr"]),
        "pricing min DSCR (NCF)": f"{summary['minimum_dscr']:.2f}x",
        "pricing min DSCR (NOI)": f"{summary['minimum_dscr_noi_before_capex']:.2f}x",
        "pricing going-in cap": _pct(summary["going_in_cap_rate"]),
        "pricing exit cap": _pct(summary["exit_cap_rate"]),
    }
    missing = {name: text for name, text in expected.items() if text not in guide}
    assert missing == {}, f"update docs/USER_GUIDE.md 'Your first 10 minutes': {missing}"
    assert "illustrative" in guide
