# Usage

All commands assume the repo root and a `private/` folder holding your config and data.

## 1. Index and report

```bash
gdrive-organizer index-drive --db private/index.sqlite --config private/config.json --client-secret private/client_secret.json
gdrive-organizer report      --db private/index.sqlite --config private/config.json > private/report.txt
```

The report is the only thing your assistant needs to read: totals, extensions, the largest
folders, loose files, duplicates, atomic units and folders modified recently (`LIVE?`, a hint that
something still syncs into them). Sensitive-looking names are masked and listed only in
`private/quarantine.txt`, for you.

Re-index after every executed round: plans are validated against the index, so they must be
built from the current state of your Drive.

## 2. Optional: content peeks

For files whose names say nothing ("Untitled document", "scan_0042.pdf"), `peek` stores short
text snippets in the index, read through the Drive API (never the local mount):

```bash
gdrive-organizer peek --db private/index.sqlite --config private/config.json --client-secret private/client_secret.json --limit 300
```

- `--max-depth 0` only looks at the top level of My Drive; `--only-keys FILE` peeks exactly the
  Drive IDs listed in `FILE`, one per line.
- Sensitive-named files are skipped, and a snippet matching `no_snippet_regex` is discarded and
  only flagged. `--include-sensitive` turns both off; use it only for content you are happy to
  show your assistant. Protected folders are never affected: they are not in the index.
- Images and scans without a text layer, Google Forms and Drawings, and old `.doc` files yield
  no text.

## 3. Rules to manifest

A rules file is plain Python defining `RULES`: each rule is a SQL `WHERE` clause over the `items`
table plus a destination template. First match wins. Start from
[examples/rules.example.py](../examples/rules.example.py), which lists the columns.

```python
dict(id="finance", where="i.depth=0 AND i.kind='dir' AND i.pathkey IN ('taxes','receipts')",
     dst="finance/{lh}")      # {lh}: folder name lowercase-hyphenated; files are never renamed
```

```bash
python3 scripts/rules_to_plan.py --db private/index.sqlite --config private/config.json --rules private/rules.py --out private/plan.jsonl
gdrive-organizer validate --db private/index.sqlite --config private/config.json --manifest private/plan.jsonl
```

Iterate on the rules until `validate` reports zero errors. What the generator does for you:

- Moves only the **outermost** match, so sources never overlap and units are never split.
- Leaves in place anything protected, not owned by you, not movable, inside an atomic unit, a dot
  entry, or modified within `recent_days` (likely a live sync or backup target).
- Adds `"override": ["sensitive"]` to each sensitive-named source, so every exception is visible
  in the manifest instead of hidden behind a global flag.
- Resolves **collisions** (two sources for one target, or a target that exists with different
  case): the target becomes an `mkdir` and the sources merge into it, leaving their emptied shells.
  Files are never renamed: an identical colliding file goes to `archive/duplicates`, a different one
  with the same name to a `same-name-2/` folder beside the first. `STRIP_NAMES` lists the only
  allowed renames (names with leading or trailing whitespace, which Drive allows but validate
  refuses).
- Optionally empties a **dump folder** (`DUMP = "old dropbox"`): a colliding child is moved whole
  into `<target>/from-old-dropbox` so the dump really ends up empty.
- Prints counts per rule and a live, archive and left-in-place split. It never prints paths.

Advice that matters more than any flag:

- Move **folders**, not files, wherever a folder is coherent. One folder move is one API call.
- Keep app landing folders (scanner targets, "Saved from Chrome") where the app expects them.
- End a dump-folder rule set with a catch-all to `archive/unsorted/{orig}`: uncertain items stay
  findable at their original relative path instead of being guessed into the wrong place.
- For names you cannot place, peek at the content and pin the decision to Drive IDs
  (`where="i.key IN (...)"`), rather than widening a pattern until it catches the wrong files.

## 4. Execute

```bash
gdrive-organizer apply --backend drive --db private/index.sqlite --config private/config.json --manifest private/plan.jsonl
gdrive-organizer apply ... --execute --confirm-sha <sha from validate> --max-ops 20     # canary
gdrive-organizer apply ... --execute --confirm-sha <sha> --batch 50 --pause 60
gdrive-organizer apply ... --execute --confirm-sha <sha> --undo                        # roll back
```

- Without `--execute` it is a dry run that re-validates and prints the first operations.
- The journal is written next to the manifest (`plan.jsonl.journal.jsonl`). Keep it: it is how undo
  works, and how a rerun skips what is already done.
- A failed operation stops the run. `--retry-failed` checks Drive first and records the op as done
  if the change already landed (for example a response that timed out), instead of doing it twice.
- Run it in a terminal you control, not through your assistant.

## 5. Remove exact duplicates (optional)

```bash
cp examples/dedupe.example.py private/dedupe.py        # edit KEEP_ORDER, LAST, NEVER_TRASH
python3 scripts/dupes_to_plan.py --db private/index.sqlite --config private/config.json --policy private/dedupe.py --out private/dedupe.jsonl
gdrive-organizer validate --db private/index.sqlite --config private/config.json --manifest private/dedupe.jsonl
```

- Only files with the same Drive `md5Checksum` and size count. Google Docs and empty files never do.
- `KEEP_ORDER` and `LAST` decide which copy stays; `NEVER_TRASH` lists areas where nothing is
  trashed (backups, software trees, code, app landing folders, curated packets you share).
- Every op names its `keep_key`. `validate` refuses a trash whose kept copy is missing, different,
  trashed or moved in the same manifest; `apply` re-checks both copies in Drive before acting.
- Run it on a fresh index after reorganizing, then `apply` it like any manifest.

## 6. Remove empty folders (optional)

```bash
python3 scripts/empty_dirs_to_plan.py --db private/index.sqlite --config private/config.json --policy private/dedupe.py --out private/empty-dirs.jsonl
```

Lists every folder with nothing but folders below it, innermost first, skipping `NEVER_TRASH` areas
and the `KEEP_EMPTY` folders you want to keep (your top-level taxonomy). `apply` asks Drive right
before each one that it still has no live children.

## Local mount fallback

No OAuth? `gdrive-organizer index-fs --root ".../My Drive"` indexes the Drive for desktop mount by
metadata only (pruning protected names before any syscall, aborting on permission errors), and
`apply --backend fs` renames locally without clobbering. You lose ownership, checksums and shortcut
targets, every rename goes through the Drive for desktop sync queue, and `trash` is not available.
See [macos-fileprovider-notes.md](macos-fileprovider-notes.md) for why the API path is preferred.
