"""runs/ingest_deal.py flags and validation output (ADR 0003)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ROLL = REPO_ROOT / "tests" / "fixtures" / "sample_rent_roll.csv"
T12 = REPO_ROOT / "tests" / "fixtures" / "sample_t12.csv"
PRICED = ("--purchase-price", "12000000", "--ltv", "0.6", "--rate", "0.06", "--amort", "30")


def _run_ingest(monkeypatch, capsys, tmp_path: Path, *flags: str) -> tuple[int, dict, str, str]:
    from runs import ingest_deal

    output = tmp_path / "deal.json"
    argv = [
        "ingest_deal.py",
        "--property-id", "Synthetic CLI Deal",
        "--rent-roll", str(ROLL),
        "--t12", str(T12),
        "--start", "2026-05",
        "--end", "2031-04",
        "--no-market-data",
        "--output", str(output),
        *flags,
    ]
    monkeypatch.setattr(sys, "argv", argv)
    code = ingest_deal.main()
    captured = capsys.readouterr()
    deal = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
    return code, deal, captured.out, captured.err


def test_validation_output_shows_missing_millage_for_a_priced_deal(monkeypatch, capsys, tmp_path):
    code, _deal, out, _err = _run_ingest(
        monkeypatch, capsys, tmp_path, *PRICED, "--exit-cap", "0.06", "--validate",
    )
    assert code == 0
    assert "Validation: FAIL" in out
    assert "ERROR [PROPERTY_TAX_MILLAGE_MISSING]" in out
