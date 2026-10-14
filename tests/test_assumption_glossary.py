"""docs/ASSUMPTION_GLOSSARY.md is generated from the code and must not go stale."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.build_glossary import GLOSSARY_PATH, render  # noqa: E402


def test_the_committed_glossary_is_current():
    assert GLOSSARY_PATH.read_text(encoding="utf-8") == render(), (
        "docs/ASSUMPTION_GLOSSARY.md is stale: run python scripts/build_glossary.py and commit it"
    )


def test_check_mode_flags_a_stale_glossary(tmp_path):
    stale = tmp_path / "glossary.md"
    stale.write_text(render() + "edited by hand\n", encoding="utf-8")
    script = REPO_ROOT / "scripts" / "build_glossary.py"
    assert subprocess.run([sys.executable, str(script), "--check", "--output", str(stale)]).returncode == 1
    stale.write_text(render(), encoding="utf-8")
    assert subprocess.run([sys.executable, str(script), "--check", "--output", str(stale)]).returncode == 0


def test_the_glossary_covers_every_key_and_every_source():
    from engine.assumptions.rationale import SOURCE_RATIONALES
    from engine.assumptions.resolver import ASSUMPTION_KEYS

    text = render()
    for key in ASSUMPTION_KEYS:
        assert f"`{key}`" in text, key
    for source in SOURCE_RATIONALES:
        assert f"`{source}`" in text, source
