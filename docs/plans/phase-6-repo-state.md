# Phase 6: repo-state pattern

Goal: a new session can learn the repo's current truth from files, not from memory or git archaeology.

1. STATE.md from CHANGELOG, tags, releases, merged PRs and a fresh test run; ROADMAP.md with
   phases 0 to 7 reconstructed from releases; LESSONS.md seeded from the security and Dependabot
   fixes; docs/DECISIONS.md with D-001 to D-012 recorded retroactively, D-013 for this phase.
2. justfile: `check` (compileall lint, all tests, demo, leak check) and `verify-phase-0..6`.
3. Guard: `scripts/state_guard.py` plus `tests/test_state_guard.py`; a `state-guard` CI job on
   pull requests; `chore:`/`deps:` titles exempt so Dependabot PRs still auto-merge.
4. Sandbox: add `./justfile` to denyWrite and `MUST_DENY`, since the owner runs it unsandboxed.
5. CLAUDE.md session ritual.

Verify: `just verify-phase-6` and `just check`; paste output into STATE.md.
