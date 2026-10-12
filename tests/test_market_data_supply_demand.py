"""PlatSupplyDemandProvider against synthetic files in the feeder's layout."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("pandas")
pytest.importorskip("pyarrow")

from engine.market_data import Geo, PlatSupplyDemandProvider  # noqa: E402
from tests.market_data_feeders import write_metro_config, write_supply_demand_forecast  # noqa: E402

Q1_VINTAGE = {"2026Q2": 0.010, "2026Q3": 0.015, "2026Q4": 0.020, "2027Q1": 0.025}
Q2_VINTAGE = {"2026Q3": 0.012, "2026Q4": 0.018, "2027Q1": 0.022, "2027Q2": 0.026}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    write_supply_demand_forecast(tmp_path, "dallas_tx", "2026Q1", Q1_VINTAGE)
    write_supply_demand_forecast(tmp_path, "dallas_tx", "2026Q2", Q2_VINTAGE)
    write_metro_config(tmp_path, "dallas_tx", "19100")
    write_metro_config(tmp_path, "_template", "00000")
    return tmp_path


def _provider(root: Path) -> PlatSupplyDemandProvider:
    return PlatSupplyDemandProvider(root, env={})


def test_latest_vintage_by_metro_slug(root):
    obs = _provider(root).get("rent_growth_forecast", Geo(metro_slug="dallas_tx", submarket="alpha"))
    assert obs is not None
    assert obs.series == pytest.approx(Q2_VINTAGE)
    assert obs.unit == "decimal_yoy"
    assert obs.horizon == "2026Q3..2027Q2"
    assert obs.as_of == date(2026, 6, 30)
    assert obs.geo == Geo(cbsa_code="19100", metro_slug="dallas_tx")  # metro-level data
    assert obs.confidence == "medium"
    assert obs.provenance.vintage == "2026Q2"
    assert obs.provenance.path.endswith("dallas_tx_forecast_2026Q2.parquet")
    assert obs.attributes["bands"]["ci_80_lo"]["2026Q3"] == pytest.approx(-0.008)
    assert obs.attributes["model_name"] == "Ensemble"


def test_as_of_picks_the_vintage_visible_then(root):
    obs = _provider(root).get("rent_growth_forecast", Geo(metro_slug="dallas_tx"), as_of=date(2026, 3, 15))
    assert obs.provenance.vintage == "2026Q1"
    assert obs.series == pytest.approx(Q1_VINTAGE)
    assert _provider(root).get("rent_growth_forecast", Geo(metro_slug="dallas_tx"), as_of=date(2025, 1, 1)) is None


def test_cbsa_code_maps_through_metro_config(root):
    obs = _provider(root).get("rent_growth_forecast", Geo(cbsa_code="19100"))
    assert obs is not None and obs.geo.metro_slug == "dallas_tx"
    assert _provider(root).get("rent_growth_forecast", Geo(cbsa_code="00000")) is None


def test_missing_data_returns_none(root, tmp_path):
    provider = _provider(root)
    assert provider.get("vacancy_current", Geo(metro_slug="dallas_tx")) is None
    assert provider.get("concession_rate", Geo(metro_slug="dallas_tx")) is None
    assert provider.get("rent_growth_forecast", Geo(metro_slug="austin_tx")) is None
    assert provider.get("rent_growth_forecast", Geo()) is None
    missing = _provider(tmp_path / "nope")
    assert not missing.available() and "not found" in missing.unavailable_reason
    assert missing.get("rent_growth_forecast", Geo(metro_slug="dallas_tx")) is None


def test_corrupt_or_wrong_layout_returns_none(tmp_path):
    import pandas as pd

    out = tmp_path / "data" / "econometrics" / "forecasts" / "testville_zz"
    out.mkdir(parents=True)
    (out / "testville_zz_forecast_2026Q2.parquet").write_bytes(b"garbage")
    assert _provider(tmp_path).get("rent_growth_forecast", Geo(metro_slug="testville_zz")) is None
    pd.DataFrame({"period": ["2026Q3"], "other": [1.0]}).to_parquet(out / "testville_zz_forecast_2026Q3.parquet")
    assert _provider(tmp_path).get("rent_growth_forecast", Geo(metro_slug="testville_zz")) is None


def test_deterministic(root):
    geo = Geo(metro_slug="dallas_tx")
    assert _provider(root).get("rent_growth_forecast", geo).as_dict() == \
        _provider(root).get("rent_growth_forecast", geo).as_dict()
