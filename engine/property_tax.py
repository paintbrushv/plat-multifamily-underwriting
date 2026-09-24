from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
import math
from typing import Any, Literal, Mapping

TaxRateUnit = Literal["mills", "decimal_rate", "percentage_points", "per_100"]
_REQUIRED_FIELDS = frozenset(
    {
        "millage_rate_mills",
        "assessment_ratio",
        "source",
        "source_locator",
        "analyst_override",
    }
)
_UNIT_MULTIPLIER: dict[TaxRateUnit, Decimal] = {
    "mills": Decimal("1"),
    "decimal_rate": Decimal("1000"),
    "percentage_points": Decimal("10"),
    "per_100": Decimal("10"),
}
_CENT = Decimal("0.01")
_REPLACEABLE_LABELS = frozenset(
    {
        "real estate taxes",
        "real estate tax",
        "property taxes",
        "property tax",
        "re taxes",
        "re tax",
        "taxes - real estate",
    }
)


class PropertyTaxPolicyError(ValueError):
    def __init__(
        self,
        code: str,
        field: str,
        value: object,
        message: str,
        source_locator: str | None = None,
    ):
        self.code = code
        self.field = field
        self.value = value
        self.source_locator = source_locator
        locator = f"; source_locator={source_locator}" if source_locator else ""
        super().__init__(f"{code}: {field}={value!r}; {message}{locator}")


def _finite_decimal(value: object, *, field: str, code: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise PropertyTaxPolicyError(
            code, field, value, "expected a positive finite decimal"
        )
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise PropertyTaxPolicyError(
            code, field, value, "expected a unit-free decimal"
        ) from None
    if not parsed.is_finite() or parsed <= 0:
        raise PropertyTaxPolicyError(
            code, field, value, "expected a positive finite decimal"
        )
    return parsed


def property_tax_decimal_to_json_number(
    value: Decimal,
    *,
    field: str,
    code: str = "invalid_property_tax_millage",
) -> float:
    converted = float(value)
    if (
        not value.is_finite()
        or not math.isfinite(converted)
        or Decimal(str(converted)) != value
    ):
        raise PropertyTaxPolicyError(
            code,
            field,
            value,
            "expected a Decimal representable exactly as a finite JSON number",
        )
    return converted


def normalize_property_tax_purchase_price(value: object) -> Decimal:
    price = _finite_decimal(
        value,
        field="purchase_price",
        code="invalid_property_tax_millage",
    )
    property_tax_decimal_to_json_number(
        price,
        field="purchase_price",
    )
    return price


def normalize_millage_rate(value: object, *, unit: TaxRateUnit) -> Decimal:
    parsed = _finite_decimal(
        value,
        field="millage_rate_mills",
        code="invalid_property_tax_millage",
    )
    return parsed * _UNIT_MULTIPLIER[unit]


def build_property_tax_policy(
    *,
    millage_rate: object,
    unit: TaxRateUnit,
    source: str,
    source_locator: str,
    assessment_ratio: object = Decimal("1.00"),
    analyst_override: bool = False,
) -> dict[str, Any]:
    mills = normalize_millage_rate(millage_rate, unit=unit)
    ratio = _finite_decimal(
        assessment_ratio,
        field="assessment_ratio",
        code="unsupported_property_tax_assessment_override",
    )
    if not isinstance(source, str) or not source.strip():
        raise PropertyTaxPolicyError(
            "invalid_property_tax_millage",
            "source",
            source,
            "expected non-empty provenance",
        )
    if not isinstance(source_locator, str) or not source_locator.strip():
        raise PropertyTaxPolicyError(
            "invalid_property_tax_millage",
            "source_locator",
            source_locator,
            "expected reproducible provenance",
        )
    is_override = ratio != Decimal("1.00")
    if not isinstance(analyst_override, bool) or analyst_override is not is_override:
        raise PropertyTaxPolicyError(
            "unsupported_property_tax_assessment_override",
            "analyst_override",
            analyst_override,
            "must be true if and only if assessment_ratio differs from 1.00",
            source_locator,
        )
    return {
        "millage_rate_mills": property_tax_decimal_to_json_number(
            mills,
            field="millage_rate_mills",
        ),
        "assessment_ratio": property_tax_decimal_to_json_number(
            ratio,
            field="assessment_ratio",
            code="unsupported_property_tax_assessment_override",
        ),
        "source": source.strip(),
        "source_locator": source_locator.strip(),
        "analyst_override": analyst_override,
    }


def validate_property_tax_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    if set(policy) != _REQUIRED_FIELDS:
        raise PropertyTaxPolicyError(
            "invalid_property_tax_millage",
            "property_tax_policy",
            sorted(policy),
            f"expected exactly {sorted(_REQUIRED_FIELDS)}",
        )
    return build_property_tax_policy(
        millage_rate=policy["millage_rate_mills"],
        unit="mills",
        assessment_ratio=policy["assessment_ratio"],
        source=policy["source"],
        source_locator=policy["source_locator"],
        analyst_override=policy["analyst_override"],
    )


def require_property_tax_policy(
    canonical: Mapping[str, Any],
) -> dict[str, Any]:
    property_summary = (
        ((canonical.get("metadata") or {}).get("property_summary") or {})
    )
    policy = property_summary.get("property_tax_policy")
    if not isinstance(policy, Mapping):
        raise PropertyTaxPolicyError(
            "missing_property_tax_millage",
            "metadata.property_summary.property_tax_policy.millage_rate_mills",
            None,
            "combined mills per $1,000 are required before valuation",
        )
    return validate_property_tax_policy(policy)


@dataclass(frozen=True)
class PropertyTaxCalculation:
    millage_rate_mills: Decimal
    decimal_tax_rate: Decimal
    assessment_ratio: Decimal
    purchase_price_basis: Decimal
    assessed_value_basis: Decimal
    annual_ad_valorem_tax: Decimal
    source: str
    source_locator: str
    analyst_override: bool

    def to_json(self) -> dict[str, Any]:
        values = asdict(self)
        return {
            key: property_tax_decimal_to_json_number(value, field=key)
            if isinstance(value, Decimal)
            else value
            for key, value in values.items()
        }


def calculate_property_tax(
    *,
    purchase_price: object,
    policy: Mapping[str, Any],
) -> PropertyTaxCalculation:
    canonical = validate_property_tax_policy(policy)
    price = normalize_property_tax_purchase_price(purchase_price)
    mills = Decimal(str(canonical["millage_rate_mills"]))
    ratio = Decimal(str(canonical["assessment_ratio"]))
    decimal_rate = mills / Decimal("1000")
    assessed_basis = price * ratio
    annual_unrounded = assessed_basis * decimal_rate
    try:
        annual = annual_unrounded.quantize(
            _CENT,
            rounding=ROUND_HALF_UP,
        )
    except InvalidOperation:
        raise PropertyTaxPolicyError(
            "invalid_property_tax_millage",
            "annual_ad_valorem_tax",
            annual_unrounded,
            "could not round the calculated tax to cents",
        ) from None
    return PropertyTaxCalculation(
        millage_rate_mills=mills,
        decimal_tax_rate=decimal_rate,
        assessment_ratio=ratio,
        purchase_price_basis=price,
        assessed_value_basis=assessed_basis,
        annual_ad_valorem_tax=annual,
        source=canonical["source"],
        source_locator=canonical["source_locator"],
        analyst_override=canonical["analyst_override"],
    )


def _normalized_label(label: object) -> str:
    return " ".join(str(label or "").split()).casefold()


def apply_property_tax_policy(
    canonical: Mapping[str, Any],
    *,
    purchase_price: object | None = None,
) -> tuple[dict[str, Any], PropertyTaxCalculation]:
    priced = deepcopy(dict(canonical))
    policy = require_property_tax_policy(priced)

    price = purchase_price
    if price is None:
        price = (priced.get("purchase_assumptions") or {}).get("purchase_price")
    calculation = calculate_property_tax(purchase_price=price, policy=policy)

    rows = list(priced.get("opex_table") or [])
    matching = [
        row
        for row in rows
        if _normalized_label(row.get("category_name")) in _REPLACEABLE_LABELS
    ]
    retained = [
        row
        for row in rows
        if _normalized_label(row.get("category_name")) not in _REPLACEABLE_LABELS
    ]
    canonical_row = dict(matching[0]) if matching else {}
    canonical_row.update(
        {
            "category_name": "Real Estate Taxes",
            "calculation_type": "fixed_annual",
            "base_value": float(calculation.annual_ad_valorem_tax),
            "recoverable_flag": False,
        }
    )
    priced["opex_table"] = [*retained, canonical_row]

    metadata = priced.setdefault("metadata", {})
    property_summary = metadata.setdefault("property_summary", {})
    property_summary["property_tax_calculation"] = calculation.to_json()
    return priced, calculation
