# MCP tools reference

The underwriting MCP server (`engine/mcp_server.py`) lets any MCP-capable
assistant (Claude, ChatGPT, Grok, Copilot, Cursor, ...) underwrite a
multifamily deal end to end. This page is the tool contract; the analyst
walkthrough is [USER_GUIDE.md](USER_GUIDE.md). Launch and client setup are in
the README section [Use it from an AI assistant](../README.md#use-it-from-an-ai-assistant).

The server only orchestrates: every number comes from the deterministic
engine (ingestion, the assumption resolver, the validator, the cash-flow
engine and the price search). Nothing is estimated by a model, and no tool
invents an input it was not given.

## Conventions

Every workflow tool returns a JSON object with:

| Key | Meaning |
|---|---|
| `ok` | `true` on success, `false` on a refusal |
| `summary` | One or two plain sentences an assistant can read out |
| `next_step` | What to call next (or what to ask the analyst for) |
| `error` | On a refusal: `{code, message, fix}`; `fix` says how to recover |

Rates are decimals (`0.065` is 6.5%), money is dollars, months are
`YYYY-MM`, dates `YYYY-MM-DD`. Assistants should relay `summary`, follow
`next_step`, and on an error explain `fix` and ask the analyst for what is
missing.

## The eight steps

| Step | Tool | Does |
|---|---|---|
| 1 | `create_deal` | Create a deal workspace; returns `deal_id` |
| 2 | `add_document` | Store the T12 and rent roll (OM and PCA optional) |
| 3 | `describe_deal` | Record the analysis period, price, debt, property tax, capex and market profile |
| 4 | `ingest_deal` | Parse, reconcile (the ingest gates) and resolve every assumption |
| 5 | `review_assumptions` | Read the assumption register |
| 6 | `edit_assumptions` | Apply analyst edits, stamped with the editor and date |
| 7 | `run_deal` | Validate and run: headline returns and the workbook |
| 8 | `solve_price` | The highest price that meets a target cash-on-cash |

`start_here`, `get_deal`, `list_deals` and `export_deal` work at any point.
`load_sample_deal` replaces steps 1 to 3 with the bundled synthetic sample.

## Tools

Hints are the MCP tool annotations: **RO** read-only (`readOnlyHint`);
**D** destructive (`destructiveHint`: may replace earlier data); **I**
idempotent (`idempotentHint`: the same call twice leaves the same state).
`openWorldHint` is false everywhere except `ingest_deal`, which sends an OM
to a hosted model when one is attached and `ANTHROPIC_API_KEY` is set.

| Tool | Hints | Arguments | Returns |
|---|---|---|---|
| `start_here()` | RO | none | `what_it_does`, `steps` (tool, what it does, what to ask), `try_the_sample`, `resources`, `prompts`, workspace limits, `mode` |
| `list_deals()` | RO | none | `deals`: id, name, type, steps done, last levered IRR and solved price, next step |
| `get_deal(deal_id)` | RO | | `steps` (done, at, stale), `profile`, `missing_for_ingest`, `documents`, `edits`, `last_ingest`, `last_run`, `last_price`, `pricing_defaults` (the sample's illustrative pricing inputs, else null), `files` |
| `load_sample_deal()` | not D, not I | none | A new deal from `engine/samples/sample_deal/` with its documents, profile and illustrative `pricing_defaults`; `SAMPLE_NOT_FOUND` when the bundle is missing |
| `create_deal(name, deal_type="stabilized")` | not D, not I | `deal_type`: `stabilized` or `value_add` | `deal_id` (a slug of the name plus a random suffix) |
| `add_document(deal_id, kind, filename, content_base64=None, path=None)` | D, I | `kind`: `t12`, `rent_roll`, `om`, `pca`; exactly one of `content_base64` or `path` | `document` (original name, stored path, bytes, sha256), `replaced` |
| `describe_deal(deal_id, **fields)` | not D, I | Any of the fields below; only the fields given change | `profile`, `updated`, `missing_for_ingest`, `recommended_next` |
| `ingest_deal(deal_id)` | D, I | | `gate` (PASS or BLOCKED, `blockers[]` each with a `fix`, the T12 and rent-roll reconciliation), `validation` (errors with a `fix`), `missing_to_run`, `assumptions` (counts and `review_keys`), `capital_plan`, `market_data` (summary and status lines) |
| `review_assumptions(deal_id, filter="all", case="deal", include_candidates=False)` | RO | `filter`: `all`, `needs_review`, `edited`, `defaults`, `enforced`; `case`: `deal` or `priced` | `rows` (key, label, group, value, default and its source, why, user edit, guardrail, flags), `summary_counts`, `capital_plan` |
| `edit_assumptions(deal_id, edits, editor)` | not D, I | `edits`: `{key: {"value": v, "note": "why"}}`, `{key: v}`, or `{key: null}` to clear an edit | `changed[]` (before, after, default, delta), `edits` (every recorded edit) |
| `run_deal(deal_id)` | not D, I | | `metrics` (year-1 and stabilized NOI, year-1 cash-on-cash, levered and unlevered IRR and equity multiple, going-in cap, DSCR on the sizing basis and on NOI, exit month, cap rate, value, sale costs, loan payoff and proceeds, and the exit NOI window), `capital_plan`, `exit_cap_grid`, `annual_cash_flow` (by analysis year: EGI, opex, NOI, the capex charge, debt service and cash flow before and after debt, the sale excluded), `files` |
| `solve_price(deal_id, target_coc=None, benchmark_5yr_treasury=None, benchmark_as_of=None, benchmark_source=None, coc_basis=None, exit_cap_rate=None, loan_sizing_noi_basis=None, min_price="1000000", max_price="100000000", max_iterations=40)` | not D, I | `target_coc` defaults to `0.08` (or the sample's own target); the benchmark is required | `pricing`: status, price, $/unit, equity, loan (amount, rate, sizing basis), going-in and exit cap, levered and unlevered IRR, minimum DSCR, `floors_enforced`, `exit_cap_grid`, capital plan, the feasible boundary and the benchmark used |
| `export_deal(deal_id)` | RO | | `files[]`: path, bytes, description, `local_path` (local mode) and `url` (HTTP mode, `/files/<deal_id>/<path>`) |

The stateless tools of the original `plat.underwriting.mcp/1` adapter are
unchanged and read-only: `backsolve_deal_price`, `validate_deal_inputs`,
`run_deal_summary`, `assumption_register` and `check_deal_feasibility`. They
take a canonical deal JSON as `inputs` and persist nothing.

### Behaviour worth knowing

- **Ingest keeps edits.** `ingest_deal` rebuilds the canonical from the
  documents and the profile; edits made with `edit_assumptions` are recorded
  in the workspace and applied again.
- **Stale steps.** Adding a document or changing the profile makes the
  ingest stale; ingesting or editing makes the run and the price stale.
  `get_deal` shows it, and its `next_step` follows it.
- **No invented rates.** `solve_price` refuses with `BENCHMARK_REQUIRED`
  until the analyst gives the 5-year Treasury yield. The sample deal carries
  an illustrative benchmark so it prices without asking; the response lists
  the inputs it filled in (`prefilled_from_deal`).
- **House floors** are enforced only when pricing: review them with
  `review_assumptions(filter="enforced", case="priced")`.
- **One document per kind.** Adding a kind again replaces the earlier file.
  The server names stored files (`documents/<kind><ext>`); the uploaded file
  name only supplies the extension and is recorded.

### Error codes

| Code | When | The fix points to |
|---|---|---|
| `INVALID_DEAL_ID`, `DEAL_NOT_FOUND` | Unknown or malformed deal id | `list_deals`, `create_deal` |
| `BAD_DEAL_TYPE`, `BAD_NAME`, `BAD_FIELD` | Bad `create_deal` or `describe_deal` input | The field and its format |
| `BAD_KIND`, `BAD_EXTENSION`, `TOO_LARGE`, `EMPTY_FILE`, `BAD_BASE64`, `NO_CONTENT`, `AMBIGUOUS_CONTENT`, `FILE_NOT_FOUND`, `BAD_PCA` | Upload problems | The allowed kinds and extensions, the size cap |
| `PATH_NOT_ALLOWED` | `path` sent to a remote server | Send `content_base64` |
| `DOCUMENTS_MISSING`, `PROFILE_INCOMPLETE` | Ingest before the T12, the rent roll or the analysis period | `add_document`, `describe_deal(start, end)` |
| `INGEST_FAILED`, `INGEST_REFUSED` | Unreadable documents, a bad deal type or edit | Re-export or correct |
| `NOT_INGESTED`, `NOT_READY`, `VALIDATION_FAILED`, `ENGINE_REFUSED` | Run before ingest, or a missing price, debt or millage | The `describe_deal` fields to supply |
| `BAD_FILTER`, `BAD_CASE`, `NOT_PRICED`, `BAD_EDIT`, `NO_EDITS`, `EDITOR_REQUIRED` | Review and edit input | The valid values |
| `BENCHMARK_REQUIRED`, `PRICE_REFUSED` | Price search inputs | Ask for the Treasury yield; correct the named input |
| `SAMPLE_NOT_FOUND`, `SAMPLE_INCOMPLETE`, `SAMPLE_INVALID` | The bundled sample is missing or broken | Reinstall, or use your own documents |
| `UNEXPECTED_ERROR` | Anything else (an unsupported layout or a bug) | `get_deal`, then report it |

## describe_deal fields

The fields mirror the `runs/ingest_deal.py` flags; each one's description in
the tool schema says why it matters (`engine/workflow/fields.py` is the
source of this table). `start` and `end` are needed to ingest;
`purchase_price`, `ltv`, `rate`, `amort` and `millage` are needed to run.

| Field | Group | Type | What | Why it matters |
|---|---|---|---|---|
| `deal_type` | Property | `stabilized` / `value_add` | Deal profile. | Selects the house assumption set and the price-search basis: stabilized prices on year-1 cash-on-cash, value_add on the first stabilized year. |
| `year_built` | Property | int | Year the property was built. | Drives the house vintage defaults (loss to lease, turn cost, the recurring-capex band and the collection-loss floor); older assets carry more capex. |
| `asset_class` | Property | `A` / `B` / `C` | Asset class (A, B or C). | Maps to a CoStar-style star tier for market data when star_tier is not given. |
| `star_tier` | Property | `1_2` / `3` / `4_5` | CoStar-style star tier. | Part of the market-data geography: rent growth, vacancy and cap rates are read at this tier. |
| `deferred_maintenance` | Property | bool | The asset has known deferred maintenance. | Recurring capex then defaults to the high end of the vintage band. |
| `msa` | Market | str | MSA name or five-digit CBSA code. | The market-data geography: MSA rent growth, vacancy, concessions and market cap rate feed the assumptions when no submarket data exists. |
| `metro_slug` | Market | str | Metro key the market-data feeders use (e.g. 'synthetic_metro'). | Feeders look the metro up by this slug; without it market data may not match. |
| `submarket` | Market | str | Submarket name. | Submarket market data outranks MSA data in the assumption precedence. |
| `submarket_vacancy` | Market | rate | Your submarket stabilized vacancy benchmark as a decimal (0.065 = 6.5%), e.g. from a broker or market survey. | It becomes the market base of the stabilized vacancy build, ahead of the market-data feeders: the asset's T12 vacancy is compared with it, and the long-run vacancy (and so NOI and price) moves with it. Give its date in submarket_vacancy_as_of. |
| `submarket_vacancy_as_of` | Market | date | Date of the submarket vacancy benchmark, YYYY-MM-DD. | Recorded with the benchmark so reviewers can see how current the market base is; required with submarket_vacancy. |
| `submarket_vacancy_source` | Market | str | Where the submarket vacancy benchmark comes from. | Recorded with the benchmark for the audit trail (default describe_deal:submarket_vacancy). |
| `start` | Analysis | month | Analysis start month, YYYY-MM (usually the month after closing). | Required to ingest: the monthly cash flow starts here. |
| `end` | Analysis | month | Analysis end month, YYYY-MM (a five-year hold is typical). | Required to ingest: the hold period, and the default exit month. |
| `exit_month` | Analysis | month | Sale month, YYYY-MM (default: the analysis end). | The reversion is priced on the NOI around this month. |
| `purchase_price` | Price and debt | float | Purchase price in dollars. | The basis for the loan, property tax, cap rates and returns; run_deal needs it (solve_price finds a price instead). |
| `ltv` | Price and debt | rate | Loan-to-value as a decimal (0.65 = 65%). | Sizes the loan commitment from the purchase price. |
| `rate` | Price and debt | rate | Loan interest rate as a decimal (0.065 = 6.5%). | Sets debt service, DSCR and cash-on-cash. |
| `amort` | Price and debt | int | Amortization in years (e.g. 30). | Sets the principal paid after any interest-only period. |
| `io_months` | Price and debt | int | Interest-only months (default 0). | Interest-only months lift early cash flow and cash-on-cash. |
| `term_months` | Price and debt | int | Loan term in months (default: the analysis length). | When the loan matures relative to the exit. |
| `loan_sizing_noi_basis` | Price and debt | `ncf_after_capex_charge` / `ncf_after_lender_reserve` / `noi_before_capex` | Cash flow the agency loan is sized on. | The house sizes on NOI less the below-NOI capex charge, so older assets carry a smaller loan; DSCR is reported on this basis and on NOI. |
| `millage` | Property tax | float | Combined property-tax millage in mills per $1,000 of assessed value (e.g. 22.5). | The engine taxes price x assessment ratio x mills / 1000; a priced deal fails validation without it (PROPERTY_TAX_MILLAGE_MISSING). |
| `assessment_ratio` | Property tax | float | Assessed value / purchase price (default 1.0). | Scales the taxable value; any value other than 1.0 is recorded as an analyst override. |
| `pca_immediate_capex` | Capital plan | float | PCA immediate needs, total dollars. | Property condition assessment evidence (deal:pca) outranks analyst and house figures for capex bucket 1, funded at close. |
| `pca_recurring_capex_per_unit` | Capital plan | float | PCA recurring capex, $ per unit per year. | PCA evidence for capex bucket 2, charged below NOI every year. |
| `pca_as_of` | Capital plan | date | Date of the PCA, YYYY-MM-DD. | Recorded with the PCA evidence so reviewers can see how current it is. |
| `pca_reference` | Capital plan | str | Which PCA report the figures come from. | Recorded with the PCA evidence for the audit trail. |
| `immediate_capex` | Capital plan | float | Immediate / deferred capex at close, total dollars (analyst figure). | Capex bucket 1 when no PCA covers it; it adds to the equity at close, never to NOI. |
| `immediate_capex_funding` | Capital plan | `equity_at_close` / `lender_holdback` | How immediate capex is funded. | equity_at_close adds it to the equity basis (lowering cash-on-cash); lender_holdback funds it from the loan. |
| `recurring_capex_per_unit` | Capital plan | float | Recurring capex, $ per unit per year, year 1 (analyst figure). | Capex bucket 2 when no PCA covers it; the charge below NOI is max(this, the replacement reserve). |
| `down_unit_scope` | Capital plan | `make_ready` / `heavy_repair` / `full_gut` | Scope of work for every down unit on the rent roll. | Down units are restored on a construction schedule before they earn rent; the scope sets the house cost, the start lag and the duration. The house assumes heavy_repair and flags it until you classify the units. |
| `down_unit_cost_per_unit` | Capital plan | float | Restoration cost per down unit, dollars (analyst figure). | Replaces the house scope x vintage cost for units without a PCA cost; the budget is reserved at close and lowers cash-on-cash and the solved price. |
| `down_unit_pace_per_month` | Capital plan | int | Down units the crew starts per month. | Sets when each unit is restored and leased: a slower crew keeps units offline (no rent) longer. |
| `down_unit_funding` | Capital plan | `equity_at_close` / `lender_holdback` | How the down-unit restoration budget is funded. | equity_at_close adds it to the equity basis at every price backsolve tries; lender_holdback upsizes the loan instead. Either way it never reduces operating cash flow. |
| `exit_cap` | Exit | rate | Exit cap rate as a decimal (analyst figure). | Sets the sale price; omitted, the house rule (going-in cap plus a spread) sets it. |
| `sale_cost` | Exit | rate | Sale costs as a decimal of the sale price (house default 2%). | Comes off the sale proceeds. |
| `rent_growth` | Operations | rate | Annual rent growth as a decimal (analyst figure). | Replaces the market or house rent-growth path with a flat rate. |
| `vacancy` | Operations | rate | Physical vacancy as a decimal (analyst figure). | Replaces the vacancy the rent roll and market data imply. |
| `collection_loss` | Operations | rate | Collection loss (bad debt) as a decimal (analyst figure). | Replaces the T12 or vintage-floor collection loss. |
| `sponsor_pct` | Fund | rate | Sponsor (GP) share of equity as a decimal (e.g. 0.10). | Splits the equity and the waterfall between GP and LP. |
| `pref_return` | Fund | rate | LP preferred return as a decimal (e.g. 0.08). | The hurdle the LP earns before the promote. |
| `acq_fee` | Fund | rate | Acquisition fee as a decimal of price (house default 1%). | Adds to the equity at close. |
| `am_fee` | Fund | rate | Asset management fee as a decimal (house default 1.5%). | Reduces distributable cash flow. |
| `disp_fee` | Fund | rate | Disposition fee as a decimal of the sale price (house default 1%). | Comes off the sale proceeds. |
| `analyst` | Workflow | str | Analyst name recorded on the deal. | Shown in the canonical metadata and outputs. |
| `use_market_data` | Workflow | bool | Use the configured market-data feeders (default true). | Off, assumptions come from deal evidence and house policy only; the market data status says what was used either way. |

## Prompts and resources

| Name | Kind | What |
|---|---|---|
| `new_deal_walkthrough(deal_name="")` | prompt | Guides the assistant through the eight steps, asking the analyst for missing inputs in plain language |
| `explain_assumption(key, deal_id="")` | prompt | Explains one assumption; with a `deal_id`, its current value, default, source and why |
| `guide://start-here` | resource | The `start_here` orientation as Markdown |
| `guide://user-guide` | resource | `docs/USER_GUIDE.md`, or a short built-in guide when the docs are not installed |
| `guide://glossary` | resource | `docs/ASSUMPTION_GLOSSARY.md`, or a glossary built from the assumption registry and `engine/assumptions/rationale.py` |
| `deal://{deal_id}/assumptions.html` | resource template | The deal's assumption register page |
| `ui://plat-underwriting/{intake,assumptions,results,pricing}` | resource (`text/html;profile=mcp-app`) | The analyst screens; see [Screens (MCP Apps)](#screens-mcp-apps) |

## Screens (MCP Apps)

The server implements the [MCP Apps](https://apps.extensions.modelcontextprotocol.io)
extension (`io.modelcontextprotocol/ui`, spec 2026-01-26). It advertises the
extension, registers four screens as `ui://` resources served as
`text/html;profile=mcp-app`, and links tools to them with
`_meta.ui.resourceUri` (and the older flat `_meta["ui/resourceUri"]` key some
hosts still read). A host that supports MCP Apps renders the linked screen
beside the tool's result; the screen gets the tool's input and result over the
spec's postMessage JSON-RPC (`ui/initialize`, `ui/notifications/tool-input`,
`ui/notifications/tool-result`).

| Screen | Resource | Opened by | Shows |
|---|---|---|---|
| Deal intake | `ui://plat-underwriting/intake` | `create_deal`, `describe_deal`, `get_deal`, `load_sample_deal` | Progress through the steps (from `get_deal`), the documents (drag and drop, uploaded with `add_document`), the deal facts grouped as Property, Location, Price & financing, Taxes, Capex & condition and Hold, each with why it matters and whether it is needed to check or to run; **Check documents** (`describe_deal`, then `ingest_deal`) |
| Check and assumptions | `ui://plat-underwriting/assumptions` | `ingest_deal`, `review_assumptions`, `edit_assumptions` | The ingest gate (PASS or BLOCKED, the fix for each blocker, the reconciliation), validation, market data status and the capital plan; the register with chips (needs review, edited, defaults, enforced in pricing), value, default and source, why this default, and inline edits with a note (`edit_assumptions`, the editor's name asked once per session) and the deltas they made |
| Results | `ui://plat-underwriting/results` | `run_deal` | Headline tiles (NOI, cash-on-cash, levered and unlevered IRR, equity multiple, minimum DSCR on NCF with NOI beside it), the capital plan by year (three buckets, which binds), `annual_cash_flow`, the exit on forward NOI and the exit-cap grid, with inline SVG charts |
| Pricing | `ui://plat-underwriting/pricing` | `solve_price` | Target cash-on-cash and the Treasury benchmark (never invented), the solve, the pricing panel (status, price, $/unit, loan and its sizing basis, caps, IRRs, floors enforced, each linking to its register row) and the asking price against the solved price |

Every screen has the step header (1 Documents, 2 Describe, 3 Check,
4 Assumptions, 5 Results, 6 Price, 7 Export), a **New? Start here** panel,
**Export files** (`export_deal`) and plain empty, loading and error states
(an error shows its `fix` first).

What each host gets:

- **Full support** (the host proxies `tools/call`, the `serverTools`
  capability): the screens act directly, calling the tools above plus
  `get_deal`, `add_document` and `export_deal`. A step that lives on another
  screen is requested from the assistant with `ui/message`
  ("Run the deal ... (run_deal)"), and a change made in a screen is reported
  with `ui/update-model-context` when the host accepts it.
- **Render only** (no `serverTools`, as in hosts with partial MCP Apps
  support): the screen shows the result it was opened with, read-only, and
  says what to ask the assistant ("Ask the assistant to ...", with a copy
  button).
- **No MCP Apps**: nothing changes; every tool returns the same JSON text.

The pages are self-contained: CSS and JavaScript are inlined, no external
URL is loaded (the default MCP Apps CSP, no `connect-src`), and the page
carries no deal data or server paths; it reads only the tool results the host
sends. The sources are the plain files in `engine/ui/static/`. The same
screens run as a browser app with `plat-uw serve`
([README](../README.md#use-it-in-your-browser)), where `POST
/api/tools/<name>` stands in for the host.

## The sample deal

`load_sample_deal` reads `engine/samples/sample_deal/` (through
`engine.samples` when installed): `profile.json` names the `documents`,
carries the `ingest` inputs (the `runs/ingest_deal.py` flags, underscored,
mapped onto the `describe_deal` fields; `no_market_data: true` becomes
`use_market_data: false`) and a `pricing` block (target cash-on-cash and an
illustrative benchmark) that `solve_price` uses when the caller gives none.
A flat profile (the `describe_deal` fields plus `name` and `deal_type`) also
loads.

## Workspace

Deals live outside the repository, one folder per deal:

```
<workspace>/deals/<deal_id>/
  deal.json          manifest: name, type, profile, documents, edits, step status
  documents/         t12.xlsx, rent_roll.csv, om.pdf, pca.json (one per kind)
  canonical.json     the ingested deal; assumptions.json and assumptions.html beside it
  run/               results.json, underwriting.xlsx, assumptions.html, canonical.json, validation.json
  price/             backsolve_summary.json, assumptions.html, the priced canonical and results
```

`<workspace>` is `$PLAT_UNDERWRITING_HOME`, else `dir` in the `[workspace]`
table of the user config file (`$PLAT_UNDERWRITING_CONFIG`, else
`~/.config/plat-underwriting/config.toml`), else
`$XDG_DATA_HOME/plat-underwriting` (`~/.local/share/plat-underwriting`).
Deal ids must match `[a-z0-9-]` and every file path is resolved inside the
deal folder, so `..`, absolute paths and NUL bytes are refused.

Upload limits: `$PLAT_UW_MAX_UPLOAD_MB` or `[workspace] max_upload_mb`
(default 25). The extensions allowed per kind (`t12` and `rent_roll`: .xlsx,
.xlsm, .xls, .csv; `om`: .pdf, .txt; `pca`: .json, .pdf) can be replaced
with `$PLAT_UW_ALLOWED_EXTENSIONS` (JSON `{kind: [".ext"]}`) or
`[workspace.allowed_extensions]`.

```toml
[workspace]
dir = "~/underwriting"
max_upload_mb = 50

[workspace.allowed_extensions]
t12 = [".xlsx", ".csv"]
```

## Transports and access

| Mode | Launch | `path` inputs | Access |
|---|---|---|---|
| stdio | `plat-uw mcp` (or `plat-underwriting-mcp`) | allowed | the local client process |
| local HTTP | `plat-uw mcp --http [--host 127.0.0.1] [--port 8765]` | allowed | loopback `Host` only; `PLAT_UW_TOKEN` enforced when set |
| remote HTTP | `plat-uw mcp --http --host 0.0.0.0 --allow-remote` | refused | bearer token `PLAT_UW_TOKEN` (16 or more characters) on every request |
| browser app | `plat-uw serve [--host 127.0.0.1] [--port 8766]` (`--allow-remote` as above) | refused (the browser uploads files) | as above; in remote mode the token is entered once at `/login` and kept in an HttpOnly, SameSite=Strict cookie; every POST needs the `X-Plat-UW: 1` header and a same-origin request |

The MCP endpoint is `/mcp`; workspace files are served at
`/files/<deal_id>/<path>` behind the same check. `PLAT_UW_PUBLIC_URL` sets
the base of the links `export_deal` returns (for a tunnel or a proxy).

The bearer token is interim access control for one trusted user, not
production authentication. Hosting adds OAuth at `engine.mcp_http.authorize`
(the one function every request passes through), per-user workspaces and
storage, and retention rules.
