# Security

- **Never attach real offering memoranda, rent rolls, T12s, or any live deal
  data to issues or PRs.** This repository is synthetic-only; reports with
  real data will be deleted.
- Secrets (API keys, tenant IDs, `.env` contents) stay out of git — including
  in test fixtures and traces.
- The default install runs fully offline. LLM-based OM extraction is an
  opt-in extra and must never receive data you are not licensed to send to a
  hosted model.
- Property tax outputs are statute-cited but require professional review per
  jurisdiction; do not treat engine output as tax advice.

## Reporting

Report suspected vulnerabilities privately to the maintainer (`security@`
address to be published with the repository). Do not open public issues for
exploitable findings. Include the exit code, the typed error, and — if
possible — a synthetic reproducer. Never include live deal data.

`tests/test_sanitize.py` is a release gate: if you can make it fail, report
it — do not edit the gate.
