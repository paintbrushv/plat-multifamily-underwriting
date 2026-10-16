# User guide: new? Start here

This guide is for an analyst who knows multifamily underwriting and has never
used this tool. It covers the whole flow, a ten-minute walkthrough on a
sample deal, and how to read what comes back. You do not need to read any
code.

Contents:

- [What this is](#what-this-is)
- [What you need](#what-you-need)
- [The flow, step by step](#the-flow-step-by-step)
- [Your first 10 minutes: the sample deal](#your-first-10-minutes-the-sample-deal)
- [Reading the ingest gates](#reading-the-ingest-gates)
- [Reading the assumption register](#reading-the-assumption-register)
- [Capex: the three buckets](#capex-the-three-buckets)
- [Reading results](#reading-results)
- [Pricing](#pricing)
- [Market data](#market-data)
- [Connecting your AI assistant](#connecting-your-ai-assistant)
- [Using the screens](#using-the-screens)
- [CLI fallback](#cli-fallback)
- [FAQ and troubleshooting](#faq-and-troubleshooting)

Every assumption key, unit, source and house value is listed in the
[assumption glossary](ASSUMPTION_GLOSSARY.md).

## What this is

A deterministic multifamily underwriting engine. You give it a T12, a rent
roll and a few deal facts. It reads the documents, checks that they tie to
their own totals, fills every assumption it needs with a value and a stated
source, and runs a monthly cash flow: NOI, cash flow after capex and debt,
cash-on-cash, IRRs, DSCR, and an exit. It can also solve for the price that
hits a target cash-on-cash return. The numbers come from the engine, not
from an AI model: the same inputs always give the same outputs. An AI
assistant can drive it for you, but it never makes up a number.

## What you need

Required:

- **T12**: the trailing twelve-month operating statement, Excel, CSV or PDF.
  It needs its own total rows (total income, total operating expenses) so
  the tool can check the parse.
- **Rent roll**: unit-level, Excel or CSV. A property-management export with
  its summary block is best (the tool ties to it).
- **A few deal facts**: deal type (stabilized or value-add), year built,
  location (MSA, metro, submarket), purchase price, financing (LTV, rate,
  amortization, interest-only months), and the property-tax millage and
  assessment ratio.

Optional:

- **PCA** (property condition assessment): immediate repairs and recurring
  capex per unit. Strongly recommended for anything built before 2000.
- **OM** (offering memorandum): the tool can read the asking price and the
  broker's exit cap from it.
- **Value-add programs**: the renovation scope, cost, premium and pace.

The analysis window is a start month and an end month (usually five years).
The T12 should be the twelve months just before the start.

## The flow, step by step

These are the tools your AI assistant uses. The exact parameters of each tool
are in [docs/MCP_TOOLS.md](MCP_TOOLS.md). The [CLI fallback](#cli-fallback)
lists the same steps as commands.

**Screens.** You can also click through the flow instead of typing. In an
assistant that supports MCP Apps, the tools open four screens beside the
chat; with `plat-uw serve` the same screens run in your browser (see
[Using the screens](#using-the-screens)). Each step below says which screen
shows it. Every screen has the step header (1 Documents, 2 Describe, 3 Check,
4 Assumptions, 5 Results, 6 Price, 7 Export), a *New? Start here* panel and
*Export files*.

| Step | Tool | In one line |
|---|---|---|
| 1 | `start_here` | Orientation: what the tool does and what to do next. |
| try first | `load_sample_deal` | A complete fictional deal, ready to ingest. Replaces steps 2 to 4. |
| 2 | `create_deal` | Make an empty deal workspace. |
| 3 | `add_document` | Attach the T12, rent roll and any PCA or OM. |
| 4 | `describe_deal` | Enter the deal facts the documents do not carry. |
| 5 | `ingest_deal` | Read the documents, check the gates, resolve every assumption. |
| 6 | `review_assumptions` / `edit_assumptions` | See every assumption and its source; change the ones you disagree with. |
| 7 | `run_deal` | Run the cash flow and get the returns. |
| 8 | `solve_price` | Find the price that hits your target cash-on-cash. |
| then | `export_deal` | Take the workbook, the register page and the results with you. |

### Step 1: `start_here`

- **What it does:** tells the assistant (and you) how the tool works and which
  step comes next.
- **You provide:** nothing.
- **You get back:** a short orientation and the list of steps.
- **Good looks like:** the assistant offers to load the sample deal or to
  start a new one.
- **Screen:** *New? Start here* on every screen; in the browser app, the home
  page and the user guide.
- **Common problems:** the assistant does not see the tool at all. The
  connection is not set up; see [Connecting your AI assistant](#connecting-your-ai-assistant).

### Try first: `load_sample_deal`

- **What it does:** creates a deal from the bundled sample, Sample Garden
  Apartments (synthetic): a fictional 128-unit, 1985 garden community with a
  T12, a rent roll and its deal facts already filled in.
- **You provide:** nothing.
- **You get back:** a deal that is ready for step 5.
- **Good looks like:** you can go straight to `ingest_deal` and follow
  [Your first 10 minutes](#your-first-10-minutes-the-sample-deal).
- **Screen:** *Deal intake*, with the documents and deal facts filled in (the
  browser app's *Try the sample deal* button).
- **Common problems:** none expected. Every name and number in the sample is
  invented; do not read anything into them.

### Step 2: `create_deal`

- **What it does:** makes an empty workspace for one deal.
- **You provide:** a name or ID for the deal.
- **You get back:** the deal's ID, used by every later step.
- **Good looks like:** one deal per property and per version of the
  underwriting you want to keep.
- **Common problems:** reusing a name you already used. Pick a new one.
- **Screen:** *Deal intake* (the browser app's *New deal* button).

### Step 3: `add_document`

- **What it does:** attaches a document to the deal and records what kind it
  is.
- **You provide:** the file and its kind: T12, rent roll, PCA or OM.
- **You get back:** confirmation that the file is attached.
- **Good looks like:** one T12 and one rent roll. Add the PCA if you have one.
- **Common problems:**
  - A workbook with many tabs. The tool picks the statement tab or the
    rent-roll tab itself; if it picks the wrong one, save the right tab as its
    own file.
  - A scanned PDF T12 with no text layer may not be readable. Ask for the
    Excel export.
- **Screen:** *Deal intake*, Documents: drop each file on its box (T12, rent
  roll, offering memo, condition report).

### Step 4: `describe_deal`

- **What it does:** records the deal facts that are not in the documents.
- **You provide:**
  - analysis start and end month;
  - deal type (`stabilized` or `value_add`) and year built;
  - location: MSA name, metro slug and submarket (and optionally star tier and
    asset class);
  - purchase price; financing: LTV, interest rate, amortization years,
    interest-only months;
  - property tax: the combined millage (mills per $1,000 of assessed value)
    and the assessment ratio (assessed value / purchase price);
  - optional: exit cap, exit month, sale cost, deferred maintenance flag,
    immediate capex, recurring capex per unit, PCA figures, fund terms.
- **You get back:** the facts as recorded, and what is still missing.
- **Good looks like:** nothing required is missing. Rates are decimals:
  `0.0575` is 5.75%.
- **Common problems:**
  - No millage with a purchase price. The deal fails validation with
    `PROPERTY_TAX_MILLAGE_MISSING`. The engine re-taxes the property at
    purchase price x assessment ratio x millage / 1,000, so it needs both.
  - No exit cap. That is fine: the house fills one and flags it for review.
  - A real metro with no market data configured. That is fine too; see
    [Market data](#market-data).
- **Screen:** *Deal intake*, Deal facts: grouped as Property, Location,
  Price & financing, Taxes, Capex & condition and Hold, each field with why it
  matters and whether it is needed to check the documents or to run. Rates
  are typed as percents there (5.75, not 0.0575).

### Step 5: `ingest_deal`

- **What it does:** reads the T12 and rent roll, ties each to its own totals
  (the ingest gates), and resolves every assumption with a value, a source
  and a reason.
- **You provide:** nothing new (the deal from steps 2 to 4).
- **You get back:** the gate results (PASS or BLOCKED), the unit count and
  floor plans, the opex categories, the three capex buckets, the number of
  assumptions flagged for review, any guardrail warnings, and the market
  data status.
- **Good looks like:** `Ingest gate: PASS` and validation PASS. Warnings are
  normal: each one is something to review, not an error.
- **Common problems:** a BLOCKED gate. See
  [Reading the ingest gates](#reading-the-ingest-gates).
- **Screen:** *Check documents* on the intake screen; the result opens *Check
  and assumptions*: the gate first (each blocker with its fix), then market
  data and the capital plan.

### Step 6: `review_assumptions` / `edit_assumptions`

- **What it does:** `review_assumptions` shows the assumption register: every
  assumption, its value, its source, why that is the default, and its flags.
  `edit_assumptions` applies your changes and re-resolves.
- **You provide:** for an edit, the assumption key, the new value and a short
  note (why you changed it). Keys are in the
  [glossary](ASSUMPTION_GLOSSARY.md).
- **You get back:** the updated register, with your edit highlighted and its
  change from the default.
- **Good looks like:** you have looked at every row marked *Needs review* and
  every guardrail flag, and either accepted the default or edited it with a
  note.
- **Common problems:**
  - An edit below a house floor. It is kept when underwriting (with a
    warning) but raised to the floor when pricing. See
    [floors](#floors-underwrite-warns-pricing-enforces).
  - Wanting to undo an edit. Clear it (set it to empty or `null`) and the row
    returns to its full default.
- **Screen:** *Check and assumptions*, the register: chips for *Needs review*,
  *Edited*, *Defaults* and *Enforced in pricing*; *Why this default* on each
  row; *Edit* to change a value with a note (*Back to default* clears it). The
  screen asks your name once per session and shows each change against the
  default.

### Step 7: `run_deal`

- **What it does:** validates the deal and runs the monthly cash flow.
- **You provide:** nothing new.
- **You get back:** year-1 NOI, unlevered and levered IRR, equity multiples,
  minimum DSCR on both bases, exit value and exit cap, cash-on-cash, and the
  exit-cap grid.
- **Good looks like:** validation PASS and numbers you can explain. See
  [Reading results](#reading-results).
- **Common problems:** the run refuses because of a validation ERROR (a
  BLOCKED gate, missing millage, an unresolved exit cap placeholder). The
  error names what to fix.
- **Screen:** *Results*: headline tiles, the cash flow by year (chart and
  table), the capital plan by year with the bucket that binds, the exit on
  forward NOI and the exit-cap grid (chart and table).

### Step 8: `solve_price`

- **What it does:** finds the highest price that still hits your target
  cash-on-cash, under the house pricing policy.
- **You provide:** the target cash-on-cash (house default 8%), the 5-year
  treasury yield as of a date and where you got it. The loan rate is that
  treasury plus the agency spread.
- **You get back:** the status, the solved price and price per unit, the loan
  and how it was sized, equity, going-in and exit cap, IRR, minimum DSCR, the
  exit-cap grid, and every house floor the pricing enforced.
- **Good looks like:** status `converged` and an achieved cash-on-cash equal
  to the target.
- **Common problems:** status `infeasible`. See [Pricing](#pricing).
- **Screen:** *Pricing*: the target and the treasury (with its date and
  source), *Solve the price*, the pricing panel with the floors enforced
  (each opens its register row) and the asking price against the solved
  price.

### Then: `export_deal`

- **What it does:** packages the outputs.
- **You provide:** nothing (optionally which outputs you want).
- **You get back:** the underwriting workbook (with its Assumptions tab), the
  assumption register page (`assumptions.html`, one self-contained file that
  works offline and prints cleanly), the results, and the pricing summary if
  you priced the deal. [docs/MCP_TOOLS.md](MCP_TOOLS.md) has the exact list.
- **Good looks like:** the register page opens in any browser and matches what
  you reviewed.
- **Screen:** *Export files* at the top of every screen.

## Your first 10 minutes: the sample deal

Load the sample (`load_sample_deal`), then ingest, run and price it. Every
figure below is what the sample actually produces. The 5-year treasury used
for pricing is **4.00%, an illustrative value, not a market quote**. Sample
Garden Apartments is fictional: the property, the city of Quillmere and every
amount are invented.

**The deal.** 128 units built in 1985, underwritten as stabilized. Asking
price $14,280,000 ($111,563 per unit). Loan at 65% LTV ($9,282,000) at 5.75%,
30-year amortization, 24 months interest-only. Millage 21 at an 80%
assessment ratio. Five-year hold, November 2026 to October 2031. The analyst
budgeted $192,000 of immediate capex at close for two roofs. There is no PCA.

**1. Ingest.** The gates pass:

```text
T12 opex: PASS parsed $843,206 vs statement $843,206
T12 income: PASS parsed $1,795,075 vs statement $1,795,075
Rent roll units: PASS parsed 128 vs export 128
Rent roll vacant units: PASS parsed 7 vs export 7
Rent roll market rent: PASS parsed $159,600/mo vs export $159,600/mo
Rent roll rent charges: PASS parsed $144,977/mo vs export $144,977/mo
Ingest gate: PASS
```

The tool resolves 57 assumptions and flags 33 for review. Things to notice:

- **Physical vacancy** starts from the rent roll (7 of 128 units, 5.5%) and
  glides to a stabilized 5.75% over 12 months. The sample has no market data,
  so the stabilized build uses the 1980s house default and has no market to
  compare the T12 with (see [Physical vacancy](#physical-vacancy)).
- **R&M is below the house floor.** R&M plus turnover is $750 per unit a
  year. The 1980s floor is $900. Underwriting keeps $750 and warns
  (`GUARDRAIL_BELOW_FLOOR`).
- **The payroll review gives no credit.** Payroll is $1,680 per unit, under
  the $2,000 benchmark, so the maintenance payroll on the T12 does not count
  toward R&M.
- **Capex buckets:** $192,000 at close (analyst), $1,000 per unit a year of
  recurring capex (the low end of the 1980s band, flagged because there is no
  PCA: `CAPEX_PCA_MISSING`), and the $250 replacement reserve, which does not
  bind.
- **House fallbacks:** the geography is fictional, so no market data matches.
  Rent growth is the house long-run 2.5%, and the exit cap is the going-in cap
  plus 50 bps. Both are flagged for review.

**2. Review.** Open the register and filter *Needs review* and *Guardrail
flags*. Read the R&M row: it shows the floor, the payroll review and the
$19,200 a year it would take to reach the floor.

**3. Run.** At the asking price:

| Metric | Sample result |
|---|---|
| NOI, year 1 | $966,944 |
| Capex charge below NOI, year 1 | $128,000 (recurring capex binds over the reserve) |
| Debt service, year 1 (interest-only) | $533,715 |
| Levered cash flow, year 1 | $305,229 |
| Equity (down payment + immediate capex) | $5,190,000 |
| Cash-on-cash, year 1 | 5.88% (target 8%) |
| Unlevered / levered IRR | 6.16% / 7.59% |
| Levered equity multiple | 1.38x |
| Minimum DSCR, NCF after capex / NOI | 1.39x / 1.60x |
| Going-in cap (year-1 NOI / price) | 6.77% |
| Exit cap (house: going-in + 50 bps) | 7.27% |
| Forward NOI at exit (Nov 2031 to Oct 2032) | $1,084,841 |
| Exit value | $14,919,496 |

Year-1 tax is $239,904, reset to the purchase price, against the seller's
$215,914 on the T12.

The exit-cap grid moves only the sale. At 50 bps above the base exit cap the
levered IRR falls from 7.59% to 4.28%; at 100 bps it is 0.95%.

Try an edit: set `r_and_m_per_unit` to 900 with the note "bring R&M to the
1980s floor". The warning clears, year-1 NOI falls by $19,200 to $947,744, and
the levered IRR drops to 7.09%. The house exit cap also moves, to 7.14%,
because it follows the going-in cap.

**4. Price.** Target 8% cash-on-cash, treasury 4.00% (illustrative):

| Metric | Sample result |
|---|---|
| Status | converged |
| Solved price | $11,473,423 ($89,636 per unit), about 20% below the ask |
| Loan | $7,458,000 at 5.50% (4.00% + 1.50% agency spread), 65% LTV, 36 months interest-only |
| Equity basis (with closing costs, fees, immediate capex) | $4,544,259 |
| Cash-on-cash, year 1 | 8.00% |
| Levered IRR | 11.52% |
| Minimum DSCR, NCF after capex / NOI | 1.86x / 2.14x |
| Going-in / exit cap | 8.67% / 9.17% |
| Floors enforced | 1: R&M raised from $750 to $900 per unit |

The asking price does not hit the house target. That is the point of the
exercise: the solved price is where the deal works under house policy, and
the register shows exactly which assumptions it rests on.

## Reading the ingest gates

Ingest checks each document against its own totals before anything is
modeled. Each check reads **PASS**, **BLOCKED** or **UNVERIFIED**.

| Check | Passes when |
|---|---|
| T12 opex | The parsed expense lines add up to the statement's total operating expenses within 0.5%. |
| T12 income | The parsed income lines add up to the statement's total income within 0.5%. |
| Rent roll units | The parsed unit count equals the export summary exactly. |
| Rent roll vacant units | The parsed vacant count equals the export summary (non-revenue units allowed). |
| Rent roll market rent | Monthly market rent ties to the summary within 0.5%. |
| Rent roll rent charges | In-place rent ties to the export's charge-code summary within 0.5%, when the export has one. |

- **PASS** on every check: the ingest gate is PASS.
- **BLOCKED**: a parse does not tie. The gate is BLOCKED, validation fails
  with `INGEST_BLOCKED`, and the engine will not run the deal. Nothing is
  guessed to make it tie.
- **UNVERIFIED**: the document has no total to tie to (a T12 without total
  rows, a rent roll without a summary block). It does not block, but it is
  flagged (`t12_totals_not_found_reconciliation_unverified`,
  `rent_roll_summary_not_found_reconciliation_unverified`). Check the totals
  by hand.

What to fix when a gate is BLOCKED:

- The message gives the parsed figure, the statement figure and the variance.
  Start there.
- **T12:** a subtotal row counted as a line, a line outside the statement's
  own totals, a non-operating item (debt service, capex) inside the expense
  section, or the wrong tab. Fix the file (or export a clean one) and ingest
  again.
- **Rent roll:** a summary from a different date than the unit rows, units on
  a second tab, or model and down units the summary counts differently. Ask
  for a fresh export.

## Reading the assumption register

The register (`assumptions.html`, also the Assumptions tab of the workbook)
lists every assumption the deal is underwritten on. Each row shows the value
used, the default and where it came from, why that is the default, your edit
if any, and its flags. The header shows the deal profile and counts by source.
Chips filter the table: All, Edited by user, Needs review, House defaults,
Deal evidence, Market, Guardrail flags.

### Sources

When several sources have a value, the higher one wins:

1. **Analyst** (`analyst`): you entered it. It always wins.
2. **Deal evidence** (`deal:t12`, `deal:rent_roll`, `deal:pca`, `deal:om`,
   `deal:comps`): this property's own documents.
3. **Market data** (`market:<provider>`): a configured feed for the
   submarket, then the MSA.
4. **House default** (`house:<rule>`): a house rule, such as the vintage band
   for the decade built.
5. **Engine fallback** (`engine_fallback`): nothing else supplied a value.
   Often zero or not modeled.

A few assumptions use their own order. For example, rent growth never comes
from the T12, and the capex buckets prefer a PCA to your own estimate on the
first ingest. *Candidates considered* on each row shows every value that was
weighed.

### Why defaults exist

The tool never leaves a hole. When the documents and market data do not
answer a question, a stated house rule does, so every deal is underwritten on
the same, conservative basis. Each row's *Why this default* gives the policy
reason. The [glossary](ASSUMPTION_GLOSSARY.md) lists them all with the house
values.

### Review flags

Every house default and engine fallback is marked **Needs review**. It is not
an error. It means: "a rule chose this, not evidence; confirm it." The
sample has 33 such rows. Most are quick (fund fees the deal does not model,
opex growth rates). Spend your time on rent growth, vacancy, the exit cap and
the capex buckets.

### Edits

Change a value with `edit_assumptions` (or the Override column of the
Assumptions tab) and add a note. The row keeps its default beside your value
and shows the difference. Clear the edit to go back to the default, market
data included. You can record who made an edit and when.

### Floors: underwrite warns, pricing enforces

Some assumptions have a house floor by vintage: physical vacancy, loss to
lease, collection loss, year-1 concessions, insurance per unit, R&M per unit,
the replacement reserve and recurring capex.

- **Underwriting** (`run_deal`) never changes your number. A value below its
  floor is kept and flagged `GUARDRAIL_BELOW_FLOOR`.
- **Pricing** (`solve_price`) raises it to the floor and lists the raise under
  *Floors enforced*, so a bid is never priced below house minimums.

Two exceptions: an explicit analyst insurance figure below the floor is kept
in pricing (the waiver is disclosed), and a PCA recurring capex below the
vintage band is raised to the band in pricing.

The vacancy floor is the vintage floor alone. Today's rent-roll vacancy, the
T12 vacancy, the broker's figure and a benchmark you supply are evidence for
the stabilized vacancy, never floors: a deal with 9% vacant today is not
priced at 9% for the whole hold. On a value-add deal the floor applies only
once the renovations finish (from the month after the last renovation): during
the renovation window the programs and the build set vacancy.

### Physical vacancy

Vacancy is case by case, so the tool builds it from evidence and shows each
step on the `physical_vacancy` row (*Why this default*):

- **Year 1** starts at the rent roll's vacancy.
- **Market base**: your own submarket vacancy benchmark when you give one
  (`--submarket-vacancy 0.065 --submarket-vacancy-as-of 2026-09-30`, or
  `submarket_vacancy` in `describe_deal`), else the submarket's stabilized
  vacancy for the property's star tier, else the metro, else the house default
  for the vintage.
- **Supply**: when more units will deliver over the next 24 months than the
  submarket's normal pace, vacancy rises by 25 bp per point of inventory over
  normal for years 1-3, up to 150 bp. No supply data, no adjustment (and the
  row says so).
- **Asset vs market**: half of the gap between the property's T12 vacancy and
  the market's, up to 200 bp either way, after units offline (down, model,
  renovation) and a lease-up are set aside. Without a T12 vacancy line, actuals
  you give the price solve (`--trailing-actuals-json`) are used, then an OM's
  trailing actuals when the OM labels them actuals; a broker pro forma never
  is. The row says which statement it used and at what confidence.
- **Stabilized** = market base + asset vs market. The curve glides from
  year 1 to it over 12 months (24 for value-add).

The row is always flagged for review. If you know the market better, edit
it with a note: a flat rate, or a curve such as
`{"start": 0.08, "stabilized": 0.06, "window_months": 12}`.

## Capex: the three buckets

Every deal budgets capital in three buckets. None of them enters NOI.

1. **Immediate (deferred) capex at close.** One-time money for deferred
   maintenance or PCA immediate repairs. It is funded with equity at close
   (or a lender holdback), so it raises your equity and lowers cash-on-cash,
   IRR and the solved price. If you flag deferred maintenance and give no
   figure, the house budgets a placeholder (one year of the top of the
   vintage capex band per unit) and tells you to commission a PCA.
2. **Recurring capex.** Per unit a year below NOI, from the vintage band: the
   low end for a stabilized deal, the midpoint for value-add, the high end
   with deferred maintenance. It grows at 3% a year. A PCA figure wins over
   yours, and yours wins over the house band.
3. **Replacement reserve.** The lender-style $250 per unit a year. It is a
   floor, not an addition: each month the charge below NOI is the larger of
   recurring capex and the reserve.

A building from before 2000 without a PCA is flagged `CAPEX_PCA_MISSING`.

### Deferred maintenance

Flag deferred maintenance when the asset needs work beyond normal turns. It
does more than budget capex: it raises the house minimums for physical
vacancy, collection loss, year-1 concessions and R&M to the upper part of the
vintage band (for a 1980s stabilized deal: 7.0%, 2.25%, 1.5% and $1,700 per
unit).

### The R&M payroll review

The R&M floor compares R&M plus turnover (payroll excluded) with the vintage
band. Payroll is payroll: site staff wages do not count as R&M, even if the
whole team's time is booked there. A credit is given only when the T12 labels
payroll lines as maintenance labor **and** total payroll per unit is above
the $2,000 benchmark. The credit is the smaller of the labeled maintenance
payroll and the payroll above the benchmark. The review is always shown on
the `r_and_m_payroll_credit` row. If you know maintenance labor is doing
work a contractor would otherwise bill as R&M, override that row with the
credit and a note.

## Reading results

- **NOI vs NCF.** NOI is before capex. The going-in cap, exit value and yield
  on cost use NOI. NCF (net cash flow) is NOI less the capex charge (the
  larger of recurring capex and the reserve). Cash flow, cash-on-cash, IRR and
  equity multiples are after it.
- **Cash-on-cash (CoC).** Free cash flow after debt service (and any asset
  management fee and partnership expenses) over total equity. Year 1 is the
  first twelve analysis months. The stabilized-year CoC is shown too and is
  the basis for pricing a value-add deal.
- **IRRs.** Unlevered: property cash flows and the sale, no debt. Levered:
  after debt, on your equity.
- **DSCR on both bases.** The headline DSCR uses NCF after the capex charge,
  as agency lenders size loans. The NOI-based DSCR is shown beside it.
  Minimum DSCR usually falls when interest-only ends.
- **Exit on forward NOI.** The sale is valued like a buyer would: NOI for the
  twelve months after the sale, over the exit cap. Only the sale uses that
  year; the hold's cash flows stop at the sale. You can switch to the
  trailing year (`exit_noi_window`).
- **The exit-cap grid.** The base case at -50, -25, 0, +25, +50, +75, +100
  and +150 bps around the exit cap it ran with. Only the sale price moves;
  each point shows exit value, sale proceeds, IRRs and equity multiples. Use
  it to see how much of the return depends on the exit.

## Pricing

`solve_price` searches for the highest purchase price whose cash-on-cash
still meets your target (house default 8%). Year 1 is the basis for a
stabilized deal; the first stabilized year for value-add.

What it does at each candidate price:

- re-taxes the property at that price;
- prices the loan at the 5-year treasury plus the agency spread (1.50%) and
  sizes it on NCF after the capex charge, within the deal's LTV;
- applies the house pricing costs: closing costs, acquisition fee, asset
  management fee and partnership costs, unless you entered your own;
- when you did not set an exit cap, re-anchors the house exit cap to the going-in cap at that price;
- raises every assumption below its house floor to the floor.

**Floors enforced** lists every value pricing raised, with its original and
applied value. The register page of the priced deal highlights those rows.

**Status:**

- `converged`: the solved price is found to the cent.
- `infeasible`: even the lowest price searched misses the target. There is no
  solved price. The tool still writes the register for the lowest price so
  you can see what the deal was priced on. Look at the floors enforced, the
  capex buckets and the target: a very old building with a high recurring
  capex charge may not reach 8% at any price.
- `ceiling_feasible`: the highest price searched still meets the target. Ask
  the assistant to raise the upper price bound.
- `iteration_limit`: the search ran out of steps before pinning the price to
  the cent. Ask for more iterations.

## Market data

Market data is the local view: the submarket or MSA rent-growth forecast,
stabilized vacancy, the supply pipeline (deliveries over the next 24 months
and the normal pace), concessions and market cap rate. It is read once, at
ingest; the engine never calls it. This repository ships no market data.
Data comes from configured feeds or from a file of your own.

**Check it before you ingest.** The doctor shows, for one geography, which
providers are available, whether the geography matched, and which assumptions
each would fill:

```bash
python runs/market_data_doctor.py --msa "Quillmere Synthetic MSA" \
  --metro-slug quillmere_sx --submarket "Quillmere East"
```

For the fictional sample it reports every slot as `FELL BACK` and exits 1.
That is expected.

**When it is missing**, nothing breaks and nothing is hidden. Each
assumption market data would have filled uses deal evidence or a house
fallback, is flagged for review, and says on its row why market data did not
apply. The ingest summary, the run and the register all end with a *Market
data status* block. To use your own figures, point `PLAT_MARKET_DATA_USER_FILE`
at a JSON or CSV file of observations; the README section
[Market data](../README.md#market-data) shows the format.

## Connecting your AI assistant

The tool runs as an MCP server, so Claude, ChatGPT, Grok and other assistants
that support MCP can use it. Setup steps are in the README section
[Use it from an AI assistant](../README.md#use-it-from-an-ai-assistant), and
every tool and its parameters are in [docs/MCP_TOOLS.md](MCP_TOOLS.md).

## Using the screens

The four screens (deal intake, check and assumptions, results, pricing) work
two ways:

- **In your assistant.** An assistant that supports MCP Apps opens the
  screen that belongs to each tool. If the assistant can run tools from the
  screen, the buttons work there; a step on another screen is sent to the
  assistant as a message. If it cannot, the screen is view-only and tells you
  what to ask the assistant. An assistant without MCP Apps simply answers in
  text, as before.
- **In your browser.** Run `plat-uw serve` and open
  `http://127.0.0.1:8766/`. The left side lists your deals, with *Try the
  sample deal* and *New deal*; this guide and the glossary are one click
  away. It only answers on this computer unless you start it with
  `--allow-remote` and a token (see the README section
  [Use it in your browser](../README.md#use-it-in-your-browser)).

Words with a dotted underline (NOI, DSCR, cap rate, millage and so on) show
a short definition when you hover, focus or tap them.

## CLI fallback

The same flow from a terminal, in a checkout of this repository (see the
README for install). Rates are decimals.

| Step | Command |
|---|---|
| `start_here` | This guide, and `python runs/ingest_deal.py --help`. |
| `load_sample_deal` | The sample is in `engine/samples/sample_deal/` (documents and `profile.json`). `python engine/samples/sample_deal/build_sample.py --out <folder>` writes a fresh copy. |
| `create_deal`, `add_document` | Put the T12, rent roll and any PCA in a folder. |
| `describe_deal` | The flags on the ingest command below (or a `--partial-json` file). |
| `ingest_deal` | `python runs/ingest_deal.py ... --validate` |
| `review_assumptions` | Open `<output>.assumptions.html`, or `python runs/assumptions_sheet.py export <deal.json> --xlsx <file.xlsx>` |
| `edit_assumptions` | `python runs/assumptions_sheet.py apply <deal.json> <overrides.json or .xlsx> --output <deal.v2.json>` |
| `run_deal` | `python runs/run_deal.py <deal.json> --output-dir <folder> --xlsx` |
| `solve_price` | `python -m engine.backsolve --canonical-json <deal.json> --output-dir <folder> ...` |
| `export_deal` | The run folder holds `underwriting.xlsx`, `assumptions.html`, `results.json`; the pricing folder holds `backsolve_summary.json` and its own `assumptions.html`. |

The sample deal end to end:

```bash
S=engine/samples/sample_deal
python runs/ingest_deal.py --property-id SAMPLE-GARDEN \
  --rent-roll $S/rent_roll.xlsx --t12 $S/t12.xlsx --start 2026-11 --end 2031-10 \
  --deal-type stabilized --year-built 1985 --asset-class B --star-tier 3 \
  --msa "Quillmere Synthetic MSA" --metro-slug quillmere_sx --submarket "Quillmere East" \
  --purchase-price 14280000 --ltv 0.65 --rate 0.0575 --amort 30 --io-months 24 \
  --sale-cost 0.02 --millage 21 --assessment-ratio 0.8 --immediate-capex 192000 \
  --output output/sample/deal.json --validate

python runs/run_deal.py output/sample/deal.json --output-dir output/sample/run --xlsx

python -m engine.backsolve --canonical-json output/sample/deal.json \
  --output-dir output/sample/price --policy-version plat.backsolve-policy/1 \
  --strategy cashflow --year-built 1985 --target-coc 0.08 \
  --benchmark-5yr-treasury 0.04 --benchmark-as-of 2026-09-30 \
  --benchmark-source "illustrative sample value"
```

An edit, as a plain overrides file:

```bash
echo '{"r_and_m_per_unit": {"value": 900, "note": "bring R&M to the 1980s floor"}}' > output/sample/overrides.json
python runs/assumptions_sheet.py apply output/sample/deal.json output/sample/overrides.json \
  --output output/sample/deal.v2.json
python runs/run_deal.py output/sample/deal.v2.json --output-dir output/sample/run_v2
```

Exit codes: ingest with `--validate` and `run_deal.py` exit 1 on a validation
failure; the pricing command exits 2 when the search is infeasible or
exhausted.

## FAQ and troubleshooting

**Why so many warnings?** Each house default is a review prompt, not a
problem. Validation PASS with warnings is the normal result. Filter the
register to *Needs review* and work through it.

**Validation fails with `PROPERTY_TAX_MILLAGE_MISSING`.** Enter the millage
and assessment ratio. The engine re-taxes at the purchase price and will not
guess a rate.

**The exit cap looks high.** With no analyst value and no market cap rate,
the house exit is the going-in cap at the modeled price plus 50 bps. Enter
your own exit cap if you have a view, with a note.

**R&M is flagged below the floor, but the T12 has a large maintenance
payroll.** Read the `r_and_m_payroll_credit` row. If payroll is under the
benchmark there is no credit by design. If you know better, override the
credit with a note.

**Why did pricing change my R&M (or vacancy, or capex)?** Pricing enforces
the house floors. The *Floors enforced* list shows each change. Underwriting
(`run_deal`) never changes your numbers. For vacancy the floor is the
vintage floor only: today's rent-roll vacancy is never enforced.

**The solved price is far below the ask.** Check the target cash-on-cash,
the treasury you entered, the capex charge and the floors enforced. Pricing
uses the house loan (treasury plus spread) and house costs, not the
financing you entered for the run.

**The rent roll check reads UNVERIFIED.** The export has no summary block to
tie to. It does not block. Check the unit count and total market rent by
hand, or use an export that includes its summary.

**Market data says FELL BACK for everything.** No feed is configured for
that geography. Run the doctor, or supply your own file. The house fallbacks
are conservative and flagged.

**Can I trust the numbers?** The math is deterministic and every input is
shown with its source. The documents are checked against their own totals.
The judgment calls are yours: review the flagged rows. Property-tax regimes
in particular need a qualified review for each jurisdiction.

**Where are all the keys and house values?** In the
[assumption glossary](ASSUMPTION_GLOSSARY.md), generated from the code.
