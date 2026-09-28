"""Versioned, installed stdio MCP adapter for small underwriting results.

The adapter calls the packaged deterministic engine.  It never persists a run
or writes a deal artifact.  Full cashflow and exports remain direct API work.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from mcp.server.fastmcp import FastMCP

from engine.engine import run_underwriting
from engine.validator import validate_deal
from engine.version import ENGINE_VERSION, SCHEMA_VERSION


CONTRACT_VERSION = "plat.underwriting.mcp/1"
mcp = FastMCP("multifamily-underwriting")


def _identity() -> dict:
    return {
        "adapter_contract": CONTRACT_VERSION,
        "engine_version": ENGINE_VERSION,
        "schema_version": SCHEMA_VERSION,
    }


@mcp.tool()
def validate_deal_inputs(inputs: dict) -> dict:
    """Validate canonical deal inputs without running or persisting a deal."""
    return {**_identity(), **validate_deal(inputs).to_dict()}


@mcp.tool()
def run_deal_summary(inputs: dict, include_renovation_summary: bool = True) -> dict:
    """Return small investment metrics without cashflow arrays or artifacts."""
    results = run_underwriting(inputs)
    metrics = results.get("metrics", {})
    cashflow_summary = results.get("cashflow", {}).get("summary", {})
    summary = {
        **_identity(),
        "status": "success",
        "irr": metrics.get("irr", {}),
        "equity_multiple": metrics.get("equity_multiple", {}),
        "dscr": {
            "minimum": metrics.get("dscr", {}).get("minimum_dscr"),
            "average": metrics.get("dscr", {}).get("average_dscr"),
        },
        "yields": metrics.get("yields", {}),
        "exit": metrics.get("exit", {}),
        "noi_summary": {
            "total_noi": cashflow_summary.get("total_noi"),
            "average_noi_margin": cashflow_summary.get("average_noi_margin"),
        },
        "total_capex": cashflow_summary.get("total_capex"),
    }
    if include_renovation_summary and "renovations" in results:
        renovations = results["renovations"]
        summary["renovation_summary"] = renovations.get("summary", {})
        summary["renovation_by_program"] = renovations.get("by_program", [])
    return summary


def _ltv(inputs: dict) -> Decimal | None:
    try:
        purchase = Decimal(str(inputs["purchase_assumptions"]["purchase_price"]))
        debt = Decimal(str(inputs["debt_terms"]["commitment"]))
    except (KeyError, TypeError, InvalidOperation):
        return None
    if not purchase.is_finite() or not debt.is_finite() or purchase <= 0 or debt < 0:
        return None
    return debt / purchase


@mcp.tool()
def check_deal_feasibility(
    inputs: dict,
    min_levered_irr: float = 0.12,
    min_dscr: float = 1.20,
    max_ltv: float = 0.75,
) -> dict:
    """Validate a deal, then report explicit IRR, DSCR, and LTV gates."""
    report = validate_deal(inputs)
    if report.status == "FAIL":
        errors = [
            issue for issue in report.to_dict()["issues"]
            if issue["severity"] == "ERROR"
        ]
        return {
            **_identity(), "feasible": False, "blocked_by": "validation",
            "validation_errors": errors[:5],
        }
    results = run_underwriting(inputs)
    metrics = results.get("metrics", {})
    irr = metrics.get("irr", {})
    dscr = metrics.get("dscr", {})
    levered_irr = irr.get("levered_irr")
    minimum_dscr = dscr.get("minimum_dscr")
    ltv = _ltv(inputs)
    gates = {
        "levered_irr": {
            "actual": levered_irr, "threshold": min_levered_irr,
            "passes": levered_irr is not None and levered_irr >= min_levered_irr,
        },
        "minimum_dscr": {
            "actual": minimum_dscr, "threshold": min_dscr,
            "passes": minimum_dscr is not None and minimum_dscr >= min_dscr,
        },
        "ltv": {
            "actual": float(ltv) if ltv is not None else None,
            "threshold": max_ltv,
            "passes": ltv is not None and ltv <= Decimal(str(max_ltv)),
        },
    }
    feasible = all(gate["passes"] for gate in gates.values())
    response = {
        **_identity(), "feasible": feasible, "gates": gates,
        "metrics_snapshot": {
            "levered_irr": levered_irr,
            "unlevered_irr": irr.get("unlevered_irr"),
            "levered_em": metrics.get("equity_multiple", {}).get("levered_em"),
            "minimum_dscr": minimum_dscr,
            "going_in_cap": metrics.get("yields", {}).get("going_in_cap_rate"),
            "ltv": float(ltv) if ltv is not None else None,
        },
    }
    if not feasible:
        response["blocked_by"] = [name for name, gate in gates.items() if not gate["passes"]]
    return response


def main() -> None:
    """Run the installed adapter over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
