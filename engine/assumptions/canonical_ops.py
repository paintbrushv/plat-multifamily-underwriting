"""Canonical opex and concession helpers shared by backsolve and the assumption layer.

Moved verbatim from ``engine/backsolve.py`` so the resolver can read opex
evidence (annualized by ``calculation_type``) without importing backsolve,
which itself imports the ingest pipeline.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from engine.modules.util import dec


def sum_units(canonical: dict[str, Any]) -> int:
    cohorts = canonical.get("unit_cohorts") or []
    if not cohorts:
        return 0
    return int(sum(dec(row.get("unit_count", 0)) for row in cohorts))


def annual_opex_amount(canonical: dict[str, Any], row: dict[str, Any]) -> Decimal:
    """Annual dollars for an opex row, whatever its calculation_type.

    Percent-of-revenue rows depend on modeled revenue and cannot be compared
    with a dollar floor, so they raise rather than being misread.
    """
    base = dec(row.get("base_value", 0))
    calc = row.get("calculation_type") or "fixed_annual"
    units = Decimal(str(sum_units(canonical)))
    if calc == "fixed_annual":
        return base
    if calc == "fixed_monthly":
        return base * Decimal("12")
    if calc == "per_unit":
        return base * units
    if calc == "per_unit_monthly":
        return base * units * Decimal("12")
    if calc == "per_sqft":
        total_sqft = sum(
            (dec(c.get("sqft") or 0) * dec(c.get("unit_count") or 0) for c in canonical.get("unit_cohorts") or []),
            Decimal("0"),
        )
        return base * total_sqft
    raise ValueError(
        f"opex line {row.get('category_name')!r} uses calculation_type={calc!r}, which house policy "
        "cannot compare with a dollar floor; enter it as an annual dollar amount"
    )


_EXCLUDED_MATCH_CONTEXT = {
    "internet": ("ad", "advertis", "office", "marketing"),
    "insurance": ("health", "group", "worker", "employee", "401"),
}


def matching_opex_rows(canonical: dict[str, Any], name_fragment: str) -> list[dict[str, Any]]:
    """opex rows naming ``name_fragment``, preferring ones outside excluded contexts
    (e.g. property insurance over employee health insurance)."""
    fragment = name_fragment.lower()
    matches = [
        row
        for row in canonical.get("opex_table") or []
        if fragment in str(row.get("category_name", "")).lower()
    ]
    excluded = _EXCLUDED_MATCH_CONTEXT.get(fragment)
    if excluded:
        preferred = [
            row
            for row in matches
            if not any(token in str(row.get("category_name", "")).lower() for token in excluded)
        ]
        matches = preferred or matches
    return matches


def find_annual_opex(canonical: dict[str, Any], name_fragment: str) -> Decimal:
    matches = matching_opex_rows(canonical, name_fragment)
    return annual_opex_amount(canonical, matches[0]) if matches else Decimal("0")


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def opex_bucket_for_policy(label: str) -> str | None:
    lower = label.lower()
    if "insurance" in lower:
        return "insurance"
    if "tax" in lower:
        return "real_estate_taxes"
    if "management fee" in lower or "management services" in lower:
        return "management_fees"
    if _contains_any(lower, ("water", "sewer", "electric", "gas", "trash", "utility")):
        return "utilities"
    if _contains_any(lower, ("repair", "r&m", "r & m")):
        # "Repairs & Maintenance + Turnover" is the analyst's R&M line; the
        # vintage R&M floor must be compared with it, not stacked on top.
        return "repairs_maintenance"
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


def sum_opex_bucket(canonical: dict[str, Any], bucket: str) -> Decimal:
    total = Decimal("0")
    for row in canonical.get("opex_table") or []:
        if opex_bucket_for_policy(str(row.get("category_name", ""))) == bucket:
            total += annual_opex_amount(canonical, row)
    return total


def ensure_year1_concession_schedule(
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


def concession_pct_rent(row: dict[str, Any]) -> Decimal:
    """A concession row as a fraction of rent; unsupported types fail closed."""
    ctype = row.get("concession_type")
    amount = Decimal(str(row.get("amount", 0)))
    if ctype == "pct_rent":
        return amount
    if ctype == "free_months":
        # Schema: free months on a 12-month lease (1 month = 8.33% of rent).
        return amount / Decimal("12")
    raise ValueError(
        f"concession_schedule row {row.get('concession_id')!r} has concession_type={ctype!r}; "
        "house policy supports pct_rent and free_months only. Re-enter it as pct_rent."
    )


def upsert_annual_opex(
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


def upsert_annual_opex_by_match(
    canonical: dict[str, Any],
    *,
    name_fragment: str,
    fallback_category_name: str,
    annual_amount: Decimal,
) -> str:
    canonical.setdefault("opex_table", [])
    matches = matching_opex_rows(canonical, name_fragment)
    if matches:
        row = matches[0]
        row["calculation_type"] = "fixed_annual"
        row["base_value"] = float(annual_amount)
        row["recoverable_flag"] = False
        return str(row["category_name"])
    upsert_annual_opex(
        canonical,
        category_name=fallback_category_name,
        annual_amount=annual_amount,
    )
    return fallback_category_name
