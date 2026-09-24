"""
Tests for Market Study Agent Integration
"""
import json
import tempfile
import unittest
from pathlib import Path

from engine.market_integration import (
    CompComparison,
    CompProperty,
    FloorplanComp,
    MarketContext,
    SubjectFloorplan,
    apply_market_calibration_to_canonical,
    comp_comparison,
    load_comp_snapshot,
    load_floorplan_summary,
    suggest_rent_growth_adjustment,
)


def _make_subject_plans():
    return [
        SubjectFloorplan("A1", "1BR", 10, 700, 1100),
        SubjectFloorplan("B1", "2BR", 8, 950, 1350),
    ]


def _make_comps():
    return [
        CompProperty(
            name="Comp A",
            address="123 Main St",
            year_built=2010,
            floorplans=[
                FloorplanComp("A1", 1, 1.0, 680, rent_min=1050, rent_max=1150, rent_per_sf=1.62),
                FloorplanComp("B1", 2, 2.0, 960, rent_min=1300, rent_max=1400, rent_per_sf=1.41),
            ],
        ),
        CompProperty(
            name="Comp B",
            address="456 Oak Ave",
            year_built=2015,
            floorplans=[
                FloorplanComp("A1", 1, 1.0, 720, rent_min=1150, rent_max=1250, rent_per_sf=1.67),
                FloorplanComp("B1", 2, 2.0, 980, rent_min=1380, rent_max=1480, rent_per_sf=1.46),
            ],
        ),
    ]


class TestCompComparison(unittest.TestCase):
    def test_returns_comparisons_by_bed_type(self):
        result = comp_comparison(_make_subject_plans(), _make_comps())
        self.assertEqual(len(result), 2)
        bed_types = [c.bed_type for c in result]
        self.assertIn("1BR", bed_types)
        self.assertIn("2BR", bed_types)

    def test_subject_rent_weighted_by_units(self):
        result = comp_comparison(_make_subject_plans(), _make_comps())
        one_br = next(c for c in result if c.bed_type == "1BR")
        self.assertAlmostEqual(one_br.subject_avg_rent, 1100, places=0)

    def test_comp_rent_averaged(self):
        result = comp_comparison(_make_subject_plans(), _make_comps())
        one_br = next(c for c in result if c.bed_type == "1BR")
        # Comp A: (1050+1150)/2 = 1100, Comp B: (1150+1250)/2 = 1200
        # Avg: (1100+1200)/2 = 1150
        self.assertAlmostEqual(one_br.comp_avg_rent, 1150, places=0)

    def test_premium_negative_when_below_market(self):
        result = comp_comparison(_make_subject_plans(), _make_comps())
        one_br = next(c for c in result if c.bed_type == "1BR")
        # Subject 1100 vs Comp 1150 → negative premium
        self.assertLess(one_br.rent_premium_pct, 0)

    def test_empty_comps_returns_empty(self):
        result = comp_comparison(_make_subject_plans(), [])
        self.assertEqual(len(result), 0)

    def test_no_matching_bed_types(self):
        # Subject has 1BR/2BR, comp only has 3BR
        comps = [CompProperty(
            name="Comp", address="",
            floorplans=[FloorplanComp("C1", 3, 2.0, 1200, rent_min=1800, rent_max=2000)],
        )]
        result = comp_comparison(_make_subject_plans(), comps)
        self.assertEqual(len(result), 0)


class TestSuggestRentGrowth(unittest.TestCase):
    def test_below_market_boosts_growth(self):
        # Subject below market → growth should increase
        comparisons = [
            CompComparison("1BR", 1100, 700, 1.57, 1200, 700, 1.71, -0.0833, 4),
        ]
        adjusted = suggest_rent_growth_adjustment(comparisons, 0.03)
        self.assertGreater(adjusted, 0.03)

    def test_above_market_reduces_growth(self):
        comparisons = [
            CompComparison("1BR", 1300, 700, 1.86, 1200, 700, 1.71, 0.0833, 4),
        ]
        adjusted = suggest_rent_growth_adjustment(comparisons, 0.03)
        self.assertLess(adjusted, 0.03)

    def test_empty_comparisons_returns_base(self):
        adjusted = suggest_rent_growth_adjustment([], 0.03)
        self.assertEqual(adjusted, 0.03)

    def test_adjustment_capped(self):
        # Very large discount shouldn't increase growth more than 100bps
        comparisons = [
            CompComparison("1BR", 800, 700, 1.14, 1200, 700, 1.71, -0.33, 10),
        ]
        adjusted = suggest_rent_growth_adjustment(comparisons, 0.03)
        self.assertLessEqual(adjusted, 0.04)  # max +100bps


class TestLoadFloorplanSummary(unittest.TestCase):
    def test_load_csv(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write("PlanCode,BedType,Units,SqFt,AvgMarketRent\n")
            f.write("A1,1BR,10,700,1100\n")
            f.write("B1,2BR,8,950,1350\n")
            f.flush()
            plans = load_floorplan_summary(Path(f.name))

        self.assertEqual(len(plans), 2)
        self.assertEqual(plans[0].plan_code, "A1")
        self.assertEqual(plans[0].units, 10)
        self.assertAlmostEqual(plans[0].rent_per_sf, 1100 / 700, places=2)


class TestLoadCompSnapshot(unittest.TestCase):
    def test_load_snapshot_json(self):
        snapshot = {
            "run_date": "2026-03-05",
            "metro": "Dallas, TX",
            "comps": [
                {
                    "name": "Test Comp",
                    "address": "100 Test St",
                    "year_built": 2005,
                    "direct": {
                        "floorplans": [
                            {
                                "floorplan_name": "A1",
                                "beds": 1,
                                "baths": 1.0,
                                "sqft": 700,
                                "rent_min": 1100,
                                "rent_max": 1200,
                                "available_units": 3,
                            }
                        ],
                        "specials": ["$200 off first month"],
                    },
                }
            ],
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(snapshot, f)
            f.flush()
            comps = load_comp_snapshot(Path(f.name))

        self.assertEqual(len(comps), 1)
        self.assertEqual(comps[0].name, "Test Comp")
        self.assertEqual(len(comps[0].floorplans), 1)
        self.assertEqual(comps[0].floorplans[0].beds, 1)
        self.assertAlmostEqual(comps[0].floorplans[0].rent_per_sf, 1150 / 700, places=2)
        self.assertEqual(comps[0].specials, ["$200 off first month"])
        self.assertEqual(comps[0].snapshot_date, "2026-03-05")


class TestMarketContext(unittest.TestCase):
    def test_from_files_with_data(self):
        # Write temp floorplan CSV
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write("PlanCode,BedType,Units,SqFt,AvgMarketRent\n")
            f.write("A1,1BR,10,700,1100\n")
            fp_path = Path(f.name)

        # Write temp comp snapshot
        snapshot = {
            "run_date": "2026-03-05",
            "comps": [{
                "name": "Comp A",
                "address": "123 Main",
                "direct": {
                    "floorplans": [
                        {"floorplan_name": "A1", "beds": 1, "baths": 1.0, "sqft": 720,
                         "rent_min": 1200, "rent_max": 1300},
                    ],
                },
            }],
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(snapshot, f)
            comp_path = Path(f.name)

        ctx = MarketContext.from_files(fp_path, comp_path, "DFW", "Test Property")
        self.assertEqual(len(ctx.subject_plans), 1)
        self.assertEqual(len(ctx.comps), 1)

        summary = ctx.comp_summary()
        self.assertEqual(summary["metro"], "DFW")
        self.assertEqual(summary["comp_properties"], 1)
        self.assertEqual(len(summary["by_bed_type"]), 1)
        self.assertEqual(summary["by_bed_type"][0]["bed_type"], "1BR")

    def test_apply_rent_adjustments(self):
        ctx = MarketContext(
            subject_plans=_make_subject_plans(),
            comps=_make_comps(),
        )
        inputs = {"growth_assumptions": {"annual_growth_rate": 0.03}}
        adjusted = ctx.apply_rent_adjustments(inputs)

        # Original not mutated
        self.assertEqual(inputs["growth_assumptions"]["annual_growth_rate"], 0.03)
        # Adjusted should differ (subject is below market for 1BR)
        self.assertTrue(adjusted["growth_assumptions"].get("comp_adjusted"))
        self.assertEqual(adjusted["growth_assumptions"]["original_growth_rate"], 0.03)

    def test_comp_property_avg_rent_per_sf(self):
        comp = CompProperty(
            name="Test", address="",
            floorplans=[
                FloorplanComp("A1", 1, 1.0, 700, rent_per_sf=1.50),
                FloorplanComp("B1", 2, 2.0, 950, rent_per_sf=1.40),
            ],
        )
        self.assertAlmostEqual(comp.avg_rent_per_sf, 1.45, places=2)


class TestApplyMarketCalibrationToCanonical(unittest.TestCase):
    """Wave 6 Task 6.3 — comp-reconciler bridge into the canonical's market_rent_curve.

    Confirms ``apply_market_calibration_to_canonical`` is the publicly-callable
    entry point from plat-agent's comp-reconciler step (Stage 7 finding: prior
    to this, ``MarketContext.apply_rent_adjustments`` had zero callers).
    """

    def _canonical(self):
        return {
            "market_rent_curve": [
                {
                    "cohort_id": "1BR",
                    "start_period": "2026-07",
                    "end_period": "2031-06",
                    "market_rent": 1200,
                },
                {
                    "cohort_id": "2BR",
                    "start_period": "2026-07",
                    "end_period": "2031-06",
                    "market_rent": 1500,
                },
            ]
        }

    def test_apply_market_calibration_use_comp_replaces_curve(self):
        canonical = self._canonical()
        calibration = [
            {"cohort_id": "1BR", "comp_p50_rent": 1400, "recommendation": "use_comp"},
        ]
        result = apply_market_calibration_to_canonical(canonical, calibration)

        one_br = next(r for r in result["market_rent_curve"] if r["cohort_id"] == "1BR")
        self.assertEqual(one_br["market_rent"], 1400)
        # 2BR untouched (no calibration entry).
        two_br = next(r for r in result["market_rent_curve"] if r["cohort_id"] == "2BR")
        self.assertEqual(two_br["market_rent"], 1500)

    def test_apply_market_calibration_use_canonical_unchanged(self):
        canonical = self._canonical()
        calibration = [
            {"cohort_id": "1BR", "comp_p50_rent": 1400, "recommendation": "use_canonical"},
        ]
        result = apply_market_calibration_to_canonical(canonical, calibration)

        one_br = next(r for r in result["market_rent_curve"] if r["cohort_id"] == "1BR")
        self.assertEqual(one_br["market_rent"], 1200)
        # No provenance marker added when we kept the canonical value.
        self.assertNotIn("market_rent_provenance", one_br)

    def test_apply_market_calibration_use_blended_50_50(self):
        canonical = self._canonical()
        calibration = [
            {"cohort_id": "1BR", "comp_p50_rent": 1400, "recommendation": "use_blended"},
        ]
        result = apply_market_calibration_to_canonical(canonical, calibration)

        one_br = next(r for r in result["market_rent_curve"] if r["cohort_id"] == "1BR")
        # 50/50 blend of canonical 1200 and comp 1400 → 1300.
        self.assertEqual(one_br["market_rent"], 1300)
        prov = one_br.get("market_rent_provenance")
        self.assertIsNotNone(prov)
        self.assertEqual(prov["source"], "use_blended")
        self.assertEqual(prov["comp_p50_rent"], 1400)
        self.assertEqual(prov["canonical_rent"], 1200)
        self.assertEqual(prov["blend_weight_comp"], 0.5)

    def test_apply_market_calibration_does_not_mutate_input(self):
        canonical = self._canonical()
        before = json.dumps(canonical, sort_keys=True)
        calibration = [
            {"cohort_id": "1BR", "comp_p50_rent": 1400, "recommendation": "use_comp"},
            {"cohort_id": "2BR", "comp_p50_rent": 1700, "recommendation": "use_blended"},
        ]
        _ = apply_market_calibration_to_canonical(canonical, calibration)
        after = json.dumps(canonical, sort_keys=True)
        self.assertEqual(before, after, "input canonical must not be mutated")


if __name__ == "__main__":
    unittest.main()
