"""Reads plat-market-study-agent OUTPUT FILES. Never imports that package.

Root: ``PLAT_MARKET_STUDY_ROOT`` (default: sibling ``plat-market-study-agent``).

Layouts read (verified against the feeder source):

1. CoStar submarket parquet, ``data/paid/costar-submarket*/<slug>.parquet``,
   written by ``etl/transform_costar_excel.py``. One file per submarket;
   ``<slug>`` is ``<costar_metro_slug>_<submarket_slug>`` (e.g.
   ``dallas_fort_worth_tx_usa_irving``). One row per (``star_rating``,
   ``date``); ``date`` is ``"2026 Q2"`` with optional ``EST``/``QTD``
   suffix; ``star_rating`` is ``"1 & 2 Star" | "3 Star" | "4 & 5 Star" |
   "All"``; ``is_forecast`` marks forecast quarters. Rates are decimals.
   Columns used: ``vacancy_rate``, ``stabilized_vacancy``,
   ``asking_rent_unit``, ``effective_rent_unit``,
   ``effective_rent_{studio,1br,2br,3br}``, ``effective_rent_growth_yoy``,
   ``market_cap_rate``, ``under_construction``, ``inventory_units``,
   ``deliveries_12mo``, ``absorption_units_12mo``.
2. Submarket ensemble forecast, ``reports/**/forecasts/`` written by
   ``python -m etl.submarket_forecast``: ``v2_ensemble_<tier_slug>.csv``
   (``period, base, bull, bear, ci_80_lo, ci_80_hi, ci_95_lo, ci_95_hi``;
   ``period`` like ``2026Q3``; values are YoY decimals) and
   ``model_diagnostics_<tier_slug>.json`` (``meta.metro_slug``,
   ``meta.tier``, ``meta.panel_end``, ``ensemble.confidence_level``
   High/Medium/Low).
3. Comp-finder ``comps.json`` (``subject.metro_slug``, ``as_of``,
   ``comps[].unit_types[]`` with ``unit_type``, ``face_rent``,
   ``effective_rent``, ``evidence_quality``) written by
   ``agents/runners/emit_comp_finder_envelope.py`` under a *deal* root
   (``<deal_root>/outputs/<run_id>/comps/comps.json``), so its paths are
   configured explicitly (``comps_paths=`` or ``PLAT_MARKET_STUDY_COMPS``,
   ``os.pathsep``-separated).

Geography: a ``Geo.metro_slug`` maps to CoStar file prefixes ``<slug>``,
``<slug>_usa`` and any alias (see ``DEFAULT_METRO_ALIASES``). Without a metro
slug, ``Geo.cbsa_code`` is mapped to one through the feeder's
``agents/configs/*.yaml`` (``notes.msa_code`` / ``notes.metro_slug``), then
``DEFAULT_CBSA_METROS``. A matching ``submarket`` selects one file; submarket
names compare on letters and digits only, so ``"North Lake/Flats"``
matches both ``<metro>_north_lake_flats`` (``transform_costar_excel.py``)
and the older ``<metro>_north_lakeflats`` naming. Otherwise every file
of the metro is combined (rates and rents weighted by ``inventory_units``;
unit counts summed), the returned ``Observation.geo`` has no submarket and
``attributes["submarket_requested"]`` names the submarket that was not found.
A missing star tier falls back to ``All`` and the returned geo says so.

Availability: the root must exist, the ``market`` extra must be installed and
the root must hold at least one feeder output (a CoStar parquet, an ensemble
forecast, or a configured comps.json); otherwise ``unavailable_reason`` says
which. ``diagnose(geo)`` reports the inventory and the geography match for the
market-data status block and ``runs/market_data_doctor.py``.

As-of: a quarter's value counts as known from the first day of that quarter
(the feeder publishes QTD/EST rows for the current quarter). Scalar metrics
use the latest actual quarter visible at ``as_of``; series metrics are
withheld when their vintage quarter starts after ``as_of``.
"""

from __future__ import annotations

import csv
import json
import os
import re
import statistics
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from engine.market_data._util import (
    canonical_quarter,
    finite,
    parquet_support,
    parse_quarter,
    quarter_end,
    quarter_start,
    sha256_files,
    slugify,
    tier_label,
    tier_slug,
)
from engine.market_data.paths import feeder_root
from engine.market_data.types import CONFIDENCE_LEVELS, Geo, Observation, Provenance

COMPS_ENV = "PLAT_MARKET_STUDY_COMPS"
COSTAR_LABEL = "CoStar submarket export via plat-market-study-agent"
ENSEMBLE_LABEL = "plat-market-study-agent submarket ensemble forecast"
COMPS_LABEL = "plat-market-study-agent comp-finder comps.json"

# Generic metro slugs -> CoStar file prefixes that do not follow
# ``<slug>`` / ``<slug>_usa``. Public metro names only.
DEFAULT_METRO_ALIASES: dict[str, tuple[str, ...]] = {
    "dallas_tx": ("dallas_fort_worth_tx_usa", "dallasfort_worth_tx_usa"),
    "dfw": ("dallas_fort_worth_tx_usa", "dallasfort_worth_tx_usa"),
    "dallas_fort_worth": ("dallas_fort_worth_tx_usa", "dallasfort_worth_tx_usa"),
}

_WEIGHTED = (
    "vacancy_rate", "stabilized_vacancy", "asking_rent_unit", "effective_rent_unit",
    "effective_rent_studio", "effective_rent_1br", "effective_rent_2br", "effective_rent_3br",
    "effective_rent_growth_yoy", "market_cap_rate",
)
_SUMMED = ("inventory_units", "under_construction", "deliveries_12mo", "absorption_units_12mo")
_BEDS = (("studio", "effective_rent_studio"), ("1br", "effective_rent_1br"),
         ("2br", "effective_rent_2br"), ("3br", "effective_rent_3br"))
_EVIDENCE_CONFIDENCE = {
    "direct_playwright": "high",
    "direct_cached": "medium",
    "official_indexed": "medium",
    "om_floorplan": "low",
    "tracking_csv_salvage": "low",
}
_RANK = {"low": 0, "medium": 1, "high": 2}

# Public CBSA codes -> generic metro slugs, used when a geography has a CBSA
# code but no metro slug. The feeder's agents/configs/*.yaml come first.
DEFAULT_CBSA_METROS: dict[str, str] = {"19100": "dallas_tx"}

# The metrics this provider can serve (renovation_premium and
# rent_growth_long_run have no feeder output).
SERVES = (
    "rent_growth_forecast", "effective_rent_by_bed", "vacancy_current", "vacancy_stabilized",
    "concession_rate", "supply_risk", "market_cap_rate",
)
COSTAR_GLOB = "data/paid/costar-submarket*/*.parquet"
ENSEMBLE_GLOB = "reports/**/forecasts/model_diagnostics_<tier>.json + v2_ensemble_<tier>.csv"
_CONFIG_MSA_RE = re.compile(r"""^\s*msa_code:\s*["']?(\d{5})["']?\s*(?:#.*)?$""", re.MULTILINE)
_CONFIG_SLUG_RE = re.compile(r"""^\s*metro_slug:\s*["']?([A-Za-z0-9_]+)["']?\s*(?:#.*)?$""", re.MULTILINE)


def compact(value: object) -> str:
    """Letters and digits only: ``"North Lake/Flats"`` -> ``northlakeflats``."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def bed_key(unit_type: object) -> str | None:
    """``"Studio"``/``"0BR"`` -> ``studio``; ``"2BR"``/``"2 Bed"``/``"2x2"`` -> ``2br``."""
    text = str(unit_type or "").strip().lower()
    if not text:
        return None
    if text.startswith(("studio", "efficiency")) or re.match(r"^0\s*(br|bd|bed|x)", text):
        return "studio"
    match = re.match(r"^(\d)\s*(?:br|bd|bed|x\s*\d|/)", text)
    if match:
        return f"{match.group(1)}br"
    return None


class PlatMarketStudyAgentProvider:
    name = "plat_market_study_agent"

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        comps_paths: Sequence[str | Path] | None = None,
        metro_aliases: Mapping[str, Iterable[str]] | None = None,
        env: Mapping[str, str] | None = None,
        root_origin: str | None = None,
    ) -> None:
        env = os.environ if env is None else env
        if root is not None:
            self.root = Path(root).expanduser()
            self.root_origin = root_origin or "explicit root"
        else:
            found = feeder_root("market_study", env)
            self.root, self.root_origin = found.path, found.origin
        # metric -> "ExcType: message" of the last exception get() swallowed.
        self.errors: dict[str, str] = {}
        if comps_paths is None:
            raw = env.get(COMPS_ENV) or ""
            comps_paths = [p for p in raw.split(os.pathsep) if p]
        self.comps_paths = [Path(p).expanduser() for p in comps_paths]
        aliases = {k: tuple(v) for k, v in DEFAULT_METRO_ALIASES.items()}
        for key, values in (metro_aliases or {}).items():
            aliases[key] = tuple(values) + aliases.get(key, ())
        self.metro_aliases = aliases

    # -- availability ----------------------------------------------------
    @property
    def unavailable_reason(self) -> str | None:
        if not self.root.is_dir():
            return (
                f"plat-market-study-agent root not found: {self.root} ({self.root_origin}); "
                "set PLAT_MARKET_STUDY_ROOT or market_study_root in the user config"
            )
        ok, reason = parquet_support()
        if not ok:
            return reason
        if not (self._costar_files() or self._diagnostics_files() or self._existing_comps()):
            return (
                f"plat-market-study-agent root {self.root} holds no feeder outputs: no CoStar parquet "
                f"({COSTAR_GLOB}), no ensemble forecast ({ENSEMBLE_GLOB}) and no comps.json "
                f"({COMPS_ENV}); generate them in the feeder repository"
            )
        return None

    def available(self) -> bool:
        return self.unavailable_reason is None

    def get(self, metric: str, geo: Geo, *, as_of: date | None = None) -> Observation | None:
        if not self.available():
            return None
        try:
            return self._get(metric, geo, as_of)
        except Exception as exc:  # noqa: BLE001 - a provider never raises for bad data
            self.errors[metric] = f"{type(exc).__name__}: {exc}"
            return None

    def _get(self, metric: str, geo: Geo, as_of: date | None) -> Observation | None:
        if metric == "rent_growth_forecast":
            return self._ensemble(geo, as_of) or self._costar(metric, geo, as_of)
        if metric == "effective_rent_by_bed":
            return self._comps(geo, as_of) or self._costar(metric, geo, as_of)
        if metric in ("vacancy_current", "vacancy_stabilized", "concession_rate",
                      "supply_risk", "market_cap_rate"):
            return self._costar(metric, geo, as_of)
        return None  # renovation_premium and unknown metrics: no feeder output

    # -- diagnostics -----------------------------------------------------
    def diagnose(self, geo: Geo) -> dict[str, Any]:
        """What this root holds and how ``geo`` matches it (reads no parquet).

        Keys: ``root``, ``root_origin``, ``root_found``, ``serves``,
        ``inventory``, ``geography``, ``matched`` (any output for the metro),
        ``errors`` (exceptions ``get`` swallowed, by metric) and ``summary``
        (one sentence on what is missing, None when nothing is).
        """
        out: dict[str, Any] = {
            "root": str(self.root),
            "root_origin": self.root_origin,
            "root_found": self.root.is_dir(),
            "serves": list(SERVES),
            "errors": dict(self.errors),
            "matched": False,
        }
        if not out["root_found"]:
            out["summary"] = f"root not found: {self.root} ({self.root_origin})"
            return out
        files = self._costar_files()
        diagnostics = self._diagnostics_files()
        comps = self._existing_comps()
        metro, via = self._metro_slug(geo)
        prefixes = self._metro_slugs(geo)
        metro_stems: list[str] = []
        prefix_used: str | None = None
        for prefix in sorted(prefixes, key=len, reverse=True):
            metro_stems = self._metro_stems(files, prefix)
            if metro_stems:
                prefix_used = prefix
                break
        _, stem = self._select_files(geo, files)
        ensemble_metros: dict[str, list[str]] = {}
        for path in diagnostics:
            try:
                meta = (json.loads(path.read_text(encoding="utf-8")).get("meta") or {})
            except (OSError, ValueError, AttributeError):
                continue
            ensemble_metros.setdefault(slugify(meta.get("metro_slug")), []).append(str(path))
        ensemble_match = sorted(p for slug in prefixes for p in ensemble_metros.get(slug, []))
        comps_match = []
        for path in comps:
            try:
                subject = (json.loads(path.read_text(encoding="utf-8")).get("subject") or {})
            except (OSError, ValueError, AttributeError):
                continue
            if slugify(subject.get("metro_slug")) in prefixes:
                comps_match.append(str(path))
        out["inventory"] = {
            "costar_parquet_files": len(files),
            "costar_dirs": sorted({str(p.parent) for p in files.values()}),
            "ensemble_forecasts": len(diagnostics),
            "ensemble_metros": sorted(m for m in ensemble_metros if m),
            "comps_configured": [str(p) for p in self.comps_paths],
            "comps_found": [str(p) for p in comps],
        }
        out["geography"] = {
            "metro_slug": metro,
            "metro_via": via,
            "file_prefixes": prefixes,
            "costar_prefix_matched": prefix_used,
            "costar_files_for_metro": len(metro_stems),
            "submarket_requested": geo.submarket,
            "submarket_file": stem,
            "submarkets_available": [s[len(prefix_used) + 1:] for s in metro_stems] if prefix_used else [],
            "ensemble_files": ensemble_match,
            "comps_files": comps_match,
        }
        out["matched"] = bool(metro_stems or ensemble_match or comps_match)

        ok, reason = parquet_support()
        problems: list[str] = []
        if not ok:
            problems.append(str(reason))
        if not (files or diagnostics or comps):
            problems.append(
                f"the root holds no feeder outputs: no CoStar parquet ({COSTAR_GLOB}), no ensemble "
                f"forecast ({ENSEMBLE_GLOB}), no comps.json ({COMPS_ENV})"
            )
        elif metro is None:
            problems.append(
                "the geography has no metro slug"
                + (f" and CBSA {geo.cbsa_code} is not mapped to one" if geo.cbsa_code else "")
            )
        else:
            if not metro_stems:
                problems.append(
                    f"no CoStar parquet for metro {metro} (file prefixes {', '.join(prefixes)}; "
                    f"{len(files)} parquet file(s) under {COSTAR_GLOB})"
                )
            elif geo.submarket and stem is None:
                problems.append(
                    f"submarket {geo.submarket!r} not found among the {len(metro_stems)} CoStar "
                    f"submarket file(s) for {prefix_used}; the metro aggregate is used"
                )
            if not ensemble_match:
                problems.append(f"no ensemble forecast for metro {metro} under reports/**/forecasts/")
        out["summary"] = "; ".join(problems) or None
        return out

    # -- geography -------------------------------------------------------
    def _cbsa_metros(self) -> dict[str, str]:
        """CBSA code -> metro slug: the feeder's agents/configs/*.yaml, then the defaults."""
        out: dict[str, str] = {}
        config_dir = self.root / "agents" / "configs"
        if config_dir.is_dir():
            for path in sorted(config_dir.glob("*.yaml")):
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                msa, slug = _CONFIG_MSA_RE.search(text), _CONFIG_SLUG_RE.search(text)
                if msa and slug:
                    out.setdefault(msa.group(1), slug.group(1))
        for code, slug in DEFAULT_CBSA_METROS.items():
            out.setdefault(code, slug)
        return out

    def _metro_slug(self, geo: Geo) -> tuple[str | None, str | None]:
        """(metro slug, how it was found: ``metro_slug`` or ``cbsa_code``)."""
        if geo.metro_slug:
            return slugify(geo.metro_slug), "metro_slug"
        if geo.cbsa_code:
            slug = self._cbsa_metros().get(str(geo.cbsa_code).strip())
            if slug:
                return slugify(slug), "cbsa_code"
        return None, None

    def _metro_slugs(self, geo: Geo) -> list[str]:
        slug, _ = self._metro_slug(geo)
        if not slug:
            return []
        out = [slug, f"{slug}_usa", *self.metro_aliases.get(slug, ())]
        return list(dict.fromkeys(out))

    def _diagnostics_files(self) -> list[Path]:
        reports = self.root / "reports"
        if not reports.is_dir():
            return []
        return sorted(p for p in reports.rglob("model_diagnostics_*.json") if p.parent.name == "forecasts")

    def _existing_comps(self) -> list[Path]:
        return [p for p in self.comps_paths if p.is_file()]

    def _costar_files(self) -> dict[str, Path]:
        paid = self.root / "data" / "paid"
        if not paid.is_dir():
            return {}
        dirs = sorted(d for d in paid.glob("costar-submarket*") if d.is_dir())
        dirs.sort(key=lambda d: (d.name != "costar-submarket", d.name))
        files: dict[str, Path] = {}
        for directory in dirs:
            for path in sorted(directory.glob("*.parquet")):
                files.setdefault(path.stem, path)
        return files

    @staticmethod
    def _metro_stems(files: Mapping[str, Path], prefix: str) -> list[str]:
        return [s for s in sorted(files) if s.startswith(prefix + "_")]

    def _select_files(
        self, geo: Geo, files: Mapping[str, Path] | None = None
    ) -> tuple[list[Path], str | None]:
        """(files, submarket stem or None for a metro aggregate)."""
        files = self._costar_files() if files is None else files
        prefixes = self._metro_slugs(geo)
        wanted = compact(geo.submarket)
        if wanted:
            if prefixes:
                for prefix in sorted(prefixes, key=len, reverse=True):
                    for stem in self._metro_stems(files, prefix):
                        if compact(stem[len(prefix) + 1:]) == wanted:
                            return [files[stem]], stem
            else:
                for stem in sorted(files):
                    if compact(stem) == wanted:
                        return [files[stem]], stem
        for prefix in sorted(prefixes, key=len, reverse=True):
            matched = [files[s] for s in self._metro_stems(files, prefix)]
            if matched:
                return matched, None
        return [], None

    # -- CoStar parquet --------------------------------------------------
    @staticmethod
    def _read_rows(path: Path, tier: str) -> list[dict[str, Any]]:
        import pandas as pd  # optional "market" extra

        frame = pd.read_parquet(path)
        if "star_rating" not in frame.columns or "date" not in frame.columns:
            return []
        frame = frame[frame["star_rating"] == tier]
        rows: dict[str, dict[str, Any]] = {}
        for rec in sorted(frame.to_dict("records"), key=lambda r: str(r.get("date"))):
            quarter = canonical_quarter(rec.get("date"))
            if quarter is None:
                continue
            rec["_quarter"] = quarter
            rows[quarter] = rec  # EST then QTD: keep last, as the feeder does
        return [rows[q] for q in sorted(rows, key=parse_quarter)]

    def _costar_panel(
        self, geo: Geo
    ) -> tuple[list[dict[str, Any]], list[Path], str | None, str] | None:
        files, stem = self._select_files(geo)
        if not files:
            return None
        requested = tier_label(geo.star_tier)
        for tier in dict.fromkeys([requested, "All"]):
            per_file = [self._read_rows(p, tier) for p in files]
            if any(per_file):
                return _combine(per_file), files, stem, tier
        return None

    def _costar(self, metric: str, geo: Geo, as_of: date | None) -> Observation | None:
        loaded = self._costar_panel(geo)
        if loaded is None:
            return None
        panel, files, stem, tier = loaded
        actual = [r for r in panel if not r["is_forecast"]]
        if not actual:
            return None
        vintage = actual[-1]["_quarter"]
        vintage_q = parse_quarter(vintage)
        assert vintage_q is not None
        visible = [r for r in actual if as_of is None or quarter_start(*parse_quarter(r["_quarter"])) <= as_of]

        value: Any = None
        horizon = "current"
        attributes: dict[str, Any] = {"star_tier": tier}
        if geo.submarket and stem is None:
            attributes["submarket_requested"] = geo.submarket
        row_q: str | None = None

        def latest(fn) -> tuple[Any, str | None]:
            for row in reversed(visible):
                got = fn(row)
                if got is not None:
                    return got, row["_quarter"]
            return None, None

        if metric == "vacancy_current":
            value, row_q = latest(lambda r: r.get("vacancy_rate"))
        elif metric == "vacancy_stabilized":
            value, row_q = latest(lambda r: r.get("stabilized_vacancy"))
            horizon = "stabilized"
        elif metric == "concession_rate":
            # Same formula as plat-supply-demand concessions.py.
            def spread(r):
                ask, eff = r.get("asking_rent_unit"), r.get("effective_rent_unit")
                return (ask - eff) / ask if ask and eff is not None and ask > 0 else None
            value, row_q = latest(spread)
        elif metric == "supply_risk":
            def pipeline(r):
                inv, uc = r.get("inventory_units"), r.get("under_construction")
                return uc / inv if inv and uc is not None and inv > 0 else None
            value, row_q = latest(pipeline)
            if row_q is not None:
                row = next(r for r in visible if r["_quarter"] == row_q)
                inv = row["inventory_units"]
                attributes.update({
                    "under_construction_units": row.get("under_construction"),
                    "inventory_units": inv,
                    "deliveries_12mo_pct_inventory": _ratio(row.get("deliveries_12mo"), inv),
                    "absorption_12mo_pct_inventory": _ratio(row.get("absorption_units_12mo"), inv),
                })
        elif metric == "effective_rent_by_bed":
            def by_bed(r):
                beds = {k: r.get(col) for k, col in _BEDS if r.get(col) is not None}
                return beds or None
            value, row_q = latest(by_bed)
        elif metric in ("market_cap_rate", "rent_growth_forecast"):
            if as_of is not None and quarter_start(*vintage_q) > as_of:
                return None
            if metric == "market_cap_rate":
                series = {r["_quarter"]: r["market_cap_rate"] for r in panel if r.get("market_cap_rate") is not None}
                last_actual = next((r for r in reversed(actual) if r.get("market_cap_rate") is not None), None)
                if last_actual is not None:
                    attributes["latest_actual_quarter"] = last_actual["_quarter"]
                    attributes["latest_actual"] = last_actual["market_cap_rate"]
            else:
                series = {
                    r["_quarter"]: r["effective_rent_growth_yoy"]
                    for r in panel
                    if r["is_forecast"] and r.get("effective_rent_growth_yoy") is not None
                }
            if not series:
                return None
            keys = sorted(series, key=parse_quarter)
            value, row_q, horizon = series, vintage, f"{keys[0]}..{keys[-1]}"
        if value is None or row_q is None:
            return None

        parsed_q = parse_quarter(row_q)
        assert parsed_q is not None
        attributes["quarter"] = row_q
        return Observation(
            metric=metric,
            geo=Geo(cbsa_code=geo.cbsa_code, metro_slug=geo.metro_slug, submarket=stem, star_tier=tier),
            value=value,
            unit=_unit(metric),
            horizon=horizon,
            as_of=quarter_end(*parsed_q),
            confidence="medium",
            attributes=attributes,
            provenance=Provenance(
                provider=self.name,
                path=str(files[0] if len(files) == 1 else files[0].parent),
                vintage=vintage,
                content_hash=sha256_files(files),
                files=tuple(str(p) for p in files),
                source_label=COSTAR_LABEL,
            ),
        )

    # -- ensemble forecast ----------------------------------------------
    def _ensemble(self, geo: Geo, as_of: date | None) -> Observation | None:
        slugs = set(self._metro_slugs(geo))
        reports = self.root / "reports"
        if not slugs or not reports.is_dir():
            return None
        requested = tier_label(geo.star_tier)
        for tier in dict.fromkeys([requested, "All"]):
            tslug = tier_slug(tier)
            best: tuple[tuple[int, int], str, Path, dict[str, Any]] | None = None
            for diag_path in sorted(reports.rglob(f"model_diagnostics_{tslug}.json")):
                if diag_path.parent.name != "forecasts":
                    continue
                try:
                    diag = json.loads(diag_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                meta = diag.get("meta") or {}
                if slugify(meta.get("metro_slug")) not in slugs:
                    continue
                panel_end = parse_quarter(meta.get("panel_end"))
                if panel_end is None:
                    continue
                if as_of is not None and quarter_start(*panel_end) > as_of:
                    continue
                if not (diag_path.parent / f"v2_ensemble_{tslug}.csv").is_file():
                    continue
                key = (panel_end, str(diag_path))
                if best is None or key > (best[0], best[1]):
                    best = (panel_end, str(diag_path), diag_path, diag)
            if best is not None:
                return self._ensemble_observation(geo, tier, best[2], best[3])
        return None

    def _ensemble_observation(
        self, geo: Geo, tier: str, diag_path: Path, diag: dict[str, Any]
    ) -> Observation | None:
        csv_path = diag_path.parent / f"v2_ensemble_{tier_slug(tier)}.csv"
        with csv_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        series: dict[str, float] = {}
        bands: dict[str, dict[str, float]] = {k: {} for k in
                                              ("bull", "bear", "ci_80_lo", "ci_80_hi", "ci_95_lo", "ci_95_hi")}
        for row in rows:
            quarter = canonical_quarter(row.get("period"))
            base = finite(row.get("base"))
            if quarter is None or base is None:
                continue
            series[quarter] = base
            for band in bands:
                val = finite(row.get(band))
                if val is not None:
                    bands[band][quarter] = val
        if not series:
            return None
        meta = diag.get("meta") or {}
        ensemble = diag.get("ensemble") or {}
        confidence = str(ensemble.get("confidence_level") or "medium").strip().lower()
        if confidence not in CONFIDENCE_LEVELS:
            confidence = "medium"
        panel_end = parse_quarter(meta.get("panel_end"))
        assert panel_end is not None
        keys = sorted(series, key=parse_quarter)
        return Observation(
            metric="rent_growth_forecast",
            geo=Geo(cbsa_code=geo.cbsa_code, metro_slug=str(meta.get("metro_slug")), star_tier=tier),
            value=series,
            unit=_unit("rent_growth_forecast"),
            horizon=f"{keys[0]}..{keys[-1]}",
            as_of=quarter_end(*panel_end),
            confidence=confidence,
            attributes={
                "scenario": "base",
                "bands": bands,
                "ensemble_rmse": finite(ensemble.get("ensemble_rmse")),
                "regime_change_flag": bool(ensemble.get("regime_change_flag", False)),
                "panel_end": meta.get("panel_end"),
            },
            provenance=Provenance(
                provider=self.name,
                path=str(csv_path),
                vintage=canonical_quarter(meta.get("panel_end")),
                content_hash=sha256_files([csv_path, diag_path]),
                files=(str(csv_path), str(diag_path)),
                source_label=ENSEMBLE_LABEL,
            ),
        )

    # -- comps.json ------------------------------------------------------
    def _comps(self, geo: Geo, as_of: date | None) -> Observation | None:
        slugs = set(self._metro_slugs(geo))
        if not slugs or not self.comps_paths:
            return None
        best: tuple[date, int, Path, dict[str, Any]] | None = None
        for index, path in enumerate(self.comps_paths):
            if not path.is_file():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            subject = payload.get("subject") or {}
            if slugify(subject.get("metro_slug")) not in slugs:
                continue
            try:
                observed = date.fromisoformat(str(payload.get("as_of") or "")[:10])
            except ValueError:
                continue
            if as_of is not None and observed > as_of:
                continue
            key = (observed, -index)
            if best is None or key > (best[0], best[1]):
                best = (observed, -index, path, payload)
        if best is None:
            return None
        observed, _, path, payload = best
        rents: dict[str, list[float]] = {}
        evidence: list[str] = []
        comp_ids: set[str] = set()
        for comp in payload.get("comps") or []:
            for unit in (comp or {}).get("unit_types") or []:
                key = bed_key((unit or {}).get("unit_type"))
                rent = finite(unit.get("effective_rent"))
                if key is None or rent is None or rent <= 0:
                    continue
                rents.setdefault(key, []).append(rent)
                evidence.append(str(unit.get("evidence_quality") or ""))
                comp_ids.add(str(comp.get("comp_id") or comp.get("name") or ""))
        if not rents:
            return None
        ranks = sorted(_RANK[_EVIDENCE_CONFIDENCE.get(e, "low")] for e in evidence)
        confidence = {v: k for k, v in _RANK.items()}[ranks[(len(ranks) - 1) // 2]]
        return Observation(
            metric="effective_rent_by_bed",
            geo=Geo(cbsa_code=geo.cbsa_code, metro_slug=str((payload.get("subject") or {}).get("metro_slug")),
                    submarket=geo.submarket),
            value={k: statistics.median(v) for k, v in rents.items()},
            unit=_unit("effective_rent_by_bed"),
            horizon="current",
            as_of=observed,
            confidence=confidence,
            attributes={
                "statistic": "median",
                "comp_count": len(comp_ids),
                "sample_sizes": {k: len(v) for k, v in sorted(rents.items())},
                "evidence_quality": {e: evidence.count(e) for e in sorted(set(evidence))},
            },
            provenance=Provenance(
                provider=self.name,
                path=str(path),
                vintage=observed.isoformat(),
                content_hash=sha256_files([path]),
                files=(str(path),),
                source_label=COMPS_LABEL,
            ),
        )


def _unit(metric: str) -> str:
    from engine.market_data.metrics import METRICS

    return METRICS[metric].unit


def _ratio(num: Any, den: Any) -> float | None:
    num, den = finite(num), finite(den)
    return num / den if num is not None and den else None


def _combine(per_file: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Combine per-submarket rows by quarter (inventory-weighted / summed)."""
    by_quarter: dict[str, list[dict[str, Any]]] = {}
    for rows in per_file:
        for row in rows:
            by_quarter.setdefault(row["_quarter"], []).append(row)
    out = []
    for quarter in sorted(by_quarter, key=parse_quarter):
        rows = by_quarter[quarter]
        merged: dict[str, Any] = {
            "_quarter": quarter,
            "is_forecast": any(bool(r.get("is_forecast")) for r in rows),
        }
        for col in _SUMMED:
            vals = [finite(r.get(col)) for r in rows]
            vals = [v for v in vals if v is not None]
            merged[col] = sum(vals) if vals else None
        for col in _WEIGHTED:
            pairs = [(finite(r.get(col)), finite(r.get("inventory_units")) or 0.0) for r in rows]
            pairs = [(v, w) for v, w in pairs if v is not None]
            if not pairs:
                merged[col] = None
                continue
            total = sum(w for _, w in pairs)
            if total > 0:
                merged[col] = sum(v * w for v, w in pairs) / total
            else:
                merged[col] = sum(v for v, _ in pairs) / len(pairs)
        out.append(merged)
    return out
