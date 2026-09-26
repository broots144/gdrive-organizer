#!/usr/bin/env python3
"""Phase 3 gate: validate a move manifest against the index before anything executes.

  gdrive-organizer validate --db private/index.sqlite --config private/config.json --manifest plan.jsonl

Manifest: JSON Lines, applied in order. Paths are relative to My Drive, as shown in the index.
  {"op": "mkdir", "dst": "Archive/Homelab"}
  {"op": "move",  "src": "old-backups/proxmox-2021", "dst": "Archive/Homelab/proxmox-2021"}
  {"op": "move",  "src": "Scan_001.pdf", "src_key": "<id from index>", "dst": "Reference/Vehicles/title.pdf"}
  {"op": "trash", "src": "Archive/dupes/scan.pdf", "src_key": "<id>", "keep_key": "<id of the copy that stays>"}
Optional per-op "override": ["recent", "sensitive", "not_owned"] makes an exception explicit and
auditable instead of a global flag.

trash is the only removal op and exists for exact duplicates: Drive's trash (recoverable for 30
days, and undo un-trashes). It is refused unless keep_key names another indexed file with the same
non-empty md5 and size that this manifest neither trashes nor moves out from under the check.

Rules enforced (errors block execution):
  * nothing touches protected ground: the folder, anything inside it, any ANCESTOR of it
    (moving or renaming an ancestor moves the folder), any protected-looking name in dst
  * every src resolves to exactly one indexed item (Drive allows duplicate names; pass src_key)
  * sources are disjoint subtrees; no op reaches inside something moved earlier
  * dst parent exists (indexed dir or earlier mkdir); dst does not exist (case and Unicode
    insensitive); dst not inside src; no dot-folder destinations
  * items inside an atomic unit (git repo, compose project, bundle, node_modules...) move
    only as the whole unit
  * folders with files modified in the last recent_days are presumed live backup targets
  * sensitive-looking names, items not owned by you, items Drive says you cannot move
  * name validity: <=255 UTF-8 bytes, no slash/NUL/control chars/colon, no edge whitespace
  * Google pointer files (.gdoc etc) keep their extension
  * trash: files only (no folders), a byte-identical keep_key copy must survive the manifest
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
import time

from . import core as g

PROG = "gdrive-organizer validate"


def load_manifest(path: str):
    ops = []
    with open(path, "r", encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            op = json.loads(line)
            op["_line"] = n
            ops.append(op)
    return ops


def ancestors(pk: str):
    parts = pk.split("/")
    for i in range(len(parts) - 1, 0, -1):
        yield "/".join(parts[:i])


def validate(db, cfg, ops):
    guard = g.Guard(cfg)
    g.load_protected(db, guard)
    backend = g.get_meta(db, "backend")
    now = time.time()
    recent = now - cfg["recent_days"] * 86400

    rows = db.execute("SELECT * FROM items").fetchall()
    by_key = {r["key"]: r for r in rows}
    by_pk = collections.defaultdict(list)
    for r in rows:
        by_pk[r["pathkey"]].append(r["key"])
    stats = collections.defaultdict(lambda: [0, 0, 0.0])  # files, bytes, newest mtime
    for r in rows:
        if r["kind"] in ("file", "gdoc"):
            for a in ancestors(r["pathkey"]):
                s = stats[a]
                s[0] += 1
                s[1] += r["size"] or 0
                s[2] = max(s[2], r["mtime"] or 0)

    exists = set(by_pk) | set(guard.protected_pks)
    created, frozen = set(), set()
    src_pks = set()
    errors, warns, plan = [], [], []

    # pass 1: collect sources for the disjointness check, and every key this manifest trashes
    trashed_keys, move_pks = set(), set()
    for op in ops:
        if op.get("op") in ("move", "trash") and "src" in op:
            src_pks.add(g.pkey(op["src"]))
        if op.get("op") == "move" and "src" in op:
            move_pks.add(g.pkey(op["src"]))
        if op.get("op") == "trash" and op.get("src_key"):
            trashed_keys.add(op["src_key"])

    for i, op in enumerate(ops):
        tag = f"line {op['_line']}"
        kind = op.get("op")
        dst = g.nfc(op.get("dst", "")).strip("/")
        dpk = g.pkey(dst)
        over = set(op.get("override", []))
        e0 = len(errors)

        def err(msg):
            errors.append(f"{tag}: {msg}")

        if kind == "trash":
            entry = validate_trash(op, err, by_key, by_pk, guard, recent, over, trashed_keys,
                                   src_pks, move_pks)
            if entry is not None and len(errors) == e0:
                entry.update(i=i, op="trash", line=op["_line"], dst="")
                plan.append(entry)
            continue
        if kind not in ("mkdir", "move"):
            err(f"unknown op {kind!r}")
            continue
        if not dst:
            err("empty dst")
            continue
        v = guard.path_violation(dst)
        if v:
            err(f"dst {v}")
        for p_ in g.name_problems(dst.rsplit("/", 1)[-1]):
            if p_.startswith("colon"):
                warns.append(f"{tag}: dst name has a {p_}")
            else:
                err(f"dst name: {p_}")
        if any(c.startswith(".") for c in dst.split("/")):
            err("dst goes into or creates a dot-folder")
        ppk = g.pkey(g.parent_of(dst))
        if ppk and ppk not in created:
            pk_items = [by_key[k] for k in by_pk.get(ppk, [])]
            dirs = [r for r in pk_items if r["kind"] == "dir"]
            if len(dirs) != 1:
                err("dst parent is not exactly one indexed folder or earlier mkdir "
                    f"(found {len(dirs)})")
            elif dirs[0]["under_atomic"]:
                err("dst parent is inside an atomic unit")
        if dpk in exists:
            err("dst already exists (case/Unicode-insensitive)")
        for a in [dpk] + list(ancestors(dpk)):
            if a in src_pks:
                err("dst is inside a source that this manifest moves (paths are pre-move)")
                break
            if a in frozen and a != dpk:
                err("dst is inside something moved earlier in this manifest")
                break

        entry = {"i": i, "op": kind, "dst": dst, "line": op["_line"]}
        if kind == "move":
            src = g.nfc(op.get("src", "")).strip("/")
            spk = g.pkey(src)
            item = None
            if op.get("src_key"):
                item = by_key.get(op["src_key"])
                if item is None or item["pathkey"] != spk:
                    err("src_key not found or does not match src path")
                    item = None
            else:
                keys = by_pk.get(spk, [])
                if len(keys) != 1:
                    err(f"src resolves to {len(keys)} items; pass src_key")
                else:
                    item = by_key[keys[0]]
            v = guard.path_violation(src)
            if v:
                err(f"src {v}")
            if is_inside(dpk, spk):
                err("dst is inside src")
            for a in ancestors(spk):
                if a in src_pks:
                    err("src lies inside another moved source (make sources disjoint)")
                    break
            if item is not None:
                if item["kind"] == "dot":
                    err("src is a dot entry")
                if item["under_atomic"] and not item["atomic_root"]:
                    err(f"src is inside atomic unit '{item['under_atomic']}'; move the unit")
                if item["sensitive"] and "sensitive" not in over:
                    err("src has a sensitive-looking name (override: sensitive)")
                if item["owned_by_me"] == 0 and "not_owned" not in over:
                    err("src not owned by you: Drive may create a shortcut instead or cut "
                        "collaborators' access (override: not_owned)")
                if item["can_move"] == 0:
                    err("Drive reports you cannot move this item")
                if item["kind"] in ("dir", "bundle", "heavy"):
                    nf, nb, newest = stats.get(spk, (0, 0, 0.0))
                    if newest > recent and "recent" not in over:
                        err(f"folder has files modified in last {cfg['recent_days']} days: "
                            "live backup target? (override: recent)")
                    entry.update(files=nf, bytes=nb)
                else:
                    entry.update(files=1, bytes=item["size"] or 0)
                    if (item["mtime"] or 0) > recent:
                        warns.append(f"{tag}: file modified recently")
                if backend == "fs" and item["kind"] == "gdoc" and g.ext_of(src) != g.ext_of(dst):
                    err("Google pointer file must keep its extension")
                elif item["kind"] == "file" and g.ext_of(src) != g.ext_of(dst):
                    warns.append(f"{tag}: extension changes {g.ext_of(src)} -> {g.ext_of(dst)}")
                if item["kind"] in ("symlink", "shortcut"):
                    warns.append(f"{tag}: moving a link/shortcut moves only the link")
                entry.update(src=src, src_key=item["key"], kind=item["kind"],
                             src_parent_key=item["parent_key"],
                             expect={"ino": item["ino"], "size": item["size"],
                                     "mtime": item["mtime"], "name": item["name"]})
            frozen.add(dpk)
        else:
            created.add(dpk)
        exists.add(dpk)
        if len(errors) == e0:
            plan.append(entry)
    return errors, warns, plan


def validate_trash(op, err, by_key, by_pk, guard, recent, over, trashed_keys, src_pks,
                   move_pks):
    """Checks for one trash op. Returns the plan entry, or None after reporting errors."""
    src = g.nfc(op.get("src", "")).strip("/")
    spk = g.pkey(src)
    item = by_key.get(op.get("src_key") or "")
    if item is None or item["pathkey"] != spk:
        err("trash needs src and a matching src_key")
        return None
    v = guard.path_violation(src)
    if v:
        err(f"src {v}")
    for a in ancestors(spk):
        if a in src_pks:
            err("src lies inside another source of this manifest")
            break
    if item["kind"] not in ("file", "gdoc"):
        err("trash applies to files only, never folders, shortcuts or bundles")
    if item["under_atomic"]:
        err(f"src is inside atomic unit '{item['under_atomic']}'")
    if item["sensitive"] and "sensitive" not in over:
        err("src has a sensitive-looking name (override: sensitive)")
    if item["owned_by_me"] == 0:
        err("src not owned by you: trashing it is not yours to do")
    if (item["mtime"] or 0) > recent and "recent" not in over:
        err("file modified recently (override: recent)")
    keep = by_key.get(op.get("keep_key") or "")
    if keep is None:
        err("keep_key not found: a trash op must name the copy that stays")
    else:
        if keep["key"] == item["key"]:
            err("keep_key is the src itself")
        if not item["md5"] or keep["md5"] != item["md5"] or keep["size"] != item["size"]:
            err("keep_key is not byte-identical (md5 and size must match and be present)")
        if not item["size"]:
            err("empty files are not deduplicated")
        if keep["key"] in trashed_keys:
            err("keep_key is itself trashed by this manifest")
        if keep["kind"] not in ("file", "gdoc"):
            err("keep_key is not a file")
        if guard.path_violation(keep["path"]):
            err("keep_key lies on protected ground")
        # by ID, not path: Drive allows identical names side by side in one folder
        if any(a in move_pks for a in [keep["pathkey"]] + list(ancestors(keep["pathkey"]))):
            err("keep_key is moved by this manifest; dedupe in a separate manifest")
    return {"src": src, "src_key": item["key"], "kind": item["kind"],
            "src_parent_key": item["parent_key"], "files": 1, "bytes": item["size"] or 0,
            "keep_key": op.get("keep_key"),
            "expect": {"ino": item["ino"], "size": item["size"], "mtime": item["mtime"],
                       "name": item["name"], "md5": item["md5"]}}


def is_inside(child_pk: str, anc_pk: str) -> bool:
    return bool(anc_pk) and g.is_within(child_pk, anc_pk)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=PROG)
    ap.add_argument("--db", required=True)
    ap.add_argument("--config")
    ap.add_argument("--manifest", required=True)
    a = ap.parse_args(argv)
    cfg = g.load_config(a.config)
    db = g.open_db(a.db)
    ops = load_manifest(a.manifest)
    errors, warns, plan = validate(db, cfg, ops)
    moves = [p for p in plan if p["op"] == "move"]
    print(f"ops={len(ops)} valid={len(plan)} errors={len(errors)} warnings={len(warns)}")
    trash = [p for p in plan if p["op"] == "trash"]
    print(f"mkdir={sum(1 for p in plan if p['op'] == 'mkdir')} move={len(moves)} "
          f"files_affected={sum(p.get('files', 0) for p in moves)} "
          f"bytes_affected={sum(p.get('bytes', 0) for p in moves)}")
    if trash:
        print(f"trash={len(trash)} bytes_trashed={sum(p['bytes'] for p in trash)}")
    for e in errors[:100]:
        print("ERROR", e)
    for w in warns[:50]:
        print("WARN ", w)
    print(f"manifest sha256={g.sha256_file(a.manifest)}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
