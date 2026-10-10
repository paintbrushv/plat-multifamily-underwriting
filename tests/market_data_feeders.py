"""Builders for synthetic feeder output files (market-data provider tests).

Each builder writes the same file layout and column names that the feeder
repositories write. The values and metro/submarket names are synthetic.

* plat-market-study-agent ``etl/transform_costar_excel.py`` (OUTPUT_COLUMNS)
* plat-market-study-agent ``etl/submarket_forecast.py`` (v2_ensemble CSV +
  ``etl/forecast_diagnostics.py`` model_diagnostics JSON)
* plat-market-study-agent ``agents/runners/emit_comp_finder_envelope.py``
  (comps.json)
* plat-supply-demand ``econometrics/cli/forecast.py`` +
  ``econometrics/models/base.py`` (forecast parquet) and
  ``econometrics/config/metros/<metro_id>.yaml``
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

# Mirrors OUTPUT_COLUMNS in the feeder's etl/transform_costar_excel.py.
COSTAR_OUTPUT_COLUMNS = [
    "date", "submarket", "geography_code", "star_rating",
    "asking_rent_unit", "asking_rent_sf",
    "asking_rent_growth_qoq", "asking_rent_growth_yoy", "asking_rent_index",
    "effective_rent_unit", "effective_rent_sf",
    "effective_rent_growth_qoq", "effective_rent_growth_yoy",
    "vacancy_rate", "occupancy_rate", "stabilized_vacancy",
    "inventory_units", "under_construction",
    "construction_starts", "construction_starts_12mo",
    "deliveries", "deliveries_12mo", "deliveries_gross",
    "absorption_units", "absorption_units_12mo", "absorption_pct",
    "demand_units",
    "sales_volume", "sales_transactions", "sold_units",
    "cap_rate", "market_cap_rate",
    "asking_rent_studio", "asking_rent_1br", "asking_rent_2br", "asking_rent_3br",
    "effective_rent_studio", "effective_rent_1br", "effective_rent_2br", "effective_rent_3br",
    "forecast_scenario", "submarket_clean", "is_forecast",
]


def costar_row(date: str, star_rating: str, *, is_forecast: bool, stem: str, **values: Any) -> dict[str, Any]:
    row: dict[str, Any] = {col: None for col in COSTAR_OUTPUT_COLUMNS}
    row.update(
        date=date,
        submarket=f"Synthetic Metro - ZZ USA - {stem}",
        geography_code="000000",
        star_rating=star_rating,
        submarket_clean=stem,
        is_forecast=is_forecast,
        inventory_units=1000,
        deliveries_gross=0,
        sales_transactions=0,
        sold_units=0,
        forecast_scenario="Base Case" if is_forecast else None,
    )
    row.update(values)
    return row


def write_costar_submarket(
    root: Path, stem: str, rows: Iterable[Mapping[str, Any]], *, subdir: str = "costar-submarket"
) -> Path:
    import pandas as pd

    out = root / "data" / "paid" / subdir
    out.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(list(rows), columns=COSTAR_OUTPUT_COLUMNS)
    for col in ("inventory_units", "deliveries_gross", "sales_transactions", "sold_units"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce").fillna(0).astype("int64")
    frame["is_forecast"] = frame["is_forecast"].astype(bool)
    path = out / f"{stem}.parquet"
    frame.to_parquet(path, index=False)
    return path


def write_ensemble(
    root: Path,
    report_dir: str,
    *,
    metro_slug: str,
    tier: str,
    tier_slug: str,
    panel_end: str,
    periods: Mapping[str, float],
    confidence_level: str = "High",
) -> Path:
    out = root / "reports" / report_dir / "forecasts"
    out.mkdir(parents=True, exist_ok=True)
    csv_path = out / f"v2_ensemble_{tier_slug}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["period", "base", "bull", "bear", "ci_80_lo", "ci_80_hi", "ci_95_lo", "ci_95_hi"])
        for period, base in periods.items():
            writer.writerow([period, base, base + 0.01, base - 0.01, base - 0.02, base + 0.02, base - 0.03, base + 0.03])
    payload = {
        "meta": {
            "metro_slug": metro_slug,
            "property": "Synthetic Subject",
            "tier": tier,
            "tier_slug": tier_slug,
            "panel_quarters": 48,
            "panel_start": "2014Q1",
            "panel_end": panel_end,
            "forecast_horizon": len(periods),
        },
        "models": {},
        "ensemble": {
            "weights": {"ARIMA": 0.5, "Ridge": 0.5},
            "ensemble_rmse": 0.01,
            "ensemble_ci80_coverage": float("nan"),
            "yoy_std": 0.03,
            "confidence_level": confidence_level,
            "regime_change_flag": False,
        },
        "data_quality_gates": {"history_quarters": "ok"},
    }
    (out / f"model_diagnostics_{tier_slug}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return csv_path


def write_comps_json(path: Path, *, metro_slug: str, as_of: str, comps: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "subject": {"address": "100 Example Way", "submarket": None,
                    "metro_slug": metro_slug, "metro_display": metro_slug.replace("_", " ").title()},
        "as_of": as_of,
        "comps": comps,
        "submarket_aggregates": {"comp_count": len(comps)},
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def comp(comp_id: str, unit_types: list[tuple[str, float, float, str]]) -> dict[str, Any]:
    return {
        "comp_id": comp_id,
        "name": comp_id.replace("_", " ").title(),
        "address": "1 Example Ave",
        "units": 200,
        "unit_types": [
            {
                "unit_type": unit_type, "sqft": 800.0, "face_rent": face, "effective_rent": eff,
                "rent_psf": None, "units_available": 1, "mom_change": None, "yoy_change": None,
                "concession": None, "evidence_quality": quality, "source_notes": None,
            }
            for unit_type, face, eff, quality in unit_types
        ],
    }


def write_supply_demand_forecast(root: Path, metro_id: str, vintage: str, points: Mapping[str, float]) -> Path:
    import pandas as pd

    out = root / "data" / "econometrics" / "forecasts" / metro_id
    out.mkdir(parents=True, exist_ok=True)
    periods = list(points)
    point = [points[p] for p in periods]
    frame = pd.DataFrame({
        "period": periods,
        "rent_growth_yoy_point": point,
        "rent_growth_yoy_ci80_lo": [p - 0.02 for p in point],
        "rent_growth_yoy_ci80_hi": [p + 0.02 for p in point],
        "rent_growth_yoy_ci95_lo": [p - 0.03 for p in point],
        "rent_growth_yoy_ci95_hi": [p + 0.03 for p in point],
        "model_name": "Ensemble",
        "vintage_date": vintage,
    })
    path = out / f"{metro_id}_forecast_{vintage}.parquet"
    frame.to_parquet(str(path))  # the feeder keeps the default index
    return path


def write_metro_config(root: Path, metro_id: str, msa_code: str) -> Path:
    out = root / "econometrics" / "config" / "metros"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{metro_id}.yaml"
    path.write_text(
        f'# Synthetic metro config\nmetro_id: {metro_id}\ndisplay_name: "Synthetic"\n'
        f'msa_code: "{msa_code}"\npermit_cbsa: "{msa_code}"\n',
        encoding="utf-8",
    )
    return path
