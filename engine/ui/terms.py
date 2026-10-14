"""Plain-language definitions the screens show as tooltips on underwriting jargon.

These are the short forms of "Reading results", "Capex: the three buckets"
and "Pricing" in ``docs/USER_GUIDE.md``; the assumption keys themselves are
explained per row by the register (``why``) and in
``docs/ASSUMPTION_GLOSSARY.md``.
"""

from __future__ import annotations

TERMS: dict[str, str] = {
    "T12": "Trailing twelve months: the property's operating statement for the last twelve months "
           "(income and expenses, with its own totals).",
    "Rent roll": "The unit-by-unit list of who pays what today: occupancy, in-place rent and market rent.",
    "OM": "Offering memorandum: the broker's marketing package. Optional; it can supply the asking price "
          "and the broker's exit cap.",
    "PCA": "Property condition assessment: an engineer's report of what must be fixed now and what the "
           "building will need each year. It outranks every other capex estimate.",
    "NOI": "Net operating income: income less operating expenses, before capex and debt.",
    "NCF": "Net cash flow: NOI less the capex charge below NOI (the larger of recurring capex and the "
           "replacement reserve).",
    "CoC": "Cash-on-cash: free cash flow after debt service over the total equity invested. Year 1 is the "
           "first twelve analysis months.",
    "IRR": "Internal rate of return over the hold. Unlevered: the property's cash flows and sale with no "
           "debt. Levered: after debt, on your equity.",
    "EM": "Equity multiple: total cash returned to equity divided by the equity invested.",
    "DSCR": "Debt service coverage ratio: cash flow divided by debt service. The headline uses NCF, as "
            "agency lenders size loans; the NOI-based figure is shown beside it.",
    "Cap rate": "NOI divided by value. The going-in cap uses year-1 NOI over the price; the exit cap turns "
                "the forward NOI at sale into a sale price.",
    "Exit cap": "The cap rate the sale is priced at: exit value = forward NOI / exit cap. Without your own "
                "figure the house uses the going-in cap plus a spread.",
    "Forward NOI": "The NOI for the twelve months after the sale. A buyer prices on it, so the exit value "
                   "uses it; the hold's own cash flows stop at the sale.",
    "LTV": "Loan-to-value: the loan as a share of the purchase price (0.65 = 65%).",
    "IO": "Interest-only months: debt service is interest only, which lifts early cash flow.",
    "Amortization": "The years over which the loan principal is repaid after any interest-only period.",
    "Millage": "Property-tax rate in mills: dollars of tax per $1,000 of assessed value. The engine taxes "
               "price x assessment ratio x millage / 1,000.",
    "Assessment ratio": "Assessed value as a share of the purchase price (1.0 when assessed at the price).",
    "Immediate capex": "Bucket 1: one-time capital at close for deferred maintenance or PCA immediate needs. "
                       "Funded with equity (or a lender holdback); never in NOI.",
    "Recurring capex": "Bucket 2: capital a year per unit below NOI, from the vintage band (or a PCA). "
                       "Grows every year.",
    "Replacement reserve": "Bucket 3: the lender-style reserve per unit a year. A floor, not an addition: "
                           "the charge below NOI is the larger of recurring capex and this.",
    "Capex charge": "The yearly charge below NOI: the larger of recurring capex and the replacement reserve, "
                    "never both.",
    "House default": "A stated house rule filled this value because the documents and market data did not. "
                     "Flagged for review: confirm it or edit it.",
    "House floor": "A minimum by vintage. Underwriting keeps your value and warns; pricing raises it to "
                   "the floor and lists it under floors enforced.",
    "Ingest gate": "The checks that the parsed T12 and rent roll tie to their own totals. BLOCKED means a "
                   "parse does not tie, and nothing is guessed to make it tie.",
    "Benchmark": "Today's 5-year US Treasury yield. The loan in pricing is priced at this plus the agency "
                 "spread. The tool never invents it: you supply the rate, its date and its source.",
    "Target CoC": "The cash-on-cash return the price search must meet (house default 8%).",
    "bps": "Basis points: hundredths of a percent (25 bps = 0.25%).",
    "Stabilized": "A deal bought for its current income; priced on year-1 cash-on-cash.",
    "Value-add": "A deal bought to improve; priced on the first stabilized year's cash-on-cash.",
}
