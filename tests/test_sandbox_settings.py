"""The committed sandbox settings must deny sandboxed writes to everything the owner runs or
trusts outside the sandbox: code (package, venv, scripts, tests, demo, root-level modules, git
hooks and config), Claude Code config (hooks and MCP servers run unsandboxed), and the inputs
`apply --execute` trusts beyond the sha-confirmed manifest (index, config, journals, OAuth files).

No sandbox runs here: this checks the list, using the glob rule of the entries ('*' stays within
one path component; a plain directory entry covers everything below it).
"""
import fnmatch
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MUST_DENY = [
    "gdrive_organizer/apply.py", ".venv/bin/python", "scripts/leak_check.py",
    "scripts/make_local_settings.py", "pyproject.toml", ".git/hooks/pre-commit", ".git/config",
    "tests/test_drive_mock.py", "tests/sitecustomize.py", "examples/demo/run.sh",
    "examples/demo/make_demo_tree.py", "examples/demo/rules.py", "sitecustomize.py", "json.py",
    "evil.pth", ".claude/settings.json", ".claude/settings.local.json", ".mcp.json",
    "private/config.json", "private/index.sqlite", "private/index.sqlite-journal",
    "private/index.sqlite-wal", "private/plan.jsonl.journal.jsonl",
    "private/plan.jsonl.journal.jsonl.undo.jsonl", "private/token_read.json",
    "private/client_secret.json",
]
MAY_WRITE = ["private/rules.py", "private/plan.jsonl", "private/report.txt"]


def covered(path, entries):
    for e in entries:
        if not e.startswith("./"):
            continue
        e = e[2:].rstrip("/")
        if "*" in e:
            if path.count("/") == e.count("/") and fnmatch.fnmatchcase(path, e):
                return True
        elif path == e or path.startswith(e + "/"):
            return True
    return False


def main():
    with open(os.path.join(ROOT, ".claude", "settings.json"), encoding="utf-8") as fh:
        s = json.load(fh)
    sb = s["sandbox"]
    assert sb["enabled"] is True and sb["allowUnsandboxedCommands"] is False
    deny = sb["filesystem"]["denyWrite"]
    missing = [p for p in MUST_DENY if not covered(p, deny)]
    assert not missing, f"sandbox denyWrite does not cover: {missing}"
    blocked = [p for p in MAY_WRITE if covered(p, deny)]
    assert not blocked, f"the assistant's own outputs must stay writable: {blocked}"
    print(f"sandbox denyWrite covers {len(MUST_DENY)} owner-run paths; "
          f"{len(MAY_WRITE)} assistant outputs stay writable")
    print("ALL SANDBOX SETTINGS TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
