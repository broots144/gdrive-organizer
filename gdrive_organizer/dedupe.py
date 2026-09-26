"""Turn exact duplicates in an index into a manifest of trash ops for `gdrive-organizer validate`.

  gdrive-organizer dedupe --db private/index.sqlite --config private/config.json \
      --policy private/dedupe.py --out private/dedupe.jsonl

Duplicates are files with the same Drive md5Checksum and size (byte-identical; Google Docs have
no md5 and are never included, nor are empty files). In each group one copy is kept and the
others are trashed where the policy allows. The policy file (see examples/dedupe.example.py):
  KEEP_ORDER   path prefixes, most preferred first: the first copy under the earliest prefix is
               kept (unlisted paths rank after all listed ones except LAST); ties go to the
               shortest path, then the oldest file
  LAST         path prefixes kept only when nothing better exists (e.g. archive/duplicates)
  NEVER_TRASH  path prefixes whose files are never trashed (whole units, app landing folders)
Every op names its keep_key, which validate and apply both check. Output is counts only.
"""
from __future__ import annotations

import argparse
import collections
import json
import runpy
import sys
import time

from . import core as g

PROG = "gdrive-organizer dedupe"


def under(pk: str, prefixes) -> bool:
    return any(g.is_within(pk, p) for p in prefixes)


def human(n) -> str:
    n = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=PROG)
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
    keep_order = [g.pkey(p) for p in pol.get("KEEP_ORDER", [])]
    last = [g.pkey(p) for p in pol.get("LAST", [])]
    never = [g.pkey(p) for p in pol.get("NEVER_TRASH", [])]
    recent = time.time() - cfg["recent_days"] * 86400

    rows = db.execute("SELECT * FROM items WHERE kind='file' AND md5 IS NOT NULL AND size>0")
    groups = collections.defaultdict(list)
    for r in rows:
        groups[(r["md5"], r["size"])].append(r)
    groups = {k: v for k, v in groups.items() if len(v) > 1}

    def rank(r):
        pk = r["pathkey"]
        for i, p in enumerate(keep_order):
            if g.is_within(pk, p):
                return (0, i)
        if under(pk, last):
            return (2, 0)
        return (1, 0)

    def why_not(r):
        if under(r["pathkey"], never):
            return "never_trash_area"
        if guard.path_violation(r["path"]):
            return "protected"
        if r["under_atomic"]:
            return "inside_atomic"
        if r["owned_by_me"] == 0:
            return "not_owned"
        if (r["mtime"] or 0) > recent:
            return "recent"
        return None

    ops, kept_in, trashed_from = [], collections.Counter(), collections.Counter()
    skipped = collections.Counter()
    bytes_trash = bytes_left = 0
    for (md5, size), rs in groups.items():
        rs.sort(key=lambda r: (rank(r), len(r["path"]), r["mtime"] or 0, r["key"]))
        keep, rest = rs[0], rs[1:]
        kept_in[keep["path"].split("/")[0] if "/" in keep["path"] else "(root)"] += 1
        for r in rest:
            reason = why_not(r)
            if reason:
                skipped[reason] += 1
                bytes_left += size
                continue
            op = {"op": "trash", "src": r["path"], "src_key": r["key"],
                  "keep_key": keep["key"], "keep": keep["path"]}
            if r["sensitive"]:
                op["override"] = ["sensitive"]
            ops.append(op)
            bytes_trash += size
            parts = r["path"].split("/")
            trashed_from["/".join(parts[:2]) if len(parts) > 2 else parts[0]] += 1

    ops.sort(key=lambda o: o["src"].casefold())
    with open(a.out, "w", encoding="utf-8") as fh:
        for op in ops:
            fh.write(json.dumps(op, ensure_ascii=False) + "\n")

    print(f"duplicate groups: {len(groups)}  extra copies: {sum(len(v) - 1 for v in groups.values())}")
    print(f"trash: {len(ops)} files, {bytes_trash} bytes ({human(bytes_trash)})")
    print(f"not trashed: {sum(skipped.values())} copies, {human(bytes_left)} {dict(skipped)}")
    print(f"sensitive-named among trashed (override set): {sum(1 for o in ops if o.get('override'))}")
    print("trashed copies by area:", dict(trashed_from.most_common()))
    print("kept copies by top folder:", dict(kept_in.most_common()))
    print(f"-> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
