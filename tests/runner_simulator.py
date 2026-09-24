"""
Runner simulator — Python analogue of the underwriting-runner LLM agent.
========================================================================

The production runner (`.claude/agents/underwriting-runner.md`) is an LLM
agent. Its prompt enforces a deterministic workflow (Wave 4 Task 4.1):

    1. splice (if applicable)
    2. validate canonical → BridgeError(code="validation_failed") on FAIL
    3. run engine with federation_mode=True
    4. read verdict / sanity_flags / reasons VERBATIM from engine result
    5. validate BridgeResponseV1 payload via Pydantic UnderwritingResponse
    6. persist deal_summary.json + _provenance.json + _response_envelope.json
       (atomic temp+rename)

This module reproduces the same workflow in straight Python so the contract
can be exercised by unit tests without an LLM in the loop. Production
emission is the LLM agent; this simulator is the unit-test analogue.

Public entrypoint:

    simulate_runner(canonical, output_dir, *, deal_slug, run_id,
                    output_workbook=False) -> dict (BridgeResponseV1 shape)
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _atomic_write_json(path: Path, data: Any) -> None:
    """Atomic temp+rename JSON write. Mirrors the runner prompt's helper."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str)
        os.replace(tmp, str(path))
    except Exception:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        finally:
            raise


def _location_text(metadata: Dict[str, Any]) -> str | None:
    address = metadata.get("address")
    if isinstance(address, dict):
        city = str(address.get("city") or "").strip()
        state = str(address.get("state") or "").strip()
        if city and state:
            return f"{city}, {state}"
        street = str(address.get("street") or "").strip()
        if street:
            return street
    elif isinstance(address, str) and address.strip():
        parts = [part.strip() for part in address.split(",") if part.strip()]
        if len(parts) >= 2:
            return ", ".join(parts[-2:])
        return address.strip()

    market = metadata.get("market")
    if market not in (None, ""):
        return str(market)
    return None


def _bridge_error_response(
    *,
    deal_slug: str,
    run_id: str,
    code: str,
    message: str,
    details: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build a BridgeResponseV1-shaped error envelope."""
    return {
        "contract_version": "v1",
        "status": "error",
        "deal_slug": deal_slug,
        "run_id": run_id,
        "agent_name": "underwriting-runner",
        "payload": None,
        "artifacts": [],
        "provenance": [],
        "error": {
            "code": code,
            "message": message,
            "details": details or [],
        },
        "sanity_flags": [],
    }


def simulate_runner(
    canonical: Dict[str, Any],
    output_dir: Path,
    *,
    deal_slug: str,
    run_id: str,
    output_workbook: bool = False,
) -> Dict[str, Any]:
    """Simulate the LLM runner's deterministic workflow.

    Returns a `BridgeResponseV1`-shaped dict (status="ok" or "error"). On the
    happy path the dict has been validated through the Pydantic
    `UnderwritingResponse` model and persisted alongside `deal_summary.json`
    and `_provenance.json` in `output_dir`.

    Parameters
    ----------
    canonical:
        The canonical deal-input dict (post-splice).
    output_dir:
        Directory to write artifacts. Created if missing.
    deal_slug, run_id:
        Echo-back fields required on every BridgeResponseV1.
    output_workbook:
        Mirrors `request.payload.output_workbook`. When False (default for
        tests) we skip workbook generation entirely.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # -- Step 2: validate canonical BEFORE engine run --------------------- #
    from engine.validator import validate_deal

    report = validate_deal(canonical)
    if report.status != "PASS":
        details = [
            {
                "severity": i.severity,
                "code": i.code,
                "message": i.message,
                "path": i.path,
            }
            for i in report.issues
        ]
        return _bridge_error_response(
            deal_slug=deal_slug,
            run_id=run_id,
            code="validation_failed",
            message=f"Validation failed: {len(report.issues)} issues",
            details=details,
        )

    # -- Step 3: run engine in federation mode ---------------------------- #
    try:
        from engine.engine import run_underwriting
        results = run_underwriting(canonical, federation_mode=True)
    except ValueError as e:
        return _bridge_error_response(
            deal_slug=deal_slug,
            run_id=run_id,
            code="validation_failed",
            message=str(e),
        )

    # -- Step 4: build standardized provenance via engine.api ------------- #
    from engine.api import build_provenance

    validator_report_dict = results.get("_validator_report")
    provenance = build_provenance(
        inputs=canonical,
        results=results,
        validator_status="PASS",
        validator_report_dict=validator_report_dict,
    )

    # -- Step 5: build payload + Pydantic-validate ------------------------ #
    metrics = results.get("metrics", {}) or {}
    yields = metrics.get("yields", {}) or {}
    irr = metrics.get("irr", {}) or {}
    em = metrics.get("equity_multiple", {}) or {}
    dscr = metrics.get("dscr", {}) or {}

    payload = {
        "metrics": {
            "levered_irr":     irr.get("levered_irr"),
            "equity_multiple": em.get("levered_em"),
            "min_dscr":        dscr.get("minimum_dscr"),
            "avg_dscr":        dscr.get("average_dscr"),
            "going_in_cap":    yields.get("going_in_cap_rate"),
            "exit_cap":        yields.get("exit_cap_rate"),
        },
        "feasibility_verdict":  provenance["feasibility_verdict"],
        "feasibility_reasons":  list(provenance["feasibility_reasons"]),
        "summary_relative":     "underwriting/deal_summary.json",
        "workbook_relative":    None,  # tests skip workbook
        "property_snapshot": {
            "year_built": canonical.get("metadata", {}).get("year_built"),
            "location": _location_text(canonical.get("metadata", {}) or {}),
            "price_per_unit": (
                canonical.get("purchase_assumptions", {}).get("purchase_price")
                / sum((c.get("unit_count", 0) or 0) for c in canonical.get("unit_cohorts", []))
                if canonical.get("purchase_assumptions", {}).get("purchase_price") is not None
                and sum((c.get("unit_count", 0) or 0) for c in canonical.get("unit_cohorts", []))
                else None
            ),
            "units": sum((c.get("unit_count", 0) or 0) for c in canonical.get("unit_cohorts", [])),
        },
    }

    try:
        from plat_agent.contracts.domain.underwriting import UnderwritingResponse
        from pydantic import ValidationError

        UnderwritingResponse(**payload)
    except ValidationError as e:
        return _bridge_error_response(
            deal_slug=deal_slug,
            run_id=run_id,
            code="schema_violation",
            message="UnderwritingResponse failed Pydantic validation",
            details=e.errors(),
        )

    # -- Step 6: persist artifacts (atomic temp+rename) ------------------- #
    summary_path = output_dir / "deal_summary.json"
    provenance_path = output_dir / "_provenance.json"
    envelope_path = output_dir / "_response_envelope.json"

    # `deal_summary.json` first — even if downstream steps fail, metrics
    # should be on disk (mirrors the runner prompt's "write summary
    # immediately after run_underwriting" rule).
    _atomic_write_json(summary_path, results)
    _atomic_write_json(provenance_path, provenance)

    artifacts: List[Dict[str, Any]] = [
        {
            "relative_path": "underwriting/deal_summary.json",
            "kind": "json",
            "description": "Full engine output (metrics + cashflow + feasibility).",
        },
        {
            "relative_path": "underwriting/_provenance.json",
            "kind": "json",
            "description": "Standardized provenance dict.",
        },
        {
            "relative_path": "underwriting/_response_envelope.json",
            "kind": "json",
            "description": "Full BridgeResponseV1 envelope (post-Pydantic-validation).",
        },
    ]

    envelope = {
        "contract_version": "v1",
        "status": "ok",
        "deal_slug": deal_slug,
        "run_id": run_id,
        "agent_name": "underwriting-runner",
        "payload": payload,
        "artifacts": artifacts,
        "provenance": [
            {
                "source": "engine.engine.run_underwriting",
                "locator": "multifamily-underwriting/engine/engine.py (federation_mode=True)",
                "note": (
                    f"Validator PASS at {datetime.now(timezone.utc).isoformat()}. "
                    f"inputs_hash_sha256={provenance.get('inputs_hash_sha256')}."
                ),
            }
        ],
        "error": None,
        "sanity_flags": list(provenance.get("feasibility_sanity_flags", [])),
    }

    _atomic_write_json(envelope_path, envelope)
    return envelope


__all__ = ["simulate_runner"]
