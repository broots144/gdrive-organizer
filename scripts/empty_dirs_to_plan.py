#!/usr/bin/env python3
"""Turn empty folders in an index into a manifest of trash ops for `gdrive-organizer validate`.

  python3 scripts/empty_dirs_to_plan.py --db private/index.sqlite --config private/config.json \
      --policy private/dedupe.py --out private/empty-dirs.jsonl

A folder is empty when nothing but folders lies below it (no files, Docs, shortcuts or dot
files); nested empty folders are listed innermost first, as validate requires. From the policy
file (the same one dupes_to_plan uses):
  NEVER_TRASH  path prefixes where nothing is trashed (whole units, app landing folders)
  KEEP_EMPTY   exact folder paths kept even when empty (e.g. your top-level taxonomy)
Folders on protected ground, inside atomic units, not owned by you or not changeable are
skipped. Output is counts only.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import runpy
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gdrive_organizer import core as g  # noqa: E402


def ancestors(pk: str):
    parts = pk.split("/")
    for i in range(len(parts) - 1, 0, -1):
        yield "/".join(parts[:i])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--policy", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)

    cfg = g.load_config(a.config)
    guard = g.Guard(cfg)
    db = g.open_db(a.db)
    g.load_protected(db, guard)
    pol = runpy.run_path(a.policy)
    never = [g.pkey(p) for p in pol.get("NEVER_TRASH", [])]
    keep_empty = {g.pkey(p) for p in pol.get("KEEP_EMPTY", [])}

    rows = db.execute("SELECT * FROM items").fetchall()
    nondir = collections.Counter()
    for r in rows:
        if r["kind"] != "dir":
            for anc in ancestors(r["pathkey"]):
                nondir[anc] += 1
    skipped = collections.Counter()
    empty = []
    for r in rows:
        if r["kind"] != "dir" or nondir.get(r["pathkey"]):
            continue
        pk = r["pathkey"]
        if any(g.is_within(pk, p) for p in never):
            skipped["never_trash_area"] += 1
        elif pk in keep_empty:
            skipped["keep_empty"] += 1
        elif guard.path_violation(r["path"]):
            skipped["protected"] += 1
        elif r["atomic_root"] or r["under_atomic"]:
            skipped["atomic"] += 1
        elif r["owned_by_me"] == 0 or r["can_move"] == 0:
            skipped["not_owned_or_locked"] += 1
        else:
            empty.append(r)
    # a folder qualifies only if every folder below it qualifies too
    chosen = {r["pathkey"] for r in empty}
    blocked = set()
    for r in rows:
        if r["kind"] == "dir" and r["pathkey"] not in chosen:
            blocked.update(ancestors(r["pathkey"]))
    final = [r for r in empty if r["pathkey"] not in blocked]
    skipped["has_kept_subfolder"] += len(empty) - len(final)
    final.sort(key=lambda r: (-r["pathkey"].count("/"), r["pathkey"]))

    with open(a.out, "w", encoding="utf-8") as fh:
        for r in final:
            fh.write(json.dumps({"op": "trash", "src": r["path"], "src_key": r["key"]},
                                ensure_ascii=False) + "\n")
    tops = collections.Counter("/".join(r["path"].split("/")[:2]) for r in final)
    print(f"empty folders to trash: {len(final)}")
    print(f"empty but kept: {sum(skipped.values())} {dict(skipped)}")
    print("by area:", dict(tops.most_common()))
    print(f"-> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
