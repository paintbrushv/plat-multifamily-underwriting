"""The provider protocol (ADR 0003 section 7).

A provider answers ``get(metric, geo, as_of=...)`` with an ``Observation`` or
``None``. Providers never raise for missing roots, files, columns, geographies
or metrics; they return ``None``. ``available()`` says whether the provider can
answer anything at all, and ``unavailable_reason`` says why not.

Providers are used only by a resolver at ingest/resolve time. The
underwriting engine never calls them during ``run_underwriting``.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from engine.market_data.types import Geo, Observation


@runtime_checkable
class MarketDataProvider(Protocol):
    name: str

    def available(self) -> bool: ...

    def get(self, metric: str, geo: Geo, *, as_of: date | None = None) -> Observation | None: ...
