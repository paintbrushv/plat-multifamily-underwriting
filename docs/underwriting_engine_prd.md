# `underwriting_engine_prd.md`

```markdown
# Stack Underwriting Engine PRD
## Multifamily Value-Add / Lease-Up / Repositioning

---

## 1. Purpose

This PRD defines the system that implements **Canonical Deal Schema v0.1** to produce
deterministic, auditable underwriting outputs while preserving Excel as the primary
human interface.

The system is designed to:
- eliminate silent Excel errors
- preserve partner adoption
- create a permanent audit trail
- scale from messy value-add to institutional reporting
- transition cleanly from local (Path 3) to web-based (Path 2) execution

---

## 2. Non-Goals (Explicit)

The system will NOT:
- replace Excel as the primary UI
- model fee-like income by default (late fees, NSF, fines)
- embed waterfall logic in property underwriting
- attempt unit-level monthly modeling everywhere
- optimize for prettiness over correctness

---

## 3. High-Level Architecture

### Phase 3 (Initial Target State)

```

Excel (UI + Tables)
↓
Canonical JSON Snapshot
↓
Deterministic Engine
↓
Validated Outputs (JSON)
↓
Power Query → Excel Outputs

```

### Phase 2 (Later, Optional)

```

Excel (UI)
↓ HTTPS POST
Web Engine (same logic)
↓
Outputs + Snapshots

```

**Core principle:**  
> The engine never changes. Only the transport does.

---

## 4. Core System Components

### 4.1 Excel Front-End (UI Layer)

**Responsibilities**
- Host all user inputs as tables
- Provide baseline templates (opex, revenue programs, cohorts)
- Display outputs
- Surface validation status (“Model Health”)

**Constraints**
- No core math in Excel
- No hidden formulas driving results
- All inputs must map 1-for-1 to schema fields

**Key Features**
- Named tables matching schema objects
- Locked calculation sheets
- Read-only output sheets
- Human-readable checks tab

---

### 4.2 Canonical Deal Snapshot

**Purpose**
- Immutable record of each underwriting run

**Artifacts (per run)**
- `inputs.json`
- `outputs.json`
- `validation_report.json`
- `metadata.json`

These files are:
- versioned
- timestamped
- diff-able
- auditable

---

### 4.3 Deterministic Underwriting Engine

**Responsibilities**
- Consume `inputs.json` (Canonical Deal Schema v0.1)
- Enforce validation rules
- Compute all revenues, opex, capex, debt, and cashflows
- Produce structured outputs

**Design Requirements**
- Pure functions (no hidden state)
- Deterministic (same inputs → same outputs)
- Schema-version aware
- Fully testable

**Explicitly Separate Modules**
- Revenue engine
- Opex engine
- Capex & lease-up schedules
- Debt engine
- Cashflow aggregator
- Metrics & sensitivities

---

### 4.4 Validation & QA Layer

Validation happens **before** computation.

**Types of Validation**
- Schema validation (required fields, types)
- Time grid consistency
- Range checks (rates ∈ [0,1])
- Capacity enforcement
- Monthly ↔ annual reconciliation

**Outputs**
- PASS / FAIL status
- Warning vs error classification
- Human-readable explanation

Excel must display:
> “Model Health: GREEN / YELLOW / RED”

---

### 4.5 Output Layer

**Primary Outputs**
- Monthly cashflows
- Annual cashflows
- Revenue breakdown (rent vs programs)
- Opex breakdown
- NOI
- DSCR
- IRR / Equity Multiple
- Sensitivities

**Secondary Outputs**
- Assumption deltas vs prior run
- Contribution analysis (what changed returns)

---

## 5. GitHub Repo Structure (Authoritative)

```

/underwriting-engine
│
├── /docs
│   ├── canonical_deal_schema_v0_1.md
│   ├── underwriting_engine_prd.md
│   ├── validation_rules.md
│   └── architecture_notes.md
│
├── /excel
│   ├── underwriting_template.xlsx
│   ├── table_contract.md
│   └── example_inputs.xlsx
│
├── /engine
│   ├── /schemas
│   │   └── deal_schema_v0_1.json
│   │
│   ├── /modules
│   │   ├── revenue.py
│   │   ├── revenue_programs.py
│   │   ├── opex.py
│   │   ├── capex.py
│   │   ├── debt.py
│   │   ├── cashflow.py
│   │   └── metrics.py
│   │
│   ├── validator.py
│   ├── engine.py
│   └── version.py
│
├── /tests
│   ├── test_revenue.py
│   ├── test_programs.py
│   ├── test_opex.py
│   ├── test_capex.py
│   └── test_end_to_end.py
│
├── /runs
│   └── /examples
│       ├── deal_001_run_001/
│       │   ├── inputs.json
│       │   ├── outputs.json
│       │   └── validation.json
│
└── README.md

```

---

## 6. Excel ↔ Engine Contract

### Input Contract
- Each Excel table maps to exactly one schema table
- Column names must match schema field names
- No implicit defaults (engine applies defaults explicitly)

### Output Contract
- Outputs returned as structured tables
- Power Query handles ingestion
- Excel never recomputes results

---

## 7. AI Agent Instructions (Critical)

The AI agent must:

1. **Read and respect**
   - `canonical_deal_schema_v0_1.md`
   - `underwriting_engine_prd.md`

2. **Scaffold repo exactly as specified**
   - No deviations in folder structure
   - No missing modules

3. **Implement engine logic in layers**
   - Validation first
   - Revenue → Opex → Capex → Debt → Cashflow

4. **Write tests before features**
   - Each module must have unit tests
   - One end-to-end test per example deal

5. **Avoid UI assumptions**
   - No hard-coding Excel behavior
   - All logic must live in engine

6. **Version everything**
   - Schema version
   - Engine version
   - Run version

---

## 8. Sequencing & Milestones

### Milestone 1
- Repo scaffolded
- Schema validator implemented
- Example deal runs end-to-end

### Milestone 2
- Excel template wired to engine
- Power Query ingestion
- Model Health indicator live

### Milestone 3
- Sensitivity engine
- Assumption diffing
- IC-ready outputs

### Milestone 4 (Optional)
- Web API wrapper
- Auth & permissions
- Multi-deal comparison UI

---

## 9. Risks & Mitigations

| Risk | Mitigation |
|-----|-----------|
| Partner rejects workflow | Excel remains primary UI |
| Schema creep | Versioned schema, PR-based changes |
| Silent math drift | Deterministic engine + tests |
| Over-engineering | Annual-first, monthly where material |

---

## 10. Success Criteria

This system is successful if:

- A partner can underwrite a messy deal **without leaving Excel**
- Two analysts produce identical outputs from the same inputs
- Every IC number can be traced to a table row
- RedIQ parity is met or exceeded
- Waterfalls can be layered later without refactoring

---

## End of PRD
```
