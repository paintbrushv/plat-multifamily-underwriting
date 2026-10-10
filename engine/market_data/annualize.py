"""Turn quarterly market series into per-analysis-year values.

Conventions (also in ``engine/market_data/README.md``):

* **Analysis year k** (1-based) covers the 12 months starting at
  ``analysis_start + 12 * (k - 1)`` months, the same windows that
  ``engine.modules.analysis_years.aggregate_analysis_years`` uses.
* **Year-over-year series** (``rent_growth_forecast``; every feeder publishes
  rent growth as a trailing-four-quarter YoY rate sampled each quarter): the
  rate for analysis year k is the YoY value of the quarter that contains the
  window's **last month**. A trailing-four-quarter rate measured at the end
  of the window is the growth over that window, so no compounding is needed.
* **Quarter-over-quarter series** (e.g. CoStar ``*_growth_qoq`` columns):
  the rate for analysis year k compounds the four quarters whose last month
  falls inside the window: ``prod(1 + q) - 1``.
* **Level series** (``market_cap_rate``): :func:`annual_value` returns the
  arithmetic mean of the quarters in a calendar year.
* **Beyond the horizon**: with ``extend="hold_last"`` (default) a year past
  the last published quarter reuses the last published value (flat
  extrapolation); with ``extend="none"`` it is ``None``. Years that end
  before the first published quarter are always ``None``.

All functions are pure; identical inputs give identical outputs.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Mapping, Union

from engine.market_data._util import parse_quarter, quarter_of
from engine.market_data.types import Observation

Extend = Literal["hold_last", "none"]
SeriesLike = Union[Mapping[str, float], Observation]


def _as_series(series: SeriesLike) -> dict[tuple[int, int], float]:
    raw: Mapping[str, float]
    if isinstance(series, Observation):
        if not series.is_series:
            raise ValueError(f"{series.metric} observation is a scalar, not a quarterly series")
        raw = series.series
    else:
        raw = series
    out: dict[tuple[int, int], float] = {}
    for key, value in raw.items():
        parsed = parse_quarter(key)
        if parsed is None or value is None:
            continue
        out[parsed] = float(value)
    return dict(sorted(out.items()))


def _parse_start(analysis_start: date | str) -> tuple[int, int]:
    if isinstance(analysis_start, date):
        return analysis_start.year, analysis_start.month
    text = str(analysis_start)
    return int(text[:4]), int(text[5:7])


def _window_last_month(start: tuple[int, int], year_index: int) -> tuple[int, int]:
    """(year, month) of the last month of 1-based analysis year ``year_index``."""
    months = start[0] * 12 + (start[1] - 1) + 12 * year_index - 1
    return months // 12, months % 12 + 1


def _quarter_index(q: tuple[int, int]) -> int:
    return q[0] * 4 + q[1] - 1


def _lookup(
    series: dict[tuple[int, int], float], quarter: tuple[int, int], extend: Extend
) -> float | None:
    if quarter in series:
        return series[quarter]
    if not series:
        return None
    first, last = next(iter(series)), next(reversed(series))
    if _quarter_index(quarter) < _quarter_index(first):
        return None
    if _quarter_index(quarter) > _quarter_index(last):
        return series[last] if extend == "hold_last" else None
    return None  # interior gap: do not invent a value


def yoy_series_to_analysis_years(
    series: SeriesLike,
    analysis_start: date | str,
    years: int,
    *,
    extend: Extend = "hold_last",
) -> list[float | None]:
    """Annual rate per analysis year from a quarterly YoY series."""
    quarters = _as_series(series)
    start = _parse_start(analysis_start)
    out: list[float | None] = []
    for k in range(1, years + 1):
        y, m = _window_last_month(start, k)
        out.append(_lookup(quarters, quarter_of(date(y, m, 1)), extend))
    return out


def qoq_series_to_analysis_years(
    series: SeriesLike,
    analysis_start: date | str,
    years: int,
    *,
    extend: Extend = "hold_last",
) -> list[float | None]:
    """Annual rate per analysis year by compounding four quarterly QoQ rates."""
    quarters = _as_series(series)
    start = _parse_start(analysis_start)
    out: list[float | None] = []
    for k in range(1, years + 1):
        y, m = _window_last_month(start, k)
        end_q = _quarter_index(quarter_of(date(y, m, 1)))
        # The four quarters whose last month is inside the window: if the
        # window's last month is not a quarter-end month, its quarter ends
        # after the window, so step back one.
        if m % 3 != 0:
            end_q -= 1
        factor = 1.0
        for idx in range(end_q - 3, end_q + 1):
            rate = _lookup(quarters, (idx // 4, idx % 4 + 1), extend)
            if rate is None:
                factor = None  # type: ignore[assignment]
                break
            factor *= 1.0 + rate
        out.append(None if factor is None else factor - 1.0)
    return out


def annual_value(series: SeriesLike, year: int, *, extend: Extend = "hold_last") -> float | None:
    """Calendar-year mean of a quarterly level series (e.g. a cap rate).

    A scalar observation returns its value for every year.
    """
    if isinstance(series, Observation) and not series.is_series:
        return series.scalar
    quarters = _as_series(series)
    in_year = [v for (y, _q), v in quarters.items() if y == year]
    if in_year:
        return sum(in_year) / len(in_year)
    return _lookup(quarters, (year, 1), extend)
