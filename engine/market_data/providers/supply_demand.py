"""Reads plat-supply-demand forecast OUTPUT FILES. Never imports that package.

Root: ``PLAT_SUPPLY_DEMAND_ROOT`` (default: sibling ``plat-supply-demand``).

Layout read (verified against the feeder source): ``econ forecast`` writes
``data/econometrics/forecasts/<metro_id>/<metro_id>_forecast_<vintage>.parquet``
(``econometrics/cli/forecast.py``) from ``ForecastResult.to_dataframe()``
(``econometrics/models/base.py``) with columns ``period``,
``rent_growth_yoy_point``, ``rent_growth_yoy_ci80_lo``,
``rent_growth_yoy_ci80_hi``, ``rent_growth_yoy_ci95_lo``,
``rent_growth_yoy_ci95_hi``, ``model_name``, ``vintage_date``. ``period`` and
``vintage`` are pandas quarter labels such as ``2026Q2``; values are YoY
decimals. ``metro_id`` is a slug such as ``dallas_tx``; the CBSA code is the
``msa_code`` in ``econometrics/config/metros/<metro_id>.yaml``.

Metrics served: ``rent_growth_forecast`` only. The feeder's concession spread
(``econometrics/data/loaders/concessions.py``) is computed in memory and not
persisted, so it is read from the market-study CoStar panel instead.

Geography: metro-level only. ``Geo.metro_slug`` is used when a forecast
directory of that name exists; otherwise ``Geo.cbsa_code`` is mapped through
the metro configs. Submarket and star tier are ignored and the returned
``Observation.geo`` is the metro. As-of: the latest vintage whose quarter
starts on or before ``as_of``. The feeder publishes no confidence, so
confidence is ``"medium"``.
"""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from engine.market_data._util import (
    canonical_quarter,
    finite,
    parquet_support,
    parse_quarter,
    quarter_end,
    quarter_start,
    sha256_files,
    slugify,
)
from engine.market_data.metrics import METRICS
from engine.market_data.paths import supply_demand_root
from engine.market_data.types import Geo, Observation, Provenance

SOURCE_LABEL = "plat-supply-demand econometric ensemble forecast"
_MSA_RE = re.compile(r"""^msa_code:\s*["']?(\d{5})["']?\s*(?:#.*)?$""", re.MULTILINE)
_METRO_ID_RE = re.compile(r"""^metro_id:\s*["']?([A-Za-z0-9_]+)["']?\s*(?:#.*)?$""", re.MULTILINE)


class PlatSupplyDemandProvider:
    name = "plat_supply_demand"

    def __init__(self, root: str | Path | None = None, *, env: Mapping[str, str] | None = None) -> None:
        env = os.environ if env is None else env
        self.root = Path(root).expanduser() if root is not None else supply_demand_root(env)

    @property
    def forecasts_dir(self) -> Path:
        return self.root / "data" / "econometrics" / "forecasts"

    @property
    def unavailable_reason(self) -> str | None:
        if not self.root.is_dir():
            return f"plat-supply-demand root not found: {self.root} (set PLAT_SUPPLY_DEMAND_ROOT)"
        ok, reason = parquet_support()
        return None if ok else reason

    def available(self) -> bool:
        return self.unavailable_reason is None

    def get(self, metric: str, geo: Geo, *, as_of: date | None = None) -> Observation | None:
        if metric != "rent_growth_forecast" or not self.available():
            return None
        try:
            return self._forecast(geo, as_of)
        except Exception:  # noqa: BLE001 - a provider never raises for bad data
            return None

    # -- geography -------------------------------------------------------
    def _cbsa_to_metro(self) -> dict[str, str]:
        config_dir = self.root / "econometrics" / "config" / "metros"
        out: dict[str, str] = {}
        if not config_dir.is_dir():
            return out
        for path in sorted(config_dir.glob("*.yaml")):
            if path.stem.startswith("_"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            msa = _MSA_RE.search(text)
            metro = _METRO_ID_RE.search(text)
            if msa:
                out.setdefault(msa.group(1), metro.group(1) if metro else path.stem)
        return out

    def _metro_id(self, geo: Geo) -> str | None:
        if geo.metro_slug:
            slug = slugify(geo.metro_slug)
            if (self.forecasts_dir / slug).is_dir():
                return slug
        if geo.cbsa_code:
            return self._cbsa_to_metro().get(str(geo.cbsa_code).strip())
        return None

    # -- forecast --------------------------------------------------------
    def _forecast(self, geo: Geo, as_of: date | None) -> Observation | None:
        metro_id = self._metro_id(geo)
        if metro_id is None:
            return None
        directory = self.forecasts_dir / metro_id
        prefix = f"{metro_id}_forecast_"
        candidates: list[tuple[tuple[int, int], Path]] = []
        for path in sorted(directory.glob(f"{prefix}*.parquet")):
            vintage = parse_quarter(path.stem[len(prefix):])
            if vintage is None:
                continue
            if as_of is not None and quarter_start(*vintage) > as_of:
                continue
            candidates.append((vintage, path))
        if not candidates:
            return None
        vintage, path = max(candidates)

        import pandas as pd  # optional "market" extra

        frame = pd.read_parquet(path)
        if "period" not in frame.columns or "rent_growth_yoy_point" not in frame.columns:
            return None
        series: dict[str, float] = {}
        bands: dict[str, dict[str, float]] = {
            "ci_80_lo": {}, "ci_80_hi": {}, "ci_95_lo": {}, "ci_95_hi": {},
        }
        columns = {
            "ci_80_lo": "rent_growth_yoy_ci80_lo", "ci_80_hi": "rent_growth_yoy_ci80_hi",
            "ci_95_lo": "rent_growth_yoy_ci95_lo", "ci_95_hi": "rent_growth_yoy_ci95_hi",
        }
        records: list[dict[str, Any]] = frame.to_dict("records")
        for rec in records:
            quarter = canonical_quarter(rec.get("period"))
            point = finite(rec.get("rent_growth_yoy_point"))
            if quarter is None or point is None:
                continue
            series[quarter] = point
            for band, col in columns.items():
                val = finite(rec.get(col))
                if val is not None:
                    bands[band][quarter] = val
        if not series:
            return None
        models = sorted({str(r.get("model_name")) for r in records if r.get("model_name") is not None})
        keys = sorted(series, key=parse_quarter)
        cbsa = geo.cbsa_code
        if cbsa is None:
            cbsa = next((c for c, m in self._cbsa_to_metro().items() if m == metro_id), None)
        return Observation(
            metric="rent_growth_forecast",
            geo=Geo(cbsa_code=cbsa, metro_slug=metro_id),
            value=series,
            unit=METRICS["rent_growth_forecast"].unit,
            horizon=f"{keys[0]}..{keys[-1]}",
            as_of=quarter_end(*vintage),
            confidence="medium",
            attributes={"scenario": "base", "bands": bands, "model_name": models[0] if len(models) == 1 else models},
            provenance=Provenance(
                provider=self.name,
                path=str(path),
                vintage=f"{vintage[0]}Q{vintage[1]}",
                content_hash=sha256_files([path]),
                files=(str(path),),
                source_label=SOURCE_LABEL,
            ),
        )
