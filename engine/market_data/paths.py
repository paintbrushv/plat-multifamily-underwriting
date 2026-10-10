"""Where the sibling feeder repositories live.

Both roots honor an environment variable and otherwise default to a sibling
directory next to this repository's checkout:

* ``PLAT_MARKET_STUDY_ROOT`` -> ``../plat-market-study-agent`` (falls back to
  the legacy ``../market-study-agent`` only when that one exists and the new
  name does not).
* ``PLAT_SUPPLY_DEMAND_ROOT`` -> ``../plat-supply-demand``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

MARKET_STUDY_ENV = "PLAT_MARKET_STUDY_ROOT"
SUPPLY_DEMAND_ENV = "PLAT_SUPPLY_DEMAND_ROOT"

# engine/market_data/paths.py -> repository root -> its parent directory.
_SIBLINGS = Path(__file__).resolve().parents[2].parent


def market_study_root(
    env: Mapping[str, str] | None = None, siblings: Path | None = None
) -> Path:
    env = os.environ if env is None else env
    override = env.get(MARKET_STUDY_ENV)
    if override:
        return Path(override).expanduser()
    base = _SIBLINGS if siblings is None else siblings
    preferred = base / "plat-market-study-agent"
    legacy = base / "market-study-agent"
    if not preferred.exists() and legacy.exists():
        return legacy
    return preferred


def supply_demand_root(
    env: Mapping[str, str] | None = None, siblings: Path | None = None
) -> Path:
    env = os.environ if env is None else env
    override = env.get(SUPPLY_DEMAND_ENV)
    if override:
        return Path(override).expanduser()
    base = _SIBLINGS if siblings is None else siblings
    return base / "plat-supply-demand"
