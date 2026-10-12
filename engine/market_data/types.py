"""Value types shared by every market-data provider (ADR 0003 section 7).

``Geo`` names the geography a caller asks about (and, on an ``Observation``,
the geography the data actually describes, which may be coarser). An
``Observation`` is one provider's answer for one metric: a scalar or a keyed
series, its unit, horizon, as-of date, confidence, and provenance.

All types are frozen. Series values are normalized to a sorted tuple of
``(key, value)`` pairs so equal inputs always compare and serialize equally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from types import MappingProxyType
from typing import Any, Mapping, Union

ScalarValue = float
SeriesValue = tuple[tuple[str, float], ...]
ObservationValue = Union[ScalarValue, SeriesValue]

CONFIDENCE_LEVELS = ("high", "medium", "low")


@dataclass(frozen=True)
class Geo:
    """A geography key. Every field is optional; providers use what they need.

    - ``cbsa_code``: 5-digit CBSA / MSA code, e.g. ``"19100"``.
    - ``metro_slug``: lowercase slug, e.g. ``"dallas_tx"`` or a CoStar-style
      ``"dallas_fort_worth_tx_usa"``.
    - ``submarket``: a submarket slug or display name.
    - ``star_tier``: CoStar star slice (``"All"``, ``"3 Star"``,
      ``"4 & 5 Star"``) or its slug (``"all"``, ``"3_star"``, ``"4_5_star"``).
    """

    cbsa_code: str | None = None
    metro_slug: str | None = None
    submarket: str | None = None
    star_tier: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "cbsa_code": self.cbsa_code,
            "metro_slug": self.metro_slug,
            "submarket": self.submarket,
            "star_tier": self.star_tier,
        }


@dataclass(frozen=True)
class Provenance:
    """Where an observation came from.

    ``path`` is the primary source file (or directory, for an aggregate),
    ``files`` lists every file that contributed, ``vintage`` is the feeder's
    own vintage label (e.g. ``"2026Q2"``), and ``content_hash`` is a SHA-256
    over the contributing files' bytes so a resolver can detect drift.
    """

    provider: str
    path: str | None = None
    vintage: str | None = None
    content_hash: str | None = None
    files: tuple[str, ...] = ()
    source_label: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "path": self.path,
            "vintage": self.vintage,
            "content_hash": self.content_hash,
            "files": list(self.files),
            "source_label": self.source_label,
        }


def _freeze_value(value: Any) -> ObservationValue:
    if isinstance(value, Mapping):
        return tuple(sorted((str(k), float(v)) for k, v in value.items()))
    if isinstance(value, tuple) and all(isinstance(p, tuple) and len(p) == 2 for p in value):
        return tuple(sorted((str(k), float(v)) for k, v in value))
    if isinstance(value, bool):
        raise TypeError("Observation value must be numeric, not bool")
    return float(value)


@dataclass(frozen=True)
class Observation:
    """One provider's value for one metric at one geography.

    ``value`` is a float or a mapping. For series metrics the keys are
    canonical quarter labels (``"2026Q3"``); for ``effective_rent_by_bed``
    they are bed keys (``"studio"``, ``"1br"``, ...). Mappings are frozen
    into a sorted tuple of pairs; use :pyattr:`series` to read them back.
    """

    metric: str
    geo: Geo
    value: ObservationValue
    unit: str
    provenance: Provenance
    horizon: str | None = None
    as_of: date | None = None
    confidence: str = "medium"
    attributes: Mapping[str, Any] = field(default_factory=dict, hash=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _freeze_value(self.value))
        conf = str(self.confidence).strip().lower()
        if conf not in CONFIDENCE_LEVELS:
            raise ValueError(f"confidence must be one of {CONFIDENCE_LEVELS}, got {self.confidence!r}")
        object.__setattr__(self, "confidence", conf)
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))

    @property
    def is_series(self) -> bool:
        return isinstance(self.value, tuple)

    @property
    def series(self) -> dict[str, float]:
        """The value as a dict (empty for a scalar)."""
        return dict(self.value) if isinstance(self.value, tuple) else {}

    @property
    def scalar(self) -> float | None:
        return None if isinstance(self.value, tuple) else self.value

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "geo": self.geo.as_dict(),
            "value": self.series if self.is_series else self.value,
            "unit": self.unit,
            "horizon": self.horizon,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "confidence": self.confidence,
            "provenance": self.provenance.as_dict(),
            "attributes": dict(self.attributes),
        }
