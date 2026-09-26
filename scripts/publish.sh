#!/usr/bin/env bash
# First publish of this folder as a GitHub repo. Two steps on purpose:
#   bash scripts/publish.sh          local only: git init, noreply identity, hooks, tests,
#                                    leak check, first commit. Prints what would be published.
#   bash scripts/publish.sh --push   creates github.com/$OWNER/$REPO and pushes. Private by
#                                    default; VISIBILITY=public bash scripts/publish.sh --push
#                                    to publish, or later: gh repo edit --visibility public
# Run it yourself (a terminal tab, or "! bash scripts/publish.sh" in Claude Code, which runs
# outside the sandbox). gh is Go based and can fail TLS inside the macOS sandbox.
set -euo pipefail
OWNER="${OWNER:-broots144}"
REPO="${REPO:-gdrive-organizer}"
VISIBILITY="${VISIBILITY:-private}"
DESC="Guarded, reversible Google Drive reorganizer for use with AI assistants: metadata-only index, protected folders, rules compiled to validated manifests, journaled moves with undo, and duplicate removal only to trash."
cd "$(dirname "$0")/.."

# One-time placement of files the delivery tool could not write (.claude/, .github/workflows/).
if [ -d _install ]; then
  mkdir -p .claude .github/workflows
  [ -f _install/claude-settings.json ] && mv -n _install/claude-settings.json .claude/settings.json
  [ -f _install/test.yml ] && mv -n _install/test.yml .github/workflows/test.yml
  rmdir _install 2>/dev/null || true
fi
# Your protected-folder sandbox denies, from private/config.json, into gitignored local settings.
[ -f private/config.json ] && python3 scripts/make_local_settings.py

command -v gh >/dev/null || { echo "need GitHub CLI: brew install gh && gh auth login"; exit 1; }
LOGIN=$(gh api user --jq .login)
ID=$(gh api user --jq .id)
[ "$LOGIN" = "$OWNER" ] || { echo "gh is logged in as $LOGIN, expected $OWNER"; exit 1; }

if [ "${1:-}" = "--push" ]; then
  [ -d .git ] && git rev-parse HEAD >/dev/null 2>&1 || { echo "run without --push first"; exit 1; }
  python3 scripts/leak_check.py --all
  if [ -d .github/workflows ] && ! gh auth status 2>&1 | grep -q "workflow"; then
    echo "your gh token lacks the 'workflow' scope needed to push .github/workflows:"
    echo "  gh auth refresh -h github.com -s workflow"; exit 1
  fi
  gh repo create "$OWNER/$REPO" "--$VISIBILITY" --source . --remote origin --push --description "$DESC"
  gh repo edit "$OWNER/$REPO" --add-topic google-drive,macos,fileprovider,claude-code,file-organization >/dev/null || true
  echo "published ($VISIBILITY): https://github.com/$OWNER/$REPO"
  exit 0
fi

[ -d .git ] || git init -q -b main
# Publish under the GitHub noreply address so no personal email lands in public history.
git config user.name "$OWNER"
git config user.email "${ID}+${OWNER}@users.noreply.github.com"
bash scripts/install_hooks.sh
python3 tests/test_fs_local.py >/dev/null && echo "tests: fs suite passed"
python3 scripts/leak_check.py --all
git add -A
if git diff --cached --quiet; then
  echo "nothing new to commit"
else
  git commit -q -F - <<'MSG'
Initial release of gdrive-organizer

Guarded, reversible reorganization of Google Drive (My Drive):
Drive API metadata index with protected folders excluded by ID, aggregate
reports for LLM review, bounded content peeks via the API, a manifest
validator, and a journaled apply/undo executor with drive and fs backends.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
MSG
fi
echo
echo "files that will be public:"; git ls-files | sed 's/^/  /'
echo
echo "author: $(git log -1 --format='%an <%ae>')"
echo "review the list above, then run: bash scripts/publish.sh --push"
