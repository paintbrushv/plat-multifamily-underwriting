"""Observation value normalization and the annualization convention."""

from __future__ import annotations

from datetime import date

import pytest

from engine.market_data import (
    Geo,
    Observation,
    Provenance,
    annual_value,
    qoq_series_to_analysis_years,
    yoy_series_to_analysis_years,
)

PROV = Provenance(provider="synthetic")


def _obs(value, metric="rent_growth_forecast"):
    return Observation(metric=metric, geo=Geo(metro_slug="testville_zz"), value=value,
                       unit="decimal_yoy", provenance=PROV)


def test_observation_freezes_series_and_validates_confidence():
    a = _obs({"2026Q4": 0.02, "2026Q3": 0.01})
    b = _obs({"2026Q3": 0.01, "2026Q4": 0.02})
    assert a == b and a.is_series and a.value == (("2026Q3", 0.01), ("2026Q4", 0.02))
    assert a.series == {"2026Q3": 0.01, "2026Q4": 0.02} and a.scalar is None
    scalar = Observation(metric="vacancy_current", geo=Geo(), value=5, unit="decimal",
                         provenance=PROV, confidence="HIGH", as_of=date(2026, 6, 30))
    assert scalar.value == 5.0 and scalar.confidence == "high" and not scalar.is_series
    assert scalar.as_dict()["as_of"] == "2026-06-30"
    with pytest.raises(ValueError):
        Observation(metric="x", geo=Geo(), value=1.0, unit="u", provenance=PROV, confidence="certain")
    with pytest.raises(Exception):
        a.value = 1.0  # frozen


YOY = {"2026Q3": 0.010, "2026Q4": 0.020, "2027Q1": 0.030, "2027Q2": 0.040,
       "2027Q3": 0.050, "2027Q4": 0.060, "2028Q1": 0.070, "2028Q2": 0.080}


def test_yoy_uses_the_quarter_containing_each_years_last_month():
    # Analysis start 2026-07: year 1 ends 2027-06 (2027Q2), year 2 ends 2028-06 (2028Q2).
    assert yoy_series_to_analysis_years(YOY, "2026-07", 2) == [0.040, 0.080]
    # Start 2026-09: year 1 ends 2027-08 (2027Q3).
    assert yoy_series_to_analysis_years(YOY, date(2026, 9, 1), 1) == [0.050]


def test_yoy_beyond_horizon_holds_last_or_returns_none():
    assert yoy_series_to_analysis_years(YOY, "2026-07", 4) == [0.040, 0.080, 0.080, 0.080]
    assert yoy_series_to_analysis_years(YOY, "2026-07", 3, extend="none") == [0.040, 0.080, None]
    # A year that ends before the first published quarter has no value.
    assert yoy_series_to_analysis_years(YOY, "2025-01", 1) == [None]


def test_yoy_accepts_an_observation_and_feeder_period_formats():
    obs = _obs({"2026-Q3": 0.01, "2027 Q2": 0.04})
    assert yoy_series_to_analysis_years(obs, "2026-07", 1) == [0.04]
    with pytest.raises(ValueError):
        yoy_series_to_analysis_years(_obs(0.03), "2026-07", 1)


def test_qoq_compounds_four_quarters_inside_the_window():
    qoq = {"2026Q3": 0.01, "2026Q4": 0.01, "2027Q1": 0.01, "2027Q2": 0.01, "2027Q3": 0.02}
    [year1] = qoq_series_to_analysis_years(qoq, "2026-07", 1)
    assert year1 == pytest.approx(1.01 ** 4 - 1)
    # Window 2026-08..2027-07: quarters ending Sep, Dec, Mar, Jun.
    assert qoq_series_to_analysis_years(qoq, "2026-08", 1)[0] == pytest.approx(1.01 ** 4 - 1)
    # Year 2 (2027Q3..2028Q2) holds the last QoQ rate for missing quarters.
    assert qoq_series_to_analysis_years(qoq, "2026-07", 2)[1] == pytest.approx(1.02 ** 4 - 1)
    assert qoq_series_to_analysis_years(qoq, "2026-07", 2, extend="none")[1] is None


def test_annual_value_is_calendar_year_mean_with_flat_extension():
    caps = {"2026Q3": 0.050, "2026Q4": 0.052, "2027Q1": 0.054, "2027Q2": 0.056}
    assert annual_value(caps, 2026) == pytest.approx(0.051)
    assert annual_value(caps, 2027) == pytest.approx(0.055)
    assert annual_value(caps, 2031) == pytest.approx(0.056)
    assert annual_value(caps, 2031, extend="none") is None
    assert annual_value(caps, 2020) is None
    assert annual_value(_obs(0.06, metric="market_cap_rate"), 2031) == 0.06


def test_annualization_is_deterministic():
    runs = [yoy_series_to_analysis_years(YOY, "2026-07", 5) for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]
