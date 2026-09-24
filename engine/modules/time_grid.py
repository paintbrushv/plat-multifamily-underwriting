from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import List

from engine.modules.util import parse_month


@dataclass(frozen=True)
class TimeGrid:
    start: date
    end: date
    month_starts: List[date]
    month_ids: List[str]
    year_ids: List[str]

    @staticmethod
    def build(analysis_start: str, analysis_end: str) -> "TimeGrid":
        start = parse_month(analysis_start)
        end = parse_month(analysis_end)
        if end < start:
            raise ValueError("analysis_end_date must be >= analysis_start_date")

        month_starts: List[date] = []
        current = start
        while current <= end:
            month_starts.append(current)
            year = current.year
            month = current.month
            if month == 12:
                current = date(year + 1, 1, 1)
            else:
                current = date(year, month + 1, 1)

        month_ids = [f"{d.year:04d}-{d.month:02d}" for d in month_starts]
        year_ids = sorted({str(d.year) for d in month_starts})
        return TimeGrid(start=start, end=end, month_starts=month_starts, month_ids=month_ids, year_ids=year_ids)

