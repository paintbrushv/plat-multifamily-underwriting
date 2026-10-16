"""Stabilized vacancy built from evidence (ADR 0003 decision o, UPL-108).

Physical vacancy starts at the rent roll's current vacancy in year 1 and glides
to a stabilized vacancy built from:

- the market base: the stabilized vacancy of the submarket for the asset's star
  tier, else the metro aggregate, else the house vintage default;
- a supply adjustment for the lease-up years: deliveries over the next 24
  months as a share of inventory above the submarket's normal pace adds
  ``HOUSE_SUPPLY_VACANCY_BPS_PER_PCT`` per point, capped;
- an asset-vs-market carry: a share of the gap between the asset's T12
  vacancy and the market's, after stabilization effects (offline units,
  lease-up) are separated, capped.

The rent roll's vacancy is evidence for year 1, not a floor: the only pricing
floor is the house vintage floor. All market data here is synthetic.
"""

from __future__ import annotations

import copy
import json
from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

import pytest

from engine import underwriting_policy as house
from engine.assumptions import resolve_assumptions
from engine.assumptions.guardrails import GuardrailProfile, apply_guardrails, house_floors
from engine.assumptions.rationale import has_rationale
from engine.assumptions.register import build_register
from engine.ingest.ingestion_pipeline import _t12_rent_evidence, build_deal_from_documents
from engine.market_data import Geo, MarketDataRegistry, SyntheticProvider
from engine.market_data.diagnostics import SLOT_METRICS, market_data_status, status_lines
from engine.market_data.metrics import METRICS, SUPPLY_PIPELINE_MONTHS
from engine.market_data.types import Observation, Provenance

FIXTURES = Path(__file__).parent / "fixtures"
T12 = FIXTURES / "entrata_style_t12.csv"  # GPR $6,000,000, vacancy loss $375,000 (6.25%)
ROLL = FIXTURES / "sample_rent_roll.csv"  # 10 units, 2 vacant (20%)

MSA = Geo(metro_slug="synthetic_metro")
SUB = Geo(metro_slug="synthetic_metro", submarket="Synthetic North")
SOURCE = "house:stabilized_vacancy_model"


def _registry(data=None, *, observations=()):
    provider = SyntheticProvider(data or {}, as_of=date(2026, 6, 30), confidence="high", observations=observations)
    return MarketDataRegistry([provider])


@lru_cache(maxsize=None)
def _ingested() -> str:
    deal = build_deal_from_documents(
        property_id="Synthetic Vacancy",
        rent_roll_path=ROLL,
        t12_path=T12,
        analysis_start="2026-08",
        analysis_end="2031-07",
        year_built=1984,
        msa="Synthetic Metro",
        metro_slug="synthetic_metro",
        submarket="Synthetic North",
        market=None,
    )
    return json.dumps(deal)


def _deal(*, t12_vacancy="keep", t12_non_revenue=None, roll_rate=None, box_score=None, deal_type=None,
          year_built=None, pre_leased=None):
    """The ingested synthetic deal ready to re-resolve from scratch, with its evidence edited."""
    deal = json.loads(_ingested())
    deal.pop("assumptions", None)
    for row in deal["physical_vacancy_curve"]:
        row["vacancy_rate"] = None  # a placeholder the resolver fills
    evidence = deal["metadata"]["ingest_gate"]["evidence"]
    t12 = evidence["t12"]
    if t12_vacancy is None:
        t12.pop("vacancy_loss", None)
    elif t12_vacancy != "keep":
        t12["vacancy_loss"] = round(t12_vacancy * t12["gross_potential_rent"], 2)
    if t12_non_revenue is not None:
        t12["non_revenue_loss"] = round(t12_non_revenue * t12["gross_potential_rent"], 2)
    if roll_rate is not None:
        evidence["rent_roll"]["physical_vacancy_rate"] = roll_rate
    if pre_leased is not None:
        evidence["rent_roll"]["pre_leased_vacant_units"] = pre_leased
    if box_score is not None:
        deal["metadata"].setdefault("property_summary", {})["house_box_score"] = box_score
    if deal_type is not None:
        deal["deal_profile"]["deal_type"] = deal_type
    if year_built is not None:
        deal["deal_profile"]["year_built"] = year_built
    return deal


def _resolve(data=None, *, observations=(), overrides=None, **evidence):
    market = _registry(data, observations=observations) if (data is not None or observations) else None
    return resolve_assumptions(_deal(**evidence), market=market, overrides=overrides)


def _entry(deal):
    return deal["assumptions"]["entries"]["physical_vacancy"]


def _build(deal):
    return _entry(deal)["detail"]["vacancy_build"]


def _monthly(deal, cohort_id="1br1ba"):
    """One vacancy rate per month of the analysis for a cohort."""
    out = []
    for row in deal["physical_vacancy_curve"]:
        if row["cohort_id"] != cohort_id:
            continue
        first = int(row["start_period"][:4]) * 12 + int(row["start_period"][5:7])
        last = int(row["end_period"][:4]) * 12 + int(row["end_period"][5:7])
        out.extend([row["vacancy_rate"]] * (last - first + 1))
    return out


BASE = {("vacancy_stabilized", SUB): 0.06, ("vacancy_stabilized", MSA): 0.065}


# ── 2. the market base: submarket > metro > house ────────────────────────


def test_market_base_is_the_submarket_stabilized_vacancy():
    deal = _resolve(BASE, t12_vacancy=None)
    entry, build = _entry(deal), _build(deal)
    assert entry["value"] == {"start": 0.2, "stabilized": 0.06, "window_months": 12}
    assert entry["source"] == SOURCE
    assert entry["review_required"] is True
    base = build["market_base"]
    assert (base["value"], base["level"], base["source"]) == (0.06, "submarket", "market:synthetic")
    assert base["provider"] == "synthetic" and base["as_of"] == "2026-06-30"
    assert build["stabilized"] == 0.06


def test_market_base_falls_back_to_the_metro_aggregate():
    deal = _resolve({("vacancy_stabilized", MSA): 0.065}, t12_vacancy=None)
    base = _build(deal)["market_base"]
    assert (base["value"], base["level"], base["source"]) == (0.065, "metro", "market:synthetic")
    assert _entry(deal)["value"]["stabilized"] == 0.065


def test_market_base_falls_back_to_the_house_vintage_default():
    deal = _resolve(None, t12_vacancy=None)
    base = _build(deal)["market_base"]
    assert base["level"] == "house"
    assert base["source"] == "house:vintage_1980s"
    assert base["value"] == 0.0575 == float(house.typical_physical_vacancy_rate(1984))
    assert _entry(deal)["source"] == SOURCE
    assert _entry(deal)["value"] == {"start": 0.2, "stabilized": 0.0575, "window_months": 12}


def test_market_base_discloses_the_star_tier_matched():
    observation = Observation(
        metric="vacancy_stabilized", geo=Geo(metro_slug="synthetic_metro", submarket="Synthetic North",
                                             star_tier="3 Star"),
        value=0.06, unit="decimal", provenance=Provenance(provider="synthetic"), as_of=date(2026, 6, 30),
    )
    deal = _deal(t12_vacancy=None)
    deal["deal_profile"]["star_tier"] = "3"
    resolved = resolve_assumptions(deal, market=_registry(observations=[observation]))
    base = _build(resolved)["market_base"]
    assert base["star_tier_requested"] == "3 Star"
    assert base["star_tier_matched"] == "3 Star"


# ── 3. the supply adjustment ─────────────────────────────────────────────


def _supply(pipeline, norm):
    data = dict(BASE)
    if pipeline is not None:
        data[("supply_pipeline", SUB)] = pipeline
    if norm is not None:
        data[("supply_pipeline_norm", SUB)] = norm
    return data


def test_excess_deliveries_add_vacancy_for_the_lease_up_years():
    deal = _resolve(_supply(0.08, 0.04), t12_vacancy=None)
    supply = _build(deal)["supply"]
    # 8% of inventory delivering over 24 months vs a 4% normal pace: 4 points of
    # excess x 25 bps = 100 bps for years 1-3.
    assert supply["available"] is True
    assert supply["deliveries_pct_inventory"] == 0.08
    assert supply["normal_pct_inventory"] == 0.04
    assert supply["excess_pct_inventory"] == pytest.approx(0.04)
    assert supply["bps"] == pytest.approx(100)
    assert supply["value"] == pytest.approx(0.01)
    assert supply["capped"] is False
    assert supply["months"] == 36
    assert supply["horizon_months"] == house.HOUSE_SUPPLY_HORIZON_MONTHS == 24
    assert _entry(deal)["value"] == {
        "start": 0.2, "stabilized": 0.06, "window_months": 12, "supply_adjustment": 0.01, "supply_months": 36,
    }
    monthly = _monthly(deal)
    assert monthly[0] == 0.2
    assert monthly[6] == pytest.approx(0.2 + (0.07 - 0.2) * 6 / 12, abs=1e-6)
    assert set(monthly[12:36]) == {0.07}  # stabilized + supply through year 3
    assert set(monthly[36:]) == {0.06}  # then the stabilized vacancy


def test_the_supply_adjustment_is_capped():
    deal = _resolve(_supply(0.20, 0.02), t12_vacancy=None)
    supply = _build(deal)["supply"]
    assert supply["bps"] == pytest.approx(450)
    assert supply["capped"] is True
    assert supply["value"] == float(house.HOUSE_SUPPLY_VACANCY_CAP) == 0.015
    assert _entry(deal)["value"]["supply_adjustment"] == 0.015


def test_deliveries_at_or_below_the_normal_pace_add_nothing():
    deal = _resolve(_supply(0.02, 0.04), t12_vacancy=None)
    supply = _build(deal)["supply"]
    assert supply["available"] is True and supply["value"] == 0.0
    assert _entry(deal)["value"] == {"start": 0.2, "stabilized": 0.06, "window_months": 12}


@pytest.mark.parametrize("pipeline, norm, missing", [(None, None, "supply_pipeline"),
                                                     (0.08, None, "supply_pipeline_norm")])
def test_missing_supply_data_falls_back_to_no_adjustment_and_says_so(pipeline, norm, missing):
    deal = _resolve(_supply(pipeline, norm), t12_vacancy=None)
    supply = _build(deal)["supply"]
    assert supply["available"] is False
    assert supply["value"] == 0.0
    assert missing in supply["note"]
    assert "no supply adjustment" in supply["note"]
    assert _entry(deal)["value"]["stabilized"] == 0.06
    assert "supply_adjustment" not in _entry(deal)["value"]
    assert "no supply adjustment" in _entry(deal)["basis"]


# ── 4. asset vs market ───────────────────────────────────────────────────


def test_a_share_of_the_t12_gap_to_the_market_is_carried():
    deal = _resolve(BASE)  # T12 vacancy loss 6.25% of GPR vs 6.00% submarket
    gap = _build(deal)["asset_vs_market"]
    assert gap["available"] is True
    assert gap["asset_vacancy"] == 0.0625
    assert gap["market_comparator"] == 0.06
    assert gap["gap"] == pytest.approx(0.0025)
    assert gap["carried_share"] == float(house.HOUSE_ASSET_GAP_CARRY_SHARE) == 0.5
    assert gap["value"] == pytest.approx(0.00125)
    assert _entry(deal)["value"]["stabilized"] == pytest.approx(0.06125)


@pytest.mark.parametrize("t12, carried, capped", [
    (0.10, 0.02, False),    # 4.0 points over: half is the 200 bp cap exactly
    (0.13, 0.02, True),     # 7.0 points over: capped at +200 bp
    (0.03, -0.015, False),  # 3.0 points under: half is carried as a credit
    (0.0, -0.02, True),     # 6.0 points under: capped at -200 bp
])
def test_the_carried_gap_is_capped_both_ways(t12, carried, capped):
    deal = _resolve(BASE, t12_vacancy=t12)
    gap = _build(deal)["asset_vs_market"]
    assert gap["value"] == pytest.approx(carried)
    assert gap["capped"] is capped
    assert gap["cap"] == float(house.HOUSE_ASSET_GAP_CAP) == 0.02
    assert _entry(deal)["value"]["stabilized"] == pytest.approx(0.06 + carried)


def test_the_comparator_is_the_markets_trailing_year_when_the_provider_has_it():
    observation = Observation(
        metric="vacancy_stabilized", geo=SUB, value=0.06, unit="decimal",
        provenance=Provenance(provider="synthetic"), as_of=date(2026, 6, 30),
        attributes={"trailing_4q_mean": 0.055},
    )
    deal = _resolve(observations=[observation])
    gap = _build(deal)["asset_vs_market"]
    assert gap["market_comparator"] == 0.055
    assert "trailing" in gap["comparator_basis"]
    assert gap["value"] == pytest.approx((0.0625 - 0.055) / 2)


def test_no_gap_is_carried_without_market_vacancy_or_without_t12_vacancy():
    house_only = _build(_resolve(None))
    assert house_only["asset_vs_market"]["available"] is False
    assert house_only["asset_vs_market"]["value"] == 0.0
    assert "market" in house_only["asset_vs_market"]["note"]
    assert house_only["stabilized"] == 0.0575

    no_t12 = _build(_resolve(BASE, t12_vacancy=None))
    assert no_t12["asset_vs_market"]["available"] is False
    assert "T12" in no_t12["asset_vs_market"]["note"]


BOX = {"total_units": 10, "status_counts": {"Occupied": 8, "Vacant": 1, "Down": 1}}


def test_offline_units_are_separated_before_the_gap_is_measured():
    deal = _resolve(BASE, t12_vacancy=0.18, box_score=BOX)
    build = _build(deal)
    exclusions = build["stabilization_exclusions"]
    assert exclusions["offline_units"] == 1
    assert exclusions["offline_share"] == pytest.approx(0.1)
    assert exclusions["offline_source"] == "house_box_score.status_counts"
    assert exclusions["offline_applied"] is True
    gap = build["asset_vs_market"]
    # 18% T12 vacancy less 10% of units offline = 8%, 2 points over the market.
    # (The exclusion never takes the asset below the market: ADR 0003 decision q.)
    assert gap["asset_vacancy"] == pytest.approx(0.18)
    assert gap["asset_vacancy_stabilized"] == pytest.approx(0.08)
    assert gap["gap"] == pytest.approx(0.02)
    assert gap["value"] == pytest.approx(0.01)
    assert _entry(deal)["value"]["stabilized"] == pytest.approx(0.07)


def test_without_exclusions_the_same_t12_carries_the_full_gap():
    deal = _resolve(BASE, t12_vacancy=0.15)
    assert _build(deal)["stabilization_exclusions"]["offline_applied"] is False
    assert _build(deal)["asset_vs_market"]["value"] == pytest.approx(0.02)  # 9 points over, capped


def test_offline_units_the_t12_books_separately_are_not_subtracted_twice():
    deal = _resolve(BASE, t12_vacancy=0.10, t12_non_revenue=0.02, box_score=BOX)
    exclusions = _build(deal)["stabilization_exclusions"]
    assert exclusions["t12_non_revenue_rate"] == pytest.approx(0.02)
    assert exclusions["offline_applied"] is False
    assert _build(deal)["asset_vs_market"]["asset_vacancy_stabilized"] == pytest.approx(0.10)


def test_an_asset_in_lease_up_carries_no_gap():
    deal = _resolve(BASE, t12_vacancy=0.15, year_built=2025)
    build = _build(deal)
    assert build["stabilization_exclusions"]["lease_up"] is True
    assert build["asset_vs_market"]["value"] == 0.0
    assert "lease-up" in build["asset_vs_market"]["note"]
    assert _entry(deal)["value"]["stabilized"] == 0.06


def test_pre_leased_vacant_units_are_disclosed():
    deal = _resolve(BASE, pre_leased=["102", "105"])
    exclusions = _build(deal)["stabilization_exclusions"]
    assert exclusions["pre_leased_vacant_units"] == 2
    assert "notice" in exclusions["note"]


# ── 1 and 5. the rent roll sets year 1; the path keeps the curve's timing ──


def test_the_rent_roll_sets_year_one_only():
    low, high = _resolve(BASE, roll_rate=0.04), _resolve(BASE, roll_rate=0.12)
    assert _entry(low)["value"]["start"] == 0.04 and _entry(high)["value"]["start"] == 0.12
    assert _entry(low)["value"]["stabilized"] == _entry(high)["value"]["stabilized"] == pytest.approx(0.06125)
    start = _build(high)["year1_start"]
    assert (start["value"], start["source"]) == (0.12, "deal:rent_roll")
    assert _monthly(high)[0] == 0.12 and _monthly(high)[-1] == pytest.approx(0.06125)


def test_value_add_keeps_its_window_and_the_supply_years():
    deal = _resolve(_supply(0.08, 0.04), t12_vacancy=None, deal_type="value_add")
    value = _entry(deal)["value"]
    assert value["window_months"] == house.HOUSE_STABILIZATION_WINDOW_MONTHS["value_add"] == 24
    assert value["supply_months"] == 36
    monthly = _monthly(deal)
    assert monthly[24:36] == [0.07] * 12 and set(monthly[36:]) == {0.06}


# ── 6. the pricing floor is the vintage floor only ───────────────────────


def test_the_rent_roll_vacancy_is_evidence_not_a_floor():
    deal = _resolve(BASE)
    floors, summary = house_floors(deal, GuardrailProfile(strategy="cashflow", year_built=1984))
    assert floors["physical_vacancy"].value == Decimal("0.050")  # 1980s vintage floor, not the roll's 20%
    assert floors["physical_vacancy"].source == "house:vintage_1980s"
    assert summary["floor"] == 0.05 and summary["floor_anchor"] == "vintage_house_floor"
    assert summary["evidence_candidates"]["rent_roll_vacant"] == pytest.approx(0.2)
    assert "rent_roll_vacant" in summary["evidence_above_floor"]
    assert summary["evidence_candidates"]["t12_vacancy_gpr"] == pytest.approx(0.0625)  # ingested T12
    assert summary["vacancy_build"] == _build(deal)


def test_t12_and_broker_vacancy_no_longer_raise_the_floor():
    deal = _resolve(BASE)
    context = {
        "broker_snapshot": {"underwriting_notes": {"vacancy": "9%"}},
        "trailing_actuals": {"gross_potential_rent": 1_000_000.0, "vacancy_loss": -80_000.0},
    }
    floors, summary = house_floors(deal, GuardrailProfile(strategy="cashflow", year_built=1984), context)
    assert floors["physical_vacancy"].value == Decimal("0.050")
    assert summary["evidence_candidates"]["t12_vacancy_gpr"] == pytest.approx(0.08)
    assert summary["evidence_candidates"]["broker_om"] == pytest.approx(0.09)


def test_deferred_maintenance_still_raises_the_vintage_floor():
    floors, _ = house_floors(_resolve(BASE), GuardrailProfile(strategy="cashflow", year_built=1984,
                                                                deferred_maintenance=True))
    assert floors["physical_vacancy"].value == house.default_physical_vacancy_rate(1984, deferred=True)
    assert floors["physical_vacancy"].value > Decimal("0.05")


def test_pricing_keeps_a_curve_above_the_vintage_floor_below_the_roll():
    deal = _resolve(BASE, overrides={"physical_vacancy": {"value": 0.06, "note": "between floor and roll"}})
    out, findings = apply_guardrails(deal, mode="pricing", profile=GuardrailProfile(strategy="cashflow",
                                                                                   year_built=1984))
    assert not [f for f in findings if f["key"] == "physical_vacancy"]
    assert {row["vacancy_rate"] for row in out["physical_vacancy_curve"]} == {0.06}


def test_pricing_still_raises_a_segment_below_the_vintage_floor():
    deal = _resolve(BASE, overrides={"physical_vacancy": {"value": 0.03, "note": "too low"}})
    out, findings = apply_guardrails(deal, mode="pricing", profile=GuardrailProfile(strategy="cashflow",
                                                                                   year_built=1984))
    vacancy = [f for f in findings if f["key"] == "physical_vacancy"]
    assert vacancy and {f["applied"] for f in vacancy} == {0.05}
    assert {f["floor_source"] for f in vacancy} == {"house:vintage_1980s"}


def test_the_build_discloses_the_floor():
    floor = _build(_resolve(BASE))["floor"]
    assert floor["value"] == 0.05 and floor["source"] == "house:vintage_1980s"
    assert "evidence" in floor["basis"]


# ── 7. review, override, re-resolution ───────────────────────────────────


def test_the_analyst_override_wins_and_the_build_stays_disclosed():
    deal = _resolve(_supply(0.08, 0.04), overrides={"physical_vacancy": {"value": 0.07, "note": "sponsor view"}})
    entry = _entry(deal)
    assert (entry["source"], entry["value"]) == ("analyst", 0.07)
    assert entry["override"]["note"] == "sponsor view"
    assert entry["default_value"]["supply_adjustment"] == 0.01
    assert entry["detail"]["vacancy_build"]["supply"]["value"] == pytest.approx(0.01)
    assert {row["vacancy_rate"] for row in deal["physical_vacancy_curve"]} == {0.07}


def test_re_resolving_without_market_data_keeps_the_build():
    deal = _resolve(_supply(0.08, 0.04))
    again = resolve_assumptions(copy.deepcopy(deal))
    assert _entry(again)["value"] == _entry(deal)["value"]
    assert _entry(again)["source"] == SOURCE
    assert _entry(again)["detail"]["vacancy_build"] == _build(deal)
    assert again["physical_vacancy_curve"] == deal["physical_vacancy_curve"]
    assert _entry(again)["override"] is None


def test_the_build_detail_names_every_component():
    build = _build(_resolve(_supply(0.08, 0.04)))
    assert set(build) >= {"method", "formula", "year1_start", "market_base", "supply", "asset_vs_market",
                          "stabilization_exclusions", "stabilized", "floor"}
    assert build["stabilized"] == pytest.approx(0.06125)
    assert build["formula"].startswith("stabilized = market base")


# ── register, rationale, status ──────────────────────────────────────────


def test_the_register_row_shows_a_compact_breakdown():
    register = build_register(_resolve(_supply(0.08, 0.04)))
    row = next(r for r in register["rows"] if r["key"] == "physical_vacancy")
    assert row["why"]["rationale_known"]
    assert row["default"]["source"] == SOURCE
    breakdown = row["breakdown"]
    labels = [line["label"] for line in breakdown]
    assert labels == ["Year 1 (rent roll)", "Market base", "Supply (years 1-3)", "Asset vs market",
                      "Stabilized", "Pricing floor"]
    assert "submarket" in breakdown[1]["text"]
    assert "+1.00%" in breakdown[2]["text"]


def test_every_new_source_has_a_rationale():
    assert has_rationale(SOURCE)


def test_supply_metrics_are_in_the_vocabulary_and_the_status():
    assert METRICS["supply_pipeline"].unit == "decimal_of_inventory"
    assert METRICS["supply_pipeline_norm"].unit == "decimal_of_inventory"
    assert SUPPLY_PIPELINE_MONTHS == house.HOUSE_SUPPLY_HORIZON_MONTHS
    assert SLOT_METRICS["vacancy_supply"] == ("supply_pipeline", "supply_pipeline_norm")

    status = market_data_status(_registry(_supply(0.08, 0.04)), SUB)
    assert status["slots"]["vacancy_supply"]["supplied"] is True
    missing = market_data_status(_registry(BASE), SUB)
    assert missing["slots"]["vacancy_supply"]["supplied"] is False
    assert any(line.strip().startswith("vacancy_supply: FELL BACK") for line in status_lines(missing))

    resolved = _resolve(_supply(0.08, 0.04))
    assert resolved["assumptions"]["market_data_status"]["slots"]["vacancy_supply"]["supplied"] is True


# ── T12 vacancy evidence at ingest ───────────────────────────────────────


def test_ingest_records_t12_vacancy_and_non_revenue_losses():
    evidence = json.loads(_ingested())["metadata"]["ingest_gate"]["evidence"]["t12"]
    assert evidence["vacancy_loss"] == 375000.0
    assert evidence["source_lines"]["vacancy_loss"] == ["Vacancy Loss"]

    lines = [
        {"label": "Gross Potential Rent", "annual_amount": 1_000_000.0},
        {"label": "Vacancy Loss", "annual_amount": -70_000.0},
        {"label": "Physical Vacancy", "annual_amount": -5_000.0},
        {"label": "Model Unit", "annual_amount": -12_000.0},
        {"label": "Down Unit Loss", "annual_amount": -8_000.0},
    ]
    evidence = _t12_rent_evidence(lines)
    assert evidence["vacancy_loss"] == 75_000.0
    assert evidence["non_revenue_loss"] == 20_000.0
    assert evidence["source_lines"]["non_revenue_loss"] == ["Model Unit", "Down Unit Loss"]
