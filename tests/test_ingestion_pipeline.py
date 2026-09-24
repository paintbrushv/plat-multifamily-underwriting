"""Tests for the ingestion pipeline: T12 + rent roll → canonical deal JSON."""
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from engine.ingest.broker_om_snapshot import stable_property_tax_source_locator
from engine.ingest.ingestion_pipeline import build_deal_from_documents
from engine.ingest.om_parser import OmData, OmUnitType

T12 = Path("tests/fixtures/sample_t12.csv")
ROLL = Path("tests/fixtures/sample_rent_roll.csv")

REQUIRED_KEYS = {
    "schema_version", "metadata", "time_grid", "unit_cohorts",
    "market_rent_curve", "loss_to_lease", "physical_vacancy_curve",
    "collection_loss_curve", "revenue_programs", "program_adoption_curve",
    "opex_table",
}


def _build(**kwargs):
    defaults = dict(
        property_id="Test Property",
        rent_roll_path=ROLL,
        t12_path=T12,
        analysis_start="2026-05",
        analysis_end="2031-04",
    )
    defaults.update(kwargs)
    return build_deal_from_documents(**defaults)


def test_builds_deal_with_required_top_level_keys():
    result = _build()
    assert REQUIRED_KEYS <= result.keys(), f"Missing: {REQUIRED_KEYS - result.keys()}"


def test_unit_cohorts_populated_from_rent_roll():
    result = _build()
    assert len(result["unit_cohorts"]) > 0
    for c in result["unit_cohorts"]:
        assert c["initial_inplace_rent"] > 0


def test_opex_table_populated_from_t12():
    result = _build()
    assert len(result["opex_table"]) > 0
    total_opex = sum(c["base_value"] for c in result["opex_table"])
    assert total_opex > 50_000


def test_market_rent_curve_has_all_cohorts():
    result = _build()
    cohort_ids = {c["cohort_id"] for c in result["unit_cohorts"]}
    mrc_keys = {entry["cohort_id"] for entry in result["market_rent_curve"]}
    assert cohort_ids == mrc_keys


def test_vacancy_curve_has_all_cohorts():
    result = _build()
    cohort_ids = {c["cohort_id"] for c in result["unit_cohorts"]}
    vc_keys = {entry["cohort_id"] for entry in result["physical_vacancy_curve"]}
    assert cohort_ids == vc_keys


def test_vacancy_rate_from_rent_roll():
    result = _build()
    # rent roll fixture has 20% vacancy
    for entry in result["physical_vacancy_curve"]:
        assert abs(entry["vacancy_rate"] - 0.20) < 0.01


def test_vacancy_rate_override():
    result = _build(vacancy_rate_override=0.05)
    for entry in result["physical_vacancy_curve"]:
        assert entry["vacancy_rate"] == pytest.approx(0.05)


def test_collection_loss_default():
    result = _build()
    cl = result["collection_loss_curve"]
    assert len(cl) == 1
    assert cl[0]["loss_rate"] == pytest.approx(0.005)


def test_metadata_populated():
    result = _build()
    meta = result["metadata"]
    assert meta["deal_id"] == "Test Property"
    assert result["schema_version"] == "0.1"


def test_metadata_as_of_date_is_full_date():
    result = _build(analysis_start="2026-05")
    assert result["metadata"]["as_of_date"] == "2026-05-01"


def test_metadata_analyst_defaults_nonempty():
    result = _build(analyst="")
    assert len(result["metadata"]["analyst"]) > 0


def test_time_grid_dates():
    result = _build(analysis_start="2026-05", analysis_end="2031-04")
    tg = result["time_grid"]
    assert tg["analysis_start_date"].startswith("2026-05")
    assert tg["analysis_end_date"].startswith("2031-04")


def test_market_rent_uses_rent_roll_market_rent():
    result = _build()
    mrc = {e["cohort_id"]: e["market_rent"] for e in result["market_rent_curve"]}
    # 1BR/1BA market rent = $950 from fixture. Cohort_id is now lowercased
    # (Wave 3 Task 3.1 / Bug 1.3 fix — "Beal" and "BEAL" must collapse).
    assert mrc.get("1br1ba") == pytest.approx(950.0)


def test_growth_assumptions_present():
    result = _build(rent_growth_rate=0.04)
    ga = result.get("growth_assumptions", {})
    assert ga.get("annual_growth_rate") == pytest.approx(0.04)


def test_growth_assumptions_uses_schema_field_name():
    result = _build()
    ga = result["growth_assumptions"]
    assert "growth_type" in ga
    assert ga["growth_type"] == "annual_compound"


def test_curve_format_flat():
    """Verify curves use flat format (cohort_id, start_period, end_period) not nested segments."""
    result = _build()
    mrc = result["market_rent_curve"][0]
    assert "cohort_id" in mrc
    assert "start_period" in mrc
    assert "end_period" in mrc
    assert "market_rent" in mrc
    assert "segments" not in mrc
    assert "key" not in mrc

    ltl = result["loss_to_lease"][0]
    assert "cohort_id" in ltl
    assert "ltl_percent" in ltl

    vc = result["physical_vacancy_curve"][0]
    assert "cohort_id" in vc
    assert "vacancy_rate" in vc

    cl = result["collection_loss_curve"][0]
    assert "applies_to" in cl
    assert "loss_rate" in cl
    assert "start_period" in cl


# ── Deal economics flags ─────────────────────────────────────────────────


class TestDealEconomics:
    def test_purchase_and_debt_from_flags(self):
        result = _build(
            purchase_price=25_000_000,
            ltv=0.75,
            loan_rate=0.065,
            amort_years=30,
        )
        assert result["purchase_assumptions"]["purchase_price"] == 25_000_000
        assert result["purchase_assumptions"]["equity_contribution"] == pytest.approx(6_250_000)
        assert result["debt_terms"]["commitment"] == pytest.approx(18_750_000)
        assert result["debt_terms"]["rate"] == pytest.approx(0.065)
        assert result["debt_terms"]["amort_years"] == 30

    def test_exit_cap_from_flag(self):
        result = _build(purchase_price=25_000_000, exit_cap_rate=0.055)
        assert result["exit_assumptions"]["exit_cap_rate"] == pytest.approx(0.055)
        # Exit month defaults to analysis end
        assert result["exit_assumptions"]["exit_month"] == "2031-04"

    def test_exit_month_override(self):
        result = _build(purchase_price=25_000_000, exit_cap_rate=0.055, exit_month="2030-06")
        assert result["exit_assumptions"]["exit_month"] == "2030-06"

    def test_fund_assumptions_from_flags(self):
        result = _build(sponsor_equity_pct=0.10, pref_return=0.08)
        fa = result["fund_assumptions"]
        assert fa["sponsor_equity_pct"] == pytest.approx(0.10)
        assert fa["lp_equity_pct"] == pytest.approx(0.90)
        assert fa["preferred_return"] == pytest.approx(0.08)
        assert len(fa["promote_splits"]) > 0

    def test_no_flags_no_sections(self):
        result = _build()
        assert "purchase_assumptions" not in result
        assert "debt_terms" not in result
        assert "exit_assumptions" not in result
        assert "fund_assumptions" not in result

    def test_io_months_and_term_months(self):
        result = _build(
            purchase_price=20_000_000,
            ltv=0.70,
            loan_rate=0.06,
            amort_years=30,
            io_months=24,
            term_months=60,
        )
        assert result["debt_terms"]["io_months"] == 24
        assert result["debt_terms"]["term_months"] == 60

    def test_purchase_without_debt(self):
        """Purchase price alone should create purchase_assumptions but not debt_terms."""
        result = _build(purchase_price=15_000_000)
        assert "purchase_assumptions" in result
        assert result["purchase_assumptions"]["purchase_price"] == 15_000_000
        assert "debt_terms" not in result  # needs ltv + rate + amort

    def test_schema_validation_passes_with_all_flags(self):
        """Full flags should produce schema-valid JSON."""
        from engine.validator import validate_deal
        result = _build(
            purchase_price=25_000_000,
            ltv=0.75,
            loan_rate=0.065,
            amort_years=30,
            exit_cap_rate=0.055,
            sponsor_equity_pct=0.10,
            pref_return=0.08,
        )
        report = validate_deal(result)
        errors = [i for i in report.issues if i.severity == "ERROR"]
        assert report.status == "PASS", f"Validation failed: {[e.message for e in errors]}"

    def test_schema_validation_passes_minimal(self):
        """Minimal ingested JSON (no deal economics) should pass schema validation."""
        from engine.validator import validate_deal
        result = _build()
        report = validate_deal(result)
        errors = [i for i in report.issues if i.severity == "ERROR"]
        assert report.status == "PASS", f"Validation failed: {[e.message for e in errors]}"


# ── OM integration ───────────────────────────────────────────────────────


def _sample_om(**kwargs) -> OmData:
    defaults = dict(
        property_name="Cedar Ridge Apartments",
        address="2847 Maple, Garland TX",
        market="dallas",
        year_built=1987,
        total_units=148,
        property_class="C",
        building_type="garden",
        asking_price=13_200_000,
        exit_cap_rate=0.0575,
        hold_years=5,
        unit_mix=[
            OmUnitType("1BR/1BA", 72, 650, 825, 975),
            OmUnitType("2BR/1BA", 52, 875, 1025, 1175),
        ],
        extraction_confidence="high",
    )
    defaults.update(kwargs)
    return OmData(**defaults)


class TestBuildDealWithOm:
    def test_purchase_price_from_om(self):
        with patch("engine.ingest.ingestion_pipeline.parse_om", return_value=_sample_om()):
            result = build_deal_from_documents(
                property_id="Cedar Ridge",
                rent_roll_path=ROLL,
                t12_path=T12,
                analysis_start="2026-05",
                analysis_end="2031-04",
                om_path="fake_om.pdf",
            )
        assert result["purchase_assumptions"]["purchase_price"] == 13_200_000

    def test_exit_cap_from_om(self):
        with patch("engine.ingest.ingestion_pipeline.parse_om", return_value=_sample_om()):
            result = build_deal_from_documents(
                property_id="Cedar Ridge",
                rent_roll_path=ROLL,
                t12_path=T12,
                analysis_start="2026-05",
                analysis_end="2031-04",
                om_path="fake_om.pdf",
            )
        assert result["exit_assumptions"]["exit_cap_rate"] == pytest.approx(0.0575)

    def test_no_om_leaves_sections_absent(self):
        result = build_deal_from_documents(
            property_id="Test",
            rent_roll_path=ROLL,
            t12_path=T12,
            analysis_start="2026-05",
            analysis_end="2031-04",
        )
        assert "purchase_assumptions" not in result

    def test_om_data_logged_in_metadata(self):
        with patch("engine.ingest.ingestion_pipeline.parse_om", return_value=_sample_om()):
            result = build_deal_from_documents(
                property_id="Cedar Ridge",
                rent_roll_path=ROLL,
                t12_path=T12,
                analysis_start="2026-05",
                analysis_end="2031-04",
                om_path="fake_om.pdf",
            )
        assert result["metadata"].get("om_extraction_confidence") == "high"

    def test_cli_flags_override_om(self):
        """CLI purchase_price should override OM extraction."""
        with patch("engine.ingest.ingestion_pipeline.parse_om", return_value=_sample_om()):
            result = build_deal_from_documents(
                property_id="Cedar Ridge",
                rent_roll_path=ROLL,
                t12_path=T12,
                analysis_start="2026-05",
                analysis_end="2031-04",
                om_path="fake_om.pdf",
                purchase_price=15_000_000,
            )
        # CLI value wins over OM
        assert result["purchase_assumptions"]["purchase_price"] == 15_000_000

    def test_om_with_debt_flags(self):
        """OM provides purchase price, CLI provides debt terms."""
        with patch("engine.ingest.ingestion_pipeline.parse_om", return_value=_sample_om()):
            result = build_deal_from_documents(
                property_id="Cedar Ridge",
                rent_roll_path=ROLL,
                t12_path=T12,
                analysis_start="2026-05",
                analysis_end="2031-04",
                om_path="fake_om.pdf",
                ltv=0.75,
                loan_rate=0.065,
                amort_years=30,
            )
        # OM purchase price used for debt calc
        assert result["debt_terms"]["commitment"] == pytest.approx(13_200_000 * 0.75)


def test_build_deal_promotes_labelled_broker_tax_evidence_end_to_end():
    broker_snapshot = {
        "property_tax_evidence_candidates": [
            {
                "producer": "broker_snapshot_mills",
                "property_tax_millage_rate": 25.31,
                "source": "offering_memorandum",
                "source_locator": "fake_om.pdf, property tax section",
            }
        ]
    }
    with (
        patch(
            "engine.ingest.ingestion_pipeline.parse_om",
            return_value=_sample_om(),
        ),
        patch(
            "engine.ingest.ingestion_pipeline.parse_broker_snapshot_from_om",
            return_value=broker_snapshot,
        ),
    ):
        result = build_deal_from_documents(
            property_id="Cedar Ridge",
            rent_roll_path=ROLL,
            t12_path=T12,
            analysis_start="2026-05",
            analysis_end="2031-04",
            om_path="fake_om.pdf",
        )

    assert result["metadata"]["property_summary"]["property_tax_policy"] == {
        "millage_rate_mills": 25.31,
        "assessment_ratio": 1.0,
        "source": "offering_memorandum",
        "source_locator": "fake_om.pdf, property tax section",
        "analyst_override": False,
    }


def test_broker_tax_locator_is_stable_across_temporary_om_paths():
    def broker_snapshot(path):
        return {
            "property_tax_evidence_candidates": [
                {
                    "producer": "broker_snapshot_mills",
                    "property_tax_millage_rate": 25.31,
                    "source": "offering_memorandum",
                    "source_locator": f"{path}, property tax section",
                }
            ]
        }

    stable_locator = (
        "OneDrive/Deals/Cedar Ridge/Cedar Ridge OM.pdf, property tax section"
    )
    results = []
    with (
        patch(
            "engine.ingest.ingestion_pipeline.parse_om",
            return_value=_sample_om(),
        ),
        patch(
            "engine.ingest.ingestion_pipeline.parse_broker_snapshot_from_om",
            side_effect=broker_snapshot,
        ),
    ):
        for temporary_path in (
            "/tmp/onedrive_a/Cedar Ridge OM.pdf",
            "/tmp/onedrive_b/Cedar Ridge OM.pdf",
        ):
            results.append(
                build_deal_from_documents(
                    property_id="Cedar Ridge",
                    rent_roll_path=ROLL,
                    t12_path=T12,
                    analysis_start="2026-05",
                    analysis_end="2031-04",
                    om_path=temporary_path,
                    om_source_locator=stable_locator,
                )
            )

    assert [
        result["metadata"]["property_summary"]["property_tax_policy"][
            "source_locator"
        ]
        for result in results
    ] == [stable_locator, stable_locator]


def test_local_om_relative_and_absolute_paths_produce_identical_canonical(
    tmp_path,
):
    absolute_om = tmp_path / "deal" / "raw_inputs" / "Local OM.pdf"
    absolute_om.parent.mkdir(parents=True)
    absolute_om.write_bytes(b"fixture")
    relative_om = Path(os.path.relpath(absolute_om, Path.cwd()))

    parser_patches = (
        patch(
            "engine.ingest.ingestion_pipeline.parse_om",
            return_value=_sample_om(),
        ),
        patch(
            "engine.ingest.broker_om_snapshot._extract_layout_text",
            return_value="property tax fixture",
        ),
        patch(
            "engine.ingest.broker_om_snapshot._extract_property_facts",
            return_value={"deal_name": "Local OM"},
        ),
        patch(
            "engine.ingest.broker_om_snapshot._extract_income_statement",
            side_effect=lambda _text: (
                {},
                {},
                {"property_tax_millage_rate": 25.31},
            ),
        ),
        patch(
            "engine.ingest.broker_om_snapshot._extract_tax_analysis",
            return_value={},
        ),
    )

    with parser_patches[0], parser_patches[1], parser_patches[2], (
        parser_patches[3]
    ), parser_patches[4]:
        relative_result = build_deal_from_documents(
            property_id="Cedar Ridge",
            rent_roll_path=ROLL,
            t12_path=T12,
            analysis_start="2026-05",
            analysis_end="2031-04",
            om_path=relative_om,
        )
        absolute_result = build_deal_from_documents(
            property_id="Cedar Ridge",
            rent_roll_path=ROLL,
            t12_path=T12,
            analysis_start="2026-05",
            analysis_end="2031-04",
            om_path=absolute_om,
        )

    expected_locator = "raw_inputs/Local OM.pdf, property tax section"
    assert relative_result["metadata"]["property_summary"][
        "property_tax_policy"
    ]["source_locator"] == expected_locator
    assert absolute_result["metadata"]["property_summary"][
        "property_tax_policy"
    ]["source_locator"] == expected_locator

    def canonical_hash(value):
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    assert canonical_hash(relative_result) == canonical_hash(absolute_result)


def test_local_om_locator_without_raw_inputs_uses_stable_filename():
    expected = "Local OM.pdf, property tax section"

    assert stable_property_tax_source_locator(
        "/tmp/session-a/Local OM.pdf"
    ) == expected
    assert stable_property_tax_source_locator(
        "downloads/session-b/Local OM.pdf"
    ) == expected


# ── Validator soft-warn ──────────────────────────────────────────────────


class TestValidatorSoftWarn:
    def test_strict_mode_fails_on_invalid_schema(self):
        from engine.validator import validate_deal
        # Invalid: missing required cohort fields
        bad = {"schema_version": "0.1", "metadata": {}, "bad_key": True}
        report = validate_deal(bad, strict=True)
        assert report.status == "FAIL"

    def test_soft_mode_passes_minimal_ingested(self):
        from engine.validator import validate_deal
        result = _build()
        report = validate_deal(result, strict=False)
        errors = [i for i in report.issues if i.severity == "ERROR"]
        assert report.status == "PASS", f"Errors: {[e.message for e in errors]}"


# ── Bug 1.12: market_rent provenance tagging ─────────────────────────────


class TestMarketRentSourceTagging:
    def test_market_rent_explicit_tagged_rent_roll_market(self):
        """Cohort with explicit market_rent → curve segment tagged
        'rent_roll_market'."""
        from engine.ingest.ingestion_pipeline import _build_market_rent_curve

        cohorts = [
            {"cohort_id": "1BR1BA", "initial_inplace_rent": 825.0},
            {"cohort_id": "2BR1BA", "initial_inplace_rent": 1025.0},
        ]
        market_rents = {"1BR1BA": 975.0, "2BR1BA": 1175.0}
        curve = _build_market_rent_curve(cohorts, market_rents, "2026-05", "2031-04")

        assert len(curve) == 2
        for seg in curve:
            assert seg["target_monthly_rent_source"] == "rent_roll_market"
        # Sanity: market_rent matches the explicit value (not in-place).
        by_id = {s["cohort_id"]: s for s in curve}
        assert by_id["1BR1BA"]["market_rent"] == pytest.approx(975.0)
        assert by_id["2BR1BA"]["market_rent"] == pytest.approx(1175.0)

    def test_market_rent_fallback_tagged_in_place(self):
        """Cohort missing market_rent → curve segment tagged
        'in_place_fallback' and value pulled from initial_inplace_rent."""
        from engine.ingest.ingestion_pipeline import _build_market_rent_curve

        cohorts = [
            {"cohort_id": "1BR1BA", "initial_inplace_rent": 825.0},
            {"cohort_id": "2BR1BA", "initial_inplace_rent": 1025.0},
        ]
        market_rents = {}  # no market data on file
        curve = _build_market_rent_curve(cohorts, market_rents, "2026-05", "2031-04")

        assert len(curve) == 2
        for seg in curve:
            assert seg["target_monthly_rent_source"] == "in_place_fallback"
        by_id = {s["cohort_id"]: s for s in curve}
        # Falls back to in-place rent.
        assert by_id["1BR1BA"]["market_rent"] == pytest.approx(825.0)
        assert by_id["2BR1BA"]["market_rent"] == pytest.approx(1025.0)

    def test_mixed_explicit_and_fallback(self):
        """Mix: one cohort has market_rent, other doesn't — tags differ."""
        from engine.ingest.ingestion_pipeline import _build_market_rent_curve

        cohorts = [
            {"cohort_id": "1BR1BA", "initial_inplace_rent": 825.0},
            {"cohort_id": "2BR1BA", "initial_inplace_rent": 1025.0},
        ]
        market_rents = {"1BR1BA": 975.0}  # only first cohort has explicit market
        curve = _build_market_rent_curve(cohorts, market_rents, "2026-05", "2031-04")

        by_id = {s["cohort_id"]: s for s in curve}
        assert by_id["1BR1BA"]["target_monthly_rent_source"] == "rent_roll_market"
        assert by_id["2BR1BA"]["target_monthly_rent_source"] == "in_place_fallback"

    def test_full_pipeline_tags_present(self):
        """End-to-end: build_deal_from_documents emits market_rent_curve
        with target_monthly_rent_source tags on every segment."""
        result = _build()
        for seg in result["market_rent_curve"]:
            assert "target_monthly_rent_source" in seg
            assert seg["target_monthly_rent_source"] in {"rent_roll_market", "in_place_fallback"}


# ── V1.2 — OM auto-extract provenance + sanity flag ──────────────────────


class TestOmAutoExtractProvenance:
    """V1.2: when om_parser populates purchase_assumptions or
    exit_assumptions, the canonical deal metadata must record the
    provenance + analyst_review_required so downstream tooling can flag
    the auto-extract for human review.
    """

    def test_om_extract_tags_purchase_assumptions_source(self):
        """OM yielded purchase_price → metadata.purchase_assumptions_source
        == 'om_haiku_extraction' AND analyst_review_required is True."""
        with patch("engine.ingest.ingestion_pipeline.parse_om", return_value=_sample_om()):
            result = build_deal_from_documents(
                property_id="Cedar Ridge",
                rent_roll_path=ROLL,
                t12_path=T12,
                analysis_start="2026-05",
                analysis_end="2031-04",
                om_path="fake_om.pdf",
            )
        assert result["metadata"]["purchase_assumptions_source"] == "om_haiku_extraction"
        assert result["metadata"]["analyst_review_required"] is True
        assert result["purchase_assumptions"]["purchase_price"] == 13_200_000

    def test_om_extract_failure_emits_sanity_flag(self):
        """parse_om raises → metadata.intake_sanity_flags contains
        'om_extraction_unavailable' and intake doesn't crash."""
        def _boom(*_args, **_kwargs):
            raise RuntimeError("Anthropic API down")
        with patch("engine.ingest.ingestion_pipeline.parse_om", side_effect=_boom):
            result = build_deal_from_documents(
                property_id="Cedar Ridge",
                rent_roll_path=ROLL,
                t12_path=T12,
                analysis_start="2026-05",
                analysis_end="2031-04",
                om_path="fake_om.pdf",
            )
        flags = result["metadata"].get("intake_sanity_flags", [])
        assert "om_extraction_unavailable" in flags
        # purchase_assumptions should be absent (no CLI flag, no OM data)
        assert "purchase_assumptions" not in result

    def test_om_extract_empty_emits_sanity_flag(self):
        """parse_om returns OmData with no usable fields → sanity flag."""
        empty_om = OmData(
            property_name="UnknownDeal",
            address=None,
            market=None,
            year_built=None,
            total_units=None,
            property_class=None,
            building_type=None,
            unit_mix=[],
            asking_price=None,
            exit_cap_rate=None,
            extraction_confidence="low",
        )
        with patch("engine.ingest.ingestion_pipeline.parse_om", return_value=empty_om):
            result = build_deal_from_documents(
                property_id="UnknownDeal",
                rent_roll_path=ROLL,
                t12_path=T12,
                analysis_start="2026-05",
                analysis_end="2031-04",
                om_path="fake_om.pdf",
            )
        flags = result["metadata"].get("intake_sanity_flags", [])
        assert "om_extraction_unavailable" in flags

    def test_cli_purchase_price_does_not_set_om_provenance(self):
        """When CLI provides purchase_price, OM is NOT the source even if
        OM also extracted a value — provenance stays unset (analyst-driven)."""
        with patch("engine.ingest.ingestion_pipeline.parse_om", return_value=_sample_om()):
            result = build_deal_from_documents(
                property_id="Cedar Ridge",
                rent_roll_path=ROLL,
                t12_path=T12,
                analysis_start="2026-05",
                analysis_end="2031-04",
                om_path="fake_om.pdf",
                purchase_price=15_000_000,
            )
        # CLI wins; purchase_assumptions_source should NOT be set to om_haiku_extraction
        assert result["metadata"].get("purchase_assumptions_source") != "om_haiku_extraction"
