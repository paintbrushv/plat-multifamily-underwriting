"""Regression: the runs/ backsolve shim must actually run the CLI.

runs/backsolve_price_for_target_coc.py re-exports engine.backsolve but had no
``if __name__ == "__main__"`` guard, so every invocation, even ``--help``,
printed nothing, wrote nothing and exited 0. That reads as a completed solve
and defeats the exit-code contract in docs/BACKSOLVE_API.md (exit 2 when the
search is infeasible).
"""
from __future__ import annotations

import json
import runpy
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

import engine.backsolve as backsolve

REPO = Path(__file__).resolve().parents[1]
SHIM = REPO / "runs" / "backsolve_price_for_target_coc.py"


def test_shim_help_prints_usage_and_exits_zero():
    completed = subprocess.run(
        [sys.executable, str(SHIM), "--help"],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr
    assert "usage:" in completed.stdout
    assert "--canonical-json" in completed.stdout


def _stub_solver(monkeypatch, tmp_path: Path, *, min_price: str, max_price: str) -> Path:
    """Stub pricing so Year-1 CoC = 8% - 0.02% x price (7% target met up to price 50)."""
    canonical_path = tmp_path / "canonical.json"
    canonical_path.write_text(json.dumps({
        "metadata": {
            "property_summary": {
                "property_tax_policy": {
                    "millage_rate_mills": 25.0,
                    "assessment_ratio": 1.0,
                    "source": "analyst",
                    "source_locator": "tests:backsolve_cli_shim",
                    "analyst_override": False,
                }
            }
        }
    }))
    output_dir = tmp_path / "pricing"

    monkeypatch.setattr(backsolve, "_prepare_house_assumptions", lambda canonical, **_kw: (canonical, {}))
    monkeypatch.setattr(backsolve, "_preview_projected_noi", lambda _canonical: Decimal("100"))

    def build_case(_canonical, *, price, **_kwargs):
        return {
            "purchase_assumptions": {"purchase_price": float(price), "total_equity_basis": 100.0},
            "debt_terms": {"commitment": 0.0, "rate": 0.0, "io_months": 0, "term_months": 60},
            "metadata": {"property_summary": {}},
        }

    def evaluate_case(case):
        price = Decimal(str(case["purchase_assumptions"]["purchase_price"]))
        coc = Decimal("0.08") - price * Decimal("0.0002")
        results = {
            "cashflow": {"by_year": [{"net_operating_income": 100.0}, {"net_operating_income": 100.0}]},
            "metrics": {
                "coc": {
                    "cash_on_cash_year_1": round(float(coc), 4),
                    "cash_on_cash_year_1_exact": float(coc),
                    "free_cf_year_1": float(coc * Decimal("100")),
                    "components": {"equity_basis": 100.0},
                },
                "irr": {"levered_irr": 0.0},
                "dscr": {"minimum_dscr": 0.0},
                "yields": {"going_in_cap_rate": 0.0, "exit_cap_rate": 0.06},
            },
        }
        return results, coc

    monkeypatch.setattr(backsolve, "_build_price_case", build_case)
    monkeypatch.setattr(backsolve, "_evaluate_case", evaluate_case)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SHIM),
            "--canonical-json", str(canonical_path),
            "--output-dir", str(output_dir),
            "--policy-version", "plat.backsolve-policy/1",
            "--benchmark-as-of", "2026-10-03", "--benchmark-source", "synthetic:test",
            "--benchmark-5yr-treasury", "0.04",
            "--exit-cap-rate", "0.06",
            "--min-price", min_price,
            "--max-price", max_price,
            "--max-iterations", "20",
        ],
    )
    return output_dir


def test_infeasible_solve_exits_2_through_the_shim(monkeypatch, tmp_path):
    output_dir = _stub_solver(monkeypatch, tmp_path, min_price="60", max_price="100")

    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(SHIM), run_name="__main__")

    assert exc.value.code == 2
    summary = json.loads((output_dir / "backsolve_summary.json").read_text())
    assert summary["status"] not in {"converged", "ceiling_feasible"}


def test_feasible_solve_writes_results_through_the_shim(monkeypatch, tmp_path):
    output_dir = _stub_solver(monkeypatch, tmp_path, min_price="20", max_price="100")

    runpy.run_path(str(SHIM), run_name="__main__")

    summary = json.loads((output_dir / "backsolve_summary.json").read_text())
    assert summary["status"] == "converged"
    assert summary["maximum_feasible_boundary"]["highest_feasible_price"] == 50.0
