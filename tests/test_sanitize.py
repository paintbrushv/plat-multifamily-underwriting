"""Public-tree sanitize gate.

Fails if any private/company identity literal leaks into the public tree:
deal and property names, the company name, tenant IDs, private host paths,
or the placeholder canary. The gate file itself is skipped (it necessarily
contains the literals it forbids).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_SELF = Path(__file__).resolve()

_FORBIDDEN = {
    # Company / tenant identity
    "upliftfunds": "company tenant hostname",
    "uplift": "company name",
    "paintbrushv": "private account handle",
    "sharepoint": "private SharePoint tenant references",
    "data/uplift": "private data root",
    # Yardi org id (filename pattern used by real underwriting workbooks)
    "-3769-": "Yardi org id in exported workbook filenames",
    # Real deal / property names
    "woodford_on_mockingbird": "real deal slug",
    "renew_at_fairmount": "real deal slug",
    "anatole_on_briarwood": "real deal slug",
    "state_at_fishers": "real deal slug",
    "the_nolan": "real deal slug",
    "university_cove": "real deal slug",
    "virtu_on_denali": "real deal slug",
    "west_oaks": "real property name",
    "chatham": "real property name",
    "park_place": "real property name",
    "blue_springs": "real property name",
    "denham": "real property name",
    "estrella": "real property name",
    "barringer": "real property name",
    "belle_mor": "real property name",
    "belle mor": "real property name",
    "marquis_at": "real property name",
    "villas_at_cantamar": "real property name",
    "300_pearl": "real property name",
    "the_carmen": "real property name",
    # Host identity and private paths
    "/home/mdai": "private host path",
    "spark-17d5": "private hostname",
    # Placeholder canary pattern (from the plat-harness release gate)
    "your-org-name": "placeholder canary",
    "00_PRIVATE_DEPARTMENT": "placeholder canary",
    "PRIVATE_SESSION_PROMPT": "placeholder canary",
}

# Word-boundary entries (avoid false positives on ordinary words that merely
# contain a forbidden substring).
_WORD_FORBIDDEN = {
    "uplift": "company name",
    "fairmount": "real property name",
    "fishers": "real property name",
    "nolan": "real property name",
    "cantamar": "real property name",
    "carmen": "real property name",
}

_GUIDE_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

_SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".pytest-tmp"}
_TEXT_SUFFIXES = {
    ".py", ".json", ".md", ".toml", ".txt", ".yml", ".yaml", ".cfg",
    ".ini", ".csv", ".html", ".css", ".js", ".sh", ".example", "",
}


def _iter_text_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        files.append(path)
    return files


def test_no_private_literals_in_tree() -> None:
    hits: list[str] = []
    for path in _iter_text_files():
        if path.resolve() == _SELF:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        lowered = text.lower()
        for snippet, why in _FORBIDDEN.items():
            if snippet in lowered:
                hits.append(f"{path.relative_to(REPO)}: {snippet!r} ({why})")
        for word, why in _WORD_FORBIDDEN.items():
            if re.search(r"\b" + re.escape(word) + r"\b", lowered):
                hits.append(f"{path.relative_to(REPO)}: {word!r} ({why})")
    assert hits == [], "private identity literals leaked:\n  " + "\n  ".join(hits)


def test_no_tenant_guids_in_cloud_context() -> None:
    """GUIDs appearing near sharepoint/azure/tenant mentions are site/tenant IDs."""
    hits: list[str] = []
    for path in _iter_text_files():
        if path.resolve() == _SELF:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if not _GUIDE_RE.search(text):
            continue
        window = text.lower()
        if any(k in window for k in ("sharepoint", "azure", "tenant", "drive_id", "site_id")):
            hits.append(str(path.relative_to(REPO)))
    assert hits == [], "GUIDs found in files mentioning cloud tenancy:\n  " + "\n  ".join(hits)
