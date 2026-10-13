"""Render the assumption register as one self-contained HTML page (ADR 0003 §8).

``render_register_html(canonical)`` returns ``assumptions.html``: the deal
profile and summary counts, filter chips (All / Edited by user / Needs review
/ House defaults / Deal evidence / Market / Guardrail flags) and one table
grouped by assumption group with the columns Assumption | Value | Default
(source) | Why this default | User edit | Flags. Each row can expand the
candidates the resolver considered.

A backsolve run passes ``pricing`` (``engine.assumptions.register_pricing.
pricing_header`` of its summary): the page then opens with a pricing summary
header (status, target and basis, solved price, loan and its sizing basis,
equity, caps, IRRs, minimum DSCR, the exit-cap sensitivity grid, and the
floors the pricing enforced), highlights each row a floor set and adds an
"Enforced in pricing" chip. Without ``pricing`` (run_deal, ingest) the page is
unchanged.

The page has no external assets: inline CSS (light and dark through
``prefers-color-scheme``, a print stylesheet, a narrow-screen card layout)
and a few lines of vanilla JavaScript for the filter chips. Every piece of
text from the canonical is HTML-escaped. The output is deterministic: the
same canonical renders the same bytes, and no timestamp is added beyond the
dates the data carries.
"""

from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from engine.assumptions.register import FILTERS, build_register, flag_labels, guardrail_detail

# (filter, chip label, tooltip)
CHIPS: tuple[tuple[str, str, str], ...] = (
    ("all", "All", "Every assumption"),
    ("edited", "Edited by user", "Assumptions an analyst changed"),
    ("review", "Needs review", "House defaults, engine fallbacks and other values flagged for review"),
    ("house", "House defaults", "Values from a house rule or an engine fallback"),
    ("deal", "Deal evidence", "Values from the rent roll, T12, OM or comps"),
    ("market", "Market", "Values from a market-data feed"),
    ("guardrail", "Guardrail flags", "Values a house floor warned on, enforced, waived or exempted"),
)
assert tuple(chip for chip, _, _ in CHIPS) == FILTERS
# Shown only on a backsolve page (the rows a house floor set in pricing).
PRICING_CHIP = ("pricing", "Enforced in pricing", "Values a house floor the pricing guardrail enforced")

_FAMILY_LABELS = {
    "analyst": "Analyst",
    "deal": "Deal",
    "market": "Market",
    "house": "House",
    "engine_fallback": "Fallback",
    "unknown": "Other",
}

_CSS = """
:root {
  color-scheme: light dark;
  --bg: #ffffff; --fg: #1a1a1a; --muted: #5f6368; --border: #d9dce1; --panel: #f6f7f9;
  --head: #eef0f3; --accent: #8a6d3b; --edited: #fff6dd; --edited-bar: #c99a2e;
  --chip: #eef0f3; --chip-on: #1a1a1a; --chip-on-fg: #ffffff;
  --b-analyst: #e5effd; --b-analyst-fg: #1d4f91; --b-deal: #e3f4e8; --b-deal-fg: #1e6b3a;
  --b-market: #e8e6fb; --b-market-fg: #4b3c9e; --b-house: #f3ece0; --b-house-fg: #74531c;
  --b-fallback: #eceff1; --b-fallback-fg: #455a64; --b-other: #eceff1; --b-other-fg: #455a64;
  --review: #fdecea; --review-fg: #a1281b; --warn: #fff1d6; --warn-fg: #8a5300;
  --ok: #e3f4e8; --ok-fg: #1e6b3a;
  --enforced: #fdebdc; --enforced-bar: #c2410c; --enforced-fg: #9a3412;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #15171a; --fg: #e8eaed; --muted: #a0a6ad; --border: #343a40; --panel: #1d2024;
    --head: #23272c; --accent: #d9b56c; --edited: #3a3220; --edited-bar: #d9a93a;
    --chip: #262a30; --chip-on: #e8eaed; --chip-on-fg: #15171a;
    --b-analyst: #1c3150; --b-analyst-fg: #a9c8f5; --b-deal: #18352a; --b-deal-fg: #9fd9b3;
    --b-market: #2a2550; --b-market-fg: #c3b9f5; --b-house: #3a2f1c; --b-house-fg: #e6c98f;
    --b-fallback: #2a3036; --b-fallback-fg: #b8c4cc; --b-other: #2a3036; --b-other-fg: #b8c4cc;
    --review: #45201c; --review-fg: #f3aaa0; --warn: #40310f; --warn-fg: #f0c674;
    --ok: #18352a; --ok-fg: #9fd9b3;
    --enforced: #3d2616; --enforced-bar: #f08a4b; --enforced-fg: #f6b98f;
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}
main { max-width: 1400px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 15px; margin: 0; }
.sub { color: var(--muted); margin: 0 0 16px; }
code, .key { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
.profile { display: flex; flex-wrap: wrap; gap: 6px 20px; margin: 0 0 16px; padding: 0; }
.profile div { display: flex; gap: 6px; }
.profile dt { color: var(--muted); }
.profile dd { margin: 0; font-weight: 600; }
.stats { display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 8px; margin: 0 0 16px; }
.stat { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; }
.stat .n { font-size: 20px; font-weight: 700; display: block; }
.stat .l { color: var(--muted); font-size: 12px; }
.capital { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px;
  margin: 0 0 16px; }
.capital h2 { margin: 0 0 6px; }
.capital ol { margin: 0; padding-left: 20px; }
.capital li { margin: 2px 0; }
.capital .binds { font-weight: 600; color: var(--accent); }
.market ul { margin: 0; padding-left: 20px; }
.market li { margin: 2px 0; }
.pricing { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px;
  margin: 0 0 16px; }
.pricing h2 { margin: 0 0 8px; }
.pricing h3 { font-size: 13px; margin: 14px 0 6px; }
.pricing .status { margin: 0 0 8px; }
.pricing .pnote { margin: 0 0 6px; }
.kpis { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 8px; margin: 0; }
.kpi { background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 6px 10px; }
.kpi dt { color: var(--muted); font-size: 12px; }
.kpi dd { margin: 0; }
.kpi dd.v { font-size: 17px; font-weight: 700; font-variant-numeric: tabular-nums; }
.gridwrap { overflow-x: auto; }
.pricing table { min-width: 0; width: auto; table-layout: auto; background: var(--bg);
  border: 1px solid var(--border); }
.pricing th, .pricing td { padding: 4px 10px; font-variant-numeric: tabular-nums; }
.pricing thead th { position: static; }
.pricing tr.base td { font-weight: 700; background: var(--head); }
.pricing ul { margin: 0; padding-left: 20px; }
.pricing li { margin: 3px 0; }
.status-converged { background: var(--ok); color: var(--ok-fg); }
.status-infeasible { background: var(--review); color: var(--review-fg); }
.status-other { background: var(--warn); color: var(--warn-fg); }
.flag-enforced { background: var(--enforced); color: var(--enforced-fg); border: 1px solid var(--enforced-bar); }
tr.row.floor-enforced td { background: var(--enforced); }
tr.row.floor-enforced td:first-child { box-shadow: inset 4px 0 0 var(--enforced-bar); }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 12px; }
.chips[hidden] { display: none; }
.chip {
  border: 1px solid var(--border); background: var(--chip); color: var(--fg); border-radius: 999px;
  padding: 4px 12px; font: inherit; font-size: 13px; cursor: pointer;
}
.chip[aria-pressed="true"] { background: var(--chip-on); color: var(--chip-on-fg); border-color: var(--chip-on); }
.chip .c { opacity: 0.7; margin-left: 4px; }
.how { color: var(--muted); font-size: 13px; margin: 0 0 12px; }
.tablewrap { overflow-x: auto; border: 1px solid var(--border); border-radius: 8px; }
table { border-collapse: collapse; width: 100%; min-width: 1080px; table-layout: fixed; }
col.c-assumption { width: 17%; } col.c-value { width: 14%; } col.c-default { width: 13%; }
col.c-why { width: 30%; } col.c-edit { width: 14%; } col.c-flags { width: 12%; }
th, td { text-align: left; vertical-align: top; padding: 8px 10px; border-bottom: 1px solid var(--border); }
thead th { background: var(--head); font-size: 12px; text-transform: uppercase; letter-spacing: 0.03em;
  color: var(--muted); position: sticky; top: 0; }
tr.group-head th { background: var(--panel); font-size: 13px; text-transform: none; letter-spacing: 0; color: var(--fg); }
tr.group-head .c { color: var(--muted); font-weight: 400; margin-left: 6px; }
tr.row.edited td { background: var(--edited); }
tr.row.edited td:first-child { box-shadow: inset 4px 0 0 var(--edited-bar); }
td.assumption .label { font-weight: 600; display: block; }
td.assumption .key, .meta { color: var(--muted); font-size: 12px; display: block; }
td { overflow-wrap: anywhere; }
td.value .v, td.default .v, .edit .v { font-weight: 600; font-variant-numeric: tabular-nums; }
td.why p { margin: 0 0 6px; }
td.why .basis { color: var(--muted); font-size: 12px; }
.badge { display: inline-block; border-radius: 4px; padding: 1px 6px; font-size: 11px; font-weight: 600;
  margin: 2px 4px 2px 0; }
.src { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 11px; color: var(--muted);
  display: block; }
.f-analyst { background: var(--b-analyst); color: var(--b-analyst-fg); }
.f-deal { background: var(--b-deal); color: var(--b-deal-fg); }
.f-market { background: var(--b-market); color: var(--b-market-fg); }
.f-house { background: var(--b-house); color: var(--b-house-fg); }
.f-engine_fallback { background: var(--b-fallback); color: var(--b-fallback-fg); }
.f-unknown { background: var(--b-other); color: var(--b-other-fg); }
.flag-review { background: var(--review); color: var(--review-fg); }
.flag-warn { background: var(--warn); color: var(--warn-fg); }
.flag-edited { background: var(--edited); color: var(--fg); border: 1px solid var(--edited-bar); }
.flag-ok { background: var(--ok); color: var(--ok-fg); }
.flag-muted { background: var(--b-other); color: var(--b-other-fg); }
.delta { font-size: 12px; }
.note { font-style: italic; }
.none { color: var(--muted); }
details { margin-top: 4px; }
summary { cursor: pointer; color: var(--accent); font-size: 12px; }
details ul { margin: 6px 0 0; padding-left: 18px; font-size: 12px; }
details li { margin-bottom: 4px; }
details li.chosen { font-weight: 600; }
.empty { padding: 16px; color: var(--muted); }
.empty[hidden] { display: none; }
footer { color: var(--muted); font-size: 12px; margin-top: 16px; }
@media (max-width: 760px) {
  table { min-width: 0; }
  thead { display: none; }
  table, tbody, tr, td, th { display: block; width: 100%; }
  tbody[hidden], tr[hidden] { display: none; }
  tr.row { border-bottom: 2px solid var(--border); padding: 4px 0; }
  td { border-bottom: 0; padding: 4px 12px; max-width: none; }
  td[data-col]::before { content: attr(data-col); display: block; font-size: 11px; color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.03em; }
  colgroup { display: none; }
  .pricing table { display: table; width: auto; }
  .pricing thead { display: table-header-group; }
  .pricing tbody { display: table-row-group; }
  .pricing tr { display: table-row; }
  .pricing td, .pricing th { display: table-cell; width: auto; padding: 4px 8px; }
}
@media print {
  :root {
    --bg: #ffffff; --fg: #000000; --muted: #444444; --border: #bbbbbb; --panel: #f2f2f2; --head: #e6e6e6;
    --edited: #fff3cc; --enforced: #fde3cf;
  }
  body { font-size: 10px; }
  main { max-width: none; padding: 0; }
  .chips, .how { display: none; }
  .tablewrap { overflow: visible; border: 0; }
  table { min-width: 0; }
  thead { display: table-header-group; }
  thead th { position: static; }
  tr { break-inside: avoid; }
  tbody[hidden], tr[hidden] { display: none; }
  * { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
}
"""

_JS = """
(function () {
  var chips = document.querySelectorAll('[data-chip]');
  var bar = document.getElementById('chips');
  var empty = document.getElementById('empty');
  if (!bar || !chips.length) { return; }
  bar.hidden = false;
  function apply(name) {
    var shown = 0;
    document.querySelectorAll('tr.row').forEach(function (tr) {
      var tags = ' ' + (tr.getAttribute('data-filters') || '') + ' ';
      var on = tags.indexOf(' ' + name + ' ') !== -1;
      tr.hidden = !on;
      if (on) { shown += 1; }
    });
    document.querySelectorAll('tbody.group').forEach(function (body) {
      var visible = body.querySelectorAll('tr.row:not([hidden])').length;
      var count = body.querySelector('tr.group-head .c');
      if (count) { count.textContent = String(visible); }
      body.hidden = visible === 0;
    });
    chips.forEach(function (chip) {
      chip.setAttribute('aria-pressed', chip.getAttribute('data-chip') === name ? 'true' : 'false');
    });
    if (empty) { empty.hidden = shown !== 0; }
  }
  chips.forEach(function (chip) {
    chip.addEventListener('click', function () {
      var name = chip.getAttribute('data-chip');
      apply(name);
      if (window.history && history.replaceState) { history.replaceState(null, '', '#filter=' + name); }
    });
  });
  var match = /filter=([a-z_]+)/.exec(window.location.hash || '');
  if (match && document.querySelector('[data-chip="' + match[1] + '"]')) { apply(match[1]); }
  var opened = [];
  window.addEventListener('beforeprint', function () {
    opened = [];
    document.querySelectorAll('details:not([open])').forEach(function (d) { d.open = true; opened.push(d); });
  });
  window.addEventListener('afterprint', function () {
    opened.forEach(function (d) { d.open = false; });
  });
})();
"""


def _e(value: Any) -> str:
    """Escape any value as HTML text (``None`` → empty)."""
    if value is None:
        return ""
    return escape(str(value), quote=True)


def _family_badge(family: Any, source: Any) -> str:
    family = family if family in _FAMILY_LABELS else "unknown"
    return (
        f'<span class="badge f-{_e(family)}">{_e(_FAMILY_LABELS[family])}</span>'
        f'<span class="src">{_e(source)}</span>'
    )


def _profile_items(register: Mapping[str, Any]) -> list[tuple[str, str]]:
    profile = register.get("deal_profile") or {}
    items: list[tuple[str, str]] = []
    if register.get("deal_id"):
        items.append(("Deal", str(register["deal_id"])))

    def add(label: str, value: Any) -> None:
        if value is None or value == "" or value == {}:
            return
        if isinstance(value, Mapping):
            value = value.get("name") or value.get("metro_slug") or ", ".join(
                f"{k}: {v}" for k, v in value.items()
            )
        items.append((label, str(value)))

    add("Deal type", (profile.get("deal_type") or "").replace("_", "-") or None)
    add("MSA", profile.get("msa"))
    add("Submarket", profile.get("submarket"))
    add("Year built", profile.get("year_built"))
    add("Asset class", profile.get("asset_class"))
    add("Star tier", profile.get("star_tier"))
    known = {"deal_type", "msa", "submarket", "year_built", "asset_class", "star_tier"}
    for name in sorted(k for k in profile if k not in known):
        add(name.replace("_", " ").capitalize(), profile[name])
    add("Mode", register.get("mode"))
    add("Resolver", register.get("resolver_version"))
    return items


_STATS: tuple[tuple[str, str], ...] = (
    ("total", "Assumptions"),
    ("from_deal_evidence", "From deal evidence"),
    ("from_market", "From market data"),
    ("house_defaults", "House defaults"),
    ("engine_fallbacks", "Engine fallbacks"),
    ("edited_by_user", "Edited by user"),
    ("needing_review", "Need review"),
    ("guardrail_warnings", "Guardrail warnings"),
    ("guardrail_enforced", "Guardrail enforced"),
)
# Added on a backsolve page only.
_PRICING_STATS: tuple[tuple[str, str], ...] = (("pinned_by_pricing", "Pinned by pricing"),)


def _chip_counts(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts = {name: 0 for name in FILTERS}
    for row in rows:
        for name in row.get("filters") or ():
            if name in counts:
                counts[name] += 1
    return counts


def _value_cell(row: Mapping[str, Any]) -> str:
    parts = [f'<span class="v">{_e(row["value_display"])}</span>']
    if row.get("unit_label"):
        parts.append(f'<span class="meta">{_e(row["unit_label"])}</span>')
    meta = []
    if row.get("confidence"):
        meta.append(f"confidence {row['confidence']}")
    if row.get("as_of"):
        meta.append(f"as of {row['as_of']}")
    if meta:
        parts.append(f'<span class="meta">{_e(" · ".join(meta))}</span>')
    return "".join(parts)


def _default_cell(row: Mapping[str, Any]) -> str:
    default = row["default"]
    parts = [
        f'<span class="v">{_e(default["display"])}</span><br/>',
        _family_badge(default.get("source_family"), default.get("source")),
    ]
    if default.get("as_of") and default.get("as_of") != row.get("as_of"):
        parts.append(f'<span class="meta">as of {_e(default["as_of"])}</span>')
    return "".join(parts)


def _candidates_html(row: Mapping[str, Any]) -> str:
    candidates = row.get("candidates")
    if not candidates:
        return ""
    items = []
    for candidate in candidates:
        cls = ' class="chosen"' if candidate.get("is_default_source") else ""
        bits = [
            f'<span class="badge f-{_e(candidate.get("source_family") or "unknown")}">'
            f'{_e(_FAMILY_LABELS.get(candidate.get("source_family"), "Other"))}</span>',
            f'<code>{_e(candidate.get("source"))}</code>: <strong>{_e(candidate.get("display"))}</strong>',
        ]
        if candidate.get("basis"):
            bits.append(f" — {_e(candidate['basis'])}")
        if candidate.get("as_of"):
            bits.append(f" (as of {_e(candidate['as_of'])})")
        items.append(f"<li{cls}>{''.join(bits)}</li>")
    return (
        f"<details><summary>Candidates considered ({len(candidates)})</summary>"
        f"<ul>{''.join(items)}</ul></details>"
    )


def _why_cell(row: Mapping[str, Any]) -> str:
    why = row["why"]
    parts = [f"<p>{_e(why['rationale'])}</p>"]
    default_basis = why.get("default_basis")
    if default_basis:
        parts.append(f'<p class="basis"><strong>How the default was set:</strong> {_e(default_basis)}</p>')
    basis = why.get("basis")
    if basis and basis != default_basis:
        parts.append(f'<p class="basis"><strong>Current value:</strong> {_e(basis)}</p>')
    parts.append(_candidates_html(row))
    return "".join(parts)


def _edit_cell(row: Mapping[str, Any]) -> str:
    edit = row.get("user_edit")
    pinned = row.get("pinned")
    if edit is None and pinned is None:
        return '<span class="none">—</span>'
    record = edit if edit is not None else pinned
    parts = ['<div class="edit">', f'<span class="v">{_e(record["display"])}</span> ']
    if record.get("differs_from_default"):
        delta = record.get("delta_display")
        text = f"{delta} vs default" if delta else "differs from default"
        parts.append(f'<span class="badge flag-warn delta">{_e(text)}</span>')
    else:
        parts.append('<span class="badge flag-muted delta">same as default</span>')
    if edit is None:
        parts.append(f'<span class="meta">Pinned by pricing ({_e(pinned.get("source"))})</span>')
    if record.get("note"):
        parts.append(f'<span class="meta note">“{_e(record["note"])}”</span>')
    who = []
    if edit is not None and edit.get("edited_by"):
        who.append(f"by {edit['edited_by']}")
    if edit is not None and edit.get("edited_at"):
        who.append(f"on {edit['edited_at']}")
    if who:
        parts.append(f'<span class="meta">{_e(" ".join(who))}</span>')
    if edit is not None and edit.get("cohort_overrides"):
        cohorts = ", ".join(f"{k}: {v}" for k, v in sorted(edit["cohort_overrides"].items()))
        parts.append(f'<span class="meta">cohorts: {_e(cohorts)}</span>')
    parts.append("</div>")
    return "".join(parts)


_FLAG_CLASSES = {
    "edited": "flag-edited",
    "review": "flag-review",
    "guardrail_warned": "flag-warn",
    "guardrail_enforced": "flag-warn",
    "guardrail_flagged": "flag-warn",
    "guardrail_waived": "flag-ok",
    "guardrail_exempt": "flag-ok",
}


def _flags_cell(row: Mapping[str, Any], *, enforced: bool = False) -> str:
    out = '<span class="badge flag-enforced">Enforced in pricing</span>' if enforced else ""
    out += "".join(
        f'<span class="badge {_FLAG_CLASSES.get(flag, "flag-muted")}">{_e(label)}</span>'
        for flag, label in zip(row.get("flags") or (), flag_labels(row))
    )
    detail = guardrail_detail(row)
    if detail:
        out += f'<span class="meta">{_e(detail)}</span>'
    return out or '<span class="none">—</span>'


_COLUMNS = ("Assumption", "Value", "Default (source)", "Why this default", "User edit", "Flags")
_CELL_CLASSES = ("assumption", "value", "default", "why", "edit", "flags")


def _row_html(row: Mapping[str, Any], *, enforced: bool = False) -> str:
    """One register row; ``enforced`` marks a row a house floor set in pricing."""
    classes = ["row"]
    if row.get("user_edit") is not None:
        classes.append("edited")
    if row.get("review_required"):
        classes.append("review")
    filters = list(row.get("filters") or ["all"])
    if enforced:
        classes.append("floor-enforced")
        filters.append(PRICING_CHIP[0])
    assumption = (
        f'<span class="label">{_e(row["label"])}</span>'
        f'<span class="key">{_e(row["key"])}</span>'
    )
    cells = (
        ("assumption", assumption),
        ("value", _value_cell(row)),
        ("default", _default_cell(row)),
        ("why", _why_cell(row)),
        ("edit", _edit_cell(row)),
        ("flags", _flags_cell(row, enforced=enforced)),
    )
    tds = "".join(
        f'<td class="{cls}" data-col="{_e(column)}">{html}</td>'
        for (cls, html), column in zip(cells, _COLUMNS)
    )
    return (
        f'<tr class="{" ".join(classes)}" id="a-{_e(row["key"])}" '
        f'data-filters="{_e(" ".join(filters))}">{tds}</tr>'
    )


def render_html(register: Mapping[str, Any]) -> str:
    """Render a register (``build_register`` output) as a complete HTML document."""
    rows = list(register.get("rows") or [])
    by_key = {row["key"]: row for row in rows}
    summary = register.get("summary") or {}
    title = "Assumption register"
    if register.get("deal_id"):
        title = f"{title}: {register['deal_id']}"
    pricing = register.get("pricing") if isinstance(register.get("pricing"), Mapping) else None
    enforced = frozenset(str(k) for k in (pricing or {}).get("enforced_keys") or ())
    if pricing is None:
        sub = ("Every underwriting assumption for this deal: the value the model uses, the default the "
               "resolver chose and why, and any edits made by the analyst.")
    else:
        sub = ("The backsolve pricing summary, then every assumption as priced: the value the model uses, "
               "the default the resolver chose and why, and what the pricing changed. Rows a house floor "
               "set in pricing are highlighted.")

    out: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8"/>',
        '<meta name="viewport" content="width=device-width, initial-scale=1"/>',
        '<meta name="color-scheme" content="light dark"/>',
        f"<title>{_e(title)}</title>",
        f"<style>{_CSS}</style>",
        "</head>",
        "<body>",
        "<main>",
        "<header>",
        f"<h1>{_e(title)}</h1>",
        f'<p class="sub">{_e(sub)}</p>',
    ]
    items = _profile_items(register)
    if items:
        out.append('<dl class="profile">')
        out.extend(f"<div><dt>{_e(label)}</dt><dd>{_e(value)}</dd></div>" for label, value in items)
        out.append("</dl>")
    if pricing is not None:
        out.extend(_pricing_html(pricing, present={row["key"] for row in rows}))
    out.append('<div class="stats">')
    stats = _STATS + (_PRICING_STATS if pricing is not None else ())
    for name, label in stats:
        out.append(
            f'<div class="stat"><span class="n">{_e(summary.get(name, 0))}</span>'
            f'<span class="l">{_e(label)}</span></div>'
        )
    out.append("</div>")
    out.extend(_capital_plan_html(register.get("capital_plan")))
    out.extend(_market_data_html(register.get("market_data")))
    out.append("</header>")

    counts = _chip_counts(rows)
    active = register.get("filter") or "all"
    out.append('<div class="chips" id="chips" role="toolbar" aria-label="Filter assumptions" hidden="hidden">')
    chips = CHIPS
    if pricing is not None:
        chips = CHIPS + (PRICING_CHIP,)
        counts[PRICING_CHIP[0]] = sum(1 for row in rows if row["key"] in enforced)
    for name, label, tip in chips:
        pressed = "true" if name == active else "false"
        out.append(
            f'<button type="button" class="chip" data-chip="{_e(name)}" aria-pressed="{pressed}" '
            f'title="{_e(tip)}">{_e(label)}<span class="c">{counts[name]}</span></button>'
        )
    out.append("</div>")
    out.append(
        '<p class="how">To change an assumption, edit its Override and Note in the "Assumptions" tab of the '
        "workbook (or the assumptions.json sheet) and apply it with <code>runs/assumptions_sheet.py apply</code> "
        "or <code>--assumption-overrides</code>. Clear an override to return to the default.</p>"
    )

    out.append('<div class="tablewrap"><table>')
    out.append("<colgroup>" + "".join(f'<col class="c-{cls}"/>' for cls in _CELL_CLASSES) + "</colgroup>")
    out.append("<thead><tr>" + "".join(f'<th scope="col">{_e(c)}</th>' for c in _COLUMNS) + "</tr></thead>")
    for group in register.get("groups") or []:
        keys = [key for key in group.get("keys") or [] if key in by_key]
        if not keys:
            continue
        out.append(f'<tbody class="group" data-group="{_e(group["group"])}">')
        out.append(
            f'<tr class="group-head"><th colspan="{len(_COLUMNS)}" scope="rowgroup">{_e(group["group"])}'
            f'<span class="c">{len(keys)}</span></th></tr>'
        )
        out.extend(_row_html(by_key[key], enforced=key in enforced) for key in keys)
        out.append("</tbody>")
    out.append("</table>")
    hidden = ' hidden="hidden"' if rows else ""
    out.append(f'<p class="empty" id="empty"{hidden}>No assumptions match this filter.</p>')
    out.append("</div>")
    out.append(
        f"<footer>{_e(register.get('register_version'))} · generated from the canonical "
        "<code>assumptions</code> section; the engine reads the canonical fields, not this page.</footer>"
    )
    out.append("</main>")
    out.append(f"<script>{_JS}</script>")
    out.append("</body>")
    out.append("</html>")
    return "\n".join(out) + "\n"


def _market_data_html(status: Any) -> list[str]:
    """Which market-data provider supplied what, and every market slot that fell back."""
    if not isinstance(status, Mapping):
        return []
    out = [
        '<section class="capital market" aria-label="Market data">',
        "<h2>Market data</h2>",
        f'<p class="meta">{_e(status.get("summary"))}</p>',
        "<ul>",
    ]
    for report in status.get("providers") or []:
        state = "available" if report.get("available") else "not available"
        if report.get("matched") is not None:
            state += ", geography matched" if report.get("matched") else ", geography not matched"
        why = report.get("unavailable_reason") or report.get("summary")
        root = f' <span class="src">{_e(report["root"])} ({_e(report.get("root_origin"))})</span>' if report.get("root") else ""
        supplied = ", ".join(report.get("metrics_supplied") or []) or "nothing for this geography"
        out.append(
            f"<li><strong>{_e(report.get('name'))}</strong>: {_e(state)}; supplies {_e(supplied)}{root}"
            + (f'<span class="meta">{_e(why)}</span>' if why else "")
            + "</li>"
        )
    for key, slot in (status.get("slots") or {}).items():
        if isinstance(slot, Mapping) and not slot.get("supplied") and not slot.get("kept_from_prior_resolution"):
            out.append(
                f'<li><a href="#a-{_e(key)}">{_e(key)}</a>: fell back to '
                f"{_e(slot.get('entry_source') or 'deal evidence or house defaults')}"
                f'<span class="meta">{_e(slot.get("note"))}</span></li>'
            )
    out.append("</ul>")
    out.append("</section>")
    return out


def _capital_plan_html(plan: Any) -> list[str]:
    """The three capex buckets side by side, with the one that binds in year 1."""
    if not isinstance(plan, Mapping):
        return []
    out = ['<section class="capital" aria-label="Capital plan">', "<h2>Capital plan: three capex buckets</h2>", "<ol>"]
    for bucket in plan.get("buckets") or []:
        binds = bucket.get("key") == plan.get("binding_year_1")
        flags = []
        if binds:
            flags.append('<span class="binds">binds in year 1</span>')
        if bucket.get("review_required"):
            flags.append('<span class="badge flag-review">Needs review</span>')
        source = f' <span class="src">{_e(bucket["source"])}</span>' if bucket.get("source") else ""
        out.append(
            f'<li><a href="#a-{_e(bucket.get("key"))}">{_e(bucket.get("label"))}</a>: '
            f'<strong>{_e(bucket.get("value_display"))}</strong> {" ".join(flags)}'
            f'<span class="meta">{_e(bucket.get("note"))}</span>{source}</li>'
        )
    out.append("</ol>")
    out.append(f'<p class="meta">{_e(plan.get("rule"))}</p>')
    out.append("</section>")
    return out


def _status_class(status: Any) -> str:
    if status == "converged":
        return "status-converged"
    if status == "infeasible":
        return "status-infeasible"
    return "status-other"


def _key_link(key: Any, label: Any, present: set[str]) -> str:
    if str(key) in present:
        return f'<a href="#a-{_e(key)}">{_e(label)}</a>'
    return f"<strong>{_e(label)}</strong>"


def _change(original: Any, applied: Any) -> str:
    if original is None and applied is None:
        return ""
    return f" {_e(original if original is not None else '—')} → <strong>{_e(applied if applied is not None else '—')}</strong>"


def _grid_html(grid: Mapping[str, Any]) -> list[str]:
    points = [p for p in grid.get("points") or [] if isinstance(p, Mapping)]
    if not points:
        return []
    columns = ("Offset", "Exit cap", "Exit value", "Levered IRR", "Unlevered IRR", "Levered EM")
    fields = ("offset_display", "exit_cap_display", "exit_value_display", "levered_irr_display",
              "unlevered_irr_display", "levered_em_display")
    out = [
        "<h3>Exit-cap sensitivity at the solved price</h3>",
        '<div class="gridwrap"><table class="grid">',
        "<thead><tr>" + "".join(f'<th scope="col">{_e(c)}</th>' for c in columns) + "</tr></thead><tbody>",
    ]
    for point in points:
        cls = "grid-row base" if point.get("is_base") else "grid-row"
        out.append(f'<tr class="{cls}">' + "".join(f"<td>{_e(point.get(f))}</td>" for f in fields) + "</tr>")
    out.append("</tbody></table></div>")
    if grid.get("rule"):
        out.append(f'<p class="meta">{_e(grid["rule"])}</p>')
    return out


def _floors_html(pricing: Mapping[str, Any], present: set[str]) -> list[str]:
    floors = [f for f in pricing.get("floors_enforced") or [] if isinstance(f, Mapping)]
    top_up = pricing.get("r_and_m_top_up") if isinstance(pricing.get("r_and_m_top_up"), Mapping) else None
    out = [f"<h3>Floors enforced in pricing ({len(floors)})</h3>"]
    findings = pricing.get("floors_enforced_findings")
    if isinstance(findings, int) and findings != len(floors):
        out.append(f'<p class="meta">{_e(findings)} findings across segments and cohorts '
                   '(the count the backsolve CLI prints).</p>')
    if not floors and top_up is None:
        out.append('<p class="none">None: every assumption cleared its house floor.</p>')
        return out
    out.append('<ul class="floors">')
    for floor in floors:
        meta = []
        if floor.get("floor_display") is not None:
            meta.append(f"floor {floor['floor_display']}")
        if floor.get("floor_source"):
            meta.append(str(floor["floor_source"]))
        if isinstance(floor.get("count"), int) and floor["count"] > 1:
            meta.append(f"{floor['count']} segments")
        if floor.get("evidence"):
            meta.append(f"over {floor['evidence']}")
        out.append(
            f"<li>{_key_link(floor.get('key'), floor.get('label'), present)} "
            f'<code>{_e(floor.get("key"))}</code> '
            f'<span class="badge flag-enforced">{_e(floor.get("action_label"))}</span>'
            f"{_change(floor.get('original_display'), floor.get('applied_display'))}"
            + (f' <span class="meta">{_e(" · ".join(meta))}</span>' if meta else "")
            + "</li>"
        )
    if top_up is not None:
        floor = f" to reach the house floor {top_up['floor_per_unit_display']}/unit" if top_up.get(
            "floor_per_unit_display") else ""
        link = _key_link(top_up.get("key"), "R&M top-up", present) if top_up.get("key") else "<strong>R&amp;M top-up</strong>"
        out.append(
            f"<li>{link}: <strong>{_e(top_up.get('annual_display'))}</strong> a year added as "
            f"“{_e(top_up.get('category_name'))}”{_e(floor)}</li>"
        )
    out.append("</ul>")
    return out


def _adjustments_html(pricing: Mapping[str, Any]) -> list[str]:
    items = [a for a in pricing.get("other_adjustments") or [] if isinstance(a, Mapping)]
    if not items:
        return []
    out = [f"<h3>Other pricing adjustments ({len(items)})</h3>", '<ul class="adjustments">']
    for item in items:
        out.append(
            f'<li><code>{_e(item.get("input"))}</code> '
            f'<span class="badge flag-muted">{_e(item.get("action_label"))}</span>'
            f"{_change(item.get('original_display'), item.get('applied_display'))}"
            + (f' <span class="meta">{_e(item["detail"])}</span>' if item.get("detail") else "")
            + "</li>"
        )
    out.append("</ul>")
    return out


def _pricing_html(pricing: Mapping[str, Any], *, present: set[str]) -> list[str]:
    """The pricing summary header of a backsolve page (``register_pricing.pricing_header``)."""
    out = [
        '<section class="pricing" id="pricing" aria-label="Backsolve pricing">',
        "<h2>Backsolve pricing</h2>",
        f'<p class="status">Status: <span class="badge {_status_class(pricing.get("status"))}">'
        f'{_e(pricing.get("status_label"))}</span>'
        + (f' <code>{_e(pricing.get("status"))}</code>' if pricing.get("status") is not None else "")
        + "</p>",
    ]
    out.extend(f'<p class="pnote">{_e(note)}</p>' for note in pricing.get("notes") or [])
    metrics = [m for m in pricing.get("metrics") or [] if isinstance(m, Mapping)]
    if metrics:
        out.append('<dl class="kpis">')
        for metric in metrics:
            detail = f'<dd class="meta">{_e(metric["detail"])}</dd>' if metric.get("detail") else ""
            out.append(
                f'<div class="kpi" data-metric="{_e(metric.get("key"))}"><dt>{_e(metric.get("label"))}</dt>'
                f'<dd class="v">{_e(metric.get("display"))}</dd>{detail}</div>'
            )
        out.append("</dl>")
    if isinstance(pricing.get("exit_cap_grid"), Mapping):
        out.extend(_grid_html(pricing["exit_cap_grid"]))
    out.extend(_floors_html(pricing, present))
    out.extend(_adjustments_html(pricing))
    out.append("</section>")
    return out


def render_register_html(canonical: Mapping[str, Any], *, pricing: Optional[Mapping[str, Any]] = None) -> str:
    """``assumptions.html`` for a canonical (every assumption, candidates included).

    ``pricing`` (``register_pricing.pricing_header``) adds the backsolve pricing header.
    """
    return render_html(build_register(canonical, pricing=pricing))


def register_html_path_for(output_path: Path | str) -> Path:
    """``deal.json`` → ``deal.assumptions.html`` in the same folder."""
    output_path = Path(output_path)
    return output_path.with_name(f"{output_path.stem}.assumptions.html")


def write_register_html(
    canonical: Mapping[str, Any], path: Path | str, *, pricing: Optional[Mapping[str, Any]] = None,
) -> Path:
    """Write ``assumptions.html`` for the canonical (with ``pricing``, the backsolve header) and return its path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_register_html(canonical, pricing=pricing), encoding="utf-8")
    return path
