"""Ordered provider registry (ADR 0003 section 7).

``resolve`` asks every available provider, in order, for a metric. The first
non-``None`` observation wins and every provider's observation is returned as
a candidate (in provider order, winner first) so a resolver can show the
alternatives. Providers that are unavailable, return ``None`` or raise are
skipped; ``resolve`` never raises for missing data.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any, Callable, Iterable, Mapping

from engine.market_data.metrics import require_metric
from engine.market_data.providers import (
    CoStarProvider,
    MarketDataProvider,
    PlatMarketStudyAgentProvider,
    PlatSupplyDemandProvider,
    RealPageProvider,
    UserFileProvider,
    YardiMatrixProvider,
)
from engine.market_data.types import Geo, Observation

PROVIDERS_ENV = "PLAT_MARKET_DATA_PROVIDERS"

# User-configured sources first (included by default only once configured),
# then the open feeders.
USER_CONFIGURED = ("user_file", "costar", "yardi_matrix", "realpage")
FEEDERS = ("plat_market_study_agent", "plat_supply_demand")


class MarketDataRegistry:
    def __init__(self, providers: Iterable[MarketDataProvider]) -> None:
        self.providers: tuple[MarketDataProvider, ...] = tuple(providers)
        names = [p.name for p in self.providers]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(f"duplicate provider names: {duplicates}")

    @property
    def names(self) -> list[str]:
        return [p.name for p in self.providers]

    def resolve(
        self, metric: str, geo: Geo, as_of: date | None = None
    ) -> tuple[Observation | None, list[Observation]]:
        require_metric(metric)
        candidates: list[Observation] = []
        for provider in self.providers:
            try:
                if not provider.available():
                    continue
                observation = provider.get(metric, geo, as_of=as_of)
            except Exception:  # noqa: BLE001 - one bad provider must not block the rest
                continue
            if observation is not None:
                candidates.append(observation)
        return (candidates[0] if candidates else None), candidates

    def status(self) -> list[dict[str, Any]]:
        """Availability of each provider, in order (for diagnostics/UI)."""
        out = []
        for provider in self.providers:
            try:
                ok = bool(provider.available())
            except Exception as exc:  # noqa: BLE001
                ok, reason = False, f"available() raised {type(exc).__name__}"
            else:
                reason = None if ok else getattr(provider, "unavailable_reason", None)
            out.append({"name": provider.name, "available": ok, "unavailable_reason": reason})
        return out


def default_registry(
    config: Mapping[str, Any] | None = None, env: Mapping[str, str] | None = None
) -> MarketDataRegistry:
    """Build the registry from ``config`` and the environment.

    ``config`` keys (all optional): ``providers`` (ordered provider names),
    ``market_study_root``, ``supply_demand_root``, ``comps_paths``,
    ``metro_aliases``, ``user_file``. Without ``providers`` the order comes
    from ``PLAT_MARKET_DATA_PROVIDERS`` (comma-separated names) or the default:
    every configured user/paid provider (``user_file, costar, yardi_matrix,
    realpage``), then ``plat_market_study_agent``, then ``plat_supply_demand``.
    """
    config = config or {}
    env = os.environ if env is None else env
    factories: dict[str, Callable[[], MarketDataProvider]] = {
        "user_file": lambda: UserFileProvider(config.get("user_file"), env=env),
        "costar": lambda: CoStarProvider(env=env),
        "yardi_matrix": lambda: YardiMatrixProvider(env=env),
        "realpage": lambda: RealPageProvider(env=env),
        "plat_market_study_agent": lambda: PlatMarketStudyAgentProvider(
            config.get("market_study_root"),
            comps_paths=config.get("comps_paths"),
            metro_aliases=config.get("metro_aliases"),
            env=env,
        ),
        "plat_supply_demand": lambda: PlatSupplyDemandProvider(config.get("supply_demand_root"), env=env),
    }
    order = config.get("providers")
    if order is None and env.get(PROVIDERS_ENV):
        order = [n.strip() for n in env[PROVIDERS_ENV].split(",") if n.strip()]
    if order is not None:
        unknown = [n for n in order if n not in factories]
        if unknown:
            raise ValueError(f"unknown market-data providers {unknown}; known: {sorted(factories)}")
        return MarketDataRegistry(factories[n]() for n in order)

    providers: list[MarketDataProvider] = []
    for name in USER_CONFIGURED:
        provider = factories[name]()
        if getattr(provider, "configured", False):
            providers.append(provider)
    providers.extend(factories[name]() for name in FEEDERS)
    return MarketDataRegistry(providers)
