# Lessons

One line per lesson, newest first.

- 2026-11-06: forward apply was write-ahead and reconciled but undo was not, and files.create was retried blind on 5xx; rule: every Drive write is journaled before it is sent, sent once, and reconciled from live state after a crash or timeout, in both directions.
- 2026-11-06: status lived only in CHANGELOG and commit messages, so a new session had to rebuild it from git; rule: STATE.md is the one current truth and CI fails code PRs that skip it (D-013).
- 2026-11-01: Dependabot update-type groups skip range-style requirements (`>=x`), giving one PR per package (#30); rule: one pattern group for pip ranges.
- 2026-10-31: `semver-major-days` cooldown is rejected for github-actions (#23); rule: only `default-days` there, majors cooldown only for pip.
- 2026-09-29: `plan` moved collisions into invented `from-<dump>` and `same-name-N` folders the owner never asked for (#20); rule: merge into the existing target or leave in place and report, never invent folders.
- 2026-09-28: tests and scripts put the repo root on `sys.path`, so a sandbox-created `./ctypes/` could shadow the stdlib and run outside the sandbox (#19); rule: load the package by path, and `test_sandbox_settings.py` shadow-checks every owner-run entry point.
- 2026-09-28: `apply` trusted Drive IDs from the index, which `--confirm-sha` does not cover (#18); rule: check every ID live in Drive against the reviewed path before acting.
- 2026-09-28: `apply --undo` accepted journal records not bound to the confirmed manifest (#17, #18); rule: every undo record must match a reviewed op and its reviewed source.
- 2026-09-27: peek fed attacker-controlled PDF and DOCX files to parsers unbounded (#16); rule: cap download bytes, refuse zip bombs, time-limit each parse.
- 2026-09-26: the apply journal was trusted, and the full-access token was written to `private/token_write.json` (#15); rule: anything outside the sha-confirmed manifest is untrusted, and the write token stays in memory. The token file was gitignored under `private/` and never committed; owners were told to delete old copies.
- 2026-10-31: 08c2d33 (2026-09-25) set `increase-if-necessary` to keep open pip floors for Python 3.9+, and #22 replaced the config without it, so Dependabot now raises floors (#24, #26, #27, #28, #31); rule: a floor bump is a support decision, so check it against `requires-python` before merging by hand.
