# Task 1.7 — Unit-Level Renovation Scheduling

**Date:** 2026-04-19
**Status:** Approved
**Complexity:** M

---

## Summary

Add optional unit-level renovation tracking alongside existing cohort-level programs. Unit overrides let you schedule specific units (e.g., "Unit 301 in month 6") with per-unit cost, premium, and downtime, while the rest of the cohort follows the program-level pace. Includes seasonal downtime multipliers and a sidecar for rich plat-costmodel metadata.

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Pool subtraction | Upfront (Option A) | Overridden units removed from cohort pool before programs run; prevents double-counting |
| Seasonal multiplier target | Downtime days (not pace) | Per-unit turn time stretches in winter; crew throughput is separately constrained |
| Seasonal scope | Both programs and unit overrides | Seasonality is environmental, not scheduling-method-dependent |
| Interface richness | Thin + sidecar (Option C) | Engine stays simple; output formatters optionally render plat-costmodel detail |

## Schema Changes

### New top-level array: `unit_renovations[]`

```json
{
  "unit_renovations": [
    {
      "unit_id": "301",
      "cohort_id": "1B_classic",
      "renovation_month": "2026-06",
      "scope": "standard_value_add",
      "cost": 14500,
      "expected_premium": 200,
      "downtime_days": 21
    }
  ]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `unit_id` | string | Yes | Unique unit identifier |
| `cohort_id` | string | Yes | Which cohort this unit belongs to (for pool subtraction) |
| `renovation_month` | month | Yes | When the renovation occurs |
| `scope` | string | Yes | Label (e.g., "standard_value_add", "light") — informational, not used by engine |
| `cost` | number | Yes | Total renovation cost for this unit |
| `expected_premium` | number | Yes | Monthly rent premium after renovation |
| `downtime_days` | integer | No | Base downtime days. If omitted, inherits from matching program's `downtime_days` (or defaults to 21) |

### New field on `renovation_programs[]`: `seasonal_downtime_multipliers`

```json
{
  "seasonal_downtime_multipliers": {
    "01": 1.3, "02": 1.3,
    "05": 0.8, "06": 0.8, "07": 0.8, "08": 0.8,
    "11": 1.3, "12": 1.3
  }
}
```

- Optional object on each renovation program
- Keys: zero-padded month numbers `"01"` through `"12"`
- Values: multiplier applied to `downtime_days`
- Missing months default to `1.0`
- Applies to both program-scheduled units and unit-level overrides whose `cohort_id` matches the program's `target_cohort`

### New optional top-level object: `renovation_detail`

```json
{
  "renovation_detail": {
    "301": {
      "line_items": [
        {"category": "flooring", "low": 1200, "high": 1800, "material_spec": "LVP 7mm rigid core"},
        {"category": "kitchen", "low": 3500, "high": 5000, "material_spec": "Granite counters, shaker cabinets"}
      ],
      "risk_flags": [
        {"flag": "galvanized_pipe", "severity": "warning", "note": "1972 build — budget $2-5K contingency if opening walls"}
      ],
      "scope_level": "standard_value_add",
      "finish_tier": "upgraded",
      "roi_result": {"clears_threshold": true, "roi_pct": 18.2}
    }
  }
}
```

- Engine ignores this entirely — passthrough sidecar for output formatters
- Keyed by `unit_id` matching entries in `unit_renovations[]`
- Schema validates as `additionalProperties: true` object (no strict shape)
- Populated by plat-costmodel's `estimate_unit()` output or manually

## Engine Logic

### `compute_renovations()` changes

1. **New parameter**: `unit_renovations: List[Dict] = []`

2. **Pool subtraction** (before program loop):
   ```
   for unit_reno in unit_renovations:
       cohort_pool[unit_reno["cohort_id"]] -= 1
   ```

3. **Unit override processing** (within the month loop):
   - For each month, check if any unit overrides are scheduled
   - Each override produces a `RenovationMonthResult` with `program_id = "__unit_override__"`
   - Seasonal multiplier lookup: find the program targeting this unit's `cohort_id`; if found, use its `seasonal_downtime_multipliers`; otherwise `1.0`
   - `effective_downtime = ceil(base_downtime * seasonal_multiplier)`
   - Premium starts the month after `renovation_month` (same as programs)

4. **Seasonal downtime on programs**: Same multiplier logic applied to program-scheduled renovations. The program's own `downtime_days` is the base; multiply by the calendar month's seasonal factor.

### `_build_renovation_context()` changes

- Pass `unit_renovations` through to `compute_renovations()`
- Unit overrides contribute to dynamic unit counts:
  - Target cohort: `-1` starting at `renovation_month` (and all subsequent months)
  - Output cohort: `+1` starting at `renovation_month` (unit moves immediately; premium starts next month via revenue timing)
- Output cohort for unit overrides: inherit from the program targeting `cohort_id`. If no program exists, synthesize `"{cohort_id}_reno"`
- Output cohort entry for unit overrides: `initial_inplace_rent` = cohort rent + `expected_premium`

### `engine.py` top-level changes

- Read `unit_renovations` from inputs (default `[]`)
- Read `renovation_detail` from inputs (pass through to results unchanged)
- Pass `unit_renovations` to `_build_renovation_context()`

## Integration with plat-costmodel

### Current state
- `plat-costmodel/bridge.py` produces cohort-level `renovation_program` dicts → `renovation_programs[]`
- MCP tool: `prepare_renovation_program_tool`

### Future state (not in this task's scope)
- New MCP tool on plat-costmodel: `estimate_unit_renovations` → returns both thin `unit_renovations[]` array and `renovation_detail{}` sidecar
- No Python imports between repos — MCP boundary stays clean
- plat-costmodel can ingest PCA reports, images, written feedback → produce per-unit estimates → bridge to this schema

### Integration surface

| Level | Flow | Interface |
|-------|------|-----------|
| Cohort (existing) | `plat-costmodel.bridge.prepare_renovation_program()` → `renovation_programs[]` | Done |
| Unit (new) | plat-costmodel MCP tool → `unit_renovations[]` + `renovation_detail{}` | Future plat-costmodel work |

## Test Plan (12 tests)

| # | Test | What it verifies |
|---|------|-----------------|
| 1 | Unit override basic | Single unit override: correct cost, downtime, premium timing |
| 2 | Pool subtraction | Override reduces cohort pool; program sees fewer available units |
| 3 | Mixed mode | 3 unit overrides + program on same cohort; totals correct |
| 4 | Seasonal downtime — winter | January reno gets `ceil(21 * 1.3) = 28` days |
| 5 | Seasonal downtime — summer | June reno gets `ceil(21 * 0.8) = 17` days |
| 6 | Seasonal downtime — default | March (no multiplier) gets `21` days unchanged |
| 7 | Seasonal on unit override | Override without explicit downtime inherits program base x seasonal |
| 8 | Override with explicit downtime | Override's downtime_days used as base, still seasonally adjusted |
| 9 | Output cohort assignment | Unit override moves to correct output cohort |
| 10 | Rollup accuracy | `by_month` aggregation includes both program and unit-override results |
| 11 | Pure unit-override mode | No programs, only overrides — works correctly with synthesized output cohort |
| 12 | Backward compat | No `unit_renovations` → identical output to current behavior |

## Out of Scope

- No plat-costmodel code changes (future MCP tool is separate work)
- No PDF/Excel rendering of `renovation_detail` sidecar (Phase 2 output layer)
- No per-unit cost inflation over time (plat-costmodel's estimator handles this)
- No unit-level lease expiration awareness (data source concern, not engine logic)
- No mono-repo consolidation (deferred; repos stay separate with MCP boundary)
