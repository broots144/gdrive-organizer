# Changelog

## Unreleased

- `plan` no longer invents holding folders on a collision. A dump child whose target exists
  merges into it like any other folder (its empty shell is left for `empty-dirs`) instead of
  moving whole to `<target>/from-<dump>`; `DUMP_NEST` is ignored with a note. A file whose name is
  taken by a different file (or a same-name Google Doc) stays in place, counted as
  `file_name_collision`, instead of going to a `same-name-N/` folder; the new `--collisions-out`
  lists those clashes as Drive IDs so a rename rule pinned to each ID can place them.

- The tests and `scripts/*_to_plan.py` no longer put the repo root on `sys.path` (or `PYTHONPATH`)
  ahead of the standard library; they load the in-repo package by file path (`tests/_pkg.py`,
  `scripts/_pkg.py`). The sandbox can still create a new root-level directory such as `./ctypes/`,
  and before this such a directory would have shadowed the stdlib module and run when you ran the
  tests outside the sandbox. `tests/test_sandbox_settings.py` now runs the tests and scripts in a
  scratch copy full of stdlib-named root-level packages and fails if any of them is imported.

## 0.3.4 (2026-09-28)

- `apply` no longer trusts Drive IDs from the index, which `--confirm-sha` does not cover. Before
  acting it checks in Drive that the index's root is your My Drive, that each destination
  folder (indexed or created by this run) sits at the reviewed path, and that each item to move
  or trash sits in the reviewed source folder under the reviewed name. Before, an index edited
  between review and execute could send a reviewed move into another folder, or move a
  same-named item from a folder you never reviewed.
- `apply --undo` binds every move record to the reviewed manifest op even after a re-index,
  when the op no longer validates: the item goes back only to the reviewed source folder
  (checked in Drive) under the reviewed name, from the planned destination. Before, such a
  record could name any unprotected folder and any name. Undo also uses the journal exactly as
  it was checked instead of reading it a second time. The fs backend's undo checks the recorded
  source path against the manifest too.
- `.claude/settings.json` denies sandboxed writes to the rest of what you run or trust outside
  the sandbox: `tests/`, `examples/`, root-level `*.py` and `*.pth`, `.claude/`, `.mcp.json`, and
  `private/config.json` and `private/*.sqlite` (with their journal files). Checked against the
  sandbox runtime; `tests/test_sandbox_settings.py` keeps the list complete.

## 0.3.3 (2026-09-28)

- `apply --undo` binds every journal record to the sha-confirmed manifest, not only to the
  ops that still validate: a journal must start with this manifest's header, and a record whose
  op index is not in the manifest, or does not match that manifest op, is refused. Before, a
  record with an unknown index was undone without any check against the reviewed plan.
- `.claude/settings.json` also denies sandboxed writes to the code the owner runs outside the
  sandbox: the package, `.venv`, `scripts/`, `.git/hooks`, `.git/config` and `pyproject.toml`.

## 0.3.2 (2026-09-27)

- `peek` bounds what it hands to the PDF and DOCX parsers, since peeked files can come
  from anyone who shares a folder with you: the download is range-capped at `--max-bytes`
  whatever the index said, a DOCX that would expand past 64 MB or compress more than 200x
  (a zip bomb) is refused, and each parse gets 20 seconds. A refusal is recorded as that
  file's error and the run continues. `pypdf>=6.0` (bounded stream decompression).

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
