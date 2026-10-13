"""Where the feeder repositories live.

Each feeder root is looked up in this order (the first that names a root wins):

1. The environment: ``PLAT_MARKET_STUDY_ROOT`` / ``PLAT_SUPPLY_DEMAND_ROOT``.
2. The user config file, ``[market_data]`` table, keys ``market_study_root`` /
   ``supply_demand_root``. The file is ``$PLAT_UNDERWRITING_CONFIG`` if set,
   else ``$XDG_CONFIG_HOME/plat-underwriting/config.toml``, else
   ``~/.config/plat-underwriting/config.toml``. Relative paths in it resolve
   against the file's directory.
3. A sibling of this checkout: ``../plat-market-study-agent`` (the legacy
   ``../market-study-agent`` only when it exists and the new name does not)
   and ``../plat-supply-demand``.
4. When this checkout is a git worktree, a sibling of the main checkout (a
   worktree under ``<repo>/.claude/worktrees/<name>`` has no feeder siblings
   of its own).

If no candidate exists, the first sibling path is returned so the caller can
report it as missing. :func:`feeder_root` also says where the root came from.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

MARKET_STUDY_ENV = "PLAT_MARKET_STUDY_ROOT"
SUPPLY_DEMAND_ENV = "PLAT_SUPPLY_DEMAND_ROOT"
CONFIG_ENV = "PLAT_UNDERWRITING_CONFIG"
CONFIG_SECTION = "market_data"

# engine/market_data/paths.py -> repository root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SIBLINGS = _REPO_ROOT.parent

# kind -> (env var, config key, sibling names in preference order)
_FEEDERS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "market_study": (MARKET_STUDY_ENV, "market_study_root", ("plat-market-study-agent", "market-study-agent")),
    "supply_demand": (SUPPLY_DEMAND_ENV, "supply_demand_root", ("plat-supply-demand",)),
}


@dataclass(frozen=True)
class FeederRoot:
    """A feeder root and where it came from (``origin``), e.g. ``env PLAT_MARKET_STUDY_ROOT``."""

    path: Path
    origin: str

    @property
    def exists(self) -> bool:
        return self.path.is_dir()


@dataclass(frozen=True)
class UserConfig:
    """The user config file: its path, whether it exists, its ``[market_data]`` table, any read error."""

    path: Path | None
    exists: bool
    market_data: Mapping[str, Any]
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path) if self.path else None,
            "exists": self.exists,
            "market_data": dict(self.market_data),
            "error": self.error,
        }


def user_config_path(env: Mapping[str, str] | None = None) -> Path | None:
    """The config file path for ``env`` (None when neither HOME nor an override is set)."""
    env = os.environ if env is None else env
    if env.get(CONFIG_ENV):
        return Path(env[CONFIG_ENV]).expanduser()
    if env.get("XDG_CONFIG_HOME"):
        return Path(env["XDG_CONFIG_HOME"]).expanduser() / "plat-underwriting" / "config.toml"
    if env.get("HOME"):
        return Path(env["HOME"]) / ".config" / "plat-underwriting" / "config.toml"
    return None


def _load_toml(text: str) -> dict[str, Any]:
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10
        import tomli as tomllib  # type: ignore[no-redef]
    return tomllib.loads(text)


def load_user_config(env: Mapping[str, str] | None = None) -> UserConfig:
    """Read the ``[market_data]`` table of the user config file. Never raises."""
    path = user_config_path(env)
    if path is None or not path.is_file():
        return UserConfig(path, False, {})
    try:
        data = _load_toml(path.read_text(encoding="utf-8"))
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10 without tomli
        return UserConfig(path, True, {}, "reading the config needs Python 3.11+ or the tomli package")
    except Exception as exc:  # noqa: BLE001 - a bad config is reported, not raised
        return UserConfig(path, True, {}, f"unreadable config: {type(exc).__name__}: {exc}")
    section = data.get(CONFIG_SECTION)
    if section is None:
        return UserConfig(path, True, {})
    if not isinstance(section, Mapping):
        return UserConfig(path, True, {}, f"[{CONFIG_SECTION}] must be a table")
    out = dict(section)
    for key in ("market_study_root", "supply_demand_root", "user_file"):
        if isinstance(out.get(key), str) and out[key]:
            out[key] = str((path.parent / Path(out[key]).expanduser()).resolve())
    if isinstance(out.get("comps_paths"), list):
        out["comps_paths"] = [str((path.parent / Path(str(p)).expanduser()).resolve()) for p in out["comps_paths"]]
    return UserConfig(path, True, out)


def main_checkout(repo_root: Path) -> Path | None:
    """The main checkout of a git worktree at ``repo_root`` (None when it is not one)."""
    marker = repo_root / ".git"
    if not marker.is_file():
        return None
    try:
        text = marker.read_text(encoding="utf-8").strip()
        if not text.startswith("gitdir:"):
            return None
        gitdir = Path(text[len("gitdir:"):].strip())
        if not gitdir.is_absolute():
            gitdir = (repo_root / gitdir).resolve()
        commondir_file = gitdir / "commondir"
        common = gitdir
        if commondir_file.is_file():
            common = (gitdir / commondir_file.read_text(encoding="utf-8").strip()).resolve()
        return common.parent if common.name == ".git" else None
    except OSError:
        return None


def _sibling_bases(siblings: Path | None, repo_root: Path) -> list[tuple[Path, str]]:
    if siblings is not None:
        return [(siblings, "sibling")]
    bases = [(repo_root.parent, "sibling of this checkout")]
    main = main_checkout(repo_root)
    if main is not None and main.parent != repo_root.parent:
        bases.append((main.parent, f"sibling of the main checkout {main}"))
    return bases


def feeder_root(
    kind: str,
    env: Mapping[str, str] | None = None,
    siblings: Path | None = None,
    *,
    user_config: UserConfig | None = None,
    repo_root: Path | None = None,
) -> FeederRoot:
    """Resolve a feeder root (``kind`` is ``market_study`` or ``supply_demand``)."""
    env_name, config_key, names = _FEEDERS[kind]
    env = os.environ if env is None else env
    override = env.get(env_name)
    if override:
        return FeederRoot(Path(override).expanduser(), f"env {env_name}")
    config = load_user_config(env) if user_config is None else user_config
    configured = config.market_data.get(config_key)
    if isinstance(configured, str) and configured:
        return FeederRoot(Path(configured), f"config {config.path} [{CONFIG_SECTION}] {config_key}")
    bases = _sibling_bases(siblings, _REPO_ROOT if repo_root is None else repo_root)
    for base, origin in bases:
        for name in names:
            if (base / name).is_dir():
                return FeederRoot(base / name, origin)
    base, origin = bases[0]
    return FeederRoot(base / names[0], f"{origin} (default; not found)")


def market_study_root(env: Mapping[str, str] | None = None, siblings: Path | None = None) -> Path:
    return feeder_root("market_study", env, siblings).path


def supply_demand_root(env: Mapping[str, str] | None = None, siblings: Path | None = None) -> Path:
    return feeder_root("supply_demand", env, siblings).path
