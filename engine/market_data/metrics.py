"""Metric vocabulary v1 (ADR 0003 section 7).

``METRICS`` is the code form of the table in ``engine/market_data/README.md``;
``tests/test_market_data_registry.py`` checks the two agree.
"""

from __future__ import annotations

from dataclasses import dataclass

# ``supply_pipeline`` and ``supply_pipeline_norm`` measure deliveries over this
# many months (engine/underwriting_policy.HOUSE_SUPPLY_HORIZON_MONTHS), and the
# norm averages the pace over this many trailing years of actuals.
SUPPLY_PIPELINE_MONTHS = 24
SUPPLY_NORM_LOOKBACK_YEARS = 10


@dataclass(frozen=True)
class MetricSpec:
    name: str
    unit: str
    shape: str  # "scalar" | "series" | "by_bed"
    description: str


METRICS: dict[str, MetricSpec] = {
    spec.name: spec
    for spec in (
        MetricSpec(
            "rent_growth_forecast", "decimal_yoy", "series",
            "Forecast year-over-year effective rent growth, keyed by quarter (e.g. 2026Q3).",
        ),
        MetricSpec(
            "rent_growth_long_run", "decimal_yoy", "scalar",
            "Long-run (terminal) annual effective rent growth for the MSA.",
        ),
        MetricSpec(
            "vacancy_current", "decimal", "scalar",
            "Latest actual physical vacancy rate.",
        ),
        MetricSpec(
            "vacancy_stabilized", "decimal", "scalar",
            "Latest actual stabilized vacancy rate.",
        ),
        MetricSpec(
            "concession_rate", "decimal_of_asking", "scalar",
            "(asking - effective) / asking rent.",
        ),
        MetricSpec(
            "market_cap_rate", "decimal", "series",
            "Market cap rate keyed by quarter, actuals followed by forecast quarters.",
        ),
        MetricSpec(
            "effective_rent_by_bed", "usd_per_unit_month", "by_bed",
            "Effective monthly rent per unit keyed by bed type (studio, 1br, 2br, 3br).",
        ),
        MetricSpec(
            "renovation_premium", "usd_per_unit_month", "scalar",
            "Monthly rent premium of a renovated unit over a classic unit.",
        ),
        MetricSpec(
            "supply_risk", "decimal_of_inventory", "scalar",
            "Units under construction / existing inventory units.",
        ),
        MetricSpec(
            "supply_pipeline", "decimal_of_inventory", "scalar",
            "Net units expected to deliver over the next 24 months / existing inventory units.",
        ),
        MetricSpec(
            "supply_pipeline_norm", "decimal_of_inventory", "scalar",
            "Normal 24-month net deliveries / inventory: the trailing long-run (10-year) pace.",
        ),
    )
}


def require_metric(metric: str) -> MetricSpec:
    try:
        return METRICS[metric]
    except KeyError:
        raise ValueError(f"unknown market-data metric {metric!r}; known: {sorted(METRICS)}") from None
