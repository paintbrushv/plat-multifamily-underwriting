"""Engine sources compile without SyntaxWarning (e.g. invalid escape sequences)."""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1] / "engine"
SOURCES = sorted(ENGINE.rglob("*.py"))


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(ENGINE)))
def test_engine_source_compiles_without_syntax_warnings(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("error", SyntaxWarning)
        compile(source, str(path), "exec")


def test_om_extractor_prompt_docstring_keeps_its_text() -> None:
    from engine.ingest import om_extractor

    doc = om_extractor._build_prompt.__doc__ or ""
    assert "a 12MB\\+ production OM" in doc
