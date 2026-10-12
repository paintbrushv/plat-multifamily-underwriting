"""Market-data provider layer (ADR 0003 section 7).

Providers read market metrics (rent growth, vacancy, concessions, cap rates,
rents by bed, renovation premium, supply risk) from feeder output files or
analyst files and return ``Observation`` objects with provenance. A
``MarketDataRegistry`` orders the providers: the first with data wins and all
results come back as candidates.

The underwriting engine stays deterministic: ``run_underwriting`` never
calls a provider. Providers are used only at ingest/resolve time.

The metric table (names, units, value shapes) and the annualization
convention are documented in ``engine/market_data/README.md``; the metric
table's code form is :data:`engine.market_data.metrics.METRICS`.
"""

from engine.market_data.annualize import (
    annual_value,
    qoq_series_to_analysis_years,
    yoy_series_to_analysis_years,
)
from engine.market_data.metrics import METRICS, MetricSpec
from engine.market_data.providers import (
    CoStarProvider,
    MarketDataProvider,
    PlatMarketStudyAgentProvider,
    PlatSupplyDemandProvider,
    RealPageProvider,
    SyntheticProvider,
    UserFileProvider,
    YardiMatrixProvider,
)
from engine.market_data.registry import MarketDataRegistry, default_registry
from engine.market_data.types import Geo, Observation, Provenance

__all__ = [
    "METRICS",
    "CoStarProvider",
    "Geo",
    "MarketDataProvider",
    "MarketDataRegistry",
    "MetricSpec",
    "Observation",
    "PlatMarketStudyAgentProvider",
    "PlatSupplyDemandProvider",
    "Provenance",
    "RealPageProvider",
    "SyntheticProvider",
    "UserFileProvider",
    "YardiMatrixProvider",
    "annual_value",
    "default_registry",
    "qoq_series_to_analysis_years",
    "yoy_series_to_analysis_years",
]
