# gdrive-organizer

Guarded, reversible reorganization of a large Google Drive (My Drive), built to be driven by a
human with an AI assistant such as Claude Code, without ever handing the assistant your files.

You describe the target layout as **rules**. The tool turns them into a **manifest** of moves, a
validator rejects anything unsafe, and **you** run the journaled executor, which can undo itself.
The assistant only ever sees aggregate reports and counts.

- **Metadata-only index** through the Drive API (`drive.metadata.readonly`). No downloads, no
  local hydration, and exact duplicates found server side from `md5Checksum`.
- **Protected folders** excluded by Drive ID and name *before* they are listed. Their contents are
  never requested, and no operation may touch them or move any of their ancestors.
- **Aggregate reports** are the only thing the assistant reads. 50,000 raw paths are over a
  million tokens; the report is a few hundred lines, with sensitive-looking names masked.
- **Rules, not rows**: SQL predicates over the index plus destination templates, compiled into a
  manifest by `scripts/rules_to_plan.py`. Reviewable, reproducible, and rerunnable.
- **Validated manifests**: collisions, atomic units (git repos, bundles, backup sets), live backup
  targets, files you don't own, and protected paths are all rejected before anything runs.
- **Journaled execution with undo**: write-ahead journal, crash reconciliation, sha-pinned
  manifest, ID-based undo. Drive moves never overwrite; local renames use `RENAME_EXCL`.

**It never deletes your files.** Manifests contain only `mkdir` and `move`. The one exception is
undo: it trashes folders the manifest created, and only if they are empty again (Drive keeps
trash for 30 days). Deleting stale backups or duplicates is left to you, later, on purpose.

Why not just walk `~/Library/CloudStorage/...`? On current macOS that mount forces whole-file
downloads and has no command-line eviction. See [docs/macos-fileprovider-notes.md](docs/macos-fileprovider-notes.md).

> Status: alpha. Tested end to end on fake trees (Linux and macOS CI) and against a mocked Drive
> API. Start with a canary of 20 operations and check the result in the Drive web UI.
> Not affiliated with Google.

## Install

```bash
git clone https://github.com/broots144/gdrive-organizer && cd gdrive-organizer
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[all]"
bash scripts/install_hooks.sh                                             # leak check on commit
mkdir -p private && cp examples/config.example.json private/config.json   # edit it
```

Everything personal lives in `private/`, which is gitignored: your config (protected folder
names and IDs), OAuth client and tokens, the index, reports, rules, manifests and journals.

OAuth (once, about 20 minutes): Google Cloud console, new project, enable Drive API, OAuth
consent screen (External, Testing, add yourself), OAuth client ID of type Desktop app. Save the
JSON as `private/client_secret.json`. Each phase gets its own least-privilege token; `apply`
asks for write access the first time you execute.

## Workflow

```bash
# 1. index (metadata only) and report
gdrive-organizer index-drive --db private/index.sqlite --config private/config.json --client-secret private/client_secret.json
gdrive-organizer report      --db private/index.sqlite --config private/config.json > private/report.txt
# optional: bounded content snippets for ambiguous names (never sensitive or protected ones)
gdrive-organizer peek        --db private/index.sqlite --config private/config.json --client-secret private/client_secret.json --limit 300

# 2. rules -> manifest -> validate (repeat until zero errors)
cp examples/rules.example.py private/rules.py                          # edit it
python3 scripts/rules_to_plan.py --db private/index.sqlite --config private/config.json --rules private/rules.py --out private/plan.jsonl
gdrive-organizer validate    --db private/index.sqlite --config private/config.json --manifest private/plan.jsonl

# 3. execute yourself, in a terminal (dry run unless --execute)
gdrive-organizer apply --backend drive --db private/index.sqlite --config private/config.json --manifest private/plan.jsonl
gdrive-organizer apply ... --execute --confirm-sha <sha from validate> --max-ops 20     # canary
gdrive-organizer apply ... --execute --confirm-sha <sha> --batch 50 --pause 60
gdrive-organizer apply ... --execute --confirm-sha <sha> --undo                        # roll back
```

### Writing rules

A rules file is plain Python defining `RULES`: each rule is a SQL `WHERE` clause over the `items`
table plus a destination template. First match wins. See
[examples/rules.example.py](examples/rules.example.py) for the columns and a full example.

```python
dict(id="finance", where="i.depth=0 AND i.kind='dir' AND i.pathkey IN ('taxes','receipts')",
     dst="finance/{lh}")      # {lh}: folder name lowercase-hyphenated; files are never renamed
```

What `rules_to_plan.py` does for you:

- Moves the **outermost** match only, so sources never overlap and units are never split.
- Leaves in place anything protected, not owned by you, not movable, inside an atomic unit, a
  dot entry, or modified within `recent_days` (a live sync or backup target).
- Adds `"override": ["sensitive"]` to each sensitive-named source, so every exception is visible
  in the manifest instead of hidden behind a global flag.
- Resolves **collisions** (two sources for one target, or a target that already exists with
  different case): the target becomes an `mkdir` and the sources merge their contents into it.
  Their emptied shells stay in place. Identical colliding files go to `archive/duplicates`.
- Optionally empties a **dump folder** (`DUMP = "old dropbox"`): its colliding children are moved
  whole into `<target>/from-old-dropbox` so the dump really ends up empty.
- Prints counts per rule and a live, archive and left-in-place split. It never prints paths.

Guidance that matters more than any flag:

- Move **folders**, not files, wherever a folder is coherent. One folder move is one API call.
- Keep app landing folders (scanner targets, "Saved from Chrome") where the app expects them.
- End a dump-folder rule set with a catch-all to `archive/unsorted/{orig}`: uncertain items stay
  findable at their original relative path instead of being guessed into the wrong place.

## Using it with Claude Code

- `.claude/settings.json` (committed) turns on the Bash sandbox, denies all writes under
  `~/Library/CloudStorage` and reads of the Drive for desktop cache, and blocks the Read tool on the
  mount, OAuth files and the quarantine list. The assistant cannot move anything, even by mistake.
- `python3 scripts/make_local_settings.py` adds OS-level denies for **your** protected folder
  names to `.claude/settings.local.json`, which is gitignored.
- `CLAUDE.md` tells the assistant the rules: read only reports, write rules not rows, never run
  `apply --execute`. You run execution yourself in a terminal.
- A good first prompt: *"Read CLAUDE.md. Run the report, summarize it, propose a taxonomy, write
  it as rules in private/rules.py, generate and validate the plan, and show me counts."*
- `gh` is Go based and can fail TLS inside the macOS sandbox. Run GitHub commands yourself, or
  with the `!` prefix in Claude Code, which runs outside the sandbox.

## Local mount fallback

No OAuth? `gdrive-organizer index-fs --root ".../My Drive"` indexes the mount by metadata only
(pruning protected names before any syscall, aborting on permission errors), and
`apply --backend fs` renames locally without clobbering. You lose ownership, checksums and
shortcut targets, and every rename goes through the Drive for desktop sync queue.

## Contributing

```bash
python3 tests/test_fs_local.py && python3 tests/test_drive_mock.py && python3 tests/test_rules_to_plan.py
```

The drive mock test needs the `[api]` extra. `bash scripts/install_hooks.sh` installs the leak
check (`scripts/leak_check.py`), which blocks commits containing strings from your private config
or `.private-patterns` (one literal per line: your name, family names, employers, email) and
commits made with a non-noreply git email. Please never paste real indexes, reports or journals
into issues; see [SECURITY.md](SECURITY.md).

MIT licensed.
