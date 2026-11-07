#!/usr/bin/env python3
"""Refuse to commit private strings: protected folder names, personal names, emails, handles.

Patterns come from files that are never committed:
  .private-patterns        one literal string per line, case-insensitive (# comments allowed)
  private/config.json      every protected_names entry is added automatically
Also refuses a commit identity (author or committer, from config, environment or git's own
guess) that is not a GitHub noreply address.

  python3 scripts/leak_check.py --all          # every file git would track
  python3 scripts/leak_check.py --staged       # staged blobs, as committed (pre-commit hook)
  python3 scripts/leak_check.py --msg FILE     # commit message (commit-msg hook)

Findings print as file:line and pattern number, never the pattern text, so running this inside
an agent session does not echo your private strings into its context.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def patterns():
    pats = []
    pp = os.path.join(REPO, ".private-patterns")
    if os.path.exists(pp):
        with open(pp, encoding="utf-8") as fh:
            pats += [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
    cfg = os.path.join(REPO, "private", "config.json")
    if os.path.exists(cfg):
        with open(cfg, encoding="utf-8") as fh:
            pats += [n for n in json.load(fh).get("protected_names", []) if n]
    return [p.casefold() for p in pats]


def git(*args):
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True).stdout


def staged_text(path):
    """The blob staged for `path` (what the commit will contain), not the working tree copy,
    which can differ or be gone. None when it is not UTF-8 text."""
    r = subprocess.run(["git", "show", f":{path}"], cwd=REPO, capture_output=True)
    if r.returncode != 0:
        return None
    try:
        return r.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


def identity_problems():
    """Author and committer email as git will actually record them: config, the GIT_AUTHOR_* /
    GIT_COMMITTER_* environment, or git's own guess (login@host) when nothing is set."""
    probs = []
    for var in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        r = subprocess.run(["git", "var", var], cwd=REPO, capture_output=True, text=True)
        ident = r.stdout.strip() if r.returncode == 0 else ""
        if not ident:
            continue  # git cannot form an identity, so it cannot commit either
        email = ident[ident.find("<") + 1:ident.rfind(">")] if "<" in ident else ""
        if not email.endswith("@users.noreply.github.com"):
            who = "author" if "AUTHOR" in var else "committer"
            probs.append(f"git {who} email is not a GitHub noreply address (set: git config "
                         "user.email <id>+<login>@users.noreply.github.com, and check the "
                         f"GIT_{who.upper()}_EMAIL and EMAIL environment variables)")
    return probs


def files(mode):
    if mode == "--staged":
        return [f for f in git("diff", "--cached", "--name-only", "--diff-filter=ACMR").splitlines() if f]
    return [f for f in git("ls-files", "--cached", "--others", "--exclude-standard").splitlines() if f]


def scan_text(label, text, pats, hits):
    for n, line in enumerate(text.splitlines(), 1):
        low = line.casefold()
        for i, p in enumerate(pats, 1):
            if p in low:
                hits.append(f"{label}:{n}: matches private pattern #{i}")


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "--all"
    pats = patterns()
    hits = []
    if mode == "--msg":
        with open(sys.argv[2], encoding="utf-8") as fh:
            scan_text("commit message", fh.read(), pats, hits)
    else:
        hits += identity_problems()
        for f in files(mode):
            if mode == "--staged":
                text = staged_text(f)  # the commit's content, even if the tree differs
            else:
                path = os.path.join(REPO, f)
                text = None
                if os.path.isfile(path):
                    try:
                        with open(path, encoding="utf-8") as fh:
                            text = fh.read()
                    except (UnicodeDecodeError, OSError):
                        pass
            if text is not None:
                scan_text(f, text, pats, hits)
            scan_text(f"(filename) {f}", f, pats, hits)
    if not pats:
        print("leak_check: WARNING no private patterns configured (.private-patterns, "
              "private/config.json)", file=sys.stderr)
    for h in hits:
        print("LEAK", h, file=sys.stderr)
    if hits:
        print(f"leak_check: {len(hits)} problem(s); commit blocked", file=sys.stderr)
        return 1
    print(f"leak_check: clean ({len(pats)} private patterns checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
