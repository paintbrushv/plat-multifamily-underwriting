from __future__ import annotations

import argparse
import json
import re
import statistics
from copy import deepcopy
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
import sys
from typing import Any

from engine.engine import run_underwriting
from engine.modules.debt import compute_agency_loan_terms
from engine.modules.util import dec
from engine.property_tax import (
    apply_property_tax_policy,
    normalize_property_tax_purchase_price,
    property_tax_decimal_to_json_number,
    require_property_tax_policy,
)
from engine.underwriting_policy import (
    DEFAULT_ACQUISITION_FEE_PCT,
    DEFAULT_AGENCY_SPREAD_PCT,
    DEFAULT_ANNUAL_PARTNERSHIP_EXPENSES,
    DEFAULT_ASSET_MANAGEMENT_FEE_PCT,
    DEFAULT_DISPOSITION_FEE_PCT,
    DEFAULT_PARTNERSHIP_CLOSING_COSTS,
    DEFAULT_PURCHASE_CLOSING_COST_PCT,
    DEFAULT_YEAR1_INSURANCE_PER_UNIT,
    PRIMARY_COC_TARGET_PCT,
    default_collection_loss_rate,
    default_loss_to_lease_rate,
    default_physical_vacancy_rate,
    default_r_and_m_per_unit,
    default_replacement_reserve_per_unit,
    default_year1_concessions_rate,
    default_year1_insurance_per_unit,
    insurance_requires_claim_review,
)
from engine.ingest.om_parser import _normalize_year_built
from engine.ingest.broker_om_snapshot import enrich_broker_snapshot_from_om

ALLOWED_METADATA_KEYS = {
    "deal_id",
    "run_id",
    "as_of_date",
    "analyst",
    "purpose",
    "notes",
    "property_summary",
    "address",
    "market",
    "year_built",
    "om_extraction_confidence",
    "intake_sanity_flags",
    "purchase_assumptions_source",
    "analyst_review_required",
}
_PRICE_CENT = Decimal("0.01")


def _first_nonempty(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None

def _status_count(status_counts: dict[str, Any], *names: str) -> int:
    wanted = {name.strip().lower().replace("_", " ").replace("-", " ") for name in names}
    total = 0
    for key, value in (status_counts or {}).items():
        normalized = str(key).strip().lower().replace("_", " ").replace("-", " ")
        if normalized in wanted:
            total += int(value or 0)
    return total



def _address_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(str(v) for v in value.values() if v not in (None, ""))
    return ""


def _infer_costar_metro(canonical: dict[str, Any], broker_snapshot: dict[str, Any] | None) -> str | None:
    metadata = canonical.get("metadata") or {}
    property_summary = metadata.get("property_summary") or {}
    subject_facts = property_summary.get("subject_facts") or {}
    broker = broker_snapshot or property_summary.get("broker_underwriting_snapshot") or {}
    market = _first_nonempty(
        metadata.get("market"),
        property_summary.get("market"),
        subject_facts.get("market"),
        subject_facts.get("market_slug"),
        broker.get("market"),
        broker.get("market_slug"),
    )
    if market:
        return str(market).replace("_", " ")

    address = " ".join(
        _address_text(v)
        for v in (
            metadata.get("address"),
            property_summary.get("address"),
            subject_facts.get("address"),
            broker.get("address"),
        )
        if v
    ).lower()
    if any(token in address for token in ("dallas", "fort worth", "plano", "irving")):
        return "Dallas-Fort Worth"
    if any(token in address for token in ("houston", "webster", "league city", "clear lake")):
        return "Houston"
    if any(token in address for token in ("oklahoma city", "edmond")):
        return "Oklahoma City"
    return None


def _costar_exit_cap_rate_for_case(
    canonical: dict[str, Any],
    broker_snapshot: dict[str, Any] | None,
) -> tuple[Decimal | None, dict[str, Any] | None]:
    metro = _infer_costar_metro(canonical, broker_snapshot)
    if not metro:
        return None, None
    exit_month = (
        (canonical.get("exit_assumptions") or {}).get("exit_month")
        or (canonical.get("time_grid") or {}).get("analysis_end_date")
    )
    try:
        exit_year = int(str(exit_month)[:4])
    except (TypeError, ValueError):
        exit_year = date.today().year + 5
    try:
        from engine.market_data.costar_cap_rates import get_exit_cap_rate

        rate = get_exit_cap_rate(metro, exit_year)
    except Exception:
        rate = None
    if rate is None:
        return None, None
    return Decimal(str(rate)), {
        "source": "CoStar 2026 Q2 Base Case Market Cap Rate",
        "metro": metro,
        "exit_year": exit_year,
        "rate": float(rate),
    }


def _costar_market_vacancy_for_case(
    canonical: dict[str, Any],
    broker_snapshot: dict[str, Any] | None,
) -> dict[str, Any] | None:
    metro = _infer_costar_metro(canonical, broker_snapshot)
    if not metro:
        return None
    try:
        from engine.market_data.costar_cap_rates import get_current_market_vacancy_rate

        return get_current_market_vacancy_rate(metro)
    except Exception:
        return None


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _load_bridge_if_available(
    bridge_path: str | None,
    output_dir: Path,
) -> dict[str, Any] | None:
    if bridge_path:
        return _load_json(Path(bridge_path))

    run_root_candidates = [
        output_dir.parent.parent,  # pricing_policy -> judgment -> run_id
        output_dir.parent.parent.parent,  # fallback when output_dir points at run-level folders
    ]
    for run_root in run_root_candidates:
        candidate = run_root / "reconciliation_house_case" / "revenue_quality_bridge.json"
        if not candidate.exists():
            continue
        bridge = _load_json(candidate)
        if bridge.get("status") == "applied":
            print(f"Auto-loaded revenue-quality bridge from {candidate}", file=sys.stderr)
            return bridge
        print(
            f"Revenue-quality bridge found at {candidate} with status={bridge.get('status')}; "
            "run with --revenue-quality-bridge-json to force application if intentional.",
            file=sys.stderr,
        )
        return None
    return None


def _load_trailing_actuals(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    payload = _load_json(path)
    return payload.get("trailing_actuals") or payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


def _sum_units(canonical: dict[str, Any]) -> int:
    cohorts = canonical.get("unit_cohorts") or []
    if not cohorts:
        return 0
    return int(sum(dec(row.get("unit_count", 0)) for row in cohorts))


def _normalized_code(value: Any) -> str:
    return str(value or "").strip().lower()


def _cohort_key(bedrooms: int, bathrooms: float, sqft: int) -> str:
    return f"{int(bedrooms)}BR_{float(bathrooms):.1f}BA_{int(sqft)}sf"


def _infer_bed_bath_from_code(row: dict[str, Any], fallback_code: Any = None) -> tuple[int | None, float | None]:
    bedrooms_raw = row.get("bedrooms")
    bathrooms_raw = row.get("bathrooms")
    try:
        bedrooms = int(bedrooms_raw) if bedrooms_raw not in (None, "") else None
    except (TypeError, ValueError):
        bedrooms = None
    try:
        bathrooms = float(bathrooms_raw) if bathrooms_raw not in (None, "") else None
    except (TypeError, ValueError):
        bathrooms = None

    label = _normalized_code(row.get("unit_type") or row.get("cohort_id") or fallback_code)
    if bedrooms in (None, 0):
        if label.startswith("studio") or label.startswith("s"):
            bedrooms = 0
            bathrooms = bathrooms or 1.0
        elif label.startswith("a"):
            bedrooms = 1
            bathrooms = bathrooms or 1.0
        elif label.startswith("b"):
            bedrooms = 2
            if bathrooms is None or bathrooms <= 1.0:
                bathrooms = 2.0
        elif label.startswith("c"):
            bedrooms = 3
            if bathrooms is None or bathrooms <= 1.0:
                bathrooms = 2.0
    if bedrooms is not None and bathrooms in (None, 0.0):
        bathrooms = 1.0 if bedrooms <= 1 else 2.0
    return bedrooms, bathrooms


def _comp_supported_rent_cap(
    grouped_comps: dict[str, Any] | None,
    *,
    bedrooms: int | None,
    bathrooms: float | None,
    sqft: float | None,
) -> tuple[Decimal | None, int]:
    if not grouped_comps or bedrooms in (None, "") or bathrooms in (None, "") or sqft in (None, ""):
        return None, 0
    try:
        key = _cohort_key(int(bedrooms), float(bathrooms), int(round(float(sqft))))
    except (TypeError, ValueError):
        return None, 0
    comps_by_cohort = grouped_comps.get("comps_by_cohort") or {}
    integer_bath_key = key.replace(
        f"_{float(bathrooms):.1f}BA_",
        f"_{float(bathrooms):g}BA_",
    )
    rows = comps_by_cohort.get(key) or comps_by_cohort.get(integer_bath_key) or []
    rents: list[float] = []
    for row in rows:
        try:
            rent = float(row.get("asking_rent"))
        except (TypeError, ValueError):
            continue
        if rent > 0:
            rents.append(rent)
    if not rents:
        return None, 0
    return Decimal(str(statistics.median(rents))), len(rents)


def _rebase_to_house_box_score(
    canonical: dict[str, Any],
    *,
    grouped_comps: dict[str, Any] | None = None,
) -> dict[str, Any]:
    property_summary = ((canonical.get("metadata") or {}).get("property_summary") or {})
    house_box = property_summary.get("house_box_score") or {}
    floorplans = house_box.get("floorplans") or []
    if not floorplans:
        return {
            "applied": False,
            "reason": "house_box_score_missing",
        }

    existing = {
        _normalized_code(row.get("cohort_id") or row.get("unit_type")): deepcopy(row)
        for row in canonical.get("unit_cohorts") or []
    }
    rebuilt: list[dict[str, Any]] = []
    capped_upside_units = 0
    comp_supported_upside_units = 0
    unsupported_upside_units = 0
    analyst_override_rent_cap_units = 0
    uncapped_gpr_reference = Decimal("0")
    cohort_support: list[dict[str, Any]] = []
    for idx, floorplan in enumerate(floorplans):
        units = int(floorplan.get("units") or 0)
        if units <= 0:
            continue
        code = floorplan.get("code") or floorplan.get("name") or f"house_box_{idx + 1}"
        key = _normalized_code(code)
        row = existing.get(key, {})
        cohort_id = row.get("cohort_id") or key or f"house_box_{idx + 1}"
        avg_sqft = floorplan.get("avg_sqft")
        avg_in_place_rent = floorplan.get("avg_in_place_rent")
        avg_market_rent = floorplan.get("avg_market_rent")
        if avg_in_place_rent in (None, ""):
            avg_in_place_rent = floorplan.get("avg_rent")
        if avg_market_rent in (None, ""):
            avg_market_rent = floorplan.get("avg_rent")
        if (
            avg_in_place_rent in (None, "", 0, 0.0)
            and avg_market_rent in (None, "", 0, 0.0)
            and floorplan.get("avg_rent") in (None, "", 0, 0.0)
        ):
            continue
        supported_target_rent = avg_in_place_rent if avg_in_place_rent not in (None, "", 0, 0.0) else avg_market_rent
        analyst_override_target = None
        if row.get("target_monthly_rent_source") == "analyst_override":
            raw_override = row.get("target_monthly_rent")
            if raw_override not in (None, "", 0, 0.0):
                analyst_override_target = Decimal(str(raw_override))
        inference_row = {**row, **{k: v for k, v in floorplan.items() if v not in (None, "")}}
        inferred_bedrooms, inferred_bathrooms = _infer_bed_bath_from_code(inference_row, code)
        comp_cap, comp_count = _comp_supported_rent_cap(
            grouped_comps,
            bedrooms=inferred_bedrooms,
            bathrooms=inferred_bathrooms,
            sqft=avg_sqft if avg_sqft not in (None, "") else row.get("sqft"),
        )
        if analyst_override_target is not None:
            supported_target_rent = float(analyst_override_target)
            analyst_override_rent_cap_units += units
            cohort_support.append(
                {
                    "cohort_id": cohort_id,
                    "comp_count": comp_count,
                    "in_place_rent": float(avg_in_place_rent) if avg_in_place_rent not in (None, "", 0, 0.0) else None,
                    "market_rent_reference": float(avg_market_rent) if avg_market_rent not in (None, "", 0, 0.0) else None,
                    "comp_supported_cap_rent": float(comp_cap) if comp_cap is not None else None,
                    "applied_target_rent": float(analyst_override_target),
                    "source": "analyst_override",
                }
            )
        elif avg_in_place_rent not in (None, "", 0, 0.0) and avg_market_rent not in (None, "", 0, 0.0):
            inplace = Decimal(str(avg_in_place_rent))
            market = Decimal(str(avg_market_rent))
            if comp_cap is not None:
                supported = min(market, max(inplace, comp_cap))
                supported_target_rent = float(supported)
                if supported > inplace:
                    comp_supported_upside_units += units
                cohort_support.append(
                    {
                        "cohort_id": cohort_id,
                        "comp_count": comp_count,
                        "in_place_rent": float(inplace),
                        "market_rent_reference": float(market),
                        "comp_supported_cap_rent": float(comp_cap),
                        "applied_target_rent": float(supported),
                    }
                )
            elif market > inplace:
                unsupported_upside_units += units
                cohort_support.append(
                    {
                        "cohort_id": cohort_id,
                        "comp_count": 0,
                        "in_place_rent": float(inplace),
                        "market_rent_reference": float(market),
                        "comp_supported_cap_rent": None,
                        "applied_target_rent": float(inplace),
                    }
                )
        rebuilt_row = {
            **row,
            "cohort_id": cohort_id,
            "unit_type": row.get("unit_type") or code,
            "unit_count": units,
        }
        if avg_sqft not in (None, ""):
            rebuilt_row["sqft"] = float(avg_sqft)
        if inferred_bedrooms is not None:
            rebuilt_row["bedrooms"] = int(inferred_bedrooms)
        if inferred_bathrooms is not None:
            rebuilt_row["bathrooms"] = float(inferred_bathrooms)
        if avg_in_place_rent not in (None, "", 0, 0.0):
            rebuilt_row["initial_inplace_rent"] = float(avg_in_place_rent)
        elif rebuilt_row.get("initial_inplace_rent") in (None, "", 0, 0.0) and supported_target_rent not in (None, "", 0, 0.0):
            rebuilt_row["initial_inplace_rent"] = float(supported_target_rent)
        if supported_target_rent not in (None, "", 0, 0.0):
            rebuilt_row["target_monthly_rent"] = float(supported_target_rent)
        if analyst_override_target is not None:
            rebuilt_row["target_monthly_rent_source"] = "analyst_override"
        if avg_market_rent not in (None, "", 0, 0.0):
            uncapped_gpr_reference += Decimal(str(avg_market_rent)) * Decimal(str(units)) * Decimal("12")
        if (
            avg_in_place_rent not in (None, "", 0, 0.0)
            and avg_market_rent not in (None, "", 0, 0.0)
            and Decimal(str(avg_market_rent)) > Decimal(str(avg_in_place_rent))
        ):
            capped_upside_units += units
        rebuilt.append(rebuilt_row)

    if not rebuilt:
        return {
            "applied": False,
            "reason": "house_box_score_empty",
        }

    canonical["unit_cohorts"] = rebuilt

    market_curve_by_cohort: dict[str, list[dict]] = {}
    for row in canonical.get("market_rent_curve") or []:
        market_curve_by_cohort.setdefault(_normalized_code(row.get("cohort_id")), []).append(deepcopy(row))
    ltl_by_cohort: dict[str, list[dict]] = {}
    for row in canonical.get("loss_to_lease") or []:
        ltl_by_cohort.setdefault(_normalized_code(row.get("cohort_id")), []).append(deepcopy(row))
    canonical["market_rent_curve"] = []
    canonical["loss_to_lease"] = []
    observed_vacancy = None
    status_counts = house_box.get("status_counts") or {}
    total_units = house_box.get("total_units") or _sum_units(canonical)
    if total_units:
        unavailable = _status_count(status_counts, "Vacant", "Non-Revenue", "Non Revenue")
        if unavailable > 0:
            observed_vacancy = Decimal(str(unavailable)) / Decimal(str(total_units))

    existing_vacancy = {
        _normalized_code(row.get("cohort_id")): deepcopy(row)
        for row in canonical.get("physical_vacancy_curve") or []
    }
    canonical["physical_vacancy_curve"] = []
    start_period = str(canonical["time_grid"]["analysis_start_date"])[:7]
    end_period = str(canonical["time_grid"]["analysis_end_date"])[:7]

    for row in rebuilt:
        cohort_id = row["cohort_id"]
        key = _normalized_code(cohort_id)
        market_rows = market_curve_by_cohort.get(key) or []
        if market_rows:
            for market_row in market_rows:
                has_explicit_period = bool(market_row.get("start_period") or market_row.get("end_period"))
                canonical["market_rent_curve"].append(
                    {
                        "cohort_id": cohort_id,
                        "start_period": market_row.get("start_period") or start_period,
                        "end_period": market_row.get("end_period") or end_period,
                        "market_rent": float(
                            (market_row.get("market_rent") if has_explicit_period else None)
                            or row.get("target_monthly_rent")
                            or row.get("initial_inplace_rent")
                            or 0
                        ),
                    }
                )
        else:
            market_rent = row.get("target_monthly_rent") or row.get("initial_inplace_rent") or 0
            canonical["market_rent_curve"].append(
                {
                    "cohort_id": cohort_id,
                    "start_period": start_period,
                    "end_period": end_period,
                    "market_rent": float(market_rent),
                }
            )

        ltl_rows = ltl_by_cohort.get(key) or []
        if ltl_rows:
            for ltl_row in ltl_rows:
                canonical["loss_to_lease"].append(
                    {
                        "cohort_id": cohort_id,
                        "start_period": ltl_row.get("start_period") or start_period,
                        "end_period": ltl_row.get("end_period") or end_period,
                        "ltl_percent": float(ltl_row.get("ltl_percent", 0.0) or 0.0),
                    }
                )
        else:
            canonical["loss_to_lease"].append(
                {
                    "cohort_id": cohort_id,
                    "start_period": start_period,
                    "end_period": end_period,
                    "ltl_percent": 0.0,
                }
            )
        vacancy_row = existing_vacancy.get(key, {})
        vacancy_rate = dec(vacancy_row.get("vacancy_rate", 0))
        if observed_vacancy is not None:
            vacancy_rate = max(vacancy_rate, observed_vacancy)
        canonical["physical_vacancy_curve"].append(
            {
                "cohort_id": cohort_id,
                "start_period": start_period,
                "end_period": end_period,
                "vacancy_rate": float(vacancy_rate),
            }
        )

    return {
        "applied": True,
        "source": house_box.get("derivation_basis") or house_box.get("source_file") or "house_box_score",
        "rebased_unit_count": int(house_box.get("total_units") or _sum_units(canonical)),
        "observed_status_counts": status_counts,
        "observed_unavailable_rate": float(observed_vacancy) if observed_vacancy is not None else None,
        "capped_upside_units": capped_upside_units,
        "comp_supported_upside_units": comp_supported_upside_units,
        "unsupported_upside_units": unsupported_upside_units,
        "analyst_override_rent_cap_units": analyst_override_rent_cap_units,
        "market_rent_capped_to_in_place_default": capped_upside_units > 0,
        "uncapped_market_gpr_reference": float(uncapped_gpr_reference) if uncapped_gpr_reference else None,
        "cohort_support": cohort_support,
    }


def _find_annual_opex(canonical: dict[str, Any], name_fragment: str) -> Decimal:
    for row in canonical.get("opex_table") or []:
        if name_fragment.lower() in str(row.get("category_name", "")).lower():
            return dec(row.get("base_value", 0))
    return Decimal("0")


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def _opex_bucket_for_policy(label: str) -> str | None:
    lower = label.lower()
    if "insurance" in lower:
        return "insurance"
    if "tax" in lower:
        return "real_estate_taxes"
    if "management fee" in lower or "management services" in lower:
        return "management_fees"
    if _contains_any(lower, ("water", "sewer", "electric", "gas", "trash", "utility")):
        return "utilities"
    if lower.startswith("t/o ") or "make ready" in lower or "turnover" in lower:
        return "make_ready"
    if _contains_any(
        lower,
        (
            "salary",
            "salaries",
            "bonus",
            "payroll",
            "401k",
            "worker",
            "employee health",
            "benefits",
            "temporary help",
            "rent allowance",
            "training/seminars",
            "education",
            "uniform",
            "recruiting",
            "placement",
            "employee screening",
        ),
    ):
        return "payroll"
    if _contains_any(
        lower,
        (
            "market",
            "advertis",
            "apartments.com",
            "locator",
            "brochures",
            "websites",
            "facebook ads",
            "google ads",
            "crm",
            "model expense",
            "move-in/renewal",
            "outreach",
            "promotional",
            "resident hospitality",
            "resident retention",
            "rent.com",
            "signs/banners",
            "social activities",
            "social media",
        ),
    ):
        return "marketing"
    if _contains_any(
        lower,
        (
            "contract",
            "landscap",
            "grounds",
            "security",
            "pest",
            "answering service",
            "cellular",
            "telephone",
            "alarm monitoring",
            "annual inspections",
            "termite",
            "office equipment rental",
            "professional services",
        ),
    ):
        return "contract_services"
    if _contains_any(
        lower,
        (
            "legal",
            "software",
            "computer",
            "bank",
            "credit",
            "dues",
            "subscription",
            "screening",
            "deductible",
            "administrative",
            "licenses",
            "permit",
            "meals",
            "mileage",
            "office supplies",
            "shipping",
            "postage",
            "storage",
            "travel",
            "office cable high speed - internet",
        ),
    ):
        return "general_administrative"
    if _contains_any(
        lower,
        (
            "repair",
            "maint",
            "supplies",
            "cleaning",
            "carpet",
            "paint",
            "gate/fence",
            "fire exting",
            "alarm/sprinkler",
            "glass",
            "screen",
            "hardware",
            "hvac",
            "key",
            "lock",
            "plumbing",
            "pool",
            "site tools",
            "site vehicle",
            "window covering",
        ),
    ):
        return "repairs_maintenance"
    return None


def _sum_opex_bucket(canonical: dict[str, Any], bucket: str) -> Decimal:
    total = Decimal("0")
    for row in canonical.get("opex_table") or []:
        if _opex_bucket_for_policy(str(row.get("category_name", ""))) == bucket:
            total += dec(row.get("base_value", 0))
    return total


def _parse_percent_note(text: str | None) -> Decimal | None:
    if not text:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", str(text))
    if not match:
        return None
    return Decimal(match.group(1)) / Decimal("100")


def _ensure_year1_concession_schedule(
    canonical: dict[str, Any],
    *,
    rate: Decimal,
) -> None:
    if rate <= 0:
        canonical.pop("concession_schedule", None)
        return
    start = str(canonical["time_grid"]["analysis_start_date"])[:7]
    sy, sm = int(start[:4]), int(start[5:7])
    end_year = sy + ((sm - 1 + 11) // 12)
    end_month = ((sm - 1 + 11) % 12) + 1
    end = f"{end_year:04d}-{end_month:02d}"
    canonical["concession_schedule"] = [
        {
            "concession_id": "house_year1_concessions",
            "start_month": start,
            "end_month": end,
            "concession_type": "pct_rent",
            "amount": float(rate),
            "applies_to_cohort": "ALL",
        }
    ]


def _extract_t12_vacancy_rate(trailing_actuals: dict[str, Any] | None) -> Decimal | None:
    if not trailing_actuals:
        return None
    gpr = dec(trailing_actuals.get("gross_potential_rent", 0))
    vacancy = dec(trailing_actuals.get("vacancy_loss", 0))
    if gpr <= 0 or vacancy <= 0:
        return None
    return vacancy / gpr


def _extract_broker_vacancy_rate(broker_snapshot: dict[str, Any] | None) -> Decimal | None:
    if not broker_snapshot:
        return None
    revenue = broker_snapshot.get("revenue_assumptions") or {}
    gpr = dec(revenue.get("gross_potential_rent", 0))
    vacancy = abs(dec(revenue.get("vacancy_loss", 0)))
    if (gpr <= 0 or vacancy <= 0) and broker_snapshot.get("income_table"):
        for row in broker_snapshot.get("income_table") or []:
            line = str(row.get("line", "")).lower()
            amount = dec(row.get("annual_amount", 0))
            if "current market rent" in line or "gross potential" in line:
                gpr += amount
            elif "value-add rent growth" in line:
                gpr += amount
            elif "vacancy" in line:
                vacancy += abs(amount)
    if gpr <= 0 or vacancy <= 0:
        return None
    return vacancy / gpr


def _vacancy_anchor_summary(
    canonical: dict[str, Any],
    *,
    default_vacancy: Decimal,
    broker_vacancy: Decimal | None,
    trailing_actuals: dict[str, Any] | None,
    broker_snapshot: dict[str, Any] | None,
) -> tuple[Decimal, dict[str, Any]]:
    property_summary = (canonical.get("metadata") or {}).get("property_summary") or {}
    house_box = property_summary.get("house_box_score") or {}
    status_counts = house_box.get("status_counts") or {}
    total_units = Decimal(str(house_box.get("total_units") or _sum_units(canonical) or 0))
    vacant_units = Decimal(str(_status_count(status_counts, "Vacant", "Non-Revenue", "Non Revenue")))
    notice_units = Decimal(str(_status_count(status_counts, "Notice")))
    rent_roll_vacant = vacant_units / total_units if total_units else None
    vacant_plus_notice = (vacant_units + notice_units) / total_units if total_units else None
    t12_vacancy = _extract_t12_vacancy_rate(trailing_actuals)
    broker_table_vacancy = _extract_broker_vacancy_rate(broker_snapshot)
    broker_anchor = broker_vacancy if broker_vacancy is not None else broker_table_vacancy
    costar = _costar_market_vacancy_for_case(canonical, broker_snapshot)
    costar_vacancy = (
        Decimal(str(costar["vacancy_rate"]))
        if costar and costar.get("vacancy_rate") is not None
        else None
    )
    cohort_rates = [
        Decimal(str(row.get("vacancy_rate", 0)))
        for row in canonical.get("physical_vacancy_curve") or []
    ]
    worst_cohort = max(cohort_rates) if cohort_rates else None

    base_candidates = {
        "broker_om": broker_anchor,
        "rent_roll_vacant": rent_roll_vacant,
        "t12_vacancy_gpr": t12_vacancy,
        "vintage_house_floor": default_vacancy,
        "costar_market_current_quarter": costar_vacancy,
    }
    conservative_candidates = {
        "broker_om": broker_anchor,
        "rent_roll_vacant_plus_notice": vacant_plus_notice,
        "t12_vacancy_gpr": t12_vacancy,
        "vintage_house_floor": default_vacancy,
        "costar_market_current_quarter": costar_vacancy,
    }
    usable_base = {k: v for k, v in base_candidates.items() if v is not None}
    usable_conservative = {k: v for k, v in conservative_candidates.items() if v is not None}
    base_rate = max(usable_base.values()) if usable_base else Decimal("0")
    conservative_rate = max(usable_conservative.values()) if usable_conservative else base_rate

    def fmt_candidates(values: dict[str, Decimal | None]) -> dict[str, float | None]:
        return {k: (float(v) if v is not None else None) for k, v in values.items()}

    return base_rate, {
        "method": "base_case_max_property_level_anchors",
        "base_case_candidates": fmt_candidates(base_candidates),
        "base_case_selected_rate": float(base_rate),
        "base_case_selected_anchor": [
            k for k, v in usable_base.items() if v == base_rate
        ],
        "conservative_downside_candidates": fmt_candidates(conservative_candidates),
        "conservative_downside_selected_rate": float(conservative_rate),
        "conservative_downside_selected_anchor": [
            k for k, v in usable_conservative.items() if v == conservative_rate
        ],
        "stress_flag_worst_cohort_vacancy": float(worst_cohort) if worst_cohort is not None else None,
        "stress_flag_note": (
            "Worst cohort vacancy is logged for review/stress only and is not "
            "applied property-wide unless explicitly selected."
        ),
        "observed_status_counts": status_counts,
        "costar_market_vacancy_source": costar,
    }


def _apply_house_revenue_policy(
    canonical: dict[str, Any],
    *,
    year_built: int | None,
    strategy: str,
    deferred: bool = False,
    broker_snapshot: dict[str, Any] | None = None,
    trailing_actuals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    broker_notes = (broker_snapshot or {}).get("underwriting_notes", {})
    default_ltl = default_loss_to_lease_rate(year_built, strategy=strategy) or Decimal("0")
    default_vacancy = default_physical_vacancy_rate(
        year_built, strategy=strategy, deferred=deferred
    ) or Decimal("0")
    default_collection = default_collection_loss_rate(
        year_built, strategy=strategy, deferred=deferred
    ) or Decimal("0")
    default_concessions = default_year1_concessions_rate(
        year_built, strategy=strategy, deferred=deferred
    ) or Decimal("0")

    broker_ltl = _parse_percent_note(broker_notes.get("loss_to_lease"))
    broker_vacancy = _parse_percent_note(broker_notes.get("vacancy"))
    broker_bad_debt = _parse_percent_note(broker_notes.get("bad_debt"))
    broker_concessions = _parse_percent_note(broker_notes.get("concessions"))

    applied_ltls: list[float] = []
    for row in canonical.get("loss_to_lease") or []:
        cohort_id = row["cohort_id"]
        cohort = next((c for c in canonical.get("unit_cohorts") or [] if c["cohort_id"] == cohort_id), {})
        market = next(
            (
                mr["market_rent"]
                for mr in canonical.get("market_rent_curve") or []
                if mr["cohort_id"] == cohort_id
            ),
            cohort.get("target_monthly_rent") or cohort.get("initial_inplace_rent") or 0,
        )
        inplace = cohort.get("initial_inplace_rent") or 0
        derived = Decimal("0")
        if market:
            market_dec = Decimal(str(market))
            inplace_dec = Decimal(str(inplace))
            derived = max(Decimal("0"), Decimal("1") - (inplace_dec / market_dec))
        else:
            market_dec = Decimal("0")
            inplace_dec = Decimal(str(inplace))
        if market_dec <= inplace_dec:
            applied = Decimal("0")
        else:
            applied = max(derived, default_ltl, broker_ltl or Decimal("0"))
        row["ltl_percent"] = float(applied)
        applied_ltls.append(float(applied))

    applied_vacancy, vacancy_summary = _vacancy_anchor_summary(
        canonical,
        default_vacancy=default_vacancy,
        broker_vacancy=broker_vacancy,
        trailing_actuals=trailing_actuals,
        broker_snapshot=broker_snapshot,
    )
    for row in canonical.get("physical_vacancy_curve") or []:
        row["vacancy_rate"] = float(applied_vacancy)

    current_concessions = Decimal("0")
    if canonical.get("concession_schedule"):
        current_concessions = max(
            Decimal(str(row.get("amount", 0)))
            for row in canonical["concession_schedule"]
            if row.get("concession_type") == "pct_rent"
        )
    applied_concessions = max(
        current_concessions,
        default_concessions,
        broker_concessions or Decimal("0"),
    )
    _ensure_year1_concession_schedule(canonical, rate=applied_concessions)

    current_collection = Decimal("0")
    if canonical.get("collection_loss_curve"):
        current_collection = max(
            Decimal(str(row.get("loss_rate", 0))) for row in canonical["collection_loss_curve"]
        )
    applied_collection = max(
        current_collection,
        default_collection,
        broker_bad_debt or Decimal("0"),
    )
    canonical["collection_loss_curve"] = [
        {
            "applies_to": "ALL",
            "start_period": str(canonical["time_grid"]["analysis_start_date"])[:7],
            "end_period": str(canonical["time_grid"]["analysis_end_date"])[:7],
            "loss_rate": float(applied_collection),
        }
    ]

    annualized_house_gpr = Decimal("0")
    for cohort in canonical.get("unit_cohorts") or []:
        market = next(
            (
                Decimal(str(mr["market_rent"]))
                for mr in canonical.get("market_rent_curve") or []
                if mr["cohort_id"] == cohort["cohort_id"]
            ),
            Decimal(str(cohort.get("initial_inplace_rent") or 0)),
        )
        annualized_house_gpr += market * Decimal(str(cohort.get("unit_count", 0))) * Decimal("12")
    broker_gpr = Decimal(str((broker_snapshot or {}).get("revenue_assumptions", {}).get("gross_potential_rent", 0)))
    market_rent_requires_comp_support = bool(
        broker_gpr and annualized_house_gpr > broker_gpr * Decimal("1.10")
    )

    return {
        "loss_to_lease_rate_applied": (
            round(sum(applied_ltls) / len(applied_ltls), 6) if applied_ltls else 0.0
        ),
        "physical_vacancy_rate_applied": float(applied_vacancy),
        "physical_vacancy_anchor_summary": vacancy_summary,
        "collection_loss_rate_applied": float(applied_collection),
        "year1_concessions_rate_applied": float(applied_concessions),
        "broker_ltl_reference": float(broker_ltl) if broker_ltl is not None else None,
        "broker_vacancy_reference": float(broker_vacancy) if broker_vacancy is not None else None,
        "broker_bad_debt_reference": float(broker_bad_debt) if broker_bad_debt is not None else None,
        "broker_concessions_reference": float(broker_concessions) if broker_concessions is not None else None,
        "annualized_house_gpr": float(annualized_house_gpr),
        "broker_gpr_reference": float(broker_gpr) if broker_gpr else None,
        "market_rent_requires_comp_support": market_rent_requires_comp_support,
    }


def _upsert_annual_opex(
    canonical: dict[str, Any],
    *,
    category_name: str,
    annual_amount: Decimal,
) -> None:
    opex = canonical.setdefault("opex_table", [])
    for row in opex:
        if str(row.get("category_name", "")).lower() == category_name.lower():
            row["calculation_type"] = "fixed_annual"
            row["base_value"] = float(annual_amount)
            row["recoverable_flag"] = False
            return
    opex.append(
        {
            "category_name": category_name,
            "calculation_type": "fixed_annual",
            "base_value": float(annual_amount),
            "recoverable_flag": False,
        }
    )


def _upsert_annual_opex_by_match(
    canonical: dict[str, Any],
    *,
    name_fragment: str,
    fallback_category_name: str,
    annual_amount: Decimal,
) -> str:
    opex = canonical.setdefault("opex_table", [])
    matches = [
        row
        for row in opex
        if name_fragment.lower() in str(row.get("category_name", "")).lower()
    ]
    if name_fragment.lower() == "internet":
        excluded_context = ("ad", "advertis", "office", "marketing")
        preferred = [
            row
            for row in matches
            if not any(token in str(row.get("category_name", "")).lower() for token in excluded_context)
        ]
        matches = preferred or matches
    if matches:
        row = matches[0]
        row["calculation_type"] = "fixed_annual"
        row["base_value"] = float(annual_amount)
        row["recoverable_flag"] = False
        return str(row["category_name"])
    _upsert_annual_opex(
        canonical,
        category_name=fallback_category_name,
        annual_amount=annual_amount,
    )
    return fallback_category_name




def _apply_revenue_quality_bridge(
    canonical: dict[str, Any],
    bridge: dict[str, Any] | None,
) -> dict[str, Any]:
    if not bridge:
        return {"applied": False, "reason": "bridge_missing"}

    start_period = str(canonical["time_grid"]["analysis_start_date"])[:7]
    end_period = str(canonical["time_grid"]["analysis_end_date"])[:7]
    program_prefix = "rq_bridge_"

    canonical["revenue_programs"] = [
        row
        for row in canonical.get("revenue_programs") or []
        if not str(row.get("program_id", "")).startswith(program_prefix)
    ]
    canonical["program_adoption_curve"] = [
        row
        for row in canonical.get("program_adoption_curve") or []
        if not str(row.get("program_id", "")).startswith(program_prefix)
    ]

    added_programs: list[dict[str, Any]] = []
    commercial_credit = Decimal("0")
    other_income_credit = Decimal("0")
    line_summaries: list[dict[str, Any]] = []
    credit_by_bucket: dict[str, Decimal] = {}
    credit_by_decision: dict[str, Decimal] = {}
    paired_expenses: dict[str, float] = {}
    bridge_lines = bridge.get("lines") or bridge.get("line_items") or []
    for line in bridge_lines:
        house_credit = dec(line.get("house_credit", line.get("house_annual_credit", 0)))
        line_item = str(line.get("line_item") or line.get("line") or "income").strip().lower()
        bucket_type = str(line.get("bucket_type") or "uncategorized")
        decision = str(line.get("decision") or "unspecified")
        t12_amount = dec(line.get("t12_amount") or line.get("source_amount") or 0)
        credit_pct = None
        if line.get("credit_pct") is not None:
            credit_pct = float(dec(line.get("credit_pct")))
        elif t12_amount > 0:
            credit_pct = float(house_credit / t12_amount)
        if house_credit > 0:
            credit_by_bucket[bucket_type] = credit_by_bucket.get(bucket_type, Decimal("0")) + house_credit
            credit_by_decision[decision] = credit_by_decision.get(decision, Decimal("0")) + house_credit
        line_summaries.append(
            {
                "line_item": line_item,
                "bucket_type": bucket_type,
                "decision": decision,
                "t12_amount": float(t12_amount) if t12_amount else None,
                "credit_pct": credit_pct,
                "annual_house_credit": float(house_credit),
                "rationale": (
                    line.get("notes")
                    or line.get("rationale")
                    or line.get("support")
                    or line.get("source_support")
                ),
            }
        )
        if house_credit <= 0:
            continue
        program_id = f"{program_prefix}{re.sub(r'[^a-z0-9]+', '_', line_item).strip('_')}"
        if line.get("bucket_type") == "mixed_use_commercial" or line_item == "commercial_income":
            commercial_credit += house_credit
        else:
            other_income_credit += house_credit
        program = {
            "program_id": program_id,
            "program_name": str(line.get("line_item") or "Revenue quality bridge income").replace("_", " ").title(),
            "program_type": "asset-based",
            "pricing_type": "$/asset",
            "price_value": float(house_credit / Decimal("12")),
            "eligible_units": "ALL",
            "start_period": start_period,
            "end_period": end_period,
        }
        adoption = {
            "program_id": program_id,
            "start_period": start_period,
            "end_period": end_period,
            "adoption_rate": 1.0,
        }
        canonical.setdefault("revenue_programs", []).append(program)
        canonical.setdefault("program_adoption_curve", []).append(adoption)
        added_programs.append(
            {
                "program_id": program_id,
                "line_item": line.get("line_item") or line.get("line"),
                "decision": line.get("decision"),
                "annual_house_credit": float(house_credit),
            }
        )
        try:
            paired_expense = dec(line.get("paired_expense") or 0)
        except Exception:
            paired_expense = Decimal("0")
        if paired_expense > 0:
            paired_expenses[line_item] = float(paired_expense)

    internet_expense = dec(
        ((bridge.get("recommended_house_revenue_view") or {}).get("paired_expenses") or {}).get(
            "internet_expense",
            0,
        )
    )
    if internet_expense <= 0 and paired_expenses.get("internet_income"):
        internet_expense = Decimal(str(paired_expenses["internet_income"]))

    paired_expense_rows: list[dict[str, Any]] = []
    if internet_expense > 0:
        category = _upsert_annual_opex_by_match(
            canonical,
            name_fragment="internet",
            fallback_category_name="Internet Expense - Revenue Quality Bridge",
            annual_amount=internet_expense,
        )
        paired_expense_rows.append(
            {
                "category_name": category,
                "annual_amount": float(internet_expense),
            }
        )

    if added_programs:
        canonical["utility_recovery_rules"] = [
            {
                "utility_category": "all_recoverable_opex",
                "recovery_basis": "disabled_replaced_by_revenue_quality_bridge",
                "recovery_rate": 0.0,
                "lag_months": 0,
            }
        ]

    property_summary = canonical.setdefault("metadata", {}).setdefault("property_summary", {})
    property_summary["revenue_quality_bridge"] = {
        "deal_slug": bridge.get("deal_slug"),
        "run_id": bridge.get("run_id"),
        "prepared_on": bridge.get("prepared_on"),
        "commercial_income_credit": float(commercial_credit),
        "other_income_credit": float(other_income_credit),
        "combined_gross_revenue_credit": float(commercial_credit + other_income_credit),
        "paired_expenses": paired_expense_rows,
        "program_count": len(added_programs),
        "credit_by_bucket_type": {
            bucket: float(amount) for bucket, amount in sorted(credit_by_bucket.items())
        },
        "credit_by_decision": {
            decision: float(amount) for decision, amount in sorted(credit_by_decision.items())
        },
        "line_summaries": line_summaries,
        "rent_roll_turnover_signal": bridge.get("rent_roll_turnover_signal"),
        "credit_policy_notes": bridge.get("credit_policy_notes"),
    }

    return {
        "applied": bool(added_programs),
        "source_deal_slug": bridge.get("deal_slug"),
        "source_run_id": bridge.get("run_id"),
        "commercial_income_credit": float(commercial_credit),
        "other_income_credit": float(other_income_credit),
        "combined_gross_revenue_credit": float(commercial_credit + other_income_credit),
        "paired_expenses": paired_expense_rows,
        "generic_utility_recovery_disabled": bool(added_programs),
        "programs": added_programs,
    }


def _ancillary_income_bridge_required(
    canonical: dict[str, Any],
    *,
    broker_snapshot: dict[str, Any] | None,
) -> tuple[bool, str | None]:
    property_summary = ((canonical.get("metadata") or {}).get("property_summary") or {})
    material = property_summary.get("material_ancillary_income") or {}
    if material.get("requires_revenue_quality_bridge"):
        reason = material.get("reason") or "metadata.material_ancillary_income requires revenue-quality bridge"
        return True, str(reason)

    revenue = (broker_snapshot or {}).get("revenue_assumptions") or {}
    egi = revenue.get("effective_gross_income") or revenue.get("total_revenue")
    ancillary_keys = (
        "other_income",
        "commercial_income",
        "utility_reimbursements",
        "rubs_income",
        "parking_storage_income",
        "internet_income",
        "package_income",
        "amenity_income",
    )
    ancillary_total = sum(dec(revenue.get(key, 0) or 0) for key in ancillary_keys)
    if ancillary_total <= 0:
        return False, None

    units = Decimal(str(_sum_units(canonical) or 0))
    if egi not in (None, "", 0, 0.0):
        egi_dec = dec(egi)
        if egi_dec > 0 and ancillary_total / egi_dec > Decimal("0.05"):
            return True, "broker ancillary/commercial income exceeds 5% of EGI"
    if units > 0 and ancillary_total / units > Decimal("250"):
        return True, "broker ancillary/commercial income exceeds $250/unit/year"
    return False, None


def _hold_months(canonical: dict[str, Any]) -> int:
    start = str(canonical["time_grid"]["analysis_start_date"])[:7]
    end = str(canonical["time_grid"]["analysis_end_date"])[:7]
    sy, sm = int(start[:4]), int(start[5:7])
    ey, em = int(end[:4]), int(end[5:7])
    return (ey - sy) * 12 + (em - sm) + 1


def _preview_projected_noi(canonical: dict[str, Any]) -> Decimal:
    preview = deepcopy(canonical)
    hold_months = _hold_months(preview)
    tax_calculation = (
        ((canonical.get("metadata") or {}).get("property_summary") or {}).get(
            "property_tax_calculation"
        )
        or {}
    )
    tax_price_basis = tax_calculation.get("purchase_price_basis")
    if tax_price_basis is None:
        preview_price = max(
            dec(
                (canonical.get("purchase_assumptions") or {}).get(
                    "purchase_price",
                    0,
                )
            ),
            Decimal("20000000"),
        )
    else:
        preview_price = dec(tax_price_basis)
    preview["purchase_assumptions"] = {
        "purchase_price": float(preview_price),
        "equity_contribution": float(preview_price),
        "closing_costs": 0.0,
        "total_equity_basis": float(preview_price),
    }
    preview["pricing_provenance"] = {
        "published_om_price": None,
        "broker_whisper_price": None,
        "strike_price": float(preview_price),
        "strike_price_basis": "other",
        "strike_price_derivation": (
            "Temporary all-equity preview case used only to estimate projected NOI "
            "before target-price backsolve."
        ),
        "om_pricing_process": "unknown",
        "as_of_date": str(date.today()),
    }
    preview["debt_terms"] = {
        "commitment": 0.0,
        "rate": 0.0,
        "amort_years": 30,
        "io_months": 0,
        "term_months": hold_months,
        "loan_start_month": str(preview["time_grid"]["analysis_start_date"])[:7],
    }
    preview.setdefault("fund_assumptions", {})
    preview.setdefault("exit_assumptions", {"exit_cap_rate": 0.0625, "sale_cost_percent": 0.02})
    results = run_underwriting(preview)
    by_year = results["cashflow"]["by_year"]
    if len(by_year) >= 2:
        return dec(by_year[1]["net_operating_income"])
    if by_year:
        return dec(by_year[0]["net_operating_income"])
    return Decimal("0")


def _prepare_house_assumptions(
    canonical: dict[str, Any],
    *,
    year_built: int | None,
    strategy: str,
    target_coc: Decimal,
    benchmark_treasury: Decimal,
    agency_spread: Decimal,
    exit_cap_rate: Decimal | None,
    sale_cost_percent: Decimal,
    purchase_closing_cost_pct: Decimal,
    partnership_closing_costs: Decimal,
    acquisition_fee_pct: Decimal,
    asset_management_fee_pct: Decimal,
    annual_partnership_expenses: Decimal,
    disposition_fee_pct: Decimal,
    loan_closing_costs: Decimal,
    insurance_per_unit_override: Decimal | None = None,
    broker_snapshot: dict[str, Any] | None = None,
    grouped_comps: dict[str, Any] | None = None,
    revenue_quality_bridge: dict[str, Any] | None = None,
    trailing_actuals: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    prepared = deepcopy(canonical)
    metadata = prepared.setdefault("metadata", {})
    property_summary = metadata.get("property_summary") or {}
    if year_built is None:
        for candidate in (
            metadata.get("year_built"),
            property_summary.get("year_built"),
            (property_summary.get("broker_underwriting_snapshot") or {}).get("year_built"),
            (broker_snapshot or {}).get("year_built"),
        ):
            if candidate not in (None, ""):
                try:
                    year_built = _normalize_year_built(int(candidate))
                    if year_built is None:
                        continue
                    break
                except (TypeError, ValueError):
                    continue
    rebase_summary = _rebase_to_house_box_score(prepared, grouped_comps=grouped_comps)
    units = _sum_units(prepared)
    observed_insurance = _find_annual_opex(prepared, "insurance")
    observed_insurance_per_unit = (
        observed_insurance / Decimal(str(units)) if units and observed_insurance > 0 else None
    )
    insurance_per_unit = (
        insurance_per_unit_override
        if insurance_per_unit_override is not None
        else default_year1_insurance_per_unit(observed_insurance_per_unit)
    )
    _upsert_annual_opex(
        prepared,
        category_name="Insurance",
        annual_amount=insurance_per_unit * Decimal(str(units)),
    )

    reserve_per_unit = default_replacement_reserve_per_unit(
        year_built,
        strategy=strategy,
        deferred=False,
    )
    if reserve_per_unit is not None:
        annual_reserve = reserve_per_unit * Decimal(str(units))
        prepared["replacement_reserves"] = [
            {
                "start_period": str(prepared["time_grid"]["analysis_start_date"])[:7],
                "end_period": str(prepared["time_grid"]["analysis_end_date"])[:7],
                "annual_amount": float(annual_reserve),
            }
        ]

    observed_r_and_m = _sum_opex_bucket(prepared, "repairs_maintenance")
    r_and_m_per_unit = default_r_and_m_per_unit(
        year_built,
        strategy=strategy,
        deferred=False,
    )
    r_and_m_floor_annual = None
    r_and_m_top_up = Decimal("0")
    if r_and_m_per_unit is not None and units:
        r_and_m_floor_annual = r_and_m_per_unit * Decimal(str(units))
        if observed_r_and_m < r_and_m_floor_annual:
            r_and_m_top_up = r_and_m_floor_annual - observed_r_and_m
            _upsert_annual_opex(
                prepared,
                category_name="Repairs & Maintenance Vintage Floor Top-Up",
                annual_amount=r_and_m_top_up,
            )

    if metadata.get("address"):
        property_summary["address"] = metadata["address"]
    if metadata.get("market"):
        property_summary["market"] = metadata["market"]
    if broker_snapshot:
        property_summary["broker_underwriting_snapshot"] = broker_snapshot
    property_summary["underwriting_strategy"] = strategy
    property_summary["primary_cash_on_cash_target_pct"] = float(target_coc)
    property_summary["agency_benchmark_treasury"] = float(benchmark_treasury)
    property_summary["agency_spread"] = float(agency_spread)
    if year_built is not None:
        metadata["year_built"] = year_built
        property_summary["year_built"] = year_built
    property_summary["year1_insurance_per_unit_applied"] = float(insurance_per_unit)
    property_summary["insurance_claim_review_required"] = insurance_requires_claim_review(
        observed_insurance_per_unit
    )
    if insurance_per_unit_override is not None:
        property_summary["insurance_override_source"] = "analyst_override"
    metadata["property_summary"] = property_summary
    prepared["metadata"] = {
        key: value
        for key, value in metadata.items()
        if key in ALLOWED_METADATA_KEYS
    }

    prepared["fund_assumptions"] = _deep_merge(
        prepared.get("fund_assumptions") or {},
        {
            "acquisition_fee_pct": float(acquisition_fee_pct),
            "asset_management_fee_pct": float(asset_management_fee_pct),
            "annual_partnership_expenses": float(annual_partnership_expenses),
            "disposition_fee_pct": float(disposition_fee_pct),
            "partnership_closing_costs": float(partnership_closing_costs),
        },
    )
    prepared["exit_assumptions"] = _deep_merge(
        prepared.get("exit_assumptions") or {},
        {
            "sale_cost_percent": float(sale_cost_percent),
            "exit_cap_rate": float(
                exit_cap_rate
                if exit_cap_rate is not None
                else dec((prepared.get("exit_assumptions") or {}).get("exit_cap_rate", Decimal("0.0625")))
            ),
            "exit_month": str(prepared["time_grid"]["analysis_end_date"])[:7],
        },
    )

    revenue_summary = _apply_house_revenue_policy(
        prepared,
        year_built=year_built,
        strategy=strategy,
        deferred=False,
        broker_snapshot=broker_snapshot,
        trailing_actuals=trailing_actuals,
    )
    if rebase_summary.get("unsupported_upside_units", 0):
        revenue_summary["market_rent_requires_comp_support"] = True
    bridge_required, bridge_required_reason = _ancillary_income_bridge_required(
        prepared,
        broker_snapshot=broker_snapshot,
    )
    if bridge_required and not revenue_quality_bridge:
        raise RuntimeError(
            "Revenue-quality bridge required before pricing: "
            f"{bridge_required_reason}. "
            "Create outputs/<run_id>/reconciliation_house_case/revenue_quality_bridge.json "
            "per .claude/references/ancillary-income-underwriting-policy.md and pass "
            "--revenue-quality-bridge-json."
        )
    revenue_quality_summary = _apply_revenue_quality_bridge(
        prepared,
        revenue_quality_bridge,
    )

    policy_summary = {
        "units": units,
        "year_built": year_built,
        "strategy": strategy,
        "target_cash_on_cash_pct": float(target_coc),
        "benchmark_treasury": float(benchmark_treasury),
        "agency_spread": float(agency_spread),
        "purchase_closing_cost_pct": float(purchase_closing_cost_pct),
        "acquisition_fee_pct": float(acquisition_fee_pct),
        "asset_management_fee_pct": float(asset_management_fee_pct),
        "annual_partnership_expenses": float(annual_partnership_expenses),
        "partnership_closing_costs": float(partnership_closing_costs),
        "loan_closing_costs": float(loan_closing_costs),
        "insurance_per_unit_applied": float(insurance_per_unit),
        "insurance_per_unit_override": (
            float(insurance_per_unit_override) if insurance_per_unit_override is not None else None
        ),
        "insurance_claim_review_required": insurance_requires_claim_review(
            observed_insurance_per_unit
        ),
        "observed_insurance_per_unit": (
            float(observed_insurance_per_unit) if observed_insurance_per_unit is not None else None
        ),
        "replacement_reserve_per_unit": (
            float(reserve_per_unit) if reserve_per_unit is not None else None
        ),
        "observed_repairs_maintenance_annual": float(observed_r_and_m),
        "repairs_maintenance_floor_per_unit": (
            float(r_and_m_per_unit) if r_and_m_per_unit is not None else None
        ),
        "repairs_maintenance_floor_annual": (
            float(r_and_m_floor_annual) if r_and_m_floor_annual is not None else None
        ),
        "repairs_maintenance_top_up_annual": float(r_and_m_top_up),
        "house_box_score_rebase": rebase_summary,
        "revenue_quality_bridge_required": bridge_required,
        "revenue_quality_bridge_required_reason": bridge_required_reason,
        "revenue_quality_bridge": revenue_quality_summary,
        **revenue_summary,
    }
    return prepared, policy_summary


def _build_price_case(
    canonical: dict[str, Any],
    *,
    price: Decimal,
    target_coc: Decimal,
    year_built: int | None,
    benchmark_treasury: Decimal,
    agency_spread: Decimal,
    purchase_closing_cost_pct: Decimal,
    partnership_closing_costs: Decimal,
    acquisition_fee_pct: Decimal,
    loan_closing_costs: Decimal,
    projected_noi: Decimal,
) -> dict[str, Any]:
    price = normalize_property_tax_purchase_price(price)
    persisted_price = property_tax_decimal_to_json_number(
        price,
        field="purchase_price",
    )
    case, tax_calculation = apply_property_tax_policy(
        canonical,
        purchase_price=price,
    )
    hold_months = _hold_months(case)
    candidate_projected_noi = _preview_projected_noi(case)
    agency_terms = compute_agency_loan_terms(
        year_built,
        t12_noi=candidate_projected_noi,
        projected_noi=candidate_projected_noi,
        purchase_price=price,
        benchmark_5yr_treasury=benchmark_treasury,
        agency_spread=agency_spread,
    )
    commitment = dec(agency_terms["loan_amount"])
    closing_costs = price * purchase_closing_cost_pct
    acquisition_fee = price * acquisition_fee_pct
    upfront_capex = sum(
        dec(row.get("amount", 0)) for row in case.get("capex_upfront_funded") or []
    )
    equity_contribution = max(Decimal("0"), price - commitment)
    total_equity_basis = (
        equity_contribution
        + closing_costs
        + acquisition_fee
        + partnership_closing_costs
        + loan_closing_costs
        + upfront_capex
    )
    case["purchase_assumptions"] = {
        "purchase_price": persisted_price,
        "equity_contribution": float(equity_contribution),
        "closing_costs": float(closing_costs),
        "total_equity_basis": float(total_equity_basis),
    }
    case["debt_terms"] = {
        "commitment": float(commitment),
        "rate": agency_terms["rate"],
        "amort_years": agency_terms["amort_years"],
        "io_months": agency_terms["io_months"],
        "term_months": hold_months,
        "loan_start_month": str(case["time_grid"]["analysis_start_date"])[:7],
        "loan_closing_costs": float(loan_closing_costs),
    }
    case.setdefault("pricing_provenance", {})
    case["pricing_provenance"] = {
        "published_om_price": None,
        "broker_whisper_price": None,
        "strike_price": persisted_price,
        "strike_price_basis": "analyst_target",
        "strike_price_derivation": (
            f"Backsolved to {float(target_coc) * 100:.1f}% Year-1 post-debt CoC "
            f"using hold-matched Treasury {agency_terms['rate_breakdown']['benchmark_5yr_treasury']:.4f} "
            f"+ spread {agency_terms['rate_breakdown']['agency_spread']:.4f}; "
            f"DSCR sizing method {agency_terms['sizing_method']}; "
            f"projected NOI used {float(candidate_projected_noi):,.0f}."
        ),
        "om_pricing_process": "unknown",
        "as_of_date": str(date.today()),
    }
    return case


def _evaluate_case(case: dict[str, Any]) -> tuple[dict[str, Any], Decimal]:
    results = run_underwriting(case)
    coc_metrics = results["metrics"]["coc"]
    free_cf = coc_metrics.get("free_cf_year_1")
    equity_basis = (coc_metrics.get("components") or {}).get("equity_basis")
    if free_cf is not None and equity_basis not in (None, 0, 0.0):
        coc = dec(free_cf) / dec(equity_basis)
    else:
        coc = coc_metrics.get("cash_on_cash_year_1_exact", coc_metrics.get("cash_on_cash_year_1"))
    if coc is None:
        raise RuntimeError("Engine did not produce cash_on_cash_year_1 for backsolve case.")
    return results, dec(coc)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backsolve a purchase price to a target post-debt CoC.")
    parser.add_argument("--canonical-json", required=True)
    parser.add_argument("--patch-json")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--year-built", type=int)
    parser.add_argument("--strategy", default="cashflow", choices=["cashflow", "value_add"])
    parser.add_argument("--target-coc", type=Decimal, default=PRIMARY_COC_TARGET_PCT)
    parser.add_argument("--benchmark-5yr-treasury", type=Decimal, required=True)
    parser.add_argument("--agency-spread", type=Decimal, default=DEFAULT_AGENCY_SPREAD_PCT)
    parser.add_argument("--broker-json")
    parser.add_argument("--grouped-comps-json")
    parser.add_argument("--revenue-quality-bridge-json")
    parser.add_argument("--trailing-actuals-json")
    parser.add_argument("--exit-cap-rate", type=Decimal)
    parser.add_argument("--sale-cost-percent", type=Decimal, default=Decimal("0.02"))
    parser.add_argument("--purchase-closing-cost-pct", type=Decimal, default=DEFAULT_PURCHASE_CLOSING_COST_PCT)
    parser.add_argument("--partnership-closing-costs", type=Decimal, default=DEFAULT_PARTNERSHIP_CLOSING_COSTS)
    parser.add_argument("--acquisition-fee-pct", type=Decimal, default=DEFAULT_ACQUISITION_FEE_PCT)
    parser.add_argument("--asset-management-fee-pct", type=Decimal, default=DEFAULT_ASSET_MANAGEMENT_FEE_PCT)
    parser.add_argument("--annual-partnership-expenses", type=Decimal, default=DEFAULT_ANNUAL_PARTNERSHIP_EXPENSES)
    parser.add_argument("--disposition-fee-pct", type=Decimal, default=DEFAULT_DISPOSITION_FEE_PCT)
    parser.add_argument("--loan-closing-costs", type=Decimal, default=Decimal("0"))
    parser.add_argument("--insurance-per-unit-override", type=Decimal)
    parser.add_argument("--min-price", type=Decimal, default=Decimal("1000000"))
    parser.add_argument("--max-price", type=Decimal, default=Decimal("100000000"))
    parser.add_argument("--max-iterations", type=int, default=40)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    canonical = _load_json(Path(args.canonical_json))
    if args.patch_json:
        canonical = _deep_merge(canonical, _load_json(Path(args.patch_json)))
    require_property_tax_policy(canonical)
    broker_snapshot = _load_json(Path(args.broker_json)) if args.broker_json else None
    if broker_snapshot is not None and args.broker_json:
        broker_snapshot, _ = enrich_broker_snapshot_from_om(
            broker_snapshot,
            broker_json_path=Path(args.broker_json),
        )
    grouped_comps = _load_json(Path(args.grouped_comps_json)) if args.grouped_comps_json else None
    revenue_quality_bridge = _load_bridge_if_available(
        args.revenue_quality_bridge_json,
        Path(args.output_dir),
    )
    trailing_actuals = _load_trailing_actuals(
        Path(args.trailing_actuals_json) if args.trailing_actuals_json else None
    )
    costar_exit_cap = None
    costar_exit_cap_summary = None
    if args.exit_cap_rate is None:
        costar_exit_cap, costar_exit_cap_summary = _costar_exit_cap_rate_for_case(
            canonical,
            broker_snapshot,
        )
    selected_exit_cap_rate = (
        args.exit_cap_rate if args.exit_cap_rate is not None else costar_exit_cap
    )

    prepared, policy_summary = _prepare_house_assumptions(
        canonical,
        year_built=args.year_built,
        strategy=args.strategy,
        target_coc=args.target_coc,
        benchmark_treasury=args.benchmark_5yr_treasury,
        agency_spread=args.agency_spread,
        exit_cap_rate=selected_exit_cap_rate,
        sale_cost_percent=args.sale_cost_percent,
        purchase_closing_cost_pct=args.purchase_closing_cost_pct,
        partnership_closing_costs=args.partnership_closing_costs,
        acquisition_fee_pct=args.acquisition_fee_pct,
        asset_management_fee_pct=args.asset_management_fee_pct,
        annual_partnership_expenses=args.annual_partnership_expenses,
        disposition_fee_pct=args.disposition_fee_pct,
        loan_closing_costs=args.loan_closing_costs,
        insurance_per_unit_override=args.insurance_per_unit_override,
        broker_snapshot=broker_snapshot,
        grouped_comps=grouped_comps,
        revenue_quality_bridge=revenue_quality_bridge,
        trailing_actuals=trailing_actuals,
    )
    if costar_exit_cap_summary is not None:
        policy_summary["exit_cap_rate_source"] = costar_exit_cap_summary
    target = dec(args.target_coc)
    projected_noi = _preview_projected_noi(prepared)

    lo = normalize_property_tax_purchase_price(args.min_price)
    hi = normalize_property_tax_purchase_price(args.max_price)
    lo_case = _build_price_case(
        prepared,
        price=lo,
        target_coc=target,
        year_built=args.year_built,
        benchmark_treasury=args.benchmark_5yr_treasury,
        agency_spread=args.agency_spread,
        purchase_closing_cost_pct=args.purchase_closing_cost_pct,
        partnership_closing_costs=args.partnership_closing_costs,
        acquisition_fee_pct=args.acquisition_fee_pct,
        loan_closing_costs=args.loan_closing_costs,
        projected_noi=projected_noi,
    )
    hi_case = _build_price_case(
        prepared,
        price=hi,
        target_coc=target,
        year_built=args.year_built,
        benchmark_treasury=args.benchmark_5yr_treasury,
        agency_spread=args.agency_spread,
        purchase_closing_cost_pct=args.purchase_closing_cost_pct,
        partnership_closing_costs=args.partnership_closing_costs,
        acquisition_fee_pct=args.acquisition_fee_pct,
        loan_closing_costs=args.loan_closing_costs,
        projected_noi=projected_noi,
    )
    lo_results, lo_coc = _evaluate_case(lo_case)
    hi_results, hi_coc = _evaluate_case(hi_case)
    if not (lo_coc >= target and hi_coc <= target):
        raise RuntimeError(
            f"Price bracket does not contain target CoC. "
            f"low={float(lo)} -> {float(lo_coc):.4f}, high={float(hi)} -> {float(hi_coc):.4f}"
        )

    search_ceiling_reached = hi_coc == target
    if search_ceiling_reached:
        solved_case = hi_case
        solved_results = hi_results
        solved_coc = hi_coc
        infeasible_case = None
        infeasible_coc = None
    else:
        solved_case = lo_case
        solved_results = lo_results
        solved_coc = lo_coc
        infeasible_case = hi_case
        infeasible_coc = hi_coc
    iterations_run = 0
    iterations_requested = 0 if search_ceiling_reached else args.max_iterations
    for iteration in range(iterations_requested):
        iterations_run = iteration + 1
        mid = ((lo + hi) / Decimal("2")).quantize(
            _PRICE_CENT,
            rounding=ROUND_HALF_UP,
        )
        mid = normalize_property_tax_purchase_price(mid)
        case = _build_price_case(
            prepared,
            price=mid,
            target_coc=target,
            year_built=args.year_built,
            benchmark_treasury=args.benchmark_5yr_treasury,
            agency_spread=args.agency_spread,
            purchase_closing_cost_pct=args.purchase_closing_cost_pct,
            partnership_closing_costs=args.partnership_closing_costs,
            acquisition_fee_pct=args.acquisition_fee_pct,
            loan_closing_costs=args.loan_closing_costs,
            projected_noi=projected_noi,
        )
        results, coc = _evaluate_case(case)
        if coc >= target:
            solved_case = case
            solved_results = results
            solved_coc = coc
            lo = mid
        else:
            hi = mid
            infeasible_case = case
            infeasible_coc = coc

    if solved_case is None or solved_results is None or solved_coc is None:
        raise RuntimeError("Backsolve did not produce a solved case.")

    output_dir = Path(args.output_dir)
    solved_case_path = output_dir / "canonical_backsolved_target_coc.json"
    summary_path = output_dir / "backsolve_summary.json"
    underwriting_path = output_dir / "underwriting_backsolved_target_coc.json"
    solved_by_year = solved_results["cashflow"]["by_year"]
    solved_projected_noi = dec(
        solved_by_year[1]["net_operating_income"]
        if len(solved_by_year) >= 2
        else solved_by_year[0]["net_operating_income"]
    )

    _write_json(solved_case_path, solved_case)
    _write_json(underwriting_path, solved_results)
    _write_json(
        summary_path,
        {
            "target_cash_on_cash_pct": float(target),
            "solved_purchase_price": solved_case["purchase_assumptions"]["purchase_price"],
            "solved_total_equity_basis": solved_case["purchase_assumptions"]["total_equity_basis"],
            "loan_amount": solved_case["debt_terms"]["commitment"],
            "interest_rate": solved_case["debt_terms"]["rate"],
            "io_months": solved_case["debt_terms"]["io_months"],
            "term_months": solved_case["debt_terms"]["term_months"],
            "projected_noi_used_for_sizing": float(solved_projected_noi),
            "property_tax_calculation": (
                ((solved_case.get("metadata") or {}).get("property_summary") or {}).get(
                    "property_tax_calculation"
                )
            ),
            "cash_on_cash_year_1": solved_results["metrics"]["coc"]["cash_on_cash_year_1"],
            "cash_on_cash_year_1_exact": solved_results["metrics"]["coc"].get(
                "cash_on_cash_year_1_exact"
            ),
            "cash_on_cash_year_1_persisted": float(solved_coc),
            "cash_on_cash_hurdle_margin": float(solved_coc - target),
            "maximum_feasible_boundary": {
                "highest_feasible_price": solved_case["purchase_assumptions"]["purchase_price"],
                "highest_feasible_coc": float(solved_coc),
                "next_infeasible_price": (
                    infeasible_case["purchase_assumptions"]["purchase_price"]
                    if infeasible_case is not None
                    else None
                ),
                "next_infeasible_coc": (
                    float(infeasible_coc) if infeasible_coc is not None else None
                ),
                "price_gap": (
                    float(
                        dec(infeasible_case["purchase_assumptions"]["purchase_price"])
                        - dec(solved_case["purchase_assumptions"]["purchase_price"])
                    )
                    if infeasible_case is not None
                    else None
                ),
                "search_ceiling_reached": search_ceiling_reached,
                "iterations": iterations_run,
                "evaluation_basis": "persisted_year1_free_cf_divided_by_persisted_total_equity",
            },
            "free_cf_year_1": solved_results["metrics"]["coc"]["free_cf_year_1"],
            "levered_irr": solved_results["metrics"]["irr"]["levered_irr"],
            "minimum_dscr": solved_results["metrics"]["dscr"]["minimum_dscr"],
            "going_in_cap_rate": solved_results["metrics"]["yields"]["going_in_cap_rate"],
            "exit_cap_rate": solved_results["metrics"]["yields"]["exit_cap_rate"],
            "policy_summary": policy_summary,
            "artifacts": {
                "canonical": str(solved_case_path),
                "underwriting": str(underwriting_path),
            },
        },
    )


if __name__ == "__main__":
    main()
