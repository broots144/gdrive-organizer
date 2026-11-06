# Current Build State

Last updated: 2026-11-06 by Claude Code: pypdf floor 6.21.4 -> 7.0.0 (major; deps/upgrade-2026-11-06).

## Phases

- [x] Phase 0: core pipeline (metadata-only index, report, peek, validate, journaled apply and undo), 0.1.0
- [x] Phase 1: rules generator, dedupe and empty-dirs trash ops, retry reconcile, outside-user docs, 0.2.0
- [x] Phase 2: CLI subcommands (`plan`, `dedupe`, `empty-dirs`) and the fake-tree demo, 0.3.0
- [x] Phase 3: assistant-boundary hardening, 0.3.1 to 0.3.4 (#15, #16, #17, #18) plus #19
- [x] Phase 4: `plan` merges or blocks collisions, never invents holding folders (#20), unreleased
- [x] Phase 5: Dependabot autopilot per ADR-089 in workshop144/sublevel (#22, #23, #29, #30)
- [~] Phase 6: repo-state pattern (this PR); done when merged with the guard green
- [ ] Phase 7: release 0.3.5 from the Unreleased changelog section

## What is true now

- Version 0.3.4 is the latest tag and GitHub release (2026-09-29). `main` carries unreleased work:
  #19 (tests and scripts load the package by path, never the repo root on `sys.path`) and #20
  (collision rework, `--collisions-out`), plus Dependabot floor bumps (#24, #26, #27, #28, #31).
- Core has no runtime dependencies; extras `api` (google-api-python-client>=2.201.0,
  google-auth-oauthlib>=1.5.0) and `peek` (pypdf>=7.0.0, python-docx>=1.2.0). Python 3.9+ for
  core; every extra floor now needs Python 3.10+ (fact: PyPI `requires_python` for each).
- CI (`.github/workflows/test.yml`): ubuntu and macOS x Python 3.10 and 3.13 run the five test
  files, the state guard rules, the demo and a CLI smoke. On PRs a `state-guard` job fails when
  the package, `scripts/` or `pyproject.toml` change without STATE.md (D-013).
- Dependabot: weekly, 3 day cooldown (7 for pip majors), one group for Python ranges and two
  for actions; n0's merger squash-merges green PRs (ADR-089). Last run on main green 2026-11-01.
- The sandbox (`.claude/settings.json`) denies writes to everything the owner runs outside it,
  now including `./justfile`; `tests/test_sandbox_settings.py` enforces the list (D-008).
- Personal Drive work happens only under gitignored `private/`; `scripts/leak_check.py` guards
  commits via hooks from `scripts/install_hooks.sh` (D-006).
- No open PRs or issues as of 2026-11-06.

### Verify output (2026-11-06, macOS, Python 3.15, `just check` with the repo venv)

```
ALL FS TESTS PASSED
ALL DRIVE MOCK TESTS PASSED
dupes_to_plan: ok (3 trash ops, 0 validation errors)
ALL PEEK LIMIT TESTS PASSED
sandbox denyWrite covers 27 owner-run paths; 3 assistant outputs stay writable
7 owner-run tests/scripts ignore 48 root-level stdlib-named packages
ALL SANDBOX SETTINGS TESTS PASSED
ALL STATE GUARD TESTS PASSED
demo and CLI smoke ok
```

## Open / BLOCKED

- Nothing blocked.
- `just check` needs `.venv` with `pip install -e ".[all]"` (or `PY=` and a PATH with the
  `gdrive-organizer` entry point); system python lacks the Google libs, so `test_drive_mock.py`
  fails there with `ModuleNotFoundError: googleapiclient`.
- Dependabot floor bumps since #22 (for example google-api-python-client>=2.201.0) were not
  checked against `requires-python = ">=3.9"`; CI covers 3.10 and 3.13 only. Decide before 0.3.5
  whether to restore `increase-if-necessary` for pip or raise `requires-python` (fact: matrix in
  test.yml; fact (PyPI, 2026-11-06): google-api-python-client 2.201.0, google-auth-oauthlib 1.5.0
  and pypdf 7.0.0 all declare `requires_python >=3.10`, so the extras do not install on 3.9).
- `just leak` only checks private patterns in the main checkout, where `.private-patterns` and
  `private/config.json` live; a worktree run warns and checks the commit identity only.

## Next

1. Phase 7: cut 0.3.5: move the Unreleased section of CHANGELOG.md under `## 0.3.5 (<date>)`,
   bump `pyproject.toml`, tag `v0.3.5`, `gh release create` with the changelog text.
2. Consider README limitations as future phases (shared drives, Docs/Sheets dedupe); they are
   deferred in ROADMAP.md with no target.
