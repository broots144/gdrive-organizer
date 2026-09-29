"""The committed sandbox settings must deny sandboxed writes to everything the owner runs or
trusts outside the sandbox: code (package, venv, scripts, tests, demo, root-level modules, git
hooks and config), Claude Code config (hooks and MCP servers run unsandboxed), and the inputs
`apply --execute` trusts beyond the sha-confirmed manifest (index, config, journals, OAuth files).

No sandbox runs here: this checks the list, using the glob rule of the entries ('*' stays within
one path component; a plain directory entry covers everything below it).

The list cannot name every new root-level DIRECTORY the sandbox may create (./ctypes/, ./sqlite3/),
so the other half is that nothing the owner runs puts the repo root on sys.path ahead of the
standard library: shadow_check() runs the tests and scripts in a scratch copy whose root holds
stdlib-named packages and fails if any of them is imported.
"""
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import tempfile

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


# stdlib modules the package, tests and scripts import (after interpreter start-up)
SHADOWED = ["ctypes", "sqlite3", "hashlib", "unicodedata", "random", "json", "subprocess",
            "tempfile", "argparse", "fnmatch", "zipfile", "copy", "contextlib", "shutil",
            "datetime", "pathlib", "string", "textwrap", "uuid", "base64", "urllib", "http",
            "email", "logging", "importlib", "runpy", "collections", "re", "io", "time", "glob",
            "threading", "platform", "struct", "signal", "locale", "html", "xml", "csv",
            "secrets", "hmac", "getpass", "socket", "ssl", "queue", "math", "heapq", "bisect"]
OWNER_RUNS = [["tests/test_fs_local.py"], ["tests/test_drive_mock.py"],
              ["tests/test_rules_to_plan.py"], ["tests/test_peek_limits.py"],
              ["scripts/rules_to_plan.py", "--help"], ["scripts/dupes_to_plan.py", "--help"],
              ["scripts/empty_dirs_to_plan.py", "--help"]]


def shadow_check():
    tmp = tempfile.mkdtemp(prefix="gdo-shadow-")
    try:
        scratch = os.path.join(tmp, "repo")
        os.mkdir(scratch)
        for d in ("gdrive_organizer", "tests", "scripts"):
            shutil.copytree(os.path.join(ROOT, d), os.path.join(scratch, d),
                            ignore=shutil.ignore_patterns("__pycache__"))
        mark = os.path.join(tmp, "shadow-imported.txt")
        for name in SHADOWED:
            os.mkdir(os.path.join(scratch, name))
            with open(os.path.join(scratch, name, "__init__.py"), "w", encoding="utf-8") as fh:
                fh.write(f"open({mark!r}, 'a').write({name!r} + '\\n')\n")
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        for cmd in OWNER_RUNS:
            r = subprocess.run([sys.executable] + [os.path.join(scratch, cmd[0])] + cmd[1:],
                               cwd=scratch, env=env, capture_output=True, text=True)
            hit = open(mark, encoding="utf-8").read().split() if os.path.exists(mark) else []
            assert not hit, f"{cmd[0]} imported root-level {sorted(set(hit))} instead of the stdlib"
            assert r.returncode == 0, f"{cmd[0]} failed in the scratch copy:\n{r.stdout}\n{r.stderr}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"{len(OWNER_RUNS)} owner-run tests/scripts ignore {len(SHADOWED)} root-level stdlib-named packages")


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
    shadow_check()
    print("ALL SANDBOX SETTINGS TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
