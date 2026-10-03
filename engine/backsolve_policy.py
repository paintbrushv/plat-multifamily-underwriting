"""Explicit assumptions for the public price-search contract.

Selecting this policy version opts into the documented house-policy floors.
The benchmark is always supplied by the caller; no live rate is implied.
"""

from datetime import date
from decimal import Decimal, InvalidOperation

from engine import underwriting_policy as house


POLICY_VERSION = "plat.backsolve-policy/1"
CONTRACT_VERSION = "plat.backsolve/1"
DEFAULTS = {
    "agency_spread": house.DEFAULT_AGENCY_SPREAD_PCT,
    "sale_cost_percent": Decimal("0.02"),
    "purchase_closing_cost_pct": house.DEFAULT_PURCHASE_CLOSING_COST_PCT,
    "partnership_closing_costs": house.DEFAULT_PARTNERSHIP_CLOSING_COSTS,
    "acquisition_fee_pct": house.DEFAULT_ACQUISITION_FEE_PCT,
    "asset_management_fee_pct": house.DEFAULT_ASSET_MANAGEMENT_FEE_PCT,
    "annual_partnership_expenses": house.DEFAULT_ANNUAL_PARTNERSHIP_EXPENSES,
    "disposition_fee_pct": house.DEFAULT_DISPOSITION_FEE_PCT,
    "loan_closing_costs": Decimal("0"),
    "insurance_per_unit_override": None,
}
RATE_FIELDS = frozenset({
    "agency_spread", "sale_cost_percent", "purchase_closing_cost_pct",
    "acquisition_fee_pct", "asset_management_fee_pct", "disposition_fee_pct", "exit_cap_rate",
})


class BacksolveInputError(ValueError):
    code = "INVALID_BACKSOLVE_INPUT"


def decimal_value(value, field, *, positive=False, rate=False, cents=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise BacksolveInputError(f"{field} must be a finite decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise BacksolveInputError(f"{field} must be a finite decimal") from None
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise BacksolveInputError(f"{field} must be finite and {'positive' if positive else 'nonnegative'}")
    if result > Decimal("1000000000000000") or (rate and result >= 1):
        raise BacksolveInputError(f"{field} is outside the supported range")
    if cents and result != result.quantize(Decimal("0.01")):
        raise BacksolveInputError(f"{field} must have at most two decimal places")
    return result


def validate_benchmark(benchmark):
    if not isinstance(benchmark, dict) or set(benchmark) != {"rate", "as_of", "source"}:
        raise BacksolveInputError("benchmark requires exactly rate, as_of, and source")
    rate = decimal_value(benchmark["rate"], "benchmark.rate", rate=True)
    as_of, source = benchmark["as_of"], benchmark["source"]
    if not isinstance(as_of, str):
        raise BacksolveInputError("benchmark.as_of must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(as_of)
    except ValueError:
        raise BacksolveInputError("benchmark.as_of must be YYYY-MM-DD") from None
    if parsed.isoformat() != as_of:
        raise BacksolveInputError("benchmark.as_of must be YYYY-MM-DD")
    if not isinstance(source, str) or not source.strip() or len(source) > 1000:
        raise BacksolveInputError("benchmark.source must identify the supplied rate")
    return {"rate": str(rate), "as_of": as_of, "source": source.strip()}


def resolve_policy(policy, canonical):
    allowed = set(DEFAULTS) | {"version", "strategy", "year_built", "exit_cap_rate"}
    if not isinstance(policy, dict) or policy.get("version") != POLICY_VERSION:
        raise BacksolveInputError(f"policy.version must be {POLICY_VERSION}")
    if set(policy) - allowed:
        raise BacksolveInputError("unknown backsolve policy fields")
    if policy.get("strategy") not in {"cashflow", "value_add"}:
        raise BacksolveInputError("policy.strategy must be cashflow or value_add")
    year = policy.get("year_built")
    if year is not None and (type(year) is not int or not 1800 <= year <= 2200):
        raise BacksolveInputError("policy.year_built must be an integer year or null")
    values = {**DEFAULTS, **{k: v for k, v in policy.items() if k in DEFAULTS}}
    values["exit_cap_rate"] = policy.get("exit_cap_rate", (canonical.get("exit_assumptions") or {}).get("exit_cap_rate"))
    for name, value in values.items():
        if name == "insurance_per_unit_override" and value is None:
            continue
        values[name] = decimal_value(value, f"policy.{name}",
                                     positive=name == "exit_cap_rate", rate=name in RATE_FIELDS,
                                     cents=name not in RATE_FIELDS)
    return {**values, "strategy": policy["strategy"], "year_built": year}


def describe_policy(values):
    return {"version": POLICY_VERSION,
            **{key: str(value) if isinstance(value, Decimal) else value for key, value in values.items()}}
