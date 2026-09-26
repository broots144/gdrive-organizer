#!/usr/bin/env bash
# gdrive-organizer in about a minute, on a fake local "My Drive" in a temp folder.
# No Google account, nothing leaves your machine. Run from the repo root after `pip install -e .`
#   bash examples/demo/run.sh          (DEMO_FAST=1 skips the typing effect)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
GO="${GO:-gdrive-organizer}"
W="$(mktemp -d "${TMPDIR:-/tmp}/gdrive-organizer-demo.XXXXXX")"; trap 'rm -rf "$W"' EXIT
D="$W/My Drive"
python3 "$HERE/make_demo_tree.py" "$D"
cp "$HERE/config.json" "$HERE/rules.py" "$W/"
cd "$W"

say() { printf '\n\033[1;36m# %s\033[0m\n' "$*"; [ -n "${DEMO_FAST:-}" ] || sleep 1; }
run() {
  printf '\033[1;32m$\033[0m '
  if [ -n "${DEMO_FAST:-}" ]; then printf '%s\n' "$*"
  else for ((i = 0; i < ${#1}; i += 4)); do printf '%s' "${1:i:4}"; sleep 0.05; done; printf '\n'; sleep 0.4; fi
  eval "$1"
  [ -n "${DEMO_FAST:-}" ] || sleep 1.5
}
tree_top() { (cd "$D" && find . -maxdepth 2 -not -path '*/.*' -not -name '.' | sed 's|^\./||' | sort | head -n "${1:-40}"); }

say "A messy Drive: loose files, an old Dropbox dump, a live camera folder, a git repo, and a protected folder"
run 'tree_top 30'
say "1. Index metadata only (the protected folder is skipped before it is ever listed)"
run '$GO index-fs --root "$D" --db index.sqlite --config config.json --allow-any-root 2>&1 | tail -n 1'
say "2. The assistant reads an aggregate report, never raw file lists"
run '$GO report --db index.sqlite --config config.json | grep -E "^files|^protected|loose files|LIVE"'
say "3. The taxonomy is written as rules: SQL over the index plus a destination"
run 'grep -E "dict\(id=" rules.py | cut -c1-95'
say "4. Compile the rules into a manifest"
run '$GO plan --db index.sqlite --config config.json --rules rules.py --out plan.jsonl'
say "5. Validate: protected ground, collisions, atomic units, live folders; then pin the sha"
run '$GO validate --db index.sqlite --config config.json --manifest plan.jsonl | tee v.txt | grep -E "^ops|sha256"'
SHA="$(sed -n 's/^manifest sha256=//p' v.txt)"
say "6. You execute it yourself; every step is journaled"
run '$GO apply --backend fs --root "$D" --db index.sqlite --config config.json --manifest plan.jsonl --allow-any-root --execute --confirm-sha "$SHA" --pause 0 | tail -n 1'
run 'tree_top 40'
say "7. And it all comes back with one command"
run '$GO apply --backend fs --root "$D" --db index.sqlite --config config.json --manifest plan.jsonl --allow-any-root --execute --confirm-sha "$SHA" --pause 0 --undo'
run 'ls "$D"'
say "Legal - do not touch was never read, listed or moved."
