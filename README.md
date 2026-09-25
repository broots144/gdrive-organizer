# gdrive-organizer

Guarded, reversible reorganization of a large Google Drive (My Drive), built to be driven by a
human with an AI assistant such as Claude Code, without ever handing the assistant your files.

- **Metadata-only index** through the Drive API (`drive.metadata.readonly`). No downloads, no
  local hydration, and exact duplicates found server side from `md5Checksum`.
- **Protected folders** excluded by Drive ID and name *before* they are listed. Their contents are
  never requested, and no operation may touch them or move any of their ancestors.
- **Aggregate reports** are the only thing the assistant reads. 50,000 raw paths are over a
  million tokens; the report is a few hundred lines, with sensitive-looking names masked.
- **Validated manifests**: collisions, atomic units (git repos, bundles, backup sets), live backup
  targets, files you don't own, and protected paths are all rejected before anything runs.
- **Journaled execution with undo**: write-ahead journal, crash reconciliation, sha-pinned
  manifest, ID-based undo. Drive moves never overwrite; local renames use `RENAME_EXCL`.

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
mkdir -p private && cp examples/config.example.json private/config.json   # edit it
```

OAuth (once, about 20 minutes): Google Cloud console, new project, enable Drive API, OAuth
consent screen (External, Testing, add yourself), OAuth client ID of type Desktop app. Save the
JSON as `private/client_secret.json`. Each phase gets its own least-privilege token.

## Workflow

```bash
gdrive-organizer index-drive --db private/index.sqlite --config private/config.json --client-secret private/client_secret.json
gdrive-organizer report      --db private/index.sqlite --config private/config.json > private/report.txt
gdrive-organizer peek        --db private/index.sqlite --config private/config.json --client-secret private/client_secret.json --limit 300
# write rules -> generate private/plan.jsonl (see examples/plan.example.jsonl)
gdrive-organizer validate    --db private/index.sqlite --config private/config.json --manifest private/plan.jsonl
gdrive-organizer apply --backend drive --db private/index.sqlite --config private/config.json --manifest private/plan.jsonl
gdrive-organizer apply ... --execute --confirm-sha <sha from validate> --max-ops 20     # canary
gdrive-organizer apply ... --execute --confirm-sha <sha> --batch 50 --pause 60
gdrive-organizer apply ... --execute --confirm-sha <sha> --undo                        # roll back
```

Guidance that matters more than any flag:

- Move **folders**, not files, wherever a folder is coherent. One folder move is one API call.
- Have the assistant write **rules** (predicates plus destination templates), then generate the
  manifest from them with code. Rules are reviewable and reproducible; 50,000 per-file decisions
  are neither.
- Deleting stale backups is a separate, later decision. Drive empties its trash after 30 days.

## Using it with Claude Code

- `.claude/settings.json` (committed) turns on the Bash sandbox, denies all writes under
  `~/Library/CloudStorage` and reads of the Drive for desktop cache, and blocks the Read tool on the
  mount, OAuth files and the quarantine list. The assistant cannot move anything, even by mistake.
- `python3 scripts/make_local_settings.py` adds OS-level denies for **your** protected folder
  names to `.claude/settings.local.json`, which is gitignored.
- `CLAUDE.md` tells the assistant the rules: read only reports, write rules not rows, never run
  `apply --execute`. You run execution yourself in a terminal.

## Local mount fallback

No OAuth? `gdrive-organizer index-fs --root ".../My Drive"` indexes the mount by metadata only
(pruning protected names before any syscall, aborting on permission errors), and
`apply --backend fs` renames locally without clobbering. You lose ownership, checksums and
shortcut targets, and every rename goes through the Drive for desktop sync queue.

## Contributing

`python3 tests/test_fs_local.py && python3 tests/test_drive_mock.py`. Run
`bash scripts/install_hooks.sh` to get the leak check (`scripts/leak_check.py`), which blocks
commits containing strings from your private config or a non-noreply git email.

MIT licensed.
