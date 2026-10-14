"""Orientation text the MCP server serves: start here, the user guide, the glossary, prompts.

``docs/USER_GUIDE.md`` and ``docs/ASSUMPTION_GLOSSARY.md`` are served when
this checkout has them; otherwise a short built-in guide and a glossary built
on the fly from the assumption registry and
``engine/assumptions/rationale.py`` stand in (an installed wheel ships no
``docs/``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from engine.workflow.fields import FIELDS

DOCS_DIR = Path(__file__).resolve().parents[2] / "docs"
USER_GUIDE_NAME = "USER_GUIDE.md"
GLOSSARY_NAME = "ASSUMPTION_GLOSSARY.md"
TOOLS_DOC = "docs/MCP_TOOLS.md"

RESOURCES = {
    "guide://start-here": "First-run orientation: what this does and the eight steps.",
    "guide://user-guide": "The analyst user guide.",
    "guide://glossary": "Every assumption and every source of a default, in plain English.",
}

STEPS: tuple[dict[str, str], ...] = (
    {"step": "1", "tool": "create_deal", "do": "Name the deal and pick stabilized or value_add.",
     "ask": "What is the deal called, and is it a stabilized or a value-add deal?"},
    {"step": "2", "tool": "add_document", "do": "Add the T12 and the rent roll (OM and PCA optional).",
     "ask": "Please share the trailing-12 operating statement and the current rent roll (Excel or CSV)."},
    {"step": "3", "tool": "describe_deal",
     "do": "Record the analysis period, price and debt, property-tax millage, year built, market and capex.",
     "ask": "When does the analysis start and end? What are the price, loan terms and tax millage?"},
    {"step": "4", "tool": "ingest_deal",
     "do": "Parse the documents, check they reconcile and resolve every assumption.",
     "ask": "Any blocker comes with the fix; resolve blockers before running."},
    {"step": "5", "tool": "review_assumptions",
     "do": "Walk the analyst through the assumptions flagged for review and the house defaults.",
     "ask": "Do these defaults match your view of the deal?"},
    {"step": "6", "tool": "edit_assumptions",
     "do": "Apply the analyst's changes, with a note and their name.", "ask": "What would you change, and why?"},
    {"step": "7", "tool": "run_deal",
     "do": "Validate and run the underwriting; returns NOI, cash-on-cash, IRRs, DSCR and the exit.",
     "ask": "Here are the headline returns."},
    {"step": "8", "tool": "solve_price",
     "do": "Solve the highest price that meets a target cash-on-cash, with the house floors enforced.",
     "ask": "What target cash-on-cash, and what is today's 5-year Treasury yield (and its date)?"},
)

WHAT_IT_DOES = (
    "This server underwrites multifamily deals with a deterministic engine: the same inputs always give "
    "the same numbers, and every assumption shows its value, its default, where the default came from and "
    "why. You (the assistant) collect the documents and the analyst's inputs, call the tools in order, and "
    "explain the results in plain language. Never invent a number the analyst has not given you: when a "
    "tool asks for an input, ask the analyst."
)


def start_here_markdown() -> str:
    lines = ["# Start here", "", WHAT_IT_DOES, "", "## The eight steps", ""]
    for step in STEPS:
        lines.append(f"{step['step']}. `{step['tool']}`: {step['do']}")
    lines += [
        "",
        "`export_deal` lists every file the deal has produced (workbook, results, assumption register) "
        "at any point. `get_deal` shows which steps are done and what to do next.",
        "",
        "## Try the sample deal",
        "",
        "Call `load_sample_deal` to create a deal from a bundled synthetic sample (a T12, a rent roll and a "
        "filled-in profile), then `ingest_deal`, `review_assumptions`, `run_deal` and `solve_price` on it.",
        "",
        "## Guides",
        "",
        *[f"- `{uri}`: {text}" for uri, text in RESOURCES.items()],
        f"- The tool reference is `{TOOLS_DOC}` in the repository.",
    ]
    return "\n".join(lines) + "\n"


def _read_doc(name: str) -> str | None:
    path = DOCS_DIR / name
    try:
        return path.read_text(encoding="utf-8") if path.is_file() else None
    except OSError:
        return None


def user_guide_markdown() -> str:
    text = _read_doc(USER_GUIDE_NAME)
    if text is not None:
        return text
    return (
        "# User guide (short version)\n\n"
        "The full guide is `docs/USER_GUIDE.md` in the repository. In brief:\n\n"
        + start_here_markdown().split("\n", 2)[2]
        + "\n## Deal profile fields\n\n"
        + "\n".join(f"- `{f.name}` ({f.group}): {f.description}" for f in FIELDS)
        + "\n\nCall `start_here` from any assistant for the same orientation.\n"
    )


def glossary_markdown() -> str:
    text = _read_doc(GLOSSARY_NAME)
    if text is not None:
        return text
    from engine.assumptions.rationale import PREFIX_RATIONALES, SOURCE_RATIONALES
    from engine.assumptions.register import group_for, label_for
    from engine.assumptions.resolver import ASSUMPTION_KEYS

    lines = ["# Assumption glossary", "",
             "Built from the assumption registry and the policy rationales "
             "(`engine/assumptions/rationale.py`).", "", "## Assumptions", ""]
    for key in ASSUMPTION_KEYS:
        lines.append(f"- `{key}`: {label_for(key)} ({group_for(key)})")
    lines += ["", "## Where defaults come from", "",
              "Precedence: analyst edit > deal evidence > submarket > MSA > house default > engine fallback.", ""]
    for source, text in SOURCE_RATIONALES.items():
        lines.append(f"- `{source}`: {text}")
    for prefix, template in PREFIX_RATIONALES:
        lines.append(f"- `{prefix}<name>`: {template.replace('{suffix}', '<name>')}")
    return "\n".join(lines) + "\n"


def walkthrough_prompt(deal_name: str = "") -> str:
    target = f" for the deal '{deal_name}'" if deal_name else ""
    lines = [
        f"Help me underwrite a multifamily deal{target} with the underwriting tools.",
        "",
        "Call `start_here` first. Then follow the eight steps one at a time. Before each tool call, ask me "
        "in plain language for anything the step needs that I have not told you; never guess a number.",
        "",
    ]
    for step in STEPS:
        lines.append(f"{step['step']}. `{step['tool']}`: {step['do']} Ask: \"{step['ask']}\"")
    lines += [
        "",
        "If I just want to see how it works, use `load_sample_deal` instead of steps 1 to 3.",
        "After each tool, tell me its `summary` in your own words and suggest its `next_step`. When a tool "
        "returns an error, explain what failed and its `fix`, and ask me for what is missing. Use "
        "`describe_deal` for any profile field (see its parameter descriptions for why each matters).",
    ]
    return "\n".join(lines)


def explain_prompt(key: str, row: dict[str, Any] | None = None) -> str:
    from engine.assumptions.rationale import rationale_for
    from engine.assumptions.register import group_for, label_for

    lines = [f"Explain the underwriting assumption `{key}` ({label_for(key)}, {group_for(key)}) to an analyst "
             "in plain language: what it is, how it moves the returns, and whether the value below looks "
             "reasonable. Do not invent figures."]
    if row is not None:
        default = row.get("default") or {}
        why = row.get("why") or {}
        lines += [
            "",
            f"Current value: {row.get('value_display')} (source {row.get('source')}).",
            f"Default: {default.get('display')} from {default.get('source')}.",
            f"Basis: {why.get('basis') or why.get('default_basis') or 'n/a'}",
            f"Why that default: {why.get('rationale')}",
        ]
        if row.get("user_edit"):
            edit = row["user_edit"]
            lines.append(f"Analyst edit: {edit.get('display')} by {edit.get('edited_by') or 'unknown'} "
                         f"({edit.get('note') or 'no note'}).")
        if row.get("flags"):
            lines.append(f"Flags: {', '.join(row['flags'])}.")
    else:
        lines += ["", "Policy behind the usual default sources:",
                  f"- deal evidence: {rationale_for('deal:t12')['text']}",
                  f"- house default: {rationale_for('house:vintage_1980s')['text']}",
                  "Read guide://glossary for every source."]
    return "\n".join(lines)
