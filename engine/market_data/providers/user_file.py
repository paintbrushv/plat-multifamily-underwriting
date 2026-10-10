"""An analyst's own market assumptions from a CSV or JSON file.

Unavailable until configured with ``path=`` or ``PLAT_MARKET_DATA_USER_FILE``
pointing at an existing file.

JSON: ``{"observations": [...]}`` (or a bare list) of objects with ``metric``,
``value`` (number, or an object for series/by-bed metrics), and optional
``unit``, ``geo`` (``cbsa_code``/``metro_slug``/``submarket``/``star_tier``),
``as_of`` (ISO date), ``horizon``, ``confidence``.

CSV: one row per value with columns ``metric, value`` and optional ``key``,
``unit, cbsa_code, metro_slug, submarket, star_tier, as_of, horizon,
confidence``. Rows that share metric, geo and as_of and carry a ``key``
(a quarter such as ``2026Q3`` or a bed type such as ``2br``) form one
series / by-bed observation.

Rows with an unknown metric or a non-numeric value are skipped. A row's geo
matches a query when every geo field the row sets equals the query's; the
most specific matching row wins, then the latest ``as_of`` not after the
requested date, then file order.
"""

from __future__ import annotations

import csv
import json
import os
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from engine.market_data._util import finite, sha256_files
from engine.market_data.metrics import METRICS
from engine.market_data.types import CONFIDENCE_LEVELS, Geo, Observation, Provenance

USER_FILE_ENV = "PLAT_MARKET_DATA_USER_FILE"
_GEO_FIELDS = ("cbsa_code", "metro_slug", "submarket", "star_tier")


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _geo_from(raw: Mapping[str, Any]) -> Geo:
    return Geo(**{f: (str(raw[f]).strip() or None) if raw.get(f) not in (None, "") else None for f in _GEO_FIELDS})


def _geo_matches(stored: Geo, query: Geo) -> bool:
    return all(v is None or v == getattr(query, k) for k, v in stored.as_dict().items())


def _specificity(geo: Geo) -> int:
    return sum(v is not None for v in geo.as_dict().values())


class UserFileProvider:
    name = "user_file"

    def __init__(self, path: str | Path | None = None, env: Mapping[str, str] | None = None) -> None:
        env = os.environ if env is None else env
        raw = path if path is not None else env.get(USER_FILE_ENV)
        self.path: Path | None = Path(raw).expanduser() if raw else None
        self._records: list[dict[str, Any]] | None = None
        self._hash: str | None = None

    @property
    def configured(self) -> bool:
        return self.path is not None

    @property
    def unavailable_reason(self) -> str | None:
        if self.path is None:
            return f"no analyst file configured (set {USER_FILE_ENV} or pass path=)"
        if not self.path.is_file():
            return f"analyst file not found: {self.path}"
        if self.path.suffix.lower() not in (".json", ".csv"):
            return f"analyst file must be .json or .csv: {self.path}"
        return None

    def available(self) -> bool:
        return self.unavailable_reason is None

    # -- loading ---------------------------------------------------------
    def _load(self) -> list[dict[str, Any]]:
        if self._records is not None:
            return self._records
        records: list[dict[str, Any]] = []
        assert self.path is not None
        try:
            if self.path.suffix.lower() == ".json":
                records = self._load_json(self.path)
            else:
                records = self._load_csv(self.path)
            self._hash = sha256_files([self.path])
        except (OSError, ValueError, json.JSONDecodeError, csv.Error):
            records = []
        self._records = records
        return records

    @staticmethod
    def _record(raw: Mapping[str, Any], geo: Geo, value: Any) -> dict[str, Any] | None:
        metric = str(raw.get("metric") or "").strip()
        if metric not in METRICS:
            return None
        if isinstance(value, Mapping):
            parsed = {str(k): finite(v) for k, v in value.items()}
            if not parsed or any(v is None for v in parsed.values()):
                return None
            value = parsed
        else:
            value = finite(value)
            if value is None:
                return None
        confidence = str(raw.get("confidence") or "medium").strip().lower()
        return {
            "metric": metric,
            "geo": geo,
            "value": value,
            "unit": str(raw.get("unit") or METRICS[metric].unit),
            "as_of": _parse_date(raw.get("as_of")),
            "horizon": str(raw["horizon"]) if raw.get("horizon") not in (None, "") else None,
            "confidence": confidence if confidence in CONFIDENCE_LEVELS else "medium",
        }

    def _load_json(self, path: Path) -> list[dict[str, Any]]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("observations", []) if isinstance(payload, dict) else payload
        out = []
        for raw in rows if isinstance(rows, list) else []:
            if not isinstance(raw, dict):
                continue
            geo_raw = raw.get("geo") if isinstance(raw.get("geo"), dict) else raw
            rec = self._record(raw, _geo_from(geo_raw), raw.get("value"))
            if rec:
                out.append(rec)
        return out

    def _load_csv(self, path: Path) -> list[dict[str, Any]]:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        grouped: dict[tuple, dict[str, Any]] = {}
        order: list[tuple] = []
        out: list[dict[str, Any]] = []
        for raw in rows:
            geo = _geo_from(raw)
            key = (raw.get("key") or "").strip()
            if not key:
                rec = self._record(raw, geo, raw.get("value"))
                if rec:
                    out.append(rec)
                continue
            gkey = (raw.get("metric"), geo, raw.get("as_of"), raw.get("unit"), raw.get("horizon"), raw.get("confidence"))
            if gkey not in grouped:
                grouped[gkey] = {"raw": raw, "geo": geo, "value": {}}
                order.append(gkey)
            grouped[gkey]["value"][key] = raw.get("value")
        for gkey in order:
            entry = grouped[gkey]
            rec = self._record(entry["raw"], entry["geo"], entry["value"])
            if rec:
                out.append(rec)
        return out

    # -- protocol --------------------------------------------------------
    def get(self, metric: str, geo: Geo, *, as_of: date | None = None) -> Observation | None:
        if not self.available():
            return None
        best: tuple[tuple[int, date], dict[str, Any]] | None = None
        for rec in self._load():
            if rec["metric"] != metric or not _geo_matches(rec["geo"], geo):
                continue
            if as_of is not None and rec["as_of"] is not None and rec["as_of"] > as_of:
                continue
            rank = (_specificity(rec["geo"]), rec["as_of"] or date.min)
            if best is None or rank > best[0]:
                best = (rank, rec)
        if best is None:
            return None
        rec = best[1]
        return Observation(
            metric=metric,
            geo=rec["geo"],
            value=rec["value"],
            unit=rec["unit"],
            horizon=rec["horizon"],
            as_of=rec["as_of"],
            confidence=rec["confidence"],
            provenance=Provenance(
                provider=self.name,
                path=str(self.path),
                vintage=rec["as_of"].isoformat() if rec["as_of"] else None,
                content_hash=self._hash,
                files=(str(self.path),),
                source_label="analyst-supplied file",
            ),
        )
