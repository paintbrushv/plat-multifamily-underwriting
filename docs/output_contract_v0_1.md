# Output Contract v0.1 (Draft)

This contract defines the **engine output JSON** structures needed to populate RedIQ-style output tabs (Summary, Cash Flow, Sources & Uses, Property CF, Sensitivity Analysis) **without** waterfall logic.

## Artifacts per run
- `inputs.json` (canonical inputs snapshot)
- `outputs.json` (engine outputs; this contract)
- `validation.json` (PASS/FAIL + issues)
- `metadata.json` (timestamp, engine/schema version, git commit)

## Required output tables (engine → PQ → Excel)

### `revenue.monthly`
- `month` (YYYY-MM)
- `net_rent`
- `net_programs`
- `net_total_revenue`

### `cashflow.monthly` (future modules)
- `month` (YYYY-MM)
- `egi` (Effective Gross Income)
- `opex_total`
- `noi`
- `capex_total`
- `debt_service`
- `unleveraged_cash_flow`
- `leveraged_cash_flow`

### `cashflow.annual`
- `year` (YYYY)
- same fields as monthly, aggregated to annual totals

### `sources_uses.at_closing` (future modules)
- purchase price
- closing costs
- initial reserves
- initial equity and debt sources

### `debt.summary` (future modules)
- loan amount, LTV, term, rate, amort/IO
- DSCR by year (or by month) derived from NOI and debt service

### `metrics.summary` (future modules)
- unleveraged IRR, leveraged IRR
- equity multiple
- cash-on-cash (annual series + stabilized)
- cap rates (trailing/forward) and rent multiples (if desired)

## RedIQ tab alignment (label scan)
The following labels were detected on these tabs (used to guide table needs):

### `Summary`
- O6: IRR
- K7: Unleveraged
- K8: Leveraged
- B12: Year Renovated
- M20: Cap Rate
- N20: Rent Mult.
- O20: Cap Rate
- P20: Rent Mult.
- B43: Loan to Value
- B44: Loan Amount
- B48: Loan Term
- B57: Purchase Price

### `Cash Flow`
- E2: Investment Cash Flow
- C5: IRR
- B7: ANNUAL CASH FLOW DURING HOLD PERIOD
- B10: Purchase Price
- B17: OPERATING CASH FLOW
- B25: OPERATING CASH FLOW
- B30: Gross Sales Proceeds
- B32: NET SALES PROCEEDS
- B36: UNLEVERAGED CASH FLOW
- B39: FINANCING CASH FLOW
- B40: Loan Drawdowns
- B41: Loan Closing Costs and Fees
- Q41: NOI:
- B42: Debt Service
- Q43: DSCR
- B44: Loan Repayments
- B45: CASH FLOW FROM LOAN PROCEEDS
- B53: CASH FLOW FROM RESERVES
- B57: LEVERAGED CASH FLOW
- B60: PARTNERSHIP CASH FLOW
- B63: Cash Flow to Partnership
- B70: LEVERAGED METRICS
- B73: Blended DSCR

### `Sources & Uses`
- F2: Sources & Uses
- B5: SOURCES & USES AT CLOSING
- B8: SOURCES
- G8: USES
- G10: Purchase Price
- B15: Assumable Loan
- G16: Loan Closing Costs
- B20: Less:  Loan Proceeds Distributed
- G21: USES BEFORE PARTNERSHIP COSTS
- B29: TOTAL SOURCES AT CLOSING
- G29: TOTAL USES AT CLOSING
- B33: SOURCES & USES OVER HOLD PERIOD
- B36: SOURCES
- G36: USES
- G38: Purchase Price
- B43: Assumable Loan
- G43: Loan Closing Costs
- G44: Debt Service Shortfall
- B47: Less:  Loan Proceeds Used for Refinancing
- B48: Less:  Loan Proceeds Distributed
- G49: Unit Renovation Costs
- B51: Cash Flow
- B52: Funded Through Cash Flow
- G54: Other Uses
- G56: Loan Paydown with Equity
- G58: Total Other Uses
- B68: TOTAL SOURCES
- G68: TOTAL USES

### `Property CF`
- D2: Property Cash Flow Projections
- B7: ANNUAL OPERATING CASH FLOW
- B11: Renovation Premium
- B15: Vacancy
- B16: Renovation Downtime
- B17: Concessions
- B19: Collection Loss / Bad Debt
- B60: Unit Renovations
- B69: OPERATING CASH FLOW
- B82: Market Rent (incl. Renovation Premium)
- B94: NOI Yield
- B95: Unleveraged Cash Flow Yield

### `Sensitivity Analysis`
- B4: PURCHASE PRICE SENSITIVITY
- J4: CAP RATE SENSITIVITY
- D6: Purchase Price
- J19: UNLEVERAGED INTERNAL RATE OF RETURN AND EQUITY MULTIPLE
- L21: Exit Cap Rate
- L38: Exit Cap Rate
- C40: Unleveraged IRR
- C44: Unleveraged Yield¹

### `Renovations`
- H2: Renovation Summary
- B5: EXISTING RENOVATED UNITS
- G5: PROJECTED UNIT RENOVATIONS
- B6: Renovated Units
- G6: Units to be Renovated
- L6: Renovations Start
- L7: Renovations End
- B9: Average Renov. Rent
- L9: Renovation Period
- L11: Renovation Downtime
- B15: EXISTING RENOVATED UNIT SUMMARY
- E18: Renovation Status
- M18: Renovation Premium
- E19: Renovated
- G19: Unrenovated
- I19: Renovated
- K19: Unrenovated
- B29: PROJECTED UNIT RENOVATION SUMMARY
- I30: Post-Renovation
- E31: Renovation Costs¹
- G31: Renov. Premium
- L31: Units To Be Renovated
- B46: Avg. Renovation Premium
- B49: Renovation ROI¹
- B50: Renovation IRR²
- B51: Post-Renovation C-o-C³
- B53: Leveraged Deal IRR
- B56: Avg. Renovation Cost
- B59: Renovation ROI¹
- B60: Renovation IRR²
- B61: Post-Renovation C-o-C³
- B63: Leveraged Deal IRR
- B65: ¹ Does not account for renovation downtime, if any
- B66: ² Includes rent premiums from renovations, downtime vacancy, and increase in residual value due to renovation

## Explicit exclusions (v0.1)
- Waterfall outputs (promotes, splits, IRR hurdles) are out of scope for engine v0.1.
- Fee-like income (late fees, NSF, fines) is not modeled by default.

