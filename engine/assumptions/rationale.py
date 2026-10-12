"""Plain-English policy rationale for each assumption source (ADR 0003 §2, §8).

An assumption entry's ``basis`` says how its value was computed. This module
says why that *kind* of source is where the default comes from: why the house
uses a rule as its default, or why a source sits where it does in the
precedence chain (analyst override > deal evidence > submarket > MSA > house
default > engine fallback, ADR 0003 §3).

``rationale_for(source)`` matches the exact source first, then a source
family prefix (``house:vintage_<bucket>``, ``market:<provider>``). A source
with no recorded rationale gets ``NO_RATIONALE`` and ``known: False``, so a
new source added elsewhere still renders, and ``tests/test_assumption_register.py``
flags it until a rationale is written here.

The texts describe policy, not numbers: the values themselves live in
``engine/underwriting_policy.py`` and ``engine/backsolve_policy.py`` and are
shown on each entry.
"""

from __future__ import annotations

from typing import Any, Optional

NO_RATIONALE = "No policy rationale recorded for this source yet."

# Source families, in precedence order (ADR 0003 §3).
FAMILY_ANALYST = "analyst"
FAMILY_DEAL = "deal"
FAMILY_MARKET = "market"
FAMILY_HOUSE = "house"
FAMILY_ENGINE_FALLBACK = "engine_fallback"
FAMILY_UNKNOWN = "unknown"
FAMILIES = (
    FAMILY_ANALYST,
    FAMILY_DEAL,
    FAMILY_MARKET,
    FAMILY_HOUSE,
    FAMILY_ENGINE_FALLBACK,
    FAMILY_UNKNOWN,
)

# Exact sources.
SOURCE_RATIONALES: dict[str, str] = {
    "analyst": (
        "An analyst value always wins. The person underwriting the deal knows things no document or "
        "data feed does, so the resolver applies the edit as given, keeps the default beside it, and "
        "never lets a house rule silently replace it."
    ),
    "engine_fallback": (
        "Last step in the precedence chain: no deal document, market feed or house rule supplied a "
        "value, so the engine's built-in default (often zero, or not modeled) applies. Treat it as a "
        "placeholder to confirm, not a judgment."
    ),
    "deal:t12": (
        "The trailing twelve months are the property's own recent operating record. Deal evidence "
        "outranks market and house defaults because it describes this asset rather than an average one; "
        "the house floors still check it."
    ),
    "deal:rent_roll": (
        "The rent roll is the current, unit-by-unit picture of occupancy and in-place rents. It is the "
        "most direct evidence for where vacancy and loss to lease start, so it sets the starting point "
        "ahead of market or house figures."
    ),
    "deal:om": (
        "The offering memorandum states the seller's or broker's view of the deal. It is used where it is "
        "the only deal-specific evidence (such as an exit cap) and can raise a house floor, but it is a "
        "marketing document, so treat it with care."
    ),
    "deal:comps": (
        "Rent comparables show what renovated and classic units actually lease for nearby. A comp-based "
        "renovation premium is deal-specific evidence, so it is preferred over a generic market figure."
    ),
    "market:supplied_benchmark": (
        "A market vacancy benchmark the analyst supplied. It can only raise the property-level vacancy "
        "anchor, never lower it, so an outside view of the market makes the underwriting more "
        "conservative and never less."
    ),
    "market:synthetic": (
        "Synthetic market data used by tests and the sample deal library. It follows the same precedence "
        "as a real market feed (submarket, then MSA) but describes no real market."
    ),
    "house:long_run_rent_growth": (
        "Forecasts are only credible for a few years. Beyond the forecast horizon the house glides rent "
        "growth to a long-run rate near long-run inflation, so the exit year does not compound a "
        "short-term spike or slump."
    ),
    "house:default_collection_loss": (
        "With no T12 bad-debt history and no known vintage, the house assumes the most conservative "
        "vintage typical-low collection loss. Unknown credit quality is underwritten as weaker, not "
        "stronger."
    ),
    "house:exit_cap_spread": (
        "Unless the analyst sets one, the house exits at the going-in cap rate at the price being modeled "
        "plus a fixed spread, recomputed whenever the price changes. The asset will be older at sale and "
        "today's pricing may not hold, so the exit is underwritten at a higher cap rate (a lower value) than "
        "the entry. Market cap rates only test for the rare steal, where the going-in cap sits well above "
        "market; then the exit is compressed by an amount that grows with the size of the discount, up to a "
        "cap, and flagged for review."
    ),
    "house:opex_growth_table": (
        "Each expense category grows at a named house rate set at or above long-run inflation, with "
        "faster rates for lines that have historically outpaced it (payroll, insurance, utilities). "
        "Overstating expense growth is the conservative error; a market expense trend replaces a rate "
        "once a provider supplies one."
    ),
    "house:property_tax_module": (
        "A sale usually triggers a reassessment, so the trailing tax bill understates what a buyer will "
        "pay. The property-tax module resets year-1 tax to purchase price × assessment ratio × millage "
        "instead of carrying the seller's bill forward."
    ),
    "house:management_fee_pct_egr": (
        "Every stabilized property pays for management, even when the T12 shows no fee (for example, "
        "owner-managed). The house charges a standard fee as a percent of effective gross revenue so "
        "the fee scales with income."
    ),
    "house:year1_insurance_floor": (
        "Insurance costs have risen sharply and a seller's policy may not transfer. The house underwrites "
        "year-1 insurance at no less than a per-unit minimum; an explicit analyst insurance figure waives "
        "the floor, and the waiver is disclosed."
    ),
    "house:vacancy_anchor": (
        "Property-level vacancy is anchored at the highest of the vintage floor and the available evidence "
        "(rent roll, T12, broker and market benchmarks). Using the highest credible figure keeps vacancy "
        "from being underwritten below what any source supports."
    ),
    "house:backsolve_policy": (
        "Fees, closing costs and sale costs follow the house pricing policy, so every deal is priced on "
        "the same cost basis and bids are comparable. An analyst value for the deal still wins."
    ),
    "house:pricing_normalization": (
        "Before solving for a price, the backsolve restates some inputs on a consistent basis (for "
        "example, rebuilding loss to lease and vacancy from the rent-roll floor plans). The price then "
        "reflects the property, not how the seller's documents happened to be prepared."
    ),
    "house:replacement_reserve": (
        "Lenders require a per-unit replacement reserve below NOI on every loan, whatever the vintage. "
        "The house carries the same lender-style figure as the floor of the annual capital charge: the "
        "charge is the larger of recurring capex and this reserve, never both, so capital is never "
        "underwritten below what a lender would escrow."
    ),
    "deal:pca": (
        "A property condition assessment is an engineer's inspection of this building: what must be fixed "
        "now and what the systems will need each year. It describes the actual asset rather than its "
        "decade, so it outranks an analyst estimate and the house vintage band for both immediate and "
        "recurring capex."
    ),
    "house:recurring_capex_vintage": (
        "Older buildings need more capital every year (roofs, HVAC, plumbing, paving, unit interiors), so "
        "the house budgets recurring capex below NOI from the vintage band: the low end for a stabilized "
        "deal, the midpoint for value-add, the high end when deferred maintenance is flagged. It grows "
        "with repairs and maintenance, and in pricing the band is a floor."
    ),
    "house:deferred_capex_placeholder": (
        "The deal is flagged with deferred maintenance, so there is capital to spend at closing, but no "
        "property condition assessment or analyst budget says how much. Pricing it at zero would ignore a "
        "known cost, so the house budgets one year of the top of the vintage capex band per unit, funded with "
        "equity at close, and flags it. It is a placeholder, not an estimate: commission a PCA (lenders escrow "
        "its immediate repairs at 125% of the estimate) and replace it."
    ),
    "house:r_and_m_payroll_review": (
        "Payroll is payroll: site staff wages are not repairs and maintenance, even when the whole team's time "
        "is booked to payroll, so by default payroll earns no credit toward the R&M floor. Only when the T12 "
        "labels payroll lines as maintenance labor and total payroll per unit is above a normal-staffing "
        "benchmark does the excess count, up to the labeled maintenance payroll. The review is always shown, "
        "and flagged when R&M is short and payroll is high, so the analyst can confirm or override the credit."
    ),
    "house:loan_sizing_noi_basis": (
        "Agency lenders size a loan on what the property earns after its capital needs, not on NOI: they "
        "deduct replacement reserves before testing debt coverage. The house sizes the loan, and reports debt "
        "coverage, on NOI less the same yearly capital charge the cash flow pays (the larger of recurring "
        "capex and the lender reserve), so an older building that needs more capital carries a smaller loan. "
        "The NOI-based coverage is shown beside it, and the analyst can choose another basis."
    ),
    "house:deferred_capex": (
        "Without a property condition assessment or an analyst budget there is no evidence of what must "
        "be fixed at closing, so nothing is budgeted at close and the entry is flagged. Inspect the asset "
        "and enter the immediate needs: they are funded with equity at close and lower returns."
    ),
}

# (prefix, rationale) for source families with a variable suffix. ``{suffix}``
# is replaced with the part of the source after the prefix.
PREFIX_RATIONALES: tuple[tuple[str, str], ...] = (
    (
        "house:vintage_",
        "The house vintage framework sets defaults and floors by decade built ({suffix} for this deal): "
        "older buildings carry higher repairs, reserves, vacancy and credit loss. It is used when the deal "
        "and market data do not supply a figure, and as a floor that flags or (in pricing) raises values "
        "below it.",
    ),
    (
        "market:",
        "A configured market-data feed ({suffix}). Submarket data outranks MSA data, and both rank below "
        "deal evidence and above house defaults: the feed describes the local market rather than a "
        "generic one, but not this specific property.",
    ),
)


def source_family(source: Any) -> str:
    """``analyst``, ``deal``, ``market``, ``house``, ``engine_fallback`` or ``unknown``."""
    if not isinstance(source, str) or not source:
        return FAMILY_UNKNOWN
    if source in (FAMILY_ANALYST, FAMILY_ENGINE_FALLBACK):
        return source
    prefix, sep, _ = source.partition(":")
    if sep and prefix in (FAMILY_DEAL, FAMILY_MARKET, FAMILY_HOUSE):
        return prefix
    return FAMILY_UNKNOWN


def rationale_for(source: Any) -> dict[str, Any]:
    """The policy rationale for a source.

    Returns ``{"source", "family", "text", "known", "matched"}``. ``matched``
    is the exact source or the prefix that supplied the text, and ``None``
    with ``known: False`` when no rationale is recorded (``text`` is then
    ``NO_RATIONALE``).
    """
    family = source_family(source)
    text: Optional[str] = None
    matched: Optional[str] = None
    if isinstance(source, str):
        if source in SOURCE_RATIONALES:
            text, matched = SOURCE_RATIONALES[source], source
        else:
            for prefix, template in PREFIX_RATIONALES:
                suffix = source[len(prefix):]
                if source.startswith(prefix) and suffix:
                    text, matched = template.replace("{suffix}", suffix), prefix
                    break
    return {
        "source": source if isinstance(source, str) else None,
        "family": family,
        "text": text if text is not None else NO_RATIONALE,
        "known": text is not None,
        "matched": matched,
    }


def has_rationale(source: Any) -> bool:
    return bool(rationale_for(source)["known"])
