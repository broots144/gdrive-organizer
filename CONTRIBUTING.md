# Contributing

Thanks for helping. This tool moves and trashes files in real Google Drives, so changes are
judged first on safety, then on usefulness.

## Setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[all]"
bash scripts/install_hooks.sh
```

## Tests

```bash
python tests/test_fs_local.py && python tests/test_drive_mock.py && python tests/test_rules_to_plan.py
```

CI runs them on Linux and macOS with Python 3.10 and 3.13. The Drive tests use an in-memory mock:
no network and no real account. A change to `validate.py`, `apply.py`, `core.py` or the index code
needs a test that fails without it.

## Ground rules

- **Guards stay on.** Do not weaken the protected-folder guard, the validator's refusals, sha
  pinning, the journal or reconcile-before-retry. New operations must be reversible and journaled.
- **No permanent deletes.** Removal goes through Drive's trash with a checked precondition.
- **No runtime dependencies in core.** Google libraries are optional extras.
- **Python 3.9 compatible** (macOS system Python).
- **Docs:** label claims as fact, estimate or guess, with a source. No em or en dashes.

## Privacy

Never commit or paste real data: indexes, reports, quarantine lists, manifests, journals, rules
or folder names from your Drive. Use made-up names in tests and issues. The pre-commit hook runs
`scripts/leak_check.py`, which blocks strings from your `private/config.json` and an optional
`.private-patterns` file (one literal per line: your name, family names, employers, email).

## Pull requests

Keep them focused, describe the safety impact, and include test output. Security problems go
through private reporting, not a public issue: see [SECURITY.md](SECURITY.md).
