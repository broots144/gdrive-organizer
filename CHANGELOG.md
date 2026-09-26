# Changelog

## 0.2.0 (2026-09-25)

- `scripts/rules_to_plan.py`: compile rules (SQL predicates plus destination templates) into a
  manifest, with collision merging, dump-folder emptying and same-name handling; files never renamed.
- `trash` operation: exact duplicates only (a byte-identical copy must stay) and empty folders
  (innermost first, checked live in Drive); Drive's trash only, undo un-trashes.
  `scripts/dupes_to_plan.py` and `scripts/empty_dirs_to_plan.py` build these manifests.
- `apply --retry-failed` reconciles against Drive first, so an operation that landed despite a
  timeout is recorded as done instead of halting on drift; reconciled operations stay undoable.
- `peek`: `--max-depth`, `--only-keys` and `--include-sensitive`.
- Expired OAuth tokens (7 days for Testing-mode apps) trigger a new sign-in instead of an error.
- Docs: usage guide, OAuth setup, safety model, contributing guide, issue and PR templates.

## 0.1.0

- Initial release: metadata-only Drive index with protected-folder exclusion, aggregate reports,
  bounded content peeks, manifest validator, journaled apply and undo (drive and fs backends).
