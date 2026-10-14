"""MCP server for multifamily underwriting: a guided deal workflow plus stateless tools.

Any MCP-capable assistant (Claude, ChatGPT, Grok, Copilot, Cursor, ...) can
underwrite a deal end to end with these tools. The tool contract is
documented in ``docs/MCP_TOOLS.md``; the implementation is
``engine.workflow.service`` (the MCP layer only declares schemas,
annotations, prompts and resources).

Workflow tools (a persistent deal workspace outside the repository, see
``engine.workflow.config``):

    start_here, list_deals, get_deal, load_sample_deal, create_deal,
    add_document, describe_deal, ingest_deal, review_assumptions,
    edit_assumptions, run_deal, solve_price, export_deal

Stateless tools (``plat.underwriting.mcp/1``; nothing is persisted):

    backsolve_deal_price, validate_deal_inputs, run_deal_summary,
    assumption_register, check_deal_feasibility

Prompts: ``new_deal_walkthrough``, ``explain_assumption``. Resources:
``guide://start-here``, ``guide://user-guide``, ``guide://glossary`` and
``deal://{deal_id}/assumptions.html``.

Launch:

    plat-uw mcp                      # stdio (Claude Desktop and other local clients)
    plat-uw mcp --http               # Streamable HTTP on http://127.0.0.1:8765/mcp
    plat-underwriting-mcp            # alias for stdio

``engine.mcp_http`` serves HTTP. A non-loopback bind needs ``--allow-remote``
and a bearer token in ``PLAT_UW_TOKEN``; remote mode refuses ``path`` inputs.
That token check is interim, not production auth: hosting adds OAuth at the
single ``engine.mcp_http.authorize`` seam.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Optional

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from engine.engine import run_underwriting
from engine.validator import validate_deal
from engine.version import ENGINE_VERSION, SCHEMA_VERSION
from engine.workflow import guides, service
from engine.workflow.fields import BY_NAME as _FIELD

CONTRACT_VERSION = "plat.underwriting.mcp/1"
INSTRUCTIONS = (
    "Deterministic multifamily underwriting. Call start_here first: it explains the eight-step flow "
    "(create_deal, add_document, describe_deal, ingest_deal, review_assumptions, edit_assumptions, run_deal, "
    "solve_price) and how to try load_sample_deal. Every tool returns a summary and a next_step; errors say "
    "what failed and how to fix it. Never invent a number: ask the analyst for missing inputs."
)
mcp = MCPServer("multifamily-underwriting", instructions=INSTRUCTIONS, version=ENGINE_VERSION)

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)


def _writes(*, destructive: bool, idempotent: bool, open_world: bool = False) -> ToolAnnotations:
    return ToolAnnotations(read_only_hint=False, destructive_hint=destructive, idempotent_hint=idempotent,
                           open_world_hint=open_world)


def _identity() -> dict:
    return {
        "adapter_contract": CONTRACT_VERSION,
        "engine_version": ENGINE_VERSION,
        "schema_version": SCHEMA_VERSION,
    }


# ── workflow tools ────────────────────────────────────────────────────────


@mcp.tool(annotations=READ_ONLY)
def start_here() -> dict:
    """First-run orientation: what this server does, the eight steps, how to try the sample deal,
    and the guide resources. Call this first."""
    return service.start_here()


@mcp.tool(annotations=READ_ONLY)
def list_deals() -> dict:
    """List the deals in the workspace with their step status and next step."""
    return service.list_deals()


@mcp.tool(annotations=READ_ONLY)
def get_deal(deal_id: str) -> dict:
    """A deal's status: which steps are done (and stale), its documents and profile, the last
    ingest, run and price summaries, the files available, and what to do next."""
    return service.get_deal(deal_id)


@mcp.tool(annotations=_writes(destructive=False, idempotent=False))
def load_sample_deal() -> dict:
    """Create a new deal from the bundled synthetic sample (a T12, a rent roll and a filled-in
    profile), ready for ingest_deal. Use it to show an analyst the flow end to end."""
    return service.load_sample_deal()


@mcp.tool(annotations=_writes(destructive=False, idempotent=False))
def create_deal(
    name: Annotated[str, Field(description="Deal name, e.g. the property name the analyst uses.")],
    deal_type: Annotated[str, Field(description="stabilized (prices on year-1 cash-on-cash) or value_add "
                                                "(prices on the first stabilized year).")] = "stabilized",
) -> dict:
    """Step 1: create a deal workspace. Returns the deal_id every other tool takes."""
    return service.create_deal(name, deal_type)


@mcp.tool(annotations=_writes(destructive=True, idempotent=True))
def add_document(
    deal_id: str,
    kind: Annotated[str, Field(description="t12 (trailing-12 operating statement, required), rent_roll "
                                           "(unit-level rent roll, required), om (offering memorandum PDF, "
                                           "optional) or pca (property condition assessment JSON or PDF, optional).")],
    filename: Annotated[str, Field(description="The file's name; its extension must suit the kind "
                                               "(.xlsx/.xls/.csv for t12 and rent_roll).")],
    content_base64: Annotated[Optional[str], Field(description="The file bytes, base64-encoded.")] = None,
    path: Annotated[Optional[str], Field(description="A path on the server's machine instead of content "
                                                     "(local mode only; refused on a remote server).")] = None,
) -> dict:
    """Step 2: store a copy of a deal document in the workspace (one per kind; adding a kind
    again replaces it). Size and extension are checked."""
    return service.add_document(deal_id, kind, filename, content_base64=content_base64, path=path)


def _d(name: str) -> Any:
    return Field(description=_FIELD[name].description)


@mcp.tool(annotations=_writes(destructive=False, idempotent=True))
def describe_deal(
    deal_id: str,
    deal_type: Annotated[Optional[str], _d("deal_type")] = None,
    year_built: Annotated[Optional[int], _d("year_built")] = None,
    asset_class: Annotated[Optional[str], _d("asset_class")] = None,
    star_tier: Annotated[Optional[str], _d("star_tier")] = None,
    deferred_maintenance: Annotated[Optional[bool], _d("deferred_maintenance")] = None,
    msa: Annotated[Optional[str], _d("msa")] = None,
    metro_slug: Annotated[Optional[str], _d("metro_slug")] = None,
    submarket: Annotated[Optional[str], _d("submarket")] = None,
    start: Annotated[Optional[str], _d("start")] = None,
    end: Annotated[Optional[str], _d("end")] = None,
    exit_month: Annotated[Optional[str], _d("exit_month")] = None,
    purchase_price: Annotated[Optional[float], _d("purchase_price")] = None,
    ltv: Annotated[Optional[float], _d("ltv")] = None,
    rate: Annotated[Optional[float], _d("rate")] = None,
    amort: Annotated[Optional[int], _d("amort")] = None,
    io_months: Annotated[Optional[int], _d("io_months")] = None,
    term_months: Annotated[Optional[int], _d("term_months")] = None,
    loan_sizing_noi_basis: Annotated[Optional[str], _d("loan_sizing_noi_basis")] = None,
    millage: Annotated[Optional[float], _d("millage")] = None,
    assessment_ratio: Annotated[Optional[float], _d("assessment_ratio")] = None,
    pca_immediate_capex: Annotated[Optional[float], _d("pca_immediate_capex")] = None,
    pca_recurring_capex_per_unit: Annotated[Optional[float], _d("pca_recurring_capex_per_unit")] = None,
    pca_as_of: Annotated[Optional[str], _d("pca_as_of")] = None,
    pca_reference: Annotated[Optional[str], _d("pca_reference")] = None,
    immediate_capex: Annotated[Optional[float], _d("immediate_capex")] = None,
    immediate_capex_funding: Annotated[Optional[str], _d("immediate_capex_funding")] = None,
    recurring_capex_per_unit: Annotated[Optional[float], _d("recurring_capex_per_unit")] = None,
    exit_cap: Annotated[Optional[float], _d("exit_cap")] = None,
    sale_cost: Annotated[Optional[float], _d("sale_cost")] = None,
    rent_growth: Annotated[Optional[float], _d("rent_growth")] = None,
    vacancy: Annotated[Optional[float], _d("vacancy")] = None,
    collection_loss: Annotated[Optional[float], _d("collection_loss")] = None,
    sponsor_pct: Annotated[Optional[float], _d("sponsor_pct")] = None,
    pref_return: Annotated[Optional[float], _d("pref_return")] = None,
    acq_fee: Annotated[Optional[float], _d("acq_fee")] = None,
    am_fee: Annotated[Optional[float], _d("am_fee")] = None,
    disp_fee: Annotated[Optional[float], _d("disp_fee")] = None,
    analyst: Annotated[Optional[str], _d("analyst")] = None,
    use_market_data: Annotated[Optional[bool], _d("use_market_data")] = None,
) -> dict:
    """Step 3: record the deal profile (partial updates: only the fields given change). start and
    end are needed to ingest; price, debt and property-tax millage are needed to run. Rates are
    decimals (0.065 = 6.5%). Each parameter says why it matters."""
    updates = {name: value for name, value in locals().items() if name != "deal_id"}
    return service.describe_deal(deal_id, **updates)


@mcp.tool(annotations=_writes(destructive=True, idempotent=True, open_world=True))
def ingest_deal(deal_id: str) -> dict:
    """Step 4: parse the documents, run the reconciliation gates and resolve every assumption (with
    market data when configured). Returns PASS/BLOCKED with the fix for each blocker, the capital
    plan, the market data status and how many assumptions need review. Rebuilds the deal's
    canonical; the analyst's assumption edits are kept. With an OM and ANTHROPIC_API_KEY set,
    the OM is read by a hosted model."""
    return service.ingest_deal(deal_id)


@mcp.tool(annotations=READ_ONLY)
def review_assumptions(
    deal_id: str,
    filter: Annotated[str, Field(description="all, needs_review, edited, defaults (house defaults and engine "
                                             "fallbacks) or enforced (house floors the price solve enforced).")] = "all",
    case: Annotated[str, Field(description="deal (the ingested deal) or priced (the solved-price case).")] = "deal",
    include_candidates: Annotated[bool, Field(description="Include every candidate value the resolver "
                                                          "considered (larger response).")] = False,
) -> dict:
    """Step 5: the assumption register: each assumption's key, label, value, default and its source,
    why that is the default, the analyst's edit and flags. The page is the resource
    deal://{deal_id}/assumptions.html."""
    return service.review_assumptions(deal_id, filter=filter, case=case, include_candidates=include_candidates)


@mcp.tool(annotations=_writes(destructive=False, idempotent=True))
def edit_assumptions(
    deal_id: str,
    edits: Annotated[dict[str, Any], Field(description="{assumption key: {'value': v, 'note': 'why'}} or "
                                                       "{key: value}; {key: null} clears an edit back to its default.")],
    editor: Annotated[str, Field(description="Name of the analyst making the edits (recorded as edited_by).")],
) -> dict:
    """Step 6: apply analyst edits to the assumptions (stamped with editor and date) and re-resolve.
    Returns each changed row before and after, with the delta from the default."""
    return service.edit_assumptions(deal_id, edits, editor)


@mcp.tool(annotations=_writes(destructive=False, idempotent=True))
def run_deal(deal_id: str) -> dict:
    """Step 7: validate and run the underwriting. Returns NOI, cash-on-cash, IRRs, equity multiples,
    DSCR on the sizing basis and on NOI, the exit cap and value, the capital plan and the exit-cap
    grid; writes results.json, underwriting.xlsx and assumptions.html under run/."""
    return service.run_deal(deal_id)


@mcp.tool(annotations=_writes(destructive=False, idempotent=True))
def solve_price(
    deal_id: str,
    target_coc: Annotated[Optional[str], Field(description="Target cash-on-cash as a decimal string "
                                                           "(default 0.08, or the sample deal's own target).")] = None,
    benchmark_5yr_treasury: Annotated[Optional[str], Field(description="Today's 5-year US Treasury yield as a "
                                                                       "decimal (0.0405 = 4.05%). Required: ask "
                                                                       "the analyst; never assume it.")] = None,
    benchmark_as_of: Annotated[Optional[str], Field(description="Date of that yield, YYYY-MM-DD (default today).")] = None,
    benchmark_source: Annotated[Optional[str], Field(description="Where the yield came from.")] = None,
    coc_basis: Annotated[Optional[str], Field(description="year_1 or stabilized_year (default by deal type).")] = None,
    exit_cap_rate: Annotated[Optional[str], Field(description="Pin the exit cap (decimal); default: entry-anchored "
                                                              "or the deal's analyst value.")] = None,
    loan_sizing_noi_basis: Annotated[Optional[str], Field(description="Override the loan sizing basis.")] = None,
    min_price: Annotated[str, Field(description="Lowest price searched, dollars.")] = "1000000",
    max_price: Annotated[str, Field(description="Highest price searched, dollars.")] = "100000000",
    max_iterations: Annotated[int, Field(description="Bisection steps, 1 to 60.")] = 40,
) -> dict:
    """Step 8: solve the highest price that meets the target cash-on-cash with the house floors
    enforced. Returns status, price and $/unit, the loan and its sizing basis, caps, IRRs, the
    floors enforced and the exit-cap grid; writes the price outputs including assumptions.html."""
    return service.solve_price(
        deal_id, target_coc=target_coc, benchmark_5yr_treasury=benchmark_5yr_treasury,
        benchmark_as_of=benchmark_as_of, benchmark_source=benchmark_source, coc_basis=coc_basis,
        exit_cap_rate=exit_cap_rate, loan_sizing_noi_basis=loan_sizing_noi_basis,
        min_price=min_price, max_price=max_price, max_iterations=max_iterations)


@mcp.tool(annotations=READ_ONLY)
def export_deal(deal_id: str) -> dict:
    """List the deal's files (workbook, results, assumption register, documents) with local paths
    (local mode) or download links served by this server (HTTP mode)."""
    return service.export_deal(deal_id)


# ── prompts ───────────────────────────────────────────────────────────────


@mcp.prompt(description="Guide the analyst through the eight underwriting steps, asking for missing inputs.")
def new_deal_walkthrough(deal_name: str = "") -> str:
    return guides.walkthrough_prompt(deal_name)


@mcp.prompt(description="Explain one assumption: what it is, its default, where that comes from and why.")
def explain_assumption(key: str, deal_id: str = "") -> str:
    return guides.explain_prompt(key, service.register_row(deal_id, key) if deal_id else None)


# ── resources ─────────────────────────────────────────────────────────────


@mcp.resource("guide://start-here", name="start-here", mime_type="text/markdown",
              description="First-run orientation and the eight steps.")
def start_here_guide() -> str:
    return guides.start_here_markdown()


@mcp.resource("guide://user-guide", name="user-guide", mime_type="text/markdown",
              description="The analyst user guide (docs/USER_GUIDE.md, or a short built-in version).")
def user_guide() -> str:
    return guides.user_guide_markdown()


@mcp.resource("guide://glossary", name="glossary", mime_type="text/markdown",
              description="Every assumption and every source of a default, in plain English.")
def glossary() -> str:
    return guides.glossary_markdown()


@mcp.resource("deal://{deal_id}/assumptions.html", name="assumption-register", mime_type="text/html",
              description="A deal's assumption register page (after ingest_deal).")
def deal_assumptions_page(deal_id: str) -> str:
    return service.register_page(deal_id)


# ── stateless tools (plat.underwriting.mcp/1) ─────────────────────────────


@mcp.tool(annotations=READ_ONLY)
def backsolve_deal_price(inputs: dict, target_coc: str, policy: dict, benchmark: dict,
                        min_price: str = "1000000.00", max_price: str = "100000000.00",
                        max_iterations: int = 40) -> dict:
    """Find a price using an explicit versioned policy and dated benchmark.

    Rates use fractional decimal strings. Return assumptions, tested bounds,
    and honest convergence status without writing artifacts or cashflow arrays.
    """
    from engine.backsolve import backsolve_price

    try:
        result = backsolve_price(inputs, target_coc=target_coc, policy=policy,
                                benchmark=benchmark, min_price=min_price, max_price=max_price,
                                max_iterations=max_iterations)
    except (ValueError, RuntimeError, KeyError, TypeError) as exc:
        return {**_identity(), "status": "refused", "error": {
            "code": getattr(exc, "code", "ENGINE_REFUSAL"), "message": str(exc)}}
    return {**_identity(), **{key: result[key] for key in (
        "contract_version", "status", "summary", "effective_assumptions", "bracket")}}


@mcp.tool(annotations=READ_ONLY)
def validate_deal_inputs(inputs: dict) -> dict:
    """Validate canonical deal inputs without running or persisting a deal."""
    return {**_identity(), **validate_deal(inputs).to_dict()}


@mcp.tool(annotations=READ_ONLY)
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


@mcp.tool(annotations=READ_ONLY)
def assumption_register(inputs: dict, filter: str = "all", include_candidates: bool = False) -> dict:
    """List a resolved deal's assumptions: value, default and its source, why
    that is the default, the user's edits and review or guardrail flags.

    Filter is one of all, edited, review, house, deal, market, guardrail.
    Candidates are left out unless asked for, to keep the response small.
    Reads the canonical only; nothing is run or written.
    """
    from engine.assumptions.register import FILTERS, build_register
    from engine.assumptions.sheet import has_assumptions

    if filter not in FILTERS:
        return {**_identity(), "status": "refused", "error": {
            "code": "BAD_FILTER", "message": f"filter must be one of {list(FILTERS)}"}}
    if not has_assumptions(inputs):
        return {**_identity(), "status": "refused", "error": {
            "code": "NO_ASSUMPTIONS",
            "message": "inputs carry no assumptions section; resolve or ingest the deal first"}}
    register = build_register(inputs, include_candidates=include_candidates, filter=filter)
    return {**_identity(), "status": "success", **register}


def _ltv(inputs: dict) -> Decimal | None:
    try:
        purchase = Decimal(str(inputs["purchase_assumptions"]["purchase_price"]))
        debt = Decimal(str(inputs["debt_terms"]["commitment"]))
    except (KeyError, TypeError, InvalidOperation):
        return None
    if not purchase.is_finite() or not debt.is_finite() or purchase <= 0 or debt < 0:
        return None
    return debt / purchase


@mcp.tool(annotations=READ_ONLY)
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
    """Run the server over stdio (the ``plat-underwriting-mcp`` console script)."""
    service.configure(transport="stdio", remote=False, base_url=None)
    mcp.run()


if __name__ == "__main__":
    main()
