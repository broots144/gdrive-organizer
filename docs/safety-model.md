# Safety model

What each stage guarantees, where it is enforced, and what is **not** guaranteed. Claims here
describe the code in this repository (fact) unless marked otherwise.

## Stages

| Stage | Guarantee | Enforced by |
|---|---|---|
| Index | Protected folders are recognized by Drive ID or name from their parent's listing and never listed, fetched or updated; shortcuts to them are quarantined. Only metadata is requested. | `index_drive.py`, `core.Guard`; tested in `tests/test_drive_mock.py` (the protected ID never appears in any API call) |
| Report | Aggregates only; sensitive-looking names masked; protected entries counted, never named. | `report.py` |
| Peek (optional) | Content read through the API only, bounded (first page, first 4 KB, or an export truncated to 1,500 characters); sensitive names skipped; matching snippets discarded unless `--include-sensitive`. | `peek.py` |
| Plan | Rules compile to `mkdir`, `move` and `trash` only. Generators skip what validate would refuse and never print paths. | `scripts/*_to_plan.py` |
| Validate | Refuses: protected ground (the folder, its contents, any ancestor, protected-looking destination names); ambiguous sources; overlapping sources; existing destinations (case and Unicode insensitive); splitting atomic units; recently modified folders, sensitive names and items not owned by you, unless the op carries an explicit override; items Drive marks immovable; invalid names; trash without a byte-identical surviving copy; trash of a non-empty folder. | `validate.py` |
| Apply | Runs only with the manifest's sha256; re-validates; re-checks protected ground before each op; checks each item against the index (drift) and, for trash, both copies or the folder's live children in Drive; write-ahead journal with fsync; reconciles unknown or failed ops against Drive before retrying; undo by Drive ID. | `apply.py`; tested in `tests/test_drive_mock.py` and `tests/test_fs_local.py` |

## What it will never do

- Permanently delete anything. `trash` uses Drive's trash, which Drive keeps for 30 days.
- Overwrite a file. Drive moves cannot clobber; local renames use `RENAME_EXCL`.
- Rename a file on its own initiative. The plan generators keep every file name, except names you
  list in `STRIP_NAMES`. A hand-written manifest may rename (validate warns if an extension changes).
- Touch a protected folder, its contents or any folder above it.
- Read file contents during indexing.

## What is not guaranteed

- **No cross-operation atomicity.** Each operation is atomic; a run that stops halfway leaves the
  Drive half reorganized (and fully described by the journal, so rerun or undo).
- **Undo is best effort after later changes.** If you edit, move or trash items after a run, undo
  stops at the first operation it can no longer reverse and records where it stopped.
- **Undo cannot resurrect what Drive has purged.** After Drive empties its trash (30 days), trashed
  items are gone.
- **Name-based protection depends on your config.** A protected folder you did not list by ID or
  name is an ordinary folder to this tool.
- **Your assistant's judgement.** The tool constrains what can happen, not whether a proposed
  taxonomy is a good one. Review the plan.

## Reporting a problem

A bug in any of the above is a security issue. See [SECURITY.md](../SECURITY.md).
