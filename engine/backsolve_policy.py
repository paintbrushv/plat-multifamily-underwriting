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
# The cash-on-cash basis the price search targets (UPL-62 decision b):
# "year_1" is the Year-1 post-debt CoC; "stabilized_year" is the post-debt CoC
# in the first stabilized analysis year. The default follows the strategy.
COC_BASES = ("year_1", "stabilized_year")
DEFAULT_COC_BASIS = {"cashflow": "year_1", "value_add": "stabilized_year"}
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


def resolve_policy(policy, canonical, analyst_values=None):
    """The policy values backsolve prices with.

    Precedence per field: an explicit policy value, else an analyst value the
    canonical carries (``analyst_values``, keyed like ``DEFAULTS``), else
    ``DEFAULTS``. The exit cap is the policy's, else the canonical's.
    """
    allowed = set(DEFAULTS) | {
        "version", "strategy", "year_built", "exit_cap_rate", "coc_basis", "loan_sizing_noi_basis",
    }
    if not isinstance(policy, dict) or policy.get("version") != POLICY_VERSION:
        raise BacksolveInputError(f"policy.version must be {POLICY_VERSION}")
    if set(policy) - allowed:
        raise BacksolveInputError("unknown backsolve policy fields")
    if policy.get("strategy") not in {"cashflow", "value_add"}:
        raise BacksolveInputError("policy.strategy must be cashflow or value_add")
    year = policy.get("year_built")
    if year is not None and (type(year) is not int or not 1800 <= year <= 2200):
        raise BacksolveInputError("policy.year_built must be an integer year or null")
    coc_basis = policy.get("coc_basis", DEFAULT_COC_BASIS[policy["strategy"]])
    if coc_basis not in COC_BASES:
        raise BacksolveInputError(f"policy.coc_basis must be one of {', '.join(COC_BASES)}")
    # The loan sizing NOI basis (ADR 0003 §3): the policy's, else the canonical's, else the house default.
    sizing_basis = policy.get("loan_sizing_noi_basis", canonical.get("loan_sizing_noi_basis"))
    if sizing_basis is None:
        sizing_basis = house.HOUSE_LOAN_SIZING_NOI_BASIS
    if sizing_basis not in house.LOAN_SIZING_NOI_BASES:
        raise BacksolveInputError(
            f"loan_sizing_noi_basis must be one of {', '.join(house.LOAN_SIZING_NOI_BASES)}")
    analyst = {}
    for name, value in (analyst_values or {}).items():
        if name not in DEFAULTS or name in policy:
            continue
        analyst[name] = decimal_value(value, f"canonical analyst value {name}",
                                      rate=name in RATE_FIELDS, cents=name not in RATE_FIELDS)
    values = {**DEFAULTS, **analyst, **{k: v for k, v in policy.items() if k in DEFAULTS}}
    values["exit_cap_rate"] = policy.get("exit_cap_rate", (canonical.get("exit_assumptions") or {}).get("exit_cap_rate"))
    for name, value in values.items():
        if name == "insurance_per_unit_override" and value is None:
            continue
        values[name] = decimal_value(value, f"policy.{name}",
                                     positive=name == "exit_cap_rate", rate=name in RATE_FIELDS,
                                     cents=name not in RATE_FIELDS)
    return {**values, "strategy": policy["strategy"], "year_built": year, "coc_basis": coc_basis,
            "loan_sizing_noi_basis": sizing_basis}


def loan_sizing_basis_source(policy, canonical):
    """Who set the loan sizing NOI basis backsolve priced with (disclosed in the summary)."""
    if "loan_sizing_noi_basis" in policy:
        return "explicit policy value"
    if canonical.get("loan_sizing_noi_basis") is not None:
        return "canonical (analyst)"
    return "house default (HOUSE_LOAN_SIZING_NOI_BASIS)"


VALUE_SOURCE_POLICY = "explicit_policy"
VALUE_SOURCE_ANALYST = "canonical_analyst"
VALUE_SOURCE_DEFAULT = "policy_default"
VALUE_SOURCE_CANONICAL = "canonical"


def policy_value_sources(policy, analyst_values=None):
    """Who set each priced policy value (disclosed in ``effective_assumptions``)."""
    analyst = analyst_values or {}
    sources = {
        name: VALUE_SOURCE_POLICY if name in policy
        else VALUE_SOURCE_ANALYST if name in analyst
        else VALUE_SOURCE_DEFAULT
        for name in DEFAULTS
    }
    sources["exit_cap_rate"] = VALUE_SOURCE_POLICY if "exit_cap_rate" in policy else VALUE_SOURCE_CANONICAL
    return sources


def describe_policy(values):
    return {"version": POLICY_VERSION,
            **{key: str(value) if isinstance(value, Decimal) else value for key, value in values.items()}}
