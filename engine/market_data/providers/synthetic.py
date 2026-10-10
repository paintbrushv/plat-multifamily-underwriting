"""In-memory provider for tests and examples."""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Mapping

from engine.market_data.metrics import METRICS
from engine.market_data.types import Geo, Observation, Provenance


def _geo_matches(stored: Geo, query: Geo) -> bool:
    """Every field set on the stored geo must equal the query's field."""
    for key, value in stored.as_dict().items():
        if value is not None and value != getattr(query, key):
            return False
    return True


def _specificity(geo: Geo) -> int:
    return sum(v is not None for v in geo.as_dict().values())


class SyntheticProvider:
    """Serves a fixed table of observations.

    ``data`` maps ``(metric, Geo)`` to a value (scalar or mapping). A stored
    geo matches a query when every field it sets equals the query's field;
    the most specific match wins. ``as_of`` filters out entries whose
    ``as_of`` is later than the requested date.
    """

    def __init__(
        self,
        data: Mapping[tuple[str, Geo], Any] | None = None,
        *,
        name: str = "synthetic",
        as_of: date | None = None,
        confidence: str = "medium",
        observations: Iterable[Observation] = (),
    ) -> None:
        self.name = name
        self._observations: list[Observation] = list(observations)
        for (metric, geo), value in (data or {}).items():
            spec = METRICS.get(metric)
            self._observations.append(
                Observation(
                    metric=metric,
                    geo=geo,
                    value=value,
                    unit=spec.unit if spec else "unknown",
                    as_of=as_of,
                    confidence=confidence,
                    provenance=Provenance(provider=name, source_label="synthetic fixture"),
                )
            )
        self.unavailable_reason: str | None = None

    def available(self) -> bool:
        return True

    def get(self, metric: str, geo: Geo, *, as_of: date | None = None) -> Observation | None:
        best: Observation | None = None
        for obs in self._observations:
            if obs.metric != metric or not _geo_matches(obs.geo, geo):
                continue
            if as_of is not None and obs.as_of is not None and obs.as_of > as_of:
                continue
            if best is None or _specificity(obs.geo) > _specificity(best.geo):
                best = obs
        return best
