# Decisions

Short ADR log. Entries are never rewritten, only superseded by a later entry. D-001 to D-012 are
recorded retroactively on 2026-11-06 from the code, docs and git history they cite.

- **D-001 (2026-09-25): metadata-only index.** Phase 1 indexes names, sizes, checksums and
  parents, never content; content is read only by bounded `peek`. See [safety-model.md](safety-model.md).
- **D-002 (2026-09-25): protected folders are excluded before listing.** Names and IDs live in
  `private/config.json`; the indexer skips them and links or shortcuts to them, and nothing
  may move them or an ancestor.
- **D-003 (2026-09-25): the human executes.** Assistants write rules and run dry runs and
  `validate`; only the owner runs `apply --execute` and `--undo --execute` (CLAUDE.md rule 1).
- **D-004 (2026-09-25): rules, not per-file decisions.** The taxonomy is SQL predicates over
  `items` plus destination templates, compiled by `plan` (e9eb38e).
- **D-005 (2026-09-25): removal is trash only, for exact duplicates and empty folders.** Each
  dedupe op names the byte-identical copy that stays; undo un-trashes (995a990, 7b4e0f8).
- **D-006 (2026-09-25): nothing personal in git.** Personal artifacts live in gitignored
  `private/`; `scripts/leak_check.py` runs in pre-commit and commit-msg hooks; commits use the
  GitHub noreply identity.
- **D-007 (2026-09-25): no runtime dependencies in core.** Google and parser libs are extras.
- **D-008 (2026-09-26): the sandbox may not write anything run or trusted outside it.** Enforced
  by `.claude/settings.json` denyWrite and `tests/test_sandbox_settings.py` (#15, #17, #18).
- **D-009 (2026-09-26): anything not covered by `--confirm-sha` is untrusted.** Journal, index
  IDs and undo records are checked against the reviewed manifest and live Drive (#15, #17, #18).
- **D-010 (2026-09-28): owner-run code never puts the repo root on `sys.path`.** Tests and
  scripts load the package by file path (#19).
- **D-011 (2026-09-29): `plan` never invents holding folders.** Collisions merge into the existing
  target or stay in place and are reported (#20).
- **D-012 (2026-10-31): Dependabot autopilot.** Weekly grouped updates with cooldown, merged by
  n0 when every CI check is green, per ADR-089 in workshop144/sublevel (#22, #29, #30).
- **D-013 (2026-11-06): repo-state pattern.** STATE.md is the single current truth, with
  ROADMAP.md, LESSONS.md, this log, `docs/plans/`, and justfile verify targets. CI's
  `state-guard` job fails a PR that changes the package, `scripts/` or `pyproject.toml` without
  STATE.md, unless its title starts with `chore:` or `deps:` (Dependabot's `chore(deps):`
  passes). The justfile is sandbox write-denied like other owner-run code (D-008).
