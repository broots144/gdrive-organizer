# Changelog

## 0.3.1 (2026-09-26)

Security fixes from an audit of the assistant boundary.

- `apply` no longer trusts its journal, which `--confirm-sha` does not cover. A journal must
  belong to the manifest being run; every record must match the reviewed op it claims to be;
  folder IDs read back from it must be the live folders this manifest created (right name,
  right parent); protected IDs are refused; undo reverses an op only when Drive still shows what
  the record says was done. Before, one forged record could send reviewed moves into any folder
  or make undo move, rename or untrash items that were never part of the plan.
- The full-access `apply` token is kept in memory by default and never written to
  `private/token_write.json`; `--token FILE` restores the old behavior. Delete any old copy.
- `.claude/settings.json` denies OAuth files at the OS level (`sandbox.filesystem.denyRead` and
  `denyWrite`), not only to the Read tool, and denies sandboxed writes to apply journals.

## 0.3.0 (2026-09-26)

- New subcommands `gdrive-organizer plan`, `dedupe` and `empty-dirs`, so the whole workflow works
  from an installed package. The `scripts/*_to_plan.py` paths remain as thin wrappers.
- `examples/demo/`: a one-minute demo on a fake local "My Drive" (no Google account), run in CI,
  and the recording at the top of the README.
- `report` creates the folder for its quarantine list instead of failing on a fresh checkout.
- Test fixtures use a neutral protected-folder name.

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
