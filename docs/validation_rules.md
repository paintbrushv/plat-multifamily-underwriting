# Validation Rules (v0.1)

This document defines engine-side validation checks that run **before** any calculation logic.

## 1. Schema Validation (Hard Fail)

- Input JSON must validate against `engine/schemas/deal_schema_v0_1.json`.
- Unknown fields are rejected.
- Required fields must be present and correctly typed.

## 2. Time Grid Validation (Hard Fail)

- `analysis_start_date` and `analysis_end_date` must resolve to month starts.
- `analysis_end_date` must be >= `analysis_start_date`.
- All `start_period` / `end_period` fields must resolve to months within the analysis grid.
- All segment ranges must satisfy `start_period <= end_period`.

## 3. Referential Integrity (Hard Fail)

- All `cohort_id` references must exist in `unit_cohorts`.
- All `program_id` references must exist in `revenue_programs`.

## 4. Segment Integrity (Hard Fail)

- For each curve keyed by (`cohort_id`) or (`program_id`), segment ranges must not overlap.
- Curves required for calculation must fully cover the months they are expected to apply to:
  - `market_rent_curve` covers all months for each cohort.
  - `loss_to_lease` covers all months for each cohort.
  - `physical_vacancy_curve` covers all months for each cohort.
  - `program_adoption_curve` covers all months a program is active.
  - `collection_loss_curve` covers all months for each `applies_to` bucket used.

## 5. Range Checks (Hard Fail)

- `ltl_percent`, `vacancy_rate`, `loss_rate`, `adoption_rate` must be in `[0, 1]`.
- Money inputs must be non-negative unless a future schema version explicitly allows negatives.

