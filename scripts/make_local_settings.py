#!/usr/bin/env python3
"""Write .claude/settings.local.json (gitignored) with OS-level denies for YOUR protected folders.

Reads protected_names from private/config.json and adds, for each name, a Claude Code sandbox
denyRead and denyWrite entry matching that folder anywhere under ~/Library/CloudStorage,
including Drive shortcut-target copies. The committed .claude/settings.json holds only generic
rules; your folder names never enter git. Existing keys in settings.local.json are preserved.

Prints counts only, so running it inside an agent session does not echo the names.
"""
from __future__ import annotations

import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    cfg_path = os.path.join(REPO, "private", "config.json")
    if not os.path.exists(cfg_path):
        print("private/config.json not found; copy examples/config.example.json there first")
        return 1
    with open(cfg_path, encoding="utf-8") as fh:
        names = [n for n in json.load(fh).get("protected_names", []) if n]
    if not names:
        print("no protected_names in private/config.json; nothing to do")
        return 0
    entries = [f"~/Library/CloudStorage/**/{n}" for n in names]
    out_path = os.path.join(REPO, ".claude", "settings.local.json")
    data = {}
    if os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as fh:
            data = json.load(fh)
    sb = data.setdefault("sandbox", {})
    sb["enabled"] = True
    sb["failIfUnavailable"] = True
    sb["allowUnsandboxedCommands"] = False
    fs = sb.setdefault("filesystem", {})
    for key in ("denyRead", "denyWrite"):
        fs[key] = sorted(set(fs.get(key, [])) | set(entries))
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    print(f"wrote {len(entries)} protected folder deny entries to .claude/settings.local.json; "
          "restart Claude Code and check /sandbox > Config")
    return 0


if __name__ == "__main__":
    sys.exit(main())
