"""runs/ingest_deal.py flags and validation output (ADR 0003)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

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
    assert code == 1  # a failed validation exits non-zero; the output is still written
    assert (tmp_path / "deal.json").is_file()
    assert "Validation: FAIL" in out
    assert "ERROR [PROPERTY_TAX_MILLAGE_MISSING]" in out
    assert "Fix: pass --millage" in out


def test_millage_flag_builds_the_analyst_property_tax_policy(monkeypatch, capsys, tmp_path):
    code, deal, out, _err = _run_ingest(
        monkeypatch, capsys, tmp_path, *PRICED, "--exit-cap", "0.06", "--millage", "22.5", "--validate",
    )
    assert code == 0
    assert deal["metadata"]["property_summary"]["property_tax_policy"] == {
        "millage_rate_mills": 22.5,
        "assessment_ratio": 1.0,
        "source": "analyst",
        "source_locator": "cli:--millage",
        "analyst_override": False,
    }
    assert "property tax 22.5 mills per $1,000" in out
    assert "Validation: PASS" in out
    assert "WARN [" in out  # warnings alone keep exit 0


def test_assessment_ratio_is_an_analyst_override(monkeypatch, capsys, tmp_path):
    code, deal, _out, _err = _run_ingest(
        monkeypatch, capsys, tmp_path, *PRICED, "--millage", "20", "--assessment-ratio", "0.7",
    )
    assert code == 0
    policy = deal["metadata"]["property_summary"]["property_tax_policy"]
    assert policy["millage_rate_mills"] == 20.0
    assert policy["assessment_ratio"] == 0.7
    assert policy["analyst_override"] is True
    assert policy["source"] == "analyst"
    assert policy["source_locator"] == "cli:--millage,--assessment-ratio"


def test_assessment_ratio_without_millage_is_bad_input(monkeypatch, capsys, tmp_path):
    code, deal, _out, err = _run_ingest(monkeypatch, capsys, tmp_path, "--assessment-ratio", "0.8")
    assert code == 2
    assert "--assessment-ratio requires --millage" in err
    assert deal == {}


@pytest.mark.parametrize("mills", ["0", "-3"])
def test_unusable_millage_is_bad_input(monkeypatch, capsys, tmp_path, mills):
    code, deal, _out, err = _run_ingest(monkeypatch, capsys, tmp_path, f"--millage={mills}")
    assert code == 2
    assert "invalid_property_tax_millage" in err
    assert deal == {}


def test_profile_flags_land_in_the_deal_profile(monkeypatch, capsys, tmp_path):
    code, deal, _out, _err = _run_ingest(
        monkeypatch, capsys, tmp_path,
        "--deal-type", "value_add", "--year-built", "1984", "--star-tier", "3", "--asset-class", "B",
        "--msa", "Synthetic Metro", "--metro-slug", "synthetic_metro",
    )
    assert code == 0
    assert deal["deal_profile"] == {
        "deal_type": "value_add",
        "msa": {"name": "Synthetic Metro", "metro_slug": "synthetic_metro"},
        "year_built": 1984,
        "asset_class": "B",
        "star_tier": "3",
    }


@pytest.mark.parametrize("flag, value", [("--star-tier", "2"), ("--asset-class", "D")])
def test_profile_flag_choices_are_enforced(flag, value):
    from runs.ingest_deal import build_parser

    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args([flag, value])
    assert exc.value.code == 2


def test_canonical_mode_merges_profile_flags_and_millage(monkeypatch, capsys, tmp_path):
    from engine.ingest.ingestion_pipeline import build_deal_from_documents
    from runs import ingest_deal

    base = build_deal_from_documents(
        property_id="Synthetic CLI Deal", rent_roll_path=ROLL, t12_path=T12,
        analysis_start="2026-05", analysis_end="2031-04", deal_type="value_add",
        msa="Synthetic Metro", metro_slug="synthetic_metro", market=None,
    )
    canonical = tmp_path / "canonical.json"
    canonical.write_text(json.dumps(base), encoding="utf-8")
    output = tmp_path / "out.json"
    monkeypatch.setattr(sys, "argv", [
        "ingest_deal.py", "--canonical-json", str(canonical), "--output", str(output),
        "--no-market-data", "--year-built", "1979", "--star-tier", "1_2", "--millage", "18",
    ])
    assert ingest_deal.main() == 0
    capsys.readouterr()
    deal = json.loads(output.read_text(encoding="utf-8"))
    assert deal["deal_profile"] == {
        "deal_type": "value_add",
        "msa": {"name": "Synthetic Metro", "metro_slug": "synthetic_metro"},
        "year_built": 1979,
        "star_tier": "1_2",
    }
    policy = deal["metadata"]["property_summary"]["property_tax_policy"]
    assert policy["millage_rate_mills"] == 18.0
    assert policy["source_locator"] == "cli:--millage"


def test_pipeline_property_tax_and_profile_kwargs():
    from engine.ingest.ingestion_pipeline import build_deal_from_documents

    deal = build_deal_from_documents(
        property_id="Synthetic Pipeline Deal", rent_roll_path=ROLL, t12_path=T12,
        analysis_start="2026-05", analysis_end="2031-04", market=None,
        year_built=1996, star_tier="4_5", asset_class="A",
        property_tax_millage=21.25, property_tax_assessment_ratio=0.85,
    )
    assert deal["deal_profile"]["year_built"] == 1996
    assert deal["deal_profile"]["star_tier"] == "4_5"
    assert deal["deal_profile"]["asset_class"] == "A"
    assert deal["metadata"]["property_summary"]["property_tax_policy"] == {
        "millage_rate_mills": 21.25,
        "assessment_ratio": 0.85,
        "source": "analyst",
        "source_locator": "analyst_input:property_tax_millage",
        "analyst_override": True,
    }


def test_pipeline_rejects_an_assessment_ratio_without_millage():
    from engine.ingest.ingestion_pipeline import build_deal_from_documents

    with pytest.raises(ValueError, match="requires property_tax_millage"):
        build_deal_from_documents(
            property_id="Synthetic Pipeline Deal", rent_roll_path=ROLL, t12_path=T12,
            analysis_start="2026-05", analysis_end="2031-04", market=None,
            property_tax_assessment_ratio=0.9,
        )


def test_analyst_millage_settles_om_tax_flags_and_keeps_the_evidence():
    from engine.ingest.ingestion_pipeline import (
        analyst_property_tax_policy,
        apply_analyst_property_tax_policy,
    )

    candidates = [{"producer": "broker_snapshot_mills", "millage_rate_mills": 19.0}]
    deal = {
        "metadata": {
            "intake_sanity_flags": ["conflicting_property_tax_millage", "om_extraction_unavailable"],
            "property_summary": {"property_tax_evidence_candidates": candidates},
        }
    }
    apply_analyst_property_tax_policy(deal, analyst_property_tax_policy(21.0, source_locator="cli:--millage"))
    assert deal["metadata"]["intake_sanity_flags"] == ["om_extraction_unavailable"]
    summary = deal["metadata"]["property_summary"]
    assert summary["property_tax_evidence_candidates"] == candidates
    assert summary["property_tax_policy"]["millage_rate_mills"] == 21.0
