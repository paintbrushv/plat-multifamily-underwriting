"""Where deal workspaces live and what uploads are accepted.

The workspace root (the folder holding ``deals/<deal_id>/``) is, first match wins:

1. ``$PLAT_UNDERWRITING_HOME``;
2. ``dir`` in the ``[workspace]`` table of the user config file (the same
   file as the market-data settings: ``$PLAT_UNDERWRITING_CONFIG``, else
   ``$XDG_CONFIG_HOME/plat-underwriting/config.toml``, else
   ``~/.config/plat-underwriting/config.toml``); a relative path resolves
   against the file's folder;
3. ``$XDG_DATA_HOME/plat-underwriting``, else ``~/.local/share/plat-underwriting``.

Deals are always kept outside the repository. Upload limits:

- ``$PLAT_UW_MAX_UPLOAD_MB`` or ``[workspace] max_upload_mb`` (default 25 MB);
- ``$PLAT_UW_ALLOWED_EXTENSIONS`` (a JSON object ``{kind: [".ext", ...]}``) or
  ``[workspace.allowed_extensions]`` narrow or replace the extensions allowed
  per document kind (``DEFAULT_EXTENSIONS``).

Example ``config.toml``::

    [workspace]
    dir = "~/underwriting"
    max_upload_mb = 50

    [workspace.allowed_extensions]
    t12 = [".xlsx", ".csv"]
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from engine.market_data.paths import _load_toml, user_config_path

HOME_ENV = "PLAT_UNDERWRITING_HOME"
MAX_UPLOAD_ENV = "PLAT_UW_MAX_UPLOAD_MB"
EXTENSIONS_ENV = "PLAT_UW_ALLOWED_EXTENSIONS"
CONFIG_SECTION = "workspace"
APP_DIR = "plat-underwriting"
DEALS_DIR = "deals"
DEFAULT_MAX_UPLOAD_MB = 25.0

DOCUMENT_KINDS = ("t12", "rent_roll", "om", "pca")
DEFAULT_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "t12": (".xlsx", ".xlsm", ".xls", ".csv"),
    "rent_roll": (".xlsx", ".xlsm", ".xls", ".csv"),
    "om": (".pdf", ".txt"),
    "pca": (".json", ".pdf"),
}


@dataclass(frozen=True)
class WorkflowConfig:
    """The resolved workspace settings and where each came from."""

    deals_dir: Path
    deals_dir_origin: str
    max_upload_bytes: int
    allowed_extensions: Mapping[str, tuple[str, ...]]
    config_path: Path | None = None
    errors: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "deals_dir": str(self.deals_dir),
            "deals_dir_origin": self.deals_dir_origin,
            "max_upload_mb": round(self.max_upload_bytes / (1024 * 1024), 3),
            "allowed_extensions": {k: list(v) for k, v in self.allowed_extensions.items()},
            "config_path": str(self.config_path) if self.config_path else None,
            "errors": list(self.errors),
        }


def _config_section(env: Mapping[str, str]) -> tuple[Path | None, dict[str, Any], list[str]]:
    path = user_config_path(env)
    if path is None or not path.is_file():
        return path, {}, []
    try:
        data = _load_toml(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - a bad config is reported, not raised
        return path, {}, [f"unreadable config {path}: {type(exc).__name__}: {exc}"]
    section = data.get(CONFIG_SECTION)
    if section is None:
        return path, {}, []
    if not isinstance(section, Mapping):
        return path, {}, [f"[{CONFIG_SECTION}] in {path} must be a table"]
    return path, dict(section), []


def _default_root(env: Mapping[str, str]) -> Path:
    if env.get("XDG_DATA_HOME"):
        return Path(env["XDG_DATA_HOME"]).expanduser() / APP_DIR
    home = env.get("HOME") or str(Path.home())
    return Path(home) / ".local" / "share" / APP_DIR


def _extensions(raw: Any, errors: list[str], origin: str) -> dict[str, tuple[str, ...]]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        errors.append(f"{origin} must map a document kind to a list of extensions")
        return {}
    out: dict[str, tuple[str, ...]] = {}
    for kind, values in raw.items():
        if kind not in DOCUMENT_KINDS or not isinstance(values, (list, tuple)):
            errors.append(f"{origin}: ignored {kind!r} (kinds are {', '.join(DOCUMENT_KINDS)}; values a list)")
            continue
        out[kind] = tuple(
            (v if str(v).startswith(".") else f".{v}").lower() for v in values if isinstance(v, str) and v
        )
    return out


def load_config(env: Mapping[str, str] | None = None) -> WorkflowConfig:
    """The workspace settings for ``env`` (default ``os.environ``). Never raises."""
    env = os.environ if env is None else env
    config_path, section, errors = _config_section(env)

    if env.get(HOME_ENV):
        root, origin = Path(env[HOME_ENV]).expanduser(), f"env {HOME_ENV}"
    elif isinstance(section.get("dir"), str) and section["dir"]:
        base = config_path.parent if config_path else Path.cwd()
        root = (base / Path(section["dir"]).expanduser()).resolve()
        origin = f"config {config_path} [{CONFIG_SECTION}] dir"
    else:
        root, origin = _default_root(env), "default (XDG data dir)"

    max_mb: Any = env.get(MAX_UPLOAD_ENV) or section.get("max_upload_mb") or DEFAULT_MAX_UPLOAD_MB
    try:
        max_mb = float(max_mb)
        if not max_mb > 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append(f"max upload size {max_mb!r} is not a positive number of MB; using {DEFAULT_MAX_UPLOAD_MB}")
        max_mb = DEFAULT_MAX_UPLOAD_MB

    allowed = dict(DEFAULT_EXTENSIONS)
    allowed.update(_extensions(section.get("allowed_extensions"), errors, f"[{CONFIG_SECTION}.allowed_extensions]"))
    if env.get(EXTENSIONS_ENV):
        try:
            allowed.update(_extensions(json.loads(env[EXTENSIONS_ENV]), errors, EXTENSIONS_ENV))
        except json.JSONDecodeError as exc:
            errors.append(f"{EXTENSIONS_ENV} is not JSON: {exc}")

    return WorkflowConfig(
        deals_dir=root / DEALS_DIR,
        deals_dir_origin=origin,
        max_upload_bytes=int(max_mb * 1024 * 1024),
        allowed_extensions=allowed,
        config_path=config_path,
        errors=tuple(errors),
    )
