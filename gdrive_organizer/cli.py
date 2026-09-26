"""Command line entry point: gdrive-organizer <command> [options]."""
from __future__ import annotations

import importlib
import sys

from . import __version__

COMMANDS = {
    "index-drive": ("index_drive", "Phase 1: index My Drive via the Drive API (metadata-only scope)"),
    "index-fs": ("index_fs", "Phase 1 fallback: index the local Drive for desktop mount, metadata only"),
    "report": ("report", "Aggregate report over an index (the only thing an LLM should read)"),
    "peek": ("peek", "Phase 2: bounded content snippets for ambiguous files via the Drive API"),
    "plan": ("plan", "Compile a rules file into a move manifest (collisions, merges, gates)"),
    "dedupe": ("dedupe", "Exact duplicates (same md5 and size) to a trash manifest"),
    "empty-dirs": ("empty_dirs", "Empty folders to a trash manifest, innermost first"),
    "validate": ("validate", "Phase 3 gate: validate a move manifest against the index"),
    "apply": ("apply", "Phase 4: execute or undo a validated manifest (dry run by default)"),
}


def usage() -> str:
    lines = [f"gdrive-organizer {__version__}", "", "usage: gdrive-organizer <command> [options]", ""]
    for name, (_, desc) in COMMANDS.items():
        lines.append(f"  {name:<12} {desc}")
    lines.append("")
    lines.append("Run 'gdrive-organizer <command> -h' for command options.")
    return "\n".join(lines)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(usage())
        return 0
    if argv[0] in ("-V", "--version"):
        print(__version__)
        return 0
    cmd = COMMANDS.get(argv[0])
    if cmd is None:
        print(f"unknown command: {argv[0]}\n\n{usage()}", file=sys.stderr)
        return 2
    mod = importlib.import_module(f".{cmd[0]}", __package__)
    return mod.main(argv[1:]) or 0
