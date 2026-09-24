"""
API Layer for Azure Functions / HTTP Endpoints
================================================
Stateless request handlers for the underwriting engine.
Designed to be called from Azure Functions, FastAPI, or any HTTP framework.

Each function takes JSON input and returns JSON output — no framework dependencies.

Usage (Azure Functions):
    import azure.functions as func
    from engine.api import handle_run_deal, handle_portfolio_summary

    @app.route(route="run")
    async def run(req: func.HttpRequest) -> func.HttpResponse:
        body = req.get_json()
        result = handle_run_deal(body)
        return func.HttpResponse(json.dumps(result), mimetype="application/json")

Usage (direct):
    from engine.api import handle_run_deal

    result = handle_run_deal({
        "inputs": {...},  # or "source_path": "path/to/rediq.xlsm"
        "options": {"scenarios": True, "scenario_type": "stabilized"}
    })
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _compute_inputs_hash(inputs: Dict[str, Any]) -> str:
    """Deterministic SHA-256 of inputs (sorted-keys JSON, default=str for Decimal/date).

    `default=str` preserves identity for Decimal/date types coming through the
    canonical pipeline; sort_keys ensures dict-order doesn't perturb the hash.
    """
    payload = json.dumps(inputs, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _serialize_validation_issues(report_dict: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Coerce ValidationIssue dataclasses (already dict-shape via to_dict) to
    plain JSON-safe dicts. Tolerant to None / missing 'issues' key."""
    if not report_dict:
        return []
    issues = report_dict.get("issues") or []
    out: List[Dict[str, Any]] = []
    for i in issues:
        if isinstance(i, dict):
            out.append({
                "severity": i.get("severity"),
                "code": i.get("code"),
                "message": i.get("message"),
                "path": i.get("path"),
            })
        else:
            # Fallback: dataclass-like object with __dict__
            d = getattr(i, "__dict__", None) or {}
            out.append({
                "severity": d.get("severity"),
                "code": d.get("code"),
                "message": d.get("message"),
                "path": d.get("path"),
            })
    return out


def build_provenance(
    *,
    inputs: Dict[str, Any],
    results: Dict[str, Any],
    validator_status: str,
    validator_report_dict: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Assemble the standardized federation provenance dict.

    Required keys (Wave 1b Task 1.5; mirrors stage_6_memo.md M-5 fix):
        engine_version, schema_version, inputs_hash_sha256, generated_at_utc,
        validator_status, validator_issues, feasibility_verdict,
        feasibility_sanity_flags, feasibility_reasons, cap_rate_derivation.
    """
    from engine.feasibility import classify
    from engine.version import ENGINE_VERSION, SCHEMA_VERSION

    metrics = (results or {}).get("metrics", {}) or {}
    feasibility = classify(metrics)

    # Preserve any pre-computed cap_rate_derivation block if the engine emits
    # one in metrics (deal_summary / runner-side derivation). None otherwise.
    cap_rate_derivation = None
    yields = metrics.get("yields") if isinstance(metrics, dict) else None
    if isinstance(yields, dict) and isinstance(yields.get("cap_rate_derivation"), dict):
        cap_rate_derivation = yields["cap_rate_derivation"]

    # V1.5: surface Year-1 Cash-on-Cash in provenance so federation
    # consumers (plat-agent CRM, recommendation rule) can read it from the
    # canonical _provenance.json shape rather than parsing engine results.
    coc_block = None
    if isinstance(metrics, dict):
        coc_src = metrics.get("coc") or metrics.get("cash_on_cash") or {}
        if isinstance(coc_src, dict) and "cash_on_cash_year_1" in coc_src:
            coc_block = {
                "cash_on_cash_year_1": coc_src.get("cash_on_cash_year_1"),
                "cash_on_cash_year_1_exact": coc_src.get("cash_on_cash_year_1_exact"),
                "free_cf_year_1": coc_src.get("free_cf_year_1"),
                "target_cash_on_cash_pct": coc_src.get("target_cash_on_cash_pct"),
                "coc_below_target": coc_src.get("coc_below_target"),
                "coc_below_target_7pct": coc_src.get("coc_below_target_7pct"),
                "coc_below_target_6pct": coc_src.get("coc_below_target_6pct"),
                "components": coc_src.get("components"),
            }

    return {
        "engine_version": ENGINE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "inputs_hash_sha256": _compute_inputs_hash(inputs),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "validator_status": validator_status,
        "validator_issues": _serialize_validation_issues(validator_report_dict),
        "feasibility_verdict": feasibility.verdict,
        "feasibility_sanity_flags": list(feasibility.sanity_flags),
        "feasibility_reasons": list(feasibility.reasons),
        "cap_rate_derivation": cap_rate_derivation,
        "coc": coc_block,
    }


def handle_run_deal(request: Dict[str, Any]) -> Dict[str, Any]:
    """Run the underwriting engine for a single deal.

    Request format:
        {
            "inputs": {...},                      # Engine inputs (validated JSON)
            "source_path": "path/to/rediq.xlsm",  # OR: path to RedIQ file
            "options": {
                "scenarios": false,                # Run Bull/Base/Bear
                "scenario_type": "stabilized",     # or "value_add"
                "include_cashflow": true,           # Include full cashflow in response
            }
        }

    Returns:
        {
            "status": "success" | "error",
            "deal_id": "...",
            "metrics": {...},
            "cashflow_summary": {...},
            "scenario_comparison": {...},  # if scenarios=true
            "elapsed_seconds": 0.5,
            "error": "..."  # if status=error
        }
    """
    start = time.time()
    options = request.get("options", {})

    try:
        # Get inputs
        inputs = request.get("inputs")
        if not inputs and request.get("source_path"):
            from engine.rediq_bridge import extract_inputs_from_rediq
            inputs = extract_inputs_from_rediq(request["source_path"])

        if not inputs:
            return {
                "status": "error",
                "error": "Either 'inputs' or 'source_path' is required",
                "elapsed_seconds": time.time() - start,
            }

        # Validate (Wave 6 Task 6.3 — pass federation context through if present
        # in the BridgeRequest so the comp-evidence check can locate
        # <deal_root>/outputs/<run_id>/market_study/comps.json).
        from engine.validator import validate_deal
        deal_root = request.get("deal_root")
        run_id_for_validator = (
            request.get("run_id")
            or inputs.get("metadata", {}).get("run_id")
        )
        federation_mode = bool(request.get("federation_mode", False))
        report = validate_deal(
            inputs,
            deal_root=Path(deal_root) if deal_root else None,
            run_id=run_id_for_validator,
            federation_mode=federation_mode,
        )
        validator_report_dict = report.to_dict()
        validator_status = "PASS" if report.status == "PASS" else "FAIL"
        if report.status != "PASS":
            # Embed full provenance even on validator FAIL so callers can
            # serialize a uniform _provenance.json shape regardless of outcome.
            failed_provenance = build_provenance(
                inputs=inputs,
                results={},
                validator_status="FAIL",
                validator_report_dict=validator_report_dict,
            )
            return {
                "status": "error",
                "error": f"Validation failed: {len(report.issues)} issues",
                "validation_issues": [str(i) for i in report.issues[:10]],
                "provenance": failed_provenance,
                "elapsed_seconds": time.time() - start,
            }

        # Market automation (pre-engine)
        market_audit = None
        market_mode = inputs.get("options", {}).get("market_automation_level", "off")
        if market_mode != "off":
            from engine.market_automation import run_market_automation
            inputs, market_audit = run_market_automation(inputs)

        # Run engine
        from engine.engine import run_underwriting
        results = run_underwriting(inputs)

        # Attach market audit trail to results and response
        if market_audit:
            from engine.market_automation import attach_audit_trail
            results = attach_audit_trail(results, market_audit)

        deal_id = inputs.get("metadata", {}).get("deal_id", "Unknown")
        metrics = results.get("metrics", {})

        response = {
            "status": "success",
            "deal_id": deal_id,
            "metrics": {
                "levered_irr": metrics.get("irr", {}).get("levered_irr"),
                "unlevered_irr": metrics.get("irr", {}).get("unlevered_irr"),
                "levered_em": metrics.get("equity_multiple", {}).get("levered_em"),
                "unlevered_em": metrics.get("equity_multiple", {}).get("unlevered_em"),
                "partnership_irr": metrics.get("irr", {}).get("partnership_irr"),
                "partnership_em": metrics.get("equity_multiple", {}).get("partnership_em"),
                "average_dscr": metrics.get("dscr", {}).get("average_dscr"),
                "minimum_dscr": metrics.get("dscr", {}).get("minimum_dscr"),
                "going_in_cap": metrics.get("yields", {}).get("going_in_cap_rate"),
            },
        }

        # Cashflow summary
        by_year = results.get("cashflow", {}).get("by_year", [])
        response["cashflow_summary"] = {
            "years": len(by_year),
            "noi_year_1": by_year[0].get("net_operating_income", 0) if by_year else 0,
            "noi_exit": by_year[-1].get("net_operating_income", 0) if by_year else 0,
        }

        if market_audit:
            response["market_adjustments"] = market_audit.get("market_adjustments", [])
            response["market_intel_summary"] = market_audit.get("market_intel_summary", {})

        # Wave 1b Task 1.5: standardized provenance shape (federation-uniform).
        # Always present; surfaces validator status + feasibility verdict so
        # downstream memo composer / cross-run reconciler sees the same keys
        # regardless of which deal/path produced the run.
        response["provenance"] = build_provenance(
            inputs=inputs,
            results=results,
            validator_status=validator_status,
            validator_report_dict=validator_report_dict,
        )

        if options.get("include_cashflow"):
            response["cashflow"] = results.get("cashflow")

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

        # Scenarios (optional)
        if options.get("scenarios"):
            from engine.modules.scenarios import run_scenarios, STABILIZED_PRESETS, VALUE_ADD_PRESETS
            scenario_type = options.get("scenario_type", "stabilized")
            presets = VALUE_ADD_PRESETS if scenario_type == "value_add" else STABILIZED_PRESETS
            scenario_results = run_scenarios(inputs, presets=presets)
            response["scenario_comparison"] = scenario_results.get("comparison")

        response["elapsed_seconds"] = round(time.time() - start, 3)
        return response

    except Exception as e:
        return {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
            "elapsed_seconds": round(time.time() - start, 3),
        }


def handle_portfolio_summary(request: Dict[str, Any]) -> Dict[str, Any]:
    """Get portfolio summary metrics.

    Request format:
        {
            "directory": "output/",           # Load from JSON pairs in directory
            "deals": [                         # OR: provide deals inline
                {"inputs": {...}, "results": {...}},
            ],
            "filters": {
                "metro": "DFW",               # Filter by metro (optional)
                "vintage_year": 2026,          # Filter by vintage (optional)
            }
        }

    Returns:
        {
            "status": "success",
            "summary": {...},
            "by_metro": {...},
            "by_vintage": {...},
            "deals": [...]
        }
    """
    start = time.time()

    try:
        from engine.portfolio import Portfolio

        portfolio = Portfolio()

        # Load from directory
        if request.get("directory"):
            loaded = portfolio.add_deals_from_directory(request["directory"])
        elif request.get("deals"):
            for deal in request["deals"]:
                portfolio.add_deal(deal["inputs"], deal["results"])
        else:
            return {
                "status": "error",
                "error": "Either 'directory' or 'deals' is required",
            }

        # Apply filters
        filters = request.get("filters", {})
        if filters.get("metro"):
            metro_filter = filters["metro"]
            portfolio.deals = [d for d in portfolio.deals if d.metro == metro_filter]
        if filters.get("vintage_year"):
            vintage_filter = filters["vintage_year"]
            portfolio.deals = [d for d in portfolio.deals if d.vintage_year == vintage_filter]

        return {
            "status": "success",
            "summary": portfolio.summary(),
            "by_metro": portfolio.group_by_metro(),
            "by_vintage": portfolio.group_by_vintage(),
            "deals": portfolio.deal_comparison_matrix(),
            "elapsed_seconds": round(time.time() - start, 3),
        }

    except Exception as e:
        return {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
            "elapsed_seconds": round(time.time() - start, 3),
        }


def handle_refi_vs_sell(request: Dict[str, Any]) -> Dict[str, Any]:
    """Compare refi-and-hold vs sell scenarios.

    Request format:
        {
            "inputs": {...},
            "refi_year": 3,
            "sell_year": 5,
            "refi_loan_terms": {
                "commitment": 800000,
                "rate": 0.055,
                "amort_years": 30,
                "io_months": 12,
                "loan_start_month": "2028-01"
            }
        }
    """
    start = time.time()

    try:
        inputs = request.get("inputs")
        if not inputs:
            return {"status": "error", "error": "'inputs' is required"}

        from engine.modules.scenarios import run_refi_vs_sell
        result = run_refi_vs_sell(
            inputs,
            refi_year=request.get("refi_year", 3),
            sell_year=request.get("sell_year", 5),
            refi_loan_terms=request.get("refi_loan_terms", {}),
        )

        return {
            "status": "success",
            "comparison": result.get("comparison"),
            "elapsed_seconds": round(time.time() - start, 3),
        }

    except Exception as e:
        return {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
            "elapsed_seconds": round(time.time() - start, 3),
        }


def handle_market_context(request: Dict[str, Any]) -> Dict[str, Any]:
    """Get market context and comp analysis for a deal.

    Request format:
        {
            "config_slug": "dallas_tx_forest_hills",
            "floorplan_csv": "path/to/floorplan_summary.csv",  # OR explicit paths
            "comp_snapshot_json": "path/to/snapshot.json",
        }
    """
    start = time.time()

    try:
        from engine.market_integration import MarketContext

        if request.get("config_slug"):
            ctx = MarketContext.from_config(request["config_slug"])
        elif request.get("floorplan_csv") or request.get("comp_snapshot_json"):
            ctx = MarketContext.from_files(
                floorplan_csv=Path(request["floorplan_csv"]) if request.get("floorplan_csv") else None,
                comp_snapshot_json=Path(request["comp_snapshot_json"]) if request.get("comp_snapshot_json") else None,
            )
        else:
            return {"status": "error", "error": "'config_slug' or file paths required"}

        return {
            "status": "success",
            "summary": ctx.comp_summary(),
            "suggested_rent_growth": ctx.suggested_rent_growth(),
            "elapsed_seconds": round(time.time() - start, 3),
        }

    except Exception as e:
        return {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
            "elapsed_seconds": round(time.time() - start, 3),
        }


def handle_portfolio_dashboard(request: Dict[str, Any]) -> Dict[str, Any]:
    """Build a full portfolio dashboard from Cosmos DB deal runs.

    Request format:
        {
            "run_tag_filter": "exploratory",  # Optional: only include matching run tags
        }

    Returns:
        {
            "status": "success",
            "portfolio": {
                "total_deals": N,
                "total_units": N,
                "total_equity": N,
                "weighted_irr": float,
                "weighted_em": float,
                "by_metro": {...},
                "deals": [...]
            },
            "stress": {
                "cap_rate": {...},
                "rate": {...},
                "concentration": {...}
            },
            "elapsed_seconds": float
        }
    """
    start = time.time()

    try:
        from engine.persistence import get_store
        store = get_store()
    except EnvironmentError as e:
        return {
            "status": "error",
            "error": f"Cosmos DB not configured: {e}",
            "elapsed_seconds": round(time.time() - start, 3),
        }

    try:
        from engine.portfolio import Portfolio

        deal_ids = store.list_deals()
        run_tag_filter = request.get("run_tag_filter")

        # Load all docs and keep a reference keyed by deal_id
        docs_by_deal: Dict[str, Dict[str, Any]] = {}
        for deal_id in deal_ids:
            doc = store.load_latest(deal_id)
            if not doc or not doc.get("inputs") or not doc.get("results"):
                continue
            if run_tag_filter:
                doc_tag = doc.get("metadata", {}).get("run_tag")
                if doc_tag != run_tag_filter:
                    continue
            docs_by_deal[deal_id] = doc

        # Build portfolio
        portfolio = Portfolio()
        for deal_id, doc in docs_by_deal.items():
            portfolio.add_deal(doc["inputs"], doc["results"])

        # Summary
        summary = portfolio.summary()

        # Build deals list from DealSnapshot + cashflow from original docs
        deals_list = []
        for snap in portfolio.deals:
            doc = docs_by_deal.get(snap.deal_id, {})
            by_year = doc.get("results", {}).get("cashflow", {}).get("by_year", [])
            deals_list.append({
                "deal_id": snap.deal_id,
                "name": doc.get("inputs", {}).get("metadata", {}).get("deal_id", snap.deal_id),
                "metro": snap.metro,
                "units": snap.units,
                "purchase_price": snap.purchase_price,
                "equity": snap.total_equity,
                "levered_irr": snap.levered_irr,
                "levered_em": snap.levered_em,
                "unlevered_irr": snap.unlevered_irr,
                "dscr_min": snap.minimum_dscr,
                "going_in_cap": snap.going_in_cap,
                "noi_year_1": snap.noi_year_1,
                "cashflow_by_year": by_year,
            })

        portfolio_response = {
            "total_deals": summary.get("deal_count", 0),
            "total_units": summary.get("total_units", 0),
            "total_equity": summary.get("total_equity", 0),
            "weighted_irr": summary.get("weighted_avg_levered_irr"),
            "weighted_em": summary.get("weighted_avg_levered_em"),
            "by_metro": portfolio.group_by_metro(),
            "deals": deals_list,
        }

        # Stress tests — wrapped in try/except so failure doesn't block summary
        stress = {}
        try:
            stress["cap_rate"] = portfolio.stress_test_cap_rate(shocks=[25, 50, 75, 100])
        except Exception:
            stress["cap_rate"] = None
        try:
            stress["rate"] = portfolio.stress_test_rates(shocks=[100, 200, 300])
        except Exception:
            stress["rate"] = None
        try:
            stress["concentration"] = portfolio.concentration_risk()
        except Exception:
            stress["concentration"] = None

        return {
            "status": "success",
            "portfolio": portfolio_response,
            "stress": stress,
            "elapsed_seconds": round(time.time() - start, 3),
        }

    except Exception as e:
        return {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
            "elapsed_seconds": round(time.time() - start, 3),
        }


def handle_portfolio_stress(request: Dict[str, Any]) -> Dict[str, Any]:
    """Run custom stress tests on the portfolio from Cosmos DB.

    Request format:
        {
            "cap_rate_shocks_bps": [25, 50, 100],       # Optional
            "rate_shocks_bps": [100, 200],               # Optional
            "concentration_thresholds": {"metro": 0.4},  # Optional
        }

    Returns:
        {
            "status": "success",
            "stress": {
                "cap_rate": {...},       # if cap_rate_shocks_bps provided
                "rate": {...},           # if rate_shocks_bps provided
                "concentration": {...},  # if concentration_thresholds provided
            },
            "elapsed_seconds": float
        }
    """
    start = time.time()

    try:
        from engine.persistence import get_store
        store = get_store()
    except EnvironmentError as e:
        return {
            "status": "error",
            "error": f"Cosmos DB not configured: {e}",
            "elapsed_seconds": round(time.time() - start, 3),
        }

    try:
        from engine.portfolio import Portfolio

        deal_ids = store.list_deals()

        portfolio = Portfolio()
        for deal_id in deal_ids:
            doc = store.load_latest(deal_id)
            if doc and doc.get("inputs") and doc.get("results"):
                try:
                    portfolio.add_deal(doc["inputs"], doc["results"])
                except Exception:
                    continue

        stress = {}

        if request.get("cap_rate_shocks_bps"):
            try:
                stress["cap_rate"] = portfolio.stress_test_cap_rate(
                    shocks=request["cap_rate_shocks_bps"]
                )
            except Exception:
                stress["cap_rate"] = None

        if request.get("rate_shocks_bps"):
            try:
                stress["rate"] = portfolio.stress_test_rates(
                    shocks=request["rate_shocks_bps"]
                )
            except Exception:
                stress["rate"] = None

        if request.get("concentration_thresholds"):
            try:
                stress["concentration"] = portfolio.concentration_risk(
                    thresholds=request["concentration_thresholds"]
                )
            except Exception:
                stress["concentration"] = None

        return {
            "status": "success",
            "stress": stress,
            "elapsed_seconds": round(time.time() - start, 3),
        }

    except Exception as e:
        return {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
            "elapsed_seconds": round(time.time() - start, 3),
        }
