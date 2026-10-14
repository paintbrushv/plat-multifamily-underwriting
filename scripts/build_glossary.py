#!/usr/bin/env python3
"""Generate docs/ASSUMPTION_GLOSSARY.md from the code.

Every fact in the glossary is read from the engine, never typed by hand:

- the assumption keys, units, market metrics and editing rules from the
  resolver (``engine/assumptions/resolver.py``);
- the labels and groups from the assumption register
  (``engine/assumptions/register.py``), in register order;
- the house floors from ``engine/assumptions/guardrails.py``;
- the "why this default" text for every source from
  ``engine/assumptions/rationale.py``;
- the house values (vintage bands, named constants, opex growth) from
  ``engine/underwriting_policy.py``.

Usage:
    python scripts/build_glossary.py            # rewrite docs/ASSUMPTION_GLOSSARY.md
    python scripts/build_glossary.py --check    # exit 1 if the committed file is stale

``tests/test_assumption_glossary.py`` fails when the committed glossary is
stale: regenerate it with this script and commit the result.
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

GLOSSARY_PATH = REPO_ROOT / "docs" / "ASSUMPTION_GLOSSARY.md"

FAMILY_TITLES = {
    "analyst": "Analyst",
    "deal": "Deal evidence",
    "market": "Market data",
    "house": "House default",
    "engine_fallback": "Engine fallback",
}
FAMILY_SUMMARIES = {
    "analyst": "A value you entered. It always wins.",
    "deal": "Read from this deal's own documents (T12, rent roll, OM, PCA, comps).",
    "market": "From a configured market-data feed for the deal's submarket or MSA.",
    "house": "A house rule or default. Flagged for review so you confirm it.",
    "engine_fallback": "Nothing else supplied a value; the engine's built-in default (often zero). Confirm it.",
}

# Vintage framework bands shown in the house table: (column title, row keys).
VINTAGE_BANDS: tuple[tuple[str, tuple[str, str]], ...] = (
    ("R&M band ($/unit/yr)", ("r_and_m_low", "r_and_m_high")),
    ("Recurring capex band ($/unit/yr)", ("economic_capex_low", "economic_capex_high")),
    ("Turn cost band ($/turn)", ("turn_low", "turn_high")),
)
# Profiles the per-vintage floor table is computed for: (label, strategy, deferred maintenance).
PROFILES: tuple[tuple[str, str, bool], ...] = (
    ("Stabilized", "cashflow", False),
    ("Value-add", "value_add", False),
    ("Stabilized, deferred maintenance", "cashflow", True),
)


def _cell(text: Any) -> str:
    """Text safe inside a Markdown table cell."""
    return " ".join(str(text).split()).replace("|", "\\|")


def _pct(value: Any) -> str:
    number = Decimal(str(value)) * 100
    text = f"{number:.3f}".rstrip("0").rstrip(".")
    return f"{text}%"


def _usd(value: Any) -> str:
    return f"${Decimal(str(value)):,.0f}"


def _fmt(value: Any, kind: str) -> str:
    return _pct(value) if kind == "pct" else _usd(value)


def _source_pattern(family: str) -> str:
    return f"`{family}`" if family in ("analyst", "engine_fallback") else f"`{family}:<name>`"


def _table(header: Iterable[str], rows: Iterable[Iterable[Any]]) -> list[str]:
    header = list(header)
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return out


# ── sections ─────────────────────────────────────────────────────────────


def _sources_section() -> list[str]:
    from engine.assumptions.rationale import (
        FAMILIES,
        FAMILY_UNKNOWN,
        PREFIX_RATIONALES,
        SOURCE_RATIONALES,
        source_family,
    )

    lines = [
        "## Where a value comes from",
        "",
        "Every assumption shows a **source**. When more than one source has a value, the higher one in this "
        "list wins:",
        "",
    ]
    families = [f for f in FAMILIES if f != FAMILY_UNKNOWN]
    for i, family in enumerate(families, 1):
        lines.append(f"{i}. **{FAMILY_TITLES[family]}** ({_source_pattern(family)}). {FAMILY_SUMMARIES[family]}")
    lines += [
        "",
        "Market data ranks submarket above MSA. Some keys use their own order (for example, rent growth "
        "never uses deal evidence, and the exit cap prefers market data to the OM); the register's "
        "*Candidates considered* shows every value that was weighed.",
        "",
        "### Why each source is a default",
        "",
        "This is the text the register shows under *Why this default*.",
        "",
    ]
    by_family: dict[str, list[tuple[str, str]]] = {f: [] for f in families}
    for source, text in SOURCE_RATIONALES.items():
        by_family.setdefault(source_family(source), []).append((f"`{source}`", text))
    for prefix, template in PREFIX_RATIONALES:
        family = source_family(prefix + "x")
        by_family.setdefault(family, []).append(
            (f"`{prefix}<name>`", template.replace("{suffix}", "<name>"))
        )
    for family in families:
        entries = sorted(by_family.get(family) or [], key=lambda item: item[0])
        if not entries:
            continue
        lines += [f"#### {FAMILY_TITLES[family]}", ""]
        lines += _table(("Source", "Why"), entries)
        lines.append("")
    return lines


def _spec_notes(spec: Any, guardrail_keys: frozenset[str], categorical: dict[str, Iterable[str]]) -> str:
    notes: list[str] = []
    if spec.market_metrics:
        notes.append("Market data can fill it (" + ", ".join(f"`{m}`" for m in spec.market_metrics) + ").")
    if spec.key in guardrail_keys:
        notes.append("Has a house floor: warned when underwriting, raised to the floor when pricing.")
    if spec.cohort:
        notes.append("Can be edited per floor plan (`cohort_overrides`).")
    if spec.value_add_only:
        notes.append("Value-add deals only.")
    if spec.price_linked:
        notes.append("Moves with the purchase price.")
    if spec.key in categorical:
        notes.append("Choices: " + ", ".join(f"`{c}`" for c in categorical[spec.key]) + ".")
    if not spec.patch_paths:
        notes.append("Shown for review; only an analyst value changes the model.")
    return " ".join(notes)


def _canonical_fields(spec: Any) -> str:
    return ", ".join(f"`{'.'.join(path)}`" for path in spec.patch_paths) or "—"


def _assumptions_section() -> list[str]:
    from engine.assumptions.guardrails import GUARDRAIL_KEYS
    from engine.assumptions.register import GROUPS, group_for, label_for, unit_label
    from engine.assumptions.resolver import CATEGORICAL_KEYS, _specs

    guardrail_keys = frozenset(GUARDRAIL_KEYS)
    specs = list(_specs())
    lines = [
        "## Assumptions by group",
        "",
        "Grouped and labeled as in the assumption register. **Key** is the name to use in an overrides "
        "file or the assumptions sheet. **Canonical field** is where the value lands in the deal JSON.",
        "",
    ]
    for group in GROUPS:
        rows = []
        for spec in specs:
            if group_for(spec.key) != group:
                continue
            rows.append((
                label_for(spec.key),
                f"`{spec.key}`",
                unit_label(spec.unit),
                _spec_notes(spec, guardrail_keys, CATEGORICAL_KEYS),
                _canonical_fields(spec),
            ))
        if group == "Opex":
            rows.append((
                "<T12 category>: base (year 1)",
                "`opex.<category>.base`",
                "$ a year (or % of EGR for a fee)",
                "One row per T12 expense category, from the T12 (`deal:t12`).",
                "`opex_table`",
            ))
            rows.append((
                "<T12 category>: growth",
                "`opex.<category>.growth`",
                "% a year",
                "The house growth rate for the category (`house:opex_growth_table`); see the table below.",
                "`opex_table`",
            ))
        if not rows:
            continue
        lines += [f"### {group}", ""]
        lines += _table(("Assumption", "Key", "Unit", "Notes", "Canonical field"), rows)
        lines.append("")
    return lines


def _house_values_section() -> list[str]:
    from engine import underwriting_policy as house

    lines = [
        "## House values",
        "",
        "The numbers behind the house defaults and floors. They are policy, not market data, and they "
        "live in `engine/underwriting_policy.py`.",
        "",
        "### By vintage (decade built)",
        "",
        "The vintage bands, in 2026 dollars:",
        "",
    ]
    rows = []
    for bucket, row in house.VINTAGE_FRAMEWORK_2026.items():
        cells = [f"{bucket} ({row['year_min']}–{row['year_max']})"]
        cells += [f"{_usd(row[low])}–{_usd(row[high])}" for _title, (low, high) in VINTAGE_BANDS]
        rows.append(cells)
    lines += _table(["Vintage", *(title for title, _keys in VINTAGE_BANDS)], rows)
    lines += [
        "",
        "The house minimum (the default when the deal has no better evidence, and the floor the guardrails "
        "check) for each profile. R&M is on the floor basis: R&M plus turnover, payroll excluded. "
        "Loss to lease is not changed by deferred maintenance.",
        "",
    ]
    rows = []
    for bucket, row in house.VINTAGE_FRAMEWORK_2026.items():
        year = row["year_min"]
        for label, strategy, deferred in PROFILES:
            rows.append((
                bucket,
                label,
                _pct(house.default_physical_vacancy_rate(year, strategy=strategy, deferred=deferred)),
                _pct(house.default_collection_loss_rate(year, strategy=strategy, deferred=deferred)),
                _pct(house.default_year1_concessions_rate(year, strategy=strategy, deferred=deferred)),
                _pct(house.default_loss_to_lease_rate(year, strategy=strategy)),
                _usd(house.default_r_and_m_per_unit(year, strategy=strategy, deferred=deferred)),
                _usd(house.default_recurring_capex_per_unit(year, strategy=strategy, deferred=deferred)),
            ))
    lines += _table(
        ("Vintage", "Profile", "Physical vacancy", "Collection loss", "Year-1 concessions", "Loss to lease",
         "R&M ($/unit/yr)", "Recurring capex ($/unit/yr)"),
        rows,
    )
    lines += ["", "### Named house values", ""]
    constants: tuple[tuple[str, str, str], ...] = (
        ("Target cash-on-cash (pricing)", "PRIMARY_COC_TARGET_PCT", "pct"),
        ("Agency spread over the benchmark treasury (pricing)", "DEFAULT_AGENCY_SPREAD_PCT", "pct"),
        ("Purchase closing costs", "DEFAULT_PURCHASE_CLOSING_COST_PCT", "pct"),
        ("Acquisition fee", "DEFAULT_ACQUISITION_FEE_PCT", "pct"),
        ("Asset management fee (a year)", "DEFAULT_ASSET_MANAGEMENT_FEE_PCT", "pct"),
        ("Disposition fee", "DEFAULT_DISPOSITION_FEE_PCT", "pct"),
        ("Partnership closing costs", "DEFAULT_PARTNERSHIP_CLOSING_COSTS", "usd"),
        ("Annual partnership expenses", "DEFAULT_ANNUAL_PARTNERSHIP_EXPENSES", "usd"),
        ("Management fee when the T12 has none (% of EGR)", "DEFAULT_MANAGEMENT_FEE_PCT_EGR", "pct"),
        ("Year-1 insurance floor ($/unit)", "DEFAULT_YEAR1_INSURANCE_PER_UNIT", "usd"),
        ("Replacement reserve, capex bucket 3 ($/unit/yr)", "HOUSE_REPLACEMENT_RESERVE_PER_UNIT", "usd"),
        ("Payroll benchmark for the R&M payroll review ($/unit/yr)", "HOUSE_PAYROLL_BENCHMARK_PER_UNIT", "usd"),
        ("Long-run rent growth", "HOUSE_LONG_RUN_RENT_GROWTH", "pct"),
        ("Collection loss with no T12 and no vintage", "HOUSE_DEFAULT_COLLECTION_LOSS", "pct"),
        ("Exit cap spread over the going-in cap", "HOUSE_EXIT_CAP_SPREAD", "pct"),
        ("Real estate tax growth after reassessment", "HOUSE_RE_TAX_GROWTH_AFTER_REASSESSMENT", "pct"),
        ("Recurring capex growth", "HOUSE_RECURRING_CAPEX_GROWTH", "pct"),
    )
    rows = [(label, f"`{name}`", _fmt(getattr(house, name), kind)) for label, name, kind in constants]
    rows.append((
        "Exit-cap grid offsets (bps)",
        "`HOUSE_EXIT_CAP_SENSITIVITY_OFFSETS_BPS`",
        ", ".join(f"{o:+d}" for o in house.HOUSE_EXIT_CAP_SENSITIVITY_OFFSETS_BPS),
    ))
    rows.append((
        "PCA review: built before",
        "`CAPEX_PCA_REVIEW_BEFORE_YEAR`",
        str(house.CAPEX_PCA_REVIEW_BEFORE_YEAR),
    ))
    rows.append(("Loan sizing basis", "`HOUSE_LOAN_SIZING_NOI_BASIS`", house.HOUSE_LOAN_SIZING_NOI_BASIS))
    rows.append(("Exit NOI window", "`HOUSE_EXIT_NOI_WINDOW`", house.HOUSE_EXIT_NOI_WINDOW))
    lines += _table(("What", "Constant", "Value"), rows)
    lines += [
        "",
        "### Opex growth by T12 category",
        "",
        "Percent-of-revenue lines (such as a management fee charged on EGR) do not grow on their own: "
        "they move with revenue. A category not listed grows at "
        f"{_pct(house.OPEX_GROWTH_DEFAULT)} (`OPEX_GROWTH_DEFAULT`).",
        "",
    ]
    rows = [
        (category, f"`{name}`", _pct(rate) if rate is not None else "—")
        for category, (name, rate) in house.OPEX_GROWTH_TABLE.items()
    ]
    lines += _table(("Category", "Constant", "Growth a year"), rows)
    lines.append("")
    return lines


def render() -> str:
    lines = [
        "# Assumption glossary",
        "",
        "<!-- Generated by scripts/build_glossary.py from the engine code. Do not edit by hand: "
        "change the code, then run `python scripts/build_glossary.py`. -->",
        "",
        "Every assumption the engine underwrites on, where its default comes from, and the house values "
        "behind the defaults. New to the tool? Start with the [user guide](USER_GUIDE.md).",
        "",
    ]
    lines += _sources_section()
    lines += _assumptions_section()
    lines += _house_values_section()
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="Exit 1 if the committed glossary is stale.")
    parser.add_argument("--output", type=Path, default=GLOSSARY_PATH)
    args = parser.parse_args(argv)
    text = render()
    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.exists() else None
        if current != text:
            print(f"stale: {args.output} (run python scripts/build_glossary.py)")
            return 1
        return 0
    args.output.write_text(text, encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
