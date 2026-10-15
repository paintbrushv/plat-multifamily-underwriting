"""Build the analyst screens: four MCP App resources and the browser app page.

The sources are plain files in ``engine/ui/static/`` (vanilla JavaScript and CSS, no build tool).
Each page is assembled here into one self-contained HTML document: the CSS and JavaScript are
inlined, the static data the screens need (the deal-profile fields and why each matters, the
glossary terms, the steps) is embedded as a JSON data block, and nothing is loaded from a network.
The color tokens are read from the assumption register page
(``engine.assumptions.register_html``) so the screens and the register look like one product.

MCP Apps (the ``io.modelcontextprotocol/ui`` extension, spec 2026-01-26): each screen is a
``ui://plat-underwriting/<screen>`` resource served as ``text/html;profile=mcp-app``, and the tools
that open it carry ``_meta.ui.resourceUri`` (plus the older flat ``_meta["ui/resourceUri"]`` key
some hosts still read). The page itself carries no deal data: it receives the tool's result from
the host over postMessage.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from engine.ui.terms import TERMS
from engine.version import ENGINE_VERSION
from engine.workflow import fields as profile_fields
from engine.workflow.config import DEFAULT_EXTENSIONS, DEFAULT_MAX_UPLOAD_MB

STATIC_DIR = Path(__file__).resolve().parent / "static"
APP_MIME_TYPE = "text/html;profile=mcp-app"
URI_PREFIX = "ui://plat-underwriting/"
PRODUCT = "Multifamily underwriting"


@dataclass(frozen=True)
class Screen:
    name: str
    title: str
    description: str
    tools: tuple[str, ...]
    scripts: tuple[str, ...]

    @property
    def uri(self) -> str:
        return f"{URI_PREFIX}{self.name}"


_SHARED = ("core.js", "bridge.js", "shell.js")
SCREENS: dict[str, Screen] = {
    "intake": Screen(
        "intake", "Deal intake",
        "Guided deal intake: the deal type, the documents (drag and drop), the deal facts with why each matters, "
        "progress through the steps, and Check documents.",
        ("create_deal", "describe_deal", "get_deal", "load_sample_deal"), ("screen_intake.js",)),
    "assumptions": Screen(
        "assumptions", "Check and assumptions",
        "The ingest gates (PASS or BLOCKED with the fix), market data status and capital plan, then the "
        "assumption register as an editable table with notes and deltas.",
        ("ingest_deal", "review_assumptions", "edit_assumptions"), ("screen_assumptions.js",)),
    "results": Screen(
        "results", "Results",
        "Headline returns, the capital plan by year, the annual cash flow, the exit on forward NOI and the "
        "exit-cap sensitivity, with charts.",
        ("run_deal",), ("charts.js", "grid.js", "screen_results.js")),
    "pricing": Screen(
        "pricing", "Pricing",
        "Solve the price for a target cash-on-cash with an analyst-supplied Treasury benchmark: status, price, "
        "loan, caps, IRRs, the floors enforced and the asking price against the solved price.",
        ("solve_price",), ("charts.js", "grid.js", "screen_pricing.js")),
}
TOOL_SCREENS: dict[str, str] = {tool: screen.name for screen in SCREENS.values() for tool in screen.tools}

# Short labels for the describe_deal fields (the tool schema carries the long "what" and "why").
FIELD_LABELS: dict[str, str] = {
    "deal_type": "Deal type", "year_built": "Year built", "asset_class": "Asset class", "star_tier": "Star tier",
    "deferred_maintenance": "Known deferred maintenance", "msa": "MSA", "metro_slug": "Metro key",
    "submarket": "Submarket", "start": "Analysis start", "end": "Analysis end", "exit_month": "Sale month",
    "purchase_price": "Purchase price", "ltv": "Loan-to-value", "rate": "Interest rate", "amort": "Amortization (years)",
    "io_months": "Interest-only months", "term_months": "Loan term (months)",
    "loan_sizing_noi_basis": "Loan sized on", "millage": "Millage (mills)", "assessment_ratio": "Assessment ratio",
    "pca_immediate_capex": "PCA immediate needs", "pca_recurring_capex_per_unit": "PCA recurring capex per unit",
    "pca_as_of": "PCA date", "pca_reference": "PCA report", "immediate_capex": "Immediate capex at close",
    "immediate_capex_funding": "Immediate capex funded by", "recurring_capex_per_unit": "Recurring capex per unit",
    "exit_cap": "Exit cap rate", "sale_cost": "Sale costs", "rent_growth": "Rent growth", "vacancy": "Physical vacancy",
    "collection_loss": "Collection loss", "sponsor_pct": "Sponsor share of equity", "pref_return": "Preferred return",
    "acq_fee": "Acquisition fee", "am_fee": "Asset management fee", "disp_fee": "Disposition fee",
    "analyst": "Analyst", "use_market_data": "Use market data",
}
# Field labels that get a glossary tooltip (engine/ui/terms.py).
LABEL_TERMS: dict[str, str] = {
    "deal_type": "Stabilized", "ltv": "LTV", "io_months": "IO", "amort": "Amortization", "millage": "Millage",
    "assessment_ratio": "Assessment ratio", "pca_immediate_capex": "PCA", "immediate_capex": "Immediate capex",
    "recurring_capex_per_unit": "Recurring capex", "exit_cap": "Exit cap", "loan_sizing_noi_basis": "NCF",
}
# The intake form's groups, in order; "more" groups start collapsed.
FIELD_GROUPS: tuple[dict[str, Any], ...] = (
    {"label": "Property", "fields": ["deal_type", "year_built", "asset_class", "star_tier", "deferred_maintenance"]},
    {"label": "Location", "lead": "Where market data is looked up. A fictional or unconfigured market simply falls back "
                                  "to house defaults, flagged for review.",
     "fields": ["msa", "metro_slug", "submarket"]},
    {"label": "Price & financing", "fields": ["purchase_price", "ltv", "rate", "amort", "io_months", "term_months",
                                              "loan_sizing_noi_basis"]},
    {"label": "Taxes", "lead": "The engine re-taxes the property at the purchase price: price x assessment ratio x "
                               "millage / 1,000.", "fields": ["millage", "assessment_ratio"]},
    {"label": "Capex & condition", "lead": "Three buckets: immediate capex at close, recurring capex per unit a "
                                           "year and the replacement reserve. A PCA outranks your own figures.",
     "fields": ["immediate_capex", "immediate_capex_funding", "recurring_capex_per_unit", "pca_immediate_capex",
                "pca_recurring_capex_per_unit", "pca_as_of", "pca_reference"]},
    {"label": "Hold", "lead": "The analysis window (a five-year hold starting the month after closing is typical) "
                              "and the exit.", "fields": ["start", "end", "exit_month", "exit_cap", "sale_cost"]},
    {"label": "Operating overrides", "more": True,
     "lead": "Leave blank to use the documents, market data and house defaults.",
     "fields": ["rent_growth", "vacancy", "collection_loss"]},
    {"label": "Fund terms and workflow", "more": True,
     "fields": ["sponsor_pct", "pref_return", "acq_fee", "am_fee", "disp_fee", "analyst", "use_market_data"]},
)
START = {
    "what": ("Give the tool a T12, a rent roll and a few deal facts. It checks the documents tie to their own totals, "
             "fills every assumption with a value and a stated source, and runs a monthly cash flow: NOI, cash-on-cash, "
             "IRRs, DSCR and an exit. It can also solve the price that hits your target cash-on-cash. Every number comes "
             "from a deterministic engine; nothing is made up."),
    "steps": [
        {"label": "Documents", "text": "Add the T12 and the rent roll (the OM and a PCA are optional)."},
        {"label": "Describe", "text": "Enter the facts the documents do not carry: the analysis period, price, "
                                      "financing and property-tax millage."},
        {"label": "Check", "text": "Read the documents, tie them to their own totals and resolve every assumption."},
        {"label": "Assumptions", "text": "Review the flagged assumptions; edit the ones you disagree with, with a note."},
        {"label": "Results", "text": "Run the monthly cash flow: NOI, cash-on-cash, IRRs, DSCR and the exit."},
        {"label": "Price", "text": "Solve the highest price that meets your target cash-on-cash."},
        {"label": "Export", "text": "Take the workbook, the assumption register and the results with you."},
    ],
    "sample": ("New here? Try the sample deal: a fictional 128-unit garden community with its documents and facts "
               "filled in. Every name and number in it is invented."),
}


# ── theme ─────────────────────────────────────────────────────────────────


def theme_css() -> str:
    """The register page's color tokens, for light, dark (OS preference) and a host-chosen theme."""
    from engine.assumptions import register_html

    css = register_html._CSS
    light = re.search(r":root \{\n(.*?)\n\}", css, re.S)
    dark = re.search(r"@media \(prefers-color-scheme: dark\) \{\n  :root \{\n(.*?)\n  \}\n\}", css, re.S)
    if light is None or dark is None:  # pragma: no cover - guarded by a test
        raise RuntimeError("the register page's color tokens moved; update engine.ui.screens.theme_css")
    light_body = "\n".join(line for line in light.group(1).splitlines() if "color-scheme" not in line)
    dark_body = dark.group(1)
    return (f":root {{\n  color-scheme: light;\n{light_body}\n}}\n"
            f"@media (prefers-color-scheme: dark) {{\n  :root:not([data-theme=\"light\"]) {{\n    color-scheme: dark;\n"
            f"{dark_body}\n  }}\n}}\n"
            f":root[data-theme=\"dark\"] {{\n  color-scheme: dark;\n{dark_body}\n}}\n")


# ── page data ─────────────────────────────────────────────────────────────


def _form_text(field: Any) -> str:
    """A field's "what" as the form shows it: rates are typed as percents there, not decimals."""
    if field.kind != "rate":
        return field.what
    text = re.sub(r"\s*\((?:e\.g\. )?0\.\d+(?: = [\d.]+%)?\)", "", field.what)
    return text.replace("as a decimal", "as a percent")


def field_specs() -> list[dict[str, Any]]:
    """Every describe_deal field with its type, limits, what it is and why it matters."""
    out = []
    for f in profile_fields.FIELDS:
        spec: dict[str, Any] = {"name": f.name, "type": f.kind, "group": f.group, "what": _form_text(f), "why": f.why}
        if f.choices:
            spec["choices"] = list(f.choices)
        if f.minimum is not None:
            spec["minimum"] = f.minimum
        if f.maximum is not None:
            spec["maximum"] = f.maximum
        out.append(spec)
    return out


def page_config(screen: Optional[str], *, mode: str, workspace: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """The static data a page embeds. ``workspace`` (browser mode) gives the live upload limits."""
    workspace = workspace or {}
    return {
        "mode": mode,
        "screen": screen,
        "version": ENGINE_VERSION,
        "fields": field_specs(),
        "labels": FIELD_LABELS,
        "label_terms": LABEL_TERMS,
        "groups": [dict(g) for g in FIELD_GROUPS],
        "required_to_ingest": list(profile_fields.REQUIRED_FOR_INGEST),
        "required_to_run": list(profile_fields.REQUIRED_TO_RUN),
        "defaults": {"use_market_data": True},
        "extensions": {k: list(v) for k, v in (workspace.get("allowed_extensions") or DEFAULT_EXTENSIONS).items()},
        "max_upload_mb": workspace.get("max_upload_mb") or DEFAULT_MAX_UPLOAD_MB,
        "terms": TERMS,
        "start": START,
    }


def _json_block(data: Any) -> str:
    """JSON safe to place inside a <script type="application/json"> element."""
    text = json.dumps(data, ensure_ascii=False, sort_keys=True)
    return text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


@functools.lru_cache(maxsize=None)
def _static(name: str) -> str:
    text = (STATIC_DIR / name).read_text(encoding="utf-8")
    if "</script" in text.lower() or "</style" in text.lower():  # pragma: no cover - guarded by a test
        raise RuntimeError(f"{name} must not contain a closing script or style tag")
    return text


def _scripts(names: tuple[str, ...]) -> str:
    seen: list[str] = []
    for name in names:
        if name not in seen:
            seen.append(name)
    return "\n".join(f"/* {name} */\n{_static(name)}" for name in seen)


def _css() -> str:
    return theme_css() + _static("app.css")


_DOCUMENT = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<link rel="icon" href="data:,">
<title>{title}</title>
<style>
{css}
</style>
</head>
<body>
{body}
<script type="application/json" id="puw-config">{config}</script>
<script>
{script}
</script>
</body>
</html>
"""


@functools.lru_cache(maxsize=None)
def screen_html(name: str) -> str:
    """One screen as a self-contained MCP App document (no deal data; the host sends the result)."""
    screen = SCREENS[name]
    body = f'<main class="puw" id="app" aria-label="{screen.title}"></main>'
    return _DOCUMENT.format(
        title=f"{screen.title} · {PRODUCT}", css=_css(), body=body,
        config=_json_block(page_config(name, mode="mcp")),
        script=_scripts(_SHARED + screen.scripts + ("main_mcp.js",)))


_APP_BODY = """<div class="app">
<aside class="side" aria-label="Deals and guides">
<div class="brand">{product}<small>Deterministic engine · runs on this computer</small></div>
<div class="stack">
<button type="button" class="btn primary" id="btn-sample">Try the sample deal</button>
<button type="button" class="btn" id="btn-new">New deal</button>
</div>
<nav aria-labelledby="deals-h"><h2 id="deals-h" class="small muted">Deals</h2><ul id="deal-list"></ul></nav>
<nav aria-labelledby="guides-h"><h2 id="guides-h" class="small muted">Guides</h2><ul>
<li><a href="#/guide/user-guide">New? Start here (user guide)</a></li>
<li><a href="#/guide/glossary">Assumption glossary</a></li>
<li><a href="#/">Home</a></li>
</ul></nav>
</aside>
<main id="main" tabindex="-1">
<div class="puw" id="app"></div>
<div class="puw" id="guide" hidden></div>
</main>
</div>"""


def app_html(workspace: Optional[Mapping[str, Any]] = None) -> str:
    """The browser app: the four screens in one page, with the deal list and the guides."""
    scripts: tuple[str, ...] = _SHARED
    for screen in SCREENS.values():
        scripts += screen.scripts
    return _DOCUMENT.format(
        title=PRODUCT, css=_css(), body=_APP_BODY.format(product=PRODUCT),
        config=_json_block(page_config(None, mode="browser", workspace=workspace)),
        script=_scripts(scripts + ("main_browser.js",)))


def script_hashes(html: str) -> list[str]:
    """CSP ``'sha256-…'`` sources for the page's inline executable scripts."""
    hashes = []
    for attrs, body in re.findall(r"<script([^>]*)>(.*?)</script>", html, re.S):
        if "application/json" in attrs:
            continue
        digest = base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode("ascii")
        hashes.append(f"'sha256-{digest}'")
    return hashes


# ── MCP Apps wiring ───────────────────────────────────────────────────────


def tool_meta(tool: str) -> Optional[dict[str, Any]]:
    """The ``_meta`` linking a tool to its screen, or None for a tool without one."""
    name = TOOL_SCREENS.get(tool)
    if name is None:
        return None
    uri = SCREENS[name].uri
    return {"ui": {"resourceUri": uri}, "ui/resourceUri": uri}


def apps_extension() -> Any:
    """The MCP Apps extension with the four screens registered (``MCPServer(extensions=[...])``)."""
    from mcp.server.apps import Apps

    apps = Apps()
    for screen in SCREENS.values():
        apps.add_html_resource(screen.uri, screen_html(screen.name), name=f"screen-{screen.name}",
                               title=f"{screen.title} screen", description=screen.description,
                               prefers_border=True)
    return apps
