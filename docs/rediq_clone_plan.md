# RedIQ Clone + Engine Integration — Implementation Plan

## Status: IN PROGRESS

## Context

We pay an annual SaaS license for RedIQ's multifamily underwriting model. The audit confirmed the model is **100% self-contained** — all 70,831 formulas use standard Excel functions, zero external add-in dependencies, zero data connections. The VBA layer (11,479 lines) is entirely UI/cloud sync infrastructure, not calculation logic.

**Goal:** Create a standalone clone that works without RedIQ's SaaS platform, AND wire it to our Python engine for programmatic scenario analysis. Zero loss of fidelity — cashflows must tie to the penny.

---

## Phase 1: Clean Copy & VBA Surgery

**Status:** Script created (`runs/rediq_vba_surgery.py`)

### 1A. Copy & strip cloud VBA
- Copy source .xlsm to `excel/rediq_clone.xlsm`
- Use `oletools` to extract all 37 VBA modules
- **Remove entirely** (4,095 lines / 36% of VBA):
  - `Sync.bas` (464 lines) — cloud sync to `secure.rediq.io`
  - `GA.bas` (46 lines) — Google Analytics tracking
  - `logInform.frm` (288 lines) — login form
  - `WebHelpers.bas` (3,124 lines) — VBA-Web HTTP library
  - `AutoProxy.bas` (43 lines) — proxy detection
  - `FieldLocation.cls` (14 lines) — field location class
- **Keep with edits** (7,384 lines / 64%):
  - `CustomRibbon.bas` — remove cloud-sync button handlers
  - `Module1.bas` — remove upload/sync calls, keep sensitivity helpers
  - `BridgeRefi.bas` — keep 100% (bridge & refi calculation helpers)
  - `InpGeneral.cls` — remove cloud triggers, keep input management
  - All sheet code-behind modules — keep (mostly empty stubs)

### 1B. Validate formula integrity
- Open clone in Excel, verify no #REF!, #NAME? errors
- Check all 1,785 named ranges resolve correctly
- Verify conditional formatting and data validations
- Spot-check key outputs (NOI, IRR, DSCR) match source

---

## Phase 2: Formula Extraction & Named Range Mapping

**Status:** Complete (`engine/rediq_bridge.py`, `docs/rediq_named_range_map.md`)

### 2A. Named range → engine schema mapping
- All 478 Input-sheet named ranges mapped to engine fields
- Documentation: `docs/rediq_named_range_map.md`
- Calculation flow documented: Input → Operating Calculations → CF Calculations → Output

### 2B. Bridge adapter
- `engine/rediq_bridge.py` reads RedIQ named ranges
- Maps to canonical `tbl_*` table format
- Outputs valid `inputs.json` for the engine
- Supports CLI: `python -m engine.rediq_bridge excel/rediq_clone.xlsm -o inputs.json`

---

## Phase 3: Engine Output → RedIQ Output Tabs

**Status:** Complete (`engine/rediq_output.py`)

### 3A. Output mapping
- Engine outputs mapped to CF Calculations sheet cells
- Annual data → columns E-N (years 1-10)
- Monthly data → columns S-EI (months 1-120)
- Summary metrics → GenValidation, Summary sheets

### 3B. Output writer
- `engine/rediq_output.py` writes engine results into RedIQ clone
- Preserves all formatting
- Supports CLI: `python -m engine.rediq_output outputs.json excel/rediq_clone.xlsm`

---

## Phase 4: Cashflow Validation (Penny-Perfect)

**Status:** Script created (`runs/rediq_validate_cashflows.py`)

### 4A. Baseline extraction
- `--extract-baseline` reads every calculated value from source
- Stores as JSON: `tests/rediq_baseline_values.json`

### 4B. Clone vs source comparison
- `--compare-clone` compares every cell to baseline
- Tolerance: ±$0.01 (penny-perfect)
- Reports mismatches with cell addresses and deltas

### 4C. Engine cross-validation
- `--engine-crossval` runs engine with bridge-extracted inputs
- Compares engine outputs to RedIQ calculated values
- Documents expected methodology differences

---

## Phase 5: VBA Ribbon & Local Automation (Future)

### 5A. Local ribbon
- Rewire CustomRibbon for local actions:
  - "Run Engine" → Python engine via Shell
  - "Refresh Outputs" → re-reads outputs.json
  - "Export JSON" → exports inputs to JSON
  - "Validate" → runs engine validator

### 5B. One-click workflow
- VBA macro: save → run engine → load outputs → navigate to Summary
- Keyboard shortcut assignment

---

## Files Created

| File | Purpose | Status |
|------|---------|--------|
| `runs/rediq_vba_surgery.py` | VBA surgery script | ✅ Created |
| `engine/rediq_bridge.py` | Named range → tbl_* adapter | ✅ Created |
| `engine/rediq_output.py` | Engine outputs → RedIQ cells writer | ✅ Created |
| `runs/rediq_validate_cashflows.py` | Penny-perfect validation | ✅ Created |
| `docs/rediq_named_range_map.md` | Named range mapping docs | ✅ Created |
| `docs/rediq_clone_plan.md` | This plan (persistent reference) | ✅ Created |
| `excel/rediq_clone.xlsm` | Standalone clone | ⏳ Run surgery script |
| `tests/rediq_baseline_values.json` | Baseline values | ⏳ Run validation |

---

## Verification Checklist

- [ ] **Formula integrity**: Open clone in Excel → no errors, all outputs match source
- [ ] **Cashflow tie-out**: Automated script compares every output cell → zero delta
- [ ] **Engine round-trip**: Extract inputs via bridge → run engine → compare to RedIQ
- [ ] **VBA functionality**: Ribbon buttons work locally without RedIQ cloud
- [ ] **Named ranges**: All 1,785 defined names resolve correctly in clone

---

## Execution Order

```
1. python runs/rediq_vba_surgery.py --dry-run      # Preview VBA changes
2. python runs/rediq_vba_surgery.py                 # Create clone
3. python runs/rediq_validate_cashflows.py --extract-baseline  # Baseline
4. python runs/rediq_validate_cashflows.py --compare-clone     # Validate clone
5. python -m engine.rediq_bridge excel/rediq_clone.xlsm -o /tmp/inputs.json  # Test bridge
6. python runs/rediq_validate_cashflows.py --engine-crossval   # Cross-validate
```
