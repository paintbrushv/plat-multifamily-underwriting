"""Small shared helpers: quarter labels, slugs, star tiers, hashing, optional deps."""

from __future__ import annotations

import calendar
import hashlib
import importlib.util
import re
from datetime import date
from pathlib import Path
from typing import Iterable

_QUARTER_RE = re.compile(r"^\s*(\d{4})\s*[-_ ]?\s*[Qq]\s*([1-4])\b")


def parse_quarter(label: object) -> tuple[int, int] | None:
    """Parse ``2026Q2``, ``2026-Q2``, ``2026 Q2`` or ``2026 Q2 EST`` to ``(2026, 2)``."""
    if label is None:
        return None
    match = _QUARTER_RE.match(str(label))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def quarter_label(year: int, quarter: int) -> str:
    return f"{year}Q{quarter}"


def canonical_quarter(label: object) -> str | None:
    parsed = parse_quarter(label)
    return quarter_label(*parsed) if parsed else None


def quarter_start(year: int, quarter: int) -> date:
    return date(year, 3 * quarter - 2, 1)


def quarter_end(year: int, quarter: int) -> date:
    month = 3 * quarter
    return date(year, month, calendar.monthrange(year, month)[1])


def quarter_of(day: date) -> tuple[int, int]:
    return day.year, (day.month - 1) // 3 + 1


def slugify(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


# CoStar star-rating slices as written by the market-study-agent transform.
STAR_TIER_LABELS = ("1 & 2 Star", "3 Star", "4 & 5 Star", "All")


def tier_slug(label: str) -> str:
    """Same rule as the feeder's ``ForecastContext.normalize_tier_slug``."""
    s = label.lower().replace("&", "")
    s = re.sub(r"\s+", "_", s.strip())
    s = re.sub(r"_+", "_", s)
    return s.strip("_")


_TIER_BY_SLUG = {tier_slug(label): label for label in STAR_TIER_LABELS}


def tier_label(value: str | None) -> str:
    """Normalize a star tier (label or slug) to its CoStar label; default ``All``."""
    if not value:
        return "All"
    text = str(value).strip()
    if text in STAR_TIER_LABELS:
        return text
    return _TIER_BY_SLUG.get(tier_slug(text), text)


def sha256_files(paths: Iterable[Path]) -> str:
    """Hash file names and bytes in a fixed (sorted) order."""
    digest = hashlib.sha256()
    for path in sorted(Path(p) for p in paths):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def parquet_support() -> tuple[bool, str | None]:
    """(ok, reason) for the optional ``market`` extra (pandas + pyarrow)."""
    missing = [name for name in ("pandas", "pyarrow") if importlib.util.find_spec(name) is None]
    if missing:
        return False, (
            f"missing optional dependency {', '.join(missing)}; "
            "install plat-multifamily-underwriting[market]"
        )
    return True, None


def finite(value: object) -> float | None:
    """Float or None for missing / NaN / non-numeric values."""
    if value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number
