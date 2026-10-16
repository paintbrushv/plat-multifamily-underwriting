"""The eight-step underwriting workflow over a persistent deal workspace.

Each public function is one MCP tool (see ``docs/MCP_TOOLS.md``) and returns a
JSON-serializable dict: ``ok``, a short human ``summary``, a ``next_step``
hint, and the tool's data. A refusal is ``ok: False`` with
``error: {code, message, fix}`` saying what failed and how to fix it.

Only orchestration lives here. Ingestion is
``engine.ingest.ingestion_pipeline.build_deal_from_documents``; assumption
edits use ``engine.assumptions.sheet.stamp_overrides`` and the resolver;
the run is ``engine.validator.validate_deal`` and
``engine.engine.run_underwriting``; pricing is ``engine.backsolve.backsolve_price``
and ``write_backsolve_artifacts``.

``configure()`` sets the transport mode. In remote mode (an HTTP server bound
beyond loopback) tools never read server-side paths (``add_document(path=)``
is refused) and responses carry no absolute server paths.
"""

from __future__ import annotations

import base64
import binascii
import functools
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote

from engine.workflow import fields as profile_fields
from engine.workflow import guides
from engine.workflow.config import DOCUMENT_KINDS, WorkflowConfig, load_config
from engine.workflow.workspace import (
    ASSUMPTIONS_NAME,
    CANONICAL_NAME,
    DOCUMENTS_DIR,
    MANIFEST_NAME,
    MANIFEST_VERSION,
    PRICE_DIR,
    REGISTER_NAME,
    RUN_DIR,
    Workspace,
    WorkspaceError,
    now_iso,
)

# The bundled synthetic sample deal: t12.*, rent_roll.*, profile.json (and optionally pca.json).
SAMPLE_DEAL_DIR = Path(__file__).resolve().parents[1] / "samples" / "sample_deal"

STEP_TOOLS = (
    ("create", "create_deal"),
    ("documents", "add_document"),
    ("describe", "describe_deal"),
    ("ingest", "ingest_deal"),
    ("review", "review_assumptions"),
    ("edit", "edit_assumptions"),
    ("run", "run_deal"),
    ("price", "solve_price"),
)
# A step is stale when one of its inputs changed after it ran.
STEP_INPUTS = {
    "ingest": ("documents", "describe"),
    "run": ("ingest", "edit"),
    "price": ("ingest", "edit"),
}
REVIEW_FILTERS = {
    "all": "all",
    "needs_review": "review",
    "edited": "edited",
    "defaults": "house",
    "enforced": None,  # rows whose guardrail floor pricing enforced
}
PRICED_CANONICAL = f"{PRICE_DIR}/canonical_backsolved_target_coc.json"
PRICED_RESULTS = f"{PRICE_DIR}/underwriting_backsolved_target_coc.json"
FILE_DESCRIPTIONS = {
    MANIFEST_NAME: "Workspace manifest: profile, documents, edits, step status",
    CANONICAL_NAME: "The ingested canonical deal (inputs the engine runs)",
    ASSUMPTIONS_NAME: "Assumptions sheet (every assumption, default, source, edits)",
    REGISTER_NAME: "Assumption register page: every assumption, its default and why, your edits",
    f"{RUN_DIR}/results.json": "Engine results (cash flow, metrics, market data status)",
    f"{RUN_DIR}/underwriting.xlsx": "Underwriting workbook (summary, pro forma, monthly detail, assumptions)",
    f"{RUN_DIR}/assumptions.html": "Assumption register page for the run",
    f"{RUN_DIR}/assumptions.json": "Assumptions sheet for the run",
    f"{RUN_DIR}/canonical.json": "The exact inputs that were run",
    f"{RUN_DIR}/validation.json": "Validation report for the run",
    f"{PRICE_DIR}/backsolve_summary.json": "Price solve summary (status, price, loan, caps, IRRs, floors)",
    f"{PRICE_DIR}/assumptions.html": "Assumption register page with the pricing header",
    f"{PRICE_DIR}/assumptions.json": "Assumptions sheet of the priced case",
    PRICED_CANONICAL: "The priced canonical (solved price, house floors applied)",
    PRICED_RESULTS: "Engine results at the solved price",
}

_MODE: dict[str, Any] = {"transport": "stdio", "remote": False, "base_url": None}


def configure(*, transport: str = "stdio", remote: bool = False, base_url: str | None = None) -> None:
    """Set how the tools are being served (stdio, local HTTP, or remote HTTP)."""
    _MODE.update(transport=transport, remote=bool(remote), base_url=base_url.rstrip("/") if base_url else None)


def mode() -> dict[str, Any]:
    return {**_MODE, "local_paths_allowed": not _MODE["remote"]}


# ── envelopes ─────────────────────────────────────────────────────────────


class Refusal(Exception):
    def __init__(self, code: str, message: str, fix: str, **data: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.fix, self.data = code, message, fix, data


def ok(summary: str, next_step: str, **data: Any) -> dict[str, Any]:
    return {"ok": True, "summary": summary, "next_step": next_step, **data}


def error(code: str, message: str, fix: str, **data: Any) -> dict[str, Any]:
    summary = message if message.endswith(".") else f"{message}."
    return {"ok": False, "summary": summary, "next_step": fix,
            "error": {"code": code, "message": message, "fix": fix}, **data}


def tool(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Turn refusals and unexpected failures into structured, actionable errors."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return _jsonable(fn(*args, **kwargs))
        except Refusal as exc:
            return error(exc.code, exc.message, exc.fix, **_jsonable(exc.data))
        except WorkspaceError as exc:
            return error(exc.code, exc.message, exc.fix)
        except Exception as exc:  # noqa: BLE001 - every tool answers with a structured error
            return error("UNEXPECTED_ERROR", f"{fn.__name__} failed: {type(exc).__name__}: {exc}",
                         "This may be an unsupported document layout or a bug. Check the inputs with "
                         "get_deal; if it persists, report it with the error message.")

    return wrapper


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


# ── workspace helpers ─────────────────────────────────────────────────────


def _config() -> WorkflowConfig:
    return load_config()


def _ws() -> Workspace:
    return Workspace(_config().deals_dir)


def _local(path: Path | str) -> str | None:
    """An absolute server path, or None in remote mode (paths never leak to remote callers)."""
    return None if _MODE["remote"] else str(Path(path).resolve())


def _workspace_field(ws: Workspace, deal_id: str) -> dict[str, Any]:
    path = _local(ws.deal_dir(deal_id))
    return {"workspace": path} if path else {}


def _bump(manifest: dict[str, Any], step: str) -> None:
    manifest["revision"] = int(manifest.get("revision", 0)) + 1
    manifest.setdefault("steps", {})[step] = {"at": now_iso(), "rev": manifest["revision"]}


def _step_rev(manifest: Mapping[str, Any], step: str) -> int | None:
    record = (manifest.get("steps") or {}).get(step)
    return record.get("rev") if isinstance(record, Mapping) else None


def _stale(manifest: Mapping[str, Any], step: str) -> bool:
    rev = _step_rev(manifest, step)
    if rev is None:
        return False
    inputs = [_step_rev(manifest, name) for name in STEP_INPUTS.get(step, ())]
    return any(other is not None and other > rev for other in inputs)


def _market(profile: Mapping[str, Any]) -> Any:
    if profile.get("use_market_data") is False:
        return None
    from engine.market_data import default_registry

    return default_registry()


def _canonical(ws: Workspace, deal_id: str) -> dict[str, Any]:
    canonical = ws.read_json(deal_id, CANONICAL_NAME)
    if not isinstance(canonical, dict):
        raise Refusal("NOT_INGESTED", f"{deal_id} has not been ingested yet",
                      f"Call ingest_deal(deal_id='{deal_id}') after adding the T12 and rent roll and "
                      "describing the deal.")
    return canonical


def _save_canonical(ws: Workspace, deal_id: str, canonical: dict[str, Any]) -> None:
    from engine.assumptions.register_html import write_register_html
    from engine.assumptions.sheet import has_assumptions, write_assumptions_json

    ws.write_json(deal_id, CANONICAL_NAME, canonical)
    if has_assumptions(canonical):
        write_assumptions_json(canonical, ws.path(deal_id, ASSUMPTIONS_NAME))
        write_register_html(canonical, ws.path(deal_id, REGISTER_NAME))


def _units(canonical: Mapping[str, Any]) -> int:
    return sum(int(c.get("unit_count") or 0) for c in canonical.get("unit_cohorts") or [] if isinstance(c, Mapping))


def _missing_for_ingest(manifest: Mapping[str, Any]) -> list[str]:
    missing = [f"document {kind}" for kind in ("t12", "rent_roll") if kind not in (manifest.get("documents") or {})]
    profile = manifest.get("profile") or {}
    missing += [name for name in profile_fields.REQUIRED_FOR_INGEST if not profile.get(name)]
    return missing


def _next_step(manifest: Mapping[str, Any]) -> str:
    deal_id = manifest["deal_id"]
    docs = manifest.get("documents") or {}
    missing_docs = [k for k in ("t12", "rent_roll") if k not in docs]
    if missing_docs:
        return (f"Add the {' and '.join(missing_docs)} with add_document(deal_id='{deal_id}', kind=..., "
                "filename=..., content_base64=...).")
    profile = manifest.get("profile") or {}
    if not all(profile.get(name) for name in profile_fields.REQUIRED_FOR_INGEST):
        return (f"Ask the analyst for the analysis period and call describe_deal(deal_id='{deal_id}', "
                "start='YYYY-MM', end='YYYY-MM', ...) with the price, debt and property-tax millage too.")
    if _step_rev(manifest, "ingest") is None or _stale(manifest, "ingest"):
        return f"Call ingest_deal(deal_id='{deal_id}')."
    last = manifest.get("last_ingest") or {}
    if last.get("gate_status") == "BLOCKED":
        return "Fix the ingest blockers (see get_deal last_ingest), then call ingest_deal again."
    if last.get("missing_to_run"):
        return (f"Ask the analyst for {', '.join(last['missing_to_run'])}, call describe_deal(deal_id='{deal_id}', ...) "
                "and ingest_deal again.")
    if last.get("validation_errors"):
        return ("Resolve the validation errors from ingest_deal (usually a describe_deal field such as "
                "purchase_price or millage), then ingest_deal again.")
    if _step_rev(manifest, "run") is None or _stale(manifest, "run"):
        return (f"Review the flagged assumptions with review_assumptions(deal_id='{deal_id}', "
                f"filter='needs_review'), then call run_deal(deal_id='{deal_id}').")
    if _step_rev(manifest, "price") is None or _stale(manifest, "price"):
        return (f"Call solve_price(deal_id='{deal_id}', target_coc=..., benchmark_5yr_treasury=...) after "
                "asking the analyst for the target cash-on-cash and today's 5-year Treasury yield.")
    return f"Call export_deal(deal_id='{deal_id}') to get the workbook, results and assumption register."


def _steps(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    out = []
    for step, tool_name in STEP_TOOLS:
        record = (manifest.get("steps") or {}).get(step) or {}
        if step == "review":
            done = _step_rev(manifest, "ingest") is not None
            out.append({"step": step, "tool": tool_name, "done": done, "at": None, "stale": False,
                        "note": "read-only; available once the deal is ingested"})
            continue
        out.append({"step": step, "tool": tool_name, "done": bool(record), "at": record.get("at"),
                    "stale": _stale(manifest, step)})
    return out


# ── 0. orientation ────────────────────────────────────────────────────────


@tool
def start_here() -> dict[str, Any]:
    config = _config()
    workspace = {"max_upload_mb": config.as_dict()["max_upload_mb"],
                 "allowed_extensions": config.as_dict()["allowed_extensions"]}
    if not _MODE["remote"]:
        workspace.update(deals_dir=str(config.deals_dir), deals_dir_origin=config.deals_dir_origin)
    return ok(
        "Deterministic multifamily underwriting: add a T12 and a rent roll, describe the deal, then ingest, "
        "review and edit the assumptions, run it and solve a price. Eight steps; the sample deal shows them.",
        "Call load_sample_deal() to try the bundled synthetic sample, or create_deal(name, deal_type) to "
        "start your own deal.",
        what_it_does=guides.WHAT_IT_DOES,
        steps=[dict(step) for step in guides.STEPS],
        anytime={"get_deal": "which steps are done and what to do next",
                 "list_deals": "every deal in the workspace",
                 "export_deal": "every file the deal produced, with paths or links"},
        try_the_sample=("Call load_sample_deal(), then ingest_deal(deal_id), review_assumptions(deal_id, "
                        "filter='needs_review'), run_deal(deal_id) and solve_price(deal_id, target_coc='0.08', "
                        "benchmark_5yr_treasury=<today's 5-year Treasury yield as a decimal>)."),
        resources=dict(guides.RESOURCES),
        prompts={"new_deal_walkthrough": "Walk the analyst through the eight steps.",
                 "explain_assumption": "Explain one assumption, its default and why."},
        tools_reference=guides.TOOLS_DOC,
        workspace=workspace,
        mode=mode(),
    )


# ── deals ─────────────────────────────────────────────────────────────────


def _deal_card(manifest: Mapping[str, Any]) -> dict[str, Any]:
    done = [step for step, _ in STEP_TOOLS if step in (manifest.get("steps") or {})]
    last_run = manifest.get("last_run") or {}
    last_price = manifest.get("last_price") or {}
    return {
        "deal_id": manifest["deal_id"], "name": manifest.get("name"), "deal_type": manifest.get("deal_type"),
        "updated_at": manifest.get("updated_at"), "steps_done": done,
        "levered_irr": last_run.get("levered_irr"), "solved_price": last_price.get("price"),
        "next_step": _next_step(manifest),
    }


@tool
def list_deals() -> dict[str, Any]:
    ws = _ws()
    deals = sorted((_deal_card(m) for m in ws.manifests()), key=lambda d: d["updated_at"] or "", reverse=True)
    if not deals:
        return ok("No deals in this workspace yet.",
                  "Call load_sample_deal() to try the sample, or create_deal(name, deal_type).", deals=[])
    return ok(f"{len(deals)} deal(s) in the workspace.",
              "Call get_deal(deal_id) for a deal's status and next step.", deals=deals)


@tool
def get_deal(deal_id: str) -> dict[str, Any]:
    ws = _ws()
    manifest = ws.load(deal_id)
    files = [f["path"] for f in ws.files(deal_id)]
    done = sum(1 for step in _steps(manifest) if step["done"])
    return ok(
        f"{manifest['name']} ({manifest['deal_type']}): {done} of {len(STEP_TOOLS)} steps done.",
        _next_step(manifest),
        deal_id=deal_id,
        name=manifest["name"],
        deal_type=manifest["deal_type"],
        created_at=manifest.get("created_at"),
        updated_at=manifest.get("updated_at"),
        profile=manifest.get("profile") or {},
        missing_for_ingest=_missing_for_ingest(manifest),
        documents=manifest.get("documents") or {},
        edits=manifest.get("edits") or {},
        steps=_steps(manifest),
        last_ingest=manifest.get("last_ingest"),
        last_run=manifest.get("last_run"),
        last_price=manifest.get("last_price"),
        pricing_defaults=manifest.get("pricing_defaults"),
        files=files,
        **_workspace_field(ws, deal_id),
    )


def _check_deal_type(deal_type: Any) -> str:
    from engine.assumptions.profile import SUPPORTED_DEAL_TYPES

    if deal_type not in SUPPORTED_DEAL_TYPES:
        raise Refusal("BAD_DEAL_TYPE", f"deal_type {deal_type!r} is not supported",
                      f"Use one of: {', '.join(SUPPORTED_DEAL_TYPES)} (lease_up and distressed are not supported yet).")
    return deal_type


def _create(name: str, deal_type: str) -> tuple[Workspace, dict[str, Any]]:
    if not isinstance(name, str) or not name.strip() or len(name) > 200:
        raise Refusal("BAD_NAME", "the deal name must be 1 to 200 characters", "Pass a short deal name.")
    _check_deal_type(deal_type)
    ws = _ws()
    created = now_iso()
    manifest = {
        "manifest_version": MANIFEST_VERSION, "deal_id": ws.new_deal_id(name), "name": name.strip(),
        "deal_type": deal_type, "created_at": created, "updated_at": created, "revision": 0,
        "profile": {}, "documents": {}, "edits": {}, "steps": {},
    }
    _bump(manifest, "create")
    ws.create(manifest)
    return ws, manifest


@tool
def create_deal(name: str, deal_type: str = "stabilized") -> dict[str, Any]:
    ws, manifest = _create(name, deal_type)
    deal_id = manifest["deal_id"]
    return ok(f"Created {manifest['name']} ({deal_type}) as {deal_id}.",
              f"Add the T12 and the rent roll with add_document(deal_id='{deal_id}', kind='t12', ...).",
              deal_id=deal_id, name=manifest["name"], deal_type=deal_type, **_workspace_field(ws, deal_id))


# ── 2. documents ──────────────────────────────────────────────────────────


def _store_document(ws: Workspace, config: WorkflowConfig, manifest: dict[str, Any], kind: str,
                    original_name: str, data: bytes) -> tuple[dict[str, Any], bool]:
    deal_id = manifest["deal_id"]
    original = Path(str(original_name).replace("\\", "/")).name[:200] or f"{kind}"
    ext = Path(original).suffix.lower()
    allowed = config.allowed_extensions.get(kind) or ()
    if ext not in allowed:
        raise Refusal("BAD_EXTENSION", f"{original!r} is not an allowed {kind} file",
                      f"Use one of {', '.join(allowed) or 'no extension is allowed for this kind'} "
                      f"for kind '{kind}'.")
    if len(data) > config.max_upload_bytes:
        raise Refusal("TOO_LARGE", f"{original} is {len(data):,} bytes; the limit is {config.max_upload_bytes:,}",
                      "Send a smaller export, or raise PLAT_UW_MAX_UPLOAD_MB / [workspace] max_upload_mb.")
    if not data:
        raise Refusal("EMPTY_FILE", f"{original} is empty", "Send the file's contents.")
    if kind == "pca" and ext == ".json":
        from engine.ingest.ingestion_pipeline import clean_pca

        try:
            clean_pca(json.loads(data.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise Refusal("BAD_PCA", f"the PCA JSON is not usable: {exc}",
                          'Send {"immediate_capex": $, "recurring_capex_per_unit": $/unit/yr, "as_of": '
                          '"YYYY-MM-DD", "reference": "..."}, or use describe_deal(pca_immediate_capex=...).') from None
    documents = manifest.setdefault("documents", {})
    replaced = kind in documents
    for old in ws.path(deal_id, DOCUMENTS_DIR).glob(f"{kind}.*"):
        old.unlink()
    stored_as = f"{DOCUMENTS_DIR}/{kind}{ext}"
    ws.write_bytes(deal_id, stored_as, data)
    record = {"kind": kind, "original_filename": original, "stored_as": stored_as, "bytes": len(data),
              "sha256": hashlib.sha256(data).hexdigest(), "added_at": now_iso()}
    documents[kind] = record
    _bump(manifest, "documents")
    return record, replaced


@tool
def add_document(deal_id: str, kind: str, filename: str, content_base64: str | None = None,
                 path: str | None = None) -> dict[str, Any]:
    ws, config = _ws(), _config()
    manifest = ws.load(deal_id)
    if kind not in DOCUMENT_KINDS:
        raise Refusal("BAD_KIND", f"kind {kind!r} is not a document kind",
                      f"Use one of: {', '.join(DOCUMENT_KINDS)} (t12 and rent_roll are required).")
    if content_base64 is None and path is None:
        raise Refusal("NO_CONTENT", "no file content was given",
                      "Pass the file as content_base64 (base64 of the file bytes)"
                      + ("" if _MODE["remote"] else ", or a local path") + ".")
    if content_base64 is not None and path is not None:
        raise Refusal("AMBIGUOUS_CONTENT", "both content_base64 and path were given", "Pass only one of them.")
    if path is not None:
        if _MODE["remote"]:
            raise Refusal("PATH_NOT_ALLOWED", "this server is remote and does not read server-side paths",
                          "Send the file's bytes as content_base64 instead.")
        source = Path(path).expanduser()
        if not source.is_file():
            raise Refusal("FILE_NOT_FOUND", f"{path} is not a file", "Check the path, or send content_base64.")
        if source.stat().st_size > config.max_upload_bytes:
            raise Refusal("TOO_LARGE", f"{source.name} is {source.stat().st_size:,} bytes; the limit is "
                          f"{config.max_upload_bytes:,}", "Use a smaller export, or raise PLAT_UW_MAX_UPLOAD_MB.")
        data, original = source.read_bytes(), source.name
    else:
        text = "".join(str(content_base64).split())
        if len(text) * 3 // 4 > config.max_upload_bytes + 3:
            raise Refusal("TOO_LARGE", f"{filename} is about {len(text) * 3 // 4:,} bytes; the limit is "
                          f"{config.max_upload_bytes:,}", "Send a smaller export, or raise PLAT_UW_MAX_UPLOAD_MB.")
        try:
            data = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError):
            raise Refusal("BAD_BASE64", "content_base64 is not valid base64",
                          "Encode the file bytes with standard base64 and send them again.") from None
        original = filename
    record, replaced = _store_document(ws, config, manifest, kind, original, data)
    ws.save(manifest)
    missing = [k for k in ("t12", "rent_roll") if k not in manifest["documents"]]
    verb = "Replaced" if replaced else "Added"
    next_step = (f"Add the {' and '.join(missing)} next." if missing else
                 f"Describe the deal with describe_deal(deal_id='{deal_id}', start=..., end=..., purchase_price=..., "
                 "millage=..., ...), then ingest_deal." if _missing_for_ingest(manifest) else
                 f"Call ingest_deal(deal_id='{deal_id}').")
    return ok(f"{verb} the {kind} ({record['original_filename']}, {record['bytes']:,} bytes).", next_step,
              document=record, replaced=replaced, **_workspace_field(ws, deal_id))


# ── 3. describe ───────────────────────────────────────────────────────────

_RECOMMENDED = ("purchase_price", "ltv", "rate", "amort", "millage", "year_built")


@tool
def describe_deal(deal_id: str, **updates: Any) -> dict[str, Any]:
    ws = _ws()
    manifest = ws.load(deal_id)
    cleaned, errors = profile_fields.check_profile_updates(updates)
    if errors:
        raise Refusal("BAD_FIELD", "; ".join(errors),
                      "Correct those fields (see profile_fields for each field's format) and call describe_deal again.",
                      profile_fields=profile_fields.field_table())
    profile = dict(manifest.get("profile") or {})
    if "deal_type" in cleaned:
        manifest["deal_type"] = cleaned.pop("deal_type")
    merged = {**profile, **cleaned}
    if merged.get("start") and merged.get("end") and merged["end"] <= merged["start"]:
        raise Refusal("BAD_FIELD", f"end {merged['end']} must be after start {merged['start']}",
                      "Pass an analysis end month after the start month.")
    updated = sorted(cleaned) + (["deal_type"] if "deal_type" in updates and updates["deal_type"] is not None else [])
    if updated:
        manifest["profile"] = merged
        _bump(manifest, "describe")
        ws.save(manifest)
    missing = _missing_for_ingest(manifest)
    recommended = [name for name in _RECOMMENDED if name not in merged]
    data: dict[str, Any] = {"deal_id": deal_id, "deal_type": manifest["deal_type"], "profile": merged,
                            "updated": updated, "missing_for_ingest": missing, "recommended_next": recommended}
    if not updated:
        data["profile_fields"] = profile_fields.field_table()
    summary = (f"Updated {', '.join(updated)}." if updated else "No fields given; the profile is unchanged.")
    if missing:
        next_step = f"Still needed before ingest: {', '.join(missing)}."
    elif recommended:
        next_step = (f"Call ingest_deal(deal_id='{deal_id}'). To run the deal you will also want: "
                     f"{', '.join(recommended)} (ask the analyst).")
    else:
        next_step = f"Call ingest_deal(deal_id='{deal_id}')."
    return ok(summary, next_step, **data)


# ── 4. ingest ─────────────────────────────────────────────────────────────

_BLOCKER_FIXES = {
    "t12_opex_does_not_reconcile": (
        "The parsed T12 expense lines do not add up to the statement's own total. Re-export the full trailing-12 "
        "statement (every GL line and the total rows) and add it again with add_document(kind='t12')."),
    "t12_income_does_not_reconcile": (
        "The parsed T12 income lines do not add up to the statement's own total. Re-export the full trailing-12 "
        "statement and add it again with add_document(kind='t12')."),
    "rent_roll": (
        "The parsed rent roll does not tie to the export's own summary. Re-export the full rent roll (every unit, "
        "vacant and non-revenue units included, with its summary block) and add it again with "
        "add_document(kind='rent_roll')."),
}
_VALIDATION_FIXES = {
    "PROPERTY_TAX_MILLAGE_MISSING": "describe_deal(deal_id, millage=<mills per $1,000>, assessment_ratio=<ratio>), "
                                    "then ingest_deal again.",
    "INGEST_BLOCKED": "Fix the ingest blockers listed under gate.blockers, then ingest_deal again.",
}
_PATH_FIXES = (
    ("/purchase_assumptions", "describe_deal(deal_id, purchase_price=...), then ingest_deal (or use solve_price)."),
    ("/debt_terms", "describe_deal(deal_id, ltv=..., rate=..., amort=..., io_months=...), then ingest_deal."),
    ("/exit_assumptions", "describe_deal(deal_id, exit_cap=... or leave it to the house rule, exit_month=...)."),
    ("/fund_assumptions", "describe_deal(deal_id, sponsor_pct=..., pref_return=...), then ingest_deal."),
    ("/metadata/property_summary/property_tax_policy", _VALIDATION_FIXES["PROPERTY_TAX_MILLAGE_MISSING"]),
)


def _validation_fix(issue: Mapping[str, Any]) -> str:
    if issue.get("code") in _VALIDATION_FIXES:
        return _VALIDATION_FIXES[issue["code"]]
    path = str(issue.get("path") or "")
    for prefix, fix in _PATH_FIXES:
        if path.startswith(prefix):
            return fix
    return f"Check {path or 'the deal'}: {issue.get('message')}"


def _validation(canonical: Mapping[str, Any]) -> dict[str, Any]:
    from engine.validator import validate_deal

    report = validate_deal(canonical).to_dict()
    issues = report.get("issues") or []
    errors = [{"code": i.get("code"), "path": i.get("path"), "message": i.get("message"), "fix": _validation_fix(i)}
              for i in issues if i.get("severity") == "ERROR"]
    return {"status": report.get("status"), "errors": errors,
            "warning_count": sum(1 for i in issues if i.get("severity") == "WARNING")}


def missing_to_run(canonical: Mapping[str, Any]) -> list[str]:
    """describe_deal fields the deal still needs before run_deal can price it (empty when ready)."""
    missing = []
    if not _get(canonical, "purchase_assumptions", "purchase_price"):
        missing.append("purchase_price")
    if not isinstance(canonical.get("debt_terms"), Mapping):
        missing += ["ltv", "rate", "amort"]
    if _get(canonical, "metadata", "property_summary", "property_tax_policy", "millage_rate_mills") is None:
        missing.append("millage")
    return missing


def _engine_fix(message: str) -> str:
    for code, fix in _VALIDATION_FIXES.items():
        if code in message:
            return fix
    return "Fix the input named in the message (usually with describe_deal), then ingest_deal and run_deal again."


def _gate(canonical: Mapping[str, Any]) -> dict[str, Any]:
    gate = ((canonical.get("metadata") or {}).get("ingest_gate") or {})
    blockers = []
    for blocker in gate.get("blockers") or []:
        code = str(blocker.get("code") or "")
        fix = _BLOCKER_FIXES.get(code) or (_BLOCKER_FIXES["rent_roll"] if code.startswith("rent_roll") else
                                           "Fix the source document and call ingest_deal again.")
        blockers.append({"code": code, "message": blocker.get("message"), "fix": fix})
    t12 = {}
    for side, entry in ((gate.get("t12_reconciliation") or {}).items()):
        if isinstance(entry, Mapping) and "status" in entry:
            t12[side] = {k: entry.get(k) for k in ("status", "parsed_total", "statement_total", "variance_pct")}
    rent_roll = gate.get("rent_roll_reconciliation") or {}
    return {
        "status": gate.get("status", "UNVERIFIED"),
        "blockers": blockers,
        "t12_reconciliation": t12,
        "rent_roll_reconciliation": {
            "status": rent_roll.get("status"),
            **{check: {k: (rent_roll.get(check) or {}).get(k) for k in ("status", "parsed", "export")}
               for check in ("units", "vacant_units", "market_rent_total", "rent_charges_total")
               if isinstance(rent_roll.get(check), Mapping)},
        },
        "assumed_defaults": gate.get("assumed_defaults") or [],
    }


def _market_status(canonical: Mapping[str, Any]) -> dict[str, Any]:
    from engine.market_data.diagnostics import disabled_status, status_lines

    status = (canonical.get("assumptions") or {}).get("market_data_status")
    if not isinstance(status, Mapping):
        status = disabled_status("this deal was resolved without market data")
    return {"enabled": status.get("enabled"), "summary": status.get("summary"),
            "lines": status_lines(status, indent="")}


def _pca_evidence(ws: Workspace, manifest: Mapping[str, Any]) -> dict[str, Any] | None:
    from engine.ingest.ingestion_pipeline import clean_pca

    pca: dict[str, Any] = {}
    doc = (manifest.get("documents") or {}).get("pca")
    if doc and doc["stored_as"].endswith(".json"):
        pca.update(json.loads(ws.path(manifest["deal_id"], doc["stored_as"]).read_text(encoding="utf-8")))
    profile = manifest.get("profile") or {}
    for field, key in (("pca_immediate_capex", "immediate_capex"),
                       ("pca_recurring_capex_per_unit", "recurring_capex_per_unit"),
                       ("pca_as_of", "as_of"), ("pca_reference", "reference")):
        if profile.get(field) is not None:
            pca[key] = profile[field]
    return clean_pca(pca) if pca else None


SUBMARKET_VACANCY_SOURCE = "describe_deal:submarket_vacancy"


def _market_vacancy_benchmark(profile: Mapping[str, Any]) -> dict[str, str] | None:
    """The analyst submarket vacancy benchmark from the deal profile, or None.

    Ingest validates it (``rate``, ``as_of``, ``source``): a rate without its
    date is refused there with a message the analyst can act on.
    """
    rate = profile.get("submarket_vacancy")
    if rate is None:
        return None
    return {"rate": str(rate), "as_of": profile.get("submarket_vacancy_as_of"),
            "source": profile.get("submarket_vacancy_source") or SUBMARKET_VACANCY_SOURCE}


def _ingest_kwargs(ws: Workspace, manifest: Mapping[str, Any], market: Any) -> dict[str, Any]:
    from engine.ingest.broker_om_snapshot import stable_property_tax_source_locator

    deal_id = manifest["deal_id"]
    p = manifest.get("profile") or {}
    docs = manifest["documents"]
    kwargs: dict[str, Any] = {
        "property_id": manifest["name"],
        "rent_roll_path": ws.path(deal_id, docs["rent_roll"]["stored_as"]),
        "t12_path": ws.path(deal_id, docs["t12"]["stored_as"]),
        "analysis_start": p["start"],
        "analysis_end": p["end"],
        "rent_growth_rate": p.get("rent_growth"),
        "vacancy_rate_override": p.get("vacancy"),
        "collection_loss_rate": p.get("collection_loss"),
        "analyst": p.get("analyst") or "",
        "purchase_price": p.get("purchase_price"),
        "ltv": p.get("ltv"),
        "loan_rate": p.get("rate"),
        "amort_years": p.get("amort"),
        "io_months": p.get("io_months") or 0,
        "term_months": p.get("term_months"),
        "exit_cap_rate": p.get("exit_cap"),
        "exit_month": p.get("exit_month"),
        "sale_cost_pct": p.get("sale_cost"),
        "sponsor_equity_pct": p.get("sponsor_pct"),
        "pref_return": p.get("pref_return"),
        "acq_fee_pct": p.get("acq_fee"),
        "am_fee_pct": p.get("am_fee"),
        "disposition_fee_pct": p.get("disp_fee"),
        "year_built": p.get("year_built"),
        "deal_type": manifest["deal_type"],
        "msa": p.get("msa"),
        "submarket": p.get("submarket"),
        "metro_slug": p.get("metro_slug"),
        "asset_class": p.get("asset_class"),
        "star_tier": p.get("star_tier"),
        "assumption_overrides": dict(manifest.get("edits") or {}) or None,
        "market": market,
        "deferred_maintenance": bool(p.get("deferred_maintenance")),
        "pca": _pca_evidence(ws, manifest),
        "immediate_capex": p.get("immediate_capex"),
        "immediate_capex_funding": p.get("immediate_capex_funding"),
        "recurring_capex_per_unit": p.get("recurring_capex_per_unit"),
        "property_tax_millage": p.get("millage"),
        "property_tax_assessment_ratio": p.get("assessment_ratio"),
        "property_tax_source_locator": "describe_deal:millage",
        "market_vacancy_benchmark": _market_vacancy_benchmark(p),
    }
    om = docs.get("om")
    if om:
        om_path = ws.path(deal_id, om["stored_as"])
        kwargs.update(om_path=om_path, om_source_locator=stable_property_tax_source_locator(om_path))
    return kwargs


@tool
def ingest_deal(deal_id: str) -> dict[str, Any]:
    from engine.assumptions import AssumptionOverrideError, UnsupportedDealTypeError
    from engine.assumptions.register import build_register
    from engine.ingest.ingestion_pipeline import build_deal_from_documents
    from engine.ingest.partial_patch import apply_partial_and_reresolve

    ws = _ws()
    manifest = ws.load(deal_id)
    docs = manifest.get("documents") or {}
    missing_docs = [k for k in ("t12", "rent_roll") if k not in docs]
    if missing_docs:
        raise Refusal("DOCUMENTS_MISSING", f"{deal_id} has no {' or '.join(missing_docs)} yet",
                      f"Add the {' and '.join(missing_docs)} with add_document(deal_id='{deal_id}', kind=...): "
                      "t12 is the trailing-12 operating statement, rent_roll the current rent roll.")
    profile = manifest.get("profile") or {}
    absent = [name for name in profile_fields.REQUIRED_FOR_INGEST if not profile.get(name)]
    if absent:
        raise Refusal("PROFILE_INCOMPLETE", f"the deal profile has no {' or '.join(absent)}",
                      f"Ask the analyst for the analysis period and call describe_deal(deal_id='{deal_id}', "
                      "start='YYYY-MM', end='YYYY-MM') (a five-year hold starting the month after closing is typical).")
    market = _market(profile)
    try:
        canonical = build_deal_from_documents(**_ingest_kwargs(ws, manifest, market))
        if profile.get("loan_sizing_noi_basis"):
            canonical = apply_partial_and_reresolve(
                canonical, {"loan_sizing_noi_basis": profile["loan_sizing_noi_basis"]},
                market=market, note="set with describe_deal")
    except (UnsupportedDealTypeError, AssumptionOverrideError) as exc:
        raise Refusal("INGEST_REFUSED", str(exc), "Correct the deal type or the edit named above and ingest again.")
    except (ValueError, KeyError) as exc:
        raise Refusal("INGEST_FAILED", f"the documents could not be ingested: {exc}",
                      "Check the t12 is a trailing-12 operating statement and the rent_roll a unit-level rent "
                      "roll export (Excel or CSV), then add them again with add_document.")
    _save_canonical(ws, deal_id, canonical)

    gate = _gate(canonical)
    validation = _validation(canonical)
    register = build_register(canonical, include_candidates=False)
    counts = register["summary"]
    review_keys = [row["key"] for row in register["rows"] if row["review_required"]]
    units = _units(canonical)
    manifest["last_ingest"] = {
        "at": now_iso(), "gate_status": gate["status"], "blockers": [b["code"] for b in gate["blockers"]],
        "units": units, "assumptions": counts["total"], "needing_review": counts["needing_review"],
        "validation_status": validation["status"], "validation_errors": [e["code"] for e in validation["errors"]],
        "missing_to_run": missing_to_run(canonical),
    }
    _bump(manifest, "ingest")
    ws.save(manifest)

    summary = (f"Ingested {manifest['name']}: {units} units; ingest gate {gate['status']}; "
               f"{counts['total']} assumptions resolved, {counts['needing_review']} flagged for review; "
               f"validation {validation['status']}.")
    to_run = missing_to_run(canonical)
    if to_run:
        summary += f" Before run_deal it still needs: {', '.join(to_run)}."
    if gate["blockers"]:
        next_step = "Fix each blocker (gate.blockers[].fix), then call ingest_deal again."
    elif validation["errors"]:
        next_step = "Resolve the validation errors (validation.errors[].fix), then call ingest_deal again."
    elif to_run:
        next_step = (f"Ask the analyst for {', '.join(to_run)} and call describe_deal(deal_id='{deal_id}', ...), "
                     "then ingest_deal again; review_assumptions works now.")
    else:
        next_step = (f"Call review_assumptions(deal_id='{deal_id}', filter='needs_review') and walk the analyst "
                     "through the flagged assumptions; then run_deal.")
    return ok(
        summary, next_step,
        deal_id=deal_id,
        units=units,
        gate=gate,
        validation=validation,
        missing_to_run=to_run,
        assumptions={
            "total": counts["total"], "needing_review": counts["needing_review"],
            "from_deal_evidence": counts["from_deal_evidence"], "from_market": counts["from_market"],
            "house_defaults": counts["house_defaults"], "engine_fallbacks": counts["engine_fallbacks"],
            "edited_by_user": counts["edited_by_user"], "review_keys": review_keys,
        },
        capital_plan=register.get("capital_plan"),
        market_data=_market_status(canonical),
        register_resource=f"deal://{deal_id}/assumptions.html",
    )


# ── 5. review ─────────────────────────────────────────────────────────────


def _register_case(ws: Workspace, deal_id: str, case: str) -> dict[str, Any]:
    if case == "deal":
        return _canonical(ws, deal_id)
    if case == "priced":
        priced = ws.read_json(deal_id, PRICED_CANONICAL)
        if not isinstance(priced, dict):
            raise Refusal("NOT_PRICED", f"{deal_id} has no solved price yet",
                          f"Call solve_price(deal_id='{deal_id}', ...) first, or review case='deal'.")
        return priced
    raise Refusal("BAD_CASE", f"case {case!r} is not deal or priced",
                  "Use case='deal' (the ingested deal) or case='priced' (the solved price).")


@tool
def review_assumptions(deal_id: str, filter: str = "all", case: str = "deal",
                       include_candidates: bool = False) -> dict[str, Any]:
    from engine.assumptions.register import build_register

    if filter not in REVIEW_FILTERS:
        raise Refusal("BAD_FILTER", f"filter {filter!r} is not supported",
                      f"Use one of: {', '.join(REVIEW_FILTERS)}.")
    ws = _ws()
    ws.load(deal_id)
    canonical = _register_case(ws, deal_id, case)
    register = build_register(canonical, include_candidates=include_candidates)
    rows = register["rows"]
    narrowed = REVIEW_FILTERS[filter]
    if narrowed is None:
        rows = [row for row in rows if (row.get("guardrail") or {}).get("state") == "enforced"]
    else:
        rows = [row for row in rows if narrowed in row["filters"]]
    counts = register["summary"]
    summary = (f"{len(rows)} of {counts['total']} assumptions ({filter}); {counts['needing_review']} flagged for "
               f"review, {counts['edited_by_user']} edited, {counts['house_defaults']} house defaults.")
    if filter == "enforced" and case == "deal" and not rows:
        next_step = "House floors are enforced only when pricing: review case='priced' after solve_price."
    else:
        next_step = (f"Ask the analyst about anything that looks off and apply changes with "
                     f"edit_assumptions(deal_id='{deal_id}', edits={{key: {{'value': ..., 'note': ...}}}}, "
                     "editor=...); otherwise call run_deal.")
    return ok(summary, next_step, deal_id=deal_id, case=case, filter=filter, rows=rows,
              summary_counts=counts, capital_plan=register.get("capital_plan"),
              register_resource=f"deal://{deal_id}/assumptions.html")


# ── 6. edit ───────────────────────────────────────────────────────────────


def _row_view(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {"value": row.get("value"), "value_display": row.get("value_display"), "source": row.get("source"),
            "user_edit": row.get("user_edit")}


@tool
def edit_assumptions(deal_id: str, edits: dict[str, Any], editor: str) -> dict[str, Any]:
    from engine.assumptions import AssumptionOverrideError, UnsupportedDealTypeError, normalize_overrides
    from engine.assumptions import resolve_assumptions
    from engine.assumptions.register import build_register
    from engine.assumptions.sheet import stamp_overrides
    from engine.ingest.ingestion_pipeline import sync_ingest_gate_assumptions

    ws = _ws()
    manifest = ws.load(deal_id)
    if not isinstance(editor, str) or not editor.strip() or len(editor) > 100:
        raise Refusal("EDITOR_REQUIRED", "edits need the editor's name", "Pass editor='<the analyst's name>'.")
    if not isinstance(edits, dict) or not edits:
        raise Refusal("NO_EDITS", "no edits were given",
                      "Pass edits={key: {'value': v, 'note': 'why'}} (or {key: null} to clear an edit).")
    canonical = _canonical(ws, deal_id)
    try:
        normalize_overrides(edits)
    except AssumptionOverrideError as exc:
        raise Refusal("BAD_EDIT", str(exc), "Use assumption keys and value shapes from review_assumptions.") from None
    before = {row["key"]: row for row in build_register(canonical, include_candidates=False)["rows"]}
    edited_at = datetime.now(timezone.utc).date().isoformat()
    stamped = stamp_overrides(edits, canonical, edited_by=editor.strip(), edited_at=edited_at)
    try:
        resolved = resolve_assumptions(canonical, overrides=stamped, market=_market(manifest.get("profile") or {}))
    except (AssumptionOverrideError, UnsupportedDealTypeError, ValueError) as exc:
        raise Refusal("BAD_EDIT", str(exc), "Use assumption keys and value shapes from review_assumptions.") from None
    sync_ingest_gate_assumptions(resolved)
    _save_canonical(ws, deal_id, resolved)

    entries = (resolved.get("assumptions") or {}).get("entries") or {}
    after = {row["key"]: row for row in build_register(resolved, include_candidates=False)["rows"]}
    recorded = manifest.setdefault("edits", {})
    changed = []
    for key in stamped:
        override = (entries.get(key) or {}).get("override")
        source = (entries.get(key) or {}).get("source")
        if isinstance(override, Mapping) and source == "analyst":
            recorded[key] = dict(override)
        else:
            recorded.pop(key, None)
        row = after.get(key) or {}
        changed.append({
            "key": key, "label": row.get("label"), "before": _row_view(before.get(key)), "after": _row_view(row),
            "default": row.get("default"),
            "delta": (row.get("user_edit") or {}).get("delta_display"),
            "cleared": stamped[key] is None or (isinstance(stamped[key], Mapping) and stamped[key].get("value") is None),
        })
    _bump(manifest, "edit")
    ws.save(manifest)
    names = ", ".join(f"{c['label'] or c['key']} {'cleared' if c['cleared'] else (c['after'] or {}).get('value_display')}"
                      for c in changed)
    return ok(f"Applied {len(changed)} edit(s) by {editor.strip()}: {names}.",
              f"Call run_deal(deal_id='{deal_id}') to see the effect (earlier run and price results are now stale).",
              deal_id=deal_id, changed=changed, edits=recorded)


# ── 7. run ────────────────────────────────────────────────────────────────


def _get(data: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(data, Mapping):
            return None
        data = data.get(key)
    return data


def _grid(grid: Any) -> dict[str, Any] | None:
    if not isinstance(grid, Mapping) or not grid.get("points"):
        return None
    return {
        "base_exit_cap_rate": grid.get("base_exit_cap_rate"),
        "rule": grid.get("rule"),
        "points": [{k: p.get(k) for k in ("offset_bps", "exit_cap_rate", "exit_value", "levered_irr", "unlevered_irr")}
                   for p in grid["points"] if isinstance(p, Mapping)],
    }


def headline_metrics(results: Mapping[str, Any]) -> dict[str, Any]:
    m = results.get("metrics") or {}
    return {
        "noi_year_1": _get(m, "noi", "year_1_noi"),
        "noi_stabilized_year": _get(m, "noi", "stabilized_year_noi"),
        "stabilized_year": _get(m, "noi", "stabilized_year"),
        "cash_on_cash_year_1": _get(m, "coc", "cash_on_cash_year_1"),
        "levered_irr": _get(m, "irr", "levered_irr"),
        "unlevered_irr": _get(m, "irr", "unlevered_irr"),
        "levered_equity_multiple": _get(m, "equity_multiple", "levered_em"),
        "unlevered_equity_multiple": _get(m, "equity_multiple", "unlevered_em"),
        "going_in_cap_rate": _get(m, "yields", "going_in_cap_rate"),
        "dscr": {
            "basis": _get(m, "dscr", "basis"),
            "basis_label": _get(m, "dscr", "basis_label"),
            "minimum": _get(m, "dscr", "minimum_dscr"),
            "average": _get(m, "dscr", "average_dscr"),
            "minimum_noi_before_capex": _get(m, "dscr", "noi_before_capex", "minimum_dscr"),
        },
        "exit": {
            "month": _get(m, "exit", "exit_month"),
            "cap_rate": _get(m, "yields", "exit_cap_rate"),
            "value": _get(m, "exit", "gross_sale_price"),
            "net_sale_proceeds": _get(m, "exit", "net_sale_proceeds"),
            "noi": _get(m, "exit", "exit_noi"),
            "noi_window": _get(m, "exit", "exit_noi_window"),
            "sale_costs": _get(m, "exit", "sale_costs"),
            "loan_payoff": _get(m, "exit", "loan_payoff"),
        },
    }


_ANNUAL_FIELDS = (
    ("effective_gross_income", "effective_gross_income"),
    ("total_opex", "total_opex"),
    ("net_operating_income", "net_operating_income"),
    ("capex_charge", "replacement_reserves"),
    ("other_capex", "total_capex"),
    ("debt_service", "debt_service"),
)


def annual_cash_flow(results: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The monthly cash flow summed by analysis year (twelve months from the analysis start).

    Operating only: the sale (net proceeds, loan payoff) is left out of both cash flows and reported
    under the exit. ``capex_charge`` is the charge below NOI, max(recurring capex, the reserve).
    """
    months = [m for m in (_get(results, "cashflow", "by_month") or []) if isinstance(m, Mapping)]
    years: list[dict[str, Any]] = []
    for index in range(0, len(months), 12):
        chunk = months[index:index + 12]
        row: dict[str, Any] = {"analysis_year": index // 12 + 1, "start": chunk[0].get("month"),
                               "end": chunk[-1].get("month"), "months": len(chunk)}
        for name, source in _ANNUAL_FIELDS:
            row[name] = round(sum(float(m.get(source) or 0.0) for m in chunk), 2)
        sale = sum(float(m.get("net_sale_proceeds") or 0.0) for m in chunk)
        payoff = sum(float(m.get("loan_payoff") or 0.0) for m in chunk)
        row["cash_flow_before_debt"] = round(
            sum(float(m.get("unleveraged_cash_flow") or 0.0) for m in chunk) - sale, 2)
        row["cash_flow_after_debt"] = round(
            sum(float(m.get("leveraged_cash_flow") or 0.0) for m in chunk) - sale + payoff, 2)
        years.append(row)
    return years


def _pct(value: Any) -> str:
    return "n/a" if not isinstance(value, (int, float)) else f"{value:.2%}"


def _usd(value: Any) -> str:
    return "n/a" if not isinstance(value, (int, float)) else f"${value:,.0f}"


def _x(value: Any) -> str:
    return "n/a" if not isinstance(value, (int, float)) else f"{value:.2f}x"


@tool
def run_deal(deal_id: str) -> dict[str, Any]:
    from engine.assumptions.register_html import write_register_html
    from engine.assumptions.sheet import has_assumptions, write_assumptions_json
    from engine.engine import run_underwriting
    from engine.excel_output import write_results_to_workbook
    from engine.market_data.diagnostics import disabled_status

    ws = _ws()
    manifest = ws.load(deal_id)
    canonical = _canonical(ws, deal_id)
    run_dir = f"{RUN_DIR}"
    ws.write_json(deal_id, f"{run_dir}/canonical.json", canonical)
    validation = _validation(canonical)
    from engine.validator import validate_deal

    ws.write_json(deal_id, f"{run_dir}/validation.json", validate_deal(canonical).to_dict())
    to_run = missing_to_run(canonical)
    if to_run:
        raise Refusal("NOT_READY", f"the deal still needs {', '.join(to_run)} before it can run",
                      f"Ask the analyst for {', '.join(to_run)}, call describe_deal(deal_id='{deal_id}', ...), then "
                      "ingest_deal and run_deal again (or solve_price to find a price; it still needs the millage).",
                      missing_to_run=to_run)
    if validation["status"] != "PASS":
        raise Refusal("VALIDATION_FAILED", f"the deal does not validate ({len(validation['errors'])} error(s))",
                      "Fix each error (validation.errors[].fix), then ingest_deal and run_deal again.",
                      validation=validation)
    try:
        results = run_underwriting(canonical)
    except ValueError as exc:
        raise Refusal("ENGINE_REFUSED", f"the engine refused the deal: {exc}", _engine_fix(str(exc)))
    status = (canonical.get("assumptions") or {}).get("market_data_status")
    results.setdefault("metadata", {})["market_data_status"] = (
        status if isinstance(status, Mapping) else disabled_status("the canonical records no market-data status"))
    ws.write_json(deal_id, f"{run_dir}/results.json", results)
    if has_assumptions(canonical):
        write_assumptions_json(canonical, ws.path(deal_id, f"{run_dir}/assumptions.json"))
        write_register_html(canonical, ws.path(deal_id, f"{run_dir}/assumptions.html"))
    write_results_to_workbook(results, ws.path(deal_id, f"{run_dir}/underwriting.xlsx"), inputs=canonical)

    metrics = headline_metrics(results)
    manifest["last_run"] = {"at": now_iso(), **{k: metrics[k] for k in (
        "noi_year_1", "cash_on_cash_year_1", "levered_irr", "unlevered_irr", "levered_equity_multiple")},
        "minimum_dscr": metrics["dscr"]["minimum"], "exit_cap_rate": metrics["exit"]["cap_rate"]}
    _bump(manifest, "run")
    ws.save(manifest)
    dscr = metrics["dscr"]
    from engine.modules.loan_sizing import SHORT_LABELS

    dscr_basis = SHORT_LABELS.get(dscr["basis"], dscr["basis"] or "NOI")
    summary = (f"{manifest['name']}: year-1 NOI {_usd(metrics['noi_year_1'])}, year-1 cash-on-cash "
               f"{_pct(metrics['cash_on_cash_year_1'])}, levered IRR {_pct(metrics['levered_irr'])} "
               f"(unlevered {_pct(metrics['unlevered_irr'])}), levered EM {_x(metrics['levered_equity_multiple'])}, "
               f"minimum DSCR {_x(dscr['minimum'])} on {dscr_basis} ({_x(dscr['minimum_noi_before_capex'])} "
               f"on NOI); exit at {_pct(metrics['exit']['cap_rate'])} cap for {_usd(metrics['exit']['value'])}.")
    return ok(summary,
              f"Solve a price with solve_price(deal_id='{deal_id}', target_coc=..., benchmark_5yr_treasury=...), or "
              f"export_deal(deal_id='{deal_id}') for the workbook.",
              deal_id=deal_id, metrics=metrics, capital_plan=results.get("capital_plan"),
              exit_cap_grid=_grid(results.get("exit_cap_sensitivity")),
              annual_cash_flow=annual_cash_flow(results),
              validation={"status": validation["status"], "warning_count": validation["warning_count"]},
              files=[f"{run_dir}/{name}" for name in ("results.json", "underwriting.xlsx", "assumptions.html",
                                                      "canonical.json", "validation.json")])


# ── 8. price ──────────────────────────────────────────────────────────────


def _floors_enforced(summary: Mapping[str, Any]) -> list[str]:
    from engine.assumptions.guardrails import ENFORCING_ACTIONS

    findings = _get(summary, "policy_summary", "guardrails", "findings") or []
    return sorted({f.get("key") for f in findings if isinstance(f, Mapping) and f.get("action") in ENFORCING_ACTIONS
                   and f.get("key")})


def _num(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def pricing_view(result: Mapping[str, Any], summary: Mapping[str, Any]) -> dict[str, Any]:
    units = _get(summary, "policy_summary", "units")
    price = _num(summary.get("solved_purchase_price"))
    sizing = summary.get("loan_sizing") or {}
    basis = summary.get("coc_basis") or {}
    return {
        "status": summary.get("status"),
        "target_coc": _num(summary.get("target_cash_on_cash_pct")) or _num(result["effective_assumptions"]["target_coc"]),
        "coc_basis": basis.get("basis") if isinstance(basis, Mapping) else basis,
        "coc_basis_year": basis.get("analysis_year") if isinstance(basis, Mapping) else None,
        "achieved_coc": _num(summary.get("cash_on_cash_persisted")),
        "cash_on_cash_year_1": _num(summary.get("cash_on_cash_year_1")),
        "price": price,
        "units": units,
        "price_per_unit": price / units if price and units else None,
        "equity": _num(summary.get("solved_total_equity_basis")),
        "loan": {
            "amount": _num(summary.get("loan_amount")),
            "rate": _num(summary.get("interest_rate")),
            "io_months": summary.get("io_months"),
            "term_months": summary.get("term_months"),
            "sizing_basis": sizing.get("basis"),
            "sizing_method": sizing.get("sizing_method"),
            "sizing_source": sizing.get("source"),
        },
        "going_in_cap_rate": _num(summary.get("going_in_cap_rate")),
        "exit_cap_rate": _num(summary.get("exit_cap_rate")),
        "exit_cap_mode": _get(summary, "exit_cap_calculation", "mode"),
        "levered_irr": _num(summary.get("levered_irr")),
        "unlevered_irr": _num(_get(result.get("results"), "metrics", "irr", "unlevered_irr")),
        "minimum_dscr": {
            "basis": summary.get("minimum_dscr_basis"),
            "minimum": _num(summary.get("minimum_dscr")),
            "minimum_noi_before_capex": _num(summary.get("minimum_dscr_noi_before_capex")),
        },
        "floors_enforced": _floors_enforced(summary),
        "exit_cap_grid": _grid(summary.get("exit_cap_sensitivity")),
        "capital_plan": summary.get("capital_plan"),
        "boundary": summary.get("maximum_feasible_boundary"),
        "benchmark": _get(result, "effective_assumptions", "benchmark"),
        "bracket": result.get("bracket"),
    }


DEFAULT_TARGET_COC = "0.08"
# Keys a sample profile's ``pricing`` block may use for each solve_price input.
_PRICING_ALIASES = {
    "target_coc": ("target_coc", "target_cash_on_cash"),
    "benchmark_5yr_treasury": ("benchmark_5yr_treasury", "benchmark_treasury", "treasury_5yr",
                               "five_year_treasury", "benchmark_rate"),
    "benchmark_as_of": ("benchmark_as_of", "as_of"),
    "benchmark_source": ("benchmark_source", "source"),
}


def pricing_defaults_from(block: Any) -> dict[str, str]:
    """solve_price inputs from a sample profile's ``pricing`` block (flat, or with a ``benchmark`` table)."""
    if not isinstance(block, Mapping):
        return {}
    flat = dict(block)
    bench = block.get("benchmark")
    if isinstance(bench, Mapping):
        renamed = {"rate": "benchmark_rate", "as_of": "benchmark_as_of", "source": "benchmark_source"}
        flat.update({renamed.get(k, k): v for k, v in bench.items()})
    out: dict[str, str] = {}
    for field, aliases in _PRICING_ALIASES.items():
        for alias in aliases:
            if flat.get(alias) not in (None, ""):
                out[field] = str(flat[alias])
                break
    return out


def pricing_inputs(manifest: Mapping[str, Any], **given: Any) -> tuple[Any, Any, Any, Any, list[str]]:
    """(target_coc, treasury, as_of, source, prefilled field names): given values win, then the deal's
    recorded pricing defaults (the sample deal's ``pricing`` block), then target_coc 0.08."""
    recorded = manifest.get("pricing_defaults") or {}
    prefilled: list[str] = []
    values = []
    for field in ("target_coc", "benchmark_5yr_treasury", "benchmark_as_of", "benchmark_source"):
        value = given.get(field)
        if value in (None, "") and recorded.get(field) not in (None, ""):
            value = recorded[field]
            prefilled.append(field)
        values.append(value)
    if values[0] in (None, ""):
        values[0] = DEFAULT_TARGET_COC
    return values[0], values[1], values[2], values[3], prefilled


@tool
def solve_price(deal_id: str, target_coc: str | None = None, benchmark_5yr_treasury: str | None = None,
                benchmark_as_of: str | None = None, benchmark_source: str | None = None,
                coc_basis: str | None = None, exit_cap_rate: str | None = None,
                loan_sizing_noi_basis: str | None = None, min_price: str = "1000000",
                max_price: str = "100000000", max_iterations: int = 40) -> dict[str, Any]:
    from engine.assumptions.profile import DEAL_TYPE_STRATEGY
    from engine.backsolve import backsolve_price, write_backsolve_artifacts
    from engine.backsolve_policy import POLICY_VERSION

    ws = _ws()
    manifest = ws.load(deal_id)
    canonical = _canonical(ws, deal_id)
    target_coc, benchmark_5yr_treasury, benchmark_as_of, benchmark_source, prefilled = pricing_inputs(
        manifest, target_coc=target_coc, benchmark_5yr_treasury=benchmark_5yr_treasury,
        benchmark_as_of=benchmark_as_of, benchmark_source=benchmark_source)
    if benchmark_5yr_treasury in (None, ""):
        raise Refusal("BENCHMARK_REQUIRED", "solve_price needs the 5-year US Treasury yield; it never assumes a rate",
                      "Ask the analyst for today's 5-year US Treasury yield as a decimal (0.0405 for 4.05%), its date "
                      "and source, then pass benchmark_5yr_treasury, benchmark_as_of='YYYY-MM-DD' and benchmark_source.")
    profile = manifest.get("profile") or {}
    policy: dict[str, Any] = {"version": POLICY_VERSION, "strategy": DEAL_TYPE_STRATEGY[manifest["deal_type"]],
                              "year_built": profile.get("year_built")}
    for name, value in (("coc_basis", coc_basis), ("exit_cap_rate", exit_cap_rate),
                        ("loan_sizing_noi_basis", loan_sizing_noi_basis)):
        if value not in (None, ""):
            policy[name] = value
    benchmark = {
        "rate": str(benchmark_5yr_treasury),
        "as_of": benchmark_as_of or datetime.now(timezone.utc).date().isoformat(),
        "source": benchmark_source or "analyst-supplied 5-year US Treasury yield",
    }
    try:
        result = backsolve_price(canonical, target_coc=str(target_coc), policy=policy, benchmark=benchmark,
                                 min_price=str(min_price), max_price=str(max_price), max_iterations=max_iterations)
    except (ValueError, RuntimeError, KeyError, TypeError) as exc:
        message = str(exc)
        fix = (_VALIDATION_FIXES["PROPERTY_TAX_MILLAGE_MISSING"] if "millage" in message.lower() or
               "property tax" in message.lower() else
               "Correct the input named in the message (rates are decimals, prices in dollars with at most "
               "two decimals) and call solve_price again.")
        raise Refusal("PRICE_REFUSED", f"the price search refused: {message}", fix) from None
    price_dir = ws.path(deal_id, PRICE_DIR)
    if price_dir.exists():
        shutil.rmtree(price_dir)
    summary, _ = write_backsolve_artifacts(result, price_dir)
    pricing = pricing_view(result, summary)
    manifest["last_price"] = {"at": now_iso(), "status": pricing["status"], "price": pricing["price"],
                              "price_per_unit": pricing["price_per_unit"], "target_coc": pricing["target_coc"],
                              "achieved_coc": pricing["achieved_coc"], "benchmark": pricing["benchmark"]}
    _bump(manifest, "price")
    ws.save(manifest)
    if pricing["price"] is None:
        text = (f"No price between ${float(min_price):,.0f} and ${float(max_price):,.0f} reaches a "
                f"{_pct(pricing['target_coc'])} cash-on-cash ({pricing['coc_basis']} basis).")
        next_step = ("Lower target_coc or min_price, or review the assumptions with review_assumptions(case='priced' "
                     "is unavailable for an infeasible solve; see price/assumptions.html via export_deal).")
    else:
        floors = pricing["floors_enforced"]
        text = (f"{pricing['status']}: {_usd(pricing['price'])} ({_usd(pricing['price_per_unit'])}/unit) meets a "
                f"{_pct(pricing['target_coc'])} cash-on-cash on the {pricing['coc_basis']} basis (achieved "
                f"{_pct(pricing['achieved_coc'])}); loan {_usd(pricing['loan']['amount'])}, going-in cap "
                f"{_pct(pricing['going_in_cap_rate'])}, exit cap {_pct(pricing['exit_cap_rate'])}, levered IRR "
                f"{_pct(pricing['levered_irr'])}; {len(floors)} house floor(s) enforced"
                + (f" ({', '.join(floors)})." if floors else "."))
        next_step = (f"Review the priced assumptions with review_assumptions(deal_id='{deal_id}', filter='enforced', "
                     f"case='priced'), then export_deal(deal_id='{deal_id}').")
    if prefilled:
        text += f" Used the deal's recorded pricing inputs for {', '.join(prefilled)} (illustrative for the sample)."
    return ok(text, next_step, deal_id=deal_id, pricing=pricing, prefilled_from_deal=prefilled,
              files=sorted(f"{PRICE_DIR}/{p.name}" for p in price_dir.iterdir() if p.is_file()))


# ── export ────────────────────────────────────────────────────────────────


@tool
def export_deal(deal_id: str) -> dict[str, Any]:
    ws = _ws()
    manifest = ws.load(deal_id)
    base_url = _MODE["base_url"] if _MODE["transport"] == "http" else None
    files = []
    for entry in ws.files(deal_id):
        rel = entry["path"]
        item: dict[str, Any] = {"path": rel, "bytes": entry["bytes"], "modified_at": entry["modified_at"],
                                "description": FILE_DESCRIPTIONS.get(rel) or (
                                    "Source document" if rel.startswith(f"{DOCUMENTS_DIR}/") else "Workspace file")}
        if not _MODE["remote"]:
            item["local_path"] = entry["local_path"]
        if base_url:
            item["url"] = f"{base_url}/files/{deal_id}/{quote(rel)}"
        files.append(item)
    where = ("links served by this server (same sign-in as MCP)" if base_url else "local paths")
    return ok(f"{len(files)} file(s) for {manifest['name']}, as {where}.",
              "Open run/underwriting.xlsx for the workbook and assumptions.html for the assumption register.",
              deal_id=deal_id, files=files, register_resource=f"deal://{deal_id}/assumptions.html",
              **_workspace_field(ws, deal_id))


# ── the sample deal ───────────────────────────────────────────────────────


# Keys of the sample profile's ``ingest`` block (runs/ingest_deal.py flags, underscored) that are not
# describe_deal fields: documents and CLI plumbing. ``no_market_data`` maps to use_market_data.
_SAMPLE_SKIP = frozenset({"rent_roll", "t12", "om", "pca", "output", "validate", "partial_json", "canonical_json",
                          "assumption_overrides", "scan_sidecar_workbooks"})


def _sample_profile(folder: Path) -> dict[str, Any]:
    """The sample's profile.json: from ``engine.samples`` when it describes this folder, else the file."""
    try:
        import engine.samples as samples  # the bundled sample module, when installed

        if Path(samples.SAMPLE_DEAL_DIR).resolve() == folder.resolve():
            loaded = samples.load_sample_profile()
            if isinstance(loaded, dict):
                return loaded
    except Exception:  # noqa: BLE001 - fall back to reading profile.json
        pass
    path = folder / "profile.json"
    if not path.is_file():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def _sample_documents(folder: Path, profile: Mapping[str, Any], config: WorkflowConfig) -> dict[str, Path]:
    named = profile.get("documents") if isinstance(profile.get("documents"), Mapping) else {}
    ingest = profile.get("ingest") if isinstance(profile.get("ingest"), Mapping) else {}
    out: dict[str, Path] = {}
    for kind in DOCUMENT_KINDS:
        name = named.get(kind) or ingest.get(kind)
        candidates = [folder / Path(name).name] if isinstance(name, str) and name else []
        candidates += [folder / f"{kind}{ext}" for ext in config.allowed_extensions.get(kind, ())]
        for candidate in candidates:
            if candidate.is_file():
                out[kind] = candidate
                break
    return out


def _sample_fields(profile: Mapping[str, Any]) -> tuple[str | None, str | None, dict[str, Any], list[str]]:
    """(name, deal_type, describe_deal fields, ignored keys) from a nested or flat sample profile."""
    if isinstance(profile.get("ingest"), Mapping):
        block = profile["ingest"]
    else:
        block = {k: v for k, v in profile.items() if k not in ("documents", "pricing", "notes")}
    fields: dict[str, Any] = {}
    ignored: list[str] = []
    name = profile.get("name") or block.get("name") or block.get("property_id")
    deal_type = profile.get("deal_type") or block.get("deal_type")
    for key, value in block.items():
        if key in ("name", "property_id", "deal_type") or key in _SAMPLE_SKIP:
            continue
        if key == "no_market_data":
            if value is True:
                fields["use_market_data"] = False
            continue
        if key in profile_fields.BY_NAME:
            fields[key] = value
        else:
            ignored.append(key)
    return (str(name) if name else None), (str(deal_type) if deal_type else None), fields, sorted(ignored)


@tool
def load_sample_deal() -> dict[str, Any]:
    folder, config = Path(SAMPLE_DEAL_DIR), _config()
    fix = ("The synthetic sample ships at engine/samples/sample_deal/ (t12, rent_roll, profile.json); reinstall the "
           "package or update the checkout, or create_deal with your own documents.")
    if not folder.is_dir():
        raise Refusal("SAMPLE_NOT_FOUND", f"the sample deal is not installed ({folder.name} is missing)", fix)
    profile = _sample_profile(folder)
    documents = _sample_documents(folder, profile, config)
    if "t12" not in documents or "rent_roll" not in documents:
        raise Refusal("SAMPLE_INCOMPLETE", "the sample deal has no t12 or rent_roll file", fix)
    name, deal_type, known, ignored = _sample_fields(profile)
    name = name or "Sample deal (synthetic)"
    deal_type = deal_type or "stabilized"
    cleaned, errors = profile_fields.check_profile_updates(known)
    if errors:
        raise Refusal("SAMPLE_INVALID", f"the sample profile is invalid: {'; '.join(errors)}", fix)
    cleaned.pop("deal_type", None)
    ws, manifest = _create(name, deal_type)
    deal_id = manifest["deal_id"]
    for kind, source in documents.items():
        _store_document(ws, config, manifest, kind, source.name, source.read_bytes())
    if cleaned:
        manifest["profile"] = cleaned
        _bump(manifest, "describe")
    pricing = pricing_defaults_from(profile.get("pricing"))
    if pricing:
        manifest["pricing_defaults"] = {**pricing, "origin": "sample profile.json pricing (illustrative)"}
    ws.save(manifest)
    return ok(f"Loaded the synthetic sample as {name} ({deal_type}), {deal_id}, with its "
              f"{', '.join(sorted(documents))} and profile.",
              f"Call ingest_deal(deal_id='{deal_id}'), then review_assumptions, run_deal and solve_price"
              + (" (the sample carries an illustrative pricing benchmark, so solve_price needs no inputs)."
                 if pricing.get("benchmark_5yr_treasury") else "."),
              deal_id=deal_id, name=name, deal_type=deal_type, profile=cleaned,
              documents=sorted(manifest["documents"]), ignored_profile_fields=ignored,
              pricing_defaults=manifest.get("pricing_defaults"), notes=profile.get("notes"),
              **_workspace_field(ws, deal_id))


# ── resources ─────────────────────────────────────────────────────────────


def register_page(deal_id: str, case: str = "deal") -> str:
    """The assumption register page of a deal (``case='priced'``: with the pricing header)."""
    ws = _ws()
    ws.load(deal_id)
    rel = REGISTER_NAME if case == "deal" else f"{PRICE_DIR}/{REGISTER_NAME}"
    return ws.resolve_file(deal_id, rel).read_text(encoding="utf-8")


def register_row(deal_id: str, key: str) -> dict[str, Any] | None:
    """One assumption's register row, or None when the deal or key is unknown."""
    from engine.assumptions.register import build_register

    try:
        ws = _ws()
        ws.load(deal_id)
        canonical = _canonical(ws, deal_id)
    except (WorkspaceError, Refusal):
        return None
    for row in build_register(canonical, include_candidates=False)["rows"]:
        if row["key"] == key:
            return row
    return None
