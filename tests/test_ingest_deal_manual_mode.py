from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from engine.ingest.ingestion_pipeline import build_deal_from_documents


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

ROLL = REPO_ROOT / "tests" / "fixtures" / "sample_rent_roll.csv"
T12 = REPO_ROOT / "tests" / "fixtures" / "sample_t12.csv"
_venv_python = REPO_ROOT / ".venv" / "bin" / "python"
VENV_PYTHON = _venv_python if _venv_python.exists() else Path(sys.executable)


class TestIngestDealManualMode:
    def test_help_shows_manual_json_flags(self):
        from runs.ingest_deal import build_parser

        help_text = build_parser().format_help()
        assert "--canonical-json" in help_text
        assert "--partial-json" in help_text

    def test_canonical_json_mode_requires_no_document_flags(self, tmp_path: Path):
        canonical = build_deal_from_documents(
            property_id="Manual Canonical Deal",
            rent_roll_path=ROLL,
            t12_path=T12,
            analysis_start="2026-05",
            analysis_end="2031-04",
        )
        canonical_path = tmp_path / "manual_canonical.json"
        canonical_path.write_text(json.dumps(canonical), encoding="utf-8")
        output_path = tmp_path / "roundtrip.json"

        result = subprocess.run(
            [
                str(VENV_PYTHON),
                "runs/ingest_deal.py",
                "--canonical-json",
                str(canonical_path),
                "--output",
                str(output_path),
                "--validate",
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )

        assert result.returncode == 0, result.stderr
        roundtrip = json.loads(output_path.read_text(encoding="utf-8"))
        assert roundtrip["metadata"]["deal_id"] == "Manual Canonical Deal"
        assert roundtrip["time_grid"]["analysis_start_date"] == "2026-05-01"

    def test_partial_json_merges_onto_document_derived_base(self, tmp_path: Path):
        partial = {
            "purchase_assumptions": {
                "purchase_price": 12_345_678,
                "equity_contribution": 4_000_000,
                "closing_costs": 0,
            },
            "metadata": {
                "notes": "patched by terminal agent",
            },
        }
        partial_path = tmp_path / "partial.json"
        partial_path.write_text(json.dumps(partial), encoding="utf-8")
        output_path = tmp_path / "merged.json"

        result = subprocess.run(
            [
                str(VENV_PYTHON),
                "runs/ingest_deal.py",
                "--property-id",
                "Patched Deal",
                "--rent-roll",
                str(ROLL),
                "--t12",
                str(T12),
                "--start",
                "2026-05",
                "--end",
                "2031-04",
                "--partial-json",
                str(partial_path),
                "--output",
                str(output_path),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )

        assert result.returncode == 0, result.stderr
        merged = json.loads(output_path.read_text(encoding="utf-8"))
        assert merged["purchase_assumptions"]["purchase_price"] == 12_345_678
        assert merged["metadata"]["purchase_assumptions_source"] == "analyst_override"
        assert merged["metadata"]["analyst_review_required"] is False
        assert merged["metadata"]["notes"] == "patched by terminal agent"

    def test_partial_json_can_supply_property_id_when_flag_omitted(self, tmp_path: Path):
        partial = {
            "metadata": {
                "deal_id": "Deal From Partial",
            },
            "purchase_assumptions": {
                "purchase_price": 9_999_999,
                "equity_contribution": 3_000_000,
                "closing_costs": 0,
            },
        }
        partial_path = tmp_path / "partial_with_id.json"
        partial_path.write_text(json.dumps(partial), encoding="utf-8")
        output_path = tmp_path / "from_partial_id.json"

        result = subprocess.run(
            [
                str(VENV_PYTHON),
                "runs/ingest_deal.py",
                "--rent-roll",
                str(ROLL),
                "--t12",
                str(T12),
                "--start",
                "2026-05",
                "--end",
                "2031-04",
                "--partial-json",
                str(partial_path),
                "--output",
                str(output_path),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )

        assert result.returncode == 0, result.stderr
        merged = json.loads(output_path.read_text(encoding="utf-8"))
        assert merged["metadata"]["deal_id"] == "Deal From Partial"
