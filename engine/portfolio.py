"""
Portfolio Analytics
====================
Cross-deal aggregation and comparison for multifamily portfolios.

Loads deal results from JSON files or dicts, computes portfolio-level
metrics, and groups by metro/vintage for LP reporting.

Usage:
    from engine.portfolio import Portfolio

    portfolio = Portfolio()
    portfolio.add_deal_from_files("output/deal1_inputs.json", "output/deal1_outputs.json")
    portfolio.add_deal_from_files("output/deal2_inputs.json", "output/deal2_outputs.json")

    summary = portfolio.summary()
    by_metro = portfolio.group_by_metro()
    comparison = portfolio.deal_comparison_matrix()
"""
from __future__ import annotations

import copy
import json
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from engine.engine import run_underwriting
from engine.modules.geo import infer_metro
from engine.modules.util import calculate_irr


def _rerun_deal_with_overrides(
    inputs: Dict[str, Any],
    overrides: Dict[str, Any],
) -> Dict[str, Any]:
    """Deep-copy inputs, apply dot-path overrides, re-run engine.

    Override keys use dot notation: 'exit_assumptions.exit_cap_rate' sets
    inputs['exit_assumptions']['exit_cap_rate'].
    Supports list indices: 'capital_stack.0.rate' sets inputs['capital_stack'][0]['rate'].
    """
    modified = copy.deepcopy(inputs)
    for key_path, value in overrides.items():
        parts = key_path.split(".")
        target = modified
        for part in parts[:-1]:
            if isinstance(target, list):
                target = target[int(part)]
            else:
                target = target[part]
        final_key = parts[-1]
        if isinstance(target, list):
            target[int(final_key)] = value
        else:
            target[final_key] = value
    return run_underwriting(modified, skip_validation=True)


def _extract_stress_metrics(results: Dict[str, Any]) -> Dict[str, Any]:
    """Pull stress-relevant metrics from full engine results."""
    metrics = results.get("metrics", {})
    irr = metrics.get("irr", {})
    em = metrics.get("equity_multiple", {})
    dscr = metrics.get("dscr", {})
    cashflow = results.get("cashflow", {})
    by_year = cashflow.get("by_year", [])

    exit_data = results.get("exit", {})
    exit_proceeds = exit_data.get("net_sale_proceeds", 0)
    noi_exit = by_year[-1].get("net_operating_income", 0) if by_year else 0
    year_1_lcf = by_year[0].get("leveraged_cash_flow", 0) if by_year else 0

    return {
        "levered_irr": irr.get("levered_irr"),
        "levered_em": em.get("levered_em"),
        "unlevered_irr": irr.get("unlevered_irr"),
        "unlevered_em": em.get("unlevered_em"),
        "min_dscr": dscr.get("minimum_dscr"),
        "avg_dscr": dscr.get("average_dscr"),
        "noi_exit": noi_exit,
        "exit_proceeds": exit_proceeds,
        "year_1_lcf": year_1_lcf,
    }


def _run_single_stress(args: tuple) -> tuple:
    """Top-level function for parallel stress execution (picklable).

    Args:
        args: (inputs_dict, overrides_dict, deal_index, shock_key)

    Returns:
        (deal_index, shock_key, extracted_metrics)
    """
    inputs, overrides, deal_idx, shock_key = args
    results = _rerun_deal_with_overrides(inputs, overrides)
    metrics = _extract_stress_metrics(results)
    return (deal_idx, shock_key, metrics)


def _compute_irr_from_cashflows(cashflows: List[float]) -> Optional[float]:
    """IRR solver. Delegates to existing calculate_irr from util."""
    return calculate_irr(cashflows)


def _equity_weighted_metrics(
    deal_metrics_list: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compute equity-weighted averages for IRR and EM fields.

    Each item: {'metrics': {'levered_irr': ..., 'levered_em': ...}, 'equity': float}
    """
    total_equity = sum(d["equity"] for d in deal_metrics_list)
    if total_equity == 0:
        return {"weighted_levered_irr": None, "weighted_levered_em": None}

    weighted_irr_items = [
        d for d in deal_metrics_list
        if d["metrics"].get("levered_irr") is not None and d["equity"] > 0
    ]
    weighted_irr = (
        sum(d["metrics"]["levered_irr"] * d["equity"] for d in weighted_irr_items)
        / sum(d["equity"] for d in weighted_irr_items)
        if weighted_irr_items else None
    )

    weighted_em_items = [
        d for d in deal_metrics_list
        if d["metrics"].get("levered_em") is not None and d["equity"] > 0
    ]
    weighted_em = (
        sum(d["metrics"]["levered_em"] * d["equity"] for d in weighted_em_items)
        / sum(d["equity"] for d in weighted_em_items)
        if weighted_em_items else None
    )

    return {
        "weighted_levered_irr": round(weighted_irr, 6) if weighted_irr is not None else None,
        "weighted_levered_em": round(weighted_em, 4) if weighted_em is not None else None,
    }


@dataclass
class DealSnapshot:
    """Extracted metrics for a single deal in the portfolio."""

    deal_id: str
    metro: str
    units: int
    sqft: int
    purchase_price: float
    price_per_unit: float
    price_per_sf: float
    going_in_cap: Optional[float]
    exit_cap: Optional[float]
    noi_year_1: float
    noi_exit: float
    levered_irr: Optional[float]
    unlevered_irr: Optional[float]
    levered_em: Optional[float]
    unlevered_em: Optional[float]
    partnership_irr: Optional[float]
    partnership_em: Optional[float]
    average_dscr: Optional[float]
    minimum_dscr: Optional[float]
    total_equity: float
    hold_start: str
    hold_end: str
    vintage_year: int


def _infer_metro(inputs: Dict[str, Any]) -> str:
    """Backwards-compat shim. Delegates to engine.modules.geo.infer_metro.

    Kept because tests import this symbol directly.
    """
    return infer_metro(inputs)


def _extract_deal_snapshot(
    inputs: Dict[str, Any],
    results: Dict[str, Any],
) -> DealSnapshot:
    """Extract a DealSnapshot from engine inputs + results."""
    meta = inputs.get("metadata", {})
    purchase = inputs.get("purchase_assumptions", {})
    exit_a = inputs.get("exit_assumptions", {})
    tg = inputs.get("time_grid", {})
    cohorts = inputs.get("unit_cohorts", [])

    total_units = sum(c.get("unit_count", 0) for c in cohorts)
    total_sf = sum(c.get("unit_count", 0) * c.get("sqft", 0) for c in cohorts)
    pp = purchase.get("purchase_price", 0)

    metrics = results.get("metrics", {})
    irr = metrics.get("irr", {})
    em = metrics.get("equity_multiple", {})
    dscr = metrics.get("dscr", {})
    yields_d = metrics.get("yields", {})

    cashflow = results.get("cashflow", {})
    by_year = cashflow.get("by_year", [])
    noi_y1 = by_year[0].get("net_operating_income", 0) if by_year else 0
    noi_exit = by_year[-1].get("net_operating_income", 0) if by_year else 0

    fund = results.get("fund_waterfall", {}).get("summary", {})

    # Total equity: prefer explicit input, then derive from debt
    total_equity = purchase.get("total_equity_basis") or purchase.get("equity_contribution")
    if not total_equity:
        if inputs.get("capital_stack"):
            loan = sum(
                layer.get("commitment", 0) for layer in inputs["capital_stack"]
                if layer.get("layer_type") in ("senior", "mezzanine")
            )
        else:
            loan = inputs.get("debt_terms", {}).get("commitment", 0)
        total_equity = pp - loan if pp > loan else pp * 0.3

    hold_start = tg.get("analysis_start_date", "")
    hold_end = tg.get("analysis_end_date", "")
    vintage = int(hold_start[:4]) if hold_start else 0

    metro = infer_metro(inputs)

    return DealSnapshot(
        deal_id=meta.get("deal_id", "Unknown"),
        metro=metro,
        units=total_units,
        sqft=total_sf,
        purchase_price=pp,
        price_per_unit=pp / total_units if total_units else 0,
        price_per_sf=pp / total_sf if total_sf else 0,
        going_in_cap=yields_d.get("going_in_cap_rate"),
        exit_cap=exit_a.get("exit_cap_rate"),
        noi_year_1=noi_y1,
        noi_exit=noi_exit,
        levered_irr=irr.get("levered_irr"),
        unlevered_irr=irr.get("unlevered_irr"),
        levered_em=em.get("levered_em"),
        unlevered_em=em.get("unlevered_em"),
        partnership_irr=irr.get("partnership_irr"),
        partnership_em=fund.get("partnership_equity_multiple"),
        average_dscr=dscr.get("average_dscr"),
        minimum_dscr=dscr.get("minimum_dscr"),
        total_equity=total_equity,
        hold_start=hold_start,
        hold_end=hold_end,
        vintage_year=vintage,
    )


class Portfolio:
    """Cross-deal portfolio analytics."""

    def __init__(self):
        self.deals: List[DealSnapshot] = []
        self._deal_inputs: List[Dict[str, Any]] = []

    def add_deal(self, inputs: Dict[str, Any], results: Dict[str, Any]) -> DealSnapshot:
        """Add a deal from in-memory inputs + results dicts."""
        snap = _extract_deal_snapshot(inputs, results)
        self.deals.append(snap)
        self._deal_inputs.append(copy.deepcopy(inputs))
        return snap

    def add_deal_from_files(
        self,
        inputs_path: str | Path,
        outputs_path: str | Path,
    ) -> DealSnapshot:
        """Add a deal from JSON files."""
        inputs = json.loads(Path(inputs_path).read_text(encoding="utf-8"))
        results = json.loads(Path(outputs_path).read_text(encoding="utf-8"))
        return self.add_deal(inputs, results)

    def add_deals_from_directory(self, directory: str | Path) -> int:
        """Scan a directory for *_inputs.json / *_outputs.json pairs and load them.

        Returns the number of deals loaded.
        """
        directory = Path(directory)
        loaded = 0
        for inputs_file in sorted(directory.glob("*_inputs.json")):
            stem = inputs_file.name.replace("_inputs.json", "")
            outputs_file = directory / f"{stem}_outputs.json"
            if outputs_file.exists():
                self.add_deal_from_files(inputs_file, outputs_file)
                loaded += 1
        return loaded

    def add_deals_from_cosmos(self, store, deal_ids: list[str] | None = None) -> int:
        """Load latest run for each deal from a CosmosStore and add to portfolio.

        Args:
            store: CosmosStore (or any object with load_latest/list_deals interface)
            deal_ids: Specific deals to load (None = all deals in store)

        Returns:
            Number of deals successfully loaded
        """
        if deal_ids is None:
            deal_ids = store.list_deals()

        loaded = 0
        for deal_id in deal_ids:
            doc = store.load_latest(deal_id)
            if doc and doc.get("inputs") and doc.get("results"):
                try:
                    self.add_deal(doc["inputs"], doc["results"])
                    loaded += 1
                except Exception:
                    continue  # Skip deals that fail to parse
        return loaded

    def summary(self) -> Dict[str, Any]:
        """Compute portfolio-level summary metrics."""
        if not self.deals:
            return {"deal_count": 0}

        total_units = sum(d.units for d in self.deals)
        total_equity = sum(d.total_equity for d in self.deals)
        total_purchase = sum(d.purchase_price for d in self.deals)
        total_noi_y1 = sum(d.noi_year_1 for d in self.deals)

        # Equity-weighted IRR / EM via shared helper (handles zero-equity guard).
        weighted = _equity_weighted_metrics([
            {
                "metrics": {"levered_irr": d.levered_irr, "levered_em": d.levered_em},
                "equity": d.total_equity,
            }
            for d in self.deals
        ])
        weighted_irr = weighted["weighted_levered_irr"]
        weighted_em = weighted["weighted_levered_em"]

        return {
            "deal_count": len(self.deals),
            "total_units": total_units,
            "total_purchase_price": round(total_purchase, 0),
            "total_equity": round(total_equity, 0),
            "total_noi_year_1": round(total_noi_y1, 0),
            "avg_price_per_unit": round(total_purchase / total_units, 0) if total_units else 0,
            "weighted_avg_levered_irr": round(weighted_irr, 4) if weighted_irr is not None else None,
            "weighted_avg_levered_em": round(weighted_em, 2) if weighted_em is not None else None,
            "metros": list(set(d.metro for d in self.deals)),
            "vintage_years": sorted(set(d.vintage_year for d in self.deals)),
        }

    def group_by_metro(self) -> Dict[str, Dict[str, Any]]:
        """Group deals by metro and compute per-metro summaries."""
        metros: Dict[str, List[DealSnapshot]] = {}
        for d in self.deals:
            metros.setdefault(d.metro, []).append(d)

        result = {}
        for metro, deals in sorted(metros.items()):
            total_units = sum(d.units for d in deals)
            total_equity = sum(d.total_equity for d in deals)
            total_purchase = sum(d.purchase_price for d in deals)

            weighted = _equity_weighted_metrics([
                {"metrics": {"levered_irr": d.levered_irr}, "equity": d.total_equity}
                for d in deals
            ])
            weighted_irr = weighted["weighted_levered_irr"]

            result[metro] = {
                "deal_count": len(deals),
                "total_units": total_units,
                "total_purchase_price": round(total_purchase, 0),
                "total_equity": round(total_equity, 0),
                "weighted_avg_levered_irr": round(weighted_irr, 4) if weighted_irr is not None else None,
                "deal_ids": [d.deal_id for d in deals],
            }

        return result

    def group_by_vintage(self) -> Dict[int, Dict[str, Any]]:
        """Group deals by acquisition vintage year."""
        vintages: Dict[int, List[DealSnapshot]] = {}
        for d in self.deals:
            vintages.setdefault(d.vintage_year, []).append(d)

        result = {}
        for year, deals in sorted(vintages.items()):
            total_units = sum(d.units for d in deals)
            total_purchase = sum(d.purchase_price for d in deals)

            result[year] = {
                "deal_count": len(deals),
                "total_units": total_units,
                "total_purchase_price": round(total_purchase, 0),
                "deal_ids": [d.deal_id for d in deals],
            }

        return result

    def deal_comparison_matrix(self) -> List[Dict[str, Any]]:
        """Generate a deal comparison matrix (one row per deal).

        Returns list of dicts suitable for table display or DataFrame conversion.
        """
        rows = []
        for d in self.deals:
            rows.append({
                "deal_id": d.deal_id,
                "metro": d.metro,
                "units": d.units,
                "purchase_price": d.purchase_price,
                "price_per_unit": round(d.price_per_unit, 0),
                "going_in_cap": d.going_in_cap,
                "exit_cap": d.exit_cap,
                "noi_year_1": d.noi_year_1,
                "levered_irr": d.levered_irr,
                "unlevered_irr": d.unlevered_irr,
                "levered_em": d.levered_em,
                "unlevered_em": d.unlevered_em,
                "partnership_irr": d.partnership_irr,
                "average_dscr": d.average_dscr,
                "total_equity": d.total_equity,
                "vintage_year": d.vintage_year,
                "hold_start": d.hold_start,
                "hold_end": d.hold_end,
            })
        return rows

    def top_deals(self, metric: str = "levered_irr", n: int = 5) -> List[DealSnapshot]:
        """Return the top N deals by a given metric (descending)."""
        scored = [(d, getattr(d, metric, None)) for d in self.deals]
        scored = [(d, v) for d, v in scored if v is not None]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [d for d, _ in scored[:n]]

    def stress_test_cap_rate(
        self,
        shocks: List[int] | None = None,
    ) -> Dict[str, Any]:
        """Run cap rate sensitivity: re-run engine with exit cap shocked by N bps.

        Args:
            shocks: List of basis point shocks to apply (default [25, 50, 75, 100])

        Returns:
            Dict with per-deal base/shocked metrics and portfolio-level aggregation.
        """
        if shocks is None:
            shocks = [25, 50, 75, 100]

        # Compute base metrics (serial — fast, one per deal)
        base_metrics_list = []
        for inputs in self._deal_inputs:
            base_results = run_underwriting(inputs, skip_validation=True)
            base_metrics_list.append(_extract_stress_metrics(base_results))

        # Build parallel work items
        work_items = []
        for deal_idx, inputs in enumerate(self._deal_inputs):
            current_cap = inputs.get("exit_assumptions", {}).get("exit_cap_rate", 0.05)
            for shock_bps in shocks:
                cap_delta = shock_bps / 10_000
                overrides = {"exit_assumptions.exit_cap_rate": current_cap + cap_delta}
                work_items.append((inputs, overrides, deal_idx, str(shock_bps)))

        # Run in parallel
        shocked_results = {}
        with ProcessPoolExecutor() as executor:
            for deal_idx, shock_key, metrics in executor.map(
                _run_single_stress, work_items
            ):
                shocked_results.setdefault(deal_idx, {})[shock_key] = metrics

        # Assemble results
        deal_results = []
        for deal_idx, snap in enumerate(self.deals):
            deal_results.append({
                "deal_id": snap.deal_id,
                "metro": snap.metro,
                "base": base_metrics_list[deal_idx],
                "shocked": shocked_results.get(deal_idx, {}),
            })

        # Portfolio aggregation
        base_agg = _equity_weighted_metrics([
            {"metrics": d["base"], "equity": snap.total_equity}
            for d, snap in zip(deal_results, self.deals)
        ])
        shocked_agg = {}
        for shock_bps in shocks:
            key = str(shock_bps)
            shocked_agg[key] = _equity_weighted_metrics([
                {"metrics": d["shocked"][key], "equity": snap.total_equity}
                for d, snap in zip(deal_results, self.deals)
            ])

        return {
            "shocks_bps": shocks,
            "deals": deal_results,
            "portfolio": {
                "base": base_agg,
                "shocked": shocked_agg,
            },
        }

    def stress_test_rates(
        self,
        shocks: List[int] | None = None,
    ) -> Dict[str, Any]:
        """Run rate shock sensitivity: re-run engine with rate shocked by N bps.

        For capital_stack deals, shocks all senior/mezzanine layer rates but NOT
        preferred equity pref_return_rate. For debt_terms deals, shocks the rate.

        Args:
            shocks: List of basis point shocks to apply (default [100, 200, 300])

        Returns:
            Dict with per-deal base/shocked metrics and portfolio-level aggregation.
        """
        if shocks is None:
            shocks = [100, 200, 300]

        # Compute base metrics (serial — fast, one per deal)
        base_metrics_list = []
        for inputs in self._deal_inputs:
            base_results = run_underwriting(inputs, skip_validation=True)
            base_metrics_list.append(_extract_stress_metrics(base_results))

        # Build parallel work items and track no-debt deals
        work_items = []
        # Maps (deal_idx, shock_key) -> base_metrics for no-debt deals
        no_debt_results: Dict[tuple, Dict[str, Any]] = {}

        for deal_idx, inputs in enumerate(self._deal_inputs):
            for shock_bps in shocks:
                rate_delta = shock_bps / 10_000
                overrides = {}
                shock_key = str(shock_bps)

                if "capital_stack" in inputs:
                    for i, layer in enumerate(inputs["capital_stack"]):
                        if layer.get("layer_type") in ("senior", "mezzanine"):
                            if layer.get("rate_type") == "variable":
                                current_spread = layer.get("base_spread", 0)
                                overrides[f"capital_stack.{i}.base_spread"] = current_spread + rate_delta
                            else:
                                current_rate = layer.get("rate", 0)
                                overrides[f"capital_stack.{i}.rate"] = current_rate + rate_delta
                elif "debt_terms" in inputs:
                    if inputs["debt_terms"].get("rate_type") == "variable":
                        current_spread = inputs["debt_terms"].get("base_spread", 0)
                        overrides["debt_terms.base_spread"] = current_spread + rate_delta
                    else:
                        current_rate = inputs["debt_terms"].get("rate", 0)
                        overrides["debt_terms.rate"] = current_rate + rate_delta

                if overrides:
                    work_items.append((inputs, overrides, deal_idx, shock_key))
                else:
                    # No debt — metrics unchanged; store base for assembly
                    no_debt_results[(deal_idx, shock_key)] = base_metrics_list[deal_idx]

        # Run shocked items in parallel
        shocked_results: Dict[int, Dict[str, Any]] = {}
        if work_items:
            with ProcessPoolExecutor() as executor:
                for deal_idx, shock_key, metrics in executor.map(
                    _run_single_stress, work_items
                ):
                    shocked_results.setdefault(deal_idx, {})[shock_key] = metrics

        # Merge no-debt results into shocked_results
        for (deal_idx, shock_key), metrics in no_debt_results.items():
            shocked_results.setdefault(deal_idx, {})[shock_key] = metrics

        # Assemble results
        deal_results = []
        for deal_idx, snap in enumerate(self.deals):
            deal_results.append({
                "deal_id": snap.deal_id,
                "metro": snap.metro,
                "base": base_metrics_list[deal_idx],
                "shocked": shocked_results.get(deal_idx, {}),
            })

        # Portfolio aggregation
        base_agg = _equity_weighted_metrics([
            {"metrics": d["base"], "equity": snap.total_equity}
            for d, snap in zip(deal_results, self.deals)
        ])
        shocked_agg = {}
        for shock_bps in shocks:
            key = str(shock_bps)
            shocked_agg[key] = _equity_weighted_metrics([
                {"metrics": d["shocked"][key], "equity": snap.total_equity}
                for d, snap in zip(deal_results, self.deals)
            ])

        return {
            "shocks_bps": shocks,
            "deals": deal_results,
            "portfolio": {
                "base": base_agg,
                "shocked": shocked_agg,
            },
        }

    def concentration_risk(
        self,
        thresholds: Dict[str, float] | None = None,
        weight_by: str = "equity",
    ) -> Dict[str, Any]:
        """Analyze concentration risk across portfolio dimensions.

        No engine re-run — pure analysis on existing deal snapshots.

        Args:
            thresholds: Max acceptable share per group per dimension.
                        Keys: 'metro', 'vintage'. Default: {'metro': 0.5, 'vintage': 0.4}
            weight_by: Weighting method — 'equity' (default), 'purchase_price', or 'units'

        Returns:
            Dict with concentrations, breaches, and thresholds per dimension.
        """
        if thresholds is None:
            thresholds = {"metro": 0.5, "vintage": 0.4}

        def _get_weight(snap: DealSnapshot) -> float:
            if weight_by == "equity":
                return snap.total_equity
            elif weight_by == "purchase_price":
                return snap.purchase_price
            elif weight_by == "units":
                return float(snap.units)
            return snap.total_equity

        total_weight = sum(_get_weight(d) for d in self.deals)
        if total_weight == 0:
            return {"weight_by": weight_by, "dimensions": {}}

        dimensions = {}

        if "metro" in thresholds:
            groups: Dict[str, float] = {}
            for d in self.deals:
                groups[d.metro] = groups.get(d.metro, 0) + _get_weight(d)
            concentrations = {k: round(v / total_weight, 4) for k, v in sorted(groups.items())}
            threshold = thresholds["metro"]
            breaches = [
                {"group": k, "share": v, "threshold": threshold}
                for k, v in concentrations.items() if v > threshold
            ]
            dimensions["metro"] = {
                "concentrations": concentrations,
                "breaches": breaches,
                "threshold": threshold,
            }

        if "vintage" in thresholds:
            groups = {}
            for d in self.deals:
                year_key = str(d.vintage_year)
                groups[year_key] = groups.get(year_key, 0) + _get_weight(d)
            concentrations = {k: round(v / total_weight, 4) for k, v in sorted(groups.items())}
            threshold = thresholds["vintage"]
            breaches = [
                {"group": k, "share": v, "threshold": threshold}
                for k, v in concentrations.items() if v > threshold
            ]
            dimensions["vintage"] = {
                "concentrations": concentrations,
                "breaches": breaches,
                "threshold": threshold,
            }

        return {
            "weight_by": weight_by,
            "dimensions": dimensions,
        }

    def merged_irr(self) -> Dict[str, Any]:
        """Compute fund-level IRR from merged monthly cashflows across all deals.

        Builds a single monthly CF series spanning the earliest deal start to the
        latest deal exit (union timeline). Each deal contributes its levered monthly
        CFs at their real calendar months.

        Returns:
            Dict with monthly_irr, annual_irr, start/end months, deal_count, total_months.
        """
        if not self.deals:
            return {"annual_irr": None, "deal_count": 0}

        # Re-run each deal to get monthly cashflows
        deal_monthly_cfs = []
        for inputs in self._deal_inputs:
            results = run_underwriting(inputs, skip_validation=True)
            by_month = results.get("cashflow", {}).get("by_month", [])
            start = inputs["time_grid"]["analysis_start_date"]
            deal_monthly_cfs.append((start, by_month))

        # Determine union timeline bounds
        all_starts = []
        all_ends = []
        for snap in self.deals:
            all_starts.append(snap.hold_start)
            all_ends.append(snap.hold_end)

        start_month = min(all_starts)
        end_month = max(all_ends)

        # Build month index
        def _parse_ym(s: str) -> tuple:
            parts = s.split("-")
            return int(parts[0]), int(parts[1])

        def _month_offset(base_y: int, base_m: int, y: int, m: int) -> int:
            return (y - base_y) * 12 + (m - base_m)

        base_y, base_m = _parse_ym(start_month)
        end_y, end_m = _parse_ym(end_month)
        total_months = _month_offset(base_y, base_m, end_y, end_m) + 1

        # Merge cashflows into single series
        merged = [0.0] * (total_months + 1)  # +1 for Day 0 equity at position 0

        for (deal_start, by_month), snap in zip(deal_monthly_cfs, self.deals):
            dy, dm = _parse_ym(deal_start)

            # Day 0: equity outflow (negative)
            day0_offset = _month_offset(base_y, base_m, dy, dm)
            merged[day0_offset] += -snap.total_equity

            # Monthly levered CFs
            for month_data in by_month:
                month_str = month_data.get("month", "")
                if not month_str:
                    continue
                my, mm = _parse_ym(month_str)
                offset = _month_offset(base_y, base_m, my, mm)
                if 0 <= offset < len(merged):
                    lcf = month_data.get("leveraged_cash_flow", 0)
                    merged[offset] += lcf

        # Compute IRR on merged series
        monthly_irr = _compute_irr_from_cashflows(merged)
        annual_irr = (1 + monthly_irr) ** 12 - 1 if monthly_irr is not None else None

        return {
            "monthly_irr": round(monthly_irr, 6) if monthly_irr is not None else None,
            "annual_irr": round(annual_irr, 4) if annual_irr is not None else None,
            "start_month": start_month,
            "end_month": end_month,
            "deal_count": len(self.deals),
            "total_months": total_months,
        }
