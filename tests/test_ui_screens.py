"""The analyst screens as MCP Apps (engine/ui, docs/MCP_TOOLS.md "Screens").

Each screen is a ``ui://plat-underwriting/<screen>`` resource served as
``text/html;profile=mcp-app`` (MCP Apps, io.modelcontextprotocol/ui, 2026-01-26), the tools that
open it carry ``_meta.ui.resourceUri``, and the page is one self-contained document: no external
URL, no deal data (the host sends the tool result over postMessage). Also the guide renderer the
browser app uses (escape everything) and the shared JavaScript helpers under Node when it exists.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import anyio
import pytest

from engine import mcp_server, ui
from engine.ui import markdown, screens
from engine.workflow import config as wf_config
from engine.workflow import fields as profile_fields
from engine.workflow import service

STATIC = Path(screens.STATIC_DIR)
EXPECTED_LINKS = {
    "create_deal": "intake", "describe_deal": "intake", "get_deal": "intake", "load_sample_deal": "intake",
    "ingest_deal": "assumptions", "review_assumptions": "assumptions", "edit_assumptions": "assumptions",
    "run_deal": "results", "solve_price": "pricing",
}
EXTERNAL = re.compile(r"""(?:src|href|action)\s*=\s*["']?\s*(?:https?:)?//|url\(\s*["']?(?:https?:)?//|@import""", re.I)


def _surface():
    async def go():
        from mcp import Client

        async with Client(mcp_server.mcp) as client:
            tools = (await client.list_tools()).tools
            resources = (await client.list_resources()).resources
            pages = {}
            for name in screens.SCREENS:
                read = await client.read_resource(f"ui://plat-underwriting/{name}")
                pages[name] = read.model_dump(by_alias=True, exclude_none=True)["contents"][0]
            return tools, resources, pages

    return anyio.run(go)


# ── the MCP Apps surface ──────────────────────────────────────────────────


def test_each_screen_is_a_listed_ui_resource_with_the_mcp_app_mime_type():
    _, resources, pages = _surface()
    listed = {str(r.uri): r for r in resources}
    for name in ("intake", "assumptions", "results", "pricing"):
        uri = f"ui://plat-underwriting/{name}"
        assert uri in listed, uri
        assert listed[uri].mime_type == "text/html;profile=mcp-app"
        assert pages[name]["mimeType"] == "text/html;profile=mcp-app"
        assert pages[name]["text"].startswith("<!doctype html>")
        assert pages[name]["_meta"]["ui"]["prefersBorder"] is True


def test_tools_link_to_their_screen_through_meta_ui_resource_uri():
    tools, _, _ = _surface()
    by_name = {tool.name: tool.model_dump(by_alias=True, exclude_none=True) for tool in tools}
    for tool, screen in EXPECTED_LINKS.items():
        meta = by_name[tool]["_meta"]
        uri = f"ui://plat-underwriting/{screen}"
        assert meta["ui"]["resourceUri"] == uri, tool
        assert meta["ui/resourceUri"] == uri, tool  # the deprecated flat key older hosts read
    for tool in ("start_here", "list_deals", "add_document", "export_deal", "backsolve_deal_price"):
        assert "ui" not in (by_name[tool].get("_meta") or {}), tool
    assert ui.TOOL_SCREENS == EXPECTED_LINKS


def test_the_server_advertises_the_mcp_apps_extension():
    capabilities = mcp_server.mcp._lowlevel_server.extensions
    assert "io.modelcontextprotocol/ui" in capabilities


def test_tool_text_output_is_unchanged_for_hosts_without_mcp_apps(tmp_path, monkeypatch):
    monkeypatch.setenv(wf_config.HOME_ENV, str(tmp_path / "ws"))

    async def go():
        from mcp import Client

        async with Client(mcp_server.mcp) as client:
            result = await client.call_tool("list_deals", {})
            return result

    result = anyio.run(go)
    assert json.loads(result.content[0].text)["ok"] is True
    assert result.structured_content is None


# ── self-contained pages ──────────────────────────────────────────────────


@pytest.mark.parametrize("name", list(screens.SCREENS))
def test_screen_html_is_self_contained(name):
    html = screens.screen_html(name)
    assert not EXTERNAL.search(html), EXTERNAL.search(html)
    assert "<link rel=\"stylesheet\"" not in html and "<script src" not in html
    assert html.count("<script") == 2  # the JSON data block and one inline script
    config = json.loads(re.search(r'<script type="application/json" id="puw-config">(.*?)</script>', html, re.S).group(1))
    assert config["mode"] == "mcp" and config["screen"] == name
    assert "PUW.screens." + name in html


def test_the_browser_app_page_is_self_contained_and_carries_every_screen():
    html = screens.app_html()
    assert not EXTERNAL.search(html)
    for name in screens.SCREENS:
        assert "PUW.screens." + name in html
    assert "main_browser.js" in html and "main_mcp.js" not in html
    assert len(screens.script_hashes(html)) == 1


def test_screens_carry_no_deal_data_or_server_paths(tmp_path, monkeypatch):
    home = tmp_path / "private-workspace"
    monkeypatch.setenv(wf_config.HOME_ENV, str(home))
    monkeypatch.setenv("PLAT_UNDERWRITING_CONFIG", str(tmp_path / "none.toml"))
    deal = service.create_deal("Synthetic Screen Check", "stabilized")
    assert deal["ok"]
    screens.screen_html.cache_clear()
    try:
        for name in screens.SCREENS:
            html = screens.screen_html(name)
            assert str(tmp_path) not in html and str(home) not in html
            assert deal["deal_id"] not in html and "Synthetic Screen Check" not in html
        app = screens.app_html(wf_config.load_config().as_dict())
        assert str(tmp_path) not in app and deal["deal_id"] not in app
    finally:
        screens.screen_html.cache_clear()


def test_embedded_json_cannot_close_its_script_element():
    block = screens._json_block({"x": "</script><script>alert(1)</script>", "y": "a & b"})
    assert "</" not in block and "<" not in block and "&" not in block
    assert json.loads(block)["x"].startswith("</script>")


def test_the_static_sources_ship_as_package_data():
    tomllib = pytest.importorskip("tomllib")
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    patterns = tomllib.loads(pyproject.read_text(encoding="utf-8"))["tool"]["setuptools"]["package-data"]["engine.ui"]
    for path in STATIC.iterdir():
        assert any(Path("static", path.name).match(pattern) for pattern in patterns), path.name


def test_static_sources_never_close_a_script_or_style_tag():
    for path in STATIC.iterdir():
        text = path.read_text(encoding="utf-8").lower()
        assert "</script" not in text and "</style" not in text, path.name


def test_the_screens_reuse_the_register_page_color_tokens():
    from engine.assumptions import register_html

    css = screens.theme_css()
    for token in ("--bg", "--fg", "--muted", "--border", "--panel", "--b-house", "--review", "--enforced-bar"):
        assert css.count(token + ":") == 3, token  # light, dark (OS preference), dark (host theme)
    light = re.search(r"--bg: (#[0-9a-f]+)", register_html._CSS).group(1)
    assert f"--bg: {light}" in css
    assert ':root[data-theme="dark"]' in css and ':root:not([data-theme="light"])' in css


# ── the intake form's field data ──────────────────────────────────────────


def test_every_profile_field_has_a_label_and_exactly_one_form_group():
    names = [f.name for f in profile_fields.FIELDS]
    assert set(screens.FIELD_LABELS) == set(names)
    grouped = [name for group in screens.FIELD_GROUPS for name in group["fields"]]
    assert sorted(grouped) == sorted(names)
    assert set(screens.LABEL_TERMS.values()) <= set(ui.screens.TERMS)


def test_field_specs_carry_why_it_matters_and_limits():
    specs = {s["name"]: s for s in screens.field_specs()}
    assert specs["purchase_price"]["why"] == profile_fields.BY_NAME["purchase_price"].why
    assert specs["ltv"]["maximum"] == 0.95 and specs["ltv"]["type"] == "rate"
    assert "decimal" not in specs["ltv"]["what"]  # the form takes percents
    assert specs["deal_type"]["choices"] == list(profile_fields.BY_NAME["deal_type"].choices)
    config = screens.page_config("intake", mode="mcp")
    assert config["required_to_ingest"] == ["start", "end"]
    assert config["required_to_run"] == ["purchase_price", "ltv", "rate", "amort", "millage"]


# ── the guide renderer (browser app) ──────────────────────────────────────


def test_markdown_escapes_everything_and_keeps_only_safe_links():
    source = "\n".join([
        "# Title <b>x</b>",
        "<!-- a comment -->",
        "<script>alert(1)</script>",
        "Text with `code <i>` and **bold** and [js](javascript:alert(1)) and [ext](https://example.org/a)",
        "and [guide](USER_GUIDE.md#pricing) and [anchor](#title) and [file](../README.md).",
        "",
        "| a | b |",
        "|---|---|",
        "| <img src=x onerror=1> | 2 |",
        "",
        "- one",
        "  - nested",
        "1. first",
        "",
        "```",
        "<tag> & stuff",
        "```",
    ])
    html = markdown.render_markdown(source, link_map=markdown.guide_link_map({"USER_GUIDE.md": "#/guide/user-guide"}))
    assert "<script" not in html and "<img" not in html and "<b>" not in html and "<i>" not in html
    assert "&lt;script&gt;" in html and "&lt;img src=x onerror=1&gt;" in html
    assert "javascript:" not in html
    assert '<a href="https://example.org/a" target="_blank" rel="noopener noreferrer">ext</a>' in html
    assert '<a href="#/guide/user-guide/pricing">guide</a>' in html
    assert '<a href="#title">anchor</a>' in html
    assert "README" not in html and ">file</a>" not in html  # an unknown relative link becomes text
    assert '<h1 id="title-bxb">' in html  # tags escaped in the text, dropped from the anchor
    assert "<strong>bold</strong>" in html and "<code>code &lt;i&gt;</code>" in html
    assert "<ul><li>one<ul><li>nested</li></ul></li></ul>" in html
    assert "<ol><li>first</li></ol>" in html
    assert "<pre><code>&lt;tag&gt; &amp; stuff</code></pre>" in html
    assert "a comment" not in html


def test_the_user_guide_renders():
    from engine.workflow import guides

    html = markdown.render_markdown(guides.user_guide_markdown())
    assert '<h2 id="your-first-10-minutes-the-sample-deal">' in html
    assert "<table>" in html


# ── shared JavaScript helpers (Node, when installed) ──────────────────────

NODE = shutil.which("node")
CORE_CHECKS = r"""
const core = require(process.argv[2]);
const assert = require('assert');
assert.strictEqual(core.usd(966944.13), '$966,944');
assert.strictEqual(core.usd(-2806577), '-$2,806,577');
assert.strictEqual(core.usd(12.5, {cents: true}), '$12.50');
assert.strictEqual(core.usd(null), '—');
assert.strictEqual(core.pct(0.0588), '5.88%');
assert.strictEqual(core.pct(-0.0326), '-3.26%');
assert.strictEqual(core.times(1.386), '1.39x');
assert.strictEqual(core.bps(25), '+25 bps');
assert.strictEqual(core.bps(0), 'base');
assert.strictEqual(core.usdShort(1500000), '$1.5M');
assert.strictEqual(core.month('2031-10'), 'Oct 2031');
const rate = {name: 'rate', type: 'rate', minimum: 0, maximum: 0.25};
assert.deepStrictEqual(core.parseField(rate, '5.75'), {ok: true, value: 0.0575});
assert.deepStrictEqual(core.parseField(rate, '6.5%'), {ok: true, value: 0.065});
assert.strictEqual(core.parseField(rate, '30').ok, false);
assert.strictEqual(core.fieldText(rate, 0.0575), '5.75');
const price = {name: 'purchase_price', type: 'float', minimum: 1};
assert.deepStrictEqual(core.parseField(price, '$14,280,000'), {ok: true, value: 14280000});
assert.strictEqual(core.parseField({name: 'amort', type: 'int'}, '30.5').ok, false);
assert.strictEqual(core.parseField({name: 'start', type: 'month'}, '2026-13').ok, false);
assert.deepStrictEqual(core.parseField({name: 'x', type: 'choice', choices: ['A', 'B']}, ''), {ok: true, value: null});
const checked = core.checkProfile([{name: 'start', type: 'month'}, {name: 'end', type: 'month'}], {start: '2026-11', end: '2026-10'});
assert.ok(checked.errors.end);
assert.deepStrictEqual(core.profileChanges({ltv: 0.65}, {ltv: 0.65, rate: 0.06, msa: null}), {rate: 0.06});
const growth = {key: 'rent_growth', unit: 'decimal_yoy_by_analysis_year', value: [0.025, 0.025, 0.03]};
assert.strictEqual(core.editMode(growth), 'list');
assert.strictEqual(core.editText(growth), '2.5, 2.5, 3');
assert.deepStrictEqual(core.parseEdit(growth, '3, 3, 3'), {ok: true, value: [0.03, 0.03, 0.03]});
assert.strictEqual(core.parseEdit(growth, '3, 3').ok, false);
const rm = {key: 'r_and_m_per_unit', unit: 'usd_per_unit_year', value: 750};
assert.deepStrictEqual(core.parseEdit(rm, '900'), {ok: true, value: 900});
assert.strictEqual(core.editMode({unit: 'decimal_of_units', value: {start: 0.05}}), 'json');
assert.strictEqual(core.editMode({unit: 'enum', value: 'forward'}), 'text');
const deal = {documents: {t12: {}, rent_roll: {}}, missing_for_ingest: [], last_ingest: {gate_status: 'PASS'},
  steps: [{step: 'describe', done: true}, {step: 'ingest', done: true}, {step: 'run', done: true, stale: true}]};
const model = core.stepStates(deal);
assert.strictEqual(model.states.check, 'done');
assert.strictEqual(model.states.results, 'stale');
assert.strictEqual(model.next, 'results');
assert.strictEqual(core.nextText(deal), 'Run the deal again: the assumptions changed.');
assert.deepStrictEqual(core.niceTicks(0, 1047594, 4), [0, 500000, 1000000, 1500000]);
const env = {content: [{type: 'text', text: '{"ok": true, "summary": "s"}'}]};
assert.deepStrictEqual(core.toolEnvelope(env), {ok: true, summary: 's'});
assert.strictEqual(core.toolEnvelope({content: [{type: 'text', text: 'boom'}], isError: true}).error.code, 'TOOL_ERROR');
assert.strictEqual(core.inferTool({ok: true, pricing: {}}), 'solve_price');
assert.strictEqual(core.inferTool({ok: true, gate: {}}), 'ingest_deal');
assert.strictEqual(core.escapeHtml('<a href="x">&'), '&lt;a href=&quot;x&quot;&gt;&amp;');
console.log('ok');
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_core_js_helpers_under_node(tmp_path):
    script = tmp_path / "check.js"
    script.write_text(CORE_CHECKS, encoding="utf-8")
    done = subprocess.run([NODE, str(script), str(STATIC / "core.js")], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ok"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_every_static_script_parses_under_node():
    for path in sorted(STATIC.glob("*.js")):
        done = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, f"{path.name}: {done.stderr}"
