"""Tests for BlobSnapshotStore and business day counter."""

import json
import pytest
from unittest.mock import MagicMock, patch
from datetime import date


class TestBlobSnapshotStore:
    def test_blob_snapshot_roundtrip(self):
        """Save and load snapshot through BlobSnapshotStore."""
        from engine.portfolio_snapshot import BlobSnapshotStore

        mock_container = MagicMock()
        store = BlobSnapshotStore(mock_container)

        portfolio = MagicMock()
        portfolio.summary.return_value = {"deal_count": 5, "total_units": 500}
        portfolio.deal_comparison_matrix.return_value = [{"deal_id": "test"}]
        portfolio.group_by_metro.return_value = {"DFW": {"deals": 3}}
        portfolio.group_by_vintage.return_value = {2024: {"deals": 2}}

        blob_path = store.save(portfolio, 2026, 4)
        assert blob_path == "portfolio_2026-04.json"
        mock_container.upload_blob.assert_called_once()

        call_args = mock_container.upload_blob.call_args
        uploaded_name = call_args[0][0]
        uploaded_data = call_args[0][1]
        assert uploaded_name == "portfolio_2026-04.json"

        mock_blob = MagicMock()
        mock_blob.readall.return_value = uploaded_data.encode() if isinstance(uploaded_data, str) else uploaded_data
        mock_container.download_blob.return_value = mock_blob

        loaded = store.load(2026, 4)
        assert loaded is not None
        assert loaded["period"] == "2026-04"
        assert loaded["summary"]["deal_count"] == 5


class TestCountBusinessDays:
    def test_business_day_counter(self):
        """5th business day detection across different month starts."""
        from engine.portfolio_snapshot import count_business_days

        # April 2026 starts on Wednesday
        assert count_business_days(2026, 4, 1) == 1
        assert count_business_days(2026, 4, 3) == 3
        assert count_business_days(2026, 4, 4) == 3  # Saturday
        assert count_business_days(2026, 4, 5) == 3  # Sunday
        assert count_business_days(2026, 4, 6) == 4
        assert count_business_days(2026, 4, 7) == 5  # 5th business day

    def test_month_starting_monday(self):
        """Month starting Monday: 5th business day = Friday the 5th."""
        from engine.portfolio_snapshot import count_business_days
        # June 2026 starts on Monday
        assert count_business_days(2026, 6, 5) == 5

    def test_month_starting_saturday(self):
        """Month starting Saturday: 5th business day = Friday the 7th."""
        from engine.portfolio_snapshot import count_business_days
        # August 2026 starts on Saturday
        assert count_business_days(2026, 8, 1) == 0  # Saturday
        assert count_business_days(2026, 8, 7) == 5  # Friday
