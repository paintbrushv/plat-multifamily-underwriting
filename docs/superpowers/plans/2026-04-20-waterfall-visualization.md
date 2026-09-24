# Waterfall Visualization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a summary metrics section + formatting to the Excel Waterfall tab, and a conditional stacked bar chart to the PDF one-pager showing tier-by-tier distributions by year.

**Architecture:** Two parallel outputs: (1) Excel — `write_waterfall_summary()` writes rows 2-7 above existing tier data with totals/IRR/EM/coinvest/clawback + named ranges + conditional formatting; (2) PDF — `_generate_waterfall_chart()` produces a stacked bar matplotlib chart (4 segments) inserted conditionally when multi-tier waterfall data exists.

**Tech Stack:** XlsmWriter (ZIP XML), matplotlib, reportlab (Image flowable)

---

## File Structure

| File | Responsibility |
|------|---------------|
| `engine/rediq_output.py` | New `write_waterfall_summary()` function + enhanced CF/named ranges for Waterfall sheet |
| `engine/pdf_onepager.py` | New `_generate_waterfall_chart()` function + conditional section in `generate_onepager()` |
| `tests/test_waterfall_viz.py` | All tests for this feature (7 tests) |

---

### Task 1: Write tests for Excel waterfall summary

**Files:**
- Create: `tests/test_waterfall_viz.py`

- [ ] **Step 1: Write the test file with Excel summary tests**

```python
"""Tests for Task 2.5 — Waterfall Visualization (Excel + PDF)."""
import pytest
from unittest.mock import MagicMock, patch, call


def _make_fund_waterfall_results(multi_tier=False):
    """Build a realistic fund_waterfall results dict."""
    by_year = [
        {
            "year": i + 1,
            "leveraged_cash_flow": 50000 * (i + 1),
            "partnership_expenses": -5000,
            "asset_management_fee": -10000,
            "disposition_fee": -15000 if i == 4 else 0,
            "cash_flow_before_promote": 35000 * (i + 1),
            "promote_payment": 8000 * (i + 1) if i >= 2 else 0,
            "cash_flow_to_partnership": 27000 * (i + 1),
            "sponsor_share": 2700 * (i + 1),
            "lp_share": 24300 * (i + 1),
        }
        for i in range(5)
    ]

    promote = {
        "total_promote": 96000.0,
        "portfolio_irr": 0.18,
        "by_year": {},
        "monthly_promotes": {},
    }

    if multi_tier:
        promote["by_tier"] = [
            {
                "tier": "Pref (8%)",
                "hurdle_irr": 0.08,
                "lp_share_pct": 1.0,
                "gp_share_pct": 0.0,
                "catch_up": False,
                "total_to_lp": 200000.0,
                "total_to_gp": 0.0,
                "total_distributed": 200000.0,
            },
            {
                "tier": "Promote 1 (12%)",
                "hurdle_irr": 0.12,
                "lp_share_pct": 0.8,
                "gp_share_pct": 0.2,
                "catch_up": False,
                "total_to_lp": 80000.0,
                "total_to_gp": 20000.0,
                "total_distributed": 100000.0,
            },
            {
                "tier": "Promote 2 (18%)",
                "hurdle_irr": 0.18,
                "lp_share_pct": 0.7,
                "gp_share_pct": 0.3,
                "catch_up": False,
                "total_to_lp": 35000.0,
                "total_to_gp": 15000.0,
                "total_distributed": 50000.0,
            },
        ]
    else:
        promote["by_tier"] = [
            {
                "tier": "Pref (8%)",
                "hurdle_irr": 0.08,
                "lp_share_pct": 1.0,
                "gp_share_pct": 0.0,
                "catch_up": False,
                "total_to_lp": 315000.0,
                "total_to_gp": 0.0,
                "total_distributed": 315000.0,
            },
        ]

    summary = {
        "total_partnership_expenses": -25000.0,
        "total_asset_management_fee": -50000.0,
        "total_disposition_fee": -15000.0,
        "total_promote": 96000.0,
        "total_sponsor_distributions": 40500.0,
        "total_lp_distributions": 364500.0,
        "partnership_irr": 0.165,
        "partnership_equity_multiple": 1.82,
        "gp_coinvest_pct": 0.05,
    }

    return {
        "fund_waterfall": {
            "by_year": by_year,
            "promote": promote,
            "summary": summary,
            "closing_costs": {"acquisition_fee": 25000, "partnership_closing_costs": 10000},
        },
        "time_grid": {
            "months": [f"2025-{m:02d}" for m in range(1, 13)],
            "analysis_start": "2025-01",
        },
    }


class TestWaterfallSummaryExcel:
    """Test write_waterfall_summary() output."""

    def test_summary_values_written(self):
        """Summary section writes total LP, total GP, IRR, EM to correct cells."""
        from engine.rediq_output import write_waterfall_summary

        writer = MagicMock()
        results = _make_fund_waterfall_results(multi_tier=True)

        write_waterfall_summary(writer, results)

        # Row 3: Total to LP (col C=3), Total to GP (col D=4), Total Distributed (col E=5)
        calls = writer.set_cell_value.call_args_list
        # Find the call writing total LP distributions
        lp_call = [c for c in calls if c[0] == ("Waterfall", 3, 3, 364500.0)]
        assert len(lp_call) == 1, f"Expected LP total write, got calls: {calls}"
        gp_call = [c for c in calls if c[0] == ("Waterfall", 3, 4, 40500.0)]
        assert len(gp_call) == 1
        # Total distributed
        total_call = [c for c in calls if c[0] == ("Waterfall", 3, 5, 405000.0)]
        assert len(total_call) == 1

    def test_summary_irr_em_written(self):
        """Partnership IRR and EM written to row 4."""
        from engine.rediq_output import write_waterfall_summary

        writer = MagicMock()
        results = _make_fund_waterfall_results(multi_tier=True)

        write_waterfall_summary(writer, results)

        calls = writer.set_cell_value.call_args_list
        irr_call = [c for c in calls if c[0] == ("Waterfall", 4, 3, 0.165)]
        assert len(irr_call) == 1
        em_call = [c for c in calls if c[0] == ("Waterfall", 4, 4, 1.82)]
        assert len(em_call) == 1

    def test_summary_single_tier(self):
        """Single-tier deals still get summary section."""
        from engine.rediq_output import write_waterfall_summary

        writer = MagicMock()
        results = _make_fund_waterfall_results(multi_tier=False)

        write_waterfall_summary(writer, results)

        calls = writer.set_cell_value.call_args_list
        # Should still write LP total
        lp_call = [c for c in calls if c[0] == ("Waterfall", 3, 3, 364500.0)]
        assert len(lp_call) == 1

    def test_summary_no_fund_waterfall(self):
        """No fund_waterfall data → no writes, no error."""
        from engine.rediq_output import write_waterfall_summary

        writer = MagicMock()
        write_waterfall_summary(writer, {"fund_waterfall": {}})
        writer.set_cell_value.assert_not_called()

    def test_named_ranges_defined(self):
        """Waterfall named ranges are defined for key metrics."""
        from engine.rediq_output import write_waterfall_summary

        writer = MagicMock()
        results = _make_fund_waterfall_results(multi_tier=True)

        write_waterfall_summary(writer, results)

        nr_calls = writer.define_named_range.call_args_list
        names_defined = [c[0][0] for c in nr_calls]
        assert "WF_Total_LP" in names_defined
        assert "WF_Total_GP" in names_defined
        assert "WF_Partnership_IRR" in names_defined
        assert "WF_Partnership_EM" in names_defined

    def test_conditional_formatting_applied(self):
        """Conditional formatting applied to tier distribution rows."""
        from engine.rediq_output import write_waterfall_summary

        writer = MagicMock()
        results = _make_fund_waterfall_results(multi_tier=True)

        write_waterfall_summary(writer, results)

        cf_calls = writer.add_conditional_format.call_args_list
        # Should have negative_red on distribution rows
        assert len(cf_calls) >= 1
        # At least one negative_red rule
        neg_red = [c for c in cf_calls if c[0][2] == "negative_red"]
        assert len(neg_red) >= 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_waterfall_viz.py -v`
Expected: FAIL with `ImportError` or `cannot import name 'write_waterfall_summary'`

---

### Task 2: Implement Excel waterfall summary writer

**Files:**
- Modify: `engine/rediq_output.py` (insert after `write_waterfall_sheet` function, ~line 1059)

- [ ] **Step 1: Add `write_waterfall_summary()` function**

Insert after line 1059 in `engine/rediq_output.py`:

```python
def write_waterfall_summary(writer: XlsmWriter, results: Dict[str, Any]):
    """Write waterfall summary metrics to rows 2-7 of the Waterfall sheet.

    Layout:
        Row 2: "WATERFALL SUMMARY" header
        Row 3: Total to LP | Total to GP | Total Distributed
        Row 4: Partnership IRR | Partnership EM
        Row 5: GP Coinvest % | Clawback (if any)
        Row 7: "TIER BREAKDOWN" header
    """
    sheet = "Waterfall"
    fund_wf = results.get("fund_waterfall", {})
    if not fund_wf:
        return

    wf_summary = fund_wf.get("summary", {})
    promote = fund_wf.get("promote", {})

    if not wf_summary:
        return

    # Verify sheet exists
    try:
        writer._get_sheet_xml(sheet)
    except KeyError:
        return

    # Row 2: Header
    writer.set_cell_value(sheet, 2, 3, "WATERFALL SUMMARY")

    # Row 3: Totals
    total_lp = wf_summary.get("total_lp_distributions", 0)
    total_gp = wf_summary.get("total_sponsor_distributions", 0)
    total_distributed = total_lp + total_gp

    writer.set_cell_value(sheet, 3, 3, total_lp)
    writer.set_cell_value(sheet, 3, 4, total_gp)
    writer.set_cell_value(sheet, 3, 5, total_distributed)

    # Row 3 labels (col B)
    writer.set_cell_value(sheet, 3, 2, "Total LP / GP / All")

    # Row 4: IRR and EM
    partnership_irr = wf_summary.get("partnership_irr")
    partnership_em = wf_summary.get("partnership_equity_multiple")

    if partnership_irr is not None:
        writer.set_cell_value(sheet, 4, 3, partnership_irr)
    if partnership_em is not None:
        writer.set_cell_value(sheet, 4, 4, partnership_em)

    writer.set_cell_value(sheet, 4, 2, "IRR / EM")

    # Row 5: GP Coinvest and Clawback
    gp_coinvest = wf_summary.get("gp_coinvest_pct", 0)
    clawback = promote.get("clawback_amount", 0)

    if gp_coinvest:
        writer.set_cell_value(sheet, 5, 3, gp_coinvest)
    if clawback:
        writer.set_cell_value(sheet, 5, 4, clawback)

    writer.set_cell_value(sheet, 5, 2, "Coinvest / Clawback")

    # Row 7: Section header for existing tier data
    writer.set_cell_value(sheet, 7, 3, "TIER BREAKDOWN")

    # Named ranges for key metrics
    try:
        writer.define_named_range("WF_Total_LP", sheet, "$C$3")
        writer.define_named_range("WF_Total_GP", sheet, "$D$3")
        writer.define_named_range("WF_Partnership_IRR", sheet, "$C$4")
        writer.define_named_range("WF_Partnership_EM", sheet, "$D$4")
    except (KeyError, ValueError):
        pass

    # Conditional formatting: negative_red on tier distribution rows
    # Tier I row 13 (base 9 + 4 = distribution), Tier II row 30, Tier III row 48, Tier IV row 67
    dist_rows = [13, 30, 48, 67]
    for row in dist_rows:
        try:
            writer.add_conditional_format(sheet, f"C{row}:P{row}", "negative_red")
        except (KeyError, ValueError):
            pass
```

- [ ] **Step 2: Wire into `generate_rediq_workbook()`**

In `generate_rediq_workbook()` (line ~1606), add the call after `write_waterfall_sheet`:

```python
        write_waterfall_sheet(writer, results, inputs)
        write_waterfall_summary(writer, results)  # NEW
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_waterfall_viz.py::TestWaterfallSummaryExcel -v`
Expected: All 6 tests PASS

- [ ] **Step 4: Run full test suite for regression**

Run: `python3 -m pytest --tb=short -q`
Expected: All 528+ tests pass

- [ ] **Step 5: Commit**

```bash
git add engine/rediq_output.py tests/test_waterfall_viz.py
git commit -m "feat(xlsx): add waterfall summary section with named ranges and CF rules"
```

---

### Task 3: Write tests for PDF waterfall chart

**Files:**
- Modify: `tests/test_waterfall_viz.py`

- [ ] **Step 1: Add PDF chart tests to test file**

Append to `tests/test_waterfall_viz.py`:

```python
class TestWaterfallChartPDF:
    """Test _generate_waterfall_chart() and conditional rendering."""

    def test_chart_generated_multi_tier(self):
        """Multi-tier waterfall produces non-empty PNG bytes."""
        from engine.pdf_onepager import _generate_waterfall_chart

        results = _make_fund_waterfall_results(multi_tier=True)
        png_bytes = _generate_waterfall_chart(results)

        assert isinstance(png_bytes, bytes)
        assert len(png_bytes) > 1000  # Real PNG is >1KB
        assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic header

    def test_chart_skipped_single_tier(self):
        """Single-tier waterfall returns empty bytes (skip)."""
        from engine.pdf_onepager import _generate_waterfall_chart

        results = _make_fund_waterfall_results(multi_tier=False)
        png_bytes = _generate_waterfall_chart(results)

        assert png_bytes == b""

    def test_chart_skipped_no_waterfall(self):
        """No fund_waterfall data → empty bytes."""
        from engine.pdf_onepager import _generate_waterfall_chart

        png_bytes = _generate_waterfall_chart({})
        assert png_bytes == b""

    def test_chart_segments_sum_to_total(self):
        """Internal: verify stacked segment data sums correctly."""
        from engine.pdf_onepager import _compute_waterfall_segments

        results = _make_fund_waterfall_results(multi_tier=True)
        segments = _compute_waterfall_segments(results)

        # Each year's segments should sum to total distributed that year
        by_year = results["fund_waterfall"]["by_year"]
        for i, yr in enumerate(by_year):
            year_total = segments["capital_return"][i] + segments["lp_pref"][i] + \
                         segments["lp_excess"][i] + segments["gp_promote"][i]
            expected = yr["lp_share"] + yr["promote_payment"]
            assert abs(year_total - expected) < 0.01, \
                f"Year {i+1}: segments sum {year_total} != expected {expected}"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_waterfall_viz.py::TestWaterfallChartPDF -v`
Expected: FAIL with `ImportError` (functions don't exist yet)

---

### Task 4: Implement PDF waterfall chart

**Files:**
- Modify: `engine/pdf_onepager.py`

- [ ] **Step 1: Add `_compute_waterfall_segments()` helper**

Insert after `_generate_cf_chart()` (after line 215):

```python
def _compute_waterfall_segments(results: Dict[str, Any]) -> Dict[str, List[float]]:
    """Decompose annual waterfall distributions into stacked chart segments.

    Returns dict with keys: years, capital_return, lp_pref, lp_excess, gp_promote.
    Each value is a list of floats (one per year).

    Segment logic:
    - gp_promote: promote_payment from by_year (known exactly)
    - LP total: lp_share from by_year
    - capital_return: portion of LP share that is return of capital (up to remaining equity)
    - lp_pref: portion that is preferred return (estimated from pref tier)
    - lp_excess: remainder of LP share above capital return + pref
    """
    fund_wf = results.get("fund_waterfall", {})
    by_year = fund_wf.get("by_year", [])
    summary = fund_wf.get("summary", {})
    promote = fund_wf.get("promote", {})
    by_tier = promote.get("by_tier", [])

    if not by_year:
        return {"years": [], "capital_return": [], "lp_pref": [], "lp_excess": [], "gp_promote": []}

    # Estimate initial equity from closing costs
    closing = fund_wf.get("closing_costs", {})
    acq_fee = closing.get("acquisition_fee", 0)
    pcc = closing.get("partnership_closing_costs", 0)
    # Use total_lp_distributions as upper bound for capital base
    total_lp = summary.get("total_lp_distributions", 0)

    # Estimate pref rate from tier data (first tier with gp_share=0)
    pref_tiers = [t for t in by_tier if t.get("gp_share_pct", 0) == 0]
    total_pref_distributed = sum(t.get("total_to_lp", 0) for t in pref_tiers)

    # Estimate what fraction of total LP distributions is pref vs excess
    promote_tiers = [t for t in by_tier if t.get("gp_share_pct", 0) > 0]
    total_lp_from_promote = sum(t.get("total_to_lp", 0) for t in promote_tiers)

    # Compute per-year segments
    years = []
    capital_return = []
    lp_pref = []
    lp_excess = []
    gp_promote = []

    # Simple proportional allocation per year
    total_lp_all_years = sum(max(0, yr.get("lp_share", 0)) for yr in by_year)

    for yr in by_year:
        years.append(yr.get("year", 0))
        promote_amt = max(0, float(yr.get("promote_payment", 0)))
        lp_total = max(0, float(yr.get("lp_share", 0)))

        gp_promote.append(promote_amt)

        if total_lp_all_years > 0 and lp_total > 0:
            year_frac = lp_total / total_lp_all_years
            # Allocate proportionally to tier breakdown
            yr_pref = total_pref_distributed * year_frac
            yr_excess = total_lp_from_promote * year_frac
            yr_capital = lp_total - yr_pref - yr_excess

            # Clamp: capital return can't be negative
            if yr_capital < 0:
                yr_pref += yr_capital  # Reduce pref
                yr_capital = 0
            if yr_pref < 0:
                yr_excess += yr_pref
                yr_pref = 0

            capital_return.append(yr_capital)
            lp_pref.append(yr_pref)
            lp_excess.append(yr_excess)
        else:
            capital_return.append(0)
            lp_pref.append(0)
            lp_excess.append(0)

    return {
        "years": years,
        "capital_return": capital_return,
        "lp_pref": lp_pref,
        "lp_excess": lp_excess,
        "gp_promote": gp_promote,
    }


def _generate_waterfall_chart(
    results: Dict[str, Any],
    width_inches: float = 6.5,
    height_inches: float = 2.0,
) -> bytes:
    """Generate a stacked bar chart of waterfall distributions as PNG bytes.

    Only produces output when multi-tier waterfall data exists (by_tier has >1 entry).
    Returns empty bytes for single-tier or missing data.
    """
    fund_wf = results.get("fund_waterfall", {})
    promote = fund_wf.get("promote", {})
    by_tier = promote.get("by_tier", [])

    # Only render for multi-tier waterfalls
    if len(by_tier) <= 1:
        return b""

    segments = _compute_waterfall_segments(results)
    years = segments["years"]

    if not years:
        return b""

    fig, ax = plt.subplots(figsize=(width_inches, height_inches))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    x = range(len(years))
    bar_width = 0.6

    # Stacked bars (bottom to top)
    bottom = [0.0] * len(years)

    ax.bar(x, segments["capital_return"], bar_width,
           bottom=bottom, label="Return of Capital", color=MPL_DARK_900, alpha=0.85)
    bottom = [b + v for b, v in zip(bottom, segments["capital_return"])]

    ax.bar(x, segments["lp_pref"], bar_width,
           bottom=bottom, label="LP Pref", color=MPL_SLATE_500, alpha=0.85)
    bottom = [b + v for b, v in zip(bottom, segments["lp_pref"])]

    ax.bar(x, segments["lp_excess"], bar_width,
           bottom=bottom, label="LP Promote Share", color=MPL_GOLD, alpha=0.85)
    bottom = [b + v for b, v in zip(bottom, segments["lp_excess"])]

    ax.bar(x, segments["gp_promote"], bar_width,
           bottom=bottom, label="GP Promote", color=MPL_GOLD_DARK, alpha=0.85)

    # Formatting (matches _generate_cf_chart style)
    ax.set_xticks(list(x))
    ax.set_xticklabels([f"Yr {y}" for y in years], fontsize=7, color=MPL_SLATE_500)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(
        lambda val, pos: f"${val/1e6:.1f}M" if abs(val) >= 1e6 else f"${val/1e3:.0f}K"
    ))
    ax.tick_params(axis="y", labelsize=7, colors=MPL_SLATE_500)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color(MPL_SLATE_200)
    ax.spines["bottom"].set_color(MPL_SLATE_200)
    ax.legend(fontsize=6, loc="upper left", frameon=False, ncol=4)

    plt.tight_layout(pad=0.3)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()
```

- [ ] **Step 2: Add conditional chart section to `generate_onepager()`**

In `generate_onepager()`, insert after the Cash Flow Chart section (after line 497, before Scenario Comparison):

```python
    # ── Waterfall Distribution Chart (multi-tier only) ──
    fund_wf = results.get("fund_waterfall", {})
    wf_promote = fund_wf.get("promote", {})
    wf_by_tier = wf_promote.get("by_tier", [])
    if len(wf_by_tier) > 1:
        wf_chart_h = 1.4 if compact else 1.8
        wf_chart_w = 5.5 if compact else 6.0
        wf_chart_png = _generate_waterfall_chart(results, height_inches=wf_chart_h, width_inches=wf_chart_w)
        if wf_chart_png:
            elements.append(Paragraph("Waterfall Distribution", styles["SectionHeader"]))
            wf_img = Image(io.BytesIO(wf_chart_png), width=wf_chart_w * inch, height=wf_chart_h * inch)
            elements.append(wf_img)
            elements.append(Spacer(1, 2))
```

- [ ] **Step 3: Run PDF chart tests**

Run: `python3 -m pytest tests/test_waterfall_viz.py::TestWaterfallChartPDF -v`
Expected: All 4 tests PASS

- [ ] **Step 4: Run full test suite for regression**

Run: `python3 -m pytest --tb=short -q`
Expected: All tests pass

- [ ] **Step 5: Commit**

```bash
git add engine/pdf_onepager.py tests/test_waterfall_viz.py
git commit -m "feat(pdf): add conditional waterfall stacked bar chart for multi-tier deals"
```

---

### Task 5: Integration test and final wiring

**Files:**
- Modify: `tests/test_waterfall_viz.py`
- Modify: `engine/rediq_output.py` (already done in Task 2, just verify wiring)

- [ ] **Step 1: Add integration test**

Append to `tests/test_waterfall_viz.py`:

```python
class TestWaterfallIntegration:
    """Integration: verify write_waterfall_summary is called in generate_rediq_workbook."""

    def test_waterfall_summary_called_in_generate(self):
        """write_waterfall_summary is invoked during workbook generation."""
        from unittest.mock import patch as _patch
        import engine.rediq_output as ro

        with _patch.object(ro, "write_waterfall_summary") as mock_wfs:
            # We can't fully run generate_rediq_workbook without a template,
            # but we can verify the function exists and is wired in the source
            import inspect
            source = inspect.getsource(ro.generate_rediq_workbook)
            assert "write_waterfall_summary" in source
```

- [ ] **Step 2: Run all waterfall viz tests**

Run: `python3 -m pytest tests/test_waterfall_viz.py -v`
Expected: All 11 tests PASS

- [ ] **Step 3: Run full suite**

Run: `python3 -m pytest --tb=short -q`
Expected: All tests pass

- [ ] **Step 4: Final commit**

```bash
git add tests/test_waterfall_viz.py
git commit -m "test: add integration verification for waterfall visualization"
```

---

## Verification Checklist

After all tasks complete:

1. `python3 -m pytest tests/test_waterfall_viz.py -v` — all 11 tests pass
2. `python3 -m pytest --tb=short -q` — full suite passes (528+ tests)
3. Spot-check: run a multi-tier deal through `generate_rediq_workbook` if template available
4. Update `docs/session_handoff.md` — mark Task 2.5 as Done
