# Design Spec: Market Intelligence Tiered Automation (Task 4.1)

**Date:** 2026-04-20
**Author:** Matthew Dickson, Claude Opus 4.6
**Status:** Approved
**Companion:** `docs/plan.md` Task 4.1

---

## Summary

Add auto/suggest/off automation levels for market-adjusted underwriting assumptions. Synthesizes three independent data sources (econometric rent growth forecasts, comp positioning, supply risk scores) into confidence-scored adjustment recommendations. Rent growth adjustments support all three modes; vacancy adjustments are suggest-only in v1.

## Architecture

```
market-study-agent repo                supply-demand repo
        |                                      |
        v                                      v
  export_market_intel.py               export_forecast.py
  (comp summary + supply risk)         (rent growth forecast)
        |                                      |
        +----------+          +----------------+
                   v          v
            data/market_intel/{metro_slug}.json
                        |
                        v
              engine/market_automation.py  (NEW)
              +-- load_market_intel(metro_slug, path) -> MarketIntel
              +-- compute_adjustments(inputs, intel) -> MarketAdjustment[]
              +-- compute_confidence(intel) -> ConfidenceScore
              +-- apply_adjustments(inputs, adjustments) -> modified inputs
              +-- attach_audit_trail(results, adjustments, mode) -> results
                        |
                        v
              CLI / API calls run_underwriting(inputs)
              then attaches audit trail to results
```

The engine (`run_underwriting()`) is unchanged. Market automation is a pre-engine orchestration layer, matching the existing pattern for scenarios, portfolio stress, and refi.

## Data Contract

One JSON file per metro at `data/market_intel/{metro_slug}.json`, produced by export scripts in the sibling repos.

```json
{
  "metro_slug": "dallas_tx",
  "as_of_date": "2026-04-11",
  "rent_growth_forecast": {
    "annual_rate": 0.018,
    "ci_80": [-0.012, 0.048],
    "ci_95": [-0.035, 0.071],
    "model": "ensemble_4",
    "vintage": "2025Q3"
  },
  "supply_risk": {
    "score": 62,
    "verdict": "CAUTION",
    "deliveries_pct_inventory": 0.034,
    "absorption_rate": 0.028
  },
  "comp_summary": {
    "snapshot_date": "2026-04-20",
    "comp_count": 8,
    "wtd_rent_premium_pct": -0.03
  }
}
```

### Required Fields

- `metro_slug`: string, matches directory convention (e.g., `dallas_tx`)
- `as_of_date`: ISO date string
- `rent_growth_forecast.annual_rate`: float, YoY rent growth point estimate
- `rent_growth_forecast.ci_80`: [lo, hi] 80% confidence interval
- `supply_risk.score`: int 0-100
- `supply_risk.verdict`: "PROCEED" | "CAUTION" | "PASS"
- `comp_summary.comp_count`: int
- `comp_summary.wtd_rent_premium_pct`: float, weighted avg premium (negative = below market)

### Optional Fields

- `rent_growth_forecast.ci_95`: [lo, hi] 95% confidence interval
- `rent_growth_forecast.model`: string, model name
- `rent_growth_forecast.vintage`: string, forecast vintage quarter
- `supply_risk.deliveries_pct_inventory`: float
- `supply_risk.absorption_rate`: float
- `comp_summary.snapshot_date`: ISO date string

## Schema Addition

New top-level `options` object added to `deal_schema_v0_1.json`:

```json
"options": {
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "market_automation_level": {
      "type": "string",
      "enum": ["auto", "suggest", "off"],
      "default": "off",
      "description": "auto: apply market adjustments with confidence scoring. suggest: compute and report but don't apply. off: no market adjustments."
    },
    "market_intel_path": {
      "type": "string",
      "description": "Override path to market intel JSON. Defaults to data/market_intel/{metro_slug}.json derived from deal metro."
    }
  }
}
```

The `options` object is optional (not in `required[]`). Missing `options` or missing `market_automation_level` defaults to `"off"`.

## Confidence Scoring

Composite 0-1 score from four independent signals:

| Component | Weight | Scoring |
|-----------|--------|---------|
| Forecast precision | 0.35 | `1 - (ci80_width / 0.10)` clamped 0-1. Narrow CI = high confidence. |
| Comp depth | 0.25 | `min(comp_count / 10, 1.0)` * recency decay (1.0 if <30d, 0.5 if 30-90d, 0.2 if >90d) |
| Supply signal clarity | 0.20 | Distance from threshold boundaries. PROCEED=0.8+, CAUTION=0.5, PASS=0.8+ |
| Signal agreement | 0.20 | Comp positioning and econometric forecast agree on direction? Same sign=1.0, opposite=0.3 |

### Confidence Thresholds (auto mode)

- `>= 0.6`: adjustment applied, confidence label "high"
- `0.4 - 0.6`: adjustment applied, confidence label "medium", warning flag set
- `< 0.4`: adjustment computed but NOT applied even in auto mode (falls back to suggest behavior)

### Recency Decay

Computed from `comp_summary.snapshot_date` relative to `metadata.as_of_date`:

- < 30 days: factor 1.0
- 30-90 days: factor 0.5
- > 90 days: factor 0.2

If `snapshot_date` is missing, assume 0.5 (moderate decay).

### Supply Signal Clarity

Supply risk score maps to confidence component:

- Score 0-20 (strong PROCEED): 0.9
- Score 21-40 (PROCEED): 0.7
- Score 41-60 (CAUTION boundary): 0.5
- Score 61-80 (CAUTION/PASS boundary): 0.5
- Score 81-100 (strong PASS): 0.9

The logic: extreme scores (clear PROCEED or clear PASS) are more informative than boundary scores.

## Adjustment Computation

### Rent Growth (auto/suggest/off)

1. Extract deal's `growth_assumptions.annual_growth_rate` as base
2. Econometric delta: `forecast.annual_rate - base`
3. Comp delta: reuse existing `suggest_rent_growth_adjustment()` from `market_integration.py` to get comp-adjusted rate, compute delta from base
4. Blended delta: `0.6 * econometric_delta + 0.4 * comp_delta`
5. Suggested value: `base + blended_delta`
6. Clamp: final rate within forecast 80% CI bounds (never extrapolate beyond model confidence)

In auto mode with confidence >= 0.4, the suggested value replaces `growth_assumptions.annual_growth_rate` in a deep-copied inputs dict. In suggest mode or low-confidence auto, the original inputs are unchanged.

### Vacancy (suggest-only in v1)

When supply risk data is present:

- CAUTION verdict: suggest `+100bps` on average vacancy rate across all segments
- PASS verdict: suggest `+200bps` on average vacancy rate
- PROCEED verdict: no vacancy adjustment suggested

The vacancy suggestion is always reported in the audit trail but never applied to inputs, regardless of `market_automation_level`.

### Metro Slug Resolution

To find the market intel file, the module needs a metro slug. Resolution order:

1. `options.market_intel_path` if provided (explicit override)
2. Infer from `metadata.deal_id` or property address using existing `portfolio._infer_metro()` logic, then map to metro slug convention (e.g., "DFW" -> "dallas_tx")
3. If no metro can be inferred, log warning and return no adjustments

## Audit Trail

Always attached to results when mode != "off":

```json
{
  "market_adjustments": [
    {
      "variable": "annual_growth_rate",
      "original_value": 0.03,
      "suggested_value": 0.022,
      "applied": true,
      "mode": "auto",
      "confidence": 0.72,
      "confidence_label": "high",
      "reason": "Econometric forecast 1.8% YoY (80% CI: -1.2% to 4.8%). Subject 3.0% below comps. Blended down from 3.0% base.",
      "components": {
        "econometric_delta": -0.012,
        "comp_delta": 0.004,
        "blended_delta": -0.008
      }
    },
    {
      "variable": "vacancy_rate",
      "original_value": 0.05,
      "suggested_value": 0.06,
      "applied": false,
      "mode": "suggest",
      "confidence": 0.55,
      "reason": "Supply risk CAUTION (score 62). Deliveries 3.4% of inventory vs 2.8% absorption."
    }
  ],
  "market_intel_summary": {
    "metro_slug": "dallas_tx",
    "intel_as_of": "2026-04-11",
    "composite_confidence": 0.72,
    "forecast_vintage": "2025Q3",
    "supply_verdict": "CAUTION",
    "comp_count": 8
  }
}
```

### Audit Trail Fields

Each `market_adjustments[]` entry:

- `variable`: string, the assumption being adjusted
- `original_value`: number, value from deal inputs
- `suggested_value`: number, market-adjusted value
- `applied`: boolean, whether the adjustment was actually applied to inputs
- `mode`: "auto" | "suggest", the effective mode for this variable
- `confidence`: float 0-1, composite confidence score
- `confidence_label`: "high" | "medium" | "low"
- `reason`: human-readable explanation string
- `components`: object with delta breakdown (rent growth only)

## Integration Points

### CLI

`runs/run_from_excel.py` and `runs/generate_rediq_workbook.py`:

- `--market auto|suggest|off` flag (default: off)
- `--market-intel <path>` override for intel JSON path
- In suggest/auto mode, prints adjustment summary table to stdout before engine run
- In auto mode, prints applied adjustments after engine run

### API

`engine/api.py` `handle_run_deal()`:

- Reads `options.market_automation_level` from request
- Calls market automation layer before `run_underwriting()`
- Attaches `market_adjustments` and `market_intel_summary` to response

### Existing Modules

- `engine/market_integration.py`: unchanged. `market_automation.py` imports `suggest_rent_growth_adjustment()` and `comp_comparison()` from it.
- `engine/engine.py`: unchanged. No modifications to `run_underwriting()`.

## Backward Compatibility

- `options` not present in inputs: no market adjustments, identical to current behavior
- `options.market_automation_level: "off"`: no market adjustments, identical to current behavior
- `market_adjustments` key only appears in results when mode != "off"
- No changes to any existing test expectations

## File Changes

### New Files

- `engine/market_automation.py` — orchestration module (~200 lines)
- `tests/test_market_automation.py` — 12+ tests
- `data/market_intel/.gitkeep` — directory placeholder
- `data/market_intel/example_dallas_tx.json` — example intel file (not gitignored)

### Modified Files

- `engine/schemas/deal_schema_v0_1.json` — add `options` property
- `runs/run_from_excel.py` — `--market` and `--market-intel` flags
- `runs/generate_rediq_workbook.py` — `--market` and `--market-intel` flags
- `engine/api.py` — market automation in `handle_run_deal()`

## Test Plan (12+ tests)

### Confidence Scoring (4 tests)

1. Full signal agreement, good data: composite >= 0.7
2. Low comp count (1-2 comps): composite drops, comp depth component low
3. Stale snapshot (>90 days): recency decay applied
4. Opposing signals (comps say up, forecast says down): agreement component = 0.3

### Mode Behavior (3 tests)

5. Auto mode with high confidence: adjustment applied, `applied: true`
6. Suggest mode: adjustment computed but not applied, `applied: false`
7. Off mode: no `market_adjustments` in results

### Vacancy Suggest (2 tests)

8. CAUTION verdict: +100bps suggested, not applied
9. PASS verdict: +200bps suggested, not applied

### Edge Cases (3 tests)

10. Auto mode with low confidence (<0.4): falls back to suggest, `applied: false`
11. Missing market intel file: graceful no-op, no adjustments
12. Backward compat: no `options` in inputs, results identical

## Non-Goals

- No real-time market data fetching (batch export pattern only)
- No submarket-level granularity in v1 (metro-level forecasts only)
- No auto vacancy adjustment in v1 (suggest-only)
- No exit cap rate adjustment (too sensitive for automation)
- No OpEx growth adjustment (no external signal source)
- No export scripts in this task (those live in sibling repos)
