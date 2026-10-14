"""On-disk deal workspaces: one folder per deal, outside the repository.

Layout of ``<deals_dir>/<deal_id>/``::

    deal.json          the workspace manifest (name, type, profile, documents,
                       analyst edits, step status, timestamps)
    documents/         a copy of each document, one per kind (t12.xlsx, ...)
    canonical.json     the ingested canonical deal
    assumptions.json   its assumptions sheet
    assumptions.html   its assumption register page
    run/               run_deal outputs (results.json, underwriting.xlsx, ...)
    price/             solve_price outputs (backsolve_summary.json, assumptions.html, ...)

A deal id is a lowercase slug of the deal name plus a short random suffix
(``DEAL_ID_RE``). Every id and relative path is checked before it touches the
filesystem: ids must match the pattern and every resolved path must stay
inside the deal's folder, so ``..``, absolute paths and NUL bytes are refused.
Writes are atomic (temp file, then rename).

Single-user and local by design: there is no locking across processes, which
a hosted, multi-user deployment would add with its storage layer.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

MANIFEST_NAME = "deal.json"
MANIFEST_VERSION = "plat.uw.workspace/1"
DOCUMENTS_DIR = "documents"
CANONICAL_NAME = "canonical.json"
ASSUMPTIONS_NAME = "assumptions.json"
REGISTER_NAME = "assumptions.html"
RUN_DIR = "run"
PRICE_DIR = "price"

DEAL_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
_SLUG_MAX = 40
_SUFFIX_BYTES = 3  # six hex characters


class WorkspaceError(Exception):
    """A refused or failed workspace operation, with a code and a fix for the caller."""

    def __init__(self, code: str, message: str, fix: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix


def now_iso() -> str:
    """The current UTC time, to the second (workspace timestamps only; never used in math)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:_SLUG_MAX].strip("-") or "deal"


def check_deal_id(deal_id: Any) -> str:
    if not isinstance(deal_id, str) or not DEAL_ID_RE.fullmatch(deal_id):
        raise WorkspaceError(
            "INVALID_DEAL_ID",
            f"{deal_id!r} is not a deal id",
            "Use the deal_id returned by create_deal or listed by list_deals "
            "(lowercase letters, digits and hyphens).",
        )
    return deal_id


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class Workspace:
    """The deal folders under ``deals_dir``."""

    def __init__(self, deals_dir: Path) -> None:
        self.deals_dir = Path(deals_dir)

    # ── ids and paths ────────────────────────────────────────────────────

    def new_deal_id(self, name: str) -> str:
        base = slugify(name)
        for _ in range(20):
            deal_id = f"{base}-{secrets.token_hex(_SUFFIX_BYTES)}"
            if not (self.deals_dir / deal_id).exists():
                return deal_id
        raise WorkspaceError("ID_EXHAUSTED", "could not allocate a free deal id", "Try again with another name.")

    def deal_dir(self, deal_id: str) -> Path:
        check_deal_id(deal_id)
        path = self.deals_dir / deal_id
        if path.resolve().parent != self.deals_dir.resolve():
            raise WorkspaceError("INVALID_DEAL_ID", f"{deal_id!r} is not a deal id", "Use list_deals.")
        return path

    def resolve_file(self, deal_id: str, relpath: Any) -> Path:
        """An existing path inside the deal's folder; refuses anything that would leave it."""
        folder = self.require(deal_id)
        if not isinstance(relpath, str) or not relpath or "\x00" in relpath or "\\" in relpath:
            raise WorkspaceError("INVALID_PATH", f"{relpath!r} is not a workspace path", "Use a path from export_deal.")
        rel = Path(relpath)
        if rel.is_absolute() or any(part in ("..", "") for part in rel.parts):
            raise WorkspaceError("INVALID_PATH", f"{relpath!r} is not a workspace path", "Use a path from export_deal.")
        root = folder.resolve()
        path = (root / rel).resolve()
        if root not in path.parents:
            raise WorkspaceError("INVALID_PATH", f"{relpath!r} is outside the deal", "Use a path from export_deal.")
        if not path.is_file():
            raise WorkspaceError("FILE_NOT_FOUND", f"{relpath} does not exist for {deal_id}",
                                 "Run export_deal to see the deal's files.")
        return path

    # ── manifests ────────────────────────────────────────────────────────

    def exists(self, deal_id: str) -> bool:
        return (self.deal_dir(deal_id) / MANIFEST_NAME).is_file()

    def require(self, deal_id: str) -> Path:
        folder = self.deal_dir(deal_id)
        if not (folder / MANIFEST_NAME).is_file():
            raise WorkspaceError(
                "DEAL_NOT_FOUND", f"no deal {deal_id!r} in {self.deals_dir}",
                "Call list_deals to see the deals in this workspace, or create_deal to start one.",
            )
        return folder

    def create(self, manifest: dict[str, Any]) -> Path:
        folder = self.deal_dir(manifest["deal_id"])
        (folder / DOCUMENTS_DIR).mkdir(parents=True, exist_ok=False)
        self.save(manifest)
        return folder

    def load(self, deal_id: str) -> dict[str, Any]:
        folder = self.require(deal_id)
        try:
            manifest = json.loads((folder / MANIFEST_NAME).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspaceError("MANIFEST_UNREADABLE", f"{deal_id}/deal.json is unreadable: {exc}",
                                 "Restore deal.json or create the deal again.") from None
        if not isinstance(manifest, dict) or manifest.get("deal_id") != deal_id:
            raise WorkspaceError("MANIFEST_UNREADABLE", f"{deal_id}/deal.json is not this deal's manifest",
                                 "Restore deal.json or create the deal again.")
        return manifest

    def save(self, manifest: dict[str, Any]) -> None:
        manifest["updated_at"] = now_iso()
        self.write_json(manifest["deal_id"], MANIFEST_NAME, manifest)

    def manifests(self) -> Iterator[dict[str, Any]]:
        if not self.deals_dir.is_dir():
            return
        for folder in sorted(self.deals_dir.iterdir()):
            if folder.is_dir() and DEAL_ID_RE.fullmatch(folder.name) and (folder / MANIFEST_NAME).is_file():
                try:
                    yield self.load(folder.name)
                except WorkspaceError:
                    continue

    # ── files ────────────────────────────────────────────────────────────

    def path(self, deal_id: str, relpath: str) -> Path:
        """A path inside the deal folder chosen by this module (never by a caller)."""
        return self.deal_dir(deal_id) / relpath

    def write_bytes(self, deal_id: str, relpath: str, data: bytes) -> Path:
        path = self.path(deal_id, relpath)
        _write_atomic(path, data)
        return path

    def write_json(self, deal_id: str, relpath: str, payload: Any) -> Path:
        return self.write_bytes(deal_id, relpath, (json.dumps(payload, indent=2, default=str) + "\n").encode("utf-8"))

    def read_json(self, deal_id: str, relpath: str) -> Any | None:
        path = self.path(deal_id, relpath)
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def files(self, deal_id: str) -> list[dict[str, Any]]:
        """Every file in the deal folder: relative path (POSIX), absolute path, size, mtime."""
        root = self.require(deal_id)
        out = []
        for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.name.startswith(".")):
            stat = path.stat()
            out.append({
                "path": path.relative_to(root).as_posix(),
                "local_path": str(path.resolve()),
                "bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc)
                .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            })
        return out
