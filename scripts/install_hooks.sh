#!/usr/bin/env bash
# Install local git hooks that run scripts/leak_check.py on every commit.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -d .git ] || { echo "not a git repo yet (run git init first)"; exit 1; }
cat > .git/hooks/pre-commit <<'EOF'
#!/usr/bin/env bash
exec python3 "$(git rev-parse --show-toplevel)/scripts/leak_check.py" --staged
EOF
cat > .git/hooks/commit-msg <<'EOF'
#!/usr/bin/env bash
exec python3 "$(git rev-parse --show-toplevel)/scripts/leak_check.py" --msg "$1"
EOF
chmod +x .git/hooks/pre-commit .git/hooks/commit-msg
echo "hooks installed: pre-commit, commit-msg"
