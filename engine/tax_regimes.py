"""Deterministic researched tax-regime schedules (Task 4.2, engine side).

Implements the four mandatory state tax regimes researched in Task 4.1
(harness ``docs/TAX_REGIME_SPEC.md``, contract ``tax-regime-research/1.0.0``)
as Decimal-based, versioned engine schedules. The engine owns every unit,
rounding and arithmetic decision; the harness selects and validates an
approved regime package and never recalculates tax.

Statutory basis (retrieved 2026-09-21 from the cited public hosts; re-research
at implementation time before relying on any figure — statutes drift):

- TX  — Jan-1 market-value appraisal at 100 percent of appraised value with
  no assessment ratio (Tex. Tax Code §§ 23.01, 26.02); the levy is the sum of
  overlapping taxing units' adopted per-$100 rates (§§ 26.04, 26.09); the
  § 23.231 20-percent non-homestead circuit breaker is threshold-gated and
  EXPIRES December 31, 2026, so post-2026 applicability is uncertainty-blocked.
  A purchase price is evidence of market value, never a statutory percentage
  reset (§ 23.013).
- CA  — Prop 13 acquisition-value system: base year value at full cash value
  on a change in ownership (Cal. Const. art. XIII A § 2(a)), adjusted annually
  by the BOE-published CPI factor never exceeding 2 percent (§ 2(b) — the
  factor is per-year parcel input, never a hardcoded constant); 1 percent
  constitutional rate cap plus voter-approved additions (§ 1(a)); R&TC § 75.11
  supplemental assessment counts; R&TC § 75.41 month-following proration.
- FL  — 10 percent nonhomestead assessment cap for all levies other than
  school district levies (Fla. Const. art. VII § 4(g),(h)); 10+-unit
  multifamily is Fla. Stat. § 193.1555 (9-or-fewer is § 193.1554 and is out
  of scope for this regime); millage per $1,000 of taxable value (§ 200.065);
  change of ownership or control resets assessment to just value.
- AL  — Class II at 20 percent of fair and reasonable market value
  (Ala. Code § 40-8-1(a)); state 6.5 mills plus jurisdiction-specific county
  millage (ADOR-published schedules); mills are per $1,000 of ASSESSED value.

Research is research, not law: every result reports
``requires_competent_human_review=True`` and carries its citations. Unknown
jurisdictions refuse — there is no "Standard" fallback state. Recorded
uncertainty blocks the rule family instead of shipping a plausible default.
This module is a standalone schedule builder; it does not alter the existing
``engine.property_tax`` millage policy or any engine run behavior.
"""
from __future__ import annotations

import math
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping

CONTRACT_VERSION = "engine-tax-regimes/1.0.0"

RETRIEVAL_DATE = "2026-09-21"

_CENT = Decimal("0.01")
_ZERO = Decimal("0")

# Refusal codes (module-local, closed set; mirrors the harness research gate).
UNSUPPORTED_REGIME = "UNSUPPORTED_TAX_REGIME"
UNSUPPORTED_RULE_FAMILY = "UNSUPPORTED_TAX_RULE_FAMILY"
UNCERTAINTY_BLOCK = "UNCERTAINTY_BLOCKS_STATUTORY_CLAIM"
MISSING_EFFECTIVE_DATE = "MISSING_EFFECTIVE_DATE"
INVALID_VALUE = "INVALID_STATUTORY_VALUE"
LOCAL_LEVY_INCOMPATIBLE = "LOCAL_LEVY_INCOMPATIBLE"

# § 23.231(k): the Texas circuit breaker expires December 31, 2026. Any tax
# year after 2026 has unresearched applicability and is uncertainty-blocked.
_TX_CIRCUIT_BREAKER_LAST_TAX_YEAR = 2026
_TX_CIRCUIT_BREAKER_CAP = Decimal("0.20")

# Cal. Const. art. XIII A: 1 percent maximum ad valorem rate (§ 1(a)); the
# annual base year value adjustment is the CPI factor, never over 2 percent
# (§ 2(b)).
_CA_ONE_PERCENT_CAP = Decimal("0.01")
_CA_TWO_PERCENT_LIMIT = Decimal("0.02")

# Fla. Const. art. VII § 4(h) / Fla. Stat. § 193.1555: 10 percent cap on
# assessment changes for all levies other than school district levies;
# multifamily applicability is 10-or-more units.
_FL_CAP = Decimal("0.10")
_FL_MIN_UNITS = 10

# Ala. Code § 40-8-1(a): Class II is 20 percent of fair and reasonable market
# value; the state levy is 6.5 mills (ADOR) and county millage is
# jurisdiction-specific input with no universal fallback.
_AL_CLASS_II_RATIO = Decimal("0.20")
_AL_STATE_MINIMUM_MILLS = Decimal("6.5")

# Unit-of-measure words that must never appear as a taxing-unit NAME: a unit
# named "mills" betrays a per-$100/per-$1,000 unit confusion.
_LEVY_UNIT_WORDS = frozenset(
    {"mills", "mill", "per_100", "per_1000", "decimal_rate", "percentage_points", "%", "$"}
)

_NO_FALLBACK = (
    "Refused: no researched applicability for this jurisdiction, and no "
    "unexplained universal fallback exists."
)


class TaxRegimeError(ValueError):
    """A typed refusal from a tax-regime schedule build.

    Never carries resident/deal data; ``value`` is the offending input only.
    """

    def __init__(self, code: str, field: str, value: object, message: str):
        self.code = code
        self.field = field
        self.value = value
        self.message = message
        super().__init__(f"{code}: {field}={value!r}; {message}")


def supported_regimes() -> tuple[str, ...]:
    """The researched regimes, sorted. Unknown states refuse elsewhere."""
    return ("AL", "CA", "FL", "TX")


# ---------------------------------------------------------------------------
# Primitive validation — the engine owns units, so every statutory input is a
# finite positive Decimal that is exactly representable as a JSON number.
# ---------------------------------------------------------------------------


def _decimal(
    value: object,
    *,
    field: str,
    positive: bool = True,
    allow_zero: bool = False,
) -> Decimal:
    if value is None or isinstance(value, bool):
        raise TaxRegimeError(INVALID_VALUE, field, value, "expected a finite decimal value")
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise TaxRegimeError(
            INVALID_VALUE, field, value, "expected a unit-free decimal"
        ) from None
    if not parsed.is_finite():
        raise TaxRegimeError(INVALID_VALUE, field, value, "expected a finite decimal value")
    if positive and (parsed < 0 or (parsed == 0 and not allow_zero)):
        raise TaxRegimeError(
            INVALID_VALUE, field, value, "expected a positive value; missing is never zero"
        )
    _json_number(parsed, field=field)
    return parsed


def _nonnegative(value: object, *, field: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise TaxRegimeError(INVALID_VALUE, field, value, "expected a finite decimal value")
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise TaxRegimeError(
            INVALID_VALUE, field, value, "expected a unit-free decimal"
        ) from None
    if not parsed.is_finite() or parsed < 0:
        raise TaxRegimeError(INVALID_VALUE, field, value, "expected a nonnegative finite value")
    _json_number(parsed, field=field)
    return parsed


def _json_number(value: Decimal, *, field: str) -> float:
    converted = float(value)
    if (
        not value.is_finite()
        or not math.isfinite(converted)
        or Decimal(str(converted)) != value
    ):
        raise TaxRegimeError(
            INVALID_VALUE,
            field,
            value,
            "expected a Decimal representable exactly as a finite JSON number",
        )
    return converted


def _round_cents(value: Decimal, *, field: str) -> Decimal:
    try:
        return value.quantize(_CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        raise TaxRegimeError(
            INVALID_VALUE, field, value, "could not round to cents without overflow"
        ) from None


def _tax_year(inputs: Mapping[str, Any]) -> int:
    year = inputs.get("tax_year")
    if year is None:
        raise TaxRegimeError(
            MISSING_EFFECTIVE_DATE,
            "tax_year",
            None,
            "the effective tax year is required before any statutory schedule",
        )
    if isinstance(year, bool) or not isinstance(year, int):
        raise TaxRegimeError(INVALID_VALUE, "tax_year", year, "expected an integer tax year")
    return year


def _year_key(value: object) -> int:
    if isinstance(value, bool):
        raise TaxRegimeError(INVALID_VALUE, "year", value, "expected a calendar year")
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise TaxRegimeError(INVALID_VALUE, "year", value, "expected a calendar year") from None


def _calendar_date(value: object, *, field: str) -> date:
    if not isinstance(value, str):
        raise TaxRegimeError(INVALID_VALUE, field, value, "expected a YYYY-MM-DD date")
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise TaxRegimeError(INVALID_VALUE, field, value, "expected a YYYY-MM-DD date") from None


# ---------------------------------------------------------------------------
# Citation records (mirrors the Task 4.1 research registry; public
# authoritative hosts only). Deep-copied into every result.
# ---------------------------------------------------------------------------

_CITATIONS: dict[str, tuple[dict[str, Any], ...]] = {
    "TX": (
        {
            "citation_id": "tx_23.01",
            "url": "https://statutes.capitol.texas.gov/GetStatute.aspx?Code=TX&Value=23.01",
            "locator": "Tex. Tax Code § 23.01(a),(b)",
            "authority_tier": "statute",
            "jurisdiction": "Texas",
            "retrieved": RETRIEVAL_DATE,
            "effective_tax_year": "2026 (statutes current through 89th 2nd C.S. 2025)",
            "note": "Jan-1 market-value appraisal; generally accepted methods; all property-specific evidence considered.",
        },
        {
            "citation_id": "tx_23.013",
            "url": "https://statutes.capitol.texas.gov/GetStatute.aspx?Code=TX&Value=23.013",
            "locator": "Tex. Tax Code § 23.013",
            "authority_tier": "statute",
            "jurisdiction": "Texas",
            "retrieved": RETRIEVAL_DATE,
            "effective_tax_year": "2026",
            "note": "Comparable-sales method; a sale is evidence of market value, not a statutory purchase-price percentage.",
        },
        {
            "citation_id": "tx_23.231",
            "url": "https://statutes.capitol.texas.gov/GetStatute.aspx?Code=TX&Value=23.231",
            "locator": "Tex. Tax Code § 23.231(b),(d),(f),(j),(k)",
            "authority_tier": "statute",
            "jurisdiction": "Texas",
            "retrieved": RETRIEVAL_DATE,
            "effective_tax_year": "2026; section expires Dec 31, 2026",
            "note": "20% non-homestead circuit breaker under a CPI-adjusted threshold; post-2026 applicability uncertain.",
        },
        {
            "citation_id": "tx_26.02",
            "url": "https://statutes.capitol.texas.gov/GetStatute.aspx?Code=TX&Value=26.02",
            "locator": "Tex. Tax Code § 26.02",
            "authority_tier": "statute",
            "jurisdiction": "Texas",
            "retrieved": RETRIEVAL_DATE,
            "effective_tax_year": "2026",
            "note": "Assessment ratios prohibited; property assessed at 100 percent of appraised value.",
        },
        {
            "citation_id": "tx_26.04",
            "url": "https://statutes.capitol.texas.gov/GetStatute.aspx?Code=TX&Value=26.04",
            "locator": "Tex. Tax Code § 26.04(c)",
            "authority_tier": "statute",
            "jurisdiction": "Texas",
            "retrieved": RETRIEVAL_DATE,
            "effective_tax_year": "2026",
            "note": "Per-$100 adopted rate structure across overlapping taxing units.",
        },
        {
            "citation_id": "tx_26.09",
            "url": "https://statutes.capitol.texas.gov/GetStatute.aspx?Code=TX&Value=26.09",
            "locator": "Tex. Tax Code § 26.09(c)",
            "authority_tier": "statute",
            "jurisdiction": "Texas",
            "retrieved": RETRIEVAL_DATE,
            "effective_tax_year": "2026",
            "note": "Tax calculation order (taxable value times rate); the engine owns the arithmetic.",
        },
        {
            "citation_id": "tx_41.44",
            "url": "https://statutes.capitol.texas.gov/GetStatute.aspx?Code=TX&Value=41.44",
            "locator": "Tex. Tax Code § 41.44(a)(1)",
            "authority_tier": "statute",
            "jurisdiction": "Texas",
            "retrieved": RETRIEVAL_DATE,
            "effective_tax_year": "2026",
            "note": "Protest by May 15 or 30 days after notice delivery, whichever is later.",
        },
    ),
    "CA": (
        {
            "citation_id": "ca_xiiia_1",
            "url": "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?article=XIII+A&lawCode=CONS&sectionNum=SECTION+1",
            "locator": "Cal. Const. art. XIII A § 1(a)",
            "authority_tier": "constitution",
            "jurisdiction": "California",
            "retrieved": RETRIEVAL_DATE,
            "effective_tax_year": "2026",
            "note": "1 percent maximum ad valorem rate; voter-approved bonded indebtedness and school levies are additions outside the cap.",
        },
        {
            "citation_id": "ca_xiiia_2",
            "url": "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?article=XIII+A&lawCode=CONS&sectionNum=SEC.+2.",
            "locator": "Cal. Const. art. XIII A § 2(a),(b)",
            "authority_tier": "constitution",
            "jurisdiction": "California",
            "retrieved": RETRIEVAL_DATE,
            "effective_tax_year": "2026",
            "note": "Base year value at full cash value on change in ownership; annual CPI adjustment not exceeding 2 percent.",
        },
        {
            "citation_id": "ca_rtc_64",
            "url": "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?sectionNum=64.&lawCode=RTC",
            "locator": "Cal. Rev. & Tax. Code § 64(a),(c),(d)",
            "authority_tier": "statute",
            "jurisdiction": "California",
            "retrieved": RETRIEVAL_DATE,
            "effective_tax_year": "2026",
            "note": "Entity transfers excluded except >50 percent control changes and original-co-owner cumulative >50 percent transfers.",
        },
        {
            "citation_id": "ca_rtc_75.11",
            "url": "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?sectionNum=75.11&lawCode=RTC",
            "locator": "Cal. Rev. & Tax. Code § 75.11(a),(b)",
            "authority_tier": "statute",
            "jurisdiction": "California",
            "retrieved": RETRIEVAL_DATE,
            "effective_tax_year": "2026",
            "note": "Two supplemental assessments (Jan 1 - May 31 events) vs one (Jun 1 - Dec 31).",
        },
        {
            "citation_id": "ca_rtc_75.41",
            "url": "https://leginfo.legislature.ca.gov/faces/codes_displayText.xhtml?article=5.&chapter=3.5.&division=1.&lawCode=RTC&part=0.5.&title=",
            "locator": "Cal. Rev. & Tax. Code § 75.41(b),(c)",
            "authority_tier": "statute",
            "jurisdiction": "California",
            "retrieved": RETRIEVAL_DATE,
            "effective_tax_year": "2026",
            "note": "Month-following presumption; monthly proration factors for supplemental taxes.",
        },
        {
            "citation_id": "ca_rule_462.180",
            "url": "https://www.boe.ca.gov/proptaxes/pdf/rules/Rule462_180.pdf",
            "locator": "Property Tax Rule 462.180(d)",
            "authority_tier": "administrative_guidance",
            "jurisdiction": "California",
            "retrieved": RETRIEVAL_DATE,
            "effective_tax_year": "2026",
            "note": "Control = >50 percent voting stock or partnership/LLC capital-and-profits interests.",
        },
    ),
    "FL": (
        {
            "citation_id": "fl_const_4g",
            "url": "https://www.flsenate.gov/Laws/Constitution?Article=VII&Section=4",
            "locator": "Fla. Const. art. VII § 4(g)",
            "authority_tier": "constitution",
            "jurisdiction": "Florida",
            "retrieved": RETRIEVAL_DATE,
            "effective_tax_year": "2026",
            "note": "9-or-fewer-unit nonhomestead residential 10 percent cap, non-school levies.",
        },
        {
            "citation_id": "fl_const_4h",
            "url": "https://www.flsenate.gov/Laws/Constitution?Article=VII&Section=4",
            "locator": "Fla. Const. art. VII § 4(h)",
            "authority_tier": "constitution",
            "jurisdiction": "Florida",
            "retrieved": RETRIEVAL_DATE,
            "effective_tax_year": "2026",
            "note": "Other nonhomestead real property: 10 percent cap for all levies other than school district levies.",
        },
        {
            "citation_id": "fl_193.1555",
            "url": "https://www.flsenate.gov/Laws/statutes/2026/193.1555",
            "locator": "Fla. Stat. § 193.1555(2),(3),(5)",
            "authority_tier": "statute",
            "jurisdiction": "Florida",
            "retrieved": RETRIEVAL_DATE,
            "effective_tax_year": "2026",
            "note": "Multifamily 10+ units; just value at qualifying; 10 percent cap; ownership/control change reset including >50 percent entity transfers.",
        },
        {
            "citation_id": "fl_193.1554",
            "url": "https://www.flsenate.gov/Laws/statutes/2026/193.1554",
            "locator": "Fla. Stat. § 193.1554(1),(3),(5)",
            "authority_tier": "statute",
            "jurisdiction": "Florida",
            "retrieved": RETRIEVAL_DATE,
            "effective_tax_year": "2026",
            "note": "9-or-fewer units only — out of scope for this regime; cited to prevent misclassification.",
        },
        {
            "citation_id": "fl_200.065",
            "url": "https://www.leg.state.fl.us/statutes/index.cfm?App_mode=Display_Statute&URL=0200-0299/0200/Sections/0200.065.html",
            "locator": "Fla. Stat. § 200.065(1)",
            "authority_tier": "statute",
            "jurisdiction": "Florida",
            "retrieved": RETRIEVAL_DATE,
            "effective_tax_year": "2026",
            "note": "Millage per $1,000 of taxable value; TRIM rolled-back rate machinery.",
        },
    ),
    "AL": (
        {
            "citation_id": "al_40-8-1",
            "url": "https://alison.legislature.state.al.us/code-of-alabama?section=40-8-1",
            "locator": "Ala. Code § 40-8-1(a) Class II",
            "authority_tier": "statute",
            "jurisdiction": "Alabama",
            "retrieved": RETRIEVAL_DATE,
            "effective_tax_year": "2026",
            "note": "Class II = all property not otherwise classified, assessed at 20 percent of fair and reasonable market value.",
        },
        {
            "citation_id": "al_ador_property",
            "url": "https://www.revenue.alabama.gov/tax-types/property-ad-valorem-tax/",
            "locator": "ADOR guidance",
            "authority_tier": "administrative_guidance",
            "jurisdiction": "Alabama",
            "retrieved": RETRIEVAL_DATE,
            "effective_tax_year": "2026",
            "note": "State 6.5 mills composition; county millages vary by jurisdiction.",
        },
        {
            "citation_id": "al_ador_millage",
            "url": "https://www.revenue.alabama.gov/faqs/what-is-a-mill/",
            "locator": "ADOR guidance",
            "authority_tier": "administrative_guidance",
            "jurisdiction": "Alabama",
            "retrieved": RETRIEVAL_DATE,
            "effective_tax_year": "2026",
            "note": "One mill = $1 per $1,000 of assessed value; Oct 1 due date, Jan 1 delinquent.",
        },
    ),
}


# ---------------------------------------------------------------------------
# Result assembly
# ---------------------------------------------------------------------------


def _base_result(state: str, regime_id: str, tax_year: int) -> dict[str, Any]:
    return {
        "state": state,
        "regime_id": regime_id,
        "contract_version": CONTRACT_VERSION,
        "tax_year": tax_year,
        "requires_competent_human_review": True,
        "citations": deepcopy(_CITATIONS[state]),
        "statute_derived": {},
        "scenario_assumptions": {},
    }


# ---------------------------------------------------------------------------
# TX — market-value regime
# ---------------------------------------------------------------------------


def _tx_schedule(inputs: Mapping[str, Any]) -> dict[str, Any]:
    year = _tax_year(inputs)
    result = _base_result("TX", "tx_ch23_market_value", year)

    appraised = _decimal(
        inputs.get("purchase_price"), field="purchase_price"
    )

    statute: dict[str, Any] = {
        "assessment_ratio": 1.0,  # § 26.02: ratios prohibited
        "purchase_price_treatment": "evidence_not_statutory_reset",  # §§ 23.01, 23.013
        "levy_unit": "dollars_per_100_of_taxable_value",  # §§ 26.04, 26.09
    }
    result["statute_derived"] = statute

    taxable = appraised
    if inputs.get("circuit_breaker"):
        if year > _TX_CIRCUIT_BREAKER_LAST_TAX_YEAR:
            # § 23.231(k) expires Dec 31, 2026: post-2026 applicability is
            # unresearched and blocks rather than shipping a default.
            raise TaxRegimeError(
                UNCERTAINTY_BLOCK,
                "circuit_breaker",
                year,
                "Tex. Tax Code § 23.231 expires December 31, 2026; re-research "
                "the circuit breaker before using it for a later tax year",
            )
        prior = _decimal(
            inputs.get("prior_appraised_value"), field="prior_appraised_value"
        )
        new_improvements = _nonnegative(
            inputs.get("new_improvement_value", "0"), field="new_improvement_value"
        )
        # § 23.231(d): prior appraised value + 20 percent + new improvements.
        cap = prior * (Decimal(1) + _TX_CIRCUIT_BREAKER_CAP) + new_improvements
        capped = min(appraised, cap)
        result["capped_appraised_value"] = _json_number(
            capped, field="capped_appraised_value"
        )
        statute["circuit_breaker_applied"] = capped < appraised
        statute["circuit_breaker_cap_value"] = _json_number(
            cap, field="circuit_breaker_cap_value"
        )
        taxable = capped
    else:
        statute["circuit_breaker_applied"] = False

    # Levy: the sum of overlapping taxing units' adopted per-$100 rates.
    rates = inputs.get("taxing_unit_rates_per_100")
    if not isinstance(rates, list) or not rates:
        raise TaxRegimeError(
            LOCAL_LEVY_INCOMPATIBLE,
            "taxing_unit_rates_per_100",
            rates,
            "expected a non-empty list of per-$100 taxing-unit rates",
        )
    components: list[dict[str, Any]] = []
    seen_units: set[str] = set()
    total_rate = _ZERO
    for entry in rates:
        if not isinstance(entry, Mapping):
            raise TaxRegimeError(
                LOCAL_LEVY_INCOMPATIBLE,
                "taxing_unit_rates_per_100",
                entry,
                "each taxing unit must be a mapping with a unit name and a per-$100 rate",
            )
        unit = entry.get("unit")
        if (
            not isinstance(unit, str)
            or not unit.strip()
            or unit.strip().casefold() in _LEVY_UNIT_WORDS
        ):
            # A unit named "mills" is a unit-of-measure confusion, not a levy.
            raise TaxRegimeError(
                LOCAL_LEVY_INCOMPATIBLE,
                "taxing_unit_rates_per_100[].unit",
                unit,
                "expected a taxing-unit name that is not a unit-of-measure word",
            )
        if unit.strip() in seen_units:
            raise TaxRegimeError(
                LOCAL_LEVY_INCOMPATIBLE,
                "taxing_unit_rates_per_100[].unit",
                unit,
                "duplicate taxing unit would double-count the levy",
            )
        seen_units.add(unit.strip())
        rate = _decimal(entry.get("rate"), field="taxing_unit_rates_per_100[].rate")
        total_rate += rate
        unit_tax = _round_cents(
            taxable * rate / Decimal(100), field="annual_ad_valorem_tax"
        )
        components.append(
            {
                "unit": unit.strip(),
                "rate": _json_number(rate, field="taxing_unit_rates_per_100[].rate"),
                "tax": _json_number(unit_tax, field="levy_component_tax"),
            }
        )

    ad_valorem = _round_cents(
        taxable * total_rate / Decimal(100), field="annual_ad_valorem_tax"
    )
    special_total = _ZERO
    special_rows: list[dict[str, Any]] = []
    for entry in inputs.get("special_assessments") or []:
        if not isinstance(entry, Mapping):
            raise TaxRegimeError(
                INVALID_VALUE,
                "special_assessments",
                entry,
                "each special assessment must be a mapping with label, amount and source_locator",
            )
        label = entry.get("label")
        if not isinstance(label, str) or not label.strip():
            raise TaxRegimeError(
                INVALID_VALUE, "special_assessments[].label", label, "expected a non-empty label"
            )
        amount = _nonnegative(entry.get("amount"), field="special_assessments[].amount")
        locator = entry.get("source_locator")
        if not isinstance(locator, str) or not locator.strip():
            raise TaxRegimeError(
                INVALID_VALUE,
                "special_assessments[].source_locator",
                locator,
                "special assessments require reproducible provenance",
            )
        special_total += amount
        special_rows.append(
            {
                "label": label.strip(),
                "amount": _json_number(amount, field="special_assessments[].amount"),
                "source_locator": locator.strip(),
            }
        )

    result["taxable_value"] = _json_number(taxable, field="taxable_value")
    result["assessment_ratio"] = 1.0
    result["total_rate_per_100"] = _json_number(total_rate, field="total_rate_per_100")
    result["levy_components"] = components
    result["annual_ad_valorem_tax"] = _json_number(
        ad_valorem, field="annual_ad_valorem_tax"
    )
    result["special_assessments"] = special_rows
    result["special_assessment_total"] = _json_number(
        special_total, field="special_assessment_total"
    )
    result["annual_total_tax"] = _json_number(
        ad_valorem + special_total, field="annual_total_tax"
    )
    result["scenario_assumptions"] = {
        "purchase_price": inputs.get("purchase_price"),
        "taxing_unit_rates_per_100": deepcopy(list(rates)),
        "special_assessments": deepcopy(special_rows),
    }
    return result


# ---------------------------------------------------------------------------
# CA — Prop 13 acquisition-value regime
# ---------------------------------------------------------------------------


def _ca_factor_value(
    value: Decimal,
    base_year: int,
    tax_year: int,
    factors: Mapping[Any, Any],
    *,
    field: str,
) -> tuple[Decimal, dict[str, float]]:
    """Factor ``value`` from ``base_year`` to ``tax_year`` using the supplied
    per-year CPI factors, each capped at the 2 percent statutory limit.

    A missing factor for any year in range refuses: the BOE-published factor
    is per-year parcel input, never a hardcoded constant.
    """
    applied: dict[str, float] = {}
    current = value
    for year in range(base_year + 1, tax_year + 1):
        raw = None
        for key in (year, str(year)):
            if key in factors:
                raw = factors[key]
                break
        if raw is None:
            raise TaxRegimeError(
                INVALID_VALUE,
                f"annual_cpi_factors[{year}]",
                None,
                "the BOE CPI factor for this year is required parcel input; "
                "a missing factor is never defaulted",
            )
        factor = _decimal(
            raw, field=f"annual_cpi_factors[{year}]", positive=False
        )
        # art. XIII A § 2(b): the annual adjustment is the CPI factor, not to
        # exceed 2 percent.
        capped = min(factor, _CA_TWO_PERCENT_LIMIT)
        current = current * (Decimal(1) + capped)
        applied[str(year)] = _json_number(capped, field=field)
    return current, applied


def _ca_schedule(inputs: Mapping[str, Any]) -> dict[str, Any]:
    year = _tax_year(inputs)
    result = _base_result("CA", "ca_prop13_base_year", year)

    base_year = inputs.get("base_year")
    if isinstance(base_year, bool) or not isinstance(base_year, int):
        raise TaxRegimeError(
            INVALID_VALUE, "base_year", base_year, "expected an integer base year"
        )
    if year < base_year:
        raise TaxRegimeError(
            INVALID_VALUE,
            "tax_year",
            year,
            "the tax year cannot precede the base year",
        )
    factors = inputs.get("annual_cpi_factors")
    if not isinstance(factors, Mapping):
        raise TaxRegimeError(
            INVALID_VALUE,
            "annual_cpi_factors",
            factors,
            "expected a mapping of calendar year to BOE CPI factor",
        )

    ownership_change = inputs.get("ownership_change")
    statute: dict[str, Any] = {
        "one_percent_cap": "Cal. Const. art. XIII A § 1(a)",
        "two_percent_limit": "Cal. Const. art. XIII A § 2(b)",
    }

    if ownership_change is not None:
        if not isinstance(ownership_change, Mapping):
            raise TaxRegimeError(
                INVALID_VALUE,
                "ownership_change",
                ownership_change,
                "expected a mapping with date and full_cash_value",
            )
        change_date = _calendar_date(
            ownership_change.get("date"), field="ownership_change.date"
        )
        fcv = _decimal(
            ownership_change.get("full_cash_value"), field="ownership_change.full_cash_value"
        )
        if change_date.year > year:
            raise TaxRegimeError(
                INVALID_VALUE,
                "ownership_change.date",
                change_date.isoformat(),
                "a change of ownership cannot occur after the effective tax year",
            )
        # art. XIII A § 2(a): a change in ownership establishes a new base
        # year value at full cash value.
        main_value, applied = _ca_factor_value(
            fcv, change_date.year, year, factors, field="applied_cpi_factors"
        )
        main_base_year = change_date.year
        statute["applied_cpi_factors"] = applied
        statute["base_year_reset"] = change_date.isoformat()
        # R&TC § 75.11: Jan 1 - May 31 events yield two supplemental
        # assessments; Jun 1 - Dec 31 events yield one.
        statute["supplemental_assessment_count"] = 2 if change_date.month <= 5 else 1
        # R&TC § 75.41: supplemental taxes prorate from the first day of the
        # month following the event.
        if change_date.month == 12:
            following = date(change_date.year + 1, 1, 1)
        else:
            following = date(change_date.year, change_date.month + 1, 1)
        statute["month_following_effective_date"] = following.isoformat()
    else:
        base_value = _decimal(inputs.get("base_year_value"), field="base_year_value")
        main_value, applied = _ca_factor_value(
            base_value, base_year, year, factors, field="applied_cpi_factors"
        )
        main_base_year = base_year
        statute["applied_cpi_factors"] = applied
        statute["base_year_reset"] = None

    # New construction carries its own base year value for the constructed
    # portion (art. XIII A § 2(a)); the remainder keeps its base year value.
    construction_value = _ZERO
    construction = inputs.get("new_construction")
    if construction is not None:
        if not isinstance(construction, Mapping):
            raise TaxRegimeError(
                INVALID_VALUE,
                "new_construction",
                construction,
                "expected a mapping with completion_year and value",
            )
        completion_year = construction.get("completion_year")
        if isinstance(completion_year, bool) or not isinstance(completion_year, int):
            raise TaxRegimeError(
                INVALID_VALUE,
                "new_construction.completion_year",
                completion_year,
                "expected an integer completion year",
            )
        portion = _decimal(
            construction.get("value"), field="new_construction.value"
        )
        if completion_year < main_base_year or completion_year > year:
            raise TaxRegimeError(
                INVALID_VALUE,
                "new_construction.completion_year",
                completion_year,
                "the completion year must fall within the base and effective tax years",
            )
        construction_value, construction_applied = _ca_factor_value(
            portion, completion_year, year, factors, field="applied_cpi_factors"
        )
        for factor_year, factor in construction_applied.items():
            statute.setdefault("applied_cpi_factors", {})
            if factor_year not in statute["applied_cpi_factors"]:
                statute["applied_cpi_factors"][factor_year] = factor

    # 1 percent constitutional cap plus voter-approved additions (never a
    # single universal rate, and never above the cap).
    rate = _decimal(inputs.get("ad_valorem_rate"), field="ad_valorem_rate")
    if rate > _CA_ONE_PERCENT_CAP:
        raise TaxRegimeError(
            INVALID_VALUE,
            "ad_valorem_rate",
            inputs.get("ad_valorem_rate"),
            "the constitutional maximum ad valorem rate is 1 percent "
            "(Cal. Const. art. XIII A § 1(a)); additions must be voter-approved",
        )
    additions = _nonnegative(
        inputs.get("voter_approved_additions", "0"),
        field="voter_approved_additions",
    )

    factored = main_value + construction_value
    ad_valorem = _round_cents(
        factored * (rate + additions), field="annual_ad_valorem_tax"
    )

    result["statute_derived"] = statute
    result["factored_base_year_value"] = _json_number(
        factored, field="factored_base_year_value"
    )
    result["ad_valorem_rate"] = _json_number(rate, field="ad_valorem_rate")
    result["voter_approved_additions"] = _json_number(
        additions, field="voter_approved_additions"
    )
    result["annual_ad_valorem_tax"] = _json_number(
        ad_valorem, field="annual_ad_valorem_tax"
    )
    result["annual_total_tax"] = _json_number(
        ad_valorem, field="annual_total_tax"
    )
    result["scenario_assumptions"] = {
        "base_year": inputs.get("base_year"),
        "base_year_value": inputs.get("base_year_value"),
        "annual_cpi_factors": deepcopy(dict(factors)),
        "ad_valorem_rate": inputs.get("ad_valorem_rate"),
        "voter_approved_additions": inputs.get("voter_approved_additions", "0"),
        "ownership_change": deepcopy(
            dict(ownership_change) if isinstance(ownership_change, Mapping) else None
        ),
        "new_construction": deepcopy(
            dict(construction) if isinstance(construction, Mapping) else None
        ),
    }
    return result


# ---------------------------------------------------------------------------
# FL — nonhomestead 10 percent cap, non-school levies only
# ---------------------------------------------------------------------------


def _fl_schedule(inputs: Mapping[str, Any]) -> dict[str, Any]:
    year = _tax_year(inputs)
    result = _base_result("FL", "fl_nonhomestead_10pct_cap", year)

    unit_count = inputs.get("unit_count")
    if unit_count is None:
        # Unit count is a mandatory applicability input (10+ → § 193.1555;
        # 9-or-fewer → § 193.1554).
        raise TaxRegimeError(
            INVALID_VALUE,
            "unit_count",
            None,
            "the unit count routes the statute and is required",
        )
    if isinstance(unit_count, bool) or not isinstance(unit_count, int) or unit_count < 1:
        raise TaxRegimeError(
            INVALID_VALUE, "unit_count", unit_count, "expected a positive integer unit count"
        )
    if unit_count < _FL_MIN_UNITS:
        # § 193.1554 (9-or-fewer) is a different statute, out of scope here.
        raise TaxRegimeError(
            UNSUPPORTED_RULE_FAMILY,
            "unit_count",
            unit_count,
            "Fla. Stat. § 193.1554 (9-or-fewer units) is out of scope for the "
            "§ 193.1555 10-plus-unit regime",
        )

    just_value = _decimal(inputs.get("just_value"), field="just_value")
    reset = inputs.get("ownership_change_or_qualifying_improvement")
    if not isinstance(reset, bool):
        raise TaxRegimeError(
            INVALID_VALUE,
            "ownership_change_or_qualifying_improvement",
            reset,
            "expected an explicit boolean; missing reset status is not a default",
        )

    non_school_mills = _decimal(
        inputs.get("non_school_millage"), field="non_school_millage"
    )
    school_mills = _decimal(inputs.get("school_millage"), field="school_millage")

    statute: dict[str, Any] = {
        "cap": "10 percent of prior assessed value, all levies other than school district levies (Fla. Const. art. VII § 4(h))",
        "unit_count_statute": "Fla. Stat. § 193.1555",
        "ownership_change_reset": reset,
    }
    result["statute_derived"] = statute

    if reset:
        # § 193.1555(5): change of ownership or control resets assessment to
        # just value.
        assessed = just_value
    else:
        prior = _decimal(
            inputs.get("prior_assessed_value"), field="prior_assessed_value"
        )
        capped = prior * (Decimal(1) + _FL_CAP)
        # No assessment may exceed just value.
        assessed = min(capped, just_value)
    result["capped_assessed_value"] = _json_number(
        assessed, field="capped_assessed_value"
    )

    # School district levies apply to just value outside the cap; every other
    # levy applies to the capped assessed value (mills per $1,000).
    non_school_tax = _round_cents(
        assessed * non_school_mills / Decimal(1000), field="annual_ad_valorem_tax"
    )
    school_tax = _round_cents(
        just_value * school_mills / Decimal(1000), field="annual_ad_valorem_tax"
    )
    total = non_school_tax + school_tax

    result["levy_components"] = [
        {
            "basis": "capped_non_school",
            "tax": _json_number(non_school_tax, field="levy_component_tax"),
        },
        {
            "basis": "just_value_school",
            "tax": _json_number(school_tax, field="levy_component_tax"),
        },
    ]
    result["annual_ad_valorem_tax"] = _json_number(
        total, field="annual_ad_valorem_tax"
    )
    result["annual_total_tax"] = _json_number(total, field="annual_total_tax")
    result["scenario_assumptions"] = {
        "unit_count": unit_count,
        "just_value": inputs.get("just_value"),
        "prior_assessed_value": inputs.get("prior_assessed_value"),
        "non_school_millage": inputs.get("non_school_millage"),
        "school_millage": inputs.get("school_millage"),
    }
    return result


# ---------------------------------------------------------------------------
# AL — Class II classified property regime
# ---------------------------------------------------------------------------


def _al_schedule(inputs: Mapping[str, Any]) -> dict[str, Any]:
    year = _tax_year(inputs)
    result = _base_result("AL", "al_class_ii_millage", year)

    jurisdiction = inputs.get("jurisdiction")
    if not isinstance(jurisdiction, str) or not jurisdiction.strip():
        # County millage is jurisdiction-specific; there is no universal
        # fallback jurisdiction.
        raise TaxRegimeError(
            LOCAL_LEVY_INCOMPATIBLE,
            "jurisdiction",
            jurisdiction,
            "a named jurisdiction is required; county millage schedules have "
            "no universal fallback",
        )

    market_value = _decimal(
        inputs.get("fair_market_value"), field="fair_market_value"
    )
    mills = _decimal(inputs.get("total_millage_mills"), field="total_millage_mills")
    if mills < _AL_STATE_MINIMUM_MILLS:
        # The state levy alone is 6.5 mills; a total below it is a unit error.
        raise TaxRegimeError(
            INVALID_VALUE,
            "total_millage_mills",
            inputs.get("total_millage_mills"),
            "total millage below the 6.5 state mills is a unit confusion "
            "(mills are per $1,000 of assessed value)",
        )

    assessed = market_value * _AL_CLASS_II_RATIO
    ad_valorem = _round_cents(
        assessed * mills / Decimal(1000), field="annual_ad_valorem_tax"
    )

    result["statute_derived"] = {
        "class": "II",
        "assessment_ratio": 0.2,
        "ratio_rule": "Ala. Code § 40-8-1(a): Class II at 20 percent of fair and reasonable market value",
        "state_minimum_mills": 6.5,
    }
    result["assessment_ratio"] = _json_number(
        _AL_CLASS_II_RATIO, field="assessment_ratio"
    )
    result["assessed_value"] = _json_number(assessed, field="assessed_value")
    result["annual_ad_valorem_tax"] = _json_number(
        ad_valorem, field="annual_ad_valorem_tax"
    )
    result["annual_total_tax"] = _json_number(
        ad_valorem, field="annual_total_tax"
    )
    result["scenario_assumptions"] = {
        "jurisdiction": jurisdiction.strip(),
        "fair_market_value": inputs.get("fair_market_value"),
        "total_millage_mills": inputs.get("total_millage_mills"),
    }
    return result


_BUILDERS = {
    "TX": _tx_schedule,
    "CA": _ca_schedule,
    "FL": _fl_schedule,
    "AL": _al_schedule,
}


def build_tax_regime_schedule(inputs: Mapping[str, Any]) -> dict[str, Any]:
    """Build the deterministic statutory tax schedule for one regime.

    ``inputs`` is a plain mapping keyed by ``state`` plus the regime's
    scenario inputs. Unknown states refuse with ``UNSUPPORTED_TAX_REGIME``:
    there is no "Standard" fallback jurisdiction. Every result separates
    statute-derived quantities (``statute_derived``) from analyst-supplied
    scenario assumptions (``scenario_assumptions``), carries its public
    citations, and reports ``requires_competent_human_review`` — research is
    research, not law.
    """
    if not isinstance(inputs, Mapping):
        raise TaxRegimeError(
            INVALID_VALUE, "inputs", inputs, "expected a mapping of regime inputs"
        )
    state = inputs.get("state")
    if not isinstance(state, str) or state.upper() not in _BUILDERS:
        raise TaxRegimeError(UNSUPPORTED_REGIME, "state", state, _NO_FALLBACK)
    return _BUILDERS[state.upper()](inputs)