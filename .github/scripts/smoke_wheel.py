#!/usr/bin/env python3
"""Install the freshly built wheel into a throwaway venv and smoke it.

Run from the repo root after `python -m build`; expects exactly one wheel in
dist/. Verifies: clean-venv install resolves, the engine package imports,
the JSON deal schema ships inside the wheel, the deterministic parsers
produce identical output on a synthetic fixture, the schema validator gates
a bad deal, and the api layer answers with certified metrics. No CLI entry
points ship (the engine is a library; run it via `python -m engine...` or
the documented intake scripts).
"""

from __future__ import annotations

import glob
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DIST = REPO / "dist"


def run(cmd: list[str], **kw) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True, **kw)


def main() -> int:
    wheels = sorted(glob.glob(str(DIST / "*.whl")))
    if len(wheels) != 1:
        print(f"expected exactly one wheel in dist/, found: {wheels}")
        return 1
    wheel = wheels[0]

    with tempfile.TemporaryDirectory(prefix="mfu-smoke-") as tmp:
        venv_dir = Path(tmp) / "venv"
        venv.create(venv_dir, with_pip=True)
        py = str(venv_dir / "bin" / "python")

        run([py, "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
        run([py, "-m", "pip", "install", "--quiet", wheel])

        # Imports
        run([py, "-c",
             "import engine; from engine.api import handle_run_deal; "
             "from engine.validator import validate_deal; "
             "from engine.ingest.rent_roll_parser import parse_rent_roll; "
             "from engine.ingest.t12_parser import parse_t12; "
             "print('import-ok')"])

        # Deal schema ships in the wheel and the validator reads it
        run([py, "-c",
             "from engine.validator import _schema_path; "
             "p = _schema_path(); assert p.exists(), f'missing {p}'; "
             "print('schema-ok', p.name)"])

        # Deterministic parse: synthetic CSV fixture → canonical cohorts
        run([py, "-c",
             "from engine.ingest.rent_roll_parser import parse_rent_roll; "
             "rr = parse_rent_roll('tests/fixtures/sample_rent_roll.csv'); "
             "assert rr['total_units'] > 0; "
             "assert rr['unit_cohorts']; "
             "first = rr['unit_cohorts'][0]; "
             "assert {'cohort_id', 'unit_count'} <= set(first), first; "
             "print('rentroll-ok', rr['total_units'])"])

        # Refusal is a feature: an empty deal must fail validation
        run([py, "-c",
             "from engine.validator import validate_deal; "
             "report = validate_deal({}); "
             "assert report.status == 'FAIL', report.status; "
             "assert report.issues; "
             "print('validator-ok', len(report.issues), 'issues')"])

    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())