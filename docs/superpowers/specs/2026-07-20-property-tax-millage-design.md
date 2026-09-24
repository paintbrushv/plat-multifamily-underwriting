# Canonical Property-Tax Millage Design

**Date:** 2026-07-20  
**Status:** Design approved — pending implementation plan  
**Scope:** `multifamily-underwriting` deterministic tax policy and `plat-agent` lifecycle, memo, reconciliation, and CRM preview integration

---

## Problem

Property-tax treatment is currently fragmented and can produce a materially unsupported acquisition underwrite:

- The executable and written house policy defaults the reassessed basis to 90% of purchase price, while the approved policy makes 100% the sole default.
- Tax assumptions are spread across ad hoc metadata fields with incompatible units. Some fields hold raw mills, some percentage points, and some decimal rates despite names ending in `_pct`.
- When no usable rate reaches the price backsolve, the reset silently does nothing. A trailing-T12 or broker tax row can remain fixed as candidate purchase price changes; if no row exists, tax can remain zero.
- `/underwrite-deal` has no single, unit-labelled question for the missing assumption. Non-interactive `plat lifecycle` has neither a millage option nor a tax-specific blocker, so it can continue toward judgment without a supported tax reset.
- The analyst answer is not persisted through the same pre-cache path as other lifecycle overrides. A punchlist checkbox can clear state but cannot store a millage value.
- Memo, reconciliation, and CRM preview do not consistently expose the rate, assessment ratio, purchase-price basis, annual tax, evidence, or whether a non-default ratio is an analyst override.

Because taxes affect NOI, debt sizing, cash-on-cash return, and price, missing or stale taxes are a valuation blocker rather than a presentation defect.

## Goals

1. Establish one canonical, unit-explicit property-tax assumption shared across both repositories.
2. Calculate reassessed ad valorem tax deterministically as purchase price changes.
3. Make `assessment_ratio = 1.00` the only default while retaining explicit, evidence-backed analyst overrides.
4. Stop valuation before judgment and underwriting when combined millage is missing.
5. Preserve human control: prompt in the interactive workflow; return an actionable structured blocker in the non-interactive workflow.
6. Persist analyst-supplied millage before lifecycle cache hashing so fresh runs and resumes are deterministic and auditable.
7. Show the final tax assumption, calculation, provenance, and override status in analyst-facing outputs.
8. Migrate old 90% guidance and known ad hoc fields without rewriting historical run artifacts or guessing ambiguous units.

## Non-goals

- Build a property-tax appeals, jurisdiction, or reassessment forecasting system.
- Infer millage from trailing tax dollars, broker tax dollars, asset value, or field magnitude.
- Make `market-study-agent` responsible for tax data or calculation.
- Define treatment for PILOT schedules, abatements, exemptions, BID charges, or fixed special assessments. Those programs require separate policy; they neither permit fabricated millage nor become silent fallbacks in this flow.
- Rewrite immutable historical run outputs solely to conform them to the new policy.
- Add a permanent alias layer for every historical tax field. Migration is explicit and finite; canonical consumers use the new object after cutover.

---

## Ownership Boundary

### `multifamily-underwriting`

This repository owns the deterministic seam:

- canonical schema and validation;
- unit-labelled normalization into mills;
- assessment-ratio default and override rules;
- the price-dependent formula and cent rounding;
- replacement of the canonical annual ad valorem OpEx row for every candidate price;
- tax calculation provenance in deterministic outputs; and
- formula, validation, price-search, and reconciliation tests.

The generic OpEx engine continues to consume annual dollars. It does not learn jurisdiction rules or prompt an analyst.

### `plat-agent`

This repository owns the human and lifecycle seam:

- deterministic harvesting of millage evidence;
- interactive `/underwrite-deal` questioning;
- the non-interactive `--millage-rate` option;
- missing-input detection and the `missing_property_tax_millage` blocker;
- persistence before cache hashing and deterministic resume behavior;
- prevention of judgment and underwriting while millage remains missing; and
- memo and CRM preview projection from final canonical/deterministic outputs.

Memo and CRM code render the result; they do not independently recalculate tax.

### Out of scope

`market-study-agent` has no property-tax responsibility. Historical deal artifacts remain evidence of prior decisions, not executable policy.

---

## Canonical Schema and Units

The only canonical acquisition-tax assumption is:

```json
{
  "metadata": {
    "property_summary": {
      "property_tax_policy": {
        "millage_rate_mills": 25.31,
        "assessment_ratio": 1.0,
        "source": "county_tax_notice",
        "source_locator": "raw_inputs/2026_tax_notice.pdf, page 2, combined millage table",
        "analyst_override": false
      }
    }
  }
}
```

| Field | Type and unit | Required semantics |
|---|---|---|
| `millage_rate_mills` | Positive finite decimal; mills per $1,000 of assessed value | Required before judgment. `25.31` means 25.31 mills, not 25.31% and not decimal rate `0.02531`. Zero and missing values are invalid; special tax programs are outside this design. |
| `assessment_ratio` | Positive finite decimal multiplier | Materialized as `1.00` when no evidence-backed exception exists. Values other than `1.00`, including jurisdictional assessed-value/equalization ratios, require `analyst_override: true` and supporting provenance. |
| `source` | Non-empty string | Identifies the authoritative evidence class. Recommended values are `county_tax_notice`, `cad_rate_table`, `offering_memorandum`, `lender_tax_memo`, `analyst`, and `composite_evidence`; the schema does not impose an enum. |
| `source_locator` | Non-empty string | Gives a reproducible file/page/table/URL or stable analyst-input location. When millage and ratio rely on different evidence, use `composite_evidence` and the deterministic composite format defined below. |
| `analyst_override` | Boolean | Means the analyst deliberately approved a ratio exception to the 100% default. It must be `true` if and only if `assessment_ratio != 1.00`; a manual millage entry with the default ratio remains `false`. Evidence alone does not synthesize analyst approval. |

Numbers are parsed and calculated as decimal values, not binary floats. The schema rejects booleans, NaN/infinity, strings with `%` or `mills`, missing provenance, and unsupported ratios without an explicit override. The canonical object is complete before deterministic valuation; downstream code must not search aliases when it is present.

Analyst-input provenance is stable and contains no timestamp or invocation-specific path:

- interactive answer: `source = "analyst"` and `source_locator = "underwrite-deal:property_tax_millage"`;
- CLI answer: `source = "analyst"` and `source_locator = "plat lifecycle:--millage-rate"`;
- separately sourced ratio override: `source = "composite_evidence"` and `source_locator = "millage=<stable millage locator>;assessment_ratio=<ratio source>:<stable ratio locator>"`.

The millage locator always precedes the ratio locator. Re-entering the same value preserves a byte-equivalent policy object and cache hash.

### Unit normalization

Normalization is allowed only when the producer or source labels the unit:

| Labelled source representation | Canonical conversion |
|---|---:|
| Raw mills `25.31` | `millage_rate_mills = 25.31` |
| Decimal tax rate `0.02531` | `millage_rate_mills = 0.02531 × 1000 = 25.31` |
| Percentage points `2.531%` | `millage_rate_mills = 2.531 × 10 = 25.31` |
| Rate per $100 `2.531` | `millage_rate_mills = 2.531 × 10 = 25.31` |

Code must never choose a conversion from numeric magnitude alone. An ambiguous value is treated as missing and returned for analyst resolution.

After normalization, two distinct candidate millage values from extracted evidence are conflicting; no source class silently outranks another. Interactive processing resolves the conflict through the required analyst answer. Non-interactive processing without `--millage-rate` treats the conflict as unresolved and emits `missing_property_tax_millage`.

---

## Formula, Basis, Rounding, and Precedence

For every candidate purchase price:

```text
decimal_tax_rate = millage_rate_mills / 1000
assessed_value_basis = purchase_price × assessment_ratio
annual_property_tax = assessed_value_basis × decimal_tax_rate
```

Equivalently:

```text
annual_property_tax = purchase_price × (millage_rate_mills / 1000) × assessment_ratio
```

`purchase_price` is the current acquisition-price candidate, not asking price, trailing assessed value, broker valuation, or the final price from a prior run. The deterministic calculation retains Decimal precision through the multiplication and rounds the annual fixed-dollar OpEx to cents with `ROUND_HALF_UP` at the output seam.

Input precedence is:

1. A valid answer to the current interactive millage question or the current invocation’s `--millage-rate`.
2. An already persisted canonical `property_tax_policy` for a non-interactive run when no new CLI value is supplied.
3. Exactly one unambiguous, unit-labelled deterministic source value normalized to mills.
4. No value, an ambiguous value, or conflicting extracted values: emit the missing-millage interaction/blocker and stop before judgment.

There is no trailing-tax, broker-tax, zero-tax, remembered-rate, or 90% fallback. Trailing and broker annual tax remain comparison evidence only.

Ratio precedence is narrower:

1. An explicit evidence-backed canonical ratio with `analyst_override: true`.
2. The sole default, `assessment_ratio = 1.00` and `analyst_override = false`.

The legacy `full_value_assessment` boolean no longer selects policy. Full value is the default, and any non-default multiplier is represented directly and visibly.

The deterministic tax adapter upserts exactly one canonical `Real Estate Taxes` ad valorem row for each candidate price. Matching is case-insensitive after trimming and collapsing whitespace, and the exhaustive replaceable labels are `Real Estate Taxes`, `Real Estate Tax`, `Property Taxes`, `Property Tax`, `RE Taxes`, `RE Tax`, and `Taxes - Real Estate`. Labels outside that set are not classified by name or magnitude. Existing separately named non-ad-valorem expense rows are outside this design and remain untouched.

---

## Data Flow

### Interactive `/underwrite-deal`

1. Harvest rate and ratio evidence from the OM, tax notice, CAD table, lender memo, and existing canonical assumptions.
2. Normalize only unambiguous labelled evidence to `millage_rate_mills`. If extracted candidates differ after normalization, show the conflict rather than selecting a source.
3. Before every valuation, ask the unit-labelled question below. Show any harvested or persisted value as context, but do not let it suppress the analyst confirmation:

   > What combined property-tax millage should be used? Enter mills per $1,000 of assessed value (for example, `25.31`).

4. Persist the answer with the stable interactive provenance defined above.
5. Do not ask whether to use 90% or 100%. Materialize `assessment_ratio = 1.00` automatically.
6. If evidence supports a ratio other than 100%, present the evidence to the analyst and require explicit approval. Persist the approved ratio, `analyst_override: true`, and the deterministic composite source locator.
7. Write the complete canonical policy to the run input and analyst assumptions record before price judgment, then run the deterministic calculation.
8. If the analyst does not answer, record the unresolved assumption and stop. Do not present a final valuation.

### Non-interactive `plat lifecycle`

1. Add `--millage-rate <mills>`, where the argument is combined mills per $1,000.
2. Apply and persist the option before cache hashing on both fresh and resumed runs.
3. If neither the option nor an unambiguous canonical/extracted value supplies millage, or extracted candidates conflict, emit a structured blocker with:
   - `id: missing_property_tax_millage`;
   - field and unit description;
   - the evidence locations inspected;
   - a human-readable explanation that valuation has not run; and
   - a rendered resume command using the current data-room path and run identity:

     ```text
     plat lifecycle '<current-data-room>' --resume <deal_slug>/<run_id> --millage-rate <mills>
     ```

4. Persist the blocker in lifecycle state and punchlist output, return a needs-analyst-input/blocked result, and stop before judgment and underwriting. The ordinary degraded-intake behavior must not bypass this gate.
5. A resume with `--millage-rate` persists the value first, clears only the matching blocker after successful validation, invalidates judgment and downstream cache entries, and continues without requiring the analyst to edit a checkbox.

The rendered command is exact for the blocked run: `<current-data-room>`, `<deal_slug>`, and `<run_id>` are replaced with shell-safe actual values; only `<mills>` remains for the analyst to supply.

---

## Persistence, Cache, and Resume Semantics

- `property_tax_policy` is part of the canonical engine input and therefore part of the cache hash.
- CLI/interactive analyst input is atomically patched into the run-scoped canonical artifact before any cache-satisfaction check, matching the existing address/market override ordering.
- The persisted millage, not a cleared punchlist checkbox, is the source of truth.
- Supplying a different valid millage on resume updates provenance, changes the hash, and reruns judgment, underwriting, reconciliation, memo, and CRM preview.
- Repeating a resume with the same canonical policy is idempotent and may reuse matching downstream cache entries.
- Removing the value restores `missing_property_tax_millage`. A present but malformed, non-finite, zero, or negative value produces `invalid_property_tax_millage`. Neither case revives a prior tax row as a fallback.
- Candidate-price search recalculates annual tax, NOI, debt sizing, equity, and cash-on-cash for every candidate. The saved final canonical and summary must record the same candidate price, basis, rate, ratio, and annual tax used by the engine.

---

## Error Handling

| Condition | Required behavior |
|---|---|
| Millage absent | Interactive workflow asks and waits; non-interactive lifecycle emits `missing_property_tax_millage`; both stop before judgment/underwriting. |
| Millage present but malformed, non-finite, zero, or negative | Reject as `invalid_property_tax_millage`, state the mills-per-$1,000 unit, preserve the submitted value for diagnosis, and do not clear the blocker. |
| Unit is ambiguous or extracted values conflict | Do not infer by magnitude or source rank. Cite every conflicting field/source; interactive processing uses the required analyst answer, while non-interactive processing emits `missing_property_tax_millage`. |
| Ratio differs from `1.00` without explicit approval or evidence locator | Reject as `unsupported_property_tax_assessment_override`. |
| Canonical and new analyst millage conflict | The current explicit analyst input wins only after validation; persist its stable provenance and invalidate downstream cache. |
| Trailing/broker tax conflicts with calculated tax | Keep the deterministic result and display the variance in reconciliation; do not silently substitute the comparison amount or create an additional approval gate. |
| A replaceable ad valorem tax alias exists in OpEx | Normalize the exhaustive label set above and replace the single ad valorem row; leave labels outside the set untouched. |
| Deterministic calculation fails | Return a named hard error with the canonical inputs and source locator; do not generate a trusted memo or CRM preview. |

Errors must be specific enough to correct without inspecting source code. No error path may convert missing evidence into a default rate or stale annual tax.

---

## Compatibility and Migration

### Retiring the 90% default

- Change executable defaults, repo policies, agent guidance, command guidance, and tests in both owning repositories to 100%.
- Preserve the historical error-memory entry as history and append a superseding policy entry rather than rewriting the record.
- Do not bulk-edit immutable prior run outputs.
- Inspect current reusable inputs that contain `0.90`:
  - retain it only when a source locator supports a jurisdictional assessed-value/equalization rule and the analyst explicitly approves it; then mark it `analyst_override: true`;
  - remove guidance-derived, unsupported, or unapproved 90% values, materialize `1.00`, and set `analyst_override: false`.
- Other evidence-backed ratios such as 70%, 33.33%, or 10% follow the same explicit-override rule. The new default must not erase legitimate jurisdiction mechanics.

### Migrating ad hoc fields

A one-time migration adapter maps only known producer/unit combinations:

| Legacy field | Known unit | Migration |
|---|---|---|
| `property_tax_millage_rate` or labelled raw `tax_millage_rate` | mills | Copy to `millage_rate_mills`. |
| Plat `property_tax_context.local_tax_rate` | decimal rate | Multiply by 1,000. |
| Engine/backsolve `tax_rate_pct` from its known canonical producer | decimal rate despite the name | Multiply by 1,000. |
| Broker-snapshot `property_tax_rate_pct` | percentage points | Multiply by 10. |
| `assessment_ratio`, `reassessment_ratio`, or `analyst_ratio_override` | multiplier | Copy to `assessment_ratio`; retain a non-1.00 value only when evidence and explicit analyst approval already exist. Never synthesize approval during migration. |

An ambiguously named field without a known producer and labelled unit is not migrated numerically. It produces the same analyst-input requirement as a missing field. After current callers and reusable inputs are migrated, deterministic consumers read only `property_tax_policy`; aliases are removed rather than maintained as a second convention.

Existing T12/broker fixed-annual tax rows remain trailing/broker evidence for reconciliation. They cannot satisfy the canonical millage requirement and cannot become the house acquisition tax by precedence.

---

## Analyst-Facing Outputs

Successful memo, reconciliation, and CRM preview surfaces read one final deterministic tax result and display:

- combined millage in mills and its equivalent percentage;
- assessment ratio;
- purchase-price basis and assessed-value basis;
- annual calculated ad valorem tax;
- source and source locator;
- `analyst_override` as `Yes` or `No`.

Example presentation:

```text
Property tax: 25.310 mills (2.5310%) × 100.00% assessment ratio
Basis: $10,000,000 purchase price → $10,000,000 assessed-value basis
Annual ad valorem tax: $253,100.00
Source: county_tax_notice — raw_inputs/2026_tax_notice.pdf, page 2
Assessment-ratio override: No
```

The reconciliation presents trailing actual, broker forecast, and house calculated tax side by side, with dollar and percentage variances and the house provenance. CRM preview carries the same rate, ratio, basis, annual tax, provenance, and override status so an analyst can verify the payload before publishing. A blocked run shows `NEEDS_DATA` and the resolution command instead of tax economics or a final recommendation.

---

## RED → GREEN Test Plan

Each behavior is introduced with a failing focused test before implementation:

1. **Formula:** `$10,000,000 × (25 / 1000) × 1.00 = $250,000.00`, with Decimal/cent-rounding coverage.
2. **100% default:** a valid millage with no ratio materializes `assessment_ratio = 1.00`, `analyst_override = false`, and full purchase-price basis.
3. **Explicit override:** an evidence-backed 70% ratio calculates on 70% of price and retains source, locator, and `analyst_override = true`; an unsupported non-100% ratio fails validation.
4. **Missing-rate blocking:** the deterministic price-dependent seam rejects absent millage instead of retaining T12/broker/no tax.
5. **Interactive prompt:** `/underwrite-deal` asks the unit-labelled combined-millage question, persists the answer, and does not ask a 90%-versus-100% question.
6. **Non-interactive blocker:** `plat lifecycle` without rate evidence emits `missing_property_tax_millage`, includes the exact rendered resume command, and never invokes judgment or underwriting.
7. **Units normalization:** labelled raw mills, decimal rates, percentage points, and per-$100 rates normalize to the same mills; ambiguous fields are rejected rather than magnitude-guessed.
8. **Price-dependent recalculation:** two candidate prices produce proportionally different tax, NOI, debt sizing, and cash-on-cash results; the final saved result matches the winning candidate.
9. **Output provenance:** memo, reconciliation, and CRM preview all show the same millage, ratio, basis, annual tax, source locator, and override status from the deterministic output.
10. **Persistence/resume:** `--millage-rate` is stored before hashing, clears the matching blocker only after validation, invalidates downstream work when changed, and is idempotent when unchanged.
11. **Compatibility:** known legacy producer/unit combinations migrate correctly; unsupported or unapproved 90% defaults are replaced by 100%, while explicit evidence-backed ratios remain intact.
12. **Validation/error paths:** malformed millage, ambiguous units, unsupported ratio overrides, and duplicate tax-category aliases produce the specified observable behavior.

Run the focused multifamily policy/backsolve/reconciliation tests and Plat parser/lifecycle/cache/memo/CRM tests. A cross-repository lifecycle fixture must prove that a blocked run resumes with analyst-supplied mills and reaches outputs whose tax amount matches the deterministic formula.

---

## Rollout

1. Add the canonical schema, validator, unit normalizer, 100% default, deterministic missing-millage error, and focused RED → GREEN engine tests.
2. Migrate the backsolve to the canonical object, recalculate each price candidate, normalize the ad valorem OpEx row, and remove alias reads after migration coverage passes.
3. Add Plat evidence normalization, interactive question, `--millage-rate`, pre-cache persistence, the pre-judgment blocker gate, and resume tests.
4. Project the deterministic result into memo, reconciliation, and CRM preview, then add output-consistency tests.
5. Update the current policy/guidance surfaces in both repositories and append the superseding error-memory lesson. Migrate active reusable inputs; leave historical outputs unchanged.
6. Run the focused suites and one cross-repository blocked-then-resumed lifecycle fixture before enabling the gate for all new runs.

Rollback is a code rollback, not a policy fallback: disable the new run path if necessary, but never restore silent stale/zero tax behavior. Runs created under the new schema retain their canonical policy and provenance.

---

## Acceptance Criteria

The design is implemented only when all of the following are true:

- [ ] The canonical object contains exactly the required fields `millage_rate_mills`, `assessment_ratio`, `source`, `source_locator`, and `analyst_override`, with the units and invariants defined above.
- [ ] The deterministic annual tax equals `purchase_price × (millage_rate_mills / 1000) × assessment_ratio`, rounded to cents at the output seam.
- [ ] `assessment_ratio = 1.00` is the sole default in executable code and current guidance across both repositories.
- [ ] Explicit non-100% assessment ratios survive only as evidence-backed analyst overrides with reproducible provenance.
- [ ] Missing millage never preserves trailing, broker, zero, or stale tax and never reaches judgment or underwriting.
- [ ] Interactive `/underwrite-deal` asks for combined millage in mills and persists the answer before valuation.
- [ ] Non-interactive `plat lifecycle` emits `missing_property_tax_millage` and the exact rendered `--millage-rate <mills>` resume command, then stops before judgment and underwriting.
- [ ] Analyst input is persisted before cache hashing; changed values invalidate downstream work, while unchanged resumes are idempotent.
- [ ] Every price candidate recalculates tax and all dependent economics from that candidate’s purchase price.
- [ ] Known labelled legacy units migrate deterministically, ambiguous fields block, unsupported old 90% defaults are removed, and historical artifacts remain unchanged.
- [ ] Memo, reconciliation, and CRM preview show consistent rate, ratio, basis, annual tax, provenance, and override status from the final deterministic result.
- [ ] All RED → GREEN cases above pass in focused repository suites and in the blocked-then-resumed cross-repository lifecycle fixture.

## Design Self-Review

- **Placeholders:** No unresolved design placeholders remain. Angle-bracket values occur only in the required CLI command template and are defined by the surrounding text.
- **Consistency:** One schema, one unit (mills), one formula, one 100% default, and one explicit override rule apply across both repositories and all output projections.
- **Ambiguity:** Rate representations, calculation basis, ratio semantics, precedence, rounding, blocker behavior, cache ordering, and resume behavior are explicit. Unit conversion by magnitude is forbidden.
- **Scope:** The design is limited to acquisition ad valorem tax, its analyst interaction, deterministic calculation, migration, and projections. Jurisdiction forecasting, tax appeals, market-study work, and separately scheduled/fixed tax programs remain outside this change.
