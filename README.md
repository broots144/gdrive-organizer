# gdrive-organizer

[![tests](https://github.com/broots144/gdrive-organizer/actions/workflows/test.yml/badge.svg)](https://github.com/broots144/gdrive-organizer/actions/workflows/test.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)

Reorganize a messy Google Drive with an AI assistant (such as Claude Code) **without handing the
assistant your files, and without anything happening that you did not review and cannot undo.**

You and the assistant look at an aggregate report of your Drive, agree on a folder layout, and
write it down as **rules**. A script compiles the rules into a **manifest** of moves, a validator
rejects anything unsafe, and **you** run the executor yourself. Every step is journaled, and the
whole thing can be rolled back.

## Why

Years of Drive usage leave a root folder full of loose files, an old Dropbox dump, three copies of
everything and folders nobody can explain. An LLM is good at proposing a taxonomy, but letting one
loose on your Drive is a bad idea: it would read private files, guess per file, and act without a
safety net. This tool splits the job so each side does what it is good at:

| The assistant | The tool | You |
|---|---|---|
| reads counts and aggregates, never raw file lists or contents (unless you allow bounded snippets) | indexes metadata only, validates every operation, journals and undoes | approve the plan, run `--execute`, keep the journal |

## What it guarantees

- **Protected folders are never seen.** Excluded by Drive ID and name *before* listing: their
  contents are never requested, never indexed, and no operation may touch them or move a parent.
- **Metadata only by default.** Indexing uses the `drive.metadata.readonly` scope. Content is read
  only by the optional `peek` command, through the API, as short snippets, skipping sensitive names.
- **Nothing runs unvalidated.** `apply` re-validates the manifest and refuses to run unless you pass
  the manifest's sha256 from `validate`, so what executes is exactly what you reviewed.
- **Moves never overwrite or delete.** Reorganization is `mkdir` and `move` only. Collisions are
  resolved, never clobbered; files are never renamed.
- **Removal is opt-in and duplicate-only.** `trash` sends a file to Drive's trash (30 days) only if
  a byte-identical copy (same md5 and size) stays; empty folders only if Drive confirms they are
  empty. Nothing is ever permanently deleted by this tool.
- **Everything is reversible.** A write-ahead journal records each operation by Drive ID;
  `--undo` puts files back and un-trashes. A failed or timed-out operation is reconciled against
  Drive before any retry, so nothing happens twice.

Details and limits: [docs/safety-model.md](docs/safety-model.md).

> **Status: alpha.** Tested in CI on Linux and macOS against a mocked Drive API and fake trees,
> and used on one real My Drive of about 22,000 items (5 rounds, 671 operations, including a
> network timeout that led to the reconcile-before-retry fix). Start with a 20-operation canary.
> Not affiliated with Google.

## Quickstart

```bash
git clone https://github.com/broots144/gdrive-organizer && cd gdrive-organizer
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[all]"
bash scripts/install_hooks.sh                 # blocks commits that contain your private strings
mkdir -p private && cp examples/config.example.json private/config.json
```

1. **OAuth client** (once, about 20 minutes): create your own Google Cloud OAuth client and save it
   as `private/client_secret.json`. Step by step: [docs/oauth-setup.md](docs/oauth-setup.md).
2. **Protect** what must never be touched: put folder names and IDs in `private/config.json`.
3. **Index and report:**
   ```bash
   gdrive-organizer index-drive --db private/index.sqlite --config private/config.json --client-secret private/client_secret.json
   gdrive-organizer report --db private/index.sqlite --config private/config.json > private/report.txt
   ```
4. **Plan** with your assistant: rules in `private/rules.py`, then
   ```bash
   python3 scripts/rules_to_plan.py --db private/index.sqlite --config private/config.json --rules private/rules.py --out private/plan.jsonl
   gdrive-organizer validate --db private/index.sqlite --config private/config.json --manifest private/plan.jsonl
   ```
5. **Execute yourself** (dry run unless `--execute`; the sha comes from `validate`):
   ```bash
   gdrive-organizer apply --backend drive --db private/index.sqlite --config private/config.json --manifest private/plan.jsonl
   gdrive-organizer apply ... --execute --confirm-sha <sha256> --max-ops 20      # canary first
   gdrive-organizer apply ... --execute --confirm-sha <sha256>                   # the rest
   gdrive-organizer apply ... --execute --confirm-sha <sha256> --undo            # roll back
   ```

Everything personal (config, OAuth files, index, reports, rules, manifests, journals) lives in
`private/`, which is gitignored.

**More:** writing rules, emptying a dump folder, removing duplicates and empty folders, content
peeks and the local-mount fallback are in [docs/usage.md](docs/usage.md).

## Using it with Claude Code

The repo is set up so an assistant session is safe by default:

- `.claude/settings.json` turns on the Bash sandbox, denies writes under `~/Library/CloudStorage`
  and reads of the Drive for desktop cache, and blocks reading OAuth files and the quarantine list.
- `python3 scripts/make_local_settings.py` adds OS-level denies for **your** protected folder names
  to `.claude/settings.local.json` (gitignored).
- [CLAUDE.md](CLAUDE.md) gives the assistant its rules: read reports not rows, write rules not
  per-file decisions, never run `apply --execute`.

A good first prompt: *"Read CLAUDE.md. Run the report, summarize it, propose a taxonomy of at most
six top-level folders plus an archive, write it as rules in private/rules.py, generate and
validate the plan, and show me counts and the largest moves."*

## Limitations

- **My Drive only.** Shared drives and "Shared with me" items are not indexed or moved.
- **Google Docs, Sheets and Slides have no checksum**, so they are never treated as duplicates.
- **Your own OAuth client is required.** There is no hosted app; see the setup guide.
- **macOS is the primary platform.** The Drive API path works anywhere Python runs; the
  local-mount fallback is macOS-specific.

## Troubleshooting

| Symptom | What to do |
|---|---|
| `op N failed: TimeoutError(...)` | Rerun the same command with `--retry-failed`. It checks Drive first and records the op as done if the change already landed. |
| `drift: ... changed since indexing` | The item changed after you indexed. Re-index and regenerate the plan. |
| "Google hasn't verified this app" | Expected for a personal OAuth client in Testing mode: choose Advanced, then continue. |
| Sign-in prompt again after a week | Testing-mode refresh tokens expire after 7 days (Google policy); the tool asks you to sign in again. |
| `gh` fails with a TLS error inside Claude Code | The macOS sandbox blocks Go's certificate check; run `gh` yourself or with the `!` prefix. |

## Contributing and security

Contributions are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md). This tool changes real Drives,
so a bug in the guard, validator or executor is a security issue: report it privately as described
in [SECURITY.md](SECURITY.md). Never paste real indexes, reports or journals into issues.

MIT licensed. See [CHANGELOG.md](CHANGELOG.md) for what changed.
