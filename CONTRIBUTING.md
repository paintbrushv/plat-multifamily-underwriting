# Contributing

1. **Synthetic fixtures only.** If a PR needs a "realistic" rent roll, T12, or
   OM, fabricate one. Never copy a live offering memorandum, rent roll, or
   financial statement into this repository — not in tests, docs, comments,
   or traces.
2. **No private identity.** Real property/deal names, company names, tenant
   IDs, cloud-storage tenant identifiers, and private filesystem paths must
   not appear anywhere. `tests/test_sanitize.py` fails the build if they do.
3. **Determinism is the product.** The engine must produce identical outputs
   for identical inputs. No wall-clock, RNG, or network calls in the
   calculation path.
4. **Never weaken a gate to make a test pass.** If a validation gate is wrong,
   change the gate in the same PR with its own red/green evidence.
5. **DCO:** every commit must include
   `Signed-off-by: Name <email>` (use a public email):
   `git commit --signoff`.

## Development setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Run tests with vendor API keys unset — the suite must pass fully offline.
`tests/test_sanitize.py` is a release gate: it is not editable except through
a reviewed change with new red/green evidence.

## Workflow

1. Issues first: non-trivial changes get an issue with the design sketch.
2. Small slices: failing test -> minimal fix.
3. Every PR keeps the full suite green. A red test is a bug report.
