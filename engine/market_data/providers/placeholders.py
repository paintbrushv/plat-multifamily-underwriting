"""Placeholders for licensed data sources.

Each implements the provider protocol and reports ``available() == False``
with a clear ``unavailable_reason``. They exist so provider ordering and
configuration can name them today; a licensed adapter replaces the body
later. They never raise and ``get`` always returns ``None``.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Mapping

from engine.market_data.types import Geo, Observation


class _LicensedPlaceholder:
    name = "placeholder"
    display_name = "licensed source"
    config_env = ""

    def __init__(self, credentials: str | None = None, env: Mapping[str, str] | None = None) -> None:
        env = os.environ if env is None else env
        self.credentials = credentials or env.get(self.config_env) or None

    @property
    def configured(self) -> bool:
        return bool(self.credentials)

    @property
    def unavailable_reason(self) -> str:
        if not self.configured:
            return (
                f"{self.display_name} is not configured (set {self.config_env}); "
                "it is a licensed source with no bundled data"
            )
        return (
            f"{self.display_name} is configured but no adapter is implemented yet; "
            "this placeholder returns no data"
        )

    def available(self) -> bool:
        return False

    def get(self, metric: str, geo: Geo, *, as_of: date | None = None) -> Observation | None:
        return None


class CoStarProvider(_LicensedPlaceholder):
    name = "costar"
    display_name = "CoStar"
    config_env = "PLAT_COSTAR_CREDENTIALS"


class YardiMatrixProvider(_LicensedPlaceholder):
    name = "yardi_matrix"
    display_name = "Yardi Matrix"
    config_env = "PLAT_YARDI_MATRIX_CREDENTIALS"


class RealPageProvider(_LicensedPlaceholder):
    name = "realpage"
    display_name = "RealPage"
    config_env = "PLAT_REALPAGE_CREDENTIALS"
