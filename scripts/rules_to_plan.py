#!/usr/bin/env python3
"""Turn a rules file into a move manifest (JSON Lines) for `gdrive-organizer validate`.

  python3 scripts/rules_to_plan.py --db private/index.sqlite --config private/config.json \
      --rules private/rules.py --out private/plan.jsonl

The rules file (see examples/rules.example.py) defines:
  RULES           list of dicts: id, where, dst. `where` is a SQL WHERE clause over `items`
                  aliased as i (REGEXP available); `dst` is a template with {lh} (name
                  lowercase-hyphenated, files unchanged) and {orig} (original path, folders
                  lowercase-hyphenated). First match wins.
  DUMP            optional: path of a dump folder you want emptied
  DUMP_NEST       optional: subfolder name for dump children that collide (default from-<dump>)
  DUPLICATES_DIR  optional: where colliding identical files go (default archive/duplicates)
  UNSORTED_DIR    optional: where units that cannot merge go (default archive/unsorted)
  LIVE_TOPS       optional: top-level names reported as the live taxonomy

Output ops are mkdir and move only. Never renames files or anything inside a moved unit.
Collisions (several sources for one target, a target that already exists case-insensitively, or
a target that must also hold other targets) become an mkdir plus a merge: a source outside the
dump moves its children in and its empty shell stays put; a direct child of the dump moves whole
to <target>/<DUMP_NEST> so the dump still empties. Sensitive sources get override "sensitive".
Not owned, not movable, recently modified, protected and dot items stay in place.
Output is counts only, never row paths.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import runpy
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gdrive_organizer import core as g  # noqa: E402

FOLDER_KINDS = ("dir", "bundle", "heavy")


def lh(name: str, kind: str) -> str:
    if kind not in FOLDER_KINDS:
        return name
    ext = ""
    if kind == "bundle" and g.ext_of(name) in g.BUNDLE_EXTS:
        ext = g.ext_of(name)
        name = name[: -len(ext)]
    s = re.sub(r"[\s_]+", "-", name.strip().lower())
    return re.sub(r"-{2,}", "-", s).strip("-") + ext


def ancestors(pk: str):
    parts = pk.split("/")
    for i in range(len(parts) - 1, 0, -1):
        yield "/".join(parts[:i])


def human(n) -> str:
    n = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--rules", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)

    cfg = g.load_config(a.config)
    guard = g.Guard(cfg)
    db = g.open_db(a.db)
    db.create_function("REGEXP", 2, lambda p, s: s is not None and re.search(p, s) is not None)
    g.load_protected(db, guard)
    ns = runpy.run_path(a.rules)
    rules, dump_pk = ns["RULES"], g.pkey(ns.get("DUMP", ""))
    live_tops = [g.pkey(t) for t in ns.get("LIVE_TOPS", [])]
    dump_nest = ns.get("DUMP_NEST") or "from-" + lh(dump_pk.rsplit("/", 1)[-1] or "dump", "dir")
    dup_dir = ns.get("DUPLICATES_DIR", "archive/duplicates").strip("/")
    unsorted_dir = ns.get("UNSORTED_DIR", "archive/unsorted").strip("/")
    recent = time.time() - cfg["recent_days"] * 86400

    rows = db.execute("SELECT * FROM items").fetchall()
    by_key = {r["key"]: r for r in rows}
    by_pk = collections.defaultdict(list)
    children = collections.defaultdict(list)
    for r in rows:
        by_pk[r["pathkey"]].append(r)
        if r["parent_key"]:
            children[r["parent_key"]].append(r["key"])
    dir_path = {pk: rs[0]["path"] for pk, rs in by_pk.items()
                if len(rs) == 1 and rs[0]["kind"] == "dir"}
    newest = collections.defaultdict(float)
    for r in rows:
        if r["kind"] in ("file", "gdoc"):
            for anc in ancestors(r["pathkey"]):
                newest[anc] = max(newest[anc], r["mtime"] or 0)

    def orig(r):
        parts = r["path"].split("/")
        segs = [lh(p, "dir") for p in parts[:-1]] + [lh(parts[-1], r["kind"])]
        return "/".join(segs)

    def in_dump(r):
        return bool(dump_pk) and g.is_within(r["pathkey"], dump_pk) and r["pathkey"] != dump_pk

    blocked = collections.Counter()

    def gate(r):
        """Reason this item cannot move, or None."""
        if guard.path_violation(r["path"]):
            return "protected"
        if r["kind"] == "dot":
            return "dot_entry"
        if r["owned_by_me"] == 0:
            return "not_owned"
        if r["can_move"] == 0:
            return "cannot_move"
        if r["under_atomic"] and not r["atomic_root"]:
            return "inside_atomic"
        if r["kind"] in FOLDER_KINDS:
            if newest.get(r["pathkey"], 0) > recent:
                return "recent"
        elif (r["mtime"] or 0) > recent:
            return "recent"
        return None

    # 1. evaluate rules, first match wins
    assign, rule_of = {}, {}
    for rule in rules:
        for (key,) in db.execute(f"SELECT i.key FROM items i WHERE {rule['where']}"):
            if key in assign:
                continue
            r = by_key[key]
            assign[key] = rule["dst"].format(lh=lh(r["name"], r["kind"]), orig=orig(r))
            rule_of[key] = rule["id"]
    # keep outermost only (sources must be disjoint)
    assigned_pks = {by_key[k]["pathkey"] for k in assign}
    for k in list(assign):
        if any(anc in assigned_pks for anc in ancestors(by_key[k]["pathkey"])):
            del assign[k]
            blocked["nested_in_other_source"] += 1
    for k in list(assign):
        reason = gate(by_key[k]) or ("protected_dst" if guard.path_violation(assign[k]) else None)
        if reason:
            del assign[k]
            blocked[reason] += 1

    # 2. resolve collisions into mkdir + merge
    shells = set()
    counts = collections.Counter()
    while True:
        groups = collections.defaultdict(list)
        for k, t in assign.items():
            groups[g.pkey(t)].append(k)
        need_parent = {anc for t in assign.values() for anc in ancestors(g.pkey(t))}
        changed = False
        for pk, keys in groups.items():
            exists_as_dir = pk in dir_path
            if len(keys) == 1 and pk not in by_pk and pk not in need_parent:
                continue
            if pk in by_pk and not exists_as_dir:
                for k in keys:  # target taken by a file or ambiguous names: leave in place
                    del assign[k]
                    blocked["target_occupied"] += 1
                changed = True
                continue
            target = assign[keys[0]]
            files = sorted((k for k in keys if by_key[k]["kind"] not in FOLDER_KINDS),
                           key=lambda k: by_key[k]["path"])
            keeper = files[0] if files and len(keys) == len(files) and not exists_as_dir \
                and pk not in need_parent else None
            for k in keys:
                r = by_key[k]
                if k == keeper:
                    continue
                if r["kind"] not in FOLDER_KINDS:
                    kr = by_key[keeper] if keeper else None
                    if kr is not None and r["md5"] and r["md5"] == kr["md5"]:
                        assign[k] = dup_dir + "/" + orig(r)
                        counts["file_collision_to_duplicates"] += 1
                    else:
                        del assign[k]
                        blocked["file_name_collision"] += 1
                    changed = True
                elif in_dump(r) and r["parent_key"] == by_pk[dump_pk][0]["key"]:
                    if target.endswith("/" + dump_nest):
                        assign[k] = unsorted_dir + "/" + orig(r)
                    else:
                        assign[k] = target + "/" + dump_nest
                    counts["dump_child_nested_under_target"] += 1
                    changed = True
                elif r["kind"] != "dir" or r["atomic_root"]:
                    assign[k] = unsorted_dir + "/" + orig(r)  # unit cannot be split
                    counts["unit_collision_to_unsorted"] += 1
                    changed = True
                else:
                    del assign[k]
                    shells.add(k)
                    counts["merged_sources"] += 1
                    for ck in children[k]:
                        c = by_key[ck]
                        reason = gate(c)
                        if reason:
                            blocked[f"merge_child_{reason}"] += 1
                            continue
                        assign[ck] = target + "/" + lh(c["name"], c["kind"])
                        rule_of[ck] = rule_of.get(k, "merge")
                    changed = True
            if changed:
                break
        if not changed:
            break

    # 3. canonical spelling for existing folders (rule 5), mkdirs, moves
    def canon(path):
        parts, out = path.split("/"), []
        for i, p in enumerate(parts):
            pk = g.pkey("/".join(parts[: i + 1]))
            out.append(dir_path[pk].rsplit("/", 1)[-1] if pk in dir_path else p)
        return "/".join(out)

    moves = sorted(((k, canon(t)) for k, t in assign.items()), key=lambda kt: kt[1].casefold())
    mk = set()
    for _, t in moves:
        for anc in ancestors(g.pkey(t)):
            if anc not in dir_path:
                mk.add(anc)
    mk_paths = {}
    for _, t in moves:
        parts = t.split("/")
        for i in range(1, len(parts)):
            p = "/".join(parts[:i])
            if g.pkey(p) in mk:
                mk_paths.setdefault(g.pkey(p), p)
    ops = [{"op": "mkdir", "dst": p} for p in sorted(mk_paths.values(),
                                                     key=lambda p: (p.count("/"), p.casefold()))]
    for k, t in moves:
        r = by_key[k]
        op = {"op": "move", "src": r["path"], "src_key": k, "dst": t,
              "rule": rule_of.get(k, "merge")}
        if r["sensitive"]:
            op["override"] = ["sensitive"]
        ops.append(op)
    with open(a.out, "w", encoding="utf-8") as fh:
        for op in ops:
            fh.write(json.dumps(op, ensure_ascii=False) + "\n")

    # 4. counts
    moved_pks = {by_key[k]["pathkey"]: t for k, t in moves}

    def moved_to(r):
        if r["pathkey"] in moved_pks:
            return moved_pks[r["pathkey"]]
        for anc in ancestors(r["pathkey"]):
            if anc in moved_pks:
                return moved_pks[anc]
        return None

    split = collections.defaultdict(lambda: [0, 0])
    by_rule = collections.defaultdict(lambda: [0, 0, 0])
    move_rule = {by_key[k]["pathkey"]: rule_of.get(k, "merge") for k, _ in moves}
    for k, _ in moves:
        by_rule[rule_of.get(k, "merge")][0] += 1
    dump_left = collections.Counter()
    for r in rows:
        t = moved_to(r)
        if r["kind"] in ("file", "gdoc"):
            if t is None:
                bucket = "left in place"
            else:
                top = g.pkey(t.split("/")[0])
                bucket = "live taxonomy" if top in live_tops else top + "/*"
                src = next(pk for pk in [r["pathkey"]] + list(ancestors(r["pathkey"]))
                           if pk in move_rule)
                by_rule[move_rule[src]][1] += 1
                by_rule[move_rule[src]][2] += r["size"] or 0
            split[bucket][0] += 1
            split[bucket][1] += r["size"] or 0
        if in_dump(r) and t is None:
            dump_left[r["kind"]] += 1

    print("per rule (moves, files, bytes):")
    for rid, (m, f, b) in by_rule.items():
        print(f"  {rid:<14} moves={m:<4} files={f:<6} bytes={b} ({human(b)})")
    print("resolution:", dict(counts), "| emptied shells left in place:", len(shells))
    print("blocked (left in place):", dict(blocked))
    print("split (files, bytes):")
    for bucket, (f, b) in sorted(split.items()):
        print(f"  {bucket:<14} files={f:<6} bytes={b} ({human(b)})")
    print(f"items remaining in dump: {sum(dump_left.values())} {dict(dump_left)}")
    print(f"ops={len(ops)} mkdir={len(mk_paths)} move={len(moves)} -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
