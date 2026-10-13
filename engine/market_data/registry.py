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
from engine.market_data.paths import feeder_root, load_user_config
from engine.market_data.providers import (
    CoStarProvider,
    MarketDataProvider,
    PlatMarketStudyAgentProvider,
    PlatSupplyDemandProvider,
    RealPageProvider,
    UserFileProvider,
    YardiMatrixProvider,
)
from engine.market_data.providers.market_study import COMPS_ENV
from engine.market_data.providers.user_file import USER_FILE_ENV
from engine.market_data.types import Geo, Observation

PROVIDERS_ENV = "PLAT_MARKET_DATA_PROVIDERS"

# User-configured sources first (included by default only once configured),
# then the open feeders.
USER_CONFIGURED = ("user_file", "costar", "yardi_matrix", "realpage")
FEEDERS = ("plat_market_study_agent", "plat_supply_demand")


class MarketDataRegistry:
    def __init__(self, providers: Iterable[MarketDataProvider]) -> None:
        self.providers: tuple[MarketDataProvider, ...] = tuple(providers)
        # (provider name, metric) -> "ExcType: message" swallowed by resolve().
        self.errors: dict[tuple[str, str], str] = {}
        # Where the provider order and the feeder roots came from (default_registry).
        self.config_source: dict[str, Any] = {}
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
            except Exception as exc:  # noqa: BLE001 - one bad provider must not block the rest
                # Recorded for the market-data status block, never raised.
                self.errors[(provider.name, metric)] = f"{type(exc).__name__}: {exc}"
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
    """Build the registry from ``config``, the environment and the user config file.

    ``config`` keys (all optional): ``providers`` (ordered provider names),
    ``market_study_root``, ``supply_demand_root``, ``comps_paths``,
    ``metro_aliases``, ``user_file``. Each key falls back to its environment
    variable (``PLAT_MARKET_DATA_PROVIDERS`` comma-separated,
    ``PLAT_MARKET_STUDY_ROOT``, ``PLAT_SUPPLY_DEMAND_ROOT``,
    ``PLAT_MARKET_STUDY_COMPS``, ``PLAT_MARKET_DATA_USER_FILE``), then to the
    ``[market_data]`` table of the user config file
    (``engine.market_data.paths.load_user_config``), then to the defaults:
    sibling feeder checkouts, and the provider order every configured
    user/paid provider (``user_file, costar, yardi_matrix, realpage``), then
    ``plat_market_study_agent``, then ``plat_supply_demand``.

    ``registry.config_source`` records the user config file and where the
    provider order came from, for the market-data status block.
    """
    config = config or {}
    env = os.environ if env is None else env
    user = load_user_config(env)
    file_cfg = user.market_data

    def feeder(kind: str, key: str) -> tuple[Any, str | None]:
        if config.get(key):
            return config[key], "default_registry config"
        found = feeder_root(kind, env, user_config=user)
        return found.path, found.origin

    def comps_paths() -> Any:
        if config.get("comps_paths") is not None:
            return config["comps_paths"]
        if env.get(COMPS_ENV):
            return None  # the provider reads the environment
        return file_cfg.get("comps_paths")

    def user_file() -> Any:
        if config.get("user_file") is not None or env.get(USER_FILE_ENV):
            return config.get("user_file")
        return file_cfg.get("user_file")

    def market_study() -> MarketDataProvider:
        root, origin = feeder("market_study", "market_study_root")
        return PlatMarketStudyAgentProvider(
            root, comps_paths=comps_paths(), metro_aliases=config.get("metro_aliases"), env=env,
            root_origin=origin,
        )

    def supply_demand() -> MarketDataProvider:
        root, origin = feeder("supply_demand", "supply_demand_root")
        return PlatSupplyDemandProvider(root, env=env, root_origin=origin)

    factories: dict[str, Callable[[], MarketDataProvider]] = {
        "user_file": lambda: UserFileProvider(user_file(), env=env),
        "costar": lambda: CoStarProvider(env=env),
        "yardi_matrix": lambda: YardiMatrixProvider(env=env),
        "realpage": lambda: RealPageProvider(env=env),
        "plat_market_study_agent": market_study,
        "plat_supply_demand": supply_demand,
    }
    order = config.get("providers")
    order_origin = "default_registry config"
    if order is None and env.get(PROVIDERS_ENV):
        order = [n.strip() for n in env[PROVIDERS_ENV].split(",") if n.strip()]
        order_origin = f"env {PROVIDERS_ENV}"
    if order is None and file_cfg.get("providers") is not None:
        raw = file_cfg["providers"]
        order = [n.strip() for n in (raw.split(",") if isinstance(raw, str) else raw) if str(n).strip()]
        order_origin = f"config {user.path} [market_data] providers"
    if order is not None:
        unknown = [n for n in order if n not in factories]
        if unknown:
            raise ValueError(f"unknown market-data providers {unknown}; known: {sorted(factories)}")
        registry = MarketDataRegistry(factories[n]() for n in order)
    else:
        order_origin = "default order"
        providers: list[MarketDataProvider] = []
        for name in USER_CONFIGURED:
            provider = factories[name]()
            if getattr(provider, "configured", False):
                providers.append(provider)
        providers.extend(factories[name]() for name in FEEDERS)
        registry = MarketDataRegistry(providers)
    registry.config_source = {"user_config": user.as_dict(), "provider_order": order_origin}
    return registry
