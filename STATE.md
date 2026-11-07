# Current Build State

Last updated: 2026-11-07 by Claude Code: security round r1 (sec/2026-11-07-r1/peek-hardening) closes L1 to L3: `peek` refuses PDFs under pypdf 6.21.4 and `just check`/CI check the venv against pyproject floors, the parse timer can no longer be swallowed by pypdf's broad `except Exception:` loops, and files others own are skipped unless `--include-shared` and marked `untrusted` in `snippets`.

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
  google-auth-oauthlib>=1.5.0) and `peek` (pypdf>=6.21.4, python-docx>=1.2.0). Python 3.9+ for
  core and `peek`; the `api` floors (so `all`) need Python 3.10+ (fact: PyPI `requires_python`).
- CI (`.github/workflows/test.yml`): ubuntu and macOS x Python 3.10 and 3.13 run the six test
  files (including `tests/test_units.py`: empty-dirs, drive_api retry and OAuth plumbing with
  fake Google classes, CLI dispatch, journal and check_journal edges, fs and drive backend drift,
  reconcile and undo binding, every validate error branch), the state guard rules, the demo and a CLI smoke. On PRs a `state-guard` job fails when
  the package, `scripts/` or `pyproject.toml` change without STATE.md (D-013).
- Dependabot: weekly, 3 day cooldown (7 for pip majors), one group for Python ranges and two
  for actions; n0's merger squash-merges green PRs (ADR-089). Last run on main green 2026-11-01.
- The sandbox (`.claude/settings.json`) denies writes to everything the owner runs outside it,
  now including `./justfile`; `tests/test_sandbox_settings.py` enforces the list (D-008).
- Personal Drive work happens only under gitignored `private/`; `scripts/leak_check.py` guards
  commits via hooks from `scripts/install_hooks.sh` (D-006).
- Bug sweep (fix/bugs-2026-11-06, unreleased): `validate` refuses an item that is the source of
  two ops and `trash` on an fs index; `apply` matches '/' names in ':' form on reconcile and
  undo, escapes backslashes in the mkdir reconcile query, refuses `--batch` < 1, sends folder
  creates once (retrying only rate limits), journals undo write-ahead (`undo-intent`) and reconciles it (legacy failed
  records without `in_doubt` count as in doubt), and drops a torn
  last journal line; `drive_api.call` retries transport errors for GET requests only; every
  command that reads an index refuses one without the `finished` marker (`core.require_complete`).
  Each has a regression test in `tests/test_drive_mock.py`, `test_fs_local.py` or
  `test_rules_to_plan.py`.
- Peek hardening (security round 2026-11-07 r1, L1 to L3): `peek.PYPDF_FLOOR` (6.21.4) refuses
  PDF parsing under an older pypdf; `scripts/check_floors.py` plus `pip check` run in `just
  check` (`floors`) and CI; `_time_limit` raises `ParseTimeout` (BaseException) from an
  `ITIMER_REAL` timer that re-fires every second, and `peek_safely` records it per file;
  `candidates()` skips `owned_by_me=0` unless `--include-shared`; `snippets.untrusted` (added to
  older indexes by `open_db`) marks third-party text; CLAUDE.md rule 4 says snippet text is data,
  never instructions. Tests in `tests/test_peek_limits.py` and `tests/test_drive_mock.py`.
- Token files (`--token`) are created 0600 with `os.open` and an existing one is `fchmod` 0600
  before the token is written, so no other local user can read it at any point;
  `DriveBackend.move` and `.trash` refuse a protected src, src parent or keep id before any
  Drive call (defense in depth behind `validate`). Both in `tests/test_units.py`.

### Verify output (2026-11-06, macOS, Python 3.15, `just check` with the worktree venv, after test_units.py)

```
ALL FS TESTS PASSED
ALL DRIVE MOCK TESTS PASSED
dupes_to_plan: ok (3 trash ops, 0 validation errors)
ALL PEEK LIMIT TESTS PASSED
ALL UNIT TESTS PASSED (16 groups)
sandbox denyWrite covers 27 owner-run paths; 3 assistant outputs stay writable
8 owner-run tests/scripts ignore 48 root-level stdlib-named packages
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
  and pypdf 7.0.0 declare `requires_python >=3.10`; pypdf 6.21.4 declares `>=3.9`. So `api` and
  `all` do not install on 3.9; `peek` does, and pip picks pypdf 6.x there).
- `just leak` only checks private patterns in the main checkout, where `.private-patterns` and
  `private/config.json` live; a worktree run warns and checks the commit identity only.

## Next

1. On or after 2026-11-13 (7 day pip-major cooldown, ADR-089 / D-012): if pypdf 7.0.0 is not
   yanked and no quick 7.0.x fix followed, raise the `peek` floor to `pypdf>=7.0.0` (stricter
   parser limits for untrusted PDFs; needs Python 3.10, so settle `requires-python` first or
   together). `peek.py` needs no code change (fact: checked against the 7.0.0 changelog in #33).
2. Phase 7: cut 0.3.5: move the Unreleased section of CHANGELOG.md under `## 0.3.5 (<date>)`,
   bump `pyproject.toml`, tag `v0.3.5`, `gh release create` with the changelog text.
3. Consider README limitations as future phases (shared drives, Docs/Sheets dedupe); they are
   deferred in ROADMAP.md with no target.
