#!/usr/bin/env python3
"""Fail when a change touches code without touching STATE.md (repo-state pattern, D-013).

  python3 scripts/state_guard.py BASE HEAD "PR title"   # CI: diff BASE...HEAD
  python3 scripts/state_guard.py                         # local: origin/main...HEAD, last subject

Code paths: the package, scripts/ and pyproject.toml. Escape hatch: a title starting with
chore: / chore(...): / deps: / deps(...): (Dependabot uses "chore(deps):"), or an older
Dependabot "Bump ..." title.
"""
from __future__ import annotations

import re
import subprocess
import sys

CODE = ("gdrive_organizer/", "scripts/")
CODE_FILES = ("pyproject.toml",)
ESCAPE = re.compile(r"^(chore|deps)(\([^)]*\))?!?:|^Bump ")


def needs_state(files, title):
    """Return the code files that need a STATE.md change, or [] when the change is fine."""
    if ESCAPE.match(title or ""):
        return []
    if "STATE.md" in files:
        return []
    return [f for f in files if f.startswith(CODE) or f in CODE_FILES]


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def main(argv):
    if len(argv) >= 3:
        base, head, title = argv[0], argv[1], argv[2]
    else:
        base, head = "origin/main", "HEAD"
        title = git("log", "-1", "--format=%s").strip()
    files = [f for f in git("diff", "--name-only", f"{base}...{head}").splitlines() if f]
    missing = needs_state(files, title)
    if missing:
        print("code changed without a STATE.md update: " + ", ".join(missing))
        print("update STATE.md in the same change, or title it chore:/deps: if it is upkeep only")
        return 1
    print(f"state guard ok ({len(files)} files changed)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
