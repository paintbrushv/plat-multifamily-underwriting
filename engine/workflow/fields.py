"""The deal profile ``describe_deal`` records: every field, its check, and why it matters.

The fields mirror the ``runs/ingest_deal.py`` flags. ``FIELDS`` is the single
source for the MCP tool schema descriptions, the validation in
``engine.workflow.service.describe_deal`` and the field table in
``docs/MCP_TOOLS.md``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from engine.assumptions.profile import ASSET_CLASSES, STAR_TIERS, SUPPORTED_DEAL_TYPES
from engine.underwriting_policy import IMMEDIATE_CAPEX_FUNDING_MODES, LOAN_SIZING_NOI_BASES

_MONTH_RE = re.compile(r"\d{4}-(0[1-9]|1[0-2])")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class Field:
    name: str
    kind: str  # int | float | rate | str | bool | month | date | choice
    group: str
    what: str
    why: str
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None

    @property
    def description(self) -> str:
        text = f"{self.what} Why it matters: {self.why}"
        if self.choices:
            text += f" One of: {', '.join(self.choices)}."
        return text

    def check(self, value: Any) -> Any:
        """The cleaned value, or ValueError with a message an analyst can act on."""
        if self.kind == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"{self.name} must be true or false")
            return value
        if self.kind in ("int", "float", "rate"):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{self.name} must be a number")
            if self.kind == "int":
                if float(value) != int(value):
                    raise ValueError(f"{self.name} must be a whole number")
                value = int(value)
            else:
                value = float(value)
            if self.minimum is not None and value < self.minimum:
                raise ValueError(f"{self.name} must be at least {self.minimum:g}")
            if self.maximum is not None and value > self.maximum:
                hint = " (rates are decimals: 0.065 means 6.5%)" if self.kind == "rate" else ""
                raise ValueError(f"{self.name} must be at most {self.maximum:g}{hint}")
            return value
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{self.name} must be a non-empty string")
        value = value.strip()
        if self.kind == "month" and not _MONTH_RE.fullmatch(value):
            raise ValueError(f"{self.name} must be a month as YYYY-MM (e.g. 2026-11)")
        if self.kind == "date" and not _DATE_RE.fullmatch(value):
            raise ValueError(f"{self.name} must be a date as YYYY-MM-DD")
        if self.choices and value not in self.choices:
            raise ValueError(f"{self.name} must be one of {', '.join(self.choices)}")
        if len(value) > 200:
            raise ValueError(f"{self.name} is too long (200 characters at most)")
        return value


FIELDS: tuple[Field, ...] = (
    # ── deal and property ────────────────────────────────────────────────
    Field("deal_type", "choice", "Property", "Deal profile.",
          "Selects the house assumption set and the price-search basis: stabilized prices on year-1 "
          "cash-on-cash, value_add on the first stabilized year.", choices=SUPPORTED_DEAL_TYPES),
    Field("year_built", "int", "Property", "Year the property was built.",
          "Drives the house vintage defaults (loss to lease, turn cost, the recurring-capex band and the "
          "collection-loss floor); older assets carry more capex.", minimum=1800, maximum=2200),
    Field("asset_class", "choice", "Property", "Asset class (A, B or C).",
          "Maps to a CoStar-style star tier for market data when star_tier is not given.",
          choices=ASSET_CLASSES),
    Field("star_tier", "choice", "Property", "CoStar-style star tier.",
          "Part of the market-data geography: rent growth, vacancy and cap rates are read at this tier.",
          choices=STAR_TIERS),
    Field("deferred_maintenance", "bool", "Property", "The asset has known deferred maintenance.",
          "Recurring capex then defaults to the high end of the vintage band."),
    # ── geography ────────────────────────────────────────────────────────
    Field("msa", "str", "Market", "MSA name or five-digit CBSA code.",
          "The market-data geography: MSA rent growth, vacancy, concessions and market cap rate feed the "
          "assumptions when no submarket data exists."),
    Field("metro_slug", "str", "Market", "Metro key the market-data feeders use (e.g. 'synthetic_metro').",
          "Feeders look the metro up by this slug; without it market data may not match."),
    Field("submarket", "str", "Market", "Submarket name.",
          "Submarket market data outranks MSA data in the assumption precedence."),
    # ── analysis period ──────────────────────────────────────────────────
    Field("start", "month", "Analysis", "Analysis start month, YYYY-MM (usually the month after closing).",
          "Required to ingest: the monthly cash flow starts here."),
    Field("end", "month", "Analysis", "Analysis end month, YYYY-MM (a five-year hold is typical).",
          "Required to ingest: the hold period, and the default exit month."),
    Field("exit_month", "month", "Analysis", "Sale month, YYYY-MM (default: the analysis end).",
          "The reversion is priced on the NOI around this month."),
    # ── price and debt ───────────────────────────────────────────────────
    Field("purchase_price", "float", "Price and debt", "Purchase price in dollars.",
          "The basis for the loan, property tax, cap rates and returns; run_deal needs it (solve_price "
          "finds a price instead).", minimum=1),
    Field("ltv", "rate", "Price and debt", "Loan-to-value as a decimal (0.65 = 65%).",
          "Sizes the loan commitment from the purchase price.", minimum=0, maximum=0.95),
    Field("rate", "rate", "Price and debt", "Loan interest rate as a decimal (0.065 = 6.5%).",
          "Sets debt service, DSCR and cash-on-cash.", minimum=0, maximum=0.25),
    Field("amort", "int", "Price and debt", "Amortization in years (e.g. 30).",
          "Sets the principal paid after any interest-only period.", minimum=1, maximum=50),
    Field("io_months", "int", "Price and debt", "Interest-only months (default 0).",
          "Interest-only months lift early cash flow and cash-on-cash.", minimum=0, maximum=600),
    Field("term_months", "int", "Price and debt", "Loan term in months (default: the analysis length).",
          "When the loan matures relative to the exit.", minimum=1, maximum=600),
    Field("loan_sizing_noi_basis", "choice", "Price and debt", "Cash flow the agency loan is sized on.",
          "The house sizes on NOI less the below-NOI capex charge, so older assets carry a smaller loan; "
          "DSCR is reported on this basis and on NOI.", choices=LOAN_SIZING_NOI_BASES),
    # ── property tax ─────────────────────────────────────────────────────
    Field("millage", "float", "Property tax", "Combined property-tax millage in mills per $1,000 of assessed "
          "value (e.g. 22.5).",
          "The engine taxes price x assessment ratio x mills / 1000; a priced deal fails validation "
          "without it (PROPERTY_TAX_MILLAGE_MISSING).", minimum=0, maximum=200),
    Field("assessment_ratio", "float", "Property tax", "Assessed value / purchase price (default 1.0).",
          "Scales the taxable value; any value other than 1.0 is recorded as an analyst override.",
          minimum=0, maximum=2),
    # ── capital plan (ADR 0003 §3) ───────────────────────────────────────
    Field("pca_immediate_capex", "float", "Capital plan", "PCA immediate needs, total dollars.",
          "Property condition assessment evidence (deal:pca) outranks analyst and house figures for "
          "capex bucket 1, funded at close.", minimum=0),
    Field("pca_recurring_capex_per_unit", "float", "Capital plan", "PCA recurring capex, $ per unit per year.",
          "PCA evidence for capex bucket 2, charged below NOI every year.", minimum=0),
    Field("pca_as_of", "date", "Capital plan", "Date of the PCA, YYYY-MM-DD.",
          "Recorded with the PCA evidence so reviewers can see how current it is."),
    Field("pca_reference", "str", "Capital plan", "Which PCA report the figures come from.",
          "Recorded with the PCA evidence for the audit trail."),
    Field("immediate_capex", "float", "Capital plan", "Immediate / deferred capex at close, total dollars "
          "(analyst figure).",
          "Capex bucket 1 when no PCA covers it; it adds to the equity at close, never to NOI.", minimum=0),
    Field("immediate_capex_funding", "choice", "Capital plan", "How immediate capex is funded.",
          "equity_at_close adds it to the equity basis (lowering cash-on-cash); lender_holdback funds it "
          "from the loan.", choices=IMMEDIATE_CAPEX_FUNDING_MODES),
    Field("recurring_capex_per_unit", "float", "Capital plan", "Recurring capex, $ per unit per year, year 1 "
          "(analyst figure).",
          "Capex bucket 2 when no PCA covers it; the charge below NOI is max(this, the replacement reserve).",
          minimum=0),
    # ── exit ─────────────────────────────────────────────────────────────
    Field("exit_cap", "rate", "Exit", "Exit cap rate as a decimal (analyst figure).",
          "Sets the sale price; omitted, the house rule (going-in cap plus a spread) sets it.",
          minimum=0.01, maximum=0.25),
    Field("sale_cost", "rate", "Exit", "Sale costs as a decimal of the sale price (house default 2%).",
          "Comes off the sale proceeds.", minimum=0, maximum=0.2),
    # ── operating overrides ──────────────────────────────────────────────
    Field("rent_growth", "rate", "Operations", "Annual rent growth as a decimal (analyst figure).",
          "Replaces the market or house rent-growth path with a flat rate.", minimum=-0.2, maximum=0.25),
    Field("vacancy", "rate", "Operations", "Physical vacancy as a decimal (analyst figure).",
          "Replaces the vacancy the rent roll and market data imply.", minimum=0, maximum=1),
    Field("collection_loss", "rate", "Operations", "Collection loss (bad debt) as a decimal (analyst figure).",
          "Replaces the T12 or vintage-floor collection loss.", minimum=0, maximum=0.5),
    # ── fund structure ───────────────────────────────────────────────────
    Field("sponsor_pct", "rate", "Fund", "Sponsor (GP) share of equity as a decimal (e.g. 0.10).",
          "Splits the equity and the waterfall between GP and LP.", minimum=0, maximum=1),
    Field("pref_return", "rate", "Fund", "LP preferred return as a decimal (e.g. 0.08).",
          "The hurdle the LP earns before the promote.", minimum=0, maximum=0.5),
    Field("acq_fee", "rate", "Fund", "Acquisition fee as a decimal of price (house default 1%).",
          "Adds to the equity at close.", minimum=0, maximum=0.1),
    Field("am_fee", "rate", "Fund", "Asset management fee as a decimal (house default 1.5%).",
          "Reduces distributable cash flow.", minimum=0, maximum=0.1),
    Field("disp_fee", "rate", "Fund", "Disposition fee as a decimal of the sale price (house default 1%).",
          "Comes off the sale proceeds.", minimum=0, maximum=0.1),
    # ── workflow ─────────────────────────────────────────────────────────
    Field("analyst", "str", "Workflow", "Analyst name recorded on the deal.",
          "Shown in the canonical metadata and outputs."),
    Field("use_market_data", "bool", "Workflow", "Use the configured market-data feeders (default true).",
          "Off, assumptions come from deal evidence and house policy only; the market data status says "
          "what was used either way."),
)

BY_NAME = {field.name: field for field in FIELDS}
REQUIRED_FOR_INGEST = ("start", "end")
# What run_deal needs beyond ingest (``service.missing_to_run``); solve_price finds a price instead.
REQUIRED_TO_RUN = ("purchase_price", "ltv", "rate", "amort", "millage")


def check_profile_updates(updates: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Cleaned values for the fields given (``None`` values skipped), and errors."""
    cleaned: dict[str, Any] = {}
    errors: list[str] = []
    for name, value in updates.items():
        if value is None:
            continue
        field = BY_NAME.get(name)
        if field is None:
            errors.append(f"unknown field {name!r}")
            continue
        try:
            cleaned[name] = field.check(value)
        except ValueError as exc:
            errors.append(str(exc))
    if not errors and "start" in cleaned and "end" in cleaned and cleaned["end"] <= cleaned["start"]:
        errors.append("end must be after start")
    return cleaned, errors


def field_table() -> list[dict[str, Any]]:
    """Every field as a row: name, group, type, what, why, choices."""
    return [
        {"name": f.name, "group": f.group, "type": f.kind, "what": f.what, "why": f.why,
         **({"choices": list(f.choices)} if f.choices else {})}
        for f in FIELDS
    ]
