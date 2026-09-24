# Portfolio Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone web dashboard for portfolio-level analytics, deal drill-down, and interactive stress testing — replacing the planned Power BI integration (Task 3.4).

**Architecture:** Vanilla JS dashboard (matching scenario dashboard pattern) served as static HTML, backed by two new Azure Function endpoints that read from Cosmos DB. Persistence becomes default (not opt-in) with run tagging. Shared CSS extracted from scenario dashboard into a common file both pages import.

**Tech Stack:** Vanilla JS, Canvas API (charts), Azure Functions (Python), Cosmos DB, existing engine Portfolio class + stress testing methods.

**Spec:** `docs/superpowers/specs/2026-04-20-portfolio-dashboard-design.md`

---

### Task 1: Extract Shared CSS

**Files:**
- Create: `frontend/shared/styles.css`
- Modify: `frontend/scenario-dashboard/index.html:1-15` (add stylesheet link, remove extracted CSS)

- [ ] **Step 1: Create the shared styles directory and file**

Create `frontend/shared/styles.css` with the design tokens, reset, and shared component styles extracted from the scenario dashboard. These are lines 13-49 (`:root` variables), 51-68 (reset/body/scrollbar), 70-110 (header), 112-135 (data-source badge, header-controls), 137-166 (view switcher), 168-192 (presentation mode button), 194-202 (main layout + presentation override), 336-378 (summary grid, metric cards), 392-418 (panels), 420-470 (filter controls) of `frontend/scenario-dashboard/index.html`.

```css
/* frontend/shared/styles.css — Shared design system for Stack dashboards */

/* ============================================================
   DESIGN TOKENS
   ============================================================ */

:root {
  --bg-primary: #0a0d12;
  --bg-secondary: #12161d;
  --bg-tertiary: #1a1f2a;
  --bg-elevated: #222836;
  --bg-hover: #2a3140;
  --text-primary: #f0f2f5;
  --text-secondary: #a0a8b8;
  --text-tertiary: #6b7280;
  --text-muted: #4a5264;
  --accent-gold: #d4a853;
  --accent-gold-dim: #a68942;
  --accent-gold-glow: rgba(212, 168, 83, 0.15);
  --data-blue: #4a9eff;
  --data-blue-dim: #2d6bb8;
  --data-green: #34d399;
  --data-green-dim: #059669;
  --data-red: #f87171;
  --data-red-dim: #dc2626;
  --data-purple: #a78bfa;
  --data-orange: #fb923c;
  --data-cyan: #22d3ee;
  --border-subtle: rgba(255, 255, 255, 0.06);
  --border-default: rgba(255, 255, 255, 0.1);
  --font-display: 'Fraunces', Georgia, serif;
  --font-body: 'Instrument Sans', -apple-system, sans-serif;
  --font-mono: 'JetBrains Mono', monospace;

  /* Tag colors */
  --tag-base: #4a9eff;
  --tag-upside: #34d399;
  --tag-downside: #fb923c;
  --tag-stress: #f87171;
  --tag-bank: #a78bfa;
  --tag-custom: #22d3ee;

  /* Metro colors */
  --metro-dfw: #4a9eff;
  --metro-austin: #34d399;
  --metro-birmingham: #a78bfa;
  --metro-san-antonio: #fb923c;
  --metro-houston: #22d3ee;
  --metro-other: #6b7280;
}

/* ============================================================
   RESET & BASE
   ============================================================ */

* { box-sizing: border-box; margin: 0; padding: 0; }
html { font-size: 14px; }

body {
  font-family: var(--font-body);
  background: var(--bg-primary);
  color: var(--text-primary);
  line-height: 1.5;
  min-height: 100vh;
}

body.presentation-mode {
  font-size: 18px;
}

::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: var(--bg-secondary); }
::-webkit-scrollbar-thumb { background: var(--bg-hover); border-radius: 4px; }

/* ============================================================
   HEADER
   ============================================================ */

.header {
  padding: 16px 32px;
  border-bottom: 1px solid var(--border-subtle);
  background: linear-gradient(180deg, var(--bg-secondary) 0%, var(--bg-primary) 100%);
  display: flex;
  justify-content: space-between;
  align-items: center;
  position: sticky;
  top: 0;
  z-index: 100;
}

.header-left { display: flex; align-items: center; gap: 16px; }

.logo {
  width: 40px;
  height: 40px;
  background: linear-gradient(135deg, var(--accent-gold) 0%, var(--accent-gold-dim) 100%);
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-family: var(--font-display);
  font-weight: 600;
  font-size: 1.25rem;
  color: var(--bg-primary);
}

.header h1 {
  font-family: var(--font-display);
  font-size: 1.5rem;
  font-weight: 500;
  letter-spacing: -0.02em;
}

.header-subtitle {
  font-size: 0.85rem;
  color: var(--text-tertiary);
  margin-top: 2px;
}

.data-source {
  display: inline-block;
  font-size: 0.7rem;
  font-weight: 600;
  padding: 2px 6px;
  border-radius: 4px;
  margin-left: 8px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.data-source.api {
  background: var(--data-green);
  color: var(--bg-primary);
}
.data-source.demo {
  background: var(--accent-gold);
  color: var(--bg-primary);
}

.header-controls {
  display: flex;
  align-items: center;
  gap: 12px;
}

/* ============================================================
   VIEW SWITCHER
   ============================================================ */

.view-switcher {
  display: flex;
  background: var(--bg-tertiary);
  border-radius: 8px;
  padding: 4px;
  gap: 4px;
}

.view-btn {
  padding: 8px 16px;
  background: transparent;
  border: none;
  border-radius: 6px;
  color: var(--text-tertiary);
  font-size: 0.85rem;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s ease;
}

.view-btn:hover { color: var(--text-secondary); }
.view-btn.active {
  background: var(--bg-hover);
  color: var(--text-primary);
}

/* ============================================================
   PRESENTATION MODE
   ============================================================ */

.present-btn {
  padding: 8px 16px;
  background: var(--bg-tertiary);
  border: 1px solid var(--border-default);
  border-radius: 8px;
  color: var(--text-secondary);
  font-size: 0.85rem;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 6px;
  transition: all 0.2s ease;
}

.present-btn:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.present-btn.active {
  background: var(--accent-gold);
  color: var(--bg-primary);
  border-color: var(--accent-gold);
}

/* ============================================================
   MAIN LAYOUT
   ============================================================ */

.main {
  padding: 24px 32px;
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.presentation-mode .main { padding: 32px 48px; gap: 32px; }

/* ============================================================
   SUMMARY GRID & METRIC CARDS
   ============================================================ */

.summary-grid {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 16px;
}

.metric-card {
  background: var(--bg-secondary);
  border: 1px solid var(--border-subtle);
  border-radius: 12px;
  padding: 20px;
  transition: all 0.2s ease;
}

.metric-card.highlight {
  background: var(--accent-gold-glow);
  border-color: var(--accent-gold-dim);
}

.metric-label {
  font-size: 0.7rem;
  color: var(--text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  margin-bottom: 8px;
}

.metric-value {
  font-family: var(--font-display);
  font-size: 1.75rem;
  font-weight: 500;
  letter-spacing: -0.02em;
}

.metric-card.highlight .metric-value { color: var(--accent-gold); }

.metric-sub {
  font-size: 0.8rem;
  color: var(--text-tertiary);
  margin-top: 4px;
  font-family: var(--font-mono);
}

/* ============================================================
   PANELS
   ============================================================ */

.panel {
  background: var(--bg-secondary);
  border: 1px solid var(--border-subtle);
  border-radius: 16px;
  overflow: hidden;
}

.panel-header {
  padding: 16px 20px;
  border-bottom: 1px solid var(--border-subtle);
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.panel-title {
  font-family: var(--font-display);
  font-size: 1rem;
  font-weight: 500;
}

.panel-subtitle {
  font-size: 0.8rem;
  color: var(--text-tertiary);
}

.panel-body { padding: 20px; }

/* ============================================================
   CONTENT GRID (3-column layout)
   ============================================================ */

.content-grid {
  display: grid;
  grid-template-columns: 280px 1fr 380px;
  gap: 20px;
}

.sidebar { display: flex; flex-direction: column; gap: 16px; }
.main-content { display: flex; flex-direction: column; gap: 16px; }
.right-panel { display: flex; flex-direction: column; gap: 16px; }

/* ============================================================
   FILTER CONTROLS
   ============================================================ */

.filter-group { margin-bottom: 16px; }

.filter-header {
  display: flex;
  justify-content: space-between;
  margin-bottom: 6px;
  font-size: 0.8rem;
}

.filter-label { color: var(--text-tertiary); }
.filter-value { font-family: var(--font-mono); color: var(--text-secondary); }

.filter-sliders { display: flex; gap: 8px; }
.filter-sliders input[type="range"] {
  flex: 1;
  accent-color: var(--data-blue);
  cursor: pointer;
}

.filter-inputs {
  display: flex;
  gap: 6px;
  margin-top: 6px;
}

.filter-input-group {
  display: flex;
  flex-direction: column;
  flex: 1;
}

.filter-input-label {
  font-size: 0.65rem;
  color: var(--text-tertiary);
  margin-bottom: 2px;
}

.filter-input {
  width: 100%;
  padding: 6px 8px;
  background: var(--bg-tertiary);
  border: 1px solid var(--border-default);
  border-radius: 6px;
  color: var(--text-primary);
  font-size: 0.8rem;
  font-family: var(--font-mono);
}

.filter-input:focus {
  outline: none;
  border-color: var(--data-blue);
}

.filter-input::placeholder {
  color: var(--text-tertiary);
}

/* ============================================================
   LOADING
   ============================================================ */

.loading-overlay {
  position: fixed;
  inset: 0;
  background: var(--bg-primary);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}

.loading-logo {
  width: 64px;
  height: 64px;
  background: linear-gradient(135deg, var(--accent-gold) 0%, var(--accent-gold-dim) 100%);
  border-radius: 16px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-family: var(--font-display);
  font-weight: 600;
  font-size: 2rem;
  color: var(--bg-primary);
  margin-bottom: 24px;
}

.loading-bar {
  width: 200px;
  height: 3px;
  background: var(--bg-tertiary);
  border-radius: 2px;
  overflow: hidden;
  margin-bottom: 16px;
}

.loading-fill {
  height: 100%;
  background: var(--accent-gold);
  border-radius: 2px;
  transition: width 0.3s ease;
}

.loading-text {
  color: var(--text-tertiary);
  font-size: 0.85rem;
}
```

- [ ] **Step 2: Refactor scenario dashboard to import shared CSS**

In `frontend/scenario-dashboard/index.html`, add the stylesheet link after the Google Fonts link (line 11) and before the `<style>` tag (line 13):

```html
  <link rel="stylesheet" href="../shared/styles.css">
```

Then remove the following CSS blocks from the `<style>` tag in `index.html` (they are now in `styles.css`):
- Lines 14-49: `:root` variables
- Lines 51-68: `*`, `html`, `body`, `body.presentation-mode`, scrollbar
- Lines 70-135: `.header` through `.header-controls`
- Lines 137-166: `.view-switcher` through `.view-btn` active states
- Lines 168-192: `.present-btn` through `.present-btn.active`
- Lines 194-202: `.main` and `.presentation-mode .main`
- Lines 336-378: `.summary-grid` through `.metric-sub`
- Lines 380-418: `.content-grid` through `.panel-body`
- Lines 420-470: `.filter-group` through `.filter-input::placeholder`

Keep ALL other CSS in `index.html` (key scenario slots, scenario table, tag dropdown, comparison panel, highlight-flash, etc. — these are page-specific).

Also keep the view-btn color overrides that are scenario-specific:
```css
.view-btn.lp.active { background: var(--data-green-dim); }
.view-btn.lender.active { background: var(--data-blue-dim); }
.view-btn.valuation.active { background: var(--accent-gold-dim); }
```

- [ ] **Step 3: Verify scenario dashboard is visually identical**

Open `frontend/scenario-dashboard/index.html` in a browser. Verify:
- Header renders with gold logo, title, data-source badge
- View switcher (LP / Lender / Valuation) renders correctly
- Metric cards display with correct styling
- Panels have rounded corners and subtle borders
- Filter sliders work
- Presentation mode (Cmd+P) toggles font size

- [ ] **Step 4: Commit**

```bash
git add frontend/shared/styles.css frontend/scenario-dashboard/index.html
git commit -m "refactor: extract shared CSS from scenario dashboard into frontend/shared/styles.css"
```

---

### Task 2: Persistence Default and Run Tagging

**Files:**
- Modify: `engine/persistence.py:70-92` (add run_tag to _extract_metadata)
- Modify: `engine/persistence.py:114-142` (add run_tag param to save_deal_run)
- Modify: `engine/api.py:122-135` (flip persist default)
- Modify: `runs/run_from_excel.py:59` (flip --persist to --no-persist)
- Modify: `runs/run_from_excel.py:104-116` (invert condition)
- Modify: `runs/generate_rediq_workbook.py:82-84` (flip --persist to --no-persist)
- Modify: `runs/generate_rediq_workbook.py:182-194` (invert condition)
- Test: `tests/test_persistence_defaults.py`

- [ ] **Step 1: Write failing tests for persistence defaults and run tagging**

Create `tests/test_persistence_defaults.py`:

```python
"""Tests for persistence default behavior and run tagging."""

import pytest
from unittest.mock import patch, MagicMock


class TestPersistenceDefaults:
    """Verify that persistence is on by default."""

    def test_handle_run_deal_persists_by_default(self):
        """handle_run_deal should persist when no_persist is not set."""
        from engine.api import handle_run_deal

        mock_store = MagicMock()
        mock_store.save_deal_run.return_value = {"id": "test__run_1", "run_id": "run_1"}

        minimal_inputs = {
            "metadata": {"deal_id": "test_deal", "as_of_date": "2026-01-01", "analyst": "Test"},
            "unit_cohorts": [{"cohort_id": "A", "unit_count": 100, "market_rent": 1000, "in_place_rent": 950}],
            "growth_assumptions": {"growth_type": "annual_compound", "annual_rent_growth": 0.03},
            "purchase_assumptions": {"purchase_price": 10000000, "closing_costs": 100000, "equity_contribution": 3000000},
            "exit_assumptions": {"exit_month": 60, "exit_cap_rate": 0.06},
            "debt_terms": {"commitment": 7100000, "rate": 0.05, "amort_years": 30, "io_months": 12, "loan_start_month": "2026-01"},
            "opex_table": [{"category": "insurance", "amount": 500, "pricing_type": "$/unit", "annual_growth_rate": 0.03}],
        }

        with patch("engine.api.get_store", return_value=mock_store):
            result = handle_run_deal({"inputs": minimal_inputs})

        assert result["status"] == "success"
        mock_store.save_deal_run.assert_called_once()

    def test_handle_run_deal_skips_when_no_persist(self):
        """handle_run_deal should skip persistence when no_persist=True."""
        from engine.api import handle_run_deal

        minimal_inputs = {
            "metadata": {"deal_id": "test_deal", "as_of_date": "2026-01-01", "analyst": "Test"},
            "unit_cohorts": [{"cohort_id": "A", "unit_count": 100, "market_rent": 1000, "in_place_rent": 950}],
            "growth_assumptions": {"growth_type": "annual_compound", "annual_rent_growth": 0.03},
            "purchase_assumptions": {"purchase_price": 10000000, "closing_costs": 100000, "equity_contribution": 3000000},
            "exit_assumptions": {"exit_month": 60, "exit_cap_rate": 0.06},
            "debt_terms": {"commitment": 7100000, "rate": 0.05, "amort_years": 30, "io_months": 12, "loan_start_month": "2026-01"},
            "opex_table": [{"category": "insurance", "amount": 500, "pricing_type": "$/unit", "annual_growth_rate": 0.03}],
        }

        with patch("engine.api.get_store") as mock_get_store:
            result = handle_run_deal({"inputs": minimal_inputs, "options": {"no_persist": True}})

        assert result["status"] == "success"
        mock_get_store.assert_not_called()

    def test_handle_run_deal_warns_on_missing_cosmos_env(self):
        """Missing Cosmos env vars should warn, not crash."""
        from engine.api import handle_run_deal

        minimal_inputs = {
            "metadata": {"deal_id": "test_deal", "as_of_date": "2026-01-01", "analyst": "Test"},
            "unit_cohorts": [{"cohort_id": "A", "unit_count": 100, "market_rent": 1000, "in_place_rent": 950}],
            "growth_assumptions": {"growth_type": "annual_compound", "annual_rent_growth": 0.03},
            "purchase_assumptions": {"purchase_price": 10000000, "closing_costs": 100000, "equity_contribution": 3000000},
            "exit_assumptions": {"exit_month": 60, "exit_cap_rate": 0.06},
            "debt_terms": {"commitment": 7100000, "rate": 0.05, "amort_years": 30, "io_months": 12, "loan_start_month": "2026-01"},
            "opex_table": [{"category": "insurance", "amount": 500, "pricing_type": "$/unit", "annual_growth_rate": 0.03}],
        }

        with patch("engine.api.get_store", side_effect=EnvironmentError("COSMOS_ENDPOINT not set")):
            result = handle_run_deal({"inputs": minimal_inputs})

        assert result["status"] == "success"
        assert result.get("persisted") is None


class TestRunTagging:
    """Verify run tagging in persistence layer."""

    def test_default_tag_is_exploratory(self):
        """Runs without explicit tag should be tagged 'exploratory'."""
        from engine.persistence import _extract_metadata

        metadata = _extract_metadata({}, {})
        assert metadata["run_tag"] == "exploratory"

    def test_custom_tag_saved(self):
        """Custom run_tag should be preserved in metadata."""
        from engine.persistence import _extract_metadata

        metadata = _extract_metadata({}, {}, run_tag="ic_memo")
        assert metadata["run_tag"] == "ic_memo"

    def test_save_deal_run_includes_tag(self):
        """save_deal_run should include run_tag in the document."""
        from engine.persistence import _extract_metadata

        metadata = _extract_metadata({}, {}, run_tag="lender")
        assert metadata["run_tag"] == "lender"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_persistence_defaults.py -v`
Expected: FAIL — `_extract_metadata` doesn't accept `run_tag`, `handle_run_deal` still uses opt-in persist.

- [ ] **Step 3: Update `_extract_metadata` to support run_tag**

In `engine/persistence.py`, modify `_extract_metadata` (line 70) to accept and include `run_tag`:

```python
def _extract_metadata(
    inputs: Dict[str, Any],
    results: Dict[str, Any],
    run_tag: str = "exploratory",
) -> Dict[str, Any]:
    """Extract summary metadata from engine inputs and results.

    Handles missing/empty inputs gracefully — every field has a safe default.
    """
    meta = inputs.get("metadata", {}) if inputs else {}
    cohorts = inputs.get("unit_cohorts", []) if inputs else []
    purchase = inputs.get("purchase_assumptions", {}) if inputs else {}
    metrics = results.get("metrics", {}) if results else {}
    irr = metrics.get("irr", {})
    em = metrics.get("equity_multiple", {})

    return {
        "analyst": meta.get("analyst", ""),
        "metro": _infer_metro_from_inputs(inputs or {}),
        "units": sum(c.get("unit_count", 0) for c in cohorts),
        "purchase_price": purchase.get("purchase_price", 0),
        "levered_irr": irr.get("levered"),
        "levered_em": em.get("levered"),
        "run_tag": run_tag,
    }
```

- [ ] **Step 4: Update `save_deal_run` to accept run_tag**

In `engine/persistence.py`, modify `save_deal_run` (line 114) to pass `run_tag` through:

```python
    def save_deal_run(
        self,
        deal_id: str,
        run_id: Optional[str] = None,
        inputs: Optional[Dict[str, Any]] = None,
        results: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        run_tag: str = "exploratory",
    ) -> Dict[str, Any]:
        """Save (upsert) a deal run document. Returns the saved document."""
        effective_run_id = run_id or _generate_run_id()
        doc_id = f"{deal_id}__{effective_run_id}"
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        auto_metadata = _extract_metadata(inputs or {}, results or {}, run_tag=run_tag)
        if metadata:
            auto_metadata.update(metadata)

        document = {
            "id": doc_id,
            "deal_id": deal_id,
            "run_id": effective_run_id,
            "timestamp": timestamp,
            "inputs": inputs,
            "results": results,
            "metadata": auto_metadata,
        }

        self._container.upsert_item(document)
        return document
```

- [ ] **Step 5: Flip persistence to default-on in `engine/api.py`**

In `engine/api.py`, replace lines 122-135:

```python
        # Persist to Cosmos by default (skip with no_persist option)
        if not options.get("no_persist"):
            try:
                from engine.persistence import get_store
                store = get_store()
                doc = store.save_deal_run(
                    deal_id=deal_id,
                    run_id=inputs.get("metadata", {}).get("run_id"),
                    inputs=inputs,
                    results=results,
                    run_tag=options.get("run_tag", "exploratory"),
                )
                response["persisted"] = {"id": doc["id"], "run_id": doc["run_id"]}
            except EnvironmentError:
                response["persisted"] = None  # Cosmos not configured — silent skip
```

Add the import at the top of `handle_run_deal` won't change — `get_store` is already lazily imported inside the block.

- [ ] **Step 6: Flip CLI flags in `runs/run_from_excel.py`**

Replace the `--persist` argument (line 59) with:

```python
parser.add_argument("--no-persist", action="store_true", help="Skip saving run to Cosmos DB")
parser.add_argument("--tag", default="exploratory", choices=["exploratory", "ic_memo", "lender", "lp_report"], help="Tag for the run (default: exploratory)")
```

Replace the persistence block (lines 104-116) with:

```python
    if not args.no_persist:
        try:
            from engine.persistence import get_store
            store = get_store()
            doc = store.save_deal_run(
                deal_id=inputs["metadata"].get("deal_id", "unknown"),
                run_id=inputs["metadata"].get("run_id"),
                inputs=inputs,
                results=outputs,
                run_tag=args.tag,
            )
            print(f"  Persisted to Cosmos: {doc['id']} (tag: {args.tag})")
        except EnvironmentError as e:
            print(f"  Warning: {e} (skipping persistence)")
```

- [ ] **Step 7: Flip CLI flags in `runs/generate_rediq_workbook.py`**

Replace the `--persist` argument (lines 82-84) with:

```python
    parser.add_argument("--no-persist", action="store_true", help="Skip saving run to Cosmos DB")
    parser.add_argument("--tag", default="exploratory", choices=["exploratory", "ic_memo", "lender", "lp_report"], help="Tag for the run (default: exploratory)")
```

Replace the persistence block (lines 182-194) with:

```python
    # Persist to Cosmos DB by default
    if not args.no_persist:
        try:
            from engine.persistence import get_store
            store = get_store()
            doc = store.save_deal_run(
                deal_id=inputs.get("metadata", {}).get("deal_id", "unknown"),
                run_id=inputs.get("metadata", {}).get("run_id"),
                inputs=inputs,
                results=results,
                run_tag=args.tag,
            )
            print(f"  Persisted to Cosmos: {doc['id']} (tag: {args.tag})")
        except EnvironmentError as e:
            print(f"  Warning: {e} (skipping persistence)")
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_persistence_defaults.py -v`
Expected: 6 PASS

- [ ] **Step 9: Run full test suite to verify no regressions**

Run: `python3 -m pytest --timeout=60 -x -q`
Expected: All existing tests pass (some persistence tests may need updating if they mock the old `options.get("persist")` pattern).

- [ ] **Step 10: Commit**

```bash
git add engine/persistence.py engine/api.py runs/run_from_excel.py runs/generate_rediq_workbook.py tests/test_persistence_defaults.py
git commit -m "feat: make Cosmos persistence default, add run tagging (exploratory/ic_memo/lender/lp_report)"
```

---

### Task 3: Portfolio Dashboard API Handlers

**Files:**
- Modify: `engine/api.py` (add `handle_portfolio_dashboard` and `handle_portfolio_stress`)
- Test: `tests/test_portfolio_dashboard_api.py`

- [ ] **Step 1: Write failing tests for portfolio dashboard API**

Create `tests/test_portfolio_dashboard_api.py`:

```python
"""Tests for portfolio dashboard API handlers."""

import pytest
from unittest.mock import patch, MagicMock


def _make_minimal_inputs(deal_id, city="Dallas", units=100, purchase_price=10000000):
    """Helper to build minimal valid engine inputs."""
    return {
        "metadata": {"deal_id": deal_id, "as_of_date": "2026-01-01", "analyst": "Test", "city": city},
        "unit_cohorts": [{"cohort_id": "A", "unit_count": units, "market_rent": 1000, "in_place_rent": 950}],
        "growth_assumptions": {"growth_type": "annual_compound", "annual_rent_growth": 0.03},
        "purchase_assumptions": {"purchase_price": purchase_price, "closing_costs": 100000, "equity_contribution": purchase_price * 0.3},
        "exit_assumptions": {"exit_month": 60, "exit_cap_rate": 0.06},
        "debt_terms": {"commitment": int(purchase_price * 0.7), "rate": 0.05, "amort_years": 30, "io_months": 12, "loan_start_month": "2026-01"},
        "opex_table": [{"category": "insurance", "amount": 500, "pricing_type": "$/unit", "annual_growth_rate": 0.03}],
    }


def _make_cosmos_doc(deal_id, city="Dallas", units=100, purchase_price=10000000):
    """Helper to build a Cosmos document with inputs and results."""
    inputs = _make_minimal_inputs(deal_id, city, units, purchase_price)
    from engine.engine import run_underwriting
    results = run_underwriting(inputs)
    return {
        "deal_id": deal_id,
        "run_id": "run_20260101_120000",
        "timestamp": "2026-01-01T12:00:00Z",
        "inputs": inputs,
        "results": results,
        "metadata": {"run_tag": "exploratory", "metro": "DFW"},
    }


class TestPortfolioDashboardHandler:
    """Tests for handle_portfolio_dashboard."""

    def test_happy_path_returns_portfolio_and_stress(self):
        """Should return portfolio summary and default stress results."""
        from engine.api import handle_portfolio_dashboard

        docs = [
            _make_cosmos_doc("deal_a", "Dallas", 100, 10000000),
            _make_cosmos_doc("deal_b", "Austin", 200, 20000000),
        ]

        mock_store = MagicMock()
        mock_store.list_deals.return_value = ["deal_a", "deal_b"]
        mock_store.load_latest.side_effect = lambda did: next((d for d in docs if d["deal_id"] == did), None)

        with patch("engine.api.get_store", return_value=mock_store):
            result = handle_portfolio_dashboard({})

        assert result["status"] == "success"
        assert "portfolio" in result
        assert "stress" in result
        assert result["portfolio"]["total_deals"] == 2
        assert len(result["portfolio"]["deals"]) == 2
        assert "by_metro" in result["portfolio"]

    def test_empty_cosmos_returns_zero_deal_portfolio(self):
        """Empty Cosmos should return a valid zero-deal response."""
        from engine.api import handle_portfolio_dashboard

        mock_store = MagicMock()
        mock_store.list_deals.return_value = []

        with patch("engine.api.get_store", return_value=mock_store):
            result = handle_portfolio_dashboard({})

        assert result["status"] == "success"
        assert result["portfolio"]["total_deals"] == 0
        assert result["portfolio"]["deals"] == []

    def test_single_deal_weighted_metrics_equal_deal_metrics(self):
        """With one deal, portfolio-weighted metrics should equal the deal's metrics."""
        from engine.api import handle_portfolio_dashboard

        doc = _make_cosmos_doc("deal_solo", "Dallas", 150, 15000000)

        mock_store = MagicMock()
        mock_store.list_deals.return_value = ["deal_solo"]
        mock_store.load_latest.return_value = doc

        with patch("engine.api.get_store", return_value=mock_store):
            result = handle_portfolio_dashboard({})

        portfolio = result["portfolio"]
        deal = portfolio["deals"][0]
        assert abs(portfolio["weighted_irr"] - deal["levered_irr"]) < 0.001
        assert abs(portfolio["weighted_em"] - deal["levered_em"]) < 0.01

    def test_metro_grouping(self):
        """Deals should land in correct metro groups."""
        from engine.api import handle_portfolio_dashboard

        docs = [
            _make_cosmos_doc("deal_dfw", "Dallas", 100, 10000000),
            _make_cosmos_doc("deal_atx", "Austin", 200, 20000000),
        ]

        mock_store = MagicMock()
        mock_store.list_deals.return_value = ["deal_dfw", "deal_atx"]
        mock_store.load_latest.side_effect = lambda did: next((d for d in docs if d["deal_id"] == did), None)

        with patch("engine.api.get_store", return_value=mock_store):
            result = handle_portfolio_dashboard({})

        by_metro = result["portfolio"]["by_metro"]
        assert "DFW" in by_metro
        assert "Austin" in by_metro
        assert by_metro["DFW"]["deals"] == 1
        assert by_metro["Austin"]["deals"] == 1

    def test_deals_include_cashflow_by_year(self):
        """Each deal in response should include cashflow_by_year for drill-down charts."""
        from engine.api import handle_portfolio_dashboard

        doc = _make_cosmos_doc("deal_cf", "Dallas", 100, 10000000)

        mock_store = MagicMock()
        mock_store.list_deals.return_value = ["deal_cf"]
        mock_store.load_latest.return_value = doc

        with patch("engine.api.get_store", return_value=mock_store):
            result = handle_portfolio_dashboard({})

        deal = result["portfolio"]["deals"][0]
        assert "cashflow_by_year" in deal
        assert len(deal["cashflow_by_year"]) > 0


class TestPortfolioStressHandler:
    """Tests for handle_portfolio_stress."""

    def test_custom_cap_rate_shocks(self):
        """Custom cap rate shocks should produce correct number of results."""
        from engine.api import handle_portfolio_stress

        doc = _make_cosmos_doc("deal_stress", "Dallas", 100, 10000000)

        mock_store = MagicMock()
        mock_store.list_deals.return_value = ["deal_stress"]
        mock_store.load_latest.return_value = doc

        with patch("engine.api.get_store", return_value=mock_store):
            result = handle_portfolio_stress({
                "cap_rate_shocks_bps": [50, 100, 150, 200],
            })

        assert result["status"] == "success"
        assert len(result["stress"]["cap_rate"]["shocks_bps"]) == 4

    def test_custom_rate_shocks(self):
        """Custom rate shocks should be applied correctly."""
        from engine.api import handle_portfolio_stress

        doc = _make_cosmos_doc("deal_rate", "Dallas", 100, 10000000)

        mock_store = MagicMock()
        mock_store.list_deals.return_value = ["deal_rate"]
        mock_store.load_latest.return_value = doc

        with patch("engine.api.get_store", return_value=mock_store):
            result = handle_portfolio_stress({
                "rate_shocks_bps": [50, 100],
            })

        assert result["status"] == "success"
        assert len(result["stress"]["rate"]["shocks_bps"]) == 2

    def test_concentration_thresholds(self):
        """Custom concentration thresholds should detect breaches."""
        from engine.api import handle_portfolio_stress

        # Two deals in DFW — should breach a 40% metro threshold
        docs = [
            _make_cosmos_doc("deal_x", "Dallas", 100, 10000000),
            _make_cosmos_doc("deal_y", "Dallas", 200, 20000000),
        ]

        mock_store = MagicMock()
        mock_store.list_deals.return_value = ["deal_x", "deal_y"]
        mock_store.load_latest.side_effect = lambda did: next((d for d in docs if d["deal_id"] == did), None)

        with patch("engine.api.get_store", return_value=mock_store):
            result = handle_portfolio_stress({
                "concentration_thresholds": {"metro": 0.4},
            })

        assert result["status"] == "success"
        # All deals are DFW, so DFW weight = 1.0, breaches 0.4 threshold
        dfw = result["stress"]["concentration"]["by_metro"]["DFW"]
        assert dfw["breached"] is True

    def test_invalid_shocks_return_error(self):
        """Empty or invalid shock parameters should return 400."""
        from engine.api import handle_portfolio_stress

        mock_store = MagicMock()
        mock_store.list_deals.return_value = []

        with patch("engine.api.get_store", return_value=mock_store):
            result = handle_portfolio_stress({})

        # No deals + no shocks = graceful empty response (not crash)
        assert result["status"] == "success"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_portfolio_dashboard_api.py -v`
Expected: FAIL — `handle_portfolio_dashboard` and `handle_portfolio_stress` don't exist yet.

- [ ] **Step 3: Implement `handle_portfolio_dashboard` in `engine/api.py`**

Add at the end of `engine/api.py`:

```python
def handle_portfolio_dashboard(request: Dict[str, Any]) -> Dict[str, Any]:
    """Get portfolio summary + default stress results for the dashboard.

    Loads all deals from Cosmos DB (latest run each), computes portfolio
    analytics and default stress scenarios.

    Request format:
        {
            "run_tag_filter": "ic_memo",  # Optional: filter by run tag
        }

    Returns:
        {
            "status": "success",
            "portfolio": { total_deals, total_units, total_equity, weighted_irr, weighted_em, by_metro, deals[] },
            "stress": { cap_rate, rate, concentration },
        }
    """
    start = time.time()

    try:
        from engine.persistence import get_store
        from engine.portfolio import Portfolio

        store = get_store()
        deal_ids = store.list_deals()

        portfolio = Portfolio()
        run_tag_filter = request.get("run_tag_filter")

        for deal_id in deal_ids:
            doc = store.load_latest(deal_id)
            if not doc:
                continue
            if run_tag_filter and doc.get("metadata", {}).get("run_tag") != run_tag_filter:
                continue
            inputs = doc.get("inputs")
            results = doc.get("results")
            if inputs and results:
                portfolio.add_deal(inputs, results)

        summary = portfolio.summary() if portfolio.deals else {}

        # Build deal list for drill-down
        deals_list = []
        for i, deal in enumerate(portfolio.deals):
            by_year = []
            if i < len(portfolio._deal_inputs):
                # Get cashflow from the stored results
                for did in deal_ids:
                    doc = store.load_latest(did)
                    if doc and doc.get("inputs", {}).get("metadata", {}).get("deal_id") == deal.deal_id:
                        by_year = doc.get("results", {}).get("cashflow", {}).get("by_year", [])
                        break

            deals_list.append({
                "deal_id": deal.deal_id,
                "name": deal.deal_id.replace("_", " ").title(),
                "metro": deal.metro,
                "units": deal.units,
                "purchase_price": deal.purchase_price,
                "equity": deal.equity,
                "levered_irr": deal.levered_irr,
                "levered_em": deal.levered_em,
                "unlevered_irr": deal.unlevered_irr,
                "dscr_min": getattr(deal, "min_dscr", None),
                "going_in_cap": getattr(deal, "going_in_cap", None),
                "noi_year_1": by_year[0].get("net_operating_income", 0) if by_year else 0,
                "cashflow_by_year": by_year,
            })

        by_metro = portfolio.group_by_metro() if portfolio.deals else {}

        portfolio_data = {
            "total_deals": len(portfolio.deals),
            "total_units": summary.get("total_units", 0),
            "total_equity": summary.get("total_equity", 0),
            "weighted_irr": summary.get("equity_weighted_irr"),
            "weighted_em": summary.get("equity_weighted_em"),
            "by_metro": by_metro,
            "deals": deals_list,
        }

        # Default stress tests (only if we have deals with inputs)
        stress_data = {"cap_rate": {}, "rate": {}, "concentration": {}}
        if portfolio.deals and portfolio._deal_inputs:
            try:
                cap_result = portfolio.stress_test_cap_rate(shocks=[25, 50, 75, 100])
                stress_data["cap_rate"] = {
                    "shocks_bps": [25, 50, 75, 100],
                    "portfolio_irr": [s.get("portfolio", {}).get("levered_irr") for s in cap_result.get("scenarios", [])],
                    "by_deal": {
                        d["deal_id"]: [
                            next((sc["deals"].get(d["deal_id"], {}).get("levered_irr") for sc in cap_result.get("scenarios", []) if sc["shock_bps"] == shock), None)
                            for shock in [25, 50, 75, 100]
                        ]
                        for d in deals_list
                    },
                }
            except Exception:
                pass  # Stress test failure shouldn't block portfolio summary

            try:
                rate_result = portfolio.stress_test_rates(shocks=[100, 200, 300])
                stress_data["rate"] = {
                    "shocks_bps": [100, 200, 300],
                    "portfolio_irr": [s.get("portfolio", {}).get("levered_irr") for s in rate_result.get("scenarios", [])],
                    "by_deal": {
                        d["deal_id"]: [
                            next((sc["deals"].get(d["deal_id"], {}).get("levered_irr") for sc in rate_result.get("scenarios", []) if sc["shock_bps"] == shock), None)
                            for shock in [100, 200, 300]
                        ]
                        for d in deals_list
                    },
                }
            except Exception:
                pass

            try:
                conc_result = portfolio.concentration_risk()
                stress_data["concentration"] = conc_result
            except Exception:
                pass

        return {
            "status": "success",
            "portfolio": portfolio_data,
            "stress": stress_data,
            "elapsed_seconds": round(time.time() - start, 3),
        }

    except EnvironmentError as e:
        return {
            "status": "error",
            "error": f"Cosmos DB not configured: {e}",
            "elapsed_seconds": round(time.time() - start, 3),
        }
    except Exception as e:
        return {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
            "elapsed_seconds": round(time.time() - start, 3),
        }


def handle_portfolio_stress(request: Dict[str, Any]) -> Dict[str, Any]:
    """Run custom stress scenarios on the portfolio.

    Request format:
        {
            "cap_rate_shocks_bps": [50, 100, 150, 200],
            "rate_shocks_bps": [50, 100, 200, 400],
            "concentration_thresholds": { "metro": 0.5, "vintage": 0.4 }
        }

    Returns:
        { "status": "success", "stress": { cap_rate, rate, concentration } }
    """
    start = time.time()

    try:
        from engine.persistence import get_store
        from engine.portfolio import Portfolio

        store = get_store()
        deal_ids = store.list_deals()

        portfolio = Portfolio()
        for deal_id in deal_ids:
            doc = store.load_latest(deal_id)
            if doc and doc.get("inputs") and doc.get("results"):
                portfolio.add_deal(doc["inputs"], doc["results"])

        stress_data = {"cap_rate": {}, "rate": {}, "concentration": {}}

        cap_shocks = request.get("cap_rate_shocks_bps", [])
        rate_shocks = request.get("rate_shocks_bps", [])
        conc_thresholds = request.get("concentration_thresholds", {})

        if cap_shocks and portfolio.deals and portfolio._deal_inputs:
            try:
                cap_result = portfolio.stress_test_cap_rate(shocks=cap_shocks)
                stress_data["cap_rate"] = {
                    "shocks_bps": cap_shocks,
                    "portfolio_irr": [s.get("portfolio", {}).get("levered_irr") for s in cap_result.get("scenarios", [])],
                    "by_deal": {},
                }
            except Exception:
                pass

        if rate_shocks and portfolio.deals and portfolio._deal_inputs:
            try:
                rate_result = portfolio.stress_test_rates(shocks=rate_shocks)
                stress_data["rate"] = {
                    "shocks_bps": rate_shocks,
                    "portfolio_irr": [s.get("portfolio", {}).get("levered_irr") for s in rate_result.get("scenarios", [])],
                    "by_deal": {},
                }
            except Exception:
                pass

        if conc_thresholds and portfolio.deals:
            try:
                conc_result = portfolio.concentration_risk(thresholds=conc_thresholds)
                stress_data["concentration"] = conc_result
            except Exception:
                pass

        return {
            "status": "success",
            "stress": stress_data,
            "elapsed_seconds": round(time.time() - start, 3),
        }

    except EnvironmentError as e:
        return {
            "status": "error",
            "error": f"Cosmos DB not configured: {e}",
            "elapsed_seconds": round(time.time() - start, 3),
        }
    except Exception as e:
        return {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
            "elapsed_seconds": round(time.time() - start, 3),
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_portfolio_dashboard_api.py -v`
Expected: 9 PASS

- [ ] **Step 5: Commit**

```bash
git add engine/api.py tests/test_portfolio_dashboard_api.py
git commit -m "feat: add portfolio dashboard and stress API handlers"
```

---

### Task 4: Azure Function Endpoints

**Files:**
- Create: `azure-functions/api_portfolio_dashboard/__init__.py`
- Create: `azure-functions/api_portfolio_dashboard/function.json`
- Create: `azure-functions/api_portfolio_stress/__init__.py`
- Create: `azure-functions/api_portfolio_stress/function.json`

- [ ] **Step 1: Create `api_portfolio_dashboard` Azure Function**

Create `azure-functions/api_portfolio_dashboard/__init__.py`:

```python
from __future__ import annotations

import azure.functions as func

from shared.http_json import error_response, json_response, options_response
from shared.vendor import add_vendored_engine_to_path


def main(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return options_response()

    add_vendored_engine_to_path()

    try:
        from engine.api import handle_portfolio_dashboard
    except ImportError as exc:
        return error_response(
            f"Engine import failed: {exc}",
            status_code=500,
            code="ENGINE_IMPORT",
        )

    payload = {}
    try:
        payload = req.get_json()
    except ValueError:
        pass  # GET with no body is fine — uses defaults

    result = handle_portfolio_dashboard(payload)
    status_code = 200 if result.get("status") == "success" else 500
    return json_response(result, status_code=status_code)
```

Create `azure-functions/api_portfolio_dashboard/function.json`:

```json
{
  "scriptFile": "__init__.py",
  "bindings": [
    {
      "authLevel": "anonymous",
      "type": "httpTrigger",
      "direction": "in",
      "name": "req",
      "methods": ["get", "options"],
      "route": "portfolio-dashboard"
    },
    {
      "type": "http",
      "direction": "out",
      "name": "$return"
    }
  ]
}
```

- [ ] **Step 2: Create `api_portfolio_stress` Azure Function**

Create `azure-functions/api_portfolio_stress/__init__.py`:

```python
from __future__ import annotations

import azure.functions as func

from shared.http_json import error_response, json_response, options_response
from shared.vendor import add_vendored_engine_to_path


def main(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return options_response()

    try:
        payload = req.get_json()
    except ValueError:
        return error_response("Request body must be valid JSON.", status_code=400, code="BAD_JSON")

    if not isinstance(payload, dict):
        return error_response("Request body must be a JSON object.", status_code=400, code="BAD_JSON")

    add_vendored_engine_to_path()

    try:
        from engine.api import handle_portfolio_stress
    except ImportError as exc:
        return error_response(
            f"Engine import failed: {exc}",
            status_code=500,
            code="ENGINE_IMPORT",
        )

    result = handle_portfolio_stress(payload)
    status_code = 200 if result.get("status") == "success" else 500
    return json_response(result, status_code=status_code)
```

Create `azure-functions/api_portfolio_stress/function.json`:

```json
{
  "scriptFile": "__init__.py",
  "bindings": [
    {
      "authLevel": "anonymous",
      "type": "httpTrigger",
      "direction": "in",
      "name": "req",
      "methods": ["post", "options"],
      "route": "portfolio-dashboard/stress"
    },
    {
      "type": "http",
      "direction": "out",
      "name": "$return"
    }
  ]
}
```

- [ ] **Step 3: Commit**

```bash
git add azure-functions/api_portfolio_dashboard/ azure-functions/api_portfolio_stress/
git commit -m "feat: add Azure Function endpoints for portfolio dashboard and custom stress"
```

---

### Task 5: Portfolio Dashboard Frontend

**Files:**
- Create: `frontend/portfolio-dashboard/index.html`

This is the largest task. The dashboard is a single HTML file (~1,500 lines) with:
- Shared CSS imported from `../shared/styles.css`
- Page-specific CSS for deal list, metro bar, stress charts, drill-down
- HTML structure: header, summary bar, 3-column content grid
- JavaScript: state management, API fetch, canvas charts, event handlers

- [ ] **Step 1: Create the portfolio dashboard HTML file**

Create `frontend/portfolio-dashboard/index.html` with the full dashboard implementation. The file structure follows the same pattern as the scenario dashboard:

1. `<head>`: Google Fonts link, shared CSS import, page-specific `<style>` block
2. `<body>`: Loading overlay, header, main content (summary bar + 3-column grid)
3. `<script>`: API config, state, formatters, chart renderers, render function, event handlers, init

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Stack | Portfolio Dashboard</title>

  <!-- Fonts -->
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,300;9..144,400;9..144,500;9..144,600&family=Instrument+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">

  <!-- Shared design system -->
  <link rel="stylesheet" href="../shared/styles.css">

  <style>
    /* ============================================================
       PORTFOLIO-SPECIFIC STYLES
       ============================================================ */

    /* Metro bar */
    .metro-bar-container { margin-top: 12px; }
    .metro-bar {
      display: flex;
      height: 8px;
      border-radius: 4px;
      overflow: hidden;
      gap: 2px;
    }
    .metro-bar-segment { transition: width 0.5s ease; min-width: 4px; }
    .metro-bar-segment.dfw { background: var(--metro-dfw); }
    .metro-bar-segment.austin { background: var(--metro-austin); }
    .metro-bar-segment.birmingham { background: var(--metro-birmingham); }
    .metro-bar-segment.san-antonio { background: var(--metro-san-antonio); }
    .metro-bar-segment.houston { background: var(--metro-houston); }
    .metro-bar-segment.other { background: var(--metro-other); }

    .metro-legend {
      display: flex;
      gap: 16px;
      margin-top: 8px;
      flex-wrap: wrap;
    }
    .metro-legend-item {
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 0.75rem;
      color: var(--text-secondary);
    }
    .metro-legend-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
    }

    /* Deal list sidebar */
    .deal-list { max-height: calc(100vh - 280px); overflow-y: auto; }

    .deal-row {
      padding: 12px 16px;
      border-bottom: 1px solid var(--border-subtle);
      cursor: pointer;
      transition: background 0.15s ease;
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .deal-row:hover { background: var(--bg-hover); }
    .deal-row.selected { background: var(--accent-gold-glow); border-left: 3px solid var(--accent-gold); }

    .deal-row-header { display: flex; justify-content: space-between; align-items: center; }
    .deal-name {
      font-weight: 500;
      font-size: 0.9rem;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      max-width: 160px;
    }
    .deal-metro-badge {
      font-size: 0.65rem;
      font-weight: 600;
      padding: 2px 6px;
      border-radius: 4px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .deal-metro-badge.dfw { background: rgba(74, 158, 255, 0.2); color: var(--metro-dfw); }
    .deal-metro-badge.austin { background: rgba(52, 211, 153, 0.2); color: var(--metro-austin); }
    .deal-metro-badge.birmingham { background: rgba(167, 139, 250, 0.2); color: var(--metro-birmingham); }
    .deal-metro-badge.san-antonio { background: rgba(251, 146, 60, 0.2); color: var(--metro-san-antonio); }
    .deal-metro-badge.houston { background: rgba(34, 211, 238, 0.2); color: var(--metro-houston); }
    .deal-metro-badge.other { background: rgba(107, 114, 128, 0.2); color: var(--metro-other); }

    .deal-row-metrics {
      display: flex;
      gap: 16px;
      font-size: 0.8rem;
    }
    .deal-row-metrics span { color: var(--text-secondary); }
    .deal-row-metrics .value { font-family: var(--font-mono); color: var(--text-primary); }

    /* Sort header */
    .sort-header {
      display: flex;
      gap: 8px;
      padding: 8px 16px;
      border-bottom: 1px solid var(--border-default);
    }
    .sort-btn {
      background: none;
      border: none;
      color: var(--text-tertiary);
      font-size: 0.7rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      cursor: pointer;
      padding: 4px 8px;
      border-radius: 4px;
    }
    .sort-btn:hover { color: var(--text-secondary); }
    .sort-btn.active { color: var(--accent-gold); }

    /* Drill-down */
    .drill-down-empty {
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 400px;
      color: var(--text-muted);
      font-size: 1rem;
    }

    .drill-down-header {
      padding: 20px;
      border-bottom: 1px solid var(--border-subtle);
    }
    .drill-down-title {
      font-family: var(--font-display);
      font-size: 1.25rem;
      font-weight: 500;
    }
    .drill-down-link {
      font-size: 0.8rem;
      color: var(--accent-gold);
      text-decoration: none;
      margin-top: 4px;
      display: inline-block;
    }
    .drill-down-link:hover { text-decoration: underline; }

    .drill-down-metrics {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 12px;
      padding: 20px;
    }
    .drill-down-metric {
      background: var(--bg-tertiary);
      border-radius: 8px;
      padding: 12px;
    }
    .drill-down-metric .metric-label { margin-bottom: 4px; }
    .drill-down-metric .metric-value { font-size: 1.25rem; }

    .chart-container {
      padding: 20px;
      border-top: 1px solid var(--border-subtle);
    }
    .chart-container canvas { width: 100%; }

    /* Stress panels */
    .stress-panel { margin-bottom: 12px; }
    .stress-panel-header {
      padding: 12px 16px;
      cursor: pointer;
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid var(--border-subtle);
    }
    .stress-panel-header:hover { background: var(--bg-hover); }
    .stress-panel-title { font-size: 0.85rem; font-weight: 500; }
    .stress-panel-toggle {
      color: var(--text-tertiary);
      font-size: 0.8rem;
      transition: transform 0.2s ease;
    }
    .stress-panel-toggle.open { transform: rotate(180deg); }
    .stress-panel-body { padding: 16px; }
    .stress-panel-body.collapsed { display: none; }

    .customize-toggle {
      padding: 6px 12px;
      background: var(--bg-tertiary);
      border: 1px solid var(--border-default);
      border-radius: 6px;
      color: var(--text-secondary);
      font-size: 0.75rem;
      cursor: pointer;
      margin-bottom: 12px;
    }
    .customize-toggle:hover { background: var(--bg-hover); }
    .customize-toggle.active { background: var(--accent-gold-glow); border-color: var(--accent-gold-dim); color: var(--accent-gold); }

    .customize-panel { margin-bottom: 12px; }
    .customize-panel.hidden { display: none; }

    .rerun-btn {
      padding: 8px 16px;
      background: var(--accent-gold);
      border: none;
      border-radius: 6px;
      color: var(--bg-primary);
      font-weight: 600;
      font-size: 0.8rem;
      cursor: pointer;
      margin-top: 8px;
      width: 100%;
    }
    .rerun-btn:hover { opacity: 0.9; }
    .rerun-btn:disabled { opacity: 0.5; cursor: not-allowed; }

    /* Donut chart */
    .donut-container { display: flex; align-items: center; gap: 20px; }
    .donut-legend {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .donut-legend-item {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 0.8rem;
    }
    .donut-legend-dot { width: 10px; height: 10px; border-radius: 50%; }
    .donut-legend-value { font-family: var(--font-mono); color: var(--text-secondary); }
    .donut-legend-breach { color: var(--data-red); font-weight: 600; }

    /* Metro filter buttons */
    .metro-filters {
      display: flex;
      flex-wrap: wrap;
      gap: 4px;
      padding: 8px 16px;
      border-bottom: 1px solid var(--border-subtle);
    }
    .metro-filter-btn {
      font-size: 0.65rem;
      padding: 3px 8px;
      border-radius: 4px;
      border: 1px solid var(--border-default);
      background: transparent;
      color: var(--text-tertiary);
      cursor: pointer;
    }
    .metro-filter-btn:hover { background: var(--bg-hover); color: var(--text-secondary); }
    .metro-filter-btn.active { background: var(--accent-gold-glow); border-color: var(--accent-gold-dim); color: var(--accent-gold); }

    /* Stress loading indicator */
    .stress-loading {
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--text-tertiary);
      font-size: 0.8rem;
      padding: 12px;
    }
    .stress-spinner {
      width: 14px;
      height: 14px;
      border: 2px solid var(--bg-hover);
      border-top-color: var(--accent-gold);
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
  </style>
</head>
<body>
  <div id="app"></div>

  <script>
    // ============================================================================
    // CONFIG
    // ============================================================================

    const API_CONFIG = {
      baseUrl: '',  // Set to Azure Functions URL (e.g., 'https://func-stack-std-12345.azurewebsites.net')
      dashboardEndpoint: '/api/portfolio-dashboard',
      stressEndpoint: '/api/portfolio-dashboard/stress',
    };

    // ============================================================================
    // STATE
    // ============================================================================

    const state = {
      isLoading: true,
      loadProgress: 0,
      loadingMessage: 'Loading portfolio...',
      dataSource: 'api',

      portfolio: null,   // { total_deals, total_units, total_equity, weighted_irr, weighted_em, by_metro, deals[] }
      stress: null,       // { cap_rate, rate, concentration }

      selectedDeal: null, // deal object from portfolio.deals[]
      sortConfig: { key: 'levered_irr', direction: 'desc' },
      metroFilter: null,  // null = all, 'DFW' = filter

      stressPanels: { cap_rate: true, rate: true, concentration: true },
      customizeOpen: false,
      customShocks: {
        cap_rate_shocks_bps: '25, 50, 75, 100',
        rate_shocks_bps: '100, 200, 300',
        metro_threshold: '0.5',
      },
      stressLoading: false,

      presentationMode: false,
    };

    // ============================================================================
    // FORMATTERS
    // ============================================================================

    const formatPercent = (v, decimals = 1) => v != null ? (v * 100).toFixed(decimals) + '%' : '—';
    const formatMultiple = (v) => v != null ? v.toFixed(2) + 'x' : '—';
    const formatCurrency = (v, compact = false) => {
      if (v == null) return '—';
      if (compact) {
        if (Math.abs(v) >= 1e6) return '$' + (v / 1e6).toFixed(1) + 'M';
        if (Math.abs(v) >= 1e3) return '$' + (v / 1e3).toFixed(0) + 'K';
      }
      return '$' + v.toLocaleString('en-US', { maximumFractionDigits: 0 });
    };
    const formatNumber = (v) => v != null ? v.toLocaleString('en-US') : '—';

    const metroClass = (metro) => (metro || 'other').toLowerCase().replace(/\s+/g, '-');

    const METRO_COLORS = {
      DFW: '#4a9eff',
      Austin: '#34d399',
      Birmingham: '#a78bfa',
      'San Antonio': '#fb923c',
      Houston: '#22d3ee',
      Other: '#6b7280',
    };

    // ============================================================================
    // API
    // ============================================================================

    const fetchDashboardData = async () => {
      const url = API_CONFIG.baseUrl + API_CONFIG.dashboardEndpoint;
      const response = await fetch(url);
      if (!response.ok) throw new Error(`API returned ${response.status}`);
      return response.json();
    };

    const fetchCustomStress = async (params) => {
      const url = API_CONFIG.baseUrl + API_CONFIG.stressEndpoint;
      const response = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(params),
      });
      if (!response.ok) throw new Error(`Stress API returned ${response.status}`);
      return response.json();
    };

    // ============================================================================
    // CHARTS
    // ============================================================================

    const drawStressLineChart = (canvasId, stressData, label) => {
      const canvas = document.getElementById(canvasId);
      if (!canvas || !stressData || !stressData.shocks_bps) return;

      const ctx = canvas.getContext('2d');
      const dpr = window.devicePixelRatio || 1;
      const rect = canvas.parentElement.getBoundingClientRect();
      const width = rect.width - 32;
      const height = 180;
      canvas.width = width * dpr;
      canvas.height = height * dpr;
      canvas.style.width = width + 'px';
      canvas.style.height = height + 'px';
      ctx.scale(dpr, dpr);
      ctx.clearRect(0, 0, width, height);

      const padding = { top: 20, right: 20, bottom: 30, left: 50 };
      const chartW = width - padding.left - padding.right;
      const chartH = height - padding.top - padding.bottom;

      const shocks = stressData.shocks_bps;
      const portfolioIrrs = stressData.portfolio_irr || [];
      const byDeal = stressData.by_deal || {};

      // Collect all IRR values for Y axis range
      let allVals = [...portfolioIrrs.filter(v => v != null)];
      Object.values(byDeal).forEach(arr => arr.forEach(v => { if (v != null) allVals.push(v); }));
      if (!allVals.length) return;

      const yMin = Math.min(...allVals) - 0.01;
      const yMax = Math.max(...allVals) + 0.01;
      const xToPixel = (i) => padding.left + (i / (shocks.length - 1)) * chartW;
      const yToPixel = (v) => padding.top + (1 - (v - yMin) / (yMax - yMin)) * chartH;

      // Grid lines
      ctx.strokeStyle = 'rgba(255,255,255,0.06)';
      ctx.lineWidth = 1;
      for (let i = 0; i <= 4; i++) {
        const y = padding.top + (i / 4) * chartH;
        ctx.beginPath(); ctx.moveTo(padding.left, y); ctx.lineTo(width - padding.right, y); ctx.stroke();
      }

      // Deal lines (muted)
      Object.entries(byDeal).forEach(([dealId, irrs]) => {
        ctx.strokeStyle = 'rgba(255,255,255,0.1)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        irrs.forEach((irr, i) => {
          if (irr == null) return;
          const fn = i === 0 ? 'moveTo' : 'lineTo';
          ctx[fn](xToPixel(i), yToPixel(irr));
        });
        ctx.stroke();
      });

      // Portfolio line (gold, thick)
      ctx.strokeStyle = '#d4a853';
      ctx.lineWidth = 3;
      ctx.beginPath();
      portfolioIrrs.forEach((irr, i) => {
        if (irr == null) return;
        const fn = i === 0 ? 'moveTo' : 'lineTo';
        ctx[fn](xToPixel(i), yToPixel(irr));
      });
      ctx.stroke();

      // Dots on portfolio line
      portfolioIrrs.forEach((irr, i) => {
        if (irr == null) return;
        ctx.fillStyle = '#d4a853';
        ctx.beginPath();
        ctx.arc(xToPixel(i), yToPixel(irr), 4, 0, Math.PI * 2);
        ctx.fill();
      });

      // X axis labels
      ctx.fillStyle = '#6b7280';
      ctx.font = '10px "JetBrains Mono", monospace';
      ctx.textAlign = 'center';
      shocks.forEach((s, i) => {
        ctx.fillText('+' + s + 'bps', xToPixel(i), height - 8);
      });

      // Y axis labels
      ctx.textAlign = 'right';
      for (let i = 0; i <= 4; i++) {
        const val = yMax - (i / 4) * (yMax - yMin);
        ctx.fillText(formatPercent(val), padding.left - 8, padding.top + (i / 4) * chartH + 3);
      }

      // Title
      ctx.fillStyle = '#a0a8b8';
      ctx.font = '11px "Instrument Sans", sans-serif';
      ctx.textAlign = 'left';
      ctx.fillText(label, padding.left, 12);
    };

    const drawDonutChart = (canvasId, concentrationData) => {
      const canvas = document.getElementById(canvasId);
      if (!canvas || !concentrationData) return;

      const byMetro = concentrationData.by_metro || {};
      const entries = Object.entries(byMetro);
      if (!entries.length) return;

      const ctx = canvas.getContext('2d');
      const dpr = window.devicePixelRatio || 1;
      const size = 140;
      canvas.width = size * dpr;
      canvas.height = size * dpr;
      canvas.style.width = size + 'px';
      canvas.style.height = size + 'px';
      ctx.scale(dpr, dpr);
      ctx.clearRect(0, 0, size, size);

      const cx = size / 2;
      const cy = size / 2;
      const outerR = 60;
      const innerR = 38;

      let startAngle = -Math.PI / 2;
      entries.forEach(([metro, data]) => {
        const weight = data.weight || 0;
        const endAngle = startAngle + weight * Math.PI * 2;
        ctx.beginPath();
        ctx.arc(cx, cy, outerR, startAngle, endAngle);
        ctx.arc(cx, cy, innerR, endAngle, startAngle, true);
        ctx.closePath();
        ctx.fillStyle = METRO_COLORS[metro] || METRO_COLORS.Other;
        ctx.fill();
        startAngle = endAngle;
      });

      // Center text
      ctx.fillStyle = '#f0f2f5';
      ctx.font = 'bold 14px "Fraunces", serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(entries.length + '', cx, cy - 6);
      ctx.fillStyle = '#6b7280';
      ctx.font = '9px "Instrument Sans", sans-serif';
      ctx.fillText('metros', cx, cy + 8);
    };

    const drawCashflowChart = (canvasId, cashflowByYear) => {
      const canvas = document.getElementById(canvasId);
      if (!canvas || !cashflowByYear || !cashflowByYear.length) return;

      const ctx = canvas.getContext('2d');
      const dpr = window.devicePixelRatio || 1;
      const rect = canvas.parentElement.getBoundingClientRect();
      const width = rect.width - 40;
      const height = 200;
      canvas.width = width * dpr;
      canvas.height = height * dpr;
      canvas.style.width = width + 'px';
      canvas.style.height = height + 'px';
      ctx.scale(dpr, dpr);
      ctx.clearRect(0, 0, width, height);

      const padding = { top: 20, right: 20, bottom: 30, left: 60 };
      const chartW = width - padding.left - padding.right;
      const chartH = height - padding.top - padding.bottom;
      const n = cashflowByYear.length;
      const barWidth = Math.min(chartW / n - 4, 40);

      const nois = cashflowByYear.map(y => y.net_operating_income || 0);
      const lcfs = cashflowByYear.map(y => y.leveraged_cash_flow || 0);
      const maxVal = Math.max(...nois, ...lcfs.map(Math.abs));
      const minVal = Math.min(0, ...lcfs);

      const yRange = maxVal - minVal;
      const yToPixel = (v) => padding.top + (1 - (v - minVal) / yRange) * chartH;

      // Grid
      ctx.strokeStyle = 'rgba(255,255,255,0.06)';
      for (let i = 0; i <= 4; i++) {
        const y = padding.top + (i / 4) * chartH;
        ctx.beginPath(); ctx.moveTo(padding.left, y); ctx.lineTo(width - padding.right, y); ctx.stroke();
      }

      // Bars
      cashflowByYear.forEach((yr, i) => {
        const x = padding.left + (i / n) * chartW + (chartW / n - barWidth) / 2;
        const noi = yr.net_operating_income || 0;
        const lcf = yr.leveraged_cash_flow || 0;

        // NOI bar (dark blue)
        ctx.fillStyle = '#2d6bb8';
        const noiTop = yToPixel(noi);
        const zeroY = yToPixel(0);
        ctx.fillRect(x, noiTop, barWidth / 2 - 1, zeroY - noiTop);

        // LCF bar (gold)
        ctx.fillStyle = lcf >= 0 ? '#d4a853' : '#f87171';
        const lcfTop = lcf >= 0 ? yToPixel(lcf) : zeroY;
        const lcfH = lcf >= 0 ? zeroY - yToPixel(lcf) : yToPixel(lcf) - zeroY;
        ctx.fillRect(x + barWidth / 2 + 1, lcfTop, barWidth / 2 - 1, Math.abs(lcfH));

        // Year label
        ctx.fillStyle = '#6b7280';
        ctx.font = '10px "JetBrains Mono", monospace';
        ctx.textAlign = 'center';
        ctx.fillText('Y' + (i + 1), x + barWidth / 2, height - 8);
      });

      // Y axis
      ctx.fillStyle = '#6b7280';
      ctx.font = '10px "JetBrains Mono", monospace';
      ctx.textAlign = 'right';
      for (let i = 0; i <= 4; i++) {
        const val = maxVal - (i / 4) * yRange;
        ctx.fillText(formatCurrency(val, true), padding.left - 8, padding.top + (i / 4) * chartH + 3);
      }

      // Legend
      ctx.font = '10px "Instrument Sans", sans-serif';
      ctx.textAlign = 'left';
      ctx.fillStyle = '#2d6bb8';
      ctx.fillRect(padding.left, 4, 10, 10);
      ctx.fillStyle = '#a0a8b8';
      ctx.fillText('NOI', padding.left + 14, 12);
      ctx.fillStyle = '#d4a853';
      ctx.fillRect(padding.left + 50, 4, 10, 10);
      ctx.fillStyle = '#a0a8b8';
      ctx.fillText('Leveraged CF', padding.left + 64, 12);
    };

    // ============================================================================
    // RENDER
    // ============================================================================

    const render = () => {
      const app = document.getElementById('app');

      if (state.isLoading) {
        app.innerHTML = `
          <div class="loading-overlay">
            <div class="loading-logo">S</div>
            <div class="loading-bar"><div class="loading-fill" style="width: ${state.loadProgress}%"></div></div>
            <div class="loading-text">${state.loadingMessage}</div>
          </div>`;
        return;
      }

      const p = state.portfolio || { total_deals: 0, total_units: 0, total_equity: 0, weighted_irr: null, weighted_em: null, by_metro: {}, deals: [] };
      const deals = getFilteredSortedDeals();
      const selected = state.selectedDeal;
      const stress = state.stress || {};

      app.innerHTML = `
        <!-- Header -->
        <div class="header">
          <div class="header-left">
            <div class="logo">S</div>
            <div>
              <h1>Portfolio Dashboard</h1>
              <div class="header-subtitle">${p.total_deals} deals <span class="data-source api">LIVE</span></div>
            </div>
          </div>
          <div class="header-controls">
            <a href="../scenario-dashboard/index.html" class="present-btn">Scenario Analysis</a>
            <button class="present-btn ${state.presentationMode ? 'active' : ''}" onclick="togglePresentationMode()">Present</button>
          </div>
        </div>

        <div class="main">
          <!-- Summary Bar -->
          <div class="summary-grid">
            <div class="metric-card">
              <div class="metric-label">Total Deals</div>
              <div class="metric-value">${p.total_deals}</div>
            </div>
            <div class="metric-card">
              <div class="metric-label">Total Units</div>
              <div class="metric-value">${formatNumber(p.total_units)}</div>
            </div>
            <div class="metric-card">
              <div class="metric-label">Total Equity</div>
              <div class="metric-value">${formatCurrency(p.total_equity, true)}</div>
            </div>
            <div class="metric-card highlight">
              <div class="metric-label">Weighted IRR</div>
              <div class="metric-value">${formatPercent(p.weighted_irr)}</div>
            </div>
            <div class="metric-card">
              <div class="metric-label">Weighted EM</div>
              <div class="metric-value">${formatMultiple(p.weighted_em)}</div>
            </div>
          </div>

          <!-- Metro Bar -->
          ${renderMetroBar(p.by_metro)}

          <!-- Three-Column Content Grid -->
          <div class="content-grid">
            <!-- Left: Deal List -->
            <div class="sidebar">
              <div class="panel">
                <div class="panel-header">
                  <div class="panel-title">Deals</div>
                  <div class="panel-subtitle">${deals.length} of ${p.deals.length}</div>
                </div>
                ${renderMetroFilters(p.by_metro)}
                ${renderSortHeader()}
                <div class="deal-list">
                  ${deals.map(d => renderDealRow(d)).join('')}
                </div>
              </div>
            </div>

            <!-- Center: Drill-Down -->
            <div class="main-content">
              <div class="panel">
                ${selected ? renderDrillDown(selected) : '<div class="drill-down-empty">Select a deal to view details</div>'}
              </div>
            </div>

            <!-- Right: Stress Testing -->
            <div class="right-panel">
              <div class="panel">
                <div class="panel-header">
                  <div class="panel-title">Stress Testing</div>
                  <button class="customize-toggle ${state.customizeOpen ? 'active' : ''}" onclick="toggleCustomize()">
                    ${state.customizeOpen ? 'Hide' : 'Customize'}
                  </button>
                </div>
                <div class="panel-body">
                  ${renderCustomizePanel()}
                  ${renderStressPanel('cap_rate', 'Cap Rate Sensitivity', stress.cap_rate)}
                  ${renderStressPanel('rate', 'Rate Shock', stress.rate)}
                  ${renderConcentrationPanel(stress.concentration)}
                </div>
              </div>
            </div>
          </div>
        </div>`;

      // Draw charts after DOM update
      requestAnimationFrame(() => {
        drawStressLineChart('cap-rate-chart', stress.cap_rate, 'Portfolio IRR vs Cap Rate Expansion');
        drawStressLineChart('rate-chart', stress.rate, 'Portfolio IRR vs Rate Shock');
        drawDonutChart('concentration-chart', stress.concentration);
        if (selected) {
          drawCashflowChart('cashflow-chart', selected.cashflow_by_year);
        }
      });
    };

    // ============================================================================
    // RENDER HELPERS
    // ============================================================================

    const renderMetroBar = (byMetro) => {
      const entries = Object.entries(byMetro || {});
      if (!entries.length) return '';

      const totalEquity = entries.reduce((sum, [, data]) => sum + (data.total_equity || data.equity || 0), 0);
      if (!totalEquity) return '';

      const segments = entries.map(([metro, data]) => {
        const eq = data.total_equity || data.equity || 0;
        const pct = (eq / totalEquity * 100).toFixed(1);
        return `<div class="metro-bar-segment ${metroClass(metro)}" style="width: ${pct}%" title="${metro}: ${pct}%"></div>`;
      }).join('');

      const legend = entries.map(([metro, data]) => {
        const eq = data.total_equity || data.equity || 0;
        const pct = (eq / totalEquity * 100).toFixed(0);
        return `<div class="metro-legend-item">
          <div class="metro-legend-dot" style="background: ${METRO_COLORS[metro] || METRO_COLORS.Other}"></div>
          ${metro} (${pct}%)
        </div>`;
      }).join('');

      return `<div class="metro-bar-container">
        <div class="metro-bar">${segments}</div>
        <div class="metro-legend">${legend}</div>
      </div>`;
    };

    const renderMetroFilters = (byMetro) => {
      const metros = Object.keys(byMetro || {});
      if (!metros.length) return '';

      const buttons = [
        `<button class="metro-filter-btn ${!state.metroFilter ? 'active' : ''}" onclick="setMetroFilter(null)">All</button>`,
        ...metros.map(m =>
          `<button class="metro-filter-btn ${state.metroFilter === m ? 'active' : ''}" onclick="setMetroFilter('${m}')">${m}</button>`
        ),
      ].join('');

      return `<div class="metro-filters">${buttons}</div>`;
    };

    const renderSortHeader = () => {
      const sortIcon = (key) => state.sortConfig.key === key ? (state.sortConfig.direction === 'desc' ? ' v' : ' ^') : '';
      return `<div class="sort-header">
        <button class="sort-btn ${state.sortConfig.key === 'deal_id' ? 'active' : ''}" onclick="handleSort('deal_id')">Name${sortIcon('deal_id')}</button>
        <button class="sort-btn ${state.sortConfig.key === 'levered_irr' ? 'active' : ''}" onclick="handleSort('levered_irr')">IRR${sortIcon('levered_irr')}</button>
        <button class="sort-btn ${state.sortConfig.key === 'levered_em' ? 'active' : ''}" onclick="handleSort('levered_em')">EM${sortIcon('levered_em')}</button>
        <button class="sort-btn ${state.sortConfig.key === 'units' ? 'active' : ''}" onclick="handleSort('units')">Units${sortIcon('units')}</button>
      </div>`;
    };

    const renderDealRow = (deal) => {
      const isSelected = state.selectedDeal?.deal_id === deal.deal_id;
      return `<div class="deal-row ${isSelected ? 'selected' : ''}" onclick="selectDeal('${deal.deal_id}')">
        <div class="deal-row-header">
          <span class="deal-name">${deal.name}</span>
          <span class="deal-metro-badge ${metroClass(deal.metro)}">${deal.metro}</span>
        </div>
        <div class="deal-row-metrics">
          <span>IRR <span class="value">${formatPercent(deal.levered_irr)}</span></span>
          <span>EM <span class="value">${formatMultiple(deal.levered_em)}</span></span>
          <span>${deal.units} units</span>
        </div>
      </div>`;
    };

    const renderDrillDown = (deal) => {
      return `
        <div class="drill-down-header">
          <div class="drill-down-title">${deal.name}</div>
          <a class="drill-down-link" href="../scenario-dashboard/index.html?deal=${deal.deal_id}">Open in Scenario Analysis &rarr;</a>
        </div>
        <div class="drill-down-metrics">
          <div class="drill-down-metric highlight">
            <div class="metric-label">Levered IRR</div>
            <div class="metric-value">${formatPercent(deal.levered_irr)}</div>
          </div>
          <div class="drill-down-metric">
            <div class="metric-label">Levered EM</div>
            <div class="metric-value">${formatMultiple(deal.levered_em)}</div>
          </div>
          <div class="drill-down-metric">
            <div class="metric-label">Unlevered IRR</div>
            <div class="metric-value">${formatPercent(deal.unlevered_irr)}</div>
          </div>
          <div class="drill-down-metric">
            <div class="metric-label">Min DSCR</div>
            <div class="metric-value">${deal.dscr_min ? deal.dscr_min.toFixed(2) + 'x' : '—'}</div>
          </div>
          <div class="drill-down-metric">
            <div class="metric-label">Going-In Cap</div>
            <div class="metric-value">${formatPercent(deal.going_in_cap)}</div>
          </div>
          <div class="drill-down-metric">
            <div class="metric-label">Year 1 NOI</div>
            <div class="metric-value">${formatCurrency(deal.noi_year_1, true)}</div>
          </div>
          <div class="drill-down-metric">
            <div class="metric-label">Purchase Price</div>
            <div class="metric-value">${formatCurrency(deal.purchase_price, true)}</div>
          </div>
          <div class="drill-down-metric">
            <div class="metric-label">Equity</div>
            <div class="metric-value">${formatCurrency(deal.equity, true)}</div>
          </div>
          <div class="drill-down-metric">
            <div class="metric-label">Units</div>
            <div class="metric-value">${formatNumber(deal.units)}</div>
          </div>
        </div>
        <div class="chart-container">
          <canvas id="cashflow-chart"></canvas>
        </div>`;
    };

    const renderStressPanel = (key, title, data) => {
      const isOpen = state.stressPanels[key];
      const hasData = data && data.shocks_bps && data.shocks_bps.length;
      return `<div class="stress-panel">
        <div class="stress-panel-header" onclick="toggleStressPanel('${key}')">
          <span class="stress-panel-title">${title}</span>
          <span class="stress-panel-toggle ${isOpen ? 'open' : ''}">&#9660;</span>
        </div>
        <div class="stress-panel-body ${isOpen ? '' : 'collapsed'}">
          ${state.stressLoading ? '<div class="stress-loading"><div class="stress-spinner"></div>Re-running stress...</div>' :
            hasData ? `<canvas id="${key}-chart"></canvas>` : '<div style="color: var(--text-muted); font-size: 0.8rem;">No stress data available</div>'}
        </div>
      </div>`;
    };

    const renderConcentrationPanel = (data) => {
      const isOpen = state.stressPanels.concentration;
      const byMetro = data?.by_metro || {};
      const entries = Object.entries(byMetro);

      const legend = entries.map(([metro, info]) => {
        const breachClass = info.breached ? 'donut-legend-breach' : '';
        return `<div class="donut-legend-item">
          <div class="donut-legend-dot" style="background: ${METRO_COLORS[metro] || METRO_COLORS.Other}"></div>
          <span>${metro}</span>
          <span class="donut-legend-value ${breachClass}">${(info.weight * 100).toFixed(0)}%${info.breached ? ' !' : ''}</span>
        </div>`;
      }).join('');

      return `<div class="stress-panel">
        <div class="stress-panel-header" onclick="toggleStressPanel('concentration')">
          <span class="stress-panel-title">Concentration Risk</span>
          <span class="stress-panel-toggle ${isOpen ? 'open' : ''}">&#9660;</span>
        </div>
        <div class="stress-panel-body ${isOpen ? '' : 'collapsed'}">
          ${entries.length ? `<div class="donut-container">
            <canvas id="concentration-chart"></canvas>
            <div class="donut-legend">${legend}</div>
          </div>` : '<div style="color: var(--text-muted); font-size: 0.8rem;">No concentration data</div>'}
        </div>
      </div>`;
    };

    const renderCustomizePanel = () => {
      if (!state.customizeOpen) return '';
      return `<div class="customize-panel">
        <div class="filter-group">
          <div class="filter-header"><span class="filter-label">Cap Rate Shocks (bps)</span></div>
          <input class="filter-input" value="${state.customShocks.cap_rate_shocks_bps}" oninput="state.customShocks.cap_rate_shocks_bps = this.value" placeholder="25, 50, 75, 100">
        </div>
        <div class="filter-group">
          <div class="filter-header"><span class="filter-label">Rate Shocks (bps)</span></div>
          <input class="filter-input" value="${state.customShocks.rate_shocks_bps}" oninput="state.customShocks.rate_shocks_bps = this.value" placeholder="100, 200, 300">
        </div>
        <div class="filter-group">
          <div class="filter-header"><span class="filter-label">Metro Threshold</span></div>
          <input class="filter-input" value="${state.customShocks.metro_threshold}" oninput="state.customShocks.metro_threshold = this.value" placeholder="0.5">
        </div>
        <button class="rerun-btn" onclick="rerunStress()" ${state.stressLoading ? 'disabled' : ''}>
          ${state.stressLoading ? 'Running...' : 'Re-run Stress Tests'}
        </button>
      </div>`;
    };

    // ============================================================================
    // DATA HELPERS
    // ============================================================================

    const getFilteredSortedDeals = () => {
      let deals = [...(state.portfolio?.deals || [])];
      if (state.metroFilter) {
        deals = deals.filter(d => d.metro === state.metroFilter);
      }
      const { key, direction } = state.sortConfig;
      deals.sort((a, b) => {
        const aVal = a[key] ?? 0;
        const bVal = b[key] ?? 0;
        if (typeof aVal === 'string') return direction === 'desc' ? bVal.localeCompare(aVal) : aVal.localeCompare(bVal);
        return direction === 'desc' ? bVal - aVal : aVal - bVal;
      });
      return deals;
    };

    // ============================================================================
    // EVENT HANDLERS
    // ============================================================================

    window.selectDeal = (dealId) => {
      const deal = state.portfolio?.deals?.find(d => d.deal_id === dealId);
      state.selectedDeal = state.selectedDeal?.deal_id === dealId ? null : deal;
      render();
    };

    window.handleSort = (key) => {
      if (state.sortConfig.key === key) {
        state.sortConfig.direction = state.sortConfig.direction === 'desc' ? 'asc' : 'desc';
      } else {
        state.sortConfig = { key, direction: 'desc' };
      }
      render();
    };

    window.setMetroFilter = (metro) => {
      state.metroFilter = metro;
      render();
    };

    window.toggleStressPanel = (key) => {
      state.stressPanels[key] = !state.stressPanels[key];
      render();
    };

    window.toggleCustomize = () => {
      state.customizeOpen = !state.customizeOpen;
      render();
    };

    window.togglePresentationMode = () => {
      state.presentationMode = !state.presentationMode;
      document.body.classList.toggle('presentation-mode', state.presentationMode);
      render();
    };

    window.rerunStress = async () => {
      state.stressLoading = true;
      render();

      try {
        const params = {};
        const capStr = state.customShocks.cap_rate_shocks_bps.trim();
        if (capStr) params.cap_rate_shocks_bps = capStr.split(',').map(s => parseInt(s.trim())).filter(n => !isNaN(n));
        const rateStr = state.customShocks.rate_shocks_bps.trim();
        if (rateStr) params.rate_shocks_bps = rateStr.split(',').map(s => parseInt(s.trim())).filter(n => !isNaN(n));
        const thresh = parseFloat(state.customShocks.metro_threshold);
        if (!isNaN(thresh)) params.concentration_thresholds = { metro: thresh };

        const result = await fetchCustomStress(params);
        if (result.status === 'success') {
          state.stress = result.stress;
        }
      } catch (err) {
        console.error('Custom stress failed:', err);
      }

      state.stressLoading = false;
      render();
    };

    // Keyboard shortcuts
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && state.selectedDeal) {
        state.selectedDeal = null;
        render();
      }
      if (e.key === 'p' && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        togglePresentationMode();
      }
    });

    // ============================================================================
    // INIT
    // ============================================================================

    const init = async () => {
      render();

      const progressInterval = setInterval(() => {
        state.loadProgress = Math.min(state.loadProgress + Math.random() * 15, 90);
        render();
      }, 100);

      try {
        state.loadingMessage = 'Connecting to Azure...';
        render();

        const data = await fetchDashboardData();

        if (data.status === 'success') {
          state.portfolio = data.portfolio;
          state.stress = data.stress;
          state.dataSource = 'api';
        } else {
          throw new Error(data.error || 'Unknown error');
        }
      } catch (err) {
        console.error('Dashboard load failed:', err);
        state.portfolio = { total_deals: 0, total_units: 0, total_equity: 0, weighted_irr: null, weighted_em: null, by_metro: {}, deals: [] };
        state.stress = {};
        state.loadingMessage = `Error: ${err.message}`;
      }

      clearInterval(progressInterval);
      state.loadProgress = 100;
      render();

      await new Promise(resolve => setTimeout(resolve, 300));
      state.isLoading = false;
      render();

      console.log('Portfolio dashboard loaded:', state.portfolio?.total_deals, 'deals');
    };

    init();
  </script>
</body>
</html>
```

- [ ] **Step 2: Verify the dashboard loads in a browser**

Open `frontend/portfolio-dashboard/index.html` in a browser. Without API configured, it should:
- Show loading screen with gold "S" logo
- Fail gracefully with "Error: ..." message
- Render empty portfolio layout (0 deals, no charts)

With API configured (`API_CONFIG.baseUrl` set to Azure Functions URL), it should:
- Load portfolio data from Cosmos
- Display summary metric cards
- Show metro bar
- Populate deal list
- Click a deal → drill-down with cashflow chart
- Stress panels with line charts and donut

- [ ] **Step 3: Commit**

```bash
git add frontend/portfolio-dashboard/index.html
git commit -m "feat: add portfolio dashboard frontend with deal drill-down and stress testing"
```

---

### Task 6: Update Session Handoff

**Files:**
- Modify: `docs/session_handoff.md`

- [ ] **Step 1: Update session handoff with Task 3.4 completion**

Update `docs/session_handoff.md`:
- Move Task 3.4 from "Next Up" to "Completed This Session" table with status **Done**
- Rename to "Portfolio Dashboard (replaces Power BI)"
- Add Task 3.4 Details section documenting what was done
- Update "Next Up" to Task 3.5 — Monthly LP Report Automation
- Add new/modified files to the file lists

- [ ] **Step 2: Commit**

```bash
git add docs/session_handoff.md
git commit -m "docs: update session handoff with Task 3.4 completion"
```

---

## Summary

| Task | Description | Key Files | Tests |
|------|-------------|-----------|-------|
| 1 | Extract shared CSS | `frontend/shared/styles.css`, refactor `index.html` | Manual visual check |
| 2 | Persistence default + run tagging | `persistence.py`, `api.py`, CLI scripts | 6 tests |
| 3 | Portfolio dashboard API handlers | `engine/api.py` | 9 tests |
| 4 | Azure Function endpoints | `azure-functions/api_portfolio_dashboard/`, `api_portfolio_stress/` | Covered by Task 3 tests |
| 5 | Portfolio dashboard frontend | `frontend/portfolio-dashboard/index.html` | Manual browser test |
| 6 | Session handoff update | `docs/session_handoff.md` | — |

**Total new tests:** 15
**Total commits:** 6
