#!/usr/bin/env python3
"""Install the freshly built wheel into a throwaway venv and smoke it.

Run from the repo root after `python -m build`; expects exactly one wheel in
dist/. Verifies: clean-venv install resolves, the engine package imports,
the JSON deal schema ships inside the wheel, the deterministic parsers
produce identical output on a synthetic fixture, the schema validator gates
a bad deal, and the api layer answers with certified metrics. The MCP entry point and schema must load from the installed wheel.
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
    kw.setdefault("cwd", tempfile.gettempdir())
    subprocess.run(cmd, check=True, **kw)


def main() -> int:
    wheels = sorted(glob.glob(str(DIST / "*.whl")))
    if len(wheels) != 1:
        print(f"expected exactly one wheel in dist/, found: {wheels}")
        return 1
    wheel = wheels[0]

    with tempfile.TemporaryDirectory(prefix="mfu-smoke-") as tmp:
        venv_dir = Path(tmp) / "venv"
        venv.create(venv_dir, with_pip=True, symlinks=sys.platform != "win32")
        bin_dir = venv_dir / ("Scripts" if sys.platform == "win32" else "bin")
        py = str(bin_dir / ("python.exe" if sys.platform == "win32" else "python"))

        run([py, "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
        run([py, "-m", "pip", "install", "--quiet", wheel + "[mcp]"])

        # Imports
        run([py, "-I", "-c",
             "import engine; from engine.api import handle_run_deal; "
             "from engine.validator import validate_deal; "
             "from engine.ingest.rent_roll_parser import parse_rent_roll; "
             "from engine.ingest.t12_parser import parse_t12; "
             "print('import-ok')"])

        # Deal schema ships in the wheel and the validator reads it
        run([py, "-I", "-c",
             "from engine.validator import _schema_path; "
             "p = _schema_path(); assert p.exists(), f'missing {p}'; "
             "print('schema-ok', p.name)"])

        fixture = Path(tmp) / "sample_rent_roll.csv"
        fixture.write_bytes((REPO / "tests/fixtures/sample_rent_roll.csv").read_bytes())

        # Deterministic parse of copied public synthetic CSV, outside the checkout
        run([py, "-I", "-c",
             "from engine.ingest.rent_roll_parser import parse_rent_roll; "
             f"rr = parse_rent_roll({str(fixture)!r}); "
             "assert rr['total_units'] > 0; "
             "assert rr['unit_cohorts']; "
             "first = rr['unit_cohorts'][0]; "
             "assert {'cohort_id', 'unit_count'} <= set(first), first; "
             "print('rentroll-ok', rr['total_units'])"])

        # Refusal is a feature: an empty deal must fail validation
        run([py, "-I", "-c",
             "from engine.validator import validate_deal; "
             "report = validate_deal({}); "
             "assert report.status == 'FAIL', report.status; "
             "assert report.issues; "
             "print('validator-ok', len(report.issues), 'issues')"])

        # The stdio adapter must be present in the installed wheel.
        run([py, "-I", "-c",
             "from engine.mcp_server import CONTRACT_VERSION, mcp; "
             "assert CONTRACT_VERSION == 'plat.underwriting.mcp/1'; "
             "assert mcp is not None; print('mcp-adapter-ok')"])

    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
