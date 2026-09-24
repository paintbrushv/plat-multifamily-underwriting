"""
Portfolio Snapshot Persistence
===============================
Save and load monthly portfolio snapshots for period-over-period comparison.

Snapshots are JSON files stored in output/snapshots/ with naming convention:
    portfolio_YYYY-MM.json

Each snapshot contains:
- Timestamp and reporting period
- Portfolio summary metrics
- Per-deal metrics
- Metro breakdown
- Vintage breakdown
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from engine.portfolio import Portfolio


def _snapshot_filename(year: int, month: int) -> str:
    """Generate snapshot filename for a given period."""
    return f"portfolio_{year:04d}-{month:02d}.json"


def save_snapshot(
    portfolio: Portfolio,
    snapshot_dir: str | Path,
    year: Optional[int] = None,
    month: Optional[int] = None,
) -> Path:
    """Save a portfolio snapshot for the given period.

    Args:
        portfolio: Portfolio with deals loaded
        snapshot_dir: Directory to save snapshots
        year: Reporting year (default: current)
        month: Reporting month (default: current)

    Returns:
        Path to the saved snapshot file
    """
    now = datetime.now()
    year = year or now.year
    month = month or now.month

    snapshot_dir = Path(snapshot_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    summary = portfolio.summary()
    matrix = portfolio.deal_comparison_matrix()
    by_metro = portfolio.group_by_metro()
    by_vintage = portfolio.group_by_vintage()

    snapshot = {
        "period": f"{year:04d}-{month:02d}",
        "generated_at": now.isoformat(),
        "summary": summary,
        "deals": matrix,
        "by_metro": by_metro,
        "by_vintage": by_vintage,
    }

    path = snapshot_dir / _snapshot_filename(year, month)
    path.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")
    return path


def load_snapshot(snapshot_dir: str | Path, year: int, month: int) -> Optional[Dict[str, Any]]:
    """Load a specific month's snapshot."""
    path = Path(snapshot_dir) / _snapshot_filename(year, month)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_latest_snapshot(snapshot_dir: str | Path) -> Optional[Dict[str, Any]]:
    """Load the most recent snapshot (by filename sort)."""
    snapshot_dir = Path(snapshot_dir)
    if not snapshot_dir.exists():
        return None

    files = sorted(snapshot_dir.glob("portfolio_*.json"))
    if not files:
        return None

    return json.loads(files[-1].read_text(encoding="utf-8"))


def load_previous_snapshot(
    snapshot_dir: str | Path,
    current_year: int,
    current_month: int,
) -> Optional[Dict[str, Any]]:
    """Load the snapshot immediately before the given period."""
    snapshot_dir = Path(snapshot_dir)
    if not snapshot_dir.exists():
        return None

    current_name = _snapshot_filename(current_year, current_month)
    files = sorted(snapshot_dir.glob("portfolio_*.json"))
    prior = [f for f in files if f.name < current_name]

    if not prior:
        return None

    return json.loads(prior[-1].read_text(encoding="utf-8"))


def compute_period_deltas(
    current: Dict[str, Any],
    previous: Dict[str, Any],
) -> Dict[str, Any]:
    """Compute changes between two snapshots.

    Returns dict with:
        - summary_deltas: changes in portfolio-level metrics
        - new_deals: deals in current but not previous
        - removed_deals: deals in previous but not current
        - metro_changes: per-metro delta in units/equity
    """
    curr_summary = current.get("summary", {})
    prev_summary = previous.get("summary", {})

    # Summary metric deltas
    summary_deltas = {}
    numeric_keys = [
        "deal_count", "total_units", "total_purchase_price", "total_equity",
        "avg_price_per_unit", "total_noi_year_1",
    ]
    for key in numeric_keys:
        curr_val = curr_summary.get(key, 0) or 0
        prev_val = prev_summary.get(key, 0) or 0
        summary_deltas[key] = {
            "current": curr_val,
            "previous": prev_val,
            "change": curr_val - prev_val,
        }

    # IRR/EM deltas (handle None)
    for key in ["weighted_avg_levered_irr", "weighted_avg_levered_em"]:
        curr_val = curr_summary.get(key)
        prev_val = prev_summary.get(key)
        if curr_val is not None and prev_val is not None:
            summary_deltas[key] = {
                "current": curr_val,
                "previous": prev_val,
                "change": curr_val - prev_val,
            }
        else:
            summary_deltas[key] = {
                "current": curr_val,
                "previous": prev_val,
                "change": None,
            }

    # Deal-level changes
    curr_deal_ids = {d["deal_id"] for d in current.get("deals", [])}
    prev_deal_ids = {d["deal_id"] for d in previous.get("deals", [])}

    new_deals = sorted(curr_deal_ids - prev_deal_ids)
    removed_deals = sorted(prev_deal_ids - curr_deal_ids)

    # Metro changes
    curr_metros = current.get("by_metro", {})
    prev_metros = previous.get("by_metro", {})
    all_metros = sorted(set(list(curr_metros.keys()) + list(prev_metros.keys())))

    metro_changes = {}
    for metro in all_metros:
        curr_m = curr_metros.get(metro, {})
        prev_m = prev_metros.get(metro, {})
        metro_changes[metro] = {
            "deals": (curr_m.get("deal_count", 0) or 0) - (prev_m.get("deal_count", 0) or 0),
            "units": (curr_m.get("total_units", 0) or 0) - (prev_m.get("total_units", 0) or 0),
            "equity": (curr_m.get("total_equity", 0) or 0) - (prev_m.get("total_equity", 0) or 0),
        }

    return {
        "current_period": current.get("period"),
        "previous_period": previous.get("period"),
        "summary_deltas": summary_deltas,
        "new_deals": new_deals,
        "removed_deals": removed_deals,
        "metro_changes": metro_changes,
    }


from datetime import date as date_type


def count_business_days(year: int, month: int, day: int) -> int:
    """Count business days (Mon-Fri) from 1st of month through given day inclusive."""
    count = 0
    for d in range(1, day + 1):
        weekday = date_type(year, month, d).weekday()  # 0=Mon, 6=Sun
        if weekday < 5:
            count += 1
    return count


class BlobSnapshotStore:
    """Azure Blob Storage backend for portfolio snapshots."""

    def __init__(self, container_client):
        self._container = container_client

    def save(self, portfolio, year: int, month: int) -> str:
        """Save snapshot to Blob. Returns blob name."""
        from datetime import datetime, timezone as tz

        summary = portfolio.summary()
        matrix = portfolio.deal_comparison_matrix()
        by_metro = portfolio.group_by_metro()
        by_vintage = portfolio.group_by_vintage()

        snapshot = {
            "period": f"{year:04d}-{month:02d}",
            "generated_at": datetime.now(tz.utc).isoformat(),
            "summary": summary,
            "deals": matrix,
            "by_metro": by_metro,
            "by_vintage": by_vintage,
        }

        blob_name = f"portfolio_{year:04d}-{month:02d}.json"
        data = json.dumps(snapshot, indent=2, default=str)
        self._container.upload_blob(blob_name, data, overwrite=True)
        return blob_name

    def load(self, year: int, month: int):
        """Load specific month's snapshot from Blob. Returns dict or None."""
        blob_name = f"portfolio_{year:04d}-{month:02d}.json"
        try:
            blob_data = self._container.download_blob(blob_name)
            return json.loads(blob_data.readall())
        except Exception:
            return None

    def load_previous(self, current_year: int, current_month: int):
        """Load snapshot immediately before given period. Returns dict or None."""
        if current_month == 1:
            prev_year, prev_month = current_year - 1, 12
        else:
            prev_year, prev_month = current_year, current_month - 1
        return self.load(prev_year, prev_month)
