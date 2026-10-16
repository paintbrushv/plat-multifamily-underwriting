"""Down units are a capital project with a schedule (ADR 0003 decision q, UPL-110).

A unit the rent roll shows as down (down, offline, uninhabitable, under
renovation, "vacant-down") is not weak demand: it needs repair or renovation,
sometimes a full gut, before it can be leased. So:

1. the rent-roll parser classifies every unit as ``down``, ``non_revenue``
   (model, admin, employee, office, corporate), ``vacant``, ``notice`` or
   ``occupied``, records the counts and the down unit ids, and the
   reconciliation gate still ties;
2. the asset-vs-market vacancy gap leaves the down units out;
3. each down unit gets a scope (make_ready, heavy_repair, full_gut; house
   default heavy_repair, flagged), a cost (PCA / analyst per unit > house
   scope x vintage) and a construction schedule (start lag, duration, crew
   pace, months to lease);
4. the restoration budget is its own capital-plan line, spent monthly as the
   units are worked, funded at close (equity, or a lender holdback), never
   part of immediate capex or the value-add renovation budget; the units earn
   no rent until restored, then lease at market;
5. backsolve carries the budget at every candidate price, so the price falls.

Every rent roll, unit and figure here is synthetic.
"""

from __future__ import annotations

import copy
import csv
import json
import shutil
from datetime import date
from functools import lru_cache
from pathlib import Path

import pytest

from engine import underwriting_policy as house
from engine.assumptions import resolve_assumptions
from engine.assumptions.rationale import has_rationale
from engine.assumptions.register import build_register
from engine.backsolve import backsolve_price
from engine.engine import run_underwriting
from engine.ingest.ingestion_pipeline import build_deal_from_documents
from engine.ingest.rent_roll_parser import classify_unit_status, parse_rent_roll
from engine.market_data import Geo, MarketDataRegistry, SyntheticProvider
from engine.modules.down_units import compute_down_unit_restoration
from engine.modules.time_grid import TimeGrid
from engine.workflow.fields import BY_NAME
from tests import deal_library as dl
from tests.yardi_fixtures import write_yardi_rent_roll, write_yardi_t12

FIXTURES = Path(__file__).parent / "fixtures"
T12 = FIXTURES / "entrata_style_t12.csv"
SUB = Geo(metro_slug="synthetic_metro", submarket="Synthetic North")
MSA = Geo(metro_slug="synthetic_metro")
BENCHMARK = {"rate": "0.04", "as_of": "2026-10-01", "source": "synthetic:down-units"}
DOWN_KEYS = (
    "down_units",
    "down_unit_scope",
    "down_unit_cost_per_unit",
    "down_unit_start_lag_months",
    "down_unit_duration_months",
    "down_unit_pace_per_month",
    "down_unit_months_to_lease",
    "down_unit_funding",
)

# (unit, type, sqft, beds, baths, status, rent, market rent, lease end)
ROLL_ROWS = [
    ("101", "1BR/1BA", 700, 1, 1, "Occupied", 1180, 1200, "2027-05-31"),
    ("102", "1BR/1BA", 700, 1, 1, "Down", 0, 1200, ""),
    ("103", "1BR/1BA", 700, 1, 1, "Vacant", 0, 1200, ""),
    ("104", "1BR/1BA", 700, 1, 1, "Notice", 1190, 1200, "2026-12-31"),
    ("105", "1BR/1BA", 700, 1, 1, "Model", 0, 1200, ""),
    ("106", "1BR/1BA", 700, 1, 1, "Occupied", 1200, 1200, "2027-08-31"),
    ("201", "2BR/2BA", 950, 2, 2, "Occupied", 1480, 1500, "2027-03-31"),
    ("202", "2BR/2BA", 950, 2, 2, "Vacant-Down", 0, 1500, ""),
    ("203", "2BR/2BA", 950, 2, 2, "Offline - Fire", 0, 1500, ""),
    ("204", "2BR/2BA", 950, 2, 2, "Occupied", 1500, 1500, "2027-06-30"),
    ("205", "2BR/2BA", 950, 2, 2, "Employee", 1100, 1500, "2027-01-31"),
    ("206", "2BR/2BA", 950, 2, 2, "Occupied", 1490, 1500, "2027-09-30"),
]
HEADER = ["Unit", "Type", "Sqft", "Beds", "Baths", "Status", "Monthly Rent", "Market Rent", "Lease End"]


def _write_roll(path: Path, rows=ROLL_ROWS) -> Path:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        writer.writerows(rows)
    return path


def _registry(data):
    provider = SyntheticProvider(data, as_of=date(2026, 6, 30), confidence="high")
    return MarketDataRegistry([provider])


MARKET = {("vacancy_stabilized", SUB): 0.06, ("vacancy_stabilized", MSA): 0.065}


@lru_cache(maxsize=None)
def _ingested_text(tmp: str, extra: str = "{}") -> str:
    roll = _write_roll(Path(tmp) / "down_roll.csv")
    deal = build_deal_from_documents(
        property_id="Synthetic Down Units",
        rent_roll_path=roll,
        t12_path=T12,
        analysis_start="2026-08",
        analysis_end="2031-07",
        year_built=1984,
        msa="Synthetic Metro",
        metro_slug="synthetic_metro",
        submarket="Synthetic North",
        market=_registry(MARKET),
        **json.loads(extra),
    )
    return json.dumps(deal)


@pytest.fixture(scope="module")
def roll_dir(tmp_path_factory):
    return str(tmp_path_factory.mktemp("down_units"))


def _ingested(roll_dir, **extra):
    return json.loads(_ingested_text(roll_dir, json.dumps(extra, sort_keys=True)))


def _entries(deal):
    return deal["assumptions"]["entries"]


def _block(deal):
    return deal["capital_plan"]["down_unit_restoration"]


# ── 1. parse: status classes, counts, the gate still ties ────────────────


@pytest.mark.parametrize(
    "status, expected",
    [
        ("Down", "down"),
        ("DOWN", "down"),
        ("Offline - Fire", "down"),
        ("Off-line", "down"),
        ("Uninhabitable", "down"),
        ("Renovation", "down"),
        ("Under Renovation", "down"),
        ("Vacant-Down", "down"),
        ("Vacant Down", "down"),
        ("Model", "non_revenue"),
        ("Admin", "non_revenue"),
        ("Employee", "non_revenue"),
        ("Office", "non_revenue"),
        ("Corporate", "non_revenue"),
        ("Non-Revenue", "non_revenue"),
        ("Vacant", "vacant"),
        ("Vacant Unrented", "vacant"),
        ("Vacant-Rented", "vacant"),
        ("", "vacant"),
        ("Notice", "notice"),
        ("Notice Rented", "notice"),
        ("Occupied-NTV", "notice"),
        ("Occupied", "occupied"),
        ("Occupied - Renovated", "occupied"),
        ("Current", "occupied"),
    ],
)
def test_unit_statuses_are_classified(status, expected):
    assert classify_unit_status(status) == expected


def test_a_plain_rent_roll_records_status_counts_and_down_unit_ids(tmp_path):
    roll = parse_rent_roll(_write_roll(tmp_path / "roll.csv"))
    assert roll["status_counts"] == {"occupied": 5, "notice": 1, "vacant": 1, "down": 3, "non_revenue": 2}
    assert sum(roll["status_counts"].values()) == roll["total_units"] == 12
    assert [u["unit"] for u in roll["down_units"]] == ["102", "202", "203"]
    assert [u["cohort_id"] for u in roll["down_units"]] == ["1br1ba", "2br2ba", "2br2ba"]
    assert [u["status"] for u in roll["down_units"]] == ["Down", "Vacant-Down", "Offline - Fire"]
    assert roll["non_revenue_units"] == ["105", "205"]
    # The rent roll's own vacancy still counts a down unit as vacant (evidence as printed) ...
    assert roll["vacant_units"] == 5
    assert roll["physical_vacancy_rate"] == pytest.approx(round(5 / 12, 4))
    # ... and the rate without the down units is recorded beside it.
    assert roll["physical_vacancy_rate_excluding_down"] == pytest.approx(round(2 / 9, 4))
    classification = roll["reconciliation"]["status_classification"]
    assert classification["counts"] == roll["status_counts"]
    assert classification["down_unit_ids"] == ["102", "202", "203"]
    assert classification["non_revenue_unit_ids"] == ["105", "205"]


@pytest.mark.parametrize("in_non_rev", [True, False])
def test_a_yardi_export_ties_whether_it_counts_down_units_as_non_revenue_or_vacant(tmp_path, in_non_rev):
    path = write_yardi_rent_roll(
        tmp_path / "rr.xlsx", off_market={"105": "DOWN", "110": "MODEL"}, off_market_in_non_rev=in_non_rev,
    )
    roll = parse_rent_roll(path)
    assert roll["status_counts"] == {"occupied": 8, "notice": 0, "vacant": 2, "down": 1, "non_revenue": 1}
    assert [u["unit"] for u in roll["down_units"]] == ["105"]
    assert roll["non_revenue_units"] == ["110"]
    reconciliation = roll["reconciliation"]
    assert reconciliation["status"] == "PASS"
    assert reconciliation["units"]["status"] == "PASS"
    assert reconciliation["vacant_units"]["status"] == "PASS"
    assert reconciliation["vacant_units"]["parsed"] == 4  # 2 vacant + the down unit + the model
    export = reconciliation["vacant_units"]["export"]
    assert export == (2 if in_non_rev else 4)
    assert "down" in reconciliation["status_classification"]["note"].lower()


def test_ingest_records_the_classification_in_the_evidence_and_the_gate(roll_dir):
    deal = _ingested(roll_dir)
    evidence = deal["metadata"]["ingest_gate"]["evidence"]["rent_roll"]
    assert evidence["status_counts"]["down"] == 3
    assert [u["unit"] for u in evidence["down_units"]] == ["102", "202", "203"]
    assert evidence["physical_vacancy_rate_excluding_down"] == pytest.approx(round(2 / 9, 4))
    gate = deal["metadata"]["ingest_gate"]["rent_roll_reconciliation"]
    assert gate["status_classification"]["down_unit_ids"] == ["102", "202", "203"]


# ── 2. the asset-vs-market gap leaves the down units out ─────────────────


def _reresolved(roll_dir, *, t12_vacancy, overrides=None):
    deal = _ingested(roll_dir)
    deal.pop("assumptions", None)
    for row in deal["physical_vacancy_curve"]:
        row["vacancy_rate"] = None
    t12 = deal["metadata"]["ingest_gate"]["evidence"]["t12"]
    t12["vacancy_loss"] = round(t12_vacancy * t12["gross_potential_rent"], 2)
    t12.pop("non_revenue_loss", None)
    return resolve_assumptions(deal, market=_registry(MARKET), overrides=overrides)


def _build(deal):
    return _entries(deal)["physical_vacancy"]["detail"]["vacancy_build"]


def test_down_units_are_excluded_from_the_asset_vs_market_gap(roll_dir):
    build = _build(_reresolved(roll_dir, t12_vacancy=0.40))
    exclusions = build["stabilization_exclusions"]
    # No box score: the rent roll's down units are the offline units of the vacancy build.
    assert exclusions["offline_source"] == "rent_roll.down_units"
    assert exclusions["offline_units"] == 3
    assert exclusions["offline_share"] == pytest.approx(0.25)
    assert exclusions["offline_applied"] is True
    assert exclusions["notice_units"] == 1
    gap = build["asset_vs_market"]
    # 40% T12 vacancy less 25% of units down = 15%, 9 points over the 6% market.
    assert gap["asset_vacancy"] == pytest.approx(0.40)
    assert gap["asset_vacancy_stabilized"] == pytest.approx(0.15)
    assert gap["gap"] == pytest.approx(0.09)
    assert "offline" in gap["note"]


def test_a_t12_that_books_down_units_apart_is_not_reduced_twice(roll_dir):
    deal = _ingested(roll_dir)
    deal.pop("assumptions", None)
    for row in deal["physical_vacancy_curve"]:
        row["vacancy_rate"] = None
    t12 = deal["metadata"]["ingest_gate"]["evidence"]["t12"]
    t12["vacancy_loss"] = round(0.10 * t12["gross_potential_rent"], 2)
    t12["non_revenue_loss"] = round(0.05 * t12["gross_potential_rent"], 2)
    build = _build(resolve_assumptions(deal, market=_registry(MARKET)))
    assert build["stabilization_exclusions"]["offline_applied"] is False
    assert build["asset_vs_market"]["asset_vacancy_stabilized"] == pytest.approx(0.10)


def test_without_modeled_down_units_the_gap_is_measured_as_before(roll_dir):
    build = _build(_reresolved(roll_dir, t12_vacancy=0.30, overrides={"down_units": 0}))
    assert build["stabilization_exclusions"]["offline_source"] is None
    assert build["stabilization_exclusions"]["offline_applied"] is False
    assert build["asset_vs_market"]["asset_vacancy_stabilized"] == pytest.approx(0.30)


def test_year_one_vacancy_starts_without_the_down_units(roll_dir):
    deal = _ingested(roll_dir)
    value = _entries(deal)["physical_vacancy"]["value"]
    # The down units are offline in the restoration schedule; counting them vacant too would charge them twice.
    assert value["start"] == pytest.approx(round(2 / 9, 4))
    cleared = _reresolved(roll_dir, t12_vacancy=0.0625, overrides={"down_units": 0})
    assert _entries(cleared)["physical_vacancy"]["value"]["start"] == pytest.approx(round(5 / 12, 4))


# ── 3. entries: count, scope, cost precedence, schedule, funding ─────────


def test_deals_without_down_units_carry_no_down_unit_entries():
    deal = dl.ingest(dl.load_deal("a_stabilized_1990s"))
    assert not [key for key in _entries(deal) if key.startswith("down_unit")]
    assert "down_unit_restoration" not in (deal.get("capital_plan") or {})


def test_every_down_unit_entry_is_recorded_with_a_rationale_and_review(roll_dir):
    entries = _entries(_ingested(roll_dir))
    for key in DOWN_KEYS:
        assert key in entries, key
        assert has_rationale(entries[key]["source"]), (key, entries[key]["source"])
    count = entries["down_units"]
    assert (count["value"], count["source"]) == (3, "deal:rent_roll")
    assert count["detail"]["status_counts"] == {"occupied": 5, "notice": 1, "vacant": 1, "down": 3, "non_revenue": 2}
    assert count["detail"]["down_unit_ids"] == ["102", "202", "203"]
    for key in DOWN_KEYS[1:]:
        assert entries[key]["source"].startswith("house:"), key
        assert entries[key]["review_required"] is True, key


def test_the_default_scope_is_heavy_repair_and_asks_the_analyst_to_classify(roll_dir):
    entry = _entries(_ingested(roll_dir))["down_unit_scope"]
    assert house.HOUSE_DOWN_UNIT_DEFAULT_SCOPE == "heavy_repair"
    assert (entry["value"], entry["source"]) == ("heavy_repair", "house:down_unit_scope")
    assert entry["review_required"] is True
    assert "classify" in entry["basis"].lower()


def test_house_cost_is_scope_times_vintage(roll_dir):
    deal = _ingested(roll_dir)
    entry = _entries(deal)["down_unit_cost_per_unit"]
    assert house.down_unit_vintage_factor(1984) == house.HOUSE_DOWN_UNIT_VINTAGE_COST_FACTORS["1980s"]
    expected = float(house.default_down_unit_cost_per_unit("heavy_repair", 1984))
    assert expected == float(house.HOUSE_DOWN_UNIT_COST_PER_UNIT["heavy_repair"]
                             * house.HOUSE_DOWN_UNIT_VINTAGE_COST_FACTORS["1980s"])
    assert (entry["value"], entry["source"]) == (expected, "house:down_unit_cost")
    assert entry["detail"]["total_budget"] == pytest.approx(3 * expected)
    # make_ready is the top of the vintage turn band; a full gut costs more than a heavy repair.
    assert house.default_down_unit_cost_per_unit("make_ready", 1984) == house.VINTAGE_FRAMEWORK_2026["1980s"]["turn_high"]
    assert house.default_down_unit_cost_per_unit("full_gut", 1984) > house.default_down_unit_cost_per_unit(
        "heavy_repair", 1984)
    # Older vintages cost more to open up (lead paint, asbestos, galvanized plumbing).
    assert house.default_down_unit_cost_per_unit("heavy_repair", 1972) > house.default_down_unit_cost_per_unit(
        "heavy_repair", 1984)
    # Unknown vintage: the highest factor, flagged.
    assert house.down_unit_vintage_factor(None) == max(house.HOUSE_DOWN_UNIT_VINTAGE_COST_FACTORS.values())


def test_the_analyst_scope_for_all_units_moves_the_cost_and_schedule_defaults(roll_dir):
    deal = resolve_assumptions(_ingested(roll_dir), overrides={"down_unit_scope": "full_gut"})
    entries = _entries(deal)
    assert (entries["down_unit_scope"]["value"], entries["down_unit_scope"]["source"]) == ("full_gut", "analyst")
    assert entries["down_unit_scope"]["review_required"] is False
    assert entries["down_unit_cost_per_unit"]["value"] == float(house.default_down_unit_cost_per_unit("full_gut", 1984))
    assert entries["down_unit_start_lag_months"]["value"] == house.HOUSE_DOWN_UNIT_START_LAG_MONTHS["full_gut"]
    assert entries["down_unit_duration_months"]["value"] == house.HOUSE_DOWN_UNIT_DURATION_MONTHS["full_gut"]


def test_cost_precedence_pca_then_analyst_per_unit_then_house(roll_dir):
    deal = _ingested(
        roll_dir,
        pca={"down_unit_costs": {"102": 31_000.0, "202": 9_000.0}},
        down_unit_scopes={"203": "full_gut"},
    )
    units = {u["unit_id"]: u for u in _block(deal)["units"]}
    assert (units["102"]["cost"], units["102"]["cost_source"]) == (31_000.0, "deal:pca")
    assert (units["202"]["cost"], units["202"]["cost_source"]) == (9_000.0, "deal:pca")
    # 203: the analyst classified it a full gut and no PCA prices it: house full-gut cost and schedule.
    assert units["203"]["scope"] == "full_gut" and units["203"]["scope_source"] == "analyst"
    assert units["203"]["cost"] == float(house.default_down_unit_cost_per_unit("full_gut", 1984))
    assert units["203"]["cost_source"] == "house:down_unit_cost"
    assert units["203"]["start_lag_months"] == house.HOUSE_DOWN_UNIT_START_LAG_MONTHS["full_gut"]
    assert units["203"]["duration_months"] == house.HOUSE_DOWN_UNIT_DURATION_MONTHS["full_gut"]
    detail = _entries(deal)["down_unit_cost_per_unit"]["detail"]
    assert {row["unit_id"]: row["cost_source"] for row in detail["units"]} == {
        "102": "deal:pca", "202": "deal:pca", "203": "house:down_unit_cost"}
    assert detail["total_budget"] == pytest.approx(31_000 + 9_000 + units["203"]["cost"])

    # An analyst cost on a unit the PCA does not price wins over the house; the PCA still wins over the analyst.
    edited = copy.deepcopy(deal)
    for unit in edited["capital_plan"]["down_unit_restoration"]["units"]:
        if unit["unit_id"] in ("102", "203"):
            unit["cost"] = 12_345.0
            unit.pop("cost_source", None)
    units = {u["unit_id"]: u for u in _block(resolve_assumptions(edited))["units"]}
    assert (units["203"]["cost"], units["203"]["cost_source"]) == (12_345.0, "analyst")
    assert (units["102"]["cost"], units["102"]["cost_source"]) == (31_000.0, "deal:pca")
    assert units["102"]["analyst_cost"] == 12_345.0


def test_the_down_unit_count_override_models_fewer_units(roll_dir):
    deal = resolve_assumptions(_ingested(roll_dir), overrides={"down_units": 1})
    assert [u["unit_id"] for u in _block(deal)["units"]] == ["102"]
    assert _entries(deal)["down_units"]["source"] == "analyst"
    assert _entries(deal)["down_unit_cost_per_unit"]["detail"]["total_budget"] == pytest.approx(
        float(house.default_down_unit_cost_per_unit("heavy_repair", 1984)))


def test_describe_deal_fields_say_why_they_matter():
    for name in ("down_unit_scope", "down_unit_cost_per_unit", "down_unit_pace_per_month", "down_unit_funding"):
        field = BY_NAME[name]
        assert field.group == "Capital plan"
        assert "Why it matters" in field.description and field.why
    assert BY_NAME["down_unit_scope"].choices == house.DOWN_UNIT_SCOPES
    assert BY_NAME["down_unit_funding"].choices == house.DOWN_UNIT_FUNDING_MODES


def test_ingest_inputs_become_overrides_only_when_the_roll_has_down_units(roll_dir, tmp_path):
    deal = _ingested(roll_dir, down_unit_inputs={"scope": "make_ready", "pace_units_per_month": 2})
    entries = _entries(deal)
    assert (entries["down_unit_scope"]["value"], entries["down_unit_scope"]["source"]) == ("make_ready", "analyst")
    assert (entries["down_unit_pace_per_month"]["value"], entries["down_unit_pace_per_month"]["source"]) == (
        2, "analyst")
    clean = [row for row in ROLL_ROWS if row[5] not in ("Down", "Vacant-Down", "Offline - Fire")]
    plain = build_deal_from_documents(
        property_id="Synthetic No Down Units", rent_roll_path=_write_roll(tmp_path / "clean.csv", clean),
        t12_path=T12, analysis_start="2026-08", analysis_end="2031-07", year_built=1984, market=None,
        down_unit_inputs={"scope": "full_gut"},
    )
    assert "down_unit_scope" not in _entries(plain)
    assert "down_unit_inputs_ignored_no_down_units" in plain["metadata"]["intake_sanity_flags"]


# ── 4. engine: schedule, monthly capex, rent restored, funding ───────────

BLOCK = {
    "units": [
        {"unit_id": "1", "cohort_id": "1BR"},
        {"unit_id": "2", "cohort_id": "1BR"},
        {"unit_id": "3", "cohort_id": "2BR", "scope": "full_gut", "cost": 50_000.0,
         "start_lag_months": 3, "duration_months": 4},
    ],
    "scope": "heavy_repair",
    "cost_per_unit": 20_000.0,
    "start_lag_months": 1,
    "duration_months": 2,
    "pace_units_per_month": 1,
    "months_to_lease": 1,
    "funding": "equity_at_close",
}
BUDGET = 2 * 20_000.0 + 50_000.0


def _with_block(deal, **changes):
    out = copy.deepcopy(deal)
    out.setdefault("capital_plan", {})["down_unit_restoration"] = {**copy.deepcopy(BLOCK), **changes}
    return out


def test_the_schedule_follows_start_lag_pace_duration_and_months_to_lease(minimal_deal_inputs):
    grid = TimeGrid.build("2026-07", "2031-06")
    result = compute_down_unit_restoration(grid, _with_block(minimal_deal_inputs))
    units = {u["unit_id"]: u for u in result["units"]}
    # One crew start a month: unit 1 starts after its 1-month lag, unit 2 waits a month for the crew.
    assert (units["1"]["start_month"], units["1"]["completion_month"], units["1"]["rent_start_month"]) == (
        "2026-08", "2026-09", "2026-11")
    assert (units["2"]["start_month"], units["2"]["completion_month"], units["2"]["rent_start_month"]) == (
        "2026-09", "2026-10", "2026-12")
    # The full gut waits 3 months for design and permits, then takes 4.
    assert (units["3"]["start_month"], units["3"]["completion_month"], units["3"]["rent_start_month"]) == (
        "2026-10", "2027-01", "2027-03")
    assert result["summary"]["units"] == 3
    assert result["summary"]["total_budget"] == BUDGET
    assert result["summary"]["last_rent_start_month"] == "2027-03"


def test_capex_is_spent_evenly_over_each_units_work_and_sums_to_the_budget(minimal_deal_inputs):
    grid = TimeGrid.build("2026-07", "2031-06")
    result = compute_down_unit_restoration(grid, _with_block(minimal_deal_inputs))
    capex = {row["month"]: row["capex"] for row in result["by_month"] if row["capex"]}
    assert capex == {"2026-08": 10_000.0, "2026-09": 20_000.0, "2026-10": 22_500.0,
                     "2026-11": 12_500.0, "2026-12": 12_500.0, "2027-01": 12_500.0}
    assert sum(row["capex"] for row in result["by_month"]) == pytest.approx(BUDGET)
    assert result["by_analysis_year"][0]["capex"] == pytest.approx(BUDGET)
    offline = {row["month"]: row["offline_units"] for row in result["by_month"]}
    assert offline["2026-07"] == 3 and offline["2026-11"] == 2 and offline["2027-02"] == 1 and offline["2027-03"] == 0


def test_rent_is_lost_until_restored_then_leased_at_market(minimal_deal_inputs):
    base = run_underwriting(copy.deepcopy(minimal_deal_inputs))
    results = run_underwriting(_with_block(minimal_deal_inputs))
    before = {r["month"]: r for r in base["revenue"]["base_rent"]["by_month"]}
    after = {r["month"]: r for r in results["revenue"]["base_rent"]["by_month"]}
    # Month 1: all three units are offline (no rent, out of GPR).
    assert after["2026-07"]["market_rent"] == pytest.approx(before["2026-07"]["market_rent"] - 2 * 1250 - 1550)
    # 2026-11: unit 1 is leased, units 2 and 3 still offline.
    assert after["2026-11"]["market_rent"] == pytest.approx(before["2026-11"]["market_rent"] - 1250 - 1550)
    # From 2027-03 every unit is back, at market rent (no premium, no loss to lease on the new leases).
    for month in ("2027-03", "2029-01", "2031-06"):
        assert after[month]["market_rent"] == pytest.approx(before[month]["market_rent"])
        assert after[month]["inplace_rent"] > before[month]["inplace_rent"]
    restored = results["down_unit_restoration"]
    assert restored["by_month"][0]["down_unit_loss"] == pytest.approx(2 * 1250 + 1550)
    assert results["metrics"]["coc"]["cash_on_cash_year_1"] < base["metrics"]["coc"]["cash_on_cash_year_1"]


def test_a_rent_premium_applies_only_when_the_analyst_sets_one(minimal_deal_inputs):
    plain = run_underwriting(_with_block(minimal_deal_inputs))
    premium = run_underwriting(_with_block(minimal_deal_inputs, rent_premium_monthly=100.0))
    month = {r["month"]: r for r in premium["revenue"]["base_rent"]["by_month"]}["2028-01"]
    reference = {r["month"]: r for r in plain["revenue"]["base_rent"]["by_month"]}["2028-01"]
    assert month["market_rent"] == pytest.approx(reference["market_rent"] + 3 * 100.0)


def test_the_budget_is_funded_at_close_never_in_operating_cash_flow(minimal_deal_inputs):
    base = run_underwriting(copy.deepcopy(minimal_deal_inputs))
    results = run_underwriting(_with_block(minimal_deal_inputs))
    stated = minimal_deal_inputs["purchase_assumptions"]["total_equity_basis"]
    assert results["metrics"]["coc"]["components"]["equity_basis"] == pytest.approx(stated + BUDGET)
    assert sum(m["total_capex"] for m in results["cashflow"]["by_month"]) == 0
    shown = {m["month"]: m.get("down_unit_restoration_capex", 0) for m in results["cashflow"]["by_month"]}
    assert sum(shown.values()) == pytest.approx(BUDGET)
    assert results["cashflow"]["by_year"][0]["down_unit_restoration_capex"] == pytest.approx(77_500.0)  # calendar 2026
    line = results["capital_plan"]["down_unit_restoration"]
    assert line["label"] == "Down-unit restoration"
    assert (line["amount"], line["funding"], line["units"]) == (BUDGET, "equity_at_close", 3)
    assert line["in_total_equity_basis"] is True and line["in_total_unlevered_basis"] is True
    assert line["added_by_engine"] == BUDGET
    assert results["metrics"]["irr"]["levered_irr"] < base["metrics"]["irr"]["levered_irr"]


def test_a_lender_holdback_moves_the_unlevered_basis_only(minimal_deal_inputs):
    results = run_underwriting(_with_block(minimal_deal_inputs, funding="lender_holdback"))
    stated = minimal_deal_inputs["purchase_assumptions"]["total_equity_basis"]
    assert results["metrics"]["coc"]["components"]["equity_basis"] == pytest.approx(stated)
    line = results["capital_plan"]["down_unit_restoration"]
    assert line["in_total_equity_basis"] is False and line["in_total_unlevered_basis"] is True


def test_never_double_counted_with_immediate_capex_or_the_renovation_budget(minimal_deal_inputs):
    deal = _with_block(minimal_deal_inputs)
    deal["capital_plan"]["immediate_capex"] = {"amount": 400_000.0}
    deal["renovation_programs"] = [{
        "program_id": "VA1", "program_name": "Classic 1BR", "target_cohort": "1BR", "output_cohort": "1BR_reno",
        "strategy": "proactive", "monthly_pace": 60, "renovation_cost_per_unit": 8_000.0,
        "rent_premium_monthly": 125.0, "downtime_days": 21, "start_month": "2026-07", "end_month": "2027-06",
    }]
    results = run_underwriting(deal)
    plan = results["capital_plan"]
    # The value-add program renovates the 48 rentable 1BR units, never the two down units.
    assert results["renovations"]["summary"]["total_units_renovated"] == 48
    assert plan["renovation_budget"]["amount"] == pytest.approx(48 * 8_000.0)
    assert plan["immediate_capex"]["amount"] == 400_000.0
    assert plan["down_unit_restoration"]["amount"] == BUDGET
    stated = minimal_deal_inputs["purchase_assumptions"]["total_equity_basis"]
    assert results["metrics"]["coc"]["components"]["equity_basis"] == pytest.approx(stated + 400_000.0 + BUDGET)
    # Renovation capex runs through operating cash flow here (operating_cash_flow funding); the restoration does not.
    assert sum(m["total_capex"] for m in results["cashflow"]["by_month"]) == pytest.approx(48 * 8_000.0)


# ── 5. backsolve: carried at every candidate price ───────────────────────


def _library_with_down_units(tmp_path: Path, count: int, *, slug: str = "a_stabilized_1990s") -> dict:
    deal = dl.load_deal(slug)
    folder = tmp_path / f"{slug}_down"
    shutil.copytree(deal["_dir"], folder)
    rows = list(csv.reader(open(folder / "rent_roll.csv", encoding="utf-8")))
    changed = 0
    for row in rows[1:]:
        if row[5] == "Occupied" and changed < count:
            row[5], row[6], row[8] = "Down", "0", ""
            changed += 1
    with open(folder / "rent_roll.csv", "w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(rows)
    deal["_dir"] = folder
    return dl.ingest(deal)


def _solve(canonical):
    result = backsolve_price(
        canonical, target_coc="0.06",
        policy={"version": "plat.backsolve-policy/1", "strategy": "cashflow"},
        benchmark=BENCHMARK, min_price="15000000.00", max_price="40000000.00",
    )
    assert result["status"] == "converged"
    return result


@lru_cache(maxsize=None)
def _baseline_price() -> float:
    return _solve(dl.ingest(dl.load_deal("a_stabilized_1990s")))["summary"]["solved_purchase_price"]


def test_backsolve_price_falls_and_the_equity_basis_carries_the_budget(tmp_path):
    canonical = _library_with_down_units(tmp_path, 6)
    block = canonical["capital_plan"]["down_unit_restoration"]
    assert len(block["units"]) == 6
    result = _solve(canonical)
    assert result["summary"]["solved_purchase_price"] < _baseline_price()
    budget = result["summary"]["capital_plan"]["down_unit_restoration"]["amount"]
    expected_budget = 6 * float(house.default_down_unit_cost_per_unit("heavy_repair", 1994))
    assert budget == pytest.approx(expected_budget)
    purchase, fund = result["case"]["purchase_assumptions"], result["case"]["fund_assumptions"]
    price = purchase["purchase_price"]
    expected = (purchase["equity_contribution"] + purchase["closing_costs"] + price * fund["acquisition_fee_pct"]
                + fund["partnership_closing_costs"] + expected_budget)
    assert purchase["total_equity_basis"] == pytest.approx(expected, abs=0.01)
    # The priced case states the amount it carries: the engine adds nothing more.
    assert result["case"]["capital_plan"]["down_unit_restoration"]["amount_in_stated_basis"] == pytest.approx(
        expected_budget)
    assert result["results"]["metrics"]["coc"]["components"]["equity_basis"] == pytest.approx(
        purchase["total_equity_basis"], abs=0.01)
    summary = result["summary"]["capital_plan"]["down_unit_restoration"]
    assert summary["included_in_total_equity_basis"] is True
    assert "year-1" in summary["coc_note"].lower()


def test_the_exit_cap_anchor_adds_back_the_down_units_lost_rent(tmp_path):
    from decimal import Decimal

    from engine.assumptions import exit_cap

    canonical = _library_with_down_units(tmp_path, 6)
    price = canonical["purchase_assumptions"]["purchase_price"]
    preview = exit_cap.preview_noi(canonical, price)
    results = run_underwriting(exit_cap.preview_case(canonical, price))
    loss = Decimal(str(results["down_unit_restoration"]["by_analysis_year"][0]["down_unit_loss"]))
    # A temporary, capital-driven hole in year-1 NOI must not compress the entry-anchored exit cap.
    assert loss > 0 and preview["year_1_down_unit_loss"] == loss
    assert preview["year_1"] == Decimal(str(results["metrics"]["noi"]["year_1_noi"])) + loss
    assert "lost rent added back" in canonical["assumptions"]["entries"]["exit_cap_rate"]["basis"]


def test_a_down_unit_holdback_upsizes_the_loan_instead_of_the_equity(tmp_path):
    canonical = _library_with_down_units(tmp_path, 6)
    canonical = resolve_assumptions(canonical, overrides={"down_unit_funding": "lender_holdback"})
    result = _solve(canonical)
    case = result["case"]
    purchase = case["purchase_assumptions"]
    budget = result["summary"]["capital_plan"]["down_unit_restoration"]["amount"]
    sized = purchase["purchase_price"] - purchase["equity_contribution"]
    assert case["debt_terms"]["commitment"] == pytest.approx(sized + budget, abs=0.01)
    assert result["summary"]["capital_plan"]["down_unit_restoration"]["loan_upsized_by_holdback"] is True


# ── register, rationale ──────────────────────────────────────────────────


def test_the_register_shows_the_restoration_under_capital(roll_dir):
    register = build_register(_ingested(roll_dir))
    rows = {row["key"]: row for row in register["rows"]}
    for key in DOWN_KEYS:
        assert rows[key]["group"] == "Capital/Capex", key
        assert rows[key]["why"]["rationale_known"], key
    line = register["capital_plan"]["down_unit_restoration"]
    assert line["units"] == 3
    assert line["scope"] == "heavy_repair"
    assert line["total_budget"] == pytest.approx(3 * float(house.default_down_unit_cost_per_unit("heavy_repair", 1984)))
    assert line["funding"] == "equity_at_close"
    assert "separate" in line["note"].lower()
