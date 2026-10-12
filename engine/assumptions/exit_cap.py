"""Entry-anchored exit cap rate (ADR 0003 §3).

Unless the analyst (or an explicit backsolve policy) sets it, the exit cap is
the going-in cap rate at the price being modeled (year-1 NOI / purchase price)
plus ``HOUSE_EXIT_CAP_SPREAD``. The rare "steal" exception (a market cap rate is
known and the going-in cap is more than ``HOUSE_STEAL_THRESHOLD`` above it) is
``engine/underwriting_policy.house_exit_cap_rate``.

Year-1 NOI is the engine's analysis-year-1 NOI at that price (the real estate
tax line is reassessed at it), measured on an all-equity preview copy of the
canonical: financing and the exit never change NOI. The resolver
(``engine/assumptions/curves.py``) and backsolve (at every candidate price) use
the same functions, so the exit cap a solved price is priced with is the one
its re-resolution records.

Deterministic: the preview is a pure function of the canonical and the price,
and its NOIs are memoized by a content hash.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from copy import deepcopy
from datetime import date
from decimal import Decimal
from typing import Any, Callable, Mapping, Optional

from engine import underwriting_policy as house
from engine.modules import loan_sizing

# The preview needs some exit cap to run; it never changes NOI.
PREVIEW_EXIT_CAP_RATE = 0.0625
_PLACES = 6
_CACHE: "OrderedDict[str, dict[str, Decimal]]" = OrderedDict()
_CACHE_SIZE = 128


def _hold_months(canonical: Mapping[str, Any]) -> int:
    start = str(canonical["time_grid"]["analysis_start_date"])[:7]
    end = str(canonical["time_grid"]["analysis_end_date"])[:7]
    return (int(end[:4]) - int(start[:4])) * 12 + (int(end[5:7]) - int(start[5:7])) + 1


def preview_case(canonical: Mapping[str, Any], price: Decimal | float) -> dict[str, Any]:
    """An all-equity copy of ``canonical`` at ``price`` (financing and fees removed)."""
    preview = deepcopy(dict(canonical))
    preview.pop("assumptions", None)
    price = float(price)
    preview["purchase_assumptions"] = {
        "purchase_price": price,
        "equity_contribution": price,
        "closing_costs": 0.0,
        "total_equity_basis": price,
    }
    preview["pricing_provenance"] = {
        "published_om_price": None,
        "broker_whisper_price": None,
        "strike_price": price,
        "strike_price_basis": "other",
        "strike_price_derivation": (
            "Temporary all-equity preview case used only to estimate NOI at a price."
        ),
        "om_pricing_process": "unknown",
        "as_of_date": str(date.today()),
    }
    preview["debt_terms"] = {
        "commitment": 0.0,
        "rate": 0.0,
        "amort_years": 30,
        "io_months": 0,
        "term_months": _hold_months(preview),
        "loan_start_month": str(preview["time_grid"]["analysis_start_date"])[:7],
    }
    preview.setdefault("fund_assumptions", {})
    exit_assumptions = preview.setdefault("exit_assumptions", {"sale_cost_percent": 0.02})
    if exit_assumptions.get("exit_cap_rate") is None:
        exit_assumptions["exit_cap_rate"] = PREVIEW_EXIT_CAP_RATE
    return preview


def _key(canonical: Mapping[str, Any], price: Decimal | float) -> str:
    body = {k: v for k, v in canonical.items() if k != "assumptions"}
    # Normalized, so 19000000, 19000000.0 and "19000000.00" share one entry.
    text = json.dumps(body, sort_keys=True, default=str) + f"|{Decimal(str(price)).normalize()}"
    return hashlib.sha256(text.encode()).hexdigest()


def preview_noi(
    canonical: Mapping[str, Any],
    price: Decimal | float,
    *,
    run: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
) -> dict[str, Optional[Decimal]]:
    """``{"year_1": analysis-year-1 NOI, "projected": the second cashflow year's NOI, ...}``.

    ``projected_capex_charge`` and ``projected_lender_reserve`` are that
    year's below-NOI capex charge and lender reserve (the deductions the loan
    sizing NOI basis takes, ``engine/modules/loan_sizing.py``).

    Runs the engine (``run``, default ``engine.engine.run_underwriting``) on
    ``preview_case``; an engine or validation error is raised. Results of the
    real engine are memoized by content hash.
    """
    from engine.engine import run_underwriting  # lazy: the engine imports the assumption layer

    run = run or run_underwriting
    cacheable = run is run_underwriting
    key = _key(canonical, price) if cacheable else None
    if key is not None and key in _CACHE:
        _CACHE.move_to_end(key)
        return dict(_CACHE[key])
    case = preview_case(canonical, price)
    # The preview needs NOI only: skip the engine's exit-cap sensitivity grid.
    results = run(case, exit_cap_sensitivity=False) if cacheable else run(case)
    by_year = results["cashflow"]["by_year"]
    projected = by_year[1] if len(by_year) >= 2 else (by_year[0] if by_year else {})
    year_1 = ((results.get("metrics") or {}).get("noi") or {}).get("year_1_noi")
    out = {
        "year_1": Decimal(str(year_1)) if year_1 is not None else None,
        "projected": Decimal(str(projected.get("net_operating_income", 0))),
        "projected_capex_charge": loan_sizing.below_noi_deduction(projected, loan_sizing.NCF_AFTER_CAPEX_CHARGE),
        "projected_lender_reserve": loan_sizing.below_noi_deduction(
            projected, loan_sizing.NCF_AFTER_LENDER_RESERVE
        ),
    }
    if key is not None:
        _CACHE[key] = out
        while len(_CACHE) > _CACHE_SIZE:
            _CACHE.popitem(last=False)
    return dict(out)


def year1_noi(canonical: Mapping[str, Any], price: Decimal | float) -> Optional[Decimal]:
    """Year-1 NOI at ``price``, or None when the engine cannot run the canonical."""
    try:
        return preview_noi(canonical, price)["year_1"]
    except Exception:  # noqa: BLE001 - an incomplete canonical has no year-1 NOI
        return None


def going_in_cap(noi: Optional[Decimal], price: Any) -> Optional[Decimal]:
    """NOI / price, or None when it is not a usable going-in cap rate."""
    if noi is None or price in (None, 0):
        return None
    rate = Decimal(str(noi)) / Decimal(str(price))
    if not Decimal("0") < rate <= house.HOUSE_EXIT_CAP_GOING_IN_MAX:
        return None
    return rate


def anchored_exit_cap(
    noi: Optional[Decimal],
    price: Any,
    *,
    market_cap_rate: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    """The house exit cap at ``price`` as a JSON-ready record, or None without a usable going-in cap.

    ``exit_cap_rate`` is rounded to 6 places; ``steal`` and ``compression``
    disclose the steal exception.
    """
    going_in = going_in_cap(noi, price)
    if going_in is None:
        return None
    decision = house.house_exit_cap_rate(going_in, market_cap_rate=market_cap_rate)
    return {
        "rule": "entry_anchored",
        "purchase_price": float(price),
        "year1_noi": float(noi),
        "going_in_cap_rate": round(float(decision["going_in_cap_rate"]), _PLACES),
        "spread": float(decision["spread"]),
        "anchored_exit_cap_rate": round(float(decision["anchored"]), _PLACES),
        "market_cap_rate": float(market_cap_rate) if market_cap_rate is not None else None,
        "excess_over_market": (
            round(float(decision["excess_over_market"]), _PLACES)
            if decision["excess_over_market"] is not None else None
        ),
        "steal": bool(decision["steal"]),
        "steal_scale": round(float(decision["steal_scale"]), _PLACES),
        "compression": round(float(decision["compression"]), _PLACES),
        "exit_cap_rate": round(float(decision["exit_cap_rate"]), _PLACES),
    }


def describe(record: Mapping[str, Any], *, noi_basis: str = "year-1 NOI") -> str:
    """A one-paragraph basis for an anchored exit cap record."""
    going_in, spread = record["going_in_cap_rate"], record["spread"]
    text = (
        f"Entry-anchored: going-in cap {going_in:.2%} at the modeled price ({noi_basis} "
        f"${record['year1_noi']:,.2f} / purchase price ${record['purchase_price']:,.2f}) plus the house "
        f"spread {spread * 10_000:g} bps (HOUSE_EXIT_CAP_SPREAD)"
    )
    market = record.get("market_cap_rate")
    if record.get("steal"):
        gap = (going_in - market) * 10_000
        threshold = float(house.HOUSE_STEAL_THRESHOLD) * 10_000
        full = float(house.HOUSE_STEAL_FULL_COMPRESSION_EXCESS) * 10_000
        cap = float(house.HOUSE_MAX_EXIT_COMPRESSION) * 10_000
        compression = float(record.get("compression") or 0) * 10_000
        return (
            f"{text} = {record['anchored_exit_cap_rate']:.2%}. Steal: going-in {going_in:.2%} vs market "
            f"{market:.2%} ({gap:.0f} bps above it, over the {threshold:g} bps HOUSE_STEAL_THRESHOLD), so the "
            f"exit compresses toward market + spread ({market + spread:.2%}) in proportion to the discount: "
            f"compression = min(1, (excess - {threshold:g} bps) / ({full:g} bps - {threshold:g} bps)) x "
            f"{cap:g} bps (HOUSE_STEAL_FULL_COMPRESSION_EXCESS, HOUSE_MAX_EXIT_COMPRESSION) = compression "
            f"{compression:.0f} bps below going-in + spread, never below market + spread: "
            f"{record['exit_cap_rate']:.2%}. Review the steal before relying on it."
        )
    text += f" = {record['exit_cap_rate']:.2%}."
    if market is None:
        return text + " No market cap rate is available, so the exit never compresses."
    if going_in <= market:
        return text + (
            f" The market cap rate {market:.2%} is a candidate only: the going-in cap is at or below it "
            "(paying at or above market), so the exit stays anchored to entry."
        )
    return text + (
        f" The market cap rate {market:.2%} is a candidate only: the going-in cap is not more than "
        f"{float(house.HOUSE_STEAL_THRESHOLD) * 10_000:g} bps (HOUSE_STEAL_THRESHOLD) above it, so there is "
        "no compression."
    )


__all__ = [
    "PREVIEW_EXIT_CAP_RATE",
    "anchored_exit_cap",
    "describe",
    "going_in_cap",
    "preview_case",
    "preview_noi",
    "year1_noi",
]
