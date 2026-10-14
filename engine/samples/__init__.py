"""Synthetic sample deals that ship with the package.

``sample_deal/`` holds Sample Garden Apartments (synthetic): a rent roll, a
T12 and ``profile.json`` (the deal facts, keyed by ``runs/ingest_deal.py``
flag names, and the pricing inputs). ``sample_deal/build_sample.py``
regenerates all three. See ``docs/USER_GUIDE.md``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SAMPLE_DEAL_DIR = Path(__file__).resolve().parent / "sample_deal"


def load_sample_profile() -> dict[str, Any]:
    """The sample deal's ``profile.json``."""
    return json.loads((SAMPLE_DEAL_DIR / "profile.json").read_text(encoding="utf-8"))


def sample_document_paths() -> dict[str, Path]:
    """Absolute paths of the sample documents, keyed ``rent_roll`` and ``t12``."""
    documents = load_sample_profile()["documents"]
    return {kind: SAMPLE_DEAL_DIR / name for kind, name in documents.items()}


def sample_ingest_argv(output: Path | str) -> list[str]:
    """``runs/ingest_deal.py`` arguments that ingest the sample deal into ``output``."""
    from engine.samples.sample_deal.build_sample import ingest_argv

    return ingest_argv(load_sample_profile(), SAMPLE_DEAL_DIR, Path(output))


__all__ = ["SAMPLE_DEAL_DIR", "load_sample_profile", "sample_document_paths", "sample_ingest_argv"]
