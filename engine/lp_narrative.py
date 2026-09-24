"""LP report narrative generation, exception detection, and outlook computation.

Content-only module — returns plain dicts. No PDF/rendering logic.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from engine.formatters import fmt_currency, fmt_pct, fmt_multiple


def generate_narrative(
    portfolio,
    deltas: Dict[str, Any],
    am_data: Optional[Dict[str, Any]] = None,
    n_exceptions: int = 0,
) -> Dict[str, Any]:
    summary = portfolio.summary()
    deal_count = summary["deal_count"]
    total_equity = summary["total_equity"]
    weighted_irr = summary["weighted_avg_levered_irr"]

    if am_data:
        noi_source = "am_actuals"
        total_actual = sum(d.get("actual_noi", 0) for d in am_data.values())
        total_underwritten = sum(d.get("engine_noi", 0) for d in am_data.values())
        if total_underwritten:
            noi_delta_pct = (total_actual - total_underwritten) / abs(total_underwritten) * 100
        else:
            noi_delta_pct = 0.0
    else:
        noi_source = "snapshot_delta"
        noi_deltas = deltas.get("summary_deltas", {}).get("total_noi_year_1", {})
        previous = noi_deltas.get("previous", 0)
        current = noi_deltas.get("current", 0)
        if previous:
            noi_delta_pct = (current - previous) / abs(previous) * 100
        else:
            noi_delta_pct = 0.0

    period = deltas.get("current_period", "")
    lines: List[str] = []

    direction = "above" if noi_delta_pct >= 0 else "below"
    lines.append(
        f"Portfolio NOI is tracking {abs(noi_delta_pct):.1f}% {direction} "
        f"underwriting across {deal_count} deals."
    )
    lines.append(
        f"Total equity deployed: {fmt_currency(total_equity)}. "
        f"Weighted average levered IRR: {fmt_pct(weighted_irr)}."
    )
    if n_exceptions > 0:
        lines.append(f"{n_exceptions} deal(s) flagged for >10% NOI variance.")

    new_deals = deltas.get("new_deals", [])
    if new_deals:
        lines.append(f"{len(new_deals)} new acquisitions this period.")

    removed = deltas.get("removed_deals", [])
    if removed:
        lines.append(f"{len(removed)} dispositions this period.")

    return {
        "summary_lines": lines,
        "period": period,
        "deal_count": deal_count,
        "portfolio_noi_delta_pct": noi_delta_pct,
        "noi_source": noi_source,
        "stale_deals": [],
    }


def detect_exceptions(
    portfolio,
    am_data: Optional[Dict[str, Any]] = None,
    prev_deal_noi: Optional[Dict[str, float]] = None,
    threshold: float = 0.10,
) -> List[Dict[str, Any]]:
    exceptions: List[Dict[str, Any]] = []
    for deal in portfolio.deals:
        deal_id = deal.deal_id
        engine_noi = deal.noi_year_1

        if am_data and deal_id in am_data:
            am = am_data[deal_id]
            actual_noi = am.get("actual_noi", 0)
            base_noi = am.get("engine_noi", engine_noi)
            if not base_noi:
                continue
            variance_pct = (actual_noi - base_noi) / abs(base_noi) * 100
            if abs(variance_pct) > threshold * 100:
                # Check for enriched line-item waterfall
                waterfall = am.get("line_item_waterfall")
                if waterfall:
                    # Use top 3 waterfall contributors for root cause
                    top_contributors = [
                        {
                            "category": w["category"],
                            "description": w.get("description", w["category"]),
                            "contribution": w["contribution"],
                            "contribution_pct": w.get("contribution_pct", 0),
                            "direction": w["direction"],
                        }
                        for w in waterfall[:3]
                    ]
                    # Derive root_cause from top contributor category
                    top_cat = top_contributors[0]["category"]
                    if top_cat in ("net_rental_income", "rubs_income", "other_income", "bad_debt"):
                        root_cause = "revenue"
                    else:
                        root_cause = "opex"
                    exceptions.append({
                        "deal_id": deal_id,
                        "noi_variance_pct": variance_pct,
                        "source": "am_actuals",
                        "root_cause": root_cause,
                        "root_cause_detail": top_contributors,
                    })
                else:
                    # Fallback: old-style component variance
                    components = {
                        "occupancy": abs(am.get("occupancy_variance", 0)),
                        "revenue": abs(am.get("revenue_variance", 0)),
                        "opex": abs(am.get("opex_variance", 0)),
                        "capex_overrun": abs(am.get("capex_variance", 0)),
                    }
                    root_cause = max(components, key=components.get)
                    exceptions.append({
                        "deal_id": deal_id,
                        "noi_variance_pct": variance_pct,
                        "source": "am_actuals",
                        "root_cause": root_cause,
                    })
        elif prev_deal_noi and deal_id in prev_deal_noi:
            prev_noi = prev_deal_noi[deal_id]
            if not prev_noi:
                continue
            variance_pct = (engine_noi - prev_noi) / abs(prev_noi) * 100
            if abs(variance_pct) > threshold * 100:
                root_cause = "revenue" if variance_pct < 0 else "opex"
                exceptions.append({
                    "deal_id": deal_id,
                    "noi_variance_pct": variance_pct,
                    "source": "snapshot_delta",
                    "root_cause": root_cause,
                })

    exceptions.sort(key=lambda e: abs(e["noi_variance_pct"]), reverse=True)
    return exceptions


def compute_outlook(
    portfolio,
    lookahead_months: int = 3,
    current_month: int = 0,
    am_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    window_start = current_month + 1
    window_end = current_month + lookahead_months

    total_units = 0
    weighted_turnover_pct = 0.0
    rate_cap_expirations: List[Dict[str, Any]] = []
    reno_units = 0
    reno_cost = 0.0
    refi_events: List[Dict[str, Any]] = []
    am_lease_total = 0
    has_am_leases = False

    for inputs in portfolio._deal_inputs:
        meta = inputs.get("metadata", {})
        deal_id = meta.get("deal_id", "unknown")

        cohorts = inputs.get("unit_cohorts", [])
        deal_units = sum(c.get("count", 0) for c in cohorts)
        total_units += deal_units

        trade_out = inputs.get("trade_out_assumptions", {})
        annual_turnover = trade_out.get("annual_turnover_pct", 0)
        weighted_turnover_pct += annual_turnover * deal_units

        debt = inputs.get("debt_terms", {})
        cap_expiry = debt.get("rate_cap_expiry_month")
        if cap_expiry and window_start <= cap_expiry <= window_end:
            rate_cap_expirations.append({"deal_id": deal_id, "expiry_month": cap_expiry})

        for layer in inputs.get("capital_stack", []):
            layer_cap = layer.get("rate_cap_expiry_month")
            if layer_cap and window_start <= layer_cap <= window_end:
                rate_cap_expirations.append({"deal_id": deal_id, "expiry_month": layer_cap})

        for reno in inputs.get("unit_renovations", []):
            reno_month = reno.get("renovation_month", 0)
            if window_start <= reno_month <= window_end:
                reno_units += 1
                reno_cost += reno.get("cost", 0)

        refi = inputs.get("refi_event", {})
        trigger = refi.get("trigger_month")
        if trigger and window_start <= trigger <= window_end:
            refi_events.append({"deal_id": deal_id, "trigger_month": trigger})

        if am_data and deal_id in am_data:
            am_leases = am_data[deal_id].get("upcoming_lease_expirations")
            if am_leases is not None:
                am_lease_total += am_leases
                has_am_leases = True

    if total_units > 0:
        avg_turnover_pct = weighted_turnover_pct / total_units
        estimated_turns = math.ceil(total_units * avg_turnover_pct / 12 * lookahead_months)
    else:
        avg_turnover_pct = 0.0
        estimated_turns = 0

    return {
        "turnover_estimate": {"units": estimated_turns, "pct": avg_turnover_pct * 100},
        "am_lease_expirations": am_lease_total if has_am_leases else None,
        "rate_cap_expirations": rate_cap_expirations,
        "renovation_pipeline": {"units_scheduled": reno_units, "cost_estimate": reno_cost},
        "upcoming_refi_events": refi_events,
    }


def detect_stale_deals(
    deal_timestamps: Dict[str, str],
    max_age_days: int = 30,
    now: Optional[datetime] = None,
) -> List[str]:
    if now is None:
        now = datetime.now(timezone.utc)
    stale: List[str] = []
    for deal_id, ts_str in deal_timestamps.items():
        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (now - ts).days
        if age > max_age_days:
            stale.append(deal_id)
    stale.sort()
    return stale
