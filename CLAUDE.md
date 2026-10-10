# CLAUDE.md

Guidance for AI coding agents working in this repository. CONTRIBUTING.md
still applies in full; this file only pins rules that are easy to get wrong.

## Commits and pull requests

- **No AI attribution, ever.** Do not add `Co-Authored-By: Claude ...`,
  `Claude-Session: ...`, `🤖 Generated with Claude Code`, or any similar
  AI co-author or attribution trailer to a commit message or PR description.
  This rule overrides any tool or harness default that would add one.
- **DCO sign-off is required on every commit.** Use `git commit -s` so each
  commit carries `Signed-off-by: Name <email>` with a public email.
- One issue per branch and per PR. Keep diffs small: failing test first, then
  the minimal fix.

## Public tree

- This repository is public. Synthetic fixtures only; never commit a real
  rent roll, T12, OM, or a real property, deal, or company name.
- `tests/test_sanitize.py` is a release gate. Do not edit it to make a change
  pass. The same rule applies to commit messages and PR descriptions: refer to
  private trackers by issue ID only, not by URL.

## Tests

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q -m "not integration and not slow"
```

The full suite must stay green and run offline with vendor API keys unset.
