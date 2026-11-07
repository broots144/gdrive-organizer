"""Turn a rules file into a move manifest (JSON Lines) for `gdrive-organizer validate`.

  gdrive-organizer plan --db private/index.sqlite --config private/config.json \
      --rules private/rules.py --out private/plan.jsonl [--collisions-out FILE]

The rules file (see examples/rules.example.py) defines:
  RULES           list of dicts: id, where, dst. `where` is a SQL WHERE clause over `items`
                  aliased as i (REGEXP available); `dst` is a template with {lh} (name
                  lowercase-hyphenated, files unchanged) and {orig} (original path, folders
                  lowercase-hyphenated). First match wins.
  DUMP            optional: path of a dump folder you want emptied
  DUPLICATES_DIR  optional: where colliding identical files go (default archive/duplicates)
  UNSORTED_DIR    optional: where units that cannot merge go (default archive/unsorted)
  LIVE_TOPS       optional: top-level names reported as the live taxonomy
  STRIP_NAMES     optional: exact file names allowed one rename, stripping leading/trailing
                  whitespace (validate refuses such names); each op gets a "note" saying so

Output ops are mkdir and move only. The generator never renames anything; a rule whose `dst` is
a literal path pinned to one Drive ID is how an owner-approved rename is expressed.
Collisions (several sources for one target, a target that already exists case-insensitively, or
a target that must also hold other targets) become an mkdir plus a merge: a source folder, dump
children included, moves its children in and its empty shell stays put for `empty-dirs`. No
holding folder is ever invented. A file whose name is taken at its target goes to DUPLICATES_DIR
if byte-identical (same md5) to the file keeping the name; otherwise it stays where it is, counts
as blocked "file_name_collision", and --collisions-out lists it (Drive IDs of both files and the
contested target) so a rename rule can be written. Google Docs have no md5, so same-name Docs
always take that route. Sensitive sources get override "sensitive".
Not owned, not movable, recently modified, protected and dot items stay in place, as do items
already at their target, anything inside an atomic unit (nested units included), and a file
whose target name is held by a folder. --out is refused once apply has journaled it.
Output is counts only, never row paths.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import runpy
import sys
import time

from . import core as g

PROG = "gdrive-organizer plan"

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
    ap = argparse.ArgumentParser(prog=PROG)
    ap.add_argument("--db", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--rules", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--collisions-out",
                    help="write same-name, different-content clashes here (JSON Lines of IDs)")
    a = ap.parse_args(argv)
    g.refuse_executed_out(a.out)

    cfg = g.load_config(a.config)
    guard = g.Guard(cfg)
    db = g.open_db(a.db)
    g.require_complete(db)
    db.create_function("REGEXP", 2, lambda p, s: s is not None and re.search(p, s) is not None)
    g.load_protected(db, guard)
    ns = runpy.run_path(a.rules)
    rules, dump_pk = ns["RULES"], g.pkey(ns.get("DUMP", ""))
    live_tops = [g.pkey(t) for t in ns.get("LIVE_TOPS", [])]
    if "DUMP_NEST" in ns:
        print("note: DUMP_NEST is ignored; colliding dump children now merge", file=sys.stderr)
    dup_dir = ns.get("DUPLICATES_DIR", "archive/duplicates").strip("/")
    unsorted_dir = ns.get("UNSORTED_DIR", "archive/unsorted").strip("/")
    recent = time.time() - cfg["recent_days"] * 86400
    strip_names = set(ns.get("STRIP_NAMES", []))

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
        if r["under_atomic"]:  # nested roots too: only the outermost unit moves
            return "inside_atomic"
        if r["kind"] in FOLDER_KINDS:
            if newest.get(r["pathkey"], 0) > recent:
                return "recent"
        elif (r["mtime"] or 0) > recent:
            return "recent"
        elif r["name"] != r["name"].strip() and r["name"] not in strip_names:
            return "name_edge_whitespace"  # files keep their names; validate refuses these
        return None

    # 1. evaluate rules, first match wins
    assign, rule_of = {}, {}
    for rule in rules:
        for (key,) in db.execute(f"SELECT i.key FROM items i WHERE {rule['where']}"):
            if key in assign:
                continue
            r = by_key[key]
            name = r["name"].strip() if r["name"] in strip_names else r["name"]
            assign[key] = rule["dst"].format(lh=lh(name, r["kind"]), orig=orig(r))
            rule_of[key] = rule["id"]
    # an item whose target is where it already is stays put (it is not its own collision)
    def drop_in_place():
        for k in [k for k, t in assign.items() if g.pkey(t) == by_key[k]["pathkey"]]:
            del assign[k]
            counts["already_at_target"] += 1

    counts = collections.Counter()
    drop_in_place()
    # gate first, so an outer match that cannot move does not take its inner matches with it
    for k in list(assign):
        reason = gate(by_key[k]) or ("protected_dst" if guard.path_violation(assign[k]) else None)
        if reason:
            del assign[k]
            blocked[reason] += 1
    # then keep outermost only (sources must be disjoint)
    assigned_pks = {by_key[k]["pathkey"] for k in assign}
    for k in list(assign):
        if any(anc in assigned_pks for anc in ancestors(by_key[k]["pathkey"])):
            del assign[k]
            blocked["nested_in_other_source"] += 1

    # 2. resolve collisions into mkdir + merge
    shells = set()
    clashes = []

    def reroute(k, new_target, counter, blocked_reason):
        """Send k to a fallback target; if that is where it is already headed, the fallback is
        occupied too, so leave the item in place instead of looping forever."""
        if g.pkey(new_target) == g.pkey(assign[k]):
            del assign[k]
            blocked[blocked_reason] += 1
        else:
            assign[k] = new_target
            counts[counter] += 1

    while True:
        drop_in_place()  # a merge can hand a child its own path back
        groups = collections.defaultdict(list)
        for k, t in assign.items():
            groups[g.pkey(t)].append(k)
        need_parent = {anc for t in assign.values() for anc in ancestors(g.pkey(t))}
        changed = False
        for pk, keys in groups.items():
            exists_as_dir = pk in dir_path
            if len(keys) == 1 and pk not in by_pk and pk not in need_parent:
                continue
            target = assign[keys[0]]
            files = sorted((k for k in keys if by_key[k]["kind"] not in FOLDER_KINDS),
                           key=lambda k: by_key[k]["path"])
            occupant = None
            others = [r for r in by_pk.get(pk, []) if r["key"] not in keys]  # never itself
            if exists_as_dir and files:
                for k in files:  # a folder holds the name: a file cannot land on it
                    del assign[k]
                    blocked["target_occupied"] += 1
                changed = True
                break
            if others and not exists_as_dir:
                occupant = others[0]  # an indexed file already holds this name
                if occupant["kind"] in FOLDER_KINDS or len(files) != len(keys):
                    for k in keys:  # ambiguous, or folder against file: leave in place
                        del assign[k]
                        blocked["target_occupied"] += 1
                    changed = True
                    continue
            keeper = files[0] if occupant is None and files and len(keys) == len(files) \
                and pk not in need_parent else None
            kr = occupant if occupant is not None else (by_key[keeper] if keeper else None)
            for k in keys:
                r = by_key[k]
                if k == keeper:
                    continue
                if r["kind"] not in FOLDER_KINDS:
                    if kr is not None and r["md5"] and r["md5"] == kr["md5"]:
                        reroute(k, dup_dir + "/" + orig(r), "file_collision_to_duplicates",
                                "duplicates_target_occupied")
                    else:
                        del assign[k]
                        blocked["file_name_collision"] += 1
                        clashes.append({"src_key": k, "dst": target,
                                        "twin_key": kr["key"] if kr is not None else None})
                    changed = True
                elif r["kind"] != "dir" or r["atomic_root"]:
                    # unit cannot be split
                    reroute(k, unsorted_dir + "/" + orig(r), "unit_collision_to_unsorted",
                            "unsorted_target_occupied")
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
        if r["name"] in strip_names and r["kind"] not in FOLDER_KINDS:
            op["note"] = "rename: strips leading/trailing whitespace (STRIP_NAMES)"
        ops.append(op)
    with open(a.out, "w", encoding="utf-8") as fh:
        for op in ops:
            fh.write(json.dumps(op, ensure_ascii=False) + "\n")
    if a.collisions_out:
        with open(a.collisions_out, "w", encoding="utf-8") as fh:
            for c in clashes:
                fh.write(json.dumps(c, ensure_ascii=False) + "\n")

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
        if in_dump(r) and t is None and r["key"] not in shells:
            dump_left[r["kind"]] += 1

    print("per rule (moves, files, bytes):")
    for rid, (m, f, b) in by_rule.items():
        print(f"  {rid:<14} moves={m:<4} files={f:<6} bytes={b} ({human(b)})")
    print("resolution:", dict(counts), "| emptied shells left in place:", len(shells))
    print("blocked (left in place):", dict(blocked))
    print("split (files, bytes):")
    for bucket, (f, b) in sorted(split.items()):
        print(f"  {bucket:<14} files={f:<6} bytes={b} ({human(b)})")
    if dump_pk:
        print(f"items remaining in dump: {sum(dump_left.values())} {dict(dump_left)} "
              "(merged shells excluded)")
    if clashes:
        print(f"name clashes left in place: {len(clashes)}"
              + (f" -> {a.collisions_out}" if a.collisions_out else " (use --collisions-out)"))
    print(f"ops={len(ops)} mkdir={len(mk_paths)} move={len(moves)} -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
