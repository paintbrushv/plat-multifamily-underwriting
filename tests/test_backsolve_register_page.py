"""UPL-97: the backsolve writes the assumption register page for the priced canonical.

Both backsolve CLIs (``python -m engine.backsolve`` and
``runs/backsolve_price_for_target_coc.py``) write ``assumptions.html`` into the
output dir, with a pricing summary header (target and basis, solved price,
status, loan and its sizing basis, equity, caps, IRRs, minimum DSCR, the
exit-cap sensitivity grid) and every floor the pricing enforced highlighted on
its row and listed in the header. ``--no-assumptions-html`` opts out. An
infeasible solve still writes the page: the summary with no solved price, and
the assumptions the search priced at the bracket floor.

Synthetic library deals only (``tests/fixtures/deals``).
"""
from __future__ import annotations

import copy
import json
import re
import runpy
import sys
from pathlib import Path

import pytest

import engine.backsolve as backsolve
from engine.assumptions import guardrails
from engine.assumptions.register import build_register
from engine.assumptions.register_html import render_register_html
from engine.assumptions.register_pricing import pricing_header
from engine.modules import loan_sizing
from tests import deal_library as dl

REPO = Path(__file__).resolve().parents[1]
SHIM = REPO / "runs" / "backsolve_price_for_target_coc.py"
PAGE = "assumptions.html"
# Deal d prices with one floor raised over PCA evidence (recurring capex) and
# one plain enforced floor (collection loss); see tests/test_deal_library.py.
SLUG = "d_value_add_1980s_msa_only"
BENCHMARK = ("0.04", "2026-10-01", "synthetic:deal-library")


def _argv(canonical: Path, out: Path, *extra: str, prog: str = "backsolve") -> list[str]:
    return [
        prog, "--canonical-json", str(canonical), "--output-dir", str(out),
        "--policy-version", backsolve.POLICY_VERSION, "--strategy", "value_add",
        "--benchmark-5yr-treasury", BENCHMARK[0], "--benchmark-as-of", BENCHMARK[1],
        "--benchmark-source", BENCHMARK[2], "--min-price", "1000000", *extra,
    ]


def _hermetic(mp: pytest.MonkeyPatch, base: Path) -> None:
    """The conftest market-data isolation, for the module-scoped runs below."""
    for name in ("PLAT_MARKET_STUDY_ROOT", "PLAT_SUPPLY_DEMAND_ROOT"):
        mp.setenv(name, str(base / "no-market-data" / name.lower()))
    for name in ("PLAT_MARKET_DATA_PROVIDERS", "PLAT_MARKET_DATA_USER_FILE", "PLAT_MARKET_STUDY_COMPS"):
        mp.delenv(name, raising=False)


@pytest.fixture(scope="module")
def library_canonical(tmp_path_factory) -> Path:
    base = tmp_path_factory.mktemp("backsolve_page")
    with pytest.MonkeyPatch.context() as mp:
        _hermetic(mp, base)
        canonical = dl.ingest(dl.load_deal(SLUG))
    path = base / "canonical.json"
    path.write_text(json.dumps(canonical), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def solved(library_canonical, tmp_path_factory):
    """One real CLI solve of deal d: (output dir, summary, page html, priced case)."""
    out = tmp_path_factory.mktemp("solved") / "pricing"
    with pytest.MonkeyPatch.context() as mp:
        _hermetic(mp, out.parent)
        mp.setattr(sys, "argv", _argv(library_canonical, out, "--target-coc", "0.03",
                                      "--max-price", "20000000"))
        backsolve.main()
    summary = json.loads((out / "backsolve_summary.json").read_text(encoding="utf-8"))
    case = json.loads((out / "canonical_backsolved_target_coc.json").read_text(encoding="utf-8"))
    return out, summary, (out / PAGE).read_text(encoding="utf-8"), case


def _section(html: str) -> str:
    match = re.search(r'<section class="pricing".*?</section>', html, re.S)
    assert match, "no pricing header"
    return match.group(0)


def _row_classes(html: str, key: str) -> list[str]:
    match = re.search(rf'<tr class="([^"]*)" id="a-{re.escape(key)}"', html)
    assert match, key
    return match.group(1).split()


# ── default: the page is written with the pricing header ─────────────────


def test_cli_writes_the_register_page_for_the_priced_canonical(solved):
    out, summary, html, case = solved
    assert summary["status"] == "converged"
    assert summary["artifacts"]["assumptions_html"] == str(out / PAGE)
    assert html.startswith("<!DOCTYPE html>")
    assert "http://" not in html and "https://" not in html  # self-contained
    # The register below the header is the priced canonical's register.
    register = build_register(case)
    assert register["mode"] == "pricing"
    for row in register["rows"]:
        assert f'id="a-{row["key"]}"' in html


def test_header_shows_the_solved_price_loan_basis_and_metrics(solved):
    _, summary, html, _ = solved
    header = _section(html)
    price = summary["solved_purchase_price"]
    units = summary["policy_summary"]["units"]
    assert "Converged" in header
    assert f"${price:,.2f}" in header
    assert f"${price / units:,.0f}/unit" in header
    assert f"${summary['loan_amount']:,.0f}" in header
    sizing = summary["loan_sizing"]
    assert loan_sizing.SHORT_LABELS[sizing["basis"]] in header
    assert sizing["source"] in header
    assert f"${summary['solved_total_equity_basis']:,.0f}" in header
    assert "Target CoC" in header and "3.00%" in header
    assert "Stabilized year" in header  # the value-add default basis
    for label in ("Going-in cap", "Exit cap", "Levered IRR", "Unlevered IRR", "Minimum DSCR"):
        assert label in header, label
    assert f"{summary['minimum_dscr']:.2f}x" in header


def test_header_has_the_exit_cap_sensitivity_grid(solved):
    _, summary, html, _ = solved
    header = _section(html)
    points = summary["exit_cap_sensitivity"]["points"]
    rows = re.findall(r'<tr class="grid-row[^"]*">.*?</tr>', header, re.S)
    assert len(rows) == len(points)
    for row, point in zip(rows, points):
        assert f"{point['offset_bps']:+d} bps" in row
        assert f"{point['levered_irr'] * 100:.2f}%" in row
        assert ("base" in row.split(">", 1)[0]) == point["is_base"]


def test_enforced_floors_are_highlighted_on_their_rows_and_listed(solved):
    _, summary, html, case = solved
    findings = summary["policy_summary"]["guardrails"]["findings"]
    enforced = {f["key"] for f in findings if f["action"] in guardrails.ENFORCING_ACTIONS}
    assert enforced == {"recurring_capex_per_unit", "collection_loss"}
    header = _section(html)
    floors = re.search(r'<ul class="floors">.*?</ul>', header, re.S).group(0)
    for key in enforced:
        assert "floor-enforced" in _row_classes(html, key), key
        assert f'href="#a-{key}"' in floors, key
    assert "Floor applied over deal evidence" in floors
    assert "Raised to house floor" in floors
    assert "$1,500" in floors and "$1,750" in floors  # PCA original -> applied
    # Values the policy set (fees) are pinned by pricing, not enforced floors.
    assert "floor-enforced" not in _row_classes(html, "acquisition_fee_pct")
    assert "Floors enforced in pricing (2)" in header


def test_opt_out_writes_no_page_from_either_cli(solved, library_canonical, tmp_path, monkeypatch):
    out, summary, _, case = solved
    result = {"status": "converged", "case": case,
              "results": json.loads((out / "underwriting_backsolved_target_coc.json").read_text()),
              "summary": {k: v for k, v in summary.items() if k != "artifacts"}}
    monkeypatch.setattr(backsolve, "backsolve_price", lambda *a, **k: copy.deepcopy(result))

    engine_out = tmp_path / "engine"
    monkeypatch.setattr(sys, "argv", _argv(library_canonical, engine_out, "--no-assumptions-html"))
    backsolve.main()
    assert not (engine_out / PAGE).exists()
    written = json.loads((engine_out / "backsolve_summary.json").read_text())
    assert "assumptions_html" not in written["artifacts"]

    shim_out, shim_off = tmp_path / "shim", tmp_path / "shim_off"
    monkeypatch.setattr(sys, "argv", _argv(library_canonical, shim_out, prog=str(SHIM)))
    runpy.run_path(str(SHIM), run_name="__main__")
    assert (shim_out / PAGE).is_file()
    monkeypatch.setattr(sys, "argv", _argv(library_canonical, shim_off, "--no-assumptions-html", prog=str(SHIM)))
    runpy.run_path(str(SHIM), run_name="__main__")
    assert not (shim_off / PAGE).exists()


# ── infeasible: the page still shows the summary and the assumptions ─────


def test_infeasible_backsolve_still_writes_the_page(library_canonical, tmp_path, monkeypatch, capsys):
    out = tmp_path / "pricing"
    # Deal d meets 3% only up to about $11M: a $15M-$20M bracket is infeasible.
    monkeypatch.setattr(sys, "argv", _argv(library_canonical, out, "--target-coc", "0.03",
                                           "--min-price", "15000000", "--max-price", "20000000"))
    with pytest.raises(SystemExit) as exit_info:
        backsolve.main()
    assert exit_info.value.code == 2
    summary = json.loads((out / "backsolve_summary.json").read_text())
    assert summary["status"] == "infeasible" and summary["solved_purchase_price"] is None
    assert summary["artifacts"] == {"assumptions_html": str(out / PAGE)}
    assert not (out / "canonical_backsolved_target_coc.json").exists()
    html = (out / PAGE).read_text(encoding="utf-8")
    header = _section(html)
    assert "Infeasible" in header
    assert "No solved price" in header
    assert "$15,000,000.00" in header  # the bracket floor it was priced at
    assert "3.00%" in header  # the target
    # The assumptions the search priced, enforced floors highlighted.
    assert 'id="a-exit_cap_rate"' in html
    assert "floor-enforced" in _row_classes(html, "recurring_capex_per_unit")
    assert str(out / PAGE) in capsys.readouterr().out


def test_infeasible_api_result_carries_the_bracket_floor_case(library_canonical):
    canonical = json.loads(library_canonical.read_text(encoding="utf-8"))
    result = backsolve.backsolve_price(
        canonical, target_coc="0.03",
        policy={"version": backsolve.POLICY_VERSION, "strategy": "value_add"},
        benchmark=dict(zip(("rate", "as_of", "source"), BENCHMARK)),
        min_price="15000000.00", max_price="20000000.00",
    )
    assert result["status"] == "infeasible"
    assert result["case"] is None  # never a solved case
    floor_case = result["bracket_floor_case"]
    assert floor_case["purchase_assumptions"]["purchase_price"] == 15_000_000.0
    assert floor_case["assumptions"]["mode"] == "pricing"
    entries = floor_case["assumptions"]["entries"]
    assert entries["recurring_capex_per_unit"]["guardrail"]["action"] == "floor_applied_over_deal_evidence"


# ── header details on synthetic summaries ────────────────────────────────


def _top_up_canonical() -> dict:
    """A priced canonical whose R&M bucket the pricing guardrail topped up."""
    top_up_key = "opex.repairs_maintenance_vintage_floor_top_up.base"
    return {
        "metadata": {"deal_id": "synthetic-top-up"},
        "assumptions": {"mode": "pricing", "resolver_version": "synthetic", "entries": {
            "r_and_m_per_unit": {
                "value": 900.0, "unit": "usd_per_unit_year", "source": "house:vintage_1980s",
                "default_value": 900.0,
                "override": {"value": 900.0, "note": guardrails.PRICING_FLOOR_LINE_NOTE},
                "guardrail": {"status": "below_floor", "action": "enforced", "floor": 900.0,
                              "floor_source": "house:vintage_1980s", "original": 700.0, "applied": 900.0},
            },
            top_up_key: {
                "value": 20000.0, "unit": "usd_per_year", "source": "house:vintage_1980s",
                "default_value": 0.0,
                "override": {"value": 20000.0, "note": guardrails.PRICING_FLOOR_LINE_NOTE},
                "detail": {"category_name": guardrails.R_AND_M_TOP_UP_CATEGORY},
            },
            "acquisition_fee_pct": {
                "value": 0.01, "unit": "decimal_of_purchase_price", "source": "house:backsolve_policy",
                "default_value": 0.01, "override": {"value": 0.01, "note": guardrails.PRICING_POLICY_NOTE},
            },
        }},
    }


def _top_up_summary() -> dict:
    return {
        "status": "converged", "solved_purchase_price": 5_000_000.0, "target_cash_on_cash_pct": 0.06,
        "achieved_coc_pct": 6.01, "coc_basis": {"basis": "year_1", "analysis_year": 1},
        "loan_amount": 3_000_000.0, "solved_total_equity_basis": 2_200_000.0,
        "loan_sizing": {"basis": "noi_before_capex", "sizing_method": "ltv_constrained", "source": "synthetic"},
        "going_in_cap_rate": 0.065, "exit_cap_rate": 0.06, "levered_irr": 0.12,
        "minimum_dscr": 1.4, "minimum_dscr_basis": "noi_before_capex",
        "exit_cap_calculation": {"mode": "entry_anchored", "going_in_cap_rate": 0.08, "spread": 0.005,
                                 "steal": True, "compression": 0.004, "exit_cap_rate": 0.06},
        "policy_summary": {
            "units": 40,
            "repairs_maintenance_top_up_annual": 20000.0,
            "repairs_maintenance_floor_per_unit": 900.0,
            "guardrails": {"findings": [{
                "key": "r_and_m_per_unit", "input": "opex_table.repairs_maintenance", "status": "below_floor",
                "action": "enforced", "floor": 900.0, "floor_source": "house:vintage_1980s",
                "original": 700.0, "applied": 900.0, "top_up_annual": 20000.0,
            }]},
            "policy_overrides": [
                {"input": "opex_table.repairs_maintenance", "floor": 900.0, "original": 700.0, "applied": 900.0,
                 "assumption": "r_and_m_per_unit", "guardrail": "house_floor"},
                {"input": "insurance_per_unit", "action": "floor_waived_by_analyst_override",
                 "floor": 600.0, "floor_source": "house:year1_insurance_floor", "original": 450.0, "applied": 500.0},
            ],
        },
    }


def test_r_and_m_top_up_line_is_highlighted_and_listed():
    canonical = _top_up_canonical()
    html = render_register_html(canonical, pricing=pricing_header(_top_up_summary(), canonical=canonical))
    header = _section(html)
    top_up_key = "opex.repairs_maintenance_vintage_floor_top_up.base"
    assert "floor-enforced" in _row_classes(html, "r_and_m_per_unit")
    assert "floor-enforced" in _row_classes(html, top_up_key)
    assert "floor-enforced" not in _row_classes(html, "acquisition_fee_pct")
    assert f'href="#a-{top_up_key}"' in header
    assert "R&amp;M top-up" in header and "$20,000" in header
    # Other pricing adjustments: the insurance floor waiver, disclosed.
    assert "House floor waived" in header and "$450" in header and "$500" in header


def test_steal_adjusted_exit_cap_is_flagged_in_the_header():
    canonical = _top_up_canonical()
    header = _section(render_register_html(canonical, pricing=pricing_header(_top_up_summary(), canonical=canonical)))
    assert "steal" in header.lower()
    assert "40 bps" in header  # the compression applied


def test_a_minimal_summary_still_renders():
    """A stubbed solver's bare summary (the CLI tests) renders without a crash."""
    for summary in ({"status": "converged"}, {"status": "infeasible", "solved_purchase_price": None}, {}):
        header = _section(render_register_html({}, pricing=pricing_header(summary)))
        assert "n/a" in header
