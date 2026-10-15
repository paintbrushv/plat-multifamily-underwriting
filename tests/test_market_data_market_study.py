"""PlatMarketStudyAgentProvider against synthetic files in the feeder's layout."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("pandas")
pytest.importorskip("pyarrow")

from engine.market_data import Geo, PlatMarketStudyAgentProvider  # noqa: E402
from engine.market_data.providers.market_study import bed_key  # noqa: E402
from tests.market_data_feeders import (  # noqa: E402
    comp,
    costar_row,
    write_comps_json,
    write_costar_submarket,
    write_ensemble,
)

METRO = "dallas_fort_worth_tx_usa"
ALPHA = f"{METRO}_alpha"
BETA = f"{METRO}_beta"


def _rows(stem: str, *, vacancy: float, inventory: int, cap: float, tiers=("All", "4 & 5 Star")):
    rows = []
    for tier in tiers:
        bump = 0.01 if tier != "All" else 0.0
        for i, q in enumerate(["2025 Q3", "2025 Q4", "2026 Q1"]):
            rows.append(costar_row(
                q, tier, is_forecast=False, stem=stem,
                vacancy_rate=vacancy + bump + 0.001 * i,
                stabilized_vacancy=0.06,
                asking_rent_unit=1500.0, effective_rent_unit=1440.0,
                effective_rent_studio=1100.0, effective_rent_1br=1300.0, effective_rent_2br=1650.0,
                effective_rent_growth_yoy=0.01,
                market_cap_rate=cap, inventory_units=inventory,
                under_construction=inventory // 10, deliveries_12mo=inventory // 20,
                absorption_units_12mo=inventory // 25,
            ))
        # The feeder can carry both an EST and a QTD row for the current quarter.
        rows.append(costar_row("2026 Q2 EST", tier, is_forecast=False, stem=stem,
                               vacancy_rate=0.5, inventory_units=inventory, market_cap_rate=cap))
        rows.append(costar_row("2026 Q2 QTD", tier, is_forecast=False, stem=stem,
                               vacancy_rate=vacancy + bump + 0.01, stabilized_vacancy=0.06,
                               asking_rent_unit=1500.0, effective_rent_unit=1425.0,
                               market_cap_rate=cap, inventory_units=inventory,
                               under_construction=inventory // 10))
        for j, q in enumerate(["2026 Q3", "2026 Q4", "2027 Q1", "2027 Q2", "2027 Q3", "2027 Q4"]):
            rows.append(costar_row(q, tier, is_forecast=True, stem=stem,
                                   vacancy_rate=0.2, effective_rent_growth_yoy=0.02 + 0.001 * j,
                                   market_cap_rate=cap + 0.001 * (j + 1), inventory_units=inventory))
    return rows


@pytest.fixture
def root(tmp_path: Path) -> Path:
    write_costar_submarket(tmp_path, ALPHA, _rows(ALPHA, vacancy=0.08, inventory=3000, cap=0.050))
    write_costar_submarket(tmp_path, BETA, _rows(BETA, vacancy=0.12, inventory=1000, cap=0.060, tiers=("All",)))
    return tmp_path


def _provider(root: Path, **kwargs) -> PlatMarketStudyAgentProvider:
    return PlatMarketStudyAgentProvider(root, env={}, **kwargs)


def test_submarket_vacancy_uses_latest_actual_and_qtd_row(root):
    obs = _provider(root).get("vacancy_current", Geo(metro_slug=METRO, submarket="alpha"))
    assert obs is not None
    assert obs.value == pytest.approx(0.09)  # 2026 Q2 QTD, not the EST row or a forecast
    assert obs.unit == "decimal"
    assert obs.as_of == date(2026, 6, 30)
    assert obs.geo == Geo(metro_slug=METRO, submarket=ALPHA, star_tier="All")
    assert obs.confidence == "medium"
    assert obs.provenance.provider == "plat_market_study_agent"
    assert obs.provenance.vintage == "2026Q2"
    assert obs.provenance.path.endswith(f"{ALPHA}.parquet")
    assert len(obs.provenance.content_hash) == 64


def test_submarket_accepts_full_file_stem(root):
    obs = _provider(root).get("vacancy_current", Geo(submarket=ALPHA))
    assert obs is not None and obs.geo.submarket == ALPHA


def test_star_tier_slug_and_fallback_to_all(root):
    provider = _provider(root)
    tier = provider.get("vacancy_current", Geo(metro_slug=METRO, submarket="alpha", star_tier="4_5_star"))
    assert tier.value == pytest.approx(0.10)
    assert tier.geo.star_tier == "4 & 5 Star"
    fallback = provider.get("vacancy_current", Geo(metro_slug=METRO, submarket="beta", star_tier="4 & 5 Star"))
    assert fallback.geo.star_tier == "All"
    assert fallback.value == pytest.approx(0.13)


def test_metro_aggregate_is_inventory_weighted(root):
    obs = _provider(root).get("vacancy_current", Geo(metro_slug=METRO))
    assert obs.geo.submarket is None
    assert obs.value == pytest.approx((0.09 * 3000 + 0.13 * 1000) / 4000)
    assert len(obs.provenance.files) == 2


def test_generic_metro_slug_alias(root):
    obs = _provider(root).get("vacancy_current", Geo(metro_slug="dallas_tx"))
    assert obs is not None and len(obs.provenance.files) == 2


def test_scalar_metrics(root):
    provider = _provider(root)
    geo = Geo(metro_slug=METRO, submarket="alpha")
    assert provider.get("vacancy_stabilized", geo).value == pytest.approx(0.06)
    concession = provider.get("concession_rate", geo)
    assert concession.value == pytest.approx((1500 - 1425) / 1500)
    assert concession.unit == "decimal_of_asking"
    supply = provider.get("supply_risk", geo)
    assert supply.value == pytest.approx(0.1)
    assert supply.attributes["inventory_units"] == 3000
    rents = provider.get("effective_rent_by_bed", geo)
    assert rents.series == {"studio": 1100.0, "1br": 1300.0, "2br": 1650.0}
    assert rents.as_of == date(2026, 3, 31)  # last quarter that reported bed rents


def test_as_of_selects_an_earlier_actual_quarter(root):
    obs = _provider(root).get("vacancy_current", Geo(metro_slug=METRO, submarket="alpha"), as_of=date(2025, 11, 15))
    assert obs.value == pytest.approx(0.081)
    assert obs.as_of == date(2025, 12, 31)
    assert _provider(root).get("vacancy_current", Geo(metro_slug=METRO, submarket="alpha"),
                               as_of=date(2020, 1, 1)) is None


def test_market_cap_rate_series_and_vintage_gate(root):
    provider = _provider(root)
    obs = provider.get("market_cap_rate", Geo(metro_slug=METRO, submarket="alpha"))
    assert obs.is_series
    assert obs.series["2026Q1"] == pytest.approx(0.05)
    assert obs.series["2027Q4"] == pytest.approx(0.056)
    assert obs.horizon == "2025Q3..2027Q4"
    assert obs.attributes["latest_actual_quarter"] == "2026Q2"
    assert provider.get("market_cap_rate", Geo(metro_slug=METRO), as_of=date(2026, 3, 31)) is None


def test_rent_growth_falls_back_to_costar_forecast_rows(root):
    obs = _provider(root).get("rent_growth_forecast", Geo(metro_slug=METRO, submarket="alpha"))
    assert obs.provenance.source_label.startswith("CoStar")
    assert list(obs.series) == ["2026Q3", "2026Q4", "2027Q1", "2027Q2", "2027Q3", "2027Q4"]
    assert obs.series["2026Q3"] == pytest.approx(0.02)
    assert obs.unit == "decimal_yoy"


def test_rent_growth_prefers_ensemble_with_feeder_confidence(root):
    periods = {"2026Q3": 0.021, "2026Q4": 0.024, "2027Q1": 0.027, "2027Q2": 0.03}
    write_ensemble(root, "synthetic-metro/subject-a", metro_slug="dallas_tx", tier="4 & 5 Star",
                   tier_slug="4_5_star", panel_end="2026Q2", periods=periods, confidence_level="Low")
    write_ensemble(root, "synthetic-metro/subject-b", metro_slug="austin_tx", tier="4 & 5 Star",
                   tier_slug="4_5_star", panel_end="2026Q2", periods={"2026Q3": 0.5})
    obs = _provider(root).get("rent_growth_forecast", Geo(metro_slug="dallas_tx", star_tier="4 & 5 Star"))
    assert obs.series == pytest.approx(periods)
    assert obs.confidence == "low"
    assert obs.as_of == date(2026, 6, 30)
    assert obs.provenance.vintage == "2026Q2"
    assert obs.attributes["bands"]["bull"]["2026Q3"] == pytest.approx(0.031)
    assert len(obs.provenance.files) == 2
    # Neither the ensemble nor the CoStar vintage is visible before 2026Q2 starts.
    early = _provider(root).get("rent_growth_forecast", Geo(metro_slug="dallas_tx", star_tier="4 & 5 Star"),
                                as_of=date(2026, 3, 1))
    assert early is None


def test_comps_effective_rent_by_bed(root, tmp_path):
    comps_path = write_comps_json(
        tmp_path / "deal" / "outputs" / "run-1" / "comps" / "comps.json",
        metro_slug="dallas_tx", as_of="2026-05-01",
        comps=[
            comp("comp_one", [("1BR", 1400, 1350, "direct_playwright"), ("2BR", 1800, 1750, "direct_playwright")]),
            comp("comp_two", [("1 Bed", 1500, 1450, "direct_cached"), ("Studio", 1100, 1100, "tracking_csv_salvage"),
                              ("A1", 999, 999, "direct_playwright")]),
        ],
    )
    provider = _provider(root, comps_paths=[comps_path])
    obs = provider.get("effective_rent_by_bed", Geo(metro_slug="dallas_tx"))
    assert obs.series == {"1br": 1400.0, "2br": 1750.0, "studio": 1100.0}
    assert obs.as_of == date(2026, 5, 1)
    assert obs.confidence == "medium"  # median of high, high, medium, low
    assert obs.attributes["comp_count"] == 2
    assert obs.provenance.source_label.endswith("comps.json")
    # Before the comp snapshot existed, the CoStar panel answers instead.
    older = provider.get("effective_rent_by_bed", Geo(metro_slug="dallas_tx"), as_of=date(2026, 4, 1))
    assert older.provenance.source_label.startswith("CoStar")


def test_missing_data_returns_none(root, tmp_path):
    provider = _provider(root)
    assert provider.get("renovation_premium", Geo(metro_slug=METRO)) is None
    assert provider.get("not_a_metric", Geo(metro_slug=METRO)) is None
    assert provider.get("vacancy_current", Geo(metro_slug="nowhere_zz")) is None
    assert provider.get("vacancy_current", Geo()) is None
    assert provider.get("vacancy_current", Geo(cbsa_code="00000")) is None
    empty = _provider(tmp_path / "empty_root_does_not_exist")
    assert not empty.available()
    assert "not found" in empty.unavailable_reason
    assert empty.get("vacancy_current", Geo(metro_slug=METRO)) is None


def test_corrupt_parquet_returns_none(tmp_path):
    bad = tmp_path / "data" / "paid" / "costar-submarket"
    bad.mkdir(parents=True)
    (bad / f"{METRO}_gamma.parquet").write_bytes(b"not a parquet file")
    assert _provider(tmp_path).get("vacancy_current", Geo(metro_slug=METRO)) is None


def test_missing_columns_return_none(tmp_path):
    import pandas as pd

    out = tmp_path / "data" / "paid" / "costar-submarket"
    out.mkdir(parents=True)
    pd.DataFrame({"date": ["2026 Q1"], "star_rating": ["All"], "is_forecast": [False]}).to_parquet(
        out / f"{METRO}_delta.parquet", index=False)
    provider = _provider(tmp_path)
    for metric in ("vacancy_current", "concession_rate", "supply_risk", "market_cap_rate", "effective_rent_by_bed"):
        assert provider.get(metric, Geo(metro_slug=METRO)) is None


def test_deterministic(root):
    geo = Geo(metro_slug=METRO)
    first = [_provider(root).get(m, geo).as_dict() for m in ("vacancy_current", "market_cap_rate", "supply_risk")]
    second = [_provider(root).get(m, geo).as_dict() for m in ("vacancy_current", "market_cap_rate", "supply_risk")]
    assert first == second


@pytest.mark.parametrize("label,expected", [
    ("Studio", "studio"), ("0BR", "studio"), ("1BR", "1br"), ("2 Bed", "2br"),
    ("3x2", "3br"), ("2/2", "2br"), ("A1", None), ("", None),
])
def test_bed_key(label, expected):
    assert bed_key(label) == expected


# ── supply pipeline (ADR 0003 decision o) ────────────────────────────────

GAMMA = f"{METRO}_gamma"


def _supply_rows(*, forecast_deliveries=10, under_construction=60, stabilized=None):
    """2016 Q3-2026 Q2 actuals (5 units delivered a quarter on 1,000), then nine forecast quarters."""
    rows = []
    actual = [f"{y} Q{q}" for y in range(2016, 2027) for q in range(1, 5)][2:-2]
    for quarter in actual:
        for tier in ("All", "3 Star"):
            rows.append(costar_row(
                quarter, tier, is_forecast=False, stem=GAMMA, inventory_units=1000, deliveries=5,
                deliveries_12mo=20, under_construction=under_construction,
                stabilized_vacancy=(stabilized or {}).get(quarter, 0.05),
            ))
    forecast = ["2026 Q3", "2026 Q4", "2027 Q1", "2027 Q2", "2027 Q3", "2027 Q4", "2028 Q1", "2028 Q2", "2028 Q3"]
    for quarter in forecast:
        for tier in ("All", "3 Star"):
            rows.append(costar_row(quarter, tier, is_forecast=True, stem=GAMMA, inventory_units=1000,
                                   deliveries=forecast_deliveries))
    return rows


def test_supply_pipeline_sums_forecast_deliveries_over_the_next_24_months(tmp_path):
    write_costar_submarket(tmp_path, GAMMA, _supply_rows())
    geo = Geo(metro_slug=METRO, submarket="gamma", star_tier="3 Star")
    pipeline = _provider(tmp_path).get("supply_pipeline", geo)
    # 8 forecast quarters x 10 units on 1,000 units of inventory (2028 Q3 is past 24 months).
    assert pipeline.value == pytest.approx(0.08)
    assert pipeline.unit == "decimal_of_inventory"
    assert pipeline.attributes["basis"] == "forecast_deliveries"
    assert pipeline.attributes["quarters"] == ["2026Q3", "2026Q4", "2027Q1", "2027Q2", "2027Q3", "2027Q4",
                                               "2028Q1", "2028Q2"]
    assert pipeline.attributes["inventory_units"] == 1000
    assert pipeline.geo.star_tier == "3 Star"
    assert pipeline.as_of == date(2026, 6, 30)


def test_supply_pipeline_falls_back_to_units_under_construction(tmp_path):
    write_costar_submarket(tmp_path, GAMMA, _supply_rows(forecast_deliveries=None))
    pipeline = _provider(tmp_path).get("supply_pipeline", Geo(metro_slug=METRO, submarket="gamma"))
    assert pipeline.value == pytest.approx(0.06)
    assert pipeline.attributes["basis"] == "under_construction"


def test_supply_pipeline_norm_is_the_trailing_long_run_pace(tmp_path):
    write_costar_submarket(tmp_path, GAMMA, _supply_rows())
    norm = _provider(tmp_path).get("supply_pipeline_norm", Geo(metro_slug=METRO, submarket="gamma",
                                                                star_tier="4 & 5 Star"))
    # 5 units a quarter on 1,000 over the last 40 actual quarters = 0.5% a quarter, 4% per 24 months.
    assert norm.value == pytest.approx(0.04)
    assert norm.attributes["quarters_used"] == 40
    assert norm.attributes["first_quarter"] == "2016Q3" and norm.attributes["last_quarter"] == "2026Q2"
    assert norm.geo.star_tier == "All"  # no 4 & 5 Star slice: the provider falls back and says so


def test_stabilized_vacancy_reports_the_trailing_four_quarters(tmp_path):
    stabilized = {"2025 Q3": 0.04, "2025 Q4": 0.05, "2026 Q1": 0.06, "2026 Q2": 0.07}
    write_costar_submarket(tmp_path, GAMMA, _supply_rows(stabilized=stabilized))
    obs = _provider(tmp_path).get("vacancy_stabilized", Geo(metro_slug=METRO, submarket="gamma"))
    assert obs.value == pytest.approx(0.07)
    assert obs.attributes["trailing_4q_mean"] == pytest.approx(0.055)
